const chatContainer = document.getElementById("chat-container");
const messageInput = document.getElementById("message-input");
const sendBtn = document.getElementById("send-btn");
const attachBtn = document.getElementById("attach-btn");
const fileInput = document.getElementById("file-input");
const pendingFilesEl = document.getElementById("pending-files");
const userIdInput = document.getElementById("user-id");
const authGate = document.getElementById("auth-gate");
const authGateMsg = document.getElementById("auth-gate-msg");
const widgetTitle = document.getElementById("widget-title");
const widgetWho = document.getElementById("widget-who");
const widgetClose = document.getElementById("widget-close");
const connDot = document.getElementById("conn-dot");

/** @type {{file: File, workspace_path?: string, filename?: string}[]} */
let pendingFiles = [];

/* =====================================================================
 * 登录态 / 身份层
 * ---------------------------------------------------------------------
 * 本页有两种运行形态：
 *   1) 被宿主产品 iframe 嵌入（弹小窗）—— 身份由宿主经 postMessage 下发；
 *   2) 直接用浏览器打开 —— 走 mock，方便本地调试与联调前自测。
 *
 * session_id 一律派生为 `web:{userId}`，**按用户长期连续**：
 * 关窗、换浏览器再回来，short_term 历史 / 快照 / 回复风格偏好都还在
 * （这几样在后端都挂在 session_id 上）。口径与企微侧 `wecom:{from_user}`
 * 一致，见 tools/preference_tool.py 的 preference_key。
 * ===================================================================*/
const EMBEDDED = window.self !== window.top;
const QUERY = new URLSearchParams(location.search);
const WIDGET_SOURCE = "csagent-widget";   // 本页发出的消息
const HOST_SOURCE = "csagent-host";       // 宿主发来的消息

/**
 * 宿主页面 origin 白名单。
 *
 * 上生产前必须填。留空 = 接受任意 origin 下发的身份，等于谁把这个页面
 * iframe 进去都能冒充任意用户，进而读到别人的对话历史与偏好。
 * 联调期留空是为了不阻塞，控制台会持续告警。
 */
const HOST_ORIGIN_ALLOWLIST = [
  "https://lab.leadfund.com.cn",       // 生产：宿主产品
];

const Identity = {
  userId: "",
  userName: "",
  token: "",
  ready: false,
  mock: false,
  /** 会话/记忆边界。空 userId 时返回空串，由闸门挡住发送。 */
  get sessionId() {
    return this.userId ? "web:" + this.userId : "";
  },
};

function setGate(visible, msg, isError) {
  authGate.classList.toggle("is-visible", Boolean(visible));
  authGate.classList.toggle("is-error", Boolean(isError));
  if (msg) authGateMsg.textContent = msg;
  connDot.classList.toggle("is-offline", Boolean(isError));
}

/** 应用一份身份；宿主 token 续期时会再次调用，此时不打断正在进行的对话。 */
function applyIdentity(payload, opts) {
  const o = opts || {};
  const prevUserId = Identity.userId;
  const userId = String(payload.userId || payload.user_id || "").trim();
  if (!userId) {
    setGate(true, "宿主下发的登录态缺少 userId，无法建立会话。", true);
    return false;
  }

  Identity.userId = userId;
  Identity.userName = String(payload.userName || payload.user_name || "").trim();
  Identity.token = String(payload.token || "").trim();
  Identity.mock = Boolean(o.mock);
  Identity.ready = true;

  document.body.classList.toggle("is-mock", Identity.mock);
  userIdInput.value = userId;
  widgetWho.textContent = Identity.userName || userId;
  if (payload.title) {
    widgetTitle.textContent = String(payload.title);
    document.title = String(payload.title);
  }

  // 换人了（宿主切账号）→ 清空聊天区，避免上一个用户的内容留在屏幕上
  if (prevUserId && prevUserId !== userId) {
    chatContainer
      .querySelectorAll(".message, .assistant-block, .file-download, .history-divider, .challenge-card")
      .forEach((el) => el.remove());
  }
  setGate(false);
  // 快捷入口（entries.js）据此拉取内容；宿主续期 token 也会再发一次，由接收方去重
  document.dispatchEvent(new CustomEvent("csagent:identity"));
  // 刷新 / 重开 / 切账号后，把服务端记着的那几轮放回屏幕上
  loadHistory();
  return true;
}

/* ---------------------- 宿主桥接（postMessage） ---------------------- */
const HostBridge = {
  /**
   * 向宿主发消息。宿主与本页同源（/debug 调试页、本地联调）时投给本源；
   * 否则白名单恰好一项时精确投递，再否则用 "*"（只发不含敏感信息的通知）。
   * 同源判断不能省：只按白名单投递的话，本地 localhost 宿主收不到 ready /
   * drag-start，表现为握手不上、小窗拖不动。
   */
  post(type, payload) {
    if (!EMBEDDED) return;
    let target = HOST_ORIGIN_ALLOWLIST.length === 1 ? HOST_ORIGIN_ALLOWLIST[0] : "*";
    try {
      // 跨源时读 parent.location.origin 会抛异常，落回上面的白名单口径
      if (window.parent.location.origin === window.location.origin) {
        target = window.location.origin;
      }
    } catch (e) {
      /* 跨源宿主 */
    }
    try {
      window.parent.postMessage(
        { source: WIDGET_SOURCE, type: type, payload: payload || {} },
        target
      );
    } catch (e) {
      /* 宿主已卸载 */
    }
  },

  originAllowed(origin) {
    // 同源永远放行：宿主页与本页同源，说明它就是本服务自己的部署，
    // 不存在跨域信任边界。
    if (origin === window.location.origin) return true;
    if (!HOST_ORIGIN_ALLOWLIST.length) {
      console.warn(
        "[csagent] HOST_ORIGIN_ALLOWLIST 为空，正在接受来自 " +
          origin +
          " 的登录态。上生产前必须配置白名单。"
      );
      return true;
    }
    return HOST_ORIGIN_ALLOWLIST.indexOf(origin) !== -1;
  },

  onMessage(event) {
    const data = event.data;
    if (!data || typeof data !== "object" || data.source !== HOST_SOURCE) return;
    if (!this.originAllowed(event.origin)) {
      console.warn("[csagent] 拒绝来自未授权 origin 的消息:", event.origin);
      return;
    }
    const payload = data.payload || {};
    switch (data.type) {
      case "auth": // 下发 / 刷新登录态
        applyIdentity(payload, { mock: false });
        break;
      case "logout": // 宿主登出
        Identity.ready = false;
        Identity.userId = "";
        historyLoadedFor = "";   // 重新登录时要再回填一次
        setGate(true, "登录态已失效，请在主产品中重新登录。", true);
        break;
      case "ping":
        this.post("pong", {});
        break;
      default:
        break;
    }
  },

  init() {
    window.addEventListener("message", (e) => this.onMessage(e));
    // 告诉宿主「我准备好了，把登录态给我」
    this.post("ready", {
      version: 1,
      wants: ["userId", "userName", "token"],
    });
  },
};

