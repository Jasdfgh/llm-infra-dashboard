"""Abstract source-adapter interface.

Every source platform (GitHub, Twitter, ArXiv, RSS, Zhihu, …) is
exposed to the rest of the ingestion pipeline through a subclass of
``SourceAdapter``. The scheduler, normalizer, and change detector all
speak only this interface — no source-specific logic (auth, API paths,
pagination, rate-limiting quirks) leaks past the adapter boundary.

See ``design/module_1_3_architecture.md`` D6 ("SourceAdapter interface")
for the full contract.

Enhancement over D6 (documented here, not in the design doc yet):

- ``discover`` is typed as ``AsyncIterator[RawSignal]`` (an async
  generator) instead of ``list[RawSignal]``. The scheduler can then
  stream the first page of results into the normalizer while later
  pages are still being fetched. For sources that paginate to tens
  of pages, this dramatically shortens time-to-first-signal. Any
  list-shaped source can trivially wrap its results with a helper
  like ``async for item in self._list(...): yield item``.

Import constraints (enforced by Layer 1-B):

- **Only stdlib + ``src.ingestion.models``.** No httpx, no aiohttp,
  no yaml. The concrete HTTP clients live in each subclass.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, ClassVar

from src.ingestion.models import RawComment, RawSignal, SourceType


@dataclass
class SourceConfig:
    """Source-specific configuration block passed to adapter methods.

    ``params`` is intentionally untyped (``dict[str, Any]``) so each
    source can declare its own shape. The authoritative schema for a
    given source's params lives in:

    - ``config/sources.yaml`` (runtime config)
    - that source's adapter module docstring (developer contract)

    Example (GitHub)::

        SourceConfig(
            source_type=SourceType.GITHUB_ISSUE,
            params={
                "repo": "vllm-project/vllm",
                "labels": ["rocm"],
                "state": "all",
            },
        )
    """

    source_type: SourceType
    params: dict[str, Any] = field(default_factory=dict)


class SourceAdapter(ABC):
    """Abstract interface that every source adapter must implement.

    Subclasses must:

    1. **Set the ``source_type`` class attribute** to a ``SourceType``
       enum value. This is how the pipeline routes incoming
       ``SourceConfig`` objects to the right adapter instance and how
       the Normalizer picks the right mapping branch.
    2. **Implement all four abstract methods** (``discover``,
       ``fetch_detail``, ``fetch_comments``, ``make_signal_id``).
    3. **Stay async.** Even if the underlying call is sync, wrap it so
       callers can ``await`` uniformly.

    See ``design/module_1_3_architecture.md`` D6 for the full contract.
    """

    # Abstract class-level attribute: subclasses MUST assign, e.g.
    #   class GitHubAdapter(SourceAdapter):
    #       source_type = SourceType.GITHUB_ISSUE
    # ``ClassVar`` signals "this is a class variable, not an instance
    # field" to type checkers. It is intentionally left without a
    # default so that accessing it on a non-overriding subclass raises
    # AttributeError at use time — cleaner than a silent default.
    source_type: ClassVar[SourceType]

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------

    @abstractmethod
    def discover(
        self,
        config: SourceConfig,
        *,
        since: str | None = None,
    ) -> AsyncIterator[RawSignal]:
        """Batch-discover raw signals matching ``config``.

        Implementations MUST be **async generators**: declare with
        ``async def`` and ``yield`` each ``RawSignal`` as it becomes
        available. Example::

            class GitHubAdapter(SourceAdapter):
                source_type = SourceType.GITHUB_ISSUE

                async def discover(
                    self, config: SourceConfig, *, since: str | None = None,
                ) -> AsyncIterator[RawSignal]:
                    page = 1
                    while True:
                        items = await self._list_issues(config, page, since)
                        if not items:
                            break
                        for item in items:
                            yield RawSignal(
                                raw_id=str(item["number"]),
                                source_type=self.source_type,
                                raw_data=item,
                            )
                        page += 1

        Args:
            config: source-specific configuration (repo, keywords, etc.).
            since: optional ISO 8601 UTC timestamp for incremental mode.
                When set, only items updated strictly after this time
                should be yielded. ``None`` means "full scan".

        Yields:
            ``RawSignal`` objects, one per discovered item. Order is
            adapter-defined (typically source-sorted by ``updated_at``
            descending).

        Note:
            This is an **enhancement over D6**, which specified
            ``list[RawSignal]``. Async generators let the scheduler
            start processing before pagination finishes.
        """
        # Abstract body — never executed; subclasses override. The
        # ``raise`` keeps static analyzers from complaining about the
        # "missing return" in a function typed to return AsyncIterator.
        raise NotImplementedError

    @abstractmethod
    async def fetch_detail(self, raw_id: str, **kwargs: Any) -> RawSignal:
        """Deep-fetch a single item by its source-native raw ID.

        Used in *targeted* sync mode: the scheduler has a specific item
        in mind (e.g. a reconciliation fill-in, a user-triggered
        refresh) and wants the full payload rather than what discovery
        would return.

        Args:
            raw_id: source-native identifier. Examples:

                * GitHub issue / PR: ``"39303"`` (the numeric part only;
                  pass ``repo`` via ``kwargs``).
                * Twitter: ``"1234567890"``.
                * ArXiv: ``"2508.12345"``.

            **kwargs: source-specific options. For example, GitHub
                requires ``repo="owner/name"`` because issue numbers
                are not globally unique.

        Returns:
            A fully-populated ``RawSignal``.

        Raises:
            Source-specific errors. Subclasses SHOULD document the
            concrete exception types in their own docstrings.
        """

    @abstractmethod
    async def fetch_comments(
        self,
        raw_id: str,
        *,
        since: str | None = None,
    ) -> list[RawComment]:
        """Return the comments / replies attached to a single item.

        Args:
            raw_id: same identifier shape as ``fetch_detail``.
            since: optional ISO 8601 UTC timestamp. When set, return
                only comments created after this time. Enables
                incremental comment sync (comments are append-only on
                most platforms so this is a useful optimization).

        Returns:
            ``RawComment`` objects in chronological order (oldest
            first). Empty list if the item has no comments.

        Raises:
            Source-specific errors (same convention as
            ``fetch_detail``).
        """

    @abstractmethod
    def make_signal_id(self, raw: dict[str, Any]) -> str:
        """Compute a deterministic ``signal_id`` for a raw payload.

        The ID MUST satisfy three properties:

        1. **Stable**: the same ``raw`` input produces the same ID
           across processes and runs. No randomness, no timestamps.
        2. **Unique within ``source_type``**: two different source
           items never collide on ID.
        3. **Follow the per-source format from D2.1**:

           * github: ``github:{repo}:{issue|pr}:{number}``
           * twitter: ``twitter:{tweet_id}``
           * arxiv: ``arxiv:{paper_id}``
           * blog: ``blog:{sha256(canonical_url)[:16]}``
           * zhihu: ``zhihu:{answer_id|article_id}``

        This is not an async method because ID generation is purely
        computational — no IO.

        Args:
            raw: the ``RawSignal.raw_data`` dict, i.e. the source's
                native JSON payload.

        Returns:
            The ``signal_id`` string.
        """
