"""配置加载。"""

from __future__ import annotations

import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIGS_DIR = PROJECT_ROOT / "configs"


PROFILES_DIR = CONFIGS_DIR / "profiles"


def _default_profile() -> str:
    """configs/agent.yaml 的 ``default_profile``：未设 AGENT_PROFILE 时用它。"""
    path = CONFIGS_DIR / "agent.yaml"
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ""
    return str(raw.get("default_profile") or "").strip()


def active_profile() -> str:
    """当前部署形态。

    取值顺序：进程环境变量 ``AGENT_PROFILE`` > ``.env`` 里的 ``AGENT_PROFILE`` >
    ``configs/agent.yaml`` 的 ``default_profile``。**不读** ``.env.<profile>``——
    那份文件正是由它选出来的。
    """
    value = os.environ.get("AGENT_PROFILE")
    if value is None:
        env_path = PROJECT_ROOT / ".env"
        if env_path.is_file():
            try:
                from dotenv import dotenv_values

                value = dotenv_values(env_path, encoding="utf-8").get("AGENT_PROFILE")
            except Exception:  # noqa: BLE001
                value = None
    value = (value or "").strip()
    return value or _default_profile()


def profile_dir(profile: str | None = None) -> Path | None:
    """形态目录 ``configs/profiles/<name>/``；不存在返回 None。"""
    name = (profile if profile is not None else active_profile()).strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    path = PROFILES_DIR / name
    return path if path.is_dir() else None


def env_files() -> tuple[Path, ...]:
    """按优先级从低到高排列的 env 文件：``.env`` 放公共项，``.env.<profile>``
    放实例项（企微机器人、端口、偏好目录）并覆盖前者。

    这样切换形态只是换一个 AGENT_PROFILE，不再改写任何文件。
    """
    files = [PROJECT_ROOT / ".env"]
    profile = active_profile()
    if profile:
        files.append(PROJECT_ROOT / f".env.{profile}")
    return tuple(files)


