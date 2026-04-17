"""
Matrix Assembler — aggregates classified evidence into a feature matrix,
computes per-dimension scores, and produces the final structured gap report.
No LLM calls needed — pure deterministic logic.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

IMPACT_POSITIVE = {"new_support", "improvement", "bug_fix"}
IMPACT_NEGATIVE = {"regression", "bug_report"}
IMPACT_NEUTRAL = {"request", "info", "none"}

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "none": 4}

# AMD status priority: higher index = better status
STATUS_RANK = {
    "unavailable": 0,
    "broken": 1,
    "unknown": 2,
    "partial": 3,
    "experimental": 4,
    "available": 5,
}


def assemble_matrix(
    taxonomy: dict[str, dict[str, Any]],
    tagged_evidence: list[dict[str, Any]],
    crawl_stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a feature matrix from classified evidence.

    Returns:
        {
            "features": {feature_id: {dimension, description, nvidia_status, amd_status, evidence, ...}},
            "dimensions": {dimension_name: {score, feature_count, gap_count}},
            "gaps": [list of gap dicts],
            "overall_score": float 0-100,
            "stats": {...}
        }
    """
    matrix: dict[str, dict[str, Any]] = {}
    for fid, spec in taxonomy.items():
        matrix[fid] = {
            "id": fid,
            "dimension": spec.get("dimension", "feature_availability"),
            "description": spec.get("description", fid),
            "nvidia_status": "available",
            "amd_status": "unknown",
            "evidence": [],
            "positive_signals": 0,
            "negative_signals": 0,
            "latest_date": None,
        }

    for item in tagged_evidence:
        features = item.get("features") or []
        impact = item.get("impact", "none")
        confidence = item.get("confidence", "low")

        for fid in features:
            if fid not in matrix:
                continue
            cell = matrix[fid]
            cell["evidence"].append({
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "type": item.get("evidence_type", ""),
                "impact": impact,
                "amd_effect": item.get("amd_effect", ""),
                "hardware": item.get("hardware", "unknown"),
                "confidence": confidence,
                "severity": item.get("severity", "none"),
                "date": item.get("date", ""),
            })

            if impact in IMPACT_POSITIVE:
                cell["positive_signals"] += 1
            elif impact in IMPACT_NEGATIVE:
                cell["negative_signals"] += 1

            item_date = item.get("date", "")
            if item_date and (cell["latest_date"] is None or item_date > cell["latest_date"]):
                cell["latest_date"] = item_date

    _infer_amd_statuses(matrix)

    dimensions = _score_dimensions(matrix)
    gaps = _extract_gaps(matrix)
    overall = _compute_overall(dimensions)

    return {
        "features": matrix,
        "dimensions": dimensions,
        "gaps": gaps,
        "overall_score": overall,
        "stats": crawl_stats or {},
        "assembled_at": datetime.now(timezone.utc).isoformat(),
    }


def _infer_amd_statuses(matrix: dict[str, dict[str, Any]]) -> None:
    for fid, cell in matrix.items():
        pos = cell["positive_signals"]
        neg = cell["negative_signals"]
        evidence = cell["evidence"]

        if not evidence:
            cell["amd_status"] = "unknown"
            continue

        has_merged_pr = any(
            e["type"] == "pull_request_merged" and e["impact"] in IMPACT_POSITIVE
            for e in evidence
        )
        has_ci = any(e["type"] == "ci_config" for e in evidence)
        has_open_bug = any(
            e["impact"] == "bug_report" and e["type"] == "issue"
            for e in evidence
        )
        has_new_support = any(e["impact"] == "new_support" for e in evidence)
        has_regression = any(e["impact"] == "regression" for e in evidence)

        if has_regression and neg > pos:
            cell["amd_status"] = "broken"
        elif has_new_support or (has_merged_pr and pos > neg):
            if has_open_bug:
                cell["amd_status"] = "partial"
            else:
                cell["amd_status"] = "available"
        elif has_merged_pr:
            cell["amd_status"] = "partial"
        elif pos > 0 and pos >= neg:
            cell["amd_status"] = "experimental"
        elif neg > pos:
            cell["amd_status"] = "partial" if pos > 0 else "unavailable"
        else:
            cell["amd_status"] = "unknown"


