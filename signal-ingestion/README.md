# AI Infra Gap Intelligence — Signal Ingestion & Storage

> AMD vs NVIDIA 竞争情报追踪系统的数据底座。
> Module 1 (Signal Ingestion) + Module 3 (Central Storage)。

## 当前数据

| Repo | Signals | Comments | 时间跨度 |
|---|---|---|---|
| vllm-project/vllm (label:rocm) | 2,277 | 11,202 | 2023-10 ~ 2026-04 |
| sgl-project/sglang (label:amd) | 640 | 2,298 | 2024-10 ~ 2026-04 |
| **总计** | **2,917** | **13,500** | |

## 30 秒上手

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
echo "GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxx" > .env   # 换成你的 token
.venv/bin/python -m pytest tests/ -q                    # 验证所有测试通过
```

首次拉数据：

```bash
.venv/bin/python scripts/init_db.py
.venv/bin/python scripts/sync_github.py \
    --repo vllm-project/vllm --labels rocm --mode full
```

## 日常运维

### 手动触发一次拉取

```bash
# 增量（默认模式，只拉上次 sync 后有变化的）
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm

# 全量（忽略 last-sync 时间戳，从头拉）
.venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full

# sglang
.venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd
```

### 定时自动拉取（crontab）

运行 `crontab -e`，添加以下行（路径按实际调整）：

```cron
# vllm: 每 2 小时增量同步（将 <project-root> 替换为实际项目路径）
0 */2 * * * cd <project-root> && .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm >> data/sync.log 2>&1

# sglang: 每 4 小时增量同步
0 */4 * * * cd <project-root> && .venv/bin/python scripts/sync_github.py --repo sgl-project/sglang --labels amd >> data/sync.log 2>&1

# vllm: 每天凌晨 2 点全量同步
0 2 * * * cd <project-root> && .venv/bin/python scripts/sync_github.py --repo vllm-project/vllm --labels rocm --mode full >> data/sync.log 2>&1
```

### 搜索和查看

```bash
# FTS5 全文搜索
.venv/bin/python scripts/search_signals.py --query "aiter MLA"

# 按 repo + state + label 过滤
.venv/bin/python scripts/search_signals.py --repo vllm-project/vllm --state open --labels rocm

# 查看单条 signal 详情（body + comments）
.venv/bin/python scripts/search_signals.py --detail github:vllm-project/vllm:issue:39303

# JSON 格式输出（方便 pipe 给其他工具）
.venv/bin/python scripts/search_signals.py --query "ROCm" --json
```

搜索语法说明：

- 保留并直传给 FTS5：`AND` / `OR` / `NOT`、`"phrases"`、`title:` / `body:` / `tags:`、`-title:`、`prefix*`、`^anchor`、`NEAR(a b, N)`。
- 用户友好容错：大小写列限定如 `TITLE:ROCm` / `Body:ROCm` 会保留原义；已知列限定的常见 typo，如 `title::ROCm`、`title: :ROCm`，会被规范化为 `title: ROCm`，而不是直接报错。
- 普通文本不会被误当成列限定：像 `subtitle:ROCm`、URL、`key:value`、`ROCm,CUDA` 会清理成普通词搜索，不会触发 `no such column`。
- 已知限制：`+` 不保留（例如 `foo + bar` 会退化为 `foo bar`）；裸 `NEAR/N` 会被简化；`NOT ROCm`、`ROCm OR NOT CUDA` 这类 FTS5 本身不支持的写法仍会返回错误。

```bash
# 查最近 24h 的有意义变更
.venv/bin/python scripts/show_changes.py --since 2026-04-24T00:00:00Z

# 查某条 signal 的变更历史
.venv/bin/python scripts/show_changes.py --signal-id github:vllm-project/vllm:issue:39303
```

### MCP 查询服务

dbhub 提供 6 个 MCP 工具给 Agent 做 ad-hoc 查询：`execute_sql`、`search_objects`、`search_signals`、`get_signal_detail`、`get_signal_changes`、`get_gap_signals`。全部只读。

**环境要求**：Node.js >= 24（`ssh-config` 依赖需要 ESM 支持）。首次安装：

```bash
npm install -g @bytebase/dbhub@latest  # 全局安装，自动编译 better-sqlite3
```

#### Cursor 配置

将以下加入 `~/.cursor/mcp.json` 的 `mcpServers` 里，重启 Cursor。注意：不能用 `npx`——Cursor 内置 Node v20 不支持 ESM，需指向 nvm v24+ 的 node 绝对路径。

获取占位符的实际值：

```bash
which node          # → <node-bin>，如 /home/user/.nvm/versions/node/v24.x/bin/node
npm root -g         # → <global-modules>，如 /home/user/.nvm/versions/node/v24.x/lib/node_modules
```

将 `<node-bin>`、`<dbhub-entry>`、`<project-root>` 替换为实际路径：

```json
"signals-db": {
  "command": "<node-bin>",
  "args": [
    "<dbhub-entry>",
    "--transport", "stdio",
    "--config", "<project-root>/dbhub.toml"
  ],
  "cwd": "<project-root>"
}
```

其中 `<dbhub-entry>` = `<global-modules>/@bytebase/dbhub/dist/index.js`。

> **重要**：`cwd` 必须设为项目根目录，因为 `dbhub.toml` 中的 DSN 使用相对路径 `data/signals.db`。缺少 `cwd` 会导致 dbhub 找不到数据库。

#### Claude Code 配置

**方式 A（项目级）**：在项目根目录创建或编辑 `.mcp.json`：

```json
{
  "mcpServers": {
    "signals-db": {
      "command": "bash",
      "args": ["-c", "cd <project-root> && npx -y @bytebase/dbhub@latest --transport stdio --config dbhub.toml"]
    }
  }
}
```

> Claude Code project-level config runs from the project root, so `cd <project-root>` ensures the relative DSN in `dbhub.toml` resolves correctly.

**方式 B（全局）**：`claude mcp add` 命令行添加：

```bash
claude mcp add signals-db \
  --command "bash -c 'cd <project-root> && npx -y @bytebase/dbhub@latest --transport stdio --config dbhub.toml'"
