"""LLM 请求/响应 body 日志开关测试。"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from csagent.infra.llm_client import (
    LlmClient,
    _dumps_body,
    append_json_array_object,
    conversation_log_stem,
    split_legacy_session_body_log,
    turn_log_stamp,
)
from csagent.infra.logging import bind_context, reset_context


def _settings(*, log_bodies: bool = True, log_dir: str | None = None) -> MagicMock:
    s = MagicMock()
    s.llm_api_key = "test-key"
    s.llm_base_url = "https://example.com/v1"
    s.llm_model = "test-model"
    s.llm_timeout_seconds = 30
    s.llm_max_retries = 1
    s.llm_temperature = 0.2
    s.llm_json_mode = True
    s.llm_log_bodies = log_bodies
    s.llm_log_bodies_dir = log_dir or "data/llm_bodies"
    s.llm_max_tokens = 131072
    return s


def test_dumps_includes_chinese_and_indent():
    text = _dumps_body({"messages": [{"content": "持仓查询"}]})
    assert "持仓查询" in text
    assert "\n" in text
    assert '  "messages"' in text


def test_conversation_log_stem_groups_wecom_room():
    assert conversation_log_stem(session_id="wecom:group:chatABC") == "wecom_group_chatABC"
    personal = conversation_log_stem(session_id="wecom:c2c:user1")
    assert personal == "wecom_c2c_user1"


def test_log_bodies_disabled_skips_info_log(tmp_path: Path):
    client = LlmClient(_settings(log_bodies=False, log_dir=str(tmp_path)))
    with patch("csagent.infra.llm_client.logger") as mock_logger:
        with patch("builtins.print") as mock_print:
            client._log_request_body({"model": "x", "messages": []}, mode="chat")
            client._log_response_body({"choices": []}, mode="chat", duration_ms=1.0)
            mock_logger.info.assert_not_called()
            mock_print.assert_not_called()
    assert list(tmp_path.rglob("*.json")) == []


def test_log_bodies_enabled_one_file_per_user_turn(tmp_path: Path):
    client = LlmClient(_settings(log_bodies=True, log_dir=str(tmp_path)))
    payload = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}
    tokens = bind_context(
        session_id="web-sess-1",
        channel="internal_agent",
        trace_id="trace-turn-a",
    )
    try:
        with patch("csagent.infra.llm_client.logger") as mock_logger:
            with patch("builtins.print") as mock_print:
                client._log_request_body(payload, mode="chat")
                client._log_response_body(
                    {"choices": [{"message": {"content": "ok"}}]},
                    mode="chat",
                    duration_ms=12.3,
                    attempt=1,
                )
                events = [call.args[0] for call in mock_logger.info.call_args_list]
                assert "llm_request_body" in events
                assert "llm_response_body" in events
                mock_print.assert_not_called()
    finally:
        reset_context(tokens)

    files = sorted(tmp_path.rglob("*.json"))
    assert len(files) == 1
    assert files[0].parent.name == "web-sess-1"
    assert files[0].name[:8].isdigit()
    raw = files[0].read_text(encoding="utf-8")
    assert raw.lstrip().startswith("[")
    records = json.loads(raw)
    assert isinstance(records, list)
    assert len(records) == 2
    assert records[0]["kind"] == "request"
    assert records[0]["body"] == payload
    assert records[1]["kind"] == "response"
    assert records[1]["body"]["choices"][0]["message"]["content"] == "ok"
    assert "LLM REQUEST" not in raw


def test_log_bodies_different_traces_get_different_files(tmp_path: Path):
    client = LlmClient(_settings(log_bodies=True, log_dir=str(tmp_path)))
    tokens_a = bind_context(session_id="wecom:BoWen", trace_id="trace-1", channel="wecom")
    try:
        client._log_request_body({"messages": [{"content": "a"}]}, mode="chat")
    finally:
        reset_context(tokens_a)
    tokens_b = bind_context(session_id="wecom:BoWen", trace_id="trace-2", channel="wecom")
    try:
        client._log_request_body({"messages": [{"content": "b"}]}, mode="chat")
    finally:
        reset_context(tokens_b)

    files = sorted((tmp_path / "wecom_BoWen").glob("*.json"))
    assert len(files) == 2


def test_split_legacy_session_body_log(tmp_path: Path):
    legacy = tmp_path / "wecom_BoWen.json"
    records = [
        {"ts": "2026-08-16T03:34:19.132+08:00", "trace_id": "aaaa1111", "kind": "request", "body": {"n": 1}},
        {"ts": "2026-08-16T03:34:20.001+08:00", "trace_id": "aaaa1111", "kind": "response", "body": {"n": 2}},
        {"ts": "2026-08-16T03:57:31.498+08:00", "trace_id": "bbbb2222", "kind": "request", "body": {"n": 3}},
    ]
    legacy.write_text(_dumps_body(records) + "\n", encoding="utf-8")
    written = split_legacy_session_body_log(legacy)
    assert len(written) == 2
    assert not legacy.exists()
    assert (tmp_path / "wecom_BoWen.legacy.json").is_file()
    names = sorted(p.name for p in written)
    assert "20260816_033419_132.json" in names
    assert "20260816_035731_498.json" in names
    first = json.loads((tmp_path / "wecom_BoWen" / "20260816_033419_132.json").read_text(encoding="utf-8"))
    assert len(first) == 2
    assert first[0]["body"]["n"] == 1


def test_turn_log_stamp_format():
    from datetime import datetime, timezone

    when = datetime(2026, 8, 16, 3, 34, 19, 132000, tzinfo=timezone.utc)
    assert turn_log_stamp(when) == "20260816_033419_132"


def test_append_json_array_object_keeps_valid_array(tmp_path: Path):
    path = tmp_path / "one.json"
    append_json_array_object(path, {"n": 1})
    append_json_array_object(path, {"n": 2})
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data == [{"n": 1}, {"n": 2}]


def test_build_payload_sets_max_tokens_128k():
    client = LlmClient(_settings())
    payload = client._build_payload(
        [{"role": "user", "content": "hi"}], json_mode=False, temperature=0.2
    )
    assert payload["max_tokens"] == 131072

    skipped = _settings()
    skipped.llm_max_tokens = 0
    payload = LlmClient(skipped)._build_payload(
        [{"role": "user", "content": "hi"}], json_mode=False, temperature=0.2
    )
    assert "max_tokens" not in payload
