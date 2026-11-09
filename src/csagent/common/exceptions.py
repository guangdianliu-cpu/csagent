"""统一异常体系。"""

from __future__ import annotations


class CsAgentError(Exception):
    """CS-Agent 业务/配置类异常基类。"""


class LlmUnavailableError(Exception):
    """LLM 服务不可用。"""


class ToolPackLoadError(CsAgentError):
    """工具包加载或初始化失败。"""


class ToolPackNotFoundError(CsAgentError):
    """tool_packs.yaml 中未定义指定工具包。"""


class ChannelError(CsAgentError):
    """入口通道错误（连接失败/消息格式异常）。"""


class AttachmentProcessError(CsAgentError):
    """附件处理失败（下载超时/格式不支持）。"""


class SkillValidationError(CsAgentError):
    """Skill 包校验失败（frontmatter 缺失/字段非法）。"""
