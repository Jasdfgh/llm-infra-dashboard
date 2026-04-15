#!/usr/bin/env python3
"""Analyze projects using LLM to extract features and support comparison."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.llm_client import analyze_dual_platform, analyze_nv_amd_pair


def main():
    parser = argparse.ArgumentParser(description="LLM analysis of project features")
    parser.add_argument("--projects", nargs="*", help="Only analyze these project IDs")
    args = parser.parse_args()

    base_dir = Path(__file__).parent.parent
    data_dir = base_dir / "data"
    metrics_dir = data_dir / "github_metrics"
    analysis_dir = data_dir / "analysis"
    analysis_dir.mkdir(exist_ok=True)

    with open(data_dir / "repo_registry.json") as f:
        registry = json.load(f)

    projects = registry["projects"]
    if args.projects:
        projects = [p for p in projects if p["id"] in args.projects]
        print(f"Filtering to {len(projects)} project(s): {args.projects}")

    for project in projects:
        project_id = project["id"]
        project_type = project["type"]
        output_path = analysis_dir / f"{project_id}.json"

        print(f"\n{'='*60}")
        print(f"Analyzing: {project_id} (type: {project_type})")

        if project_type == "dual_platform":
            metrics_path = metrics_dir / f"{project_id}.json"
            if not metrics_path.exists():
                print(f"  No metrics found for {project_id}, run 1_fetch_github.py first")
                continue

            with open(metrics_path) as f:
                metrics = json.load(f)

            try:
                result = analyze_dual_platform(
                    project_id=project_id,
                    project_name=project_id,
                    repo=project["nv_repo"],
                    metrics=metrics,
                )
                with open(output_path, "w") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                print(f"  Analysis saved to {output_path}")
                print(f"  Features found: {len(result.get('features', []))}")
            except Exception as e:
                print(f"  ERROR: {e}")
                import traceback
                traceback.print_exc()

        elif project_type == "nv_amd_pair":
            nv_metrics_path = metrics_dir / f"{project_id}_nv.json"
            amd_metrics_path = metrics_dir / f"{project_id}_amd.json"

            if not nv_metrics_path.exists() or not amd_metrics_path.exists():
                print(f"  No metrics found for {project_id}, run 1_fetch_github.py first")
                continue

            with open(nv_metrics_path) as f:
                nv_metrics = json.load(f)
            with open(amd_metrics_path) as f:
                amd_metrics = json.load(f)

            try:
                result = analyze_nv_amd_pair(
                    project_id=project_id,
                    nv_name=project.get("nv_name", project["nv_repo"]),
                    nv_repo=project["nv_repo"],
                    nv_metrics=nv_metrics,
                    amd_name=project.get("amd_name", project.get("amd_repo", "")),
                    amd_repo=project["amd_repo"],
                    amd_metrics=amd_metrics,
                )
                with open(output_path, "w") as f:
                    json.dump(result, f, indent=2, ensure_ascii=False)
                print(f"  Analysis saved to {output_path}")
                shared = len(result.get("comparison", {}).get("shared_features", []))
                nv_only = len(result.get("comparison", {}).get("nv_only_features", []))
                print(f"  Shared features: {shared}, NV-only: {nv_only}")
            except Exception as e:
                print(f"  ERROR: {e}")
                import traceback
                traceback.print_exc()

    print("\n\nDone! Analysis results saved to data/analysis/")


if __name__ == "__main__":
    main()
