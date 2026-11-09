"""语义查询分页与出口契约。

audit wecom_BoWen 05:17：某表命中 780 行、单页上限 200 行，而 QuerySpec 没有
offset——模型改 limit、改 offset（被静默忽略）试了四轮，都拿回同一页；转而自己
构造 keyset 游标（`基金代码 > 上页末值`），又撞上该字段只开放 eq/in。整轮任务
以 StallGuard 收口告终，用户什么也没拿到。
"""

from __future__ import annotations

import pytest

from csagent.react.models import CustomerScope
from csagent.tools.semantic.catalog import QueryCatalog
from csagent.tools.semantic.executor import SemanticQueryExecutor
from csagent.tools.semantic.models import QuerySpec, SemanticQueryOutcome


class _RecordingBackend:
    """记录收到的 QuerySpec，返回固定行数。"""

    def __init__(self, rows_per_call: int = 2) -> None:
        self.specs: list[QuerySpec] = []
        self._rows_per_call = rows_per_call

    async def execute(self, spec: QuerySpec) -> SemanticQueryOutcome:
        self.specs.append(spec)
        rows = [{"id": i} for i in range(self._rows_per_call)]
        return SemanticQueryOutcome(entity=spec.entity, text="{}", rows=rows)


@pytest.fixture
def catalog(tmp_path):
    yaml_text = """
entities:
  widgets:
    domain: testing
    description: 测试实体
    default_limit: 20
    max_rows: 200
    fields:
      name:
        type: string
        ops: [eq, in]
"""
    path = tmp_path / "entities.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return QueryCatalog.load(path)


@pytest.fixture
def scope() -> CustomerScope:
    return CustomerScope(session_id="s")


async def _run(catalog, backend, scope, arguments):
    executor = SemanticQueryExecutor(catalog=catalog, backend=backend)
    return await executor.execute_outcome(arguments, scope)


@pytest.mark.asyncio
async def test_offset_is_parsed_and_forwarded(catalog, scope):
    """offset 过去被静默丢弃——调用方每次都拿到同一页。"""
    backend = _RecordingBackend()
    await _run(catalog, backend, scope, {"entity": "widgets", "limit": 50, "offset": 200})
    assert backend.specs[0].offset == 200


@pytest.mark.asyncio
async def test_offset_defaults_to_none_when_absent(catalog, scope):
    backend = _RecordingBackend()
    await _run(catalog, backend, scope, {"entity": "widgets"})
    assert backend.specs[0].offset is None


@pytest.mark.asyncio
async def test_negative_offset_is_clamped_to_zero(catalog, scope):
    backend = _RecordingBackend()
    await _run(catalog, backend, scope, {"entity": "widgets", "offset": -5})
    assert backend.specs[0].offset == 0


@pytest.mark.asyncio
async def test_limit_still_clamped_to_max_rows_with_offset(catalog, scope):
    """limit 压到 max_rows 的行为不变；offset 才是翻页的正解。"""
    backend = _RecordingBackend()
    await _run(catalog, backend, scope, {"entity": "widgets", "limit": 5000, "offset": 400})
    assert backend.specs[0].limit == 200
    assert backend.specs[0].offset == 400


@pytest.mark.asyncio
async def test_paging_walks_distinct_offsets(catalog, scope):
    """按 next_offset 连续翻页时，每次下发的 offset 都不同。"""
    backend = _RecordingBackend()
    for off in (0, 200, 400, 600):
        await _run(catalog, backend, scope, {"entity": "widgets", "limit": 200, "offset": off})
    assert [s.offset for s in backend.specs] == [0, 200, 400, 600]


def test_query_spec_accepts_offset():
    spec = QuerySpec(entity="widgets", offset=200)
    assert spec.offset == 200
    assert QuerySpec(entity="widgets").offset is None
