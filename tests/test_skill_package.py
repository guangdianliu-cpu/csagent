"""test_skill_package.py — SkillPackage 解析与校验测试。"""

from __future__ import annotations

import pytest
from pathlib import Path

from csagent.common.exceptions import SkillValidationError
from csagent.skills.package import parse_skill_md


# ---------- fixtures ----------

VALID_SKILL_MD = """\
---
name: example-skill
description: 生成与美化 Excel 报表。当用户要求导出表格、做数据汇总、套用报表样式时使用。
version: 1.0.0
tags: [excel, report, data]
allowed-tools: [read_file, write_file, excel_tool]
---

# Excel 报表生成

## 适用场景
- 导出带样式的 Excel
"""

MINIMAL_SKILL_MD = """\
---
name: code-review
description: 对代码变更做结构化评审。当用户要求 review、找 bug、检查规范时使用。
---

# Code Review

审查代码。
"""


def _make_root(tmp_path: Path, name: str) -> Path:
    root = tmp_path / name
    root.mkdir()
    return root


# ---------- 正常解析 ----------

def test_parse_valid_full(tmp_path):
    root = _make_root(tmp_path, "example-skill")
    pkg = parse_skill_md(VALID_SKILL_MD, root=root, source="builtin")
    assert pkg.name == "example-skill"
    assert "报表" in pkg.description
    assert pkg.version == "1.0.0"
    assert "excel" in pkg.tags
    assert pkg.allowed_tools == ["read_file", "write_file", "excel_tool"]
    assert pkg.enabled is True
    assert pkg.source == "builtin"
    assert "Excel 报表生成" in pkg.body


def test_parse_minimal(tmp_path):
    root = _make_root(tmp_path, "code-review")
    pkg = parse_skill_md(MINIMAL_SKILL_MD, root=root, source="user")
    assert pkg.name == "code-review"
    assert pkg.version == "1.0.0"
    assert pkg.tags == []
    assert pkg.allowed_tools is None
    assert pkg.source == "user"


# ---------- 校验拒绝 ----------

def test_reject_missing_frontmatter(tmp_path):
    root = _make_root(tmp_path, "bad")
    with pytest.raises(SkillValidationError, match="frontmatter"):
        parse_skill_md("# No frontmatter here\nSome body.", root=root, source="builtin")


def test_reject_missing_name(tmp_path):
    root = _make_root(tmp_path, "bad")
    text = "---\ndescription: Some description that is long enough\n---\nbody"
    with pytest.raises(SkillValidationError, match="name"):
        parse_skill_md(text, root=root, source="builtin")


def test_reject_missing_description(tmp_path):
    root = _make_root(tmp_path, "myskill")
    text = "---\nname: myskill\n---\nbody"
    with pytest.raises(SkillValidationError, match="description"):
        parse_skill_md(text, root=root, source="builtin")


def test_reject_short_description(tmp_path):
    root = _make_root(tmp_path, "myskill")
    text = "---\nname: myskill\ndescription: too short\n---\nbody"
    with pytest.raises(SkillValidationError, match="过短"):
        parse_skill_md(text, root=root, source="builtin")


def test_reject_name_dir_mismatch(tmp_path):
    root = _make_root(tmp_path, "actual-dir-name")
    text = "---\nname: different-name\ndescription: 这个描述足够长用于通过校验测试。\n---\nbody"
    with pytest.raises(SkillValidationError, match="不一致"):
        parse_skill_md(text, root=root, source="builtin")


# ---------- 资源收集 ----------

def test_collect_resources(tmp_path):
    root = _make_root(tmp_path, "with-resources")
    (root / "scripts").mkdir()
    (root / "scripts" / "run.py").write_text("pass")
    (root / "references").mkdir()
    (root / "references" / "guide.md").write_text("# Guide")
    (root / "assets").mkdir()
    (root / "assets" / "template.xlsx").write_bytes(b"fake")

    text = "---\nname: with-resources\ndescription: 一个带有脚本和参考资料的 Skill 用于测试资源收集。\n---\nbody"
    pkg = parse_skill_md(text, root=root, source="builtin")

    assert "scripts/run.py" in pkg.resources
    assert "references/guide.md" in pkg.resources
    assert "assets/template.xlsx" in pkg.resources
    assert len(pkg.resources) == 3


# ---------- enabled 字段 ----------

def test_enabled_false(tmp_path):
    root = _make_root(tmp_path, "disabled")
    text = "---\nname: disabled\ndescription: 临时下线的 Skill 用于测试 enabled 字段。\nenabled: false\n---\nbody"
    pkg = parse_skill_md(text, root=root, source="builtin")
    assert pkg.enabled is False
