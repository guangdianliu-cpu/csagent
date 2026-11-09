"""memory.short_term.ShortTermMemory 的裁剪与会话淘汰测试。"""

from __future__ import annotations

from csagent.memory.short_term import ShortTermMemory


def test_load_empty_session_returns_empty_list():
    memory = ShortTermMemory()
    assert memory.load("unknown") == []


def test_append_and_load_round_trip():
    memory = ShortTermMemory(max_messages=20)
    memory.append("s1", "你好", "您好，有什么可以帮您")
    history = memory.load("s1")
    assert len(history) == 2
    assert history[0].role == "user"
    assert history[1].role == "assistant"


def test_history_trimmed_to_max_messages():
    memory = ShortTermMemory(max_messages=4)
    for i in range(5):
        memory.append("s1", f"问题{i}", f"回答{i}")
    history = memory.load("s1")
    assert len(history) == 4
    assert history[-1].content == "回答4"


def test_session_count_capped_with_lru_eviction():
    memory = ShortTermMemory(max_sessions=2)
    memory.append("s1", "q1", "a1")
    memory.append("s2", "q2", "a2")
    memory.append("s3", "q3", "a3")  # 触发淘汰最久未使用的 s1

    assert memory.load("s1") == []
    assert len(memory.load("s2")) == 2
    assert len(memory.load("s3")) == 2


def test_recently_accessed_session_is_not_evicted():
    memory = ShortTermMemory(max_sessions=2)
    memory.append("s1", "q1", "a1")
    memory.append("s2", "q2", "a2")
    memory.load("s1")  # 访问 s1，使其成为最近使用
    memory.append("s3", "q3", "a3")  # 应淘汰 s2 而非 s1

    assert len(memory.load("s1")) == 2
    assert memory.load("s2") == []


def test_long_message_clipped_on_write():
    """写入时截断：读取侧的 Token 预算只影响进模型的文本，存储里留的仍是全文。"""
    memory = ShortTermMemory(max_message_chars=100)
    memory.append("s1", "问" * 500, "答" * 500)
    history = memory.load("s1")

    assert len(history[0].content) == 100 + len("…（已截断）")
    assert history[0].content.startswith("问" * 100)
    assert history[0].content.endswith("…（已截断）")
    assert len(history[1].content) == 100 + len("…（已截断）")


def test_short_message_not_marked_as_clipped():
    memory = ShortTermMemory(max_message_chars=100)
    memory.append("s1", "你好", "您好")
    history = memory.load("s1")

    assert history[0].content == "你好"
    assert history[1].content == "您好"


def test_clip_disabled_when_limit_non_positive():
    memory = ShortTermMemory(max_message_chars=0)
    memory.append("s1", "问" * 500, "答" * 500)
    history = memory.load("s1")

    assert len(history[0].content) == 500
    assert len(history[1].content) == 500
