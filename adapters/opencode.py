"""opencode (本机) 适配器 —— 同步 CPA provider 配置到 opencode.json。

opencode 的 provider 配置在 ~/.config/opencode/opencode.json：
  {"provider": {"cpa": {"models": {...}, "npm": "...", "options": {baseURL, apiKey}},
                 "iamhc": {...}, "pie-xian": {...}}}

本适配器：
  - 只更新 provider.cpa 的 options.baseURL / options.apiKey + models
  - 保留 mcp（context7/playwright/tavily）及其它 provider（iamhc/pie-xian）
  - opencode 的 models 是字典 {model_id: {name}}，无 contextWindow 字段
  - 直接读写本机文件，写入前备份
"""
from __future__ import annotations

import json
import os
from typing import Any

from adapters.base import Adapter
from core.ir import ProviderIR

OPENCODE_JSON = os.path.expanduser("~/.config/opencode/opencode.json")


class OpencodeAdapter(Adapter):
    name = "opencode"
    label = "opencode (本机)"

    def __init__(self, config=None):
        super().__init__(config)
        self.path = OPENCODE_JSON

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        data = self._read()
        cpa = self._find_cpa(data)
        if cpa is None:
            return None
        return self._provider_to_ir(cpa)

    # ---- 渲染：IR -> opencode.json 的 provider.cpa ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        data = self._read()
        data.setdefault("provider", {})
        providers = data["provider"]
        if "cpa" not in providers:
            providers["cpa"] = {}
        cpa = providers["cpa"]
        cpa.setdefault("npm", "@ai-sdk/openai-compatible")
        options = cpa.setdefault("options", {})
        options["baseURL"] = ir.base_url
        options["apiKey"] = ir.api_key
        options.setdefault("setCacheKey", True)
        cpa["models"] = {
            m.id: {
                "name": m.id,
                "contextWindow": m.context_window,
                "maxTokens": m.max_tokens,
                "input": m.input_modes,
                "reasoning": m.reasoning,
            }
            for m in ir.models
        }
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
    def _find_cpa(data: dict) -> dict | None:
        providers = data.get("provider", {}) or {}
        cpa = providers.get("cpa")
        if isinstance(cpa, dict):
            return cpa
        # 兜底：按 baseURL 含 8317
        for v in providers.values():
            if isinstance(v, dict) and ":8317" in str(v.get("options", {}).get("baseURL", "")):
                return v
        return None

    @staticmethod
    def _provider_to_ir(provider: dict) -> ProviderIR:
        from core.ir import ModelIR

        options = provider.get("options", {}) or {}
        models = []
        for mid, m in (provider.get("models", {}) or {}).items():
            if not isinstance(m, dict):
                m = {}
            models.append(
                ModelIR(
                    id=mid,
                    context_window=int(m.get("contextWindow", 128000) or 128000),
                    max_tokens=int(m.get("maxTokens", 16384) or 16384),
                    input_modes=list(m.get("input", ["text"]) or ["text"]),
                    reasoning=bool(m.get("reasoning", False)),
                )
            )
        return ProviderIR(
            base_url=str(options.get("baseURL", "")),
            api_key=str(options.get("apiKey", "")),
            api="openai-completions",
            models=models,
        )
