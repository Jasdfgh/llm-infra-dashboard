"""Integration test for get_feed() enhancements and update_classification().

Verifies D3.1 feed fields (body_preview, recent_changes, total_token_estimate)
and Module 2 classification write-back. Uses a temp DB — no external deps.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from src.ingestion.models import (  # noqa: E402
    ChangeEvent,
    ChangeType,
    GitHubPayload,
    Signal,
)
from src.storage.database import init_db  # noqa: E402
from src.storage.repository import SignalRepository  # noqa: E402
from src.storage.search import SignalSearch  # noqa: E402

_PASS = 0
_FAIL = 0


def _check(label: str, condition: bool, detail: str = "") -> None:
    global _PASS, _FAIL
    if condition:
        _PASS += 1
        print(f"  OK  {label}")
    else:
        _FAIL += 1
        msg = f"  FAIL  {label}"
        if detail:
            msg += f" — {detail}"
        print(msg)


def main() -> int:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "test_feed.db"
    init_db(db)
    now = datetime.now(timezone.utc).isoformat()

    long_body = "This is a detailed body about aiter MLA bug. " * 10

    with SignalRepository(db) as repo:
        sig = Signal(
            signal_id="github:test/repo:issue:42",
            source_type="github_issue",
            source_url="https://github.com/test/repo/issues/42",
            source_repo="test/repo",
            source_number=42,
            title="ROCm aiter bug",
            body=long_body,
            body_token_estimate=480,
            author="alice",
            created_at="2026-04-01T00:00:00Z",
            updated_at="2026-04-20T00:00:00Z",
            first_seen_at=now,
            last_synced_at=now,
            content_hash="a" * 64,
            tags=["bug", "rocm"],
            github=GitHubPayload(state="open", labels=["bug", "rocm"], comment_count=7),
        )
        repo.upsert_signal(sig)

        ev = ChangeEvent(
            signal_id=sig.signal_id,
            change_type=ChangeType.NEW_COMMENT,
            changed_at="2026-04-20T10:00:00Z",
            detected_at=now,
            old_value=json.dumps(5),
            new_value=json.dumps(7),
            is_meaningful=True,
            sync_run_id="s1",
        )
        repo.append_changes([ev])

        noise = ChangeEvent(
            signal_id=sig.signal_id,
            change_type=ChangeType.BODY_EDIT,
            changed_at="2026-04-19T00:00:00Z",
            detected_at=now,
            old_value=None,
            new_value=None,
            is_meaningful=False,
            sync_run_id="s1",
        )
        repo.append_changes([noise])

        # ── update_classification ──
        print("=== update_classification ===")
        ok = repo.update_classification(
            "github:test/repo:issue:42",
            gap_ids=["gap_001", "gap_002"],
            signal_category="amd_gap",
            confidence=0.85,
            classifier_version="v1",
        )
        _check("returns True for existing signal", ok is True)

        row = repo.get_by_id("github:test/repo:issue:42")
        cls = json.loads(row["classification_json"])
        _check("signal_category written", cls["signal_category"] == "amd_gap")
        _check("gap_ids in classification_json", cls["gap_ids"] == ["gap_001", "gap_002"])
        _check("confidence written", cls["confidence"] == 0.85)
        _check("classifier_version", cls["classifier_version"] == "v1")
        _check("classified_at is ISO string", "T" in cls["classified_at"])

        gaps_col = json.loads(row["gap_ids"])
        _check("gap_ids column matches", gaps_col == ["gap_001", "gap_002"])

        ok2 = repo.update_classification(
            "nonexistent:signal",
            gap_ids=[],
            signal_category="noise",
            confidence=0.1,
            classifier_version="v1",
        )
        _check("returns False for nonexistent", ok2 is False)

        # ── get_feed enhancements ──
        print("\n=== get_feed enhancements ===")
        search = SignalSearch(repo)
        feed = search.get_feed(since="2026-04-01T00:00:00Z", classified=None)
        sigs = feed["signals"]
        _check("feed returns >= 1 signal", len(sigs) >= 1)

        s = sigs[0]

        # body_preview
        _check("body_preview present", "body_preview" in s)
        _check(
            "body_preview <= 200 chars",
            len(s.get("body_preview", "")) <= 200,
            f"got {len(s.get('body_preview', ''))}",
        )
        _check(
            "body_preview starts correctly",
            s.get("body_preview", "").startswith("This is a detailed"),
        )

        # recent_changes
        _check("recent_changes present", "recent_changes" in s)
        rc_list = s.get("recent_changes", [])
        _check("has >= 1 recent_change", len(rc_list) >= 1, f"got {len(rc_list)}")
        if rc_list:
            rc = rc_list[0]
            _check("change_type is new_comment", rc["change_type"] == "new_comment")
            _check("old_value present", "old_value" in rc)
            _check("new_value present", "new_value" in rc)
        _check(
            "noise change excluded (is_meaningful=0)",
            all(c.get("change_type") != "body_edit" for c in rc_list),
        )

        # total_token_estimate in meta
        meta = feed.get("meta", {})
        _check("total_token_estimate in meta", "total_token_estimate" in meta)
        _check(
            "total_token_estimate value correct",
            meta.get("total_token_estimate") == 480,
            f"got {meta.get('total_token_estimate')}",
        )

    print(f"\n=== RESULTS: {_PASS} passed, {_FAIL} failed ===")
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
