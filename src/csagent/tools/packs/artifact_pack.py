"""外置大结果的按需取回（M3.1）。

独立成包而不是塞进 ``workspace_files``：它不走工作区路径解析，只认 artifacts
表里已经登记过、且属于**当前会话**的记录。两边的安全边界不同——工作区工具防的
是路径穿越，这里防的是跨会话取回别人的中间结果。
"""

from __future__ import annotations

from typing import Any

from csagent.memory.artifact_offload import read_artifact_slice
from csagent.memory.artifact_store import get_artifact_store
from csagent.react.models import CustomerScope
from csagent.tools.base import BaseTool, ToolPack


class ReadArtifactTool(BaseTool):
    """按行取回已外置的大工具结果。"""

    name = "read_artifact"
    description = (
        "读取本轮或本会话已外置的大工具结果。参数 artifact_id 必填（来自工具结果里的"
        "[大结果已外置] 占位）。大结果必须用 offset(1-based)+limit 分页，建议 limit=200；"
        "记录型结果可传 columns 只取需要的列。需要过滤/聚合/统计时不要翻页，"
        "直接 run_script 读占位里给出的文件路径。"
    )

    @property
    def parameters_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "外置时返回的 artifact_id",
                },
                "offset": {
                    "type": "integer",
                    "description": "起始行号（1-based）。下一页=上一页结束行号+1",
                },
                "limit": {
                    "type": "integer",
                    "description": "本段最多读取行数，建议 200",
                },
                "columns": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "仅对 JSON 记录有效：只保留这些字段",
                },
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        }

    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        """按 id 取回一段；不属于当前会话的一律当不存在。

        :param arguments: artifact_id 必填；offset/limit/columns 可选
        :param scope: 当前请求的身份范围，用来卡会话归属
        :returns: 分页文本或错误说明
        """
        artifact_id = str(arguments.get("artifact_id") or "").strip()
        if not artifact_id:
            return "错误: 缺少参数 artifact_id"

        store = get_artifact_store()
        if store is None:
            return (
                "错误: 当前记忆后端没有产物清单，无法取回外置结果。"
                "请重新执行产生该结果的工具。"
            )

        record = store.get(artifact_id)
        # 不存在和「属于别人」返回同一句话：避免靠报错差异探测其他会话的 id。
        if record is None or record.session_id != scope.session_id:
            return "错误: artifact 不存在或不属于当前会话。"

        offset = _as_int(arguments.get("offset"), default=1)
        limit = _as_int(arguments.get("limit"), default=200)
        columns = arguments.get("columns")
        if not isinstance(columns, list):
            columns = None
        else:
            columns = [str(c) for c in columns if str(c).strip()]

        return read_artifact_slice(
            record, offset=offset, limit=limit, columns=columns or None
        )


def _as_int(value: Any, *, default: int) -> int:
    """把模型传来的数字参数收成 int；非法时回落到默认值。"""
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class ArtifactToolPack(ToolPack):
    """大结果外置的取回工具。无外部依赖，initialize 为空操作。"""

    pack_name = "artifacts"
    description = "按 artifact_id 分页取回已外置的大工具结果"

    def __init__(self) -> None:
        self._tools: list[BaseTool] = []

    def initialize(self, config: dict[str, Any]) -> None:
        """构造 ``read_artifact``；YAML 里的 description 若有则覆盖默认文案。"""
        self._tools = [ReadArtifactTool()]
        desc_by_name = {
            str(item.get("name")): str(item.get("description") or "")
            for item in (config.get("tools") or [])
            if isinstance(item, dict) and item.get("name")
        }
        for tool in self._tools:
            if desc_by_name.get(tool.name):
                tool.description = desc_by_name[tool.name]

    def get_tools(self) -> list[BaseTool]:
        return list(self._tools)
