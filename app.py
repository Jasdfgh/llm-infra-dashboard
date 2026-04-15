import streamlit as st

st.set_page_config(
    page_title="AMD vs NVIDIA LLM Infra Gap Dashboard",
    page_icon=":bar_chart:",
    layout="wide",
)

st.title("AMD vs NVIDIA LLM Infrastructure Gap Dashboard")

st.markdown("""
Track the feature gap between NVIDIA and AMD (ROCm) across key LLM infrastructure projects.
Data is collected from GitHub and analyzed by LLM to identify gaps and priorities.

**Navigation:**
- **Overview** — Hot map of AMD support across all projects
- **Project Details** — Deep dive into individual project feature comparison
- **Gap Analysis** — Prioritized list of gaps for AMD to address
- **Trends** — GitHub activity related to ROCm support
- **Pipeline** — Run data collection and analysis
""")
