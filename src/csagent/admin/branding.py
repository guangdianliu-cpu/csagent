"""品牌外观：宿主页面上悬浮按钮的图标。

管理后台上传图标、选择显示方式；SDK（static/agent-widget.js）启动时读公开的
``/api/branding`` 决定按钮上画什么。没有配置时 SDK 保持默认的 💬。

存储：``data/branding/`` 下一个图标文件 + ``branding.json``（不进仓库）。
只保留当前一份，换图即覆盖。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from csagent.infra.config import PROJECT_ROOT

BRANDING_DIR = PROJECT_ROOT / "data" / "branding"
_STATE_FILE = "branding.json"

MAX_ICON_BYTES = 512 * 1024
# 扩展名 → 响应 Content-Type
ICON_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
}
# fill：图片铺满圆形按钮（适合做好的圆形 logo）
# center：保留主题色圆底，图标居中缩小（适合透明底小图标）
ICON_MODES = ("fill", "center")


@dataclass
class BrandingState:
    icon_file: str = ""   # BRANDING_DIR 下的文件名；空 = 未配置
    icon_mode: str = "center"
    version: int = 0      # 换图 / 改方式时递增，给前端做缓存破坏

    def public(self) -> dict:
        return asdict(self)


def _dir() -> Path:
    return BRANDING_DIR


def load_state() -> BrandingState:
    path = _dir() / _STATE_FILE
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return BrandingState()
    state = BrandingState(
        icon_file=str(raw.get("icon_file") or ""),
        icon_mode=str(raw.get("icon_mode") or "center"),
        version=int(raw.get("version") or 0),
    )
    if state.icon_mode not in ICON_MODES:
        state.icon_mode = "center"
    # 文件被手动删掉时视为未配置
    if state.icon_file and not (_dir() / state.icon_file).is_file():
        state.icon_file = ""
    return state


def _save_state(state: BrandingState) -> None:
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{_STATE_FILE}.tmp"
    tmp.write_text(json.dumps(state.public(), ensure_ascii=False), encoding="utf-8")
    tmp.replace(d / _STATE_FILE)


def _bump(state: BrandingState) -> None:
    # 用时间戳而不是 +1：状态文件丢了重建时也不会撞回旧版本号、命中浏览器旧缓存
    state.version = max(state.version + 1, int(time.time()))


def looks_like_image(data: bytes, ext: str) -> bool:
    """按文件头粗查，防止把任意文件改个扩展名传上来。"""
    if ext == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if ext in (".jpg", ".jpeg"):
        return data.startswith(b"\xff\xd8\xff")
    if ext == ".webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if ext == ".svg":
        head = data[:2048].decode("utf-8", "ignore").lower()
        return "<svg" in head
    return False


def save_icon(data: bytes, ext: str) -> BrandingState:
    ext = ext.lower()
    d = _dir()
    d.mkdir(parents=True, exist_ok=True)
    state = load_state()
    new_name = f"launcher-icon{ext}"
    tmp = d / f".{new_name}.tmp"
    tmp.write_bytes(data)
    tmp.replace(d / new_name)
    # 换了扩展名时删掉旧文件，只留一份
    if state.icon_file and state.icon_file != new_name:
        (d / state.icon_file).unlink(missing_ok=True)
    state.icon_file = new_name
    _bump(state)
    _save_state(state)
    return state


def set_mode(mode: str) -> BrandingState:
    if mode not in ICON_MODES:
        raise ValueError(f"icon_mode 只能是 {', '.join(ICON_MODES)}")
    state = load_state()
    if state.icon_mode != mode:
        state.icon_mode = mode
        _bump(state)
        _save_state(state)
    return state


def reset_icon() -> BrandingState:
    state = load_state()
    if state.icon_file:
        (_dir() / state.icon_file).unlink(missing_ok=True)
    state.icon_file = ""
    _bump(state)
    _save_state(state)
    return state


def icon_path(state: BrandingState) -> Path | None:
    if not state.icon_file:
        return None
    path = _dir() / state.icon_file
    return path if path.is_file() else None
