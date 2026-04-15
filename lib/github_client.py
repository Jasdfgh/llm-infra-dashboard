from __future__ import annotations

import base64
import json
import os
import time
from datetime import datetime
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
GITHUB_API = "https://api.github.com"

HEADERS = {
    "Accept": "application/vnd.github.v3+json",
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"


def _request(path: str, params: Optional[dict] = None, retries: int = 3, follow_redirects: bool = True) -> dict:
    url = f"{GITHUB_API}{path}"
    for attempt in range(retries):
        try:
            with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=follow_redirects) as client:
                r = client.get(url, params=params)
                if r.status_code == 403:
                    if "rate limit" in r.text.lower():
                        wait = 60 * (attempt + 1)
                        print(f"    Rate limited, waiting {wait}s... (attempt {attempt+1}/{retries})")
                        time.sleep(wait)
                        continue
                    else:
                        print(f"    403 Forbidden for {url}: access may be restricted")
                        return {}
                r.raise_for_status()
                return r.json()
        except (httpx.HTTPStatusError, httpx.TimeoutException) as e:
            if attempt < retries - 1:
                time.sleep(5)
                continue
            print(f"    Failed after {retries} attempts for {url}: {e}")
            return {}
    return {}


def get_repo_info(repo: str) -> dict:
    return _request(f"/repos/{repo}") or {}


def get_readme(repo: str) -> str:
    try:
        data = _request(f"/repos/{repo}/readme")
        if not data:
            return ""
        content = data.get("content", "")
        if content:
            return base64.b64decode(content).decode("utf-8", errors="replace")[:8000]
    except Exception:
        pass
    return ""


def get_repo_structure(repo: str) -> list[str]:
    for branch in ["main", "master"]:
        try:
            data = _request(f"/repos/{repo}/git/trees/{branch}?recursive=1")
            if not data:
                continue
            return [item["path"] for item in data.get("tree", [])[:300]]
        except Exception:
            continue
    return []


def search_count(repo: str, query: str) -> int:
    try:
        result = _request("/search/issues", {"q": f"repo:{repo} {query}", "per_page": 1})
        if not result:
            return 0
        return result.get("total_count", 0)
    except Exception:
        return 0


ROCM_KEYWORDS = [
    "rocm", "hip", "amd", "mi200", "mi250", "mi300", "gfx90a", "gfx942",
    "rccl", "miopen", "rocblas", "hipblas", "hipsparse",
]


def fetch_and_save_metrics(repo: str, output_path: str) -> dict:
    print(f"    Fetching repo info for {repo}...")
    repo_info = get_repo_info(repo)

    if not repo_info:
        print(f"    WARNING: Could not fetch repo info for {repo} (access restricted?)")
        repo_info = {"stargazers_count": 0, "forks_count": 0, "open_issues_count": 0, "pushed_at": "", "description": ""}

    print(f"    Fetching README for {repo}...")
    readme = get_readme(repo)

    print(f"    Fetching repo structure for {repo}...")
    structure = get_repo_structure(repo)

    readme_lower = readme.lower()
    rocm_keywords_found = []
    for kw in ROCM_KEYWORDS:
        count = readme_lower.count(kw)
        if count > 0:
            rocm_keywords_found.append(f"{kw}:{count}")

    structure_lower = [f.lower() for f in structure]
    has_rocm_dockerfile = any("rocm" in f and "docker" in f for f in structure_lower)
    has_rocm_ci = any((".github" in f and ("rocm" in f or "hip" in f or "amd" in f)) for f in structure_lower)

    print(f"    Counting ROCm-related issues/PRs for {repo}...")
    rocm_issues_open = search_count(repo, "rocm is:issue is:open")
    time.sleep(3)
    rocm_prs_merged = search_count(repo, "rocm is:pr is:merged")
    time.sleep(3)

    metrics = {
        "stars": repo_info.get("stargazers_count", 0),
        "forks": repo_info.get("forks_count", 0),
        "open_issues": repo_info.get("open_issues_count", 0),
        "last_commit_date": repo_info.get("pushed_at", ""),
        "description": repo_info.get("description", ""),
        "rocm_mentions_in_readme": len(rocm_keywords_found),
        "rocm_keywords_in_readme": rocm_keywords_found,
        "rocm_issues_open": rocm_issues_open,
        "rocm_prs_merged": rocm_prs_merged,
        "has_rocm_ci": has_rocm_ci,
        "has_rocm_dockerfile": has_rocm_dockerfile,
        "readme_excerpt": readme[:5000],
        "repo_structure": structure[:200],
        "fetched_at": datetime.utcnow().isoformat(),
        "repo_accessible": bool(repo_info.get("stargazers_count") is not None),
    }

    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(metrics, f, indent=2, ensure_ascii=False)
    return metrics
