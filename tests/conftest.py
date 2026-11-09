"""Ensure tests import this workspace's package, not another install."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
_src_str = str(_SRC)
if _src_str not in sys.path:
    sys.path.insert(0, _src_str)

# fund-product-query 的业务模块活在 skills/ 目录（不属于 csagent 包），生产环境靠
# `python query.py` 子进程自动把脚本目录塞进 sys.path[0] 做同目录 import；测试里
# 直接 import 这些模块（如 product_center_backend）时手动补上同样的路径。
_FUND_QUERY_SCRIPTS = (
    Path(__file__).resolve().parents[1] / "skills" / "fund-product-query" / "scripts"
)
_fund_query_scripts_str = str(_FUND_QUERY_SCRIPTS)
if _fund_query_scripts_str not in sys.path:
    sys.path.insert(0, _fund_query_scripts_str)

# If a different csagent was already imported (e.g. sibling editable install), drop it.
for _mod in list(sys.modules):
    if _mod == "csagent" or _mod.startswith("csagent."):
        del sys.modules[_mod]


@pytest.fixture(autouse=True)
def _isolate_uploads_root(tmp_path_factory, monkeypatch):
    """把附件暂存根重定向到 tmp，禁止测试写进真实 data/uploads/。

    不加这层隔离时，任何直接构造 MessageGateway / stage_attachment 而没有自行
    打桩的测试，都会往仓库里的 data/uploads/{session}/ 落文件——每跑一次多一份，
    `s1`、`wecom_u1` 这些目录就是这么堆起来的。
    单个测试仍可继续显式 monkeypatch 这两个常量，后设置的生效。
    """
    root = tmp_path_factory.mktemp("uploads_root")
    monkeypatch.setattr(
        "csagent.channels.workspace_uploads.DEFAULT_UPLOADS_ROOT", root
    )
    # media / HTTP 上传在 import 时把该常量绑成了自己的名字，需一并改写
    monkeypatch.setattr("csagent.wecom.media.DEFAULT_UPLOAD_ROOT", root, raising=False)
    monkeypatch.setattr(
        "csagent.api.router_files.DEFAULT_UPLOADS_ROOT", root, raising=False
    )
    return root


@pytest.fixture(autouse=True)
def _isolate_artifact_store(monkeypatch):
    """全局产物登记库逐个测试复位。

    装配类测试（test_agent_assembly 调 ``_build_app_components``）会把它指向真实的
    ``data/csagent.db`` 且不复原，之后任何 send_file / 上传类用例都会往开发机的真实库里
    登记测试产物（排查时发现已积累了几十条 web:zhang3 / web:li4 记录）。
    需要登记库的用例在用例内自己 ``set_artifact_store``，结束后这里还原。
    """
    monkeypatch.setattr("csagent.memory.artifact_store._store", None)


@pytest.fixture(autouse=True)
def _isolate_security_state(tmp_path_factory, monkeypatch):
    """安全相关的进程级状态逐个测试隔离。

    - 安全审计写到 tmp：管理接口、下载接口的测试会产生事件，不能落进真实 data/audit/；
    - 密钥遮挡器置空：默认会从本机 .env 收集真实密钥值，测试结果不能取决于开发机配置，
      需要它的用例自己用 ``reset_secret_masker`` / monkeypatch 注入；
    - 读路径放行前缀清空：个别用例会打开它，不能串到其它用例。
    """
    from csagent.audit.security_logger import SecurityAuditLogger
    from csagent.common.secrets import SecretMasker

    audit_dir = tmp_path_factory.mktemp("security_audit")
    monkeypatch.setattr(
        "csagent.audit.security_logger._instance", SecurityAuditLogger(audit_dir)
    )
    monkeypatch.setattr("csagent.common.secrets._masker", SecretMasker([]))
    monkeypatch.setattr("csagent.channels.workspace_uploads._read_escape_prefixes", ())
    # 审计证据存档（脚本 / 文件副本）同样落 tmp，不进真实 data/audit/blobs/
    monkeypatch.setattr("csagent.audit.evidence._blob_root_override", audit_dir / "blobs")
    return audit_dir
