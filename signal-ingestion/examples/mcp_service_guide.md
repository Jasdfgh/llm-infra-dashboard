# Signals Service MCP 使用指南

> Last updated: 2026-04-29

一个入口，12 个工具，覆盖 vllm + sglang + pytorch 共 **~111,000 signals、~525,000 comments**（1.5 GB 数据库）的查询、同步、管理。

| 类别 | 工具数 | 工具列表 |
|---|---|---|
| **Query** (查询) | 5 | search_signals, get_signal_detail, get_signal_feed, get_signal_changes, get_gap_signals |
| **Analytics** (分析) | 2 | get_stats, execute_sql |
| **Sync** (同步) | 3 | trigger_sync, trigger_sync_all, sync_status |
| **Maintenance** (维护) | 2 | db_health, db_maintain |

## MCP 配置

在 Cursor 或 Claude Code 的 MCP 配置文件中加入 `signals-service`，根据网络环境选择一种：

### 本机访问（localhost）

服务和客户端在同一台机器上：

```json
"signals-service": {
  "url": "http://localhost:8082/mcp"
}
```

### 局域网访问（LAN）

服务运行在内网服务器上（需要 `--host 0.0.0.0` 启动），客户端通过 IP 连接：

```json
"signals-service": {
  "url": "http://<server-ip>:8082/mcp"
}
```

**配置文件位置：**
- Cursor: `~/.cursor/mcp.json` 的 `mcpServers` 字段
- Claude Code: `~/.claude.json` 的 `mcpServers` 字段

添加后重载编辑器（Cursor: `Ctrl+Shift+P` → `Developer: Reload Window`）即可使用。

> **Tips:** 可用 `bash workshop/scripts/signals_status.sh` 查看服务运行状态和实际绑定地址。

---

## Query Tools — 信号查询

日常使用最多的工具。支持全文搜索、多维过滤、分页、排序。

### 1. `search_signals`

**用途：** 最常用的工具。在 111,000+ signals 上做全文搜索 + 多维过滤，FTS5 引擎，毫秒级响应。适用于：找某个关键词的 issue/PR、筛选特定仓库的 open issues、按标签交叉过滤等。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `query` | string | `""` | FTS5 全文搜索，支持 AND/OR/NOT、"短语"、prefix* |
| `repos` | string | `""` | 逗号分隔仓库过滤，如 `"vllm-project/vllm"` |
| `source_types` | string | `""` | `"github_issue"` 或 `"github_pr"` 或两者 |
| `labels` | string | `""` | 标签 AND 过滤，如 `"rocm,bug"` |
| `state` | string | `""` | `"open"` / `"closed"` / `"all"` |
| `since` | string | `""` | ISO 8601 时间下界 (updated_at) |
| `until` | string | `""` | ISO 8601 时间上界 |
| `sort` | string | `"updated"` | `"relevance"`(需要 query) / `"updated"` / `"created"` |
| `limit` | int | `20` | 每页条数 1-50 |
| `offset` | int | `0` | 分页偏移 |

**调用示例：**

场景 1: 搜关键词 — "ROCm 上的 MLA attention 相关问题有哪些？"
```json
{ "query": "aiter MLA", "limit": 10 }
```

场景 2: 仓库 + 状态过滤 — "sglang 目前还有多少 open 的 issue？"
```json
{ "repos": "sgl-project/sglang", "state": "open", "sort": "updated" }
```

场景 3: 标签交叉 — "vllm 里同时有 rocm 和 bug 标签的 open items"
```json
{ "repos": "vllm-project/vllm", "labels": "rocm,bug", "state": "open" }
```

场景 4: 时间范围 — "过去 7 天更新的 DeepSeek 相关 PR"
```json
{ "query": "DeepSeek", "source_types": "github_pr", "since": "2026-04-22T00:00:00Z" }
```

场景 5: 分页 — 拿第 2 页
```json
{ "repos": "vllm-project/vllm", "state": "open", "limit": 20, "offset": 20 }
```

