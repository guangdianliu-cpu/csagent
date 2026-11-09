"""产物登记（M1c）测试。

登记要回答的是一个月后的「上次那个报表还在吗」。两条硬约束：
1. 登记失败绝不能连累文件本身的上传/发送——它只是一份清单。
2. 清单和磁盘会分叉（文件可能被清理掉），「还在吗」必须真的去看一眼。
"""

from __future__ import annotations

import pytest

from csagent.memory.artifact_store import (
    ArtifactStore,
    register_artifact,
    set_artifact_store,
)


@pytest.fixture
def store(tmp_path):
    """建一个临时 store 并注入模块级单例，用完还原，避免污染其他用例。"""
    s = ArtifactStore(tmp_path / "csagent.db")
    set_artifact_store(s)
    yield s
    set_artifact_store(None)


def _write(tmp_path, name: str, body: bytes = b"x" * 10):
    path = tmp_path / name
    path.write_bytes(body)
    return path


# ---------------------------------------------------------------- 登记与检索


def test_register_and_find_by_session(store, tmp_path):
    path = _write(tmp_path, "报表.xlsx")
    store.register(
        session_id="web:u1", kind="outbound", path=str(path), filename="报表.xlsx"
    )

    found = store.find(session_id="web:u1")

    assert len(found) == 1
    assert found[0].filename == "报表.xlsx"
    assert found[0].kind == "outbound"
    assert found[0].size == 10


def test_find_by_partial_filename(store, tmp_path):
    """用户只记得半个名字的场景。"""
    for name in ("二季度优选报表.xlsx", "无关文件.txt"):
        store.register(
            session_id="web:u1", kind="outbound",
            path=str(_write(tmp_path, name)), filename=name,
        )

    found = store.find(filename_like="报表")

    assert [r.filename for r in found] == ["二季度优选报表.xlsx"]


def test_find_filters_by_kind_and_session(store, tmp_path):
    store.register(
        session_id="web:u1", kind="inbound",
        path=str(_write(tmp_path, "a.txt")), filename="a.txt",
    )
    store.register(
        session_id="web:u2", kind="outbound",
        path=str(_write(tmp_path, "b.txt")), filename="b.txt",
    )

    assert len(store.find(session_id="web:u1")) == 1
    assert len(store.find(kind="outbound")) == 1
    assert len(store.find(session_id="web:u1", kind="outbound")) == 0


def test_find_returns_newest_first(store, tmp_path):
    for name in ("first.txt", "second.txt"):
        store.register(
            session_id="web:u1", kind="outbound",
            path=str(_write(tmp_path, name)), filename=name,
        )

    assert [r.filename for r in store.find(session_id="web:u1")][0] == "second.txt"


def test_get_by_id_returns_the_same_record(store, tmp_path):
    path = _write(tmp_path, "a.txt")
    artifact_id = store.register(
        session_id="web:u1", kind="outbound", path=str(path), filename="a.txt",
        artifact_id="ab" * 16,
    )

    got = store.get(artifact_id)

    assert got is not None
    assert got.id == "ab" * 16
    assert store.get("no-such-id") is None


def test_hash_is_computed_for_small_files(store, tmp_path):
    path = _write(tmp_path, "a.txt", b"hello")
    store.register(
        session_id="web:u1", kind="outbound", path=str(path), filename="a.txt"
    )

    assert len(store.find(session_id="web:u1")[0].sha256) == 64


# ---------------------------------------------------------------- 清单 vs 磁盘


def test_exists_reflects_disk_not_the_ledger(store, tmp_path):
    """登记永久保留，文件却可能被清理——「还在吗」不能只信清单。"""
    path = _write(tmp_path, "会被删掉.xlsx")
    store.register(
        session_id="web:u1", kind="outbound", path=str(path), filename="会被删掉.xlsx"
    )
    assert store.find(session_id="web:u1")[0].exists is True

    path.unlink()

    record = store.find(session_id="web:u1")[0]
    assert record.filename == "会被删掉.xlsx", "记录本身必须还在"
    assert record.exists is False


# ---------------------------------------------------------------- 尽力而为


def test_register_never_raises_on_bad_input(store):
    """路径不存在时照常登记（只是算不出 size/hash），不抛异常。"""
    artifact_id = store.register(
        session_id="web:u1", kind="outbound",
        path="不存在的目录/幽灵.xlsx", filename="幽灵.xlsx",
    )

    assert artifact_id
    assert store.find(session_id="web:u1")[0].sha256 == ""


def test_register_skips_silently_without_session():
    """缺少会话标识时静默跳过，不写脏数据。"""
    set_artifact_store(None)
    assert register_artifact(session_id="", kind="outbound", path="a", filename="a") == ""


def test_module_level_helper_is_a_noop_when_unconfigured():
    """非 sqlite 后端 / 测试环境下未注入 store，登记点不该因此报错。"""
    set_artifact_store(None)
    assert register_artifact(
        session_id="web:u1", kind="outbound", path="a.txt", filename="a.txt"
    ) == ""


def test_module_level_helper_writes_through_to_store(store, tmp_path):
    register_artifact(
        session_id="web:u1", kind="inbound",
        path=str(_write(tmp_path, "up.xlsx")), filename="up.xlsx",
    )

    assert [r.filename for r in store.find(session_id="web:u1")] == ["up.xlsx"]