/* --------------------------- 联调登录（dev） -------------------------- */
/* 仅当服务端 AGENT_AUTH_DEV_LOGIN=true 时可用。它**不是** mock：拿到的是
   真令牌，后续请求走与生产完全相同的验签路径，所以联调期发现的鉴权问题
   在生产同样成立。服务端关掉这个开关，下面整段就自动失效。 */
const DEV_USER_KEY = "csagent_dev_user";

async function startDevLogin(reason) {
  const userId = (QUERY.get("userId") || localStorage.getItem(DEV_USER_KEY) || "u1001").trim();
  try {
    const resp = await fetch("api/auth/dev-token", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: userId, name: "调试用户 " + userId }),
    });
    if (!resp.ok) {
      setGate(true, "联调登录不可用（服务端未开启 dev-token）。", true);
      return;
    }
    const data = await resp.json();
    applyIdentity(
      { userId: userId, userName: "调试用户 " + userId, token: data.token },
      { mock: true }
    );
    localStorage.setItem(DEV_USER_KEY, userId);
    console.info(
      "[csagent] 联调登录（" + reason + "）：userId=" + userId +
        " → session_id=" + Identity.sessionId
    );
  } catch (e) {
    setGate(true, "联调登录请求失败：" + e.message, true);
  }
}

// 调试栏改了就换一个联调用户，方便验证记忆是否按用户隔离
userIdInput.addEventListener("change", () => {
  const v = userIdInput.value.trim();
  if (!v || !Identity.mock) return;
  localStorage.setItem(DEV_USER_KEY, v);
  startDevLogin("切换用户");
});

async function initIdentity() {
  const wantDev = QUERY.get("mock") === "1" || !EMBEDDED;

  if (EMBEDDED) document.body.classList.add("is-embedded");

  // 联调登录只在服务端显式开启时才走；否则一律要求宿主下发登录态。
  // 生产上 dev_login=false，独立打开这个页面会停在闸门上，什么也做不了。
  if (wantDev && ServerAuth.devLogin) {
    await startDevLogin(EMBEDDED ? "?mock=1" : "独立打开");
    return;
  }

  if (!EMBEDDED) {
    setGate(
      true,
      "本页需要从主产品中打开。直接访问无法获取登录态。",
      true
    );
    return;
  }

  setGate(true, "正在获取登录态…");
  HostBridge.init();
  // 宿主迟迟不下发就别一直转圈，给个明确说法
  setTimeout(() => {
    if (!Identity.ready) {
      setGate(
        true,
        "未收到宿主下发的登录态。请确认主产品已实现 postMessage 握手。",
        true
      );
    }
  }, 8000);
}

widgetClose.addEventListener("click", () => HostBridge.post("close", {}));

/* 标题栏发起拖拽。
   iframe 内部只负责「告诉宿主从哪儿按下的」，之后的 mousemove 由宿主接管 ——
   指针一旦移出 iframe，这里就再也收不到事件了，必须外面接。 */
document.querySelector(".widget-header").addEventListener("mousedown", (e) => {
  if (e.button !== 0) return;
  if (e.target.closest("button")) return;   // 点关闭按钮不算拖拽
  e.preventDefault();
  HostBridge.post("drag-start", { x: e.clientX, y: e.clientY });
});

let isStreaming = false;
// 当前这轮的流读取控制器；停止时若服务端迟迟不收尾，用它兜底断开
let streamAbort = null;
let stopRequested = false;

function setSendButtonMode(streaming) {
  if (streaming) {
    sendBtn.classList.add("stop");
    sendBtn.textContent = "■";
    sendBtn.setAttribute("aria-label", "停止");
    sendBtn.title = "停止本轮";
    sendBtn.disabled = false;
  } else {
    sendBtn.classList.remove("stop");
    sendBtn.textContent = "↑";
    sendBtn.setAttribute("aria-label", "发送");
    sendBtn.title = "";
    sendBtn.disabled = false;
  }
}

/** 请服务端终止本会话正在跑的一轮（含刷新前留在后台的那轮）。 */
async function requestStop() {
  const resp = await fetch("api/chat/stop", { method: "POST", headers: authHeaders() });
  if (resp.status === 401) { onUnauthorized(); throw new Error("登录态已失效，请重新登录"); }
  if (!resp.ok) throw new Error("HTTP " + resp.status);
  return resp.json();
}

async function stopCurrentTurn() {
  if (!isStreaming || stopRequested) return;
  stopRequested = true;
  sendBtn.disabled = true;
  try {
    await requestStop();
  } catch (err) {
    appendMessage("error", "停止失败: " + (err.message || err));
  }
  // 服务端正常会推 stopped 事件并关流；3 秒还没关就本地断开
  const ctrl = streamAbort;
  setTimeout(() => { if (isStreaming && ctrl === streamAbort && ctrl) ctrl.abort(); }, 3000);
}

/** 上一轮仍在后台跑（例如刷新前发出的问题）：给一条带「停止上一轮」按钮的提示。 */
function appendBusyNotice(detail) {
  const el = appendMessage("error", detail || "上一轮还在处理中");
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "inline-action";
  btn.textContent = "停止上一轮";
  btn.addEventListener("click", async () => {
    btn.disabled = true;
    try {
      const r = await requestStop();
      btn.textContent = r.stopped ? "已停止，可重新发送" : "上一轮已结束，可重新发送";
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "停止失败，重试";
    }
  });
  el.appendChild(btn);
}
let showThinkingProcess = false;

