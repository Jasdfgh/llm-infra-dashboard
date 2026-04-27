#!/usr/bin/env python3
"""Generate pre-filled fixture database and JSON cache for downstream development.

Usage:
    .venv/bin/python scripts/generate_fixtures.py

Output:
    data/fixtures/signals_fixture.db  — SQLite with ~22 signals, comments, changes
    data/fixtures/cache/              — JSON cache files matching DB content
"""

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.ingestion.models import (
    ChangeEvent,
    ChangeType,
    Comment,
    RawSignal,
    SourceType,
    estimate_tokens,
)
from src.ingestion.normalizer import Normalizer
from src.storage.cache import JSONCache
from src.storage.database import init_db
from src.storage.repository import SignalRepository

FIXTURE_DB = ROOT / "data" / "fixtures" / "signals_fixture.db"
FIXTURE_CACHE = ROOT / "data" / "fixtures" / "cache"
DEMO_DIR = ROOT / "demo" / "signals"
SYNC_RUN_ID = "fixture_gen_001"
SYNCED_AT = "2026-04-22T06:03:00Z"
ISSUE_SID = "github:vllm-project/vllm:issue:39303"


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------


def _clean():
    for suffix in ("", "-wal", "-shm"):
        p = FIXTURE_DB.parent / (FIXTURE_DB.name + suffix)
        if p.exists():
            p.unlink()
    if FIXTURE_CACHE.exists():
        shutil.rmtree(FIXTURE_CACHE)


# ---------------------------------------------------------------------------
# Demo JSON → RawSignal converters
# ---------------------------------------------------------------------------


def _load_json(name: str) -> dict:
    return json.loads((DEMO_DIR / name).read_text(encoding="utf-8"))


def _demo_issue_to_raw(data: dict, repo: str) -> RawSignal:
    raw_data = {
        "number": data["number"],
        "title": data["title"],
        "body": data.get("body_text", ""),
        "user": {"login": data["author"]},
        "state": data["state"],
        "labels": [{"name": l} for l in data.get("labels", [])],
        "assignees": [],
        "comments": data.get("comment_count", 0),
        "created_at": data["created_at"],
        "updated_at": data.get("updated_at", data["created_at"]),
        "closed_at": data.get("closed_at"),
        "closed_by": (
            {"login": data["closed_by"]} if data.get("closed_by") else None
        ),
        "html_url": data["html_url"],
        "repository_url": f"https://api.github.com/repos/{repo}",
    }
    return RawSignal(
        raw_id=str(data["number"]),
        source_type=SourceType.GITHUB_ISSUE,
        raw_data=raw_data,
    )


def _demo_pr_to_raw(data: dict, repo: str) -> RawSignal:
    cf = data.get("changed_files", {})
    raw_data = {
        "number": data["number"],
        "title": data["title"],
        "body": data.get("body_text", ""),
        "user": {"login": data["author"]},
        "state": data["state"],
        "labels": [{"name": l} for l in data.get("labels", [])],
        "assignees": [],
        "comments": 0,
        "created_at": data["created_at"],
        "updated_at": data.get("merged_at") or data["created_at"],
        "closed_at": data.get("merged_at"),
        "html_url": data["html_url"],
        "repository_url": f"https://api.github.com/repos/{repo}",
        "pull_request": {"merged_at": data.get("merged_at")},
        "merged": data.get("merged", False),
        "merged_at": data.get("merged_at"),
        "changed_files": cf.get("total", 0),
        "additions": cf.get("additions", 0),
        "deletions": cf.get("deletions", 0),
    }
    return RawSignal(
        raw_id=str(data["number"]),
        source_type=SourceType.GITHUB_PR,
        raw_data=raw_data,
    )


def _discovery_to_raw(sig: dict, repo: str) -> RawSignal:
    is_pr = "/pull/" in sig["html_url"]
    raw_data = {
        "number": sig["number"],
        "title": sig["title"],
        "body": "",
        "user": {"login": sig["author"]},
        "state": sig["state"],
        "labels": [{"name": l} for l in sig.get("labels", [])],
        "assignees": [],
        "comments": sig.get("comment_count", 0),
        "created_at": sig["created_at"],
        "updated_at": sig["updated_at"],
        "html_url": sig["html_url"],
        "repository_url": f"https://api.github.com/repos/{repo}",
    }
    if is_pr:
        raw_data["pull_request"] = {}
    st = SourceType.GITHUB_PR if is_pr else SourceType.GITHUB_ISSUE
    return RawSignal(raw_id=str(sig["number"]), source_type=st, raw_data=raw_data)


