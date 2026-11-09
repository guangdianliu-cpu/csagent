"""单 Agent：会话管理 + ReAct 循环 + 全量工具 + Skill 索引/加载。

合并旧 orchestrator/main_agent.py + domain_agents/base.py + domain_agents/generic_agent.py。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any

import structlog

from csagent.audit.fingerprint import turn_config_digest
from csagent.audit.security_logger import record_security_event
from csagent.audit.session_logger import SessionAuditLogger, SessionAuditRecord
from csagent.audit.trace_buffer import (
    AuditTraceBuffer,
    ToolCallRecord,
    audit_trace_var,
    get_audit_buffer,
)
from csagent.guardrail.input_guard import InputGuard
from csagent.guardrail.output_guard import OutputGuard
from csagent.infra.logging import bind_context, reset_context
from csagent.memory.budget import ContextBudgetAllocator
from csagent.memory.compaction_policy import HistoryCompactionPolicy
from csagent.memory.context_pipeline import ContextFrame, ContextPipeline
from csagent.memory.context_selector import QueryAwareContextSelector
from csagent.memory.prompt_layout import PromptLayoutOptimizer
from csagent.memory.protocols import (
    SessionSnapshotStoreProtocol,
    ShortTermMemoryStore,
    TranscriptStore,
    TurnHandle,
)
from csagent.memory.session_snapshot import SessionSnapshotStore, ToolCallSnapshot
from csagent.memory.token_counter import TokenCounter
from csagent.prompts.capability_notes import format_capability_notes
from csagent.prompts.loader import render_prompt
from csagent.react.loop import ReactLoop
from csagent.react.models import ChatMessage, DomainTaskRequest, DomainTaskResult
from csagent.session_state import SessionState, session_state_var
from csagent.skills.discovery import SkillDiscovery
from csagent.stream_events import AgentEventType, AgentStreamEvent
from csagent.tools.approvals import ApprovalStore, PendingApproval
from csagent.tools.delegation import begin_delegation_scope, current_delegation, delegation_scope_var
from csagent.tools.packs.skill_pack import render_resident_skills
from csagent.tools.preference_tool import ReplyPreferenceStore
from csagent.tools.registry import ToolRegistry
from csagent.user_progress import SESSION_START

logger = structlog.get_logger(__name__)

ThoughtCallback = Callable[[str], Awaitable[None]]

# 中断落盘时给助手文本加的尾标记：让下一轮的模型知道这轮话没说完，
# 而不是把半截回答当成一次完整应答来做指代消解。
INTERRUPTED_SUFFIX = "\n\n（本轮回答因连接中断未完成）"
STOPPED_SUFFIX = "\n\n（用户已手动停止本轮，回答未完成）"
STOPPED_EMPTY_REPLY = "（用户在回答产出前手动停止了本轮）"

# 输出护栏拦截：一个字都还没发出时整条替换；已经流出一部分时只能停止并补一句说明
BLOCKED_OUTPUT_REPLY = "抱歉，这条回复包含不合规内容，已被拦截。请换个方式描述您的问题。"
BLOCKED_OUTPUT_NOTICE = "\n\n（后续内容包含不合规信息，已停止输出。）"


@dataclass
class ApprovedAction:
    """卡片批准后执行一次的结果。

    ``text`` 已过输出护栏，可以直接给用户看；``continuation`` 是续跑那一轮发给
    模型的说明（同一份已记在确认簿里，渠道用 ``take_continuation`` 一次性取走）。
    """

    task_id: str
    status: str
    text: str
    continuation: str


class _PassThroughStream:
    """护栏不支持流式遮挡时（测试替身）的直通实现。"""

    blocked = False
    violations: list[str] = []

    def feed(self, chunk: str) -> str:
        return chunk

    def finish(self) -> str:
        return ""


def _principal_fields(scope: Any) -> dict[str, str]:
    """审计记录里的请求主体字段（scope 是测试替身时缺的字段按空串）。"""
    return {
        name: str(getattr(scope, name, "") or "")
        for name in ("user_id", "auth_method", "chat_type", "chat_id")
    }


def _finish_stream(stream: Any, parts: list[str]) -> str:
    """放出流式遮挡器扣住的尾巴，并记进已发出片段；没有尾巴返回空串。"""
    tail = stream.finish()
    if tail:
        parts.append(tail)
    return tail


def _discard_background_task(task: asyncio.Task) -> None:
    """取消后台任务并吞掉它的结果，避免 "exception never retrieved" 噪声。

    用在客户端断连的收尾路径上：此时已经没有消费者了，ReAct 循环继续跑只是白烧
    Token 和工具调用配额。这里只发取消信号、不等待——收尾路径不允许 await。

    :param task: 要丢弃的后台任务；已结束的任务调用 ``cancel()`` 是安全的空操作
    """
    task.cancel()

    def _swallow(done: asyncio.Task) -> None:
        # cancelled() 为真时短路，不去碰 exception()（那会重新抛 CancelledError）。
        if not done.cancelled():
            done.exception()

    task.add_done_callback(_swallow)


class Agent:
    """单 Agent：ReAct 循环 + 全量工具 + Skill 索引/加载 + 会话管理。

    对外接口兼容旧 MainAgent：
    - handle_message_stream(...) -> AsyncIterator[AgentStreamEvent]
    - start_audit_logger() / stop_audit_logger()
    """

    def __init__(
        self,
        react_loop: ReactLoop,
        tool_registry: ToolRegistry,
        short_term_memory: ShortTermMemoryStore,
        input_guard: InputGuard | None = None,
        output_guard: OutputGuard | None = None,
        audit_logger: SessionAuditLogger | None = None,
        session_snapshot_store: SessionSnapshotStoreProtocol | None = None,
        transcript_store: TranscriptStore | None = None,
        history_compactor: Any = None,
        history_window: int = 12,
        show_thinking_process: bool = False,
        max_rounds: int = 48,
        *,
        agent_name: str = "agent",
        skill_discovery: SkillDiscovery | None = None,
        resident_skills: tuple[str, ...] = (),
        reply_preference_store: ReplyPreferenceStore | None = None,
        token_counter: TokenCounter | None = None,
        budget_allocator: ContextBudgetAllocator | None = None,
        context_selector: QueryAwareContextSelector | None = None,
        prompt_layout: PromptLayoutOptimizer | None = None,
        persistent_instructions: str = "",
        compaction_policy: HistoryCompactionPolicy | None = None,
        graded_memory: Any = None,
        recaller: Any = None,
        context_token_high_ratio: float = 0.50,
    ) -> None:
        self._react_loop = react_loop
        self._tool_registry = tool_registry
        self._short_term_memory = short_term_memory
        self._input_guard = input_guard or InputGuard()
        self._output_guard = output_guard or OutputGuard()
        self._audit_logger = audit_logger or SessionAuditLogger(enabled=False)
        self._snapshot_store = session_snapshot_store or SessionSnapshotStore()
        self._transcript_store = transcript_store
        self._history_compactor = history_compactor
        self._history_window = max(1, history_window)
        self._show_thinking_process = show_thinking_process
        self._max_rounds = max_rounds
        self._reply_preference_store = reply_preference_store

        # Phase 1-5 新增组件
        self._counter = token_counter or TokenCounter()
        self._budget_allocator = budget_allocator or ContextBudgetAllocator(self._counter)
        self._context_selector = context_selector or QueryAwareContextSelector(self._counter)
        self._prompt_layout = prompt_layout or PromptLayoutOptimizer(self._counter)
        self._persistent_instructions = (persistent_instructions or "").strip()
        self._graded = graded_memory
        # 0 表示关掉这条告警。M3.3 把窗口加大之后需要一条可配置的硬线。
        self._context_token_high_ratio = max(0.0, float(context_token_high_ratio or 0.0))

        # 上下文组装管线：把原 _handle_inner 里步骤 1-8 的过程式编排收拢成一条 stage
        # 序列。这里只声明「用哪些组件」，「按什么顺序、各分多少预算」全在管线内部——
        # 想知道某轮发给 LLM 的到底是什么，看 ContextPipeline.build 一处即可。
        self._context_pipeline = ContextPipeline(
            short_term_memory=self._short_term_memory,
            snapshot_store=self._snapshot_store,
            history_compactor=self._history_compactor,
            token_counter=self._counter,
            budget_allocator=self._budget_allocator,
            context_selector=self._context_selector,
            prompt_layout=self._prompt_layout,
            reply_preference_store=self._reply_preference_store,
            persistent_instructions=self._persistent_instructions,
            history_window=self._history_window,
            compaction_policy=compaction_policy,
            graded_memory=graded_memory,
            recaller=recaller,
        )

        # Agent 配置
        self.domain = "agent"
        self.agent_name = agent_name
        self._skill_discovery = skill_discovery
        # 形态的常驻 Skill（profile.yaml 的 resident_skills）：说明每轮都在系统提示词末尾
        self._resident_skills = tuple(resident_skills)
        # 用户主动点「停止」的会话。中断收尾据此区分「用户停止」和「连接断开」，
        # 用完即删（见 _finalize_interrupted_turn）。
        self._stop_requested: set[str] = set()

    def mark_stop_requested(self, session_id: str) -> None:
        """标记该会话本轮是用户主动停止的；随后由调用方取消承载本轮的任务。"""
        self._stop_requested.add(session_id)

    def start_audit_logger(self) -> None:
        if hasattr(self._audit_logger, "start"):
            self._audit_logger.start()

    async def stop_audit_logger(self) -> None:
        if hasattr(self._audit_logger, "stop"):
            await self._audit_logger.stop()

    @property
    def approvals(self) -> ApprovalStore | None:
        """确认卡片登记簿（挂在工具注册中心上）；部署没有确认通道时为 None。

        渠道层（网页 / 企微）经这里取，不要伸手去拿 ``_tool_registry``。
        """
        return getattr(self._tool_registry, "approvals", None)

    # ------------------------------------------------------------------
    # 会话历史（只读）
    # ------------------------------------------------------------------

    def load_history(self, session_id: str, limit: int | None = None) -> list[ChatMessage]:
        """读取会话历史，供前端刷新后回填聊天区。

        取的是**对话记录**（``transcript``，落盘、最近 100 条），不是模型的记忆窗口。
        两者的分工见 ``memory/transcript.py`` 的模块注释：屏幕上能翻到的，不等于
        模型还记得的。记录库没配或读失败时回落到 ``short_term``——那时能看到的
        少一些（20 条封顶），但至少与模型上下文一致，不会整块空白。

        纯读：不触发压缩、不写审计、不动快照。走 Agent 而不是让 API 层直接持有
        store，是为了守住「记忆统一由 Agent 串」这条约定——将来记忆分层变化时，
        回填口径跟着这里走，不用同步改 API。

        记忆边界是传进来的 ``session_id``，由调用方从验签身份派生
        （``infra/auth.py::Principal.session_id``），不得取自请求参数。
        """
        if not session_id:
            return []
        if self._transcript_store is not None:
            try:
                return list(self._transcript_store.load_recent(session_id, limit))
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "transcript_load_failed", session_id=session_id, error=str(exc)
                )
        return list(self._short_term_memory.load(session_id))

    # ------------------------------------------------------------------
    # 系统提示词
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        skill_index = ""
        resident = ""
        if self._skill_discovery is not None:
            skill_index = self._skill_discovery.render_index(resident=self._resident_skills)
            resident = render_resident_skills(self._skill_discovery, self._resident_skills)
        prompt = render_prompt(
            "agent",
            skill_index=skill_index,
            capability_notes=format_capability_notes(self._list_allowed_tool_names()),
        )
        # 常驻 Skill 的正文长，放在最后，不打断前面的身份、路由与边界规则
        return f"{prompt}\n{resident}\n" if resident else prompt

    def _list_allowed_tool_names(self) -> list[str]:
        return [t.name for t in self._tool_registry.list_tools_for_agent(self.agent_name)]

    # ------------------------------------------------------------------
    # ReAct 执行
    # ------------------------------------------------------------------

    async def _run_react(
        self,
        request: DomainTaskRequest,
        on_thought: ThoughtCallback | None = None,
        on_reasoning: ThoughtCallback | None = None,
        on_delta: ThoughtCallback | None = None,
    ) -> DomainTaskResult:
        async def wrapped_thought(text: str) -> None:
            if on_thought is not None:
                await on_thought(f"[{self.domain}] {text}")

        async def wrapped_progress(text: str) -> None:
            state = session_state_var.get()
            if state is not None and state.on_progress is not None:
                await state.on_progress(text)

        async def wrapped_reasoning(text: str) -> None:
            cb = on_reasoning
            if cb is None:
                state = session_state_var.get()
                if state is not None:
                    cb = state.on_reasoning
            if cb is not None:
                await cb(text)

        state = session_state_var.get()
        has_reasoning_sink = on_reasoning is not None or (
            state is not None and state.on_reasoning is not None
        )

        return await self._react_loop.run(
            request=request,
            system_prompt=self.build_system_prompt(),
            agent_name=self.agent_name,
            on_thought=wrapped_thought,
            on_progress=wrapped_progress,
            on_delta=on_delta,
            on_reasoning=wrapped_reasoning if has_reasoning_sink else None,
        )

    # ------------------------------------------------------------------
    # 消息处理（对外主接口）
    # ------------------------------------------------------------------

    async def handle_message_stream(
        self,
        session_id: str,
        text: str,
        channel: str = "internal_agent",
        vision_parts: list | None = None,
        transcript_user_text: str | None = None,
        transcript_user_files: list[dict] | None = None,
        principal: dict[str, str] | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        """``transcript_user_text``：只写进对话记录的用户原话。

        ``principal``：通道填的请求主体（user_id / auth_method / chat_type / chat_id），
        进 CustomerScope 供工具策略判定与审计；缺省为空（内部调用、测试）。

        ``text`` 是喂给模型的富文本——附件块、路径、内容预览、默认指令全在里面，
        动辄几千字。那是模型需要的，但界面翻历史时用户想看到的是自己当时打的那句话
        加一句「[附件] xx.xlsx」。两者分开传，谁也不将就谁。留空则回落到 ``text``。
        """
        trace_id = uuid.uuid4().hex
        from csagent.react.models import CustomerScope

        who = principal or {}
        scope = CustomerScope(
            session_id=session_id,
            channel=channel,
            user_id=str(who.get("user_id") or ""),
            auth_method=str(who.get("auth_method") or ""),
            chat_type=str(who.get("chat_type") or ""),
            chat_id=str(who.get("chat_id") or ""),
            trace_id=trace_id,
        )
        ctx_tokens = bind_context(
            trace_id=trace_id,
            session_id=session_id,
            channel=channel,
        )
        # 请求级审计缓冲：收集 thought / 结构化事件 / 工具调用 / LLM 轮次。
        # 始终创建（不受 audit_log_enabled 影响）：写磁盘与否由 SessionAuditLogger.write()
        # 内部的 enabled 判断控制；但工具调用快照回写记忆（M3.1）不应依赖审计开关。
        audit_token = audit_trace_var.set(AuditTraceBuffer())
        # 本轮委托范围（Skill allowed-tools 与已加载 Skill）：可变对象放进上下文，
        # 工具批次的子任务修改的是同一个对象，硬约束才能跨批次生效（见 tools/delegation.py）
        delegation_token = begin_delegation_scope()
        # 常驻 Skill 的说明每轮都在提示词里，等于已加载：硬约束模式下它的资源照常可读
        current_delegation().loaded_skills.update(self._resident_skills)
        # M2.2：在**到达时**就把本轮的 episode 与 turn_id 定下来，而不是等回写。
        # 同一个人并发提两个问题时，这决定了历史按「谁先问」还是「谁先跑完」排序，
        # 两者能差出几十秒。拿号必须在任何 await 之前，否则又滑回完成序。
        turn = self._begin_turn(session_id)
        # 上一轮没被通道取走的报告卡片（企微 / 微信群暂不渲染，M10 降级）不能贴到这一轮
        from csagent.channels.outbound_reports import clear_outbound_reports

        clear_outbound_reports(session_id)
        logger.info("会话开始", message_length=len(text))
        try:
            # aclosing 保证内层生成器被**确定性**关闭：客户端断连时 GeneratorExit
            # 会立刻送进 _handle_inner 的 finally（中断落盘就在那里执行），而不是
            # 等 GC 在某个不确定的时刻替我们收尾——那时 audit_trace_var 早已 reset，
            # 本轮的工具调用就再也捞不回来了。
            async with aclosing(
                self._handle_inner(
                    scope, trace_id, text, list(vision_parts or []),
                    transcript_user_text=transcript_user_text,
                    transcript_user_files=transcript_user_files,
                    turn=turn,
                )
            ) as stream:
                async for event in stream:
                    yield event
        finally:
            delegation_scope_var.reset(delegation_token)
            audit_trace_var.reset(audit_token)
            reset_context(ctx_tokens)

    async def _handle_inner(
        self,
        scope: Any,
        trace_id: str,
        text: str,
        vision_parts: list,
        *,
        transcript_user_text: str | None = None,
        transcript_user_files: list[dict] | None = None,
        turn: TurnHandle | None = None,
    ) -> AsyncIterator[AgentStreamEvent]:
        # 上一轮若在收尾前就被停掉（还没进到中断收口），残留的停止标记不能带进本轮
        self._stop_requested.discard(scope.session_id)
        # 输入护栏
        guard_verdict = await self._input_guard.review(text)
        if not guard_verdict.passed:
            logger.warning("input_guard_blocked", violations=guard_verdict.violations)
            reply = "抱歉，您的输入包含不合规内容，请重新描述您的问题。"
            yield self._delta(reply)
            await self._write_audit(
                scope=scope, trace_id=trace_id, user_message=text,
                final_reply=reply, status="blocked_input",
                violations=guard_verdict.violations,
            )
            return

        if self._show_thinking_process:
            yield self._thought("Guardrail 检测通过")

        # 会话上下文：交给 ContextPipeline 统一组装（历史 / 快照 / Token 预算 /
        # 结构化压缩 / 回复偏好 / 工具调用回溯）。返回的 ContextFrame 就是这轮要发给
        # LLM 的全部动态输入——排查「这轮到底发了什么」只看 ContextPipeline.build 一处，
        # 不用再顺着这里的步骤 1-8 追五个组件。
        frame = self._context_pipeline.build(
            session_id=scope.session_id,
            user_text=text,
            system_prompt=self.build_system_prompt(),
            tool_schemas=[
                t.to_function_schema() if hasattr(t, "to_function_schema") else {}
                for t in self._tool_registry.list_tools_for_agent(self.agent_name)
            ],
            scope=scope,
        )
        self._record_context_layers(frame)
        self._record_config_fingerprint()
        context_summary = frame.context_summary
        checkpoint_summary = frame.checkpoint_summary
        persistent_instructions = frame.persistent_instructions

        try:
            yield self._progress(SESSION_START)
            yield self._progress("正在处理...")
        except (GeneratorExit, asyncio.CancelledError):
            # ReAct 还没开跑就被用户停止：同样落一轮「已停止」，否则问题从历史里消失。
            # 单纯断连（未点停止）维持原语义：屏幕上什么都没有，就什么都不记。
            if scope.session_id in self._stop_requested:
                self._finalize_interrupted_turn(
                    scope=scope,
                    trace_id=trace_id,
                    user_text=text,
                    streamed_parts=[],
                    reasoning_parts=[],
                    transcript_user_text=transcript_user_text,
                    transcript_user_files=transcript_user_files,
                    turn=turn,
                )
            raise

        request = DomainTaskRequest(
            task_id=uuid.uuid4().hex,
            domain=self.domain,
            instruction=text,
            customer_scope=scope,
            context_summary=context_summary,
            persistent_instructions=persistent_instructions,
            checkpoint_summary=checkpoint_summary,
            memory_snapshot={},
            trace_id=trace_id,
            max_rounds=self._max_rounds,
            vision_parts=vision_parts,
        )

        # 流式队列
        thought_queue: asyncio.Queue[str | None] = asyncio.Queue()
        reasoning_queue: asyncio.Queue[str | None] = asyncio.Queue()
        delta_queue: asyncio.Queue[str | None] = asyncio.Queue()
        progress_queue: asyncio.Queue[str | None] = asyncio.Queue()
        # streamed_parts / reasoning_parts 记的是**已经发给客户端**的（脱敏后的）片段
        streamed_parts: list[str] = []
        reasoning_parts: list[str] = []

        # 输出护栏必须在片段出站前生效：终答与思考过程都是边生成边推，客户端是追加
        # 语义，等全文生成完再脱敏，原文早已发出。每路流各开一个遮挡器。
        delta_stream = self._open_output_stream()
        reasoning_stream = self._open_output_stream()

        async def on_thought(msg: str) -> None:
            await thought_queue.put(self._mask_output(msg))

        async def on_reasoning(msg: str) -> None:
            safe = reasoning_stream.feed(msg) if msg else ""
            if safe:
                reasoning_parts.append(safe)
                await reasoning_queue.put(safe)

        async def on_delta(chunk: str) -> None:
            safe = delta_stream.feed(chunk) if chunk else ""
            if safe:
                streamed_parts.append(safe)
                await delta_queue.put(safe)

        async def on_progress(msg: str) -> None:
            await progress_queue.put(self._mask_output(msg))

        session_state = SessionState(
            context_summary=context_summary,
            trace_id=trace_id,
            on_progress=on_progress,
            on_reasoning=on_reasoning,
        )
        state_token = session_state_var.set(session_state)

        agent_task = asyncio.create_task(
            self._run_react(
                request,
                on_thought=on_thought if self._show_thinking_process else None,
                on_reasoning=on_reasoning,
                on_delta=on_delta,
            )
        )

        # ---------- 流式多路复用 + 中断兜底 ----------
        # turn_finalized 标记「本轮已按正常路径收口」。客户端断连（关页面 / 刷新 /
        # 网络抖动）会在下面的 yield 或 sleep 处抛出 CancelledError 或 GeneratorExit，
        # 把后续所有回写一并掀掉——那正是「用户屏幕上留着半截答案、模型侧零记录」的成因。
        #
        # 兜底放在 finally 而不是 except：异常类型不用穷举，生成器被 aclose 关闭时也会执行。
        # 约束：中断分支内**不能 yield、不能 await**（生成器正在关闭），
        # 因此 _finalize_interrupted_turn 整条链路必须是同步的。
        turn_finalized = False
        try:
            try:
                while not agent_task.done() or any(
                    not q.empty()
                    for q in (thought_queue, reasoning_queue, delta_queue, progress_queue)
                ):
                    drained = False
                    for queue, factory in (
                        (thought_queue, self._thought),
                        (reasoning_queue, self._reasoning),
                        (progress_queue, self._progress),
                        (delta_queue, self._delta),
                    ):
                        while not queue.empty():
                            item = queue.get_nowait()
                            if item:
                                drained = True
                                yield factory(item)
                    if agent_task.done() and all(
                        q.empty()
                        for q in (thought_queue, reasoning_queue, delta_queue, progress_queue)
                    ):
                        break
                    if not drained:
                        await asyncio.sleep(0.02)

                try:
                    result = await agent_task
                except Exception as exc:
                    logger.exception("agent_run_failed", error=str(exc))
                    # 遮挡器扣住的尾巴是已生成、只是还没发的正文，先放出来再报失败
                    if tail := _finish_stream(reasoning_stream, reasoning_parts):
                        yield self._reasoning(tail)
                    if tail := _finish_stream(delta_stream, streamed_parts):
                        yield self._delta(tail)
                    # 失败正文也算已经展示给用户的内容，交给下面的中断收口落库。
                    # 这里若把 turn_finalized 置真，finally 会跳过落库。
                    fail_text = self._mask_output(f"抱歉，处理失败：{exc}")
                    streamed_parts.append(fail_text)
                    yield self._delta(fail_text)
                    return
            finally:
                # 保持原有的重置时机：回写阶段派生的后台任务（如 Checkpoint 刷新）
                # 不应继承本轮的流式回调。
                session_state_var.reset(state_token)

            if tail := _finish_stream(reasoning_stream, reasoning_parts):
                yield self._reasoning(tail)
            if tail := _finish_stream(delta_stream, streamed_parts):
                yield self._delta(tail)

            # M3.1：把本轮工具调用写入会话快照，供下一轮回溯（不依赖 audit_log_enabled）
            self._append_tool_call_snapshots(scope.session_id, turn)

            already_streamed = bool("".join(streamed_parts).strip())
            final_reply = (result.answer_draft or "").strip()
            if not final_reply and already_streamed:
                final_reply = "".join(streamed_parts).strip()
            if not final_reply:
                final_reply = "抱歉，暂时无法理解您的问题，请换个方式描述。"

            # 输出护栏：流式片段出站前已逐段脱敏 / 拦截，这里对全文再判一次，结果用于
            # 落库与审计。已经流出去的内容**不再重发**：客户端是追加语义，重发只会让
            # 用户看到两遍（以前脱敏后的全文就是这样被追加在原文后面的）。
            output_verdict = self._output_guard.review(final_reply)
            violations = list(
                dict.fromkeys([*delta_stream.violations, *output_verdict.violations])
            )
            blocked = delta_stream.blocked or not output_verdict.passed
            if blocked:
                if already_streamed:
                    yield self._delta(BLOCKED_OUTPUT_NOTICE)
                    final_reply = "".join(streamed_parts).strip() + BLOCKED_OUTPUT_NOTICE
                else:
                    final_reply = BLOCKED_OUTPUT_REPLY
                    yield self._delta(final_reply)
                logger.warning("output_guard_blocked", violations=violations)
            else:
                final_reply = output_verdict.sanitized_text or final_reply
                if not already_streamed:
                    yield self._delta(final_reply)

            # 本轮有操作被策略要求确认。网页和企微都靠卡片事件，不往回复里写编号。
            for event in self._challenge_events():
                yield event
            notice_for_memory = self._challenge_notice()
            if notice_for_memory:
                final_reply = final_reply + notice_for_memory

            self._write_back_memory(
                scope=scope,
                user_text=text,
                assistant_text=final_reply,
                transcript_user_text=transcript_user_text,
                transcript_user_files=transcript_user_files,
                turn=turn,
            )
            # 记忆已落盘即视为收口：此后审计写失败也不该触发中断分支的二次回写。
            turn_finalized = True
            await self._write_audit(
                scope=scope, trace_id=trace_id, user_message=text,
                final_reply=final_reply,
                status="blocked_output" if blocked else "success",
                violations=violations,
            )
            buf = get_audit_buffer()
            logger.info(
                "会话结束",
                reply_length=len(final_reply),
                prompt_tokens=0 if buf is None else buf.prompt_tokens,
                completion_tokens=0 if buf is None else buf.completion_tokens,
            )
        finally:
            if not turn_finalized:
                # 正文已经生成完，只是生成器在落库前被关掉（SSE 收尾取消、
                # 反代收包后断开）。这不是半截回答，按成功回写，不要再追加中断后缀。
                finished_reply = self._finished_reply_if_agent_done(agent_task, streamed_parts)
                if finished_reply:
                    # answer_draft 是模型原文，落库前同样要脱敏；被拦截的轮次只记已发出的部分
                    if delta_stream.blocked:
                        finished_reply = (
                            "".join(streamed_parts).strip() + BLOCKED_OUTPUT_NOTICE
                        )
                    else:
                        finished_reply = self._mask_output(finished_reply)
                    self._write_back_memory(
                        scope=scope,
                        user_text=text,
                        assistant_text=finished_reply,
                        transcript_user_text=transcript_user_text,
                        transcript_user_files=transcript_user_files,
                        turn=turn,
                    )
                    turn_finalized = True
                else:
                    _discard_background_task(agent_task)
                    self._finalize_interrupted_turn(
                        scope=scope,
                        trace_id=trace_id,
                        user_text=text,
                        streamed_parts=streamed_parts,
                        reasoning_parts=reasoning_parts,
                        transcript_user_text=transcript_user_text,
                        transcript_user_files=transcript_user_files,
                        turn=turn,
                    )

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _challenge_notice() -> str:
        """落库用的确认提示。本轮没有待确认操作时为空串。"""
        buf = get_audit_buffer()
        if buf is None or not buf.challenges:
            return ""
        stored = [
            f"【已向用户发出确认请求】{item['summary']}（等待用户在卡片上确认）"
            for item in buf.challenges
        ]
        return "\n\n" + "\n".join(stored)

    @staticmethod
    def _challenge_events() -> list[AgentStreamEvent]:
        """网页和企微用来画确认卡片的事件。"""
        buf = get_audit_buffer()
        if buf is None:
            return []
        events: list[AgentStreamEvent] = []
        for item in buf.challenges:
            payload = {
                "task_id": item.get("task_id") or "",
                "tool": item.get("tool") or "",
                "summary": item.get("summary") or "",
                "reason": item.get("reason") or "",
                "rule_id": item.get("rule_id") or "",
                "user_id": item.get("user_id") or "",
                "chat_type": item.get("chat_type") or "",
                "expires_at": item.get("expires_at") or 0,
            }
            events.append(
                AgentStreamEvent(
                    event_type=AgentEventType.CHALLENGE,
                    content=json.dumps(payload, ensure_ascii=False),
                )
            )
        return events

    async def execute_approved_action(self, pending: PendingApproval) -> ApprovedAction:
        """卡片已批准：按保存的参数执行一次，记下结果，返回续跑说明。

        这里只执行、不收口：渠道随后拿 ``take_continuation`` 起一轮新的对话，
        模型带着执行结果把原任务做完（续跑那一轮再按原参数调用会被回放，不会重复执行）。
        """
        from csagent.react.models import CustomerScope

        trace_id = uuid.uuid4().hex
        scope = CustomerScope(
            session_id=pending.session_id,
            channel=pending.channel or "wecom",
            user_id=pending.user_id,
            auth_method=pending.auth_method,
            chat_type=pending.chat_type,
            chat_id=pending.chat_id,
            trace_id=trace_id,
        )
        # 单独起一份审计缓冲：这次执行不在任何 ReAct 轮次里，不起的话工具登记的
        # 文件摘要 / 脚本存档无处可去，会话审计里也看不到这一步真正做了什么
        audit_token = audit_trace_var.set(AuditTraceBuffer())
        approvals = self.approvals
        completed = False
        try:
            self._record_config_fingerprint()
            started = time.monotonic()
            outcome = await self._tool_registry.execute_approved(
                pending.tool_name,
                dict(pending.arguments),
                scope,
                pending.agent_name or self.agent_name,
                rule_id=pending.rule_id,
            )
            buf = get_audit_buffer()
            if buf is not None:
                buf.add_tool_call(ToolCallRecord(
                    round_no=0,
                    tool_name=pending.tool_name,
                    arguments_preview=json.dumps(pending.arguments, ensure_ascii=False, default=str)[:200],
                    result_preview=(outcome.text or "")[:500],
                    duration_ms=int((time.monotonic() - started) * 1000),
                    status=outcome.status,
                    risk=outcome.risk,
                    decision=outcome.decision,
                    rule_id=outcome.rule_id,
                ))
            ok = outcome.status == "success"
            record_security_event(
                "tool_challenge_executed",
                outcome="success" if ok else "failure",
                actor=pending.operator or pending.user_id,
                session_id=pending.session_id,
                tool=pending.tool_name,
                rule_id=pending.rule_id,
                summary=pending.summary,
                trace_id=trace_id,
            )
            raw = (outcome.text or "").strip() or "（工具没有返回内容）"
            continuation = self._continuation_note(pending.summary, raw, ok)
            if approvals is not None:
                completed = approvals.complete(
                    pending.task_id,
                    result_text=raw,
                    result_status=outcome.status,
                    continuation=continuation,
                )
                if not completed:
                    # 操作已经执行，但结果没处记：续跑取不到说明，不能再让渠道去续跑
                    logger.error("approved_action_result_not_recorded", task_id=pending.task_id)
                    continuation = ""
            # 给用户看的那一行同样要过输出护栏
            shown = self._mask_output(raw if ok else f"操作未完成：{raw}")
            user_text = f"【已批准】{pending.summary}"
            await self._write_audit(
                scope=scope, trace_id=trace_id, user_message=user_text,
                final_reply=shown, status="approved_action",
            )
            return ApprovedAction(
                task_id=pending.task_id,
                status=outcome.status,
                text=shown,
                continuation=continuation,
            )
        finally:
            audit_trace_var.reset(audit_token)
            # 没记下结果（中途出错）也要放开卡片，不能让它一直停在「执行中」
            if approvals is not None and not completed:
                approvals.finish(pending.task_id)

    @staticmethod
    def _continuation_note(summary: str, result: str, ok: bool) -> str:
        """续跑那一轮发给模型的说明：批准了什么、执行结果、接下来怎么做。"""
        clipped = result if len(result) <= 3000 else result[:3000] + "…（已截断）"
        if ok:
            return (
                f"[系统] 用户已批准此前暂停的操作：{summary}。系统已按原参数执行，结果：\n"
                f"{clipped}\n\n"
                "请基于这个结果继续完成用户原来的任务；这项操作已经执行过，不要再重复调用。"
                "如果原任务已经完成，简要告诉用户结果即可。"
            )
        return (
            f"[系统] 用户已批准此前暂停的操作：{summary}，但执行没有成功：\n"
            f"{clipped}\n\n"
            "请告诉用户失败的原因；原任务里不依赖这一步的部分可以继续完成，"
            "依赖它的部分说明做不了，不要自行重试。"
        )

    def _record_config_fingerprint(self) -> None:
        """登记本轮配置指纹与策略版本，写进审计。"""
        buf = get_audit_buffer()
        if buf is None:
            return
        try:
            buf.config_digest = turn_config_digest(
                self.build_system_prompt(), self._list_allowed_tool_names()
            )
        except Exception as exc:  # noqa: BLE001 - 指纹算不出来不能拖垮一轮对话
            logger.warning("config_fingerprint_failed", error=str(exc))
        policy = getattr(self._tool_registry, "policy", None)
        buf.policy_version = str(getattr(policy, "label", "") or "")

    def _open_output_stream(self) -> Any:
        """为一路流式输出开一个护栏遮挡器；护栏不支持流式（测试替身）时直通。"""
        factory = getattr(self._output_guard, "stream", None)
        return factory() if callable(factory) else _PassThroughStream()

    def _mask_output(self, text: str) -> str:
        """对一段完整的出站文本做脱敏（思考事件、进度文案、失败提示、落库正文）。"""
        mask = getattr(self._output_guard, "mask", None)
        return mask(text) if text and callable(mask) else text

    def _thought(self, content: str) -> AgentStreamEvent:
        return AgentStreamEvent(event_type=AgentEventType.THOUGHT, content=content)

    def _reasoning(self, content: str) -> AgentStreamEvent:
        return AgentStreamEvent(event_type=AgentEventType.REASONING, content=content)

    def _progress(self, content: str) -> AgentStreamEvent:
        return AgentStreamEvent(event_type=AgentEventType.PROGRESS, content=content)

    def _delta(self, content: str) -> AgentStreamEvent:
        return AgentStreamEvent(event_type=AgentEventType.DELTA, content=content)

    @staticmethod
    def _finished_reply_if_agent_done(
        agent_task: asyncio.Task | None,
        streamed_parts: list[str],
    ) -> str:
        """Agent 已正常跑完时取出完整正文；还在跑或失败则返回空串。

        生成器在落库前被关掉时，用它区分「答完了」和「说到一半断了」。
        只有前者可以不加中断后缀。

        :param agent_task: 本轮 ReAct 任务
        :param streamed_parts: 已经流给客户端的正文片段
        :returns: 完整回复；不能当成功收口时为空串
        """
        if agent_task is None or not agent_task.done() or agent_task.cancelled():
            return ""
        try:
            if agent_task.exception() is not None:
                return ""
            result = agent_task.result()
        except (asyncio.CancelledError, Exception):
            return ""
        final_reply = (getattr(result, "answer_draft", None) or "").strip()
        if not final_reply:
            final_reply = "".join(streamed_parts).strip()
        return final_reply

    @staticmethod
    def _finished_reply_if_agent_done(
        agent_task: asyncio.Task | None,
        streamed_parts: list[str],
    ) -> str:
        """Agent 已正常跑完时取出完整正文；还在跑或失败则返回空串。

        生成器在落库前被关掉时，用它区分「答完了」和「说到一半断了」。
        只有前者可以不加中断后缀。

        :param agent_task: 本轮 ReAct 任务
        :param streamed_parts: 已经流给客户端的正文片段
        :returns: 完整回复；不能当成功收口时为空串
        """
        if agent_task is None or not agent_task.done() or agent_task.cancelled():
            return ""
        try:
            if agent_task.exception() is not None:
                return ""
            result = agent_task.result()
        except (asyncio.CancelledError, Exception):
            return ""
        final_reply = (getattr(result, "answer_draft", None) or "").strip()
        if not final_reply:
            final_reply = "".join(streamed_parts).strip()
        return final_reply

    @staticmethod
    def _format_tool_call_line(tc: Any) -> str | None:
        """把工具快照压成短行；失败/堆栈类结果不注入。"""
        name = str(getattr(tc, "tool_name", "") or "").strip()
        summary = str(getattr(tc, "result_summary", "") or "").strip()
        if not name or not summary:
            return None
        low = summary.lower()
        if summary.startswith(("错误", "失败", "Traceback", "Error", "Exception")):
            return None
        if "脚本执行失败" in summary or "traceback" in low:
            return None
        if len(summary) > 120:
            summary = summary[:120].rstrip() + "…"
        return f"- {name}: {summary}"

    def _record_context_layers(self, frame: ContextFrame) -> None:
        """把本轮各层占用写进审计缓冲（M4.1）。

        预算分配器给出的是上限；组装后的实测长度才是「钱花在哪一层」。
        没有缓冲就跳过——护栏拦截、装配测试都不会建 buffer。

        :param frame: 本轮 ``ContextPipeline.build`` 的产物
        """
        buf = get_audit_buffer()
        if buf is None:
            return
        layers: dict[str, Any] = {}
        if frame.allocation is not None:
            layers.update(frame.allocation.as_audit_dict())
        layers["context_summary"] = self._counter.count_text(frame.context_summary or "")
        layers["checkpoint"] = self._counter.count_text(frame.checkpoint_summary or "")
        layers["persistent"] = self._counter.count_text(frame.persistent_instructions or "")
        buf.set_context_layers(layers)
        # 超过模型窗口 × context_token_high_ratio 就打告警，方便对照审计。
        limit = self._counter.model_context_limit
        occupied = int(layers.get("context_summary") or 0) + int(layers.get("checkpoint") or 0)
        ratio = self._context_token_high_ratio
        if limit and ratio > 0 and occupied > limit * ratio:
            logger.warning(
                "context_token_high",
                occupied=occupied,
                limit=limit,
                ratio=round(occupied / limit, 3),
                threshold=ratio,
            )

    async def _write_audit(
        self, *, scope: Any, trace_id: str, user_message: str,
        final_reply: str, status: str, violations: list[str] | None = None,
    ) -> None:
        record = SessionAuditRecord(
            trace_id=trace_id,
            session_id=scope.session_id,
            channel=scope.channel,
            user_message=user_message,
            final_reply=final_reply,
            status=status,
            domains=[self.domain],
            violations=violations or [],
            **_principal_fields(scope),
        )
        self._audit_logger.fill_from_buffer(record, get_audit_buffer())
        await self._audit_logger.write(record)

    @staticmethod
    def _sent_files(session_id: str) -> list[dict]:
        """本轮 send_file 产出的文件（结构化，供历史里出下载链接）。

        同样只能 peek：真正出队在 router_chat / 企微 bridge。
        """
        try:
            from csagent.channels.outbound_files import peek_outbound_files

            from csagent.channels.outbound_reports import peek_outbound_reports

            files = [
                {"filename": f.filename, "workspace_path": f.workspace_path}
                for f in peek_outbound_files(session_id)
            ]
            # 报告卡片跟文件存同一列，靠 kind 区分（router_chat 的 /history 按它拆开）
            return files + [
                {"kind": "report", **r.as_dict()} for r in peek_outbound_reports(session_id)
            ]
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript_peek_files_failed", error=str(exc))
            return []

    @staticmethod
    def _with_sent_files(assistant_text: str, session_id: str) -> str:
        """给对话记录的助手侧补上本轮发出的文件。

        必须 peek 不能 drain——真正的出队在 router_chat / 企微 bridge 那边，
        这里抢着 drain 会让文件发不出去。此刻队列里的就是本轮 send_file 的产物。
        """
        try:
            from csagent.channels.outbound_files import peek_outbound_files

            from csagent.channels.outbound_reports import peek_outbound_reports

            files = peek_outbound_files(session_id)
            reports = peek_outbound_reports(session_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("transcript_peek_files_failed", error=str(exc))
            return assistant_text
        markers = []
        if files:
            markers.append("[已发送文件] " + "、".join(f.filename for f in files))
        if reports:
            markers.append("[报告] " + "、".join(f"{r.title} v{r.version}" for r in reports))
        if not markers:
            return assistant_text
        marker = "\n".join(markers)
        if not assistant_text.strip():
            return marker
        return assistant_text + "\n\n" + marker

    def _begin_turn(self, session_id: str) -> TurnHandle | None:
        """向记忆层预留本轮的 episode 与 turn_id（M2.2）。

        只有落盘后端（``SqliteMemoryStore``）有号源；进程内 / Redis 后端与测试替身
        没有 ``begin_turn``，返回 None 即可——回写侧会回落到写时分配，语义同改动前。

        :param session_id: 会话标识
        :returns: 定序凭据；后端不支持或取号失败时为 None
        """
        begin = getattr(self._short_term_memory, "begin_turn", None)
        if begin is None:
            return None
        try:
            return begin(session_id)
        except Exception as exc:  # noqa: BLE001
            # 取号失败不该让整轮对话失败：没有号就退回写时分配，只是顺序退化
            logger.warning("begin_turn_failed", session_id=session_id, error=str(exc))
            return None

    def _append_tool_call_snapshots(
        self, session_id: str, turn: TurnHandle | None = None
    ) -> None:
        """从本次请求的审计缓冲中提取工具调用，写入会话快照（M3.1）。

        直接复用审计侧的 ToolCallRecord（同一份数据，两处消费：审计落盘 + 记忆回溯），
        避免重复采集。快照 store 若未实现 append_tool_calls（如极简测试替身）则静默跳过。

        :param session_id: 会话标识
        :param turn: 本轮定序凭据，让流水挂到正确的轮次上（M2.2）
        """
        append_tool_calls = getattr(self._snapshot_store, "append_tool_calls", None)
        if append_tool_calls is None:
            return
        buffer = get_audit_buffer()
        if buffer is None or not buffer.tool_calls:
            return
        snapshots = [
            ToolCallSnapshot(
                tool_name=str(tc.get("tool_name") or ""),
                arguments_summary=str(tc.get("arguments_preview") or ""),
                result_summary=str(tc.get("result_preview") or ""),
            )
            for tc in buffer.tool_calls
            if tc.get("status") == "success"
        ]
        if snapshots:
            append_tool_calls(session_id, snapshots, turn=turn)

    def _finalize_interrupted_turn(
        self,
        *,
        scope: Any,
        trace_id: str,
        user_text: str,
        streamed_parts: list[str],
        reasoning_parts: list[str],
        transcript_user_text: str | None,
        transcript_user_files: list[dict] | None,
        turn: TurnHandle | None = None,
    ) -> None:
        """客户端断连后的收尾：把已经流给用户的半截回答补进记忆。

        不落盘的话，用户屏幕上留着一段文字、模型侧却零记录，下一轮就会出现
        「你刚才明明说过」这类对不上的情况——断连越频繁，错位越严重。

        **整条链路必须同步**：调用点在生成器关闭（GeneratorExit）时的 finally 里，
        那里 await 会破坏关闭流程，所以审计走 ``write_nowait`` 而不是 ``await write``。
        同理，任何异常都只记告警不外抛——收尾失败不能盖掉正在传播的取消信号。

        :param scope: 当前 CustomerScope，提供 session_id / channel
        :param trace_id: 本轮 trace id，写进审计便于与日志对齐
        :param user_text: 喂给模型的富文本用户输入
        :param streamed_parts: 已经流式吐给客户端的回复片段
        :param transcript_user_text: 只写进对话记录的用户原话；留空回落 ``user_text``
        :param transcript_user_files: 用户本轮上传的附件（仅对话记录侧）
        :param turn: 本轮定序凭据（M2.2）；中断的轮次同样要按到达顺序落位
        """
        stopped = False
        try:
            stopped = scope.session_id in self._stop_requested
            self._stop_requested.discard(scope.session_id)
            partial_reply = "".join(streamed_parts).strip()
            reasoning_text = "".join(reasoning_parts or []).strip()

            # 工具调用无论有没有吐字都要记：下一轮才不会重复查同样的东西。
            self._append_tool_call_snapshots(scope.session_id, turn)

            if not partial_reply and stopped:
                # 用户主动停止时即使什么都没吐也要落一轮：否则用户的问题从历史里消失，
                # 下一句「继续」「换个方式」就没有指代对象。
                partial_reply = STOPPED_EMPTY_REPLY
            elif not partial_reply and reasoning_text:
                # 思考已经推到页面上，但正文 delta 还没开始。以前把这当成
                # 「屏幕是空的」直接丢弃，历史库就没有这一轮。
                excerpt = reasoning_text[:2000]
                partial_reply = "（连接中断，正文未完成。已收到的思考：）\n" + excerpt

            if not partial_reply:
                logger.warning(
                    "turn_interrupted_empty",
                    session_id=scope.session_id,
                    trace_id=trace_id,
                )
                return

            if partial_reply == STOPPED_EMPTY_REPLY:
                assistant_text = partial_reply
            else:
                assistant_text = partial_reply + (STOPPED_SUFFIX if stopped else INTERRUPTED_SUFFIX)
            self._write_back_memory(
                scope=scope,
                user_text=user_text,
                assistant_text=assistant_text,
                transcript_user_text=transcript_user_text,
                transcript_user_files=transcript_user_files,
                interrupted=True,
                turn=turn,
            )

            record = SessionAuditRecord(
                trace_id=trace_id,
                session_id=scope.session_id,
                channel=getattr(scope, "channel", ""),
                user_message=user_text,
                final_reply=assistant_text,
                status="stopped" if stopped else "interrupted",
                domains=[self.domain],
                **_principal_fields(scope),
            )
            self._audit_logger.fill_from_buffer(record, get_audit_buffer())
            self._audit_logger.write_nowait(record)

            logger.warning(
                "turn_stopped_by_user" if stopped else "turn_interrupted",
                session_id=scope.session_id,
                trace_id=trace_id,
                partial_length=len(partial_reply),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "turn_interrupt_finalize_failed",
                session_id=getattr(scope, "session_id", ""),
                trace_id=trace_id,
                error=str(exc),
            )

    def _write_back_memory(
        self,
        *,
        scope: Any,
        user_text: str,
        assistant_text: str,
        transcript_user_text: str | None = None,
        transcript_user_files: list[dict] | None = None,
        interrupted: bool = False,
        turn: TurnHandle | None = None,
    ) -> None:
        """把本轮写进记忆与对话记录（唯一写入点）。

        两侧写的内容不同，且必须保持不同：记忆窗口要富文本（模型靠里面的工作区
        路径继续操作文件），对话记录要人话。文件收发的痕迹只补在记录那一侧，
        不动模型上下文。

        :param scope: 当前 CustomerScope，提供 session_id / channel
        :param user_text: 喂给模型的富文本用户输入
        :param assistant_text: 助手回复
        :param transcript_user_text: 只写进对话记录的用户原话；留空回落 ``user_text``
        :param transcript_user_files: 用户本轮上传的附件（仅对话记录侧）
        :param interrupted: 该轮是否因断连未说完（M0.1），仅统一存储会落这一位
        :param turn: 入口处预留的定序凭据（M2.2），仅统一存储会用到
        """
        display_user = (
            transcript_user_text if transcript_user_text is not None else user_text
        )
        display_assistant = self._with_sent_files(assistant_text, scope.session_id)

        # M1：sqlite 后端下记忆与记录是同一个 store（同一批行，两种读取口径），
        # 必须走单次 append_turn，否则同一轮会被写两遍。
        if self._transcript_store is self._short_term_memory and hasattr(
            self._short_term_memory, "append_turn"
        ):
            self._short_term_memory.append_turn(
                scope.session_id,
                user_text=user_text,
                assistant_text=assistant_text,
                display_user_text=display_user,
                display_assistant_text=display_assistant,
                channel=getattr(scope, "channel", ""),
                user_files=transcript_user_files,
                assistant_files=self._sent_files(scope.session_id),
                interrupted=interrupted,
                turn=turn,
            )
        else:
            # 分离存储：窗口滚出去的轮次，记录里还在（界面能翻，模型不再记得）。
            # 写失败由 store 内部吞掉并告警。
            self._short_term_memory.append(scope.session_id, user_text, assistant_text)
            if self._transcript_store is not None:
                self._transcript_store.append(
                    scope.session_id,
                    display_user,
                    display_assistant,
                    channel=getattr(scope, "channel", ""),
                    user_files=transcript_user_files,
                    assistant_files=self._sent_files(scope.session_id),
                )
        if self._graded is not None and turn is not None:
            try:
                self._graded.schedule_after_turn(
                    scope.session_id,
                    episode_id=turn.episode_id,
                    turn_id=turn.turn_id,
                    user_text=user_text,
                    assistant_text=assistant_text,
                    created_at=turn.started_at,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "l1_schedule_failed",
                    session_id=scope.session_id,
                    error=str(exc),
                )
