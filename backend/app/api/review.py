"""
Review API — expert reviews and approves/rejects Agent findings.
Updates flow back into the YAML knowledge base.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel

from app.agent.knowledge import (
    load_project_knowledge,
    list_known_projects,
    update_feature_status,
    add_agent_finding,
)

logger = logging.getLogger(__name__)
router = APIRouter()


class ReviewAction(BaseModel):
    project_id: str
    feature_id: str
    action: str  # approve | reject | update
    new_status: str | None = None
    confidence: str = "high"
    notes: str = ""


class FeatureUpdate(BaseModel):
    project_id: str
    feature_id: str
    amd_status: str
    confidence: str = "high"
    notes: str = ""
    evidence_url: str | None = None
    evidence_type: str | None = None


@router.get("/knowledge")
async def list_knowledge_projects() -> list[str]:
    return list_known_projects()


@router.get("/knowledge/{project_id}")
async def get_project_knowledge(project_id: str) -> dict[str, Any]:
    kb = load_project_knowledge(project_id)
    if kb is None:
        return {"error": f"No knowledge base for {project_id}"}
    return kb


@router.get("/knowledge/{project_id}/pending")
async def get_pending_findings(project_id: str) -> list[dict[str, Any]]:
    """Get all agent findings that haven't been reviewed yet."""
    kb = load_project_knowledge(project_id)
    if kb is None:
        return []

    pending = []
    for fid, spec in kb.get("features", {}).items():
        for finding in spec.get("agent_findings", []):
            pending.append({
                "feature_id": fid,
                "feature_name": spec.get("name", fid),
                "current_status": spec.get("amd_status", "unknown"),
                "confidence": spec.get("confidence", "low"),
                "finding": finding,
            })

    # Also include features with pending_verification status
    for fid, spec in kb.get("features", {}).items():
        if spec.get("amd_status") == "pending_verification" and not spec.get("agent_findings"):
            pending.append({
                "feature_id": fid,
                "feature_name": spec.get("name", fid),
                "current_status": "pending_verification",
                "confidence": spec.get("confidence", "low"),
                "finding": {
                    "title": f"Status is pending_verification for {spec.get('name', fid)}",
                    "impact": "needs_review",
                    "amd_effect": spec.get("expert_notes", ""),
                },
            })

    return pending


@router.post("/review")
async def review_finding(action: ReviewAction) -> dict[str, Any]:
    """Expert reviews an Agent finding: approve, reject, or update."""
    kb = load_project_knowledge(action.project_id)
    if kb is None:
        return {"error": f"No knowledge base for {action.project_id}"}

    features = kb.get("features", {})
    if action.feature_id not in features:
        return {"error": f"Feature {action.feature_id} not found"}

    if action.action == "approve":
        new_status = action.new_status or _infer_status_from_findings(
            features[action.feature_id]
        )
        success = update_feature_status(
            action.project_id,
            action.feature_id,
            new_status=new_status,
            confidence=action.confidence,
            source="expert",
            notes=action.notes,
        )
        if success:
            _clear_agent_findings(action.project_id, action.feature_id)
        return {"status": "approved", "new_status": new_status, "success": success}

    elif action.action == "reject":
        _clear_agent_findings(action.project_id, action.feature_id)
        if action.notes:
            update_feature_status(
                action.project_id,
                action.feature_id,
                new_status=features[action.feature_id].get("amd_status", "unknown"),
                confidence=action.confidence,
                source="expert",
                notes=action.notes,
            )
        return {"status": "rejected"}

    elif action.action == "update":
        if not action.new_status:
            return {"error": "new_status required for update action"}
        success = update_feature_status(
            action.project_id,
            action.feature_id,
            new_status=action.new_status,
            confidence=action.confidence,
            source="expert",
            notes=action.notes,
        )
        _clear_agent_findings(action.project_id, action.feature_id)
        return {"status": "updated", "new_status": action.new_status, "success": success}

    return {"error": f"Unknown action: {action.action}"}


@router.post("/feature")
async def update_feature(update: FeatureUpdate) -> dict[str, Any]:
    """Expert directly updates a feature status (e.g. from chat/conversation)."""
    evidence = None
    if update.evidence_url:
        evidence = [{
            "url": update.evidence_url,
            "type": update.evidence_type or "expert",
            "summary": update.notes,
        }]

    success = update_feature_status(
        update.project_id,
        update.feature_id,
        new_status=update.amd_status,
        confidence=update.confidence,
        source="expert",
        notes=update.notes,
        evidence=evidence,
    )
    return {"success": success}


def _infer_status_from_findings(feature: dict[str, Any]) -> str:
    """If agent found positive signals, infer a status upgrade."""
    findings = feature.get("agent_findings", [])
    if not findings:
        return feature.get("amd_status", "unknown")

    positive = sum(1 for f in findings if f.get("impact") in ("new_support", "improvement", "bug_fix"))
    negative = sum(1 for f in findings if f.get("impact") in ("regression", "bug_report"))

    current = feature.get("amd_status", "unknown")
    if positive > negative and current in ("unknown", "pending_verification"):
        return "confirmed_partial"
    elif positive > 0 and negative == 0 and current == "confirmed_partial":
        return "confirmed_available"
    return current


def _clear_agent_findings(project_id: str, feature_id: str) -> None:
    """Remove agent_findings after review."""
    import yaml
    from app.agent.knowledge import PROJECTS_DIR

    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return

    with open(path) as f:
        kb = yaml.safe_load(f) or {}

    features = kb.get("features", {})
    if feature_id in features:
        features[feature_id].pop("agent_findings", None)

    with open(path, "w") as f:
        yaml.dump(kb, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