# ---------------------------------------------------------------------------
# Mock comments for #39303 (8 human + 2 bot)
# ---------------------------------------------------------------------------


def _make_comments() -> list[Comment]:
    sid = ISSUE_SID
    items = [
        (
            "c_001", "ghpu", "2026-04-08T14:00:00Z", False,
            "Added more reproduction details — the transition is exactly at "
            "ctx=2048 to 2049. Tested on both mi300x and mi355x with identical results.",
        ),
        (
            "c_002", "hongxiayang", "2026-04-08T16:30:00Z", False,
            "Thanks for the thorough report. I can reproduce on mi355x with "
            "the exact same nightly. The SplitKV boundary at 2048 is suspicious.",
        ),
        (
            "c_003", "github-actions[bot]", "2026-04-09T02:00:00Z", True,
            "CI passed on commit abc123def456",
        ),
        (
            "c_004", "tjtanaa", "2026-04-09T08:15:00Z", False,
            "This looks related to the SplitKV refactor that landed on April 5th. "
            "The shared memory buffer size was changed from 2048*sizeof(float) to a "
            "computed value.",
        ),
        (
            "c_005", "rasmith", "2026-04-10T11:00:00Z", False,
            "Confirmed the regression window. Git bisect points to aiter commit "
            "7a2f3e1 which restructured the pa_mqa_logits kernel grid dispatch.",
        ),
        (
            "c_006", "ghpu", "2026-04-11T09:00:00Z", False,
            "Bisected further: the issue is in _deepgemm_fp8_paged_mqa_logits_stage1 "
            "where the SplitKV range computation uses a hardcoded 2048 max instead of "
            "the actual context length.",
        ),
        (
            "c_007", "gemini-code-assist[bot]", "2026-04-12T03:00:00Z", True,
            "<!-- Automated analysis: this issue affects the aiter attention kernel "
            "path on ROCm gfx950 targets -->",
        ),
        (
            "c_008", "micah-wil", "2026-04-13T15:30:00Z", False,
            "Workaround confirmed: setting VLLM_ROCM_USE_AITER=0 falls back to the "
            "triton attention path which handles arbitrary context lengths correctly. "
            "Performance drops ~30% but output is correct.",
        ),
        (
            "c_009", "hongxiayang", "2026-04-15T10:00:00Z", False,
            "Fix PR incoming. The root cause was a shared memory allocation that "
            "capped at 2048 positions per SplitKV partition. Extending to "
            "max_model_len fixes it.",
        ),
        (
            "c_010", "ghpu", "2026-04-16T14:00:00Z", False,
            "Confirmed fixed on latest nightly (image id a3b4c5d6). Full context "
            "range tested up to 16384 tokens — all topk_set_match values at 1.0. "
            "Thank you everyone!",
        ),
    ]
    return [
        Comment(
            signal_id=sid,
            comment_id=cid,
            author=author,
            body=body,
            body_token_estimate=estimate_tokens(body),
            created_at=ts,
            is_bot=bot,
        )
        for cid, author, ts, bot, body in items
    ]


# ---------------------------------------------------------------------------
# Change events for #39303 (new_signal + state_change + new_comment)
# ---------------------------------------------------------------------------


def _make_change_events() -> list[ChangeEvent]:
    sid = ISSUE_SID
    det = SYNCED_AT
    return [
        ChangeEvent(
            signal_id=sid,
            change_type=ChangeType.NEW_SIGNAL,
            changed_at="2026-04-08T13:27:58Z",
            detected_at=det,
            new_value=json.dumps({"signal_id": sid}),
            is_meaningful=True,
            sync_run_id=SYNC_RUN_ID,
        ),
        ChangeEvent(
            signal_id=sid,
            change_type=ChangeType.STATE_CHANGE,
            changed_at="2026-04-16T14:26:57Z",
            detected_at=det,
            old_value=json.dumps("open"),
            new_value=json.dumps("closed"),
            is_meaningful=True,
            sync_run_id=SYNC_RUN_ID,
        ),
        ChangeEvent(
            signal_id=sid,
            change_type=ChangeType.NEW_COMMENT,
            changed_at="2026-04-16T14:00:00Z",
            detected_at=det,
            old_value=json.dumps(0),
            new_value=json.dumps(11),
            is_meaningful=True,
            sync_run_id=SYNC_RUN_ID,
        ),
    ]


