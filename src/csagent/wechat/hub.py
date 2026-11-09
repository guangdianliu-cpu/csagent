"""发送机主动连本服务（模式 B）：发送机只需要出站访问本服务，本服务不用连得到发送机。

发送机（wecom-sender 的 uplink）连上来以后：

1. ``POST /api/wechat/link/hello`` ``{inbox, last_id}`` → ``{cursor, session}``：从哪条开始推。游标由桥记、
   处理完才前进；发送机重连、或发现 ``session`` 变了（本服务重启过）就重新 hello，从游标往后补推，不丢。
2. ``POST /api/wechat/link/messages``（multipart：``inbox``、``messages`` JSON、引用的原图 ``image_<id>``）：
   按 id 顺序推消息。id 不大于桥已经拿到的会被桥忽略，重复推没关系。
3. ``GET /api/wechat/link/outbox?wait=`` 长轮询取回复任务，``GET .../outbox/{job}/files/{n}`` 取附件，
   发完 ``POST .../outbox/{job}/result`` 回报。交出去的任务过 ``REDELIVER_AFTER_SECONDS`` 还没回报就再交一次：
   回复的 request_id 幂等，发送机按它去重。

回复任务只在内存里：本服务重启时没发出去的那条消息还在游标之后，会被重推、重答，不需要落库。
所有接口用 ``WECHAT_LINK_TOKEN`` 鉴权；本服务可能在公网，只走 HTTPS。
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from csagent.audit.security_logger import record_security_event

logger = structlog.get_logger(__name__)

PREFIX = "/api/wechat/link"
# 交给发送机的任务这么久没回报就再交一次（发送机串行发送，排队时一条要等好几条）
REDELIVER_AFTER_SECONDS = 180.0
# 这么久都没发出去（发送机不在线）就作废，告诉桥发送失败
JOB_TTL_SECONDS = 900.0
# 桥同步等发送结果的上限；超时返回 pending，任务照常留在发件箱里
SEND_TIMEOUT_SECONDS = 300.0
# 多久没有任何请求算发送机离线（它的长轮询不超过 60 秒一次）
ONLINE_WINDOW_SECONDS = 90.0
# 缓冲里最多攒这么多条还没被桥取走的消息、这么多张原图
_MAX_BUFFERED = 500
_MAX_IMAGES = 30
_MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_REF = "link://"


class _Hello(BaseModel):
    inbox: str = Field(..., min_length=1, max_length=64)
    last_id: int = Field(..., ge=0)
    instance: str = Field("", max_length=128)


@dataclass
class _Job:
    id: str
    group: str
    items: list[dict[str, Any]]
    files: list[tuple[str, bytes]]
    request_id: str
    created: float = field(default_factory=time.monotonic)
    delivered_at: float | None = None
    future: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "group": self.group,
            "items": self.items,
            "request_id": self.request_id,
            "files": [name for name, _ in self.files],
        }


class HubLink:
    """发送机连进来的那一端：桥用 ``bind`` / ``pull`` / ``download`` / ``send``，发送机经 ``router()`` 的接口进来。"""

    mode = "hub"

    def __init__(self, token: str) -> None:
        self._token = token.strip()
        # 每次进程启动换一个：发送机看到它变了就知道本服务重启过，要重新 hello 补推
        self.session = uuid.uuid4().hex[:12]
        self._on_hello: Callable[[str, int], int] | None = None
        self._inbox: str | None = None
        self._last_id = 0
        self._buffer: dict[int, dict[str, Any]] = {}
        self._images: OrderedDict[int, bytes] = OrderedDict()
        self._jobs: OrderedDict[str, _Job] = OrderedDict()
        self._msg_ready = asyncio.Event()
        self._job_ready = asyncio.Event()
        self._last_seen: float | None = None
        self._instance = ""

    # ---------- 给桥用 ----------
    def bind(self, on_hello: Callable[[str, int], int]) -> None:
        """发送机 hello 时问桥：这个消息库从哪条开始收（返回游标）。"""
        self._on_hello = on_hello

    async def pull(self, after: int, *, wait: float, limit: int = 20) -> tuple[list[dict[str, Any]], int, str | None]:
        """id > after 的消息；没有时最多等 wait 秒。

        :returns: (消息, 下次的 after, 消息库标识)；发送机还没 hello 过时标识为 None
        """
        deadline = time.monotonic() + wait
        while True:
            ready = sorted((mid for mid in self._buffer if mid > after))[:limit]
            if ready:
                msgs = [self._buffer.pop(mid) for mid in ready]
                for mid in [m for m in self._buffer if m <= after]:
                    self._buffer.pop(mid, None)  # 桥已经拿过的（重推的副本）
                return msgs, ready[-1], self._inbox
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return [], after, self._inbox
            self._msg_ready.clear()
            try:
                await asyncio.wait_for(self._msg_ready.wait(), remaining)
            except TimeoutError:
                pass

    async def download(self, ref: str) -> bytes:
        """取消息里引用的原图（随消息推上来的，取一次就丢）。

        :raises LookupError: 没有这张图
        """
        try:
            mid = int(ref.removeprefix(IMAGE_REF))
        except ValueError as exc:
            raise LookupError(f"不认识的图片引用: {ref}") from exc
        data = self._images.pop(mid, None)
        if data is None:
            raise LookupError(f"消息 {mid} 的原图不在了（本服务重启过或已取过）")
        return data

    async def send(
        self,
        group: str,
        items: list[dict[str, Any]],
        *,
        files: list[tuple[str, bytes]] | None = None,
        request_id: str,
        wait: bool = True,
    ) -> dict[str, Any]:
        """放进发件箱等发送机来取。同一 request_id 还没发完时复用原任务。

        :returns: 发送机回报的结果；不等或等超时返回 ``{"pending": true, "job_id": ...}``
        """
        self._expire_jobs()
        job = next((j for j in self._jobs.values() if j.request_id == request_id), None)
        if job is None:
            job = _Job(id=uuid.uuid4().hex[:16], group=group, items=items, files=list(files or []), request_id=request_id)
            self._jobs[job.id] = job
            self._job_ready.set()
            logger.info("wechat_link_job_queued", job_id=job.id, group=group, request_id=request_id)
        if not wait:
            return {"pending": True, "job_id": job.id}
        try:
            return dict(await asyncio.wait_for(asyncio.shield(job.future), SEND_TIMEOUT_SECONDS))
        except TimeoutError:
            return {"pending": True, "job_id": job.id}

    async def close(self) -> None:
        for job in self._jobs.values():
            if not job.future.done():
                job.future.set_result({"ok": False, "error": "本服务正在停止"})
        self._jobs.clear()

    @property
    def status(self) -> dict[str, Any]:
        seen = self._last_seen
        return {
            "mode": self.mode,
            "online": seen is not None and time.monotonic() - seen <= ONLINE_WINDOW_SECONDS,
            "instance": self._instance,
            "inbox": self._inbox,
            "outbox": len(self._jobs),
            "buffered": len(self._buffer),
        }

    # ---------- 发件箱 ----------
    def _expire_jobs(self) -> None:
        now = time.monotonic()
        for job_id, job in list(self._jobs.items()):
            if now - job.created > JOB_TTL_SECONDS:
                self._jobs.pop(job_id, None)
                if not job.future.done():
                    job.future.set_result({"ok": False, "error": "发送机一直没来取或没回报结果（可能不在线），回复作废"})
                logger.error("wechat_link_job_expired", job_id=job_id, group=job.group, delivered=job.delivered_at is not None)

    def _take_jobs(self, limit: int = 10) -> list[_Job]:
        now = time.monotonic()
        out = []
        for job in self._jobs.values():
            if job.delivered_at is None or now - job.delivered_at >= REDELIVER_AFTER_SECONDS:
                if job.delivered_at is not None:
                    logger.warning("wechat_link_job_redelivered", job_id=job.id, request_id=job.request_id)
                job.delivered_at = now
                out.append(job)
                if len(out) >= limit:
                    break
        return out

    # ---------- HTTP ----------
    def router(self) -> APIRouter:
        router = APIRouter(prefix=PREFIX, tags=["wechat-link"], dependencies=[Depends(self._auth)])

        @router.post("/hello")
        async def hello(body: _Hello) -> dict[str, Any]:
            self._touch(body.instance)
            if self._on_hello is None:
                raise HTTPException(503, "微信通道还没准备好，稍后重试")
            if body.inbox != self._inbox:
                # 换了库（发送机删库重建、或换了一台）：旧缓冲作废
                self._buffer.clear()
                self._images.clear()
            cursor = self._on_hello(body.inbox, body.last_id)
            self._inbox, self._last_id = body.inbox, body.last_id
            logger.info("wechat_link_hello", inbox=body.inbox, last_id=body.last_id, cursor=cursor, instance=body.instance)
            return {"ok": True, "cursor": cursor, "session": self.session}

        @router.post("/messages")
        async def messages(request: Request) -> JSONResponse:
            self._touch()
            form = await request.form(max_files=_MAX_IMAGES + 20)
            if form.get("inbox") != self._inbox:
                return JSONResponse({"ok": False, "error": "inbox 和 hello 时不一致，请重新 hello"}, status_code=409)
            try:
                batch = json.loads(str(form.get("messages") or "[]"))
            except ValueError as exc:
                raise HTTPException(422, "messages 不是合法的 JSON") from exc
            if not isinstance(batch, list):
                raise HTTPException(422, "messages 需为 JSON 数组")
            if len(self._buffer) + len(batch) > _MAX_BUFFERED:
                raise HTTPException(503, "积压的消息太多，稍后重推")
            accepted: list[int] = []
            for msg in batch:
                if not isinstance(msg, dict) or not isinstance(msg.get("id"), int):
                    raise HTTPException(422, "每条消息要有整数 id")
                mid = msg["id"]
                upload = form.get(f"image_{mid}")
                if upload is not None and hasattr(upload, "read"):
                    data = await upload.read()
                    if len(data) > _MAX_IMAGE_BYTES:
                        raise HTTPException(413, f"消息 {mid} 的原图超过 {_MAX_IMAGE_BYTES // 1024 // 1024}MB")
                    self._images[mid] = data
                    while len(self._images) > _MAX_IMAGES:
                        self._images.popitem(last=False)
                    if isinstance(msg.get("quote"), dict):
                        msg["quote"]["image_url"] = f"{IMAGE_REF}{mid}"
                self._buffer[mid] = msg
                accepted.append(mid)
            if accepted:
                self._msg_ready.set()
            return JSONResponse({"ok": True, "accepted": accepted, "session": self.session})

        @router.get("/outbox")
        async def outbox(wait: Annotated[float, Query(ge=0, le=60)] = 25) -> dict[str, Any]:
            self._touch()
            deadline = time.monotonic() + wait
            while True:
                self._expire_jobs()
                jobs = self._take_jobs()
                remaining = deadline - time.monotonic()
                if jobs or remaining <= 0:
                    return {"ok": True, "session": self.session, "jobs": [j.public() for j in jobs]}
                self._job_ready.clear()
                try:
                    await asyncio.wait_for(self._job_ready.wait(), remaining)
                except TimeoutError:
                    pass

        @router.get("/outbox/{job_id}/files/{index}")
        async def job_file(job_id: str, index: int) -> Response:
            self._touch()
            job = self._jobs.get(job_id)
            if job is None or not 0 <= index < len(job.files):
                raise HTTPException(404, "任务或附件不存在（已过期或本服务重启过）")
            return Response(content=job.files[index][1], media_type="application/octet-stream")

        @router.post("/outbox/{job_id}/result")
        async def job_result(job_id: str, body: Annotated[dict[str, Any], Body()]) -> dict[str, Any]:
            self._touch()
            job = self._jobs.pop(job_id, None)
            if job is None:
                raise HTTPException(404, "任务不存在（已过期或本服务重启过）")
            result = {k: body[k] for k in ("ok", "error", "dry_run") if k in body}
            if not job.future.done():
                job.future.set_result(result)
            logger.info("wechat_link_job_done", job_id=job_id, ok=result.get("ok"), error=result.get("error"))
            return {"ok": True}

        return router

    def _auth(self, request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        given = (authorization or "").removeprefix("Bearer ").strip()
        if not given or not secrets.compare_digest(given, self._token):
            record_security_event("wechat_link_auth", outcome="denied", request=request)
            raise HTTPException(401, "token 错误")

    def _touch(self, instance: str = "") -> None:
        self._last_seen = time.monotonic()
        if instance:
            self._instance = instance
