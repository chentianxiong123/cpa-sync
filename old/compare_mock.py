#!/usr/bin/env python3
"""行为一致性对比：Python 版 vs Rust 版。

同一 mock CPA + 同一份配置，两版各自 apply 到隔离目录，
逐字段拍平 diff 全部产物（8 个文件 + sqlite 12 行），验证语义等价。

用法（路径全部参数化，不写死个人目录）:
    python tests/compare_mock.py \
        --rust-exe ./target/release/cpa-sync \
        --py-src . --workdir /tmp/cmp-mock [--mock-port 18317] [--keep]

流程:
    1. 起 mock CPA（或复用 --mock-port 上已运行的）
    2. 生成 py.yaml / rs.yaml（路径指向 workdir/py 与 workdir/rs）
    3. 双跑 apply：Python [python cpa-sync apply -y] / Rust [exe apply]
    4. 8 个文件逐字段 diff + claude/codex sqlite 12 行 diff
    5. 汇总 一致/差异

退出码：0=全部一致，1=有差异，2=执行失败。
"""
import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

# 8 个文件型产物：文件名 -> 解析方式（json/yaml/text）
FILES = [
    ("opencode.json", "json"),
    ("models.json", "json"),
    ("openclaw.json", "json"),
    ("picoclaw.json", "json"),
    ("cordis.patch.yml", "yaml"),
    ("hermes.yaml", "yaml"),
    ("agents.yaml", "yaml"),
    ("crushrc", "text"),
]


def flatten(v, prefix="", out=None):
    out = out if out is not None else {}
    if isinstance(v, dict):
        if not v:
            out[prefix] = "{}"
        for k, x in v.items():
            flatten(x, f"{prefix}.{k}", out)
    elif isinstance(v, list):
        if not v:
            out[prefix] = "[]"
        for i, x in enumerate(v):
            flatten(x, f"{prefix}[{i}]", out)
    elif v is None:
        out[prefix] = "<null>"
    else:
        out[prefix] = repr(v)
    return out


def load(path: Path, kind: str):
    txt = path.read_text(encoding="utf-8")
    if kind == "text":
        return [l.strip() for l in txt.splitlines() if l.strip()]
    if kind == "json":
        return json.loads(txt) if txt.strip() else None
    import yaml
    return yaml.safe_load(txt) if txt.strip() else None


def diff_files(py_dir: Path, rs_dir: Path) -> tuple[int, list[str]]:
    diffs = []
    for name, kind in FILES:
        p, r = py_dir / name, rs_dir / name
        if not p.exists() and not r.exists():
            continue
        a = load(p, kind) if p.exists() else None
        b = load(r, kind) if r.exists() else None
        if kind == "text":
            lines_a, lines_b = a or [], b or []
            bad = len(lines_a) != len(lines_b) or any(
                x != y for x, y in zip(lines_a, lines_b))
            if bad:
                diffs.append(f"[{name}] 文本不一致 py={len(lines_a)}行 rs={len(lines_b)}行")
            continue
        fa, fb = flatten(a), flatten(b)
        for k in sorted(set(fa) | set(fb)):
            va, vb = fa.get(k, "<缺>"), fb.get(k, "<缺>")
            if va != vb:
                diffs.append(f"[{name}] {k}: py={va} | rs={vb}")
    return len(diffs), diffs


def diff_sqlite(py_db: Path, rs_db: Path) -> tuple[int, list[str]]:
    def rows(p):
        if not p.exists():
            return []
        c = sqlite3.connect(str(p))
        r = c.execute("""SELECT app_type, name, settings_config, notes, meta, is_current
                          FROM providers ORDER BY app_type, name""").fetchall()
        c.close()
        return [(a, n, s, notes or "", meta or "", int(cur or 0))
                for a, n, s, notes, meta, cur in r]

    A, B = rows(py_db), rows(rs_db)
    diffs = []
    if len(A) != len(B):
        diffs.append(f"[sqlite] 行数 py={len(A)} rs={len(B)}")
        return len(diffs), diffs
    for a, b in zip(A, B):
        if a[1] != b[1] or a[3] != b[3] or a[5] != b[5]:
            diffs.append(f"[sqlite/{a[1]}] 标量不一致")
        for tag, x, y in (("settings", a[2], b[2]), ("meta", a[4], b[4])):
            fx, fy = flatten(json.loads(x) if x else {}), flatten(json.loads(y) if y else {})
            for k in sorted(set(fx) | set(fy)):
                if fx.get(k, "<缺>") != fy.get(k, "<缺>"):
                    diffs.append(f"[sqlite/{a[1]}] {tag}{k}: py={fx.get(k,'<缺>')} | rs={fy.get(k,'<缺>')}")
    return len(diffs), diffs


