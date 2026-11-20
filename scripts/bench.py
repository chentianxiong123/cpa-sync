#!/usr/bin/env python3
"""性能基准：Rust 版 vs Python 版，同机对比。

用途：换机器 / 升级依赖 / 改 profile 后跑一次，看性能有没有回退。

用法:
    python scripts/bench.py                     # 全自动探测
    python scripts/bench.py --profile debug     # 测 debug 构建
    python scripts/bench.py --rust ./cpa-sync.exe --py-src /path/to/py-src
    python scripts/bench.py --config config/cpa-config.yaml --cli-rounds 5

路径全部可传参，不写死任何个人目录。探测顺序：命令行 > 环境变量 > 自动推导。
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import socket
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # 项目根

# ---------------- 内存采样（跨平台） ----------------

_RSS_CANDIDATES = None


def _rss_via_proc(pid: int) -> int | None:
    """Linux: /proc/<pid>/status 的 VmRSS（KB）。"""
    try:
        for line in (Path("/proc") / str(pid) / "status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    except Exception:
        return None
    return None


def _rss_via_powershell(pid: int) -> int | None:
    """Windows: Get-Process.WorkingSet64（对 64 位进程可靠）。

    注意：不能用 32 位 GetProcessMemoryInfo API，对 64 位进程返回错误值。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             f"(Get-Process -Id {pid}).WorkingSet64"],
            capture_output=True, text=True, timeout=20,
            creationflags=0x08000000 if os.name == "nt" else 0).stdout.strip().splitlines()
        return int(out[-1]) // 1024
    except Exception:
        return None


def rss_kb(pid: int) -> int | None:
    if os.name == "nt":
        return _rss_via_powershell(pid)
    return _rss_via_proc(pid) or _rss_via_powershell(pid)


# ---------------- 网络 / HTTP ----------------

def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def wait_port(port: int, budget: float = 90) -> float | None:
    """等到端口可用，返回耗时 ms。"""
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < budget:
        if port_open(port):
            return (time.perf_counter() - t0) * 1000
        time.sleep(0.02)
    return None


def http_get(url: str, timeout: float = 5) -> tuple[int | None, int | None, float]:
    import urllib.request

    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            n = len(r.read())
            return r.status, n, (time.perf_counter() - t0) * 1000
    except Exception:
        return None, None, (time.perf_counter() - t0) * 1000


def latency_profile(port: int, url_path: str = "/api/version", n: int = 40) -> dict | None:
    lat: list[float] = []
    for _ in range(n):
        st, _, ms = http_get(f"http://127.0.0.1:{port}{url_path}")
        if st == 200:
            lat.append(ms)
    if not lat:
        return None
    lat.sort()
    return {"median_ms": round(statistics.median(lat), 2),
            "p95_ms": round(lat[int(len(lat) * 0.95)], 2),
            "max_ms": round(max(lat), 2), "ok": len(lat)}


# ---------------- 两项基准 ----------------

