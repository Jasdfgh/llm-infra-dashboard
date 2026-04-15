import subprocess
import sys
import streamlit as st
from pathlib import Path

st.set_page_config(page_title="Pipeline", layout="wide")
st.title("Pipeline — Data Collection & Analysis")

st.markdown("""
Run the data pipeline to fetch GitHub metrics and analyze project features using LLM.

### Pipeline Steps:
1. **Fetch GitHub Data** — Collect metrics from GitHub APIs
2. **Discover AMD Counterparts** — Use LLM to find AMD equivalent repos (optional)
3. **LLM Analysis** — Extract features and compare NV vs AMD support
4. **Run All Steps** — Execute the complete pipeline
""")

st.markdown("### Configuration")
st.caption("Set `GITHUB_TOKEN`, `OPENAI_API_KEY`, `OPENAI_BASE_URL`, and `OPENAI_MODEL` in `.env` file.")

col1, col2 = st.columns(2)

with col1:
    st.markdown("#### Individual Steps")
    if st.button("1. Fetch GitHub Metrics", use_container_width=True):
        with st.spinner("Fetching GitHub metrics..."):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / "pipeline" / "1_fetch_github.py")],
                capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
            )
            st.code(result.stdout)
            if result.returncode != 0:
                st.error(f"Error:\n{result.stderr}")
            else:
                st.success("GitHub metrics fetched successfully!")

    if st.button("2. Discover AMD Counterparts", use_container_width=True):
        with st.spinner("Discovering AMD counterparts..."):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / "pipeline" / "2_discover_amd.py")],
                capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
            )
            st.code(result.stdout)
            if result.returncode != 0:
                st.error(f"Error:\n{result.stderr}")
            else:
                st.success("AMD discovery complete!")

    if st.button("3. LLM Analysis", use_container_width=True):
        with st.spinner("Running LLM analysis..."):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / "pipeline" / "3_llm_analyze.py")],
                capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
            )
            st.code(result.stdout)
            if result.returncode != 0:
                st.error(f"Error:\n{result.stderr}")
            else:
                st.success("LLM analysis complete!")

with col2:
    st.markdown("#### Full Pipeline")
    if st.button("Run All Steps", use_container_width=True, type="primary"):
        with st.spinner("Running full pipeline..."):
            result = subprocess.run(
                [sys.executable, str(Path(__file__).parent.parent / "pipeline" / "4_run_all.py")],
                capture_output=True, text=True, cwd=str(Path(__file__).parent.parent)
            )
            st.code(result.stdout)
            if result.returncode != 0:
                st.error(f"Error:\n{result.stderr}")
            else:
                st.success("Pipeline complete! Refresh other pages to see updated data.")

st.markdown("---")
st.markdown("### Data Status")
data_dir = Path(__file__).parent.parent / "data"
metrics_dir = data_dir / "github_metrics"
analysis_dir = data_dir / "analysis"

if metrics_dir.exists():
    metric_files = list(metrics_dir.glob("*.json"))
    st.markdown(f"**GitHub metrics:** {len(metric_files)} files")
    for f in metric_files:
        st.caption(f"  - {f.name}")
else:
    st.markdown("**GitHub metrics:** No data yet")

if analysis_dir.exists():
    analysis_files = list(analysis_dir.glob("*.json"))
    st.markdown(f"**Analysis results:** {len(analysis_files)} files")
    for f in analysis_files:
        st.caption(f"  - {f.name}")
else:
    st.markdown("**Analysis results:** No data yet")
