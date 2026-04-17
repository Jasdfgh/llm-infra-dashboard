from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Gap, GapReport, Project, get_db
from app.models.schemas import ChangeItem, TrendItem

router = APIRouter()


def _group_reports(reports: list[GapReport]) -> dict[str, list[GapReport]]:
    grouped: dict[str, list[GapReport]] = {}
    for r in sorted(reports, key=lambda x: x.id):
        grouped.setdefault(r.project_id, []).append(r)
    return grouped


async def _gaps_for_report(session: AsyncSession, report_id: int) -> list[Gap]:
    res = await session.execute(select(Gap).where(Gap.report_id == report_id))
    return list(res.scalars())


@router.get("/")
async def list_trends(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[TrendItem]:
    proj_res = await db.execute(
        select(Project).where(Project.status == "active").order_by(Project.name.asc())
    )
    projects = {p.id: p for p in proj_res.scalars()}
    rep_res = await db.execute(select(GapReport).order_by(GapReport.id.asc()))
    grouped = _group_reports(list(rep_res.scalars()))
    items: list[TrendItem] = []
    for pid, p in projects.items():
        lst = grouped.get(pid, [])
        if not lst:
            continue
        current = lst[-1]
        previous = lst[-2] if len(lst) >= 2 else current
        cur_score = float(current.overall_score)
        prev_score = float(previous.overall_score)
        items.append(
            TrendItem(
                project_id=p.id,
                project_name=p.name,
                current_score=cur_score,
                previous_score=prev_score,
                change=cur_score - prev_score,
            )
        )
    items.sort(key=lambda t: t.current_score, reverse=True)
    return items


@router.get("/changes")
async def recent_changes(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[ChangeItem]:
    rep_res = await db.execute(select(GapReport).order_by(GapReport.id.asc()))
    reports = list(rep_res.scalars())
    grouped = _group_reports(reports)
    out: list[ChangeItem] = []
    for pid, lst in grouped.items():
        if len(lst) < 2:
            continue
        latest = lst[-1]
        previous = lst[-2]
        latest_gaps = await _gaps_for_report(db, latest.id)
        prev_gaps = await _gaps_for_report(db, previous.id)
        prev_map: dict[tuple[str, str], Gap] = {(g.gap_type, g.name): g for g in prev_gaps}
        latest_map: dict[tuple[str, str], Gap] = {(g.gap_type, g.name): g for g in latest_gaps}
        for key, g in latest_map.items():
            if key not in prev_map:
                out.append(
                    ChangeItem(
                        type="new",
                        title=g.name,
                        description=g.description[:800] if g.description else g.name,
                        project_id=g.project_id,
                        date=latest.created_at,
                        evidence_url=g.evidence_url,
                    )
                )
        for key, g in prev_map.items():
            if key not in latest_map:
                out.append(
                    ChangeItem(
                        type="resolved",
                        title=g.name,
                        description="Gap no longer present in latest report",
                        project_id=g.project_id,
                        date=latest.created_at,
                        evidence_url=g.resolved_evidence_url or g.evidence_url,
                    )
                )
    out.sort(key=lambda c: c.date.timestamp(), reverse=True)
    return out
