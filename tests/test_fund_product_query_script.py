"""skills/fund-product-query/scripts/query.py 单测。

脚本不在 src/csagent 包内（活在 skills/ 目录，由 run_script 起子进程执行），
用 importlib 按文件路径加载后直接调用其内部函数，不走真实子进程。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "fund-product-query"
    / "scripts"
    / "query.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("fund_product_query_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


class _StubClient:
    async def aclose(self) -> None:
        pass


# ------------------------------------------------------------------
# argparse 层：--action / --payload 校验
# ------------------------------------------------------------------


def _failure(capsys) -> dict:
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    return out["error"]


def test_invalid_action_reports_arg_error(script, capsys):
    exit_code = script.main(["--action", "bogus", "--payload", "{}"])
    err = _failure(capsys)
    assert exit_code == 1
    assert err["code"] == "BAD_PAYLOAD"
    assert "参数错误" in err["message"]


def test_missing_payload_reports_arg_error(script, capsys):
    exit_code = script.main(["--action", "query"])
    assert exit_code == 1
    assert "参数错误" in _failure(capsys)["message"]


def test_malformed_json_payload(script, capsys):
    exit_code = script.main(["--action", "query", "--payload", "{not json"])
    err = _failure(capsys)
    assert exit_code == 1
    assert "参数错误" in err["message"]
    assert "JSON" in err["message"]


def test_payload_must_be_json_object(script, capsys):
    exit_code = script.main(["--action", "query", "--payload", "[1,2,3]"])
    assert exit_code == 1
    assert "JSON 对象" in _failure(capsys)["message"]


# ------------------------------------------------------------------
# 出口契约：成功原样输出，失败统一信封 + 非 0 退出码
# ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("权限错误: 不允许实体 'x'。可用实体: fund_products", "UNKNOWN_ENTITY"),
        ("实体 fund_x 暂未接入产品中心网关。请换用已支持实体", "UNKNOWN_ENTITY"),
        ("产品中心查询失败: fund_archive 需要 fund_code", "BAD_PAYLOAD"),
        ("产品中心查询失败: product-pc-bridge /api/x -> 500: boom", "UPSTREAM_ERROR"),
        ("多基金查询失败: timeout", "UPSTREAM_ERROR"),
        ("字段 'fund_name' 不存在。可筛选: ['fund_code']", "FIELD_NOT_FOUND"),
        ("字段 'nav' 不可筛选", "FIELD_NOT_FILTERABLE"),
        ("字段 'fund_code' 不支持操作符 'gt'，允许: ['eq']", "OP_NOT_ALLOWED"),
        ("意料之外的文案", "QUERY_REJECTED"),
    ],
)
def test_failures_get_error_codes(script, capsys, text, code):
    assert script._emit(text) == 1
    err = _failure(capsys)
    assert err["code"] == code
    assert err["message"] == text


@pytest.mark.parametrize(
    "text",
    ['{"funds": {}}', "查询成功，实体 fund_products，共 1 行：\n| fund_code |\n| --- |\n| 000001 |"],
)
def test_successes_pass_through_unchanged(script, capsys, text):
    assert script._emit(text) == 0
    assert capsys.readouterr().out.strip() == text


def test_payload_can_come_from_stdin(script, monkeypatch, capsys):
    """tools.yaml 声明的原生工具传 --payload -，参数 JSON 走 stdin。"""
    import io

    async def _fake(client, fund_code, *, mode):
        return json.dumps({"fund_code": fund_code}, ensure_ascii=False)

    monkeypatch.setattr(script, "_build_client", lambda: _StubClient())
    monkeypatch.setattr(script, "run_share_siblings", _fake)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"fund_code": "000001"}'))
    assert script.main(["--action", "share_siblings", "--payload", "-"]) == 0
    assert json.loads(capsys.readouterr().out) == {"fund_code": "000001"}


# ------------------------------------------------------------------
# 各 action 的分发逻辑
# ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_query_rejects_disallowed_entity(script):
    result = await script._run_query({"entity": "not_a_real_entity"})
    assert "权限错误" in result
    assert "not_a_real_entity" in result


@pytest.mark.asyncio
async def test_run_multi_lookup_delegates_to_run_multi_fund_lookup(script, monkeypatch):
    calls = {}

    async def _fake(client, fund_codes, *, custtype=None, aspects=None, out_file=None):
        calls["fund_codes"] = fund_codes
        calls["custtype"] = custtype
        calls["aspects"] = aspects
        calls["out_file"] = out_file
        return "OK-MULTI"

    monkeypatch.setattr(script, "_build_client", lambda: _StubClient())
    monkeypatch.setattr(script, "run_multi_fund_lookup", _fake)

    result = await script._run_multi_lookup(
        {"fund_codes": ["000001"], "custtype": "1", "aspects": ["basic"]}
    )
    assert result == "OK-MULTI"
    assert calls == {
        "fund_codes": ["000001"],
        "custtype": "1",
        "aspects": ["basic"],
        "out_file": None,
    }


@pytest.mark.asyncio
async def test_run_share_siblings_delegates_and_uses_settings_mode(script, monkeypatch):
    calls = {}

    async def _fake(client, fund_code, *, mode):
        calls["fund_code"] = fund_code
        calls["mode"] = mode
        return "OK-SIBLINGS"

    monkeypatch.setattr(script, "_build_client", lambda: _StubClient())
    monkeypatch.setattr(script, "run_share_siblings", _fake)

    result = await script._run_share_siblings({"fund_code": "000001"})
    assert result == "OK-SIBLINGS"
    assert calls["fund_code"] == "000001"
    # mode 来自 PRODUCT_SHARE_SIBLING_MODE（环境变量 / .env），不由模型的 payload 提供
    assert calls["mode"]


def test_main_end_to_end_multi_lookup(script, monkeypatch, capsys):
    async def _fake(client, fund_codes, *, custtype=None, aspects=None, out_file=None):
        return json.dumps({"ok": True, "fund_codes": fund_codes}, ensure_ascii=False)

    monkeypatch.setattr(script, "_build_client", lambda: _StubClient())
    monkeypatch.setattr(script, "run_multi_fund_lookup", _fake)

    exit_code = script.main(
        [
            "--action",
            "multi_lookup",
            "--payload",
            json.dumps({"fund_codes": ["000001"]}),
        ]
    )
    out = capsys.readouterr().out
    assert exit_code == 0
    assert '"ok": true' in out
    assert "000001" in out
