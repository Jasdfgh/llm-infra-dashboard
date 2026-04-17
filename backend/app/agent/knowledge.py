"""
Knowledge Base loader — reads expert-curated YAML files and provides
a structured view of the current known state for each project.
Agent uses this to do TARGETED evidence collection, not open-ended crawling.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

KNOWLEDGE_DIR = Path(__file__).parent.parent.parent / "knowledge"
PROJECTS_DIR = KNOWLEDGE_DIR / "projects"


def load_hardware_context() -> dict[str, Any]:
    path = KNOWLEDGE_DIR / "hardware.yaml"
    if not path.exists():
        logger.warning("hardware.yaml not found at %s", path)
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_project_knowledge(project_id: str) -> dict[str, Any] | None:
    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f) or {}


def list_known_projects() -> list[str]:
    if not PROJECTS_DIR.exists():
        return []
    return [p.stem for p in PROJECTS_DIR.glob("*.yaml") if p.stem != "_template"]


def get_features_needing_verification(kb: dict[str, Any]) -> list[dict[str, Any]]:
    """Features where status is pending_verification or unknown."""
    results = []
    for fid, spec in kb.get("features", {}).items():
        status = spec.get("amd_status", "unknown")
        if status in ("pending_verification", "unknown"):
            results.append({"id": fid, **spec})
    return results


def get_search_hints(kb: dict[str, Any]) -> list[str]:
    """Collect all agent_search_hints from features + agent_focus."""
    hints: list[str] = []
    for spec in kb.get("features", {}).values():
        hints.extend(spec.get("agent_search_hints", []))
    focus = kb.get("agent_focus", {})
    hints.extend(focus.get("priority_searches", []))
    return hints


def get_watch_for(kb: dict[str, Any]) -> list[str]:
    return kb.get("agent_focus", {}).get("watch_for", [])


def get_ignore_patterns(kb: dict[str, Any]) -> list[str]:
    return kb.get("agent_focus", {}).get("ignore", [])


def update_feature_status(
    project_id: str,
    feature_id: str,
    new_status: str,
    confidence: str = "medium",
    source: str = "expert",
    notes: str = "",
    evidence: list[dict[str, str]] | None = None,
) -> bool:
    """Update a feature's AMD status in the knowledge base YAML file."""
    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        logger.warning("Knowledge file not found for %s", project_id)
        return False

    with open(path) as f:
        kb = yaml.safe_load(f) or {}

    features = kb.get("features", {})
    if feature_id not in features:
        logger.warning("Feature %s not found in %s", feature_id, project_id)
        return False

    feat = features[feature_id]
    feat["amd_status"] = new_status
    feat["confidence"] = confidence
    feat["source"] = source
    if notes:
        feat["expert_notes"] = notes
    if evidence:
        existing = feat.get("evidence", [])
        existing.extend(evidence)
        feat["evidence"] = existing

    from datetime import date
    feat["last_verified"] = date.today().isoformat()

    with open(path, "w") as f:
        yaml.dump(kb, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    logger.info("Updated %s.%s -> %s (by %s)", project_id, feature_id, new_status, source)
    return True


def add_agent_finding(
    project_id: str,
    feature_id: str,
    finding: dict[str, Any],
) -> bool:
    """Agent appends a finding to a feature's evidence list (status stays unchanged
    until expert confirms)."""
    path = PROJECTS_DIR / f"{project_id}.yaml"
    if not path.exists():
        return False

    with open(path) as f:
        kb = yaml.safe_load(f) or {}

    features = kb.get("features", {})
    if feature_id not in features:
        return False

    feat = features[feature_id]
    pending = feat.get("agent_findings", [])
    pending.append(finding)
    feat["agent_findings"] = pending

    with open(path, "w") as f:
        yaml.dump(kb, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    return True
