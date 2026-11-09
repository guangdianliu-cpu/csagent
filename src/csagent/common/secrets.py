"""已知密钥值的识别与遮挡（数据防泄漏的最后一道兜底）。

从 ``.env`` / ``.env.<形态>``、进程环境与 Settings 里收集「名字像密钥」的变量值
（KEY / SECRET / TOKEN / PASSWORD 等，见 :func:`is_secret_env_name`），供三处使用：

1. 输出护栏的 ``secret_values`` 规则：回复正文、思考过程里原样出现就遮掉；
2. 工具结果过滤（ToolRegistry）：进模型上下文之前先遮掉——模型看不到，就不会复述，
   值也不会顺着上下文进 LLM 服务商、会话审计与记忆库；
3. send_file 外发前扫描文件内容：脚本把密钥写进文件再发出去，绕过了前两处。

只认长度 >= :data:`MIN_SECRET_CHARS` 的值：短值（端口、开关、"true"）误伤面太大。
**值本身绝不进日志**，日志里只出现个数。
"""

from __future__ import annotations

import os
import re
import threading
import zipfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

MIN_SECRET_CHARS = 12
DEFAULT_SECRET_MASK = "[已屏蔽]"

_SECRET_ENV_NAME = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|PRIVATE", re.IGNORECASE
)
_SECRET_ENV_EXACT = frozenset({"ADMIN_ACCOUNTS"})

# Settings 里的密钥字段：值可能来自 Settings 默认之外的渠道，单独再收一遍
_SETTINGS_SECRET_FIELDS = (
    "llm_api_key",
    "llm_vision_api_key",
    "wecom_secret",
    "agent_shared_secret",
    "admin_token_secret",
)

# 外发文件扫描的上限：普通文件读这么多字节；zip 容器（xlsx/docx/pptx）解压总量上限
_SCAN_MAX_BYTES = 20 * 1024 * 1024
_SCAN_MAX_UNZIPPED_BYTES = 64 * 1024 * 1024

# ASCII 可见字符（不含空格）：号码、密钥、URL 都由它组成，流式遮挡按它判断边界
_TOKEN_CHAR = re.compile(r"[\x21-\x7e]")


def is_secret_env_name(name: str) -> bool:
    """环境变量名是否像密钥（KEY / SECRET / TOKEN / PASSWORD 等，或 ADMIN_ACCOUNTS）。"""
    return name.upper() in _SECRET_ENV_EXACT or bool(_SECRET_ENV_NAME.search(name))


def is_token_char(ch: str) -> bool:
    """单个字符是否属于「可能组成号码 / 密钥」的 ASCII 可见字符。"""
    return bool(_TOKEN_CHAR.fullmatch(ch))


def _values_from(name: str, value: str | None) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    if name.upper() == "ADMIN_ACCOUNTS":
        # 「用户名:口令哈希」逗号分隔：用户名不是秘密，只收哈希
        return [item.partition(":")[2].strip() for item in raw.split(",") if ":" in item]
    return [raw]


def collect_secret_values() -> list[str]:
    """收集当前进程可见的全部已知密钥值（去重，按长度从长到短）。"""
    values: set[str] = set()
    try:
        from dotenv import dotenv_values

        from csagent.infra.config import env_files

        for env_path in env_files():
            if env_path.is_file():
                for name, value in dotenv_values(env_path, encoding="utf-8").items():
                    if is_secret_env_name(name):
                        values.update(_values_from(name, value))
    except Exception as exc:  # noqa: BLE001 - 读不到 .env 不能拖垮装配
        logger.warning("secret_values_dotenv_read_failed", error=str(exc))

    for name, value in os.environ.items():
        if is_secret_env_name(name):
            values.update(_values_from(name, value))

    try:
        from csagent.infra.config import get_settings

        settings = get_settings()
        for field in _SETTINGS_SECRET_FIELDS:
            values.update(_values_from(field, getattr(settings, field, "")))
    except Exception as exc:  # noqa: BLE001
        logger.warning("secret_values_settings_read_failed", error=str(exc))

    return sorted((v for v in values if len(v) >= MIN_SECRET_CHARS), key=len, reverse=True)


