"""Token 精确计数器：基于 tiktoken 实现，替代全链路字符计数。

用法
----
counter = TokenCounter("gpt-4o")
n = counter.count_text("你好世界")
truncated = counter.truncate_to_tokens("很长的文本...", max_tokens=100)
"""

from __future__ import annotations

import re
from typing import Any

import structlog

logger = structlog.get_logger(__name__)

# 模型 → 上下文窗口大小（tokens）
# 按需扩充；未匹配时走 _DEFAULT_CONTEXT_LIMIT
_MODEL_CONTEXT_LIMITS: dict[str, int] = {
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    # DeepSeek
    "deepseek-v4-flash": 1_000_000,
    "deepseek-v4": 1_000_000,
    "deepseek-v3": 128_000,
    "deepseek-chat": 128_000,
    "deepseek-coder": 128_000,
    "deepseek-reasoner": 128_000,
    # Anthropic
    "claude-3-opus": 200_000,
    "claude-3-sonnet": 200_000,
    "claude-3-haiku": 200_000,
    "claude-3.5-sonnet": 200_000,
    "claude-4-sonnet": 200_000,
    # Google
    "gemini-1.5-pro": 1_000_000,
    "gemini-1.5-flash": 1_000_000,
    "gemini-2.0-flash": 1_000_000,
    # Qwen
    "qwen-turbo": 131_072,
    "qwen-plus": 131_072,
    "qwen-max": 32_768,
}

_DEFAULT_CONTEXT_LIMIT = 64_000

# count_text 缓存：短文本直接算（缓存反而更贵），长文本按内容缓存
_CACHE_MIN_CHARS = 256
_CACHE_MAX_ENTRIES = 2048
# 缓存以字符串本身为键，会延长这些字符串的生命周期；按总字符数封顶（约十几 MB）
_CACHE_MAX_CHARS = 4_000_000

# 每条消息的 overhead tokens（role + 分隔符等），OpenAI Chat 格式约 4 tokens
_MSG_OVERHEAD = 4
# 整个 messages 数组的 overhead
_REPLY_OVERHEAD = 3

# 自然断句分隔符（优先级从高到低）
_BREAK_SEPS = ("\n\n", "\n", "。", "；", ".", ";", "，", ",")


def _try_import_tiktoken():
    """延迟导入 tiktoken，未安装时降级为字符估算。"""
    try:
        import tiktoken
        return tiktoken
    except ImportError:
        return None


