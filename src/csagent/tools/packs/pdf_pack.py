"""HTML → PDF 工具包：把工作区里的 HTML 转成可 send_file 的 PDF。

模型写 HTML，本工具出 PDF。版式 CSS 由调用方自己写（或后续模板），这里只铺
渲染与落盘路径。默认写到当前会话 outbound/，并登记 artifacts。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import structlog

from csagent.channels.workspace_uploads import (
    bind_session_workspace_path,
    session_outbound_rel,
)
from csagent.infra.config import PROJECT_ROOT
from csagent.memory.artifact_store import register_artifact
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack
from csagent.tools.packs.html_pdf_render import HtmlPdfError, render_html_to_pdf_async
from csagent.tools.packs.workspace_files_pack import (
    _DEFAULT_DENY_NAMES,
    _send_file_hint,
    is_denied_path,
    resolve_workspace_path,
)

logger = structlog.get_logger(__name__)

_HTML_SUFFIXES = {".html", ".htm"}


class HtmlToPdfTool(BaseTool):
    """把工作区 HTML 渲染成 PDF。"""

    name = "html_to_pdf"
    description = (
        "把已有的 HTML 文件转成 PDF，写入当前会话 outbound/。"
        "参数 html_path 必填（先 write_file 写出 HTML）；pdf_path 可选，默认同名 .pdf。"
        "仅当用户明确要求发回时再 send_file。版式写在 HTML/CSS 里，本工具不做模板。"
    )
    timeout_seconds = 90.0

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "html_path": {
                    "type": "string",
                    "description": "源 HTML 路径，相对工作区，如 report.html",
                },
                "pdf_path": {
                    "type": "string",
                    "description": "输出 PDF 路径；默认 outbound 下与 HTML 同名",
                },
            },
            "required": ["html_path"],
            "additionalProperties": False,
        }

    def __init__(
        self,
        root: Path,
        *,
        allow_absolute: bool = False,
        session_scoped: bool = True,
        send_enabled: bool = True,
        engine: str = "auto",
        chromium_path: str = "",
        timeout_seconds: float = 60.0,
        deny_names: set[str] | None = None,
    ) -> None:
        """绑定工作区根目录与渲染选项。"""
        self._root = root
        self._allow_absolute = allow_absolute
        self._session_scoped = session_scoped
        self._send_enabled = send_enabled
        self._engine = engine
        self._chromium_path = chromium_path
        self._timeout_seconds = timeout_seconds
        self._deny_names = deny_names or set(_DEFAULT_DENY_NAMES)

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        """渲染 HTML 并登记 outbound 产物。

        :param arguments: ``html_path`` 必填；``pdf_path`` 可选
        :param scope: 当前会话，用于绑定 inbound/outbound
        :returns: 成功文案（含相对路径与 send_file 提示）或 ``错误: ...``
        """
        html_raw = str(arguments.get("html_path") or "").strip()
        if not html_raw:
            return "错误: 缺少参数 html_path"
        html_rel = self._bind(html_raw, scope, for_write=False)
        html_path = self._resolve(html_rel)
        if isinstance(html_path, str):
            return html_path
        if is_denied_path(html_path, self._deny_names):
            return f"错误: 拒绝读取敏感文件: {html_path.name}"
        if html_path.suffix.lower() not in _HTML_SUFFIXES:
            return "错误: html_path 必须是 .html / .htm"
        if not html_path.is_file():
            return f"错误: 找不到 HTML 文件: {html_rel}"

        pdf_raw = str(arguments.get("pdf_path") or "").strip()
        if pdf_raw:
            pdf_rel = self._bind(pdf_raw, scope, for_write=True)
        else:
            pdf_rel = f"{session_outbound_rel(scope.session_id)}/{html_path.stem}.pdf"
        if not pdf_rel.lower().endswith(".pdf"):
            return "错误: pdf_path 必须以 .pdf 结尾"
        pdf_path = self._resolve(pdf_rel)
        if isinstance(pdf_path, str):
            return pdf_path
        if is_denied_path(pdf_path, self._deny_names):
            return f"错误: 拒绝写入敏感路径: {pdf_path.name}"

        try:
            engine = await render_html_to_pdf_async(
                html_path,
                pdf_path,
                engine=self._engine,
                chromium_path=self._chromium_path,
                timeout_seconds=self._timeout_seconds,
            )
        except HtmlPdfError as exc:
            logger.warning("html_to_pdf_failed", html=html_rel, error=str(exc))
            return f"错误: {exc}"
        except OSError as exc:
            logger.warning("html_to_pdf_io_failed", html=html_rel, error=str(exc))
            return f"错误: 读写失败: {exc}"

        try:
            rel_out = pdf_path.relative_to(self._root.resolve()).as_posix()
        except ValueError:
            rel_out = str(pdf_path)
        size = pdf_path.stat().st_size
        register_artifact(
            session_id=scope.session_id,
            kind="outbound",
            path=rel_out,
            filename=pdf_path.name,
            size=size,
        )
        hint = _send_file_hint(rel_out, send_enabled=self._send_enabled)
        return f"已生成 PDF: {rel_out}（{size} bytes，引擎={engine}）{hint}"

    def _bind(self, relative: str, scope: CustomerScope, *, for_write: bool) -> str:
        """会话作用域下把裸文件名绑到 inbound/outbound。"""
        if not self._session_scoped:
            return relative
        return bind_session_workspace_path(
            relative,
            scope.session_id,
            for_write=for_write,
            prefer_outbound=for_write,
            project_root=self._root,
        )

    def _resolve(self, relative: str) -> Path | str:
        """解析工作区路径；越界时返回错误字符串。"""
        return resolve_workspace_path(
            self._root, relative, allow_absolute=self._allow_absolute
        )


class HtmlPdfToolPack(ToolPack):
    """HTML → PDF。"""

    pack_name = "html_pdf"
    description = "把工作区 HTML 渲染成 PDF"

    def __init__(self) -> None:
        """延迟到 ``initialize`` 再构造工具。"""
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        """按 YAML config 构造 ``html_to_pdf``。"""
        root = Path(str(config.get("root_dir") or "."))
        if not root.is_absolute():
            root = PROJECT_ROOT / root
        root = root.resolve()
        tool = HtmlToPdfTool(
            root,
            allow_absolute=bool(config.get("allow_absolute", False)),
            session_scoped=bool(config.get("session_scoped", True)),
            send_enabled=bool(config.get("send_enabled", True)),
            engine=str(config.get("engine") or "auto"),
            chromium_path=str(config.get("chromium_path") or ""),
            timeout_seconds=float(config.get("timeout_seconds") or 60.0),
        )
        for item in config.get("tools") or []:
            if isinstance(item, dict) and item.get("name") == "html_to_pdf" and item.get("description"):
                tool.description = str(item["description"])
        self._tools = [tool]
        logger.info(
            "html_pdf_pack_ready",
            root=str(root),
            engine=str(config.get("engine") or "auto"),
        )

    def get_tools(self) -> list[BaseTool]:
        """返回已初始化的工具列表。"""
        return list(self._tools)
