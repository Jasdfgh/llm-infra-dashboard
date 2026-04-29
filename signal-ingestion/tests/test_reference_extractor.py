"""Unit tests for ``src.ingestion.reference_extractor``.

Covers the full matrix from the task spec:

* bare ``#NNNN`` refs (with and without ``source_repo`` context)
* cross-repo ``owner/repo#N`` refs
* full GitHub URLs (``/issues/`` vs ``/pull/``)
* Hugging Face URLs → ``external_urls``
* ``@mentions`` with denylist filtering (``@param``, ``@staticmethod``, …)
* code-block stripping (inline + fenced)
* URL fragments (``foo.com/a/b#99``) must not leak
* deduplication + empty/None inputs
* real demo payload from ``tests/fixtures/signals/pr_39616.json``

These are pure-function tests; no DB / network / fixtures beyond the
JSON demo file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.ingestion.reference_extractor import extract_references


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


DEMO_DIR = Path(__file__).resolve().parent / "fixtures" / "signals"


@pytest.fixture(scope="module")
def pr_39616_raw() -> dict:
    """Load the demo PR payload (used by the integration-style test)."""
    return json.loads((DEMO_DIR / "pr_39616.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Bare #NNNN
# ---------------------------------------------------------------------------


def test_bare_hash_without_source_repo() -> None:
    """``#39303`` with no source_repo → IssueRef(repo=None, number=39303)."""
    refs = extract_references("See #39303 for details.")
    assert len(refs.github_issues) == 1
    ref = refs.github_issues[0]
    assert ref.repo is None
    assert ref.number == 39303
    assert ref.raw == "#39303"


def test_bare_hash_with_source_repo_fills_repo() -> None:
    """``#39303`` with source_repo="vllm/vllm" → repo is filled in."""
    refs = extract_references("See #39303 for details.", source_repo="vllm/vllm")
    assert len(refs.github_issues) == 1
    assert refs.github_issues[0].repo == "vllm/vllm"
    assert refs.github_issues[0].number == 39303


def test_bare_hash_rejects_word_char_prefix() -> None:
    """``foo#42`` (no space) is not a bare hash ref — word-char lookbehind blocks."""
    refs = extract_references("variable foo#42 is not an issue")
    assert refs.github_issues == []


# ---------------------------------------------------------------------------
# Cross-repo owner/repo#N
# ---------------------------------------------------------------------------


def test_cross_repo_ref() -> None:
    """``ROCm/aiter#2720`` → IssueRef(repo="ROCm/aiter", number=2720)."""
    refs = extract_references("tracked in ROCm/aiter#2720.")
    assert len(refs.github_issues) == 1
    ref = refs.github_issues[0]
    assert ref.repo == "ROCm/aiter"
    assert ref.number == 2720
    assert ref.raw == "ROCm/aiter#2720"


def test_cross_repo_ref_with_dots_and_dashes() -> None:
    """Cross-repo pattern allows ``.`` / ``-`` inside owner/name segments."""
    refs = extract_references("see vllm-project/vllm#1 and a.b/c.d#7")
    keys = {(r.repo, r.number) for r in refs.github_issues}
    assert ("vllm-project/vllm", 1) in keys
    assert ("a.b/c.d", 7) in keys


# ---------------------------------------------------------------------------
# Full GitHub URLs
# ---------------------------------------------------------------------------


def test_github_issue_url_goes_to_github_issues() -> None:
    """A ``/issues/N`` URL is captured in ``github_issues``, not external_urls."""
    text = "See https://github.com/vllm-project/vllm/issues/39300 for context."
    refs = extract_references(text)
    assert len(refs.github_issues) == 1
    assert refs.github_issues[0].repo == "vllm-project/vllm"
    assert refs.github_issues[0].number == 39300
    assert refs.github_issues[0].raw == (
        "https://github.com/vllm-project/vllm/issues/39300"
    )
    assert refs.external_urls == []
    assert refs.github_prs == []


def test_github_pr_url_goes_to_github_prs() -> None:
    """``/pull/N`` (singular, not ``/pulls/``) → ``github_prs``."""
    text = "Fixed in https://github.com/vllm-project/vllm/pull/39616"
    refs = extract_references(text)
    assert len(refs.github_prs) == 1
    assert refs.github_prs[0].repo == "vllm-project/vllm"
    assert refs.github_prs[0].number == 39616
    assert refs.github_issues == []


def test_github_url_does_not_also_emit_cross_repo() -> None:
    """URL masking prevents the cross-repo pattern from re-capturing the fragment."""
    text = "https://github.com/vllm-project/vllm/issues/39300"
    refs = extract_references(text)
    # Exactly one github_issue — not also a cross-repo one.
    assert len(refs.github_issues) == 1


# ---------------------------------------------------------------------------
# Hugging Face URLs
# ---------------------------------------------------------------------------


def test_huggingface_url_is_external() -> None:
    """HF URLs land in ``external_urls`` (plain string, deduped on URL)."""
    text = "model https://huggingface.co/amd/Kimi-K2.5-MXFP4 works"
    refs = extract_references(text)
    assert refs.external_urls == ["https://huggingface.co/amd/Kimi-K2.5-MXFP4"]


