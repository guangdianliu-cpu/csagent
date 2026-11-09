"""从 Catalog 生成实体路由表与字段摘要文本（供领域 Prompt 使用）。"""

from __future__ import annotations

from csagent.tools.semantic.catalog import EntityDef, QueryCatalog


def format_entity_routing_table(catalog: QueryCatalog, domain: str) -> str:
    """从 Catalog 生成「场景 → entity」路由表，替代手写 Prompt 表格。"""
    entities = catalog.entities_for_domain(domain)
    if not entities:
        return "（当前领域无可用实体）"

    lines = [
        "| 场景/用途 | entity | 关联实体 |",
        "|-----------|--------|----------|",
    ]
    for entity in sorted(entities, key=lambda e: e.name):
        scenario = _entity_scenario_label(entity)
        extra = (
            "关联:" + ",".join(entity.related_entities[:3])
            if entity.related_entities
            else "—"
        )
        lines.append(f"| {scenario} | {entity.name} | {extra} |")
    return "\n".join(lines)


def format_domain_prompt_catalog(catalog: QueryCatalog, domain: str) -> str:
    """注入 system prompt 的 Catalog：场景路由 + 字段语义。"""
    from csagent.tools.semantic.catalog import format_domain_catalog

    routing = format_entity_routing_table(catalog, domain)
    detail = format_domain_catalog(catalog, domain)
    return (
        f"## 实体路由\n{routing}\n\n"
        f"## 实体字段摘要\n{detail}"
    )


def _entity_scenario_label(entity: EntityDef) -> str:
    """优先用 description；过长则截断。"""
    desc = (entity.description or "").strip()
    if desc:
        return desc if len(desc) <= 40 else desc[:37] + "…"
    return entity.name.replace("_", " ")
