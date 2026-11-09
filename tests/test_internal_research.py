"""skills/internal-research/scripts/*.py 单测。

脚本不在 src/csagent 包内（活在 skills/ 目录，由 run_script 起子进程执行），
用 importlib 按文件路径加载后直接调用其内部函数，不起真实子进程、不打真实内网服务。

对端已从「问答 Agent」改为「无状态检索服务」（见 docs/external-agent-api.md）：
search / context / reports / records / documents/content / download，不收 conversation_id，回答由本 Agent 自己组织。
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "skills"
    / "internal-research"
    / "scripts"
    / "ask.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("internal_research_script", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def script():
    return _load_module()


def _search_response() -> dict:
    return {
        "request_id": "req-1",
        "status": "success",
        "evidence_status": "sufficient",
        "passages": [
            {
                "evidence_id": "ev-1",
                "document_id": "doc-1",
                "attachment_id": "att-1",
                "title": "一周券商债市观点",
                "file_name": "观点.xlsx",
                "location": "债券观点!A1:H25",
                "text": "短期债市预计维持震荡。",
                "score": 0.03,
                "source_updated_at": "2026-08-20T08:00:00Z",
                "last_synced_at": "2026-08-21T08:00:00Z",
            }
        ],
        "citations": [
            {
                "source_type": "internal",
                "evidence_id": "ev-1",
                "document_id": "doc-1",
                "title": "一周券商债市观点",
                "file_name": "观点.xlsx",
                "location": "债券观点!A1:H25",
            }
        ],
        "knowledge_as_of": "2026-08-21T08:00:00Z",
        "error_code": None,
        "error_message": None,
    }


def _reports_response() -> dict:
    return {
        "request_id": "reports-1",
        "status": "success",
        "reports": [
            {
                "report_id": "rep-1",
                "title": "【利得基金】9月行业指数基金轮动策略月报 - 20260904",
                "report_date": "2026-09-04",
                "researcher": "示例研究员",
                "research_line": "示例研究条线",
                "research_type": "示例研究类型",
                "attachments": [
                    {
                        "attachment_id": "att-1",
                        "document_id": "doc-9",
                        "file_name": "【利得基金】9月行业指数基金轮动策略月报 - 20260904(3).pdf",
                        "parse_status": "success",
                    }
                ],
            }
        ],
        "total": 1,
        "page": 1,
        "page_size": 10,
        "has_more": False,
        "catalog_updated_at": "2026-09-04T08:00:00Z",
        "error_code": None,
        "error_message": None,
    }


def _context_response() -> dict:
    return {
        "request_id": "req-2",
        "status": "success",
        "evidence_status": "sufficient",
        "document_id": "doc-1",
        "title": "一周券商债市观点",
        "file_name": "观点.xlsx",
        "contexts": [
            {
                "evidence_id": "ev-1",
                "location": "债券观点!A1:H25",
                "text": "命中位置前两段、命中段和后两段……",
            }
        ],
        "knowledge_as_of": "2026-08-21T08:00:00Z",
        "error_code": None,
        "error_message": None,
    }


# ------------------------------------------------------------------
# 请求体：无状态检索面，绝不能再带 conversation_id
# ------------------------------------------------------------------
def test_search_sends_expected_body(script, monkeypatch):
    monkeypatch.delenv("RESEARCH_AGENT_CALLER_ID", raising=False)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_search_response())

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    data = asyncio.run(client.search("近期债市观点", filters={"research_type": "债券研究"}, top_k=5))
    asyncio.run(client.aclose())

    assert data["status"] == "success"
    assert seen["url"] == "http://agent.test:8000/api/v1/research/search"
    body = seen["body"]
    assert body["caller_id"] == "leadbank-cs-agent"
    assert body["query"] == "近期债市观点"
    assert body["top_k"] == 5
    assert body["filters"] == {"research_type": "债券研究"}
    assert body["request_id"]
    # 会话记忆在我们这边，对端不该收到任何会话标识
    assert "conversation_id" not in body


def test_context_sends_ids_and_clamps_neighbors(script):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_context_response())

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(client.context(document_id="doc-1", evidence_ids=["ev-1"], before=9, after=9))
    asyncio.run(client.aclose())

    assert seen["url"] == "http://agent.test:8000/api/v1/research/context"
    body = seen["body"]
    assert body["document_id"] == "doc-1"
    assert body["evidence_ids"] == ["ev-1"]
    assert body["before"] == 2 and body["after"] == 2  # 对端上限
    assert "conversation_id" not in body


def test_reports_sends_expected_body(script):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_reports_response())

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(
        client.list_reports(page=2, page_size=99, keyword="月报", filters={"researcher": "张三"})
    )
    asyncio.run(client.aclose())

    assert seen["url"] == "http://agent.test:8000/api/v1/research/reports/list"
    body = seen["body"]
    assert body["page"] == 2
    assert body["page_size"] == 50  # 对端上限
    assert body["keyword"] == "月报"
    assert body["filters"] == {"researcher": "张三"}
    assert "conversation_id" not in body


def test_reports_omits_empty_keyword_and_filters(script):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_reports_response())

    client = script.InternalResearchClient(transport=httpx.MockTransport(handler))
    asyncio.run(client.list_reports())
    asyncio.run(client.aclose())

    assert seen["body"]["page"] == 1 and seen["body"]["page_size"] == 10
    assert "keyword" not in seen["body"] and "filters" not in seen["body"]


def test_page_and_page_size_are_clamped(script):
    assert script.normalize_page(0) == 1
    assert script.normalize_page(10**9) == 100000
    assert script.normalize_page("abc") == 1
    assert script.normalize_page_size(0) == 1
    assert script.normalize_page_size(999) == 50
    assert script.normalize_page_size(None) == 10


def test_top_k_is_clamped_to_peer_range(script):
    assert script.normalize_top_k(0) == 1
    assert script.normalize_top_k(99) == 20
    assert script.normalize_top_k("abc") == 8


def test_unknown_filter_keys_are_dropped(script):
    kept = script.clean_filters(
        {"researcher": "张三", "date_from": "2026-08-01", "作者": "李四", "research_line": ""}
    )
    assert kept == {"researcher": "张三", "date_from": "2026-08-01"}


def test_caller_id_is_sanitized(script):
    client = script.InternalResearchClient(caller_id="利得 CS Agent")
    assert client.caller_id == "CS-Agent"


def test_http_error_becomes_runtime_error(script):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    client = script.InternalResearchClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="503"):
        asyncio.run(client.search("问题"))
    asyncio.run(client.aclose())


def test_422_says_request_body_is_invalid(script):
    """对端只有请求体不合法才回 4xx（FastAPI 422）——重试没有意义，文案要说清。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, text='{"detail":[{"loc":["body","query"]}]}')

    client = script.InternalResearchClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="重试无用"):
        asyncio.run(client.search("问题"))
    asyncio.run(client.aclose())