/* ---------------------------------------------------------------------
 * Markdown 渲染：优先用本地 marked + DOMPurify；取不到就用内置兜底。
 *
 * 原先挂 cdn.jsdelivr.net，嵌入内网宿主产品后基本拿不到，而投研回答里表格很多，
 * 退化成 textContent 会糊成一团。兜底渲染器覆盖标题/粗斜体/行内码/代码块/
 * 列表/表格/链接这几样够用的语法。
 *
 * 安全：兜底路径**先整体转义 HTML，再做标记替换**，所以模型输出里的
 * <script> 之类只会被当字面量显示，不会形成节点。
 * ------------------------------------------------------------------- */
function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function fallbackMarkdown(src) {
  const lines = escapeHtml(src).split("\n");
  const out = [];
  let i = 0;

  const inline = (t) =>
    t
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*]+)\*/g, "$1<em>$2</em>")
      .replace(
        /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );

  const isTableSep = (s) => /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(s) && s.indexOf("-") !== -1;
  const cells = (s) => s.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());

  while (i < lines.length) {
    const line = lines[i];

    // 代码块
    if (/^\s*```/.test(line)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push("<pre><code>" + buf.join("\n") + "</code></pre>");
      continue;
    }

    // 表格：当前行有 | 且下一行是分隔行
    if (line.indexOf("|") !== -1 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      const head = cells(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].indexOf("|") !== -1 && lines[i].trim()) {
        body.push(cells(lines[i++]));
      }
      out.push(
        "<table><thead><tr>" +
          head.map((c) => "<th>" + inline(c) + "</th>").join("") +
          "</tr></thead><tbody>" +
          body
            .map((r) => "<tr>" + r.map((c) => "<td>" + inline(c) + "</td>").join("") + "</tr>")
            .join("") +
          "</tbody></table>"
      );
      continue;
    }

    // 标题
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      const lv = h[1].length + 2 > 6 ? 6 : h[1].length + 2; // # -> h3，避免抢页面标题层级
      out.push("<h" + lv + ">" + inline(h[2]) + "</h" + lv + ">");
      i++;
      continue;
    }

    // 列表（连续行合并成一个 ul/ol）
    if (/^\s*([-*+]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const items = [];
      while (i < lines.length && /^\s*([-*+]|\d+\.)\s+/.test(lines[i])) {
        items.push("<li>" + inline(lines[i].replace(/^\s*([-*+]|\d+\.)\s+/, "")) + "</li>");
        i++;
      }
      out.push((ordered ? "<ol>" : "<ul>") + items.join("") + (ordered ? "</ol>" : "</ul>"));
      continue;
    }

    // 空行 / 普通段落
    if (!line.trim()) {
      i++;
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,4}\s|[-*+]\s|\d+\.\s)/.test(lines[i])) {
      para.push(lines[i++]);
    }
    out.push("<p>" + inline(para.join("<br>")) + "</p>");
  }
  return out.join("");
}

/* CDN 是 async 的，加载完成时机不定，所以每次渲染现判而不是启动时固化。 */
let mdModeLogged = false;
function hasCdnMd() {
  const ok = typeof marked !== "undefined" && typeof DOMPurify !== "undefined";
  if (!mdModeLogged) {
    mdModeLogged = true;
    if (!ok) {
      console.info("[csagent] marked/DOMPurify 不可用（内网常见），使用内置 Markdown 兜底渲染。");
    }
  }
  if (ok && !marked.__csagentConfigured) {
    marked.setOptions({ breaks: true, gfm: true });
    marked.__csagentConfigured = true;
  }
  return ok;
}

/* marked 只要抛过一次就别再试了：小程序 webview 内核版本杂，polyfill 补不全
   所有缺失的 API，兜底渲染比整条消息渲染失败强。 */
let mdBroken = false;

/* GFM 把单个 ~text~ 也当删除线，而金融文案里 ~ 是区间符号——
   「0~100万: 1.5%; 100万~500万: 1.2%」会被解析成
   「0<del>100万: 1.5%; 100万</del>500万: 1.2%」，费率直接看不懂。

   解法：解析前把**落单的** ~ 转义掉，成对的 ~~ 留着（真删除线仍可用）。
   不走 marked.use({hooks}) —— 实测 v15 里那个形式不生效；也不用带后行
   断言的正则，小程序 webview 内核不一定支持。

   代码区（``` 围栏与 ` 行内码）里的 ~ 要原样保留，否则 `rm -rf ~/tmp`
   会显示成 `rm -rf \~/tmp`。按反引号切段，只处理段外的部分。 */
function escapeLoneTildes(md) {
  if (md.indexOf("~") === -1) return md;
  const KEEP = "\u0000DBL\u0000";
  // 以围栏和行内码为界切开，奇数段落在代码区内，跳过
  const parts = String(md).split(/(```[\s\S]*?```|`[^`\n]*`)/g);
  for (let i = 0; i < parts.length; i += 2) {
    parts[i] = parts[i]
      .split("~~").join(KEEP)
      .split("~").join("\\~")
      .split(KEEP).join("~~");
  }
  return parts.join("");
}

function renderMarkdown(el, text) {
  el.classList.add("markdown-body");
  if (!mdBroken && hasCdnMd()) {
    try {
      el.innerHTML = DOMPurify.sanitize(marked.parse(escapeLoneTildes(text)), {
        USE_PROFILES: { html: true },
      });
      enhanceAnswer(el);
      return;
    } catch (e) {
      mdBroken = true;
      console.warn("[csagent] marked 渲染失败，改用内置兜底渲染：", e);
    }
  }
  el.innerHTML = fallbackMarkdown(text);
  enhanceAnswer(el);
}

/** 服务端鉴权开关；initIdentity() 要靠它决定走宿主握手还是联调登录。 */
const ServerAuth = { enabled: true, devLogin: false };

async function loadChatConfig() {
  try {
    const response = await fetch("api/chat/config");
    if (!response.ok) return;
    const config = await response.json();
    showThinkingProcess = Boolean(config.show_thinking_process);
    ServerAuth.enabled = config.auth_enabled !== false;
    ServerAuth.devLogin = Boolean(config.dev_login);
  } catch {
    /* ignore */
  }
}

