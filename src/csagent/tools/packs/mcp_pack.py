"""MCP 协议工具包：连接 MCP Server、动态发现并代理远端工具。"""

from __future__ import annotations

import asyncio
import json
import os
import re
from typing import Any

import structlog

from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack

logger = structlog.get_logger(__name__)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_]+")


def _sanitize_tool_name(server: str, tool: str) -> str:
    """生成本地唯一工具名：mcp_{server}_{tool}。"""
    left = _SAFE_NAME.sub("_", server.strip()) or "server"
    right = _SAFE_NAME.sub("_", tool.strip()) or "tool"
    return f"mcp_{left}_{right}"


def _resolve_env_value(value: str) -> str:
    """支持 ``${ENV}`` 或 ``$ENV`` 环境变量展开。"""
    text = str(value)
    if text.startswith("${") and text.endswith("}") and len(text) > 3:
        return os.getenv(text[2:-1], "")
    if text.startswith("$") and len(text) > 1 and text[1:].replace("_", "").isalnum():
        return os.getenv(text[1:], "")
    return text


def _format_mcp_content(result: Any, *, max_chars: int) -> str:
    """把 MCP call_tool 结果压成可注入 prompt 的文本。"""
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for block in content:
        text = getattr(block, "text", None)
        if text:
            parts.append(str(text))
            continue
        # 兜底：结构化块转 JSON
        data = getattr(block, "data", None)
        if data is not None:
            try:
                parts.append(json.dumps(data, ensure_ascii=False))
            except TypeError:
                parts.append(str(data))
            continue
        parts.append(str(block))

    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    if structured is not None and not parts:
        try:
            parts.append(json.dumps(structured, ensure_ascii=False))
        except TypeError:
            parts.append(str(structured))

    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    text = "\n".join(parts).strip() or "(empty)"
    if is_error:
        text = f"[MCP error]\n{text}"
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…(truncated)"
    return text


class _McpSessionHandle:
    """单个 MCP Server 的长连接句柄。"""

    def __init__(self, name: str, config: dict[str, Any]) -> None:
        self.name = name
        self.config = config
        self.session: Any = None
        self._stack: list[Any] = []
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        async with self._lock:
            if self.session is not None:
                return
            transport = str(self.config.get("transport") or "stdio").strip().lower()
            if transport == "stdio":
                await self._connect_stdio()
            elif transport in {"sse", "http_sse"}:
                await self._connect_sse()
            elif transport in {"streamable_http", "http", "streamable-http"}:
                await self._connect_streamable_http()
            else:
                raise ValueError(f"不支持的 MCP transport={transport!r}（server={self.name}）")

    async def _connect_stdio(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        command = str(self.config.get("command") or "").strip()
        if not command:
            raise ValueError(f"MCP server {self.name!r} stdio 缺少 command")
        args = [str(a) for a in (self.config.get("args") or [])]
        env_cfg = self.config.get("env") or {}
        env = None
        if isinstance(env_cfg, dict) and env_cfg:
            env = {**os.environ}
            for key, value in env_cfg.items():
                env[str(key)] = _resolve_env_value(str(value))

        params = StdioServerParameters(command=command, args=args, env=env)
        stdio_cm = stdio_client(params)
        read, write = await stdio_cm.__aenter__()
        self._stack.append(stdio_cm)
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        self._stack.append(session_cm)
        await session.initialize()
        self.session = session

    async def _connect_sse(self) -> None:
        from mcp import ClientSession
        from mcp.client.sse import sse_client

        url = str(self.config.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server {self.name!r} sse 缺少 url")
        headers = self._resolved_headers()
        sse_cm = sse_client(url, headers=headers or None)
        read, write = await sse_cm.__aenter__()
        self._stack.append(sse_cm)
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        self._stack.append(session_cm)
        await session.initialize()
        self.session = session

    async def _connect_streamable_http(self) -> None:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        url = str(self.config.get("url") or "").strip()
        if not url:
            raise ValueError(f"MCP server {self.name!r} streamable_http 缺少 url")
        headers = self._resolved_headers()
        http_cm = streamablehttp_client(url, headers=headers or None)
        streams = await http_cm.__aenter__()
        self._stack.append(http_cm)
        # SDK 返回 (read, write) 或 (read, write, get_session_id)
        read, write = streams[0], streams[1]
        session_cm = ClientSession(read, write)
        session = await session_cm.__aenter__()
        self._stack.append(session_cm)
        await session.initialize()
        self.session = session

    def _resolved_headers(self) -> dict[str, str]:
        raw = self.config.get("headers") or {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): _resolve_env_value(str(v)) for k, v in raw.items()}

    async def close(self) -> None:
        async with self._lock:
            self.session = None
            while self._stack:
                cm = self._stack.pop()
                try:
                    await cm.__aexit__(None, None, None)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "mcp_session_close_failed",
                        server=self.name,
                        error=str(exc),
                    )