# ------------------------------------------------------------------
# search 结果格式化：原文片段 + 出处 + 扩展用的 ID
# ------------------------------------------------------------------
def test_format_search_keeps_source_and_ids(script):
    text = script.format_search_result(_search_response())
    assert "sufficient" in text
    assert "短期债市预计维持震荡。" in text
    assert "观点.xlsx" in text and "债券观点!A1:H25" in text
    # context 要靠这两个 ID，不能被截断或改写
    assert "document_id=doc-1" in text and "evidence_id=ev-1" in text
    assert "2026-08-21T08:00:00Z" in text  # 知识库同步时间


def test_format_search_insufficient_forbids_making_things_up(script):
    data = _search_response()
    data["evidence_status"] = "insufficient"
    data["passages"] = []
    data["citations"] = []
    text = script.format_search_result(data)
    assert "0 条" in text
    assert "禁止" in text or "不要用自己的知识" in text


def test_format_search_service_unavailable_is_not_a_miss(script):
    data = _search_response()
    data["evidence_status"] = "service_unavailable"
    data["passages"] = []
    text = script.format_search_result(data)
    assert "不是「内部没有资料」" in text


def test_long_passage_text_is_truncated(script):
    data = _search_response()
    data["passages"][0]["text"] = "长" * 5000
    text = script.format_search_result(data)
    assert "本段被截断" in text
    assert len(text) < 3000


def test_many_passages_spill_to_outbound_file(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))
    data = _search_response()
    one = data["passages"][0]
    data["passages"] = [dict(one, evidence_id=f"ev-{i}", text="正文" * 200) for i in range(20)]
    # 显式压低预算，验证超限落盘（默认 OUTPUT_CHAR_BUDGET 已到 2.8 万）
    text = script.format_search_result(data, char_budget=2000, out_file="hits.md")

    assert len(text) < 8000
    saved = tmp_path / "hits.md"
    assert saved.is_file()
    assert "ev-19" in saved.read_text(encoding="utf-8")
    assert "read_file" in text
    assert "offset=" in text and "limit=" in text
    assert "禁止一次" in text or "不带 limit" in text


