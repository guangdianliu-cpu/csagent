"""内部研究资料脚本：internal-research 这个 Skill 的业务入口。

七个 action 由 Skill 的 tools.yaml 声明成原生工具（research_reports / research_record /
research_content / research_download / research_search / research_context / research_capabilities）：
内核把工具参数 JSON 经 stdin 交给 ``--payload -``。调试时也可以直接跑：

    python ask.py --action search --payload '{"query":"……"}'

action 一览：

| action | 用途 | --payload |
|---|---|---|
| `reports` | 按课题完成时间倒序列目录 | `{"page":1起, "page_size":1-50, "keyword":"标题片段", "filters":{可选}}` |
| `record` | 一行业务字段 + 全部附件映射 | `{"record_id":"来自 reports 的 report_id"}` |
| `content` | 按 document_id 顺序读已索引正文 | `{"document_id":"必填", "expected_version":"建议", "limit":1-50, "max_chars":1000-60000, "cursor":"续读"}` |
| `download` | 下载附件原件到 outbound | `{"attachment_id":"必填", "content_hash":"必填", "file_name":"必填-record原名含扩展名", "out_file":"可选"}` |
| `search` | 语义检索片段（定位用，不是读全文必经步骤） | `{"query":"必填", "filters":{可选}, "top_k":1-20}` |
| `context` | 扩展 search 命中片段前后文 | `{"document_id":"必填", "evidence_ids":["必填"], "before":0-2, "after":0-2}` |
| `capabilities` | 探活 | `{}` |

推荐流程：**指定资料 / 总结整篇 / 核对完整数据** → `reports` → `record` → `content`（必要时 `download`）。
`search` 只辅助定位局部观点；不要把「最新报告」改写成关键词去 search。

对端不收 conversation_id、不调大模型、不写会话记忆。**回答由本 Agent 自己组织**。
业务错误走 HTTP 200 + error_code（download 用真实 HTTP 状态码）。

出口契约：成功输出给模型读的文本，exit 0；失败输出 {"ok": false, "error": {"code", "message"}}，
exit 1。message 就是原来的人话说明；code 取对端的 error_code（大写），本地判出的失败用
BAD_PAYLOAD / UPSTREAM_TIMEOUT / UPSTREAM_UNREACHABLE / UPSTREAM_ERROR 等。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

import httpx

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from research_agent_client import (  # noqa: E402
    MAX_EVIDENCE_IDS,
    MAX_KEYWORD_CHARS,
    MAX_QUERY_CHARS,
    InternalResearchClient,
    clean_filters,
    format_capabilities,
    format_content_result,
    format_context_result,
    format_download_result,
    resolve_download_filename,  # noqa: F401 - 对外导出：测试经 ask 模块取用
    format_record_result,
    format_reports_result,
    format_search_result,
    normalize_content_limit,
    normalize_max_chars,
    normalize_page,
    normalize_page_size,
    normalize_top_k,
)

_ACTIONS = (
    "search",
    "context",
    "reports",
    "record",
    "content",
    "download",
    "capabilities",
)


def _load_project_env() -> None:
    """把仓库 .env 里的 RESEARCH_AGENT_* 补进 os.environ（进程环境优先）。"""
    root = os.environ.get("CSAGENT_PROJECT_ROOT")
    env_path = Path(root) / ".env" if root else Path(__file__).resolve().parents[3] / ".env"
    if not env_path.is_file():
        return
    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
    except Exception as exc:  # noqa: BLE001
        print(f"（提示: 读取 {env_path} 失败，将使用脚本内默认值: {exc}）", file=sys.stderr)


class _Failure:
    """一次失败的调用：code 给工具层与审计，message 是给模型看的人话说明。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message


# download 用真实 HTTP 状态码表达业务失败（见 research_agent_client.download_attachment）
_HTTP_STATUS_CODES = {
    404: "RESOURCE_NOT_FOUND",
    409: "RESOURCE_NOT_READY_OR_CHANGED",
    422: "BAD_PAYLOAD",
    503: "KNOWLEDGE_SERVICE_UNAVAILABLE",
}


def _bad(message: str) -> _Failure:
    return _Failure("BAD_PAYLOAD", message)


def _http_failure(exc: RuntimeError) -> _Failure:
    text = str(exc)
    if "SHA-256" in text:
        code = "CONTENT_HASH_MISMATCH"
    else:
        status = re.search(r"-> HTTP (\d{3})", text)
        code = _HTTP_STATUS_CODES.get(int(status.group(1)) if status else 0, "UPSTREAM_ERROR")
    return _Failure(code, _http_error_text(exc))


def _formatted(text: str, data: dict) -> str | _Failure:
    """格式化结果以「调用失败」开头的，是对端的业务失败（HTTP 200 + status/error_code）。"""
    if not text.startswith("调用失败"):
        return text
    peer_code = str((data or {}).get("error_code") or "").strip()
    if peer_code:
        return _Failure(peer_code.upper(), text)
    return _Failure("INCOMPLETE_RESPONSE" if "响应缺少" in text else "UPSTREAM_ERROR", text)


