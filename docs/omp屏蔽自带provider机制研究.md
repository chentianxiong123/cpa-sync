# omp (oh-my-pi) 屏蔽自带 provider 机制研究

> 2026-11-06，win11（192.168.31.12，omp v18.8.6）
> 研究 omp 怎么"扫掉自带 provider"（用户已手动扫 86 个），沉淀成可复用脚本。

## 核心结论

**omp 屏蔽自带 provider = 把 provider id 写进 `~/.omp/agent/config.yml` 的 `disabledProviders` 数组。**
omp 启动时按这个数组过滤，不加载这些内置 provider。

**自带 provider 全集 = 包内 `@oh-my-pi/pi-coding-agent/node_modules/@oh-my-pi/pi-catalog/src/compat/rules.json` 的 `providers` keys（85 个）。**
这是编译产物，源是 `rules/providers/*.kdl`（`provider-ids.ts` 的 `KnownProvider` 类型同源）。

## 两个配置文件分工

| 文件 | 作用 |
|------|------|
| `~/.omp/agent/models.yml` | **provider 定义**：`providers.cpa`（baseUrl/apiKey/models）+ `providers.sensenova`（旧） |
| `~/.omp/agent/config.yml` | **屏蔽 + 角色**：`disabledProviders`（86 个自带）+ `modelRoles`（default/plan/vision）+ UI |
| `~/.omp/agent/agent.db` | 运行时状态库（model_perf/model_usage/settings/auth_credentials，**无 provider 定义**） |

## 屏蔽机制细节

- `rules.json` 里 **85 个内置 provider**（chat/多模态 + `web` 搜索）；
- **`web` 是唯一 search provider**（`api: web-search`，seed 模型 hosted/parallel/perplexity/exa...），**不该屏蔽**；
- 自定义 provider（`cpa`/`sensenova`）**不在 rules.json**（非内置），天然不受 disabledProviders 影响；
- 用户扫的 86 个 = 84 个内置（除 web）+ `llama.cpp`/`minimax-cn`（旧/别名，不在当前内置）。

## 复用脚本

`cpa-sync/scripts/omp_disable_providers.py` —— 自动读 `rules.json` 的内置 provider 全集，写进 config.yml 的 `disabledProviders`（保留 `web` + 自定义 provider）。

```bash
# 本机
python3 scripts/omp_disable_providers.py --dry-run
# win11（显式指定 rules.json 路径）
python scripts/omp_disable_providers.py \
  --config "C:\Users\a1\.omp\agent\config.yml" \
  --rules "C:\Users\a1\AppData\Roaming\npm\node_modules\@oh-my-pi\pi-coding-agent\node_modules\@oh-my-pi\pi-catalog\src\compat\rules.json"
# 额外保留（默认保留 web）
python3 scripts/omp_disable_providers.py --keep web,cpa,sensenova
```

脚本行为：
1. 读 `rules.json` 的 `providers` keys → 内置全集（85）；
2. 减保留集（默认 `web`）→ 要屏蔽的（84）；
3. 合并进 config.yml 的 `disabledProviders`（去重，保留用户已有条目）；
4. 写前备份 `config.yml.bak.<user>` + 原子替换（保留 yaml 结构）。

## 验证（win11 实测）

- dry-run：85 内置，保留 web，屏蔽 84，disabledProviders 86→86（用户已全扫，+0）；
- 部分扫描模拟：3→84（+81，自动补全）；
- 真写入：备份 + 写回 86 个，modelRoles 保留（default=cpa/sensenova-6.8-flash-lite），web 不在 disabledProviders（保留）——**全部正确**。

## 注意

- win11 跑脚本需 `pip install pyyaml`（脚本依赖 yaml）；
- `disabledProviders` 只影响**内置** provider（rules.json 里那些）；自定义 provider（cpa/sensenova）不受影响，无需列入；
- omp adapter（`adapters/omp.py`）同步的是 `models.yml` 的 `providers.cpa`（provider 定义）；屏蔽自带是独立动作（本脚本），两者不冲突。
