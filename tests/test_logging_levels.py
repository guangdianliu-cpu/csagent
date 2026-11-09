"""日志体系：一个 LOG_LEVEL 主开关带动所有落盘产物（对齐 logback root level）。

规整前有三套互不相干的开关（LOG_LEVEL / LLM_LOG_BODIES / AUDIT_LOG_ENABLED +
AUDIT_CAPTURE_LLM），且同一开关在 .env 与 base.yaml 各配一份、YAML 无条件覆盖
——`.env` 里写 AUDIT_CAPTURE_LLM=false 实际跑的是 True，改了不生效。
现在 audit/detail 与**全量**运行日志文件 app.log 都已移除（前者是 llm_bodies
的有损副本，后者在 DEBUG 下几乎全是心跳噪音）。只剩：stdout 流水（跟随
LOG_LEVEL）、``data/logs/error.log``（**恒定 ERROR，不跟随 LOG_LEVEL**）、
会话审计摘要、LLM 原文。
"""

from __future__ import annotations

import importlib
import logging
import tempfile
from pathlib import Path

import pytest
import structlog


def _settings(monkeypatch: pytest.MonkeyPatch, **env: str):
    """按给定环境变量重新解析一次配置。"""
    for key in (
        "LOG_LEVEL",
        "LLM_LOG_BODIES",
        "AUDIT_LOG_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    import csagent.infra.config as cfg

    importlib.reload(cfg)
    return cfg.get_settings()


# ---------------------------------------------------------------- 级别联动


@pytest.mark.parametrize("level", ["ERROR", "WARNING", "INFO"])
def test_heavy_sinks_off_below_debug(monkeypatch: pytest.MonkeyPatch, level: str):
    """LLM 原始报文是排查产物，非 DEBUG 一律不落盘。"""
    s = _settings(monkeypatch, LOG_LEVEL=level)
    assert s.llm_log_bodies is False


def test_debug_turns_on_heavy_sinks(monkeypatch: pytest.MonkeyPatch):
    s = _settings(monkeypatch, LOG_LEVEL="DEBUG")
    assert s.llm_log_bodies is True


@pytest.mark.parametrize("level", ["ERROR", "INFO", "DEBUG"])
def test_audit_summary_is_business_record_not_a_log_level(
    monkeypatch: pytest.MonkeyPatch, level: str
):
    """会话审计摘要是业务留痕，任何级别下都不该被关掉。"""
    s = _settings(monkeypatch, LOG_LEVEL=level)
    assert s.audit_log_enabled is True


# ---------------------------------------------------------------- 显式覆盖


def test_explicit_env_can_force_sink_on_in_production(monkeypatch: pytest.MonkeyPatch):
    """生产临时抓包：显式开关优先于级别（等价 logback 给单个 appender 配 level）。"""
    s = _settings(monkeypatch, LOG_LEVEL="ERROR", LLM_LOG_BODIES="true")
    assert s.llm_log_bodies is True


def test_explicit_env_can_force_sink_off_in_debug(monkeypatch: pytest.MonkeyPatch):
    s = _settings(monkeypatch, LOG_LEVEL="DEBUG", LLM_LOG_BODIES="false")
    assert s.llm_log_bodies is False


def test_audit_detail_switch_is_gone(monkeypatch: pytest.MonkeyPatch):
    """audit/detail 已移除，配置面上不该再有它的开关。"""
    s = _settings(monkeypatch, LOG_LEVEL="DEBUG")
    assert not hasattr(s, "audit_capture_llm")


def test_env_wins_over_yaml(monkeypatch: pytest.MonkeyPatch):
    """回归：YAML 曾无条件覆盖 .env，导致开关改了不生效。"""
    s = _settings(monkeypatch, LOG_LEVEL="INFO", AUDIT_LOG_ENABLED="false")
    assert s.audit_log_enabled is False


# ---------------------------------------------------------------- appender


def test_console_appender_renders_context(capsys: pytest.CaptureFixture[str]):
    """唯一的 appender 是 stdout；contextvars 绑定的字段要跟着渲染出来。"""
    from csagent.infra.logging import bind_context, configure_logging, reset_context

    configure_logging("INFO", "json")
    log = structlog.get_logger("test_appender")

    token = bind_context(trace_id="t-1", session_id="s-1")
    log.info("business_event", rows=11)
    log.debug("filtered_at_info")
    reset_context(token)

    out = capsys.readouterr().out
    assert '"event": "business_event"' in out
    assert '"trace_id": "t-1"' in out
    assert '"rows": 11' in out
    assert "filtered_at_info" not in out


def test_error_file_only_takes_errors_even_at_debug():
    """核心性质：error.log 恒定 ERROR，不跟随 LOG_LEVEL。

    root 开到 DEBUG（排查态）时，控制台该刷什么刷什么，但 error.log 里
    只能有 ERROR —— 否则它会重蹈 app.log 的覆辙，被心跳流水灌满。
    """
    from csagent.infra.logging import configure_logging

    tmp = Path(tempfile.mkdtemp())
    configure_logging(
        "DEBUG", "console", error_log_enabled=True, error_log_dir=str(tmp), project_root=tmp
    )
    log = structlog.get_logger("test_error_file")
    log.debug("noisy_heartbeat")
    log.info("business_flow")
    log.warning("retrying_once")
    log.error("gave_up_for_real")

    content = (tmp / "error.log").read_text(encoding="utf-8")
    assert '"event": "gave_up_for_real"' in content
    for noise in ("noisy_heartbeat", "business_flow", "retrying_once"):
        assert noise not in content, f"{noise} 不该进 error.log"


def test_error_file_carries_context_as_json():
    from csagent.infra.logging import bind_context, configure_logging, reset_context

    tmp = Path(tempfile.mkdtemp())
    configure_logging(
        "ERROR", "console", error_log_enabled=True, error_log_dir=str(tmp), project_root=tmp
    )
    token = bind_context(trace_id="t-1", session_id="s-1")
    structlog.get_logger("test_ctx").error("llm_rate_limited", status=429)
    reset_context(token)

    content = (tmp / "error.log").read_text(encoding="utf-8")
    assert '"trace_id": "t-1"' in content
    assert '"status": 429' in content


def test_json_log_keeps_chinese_characters():
    """中文必须原样落盘。转成 \\uXXXX 后，Graylog 告警会再吃掉反斜杠。"""
    from csagent.infra.logging import configure_logging

    tmp = Path(tempfile.mkdtemp())
    configure_logging(
        "ERROR", "json", error_log_enabled=True, error_log_dir=str(tmp), project_root=tmp
    )
    structlog.get_logger("test_zh").error(
        "dataset_publish_failed",
        file="【利得基金】公募基金可转债投资能力与业绩归因数据库 - 2026Q3.xlsx",
    )

    content = (tmp / "error.log").read_text(encoding="utf-8")
    assert "【利得基金】公募基金可转债投资能力与业绩归因数据库" in content
    assert "\\u5229" not in content


def test_full_mirror_app_log_stays_gone():
    """回归：只留 ERROR 一份，不要把全量镜像 app.log 加回来。"""
    from csagent.infra.logging import configure_logging

    tmp = Path(tempfile.mkdtemp())
    configure_logging(
        "DEBUG", "console", error_log_enabled=True, error_log_dir=str(tmp), project_root=tmp
    )
    structlog.get_logger("test_no_mirror").info("hello")
    assert not (tmp / "app.log").exists()


def test_error_file_can_be_disabled():
    from csagent.infra.logging import configure_logging

    tmp = Path(tempfile.mkdtemp())
    configure_logging(
        "ERROR", "console", error_log_enabled=False, error_log_dir=str(tmp), project_root=tmp
    )
    structlog.get_logger("test_off").error("boom")
    assert not (tmp / "error.log").exists()


def test_settings_have_no_full_mirror_knobs(monkeypatch: pytest.MonkeyPatch):
    """全量镜像的旋钮已随 app.log 移除；只剩 ERROR_LOG_* 一组。"""
    s = _settings(monkeypatch, LOG_LEVEL="INFO")
    for gone in ("log_dir", "log_file_enabled", "log_max_bytes", "log_backup_count"):
        assert not hasattr(s, gone), f"{gone} 应随 app.log 一并移除"
    assert s.error_log_enabled is True
    assert s.error_log_dir == "data/logs"


def test_reconfigure_does_not_stack_handlers():
    from csagent.infra.logging import configure_logging

    tmp = Path(tempfile.mkdtemp())
    for _ in range(3):
        configure_logging(
            "INFO", "console", error_log_enabled=True, error_log_dir=str(tmp), project_root=tmp
        )
    # 重复配置不该越堆越多 handler（否则同一条日志会被写多份）
    assert len(logging.getLogger().handlers) == 2  # console + error file


def test_noisy_third_party_loggers_are_capped(monkeypatch: pytest.MonkeyPatch):
    from csagent.infra.logging import configure_logging

    configure_logging("INFO", "console")
    assert logging.getLogger("httpx").level == logging.WARNING
    configure_logging("DEBUG", "console")
    assert logging.getLogger("httpx").level == logging.DEBUG
