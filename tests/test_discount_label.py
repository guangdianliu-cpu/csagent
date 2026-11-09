"""折扣字段解读单元测试。"""

from __future__ import annotations

from discount_label import (
    DISCOUNT_SCOPE,
    describe_busincode,
    describe_discount,
    enrich_discount_row,
)


def test_describe_discount_special_values():
    assert describe_discount(0) == "无费用（免收）"
    assert describe_discount("0") == "无费用（免收）"
    assert describe_discount(1) == "不打折（按原费率）"
    assert describe_discount("1.0") == "不打折（按原费率）"


def test_describe_discount_fold_values():
    assert describe_discount(0.1) == "1折（按原费率×0.1）"
    assert describe_discount("0.01") == "0.1折（按原费率×0.01）"
    assert describe_discount(0.05) == "0.5折（按原费率×0.05）"


def test_enrich_adds_desc_without_overwrite():
    row = enrich_discount_row({"fund_code": "X", "discount": 0.1, "fix_discount": 0})
    assert row["discount_desc"] == "1折（按原费率×0.1）"
    assert row["fix_discount_desc"] == "无费用（免收）"

    kept = enrich_discount_row(
        {"discount": 0.1, "discount_desc": "自定义说明"}
    )
    assert kept["discount_desc"] == "自定义说明"


def test_describe_busincode_known_and_unknown():
    assert describe_busincode("020") == "认购"
    assert describe_busincode("022") == "申购"
    assert describe_busincode("024") == "赎回"
    # 未知业务码原样带出，不猜、不并进申购/认购
    assert describe_busincode("039") == "其他业务(039)"
    assert describe_busincode("") == ""
    assert describe_busincode(None) == ""


def test_enrich_adds_busincode_name_and_scope():
    # 008864 线上口径：020 认购 1 折、022 申购 0.1 折，固定费用均不打折
    sub = enrich_discount_row(
        {"fund_code": "008864", "busincode": "020", "discount": 0.1, "fix_discount": 1}
    )
    buy = enrich_discount_row(
        {"fund_code": "008864", "busincode": "022", "discount": 0.01, "fix_discount": 1}
    )
    assert sub["busincode_name"] == "认购"
    assert sub["discount_desc"] == "1折（按原费率×0.1）"
    assert buy["busincode_name"] == "申购"
    assert buy["discount_desc"] == "0.1折（按原费率×0.01）"
    # 单笔固定费用独立于费率折扣，这里是不打折
    assert sub["fix_discount_desc"] == "不打折（按原费率）"
    assert buy["fix_discount_desc"] == "不打折（按原费率）"
    assert buy["discount_scope"] == DISCOUNT_SCOPE


def test_enrich_keeps_existing_busincode_name_and_scope():
    row = enrich_discount_row(
        {
            "busincode": "022",
            "busincode_name": "自定义业务",
            "discount": 0.01,
            "discount_scope": "自定义范围",
        }
    )
    assert row["busincode_name"] == "自定义业务"
    assert row["discount_scope"] == "自定义范围"


def test_enrich_without_discount_fields_adds_no_scope():
    row = enrich_discount_row({"fund_code": "X", "busincode": "022"})
    assert row["busincode_name"] == "申购"
    assert "discount_scope" not in row
