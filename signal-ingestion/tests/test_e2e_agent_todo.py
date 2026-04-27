"""End-to-end Agent smoke tests — TODO.

These tests verify that an LLM Agent (via Cursor CLI headless or Claude Code)
can use the signals-db MCP tools to answer real business questions correctly.

Unlike test_mcp_dbhub.py (which tests JSON-RPC protocol correctness),
these tests validate **semantic correctness** of Agent-tool interaction:
  - Does the Agent pick the right tool for the question?
  - Does the Agent interpret the results correctly?
  - Does the Agent handle edge cases (empty results, ambiguous queries)?

Design decisions:
  - Execution: Cursor CLI ``--headless --print`` or ``echo "..." | claude --print``
  - Output: Agent is prompted to emit structured ``RESULT: key=value`` lines
  - Judgment: regex extraction + optional LLM-as-judge for fuzzy checks
  - Skip: all tests skip with descriptive TODO unless env var AGENT_E2E=1

Implementation plan (Week 2+):
  1. Pick execution runtime (Cursor headless vs Claude Code CLI)
  2. Write a ``run_agent(prompt) -> str`` helper
  3. Write a ``judge(agent_output, expected) -> bool`` helper
  4. Implement each case below
"""

# ═══════════════════════════════════════════════════════════════
# STATUS: TODO — all tests skip. Implementation blocked on:
#   1. Choosing runtime: Cursor CLI headless vs Claude Code CLI
#   2. Writing run_agent() helper with timeout + output capture
#   3. Writing judge() helper (regex + optional LLM-as-judge)
# Target: Week 2+ when Agent Workflow (Module 4) starts development
# ═══════════════════════════════════════════════════════════════

import pytest


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


@pytest.mark.e2e_agent
def test_e2e01_agent_search_aiter_mla():
    """E2E-01: Agent uses search_signals to find aiter MLA related issues.

    Prompt (copy-paste ready)::

        使用 signals-db 的 search_signals 工具搜索 'aiter MLA'。
        输出格式：RESULT: count=<数字>, first_title=<标题>

    Expected:
        - ``count >= 5`` (we know there are 11 hits in real DB)
        - ``first_title`` contains "aiter" or "MLA" (case-insensitive)

    Judgment:
        regex extract ``count`` and ``first_title``, assert ranges.

    Example CLI invocation::

        cursor --headless --print --trust --workspace . \\
          --prompt "使用 signals-db 的 search_signals 工具搜索 'aiter MLA'。\\
                    输出格式：RESULT: count=<数字>, first_title=<标题>" \\
          2>&1 | grep "RESULT:"
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e02_agent_detail_39303():
    """E2E-02: Agent uses get_signal_detail for issue #39303.

    Prompt::

        使用 signals-db 的 get_signal_detail 查看 github:vllm-project/vllm:issue:39303。
        这个 issue 是关于什么 bug？状态是什么？有多少条评论？
        RESULT: topic=<一句话>, state=<open/closed>, comments=<数字>

    Expected:
        - ``topic`` contains "aiter" or "MLA" or "sparse" (semantic match)
        - ``state == "closed"``
        - ``comments >= 10``

    Judgment:
        regex extract three fields; ``topic`` check is case-insensitive
        substring match against a set of known keywords.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e03_agent_competitive_analysis():
    """E2E-03: Agent answers a business question using multiple tools.

    Prompt::

        使用 signals-db 回答：vllm 仓库里，目前有多少 open 的 rocm issue？
        有多少 open 的 rocm PR？
        RESULT: open_issues=<数字>, open_prs=<数字>

    Expected:
        - ``open_issues > 10`` (we know ~80 open rocm signals,
          split between issue/PR)
        - ``open_prs > 20``
        - Agent should use ``execute_sql`` with appropriate WHERE clause

    Judgment:
        regex extract two integers; verify both are positive and
        in reasonable ranges. Optionally verify Agent's tool choice
        via output inspection.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e04_agent_empty_result():
    """E2E-04: Agent handles a query that returns no results gracefully.

    Prompt::

        使用 signals-db 搜索 'zyxwvutsrqp_nonexistent_term'。找到了多少条？
        RESULT: count=<数字>

    Expected:
        - ``count == 0``
        - Agent should NOT hallucinate results

    Judgment:
        regex extract ``count``; assert exactly 0. Scan full output
        for fabricated titles or descriptions as a hallucination check.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e05_agent_picks_right_tool():
    """E2E-05: Agent picks search_signals (not execute_sql) for a natural language query.

    Prompt::

        在 signals 数据库里搜索和 Eagle3 投机解码相关的内容。用最合适的工具。
        告诉我你用了哪个工具，找到了几条结果。
        RESULT: tool=<工具名>, count=<数字>

    Expected:
        - ``tool == "search_signals"`` (not ``execute_sql``)
        - ``count >= 1``

    Judgment:
        Agent should prefer the semantic search tool over raw SQL
        for a natural language query. Regex extract ``tool`` and
        ``count``; exact match on tool name.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e06_agent_cross_repo_comparison():
    """E2E-06: Agent compares AMD activity across vllm and sglang.

    Prompt::

        对比 vllm-project/vllm 和 sgl-project/sglang 两个仓库的 signal 数量。
        哪个更多？各有多少？
        RESULT: vllm=<数字>, sglang=<数字>, more=<vllm/sglang>

    Expected:
        - ``vllm > 2000``
        - ``sglang > 500``
        - ``more == "vllm"``

    Judgment:
        regex extract three fields; ``more`` must be consistent with
        the two counts. This tests multi-step reasoning: Agent needs
        two queries + comparison.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e07_agent_cannot_write():
    """E2E-07: Agent is prevented from writing to the database.

    Prompt::

        尝试在 signals 表里插入一条假数据。如果被拒绝，告诉我原因。
        RESULT: blocked=<yes/no>, reason=<原因>

    Expected:
        - ``blocked == "yes"``
        - ``reason`` contains "readonly" or "not allowed" or "read-only"

    Judgment:
        regex extract ``blocked`` and ``reason``; exact match on
        ``blocked``, substring match on ``reason`` against a set
        of acceptable phrases.
    """
    pytest.skip("TODO: implement Agent E2E — see docstring for design")


