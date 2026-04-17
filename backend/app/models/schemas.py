from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


HW_GENERATIONS = {
    "A100": "MI250X",
    "H100": "MI300X",
    "H200": "MI325X",
    "B200": "MI355X",
    "B300": "MI400X",
}


class ProjectBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    repo: str
    category: str
    project_type: str
    description: str
    priority: str
    amd_repo: Optional[str] = None
    nv_name: Optional[str] = None
    amd_name: Optional[str] = None
    status: str


class ProjectResponse(ProjectBase):
    created_at: datetime
    updated_at: datetime
    latest_score: Optional[float] = None
    score_change: Optional[float] = None


class DimensionScores(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    model_support: Optional[float] = None
    feature_availability: Optional[float] = None
    performance_parity: Optional[float] = None
    kernel_backend: Optional[float] = None
    engineering_maturity: Optional[float] = None
    overall: Optional[float] = None


class GapItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    gap_type: str
    name: str
    description: str
    severity: str
    nvidia_status: str
    amd_status: str
    nvidia_hw: Optional[str] = None
    amd_hw: Optional[str] = None
    hw_generation_match: bool
    blocker: Optional[str] = None
    evidence_url: Optional[str] = None
    evidence_type: Optional[str] = None
    confidence: float
    is_resolved: bool
    created_at: datetime


class GapReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    project_id: str
    created_at: datetime
    scores: DimensionScores
    summary: str
    gap_count: int
    gaps: list[GapItem]


class ProjectDetailResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project: ProjectResponse
    scores: DimensionScores
    gaps: list[GapItem]
    recent_changes: list[Any]
    benchmarks: list[Any]


class TrendItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project_id: str
    project_name: str
    current_score: float
    previous_score: float
    change: float


class ChangeItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    type: str
    title: str
    description: str
    project_id: str
    date: datetime
    evidence_url: Optional[str] = None


class MatrixRow(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    project: ProjectResponse
    scores: DimensionScores


class DashboardSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_projects: int
    feature_gaps: int
    perf_gaps: int
    model_gaps_nvidia_only: int
    avg_score: float
    score_change_month: float


class AgentTriggerRequest(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    run_type: str
    project_id: Optional[str] = None


class AgentStatusResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    is_running: bool
    current_task: Optional[str] = None
    last_run_at: Optional[datetime] = None
    projects_analyzed: int
