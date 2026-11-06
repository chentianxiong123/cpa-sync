#!/usr/bin/env python3
"""效仿用户扫掉 omp 自带 provider 的动作：把 omp 内置自带 provider 全写进
~/.omp/agent/config.yml 的 disabledProviders（只保留 web + 自定义 provider 如 cpa/sensenova）。

机制（研究结论）:
  - omp 内置自带 provider 全集 = @oh-my-pi/pi-coding-agent 包内
    node_modules/@oh-my-pi/pi-catalog/src/compat/rules.json 的 `providers` keys
    （编译自 rules/providers/*.kdl，含 85 个：chat/多模态 + web 搜索）。
  - 屏蔽方式 = config.yml 的 disabledProviders 数组（omp 启动时过滤，不加载这些 provider）。
  - 保留 = web（唯一 search provider，api=web-search）+ 自定义 provider（cpa/sensenova，不在 rules.json）。

用法:
  python3 scripts/omp_disable_providers.py                          # 本机 ~/.omp/agent/config.yml
  python3 scripts/omp_disable_providers.py --config /path/config.yml
  python3 scripts/omp_disable_providers.py --rules /path/rules.json  # 显式指定内置 provider 全集
  python3 scripts/omp_disable_providers.py --dry-run                 # 只打印不改
  python3 scripts/omp_disable_providers.py --keep web,sensenova,cpa  # 额外保留（默认保留 web）

只操作 disabledProviders；不动 providers/modelRoles 等其它字段。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

DEFAULT_CONFIG = "~/.omp/agent/config.yml"
# 保留集：web 是唯一 search provider（api=web-search），不该屏蔽。
# 自定义 provider（cpa/sensenova）不在 rules.json 里，天然不受影响，无需列这。
DEFAULT_KEEP = {"web"}


def load_rules(rules_path: Path) -> set[str]:
    """读 omp 内置 provider 全集（rules.json 的 providers keys）。"""
    data = json.loads(rules_path.read_text(encoding="utf-8"))
    providers = data.get("providers", {})
    if not isinstance(providers, dict) or not providers:
        raise ValueError(f"{rules_path} 里没有 providers 段（keys={list(data.keys())}）")
    return set(providers.keys())


def find_rules_json(config_path: Path) -> Path | None:
    """从 config.yml 反查 omp 包里的 rules.json（同一 ~/.omp 树 / npm 全局包）。"""
    candidates = [
        # 本机 ~/.omp 树（omp 装在 npm 全局时的相对位置不固定，先试常见）
        config_path.parent / ".." / ".." / "node_modules" / "@oh-my-pi" / "pi-coding-agent" /
        "node_modules" / "@oh-my-pi" / "pi-catalog" / "src" / "compat" / "rules.json",
        # npm 全局包（本机/win11）
        Path.home() / "AppData" / "Roaming" / "npm" / "node_modules" / "@oh-my-pi" /
        "pi-coding-agent" / "node_modules" / "@oh-my-pi" / "pi-catalog" / "src" / "compat" / "rules.json",
        Path.home() / ".npm" / "_npx" / "node_modules" / "@oh-my-pi" /
        "pi-coding-agent" / "node_modules" / "@oh-my-pi" / "pi-catalog" / "src" / "compat" / "rules.json",
    ]
    for c in candidates:
        if c.is_file():
            return c
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description="把 omp 内置自带 provider 全写进 config.yml 的 disabledProviders")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="omp config.yml 路径（默认 ~/.omp/agent/config.yml）")
    ap.add_argument("--rules", default=None, help="omp rules.json 路径（自动查找）")
    ap.add_argument("--keep", default=", ".join(sorted(DEFAULT_KEEP)),
                    help="额外保留的 provider id（逗号分隔；默认只保留 web）")
    ap.add_argument("--dry-run", action="store_true", help="只打印差异不写文件")
    ap.add_argument("--no-backup", action="store_true", help="写前不备份")
    args = ap.parse_args()

    config_path = Path(os.path.expanduser(args.config))
    if not config_path.is_file():
        print(f"[!] config.yml 不存在: {config_path}", file=sys.stderr)
        return 2

    keep = set(x.strip() for x in args.keep.split(",") if x.strip())

    # 内置 provider 全集
    rules_path = Path(args.rules) if args.rules else find_rules_json(config_path)
    if rules_path is None or not rules_path.is_file():
        print(f"[!] 找不到 rules.json（omp 内置 provider 全集）: {rules_path}", file=sys.stderr)
        print("    用 --rules 显式指定（win11: C:\\Users\\a1\\AppData\\Roaming\\npm\\node_modules\\"
              "@oh-my-pi\\pi-coding-agent\\node_modules\\@oh-my-pi\\pi-catalog\\src\\compat\\rules.json）",
              file=sys.stderr)
        return 2
    builtin = load_rules(rules_path)
    print(f"[*] 内置 provider 全集: {len(builtin)} 个 (来自 {rules_path})")

    # 要屏蔽的 = 内置全集 - 保留集
    to_disable = builtin - keep
    print(f"[*] 保留: {sorted(keep)}")
    print(f"[*] 本次将屏蔽: {len(to_disable)} 个内置 provider")

    # 读 config.yml
    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    existing = list(data.get("disabledProviders", []) or [])
    existing_set = set(existing)

    # 合并：保留用户已有条目（不删），新增内置屏蔽集
    merged = list(existing_set | to_disable)
    # 保持顺序：现有在前（用户顺序），新增按内置全集顺序
    ordered = list(dict.fromkeys(existing + sorted(to_disable)))
    added = sorted(to_disable - existing_set)
    print(f"[*] disabledProviders: {len(existing)} -> {len(ordered)} (+{len(added)})")
    if added:
        print(f"[+] 新增屏蔽: {added}")

    if args.dry_run:
        print("[dry-run] 不写文件")
        return 0

    # 写前备份
    if not args.no_backup:
        bak = config_path.with_name(config_path.name + f".bak.{os.environ.get('USER', 'omp')}")
        shutil.copy2(config_path, bak)
        print(f"[*] 备份: {bak}")

    # 原子写（保留 yaml 结构）
    data["disabledProviders"] = ordered
    tmp_fd, tmp_path = tempfile.mkstemp(dir=config_path.parent, suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, config_path)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
    print(f"[✓] 已写入: {config_path} (disabledProviders={len(ordered)})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
