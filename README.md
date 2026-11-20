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
(唯一事实来源)                 ├─ dsh.py      → 各机 ~/.dsh/profiles/web/cordis.patch.yml
                             ├─ picoclaw.py → meilan-phone1 ~/.picoclaw/config.json (多主机)
                             ├─ crush.py    → meilan-phone1 ~/.config/crush/crushrc (crushrc 配置层，多主机)
                             ├─ openclaw.py → win11 ~/.openclaw/openclaw.json (多主机)
                             └─ hermes.py   → win11 .../hermes/config.yaml (多主机)
        │
        └─ 校验: GET /v1/models (CPA 能力检查，动态扫模型)

agent-sync (目标机自拉，无 SSH):
  目标机 config/cpa-config.yaml → 连 CPA 扫模型 → 写本机 agent 文件
```

## 已实现 adapter（10 个）

| adapter | 位置 | 说明 |
|---------|------|------|
| `pi` | 本机 `~/.pi/agent/models.json` | provider `cpa`，19 模型 |
| `opencode` | 本机 `~/.config/opencode/opencode.json` | provider `cpa`，19 模型 |
| `claude` | cc-switch `providers` 表 (claude) | **每条模型一条 provider**，notes=cpa，meta=openai_chat |
| `codex` | cc-switch `providers` 表 (codex) | 每条模型一条 provider，notes=cpa，wire_api=responses |
| `omp` | 各机 `~/.omp/agent/models.yml`（多主机，见 adapter_instances） | 收拢到 providers.cpa；屏蔽自带 provider 用 `scripts/omp_disable_providers.py`（写 config.yml 的 disabledProviders） |
| `dsh` | 各机 `~/.dsh/profiles/web/cordis.patch.yml`（多主机） | provider `cpa` |
| `picoclaw` | meilan-phone1 `~/.picoclaw/config.json`（多主机） | model_list 里 provider=cpa（api_keys 数组）；砍自带模型用 `scripts/picoclaw_disable_builtin.py`（删 model_list 非 cpa 条目） |
| `crush` | meilan-phone1 `~/.config/crush/crushrc`（crushrc 配置层，多主机） | `provider add cpa --type openai-compat` + `model add`（19 模型）；`option default-providers false` 屏蔽自带（Catwalk）模型 |
| `openclaw` | win11 `~/.openclaw/openclaw.json`（多主机） | models.providers.cpa |
| `hermes` | win11 `.../hermes/config.yaml`（多主机） | providers.cpa + model 主端点 |

**claude/codex 风格**（用户明确）：同一供应商（CPA）下，**每个模型单独一条 provider**，全部指向 CPA，
`name=模型名`、`notes=cpa`，像以前"小水管"那样。claude 用根路径 base_url（claude 自己拼 `/v1/messages`），
codex 用带 `/v1` 的 base_url（OpenAI 格式）+ `wire_api=responses`。

## 用法

```bash
cd /mnt/shared/my-project/cpa-sync
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
  default_input_modes: ["text"]     # 全局默认 input 模式（无覆盖/未命中时的兜底）
  image_models: ["*"]               # 识图模型匹配（fnmatch 通配符），命中则 input 加 "image"
