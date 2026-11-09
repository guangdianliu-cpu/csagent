"""唯一 Agent 配置读取（configs/agent.yaml + profile overlay）。"""

from __future__ import annotations

from typing import Any

import yaml

from csagent.infra.config import CONFIGS_DIR, PROFILES_DIR, active_profile, profile_dir


def _load_profile_overlay() -> dict[str, Any]:
    """加载当前形态的 profile.yaml；无形态或文件缺失时返回空 dict。

    新布局是 ``configs/profiles/<name>/profile.yaml``；旧的平铺文件
    ``configs/profiles/<name>.yaml`` 仍兼容（本地私有形态可能还是旧布局）。
    """
    profile = active_profile()
    if not profile:
        return {}
    directory = profile_dir(profile)
    candidates = []
    if directory is not None:
        candidates.append(directory / "profile.yaml")
    candidates.append(PROFILES_DIR / f"{profile}.yaml")
    for path in candidates:
        if path.is_file():
            with path.open(encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
    return {}


def load_skill_allowlist() -> frozenset[str] | None:
    """当前形态可见的 Skill 名单；形态没写 ``skills`` 键时返回 None（沿用 SKILL.md 的 enabled）。"""
    overlay = _load_profile_overlay()
    if "skills" not in overlay:
        return None
    return frozenset(str(s) for s in (overlay.get("skills") or []))


def load_resident_skills() -> tuple[str, ...]:
    """当前形态的常驻 Skill（``resident_skills``）：说明直接放进系统提示词，不用每问先 load_skill。"""
    overlay = _load_profile_overlay()
    return tuple(str(s) for s in (overlay.get("resident_skills") or []))


def load_tool_pack_names() -> tuple[str, ...]:
    """读取 tool_packs 列表。

    优先级：profile overlay 的 agent.tool_packs > configs/agent.yaml。
    """
    overlay = _load_profile_overlay()
    overlay_packs = (overlay.get("agent") or {}).get("tool_packs")
    if overlay_packs:
        return tuple(str(p) for p in overlay_packs)

    path = CONFIGS_DIR / "agent.yaml"
    if not path.exists():
        return ()
    with path.open(encoding="utf-8") as handle:
        raw: dict[str, Any] = yaml.safe_load(handle) or {}
    return tuple(str(p) for p in (raw.get("tool_packs") or []))


def load_tool_pack_overrides() -> dict[str, dict[str, Any]]:
    """从 profile overlay 读取 tool_pack_overrides；无 overlay 时返回空 dict。

    返回格式：{pack_name: {config: {...}}}
    pack_loader 用它把差异配置深合并进默认的 tool_packs.yaml 条目。
    """
    overlay = _load_profile_overlay()
    return dict(overlay.get("tool_pack_overrides") or {})


def load_domain_text_overrides() -> dict[str, Any]:
    """从 profile overlay 读取 domain_text 覆盖；无 overlay 时返回空 dict。"""
    overlay = _load_profile_overlay()
    return dict(overlay.get("domain_text") or {})
