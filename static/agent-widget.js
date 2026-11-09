/*!
 * 投研助手 · 宿主接入 SDK
 * ---------------------------------------------------------------------------
 * 给宿主产品用的一个文件。它负责：气泡按钮、弹窗容器、iframe、postMessage 握手。
 * 宿主只需要提供「当前登录用户是谁」。
 *
 * 用法：
 *
 *   <script src="https://{智能体域名}/csagent/agent-widget.js"></script>
 *   <script>
 *     CsAgentWidget.init({
 *       // origin 与 path 都会从本脚本自己的 URL 推出来，通常不用填。
 *       // 上面这个 src 就已经说明了服务在哪、聊天页挂在哪个前缀下。
 *       getAuth: function () {
 *         return {
 *           userId:   currentUser.id,       // 必填：稳定的用户主键
 *           userName: currentUser.name,
 *           token:    currentUser.token
 *         };
 *       }
 *     });
 *   </script>
 *
 * 换账号：CsAgentWidget.refreshAuth()
 * 登出：  CsAgentWidget.logout()
 * ---------------------------------------------------------------------------
 */
(function (global) {
  "use strict";

  var WIDGET_SOURCE = "csagent-widget";
  var HOST_SOURCE = "csagent-host";

  /* ---------------------------------------------------------------------
   * 自我定位：从本脚本自己的 URL 推出智能体服务的 origin 与挂载路径。
   *
   * 线上 nginx 把 /csagent 转给智能体服务，本文件的地址是
   *   https://lab.leadfund.com.cn/csagent/agent-widget.js
   * 那么 origin 就是 https://lab.leadfund.com.cn，聊天页在 /csagent/。
   *
   * 这两个值本来要宿主手填（origin + path），但填错的代价很隐蔽——path 漏填时
   * iframe 会去加载宿主自己的首页，页面能出来、就是不对，很难查。SDK 自己就是
   * 从那个地址加载的，没理由让接入方再抄一遍。
   *
   * 必须在脚本**执行期**取 currentScript，init() 里再取就已经是 null 了。
   * ------------------------------------------------------------------- */
  var SELF = (function () {
    var src = "";
    if (document.currentScript && document.currentScript.src) {
      src = document.currentScript.src;
    } else {
      // 动态插入的 async 脚本拿不到 currentScript，退回按文件名找
      var list = document.getElementsByTagName("script");
      for (var i = list.length - 1; i >= 0; i--) {
        if (list[i].src && list[i].src.indexOf("agent-widget.js") !== -1) {
          src = list[i].src;
          break;
        }
      }
    }
    if (!src) return { origin: "", path: "" };
    try {
      var u = new URL(src, location.href);
      return {
        origin: u.origin,
        // 去掉文件名，留目录部分并保证以 / 结尾
        path: u.pathname.replace(/[^/]*$/, "") || "/",
      };
    } catch (e) {
      return { origin: "", path: "" };
    }
  })();

  var cfg = null;
  var els = {};
  var ready = false;
  var busy = false;
  var open = false;
  // init() 后、DOM ready 前就调 open() 的情况：先记下来，build 完再补开。
  // 否则会被 open() 里的 els.panel 判空静默吞掉，宿主很难查。
  var pendingOpen = false;

  // 拖动/缩放后的几何。null = 还没动过，仍按 right/bottom 贴边。
  var geom = null;
  var MIN_W = 320, MIN_H = 380;

  function css(el, styles) {
    for (var k in styles) if (styles.hasOwnProperty(k)) el.style[k] = styles[k];
    return el;
  }

  function build() {
    var side = cfg.position === "left" ? "left" : "right";

    // 气泡
    var launcher = document.createElement("button");
    launcher.type = "button";
    launcher.setAttribute("aria-label", cfg.title);
    launcher.title = cfg.title;
    // 图标放在独立节点里：换成自定义图片时只替换它，不碰未读红点
    var icon = document.createElement("span");
    // 用转义而非字面量：宿主页没声明 UTF-8 时，字面量 emoji 会被按 Latin-1 解码成乱码
    icon.textContent = "\uD83D\uDCAC";
    icon.setAttribute("aria-hidden", "true");
    css(icon, { display: "flex", alignItems: "center", justifyContent: "center",
      width: "100%", height: "100%", borderRadius: "50%", overflow: "hidden" });
    launcher.appendChild(icon);
    css(launcher, {
      position: "fixed", bottom: "24px", zIndex: String(cfg.zIndex),
      width: "52px", height: "52px", borderRadius: "50%",
      background: cfg.themeColor, color: "#fff", fontSize: "22px",
      border: "0", cursor: "pointer",
      boxShadow: "0 6px 20px rgba(0,21,41,.28)",
      display: "flex", alignItems: "center", justifyContent: "center",
    });
    launcher.style[side] = "24px";
    launcher.style.padding = "0";

    // 未读红点
    var badge = document.createElement("span");
    css(badge, {
      position: "absolute", top: "2px", right: "2px",
      width: "10px", height: "10px", borderRadius: "50%",
      background: "#ff4d4f", display: "none",
    });
    launcher.appendChild(badge);

    // 弹窗
    var panel = document.createElement("div");
    css(panel, {
      position: "fixed", bottom: "88px", zIndex: String(cfg.zIndex),
      width: cfg.width, height: cfg.height,
      maxWidth: "calc(100vw - 32px)", maxHeight: "calc(100vh - 120px)",
      borderRadius: "10px", overflow: "hidden", background: "#fff",
      boxShadow: "0 12px 40px rgba(0,21,41,.22)", display: "none",
    });
    panel.style[side] = "24px";

    var iframe = document.createElement("iframe");
    iframe.src = cfg.origin + cfg.path;
    iframe.title = cfg.title;
    // 需要脚本与同源 XHR（对 iframe 自身域而言）；不给 top-navigation
    iframe.setAttribute("allow", "clipboard-write");
    css(iframe, { width: "100%", height: "100%", border: "0", display: "block" });
    panel.appendChild(iframe);

    document.body.appendChild(launcher);
    document.body.appendChild(panel);

    els = { launcher: launcher, icon: icon, badge: badge, panel: panel, iframe: iframe };
    loadBranding();
    addResizeHandles();
    // 恢复上次拖动/缩放的位置，重开还在原地
    var saved = loadGeom();
    if (saved) { geom = saved; applyGeom(); }

    launcher.addEventListener("click", function () {
      open ? api.close() : api.open();
    });

  }

  /* =======================================================================
   * 悬浮图标：管理后台可上传，读公开的 api/branding。
   * 取不到、未配置、图片加载失败，一律保持默认 💬，不影响其它功能。
   * ===================================================================== */
  function loadBranding() {
    if (!global.fetch) return;
    var base = cfg.origin + cfg.path;
    global.fetch(base + "api/branding", { credentials: "omit" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (b) {
        if (!b || !b.icon_url || !els.icon) return;
        var img = document.createElement("img");
        img.alt = "";
        img.draggable = false;
        // fill：铺满圆形按钮；center：保留主题色圆底，图标居中缩小
        var fill = b.icon_mode === "fill";
        css(img, fill
          ? { width: "100%", height: "100%", objectFit: "cover", display: "block" }
          : { width: "60%", height: "60%", objectFit: "contain", display: "block" });
        img.onload = function () {
          els.icon.textContent = "";
          els.icon.appendChild(img);
          if (fill) els.launcher.style.background = "transparent";
        };
        img.src = base + b.icon_url;
      })
      .catch(function () { /* 保持默认图标 */ });
  }

  /* =======================================================================
   * 拖动 / 缩放
   * -----------------------------------------------------------------------
   * 两个必须处理的坑：
   *
   * 1) iframe 会吞事件。指针一旦移到 iframe 上，宿主文档就收不到 mousemove，
   *    拖拽会「粘住」。所以整个手势期间盖一层全屏透明遮罩，事件走遮罩。
   * 2) 面板默认是 right/bottom 贴边定位，拖动后必须切成 left/top，
   *    否则改 left 不生效、或者缩放时朝反方向长。
   * =====================================================================*/

  var GEOM_KEY = "csagent_widget_geom";

  function loadGeom() {
    try {
      var raw = localStorage.getItem(GEOM_KEY);
      if (!raw) return null;
      var g = JSON.parse(raw);
      if (typeof g.w !== "number" || typeof g.h !== "number") return null;
      return g;
    } catch (e) { return null; }
  }

  function saveGeom() {
    if (!geom) return;
    try { localStorage.setItem(GEOM_KEY, JSON.stringify(geom)); } catch (e) {}
  }

  /** 把当前贴边定位换算成绝对 left/top/width/height，之后统一按绝对值管理。 */
  function detachFromCorner() {
    if (geom) return;
    var r = els.panel.getBoundingClientRect();
    geom = { x: r.left, y: r.top, w: r.width, h: r.height };
    els.panel.style.right = "auto";
    els.panel.style.bottom = "auto";
    applyGeom();
  }

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  function applyGeom() {
    if (!geom) return;
    var maxW = window.innerWidth - 16;
    var maxH = window.innerHeight - 16;
    geom.w = clamp(geom.w, MIN_W, maxW);
    geom.h = clamp(geom.h, MIN_H, maxH);
    // 至少露出一部分，别让面板被拖出屏幕再也点不着
    geom.x = clamp(geom.x, 8 - geom.w + 120, window.innerWidth - 120);
    geom.y = clamp(geom.y, 8, window.innerHeight - 60);

    var st = els.panel.style;
    st.left = geom.x + "px";
    st.top = geom.y + "px";
    st.width = geom.w + "px";
    st.height = geom.h + "px";
    st.right = "auto";
    st.bottom = "auto";
  }

  /** 手势期间的全屏遮罩：接管 mousemove，同时屏蔽 iframe 的事件吞噬。 */
  function withOverlay(cursor, onMove, onEnd) {
    var ov = document.createElement("div");
    css(ov, {
      position: "fixed", inset: "0", zIndex: String(cfg.zIndex + 1),
      cursor: cursor, background: "transparent",
    });
    document.body.appendChild(ov);
    // 拖拽时 iframe 内不该出现文字选中
    var prevSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";

    function move(e) { onMove(e); }
    function up(e) {
      document.removeEventListener("mousemove", move);
      document.removeEventListener("mouseup", up);
      if (ov.parentNode) ov.parentNode.removeChild(ov);
      document.body.style.userSelect = prevSelect;
      if (onEnd) onEnd(e);
      saveGeom();
    }
    document.addEventListener("mousemove", move);
    document.addEventListener("mouseup", up);
  }

  /** grabX/grabY：按下点相对面板左上角的偏移，拖动时保持不变才不会「跳一下」。 */
  function startDrag(grabX, grabY) {
    if (!cfg.draggable || !els.panel) return;
    detachFromCorner();
    withOverlay("move", function (e) {
      geom.x = e.clientX - grabX;
      geom.y = e.clientY - grabY;
      applyGeom();
    });
  }

  var HANDLES = [
    { dir: "n",  css: { top: "-4px", left: "8px", right: "8px", height: "8px", cursor: "ns-resize" } },
    { dir: "s",  css: { bottom: "-4px", left: "8px", right: "8px", height: "8px", cursor: "ns-resize" } },
    { dir: "w",  css: { left: "-4px", top: "8px", bottom: "8px", width: "8px", cursor: "ew-resize" } },
    { dir: "e",  css: { right: "-4px", top: "8px", bottom: "8px", width: "8px", cursor: "ew-resize" } },
    { dir: "nw", css: { top: "-4px", left: "-4px", width: "14px", height: "14px", cursor: "nwse-resize" } },
    { dir: "ne", css: { top: "-4px", right: "-4px", width: "14px", height: "14px", cursor: "nesw-resize" } },
    { dir: "sw", css: { bottom: "-4px", left: "-4px", width: "14px", height: "14px", cursor: "nesw-resize" } },
    { dir: "se", css: { bottom: "-4px", right: "-4px", width: "14px", height: "14px", cursor: "nwse-resize" } },
  ];

  function addResizeHandles() {
    if (!cfg.resizable) return;
    HANDLES.forEach(function (h) {
      var el = document.createElement("div");
      css(el, { position: "absolute", zIndex: "2", background: "transparent" });
      css(el, h.css);
      el.addEventListener("mousedown", function (e) {
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        detachFromCorner();
        var s0 = { x: geom.x, y: geom.y, w: geom.w, h: geom.h };
        var mx = e.clientX, my = e.clientY;
        withOverlay(h.css.cursor, function (ev) {
          var dx = ev.clientX - mx, dy = ev.clientY - my;
          if (h.dir.indexOf("e") !== -1) geom.w = s0.w + dx;
          if (h.dir.indexOf("s") !== -1) geom.h = s0.h + dy;
          if (h.dir.indexOf("w") !== -1) {
            // 往左拉：宽度增加的同时左边界左移，右边界保持不动
            geom.w = s0.w - dx;
            geom.x = s0.x + dx;
            if (geom.w < MIN_W) geom.x = s0.x + (s0.w - MIN_W);
          }
          if (h.dir.indexOf("n") !== -1) {
            geom.h = s0.h - dy;
            geom.y = s0.y + dy;
            if (geom.h < MIN_H) geom.y = s0.y + (s0.h - MIN_H);
          }
          applyGeom();
        });
      });
      els.panel.appendChild(el);
    });
  }

  function post(type, payload) {
    if (!els.iframe || !els.iframe.contentWindow) return;
    try {
      els.iframe.contentWindow.postMessage(
        { source: HOST_SOURCE, type: type, payload: payload || {} },
        cfg.origin
      );
    } catch (e) {
      if (cfg.debug) console.warn("[CsAgentWidget] postMessage 失败", e);
    }
  }

  function sendAuth() {
    var auth;
    try {
      auth = cfg.getAuth();
    } catch (e) {
      console.error("[CsAgentWidget] getAuth() 抛错", e);
      return;
    }
    if (!auth || !auth.userId) {
      console.error("[CsAgentWidget] getAuth() 必须返回 userId，否则小窗无法建立会话");
      return;
    }
    if (cfg.title) auth.title = auth.title || cfg.title;
    post("auth", auth);
    if (cfg.debug) console.info("[CsAgentWidget] 已下发 auth: userId=" + auth.userId);
  }

  function onMessage(e) {
    // 只认智能体服务的 origin，防止别的页面伪造小窗消息
    if (e.origin !== cfg.origin) return;
    var msg = e.data;
    if (!msg || typeof msg !== "object" || msg.source !== WIDGET_SOURCE) return;

    switch (msg.type) {
      case "ready":
        ready = true;
        sendAuth();
        break;
      case "close":
        if (busy && cfg.confirmCloseWhenBusy &&
            !global.confirm("回复还没生成完，确定关闭？")) return;
        api.close();
        break;
      case "drag-start":
        // 小窗标题栏按下了，宿主接管后续拖动
        startDrag((msg.payload && msg.payload.x) || 0, (msg.payload && msg.payload.y) || 0);
        break;
      case "busy":
        busy = !!(msg.payload && msg.payload.busy);
        // 收起状态下回复完成 → 打未读点
        if (!busy && !open) els.badge.style.display = "block";
        if (typeof cfg.onBusyChange === "function") cfg.onBusyChange(busy);
        break;
      default:
        break;
    }
    if (typeof cfg.onMessage === "function") cfg.onMessage(msg);
  }

  var api = {
    init: function (options) {
      if (cfg) {
        console.warn("[CsAgentWidget] 已初始化过，忽略重复调用");
        return api;
      }
      options = options || {};
      if (!options.origin && !SELF.origin) {
        throw new Error(
          "[CsAgentWidget] 推不出 origin（拿不到本脚本 URL），请显式传 origin"
        );
      }
      if (typeof options.getAuth !== "function") {
        throw new Error("[CsAgentWidget] 必须传 getAuth()，返回 {userId, ...}");
      }
      // 显式传的 origin 与脚本实际来源不一致，多半是抄配置时抄错了域名。
      // 不拦（可能是有意的反代场景），但要吭一声——否则表现是 iframe 白屏。
      if (options.origin && SELF.origin &&
          String(options.origin).replace(/\/+$/, "") !== SELF.origin) {
        console.warn(
          "[CsAgentWidget] 传入 origin=" + options.origin +
          " 与本脚本来源 " + SELF.origin + " 不一致，确认是否写错"
        );
      }
      cfg = {
        origin: String(options.origin || SELF.origin).replace(/\/+$/, ""),
        // 默认取本脚本所在目录（线上即 /csagent/），不是写死的 "/"
        path: options.path || SELF.path || "/",
        getAuth: options.getAuth,
        title: options.title || "投研助手",
        width: options.width || "400px",
        height: options.height || "600px",
        position: options.position || "right",
        themeColor: options.themeColor || "#001529",
        zIndex: options.zIndex || 9999,
        confirmCloseWhenBusy: options.confirmCloseWhenBusy !== false,
        draggable: options.draggable !== false,
        resizable: options.resizable !== false,
        onBusyChange: options.onBusyChange,
        onMessage: options.onMessage,
        debug: !!options.debug,
      };

      function boot() {
        build();
        global.addEventListener("message", onMessage);
        if (options.openOnInit || pendingOpen) api.open();
      }
      if (document.readyState === "loading") {
        document.addEventListener("DOMContentLoaded", boot);
      } else {
        boot();
      }
      return api;
    },

    open: function () {
      if (!els.panel) { pendingOpen = true; return; }
      pendingOpen = false;
      open = true;
      els.panel.style.display = "block";
      els.badge.style.display = "none";
      if (ready) sendAuth();   // 每次打开顺带刷新登录态
    },

    close: function () {
      pendingOpen = false;
      if (!els.panel) return;
      open = false;
      els.panel.style.display = "none";
    },

    /** 切账号 / token 续期后调用。不会打断小窗里正在进行的对话。 */
    refreshAuth: function () {
      if (ready) sendAuth();
    },

    logout: function () {
      post("logout", {});
      api.close();
    },

    isBusy: function () { return busy; },

    /** 拖乱了/拖丢了，一键回到右下角默认尺寸。 */
    resetPosition: function () {
      geom = null;
      try { localStorage.removeItem(GEOM_KEY); } catch (e) {}
      if (!els.panel) return;
      var st = els.panel.style;
      st.left = "auto"; st.top = "auto";
      st.width = cfg.width; st.height = cfg.height;
      st[cfg.position === "left" ? "left" : "right"] = "24px";
      st.bottom = "88px";
    },
  };

  global.CsAgentWidget = api;
})(window);
