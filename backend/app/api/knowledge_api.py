"""API to serve structured knowledge base data to the frontend Dashboard.

Data model (new in this revision):
    known_gaps:
      layer1_model:
        features: [ ... ]   # capability missing on AMD
        bugs:     [ ... ]   # capability present but broken / regressed
      layer2_serving:
        features: [ ... ]
        bugs:     [ ... ]

Only L1 (Model) and L2 (Serving Architecture) layers are active in the dashboard.
L3 (Kernel / Quantization) and L4 (Engineering / Ecosystem) are intentionally
descoped for the MVP — kept in ``background_context`` inside each project YAML
as narrative reference, not as active tracks.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

import yaml

from app.agent.knowledge import (
    KNOWLEDGE_DIR,
    list_known_projects,
    load_hardware_context,
    load_project_knowledge,
)

OP_COVERAGE_PATH = KNOWLEDGE_DIR / "operator_coverage.yaml"
MODEL_PRECISION_PATH = KNOWLEDGE_DIR / "model_precision.yaml"

logger = logging.getLogger(__name__)
router = APIRouter()

ACTIVE_LAYERS: tuple[str, ...] = ("layer1_model", "layer2_serving")
GAP_TYPES: tuple[str, ...] = ("features", "bugs")


def _iter_layer_gaps(kb: dict[str, Any], layer_key: str) -> list[tuple[str, dict[str, Any]]]:
    """Yield (gap_type, gap_dict) pairs for a given layer, supporting both the
    new ``features`` / ``bugs`` split and the legacy flat-list form.
    """
    known_gaps = kb.get("known_gaps", {}) or {}
    layer_node = known_gaps.get(layer_key)
    if layer_node is None:
        return []

    if isinstance(layer_node, dict):
        out: list[tuple[str, dict[str, Any]]] = []
        for gtype in GAP_TYPES:
            items = layer_node.get(gtype) or []
            if isinstance(items, list):
                for g in items:
                    if isinstance(g, dict):
                        out.append((gtype, g))
        return out

    if isinstance(layer_node, list):
        out = []
        for g in layer_node:
            if not isinstance(g, dict):
                continue
            gtype_raw = str(g.get("type", "")).lower()
            gtype = "bugs" if gtype_raw == "bug" else "features"
            out.append((gtype, g))
        return out

    return []


def _enrich_gap(gap: dict[str, Any], *, project_id: str, project_name: str,
                layer_key: str, gap_type: str, index_in_type: int) -> dict[str, Any]:
    entry = dict(gap)
    entry["project_id"] = project_id
    entry["project_name"] = project_name
    entry["layer"] = layer_key
    entry["gap_type"] = "bug" if gap_type == "bugs" else "feature"
    entry["index_in_type"] = index_in_type
    entry.setdefault("severity", "medium")
    entry.setdefault("trajectory", "stable")
    entry.setdefault("status", "active")
    entry.setdefault("discovered_at", "2026-04-15")
    entry.setdefault("last_verified", "2026-04-16")
    return entry


def _collect_all_gaps(kb: dict[str, Any]) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    proj = kb.get("project", {}) or {}
    pid = proj.get("id", "")
    pname = proj.get("name", pid)
    for layer_key in ACTIVE_LAYERS:
        pairs = _iter_layer_gaps(kb, layer_key)
        per_type_counter: dict[str, int] = {"features": 0, "bugs": 0}
        for gtype, g in pairs:
            idx = per_type_counter[gtype]
            per_type_counter[gtype] += 1
            gaps.append(_enrich_gap(g, project_id=pid, project_name=pname,
                                    layer_key=layer_key, gap_type=gtype,
                                    index_in_type=idx))
    return gaps


@router.get("/projects")
async def get_projects() -> list[dict[str, Any]]:
    projects = []
    for pid in list_known_projects():
        kb = load_project_knowledge(pid)
        if not kb:
            continue
        proj = kb.get("project", {}) or {}
        all_gaps = _collect_all_gaps(kb)
        total = len(all_gaps)
        critical = sum(1 for g in all_gaps if g.get("severity") == "critical")
        closing = sum(1 for g in all_gaps if "closing" in g.get("trajectory", ""))
        feature_count = sum(1 for g in all_gaps if g.get("gap_type") == "feature")
        bug_count = sum(1 for g in all_gaps if g.get("gap_type") == "bug")
        projects.append({
            "id": pid,
            "name": proj.get("name", pid),
            "repo": proj.get("repo", ""),
            "category": proj.get("category", ""),
            "summary": proj.get("expert_summary", ""),
            "gap_count": total,
            "feature_count": feature_count,
            "bug_count": bug_count,
            "critical_count": critical,
            "closing_count": closing,
        })
    return projects


@router.get("/summary")
async def get_summary() -> dict[str, Any]:
    all_gaps: list[dict[str, Any]] = []
    project_count = 0
    for pid in list_known_projects():
        kb = load_project_knowledge(pid)
        if not kb:
            continue
        project_count += 1
        all_gaps.extend(_collect_all_gaps(kb))

    critical = sum(1 for g in all_gaps if g.get("severity") == "critical")
    high = sum(1 for g in all_gaps if g.get("severity") == "high")
    closing = sum(1 for g in all_gaps if "closing" in g.get("trajectory", ""))
    widening = sum(1 for g in all_gaps if "widening" in g.get("trajectory", ""))
    feature_count = sum(1 for g in all_gaps if g.get("gap_type") == "feature")
    bug_count = sum(1 for g in all_gaps if g.get("gap_type") == "bug")

    pending_updates = sum(
        1 for g in all_gaps
        for u in (g.get("updates") or [])
        if isinstance(u, dict) and not u.get("reviewed")
    )

    return {
        "total_gaps": len(all_gaps),
        "project_count": project_count,
        "critical": critical,
        "high": high,
        "closing": closing,
        "widening": widening,
        "feature_count": feature_count,
        "bug_count": bug_count,
        "pending_updates": pending_updates,
    }


@router.get("/gaps")
async def get_all_gaps(
    project: str | None = None,
    layer: str | None = None,
    gap_type: str | None = None,
) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """Return all active-layer gaps organized as {layer: {features: [...], bugs: [...]}}.

    Query params:
      - project : filter by project_id (e.g. "vllm", "sglang")
      - layer   : filter by layer key (e.g. "layer1_model")
      - gap_type: filter by "feature" or "bug"
    """
    result: dict[str, dict[str, list[dict[str, Any]]]] = {
        k: {"features": [], "bugs": []} for k in ACTIVE_LAYERS
    }

    pids = [project] if project else list_known_projects()
    for pid in pids:
        kb = load_project_knowledge(pid)
        if not kb:
            continue
        proj = kb.get("project", {}) or {}
        pname = proj.get("name", pid)

        for layer_key in ACTIVE_LAYERS:
            if layer and layer != layer_key:
                continue
            pairs = _iter_layer_gaps(kb, layer_key)
            per_type_counter: dict[str, int] = {"features": 0, "bugs": 0}
            for gtype, g in pairs:
                idx = per_type_counter[gtype]
                per_type_counter[gtype] += 1
                if gap_type:
                    want = "features" if gap_type.lower().startswith("feat") else "bugs"
                    if gtype != want:
                        continue
                entry = _enrich_gap(g, project_id=pid, project_name=pname,
                                    layer_key=layer_key, gap_type=gtype,
                                    index_in_type=idx)
                result[layer_key][gtype].append(entry)

    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    for layer_key in result:
        for gtype in result[layer_key]:
            result[layer_key][gtype].sort(
                key=lambda g: severity_order.get(g.get("severity", "medium"), 2)
            )

    return result


@router.get("/gaps/{project_id}/{layer}/{gap_type}/{gap_index}")
async def get_gap_detail(
    project_id: str,
    layer: str,
    gap_type: str,
    gap_index: int,
) -> dict[str, Any]:
    """Return full detail of a single gap, keyed by (project, layer, type, index-in-type)."""
    kb = load_project_knowledge(project_id)
    if not kb:
        return {"error": "Project not found"}
    if layer not in ACTIVE_LAYERS:
        return {"error": f"Layer '{layer}' is not active (use one of {ACTIVE_LAYERS})"}

    want = "features" if gap_type.lower().startswith("feat") else "bugs"
    pairs = _iter_layer_gaps(kb, layer)
    filtered = [g for (gtype, g) in pairs if gtype == want]
    if gap_index < 0 or gap_index >= len(filtered):
        return {"error": "Gap not found"}

    proj = kb.get("project", {}) or {}
    gap = _enrich_gap(
        filtered[gap_index],
        project_id=project_id,
        project_name=proj.get("name", project_id),
        layer_key=layer,
        gap_type=want,
        index_in_type=gap_index,
    )
    gap["project_summary"] = proj.get("expert_summary", "")

    feature_id = gap.get("feature", gap.get("model", gap.get("id", "")))
    features = kb.get("features", {}) or {}
    if feature_id and feature_id in features:
        gap["feature_detail"] = features[feature_id]

    return gap


@router.get("/gaps-legacy/{project_id}/{layer}/{gap_index}")
async def get_gap_detail_legacy(project_id: str, layer: str, gap_index: int) -> dict[str, Any]:
    """Compatibility shim: treat the global gap_index within a layer as an
    ordered concatenation of features followed by bugs.
    """
    kb = load_project_knowledge(project_id)
    if not kb or layer not in ACTIVE_LAYERS:
        return {"error": "Not found"}
    pairs = _iter_layer_gaps(kb, layer)
    if gap_index < 0 or gap_index >= len(pairs):
        return {"error": "Gap not found"}
    gtype, g = pairs[gap_index]
    features = [p for p in pairs if p[0] == "features"]
    bugs = [p for p in pairs if p[0] == "bugs"]
    if gtype == "features":
        idx_in_type = next(i for i, (_, cand) in enumerate(features) if cand is g)
    else:
        idx_in_type = next(i for i, (_, cand) in enumerate(bugs) if cand is g)
    return await get_gap_detail(project_id, layer, gtype, idx_in_type)


@router.get("/hardware")
async def get_hardware() -> dict[str, Any]:
    return load_hardware_context()


@router.get("/operator-coverage")
async def get_operator_coverage() -> dict[str, Any]:
    if not OP_COVERAGE_PATH.exists():
        return {"error": "Operator coverage data not found"}
    with open(OP_COVERAGE_PATH) as f:
        return yaml.safe_load(f) or {}


@router.get("/model-precision")
async def get_model_precision() -> dict[str, Any]:
    if not MODEL_PRECISION_PATH.exists():
        return {"error": "Model precision data not found"}
    with open(MODEL_PRECISION_PATH) as f:
        return yaml.safe_load(f) or {}