**Tips:**
- `query` 为空时跳过 FTS，纯走索引过滤，速度更快
- `sort="relevance"` 必须配合 `query` 使用，否则会退化成 updated 排序
- FTS5 语法: `"exact phrase"`, `prefix*`, `term1 AND term2`, `term1 OR term2`, `NOT term`

---

### 2. `get_signal_detail`

**用途：** 获取单个 signal 的完整详情：body 全文、标签、引用关系 (references)、评论列表。适用于：深入了解某个 issue/PR 的讨论内容、查看 review 意见等。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `signal_id` | string | (必填) | 信号唯一 ID，格式: `github:<owner/repo>:<issue\|pr>:<number>` |
| `include_body` | bool | `true` | 是否包含 body 全文 (false 则只有 200 字 preview) |
| `include_comments` | bool | `true` | 是否包含评论列表 |
| `max_comments` | int | `50` | 最多返回评论数 1-200 |

**调用示例：**

场景 1: 看某个 PR 的完整讨论
```json
{ "signal_id": "github:vllm-project/vllm:pr:41016" }
```

场景 2: 快速扫 — 只要标题和标签，不要 body 和 comments
```json
{ "signal_id": "github:sgl-project/sglang:issue:23814", "include_body": false, "include_comments": false }
```

场景 3: 只看最新 5 条评论
```json
{ "signal_id": "github:vllm-project/vllm:issue:39774", "max_comments": 5 }
```

**Tips:**
- `signal_id` 可以从 `search_signals` 结果的 `signal_id` 字段拿到
- 返回的 `references_json` 包含该 signal 引用的其他 issue/PR
- `include_body=false` 可以节省 token，适合批量扫描

---

### 3. `get_signal_feed`

**用途：** 增量 Feed — 获取某个时间点之后新增/更新的 signals，支持 keyset 分页。适用于：定时拉取新信号、喂给下游分类模块、做每日 digest 等。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `since` | string | (必填) | ISO 8601 时间点，匹配 `last_synced_at >= since` |
| `classified` | string | `"false"` | `"false"`=未分类 / `"true"`=已分类 / `"all"`=不过滤 |
| `source_types` | string | `""` | 类型过滤 |
| `limit` | int | `100` | 每页 1-500 |
| `cursor` | string | `""` | 翻页游标，来自上一次 `pagination.next_cursor` |

**调用示例：**

场景 1: 拿今天新同步的所有 signals
```json
{ "since": "2026-04-29T00:00:00Z", "limit": 100 }
```

场景 2: 只拿未分类的 issues（喂给分类器）
```json
{ "since": "2026-04-28T00:00:00Z", "classified": "false", "source_types": "github_issue" }
```

场景 3: 用 cursor 翻页
```json
{ "since": "2026-04-28T00:00:00Z", "cursor": "上一次返回的next_cursor值" }
```

**Tips:**
- 比 `search_signals` 更适合"持续消费"场景，保证不遗漏
- keyset 分页比 offset 分页更稳定，不会因为中途有新数据而跳过

---

### 4. `get_signal_changes`

**用途：** 变更历史追踪 — 查询 signal 的状态变迁记录（open→closed, label 增删等）。适用于：追踪某个 issue 被关闭/重开的时间线、分析 label 变更趋势。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `signal_id` | string | `""` | 限定单个 signal，空=查全部变更 |
| `since` | string | `""` | 时间下界 |
| `meaningful_only` | bool | `true` | 排除 bot 噪声事件 |
| `limit` | int | `100` | 最大行数 1-500 |

**调用示例：**

场景 1: 看某个 PR 的完整变更时间线
```json
{ "signal_id": "github:vllm-project/vllm:pr:41016" }
```

场景 2: 过去 24 小时所有有意义的变更
```json
{ "since": "2026-04-28T00:00:00Z", "meaningful_only": true, "limit": 200 }
```

