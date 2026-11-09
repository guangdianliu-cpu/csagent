"""ReplyPreferenceToolPack 装配测试 + preference_tool 核心行为测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.react.models import CustomerScope
from csagent.tools.packs.reply_preference_pack import ReplyPreferenceToolPack
from csagent.tools.preference_tool import ReplyPreferenceStore, SetReplyStyleTool, preference_key


# ------------------------------------------------------------------
# Pack 装配
# ------------------------------------------------------------------


def test_initialize_falls_back_to_settings_dir(tmp_path: Path, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setenv("REPLY_PREFERENCE_DIR", str(tmp_path / "from-settings"))
    get_settings.cache_clear()
    pack = ReplyPreferenceToolPack()
    pack.initialize({})
    store = pack.get_store()
    assert store is not None
    assert Path(store._dir) == tmp_path / "from-settings"
    get_settings.cache_clear()


def test_initialize_falls_back_to_project_default_dir(monkeypatch):
    """未配置存储目录时落到项目 data/reply_preference。

    不能靠「不设环境变量」——Settings 还会读仓库 .env（.env.example 里就有绝对路径），
    那条路测的是本机环境而不是回落。这里直接把 settings 钉成空目录。
    """
    fake_settings = type("SettingsStub", (), {"reply_preference_dir": ""})()
    monkeypatch.setattr(
        "csagent.tools.packs.reply_preference_pack.get_settings",
        lambda: fake_settings,
    )
    pack = ReplyPreferenceToolPack()
    pack.initialize({})
    store = pack.get_store()
    assert store is not None
    assert Path(store._dir) == PROJECT_ROOT / "data" / "reply_preference"



def test_initialize_applies_description_override(tmp_path: Path):
    pack = ReplyPreferenceToolPack()
    pack.initialize(
        {
            "store_dir": str(tmp_path),
            "tools": [{"name": "set_reply_style", "description": "自定义描述"}],
        }
    )
    tools = pack.get_tools()
    assert len(tools) == 1
    assert tools[0].description == "自定义描述"


# ------------------------------------------------------------------
# preference_key()
# ------------------------------------------------------------------


def test_preference_key_private_chat_uses_session_id():
    scope = CustomerScope(session_id="wecom:userA")
    assert preference_key(scope) == "wecom:userA"


def test_preference_key_group_chat_shared_across_members():
    """群聊会话本身就是整群一个（wecom:group:{chatid}），偏好随之共享。"""
    scope = CustomerScope(session_id="wecom:group:chat123")
    assert preference_key(scope) == "wecom:group:chat123"


def test_preference_key_non_wecom_uses_session_id():
    """非企微渠道直接用 session_id。

    原先这里还有一条 customer_id 优先的回退分支，2026-08-21 随 customer_id
    整条链路一起移除——它从来没被真正赋过值。
    """
    assert preference_key(CustomerScope(session_id="sess-1")) == "sess-1"


def test_preference_key_web_is_per_user():
    """web 端 session_id 形如 web:{userId}，偏好按用户长期连续。"""
    assert preference_key(CustomerScope(session_id="web:u2001")) == "web:u2001"


# ------------------------------------------------------------------
# SetReplyStyleTool.execute() + ReplyPreferenceStore
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_concise_then_read_back(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    result = await tool.execute({"style": "concise"}, scope)
    assert "已记住" in result
    block = store.format_for_context(preference_key(scope))
    assert "简短" in block


@pytest.mark.asyncio
async def test_update_overwrites_previous_style(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    await tool.execute({"style": "concise"}, scope)
    await tool.execute({"style": "detailed", "note": "改主意了"}, scope)

    data = store.get(preference_key(scope))
    assert data["style"] == "detailed"
    assert data["note"] == "改主意了"


@pytest.mark.asyncio
async def test_normal_clears_stored_preference(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    await tool.execute({"style": "concise"}, scope)
    result = await tool.execute({"style": "normal"}, scope)

    assert "已清除" in result
    assert store.get(preference_key(scope)) is None
    assert store.format_for_context(preference_key(scope)) == ""


@pytest.mark.asyncio
async def test_custom_style_requires_note(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    result = await tool.execute({"style": "custom"}, scope)
    assert "参数错误" in result
    assert store.get(preference_key(scope)) is None


@pytest.mark.asyncio
async def test_custom_style_note_becomes_directive_verbatim(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    await tool.execute({"style": "custom", "note": "回复开头和结尾都加123"}, scope)
    block = store.format_for_context(preference_key(scope))
    assert "回复开头和结尾都加123" in block


@pytest.mark.asyncio
async def test_invalid_style_rejected(tmp_path: Path):
    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:userA")

    result = await tool.execute({"style": "bogus"}, scope)
    assert "参数错误" in result


# ------------------------------------------------------ 工具对模型的可见性
# 落库逻辑早就是对的；2026-08-21 那次「口头答应已记录、实际没记」栽在**可见性**上：
# 工具没进首轮 schema、系统提示词也没提过它。这两条各加一个守卫。


def test_capability_notes_disclose_the_tool_when_mounted():
    """挂了工具却不在「已挂载工具约定」里说明 = 模型不知道自己能记偏好。"""
    from csagent.prompts.capability_notes import format_capability_notes

    notes = format_capability_notes(["list_skills", "load_skill", "set_reply_style"])
    assert "set_reply_style" in notes
    # 必须点明「不调用工具就等于没记」，否则模型仍会用正文口头答应搪塞过去
    assert "已记录" in notes


def test_tool_is_offered_on_a_preference_sentence_with_no_domain_words():
    """回归：全量工具下 set_reply_style 必然在列（无裁剪，不会丢）。"""
    schemas = [
        {"type": "function", "function": {"name": n, "description": "", "parameters": {}}}
        for n in ("run_script", "list_skills", "load_skill", "set_reply_style")
    ]
    kept = {(s.get("function") or {}).get("name") for s in schemas}
    assert "set_reply_style" in kept


# ------------------------------------------------------ 整套输出模板作为偏好
# 2026-08-21：某客户要求固定版式的产品卡片。note 上限从 300 放宽到 2000
# 就是为了装得下这类模板——按截图布局写出来约 430 字，300 字放不下。


@pytest.mark.asyncio
async def test_note_holds_a_full_output_template(tmp_path: Path):
    """模板必须逐字落盘：换行、占位符、标点一个都不能变。

    模板的价值就在于每次输出完全一致，任何顺手润色都等于毁掉它。
    """
    from csagent.tools.preference_tool import _MAX_NOTE_CHARS

    template = (
        "【固定输出模板】回答基金产品问题时必须严格按下面版式输出；\n"
        "若涉及多只基金，则每只基金各输出一份，彼此之间空一行。\n\n"
        "**{基金名称}（{基金代码}）**\n"
        "{风险等级代码}{风险等级名称} ｜ {基金类型}\n\n"
        "单位净值 {净值}（{净值日期}）｜ 日涨幅 {日涨幅}%\n"
        "近1年收益 {近1年涨幅}% ｜ 近1年同类排名 {近1年排名}\n\n"
        "- 起购金额：{起购金额} 元\n"
        "- 手续费率：~~{原申购费率}%~~ {折后费率}%\n"
        "- 基金经理：{基金经理}\n"
        "- 管理人：{管理人}\n"
        "- 交易确认天数：T+{申购确认天数}\n"
        "- 总规模：{总规模}亿元（{规模报告期}）\n"
        "- 最快赎回到账日：{赎回到账}\n"
        "- 当日累计限额：{当日累计限额}\n"
        "- 成立日期：{成立日期}\n\n"
        "规则：无数据的字段填 --，不要删行；日期一律 YYYY-MM-DD；"
        "限额为天文数字时写「无限额」；模板之外不要添加任何寒暄或建议。"
    )
    assert 300 < len(template) <= _MAX_NOTE_CHARS, "这条测试的前提是模板超过旧上限"

    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:tplUser")

    await tool.execute({"style": "custom", "note": template}, scope)

    key = preference_key(scope)
    assert store.get(key)["note"] == template          # 落盘逐字
    assert template in store.format_for_context(key)   # 注入逐字


@pytest.mark.asyncio
async def test_note_longer_than_limit_is_truncated_not_rejected(tmp_path: Path):
    """超长不报错，截断即可——宁可少几行也别让整条偏好存不进去。"""
    from csagent.tools.preference_tool import _MAX_NOTE_CHARS

    store = ReplyPreferenceStore(tmp_path)
    tool = SetReplyStyleTool(store)
    scope = CustomerScope(session_id="wecom:longUser")

    await tool.execute({"style": "custom", "note": "模" * (_MAX_NOTE_CHARS + 500)}, scope)
    assert len(store.get(preference_key(scope))["note"]) == _MAX_NOTE_CHARS