def _score_dimensions(matrix: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    dim_features: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for cell in matrix.values():
        dim_features[cell["dimension"]].append(cell)

    dimensions = {}
    for dim, features in dim_features.items():
        if not features:
            dimensions[dim] = {"score": None, "feature_count": 0, "gap_count": 0}
            continue

        scores = []
        gap_count = 0
        for f in features:
            amd = f["amd_status"]
            s = _status_to_score(amd)
            scores.append(s)
            if amd in ("unavailable", "broken", "unknown"):
                gap_count += 1
            elif amd == "partial":
                gap_count += 0.5

        avg = sum(scores) / len(scores) if scores else 50
        dimensions[dim] = {
            "score": round(avg, 1),
            "feature_count": len(features),
            "gap_count": gap_count,
        }

    return dimensions


def _status_to_score(status: str) -> float:
    return {
        "available": 95,
        "experimental": 70,
        "partial": 50,
        "unknown": 40,
        "broken": 15,
        "unavailable": 5,
    }.get(status, 40)


def _extract_gaps(matrix: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    gaps = []
    for fid, cell in matrix.items():
        amd = cell["amd_status"]
        if amd in ("available",):
            continue

        best_evidence = _pick_best_evidence(cell["evidence"])
        hw = best_evidence.get("hardware", "unknown") if best_evidence else "unknown"

        severity = _infer_severity(cell)

        gap = {
            "feature_id": fid,
            "type": _gap_type_from_dimension(cell["dimension"]),
            "name": cell["description"],
            "dimension": cell["dimension"],
            "nvidia_status": cell["nvidia_status"],
            "amd_status": amd,
            "severity": severity,
            "evidence_url": best_evidence.get("url", "") if best_evidence else "",
            "evidence_type": best_evidence.get("type", "") if best_evidence else "",
            "blocker": best_evidence.get("amd_effect", "") if best_evidence else "",
            "hardware": hw,
            "hw_generation_match": hw not in ("unknown", ""),
            "confidence": best_evidence.get("confidence", "low") if best_evidence else "low",
            "evidence_count": len(cell["evidence"]),
        }
        gaps.append(gap)

    gaps.sort(key=lambda g: (SEVERITY_ORDER.get(g["severity"], 4), -g["evidence_count"]))
    return gaps


def _pick_best_evidence(evidence: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not evidence:
        return None
    confidence_rank = {"high": 0, "medium": 1, "low": 2}
    scored = sorted(
        evidence,
        key=lambda e: (
            confidence_rank.get(e.get("confidence", "low"), 2),
            0 if e.get("url") else 1,
            -(len(e.get("date", ""))),
        ),
    )
    return scored[0]


def _infer_severity(cell: dict[str, Any]) -> str:
    amd = cell["amd_status"]
    neg = cell["negative_signals"]

    if amd == "unavailable":
        return "critical" if neg > 0 else "high"
    elif amd == "broken":
        return "critical"
    elif amd == "unknown":
        return "medium" if cell["evidence"] else "low"
    elif amd in ("partial", "experimental"):
        return "medium" if neg > 0 else "low"
    return "none"


def _gap_type_from_dimension(dimension: str) -> str:
    return {
        "model_support": "model",
        "feature_availability": "feature",
        "performance_parity": "performance",
        "kernel_backend": "kernel",
        "engineering_maturity": "feature",
    }.get(dimension, "feature")


def _compute_overall(dimensions: dict[str, dict[str, Any]]) -> float:
    weights = {
        "model_support": 0.25,
        "feature_availability": 0.25,
        "performance_parity": 0.15,
        "kernel_backend": 0.20,
        "engineering_maturity": 0.15,
    }
    total_weight = 0.0
    weighted_sum = 0.0
    for dim, info in dimensions.items():
        score = info.get("score")
        if score is None:
            continue
        w = weights.get(dim, 0.1)
        weighted_sum += score * w
        total_weight += w

    if total_weight == 0:
        return 50.0
    return round(weighted_sum / total_weight, 1)


def format_matrix_for_summary(assembled: dict[str, Any]) -> str:
    """Compact text representation for the final LLM summary call."""
    lines = []
    lines.append(f"Overall score: {assembled['overall_score']}/100")
    lines.append("")

    for dim, info in assembled["dimensions"].items():
        score = info.get("score", "N/A")
        lines.append(f"[{dim}] score={score}, features={info['feature_count']}, gaps={info['gap_count']}")

    lines.append("")
    lines.append("Top gaps:")
    for gap in assembled["gaps"][:15]:
        lines.append(
            f"  - [{gap['severity']}] {gap['name']} "
            f"(NV={gap['nvidia_status']}, AMD={gap['amd_status']}, "
            f"hw={gap['hardware']}, evidence={gap.get('evidence_url','')})"
        )

    return "\n".join(lines)
