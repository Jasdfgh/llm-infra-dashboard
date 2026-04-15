#!/usr/bin/env python3
"""Run the full pipeline: fetch -> discover -> analyze."""

import subprocess
import sys
from pathlib import Path


def run_step(script_name: str, description: str):
    print(f"\n{'#'*60}")
    print(f"# Step: {description}")
    print(f"{'#'*60}")
    script_path = Path(__file__).parent / script_name
    result = subprocess.run([sys.executable, str(script_path)], cwd=Path(__file__).parent.parent)
    if result.returncode != 0:
        print(f"ERROR: {script_name} failed with exit code {result.returncode}")
        sys.exit(1)


def main():
    print("="*60)
    print("LLM Infra Dashboard - Full Pipeline")
    print("="*60)

    run_step("1_fetch_github.py", "Fetching GitHub metrics")
    run_step("2_discover_amd.py", "Discovering AMD counterparts")
    run_step("3_llm_analyze.py", "Running LLM analysis")

    print("\n\n" + "="*60)
    print("Pipeline complete! Run Streamlit dashboard with:")
    print("  streamlit run app.py")
    print("="*60)


if __name__ == "__main__":
    main()
