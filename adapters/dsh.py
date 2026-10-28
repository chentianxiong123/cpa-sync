"""dsh (win11) 适配器 —— 同步 CPA provider 配置到 cordis.patch.yml。

dsh 的模型/provider 配置在 win11 上：
  C:\\Users\\a1\\.dsh\\profiles\\web\\cordis.patch.yml
其中 llm-pi-ai 条目下的 config.providers.cpa / config.providers.iamhc
是 CPA 网关的配置。密钥走环境变量 DSH_CPA_API_KEY（.credentials.yaml）。

本适配器：
  - 只更新 llm-pi-ai.providers.cpa 的 baseURL + models
  - 保留 cordis.patch.yml 里所有其它条目（MCP 插入、UI、theme 等）
  - 通过 ssh 到 win11 读写，写入前远程备份
"""
from __future__ import annotations

import os
from typing import Any

import yaml

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

# dsh 的 profile 根目录（win11 用户 a1）
DSH_PROFILE = r"C:\Users\a1\.dsh\profiles\web\cordis.patch.yml"
DSH_CREDENTIALS = r"C:\Users\a1\.dsh\.credentials.yaml"
DEFAULT_HOST = "win11"


class DshAdapter(Adapter):
    name = "dsh"
    label = "dsh (win11)"

    def __init__(self, config=None, host: str | None = None):
        super().__init__(config)
        self.host = host or os.environ.get("CPA_SYNC_DSH_HOST", DEFAULT_HOST)
        self.remote = RemoteIO(self.host)

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        raw = self.remote.read(DSH_PROFILE)
        data = yaml.safe_load(raw)
        provider = self._find_cpa_provider(data)
        if provider is None:
            return None
        return self._provider_to_ir(provider, self._current_api_key())

    def _current_api_key(self) -> str:
        """读 credentials 里的 DSH_CPA_API_KEY（dsh 的密钥存这里）。"""
        try:
            creds = yaml.safe_load(self.remote.read(DSH_CREDENTIALS)) or {}
            return str(creds.get("refs", {}).get("DSH_CPA_API_KEY", ""))
        except Exception:
            return ""

    # ---- 渲染：IR -> cordis.patch.yml 的 providers.cpa ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        """返回要写回 cordis.patch.yml 的完整 YAML（只改 cpa，保留其它）。"""
        raw = self.remote.read(DSH_PROFILE)
        data = yaml.safe_load(raw) or []
        entry = self._find_llm_pi_ai(data)
        if entry is None:
            # 没有 llm-pi-ai 条目，新建一个
            entry = {"id": "llm-pi-ai", "config": {"providers": {}}}
            data.append(entry)
        providers = entry.setdefault("config", {}).setdefault("providers", {})
        cpa = providers.get("cpa", {})
        # 保留 apiKeyEnv / displayName / retryPolicy，只更新 baseURL 和 models
        cpa["baseURL"] = ir.base_url
        cpa["api"] = ir.api or "openai-completions"
        cpa["displayName"] = cpa.get("displayName", "cpa")
        cpa.setdefault("apiKeyEnv", "DSH_CPA_API_KEY")
        cpa["models"] = [
            {
                "id": m.id,
                "contextWindow": m.context_window,
                "maxTokens": m.max_tokens,
            }
            for m in ir.models
        ]
        providers["cpa"] = cpa
        return data

    # ---- 写入（含远程备份）----
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        text = yaml.safe_dump(rendered, sort_keys=False, allow_unicode=True)
        self.remote.write(DSH_PROFILE, text)  # remote.write 内部自动备份+原子替换
        # 若 api_key 变化，同步到 .credentials.yaml 的 DSH_CPA_API_KEY
        self._sync_api_key_if_needed()

    def _sync_api_key_if_needed(self) -> None:
        try:
            creds_raw = self.remote.read(DSH_CREDENTIALS)
        except Exception:
            return
        creds = yaml.safe_load(creds_raw) or {}
        refs = creds.get("refs", {})
        if refs.get("DSH_CPA_API_KEY") == self._target_api_key():
            return
        # 更新 refs
        refs["DSH_CPA_API_KEY"] = self._target_api_key()
        creds["refs"] = refs
        self.remote.write(DSH_CREDENTIALS, yaml.safe_dump(creds, sort_keys=False, allow_unicode=True))

    # ---- 内部辅助 ----
    def _target_api_key(self) -> str:
        # 中央配置里的 api_key 从 config 读取（由 CLI 注入）
        if self.config is not None and hasattr(self.config, "cpa"):
            return getattr(self.config.cpa, "api_key", "")
        return ""

    @staticmethod
    def _find_llm_pi_ai(data: list) -> dict | None:
        for item in data:
            if isinstance(item, dict) and item.get("id") == "llm-pi-ai":
                return item
        return None

    @classmethod
    def _find_cpa_provider(cls, data: list) -> dict | None:
        entry = cls._find_llm_pi_ai(data)
        if entry is None:
            return None
        providers = entry.get("config", {}).get("providers", {})
        return providers.get("cpa")

    @staticmethod
    def _provider_to_ir(provider: dict, api_key: str = "") -> ProviderIR:
        from core.ir import ModelIR

        models = []
        for m in provider.get("models", []) or []:
            models.append(
                ModelIR(
                    id=m.get("id", ""),
                    context_window=m.get("contextWindow", 128000),
                    max_tokens=m.get("maxTokens", 16384),
                )
            )
        return ProviderIR(
            base_url=provider.get("baseURL", ""),
            api_key=api_key,
            api=provider.get("api", "openai-completions"),
            models=models,
        )
