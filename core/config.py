"""中央配置 (cpa-config.yaml) 的加载与默认值。

中央配置是唯一事实来源：用户在这里填 CPA 的 IP/密钥/模型白名单，
sync 引擎据此生成 IR 并分发到各 adapter。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = os.environ.get(
    "CPA_SYNC_CONFIG", "/mnt/shared/cpa-sync/config/cpa-config.yaml"
)


@dataclass
class CPASettings:
    """CPA 连接信息 + 模型选择。"""

    base_url: str = "http://192.168.31.213:8317/v1"
    api_key: str = "123456"
    include_models: list[str] = field(default_factory=list)  # 空 = 全部
    exclude_models: list[str] = field(default_factory=list)  # 支持通配符
    # 全局上下文窗口（默认 500K，所有下游统一）
    context_window: int = 512000
    max_tokens: int = 16384
    # codex 的自动压缩阈值（model_auto_compact_token_limit），中央统一控制
    auto_compact_token_limit: int = 300000
    # 每个模型可选的覆盖（contextWindow/maxTokens 等）
    model_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SyncConfig:
    """完整同步配置。"""

    cpa: CPASettings = field(default_factory=CPASettings)
    targets: list[str] = field(default_factory=list)  # 启用的 adapter 名
    # 模型同步策略: "merge"(只增不删, 默认安全) | "replace"(全盘对齐)
    strategy: str = "merge"
    path: str = DEFAULT_CONFIG_PATH

    @classmethod
    def load(cls, path: str | None = None) -> "SyncConfig":
        cfg_path = path or DEFAULT_CONFIG_PATH
        if not os.path.exists(cfg_path):
            raise FileNotFoundError(f"中央配置不存在: {cfg_path}")
        with open(cfg_path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cpa_raw = raw.get("cpa", {}) or {}
        return cls(
            cpa=CPASettings(
                base_url=cpa_raw.get("base_url", "http://192.168.31.213:8317/v1"),
                api_key=str(cpa_raw.get("api_key", "123456")),
                include_models=list(cpa_raw.get("include_models", []) or []),
                exclude_models=list(cpa_raw.get("exclude_models", []) or []),
                context_window=int(cpa_raw.get("context_window", 512000) or 512000),
                max_tokens=int(cpa_raw.get("max_tokens", 16384) or 16384),
                auto_compact_token_limit=int(
                    cpa_raw.get("auto_compact_token_limit", 300000) or 300000
                ),
                model_overrides=dict(cpa_raw.get("model_overrides", {}) or {}),
            ),
            targets=list(raw.get("targets", []) or []),
            strategy=str(raw.get("strategy", "merge") or "merge"),
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
                input_modes=ov.get("input", ["text"]),
                reasoning=ov.get("reasoning", False),
                extra=m.extra,
            )
        )
    return ProviderIR(
        base_url=cfg.cpa.base_url,
        api_key=cfg.cpa.api_key,
        models=final,
        meta={
            "context_window": cfg.cpa.context_window,
            "auto_compact_token_limit": cfg.cpa.auto_compact_token_limit,
        },
    )
