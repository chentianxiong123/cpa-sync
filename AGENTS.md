# cpa-sync 项目约定（pi 上下文）

cpa-sync 把 CPA (cli-proxy-api) 配置同步到各 agent 应用。核心约定：

- **中央配置 `config/cpa-config.yaml` = 唯一事实来源**（真实值不入仓库，模板 `cpa-config.example.yaml`）。
- **生成物不可手改**：`~/.pi/agent/models.json`、`~/.omp/agent/models.yml` 等是渲染产物，`strategy: replace` 会在 `apply` 时覆盖手改。要改就改中央配置或 `adapters/<name>.py`。
- **能力靠声明，不靠探测**：CPA `/v1/models` 无能力字段，识图只能靠中央配置声明（`default_input_modes` + `image_models` + `model_overrides` 三层优先级）。
- **先 `check`（干跑）后 `apply`（写盘，自动备份）**。
- **新 adapter**：继承 `adapters.base.Adapter`，实现 `load_current / render / write`，注册到 `adapters/__init__.py`。
- **测试**：`python3 -m pytest tests/`。
- **多主机**：win11 走 ssh（默认 cmd shell，`type` 而非 `Get-Content`）；本机 pi/opencode 直读写。

详细技巧、配置心智模型、踩坑表：**`docs/使用技巧.md`**。