/* 下面所有 fetch 都用**相对路径**（api/… 而非 /api/…）。
   生产上 nginx 只把 /csagent 转发过来，写死的 /api/… 会打到宿主域名的根路径、
   根本到不了本服务。相对路径跟着页面 URL 走，本地 / 与线上 /csagent/ 都对。
   前提是页面 URL 带尾斜杠——后端对裸 /csagent 会 307 补上。 */

/** 统一给请求加登录态头。 */
function authHeaders(extra) {
  const h = Object.assign({}, extra || {});
  if (Identity.token) h["Authorization"] = "Bearer " + Identity.token;
  return h;
}

/** 401 统一处理：清掉本地登录态，提示去主产品重新登录。 */
function onUnauthorized() {
  Identity.ready = false;
  Identity.token = "";
  historyLoadedFor = "";   // 换到新令牌后要再回填一次
  setGate(true, "登录态已失效，请在主产品中重新登录。", true);
  HostBridge.post("unauthorized", {});
}

function setIdentityInputsDisabled(disabled) {
  userIdInput.disabled = disabled;
  attachBtn.disabled = disabled;
}

function renderPendingFiles() {
  pendingFilesEl.innerHTML = "";
  if (!pendingFiles.length) {
    pendingFilesEl.classList.remove("is-visible");
    return;
  }
  pendingFilesEl.classList.add("is-visible");
  pendingFiles.forEach((item, index) => {
    const chip = document.createElement("span");
    chip.className = "file-chip";
    chip.textContent = item.filename || item.file.name;
    const removeBtn = document.createElement("button");
    removeBtn.type = "button";
    removeBtn.setAttribute("aria-label", "移除");
    removeBtn.textContent = "×";
    removeBtn.addEventListener("click", () => {
      pendingFiles.splice(index, 1);
      renderPendingFiles();
    });
    chip.appendChild(removeBtn);
    pendingFilesEl.appendChild(chip);
  });
}

async function uploadPendingFiles() {
  const uploaded = [];
  for (const item of pendingFiles) {
    if (item.workspace_path && item.download_url) {
      uploaded.push({
        workspace_path: item.workspace_path,
        filename: item.filename || item.file?.name,
        download_url: item.download_url,
      });
      continue;
    }
    const form = new FormData();
    // session_id 不再由前端指定：服务端从验签身份派生
    form.append("file", item.file, item.file.name);
    const resp = await fetch("api/files/upload", {
      method: "POST",
      headers: authHeaders(),
      body: form,
    });
    if (resp.status === 401) {
      onUnauthorized();
      throw new Error("登录态已失效");
    }
    if (!resp.ok) {
      const detail = await resp.text();
      throw new Error("上传失败: " + (detail || resp.status));
    }
    const data = await resp.json();
    item.workspace_path = data.workspace_path;
    item.filename = data.filename;
    item.download_url = data.download_url;
    uploaded.push({
      workspace_path: data.workspace_path,
      filename: data.filename,
      download_url: data.download_url,
    });
  }
  return uploaded;
}

const IMAGE_FILE_RE = /\.(png|jpe?g|gif|webp|bmp|svg)$/i;

function isImageFile(filename) {
  return IMAGE_FILE_RE.test(filename || "");
}

function appendFileDownload(filename, url, isUser = false, size = 0) {
  const block = document.createElement("div");
  block.className = isUser ? "assistant-block is-user-file" : "assistant-block";
  // 助手发出的图片直接渲染；用户自己上传的仍只给下载条。
  if (!isUser && isImageFile(filename)) {
    const img = document.createElement("img");
    img.className = "file-preview";
    img.alt = filename || "图片";
    img.src = url;
    block.appendChild(img);
  }
  // 文件卡片在 report.js
  block.appendChild(buildFileCard(filename, url, size, isUser));
  const typing = document.getElementById("typing");
  if (typing) {
    chatContainer.insertBefore(block, typing);
  } else {
    chatContainer.appendChild(block);
  }
  chatContainer.scrollTop = chatContainer.scrollHeight;
  return block;
}

function appendMessage(role, content) {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.textContent = content;
  chatContainer.appendChild(el);
  chatContainer.scrollTop = chatContainer.scrollHeight;
  return el;
}

/* =====================================================================
 * 确认卡片
 * ---------------------------------------------------------------------
 * 策略挂起高危操作后，SSE 推一条 challenge。按钮在本轮结束前
 * 不可点：服务端此时仍占着会话，点了只会 409。刷新后靠 /api/chat/challenges
 * 把还没过期的卡片画回来。
 * ===================================================================*/
// 同一任务里的确认要按先后点：前面的没处理完，后面的置灰
const CHALLENGE_WAITING = "等上一张确认处理完后可点";

/** 过期时刻（Unix 秒）→ 「 · HH:MM 前有效」；没有时不显示。 */
function challengeValidity(expiresAt) {
  const seconds = Number(expiresAt) || 0;
  if (!seconds) return "";
  const at = new Date(seconds * 1000);
  const hh = String(at.getHours()).padStart(2, "0");
  const mm = String(at.getMinutes()).padStart(2, "0");
  return " · " + hh + ":" + mm + " 前有效";
}

function setChallengeReady(card, ready, enabled) {
  card.dataset.ready = ready ? "1" : "0";
  if (card.classList.contains("is-settled")) return;
  const clickable = ready && enabled;
  card.querySelectorAll("button").forEach((btn) => { btn.disabled = !clickable; });
  const status = card.querySelector(".challenge-status");
  if (!status) return;
  if (!ready) status.textContent = CHALLENGE_WAITING;
  else if (!enabled) status.textContent = "本轮结束后可以确认";
  else if (status.textContent === CHALLENGE_WAITING || status.textContent === "本轮结束后可以确认") {
    status.textContent = "";
  }
}

