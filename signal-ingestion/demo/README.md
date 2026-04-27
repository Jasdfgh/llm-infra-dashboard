# Demo: 纵向穿刺 — AITER MLA Sparse Regression

> 2026-04-22 | 用于 15:00 会议展示

## 这个 Demo 展示什么

以 vLLM 的一个真实 bug（issue #39303）为例，展示 Agent 智能爬虫的完整纵向能力：

1. **数据链** — 从 GitHub 搜索到最终分析的每一步都有来源
2. **推理链** — 从观察 → 分析 → 战略判断，三层递进，每层引用上层
3. **跟踪效果** — 一个 bug 从"发现"到"修复确认"的完整生命周期

## 被追踪的 Gap

| 字段 | 值 |
|---|---|
| Bug | AITER MLA sparse kernel 在 MI355X 上 context > 2048 时输出错误 |
| 严重性 | High — 影响所有 GLM-5 系列模型的长上下文推理 |
| Tracking Issue | [vllm-project/vllm#39303](https://github.com/vllm-project/vllm/issues/39303) |
| 硬件 | 8x AMD Instinct MI355X (gfx950) |

## 数据链（4 步，12 次 API 调用）

```
Step 1: get_issue(#39303) → issue closed, 11 comments
Step 2: fetch_comments(#39303) → 完整讨论时间线
Step 3: get_pull_request(#39509) → fix PR: aiter 版本回退
Step 4: search_issues(AITER MLA merged:>4/14) → 3 个后续相关 PR
```

## 时间线

```
04-03  旧 nightly 正常 ─────────── regression 窗口开始
04-08  ghpu 报告 issue #39303 ── bug reported（附完整复现脚本）
       │  hongxiayang 开始响应 ── triage（2小时内）
       │  ghpu 确认根因 ────────── aiter 0.1.10 → 0.1.12 regression
04-10  tjtanaa 提交 PR #39509 ── fix PR merged（报告后 2 天）
       │  策略：回退 aiter 到 v0.1.10.post3
04-14  hongxiayang 请验证 ────── verification requested
04-16  ghpu 确认修复 ────────── closed（全流程 8 天）
04-20  后续 3 个 MLA PR 合并 ── Eagle3+MLA(+73%), RMS norm, hotfix
```

## 推理链（三层递进）

### Layer 1: Observations（观察 — 来自 Worker 产出的事实）
- **obs_1**: Issue closed on 4/16, reporter confirmed fix
- **obs_2**: Fix 是版本回退，不是 kernel bug 正面修复
- **obs_3**: 上游 aiter v0.1.12 tag 不稳定，有新 kernel bug #2720
- **obs_4**: AMD 团队一周内合并 3 个 MLA 相关 PR

### Layer 2: Analyses（分析 — 对事实的解读）
- **ana_1** (引用 obs_1, obs_2): Bug 通过回退绕过，不是根治。aiter 升级时可能复现。
- **ana_2** (引用 obs_3, obs_4): AMD MLA 方向势头好（3 PRs/周），但 aiter 上游稳定性是系统性风险。

### Layer 3: Strategic Judgments（战略判断 — 给领导的建议）
- **judge_1** (引用 ana_1): 此 gap 降级为 resolved-with-caveat，监控 aiter 升级
- **judge_2** (引用 ana_2): 建议新增追踪项 "AITER Upstream Stability"

### 一句话总结
> AITER MLA sparse regression 已关闭（8天），但修复是版本回退非根治；AMD MLA 投入强劲（3 PRs/周），但 aiter 上游稳定性是新的系统性风险。

## 竞争力评估

| 维度 | 数据 |
|---|---|
| AMD 响应速度 | Bug report → Fix PR: 2 天 | Fix PR → User confirmed: 8 天 |
| AMD MLA 投入 | 本周 3 个 PR 合并：Eagle3+MLA (+73% 吞吐), RMS norm 融合, 兼容性热修复 |
| 风险 | aiter 上游 v0.1.12 不稳定（tag 漂移 + kernel bug #2720） |

## 技术栈

- **Agent runtime**: Cursor CLI headless
- **数据获取**: GitHub MCP (search_issues, get_issue, get_pull_request) + Fetch MCP (issue comments API)
- **输出**: 结构化 JSON (`demo/gap_analysis_39303.json`)
- **推理链**: 三层结构 (observations → analyses → strategic_judgments)，每层 ID 引用，全链路可审计
