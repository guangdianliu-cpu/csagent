"""本轮生效配置的指纹，写进会话审计的 ``config_digest``。

规则会改、提示词会改、Skill 会热重载。事后复盘「为什么那天它这么做」时，要先知道当时
跑的是哪一版：指纹 = 系统提示词（含 Skill 索引）+ 挂载的工具集 + 护栏 / 策略 / 规则 /
工具包 / 形态配置文件的内容摘要。同一指纹 ⇒ 同一套配置。
"""

from __future__ import annotations

import hashlib
from functools import lru_cache

from csagent.infra.config import CONFIGS_DIR, active_profile

# 影响行为的配置文件（相对 configs/）；形态目录下的两份按当前形态取
_STATIC_FILES = (
    "agent.yaml",
    "tool_packs.yaml",
    "guardrail.yaml",
    "policy.yaml",
    "rules.md",
    "domain_text.yaml",
)


@lru_cache(maxsize=1)
def static_config_digest() -> str:
    """配置文件内容的摘要（进程内算一次；改配置要重启生效，与加载口径一致）。"""
    digest = hashlib.sha256()
    profile = active_profile()
    names = [*_STATIC_FILES, f"profiles/{profile}/profile.yaml", f"profiles/{profile}/agent.md"]
    for name in names:
        path = CONFIGS_DIR / name
        digest.update(name.encode("utf-8") + b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def turn_config_digest(system_prompt: str, tool_names: list[str]) -> str:
    """本轮指纹（16 位十六进制，够区分版本、又便于在审计里肉眼比对）。"""
    digest = hashlib.sha256()
    digest.update(static_config_digest().encode("ascii"))
    digest.update((system_prompt or "").encode("utf-8"))
    digest.update(",".join(sorted(tool_names)).encode("utf-8"))
    return digest.hexdigest()[:16]
