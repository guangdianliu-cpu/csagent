"""聊天接口（HTTP/SSE）：经 MessageGateway 统一入站。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from csagent.admin import quick_entries
from csagent.admin.name_index import get_index as get_name_index
from csagent.api.attachments import resolve_attachment_paths
from csagent.api.router_files import build_download_url
from csagent.api.schemas import (
    ChallengeDecisionRequest,
    ChallengeDecisionResponse,
    ChallengeFile,
    ChatConfigResponse,
    ChatEntriesResponse,
    ChatSuggestResponse,
    ChatHistoryMessage,
    ChatHistoryResponse,
    ChatRequest,
    HistoryFile,
    PendingChallenge,
    PendingChallengeList,
    ReportCard,
)
from csagent.channels.approval_flow import (
    ClickOutcome,
    click_notice,
    resolve_click,
    resume_transcript,
    run_approved,
)
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.models import UnifiedMessage
from csagent.channels.outbound_files import drain_outbound_files
from csagent.channels.outbound_reports import drain_outbound_reports
from csagent.common.exceptions import LlmUnavailableError
from csagent.infra.auth import Principal, require_principal
from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.agent import Agent as MainAgent
from csagent.stream_events import AgentEventType
from csagent.tools.approvals import ApprovalStore, approval_chain_var

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])

# 工具执行和长思考期间可能几十秒没有业务事件。反代的空闲读超时常见是 60 秒，
# 到点会拆掉 SSE，浏览器侧就像前端把连接关了。注释心跳不进业务解析。
_SSE_HEARTBEAT_SECONDS = 15.0

# 用户关页后仍在跑的轮次。事件循环本身会留着 Task，这里再握一份引用，
# 避免被回收，也方便完成时打日志。
_detached_turns: set[asyncio.Task] = set()

# 每个会话同一时刻只允许一轮在跑：session_id → 承载该轮的泵任务。
# 关页后后台继续跑的轮次也在这里，用户刷新回来可以停掉它。
# 同一会话两轮并发会让记忆写入交错（两份上下文各自回写），所以直接拒绝。
_active_turns: dict[str, asyncio.Task] = {}

TURN_BUSY_DETAIL = "上一轮还在处理中，请等待完成或先停止上一轮"


# /challenge 执行批准操作时占住会话的占位任务名前缀；/stop 认出它就不去取消
_APPROVAL_HOLD_PREFIX = "approval-hold:"


def _running_turn(session_id: str) -> asyncio.Task | None:
    task = _active_turns.get(session_id)
    if task is not None and task.done():
        _active_turns.pop(session_id, None)
        return None
    return task


def _register_turn(session_id: str, task: asyncio.Task) -> None:
    _active_turns[session_id] = task

    def _on_done(done: asyncio.Task) -> None:
        # 只清自己：极端情况下同会话已登记了新一轮，不能把它的登记删掉
        if _active_turns.get(session_id) is done:
            _active_turns.pop(session_id, None)

    task.add_done_callback(_on_done)


def _keep_turn_alive(task: asyncio.Task, session_id: str) -> None:
    """客户端断开后让本轮继续跑完并落库。

    思考中关页只拆掉 SSE，不再取消 Agent。跑完后按正常回复写入记录，
    不会再记成「连接中断，正文未完成」。

    :param task: 承载本轮 ``gateway.handle`` 的泵任务
    :param session_id: 仅用于日志
    """
    _detached_turns.add(task)

    def _on_done(done: asyncio.Task) -> None:
        _detached_turns.discard(done)
        if done.cancelled():
            logger.warning("detached_turn_cancelled", session_id=session_id)
        else:
            exc = done.exception()
            if exc is not None:
                logger.warning("detached_turn_failed", session_id=session_id, error=str(exc))
            else:
                logger.info("detached_turn_finished", session_id=session_id)
        # 连接已经没了，文件进不了这一轮的 SSE。记录侧在落库时已经 peek 过，
        # 这里必须出队，否则会贴到同一会话的下一条消息上。
        drain_outbound_files(session_id)
        drain_outbound_reports(session_id)

    task.add_done_callback(_on_done)


def _encode_challenge(content: str, store: object | None = None) -> str | None:
    """把 Agent 的待确认事件转成 SSE。``ready`` 告诉前端这张现在能不能点。"""
    try:
        fields = json.loads(content or "")
    except json.JSONDecodeError:
        return None
    if not isinstance(fields, dict):
        return None
    task_id = str(fields.get("task_id") or "")
    if not task_id:
        return None
    return _sse_event(
        "challenge",
        content=str(fields.get("summary") or ""),
        task_id=task_id,
        tool=str(fields.get("tool") or ""),
        reason=str(fields.get("reason") or ""),
        ready=bool(store.is_ready(task_id)) if store is not None else True,
        expires_at=float(fields.get("expires_at") or 0),
    )


def _store_or_none(request: Request) -> ApprovalStore | None:
    """当前进程的确认登记簿；没接上时为 None（只用于给卡片标「能不能点」）。"""
    agent = getattr(request.app.state, "main_agent", None)
    return agent.approvals if agent is not None else None


def _approvals_of(request: Request) -> tuple[MainAgent, ApprovalStore]:
    """当前进程的 Agent 与确认登记簿。没接上时卡片无法核销。"""
    agent = getattr(request.app.state, "main_agent", None)
    store = agent.approvals if agent is not None else None
    if agent is None or store is None:
        raise HTTPException(status_code=503, detail="确认通道未启用")
    return agent, store


def _sse_event(
    event_type: str,
    content: str = "",
    error: str | None = None,
    **extra: object,
) -> str:
    payload: dict = {"type": event_type, "content": content}
    if error:
        payload["error"] = error
    payload.update(extra)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _file_size(workspace_path: str) -> int:
    """文件卡片上显示的大小；文件不在了（会话目录被清理）返回 0。"""
    try:
        return (PROJECT_ROOT / workspace_path).stat().st_size
    except (OSError, ValueError):
        return 0


def _require_http_channel(request: Request) -> None:
    """channels.yaml 关闭 http_api 时拒绝请求。"""
    manager = getattr(request.app.state, "channel_manager", None)
    if manager is None:
        return
    channel = manager.get("http_api")
    if channel is None or not getattr(channel, "is_running", False):
        raise HTTPException(status_code=503, detail="http_api channel disabled")


def _get_gateway(request: Request) -> MessageGateway:
    gateway = getattr(request.app.state, "message_gateway", None)
    if gateway is not None:
        return gateway
    main_agent: MainAgent = request.app.state.main_agent
    return MessageGateway(main_agent=main_agent)


@router.get("/config", response_model=ChatConfigResponse)
async def chat_config(request: Request) -> ChatConfigResponse:
    """返回聊天界面配置。

    不鉴权：前端要靠 ``dev_login`` 决定走不走联调登录，拿不到它就没法初始化。
    这里只暴露两个开关，不含任何用户数据。
    """
    _require_http_channel(request)
    settings = get_settings()
    return ChatConfigResponse(
        show_thinking_process=settings.show_thinking_process,
        auth_enabled=settings.agent_auth_enabled,
        dev_login=settings.agent_auth_dev_login,
    )


@router.get("/entries", response_model=ChatEntriesResponse)
async def chat_entries(
    request: Request,
    _: Principal = Depends(require_principal),
) -> ChatEntriesResponse:
    """快捷入口内容（欢迎问题组、分类标签栏、抽屉）。

    要鉴权：内容不敏感，但它是业务配置，不该像 ``/config`` 那样对外裸露。
    每次现读：管理后台保存后下一次请求即生效。
    """
    _require_http_channel(request)
    loaded = quick_entries.load()
    return ChatEntriesResponse(
        source=loaded.source, version=loaded.version, entries=loaded.entries
    )


@router.get("/suggest", response_model=ChatSuggestResponse)
async def chat_suggest(
    request: Request,
    q: str = Query(default="", max_length=100),
    _: Principal = Depends(require_principal),
) -> ChatSuggestResponse:
    """关键词联想：在输入里认出一个实体名称，代入快捷入口里带 XXX 的问题。

    名称来源见形态目录的 name_index.yaml（admin/name_index.py），库不在时恒返回空。
    """
    _require_http_channel(request)
    entries = quick_entries.load().entries
    if entries is None:
        return ChatSuggestResponse()
    allowed = quick_entries.suggest_entities(entries)
    hit = get_name_index().match(q, allowed) if allowed else None
    if hit is None:
        return ChatSuggestResponse()
    questions = quick_entries.placeholder_questions(entries, hit.entity)[:5]
    return ChatSuggestResponse(
        entity=hit.entity,
        name=hit.name,
        suggestions=[x.replace(quick_entries.PLACEHOLDER, hit.name) for x in questions],
    )


@router.get("/history", response_model=ChatHistoryResponse)
async def chat_history(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> ChatHistoryResponse:
    """返回当前身份的会话历史，供小窗刷新后回填聊天区。

    浏览器一刷新（或宿主重建 iframe），聊天区就空了，但服务端 short_term 仍按
    ``session_id`` 记着这轮对话——模型记得、用户看不见，用起来就是「刚聊的没了」。
    这里把服务端那一份读回去，让屏幕内容与模型上下文对齐。

    ``session_id`` 与 ``/stream`` 同源：一律由验签身份派生，**不接受客户端传入**。
    """
    _require_http_channel(request)
    main_agent: MainAgent = request.app.state.main_agent
    session_id = principal.session_id

    def _plain() -> ChatHistoryResponse:
        """纯文本回落：没有 transcript 后端、或读取出任何岔子时走这条。

        历史回填**宁可少给附件也不能不给正文**——正文没了用户会以为聊天记录丢了。
        """
        return ChatHistoryResponse(
            messages=[
                ChatHistoryMessage(role=m.role, content=m.content)
                for m in main_agent.load_history(session_id)
            ]
        )

    # 走 app.state 的公开接线，不去掏 main_agent 的私有属性：
    # 后者在测试里是 MagicMock，任何 hasattr 都为真，会把假对象当成真 store。
    store = getattr(request.app.state, "transcript_store", None)
    if store is None:
        return _plain()

    try:
        rows = store.load_recent_with_files(session_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("history_with_files_failed", error=str(exc))
        return _plain()

    messages = []
    for row in rows:
        files = []
        for f in row.get("files") or []:
            rel = f.get("workspace_path") or ""
            if not rel:
                continue
            # 下载授权在**读取时**现签：存进库里的必然过期（授权 24h / 记录 30 天）
            files.append(
                HistoryFile(
                    filename=f.get("filename") or "文件",
                    url=build_download_url(rel, session_id=session_id),
                    size=_file_size(rel),
                )
            )
        reports = []
        for f in row.get("files") or []:
            # 报告和文件存在同一列（agent._sent_files），按 kind 分开
            if f.get("kind") != "report":
                continue
            try:
                reports.append(ReportCard.model_validate(f))
            except ValueError:
                continue
        messages.append(
            ChatHistoryMessage(
                role=row["role"], content=row["content"], files=files, reports=reports
            )
        )
    return ChatHistoryResponse(messages=messages)


@router.post("/stop")
async def chat_stop(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> dict:
    """终止本会话正在进行的一轮（含关页后仍在后台跑的那轮）。

    幂等：没有在跑的轮次时返回 ``stopped=False``。已流出的部分回答会带
    「用户已停止」标记写进记忆，下一轮模型知道上一轮没做完。
    """
    _require_http_channel(request)
    session_id = principal.session_id
    task = _running_turn(session_id)
    if task is None:
        return {"stopped": False}
    if task.get_name().startswith(_APPROVAL_HOLD_PREFIX):
        # 批准的操作正在执行：已经发出去的动作停不下来，取消占位只会让响应断掉、
        # 续跑丢失，还会提前放开会话。告诉前端等它执行完
        return {"stopped": False, "reason": "approval_running"}
    main_agent = getattr(request.app.state, "main_agent", None)
    if main_agent is not None and hasattr(main_agent, "mark_stop_requested"):
        main_agent.mark_stop_requested(session_id)
    task.cancel()
    logger.info("chat_turn_stop_requested", session_id=session_id)
    return {"stopped": True}


@router.get("/challenges", response_model=PendingChallengeList)
async def list_challenges(
    request: Request,
    principal: Principal = Depends(require_principal),
) -> PendingChallengeList:
    """刷新页面后把还没点过的确认卡片交还给这个会话。"""
    _require_http_channel(request)
    _, store = _approvals_of(request)
    items = [
        PendingChallenge(
            task_id=item.task_id,
            tool=item.tool_name,
            summary=item.summary,
            reason=item.reason,
            ready=store.is_ready(item.task_id),
            expires_at=item.expires_at,
        )
        for item in store.list_pending(principal.session_id)
    ]
    return PendingChallengeList(items=items)


@router.post("/challenge", response_model=ChallengeDecisionResponse)
async def decide_challenge(
    request: Request,
    body: ChallengeDecisionRequest,
    principal: Principal = Depends(require_principal),
) -> ChallengeDecisionResponse:
    """网页卡片：批准则按保存的参数执行一次，拒绝则作废。

    批准成功后返回 ``resume_task_id``，前端随即带它调 ``/stream`` 续跑原任务。
    同一会话的卡片按先后批准，前面的没处理时返回 ``blocked``。
    只接受本会话自己的卡片。执行期间占住本会话的轮次，避免和下一句聊天同时写记忆。
    """
    _require_http_channel(request)
    session_id = principal.session_id
    # 检查和登记之间不能 await，否则两下点击会同时挤进执行。
    # 占位用独立任务，而不是当前请求任务：有的 ASGI 调用方会把多次请求跑在同一个任务里。
    if _running_turn(session_id) is not None:
        raise HTTPException(status_code=409, detail=TURN_BUSY_DETAIL)
    released: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    async def _hold_turn() -> None:
        await released

    hold_task = asyncio.create_task(
        _hold_turn(), name=f"{_APPROVAL_HOLD_PREFIX}{session_id}"
    )
    _register_turn(session_id, hold_task)
    try:
        return await _decide_challenge(request, body, principal, session_id)
    finally:
        if not released.done():
            released.set_result(None)
        try:
            await hold_task
        except asyncio.CancelledError:
            # 占位被别处取消（不是本请求被取消）：操作已经执行完，照常把结果返回给前端
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise


async def _decide_challenge(
    request: Request,
    body: ChallengeDecisionRequest,
    principal: Principal,
    session_id: str,
) -> ChallengeDecisionResponse:
    """核销卡片并执行或作废（规则见 channels/approval_flow.py）。调用方已占住本会话的轮次。"""
    agent, store = _approvals_of(request)
    click = resolve_click(
        store,
        body.task_id,
        approve=body.action == "confirm",
        operator=principal.user_id,
        # 只认本会话的卡片；别人的卡片当成不存在
        belongs_here=lambda item: item.session_id == session_id,
    )
    reply = click_notice(click)
    if click.outcome == ClickOutcome.REJECTED and click.pending is not None:
        # 拒绝也续跑一轮，让模型知道被拒了（前端拿 resume_task_id 调 /stream）
        return ChallengeDecisionResponse(
            status="cancelled", reply=reply, resume_task_id=click.pending.task_id
        )
    if click.outcome == ClickOutcome.BLOCKED and click.pending is not None:
        return ChallengeDecisionResponse(
            status="blocked", reply=reply, blocked_by=click.pending.task_id
        )
    if click.outcome != ClickOutcome.APPROVED or click.pending is None:
        return ChallengeDecisionResponse(status=str(click.outcome), reply=reply)

    run = await run_approved(agent, store, click.pending)
    if run.action is None:
        raise HTTPException(status_code=500, detail=run.error)
    # 网页端同一会话同时只跑一轮：续跑那一轮占着会话时，后面的卡片点批准会被 409 挡住，
    # 不用再靠确认簿等续跑结束。马上放开，免得关页丢了续跑后，后面的卡片被挡到超时
    store.settle(click.pending.task_id)

    files = [
        ChallengeFile(
            filename=item.filename,
            url=build_download_url(item.workspace_path, session_id=session_id),
        )
        for item in drain_outbound_files(session_id)
    ]
    reports = [ReportCard(**r.as_dict()) for r in drain_outbound_reports(session_id)]
    return ChallengeDecisionResponse(
        status="approved",
        reply=run.action.text or "",
        files=files,
        reports=reports,
        resume_task_id=run.action.task_id if run.action.continuation else "",
    )


@router.post("/stream")
async def chat_stream(
    request: Request,
    body: ChatRequest,
    principal: Principal = Depends(require_principal),
) -> StreamingResponse:
    """流式聊天接口，使用 SSE 推送回复片段。

    ``session_id`` 一律由验签后的身份派生，**不接受客户端传入**——它是记忆边界，
    客户端可选就等于可以读任何人的历史。
    """
    _require_http_channel(request)
    session_id = principal.session_id
    gateway = _get_gateway(request)
    if _running_turn(session_id) is not None:
        raise HTTPException(status_code=409, detail=TURN_BUSY_DETAIL)
    attachments, dropped = resolve_attachment_paths(body.attachment_paths)

    # 附件解析失败必须让模型知道，否则它会以为用户什么都没发、然后凭空作答
    message_text = body.message
    metadata: dict[str, str] = {}
    resume_chain = ""
    if body.resume_task_id:
        # 批准 / 拒绝后续跑：正文换成服务端记下的说明（执行结果或被拒），客户端给的文字不用。
        # 一次性取走，同一张卡片不能续跑两次；别的会话的卡片取不到
        _, store = _approvals_of(request)
        resumed = store.take_continuation(session_id, body.resume_task_id)
        if resumed is None:
            raise HTTPException(status_code=409, detail="该确认已续跑过或已过期")
        message_text = resumed.continuation
        attachments, dropped = [], []
        metadata["transcript_text"] = resume_transcript(resumed)
        resume_chain = resumed.chain
    if dropped:
        detail = "；".join(f"{d['path']}（{d['reason']}）" for d in dropped)
        notice = (
            f"[系统提示] 用户本次提交的 {len(dropped)} 个附件未能载入：{detail}。"
            "请如实告知用户文件没有收到、需要重新上传，不要假装已经看过这些文件。"
        )
        message_text = f"{notice}\n\n{message_text}" if message_text else notice

    def _encode(event: object) -> str | None:
        event_type = getattr(event, "event_type", None)
        content = getattr(event, "content", "") or ""
        if event_type == AgentEventType.THOUGHT:
            return _sse_event("thought", content=content)
        if event_type == AgentEventType.REASONING:
            return _sse_event("reasoning", content=content)
        if event_type == AgentEventType.PROGRESS:
            return _sse_event("progress", content=content)
        if event_type == AgentEventType.DELTA:
            return _sse_event("delta", content=content)
        if event_type == AgentEventType.CHALLENGE:
            return _encode_challenge(content, _store_or_none(request))
        return None

    async def event_generator() -> AsyncIterator[str]:
        yield _sse_event("start")
        # 整段对话必须留在同一个 Task 里。若每次 __anext__ 都 create_task，
        # audit_trace_buffer 的 Token 会在另一个 Context 里 reset，报
        # "was created in a different Context"。
        events: asyncio.Queue[object] = asyncio.Queue()
        stream_done = object()
        stopped_marker = object()

        async def _pump() -> None:
            # 泵是独立任务、有自己的上下文副本，这里设了不用还原
            if resume_chain:
                approval_chain_var.set(resume_chain)
            try:
                async for event in gateway.handle(
                    UnifiedMessage(
                        channel=body.channel or "http_api",
                        session_id=session_id,
                        text=message_text,
                        attachments=attachments,
                        metadata=metadata,
                        principal={
                            "user_id": principal.user_id,
                            "auth_method": principal.auth_method,
                        },
                    )
                ):
                    await events.put(event)
            except asyncio.CancelledError:
                # /api/chat/stop 取消了本轮。Agent 侧的 finally 已按「用户停止」落库；
                # 告诉仍连着的前端一声，然后照常传播取消。
                events.put_nowait(stopped_marker)
                raise
            except Exception as exc:  # noqa: BLE001
                await events.put(exc)
            else:
                await events.put(stream_done)

        # 生成器开始执行时再查一次：两次请求可能在同一时刻都通过了上面的检查
        if _running_turn(session_id) is not None:
            yield _sse_event("error", error=TURN_BUSY_DETAIL)
            return
        pump_task = asyncio.create_task(_pump())
        _register_turn(session_id, pump_task)
        try:
            while True:
                try:
                    item = await asyncio.wait_for(events.get(), timeout=_SSE_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                if item is stream_done:
                    break
                if isinstance(item, Exception):
                    raise item
                if item is stopped_marker:
                    drain_outbound_files(session_id)
                    drain_outbound_reports(session_id)
                    yield _sse_event("stopped")
                    return
                encoded = _encode(item)
                if encoded:
                    yield encoded

            # HTTP 通道：send_file 入队的文件以可下载链接形式推给前端
            for item in drain_outbound_files(session_id):
                url = build_download_url(
                    item.workspace_path, session_id=session_id
                )
                yield _sse_event(
                    "file",
                    content=item.filename,
                    workspace_path=item.workspace_path,
                    filename=item.filename,
                    url=url,
                    size=_file_size(item.workspace_path),
                )
            # 报告卡片：同样在本轮文字之后、done 之前
            for report in drain_outbound_reports(session_id):
                yield _sse_event("report", **report.as_dict())
            yield _sse_event("done")
        except LlmUnavailableError as exc:
            drain_outbound_files(session_id)
            drain_outbound_reports(session_id)
            yield _sse_event("error", error=str(exc))
        except Exception as exc:  # noqa: BLE001
            drain_outbound_files(session_id)
            drain_outbound_reports(session_id)
            yield _sse_event("error", error=f"服务内部错误: {exc}")
        finally:
            # 正常结束时泵已经把 stream_done 放进队列。关页/反代拆连接时泵还在跑：
            # 不取消、也不在已取消的请求里 await，让本轮在后台跑完再落库。
            if pump_task.done():
                if not pump_task.cancelled():
                    pump_task.exception()
            else:
                logger.info("sse_client_gone_keep_turn", session_id=session_id)
                _keep_turn_alive(pump_task, session_id)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
