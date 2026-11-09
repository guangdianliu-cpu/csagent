"""加载提示词模板，支持 {{placeholder}} 替换。

查找顺序：当前形态目录 ``configs/profiles/<name>/<提示词名>.md`` >
``configs/prompts/<提示词名>.md``。系统提示词 ``agent`` 按形态放在各自目录里，
切换形态不再需要给文件改名。
"""

from __future__ import annotations

import re
from functools import lru_cache

from csagent.infra.config import CONFIGS_DIR, profile_dir

PROMPTS_DIR = CONFIGS_DIR / "prompts"

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@lru_cache(maxsize=64)
def _load_raw(name: str) -> str:
    """按名称加载 md 原文（不含扩展名）。"""
    candidates = []
    directory = profile_dir()
    if directory is not None:
        candidates.append(directory / f"{name}.md")
    candidates.append(PROMPTS_DIR / f"{name}.md")
    for path in candidates:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"提示词文件不存在，已查找: {', '.join(str(p) for p in candidates)}"
    )


def reload_prompts() -> None:
    """清空缓存（测试或热更新文案后调用）。"""
    _load_raw.cache_clear()


def render_prompt(name: str, **variables: str) -> str:
    """渲染提示词模板；未提供的占位符替换为空串。"""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        return str(variables.get(key, ""))

    text = _PLACEHOLDER_RE.sub(_replace, _load_raw(name))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"
