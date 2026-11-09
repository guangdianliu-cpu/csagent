"""语义查询执行器：对照 Catalog 校验 QuerySpec，再交给 backend 执行；backend 是哪个领域的哪个下游系统，本文件不关心。"""

from __future__ import annotations

from typing import Any, Protocol

import structlog

from csagent.react.models import CustomerScope
from csagent.tools.semantic.catalog import QueryCatalog
from csagent.tools.semantic.models import FilterSpec, OrderBySpec, QuerySpec, SemanticQueryOutcome

logger = structlog.get_logger(__name__)


class QueryBackend(Protocol):
    """QuerySpec 的真实执行者：把校验通过的 QuerySpec 转成对下游系统的请求。

    具体实现（如产品中心网关）按业务领域走 Skill 自己的脚本，不属于这层通用引擎。
    """

    async def execute(self, spec: QuerySpec) -> SemanticQueryOutcome: ...


class SemanticQueryExecutor:
    """校验 QuerySpec（对照 Catalog）后交给 backend 执行，不关心 backend 具体实现。"""

    def __init__(
        self,
        catalog: QueryCatalog,
        *,
        backend: QueryBackend,
    ) -> None:
        self._catalog = catalog
        self._backend = backend

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        outcome = await self.execute_outcome(arguments, scope)
        return outcome.text

    async def execute_outcome(
        self, arguments: dict[str, Any], scope: CustomerScope
    ) -> SemanticQueryOutcome:
        _ = scope  # 部分通用领域查询无客户身份绑定
        try:
            spec = self._parse_arguments(arguments)
        except ValueError as exc:
            return SemanticQueryOutcome(entity="", text=f"QuerySpec 格式错误: {exc}")

        entity = self._catalog.get(spec.entity)
        if entity is None:
            return SemanticQueryOutcome(
                entity=str(arguments.get("entity") or ""),
                text=f"未知实体: {spec.entity}",
            )

        err = self._validate_filters(entity.name, spec)
        if err:
            return SemanticQueryOutcome(entity=entity.name, text=err)

        # 规范化 limit / offset
        updates: dict[str, Any] = {}
        if spec.limit is None:
            updates["limit"] = entity.default_limit
        elif spec.limit > entity.max_rows:
            updates["limit"] = entity.max_rows
        if spec.offset is not None and spec.offset < 0:
            updates["offset"] = 0
        if updates:
            spec = spec.model_copy(update=updates)

        return await self._backend.execute(spec)

    async def execute_entity(
        self,
        entity: str,
        *,
        filters: list[dict[str, Any]] | None = None,
        order_by: dict[str, Any] | None = None,
        limit: int | None = None,
        scope: CustomerScope | None = None,
    ) -> SemanticQueryOutcome:
        args: dict[str, Any] = {"entity": entity, "filters": filters or []}
        if order_by:
            args["order_by"] = order_by
        if limit is not None:
            args["limit"] = limit
        return await self.execute_outcome(
            args, scope or CustomerScope(session_id="")
        )

    def _parse_arguments(self, arguments: dict[str, Any]) -> QuerySpec:
        entity = arguments.get("entity")
        if not entity or not isinstance(entity, str):
            raise ValueError("缺少 entity")
        filters_raw = arguments.get("filters") or []
        filters = [FilterSpec(**item) for item in filters_raw if isinstance(item, dict)]
        order_by_raw = arguments.get("order_by")
        order_by = OrderBySpec(**order_by_raw) if isinstance(order_by_raw, dict) else None
        select_raw = arguments.get("select")
        select = list(select_raw) if isinstance(select_raw, list) else None
        limit = arguments.get("limit")
        offset = arguments.get("offset")
        return QuerySpec(
            entity=entity,
            filters=filters,
            order_by=order_by,
            limit=int(limit) if limit is not None else None,
            offset=int(offset) if offset is not None else None,
            select=select,
        )

    def _validate_filters(self, entity_name: str, spec: QuerySpec) -> str | None:
        entity = self._catalog.get(entity_name)
        if entity is None:
            return f"未知实体: {entity_name}"
        for flt in spec.filters:
            fdef = entity.fields.get(flt.field)
            if fdef is None:
                return f"字段 '{flt.field}' 不存在。可筛选: {entity.filterable_field_names()}"
            if not fdef.filterable:
                return f"字段 '{flt.field}' 不可筛选"
            if flt.op not in fdef.ops:
                return f"字段 '{flt.field}' 不支持操作符 '{flt.op}'，允许: {sorted(fdef.ops)}"
        if spec.order_by:
            fdef = entity.fields.get(spec.order_by.field)
            if fdef is None or not fdef.sortable:
                return f"字段 '{spec.order_by.field}' 不可排序"
        return None
