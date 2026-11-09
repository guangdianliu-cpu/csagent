"""企微桥接轻量单测（不依赖 aibot SDK）。"""

from __future__ import annotations

from csagent.wecom.bridge import (
    _AT_MENTION_RE,
    _compose_user_text,
    _extract_quote_text,
    _session_id_from_body,
    _stream_bubble_content,
    append_round_elapsed,
    format_round_elapsed,
)


def test_strip_at_mention():
    raw = "@产品助手 110022净值多少"
    text = _AT_MENTION_RE.sub("", raw).strip()
    assert text == "110022净值多少"


def test_strip_at_mention_only_leaves_empty():
    """群聊仅 @ 机器人、无正文时清洗后为空，bridge 应对此发引导回复。"""
    assert _AT_MENTION_RE.sub("", "@产品助手").strip() == ""
    assert _AT_MENTION_RE.sub("", "@产品助手 ").strip() == ""
    assert _AT_MENTION_RE.sub("", "<@woXXXX>").strip() == ""


def test_strip_angle_at_userid():
    """流式正文误拼的 <@userid> 应被清洗，避免当用户问题再喂给 Agent。"""
    raw = "<@wo5SXpZAAAW1cuIB0BSBq1t4-UrPuA> 你好"
    text = _AT_MENTION_RE.sub("", raw).strip()
    assert text == "你好"


def test_extract_quote_text_types():
    assert (
        _extract_quote_text({"msgtype": "text", "text": {"content": " 原文 "}})
        == "原文"
    )
    assert (
        _extract_quote_text(
            {"msgtype": "voice", "voice": {"content": "语音转写结果"}}
        )
        == "语音转写结果"
    )
    assert _extract_quote_text({"msgtype": "image"}) == "[图片]"
    assert (
        _extract_quote_text(
            {
                "msgtype": "mixed",
                "mixed": {
                    "msg_item": [
                        {"msgtype": "text", "text": {"content": "@机器人 说明"}},
                        {"msgtype": "image"},
                    ]
                },
            }
        )
        == "说明\n[图片]"
    )


def test_compose_user_text_with_quote():
    assert _compose_user_text(user_text="帮我解释", quote=None) == "帮我解释"
    assert (
        _compose_user_text(
            user_text="帮我解释",
            quote={"msgtype": "text", "text": {"content": "今日净值1.23"}},
        )
        == "【引用消息】\n今日净值1.23\n\n【用户问题】\n帮我解释"
    )
    assert (
        _compose_user_text(
            user_text="",
            quote={"msgtype": "text", "text": {"content": "今日净值1.23"}},
        )
        == "用户引用了以下消息，请结合引用内容回答：\n今日净值1.23"
    )


def test_group_session_shared_by_members():
    """群里不同成员落在同一个会话：记忆、工作区整群共用。"""
    def sid(userid: str) -> tuple[str, str, bool]:
        return _session_id_from_body(
            {"chattype": "group", "chatid": "wrXXXX", "from": {"userid": userid}}
        )

    assert sid("zhangsan") == ("wecom:group:wrXXXX", "zhangsan", True)
    assert sid("lisi")[0] == "wecom:group:wrXXXX"


def test_single_chat_session_per_user():
    assert _session_id_from_body(
        {"chattype": "single", "from": {"userid": "zhangsan"}}
    ) == ("wecom:zhangsan", "zhangsan", False)


def test_compose_progress_drops_status_once_checklist_exists():
    from csagent.wecom.bridge import _compose_progress

    checklist = "待办\n✅ 查净值\n☐ 写报告"
    assert _compose_progress(checklist=checklist, status="正在搜索...") == checklist
    assert _compose_progress(checklist="", status="正在搜索...") == "正在搜索..."
    assert _compose_progress(checklist=checklist, status="") == checklist


def test_stream_bubble_prefers_progress_then_answer():
    assert _stream_bubble_content(answer="", progress="", finish=False) == "…"
    assert (
        _stream_bubble_content(
            answer="", progress="正在查询产品信息...", finish=False
        )
        == "正在查询产品信息..."
    )
    assert (
        _stream_bubble_content(
            answer="净值 1.23", progress="正在查询产品信息...", finish=False
        )
        == "净值 1.23"
    )
    # finish 时不把进度留在最终气泡
    assert (
        _stream_bubble_content(
            answer="", progress="正在查询产品信息...", finish=True
        )
        == "抱歉，暂时没有生成回复。"
    )


def test_format_round_elapsed_matches_debug_copy():
    assert format_round_elapsed(12.4) == "【本轮共耗时】：12秒"
    assert format_round_elapsed(12.5) == "【本轮共耗时】：13秒"
    assert format_round_elapsed(0.2) == "【本轮共耗时】：0秒"


def test_append_round_elapsed_goes_after_answer():
    text = append_round_elapsed("净值 1.23", 8.2)
    assert text.startswith("净值 1.23")
    assert text.endswith("【本轮共耗时】：8秒")
    assert "\n\n【本轮共耗时】：8秒" in text
    assert append_round_elapsed("", 3) == "【本轮共耗时】：3秒"


def test_append_round_elapsed_skips_when_disabled():
    assert append_round_elapsed("净值 1.23", 8.2, enabled=False) == "净值 1.23"
    assert append_round_elapsed("", 3, enabled=False) == ""
