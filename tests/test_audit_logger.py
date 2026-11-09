"""审计日志持久化测试。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from csagent.audit.session_logger import SessionAuditLogger, SessionAuditRecord
from csagent.audit.trace_buffer import AuditTraceBuffer


@pytest.mark.asyncio
async def test_session_audit_logger_writes_jsonl(tmp_path: Path):
    logger = SessionAuditLogger(audit_dir=tmp_path, enabled=True)
    logger.start()
    record = SessionAuditRecord(
        trace_id="abc123",
        session_id="sess-1",
        channel="internal_agent",
        user_message="我的分红到账了吗",
        final_reply="近期有一笔分红确认记录。",
        status="success",
        domains=["trade"],
        thought_trace=["【主Agent】进入 ReAct"],
    )
    await logger.write(record)
    await logger.stop()

    files = list(tmp_path.glob("sessions-*.jsonl"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8").strip())
    assert payload["trace_id"] == "abc123"
    assert payload["domains"] == ["trade"]
    assert payload["status"] == "success"
    assert payload["thought_trace"] == ["【主Agent】进入 ReAct"]


@pytest.mark.asyncio
async def test_session_audit_logger_disabled(tmp_path: Path):
    logger = SessionAuditLogger(audit_dir=tmp_path, enabled=False)
    await logger.write(
        SessionAuditRecord(
            trace_id="x",
            session_id="s",
            channel="internal_agent",
            user_message="hi",
            final_reply="ok",
            status="success",
        )
    )
    assert list(tmp_path.glob("*.jsonl")) == []


@pytest.mark.asyncio
async def test_summary_carries_llm_pointer_not_a_copy(tmp_path: Path):
    """LLM 消息全文不进审计：摘要只留计数与指向 data/llm_bodies 的直链。

    以前这里会另写一份 detail/*.md，内容是 llm_bodies 的**有损**副本
    （单条 8000 / 20000 字符截断），且没有 tools schema 与 token 用量，
    还没有任何读取方——纯属同一份信息写两遍。
    """
    logger = SessionAuditLogger(audit_dir=tmp_path, enabled=True)
    logger.start()
    buf = AuditTraceBuffer()
    buf.add_thought("【Agent】开始处理")
    buf.note_llm_call(prompt_tokens=12, completion_tokens=3)
    buf.note_llm_call(prompt_tokens=8, completion_tokens=2)
    buf.set_llm_body_path("data/llm_bodies/wecom_BoWen/20260816_132820_163.json")
    # 同一 trace 多轮共用一个原文文件，后续登记不应覆盖
    buf.set_llm_body_path("data/llm_bodies/other/999.json")
    buf.set_context_layers({"system": 40, "checkpoint": 9})

    record = SessionAuditRecord(
        trace_id="detail001",
        session_id="sess-d",
        channel="http_api",
        user_message="列出当前目录",
        final_reply="目录如下",
        status="success",
        domains=["generic"],
    )
    record = logger.fill_from_buffer(record, buf)
    await logger.write(record)
    await logger.stop()

    assert not (tmp_path / "detail").exists(), "不应再产出 detail 目录"

    summary = json.loads(next(tmp_path.glob("sessions-*.jsonl")).read_text(encoding="utf-8"))
    assert summary["llm_turn_count"] == 2
    assert summary["llm_body_path"] == "data/llm_bodies/wecom_BoWen/20260816_132820_163.json"
    assert summary["prompt_tokens"] == 20
    assert summary["completion_tokens"] == 5
    assert summary["context_layers"] == {"system": 40, "checkpoint": 9}
    assert "llm_turns" not in summary
    assert "detail_path" not in summary
    # 体积可控的部分照常全量记录
    assert summary["thought_trace"] == ["【Agent】开始处理"]


@pytest.mark.asyncio
async def test_audit_still_written_without_any_llm_call(tmp_path: Path):
    """没发生 LLM 调用（如输入被护栏拦下）时，摘要照写，指针为空。"""
    logger = SessionAuditLogger(audit_dir=tmp_path, enabled=True)
    logger.start()
    record = SessionAuditRecord(
        trace_id="blocked01",
        session_id="s",
        channel="wecom",
        user_message="...",
        final_reply="",
        status="blocked_input",
    )
    record = logger.fill_from_buffer(record, AuditTraceBuffer())
    await logger.write(record)
    await logger.stop()

    summary = json.loads(next(tmp_path.glob("sessions-*.jsonl")).read_text(encoding="utf-8"))
    assert summary["llm_turn_count"] == 0
    assert summary["llm_body_path"] is None
    assert summary["prompt_tokens"] == 0
    assert summary["completion_tokens"] == 0
    assert summary["context_layers"] == {}
