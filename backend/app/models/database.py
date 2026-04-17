from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_DATABASE_FILE = (_BACKEND_DIR / ".." / "data" / "dashboard.db").resolve()
DATABASE_URL = f"sqlite+aiosqlite:///{_DATABASE_FILE.as_posix()}"

engine = create_async_engine(DATABASE_URL, echo=False)
async_session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    repo: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    project_type: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(String)
    priority: Mapped[int] = mapped_column(Integer, default=3)
    amd_repo: Mapped[str | None] = mapped_column(String, nullable=True)
    nv_name: Mapped[str | None] = mapped_column(String, nullable=True)
    amd_name: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="active")
    discovered_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String)
    error_message: Mapped[str | None] = mapped_column(String, nullable=True)
    github_api_calls: Mapped[int] = mapped_column(Integer, default=0)
    llm_api_calls: Mapped[int] = mapped_column(Integer, default=0)


class GapReport(Base):
    __tablename__ = "gap_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    crawl_run_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("crawl_runs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    overall_score: Mapped[float] = mapped_column(Float)
    model_support_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    feature_availability_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    performance_parity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    kernel_backend_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    engineering_maturity_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    summary: Mapped[str] = mapped_column(Text)
    raw_data: Mapped[str] = mapped_column(Text)


class Gap(Base):
    __tablename__ = "gaps"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    report_id: Mapped[int] = mapped_column(Integer, ForeignKey("gap_reports.id"))
    project_id: Mapped[str] = mapped_column(String, ForeignKey("projects.id"))
    gap_type: Mapped[str] = mapped_column(String)
    name: Mapped[str] = mapped_column(String)
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String)
    nvidia_status: Mapped[str] = mapped_column(String)
    amd_status: Mapped[str] = mapped_column(String)
    nvidia_hw: Mapped[str | None] = mapped_column(String, nullable=True)
    amd_hw: Mapped[str | None] = mapped_column(String, nullable=True)
    hw_generation_match: Mapped[bool] = mapped_column(Boolean, default=True)
    blocker: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_url: Mapped[str | None] = mapped_column(String, nullable=True)
    evidence_type: Mapped[str | None] = mapped_column(String, nullable=True)
    confidence: Mapped[str] = mapped_column(String, default="medium")
    is_resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_evidence_url: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class AgentLog(Base):
    __tablename__ = "agent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_type: Mapped[str] = mapped_column(String)
    project_id: Mapped[str | None] = mapped_column(String, ForeignKey("projects.id"), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    level: Mapped[str] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
