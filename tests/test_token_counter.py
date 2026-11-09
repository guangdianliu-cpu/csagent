"""TokenCounter 单元测试。"""

from csagent.memory.token_counter import TokenCounter


def test_token_counter_basic_text():
    counter = TokenCounter("gpt-4o")
    # 纯英文
    tokens_en = counter.count_text("Hello world")
    assert tokens_en > 0

    # 中文
    tokens_zh = counter.count_text("你好，世界！这是一段测试文本。")
    assert tokens_zh > 0

    # 空文本
    assert counter.count_text("") == 0


def test_token_counter_messages():
    counter = TokenCounter("gpt-4o")
    messages = [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "查一下 000001 基金的费率。"},
    ]
    tokens = counter.count_messages(messages)
    assert tokens > 10


def test_token_counter_truncate():
    counter = TokenCounter("gpt-4o")
    long_text = "这是第一句话。这是第二句话。这是第三句话。这是第四句话。这是第五句话。"
    truncated = counter.truncate_to_tokens(long_text, max_tokens=10)
    assert len(truncated) < len(long_text)
    assert truncated.endswith("…") or len(truncated) <= len(long_text)


def test_token_counter_model_limits():
    counter_4o = TokenCounter("gpt-4o")
    assert counter_4o.model_context_limit == 128_000

    counter_ds = TokenCounter("deepseek-chat")
    assert counter_ds.model_context_limit == 128_000

    counter_v4 = TokenCounter("deepseek-v4-flash")
    assert counter_v4.model_context_limit == 1_000_000

    counter_unknown = TokenCounter("some-unknown-model-xyz")
    assert counter_unknown.model_context_limit == 64_000

    overridden = TokenCounter("gpt-4o", context_limit=32_000)
    assert overridden.model_context_limit == 32_000


def test_context_cap_limits_inferred_window_only(monkeypatch):
    """标称 1M 的模型按有效窗口封顶；显式指定的窗口不受封顶影响。"""
    from csagent.memory.token_counter import TokenCounter

    monkeypatch.delenv("LLM_CONTEXT_LIMIT", raising=False)
    monkeypatch.delenv("MODEL_CONTEXT_LIMIT", raising=False)
    assert TokenCounter("deepseek-v4-flash").model_context_limit == 1_000_000
    assert TokenCounter("deepseek-v4-flash", context_cap=200_000).model_context_limit == 200_000
    # 推断值本就小于封顶：不变
    assert TokenCounter("deepseek-chat", context_cap=200_000).model_context_limit == 128_000
    # 显式覆盖优先
    assert TokenCounter(
        "deepseek-v4-flash", context_limit=500_000, context_cap=200_000
    ).model_context_limit == 500_000


def test_count_text_cache_consistent_and_bounded(monkeypatch):
    from csagent.memory import token_counter as tcm
    from csagent.memory.token_counter import TokenCounter

    monkeypatch.setattr(tcm, "_CACHE_MAX_CHARS", 5000)
    tc = TokenCounter("deepseek-v4-flash")
    text = "净值" * 400
    first = tc.count_text(text)
    assert tc.count_text(text) == first == tc._count_uncached(text)
    for i in range(20):
        tc.count_text(f"{i}" + "x" * 1000)
    assert tc._count_cache_chars <= 5000
    assert tc.count_text("短") == tc._count_uncached("短")
