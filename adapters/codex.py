"""codex (cc-switch) 适配器 —— 同步 CPA provider 配置到 cc-switch.db。

codex 配置全靠 cc-switch 的 providers 表（app_type='codex'）。每个 provider
的 settings_config 是 auth + config(TOML) + modelCatalog：
  {"auth": {"OPENAI_API_KEY": ...},
   "config": "model_provider=\"custom\"\nmodel=\"...\"\n\n[model_providers.custom]\nbase_url=\"...\"",
   "modelCatalog": {"models": [{"model","displayName"}, ...]}}

用户要求的风格（与以前一致）：同一个供应商（CPA）下，**每个模型单独一条
provider**，全部指向 CPA（base 带 /v1，OpenAI 格式），modelCatalog 放该模型。

  - 删除同 app_type 下所有非 CPA 的 provider
  - 为 CPA 扫出的每个模型建一条 provider（name=模型名，notes=cpa）
  - 把第一条（默认模型）设为 is_current
"""
from __future__ import annotations

from typing import Any

from adapters import _ccswitch
from adapters.base import Adapter
from core.ir import ModelIR, ProviderIR


class CodexAdapter(Adapter):
    name = "codex"
    label = "codex (cc-switch)"

    APP_TYPE = "codex"

    def __init__(self, config=None):
        super().__init__(config)

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        recs = _ccswitch.get_cpa_providers(self.APP_TYPE)
        if not recs:
            return None
        ctx_win = int(getattr(getattr(self.config, "cpa", None), "context_window", 512000) or 512000)
        max_tok = int(getattr(getattr(self.config, "cpa", None), "max_tokens", 16384) or 16384)
        base = ""
        key = ""
        seen: list[str] = []
        api_format = None
        for rec in recs:
            cfg = _json_loads(rec.get("settings_config"))
            if not base:
                import re
                bm = re.search(r'base_url\s*=\s*"([^"]+)"', cfg.get("config", "") or "")
                base = bm.group(1) if bm else ""
            if not key:
                key = str((cfg.get("auth", {}) or {}).get("OPENAI_API_KEY", ""))
            if api_format is None:
                meta = rec.get("meta")
                if meta:
                    try:
                        api_format = json.loads(meta).get("apiFormat")
                    except Exception:
                        api_format = None
            catalog = cfg.get("modelCatalog", {}) or {}
            models_raw = catalog.get("models", []) if isinstance(catalog, dict) else []
            for m in models_raw:
                mid = m.get("model") or m.get("displayName") if isinstance(m, dict) else m
                if mid and mid not in seen:
                    seen.append(mid)
        models = [
            ModelIR(id=mid, context_window=ctx_win, max_tokens=max_tok)
            for mid in seen
        ]
        return ProviderIR(
            base_url=base, api_key=key, api="responses", models=models,
            meta={"api_format": api_format},
        )

    # ---- 覆写 diff：codex 是 cc-switch 管理的“切换式”，
    # 同步策略是“推翻再来”（全盘重建），因此不做增量 diff，
    # 每次同步都重新渲染并全盘重建 provider 集合。 ----
    def diff(self, current, target) -> list[str]:
        return [f"  [{self.name}] 每次同步全盘重建（cc-switch 切换式，不增量）"]

    # ---- 渲染：IR -> 多条 provider（每条一个模型）----
    def render(self, ir: ProviderIR) -> list[dict[str, Any]]:
        models = ir.model_ids()
        if not models:
            raise ValueError("没有可用模型")
        base = ir.base_url.rstrip("/")
        # 中央统一配置：context_window / auto_compact_token_limit 由 build_provider_ir
        # 塞进 ir.meta，改中央配置即全改（所有 agent 统一）。
        ctx_win = int(ir.meta.get("context_window", 512000) or 512000)
        auto_compact = int(ir.meta.get("auto_compact_token_limit", 200000) or 200000)
        providers: list[dict[str, Any]] = []
        for m in models:
            config = (
                f'model_provider = "custom"\n'
                f'model = "{m}"\n'
                f'review_model = "{m}"\n'
                f'model_context_window = {ctx_win}\n'
                f'model_auto_compact_token_limit = {auto_compact}\n'
                f'\n'
                f'[model_providers]\n'
                f'[model_providers.custom]\n'
                f'name = "custom"\n'
                f'wire_api = "responses"\n'
                f'requires_openai_auth = true\n'
                f'base_url = "{base}"\n'
            )
            providers.append({
                "name": m,
                "notes": "cpa",
                "settings_config": {
                    "auth": {"OPENAI_API_KEY": ir.api_key},
                    "config": config,
                    "modelCatalog": {
                        "models": [{"model": m, "displayName": m}]
                    },
                },
                "meta": {
                    "commonConfigEnabled": False,
                    "endpointAutoSelect": True,
                    "apiFormat": "openai_chat",
                    "codexChatReasoning": {
                        "supportsThinking": True,
                        "supportsEffort": False,
                        "thinkingParam": "thinking",
                        "effortParam": "none",
                        "outputFormat": "auto",
                    },
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

        auth = cfg.get("auth", {}) or {}
        config = cfg.get("config", "") or ""
        catalog = cfg.get("modelCatalog", {}) or {}
        models_raw = catalog.get("models", []) if isinstance(catalog, dict) else []
        ids = []
        for m in models_raw:
            if isinstance(m, dict):
                mid = m.get("model") or m.get("displayName")
            else:
                mid = m
            if mid and mid not in ids:
                ids.append(mid)
        if not ids:
            import re
            m = re.search(r'^model\s*=\s*"([^"]+)"', config, re.M)
            if m:
                ids.append(m.group(1))
        models = [
            ModelIR(id=mid, context_window=512000, max_tokens=16384)
            for mid in ids
        ]
        import re
        bm = re.search(r'base_url\s*=\s*"([^"]+)"', config)
        base = bm.group(1) if bm else ""
        return ProviderIR(
            base_url=base,
            api_key=str(auth.get("OPENAI_API_KEY", "")),
            api="responses",
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
