"""FastAPI 应用入口（单 Agent + 全工具）。

权威配置：
  - configs/agent.yaml       — 唯一 Agent 挂哪些 tool_packs
  - configs/tool_packs.yaml  — 工具包定义
入站统一：MessageGateway（HTTP SSE + 企微）
"""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from csagent.agent import Agent
from csagent.audit.session_logger import SessionAuditLogger, resolve_audit_dir
from csagent.channels.file_handler import FileHandler
from csagent.channels.manager import ChannelManager
from csagent.channels.message_gateway import MessageGateway
from csagent.channels.workspace_uploads import configure_read_escape_prefixes
from csagent.common.secrets import get_secret_masker
from csagent.guardrail.config_loader import load_guardrail_config
from csagent.guardrail.input_guard import InputGuard
from csagent.guardrail.output_guard import OutputGuard
from csagent.infra.agents_config import load_resident_skills, load_tool_pack_names
from csagent.infra.auth import (
    MIN_SECRET_LENGTH,
    AuthError,
    resolve_admin_secret,
    resolve_secret,
)
from csagent.infra.config import PROJECT_ROOT, get_settings
from csagent.infra.llm_client import LlmClient
from csagent.infra.logging import configure_logging
from csagent.memory import (
    CompactorConfig,
    ContextBudgetAllocator,
    ContextBudgetConfig,
    HistoryCompactionPolicy,
    PromptLayoutOptimizer,
    QueryAwareContextSelector,
    StructuredCompactor,
    TokenCounter,
    format_tool_digest,
)
from csagent.memory.artifact_store import ArtifactStore, set_artifact_store
from csagent.memory.factory import build_memory_components
from csagent.react.loop import ReactLoop
from csagent.tools.approvals import ApprovalStore
from csagent.tools.pack_loader import ToolPackLoader
from csagent.tools.policy import ToolPolicy
from csagent.tools.packs.reply_preference_pack import ReplyPreferenceToolPack
from csagent.tools.packs.skill_pack import SkillToolPack
from csagent.tools.registry import ToolRegistry
from csagent.tools.skill_paths import set_skill_discovery_getter
from csagent.tools.skill_tools import build_skill_tools

logger = structlog.get_logger(__name__)

STATIC_DIR = Path(__file__).resolve().parents[2] / "static"
# 对外挂载前缀。nginx 上配的是 `location /csagent`，但它**不剥前缀**转发过来，
# 后端收到的是 /csagent/、/csagent/api/chat/stream。PrefixStripper 在 ASGI 入口
# 把它剥掉，所以 nginx 剥不剥都能跑（剥了就是普通 / 路径，压根走不到剥离分支）。
#
# 前端不能写死这个前缀：index.html 用相对路径（assets/... 、api/...），页面在
# /csagent/ 下就自动落到 /csagent/assets/...，在 / 下就落到 /assets/...。
# 相对路径成立的前提是**页面 URL 必须以斜杠结尾**，所以 /csagent 会 307 到 /csagent/。
URL_PREFIX = "/csagent"
AGENT_NAME = "agent"


def _chat_asset_version() -> str:
    """``static/assets/chat/`` 下文件的内容摘要（前 10 位），给 index.html 的 ?v= 用。

    每次请求现算：文件只有几十 KB，省掉缓存就不用管热更新时的失效。
    """
    digest = hashlib.sha1()
    chat_dir = STATIC_DIR / "assets" / "chat"
    for path in sorted(chat_dir.glob("*")) if chat_dir.is_dir() else []:
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def _load_persistent_instructions() -> str:
    """加载运营可编辑的项目规则与持久指令（configs/rules.md，类 CLAUDE.md）。"""
    path = PROJECT_ROOT / "configs" / "rules.md"
    if not path.is_file():
        for legacy_name in ("AGENT.md", "persistent_instructions.md"):
            legacy = PROJECT_ROOT / "configs" / legacy_name
            if legacy.is_file():
                path = legacy
                break
        else:
            return ""
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""




