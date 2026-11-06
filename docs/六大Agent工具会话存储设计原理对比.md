# 六大 AI Coding Agent 的会话存储设计：原理、权衡与逆向方法

> 你每天和 Coding Agent 对话，它把每句话、每个文件改动、每次模型调用都存了下来。但你知道它是**怎么存**的吗？pi、Claude、Codex、OpenCode、dsh、omp 六款主流工具，存储设计居然分成了三种流派——纯文件、SQLite 混合、事件溯源。
>
> 这篇文章从存储设计的视角拆解这六款工具：**它们把会话和元数据放在哪、用什么格式、为什么这么设计、各自的膨胀元凶是什么**，并附上我逆向这些存储结构时总结的**可复用方法论**。

---

## 1. 为什么存储设计值得研究

Coding Agent 的核心资产是两类数据：

- **会话历史**（conversation history）：每轮对话、工具调用、文件读写、模型输出的完整记录
- **元数据**（metadata）：模型目录、Provider 配置、用量统计、任务/目标、认证凭证

存储设计直接决定四个工程指标：

| 指标 | 说明 |
|---|---|
| **启动开销** | 模型/会话列表加载快不快 |
| **膨胀速度** | 用得越久体积涨多快 |
| **可搜索性** | 能否按关键字/时间/项目捞会话 |
| **可迁移/可清理** | 能否整体搬走、安全瘦身 |

六款工具恰好给出了**三种截然不同的设计流派**，下面逐个拆解。

---

## 2. 知识点主线：三种流派的存储设计

### 2.1 纯文件派：pi / Claude / dsh —— 会话就是文件，零数据库

**pi（开源，轻装版）**

```
~/.pi/agent/
  sessions/<工作目录>/<timestamp>_<uuid>.jsonl   ← 按工作目录分组
  models.json / settings.json / auth.json / mcp.json   ← 全部 JSON 配置
```

- **零数据库**：配置是 JSON 文件，会话是 JSONL（每行一条消息）。
- **懒加载元数据**：`models.json` 只存你**实际配置过**的模型，不缓存全量模型目录——用到哪个拉哪个。
- **会话按工作目录分组**：目录名是路径转义（`--home-a1--`），同一项目的历史聚在一起。
- **数据目录可外置**：agent 目录可软链到独立数据盘，主目录只留壳。

**Claude**

```
~/.claude/
  projects/<项目目录>/<session>.jsonl   ← 按项目目录分组
  history.jsonl / settings.json
  file-history/ session-env/ tasks/ paste-cache/ backups/
```

- **零数据库**：会话 = 一个 JSONL 文件，按项目目录分组。
- **会话 = 文件**：一条会话就是一个可读文件，直接看、直接 diff、直接搬。
- **辅助存储**：`file-history/`（文件修改历史）、`session-env/`（会话环境）、`tasks/`，按 session-id 平铺。

**dsh**

```
~/.dsh/
  sessions/<工作目录>/session-<uuid>/session.v4.jsonl.zstd   ← 每会话一个目录
  storages/ attachments/ profiles/ logs/ skills/ mcp/
```

- **每会话一个目录**：目录级别隔离，互不干扰。
- **zstd 压缩**：会话文件压缩存储（`.zstd`），是六款中**唯一用压缩**的。
- **按工作目录分组**：同 pi / Claude。

> **纯文件派共性**：会话即文件，透明、可 diff、可 git 化、易迁移；代价是**无结构化索引**，搜索退化为扫文件。

### 2.2 SQLite + 文件混合派：omp / Codex —— 数据库管元数据，会话文件保留

**omp（商汤定制打包版）**

```
~/.omp/agent/
  sessions/-tmp/<timestamp>_<uuid>.jsonl   ← 会话集中存放
  models.db      ← 模型目录物化缓存（核心）
  history.db     ← 历史 + FTS5 全文索引
  agent.db       ← agent 状态/用量
  skill-descriptions.db
  config.yml / models.yml   ← YAML 配置
```

- **多 SQLite 库分工**：models.db 管模型目录、history.db 管历史（带 **FTS5 全文索引**，搜索快）、agent.db 管状态与用量、skill-descriptions.db 管技能描述。
- **模型目录全量物化**：`model_cache` 表把接入的**所有 Provider 的所有模型**（含价格/上下文窗口/能力）一次性下载并落盘，每行一个 Provider 的完整清单。带 `static_fingerprint` / `materialization_policy` / `models_hash` 做**版本指纹增量刷新**——升级 app 版本时指纹不匹配，触发整行重物化（**替换型，不膨胀**）。
- **会话集中存放**：sessions 挤在 `-tmp/`，不按目录分（找项目靠 DB 索引，不靠文件树）。
- **YAML 配置**：config.yml / models.yml。

