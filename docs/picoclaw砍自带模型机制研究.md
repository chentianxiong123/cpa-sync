# picoclaw 屏蔽/砍掉自带模型机制研究

> 2026-11-06，meilan-phone1（192.168.31.213，picoclaw v0.2.x，config version 3）
> 研究 picoclaw 怎么"砍掉原版模型"（用户要只留 CPA 网关 cpa），沉淀成可复用脚本。

## 核心结论

**picoclaw 没有 omp 那种 `disabledProviders` 数组——provider/model 全在 `config.json` 的 `model_list`（数组）。**
屏蔽自带 = **从 `model_list` 删掉内置 provider 条目**（删了就不加载）。

## config.json 结构

| 字段 | 说明 |
|------|------|
| `model_list` | **provider 模型注册表**（数组），每条 `{model_name, provider, model, api_base, api_keys?, auth_method?, extra_body?}` |
| `agents.defaults` | 默认模型（`model_name`/`provider`，**空 = limited_mode 启动**） |
| `gateway`/`channel_list`/`tools`/`hooks` | 网关/渠道/工具/钩子配置（与模型无关） |
| `version: 3` | 配置版本（敏感码在 `.security.yml`） |

## model_list 条目两类

- **内置 provider**：`provider` = 内置名（openai/anthropic/zhipu/deepseek/venice/nearai/gemini/qwen/moonshot/groq/openrouter/nvidia/cerebras/vivgrid/volcengine/shengsuanyun/antigravity/github-copilot/ollama/mistral/avian/minimax/longcat/modelscope/vllm/lmstudio/azure...），`api_base` 指向官方（api.openai.com 等）或 localhost（ollama/vllm/lmstudio）。**要砍的"原版模型"**。
- **自定义网关**：`provider=cpa`，`api_base` 指向 CPA 网关（http://192.168.31.213:8317/v1）。**保留**。

## 脚本行为

`cpa-sync/scripts/picoclaw_disable_builtin.py` —— 删 `model_list` 里非保留 provider 条目（默认保留 `cpa`），备份 + 原子写，可选重启 picoclaw-gateway。

```bash
python3 scripts/picoclaw_disable_builtin.py --dry-run                 # 只打印
python3 scripts/picoclaw_disable_builtin.py --restart-picoclaw        # 删 + 重启 gateway
python3 scripts/picoclaw_disable_builtin.py --keep cpa,myprovider     # 额外保留
```

## 实测（meilan-phone1）

- 砍前：`model_list` 49 条（19 cpa + 30 内置自带）；
- dry-run：49→19（删 30 条自带，只留 cpa）；
- 真删：备份 `config.json.bak.root`，写入 19 条全 cpa（MiniMax-M3.1-Flash 等，api_base=CPA 网关）；
- 重启 `picoclaw-gateway`（supervisorctl restart）成功，进程 RUNNING（pid 新），panic 日志空；
- `agents.defaults.model_name` 一直为空（`limited_mode` 是原状，非砍模型导致）。

## 注意

- **默认模型**：`agents.defaults.model_name` 为空 → gateway `limited_mode` 启动（"started without default model"）。砍掉自带后若要自动用某个 cpa 模型，设 `agents.defaults.model_name` 为 cpa 模型名；
- 手机无 sqlite3 CLI，脚本用 python3（json 标准库，无第三方依赖）；
- 改 config 后必须 `supervisorctl restart picoclaw-gateway` 才生效（脚本 `--restart-picoclaw` 自动做）。