class McpDiscoveredTool(BaseTool):
    """代理单个已发现的远端 MCP 工具。"""

    def __init__(
        self,
        *,
        name: str,
        description: str,
        remote_name: str,
        server_name: str,
        pack: "McpToolPack",
    ) -> None:
        self.name = name
        self.description = description
        self._remote_name = remote_name
        self._server_name = server_name
        self._pack = pack

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        return await self._pack.call_remote_tool(
            server=self._server_name,
            tool_name=self._remote_name,
            arguments=arguments if isinstance(arguments, dict) else {},
            scope=scope,
        )


class McpListToolsTool(BaseTool):
    """列出已配置 MCP Server 上的工具。"""

    name = "mcp_list_tools"
    description = (
        "列出已连接 MCP Server 的远端工具。"
        "可选参数: server(指定服务器名；省略则列出全部)。"
    )

    def __init__(self, pack: "McpToolPack") -> None:
        self._pack = pack

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        server = str(arguments.get("server") or "").strip() or None
        await self._pack.ensure_connected()
        return self._pack.format_tool_catalog(server=server)


class McpCallTool(BaseTool):
    """按名调用远端 MCP 工具（动态发现的通用入口）。"""

    name = "mcp_call_tool"
    description = (
        "调用 MCP 远端工具。参数: server(必填), tool(必填), arguments(可选对象)。"
        "可用 mcp_list_tools 查看工具清单。"
    )

    def __init__(self, pack: "McpToolPack") -> None:
        self._pack = pack

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        server = str(arguments.get("server") or "").strip()
        tool = str(arguments.get("tool") or arguments.get("tool_name") or "").strip()
        if not server:
            return "错误: 缺少参数 server"
        if not tool:
            return "错误: 缺少参数 tool"
        raw_args = arguments.get("arguments")
        if raw_args is None:
            # 允许把除 server/tool 外的字段当作远端参数
            raw_args = {
                k: v
                for k, v in arguments.items()
                if k not in {"server", "tool", "tool_name", "arguments"}
            }
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                return "错误: arguments 必须是 JSON 对象"
        if not isinstance(raw_args, dict):
            return "错误: arguments 必须是 JSON 对象"
        return await self._pack.call_remote_tool(
            server=server,
            tool_name=tool,
            arguments=raw_args,
            scope=scope,
        )


