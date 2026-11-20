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
INDEX_PATH = ROOT / "web" / "static" / "index.html"
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


# ---------------- CPA 节点管理（多端 / 状态监测 / 嗅探） ----------------
def _gateway_cfg() -> dict:
    """读 config/gateway.yaml：listen / panel_port / management.key / upstreams。"""
    try:
        raw = yaml.safe_load(Path(ROOT / "config" / "gateway.yaml").read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        raw = {}
    g = raw.get("gateway", {}) or {}
    mgmt = g.get("management", {}) or {}
    return {
        "listen": g.get("listen", 8380),
        "panel_port": g.get("panel_port", 17834),
        "key": mgmt.get("key", ""),
        "upstreams": g.get("upstreams", []) or [],
    }


def _mgmt(node: dict, path: str, method: str = "GET", data=None, timeout: int = 10):
    """调 CPA 管理 API /v8/management/<path>，Bearer key。返回 (status, payload)。"""
    import urllib.error
    import urllib.request

    key = _gateway_cfg()["key"]
    url = f"http://{node['host']}:{int(node.get('port', 8317))}/v8/management/{path}"
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            return resp.status, json.loads(raw)
        except Exception:  # noqa: BLE001
            return resp.status, raw
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")[:300]
        try:
            return e.code, json.loads(raw)
        except Exception:  # noqa: BLE001
            return e.code, raw
    except Exception as e:  # noqa: BLE001
        return 0, str(e)[:200]


def _nodes_from_req(payload: dict) -> list[dict]:
    """取指定 host 节点（列表），无则全部 upstreams。"""
    cfg = _gateway_cfg()
    wanted = [str(x).strip() for x in payload.get("hosts", []) if str(x).strip()]
    if wanted:
        return [u for u in cfg["upstreams"] if u["host"] in wanted]
    return cfg["upstreams"]


def get_nodes() -> dict:
    """状态监测：每节点 健康/模型/版本/用量。"""
    cfg = _gateway_cfg()
    nodes = []
    for u in cfg["upstreams"]:
        node = {"host": u["host"], "port": int(u.get("port", 8317))}
        try:
            from core.cpa_client import CPAClient

            ms = CPAClient(f"http://{node['host']}:{node['port']}/v1", cfg["key"], timeout=6).list_models()
            node.update({"ok": True, "models": [m.id for m in ms], "model_count": len(ms)})
        except Exception as e:  # noqa: BLE001
            node.update({"ok": False, "error": str(e)[:120], "models": []})
        st, v = _mgmt(node, "server/latest-version")
        node["version"] = v if st == 200 else None
        st, usage = _mgmt(node, "observability/usage/api-keys")
        node["usage"] = usage if st == 200 else None
        nodes.append(node)
    return {"ok": True, "nodes": nodes}


def node_config(payload: dict) -> dict:
    """拉取节点完整 config（管理 API GET /config）。"""
    nodes = _nodes_from_req(payload)
    if not nodes:
        return {"ok": False, "error": "无匹配节点"}
    out = {}
    for u in nodes:
        st, cfg = _mgmt(u, "config")
        out[u["host"]] = {"ok": st == 200, "config": cfg if st == 200 else None, "error": None if st == 200 else str(cfg)[:200]}
    return {"ok": True, "configs": out}


def node_diff(payload: dict) -> dict:
    """多节点 config 对比（providers/上游差异）。"""
    r = node_config(payload)
    if not r.get("ok"):
        return r
    configs = r["configs"]
    summary = {}
    for host, info in configs.items():
        cfg = info.get("config") or {}
        provs = {}
        for brand, items in (cfg.get("api-keys", {}) or {}).items():
            if isinstance(items, list):
                for p in items:
                    if isinstance(p, dict) and p.get("name"):
                        provs[p["name"]] = {
                            "brand": brand,
                            "base": p.get("base-url", ""),
                            "disabled": bool(p.get("disabled", False)),
                            "models": [m.get("name") for m in (p.get("models", []) or [])],
                        }
        summary[host] = {"ok": info["ok"], "providers": provs}
    # 差异
    hosts = list(summary)
    only = {}
    if len(hosts) >= 2:
        base = hosts[0]
        base_names = set(summary[base]["providers"])
        for h in hosts[1:]:
            names = set(summary[h]["providers"])
            only[h] = {"only": sorted(names - base_names), "missing": sorted(base_names - names)}
    return {"ok": True, "summary": summary, "only": only}


def node_sync(payload: dict) -> dict:
    """权威源同步：GET 源节点 config → PUT 目标节点（全量）。"""
    src_host = payload.get("source")
    dst_host = payload.get("target")
    cfg = _gateway_cfg()
    nodes = {u["host"]: u for u in cfg["upstreams"]}
    if src_host not in nodes or dst_host not in nodes:
        return {"ok": False, "error": f"源/目标不在 upstreams: {src_host}->{dst_host}"}
    st, src_cfg = _mgmt(nodes[src_host], "config")
    if st != 200:
        return {"ok": False, "error": f"读源 {src_host} 失败: {str(src_cfg)[:150]}"}
    st, resp = _mgmt(nodes[dst_host], "config", method="PUT", data=src_cfg)
    if st not in (200, 204):
        return {"ok": False, "error": f"写目标 {dst_host} 失败: {str(resp)[:150]}"}
    # 目标刷新模型
    _mgmt(nodes[dst_host], "routing/model-definitions/refresh", method="POST")
    return {"ok": True, "source": src_host, "target": dst_host, "status": st}


def node_call(payload: dict) -> dict:
    """嗅探/探测：api-call 测试上游（或直连 /v1/models 探活）。"""
    cfg = _gateway_cfg()
    nodes = {u["host"]: u for u in cfg["upstreams"]}
    host = payload.get("host")
    if host not in nodes:
        return {"ok": False, "error": "host 不在 upstreams"}
    node = nodes[host]
    if payload.get("mode") == "ping":
        # 直连 OpenAI /v1/models 探活
        try:
            from core.cpa_client import CPAClient

            ms = CPAClient(f"http://{host}:{int(node.get('port',8317))}/v1", cfg["key"], timeout=6).list_models()
            return {"ok": True, "mode": "ping", "host": host, "models": [m.id for m in ms]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "mode": "ping", "host": host, "error": str(e)[:150]}
    # 管理面 api-call（透传，测上游 provider）
    body = payload.get("body") or {}
    st, resp = _mgmt(node, "requests/api-call", method="POST", data=body)
    return {"ok": st in (200, 201), "host": host, "status": st, "resp": resp}


def node_refresh(payload: dict) -> dict:
    """刷新节点模型定义（多端模型一致性）。"""
    cfg = _gateway_cfg()
    nodes = {u["host"]: u for u in cfg["upstreams"]}
    done = []
    for host in payload.get("hosts") or list(nodes):
        if host in nodes:
            st, resp = _mgmt(nodes[host], "routing/model-definitions/refresh", method="POST")
            if st == 404:  # 旧版端点回退
                st, resp = _mgmt(nodes[host], "models/refresh", method="POST")
            done.append({"host": host, "status": st, "resp": resp if st != 200 else "ok"})
    return {"ok": True, "results": done}


def node_credentials(payload: dict) -> dict:
    """凭据列表（credentials）。"""
    out = {}
    for u in _nodes_from_req(payload):
        st, data = _mgmt(u, "credentials")
        out[u["host"]] = {"ok": st == 200, "status": st, "data": data if st == 200 else None}
    return {"ok": True, "nodes": out}


def node_plugins(payload: dict) -> dict:
    """插件列表（plugins）。"""
    out = {}
    for u in _nodes_from_req(payload):
        st, data = _mgmt(u, "plugins")
        out[u["host"]] = {"ok": st == 200, "status": st, "data": data if st == 200 else None}
    return {"ok": True, "nodes": out}


def node_logs(payload: dict) -> dict:
    """日志（observability/logs，需开 logging-to-file）。"""
    out = {}
    for u in _nodes_from_req(payload):
        st, data = _mgmt(u, "observability/logs")
        out[u["host"]] = {"ok": st == 200, "status": st, "data": data if st == 200 else None, "error": None if st == 200 else str(data)[:160]}
    return {"ok": True, "nodes": out}


def node_oauth(payload: dict) -> dict:
    """OAuth 状态（oauth/status）。"""
    out = {}
    for u in _nodes_from_req(payload):
        st, data = _mgmt(u, "oauth/status")
        out[u["host"]] = {"ok": st == 200, "status": st, "data": data if st == 200 else None}
    return {"ok": True, "nodes": out}


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
            # 单进程面板：静态页由 Python 自己发（无前后端分离、无 nginx）
            try:
                return self._send(200, INDEX_PATH.read_bytes(), "text/html; charset=utf-8")
            except OSError as e:  # noqa: BLE001
                return self._json({"ok": False, "error": str(e)}, 500)
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
        if p == "/api/nodes":
            return self._json(get_nodes())
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

        if p == "/api/nodes/config":
            return self._json(node_config(payload))
        if p == "/api/nodes/diff":
            return self._json(node_diff(payload))
        if p == "/api/nodes/sync":
            return self._json(node_sync(payload))
        if p == "/api/nodes/call":
            return self._json(node_call(payload))
        if p == "/api/nodes/refresh":
            return self._json(node_refresh(payload))
        if p == "/api/nodes/credentials":
            return self._json(node_credentials(payload))
        if p == "/api/nodes/plugins":
            return self._json(node_plugins(payload))
        if p == "/api/nodes/logs":
            return self._json(node_logs(payload))
        if p == "/api/nodes/oauth":
            return self._json(node_oauth(payload))
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
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=17834)
    a = p.parse_args()
    print(f"cpa-sync Web 控制面板 → http://{a.host}:{a.port}")
    ThreadingHTTPServer((a.host, a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
