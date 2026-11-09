"""从 YAML 加载实体目录（Catalog，产品中心 API 实体，无 SQL 映射）。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from csagent.infra.config import CONFIGS_DIR, PROJECT_ROOT

SKILL_ENTITIES_PATH = (
    PROJECT_ROOT / "skills" / "fund-product-query" / "scripts" / "entities.yaml"
)
PRODUCT_API_PATH = SKILL_ENTITIES_PATH


@dataclass(frozen=True)
class FieldDef:
    """实体字段定义。"""

    name: str
    type: str = "string"
    filterable: bool = False
    sortable: bool = False
    ops: frozenset[str] = frozenset()
    enum_values: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class EntityDef:
    """可查询实体定义。"""

    name: str
    domain: str
    description: str = ""
    default_limit: int = 20
    max_rows: int = 50
    fields: dict[str, FieldDef] = field(default_factory=dict)
    default_order_by: tuple[str, str] | None = None
    related_entities: tuple[str, ...] = ()

    def filterable_field_names(self) -> list[str]:
        return [name for name, fdef in self.fields.items() if fdef.filterable]

    def sortable_field_names(self) -> list[str]:
        return [name for name, fdef in self.fields.items() if fdef.sortable]


class QueryCatalog:
    """全量实体目录。"""

    def __init__(self, entities: dict[str, EntityDef]) -> None:
        self._entities = entities

    @classmethod
    def load(cls, api_path: Path | None = None) -> "QueryCatalog":
        """加载实体文件（默认 skills/fund-product-query/scripts/entities.yaml）。"""
        if api_path:
            path = api_path
        elif SKILL_ENTITIES_PATH.exists():
            path = SKILL_ENTITIES_PATH
        else:
            path = CONFIGS_DIR / "product_api.yaml"
        if not path.exists():
            raise FileNotFoundError(f"产品 API 实体文件不存在: {path}")
        entities: dict[str, EntityDef] = {}
        with path.open(encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        for entity_name, cfg in (raw.get("entities") or {}).items():
            if isinstance(cfg, dict):
                entities[entity_name] = _parse_entity(entity_name, cfg)
        return cls(entities)

    def get(self, entity_name: str) -> EntityDef | None:
        return self._entities.get(entity_name)

    def entities_for_domain(self, domain: str) -> list[EntityDef]:
        return [e for e in self._entities.values() if e.domain == domain]

    def all_entities(self) -> dict[str, EntityDef]:
        return dict(self._entities)


def _parse_entity(name: str, cfg: dict[str, Any]) -> EntityDef:
    fields: dict[str, FieldDef] = {}
    for field_name, field_cfg in (cfg.get("fields") or {}).items():
        if not isinstance(field_cfg, dict):
            continue
        enum_raw = field_cfg.get("values") or {}
        enum_values = {str(k): str(v) for k, v in enum_raw.items()} if enum_raw else {}
        fields[field_name] = FieldDef(
            name=field_name,
            type=str(field_cfg.get("type", "string")),
            filterable=bool(field_cfg.get("filterable", False)),
            sortable=bool(field_cfg.get("sortable", False)),
            ops=frozenset(field_cfg.get("ops") or []),
            enum_values=enum_values,
            description=str(field_cfg.get("description", "")),
        )

    default_order = cfg.get("default_order_by")
    default_order_by: tuple[str, str] | None = None
    if isinstance(default_order, dict) and default_order.get("field"):
        default_order_by = (
            str(default_order["field"]),
            str(default_order.get("direction", "asc")).lower(),
        )

    related = tuple(str(x) for x in (cfg.get("related_entities") or []) if x)

    return EntityDef(
        name=name,
        domain=str(cfg["domain"]),
        description=str(cfg.get("description", "")),
        default_limit=int(cfg.get("default_limit", 20)),
        max_rows=int(cfg.get("max_rows", 50)),
        fields=fields,
        default_order_by=default_order_by,
        related_entities=related,
    )


def format_entity_summary(entity: EntityDef) -> str:
    """生成供 LLM 阅读的实体摘要（含字段语义）。"""
    filter_parts = []
    for fname in entity.filterable_field_names():
        fdef = entity.fields[fname]
        ops = "/".join(sorted(fdef.ops))
        enum_hint = ""
        if fdef.enum_values:
            enum_hint = f" 枚举:{','.join(f'{k}={v}' for k, v in fdef.enum_values.items())}"
        filter_parts.append(f"{fname}({ops}){enum_hint}")

    sort_fields = ", ".join(entity.sortable_field_names()) or "无"
    field_lines: list[str] = []
    for fname, fdef in entity.fields.items():
        desc = (fdef.description or "").strip()
        if desc:
            field_lines.append(f"{fname}: {desc}")
        else:
            field_lines.append(fname)

    lines = [
        f"- **{entity.name}**: {entity.description or '（无描述）'}",
        f"  字段语义: {'; '.join(field_lines) if field_lines else '无'}",
        f"  可筛选: {', '.join(filter_parts) if filter_parts else '无'}",
        f"  可排序: {sort_fields}",
        f"  默认上限: {entity.default_limit} 行（最大 {entity.max_rows}）",
    ]
    if entity.related_entities:
        lines.append(f"  关联实体: {', '.join(entity.related_entities)}")
    return "\n".join(lines)


def format_domain_catalog(catalog: QueryCatalog, domain: str) -> str:
    """生成某领域全部实体的工具说明文本。"""
    entities = catalog.entities_for_domain(domain)
    if not entities:
        return "（当前领域无可用查询实体）"
    return "\n".join(format_entity_summary(entity) for entity in entities)