# ---------------------------------------------------------------------------
# Main generation
# ---------------------------------------------------------------------------


def generate():
    print("=== Generating fixture data ===")
    print("Cleaning old fixtures...")
    _clean()

    FIXTURE_DB.parent.mkdir(parents=True, exist_ok=True)
    print(f"Initializing DB: {FIXTURE_DB}")
    init_db(FIXTURE_DB)

    norm = Normalizer()
    all_signals = []
    comment_map: dict[str, list[Comment]] = {}

    # ── Load demo files ──
    issue_data = _load_json("issue_39303.json")
    pr_data = _load_json("pr_39616.json")
    disc_data = _load_json("discovery_vllm_rocm.json")

    # ── Normalize issue #39303 ──
    sig_issue = norm.normalize_signal(
        _demo_issue_to_raw(issue_data, issue_data["repo"]),
        sync_run_id=SYNC_RUN_ID,
        first_seen_at=SYNCED_AT,
        last_synced_at=SYNCED_AT,
    )
    all_signals.append(sig_issue)

    # ── Normalize PR #39616 ──
    sig_pr = norm.normalize_signal(
        _demo_pr_to_raw(pr_data, pr_data["repo"]),
        sync_run_id=SYNC_RUN_ID,
        first_seen_at=SYNCED_AT,
        last_synced_at=SYNCED_AT,
    )
    all_signals.append(sig_pr)

    # ── Normalize discovery signals (20 items) ──
    for entry in disc_data["signals"]:
        sig = norm.normalize_signal(
            _discovery_to_raw(entry, disc_data["repo"]),
            sync_run_id=SYNC_RUN_ID,
            first_seen_at=SYNCED_AT,
            last_synced_at=SYNCED_AT,
        )
        all_signals.append(sig)

    print(f"Normalized {len(all_signals)} signals")

    # ── Generate mock data ──
    comments = _make_comments()
    comment_map[ISSUE_SID] = comments
    changes = _make_change_events()

    # ── Write to DB ──
    print("Writing to fixture DB...")
    with SignalRepository(FIXTURE_DB) as repo:
        for sig in all_signals:
            repo.upsert_signal(sig)
        n_comments = repo.upsert_comments(comments)
        n_changes = repo.append_changes(changes)
        print(f"  Inserted {n_comments} comments, {n_changes} changes")

        ok = repo.update_classification(
            ISSUE_SID,
            gap_ids=["gap_001"],
            signal_category="amd_gap",
            confidence=0.85,
            classifier_version="v1",
        )
        print(f"  Classified {ISSUE_SID}: {ok}")

    # ── Write JSON cache ──
    print(f"Writing cache to {FIXTURE_CACHE}...")
    cache = JSONCache(FIXTURE_CACHE)
    for sig in all_signals:
        cache.write(sig, comment_map.get(sig.signal_id, []))

    # ── Report stats ──
    with SignalRepository(FIXTURE_DB) as repo:
        conn = repo.connection
        n_sig = repo.count_signals()
        n_com = conn.execute(
            "SELECT COUNT(*) AS n FROM signal_comments"
        ).fetchone()["n"]
        n_chg = conn.execute(
            "SELECT COUNT(*) AS n FROM signal_changes"
        ).fetchone()["n"]
        n_ref = conn.execute(
            "SELECT COUNT(*) AS n FROM signal_refs"
        ).fetchone()["n"]

    n_cache = len(list(FIXTURE_CACHE.rglob("*.json")))

    print(f"\n{'=' * 40}")
    print(f"Signals:    {n_sig}")
    print(f"Comments:   {n_com}")
    print(f"Changes:    {n_chg}")
    print(f"Refs:       {n_ref}")
    print(f"Cache:      {n_cache} files")
    print(f"{'=' * 40}")
    print("Done!")

    return n_sig, n_com, n_chg, n_ref


if __name__ == "__main__":
    generate()
