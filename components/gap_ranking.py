from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from lib.types import (
    ProjectType,
    SupportLevel,
    SUPPORT_LEVEL_COLORS,
    SUPPORT_LEVEL_LABELS,
)
from lib.data_loader import get_feature_gaps, get_pair_gaps


def render_gap_ranking(data_list: list, category_filter: str = "all") -> tuple[pd.DataFrame, go.Figure]:
    all_gaps = []

    for d in data_list:
        entry = d.entry
        if category_filter != "all" and entry.category.value != category_filter:
            continue

        if not d.analysis:
            continue

        if entry.type == ProjectType.DUAL_PLATFORM:
            gaps = get_feature_gaps(d.analysis)
        else:
            gaps = get_pair_gaps(d.analysis)

        for gap in gaps:
            all_gaps.append({
                "project": entry.id,
                "category": entry.category.value,
                "feature": gap["name"],
                "nvidia_support": SUPPORT_LEVEL_LABELS.get(gap["nvidia_support"], "Unknown"),
                "amd_support": SUPPORT_LEVEL_LABELS.get(gap["amd_support"], "Unknown"),
                "priority": gap["priority"],
                "gap_score": gap["gap_score"],
                "blockers": "; ".join(gap["blockers"]),
                "description": gap["description"],
            })

    if not all_gaps:
        return pd.DataFrame(), go.Figure()

    df = pd.DataFrame(all_gaps)
    df = df.sort_values(["priority", "gap_score"], ascending=[True, False])

    fig = go.Figure()
    for _, row in df.head(20).iterrows():
        color_map = {
            "Critical gap": "#ef4444",
            "Major gap": "#f97316",
            "Minor gap": "#f59e0b",
            "Parity": "#22c55e",
        }
        gap_label = row.get("gap_label", "Minor gap")
        color = SUPPORT_LEVEL_COLORS.get(row["nvidia_support"], "#94a3b8")

        fig.add_trace(go.Bar(
            x=[row["gap_score"]],
            y=[f"{row['project']}: {row['feature']}"],
            orientation="h",
            marker_color=color,
            hovertext=f"Priority: P{row['priority']}<br>Gap: {row['nvidia_support']} vs {row['amd_support']}<br>{row['blockers']}",
            hoverinfo="text",
            showlegend=False,
        ))

    fig.update_layout(
        height=max(400, min(len(df) * 30, 800)),
        margin=dict(l=200, r=20, t=30, b=30),
        xaxis_title="Gap Score",
        yaxis=dict(showticklabels=True),
        plot_bgcolor="white",
        paper_bgcolor="white",
    )

    return df, fig
