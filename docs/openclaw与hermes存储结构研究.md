# openclaw 与 hermes 的会话存储设计：「会话即记录」的另一条路（附 picoclaw / crush 对照）

> 前面拆过 pi、Claude、Codex、OpenCode、dsh、omp 六款工具，看到"纯文件 / SQLite 混合 / 事件溯源"三种流派——它们的**会话内容大多留在文件里**（.jsonl），数据库只做索引或元数据。而 **openclaw** 和 **hermes** 选了**相反的路**：把会话**整个搬进数据库**，做成可 SQL 查询的强类型记录。
>
> 这篇文章拆解这两个"会话即记录"的代表：一个是**多 Agent 编排平台**（110 张表的状态机库），一个是**重结构化的会话记账本**（sessions 表 62 列 + 双全文索引）；同时补上 **picoclaw**（摘要+双 FTS5 索引层）和 **crush**（轻量会话落库）做对照——它们落在同一条"会话即记录"连续谱的不同位置。附逆向方法与膨胀预测。

---

## 1. 为什么值得研究：同一个问题的两种答案

前六款工具的默认答案是"**会话即文件**"——一条会话一个 `.jsonl`，透明、可 diff、可迁移。但文件派有个代价：**无结构化索引**，想"查上个月所有 cost>1$ 的会话"只能扫文件。

openclaw 和 hermes 给出第二种答案"**会话即记录**"：会话是数据库里的一行，可 SQL 查询、可统计、可按任意列过滤。代价是**放弃文件的透明性**，schema 变宽、写放大集中在数据库。

| 维度 | 会话即文件（前六款为主） | 会话即记录（openclaw/hermes） |
|---|---|---|
| 会话载体 | `.jsonl` 文件 | DB 表的一行 |
| 可读性 | 直接 cat / diff | 需 SQL 查询 |
| 可迁移 | 拷文件即可 | 需导出 DB |
| 可查询 | 扫文件 | 任意列 WHERE |
| 结构化程度 | 低（无 schema） | 高（宽 schema + 索引） |

---

## 2. 知识点主线：四个"会话即记录/索引"的设计

### 2.1 openclaw：110 张表的编排状态机

```
~/.openclaw/
  openclaw.json              ← 配置（models/plugins/agents/gateway/skills）
  openclaw.json.bak[.1..4]   ← 配置多版本备份（可回滚）
  state/openclaw.sqlite      ← 核心状态库（110 张表）
  state/openclaw-quarantine.sqlite
  agents/ credentials/ workspace/ media/ logs/ cache/
  gateway.cmd / gateway.vbs  ← 网关启动
  config-journal-fingerprint.key
```

**数据库是"多 Agent 编排状态机"**，核心概念各占一组表：

| 概念 | 代表表 | 说明 |
|---|---|---|
| 会话 | `acp_sessions` / `session_groups` / `session_state_heads` | ACP 协议会话、分组、状态链 |
| 会话放置 | `worker_session_placements` | 会话被"放置"到哪个 worker、执行模式、恢复错误、transcript 同步游标 |
| 任务 | `task_runs` | task_kind / requester / parent_task / child_session / execution_owner |
| 子 Agent | `subagent_runs` | 子 Agent 执行记录 |
| transcript | `worker_transcript_commits` / `worker_transcript_commit_heads` | transcript 分块提交 + 提交头游标 |
| 推理 | `worker_inference_turns` | 每轮推理 |
| 工具 | `worker_turn_tool_authorities` / `worker_session_tool_operations` | 工具授权、操作 |

**关键设计**：`worker_session_placements` 有 `session_id / agent_id / session_key / execution_mode / state / worker_bundle_hash / last_transcript_ack_cursor / recovery_error`——说明**会话不绑定进程**，而是"放置"到某个 worker，可迁移、可恢复、有 transcript 同步游标。这是典型的**分布式编排**模型。

**配置**：JSON 顶层 `models / plugins / agents / gateway / skills`，带 **5 版本备份**（`.bak` ~ `.bak.4`）+ `config-journal-fingerprint.key` 校验——配置是**版本化、可回滚**的。

