"""通用 HTTP 请求工具包（内部能力：默认不锁域名，可选收紧）。"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import httpx
import structlog

from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)

_DEFAULT_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"})


def host_matches_whitelist(hostname: str, patterns: list[str]) -> bool:
    """域名匹配：精确或 ``*.example.com``；空列表 / ``*`` 表示不限制。"""
    normalized = [str(p or "").strip().lower().rstrip(".") for p in patterns]
    normalized = [p for p in normalized if p]
    if not normalized or "*" in normalized:
        return True

    host = (hostname or "").strip().lower().rstrip(".")
    if not host:
        return False
    for pattern in normalized:
        if pattern.startswith("*."):
            suffix = pattern[1:]  # ".example.com"
            base = pattern[2:]  # "example.com"
            if host == base or host.endswith(suffix):
                return True
        elif host == pattern:
            return True
    return False


def _domains_open(patterns: list[str]) -> bool:
    normalized = [str(p or "").strip() for p in patterns]
    normalized = [p for p in normalized if p]
    return (not normalized) or ("*" in normalized)


class HttpCallTool(BaseTool):
    """发起 HTTP/HTTPS 请求（默认开放；配置 allowed_domains 后才收紧）。

    参数:
      - url (必填): 完整 http/https URL
      - method: 默认 GET
      - headers / query / json_body / body
    """

    name = "http_call"
    description = (
        "向 HTTP API 发送请求。"
        "参数: url(必填), method, headers, query, json_body, body。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "完整 http/https URL"},
                "method": {
                    "type": "string",
                    "description": "HTTP 方法，默认 GET",
                },
                "headers": {
                    "type": "object",
                    "description": "请求头",
                    "additionalProperties": {"type": "string"},
                },
                "query": {
                    "type": "object",
                    "description": "Query 参数",
                    "additionalProperties": True,
                },
                "json_body": {
                    "description": "JSON 请求体（与 body 互斥）",
                },
                "body": {
                    "type": "string",
                    "description": "原始请求体（与 json_body 互斥）",
                },
            },
            "required": ["url"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        *,
        allowed_domains: list[str] | None = None,
        timeout_seconds: float = 30.0,
        max_response_chars: int = 8000,
        allowed_methods: list[str] | None = None,
        default_headers: dict[str, str] | None = None,
        follow_redirects: bool = True,
    ) -> None:
        self._allowed_domains = [str(d).strip() for d in (allowed_domains or []) if str(d).strip()]
        self._timeout_seconds = float(timeout_seconds)
        self._max_response_chars = max(500, int(max_response_chars))
        if allowed_methods:
            self._allowed_methods = {
                str(m).strip().upper() for m in allowed_methods if str(m).strip()
            }
        else:
            self._allowed_methods = set(_DEFAULT_METHODS)
        self._default_headers = dict(default_headers or {})
        self._follow_redirects = bool(follow_redirects)

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        # Step 1: 解析 URL / method
        url = str(arguments.get("url") or "").strip()
        if not url:
            return "错误: 缺少参数 url"

        parsed = urlparse(url)
        scheme = (parsed.scheme or "").lower()
        if scheme not in {"http", "https"}:
            return f"错误: 仅支持 http/https，当前 scheme={scheme!r}"
        host = parsed.hostname or ""
        if not host_matches_whitelist(host, self._allowed_domains):
            return (
                f"错误: 域名未允许 host={host!r}，"
                f"allowed_domains={self._allowed_domains}"
            )

        method = str(arguments.get("method") or "GET").strip().upper() or "GET"
        if self._allowed_methods and method not in self._allowed_methods:
            return (
                f"错误: method={method!r} 不在 allowed_methods="
                f"{sorted(self._allowed_methods)}"
            )

        headers = self._merge_headers(arguments.get("headers"))
        if headers is None:
            return "错误: headers 必须是 JSON 对象"
        query = arguments.get("query")
        if query is not None and not isinstance(query, dict):
            return "错误: query 必须是 JSON 对象"

        json_body = arguments.get("json_body", arguments.get("json"))
        raw_body = arguments.get("body")
        if json_body is not None and raw_body is not None:
            return "错误: json_body 与 body 不能同时提供"
        if json_body is not None and not isinstance(json_body, (dict, list)):
            return "错误: json_body 必须是 JSON 对象或数组"

        # Step 2: 发起请求
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                follow_redirects=self._follow_redirects,
            ) as client:
                resp = await client.request(
                    method,
                    url,
                    headers=headers or None,
                    params=query,
                    json=json_body if json_body is not None else None,
                    content=None if json_body is not None else (
                        raw_body.encode("utf-8") if isinstance(raw_body, str) else raw_body
                    ),
                )
        except httpx.TimeoutException:
            return f"错误: HTTP 请求超时（{self._timeout_seconds}s）url={url}"
        except httpx.RequestError as exc:
            logger.warning("http_call_request_error", url=url, error=str(exc))
            return f"HTTP 请求失败: {exc}"

        # Step 3: 若配置了域名限制且跟随了重定向，再核一次最终 host
        if not _domains_open(self._allowed_domains):
            final_host = urlparse(str(resp.url)).hostname or ""
            if not host_matches_whitelist(final_host, self._allowed_domains):
                return (
                    f"错误: 最终响应域名未允许 host={final_host!r}，"
                    f"allowed_domains={self._allowed_domains}"
                )

        # Step 4: 格式化响应
        text = resp.text
        truncated = False
        if len(text) > self._max_response_chars:
            text = text[: self._max_response_chars] + "\n…(truncated)"
            truncated = True

        lines = [
            f"status={resp.status_code}",
            f"url={resp.url}",
            f"method={method}",
            f"channel={scope.channel or '-'}",
        ]
        content_type = resp.headers.get("content-type")
        if content_type:
            lines.append(f"content_type={content_type}")
        if truncated:
            lines.append(f"truncated=true max_chars={self._max_response_chars}")
        lines.append("")
        lines.append(text)
        return "\n".join(lines)

    def _merge_headers(self, raw: Any) -> dict[str, str] | None:
        headers = {str(k): str(v) for k, v in self._default_headers.items()}
        if raw is None:
            return headers
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                return None
        if not isinstance(raw, dict):
            return None
        for key, value in raw.items():
            headers[str(key)] = str(value)
        return headers


class HttpRequestToolPack(ToolPack):
    """通用 HTTP 请求工具包。"""

    pack_name = "http_request"
    description = "通用 HTTP 请求工具包"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        # 默认开放：未配 / 空 / ["*"] 均不限制域名（内部使用）
        domains = [str(d) for d in (config.get("allowed_domains") or [])]
        default_headers = config.get("default_headers") or {}
        if not isinstance(default_headers, dict):
            default_headers = {}
        tool = HttpCallTool(
            allowed_domains=domains,
            timeout_seconds=float(config.get("timeout_seconds") or 30),
            max_response_chars=int(config.get("max_response_chars") or 8000),
            allowed_methods=list(config.get("allowed_methods") or []) or None,
            default_headers={str(k): str(v) for k, v in default_headers.items()},
            follow_redirects=bool(config.get("follow_redirects", True)),
        )
        for decl in config.get("tools") or []:
            if isinstance(decl, dict) and str(decl.get("name") or "") == "http_call":
                desc = str(decl.get("description") or "").strip()
                if desc:
                    tool.description = desc
                break
        self._tools = [tool]
        logger.info(
            "http_request_pack_ready",
            allowed_domains=domains or ["*"],
            open_domains=_domains_open(domains),
            timeout_seconds=tool._timeout_seconds,
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