**Tips:**
- `meaningful_only=true` 会过滤掉 bot 自动标签、CI 状态更新等噪声
- 适合做"每日变更 digest"

---

### 5. `get_gap_signals`

**用途：** 按 Gap ID 查询关联的 signals。Gap 是我们定义的能力缺口分类。适用于：查看某个 gap 关联了哪些 issue/PR，评估 gap 的严重程度和覆盖面。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `gap_id` | string | (必填) | Gap 标识符，如 `"gap_001"` |
| `limit` | int | `20` | 最大结果数 1-50 |

**调用示例：**

```json
{ "gap_id": "gap_001", "limit": 50 }
```

**Tips:**
- 需要先有分类结果（`classification_json` 和 `gap_ids` 字段），否则返回为空
- 可以配合 `get_stats` 先看全局有哪些 gap 分布

---

## Analytics Tools — 统计分析

看全局数据分布，或者用 SQL 做自定义分析。

### 6. `get_stats`

**用途：** 一键获取全局统计：按 repo/state/type 的完整分布、总数、最后同步时间、DB 大小。适用于：快速了解数据全貌、做报告开头的数字概览。

**参数：** 无

**调用示例：**

```json
{}
```

**Tips:**
- 无参数，直接调用
- 返回的 `last_sync` 字段会告诉你最近一次同步的状态

---

### 7. `execute_sql`

**用途：** Escape hatch — 直接执行只读 SQL。当内置工具满足不了需求时，可以写任意 SELECT 查询。适用于：复杂聚合、自定义统计、跨表 JOIN、数据导出等。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `sql` | string | (必填) | SQL 查询 (只允许 SELECT) |
| `max_rows` | int | `100` | 最大返回行数 1-1000 |

**安全机制（四层防护）：**

| 层 | 机制 | 说明 |
|---|---|---|
| 1 | read-only URI | 连接使用 `file:...?mode=ro`，数据库级别只读 |
| 2 | authorizer 回调 | 白名单 PRAGMA（`table_info`, `index_list` 等）；黑名单函数（`randomblob`, `zeroblob`, `writefile`, `readfile`, `load_extension` 等被拒绝） |
| 3 | SQLITE_LIMIT_LENGTH | 单值上限 1 MB，防止构造巨型字符串 |
| 4 | progress handler | 查询步数上限，自动终止长时间运行的查询 |

**输出限制：**
- 单个 cell 超过 10,000 字符会被截断，返回中出现 `cell_truncated: true` 标记
- blob 超过 1 KB 会替换为 `<blob N bytes>` 占位
- 整体响应有 **5 MB** 上限，超出时自动裁减行数

**调用示例：**

场景 1: 按作者统计贡献排名
```json
{ "sql": "SELECT author, COUNT(*) as cnt FROM signals WHERE source_repo='vllm-project/vllm' AND github_is_pr=1 GROUP BY author ORDER BY cnt DESC LIMIT 15" }
```

场景 2: 每月 issue 创建趋势
```json
{ "sql": "SELECT strftime('%Y-%m', created_at) as month, COUNT(*) as cnt FROM signals WHERE source_type='github_issue' GROUP BY month ORDER BY month DESC LIMIT 12" }
```

场景 3: 查看表结构
```json
{ "sql": "SELECT name, sql FROM sqlite_master WHERE type='table'" }
```

场景 4: 找评论最多的 issue
```json
{ "sql": "SELECT signal_id, title, github_comment_count FROM signals WHERE github_state='open' ORDER BY github_comment_count DESC LIMIT 10" }
```

**Tips:**
- 主要表: `signals`, `signal_comments`, `signal_changes`, `sync_runs`
- 可以用 `sqlite_master` 查看所有表和索引
- 可以使用白名单内的 PRAGMA：`table_info`, `table_xinfo`, `index_list`, `index_info`, `database_list`, `compile_options`, `function_list`
- 适合做自定义数据探索，当其他工具的参数不够灵活时用这个

---

## Sync Tools — 数据同步

