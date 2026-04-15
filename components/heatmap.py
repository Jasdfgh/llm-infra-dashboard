from __future__ import annotations

import plotly.graph_objects as go
from lib.types import SUPPORT_LEVEL_COLORS, SUPPORT_LEVEL_LABELS, SupportLevel


def render_heatmap(data_list: list) -> go.Figure:
    categories = ["inference", "training", "tool"]
    cat_labels = {"inference": "Inference", "training": "Training", "tool": "Tool"}
    
    projects_by_cat = {}
    for d in data_list:
        cat = d.entry.category.value
        if cat not in projects_by_cat:
            projects_by_cat[cat] = []
        projects_by_cat[cat].append(d)

    y_labels = []
    z_values = []
    z_colors = []
    hover_texts = []

    for cat in categories:
        projects = projects_by_cat.get(cat, [])
        for i, d in enumerate(projects):
            entry = d.entry
            y_labels.append(entry.id)
            z_values.append(1)

            if d.analysis:
                if entry.type.value == "dual_platform":
                    level = d.analysis.get("rocm_support_level", "none")
                else:
                    level = d.analysis.get("rocm_support_level", "none")
            else:
                level = "none"

            try:
                support = SupportLevel(level)
            except ValueError:
                support = SupportLevel.NONE

            z_colors.append(SUPPORT_LEVEL_COLORS[support])
            hover_texts.append(
                f"<b>{entry.id}</b><br>"
                f"Category: {cat_labels[cat]}<br>"
                f"AMD Support: {SUPPORT_LEVEL_LABELS[support]}<br>"
                f"Type: {entry.type.value}"
            )

            if i < len(projects) - 1:
                y_labels.append("")
                z_values.append(None)
                z_colors.append("#ffffff")
                hover_texts.append("")

    from lib.data_loader import get_overall_amd_support, get_feature_gaps, get_pair_gaps
    from lib.types import ProjectType

    rows = []
    hovers = []
    colors = []
    
    for cat in categories:
        projects = projects_by_cat.get(cat, [])
        for d in projects:
            entry = d.entry
            if d.analysis:
                if entry.type == ProjectType.DUAL_PLATFORM:
                    level = d.analysis.get("rocm_support_level", "none")
                    gap_count = len(get_feature_gaps(d.analysis))
                else:
                    comparison = d.analysis.get("comparison", {})
                    level = d.analysis.get("rocm_support_level", "none")
                    gap_count = len(get_pair_gaps(d.analysis))
            else:
                level = "none"
                gap_count = 0

            try:
                support = SupportLevel(level)
            except ValueError:
                support = SupportLevel.NONE

            rows.append(f"{entry.id}<br><sub>{cat_labels[cat]}</sub>")
            colors.append(SUPPORT_LEVEL_COLORS[support])
            hovers.append(
                f"<b>{entry.id}</b><br>"
                f"Category: {cat_labels[cat]}<br>"
                f"AMD Support: {SUPPORT_LEVEL_LABELS[support]}<br>"
                f"Feature Gaps: {gap_count}"
            )

    fig = go.Figure()

    for i, (row, color, hover) in enumerate(zip(rows, colors, hovers)):
        fig.add_trace(go.Bar(
            x=[1],
            y=[row],
            orientation="h",
            marker_color=color,
            hovertext=hover,
            hoverinfo="text",
            showlegend=False,
            width=0.8,
        ))

    fig.update_layout(
        height=max(400, len(rows) * 50 + 100),
        margin=dict(l=120, r=20, t=30, b=20),
        xaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        yaxis=dict(showticklabels=True, showgrid=False),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )

    return fig


def render_support_legend() -> go.Figure:
    levels = [
        (SupportLevel.FIRST_CLASS, "First-class"),
        (SupportLevel.EXPERIMENTAL, "Experimental"),
        (SupportLevel.COMMUNITY, "Community"),
        (SupportLevel.NONE, "None / No data"),
    ]

    fig = go.Figure()
    for i, (level, label) in enumerate(levels):
        fig.add_trace(go.Bar(
            x=[label],
            y=[1],
            marker_color=SUPPORT_LEVEL_COLORS[level],
            hoverinfo="text",
            hovertext=f"{label}: {SUPPORT_LEVEL_LABELS[level]}",
            showlegend=False,
            name=label,
        ))

    fig.update_layout(
        height=120,
        margin=dict(l=0, r=0, t=0, b=30),
        xaxis=dict(showgrid=False, tickfont=dict(size=11)),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        plot_bgcolor="white",
        paper_bgcolor="white",
        bargap=0.3,
    )

    return fig