class McpToolPack(ToolPack):
    """MCP 协议工具包：连接 Server、动态发现工具并代理调用。"""

    pack_name = "mcp"
    description = "MCP 协议工具包"

    def __init__(self) -> None:
        self._handles: dict[str, _McpSessionHandle] = {}
        self._discovered: list[McpDiscoveredTool] = []
        self._catalog: list[dict[str, Any]] = []
        self._tools: list[BaseTool] = []
        self._max_result_chars = 8000
        self._register_discovered = True
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._mcp_available = True
        self._init_error: str | None = None

    def initialize(self, config: dict[str, Any]) -> None:
        # Step 1: 解析 servers 配置
        servers = config.get("servers") or []
        if not isinstance(servers, list):
            servers = []
        self._max_result_chars = int(config.get("max_result_chars") or 8000)
        self._register_discovered = bool(config.get("register_discovered_tools", True))

        self._handles = {}
        for item in servers:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                logger.warning("mcp_server_missing_name", item=item)
                continue
            self._handles[name] = _McpSessionHandle(name, item)

        # Step 2: 检测 SDK
        try:
            import mcp  # noqa: F401
        except ImportError:
            self._mcp_available = False
            self._init_error = (
                "未安装 mcp SDK。请执行: pip install -e \".[mcp]\" 或 pip install mcp"
            )
            logger.warning("mcp_sdk_missing")

        # Step 3: 稳定入口工具（即使尚未 connect 也可被 Agent 挂载）
        self._tools = [McpListToolsTool(self), McpCallTool(self)]
        logger.info(
            "mcp_pack_initialized",
            servers=list(self._handles.keys()),
            mcp_sdk=self._mcp_available,
        )

    def get_tools(self) -> list[BaseTool]:
        if self._register_discovered and self._discovered:
            return [*self._tools[:2], *self._discovered]
        return list(self._tools)

    async def connect(self) -> None:
        """连接全部 Server 并动态发现工具（启动期调用）。"""
        await self.ensure_connected()

    async def ensure_connected(self) -> None:
        async with self._connect_lock:
            if self._connected:
                return
            if not self._handles:
                self._connected = True
                logger.info("mcp_pack_no_servers_configured")
                return
            if not self._mcp_available:
                logger.warning("mcp_connect_skipped", reason=self._init_error)
                self._connected = True
                return

            discovered: list[McpDiscoveredTool] = []
            catalog: list[dict[str, Any]] = []
            for name, handle in self._handles.items():
                try:
                    await handle.connect()
                    assert handle.session is not None
                    listed = await handle.session.list_tools()
                    remote_tools = list(getattr(listed, "tools", None) or [])
                    for remote in remote_tools:
                        remote_name = str(getattr(remote, "name", "") or "").strip()
                        if not remote_name:
                            continue
                        desc = str(getattr(remote, "description", "") or "").strip()
                        local_name = _sanitize_tool_name(name, remote_name)
                        full_desc = (
                            f"[MCP:{name}] {desc or remote_name}"
                            f"（远端工具名={remote_name}）"
                        )
                        discovered.append(
                            McpDiscoveredTool(
                                name=local_name,
                                description=full_desc,
                                remote_name=remote_name,
                                server_name=name,
                                pack=self,
                            )
                        )
                        catalog.append(
                            {
                                "server": name,
                                "tool": remote_name,
                                "local_name": local_name,
                                "description": desc,
                            }
                        )
                    logger.info(
                        "mcp_server_connected",
                        server=name,
                        tools=[t.name for t in remote_tools],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("mcp_server_connect_failed", server=name, error=str(exc))
                    catalog.append(
                        {
                            "server": name,
                            "tool": None,
                            "error": str(exc),
                        }
                    )

            self._discovered = discovered
            self._catalog = catalog
            self._connected = True

    async def call_remote_tool(
        self,
        *,
        server: str,
        tool_name: str,
        arguments: dict[str, Any],
        scope: CustomerScope,
    ) -> str:
        if not self._mcp_available:
            return f"错误: {self._init_error}"
        if not self._handles:
            return "错误: 未配置任何 MCP servers（见 tool_packs.yaml config.servers）"

        await self.ensure_connected()
        handle = self._handles.get(server)
        if handle is None:
            known = ", ".join(sorted(self._handles)) or "(none)"
            return f"错误: 未知 MCP server={server!r}，已知: {known}"
        if handle.session is None:
            return f"错误: MCP server={server!r} 未连接成功，请检查日志 / init doctor"

        try:
            result = await handle.session.call_tool(tool_name, arguments=arguments)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "mcp_call_failed",
                server=server,
                tool=tool_name,
                error=str(exc),
            )
            return f"MCP 调用失败 server={server} tool={tool_name}: {exc}"

        header = (
            f"mcp_server={server} tool={tool_name} "
            f"channel={scope.channel or '-'}"
        )
        body = _format_mcp_content(result, max_chars=self._max_result_chars)
        return f"{header}\n{body}"

    def format_tool_catalog(self, *, server: str | None = None) -> str:
        if not self._handles:
            return "未配置 MCP servers。在 tool_packs.yaml 的 mcp.config.servers 中添加。"
        if not self._mcp_available:
            return f"错误: {self._init_error}"

        rows = self._catalog
        if server:
            rows = [r for r in rows if r.get("server") == server]
            if not rows and server not in self._handles:
                known = ", ".join(sorted(self._handles)) or "(none)"
                return f"错误: 未知 server={server!r}，已知: {known}"

        if not rows:
            return f"MCP server={server or '*'} 暂无已发现工具（可能连接失败）。"

        lines = ["MCP 工具清单:"]
        for row in rows:
            if row.get("error"):
                lines.append(f"- [{row['server']}] 连接失败: {row['error']}")
                continue
            lines.append(
                f"- [{row['server']}] {row['tool']} "
                f"(local={row.get('local_name')}) "
                f"{row.get('description') or ''}".rstrip()
            )
        return "\n".join(lines)

    async def shutdown(self) -> None:
        for handle in self._handles.values():
            await handle.close()
        self._connected = False
        self._discovered = []
        self._catalog = []
