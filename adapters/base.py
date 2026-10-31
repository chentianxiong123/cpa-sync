"""Adapter 抽象基类。

每个下游应用一个 Adapter 子类，只需实现：
  - load_current()  : 读取目标应用当前配置（用于 diff / 只读模式）
  - render()        : IR -> 目标应用配置格式
  - write()         : 写入目标应用（含备份）
所有子类共享 diff() / check() 逻辑。
"""
from __future__ import annotations

import abc
import shutil
import time
from pathlib import Path
from typing import Any

from core.ir import ProviderIR


class Adapter(abc.ABC):
    """同步适配器基类。"""

    name: str = ""          # 子类覆盖，如 "dsh"
    label: str = ""         # 人类可读名

    def __init__(self, config: Any = None):
        self.config = config

    # ---- 子类必须实现 ----
    @abc.abstractmethod
    def load_current(self) -> ProviderIR | None:
        """读取目标应用当前 CPA provider 配置，返回 IR；读不到返回 None。"""

    @abc.abstractmethod
    def render(self, ir: ProviderIR) -> Any:
        """把 IR 渲染成目标应用的写入数据（dict/str/bytes）。"""

    @abc.abstractmethod
    def write(self, rendered: Any, *, backup: bool = False) -> None:
        """把 render() 结果写入目标应用。backup=True 时写前自动备份。"""

    # ---- 子类可选覆写 ----
    def iter_instances(self) -> list[tuple[str, "Adapter"]]:
        """返回 [(接入点名, adapter)]。默认单实例；远程多机接入点可覆写。"""
        return [("default", self)]

    def backup(self, path: str | Path, backup_dir: str | None = None) -> str | None:
        """写入前备份单个文件，返回备份路径。"""
        p = Path(path)
        if not p.exists():
            return None
        bdir = Path(backup_dir) if backup_dir else p.parent / f"{p.name}.bak"
        bdir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = bdir / f"{p.name}.{stamp}"
        shutil.copy2(p, dest)
        return str(dest)

    # ---- 同步策略 ----
    def merge_with_current(self, target: ProviderIR) -> ProviderIR:
        """按策略处理模型清单。

        strategy == "merge"（默认，只增不删）：保留下游当前有的、
        但目标白名单里没有的模型（不删除）。
        strategy == "replace"（全盘对齐）：严格等于目标白名单。

        注意：base_url / api_key 始终精确覆盖，不受此策略影响。
        """
        if getattr(self.config, "strategy", "merge") != "merge":
            return target
        try:
            cur = self.load_current()
        except Exception:
            return target
        if cur is None:
            return target
        # 下游当前有、目标没有的模型 → 保留（不删）
        tgt_ids = set(target.model_ids())
        extra = [m for m in cur.models if m.id not in tgt_ids]
        if extra:
            target = ProviderIR(
                name=target.name,
                base_url=target.base_url,
                api_key=target.api_key,
                api=target.api,
                models=list(target.models) + extra,
                meta=target.meta,
            )
        return target

    # ---- 公共逻辑 ----
    def diff(self, current: ProviderIR | None, target: ProviderIR) -> list[str]:
        """返回人类可读的差异行；空列表 = 无差异。"""
        lines: list[str] = []
        cur = current
        if cur is None:
            lines.append(f"  [{self.name}] 当前无配置，将新建")
        else:
            if cur.base_url != target.base_url:
                lines.append(f"  [{self.name}] base_url: {cur.base_url} -> {target.base_url}")
            if cur.api_key != target.api_key:
                lines.append(f"  [{self.name}] api_key: {'****' if cur.api_key else '(空)'} -> {'****' if target.api_key else '(空)'}")
            cur_ids = set(cur.model_ids())
            tgt_ids = set(target.model_ids())
            added = sorted(tgt_ids - cur_ids)
            removed = sorted(cur_ids - tgt_ids)
            if added:
                lines.append(f"  [{self.name}] 新增模型: {', '.join(added)}")
            if removed:
                lines.append(f"  [{self.name}] 移除模型: {', '.join(removed)}")
            # contextWindow / maxTokens 不一致 -> 触发重写（字段级对齐）
            for mid in sorted(tgt_ids & cur_ids):
                cm = next((m for m in cur.models if m.id == mid), None)
                tm = next((m for m in target.models if m.id == mid), None)
                if cm is None or tm is None:
                    continue
                if cm.context_window != tm.context_window:
                    lines.append(f"  [{self.name}] 模型 {mid}: contextWindow {cm.context_window} -> {tm.context_window}")
                if cm.max_tokens != tm.max_tokens:
                    lines.append(f"  [{self.name}] 模型 {mid}: maxTokens {cm.max_tokens} -> {tm.max_tokens}")
        return lines

    def check(self, target: ProviderIR) -> list[str]:
        """只对比不写，返回差异。"""
        current = self.load_current()
        return self.diff(current, target)
