from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.crawler import CrawlAgent
from app.agent.github_client import GitHubClient
from app.agent.llm_client import LLMClient
from app.models.database import (
    AgentLog,
    CrawlRun,
    Gap,
    GapReport,
    Project,
    async_session_maker,
    get_db,
)
from app.models.schemas import AgentStatusResponse, AgentTriggerRequest

logger = logging.getLogger(__name__)
router = APIRouter()

_agent_lock = asyncio.Lock()
_is_running = False
_current_task: str | None = None
_last_run_at: datetime | None = None
_projects_analyzed = 0


def _nested_score(val: Any) -> float | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, dict):
        s = val.get("score")
        if isinstance(s, (int, float)):
            return float(s)
    return None


def _project_slug(repo: str) -> str:
    base = re.sub(r"[^a-zA-Z0-9_-]+", "_", repo.replace("/", "_"))[:72]
    return base or "project"


async def _log(
    session: AsyncSession,
    *,
    run_type: str,
    project_id: str | None,
    message: str,
    level: str = "info",
) -> None:
    session.add(
        AgentLog(
            run_type=run_type,
            project_id=project_id,
            message=message,
            level=level,
        )
    )
    await session.commit()


async def _persist_analysis(
    session: AsyncSession,
    *,
    project: Project,
    crawl_run_id: int,
    analysis: dict[str, Any],
    github_calls: int,
    llm_calls: int,
) -> None:
    overall = float(analysis.get("overall_score") or 0.0)
    ms = _nested_score(analysis.get("model_support"))
    fa = _nested_score(analysis.get("feature_availability"))
    pp = _nested_score(analysis.get("performance_parity"))
    kb = _nested_score(analysis.get("kernel_backend"))
    em = _nested_score(analysis.get("engineering_maturity"))
    summary = str(analysis.get("summary") or "")
    raw = json.dumps(analysis, ensure_ascii=False, default=str)
    report = GapReport(
        project_id=project.id,
        crawl_run_id=crawl_run_id,
        overall_score=overall,
        model_support_score=ms,
        feature_availability_score=fa,
        performance_parity_score=pp,
        kernel_backend_score=kb,
        engineering_maturity_score=em,
        summary=summary,
        raw_data=raw,
    )
    session.add(report)
    await session.flush()
    for item in analysis.get("gaps") or []:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description") or item.get("name") or "")
        session.add(
            Gap(
                report_id=report.id,
                project_id=project.id,
                gap_type=str(item.get("type") or "feature"),
                name=str(item.get("name") or "gap"),
                description=desc,
                severity=str(item.get("severity") or "medium"),
                nvidia_status=str(item.get("nvidia_status") or ""),
                amd_status=str(item.get("amd_status") or ""),
                nvidia_hw=item.get("nvidia_hw"),
                amd_hw=item.get("amd_hw"),
                hw_generation_match=bool(item.get("hw_generation_match", True)),
                blocker=item.get("blocker"),
                evidence_url=item.get("evidence_url"),
                evidence_type=item.get("evidence_type"),
                confidence=str(item.get("confidence") or "medium"),
            )
        )
    cr = await session.get(CrawlRun, crawl_run_id)
    if cr:
        cr.status = "completed"
        cr.completed_at = datetime.now(timezone.utc)
        cr.github_api_calls = github_calls
        cr.llm_api_calls = llm_calls


