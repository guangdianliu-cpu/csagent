"""image_pack 工具包与 view_image 通用图片识别工具单元测试。"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from csagent.react.models import CustomerScope
from csagent.tools.packs.image_pack import ImageToolPack, ViewImageTool


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="wecom:BoWen", channel="wecom")


def _create_test_image(path: Path, width: int = 20, height: int = 20, color: str = "blue") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (width, height), color=color)
    img.save(str(path), format="PNG")
    return path


def test_image_pack_initialization(tmp_path: Path):
    pack = ImageToolPack()
    pack.initialize({"root_dir": str(tmp_path), "default_model": "glm-5.3-flash"})
    tools = pack.get_tools()
    assert len(tools) == 1
    assert tools[0].name == "view_image"

    schema = tools[0].parameters_schema
    assert schema["type"] == "object"
    assert "path" in schema["properties"]
    assert "prompt" in schema["properties"]
    # model 参数不对 Agent 暴露，统一由系统配置决定
    assert "model" not in schema["properties"]
    assert schema["required"] == ["path"]


@pytest.mark.asyncio
async def test_view_image_missing_path(tmp_path: Path, scope: CustomerScope):
    tool = ViewImageTool(root=tmp_path)
    res = await tool.execute({}, scope)
    assert "缺少参数 path" in res


@pytest.mark.asyncio
async def test_view_image_not_found(tmp_path: Path, scope: CustomerScope):
    tool = ViewImageTool(root=tmp_path)
    res = await tool.execute({"path": "nonexistent.png"}, scope)
    assert "不存在" in res


@pytest.mark.asyncio
async def test_view_image_unsupported_suffix(tmp_path: Path, scope: CustomerScope):
    txt_file = tmp_path / "data" / "uploads" / "wecom_BoWen" / "outbound" / "doc.txt"
    txt_file.parent.mkdir(parents=True, exist_ok=True)
    txt_file.write_text("hello", encoding="utf-8")

    tool = ViewImageTool(root=tmp_path)
    res = await tool.execute({"path": "doc.txt"}, scope)
    assert "不支持的图片格式" in res
    assert "read_file" in res


@pytest.mark.asyncio
async def test_view_image_empty_file(tmp_path: Path, scope: CustomerScope):
    empty_png = tmp_path / "data" / "uploads" / "wecom_BoWen" / "outbound" / "empty.png"
    empty_png.parent.mkdir(parents=True, exist_ok=True)
    empty_png.write_bytes(b"")

    tool = ViewImageTool(root=tmp_path)
    res = await tool.execute({"path": "empty.png"}, scope)
    assert "图片文件为空" in res


@pytest.mark.asyncio
async def test_view_image_success_with_mock_llm(tmp_path: Path, scope: CustomerScope):
    img_file = tmp_path / "data" / "uploads" / "wecom_BoWen" / "outbound" / "preview" / "p1.png"
    _create_test_image(img_file, width=100, height=50)

    mock_llm = MagicMock()
    mock_llm.chat_stream = AsyncMock(return_value="【内容分析】：图表显示2026年业绩上行趋势，排版良好。")

    tool = ViewImageTool(root=tmp_path, default_model="glm-5.3-flash", llm_client=mock_llm)
    res = await tool.execute({"path": "preview/p1.png"}, scope)

    assert "图片内容分析" in res
    assert "100x50 px" in res
    assert "PNG" in res
    assert "业绩上行趋势" in res
    assert "glm-5.3-flash" in res
    mock_llm.chat_stream.assert_awaited_once()

    # 验证传入模型的参数结构与模型名称
    call_args = mock_llm.chat_stream.await_args
    kwargs = call_args[1]
    assert kwargs.get("model") == "glm-5.3-flash"

    messages = call_args[0][0]
    assert len(messages) == 1
    content_parts = messages[0]["content"]
    assert any(p.get("type") == "text" for p in content_parts)
    assert any(p.get("type") == "image_url" for p in content_parts)


@pytest.mark.asyncio
async def test_view_image_custom_prompt_and_configured_vision_model(tmp_path: Path, scope: CustomerScope):
    # 测试读取用户上传的 inbound 附件图片
    inbound_img = tmp_path / "data" / "uploads" / "wecom_BoWen" / "inbound" / "user_table.png"
    _create_test_image(inbound_img, width=200, height=120)

    mock_llm = MagicMock()
    mock_llm.chat_stream = AsyncMock(return_value="提取表格数据：第1行 A=10, B=20")

    tool = ViewImageTool(root=tmp_path, default_model="glm-5.3-flash", llm_client=mock_llm)
    res = await tool.execute(
        {
            "path": "inbound/user_table.png",
            "prompt": "请提取图中的表格所有单元格数据",
        },
        scope,
    )

    assert "图片内容分析" in res
    assert "200x120 px" in res
    assert "glm-5.3-flash" in res
    assert "提取表格数据" in res

    # 验证透传了配置的模型与自定义 prompt
    call_args = mock_llm.chat_stream.await_args
    kwargs = call_args[1]
    assert kwargs.get("model") == "glm-5.3-flash"

    messages = call_args[0][0]
    text_part = [p for p in messages[0]["content"] if p.get("type") == "text"][0]
    assert text_part["text"] == "请提取图中的表格所有单元格数据"
