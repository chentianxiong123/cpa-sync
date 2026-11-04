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




class TestProviderId(unittest.TestCase):
    """供应商标识（provider_id）默认值 + 可配置。"""

    def test_default_provider_id(self):
        # 默认就是有意义的 "cpa"，不是 UUID
        from core.config import build_provider_ir
        ir = build_provider_ir(cfg_with_cpa(), [ModelIR(id="glm-5.2")])
        self.assertEqual(ir.provider_id, "cpa")

    def test_configurable_provider_id(self):
        from core.config import build_provider_ir
        cfg = cfg_with_cpa(provider_id="my-cpa")
        ir = build_provider_ir(cfg, [ModelIR(id="glm-5.2")])
        self.assertEqual(ir.provider_id, "my-cpa")

    def test_config_yaml_parses_provider_id(self):
        fd, path = tempfile.mkstemp(suffix=".yaml")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("cpa:\n  provider_id: my-cpa\n")
        try:
            cfg = SyncConfig.load(path)
            self.assertEqual(cfg.cpa.provider_id, "my-cpa")
        finally:
            os.unlink(path)


class TestPiMigration(unittest.TestCase):
    """pi adapter 把历史遗留的 UUID key 迁移到 provider_id。"""

    def _make(self, providers: dict):
        import tempfile as _tf
        tmp = _tf.NamedTemporaryFile(suffix=".json", mode="w", delete=False)
        json.dump({"providers": providers}, tmp)
        tmp.close()
        return tmp.name

    def test_uuid_key_migrated_to_provider_id(self):
        from adapters.pi import PiAdapter

        cfg = cfg_with_cpa(provider_id="cpa")
        ad = PiAdapter(cfg)
        ad.path = self._make({
            "b1fb7326-84d3-4a7f-b707-fa4dfe3ec067": {
                "name": "cpa", "baseUrl": "http://old/v1",
                "api": "openai-completions", "apiKey": "old", "models": [],
            },
            "sensenova": {"name": "sensenova", "baseUrl": "https://x/v1"},
        })
        ir = ProviderIR(
            provider_id="cpa", base_url="http://new/v1", api_key="new",
            models=[ModelIR(id="glm-5.2")],
        )
        out = ad.render(ir)
        keys = list(out["providers"].keys())
        # UUID key 删除，换成 "cpa"；sensenova 保留
        self.assertNotIn("b1fb7326-84d3-4a7f-b707-fa4dfe3ec067", keys)
        self.assertIn("cpa", keys)
        self.assertIn("sensenova", keys)
        self.assertEqual(out["providers"]["cpa"]["baseUrl"], "http://new/v1")
        self.assertEqual(out["providers"]["cpa"]["apiKey"], "new")
        os.unlink(ad.path)

    def test_existing_provider_id_key_kept(self):
        from adapters.pi import PiAdapter

        cfg = cfg_with_cpa(provider_id="cpa")
        ad = PiAdapter(cfg)
        ad.path = self._make({
            "cpa": {"name": "cpa", "baseUrl": "http://old/v1",
                    "api": "openai-completions", "apiKey": "old", "models": []},
        })
        ir = ProviderIR(
            provider_id="cpa", base_url="http://new/v1", api_key="new",
            models=[ModelIR(id="glm-5.2")],
        )
        out = ad.render(ir)
        self.assertEqual(list(out["providers"].keys()), ["cpa"])
        self.assertEqual(out["providers"]["cpa"]["baseUrl"], "http://new/v1")
        os.unlink(ad.path)


def cfg_with_cpa(provider_id: str = "cpa"):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"cpa:\n  base_url: http://x/v1\n  provider_id: {provider_id}\n")
    try:
        return SyncConfig.load(path)
    finally:
        os.unlink(path)