async def run_agent_task(req: AgentTriggerRequest) -> None:
    global _is_running, _current_task, _last_run_at, _projects_analyzed
    _current_task = f"{req.run_type}"
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_KEY") or ""
    github = GitHubClient(token)
    llm = LLMClient()
    crawler = CrawlAgent(github)
    analyzed = 0
    try:
        if req.run_type == "crawl":
            async with async_session_maker() as session:
                stmt = select(Project).where(Project.status == "active")
                if req.project_id:
                    stmt = stmt.where(Project.id == req.project_id)
                targets = list((await session.execute(stmt)).scalars())
                await _log(
                    session,
                    run_type="crawl",
                    project_id=req.project_id,
                    message=f"crawl started for {len(targets)} project(s)",
                )
            for project in targets:
                _current_task = f"crawl:{project.id}"
                async with async_session_maker() as session:
                    run = CrawlRun(
                        project_id=project.id,
                        status="running",
                        github_api_calls=0,
                        llm_api_calls=0,
                    )
                    session.add(run)
                    await session.commit()
                    await session.refresh(run)
                    run_id = run.id
                try:
                    crawl_data = await crawler.crawl_project(
                        project_id=project.id,
                        repo=project.repo,
                        project_type=project.project_type,
                        amd_repo=project.amd_repo,
                    )
                    analysis = await llm.analyze_project(
                        project.id, project.name, crawl_data,
                        category=project.category,
                    )
                except Exception as exc:
                    logger.exception("crawl pipeline failed for %s", project.id)
                    async with async_session_maker() as session:
                        cr = await session.get(CrawlRun, run_id)
                        if cr:
                            cr.status = "failed"
                            cr.completed_at = datetime.now(timezone.utc)
                            cr.error_message = str(exc)
                            await session.commit()
                    async with async_session_maker() as session:
                        await _log(
                            session,
                            run_type="crawl",
                            project_id=project.id,
                            message=str(exc),
                            level="error",
                        )
                    continue
                async with async_session_maker() as session:
                    proj = await session.get(Project, project.id)
                    if proj is None:
                        continue
                    await _persist_analysis(
                        session,
                        project=proj,
                        crawl_run_id=run_id,
                        analysis=analysis,
                        github_calls=github.api_call_count,
                        llm_calls=1,
                    )
                    await session.commit()
                analyzed += 1
                async with async_session_maker() as session:
                    await _log(
                        session,
                        run_type="crawl",
                        project_id=project.id,
                        message=f"completed analysis for {project.id}",
                    )
        elif req.run_type == "discovery":
            _current_task = "discovery"
            async with async_session_maker() as session:
                await _log(
                    session,
                    run_type="discovery",
                    project_id=None,
                    message="discovery started",
                )
                res = await session.execute(select(Project.repo))
                existing = [row[0] for row in res.all()]
            candidates = await llm.discover_projects(existing)
            created = 0
            async with async_session_maker() as session:
                res = await session.execute(select(Project.repo))
                existing = [row[0] for row in res.all()]
                for c in candidates:
                    repo = str(c.get("repo", "")).strip()
                    if not repo or repo in existing:
                        continue
                    pid = _project_slug(repo)
                    if await session.get(Project, pid):
                        suffix = 1
                        while await session.get(Project, f"{pid}_{suffix}"):
                            suffix += 1
                        pid = f"{pid}_{suffix}"
                    session.add(
                        Project(
                            id=pid,
                            name=str(c.get("name") or repo),
                            repo=repo,
                            category=str(c.get("category") or "tool"),
                            project_type="unknown",
                            description=str(c.get("description") or ""),
                            priority=3,
                            status="candidate",
                            discovered_by="agent_discovery",
                        )
                    )
                    existing.append(repo)
                    created += 1
                await session.commit()
                await _log(
                    session,
                    run_type="discovery",
                    project_id=None,
                    message=f"discovery finished, {created} candidate(s) saved",
                )
        else:
            async with async_session_maker() as session:
                await _log(
                    session,
                    run_type=req.run_type,
                    project_id=req.project_id,
                    message=f"unknown run_type {req.run_type}",
                    level="error",
                )
    except Exception:
        logger.exception("agent task failed")
        async with async_session_maker() as session:
            await _log(
                session,
                run_type=req.run_type,
                project_id=req.project_id,
                message="agent task crashed",
                level="error",
            )
    finally:
        await github.aclose()
        _projects_analyzed = analyzed
        _last_run_at = datetime.now(timezone.utc)
        _current_task = None
        _is_running = False


@router.post("/trigger")
async def trigger_agent(
    body: AgentTriggerRequest,
) -> dict[str, str]:
    global _is_running
    if body.run_type not in {"crawl", "discovery"}:
        raise HTTPException(status_code=400, detail="invalid run_type")
    async with _agent_lock:
        if _is_running:
            raise HTTPException(status_code=409, detail="agent already running")
        _is_running = True
    msg = (
        f"{body.run_type} run scheduled"
        + (f" for {body.project_id}" if body.project_id else "")
    )
    asyncio.create_task(run_agent_task(body))
    return {"status": "started", "message": msg}


@router.get("/status")
async def agent_status() -> AgentStatusResponse:
    return AgentStatusResponse(
        is_running=_is_running,
        current_task=_current_task,
        last_run_at=_last_run_at,
        projects_analyzed=_projects_analyzed,
    )


@router.get("/logs")
async def agent_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[dict[str, Any]]:
    res = await db.execute(
        select(AgentLog).order_by(AgentLog.id.desc()).limit(50)
    )
    rows = list(res.scalars())
    rows.reverse()
    return [
        {
            "id": r.id,
            "run_type": r.run_type,
            "project_id": r.project_id,
            "message": r.message,
            "level": r.level,
            "created_at": r.created_at,
        }
        for r in rows
    ]