class _ArgError(Exception):
    """argparse 用法错误的可捕获形式。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # noqa: D102
        raise _ArgError(message)


def _build_parser() -> _Parser:
    parser = _Parser(description="内部研究资料脚本", add_help=False)
    parser.add_argument("--action", choices=_ACTIONS, required=True)
    parser.add_argument("--payload", default="{}", help="JSON 对象字符串；写 - 表示从 stdin 读")
    return parser


def _build_client(payload: dict) -> InternalResearchClient:
    return InternalResearchClient(timeout_seconds=payload.get("timeout_seconds") or None)


def _timeout(client: InternalResearchClient) -> _Failure:
    return _Failure("UPSTREAM_TIMEOUT", (
        f"检索超时（>{client.timeout_seconds:.0f}s）: 对端知识服务没在预期时间内返回。"
        "最多再试一次；仍超时就如实告诉用户「内部研究资料服务响应超时」，"
        "不要改用自己的知识作答。"
    ))


def _unreachable(client: InternalResearchClient, exc: Exception) -> _Failure:
    return _Failure("UPSTREAM_UNREACHABLE", (
        f"连不上内部研究资料服务（{client.base_url}）: {exc}\n"
        "常见原因：服务未启动、本机不在可访问该内网地址的网络里、或 RESEARCH_AGENT_BASE_URL 配错。"
        "先用 research_capabilities 探活；不要重复重试，直接如实告诉用户这项能力当前不可用。"
    ))


def _http_error_text(exc: RuntimeError) -> str:
    return (
        f"{exc}\nHTTP 层就失败了，通常是请求体不合法或服务没起来，重试解决不了。"
        "如实告知用户这项能力当前不可用。"
    )


async def _run_search(payload: dict) -> str | _Failure:
    query = str(payload.get("query") or payload.get("message") or "").strip()
    if not query:
        return _bad(
            "参数错误: payload.query 必填。写成一句完整的检索问题（含主题、时间、口径），"
            "对端按语义检索，光给一个词命中质量很差。"
        )
    if len(query) > MAX_QUERY_CHARS:
        return _bad(
            f"参数错误: query 长度 {len(query)} 超过 {MAX_QUERY_CHARS} 字符。"
            "先把要查的东西提炼成一个问题，不要把整篇文档塞进来。"
        )

    client = _build_client(payload)
    try:
        data = await client.search(
            query,
            filters=clean_filters(payload.get("filters")),
            top_k=normalize_top_k(payload.get("top_k")),
        )
    except httpx.TimeoutException:
        return _timeout(client)
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    text = format_search_result(data, out_file=payload.get("out_file"))
    if str(data.get("evidence_status") or "") == "service_unavailable" and not data.get("passages"):
        # HTTP 200、status 也可能是 success，但对端声明这次没查成——不是「内部没有资料」
        return _Failure("KNOWLEDGE_SERVICE_UNAVAILABLE", text)
    return _formatted(text, data)


async def _run_context(payload: dict) -> str | _Failure:
    document_id = str(payload.get("document_id") or "").strip()
    raw_ids = payload.get("evidence_ids") or payload.get("evidence_id") or []
    if isinstance(raw_ids, str):
        raw_ids = [raw_ids]
    evidence_ids = [str(x).strip() for x in raw_ids if str(x).strip()]

    if not document_id or not evidence_ids:
        return _bad(
            "参数错误: context 需要 document_id 和 evidence_ids，两者都来自上一次 search 的输出，"
            "不能自己编。先 search 拿到片段，再用同一片段里的 ID 扩展。"
        )
    if len(evidence_ids) > MAX_EVIDENCE_IDS:
        return _bad(
            f"参数错误: evidence_ids 最多 {MAX_EVIDENCE_IDS} 个，且必须同属一个 document_id。"
        )

    client = _build_client(payload)
    try:
        data = await client.context(
            document_id=document_id,
            evidence_ids=evidence_ids,
            before=payload.get("before", 2),
            after=payload.get("after", 2),
        )
    except httpx.TimeoutException:
        return _timeout(client)
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    return _formatted(format_context_result(data, out_file=payload.get("out_file")), data)


async def _run_reports(payload: dict) -> str | _Failure:
    keyword = str(payload.get("keyword") or "").strip()
    if len(keyword) > MAX_KEYWORD_CHARS:
        return _bad(
            f"参数错误: keyword 长度 {len(keyword)} 超过 {MAX_KEYWORD_CHARS} 字符。"
            "它是报告标题的字面包含匹配，不是问题——「列出最新报告」这类指令不要往里填，"
            "不确定就整个省掉，默认已经按课题完成时间倒序。"
        )

    client = _build_client(payload)
    try:
        data = await client.list_reports(
            page=normalize_page(payload.get("page")),
            page_size=normalize_page_size(payload.get("page_size")),
            keyword=keyword,
            filters=clean_filters(payload.get("filters")),
        )
    except httpx.TimeoutException:
        return _timeout(client)
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    return _formatted(format_reports_result(data, out_file=payload.get("out_file")), data)


async def _run_record(payload: dict) -> str | _Failure:
    record_id = str(
        payload.get("record_id") or payload.get("report_id") or ""
    ).strip()
    if not record_id:
        return _bad(
            "参数错误: record 需要 record_id（即 reports 返回的 report_id）。"
            "行号/标题/文件名不能代替稳定 ID。"
        )

    client = _build_client(payload)
    try:
        data = await client.get_record(record_id)
    except httpx.TimeoutException:
        return _timeout(client)
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    return _formatted(format_record_result(data, out_file=payload.get("out_file")), data)


async def _run_content(payload: dict) -> str | _Failure:
    document_id = str(payload.get("document_id") or "").strip()
    if not document_id:
        return _bad(
            "参数错误: content 需要 document_id（来自 record 附件字段或 search 命中）。"
            "不要把 report_id/record_id 当成 document_id。"
        )

    cursor = payload.get("cursor", None)
    if cursor is not None:
        cursor = str(cursor).strip()
        if not cursor or cursor.lower() == "null":
            cursor = None

    expected_version = str(payload.get("expected_version") or payload.get("document_version") or "").strip() or None

    client = _build_client(payload)
    try:
        data = await client.read_document(
            document_id,
            expected_version=expected_version,
            limit=normalize_content_limit(payload.get("limit")),
            max_chars=normalize_max_chars(payload.get("max_chars")),
            cursor=cursor,
        )
    except httpx.TimeoutException:
        return _timeout(client)
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    return _formatted(format_content_result(data, out_file=payload.get("out_file")), data)


async def _run_download(payload: dict) -> str | _Failure:
    attachment_id = str(payload.get("attachment_id") or "").strip()
    content_hash = str(
        payload.get("content_hash") or payload.get("expected_content_hash") or ""
    ).strip()
    file_name = str(
        payload.get("file_name") or payload.get("filename") or payload.get("out_file") or ""
    ).strip()
    if not attachment_id or not content_hash:
        return _bad(
            "参数错误: download 需要 attachment_id 与 content_hash，二者都来自 record 的附件字段；"
            "缺一不可。下载中断或哈希不一致时不能当完整文件使用。"
        )
    if not file_name:
        return _bad(
            "参数错误: download 还必须传 file_name（record 附件里的原始文件名，含 .pdf/.xlsx 等扩展名）。"
            "否则会落成无类型的临时名，send_file 发给用户时格式不对。"
            '示例 payload: {"attachment_id":"...","content_hash":"...","file_name":"报告原名.pdf"}'
        )

    client = _build_client(payload)
    try:
        data = await client.download_attachment(
            attachment_id,
            content_hash=content_hash,
            out_file=payload.get("out_file"),
            file_name=file_name,
            timeout_seconds=payload.get("timeout_seconds"),
        )
    except ValueError as exc:
        return _bad(f"参数错误: {exc}")
    except httpx.TimeoutException:
        return _Failure("UPSTREAM_TIMEOUT", (
            f"下载超时（>{client.timeout_seconds:.0f}s）: 不要把中断的文件当完整原件。"
            "可稍后重试；仍失败如实告知用户。"
        ))
    except httpx.HTTPError as exc:
        return _unreachable(client, exc)
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()

    return format_download_result(data)


async def _run_capabilities(payload: dict) -> str | _Failure:
    client = _build_client(payload)
    try:
        data = await client.capabilities()
    except httpx.HTTPError as exc:
        return _Failure("UPSTREAM_UNREACHABLE", f"探活失败（{client.base_url}）: {exc}")
    except RuntimeError as exc:
        return _http_failure(exc)
    finally:
        await client.aclose()
    return format_capabilities(data)


async def _dispatch(action: str, payload: dict) -> str | _Failure:
    if action == "capabilities":
        return await _run_capabilities(payload)
    if action == "context":
        return await _run_context(payload)
    if action == "reports":
        return await _run_reports(payload)
    if action == "record":
        return await _run_record(payload)
    if action == "content":
        return await _run_content(payload)
    if action == "download":
        return await _run_download(payload)
    return await _run_search(payload)


def _emit(result: str | _Failure) -> int:
    """成功原样输出，exit 0；失败包成统一信封，exit 1——原生工具与审计据退出码把调用记为失败。"""
    if isinstance(result, _Failure):
        print(json.dumps(
            {"ok": False, "error": {"code": result.code, "message": result.message}}, ensure_ascii=False
        ))
        return 1
    print(result)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except _ArgError as exc:
        return _emit(_bad(f"参数错误: {exc}"))

    # 原生工具传 --payload -：参数 JSON 从 stdin 读，不经命令行转义
    raw = sys.stdin.read() if args.payload == "-" else args.payload
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return _emit(_bad(f"参数错误: payload 不是合法 JSON: {exc}"))
    if not isinstance(payload, dict):
        return _emit(_bad("参数错误: payload 必须是 JSON 对象"))

    _load_project_env()
    return _emit(asyncio.run(_dispatch(args.action, payload)))


if __name__ == "__main__":
    raise SystemExit(main())
