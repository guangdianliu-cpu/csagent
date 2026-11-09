"""统一消息网关：所有通道消息的唯一入口。

职责：
1. 接收 UnifiedMessage
2. 并发准入：全局闸门满员时先回一句排队提示，再等名额（M2.2）
3. 附件暂存到工作区 data/uploads/{session_id}/inbound/
4. 附件预处理（路径必带 + 小文本短预览 / 多模态 vision）
5. 仅在用户指代「这个文件」等时注入「当前用户文件」指针（普通提问不注入，避免绑旧附件）
6. 调用 MainAgent.handle_message_stream
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog

from csagent.agent import Agent as MainAgent
from csagent.channels.file_handler import FileHandler
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.channels.workspace_uploads import get_active_inbound, stage_attachment
from csagent.prompts.domain_text import (
    ATTACHMENT_LOST_HINT,
    ATTACHMENT_USAGE_HINT,
    FILE_ONLY_INSTRUCTION,
)
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.user_progress import QUEUED_NOTICE

logger = structlog.get_logger(__name__)

# 入站混合策略：小文本预览进提示词；大文件只给路径让 Agent read_file
_PREVIEW_CHARS = 500

# 领域口径见 prompts/domain_text.py（产品层，按行业各自维护）

# 本轮带了附件但一个都没落地时注入。此时「当前用户文件」指针还停在上一份文件上，
# 若照常注入，Agent 会拿旧文件顶替用户刚发的这份，且从回复里看不出错在哪。
_ATTACHMENT_LOST_NOTICE = (
    "注意：用户本轮发送/引用了文件，但系统未能取到文件内容（原因见上方附件说明）。\n"
    "工作区里没有这份文件。**不要**改用 inbound/ 或 outbound/ 里的其它文件顶替，"
    "也不要沿用此前提到过的文件。请直接告诉用户文件没收到、并请其重新发送。"
)

# 用户话术里出现这些指代时，才把「当前用户文件」指针注入正文（否则会误导无关提问）。
_FILE_DEIXIS_MARKERS = (
    "这个文件",
    "该文件",
    "上面的文件",
    "这份文件",
    "此文件",
    "那个文件",
    "这个表格",
    "该表格",
    "上面的表格",
    "这份表格",
    "这个附件",
    "该附件",
    "附件里",
    "附件中",
    "刚才的文件",
    "之前的文件",
    "上次的文件",
    "刚才发的",
    "刚发的文件",
)


def _user_refers_to_prior_file(text: str) -> bool:
    """用户是否在指代此前上传/挂着的文件（而非本轮新附件块）。"""
    body = (text or "").strip()
    if not body:
        return False
    return any(marker in body for marker in _FILE_DEIXIS_MARKERS)


class MessageGateway:
    """统一消息网关。"""

    def __init__(
        self,
        main_agent: MainAgent,
        file_handler: FileHandler | None = None,
        *,
        preview_chars: int = _PREVIEW_CHARS,
        max_concurrent_requests: int = 0,
    ) -> None:
        """:param max_concurrent_requests: 同时在跑的请求上限；``<= 0`` 表示不限流。"""
        self._main_agent = main_agent
        self._file_handler = file_handler or FileHandler()
        self._preview_chars = max(200, int(preview_chars))
        # 闸门在这里而不是在 Agent 里：拿名额必须发生在做任何实际工作**之前**，
        # 而附件下载与文本抽取（_enrich_with_attachments）就在 Agent 之前，
        # 那步本身也要打网络和磁盘，不该不受限地并发。
        self._limiter = (
            asyncio.Semaphore(max_concurrent_requests)
            if max_concurrent_requests > 0
            else None
        )

    # ---------------------------------------------------------------- 入口

    async def handle(
        self, message: UnifiedMessage
    ) -> AsyncIterator[AgentStreamEvent]:
        """处理统一消息，返回流式事件。满员时先回排队提示再等名额。"""
        if self._limiter is None:
            async for event in self._dispatch(message):
                yield event
            return

        # locked() 为真即「此刻没有空闲名额」。先把提示吐出去再 await，顺序反了
        # 用户就会在沉默里等待整段排队时间。
        if self._limiter.locked():
            logger.info(
                "message_gateway_queued",
                session_id=message.session_id,
                channel=message.channel,
            )
            yield AgentStreamEvent(
                event_type=AgentEventType.PROGRESS, content=QUEUED_NOTICE
            )

        async with self._limiter:
            async for event in self._dispatch(message):
                yield event

    async def _dispatch(
        self, message: UnifiedMessage
    ) -> AsyncIterator[AgentStreamEvent]:
        """附件预处理 + 提示词拼装 + 转交 Agent（原 ``handle`` 的全部内容）。"""
        enriched_text, vision_parts = await self._enrich_with_attachments(message)

        final_text = enriched_text or message.text
        if message.quote_text:
            final_text = f"[引用消息] {message.quote_text}\n\n{final_text}"

        # 只发文件不打字：补一句默认指令，否则模型面对纯附件块容易无所适从
        if not (message.text or "").strip() and message.attachments:
            final_text = f"{FILE_ONLY_INSTRUCTION}\n\n{final_text}"

        # 群聊整群一个会话：每句话标上发言人，模型才分得清是谁在问、在接谁的话
        final_text = self._with_speaker(message, final_text)

        # 附件全军覆没时，旧指针已不可信：注入告警而不是把它当成「这个文件」
        if self._all_attachments_lost(message):
            logger.warning(
                "message_gateway_attachments_lost",
                session_id=message.session_id,
                channel=message.channel,
                attachments=len(message.attachments),
            )
            final_text = f"{final_text}\n\n{_ATTACHMENT_LOST_NOTICE}"
        else:
            this_turn_has_file = any(
                att.metadata.get("workspace_path") for att in (message.attachments or [])
            )
            final_text = self._append_active_inbound_hint(
                final_text,
                message.session_id,
                this_turn_has_file=this_turn_has_file,
            )

        logger.debug(
            "message_gateway_handle",
            channel=message.channel,
            session_id=message.session_id,
            has_attachments=bool(message.attachments),
            vision_parts=len(vision_parts),
            image_mode=getattr(self._file_handler, "image_mode", None),
        )

        async for event in self._main_agent.handle_message_stream(
            session_id=message.session_id,
            text=final_text,
            channel=message.channel,
            vision_parts=vision_parts or None,
            transcript_user_text=self._transcript_user_text(message),
            transcript_user_files=self._transcript_user_files(message),
            principal=message.principal or None,
        ):
            yield event

    @staticmethod
    def _transcript_user_files(message: UnifiedMessage) -> list[dict]:
        """入站附件的结构化记录，供历史里出下载链接。

        只收 stage 成功（有 workspace_path）的那些——没落地的文件给个链接也是 404。
        """
        out = []
        for att in message.attachments or []:
            rel = (att.metadata or {}).get("workspace_path")
            if not rel:
                continue
            out.append(
                {
                    "filename": (att.filename or "未命名文件").strip(),
                    "workspace_path": str(rel),
                }
            )
        return out

    @staticmethod
    def _transcript_user_text(message: UnifiedMessage) -> str:
        """给对话记录用的用户原话：本人打的字 + 附件名。

        不能直接用 final_text——那里面塞满了附件块、工作区路径、内容预览和默认
        指令，是给模型看的，几千字起步。界面翻历史要看到的是「我当时发了什么」。

        附件只记文件名，不记路径：路径是内部实现，且 session 目录变了就失效。

        ``metadata["transcript_text"]`` 可以直接指定：批准后续跑那一轮，正文是给模型的
        系统说明，界面上只该显示「【已批准】…」。
        """
        override = str((message.metadata or {}).get("transcript_text") or "").strip()
        if override:
            return override
        parts: list[str] = []
        text = (message.text or "").strip()
        if text:
            parts.append(text)
        if message.quote_text:
            parts.insert(0, f"[引用] {message.quote_text.strip()}")
        names = [
            (att.filename or "未命名文件").strip()
            for att in (message.attachments or [])
        ]
        if names:
            parts.append(f"[附件] {'、'.join(names)}")
        text = "\n".join(parts) if parts else (message.text or "")
        return MessageGateway._with_speaker(message, text)

    @staticmethod
    def _with_speaker(message: UnifiedMessage, text: str) -> str:
        """群聊消息开头标上发言人：``[群成员 zhangsan] …``。

        通道在 ``metadata["speaker"]`` 里给发言人；单聊、网页、批准后续跑都不给，原样返回。
        """
        speaker = str((message.metadata or {}).get("speaker") or "").strip()
        if not speaker:
            return text
        return f"[群成员 {speaker}] {text}"

    async def _enrich_with_attachments(
        self, message: UnifiedMessage
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """暂存附件并组装提示：路径必带，文本仅短预览。"""
        if not message.attachments:
            return None, []

        parts = [message.text] if message.text else []
        vision_parts: list[dict[str, Any]] = []

        # 「当前用户文件」指针归属：主消息自带附件时以主消息为准，引用里的旧附件
        # 只暂存不抢指针；主消息没有附件时才让引用更新——群聊里文件只能经引用
        # 抵达（裸文件消息不会推送给机器人），这条分支不能省。
        has_main_attachment = any(
            not att.metadata.get("from_quote") for att in message.attachments
        )

        for index, att in enumerate(message.attachments):
            from_quote = bool(att.metadata.get("from_quote"))
            workspace_path = stage_attachment(
                att,
                session_id=message.session_id,
                index=index,
                update_pointer=not (from_quote and has_main_attachment),
            )
            result = await self._file_handler.process(att)
            parts.append(
                self._format_attachment_block(
                    att,
                    workspace_path=workspace_path,
                    extracted=result.text,
                )
            )
            vision_parts.extend(result.vision_parts)

        enriched = "\n\n".join(parts) if parts else None
        return enriched, vision_parts

    @staticmethod
    def _all_attachments_lost(message: UnifiedMessage) -> bool:
        """本轮有附件、却一个都没暂存成功。

        stage_attachment 仅在成功时写 metadata.workspace_path。部分成功不算：
        只要有一份落了地，指针就是本轮的，可以照常注入。
        """
        if not message.attachments:
            return False
        return not any(
            att.metadata.get("workspace_path") for att in message.attachments
        )

    def _append_active_inbound_hint(
        self,
        text: str | None,
        session_id: str,
        *,
        this_turn_has_file: bool = False,
    ) -> str:
        """按需注入最近用户上传路径，供『这个文件』指代。

        - 本轮已有附件块：路径已在附件说明里，不再叠「当前用户文件」，避免重复误导。
        - 纯文本轮：仅当用户话术含文件指代（这个文件/该表格…）时才注入；
          普通提问（如「研究宏观周期…」）不注入，避免把无关旧 xlsx 绑进任务。
        """
        body = (text or "").strip()
        if this_turn_has_file:
            return body
        if not _user_refers_to_prior_file(body):
            return body
        active = get_active_inbound(session_id)
        if not active:
            return body
        hint = (
            f"当前用户文件（仅供本轮文件指代）: {active}\n"
            "说明: 用户话术提到了「这个文件」等指代，默认指此路径"
            "（位于 inbound/）。若问题主体与该文件无关，忽略此路径；"
            "不要因此主动分析该文件或 send_file。"
        )
        if not body:
            return hint
        if active in body and "当前用户文件" in body:
            return body
        return f"{body}\n\n{hint}"

    def _format_attachment_block(
        self,
        att: Attachment,
        *,
        workspace_path: str | None,
        extracted: str | None,
    ) -> str:
        name = att.filename or "未命名"
        lines = [f"[附件 {att.type.value}: {name}]"]
        lines.append("角色: 用户上传 (inbound)")
        size = att.size_bytes
        if size is None and att.local_path:
            try:
                size = Path(att.local_path).stat().st_size
            except OSError:
                size = None
        if size is not None:
            lines.append(f"大小: {size} bytes")

        if workspace_path:
            lines.append(f"工作区路径: {workspace_path}")
            lines.append(ATTACHMENT_USAGE_HINT)
        else:
            reason = str(att.metadata.get("download_error") or "缺少可用 local_path")
            lines.append(f"⚠️ 未取到文件内容：{reason}")
            lines.append(ATTACHMENT_LOST_HINT)

        # 图片：保留 FileHandler 提示（多模态/OCR），不灌长文
        if att.type == AttachmentType.IMAGE:
            if extracted:
                lines.append(extracted.strip())
            return "\n".join(lines)

        # 文件：混合策略 — 短预览进提示词，长内容让 Agent 自读
        if extracted:
            text = extracted.strip()
            if len(text) <= self._preview_chars:
                lines.append("内容预览:")
                lines.append(text)
            else:
                lines.append(
                    f"内容预览（已截断至 {self._preview_chars} 字，完整请 read_file）:"
                )
                lines.append(text[: self._preview_chars] + "…")
        elif workspace_path:
            lines.append("未抽取到文本预览（可能是二进制）；请用 read_file 或专用工具处理。")

        return "\n".join(lines)
