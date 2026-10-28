"""pi (本机) 适配器 —— 同步 CPA provider 配置到 ~/.pi/agent/models.json。

pi 的 provider 配置在 ~/.pi/agent/models.json：
  {"providers": {"<uuid>": {name: cpa, baseUrl, api, apiKey, models:[...]},
                  "iamhc": {...}, ...}}

本适配器：
  - 只更新 name==cpa 的 provider（key 为 UUID，保留原 key）
  - 保留 iamhc 及其它 provider
  - 直接读写本机文件，写入前备份
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import yaml

from adapters.base import Adapter
from core.ir import ProviderIR

PI_MODELS = os.path.expanduser("~/.pi/agent/models.json")


class PiAdapter(Adapter):
    name = "pi"
    label = "pi (本机)"

    def __init__(self, config=None):
        super().__init__(config)
        self.path = PI_MODELS

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        data = self._read()
        key = self._find_cpa_key(data)
        if key is None:
            return None
        return self._provider_to_ir(data["providers"][key])

    # ---- 渲染：IR -> models.json 的 cpa provider ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        data = self._read()
        data.setdefault("providers", {})
        providers = data["providers"]
        key = self._find_cpa_key(data)
        if key is None:
            key = "cpa"
            providers[key] = {}
        cpa = providers[key]
        cpa["name"] = "cpa"
        cpa["baseUrl"] = ir.base_url
        cpa["api"] = ir.api or "openai-completions"
        cpa["apiKey"] = ir.api_key
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

    # ---- 写入（含备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        self.backup(self.path)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(rendered, f, ensure_ascii=False, indent=2)
            f.write("\n")

    # ---- 内部辅助 ----
    def _read(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f) or {}

    @staticmethod
    def _find_cpa_key(data: dict) -> str | None:
        providers = data.get("providers", {}) or {}
        for k, v in providers.items():
            if isinstance(v, dict) and v.get("name") == "cpa":
                return k
        for k, v in providers.items():
            if isinstance(v, dict) and ":8317" in str(v.get("baseUrl", "")):
                return k
        return None

    @staticmethod
    def _provider_to_ir(provider: dict) -> ProviderIR:
        from core.ir import ModelIR

        models = []
        for m in provider.get("models", []) or []:
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
            base_url=provider.get("baseUrl", ""),
            api_key=str(provider.get("apiKey", "")),
            api=provider.get("api", "openai-completions"),
            models=models,
        )
