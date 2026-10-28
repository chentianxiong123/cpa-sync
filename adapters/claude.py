"""claude (cc-switch) 适配器 —— 同步 CPA provider 配置到 cc-switch.db。

claude 没有自己的多来源管理，配置全靠 cc-switch 的 providers 表
（app_type='claude'）。每个 provider 的 settings_config 是 env 结构：
  {"env": {"ANTHROPIC_BASE_URL": ..., "ANTHROPIC_AUTH_TOKEN": ...,
           "ANTHROPIC_MODEL": ..., "ANTHROPIC_DEFAULT_OPUS_MODEL": ..., ...}}

用户要求的风格（与以前“小水管”一致）：同一个供应商（CPA）下，
**每个模型单独一条 provider**，全部指向 CPA，档位填对应模型。

  - 删除同 app_type 下所有非 CPA 的 provider
  - 为 CPA 扫出的每个模型建一条 provider（name=模型名，notes=cpa，env 档位=该模型）
  - 把第一条（默认模型）设为 is_current
"""
from __future__ import annotations

import json
from typing import Any

from adapters import _ccswitch
from adapters.base import Adapter
from core.ir import ModelIR, ProviderIR


class ClaudeAdapter(Adapter):
    name = "claude"
    label = "claude (cc-switch)"

    APP_TYPE = "claude"

    def __init__(self, config=None):
        super().__init__(config)

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        recs = _ccswitch.get_cpa_providers(self.APP_TYPE)
        if not recs:
            return None
        ctx_win = int(getattr(getattr(self.config, "cpa", None), "context_window", 512000) or 512000)
        max_tok = int(getattr(getattr(self.config, "cpa", None), "max_tokens", 16384) or 16384)
        # 合并所有 notes=cpa 的模型（每条一个模型），base/key 取第一条
        base = ""
        key = ""
        seen: list[str] = []
        has_subagent = False
        api_format = None
        for rec in recs:
            cfg = _json_loads(rec.get("settings_config"))
            env = cfg.get("env", {}) or {}
            if not base:
                b = str(env.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
                if b and not b.endswith("/v1"):
                    b = b + "/v1"
                base = b
            if not key:
                key = str(env.get("ANTHROPIC_AUTH_TOKEN", ""))
            if env.get("CLAUDE_CODE_SUBAGENT_MODEL"):
                has_subagent = True
            if api_format is None:
                meta = rec.get("meta")
                if meta:
                    try:
                        api_format = json.loads(meta).get("apiFormat")
                    except Exception:
                        api_format = None
            for k in _ccswitch.CLAUDE_MODEL_KEYS:
                v = env.get(k)
                if v and v not in seen:
                    seen.append(v)
        models = [
            ModelIR(id=m, context_window=ctx_win, max_tokens=max_tok)
            for m in seen
        ]
        return ProviderIR(
            base_url=base, api_key=key, api="anthropic", models=models,
            meta={"has_subagent": has_subagent, "api_format": api_format},
        )

    # ---- 覆写 diff：claude 是 cc-switch 管理的“切换式”，
    # 同步策略是“推翻再来”（全盘重建），因此不做增量 diff，
    # 每次同步都重新渲染并全盘重建 provider 集合。 ----
    def diff(self, current, target) -> list[str]:
        return [f"  [{self.name}] 每次同步全盘重建（cc-switch 切换式，不增量）"]

    # ---- 渲染：IR -> 多条 provider（每条一个模型）----
    def render(self, ir: ProviderIR) -> list[dict[str, Any]]:
        models = ir.model_ids()
        if not models:
            raise ValueError("没有可用模型")
        # claude 的 ANTHROPIC_BASE_URL 用根路径（claude 自己拼 /v1/messages）。
        root = ir.base_url.rstrip("/")
        if root.endswith("/v1"):
            root = root[: -len("/v1")]
        providers: list[dict[str, Any]] = []
        for m in models:
            env = {
                "ANTHROPIC_BASE_URL": root,
                "ANTHROPIC_AUTH_TOKEN": ir.api_key,
                "ANTHROPIC_MODEL": m,
                "ANTHROPIC_DEFAULT_OPUS_MODEL": m,
                "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": m,
                "ANTHROPIC_DEFAULT_SONNET_MODEL": m,
                "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": m,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL": m,
                "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": m,
                "ANTHROPIC_DEFAULT_FABLE_MODEL": m,
                "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": m,
                "DISABLE_AUTOUPDATER": "1",
                "CLAUDE_CODE_SUBAGENT_MODEL": m,
            }
            providers.append({
                "name": m,
                "notes": "cpa",
                "settings_config": {
                    "env": env,
                    "attribution": {"commit": "", "pr": ""},
                },
                "meta": {
                    "commonConfigEnabled": False,
                    "endpointAutoSelect": True,
                    "apiFormat": "openai_chat",
                    "customUserAgent": "claude-cli/2.1.161 (external, cli)",
                },
            })
        return providers

    # ---- 写入（cc-switch.db，事务 + 删其它 provider）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        if backup:
            _ccswitch.backup_db()
        _ccswitch.sync_providers(
            self.APP_TYPE,
            rendered,
            current_name=rendered[0]["name"],
        )

    # ---- 内部辅助 ----
    @staticmethod
    def _provider_to_ir(cfg: dict) -> ProviderIR:
        from core.ir import ModelIR

        env = cfg.get("env", {}) or {}
        base = str(env.get("ANTHROPIC_BASE_URL", "")).rstrip("/")
        if base and not base.endswith("/v1"):
            base = base + "/v1"
        seen: list[str] = []
        for k in _ccswitch.CLAUDE_MODEL_KEYS:
            v = env.get(k)
            if v and v not in seen:
                seen.append(v)
        models = [
            ModelIR(id=m, context_window=512000, max_tokens=16384)
            for m in seen
        ]
        return ProviderIR(
            base_url=base,
            api_key=str(env.get("ANTHROPIC_AUTH_TOKEN", "")),
            api="anthropic",
            models=models,
        )


def _json_loads(s: str | None) -> dict:
    import json
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:
        return {}
