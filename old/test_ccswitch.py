"""_ccswitch.sync_providers 事务测试（临时 SQLite 库）。"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from adapters import _ccswitch


def _make_db() -> str:
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE providers (
            id TEXT PRIMARY KEY,
            app_type TEXT NOT NULL,
            name TEXT NOT NULL,
            settings_config TEXT,
            website_url TEXT,
            category TEXT,
            created_at INTEGER,
            sort_index INTEGER,
            notes TEXT,
            icon TEXT,
            icon_color TEXT,
            meta TEXT,
            is_current INTEGER DEFAULT 0,
            provider_type TEXT
        );
        """
    )
    con.commit()
    con.close()
    return path


class TestSyncProviders(unittest.TestCase):
    def setUp(self):
        self._db = _make_db()
        # 直接 patch 模块常量（CC_SWITCH_DB 是 import 时固定的，改环境变量无效）
        self._old_db = _ccswitch.CC_SWITCH_DB
        _ccswitch.CC_SWITCH_DB = self._db

    def tearDown(self):
        _ccswitch.CC_SWITCH_DB = self._old_db
        os.unlink(self._db)

    def _seed(self, app_type, name, notes=None, current=0):
        con = sqlite3.connect(self._db)
        con.execute(
            "INSERT INTO providers (id, app_type, name, settings_config, notes, created_at, is_current)"
            " VALUES (?,?,?,?,?,?,?)",
            (f"{app_type}-{name}-seed", app_type, name,
             json.dumps({"env": {"ANTHROPIC_MODEL": name}}), notes, 1, current),
        )
        con.commit()
        con.close()

    def test_replaces_and_sets_current(self):
        # 旧的非 cpa provider + 旧的 cpa-* 记录
        self._seed("codex", "old-provider")
        self._seed("codex", "cpa-OldModel")
        want = [
            {"name": "glm-5.3", "notes": "cpa", "settings_config": {"config": "model = \"glm-5.3\"\n"}},
            {"name": "deepseek-v4-flash", "notes": "cpa", "settings_config": {"config": "model = \"deepseek-v4-flash\"\n"}},
        ]
        _ccswitch.sync_providers("codex", want, current_name="glm-5.3")
        provs = _ccswitch.list_providers("codex")
        # list_providers 按 name 字母序返回
        names = [p["name"] for p in provs]
        self.assertEqual(names, ["deepseek-v4-flash", "glm-5.3"])
        cur = [p["name"] for p in provs if p["is_current"]]
        self.assertEqual(cur, ["glm-5.3"])
        # notes 写入
        self.assertTrue(all(p["notes"] == "cpa" for p in provs))

    def test_meta_written(self):
        want = [
            {"name": "glm-5.3", "notes": "cpa",
             "settings_config": {"config": "model = \"glm-5.3\"\n"},
             "meta": {"apiFormat": "openai_chat"}},
        ]
        _ccswitch.sync_providers("codex", want, current_name="glm-5.3")
        provs = _ccswitch.list_providers("codex")
        meta = json.loads(provs[0]["meta"])
        self.assertEqual(meta["apiFormat"], "openai_chat")

    def test_empty_wanted_removes_all(self):
        self._seed("claude", "a")
        self._seed("claude", "b")
        # 空 wanted 是防误删保护：抛 ValueError
        with self.assertRaises(ValueError):
            _ccswitch.sync_providers("claude", [], current_name=None)

    def test_update_existing_preserves_order(self):
        self._seed("codex", "glm-5.3", notes="cpa")
        want = [
            {"name": "glm-5.3", "notes": "cpa", "settings_config": {"config": "model = \"glm-5.3\"\n", "auth": {"OPENAI_API_KEY": "k"}}},
            {"name": "new-model", "notes": "cpa", "settings_config": {"config": "model = \"new-model\"\n"}},
        ]
        _ccswitch.sync_providers("codex", want, current_name="new-model")
        provs = _ccswitch.list_providers("codex")
        self.assertEqual([p["name"] for p in provs], ["glm-5.3", "new-model"])
        cur = [p["name"] for p in provs if p["is_current"]]
        self.assertEqual(cur, ["new-model"])


if __name__ == "__main__":
    unittest.main()
