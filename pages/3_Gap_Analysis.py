import streamlit as st
import pandas as pd
from lib.data_loader import load_all_dashboard_data, get_feature_gaps, get_pair_gaps
from lib.types import ProjectType, SUPPORT_LEVEL_LABELS
from components.gap_ranking import render_gap_ranking

st.set_page_config(page_title="Gap Analysis", layout="wide")
st.title("Gap Analysis — Prioritized AMD Gaps")

data = load_all_dashboard_data()
if not data:
    st.warning("No data found. Run the pipeline first.")
    st.stop()

category = st.selectbox("Filter by category", ["all", "inference", "training", "tool"])

df, fig = render_gap_ranking(data, category)

if df.empty:
    st.info("No gap data available. Run the pipeline to generate analysis.")
    st.stop()

st.markdown("### Top Gaps by Priority and Severity")
st.plotly_chart(fig, use_container_width=True)

st.markdown("### All Gaps")
st.dataframe(df, use_container_width=True, hide_index=True)

st.markdown("---")
st.markdown("### Gap Severity Legend")
st.markdown("- **Critical gap**: NVIDIA fully supported, AMD has nothing")
st.markdown("- **Major gap**: NVIDIA first-class, AMD experimental/community")
st.markdown("- **Minor gap**: Small difference in support level")
st.markdown("- **Parity**: Same support level on both platforms")

priority_counts = df["priority"].value_counts().sort_index()
st.markdown("### Gaps by Priority")
for p, count in priority_counts.items():
    label = {1: "P0 Critical", 2: "P1 High", 3: "P2 Medium", 4: "P3 Low", 5: "P4 Nice-to-have"}.get(p, f"P{p}")
    st.markdown(f"- **{label}**: {count} gaps")