function appendChallengeCard(item, enabled) {
  const taskId = (item && item.task_id) || "";
  if (!taskId) return null;
  const ready = item.ready !== false;
  const existing = chatContainer.querySelector('.challenge-card[data-task-id="' + CSS.escape(taskId) + '"]');
  if (existing) {
    setChallengeReady(existing, ready, enabled);
    return null;
  }
  const card = document.createElement("div");
  card.className = "challenge-card";
  card.dataset.taskId = taskId;

  const title = document.createElement("div");
  title.className = "challenge-title";
  title.textContent = "需要您确认这项操作";
  card.appendChild(title);

  if (item.reason) {
    const reason = document.createElement("div");
    reason.className = "challenge-reason";
    reason.textContent = item.reason;
    card.appendChild(reason);
  }

  const quote = document.createElement("div");
  quote.className = "challenge-quote";
  quote.textContent = item.summary || item.content || item.tool || "待确认操作";
  card.appendChild(quote);

  if (item.tool) {
    const meta = document.createElement("div");
    meta.className = "challenge-meta";
    meta.textContent = "执行工具：" + item.tool + challengeValidity(item.expires_at);
    card.appendChild(meta);
  }

  const actions = document.createElement("div");
  actions.className = "challenge-actions";
  const approve = document.createElement("button");
  approve.type = "button";
  approve.className = "challenge-approve";
  approve.textContent = "批准执行";
  approve.disabled = !enabled;
  approve.addEventListener("click", () => decideChallenge(card, "confirm"));
  const reject = document.createElement("button");
  reject.type = "button";
  reject.className = "challenge-reject";
  reject.textContent = "拒绝驳回";
  reject.disabled = !enabled;
  reject.addEventListener("click", () => decideChallenge(card, "cancel"));
  actions.appendChild(approve);
  actions.appendChild(reject);
  card.appendChild(actions);

  const status = document.createElement("div");
  status.className = "challenge-status";
  card.appendChild(status);
  setChallengeReady(card, ready, enabled);

  const typing = document.getElementById("typing");
  if (typing) chatContainer.insertBefore(card, typing);
  else chatContainer.appendChild(card);
  chatContainer.scrollTop = chatContainer.scrollHeight;
  return card;
}

function enableChallengeCards() {
  chatContainer.querySelectorAll(".challenge-card:not(.is-settled)").forEach((card) => {
    setChallengeReady(card, card.dataset.ready !== "0", true);
  });
  // 前面的卡片处理完后，后面的要从服务端拿到最新的「能不能点」
  loadPendingChallenges();
}

function settleChallenge(card, titleText, statusText) {
  card.classList.add("is-settled");
  const title = card.querySelector(".challenge-title");
  if (title) title.textContent = titleText;
  card.querySelector(".challenge-actions")?.remove();
  const status = card.querySelector(".challenge-status");
  if (status) status.textContent = statusText || "";
}

async function decideChallenge(card, action) {
  if (isStreaming || card.classList.contains("is-settled")) return;
  const taskId = card.dataset.taskId;
  card.querySelectorAll("button").forEach((btn) => { btn.disabled = true; });
  const status = card.querySelector(".challenge-status");
  if (status) status.textContent = action === "confirm" ? "正在执行…" : "正在取消…";
  try {
    const resp = await fetch("api/chat/challenge", {
      method: "POST",
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({ task_id: taskId, action: action }),
    });
    if (resp.status === 401) {
      onUnauthorized();
      return;
    }
    if (resp.status === 409) {
      const body = await resp.json().catch(() => ({}));
      if (status) status.textContent = body.detail || "上一轮还在处理，请稍后再点";
      card.querySelectorAll("button").forEach((btn) => { btn.disabled = false; });
      return;
    }
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      settleChallenge(card, "操作未完成", body.detail || ("请求失败 HTTP " + resp.status));
      return;
    }
    const data = await resp.json();
    if (data.status === "approved") {
      settleChallenge(card, "操作已批准", "已执行，正在继续原来的任务…");
      (data.files || []).forEach((f) => {
        if (f && f.url) appendFileDownload(f.filename || "附件", f.url);
      });
      (data.reports || []).forEach(appendReportCard);
      if (data.resume_task_id) {
        // 执行结果回灌给模型，把原任务做完（后面的卡片会在这一轮结束后可点）
        await resumeAfterApproval(data.resume_task_id);
        if (status) status.textContent = "";
      } else if (data.reply) {
        const el = appendMessage("assistant", "");
        renderMarkdown(el, data.reply);
      }
      return;
    }
    if (data.status === "blocked") {
      // 兜底：正常情况下排在后面的卡片已经置灰，点不到这里
      setChallengeReady(card, false, true);
      return;
    }
    if (data.status === "cancelled") {
      settleChallenge(card, "操作已驳回", data.reply || "不会执行这项操作。");
      // 让模型知道被拒了，把原任务里不依赖它的部分做完
      if (data.resume_task_id) await resumeAfterApproval(data.resume_task_id);
      else loadPendingChallenges();
      return;
    }
    settleChallenge(
      card,
      data.status === "duplicate" ? "已经处理过" : "无法确认",
      data.reply || "操作没有执行。",
    );
  } catch (err) {
    if (status) status.textContent = "请求失败: " + (err.message || err);
    card.querySelectorAll("button").forEach((btn) => { btn.disabled = false; });
  }
}

async function loadPendingChallenges() {
  try {
    const resp = await fetch("api/chat/challenges", { headers: authHeaders() });
    if (resp.status === 401) {
      onUnauthorized();
      return;
    }
    if (!resp.ok) return;
    const data = await resp.json();
    (data.items || []).forEach((item) => appendChallengeCard(item, !isStreaming));
  } catch (e) {
    console.warn("[csagent] 待确认卡片加载失败：", e);
  }
}

/* =====================================================================
 * 历史回填
 * ---------------------------------------------------------------------
 * 刷新页面、或宿主重建 iframe 之后，聊天区是空的，但后端 short_term 仍按
 * session_id 记着这轮对话 —— 模型记得、用户看不见，用起来就是「刚聊的没了」。
 * 拿到身份后回填一次，让屏幕上的内容与模型的上下文对齐。
 *
 * 只认服务端这一份，不在浏览器另存副本：后端记忆有窗口上限、有 TTL，
 * memory 后端进程重启还会清空。本地副本一旦活得比服务端久，用户对着历史追问、
 * 模型却完全不认，比空白更难解释。
 * ===================================================================*/
let historyLoadedFor = "";

/** 历史里的文件 / 报告标记：有真链接或卡片时就不再显示这几行文字。 */
const FILE_MARKER_RE = /(?:\n*\[(?:附件|已发送文件|报告)\][^\n]*)+$/;

