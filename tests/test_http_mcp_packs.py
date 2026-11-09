"""HttpRequestToolPack / McpToolPack 单元测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from csagent.react.models import CustomerScope
from csagent.tools.packs.http_request_pack import (
    HttpCallTool,
    HttpRequestToolPack,
    host_matches_whitelist,
)
from csagent.tools.packs.mcp_pack import McpToolPack, _sanitize_tool_name


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s-test", channel="http_api")


def test_host_whitelist_exact_and_wildcard():
    assert host_matches_whitelist("api.internal.company.com", ["*.internal.company.com"])
    assert host_matches_whitelist("internal.company.com", ["*.internal.company.com"])
    assert host_matches_whitelist("localhost", ["localhost", "127.0.0.1"])
    assert not host_matches_whitelist("evil.com", ["*.internal.company.com"])
    assert not host_matches_whitelist("company.com", ["*.internal.company.com"])
    # 空 / * = 不限制
    assert host_matches_whitelist("evil.com", [])
    assert host_matches_whitelist("evil.com", ["*"])


@pytest.mark.asyncio
async def test_http_call_open_when_empty_domains(scope: CustomerScope, monkeypatch: pytest.MonkeyPatch):
    tool = HttpCallTool(allowed_domains=[])
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(
        return_value=httpx.Response(
            200,
            text="ok",
            request=httpx.Request("GET", "https://example.com/x"),
        )
    )
    monkeypatch.setattr(
        "csagent.tools.packs.http_request_pack.httpx.AsyncClient",
        lambda **kwargs: mock_client,
    )
    out = await tool.execute({"url": "https://example.com/x"}, scope)
    assert "status=200" in out


@pytest.mark.asyncio
async def test_http_call_rejects_off_whitelist(scope: CustomerScope):
    tool = HttpCallTool(allowed_domains=["localhost"])
    out = await tool.execute({"url": "https://example.com/api"}, scope)
    assert "未允许" in out or "域名" in out


@pytest.mark.asyncio
async def test_http_call_rejects_bad_method(scope: CustomerScope):
    tool = HttpCallTool(allowed_domains=["localhost"], allowed_methods=["GET"])
    out = await tool.execute({"url": "http://localhost/x", "method": "POST"}, scope)
    assert "method" in out.lower() or "不允许" in out


@pytest.mark.asyncio
async def test_http_call_success(scope: CustomerScope, monkeypatch: pytest.MonkeyPatch):
    tool = HttpCallTool(allowed_domains=["127.0.0.1"], max_response_chars=100)

    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.request = AsyncMock(
        return_value=httpx.Response(
            200,
            text='{"ok": true}',
            request=httpx.Request("POST", "http://127.0.0.1/echo"),
            headers={"content-type": "application/json"},
        )
    )

    monkeypatch.setattr(
        "csagent.tools.packs.http_request_pack.httpx.AsyncClient",
        lambda **kwargs: mock_client,
    )
    out = await tool.execute(
        {
            "url": "http://127.0.0.1/echo",
            "method": "POST",
            "json_body": {"a": 1},
        },
        scope,
    )
    assert "status=200" in out
    assert '{"ok": true}' in out
    mock_client.request.assert_awaited()


@pytest.mark.asyncio
async def test_http_pack_loads_from_config():
    pack = HttpRequestToolPack()
    pack.initialize(
        {
            "allowed_domains": ["localhost"],
            "timeout_seconds": 5,
            "tools": [{"name": "http_call", "description": "自定义描述"}],
        }
    )
    tools = pack.get_tools()
    assert len(tools) == 1
    assert tools[0].name == "http_call"
    assert tools[0].description == "自定义描述"


def test_sanitize_mcp_tool_name():
    assert _sanitize_tool_name("demo", "list-files") == "mcp_demo_list_files"


@pytest.mark.asyncio
async def test_mcp_pack_without_servers(scope: CustomerScope):
    pack = McpToolPack()
    pack.initialize({"servers": []})
    await pack.connect()
    names = [t.name for t in pack.get_tools()]
    assert names == ["mcp_list_tools", "mcp_call_tool"]
    listed = await pack.get_tools()[0].execute({}, scope)
    assert "未配置" in listed


@pytest.mark.asyncio
async def test_mcp_pack_discovers_and_calls(scope: CustomerScope):
    pack = McpToolPack()
    pack.initialize(
        {
            "servers": [{"name": "demo", "transport": "stdio", "command": "echo"}],
            "register_discovered_tools": True,
        }
    )
    pack._mcp_available = True
    pack._init_error = None

    remote_tool = SimpleNamespace(name="add", description="Add two numbers")
    session = MagicMock()
    session.list_tools = AsyncMock(return_value=SimpleNamespace(tools=[remote_tool]))
    session.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            content=[SimpleNamespace(text="8")],
            structuredContent=None,
            isError=False,
        )
    )

    handle = pack._handles["demo"]
    handle.session = session
    handle.connect = AsyncMock()  # type: ignore[method-assign]

    await pack.connect()
    names = [t.name for t in pack.get_tools()]
    assert "mcp_list_tools" in names
    assert "mcp_call_tool" in names
    assert "mcp_demo_add" in names

    catalog = await pack.get_tools()[0].execute({}, scope)
    assert "add" in catalog

    call_tool = next(t for t in pack.get_tools() if t.name == "mcp_call_tool")
    out = await call_tool.execute(
        {"server": "demo", "tool": "add", "arguments": {"a": 3, "b": 5}},
        scope,
    )
    assert "8" in out
    session.call_tool.assert_awaited_with("add", arguments={"a": 3, "b": 5})

    discovered = next(t for t in pack.get_tools() if t.name == "mcp_demo_add")
    out2 = await discovered.execute({"a": 1, "b": 2}, scope)
    assert "mcp_server=demo" in out2


@pytest.mark.asyncio
async def test_mcp_pack_missing_sdk_message(scope: CustomerScope):
    pack = McpToolPack()
    pack.initialize({"servers": [{"name": "x", "transport": "stdio", "command": "x"}]})
    pack._mcp_available = False
    pack._init_error = "未安装 mcp SDK"
    out = await pack.get_tools()[1].execute(
        {"server": "x", "tool": "t", "arguments": {}},
        scope,
    )
    assert "mcp SDK" in out