def make_config(workdir: Path, side: str, port: int) -> Path:
    """生成一份配置，adapter 路径指向 workdir/<side>/。"""
    d = workdir / side
    d.mkdir(parents=True, exist_ok=True)
    # 预创建空文件（首次同步读不到文件会跳过，先占位）
    for name, _ in FILES:
        (d / name).touch(exist_ok=True)
    q = lambda p: json.dumps(str(p))  # Windows 路径安全转义
    mapping = {
        "opencode": "opencode.json", "pi": "models.json", "openclaw": "openclaw.json",
        "picoclaw": "picoclaw.json", "dsh": "cordis.patch.yml", "hermes": "hermes.yaml",
        "omp": "agents.yaml", "crush": "crushrc",
    }
    cfg = f"""cpa:
  base_url: http://127.0.0.1:{port}/v1
  api_key: "123456"
  provider_id: cpa
  context_window: 128000
  max_tokens: 8192
  default_input_modes: [text]
  image_models: ["sensenova*", "*qwen*"]
  model_overrides:
    sensenova-6.8-flash-lite:
      contextWindow: 262144
      maxTokens: 32768
      reasoning: true
    hybgzs/gpt-5.6:
      reasoning: true
      input: [text, image]
    agnes-2.0-flash:
      contextWindow: 131072
targets: [opencode, pi, openclaw, picoclaw, dsh, hermes, omp, crush]
adapter_instances:
"""
    for agent, fname in mapping.items():
        cfg += f"  {agent}: [{{name: cmp, path: {q(d / fname)}, platform: \"\"}}]\n"
    out = workdir / f"{side}.yaml"
    out.write_text(cfg, encoding="utf-8")
    return out


def run_apply(cmd: list, cwd: str | None = None, timeout: int = 180) -> tuple[int, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return -1, f"执行失败: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rust-exe", required=True)
    ap.add_argument("--py-src", default=str(ROOT))
    ap.add_argument("--workdir", default=str(tempfile.mkdtemp(prefix="cpa-cmp-")))
    ap.add_argument("--mock-port", type=int, default=18317)
    ap.add_argument("--keep", action="store_true", help="保留 workdir")
    ap.add_argument("--python", default=sys.executable)
    a = ap.parse_args()

    rust = Path(a.rust_exe)
    py_src = Path(a.py_src)
    workdir = Path(a.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if not rust.exists():
        print(f"✗ rust exe 不存在: {rust}")
        return 2

    # 1) 起 mock CPA（若端口已被占用则复用）
    import importlib.util
    spec = importlib.util.spec_from_file_location("mock_cpa", HERE / "mock_cpa.py")
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    started = False
    try:
        import socket
        s = socket.socket(); s.settimeout(0.5)
        s.connect(("127.0.0.1", a.mock_port)); s.close()
    except Exception:
        t = threading.Thread(target=mock.main, kwargs={"port": a.mock_port}, daemon=True)
        t.start()
        started = True
        time.sleep(1.2)
    print(f"[mock] CPA {'已运行' if not started else '已启动'} @127.0.0.1:{a.mock_port}")

    # 2) 生成配置 + 双跑
    py_cfg = make_config(workdir, "py", a.mock_port)
    rs_cfg = make_config(workdir, "rs", a.mock_port)
    print(f"[run] Python apply -> {workdir}/py/")
    c1, o1 = run_apply([str(a.python), "cpa-sync", "--config", str(py_cfg), "apply", "-y"], cwd=str(py_src))
    print(f"[run] Rust apply   -> {workdir}/rs/")
    c2, o2 = run_apply([str(rust), "apply", "--config", str(rs_cfg)])
    if c1 != 0 or c2 != 0:
        print(f"✗ apply 失败 py={c1} rs={c2}\n--- py out ---\n{o1[-800:]}\n--- rs out ---\n{o2[-800:]}")
        return 2

    # 3) 文件 diff
    n1, d1 = diff_files(workdir / "py", workdir / "rs")
    # 4) sqlite diff（claude/codex 写各自的库）
    py_db, rs_db = workdir / "py-db.db", workdir / "rs-db.db"
    for db in (py_db, rs_db):
        if db.exists():
            db.unlink()
    import sqlite3 as _sq
    for db in (py_db, rs_db):
        c = _sq.connect(str(db))
        c.execute("""CREATE TABLE providers (id TEXT PRIMARY KEY, app_type TEXT, name TEXT,
            settings_config TEXT, notes TEXT, meta TEXT, is_current INTEGER DEFAULT 0,
            sort_index INTEGER DEFAULT 0, created_at INTEGER)""")
        c.commit(); c.close()
    env_py = dict(os.environ, CC_SWITCH_DB=str(py_db))
    env_rs = dict(os.environ, CC_SWITCH_DB=str(rs_db))
    subprocess.run([str(a.python), "cpa-sync", "--config", str(py_cfg), "apply", "-y", "claude", "codex"],
                   cwd=str(py_src), env=env_py, capture_output=True, timeout=180)
    subprocess.run([str(rust), "apply", "claude", "codex", "--config", str(rs_cfg)],
                   env=env_rs, capture_output=True, timeout=180)
    n2, d2 = diff_sqlite(py_db, rs_db)

    total = n1 + n2
    print(f"\n=== 结果 ===")
    print(f"文件产物: {'一致 ✓' if n1 == 0 else f'{n1} 处差异'}")
    print(f"sqlite  : {'一致 ✓' if n2 == 0 else f'{n2} 处差异'}")
    for d in d1[:10] + d2[:10]:
        print("  " + d[:200])
    print(f"\n汇总: {'全部一致 ✓' if total == 0 else f'{total} 处差异 ✗'}")
    print(f"产物目录: {workdir}")
    if not a.keep:
        pass  # 保留产物便于排查，--keep 只是不清理临时目录（默认 workdir 是临时目录）
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
