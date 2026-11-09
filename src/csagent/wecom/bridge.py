"""企微消息 → MessageGateway 流式桥接（文本 / 图片 / 文件 / 图文混排）。"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from csagent.channels.approval_flow import (
    ClickOutcome,
    click_notice,
    resolve_click,
    resume_transcript,
    run_approved,
)
from csagent.channels.message_gateway import MessageGateway
from csagent.common.keyed_lock import KeyedLocks
from csagent.channels.models import Attachment, UnifiedMessage
from csagent.channels.outbound_files import OutboundFile, drain_outbound_files
from csagent.infra.config import PROJECT_ROOT, Settings
from csagent.react.loop import HARD_TIMEOUT_GRACE_SECONDS
from csagent.prompts.domain_text import (
    EMPTY_INBOUND_GUIDE,
    HANDLE_FAILED_REPLY,
    HANDLE_TIMEOUT_REPLY,
    TRANSPORT_FAILURE_REPLY,
)
from csagent.agent import Agent as MainAgent
from csagent.stream_events import AgentEventType
from csagent.user_progress import is_todo_all_done, is_todo_checklist
from csagent.tools.approvals import ApprovalStore, PendingApproval, approval_chain_var
from csagent.wecom.cards import (
    CANCEL_ACTION,
    CONFIRM_ACTION,
    build_confirmation_card,
    build_updated_card,
    parse_template_card_event,
)
from csagent.wecom.client import create_ws_client
from csagent.wecom.media import (
    download_files_as_attachments,
    download_images_as_attachments,
    extract_plaintext_from_body,
    iter_file_sections,
    iter_image_sections,
    upload_dir_for_session,
)
from csagent.wecom.media_upload import media_message_body, upload_media

logger = structlog.get_logger(__name__)

# 去掉群聊 @机器人 前缀；同时清洗误入正文的 <@userid>（流式接口不会渲染）
_AT_MENTION_RE = re.compile(r"(?:@\S+|<@[^\s>]+>)\s*")
# 企微一轮处理的兜底超时，跟网页一致：由 ReAct 的软截止（overall_timeout_seconds）
# 负责收口，这里只在 ReAct 硬截止（软截止 + 宽限）之后再留一点余量给记忆落库与收尾，
# 正常情况下永远不会先于 ReAct 触发
_HANDLE_TIMEOUT_MARGIN_SECONDS = 60.0
# 企微流式消息从首次推送起 10 分钟内必须 finish，否则被自动结束、之后的刷新全部失败。
# 到这个时刻任务还没做完：先把流式消息收尾（留 40 秒给回执），任务照常跑完，
# 结果用主动消息单独发出
_STREAM_WINDOW_SECONDS = 560.0
_STREAM_HANDOFF_NOTICE = (
    "（任务较长，已到单条消息的时长上限。仍在继续处理，完成后会单独发送完整结果。）"
)
_SUPPORTED_MSGTYPES = frozenset({"text", "image", "mixed", "voice", "file"})


def _strip_at(text: str) -> str:
    return _AT_MENTION_RE.sub("", text or "").strip()


def _public_base_url() -> str:
    from csagent.api.router_files import _public_base_url as _from_settings

    return _from_settings()


def _outbound_https_footer(pending: list[OutboundFile], session_id: str) -> str:
    from csagent.api.router_files import build_download_url

    blocks = []
    for item in pending:
        url = build_download_url(item.workspace_path, session_id=session_id)
        name = (
            (item.filename or "下载文件")
            .replace("[", "［")
            .replace("]", "］")
            .replace("\n", " ")
        )
        blocks.append(f"[{name}]({url})")
    return "\n".join(blocks)


def format_round_elapsed(elapsed_seconds: float) -> str:
    """网页 timing-badge 同口径的整轮墙钟：四舍五入到秒（0.5 进位），供调试对照。"""
    seconds = max(0, int(float(elapsed_seconds) + 0.5))
    return f"【本轮共耗时】：{seconds}秒"


def append_round_elapsed(
    answer: str, elapsed_seconds: float, *, enabled: bool = True
) -> str:
    """把耗时戳追加在终答（含出站链接脚注）之后。

    enabled=False（配置 WECOM_APPEND_ROUND_ELAPSED 未开）时原样返回，不拼接。
    空正文且开启时仍保留收尾兜底句，避免气泡被收成空白。
    """
    if not enabled:
        return answer
    stamp = format_round_elapsed(elapsed_seconds)
    body = (answer or "").rstrip()
    if not body:
        return stamp
    return f"{body}\n\n{stamp}"


def _stream_bubble_content(*, answer: str, progress: str, finish: bool) -> str:
    """组装企微流式气泡正文：有回答优先；否则显示 PROGRESS；收尾不留进度占位。"""
    if answer:
        return answer
    if finish:
        return "抱歉，暂时没有生成回复。"
    return progress or "…"


def _compose_progress(*, checklist: str, status: str) -> str:
    """有待办清单时气泡只放清单。

    「正在…」每轮长短不一样，接在清单下面气泡高度会来回跳。
    还没列出待办时，才用那句进度。

    :param checklist: 最近一次待办清单；没有则为空
    :param status: 最近一句工具进度；清单存在时忽略
    :returns: 气泡正文
    """
    checklist = (checklist or "").strip()
    if checklist:
        return checklist
    return (status or "").strip()


def _extract_quote_text(quote: Any) -> str:
    """从企微 body.quote 抽出可供 Agent 阅读的文本。"""
    if not isinstance(quote, dict):
        return ""
    msgtype = str(quote.get("msgtype") or "").strip().lower()

    if msgtype == "text":
        return str((quote.get("text") or {}).get("content") or "").strip()

    if msgtype == "voice":
        return str((quote.get("voice") or {}).get("content") or "").strip()

    if msgtype == "mixed":
        parts: list[str] = []
        items = (quote.get("mixed") or {}).get("msg_item") or []
        if isinstance(items, list):
            for item in items:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("msgtype") or "").strip().lower()
                if item_type == "text":
                    content = str((item.get("text") or {}).get("content") or "").strip()
                    if content:
                        parts.append(_strip_at(content) or content)
                elif item_type == "image":
                    parts.append("[图片]")
        return "\n".join(parts).strip()

    if msgtype == "image":
        return "[图片]"
    if msgtype == "file":
        return "[文件]"
    if msgtype == "video":
        return "[视频]"
    return ""


def _compose_user_text(*, user_text: str, quote: Any) -> str:
    """把用户正文与引用消息拼成送给 Agent 的文本（引用图另走 Attachment）。"""
    user_text = (user_text or "").strip()
    quote_text = _extract_quote_text(quote)
    if not quote_text:
        return user_text
    if not user_text:
        return (
            "用户引用了以下消息，请结合引用内容回答：\n"
            f"{quote_text}"
        )
    return (
        f"【引用消息】\n{quote_text}\n\n"
        f"【用户问题】\n{user_text}"
    )


_FRAME_DUMP_DIR = PROJECT_ROOT / "data" / "wecom_frames"


def _dump_frame(frame: dict[str, Any]) -> None:
    """把入站原始帧整体落盘（WECOM_FRAME_DUMP=true 时）。

    排查群聊 quote / 文件段真实结构用：企微群与单聊的帧形状不一致，
    单测里的 quote 结构均为手写假数据，需要真实样本对齐。
    """
    try:
        body = frame.get("body") or {}
        msgtype = str(body.get("msgtype") or "none")
        chattype = str(body.get("chattype") or "single")
        stamp = datetime.now().strftime("%H%M%S-%f")[:13]
        _FRAME_DUMP_DIR.mkdir(parents=True, exist_ok=True)
        path = _FRAME_DUMP_DIR / f"{stamp}_{chattype}_{msgtype}.json"
        path.write_text(
            json.dumps(frame, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("wecom_frame_dumped", path=str(path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("wecom_frame_dump_failed", error=str(exc))


def _session_id_from_body(body: dict[str, Any]) -> tuple[str, str, bool]:
    """返回 (session_id, from_user, is_group)。

    群聊按群一个会话（``wecom:group:{chatid}``）：记忆、工作区整群共用，A 问到一半
    B 接着追问也接得上。谁说的话由 ``metadata["speaker"]`` 标进正文（见 MessageGateway）。
    """
    from_user = str((body.get("from") or {}).get("userid") or "unknown")
    chattype = str(body.get("chattype") or "single").lower()
    chatid = str(body.get("chatid") or "")
    is_group = chattype == "group"
    session_id = (
        f"wecom:group:{chatid}"
        if is_group and chatid
        else f"wecom:{from_user}"
    )
    return session_id, from_user, is_group


# 企微 markdown 消息正文上限 4096 字节，留点余量
_MARKDOWN_MAX_BYTES = 4000


def _split_markdown(text: str, limit: int = _MARKDOWN_MAX_BYTES) -> list[str]:
    """按行切成不超过 ``limit`` 字节的几段；单行超长时按字符硬切。"""
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate.encode("utf-8")) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line.encode("utf-8")) > limit:
            cut = len(line)
            while len(line[:cut].encode("utf-8")) > limit:
                cut = max(1, cut * limit // len(line[:cut].encode("utf-8")) - 1)
            chunks.append(line[:cut])
            line = line[cut:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _card_belongs_here(item: PendingApproval, click: dict[str, str]) -> bool:
    """企微卡片的归属校验。

    只认企微发起的操作：网页端的 uid 和企微 userid 是两套命名空间，撞名不能让一边批准
    另一边的操作。群里的卡片群成员都可以处理，但只能在同一个群里；单聊卡片只有本人
    收得到，别人点（被转发了）不认。
    """
    if item.channel != "wecom":
        ok = False
    elif item.chat_type == "group":
        ok = item.chat_id == click["chatid"]
    else:
        ok = item.user_id == click["operator"]
    if not ok:
        logger.warning(
            "wecom_card_scope_mismatch",
            task_id=item.task_id,
            operator=click["operator"],
            channel=item.channel,
        )
    return ok


def _mention(userid: str) -> str:
    """企微消息里称呼一个人：纯文字 ``@userid``。

    长连接主动推送的 markdown 不渲染 ``<@userid>``（实测原样显示），流式消息同样不渲染，
    所以只写纯文字，不会真的提醒到人。回调里只有 userid，拿不到姓名。
    """
    return f"@{userid}"


def _challenge_payload(content: str) -> dict[str, Any]:
    """解析 Agent 发出的待确认事件。坏 JSON 不当成卡片。"""
    try:
        data = json.loads(content or "")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class WeComAgentBridge:
    """企微智能机器人 → MessageGateway（支持文本与图片多模态）。"""

    def __init__(
        self,
        *,
        main_agent: MainAgent,
        settings: Settings,
        message_gateway: MessageGateway | None = None,
    ) -> None:
        self._main_agent = main_agent
        self._settings = settings
        self._message_gateway = message_gateway or MessageGateway(main_agent=main_agent)
        self._client: Any | None = None
        self._stream_interval = max(settings.wecom_stream_interval_ms, 50) / 1000.0
        # ReAct 没配总时长（0）时网页不设上限，这里同样不设（asyncio.timeout(None)）
        overall = float(getattr(settings, "react_overall_timeout_seconds", 0) or 0)
        self._handle_timeout: float | None = (
            overall + HARD_TIMEOUT_GRACE_SECONDS + _HANDLE_TIMEOUT_MARGIN_SECONDS
            if overall > 0
            else None
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._authenticated = False
        self._inflight = 0
        self._tasks: set[asyncio.Task[Any]] = set()
        # 批准后续跑按会话排队：普通消息仍可并行，续跑之间不交错写记忆。
        # 这只是为了记忆写入不交错，卡片的先后约束按任务链算，见 approvals
        self._resume_locks = KeyedLocks()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "connected": self._client is not None,
            "authenticated": self._authenticated,
            "inflight": self._inflight,
        }

    async def start(self) -> None:
        bot_id = self._settings.wecom_bot_id.strip()
        secret = self._settings.wecom_secret.strip()
        if not bot_id or not secret:
            raise RuntimeError("WECOM_ENABLED=true 时必须配置 WECOM_BOT_ID 与 WECOM_SECRET")

        self._loop = asyncio.get_running_loop()
        client = create_ws_client(bot_id=bot_id, secret=secret)

        client.on("authenticated", self._on_authenticated)
        client.on("disconnected", self._on_disconnected)
        client.on("reconnecting", self._on_reconnecting)
        client.on("error", self._on_error)
        client.on("message", self._on_message)
        client.on("event.enter_chat", self._on_enter_chat)
        client.on("event.template_card_event", self._on_template_card_event)

        await client.connect()
        self._client = client
        logger.info(
            "wecom_ws_connected",
            bot_id=bot_id,
            hint="同一 BotID 同时只能有 1 条长连接；多开进程会互相踢线导致收不到消息",
        )

    async def stop(self) -> None:
        self._authenticated = False
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        if self._client is None:
            return
        try:
            self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            logger.warning("wecom_disconnect_error", error=str(exc))
        self._client = None
        logger.info("wecom_ws_stopped")

    def _schedule(self, coro: Any) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            logger.error("wecom_schedule_failed", reason="event_loop_unavailable")
            return

        def _spawn() -> None:
            task = loop.create_task(coro)
            self._tasks.add(task)

            def _done(done: asyncio.Task[Any]) -> None:
                self._tasks.discard(done)
                if done.cancelled():
                    return
                exc = done.exception()
                if exc is not None:
                    logger.exception("wecom_task_failed", error=str(exc))

            task.add_done_callback(_done)

        loop.call_soon_threadsafe(_spawn)

    def _on_authenticated(self) -> None:
        self._authenticated = True
        logger.info("wecom_authenticated")

    def _on_disconnected(self, reason: Any = None) -> None:
        self._authenticated = False
        logger.warning(
            "wecom_disconnected",
            reason=str(reason) if reason else "",
            hint="若刚启动了第二个进程，通常是被新连接踢掉；请只保留一个运行实例",
        )

    def _on_reconnecting(self, attempt: Any = None) -> None:
        self._authenticated = False
        logger.warning("wecom_reconnecting", attempt=attempt)

    def _on_error(self, error: Any = None) -> None:
        logger.error("wecom_ws_error", error=str(error) if error else "")

    def _on_enter_chat(self, frame: dict[str, Any]) -> None:
        self._schedule(self._handle_enter_chat(frame))

    def _on_template_card_event(self, frame: dict[str, Any]) -> None:
        self._schedule(self._handle_template_card_event(frame))

    def _on_message(self, frame: dict[str, Any]) -> None:
        body = frame.get("body") or {}
        msgtype = str(body.get("msgtype") or "").strip().lower()
        cmd = str(frame.get("cmd") or "")
        # 在 msgtype 过滤之前落盘：需要知道群里被丢掉的帧长什么样
        if self._settings.wecom_frame_dump:
            _dump_frame(frame)
        logger.info(
            "wecom_frame_received",
            cmd=cmd,
            msgtype=msgtype,
            chattype=str(body.get("chattype") or "single"),
            authenticated=self._authenticated,
            inflight=self._inflight,
        )
        if msgtype in _SUPPORTED_MSGTYPES:
            self._schedule(self._handle_inbound_message(frame))
        elif msgtype and msgtype not in {"stream"}:
            logger.info("wecom_message_ignored", msgtype=msgtype)

    async def _handle_enter_chat(self, frame: dict[str, Any]) -> None:
        if self._client is None:
            return
        welcome = self._settings.wecom_welcome.strip()
        if not welcome:
            return
        try:
            await self._client.reply_welcome(
                frame,
                {"msgtype": "text", "text": {"content": welcome}},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("wecom_welcome_failed", error=str(exc))

    async def _handle_inbound_message(self, frame: dict[str, Any]) -> None:
        """Step 1 解析正文/图片 → Step 2 下载落盘 → Step 3 Gateway 流式回复。"""
        if self._client is None:
            return
        body = frame.get("body") or {}
        if not isinstance(body, dict):
            return

        session_id, from_user, is_group = _session_id_from_body(body)
        msgtype = str(body.get("msgtype") or "").strip().lower()

        # Step 1: 文本（含 mixed 内文字、voice 转写）
        user_text = extract_plaintext_from_body(body, strip_at=_strip_at)
        text = _compose_user_text(user_text=user_text, quote=body.get("quote"))

        # Step 2: 图片/文件下载落盘（主消息 + 引用；目录 data/uploads/{session}/inbound/）
        attachments: list[Attachment] = []
        image_sections = iter_image_sections(body)
        file_sections = iter_file_sections(body)
        if image_sections or file_sections:
            dest = upload_dir_for_session(session_id)
            if image_sections:
                attachments.extend(
                    await download_images_as_attachments(
                        self._client,
                        image_sections,
                        dest_dir=dest,
                    )
                )
            if file_sections:
                attachments.extend(
                    await download_files_as_attachments(
                        self._client,
                        file_sections,
                        dest_dir=dest,
                    )
                )

        if not text and not attachments:
            guide = EMPTY_INBOUND_GUIDE
            logger.info(
                "wecom_empty_inbound_guide_reply",
                userid=from_user,
                msgtype=msgtype,
            )
            try:
                await self._reply_final(
                    frame,
                    content=guide,
                    userid=from_user,
                    is_group=is_group,
                    stream_id=uuid.uuid4().hex,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("wecom_empty_guide_reply_failed", error=str(exc))
            return

        # 只发文件不打字的兜底指令统一由 MessageGateway 注入（_FILE_ONLY_INSTRUCTION），
        # 这里保持 text 为空，避免企微与 HTTP 两处口径不一致。

        logger.info(
            "wecom_message_received",
            userid=from_user,
            msgtype=msgtype,
            session_id=session_id,
            text_length=len(text),
            attachments=len(attachments),
            has_quote=bool(body.get("quote")),
        )

        group_chatid = str(body.get("chatid") or "") if is_group else ""
        await self._stream_via_gateway(
            frame,
            UnifiedMessage(
                channel="wecom",
                session_id=session_id,
                text=text,
                attachments=attachments,
                metadata={"speaker": from_user} if is_group else {},
                principal={
                    "user_id": from_user,
                    "auth_method": "wecom",
                    "chat_type": "group" if is_group else "single",
                    "chat_id": group_chatid,
                },
            ),
            userid=from_user,
            is_group=is_group,
            group_chatid=group_chatid,
        )

    async def _stream_via_gateway(
        self,
        frame: dict[str, Any],
        message: UnifiedMessage,
        *,
        userid: str,
        is_group: bool,
        group_chatid: str = "",
    ) -> None:
        """把 Gateway 流式事件刷到企微 reply_stream。

        企微流式消息从首次推送起 10 分钟内必须结束：任务超过 ``_STREAM_WINDOW_SECONDS``
        还没做完时先给这条消息收尾，任务照常跑到 ReAct 的时限，终答用主动消息发出。
        """
        stream_id = uuid.uuid4().hex
        answer = ""
        progress = ""
        # 清单出来之后不再往气泡里接「正在…」，避免高度跟着进度文案变。
        checklist = ""
        status_line = ""
        last_sent = ""
        last_flush = 0.0
        self._inflight += 1

        transport_degraded = False
        # 流式消息已收尾（finish 发过）后不能再刷；handed_off 表示是到时长上限提前收尾的，
        # 终答改用主动消息发送。所有 reply_stream 经 stream_lock 串行，收尾与刷新不交错
        stream_lock = asyncio.Lock()
        stream_closed = False
        handed_off = False
        window_closing = False

        async def flush(*, finish: bool = False) -> None:
            nonlocal last_sent, last_flush, stream_closed
            if self._client is None:
                return
            async with stream_lock:
                if stream_closed:
                    return
                content = _stream_bubble_content(
                    answer=answer, progress=progress, finish=finish
                )
                if not finish and content == last_sent:
                    return
                await self._client.reply_stream(
                    frame,
                    stream_id,
                    content,
                    finish=finish,
                )
                last_sent = content
                last_flush = time.monotonic()
                if finish:
                    stream_closed = True

        async def close_stream_window() -> None:
            """到流式时长上限仍没做完：先给这条消息收尾，告诉用户结果会单独发。"""
            nonlocal stream_closed, handed_off, window_closing
            await asyncio.sleep(_STREAM_WINDOW_SECONDS)
            window_closing = True
            async with stream_lock:
                if stream_closed or self._client is None:
                    return
                body = answer.rstrip() or progress or ""
                content = f"{body}\n\n{_STREAM_HANDOFF_NOTICE}" if body else _STREAM_HANDOFF_NOTICE
                handed_off = True
                stream_closed = True
                logger.warning(
                    "wecom_stream_window_handoff",
                    session_id=message.session_id,
                    answer_chars=len(answer),
                )
                try:
                    await self._client.reply_stream(frame, stream_id, content, finish=True)
                except Exception as exc:  # noqa: BLE001 - 收尾失败也照样改走主动消息
                    logger.warning("wecom_stream_handoff_failed", error=str(exc))

        async def try_flush() -> None:
            """中间帧尽力而为：它们只是进度气泡，送不到不该毁掉整轮回答。

            企微 SDK 的 reply_stream 会等服务端回执，5s 收不到就抛**内置**
            TimeoutError（aibot/ws.py `_on_reply_ack_timeout`）。以前这个异常一路
            冒到下面的 except，和本方法自己的 asyncio.timeout 撞成同一种处理，
            于是一次回执抖动就让用户收到「处理超时」——而 Agent 那时甚至还没
            发起第一次 LLM 调用（见 audit 2026-08-16 13:30/13:31）。
            """
            nonlocal transport_degraded
            try:
                await flush(finish=False)
            except TimeoutError as exc:
                transport_degraded = True
                logger.warning(
                    "wecom_stream_ack_timeout",
                    session_id=message.session_id,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                transport_degraded = True
                logger.warning(
                    "wecom_stream_flush_failed",
                    session_id=message.session_id,
                    error=str(exc),
                )

        timeout_cm: asyncio.Timeout | None = None
        started = time.monotonic()
        challenges: list[dict[str, Any]] = []
        # 计时从首次推送开始，与企微的 10 分钟口径一致
        window_task = asyncio.create_task(close_stream_window())
        try:
            await try_flush()
            async with asyncio.timeout(self._handle_timeout) as timeout_cm:
                async for event in self._message_gateway.handle(message):
                    if event.event_type == AgentEventType.PROGRESS:
                        if answer:
                            continue
                        text_progress = (event.content or "").strip()
                        if not text_progress:
                            continue
                        if is_todo_checklist(text_progress):
                            # 全部完成不先收成一行「正在…」。清单留在气泡里，
                            # 答案那一帧再整段换掉，高度只变一次。
                            if is_todo_all_done(text_progress):
                                continue
                            checklist = text_progress
                        elif checklist:
                            continue
                        else:
                            status_line = text_progress
                        merged = _compose_progress(checklist=checklist, status=status_line)
                        if not merged or merged == progress:
                            continue
                        progress = merged
                        await try_flush()
                        continue
                    if event.event_type == AgentEventType.CHALLENGE:
                        challenges.append(_challenge_payload(event.content))
                        continue
                    if event.event_type != AgentEventType.DELTA:
                        continue
                    answer += event.content or ""
                    now = time.monotonic()
                    if now - last_flush >= self._stream_interval:
                        await try_flush()
        except TimeoutError as exc:
            # Python 3.11 起 asyncio.TimeoutError 就是内置 TimeoutError，本方法的
            # 处理超时和任何下游库的超时会撞同一个类型。用 expired() 区分，别再把
            # 传输层故障当成「Agent 处理慢」报给用户。
            if timeout_cm is not None and timeout_cm.expired():
                logger.error("wecom_handle_timeout", session_id=message.session_id)
                if not answer:
                    answer = HANDLE_TIMEOUT_REPLY
            else:
                transport_degraded = True
                logger.error(
                    "wecom_transport_timeout",
                    session_id=message.session_id,
                    error=str(exc),
                )
                if not answer:
                    answer = TRANSPORT_FAILURE_REPLY
        except Exception as exc:  # noqa: BLE001
            logger.exception("wecom_handle_failed", error=str(exc))
            answer = HANDLE_FAILED_REPLY.format(error=exc)
        else:
            if transport_degraded:
                logger.info(
                    "wecom_stream_degraded_but_completed",
                    session_id=message.session_id,
                    answer_chars=len(answer),
                )
        finally:
            pending = drain_outbound_files(message.session_id)
            public = _public_base_url()
            if pending and public:
                footer = _outbound_https_footer(pending, message.session_id)
                answer = f"{answer.rstrip()}\n\n{footer}" if answer.strip() else footer
            # 与网页 timing-badge 一样量整轮墙钟；企微没有独立 UI，只能写进终答。
            # 是否拼接由 WECOM_APPEND_ROUND_ELAPSED 控制，默认不进对客气泡。
            finish_body = _stream_bubble_content(
                answer=answer, progress=progress, finish=True
            )
            answer = append_round_elapsed(
                finish_body,
                time.monotonic() - started,
                enabled=self._settings.wecom_append_round_elapsed,
            )
            # 正在收尾的不能取消（可能停在 reply_stream 中途），等它做完
            if window_closing:
                await asyncio.gather(window_task, return_exceptions=True)
            else:
                window_task.cancel()
                await asyncio.gather(window_task, return_exceptions=True)
            # 主动消息的目标：群里发群、单聊发本人
            push_target = (group_chatid if is_group and group_chatid else userid or "").strip()
            if handed_off:
                text = answer.strip()
                if is_group and userid and text:
                    # 群里并行的任务多，标明是谁的问题（纯文字，见 _mention）
                    text = f"{_mention(userid)} {text}"
                for chunk in _split_markdown(text):
                    await self._send_markdown(push_target, chunk)
            else:
                try:
                    await flush(finish=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("wecom_finish_reply_failed", error=str(exc))
            if challenges:
                if handed_off:
                    # 原消息已收尾，卡片不再挂在它下面，主动发到会话
                    await self._send_ready_cards(message.session_id, chatid=push_target)
                else:
                    await self._send_ready_cards(message.session_id, frame=frame)
            if pending and not public:
                try:
                    await self._flush_outbound_files(
                        session_id=message.session_id,
                        userid=userid,
                        is_group=is_group,
                        group_chatid=group_chatid,
                        pending=pending,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("wecom_outbound_files_failed", error=str(exc))
            self._inflight = max(0, self._inflight - 1)

    async def _flush_outbound_files(
        self,
        *,
        session_id: str,
        userid: str,
        is_group: bool = False,
        group_chatid: str = "",
        pending: list[OutboundFile] | None = None,
    ) -> None:
        """文字流结束后：上传并主动发送 Agent 排队的工作区文件。

        群消息时用群 chatid 发送（文件发到群里），个人消息用 userid。
        已配 PUBLIC_BASE_URL 时由流式气泡内嵌 HTTPS 链接，不再走这里。
        """
        if pending is None:
            pending = drain_outbound_files(session_id)
        if self._client is None:
            return
        if not pending:
            return

        # 群消息：用群 chatid；单聊：用发消息人的 userid
        target_id = (group_chatid if is_group and group_chatid else userid or "").strip()
        if not target_id:
            logger.warning(
                "wecom_outbound_no_target",
                session_id=session_id,
                is_group=is_group,
                count=len(pending),
            )
            return

        for item in pending:
            try:
                path = Path(item.path)
                if not path.is_file():
                    logger.warning(
                        "wecom_outbound_missing",
                        path=item.path,
                        workspace_path=item.workspace_path,
                    )
                    continue
                data = path.read_bytes()
                media_id = await upload_media(
                    self._client,
                    data,
                    filename=item.filename,
                    media_type=item.media_type,
                )
                body = media_message_body(item.media_type, media_id)
                await self._client.send_message(target_id, body)
                logger.info(
                    "wecom_outbound_file_sent",
                    target=target_id,
                    is_group=is_group,
                    workspace_path=item.workspace_path,
                    media_type=item.media_type,
                    size=len(data),
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "wecom_outbound_file_send_failed",
                    workspace_path=item.workspace_path,
                    error=str(exc),
                )

    async def _send_ready_cards(
        self,
        session_id: str,
        *,
        frame: dict[str, Any] | None = None,
        chatid: str = "",
    ) -> None:
        """发出本会话现在能点的确认卡片。文字流结束之后再发，避免插进半截回复里。

        企微发出的卡片事后改不了，所以同一任务里排在后面的卡片先不发，等前面那张
        处理完（点卡片的回调里会再调一次这里）再发，用户始终只看到能点的那一张。
        有原消息帧时回复在该消息下；点卡片 / 续跑时没有消息帧，主动发到 ``chatid``。
        """
        approvals = self._main_agent.approvals
        if self._client is None or approvals is None:
            return
        for item in approvals.take_ready_cards(session_id):
            task_id = item.task_id
            card = build_confirmation_card(
                task_id=task_id,
                summary=item.summary,
                tool_name=item.tool_name,
                reason=item.reason,
                from_user=item.user_id,
                chat_type=item.chat_type,
                queued=approvals.queued_behind(task_id),
                expires_at=item.expires_at,
            )
            try:
                if frame is not None:
                    await self._client.reply_template_card(frame, card)
                elif chatid:
                    await self._client.send_message(
                        chatid, {"msgtype": "template_card", "template_card": card}
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("wecom_challenge_card_failed", task_id=task_id, error=str(exc))

    async def _handle_template_card_event(self, frame: dict[str, Any]) -> None:
        """点卡片：先在几秒内把卡片置灰，再执行或作废，之后带着结果续跑原任务。

        认领 / 作废 / 执行的规则与网页共用（channels/approval_flow.py）。同一任务里排在
        后面的卡片要等这张续跑结束才发出（见 _send_ready_cards）。
        """
        if self._client is None:
            return
        click = parse_template_card_event(frame)
        approvals = self._main_agent.approvals
        if (
            not click["task_id"]
            or click["event_key"] not in (CONFIRM_ACTION, CANCEL_ACTION)
            or approvals is None
        ):
            return
        result = resolve_click(
            approvals,
            click["task_id"],
            approve=click["event_key"] == CONFIRM_ACTION,
            operator=click["operator"],
            belongs_here=lambda item: _card_belongs_here(item, click),
        )
        if result.outcome == ClickOutcome.FORBIDDEN:
            # 回调里拿不到点击人：无法记录是谁处理的，不处理
            logger.warning("wecom_card_click_without_operator", task_id=click["task_id"])
            return
        if (
            result.outcome not in (ClickOutcome.APPROVED, ClickOutcome.REJECTED)
            or result.pending is None
        ):
            await self._send_markdown(click["chatid"], click_notice(result, mention=_mention))
            return
        approved = result.outcome == ClickOutcome.APPROVED
        # 企微要求几秒内更新原卡片：先置灰，工具执行放在后面
        await self._update_card(frame, result.pending, approved=approved, operator=click["operator"])
        await self._finish_card(approvals, result.pending, click, approved=approved)

    async def _update_card(
        self, frame: dict[str, Any], pending: PendingApproval, *, approved: bool, operator: str
    ) -> None:
        """把点过的卡片就地换成「已批准 / 已驳回」。失败只告警：执行不能因此卡住。"""
        try:
            await self._client.update_template_card(
                frame,
                build_updated_card(
                    task_id=pending.task_id,
                    approved=approved,
                    operator=operator,
                    summary=pending.summary,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("wecom_card_update_failed", task_id=pending.task_id, error=str(exc))

    async def _finish_card(
        self,
        approvals: ApprovalStore,
        pending: PendingApproval,
        click: dict[str, str],
        *,
        approved: bool,
    ) -> None:
        """执行（批准时）→ 续跑原任务 → 放开并发出同一任务里排在后面的卡片。"""
        chatid = click["chatid"]
        try:
            if approved:
                run = await run_approved(self._main_agent, approvals, pending)
                if run.action is None:
                    await self._send_markdown(chatid, run.error)
                    return
                if not run.action.continuation:
                    # 执行了但结果没记上、续跑不了：至少把执行结果告诉用户，不能没有下文
                    await self._send_markdown(chatid, run.action.text)
                    return
            # 批准或拒绝都续跑一轮：带着执行结果（或「被拒绝」）把原任务做完。
            # 企微同一会话的多轮会并行：续跑结束前确认簿一直挡着同一任务链里后面的卡片
            # （见 approvals），续跑之间再按会话排队
            resumed = approvals.take_continuation(pending.session_id, pending.task_id)
            if resumed is not None:
                async with self._resume_locks.hold(pending.session_id):
                    await self._continue_after_approval(
                        resumed,
                        chatid=chatid,
                        operator=click["operator"],
                        is_group=click["chattype"] == "group",
                    )
        finally:
            # 这张处理完了（含失败）：放开并发出排在它后面的卡片
            approvals.settle(pending.task_id)
            await self._send_ready_cards(pending.session_id, chatid=chatid)

    async def _continue_after_approval(
        self, pending: PendingApproval, *, chatid: str, operator: str, is_group: bool
    ) -> None:
        """批准后续跑：执行结果作为新一轮的输入，让模型把原任务做完。

        卡片回调帧不能流式回复，答案、新卡片和文件都主动发到原会话。
        """
        message = UnifiedMessage(
            channel="wecom",
            session_id=pending.session_id,
            text=pending.continuation,
            metadata={"transcript_text": resume_transcript(pending)},
            principal={
                "user_id": pending.user_id,
                "auth_method": pending.auth_method or "wecom",
                "chat_type": pending.chat_type,
                "chat_id": pending.chat_id,
            },
        )
        answer = ""
        self._inflight += 1
        # 续跑里再出的卡片沿用原任务的链，排在它后面；和并行的其他任务互不挡
        chain_token = approval_chain_var.set(pending.chain)
        try:
            async with asyncio.timeout(self._handle_timeout):
                async for event in self._message_gateway.handle(message):
                    if event.event_type == AgentEventType.DELTA:
                        answer += event.content or ""
        except TimeoutError:
            logger.error("wecom_resume_timeout", session_id=pending.session_id)
            answer = answer or HANDLE_TIMEOUT_REPLY
        except Exception as exc:  # noqa: BLE001
            logger.exception("wecom_resume_failed", error=str(exc))
            answer = HANDLE_FAILED_REPLY.format(error=exc)
        finally:
            approval_chain_var.reset(chain_token)
            self._inflight = max(0, self._inflight - 1)

        outbound = drain_outbound_files(pending.session_id)
        public = _public_base_url()
        if outbound and public:
            footer = _outbound_https_footer(outbound, pending.session_id)
            answer = f"{answer.rstrip()}\n\n{footer}" if answer.strip() else footer
        for chunk in _split_markdown(answer.strip()):
            await self._send_markdown(chatid, chunk)
        # 续跑里新出的卡片由调用方在放开这张之后统一发（见 _send_ready_cards）
        if outbound and not public:
            try:
                await self._flush_outbound_files(
                    session_id=pending.session_id,
                    userid=operator,
                    is_group=is_group,
                    group_chatid=chatid if is_group else "",
                    pending=outbound,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("wecom_outbound_files_failed", error=str(exc))

    async def _send_markdown(self, chatid: str, content: str) -> None:
        if self._client is None or not chatid or not content:
            return
        body = {"msgtype": "markdown", "markdown": {"content": content}}
        try:
            # 主动消息：chatid 为群或单聊目标
            await self._client.send_message(chatid=chatid, body=body)
        except TypeError:
            try:
                await self._client.send_message(chatid, body)
            except Exception as exc:  # noqa: BLE001
                logger.warning("wecom_markdown_failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            logger.warning("wecom_markdown_failed", error=str(exc))

    async def _reply_final(
        self,
        frame: dict[str, Any],
        *,
        content: str,
        userid: str,
        is_group: bool,
        stream_id: str,
        use_stream_only: bool = False,
    ) -> None:
        """最终回复统一走流式收尾。"""
        _ = userid, is_group, use_stream_only
        if self._client is None:
            return
        await self._client.reply_stream(frame, stream_id, content, finish=True)
