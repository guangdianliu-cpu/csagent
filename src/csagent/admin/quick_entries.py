"""聊天页快捷入口：欢迎问题组、分类标签栏、「能查什么」与分类抽屉的内容。

两层来源，后者优先（docs/对话页升级方案.md §2.2）：

- 默认值：``configs/profiles/<形态>/quick_entries.yaml``，进仓库，改了要重启；
- 线上值：``data/quick_entries/<形态>.json``，管理后台保存即生效，不进仓库。

两处都没有（或都不合法）时快捷入口整块不显示，聊天照常——这里任何读错都只告警，
不能让一份写坏的配置把聊天页拖垮。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import structlog
import yaml
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from csagent.infra.config import PROJECT_ROOT, active_profile, profile_dir

logger = structlog.get_logger(__name__)

QUICK_ENTRIES_DIR = PROJECT_ROOT / "data" / "quick_entries"
DEFAULT_FILE = "quick_entries.yaml"
# 管理后台保存 / 回退 / 恢复默认前，把当时的线上值留档，只留最近这么多份
HISTORY_KEEP = 5
PLACEHOLDER = "XXX"

# 关键词联想按这个类型识别名称（§2.4）；空串 = 该分类不参与联想
EntityType = Literal["", "fund", "manager", "company", "private"]
_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")


def _question(text: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("问题不能为空")
    if len(text) > 60:
        raise ValueError(f"问题不能超过 60 字：{text[:20]}…")
    if text.count(PLACEHOLDER) > 1:
        raise ValueError(f"每个问题最多 1 个 {PLACEHOLDER} 占位：{text}")
    return text


class EntryTag(BaseModel):
    """能力胶囊：「能查什么」里一行，也是分类抽屉顶部的筛选项。"""

    label: str = Field(..., min_length=1, max_length=12)
    keywords: list[str] = Field(default_factory=list, max_length=8)
    questions: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("keywords")
    @classmethod
    def _keywords(cls, value: list[str]) -> list[str]:
        out = [k.strip() for k in value if k.strip()]
        if any(len(k) > 30 for k in out):
            raise ValueError("关键词不能超过 30 字")
        return out

    @field_validator("questions")
    @classmethod
    def _questions(cls, value: list[str]) -> list[str]:
        return [_question(q) for q in value]


class EntryCategory(BaseModel):
    """标签栏上的一个分类，如 configs/profiles/research/quick_entries.yaml 里的各项。"""

    key: str
    label: str = Field(..., min_length=1, max_length=8)
    icon: str = Field(default="", max_length=16)
    entity: EntityType = ""
    # 带 XXX 的问题在列表里显示成「[占位提示]」、点了填进输入框让用户改；空则显示「[名称]」
    fill_label: str = Field(default="", max_length=8)
    tags: list[EntryTag] = Field(..., min_length=1, max_length=8)

    @field_validator("key")
    @classmethod
    def _key(cls, value: str) -> str:
        if not _KEY_RE.match(value):
            raise ValueError(f"key 只能是小写字母开头的 a-z0-9_，最长 32：{value}")
        return value


class WelcomeGroup(BaseModel):
    """「您可以这样问我」里的一组，组名独立于分类。"""

    label: str = Field(..., min_length=1, max_length=12)
    questions: list[str] = Field(..., min_length=1, max_length=20)

    @field_validator("questions")
    @classmethod
    def _questions(cls, value: list[str]) -> list[str]:
        return [_question(q) for q in value]


class Welcome(BaseModel):
    title: str = Field(default="", max_length=20)
    subtitle: str = Field(default="", max_length=30)
    placeholder: str = Field(default="", max_length=30)
    groups: list[WelcomeGroup] = Field(default_factory=list, max_length=6)


class QuickEntries(BaseModel):
    welcome: Welcome = Field(default_factory=Welcome)
    capability_intro: str = Field(default="", max_length=60)
    categories: list[EntryCategory] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def _unique_keys(self) -> QuickEntries:
        keys = [c.key for c in self.categories]
        dup = {k for k in keys if keys.count(k) > 1}
        if dup:
            raise ValueError(f"分类 key 重复：{', '.join(sorted(dup))}")
        return self


@dataclass(frozen=True)
class LoadedEntries:
    """当前生效的快捷入口。``entries`` 为 None 表示不显示。"""

    source: Literal["online", "default", "none"]
    version: int
    entries: QuickEntries | None
    updated_by: str = ""
    updated_at: int = 0


def _profile() -> str:
    return active_profile() or "default"


def online_path(profile: str | None = None) -> Path:
    return QUICK_ENTRIES_DIR / f"{profile or _profile()}.json"


def default_path(profile: str | None = None) -> Path | None:
    d = profile_dir(profile)
    return d / DEFAULT_FILE if d is not None else None


def _load_online(path: Path) -> LoadedEntries | None:
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        entries = QuickEntries.model_validate(raw.get("content") or {})
    except (OSError, ValueError, ValidationError) as exc:
        # 线上值坏了退回仓库默认，不让聊天页整块消失
        logger.warning("quick_entries_online_invalid", path=str(path), error=str(exc))
        return None
    return LoadedEntries(
        source="online",
        version=int(raw.get("version") or 0),
        entries=entries,
        updated_by=str(raw.get("updated_by") or ""),
        updated_at=int(raw.get("updated_at") or 0),
    )


def _load_default(path: Path | None) -> LoadedEntries | None:
    if path is None or not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        entries = QuickEntries.model_validate(raw)
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        logger.warning("quick_entries_default_invalid", path=str(path), error=str(exc))
        return None
    # 默认值没有显式版本号：用文件修改时间，改了 YAML 重启后前端能识别出新版本
    return LoadedEntries(source="default", version=int(path.stat().st_mtime), entries=entries)


def load(profile: str | None = None) -> LoadedEntries:
    """当前生效的快捷入口：线上值 > 仓库默认 > 不显示。"""
    return (
        _load_online(online_path(profile))
        or _load_default(default_path(profile))
        or LoadedEntries(source="none", version=0, entries=None)
    )


def placeholder_questions(entries: QuickEntries, entity: str) -> list[str]:
    """该实体类型各分类里带 XXX 的问题，按配置顺序、去重。关键词联想用（§2.4）。"""
    out: list[str] = []
    for cat in entries.categories:
        if cat.entity != entity:
            continue
        for tag in cat.tags:
            for q in tag.questions:
                if PLACEHOLDER in q and q not in out:
                    out.append(q)
    return out


def suggest_entities(entries: QuickEntries) -> set[str]:
    """有可用模板问题的实体类型；不在这里的类型不必去认名称。"""
    return {c.entity for c in entries.categories if c.entity and placeholder_questions(entries, c.entity)}


# ---------------------------------------------------------------- 管理后台写入（§2.3）


class VersionConflict(Exception):
    """保存时线上版本已不是编辑时看到的那个（别人先保存了）。"""


def history_dir(profile: str | None = None) -> Path:
    return QUICK_ENTRIES_DIR / f"{profile or _profile()}.history"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def _archive_online(profile: str | None) -> None:
    """把当前线上值（如果有）挪进历史目录，超出 HISTORY_KEEP 的旧档删掉。"""
    current = online_path(profile)
    if not current.is_file():
        return
    try:
        raw = json.loads(current.read_text(encoding="utf-8"))
        version = int(raw.get("version") or 0)
    except (OSError, ValueError):
        # 坏文件不留档，直接被新值覆盖
        return
    hdir = history_dir(profile)
    hdir.mkdir(parents=True, exist_ok=True)
    (hdir / f"{version}.json").write_text(
        current.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for old in sorted(hdir.glob("*.json"), key=_history_version, reverse=True)[HISTORY_KEEP:]:
        old.unlink(missing_ok=True)


def _history_version(path: Path) -> int:
    try:
        return int(path.stem)
    except ValueError:
        return 0


def history(profile: str | None = None) -> list[dict]:
    """最近的历史版本（新的在前）：``[{version, updated_by, updated_at}]``。"""
    out = []
    hdir = history_dir(profile)
    for path in sorted(hdir.glob("*.json"), key=_history_version, reverse=True)[:HISTORY_KEEP]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        out.append(
            {
                "version": int(raw.get("version") or 0),
                "updated_by": str(raw.get("updated_by") or ""),
                "updated_at": int(raw.get("updated_at") or 0),
            }
        )
    return out


def _check_base(base_version: int | None, profile: str | None) -> LoadedEntries:
    current = load(profile)
    if base_version is not None and base_version != current.version:
        raise VersionConflict(
            f"快捷入口已被修改（当前版本 {current.version}，你编辑的是 {base_version}），请刷新后再改"
        )
    return current


def save(
    entries: QuickEntries,
    operator: str,
    *,
    base_version: int | None = None,
    profile: str | None = None,
) -> LoadedEntries:
    """整份保存为线上值，旧线上值留档。``base_version`` 不是当前版本时拒绝。"""
    current = _check_base(base_version, profile)
    _archive_online(profile)
    now = int(time.time())
    # 不小于当前版本 + 1：默认值的版本是 YAML 修改时间，可能比现在还「新」
    version = max(current.version + 1, now)
    payload = {
        "version": version,
        "updated_by": operator,
        "updated_at": now,
        "content": entries.model_dump(mode="json"),
    }
    _write_json(online_path(profile), payload)
    return load(profile)


def rollback(version: int, operator: str, *, profile: str | None = None) -> LoadedEntries:
    """回到某个历史版本：取出那份内容另存为新版本（历史本身不改）。"""
    path = history_dir(profile) / f"{int(version)}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"历史版本 {version} 不存在") from exc
    entries = QuickEntries.model_validate(raw.get("content") or {})
    return save(entries, operator, profile=profile)


def reset(*, profile: str | None = None) -> LoadedEntries:
    """删掉线上值，退回仓库默认（删之前留档，可再回退回来）。"""
    _archive_online(profile)
    online_path(profile).unlink(missing_ok=True)
    return load(profile)
