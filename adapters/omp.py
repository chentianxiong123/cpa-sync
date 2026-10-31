"""omp (oh-my-pi, win11) 适配器 —— 同步 CPA provider 配置到 models.yml。

omp 的模型/provider 配置在 win11 上：
  C:\\Users\\a1\\.omp\\agent\\models.yml
其中 providers.sensenova 是 CPA 网关（name: cpa），providers.iamhc 是备份。

本适配器：
  - 只更新 providers.sensenova（CPA）的 baseUrl / apiKey / models
  - 保留 providers.iamhc 及其它顶层字段
  - omp 的 apiKey 直接内联（不像 dsh 走 env），直接覆盖
  - 通过 ssh/scp 读写 win11，写入前远程备份
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

OMP_MODELS = r"C:\Users\a1\.omp\agent\models.yml"
DEFAULT_HOST = "win11"


class OmpAdapter(Adapter):
    name = "omp"
    label = "omp (oh-my-pi)"

    def __init__(self, config=None, host: str | None = None, path: str | None = None, platform: str | None = None):
        super().__init__(config)
        # host=None = 本机模式（RemoteIO 直接读写本地文件，无 SSH）
        self.host = host
        if path is None:
            if host is None:
                # 本机默认：~/.omp/agent/models.yml（发行物无需配路径）
                path = os.path.expanduser("~/.omp/agent/models.yml")
            else:
                path = os.environ.get("CPA_SYNC_OMP_PATH", OMP_MODELS)
        self.path = path
        self.platform = platform
        self.remote = RemoteIO(self.host, platform=self.platform)

    def iter_instances(self) -> list[tuple[str, "OmpAdapter"]]:
        """中央配置 adapter_instances 里 omp 的接入点（每台机器一个）。
        没配则回退单实例（显式 host/path 或默认 win11）。"""
        instances = getattr(self.config, "adapter_instances", {}) or {}
        omp_insts = instances.get("omp", []) if isinstance(instances, dict) else []
        if omp_insts:
            return [
                (inst.name, OmpAdapter(self.config, host=inst.host, path=inst.path, platform=inst.platform))
                for inst in omp_insts
            ]
        return [("default", self)]

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        raw = self.remote.read(self.path)
        data = yaml.safe_load(raw) or {}
        provider = self._find_cpa_provider(data)
        if not isinstance(provider, dict):
            return None
        return self._provider_to_ir(provider)

    # ---- 渲染：IR -> models.yml 的 providers.cpa ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        raw = self.remote.read(self.path)
        data = yaml.safe_load(raw) or {}
        data.setdefault("providers", {})
        providers = data["providers"]
        # 清掉任何代表 CPA 但 key 不是 cpa 的旧条目（如 sensenova），
        # 统一收拢到 key=cpa，只保留一个 CPA provider。
        for k in list(providers.keys()):
            v = providers[k]
            if k != "cpa" and isinstance(v, dict) and v.get("name") == "cpa":
                del providers[k]
        cpa = providers.setdefault("cpa", {})
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

    # ---- 写入（含远程备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        text = yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True)
        self.remote.write(self.path, text)

    # ---- 内部辅助 ----
    @staticmethod
    def _find_cpa_provider_key(data: dict) -> str | None:
        """找代表 CPA 的 provider key：优先 key=cpa，否则任意 name=cpa 的 key。"""
        providers = data.get("providers", {}) or {}
        if isinstance(providers.get("cpa"), dict):
            return "cpa"
        for k, v in providers.items():
            if isinstance(v, dict) and v.get("name") == "cpa":
                return k
        return None

    @classmethod
    def _find_cpa_provider(cls, data: dict) -> dict | None:
        key = cls._find_cpa_provider_key(data)
        if key is None:
            return None
        return data.get("providers", {}).get(key)

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
