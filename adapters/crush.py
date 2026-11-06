"""crush 适配器 —— 同步 CPA provider 配置到 crushrc（crush 配置脚本）。

crush 的 provider 配置在 ~/.config/crush/crushrc（Bash 脚本，用内建命令
`provider add` / `model add` 构建）。crushrc 是 crush 的**配置层**：
`crush update-providers`（Catwalk 源）只会重建 providers.json，**不会覆盖
crushrc**——所以 cpa 必须写进 crushrc 才不会被 crush 覆盖。

注意：
  - crush 默认从 ~/.config/crush/crushrc 读配置（shellConfigSibling）
  - 手机 crush v0.98.1 用旧 catwalk，--type 用 openai-compat（不是
    completions，那是 catwalk v3/main 的新值，v0.98.1 会判 unsupported）
  - cpa 是 OpenAI 兼容（chat completions）→ --type openai-compat

本适配器用 marker 段管理 cpa provider，保留 crushrc 其它内容：

  # --- cpa-sync cpa provider BEGIN ---
  provider add cpa --type openai-compat --base-url "..." --api-key "..."
  model add cpa/<id> --context-window N --default-max-tokens M
  model large cpa/<large>
  model small cpa/<small>
  # --- cpa-sync cpa provider END ---
"""
from __future__ import annotations

import os
import re
from typing import Any

from adapters.base import Adapter
from core.ir import ProviderIR
from core.remote import RemoteIO

DEFAULT_HOST = "meilan-phone1"
DEFAULT_PATH = "/root/.config/crush/crushrc"
CPA_ID = "cpa"
CPA_TYPE = "openai-compat"  # 手机 crush v0.98.1 用旧 catwalk；main/catwalk v3 是 completions
BEGIN_MARK = "# --- cpa-sync cpa provider BEGIN ---"
END_MARK = "# --- cpa-sync cpa provider END ---"


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

    # ---- 读取当前配置（crushrc marker 段）----
    def load_current(self) -> ProviderIR | None:
        text = self.remote.read(self.path)
        seg = self._extract_segment(text)
        if seg is None:
            return None
        return self._segment_to_ir(seg)

    # ---- 渲染：IR -> crushrc marker 段 ----
    def render(self, ir: ProviderIR) -> str:
        lines = [BEGIN_MARK]
        lines.append(
            f'provider add {CPA_ID} --type {CPA_TYPE} --base-url "{self._loopback_url(ir.base_url)}" --api-key "{ir.api_key}"'
        )
        for m in ir.models:
            cw = m.context_window or 256000
            mt = m.max_tokens or 16384
            lines.append(
                f'model add {CPA_ID}/{m.id} --name "{m.id}" --context-window {cw} --default-max-tokens {mt}'
            )
        if ir.models:
            lines.append(f"model large {CPA_ID}/{ir.models[0].id}")
            lines.append(f"model small {CPA_ID}/{ir.models[-1].id}")
        lines.append(END_MARK)
        return "\n".join(lines)

    # ---- 写入：替换 crushrc marker 段（保留其它内容）----
    def write(self, rendered: str, *, backup: bool = False) -> None:
        cur = self.remote.read(self.path)
        seg = "\n" + rendered + "\n"
        if BEGIN_MARK in cur:
            new = re.sub(
                re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK),
                lambda _m: seg,
                cur,
                flags=re.S,
            )
        else:
            new = (cur.rstrip() + "\n" + seg) if cur.strip() else seg.lstrip("\n") + "\n"
        self.remote.write(self.path, new)

    # ---- 差异：crush 与 cpa 同机（meilan-phone1），base_url 固定回环 127.0.0.1 ----
    def diff(self, current: ProviderIR | None, target: ProviderIR) -> list[str]:
        lines = super().diff(current, target)
        # crush 同机连 cpa，base_url 是回环（127.0.0.1），与 target 的局域网地址
        # 主机段不同是 crush.py 的固定细节，不参与差异对比（模型/密钥仍对比）。
        return [l for l in lines if not l.startswith(f"  [{self.name}] base_url:")]

    # ---- 内部辅助 ----
    @staticmethod
    def _loopback_url(url: str) -> str:
        """http://192.168.31.213:8317/v1 -> http://127.0.0.1:8317/v1（crush 同机 cpa 回环）。"""
        m = re.match(r"(https?://)[^/:]+(:\d+)?(.*)", url)
        if m:
            return f"http://127.0.0.1{m.group(2) or ''}{m.group(3) or ''}"
        return url

    @staticmethod
    def _extract_segment(text: str) -> str | None:
        m = re.search(re.escape(BEGIN_MARK) + r"(.*?)" + re.escape(END_MARK), text, flags=re.S)
        return m.group(1) if m else None

    @staticmethod
    def _segment_to_ir(seg: str) -> ProviderIR:
        from core.ir import ModelIR

        base_url = ""
        api_key = ""
        models = []
        for line in seg.splitlines():
            line = line.strip()
            if line.startswith("provider add " + CPA_ID):
                base_url = CrushAdapter._flag(line, "--base-url")
                api_key = CrushAdapter._flag(line, "--api-key")
            elif line.startswith("model add " + CPA_ID + "/"):
                rest = line[len("model add " + CPA_ID + "/"):].split()
                if not rest:
                    continue
                mid = rest[0]
                cw = int(CrushAdapter._flag(line, "--context-window") or 256000)
                mt = int(CrushAdapter._flag(line, "--default-max-tokens") or 16384)
                models.append(ModelIR(id=mid, context_window=cw, max_tokens=mt, input_modes=["text"], reasoning=False))
        if not models and not base_url:
            return None
        return ProviderIR(base_url=base_url, api_key=api_key, api="openai-completions", models=models)

    @staticmethod
    def _flag(line: str, name: str) -> str:
        m = re.search(re.escape(name) + r'\s+"?([^"\s]+)"?', line)
        return m.group(1) if m else ""