### 2.2 hermes：62 列会话记账本

```
%LOCALAPPDATA%\hermes\
  state.db           ← 核心库：sessions(62列) + messages(30列) + FTS5 双索引
  shared-state.db / projects.db   ← 共享状态 / 项目库
  sessions/          ← 空目录（会话不在文件里，在 DB）
  runtime/active_sessions.json + .lock   ← 运行态（与持久态分离）
  terminal-sessions/wt_session-*   ← 终端会话
  config.yaml / auth.json / .env   ← YAML 配置 + 凭证
  audio_cache/ image_cache/ cache/ backups/ cron/ hooks/
```

**`sessions` 表 62 列**，几乎把会话所有可量化信息都落列：

- **标识**：`id / source / user_id / session_key / chat_id / thread_id / display_name / parent_session_id`
- **模型**：`model / model_config / system_prompt / system_prompt_hash`
- **时间**：`started_at / ended_at / end_reason / last_activity_at`
- **计量**：`message_count / tool_call_count / input_tokens / output_tokens / cache_read_tokens / cache_write_tokens / api_call_count`
- **成本**：`billing_* / estimated_cost_usd / actual_cost_usd / cost_status / pricing_version`
- **上下文压缩**：`compression_failure_cooldown_until / compression_fallback_streak / compression_ineffective_count / compression_recovery_deadline / compression_overload_streak`
- **环境**：`cwd / git_branch / git_repo_root / git_metadata_generation`
- **状态**：`title / archived / auto_archived / pinned / hidden / rewind_count / profile_name / transport_profile`

**`messages` 表 30 列**，同样重结构化：
- `id / session_id / role / content / tool_calls / tool_name / effect_disposition`
- `timestamp / token_count / finish_reason`
- `reasoning_content / reasoning_details / codex_reasoning_items / codex_message_items`（**推理内容单独成列**）
- `_compressed_summary / active / compacted / api_content`（**压缩摘要**）
- `display_kind / display_metadata / display_order / message_uid / absorbed_message_uids`（**展示层元数据**）

**最特别的是双 FTS5 索引**：`messages_fts`（标准全文索引）+ `messages_fts_trigram`（**trigram 三重字符索引**，适合模糊/子串搜索）——比 omp 的 FTS5 还多一层 trigram，把"搜索体验"做到极致，代价是索引体积与写放大。

**配置**：YAML，头注释"由 DSH 自动迁移生成"——hermes 是 **DSH 生态**成员，配置可被 DSH 管理/迁移；`model.provider: custom`（本地网关）+ `providers` 多上游 fallback。

### 2.3 picoclaw：摘要+双 FTS5 的索引层（Seahorse 上下文管理器）

```
/root/.picoclaw/
  config.json              ← 配置（model_list 是 provider 模型注册表 + agents.defaults.context_manager="seahorse"）
  workspace/sessions/seahorse.db  ← 会话库（19 表）
  launcher-auth.db / logs/ / workspace/
```

**数据库是"摘要 + 全文索引"层**（Seahorse 上下文管理器开启时）：

| 表 | 列数 | 说明 |
|---|---|---|
| `conversations` | 4 | conversation_id / session_key / created_at / updated_at |
| `messages` | 8 | message_id / conversation_id / role / content / model_name / reasoning_content / token_count / created_at |
| `context_items` | 7 | conversation_id / ordinal / item_type / summary_id / message_id / token_count / created_at（**摘要/消息混排**） |
| `summaries` | 13 | summary_id / conversation_id / kind / depth / content / token_count / earliest_at / latest_at / descendant_count / descendant_token_count / source_message_token_count / model / created_at（**压缩摘要树**） |
| `messages_fts` + `summaries_fts` | — | **双 FTS5**（各带 5 影子表）——消息全文 + 摘要全文 |

