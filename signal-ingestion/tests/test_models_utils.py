"""Unit tests for pure functions in ``src.ingestion.models``."""

from __future__ import annotations

import pytest

from src.ingestion.models import BOT_AUTHORS, estimate_tokens, is_bot_author


# ============================================================================
# estimate_tokens
# ============================================================================


def test_estimate_tokens_empty_string() -> None:
    assert estimate_tokens("") == 0


def test_estimate_tokens_none() -> None:
    assert estimate_tokens(None) == 0


def test_estimate_tokens_ascii() -> None:
    assert estimate_tokens("hello world") == len("hello world") // 4


def test_estimate_tokens_chinese_dominant() -> None:
    text = "中文内容abc"
    n = len(text)
    ascii_count = sum(1 for c in text if ord(c) < 128)
    assert ascii_count / n <= 0.8, "precondition: mostly non-ASCII"
    assert estimate_tokens(text) == n // 2


# ============================================================================
# is_bot_author
# ============================================================================


def test_is_bot_known_bot() -> None:
    assert is_bot_author("github-actions[bot]") is True


def test_is_bot_custom_bot_suffix() -> None:
    assert is_bot_author("some-custom-bot[bot]") is True


def test_is_bot_regular_user() -> None:
    assert is_bot_author("alice") is False


def test_is_bot_none() -> None:
    assert is_bot_author(None) is False


def test_is_bot_empty() -> None:
    assert is_bot_author("") is False


def test_bot_authors_set_is_nonempty() -> None:
    assert len(BOT_AUTHORS) >= 5
