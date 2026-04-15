#!/usr/bin/env python3
"""Discover AMD counterpart repos using LLM analysis + GitHub search."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.llm_client import discover_amd_counterpart


def main():
    base_dir = Path(__file__).parent.parent
    data_dir = base_dir / "data"

    with open(data_dir / "repo_registry.json") as f:
        registry = json.load(f)

    known_mappings = []
    for p in registry["projects"]:
        if p["type"] == "nv_amd_pair" and p.get("amd_repo"):
            known_mappings.append(f"- {p.get('nv_name', p['nv_repo'])} -> {p.get('amd_name', p['amd_repo'])} ({p['amd_repo']})")

    known_mappings_str = "\n".join(known_mappings) if known_mappings else "None known yet."

    unpaired = [p for p in registry["projects"] if p["type"] == "dual_platform" and not p.get("amd_repo")]
    if not unpaired:
        print("All dual_platform projects already have AMD counterparts or don't need one.")
        return

    for project in unpaired:
        project_id = project["id"]
        print(f"\nDiscovering AMD counterpart for: {project_id}")

        metrics_path = data_dir / "github_metrics" / f"{project_id}.json"
        metrics = {}
        if metrics_path.exists():
            with open(metrics_path) as f:
                metrics = json.load(f)
        else:
            print(f"  No metrics found for {project_id}, run 1_fetch_github.py first")
            continue

        try:
            result = discover_amd_counterpart(
                project_id=project_id,
                project_name=project_id,
                repo=project["nv_repo"],
                description=project.get("description", ""),
                metrics=metrics,
                known_mappings=known_mappings_str,
            )
            print(f"  Result: {json.dumps(result, indent=2)}")
        except Exception as e:
            print(f"  ERROR: {e}")


if __name__ == "__main__":
    main()