/** 一条历史消息 → 一个或多个节点（正文 + 每个文件一条下载链接）。 */
function buildHistoryNodes(msg) {
  const files = Array.isArray(msg.files) ? msg.files : [];
  const reports = Array.isArray(msg.reports) ? msg.reports : [];
  let content = msg.content || "";
  // 正文末尾那句「[附件] xx.xlsx」是给没有链接时看的兜底；既然拿到了真链接，
  // 就把它摘掉，否则同一个文件名会出现两次。
  if (files.length || reports.length) content = content.replace(FILE_MARKER_RE, "");

  const nodes = [];
  if (content.trim()) {
    const el = document.createElement("div");
    // 存储里只有 user/assistant 两种角色，其余一律按助手气泡渲染
    if (msg.role === "user") {
      el.className = "message user";
      el.textContent = content;
    } else {
      el.className = "message assistant";
      renderMarkdown(el, content);
    }
    nodes.push(el);
  }

  files.forEach((f) => {
    if (!f || !f.url) return;
    const block = document.createElement("div");
    block.className = msg.role === "user" ? "assistant-block is-user-file" : "assistant-block";
    if (msg.role !== "user" && isImageFile(f.filename)) {
      const img = document.createElement("img");
      img.className = "file-preview";
      img.alt = f.filename || "图片";
      img.src = f.url;
      block.appendChild(img);
    }
    block.appendChild(buildFileCard(f.filename, f.url, f.size || 0, msg.role === "user"));
    nodes.push(block);
  });
  reports.forEach((r) => {
    const block = document.createElement("div");
    block.className = "assistant-block";
    block.appendChild(buildReportCard(r));
    nodes.push(block);
  });
  return nodes;
}

/** 插在已有消息**之前**：回填还没回来、用户已经发了一条时，顺序才不会颠倒。 */
function insertHistoryNodes(nodes) {
  const frag = document.createDocumentFragment();
  nodes.forEach((n) => frag.appendChild(n));
  const anchor = chatContainer.querySelector(
    ".message, .assistant-block, .file-download, .typing-indicator, .challenge-card, .history-divider"
  );
  if (anchor) chatContainer.insertBefore(frag, anchor);
  else chatContainer.appendChild(frag);
}

async function loadHistory() {
  const uid = Identity.userId;
  // 宿主每次开窗都会重发 auth，同一用户只回填一次
  if (!uid || historyLoadedFor === uid) return;
  historyLoadedFor = uid;
  try {
    const resp = await fetch("api/chat/history", { headers: authHeaders() });
    if (resp.status === 401) {
      onUnauthorized();
      return;
    }
    if (!resp.ok) throw new Error("HTTP " + resp.status);
    const data = await resp.json();
    const messages = Array.isArray(data.messages) ? data.messages : [];
    // 请求飞行途中宿主可能已经切了账号，这份历史就不是当前用户的了
    if (Identity.userId !== uid) return;
    // 空会话才显示「您可以这样问我」（entries.js）
    document.dispatchEvent(new CustomEvent("csagent:history", { detail: { count: messages.length } }));
    if (messages.length) {
      const nodes = messages.flatMap(buildHistoryNodes);
      const divider = document.createElement("div");
      divider.className = "history-divider";
      divider.textContent = "以上为历史消息";
      nodes.push(divider);
      insertHistoryNodes(nodes);
      // 首屏期间同步设 scrollTop 会被浏览器随后的布局/滚动恢复抹掉（实测停在顶部），
      // 等下一帧再滚。
      requestAnimationFrame(() => {
        chatContainer.scrollTop = chatContainer.scrollHeight;
      });
    }
  } catch (e) {
    // 回填失败不挡输入（后端记忆没丢，模型仍有上下文）；下次开窗再试
    historyLoadedFor = "";
    console.warn("[csagent] 历史回填失败：", e);
  }
  // 历史接口失败时卡片仍要画出来，否则刷新后无法批准
  if (Identity.userId === uid) await loadPendingChallenges();
}

