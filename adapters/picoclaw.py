"""picoclaw 适配器 —— 同步 CPA provider 配置到 config.json 的 model_list。

picoclaw 的模型/provider 配置在 config.json 的 model_list：
  [{model_name, provider, model, api_base, api_keys, auth_method, extra_body, ...}, ...]

本适配器：
  - provider=="cpa" 的条目代表 CPA 网关（api_base=CPA base_url, api_keys=[CPA key]）
  - 同步时清掉旧 cpa 条目、按 IR 重建，保留其它 provider 条目
  - 经 RemoteIO 读写（本机直读写 / 远程走 ssh），写入前自动备份 + 原子替换
"""
from __future__ import annotations

import json
import os
from typing import Any

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

DEFAULT_HOST = "meilan-phone1"
DEFAULT_PATH = "/root/.picoclaw/config.json"
CPA_PROVIDER = "cpa"


class PicoclawAdapter(Adapter):
    name = "picoclaw"
    label = "picoclaw"

    def __init__(self, config=None, host: str | None = None, path: str | None = None, platform: str | None = None):
        super().__init__(config)
        self.host = host
        if path is None:
            path = os.environ.get("CPA_SYNC_PICOCLAW_PATH", DEFAULT_PATH)
        self.path = path
        self.platform = platform
        self.remote = RemoteIO(self.host, platform=self.platform)

    def iter_instances(self) -> list[tuple[str, "PicoclawAdapter"]]:
        instances = getattr(self.config, "adapter_instances", {}) or {}
        insts = instances.get("picoclaw", []) if isinstance(instances, dict) else []
        if insts:
            return [
                (i.name, PicoclawAdapter(self.config, host=i.host, path=i.path, platform=i.platform))
                for i in insts
            ]
        return [("default", self)]

    # ---- 读取当前配置 ----
    def load_current(self) -> ProviderIR | None:
        data = self._read()
        entries = [
            m for m in data.get("model_list", [])
            if isinstance(m, dict) and m.get("provider") == CPA_PROVIDER
        ]
        if not entries:
            return None
        return self._entries_to_ir(entries)

    # ---- 渲染：IR -> config.json 的 model_list cpa 条目 ----
    def render(self, ir: ProviderIR) -> dict[str, Any]:
        data = self._read()
        # 清掉旧 cpa 条目，保留其它 provider
        data["model_list"] = [
            m for m in data.get("model_list", [])
            if not (isinstance(m, dict) and m.get("provider") == CPA_PROVIDER)
        ]
        for m in ir.models:
            data["model_list"].append({
                "model_name": m.id,
                "provider": CPA_PROVIDER,
                "model": m.id,
                "api_base": ir.base_url,
                "api_keys": [ir.api_key],
            })
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
    def _entries_to_ir(entries: list[dict]) -> ProviderIR:
        from core.ir import ModelIR

        models = []
        for e in entries:
            mid = e.get("model") or e.get("model_name", "")
            models.append(
                ModelIR(
                    id=mid,
                    context_window=128000,
                    max_tokens=16384,
                    input_modes=["text"],
                    reasoning=False,
                )
            )
        base_url = ""
        api_key = ""
        for e in entries:
            if e.get("api_base"):
                base_url = e["api_base"]
                break
        for e in entries:
            keys = e.get("api_keys") or []
            if keys:
                api_key = str(keys[0])
                break
        return ProviderIR(base_url=base_url, api_key=api_key, api="openai-completions", models=models)
