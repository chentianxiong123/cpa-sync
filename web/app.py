#!/usr/bin/env python3
"""cpa-sync Web 控制面板 —— 纯标准库实现（零新依赖）。

覆盖 cpa-sync 全部功能：
- 中心模式命令（models/list/check/apply/doctor）—— 复用 cpa-sync cmd_*
- 客户端模式命令（agent-sync list/check/apply/doctor）—— 复用 agent-sync main(argv)
- 一次性脚本（omp_disable_providers / picoclaw_disable_builtin）
- 配置读写（cpa-config.yaml：cpa/识图三层/strategy/hosts/adapter_instances/targets）
- 动态 adapter 发现（adapters.available_names）

用法: .venv/bin/python web/app.py [--host 127.0.0.1] [--port 17834]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 复用 CLI（与 gui/app.py 同法：SourceFileLoader）
_cli = SourceFileLoader("cpa_sync_cli", str(ROOT / "cpa-sync")).load_module()
_agent = SourceFileLoader("agent_sync_cli", str(ROOT / "agent-sync")).load_module()
import adapters  # noqa: E402  # available_names() 动态发现

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

CONFIG_PATH = str(ROOT / "config" / "cpa-config.yaml")
INDEX_PATH = Path(__file__).parent / "static" / "index.html"
VENV_PY = str(ROOT / ".venv" / "bin" / "python")

# 中心 / 客户端 命令集合
CENTRAL_CMDS = {"models", "list", "check", "apply", "doctor"}
CLIENT_CMDS = {"list", "check", "apply", "doctor"}
SCRIPTS = {
    "omp_disable_providers": "scripts/omp_disable_providers.py",
    "picoclaw_disable_builtin": "scripts/picoclaw_disable_builtin.py",
}


def _make_args() -> argparse.Namespace:
    return argparse.Namespace(config=CONFIG_PATH, targets=[], yes=True, backup=False, force=False)


def run_captured(fn) -> dict:
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = fn()
    except SystemExit as e:  # argparse / SystemExit
        code = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        buf.write(f"\n✗ {type(e).__name__}: {e}\n")
        code = 1
    return {"code": code, "output": buf.getvalue()}


def run_central(name: str) -> dict:
    fn = getattr(_cli, f"cmd_{name}", None)
    if fn is None:
        return {"code": 2, "output": f"未知中心命令: {name}"}
    return run_captured(lambda: fn(_make_args()))


def run_client(name: str) -> dict:
    if name not in CLIENT_CMDS:
        return {"code": 2, "output": f"未知客户端命令: {name}"}
    return run_captured(lambda: _agent.main([name]))


def run_script(name: str, params: dict | None) -> dict:
    rel = SCRIPTS.get(name)
    if rel is None:
        return {"code": 2, "output": f"未知脚本: {name}"}
    cmd = [VENV_PY, str(ROOT / rel)]
    for k, v in (params or {}).items():
        cmd.append("--" + k.replace("_", "-"))
        if v is not True:
            cmd.append(str(v))
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        return {"code": r.returncode, "output": (r.stdout or "") + (r.stderr or "")}
    except Exception as e:  # noqa: BLE001
        return {"code": 1, "output": f"✗ {type(e).__name__}: {e}"}


def get_adapters_info() -> dict:
    """动态 adapter 列表 + 配置里的 hosts / adapter_instances / targets。"""
    try:
        names = adapters.available_names()
    except Exception as e:  # noqa: BLE001
        names = []
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        cfg = {}
    return {
        "names": names,
        "hosts": cfg.get("hosts", {}) or {},
        "adapter_instances": cfg.get("adapter_instances", {}) or {},
        "targets": cfg.get("targets", []) or [],
        "strategies": ["replace", "merge"],
    }


def get_models() -> dict:
    """拉取 CPA 实际模型列表 + 每个模型的当前覆盖（model_overrides 驼峰字段）。"""
    from core.config import SyncConfig
    from core.cpa_client import CPAClient

    cfg = SyncConfig.load(CONFIG_PATH)
    client = CPAClient(cfg.cpa.base_url, cfg.cpa.api_key)
    try:
        models = client.list_models()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "models": [], "cpa": {}}
    ovs = cfg.cpa.model_overrides or {}
    return {
        "ok": True,
        "cpa": {
            "context_window": cfg.cpa.context_window,
            "max_tokens": cfg.cpa.max_tokens,
            "auto_compact_token_limit": cfg.cpa.auto_compact_token_limit,
            "default_input_modes": cfg.cpa.default_input_modes,
            "image_models": cfg.cpa.image_models,
        },
        "models": [{"id": m.id, "override": ovs.get(m.id, {}) or {}} for m in models],
    }


def get_version() -> dict:
    import re
    ver = "?"
    try:
        with open(str(ROOT / "cpa-sync"), encoding="utf-8") as f:
            m = re.search(r'VERSION\s*=\s*"([^"]+)"', f.read())
            if m:
                ver = m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return {"cpa_sync": ver, "agent_sync": "1.0.0", "adapters": len(adapters.available_names())}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: dict, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    # ---- GET ----
    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            html = INDEX_PATH.read_bytes() if INDEX_PATH.exists() else "<h1>index.html 缺失</h1>".encode("utf-8")
            return self._send(200, html, "text/html; charset=utf-8")
        if p == "/api/config":
            try:
                with open(CONFIG_PATH, encoding="utf-8") as f:
                    return self._json({"ok": True, "config": yaml.safe_load(f) or {}})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/adapters":
            return self._json({"ok": True, **get_adapters_info()})
        if p == "/api/models":
            return self._json(get_models())
        if p == "/api/version":
            return self._json({"ok": True, **get_version()})
        self._json({"ok": False, "error": "not found"}, 404)

    # ---- POST ----
    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            ln = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(ln) if ln else b"{}"
            payload = json.loads(body) if body else {}
        except Exception as e:  # noqa: BLE001
            return self._json({"ok": False, "error": f"bad json: {e}"}, 400)

        if p == "/api/config":
            if yaml is None:
                return self._json({"ok": False, "error": "pyyaml 未安装"}, 500)
            cfg = payload.get("config")
            if not isinstance(cfg, dict):
                return self._json({"ok": False, "error": "config 必须为对象"}, 400)
            try:
                with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                    yaml.safe_dump(cfg, f, allow_unicode=True, sort_keys=False, default_flow_style=False)
                return self._json({"ok": True})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)

        if p == "/api/run":
            mode = payload.get("mode", "central")
            cmd = payload.get("cmd", "")
            if mode == "client":
                if cmd not in CLIENT_CMDS:
                    return self._json({"ok": False, "error": f"未知客户端命令: {cmd}"}, 400)
                return self._json({"ok": True, **run_client(cmd)})
            if cmd not in CENTRAL_CMDS:
                return self._json({"ok": False, "error": f"未知中心命令: {cmd}"}, 400)
            return self._json({"ok": True, **run_central(cmd)})

        if p == "/api/scripts":
            name = payload.get("name", "")
            return self._json({"ok": True, **run_script(name, payload.get("params"))})

        self._json({"ok": False, "error": "not found"}, 404)


def main() -> int:
    p = argparse.ArgumentParser(description="cpa-sync Web 控制面板")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=17834)
    a = p.parse_args()
    print(f"cpa-sync Web 控制面板 → http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
