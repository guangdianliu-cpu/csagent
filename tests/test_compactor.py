"""StructuredCompactor 单元测试。"""

import pytest

from csagent.memory.compactor import CompactorConfig, StructuredCompactor
from csagent.memory.compactor_cache import InMemoryCompactorCache
from csagent.memory.token_counter import TokenCounter
from csagent.react.models import ChatMessage


class DummyLlmClient:
    async def chat(self, messages, **kwargs):
        return """### 用户目标与状态
- 查询 000001 费率

### 关键决策与已完成操作
- 已调用 product_api 查询费率

### 涉及关键实体与数据
- 基金: 000001, 申购费率: 0.15%

### 未决事项与下一步
- 无
"""


@pytest.mark.asyncio
async def test_structured_compactor_build_context():
    counter = TokenCounter("gpt-4o")
    llm = DummyLlmClient()
    cache = InMemoryCompactorCache()
    cfg = CompactorConfig(window=2)
    compactor = StructuredCompactor(
        llm_client=llm,
        token_counter=counter,
        config=cfg,
        cache_backend=cache,
        enabled=True,
    )

    history = [
        ChatMessage(role="user", content="查下 000001 费率"),
        ChatMessage(role="assistant", content="000001 申购费率 0.15%"),
        ChatMessage(role="user", content="那 000002 呢？"),
        ChatMessage(role="assistant", content="000002 申购费率 0.12%"),
    ]

    # 首次 build（摘要生成中/未生成）
    ctx = compactor.build_context("sess_1", history, current_query="000002 收益率")
    assert "最近对话" in ctx.full_text

    # 执行刷新
    await compactor.refresh("sess_1", history)

    # 再次 build（包含 Checkpoint）
    ctx_after = compactor.build_context("sess_1", history)
    assert "会话历史 Checkpoint" in ctx_after.summary_block
    assert "000001" in ctx_after.full_text
