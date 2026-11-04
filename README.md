# cpa-sync —— CPA 配置同步

把 **CPA (cli-proxy-api)** 配置同步到所有 agent 应用。两套工具：

- **`cpa-sync`（中心模式）**：在中心机跑，中央配置为唯一事实来源，一键下发到各接入机（ssh/scp）。
- **`agent-sync`（客户端模式）**：拷到目标机跑，**无 SSH**，目标机自己连 CPA 拉模型、写本机 agent 文件。

**一句话**：中央配置 = 唯一事实来源；中心机用 `cpa-sync` 下发，各目标机也可用 `agent-sync` 自拉。

## 架构

```
             ┌─ cpa-sync sync ─┬─ pi.py       → 本机 ~/.pi/agent/models.json
             │                  ├─ opencode.py → 本机 ~/.config/opencode/opencode.json
             │                  ├─ claude.py   → cc-switch providers (app_type=claude)
             │                  ├─ codex.py    → cc-switch providers (app_type=codex)
config/cpa-config.yaml        ├─ omp.py      → 各机 ~/.omp/agent/models.yml (多主机)
(唯一事实来源)                 └─ dsh.py      → 各机 ~/.dsh/profiles/web/cordis.patch.yml
        │
        └─ 校验: GET /v1/models (CPA 能力检查，动态扫模型)

agent-sync (目标机自拉，无 SSH):
  目标机 config/cpa-config.yaml → 连 CPA 扫模型 → 写本机 agent 文件
```

## 已实现 adapter（6 个）

| adapter | 位置 | 说明 |
|---------|------|------|
| `pi` | 本机 `~/.pi/agent/models.json` | provider `cpa`，19 模型 |
| `opencode` | 本机 `~/.config/opencode/opencode.json` | provider `cpa`，19 模型 |
| `claude` | cc-switch `providers` 表 (claude) | **每条模型一条 provider**，notes=cpa，meta=openai_chat |
| `codex` | cc-switch `providers` 表 (codex) | 每条模型一条 provider，notes=cpa，wire_api=responses |
| `omp` | 各机 `~/.omp/agent/models.yml`（多主机，见 adapter_instances） | 收拢到 providers.cpa |
| `dsh` | 各机 `~/.dsh/profiles/web/cordis.patch.yml`（多主机） | provider `cpa` |

**claude/codex 风格**（用户明确）：同一供应商（CPA）下，**每个模型单独一条 provider**，全部指向 CPA，
`name=模型名`、`notes=cpa`，像以前"小水管"那样。claude 用根路径 base_url（claude 自己拼 `/v1/messages`），
codex 用带 `/v1` 的 base_url（OpenAI 格式）+ `wire_api=responses`。

## 用法

```bash
cd /mnt/shared/cpa-sync
./cpa-sync models                  # CPA 暴露的全部模型
./cpa-sync list                    # 各 adapter 当前状态
./cpa-sync check [adapter...]      # dry-run：只对比不写
./cpa-sync apply [adapter...] -y   # 真正同步（自动备份）
./cpa-sync sync  [adapter...] -y   # 交互式同步向导
./cpa-sync doctor                  # 诊断连接/配置
./cpa-sync --version               # 版本
```

## 中央配置（`config/cpa-config.yaml`，真实值，不进仓库）

> 真实配置 `config/cpa-config.yaml` 已被 .gitignore 排除；仓库只带脱敏模板 `config/cpa-config.example.yaml`。
> 新机器：`cp config/cpa-config.example.yaml config/cpa-config.yaml` 再填真实值。

```yaml
cpa:
  base_url: "http://YOUR_CPA_IP:8317/v1"   # CPA 网关（换成你的）
  api_key: "YOUR_CPA_API_KEY"
  include_models: []        # 空 = 动态扫 /v1/models，扫出什么配什么
  exclude_models: []        # 通配符排除
  context_window: 512000    # 所有下游统一（pi/opencode/dsh/omp 的 contextWindow + codex 的 model_context_window）
  max_tokens: 16384
  auto_compact_token_limit: 300000   # codex 的 model_auto_compact_token_limit
strategy: replace            # replace = 全盘对齐，不留旧模型
```

**统一管理**：`context_window` / `auto_compact_token_limit` 由中央配置控制，改一处 → `cpa-sync sync` → 所有 agent 统一。

## 关键设计

- **中央配置 = 唯一事实来源**：改 `config/cpa-config.yaml`，一键下发
- **适配器模式**：新接入系统 = 新增 `adapters/<name>.py` + 子类化 `Adapter`，自动发现
- **跨平台**：本机直读写，win11 走 `ssh/scp`（scp 原始字节 + 原子替换，避免编码问题）
- **幂等 + 备份**：写入前自动备份 `.bak.<时间戳>`；内容无变化不写
- **写文件安全**：scp 上传临时文件 → 远端原子替换，失败不会清空原文件
- **cc-switch 切换式**：claude/codex 走"推翻再来"（每次全盘重建 provider 集合，不做增量 diff）

## 两套工具分工

| | `cpa-sync`（中心） | `agent-sync`（客户端） |
|---|---|---|
| 跑在哪 | 中心机 | 目标机 |
| 连接 | SSH 进目标机 | 无 SSH |
| 配置 | 中央配置 + 记忆各机 | 本机 base_url + api_key |
| 适用 | 中心统一管理 | 发行 / 自部署 |

> 两者共用同一套 `adapters/` 渲染逻辑，只是运行位置不同。详见 `AGENT-SYNC.md`。

## 目录

```
cpa-sync/
├── cpa-sync              # CLI 入口（中心模式）
├── agent-sync            # CLI 入口（客户端模式，无 SSH）
├── agent-sync.sh/.ps1    # 客户端启动器（macOS/Linux / Windows）
├── core/                 # ir / cpa_client / config / remote
├── adapters/             # base + pi/opencode/claude/codex/omp/dsh
├── config/cpa-config.yaml# 中央配置（中心模式）
└── tests/                # 单元测试
```

## 开发

```bash
python3 -m pytest tests/    # 跑测试
```

新 adapter：继承 `adapters.base.Adapter`，实现 `load_current / render / write`，注册到 `adapters/__init__.py`。

## GUI（PySide6）

```bash
./cpa-sync-gui          # 启动图形界面
```

GUI 是 CLI 的图形外壳（复用全部 cmd_* 逻辑，零重复）：
- **📡 模型**：扫描 CPA 当前模型
- **📋 状态**：各 agent 当前配置
- **🔍 检查(干跑)**：看 diff 不改
- **🚀 同步**：一键下发
- **🩺 诊断**：doctor
- **🔄 重载配置**：重新读 cpa-config.yaml

依赖装在 `.venv/`（`PySide6-Essentials`，不含 WebEngine）。首次：
```bash
python3 -m venv .venv && .venv/bin/pip install PySide6-Essentials PyYAML
```