class SecretMasker:
    """把已知密钥值替换成占位文本；没有任何密钥时什么都不做。"""

    def __init__(self, values: list[str], *, mask_with: str = DEFAULT_SECRET_MASK) -> None:
        ordered = sorted({v for v in values if v}, key=len, reverse=True)
        self._values = tuple(ordered)
        self._mask_with = mask_with
        self._pattern: re.Pattern[str] | None = (
            re.compile("|".join(re.escape(v) for v in ordered)) if ordered else None
        )
        self._bytes_pattern: re.Pattern[bytes] | None = (
            re.compile(b"|".join(re.escape(v.encode("utf-8")) for v in ordered))
            if ordered
            else None
        )

    @property
    def count(self) -> int:
        return len(self._values)

    @property
    def pattern_source(self) -> str:
        """合并后的正则源码（交给 OutputRule）；没有密钥时为空串。"""
        return self._pattern.pattern if self._pattern is not None else ""

    @property
    def stream_holdback(self) -> int:
        """流式遮挡需要额外扣住的尾部长度。

        全由 ASCII 可见字符组成的密钥不需要：流式遮挡本来就会扣住尾部未结束的
        ASCII 串。含空格或非 ASCII 字符的密钥可能被这样的边界切开，得多扣
        「最长者长度 - 1」个字符，保证它的开头不会先于结尾发出。
        """
        lengths = [
            len(v) for v in self._values if not all(is_token_char(ch) for ch in v)
        ]
        return max(lengths) - 1 if lengths else 0

    def mask(self, text: str) -> str:
        if not text or self._pattern is None:
            return text
        return self._pattern.sub(self._mask_with, text)

    def contains(self, text: str) -> bool:
        return bool(text) and self._pattern is not None and self._pattern.search(text) is not None

    def file_contains_secret(self, path: Path) -> bool:
        """文件内容里是否出现已知密钥值。

        普通文件按原始字节扫前 20 MB；xlsx / docx / pptx 这类 zip 容器逐个成员解压后扫
        （正文在压缩流里，不解压看不到）。PDF 等其它压缩格式扫不到，这里不承诺。
        读失败按「未发现」处理并告警——扫描是兜底，不该让正常的发文件失败。
        """
        if self._bytes_pattern is None:
            return False
        try:
            if zipfile.is_zipfile(path):
                return self._zip_contains(path)
            with path.open("rb") as handle:
                data = handle.read(_SCAN_MAX_BYTES)
            return self._bytes_pattern.search(data) is not None
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            logger.warning("secret_scan_failed", file=path.name, error=str(exc))
            return False

    def _zip_contains(self, path: Path) -> bool:
        assert self._bytes_pattern is not None
        budget = _SCAN_MAX_UNZIPPED_BYTES
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir() or budget <= 0:
                    continue
                with archive.open(info) as member:
                    data = member.read(budget)
                budget -= len(data)
                if self._bytes_pattern.search(data) is not None:
                    return True
        return False


_masker: SecretMasker | None = None
_masker_lock = threading.Lock()


def get_secret_masker() -> SecretMasker:
    """进程内共享的密钥遮挡器；首次调用时收集一次密钥值并缓存。"""
    global _masker
    if _masker is None:
        with _masker_lock:
            if _masker is None:
                _masker = SecretMasker(collect_secret_values())
                logger.info("secret_masker_ready", secret_count=_masker.count)
    return _masker


def reset_secret_masker(masker: SecretMasker | None = None) -> None:
    """替换或清空缓存的遮挡器（测试、热更新密钥后用）。"""
    global _masker
    with _masker_lock:
        _masker = masker