async def _build_app_components(settings):
    """装配：工具包 -> Agent -> MessageGateway。"""
    # ---------- 基础设施 ----------
    llm_client = LlmClient(settings)

    # ---------- 读路径放行前缀（默认空：源码 / 配置 / 审计对模型不可见）----------
    configure_read_escape_prefixes(
        [p for p in settings.workspace_read_escape_prefixes.split(",") if p.strip()]
    )

    # ---------- 工具包（configs/agent.yaml 的唯一 tool_packs 列表）----------
    tool_pack_loader = ToolPackLoader()
    pack_names = list(load_tool_pack_names())
    await tool_pack_loader.prepare_packs(pack_names)
    tools = tool_pack_loader.load_tools(pack_names)

    # ---------- SkillDiscovery（从 SkillToolPack 获取）----------
    skill_discovery = None
    skill_pack = tool_pack_loader.get_pack("skills")
    if isinstance(skill_pack, SkillToolPack):
        skill_discovery = skill_pack.get_discovery()
        if skill_discovery is not None:
            skill_discovery.scan()
            set_skill_discovery_getter(lambda: skill_discovery)
            logger.info("skill_discovery_ready",
                        count=len(skill_discovery.scan()))

    # 常驻 Skill 写错名字 / 不在本形态白名单里，系统提示词里就不会有它——启动时说出来，
    # 而不是让模型每问都去 load_skill 一个以为常驻了的 Skill
    resident_skills = load_resident_skills()
    for name in resident_skills:
        pkg = skill_discovery.get(name) if skill_discovery is not None else None
        if pkg is None or not pkg.enabled:
            logger.error("resident_skill_unavailable", skill=name, hint="检查 profile.yaml 的 skills 白名单")

    # ---------- Skill 在 tools.yaml 里声明的原生工具（须在策略与注册中心之前）----------
    tools.extend(build_skill_tools(
        skill_discovery,
        tool_pack_loader.get_loaded_packs(),
        taken={tool.name for tool in tools},
    ))

    # 工具结果进模型上下文前先遮掉已知密钥值：模型看不到，就不会复述，
    # 值也不会顺着上下文进 LLM 服务商、会话审计与记忆库
    secret_masker = get_secret_masker()
    # 策略与确认卡片：没有 policy.yaml 时恒放行，行为与改造前一致。
    # 卡片只活在本进程里，重启即失效。
    tool_policy = ToolPolicy.from_file()
    unclassified = tool_policy.unclassified([tool.name for tool in tools])
    if unclassified:
        # ERROR 而不是 WARNING：生产 LOG_LEVEL=ERROR。没归类的工具按 default_risk 处理，
        # 按风险写的规则管不到它们（新接的 MCP 服务忘了配就是这种情况）
        logger.error(
            "tool_policy_unclassified_tools",
            tools=unclassified,
            hint="在 configs/policy.yaml 的 tool_risks 里为它们配置风险等级（可用「前缀*」）",
        )
    tool_registry = ToolRegistry.from_tools_and_agent(
        tools,
        AGENT_NAME,
        default_timeout_seconds=float(settings.tool_timeout_seconds),
        result_filter=secret_masker.mask if secret_masker.count else None,
        policy=tool_policy,
        approvals=ApprovalStore(),
    )

    # ---------- 回复偏好 store（从 ReplyPreferenceToolPack 获取，未挂载则为 None）----------
    reply_preference_store = None
    if "reply_preference" in pack_names:
        reply_preference_pack = tool_pack_loader.get_pack("reply_preference")
        if isinstance(reply_preference_pack, ReplyPreferenceToolPack):
            reply_preference_store = reply_preference_pack.get_store()

    # ---------- Token 与预算控制基础设施 ----------
    ctx_limit = int(getattr(settings, "model_context_limit", 0) or 0) or None
    token_counter = TokenCounter(
        model_name=settings.llm_model,
        context_limit=ctx_limit,
        context_cap=int(getattr(settings, "model_context_cap", 0) or 0) or None,
    )
    budget_cfg = ContextBudgetConfig(
        compact_threshold=float(settings.compact_threshold),
        response_reserve_ratio=float(getattr(settings, "response_reserve_ratio", 0.10) or 0.10),
        tool_history_tokens=int(getattr(settings, "tool_history_tokens", 600) or 600),
    )
    budget_allocator = ContextBudgetAllocator(token_counter, budget_cfg)
    context_selector = QueryAwareContextSelector(token_counter)
    prompt_layout = PromptLayoutOptimizer(token_counter)
    # 历史压缩策略：整个进程只此一份，Agent 层（循环外 LLM 摘要）与 ReactLoop
    # （循环内水位兜底）共用，避免两条链路各配一套阈值（见问题 E）。
    compaction_policy = HistoryCompactionPolicy(
        token_counter,
        compact_threshold=float(settings.compact_threshold),
    )

    react_loop = ReactLoop(
        llm_client=llm_client,
        tool_registry=tool_registry,
        max_tool_calls=settings.react_max_tool_calls,
        overall_timeout_seconds=float(
            getattr(settings, "react_overall_timeout_seconds", 180) or 180
        ),
        max_tool_result_chars=int(settings.react_max_tool_result_chars),
        # M1.2：同轮独立业务工具调用并行执行
        max_parallel_tool_calls=int(settings.react_max_parallel_tool_calls or 1),
        token_counter=token_counter,
        prompt_layout=prompt_layout,
        compaction_policy=compaction_policy,
    )

    # ---------- 记忆 / 护栏 / 审计 ----------
    short_term_memory, session_snapshot_store, unified_store = build_memory_components(
        settings, token_counter=token_counter, llm_client=llm_client
    )

    # 对话记录：给前端翻历史用。
    #
    # sqlite 后端下 unified_store 就是 short_term 本身（同一批行，两种读取口径），
    # 直接复用；其余后端（memory / redis 不落盘）回落到独立的 transcripts.db，
    # 否则关掉界面历史等于把唯一的落盘记录也关掉了。
    # 产物清单（M1c）：与记忆同库，登记会话收发过的文件，供事后检索与清理。
    # 注意这和 channels/outbound_files 的出站队列是两件事——那个是单轮内的交接，
    # 这个是永久清单。登记点在工具包与 HTTP 路由里，拿不到 app.state，故走模块级注入。
    if unified_store is not None:
        set_artifact_store(ArtifactStore(unified_store.path))
        logger.info("artifact_store_ready", path=str(unified_store.path))
    else:
        set_artifact_store(None)

    transcript_store = None
    if settings.transcript_enabled:
        if unified_store is not None:
            transcript_store = unified_store
            logger.info("transcript_store_ready", path=str(unified_store.path), unified=True)
        else:
            from csagent.memory.transcript import SqliteTranscriptStore

            db_path = Path(settings.transcript_db_path)
            if not db_path.is_absolute():
                db_path = PROJECT_ROOT / db_path
            transcript_store = SqliteTranscriptStore(
                db_path,
                max_message_chars=settings.max_message_chars,
                max_return=settings.transcript_max_return,
            )
            logger.info("transcript_store_ready", path=str(db_path), unified=False)

    # 结构化压缩器缓存后端
    compactor_cache = None
    if (settings.memory_backend or "").strip().lower() == "redis" and settings.redis_url:
        from csagent.memory.compactor_cache import RedisCompactorCache
        compactor_cache = RedisCompactorCache(
            settings.redis_url, ttl_seconds=settings.memory_ttl_seconds
        )
    else:
        from csagent.memory.compactor_cache import InMemoryCompactorCache
        compactor_cache = InMemoryCompactorCache(max_sessions=settings.max_sessions)

    compactor_config = CompactorConfig(
        window=settings.history_window,
        summary_max_tokens=settings.summary_max_tokens,
        recent_user_max_tokens=settings.recent_user_max_tokens,
        recent_assistant_max_tokens=settings.recent_assistant_max_tokens,
    )
    structured_compactor = StructuredCompactor(
        llm_client=llm_client,
        token_counter=token_counter,
        config=compactor_config,
        cache_backend=compactor_cache,
        enabled=settings.history_summary_enabled,
        # 让 Checkpoint 能写「已完成操作」：工具调用存在 snapshot store 里，
        # 而压缩器的对话输入只有 user/assistant 文本，这里把两者接起来。
        tool_digest_provider=lambda sid: format_tool_digest(session_snapshot_store, sid),
    )

    # M3.3 / M3.4：分级记忆与跨段召回只挂在 sqlite 权威存储上。
    graded_memory = None
    recaller = None
    if unified_store is not None:
        from csagent.memory.graded import GradedMemory
        from csagent.memory.recall import EpisodeRecaller

        graded_memory = GradedMemory(
            unified_store,
            llm_client=llm_client if settings.history_summary_enabled else None,
            token_counter=token_counter,
            l1_max_tokens=int(getattr(settings, "l1_max_tokens", 200) or 200),
            l2_merge_every=int(getattr(settings, "l2_merge_every", 8) or 8),
            l2_max_tokens=int(settings.summary_max_tokens or 4000),
            enabled=settings.history_summary_enabled,
        )
        recaller = EpisodeRecaller(
            unified_store.path,
            token_counter=token_counter,
            window_days=float(getattr(settings, "recall_window_days", 180.0) or 180.0),
        )

    # M2.1：配置驱动的输入/输出护栏规则（configs/guardrail.yaml，改配置即生效）
    guardrail_cfg = load_guardrail_config()
    if guardrail_cfg.get("enabled", True):
        input_guard = InputGuard.from_config(guardrail_cfg.get("input_rules"))
        output_guard = OutputGuard.from_config(guardrail_cfg.get("output_rules"))
    else:
        input_guard = InputGuard()
        output_guard = OutputGuard()

    agent = Agent(
        react_loop=react_loop,
        tool_registry=tool_registry,
        short_term_memory=short_term_memory,
        input_guard=input_guard,
        output_guard=output_guard,
        show_thinking_process=settings.show_thinking_process,
        max_rounds=settings.react_max_rounds,
        audit_logger=SessionAuditLogger(
            # 与安全审计（audit/security_logger.py）同一个解析口径，两份留痕落在一处
            audit_dir=resolve_audit_dir(settings.audit_log_dir),
            enabled=settings.audit_log_enabled,
        ),
        session_snapshot_store=session_snapshot_store,
        transcript_store=transcript_store,
        history_compactor=structured_compactor,
        history_window=settings.history_window,
        agent_name=AGENT_NAME,
        skill_discovery=skill_discovery,
        resident_skills=resident_skills,
        reply_preference_store=reply_preference_store,
        token_counter=token_counter,
        budget_allocator=budget_allocator,
        context_selector=context_selector,
        prompt_layout=prompt_layout,
        persistent_instructions=_load_persistent_instructions(),
        compaction_policy=compaction_policy,
        graded_memory=graded_memory,
        recaller=recaller,
        context_token_high_ratio=float(
            getattr(settings, "context_token_high_ratio", 0.50) or 0.0
        ),
    )

    logger.info("agent_created", tools=[t.name for t in tools])

    # 网关是进程内唯一实例（HTTP 与企微都从 app.state 取同一个），所以它持有的
    # 并发闸门就是全局闸门。别在别处再 new 一个 MessageGateway，那会绕开限流。
    message_gateway = MessageGateway(
        main_agent=agent,
        file_handler=FileHandler(image_mode=settings.attachment_image_mode),
        max_concurrent_requests=settings.max_concurrent_requests,
    )

    return {
        "llm_client": llm_client,
        "main_agent": agent,
        "skill_discovery": skill_discovery,
        "tool_pack_loader": tool_pack_loader,
        "message_gateway": message_gateway,
        "transcript_store": transcript_store,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(
        log_level=settings.log_level,
        log_format=settings.log_format,
        error_log_enabled=settings.error_log_enabled,
        error_log_dir=settings.error_log_dir,
        max_bytes=settings.error_log_max_bytes,
        backup_count=settings.error_log_backup_count,
        project_root=PROJECT_ROOT,
        graylog_enabled=settings.graylog_enabled,
        graylog_host=settings.graylog_host,
        graylog_port=settings.graylog_port,
        graylog_facility=settings.graylog_facility,
    )
    logger.info(
        "logging_configured",
        level=settings.log_level,
        stream="stdout",
        error_file=(
            f"{settings.error_log_dir}/error.log" if settings.error_log_enabled else "disabled"
        ),
        llm_bodies=settings.llm_log_bodies,
        audit_summary=settings.audit_log_enabled,
    )
    components = await _build_app_components(settings)

    llm_client = components["llm_client"]
    main_agent = components["main_agent"]
    tool_pack_loader = components["tool_pack_loader"]
    message_gateway = components["message_gateway"]

    app.state.main_agent = main_agent
    app.state.llm_client = llm_client
    app.state.message_gateway = message_gateway
    app.state.tool_pack_loader = tool_pack_loader
    app.state.skill_discovery = components.get("skill_discovery")
    app.state.transcript_store = components.get("transcript_store")
    app.state.wecom_channel = None
    app.state.channel_manager = None

    await llm_client.warmup()
    main_agent.start_audit_logger()

    transcript_store = components.get("transcript_store")
    if transcript_store is not None and settings.transcript_retention_days > 0:
        # 放启动时做而不是定时任务：单进程部署下没有调度器，而对话记录的清理
        # 精度到天即可，重启一次就够了。
        removed = await asyncio.to_thread(
            transcript_store.purge_older_than, settings.transcript_retention_days
        )
        logger.info(
            "transcript_purge_startup",
            retention_days=settings.transcript_retention_days,
            removed=removed,
        )

    if settings.workspace_cleanup_on_startup and settings.workspace_cleanup_ttl_days > 0:
        from csagent.channels.workspace_cleanup import cleanup_workspace

        report = await asyncio.to_thread(
            cleanup_workspace,
            ttl_days=settings.workspace_cleanup_ttl_days,
        )
        logger.info(
            "workspace_cleanup_startup",
            ttl_days=settings.workspace_cleanup_ttl_days,
            removed_dirs=len(report.removed_dirs),
            removed_files=len(report.removed_files),
            errors=len(report.errors),
        )

    channel_manager = ChannelManager(
        settings=settings,
        main_agent=main_agent,
        message_gateway=message_gateway,
        app=app,
    )
    await channel_manager.start_all()
    app.state.channel_manager = channel_manager
    app.state.wecom_channel = channel_manager.get("wecom")

    logger.info(
        "app_started",
        host=settings.app_host,
        port=settings.app_port,
        channels=list(channel_manager.channels.keys()),
    )
    yield
    await channel_manager.stop_all()
    await main_agent.stop_audit_logger()
    await tool_pack_loader.shutdown_all()
    await llm_client.close()
    logger.info("app_stopped")


class PrefixStripper:
    """把 nginx 未剥离的对外前缀在 ASGI 入口剥掉。

    ``location /csagent { proxy_pass http://127.0.0.1:8001; }``（无尾斜杠）会把
    完整路径原样转发，后端看到 /csagent/api/...，与内部路由 /api/... 对不上。
    正确的 nginx 写法是 ``proxy_pass http://127.0.0.1:8001/;``（**带尾斜杠**，
    自动剥前缀），但运维那边不一定愿意改，所以这里自己兜住。

    两种情况都能跑：
      - nginx 已剥前缀 → 路径本来就是 /api/...，不进剥离分支，原样放行；
      - nginx 未剥前缀 → 剥掉 /csagent，并把它记进 root_path，
        FastAPI 生成的 URL（如 openapi/docs）才不会丢前缀。

    裸前缀（/csagent，无尾斜杠）单独 307 到 /csagent/：index.html 里全是相对
    路径，浏览器地址栏若停在无斜杠的 /csagent，assets/x.js 会解析成 /assets/x.js，
    nginx 不转发那个路径，页面直接白屏。
    """

    def __init__(self, app, prefix: str) -> None:
        self._app = app
        self._prefix = prefix.rstrip("/")

    @property
    def app(self):
        """被包住的 FastAPI 实例。"""
        return self._app

    def __getattr__(self, name):
        """把 .state / .router 之类的属性透传给内层 app。

        create_app() 的返回值以前是 FastAPI，现在是本类；调用方（测试、
        运维脚本）可能仍按 FastAPI 用它，透传一下免得改一处炸一片。
        """
        return getattr(self._app, name)

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self._prefix:
                await self._redirect(scope, send, self._prefix + "/")
                return
            if path.startswith(self._prefix + "/"):
                scope = dict(scope)
                scope["path"] = path[len(self._prefix):]
                # raw_path 若留着旧值，某些中间件会拿它反推路径，与 path 打架
                scope.pop("raw_path", None)
                # **不要设 root_path**：Starlette 的 Mount 会把 root_path 累加进
                # 子作用域，StaticFiles 再按它剥一次，/csagent/assets/x.js 会被
                # 当成 assets/assets/x.js 而 404。代价是 FastAPI 自动生成的绝对
                # URL（openapi/docs）不带前缀——生产上这些本来就关着，前端也全用
                # 相对路径，不受影响。
        await self._app(scope, receive, send)

    async def _redirect(self, scope, send, location: str) -> None:
        query = scope.get("query_string") or b""
        target = location.encode("latin-1") + (b"?" + query if query else b"")
        await send({
            "type": "http.response.start",
            "status": 307,
            "headers": [(b"location", target), (b"content-length", b"0")],
        })
        await send({"type": "http.response.body", "body": b""})


def _verify_auth_config() -> None:
    """启动即校验鉴权配置，配错就别让服务起来。

    这类问题一旦静默放过，表现是「一切正常，只是谁都能读别人的会话」——
    没有任何报错会提醒你，所以必须在启动时炸掉。
    """
    settings = get_settings()
    if not settings.agent_auth_enabled:
        logger.warning(
            "auth_disabled_insecure",
            hint="AGENT_AUTH_ENABLED=false，所有接口免鉴权，仅限本地开发",
        )
        return
    try:
        resolve_secret()
    except AuthError as exc:
        raise RuntimeError(
            f"鉴权已开启但密钥不可用：{exc}。"
            f"请在 .env 配置 AGENT_SHARED_SECRET（>= {MIN_SECRET_LENGTH} 字符），"
            "或显式设 AGENT_AUTH_ENABLED=false（仅本地开发）。"
        ) from exc
    if settings.agent_auth_dev_login:
        logger.warning(
            "auth_dev_login_enabled",
            hint="AGENT_AUTH_DEV_LOGIN=true，任何人可为任意 uid 换取令牌，生产必须关闭",
        )
    if str(settings.admin_accounts or "").strip():
        # 只影响管理后台：配错时管理接口拒绝登录（503），聊天服务照常启动。
        # 打 ERROR 而不是 WARNING：生产 LOG_LEVEL=ERROR，WARNING 根本不落盘。
        try:
            resolve_admin_secret()
        except AuthError as exc:
            logger.error(
                "admin_token_secret_invalid",
                error=str(exc),
                hint=(
                    f"管理后台不可登录：请在 .env 配置 ADMIN_TOKEN_SECRET（>= {MIN_SECRET_LENGTH} "
                    "字符、只在本服务、不能与 AGENT_SHARED_SECRET 相同）"
                ),
            )


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    _verify_auth_config()
    settings = get_settings()
    # 交互式 API 文档只在鉴权关闭（本地开发）时开。生产开着等于把完整路由表
    # ——含 /api/auth/dev-token 的存在——直接告诉扫描器。
    docs_open = not settings.agent_auth_enabled
    app = FastAPI(
        title="通用 Agent",
        description="单 Agent 服务；实际能力见 /health 或 configs/agent.yaml",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs" if docs_open else None,
        redoc_url="/redoc" if docs_open else None,
        openapi_url="/openapi.json" if docs_open else None,
    )

    # 静态资源挂在根下的确定子路径，不用 catch-all mount——catch-all 会吃掉
    # /api/*，而 API 路由是 lifespan 里由 http_api 通道后挂的，顺序上抢不过它。
    if (STATIC_DIR / "assets").is_dir():
        app.mount(
            "/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets"
        )

    @app.get("/agent-widget.js", include_in_schema=False)
    async def agent_widget_js():
        """宿主接入 SDK。

        对外地址是 {前缀}/agent-widget.js（已给到运维与对接方），经 PrefixStripper
        剥掉前缀后落到这里。单独开一条路由而不是靠 StaticFiles 兜底，是为了不引入
        会吃掉 /api/* 的 catch-all 挂载。
        """
        path = STATIC_DIR / "agent-widget.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="SDK 不存在")
        # 显式 charset：宿主页编码未必是 UTF-8，脚本编码不能跟着宿主猜
        return FileResponse(path, media_type="application/javascript; charset=utf-8")

    @app.get("/")
    async def index():
        path = STATIC_DIR / "index.html"
        if path.exists():
            # 样式与脚本拆在 assets/chat/ 下，宿主 webview（尤其微信）缓存很凶，
            # 发版后会拿旧 js 配新 html。按文件内容现算版本号填进 ?v=，页面本身不缓存。
            html = path.read_text(encoding="utf-8").replace(
                "__ASSET_V__", _chat_asset_version()
            )
            return HTMLResponse(html, headers={"Cache-Control": "no-cache"})
        return {"message": "通用 Agent API 运行中"}

    @app.get("/admin", include_in_schema=False)
    async def admin_page():
        """管理后台（数据发布）。页面本身无敏感内容，接口另有管理鉴权。"""
        path = STATIC_DIR / "admin.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="管理页面不存在")
        return FileResponse(path)

    @app.get("/debug", include_in_schema=False)
    async def debug_page():
        """小窗调试页。页面不含令牌，须管理员登录后经 /api/admin/debug-token 现签。"""
        path = STATIC_DIR / "debug.html"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="调试页面不存在")
        return FileResponse(path)

    @app.get("/health")
    async def health():
        """存活探针。

        鉴权开启时只回 status——这个端点无鉴权，把企微连接态、渠道明细
        暴露给公网没有必要。要看详情就本地开发时看，或查日志。
        """
        current = get_settings()
        if current.agent_auth_enabled:
            return {"status": "ok"}
        channel_manager = getattr(app.state, "channel_manager", None)
        return {
            "status": "ok",
            "wecom_enabled": current.wecom_enabled,
            "channels": channel_manager.status() if channel_manager else {},
        }

    # 用 ASGI 包一层而不是 FastAPI(root_path=...)：root_path 的剥离语义随
    # Starlette 版本变过，而这里的行为必须是确定的。包一层则完全可控，
    # 且 app 本身仍是纯粹的 / 路由，测试里可以不经前缀直接打。
    return PrefixStripper(app, URL_PREFIX)


# 模块级实例：uvicorn 以 "csagent.main:app" 字符串引用它。
# 注意这是 PrefixStripper（ASGI 可调用），不是 FastAPI 实例；
# 属性访问已透传，按 app.state 用它的地方不受影响。
app = create_app()


def main() -> None:
    """命令行启动入口。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "csagent.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=False,
    )


if __name__ == "__main__":
    main()