**关键设计**：`context_items` 把消息和摘要**混排成一个有序列表**（item_type=summary/message，ordinal 排序）——这是"摘要压缩"的实现：长会话被压成摘要树（`summaries` 有 depth/descendant_count），搜索时**消息 + 摘要都能 FTS5 全文命中**。会话**本体仍在文件**（.jsonl），seahorse 只做**摘要 + 索引**。

### 2.4 crush：轻量会话落库（7 表）

```
/root/.crush/
  crush.db              ← 核心库（7 表）
  providers.json / hyper.json / 配置 crushrc
```

**数据库把会话记成轻量行**：

| 表 | 列数 | 说明 |
|---|---|---|
| `sessions` | 12 | id / parent_session_id / title / message_count / prompt_tokens / completion_tokens / cost / updated_at / created_at / summary_message_id / todos / channel |
| `messages` | 14 | id / session_id / role / parts / model / created_at / updated_at / finished_at / provider / is_summary_message / prism_model_id / prism_model_name / prism_hypercredit_savings / prism_dollar_savings |
| `files` / `read_files` | — | 会话引用的文件 / 已读文件 |
| `mcp_enabled_servers` / `mcp_disabled_servers` | — | MCP 服务器开关 |
| `goose_db_version` | — | 迁移版本（goose） |

**关键设计**：`sessions` 有 **message_count / prompt_tokens / completion_tokens / cost**（计量）+ `summary_message_id`（**摘要消息指向**——长会话压缩成一条摘要消息，存 messages 表）+ `todos`（待办）+ `channel`（渠道）。messages 有 `parts`（多段内容） + `is_summary_message`（标识摘要消息）+ `prism_*`（成本节省记账）。**这就是 hermes 的轻量版**：会话落库可 SQL 查询，但列少、无 FTS——要搜内容得用 `messages.parts`。

---

## 3. 横向对比（设计维度）

| 设计维度 | openclaw | hermes | picoclaw | crush | 前面六款 |
|---|---|---|---|---|---|
| 存储引擎 | SQLite 单库（110 表） | SQLite 多库 | SQLite（seahorse 19 表） | SQLite（7 表） | 纯文件 / SQLite / 事件溯源 |
| 会话位置 | DB 表（acp_sessions） | DB 表（sessions 62 列） | **文件 .jsonl + DB 摘要索引** | DB 表（sessions 12 列） | 多为文件（.jsonl） |
| 会话组织 | worker 放置/分组 | DB 内 + 活跃运行时 | 文件 + context_items 混排 | DB 内 + messages 行 | 目录 / 日期 / 表 |
| 配置格式 | JSON（5 版本备份） | YAML | JSON | 配置层（crushrc）+ JSON | JSON / YAML / TOML |
| 全文搜索 | 无（状态机为主） | **FTS5 + trigram 双索引** | **双 FTS5（消息+摘要）** | 无 | 仅 omp 有 FTS5 |
| 特色 | 多 Agent 编排、transcript 分块提交 | token/成本/压缩全量记账 | **摘要压缩树 + 消息/摘要双搜** | **轻量会话计量（cost/todos/summary）** | — |

---

## 4. 设计权衡：会话存储是连续谱，不是二选一

把前六款 + 这四款放在一条谱上：

| 流派 | 工具 | 会话载体 | 本质 | 代价 |
|---|---|---|---|---|
| 纯文件派 | pi / Claude / dsh | `.jsonl` 文件 | 会话即文件 | 无索引，搜索靠扫文件 |
| SQLite+文件派 | omp / Codex | `.jsonl` + DB 索引 | 文件保留，DB 管元数据 | WAL 写放大，不可直接读 |
| 摘要索引派 | **picoclaw** | `.jsonl` + DB 摘要/双 FTS5 | 文件保留，DB 管**摘要树+全文** | 摘要树写放大（每轮压缩） |
| 事件溯源派 | OpenCode | 库内 event 行 | 一切动作 append 成 event | event 只增不减，必然膨胀 |
| **会话即记录派** | **openclaw / hermes / crush** | **DB 表行** | **会话整个进 DB，可 SQL 查询** | **schema 宽、写放大集中 DB** |

