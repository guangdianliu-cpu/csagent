"""管理后台：登录鉴权边界 + 数据集上传发布。

鉴权要钉死的边界：
1. 管理令牌（typ=adm）进不了聊天接口，宿主身份令牌也进不了管理接口；
2. 账号从 ADMIN_ACCOUNTS 删掉后，未过期的管理令牌立即失效；
3. 连续输错口令会被锁定。
发布流程用一个假 builder 脚本验证：通用层只负责暂存、调子进程、报告结果。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from csagent.admin import datasets as ds_mod
from csagent.admin.datasets import DatasetPublisher, DatasetSpec
from csagent.admin.passwd import hash_password, parse_accounts, verify_password
from csagent.api import router_admin
from csagent.infra import auth

SECRET = "test-secret-that-is-long-enough-for-hmac-32"
# 管理令牌专用密钥：只在本服务，与给宿主的 SECRET 不同
ADMIN_SECRET = "admin-only-secret-never-shared-with-the-host"

_FAKE_BUILDER = r'''
import json, shutil, sys
from pathlib import Path
args = sys.argv[1:]
if "--describe" in args:
    print(json.dumps({"data_dir": "x", "db_exists": True, "datasets": []}))
    sys.exit(0)
src = Path(args[args.index("--publish") + 1])
name = args[args.index("--filename") + 1]
if not name.startswith("good"):
    print(json.dumps({"ok": False, "error": "文件名不属于任何数据集"}))
    sys.exit(2)
out = Path(__file__).with_name("published")
out.mkdir(exist_ok=True)
shutil.copyfile(src, out / name)
print(json.dumps({"ok": True, "file": name, "tables": [{"table": "t", "sheet": "s", "rows": 3}]}))
'''


def _settings(accounts: str) -> MagicMock:
    s = MagicMock()
    s.agent_auth_enabled = True
    s.agent_shared_secret = SECRET
    s.agent_token_ttl_seconds = 1800
    s.admin_accounts = accounts
    s.admin_token_ttl_seconds = 600
    s.admin_token_secret = ADMIN_SECRET
    return s


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    accounts = f"alice:{hash_password('correct-horse')}"
    settings = _settings(accounts)
    monkeypatch.setattr("csagent.infra.auth.get_settings", lambda: settings)
    monkeypatch.setattr("csagent.api.router_admin.get_settings", lambda: settings)
    monkeypatch.setattr(router_admin, "STAGING_DIR", tmp_path / "staging")
    router_admin._failures.clear()

    builder = tmp_path / "builder.py"
    builder.write_text(_FAKE_BUILDER, encoding="utf-8")
    spec = DatasetSpec(id="demo", title="演示", builder=builder, max_upload_bytes=1024)

    app = FastAPI()
    app.include_router(router_admin.router)
    app.state.dataset_publisher = DatasetPublisher({"demo": spec})
    # 用上下文管理器保持同一个事件循环：发布任务是后台 Task，跨请求存活
    with TestClient(app) as client:
        yield client, settings, tmp_path


def _login(client: TestClient, password: str = "correct-horse"):
    return client.post("/api/admin/login", json={"username": "alice", "password": password})


def _wait_job(client: TestClient, token: str, job_id: str) -> dict:
    for _ in range(100):
        job = client.get(
            f"/api/admin/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"}
        ).json()["job"]
        if job["status"] in ("succeeded", "failed"):
            return job
        time.sleep(0.05)
    raise AssertionError("job 未结束")


def test_password_hash_roundtrip():
    encoded = hash_password("s3cret-pass")
    assert verify_password("s3cret-pass", encoded)
    assert not verify_password("wrong", encoded)
    assert parse_accounts(f"a:{encoded}, bad ,b:") == {"a": encoded}


def test_login_and_me(env):
    client, _, _ = env
    resp = _login(client)
    assert resp.status_code == 200
    token = resp.json()["token"]
    me = client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["username"] == "alice"


def test_wrong_password_rejected_and_locked(env):
    client, _, _ = env
    for _ in range(router_admin._MAX_FAILURES):
        assert _login(client, "nope").status_code == 401
    # 锁定期内即使口令正确也拒绝
    assert _login(client).status_code == 429


def test_identity_token_cannot_access_admin(env):
    client, settings, _ = env
    host_token = auth.issue_token("alice", secret=SECRET)
    resp = client.get("/api/admin/me", headers={"Authorization": f"Bearer {host_token}"})
    assert resp.status_code == 401


def test_admin_token_cannot_pass_identity_check(env):
    client, _, _ = env
    token = _login(client).json()["token"]
    with pytest.raises(auth.AuthError):
        auth.verify_token(token, secret=SECRET)


def _forge_admin_token(uid: str, secret: str) -> str:
    """按令牌格式手工签一张 typ=adm 的令牌（issue_token 不允许用共享密钥签 adm）。"""
    now = int(time.time())
    payload = {"typ": "adm", "uid": uid, "iat": now, "exp": now + 600}
    body = auth._b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    signing_input = f"v1.{body}"
    return f"{signing_input}.{auth._sign(signing_input, secret)}"


def _security_events(audit_dir: Path) -> list[dict]:
    events: list[dict] = []
    for path in sorted(audit_dir.glob("security-*.jsonl")):
        events.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    return events


def test_admin_token_forged_with_shared_secret_is_rejected(env, _isolate_security_state):
    """宿主后端持有共享密钥：用它签出来的 adm 令牌必须进不了管理接口，且要留痕。"""
    client, _, _ = env
    forged = _forge_admin_token("alice", SECRET)
    resp = client.get("/api/admin/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401
    rejected = [e for e in _security_events(_isolate_security_state)
                if e["event"] == "admin_auth_rejected"]
    assert rejected and rejected[-1]["outcome"] == "denied"
    # 令牌本身不进审计
    assert forged not in json.dumps(rejected, ensure_ascii=False)


@pytest.mark.parametrize("admin_secret", ["", "too-short", SECRET])
def test_login_fails_closed_without_usable_admin_secret(env, admin_secret):
    """管理密钥缺失、过短、或与给宿主的共享密钥相同：口令对也不签令牌（503）。"""
    client, settings, _ = env
    settings.admin_token_secret = admin_secret
    resp = _login(client)
    assert resp.status_code == 503
    assert "ADMIN_TOKEN_SECRET" in resp.json()["detail"]


def test_admin_events_are_written_to_security_audit(env, _isolate_security_state):
    client, _, _ = env
    assert _login(client, "wrong-password").status_code == 401
    token = _login(client).json()["token"]
    client.post(
        "/api/admin/debug-token",
        json={"user_id": "u7", "ttl_seconds": 600},
        headers={"Authorization": f"Bearer {token}"},
    )
    events = _security_events(_isolate_security_state)
    logins = [(e["event"], e["outcome"], e["actor"]) for e in events if e["event"] == "admin_login"]
    assert ("admin_login", "failure", "alice") in logins
    assert ("admin_login", "success", "alice") in logins
    issued = [e for e in events if e["event"] == "admin_debug_token_issued"]
    assert issued and issued[-1]["actor"] == "alice"
    assert issued[-1]["target_uid"] == "debug:u7"
    assert all(token not in json.dumps(e, ensure_ascii=False) for e in events)


def test_removed_account_token_invalidated(env):
    client, settings, _ = env
    token = _login(client).json()["token"]
    settings.admin_accounts = ""
    resp = client.get("/api/admin/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_debug_token_requires_admin(env):
    client, _, _ = env
    assert client.post("/api/admin/debug-token", json={}).status_code == 401
    # 身份令牌不能自己给自己续签
    host_token = auth.issue_token("alice", secret=SECRET)
    resp = client.post(
        "/api/admin/debug-token", json={}, headers={"Authorization": f"Bearer {host_token}"}
    )
    assert resp.status_code == 401


def test_debug_token_is_short_lived_identity_token(env):
    client, _, _ = env
    headers = {"Authorization": f"Bearer {_login(client).json()['token']}"}
    resp = client.post(
        "/api/admin/debug-token", json={"user_id": "u42", "name": "调试"}, headers=headers
    )
    assert resp.status_code == 200
    principal = auth.verify_token(resp.json()["token"], secret=SECRET)
    # 调试令牌固定落在 debug: 命名空间，冒充不了真实用户 u42
    assert principal.user_id == "debug:u42"
    assert principal.session_id == "web:debug:u42"
    assert resp.json()["user_id"] == "debug:u42"
    assert resp.json()["expires_in"] <= router_admin._DEBUG_TOKEN_MAX_TTL
    # 超过上限直接拒，不静默截断
    too_long = client.post(
        "/api/admin/debug-token",
        json={"ttl_seconds": router_admin._DEBUG_TOKEN_MAX_TTL + 1},
        headers=headers,
    )
    assert too_long.status_code == 422


def test_upload_requires_login(env):
    client, _, _ = env
    resp = client.post(
        "/api/admin/datasets/demo/upload",
        files={"file": ("good.xlsx", b"data", "application/octet-stream")},
    )
    assert resp.status_code == 401


def test_upload_publishes_via_builder(env):
    client, _, tmp_path = env
    token = _login(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}
    resp = client.post(
        "/api/admin/datasets/demo/upload",
        headers=headers,
        files={"file": ("good-2026.xlsx", b"hello", "application/octet-stream")},
    )
    assert resp.status_code == 200, resp.text
    job = _wait_job(client, token, resp.json()["job"]["id"])
    assert job["status"] == "succeeded", job
    assert job["operator"] == "alice"
    assert (tmp_path / "published" / "good-2026.xlsx").read_bytes() == b"hello"
    # 暂存文件用完即删
    assert not any((tmp_path / "staging").iterdir())


def test_upload_builder_rejection_reported(env):
    client, _, _ = env
    token = _login(client).json()["token"]
    resp = client.post(
        "/api/admin/datasets/demo/upload",
        headers={"Authorization": f"Bearer {token}"},
        files={"file": ("bad.xlsx", b"hello", "application/octet-stream")},
    )
    job = _wait_job(client, token, resp.json()["job"]["id"])
    assert job["status"] == "failed"
    assert "不属于任何数据集" in job["error"]


def test_upload_rejects_wrong_extension_and_oversize(env):
    client, _, _ = env
    headers = {"Authorization": f"Bearer {_login(client).json()['token']}"}
    r1 = client.post(
        "/api/admin/datasets/demo/upload", headers=headers,
        files={"file": ("good.csv", b"x", "text/csv")},
    )
    assert r1.status_code == 400
    r2 = client.post(
        "/api/admin/datasets/demo/upload", headers=headers,
        files={"file": ("good.xlsx", b"x" * 2048, "application/octet-stream")},
    )
    assert r2.status_code == 413


def test_list_datasets(env):
    client, _, _ = env
    headers = {"Authorization": f"Bearer {_login(client).json()['token']}"}
    data = client.get("/api/admin/datasets", headers=headers).json()
    assert data["datasets"][0]["id"] == "demo"
    assert data["datasets"][0]["state"]["db_exists"] is True


def test_dataset_config_loads():
    specs = ds_mod.load_dataset_specs()
    assert "fund-research-db" in specs
    assert specs["fund-research-db"].builder.is_file()
    assert sys.executable


def test_batch_upload_publishes_each_file_independently(env):
    client, _, tmp_path = env
    token = _login(client).json()["token"]
    resp = client.post(
        "/api/admin/datasets/demo/upload",
        headers={"Authorization": f"Bearer {token}"},
        files=[
            ("files", ("good-a.xlsx", b"A", "application/octet-stream")),
            ("files", ("bad-b.xlsx", b"B", "application/octet-stream")),
            ("files", ("good-c.xlsx", b"C", "application/octet-stream")),
        ],
    )
    assert resp.status_code == 200, resp.text
    jobs = resp.json()["jobs"]
    assert [j["filename"] for j in jobs] == ["good-a.xlsx", "bad-b.xlsx", "good-c.xlsx"]
    states = [_wait_job(client, token, j["id"])["status"] for j in jobs]
    # 中间一个失败不影响前后两个
    assert states == ["succeeded", "failed", "succeeded"]
    assert (tmp_path / "published" / "good-a.xlsx").read_bytes() == b"A"
    assert (tmp_path / "published" / "good-c.xlsx").read_bytes() == b"C"


def test_batch_rejected_whole_when_one_file_invalid(env):
    client, _, tmp_path = env
    headers = {"Authorization": f"Bearer {_login(client).json()['token']}"}
    resp = client.post(
        "/api/admin/datasets/demo/upload",
        headers=headers,
        files=[
            ("files", ("good-a.xlsx", b"A", "application/octet-stream")),
            ("files", ("good-b.csv", b"B", "text/csv")),
        ],
    )
    assert resp.status_code == 400
    assert not (tmp_path / "published").exists()
    # 暂存区不留残片
    staging = tmp_path / "staging"
    assert not staging.exists() or not any(staging.iterdir())

    dup = client.post(
        "/api/admin/datasets/demo/upload",
        headers=headers,
        files=[
            ("files", ("good-a.xlsx", b"A", "application/octet-stream")),
            ("files", ("good-a.xlsx", b"A2", "application/octet-stream")),
        ],
    )
    assert dup.status_code == 400 and "重名" in dup.json()["detail"]

    oversize = client.post(
        "/api/admin/datasets/demo/upload",
        headers=headers,
        files=[
            ("files", ("good-a.xlsx", b"A", "application/octet-stream")),
            ("files", ("good-b.xlsx", b"x" * 2048, "application/octet-stream")),
        ],
    )
    assert oversize.status_code == 413
    assert not any((tmp_path / "staging").iterdir())