def bench_web(exe: Path, extra_args: list[str], port: int, name: str) -> dict:
    """起服务：测冷启动 + 空闲/忙时工作集 + 请求延迟。"""
    p = subprocess.Popen([str(exe), *extra_args],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    ms = wait_port(port)
    time.sleep(1.0)
    idle = rss_kb(p.pid)
    lat: list[float] = []
    for _ in range(40):
        st, _, took = http_get(f"http://127.0.0.1:{port}/api/version")
        if st == 200:
            lat.append(took)
    time.sleep(1.5)
    hot = rss_kb(p.pid)
    p.terminate()
    try:
        p.wait(timeout=10)
    except Exception:
        p.kill()
    prof = _summarize(lat) if lat else None
    return {"name": name, "start_ms": round(ms, 1) if ms else None,
            "rss_idle_kb": idle, "rss_hot_kb": hot, "latency": prof}


def _summarize(lat: list[float]) -> dict:
    """把一次延迟采样集合成 中位/P95/max。"""
    s = sorted(lat)
    return {"median_ms": round(statistics.median(lat), 2),
            "p95_ms": round(s[int(len(s) * 0.95)], 2),
            "max_ms": round(max(lat), 2), "n": len(lat)}


def bench_cli(exe: Path, extra_args: list[str], config: Path, rounds: int) -> dict:
    """CLI check 耗时（含解释器/依赖加载，这正是两版的差距来源）。"""
    vals: list[float] = []
    for _ in range(rounds):
        t0 = time.perf_counter()
        subprocess.run([str(exe), *extra_args, "check", "--config", str(config)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        vals.append(round((time.perf_counter() - t0) * 1000, 1))
    return {"median_ms": round(statistics.median(vals), 1), "all": vals}


def bench_python_import_cost() -> dict | None:
    """Python 内存构成：解释器基线 / +第三方依赖 / +本项目模块。"""
    if os.name != "nt" and platform.system() != "Linux":
        return None
    probe = r'''
import os, subprocess, sys
def kb():
    pid = os.getpid()
    if os.name == "nt":
        out = subprocess.run(["powershell","-NoProfile","-Command",
            f"(Get-Process -Id {pid}).WorkingSet64"],
            capture_output=True, text=True, timeout=20).stdout.strip().splitlines()
        return int(out[-1])//1024
    for line in open(f"/proc/{pid}/status"):
        if line.startswith("VmRSS:"): return int(line.split()[1])
base = kb()
stage = sys.argv[1]
if stage in ("deps","app"):
    import yaml, http.server
elif stage == "app":
    sys.path.insert(0, sys.argv[2]); import adapters, core.config
print(f"{stage} {kb()} {kb()-base}")
'''
    out: dict[str, int | None] = {}
    prev = 0
    for stage, arg in (("base", None), ("deps", "deps"), ("app", f"app {ROOT}")):
        r = subprocess.run([sys.executable, "-c", probe, stage or "base"],
                          capture_output=True, text=True, timeout=60)
        parts = r.stdout.strip().splitlines()[-1].split() if r.stdout.strip() else []
        out[stage] = int(parts[1]) if len(parts) >= 2 else None
        if len(parts) >= 3:
            prev = int(parts[2])
    return out


# ---------------- 探测 ----------------

def find_rust_exe(profile: str) -> Path | None:
    """探测 cargo 产物：CARGO_MANIFEST_DIR > 项目根/target。"""
    suffix = Path(".exe") if os.name == "nt" else Path("")
    dirs = []
    env = os.environ.get("CARGO_MANIFEST_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(ROOT)
    for d in dirs:
        for p in (d / "target" / profile / f"cpa-sync{suffix}",
                  d / "target" / profile / "cpa-sync"):
            if p.exists():
                return p
    return None


def find_python_web(py_src: Path | None) -> tuple[Path, Path] | None:
    """找 (python 解释器, web/app.py)。"""
    src = py_src or ROOT
    app = src / "web" / "app.py"
    if not app.exists():
        return None
    return sys.executable, app


# ---------------- 主流程 ----------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--profile", choices=["debug", "release"], default="release")
    ap.add_argument("--rust", help="Rust 可执行文件路径")
    ap.add_argument("--python", help="Python 解释器（默认 sys.executable）")
    ap.add_argument("--py-src", help="Python 版源码目录（默认项目根）")
    ap.add_argument("--config", help="同步配置 yaml")
    ap.add_argument("--port-rust", type=int, default=17834)
    ap.add_argument("--port-python", type=int, default=17841)
    ap.add_argument("--cli-rounds", type=int, default=5)
    ap.add_argument("--no-web", action="store_true", help="只测 CLI，不起 web 服务")
    args = ap.parse_args()

    rust = Path(args.rust) if args.rust else find_rust_exe(args.profile)
    py_src = Path(args.py_src) if args.py_src else ROOT
    config = Path(args.config) if args.config else next(
        (p for p in (ROOT / "config" / "cpa-config.yaml",
                     Path.home() / "cpa-sync" / "config" / "cpa-config.yaml") if p.exists()),
        Path("cpa-config.yaml"))
    py = find_python_web(py_src)

    print(f"平台        : {platform.system()} {platform.release()}")
    print(f"Rust 二进制 : {rust or '未找到'}  (profile={args.profile})")
    print(f"Python      : {sys.executable}  {sys.version.split()[0]}")
    print(f"Python 源码 : {py_src}  web/app.py={'有' if py else '无'}")
    print(f"配置        : {config}")
    print()

    result: dict = {}

    if not args.no_web and rust and py:
        print("=== Web 服务：冷启动 + 内存 ===")
        rust_web = bench_web(rust, [], args.port_rust, "rust")
        result["web_rust"] = rust_web
        py_bin, py_app = py
        py_web = bench_web(Path(py_bin),
                           [str(py_app), "--host", "127.0.0.1", "--port", str(args.port_python)],
                           args.port_python, "python")
        result["web_python"] = py_web
        for r in (rust_web, py_web):
            print(f"  {r['name']:8s} 启动={r['start_ms']}ms  "
                  f"空闲RSS={r['rss_idle_kb']}KB  忙时RSS={r['rss_hot_kb']}KB")
            if r["latency"]:
                L = r["latency"]
                print(f"  {'':8s} 请求中位={L['median_ms']}ms  P95={L['p95_ms']}ms  "
                      f"max={L['max_ms']}ms  (n={L['n']})")

    print("\n=== CLI check 耗时（同一 config）===")
    result["cli_rust"] = bench_cli(rust, [], config, args.cli_rounds) if rust else None
    result["cli_python"] = (bench_cli(Path(sys.executable), ["cpa-sync", "--config", str(config)],
                                      config, args.cli_rounds)
                            if py else None)
    for k in ("cli_rust", "cli_python"):
        v = result[k]
        if v:
            print(f"  {k:11s} 中位={v['median_ms']}ms  各轮={v['all']}")

    if py:
        print("\n=== Python 内存构成 ===")
        m = bench_python_import_cost()
        if m:
            result["py_mem_stage"] = m
            print(f"  纯解释器 {m.get('base')}KB -> +依赖 {m.get('deps')}KB -> +本项目 {m.get('app')}KB")

    out = ROOT / "target" / "bench-result.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n完整数据 -> {out}")


if __name__ == "__main__":
    main()
