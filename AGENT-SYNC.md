# agent-sync —— 目标机本机同步（无 SSH）

**发行物**：拷 `agent-sync/` 目录到目标机，填 `config/cpa-config.yaml` 的 `base_url` + `api_key`，跑脚本。
**无 SSH、无网络拉取**：脚本读本机配置 → 连 CPA 扫模型 → 写本机 agent 文件。

## 每台机怎么配（只需 base_url + api_key）

`config/cpa-config.yaml` 里 `targets` 只列**本机**的 agent：

```yaml
cpa:
  base_url: "http://192.168.31.213:8317/v1"   # 你的 CPA
  api_key: "123456"
  context_window: 256000
  max_tokens: 16384
  auto_compact_token_limit: 200000
strategy: replace
targets:
  - pi            # 本机有哪些 agent 就列哪些
  - opencode
  - claude
  - codex
```

| 机器 | targets |
|---|---|
| laptop | `pi` `opencode` `claude` `codex` |
| macos | `omp` |
| win11 | `omp` `dsh` |

## 用法（在目标机上）

```bash
./agent-sync.sh check      # 干跑，看会改什么
./agent-sync.sh apply      # 真正同步（自动备份）
./agent-sync.sh list       # 本机各 agent 状态
./agent-sync.sh doctor     # 诊断
```

Windows：`.\agent-sync.ps1 check` 等。

## 首次依赖

macOS/Linux：`python3 -m venv .venv && .venv/bin/pip install pyyaml`
Windows：`python -m venv .venv; .venv\Scripts\pip install pyyaml`

（无外网时，可离线装：laptop 下载对应平台的 `PyYAML` wheel 拷过去 `pip install --no-index --find-links <dir> PyYAML`）

## 本机适配器

- `pi`/`opencode`：本机 `~/.pi/agent/models.json` / `~/.config/opencode/opencode.json`
- `claude`/`codex`：本机 cc-switch（`~/.cc-switch/cc-switch.db`）
- `omp`：本机 `~/.omp/agent/models.yml`
- `dsh`：本机 `~/.dsh/profiles/web/cordis.patch.yml`

## 和 cpa-sync（中心模式）的区别

| | agent-sync（本机） | cpa-sync（中心） |
|---|---|---|
| 跑在哪 | 目标机 | 中心机 |
| 连接 | 无 SSH | SSH 进目标机 |
| 配置 | 本机 base_url+api_key | 中央配置 + 记忆各机 |
| 适用 | 发行/别人自部署 | 中心统一管理 |

两者共用同一套 `adapters/` 渲染逻辑，只是运行位置不同。
