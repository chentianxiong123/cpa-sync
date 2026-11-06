"""hermes 适配器 —— 同步 CPA provider 配置到 config.yaml。

hermes 的配置在 %LOCALAPPDATA%\\hermes\\config.yaml（YAML）：
  model:      {provider, default, base_url, api_key, api_mode}
  providers:  {<name>: {name, base_url, api_key, models: {<id>: {}, ...}}, ...}

本适配器：
  - model 段 = CPA 主端点（provider=custom 的 base_url/api_key 直连 CPA 网关）
  - providers.cpa = CPA 的模型清单（models 是 dict）
  - model.default 指向第一个 CPA 模型（若原 default 已在 cpa 清单内则保留）
  - 保留其它 provider（上游 fallback）
  - 经 RemoteIO 读写，写入前自动备份 + 原子替换
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

DEFAULT_HOST = "win11"
DEFAULT_PATH = r"C:\Users\a1\AppData\Local\hermes\config.yaml"
CPA_ID = "cpa"


class HermesAdapter(Adapter):
    name = "hermes"
    label = "hermes"

    def __init__(self, config=None, host: str | None = None, path: str | None = None, platform: str | None = None):
        super().__init__(config)
        self.host = host
        if path is None:
            path = os.environ.get("CPA_SYNC_HERMES_PATH", DEFAULT_PATH)
        self.path = path
        self.platform = platform
        self.remote = RemoteIO(self.host, platform=self.platform)

    def iter_instances(self) -> list[tuple[str, "HermesAdapter"]]:
        instances = getattr(self.config, "adapter_instances", {}) or {}
        insts = instances.get("hermes", []) if isinstance(instances, dict) else []
        if insts:
            return [
                (i.name, HermesAdapter(self.config, host=i.host, path=i.path, platform=i.platform))
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

    # ---- 渲染：IR -> config.yaml 的 providers.cpa + model 主端点 ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        data = self._read()
        # 1) CPA 主端点：model 段（provider=custom 直连 CPA 网关）
        model_cfg = data.setdefault("model", {})
        model_cfg.setdefault("provider", "custom")
        model_cfg["base_url"] = ir.base_url
        model_cfg["api_key"] = ir.api_key
        model_cfg.setdefault("api_mode", "chat_completions")
        # 2) providers.cpa：CPA 模型清单（models 是 dict）
        providers = data.setdefault("providers", {})
        cpa = providers.setdefault(CPA_ID, {})
        cpa["name"] = "cpa"
        cpa["base_url"] = ir.base_url
        cpa["api_key"] = ir.api_key
        cpa["models"] = {m.id: {} for m in ir.models}
        # 3) model.default：保留原值（若仍在 cpa 清单内），否则取第一个 CPA 模型
        default = model_cfg.get("default", "")
        if default not in cpa["models"]:
            model_cfg["default"] = ir.models[0].id if ir.models else default
        return data

    # ---- 写入（含远程备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        text = yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True)
        self.remote.write(self.path, text)

    # ---- 内部辅助 ----
    def _read(self) -> dict:
        raw = self.remote.read(self.path)
        if not raw.strip():
            return {}
        try:
            return yaml.safe_load(raw) or {}
        except yaml.YAMLError:
            return {}

    @staticmethod
    def _find_cpa_provider(data: dict) -> dict | None:
        providers = data.get("providers", {}) or {}
        if isinstance(providers.get(CPA_ID), dict):
            return providers[CPA_ID]
        for k, v in providers.items():
            if isinstance(v, dict) and v.get("name") == CPA_ID:
                return v
        return None

    @staticmethod
    def _provider_to_ir(provider: dict) -> ProviderIR:
        from core.ir import ModelIR

        models = []
        raw_models = provider.get("models", {}) or {}
        if isinstance(raw_models, dict):
            for mid in raw_models:
                models.append(ModelIR(id=mid, context_window=128000, max_tokens=16384, input_modes=["text"]))
        return ProviderIR(
            base_url=str(provider.get("base_url", "")),
            api_key=str(provider.get("api_key", "")),
            api="openai-completions",
            models=models,
        )