@pytest.mark.e2e_agent
def test_e2e08_llm_judge_semantic():
    """E2E-08: Use a second LLM call to judge whether the Agent's answer is correct.

    This is the most advanced test pattern — "LLM-as-judge":

    Step 1 — Agent prompt::

        使用 signals-db 分析 issue #39303。这个 bug 的根因是什么？
        影响了哪些硬件？修复方式是什么？

    Step 2 — Judge prompt (fed with Agent output + ground truth)::

        以下是 Agent 对 vllm issue #39303 的分析。
        已知事实：这个 bug 是 aiter sparse-MLA paged decode kernel 在
        context_len > 2048 时输出错误，影响 MI355X (gfx950)，
        通过 ROCm/aiter 版本回退修复。

        Agent 的回答是否基本正确？JUDGE: PASS 或 FAIL + 理由

    Expected:
        - ``JUDGE: PASS``

    Implementation notes:
        - Requires two LLM calls (Agent + Judge)
        - Judge can be a cheaper/faster model (e.g. claude-3-haiku)
        - Ground truth sourced from demo/signals/issue_39303.json
        - Consider caching Agent output to avoid redundant API calls
          when iterating on judge prompts
    """
    pytest.skip("TODO: LLM-as-judge pattern — most complex, implement last")


# ---------------------------------------------------------------------------
# Implementation helpers (TODO)
# ---------------------------------------------------------------------------


def run_agent(prompt: str, *, timeout_s: int = 120, runtime: str = "cursor") -> str:
    """Execute an Agent prompt and capture its stdout.

    Parameters
    ----------
    prompt : str
        The natural-language prompt to send to the Agent.
    timeout_s : int
        Max seconds to wait before killing the subprocess.
    runtime : str
        ``"cursor"`` for Cursor CLI headless, ``"claude"`` for Claude Code CLI.

    Returns
    -------
    str
        Full stdout/stderr from the Agent process.

    Implementation sketch::

        if runtime == "cursor":
            cmd = [
                "cursor", "--headless", "--print", "--trust",
                "--workspace", ".", "--prompt", prompt,
            ]
        elif runtime == "claude":
            cmd = ["bash", "-c", f'echo "{prompt}" | claude --print']

        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=timeout_s)
        return result.stdout + result.stderr
    """
    raise NotImplementedError("run_agent: blocked on runtime selection")


def judge(
    agent_output: str,
    expected: dict[str, str],
    *,
    mode: str = "regex",
) -> bool:
    """Judge whether the Agent's output satisfies expectations.

    Parameters
    ----------
    agent_output : str
        Raw stdout from ``run_agent()``.
    expected : dict[str, str]
        Mapping of field name to expected value/pattern.
        Values can be exact strings, regex patterns, or numeric ranges
        like ``">10"`` or ``">=5"``.
    mode : str
        ``"regex"`` — extract ``RESULT: key=value`` lines and compare.
        ``"llm"``  — send output + expected to a judge LLM for
        semantic evaluation (returns True if judge says PASS).

    Returns
    -------
    bool
        True if the Agent's answer satisfies all expectations.

    Implementation sketch::

        if mode == "regex":
            result_line = re.search(r"RESULT:\\s*(.+)", agent_output)
            pairs = dict(re.findall(r"(\\w+)=([^,]+)", result_line.group(1)))
            for key, pattern in expected.items():
                if not _match(pairs[key], pattern):
                    return False
            return True

        elif mode == "llm":
            judge_prompt = f"Agent output:\\n{agent_output}\\n\\n"
            judge_prompt += f"Expected: {expected}\\n"
            judge_prompt += "Is the answer correct? Reply JUDGE: PASS or FAIL"
            resp = call_llm(judge_prompt)
            return "JUDGE: PASS" in resp
    """
    raise NotImplementedError("judge: blocked on runtime selection")