从 GitHub 拉取最新数据。支持增量、全量、定向同步。允许同步的仓库列表由 `config/sources.yaml` 集中管理。

### 8. `trigger_sync`

**用途：** 触发单个仓库的同步。支持三种模式：增量（只拉最近更新）、全量（所有 issue/PR）、定向（指定编号）。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `repo` | string | `"vllm-project/vllm"` | 仓库名（必须在 `config/sources.yaml` 白名单中） |
| `mode` | string | `"incremental"` | `"incremental"` / `"full"` / `"targeted"` |
| `labels` | string | `""` | 标签过滤 (incremental/full 模式) |
| `target` | string | `""` | 指定 issue/PR 编号 (targeted 模式必填) |

**验证规则：**
- repo 名必须匹配 `owner/name` 格式（正则校验）
- repo 必须存在于 `config/sources.yaml` 的白名单中，否则拒绝同步
- 白名单加载失败时 fail-closed（拒绝所有同步请求）

**调用示例：**

场景 1: vllm 增量同步 — "拉一下 vllm 的最新数据"
```json
{ "repo": "vllm-project/vllm", "mode": "incremental" }
```

场景 2: sglang 增量同步
```json
{ "repo": "sgl-project/sglang", "mode": "incremental" }
```

场景 3: 只同步特定 issue — "帮我更新 #39303 和 #39616 的数据"
```json
{ "repo": "vllm-project/vllm", "mode": "targeted", "target": "39303,39616" }
```

场景 4: 只同步带 rocm 标签的 issues
```json
{ "repo": "vllm-project/vllm", "mode": "incremental", "labels": "rocm" }
```

**Tips:**
- `full` 模式耗时较长（几十分钟），日常用 `incremental` 即可
- `targeted` 模式适合快速刷新你正在跟踪的特定 issue
- 同步是后台运行的，用 `sync_status()` 查看进度
- 如果传入未配置的仓库名，会返回错误和允许的仓库列表

---

### 9. `trigger_sync_all`

**用途：** 一键触发所有仓库的增量同步。从 `config/sources.yaml` 读取仓库列表，逐个执行 incremental sync，完成后自动执行 WAL checkpoint + ANALYZE。

**参数：** 无

**调用示例：**

```json
{}
```

**Tips:**
- 仓库列表不再硬编码，由 `config/sources.yaml` 统一管理
- 同步完成后自动做 checkpoint 和 analyze，无需手动维护
- 用 `sync_status()` 监控进度

---

### 10. `sync_status`

**用途：** 查看当前同步状态和历史。适用于：确认同步是否完成、排查同步失败。

**参数：** 无

**调用示例：**

```json
{}
```

**Tips:**
- 会返回当前进程 PID（如果在运行）
- `recent_runs` 里有最近 5 次同步的开始/结束时间、状态和统计数据
- `db` 字段包含当前 signals 和 comments 总数

---

## Maintenance Tools — 数据库维护

健康检查和日常维护，确保查询性能。

### 11. `db_health`

**用途：** 数据库健康检查：大小、WAL 大小、signal/comment 数量、FTS5 完整性、最后同步时间。

**参数：** 无

**调用示例：**

```json
{}
```

**Tips:**
- `fts_integrity: "ok"` 表示 FTS5 索引正常
- WAL 太大 (>50MB) 时建议跑 `db_maintain` checkpoint
- 同步运行中时 `fts_integrity` 可能显示 `"database is locked"`，正常现象

---

### 12. `db_maintain`

**用途：** 执行数据库维护操作。四种 action 分别优化不同方面。

**参数：**

| Param | Type | Default | Description |
|---|---|---|---|
| `action` | string | `"analyze"` | `"analyze"` / `"optimize_fts"` / `"checkpoint"` / `"reconnect"` |

**调用示例：**

场景 1: 更新查询优化器统计 — 大批量同步后建议跑
```json
{ "action": "analyze" }
```

