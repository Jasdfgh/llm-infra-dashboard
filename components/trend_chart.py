from __future__ import annotations

import plotly.graph_objects as go


def render_trend_chart(data_list: list) -> go.Figure:
    fig = go.Figure()

    categories = ["inference", "training", "tool"]
    cat_colors = {"inference": "#2563eb", "training": "#7c3aed", "tool": "#059669"}

    for cat in categories:
        projects_in_cat = [d for d in data_list if d.entry.category.value == cat]
        x_vals = []
        y_stars = []
        y_rocm_issues = []
        y_rocm_prs = []
        names = []

        for d in projects_in_cat:
            metrics = d.metrics
            if not metrics:
                continue
            names.append(d.entry.id)
            x_vals.append(d.entry.id)
            y_stars.append(metrics.get("stars", 0))
            y_rocm_issues.append(metrics.get("rocm_issues_open", 0))
            y_rocm_prs.append(metrics.get("rocm_prs_merged", 0))

        if not x_vals:
            continue

        fig.add_trace(go.Bar(
            name=f"{cat.title()} - Stars",
            x=x_vals,
            y=y_stars,
            marker_color=cat_colors[cat],
            offsetgroup=cat,
            showlegend=True,
        ))

    fig.update_layout(
        barmode="group",
        height=400,
        margin=dict(l=20, r=20, t=30, b=60),
        title="GitHub Stars by Project",
        xaxis_title="",
        yaxis_title="Stars",
        plot_bgcolor="white",
        paper_bgcolor="white",
    )

    return fig


def render_rocm_activity_chart(data_list: list) -> go.Figure:
    fig = go.Figure()

    for d in data_list:
        metrics = d.metrics
        if not metrics:
            continue

        fig.add_trace(go.Bar(
            name=f"{d.entry.id} (Open Issues)",
            x=[d.entry.id],
            y=[metrics.get("rocm_issues_open", 0)],
            marker_color="#f97316",
            showlegend=True,
        ))
        fig.add_trace(go.Bar(
            name=f"{d.entry.id} (Merged PRs)",
            x=[d.entry.id],
            y=[metrics.get("rocm_prs_merged", 0)],
            marker_color="#22c55e",
            showlegend=True,
        ))

    fig.update_layout(
        barmode="group",
        height=400,
        margin=dict(l=20, r=20, t=30, b=60),
        title="ROCm-Related GitHub Activity",
        xaxis_title="",
        yaxis_title="Count",
        plot_bgcolor="white",
        paper_bgcolor="white",
    )

    return fig
