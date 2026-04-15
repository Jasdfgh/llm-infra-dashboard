from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class ProjectType(str, Enum):
    DUAL_PLATFORM = "dual_platform"
    NV_AMD_PAIR = "nv_amd_pair"


class Category(str, Enum):
    INFERENCE = "inference"
    TRAINING = "training"
    TOOL = "tool"


class SupportLevel(str, Enum):
    FIRST_CLASS = "first-class"
    EXPERIMENTAL = "experimental"
    COMMUNITY = "community"
    NONE = "none"


class Impact(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


SUPPORT_LEVEL_COLORS = {
    SupportLevel.FIRST_CLASS: "#22c55e",
    SupportLevel.EXPERIMENTAL: "#f59e0b",
    SupportLevel.COMMUNITY: "#f97316",
    SupportLevel.NONE: "#ef4444",
}

SUPPORT_LEVEL_LABELS = {
    SupportLevel.FIRST_CLASS: "First-class",
    SupportLevel.EXPERIMENTAL: "Experimental",
    SupportLevel.COMMUNITY: "Community",
    SupportLevel.NONE: "None",
}

CATEGORY_LABELS = {
    Category.INFERENCE: "Inference",
    Category.TRAINING: "Training",
    Category.TOOL: "Tool",
}


@dataclass
class ProjectEntry:
    id: str
    type: ProjectType
    category: Category
    description: str
    nv_repo: str
    amd_repo: Optional[str] = None
    nv_name: Optional[str] = None
    amd_name: Optional[str] = None
    priority: int = 3


@dataclass
class FeatureComparison:
    name: str
    nvidia_support: SupportLevel
    amd_support: SupportLevel
    description: str = ""
    blockers: list[str] = field(default_factory=list)
    priority: int = 3


@dataclass
class NVFeature:
    name: str
    description: str
    nv_only: bool = False


@dataclass
class AMDFeature:
    name: str
    description: str
    amd_only: bool = False


@dataclass
class PairComparison:
    nv_name: str
    amd_name: str
    nv_repo: str
    amd_repo: str
    shared_features: list[FeatureComparison] = field(default_factory=list)
    nv_only_features: list[NVFeature] = field(default_factory=list)
    amd_only_features: list[AMDFeature] = field(default_factory=list)


@dataclass
class Blocker:
    description: str
    impact: Impact
    workaround: str = ""


@dataclass
class DualPlatformAnalysis:
    project_id: str
    repo: str
    rocm_support_level: SupportLevel
    features: list[FeatureComparison] = field(default_factory=list)
    key_blockers: list[Blocker] = field(default_factory=list)
    summary: str = ""
    analyzed_at: str = ""


@dataclass
class NVAMDPairAnalysis:
    project_id: str
    nv_repo: str
    amd_repo: str
    nv_name: str
    amd_name: str
    comparison: PairComparison = field(default_factory=PairComparison)
    summary: str = ""
    analyzed_at: str = ""


@dataclass
class GitHubMetrics:
    stars: int = 0
    forks: int = 0
    open_issues: int = 0
    last_commit_date: Optional[str] = None
    rocm_mentions_in_readme: int = 0
    rocm_keywords_in_readme: list[str] = field(default_factory=list)
    rocm_issues_open: int = 0
    rocm_issues_closed: int = 0
    rocm_prs_open: int = 0
    rocm_prs_merged: int = 0
    has_rocm_ci: bool = False
    has_rocm_dockerfile: bool = False
    readme_excerpt: str = ""
    fetched_at: str = ""
