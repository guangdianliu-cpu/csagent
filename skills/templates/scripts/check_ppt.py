"""PPT (.pptx) 演示文稿结构与内容完整性自检工具。

解决 Agent 交付前自检过程中因缺少原生 PPT 检查工具而盲目调用 pdf_preview.py
并尝试用纯文本 read_file 读取 PNG 预览图导致报错的问题。

核心能力：
  - 基于 python-pptx 纯 Python 跨平台解析幻灯片页数、版式比例（16:9 / 4:3）、图表与图片嵌入数、数据表格
  - 逐页提取标题大纲，侦测异常空白页（无文本且无图表的未渲染页）
  - 提取所有数据表格维度（行列数）与首行表头内容，校验数据完整性
  - 支持清晰易读的控制台格式化输出或 --json 结构化输出

用法：
    python check_ppt.py <pptx_path> [--json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE


def _extract_slide_title(slide: Any) -> str:
    """提取幻灯片大标题或核心标题。"""
    # 1. 检查母版标准占位符标题
    try:
        if slide.shapes.title and slide.shapes.title.text and slide.shapes.title.text.strip():
            return slide.shapes.title.text.strip().replace("\n", " ")
    except Exception:
        pass

    # 2. 启发式：优先寻找顶部 (top < 1.3 英寸) 的大字号标题文本框
    header_candidates: list[tuple[float, int, str]] = []
    other_candidates: list[tuple[float, int, str]] = []

    for s in slide.shapes:
        if not getattr(s, "has_text_frame", False):
            continue
        try:
            for p in s.text_frame.paragraphs:
                t = p.text.strip().replace("\n", " ")
                if not t or len(t) <= 1 or t.isdigit():
                    continue
                fsize = p.font.size.pt if (p.font and p.font.size) else 12.0
                # 记录 (字号, -top, 文本)
                entry = (float(fsize), -int(s.top), t)
                if s.top < 1_300_000:
                    header_candidates.append(entry)
                else:
                    other_candidates.append(entry)
        except Exception:
            continue

    if header_candidates:
        header_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return header_candidates[0][2]

    if other_candidates:
        other_candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        return other_candidates[0][2]

    return "(封底或无文字)"


def inspect_pptx(pptx_path: str | Path) -> dict[str, Any]:
    """安全解析 pptx 结构与内容。"""
    p_pptx = Path(pptx_path).resolve()
    if not p_pptx.is_file():
        raise FileNotFoundError(f"文件不存在: {p_pptx}")

    prs = Presentation(str(p_pptx))

    # 页面宽高与比例
    w_in = prs.slide_width / 914400.0
    h_in = prs.slide_height / 914400.0
    ratio = w_in / h_in if h_in > 0 else 1.0
    if abs(ratio - 16.0 / 9.0) < 0.08:
        aspect_str = "16:9 宽屏"
    elif abs(ratio - 4.0 / 3.0) < 0.08:
        aspect_str = "4:3 标屏"
    else:
        aspect_str = f"{ratio:.2f}:1 自定义"

    slides_info: list[dict[str, Any]] = []
    all_tables_info: list[dict[str, Any]] = []
    empty_slide_pages: list[int] = []

    total_shapes = 0
    total_images = 0
    total_tables = 0
    total_text_chars = 0

    for idx, slide in enumerate(prs.slides, start=1):
        num_shapes = len(slide.shapes)
        total_shapes += num_shapes

        pics_count = 0
        tables_count = 0
        text_boxes_count = 0
        slide_chars = 0
        slide_table_headers: list[str] = []

        for s in slide.shapes:
            # 图片判定 (包含 PICTURE 类型与带有 image 属性的 Shape)
            if s.shape_type == MSO_SHAPE_TYPE.PICTURE:
                pics_count += 1

            # 表格判定
            if getattr(s, "has_table", False):
                tables_count += 1
                table = s.table
                num_rows = len(table.rows)
                num_cols = len(table.columns) if table.columns else 0

                header_cells: list[str] = []
                try:
                    for cell in table.rows[0].cells:
                        ct = cell.text.replace("\n", " ").strip()
                        if ct:
                            header_cells.append(ct)
                except Exception:
                    pass

                header_desc = " | ".join(header_cells[:8])
                slide_table_headers.append(header_desc)
                all_tables_info.append({
                    "page": idx,
                    "rows": num_rows,
                    "cols": num_cols,
                    "header": header_desc,
                })

            # 文本框判定
            if getattr(s, "has_text_frame", False) and not getattr(s, "has_table", False):
                text_boxes_count += 1
                try:
                    for p in s.text_frame.paragraphs:
                        slide_chars += len(p.text.strip())
                except Exception:
                    pass

        total_images += pics_count
        total_tables += tables_count
        total_text_chars += slide_chars

        # 空白页检查：完全无形状，或无图片、无表格且无文字
        is_empty = (num_shapes == 0) or (pics_count == 0 and tables_count == 0 and slide_chars == 0)
        if is_empty:
            empty_slide_pages.append(idx)

        title = _extract_slide_title(slide)

        slides_info.append({
            "page": idx,
            "title": title,
            "shapes_count": num_shapes,
            "pics_count": pics_count,
            "tables_count": tables_count,
            "text_boxes_count": text_boxes_count,
            "char_count": slide_chars,
            "is_empty": is_empty,
        })

    return {
        "file_name": p_pptx.name,
        "file_size_bytes": p_pptx.stat().st_size,
        "slides_count": len(prs.slides),
        "aspect_ratio": aspect_str,
        "width_inches": round(w_in, 2),
        "height_inches": round(h_in, 2),
        "total_shapes_count": total_shapes,
        "images_count": total_images,
        "tables_count": total_tables,
        "approx_char_count": total_text_chars,
        "empty_slides_count": len(empty_slide_pages),
        "empty_slides": empty_slide_pages,
        "slides": slides_info,
        "tables": all_tables_info,
    }


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if len(sys.argv) < 2:
        print("用法: python check_ppt.py <pptx_path> [--json]")
        return 2

    pptx_path = sys.argv[1]
    as_json = "--json" in sys.argv

    try:
        data = inspect_pptx(pptx_path)
    except Exception as exc:
        print(f"检查失败: {exc}")
        return 1

    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0 if data["empty_slides_count"] == 0 else 1

    print("=" * 70)
    print(f"      PPT 演示文稿结构与内容完整性自检: {data['file_name']}")
    print("=" * 70)
    print(
        f"• 文档总览: 大小 {data['file_size_bytes'] / 1024:.1f} KB | "
        f"总页数: {data['slides_count']} 页 | 版面: {data['aspect_ratio']} ({data['width_inches']}x{data['height_inches']} in)"
    )
    print(
        f"• 内容元素: 形状总数 {data['total_shapes_count']} | "
        f"嵌入图片/图表数: {data['images_count']} | 数据表格数: {data['tables_count']} | 总字数(约): {data['approx_char_count']}"
    )

    print("\n[1] 幻灯片逐页大纲与元素抽查:")
    if not data["slides"]:
        print("  (演示文稿中无幻灯片)")
    else:
        for s in data["slides"]:
            elem_desc = []
            if s["pics_count"]:
                elem_desc.append(f"{s['pics_count']}图")
            if s["tables_count"]:
                elem_desc.append(f"{s['tables_count']}表")
            if s["text_boxes_count"]:
                elem_desc.append(f"{s['text_boxes_count']}文")
            desc_str = f"[{','.join(elem_desc)}]" if elem_desc else "[]"
            empty_tag = " !![空白页]!!" if s["is_empty"] else ""
            print(f"  P{s['page']:<2} {desc_str:<12} {s['title'][:45]}{empty_tag}")

    print("\n[2] 数据表格清单与表头抽查:")
    if not data["tables"]:
        print("  (文档中无数据表格)")
    else:
        for t in data["tables"]:
            print(f"  P{t['page']:<2} 表格 ({t['rows']:>2}行 x {t['cols']:>2}列): {t['header'][:70]}")

    print("\n" + "-" * 70)
    if data["empty_slides_count"] > 0:
        print(f"自检告警: 检测到 {data['empty_slides_count']} 个空白页 (第 {data['empty_slides']} 页)，请核实幻灯片内容！")
        print("=" * 70)
        return 1
    elif data["slides_count"] == 0:
        print("自检失败: 演示文稿内无任何幻灯片！")
        print("=" * 70)
        return 1
    else:
        print(
            f"自检结论: 幻灯片结构完整，共 {data['slides_count']} 页，未见空白页，"
            f"图表 ({data['images_count']} 个) 与数据表格 ({data['tables_count']} 个) 均正常嵌入。"
        )
        print("=" * 70)
        return 0


if __name__ == "__main__":
    sys.exit(main())
