#!/usr/bin/env python3
"""cpa-sync Web 控制面板 —— 纯标准库实现（零新依赖）。

复用 cpa-sync 的 cmd_* 全部逻辑（SourceFileLoader 加载主入口），
把输出捕获成 JSON 给前端。配置读写走 cpa-config.yaml。

用法: .venv/bin/python web/app.py [--host 127.0.0.1] [--port 8080]
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 复用 CLI（cpa-sync 无 .py 后缀，用 SourceFileLoader 加载，与 gui/app.py 同法）
_cli = SourceFileLoader("cpa_sync_cli", str(ROOT / "cpa-sync")).load_module()

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

CONFIG_PATH = str(ROOT / "config" / "cpa-config.yaml")
INDEX_PATH = Path(__file__).parent / "static" / "index.html"

# 各 agent 接入点（与 gui/app.py 一致）
ADAPTER_PATHS = [
    {"agent": "pi", "path": "本机 ~/.pi/agent/models.json"},
    {"agent": "opencode", "path": "本机 ~/.config/opencode/opencode.json"},
    {"agent": "claude", "path": "本机 ~/.cc-switch/cc-switch.db（app_type=claude）"},
    {"agent": "codex", "path": "本机 ~/.cc-switch/cc-switch.db（app_type=codex）"},
    {"agent": "dsh", "path": "win11 a1@192.168.31.66:C:\\Users\\a1\\.dsh\\profiles\\web\\cordis.patch.yml"},
    {"agent": "omp", "path": "win11 a1@192.168.31.66:C:\\Users\\a1\\.omp\\agent\\models.yml"},
]

COMMANDS = {"models", "list", "check", "apply", "doctor"}


def _make_args() -> argparse.Namespace:
    """构造 CLI 命令所需的最小 args（与 gui/app.py 相同字段）。"""
    return argparse.Namespace(config=CONFIG_PATH, targets=[], yes=True, backup=False, force=False)


def run_cmd(name: str) -> dict:
    """执行一条 CLI 命令函数，捕获 stdout/stderr，返回 {code, output}。"""
    fn = {
        "models": _cli.cmd_models,
        "list": _cli.cmd_list,
        "check": _cli.cmd_check,
        "apply": _cli.cmd_apply,
        "doctor": _cli.cmd_doctor,
    }.get(name)
    if fn is None:
        return {"code": 2, "output": f"未知命令: {name}"}
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            code = fn(_make_args())
    except SystemExit as e:  # argparse / SystemExit
        code = int(e.code or 0)
    except Exception as e:  # noqa: BLE001
        buf.write(f"\n✗ {type(e).__name__}: {e}\n")
        code = 1
    return {"code": code, "output": buf.getvalue()}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # 静默访问日志
        pass

    # ---- 响应辅助 ----
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
                    data = yaml.safe_load(f) or {}
                return self._json({"ok": True, "config": data})
            except Exception as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
        if p == "/api/adapter-paths":
            return self._json({"ok": True, "adapters": ADAPTER_PATHS})
        if p == "/api/status":
            return self._json({"ok": True, **run_cmd("list")})
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
            cmd = payload.get("cmd", "")
            if cmd not in COMMANDS:
                return self._json({"ok": False, "error": f"unknown cmd: {cmd}"}, 400)
            return self._json({"ok": True, **run_cmd(cmd)})

        self._json({"ok": False, "error": "not found"}, 404)


def main() -> int:
    p = argparse.ArgumentParser(description="cpa-sync Web 控制面板")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    a = p.parse_args()
    print(f"cpa-sync Web 控制面板 → http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