```

> Global mode does not guarantee cwd. The `cd` wrapper ensures dbhub starts in the project root.

添加后在 Claude Code 里输入 `/mcp` 可验证 `signals-db` 是否已连接。

#### 其他 MCP Client（Cline、OpenCode、自建等）

所有支持 MCP stdio 协议的 client 都能用。**注意**：`dbhub.toml` 使用相对 DSN `data/signals.db`，启动时必须确保工作目录为项目根：

```bash
cd <project-root> && npx -y @bytebase/dbhub@latest --transport stdio --config dbhub.toml
```

每个 client 的配置文件格式不同，但原理一样：用 `cd` 或 `cwd` 配置保证工作目录，再指定 `command` + `args`。

#### 远程 HTTP 模式

如果 Vivi / Zijun 在其他机器上，在数据库所在机器起 HTTP 服务：

```bash
npx @bytebase/dbhub@latest --transport http --port 8080 --config dbhub.toml
```

远程 MCP client 连接 `http://<your-ip>:8080/mcp`。

#### GitHub MCP（读代码文件、搜 issue 等）

Agent 如果需要**实时读代码文件**（不是我们 DB 里的 signal，是 GitHub 上的源码），需要 GitHub MCP：

- **Cursor**：`~/.cursor/mcp.json` 里加 `"github": {"url": "https://api.githubcopilot.com/mcp/"}`
- **Claude Code**：`claude mcp add github --url https://api.githubcopilot.com/mcp/`
- **其他 client**：参考 [github/github-mcp-server](https://github.com/github/github-mcp-server) 文档

### 常见问题排查

| 问题 | 原因 | 解决 |
|---|---|---|
| `database is locked` | 多个 sync 同时跑 | 确保同一时间只有一个 sync 进程 |
| `403 rate limit` | API 配额耗尽 | 等 1 小时自动重置，或检查 `.env` token |
| FTS5 搜索返回 0 | 关键词在 body 里不存在 | 换个关键词，或用 `--state open` 过滤 |
| signal_comments 为 0 | sync 时没加 `--include-comments` | 重跑一次加 `--include-comments` |
| `SyntaxError: Cannot use import statement outside a module` | Cursor 用了内置 Node v20 而非 nvm v24 | mcp.json 里 command 改成 node 绝对路径，见上方配置 |

## 给下游团队

### Vivi (Module 2 — Signal Classifier)

你的工作流：`get_feed(classified=False)` 拉未分类 signal → LLM 分类器打标签 → `update_classification()` 写回 DB。完整 7 步示例见 [`examples/module2_quickstart.py`](examples/module2_quickstart.py)，可直接运行。

### Zijun (Module 4 — Agent Workflow)

通过 MCP 或 Python API 查询 signal 数据。6 种查询模式的完整示例见 [`examples/module4_agent_queries.py`](examples/module4_agent_queries.py)。MCP 工具列表：

- `search_signals(query)` — FTS5 全文搜索
- `get_signal_detail(signal_id)` — 单条详情 + comments
- `get_signal_changes(signal_id)` — 变更历史
- `get_gap_signals(gap_id)` — gap 关联查询
- `execute_sql(sql)` — 任意只读 SQL

### Vincent (Module 5-6 — Dashboard)

Week 2 REST API 待做（FastAPI，接口已对齐 D3.3）。目前可以：

- 直接查 `data/signals.db`（SQLite WAL 模式，读写不互锁）
- 用 dbhub MCP 的 `execute_sql` 做 ad-hoc 查询

## 测试

```bash
.venv/bin/python -m pytest tests/ -q   # 验证所有测试通过
```

## 项目结构

```
├── src/
│   ├── ingestion/            # Module 1: GitHub 采集 + 标准化 + 变更检测
│   ├── storage/              # Module 3: SQLite + FTS5 + Repository + Search
│   └── sync/                 # 编排层: SyncOrchestrator
├── scripts/
│   ├── init_db.py            # 初始化 DB schema
│   ├── sync_github.py        # 触发 GitHub 同步
│   ├── search_signals.py     # CLI 搜索
│   └── show_changes.py       # CLI 查看变更
├── examples/
│   ├── module2_quickstart.py # Vivi 接入示例（7 步）
│   ├── module4_agent_queries.py  # Zijun 查询示例（6 场景）
│   └── ops_cheatsheet.sh     # 运维命令速查
├── config/sources.yaml       # 数据源配置
├── dbhub.toml                # MCP 工具配置（6 个工具）
├── data/
│   ├── signals.db            # SQLite 数据库（7 表 + FTS5）
│   └── cache/                # JSON 缓存（原子写，可直接读）
```

## 深入文档

| 文件 | 内容 |
|---|---|
| `src/README.md` | 开发者详细文档（517 行）：模块职责、API 接口、前向扩展、内部约定 |
| `dbhub.toml` | MCP 工具配置（6 个工具：2 内置 + 4 自定义） |
