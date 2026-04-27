"""Reference extraction from free-form signal text.

Parses GitHub issue/PR references, external URLs (Hugging Face today,
more families tomorrow), and GitHub ``@mentions`` out of bodies and
comments. The output is a ``References`` Pydantic model that drops
directly into ``Signal.references``.

See ``design/module_1_3_architecture.md`` D4 Step 4c for the regex
catalog. The extraction rules here stay deliberately conservative:
we would rather miss an obscure shorthand than add a fake reference
that pollutes downstream reconciliation.

Design decisions:

- **Code blocks are stripped first.** Fenced blocks and inline
  backticks often contain ``#define``, ``#include``, or variable
  names like ``var#42`` that must not be classified as issue refs.
- **Match order matters.** We extract in specificity order (full
  GitHub URL → HF URL → ``owner/repo#N`` → bare ``#N``) and mask
  each match out of the working text so the next, looser pattern
  cannot re-capture the same fragment.
- **Bare ``#N`` has ``repo=None`` by default.** The Normalizer's
  reconciliation step (D5 Step 7e) fills it in against the source
  repo. Callers that already know the context can pass
  ``source_repo=...`` to have it filled in up-front.
- **Regexes are compiled once at module load.** Hot-path extraction
  runs millions of times across history backfill; avoiding
  ``re.compile`` per call is worth the module-level constants.
- **@mention denylist.** GitHub usernames can collide with Python
  decorators (``@staticmethod``), Javadoc tags (``@param``), and
  Markdown extensions (``@see``). We filter a small, commonly-seen
  set rather than try to build an authoritative list.
"""

from __future__ import annotations

import re

from src.ingestion.models import IssueRef, References


# ---------------------------------------------------------------------------
# Denylist for @mentions: tokens that look like mentions but are actually
# code-language keywords (Python decorators, Javadoc/Javascript-doc tags).
# Compared lowercased. Keep small and intentional; over-filtering risks
# dropping real user cc's.
# ---------------------------------------------------------------------------
_MENTION_DENYLIST: frozenset[str] = frozenset(
    {
        # Javadoc / docstring tags
        "param",
        "params",
        "return",
        "returns",
        "raises",
        "raise",
        "throws",
        "throw",
        "see",
        "since",
        "deprecated",
        "author",
        "version",
        "todo",
        "note",
        "example",
        "link",
        # Python decorators
        "staticmethod",
        "classmethod",
        "property",
        "abstractmethod",
        "override",
        "dataclass",
        "wraps",
        "functools",
        "cached_property",
        "contextmanager",
    }
)


# ---------------------------------------------------------------------------
# Pre-compiled patterns (module-level constants for performance)
# ---------------------------------------------------------------------------

# Fenced markdown code block ```lang ... ```. DOTALL so bodies can span lines.
_FENCED_CODE_RE: re.Pattern[str] = re.compile(r"```.*?```", re.DOTALL)

# Inline code `foo`. Forbidding newlines inside the capture keeps us from
# accidentally spanning across paragraphs when a user writes unmatched
# backticks.
_INLINE_CODE_RE: re.Pattern[str] = re.compile(r"`[^`\n]*`")

# Full GitHub issue/PR URL. groups: (repo, kind, number).
# Intentionally does NOT require a trailing word boundary — trailing
# punctuation (``)``, ``.``) is stripped in post-processing.
_GITHUB_URL_RE: re.Pattern[str] = re.compile(
    r"https?://github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)"
)

# Hugging Face URL (model, dataset, space, doc). Greedy path-char set,
# trailing punctuation stripped by ``_clean_url``.
_HF_URL_RE: re.Pattern[str] = re.compile(r"https?://huggingface\.co/[\w./-]+")

# Cross-repo ``owner/repo#N`` reference.
# The leading lookbehind ``(?<![\w./-])`` prevents matches inside URL paths:
# given ``domain.com/a/b#1``, the ``a`` char is preceded by ``/`` which
# fails the lookbehind, so nothing is produced. (Cross-repo refs appear
# at token boundaries in prose.)
_CROSS_REPO_RE: re.Pattern[str] = re.compile(
    r"(?<![\w./-])([\w.-]+)/([\w.-]+)#(\d+)"
)

# Bare ``#NNNN``. Lookbehind rejects word-char prefix so ``foo#42`` is
# not captured (that would require the cross-repo pattern's ``owner/`` prefix).
_BARE_HASH_RE: re.Pattern[str] = re.compile(r"(?<!\w)#(\d+)")

# GitHub ``@username``. GitHub usernames: [A-Za-z0-9-], 1-39 chars,
# cannot start or end with hyphen. We approximate with a reasonable bound;
# the denylist filters out non-user false positives.
_MENTION_RE: re.Pattern[str] = re.compile(
    r"(?<!\w)@([A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?)(?!\w)"
)

# Trailing punctuation chars to strip from captured URLs (URL grammars
# allow these but in prose they're almost always sentence punctuation).
_URL_TRAIL_PUNCT: str = ".,;:!?)\"']}>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_code(text: str) -> str:
    """Replace fenced and inline code blocks with whitespace.

    We replace with spaces (same length) rather than empty string so
    character offsets remain meaningful for any downstream tooling that
    wants to correlate matches to source positions. Spaces never match
    any of our reference regexes, so downstream passes are safe.
    """
    text = _FENCED_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    text = _INLINE_CODE_RE.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _mask_match(match: re.Match[str]) -> str:
    """Replace a regex match with an equal-length whitespace span."""
    return " " * len(match.group(0))


