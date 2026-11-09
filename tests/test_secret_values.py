"""已知密钥值的收集与遮挡：输出护栏、工具结果过滤、外发文件扫描共用的那一份。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from csagent.common import secrets as secrets_mod
from csagent.common.secrets import SecretMasker, collect_secret_values
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool
from csagent.tools.registry import ToolRegistry

SECRET = "sk-live-0123456789abcdef"


def test_collect_secret_values_reads_env_files_and_environ(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env.demo"
    env_file.write_text(
        "LLM_API_KEY=sk-from-env-file-123456\n"
        "APP_PORT=8000\n"
        "SHORT_TOKEN=abc\n"
        "ADMIN_ACCOUNTS=alice:pbkdf2_sha256$200000$c2FsdA==$ZGlnZXN0ZGlnZXN0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("csagent.infra.config.env_files", lambda: (env_file,))
    monkeypatch.setenv("PRODUCT_PC_BRIDGE_TOKEN", "bridge-token-from-process")
    monkeypatch.setenv("FUND_DATA_DIR", "/a/very/long/but/not/secret/path")

    values = collect_secret_values()
    assert "sk-from-env-file-123456" in values
    assert "bridge-token-from-process" in values
    # ADMIN_ACCOUNTS 只收口令哈希，用户名不是秘密
    assert "pbkdf2_sha256$200000$c2FsdA==$ZGlnZXN0ZGlnZXN0" in values
    assert not any(v.startswith("alice") for v in values)
    # 名字不像密钥、或值太短的都不收
    assert "/a/very/long/but/not/secret/path" not in values
    assert "abc" not in values and "8000" not in values
    assert values == sorted(values, key=len, reverse=True)


def test_masker_masks_all_occurrences_longest_first() -> None:
    masker = SecretMasker(["abcdefghijklmnop", "abcdefghijklmnopqrstuvwxyz"])
    out = masker.mask("a=abcdefghijklmnopqrstuvwxyz b=abcdefghijklmnop")
    assert out == "a=[已屏蔽] b=[已屏蔽]"
    assert SecretMasker([]).mask("anything") == "anything"


def test_stream_holdback_only_for_secrets_with_separators() -> None:
    assert SecretMasker([SECRET]).stream_holdback == 0
    assert SecretMasker(["pass phrase with spaces"]).stream_holdback == len("pass phrase with spaces") - 1


def test_file_scan_finds_secret_in_plain_and_zip_files(tmp_path: Path) -> None:
    masker = SecretMasker([SECRET])
    plain = tmp_path / "dump.csv"
    plain.write_text(f"name,value\nkey,{SECRET}\n", encoding="utf-8")
    clean = tmp_path / "report.csv"
    clean.write_text("name,value\nfund,1.23\n", encoding="utf-8")
    # xlsx / docx 是 zip 容器，正文在压缩流里，不解压看不到
    xlsx = tmp_path / "leak.xlsx"
    with zipfile.ZipFile(xlsx, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/sharedStrings.xml", f"<sst><si><t>{SECRET}</t></si></sst>")
    assert masker.file_contains_secret(plain)
    assert masker.file_contains_secret(xlsx)
    assert not masker.file_contains_secret(clean)


class _EchoSecret(BaseTool):
    name = "echo"
    description = "echo"

    async def execute(self, arguments, scope):
        return f"读到配置：LLM_API_KEY={SECRET}"


class _Boom(BaseTool):
    name = "boom"
    description = "boom"

    async def execute(self, arguments, scope):
        raise RuntimeError(f"connect https://user:{SECRET}@host failed")


@pytest.mark.asyncio
async def test_registry_masks_secrets_in_results_and_error_texts() -> None:
    masker = SecretMasker([SECRET])
    registry = ToolRegistry.from_tools_and_agent(
        [_EchoSecret(), _Boom()], "agent", result_filter=masker.mask
    )
    scope = CustomerScope(session_id="s")
    ok = await registry.execute("echo", {}, scope, "agent")
    failed = await registry.execute("boom", {}, scope, "agent")
    assert SECRET not in ok and "[已屏蔽]" in ok
    assert SECRET not in failed and "执行失败" in failed


def test_get_secret_masker_is_cached_and_resettable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(secrets_mod, "_masker", None)
    monkeypatch.setattr(secrets_mod, "collect_secret_values", lambda: [SECRET])
    first = secrets_mod.get_secret_masker()
    assert first.count == 1 and secrets_mod.get_secret_masker() is first
    secrets_mod.reset_secret_masker(SecretMasker([]))
    assert secrets_mod.get_secret_masker().count == 0
