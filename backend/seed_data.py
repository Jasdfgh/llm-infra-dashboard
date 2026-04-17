"""Migrate existing repo_registry.json and analysis data into the new SQLite database."""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from app.models.database import init_db, async_session_maker, Project, GapReport, Gap


DATA_DIR = Path(__file__).parent.parent / "data"


async def seed():
    await init_db()

    with open(DATA_DIR / "repo_registry.json") as f:
        registry = json.load(f)

    async with async_session_maker() as session:
        for p in registry["projects"]:
            project = Project(
                id=p["id"],
                name=p["id"].replace("_", " ").title(),
                repo=p["nv_repo"],
                category=p["category"],
                project_type=p["type"],
                description=p.get("description", ""),
                priority=p.get("priority", 3),
                amd_repo=p.get("amd_repo"),
                nv_name=p.get("nv_name"),
                amd_name=p.get("amd_name"),
                status="active",
                discovered_by="manual",
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
            session.add(project)

            analysis_path = DATA_DIR / "analysis" / f"{p['id']}.json"
            if analysis_path.exists():
                with open(analysis_path) as f:
                    analysis = json.load(f)

                scores = _extract_scores(analysis, p["type"])
                report = GapReport(
                    project_id=p["id"],
                    created_at=datetime.now(timezone.utc),
                    overall_score=scores["overall"],
                    model_support_score=scores["model_support"],
                    feature_availability_score=scores["feature_availability"],
                    performance_parity_score=scores.get("performance_parity"),
                    kernel_backend_score=scores.get("kernel_backend"),
                    engineering_maturity_score=scores.get("engineering_maturity"),
                    summary=analysis.get("summary", ""),
                    raw_data=json.dumps(analysis),
                )
                session.add(report)
                await session.flush()

                gaps = _extract_gaps(analysis, p["id"], report.id, p["type"])
                for g in gaps:
                    session.add(g)

        await session.commit()
        print(f"Seeded {len(registry['projects'])} projects with analysis data.")


SUPPORT_SCORE_MAP = {
    "first-class": 95,
    "experimental": 60,
    "community": 35,
    "none": 5,
}


def _extract_scores(analysis: dict, project_type: str) -> dict:
    if project_type == "dual_platform":
        features = analysis.get("features", [])
        if not features:
            level = analysis.get("rocm_support_level", "none")
            overall = SUPPORT_SCORE_MAP.get(level, 50) / 100
            return {"overall": overall, "model_support": None, "feature_availability": overall, "performance_parity": None, "kernel_backend": None, "engineering_maturity": None}

        amd_scores = []
        for f in features:
            amd = f.get("amd_support", "none")
            amd_scores.append(SUPPORT_SCORE_MAP.get(amd, 50))

        feature_score = sum(amd_scores) / len(amd_scores) / 100 if amd_scores else 0.5
        level = analysis.get("rocm_support_level", "none")
        overall = SUPPORT_SCORE_MAP.get(level, 50) / 100

        return {
            "overall": round(max(overall, feature_score), 2),
            "model_support": round(feature_score + 0.05, 2) if feature_score > 0.5 else None,
            "feature_availability": round(feature_score, 2),
            "performance_parity": None,
            "kernel_backend": None,
            "engineering_maturity": round(overall, 2),
        }
    else:
        comparison = analysis.get("comparison", {})
        shared = comparison.get("shared_features", [])
        nv_only = comparison.get("nv_only_features", [])
        amd_only = comparison.get("amd_only_features", [])

        total_features = len(shared) + len(nv_only) + len(amd_only)
        if total_features == 0:
            return {"overall": 0.5, "model_support": None, "feature_availability": 0.5, "performance_parity": None, "kernel_backend": None, "engineering_maturity": None}

        amd_scores = []
        for f in shared:
            amd = f.get("amd_support", "none")
            amd_scores.append(SUPPORT_SCORE_MAP.get(amd, 50))
        for _ in nv_only:
            amd_scores.append(5)
        for _ in amd_only:
            amd_scores.append(95)

        feature_score = sum(amd_scores) / len(amd_scores) / 100 if amd_scores else 0.5
        coverage = (len(shared) + len(amd_only)) / total_features if total_features > 0 else 0.5

        return {
            "overall": round(feature_score, 2),
            "model_support": None,
            "feature_availability": round(coverage, 2),
            "performance_parity": None,
            "kernel_backend": round(feature_score, 2),
            "engineering_maturity": None,
        }


def _extract_gaps(analysis: dict, project_id: str, report_id: int, project_type: str) -> list[Gap]:
    gaps = []
    now = datetime.now(timezone.utc)

    if project_type == "dual_platform":
        for f in analysis.get("features", []):
            nv = f.get("nvidia_support", "none")
            amd = f.get("amd_support", "none")
            nv_score = SUPPORT_SCORE_MAP.get(nv, 50)
            amd_score = SUPPORT_SCORE_MAP.get(amd, 50)
            if nv_score <= amd_score:
                continue

            diff = nv_score - amd_score
            severity = "low"
            if diff >= 60:
                severity = "critical"
            elif diff >= 30:
                severity = "high"
            elif diff >= 15:
                severity = "medium"

            gaps.append(Gap(
                report_id=report_id,
                project_id=project_id,
                gap_type="feature",
                name=f.get("name", "Unknown"),
                description=f.get("description", ""),
                severity=severity,
                nvidia_status=nv,
                amd_status=amd,
                nvidia_hw="H100",
                amd_hw="MI300X",
                hw_generation_match=True,
                blocker="; ".join(f.get("blockers", [])) or None,
                confidence="medium",
                created_at=now,
            ))

        for b in analysis.get("key_blockers", []):
            gaps.append(Gap(
                report_id=report_id,
                project_id=project_id,
                gap_type="feature",
                name=b.get("description", "Blocker")[:100],
                description=b.get("description", ""),
                severity="high" if b.get("impact") == "high" else "medium",
                nvidia_status="first-class",
                amd_status="none",
                nvidia_hw="H100",
                amd_hw="MI300X",
                hw_generation_match=True,
                blocker=b.get("workaround"),
                confidence="medium",
                created_at=now,
            ))
    else:
        comparison = analysis.get("comparison", {})
        for f in comparison.get("shared_features", []):
            nv = f.get("nvidia_support", "none")
            amd = f.get("amd_support", "none")
            nv_score = SUPPORT_SCORE_MAP.get(nv, 50)
            amd_score = SUPPORT_SCORE_MAP.get(amd, 50)
            if nv_score <= amd_score:
                continue

            diff = nv_score - amd_score
            severity = "medium" if diff < 30 else "high"

            gaps.append(Gap(
                report_id=report_id,
                project_id=project_id,
                gap_type="kernel",
                name=f.get("name", "Unknown"),
                description=f.get("description", ""),
                severity=severity,
                nvidia_status=nv,
                amd_status=amd,
                nvidia_hw="H100",
                amd_hw="MI300X",
                hw_generation_match=True,
                blocker="; ".join(f.get("blockers", [])) or None,
                confidence="medium",
                created_at=now,
            ))

        for f in comparison.get("nv_only_features", []):
            gaps.append(Gap(
                report_id=report_id,
                project_id=project_id,
                gap_type="kernel",
                name=f.get("name", "Unknown"),
                description=f.get("description", ""),
                severity="high",
                nvidia_status="first-class",
                amd_status="none",
                nvidia_hw="H100",
                amd_hw="MI300X",
                hw_generation_match=True,
                blocker="NVIDIA-only feature",
                confidence="medium",
                created_at=now,
            ))

    return gaps


if __name__ == "__main__":
    asyncio.run(seed())
