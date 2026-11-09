"""结构化上下文压缩器：结构化 Checkpoint + Token 精确控制 + Anti-Recursive 状态保留。

吸收借鉴自：
- Claude Code 的 Auto-Compact 机制与 Custom Focus 指令
- OpenAI Codex 的 Handoff Checkpoint 与 Anti-Recursive 状态保留设计
- 结合 TokenCounter 进行 Token 精确预算控制
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections.abc import Callable
from contextvars import Context
from dataclasses import dataclass

import structlog

from csagent.common.keyed_lock import KeyedLocks

from csagent.memory.compactor_cache import (
    CompactorCacheBackend,
    InMemoryCompactorCache,
    SessionSummaryRecord,
)
from csagent.memory.token_counter import TokenCounter
from csagent.prompts.domain_text import MEMORY_DOMAIN_STOPWORDS, extract_domain_entities
from csagent.react.models import ChatMessage

logger = structlog.get_logger(__name__)

# 通用停用词（与行业无关）；领域高频泛词由 domain_text 提供。
_GENERIC_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "我", "你", "他", "和", "就", "都", "而", "及", "与",
        "着", "或", "没有", "我们", "你们", "他们", "什么", "怎么", "吗", "呢", "啊",
        "这个", "那个", "一下", "请问", "麻烦", "帮我", "需要", "可以", "进行", "基于",
        "以及", "还有", "然后", "因为", "所以", "如果", "已经", "不是",
        "the", "a", "an", "is", "are", "to", "of", "and", "for", "in", "on",
    }
)

_STOPWORDS = _GENERIC_STOPWORDS | MEMORY_DOMAIN_STOPWORDS

# 默认结构化 Checkpoint 提示词模板（通用兜底）。
# 领域版本放 configs/prompts/compactor.md —— 该文件存在时优先使用，
# 「必须保留哪类实体」这种行业口径只应出现在那里。
STRUCTURED_COMPACTOR_PROMPT = """你是对话历史结构化压缩器（Handoff Checkpoint 模式）。
请将提供的对话历史提炼为结构化 Checkpoint，供下一轮 Agent 继续执行任务。

请按以下固定结构输出 Markdown（不得包含多余寒暄）：

### 用户目标与状态
- [记录用户核心诉求、任务进度与当前约束]

### 关键决策与已完成操作
- [记录已调用的工具、得出的结论、已确认的技术/业务决策]
- [涉及查询结果时写「查过什么、结论是什么」，不要把返回的数字抄进来]

### 涉及关键实体
- [只记录标识：代码、名称、文件路径、artifact_id]
- [具体数值一律不写；需要指向来源时写成「第 N 轮查询了 X（见工具调用记录 / artifact_id=…）」]

### 未决事项与下一步
- [记录待办、待确认疑问、下一步行动建议]
- [若下一步需要精确数字，写「重新调用工具或 read_artifact」]

重要规则：
1. 保持高度客观浓缩，丢弃寒暄、重复讨论与无效中间排版。
2. **标识不得丢失**；**具体数字必须丢掉**，改写成来源指针。摘要一旦复述数字，下一轮就会把过期值当成当前值答出去。
3. 需要精确数值时，明确指示下一轮重新调用工具或 read_artifact，不要凭本摘要复述数字。
4. 若提供了「已有 Checkpoint」，必须在此基础上进行字段级合并更新，不可遗忘早期关键决策；
   合并时若旧摘要里残留了数字，删掉，改成来源指针。
