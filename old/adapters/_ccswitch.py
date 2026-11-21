"""cc-switch 数据库公共读写逻辑（claude/codex/pi/opencode 共用）。

cc-switch 用一个 SQLite 库管理多个 app 的 provider：
  providers(id, app_type, name, settings_config, is_current, ...)

本模块封装：
  - 读取某 app_type 的所有 provider
  - 更新 cpa provider 的 settings_config（指向 CPA + 模型）
  - 删除该 app_type 下除 cpa 外的所有 provider（"只留 cpa"）
  - 把 cpa 设为 is_current
写入走事务，失败自动回滚；操作前对整库做一致性快照备份。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

CC_SWITCH_DB = os.environ.get("CC_SWITCH_DB", os.path.expanduser("~/.cc-switch/cc-switch.db"))

# claude 的 env 模型键（多档）
CLAUDE_MODEL_KEYS = [
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME",
    "ANTHROPIC_MODEL",
]


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(CC_SWITCH_DB)
    con.row_factory = sqlite3.Row
    return con


def backup_db(backup_dir: str | None = None) -> str | None:
    """写库前对 cc-switch.db 做一致性快照（SQLite backup API，含 WAL）。"""
    if not os.path.exists(CC_SWITCH_DB):
        return None
    bdir = Path(backup_dir) if backup_dir else Path(CC_SWITCH_DB).parent
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = bdir / f"{Path(CC_SWITCH_DB).name}.bak.{stamp}"
    src = sqlite3.connect(CC_SWITCH_DB)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return str(dest)


def list_providers(app_type: str) -> list[dict[str, Any]]:
    """返回某 app_type 的所有 provider。"""
    con = _connect()
    try:
        cur = con.execute(
            "SELECT * FROM providers WHERE app_type=? ORDER BY sort_index, name",
            (app_type,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def get_cpa(app_type: str) -> dict[str, Any] | None:
    """返回 cpa provider 记录（无则 None）。"""
    con = _connect()
    try:
        cur = con.execute(
            "SELECT * FROM providers WHERE app_type=? AND name='cpa'", (app_type,)
        )
        r = cur.fetchone()
        return dict(r) if r else None
    finally:
        con.close()


def get_cpa_providers(app_type: str) -> list[dict[str, Any]]:
    """返回所有备注为 cpa 的 provider（notes='cpa'，或 name='cpa' 兼容旧数据）。"""
    con = _connect()
    try:
        cur = con.execute(
            "SELECT * FROM providers WHERE app_type=? AND (notes='cpa' OR name='cpa' OR name LIKE 'cpa-%')"
            " ORDER BY sort_index, name",
            (app_type,),
        )
        return [dict(r) for r in cur.fetchall()]
    finally:
        con.close()


def sync_providers(
    app_type: str,
    providers: list[dict[str, Any]],
    *,
    current_name: str | None = None,
) -> list[dict[str, Any]]:
    """把某 app_type 的 provider 集合整体对齐为 providers：删除该 app_type 下
    不在 providers 里的旧记录（按 name 匹配），新增/更新 providers 里的每条。

    providers 的每个元素形如 {"name", "settings_config"}（可选 "id"）。
    """
    if not providers:
        raise ValueError("providers 不能为空")
    con = _connect()
    try:
        # 现有 name 集合
        existing = {
            r["name"]: r["id"]
            for r in con.execute(
                "SELECT id, name FROM providers WHERE app_type=?", (app_type,)
            ).fetchall()
        }
        wanted_names = {p["name"] for p in providers}
        # 1) 删除不在 wanted 里的旧 provider
        for name in list(existing):
            if name not in wanted_names:
                con.execute(
                    "DELETE FROM providers WHERE app_type=? AND name=?", (app_type, name)
                )
        # 2) 插入或更新每条
        for p in providers:
            name = p["name"]
            cfg_json = json.dumps(p["settings_config"], ensure_ascii=False)
            notes = p.get("notes")
            meta_json = json.dumps(p.get("meta"), ensure_ascii=False) if p.get("meta") is not None else None
            if name in existing:
                con.execute(
                    "UPDATE providers SET settings_config=?, notes=?, meta=? WHERE app_type=? AND name=?",
                    (cfg_json, notes, meta_json, app_type, name),
                )
            else:
                pid = p.get("id") or f"{app_type}-{name}-{int(time.time()*1000)}"
                con.execute(
                    "INSERT INTO providers (id, app_type, name, settings_config, notes, meta, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (pid, app_type, name, cfg_json, notes, meta_json, int(time.time())),
                )
        # 3) 设当前
        cur_name = current_name or providers[0]["name"]
        con.execute("UPDATE providers SET is_current=0 WHERE app_type=?", (app_type,))
        con.execute(
            "UPDATE providers SET is_current=1 WHERE app_type=? AND name=?", (app_type, cur_name)
        )
        con.commit()
        return list_providers(app_type)
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()
