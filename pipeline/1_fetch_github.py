#!/usr/bin/env python3
"""Fetch GitHub metrics for all projects in the registry."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.github_client import fetch_and_save_metrics


def main():
    parser = argparse.ArgumentParser(description="Fetch GitHub metrics")
    parser.add_argument("--projects", nargs="*", help="Only fetch these project IDs (e.g. vllm flashinfer_vs_aiter)")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    data_dir = base_dir / "data"

    with open(data_dir / "repo_registry.json") as f:
        registry = json.load(f)

    metrics_dir = data_dir / "github_metrics"
    metrics_dir.mkdir(exist_ok=True)

    projects = registry["projects"]
    if args.projects:
        projects = [p for p in projects if p["id"] in args.projects]
        print(f"Filtering to {len(projects)} project(s): {args.projects}")

    for project in projects:
        project_id = project["id"]
        print(f"\n{'='*60}")
        print(f"Fetching: {project_id}")

        repos_to_fetch = []

        if project["type"] == "dual_platform":
            repos_to_fetch.append(("nv", project["nv_repo"]))
        elif project["type"] == "nv_amd_pair":
            repos_to_fetch.append(("nv", project["nv_repo"]))
            if project.get("amd_repo"):
                repos_to_fetch.append(("amd", project["amd_repo"]))
        else:
            repos_to_fetch.append(("nv", project["nv_repo"]))

        for side, repo in repos_to_fetch:
            print(f"  Fetching {side} repo: {repo}")
            output_name = f"{project_id}_{side}" if project["type"] == "nv_amd_pair" else project_id
            output_path = metrics_dir / f"{output_name}.json"

            try:
                metrics = fetch_and_save_metrics(repo, str(output_path))
                print(f"    Stars: {metrics['stars']}, ROCm mentions: {metrics['rocm_mentions_in_readme']}")
                print(f"    ROCm issues open: {metrics['rocm_issues_open']}, PRs merged: {metrics['rocm_prs_merged']}")
                print(f"    Has ROCm CI: {metrics['has_rocm_ci']}, Dockerfile: {metrics['has_rocm_dockerfile']}")
            except Exception as e:
                print(f"    ERROR: {e}")

    print("\n\nDone! Metrics saved to data/github_metrics/")


if __name__ == "__main__":
    main()