function createAssistantBlock() {
  const block = document.createElement("div");
  block.className = "assistant-block";

  const progressEl = document.createElement("div");
  progressEl.className = "progress-banner";
  block.appendChild(progressEl);

  const reasoningPanel = document.createElement("details");
  reasoningPanel.className = "reasoning-panel";
  reasoningPanel.open = true;
  const reasoningSummary = document.createElement("summary");
  reasoningSummary.textContent = "深度思考中…";
  reasoningPanel.appendChild(reasoningSummary);
  const reasoningBody = document.createElement("div");
  reasoningBody.className = "reasoning-body";
  reasoningPanel.appendChild(reasoningBody);
  block.appendChild(reasoningPanel);

  let thoughtSteps = null;
  if (showThinkingProcess) {
    const thoughtPanel = document.createElement("details");
    thoughtPanel.className = "thought-panel is-enabled";
    thoughtPanel.open = false;
    const summary = document.createElement("summary");
    summary.textContent = "处理过程";
    thoughtPanel.appendChild(summary);
    thoughtSteps = document.createElement("div");
    thoughtSteps.className = "thought-steps";
    thoughtPanel.appendChild(thoughtSteps);
    block.appendChild(thoughtPanel);
  }

  const assistantEl = document.createElement("div");
  assistantEl.className = "message assistant";
  assistantEl.style.display = "none";
  block.appendChild(assistantEl);

  const timingEl = document.createElement("div");
  timingEl.className = "timing-badge";
  timingEl.style.display = "none";
  block.appendChild(timingEl);

  let markdownBuffer = "";   // 已经吐到屏幕上的正文
  let pendingText = "";      // 收到了但还没吐出来的，由打字机逐帧消费
  let reasoningBuffer = "";
  let lastReasoningAt = 0;

  // 后端不再逐字流正文：带 tools 的那一轮无法预知这次是要答话还是要调工具，
  // 边收边推会把「我先确认一下」这类旁白留在屏幕上，所以正文改成确认本轮
  // 没有 tool_calls 之后一次性下发。打字机在这里补回逐字观感——纯展示层的
  // 事，不影响任何数据。
  let typeBase = 0;          // 本段动画开始时已渲染的字数
  let typeStartedAt = 0;
  let typeRaf = null;
  let lastRenderAt = 0;
  let onTypedDone = null;    // 打字机排空后要做的事（如显示耗时）

  function stickToBottom() {
    // 只在用户本来就贴着底部时才跟随，否则他往上翻看历史会被一直拽回来
    const gap =
      chatContainer.scrollHeight - chatContainer.scrollTop - chatContainer.clientHeight;
    if (gap < 120) chatContainer.scrollTop = chatContainer.scrollHeight;
  }

  function typeTick() {
    typeRaf = null;
    // 标签页被切到后台时 rAF 会被暂停甚至完全不触发，动画停在半截、
    // 正文就少一块。后台没人看动画，直接落地全文。
    if (document.hidden) {
      flushTyping();
      return;
    }
    const now = performance.now();
    const total = markdownBuffer.length + pendingText.length;
    const span = total - typeBase;              // 这段动画要吐的总字数
    // 按总时长推进而不是固定每帧字数：短答案不拖沓，长答案也不会吐上十几秒
    const dur = Math.min(2500, Math.max(700, span * 3));
    const ratio = Math.min(1, (now - typeStartedAt) / dur);
    const want = typeBase + Math.ceil(span * ratio);
    const take = want - markdownBuffer.length;
    if (take > 0) {
      markdownBuffer += pendingText.slice(0, take);
      pendingText = pendingText.slice(take);
    }
    // renderMarkdown 要跑 marked + DOMPurify，按帧重排太贵，限到 ~30ms 一次
    if (!pendingText || now - lastRenderAt > 30) {
      lastRenderAt = now;
      renderMarkdown(assistantEl, markdownBuffer);
      stickToBottom();
    }
    if (pendingText) {
      typeRaf = requestAnimationFrame(typeTick);
    } else if (onTypedDone) {
      const fn = onTypedDone;
      onTypedDone = null;
      fn();
    }
  }

  function flushTyping() {
    if (typeRaf !== null) {
      cancelAnimationFrame(typeRaf);
      typeRaf = null;
    }
    if (pendingText) {
      markdownBuffer += pendingText;
      pendingText = "";
      renderMarkdown(assistantEl, markdownBuffer);
      stickToBottom();
    }
    onTypedDone = null;
  }

  chatContainer.appendChild(block);
  chatContainer.scrollTop = chatContainer.scrollHeight;

  // 待办和「正在…」是两条进度。后到的不能把先到的整段换掉。
  let checklistText = "";
  let statusText = "";

  return {
    thoughtSteps,
    setProgress(text) {
      const body = String(text || "").trim();
      if (!body) return;
      const head = body.split("\n", 1)[0].trim();
      if (head === "待办") {
        // 全部完成时只剩标题「待办」，留着上一份清单，避免被收成一行。
        if (body !== "待办") checklistText = body;
      } else {
        statusText = body;
      }
      const shown = checklistText && statusText
        ? checklistText + "\n\n" + statusText
        : (checklistText || statusText);
      progressEl.classList.add("is-visible");
      progressEl.textContent = shown;
      chatContainer.scrollTop = chatContainer.scrollHeight;
    },
    hideProgress() {
      progressEl.classList.remove("is-visible");
    },
    /** 停止时若还一个字都没出（只有进度条），把空气泡整个拿掉。 */
    removeIfEmpty() {
      if (!pendingText && !assistantEl.textContent.trim() && !block.querySelector(".thought-step")
          && !(reasoningBody.textContent || "").trim()) {
        block.remove();
      }
    },
    appendThought(text) {
      if (!thoughtSteps) return;
      const step = document.createElement("div");
      step.className = "thought-step";
      step.textContent = text;
      thoughtSteps.appendChild(step);
    },
    appendReasoning(text) {
      if (!text) return;
      reasoningPanel.classList.add("is-visible");
      // 思考框限高且自带滚动条。只滚外层聊天区的话，框里一直停在开头几行，
      // 新内容都藏在框内下方，看起来就像「只推了前半段然后卡住」。
      // 用户在框里往上翻看时不强行拽回（同 stickToBottom 的口径）。
      const innerGap =
        reasoningBody.scrollHeight - reasoningBody.scrollTop - reasoningBody.clientHeight;
      const followInner = innerGap < 40;
      // 多轮 ReAct 的思考是分段到达的（中间隔着工具执行），段间空一行
      const now = Date.now();
      if (reasoningBuffer && now - lastReasoningAt > 1500 && !reasoningBuffer.endsWith("\n")) {
        reasoningBuffer += "\n\n";
      }
      lastReasoningAt = now;
      reasoningBuffer += text;
      reasoningBody.textContent = reasoningBuffer;
      reasoningSummary.textContent = "深度思考中…";
      if (followInner) reasoningBody.scrollTop = reasoningBody.scrollHeight;
      stickToBottom();
    },
    finishReasoning() {
      if (!reasoningBuffer) return;
      reasoningPanel.classList.add("is-done");
      reasoningSummary.textContent = "深度思考完成";
      reasoningPanel.open = false;
    },
    appendDelta(text) {
      if (!text) return;
      this.hideProgress();
      this.finishReasoning();
      assistantEl.style.display = "";
      pendingText += text;
      if (document.hidden) {
        flushTyping();
        return;
      }
      if (typeRaf === null) {
        typeBase = markdownBuffer.length;
        typeStartedAt = performance.now();
        typeRaf = requestAnimationFrame(typeTick);
      }
    },
    flush: flushTyping,
    showTiming(ms) {
      this.hideProgress();
      this.finishReasoning();
      const apply = () => {
        timingEl.textContent = (ms / 1000).toFixed(1) + "s";
        timingEl.style.display = "";
      };
      // 字还没吐完就挂耗时会显得先斩后奏，等排空再挂
      if (pendingText) onTypedDone = apply;
      else apply();
    },
  };
}

function showTyping() {
  const el = document.createElement("div");
  el.className = "typing-indicator";
  el.id = "typing";
  // 通用文案：前端不区分形态（产品 / 投研），也不知道这一问要不要查数据
  el.textContent = "正在分析问题";
  chatContainer.appendChild(el);
  chatContainer.scrollTop = chatContainer.scrollHeight;
}

function removeTyping() {
  document.getElementById("typing")?.remove();
}

