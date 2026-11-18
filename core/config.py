"""中央配置 (cpa-config.yaml) 的加载与默认值。

中央配置是唯一事实来源：用户在这里填 CPA 的 IP/密钥/模型白名单，
sync 引擎据此生成 IR 并分发到各 adapter。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# 默认配置路径：基于项目根目录自动推导（core/ 的上一级），不写死绝对路径。
# 这样项目拷到任何位置（中心机/目标机/发行目录）都能直接用。
# 仍可用环境变量 CPA_SYNC_CONFIG 覆盖。
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = os.environ.get(
    "CPA_SYNC_CONFIG", str(_PROJECT_ROOT / "config" / "cpa-config.yaml")
)


@dataclass
class CPASettings:
    """CPA 连接信息 + 模型选择。"""

    base_url: str = "http://YOUR_CPA_IP:8317/v1"
    api_key: str = "YOUR_CPA_API_KEY"
    # 供应商标识：下游（pi/opencode 等）用它作 provider 的 key（如 "cpa"）。
    # 有意义且稳定，不要用随机 UUID。历史遗留的 UUID key 会在 apply 时自动迁移。
    provider_id: str = "cpa"
    include_models: list[str] = field(default_factory=list)  # 空 = 全部
    exclude_models: list[str] = field(default_factory=list)  # 支持通配符
    # 全局上下文窗口（默认 500K，所有下游统一）
    context_window: int = 512000
    max_tokens: int = 16384
    # codex 的自动压缩阈值（model_auto_compact_token_limit），中央统一控制
    auto_compact_token_limit: int = 300000
    # 每个模型可选的覆盖（contextWindow/maxTokens/input/reasoning 等）
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 全局默认 input 模式（无覆盖、无识图匹配时的兜底）。如 ["text"] 或 ["text", "image"]。
    default_input_modes: list[str] = field(default_factory=lambda: ["text"])
    # 识图模型匹配：模型名通配符（fnmatch），命中则 input 加 "image"（叠加到 default_input_modes 上）。
    # 例：["*"] 全开；["sensenova-*", "gemini-*"] 部分开。
    # 单个模型想排除/收紧，用 model_overrides 显式写 input（优先级最高）。
    image_models: list[str] = field(default_factory=list)


@dataclass
class HostDef:
    """一台机器（可记忆：ssh 别名 + 系统类型）。"""

    ssh: str
    platform: str = "win"  # "win" | "unix"


@dataclass
class AdapterInstance:
    """一个 adapter 接入点（某台机器上某个 app 的配置文件）。"""

    name: str
    host: str
    path: str
    platform: str | None = None  # None = 从 hosts 注册表查


@dataclass
class SyncConfig:
    """完整同步配置。"""

    cpa: CPASettings = field(default_factory=CPASettings)
    targets: list[str] = field(default_factory=list)  # 启用的 adapter 名
    # 模型同步策略: "merge"(只增不删, 默认安全) | "replace"(全盘对齐)
    strategy: str = "merge"
    # 机器注册表（记忆哪些机器、什么系统）
    hosts: dict[str, HostDef] = field(default_factory=dict)
    # adapter 接入点（可配置可选择：每个 adapter 可接多台机器）
    adapter_instances: dict[str, list[AdapterInstance]] = field(default_factory=dict)
    path: str = DEFAULT_CONFIG_PATH

    def instance_for(self, adapter_name: str) -> list[AdapterInstance]:
        """返回某 adapter 的接入点列表（配置里没配则空）。"""
        return self.adapter_instances.get(adapter_name, []) or []

    @classmethod
    def load(cls, path: str | None = None) -> "SyncConfig":
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"中央配置不存在: {cfg_path}")
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cpa_raw = raw.get("cpa", {}) or {}
        hosts_raw = raw.get("hosts", {}) or {}
        hosts = {
            k: HostDef(ssh=str(v.get("ssh", k)), platform=str(v.get("platform", "win")))
            for k, v in hosts_raw.items()
        } if isinstance(hosts_raw, dict) else {}
        adapter_instances: dict[str, list[AdapterInstance]] = {}
        for aname, insts in (raw.get("adapter_instances", {}) or {}).items():
            if not isinstance(insts, list):
                continue
            items = []
            for i in insts:
                if not isinstance(i, dict):
                    continue
                h = str(i.get("host", ""))
                # platform 优先取接入点显式值，否则从 hosts 注册表查
                plat = i.get("platform")
                if plat is None and h in hosts:
                    plat = hosts[h].platform
                items.append(
                    AdapterInstance(
                        name=str(i.get("name", h)),
                        host=h,
                        path=str(i.get("path", "")),
                        platform=plat,
                    )
                )
            adapter_instances[aname] = items
        return cls(
            cpa=CPASettings(
                base_url=cpa_raw.get("base_url", "http://YOUR_CPA_IP:8317/v1"),
                api_key=str(cpa_raw.get("api_key", "YOUR_CPA_API_KEY")),
                provider_id=str(cpa_raw.get("provider_id", "cpa") or "cpa"),
                include_models=list(cpa_raw.get("include_models", []) or []),
                exclude_models=list(cpa_raw.get("exclude_models", []) or []),
                context_window=int(cpa_raw.get("context_window", 512000) or 512000),
                max_tokens=int(cpa_raw.get("max_tokens", 16384) or 16384),
                auto_compact_token_limit=int(
                    cpa_raw.get("auto_compact_token_limit", 300000) or 300000
                ),
                model_overrides=dict(cpa_raw.get("model_overrides", {}) or {}),
                default_input_modes=list(cpa_raw.get("default_input_modes", ["text"]) or ["text"]),
                image_models=list(cpa_raw.get("image_models", []) or []),
            ),
            targets=list(raw.get("targets", []) or []),
            strategy=str(raw.get("strategy", "merge") or "merge"),
            hosts=hosts,
            adapter_instances=adapter_instances,
            path=cfg_path,
        )


def filter_models(
    models: list[ModelIR],
    include: list[str],
    exclude: list[str],
) -> list[ModelIR]:
    """按白名单/黑名单过滤模型。include 为空 = 全部。"""
    import fnmatch

    def excluded(mid: str) -> bool:
        for pat in exclude:
            if fnmatch.fnmatch(mid, pat):
                return True
        return False

    if include:
        # include 里的名字可能带前缀（如 sensenova/deepseek-v4-flash，按前缀匹配 id），
        # 也允许按后缀匹配（deepseek-v4-flash，不带 / 时按 endswith 匹配）。
        result = []
        for m in models:
            hit = False
            for inc in include:
                if m.id == inc:
                    hit = True
                    break
                if "/" in inc:
                    # 带前缀：如 "sensenova/deepseek-v4-flash" 匹配 "sensenova-6.8-flash-lite"？
                    # 这里按前缀段匹配：inc 前缀部分匹配 id 前缀，或整体按后缀
                    prefix = inc.split("/")[0]
                    if m.id.startswith(prefix):
                        hit = True
                        break
                elif m.id.endswith(inc):
                    hit = True
                    break
            if hit:
                result.append(m)
        models = result
    return [m for m in models if not excluded(m.id)]


def resolve_input_modes(mid: str, ov: dict[str, Any], cfg: SyncConfig) -> list[str]:
    """决定一个模型最终的 input 模式（识图能力）。

    优先级：
      1. model_overrides[id].input 显式给出 → 完全尊重（可开可关）
      2. 否则 base = default_input_modes；若 image_models 通配符命中 → base 加 "image"
    """
    import fnmatch

    if "input" in ov:
        return list(ov["input"] or ["text"])
    base = list(cfg.cpa.default_input_modes or ["text"])
    if "image" not in base and any(
        fnmatch.fnmatch(mid, pat) for pat in (cfg.cpa.image_models or [])
    ):
        base = base + ["image"]
    return base


def build_provider_ir(cfg: SyncConfig, cpa_models: list[ModelIR]) -> ProviderIR:
    """由中央配置 + CPA 能力构建最终要分发的 ProviderIR。"""
    from .ir import ModelIR, ProviderIR

    overrides = cfg.cpa.model_overrides
    final: list[ModelIR] = []
    for m in filter_models(cpa_models, cfg.cpa.include_models, cfg.cpa.exclude_models):
        ov = overrides.get(m.id, {})
        final.append(
            ModelIR(
                id=m.id,
                context_window=ov.get("contextWindow", cfg.cpa.context_window),
                max_tokens=ov.get("maxTokens", cfg.cpa.max_tokens),
                input_modes=resolve_input_modes(m.id, ov, cfg),
                reasoning=ov.get("reasoning", False),
                extra=m.extra,
            )
        )
    return ProviderIR(
        provider_id=cfg.cpa.provider_id,
        base_url=cfg.cpa.base_url,
        api_key=cfg.cpa.api_key,
        models=final,
        meta={
            "context_window": cfg.cpa.context_window,
            "auto_compact_token_limit": cfg.cpa.auto_compact_token_limit,
        },
    )