**关键洞察**：会话存储**不是"文件 vs 库"二选一**，而是**连续谱**——从"文件（0 结构化）"→"文件+索引"→"摘要索引"→"全量记录（max 结构化）"。openclaw / hermes / crush 站在谱的最右端，把可查询性拉到最大；代价是放弃文件透明性 + 宽 schema。

**三者内部也相反**：
- openclaw 的会话表（`acp_sessions`）只有 **13 列**——它把重心放在**编排状态**（worker 放置/transcript 同步），会话内容不在这。
- hermes 的会话表 **62 列**——它把重心放在**会话记账**（token/成本/压缩/环境全量落列），是真正的"会话即记录"。
- crush 的会话表 **12 列**——**轻量记账**（message_count/tokens/cost/summary_message_id/todos/channel），是 hermes 的瘦身版。
- picoclaw 站在**中间**：会话还在文件（.jsonl），但 DB 里做**摘要压缩树 + 双 FTS5**（消息+摘要都可全文搜）——比"SQLite+文件派"（omp/Codex）更重的索引，但会话本体没进库。

---

## 5. 我的探索贡献与路径：逆向这些结构的通用方法

结论不是看文档来的，是**逐个扒开存储目录逆向**出来的。方法同前（定位 → 形态判断 → 只读开库 → 按表/列找意图），本次补充三条：

1. **定位要跨生态**：openclaw 在 `~/.openclaw`；hermes 在 `%LOCALAPPDATA%\hermes`（Windows 标准用户目录）。**注意 hermes 是 DSH 生态**，配置可能由 DSH 迁移生成——看 `config.yaml` 头注释能发现来源，别当成独立工具。
2. **只读开库 + 列数即结构化程度**：`sqlite3 "file:...?mode=ro" ".tables"`。表数说明规模（openclaw 110 表）；**列数说明结构化程度**（hermes sessions 62 列 = 重记账）。`PRAGMA table_info("sessions")` 直接看列清单。
3. **表名反推设计意图**：
   - 表名含 `worker_/placement/transcript/subagent` → **分布式多 Agent 编排**（openclaw）
   - 表名含 `_fts` / `_fts_trigram` → **全文 + 模糊搜索**（hermes）
   - 列名含 `compression_/rewind/absorbed` → **压缩/回滚状态机**（hermes）

**本次关键观察**：hermes 的 `messages_fts_trigram`（trigram 索引）做**模糊子串搜索**，前六款里没见过——这是把"搜索体验"做到极致的标志；而 openclaw 用 `worker_session_placements` 的 turn_claim 机制（带复杂 CHECK 约束）实现**会话的 worker 接管/迁移/恢复**，也是前六款没有的。

---

## 6. 结论与选型建议

### 6.1 核心结论（浓缩）

- **openclaw**：多 Agent 编排平台，110 张表**状态机库**，会话靠 worker 放置/可迁移/可恢复，transcript 分块提交；配置 JSON 版本化备份。
- **hermes**：CLI Agent 的**会话记账本**，sessions 62 列 + messages 30 列，token/成本/压缩/环境/展示全量落列，配 **FTS5 + trigram 双索引**；YAML 配置由 DSH 生态迁移管理。
- **picoclaw**：会话**仍在文件**（.jsonl），Seahorse 上下文管理器在 DB 里做**摘要压缩树 + 双 FTS5**（消息 + 摘要都可全文搜）——比 omp/Codex 的"文件+索引"更重，但会话本体没进库。
- **crush**：**轻量会话落库**，sessions 12 列（message_count/tokens/cost/summary_message_id/todos/channel）+ messages 14 列（parts/is_summary_message/prism_*），无 FTS——hermes 的瘦身版。
- **两者共同点**：会话**全在 SQLite**（放弃"会话即文件"透明性），换来**强类型 SQL 查询**与（hermes 的）**极致搜索**。
- **和前面六款对照**：它们把"会话即文件"当默认，openclaw/hermes/crush 选"会话即记录"，picoclaw 居中——**一条谱的两端**，前者透明可迁移，后者可查询可统计。

