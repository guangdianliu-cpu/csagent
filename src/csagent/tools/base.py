"""工具层抽象（v2：BaseTool + ToolPack）。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from csagent.react.models import CustomerScope


class ToolCallError(Exception):
    """工具跑完了，但这次调用失败：``text`` 原样交给模型，审计里记 error。

    普通异常会被注册中心包成「工具 X 执行失败: …」。工具自己已经写好给模型看的失败说明
    （比如结构化的 ``{"ok": false, "error": …}``）时抛它，不再多套一层前缀。
    """

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


class BaseTool(ABC):
    """所有工具的统一接口（保持 v1 兼容）。"""

    name: str
    description: str

    # 单工具超时（秒）。None = 用 ToolRegistry 的全局默认值。
    #
    # 只有「本身就该跑很久、且自己没有内部超时」的工具才需要覆盖它。像
    # run_script / run_shell（内部 60s）、web_search（内部 15s）这类已经自带
    # 超时并会返回友好文案的，保持 None 即可——注册中心那层只是兜底，触发它
    # 说明工具连自己的超时都没走到。
    timeout_seconds: float | None = None

    @property
    def parameters_schema(self) -> dict[str, Any]:
        """OpenAI function calling 参数 JSON Schema。

        子类可覆盖此属性返回精确 schema。
        默认返回宽松 schema，表示接受任意 key-value 参数（兼容现有工具的 dict 参数风格）。
        """
        return {
            "type": "object",
            "properties": {},
            "additionalProperties": True,
        }

    def approval_identity(self, arguments: dict[str, Any], scope: CustomerScope) -> dict[str, Any]:
        """确认卡片判断「是不是同一项操作」时用的参数，默认原样。

        同一个目标有多种写法的工具（如相对 / 完整路径）应在这里规整成一种，
        否则模型换个写法再调一次，就会多出一张指向同一操作的卡片。
        """
        return arguments

    @abstractmethod
    async def execute(self, arguments: dict[str, Any], scope: CustomerScope) -> str:
        """执行工具并返回可注入 LLM 上下文的文本结果。"""
        raise NotImplementedError


class ToolPack(ABC):
    """工具包抽象：一组语义相关的工具集合。

    设计要点：
    1. 工具包是工具注册的最小可插拔单元
    2. 工具包负责初始化自身依赖（连接池、SDK 客户端等）
    3. 工具包可以包含 1~N 个 BaseTool 实例
    4. 配置通过 config 字典传入，由工具包自行解析
    """

    pack_name: str = ""
    description: str = ""

    @abstractmethod
    def initialize(self, config: dict[str, Any]) -> None:
        """根据配置初始化工具包（建立连接、加载资源等）。"""
        raise NotImplementedError

    @abstractmethod
    def get_tools(self) -> list[BaseTool]:
        """返回该工具包提供的所有工具实例。"""
        raise NotImplementedError

    async def shutdown(self) -> None:
        """关闭工具包资源（连接池等），默认空实现。"""
        return None
