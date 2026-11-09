"""微信群 @我 → MessageGateway → 经发送机回复。

发送机（wecom-sender）负责看屏幕读消息、按快捷键发消息；这里负责：
1. 收读到的 @我 消息。游标按发送机的消息库（inbox）落盘，只在一条消息处理完之后前进，重启从没处理完的
   那条接着收；回复带 ``request_id=wechat-at-<库>-<消息id>-reply``，重复处理也只发一次（发送机去重）。
2. 每条消息按「群 + 提问人」作为一个会话交给 Agent。引用了截图的，先取原图作为图片附件。
3. 答得慢先回一句「已收到，正在处理」，答完把「@提问人 回答」发回群里。

本服务是服务端：发送机主动连上来（``HubLink``，见 ``hub.py``），推消息、取回复，只需要它能出站访问本服务。

安全阀：发送机标了没把握（OCR 置信度低、原图没取到、认不出提问人）的不自动回复；太旧的消息不回；
每个群每分钟最多回几条，避免和别的机器人互相 @ 停不下来。回复模式分三档：
``off`` 只记录、``draft`` 跑 Agent 但不发到原群（可发到审核群）、``auto`` 直接回复。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import structlog

from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import Attachment, AttachmentType, UnifiedMessage
from csagent.channels.outbound_files import drain_outbound_files
from csagent.channels.workspace_uploads import session_inbound_dir
from csagent.common.keyed_lock import KeyedLocks
from csagent.infra.auth import MIN_SECRET_LENGTH
from csagent.infra.config import PROJECT_ROOT, Settings
from csagent.prompts.domain_text import EMPTY_INBOUND_GUIDE, HANDLE_FAILED_REPLY, HANDLE_TIMEOUT_REPLY
from csagent.react.loop import HARD_TIMEOUT_GRACE_SECONDS
from csagent.stream_events import AgentEventType
from csagent.wechat.hub import HubLink

logger = structlog.get_logger(__name__)

CHANNEL = "wechat"
# 发送机给的警告里只有这个不影响自动回复：某个群第一次被读，只交付了最近的消息
_HARMLESS_WARNINGS = frozenset({"first_read"})
_HANDLE_TIMEOUT_MARGIN_SECONDS = 60.0
_DEFAULT_CURSOR_PATH = PROJECT_ROOT / "data" / "wechat" / "cursor.json"
_CHALLENGE_NOTE = "（有一项操作需要人工确认，群里没法完成，已记录待处理。）"


@dataclass
class WeChatConfig:
    """configs/channels.yaml 里 wechat.config 的各项。"""

    # off：只记录收到的 @；draft：跑 Agent，回复不发到原群（配了 review_group 就发到那里）；auto：直接回复
    reply_mode: str = "draft"
    review_group: str = ""
    # 这么久还没答完，先回一句 ack_text（每次发送要六七秒，答得快就不发）
    ack_after_seconds: float = 10.0
    ack_text: str = "已收到，正在处理"
    # 读到时间早于这么多分钟的消息不回（通道停过一阵、积压的旧问题）
    max_age_minutes: float = 30.0
    # 每个群每分钟最多回复几条
    max_replies_per_minute: int = 3
    max_reply_chars: int = 3000
    poll_wait_seconds: float = 25.0
    max_concurrency: int = 4
    # 没有游标（首次接入）时：latest 从现在开始收，earliest 把发送机上的历史都处理一遍
    start_from: str = "latest"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> WeChatConfig:
        known = {k: v for k, v in (raw or {}).items() if k in cls.__dataclass_fields__}
        cfg = cls(**known)
        if cfg.reply_mode not in ("off", "draft", "auto"):
            raise ValueError(f"wechat.config.reply_mode 只能是 off / draft / auto，当前 {cfg.reply_mode!r}")
        return cfg


# ---------- 文本处理 ----------
_MD_LINK = re.compile(r"!?\[([^\]]*)\]\(([^)\s]+)\)")
_MD_EMPHASIS = re.compile(r"(\*\*|__)(.+?)\1")
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_MD_QUOTE = re.compile(r"^\s{0,3}>\s?", re.M)
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+", re.M)
_MD_RULE = re.compile(r"^\s*([-*_]\s*){3,}$", re.M)
# 表格的 |---|---| 分隔行连同换行一起去掉
_MD_TABLE_SEP = re.compile(r"^[ \t]*\|?([ \t]*:?-{3,}:?[ \t]*\|)+[ \t]*(:?-{3,}:?)?[ \t]*\|?[ \t]*\n?", re.M)


def markdown_to_plain(text: str) -> str:
    """微信不渲染 Markdown：去掉标记、保留内容，链接写成「文字（地址）」。"""
    out = (text or "").replace("\r\n", "\n")
    out = re.sub(r"^```[^\n]*\n?", "", out, flags=re.M)
    out = _MD_LINK.sub(lambda m: f"{m.group(1)}（{m.group(2)}）" if m.group(1) else m.group(2), out)
    out = _MD_EMPHASIS.sub(r"\2", out)
    out = _MD_CODE.sub(r"\1", out)
    out = _MD_HEADING.sub("", out)
    out = _MD_QUOTE.sub("", out)
    out = _MD_TABLE_SEP.sub("", out)
    out = _MD_RULE.sub("", out)
    out = _MD_BULLET.sub(r"\1• ", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def strip_mention(text: str, name: str) -> str:
    """去掉正文里的「@我的名字」，剩下的才是问题。"""
    if name:
        text = re.sub(r"[@＠]\s*" + re.escape(name), " ", text or "")
    return re.sub(r"\s+", " ", text or "").strip("  　,，:：")


def compose_reply(sender: str, answer: str, *, limit: int) -> str:
    """群里的回复：开头写「@提问人」（普通文字，不会真的提醒到人）。"""
    body = markdown_to_plain(answer) or "抱歉，暂时没有生成回复。"
    if len(body) > limit:
        body = body[:limit].rstrip() + "……（内容较长，已截断）"
    return f"@{sender} {body}" if sender else body


def _parse_time(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def image_type(data: bytes) -> tuple[str, str]:
    """按文件头认原图格式 → (扩展名, MIME)。发送机原样上传：手机发的多是 JPEG，电脑截图是 PNG。

    交给模型的 data URL 带着 MIME，和内容对不上有的接口会拒收。认不出的按 PNG。
    """
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif", "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return ".png", "image/png"


# ---------- 发送机接入 ----------
def make_hub(settings: Settings) -> HubLink:
    """发送机靠 WECHAT_LINK_TOKEN 连本服务。

    :raises ValueError: 没配或太短
    """
    token = settings.wechat_link_token.strip()
    if not token:
        raise ValueError("没有配置 WECHAT_LINK_TOKEN：发送机靠它连本服务，发送机 uplink.token 填同一个")
    if len(token) < MIN_SECRET_LENGTH:
        raise ValueError(f"WECHAT_LINK_TOKEN 至少 {MIN_SECRET_LENGTH} 个字符：本服务在公网，令牌太短能被挨个试出来")
    return HubLink(token)


# ---------- 桥 ----------
class WeChatAtBridge:
    def __init__(
        self,
        *,
        settings: Settings,
        message_gateway: MessageGateway,
        config: WeChatConfig,
        link: HubLink | None = None,
        cursor_path: Path | None = None,
    ) -> None:
        self._settings = settings
        self._gateway = message_gateway
        self._cfg = config
        self._link = link or make_hub(settings)
        self._cursor_path = cursor_path or _DEFAULT_CURSOR_PATH
        overall = float(getattr(settings, "react_overall_timeout_seconds", 0) or 0)
        self._handle_timeout: float | None = (
            overall + HARD_TIMEOUT_GRACE_SECONDS + _HANDLE_TIMEOUT_MARGIN_SECONDS if overall > 0 else None
        )
        self._sem = asyncio.Semaphore(max(1, config.max_concurrency))
        self._session_locks = KeyedLocks()
        self._task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()
        # 已拉到、按 id 排好的消息；处理完的从队头弹出时游标才前进
        self._pending: deque[int] = deque()
        self._done: set[int] = set()
        # 游标属于哪个消息库（发送机的 inbox 标识）。库重建后 id 从 1 重来，游标要跟着换
        self._inbox: str | None = None
        self._cursor = 0
        self._fetched = 0
        # 每换一次库加一：旧库还在处理的消息做完了也不能挪新库的游标
        self._generation = 0
        self._replies: dict[str, deque[float]] = {}
        self._connected = False
        self.last_error = ""

    # ---------- 生命周期 ----------
    @property
    def link(self) -> HubLink:
        return self._link

    async def start(self) -> None:
        bind = getattr(self._link, "bind", None)
        if bind is not None:
            bind(self.cursor_for)
        self._task = asyncio.create_task(self._run(), name="wechat-at-pull")

    async def stop(self) -> None:
        for task in [self._task, *self._tasks]:
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in [self._task, *self._tasks] if t is not None), return_exceptions=True)
        self._tasks.clear()
        await self._link.close()

    @property
    def status(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "inbox": self._inbox,
            "cursor": self._cursor,
            "inflight": len(self._pending),
            "reply_mode": self._cfg.reply_mode,
            "link": self._link.status,
            "last_error": self.last_error,
        }

    # ---------- 拉取 ----------
    async def _run(self) -> None:
        backoff = 1.0
        while True:
            try:
                # 消息库和游标在发送机 hello 时已经对好（cursor_for）
                messages, _, _ = await self._link.pull(self._fetched, wait=self._cfg.poll_wait_seconds)
                self._connected = True
                backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 发送机重启、断网都只是等一会儿再连
                self._connected = False
                self.last_error = str(exc)
                logger.warning("wechat_pull_failed", error=str(exc), retry_in=backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            for msg in sorted(messages, key=lambda m: int(m.get("id") or 0)):
                mid = int(msg.get("id") or 0)
                if mid <= self._fetched:
                    continue
                self._fetched = mid
                self._pending.append(mid)
                task = asyncio.create_task(self._process(msg, self._generation), name=f"wechat-at-{mid}")
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)

    def cursor_for(self, inbox: str, last_id: int) -> int:
        """发送机（重新）接上时：这个消息库从哪条之后开始收。

        同一个库、本进程里已经在收：返回处理完的位置（在途的重推过来会被忽略）。否则读落盘的游标；
        换了库或第一次接入，按 start_from 从发送机最新的 id（不补历史）或从头开始。
        """
        if inbox == self._inbox and self._fetched:
            return self._cursor
        saved = self._load_cursor()
        if saved is not None and saved[0] == inbox:
            cursor = min(saved[1], last_id)
        else:
            cursor = last_id if self._cfg.start_from == "latest" else 0
            logger.info("wechat_cursor_initialized", inbox=inbox, cursor=cursor, start_from=self._cfg.start_from)
        if inbox != self._inbox:
            self._generation += 1
            self._pending.clear()
            self._done.clear()
        self._inbox = inbox
        self._cursor = self._fetched = cursor
        self._save_cursor()
        return cursor

    def _load_cursor(self) -> tuple[str, int] | None:
        try:
            data = json.loads(self._cursor_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not data.get("inbox"):
            return None  # 老格式（按发送机地址记的），对不上库，按首次接入处理
        return str(data["inbox"]), int(data.get("cursor") or 0)

    def _save_cursor(self) -> None:
        try:
            self._cursor_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._cursor_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"inbox": self._inbox, "cursor": self._cursor}), encoding="utf-8")
            tmp.replace(self._cursor_path)
        except OSError as exc:
            logger.warning("wechat_cursor_save_failed", error=str(exc))

    def _complete(self, mid: int, generation: int) -> None:
        if generation != self._generation:
            return  # 换库之前拉到的消息
        self._done.add(mid)
        moved = False
        while self._pending and self._pending[0] in self._done:
            self._cursor = self._pending.popleft()
            self._done.discard(self._cursor)
            moved = True
        if moved:
            self._save_cursor()

    # ---------- 处理一条 ----------
    async def _process(self, msg: dict[str, Any], generation: int) -> None:
        mid = int(msg.get("id") or 0)
        try:
            async with self._sem:
                await self._handle(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 一条出错不影响后面的
            logger.exception("wechat_handle_failed", message_id=mid, error=str(exc))
        finally:
            self._complete(mid, generation)

    async def _handle(self, msg: dict[str, Any]) -> None:
        mid = int(msg["id"])
        chat = str(msg.get("chat") or "")
        sender = str(msg.get("sender") or "")
        question = strip_mention(str(msg.get("text") or ""), str(msg.get("at") or ""))
        warnings = [w for w in msg.get("warnings") or [] if w not in _HARMLESS_WARNINGS]
        logger.info(
            "wechat_at_received",
            message_id=mid,
            chat=chat,
            sender=sender,
            question=question[:200],
            quote=(msg.get("quote") or {}).get("type"),
            warnings=msg.get("warnings") or [],
        )
        if self._cfg.reply_mode == "off":
            return
        skip = self._skip_reason(msg, chat, sender, warnings)
        if skip:
            logger.warning("wechat_at_skipped", message_id=mid, chat=chat, sender=sender, reason=skip)
            return

        # 按群一个会话：记忆、工作区整群共用，群友之间能接着彼此的话问
        session_id = f"wechat:group:{chat}"
        quote = msg.get("quote") or {}
        quote_text = str(quote.get("text") or "") if quote.get("type") == "text" else None
        # 原图等轮到这条才放进会话目录、产出的文件也在轮到时取走：同群连着问，前一条还在答时
        # Agent 会翻目录，把后一条引用的图当成这条的
        async with self._session_locks.hold(session_id):
            attachments = await self._quote_attachments(msg, session_id)
            if not question and not attachments and not quote_text:
                await self._reply(chat, mid, compose_reply(sender, EMPTY_INBOUND_GUIDE, limit=self._cfg.max_reply_chars))
                return
            message = UnifiedMessage(
                channel=CHANNEL,
                session_id=session_id,
                text=question,
                attachments=attachments,
                quote_text=quote_text,
                metadata={"speaker": sender},
                principal={"user_id": sender, "auth_method": CHANNEL, "chat_type": "group", "chat_id": chat},
            )
            answer, challenged = await self._ask(message, chat, sender, mid)
            files = self._outbound_files(session_id)
        if challenged:
            answer = f"{answer.rstrip()}\n\n{_CHALLENGE_NOTE}" if answer.strip() else _CHALLENGE_NOTE
        reply = compose_reply(sender, answer, limit=self._cfg.max_reply_chars)
        await self._reply(chat, mid, reply, files=files, question=question, sender=sender)

    def _skip_reason(self, msg: dict[str, Any], chat: str, sender: str, warnings: list[str]) -> str:
        if not chat or not sender:
            return "认不出群或提问人"
        if warnings:
            return f"发送机识别没把握：{','.join(warnings)}"
        read_at = _parse_time(str(msg.get("read_at") or ""))
        if read_at is not None:
            age = (datetime.now(read_at.tzinfo) - read_at).total_seconds()
            if age > self._cfg.max_age_minutes * 60:
                return f"消息太旧（{int(age // 60)} 分钟前读到）"
        recent = self._replies.setdefault(chat, deque())
        now = time.monotonic()
        while recent and now - recent[0] > 60:
            recent.popleft()
        if len(recent) >= self._cfg.max_replies_per_minute:
            return f"该群一分钟内已回复 {len(recent)} 条，限速"
        recent.append(now)
        return ""

    async def _quote_attachments(self, msg: dict[str, Any], session_id: str) -> list[Attachment]:
        quote = msg.get("quote") or {}
        url = quote.get("image_url")
        if quote.get("type") != "image" or not url:
            return []
        try:
            data = await self._link.download(str(url))
        except LookupError as exc:
            logger.warning("wechat_quote_image_failed", message_id=msg["id"], error=str(exc))
            return [
                Attachment(
                    type=AttachmentType.IMAGE,
                    filename=f"引用截图_{msg['id']}.png",
                    metadata={"download_error": str(exc), "from_quote": True},
                )
            ]
        ext, mime = image_type(data)
        filename = f"引用截图_{msg['id']}{ext}"
        dest = session_inbound_dir(session_id)
        dest.mkdir(parents=True, exist_ok=True)
        path = dest / filename
        path.write_bytes(data)
        return [
            Attachment(
                type=AttachmentType.IMAGE,
                filename=filename,
                mime_type=mime,
                size_bytes=len(data),
                local_path=str(path),
                metadata={"from_quote": True},
            )
        ]

    async def _ask(self, message: UnifiedMessage, chat: str, sender: str, mid: int) -> tuple[str, bool]:
        """跑一轮 Agent，收集终答。超过 ack_after_seconds 还没答完，先回一句「已收到」。"""
        answer = ""
        challenged = False
        ack: asyncio.Task | None = None
        ack_sending = False

        async def ack_later() -> None:
            nonlocal ack_sending
            await asyncio.sleep(self._cfg.ack_after_seconds)
            ack_sending = True
            text = compose_reply(sender, self._cfg.ack_text, limit=self._cfg.max_reply_chars)
            await self._reply(chat, mid, text, request_suffix="ack", wait=False)

        if self._cfg.reply_mode == "auto" and self._cfg.ack_after_seconds > 0:
            ack = asyncio.create_task(ack_later())
        try:
            async with asyncio.timeout(self._handle_timeout):
                async for event in self._gateway.handle(message):
                    if event.event_type == AgentEventType.DELTA:
                        answer += event.content or ""
                    elif event.event_type == AgentEventType.CHALLENGE:
                        challenged = True
        except TimeoutError:
            logger.error("wechat_handle_timeout", message_id=mid)
            answer = answer or HANDLE_TIMEOUT_REPLY
        except Exception as exc:  # noqa: BLE001
            logger.exception("wechat_agent_failed", message_id=mid, error=str(exc))
            answer = HANDLE_FAILED_REPLY.format(error=exc)
        finally:
            if ack is not None:
                if not ack_sending:
                    ack.cancel()
                # 已经在发「已收到」：等它进了发送队列再发正式回复，保证先后
                await asyncio.gather(ack, return_exceptions=True)
        return answer, challenged

    def _outbound_files(self, session_id: str) -> list[tuple[str, bytes]]:
        files = []
        for item in drain_outbound_files(session_id):
            try:
                files.append((item.filename, Path(item.path).read_bytes()))
            except OSError as exc:
                logger.warning("wechat_outbound_missing", path=item.path, error=str(exc))
        return files

    def _request_id(self, mid: int, suffix: str) -> str:
        """回复的幂等键：带上消息库标识，库重建后 id 从 1 重来也不会撞上以前的回复。"""
        inbox = "".join(ch for ch in (self._inbox or "") if ch.isalnum())[:8] or "x"
        return f"wechat-at-{inbox}-{mid}-{suffix}"

    async def _reply(
        self,
        chat: str,
        mid: int,
        text: str,
        *,
        files: list[tuple[str, bytes]] | None = None,
        request_suffix: str = "reply",
        wait: bool = True,
        question: str = "",
        sender: str = "",
    ) -> None:
        files = files or []
        items: list[dict[str, Any]] = [{"type": "text", "text": text}]
        items += [{"type": "file", "file": i} for i in range(len(files))]
        target = chat
        if self._cfg.reply_mode != "auto":
            if not self._cfg.review_group or request_suffix == "ack":
                logger.info("wechat_reply_draft", message_id=mid, chat=chat, reply=text[:500], files=len(files))
                return
            target = self._cfg.review_group
            items[0] = {"type": "text", "text": f"【待审核】{chat} / {sender}：{question}\n——\n{text}"}
        # 进发件箱等发送机来取；发送机不在线时任务过期，这里拿到 ok=false
        result = await self._link.send(target, items, files=files, request_id=self._request_id(mid, request_suffix), wait=wait)
        if result.get("ok") is False:
            logger.error("wechat_reply_failed", message_id=mid, chat=target, error=result.get("error"))
            return
        logger.info(
            "wechat_replied",
            message_id=mid,
            chat=target,
            kind=request_suffix,
            pending=bool(result.get("pending")),
            dry_run=bool(result.get("dry_run")),
            chars=len(text),
            files=len(files),
        )
