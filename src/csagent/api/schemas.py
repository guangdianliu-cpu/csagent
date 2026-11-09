"""API 请求/响应模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from csagent.admin.quick_entries import QuickEntries


class ChatRequest(BaseModel):
    """流式聊天请求。"""

    # 允许为空：用户可以只发文件不打字（直接甩个文件过来让你看，是真实高频用法）。
    # 空文本 + 有附件时，由 MessageGateway 注入默认指令，不在这里拦。
    message: str = Field(default="", max_length=4000)
    # session_id 已移除：它是记忆边界，必须由服务端从验签后的身份派生
    # （infra/auth.py::Principal.session_id），客户端可选就等于可以读任何人的历史。
    channel: str = Field(default="internal_agent", max_length=32)
    # 先调 /api/files/upload 拿到的工作区路径列表
    attachment_paths: list[str] = Field(default_factory=list, max_length=20)
    # 卡片批准并执行后，带着这张卡片的 task_id 再发一轮：服务端取出执行结果回灌给模型，
    # 让它把原任务做完。此时 message 可以为空（正文由服务端给）
    resume_task_id: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def _require_text_or_attachment(self) -> "ChatRequest":
        if self.resume_task_id:
            return self
        if not self.message.strip() and not self.attachment_paths:
            raise ValueError("message 与 attachment_paths 不能同时为空")
        return self


class ChatConfigResponse(BaseModel):
    """聊天界面配置。前端据此决定是否走联调登录。"""

    show_thinking_process: bool
    # 服务端是否开启鉴权；false 只应出现在本地开发
    auth_enabled: bool = True
    # 是否暴露 /api/auth/dev-token（联调后门），生产恒为 false
    dev_login: bool = False


class ChatEntriesResponse(BaseModel):
    """聊天页快捷入口。``entries`` 为 None 时前端整块不显示。"""

    # online：管理后台保存的；default：仓库里形态目录的 YAML；none：都没有
    source: Literal["online", "default", "none"]
    # 内容变了就变，前端据此判断要不要重绘
    version: int
    entries: QuickEntries | None = None


class ChatSuggestResponse(BaseModel):
    """关键词联想：认出的名称与代入后的候选问题。没认出时 ``suggestions`` 为空。"""

    entity: str = ""
    name: str = ""
    suggestions: list[str] = Field(default_factory=list)


class HistoryFile(BaseModel):
    """历史消息里的一个文件。

    ``url`` 由服务端在**返回时**现签（含 dl 授权），不落库——授权 24 小时过期，
    而记录留 30 天，存下来的链接绝大多数时候是死的。
    """

    filename: str
    url: str
    # 字节数，文件卡片上显示；文件已被清理时为 0
    size: int = 0


class ReportCard(BaseModel):
    """对话里的一张报告卡片（SSE ``report`` 事件、历史回填、确认续跑共用）。"""

    report_id: str
    version: int
    title: str
    summary: str = ""
    # 有表格才出「Excel」按钮；M7 时落库的旧记录没有这一项，按有处理
    has_tables: bool = True


class ChatHistoryMessage(BaseModel):
    """会话历史里的一条消息。"""

    role: str
    content: str
    # 本条涉及的文件（用户传的 / 助手发的），带现签的下载链接
    files: list[HistoryFile] = Field(default_factory=list)
    # 本条发布 / 更新的报告
    reports: list[ReportCard] = Field(default_factory=list)


class ChallengeDecisionRequest(BaseModel):
    """网页卡片上的批准或拒绝。"""

    task_id: str = Field(min_length=1, max_length=80)
    action: Literal["confirm", "cancel"]


class ChallengeFile(BaseModel):
    """批准执行后随结果一起返回的文件。"""

    filename: str
    url: str


class ChallengeDecisionResponse(BaseModel):
    """卡片点击的结果。

    ``status`` 为 approved / cancelled / blocked（前面还有没处理的卡片）/ forbidden /
    duplicate / missing。approved 时 ``resume_task_id`` 非空：前端拿它调
    ``/api/chat/stream`` 续跑原任务。
    """

    status: str
    reply: str = ""
    files: list[ChallengeFile] = Field(default_factory=list)
    reports: list[ReportCard] = Field(default_factory=list)
    resume_task_id: str = ""
    # blocked 时挡在前面的那张卡片
    blocked_by: str = ""


class PendingChallenge(BaseModel):
    """刷新页面后还要画出来的一张待确认卡片。"""

    task_id: str
    tool: str = ""
    summary: str = ""
    reason: str = ""
    # 同一任务里前面还有没处理完的确认时为 false：前端置灰，等前面的处理完再点
    ready: bool = True
    # 过期时刻（Unix 秒）；前端显示成「HH:MM 前有效」
    expires_at: float = 0


class PendingChallengeList(BaseModel):
    """当前会话里还没点过的确认卡片。"""

    items: list[PendingChallenge] = Field(default_factory=list)


class ChatHistoryResponse(BaseModel):
    """刷新后回填聊天区用的历史消息。

    给的是**短期记忆窗口**里那几轮（``chat.max_history_messages``，默认 20 条），
    不是完整对话存档——存档是审计侧的 data/audit/。这样回填出来的内容与模型真正
    还记得的上下文是同一份，不会出现「屏幕上有、模型不认」的错位。
    """

    messages: list[ChatHistoryMessage] = Field(default_factory=list)


class ReportVersionResponse(BaseModel):
    """``GET /api/reports/{id}``：一版报告的完整内容，外加已有版本号供面板切换。"""

    report: dict
    versions: list[int]
    # 图表积木在服务端画好的 SVG，键是全篇积木的顺序下标（各章节依次数下来；面板与 PDF 用同一份）
    charts: dict[str, str] = Field(default_factory=dict)
    # 记分卡表格的最优格 [[行, 列], …]；对象卡是 [[指标, 对象], …]。键同 charts
    highlights: dict[str, list[list[int]]] = Field(default_factory=dict)
    # 各赢几项：{"wins": [[名字, 项数], …], "ties": 并列项数}，键同 charts
    tallies: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # 配置里的通用风险提示，排在报告自己的 risk_note 后面
    risk_note_default: str = ""


class ReportExportResponse(BaseModel):
    """``GET /api/reports/{id}/export``：签好的下载链接。"""

    filename: str
    url: str
    size: int = 0