def test_out_file_cannot_escape_outbound_dir(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))
    data = _search_response()
    data["passages"] = [dict(data["passages"][0], text="正文" * 500) for _ in range(20)]
    script.format_search_result(data, char_budget=2000, out_file="../escaped.md")
    assert not (tmp_path.parent / "escaped.md").exists()
    assert (tmp_path / "escaped.md").is_file()


# ------------------------------------------------------------------
# reports 结果格式化：目录，不是正文
# ------------------------------------------------------------------
def test_format_reports_lists_date_title_and_attachment(script):
    text = script.format_reports_result(_reports_response())
    assert "2026-09-04" in text
    assert "9月行业指数基金轮动策略月报" in text
    assert "(3).pdf" in text and "success" in text
    assert "共 1 条" in text
    # 追问路径：report_id → record → content；report_id 必须出现
    assert "report_id=rep-1" in text
    assert "record" in text and "content" in text


def test_format_reports_flags_unparsed_attachment(script):
    data = _reports_response()
    data["reports"][0]["attachments"][0]["parse_status"] = "failed"
    text = script.format_reports_result(data)
    assert "正文可能取不到" in text


def test_format_reports_empty_is_not_a_failure(script):
    data = _reports_response()
    data["reports"] = []
    data["total"] = 0
    text = script.format_reports_result(data)
    assert "本页 0 条" in text
    assert "不要改用 search" in text


def test_format_reports_unavailable_is_not_empty(script):
    """故障时 total/has_more 为 null，不能当成「没有报告」。"""
    data = _reports_response()
    data["status"] = "unavailable"
    data["error_code"] = "request_timeout"
    data["reports"] = []
    data["total"] = None
    data["has_more"] = None
    text = script.format_reports_result(data)
    assert "调用失败" in text
    assert "不等于「内部没有这份资料」" in text


def test_format_reports_spills_to_file_when_long(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))
    data = _reports_response()
    one = data["reports"][0]
    data["reports"] = [dict(one, title="标" * 300, report_id=f"rep-{i}") for i in range(50)]
    data["total"] = 50
    text = script.format_reports_result(data, char_budget=2000, out_file="catalog.md")
    assert len(text) < 8000
    assert (tmp_path / "catalog.md").is_file()
    assert "read_file" in text


# ------------------------------------------------------------------
# context 结果格式化
# ------------------------------------------------------------------
def test_format_context_shows_document_and_text(script):
    text = script.format_context_result(_context_response())
    assert "一周券商债市观点" in text
    assert "命中位置前两段" in text


def test_format_context_not_found_stops_retrying(script):
    data = _context_response()
    data["evidence_status"] = "not_found"
    text = script.format_context_result(data)
    assert "not_found" in text
    assert "别再重试" in text


def test_format_context_long_text_spills_to_file(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))
    data = _context_response()
    data["contexts"][0]["text"] = "长" * 12000
    text = script.format_context_result(data, char_budget=2000, out_file="ctx.md")
    assert len(text) < 8000
    assert (tmp_path / "ctx.md").is_file()
    assert "read_file" in text


# ------------------------------------------------------------------
# 业务错误：HTTP 200 + error_code
# ------------------------------------------------------------------
def test_format_request_timeout_is_retryable_and_not_a_miss(script):
    data = _search_response()
    data["status"] = "unavailable"
    data["error_code"] = "request_timeout"
    data["error_message"] = "knowledge service timed out"
    data["evidence_status"] = "service_unavailable"
    text = script.format_search_result(data)
    assert "request_timeout" in text
    assert "再试一次" in text
    assert "不等于「内部没有这份资料」" in text


def test_format_internal_error_is_not_retryable(script):
    data = _search_response()
    data["status"] = "failed"
    data["error_code"] = "internal_error"
    text = script.format_search_result(data)
    assert "别把同一个请求原样再发一遍" in text


def test_format_contract_error_points_at_parameters(script):
    data = _context_response()
    data["status"] = "failed"
    data["error_code"] = "knowledge_contract_error"
    text = script.format_context_result(data)
    assert "参数配错" in text


# ------------------------------------------------------------------
# CLI 层：参数校验与失败文案
# ------------------------------------------------------------------
def test_main_rejects_bad_payload(script, capsys):
    assert script.main(["--action", "search", "--payload", "not-json"]) == 1
    assert "不是合法 JSON" in capsys.readouterr().out


