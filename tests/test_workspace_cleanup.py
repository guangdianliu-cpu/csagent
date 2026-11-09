"""会话临时工作区 TTL 清理。"""

from __future__ import annotations

import time
from pathlib import Path

from csagent.channels.workspace_cleanup import cleanup_workspace


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        if path.suffix or path.name.startswith("."):
            path.write_bytes(b"x")
        else:
            path.mkdir(parents=True, exist_ok=True)
            (path / ".keep").write_text("x", encoding="utf-8")
    # 目录：改自身；文件：改自身
    import os

    os.utime(path, (mtime, mtime))


def test_cleanup_removes_old_session_dirs(tmp_path: Path):
    uploads = tmp_path / "data" / "uploads"
    scripts = tmp_path / "scripts" / "tools"
    old = time.time() - 10 * 86400
    recent = time.time() - 1 * 86400

    old_upload = uploads / "sess-old" / "outbound"
    _touch(old_upload / "a.xlsx", old)
    _touch(old_upload, old)
    _touch(uploads / "sess-old", old)

    new_upload = uploads / "sess-new" / "outbound"
    _touch(new_upload / "b.xlsx", recent)
    _touch(new_upload, recent)
    _touch(uploads / "sess-new", recent)

    old_script = scripts / "sess-old"
    _touch(old_script / "make.py", old)
    _touch(old_script, old)

    loose = scripts / "误写.xlsx"
    _touch(loose, old)

    report = cleanup_workspace(
        project_root=tmp_path,
        uploads_root=uploads,
        scripts_dir=scripts,
        ttl_days=7,
        dry_run=False,
    )
    assert "data/uploads/sess-old" in report.removed_dirs
    assert "scripts/tools/sess-old" in report.removed_dirs
    assert "scripts/tools/误写.xlsx" in report.removed_files
    assert not (uploads / "sess-old").exists()
    assert (uploads / "sess-new").exists()
    assert not loose.exists()


def test_cleanup_dry_run_and_disabled(tmp_path: Path):
    uploads = tmp_path / "data" / "uploads"
    scripts = tmp_path / "scripts" / "tools"
    old = time.time() - 30 * 86400
    target = uploads / "s1" / "inbound"
    _touch(target / "f.xlsx", old)
    _touch(target, old)
    _touch(uploads / "s1", old)

    dry = cleanup_workspace(
        project_root=tmp_path,
        uploads_root=uploads,
        scripts_dir=scripts,
        ttl_days=7,
        dry_run=True,
    )
    assert dry.removed_dirs
    assert (uploads / "s1").exists()

    none = cleanup_workspace(
        project_root=tmp_path,
        uploads_root=uploads,
        scripts_dir=scripts,
        ttl_days=0,
    )
    assert none.removed_count == 0
    assert (uploads / "s1").exists()
