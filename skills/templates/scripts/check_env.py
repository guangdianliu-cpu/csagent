"""利得模板与导出引擎环境前置自检工具 (Preflight Check)。

用于在执行研报与演示文稿导出任务前，前置探测系统依赖与渲染环境：
  1. PDF 转档引擎：LibreOffice / Word COM / PowerPoint COM
  2. 中文字体支持：微软雅黑、思源黑体、宋体及 Matplotlib 中文渲染
  3. 预览校验链路：pdftoppm / pymupdf / LibreOffice Draw
  4. 核心 Python 库：python-docx、python-pptx、matplotlib

用法：
    python skills/templates/scripts/check_env.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def _check_soffice() -> tuple[bool, str]:
    """探测系统安装的 LibreOffice。"""
    candidates = [
        r"C:\Program Files\LibreOffice\program\soffice.com",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.com",
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/usr/bin/libreoffice",
        "/usr/local/bin/soffice",
    ]
    for c in candidates:
        if c and Path(c).is_file():
            try:
                proc = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=5, check=False)
                ver = proc.stdout.strip() or proc.stderr.strip() or "已安装"
                return True, f"{ver} ({c})"
            except Exception:
                return True, f"已找到可执行文件 ({c})"
    return False, "未检测到 LibreOffice (建议 Linux 安装 libreoffice-nogui，Windows 安装 LibreOffice)"


def _check_windows_com(prog_id: str) -> tuple[bool, str]:
    """探测 Windows COM 自动化支持。

    优先通过轻量独立的 PowerShell 子进程进行探测，
    彻底避免在当前 Python 进程内直连 COM 在多线程或快速销毁时触发 Windows 0x800706ba RPC 致命异常。
    """
    if not sys.platform.startswith("win"):
        return False, "非 Windows 系统，不适用 COM 链路"

    # 1. 进程隔离探测 (安全首选)
    try:
        ps_cmd = (
            f"$o = New-Object -ComObject {prog_id}; "
            f"$o.Quit(); "
            f"[System.Runtime.InteropServices.Marshal]::ReleaseComObject($o) | Out-Null;"
        )
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            timeout=8,
            check=False,
        )
        if res.returncode == 0:
            return True, f"已检测到 Windows {prog_id} COM 自动化接口"
    except Exception:
        pass

    # 2. Python win32com 兜底探测
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore

        pythoncom.CoInitialize()
        try:
            obj = win32com.client.DispatchEx(prog_id)
            try:
                obj.Quit()
            except Exception:
                pass
            del obj
            return True, f"已检测到 Windows {prog_id} COM 自动化接口 (pywin32)"
        finally:
            pythoncom.CoUninitialize()
    except Exception:
        pass

    return False, f"未检测到可用 {prog_id} COM 自动化"


def _check_chinese_fonts() -> tuple[bool, list[str], str]:
    """探测系统已安装的中文字体。"""
    found_fonts: list[str] = []
    target_fonts = ["Microsoft YaHei", "SimSun", "SimHei", "Source Han Sans", "Noto Sans CJK SC", "PingFang SC"]

    # 1. 检查 matplotlib 字体管理器
    try:
        import matplotlib.font_manager as fm

        sys_fonts = {f.name for f in fm.fontManager.ttflist}
        for tf in target_fonts:
            if any(tf.lower() in sf.lower() for sf in sys_fonts):
                found_fonts.append(tf)
    except Exception:
        pass

    # 2. Windows 字体目录兜底检测
    if sys.platform.startswith("win"):
        font_dir = Path("C:/Windows/Fonts")
        if font_dir.is_dir():
            files = [f.name.lower() for f in font_dir.glob("*")]
            if ("msyh.ttc" in files or "msyh.ttf" in files) and "Microsoft YaHei" not in found_fonts:
                found_fonts.append("Microsoft YaHei")
            if ("simsun.ttc" in files or "simsun.ttf" in files) and "SimSun" not in found_fonts:
                found_fonts.append("SimSun")
            if ("simhei.ttf" in files) and "SimHei" not in found_fonts:
                found_fonts.append("SimHei")

    # 3. Matplotlib 中文渲染验证
    render_msg = ""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(2, 1))
        ax.text(0.5, 0.5, "利得投研测试", fontproperties="Microsoft YaHei" if "Microsoft YaHei" in found_fonts else None)
        fig.canvas.draw()
        plt.close(fig)
        render_msg = "Matplotlib 中文文本画布绘制正常"
    except Exception as e:
        render_msg = f"Matplotlib 绘制中文异常: {e}"

    font_ok = len(found_fonts) > 0
    return font_ok, found_fonts, render_msg


def _check_preview_engines() -> list[tuple[str, bool, str]]:
    """探测 PDF 预览渲染工具。"""
    results: list[tuple[str, bool, str]] = []

    # 1. pdftoppm
    p = shutil.which("pdftoppm")
    if p:
        results.append(("pdftoppm (poppler)", True, f"已安装: {p}"))
    else:
        results.append(("pdftoppm (poppler)", False, "未安装 (Linux 生产机建议: apt install poppler-utils)"))

    # 2. pymupdf
    try:
        try:
            import pymupdf as fitz  # type: ignore
        except ImportError:
            import fitz  # type: ignore

        ver = getattr(fitz, "__version__", "已安装")
        results.append(("pymupdf (fitz)", True, f"已安装: 版本 {ver}"))
    except ImportError:
        results.append(("pymupdf (fitz)", False, "未安装 (建议: pip install pymupdf)"))

    # 3. LibreOffice Draw
    soffice_ok, msg = _check_soffice()
    if soffice_ok:
        results.append(("LibreOffice Draw", True, "可用 (支持首页渲染兜底)"))
    else:
        results.append(("LibreOffice Draw", False, "不可用"))

    return results


def check_environment() -> dict[str, Any]:
    """收集并返回完整环境前置检查报告。"""
    report: dict[str, Any] = {}

    # 1. Python 核心库
    libs: dict[str, bool] = {}
    for lib in ["docx", "pptx", "matplotlib"]:
        try:
            __import__(lib)
            libs[lib] = True
        except ImportError:
            libs[lib] = False
    report["libraries"] = libs

    # 2. 转档引擎
    soffice_ok, soffice_desc = _check_soffice()
    word_com_ok, word_com_desc = _check_windows_com("Word.Application")
    ppt_com_ok, ppt_com_desc = _check_windows_com("PowerPoint.Application")

    report["engines"] = {
        "soffice": {"ok": soffice_ok, "desc": soffice_desc},
        "word_com": {"ok": word_com_ok, "desc": word_com_desc},
        "ppt_com": {"ok": ppt_com_ok, "desc": ppt_com_desc},
        "docx_to_pdf_available": soffice_ok or word_com_ok,
        "pptx_to_pdf_available": soffice_ok or ppt_com_ok,
    }

    # 3. 字体
    font_ok, fonts, render_desc = _check_chinese_fonts()
    report["fonts"] = {
        "ok": font_ok,
        "detected": fonts,
        "render_desc": render_desc,
    }

    # 4. 预览工具
    previews = _check_preview_engines()
    preview_dict = {name.split()[0].lower(): ok for name, ok, _ in previews}
    multipage_ok = preview_dict.get("pdftoppm", False) or preview_dict.get("pymupdf", False)
    singlepage_ok = preview_dict.get("libreoffice", False)

    if multipage_ok:
        preview_mode = "多页完备 (支持逐页高清目检)"
    elif singlepage_ok:
        preview_mode = "受限 (仅支持 LibreOffice 首页渲染，第2页及以后无法目检)"
    else:
        preview_mode = "不可用 (将跳过图片目检)"

    report["previews"] = {
        "available": multipage_ok or singlepage_ok,
        "multipage_available": multipage_ok,
        "mode": preview_mode,
        "details": previews,
    }

    return report


def main() -> int:
    """打印格式化报告并返回退出码。"""
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    print("=" * 68)
    print("      利得基金 Office 模板与导出引擎环境前置自检 (Preflight Check)")
    print("=" * 68)

    data = check_environment()

    # 1. 核心依赖
    print("\n[1] Python 核心排版库：")
    for lib, ok in data["libraries"].items():
        status = "OK" if ok else "FAIL"
        print(f"  - {lib:<16}: [{status}]")

    # 2. 转档引擎
    print("\n[2] PDF 高保真转换引擎：")
    soffice = data["engines"]["soffice"]
    s_status = "OK" if soffice["ok"] else "MISSING"
    print(f"  - LibreOffice    : [{s_status}] {soffice['desc']}")

    if sys.platform.startswith("win"):
        wcom = data["engines"]["word_com"]
        pcom = data["engines"]["ppt_com"]
        print(f"  - Word COM       : [{'OK' if wcom['ok'] else 'MISSING'}] {wcom['desc']}")
        print(f"  - PowerPoint COM : [{'OK' if pcom['ok'] else 'MISSING'}] {pcom['desc']}")

    can_word_pdf = data["engines"]["docx_to_pdf_available"]
    can_ppt_pdf = data["engines"]["pptx_to_pdf_available"]
    print(f"  * Word -> PDF 状态 : [{'可用' if can_word_pdf else '不可用'}]")
    print(f"  * PPT -> PDF 状态  : [{'可用' if can_ppt_pdf else '不可用'}]")

    # 3. 字体支持
    print("\n[3] 中文字体与渲染支持：")
    fonts_info = data["fonts"]
    f_status = "OK" if fonts_info["ok"] else "WARN"
    detected_str = ", ".join(fonts_info["detected"]) if fonts_info["detected"] else "未检测到常见中文字体"
    print(f"  - 中文字体库     : [{f_status}] 检测到: {detected_str}")
    print(f"  - Matplotlib 渲染: {fonts_info['render_desc']}")

    # 4. 预览目检工具
    print("\n[4] PDF 页面渲染目检工具 (pdf_preview.py)：")
    for name, ok, desc in data["previews"]["details"]:
        print(f"  - {name:<18}: [{'OK' if ok else 'MISSING'}] {desc}")
    status_label = "可用" if data["previews"]["multipage_available"] else ("受限" if data["previews"]["available"] else "降级")
    print(f"  * 目检链路状态  : [{status_label}] {data['previews']['mode']}")

    # 5. 建议与总结
    print("\n" + "-" * 68)
    issues = []
    if not can_word_pdf or not can_ppt_pdf:
        issues.append("PDF 引擎缺失：若需交付 .pdf 请安装 LibreOffice 或 Microsoft Office，否则仅交付 .docx/.pptx 并向用户说明。")
    if not fonts_info["ok"]:
        issues.append("中文字体缺失：可能导致 PDF 导出时中文显示为方块或回退西文字体，建议安装中文字体包。")
    if not data["previews"]["multipage_available"]:
        if data["previews"]["available"]:
            issues.append("多页目检受限：当前环境仅支持 LibreOffice 首页渲染，多页目检不可用。请执行 `pip install pymupdf` 以开启完整多页排版目检自查。")
        else:
            issues.append("预览工具缺失：无法生成排版预览图，请执行 `pip install pymupdf` 以开启生成后排版图片目检。")

    if issues:
        print("环境降级与配置建议：")
        for idx, iss in enumerate(issues, 1):
            print(f"  {idx}. {iss}")
    else:
        print("环境状态完备：支持 Word/PPT 构建、PDF 无损转档与多页渲染自查！")
    print("=" * 68)

    all_libs_ok = all(data["libraries"].values())
    return 0 if all_libs_ok else 1


if __name__ == "__main__":
    sys.exit(main())
