from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from lib.types import (
    Category,
    FeatureComparison,
    ProjectEntry,
    ProjectType,
    SupportLevel,
    SUPPORT_LEVEL_LABELS,
)

BASE_DIR = Path(__file__).parent.parent
DATA_DIR = BASE_DIR / "data"


def load_registry() -> list[ProjectEntry]:
    with open(DATA_DIR / "repo_registry.json") as f:
        data = json.load(f)
    entries = []
    for p in data["projects"]:
        entries.append(ProjectEntry(
            id=p["id"],
            type=ProjectType(p["type"]),
            category=Category(p["category"]),
            description=p.get("description", ""),
            nv_repo=p["nv_repo"],
            amd_repo=p.get("amd_repo"),
            nv_name=p.get("nv_name"),
            amd_name=p.get("amd_name"),
            priority=p.get("priority", 3),
        ))
    return entries


def load_metrics(project_id: str, side: str = "") -> Optional[dict]:
    if side:
        path = DATA_DIR / "github_metrics" / f"{project_id}_{side}.json"
    else:
        path = DATA_DIR / "github_metrics" / f"{project_id}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


def load_analysis(project_id: str) -> Optional[dict]:
    path = DATA_DIR / "analysis" / f"{project_id}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return None


@dataclass
class ProjectDashboardData:
    entry: ProjectEntry
    metrics: Optional[dict] = None
    analysis: Optional[dict] = None
    amd_metrics: Optional[dict] = None


def load_all_dashboard_data() -> list[ProjectDashboardData]:
    registry = load_registry()
    results = []
    for entry in registry:
        data = ProjectDashboardData(entry=entry)

        if entry.type == ProjectType.DUAL_PLATFORM:
            data.metrics = load_metrics(entry.id)
            data.analysis = load_analysis(entry.id)
        elif entry.type == ProjectType.NV_AMD_PAIR:
            data.metrics = load_metrics(entry.id, "nv")
            data.amd_metrics = load_metrics(entry.id, "amd")
            data.analysis = load_analysis(entry.id)

        results.append(data)
    return results


def get_overall_amd_support(analysis: Optional[dict]) -> SupportLevel:
    if not analysis:
        return SupportLevel.NONE
    level = analysis.get("rocm_support_level", "none")
    try:
        return SupportLevel(level)
    except ValueError:
        return SupportLevel.NONE


def get_feature_gaps(analysis: Optional[dict]) -> list[dict]:
    if not analysis:
        return []
    features = analysis.get("features", [])
    gaps = []
    for feat in features:
        nv_level = feat.get("nvidia_support", "none")
        amd_level = feat.get("amd_support", "none")
        try:
            nv = SupportLevel(nv_level)
            amd = SupportLevel(amd_level)
        except ValueError:
            continue
        if nv != amd and nv != SupportLevel.NONE:
            gap_score = _support_gap_score(nv, amd)
            if gap_score > 0:
                gaps.append({
                    "name": feat.get("name", ""),
                    "nvidia_support": nv,
                    "amd_support": amd,
                    "description": feat.get("description", ""),
                    "blockers": feat.get("blockers", []),
                    "priority": feat.get("priority", 3),
                    "gap_score": gap_score,
                })
    return sorted(gaps, key=lambda g: g["gap_score"], reverse=True)


def get_pair_gaps(analysis: Optional[dict]) -> list[dict]:
    if not analysis:
        return []
    comparison = analysis.get("comparison", {})
    gaps = []

    for feat in comparison.get("shared_features", []):
        nv_level = feat.get("nvidia_support", "none")
        amd_level = feat.get("amd_support", "none")
        try:
            nv = SupportLevel(nv_level)
            amd = SupportLevel(amd_level)
        except ValueError:
            continue
        gap_score = _support_gap_score(nv, amd)
        gaps.append({
            "name": feat.get("name", ""),
            "nvidia_support": nv,
            "amd_support": amd,
            "description": feat.get("description", ""),
            "blockers": feat.get("blockers", []),
            "priority": feat.get("priority", 3),
            "gap_score": gap_score,
        })

    for feat in comparison.get("nv_only_features", []):
        gaps.append({
            "name": feat.get("name", ""),
            "nvidia_support": SupportLevel.FIRST_CLASS,
            "amd_support": SupportLevel.NONE,
            "description": feat.get("description", ""),
            "blockers": ["NVIDIA-only feature, no AMD equivalent"],
            "priority": feat.get("priority", 3) if "priority" in feat else 3,
            "gap_score": 3,
        })

    return sorted(gaps, key=lambda g: g["gap_score"], reverse=True)


def _support_gap_score(nv: SupportLevel, amd: SupportLevel) -> int:
    scores = {
        SupportLevel.FIRST_CLASS: 3,
        SupportLevel.EXPERIMENTAL: 2,
        SupportLevel.COMMUNITY: 1,
        SupportLevel.NONE: 0,
    }
    return scores.get(nv, 0) - scores.get(amd, 0)
