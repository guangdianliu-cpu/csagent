"""test_skill_discovery.py — SkillDiscovery 多根扫描、冲突、reload、索引渲染测试。"""

from __future__ import annotations

from pathlib import Path

from csagent.skills.discovery import SkillDiscovery


def _write_skill(root: Path, name: str, description: str, *, enabled: bool = True) -> Path:
    """在 root 下创建一个合法 Skill 包目录。"""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    enabled_line = f"enabled: {'true' if enabled else 'false'}"
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n{enabled_line}\n---\n\n# {name}\n\nBody.\n",
        encoding="utf-8",
    )
    return skill_dir


# ---------- 基本扫描 ----------

def test_scan_single_root(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "my-skill", "一个用于测试扫描的示例 Skill 描述文本。")

    discovery = SkillDiscovery(roots=[root])
    packages = discovery.scan()

    assert len(packages) == 1
    assert packages[0].name == "my-skill"


def test_scan_multiple_skills(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "skill-a", "第一个测试 Skill 的描述文本足够长。")
    _write_skill(root, "skill-b", "第二个测试 Skill 的描述文本足够长。")

    discovery = SkillDiscovery(roots=[root])
    packages = discovery.scan()

    assert len(packages) == 2
    names = {p.name for p in packages}
    assert names == {"skill-a", "skill-b"}


# ---------- 非法 Skill 不影响其他加载 ----------

def test_invalid_skill_skipped(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "good-skill", "一个合法的 Skill 描述文本足够长通过校验。")

    # 创建一个缺少 description 的非法 Skill
    bad_dir = root / "bad-skill"
    bad_dir.mkdir()
    (bad_dir / "SKILL.md").write_text(
        "---\nname: bad-skill\n---\n\nNo description.",
        encoding="utf-8",
    )

    discovery = SkillDiscovery(roots=[root])
    packages = discovery.scan()

    assert len(packages) == 1
    assert packages[0].name == "good-skill"


# ---------- 同名冲突：高优先级根胜出 ----------

def test_name_conflict_higher_priority_wins(tmp_path):
    low_root = tmp_path / "low"
    high_root = tmp_path / "high"
    low_root.mkdir()
    high_root.mkdir()

    _write_skill(low_root, "dup-skill", "来自低优先级根的 Skill 描述文本。")
    _write_skill(high_root, "dup-skill", "来自高优先级根的 Skill 描述文本。")

    discovery = SkillDiscovery(roots=[low_root, high_root])
    packages = discovery.scan()

    assert len(packages) == 1
    assert packages[0].description == "来自高优先级根的 Skill 描述文本。"
    assert str(high_root) in str(packages[0].root)


# ---------- get ----------

def test_get_by_name(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "target-skill", "通过 get 方法按名称获取的 Skill。")

    discovery = SkillDiscovery(roots=[root])
    pkg = discovery.get("target-skill")
    assert pkg is not None
    assert pkg.name == "target-skill"

    assert discovery.get("nonexistent") is None


# ---------- reload ----------

def test_reload_picks_up_new_skill(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "first-skill", "第一次扫描时已存在的 Skill 描述。")

    discovery = SkillDiscovery(roots=[root])
    assert len(discovery.scan()) == 1

    # 新增一个 Skill
    _write_skill(root, "second-skill", "第二次扫描时新增的 Skill 描述文本。")
    packages = discovery.reload()

    assert len(packages) == 2
    names = {p.name for p in packages}
    assert "second-skill" in names


# ---------- render_index ----------

def test_render_index_format(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "alpha-skill", "Alpha 的描述信息用于测试索引渲染格式。")
    _write_skill(root, "beta-skill", "Beta 的描述信息用于测试索引渲染格式。")

    discovery = SkillDiscovery(roots=[root])
    index = discovery.render_index()

    assert "## 可用 Skill" in index
    assert "load_skill" in index
    assert "- alpha-skill:" in index
    assert "- beta-skill:" in index


def test_render_index_truncation(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    # 创建很多 Skill
    for i in range(20):
        _write_skill(root, f"skill-{i:03d}", f"这是第 {i} 个 Skill 的描述，用于测试索引截断功能。")

    discovery = SkillDiscovery(roots=[root])
    index = discovery.render_index(max_chars=300)

    # 应该被截断
    assert "仅显示部分" in index
    assert len(index) <= 500  # 加上截断提示的合理范围


def test_render_index_excludes_disabled(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "active-skill", "一个启用的 Skill 的描述文本足够长。")
    _write_skill(root, "hidden-skill", "一个禁用的 Skill 的描述文本足够长。", enabled=False)

    discovery = SkillDiscovery(roots=[root])
    index = discovery.render_index()

    assert "active-skill" in index
    assert "hidden-skill" not in index


# ---------- 空根目录 ----------

def test_scan_nonexistent_root(tmp_path):
    discovery = SkillDiscovery(roots=[tmp_path / "does-not-exist"])
    packages = discovery.scan()
    assert packages == []


# ---------- 缓存行为 ----------

def test_scan_uses_cache(tmp_path):
    root = tmp_path / "skills"
    root.mkdir()
    _write_skill(root, "cached-skill", "用于测试缓存行为的 Skill 描述文本。")

    discovery = SkillDiscovery(roots=[root])
    first = discovery.scan()
    second = discovery.scan()

    # 应返回相同内容（缓存）
    assert len(first) == len(second)
    assert first[0].name == second[0].name
