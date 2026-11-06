"""crush 适配器 —— 同步 CPA provider 配置到 providers.json。

crush 的 provider 配置在 ~/.local/share/crush/providers.json，是一个 list：
  [{name, id, api_key, api_endpoint, type, default_large_model_id, default_small_model_id,
    models: [{id, name, cost_per_1m_in, cost_per_1m_out, ...}]}, ...]

本适配器：
  - id=="cpa" 的 provider 代表 CPA 网关（type=openai-compat，api_endpoint=CPA base_url）
  - 同步时移除旧 cpa provider、按 IR 重建，保留其它 provider
  - 经 RemoteIO 读写，写入前自动备份 + 原子替换
"""
from __future__ import annotations

import json
import os
from typing import Any

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

DEFAULT_HOST = "meilan-phone1"
DEFAULT_PATH = "/root/.local/share/crush/providers.json"
CPA_ID = "cpa"


class CrushAdapter(Adapter):
    name = "crush"
    label = "crush"

    def __init__(self, config=None, host: str | None = None, path: str | None = None, platform: str | None = None):
        super().__init__(config)
        self.host = host
        if path is None:
            path = os.environ.get("CPA_SYNC_CRUSH_PATH", DEFAULT_PATH)
        self.path = path
        self.platform = platform
        self.remote = RemoteIO(self.host, platform=self.platform)

    def iter_instances(self) -> list[tuple[str, "CrushAdapter"]]:
        instances = getattr(self.config, "adapter_instances", {}) or {}
        insts = instances.get("crush", []) if isinstance(instances, dict) else []
        if insts:
            return [
                (i.name, CrushAdapter(self.config, host=i.host, path=i.path, platform=i.platform))
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

    # ---- 渲染：IR -> providers.json 的 cpa provider ----
    def render(self, ir: ProviderIR) -> list[dict]:
        data = self._read()
        # 移除旧 cpa provider，保留其它
        providers = [p for p in data if not (isinstance(p, dict) and p.get("id") == CPA_ID)]
        models = []
        for m in ir.models:
            models.append({
                "id": m.id,
                "name": m.id,
                "cost_per_1m_in": 0,
                "cost_per_1m_out": 0,
                "cost_per_1m_in_cached": 0,
                "cost_per_1m_out_cached": 0,
            })
        providers.append({
            "name": "cpa",
            "id": CPA_ID,
            "api_key": ir.api_key,
            "api_endpoint": ir.base_url,
            "type": "openai-compat",
            "default_large_model_id": models[0]["id"] if models else "",
            "default_small_model_id": models[0]["id"] if models else "",
            "models": models,
        })
        return providers

    # ---- 写入（含远程备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        text = json.dumps(rendered, ensure_ascii=False, indent=2) + "\n"
        self.remote.write(self.path, text)

    # ---- 内部辅助 ----
    def _read(self) -> list:
        raw = self.remote.read(self.path)
        if not raw.strip():
            return []
        try:
            data = json.loads(raw)
            return data if isinstance(data, list) else []
        except json.JSONDecodeError:
            return []

    @staticmethod
    def _find_cpa_provider(data: list) -> dict | None:
        for p in data:
            if isinstance(p, dict) and p.get("id") == CPA_ID:
                return p
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
                    context_window=128000,
                    max_tokens=16384,
                    input_modes=["text"],
                    reasoning=False,
                )
            )
        return ProviderIR(
            base_url=str(provider.get("api_endpoint", "")),
            api_key=str(provider.get("api_key", "")),
            api="openai-completions",
            models=models,
        )
