#!/usr/bin/env python3
"""渲染 nginx 网关配置：读 config/gateway.yaml → 套 web/nginx.conf 模板 → 输出。

upstreams 数量可变（1~N 个 CPA），因此这是"可选模式"的单一渲染入口：
  1 个上游 = 纯转发；2+ 个 = 聚合 + 故障切换。

用法:
    python3 scripts/render_gateway.py              # 打印到 stdout
    python3 scripts/render_gateway.py --output /etc/nginx/conf.d/cpa-sync.conf
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def render(cfg: dict, template: str) -> str:
    gw = cfg.get("gateway", {})
    listen = gw.get("listen", 8380)
    timeout = gw.get("proxy_read_timeout", 120)
    ups = gw.get("upstreams", [])
    if not ups:
        raise SystemExit("gateway.yaml: gateway.upstreams 不能为空")
    lines = "\n".join(
        f"    server {u['host']}:{int(u.get('port', 8317))};" for u in ups
    )
    # 面板反代（panel_proxy_port>0 才启用）
    panel_port = int(gw.get("panel_port", 17834))
    panel_proxy = int(gw.get("panel_proxy_port", 0))
    if panel_proxy:
        panel_block = f"""server {{
    listen {panel_proxy};
    server_name _;
    location / {{
        proxy_pass http://127.0.0.1:{panel_port};
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_connect_timeout 5s;
        proxy_read_timeout 120s;
    }}
}}"""
    else:
        panel_block = "# (未启用面板反代 panel_proxy_port=0)"
    return (
        template.replace("__LISTEN__", str(listen))
        .replace("__UPSTREAMS__", lines)
        .replace("__READ_TIMEOUT__", str(timeout))
        .replace("__PANEL_BLOCK__", panel_block)
    )


def main() -> int:
    p = argparse.ArgumentParser(description="渲染 cpa-sync 网关 nginx 配置")
    p.add_argument("--config", default=str(ROOT / "config" / "gateway.yaml"))
    p.add_argument("--template", default=str(ROOT / "web" / "nginx.conf"))
    p.add_argument("--output", help="输出文件路径；缺省打印到 stdout")
    a = p.parse_args()

    try:
        import yaml
    except ImportError:
        print("✗ 需要 pyyaml（.venv/bin/pip install PyYAML）", file=sys.stderr)
        return 1

    cfg = yaml.safe_load(Path(a.config).read_text(encoding="utf-8"))
    out = render(cfg, Path(a.template).read_text(encoding="utf-8"))

    if a.output:
        Path(a.output).write_text(out, encoding="utf-8")
        print(f"✓ 渲染 → {a.output}")
    else:
        sys.stdout.write(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
