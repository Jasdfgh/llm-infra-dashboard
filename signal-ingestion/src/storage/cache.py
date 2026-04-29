"""JSONCache — file-backed cache for full Signal+Comments dumps.

JSON file cache for complete signal + comments snapshots (D4 Step 8):

> For signals with changes:
>     Path: data/cache/github/{repo_slug}/issues/{number}.json
>     Content: Full Signal envelope + embedded comments[]
>     Purpose: Worker Agent reads the file directly for context injection, no DB query needed

Path convention
---------------
``{base_dir}/github/{repo_slug}/issues/{number}.json``   (issues)
``{base_dir}/github/{repo_slug}/pulls/{number}.json``    (PRs)
``{base_dir}/twitter/{tweet_id}.json``                   (future)
``{base_dir}/arxiv/{paper_id}.json``                     (future)

``repo_slug`` replaces ``/`` with ``_``:
``"vllm-project/vllm"`` → ``"vllm-project_vllm"``. This keeps filesystems
happy on every platform (including Windows) and is trivially reversible.

Atomic writes
-------------
We write to ``{path}.tmp`` then ``os.replace`` into the final path.
``os.replace`` is atomic on the same filesystem (POSIX ``rename``). This
avoids torn writes if the process crashes mid-write — a Worker Agent
reading the cache file concurrently will see either the old file or the
new file, never a partial one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional, Union

from src.ingestion.models import Comment, Signal

DEFAULT_CACHE_DIR: Path = Path("data/cache")


def _enum_to_str(value: Any) -> Any:
    """Unwrap Enum.value; pass through otherwise.

    Defensive helper — Signal's ``source_type`` is already a string at
    runtime thanks to ``use_enum_values=True``, but callers may hand us a
    raw dict or enum.
    """
    return getattr(value, "value", value)


class JSONCache:
    """File-backed cache at ``base_dir`` (default ``data/cache``).

    See module docstring for path convention and atomic-write guarantee.
    """

    def __init__(self, base_dir: str | Path = DEFAULT_CACHE_DIR) -> None:
        """Build a cache rooted at ``base_dir``.

        The directory is NOT created eagerly — it's created lazily on
        first ``write()`` so a read-only deployment doesn't accidentally
        create an empty cache tree.
        """
        self.base_dir: Path = Path(base_dir)

    # ── Path resolution ───────────────────────────────────

    @staticmethod
    def _repo_slug(repo: str) -> str:
        """``owner/repo`` → ``owner_repo``. Reversible and filesystem-safe."""
        return repo.replace("/", "_")

    def path_for_signal(
        self, signal: Union[Signal, dict[str, Any], str]
    ) -> Path:
        """Compute the cache file path for a signal.

        Accepts:
        * ``Signal`` — reads ``source_type`` / ``source_repo`` / ``source_number``.
        * ``dict``   — same fields (plus ``signal_id`` fallback for the tail).
        * ``str``    — a ``signal_id`` like ``github:owner/repo:issue:NNN``.

        For ``signal_id`` strings we parse the deterministic format from
        D2.1. Unknown source types fall back to ``misc/{safe_id}.json``.
        """
        source_type: Any
        source_repo: Optional[str]
        source_number: Optional[int]
        signal_id: str

        if isinstance(signal, Signal):
            source_type = signal.source_type
            source_repo = signal.source_repo
            source_number = signal.source_number
            signal_id = signal.signal_id
        elif isinstance(signal, dict):
            source_type = signal.get("source_type")
            source_repo = signal.get("source_repo")
            source_number = signal.get("source_number")
            signal_id = str(signal.get("signal_id", ""))
        elif isinstance(signal, str):
            signal_id = signal
            source_type, source_repo, source_number = self._parse_signal_id(
                signal_id
            )
        else:  # pragma: no cover — developer error
            raise TypeError(
                f"path_for_signal: unsupported input type {type(signal)!r}"
            )

        st = _enum_to_str(source_type)

        if st == "github_issue":
            if source_repo is None or source_number is None:
                raise ValueError(
                    "github_issue signal missing source_repo / source_number"
                )
            return (
                self.base_dir
                / "github"
                / self._repo_slug(source_repo)
                / "issues"
                / f"{source_number}.json"
            )
        if st == "github_pr":
            if source_repo is None or source_number is None:
                raise ValueError(
                    "github_pr signal missing source_repo / source_number"
                )
            return (
                self.base_dir
                / "github"
                / self._repo_slug(source_repo)
                / "pulls"
                / f"{source_number}.json"
            )
        if st == "tweet":
            tail = signal_id.split(":", 1)[1] if ":" in signal_id else signal_id
            return self.base_dir / "twitter" / f"{self._safe_name(tail)}.json"
        if st == "arxiv_paper":
            tail = signal_id.split(":", 1)[1] if ":" in signal_id else signal_id
            return self.base_dir / "arxiv" / f"{self._safe_name(tail)}.json"

        # Unknown / future source type — bucket under misc/.
        return self.base_dir / "misc" / f"{self._safe_name(signal_id)}.json"

    @staticmethod
    def _parse_signal_id(
        signal_id: str,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        """Decompose a ``signal_id`` back to (source_type, repo, number).

        Format (D2.1):
            github:{repo}:{issue|pr}:{number}   → ("github_issue"|"github_pr", repo, N)
            twitter:{tweet_id}                  → ("tweet", None, None)
            arxiv:{paper_id}                    → ("arxiv_paper", None, None)
            blog:{hash}                         → ("blog_post", None, None)
            zhihu:{id}                          → ("zhihu_post", None, None)

        Returns a best-effort triple. Callers should treat None values as
        "use the signal_id tail for the filename".
        """
        parts = signal_id.split(":", 3)
        if not parts:
            return None, None, None

        head = parts[0]
        if head == "github" and len(parts) == 4:
            _, repo, kind, number_str = parts
            try:
                number = int(number_str)
            except ValueError:
                number = None
            source_type = "github_issue" if kind == "issue" else "github_pr"
            return source_type, repo, number
        if head == "twitter":
            return "tweet", None, None
        if head == "arxiv":
            return "arxiv_paper", None, None
        if head == "blog":
            return "blog_post", None, None
        if head == "zhihu":
            return "zhihu_post", None, None
        return None, None, None

    @staticmethod
    def _safe_name(raw: str) -> str:
        """Sanitize ``raw`` for use as a filename.

        Replaces ``/`` and ``:`` (common in signal_ids) with ``_``.
        """
        return raw.replace("/", "_").replace(":", "_")

    # ── I/O ───────────────────────────────────────────────

    def write(
        self,
        signal: Signal,
        comments: Optional[list[Comment]] = None,
    ) -> Path:
        """Atomically write ``signal + comments[]`` to the cache. Returns
        the final path.

        Content shape (matches D2.1 Signal envelope + embedded comments)::

            {
              "signal_id": "...",
              "source_type": "...",
              ...  # full Signal.model_dump(mode='json')
              "comments": [
                {"signal_id": "...", "comment_id": "...", ...},
                ...
              ]
            }

        The ``mode='json'`` dump ensures any Enum / datetime values
        become JSON-native primitives.
        """
        path = self.path_for_signal(signal)
        path.parent.mkdir(parents=True, exist_ok=True)

        payload: dict[str, Any] = signal.model_dump(mode="json")
        payload["comments"] = [
            c.model_dump(mode="json") for c in (comments or [])
        ]

        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())  # durability best-effort
            except OSError:
                # Some filesystems (e.g. certain network mounts) refuse
                # fsync; the rename is still atomic — not a hard failure.
                pass
        os.replace(tmp, path)
        return path

    def read(self, signal_id: str) -> Optional[dict[str, Any]]:
        """Read back by ``signal_id``. Returns None if not cached.

        The returned dict is the raw JSON — not re-validated against the
        Pydantic ``Signal`` model. Callers that need a ``Signal`` object
        should do ``Signal.model_validate(cache.read(sid))`` themselves.
        """
        path = self.path_for_signal(signal_id)
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    def exists(self, signal_id: str) -> bool:
        """True iff the cache file exists on disk."""
        return self.path_for_signal(signal_id).exists()
