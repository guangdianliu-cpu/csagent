"""通用多模态图片识别与视觉分析工具包。

提供类似 read_file 的通用图片读取与多模态分析工具 view_image：
  - 支持工作区中任意图片读取（用户上传附件 inbound/、生成产物 outbound/、项目图片等）；
  - 支持传入自定义 prompt 指定分析要求（提取表格数据、识别图表走势、文字 OCR、排版质检等）；
  - 支持按需指定视觉模型（如 deepseek-v4-flash 等轻量模型节省 token，或高精度模型）；
  - 支持独立配置视觉模型服务端点与密钥。
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import Any

import structlog
from PIL import Image

from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.infra.llm_client import LlmClient
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.packs.workspace_files_pack import (
    _DEFAULT_DENY_NAMES,
    _not_found_message,
    _reject_dotdot,
    _tool_rel_path,
    is_denied_path,
    resolve_workspace_path,
)

logger = structlog.get_logger(__name__)

_SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB

_DEFAULT_VISION_PROMPT = (
    "请详细分析这张图片并提取关键信息：\n"
    "1. 【主要内容】：识别图中的核心主体、标题、图表类型或展示场景；\n"
    "2. 【关键信息/数据】：提取图中的主要文字、关键数值、图表趋势或重要结论；\n"
    "3. 【结构与排版】：描述布局结构，若为文档/幻灯片页面则检查是否有文字溢出截断、重叠遮挡或排版异常；\n"
    "4. 【总结】：用简练语言给出核心结论。"
)


class ViewImageTool(BaseTool):
    """通用图片读取与视觉多模态分析工具。"""

    name = "view_image"
    description = (
        "读取工作区或会话中的图片文件（PNG/JPG/JPEG/WEBP/BMP/GIF 等），由多模态视觉模型进行理解、"
        "信息提取、图表分析或排版质检。类似文本文件的 read_file，作为查看图像的通用工具使用。"
        "参数 path 必填（支持会话目录、inbound/ 附件、outbound/ 产物等）；"
        "prompt 可选（具体分析要求，如提取数据、图表解读、排版质检等）。"
    )
    timeout_seconds = 120.0

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "图片文件路径（支持会话相对路径、inbound/ 附件、outbound/ 产物或项目路径）",
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "对该图片的分析要求或提问（例如提取图片中的文字与数据、分析图表走势、"
                        "识别主体内容、或检查版面排版等）。留空时默认进行全方位内容理解与关键信息提取。"
                    ),
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        session_scoped: bool = True,
        max_image_bytes: int = _MAX_IMAGE_BYTES,
        default_model: str = "",
        deny_names: set[str] | None = None,
        llm_client: LlmClient | None = None,
    ) -> None:
        self._root = root
        self._allow_absolute = allow_absolute
        self._session_scoped = session_scoped
        self._max_image_bytes = max_image_bytes
        self._default_model = default_model
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)
        self._llm_client = llm_client

    def _get_llm_client(self) -> tuple[LlmClient, str]:
        """获取适用于视觉调用的 LlmClient 及生效的模型名称。"""
        if self._llm_client is not None:
            settings = getattr(self._llm_client, "_settings", None) or get_settings()
            active_model = (
                self._default_model
                or getattr(settings, "llm_vision_model", "").strip()
                or settings.llm_model
            )
            return self._llm_client, active_model

        settings = get_settings()
        active_model = (
            self._default_model
            or getattr(settings, "llm_vision_model", "").strip()
            or settings.llm_model
        )

        # 若独立配置了视觉模型服务地址或密钥，构建专用客户端；否则复用主客户端
        vision_base = getattr(settings, "llm_vision_base_url", "").strip()
        vision_key = getattr(settings, "llm_vision_api_key", "").strip()
        if vision_base or vision_key:
            vision_settings = settings.model_copy(
                update={
                    "llm_base_url": vision_base or settings.llm_base_url,
                    "llm_api_key": vision_key or settings.llm_api_key,
                    "llm_model": active_model,
                }
            )
            return LlmClient(vision_settings), active_model

        self._llm_client = LlmClient(settings)
        return self._llm_client, active_model

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        raw = str(arguments.get("path") or "").strip()
        if not raw:
            return "错误: 缺少参数 path"

        # 优先探测会话内已存在文件（支持用户上传 inbound、生成产物 outbound 以及项目文件）
        rel = _tool_rel_path(
            raw,
            scope,
            session_scoped=self._session_scoped,
            for_write=False,
            prefer_outbound=True,
            project_root=self._root,
        )
        if err := _reject_dotdot(rel):
            return err

        target = resolve_workspace_path(
            self._root, rel, allow_absolute=self._allow_absolute
        )
        if isinstance(target, str):
            return target
        if is_denied_path(target, self._deny_names):
            return f"错误: 出于安全限制，拒绝访问敏感文件: {target.name}"
        if not target.is_file():
            return _not_found_message(
                raw, rel, scope, session_scoped=self._session_scoped, prefer_outbound=True
            )

        suffix = target.suffix.lower()
        if suffix not in _SUPPORTED_IMAGE_SUFFIXES:
            return (
                f"错误: 不支持的图片格式 {suffix}；仅支持 {sorted(_SUPPORTED_IMAGE_SUFFIXES)}。"
                "如果是文本文件请用 read_file。"
            )

        try:
            size_bytes = target.stat().st_size
        except OSError as exc:
            return f"错误: 无法获取文件信息: {exc}"

        if size_bytes == 0:
            return f"错误: 图片文件为空 (0 bytes): {rel}"
        if size_bytes > self._max_image_bytes:
            limit_mb = self._max_image_bytes / (1024 * 1024)
            actual_mb = size_bytes / (1024 * 1024)
            return f"错误: 图片大小 ({actual_mb:.1f} MB) 超出上限 ({limit_mb:.1f} MB)"

        # 尝试读取图片规格（宽x高）
        dim_str = "未知尺寸"
        try:
            with Image.open(target) as img:
                w, h = img.size
                dim_str = f"{w}x{h} px"
        except Exception:
            pass

        try:
            raw_bytes = target.read_bytes()
        except OSError as exc:
            return f"错误: 读取图片文件失败: {exc}"

        mime = mimetypes.guess_type(target.name)[0] or "image/png"
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"

        user_prompt = str(arguments.get("prompt") or "").strip() or _DEFAULT_VISION_PROMPT

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ]

        try:
            client, active_model = self._get_llm_client()
            # 走流式：非流式下 httpx 的 read timeout 管「整段响应」，服务端不生成完
            # 就不吐第一个字节，长质检（逐子图检查 + 大图 prefill）会被 60s 掐死。
            # 流式按 chunk 重置读超时，只要 token 在流动就不会触发。
            analysis = await client.chat_stream(messages, model=active_model, json_mode=False)
        except Exception as exc:
            logger.warning("view_image_llm_call_failed", path=rel, error=str(exc))
            return f"错误: 视觉模型分析失败: {exc}"

        if not analysis or not analysis.strip():
            return f"错误: 视觉模型未返回有效分析结果: {rel}"

        size_kb = size_bytes / 1024.0
        return (
            f"【图片内容分析: {rel}】\n"
            f"• 规格: 尺寸 {dim_str} | 大小 {size_kb:.1f} KB | 格式 {suffix.upper().lstrip('.')} | 模型 {active_model}\n"
            f"--------------------------------------------------\n"
            f"{analysis.strip()}"
        )


class ImageToolPack(ToolPack):
    """通用多模态图片识别与分析工具包。"""

    pack_name = "image_tools"
    description = "工作区图片与多模态视觉理解分析"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        raw_root = config.get("root_dir") or "."
        root = Path(raw_root)
        if not root.is_absolute():
            root = (PROJECT_ROOT / root).resolve()

        session_scoped = bool(config.get("session_scoped", True))
        allow_absolute = bool(config.get("allow_absolute", False))
        max_image_bytes = int(config.get("max_image_bytes") or _MAX_IMAGE_BYTES)
        default_model = str(config.get("model") or config.get("default_model") or "").strip()

        tools: list[BaseTool] = [
            ViewImageTool(
                root,
                allow_absolute=allow_absolute,
                session_scoped=session_scoped,
                max_image_bytes=max_image_bytes,
                default_model=default_model,
            )
        ]

        desc_by_name = {
            str(item.get("name")): str(item.get("description") or "")
            for item in (config.get("tools") or [])
            if isinstance(item, dict) and item.get("name")
        }
        for tool in tools:
            if desc_by_name.get(tool.name):
                tool.description = desc_by_name[tool.name]

        self._tools = tools
        logger.info(
            "image_pack_ready",
            root=str(root),
            tools=[t.name for t in tools],
            allow_absolute=allow_absolute,
            session_scoped=session_scoped,
            default_model=default_model or "(inherit)",
        )

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)

    async def shutdown(self) -> None:
        for tool in self._tools:
            if isinstance(tool, ViewImageTool) and tool._llm_client is not None:
                await tool._llm_client.close()
