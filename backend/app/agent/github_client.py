from __future__ import annotations

import asyncio
import base64
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
LOW_REMAINING_THRESHOLD = 10
MAX_RATE_LIMIT_RETRIES = 3


class GitHubClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.api_call_count = 0
        self._rate_limit_headers: httpx.Headers | None = None
        self._client = httpx.AsyncClient(
            base_url=GITHUB_API_BASE,
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
            timeout=httpx.Timeout(60.0),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _parse_repo(self, repo: str) -> tuple[str, str]:
        parts = repo.strip().split("/")
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(f"Invalid repo slug: {repo!r}")
        return parts[0], parts[1]

    async def _sleep_for_rate_limit_headers(self, headers: httpx.Headers) -> None:
        try:
            remaining = int(headers.get("x-ratelimit-remaining", "9999"))
            reset = int(headers.get("x-ratelimit-reset", "0"))
        except ValueError:
            return
        if remaining >= LOW_REMAINING_THRESHOLD:
            return
        now = int(time.time())
        wait = max(0, reset - now) + 1
        logger.warning(
            "GitHub rate limit low (remaining=%s), sleeping %ss",
            remaining,
            wait,
        )
        await asyncio.sleep(wait)

    def _b64_decode_content(self, raw: str | None) -> str:
        if not raw:
            return ""
        try:
            pad = (-len(raw)) % 4
            data = base64.b64decode(raw + ("=" * pad), validate=False)
            return data.decode("utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Failed to decode base64 GitHub content: %s", exc)
            return ""

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> httpx.Response | None:
        headers = extra_headers or {}
        backoff = 1.0
        for attempt in range(MAX_RATE_LIMIT_RETRIES + 1):
            try:
                if self._rate_limit_headers is not None:
                    await self._sleep_for_rate_limit_headers(self._rate_limit_headers)
                self.api_call_count += 1
                resp = await self._client.request(method, path, params=params, headers=headers)
                self._rate_limit_headers = resp.headers
                await self._sleep_for_rate_limit_headers(resp.headers)

                if resp.status_code == 403:
                    remaining = resp.headers.get("x-ratelimit-remaining")
                    reset = resp.headers.get("x-ratelimit-reset")
                    if remaining == "0" or (
                        "rate limit" in (resp.text or "").lower()
                    ):
                        if attempt >= MAX_RATE_LIMIT_RETRIES:
                            logger.warning(
                                "GitHub rate limited on %s after %s retries",
                                path,
                                MAX_RATE_LIMIT_RETRIES,
                            )
                            return resp
                        try:
                            reset_ts = int(reset or "0")
                            wait = max(backoff, max(0, reset_ts - int(time.time())) + 1)
                        except ValueError:
                            wait = backoff
                        logger.warning(
                            "GitHub rate limit 403 on %s, retry in %.1fs (attempt %s/%s)",
                            path,
                            wait,
                            attempt + 1,
                            MAX_RATE_LIMIT_RETRIES,
                        )
                        await asyncio.sleep(wait)
                        backoff *= 2
                        continue

                return resp
            except httpx.HTTPError as exc:
                logger.warning("GitHub HTTP error on %s: %s", path, exc)
                if attempt >= MAX_RATE_LIMIT_RETRIES:
                    return None
                await asyncio.sleep(backoff)
                backoff *= 2
        return None

    async def get_repo_info(self, repo: str) -> dict[str, Any]:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return {}
        resp = await self._request("GET", f"/repos/{owner}/{name}")
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_repo_info failed for %s: status=%s",
                repo,
                getattr(resp, "status_code", None),
            )
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_repo_info invalid JSON for %s: %s", repo, exc)
            return {}
        return {
            "stars": data.get("stargazers_count"),
            "forks": data.get("forks"),
            "open_issues": data.get("open_issues_count"),
            "pushed_at": data.get("pushed_at"),
            "description": data.get("description"),
            "topics": list(data.get("topics") or []),
        }

    async def get_readme(self, repo: str) -> str:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return ""
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/readme",
            extra_headers={"Accept": "application/vnd.github+json"},
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_readme failed for %s: status=%s",
                repo,
                getattr(resp, "status_code", None),
            )
            return ""
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_readme invalid JSON for %s: %s", repo, exc)
            return ""
        text = self._b64_decode_content(data.get("content"))
        return text[:30000]

    async def _branch_tree_sha(self, owner: str, name: str, branch: str) -> str | None:
        resp = await self._request("GET", f"/repos/{owner}/{name}/commits/{branch}")
        if resp is None or resp.status_code != 200:
            return None
        try:
            data = resp.json()
            tree = (data.get("commit") or {}).get("tree") or {}
            sha = tree.get("sha")
            return str(sha) if sha else None
        except ValueError as exc:
            logger.warning("commits response invalid JSON: %s", exc)
            return None

    async def get_repo_tree(self, repo: str) -> list[str]:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return []
        tree_sha: str | None = None
        for branch in ("main", "master"):
            tree_sha = await self._branch_tree_sha(owner, name, branch)
            if tree_sha:
                break
        if not tree_sha:
            logger.warning("get_repo_tree could not resolve branch for %s", repo)
            return []
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/git/trees/{tree_sha}",
            params={"recursive": "1"},
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_repo_tree failed for %s: status=%s",
                repo,
                getattr(resp, "status_code", None),
            )
            return []
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_repo_tree invalid JSON for %s: %s", repo, exc)
            return []
        tree = data.get("tree") or []
        paths: list[str] = []
        for item in tree:
            if item.get("type") == "blob" and item.get("path"):
                paths.append(str(item["path"]))
        return paths[:500]

    def _normalize_issue_item(self, item: dict[str, Any]) -> dict[str, Any]:
        body = item.get("body") or ""
        if isinstance(body, str) and len(body) > 2000:
            body = body[:2000]
        labels_raw = item.get("labels") or []
        labels: list[dict[str, Any]] = []
        if isinstance(labels_raw, list):
            for lab in labels_raw:
                if isinstance(lab, dict):
                    labels.append(
                        {
                            "name": lab.get("name"),
                            "color": lab.get("color"),
                        }
                    )
        reactions = item.get("reactions")
        if reactions is not None and not isinstance(reactions, dict):
            reactions = None
        return {
            "number": item.get("number"),
            "title": item.get("title"),
            "body": body,
            "state": item.get("state"),
            "html_url": item.get("html_url"),
            "created_at": item.get("created_at"),
            "updated_at": item.get("updated_at"),
            "comments": item.get("comments"),
            "labels": labels,
            "reactions": reactions,
        }

    async def search_issues(
        self, repo: str, query: str, max_results: int = 20,
        sort: str = "updated", order: str = "desc",
    ) -> list[dict[str, Any]]:
        try:
            self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return []
        q = f"repo:{repo} {query}".strip()
        results: list[dict[str, Any]] = []
        page = 1
        while len(results) < max_results:
            per_page = min(30, max_results - len(results))
            if per_page <= 0:
                break
            params: dict[str, Any] = {"q": q, "per_page": per_page, "page": page}
            if sort:
                params["sort"] = sort
                params["order"] = order
            resp = await self._request(
                "GET",
                "/search/issues",
                params=params,
            )
            if resp is None or resp.status_code != 200:
                logger.warning(
                    "search_issues failed for %s: status=%s",
                    repo,
                    getattr(resp, "status_code", None),
                )
                break
            try:
                payload = resp.json()
            except ValueError as exc:
                logger.warning("search_issues invalid JSON: %s", exc)
                break
            items = payload.get("items") or []
            if not items:
                break
            for item in items:
                if isinstance(item, dict):
                    results.append(self._normalize_issue_item(item))
                if len(results) >= max_results:
                    break
            if len(items) < per_page:
                break
            page += 1
            if len(results) < max_results:
                await asyncio.sleep(2.0)
        return results[:max_results]

    async def get_issue_comments(
        self, repo: str, issue_number: int, max_comments: int = 5
    ) -> list[dict[str, Any]]:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return []
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/issues/{issue_number}/comments",
            params={"per_page": max_comments, "page": 1},
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_issue_comments failed for %s#%s: status=%s",
                repo,
                issue_number,
                getattr(resp, "status_code", None),
            )
            return []
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_issue_comments invalid JSON: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for row in data[:max_comments]:
            if not isinstance(row, dict):
                continue
            user = row.get("user") if isinstance(row.get("user"), dict) else {}
            body = row.get("body") or ""
            if isinstance(body, str) and len(body) > 1500:
                body = body[:1500]
            out.append(
                {
                    "user_login": (user or {}).get("login"),
                    "body": body,
                    "created_at": row.get("created_at"),
                    "html_url": row.get("html_url"),
                }
            )
        return out

    async def get_file_content(self, repo: str, path: str) -> str:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return ""
        encoded = quote(path.lstrip("/"), safe="/")
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/contents/{encoded}",
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_file_content failed for %s/%s: status=%s",
                repo,
                path,
                getattr(resp, "status_code", None),
            )
            return ""
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_file_content invalid JSON: %s", exc)
            return ""
        if isinstance(data, list):
            logger.warning("get_file_content path is a directory for %s/%s", repo, path)
            return ""
        if not isinstance(data, dict):
            return ""
        text = self._b64_decode_content(data.get("content"))
        return text[:50000]

    async def get_releases(
        self, repo: str, max_releases: int = 5
    ) -> list[dict[str, Any]]:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return []
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/releases",
            params={"per_page": max_releases, "page": 1},
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_releases failed for %s: status=%s",
                repo,
                getattr(resp, "status_code", None),
            )
            return []
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_releases invalid JSON: %s", exc)
            return []
        if not isinstance(data, list):
            return []
        out: list[dict[str, Any]] = []
        for row in data[:max_releases]:
            if not isinstance(row, dict):
                continue
            body = row.get("body") or ""
            if isinstance(body, str) and len(body) > 3000:
                body = body[:3000]
            out.append(
                {
                    "tag_name": row.get("tag_name"),
                    "name": row.get("name"),
                    "body": body,
                    "published_at": row.get("published_at"),
                    "html_url": row.get("html_url"),
                }
            )
        return out

    async def get_workflows(self, repo: str) -> list[dict[str, Any]]:
        try:
            owner, name = self._parse_repo(repo)
        except ValueError as exc:
            logger.warning("%s", exc)
            return []
        resp = await self._request(
            "GET",
            f"/repos/{owner}/{name}/actions/workflows",
        )
        if resp is None or resp.status_code != 200:
            logger.warning(
                "get_workflows failed for %s: status=%s",
                repo,
                getattr(resp, "status_code", None),
            )
            return []
        try:
            data = resp.json()
        except ValueError as exc:
            logger.warning("get_workflows invalid JSON: %s", exc)
            return []
        workflows = data.get("workflows") or []
        if not isinstance(workflows, list):
            return []
        out: list[dict[str, Any]] = []
        for row in workflows:
            if not isinstance(row, dict):
                continue
            out.append(
                {
                    "name": row.get("name"),
                    "path": row.get("path"),
                    "state": row.get("state"),
                }
            )
        return out
