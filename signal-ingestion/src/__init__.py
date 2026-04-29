"""AI Infra Gap Intelligence — Module 1 (Signal Ingestion) + Module 3 (Infra).

Tracks AMD vs NVIDIA capability gaps in open-source LLM infrastructure repos.

Interface Versioning
--------------------
Downstream modules can pin the interface version to ensure upstream
changes do not silently break your code::

    from src import INTERFACE_VERSION, INTERFACE_CONTRACT
    assert INTERFACE_VERSION == "1.0", f"unexpected interface v{INTERFACE_VERSION}"

INTERFACE_CONTRACT describes the return fields and boundary-behavior
contract of each core method. We bump INTERFACE_VERSION on breaking
changes and document them in the CHANGELOG.
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
