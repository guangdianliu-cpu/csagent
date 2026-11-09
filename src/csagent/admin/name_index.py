"""关键词联想用的名称索引：从查询库读出可被认出的实体名称。

docs/对话页升级方案.md §2.4。读哪个库、哪些表哪些列、谁优先，全在形态目录的
``name_index.yaml`` 里；这里只做通用的「读名称 → 在输入里认出来」。只读 SQLite，
不经过 Skill 的查询链：要的只是几列名称，一次 ``SELECT DISTINCT`` 就够。

- 没有配置 / 库文件不存在（本地没构建、新部署还没发布数据）→ 索引为空，联想不出现，不报错；
- 管理后台发布新数据集会原子替换库文件 → 按文件修改时间判断，变了下次请求自动重建。
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path

import structlog
import yaml

from csagent.infra.config import PROJECT_ROOT, profile_dir

logger = structlog.get_logger(__name__)

CONFIG_FILE = "name_index.yaml"
_MIN_NAME_LEN = 2


@dataclass(frozen=True)
class NameMatch:
    entity: str  # 与快捷入口分类的 entity 对应
    name: str


@dataclass(frozen=True)
class NameSources:
    """``name_index.yaml`` 的内容。"""

    db_path: Path
    # 实体类型 → [(表, 名称列)]
    sources: dict[str, list[tuple[str, str]]]
    # 同样长度时的优先级，越靠前越优先
    priority: list[str] = field(default_factory=list)
    # 代码直认：在 code_entity 的各张表里按 code_column 找名称
    code_entity: str = ""
    code_column: str = ""
    code_digits: int = 6

    def rank(self, entity: str) -> int:
        return self.priority.index(entity) if entity in self.priority else len(self.priority)

    @classmethod
    def from_dict(cls, raw: dict) -> NameSources:
        db = Path(str(raw["db"]))
        sources = {
            str(entity): [(str(x["table"]), str(x["column"])) for x in items or []]
            for entity, items in (raw.get("sources") or {}).items()
        }
        codes = raw.get("codes") or {}
        return cls(
            db_path=db if db.is_absolute() else PROJECT_ROOT / db,
            sources=sources,
            priority=[str(x) for x in raw.get("priority") or []],
            code_entity=str(codes.get("entity") or ""),
            code_column=str(codes.get("column") or ""),
            code_digits=int(codes.get("digits") or 6),
        )


def load_sources(profile: str | None = None) -> NameSources | None:
    """读形态目录下的 ``name_index.yaml``；没有或写坏了返回 None（联想关闭）。"""
    d = profile_dir(profile)
    path = d / CONFIG_FILE if d is not None else None
    if path is None or not path.is_file():
        return None
    try:
        return NameSources.from_dict(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    except (OSError, yaml.YAMLError, KeyError, TypeError, ValueError) as exc:
        logger.warning("name_index_config_invalid", path=str(path), error=str(exc))
        return None


class NameIndex:
    """进程内单例；``match`` 前按库文件 mtime 判断要不要重建。"""

    def __init__(self, config: NameSources | None) -> None:
        self._config = config
        self._code_re = (
            re.compile(rf"(?<!\d)(\d{{{config.code_digits}}})(?!\d)") if config and config.code_entity else None
        )
        self._lock = threading.Lock()
        self._mtime: float | None = None
        # [(名称, 类型)]，按名称长度倒序：包含匹配时先碰到最长的
        self._names: list[tuple[str, str]] = []
        self._codes: dict[str, str] = {}

    def _current_mtime(self) -> float | None:
        if self._config is None:
            return None
        try:
            return self._config.db_path.stat().st_mtime
        except OSError:
            return None

    def _ensure_fresh(self) -> None:
        mtime = self._current_mtime()
        if mtime == self._mtime:
            return
        with self._lock:
            if mtime == self._mtime:
                return
            names, codes = self._load() if mtime is not None else ([], {})
            self._names, self._codes, self._mtime = names, codes, mtime
            logger.info("name_index_loaded", names=len(names), codes=len(codes))

    def _load(self) -> tuple[list[tuple[str, str]], dict[str, str]]:
        cfg = self._config
        assert cfg is not None
        seen: dict[str, str] = {}
        codes: dict[str, str] = {}
        try:
            conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            logger.warning("name_index_open_failed", error=str(exc))
            return [], {}
        try:
            columns = _columns_by_table(conn)
            for entity, sources in cfg.sources.items():
                for table, col in sources:
                    if col not in columns.get(table, ()):
                        continue
                    for (value,) in conn.execute(
                        f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL'
                    ):
                        name = str(value).strip()
                        if len(name) >= _MIN_NAME_LEN and name not in seen:
                            seen[name] = entity
            code_col = cfg.code_column
            for table, name_col in cfg.sources.get(cfg.code_entity, []) if self._code_re else []:
                cols = columns.get(table, ())
                if code_col not in cols or name_col not in cols:
                    continue
                for code, name in conn.execute(
                    f'SELECT "{code_col}", "{name_col}" FROM "{table}" WHERE "{code_col}" IS NOT NULL'
                ):
                    m = self._code_re.search(str(code))
                    if m and name:
                        codes.setdefault(m.group(1), str(name).strip())
        except sqlite3.Error as exc:
            logger.warning("name_index_load_failed", error=str(exc))
        finally:
            conn.close()
        names = sorted(seen.items(), key=lambda kv: (-len(kv[0]), cfg.rank(kv[1])))
        return names, codes

    def match(self, text: str, entities: set[str] | None = None) -> NameMatch | None:
        """在输入里认出一个名称。

        1. 输入里含配置的代码（如 6 位数字）且库里有 → 对应名称；
        2. 输入恰好是某个完整名称 → 就是它；
        3. 输入是某个名称的**开头**（还没打完）→ 取最短的那个；
        4. 输入**包含**某个完整名称 → 取最长的那个（长名称里常含短名称，取长的更准）。
        """
        self._ensure_fresh()
        q = re.sub(r"\s+", "", text or "")
        if len(q) < _MIN_NAME_LEN or not self._names:
            return None
        cfg = self._config
        assert cfg is not None
        allowed = entities if entities is not None else set(cfg.sources)

        if self._code_re and cfg.code_entity in allowed:
            for code in self._code_re.findall(q):
                if code in self._codes:
                    return NameMatch(cfg.code_entity, self._codes[code])

        contained = next(
            (NameMatch(entity, name) for name, entity in self._names if entity in allowed and name in q),
            None,
        )
        # 输入恰好是个完整名称就认它；输入比它长且是另一个名称的开头，说明用户在打更长的那个
        if contained is not None and contained.name == q:
            return contained
        prefix = [(name, entity) for name, entity in self._names if entity in allowed and name.startswith(q)]
        if prefix:
            name, entity = min(prefix, key=lambda kv: (len(kv[0]), cfg.rank(kv[1])))
            return NameMatch(entity, name)
        return contained


def _columns_by_table(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
    out: dict[str, set[str]] = {}
    for table in tables:
        out[table] = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
    return out


_index: NameIndex | None = None


def get_index() -> NameIndex:
    global _index
    if _index is None:
        _index = NameIndex(load_sources())
    return _index
