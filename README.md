# 通用 Agent（单 Agent + 全工具）

本分支从客服多域编排演化为 **通用编程与工具助手**：一个 ReAct 循环挂载全部工具，无产品/知识/账户/交易等中间层，无编排层，也没有按业务场景切换的"模式"——只有一个 Agent、一份系统提示词，能力范围由挂载了哪些 tool_pack/Skill 决定。

## 目录地图

| 你想改什么 | 去哪里 |
|------------|--------|
| Agent 挂哪些工具包 | `configs/agent.yaml` |
| 工具包定义 | `configs/tool_packs.yaml` |
| 部署形态（提示词 / 工具包 / Skill 白名单） | `configs/profiles/<形态>/`，由 `AGENT_PROFILE` 选择；见 `docs/产品切换.md` |
| 入口通道启停 | `configs/channels.yaml` |
| Skill | `skills/<name>/SKILL.md`（frontmatter + 正文 + 可选 `scripts/`/`references/`/`assets/`，渐进式披露） |
| 受控脚本 | `scripts/tools/` |
| 全局运行参数 | `configs/base.yaml` / `.env` |
| 审计日志 | `data/audit/` 下按天的 JSONL；说明见 `docs/审计日志.md` |
| 可发布的数据集（管理后台） | `configs/datasets.yaml`；说明见 `docs/数据管理后台.md` |
| 循环内压缩提示词 | `configs/prompts/loop_compactor.md` |
| 源码 | `src/csagent/` |

## 架构概览

```
企微 / HTTP SSE / 微信群（经发送机 wecom-sender）
  → MessageGateway（附件 / 护栏入口）
       → Agent（记忆 / 审计 / 流式 / ReAct 循环）
            ├─ workspace_files（list/read/write/edit/send）
            ├─ script_executor（write/run_script/run_shell）
            ├─ web_search
            ├─ http_call
            ├─ excel_tool
            ├─ mcp（可选）
            ├─ reply_preference（回复风格偏好）
            └─ skills（list_skills/load_skill）
  → Skill 索引常驻 system prompt，模型按需 load_skill 取正文/脚本
```

与客服（`main` 分支）关系：本分支独立演化。基金产品查询这类结构化查询能力已原生移植（见
[`docs/当前实现说明.md`](docs/当前实现说明.md) 的 ADR-2/ADR-4）；其余客服能力（知识/账户/交易）
需要时通过 `http_call` 或 MCP 调用 `main` 进程的稳定接口，不在本分支重建多 Agent。

## 文档

| 文档 | 说明 |
|------|------|
| [`docs/当前实现说明.md`](docs/当前实现说明.md) | **改代码前先读**：代码地图、跨层契约、配置权威源、红线、技术债、架构决策记录 |
| [`docs/架构图.md`](docs/架构图.md) | 分层架构与调用链路图 |
| [`docs/宿主接入说明.md`](docs/宿主接入说明.md) | 给对接方：令牌签发、小窗嵌入 SDK、接口契约、上线检查 |

## 快速开始

```bash
# 创建并激活虚拟环境
python -m venv .venv
Windows: 
.venv\Scripts\activate
# macOS / Linux:
#source .venv/bin/activate

pip install -e ".[dev]"
cp .env.example .env
# 编辑 LLM_*
python -m csagent.main
# http://127.0.0.1:8000/health
```

### 环境变量（必填）

```env
LLM_API_KEY=...
LLM_BASE_URL=...
LLM_MODEL=...

# 可选企微
WECOM_ENABLED=false
WECOM_BOT_ID=
WECOM_SECRET=
```

### 企微

1. `WECOM_ENABLED=true` + BotID/Secret
2. 同一 BotID 仅允许 1 条长连接
3. 健康检查：`GET /health`

### 微信群（经发送机）

个人微信没有接口：发送机（[wecom-sender](https://github.com/guangdianliu-cpu/wecom-sender)）看屏幕读白名单群里 @我 的消息，
本服务按群作为会话交给 Agent（整群共用记忆和工作区，正文标上发言人），再经发送机把「@提问人 回答」发回群里。

本服务是服务端，发送机主动连上来（只需要它能出站访问本服务）：`.env` 配 `WECHAT_LINK_TOKEN`（>= 32 字符），
发送机 `config.local.yaml` 的 `uplink.url` 填本服务对外地址（如 `https://lab.leadfund.com.cn/csagent`）、`uplink.token`
填同一个。本服务多出 `/api/wechat/link/*`（只给发送机用，令牌鉴权）。前面的 nginx 要放行：
`client_max_body_size` 要放得下最大的截图（引用的原图原样随消息上传，一次一张，本服务单张上限 20MB；超了发送机会不带图重推并标 `image_missing`），`proxy_read_timeout` 大于 30 秒（发送机长轮询 25 秒）。

1. `configs/channels.yaml` → `wechat.config.reply_mode`：`off` 只记录、`draft` 跑 Agent 不发到原群（配了 `review_group`
   就把草稿发到审核群）、`auto` 直接回复。上线按 off → draft → auto 逐级放开
2. 发送机标了没把握的消息（OCR 置信度低、原图没取到等）、读到超过 `max_age_minutes` 的旧消息不自动回复；
   每个群每分钟最多回 `max_replies_per_minute` 条；答得慢先回一句「已收到，正在处理」
3. 游标按发送机的消息库存 `data/wechat/cursor.json`，处理完才前进；哪边重启都从没处理完的那条接着收

## 设计原则

- **单 Agent + 全工具**：只有一个 Agent、一份系统提示词，没有按业务场景切换的"模式"；LLM 直接选工具，跨能力自然组合
- **ToolPack 配置驱动**：增删能力改 YAML，不改核心代码
- **基础工具是一等公民，业务逻辑在 Skill**：`run_script`/`read_file`/`http_call` 等是少数几个通用、稳定的原语；业务 SOP 与身份陈述放 `skills/<name>/SKILL.md`（可选自带脚本），不写死进系统提示词或新增 Tool 类
- **客服能力外挂**：本分支未原生移植的能力（知识/账户/交易）需要时 HTTP/MCP 接 `main`，不在本分支重建多 Agent
