"""加载 configs/guardrail.yaml（M2.1，配置优先，改配置即可启停）。"""

from __future__ import annotations

from typing import Any

import yaml

from csagent.infra.config import CONFIGS_DIR


def load_guardrail_config() -> dict[str, Any]:
    """读取 guardrail.yaml 的 `guardrail` 节；文件不存在或为空时返回禁用态默认值。"""
    path = CONFIGS_DIR / "guardrail.yaml"
    if not path.exists():
        return {"enabled": False, "input_rules": [], "output_rules": []}
    with path.open(encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    cfg = raw.get("guardrail") or {}
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "input_rules": list(cfg.get("input_rules") or []),
        "output_rules": list(cfg.get("output_rules") or []),
    }
