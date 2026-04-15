import streamlit as st
from lib.data_loader import load_all_dashboard_data
from components.trend_chart import render_trend_chart, render_rocm_activity_chart

st.set_page_config(page_title="Trends", layout="wide")
st.title("Trends — ROCm GitHub Activity")

data = load_all_dashboard_data()
if not data:
    st.warning("No data found. Run the pipeline first.")
    st.stop()

st.markdown("### GitHub Stars by Project")
fig = render_trend_chart(data)
st.plotly_chart(fig, use_container_width=True)

st.markdown("### ROCm-Related Activity")
fig2 = render_rocm_activity_chart(data)
st.plotly_chart(fig2, use_container_width=True)

st.markdown("### Project Metrics Detail")
for d in data:
    if not d.metrics:
        continue
    with st.expander(f"{d.entry.id} — {d.entry.description[:60]}"):
        m = d.metrics
        col1, col2, col3 = st.columns(3)
        with col1:
            st.metric("Stars", m.get("stars", "N/A"))
            st.metric("Forks", m.get("forks", "N/A"))
            st.metric("Open Issues", m.get("open_issues", "N/A"))
        with col2:
            st.metric("ROCm Issues (Open)", m.get("rocm_issues_open", 0))
            st.metric("ROCm Issues (Closed)", m.get("rocm_issues_closed", 0))
            st.metric("ROCm PRs (Merged)", m.get("rocm_prs_merged", 0))
        with col3:
            st.metric("Has ROCm CI", "Yes" if m.get("has_rocm_ci") else "No")
            st.metric("Has ROCm Dockerfile", "Yes" if m.get("has_rocm_dockerfile") else "No")
            st.metric("ROCm Keywords", ", ".join(m.get("rocm_keywords_in_readme", [])[:5]))
