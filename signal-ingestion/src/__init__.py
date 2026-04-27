"""AI Infra Gap Intelligence — Module 1 (Signal Ingestion) + Module 3 (Infra).

Tracks AMD vs NVIDIA capability gaps in open-source LLM infrastructure repos.

接口版本控制
----------
下游模块可 pin 接口版本，确保上游不会偷偷 break 你的代码::

    from src import INTERFACE_VERSION, INTERFACE_CONTRACT
    assert INTERFACE_VERSION == "1.0", f"unexpected interface v{INTERFACE_VERSION}"

INTERFACE_CONTRACT 描述了每个核心方法的返回字段和边界行为约定。
当我们做 breaking change 时会 bump INTERFACE_VERSION，并在 CHANGELOG 里说明。
"""

__version__ = "0.1.0"

INTERFACE_VERSION = "1.0"

INTERFACE_CONTRACT = {
    "get_feed": {
        "returns_fields": [
            "signal_id", "source_type", "source_url", "source_repo", "source_number",
            "title", "author", "created_at", "updated_at", "last_synced_at", "version",
            "tags", "body_preview", "body_token_estimate",
            "github_state", "github_labels", "github_comment_count", "github_is_pr",
            "gap_ids", "recent_changes",
        ],
        "none_on_not_found": False,
        "empty_result": {
            "signals": [],
            "pagination": {
                "total": 0,
                "returned": 0,
                "next_cursor": None,
                "has_more": False,
            },
        },
    },
    "get_detail": {
        "none_on_not_found": True,
    },
    "get_changes": {
        "empty_on_not_found": True,
    },
    "update_classification": {
        "false_on_not_found": True,
    },
}
