"""所有产品形态的领域配置都必须键齐备。

事故（2026-08-16）：给「通道级兜底答复」加了 handle_timeout_reply /
transport_failure_reply / handle_failed_reply 三个键，但只改了当时转正的那份
domain_text.yaml（投研）和 domain_text_product.yaml，漏了法律那份。
`scripts/start.py 法律` 把法律版换成正式文件后，启动直接死在 import 链深处：

    File "src/csagent/prompts/domain_text.py", line 45, in <module>
        HANDLE_TIMEOUT_REPLY: str = _data["handle_timeout_reply"]
    KeyError: 'handle_timeout_reply'

只测「当前转正的那份」是测不出来的——待机的几份平时不参与运行，
必须在测试里显式遍历全部 domain_text*.yaml。
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from csagent.infra.config import CONFIGS_DIR
from csagent.prompts.domain_text import REQUIRED_KEYS


def _variants() -> list[Path]:
    return sorted(CONFIGS_DIR.glob("domain_text*.yaml"))


def test_variants_exist():
    files = _variants()
    assert files, "未找到任何 configs/domain_text*.yaml"
    # 至少应有一份转正的 domain_text.yaml
    assert any(p.name == "domain_text.yaml" for p in files)


@pytest.mark.parametrize("path", _variants(), ids=lambda p: p.name)
def test_every_variant_has_all_required_keys(path: Path):
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing = [k for k in REQUIRED_KEYS if k not in data]
    assert not missing, (
        f"{path.name} 缺少必需键 {missing}；"
        "新增领域文案键时，configs/domain_text*.yaml 每一份都要补"
    )


@pytest.mark.parametrize("path", _variants(), ids=lambda p: p.name)
def test_every_variant_values_are_non_empty_strings(path: Path):
    """空串会让兜底答复变成一条空消息发给用户，比报错更难排查。"""
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    blank = [k for k in REQUIRED_KEYS if not str(data.get(k) or "").strip()]
    assert not blank, f"{path.name} 的这些键为空: {blank}"


def test_handle_failed_reply_keeps_error_placeholder():
    """handle_failed_reply 要带 {error} 占位符，bridge 用 .format(error=exc) 填。"""
    for path in _variants():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        tpl = str(data.get("handle_failed_reply") or "")
        assert "{error}" in tpl, f"{path.name} 的 handle_failed_reply 丢了 {{error}} 占位符"
        # 能正常格式化，不会因为多余的花括号抛 KeyError
        assert tpl.format(error="boom")


def test_required_keys_matches_module_usage():
    """REQUIRED_KEYS 要覆盖模块里所有 _data["..."] 直取的键，否则校验会漏。"""
    import re

    src = Path(__file__).resolve().parents[1] / "src" / "csagent" / "prompts" / "domain_text.py"
    used = set(re.findall(r'_data\["([^"]+)"\]', src.read_text(encoding="utf-8")))
    assert used <= set(REQUIRED_KEYS), (
        f"这些键被直取但没进 REQUIRED_KEYS，切产品时会漏校验: {sorted(used - set(REQUIRED_KEYS))}"
    )