def test_main_requires_query(script, capsys):
    assert script.main(["--action", "search", "--payload", "{}"]) == 1
    assert "query 必填" in capsys.readouterr().out


def test_main_rejects_overlong_query(script, capsys):
    payload = json.dumps({"query": "问" * 1001})
    assert script.main(["--action", "search", "--payload", payload]) == 1
    assert "超过 1000 字符" in capsys.readouterr().out


def test_reports_rejects_instruction_as_keyword(script, capsys):
    payload = json.dumps({"keyword": "请列出最新的十份研究报告并按日期排序给我看" * 2})
    assert script.main(["--action", "reports", "--payload", payload]) == 1
    out = capsys.readouterr().out
    assert "keyword" in out
    assert "不是问题" in out


def test_context_requires_ids_from_search(script, capsys):
    assert script.main(["--action", "context", "--payload", '{"document_id":"doc-1"}']) == 1
    out = capsys.readouterr().out
    assert "evidence_ids" in out
    assert "不能自己编" in out


# ------------------------------------------------------------------
# record / content / download：目录索引 + 按 ID 读全文
# ------------------------------------------------------------------
def _record_response() -> dict:
    return {
        "request_id": "record-1",
        "status": "success",
        "record": {
            "record_id": "rep-1",
            "external_record_id": "ext-1",
            "row_number": 12,
            "record_version": 3,
            "title": "一周券商债市观点",
            "business_fields": {"研究员": "张三", "课题完成时间": "2026-08-20"},
            "redacted_fields": ["secret_url"],
            "source_document_id": "tencent-doc",
            "source_updated_at": "2026-08-20T08:00:00Z",
            "last_synced_at": "2026-08-21T08:00:00Z",
            "document_id": "row-summary-doc",
            "attachments": [
                {
                    "attachment_id": "att-1",
                    "document_id": "doc-1",
                    "document_version": "ver-1",
                    "file_name": "观点.xlsx",
                    "mime_type": "application/vnd.ms-excel",
                    "byte_size": 1024,
                    "content_hash": "a" * 64,
                    "download_status": "ready",
                    "download_available": True,
                    "parse_status": "success",
                },
                {
                    "attachment_id": "att-2",
                    "document_id": None,
                    "document_version": None,
                    "file_name": "附图.pdf",
                    "mime_type": "application/pdf",
                    "byte_size": 2048,
                    "content_hash": "b" * 64,
                    "download_available": True,
                    "parse_status": "failed",
                    "processing_error_category": "ocr_failed",
                },
            ],
        },
        "error_code": None,
        "error_message": None,
    }


def _content_response(*, has_more: bool = True) -> dict:
    return {
        "request_id": "content-1",
        "status": "success",
        "content": {
            "document_id": "doc-1",
            "record_id": "rep-1",
            "attachment_id": "att-1",
            "title": "一周券商债市观点",
            "file_name": "观点.xlsx",
            "version_hash": "ver-1",
            "last_synced_at": "2026-08-21T08:00:00Z",
            "parse_status": "success",
            "total_chunks": 3,
            "total_chars": 100,
            "returned_chars": 40,
            "has_more": has_more,
            "next_cursor": "cursor-2" if has_more else None,
            "content_scope": "all_indexed_text",
            "source_completeness": "not_verified",
            "overlap_possible": True,
            "parts": [
                {
                    "evidence_id": "ev-1",
                    "chunk_index": 0,
                    "char_start": 0,
                    "char_end": 20,
                    "chunk_chars": 20,
                    "text": "短期债市预计维持震荡。",
                    "sheet_name": "债券观点",
                    "cell_range": "A1:H25",
                }
            ],
        },
        "error_code": None,
        "error_message": None,
    }


def test_get_record_sends_expected_body(script):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_record_response())

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    data = asyncio.run(client.get_record("rep-1"))
    asyncio.run(client.aclose())

    assert data["status"] == "success"
    assert seen["url"] == "http://agent.test:8000/api/v1/research/records/get"
    assert seen["body"]["record_id"] == "rep-1"
    assert "conversation_id" not in seen["body"]


def test_read_document_sends_cursor_and_clamps(script):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json=_content_response())

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(
        client.read_document(
            "doc-1",
            expected_version="ver-1",
            limit=99,
            max_chars=10,
            cursor="cursor-1",
        )
    )
    asyncio.run(client.aclose())

    body = seen["body"]
    assert body["document_id"] == "doc-1"
    assert body["expected_version"] == "ver-1"
    assert body["limit"] == 50
    assert body["max_chars"] == 1000
    assert body["cursor"] == "cursor-1"
    assert "conversation_id" not in body