"""


def _load_compactor_system_prompt() -> str:
    """优先读取 configs/prompts/compactor.md，缺失时用内置模板。"""
    try:
        from csagent.infra.config import CONFIGS_DIR

        path = CONFIGS_DIR / "prompts" / "compactor.md"
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
    except OSError:
        pass
    return STRUCTURED_COMPACTOR_PROMPT


@dataclass(frozen=True)
class CompactorConfig:
    """压缩器配置。"""

    window: int = 12
    # Token 预算配置
    summary_max_tokens: int = 4000
    # 最近窗口内「非末条」消息的角色基准上限；有预算时会按预算放宽（见 _format_messages）
    recent_user_max_tokens: int = 400
    recent_assistant_max_tokens: int = 600
    # 末条消息完整保留，这里只是防御性天花板：正常流量远达不到，
    # 用于挡住异常超长输出把窗口吃光。
    recent_latest_max_tokens: int = 8000
    # 预算极紧时每条消息仍要保留的下限，避免整条消失
    recent_min_tokens: int = 80
    # 关键词片段补充（摘要缺失时）
    keyword_max_inject: int = 2
    keyword_max_snippet_tokens: int = 80
    keyword_min_overlap: int = 2
    summarize_msg_max_tokens: int = 300


@dataclass
class CompactedContext:
    """压缩构建后的上下文产物。"""

    summary_block: str = ""
    recent_block: str = ""
    total_tokens: int = 0
    needs_refresh: bool = False

    @property
    def full_text(self) -> str:
        parts = [p for p in (self.summary_block, self.recent_block) if p]
        return "\n\n".join(parts)


def _digest(msg: ChatMessage) -> str:
    raw = f"{msg.role}\n{msg.content or ''}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _tokenize(text: str) -> set[str]:
    """轻量分词：中文 2-gram + 英文词 + 领域实体（正则由 domain_text 配置）。"""
    if not text:
        return set()
    tokens: set[str] = set(extract_domain_entities(text))
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.add(run)
        else:
            tokens.update(run[i : i + 2] for i in range(len(run) - 1))
    for word in re.findall(r"[A-Za-z]{2,}|[A-Za-z]*\d+[A-Za-z\d]*", text):
        if len(word) >= 2:
            tokens.add(word.lower())
    return tokens - _STOPWORDS


def _looks_like_long_dump(text: str) -> bool:
    if not text:
        return False
    if text.count("|") >= 8 and text.count("\n") >= 6:
        return True
    if len(text) > 2500 and text.count("\n") >= 20:
        return True
    return False


class StructuredCompactor:
    """结构化上下文压缩器。

    核心特性：
    1. 结构化 Handoff Checkpoint 输出
    2. Anti-Recursive 防递归衰减（字段合并）
    3. Query-Aware 焦点引导
    4. Token 精确裁剪与预算感知
    5. 分布式缓存（Redis / 内存）支持
    """

    def __init__(
        self,
        llm_client=None,
        token_counter: TokenCounter | None = None,
        config: CompactorConfig | None = None,
        cache_backend: CompactorCacheBackend | None = None,
        *,
        enabled: bool = True,
        tool_digest_provider: Callable[[str], str] | None = None,
    ) -> None:
        """
        Args:
            tool_digest_provider: ``session_id -> 工具调用流水文本``。注入而非由调用方
                逐处传参——refresh 有「水位线触发」「写回记忆」「摘要缺失预热」三条
                入口，逐处传参必然漏一条，导致 Checkpoint 时有时无地丢掉工具信息。
        """
        self._llm = llm_client
        self._counter = token_counter or TokenCounter()
        self._config = config or CompactorConfig()
        self._cache = cache_backend or InMemoryCompactorCache()
        self._enabled = enabled and llm_client is not None
        self._tool_digest_provider = tool_digest_provider
        # 用完即删，不随历史会话数增长（见 common/keyed_lock.py）
        self._locks = KeyedLocks()

    def _tool_digest(self, session_id: str) -> str:
        if self._tool_digest_provider is None:
            return ""
        try:
            return self._tool_digest_provider(session_id) or ""
        except Exception as exc:  # noqa: BLE001
            logger.warning("tool_digest_provider_failed", session_id=session_id, error=str(exc))
            return ""

    def peek_cached_summary(self, session_id: str) -> str:
        """读取已缓存 Checkpoint 原文（供预算分配器估算占用）。"""
        if not session_id:
            return ""
        cached = self._cache.get(session_id)
        return (cached.summary or "") if cached is not None else ""

    def build_context(
        self,
        session_id: str,
        history: list[ChatMessage],
        *,
        current_query: str = "",
        token_budget: int | None = None,
        recent_token_budget: int | None = None,
    ) -> CompactedContext:
        """组装注入主 Agent 的上下文（同步只读缓存，不调 LLM）。

        Args:
            token_budget: Checkpoint（Tier 3）的截断预算。
            recent_token_budget: 最近对话（Tier 1）的预算，来自 ContextBudgetAllocator。
                缺省时退化为配置里的角色基准上限。

        Returns:
            CompactedContext: summary_block 给 Prompt Cache BP3，recent_block 进动态尾部。
        """
        if not history:
            return CompactedContext()

        window = max(1, self._config.window)
        aged = history[:-window] if len(history) > window else []
        recent = history[-window:] if aged else history

        summary_block = ""
        extra_parts: list[str] = []
        cached = self._cache.get(session_id) if session_id else None
        aged_digests = [_digest(m) for m in aged]
        needs_refresh = False

        # 1. 结构化摘要注入（独立块，便于 Cache Breakpoint）
        if aged and self._enabled:
            summary_ready = (
                cached is not None
                and bool(cached.summary)
                and cached.covered_digests == aged_digests
            )
            if cached is not None and cached.summary:
                summary_text = cached.summary
                if token_budget is not None:
                    summary_text = self._counter.truncate_to_tokens(summary_text, token_budget)
                status = "结构化压缩" if summary_ready else "待更新"
                summary_block = (
                    f"## 会话历史 Checkpoint（{status}，覆盖前 {cached.covered_rounds} 条早期消息）\n"
                    f"{summary_text}"
                )
                if not summary_ready:
                    needs_refresh = True
            else:
                summary_block = (
                    f"## 会话历史 Checkpoint\n"
                    f"（另有 {len(aged)} 条更早消息；请优先依据下方最近对话）"
                )
                needs_refresh = True
        elif aged and not self._enabled:
            summary_block = (
                f"## 会话历史 Checkpoint\n"
                f"（智能压缩未启用；另有 {len(aged)} 条更早消息未注入）"
            )

        # 2. 关键词召回兜底（仅在无可用摘要时补充到动态尾部）
        has_summary = bool(cached.summary) if cached is not None else False
        if aged and current_query and not has_summary:
            relevant = self._find_relevant_older(aged, current_query)
            if relevant:
                extra_parts.append(
                    "## 相关历史片段（按关键词匹配补充）\n" + "\n".join(relevant)
                )

        # 3. 最近对话原文（Token 精确截断）
        recent_block = self._format_messages(recent, token_budget=recent_token_budget)
        if extra_parts:
            recent_block = "\n\n".join([*extra_parts, f"## 最近对话\n{recent_block}" if recent_block else ""])
        elif recent_block:
            recent_block = "## 最近对话\n" + recent_block

        full = CompactedContext(
            summary_block=summary_block,
            recent_block=recent_block,
            needs_refresh=needs_refresh,
        )
        full.total_tokens = self._counter.count_text(full.full_text)
        return full

    def _role_cap(self, role: str) -> int:
        if role == "user":
            return self._config.recent_user_max_tokens
        return self._config.recent_assistant_max_tokens

    def _format_messages(
        self, messages: list[ChatMessage], *, token_budget: int | None = None
    ) -> str:
        """渲染最近窗口。

        两条规则：
        1. **末条完整保留**。它是指代消解的唯一可靠来源——用户说「把这 11 只导出」
           时，那 11 只只存在于上一条助手回复里。过去按 250 token 一刀切，
           清单正好被截在表头之后，下一轮只能把整个分析重跑一遍
           （见 audit wecom_BoWen req#108）。
        2. 其余消息取「角色基准上限」与「预算均分份额」的较大值：窗口有余量就放宽，
           预算吃紧则收敛到均分份额，但不低于 recent_min_tokens。
        """
        if not messages:
            return ""

        *older, latest = messages
        latest_line = (
            f"{latest.role}: "
            f"{self._counter.truncate_to_tokens(latest.content or '', self._config.recent_latest_max_tokens)}"
        )

        share: int | None = None
        if token_budget is not None and token_budget > 0 and older:
            left = token_budget - self._counter.count_text(latest_line)
            share = max(self._config.recent_min_tokens, left // len(older))

        lines: list[str] = []
        for msg in older:
            cap = self._role_cap(msg.role)
            if share is not None:
                cap = max(cap, share) if share > cap else share
            lines.append(
                f"{msg.role}: {self._counter.truncate_to_tokens(msg.content or '', cap)}"
            )
        lines.append(latest_line)
        return "\n".join(lines)

    def _find_relevant_older(self, older: list[ChatMessage], current_query: str) -> list[str]:
        cfg = self._config
        query_tokens = _tokenize(current_query)
        if not query_tokens:
            return []

        scored: list[tuple[float, int, ChatMessage]] = []
        for idx, msg in enumerate(older):
            if msg.role not in ("user", "assistant"):
                continue
            content = msg.content or ""
            if _looks_like_long_dump(content) and msg.role == "assistant":
                continue
            overlap = len(_tokenize(content) & query_tokens)
            if overlap < cfg.keyword_min_overlap:
                continue
            score = float(overlap)
            if msg.role == "user":
                score *= 3.0
            else:
                score *= 0.6
            # 同一领域实体（如产品标识符）复现：强相关信号
            q_entities = extract_domain_entities(current_query)
            if q_entities and q_entities & extract_domain_entities(content):
                score += 5.0
            scored.append((score, idx, msg))

        if not scored:
            return []

        scored.sort(key=lambda item: (-item[0], -item[1]))
        top = scored[: cfg.keyword_max_inject]
        top.sort(key=lambda item: item[1])

        snippets: list[str] = []
        for _score, idx, msg in top:
            content = self._counter.truncate_to_tokens(
                (msg.content or "").strip(), cfg.keyword_max_snippet_tokens
            )
            snippets.append(f"[第{idx + 1}条 {msg.role}] {content}")
        return snippets

    def schedule_refresh(
        self,
        session_id: str,
        history: list[ChatMessage],
        *,
        focus: str = "",
    ) -> None:
        """后台异步刷新摘要。

        用空 ``Context()`` 起任务，避免继承本请求的 ``audit_trace_var``——
        压缩器 LLM 的用量不应记进当前用户这一轮（M4.1）。
        """
        if not self._enabled or not session_id:
            return
        window = max(1, self._config.window)
        # 短对话（总条数少于保留窗口*2，且总Token很小）不触发LLM压缩，避免打招呼白白浪费推理资源
        if len(history) <= window * 2:
            return
        aged = history[:-window] if len(history) > window else []
        if not aged:
            return

        # 检查总 Token 数：如果尚未达到 2000 tokens，不值得调用昂贵 LLM 进行总结
        if self._counter is not None:
            total_tokens = sum(self._counter.count_text(m.content or "") for m in aged)
            if total_tokens < 2000:
                return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self.refresh(session_id, history, focus=focus), context=Context())

    async def refresh(
        self,
        session_id: str,
        history: list[ChatMessage],
        *,
        focus: str = "",
    ) -> None:
        """执行增量/全量 Checkpoint 提炼。"""
        if not self._enabled or not session_id:
            return
        window = max(1, self._config.window)
        aged = history[:-window] if len(history) > window else []
        if not aged:
            return

        async with self._locks.hold(session_id):
            aged_digests = [ _digest(m) for m in aged ]
            cached = self._cache.get(session_id)
            if cached is not None and cached.covered_digests == aged_digests and not focus:
                return

            prev_n = len(cached.covered_digests) if cached else 0
            incremental = (
                cached is not None
                and 0 < prev_n < len(aged_digests)
                and cached.covered_digests == aged_digests[:prev_n]
                and bool(cached.summary)
            )
            new_msgs = aged[prev_n:] if incremental else aged
            prev_summary = cached.summary if (incremental and cached) else ""

            summary = await self._summarize(
                prev_summary,
                new_msgs,
                focus=focus,
                tool_digest=self._tool_digest(session_id),
            )
            if summary is None:
                logger.warning("structured_compactor_llm_failed", session_id=session_id)
                return

            cleaned = summary.strip()
            if not cleaned:
                logger.warning("structured_compactor_empty", session_id=session_id)
                return

            # Token 精确截断
            clipped = self._counter.truncate_to_tokens(
                cleaned, self._config.summary_max_tokens
            )
            record = SessionSummaryRecord(
                summary=clipped,
                covered_digests=aged_digests,
                covered_rounds=len(aged_digests),
            )
            self._cache.set(session_id, record)
            logger.info(
                "structured_compactor_refreshed",
                session_id=session_id,
                covered_rounds=len(aged_digests),
                tokens=self._counter.count_text(clipped),
            )

    async def _summarize(
        self,
        prev_summary: str,
        messages: list[ChatMessage],
        *,
        focus: str = "",
        tool_digest: str = "",
    ) -> str | None:
        """调用 LLM 生成/更新结构化 Checkpoint。"""
        convo_lines = []
        for m in messages:
            content = self._counter.truncate_to_tokens(
                m.content or "", self._config.summarize_msg_max_tokens
            )
            convo_lines.append(f"{m.role}: {content}")
        convo = "\n".join(convo_lines)

        user_parts: list[str] = []
        if prev_summary:
            user_parts.append(f"## 已有 Checkpoint\n{prev_summary}")
        user_parts.append(f"## 新增对话\n{convo}")
        if tool_digest:
            # Checkpoint 是「到目前为止的状态」而非严格的对话切片，故这里给的是本会话
            # 累计工具流水，用于填「关键决策与已完成操作」；它可能略超出上面的对话切片。
            user_parts.append(
                "## 本会话已执行的工具调用（用于填写「关键决策与已完成操作」）\n"
                + tool_digest
            )
        if focus:
            user_parts.append(f"## 重点关注方向 (Focus)\n请重点关注并提炼：{focus}")

        user_parts.append(
            f"请输出合并更新后的结构化 Checkpoint，内容严谨精炼，总 Token 预算控制在 {self._config.summary_max_tokens} 以内。"
        )

        try:
            return await self._llm.chat(
                [
                    {"role": "system", "content": _load_compactor_system_prompt()},
                    {"role": "user", "content": "\n\n".join(user_parts)},
                ],
                json_mode=False,
                temperature=0.1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("structured_compactor_error", error=str(exc))
            return None
