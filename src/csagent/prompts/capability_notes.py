"""按已挂载工具自动注入的能力约定。"""

from __future__ import annotations

# 工具名 → 短约定。仅当 Agent 实际挂载该工具时注入。
_TOOL_CAPABILITY_NOTES: dict[str, str] = {
    "read_file": (
        "read_file：大文件必须带 offset+limit 分段读，建议 limit=600；"
        "超长结果会被外置，用 read_artifact 按页取回，不要一次读整文件。"
    ),
    "read_artifact": (
        "read_artifact：取回本会话已外置的大工具结果，artifact_id 来自工具返回的"
        "[大结果已外置] 占位；必须带 offset+limit 分页。"
        "需要过滤、聚合、统计时不要翻页，直接 run_script 读占位里的文件路径。"
        "占位里的前几行预览不能当作全集来作答。"
    ),
    "send_file": (
        "send_file：仅当本轮用户明确要求发送/导出文件（如「发excel」「发附件」「导出」）时才可调用；"
        "禁止因历史轮次发过文件、结果很多、或上下文里挂着上次 xlsx 就自行发送；"
        "文件须已有有效内容；裸文件名优先找 outbound/（Agent 产出），再找 inbound/（用户上传）；"
        "工具确认入队后，答复说明文件即将发出，勿否认发送能力。"
    ),
    "write_file": (
        "write_file：默认写入 data/uploads/{session}/outbound/；"
        "用户上传在 inbound/，不要往 inbound 写；"
        "要出 PDF 时先把完整 HTML 写到 outbound，再调 html_to_pdf；"
        "仅当用户明确要求发回时再调 send_file（若已挂载），勿写完就默认发送。"
    ),
    "html_to_pdf": (
        "html_to_pdf：把已有 HTML 转成 PDF，默认 outbound 同名 .pdf。"
        "先 write_file 写出 HTML（可引用 outbound 里的 PNG）；版式写在 HTML/CSS 里，"
        "本工具不做模板。仅当用户明确要求发回时再 send_file。"
    ),
    "excel_tool": (
        "excel_tool：create 必须带 headers 和/或 rows，禁止空表；"
        "仅当用户明确要求发回时再调 send_file。"
    ),
    "run_script": (
        "run_script：会话脚本用 script=\"scripts/tools/{session}/\" 内相对路径，"
        "action=list 可查看会话与共享脚本；"
        "执行某个 Skill 自带的脚本用 script=\"<skill:name>/scripts/x.py\"（确切路径见该 Skill 的说明）。"
        "运行环境已安装 matplotlib，可按数据画折线/柱状/散点/饼图/热力图，"
        "输出 PNG 写到 CSAGENT_OUTBOUND_DIR（或 data/uploads/{session}/outbound/）；"
        "无显示器，脚本开头必须 matplotlib.use(\"Agg\")；"
        "中文按 Microsoft YaHei、SimHei、WenQuanYi Micro Hei、Noto Sans CJK SC 这个顺序设 font.sans-serif，并设 axes.unicode_minus=False。"
        "这是数据可视化，不是文生图；用户要图表时直接查数绘图，不要说没有画图能力，"
        "也不要先甩一张四要素调查表——缺口径再追问一项即可。"
        "仅当用户明确要求发图时再 send_file。"
    ),
    "write_script": (
        "write_script：默认写入 scripts/tools/{session}/。"
    ),
    "run_shell": (
        "run_shell：仅用于当前会话工作区的临时命令；禁止在仓库根、.git、.venv、"
        "data/llm_bodies、data/audit 上列举或搜索。"
        "Windows 默认 powershell；复杂分析优先 write_script+run_script。"
    ),
    "web_search": (
        "web_search：公开检索仅作参考并标明来源。"
    ),
    "http_call": (
        "http_call：确认目标地址可信后再请求；注意超时与响应截断。"
    ),
    "grep": (
        "grep：仅搜索会话工作区或 Skill 文档；禁止搜 llm_bodies/audit/.git/源码。"
    ),
    "glob": (
        "glob：仅匹配会话工作区内路径；禁止扫描整个仓库。"
    ),
    "todo_write": (
        "todo_write：会话级待办。每次调用都会马上在聊天里推一条勾选清单，"
        "所以必须边做边更新，不能等做完再一次性标记。"
        "预计 3 步以上的任务（多表查询、生成报告、跨文件处理），"
        "在任何业务工具之前先单独调用一次列出计划：正在做的那项 in_progress，其余 pending。"
        "每完成一步立刻再调用一次（merge=true），只更新刚完成的那一项，"
        "不要和其他工具同一轮一起调用。"
        "最终回复里不要再贴清单。用户说「继续」时先 todo_read。"
        "一两步能答完的问题不要用。"
    ),
    "list_skills": (
        "list_skills：列出可用 Skill；可传 query 过滤。"
    ),
    "load_skill": (
        "load_skill：加载 Skill 操作说明；执行领域任务前必须先加载（索引里标「已常驻」的不用）。"
    ),
    "set_reply_style": (
        "set_reply_style：用户表达「以后回复简短/详细点」「用英文回复」「开头结尾加123」"
        "这类**对回复风格本身**的要求时，必须调用它落库，"
        "否则偏好只活在本轮上下文里，换轮即失效。"
        "光在正文里答应「已记录」而不调用，等于没记——这是明确的错误。"
        "简短=concise，详细=detailed，恢复默认=normal，"
        "其他格式要求=custom 并在 note 里原样写清诉求。"
        "用户只是在问业务问题时不要调用。"
    ),
}


# 没挂 write_script 的部署（只许跑 Skill 自带脚本）：上面那几条会把模型引向
# 「自己写脚本画图 / 读外置文件」，做不到只会空耗轮次，这里换成收窄后的说法。
_NOTES_WITHOUT_WRITE_SCRIPT: dict[str, str] = {
    "run_script": (
        "run_script：只运行 Skill 自带的脚本，script=\"<skill:name>/scripts/x.py\"，"
        "确切路径与参数见该 Skill 的 load_skill 结果；本部署不能自写脚本，"
        "load_skill 里没给出的脚本不要尝试。"
    ),
    "read_artifact": (
        "read_artifact：取回本会话已外置的大工具结果，artifact_id 来自工具返回的"
        "[大结果已外置] 占位；必须带 offset+limit 分页。"
        "占位里的前几行预览不能当作全集来作答。"
    ),
}


def format_capability_notes(tool_names: list[str] | set[str]) -> str:
    """根据当前 Agent 可用工具生成约定段落；无匹配则返回空串。"""
    names = set(tool_names)
    notes = dict(_TOOL_CAPABILITY_NOTES)
    if "write_script" not in names:
        notes.update(_NOTES_WITHOUT_WRITE_SCRIPT)
    lines = [
        f"- {notes[name]}"
        for name in notes
        if name in names
    ]
    if not lines:
        return ""
    return "## 已挂载工具约定\n" + "\n".join(lines)
