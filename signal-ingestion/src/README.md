# AI Infra Gap Intelligence — Module 1 & 3

> Signal Ingestion + Central Storage for the AMD vs NVIDIA LLM infra gap tracker.
> 面向开发者的快速上手和内部结构说明。本文件即开发者指南，涵盖架构概览、设计决策和前向扩展路线。

---

## 目录

- [项目定位](#项目定位)
- [我们负责什么 / 不负责什么](#我们负责什么--不负责什么)
- [快速上手（5 分钟跑通）](#快速上手5-分钟跑通)
- [目录与模块职责](#目录与模块职责)
- [数据流（一张图）](#数据流一张图)
- [下游消费指南](#下游消费指南)
  - [Module 2 (Signal Classifier, Vivi)](#module-2-signal-classifier-vivi)
  - [Module 4 (Agent Workflow)](#module-4-agent-workflow)
  - [Module 5-6 (Dashboard)](#module-5-6-dashboard)
- [前向扩展](#前向扩展)
- [内部约定 / 坑](#内部约定--坑)
- [测试](#测试)
- [相关文档](#相关文档)

---

## 项目定位

为 AMD 管理层构建的**实时竞争情报系统**数据底座。跟踪 AMD vs NVIDIA 在
开源 LLM 基础设施（vLLM、SGLang、triton、ROCm/aiter …）上的能力差距、
bug、修复进度。

- **产品形态**：24h 在线 dashboard（最终给 AMD 管理层看）
- **MVP 边界**：支持 `<10` 个 GitHub repo，只做 `GitHub issues + PRs + comments` 采集
- **我们（本 src/）的位置**：**上游数据底座**。下游分类、Agent、前端由其他组做

---

## 我们负责什么 / 不负责什么

| 模块 | 谁 | 本仓库是否包含 |
|---|---|---|
| 1. Signal Ingestion（数据采集 + 标准化 + 变更检测） | 我们 | ✅ `src/ingestion/` |
| 3. Central Storage（SQLite + FTS5 + JSON cache + 检索层） | 我们 | ✅ `src/storage/` + `src/sync/` |
| 2. Signal Classifier（gap 分类） | Vivi | ❌ 消费本仓库的 feed |
| 4. Agent Workflow（Worker + Coordinator） | Vivi + Zijun | ❌ 未来通过 MCP 消费 |
| 5-6. Dashboard / REST API | Vincent | ❌ 未来通过 REST API 消费 |

**已实现但不在本 src/ 中**：
- MCP Service（12 tools，见 `scripts/signals_mcp_server.py` + `dbhub.toml.example`）
  `dbhub.toml.example` is the template; run `scripts/start_dbhub_server.sh` to generate the local `dbhub.toml`.
- 定时调度（systemd timer 替代了原计划的 APScheduler）

**Planned**：
- HTTP REST API、Twitter/ArXiv adapter、
  Module 2 分类回写端点、Dashboard 聚合 API

---

## 快速上手（5 分钟跑通）

### 先决条件

- Python 3.12+（项目用了 `str | None` union syntax、`@dataclass` slots 等）
- 写入 `.env`（放在项目根目录，**不是 `src/`**）：
  ```
  GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxx
  ```

### 装依赖

```bash
cd /path/to/my-llm-infra-dashboard
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 三条命令跑通 MVP

```bash
# 1. 初始化 DB (只跑一次)
.venv/bin/python scripts/init_db.py

# 2. 拉 vllm 所有 rocm 相关 issue/PR + comments
.venv/bin/python scripts/sync_github.py \
    --repo vllm-project/vllm --labels rocm \
    --mode full --include-comments

# 3. 查一下
.venv/bin/python scripts/search_signals.py --query "aiter MLA"
```

### 后续增量同步

2 小时后再跑（自动以上次成功 sync 的时间作为 `since`）：

```bash
.venv/bin/python scripts/sync_github.py \
    --repo vllm-project/vllm --labels rocm \
    --mode incremental
```

Orchestrator 会：
- 快路径：`content_hash` 未变 → 只更新 `last_synced_at`
- 慢路径：有变化 → 拉 comments、生成 `ChangeEvent[]`、bump version

---

## 目录与模块职责

```
src/
├── ingestion/                       # Module 1 — 采集层
│   ├── models.py                    # Pydantic schemas（统一 Signal envelope）
│   ├── reference_extractor.py       # body → #NNNN / owner/repo#N / URL / @mentions
│   ├── rate_limiter.py              # GitHub REST/Search 双桶限流（5000/hr + 30/min）
│   ├── normalizer.py                # RawSignal → Signal（字段映射 + hash）
│   ├── change_detector.py           # content_hash + 8 种 ChangeEvent 检测
│   └── adapters/
│       ├── base.py                  # SourceAdapter ABC（4 个抽象方法）
│       └── github_adapter.py        # httpx 实现，3 种 sync 模式
│
├── storage/                         # Module 3 — 存储层
│   ├── database.py                  # SQLite DDL（9 表 + FTS5 + triggers）
│   ├── repository.py                # CRUD + 事务管理（transaction() 公开）
│   ├── cache.py                     # data/cache/github/.../N.json（原子写）
│   └── search.py                    # FTS5 + 组合查询 + get_feed() D3.1
│
└── sync/                            # 编排层
    └── orchestrator.py              # SyncOrchestrator + build_default_orchestrator()
```

**导入方向是严格分层的**，不能反向：

```
scripts/*.py
    ↓
sync.orchestrator
    ↓
   ingestion.*    ←→   storage.*
    (互不依赖)
    ↓
ingestion.models  ←  所有模块共享的 Pydantic schemas
```

---

## 数据流（一张图）

```
GitHub REST API
    ↓ httpx
[GitHubAdapter].discover(labels=rocm, since=...)
    ↓ async generator of RawSignal
[Normalizer].normalize_signal(raw)
    ↓ Signal (with references, tags, content_hash)
[ChangeDetector].detect(new, existing)
    ↓ list[ChangeEvent]
 ┌──────────── 仅当 is_new 或 comment_count 变 ─────────┐
 ↓                                                       ↓
[GitHubAdapter].fetch_comments()                         │
[extract_references(comments)]                           │
 ↓                                                       ↓
with repository.transaction():  ← 每条 signal 原子事务 ←┘
    upsert_signal + upsert_comments + append_changes + upsert_refs
    ↓
[JSONCache].write(signal, comments)  ← data/cache/.../N.json
    ↓
(loop next signal)
    ↓
repository.reconcile_refs()   ← 回填 signal_refs.to_signal_id
    ↓
update_sync_run(status=completed, 统计字段)
```

---

## 下游消费指南

### Module 2 (Signal Classifier, Vivi)

**目的**：给每条 signal 打分类标签（`amd_gap` / `amd_fix` / `nvidia_advantage` / `noise`）并关联 `gap_ids`。

**MVP 推荐：直接 import Python 接口**（HTTP API Week 2 才有）：

```python
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

with SignalRepository("data/signals.db") as repo:
    search = SignalSearch(repo)

    # 增量拉"未分类的"
    feed = search.get_feed(
        since="2026-04-23T00:00:00Z",
        classified=False,
        limit=100,
    )

    for sig in feed["signals"]:
        # sig 是已 JSON-parsed 的 dict
        sig_id = sig["signal_id"]
        title = sig["title"]
        body_preview = sig["body_preview"]     # 前 200 字符，省 token
        tags = sig["tags"]                     # ["bug", "rocm"]
        gh_state = sig["github_state"]
        gh_labels = sig["github_labels"]

        # 深入需要完整 body + comments:
        detail = search.get_detail(sig_id, include_comments=True)
        # detail["body"], detail["comments"]

        # 你的分类逻辑
        category, gap_ids, confidence = your_classifier(detail)

        # 回写（repository.update_classification 已实现）
```

**更简单：直接读 JSON 缓存**（零 SQL）：

```python
import json
from pathlib import Path

for p in Path("data/cache/github/vllm-project_vllm/issues/").glob("*.json"):
    sig = json.loads(p.read_text())
    # Full signal shape: see D2.1 (Signal envelope) in models.py
```

**Signal envelope 关键字段**（D2.1 原文）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `signal_id` | str | `github:vllm-project/vllm:issue:39303`，确定性 |
| `source_type` | str | `github_issue` / `github_pr` / `tweet` / `arxiv_paper` / ... |
| `title` / `body` | str | **body 完整不截断**（D2.1 硬性要求） |
| `body_token_estimate` | int | cl100k_base 估算（MVP 用 `len/4` 快算） |
| `tags` | list[str] | GitHub labels + 未来的关键词提取 |
| `references` | dict | `{github_issues, github_prs, external_urls, mentions}` |
| `github` | dict | `{state, labels, assignees, comment_count, is_pr, pr_merged, ...}` |
| `content_hash` | str | SHA-256 over meaningful fields，用来做幂等 |
| `version` | int | 每次"有意义变更"+1 |
| `classification` | dict\|null | Vivi 回写字段（目前都是 null） |

### Module 4 (Agent Workflow)

MCP Service 已上线（`scripts/signals_mcp_server.py` + `dbhub.toml.example`），提供 12 个工具，包括 D3.2 规划的核心 4 个：`search_signals` / `get_signal_detail` / `get_signal_changes` / `get_gap_signals`，以及 `execute_sql`、`search_objects` 等。配置方式见根目录 `README.md`。

Agent 也可使用 `GitHub MCP`（已在 `~/.cursor/mcp.json` 配好，41 工具）做实时代码级查询，或直接读 `data/cache/github/**/*.json` 缓存文件。

### Module 5-6 (Dashboard)

目前 MVP 没有 REST API。Week 2 会按 D3.3 实现：

- `GET /api/v1/signals` / `:id` / `:id/changes`
- `GET /api/v1/stats/overview` / `by-repo` / `by-date` / `activity`
- `POST /api/v1/sync/trigger`

**当前临时方案**：直接查 `data/signals.db` 或调用 `SignalSearch` Python 接口。

---

## 前向扩展

### 加一个新 repo：零代码改动

编辑 `config/sources.yaml`（或者 CLI 直接传）：

```yaml
github:
  repos:
    - repo: "sgl-project/sglang"
      priority: P0
      track_labels: ["amd", "rocm"]
      include_comments: true
```

```bash
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd,rocm
```

### 加一个新数据源（Twitter / ArXiv）：1 新文件 + 4 处小改

按 D6 的 `SourceAdapter` 接口：

```python
# src/ingestion/adapters/twitter_adapter.py  (新文件)
from src.ingestion.adapters.base import SourceAdapter, SourceConfig
from src.ingestion.models import RawSignal, RawComment, SourceType

class TwitterAdapter(SourceAdapter):
    source_type = SourceType.TWEET

    async def discover(self, config: SourceConfig, *, since=None):
        # yield RawSignal(...)
        ...

    async def fetch_detail(self, raw_id, **kw) -> RawSignal: ...
    async def fetch_comments(self, raw_id, *, since=None) -> list[RawComment]: ...
    def make_signal_id(self, raw): return f"twitter:{raw['id']}"
```

修改 4 处：
1. `src/ingestion/normalizer.py` 增加 `_normalize_tweet()` dispatch
2. `src/ingestion/change_detector.py::compute_content_hash` 加 tweet 分支
3. `config/sources.yaml` 增加 `twitter:` 配置块
4. `src/sync/orchestrator.py::build_default_orchestrator` 注入新 adapter

**存储层完全不动**——`signals.twitter_json` 列和 `source_type = tweet` 已经在 DDL 里预留。

### 加 Module 2 分类回写接口

在 `src/storage/repository.py` 加一个方法：

```python
def update_classification(
    self,
    signal_id: str,
    *,
    gap_ids: list[str],
    signal_category: str,
    confidence: float,
    classifier_version: str,
) -> None:
    """Module 2 write-back. See D3.1 PUT /api/v1/signals/{id}/classification."""
    ts = datetime.now(timezone.utc).isoformat()
    payload = {
        "classified_at": ts,
        "gap_ids": gap_ids,
        "signal_category": signal_category,
        "confidence": confidence,
        "classifier_version": classifier_version,
    }
    with self._txn():
        self._conn.execute(
            "UPDATE signals SET classification_json=?, gap_ids=? WHERE signal_id=?",
            (json.dumps(payload), json.dumps(gap_ids), signal_id),
        )
```

### 加 HTTP REST API（Week 2）

```python
# src/api/main.py (未来)
from fastapi import FastAPI
from src.storage.repository import SignalRepository
from src.storage.search import SignalSearch

app = FastAPI()

@app.get("/api/v1/signals/feed")
async def feed(since: str, classified: bool = False, limit: int = 100):
    with SignalRepository("data/signals.db") as repo:
        return SignalSearch(repo).get_feed(
            since=since, classified=classified, limit=limit
        )
```

`SignalSearch.search() / get_detail() / get_feed()` 签名已经按 D3.1/D3.3 对齐，包一层 FastAPI 即可。

### MCP Service（已实现）

MCP Service 已上线，提供 12 个工具（含 D3.2 规划的 `search_signals` /
`get_signal_detail` / `get_signal_changes` / `get_gap_signals`）。
实现见 `scripts/signals_mcp_server.py` + `dbhub.toml.example`，配置方式见根目录 `README.md`。

### 定时调度（已实现）

使用 systemd timer（或 crontab）驱动增量同步，替代了原计划的 APScheduler。
配置示例见根目录 `README.md` "定时自动拉取" 小节。

### DB 升级（D9 路径）

```
SQLite + FTS5  →  Turso / LibSQL  →  Postgres + tsvector
(MVP)           (第一次升级)       (生产)
```

| 层 | 需要改什么 |
|---|---|
| Repository 公开 API | **不改** |
| Repository 内部 SQL | 几个 dialect 差异（`INSERT OR REPLACE` → `ON CONFLICT DO UPDATE`） |
| FTS5 | `signals_fts` 虚拟表 → Postgres `tsvector` 列 + `GIN` 索引 |
| 连接驱动 | `sqlite3` → `libsql` → `asyncpg` |

---

## 内部约定 / 坑

### 连接必须用 `get_connection()`

SQLite 的 `foreign_keys`、`busy_timeout` 是 **per-connection** 的。直接用
`sqlite3.connect()` 会丢掉这些 PRAGMA，`signal_comments.ON DELETE CASCADE`
会静默失效。

### Repository 的两种事务模式

```python
# 拥有连接 (默认): 每个方法内部 `with self._conn:` 自动 commit
repo = SignalRepository("data/signals.db")
repo.upsert_signal(sig)  # 自己是一个事务

# 注入连接: 需要调用方显式 transaction()
conn = get_connection(...)
repo = SignalRepository(connection=conn)
with repo.transaction():
    repo.upsert_signal(sig)
    repo.upsert_comments(cs)      # 这 4 个在同一个事务
    repo.append_changes(events)
    repo.upsert_refs(refs)
```

**Orchestrator 用后者**（4 个方法原子），所以永远要显式 `with repo.transaction()`。

### Signal.version 由 Repository 决定，Normalizer 总是给 1

```python
sig = normalizer.normalize_signal(raw)
assert sig.version == 1  # Normalizer 不知道 DB 历史

# Orchestrator 的典型逻辑
existing = repo.get_by_id(sig.signal_id)
if existing and existing["content_hash"] == sig.content_hash:
    # 没变化 → 保持原 version
    repo.upsert_signal(sig, version_override=existing["version"])
elif existing:
    # 有变化 → 自动 +1
    repo.upsert_signal(sig)  # Repository 内部 existing+1
else:
    # 新 signal
    repo.upsert_signal(sig)  # version=1
```

### body 完整保留，不截断

D2.1 硬性要求："body": "..." // 完整 body，
不截断。所以 `Normalizer` 的 `body_max_chars=0` 默认，**不要**改成任何正数。

### content_hash 的 "meaningful fields"

只包含**会影响分析结论**的字段：`title`, `body`, `author`, `state`, `labels`,
`assignees`, `comment_count`, `closed_at`, `is_pr`, `pr_merged`。

**不包含**：timestamps、sync metadata、token estimates、version。

即：同一条 issue 在不同时间 sync，只要 GitHub 上没人改过内容，
`content_hash` 就不变 → 走快路径。

### FTS5 查询需要 JOIN

`signals_fts` 是 `content=signals` 的虚拟表，本身不存完整列：

```sql
-- ❌ 错误: FTS5 表里没有 signal_id
SELECT signal_id FROM signals_fts WHERE signals_fts MATCH 'aiter MLA';

-- ✅ 正确: JOIN 回 signals
SELECT s.* FROM signals s
JOIN signals_fts f ON s.rowid = f.rowid
WHERE f MATCH 'aiter MLA';
```

`SignalSearch.search()` 已经封装好，调用方不用操心。

### Bot 判断

`src/ingestion/models.py::is_bot_author()` 用精确匹配 + `[bot]` 后缀。
`github-actions[bot]`、`dependabot[bot]`、`stale[bot]` 等内置。comment 如果
`is_bot=True` 且 body 含 "CI passed" / "Signed-off-by:" 等模式，在
`classify_comment_meaningfulness()` 里会被标为 noise。

### Rate limiter 两个 bucket

REST API（`/repos/.../issues`）用 `bucket="rest"`（5000/hr），Search API
（`/search/issues`）用 `bucket="search"`（30/min）。调用方传错会被
`ValueError` 拒绝。**GitHubAdapter 内部已经按端点自动选对**。

---

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
# 验证所有测试通过（运行 pytest tests/ -q 查看最新计数）
```

测试覆盖概况：

| 模块 | 测试文件 | 用例数 | 覆盖要点 |
|------|----------|--------|----------|
| `reference_extractor` | `test_reference_extractor.py` | 24 | 正则边界、代码块屏蔽、URL 片段误匹配 |
| `change_detector` | `test_change_detector.py` | 33 | 8 种 ChangeType、content_hash determinism、meaningful threshold |
| `normalizer` | `test_normalizer.py` | 19 | 字段映射、issue/PR 区分、bot 识别 |
| `rate_limiter` | `test_rate_limiter.py` | 26 | async 并发行为、quarantine 逻辑 |
| `GitHubAdapter` | `test_github_adapter.py` | 30 | 429/retry mock、HTTP 层行为 |
| `Repository` | `test_repository.py` | 17 | 真实 SQLite CRUD + 事务 |
| orchestrator E2E | `test_integration.py` | 30 | mock adapter 端到端流程 |

**剩余 gap**：
- MCP dbhub 集成测试（`test_mcp_dbhub.py`）需要 Node.js v24 + dbhub 运行环境，CI 中默认跳过
- MCP signals server 的 SSE 长连接行为未覆盖

---

## 相关文档

| 文件 | 内容 |
|---|---|
| `tests/fixtures/signals/*.json` | 3 个真实数据样本：单 issue、单 PR、批量发现（93.4% 压缩比） |

**改代码之前先读本文档和目标模块的 docstring**。