function autoGrow() {
  messageInput.style.height = "auto";
  messageInput.style.height = Math.min(messageInput.scrollHeight, 120) + "px";
}

async function sendMessage() {
  const message = messageInput.value.trim();
  if ((!message && !pendingFiles.length) || isStreaming) return;

  // 没拿到登录态就不让发。注意这只是 UX 闸门，真正的边界在服务端验签。
  if (!Identity.ready || !Identity.sessionId) {
    appendMessage("error", "尚未获取到登录态，请稍候或在主产品中重新登录。");
    return;
  }
  if (ServerAuth.enabled && !Identity.token) {
    appendMessage("error", "缺少访问令牌，请在主产品中重新登录。");
    return;
  }

  messageInput.value = "";
  autoGrow();
  messageInput.blur();
  if (message) {
    appendMessage("user", message);
  }

  await streamTurn(async () => {
    const uploadedFiles = await uploadPendingFiles();
    for (const f of uploadedFiles) {
      if (f.download_url) {
        appendFileDownload(f.filename, f.download_url, true);
      }
    }
    pendingFiles = [];
    renderPendingFiles();
    return {
      // session_id 已由服务端从验签身份派生，前端传了也没用
      message: message,
      channel: "internal_agent",
      attachment_paths: uploadedFiles.map((x) => x.workspace_path),
    };
  });
}

/* 卡片批准并执行后续跑原任务：正文由服务端给（执行结果说明），这里只带 task_id。
 * 和普通一轮共用同一套流式渲染、停止与卡片逻辑。 */
function resumeAfterApproval(taskId) {
  return streamTurn(async () => ({
    message: "",
    channel: "internal_agent",
    attachment_paths: [],
    resume_task_id: taskId,
  }));
}

/* 跑一轮流式对话。buildBody 在占住会话后执行（可以在里面上传附件），返回请求体。 */
async function streamTurn(buildBody) {
  if (isStreaming) return;
  isStreaming = true;
  stopRequested = false;
  document.dispatchEvent(new CustomEvent("csagent:turn"));
  streamAbort = new AbortController();
  HostBridge.post("busy", { busy: true });
  sendBtn.disabled = true;
  setIdentityInputsDisabled(true);
  showTyping();

  let assistantBlock = null;
  const startTime = Date.now();

  try {
    const requestBody = await buildBody();

    // 附件上传完才允许停止：停止针对的是服务端这一轮
    setSendButtonMode(true);
    const response = await fetch("api/chat/stream", {
      method: "POST",
      signal: streamAbort.signal,
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(requestBody),
    });

    if (response.status === 401) {
      onUnauthorized();
      throw new Error("登录态已失效，请重新登录");
    }
    if (response.status === 409) {
      const body = await response.json().catch(() => ({}));
      removeTyping();
      appendBusyNotice(body.detail);
      return;
    }
    if (!response.ok) throw new Error("HTTP " + response.status);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let sawTerminal = false;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        let data;
        try {
          data = JSON.parse(line.slice(6));
        } catch {
          continue;
        }

        if (data.type === "progress" || data.type === "thought" || data.type === "reasoning" || data.type === "delta") {
          if (!assistantBlock) {
            removeTyping();
            assistantBlock = createAssistantBlock();
          }
          if (data.type === "progress") assistantBlock.setProgress(data.content || "");
          if (data.type === "thought") assistantBlock.appendThought(data.content || "");
          if (data.type === "reasoning") assistantBlock.appendReasoning(data.content || "");
          if (data.type === "delta") assistantBlock.appendDelta(data.content || "");
        } else if (data.type === "challenge") {
          removeTyping();
          appendChallengeCard(data, false);
        } else if (data.type === "file") {
          removeTyping();
          appendFileDownload(data.filename || data.content || "附件", data.url || "#", false, data.size || 0);
        } else if (data.type === "report") {
          removeTyping();
          appendReportCard(data);
        } else if (data.type === "done") {
          sawTerminal = true;
          if (assistantBlock) assistantBlock.hideProgress();
        } else if (data.type === "stopped") {
          sawTerminal = true;
          removeTyping();
          if (assistantBlock) {
            assistantBlock.hideProgress();
            assistantBlock.flush();
            assistantBlock.removeIfEmpty();
          }
          appendMessage("error", "已停止本轮");
        } else if (data.type === "error") {
          sawTerminal = true;
          removeTyping();
          if (assistantBlock) {
            assistantBlock.flush();
            assistantBlock.showTiming(Date.now() - startTime);
          }
          appendMessage("error", data.error || "未知错误");
        }
      }
    }

    removeTyping();
    if (assistantBlock) {
      assistantBlock.hideProgress();
      assistantBlock.flush();
      assistantBlock.showTiming(Date.now() - startTime);
    }
    // 没收到 done/error 就结束，是连接被拆掉，不是模型正常说完。
    if (!sawTerminal) {
      appendMessage("error", "连接中断，回答未完成");
    }
  } catch (err) {
    removeTyping();
    if (assistantBlock) {
      assistantBlock.hideProgress();
      assistantBlock.flush();   // 连接断了就别再演打字动画，把收到的落地
      assistantBlock.showTiming(Date.now() - startTime);
    }
    if (stopRequested && err && err.name === "AbortError") {
      appendMessage("error", "已停止本轮");
    } else {
      appendMessage("error", "请求失败: " + (err.message || err));
    }
  } finally {
    isStreaming = false;
    stopRequested = false;
    streamAbort = null;
    setSendButtonMode(false);
    HostBridge.post("busy", { busy: false });
    sendBtn.disabled = false;
    setIdentityInputsDisabled(false);
    enableChallengeCards();
  }
}

// 必须先拿到 config（里面有鉴权开关），initIdentity 才知道走哪条路
loadChatConfig().then(() => {
  initIdentity();
  sendBtn.addEventListener("click", () => (isStreaming ? stopCurrentTurn() : sendMessage()));
  attachBtn.addEventListener("click", () => fileInput.click());
  fileInput.addEventListener("change", () => {
    for (const file of fileInput.files || []) {
      pendingFiles.push({ file });
    }
    fileInput.value = "";
    renderPendingFiles();
  });
  messageInput.addEventListener("input", autoGrow);
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
});
