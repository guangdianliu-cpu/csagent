"""管理员口令哈希与校验（PBKDF2-SHA256，无第三方依赖）。

生成 ``ADMIN_ACCOUNTS`` 里的一项::

    python -m csagent.admin.passwd alice
    # 按提示输入口令，输出 alice:pbkdf2_sha256$...，追加到 .env 的 ADMIN_ACCOUNTS
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import hmac
import secrets
import sys

_ALGO = "pbkdf2_sha256"
_ITERATIONS = 200_000


def hash_password(password: str, *, iterations: int = _ITERATIONS) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "$".join((
        _ALGO,
        str(iterations),
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    ))


def verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iterations, salt_b64, digest_b64 = encoded.split("$")
        if algo != _ALGO:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iterations)
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def parse_accounts(raw: str) -> dict[str, str]:
    """``"alice:hash,bob:hash"`` → ``{"alice": "hash", ...}``；格式不对的项忽略。"""
    accounts: dict[str, str] = {}
    for item in (raw or "").split(","):
        name, sep, encoded = item.strip().partition(":")
        if sep and name.strip() and encoded.strip():
            accounts[name.strip()] = encoded.strip()
    return accounts


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("用法: python -m csagent.admin.passwd <用户名>", file=sys.stderr)
        return 2
    name = args[0].strip()
    if not name or ":" in name or "," in name:
        print("用户名不能为空，且不能含 ':' 或 ','", file=sys.stderr)
        return 2
    password = getpass.getpass("口令: ")
    if len(password) < 8:
        print("口令至少 8 位", file=sys.stderr)
        return 2
    if getpass.getpass("再输一次: ") != password:
        print("两次输入不一致", file=sys.stderr)
        return 2
    print(f"{name}:{hash_password(password)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
