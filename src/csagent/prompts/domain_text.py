"""产品领域层：从 configs/domain_text.yaml 加载领域口径。

核心代码只允许引用此处的名字，不得内联任何领域口径——文案、Skill 名、
行业关键词、行业字段名都算。判断标准很简单：**换一个行业要改的东西，
就该在 configs/domain_text.yaml 里。**

本文件是通用加载器，不含任何领域内容——两个同源产品共享同一份。
"""

from __future__ import annotations

import re

import yaml

from csagent.infra.config import CONFIGS_DIR


# 本模块直接下标取用（非 .get）的必需键。多产品形态各有一份 domain_text_*.yaml，
# 新增键时容易只改了当前转正的那份、漏掉待机的另外几份——那样切产品后启动会在
# import 链深处抛裸 KeyError（见 2026-08-16 切法律形态时的 handle_timeout_reply）。
# 这里一次性校验并把**所有**缺失键连同文件路径报出来。
REQUIRED_KEYS: tuple[str, ...] = (
    "file_only_instruction",
    "attachment_usage_hint",
    "attachment_lost_hint",
    "empty_inbound_guide",
    "fallback_timeout_answer",
    "fallback_failed_answer",
    "handle_timeout_reply",
    "transport_failure_reply",
    "handle_failed_reply",
    "handoff_tool_description",
    "memory_summarizer_prompt",
    "web_search_note",
)


def _load() -> dict:
    """从 configs/domain_text.yaml 加载领域配置，应用 profile overlay，校验必需键。"""
    path = CONFIGS_DIR / "domain_text.yaml"
    if not path.is_file():
        raise FileNotFoundError(f"领域配置文件不存在: {path}")
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 应用 profile overlay 的 domain_text 覆盖
    try:
        from csagent.infra.agents_config import load_domain_text_overrides
        overrides = load_domain_text_overrides()
        data.update(overrides)
    except Exception:  # noqa: BLE001
        pass  # overlay 是可选的

    missing = [k for k in REQUIRED_KEYS if k not in data]
    if missing:
        raise KeyError(
            f"领域配置 {path} 缺少必需键: {missing}。"
            "若刚切换过产品形态，请检查 configs/profiles/ 下对应的 overlay 是否补齐了这些键。"
        )
    return data


_data = _load()


# ---------------------------------------------------------------- 文案
# 下游 import 名不变，向后兼容。

FILE_ONLY_INSTRUCTION: str = _data["file_only_instruction"]

ATTACHMENT_USAGE_HINT: str = _data["attachment_usage_hint"]

ATTACHMENT_LOST_HINT: str = _data["attachment_lost_hint"]

EMPTY_INBOUND_GUIDE: str = _data["empty_inbound_guide"]

FALLBACK_TIMEOUT_ANSWER: str = _data["fallback_timeout_answer"]
FALLBACK_FAILED_ANSWER: str = _data["fallback_failed_answer"]

# 通道级兜底（消息没送出去），与上面的 ReAct 兜底（答不出来）区分开。
HANDLE_TIMEOUT_REPLY: str = _data["handle_timeout_reply"]
TRANSPORT_FAILURE_REPLY: str = _data["transport_failure_reply"]
HANDLE_FAILED_REPLY: str = _data["handle_failed_reply"]

HANDOFF_TOOL_DESCRIPTION: str = _data["handoff_tool_description"]

MEMORY_SUMMARIZER_PROMPT: str = _data["memory_summarizer_prompt"]

WEB_SEARCH_NOTE: str = _data["web_search_note"]

# ---------------------------------------------------------------- 结构

# json_preview_keys：YAML dict[str, list[str]] → dict[str, tuple[str, ...]
JSON_PREVIEW_KEYS: dict[str, tuple[str, ...]] = {
    k: tuple(v) for k, v in (_data.get("json_preview_keys") or {}).items()
}


# ---------------------------------------------------------------- 记忆层词表

# memory_domain：记忆/压缩/召回层的领域词表。
# 通用源码（compactor / summarizer / context_selector）只引用这两个常量，
# 自身不认识任何行业实体——缺配置时退化为「无领域实体、无领域停用词」，
# 纯通用分词照常工作。
_memory_domain = _data.get("memory_domain") or {}

# 分词时整体保留、召回打分时强加权的领域实体正则（已编译）。
MEMORY_ENTITY_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(str(p)) for p in (_memory_domain.get("entity_patterns") or [])
)

# 领域高频泛词：区分度低，关键词召回时剔除以降噪。
MEMORY_DOMAIN_STOPWORDS: frozenset[str] = frozenset(
    str(w) for w in (_memory_domain.get("stopwords") or [])
)



def extract_domain_entities(text: str) -> set[str]:
    """按领域正则抽取实体串；无配置时返回空集。

    正则可能带捕获组（如 6 位代码），统一取整体匹配文本，避免 findall
    在有/无分组时返回结构不一致。
    """
    if not text or not MEMORY_ENTITY_PATTERNS:
        return set()
    found: set[str] = set()
    for pattern in MEMORY_ENTITY_PATTERNS:
        found.update(m.group(0) for m in pattern.finditer(text))
    return found
