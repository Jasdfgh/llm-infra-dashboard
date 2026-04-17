from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Any

from app.agent.github_client import GitHubClient

logger = logging.getLogger(__name__)

_DISPATCH_HINTS = ("dispatch", "registry")
_DISPATCH_SUFFIXES = (".py", ".cpp", ".cu", ".cuh", ".h", ".hpp")
_HIP_MARKERS = (
    ".hip",
    "/hip/",
    "hip/",
    "_hip_",
    "/hip_",
    "hipcc",
    "hipblas",
    "hipfft",
    "hiprand",
    "hipify",
)


def _iso_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _issue_engagement(issue: dict[str, Any]) -> int:
    comments = issue.get("comments")
    c = int(comments) if isinstance(comments, int) else 0
    reactions = issue.get("reactions")
    if isinstance(reactions, dict):
        total = reactions.get("total_count")
        if isinstance(total, int):
            return c + total
    return c


def _dedupe_issues_by_number(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    out: list[dict[str, Any]] = []
    for it in items:
        n = it.get("number")
        if n in seen:
            continue
        seen.add(n)
        out.append(it)
    return out


def _path_rocmish(path: str) -> bool:
    low = path.lower()
    if "rocm" in low:
        return True
    if low.endswith(".hip"):
        return True
    if any(m in low for m in _HIP_MARKERS):
        return True
    if "/amd/" in f"/{low}/" or low.startswith("amd/"):
        return True
    if "_amd_" in low or low.startswith("amd_") or "/amd_" in low:
        return True
    base = low.rsplit("/", 1)[-1]
    return "amd" in base and "gpu" in base


def _path_cudaish(path: str) -> bool:
    low = path.lower()
    if "cuda" in low:
        return True
    if low.endswith((".cu", ".cuh")) and not low.endswith(".hip"):
        return True
    return False


def _kernel_dirs_from_tree(tree: list[str]) -> list[str]:
    found: list[str] = []
    for name in ("csrc", "kernels", "ops", "backends"):
        prefix = f"{name}/"
        if any(p.startswith(prefix) or f"/{prefix}" in p for p in tree):
            found.append(name)
    return found


def _find_dispatch_paths(tree: list[str]) -> list[str]:
    out: list[str] = []
    for p in sorted(tree):
        low = p.lower()
        if not any(h in low for h in _DISPATCH_HINTS):
            continue
        if not low.endswith(_DISPATCH_SUFFIXES):
            continue
        if not any(
            low.startswith(f"{k}/") or f"/{k}/" in f"/{low}/"
            for k in ("ops", "backends", "csrc")
        ):
            continue
        out.append(p)
    return out[:5]


def _identify_key_files(tree: list[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def add(path: str) -> None:
        if path not in seen:
            seen.add(path)
            ordered.append(path)

    wf_paths = sorted(
        p
        for p in tree
        if p.startswith(".github/workflows/")
        and p.lower().endswith((".yml", ".yaml"))
    )
    for p in wf_paths:
        add(p)

    for p in sorted(tree):
        base = p.rsplit("/", 1)[-1].lower()
        if "dockerfile" in base or base == "dockerfile":
            add(p)

    for p in sorted(tree):
        if p.endswith("setup.py") or p.endswith("pyproject.toml"):
            add(p)

    for p in sorted(tree):
        if p.lower().endswith("cmakelists.txt"):
            add(p)

    for p in sorted(tree):
        low = p.lower()
        if not any(h in low for h in _DISPATCH_HINTS):
            continue
        if low.endswith((".py", ".cpp", ".cu", ".cuh")):
            add(p)

    return ordered


def _workflow_paths_from_tree(tree: list[str]) -> list[str]:
    return sorted(
        p
        for p in tree
        if p.startswith(".github/workflows/")
        and p.lower().endswith((".yml", ".yaml"))
    )


def _mentions_rocm_ci(text: str) -> bool:
    low = text.lower()
    return bool(re.search(r"\b(rocm|hip|amd)\b", low))


class CrawlAgent:
    def __init__(self, github_client: GitHubClient) -> None:
        self.gh = github_client

    async def crawl_project(
        self,
        project_id: str,
        repo: str,
        project_type: str = "dual_platform",
        amd_repo: str | None = None,
    ) -> dict[str, Any]:
        api_start = self.gh.api_call_count
        crawled_at = _iso_now()

        info, readme, tree, amd_info, amd_readme = await self._phase1_overview(
            repo, project_type, amd_repo
        )

        from datetime import datetime, timedelta, timezone
        cutoff_90d = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        cutoff_180d = (datetime.now(timezone.utc) - timedelta(days=180)).strftime("%Y-%m-%d")

        (
            rocm_core,
            rocm_recent,
            model_gaps,
            perf_issues,
            feature_issues,
            merged_prs,
            recent_closed,
        ) = await asyncio.gather(
            self.gh.search_issues(
                repo,
                f"rocm OR amd OR hip OR mi300 OR mi250 is:issue updated:>{cutoff_180d}",
                30, sort="updated",
            ),
            self.gh.search_issues(
                repo,
                f"rocm OR amd OR hip OR mi300 is:issue created:>{cutoff_90d}",
                20, sort="created",
            ),
            self.gh.search_issues(
                repo,
                f"(model OR architecture) (rocm OR amd OR hip) is:issue updated:>{cutoff_180d}",
                15, sort="updated",
            ),
            self.gh.search_issues(
                repo,
                f"(benchmark OR performance OR throughput OR latency) (mi300 OR rocm OR amd) is:issue updated:>{cutoff_180d}",
                15, sort="updated",
            ),
            self.gh.search_issues(
                repo,
                f"(\"cuda only\" OR \"not supported\" OR \"rocm\" OR \"hip\") is:issue updated:>{cutoff_180d}",
                15, sort="updated",
            ),
            self.gh.search_issues(
                repo,
                f"rocm OR hip OR amd is:pr is:merged merged:>{cutoff_180d}",
                30, sort="updated",
            ),
            self.gh.search_issues(
                repo,
                f"rocm OR amd OR hip is:issue is:closed closed:>{cutoff_90d}",
                15, sort="updated",
            ),
        )

        rocm_related = _dedupe_issues_by_number(
            list(rocm_core) + list(rocm_recent) + list(feature_issues) + list(recent_closed)
        )
        performance = _dedupe_issues_by_number(list(perf_issues))

        ranked = sorted(rocm_core, key=_issue_engagement, reverse=True)
        top5 = ranked[:5]
        comment_pairs: list[tuple[int, dict[str, Any]]] = []
        for it in top5:
            raw = it.get("number")
            try:
                num = int(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            comment_pairs.append((num, it))
        comment_lists = await asyncio.gather(
            *[self.gh.get_issue_comments(repo, num, 3) for num, _ in comment_pairs]
        ) if comment_pairs else []
        with_comments: dict[str, list[dict[str, Any]]] = {
            str(num): comments for (num, _), comments in zip(comment_pairs, comment_lists)
        }

        code_block = await self._phase3_code_reads(repo, tree)

        for dp in _find_dispatch_paths(tree):
            if dp not in code_block["file_contents"]:
                text = await self.gh.get_file_content(repo, dp)
                if text:
                    code_block["file_contents"][dp] = text

        ci_block = await self._phase6_ci(repo, tree)
        releases = await self.gh.get_releases(repo, 5)

        rocm_open = sum(1 for i in rocm_related if i.get("state") == "open")
        rocm_closed = sum(1 for i in rocm_related if i.get("state") == "closed")

        api_calls = self.gh.api_call_count - api_start

        return {
            "project_id": project_id,
            "repo": repo,
            "project_type": project_type,
            "crawled_at": crawled_at,
            "api_calls": api_calls,
            "overview": {
                "info": info,
                "readme": readme,
                "tree": tree,
                "amd_info": amd_info,
                "amd_readme": amd_readme,
            },
            "issues": {
                "rocm_related": rocm_related,
                "model_gaps": model_gaps,
                "performance": performance,
                "with_comments": with_comments,
            },
            "pull_requests": {"rocm_merged": merged_prs},
            "code": code_block,
            "ci": ci_block,
            "releases": releases,
            "stats": {
                "rocm_issues_open": rocm_open,
                "rocm_issues_closed": rocm_closed,
                "rocm_prs_merged": len(merged_prs),
                "cuda_file_count": len(code_block["cuda_files"]),
                "rocm_file_count": len(code_block["rocm_files"]),
            },
        }

    async def _phase1_overview(
        self,
        repo: str,
        project_type: str,
        amd_repo: str | None,
    ) -> tuple[dict[str, Any], str, list[str], dict[str, Any] | None, str | None]:
        if project_type == "nv_amd_pair" and amd_repo:
            info, readme, tree, amd_info, amd_readme = await asyncio.gather(
                self.gh.get_repo_info(repo),
                self.gh.get_readme(repo),
                self.gh.get_repo_tree(repo),
                self.gh.get_repo_info(amd_repo),
                self.gh.get_readme(amd_repo),
            )
            return info, readme, tree, amd_info, amd_readme
        info, readme, tree = await asyncio.gather(
            self.gh.get_repo_info(repo),
            self.gh.get_readme(repo),
            self.gh.get_repo_tree(repo),
        )
        return info, readme, tree, None, None

    async def _phase3_code_reads(
        self, repo: str, tree: list[str]
    ) -> dict[str, Any]:
        rocm_files = sorted(
            {
                p
                for p in tree
                if _path_rocmish(p)
                or any(
                    x in p.lower()
                    for x in (
                        "rocm",
                        "hip",
                        "/amd/",
                        "_amd_",
                        "amd_",
                        "/backend",
                        "backend/",
                    )
                )
            }
        )
        cuda_files = sorted(
            {p for p in tree if _path_cudaish(p) and p not in rocm_files}
        )
        kernel_dirs = _kernel_dirs_from_tree(tree)

        candidates = _identify_key_files(tree)[:5]
        read_tasks = [self.gh.get_file_content(repo, path) for path in candidates]
        blobs = await asyncio.gather(*read_tasks) if read_tasks else []
        contents: dict[str, str] = {}
        for path, text in zip(candidates, blobs):
            if text:
                contents[path] = text

        return {
            "rocm_files": rocm_files,
            "cuda_files": cuda_files,
            "kernel_dirs": kernel_dirs,
            "file_contents": contents,
        }

    async def _phase6_ci(self, repo: str, tree: list[str]) -> dict[str, Any]:
        workflows = await self.gh.get_workflows(repo)
        paths = _workflow_paths_from_tree(tree)[:25]
        read_tasks = [self.gh.get_file_content(repo, p) for p in paths]
        blobs = await asyncio.gather(*read_tasks) if read_tasks else []
        rocm_ci_files: dict[str, str] = {}
        for path, body in zip(paths, blobs):
            if body and _mentions_rocm_ci(body):
                rocm_ci_files[path] = body
        return {"workflows": workflows, "rocm_ci_files": rocm_ci_files}