def test_content_limit_and_chars_helpers(script):
    assert script.normalize_content_limit(0) == 1
    assert script.normalize_content_limit(99) == 50
    assert script.normalize_content_limit(None) == 20
    assert script.normalize_max_chars(10) == 1000
    assert script.normalize_max_chars(999999) == 60000
    assert script.normalize_max_chars(None) == 24000


def test_format_record_shows_business_fields_and_all_attachments(script):
    text = script.format_record_result(_record_response())
    assert "record_id=rep-1" in text
    assert "row_number=12" in text and "不能当唯一标识" in text
    assert "张三" in text
    assert "attachment_id=att-1" in text and "document_id=doc-1" in text
    assert "document_version=ver-1" in text
    assert "att-2" in text and "failed" in text
    assert "行摘要" in text
    assert "content" in text


def test_format_content_exposes_range_and_cursor(script):
    text = script.format_content_result(_content_response(has_more=True))
    assert "本次返回 40 字符" in text
    assert "还有后续" in text
    assert "next_cursor=cursor-2" in text
    assert "短期债市预计维持震荡。" in text
    assert "overlap_possible" in text


def test_format_content_end_does_not_claim_prior_pages_read(script):
    text = script.format_content_result(_content_response(has_more=False))
    assert "has_more=false" in text
    assert "不能宣称整篇已审阅" in text or "从 cursor=null" in text


def test_format_content_resource_not_found(script):
    data = {
        "status": "failed",
        "error_code": "resource_not_found",
        "error_message": "gone",
        "content": None,
    }
    text = script.format_content_result(data)
    assert "resource_not_found" in text
    assert "刷新" in text


def test_download_saves_file_and_verifies_hash(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))
    payload = b"hello-research-file"
    digest = __import__("hashlib").sha256(payload).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/api/v1/research/attachments/att-1/download" in str(request.url)
        assert f"expected_content_hash={digest}" in str(request.url)
        return httpx.Response(
            200,
            content=payload,
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'attachment; filename="ignored-by-file_name.pdf"',
                "X-Content-SHA256": digest,
            },
        )

    client = script.InternalResearchClient(
        base_url="http://agent.test:8000",
        transport=httpx.MockTransport(handler),
    )
    data = asyncio.run(
        client.download_attachment(
            "att-1",
            content_hash=digest,
            file_name="20260731【利得策略】8月大类资产配置及投资策略.pdf",
        )
    )
    asyncio.run(client.aclose())

    assert data["saved_as"].endswith(".pdf")
    assert "大类资产配置" in data["saved_as"]
    assert (tmp_path / data["saved_as"]).read_bytes() == payload
    text = script.format_download_result(data)
    assert data["saved_as"] in text and "SHA-256" in text
    assert ".bin" not in text or "不要发 .bin" in text


def test_resolve_download_filename_prefers_original_and_mime(script):
    assert script.resolve_download_filename(file_name="报告.pdf") == "报告.pdf"
    assert script.resolve_download_filename(
        content_type="application/pdf", attachment_id="abcdef12"
    ).endswith(".pdf")
    assert script.resolve_download_filename(
        content_disposition='attachment; filename="foo.xlsx"', attachment_id="x"
    ) == "foo.xlsx"


def test_main_download_requires_file_name(script, capsys):
    assert (
        script.main(
            [
                "--action",
                "download",
                "--payload",
                '{"attachment_id":"att-1","content_hash":"' + ("a" * 64) + '"}',
            ]
        )
        == 1
    )
    out = capsys.readouterr().out
    assert "file_name" in out


