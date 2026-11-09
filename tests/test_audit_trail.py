"""审计留痕：状态改动证据存档、trace 透传到下游。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from csagent.audit import evidence
from csagent.audit.trace_buffer import AuditTraceBuffer, audit_trace_var
from csagent.react.models import CustomerScope
from csagent.tools.packs.script_pack import WriteScriptTool


@pytest.mark.asyncio
async def test_write_script_evidence_is_archived_only_inside_a_request(tmp_path: Path) -> None:
    blobs = evidence._blob_root_override
    assert blobs is not None  # conftest 已隔离到 tmp
    tool = WriteScriptTool(scripts_dir=tmp_path / "scripts", allowed_extensions=[".py"])
    scope = CustomerScope(session_id="s-ev")

    await tool.execute({"path": "a.py", "content": "print('outside')"}, scope)
    assert not Path(blobs).exists()  # 没有请求上下文：不登记、也不留无主存档

    token = audit_trace_var.set(AuditTraceBuffer())
    try:
        await tool.execute({"path": "b.py", "content": "print('inside')"}, scope)
        change = audit_trace_var.get().state_changes[-1]
    finally:
        audit_trace_var.reset(token)
    assert change["kind"] == "write_script"
    assert (Path(blobs).parent / change["blob"]).read_text(encoding="utf-8") == "print('inside')"


def test_bridge_client_sends_trace_and_session_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    path = (Path(__file__).resolve().parents[1] / "skills" / "fund-product-query"
            / "scripts" / "product_center_client.py")
    spec = importlib.util.spec_from_file_location("_pcc_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    monkeypatch.setenv("CSAGENT_TRACE_ID", "trace-123")
    monkeypatch.setenv("CSAGENT_SESSION_ID", "web:u1")
    client_cls = next(v for v in vars(module).values()
                      if isinstance(v, type) and hasattr(v, "fund_detail"))
    client = client_cls(base_url="http://bridge.invalid", token="t")
    headers = client._client.headers
    assert headers["X-Csagent-Trace-Id"] == "trace-123"
    assert headers["X-Csagent-Session"] == "web:u1"
    assert headers["X-Bridge-Token"] == "t"
