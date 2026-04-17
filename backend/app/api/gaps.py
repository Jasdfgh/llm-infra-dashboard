from __future__ import annotations

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.database import Gap, GapReport, Project, get_db
from app.models.schemas import DashboardSummary, GapItem

router = APIRouter()


def _confidence_float(raw: str) -> float:
    m = {"high": 0.85, "medium": 0.55, "low": 0.25}
    return m.get(str(raw).lower(), 0.5)


def _gap_item(g: Gap) -> GapItem:
    return GapItem(
        id=str(g.id),
        project_id=g.project_id,
        gap_type=g.gap_type,
        name=g.name,
        description=g.description,
        severity=g.severity,
        nvidia_status=g.nvidia_status,
        amd_status=g.amd_status,
        nvidia_hw=g.nvidia_hw,
        amd_hw=g.amd_hw,
        hw_generation_match=g.hw_generation_match,
        blocker=g.blocker,
        evidence_url=g.evidence_url,
        evidence_type=g.evidence_type,
        confidence=_confidence_float(g.confidence),
        is_resolved=g.is_resolved,
        created_at=g.created_at,
    )


def _severity_rank(sev: str) -> int:
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    return order.get(str(sev).lower(), 9)


async def _latest_report_ids(session: AsyncSession) -> set[int]:
    res = await session.execute(select(GapReport).order_by(GapReport.id.asc()))
    reports = list(res.scalars())
    latest: dict[str, GapReport] = {}
    for r in reports:
        latest[r.project_id] = r
    return {r.id for r in latest.values()}


@router.get("/")
async def list_gaps(
    db: Annotated[AsyncSession, Depends(get_db)],
    gap_type: Optional[str] = None,
    severity: Optional[str] = None,
    nvidia_hw: Optional[str] = None,
    amd_hw: Optional[str] = None,
    category: Optional[str] = None,
) -> list[GapItem]:
    latest_ids = await _latest_report_ids(db)
    if not latest_ids:
        return []
    stmt = select(Gap).where(Gap.report_id.in_(latest_ids))
    if gap_type:
        stmt = stmt.where(Gap.gap_type == gap_type)
    if severity:
        stmt = stmt.where(Gap.severity == severity)
    if nvidia_hw:
        stmt = stmt.where(Gap.nvidia_hw == nvidia_hw)
    if amd_hw:
        stmt = stmt.where(Gap.amd_hw == amd_hw)
    if category:
        stmt = stmt.join(Project, Project.id == Gap.project_id).where(
            Project.category == category
        )
    res = await db.execute(stmt)
    gaps = list(res.scalars())
    gaps.sort(
        key=lambda g: (
            _severity_rank(g.severity),
            -g.created_at.timestamp(),
        )
    )
    return [_gap_item(g) for g in gaps]


@router.get("/summary")
async def gaps_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> DashboardSummary:
    proj_res = await db.execute(select(Project).where(Project.status == "active"))
    projects = list(proj_res.scalars())
    total_projects = len(projects)
    latest_ids = await _latest_report_ids(db)
    if not latest_ids:
        return DashboardSummary(
            total_projects=total_projects,
            feature_gaps=0,
            perf_gaps=0,
            model_gaps_nvidia_only=0,
            avg_score=0.0,
            score_change_month=0.0,
        )
    gaps_res = await db.execute(select(Gap).where(Gap.report_id.in_(latest_ids)))
    gaps = list(gaps_res.scalars())
    feature_gaps = sum(1 for g in gaps if g.gap_type == "feature")
    perf_gaps = sum(1 for g in gaps if g.gap_type == "performance")
    model_gaps_nvidia_only = sum(1 for g in gaps if g.gap_type == "model")
    reports_res = await db.execute(select(GapReport).order_by(GapReport.id.asc()))
    all_reports = list(reports_res.scalars())
    latest_by_project: dict[str, GapReport] = {}
    for r in all_reports:
        latest_by_project[r.project_id] = r
    scores = [float(r.overall_score) for r in latest_by_project.values()]
    avg_score = sum(scores) / len(scores) if scores else 0.0
    now = all_reports[-1].created_at if all_reports else None
    score_change_month = 0.0
    if now is not None and scores:
        from datetime import timedelta

        cutoff = now - timedelta(days=30)
        old_scores: list[float] = []
        grouped: dict[str, list[GapReport]] = {}
        for r in all_reports:
            grouped.setdefault(r.project_id, []).append(r)
        for pid, lst in grouped.items():
            old = [r for r in lst if r.created_at <= cutoff]
            if old:
                old_scores.append(float(old[-1].overall_score))
        if old_scores:
            prev_avg = sum(old_scores) / len(old_scores)
            score_change_month = avg_score - prev_avg
    return DashboardSummary(
        total_projects=total_projects,
        feature_gaps=feature_gaps,
        perf_gaps=perf_gaps,
        model_gaps_nvidia_only=model_gaps_nvidia_only,
        avg_score=avg_score,
        score_change_month=score_change_month,
    )
