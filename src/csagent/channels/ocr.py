"""可插拔 OCR 后端（可选依赖，未安装时明确降级）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


class OcrBackend(ABC):
    """OCR 抽象：输入本地图片路径，输出纯文本。"""

    name: str = "ocr"

    @abstractmethod
    def extract_text(self, image_path: Path) -> str | None:
        raise NotImplementedError


class TesseractOcrBackend(OcrBackend):
    """基于 pytesseract + Pillow（需系统安装 tesseract）。"""

    name = "tesseract"

    def extract_text(self, image_path: Path) -> str | None:
        try:
            from PIL import Image  # type: ignore
            import pytesseract  # type: ignore
        except ImportError:
            logger.debug("ocr_tesseract_deps_missing")
            return None
        try:
            image = Image.open(image_path)
            text = pytesseract.image_to_string(image, lang="chi_sim+eng")
            cleaned = (text or "").strip()
            return cleaned or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr_tesseract_failed", error=str(exc), path=str(image_path))
            return None


class RapidOcrBackend(OcrBackend):
    """基于 rapidocr_onnxruntime（纯 pip 可选依赖）。"""

    name = "rapidocr"

    def __init__(self) -> None:
        self._engine = None

    def extract_text(self, image_path: Path) -> str | None:
        try:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore
        except ImportError:
            logger.debug("ocr_rapidocr_deps_missing")
            return None
        try:
            if self._engine is None:
                self._engine = RapidOCR()
            result, _ = self._engine(str(image_path))
            if not result:
                return None
            # result: list of [box, text, score]
            lines = [str(item[1]).strip() for item in result if len(item) >= 2 and item[1]]
            text = "\n".join(lines).strip()
            return text or None
        except Exception as exc:  # noqa: BLE001
            logger.warning("ocr_rapidocr_failed", error=str(exc), path=str(image_path))
            return None


class CompositeOcrBackend(OcrBackend):
    """按顺序尝试多个后端，命中即返回。"""

    name = "composite"

    def __init__(self, backends: list[OcrBackend] | None = None) -> None:
        self._backends = backends or [RapidOcrBackend(), TesseractOcrBackend()]

    def extract_text(self, image_path: Path) -> str | None:
        for backend in self._backends:
            text = backend.extract_text(image_path)
            if text:
                logger.info("ocr_backend_hit", backend=backend.name, chars=len(text))
                return text
        return None


def create_default_ocr_backend() -> OcrBackend:
    return CompositeOcrBackend()