def _clean_url(url: str) -> str:
    """Strip trailing sentence punctuation that the greedy URL regex ate."""
    while url and url[-1] in _URL_TRAIL_PUNCT:
        url = url[:-1]
    return url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def extract_references(
    text: str,
    *,
    source_repo: str | None = None,
) -> References:
    """Extract GitHub refs, external URLs, and @mentions from free text.

    Args:
        text: body / comment content. Empty or ``None`` yields an empty
            ``References``.
        source_repo: the repo this text belongs to, used to fill in
            ``IssueRef.repo`` for bare ``#NNNN`` matches. Default ``None``
            so callers can defer resolution to the reconciliation pass
            (D5 Step 7e) which has richer context.

    Returns:
        A populated ``References`` object. Deduplication:

        - ``github_issues`` / ``github_prs``: unique by ``(repo, number)``.
        - ``external_urls``: unique by URL string.
        - ``mentions``: unique by username.

        First-seen order is preserved within each list.

    Notes:
        - GitHub URLs land in ``github_issues`` or ``github_prs`` based
          on the ``/issues/`` vs ``/pull/`` path segment. They are **not**
          also added to ``external_urls`` — the ``IssueRef.raw`` field
          preserves the URL, which is enough for downstream linkouts.
        - Bare ``#NNNN`` lands in ``github_issues`` because the ``#``
          syntax cannot distinguish issue from PR (GitHub shares the
          numbering space). The deep-fetch pass classifies correctly.
    """
    if not text:
        return References()

    stripped = _strip_code(text)

    github_issues: list[IssueRef] = []
    github_prs: list[IssueRef] = []
    external_urls: list[str] = []
    mentions: list[str] = []

    seen_issue_keys: set[tuple[str | None, int]] = set()
    seen_pr_keys: set[tuple[str | None, int]] = set()
    seen_urls: set[str] = set()
    seen_mentions: set[str] = set()

    # ── Pass 1: Full GitHub URLs (most specific first) ────────────────
    for match in _GITHUB_URL_RE.finditer(stripped):
        repo = match.group(1)
        kind = match.group(2)
        number = int(match.group(3))
        raw = _clean_url(match.group(0))
        ref = IssueRef(repo=repo, number=number, raw=raw)
        key = (repo, number)
        if kind == "issues":
            if key not in seen_issue_keys:
                github_issues.append(ref)
                seen_issue_keys.add(key)
        else:  # "pull"
            if key not in seen_pr_keys:
                github_prs.append(ref)
                seen_pr_keys.add(key)

    # Mask GitHub URLs so later passes cannot re-extract their fragments
    # (e.g. the trailing ``/39300`` would otherwise feed the bare-#N pass
    # if it contained a ``#``, and ``vllm-project/vllm`` would feed the
    # cross-repo pass).
    stripped_no_gh_url = _GITHUB_URL_RE.sub(_mask_match, stripped)

    # ── Pass 2: Hugging Face URLs ─────────────────────────────────────
    for match in _HF_URL_RE.finditer(stripped_no_gh_url):
        url = _clean_url(match.group(0))
        if url and url not in seen_urls:
            external_urls.append(url)
            seen_urls.add(url)

    stripped_no_urls = _HF_URL_RE.sub(_mask_match, stripped_no_gh_url)

    # ── Pass 3: Cross-repo ``owner/repo#N`` ───────────────────────────
    for match in _CROSS_REPO_RE.finditer(stripped_no_urls):
        repo = f"{match.group(1)}/{match.group(2)}"
        number = int(match.group(3))
        raw = match.group(0)
        ref = IssueRef(repo=repo, number=number, raw=raw)
        key = (repo, number)
        # ``#`` syntax doesn't distinguish issue vs PR → default to issues.
        if key not in seen_issue_keys:
            github_issues.append(ref)
            seen_issue_keys.add(key)

    stripped_no_cross = _CROSS_REPO_RE.sub(_mask_match, stripped_no_urls)

    # ── Pass 4: Bare ``#NNNN`` ────────────────────────────────────────
    for match in _BARE_HASH_RE.finditer(stripped_no_cross):
        number = int(match.group(1))
        raw = match.group(0)
        ref = IssueRef(repo=source_repo, number=number, raw=raw)
        key = (source_repo, number)
        if key not in seen_issue_keys:
            github_issues.append(ref)
            seen_issue_keys.add(key)

    # ── Pass 5: @mentions ─────────────────────────────────────────────
    # Scan the code-stripped text (not the URL-masked one) so that a
    # ``cc @user`` appearing adjacent to a URL is not lost. URLs very
    # rarely contain ``@`` in human GitHub prose.
    for match in _MENTION_RE.finditer(stripped):
        name = match.group(1)
        if name.lower() in _MENTION_DENYLIST:
            continue
        if name not in seen_mentions:
            mentions.append(name)
            seen_mentions.add(name)

    return References(
        github_issues=github_issues,
        github_prs=github_prs,
        external_urls=external_urls,
        mentions=mentions,
    )
