"""CPA (cli-proxy-api) 客户端。

从 CPA 的 /v1/models 拉取实际暴露的模型列表，作为"能力校验"。
同时提供健康检查 / 模型存在性校验。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from .ir import ModelIR, ProviderIR


class CPAError(Exception):
    """CPA 请求/解析失败。"""


class CPAClient:
    """OpenAI 兼容的 CPA 网关客户端。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 8.0):
        # 去掉尾部斜杠，保证拼接安全
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        # OpenAI 兼容：base_url 可能带 /v1 或不带，统一规整出根地址
        if self.base_url.endswith("/v1"):
            self._root = self.base_url[:-3]
        else:
            self._root = self.base_url

    def _endpoint(self, path: str) -> str:
        """构造完整端点 URL。base_url 已含 /v1 时 path 用 /models。"""
        if self.base_url.endswith("/v1"):
            return f"{self.base_url}{path}"
        return f"{self.base_url}/v1{path}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }

    def _get(self, path: str) -> Any:
        url = self._endpoint(path)
        req = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise CPAError(
                f"GET {url} 失败: HTTP {e.code} {detail}"
            ) from e
        except urllib.error.URLError as e:
            raise CPAError(f"GET {url} 失败: {e.reason}") from e
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise CPAError(f"GET {url} 返回非 JSON: {body[:200]}") from e

    def list_models(self) -> list[ModelIR]:
        """拉取 CPA 实际暴露的模型列表（/v1/models）。"""
        payload = self._get("/models")
        data = payload.get("data", []) if isinstance(payload, dict) else []
        models: list[ModelIR] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            mid = item.get("id")
            if not mid:
                continue
            models.append(
                ModelIR(
                    id=mid,
                    # /v1/models 通常不带 contextWindow，保留 extra 备用
                    extra={k: v for k, v in item.items() if k not in ("id", "object")},
                )
            )
        return models

    def provider_ir(self) -> ProviderIR:
        """把 CPA 能力转成 ProviderIR（模型清单 + 连接信息）。"""
        return ProviderIR(
            base_url=self.base_url,
            api_key=self.api_key,
            models=self.list_models(),
        )

    def ping(self) -> bool:
        """健康检查：/v1/models 能否正常返回。"""
        try:
            self.list_models()
            return True
        except CPAError:
            return False