def test_huggingface_url_trailing_punct_stripped() -> None:
    """Sentence-trailing punctuation must be stripped from captured URLs."""
    text = "see https://huggingface.co/amd/Kimi-K2.5-MXFP4)."
    refs = extract_references(text)
    assert refs.external_urls == ["https://huggingface.co/amd/Kimi-K2.5-MXFP4"]


# ---------------------------------------------------------------------------
# @mentions
# ---------------------------------------------------------------------------


def test_mention_captured() -> None:
    """Plain ``@username`` mention lands in ``mentions``."""
    refs = extract_references("cc @hongxiayang please review")
    assert refs.mentions == ["hongxiayang"]


def test_mention_denylist_excludes_decorators_and_doctags() -> None:
    """Python decorators and javadoc tags must NOT land in mentions."""
    text = "@param x is the input. @staticmethod wraps it. @see also."
    refs = extract_references(text)
    assert refs.mentions == []


def test_mention_mixed_real_and_denylisted() -> None:
    """A real mention next to denylisted tokens survives; denylist entries drop."""
    text = "@param hello from @alice and @staticmethod"
    refs = extract_references(text)
    assert refs.mentions == ["alice"]


# ---------------------------------------------------------------------------
# Code-block stripping
# ---------------------------------------------------------------------------


def test_inline_code_suppresses_hash_ref() -> None:
    """Inline ``code`` blocks mask their content: ``` `#999` ``` must not leak."""
    refs = extract_references("fix `#999` per the style guide")
    assert refs.github_issues == []


def test_fenced_code_suppresses_hash_ref() -> None:
    """Fenced code blocks mask their content: ``#123`` inside ```` ```python``` ```` is ignored."""
    text = "prose\n```python\n#123\nprint('hi')\n```\ntrailing prose"
    refs = extract_references(text)
    assert refs.github_issues == []


def test_fenced_code_suppresses_mention() -> None:
    """Mentions inside fenced code should not be extracted."""
    text = "prose\n```python\n@alice\n```\n"
    refs = extract_references(text)
    assert refs.mentions == []


# ---------------------------------------------------------------------------
# URL fragment safety
# ---------------------------------------------------------------------------


def test_url_fragment_not_captured_as_cross_repo() -> None:
    """``https://example.com/foo/bar#99`` must not be parsed as cross-repo ref."""
    text = "see https://example.com/foo/bar#99 in the docs"
    refs = extract_references(text)
    assert refs.github_issues == []


def test_url_fragment_not_captured_as_bare_hash() -> None:
    """The ``#99`` inside the URL must not fall through to bare-hash either."""
    text = "link https://example.com/path#99"
    refs = extract_references(text)
    assert refs.github_issues == []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_dedup_same_issue_twice() -> None:
    """An issue mentioned twice is returned only once (first-seen wins)."""
    refs = extract_references("#42 and again #42!", source_repo="foo/bar")
    assert len(refs.github_issues) == 1
    assert refs.github_issues[0].number == 42


def test_dedup_url_then_cross_repo_same_target() -> None:
    """A GitHub URL + a matching ``owner/repo#N`` collapse to a single ref."""
    text = (
        "see https://github.com/vllm-project/vllm/issues/1 "
        "and vllm-project/vllm#1"
    )
    refs = extract_references(text)
    assert len(refs.github_issues) == 1
    # URL pass wins (it ran first): raw contains the full URL.
    assert refs.github_issues[0].raw.startswith("https://")


def test_dedup_mentions() -> None:
    """Repeated mentions are returned once, preserving first-seen order."""
    refs = extract_references("cc @alice @bob @alice @carol @bob")
    assert refs.mentions == ["alice", "bob", "carol"]


# ---------------------------------------------------------------------------
# Empty / None inputs
# ---------------------------------------------------------------------------


def test_empty_string_returns_empty_refs() -> None:
    """Empty input short-circuits to an empty ``References``."""
    refs = extract_references("")
    assert refs.github_issues == []
    assert refs.github_prs == []
    assert refs.external_urls == []
    assert refs.mentions == []


def test_none_input_returns_empty_refs() -> None:
    """``None`` is tolerated by the ``if not text`` guard."""
    refs = extract_references(None)  # type: ignore[arg-type]
    assert refs.github_issues == []
    assert refs.github_prs == []
    assert refs.external_urls == []
    assert refs.mentions == []


# ---------------------------------------------------------------------------
# Real demo payload
# ---------------------------------------------------------------------------


def test_demo_pr_body_extracts_2720_and_hf_urls(pr_39616_raw: dict) -> None:
    """PR 39616 body should yield ROCm/aiter#2720 + two HF URLs."""
    body = pr_39616_raw["body_text"]
    refs = extract_references(body, source_repo="vllm-project/vllm")

    # ROCm/aiter#2720 should appear — extracted from the full GitHub URL.
    issue_keys = {(r.repo, r.number) for r in refs.github_issues}
    assert ("ROCm/aiter", 2720) in issue_keys, (
        f"expected ROCm/aiter#2720 in {issue_keys}"
    )

    # Both HF URLs should be in external_urls.
    assert "https://huggingface.co/amd/Kimi-K2.5-MXFP4" in refs.external_urls
    assert (
        "https://huggingface.co/lightseekorg/kimi-k2.5-eagle3"
        in refs.external_urls
    )
