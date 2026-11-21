"""适配器注册表。

自动发现 adapters/ 下的 Adapter 子类，无需手动注册。
新增 adapter = 在 adapters/ 加一个模块 + 子类化 Adapter，自动被识别。
"""
from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import TYPE_CHECKING

from .base import Adapter

if TYPE_CHECKING:
    from .ir import ProviderIR


_REGISTRY: dict[str, type[Adapter]] = {}


def _discover() -> None:
    if _REGISTRY:
        return
    import adapters as pkg  # noqa: F401  # 确保包可导入

    for modinfo in pkgutil.iter_modules(pkg.__path__):
        if modinfo.name.startswith("_") or modinfo.name == "base":
            continue
        try:
            mod = importlib.import_module(f"adapters.{modinfo.name}")
        except Exception:
            continue
        for _, obj in inspect.getmembers(mod, inspect.isclass):
            if issubclass(obj, Adapter) and obj is not Adapter and getattr(obj, "name", ""):
                _REGISTRY[obj.name] = obj


def get_adapter(name: str, config=None) -> Adapter:
    """按名字实例化 adapter（未注册会尝试自动发现）。"""
    _discover()
    cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(f"未知 adapter: {name}（已注册: {sorted(_REGISTRY)}）")
    return cls(config=config)


def all_adapters(config=None) -> dict[str, Adapter]:
    """返回所有已注册 adapter 的实例。"""
    _discover()
    return {name: cls(config=config) for name, cls in _REGISTRY.items()}


def available_names() -> list[str]:
    _discover()
    return sorted(_REGISTRY)
