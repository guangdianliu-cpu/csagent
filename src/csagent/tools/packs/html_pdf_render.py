"""HTML 转 PDF 的渲染后端。

WeasyPrint 优先（CSS 分页、无浏览器）；装不上或本机没有 Pango 时，回落到
已经装好的 Edge / Chrome 无头打印。Windows 中文后者往往更省事——系统字体现成。

不内置报告版式：调用方传入完整 HTML。WeasyPrint 缺中文字体时，只补一条
``@font-face`` 指向本机已有的 CJK 字体，避免方框；用户 HTML 里已有 @font-face
则不动。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from pathlib import Path

import structlog

from csagent.common.proc import kill_tree, spawn_kwargs

logger = structlog.get_logger(__name__)

# 本机常见中文字体。找到第一个存在的就用来喂 WeasyPrint，不是版式模板。
_CJK_FONT_CANDIDATES = (
    Path(r"C:\Windows\Fonts\msyh.ttc"),
    Path(r"C:\Windows\Fonts\simsun.ttc"),
    Path(r"C:\Windows\Fonts\simhei.ttf"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
)


class HtmlPdfError(RuntimeError):
    """渲染失败；文案可直接回给模型。"""


def available_engines() -> list[str]:
    """当前进程能用的引擎，按优先序。"""
    found: list[str] = []
    if _weasyprint_importable():
        found.append("weasyprint")
    if find_chromium():
        found.append("chromium")
    return found


def resolve_engine(requested: str) -> str:
    """把配置值解析成实际引擎名。

    :param requested: auto / weasyprint / chromium
    :raises HtmlPdfError: 指定的引擎不可用，或 auto 时一个都没有
    """
    want = (requested or "auto").strip().lower() or "auto"
    found = available_engines()
    if want == "auto":
        if not found:
            raise HtmlPdfError(
                "没有可用的 PDF 引擎。任选其一："
                "pip install 'cs-agent[pdf]'（WeasyPrint），"
                "或安装 Microsoft Edge / Google Chrome 后重试。"
            )
        return found[0]
    if want in {"weasyprint", "chromium"}:
        if want not in found:
            raise HtmlPdfError(_engine_missing_hint(want))
        return want
    raise HtmlPdfError(f"未知引擎 {requested!r}，可选 auto / weasyprint / chromium")


def render_html_to_pdf(
    html_path: Path,
    pdf_path: Path,
    *,
    engine: str = "auto",
    chromium_path: str = "",
    timeout_seconds: float = 60.0,
) -> str:
    """同步渲染。放进 ``asyncio.to_thread`` 以免堵住事件循环。

    :returns: 实际使用的引擎名
    """
    chosen = resolve_engine(engine)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if chosen == "weasyprint":
        _render_weasyprint(html_path, pdf_path)
        return chosen
    _render_chromium(
        html_path, pdf_path,
        chromium_path=chromium_path,
        timeout_seconds=timeout_seconds,
    )
    return chosen


async def render_html_to_pdf_async(
    html_path: Path,
    pdf_path: Path,
    *,
    engine: str = "auto",
    chromium_path: str = "",
    timeout_seconds: float = 60.0,
) -> str:
    """异步入口：WeasyPrint 丢线程池，Chromium 走子进程。"""
    chosen = resolve_engine(engine)
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    if chosen == "weasyprint":
        await asyncio.to_thread(_render_weasyprint, html_path, pdf_path)
        return chosen
    await _render_chromium_async(
        html_path, pdf_path,
        chromium_path=chromium_path,
        timeout_seconds=timeout_seconds,
    )
    return chosen


def find_chromium(explicit: str = "") -> Path | None:
    """定位 Edge / Chrome / Chromium 可执行文件。"""
    if explicit.strip():
        path = Path(explicit.strip())
        return path if path.is_file() else None
    env = (os.environ.get("HTML_TO_PDF_CHROMIUM") or os.environ.get("CHROMIUM_PATH") or "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
    for raw in _chromium_candidates():
        path = Path(raw)
        if path.is_file():
            return path
    for name in ("msedge", "chrome", "google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


# ---------- WeasyPrint ----------


def _weasyprint_importable() -> bool:
    """WeasyPrint 能否 import；缺 GTK/Pango 时视为不可用。"""
    try:
        import weasyprint  # noqa: F401
    except Exception:  # noqa: BLE001 — 缺 GTK/Pango 时 import 直接炸
        return False
    return True


def _render_weasyprint(html_path: Path, pdf_path: Path) -> None:
    """用 WeasyPrint 写出 PDF；缺中文字体时补一条 @font-face。"""
    try:
        from weasyprint import HTML
    except Exception as exc:  # noqa: BLE001
        raise HtmlPdfError(_engine_missing_hint("weasyprint")) from exc
    html_text = html_path.read_text(encoding="utf-8")
    css = _cjk_font_css()
    if css and "@font-face" not in html_text.lower():
        html_text = _inject_style(html_text, css)
    try:
        HTML(string=html_text, base_url=str(html_path.parent.resolve())).write_pdf(str(pdf_path))
    except Exception as exc:  # noqa: BLE001
        logger.warning("weasyprint_render_failed", error=str(exc))
        raise HtmlPdfError(f"WeasyPrint 渲染失败: {exc}") from exc
    _assert_pdf_written(pdf_path)


def _cjk_font_css() -> str:
    """本机有 CJK 字体时返回 @font-face CSS，否则空串。"""
    font = next((p for p in _CJK_FONT_CANDIDATES if p.is_file()), None)
    if font is None:
        return ""
    uri = font.resolve().as_uri()
    return (
        f'@font-face {{ font-family: "CsAgentCJK"; src: url("{uri}"); }}\n'
        'html, body { font-family: "CsAgentCJK", sans-serif; }\n'
    )


def _inject_style(html: str, css: str) -> str:
    """把 CSS 插进 ``<head>``；没有 head 就插到文档开头。"""
    snippet = f"<style>\n{css}</style>\n"
    lower = html.lower()
    idx = lower.find("<head")
    if idx >= 0:
        gt = html.find(">", idx)
        if gt >= 0:
            return html[: gt + 1] + snippet + html[gt + 1 :]
    return snippet + html


# ---------- Chromium / Edge ----------


def _chromium_candidates() -> list[str]:
    """按平台列出常见浏览器安装路径。"""
    if sys.platform.startswith("win"):
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        return [
            str(Path(pf) / "Microsoft/Edge/Application/msedge.exe"),
            str(Path(pf86) / "Microsoft/Edge/Application/msedge.exe"),
            str(Path(pf) / "Google/Chrome/Application/chrome.exe"),
            str(Path(pf86) / "Google/Chrome/Application/chrome.exe"),
            str(Path(local) / "Google/Chrome/Application/chrome.exe") if local else "",
        ]
    if sys.platform == "darwin":
        return [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    return [
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/microsoft-edge",
    ]


def _chromium_cmd(html_path: Path, pdf_path: Path, binary: Path) -> list[str]:
    """无头 print-to-pdf 命令行。"""
    return [
        str(binary),
        "--headless",
        "--disable-gpu",
        "--no-first-run",
        "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path.resolve()}",
        html_path.resolve().as_uri(),
    ]


def _render_chromium(
    html_path: Path,
    pdf_path: Path,
    *,
    chromium_path: str,
    timeout_seconds: float,
) -> None:
    """同步拉起 Edge/Chrome 出 PDF。"""
    import subprocess

    binary = find_chromium(chromium_path)
    if binary is None:
        raise HtmlPdfError(_engine_missing_hint("chromium"))
    cmd = _chromium_cmd(html_path, pdf_path, binary)
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout_seconds, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise HtmlPdfError(f"浏览器出 PDF 超时（{timeout_seconds:.0f}s）") from exc
    except OSError as exc:
        raise HtmlPdfError(f"无法启动浏览器 {binary}: {exc}") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or b"").decode("utf-8", errors="replace")[-400:]
        raise HtmlPdfError(f"浏览器出 PDF 失败（exit={proc.returncode}）{detail}")
    _assert_pdf_written(pdf_path)


async def _render_chromium_async(
    html_path: Path,
    pdf_path: Path,
    *,
    chromium_path: str,
    timeout_seconds: float,
) -> None:
    """异步拉起 Edge/Chrome 出 PDF，避免堵住事件循环。"""
    binary = find_chromium(chromium_path)
    if binary is None:
        raise HtmlPdfError(_engine_missing_hint("chromium"))
    cmd = _chromium_cmd(html_path, pdf_path, binary)
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs(),
        )
    except OSError as exc:
        raise HtmlPdfError(f"无法启动浏览器 {binary}: {exc}") from exc
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except TimeoutError as exc:
        # 浏览器会拉起一堆 renderer / gpu 子进程，只 kill 主进程会留下它们
        await kill_tree(proc)
        raise HtmlPdfError(f"浏览器出 PDF 超时（{timeout_seconds:.0f}s）") from exc
    except asyncio.CancelledError:
        await kill_tree(proc)
        raise
    if proc.returncode not in (0, None):
        detail = (stderr or b"").decode("utf-8", errors="replace")[-400:]
        raise HtmlPdfError(f"浏览器出 PDF 失败（exit={proc.returncode}）{detail}")
    _assert_pdf_written(pdf_path)


def _assert_pdf_written(pdf_path: Path) -> None:
    """确认目标文件存在且看起来像 PDF。"""
    if not pdf_path.is_file() or pdf_path.stat().st_size < 8:
        raise HtmlPdfError("引擎没有写出有效的 PDF 文件")


def _engine_missing_hint(engine: str) -> str:
    """给模型看的安装提示，不要堆栈。"""
    if engine == "weasyprint":
        return (
            "WeasyPrint 不可用。安装: pip install 'cs-agent[pdf]'。"
            "Linux 还需要 pango / fontconfig 系统库。"
        )
    return (
        "未找到 Edge / Chrome / Chromium。"
        "安装浏览器，或设环境变量 HTML_TO_PDF_CHROMIUM 指向可执行文件。"
    )
