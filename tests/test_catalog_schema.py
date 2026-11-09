"""产品 API Catalog 生成路由表与 QuerySpec schema 测试。"""

from __future__ import annotations

from csagent.tools.semantic.catalog import QueryCatalog, format_entity_summary
from csagent.tools.semantic.schema import (
    format_domain_prompt_catalog,
    format_entity_routing_table,
)


def test_routing_table_lists_product_entities():
    catalog = QueryCatalog.load()
    table = format_entity_routing_table(catalog, "product")
    assert "fund_products" in table
    assert "fund_performance" in table
    assert "| 场景/用途 | entity | 关联实体 |" in table


def test_related_entities_are_valid():
    catalog = QueryCatalog.load()
    entity_names = set(catalog.all_entities())
    errors: list[str] = []
    for name, entity in catalog.all_entities().items():
        assert entity.domain == "product"
        for related in entity.related_entities:
            if related not in entity_names:
                errors.append(f"{name}.related_entities: 未知实体 `{related}`")
    assert not errors, "\n".join(errors)


def test_domain_prompt_catalog_includes_routing_and_fields():
    catalog = QueryCatalog.load()
    block = format_domain_prompt_catalog(catalog, "product")
    assert "## 实体路由" in block
    assert "fund_products" in block
    assert "字段语义:" in block


def test_entity_summary_includes_field_semantics():
    catalog = QueryCatalog.load()
    products = catalog.get("fund_products")
    assert products is not None
    summary = format_entity_summary(products)
    assert "字段语义:" in summary
    assert "基金代码" in summary or "fund_code" in summary
    assert "关联实体:" in summary


def test_catalog_field_descriptions_present():
    catalog = QueryCatalog.load()
    for name in ("fund_products", "fund_performance", "fund_fee_rates", "fund_quota_info"):
        entity = catalog.get(name)
        assert entity is not None, name
        described = [f for f in entity.fields.values() if f.description]
        assert described, f"{name} 应有字段 description"
