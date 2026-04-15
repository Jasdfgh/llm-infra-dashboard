from __future__ import annotations

import pandas as pd
import streamlit as st
from lib.types import SUPPORT_LEVEL_COLORS, SUPPORT_LEVEL_LABELS, SupportLevel


def render_dual_platform_table(analysis: dict) -> pd.DataFrame:
    if not analysis:
        return pd.DataFrame()

    features = analysis.get("features", [])
    rows = []
    for feat in features:
        nv = feat.get("nvidia_support", "none")
        amd = feat.get("amd_support", "none")
        try:
            nv_level = SupportLevel(nv)
            amd_level = SupportLevel(amd)
        except ValueError:
            nv_level = SupportLevel.NONE
            amd_level = SupportLevel.NONE

        rows.append({
            "Feature": feat.get("name", ""),
            "NVIDIA": SUPPORT_LEVEL_LABELS[nv_level],
            "AMD": SUPPORT_LEVEL_LABELS[amd_level],
            "Gap": _gap_emoji(nv_level, amd_level),
            "Priority": _priority_label(feat.get("priority", 3)),
            "Description": feat.get("description", ""),
            "Blockers": "; ".join(feat.get("blockers", [])),
        })
    return pd.DataFrame(rows)


def render_pair_table(analysis: dict) -> pd.DataFrame:
    if not analysis:
        return pd.DataFrame()

    comparison = analysis.get("comparison", {})
    rows = []

    for feat in comparison.get("shared_features", []):
        nv = feat.get("nvidia_support", "none")
        amd = feat.get("amd_support", "none")
        try:
            nv_level = SupportLevel(nv)
            amd_level = SupportLevel(amd)
        except ValueError:
            nv_level = SupportLevel.NONE
            amd_level = SupportLevel.NONE

        rows.append({
            "Feature": feat.get("name", ""),
            "NVIDIA (CUTLASS/etc)": SUPPORT_LEVEL_LABELS[nv_level],
            "AMD (CK/etc)": SUPPORT_LEVEL_LABELS[amd_level],
            "Gap": _gap_emoji(nv_level, amd_level),
            "Priority": _priority_label(feat.get("priority", 3)),
            "Description": feat.get("description", ""),
            "Blockers": "; ".join(feat.get("blockers", [])),
        })

    for feat in comparison.get("nv_only_features", []):
        rows.append({
            "Feature": feat.get("name", ""),
            "NVIDIA (CUTLASS/etc)": "First-class",
            "AMD (CK/etc)": "None",
            "Gap": "Not available",
            "Priority": _priority_label(feat.get("priority", 3) if "priority" in feat else 3),
            "Description": feat.get("description", ""),
            "Blockers": "NVIDIA-only feature",
        })

    for feat in comparison.get("amd_only_features", []):
        rows.append({
            "Feature": feat.get("name", ""),
            "NVIDIA (CUTLASS/etc)": "None",
            "AMD (CK/etc)": "Available",
            "Gap": "AMD advantage",
            "Priority": "-",
            "Description": feat.get("description", ""),
            "Blockers": "",
        })

    return pd.DataFrame(rows)


def _gap_emoji(nv: SupportLevel, amd: SupportLevel) -> str:
    scores = {
        SupportLevel.FIRST_CLASS: 3,
        SupportLevel.EXPERIMENTAL: 2,
        SupportLevel.COMMUNITY: 1,
        SupportLevel.NONE: 0,
    }
    gap = scores.get(nv, 0) - scores.get(amd, 0)
    if gap == 0:
        return "Parity"
    elif gap == 1:
        return "Minor gap"
    elif gap == 2:
        return "Major gap"
    else:
        return "Critical gap"


def _priority_label(p: int) -> str:
    labels = {1: "P0 Critical", 2: "P1 High", 3: "P2 Medium", 4: "P3 Low", 5: "P4 Nice-to-have"}
    return labels.get(p, f"P{p}")
