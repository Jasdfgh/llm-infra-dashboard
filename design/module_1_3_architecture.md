# Module 1 & 3 — Signal Ingestion + Central Storage 完整技术架构

> 版本: v2.0  
> 日期: 2026-04-23  
> 作者: Wang, Yayuan  
> 状态: **v2.0 — 融合会议决策 + 数据库调研**  
> 覆盖: Module 1 (Signal Ingestion) + Module 3 (Infra: Storage + Retrieval + MCP Service)

---

## 目录

- [D0. 会议决策记录 (4/22)](#d0-会议决策记录-422)
- [D1. 数据流图](#d1-数据流图)
- [EX. 实验验证](#ex-实验验证)
- [D2. 核心数据模型](#d2-核心数据模型)
- [D3. API/接口定义](#d3-api接口定义)
- [D4. 增量更新流程](#d4-增量更新流程)
- [D5. 变更检测算法](#d5-变更检测算法)
- [D6. 前向扩展指南](#d6-前向扩展指南)
- [D7. 核心设计问题 C1-C7 回答](#d7-核心设计问题-c1-c7-回答)
- [D8. MVP 4/24 交付清单](#d8-mvp-424-交付清单)
- [D9. 数据库升级路径](#d9-数据库升级路径)

---

## D0. 会议决策记录 (4/22)

> 4/22 全体会议确认的关键决策，直接影响架构设计方向。

| # | 议题 | 决策 | 架构影响 |
|---|---|---|---|
| 1 | 存储格式 | 团队完全自主。Vincent 认为 SQLite 未来需要升级到更好的 DB，但 MVP 可以先用 | MVP 继续用 SQLite；新增 D9 数据库升级路径 |
| 2 | Signal 格式 | 先做出来，Vivi（Module 2）后面再看 | D2 Signal Envelope 不变，交付后由 Module 2 审阅 |
| 3 | 增量接口 | Pull 模式确认 OK | D3.1 feed API 设计不变 |
| 4 | GitHub MCP | 完全自主升级，从 `@modelcontextprotocol/server-github`（26 工具）切换到 `github/github-mcp-server`（40+ 工具，含 issue comments、list_tags） | 新增 D3.2½ GitHub MCP 升级计划 |
| 5 | X/Twitter | 未来做，MVP 不做 | 附录 C（Twitter 设计）保留作为参考，MVP 不实现 |
| 6 | Cron 调度 | 完全自主 | D4 Scheduler 设计不变 |
| 7 | API 限制扩展 | 注册多个 GitHub App / 账号，每小时轮换 token | 新增 D6.3 API 限制扩展策略 |

---

## D1. 数据流图

### 全景数据流

```
┌──────────────────────── DATA SOURCES ────────────────────────┐
│                                                               │
│  GitHub REST API          X/Twitter API       ArXiv/Zhihu/Blogs
│  (httpx 直调)             (twitter MCP)       (fetch MCP / httpx)
│  ┌─────────────┐         ┌──────────┐        ┌──────────────┐ │
│  │ list_issues  │         │ search   │        │ RSS/API fetch│ │
│  │ get_issue    │         │ get_tweet│        │ HTML parse   │ │
│  │ get_comments │         └────┬─────┘        └──────┬───────┘ │
│  │ search_issues│              │                     │         │
│  └──────┬───────┘              │                     │         │
└─────────┼──────────────────────┼─────────────────────┼─────────┘
          │ JSON                 │ JSON                │ JSON
          ▼                      ▼                     ▼
┌─────────────────── MODULE 1: SIGNAL INGESTION ───────────────┐
│                                                               │
│  ┌─────────────────────────────────────────────────────────┐  │
│  │  Source Adapter Layer                                    │  │
│  │  GitHubAdapter | TwitterAdapter | ArxivAdapter | ...    │  │
│  │  每个 adapter 实现: discover() + fetch_detail()          │  │
│  │  + fetch_comments() + make_signal_id()                  │  │
│  └──────────────────────┬──────────────────────────────────┘  │
│                         │ RawSignal                            │
│  ┌──────────────────────▼──────────────────────────────────┐  │
│  │  Normalizer                                              │  │
│  │  RawSignal → Signal envelope (统一 JSON schema)          │  │
│  │  - 字段映射                                               │  │
│  │  - body token 估算                                        │  │
│  │  - 引用提取 (regex: #NNNN, URLs, @mentions)              │  │
│  │  - content_hash 计算                                      │  │
│  └──────────────────────┬──────────────────────────────────┘  │
│                         │ Signal                               │
│  ┌──────────────────────▼──────────────────────────────────┐  │
│  │  Change Detector                                         │  │
│  │  - DB lookup by signal_id                                │  │
│  │  - content_hash 对比                                      │  │
│  │  - 逐字段 diff → ChangeEvent[]                            │  │
│  │  - 有意义变更 vs 噪音变更 过滤                             │  │
│  └──────────────────────┬──────────────────────────────────┘  │
│                         │ Signal + ChangeEvent[]               │
│  ┌──────────────────────▼──────────────────────────────────┐  │
│  │  Scheduler + Rate Limiter                                │  │
│  │  - 全局 token bucket (Search 30/min, REST 5000/hr)       │  │
│  │  - ETag 缓存减少无效调用                                   │  │
│  │  - 按 repo 优先级分层调度                                  │  │
│  │  - sync_runs 记录每次同步状态                              │  │
│  └──────────────────────┬──────────────────────────────────┘  │
└──────────────────────────┼────────────────────────────────────┘
                           │ Normalized Signal + Changes
                           ▼
┌──────────────── MODULE 3: CENTRAL STORAGE ───────────────────┐
│                                                               │
│  ┌─────────────────── SQLite (signals.db) ──────────────────┐│
│  │                                                           ││
│  │  signals          ← 主表，所有源的标准化信号               ││
│  │  signal_comments  ← 评论/回复（GitHub comments 等）       ││
│  │  signal_changes   ← 变更审计日志（append-only）           ││
│  │  signal_refs      ← 跨信号关联（tweet→issue 等）          ││
│  │  sync_runs        ← 同步运行记录                          ││
│  │  etag_cache       ← HTTP ETag 缓存                        ││
│  │                                                           ││
│  │  signals_fts      ← FTS5 全文搜索虚拟表                   ││
│  │  idx_signals_repo_updated  ← B-tree 复合索引              ││
│  │  idx_signals_source_type   ← 源类型索引                   ││
│  │  idx_signals_tags          ← 标签索引（辅助）             ││
│  │                                                           ││
│  └───────────────────────────────────────────────────────────┘│
│                                                               │
│  ┌──────────────── JSON File Cache ─────────────────────────┐│
│  │  data/cache/{source_type}/{repo_slug}/{number}.json       ││
│  │  完整 Signal + Comments 的 JSON dump                      ││
│  │  用途：Agent context 注入、调试、离线分析                  ││
│  └───────────────────────────────────────────────────────────┘│
│                                                               │
│  ┌──────────────── Retrieval Layer ─────────────────────────┐│
│  │  SignalRepository                                         ││
│  │  - search(query, repos, labels, since, until, ...)       ││
│  │  - get_detail(signal_id, include_comments=True)          ││
│  │  - get_changes(signal_id, since)                         ││
│  │  - get_feed(since, classified, limit)  [→ Module 2]      ││
│  │  - get_stats(group_by, repos, since)   [→ Module 5-6]   ││
│  │                                                           ││
│  │  内部路由：FTS5 + B-tree + label filter + repo filter     ││
│  │  自动选择最优索引组合                                      ││
│  └──────────────────────┬────────────────────────────────────┘│
│                         │                                     │
│  ┌──────────────────────▼────────────────────────────────────┐│
│  │  MCP Service Layer                                        ││
│  │                                                           ││
│  │  自定义 MCP Tools (for Module 4 Agent):                   ││
│  │  - search_signals()   组合搜索                            ││
│  │  - get_signal_detail() 单信号详情                         ││
│  │  - get_signal_changes() 变更历史                          ││
│  │  - get_gap_signals()  关联 gap 的所有信号                 ││
│  │                                                           ││
│  │  SQLite MCP (只读，for Coordinator Agent ad-hoc):         ││
│  │  - 直接 SQL 查询，用于非标准化的探索式分析                 ││
│  └───────────────────────────────────────────────────────────┘│
└───────────────────────────┬───────────────────────────────────┘
                            │
              ┌─────────────┼──────────────┐
              ▼             ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────────┐
│  Module 2    │ │  Module 4    │ │  Module 5-6      │
│  Classifier  │ │  Agent       │ │  Dashboard       │
│              │ │  Workflow    │ │                  │
│ 增量 feed    │ │ MCP tools   │ │  REST API        │
│ HTTP pull    │ │ + SQL MCP   │ │  (聚合/时间线)    │
└──────────────┘ └──────────────┘ └──────────────────┘
```

### 每一步的数据格式和工具

| 步骤 | 输入 | 工具/API | 输出 | 存储位置 |
|---|---|---|---|---|
| GitHub 发现扫描 | repo slug + labels | `httpx → GET /repos/{o}/{r}/issues?labels=rocm&since=...` | `list[RawGitHubIssue]` | 内存 |
| GitHub 搜索扫描 | search query | `httpx → GET /search/issues?q=...` | `list[RawGitHubIssue]` | 内存 |
| GitHub 深度获取 | issue number | `httpx → GET /repos/{o}/{r}/issues/{n}` | `RawGitHubIssue` | 内存 |
| GitHub 评论获取 | issue number | `httpx → GET /repos/{o}/{r}/issues/{n}/comments` | `list[RawComment]` | 内存 |
| 标准化 | `RawGitHubIssue` | `Normalizer.normalize()` | `Signal` (JSON envelope) | 内存 |
| 引用提取 | Signal.body + Comments | `ReferenceExtractor.extract()` | `list[Reference]` | Signal.references_json |
| 变更检测 | Signal + DB lookup | `ChangeDetector.detect()` | `list[ChangeEvent]` | signal_changes 表 |
| 持久化 | Signal + Comments + Changes | `SignalRepository.upsert()` | — | signals.db + JSON cache |
| FTS 同步 | Signal (title + body + tags) | SQLite FTS5 trigger | — | signals_fts 虚拟表 |
| 下游通知 | — | 增量 feed API (pull 模式) | — | Module 2 按需拉取 |

---

## EX. 实验验证

> 基于 vllm-project/vllm 和 ROCm/aiter 的真实数据，验证采集 → 清洗 → 标准化管线的可行性。

### EX.1 量化数据

| 信号类型 | 原始大小 | 清洗后大小 | 压缩比 | 文件路径 |
|---|---|---|---|---|
| Issue #39303（深度） | 16.3 KB | 13.2 KB | 19% | `demo/signals/issue_39303.json` |
| PR #39616（深度） | 13.1 KB | 9.4 KB | 28% | `demo/signals/pr_39616.json` |
| Discovery batch（20 条） | 157.7 KB | 10.3 KB | **93.4%** | `demo/signals/discovery_vllm_rocm.json` |

### EX.2 关键发现

1. **批量发现扫描压缩比 93.4%**——绝大部分噪音来自 avatar URL、GitHub App permissions、pagination metadata 等字段。标准化后只保留语义相关内容。
2. **PR 自动文件分类成功**——PR #39616 的 changed files 被正确分类为 `rocm_specific` vs `shared`，可供 Module 2 直接使用。
3. **引用提取正确工作**——从 PR body 成功提取 `ROCm/aiter#2720` 和 HuggingFace 模型链接 (`https://huggingface.co/amd/Kimi-K2.5-MXFP4`)。
4. **私有仓库边界确认**——`ROCm/aiter` 是私有仓库，GitHub Search API 返回 422 Unprocessable Entity。已在附录 A 的私有仓库处理策略中覆盖。
5. **噪音关键词警告**——泛搜关键词 `hip`（HIP = Heterogeneous-Compute Interface for Portability）是严重噪音源，匹配到大量无关 issue。需要在 `tracking_config.yaml` 中用 `keyword_scope: "title"` 或 `exclude_keywords` 限制。

### EX.3 对架构验证的影响

| 验证点 | 结论 | 架构影响 |
|---|---|---|
| 数据清洗有效性 | 93.4% 压缩比证明清洗管线必要且有效 | Normalizer 设计确认 |
| 引用提取覆盖度 | 跨仓库引用 + HuggingFace URL 都能提取 | ReferenceExtractor regex 覆盖足够 |
| 私有仓库限制 | Search API 不可用，REST API 需 repo scope token | tracking_config 需标记 `access: private` |
| 关键词噪音 | 单字通用词不适合全文搜索 | 搜索配置需加噪音过滤规则 |

---

## D2. 核心数据模型

### D2.1 Signal Envelope JSON Schema

```jsonc
{
  // ── 全局标识 ──
  "signal_id": "github:vllm-project/vllm:issue:39303",
  // 格式: {source_type_prefix}:{source_specific_id}
  // github: github:{repo}:{issue|pr}:{number}
  // twitter: twitter:{tweet_id}
  // arxiv: arxiv:{paper_id}  (e.g. arxiv:2406.12345)
  // blog: blog:{sha256(canonical_url)[:16]}
  // zhihu: zhihu:{answer_id|article_id}

  "source_type": "github_issue",
  // enum: github_issue | github_pr | tweet | arxiv_paper | blog_post | zhihu_post

  "source_url": "https://github.com/vllm-project/vllm/issues/39303",
  "source_repo": "vllm-project/vllm",   // null for non-GitHub
  "source_number": 39303,                // null for non-GitHub

  // ── 通用内容字段 ──
  "title": "[Bug]: aiter.ops... returns random topk...",
  "body": "## Summary\nInvestigations done with Claude...",  // 完整 body，不截断
  "body_token_estimate": 3020,            // tiktoken cl100k_base 估算
  "author": "ghpu",
  "created_at": "2026-04-08T13:27:58Z",  // 源平台的创建时间
  "updated_at": "2026-04-16T14:26:57Z",  // 源平台的最后更新时间

  // ── 系统元数据 ──
  "first_seen_at": "2026-04-22T10:00:00Z",  // 首次采集时间
  "last_synced_at": "2026-04-22T14:00:00Z", // 最近一次同步时间
  "content_hash": "a7f3b2c1d4e5...",         // SHA-256 of meaningful fields
  "version": 3,                               // 单调递增，每次有意义变更 +1
  "sync_run_id": "sync_vllm-project_vllm_20260422_140000",

  // ── 引用提取 ──
  "references": {
    "github_issues": [
      {"repo": "vllm-project/vllm", "number": 39300, "raw": "#39300"}
    ],
    "github_prs": [
      {"repo": "vllm-project/vllm", "number": 39616, "raw": "#39616"}
    ],
    "external_urls": [
      "https://huggingface.co/amd/Kimi-K2.5-MXFP4"
    ],
    "mentions": ["@hongxiayang"]
  },

  // ── 标签（聚合源：GitHub labels + 自动提取的关键词）──
  "tags": ["rocm", "bug", "aiter", "mla", "gfx950", "mi355x"],

  // ── GitHub 特有字段（其他源为 null）──
  "github": {
    "state": "closed",               // open | closed
    "labels": ["bug", "rocm"],       // 原始 GitHub labels
    "assignees": ["hongxiayang"],
    "comment_count": 11,
    "closed_at": "2026-04-16T14:26:57Z",
    "closed_by": "hongxiayang",
    "is_pr": false,
    "pr_merged": null,
    "pr_merged_at": null,
    "pr_changed_files": null,         // PR only: {total, additions, deletions, files[]}
    "pr_review_comments": null        // PR only: [{author, body_preview, created_at}]
  },

  // ── Twitter 特有字段 ──
  "twitter": null,
  // 非 null 时:
  // {
  //   "tweet_id": "1234567890",
  //   "likes": 42,
  //   "retweets": 15,
  //   "replies": 3,
  //   "author_followers": 12000,
  //   "is_thread": false,
  //   "thread_position": null,
  //   "media_urls": []
  // }

  // ── ArXiv 特有字段 ──
  "arxiv": null,
  // 非 null 时:
  // {
  //   "paper_id": "2406.12345",
  //   "authors": ["Author A", "Author B"],
  //   "categories": ["cs.LG", "cs.AI"],
  //   "abstract": "...",
  //   "pdf_url": "https://arxiv.org/pdf/2406.12345"
  // }

  // ── Module 2 填写（采集时为 null）──
  "classification": null,
  // 非 null 时: {
  //   "classified_at": "...",
  //   "gap_ids": ["gap_001", "gap_002"],
  //   "signal_category": "amd_gap | amd_fix | nvidia_advantage | ...",
  //   "confidence": 0.85,
  //   "classifier_version": "v1"
  // }
}
```

### D2.2 SQLite DDL

```sql
-- ============================================================
-- signals.db — Module 1+3 中心存储
-- ============================================================

PRAGMA journal_mode = WAL;          -- 并发读写
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;         -- 5s 等待锁

-- ┌──────────────────────────────────────────────────────────┐
-- │ signals — 主表：所有源的标准化信号                        │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS signals (
    -- 主键：SQLite 隐式 rowid 用于 FTS5 content 表关联
    rowid         INTEGER PRIMARY KEY,
    signal_id     TEXT    NOT NULL UNIQUE,

    -- 源标识
    source_type   TEXT    NOT NULL,  -- github_issue|github_pr|tweet|arxiv_paper|blog_post|zhihu_post
    source_url    TEXT    NOT NULL,
    source_repo   TEXT,              -- owner/repo (GitHub only)
    source_number INTEGER,           -- issue/PR number (GitHub only)

    -- 通用内容
    title         TEXT    NOT NULL,
    body          TEXT,
    body_token_estimate INTEGER DEFAULT 0,
    author        TEXT,
    created_at    TEXT    NOT NULL,   -- ISO 8601 UTC
    updated_at    TEXT    NOT NULL,

    -- 系统元数据
    first_seen_at   TEXT  NOT NULL,
    last_synced_at  TEXT  NOT NULL,
    content_hash    TEXT  NOT NULL,
    version         INTEGER DEFAULT 1,
    sync_run_id     TEXT,

    -- 引用 (JSON)
    references_json TEXT,            -- {"github_issues":[], "github_prs":[], ...}

    -- 聚合标签 (JSON array)
    tags            TEXT,            -- ["rocm","bug","aiter"]

    -- GitHub 特有 (JSON object, null for non-GitHub)
    github_json     TEXT,
    -- 为避免 JSON 查询性能问题，高频过滤字段提升为物理列：
    github_state    TEXT,            -- open|closed (冗余，方便索引)
    github_labels   TEXT,            -- JSON array of label names
    github_is_pr    INTEGER DEFAULT 0,
    github_comment_count INTEGER DEFAULT 0,

    -- Twitter 特有 (JSON object)
    twitter_json    TEXT,

    -- ArXiv 特有 (JSON object)
    arxiv_json      TEXT,

    -- Module 2 分类结果 (JSON object)
    classification_json TEXT,
    gap_ids         TEXT             -- JSON array: ["gap_001", "gap_002"]
);

-- 四种确定性索引
-- 1. 空间索引：按 repo 过滤 + 时间排序
CREATE INDEX IF NOT EXISTS idx_signals_repo_updated
    ON signals(source_repo, updated_at DESC);

-- 2. 时间索引：日期范围查询
CREATE INDEX IF NOT EXISTS idx_signals_synced
    ON signals(last_synced_at DESC);

CREATE INDEX IF NOT EXISTS idx_signals_created
    ON signals(created_at DESC);

-- 3. 逻辑索引：源类型 + 状态
CREATE INDEX IF NOT EXISTS idx_signals_type_state
    ON signals(source_type, github_state);

-- 4. 增量 feed 索引（Module 2 消费用）
CREATE INDEX IF NOT EXISTS idx_signals_feed
    ON signals(last_synced_at ASC)
    WHERE classification_json IS NULL;

-- 去重辅助索引
CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_dedup
    ON signals(source_type, source_repo, source_number)
    WHERE source_number IS NOT NULL;


-- ┌──────────────────────────────────────────────────────────┐
-- │ signals_fts — FTS5 全文搜索（物理索引）                   │
-- └──────────────────────────────────────────────────────────┘
-- content= 模式：数据存在 signals 表，FTS5 只存索引
-- 需要手动同步（通过 trigger 或代码层 after insert/update）
CREATE VIRTUAL TABLE IF NOT EXISTS signals_fts USING fts5(
    title,
    body,
    tags,
    content=signals,
    content_rowid=rowid,
    tokenize='unicode61 remove_diacritics 2'
);

-- FTS5 同步 triggers
-- INSERT 时
CREATE TRIGGER IF NOT EXISTS signals_ai AFTER INSERT ON signals BEGIN
    INSERT INTO signals_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;

-- UPDATE 时
CREATE TRIGGER IF NOT EXISTS signals_au AFTER UPDATE ON signals BEGIN
    INSERT INTO signals_fts(signals_fts, rowid, title, body, tags)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags);
    INSERT INTO signals_fts(rowid, title, body, tags)
    VALUES (new.rowid, new.title, new.body, new.tags);
END;

-- DELETE 时
CREATE TRIGGER IF NOT EXISTS signals_ad AFTER DELETE ON signals BEGIN
    INSERT INTO signals_fts(signals_fts, rowid, title, body, tags)
    VALUES ('delete', old.rowid, old.title, old.body, old.tags);
END;


-- ┌──────────────────────────────────────────────────────────┐
-- │ signal_comments — 评论/回复                               │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS signal_comments (
    id              INTEGER PRIMARY KEY,
    signal_id       TEXT    NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    comment_id      TEXT    NOT NULL,   -- 平台原生 ID (GitHub comment ID)
    author          TEXT,
    body            TEXT,
    body_token_estimate INTEGER DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT,
    is_bot          INTEGER DEFAULT 0,  -- 1 = bot comment

    UNIQUE(signal_id, comment_id)
);

CREATE INDEX IF NOT EXISTS idx_comments_signal
    ON signal_comments(signal_id, created_at ASC);


-- ┌──────────────────────────────────────────────────────────┐
-- │ signal_changes — 变更审计日志（append-only）              │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS signal_changes (
    id              INTEGER PRIMARY KEY,
    signal_id       TEXT    NOT NULL REFERENCES signals(signal_id) ON DELETE CASCADE,
    change_type     TEXT    NOT NULL,
    -- enum: new_signal | state_change | label_change | new_comment
    --     | body_edit | assignee_change | pr_linked | pr_merged
    --     | closed | reopened | comment_count_change

    changed_at      TEXT    NOT NULL,   -- 变更发生的时间（源平台时间）
    detected_at     TEXT    NOT NULL,   -- 我们检测到的时间
    old_value       TEXT,               -- JSON: 变更前的值
    new_value       TEXT,               -- JSON: 变更后的值
    is_meaningful   INTEGER DEFAULT 1,  -- 0 = 噪音变更（bot comment 等）
    sync_run_id     TEXT
);

CREATE INDEX IF NOT EXISTS idx_changes_signal
    ON signal_changes(signal_id, detected_at DESC);

CREATE INDEX IF NOT EXISTS idx_changes_meaningful
    ON signal_changes(detected_at DESC)
    WHERE is_meaningful = 1;


-- ┌──────────────────────────────────────────────────────────┐
-- │ signal_refs — 跨信号关联                                  │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS signal_refs (
    id              INTEGER PRIMARY KEY,
    from_signal_id  TEXT    NOT NULL,
    to_signal_id    TEXT,               -- null if target not yet ingested
    to_url          TEXT    NOT NULL,   -- 原始 URL/引用文本
    ref_type        TEXT    NOT NULL,   -- mentions | fixes | related | upstream | downstream
    created_at      TEXT    NOT NULL,

    UNIQUE(from_signal_id, to_url)
);

CREATE INDEX IF NOT EXISTS idx_refs_from
    ON signal_refs(from_signal_id);

CREATE INDEX IF NOT EXISTS idx_refs_to
    ON signal_refs(to_signal_id)
    WHERE to_signal_id IS NOT NULL;


-- ┌──────────────────────────────────────────────────────────┐
-- │ sync_runs — 同步运行记录                                  │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS sync_runs (
    id              TEXT    PRIMARY KEY,
    -- 格式: sync_{repo_slug}_{YYYYMMDD_HHMMSS}
    -- 或: sync_twitter_{YYYYMMDD_HHMMSS}

    source_type     TEXT    NOT NULL,   -- github | twitter | arxiv | blog | zhihu
    source_repo     TEXT,               -- GitHub only
    sync_mode       TEXT    NOT NULL,   -- full | incremental | targeted | discovery
    started_at      TEXT    NOT NULL,
    completed_at    TEXT,
    status          TEXT    NOT NULL,   -- running | completed | partial | failed

    -- 统计
    signals_total     INTEGER DEFAULT 0,
    signals_created   INTEGER DEFAULT 0,
    signals_updated   INTEGER DEFAULT 0,
    signals_unchanged INTEGER DEFAULT 0,
    comments_fetched  INTEGER DEFAULT 0,
    api_calls_used    INTEGER DEFAULT 0,

    -- 错误和恢复
    error_message   TEXT,
    resume_token    TEXT,               -- JSON: 断点续传信息
    -- GitHub: {"last_page": 3, "last_since": "2026-04-22T10:00:00Z"}
    -- Twitter: {"last_tweet_id": "1234567890"}

    config_snapshot TEXT                -- JSON: 本次同步使用的配置快照
);

CREATE INDEX IF NOT EXISTS idx_sync_runs_repo
    ON sync_runs(source_repo, started_at DESC);


-- ┌──────────────────────────────────────────────────────────┐
-- │ etag_cache — HTTP 条件请求缓存                            │
-- └──────────────────────────────────────────────────────────┘
CREATE TABLE IF NOT EXISTS etag_cache (
    url             TEXT    PRIMARY KEY,
    etag            TEXT,
    last_modified   TEXT,
    cached_at       TEXT    NOT NULL
);
```

### D2.3 Change Event JSON Schema

```jsonc
{
  "id": 42,                              // auto-increment
  "signal_id": "github:vllm-project/vllm:issue:39303",
  "change_type": "state_change",
  // enum: new_signal | state_change | label_change | new_comment
  //     | body_edit | assignee_change | pr_linked | pr_merged
  //     | closed | reopened

  "changed_at": "2026-04-16T14:26:57Z",  // 源平台时间
  "detected_at": "2026-04-22T14:00:00Z", // 我们检测到的时间

  "old_value": "\"open\"",
  "new_value": "\"closed\"",

  "is_meaningful": true,
  // 判断规则:
  //   meaningful: state_change, label_change(非bot), new_comment(非bot),
  //              body_edit(>50 chars diff), pr_linked, pr_merged
  //   noise:     new_comment(bot), assignee_change, body_edit(<50 chars)

  "sync_run_id": "sync_vllm-project_vllm_20260422_140000"
}
```

### D2.4 Sync Run Record Schema

```jsonc
{
  "id": "sync_vllm-project_vllm_20260422_140000",
  "source_type": "github",
  "source_repo": "vllm-project/vllm",
  "sync_mode": "incremental",
  "started_at": "2026-04-22T14:00:00Z",
  "completed_at": "2026-04-22T14:02:35Z",
  "status": "completed",

  "signals_total": 45,
  "signals_created": 3,
  "signals_updated": 8,
  "signals_unchanged": 34,
  "comments_fetched": 22,
  "api_calls_used": 52,

  "error_message": null,
  "resume_token": null,

  "config_snapshot": {
    "labels_filter": ["rocm"],
    "since": "2026-04-22T12:00:00Z",
    "include_comments": true,
    "max_comments_per_issue": 100,
    "deep_scan_threshold": 5
  }
}
```

---

## D3. API/接口定义

### D3.1 给 Module 2（Signal Classifier）的增量查询接口

**设计原则**：Pull 模式。Module 2 定期来拉"自上次以来有什么新的/变化的"。

```
GET /api/v1/signals/feed
```

**参数**：

| 参数 | 类型 | 必须 | 说明 |
|---|---|---|---|
| `since` | ISO 8601 | 是 | 起始时间戳（上次拉取时间） |
| `classified` | bool\|null | 否 | `false`=只返回未分类的，`true`=只返回已分类的，`null`=返回全部，默认 `false` |
| `source_types` | string[] | 否 | 过滤源类型，默认全部 |
| `limit` | int | 否 | 每页数量，默认 100，最大 500 |
| `cursor` | string | 否 | 分页游标（上次返回的 `next_cursor`） |

**返回**：

```jsonc
{
  "signals": [
    {
      "signal_id": "github:vllm-project/vllm:issue:39303",
      "source_type": "github_issue",
      "source_url": "https://github.com/vllm-project/vllm/issues/39303",
      "source_repo": "vllm-project/vllm",
      "title": "[Bug]: aiter.ops... returns random topk...",
      "author": "ghpu",
      "created_at": "2026-04-08T13:27:58Z",
      "updated_at": "2026-04-16T14:26:57Z",
      "last_synced_at": "2026-04-22T14:00:00Z",
      "version": 3,
      "tags": ["rocm", "bug", "aiter"],

      // 摘要（不是全文，省 token）
      "body_preview": "## Summary\nInvestigations done with Claude Code on a mi355 mode...",
      "body_token_estimate": 3020,

      // 自上次以来的变更
      "recent_changes": [
        {
          "change_type": "new_comment",
          "changed_at": "2026-04-16T12:00:00Z",
          "summary": "hongxiayang 确认 bug 已在 aiter v0.9.3 修复"
        }
      ],

      // GitHub 关键字段（不是全部，够分类用）
      "github_state": "closed",
      "github_labels": ["bug", "rocm"],
      "github_comment_count": 11,
      "github_is_pr": false
    }
    // ... more signals
  ],
  "pagination": {
    "total": 42,
    "returned": 42,
    "next_cursor": null,         // null = no more pages
    "has_more": false
  },
  "meta": {
    "query_since": "2026-04-22T12:00:00Z",
    "query_time_ms": 23
  }
}
```

**Module 2 回写分类结果**：

```
PUT /api/v1/signals/{signal_id}/classification
```

Body:
```jsonc
{
  "gap_ids": ["gap_001"],
  "signal_category": "amd_gap",
  "confidence": 0.85,
  "classifier_version": "v1"
}
```

### D3.2 给 Module 4（Agent Workflow）的 MCP 工具定义

#### 工具 1: `search_signals`

```jsonc
{
  "name": "search_signals",
  "description": "搜索信号数据库。支持关键词全文搜索、时间范围、repo 过滤、标签过滤的任意组合。返回信号摘要列表。如需完整内容，用 get_signal_detail。",
  "parameters": {
    "type": "object",
    "properties": {
      "query":        {"type": "string",   "description": "全文搜索关键词（FTS5 语法，支持 AND/OR/NOT/\"短语\"）"},
      "repos":        {"type": "array",    "items": {"type": "string"}, "description": "过滤 repo，如 [\"vllm-project/vllm\"]"},
      "source_types": {"type": "array",    "items": {"type": "string"}, "description": "过滤源类型: github_issue, github_pr, tweet, ..."},
      "labels":       {"type": "array",    "items": {"type": "string"}, "description": "GitHub labels 过滤"},
      "state":        {"type": "string",   "enum": ["open", "closed", "all"], "description": "GitHub 状态过滤"},
      "since":        {"type": "string",   "description": "ISO 8601，只返回此时间之后更新的信号"},
      "until":        {"type": "string",   "description": "ISO 8601，只返回此时间之前更新的信号"},
      "gap_ids":      {"type": "array",    "items": {"type": "string"}, "description": "关联的 gap ID 过滤"},
      "sort":         {"type": "string",   "enum": ["relevance", "updated", "created"], "default": "updated"},
      "limit":        {"type": "integer",  "default": 20, "maximum": 50}
    },
    "required": []
  },
  "returns": {
    "description": "信号摘要列表 + 分页信息",
    "example": {
      "results": [
        {
          "signal_id": "github:vllm-project/vllm:issue:39303",
          "source_type": "github_issue",
          "title": "...",
          "author": "ghpu",
          "updated_at": "2026-04-16T14:26:57Z",
          "github_state": "closed",
          "github_labels": ["bug", "rocm"],
          "tags": ["rocm", "aiter", "mla"],
          "body_preview": "...(前 200 字符)...",
          "body_token_estimate": 3020,
          "version": 3,
          "gap_ids": ["gap_001"]
        }
      ],
      "total": 42,
      "total_token_estimate": 8400
    }
  }
}
```

**设计决策**：选择粗粒度（一个 `search_signals` 搞定所有过滤）而非细粒度（每种索引一个工具）。
- **原因 1**：Agent 认知负担小——一个工具 + 可选参数，比记忆 4 个工具 + 组合调用更可靠
- **原因 2**：内部自动路由——当 `query` 非空时用 FTS5，当 `repos` 非空时用 repo 索引，当 `since` 非空时用时间索引，组合条件用 SQL AND
- **原因 3**：避免 Agent "忘记"调用某个过滤工具导致结果不正确

#### 工具 2: `get_signal_detail`

```jsonc
{
  "name": "get_signal_detail",
  "description": "获取单个信号的完整详情，包括全文 body 和所有 comments。用于 Agent 深入分析某个具体信号。",
  "parameters": {
    "type": "object",
    "properties": {
      "signal_id":        {"type": "string", "description": "信号 ID"},
      "include_body":     {"type": "boolean", "default": true},
      "include_comments": {"type": "boolean", "default": true},
      "max_comments":     {"type": "integer", "default": 50, "description": "最多返回多少条 comment"}
    },
    "required": ["signal_id"]
  }
}
```

#### 工具 3: `get_signal_changes`

```jsonc
{
  "name": "get_signal_changes",
  "description": "获取一个信号的变更历史。用于追踪 issue/PR 的演变过程。",
  "parameters": {
    "type": "object",
    "properties": {
      "signal_id": {"type": "string"},
      "since":     {"type": "string", "description": "只返回此时间之后的变更"},
      "meaningful_only": {"type": "boolean", "default": true}
    },
    "required": ["signal_id"]
  }
}
```

#### 工具 4: `get_gap_signals`

```jsonc
{
  "name": "get_gap_signals",
  "description": "获取与特定 gap 关联的所有信号。用于 Agent 分析某个 gap 的全貌。",
  "parameters": {
    "type": "object",
    "properties": {
      "gap_id":           {"type": "string"},
      "include_body":     {"type": "boolean", "default": false},
      "include_changes":  {"type": "boolean", "default": true, "description": "附带最近变更"}
    },
    "required": ["gap_id"]
  }
}
```

**SQLite MCP vs 自定义 MCP 的分工**：

| 场景 | 用哪个 | 理由 |
|---|---|---|
| Worker Agent 事实核查 | 自定义 MCP (`search_signals`, `get_signal_detail`) | 结构化、可控、token 预算可控 |
| Coordinator 战略分析 | 自定义 MCP 为主 | 同上 |
| Coordinator 探索式查询 | SQLite MCP (只读 SQL) | "看看最近一周 vllm 里有多少 aiter 相关的 closed PR"——自定义工具可能没覆盖 |
| Dashboard 后端 | 都不用，直接 Python 调 Repository | 非 Agent 场景，不走 MCP |

**安全**：SQLite MCP 配置为只读（`PRAGMA query_only = ON;`），Agent 无法修改数据。

### D3.2½ GitHub MCP 升级计划

> 4/22 会议决策：完全自主升级 GitHub MCP Server。

**当前版本**：`@modelcontextprotocol/server-github`（26 工具）

缺少的关键能力：
- `get_issue_comments` — 无法直接通过 MCP 获取 issue 评论
- `list_tags` — 无法获取 repo 的 tag/release 列表
- `get_issue_timeline` — 无法获取 issue 事件时间线
- `get_sub_issues` — 无法获取 sub-issue 关系

**升级目标**：`github/github-mcp-server`（GitHub 官方，40+ 工具）

新增能力：
- `issue_read` 支持 `get_comments` 和 `get_sub_issues`
- `list_tags`、`list_discussions`
- 更完整的 PR review 工具链

**安装方式**：Docker `ghcr.io/github/github-mcp-server` 或 GitHub 发布的二进制。

**升级后的影响**：

| 方面 | 变化 |
|---|---|
| Agent MCP 调用 | 不再需要 Fetch MCP 补 issue comments 缺口 |
| 数据链路 | Agent（Module 4）可直接通过 MCP 获取 comments，简化实时查询链路 |
| 数据同步层 | Module 1 数据同步仍然用 `httpx` 直调 REST API（批量效率更高、更可靠、可控性更强） |
| MCP 工具列表 | 需更新 D3.2 工具描述中 Agent 可用的外部 MCP 能力 |

> **原则**：Module 1 同步数据走 REST API（批量、可控、可审计），Module 4 Agent 实时查询走 MCP（方便、Agent 友好）。两条路径互补，不互相替代。

### D3.3 给 Module 5-6（Web Dashboard）的 REST API

```
# ── 信号查询 ──
GET  /api/v1/signals                    # 搜索信号（同 search_signals 参数）
GET  /api/v1/signals/:signal_id         # 信号详情
GET  /api/v1/signals/:signal_id/changes # 变更历史
GET  /api/v1/signals/:signal_id/refs    # 关联引用

# ── 聚合统计 ──
GET  /api/v1/stats/overview             # 总览：信号总数、按类型/状态分布
GET  /api/v1/stats/by-repo              # 按 repo 分组统计
GET  /api/v1/stats/by-date              # 按日期分组（时间线图）
GET  /api/v1/stats/by-label             # 按标签分组
GET  /api/v1/stats/activity             # 活跃度：每日新增/变更信号数

# ── 同步管理 ──
GET  /api/v1/sync/status                # 当前同步状态（是否有运行中的 sync）
GET  /api/v1/sync/runs                  # 同步历史
GET  /api/v1/sync/runs/:run_id          # 单次同步详情

# ── 手动触发 ──
POST /api/v1/sync/trigger               # 手动触发同步
```

`POST /api/v1/sync/trigger` Body:
```jsonc
{
  "source_type": "github",       // 必须
  "source_repo": "vllm-project/vllm",  // GitHub 必须
  "sync_mode": "incremental",   // full | incremental | targeted
  "options": {
    "labels": ["rocm"],
    "include_comments": true,
    "target_numbers": [39303]    // targeted 模式：只同步这些 issue
  }
}
```

### D3.4 内部 Sync Trigger API（代码级接口）

```python
class SyncOrchestrator:
    async def trigger_sync(
        self,
        source_type: str,
        source_repo: str | None = None,
        sync_mode: Literal["full", "incremental", "targeted", "discovery"] = "incremental",
        *,
        labels: list[str] | None = None,
        since: datetime | None = None,
        target_numbers: list[int] | None = None,
        include_comments: bool = True,
        max_comments_per_issue: int = 100,
        deep_scan_on_change: bool = True,
    ) -> SyncRunRecord:
        """
        触发一次同步。返回 SyncRunRecord（可追踪进度）。

        参数:
          source_type: 数据源类型
          source_repo: GitHub repo（owner/repo 格式）
          sync_mode:
            - full: 全量同步，忽略 since
            - incremental: 增量同步，只拉 since 之后的变更
            - targeted: 只同步指定的 issue/PR numbers
            - discovery: 搜索扫描，找新信号
          labels: GitHub label 过滤
          since: 增量起点（None = 使用上次成功 sync 的时间）
          target_numbers: targeted 模式的目标列表
          include_comments: 是否拉取 comments
          max_comments_per_issue: 单 issue 最多拉多少 comment
          deep_scan_on_change: 检测到变更时是否自动深度扫描
        """
```

---

## D4. 增量更新流程

### 完整流程：从 Scheduler 触发到下游可查

```
Step 1: Scheduler 触发
    │
    │  Cron (APScheduler) 或手动 POST /api/v1/sync/trigger
    │  读取 tracking_config.yaml 确定本次同步范围
    │
    ▼
Step 2: 创建 SyncRun 记录
    │
    │  sync_runs.status = "running"
    │  sync_runs.started_at = now()
    │  确定 since 时间：
    │    如果 sync_mode=incremental:
    │      since = 上次该 repo 成功 sync 的 completed_at
    │    如果 sync_mode=full:
    │      since = null (不限)
    │    如果有 resume_token（上次 partial 失败）:
    │      since = resume_token.last_since, page = resume_token.last_page
    │
    ▼
Step 3: 获取原始数据（GitHub Adapter）
    │
    │  3a. 增量获取: GET /repos/{o}/{r}/issues?since={since}&state=all&labels={labels}&per_page=100
    │      ↳ 分页遍历，每页 100 条
    │      ↳ rate_limiter.acquire("rest") 每次请求前检查
    │      ↳ ETag 条件请求：If-None-Match / If-Modified-Since
    │        → 304: 跳过此页（无变化），不消耗 REST 配额
    │        → 200: 有新数据
    │      ↳ 每完成一页，更新 resume_token = {"last_page": N, "last_since": since}
    │
    │  3b. (可选) 搜索发现: GET /search/issues?q=repo:{r}+{keywords}
    │      ↳ rate_limiter.acquire("search") — 30/min 限制
    │      ↳ 用于发现不在 labels 范围内的新信号
    │
    ▼
Step 4: 标准化 + 引用提取（Normalizer）
    │
    │  对每条 RawGitHubIssue:
    │    4a. 字段映射 → Signal envelope
    │    4b. body token 估算 (len(body) / 4 粗估)
    │    4c. 引用提取:
    │        正则: #(\d+) → same-repo issue/PR
    │        正则: ([\w-]+/[\w-]+)#(\d+) → cross-repo reference
    │        正则: github\.com/([\w-]+/[\w-]+)/(issues|pull)/(\d+) → full URL
    │        正则: huggingface\.co/[\w/-]+ → HF model reference
    │    4d. content_hash 计算（见 D5 节）
    │    4e. 标签聚合: GitHub labels + 从 title/body 提取的关键词
    │
    ▼
Step 5: 变更检测（ChangeDetector）
    │
    │  对每条 Signal:
    │    5a. DB 查询: SELECT content_hash, version, github_state, github_labels,
    │                        github_comment_count FROM signals WHERE signal_id = ?
    │    5b. 如果不存在 → 新信号:
    │        - 生成 ChangeEvent(type=new_signal)
    │        - is_new = True
    │    5c. 如果存在且 content_hash 相同 → 无变化:
    │        - 只更新 last_synced_at
    │        - signals_unchanged += 1
    │    5d. 如果存在且 content_hash 不同 → 有变化:
    │        - 逐字段比对，生成 ChangeEvent[] (见 D5 节)
    │        - version += 1
    │
    ▼
Step 6: 深度扫描决策
    │
    │  如果 5d 检测到变化 且 deep_scan_on_change=True:
    │    6a. 获取 comments: GET /repos/{o}/{r}/issues/{n}/comments?per_page=100
    │        ↳ 优化: 如果 comment_count 没变，跳过
    │        ↳ 优化: 如果已有 N 条 comments，只拉第 N+1 页起的（since 参数）
    │    6b. 对每条 comment:
    │        - 去重: UNIQUE(signal_id, comment_id)
    │        - bot 检测: author.login 匹配 BOT_AUTHORS 列表
    │        - 引用提取（同 Step 4c）
    │    6c. 如果是新信号（5b）且 comment_count > 0，也拉 comments
    │
    ▼
Step 7: 持久化（事务）
    │
    │  在一个 SQLite 事务内:
    │    7a. UPSERT signals: INSERT OR REPLACE INTO signals ...
    │        ↳ 这会触发 FTS5 trigger 自动更新全文索引
    │    7b. INSERT signal_comments (新 comments)
    │    7c. INSERT signal_changes (变更事件)
    │    7d. UPSERT signal_refs (新引用关系)
    │    7e. 尝试关联: UPDATE signal_refs SET to_signal_id = ?
    │                 WHERE to_url LIKE '%{repo}%{number}%' AND to_signal_id IS NULL
    │
    │  *** 每条 signal 独立事务 ***
    │  原因: 如果某条 signal 写入失败，不影响其他 signal
    │  代价: 性能略低（每条一个 commit），但 WAL 模式下可接受
    │
    ▼
Step 8: JSON 文件缓存写入
    │
    │  对有变化的 signal:
    │    路径: data/cache/github/{repo_slug}/issues/{number}.json
    │    内容: 完整 Signal envelope + 嵌入的 comments[]
    │    用途: Worker Agent context 注入时直接读文件，不查 DB
    │
    ▼
Step 9: 更新 SyncRun 记录
    │
    │  sync_runs.status = "completed" (或 "partial" 如果有错误)
    │  sync_runs.completed_at = now()
    │  sync_runs.signals_total/created/updated/unchanged = ...
    │  sync_runs.resume_token = null (成功则清除)
    │
    ▼
Step 10: 下游可查
    │
    │  Module 2: 下次调用 GET /api/v1/signals/feed?since={上次拉取时间}
    │           → 自动返回 Step 7 写入的新/变更信号
    │  Module 4: 调用 MCP search_signals / get_signal_detail
    │           → 实时查到最新数据
    │  Module 5-6: 刷新 Dashboard → REST API 返回最新聚合
```

### 失败恢复

```
场景 A: Step 3 中间网络断开（拉了 3 页中的 2 页）

  → sync_runs.status = "partial"
  → sync_runs.resume_token = {"last_page": 2, "last_since": "2026-04-22T12:00:00Z"}
  → 前 2 页的 signals 已写入 DB（每条独立事务）
  → 下次触发同步时:
    - 检测到 resume_token 存在
    - 从 page 3 继续拉取
    - 补充写入剩余 signals
    - 成功后 resume_token 清空

场景 B: Step 7 单条 signal 写入失败（例如 JSON 格式异常）

  → 该条 signal 事务回滚
  → 其他 signal 不受影响
  → sync_runs.error_message 记录失败的 signal_id + 错误原因
  → 人工排查后可用 targeted 模式重新同步该条

场景 C: Rate limit 403

  → rate_limiter 检测到 remaining=0
  → 自动等待 reset 时间 + 1s
  → 重试最多 3 次
  → 如果持续 403: sync_runs.status = "partial"，记录断点
```

### 去重保证

```
三层去重:

1. signal_id 主键: INSERT OR REPLACE — 同 ID 自动覆盖
   例: search_issues 和 list_issues 都返回 #39303
      → 两次都生成 signal_id = "github:vllm-project/vllm:issue:39303"
      → 第二次 INSERT OR REPLACE 覆盖第一次，content_hash 可能相同（无变更）

2. UNIQUE 约束: (source_type, source_repo, source_number)
   例: 防止同一 issue 以不同格式的 signal_id 重复入库
   → 仅对 source_number 非 NULL 的记录生效

3. 变更检测: content_hash 相同则不生成 ChangeEvent
   → 即使同一 signal 被多次发现，只有真正有变化时才触发下游
```

---

## D5. 变更检测算法

### content_hash 计算

```python
import hashlib
import json

def compute_content_hash(signal: dict) -> str:
    """
    计算信号的内容哈希。只包含"有意义"的字段。
    排除: timestamps, sync metadata, token estimates
    包含: 所有可能影响分析结论的字段
    """
    source_type = signal["source_type"]

    # 所有源共有的语义字段
    meaningful = {
        "title": signal.get("title", ""),
        "body": signal.get("body", ""),
        "author": signal.get("author", ""),
    }

    if source_type in ("github_issue", "github_pr"):
        gh = signal.get("github", {}) or {}
        meaningful.update({
            "state": gh.get("state"),
            "labels": sorted(gh.get("labels", [])),
            "assignees": sorted(gh.get("assignees", [])),
            "comment_count": gh.get("comment_count", 0),
            "closed_at": gh.get("closed_at"),
            "is_pr": gh.get("is_pr", False),
            "pr_merged": gh.get("pr_merged"),
        })

    elif source_type == "tweet":
        tw = signal.get("twitter", {}) or {}
        meaningful.update({
            "likes": tw.get("likes", 0),
            "retweets": tw.get("retweets", 0),
        })

    canonical = json.dumps(meaningful, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
```

### 变更检测主流程

```python
from dataclasses import dataclass
from enum import Enum

class ChangeType(str, Enum):
    NEW_SIGNAL      = "new_signal"
    STATE_CHANGE    = "state_change"
    LABEL_CHANGE    = "label_change"
    NEW_COMMENT     = "new_comment"
    BODY_EDIT       = "body_edit"
    ASSIGNEE_CHANGE = "assignee_change"
    PR_LINKED       = "pr_linked"
    PR_MERGED       = "pr_merged"
    CLOSED          = "closed"
    REOPENED        = "reopened"

@dataclass
class ChangeEvent:
    change_type: ChangeType
    old_value: str | None
    new_value: str | None
    is_meaningful: bool
    changed_at: str  # 源平台时间

BOT_AUTHORS = {
    "github-actions[bot]", "dependabot[bot]", "stale[bot]",
    "codecov[bot]", "mergify[bot]", "gemini-code-assist[bot]",
    "copilot-workspace[bot]",
}

def detect_changes(
    new_signal: dict,
    existing_row: dict | None,  # DB 查询结果，None = 新信号
) -> list[ChangeEvent]:
    """
    检测新信号与数据库中已有记录之间的有意义变更。
    返回 ChangeEvent 列表。
    """
    if existing_row is None:
        return [ChangeEvent(
            change_type=ChangeType.NEW_SIGNAL,
            old_value=None,
            new_value=json.dumps({"signal_id": new_signal["signal_id"]}),
            is_meaningful=True,
            changed_at=new_signal.get("updated_at", ""),
        )]

    changes: list[ChangeEvent] = []
    new_gh = new_signal.get("github", {}) or {}
    old_gh = json.loads(existing_row.get("github_json", "{}") or "{}")

    # ── 状态变更 ──
    old_state = old_gh.get("state")
    new_state = new_gh.get("state")
    if old_state != new_state and old_state is not None:
        ct = ChangeType.CLOSED if new_state == "closed" else ChangeType.REOPENED
        changes.append(ChangeEvent(
            change_type=ct,
            old_value=json.dumps(old_state),
            new_value=json.dumps(new_state),
            is_meaningful=True,
            changed_at=new_signal.get("updated_at", ""),
        ))

    # ── 标签变更 ──
    old_labels = set(old_gh.get("labels", []))
    new_labels = set(new_gh.get("labels", []))
    if old_labels != new_labels:
        added = new_labels - old_labels
        removed = old_labels - new_labels
        changes.append(ChangeEvent(
            change_type=ChangeType.LABEL_CHANGE,
            old_value=json.dumps(sorted(old_labels)),
            new_value=json.dumps(sorted(new_labels)),
            is_meaningful=True,
            changed_at=new_signal.get("updated_at", ""),
        ))

    # ── 评论数变更 ──
    old_count = old_gh.get("comment_count", 0)
    new_count = new_gh.get("comment_count", 0)
    if new_count > old_count:
        changes.append(ChangeEvent(
            change_type=ChangeType.NEW_COMMENT,
            old_value=json.dumps(old_count),
            new_value=json.dumps(new_count),
            is_meaningful=True,  # 具体是否有意义在 comment 入库时按 bot 判断
            changed_at=new_signal.get("updated_at", ""),
        ))

    # ── Body 编辑 ──
    old_body = existing_row.get("body", "") or ""
    new_body = new_signal.get("body", "") or ""
    if old_body != new_body:
        diff_len = abs(len(new_body) - len(old_body))
        changes.append(ChangeEvent(
            change_type=ChangeType.BODY_EDIT,
            old_value=json.dumps(f"(len={len(old_body)})"),
            new_value=json.dumps(f"(len={len(new_body)}, diff≈{diff_len})"),
            is_meaningful=diff_len > 50,  # <50 字符变更视为噪音（typo fix）
            changed_at=new_signal.get("updated_at", ""),
        ))

    # ── Assignee 变更 ──
    old_assignees = set(old_gh.get("assignees", []))
    new_assignees = set(new_gh.get("assignees", []))
    if old_assignees != new_assignees:
        changes.append(ChangeEvent(
            change_type=ChangeType.ASSIGNEE_CHANGE,
            old_value=json.dumps(sorted(old_assignees)),
            new_value=json.dumps(sorted(new_assignees)),
            is_meaningful=False,  # assignee 变更通常是管理操作，不影响分析
            changed_at=new_signal.get("updated_at", ""),
        ))

    # ── PR Merged ──
    old_merged = old_gh.get("pr_merged")
    new_merged = new_gh.get("pr_merged")
    if not old_merged and new_merged:
        changes.append(ChangeEvent(
            change_type=ChangeType.PR_MERGED,
            old_value=json.dumps(False),
            new_value=json.dumps(True),
            is_meaningful=True,
            changed_at=new_gh.get("pr_merged_at", new_signal.get("updated_at", "")),
        ))

    return changes


def classify_comment_meaningfulness(comment: dict) -> bool:
    """判断一条 comment 是否有意义（非 bot、非自动化）"""
    author = (comment.get("author") or "").lower()
    if author in BOT_AUTHORS:
        return False

    body = comment.get("body") or ""

    # 自动化评论模式
    auto_patterns = [
        "This comment was marked as",
        "Signed-off-by:",
        "CI passed",
        "CI failed",
        "<!-- ",
        "🔍 Vulnerabilities",
    ]
    for pattern in auto_patterns:
        if pattern in body:
            return False

    return True
```

---

## D6. 前向扩展指南

### 加一个新数据源

**需要的文件和步骤**：

| 步骤 | 文件 | 操作 |
|---|---|---|
| 1 | `src/ingestion/adapters/{source_name}_adapter.py` | **新建**：实现 `SourceAdapter` 接口 |
| 2 | `config/sources.yaml` | **添加**：新源的配置块 |
| 3 | `src/ingestion/normalizer.py` | **添加**：新源的字段映射规则（~20 行） |
| 4 | `src/ingestion/change_detector.py` | **添加**：新源的 `compute_content_hash` 分支（~10 行） |
| 5 | `src/ingestion/scheduler.py` | **添加**：新源的调度配置（~5 行） |
| 合计 | 1 个新文件 + 4 处小改动 | |

**SourceAdapter 接口**：

```python
from abc import ABC, abstractmethod
from dataclasses import dataclass

@dataclass
class RawSignal:
    """源 adapter 返回的原始数据，尚未标准化"""
    raw_id: str
    raw_data: dict
    source_type: str

@dataclass
class RawComment:
    comment_id: str
    author: str
    body: str
    created_at: str

@dataclass
class SourceConfig:
    source_type: str
    params: dict  # 源特定配置

class SourceAdapter(ABC):
    source_type: str

    @abstractmethod
    async def discover(self, config: SourceConfig) -> list[RawSignal]:
        """批量发现：返回匹配配置的所有信号"""

    @abstractmethod
    async def fetch_detail(self, raw_id: str) -> RawSignal:
        """深度获取：返回单个信号的完整数据"""

    @abstractmethod
    async def fetch_comments(self, raw_id: str) -> list[RawComment]:
        """获取信号的评论/回复"""

    @abstractmethod
    def make_signal_id(self, raw: dict) -> str:
        """生成确定性的 signal_id"""

    @abstractmethod
    def compute_content_hash(self, raw: dict) -> str:
        """计算内容哈希（见 D5 节）"""
```

**示例：添加 X/Twitter 源**：

`src/ingestion/adapters/twitter_adapter.py`:
```python
class TwitterAdapter(SourceAdapter):
    source_type = "tweet"

    def __init__(self, mcp_client):
        self.mcp = mcp_client  # mcp-twitter-server

    async def discover(self, config: SourceConfig) -> list[RawSignal]:
        keywords = config.params["keywords"]  # ["vLLM ROCm", "AMD MI300 inference"]
        results = []
        for kw in keywords:
            tweets = await self.mcp.call("search_tweets", query=kw, max_results=50)
            for t in tweets:
                results.append(RawSignal(
                    raw_id=t["id"],
                    raw_data=t,
                    source_type="tweet",
                ))
        return results

    def make_signal_id(self, raw: dict) -> str:
        return f"twitter:{raw['id']}"
```

`config/sources.yaml` 新增:
```yaml
twitter:
  source_type: twitter
  adapter: twitter_adapter.TwitterAdapter
  schedule:
    frequency: "0 */4 * * *"  # 每 4 小时
    priority: P1
  params:
    keywords:
      - "vLLM ROCm"
      - "AMD MI300 inference"
      - "AMD GPU LLM"
      - "ROCm performance"
    exclude_keywords:
      - "giveaway"
      - "hiring"
    min_likes: 5  # 过滤低质推文
```

### 加一个新 repo

**零代码改动。只需编辑一个配置文件。**

`config/tracking_config.yaml` 新增:
```yaml
projects:
  # ... existing projects ...

  new_project:
    repo: "org/new-repo"
    role: "inference_framework"
    priority: P1  # P0 = 2h, P1 = 4h, P2 = daily
    track_labels: ["amd", "rocm"]
    track_keywords: ["ROCm", "AMD", "HIP"]
    keyword_scope: "title"
    gap_search_queries:
      - "rocm is:issue is:open sort:updated"
      - "amd is:pr is:merged sort:updated"
```

调度器会在下一个周期自动发现新 repo 并开始同步。

### API 限制扩展策略

> 4/22 会议决策：注册多个 GitHub App / 账号，每小时轮换 token。

**多账号 Token 轮换方案**：

核心思路：`rate_limiter` 维护一个 token 池，每个 token 独立追踪 `remaining/reset`。当当前 token `remaining < 阈值` 时自动切换到下一个。

**配置格式**（`config/github_tokens.yaml`）：

```yaml
github_tokens:
  - token: "ghp_token_1"
    label: "primary"
  - token: "ghp_token_2"
    label: "secondary"
  - token: "ghp_token_3"
    label: "tertiary"
rotation_strategy: "least_used"  # 或 "round_robin"
```

**轮换逻辑**：

```python
class TokenPool:
    def __init__(self, tokens: list[dict]):
        self.tokens = tokens
        self.state = {t["label"]: {"remaining": 5000, "reset": 0} for t in tokens}

    def get_token(self) -> str:
        best = max(self.tokens, key=lambda t: self.state[t["label"]]["remaining"])
        if self.state[best["label"]]["remaining"] < 100:
            wait_until = min(s["reset"] for s in self.state.values())
            sleep_until(wait_until)
        return best["token"]

    def update_limits(self, label: str, remaining: int, reset: int):
        self.state[label] = {"remaining": remaining, "reset": reset}
```

**容量估算**：

| 配置 | REST 配额/天 | 当前消耗 | 富余倍数 |
|---|---|---|---|
| 单 token | 120,000 | ~310 | 387× |
| 3 token 轮换 | 360,000 | ~310 | 1161× |

**MVP 阶段**：单 token 配置即可。Token Pool 设计提前做好，`config/github_tokens.yaml` 支持多 token，代码先用第一个。当监控到单 token 配额消耗 >50% 时启动多 token。

---

## D7. 核心设计问题 C1-C7 回答

### C1. 增量更新的原子性

**决策**：每条 Signal 独立事务 + resume_token 断点续传。

**机制**：
- 每条 signal 的 UPSERT 在独立 SQLite 事务中执行
- 如果同步到一半失败（50 条拉了 30 条就断了），前 30 条已安全写入
- `sync_runs.resume_token` 记录断点：`{"last_page": 3, "last_since": "..."}`
- 下次同步从断点继续，不会重复处理已入库的 signal（INSERT OR REPLACE 保证幂等）
- 最终 sync_run.status 标记为 "partial"，管理员可看到

**为什么不用批量事务（一次性提交所有 signal）**：
- 批量事务 = 全成功或全失败，对我们来说太严格
- 单条事务 = 渐进式进展，任何时刻中断都不丢已处理的数据
- SQLite WAL 模式下单条事务开销很小（<1ms）

### C2. 信号去重

**决策**：确定性 signal_id 主键去重 + 跨源信号用 signal_refs 关联而非合并。

**同 issue 多途径发现**：
- `search_issues` 和 `list_issues` 都返回 #39303
- 两次都生成相同的 `signal_id = "github:vllm-project/vllm:issue:39303"`
- 第二次写入时 INSERT OR REPLACE 自动覆盖（取较新的数据）
- 不会产生重复

**推文引用 issue**：
- 这是 **两个信号**，不是一个
- `twitter:1234567890` (推文，可能包含独立的分析/观点)
- `github:vllm-project/vllm:issue:39303` (issue)
- `signal_refs` 表记录关联：`from=twitter:..., to=github:..., ref_type=mentions`
- **原因**：推文和 issue 是不同的信息载体，推文可能包含 issue 里没有的观点/情绪/传播度信息。合并会丢失这些维度。

### C3. 变更粒度

**决策**：一信号多版本。Issue 加 comment = signal.version += 1，不创建新信号。

**具体机制**：
- 一个 GitHub issue = signals 表里一行
- 新 comment → signal_comments 表添加行 + signals.version += 1 + signal_changes 记录
- Module 2 通过增量 feed 看到 "这个 signal 变了"
- Module 2 可以看 signal_changes 判断 "变了什么"（state_change vs new_comment vs ...）

**不选"一 comment 一信号"的原因**：
- issue 是 gap 跟踪的原子单位，comment 是 issue 的上下文
- 如果 100 条 comment 各自是独立信号，gap_ids 关联会爆炸（100 条 signal 都关联同一个 gap）
- Module 4 Agent 分析时需要看完整上下文，一个 signal 给它比 100 个碎片更容易

### C4. 存储爆炸

**决策**：存全部 comments，增量追加。

**存储估算**：
- 极端情况: 1 issue × 100 comments × 1KB/comment = 100KB
- 1000 个 issue × 100 comments = 100MB → SQLite 轻松承受（实测 SQLite 在 GB 级数据仍很快）
- 实际分布: 大部分 issue <10 comments，少数 >50

**增量追加机制**：
- signal_comments 表的 UNIQUE(signal_id, comment_id) 保证不重复
- 每次同步只写入新 comments（INSERT OR IGNORE）
- 不需要删除旧 comments（GitHub comments 是不可变的，除非被 edit，但 edit 不改 comment_id）

**如果存储真的成为问题（>10GB）**：
- Phase 1: 对 >6 个月未变更的 signal 的 body 做 LZ4 压缩
- Phase 2: 迁移到 Postgres（见 D9 数据库升级路径）

### C5. 跨源关联

**决策**：引用提取 + signal_refs 表 + 延迟解析。

**机制**：
```
1. 采集推文时从 body 提取 URL
   "vLLM ROCm bug #39303 breaks MI355X, see https://github.com/vllm-project/vllm/issues/39303"
   → 提取出 github URL

2. 写入 signal_refs:
   from_signal_id = "twitter:1234567890"
   to_url = "https://github.com/vllm-project/vllm/issues/39303"
   ref_type = "mentions"
   to_signal_id = NULL (如果 issue 还没入库)

3. 延迟解析（reconciliation）:
   每次 sync 完成后跑一次:
   UPDATE signal_refs
   SET to_signal_id = (
       SELECT signal_id FROM signals
       WHERE source_url = signal_refs.to_url
   )
   WHERE to_signal_id IS NULL;

4. Module 4 Agent 可以查:
   "给我所有引用了 gap_001 相关 issue 的推文"
   → signal_refs JOIN signals JOIN classification
```

### C6. 实时性 vs 效率

**决策**：分层频率，默认 2h 周期，支持按需触发。

| 优先级 | repos | 增量同步 | 全量同步 | 搜索发现 | API 消耗/天 |
|---|---|---|---|---|---|
| P0 | vllm, sglang | 每 2h | 每日 2AM | 每 4h | ~240 REST + 48 Search |
| P1 | aiter, mori, triton | 每 4h | 每日 2AM | 每 8h | ~60 REST + 12 Search |
| P2 | 其他 | 每日 | 每周 | 每日 | ~10 REST + 2 Search |
| 手动 | 任意 | 按需 | 按需 | 按需 | 按需 |

**总消耗估算**（10 repos）：~310 REST/天 + ~62 Search/天

**对比 API 配额**：REST 5000/hr = 120,000/天，Search 30/min = 43,200/天 → **极其宽裕**

**"够实时"的判断**：
- 受众是 AMD 管理层看 dashboard，不是交易系统
- 2h 延迟对周报/日报级别的决策完全足够
- 如果有突发事件（重大 PR merge、重要 issue），可手动 POST /api/v1/sync/trigger 立即同步
- 未来可加 GitHub Webhooks 实现真正的实时推送（需要公网 endpoint）

### C7. Signal 的不可变性

**决策**：signals 表可变（反映当前最新状态）+ signal_changes 表不可变（append-only 审计日志）。

**理由**：
- **查询方便**：查 "目前所有 open 的 rocm bug" = `SELECT * FROM signals WHERE github_state='open'`，不需要重放事件
- **历史可追溯**：signal_changes 记录了每次变更的 before/after，可以回溯 "4/16 这个 issue 是什么状态"
- **简单**：对于 MVP，这比完整的 event sourcing 简单很多
- **trade-off**：如果需要 "精确重建 T 时刻的 signal 快照"，需要从 signal_changes 回放。目前不需要，未来如果需要可以加 `signal_snapshots` 表。

**不可变的是**：
- signal_changes 行一旦写入，永不修改/删除
- signal_comments 行一旦写入，永不修改（GitHub comments 不可删除）
- sync_runs 行一旦 completed，永不修改

**可变的是**：
- signals 主表（反映最新快照）
- signals.classification_json（Module 2 回写分类结果）

---

## D8. MVP 4/24 交付清单

> **4/22 会议确认**：所有模块完全自主，无外部依赖。Vivi 会在我们交付后查看 Signal 格式。

### 目标

后天（4/24）需要交付：**Gap schema + DB setup + basic ingestion** — 能实际跑通 "同步 vllm rocm issues 到本地 DB，能搜索，能看变更"。

### 文件清单

```
my-llm-infra-dashboard/
├── src/
│   ├── __init__.py
│   │
│   ├── ingestion/                          # Module 1
│   │   ├── __init__.py
│   │   ├── models.py                       # Pydantic: Signal, Comment, ChangeEvent, SyncRun
│   │   ├── adapters/
│   │   │   ├── __init__.py
│   │   │   ├── base.py                     # SourceAdapter ABC
│   │   │   └── github_adapter.py           # GitHubAdapter (httpx 直调 REST API)
│   │   ├── normalizer.py                   # RawSignal → Signal 标准化
│   │   ├── reference_extractor.py          # body → references (regex)
│   │   ├── change_detector.py              # content_hash + diff
│   │   └── rate_limiter.py                 # Token bucket (Search 30/min, REST 5000/hr)
│   │
│   ├── storage/                            # Module 3
│   │   ├── __init__.py
│   │   ├── database.py                     # SQLite 连接 + DDL init
│   │   ├── repository.py                   # SignalRepository CRUD
│   │   └── search.py                       # FTS5 + 组合查询
│   │
│   └── sync/                               # 同步编排
│       ├── __init__.py
│       └── orchestrator.py                 # SyncOrchestrator
│
├── config/
│   ├── sources.yaml                        # 数据源配置（MVP: 只有 GitHub）
│   └── tracking_config.yaml                # 已有，增强
│
├── scripts/
│   ├── init_db.py                          # 初始化 SQLite DB
│   ├── sync_github.py                      # CLI: 手动触发 GitHub 同步
│   ├── search_signals.py                   # CLI: 搜索信号 DB
│   └── show_changes.py                     # CLI: 查看变更日志
│
├── data/                                   # Runtime (gitignored)
│   ├── signals.db                          # SQLite 数据库
│   └── cache/                              # JSON 文件缓存
│       └── github/
│           └── vllm-project_vllm/
│               └── issues/
│                   └── 39303.json
│
├── tests/
│   ├── test_normalizer.py                  # 标准化测试（用 demo/signals/ 数据）
│   ├── test_change_detector.py             # 变更检测测试
│   └── test_repository.py                  # DB CRUD 测试
│
├── requirements.txt                        # 依赖
│   # httpx>=0.27
│   # pydantic>=2.0
│   # tiktoken>=0.7
│   # apscheduler>=3.10
│   # aiosqlite>=0.20 (未来 async 用)
│
└── design/
    └── module_1_3_architecture.md          # 本文档
```

### 可验证的 Demo 流程

```bash
# ── Step 1: 环境 ──
cd ~/my-llm-infra-dashboard
pip install -r requirements.txt

# ── Step 2: 初始化 DB ──
python scripts/init_db.py
# 输出: Created signals.db with 7 tables + FTS5 index

# ── Step 3: 同步 vllm rocm issues（增量） ──
GITHUB_TOKEN=ghp_xxx python scripts/sync_github.py \
    --repo vllm-project/vllm \
    --labels rocm \
    --mode incremental \
    --include-comments
# 输出:
#   Sync started: sync_vllm-project_vllm_20260424_...
#   Fetching issues... page 1 (20 results)
#   Fetching issues... page 2 (15 results)
#   Processing 35 signals...
#     [NEW] github:vllm-project/vllm:issue:39303 - [Bug]: aiter.ops...
#     [NEW] github:vllm-project/vllm:pr:39616 - [ROCm][Feature] Enable AITER MLA...
#     ... (33 more)
#   Fetching comments for 12 signals with comment_count > 0...
#     #39303: 11 comments fetched
#     #39378: 18 comments fetched
#     ...
#   Sync completed: 35 total, 35 created, 0 updated, 0 unchanged
#   API calls: 42 REST, 0 Search
#   Time: 28.3s

# ── Step 4: 搜索 ──
python scripts/search_signals.py --query "aiter MLA"
# 输出:
#   Found 3 signals matching "aiter MLA":
#   1. [closed] #39303 - [Bug]: aiter.ops.triton... (11 comments, v1)
#   2. [merged] #39616 - [ROCm][Feature] Enable AITER MLA... (3 comments, v1)
#   3. [open]  #38313 - [ROCm] Add AITER RoPE + KV cache... (4 comments, v1)

python scripts/search_signals.py --repo vllm-project/vllm --state open --labels rocm
# 输出:
#   Found 14 open signals with label "rocm" in vllm-project/vllm:
#   ...

# ── Step 5: 再次同步（测试增量 + 变更检测） ──
# 等 2 小时后，或手动改一些 issue
GITHUB_TOKEN=ghp_xxx python scripts/sync_github.py \
    --repo vllm-project/vllm \
    --labels rocm \
    --mode incremental
# 输出:
#   Sync completed: 35 total, 1 created, 3 updated, 31 unchanged
#   Changes detected:
#     [STATE_CHANGE] #40461: open → closed
#     [NEW_COMMENT] #39749: 1 new comment (simon-mo: "Updated roadmap...")
#     [LABEL_CHANGE] #40573: added "ready"

# ── Step 6: 查看变更日志 ──
python scripts/show_changes.py --since 2026-04-24T00:00:00Z
# 输出:
#   3 meaningful changes since 2026-04-24:
#   [2026-04-24 08:15] STATE_CHANGE #40461: open → closed
#   [2026-04-24 10:30] NEW_COMMENT  #39749: simon-mo 更新了 roadmap
#   [2026-04-24 10:30] LABEL_CHANGE #40573: +ready
```

### 4/24 不需要交付的

- ❌ Twitter / ArXiv / Zhihu / Blog adapters（v2）
- ❌ MCP Service（Week 2）
- ❌ REST API endpoints（Week 2）
- ❌ APScheduler 自动调度（Week 2，MVP 用 CLI 手动触发）
- ❌ Module 2 分类接口的服务端（Week 2）
- ❌ Dashboard 集成（Week 3）

### 4/24 的硬性验收标准

1. `signals.db` 包含 >30 条来自 vllm rocm 的真实 signal
2. 每条 signal 有正确的 content_hash 和 version
3. FTS5 搜索 "aiter MLA" 能返回正确结果
4. 第二次增量同步能正确检测变更（comment 增减、状态变更）
5. signal_changes 表有审计记录
6. JSON 缓存文件存在且内容正确

---

## 附录 A: GitHub API 调用策略详解

### 三种 API 的使用场景

| API | 场景 | Rate Limit | 用法 |
|---|---|---|---|
| `GET /repos/{o}/{r}/issues` (list) | 增量同步：拉某 repo 某 label 的所有 issue | REST 5000/hr | `?state=all&labels=rocm&since={ts}&per_page=100` |
| `GET /search/issues` (search) | 发现扫描：跨 repo 关键词搜索 | Search 30/min | `?q=repo:{r}+{keywords}&sort=updated` |
| `GET /repos/{o}/{r}/issues/{n}` (get) | 深度获取：单条 issue 完整数据 | REST 5000/hr | 用于 targeted sync |

**选择逻辑**：

```python
def choose_api(sync_mode, config):
    if sync_mode == "incremental":
        # 优先用 list_issues（REST 配额宽裕，支持 since 参数）
        return "list_issues"

    elif sync_mode == "discovery":
        # 用 search_issues（可跨 repo、关键词搜索）
        # 注意 30/min 限制
        return "search_issues"

    elif sync_mode == "targeted":
        # 用 get_issue（单条精确获取）
        return "get_issue"

    elif sync_mode == "full":
        # 用 list_issues（不带 since，拉全部）
        return "list_issues"
```

### ETag 条件请求

```python
async def fetch_with_etag(self, url: str, params: dict) -> tuple[int, dict | None]:
    """
    使用 ETag 条件请求。304 = 无变化，不消耗 REST 配额。
    """
    cached = await self.db.get_etag(url)
    headers = {}
    if cached:
        if cached.etag:
            headers["If-None-Match"] = cached.etag
        if cached.last_modified:
            headers["If-Modified-Since"] = cached.last_modified

    resp = await self.client.get(url, params=params, headers=headers)

    if resp.status_code == 304:
        return 304, None  # 无变化

    if resp.status_code == 200:
        await self.db.save_etag(
            url=url,
            etag=resp.headers.get("ETag"),
            last_modified=resp.headers.get("Last-Modified"),
        )
        return 200, resp.json()

    return resp.status_code, None
```

### 私有仓库处理（ROCm/aiter）

ROCm/aiter 是私有仓库，search API 返回 422。

**策略**：
1. `tracking_config.yaml` 标记 `access: private`
2. 如果有 token 且 token 有 repo scope → 用 `list_issues` 直接拉取（REST API 对有权限的私有仓库可用）
3. 如果无权限 → 跳过 API 同步，YAML 手动维护关键信息
4. sync_runs 记录 `note: "private repo, skipped API sync"`

```yaml
aiter:
  repo: "ROCm/aiter"
  access: private  # 标记
  fallback: manual  # 无 API 权限时手动维护
```

---

## 附录 B: Token 估算策略

```python
def estimate_tokens(text: str) -> int:
    """
    估算 text 的 token 数。
    精确方法: tiktoken.encoding_for_model("gpt-4").encode(text)
    快速方法: len(text) / 4 (英文), len(text) / 2 (中文)

    MVP 用快速方法，误差 <20%。
    """
    if not text:
        return 0
    ascii_ratio = sum(1 for c in text if ord(c) < 128) / max(len(text), 1)
    if ascii_ratio > 0.8:
        return len(text) // 4  # 英文为主
    else:
        return len(text) // 2  # 中文/混合
```

**用途**：
- Signal.body_token_estimate：让 Agent 决定要不要读全文（preview vs full）
- search_signals 返回的 total_token_estimate：Agent 知道把这些结果塞进 context 要用多少 token
- 预算控制：Agent 可以设定 "只给我 total_token_estimate < 50000 的结果"

---

## D9. 数据库升级路径

> v1.0 的附录 C "SQLite → Postgres 迁移路径" 在 v2.0 中扩展为完整章节，融合了 4/22 会议讨论和数据库技术调研结果。

### D9.1 数据库对比

| DB | MCP 工具数 | 全文搜索 | 增量更新 | 基础设施 | 代码改动 | 推荐阶段 |
|---|---|---|---|---|---|---|
| **SQLite + FTS5** | 17 | BM25, trigger 自动同步 | 好 | 零（单文件） | 零 | **MVP 现在** |
| **Turso / LibSQL** | 9（CLI 内建） | 兼容 SQLite FTS5 | 好 | 一个 CLI binary | 极小 | **第一次升级** |
| **PostgreSQL** | 38（最多） | tsvector + GIN | 好 | Postgres server | 中等 | **生产环境** |
| DuckDB | 有限 | BM25 extension | 差（不自动更新索引） | 单文件 | 中等 | **不适合** |

### D9.2 推荐路径

```
SQLite (MVP) → Turso/LibSQL (第一次升级) → PostgreSQL (生产环境)
```

**为什么是 Turso/LibSQL 而不是直接跳 Postgres？**

Turso 是 SQLite 的开源 fork（LibSQL），关键优势：
- **完全兼容 SQLite API**——代码几乎不改，`import libsql` 替换 `import sqlite3` 即可
- **MCP 内建在 CLI**——`tursodb signals.db --mcp` 直接暴露 MCP 接口，无需自建 MCP Service
- **免费层**：10GB 存储 / 500 数据库
- **全球复制和 Edge 部署**——未来如果需要多地域访问
- **兼容 FTS5**——现有全文搜索 trigger 和查询不需要改

**为什么排除 DuckDB？**

DuckDB 的 FTS 索引（BM25 extension）**不自动更新**——每次数据变化都需要重建索引。这直接杀死增量更新流程（D4）：我们的核心场景是频繁小批量写入 + 实时可搜索，DuckDB 无法满足。

### D9.3 对 Vincent "SQLite 慢" 的回应

> 4/22 会议上 Vincent 提出 SQLite 未来需要升级到更好的 DB。

当前数据量 <100K 行，SQLite FTS5 搜索延迟 <100ms。"慢" 是对并发写场景的合理顾虑，但我们的写入是单 sync 进程顺序执行，不存在并发写冲突。

| 场景 | SQLite 表现 | 瓶颈出现时间 |
|---|---|---|
| 单进程顺序写入 | 无问题 | N/A |
| FTS5 全文搜索 <100K 行 | <100ms | 预计 >1M 行时需关注 |
| 多进程并发写 | **WAL 模式下写锁等待** | 如需多个 sync worker 并行时 |
| 数据量 >5GB | 单文件 I/O 瓶颈 | 预计 >10 repo × 1 年持续采集 |

**结论**：MVP 用 SQLite 没有性能风险。当触发以下任一条件时启动升级：
1. 数据量 > 5GB
2. 需要多 sync worker 并发写
3. 需要 LISTEN/NOTIFY 实时推送
4. 需要高级 JSON 查询（Postgres JSONB 操作符）

### D9.4 迁移时需要改什么

| 层 | SQLite 当前 | Turso 改动 | Postgres 改动 |
|---|---|---|---|
| DDL | `CREATE TABLE` | 无需改 | 改类型: TEXT→VARCHAR, INTEGER→BIGINT, 加 SERIAL |
| FTS | FTS5 trigger | 无需改（兼容 FTS5） | 改用 `tsvector` + `GIN index` + `to_tsvector()` |
| 连接 | `sqlite3` / `aiosqlite` | `libsql` | `asyncpg` / `psycopg` |
| JSON | `json_extract()` | 无需改 | `->` / `->>` / `jsonb` |
| WAL | `PRAGMA journal_mode=WAL` | 无需改 | 默认支持 MVCC |
| UPSERT | `INSERT OR REPLACE` | 无需改 | `INSERT ... ON CONFLICT DO UPDATE` |
| Repository | `SignalRepository` 内部 SQL | **代码几乎不改** | 替换 SQL dialect |

### D9.5 设计层面的迁移友好（已内建）

1. **Repository 模式**：所有 SQL 封装在 `SignalRepository` 里，上层代码不直接写 SQL
2. **标准 SQL**：尽量用 ANSI SQL，减少 SQLite 方言
3. **JSON 字段**：已经用 TEXT 存 JSON，Turso 直接兼容，Postgres 对应 JSONB
4. **FTS 是唯一大改**：SQLite FTS5 → Postgres tsvector/GIN，但接口（`search()` 方法）不变。Turso 阶段完全不需要改 FTS。

---

## 附录 C: X/Twitter 数据源设计补充

### 搜索关键词

```yaml
twitter:
  search_queries:
    # 高信噪比查询
    - query: "vLLM ROCm"
      priority: P0
    - query: "AMD MI300X inference"
      priority: P0
    - query: "ROCm performance LLM"
      priority: P1
    - query: "AMD GPU benchmark vLLM OR sglang"
      priority: P1
    # 竞对监控
    - query: "NVIDIA H100 vLLM performance"
      priority: P1
    - query: "TensorRT-LLM benchmark"
      priority: P2

  noise_filters:
    # 排除
    exclude_keywords: ["giveaway", "hiring", "salary", "NFT"]
    min_likes: 3           # 低于 3 likes 的推文大概率噪音
    min_followers: 100     # 作者粉丝数太低 = 低质量
    exclude_retweets: true # 只看原创
```

### 推文到 GitHub issue 的关联

```python
def extract_github_refs_from_tweet(tweet_body: str) -> list[str]:
    """从推文 body 提取 GitHub issue/PR 引用"""
    patterns = [
        r'github\.com/([\w.-]+/[\w.-]+)/(issues|pull)/(\d+)',
        r'([\w.-]+/[\w.-]+)#(\d+)',
    ]
    refs = []
    for p in patterns:
        for m in re.finditer(p, tweet_body):
            refs.append(m.group(0))
    return refs
```

### ArXiv / Zhihu / Blog 的触发条件

| 源 | 触发方式 | 频率 | 相关性判断 |
|---|---|---|---|
| ArXiv | RSS feed (`cs.LG`, `cs.AI`) + 关键词过滤 | 每日 | title/abstract 含 "ROCm" OR "AMD" OR "MI300" OR "HIP" |
| Zhihu | 搜索 API / Fetch MCP | 每周 | 关键词: "ROCm", "AMD GPU", "vLLM AMD" |
| Blog | RSS feed (主要技术博客列表) | 每日 | URL 白名单 + 关键词 |

**MVP 不做这三个源。Week 2+ 按需添加。**

---

## 附录 D: 组合查询内部路由

当 `search_signals()` 收到多个过滤条件时，内部 SQL 怎么组合：

```python
def build_query(
    query: str | None,
    repos: list[str] | None,
    labels: list[str] | None,
    state: str | None,
    since: str | None,
    until: str | None,
    sort: str = "updated",
    limit: int = 20,
) -> tuple[str, list]:
    """构建组合查询 SQL"""

    if query:
        # FTS5 路径：先全文搜索，再过滤
        sql = """
            SELECT s.* FROM signals s
            JOIN signals_fts fts ON s.rowid = fts.rowid
            WHERE signals_fts MATCH ?
        """
        params = [query]
    else:
        # 非 FTS 路径：直接查 signals 表
        sql = "SELECT * FROM signals WHERE 1=1"
        params = []

    if repos:
        placeholders = ",".join("?" * len(repos))
        sql += f" AND s.source_repo IN ({placeholders})"
        params.extend(repos)

    if labels:
        # JSON array 包含检查
        for label in labels:
            sql += " AND s.github_labels LIKE ?"
            params.append(f'%"{label}"%')

    if state and state != "all":
        sql += " AND s.github_state = ?"
        params.append(state)

    if since:
        sql += " AND s.updated_at >= ?"
        params.append(since)

    if until:
        sql += " AND s.updated_at <= ?"
        params.append(until)

    # 排序
    order_map = {
        "updated": "s.updated_at DESC",
        "created": "s.created_at DESC",
        "relevance": "rank" if query else "s.updated_at DESC",
    }
    sql += f" ORDER BY {order_map.get(sort, 's.updated_at DESC')}"
    sql += " LIMIT ?"
    params.append(limit)

    return sql, params
```

**支持的组合查询示例**：

```sql
-- "repo=vllm AND labels=rocm AND updated_at>2026-04-14 AND body MATCH 'AITER MLA'"
SELECT s.* FROM signals s
JOIN signals_fts fts ON s.rowid = fts.rowid
WHERE signals_fts MATCH 'AITER MLA'
  AND s.source_repo = 'vllm-project/vllm'
  AND s.github_labels LIKE '%"rocm"%'
  AND s.updated_at >= '2026-04-14T00:00:00Z'
ORDER BY rank
LIMIT 20;
```

**分页**：使用 cursor-based pagination（基于 `(updated_at, signal_id)` 双字段），不用 OFFSET（OFFSET 在大数据集上性能差）。

```sql
-- 第二页
WHERE (s.updated_at, s.signal_id) < (?, ?)
ORDER BY s.updated_at DESC, s.signal_id DESC
LIMIT 20;
```