场景 2: 优化 FTS5 索引 — 搜索变慢时用
```json
{ "action": "optimize_fts" }
```

场景 3: 收缩 WAL 文件 — WAL 过大时用
```json
{ "action": "checkpoint" }
```

场景 4: 重建 DB 连接 — checkpoint 截断失败时用
```json
{ "action": "reconnect" }
```

**Tips:**

| Action | 作用 | 适用场景 |
|---|---|---|
| `analyze` | 重新统计索引分布，优化查询计划 | 大批量同步后 |
| `optimize_fts` | 合并 FTS5 B-tree 段，加速全文搜索 | 大量写入后搜索变慢 |
| `checkpoint` | 将 WAL 写入主库并截断，释放磁盘空间 | WAL 文件过大 |
| `reconnect` | 关闭当前 DB 连接，下次查询自动重建 | 需要释放 WAL 快照以允许 checkpoint 截断 |

- 日常维护建议: 每次大同步后跑 `analyze` → `optimize_fts` → `checkpoint`
- 如果 checkpoint 报 busy 或无法截断，先 `reconnect` 释放旧连接持有的 WAL 快照，再重试 `checkpoint`

---

## Workflow Recipes — 实战组合

### Workflow 1: 每日情报巡查

**目标: 快速了解过去 24 小时发生了什么**

```
Step 1: get_stats()
  → 看全局数字有无异常

Step 2: search_signals(since="2026-04-28T00:00:00Z", sort="updated", limit=20)
  → 浏览最近更新的 signals

Step 3: get_signal_changes(since="2026-04-28T00:00:00Z", meaningful_only=true)
  → 看哪些 issue 发生了状态变更

Step 4: 对感兴趣的 signal 用 get_signal_detail() 深入查看
```

### Workflow 2: ROCm 专题跟踪

**目标: 聚焦 ROCm 相关的 open issues 和 PR**

```
Step 1: search_signals(labels="rocm", state="open", sort="updated")
  → 所有带 rocm 标签的 open items

Step 2: search_signals(query="ROCm hip", source_types="github_issue", state="open")
  → FTS 搜索补充，找标签遗漏的

Step 3: execute_sql("SELECT author, COUNT(*) FROM signals WHERE ... GROUP BY author")
  → 看谁在贡献 ROCm 相关代码

Step 4: 对重点 PR 用 get_signal_detail() 看 review 讨论
```

### Workflow 3: 数据刷新 + 维护

**目标: 同步最新数据并确保数据库健康**

```
Step 1: trigger_sync_all()
  → 触发全库增量同步（仓库列表来自 config/sources.yaml）

Step 2: sync_status()  (等几分钟后)
  → 确认同步完成

Step 3: db_maintain(action="analyze")
  → 更新查询计划统计

Step 4: db_maintain(action="optimize_fts")
  → 合并 FTS 索引段

Step 5: db_maintain(action="reconnect")
  → 释放旧连接的 WAL 快照

Step 6: db_maintain(action="checkpoint")
  → 收缩 WAL 文件

Step 7: db_health()
  → 确认一切正常
```

### Workflow 4: 用自然语言直接问

**目标: 你不需要记参数——直接问 Cursor Agent 就行**

```
你说: "vllm 里 DeepSeek 相关的 open PR 有哪些？"
Agent: 调用 search_signals(query="DeepSeek", repos="vllm-project/vllm",
       source_types="github_pr", state="open")

你说: "给我看 #41016 的讨论内容"
Agent: 调用 get_signal_detail(signal_id="github:vllm-project/vllm:pr:41016")

你说: "过去一周谁提交的 PR 最多？"
Agent: 调用 execute_sql(sql="SELECT author, COUNT(*) ...")

你说: "帮我刷新一下数据"
Agent: 调用 trigger_sync_all() → sync_status()

你说: "数据库 WAL 太大了，清理一下"
Agent: 调用 db_maintain(action="reconnect") → db_maintain(action="checkpoint") → db_health()
```
