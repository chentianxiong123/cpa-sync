"""cpa-sync 核心逻辑单元测试。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

# 项目根加入 path，保证能 import core/adapters
ROOT = Path(__file__).resolve().parent.parent
import sys
sys.path.insert(0, str(ROOT))

from core.config import SyncConfig, filter_models
from core.ir import ModelIR, ProviderIR


class TestFilterModels(unittest.TestCase):
    def setUp(self):
        self.models = [
            ModelIR(id="deepseek-v4-flash"),
            ModelIR(id="glm-5.3"),
            ModelIR(id="sensenova-6.8-flash-lite"),
            ModelIR(id="qwen3.8-27b"),
        ]

    def test_empty_include_all(self):
        out = filter_models(self.models, [], [])
        self.assertEqual(len(out), 4)

    def test_include_whitelist(self):
        out = filter_models(self.models, ["glm-5.3"], [])
        self.assertEqual([m.id for m in out], ["glm-5.3"])

    def test_include_prefix_match(self):
        # include 里带前缀（sensenova/xxx）：按前缀段匹配 id 前缀
        out = filter_models(self.models, ["sensenova/deepseek-v4-flash"], [])
        self.assertEqual([m.id for m in out], ["sensenova-6.8-flash-lite"])

    def test_include_suffix_match(self):
        # include 不带 /：按后缀匹配
        out = filter_models(self.models, ["deepseek-v4-flash"], [])
        self.assertEqual([m.id for m in out], ["deepseek-v4-flash"])

    def test_exclude_wildcard(self):
        out = filter_models(self.models, [], ["*-flash*"])
        self.assertEqual([m.id for m in out], ["glm-5.3", "qwen3.8-27b"])


class TestSyncConfig(unittest.TestCase):
    def _write(self, yaml_text: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(yaml_text)
        return path

    def test_load_defaults(self):
        path = self._write("cpa:\n  base_url: http://x/v1\n")
        try:
            cfg = SyncConfig.load(path)
            self.assertEqual(cfg.cpa.base_url, "http://x/v1")
            self.assertEqual(cfg.cpa.context_window, 512000)
            self.assertEqual(cfg.cpa.auto_compact_token_limit, 300000)
            self.assertEqual(cfg.strategy, "merge")
        finally:
            os.unlink(path)

    def test_load_custom(self):
        path = self._write(
            "cpa:\n"
            "  context_window: 1000000\n"
            "  auto_compact_token_limit: 400000\n"
            "  max_tokens: 8192\n"
            "strategy: replace\n"
        )
        try:
            cfg = SyncConfig.load(path)
            self.assertEqual(cfg.cpa.context_window, 1000000)
            self.assertEqual(cfg.cpa.auto_compact_token_limit, 400000)
            self.assertEqual(cfg.cpa.max_tokens, 8192)
            self.assertEqual(cfg.strategy, "replace")
        finally:
            os.unlink(path)

    def test_model_overrides(self):
        path = self._write(
            "cpa:\n"
            "  model_overrides:\n"
            "    glm-5.3:\n"
            "      contextWindow: 256000\n"
        )
        try:
            cfg = SyncConfig.load(path)
            self.assertEqual(cfg.cpa.model_overrides["glm-5.3"]["contextWindow"], 256000)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
