"""CPA 同步的中间表示 (IR)。

IR 是所有 adapter 的通用数据格式。核心引擎从中央配置 + CPA /v1/models
生成 IR，每个 adapter 负责把 IR 渲染成目标应用的配置格式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ModelIR:
    """单个模型在 IR 中的表示。"""

    id: str
    context_window: int = 128000
    max_tokens: int = 16384
    input_modes: list[str] = field(default_factory=lambda: ["text"])
    reasoning: bool = False
    # 保留 CPA /v1/models 里其它未识别字段，adapter 可自行决定是否用
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "contextWindow": self.context_window,
            "maxTokens": self.max_tokens,
            "input": self.input_modes,
            "reasoning": self.reasoning,
            **self.extra,
        }


@dataclass
class ProviderIR:
    """一个上游 provider（通常是 CPA 网关）在 IR 中的表示。"""

    name: str = "cpa"
    base_url: str = ""
    api_key: str = ""
    api: str = "openai-completions"
    models: list[ModelIR] = field(default_factory=list)
    # 附加信息：如 display_name、retry 策略等，adapter 可读取
    meta: dict[str, Any] = field(default_factory=dict)

    def model_ids(self) -> list[str]:
        return [m.id for m in self.models]
