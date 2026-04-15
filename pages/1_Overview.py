import streamlit as st
from lib.data_loader import load_all_dashboard_data, get_overall_amd_support
from lib.types import (
    ProjectType,
    SUPPORT_LEVEL_COLORS,
    SUPPORT_LEVEL_LABELS,
    CATEGORY_LABELS,
)
from components.heatmap import render_heatmap, render_support_legend

st.set_page_config(page_title="Overview", layout="wide")
st.title("Overview — AMD Support Heat Map")

data = load_all_dashboard_data()

if not data:
    st.warning("No data found. Run the pipeline first (Pipeline page).")
    st.stop()

total = len(data)
with_analysis = sum(1 for d in data if d.analysis)
with_rocm = sum(1 for d in data if d.analysis and get_overall_amd_support(d.analysis) != None)

support_counts = {}
for d in data:
    level = get_overall_amd_support(d.analysis)
    label = SUPPORT_LEVEL_LABELS.get(level, "None")
    support_counts[label] = support_counts.get(label, 0) + 1

col1, col2, col3, col4 = st.columns(4)
col1.metric("Total Projects", total)
col2.metric("Analyzed", with_analysis)
col3.metric("With ROCm Support", sum(v for k, v in support_counts.items() if k in ("First-class", "Experimental")))
col4.metric("No Support", support_counts.get("None", 0) + (total - with_analysis))

st.markdown("### Support Level Legend")
legend_fig = render_support_legend()
st.plotly_chart(legend_fig, use_container_width=True, config={"displayModeBar": False})

st.markdown("### AMD Support by Project")
for cat_key, cat_label in CATEGORY_LABELS.items():
    cat_projects = [d for d in data if d.entry.category.value == cat_key]
    if not cat_projects:
        continue

    st.markdown(f"#### {cat_label}")
    cols = st.columns(min(len(cat_projects), 3))
    for i, d in enumerate(cat_projects):
        with cols[i % 3]:
            level = get_overall_amd_support(d.analysis)
            color = SUPPORT_LEVEL_COLORS[level]
            label = SUPPORT_LEVEL_LABELS[level]

            st.markdown(f"**{d.entry.id}**")
            st.markdown(f"<span style='color:{color}; font-weight:bold'>{label}</span>", unsafe_allow_html=True)
            st.caption(d.entry.description[:80] + "..." if len(d.entry.description) > 80 else d.entry.description)

            if d.metrics:
                st.caption(f"Stars: {d.metrics.get('stars', 'N/A')} | ROCm issues: {d.metrics.get('rocm_issues_open', 'N/A')}")

            if d.entry.type == ProjectType.NV_AMD_PAIR and d.entry.amd_name:
                st.caption(f"NV: {d.entry.nv_name or d.entry.nv_repo} ↔ AMD: {d.entry.amd_name}")

            if d.analysis:
                gap_count = len(d.analysis.get("features", [])) if d.entry.type == ProjectType.DUAL_PLATFORM else len(d.analysis.get("comparison", {}).get("shared_features", []))
                st.caption(f"Features analyzed: {gap_count}")

st.markdown("---")
st.caption("Data is auto-collected from GitHub and analyzed by LLM. Run the Pipeline to refresh.")
