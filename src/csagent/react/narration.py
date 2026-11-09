"""旁白检测：判断 LLM 纯文本输出是否为「中间过程旁白」而非终答。

从 loop.py 拆出，集中维护旁白词表，便于配置化与国际化。
"""

from __future__ import annotations

# 旁白是「引导语」，只会出现在文本开头；正文/表格里出现同类词不算旁白——
# 例如用户偏好要求每轮回复附「本轮工具调用」表格时，表格里的 load_skill 和
# 口径说明里的「查看」曾因全文子串扫描把 1900+ 字完整终答误杀为旁白，
# 收口拖了 3 分 40 秒（audit 20260904_174358_181）。因此只扫开头窗口。
NARRATION_HEAD_MAX_CHARS = 80

# 命中这些前缀/短语视为「中间态旁白」，要求模型改调工具或 __final_answer__。
# 只保留「正在查/读/稍等/加载 Skill」类旁白；不要用「我需要先 / 接下来我会 / 让我确认」——
# 追问要素的终答常含这些词，会误拒收（见 audit 33452fc7）。
INTERIM_NARRATION_MARKERS: tuple[str, ...] = (
    "我来查",
    "我先查",
    "我先加载",
    "我来加载",
    "先加载",
    "让我查",
    "让我加载",
    "正在查",
    "正在读",
    "正在读取",
    "正在加载",
    "正在分析",
    "正在整理",
    "正在确认",
    "让我读取",
    "稍等",
    "请稍等",
    "补充查询",
    "我来看一下",
    "我先看一下",
    "让我看一下",
    "查看是否有",
    "看看有没有相关",
    "加载skill",
)

# 短句 + 过程词：无具体结论时也视为旁白（补词表漏检，见 audit b1d42d05）。
INTERIM_PROCESS_TOKENS: tuple[str, ...] = (
    "加载",
    "查看",
    "确认",
    "读取",
    "筛选",
    "查询",
    "分析",
    "整理",
)
INTERIM_SHORT_MAX_CHARS = 80

# 短句旁白需同时带「即将动手」前缀，避免误伤「已根据说明完成回复」等短终答。
INTERIM_INTENT_PREFIXES: tuple[str, ...] = (
    "我先",
    "我来",
    "让我",
    "正在",
    "先加载",
    "先查",
    "稍等",
    "请稍等",
)

# fail-open 阈值：旁白特征命中但文本具备「答案实质」信号（长文 / markdown 表格 /
# 含数字）时放行——宁可偶尔漏一句旁白开头，也不误杀完整终答。
FAIL_OPEN_MIN_CHARS = 300


def _narration_hits(
    text: str,
    *,
    for_final_answer: bool,
    markers: tuple[str, ...] | None,
) -> list[str]:
    """返回命中的旁白特征列表；空列表 = 不是旁白。"""
    stripped = (text or "").strip()
    if not stripped:
        return ["<empty>"]
    compact = stripped.replace(" ", "").replace("　", "")
    _markers = markers if markers is not None else INTERIM_NARRATION_MARKERS
    head = compact[:NARRATION_HEAD_MAX_CHARS]
    hits = [marker for marker in _markers if marker in head]
    if "skill" in head.lower() and any(tok in head for tok in ("加载", "查看", "读取", "正在")):
        hits.append("skill+过程词")
    if for_final_answer:
        return hits
    # 短句且像「我先去查/加载」过程意图、无明显结论信号
    if len(compact) <= INTERIM_SHORT_MAX_CHARS:
        has_intent = any(
            compact.startswith(p) or p in compact[:8] for p in INTERIM_INTENT_PREFIXES
        )
        has_process = any(tok in compact for tok in INTERIM_PROCESS_TOKENS)
        has_substance = any(ch.isdigit() for ch in compact) or any(
            mark in compact
            for mark in ("：", ":", "、", "结论", "建议", "如下", "排名")
        )
        if has_intent and has_process and not has_substance:
            hits.append("short:intent+process")
    return hits


def has_answer_substance(text: str) -> bool:
    """判断文本是否具备「答案实质」信号：长文 / markdown 表格 / 含数字。

    用于旁白检测的 fail-open 决策：特征命中但实质明确的文本放行，
    避免重试把正确终答拖成几分钟的空转（audit 20260904_174358_181）。
    """
    stripped = (text or "").strip()
    if not stripped:
        return False
    compact = stripped.replace(" ", "").replace("　", "")
    if len(compact) >= FAIL_OPEN_MIN_CHARS:
        return True
    if "|" in compact and "\n" in stripped:
        return True
    return any(ch.isdigit() for ch in compact)


def looks_like_interim_narration(
    text: str,
    *,
    for_final_answer: bool = False,
    markers: tuple[str, ...] | None = None,
) -> bool:
    """判断纯文本是否像「还没答完」的中间旁白。

    for_final_answer=True 时只用明确旁白特征，避免误拒短终答
    （如「已根据说明完成回复」）。

    markers 可注入自定义词表（测试/国际化），缺省用内置中文词表。
    """
    return bool(_narration_hits(text, for_final_answer=for_final_answer, markers=markers))