### 6.2 长期使用膨胀量预测（架构推断，非实测）

> 基于存储**架构原理**推断相对膨胀趋势（相同使用强度），非实测。**基准 = pi**（无压缩纯 JSONL 线性 = 1x）。

| 工具 | 膨胀机制 | 相对增长率（pi=1x） | 固定底座 | 清理需求 |
|---|---|---|---|---|
| openclaw | 会话内容不落库（仅编排元数据 1 行级） | **~0（会话存储）** | 中（110 表 schema） | 低（元数据极少） |
| hermes | 双 FTS5（unicode61 + trigram）写放大 + 62 列宽行 | **~2~3x** | 中 | **中高**（VACUUM + 清旧会话 + 重建索引） |
| picoclaw | 会话在文件 + DB 摘要树/双 FTS5（摘要写放大） | **~1.5~2x** | 中（19 表 + 2 FTS5） | **中**（清旧摘要 + VACUUM + 重建 FTS） |
| crush | 会话落库（12 列窄行 + 无 FTS） | **~1x** | 低（7 表） | 低（行少、无索引） |

**预测要点**：
1. **openclaw 的会话存储几乎不膨胀**——对话历史不落 SQLite，库只存编排状态/transcript 游标，行数极低；这是"会话即记录"的例外：**记录的是编排，不是对话**。
2. **hermes 最重**——双 FTS5 索引（尤其 trigram 写放大）+ 62 列宽行，是"会话即记录"里最"重"的形态，介于 omp（1.5~2x）与 OpenCode（3~5x）之间。
3. **picoclaw 居中**——摘要树每次压缩都写新行 + 双 FTS5 索引，比 omp/Codex 的"文件+索引"更重，但会话本体在文件（不随 DB 膨胀）。
4. **crush 最轻**——12 列窄行 + 无 FTS，几乎不额外膨胀（同 pi 量级），但代价是搜内容只能扫 messages.parts。
5. **清理建议**：hermes 需定期 `VACUUM` + 重建 FTS 索引；picoclaw 清旧摘要 + 重建 FTS；openclaw 几乎无需清会话（但编排状态机随 worker/transcript 增长，需监控）。

### 6.3 选型建议

| 需求 | 选型 |
|---|---|
| 会话透明、可迁移、可 diff | 纯文件派（pi / Claude / dsh） |
| 本地秒搜全部历史 + 索引 | SQLite 派（omp / Codex） |
| **摘要压缩 + 消息/摘要双全文搜** | **picoclaw** |
| 完整审计、可重放 | 事件溯源（OpenCode） |
| **多 Agent 编排、会话可迁移/恢复** | **openclaw** |
| **会话强类型查询、成本/压缩记账、模糊搜索** | **hermes** |
| **轻量会话计量（cost/todos/摘要），省空间** | **crush** |

---

## 7. 一句话总结

> 前六款工具把会话留成文件，**openclaw、hermes、crush 把会话整个搬进数据库**：openclaw 用 110 张表做**多 Agent 编排状态机**（会话靠 worker 放置/可迁移/可恢复，对话不落库）；hermes 用 **62 列会话 + 30 列消息 + FTS5/trigram 双索引**做**会话记账本**（token/成本/压缩/环境全量落列）；crush 用 **12 列会话 + 14 列消息**做**轻量记账**（cost/todos/summary，无 FTS）。而 **picoclaw 站在中间**：会话还在文件（.jsonl），DB 里做**摘要压缩树 + 双 FTS5**（消息+摘要都可全文搜）。会话存储是连续谱：从"文件"到"文件+索引"到"摘要索引"到"全量记录"，这四款分别落在谱的不同位置——要**可 SQL 查询/统计**选 openclaw/hermes/crush，要**摘要压缩+双搜**选 picoclaw，要**透明可迁移**回到文件派。

---

*参考版本：hermes 配置 `_config_version: 50`；openclaw `meta.lastTouchedVersion: 2026.9.9`（均在数据目录内提取，非官方版本号）。膨胀预测为架构推断，非实测。*
