import pandas as pd
import streamlit as st
from lib.data_loader import load_all_dashboard_data, load_metrics
from lib.types import (
    ProjectType,
    SUPPORT_LEVEL_COLORS,
    SUPPORT_LEVEL_LABELS,
    CATEGORY_LABELS,
)
from components.feature_table import render_dual_platform_table, render_pair_table

st.set_page_config(page_title="Project Details", layout="wide")
st.title("Project Details")

data = load_all_dashboard_data()
if not data:
    st.warning("No data found. Run the pipeline first.")
    st.stop()

project_choices = {f"{d.entry.id} ({CATEGORY_LABELS[d.entry.category]})": d for d in data}
selected = st.selectbox("Select a project", list(project_choices.keys()))

d = project_choices[selected]
entry = d.entry

st.markdown(f"## {entry.id}")
st.markdown(entry.description)

col1, col2 = st.columns(2)
with col1:
    st.markdown(f"**Type:** {entry.type.value}")
    st.markdown(f"**Category:** {CATEGORY_LABELS[entry.category]}")
    st.markdown(f"**NVIDIA repo:** `{entry.nv_repo}`")
    if entry.type == ProjectType.NV_AMD_PAIR:
        st.markdown(f"**AMD repo:** `{entry.amd_repo}`")
        st.markdown(f"**NV name:** {entry.nv_name}")
        st.markdown(f"**AMD name:** {entry.amd_name}")

with col2:
    if d.metrics:
        m = d.metrics
        st.markdown(f"**Stars:** {m.get('stars', 'N/A')}")
        st.markdown(f"**Forks:** {m.get('forks', 'N/A')}")
        st.markdown(f"**ROCm mentions in README:** {m.get('rocm_mentions_in_readme', 0)}")
        st.markdown(f"**ROCm CI:** {'Yes' if m.get('has_rocm_ci') else 'No'}")
        st.markdown(f"**ROCm Dockerfile:** {'Yes' if m.get('has_rocm_dockerfile') else 'No'}")
        st.markdown(f"**ROCm issues (open):** {m.get('rocm_issues_open', 0)}")
        st.markdown(f"**ROCm PRs (merged):** {m.get('rocm_prs_merged', 0)}")

if d.analysis:
    st.markdown("---")
    st.markdown(f"### Analysis Summary")
    st.info(d.analysis.get("summary", "No summary available."))

    if entry.type == ProjectType.DUAL_PLATFORM:
        st.markdown("### Feature Support Comparison (NVIDIA vs AMD)")
        df = render_dual_platform_table(d.analysis)
        if not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)

        blockers = d.analysis.get("key_blockers", [])
        if blockers:
            st.markdown("### Key Blockers")
            for b in blockers:
                impact_color = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(b.get("impact", ""), "")
                st.markdown(f"{impact_color} **{b['description']}** — Impact: {b.get('impact', 'N/A')}")
                if b.get("workaround"):
                    st.caption(f"Workaround: {b['workaround']}")

    elif entry.type == ProjectType.NV_AMD_PAIR:
        st.markdown(f"### Feature Comparison: {d.entry.nv_name or entry.nv_repo} vs {d.entry.amd_name or entry.amd_repo}")
        df = render_pair_table(d.analysis)
        if not df.empty:
            st.dataframe(df, use_container_width=True, hide_index=True)

        nv_only = d.analysis.get("comparison", {}).get("nv_only_features", [])
        if nv_only:
            st.markdown("### NVIDIA-Only Features (AMD Gaps)")
            for feat in nv_only:
                st.markdown(f"- **{feat['name']}**: {feat.get('description', '')}")

        amd_only = d.analysis.get("comparison", {}).get("amd_only_features", [])
        if amd_only:
            st.markdown("### AMD-Only Features (AMD Advantages)")
            for feat in amd_only:
                st.markdown(f"- **{feat['name']}**: {feat.get('description', '')}")
else:
    st.warning("No analysis data available. Run the pipeline to generate analysis.")
