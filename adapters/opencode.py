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
                "capabilities": {
                    "tools": True,
                    "input": m.input_modes,
                    "output": ["text"],
                    "reasoning": {"supported": m.reasoning},
                },
                "limit": {
                    "context": m.context_window,
                    "output": m.max_tokens,
                },
            }
            for m in ir.models
        }
        return data

    def diff(self, current, target) -> list[str]:
        lines = super().diff(current, target)
        # 检测旧 schema（顶层 input/contextWindow/maxTokens）→ 需要迁移到 capabilities/limit。
        # IR 值相同时 base.diff 不会报差异，这里显式触发一次重写完成迁移。
        data = self._read()
        cpa = self._find_cpa(data)
        if isinstance(cpa, dict):
            ms = cpa.get("models", {})
            if isinstance(ms, dict):
                sample = next((v for v in ms.values() if isinstance(v, dict)), {})
                if "input" in sample or "contextWindow" in sample or "maxTokens" in sample:
                    lines.append(
                        f"  [{self.name}] schema 迁移: 顶层 input/contextWindow/maxTokens -> capabilities/limit"
                    )
        return lines

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
            caps = m.get("capabilities", {}) or {}
            limit = m.get("limit", {}) or {}
            # 官方 v2：capabilities.input / capabilities.reasoning / limit.context / limit.output
            # 兼容旧 schema：顶层 input / contextWindow / maxTokens / reasoning
            input_modes = caps.get("input")
            if input_modes is None:
                input_modes = m.get("input", ["text"])
            reasoning = bool(m.get("reasoning", False))
            r = caps.get("reasoning")
            if isinstance(r, dict):
                reasoning = bool(r.get("supported", False))
            ctx = limit.get("context")
            if ctx is None:
                ctx = m.get("contextWindow", 128000)
            out = limit.get("output")
            if out is None:
                out = m.get("maxTokens", 16384)
            models.append(
                ModelIR(
                    id=mid,
                    context_window=int(ctx or 128000),
                    max_tokens=int(out or 16384),
                    input_modes=list(input_modes or ["text"]),
                    reasoning=reasoning,
                )
            )
        return ProviderIR(
            base_url=str(options.get("baseURL", "")),
            api_key=str(options.get("apiKey", "")),
            api="openai-completions",
            models=models,
        )
