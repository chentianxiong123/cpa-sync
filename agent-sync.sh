#!/usr/bin/env bash
# agent-sync 启动器（macOS / Linux）
# 用法: ./agent-sync.sh check | apply | list | doctor [agent...]
# 首次: python3 -m venv .venv && .venv/bin/pip install pyyaml
set -e
cd "$(dirname "$0")"
PY=".venv/bin/python"
if [ ! -x "$PY" ]; then
    PY=python3
fi
exec "$PY" "$(dirname "$0")/agent-sync" "$@"
