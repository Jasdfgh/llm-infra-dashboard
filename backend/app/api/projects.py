from __future__ import annotations

import json
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Gap, GapReport, Project, get_db
from app.models.schemas import (
    ChangeItem,
    DimensionScores,
    GapItem,
    MatrixRow,
    ProjectDetailResponse,
    ProjectResponse,
)

router = APIRouter()


def _confidence_float(raw: str) -> float:
    m = {"high": 0.85, "medium": 0.55, "low": 0.25}
    return m.get(str(raw).lower(), 0.5)


def _scores_from_report(r: GapReport | None) -> DimensionScores:
    if r is None:
        return DimensionScores()
    return DimensionScores(
        model_support=r.model_support_score,
        feature_availability=r.feature_availability_score,
        performance_parity=r.performance_parity_score,
        kernel_backend=r.kernel_backend_score,
        engineering_maturity=r.engineering_maturity_score,
        overall=r.overall_score,
    )


def _project_response(
    p: Project,
    *,
    latest_score: float | None = None,
    score_change: float | None = None,
) -> ProjectResponse:
    return ProjectResponse(
        id=p.id,
        name=p.name,
        repo=p.repo,
        category=p.category,
        project_type=p.project_type,
        description=p.description,
        priority=str(p.priority),
        amd_repo=p.amd_repo,
        nv_name=p.nv_name,
        amd_name=p.amd_name,
        status=p.status,
        created_at=p.created_at,
        updated_at=p.updated_at,
        latest_score=latest_score,
        score_change=score_change,
    )


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


async def _all_reports(session: AsyncSession) -> list[GapReport]:
    res = await session.execute(select(GapReport).order_by(GapReport.id.asc()))
    return list(res.scalars())


def _latest_by_project(reports: list[GapReport]) -> dict[str, GapReport]:
    out: dict[str, GapReport] = {}
    for r in reports:
        out[r.project_id] = r
    return out


def _second_latest_by_project(reports: list[GapReport]) -> dict[str, GapReport]:
    grouped: dict[str, list[GapReport]] = {}
    for r in sorted(reports, key=lambda x: x.id):
        grouped.setdefault(r.project_id, []).append(r)
    out: dict[str, GapReport] = {}
    for pid, lst in grouped.items():
        if len(lst) >= 2:
            out[pid] = lst[-2]
    return out


async def _gaps_for_report(session: AsyncSession, report_id: int) -> list[Gap]:
    res = await session.execute(select(Gap).where(Gap.report_id == report_id))
    return list(res.scalars())


def _benchmarks_from_raw(raw: str) -> list[Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    perf = data.get("performance_parity")
    if isinstance(perf, dict):
        b = perf.get("benchmarks")
        if isinstance(b, list):
            return b
    return []


def _recent_changes_for_project(
    latest: GapReport | None,
    previous: GapReport | None,
    latest_gaps: list[Gap],
    prev_gaps: list[Gap],
) -> list[ChangeItem]:
    if latest is None or previous is None:
        return []
    prev_map = {(g.gap_type, g.name): g for g in prev_gaps}
    latest_map = {(g.gap_type, g.name): g for g in latest_gaps}
    items: list[ChangeItem] = []
    for key, g in latest_map.items():
        if key not in prev_map:
            items.append(
                ChangeItem(
                    type="new",
                    title=g.name,
                    description=g.description[:500] if g.description else g.name,
                    project_id=g.project_id,
                    date=latest.created_at,
                    evidence_url=g.evidence_url,
                )
            )
    for key, g in prev_map.items():
        if key not in latest_map and not g.is_resolved:
            items.append(
                ChangeItem(
                    type="resolved",
                    title=g.name,
                    description=f"Previously tracked gap no longer present in latest report: {g.name}",
                    project_id=g.project_id,
                    date=latest.created_at,
                    evidence_url=g.evidence_url,
                )
            )
    return items


@router.get("/")
async def list_projects(
    db: Annotated[AsyncSession, Depends(get_db)],
    category: Optional[str] = None,
    sort_by: Optional[str] = Query(default="overall_score"),
) -> list[MatrixRow]:
    proj_res = await db.execute(select(Project))
    projects = list(proj_res.scalars())
    if category:
        projects = [p for p in projects if p.category == category]
    reports = await _all_reports(db)
    latest = _latest_by_project(reports)
    second = _second_latest_by_project(reports)
    rows: list[MatrixRow] = []
    for p in projects:
        lr = latest.get(p.id)
        pr = second.get(p.id)
        score_change = None
        if lr is not None and pr is not None:
            score_change = float(lr.overall_score) - float(pr.overall_score)
        rows.append(
            MatrixRow(
                project=_project_response(
                    p,
                    latest_score=float(lr.overall_score) if lr else None,
                    score_change=score_change,
                ),
                scores=_scores_from_report(lr),
            )
        )
    if sort_by == "name":
        rows.sort(key=lambda r: r.project.name.lower())
    else:
        rows.sort(
            key=lambda r: (
                r.scores.overall is not None,
                r.scores.overall or -1.0,
            ),
            reverse=True,
        )
    return rows


@router.get("/{project_id}")
async def get_project(
    project_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProjectDetailResponse:
    p = await db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    reports = [r for r in await _all_reports(db) if r.project_id == project_id]
    if not reports:
        return ProjectDetailResponse(
            project=_project_response(p),
            scores=DimensionScores(),
            gaps=[],
            recent_changes=[],
            benchmarks=[],
        )
    reports.sort(key=lambda r: r.id)
    latest = reports[-1]
    previous = reports[-2] if len(reports) >= 2 else None
    latest_gaps = await _gaps_for_report(db, latest.id)
    prev_gaps = await _gaps_for_report(db, previous.id) if previous else []
    score_change = None
    if previous is not None:
        score_change = float(latest.overall_score) - float(previous.overall_score)
    recent = _recent_changes_for_project(latest, previous, latest_gaps, prev_gaps)
    return ProjectDetailResponse(
        project=_project_response(
            p,
            latest_score=float(latest.overall_score),
            score_change=score_change,
        ),
        scores=_scores_from_report(latest),
        gaps=[_gap_item(g) for g in latest_gaps],
        recent_changes=recent,
        benchmarks=_benchmarks_from_raw(latest.raw_data),
    )


@router.get("/{project_id}/history")
async def project_history(
    project_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    p = await db.get(Project, project_id)
    if p is None:
        raise HTTPException(status_code=404, detail="project not found")
    res = await db.execute(
        select(GapReport)
        .where(GapReport.project_id == project_id)
        .order_by(GapReport.id.asc())
    )
    reports = list(res.scalars())
    out: list[dict[str, Any]] = []
    for r in reports:
        out.append(
            {
                "date": r.created_at,
                "overall_score": float(r.overall_score),
                "dimension_scores": {
                    "model_support": r.model_support_score,
                    "feature_availability": r.feature_availability_score,
                    "performance_parity": r.performance_parity_score,
                    "kernel_backend": r.kernel_backend_score,
                    "engineering_maturity": r.engineering_maturity_score,
                    "overall": float(r.overall_score),
                },
            }
        )
    return out
