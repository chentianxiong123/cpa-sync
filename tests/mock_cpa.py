#!/usr/bin/env python3
"""mock CPA 服务器：返回固定模型集，供行为一致性对比/联调。

用法:
    python tests/mock_cpa.py [--port 18317]

模型集与真实 213 节点对齐（6 个典型模型，含多模态/推理模型），
供 Rust 版与 Python 版在完全相同的 CPA 响应下做逐字段对比。
"""
import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# 固定模型集：id / context_length（CPA /v1/models 无能力字段，识图靠中央配置声明）
MODELS = [
    {"id": "sensenova-6.8-flash-lite", "context_length": 262144},
    {"id": "sensenova-6.8-flash", "context_length": 262144},
    {"id": "agnes-2.0-flash", "context_length": 131072},
    {"id": "hybgzs/gpt-5.6", "context_length": 400000},
    {"id": "gemini-1/claude-opus-4-8", "context_length": 200000},
    {"id": "elysiver/qwen-3.6-flash", "context_length": 128000},
]


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._json({"object": "list", "data": MODELS})
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # 静默
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=18317)
    a = ap.parse_args()
    print(f"mock CPA → http://127.0.0.1:{a.port}/v1/models （{len(MODELS)} 个模型）", flush=True)
    ThreadingHTTPServer(("127.0.0.1", a.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
