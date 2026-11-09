"""语义查询协议模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator
from dataclasses import dataclass


class FilterSpec(BaseModel):
    """单个筛选条件。"""

    field: str
    op: str
    value: Any


class OrderBySpec(BaseModel):
    """排序规则。"""

    field: str
    direction: str = "asc"

    @field_validator("direction")
    @classmethod
    def normalize_direction(cls, value: str) -> str:
        lowered = value.lower()
        if lowered not in {"asc", "desc"}:
            raise ValueError("direction 必须是 asc 或 desc")
        return lowered


class QuerySpec(BaseModel):
    """LLM 输出的受控查询规格。"""

    entity: str
    filters: list[FilterSpec] = Field(default_factory=list)
    order_by: OrderBySpec | None = None
    limit: int | None = None
    # 分页起点。没有它时，命中数超过 max_rows 的查询只能反复拿到同一页——
    # 调用方唯一的出路是构造 keyset 过滤（field > last_value），而这又依赖该字段
    # 恰好开放了 gt 操作符，多数情况并不成立（见 audit wecom_BoWen 05:17）。
    offset: int | None = None
    select: list[str] | None = None


@dataclass(frozen=True)
class SemanticQueryOutcome:
    """语义查询执行结果（文本 + 可选结构化行）。"""

    entity: str
    text: str
    rows: list[dict[str, Any]] | None = None
    row_limit: int | None = None
