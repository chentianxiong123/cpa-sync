"""openclaw 适配器 —— 同步 CPA provider 配置到 openclaw.json。

openclaw 的模型/provider 配置在 openclaw.json 的 models.providers：
  {"models": {"mode": "merge", "providers": {
      "<id>": {baseUrl, apiKey, api, auth, timeoutSeconds,
               models: [{id, name, reasoning, input, contextWindow, maxTokens}, ...]}
  }}}

本适配器：
  - providers.cpa 代表 CPA 网关（auth="api-key"）
  - 同步时更新/新建 providers.cpa，保留其它 provider
  - 经 RemoteIO 读写，写入前自动备份 + 原子替换
"""
from __future__ import annotations

import json
import os
from typing import Any

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

DEFAULT_HOST = "win11"
DEFAULT_PATH = r"C:\Users\a1\.openclaw\openclaw.json"
CPA_ID = "cpa"


class OpenclawAdapter(Adapter):
    name = "openclaw"
    label = "openclaw"

    def __init__(self, config=None, host: str | None = None, path: str | None = None, platform: str | None = None):
        super().__init__(config)
        self.host = host
        if path is None:
            path = os.environ.get("CPA_SYNC_OPENCLAW_PATH", DEFAULT_PATH)
        self.path = path
        self.platform = platform
        self.remote = RemoteIO(self.host, platform=self.platform)

    def iter_instances(self) -> list[tuple[str, "OpenclawAdapter"]]:
        instances = getattr(self.config, "adapter_instances", {}) or {}
        insts = instances.get("openclaw", []) if isinstance(instances, dict) else []
        if insts:
            return [
                (i.name, OpenclawAdapter(self.config, host=i.host, path=i.path, platform=i.platform))
                for i in insts
            ]
        return [("default", self)]

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        data = self._read()
        provider = self._find_cpa_provider(data)
        if provider is None:
            return None
        return self._provider_to_ir(provider)

    # ---- 渲染：IR -> openclaw.json 的 models.providers.cpa ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        data = self._read()
        models_cfg = data.setdefault("models", {})
        models_cfg.setdefault("mode", "merge")
        providers = models_cfg.setdefault("providers", {})
        cpa = providers.setdefault(CPA_ID, {})
        cpa["baseUrl"] = ir.base_url
        cpa["apiKey"] = ir.api_key
        cpa["api"] = ir.api or "openai-completions"
        cpa["auth"] = "api-key"
        cpa.setdefault("timeoutSeconds", 300)
        cpa["models"] = [
            {
                "id": m.id,
                "name": m.id,
                "reasoning": m.reasoning,
                "input": m.input_modes,
                "contextWindow": m.context_window,
                "maxTokens": m.max_tokens,
            }
            for m in ir.models
        ]
        return data

    # ---- 写入（含远程备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        text = json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"
        self.remote.write(self.path, text)

    # ---- 内部辅助 ----
    def _read(self) -> dict:
        raw = self.remote.read(self.path)
        if not raw.strip():
            return {}
        try:
            return json.loads(raw) or {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _find_cpa_provider(data: dict) -> dict | None:
        providers = (data.get("models", {}) or {}).get("providers", {}) or {}
        if isinstance(providers.get(CPA_ID), dict):
            return providers[CPA_ID]
        for k, v in providers.items():
            if isinstance(v, dict) and v.get("baseUrl") and (k == CPA_ID or "8317" in str(v.get("baseUrl", ""))):
                return v
        return None

    @staticmethod
    def _provider_to_ir(provider: dict) -> ProviderIR:
        from core.ir import ModelIR

        models = []
        for m in provider.get("models", []) or []:
            if not isinstance(m, dict):
                continue
            models.append(
                ModelIR(
                    id=m.get("id", ""),
                    context_window=m.get("contextWindow", 128000),
                    max_tokens=m.get("maxTokens", 16384),
                    input_modes=list(m.get("input", ["text"]) or ["text"]),
                    reasoning=bool(m.get("reasoning", False)),
                )
            )
        return ProviderIR(
            base_url=str(provider.get("baseUrl", "")),
            api_key=str(provider.get("apiKey", "")),
            api=provider.get("api", "openai-completions"),
            models=models,
        )