**Codex**

```
~/.codex/
  sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl  ← 按日期分层
  session_index.jsonl            ← 会话索引（id + name + updated_at）
  state_5.sqlite                 ← threads 元数据
  thread_history_1.sqlite        ← thread_items 消息历史
  goals_1 / logs_2 / queue_1 / memories_1 .sqlite
  config.toml                    ← TOML 配置
```

- **thread 是核心抽象**：Codex 把"会话"叫 thread。thread 的**元数据**在 `state_5.sqlite.threads`，**消息历史**在 `thread_history_1.sqlite.thread_items`，**原始消息流**在 `rollout-*.jsonl`——**一条会话横跨文件 + 两个库**。
- **会话按日期分层**：`sessions/YYYY/MM/DD/`，按时间归档而非按项目。配合 `session_index.jsonl` 做快速列举。
- **多库按职责拆**：goals（目标）、logs（日志）、queue（队列）、memories（记忆），各自独立 SQLite，迁移版本（`_sqlx_migrations`）内置。
- **TOML 配置**：config.toml。

> **SQLite 派共性**：用数据库给元数据/历史加**索引**、支持事务与增量更新；代价是 **WAL 写放大**、页级预分配、会话文件不可直接读。

### 2.3 事件溯源派：OpenCode —— 一个库装天下

```
~/.opencode/
  opencode.db           ← 一个库管所有项目与会话
  storage/ tool-output/ log/ repos/ snapshot/
```

- **事件溯源（Event Sourcing）**：所有动作 append 成 `event` 行（会话/工具调用/状态变化），是唯一事实来源。
- **分层聚合**：`message`（消息）聚合 event，`part`（消息内容分片）拆分大消息，`session` 只是轻量元数据表。
- **单库**：一个 opencode.db 管所有项目与会话，表按职责分区（event/message/part/session/todo/project）。
- **JSON 配置**：opencode.json。

> **事件溯源派特性**：可重放、审计完整、一条 event 流能重建任何状态、事务一致性高；代价是 **event 只增不减**，膨胀是设计使然。

---

## 3. 横向对比（设计维度）

| 维度 | pi | omp | Claude | Codex | OpenCode | dsh |
|---|---|---|---|---|---|---|
| 存储引擎 | 纯文件 | SQLite 多库 | 纯文件 | SQLite 多库 | 单库 SQLite | 纯文件 |
| 会话模型 | `*.jsonl` | `*.jsonl` | `*.jsonl` | rollout `*.jsonl` | 库内 session 表 | `*.jsonl.zstd` |
| 会话组织 | 按工作目录 | 集中目录 | 按项目目录 | 按日期 YYYY/MM/DD | 库内表 | 按工作目录 |
| 元数据格式 | JSON | YAML | JSON | TOML | JSON | YAML |
| 全文搜索 | 无（扫文件） | FTS5 索引 | 无（扫文件） | SQLite | event 索引 | 无（需解压） |
| 压缩 | 无 | 无 | 无 | 无 | 无 | **zstd** |
| 模型目录 | 懒加载（子集） | **全量物化** | — | SQLite | — | — |
| 主要膨胀源 | 会话文件数 | 全量模型+运行时 | 文件历史 | 多库+rollout | **event 事件流** | 无（最省） |
| 启动开销 | 极低 | 低（DB 索引） | 极低 | 低 | 中（大库） | 低 |
| 可迁移性 | 高（文件） | 中（DB+WAL） | 高（文件） | 中（多库） | 低（单大库） | 高（文件） |

---

## 4. 设计权衡：为什么这么设计

### 4.1 JSON 文件 vs SQLite
- **JSON/JSONL**：透明、可 diff、可 git 化、零依赖。代价是**无索引**，搜索退化为扫文件。
- **SQLite**：给元数据/历史加索引、支持事务与增量更新。代价是 **WAL 写放大**、页级预分配、文件不可直接读。

### 4.2 文件 vs 库 vs 事件溯源
- **文件**：易迁移、易导出，但文件多了难管理。
- **多库**：职责清晰、可迁移版本，但同步复杂。
- **事件溯源**：审计/重放最强，但**只增不减**，体积必然膨胀。

