#!/usr/bin/env python3
"""效仿用户砍掉 picoclaw 自带模型的动作：从 config.json 的 model_list 删掉内置 provider 条目
（picoclaw 没有 omp 那种 disabledProviders 数组——model_list 就是 provider 模型注册表，
删掉条目即不再加载），只保留自定义网关（默认 cpa）+ 可选保留项。

机制（研究结论）:
  - picoclaw 的 provider/model 配置全在 config.json 的 `model_list`（数组）。
  - 条目结构: {model_name, provider, model, api_base, api_keys?, auth_method?, extra_body?}
  - 内置 provider（openai/anthropic/zhipu/deepseek/... 30+ 个）的模型写在这里，
    api_base 指向官方（api.openai.com 等）或 localhost（ollama/vllm/lmstudio）。
  - 自定义网关（cpa）也是 model_list 条目（provider=cpa），是用户接入的 CPA 服务。
  - 屏蔽自带 = 删掉 provider 属于"内置"的条目（保留 cpa + 用户自定义非内置）。
  - 内置全集可从 picoclaw 二进制 strings / README Providers 表提取，但脚本更稳的判据:
    保留集（默认 cpa）之外的 provider 都视为内置自带，删掉（可 --keep 额外保留）。

用法:
  python3 scripts/picoclaw_disable_builtin.py                                # meilan-phone1 /root/.picoclaw/config.json
  python3 scripts/picoclaw_disable_builtin.py --config /root/.picoclaw/config.json
  python3 scripts/picoclaw_disable_builtin.py --keep cpa,myprovider          # 保留 cpa + myprovider
  python3 scripts/picoclaw_disable_builtin.py --dry-run                      # 只打印不改
  python3 scripts/picoclaw_disable_builtin.py --restart-picoclaw             # 改完重启 picoclaw-gateway

只操作 model_list；不动其它字段（agents/gateway/channel_list 等）。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

DEFAULT_CONFIG = "/root/.picoclaw/config.json"
DEFAULT_KEEP = {"cpa"}  # cpa = 用户接入的 CPA 网关（唯一自定义 provider）


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f) or {}
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="从 picoclaw config.json 的 model_list 删掉内置 provider 条目")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="picoclaw config.json 路径")
    ap.add_argument("--keep", default=", ".join(sorted(DEFAULT_KEEP)),
                    help="保留的 provider id（逗号分隔；默认只保留 cpa）")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异不写文件")
    ap.add_argument("--no-backup", action="store_true", help="写前不备份")
    ap.add_argument("--restart-picoclaw", action="store_true",
                    help="改完重启 picoclaw-gateway（supervisorctl restart）")
    args = ap.parse_args()

    path = Path(args.config)
    if not path.is_file():
        print(f"[!] config.json 不存在: {path}", file=sys.stderr)
        return 2
    keep = set(x.strip() for x in args.keep.split(",") if x.strip())

    data = load_config(path)
    model_list = data.get("model_list", []) or []
    if not isinstance(model_list, list):
        print(f"[!] model_list 不是数组: {type(model_list)}", file=sys.stderr)
        return 2

    kept, removed = [], []
    for m in model_list:
        if not isinstance(m, dict):
            kept.append(m)  # 非 dict 条目保留（不动）
            continue
        prov = str(m.get("provider", ""))
        if prov in keep:
            kept.append(m)
        else:
            removed.append(m)

    print(f"[*] model_list: {len(model_list)} -> {len(kept)} (删 {len(removed)} 条自带)")
    if removed:
        print("[+] 将删除（自带 provider）:")
        for m in removed[:30]:
            print(f"    - {m.get('provider')}/{m.get('model')}")
        if len(removed) > 30:
            print(f"    ... 还有 {len(removed)-30} 条")
    print(f"[*] 保留（{len(kept)} 条）: {sorted({str(m.get('provider')) for m in kept if isinstance(m, dict)})}")

    if args.dry_run:
        print("[dry-run] 不写文件")
        return 0

    # 备份 + 原子写
    if not args.no_backup:
        bak = path.with_name(path.name + f".bak.{os.environ.get('USER', 'picoclaw')}")
        shutil.copy2(path, bak)
        print(f"[*] 备份: {bak}")

    data["model_list"] = kept
    tmp_fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    print(f"[✓] 已写入: {path} (model_list={len(kept)})")

    if args.restart_picoclaw:
        print("[*] 重启 picoclaw-gateway ...")
        r = subprocess.run(["supervisorctl", "restart", "picoclaw-gateway"],
                           capture_output=True, text=True, timeout=60)
        print((r.stdout or r.stderr).strip())
        if r.returncode != 0:
            print("[!] 重启失败（可能需要手动 supervisorctl restart picoclaw-gateway）", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