strategy: replace            # replace = 全盘对齐，不留旧模型
```

**统一管理**：`context_window` / `auto_compact_token_limit` 由中央配置控制，改一处 → `cpa-sync sync` → 所有 agent 统一。

**识图能力匹配**：三个层级，优先级从低到高：

1. `default_input_modes`：全局兜底（默认 `["text"]`）
2. `image_models`：模型名通配符（fnmatch），命中则叠加 `image`；`["*"]` = 全部模型识图
3. `model_overrides.<model>.input`：单模型显式声明，**最高优先级**（可全开后单独关掉不识图的模型）

适配 omp / pi / opencode / omp 等写 `input` 字段的 adapter。声明 `image` 只代表 agent 允许发图，
不代表上游模型一定支持视觉（CPA 的 `/v1/models` 不带能力字段，只能靠这里声明）。

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

cpa-sync/
├── cpa-sync              # CLI 入口（中心模式）
├── agent-sync            # CLI 入口（客户端模式，无 SSH）
├── agent-sync.sh/.ps1    # 客户端启动器（macOS/Linux / Windows）
├── cpa-sync-web          # Web 控制面板启动器（venv python）
├── cpa-sync-nginx        # nginx 网关安装器（渲染 gateway.yaml → /etc/nginx/conf.d）
├── core/                 # ir / cpa_client / config / remote
├── adapters/             # base + pi/opencode/claude/codex/omp/dsh/picoclaw/crush/openclaw/hermes
├── web/                  # Web 控制面板（app.py 面板 + nginx.conf 模板 + static/）
├── scripts/              # 辅助脚本（omp/picoclaw 屏蔽自带 + render_gateway 渲染网关）
├── docs/                 # 研究/设计笔记（如 omp 屏蔽自带 provider 机制）
├── config/               # cpa-config.yaml 中央配置 + gateway.yaml 网关配置（真实值，不进仓库）
└── tests/                # 单元测试

## 开发

```bash
python3 -m pytest tests/    # 跑测试
```

新 adapter：继承 `adapters.base.Adapter`，实现 `load_current / render / write`，注册到 `adapters/__init__.py`。

## Web 控制面板 + 网关（nginx 数据面反代）

**双端口、可选模式**（`config/gateway.yaml`，上游数量可配，不写死）：

| 端口（默认） | 进程 | 职责 | 谁连 |
|---|---|---|---|
| `17834` | Python（`web/app.py`） | 静态页 + 配置 API（管理面） | 浏览器 |
| `8380` | nginx | 纯反代聚合 CPA（数据面） | agent |

- **可选模式**：`config/gateway.yaml` 的 `upstreams` 可 1~N 个 CPA——1 个=纯转发；2+ 个=聚合+故障切换（round-robin，一台挂了自动切下一台）。
- **无前后端分离**：面板由 Python 自己发静态页 + 管配置；nginx 只做高性能反代（不碰静态、不碰配置）。
- 启动：
  ```bash
  .venv/bin/python web/app.py   # 面板（默认 0.0.0.0:17834）
  ./cpa-sync-nginx install      # 渲染 gateway.yaml → /etc/nginx/conf.d/cpa-sync.conf → reload
  ```
- 配置：`config/gateway.yaml`（真实值，不进仓库）；模板 `config/gateway.yaml.example`；渲染入口 `scripts/render_gateway.py`。


## 远程面板 + 面板安全

**nginx 一体化**（1 个 nginx 进程，两个 server）：

```
nginx (1 进程 ~55MB)
├─ :8380  → upstream 192.168.31.213 + 192.168.31.253（CPA 网关，故障切换）
└─ :17835 → 127.0.0.1:17834（面板远程反代，局域网可访问）
                    └─ app.py（本机自 serve，认证透传）
```

- **远程访问**：`http://192.168.31.204:17835`（局域网任一设备浏览器可开，完整面板功能）
- **app.py 仍绑本机** `127.0.0.1:17834`——远程全靠 nginx 反代，面板本身不暴露到局域网

### 开关（`config/gateway.yaml`，改后跑 `./cpa-sync-nginx install` 重载）

| 配置 | 作用 | 默认 |
|---|---|---|
| `gateway.panel_proxy_port` | nginx 对外反代面板端口；**0 = 关掉远程面板**（只留本机 17834） | `17835` |
| `gateway.panel.auth_key` | 面板访问密钥；设后 `/api/*` 需 `X-Panel-Key`（静态壳仍可开，用来输密钥） | `""`（不认证） |
| `gateway.panel.bind` | app.py 绑定地址（`127.0.0.1` 本机 / `0.0.0.0` 直连远程，一般不改） | `127.0.0.1` |

**认证语义**：`auth_key` 空 = 不认证；设了则无 key 访问 `/api/*` 返 **401**，带 key 返 200（面板顶部「🔒 面板安全」按钮设置，即时生效，不需重启）。

## CPA 节点日志运维

面板「🧭 CPA 节点」卡的管理面子区：

| 操作 | 作用 | 备注 |
|---|---|---|
| 📜 日志 | 看 CPA 运行日志 | 需先开 logging-to-file |
| 🧹 清空日志 | 清空两节点日志 | DELETE observability/logs |
| 📝 开日志 / 🚫 关日志 | logging-to-file 开关 | 改节点 config，即时生效 |

**logging-to-file** 默认关（日志接口返 400），面板一键开后即可看/清。


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