### 4.3 全量缓存 vs 懒加载（模型目录）——「省 vs 重」的真正分水岭
- **懒加载**（pi）：只存配置子集，体积小，代价是每次要拉模型清单。
- **全量物化**（omp）：把几千个模型提前拉回本地，启动秒选，代价是几 MB 固定缓存 + 指纹做增量刷新。
- **关键结论**：*看起来"SQLite 比 JSON 贵"，其实不是引擎贵，而是**缓存范围不同**——一个存 24 个你配的模型，一个缓存 3000+ 个全量模型。*

### 4.4 压缩 vs 可搜索
- **zstd 压缩**（dsh）：省空间，但搜索要先解压。
- **不压缩**（其余）：可流式搜索/追加，代价是体积大。

### 4.5 组织维度：目录 vs 日期
- **按目录**（pi / Claude / dsh）：适合"按项目找历史"。
- **按日期**（Codex）：适合"按时间回溯"。
- **按 DB 索引**（omp / OpenCode）：不依赖文件树，靠查询。

---

## 5. 我的探索贡献与路径：逆向这些存储的通用方法论

这些结论不是看文档看来的，是我**逐个扒开它们的存储目录**逆向出来的。方法可复用，五步走：

### 5.1 侦察定位
先找到每个工具的存储目录。多数工具按惯例放 `~/.<工具名>/`；但**很多部署会把数据目录软链到独立数据盘**（主目录只留一个符号链接，`ls -l ~/.pi/agent` 一看便知）。所以定位时先 `ls -l` 看是不是符号链接，跟着链接找真实数据。

### 5.2 形态判断
看目录树判断流派：
- 有 `.db` / `.sqlite` → SQLite 派
- 有 `.jsonl` / `.zstd` → 纯文件派
- 同时有 `.db` + `.jsonl` → 混合派
- 有 `.db-wal` / `.db-shm` → 该库开 **WAL 模式**（写放大，删除空间不立即归还）

```bash
ls -la ~/.pi/agent/          # 看顶层
find ~/.pi/agent -name "*.db*" -o -name "*.jsonl*" | head
```

### 5.3 打开数据库（务必只读）
用 `mode=ro` 避免对使用中的库触发写锁或修改。列出所有表 + 行数，**行数能判断用途**：0 行 = 预留 schema；几十行 = 配置/目录；几万行 = 消息/事件流。

```bash
sqlite3 "file:~/.omp/agent/models.db?mode=ro" ".tables"
sqlite3 "file:~/.omp/agent/models.db?mode=ro" "SELECT count(*) FROM model_cache;"
```

### 5.4 按表找膨胀元凶
用 SQLite 的 `dbstat` 虚拟表按表聚合占用，直接定位"哪个表吃掉了大半体积"（如 OpenCode 的 `event` 表）。若库未开 dbstat，退化为 `PRAGMA page_count/page_size` 或按行数估算。

```sql
SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC;
```

### 5.5 设计意图推断
看表/文件名反推设计取舍：
- 有 `static_fingerprint` / `materialization_policy` / `models_hash` 列 → **版本指纹增量物化**（升级触发重物化，替换型不膨胀）
- 有 `history_fts*` 表 → **FTS5 全文索引**
- 有 `event` 表且只增 → **事件溯源**
- 会话文件名带 `.zstd` → **压缩策略**
- 会话按 `YYYY/MM/DD` → **按时间归档**

**一个实测观察**：omp 的 `model_cache` 表只有 35 行（每个 Provider 一行，存完整模型清单），却缓存了 **3000+ 个模型**（含价格/上下文/能力），单行最大 2.7MB——这就是"全量物化缓存"的典型形态；对比 pi 的 `models.json` 只存了你配置的十几个模型。**这一下就看穿了"为什么一个几 MB、一个几 KB"。**

---

## 6. 结论与选型建议

### 6.1 核心结论（浓缩）

**六工具 = 三种设计流派：**

| 流派 | 工具 | 本质 | 代价 |
|---|---|---|---|
| 纯文件派 | pi / Claude / dsh | 会话就是文件，零数据库 | 无索引，搜索靠扫文件 |
| SQLite+文件派 | omp / Codex | 数据库管元数据+索引，会话文件保留 | WAL 写放大，不可直接读 |
| 事件溯源派 | OpenCode | 单库，一切动作 append 成 event | event 只增不减，必然膨胀 |