def test_download_rejects_hash_mismatch(script, tmp_path, monkeypatch):
    monkeypatch.setenv("CSAGENT_OUTBOUND_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"tampered")

    client = script.InternalResearchClient(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="SHA-256"):
        asyncio.run(client.download_attachment("att-1", content_hash="a" * 64))
    asyncio.run(client.aclose())


def test_main_record_requires_id(script, capsys):
    assert script.main(["--action", "record", "--payload", "{}"]) == 1
    assert "record_id" in capsys.readouterr().out


def test_main_content_requires_document_id(script, capsys):
    assert script.main(["--action", "content", "--payload", "{}"]) == 1
    assert "document_id" in capsys.readouterr().out


def test_main_download_requires_hash(script, capsys):
    assert script.main(["--action", "download", "--payload", '{"attachment_id":"att-1"}']) == 1
    out = capsys.readouterr().out
    assert "content_hash" in out



# ------------------------------------------------------------------
# 原生工具出口：stdin 入参、失败统一信封 + 非 0 退出码
# ------------------------------------------------------------------
def _run_cli(script, monkeypatch, capsys, action, payload, handler):
    """按 tools.yaml 的方式跑 main()：参数走 stdin，对端换成 MockTransport。"""
    import io

    monkeypatch.setattr(
        script,
        "_build_client",
        lambda _payload: script.InternalResearchClient(
            base_url="http://agent.test:8000", transport=httpx.MockTransport(handler)
        ),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload, ensure_ascii=False)))
    code = script.main(["--action", action, "--payload", "-"])
    return code, capsys.readouterr().out


def test_cli_reads_payload_from_stdin(script, monkeypatch, capsys):
    code, out = _run_cli(
        script, monkeypatch, capsys, "search", {"query": "近期债市观点"},
        lambda request: httpx.Response(200, json=_search_response()),
    )
    assert code == 0
    assert "短期债市预计维持震荡。" in out


def test_cli_insufficient_evidence_is_still_a_success(script, monkeypatch, capsys):
    """内部确实没有资料是有效答案，不是调用失败。"""
    data = _search_response()
    data.update(evidence_status="insufficient", passages=[], citations=[])
    code, out = _run_cli(
        script, monkeypatch, capsys, "search", {"query": "冷门主题"},
        lambda request: httpx.Response(200, json=data),
    )
    assert code == 0
    assert "0 条" in out


def test_cli_service_unavailable_search_is_a_failure(script, monkeypatch, capsys):
    data = _search_response()
    data.update(evidence_status="service_unavailable", passages=[])
    code, out = _run_cli(
        script, monkeypatch, capsys, "search", {"query": "近期债市观点"},
        lambda request: httpx.Response(200, json=data),
    )
    assert code == 1
    assert json.loads(out)["error"]["code"] == "KNOWLEDGE_SERVICE_UNAVAILABLE"


def test_cli_peer_error_code_becomes_the_failure_code(script, monkeypatch, capsys):
    data = _context_response()
    data.update(status="failed", error_code="knowledge_contract_error", error_message="cursor 不匹配")
    code, out = _run_cli(
        script, monkeypatch, capsys, "context", {"document_id": "doc-1", "evidence_ids": ["ev-1"]},
        lambda request: httpx.Response(200, json=data),
    )
    err = json.loads(out)["error"]
    assert code == 1
    assert err["code"] == "KNOWLEDGE_CONTRACT_ERROR"
    assert "参数配错" in err["message"]


def test_cli_unreachable_service(script, monkeypatch, capsys):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    code, out = _run_cli(script, monkeypatch, capsys, "record", {"record_id": "rep-1"}, handler)
    err = json.loads(out)["error"]
    assert code == 1
    assert err["code"] == "UPSTREAM_UNREACHABLE"
    assert "research_capabilities" in err["message"]


def test_cli_http_422_is_bad_payload(script, monkeypatch, capsys):
    code, out = _run_cli(
        script, monkeypatch, capsys, "search", {"query": "近期债市观点"},
        lambda request: httpx.Response(422, text='{"detail":[]}'),
    )
    assert code == 1
    assert json.loads(out)["error"]["code"] == "BAD_PAYLOAD"


@pytest.mark.parametrize(
    ("message", "code"),
    [
        ("附件下载 /api/x -> HTTP 404（文件不可用）: ", "RESOURCE_NOT_FOUND"),
        ("附件下载 /api/x -> HTTP 409（内容版本已变）: ", "RESOURCE_NOT_READY_OR_CHANGED"),
        ("下载内容 SHA-256 与 expected_content_hash 不一致：expected=a actual=b", "CONTENT_HASH_MISMATCH"),
        ("内部研究资料服务 /api/v1/research/search -> HTTP 500: boom", "UPSTREAM_ERROR"),
    ],
)
def test_http_failures_get_codes(script, message, code):
    assert script._http_failure(RuntimeError(message)).code == code


def test_cli_argument_errors_use_the_envelope(script, capsys):
    assert script.main(["--action", "search", "--payload", "not-json"]) == 1
    err = json.loads(capsys.readouterr().out)["error"]
    assert err["code"] == "BAD_PAYLOAD"
    assert "不是合法 JSON" in err["message"]
