"""大工具结果外置：把硬截断从「信息丢失」改成「信息降级」（M3.1）。

问题
----
此前超过 ``react.max_tool_result_chars``（5 千字符）的工具结果直接被切掉尾巴，
补一句「大结果请落盘后 read_file」。被切掉的部分**再也拿不回来**——工具已经跑完
且不会重跑，模型手里只剩前 3 万字符。一次十万行的查询，后面九万多行就这么没了，
而模型看不出自己少了什么，于是拿着残缺数据往下答。

做法
----
超限时全文落盘到 ``data/artifacts/{session}/{id}.{ext}``、登记进 artifacts 表，
上下文里只留「结构摘要 + 前若干行 + artifact_id」。模型需要细节时用
``read_artifact`` 按页取回，需要任意过滤聚合时直接对那个文件 ``run_script``。

顺带一个别处换不来的好处：artifact 里冻结的是**当时那一刻**的数据截面。
模型事后要精确数值时取回它，比重新查一遍更准——重查拿到的是今天的数。
这也是 M3.2 数值纪律能够成立的前提：摘要里不留数字，是因为原始数字有地方可取。

存储形态
--------
JSON 数组会被重排成 **JSONL（一行一条）** 再落盘。这不是洁癖：大 JSON 结果通常
是**单行**的，原样存下来的话按行分页等于没分页，``run_script`` 也只能一次性
``json.load`` 整个文件。逐行化之后分页与流式处理都成立，语义一个字没少。
字典则按 ``indent=2`` 展开，同理。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import structlog

from csagent.infra.config import PROJECT_ROOT
from csagent.memory.artifact_store import ArtifactRecord, register_artifact

logger = structlog.get_logger(__name__)

# 外置文件的落点（相对项目根）。与 data/uploads 分开：那棵树是用户可见的收发件，
# 这里是机器产生、用户不可见的中间结果，清理策略也不同（见设计文档 M2.3）。
ARTIFACTS_DIRNAME = "artifacts"

# 占位文本里附带的预览行数。它的用途是让模型**判断结构**、决定接下来取哪一段，
# 不是让它据此作答——所以给得少而明确，并在文案里说死。
_PREVIEW_LINES = 20

# 推断字段名时最多扫多少条。十万行数组没必要全扫一遍求并集。
_COLUMN_SAMPLE_ROWS = 200


# ---------------------------------------------------------------- 落盘


def offload_tool_result(
    text: str,
    *,
    session_id: str,
    tool_name: str,
    limit: int,
    artifacts_root: Path | None = None,
) -> str | None:
    """超限的工具结果外置，返回给模型看的占位文本。

    **失败返回 None**，由调用方回落到原来的硬截断。外置是增强不是依赖：
    磁盘满了、目录没权限、登记库坏了，都不该让一次正常的工具调用失败。

    :param text: 工具返回的完整文本
    :param session_id: 所属会话，决定落点目录与归属校验
    :param tool_name: 工具名，写进占位文本帮助模型回忆这是什么结果
    :param limit: 上下文阈值；未超过则不外置
    :param artifacts_root: 落点根目录，默认 ``<项目根>/data/artifacts``
    :returns: 占位文本；无需外置或外置失败时返回 None
    """
    if not text or len(text) <= limit or not session_id:
        return None

    payload, shape = _normalize(text)
    root = artifacts_root or (PROJECT_ROOT / "data" / ARTIFACTS_DIRNAME)
    artifact_id = uuid.uuid4().hex

    # 先落盘再登记。反过来的话，写盘失败会在库里留下一条指向空气的登记，模型拿着
    # 它反复 read_artifact 全部失败，白烧轮次——那比直接截断更糟。
    # 这个顺序的代价只是「登记失败时盘上多一个没人认领的文件」，纯占磁盘，无害。
    try:
        from csagent.channels.workspace_uploads import safe_session_segment

        target_dir = Path(root) / safe_session_segment(session_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        path = target_dir / f"{artifact_id}.{shape.extension}"
        path.write_text(payload, encoding="utf-8")
    except OSError as exc:
        logger.warning("artifact_offload_write_failed", error=str(exc))
        return None

    rel_path = _relative_to_project(path)
    registered = register_artifact(
        session_id=session_id,
        kind="tool_result",
        path=rel_path,
        filename=f"{tool_name or 'tool_result'}.{shape.extension}",
        size=len(payload.encode("utf-8")),
        compute_hash=False,
        artifact_id=artifact_id,
    )
    if not registered:
        # 登记库不可用（或压根没接，如 memory 后端）。read_artifact 查不到归属就
        # 拒绝读取，此时给出 artifact_id 是在骗模型。
        logger.warning(
            "artifact_offload_skipped", reason="register_failed", path=rel_path
        )
        return None

    logger.info(
        "tool_result_offloaded",
        session_id=session_id, tool=tool_name,
        chars=len(text), artifact_id=artifact_id, path=rel_path,
    )
    return _render_placeholder(
        artifact_id=artifact_id,
        rel_path=rel_path,
        tool_name=tool_name,
        original_chars=len(text),
        payload=payload,
        shape=shape,
    )


# ---------------------------------------------------------------- 结构识别


class _Shape:
    """外置内容的形状，供摘要与分页使用。

    :param kind: ``jsonl`` 一行一条记录 / ``json`` 展开的对象 / ``text`` 纯文本
    :param lines: 总行数
    :param columns: 记录型内容的字段名（可能是抽样得到的）
    :param sampled: 字段名是否来自抽样（数据量大时只扫前若干条）
    """

    def __init__(
        self,
        kind: str,
        lines: int,
        columns: list[str] | None = None,
        sampled: bool = False,
    ) -> None:
        self.kind = kind
        self.lines = lines
        self.columns = columns or []
        self.sampled = sampled

    @property
    def extension(self) -> str:
        return {"jsonl": "jsonl", "json": "json"}.get(self.kind, "txt")

    def describe(self) -> str:
        """一行人话的结构说明。"""
        if self.kind == "jsonl":
            head = f"JSON 记录数组，共 {self.lines} 条（已逐行化，一行一条）"
            if self.columns:
                suffix = "（字段名取自前若干条，可能不全）" if self.sampled else ""
                return f"{head}；字段: {', '.join(self.columns)}{suffix}"
            return head
        if self.kind == "json":
            return f"JSON 对象，展开后 {self.lines} 行"
        return f"纯文本，共 {self.lines} 行"


def _normalize(text: str) -> tuple[str, _Shape]:
    """把原始文本整理成便于分页的落盘形态，并识别结构。

    :returns: (待落盘文本, 结构)
    """
    stripped = text.strip()
    if stripped[:1] in ("{", "["):
        try:
            data = json.loads(stripped)
        except (json.JSONDecodeError, ValueError, RecursionError):
            data = None
        if data is not None:
            return _normalize_json(data)
    return text, _Shape("text", _count_lines(text))


def _normalize_json(data: Any) -> tuple[str, _Shape]:
    """JSON 数组 → JSONL；JSON 对象 → 缩进展开。"""
    if isinstance(data, list):
        lines = [json.dumps(item, ensure_ascii=False) for item in data]
        payload = "\n".join(lines)
        columns, sampled = _infer_columns(data)
        return payload, _Shape("jsonl", len(lines), columns, sampled)

    payload = json.dumps(data, ensure_ascii=False, indent=2)
    return payload, _Shape("json", _count_lines(payload))


def _infer_columns(rows: list) -> tuple[list[str], bool]:
    """从记录数组里推断字段名（保持首次出现顺序）。

    :returns: (字段名列表, 是否为抽样结果)
    """
    seen: dict[str, None] = {}
    for item in rows[:_COLUMN_SAMPLE_ROWS]:
        if isinstance(item, dict):
            for key in item:
                seen.setdefault(str(key), None)
    return list(seen), len(rows) > _COLUMN_SAMPLE_ROWS


def _count_lines(text: str) -> int:
    return text.count("\n") + 1 if text else 0


# ---------------------------------------------------------------- 占位文本


def _render_placeholder(
    *,
    artifact_id: str,
    rel_path: str,
    tool_name: str,
    original_chars: int,
    payload: str,
    shape: _Shape,
) -> str:
    """组装回灌进上下文的占位文本。

    措辞上有三件事必须说清楚，少一件模型就会走偏：
    1. **没有丢**——否则模型会以为数据不可得，转而从预览里硬凑答案；
    2. 预览**不能据此作答**——20 行样本最容易被当成全集；
    3. 怎么取回，包括任意过滤该走哪条路（脚本直读文件，而不是反复翻页）。
    """
    preview = "\n".join(payload.split("\n")[:_PREVIEW_LINES])
    more = max(0, shape.lines - _PREVIEW_LINES)

    parts = [
        f"[大结果已外置] 工具 {tool_name} 返回 {original_chars:,} 字符，"
        "已完整保存到下面这个文件，**一个字都没有丢**。",
        f"artifact_id: {artifact_id}",
        f"文件: {rel_path}",
        f"结构: {shape.describe()}",
        "",
        f"前 {min(_PREVIEW_LINES, shape.lines)} 行"
        f"（{'还有 %d 行未显示；' % more if more else ''}仅供判断结构，**不要据此作答**）:",
        preview,
        "",
        "取回完整内容：",
        f'  read_artifact({{"artifact_id":"{artifact_id}","offset":1,"limit":200}})'
        "  — 按行分页，offset 从 1 开始",
    ]
    if shape.columns:
        parts.append(
            '  加 "columns":["字段A","字段B"] 只取需要的列，能显著省上下文'
        )
    parts.append(
        f"  需要任意过滤、聚合、统计时，别翻页——直接 run_script 读 {rel_path}，"
        "在脚本里算完只回结论。"
    )
    return "\n".join(parts)


# ---------------------------------------------------------------- 读回


def read_artifact_slice(
    record: ArtifactRecord,
    *,
    offset: int = 1,
    limit: int = 200,
    columns: list[str] | None = None,
    max_chars: int = 20000,
    artifacts_root: Path | None = None,
) -> str:
    """按行取回 artifact 的一段，返回给模型的文本（含分页导航）。

    :param record: 已校验归属的登记
    :param offset: 起始行号（1-based）
    :param limit: 最多返回多少行
    :param columns: 仅对记录型内容有效，只保留这些字段
    :param max_chars: 返回文本的字符上限，防止一次取回又把上下文撑爆
    :param artifacts_root: 允许读取的根目录；默认 ``<项目根>/data/artifacts``。
        登记表里的 path 若被改成指向别处，这里会拒绝——id 的熵不是访问控制。
    :returns: 供模型阅读的文本；文件已不存在或越界时返回说明文案
    """
    path = _resolve_readable_path(record, artifacts_root)
    if isinstance(path, str):
        return path

    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except OSError as exc:
        return f"错误: 读取 artifact {record.id} 失败：{exc}"

    total = len(lines)
    start = max(1, offset)
    end = min(total, start + max(1, limit) - 1)
    if start > total:
        return f"错误: offset={offset} 超出范围，该 artifact 共 {total} 行。"

    chunk = lines[start - 1 : end]
    if columns:
        chunk = [_project_columns(line, columns) for line in chunk]

    body = "\n".join(chunk)
    clipped = len(body) > max_chars
    if clipped:
        body = body[:max_chars]

    header = f"[artifact {record.id}] 第 {start}~{end} 行，共 {total} 行"
    footer = _pagination_hint(record.id, end, total, clipped=clipped, path=record.path)
    return f"{header}\n{body}\n{footer}"


def _project_columns(line: str, columns: list[str]) -> str:
    """对一行 JSON 记录做列投影；解析不了就原样返回。"""
    try:
        row = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return line
    if not isinstance(row, dict):
        return line
    return json.dumps({k: row[k] for k in columns if k in row}, ensure_ascii=False)


def _pagination_hint(
    artifact_id: str, end: int, total: int, *, clipped: bool, path: str
) -> str:
    """告诉模型「还有没有下一页」以及什么时候该换条路。"""
    if clipped:
        return (
            "[本段因字符上限被截断，请减小 limit 或用 columns 收窄字段后重取]"
        )
    if end >= total:
        return "[已到末尾]"
    return (
        f'[还有 {total - end} 行。下一页: read_artifact({{"artifact_id":"{artifact_id}",'
        f'"offset":{end + 1},"limit":200}})；'
        f"若只是要统计或筛选，改用 run_script 直接读 {path} 更省]"
    )


# ---------------------------------------------------------------- 内部


def _resolve_readable_path(
    record: ArtifactRecord, artifacts_root: Path | None
) -> Path | str:
    """把登记路径解析成可读文件；越出外置根目录则拒绝。

    :returns: 绝对路径；失败时返回给模型看的错误文案
    """
    root = (artifacts_root or (PROJECT_ROOT / "data" / ARTIFACTS_DIRNAME)).resolve()
    path = Path(record.path)
    resolved = path.resolve() if path.is_absolute() else (PROJECT_ROOT / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return (
            f"错误: artifact {record.id} 的路径越出允许目录，拒绝读取。"
            "请重新执行产生它的那次查询。"
        )
    if not resolved.is_file():
        return (
            f"错误: artifact {record.id} 的文件已不在盘上（{record.path}）。"
            "登记永久保留但文件可能被清理过，请重新执行产生它的那次查询。"
        )
    return resolved


def _relative_to_project(path: Path) -> str:
    """转成相对项目根的 posix 路径；越界时退回绝对路径。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError:
        return str(path)