class Settings(BaseSettings):
    """应用运行时配置，优先读取环境变量。"""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    llm_api_key: str = Field(default="", alias="LLM_API_KEY")
    llm_base_url: str = Field(default="https://api.deepseek.com/v1", alias="LLM_BASE_URL")
    llm_model: str = Field(default="deepseek-chat", alias="LLM_MODEL")
    # 视觉多模态大模型配置；留空时默认复用 llm_model / llm_base_url / llm_api_key
    llm_vision_model: str = Field(default="", alias="LLM_VISION_MODEL")
    llm_vision_base_url: str = Field(default="", alias="LLM_VISION_BASE_URL")
    llm_vision_api_key: str = Field(default="", alias="LLM_VISION_API_KEY")
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 2
    llm_temperature: float = Field(default=0.2, alias="LLM_TEMPERATURE")
    llm_json_mode: bool = Field(default=True, alias="LLM_JSON_MODE")
    # 单次补全最大输出 token（含思考）。不传时方舟 Chat Completions 默认 4096，
    # 长终答会被腰斩。128K = 131072；0 表示不带这个字段，走对端默认。
    llm_max_tokens: int = Field(default=131072, alias="LLM_MAX_TOKENS")
    # 是否把每轮 LLM 请求/响应完整 body 写入文件（体积大；不打控制台）。
    # None（默认）= 跟随 LOG_LEVEL：仅 DEBUG 开启。显式设 true/false 则强制覆盖。
    llm_log_bodies: bool | None = Field(default=None, alias="LLM_LOG_BODIES")
    # 相对项目根或绝对路径；默认 data/llm_bodies/
    llm_log_bodies_dir: str = Field(default="data/llm_bodies", alias="LLM_LOG_BODIES_DIR")

    app_host: str = Field(default="0.0.0.0", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    # 花生壳等 HTTPS 反代的对外根，例如 https://xxx.vicp.fun ；留空则下载链接仍为相对路径。
    public_base_url: str = Field(default="", alias="PUBLIC_BASE_URL")

    # ---------------------------------------------------------------- 日志
    # 唯一主开关，含义对齐 logback 的 root level：
    #   ERROR 生产 / INFO 测试 / DEBUG 排查
    # 它同时决定三类产物是否落盘，见 _resolve_log_sinks()。
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: str = Field(default="console", alias="LOG_FORMAT")  # console | json
    # 运行日志流水只打 stdout，不落盘（全量镜像 app.log 已移除）。
    # 只有 ERROR 单独留一份 data/logs/error.log —— 恒定 ERROR 级、不跟随
    # LOG_LEVEL，因此任何级别下都很小。它是唯一长期留存的故障轨迹：渠道掉线、
    # 启动失败、LLM 限流都不进 data/audit/。
    error_log_enabled: bool = Field(default=True, alias="ERROR_LOG_ENABLED")
    error_log_dir: str = Field(default="data/logs", alias="ERROR_LOG_DIR")
    error_log_max_bytes: int = Field(default=10 * 1024 * 1024, alias="ERROR_LOG_MAX_BYTES")
    error_log_backup_count: int = Field(default=5, alias="ERROR_LOG_BACKUP_COUNT")
    # Graylog GELF TCP。与 product-pc-bridge 一致，只推 ERROR。
    # 本地不想推时在 .env 设 GRAYLOG_ENABLED=false。
    graylog_enabled: bool = Field(default=True, alias="GRAYLOG_ENABLED")
    graylog_host: str = Field(default="10.33.30.160", alias="GRAYLOG_HOST")
    graylog_port: int = Field(default=12201, alias="GRAYLOG_PORT")
    graylog_facility: str = Field(default="ld308_csagent", alias="GRAYLOG_FACILITY")

    max_history_messages: int = 200
    max_sessions: int = Field(default=2000, alias="MAX_SESSIONS")
    show_thinking_process: bool = Field(default=False, alias="SHOW_THINKING_PROCESS")

    # 会话记忆注入（窗口外走 LLM 滚动摘要，窗口内保留原文）
    history_window: int = Field(default=12, alias="HISTORY_WINDOW")
    history_summary_enabled: bool = Field(default=True, alias="HISTORY_SUMMARY_ENABLED")
    history_summary_max_chars: int = Field(
        default=16000, alias="HISTORY_SUMMARY_MAX_CHARS"
    )
    summary_max_tokens: int = Field(default=4000, alias="SUMMARY_MAX_TOKENS")
    # 最近窗口内「非末条」消息的角色基准上限；末条始终完整保留。
    # 实际上限取本值与 Tier1 预算均分份额的较大者，窗口有余量时自动放宽。
    recent_user_max_tokens: int = Field(default=400, alias="RECENT_USER_MAX_TOKENS")
    recent_assistant_max_tokens: int = Field(
        default=600, alias="RECENT_ASSISTANT_MAX_TOKENS"
    )
    compact_threshold: float = Field(default=0.80, alias="COMPACT_THRESHOLD")
    # 0 表示按模型名自动推断上下文窗口
    model_context_limit: int = Field(default=0, alias="MODEL_CONTEXT_LIMIT")
    # 自动推断窗口的封顶值（有效窗口）；0 = 不封顶。显式 model_context_limit 不受限。
    model_context_cap: int = Field(default=200000, alias="MODEL_CONTEXT_CAP")
    response_reserve_ratio: float = Field(default=0.10, alias="RESPONSE_RESERVE_RATIO")
    # 最近工具调用摘要的注入预算（固定配额，在分层之前先扣）。
    # M0.2 之前它从已删除的领域快照预算派生，被永久钉死在 80 token。
    tool_history_tokens: int = Field(default=600, alias="TOOL_HISTORY_TOKENS")

    # 写入短期记忆时单条消息的字符上限，防止一条超长回复把会话 key 撑爆。
    # 取值须 >= 读取侧 CompactorConfig.recent_latest_max_tokens（8000 token）能吃下的量，
    # 否则会真的截掉模型本来读得到的内容。中文约 1 字 >= 1 token，8000 字符即已覆盖；
    # 英文占比高的流量（约 4 字符/token）需上调到 32000 才能完全无损。
    max_message_chars: int = Field(default=8000, alias="MAX_MESSAGE_CHARS")

    # 对话记录（SQLite）：只给界面翻历史用，不进模型上下文。
    # 与上面的 max_history_messages 是两个独立旋钮，改这里不影响模型记得多少。
    transcript_enabled: bool = Field(default=True, alias="TRANSCRIPT_ENABLED")
    transcript_db_path: str = Field(
        default="data/transcripts.db", alias="TRANSCRIPT_DB_PATH"
    )
    # 单次回填给前端的最大条数
    transcript_max_return: int = Field(default=100, alias="TRANSCRIPT_MAX_RETURN")
    # 启动时清理多少天前的记录；<=0 表示不清理。
    # 默认 0：合并存储后删记录等于删模型的记忆源，膨胀改由召回窗口治理。
    # 详见 configs/base.yaml 的 transcript.retention_days 注释。
    transcript_retention_days: float = Field(
        default=0.0, alias="TRANSCRIPT_RETENTION_DAYS"
    )

    # sqlite（落盘权威存储，默认）| memory（进程内，重启失忆）| redis（跨进程 TTL 缓存）
    memory_backend: str = Field(default="sqlite", alias="MEMORY_BACKEND")
    # 统一记忆库：模型记忆 + 界面记录 + 工具流水同库。相对路径相对项目根。
    memory_db_path: str = Field(default="data/csagent.db", alias="MEMORY_DB_PATH")
    # 静默多久算一段新的时间线（episode）。用户侧无感，没有「新建会话」按钮；
    # 它给 Checkpoint 和将来的召回提供边界。12 小时 = 「隔天来问就是新话题」。
    episode_gap_hours: float = Field(default=12.0, alias="EPISODE_GAP_HOURS")
    # 跨 episode 召回默认时间窗（天）。0 = 不截时间。问句带明确时间指向时会放开。
    recall_window_days: float = Field(default=180.0, alias="RECALL_WINDOW_DAYS")
    # 组装后上下文 / 模型窗口 超过该比例打 context_token_high。0 = 关闭告警。
    context_token_high_ratio: float = Field(
        default=0.50, alias="CONTEXT_TOKEN_HIGH_RATIO"
    )
    # tool_result 外置文件保留期（天）。0 = 不删。运行时尚未按该值清理，见设计 §9。
    tool_result_retention_days: float = Field(
        default=0.0, alias="TOOL_RESULT_RETENTION_DAYS"
    )
    # L1 单轮摘要预算；L2 每这么多条 L1 合并一份 Checkpoint。
    l1_max_tokens: int = Field(default=200, alias="L1_MAX_TOKENS")
    l2_merge_every: int = Field(default=8, alias="L2_MERGE_EVERY")
    # 同时在跑的请求上限（过载保护，不是排他锁）。满员时新请求先收到排队提示再等名额。
    # 一个请求 = 一整轮 ReAct，可能十几次 LLM 往返加若干工具调用，8 路已经能把
    # 上游模型服务压得不轻。<= 0 表示不限流。
    max_concurrent_requests: int = Field(default=8, alias="MAX_CONCURRENT_REQUESTS")
    redis_url: str = Field(default="", alias="REDIS_URL")
    memory_ttl_seconds: int = Field(default=86400, alias="MEMORY_TTL_SECONDS")
    # 启动时探测 Redis 的 maxmemory / maxmemory-policy，未设上限或 noeviction 时告警。
    # 关掉它只应该是因为托管 Redis 禁用了 CONFIG 命令（那种情况本就会静默跳过）。
    redis_memory_policy_check: bool = Field(
        default=True, alias="REDIS_MEMORY_POLICY_CHECK"
    )


    # 单个 Agent ReAct 循环整体墙钟超时（秒）
    tool_timeout_seconds: float = Field(
        default=180.0, alias="TOOL_TIMEOUT_SECONDS"
    )
    # 软截止：到点后不再开新一轮，用已有结果强制收口（见 react/loop.py）
    react_overall_timeout_seconds: float = Field(
        default=1500.0, alias="REACT_OVERALL_TIMEOUT_SECONDS"
    )
    # 会话层整体墙钟超时（秒）；单 Agent 模式下通常与 react 超时接近
    orchestrator_overall_timeout_seconds: float = Field(
        default=600.0, alias="ORCHESTRATOR_OVERALL_TIMEOUT_SECONDS"
    )
    # M1.2：同一轮内独立工具调用的最大并行数；1 表示退化为顺序执行
    react_max_parallel_tool_calls: int = Field(
        default=5, alias="REACT_MAX_PARALLEL_TOOL_CALLS"
    )
    # 单次 ReAct 循环最大轮数 / 最大工具调用次数
    react_max_rounds: int = Field(default=48, alias="REACT_MAX_ROUNDS")
    react_max_tool_calls: int = Field(default=80, alias="REACT_MAX_TOOL_CALLS")
    # 单条工具结果写入 LLM 上下文的最大字符数（仅影响 prompt 展示，不影响工具实际执行）
    react_max_tool_result_chars: int = Field(
        default=5000, alias="REACT_MAX_TOOL_RESULT_CHARS"
    )

    # 企微智能机器人长连接（官方 wecom-aibot-python-sdk）
    wecom_enabled: bool = Field(default=False, alias="WECOM_ENABLED")
    wecom_bot_id: str = Field(default="", alias="WECOM_BOT_ID")
    wecom_secret: str = Field(default="", alias="WECOM_SECRET")
    wecom_welcome: str = Field(
        default="您好，我是通用助手。可协助文件操作、脚本执行、检索与表格处理。",
        alias="WECOM_WELCOME",
    )
    wecom_stream_interval_ms: int = Field(
        default=200, alias="WECOM_STREAM_INTERVAL_MS"
    )
    # true：把每条入站原始帧写到 data/wecom_frames/（排查群聊 quote / 文件结构用）
    wecom_frame_dump: bool = Field(default=False, alias="WECOM_FRAME_DUMP")
    # true：终答末尾拼接「【本轮共耗时】：N秒」（网页 timing-badge 同口径）。
    # 对客默认关；对照网页耗时或排查变慢时再开。
    wecom_append_round_elapsed: bool = Field(
        default=False, alias="WECOM_APPEND_ROUND_ELAPSED"
    )

    # 微信群通道：发送机（wecom-sender）读群里 @我 的消息，主动连本服务推消息、取回复。
    # 发送机 uplink.token 填同一个；留空则不启用
    wechat_link_token: str = Field(default="", alias="WECHAT_LINK_TOKEN")

    # 会话审计摘要（谁问了什么、我们答了什么）属于业务留痕，不是「日志级别」的事，
    # 生产也要留，故默认常开、不跟随 LOG_LEVEL。
    audit_log_enabled: bool = Field(default=True, alias="AUDIT_LOG_ENABLED")
    audit_log_dir: str = Field(default="", alias="AUDIT_LOG_DIR")

    # 聊天界面（static/index.html）是否要求先填客户号才允许发送

    # 图片附件：multimodal | ocr | stub
    attachment_image_mode: str = Field(
        default="multimodal", alias="ATTACHMENT_IMAGE_MODE"
    )

    # 会话临时工作区清理（uploads + scripts/tools）
    # 默认 0：与 transcript.retention_days 同进同退，永不自动删除。
    workspace_cleanup_ttl_days: float = Field(
        default=0.0, alias="WORKSPACE_CLEANUP_TTL_DAYS"
    )
    workspace_cleanup_on_startup: bool = Field(
        default=True, alias="WORKSPACE_CLEANUP_ON_STARTUP"
    )
    # 读类工具额外放行的项目路径前缀，逗号分隔（如 "docs/,configs/"）；默认空 = 不放行。
    # 见 channels/workspace_uploads.py 的 configure_read_escape_prefixes。
    workspace_read_escape_prefixes: str = Field(
        default="", alias="WORKSPACE_READ_ESCAPE_PREFIXES"
    )

    # 回复风格偏好存储目录；留空则落 data/reply_preference
    reply_preference_dir: str = Field(default="", alias="REPLY_PREFERENCE_DIR")

    # ------------------------------------------------------- 宿主接入鉴权
    # 与宿主服务端共享的长密钥，用于 HMAC-SHA256 验签（见 infra/auth.py）。
    # 只存在于两边的服务端，绝不下发浏览器。
    agent_shared_secret: str = Field(default="", alias="AGENT_SHARED_SECRET")
    # 关掉即所有接口免鉴权，**只允许本地开发**。生产必须为 true，
    # 且 create_app() 启动时会校验密钥强度，配错直接启动失败。
    agent_auth_enabled: bool = Field(default=True, alias="AGENT_AUTH_ENABLED")
    # 令牌有效期（秒）。宿主签发时应采用同一口径；小窗靠 refreshAuth() 续期。
    agent_token_ttl_seconds: int = Field(default=1800, alias="AGENT_TOKEN_TTL_SECONDS")
    # 开启后暴露 POST /api/auth/dev-token，可为任意 uid 换取令牌。
    # 这是给联调用的后门，生产必须为 false。
    agent_auth_dev_login: bool = Field(default=False, alias="AGENT_AUTH_DEV_LOGIN")
    # 下载链接自带授权的有效期。企微里用户可能隔很久才点，故独立于身份令牌、给得长些。
    agent_download_grant_ttl_seconds: int = Field(
        default=86400, alias="AGENT_DOWNLOAD_GRANT_TTL_SECONDS"
    )

    # ------------------------------------------------------- 管理后台
    # 管理员账号，逗号分隔的「用户名:口令哈希」。哈希用
    #   python -m csagent.admin.passwd <用户名>
    # 生成（PBKDF2-SHA256）。留空 = 管理后台不可登录。
    admin_accounts: str = Field(default="", alias="ADMIN_ACCOUNTS")
    # 管理后台令牌有效期（秒）
    admin_token_ttl_seconds: int = Field(default=7200, alias="ADMIN_TOKEN_TTL_SECONDS")
    # 管理令牌（typ=adm）专用签名密钥，>= 32 字符，**只存在于本服务、不给宿主**，
    # 且不能与 AGENT_SHARED_SECRET 相同。以前管理令牌与宿主身份令牌共用一把密钥，
    # 持有它的宿主后端就能给任意已知管理员用户名签出管理令牌。未配置 = 管理后台不可登录。
    admin_token_secret: str = Field(default="", alias="ADMIN_TOKEN_SECRET")

    # 部署形态：research（默认）| product。决定加载哪份 profiles overlay。
    # 替代原来的「文件改名切产品」方式。
    # 仅供展示；实际形态以 active_profile() 为准（含 configs/agent.yaml 的默认值）
    agent_profile: str = Field(default="", alias="AGENT_PROFILE")

def _load_yaml_config() -> dict:
    path = CONFIGS_DIR / "base.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _csv(value: object) -> str:
    """YAML 列表 / 字符串 → 逗号分隔字符串（与同名环境变量的写法一致）。"""
    if isinstance(value, (list, tuple)):
        return ",".join(str(item).strip() for item in value if str(item).strip())
    return str(value)


# LOG_LEVEL → 三类落盘产物是否开启（对齐 logback：一个 root level 带动所有 appender）
_DEBUG_ONLY_SINKS = ("llm_log_bodies",)


def _resolve_log_sinks(settings: Settings) -> None:
    """把「跟随级别」的日志产物解析成确定的布尔值。

    值为 None 表示没人显式指定过，跟随 LOG_LEVEL；显式给了 true/false 就是强制
    覆盖（等价于 logback 里给某个 appender 单独配 level），用于生产临时开调试。
    """
    debug_on = settings.log_level.strip().upper() == "DEBUG"
    for name in _DEBUG_ONLY_SINKS:
        if getattr(settings, name) is None:
            setattr(settings, name, debug_on)


@lru_cache
def get_settings() -> Settings:
    """加载并缓存应用配置。

    优先级：环境变量 / .env  >  configs/base.yaml 默认值。
    环境变量已显式设定的字段，YAML 不再覆盖。
    """
    yaml_cfg = _load_yaml_config()
    settings = Settings(_env_file=env_files())
    env_set = set(settings.model_fields_set)

    # ---- 声明式映射表：(YAML 路径, Settings 属性名, 类型转换) ----
    # 新增配置项只需在此加一行，不用写 if/setattr 样板代码。
    _YAML_MAP: list[tuple[tuple[str, ...], str, Callable[[Any], Any]]] = [
        # llm
        (("llm", "timeout_seconds"),       "llm_timeout_seconds",       int),
        (("llm", "max_retries"),            "llm_max_retries",           int),
        (("llm", "max_tokens"),             "llm_max_tokens",            int),
        (("llm", "log_bodies"),             "llm_log_bodies",            bool),
        (("llm", "log_bodies_dir"),         "llm_log_bodies_dir",        str),
        # chat / memory
        (("chat", "max_history_messages"),   "max_history_messages",      int),
        (("chat", "show_thinking_process"),  "show_thinking_process",     bool),
        (("chat", "history_window"),         "history_window",            int),
        (("chat", "history_summary_enabled"),"history_summary_enabled",   bool),
        (("chat", "history_summary_max_chars"),"history_summary_max_chars",int),
        (("chat", "summary_max_tokens"),     "summary_max_tokens",        int),
        (("chat", "recent_user_max_tokens"), "recent_user_max_tokens",    int),
        (("chat", "recent_assistant_max_tokens"),"recent_assistant_max_tokens",int),
        (("chat", "compact_threshold"),      "compact_threshold",         float),
        (("chat", "model_context_limit"),    "model_context_limit",       int),
        (("chat", "model_context_cap"),      "model_context_cap",         int),
        (("chat", "response_reserve_ratio"), "response_reserve_ratio",    float),
        (("chat", "tool_history_tokens"),    "tool_history_tokens",       int),
        (("chat", "max_message_chars"),      "max_message_chars",         int),
        (("chat", "memory_backend"),         "memory_backend",            str),
        (("chat", "memory_db_path"),         "memory_db_path",            str),
        (("chat", "episode_gap_hours"),      "episode_gap_hours",         float),
        (("chat", "recall_window_days"),     "recall_window_days",        float),
        (("chat", "context_token_high_ratio"),"context_token_high_ratio", float),
        (("chat", "tool_result_retention_days"),"tool_result_retention_days",float),
        (("chat", "l1_max_tokens"),          "l1_max_tokens",             int),
        (("chat", "l2_merge_every"),         "l2_merge_every",            int),
        (("chat", "max_concurrent_requests"),"max_concurrent_requests",   int),
        (("chat", "redis_url"),              "redis_url",                 str),
        (("chat", "memory_ttl_seconds"),     "memory_ttl_seconds",       int),
        # transcript
        (("transcript", "enabled"),          "transcript_enabled",        bool),
        (("transcript", "retention_days"),   "transcript_retention_days", float),
        (("transcript", "max_return"),       "transcript_max_return",     int),
        # audit
        (("audit", "enabled"),               "audit_log_enabled",         bool),
        (("audit", "dir"),                   "audit_log_dir",             str),
        # logging (ERROR 日志落盘，从 .env 迁移到 base.yaml)
        (("logging", "error_log_enabled"),   "error_log_enabled",         bool),
        (("logging", "error_log_dir"),       "error_log_dir",             str),
        (("logging", "error_log_max_bytes"), "error_log_max_bytes",       int),
        (("logging", "error_log_backup_count"),"error_log_backup_count",  int),
        (("graylog", "enabled"),             "graylog_enabled",           bool),
        (("graylog", "host"),                "graylog_host",              str),
        (("graylog", "port"),                "graylog_port",              int),
        (("graylog", "facility"),            "graylog_facility",          str),
        # react
        (("react", "max_parallel_tool_calls"),"react_max_parallel_tool_calls",int),
        (("react", "max_rounds"),            "react_max_rounds",          int),
        (("react", "max_tool_calls"),        "react_max_tool_calls",      int),
        (("react", "max_tool_result_chars"), "react_max_tool_result_chars",int),
        (("react", "overall_timeout_seconds"),"react_overall_timeout_seconds",float),
        (("react", "tool_timeout_seconds"),  "tool_timeout_seconds",      float),
        # attachment
        (("attachment", "image_mode"),       "attachment_image_mode",     str),
        # workspace
        (("workspace", "cleanup_ttl_days"), "workspace_cleanup_ttl_days",float),
        (("workspace", "cleanup_on_startup"),"workspace_cleanup_on_startup",bool),
        (("workspace", "read_escape_prefixes"),"workspace_read_escape_prefixes",_csv),
    ]

    for path, attr, conv in _YAML_MAP:
        if attr in env_set:
            continue  # 环境变量优先，YAML 不覆盖
        node = yaml_cfg
        for key in path:
            node = (node or {}).get(key)
        if node is not None and node != "":
            try:
                setattr(settings, attr, conv(node))
            except (ValueError, TypeError):
                pass  # 类型转换失败则保留代码默认值

    # 放在最后：此时 env 与 YAML 都已合并，未显式指定的日志产物才按 LOG_LEVEL 定档
    _resolve_log_sinks(settings)
    return settings