**最重要的一个洞察：**「省 vs 重」的真正分水岭不是 JSON vs SQLite，而是 **懒加载子集 vs 全量物化缓存**——pi 只存你配置的十几个模型（几 KB），omp 缓存 3000+ 个模型的完整目录（含价格/上下文/能力，单行 2.7MB）。不是引擎贵，是**缓存范围**不同。

### 6.2 长期使用膨胀量预测（架构推断，非实测）

> 基于各工具的存储**架构原理**推断相对膨胀趋势（相同使用强度），非实测。**基准 = pi**（无压缩纯 JSONL 线性：1 单位使用 → 1 单位存储，最自然的基线）。

| 工具 | 膨胀机制 | 相对增长率（pi=1x） | 固定底座（启动即占） | 清理需求 |
|---|---|---|---|---|
| pi | 纯 JSONL 线性 | **1x（基准）** | 极小 | 低（删旧会话文件） |
| dsh | zstd 压缩 | **~0.25x（最省）** | 极小 | 几乎不用 |
| Claude | JSONL + file-history/paste-cache | ~1.2x | 小 | 低 |
| omp | FTS5 索引(2~3x) + WAL + 会话文件 | ~1.5~2x | **大**（模型缓存+原生+浏览器，固定） | 中（清旧会话 + VACUUM） |
| Codex | 跨 rollout+thread_items+索引 三处写 + 多库 WAL | ~2~3x | 小 | 中高（多库分别清） |
| OpenCode | event 只增不减、细粒度 | **~3~5x+（超线性）** | 中 | **高**（需按时间归档 event） |

**预测要点：**

1. **基准意义**：pi = 1x 是无压缩的线性基线；**dsh 因为 zstd 压到 ~0.25x，是唯一长期低于基线、几乎不膨胀的**。
2. **增量排名（pi=1x）**：OpenCode(~3~5x+) > Codex(~2~3x) > omp(~1.5~2x) > Claude(~1.2x) > pi(1x) > dsh(~0.25x)。
3. **区分「固定底座」与「增量」**：omp 启动就占大头（浏览器+原生+模型缓存固定，不随使用涨），但**增量中等**；OpenCode 启动小，但**增量最猛**。
4. **短期 vs 长期**：短期看 omp 最大（固定底座重）；**长期看 OpenCode 反超**（event 超线性增长，1 条消息拆成多条 event + part）。
5. **最需要定期维护的是 OpenCode**：event 表只增不减，必须按时间分区/归档压缩；dsh 几乎零维护。

### 三种流派的适用场景
| 流派 | 代表 | 适合 |
|---|---|---|
| 纯文件 | pi / Claude / dsh | 重视透明、可迁移、可导出分享 |
| SQLite+文件 | omp / Codex | 重视搜索、规模、结构化查询 |
| 事件溯源 | OpenCode | 重视审计、可重放、一致性 |

### 按需求选型
- **"所见即所得、文件即会话"** → Claude / pi（纯 JSONL）
- **"本地秒搜全部历史"** → omp / Codex（SQLite + 索引）
- **"最省空间/会话隔离"** → dsh（zstd）
- **"完整审计/可重放"** → OpenCode（事件溯源）

### 清理策略（按流派）
- **纯文件派**：删旧 `.jsonl` 即瘦身，安全。
- **SQLite 派**：清旧 session 文件 + `VACUUM` 回收页；WAL 需 checkpoint。
- **事件溯源派**：event 表需按时间分区/归档压缩，否则只增不减。
- **模型缓存**：懒加载（pi）天然小；全量物化（omp）是固定天花板，靠版本指纹刷新，不必手动清。

---

## 7. 一句话总结

> 六款工具 = 三种设计流派：
> **pi / Claude / dsh** 用文件存会话（dsh 额外 zstd 压缩，最省空间）；
> **omp / Codex** 用 SQLite 管元数据+索引、会话文件保留（Codex 按日期分层、一条会话跨文件+双库）；
> **OpenCode** 用单库事件溯源，一切动作 append 成 event 流。
>
> 存储"省"与"重"的真正分水岭不是 JSON vs SQLite，而是 **懒加载子集 vs 全量物化缓存**，以及是否打包重型运行时。选型看你要的是**透明可迁移**（文件）、**可搜索**（SQLite）还是**可重放审计**（事件溯源）。

---

*参考版本：pi 1.4.0、omp 18.16.0（实测）；其余工具版本未在本次数据目录中提取到，如有出入以官方为准。*