class TokenCounter:
    """基于 tiktoken 的 Token 精确计数器。

    若 tiktoken 未安装，自动降级为启发式字符估算（中文 ≈ 2 tokens/字，英文 ≈ 0.75 tokens/word）。
    """

    def __init__(
        self,
        model_name: str = "gpt-4o",
        *,
        context_limit: int | None = None,
        context_cap: int | None = None,
    ) -> None:
        """Params:
            model_name: 用于选择 encoding 与默认窗口。
            context_limit: 显式覆盖模型窗口；None 则读环境变量或内置表。
            context_cap: 只对**按模型名推断**出的窗口封顶。标称 1M 的模型在几十万
                token 时就明显变慢、注意力发散，压缩水位不能按标称值算。
                显式覆盖（context_limit / 环境变量）不受它限制。
        """
        self._model_name = model_name
        self._context_limit_override = int(context_limit) if context_limit and context_limit > 0 else None
        self._context_cap = int(context_cap) if context_cap and context_cap > 0 else None
        self._encoding = None
        self._fallback = False
        self._count_cache: dict[str, int] = {}
        self._count_cache_chars = 0

        tiktoken = _try_import_tiktoken()
        if tiktoken is not None:
            try:
                self._encoding = tiktoken.encoding_for_model(model_name)
            except KeyError:
                # 模型名不在 tiktoken 注册表，尝试通用 encoding
                try:
                    self._encoding = tiktoken.get_encoding("cl100k_base")
                except Exception:
                    self._fallback = True
        else:
            self._fallback = True
            logger.info("tiktoken_not_installed", hint="pip install tiktoken; 已降级为字符估算")

    @property
    def model_context_limit(self) -> int:
        """返回当前模型的上下文窗口大小（tokens）。优先读取构造覆盖值 / 环境变量。"""
        import os
        if self._context_limit_override:
            return self._context_limit_override
        env_limit = os.getenv("LLM_CONTEXT_LIMIT") or os.getenv("MODEL_CONTEXT_LIMIT")
        if env_limit:
            try:
                val = int(env_limit.strip())
                if val > 0:
                    return val
            except ValueError:
                pass

        inferred = self._inferred_context_limit()
        if self._context_cap:
            return min(inferred, self._context_cap)
        return inferred

    def _inferred_context_limit(self) -> int:
        # 精确匹配
        if self._model_name in _MODEL_CONTEXT_LIMITS:
            return _MODEL_CONTEXT_LIMITS[self._model_name]
        # 前缀匹配（如 deepseek-v4-flash, gpt-4o-2024-08-06）
        for prefix, limit in _MODEL_CONTEXT_LIMITS.items():
            if self._model_name.startswith(prefix):
                return limit
        # 若包含 1m 或 flash / 1000k 关键词
        if "1m" in self._model_name.lower() or "1000k" in self._model_name.lower():
            return 1_000_000
        return _DEFAULT_CONTEXT_LIMIT

    def count_text(self, text: str) -> int:
        """计算纯文本的 Token 数。

        长文本按内容缓存：ReAct 每轮都要对整个 messages 重算水位，而其中绝大多数
        消息（工具结果、历史）是同一批字符串对象——str 的 hash 在对象上只算一次，
        命中缓存几乎零成本，省掉的是反复的分词 / 估算。
        """
        if not text:
            return 0
        if len(text) < _CACHE_MIN_CHARS:
            return self._count_uncached(text)
        cached = self._count_cache.get(text)
        if cached is not None:
            return cached
        n = self._count_uncached(text)
        if (
            len(self._count_cache) >= _CACHE_MAX_ENTRIES
            or self._count_cache_chars + len(text) > _CACHE_MAX_CHARS
        ):
            # 简单整体清空：命中主要来自同一轮循环内的重复计算，不需要精细 LRU
            self._count_cache.clear()
            self._count_cache_chars = 0
        self._count_cache[text] = n
        self._count_cache_chars += len(text)
        return n

    def _count_uncached(self, text: str) -> int:
        if self._encoding is not None:
            return len(self._encoding.encode(text))
        return self._estimate_tokens(text)

    def count_messages(self, messages: list[dict[str, Any]]) -> int:
        """计算 OpenAI Chat 格式消息列表的总 Token 数。

        包含每条消息的 role/name overhead 和整个数组的 reply overhead。
        """
        total = _REPLY_OVERHEAD
        for msg in messages:
            total += _MSG_OVERHEAD
            content = msg.get("content")
            if isinstance(content, str):
                total += self.count_text(content)
            elif isinstance(content, list):
                # 多模态消息（text + image_url parts）
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        total += self.count_text(part.get("text", ""))
                    elif isinstance(part, dict) and part.get("type") == "image_url":
                        total += 85  # 低分辨率图片约 85 tokens
            if msg.get("name"):
                total += self.count_text(msg["name"])
            # tool_calls
            tool_calls = msg.get("tool_calls")
            if tool_calls:
                for tc in tool_calls:
                    fn = tc.get("function") or {}
                    total += self.count_text(fn.get("name", ""))
                    total += self.count_text(fn.get("arguments", ""))
                    total += 4  # tool_call 结构 overhead
        return total

    def count_tools(self, tools: list[dict[str, Any]]) -> int:
        """估算工具 schema 定义占用的 Token 数。"""
        if not tools:
            return 0
        # 工具 schema 序列化后的 token 数
        import json
        text = json.dumps(tools, ensure_ascii=False)
        return self.count_text(text)

    def truncate_to_tokens(self, text: str, max_tokens: int) -> str:
        """按 Token 精确截断文本，优先在自然断句处截断。

        Returns:
            截断后的文本。如果发生了截断，末尾会加 '…'。
        """
        if not text or max_tokens <= 0:
            return ""
        current = self.count_text(text)
        if current <= max_tokens:
            return text

        if self._encoding is not None:
            tokens = self._encoding.encode(text)
            truncated_tokens = tokens[:max_tokens]
            base = self._encoding.decode(truncated_tokens)
        else:
            # 降级：按比例字符截断
            ratio = max_tokens / max(current, 1)
            char_limit = int(len(text) * ratio)
            base = text[:char_limit]

        # 尝试在自然断句处截断
        min_keep = max(20, len(base) // 3)
        for sep in _BREAK_SEPS:
            idx = base.rfind(sep)
            if idx >= min_keep:
                end = idx if sep in ("\n", "\n\n") else idx + len(sep)
                return base[:end] + "…"
        return base.rstrip() + "…"

    def tokens_to_chars_estimate(self, tokens: int) -> int:
        """将 Token 数估算为字符数（用于向后兼容的配置转换）。

        中文约 1.5 字/token，英文约 4 字符/token，取混合估算 ≈ 2.5 字符/token。
        """
        return int(tokens * 2.5)

    @staticmethod
    def _estimate_tokens(text: str) -> int:
        """降级估算：中文 ≈ 1.5 token/字，英文 ≈ 0.25 token/字符，数字/标点按字符。"""
        if not text:
            return 0
        # 粗略拆分中文与非中文
        chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
        other_chars = len(text) - chinese_chars
        return int(chinese_chars * 1.5 + other_chars * 0.4) + 1
