"""Web 搜索工具包：DuckDuckGo（免 Key）或自定义 HTTP 搜索 API。"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote_plus

import httpx
import structlog

from csagent.prompts.domain_text import WEB_SEARCH_NOTE

from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)


class WebSearchTool(BaseTool):
    """搜索互联网公开信息。

    支持：
    - duckduckgo：免 API Key（Instant Answer JSON）
    - custom：调用 config.endpoint（需 API Key / Bearer）
    - google / bing：需配置 API Key（未配 Key 时明确降级）
    """

    name = "web_search"
    description = (
        "搜索互联网获取最新公开信息。参数: query(必填), max_results(可选)。"
        f"{WEB_SEARCH_NOTE}"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索关键词"},
                "max_results": {
                    "type": "integer",
                    "description": "最多返回条数，默认引擎配置值",
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        *,
        search_engine: str = "duckduckgo",
        api_key: str = "",
        max_results: int = 5,
        endpoint: str = "",
        timeout_seconds: float = 15.0,
    ) -> None:
        self._search_engine = search_engine.strip().lower() or "duckduckgo"
        self._api_key = api_key
        self._max_results = max(1, min(max_results, 10))
        self._endpoint = endpoint.strip()
        self._timeout_seconds = timeout_seconds

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        query = str(arguments.get("query") or "").strip()
        if not query:
            return "错误: 缺少参数 query"

        limit = arguments.get("max_results")
        try:
            max_results = int(limit) if limit is not None else self._max_results
        except (TypeError, ValueError):
            max_results = self._max_results
        max_results = max(1, min(max_results, 10))

        # Step 1: 按引擎分流
        try:
            if self._search_engine == "duckduckgo":
                return await self._search_duckduckgo(query, max_results)
            if self._search_engine == "custom":
                return await self._search_custom(query, max_results)
            if self._search_engine in {"google", "bing"}:
                return await self._search_provider_stub(query)
            return (
                f"错误: 不支持的 search_engine={self._search_engine!r}，"
                "请使用 duckduckgo / custom / google / bing。"
            )
        except httpx.TimeoutException:
            return f"错误: Web 搜索超时（{self._timeout_seconds}s）"
        except Exception as exc:  # noqa: BLE001
            logger.warning("web_search_failed", engine=self._search_engine, error=str(exc))
            return f"Web 搜索失败: {exc}"

    async def _search_duckduckgo(self, query: str, max_results: int) -> str:
        """Step 2a: DuckDuckGo Instant Answer（公开 JSON，无需 Key）。"""
        url = (
            "https://api.duckduckgo.com/"
            f"?q={quote_plus(query)}&format=json&no_html=1&skip_disambig=1"
        )
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            resp = await client.get(url, headers={"User-Agent": "cs-agent/0.5"})
            resp.raise_for_status()
            data = resp.json()

        lines: list[str] = ["搜索引擎: duckduckgo", f"查询: {query}"]
        abstract = str(data.get("AbstractText") or "").strip()
        abstract_url = str(data.get("AbstractURL") or "").strip()
        heading = str(data.get("Heading") or "").strip()
        if abstract:
            lines.append(f"\n摘要{'（' + heading + '）' if heading else ''}:" )
            lines.append(abstract[:1500])
            if abstract_url:
                lines.append(f"来源: {abstract_url}")

        related = data.get("RelatedTopics") or []
        items: list[str] = []
        for topic in related:
            if not isinstance(topic, dict):
                continue
            if "Topics" in topic:
                for sub in topic.get("Topics") or []:
                    if isinstance(sub, dict) and sub.get("Text"):
                        items.append(str(sub["Text"]))
            elif topic.get("Text"):
                items.append(str(topic["Text"]))
            if len(items) >= max_results:
                break

        if items:
            lines.append("\n相关条目:")
            for i, text in enumerate(items[:max_results], 1):
                lines.append(f"{i}. {text[:400]}")

        if len(lines) <= 2:
            return (
                f"未从 DuckDuckGo 检索到摘要结果（query={query!r}）。"
                "可换关键词重试，或改用 custom 引擎并配置 API。"
            )
        return "\n".join(lines)

    async def _search_custom(self, query: str, max_results: int) -> str:
        """Step 2b: 自定义搜索 endpoint（Bearer + JSON body）。"""
        if not self._endpoint:
            return "错误: custom 引擎需配置 endpoint（tool_packs.yaml config.endpoint）"
        if not self._api_key:
            return "错误: custom 引擎需配置 API Key（api_key_env）"

        payload = {"query": query, "max_results": max_results}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "cs-agent/0.5",
        }
        async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
            resp = await client.post(self._endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        # 兼容常见字段：results[{title,url,snippet}] 或纯文本
        if isinstance(data, dict) and "results" in data:
            results = data.get("results") or []
            lines = ["搜索引擎: custom", f"查询: {query}", ""]
            for i, item in enumerate(results[:max_results], 1):
                if not isinstance(item, dict):
                    continue
                title = str(item.get("title") or item.get("name") or "").strip()
                url = str(item.get("url") or item.get("link") or "").strip()
                snippet = str(
                    item.get("snippet") or item.get("content") or item.get("description") or ""
                ).strip()
                lines.append(f"{i}. {title or '(无标题)'}")
                if snippet:
                    lines.append(f"   {snippet[:500]}")
                if url:
                    lines.append(f"   {url}")
            return "\n".join(lines) if len(lines) > 3 else f"自定义搜索无结果（query={query!r}）"

        text = str(data)[:4000]
        return f"搜索引擎: custom\n查询: {query}\n\n{text}"

    async def _search_provider_stub(self, query: str) -> str:
        """Step 2c: Google/Bing 需厂商 SDK；未配 Key 时明确降级。"""
        if not self._api_key:
            return (
                f"错误: {self._search_engine} 搜索未配置 API Key，暂不可用。"
                "可改用 duckduckgo（免 Key），或配置 WEB_SEARCH_API_KEY。"
            )
        return (
            f"错误: {self._search_engine} 官方 SDK 尚未接入（query={query!r}）。"
            "请将 search_engine 设为 duckduckgo 或 custom。"
        )


class WebSearchToolPack(ToolPack):
    """Web 搜索工具包。"""

    pack_name = "web_search"
    description = "Web 搜索工具包"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        api_key_env = str(config.get("api_key_env") or "WEB_SEARCH_API_KEY")
        api_key = os.environ.get(api_key_env, "").strip()
        search_engine = str(config.get("search_engine") or "duckduckgo")
        tool = WebSearchTool(
            search_engine=search_engine,
            api_key=api_key,
            max_results=int(config.get("max_results") or 5),
            endpoint=str(config.get("endpoint") or ""),
            timeout_seconds=float(config.get("timeout_seconds") or 15),
        )
        for item in config.get("tools") or []:
            if isinstance(item, dict) and item.get("name") == "web_search":
                if item.get("description"):
                    tool.description = str(item["description"])
                break
        self._tools = [tool]
        logger.info(
            "web_search_pack_ready",
            engine=search_engine,
            has_api_key=bool(api_key),
            has_endpoint=bool(config.get("endpoint")),
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
