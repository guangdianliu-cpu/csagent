"""源码纯净度守卫：``src/`` 里不得出现行业词汇。

架构约定（见 csagent/prompts/domain_text.py 的模块 docstring）：
通用源码只引用领域层暴露的名字，所有「换一个行业就要改」的内容——文案、提示词、
行业关键词、实体正则、字段名——只能出现在 ``configs/domain_text*.yaml``、
``configs/prompts/`` 与 ``skills/`` 里。

这条约定过去只写在注释里，于是记忆/压缩层逐步内联了基金代码正则与「6 位基金代码」
提示词。本测试把它变成可执行的断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "csagent"

# 行业词：出现在通用源码里即视为泄漏。
FORBIDDEN_TERMS = (
    "基金",
    "投研",
    "利得",
    "固收",
    "纯债",
    "债基",
    "净值",
    "费率",
    "持仓",
    "客服",
)

# 领域层加载器本身允许出现在路径中；它不含领域内容，只是引用点。
_ALLOWED_FILES = frozenset()


def _source_files() -> list[Path]:
    return [
        p
        for p in SRC_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
        and p.relative_to(SRC_ROOT).as_posix() not in _ALLOWED_FILES
    ]


def test_source_files_exist():
    assert _source_files(), "未扫描到源码文件，路径可能不对"


@pytest.mark.parametrize("term", FORBIDDEN_TERMS)
def test_no_domain_vocabulary_in_source(term: str):
    hits: list[str] = []
    for path in _source_files():
        text = path.read_text(encoding="utf-8")
        if term not in text:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if term in line:
                rel = path.relative_to(SRC_ROOT).as_posix()
                hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert not hits, (
        f"通用源码出现行业词 {term!r}，应下沉到 configs/domain_text*.yaml、"
        f"configs/prompts/ 或 skills/：\n" + "\n".join(hits)
    )


def test_no_hardcoded_deployment_path_in_source():
    """部署路径（仓库目录名）不得写死在源码里——换机器/换目录即失效。"""
    project_root = SRC_ROOT.parent.parent
    needles = (project_root.name, project_root.parent.name)
    hits: list[str] = []
    for path in _source_files():
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), 1
        ):
            # 包名（cs-agent[...] / cs-agent/x.y）是合法引用，不算部署路径
            if "pip install" in line or "User-Agent" in line:
                continue
            for needle in needles:
                if needle and needle in line:
                    rel = path.relative_to(SRC_ROOT).as_posix()
                    hits.append(f"{rel}:{lineno}: {line.strip()}")
    assert not hits, "源码写死了部署路径，应从 PROJECT_ROOT 推导：\n" + "\n".join(hits)
