/* =====================================================================
 * 报告卡片 + 报告面板 + 文件卡片（docs/对话页升级方案.md §3.1 / §3.6）
 *
 * 用到 app.js 的全局：chatContainer、authHeaders、renderMarkdown、EMBEDDED、
 * onUnauthorized。对外只挂三个函数到 window：
 *   buildFileCard(filename, url, size, isUser) → 元素（PDF / Excel / CSV 点开在面板里预览）
 *   buildReportCard(card)                      → 元素（标题 + 内容缩略图）
 *   appendReportCard(card)                     → 插进聊天区
 * 面板共用一个：报告和文件预览轮流用，showPanel() 每次打开递增序号，迟到的异步结果自行作废。
 * 图表 SVG 由服务端画好（与 PDF 同一份），这里嵌进去并接上悬停 / 点按提示。
 * 报告骨架固定：报告头 → 核心观点 → 目录 → 编号章节 → 风险提示 → 数据来源。
 * ===================================================================*/
(function () {
  "use strict";

  const WIDE_MIN = 960;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function fmtSize(n) {
    if (!n) return "";
    if (n < 1024) return n + " B";
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10 * 1024 ? 1 : 0) + " KB";
    return (n / 1024 / 1024).toFixed(1) + " MB";
  }

  const KINDS = [
    [/\.pdf$/i, "PDF", "is-pdf"],
    [/\.(xlsx|xls|csv)$/i, "Excel", "is-excel"],
    [/\.(docx?|rtf)$/i, "Word", "is-word"],
    [/\.(pptx?)$/i, "PPT", "is-ppt"],
    [/\.(png|jpe?g|gif|webp|bmp|svg)$/i, "图片", "is-image"],
    [/\.(zip|rar|7z|gz)$/i, "压缩包", "is-zip"],
    [/\.(md|txt|json|html?)$/i, "文本", "is-text"],
  ];

  function fileKind(filename) {
    for (const [re, label, cls] of KINDS) if (re.test(filename || "")) return { label, cls };
    return { label: "文件", cls: "" };
  }

  function extLabel(filename) {
    const m = /\.([a-z0-9]{1,5})$/i.exec(filename || "");
    return m ? m[1].toUpperCase() : "FILE";
  }

  const DOWNLOAD_ICON =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M12 4v11m0 0 4.5-4.5M12 15l-4.5-4.5M5 19h14" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg>';
  const REPORT_ICON =
    '<svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true"><path d="M7 3h7l5 5v12a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z M14 3v5h5 M9 13h6 M9 17h4" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/></svg>';

  /* ------------------------------------------------------------ 文件卡片 */

  const PREVIEW_PDF_RE = /\.pdf$/i;
  const PREVIEW_SHEET_RE = /\.(xlsx|xlsm|csv)$/i;

  function canPreview(filename) {
    return PREVIEW_PDF_RE.test(filename || "") || PREVIEW_SHEET_RE.test(filename || "");
  }

  /** 文件卡片。PDF / Excel / CSV 点卡片在面板里预览，右侧图标下载；其余类型点卡片直接下载。 */
  function buildFileCard(filename, url, size, isUser) {
    const kind = fileKind(filename);
    const usable = url && url !== "#";
    const preview = usable && canPreview(filename);
    const card = el(preview ? "div" : "a", "file-card " + kind.cls + (isUser ? " is-user" : "") + (preview ? " can-preview" : ""));
    if (!preview) {
      card.href = url || "#";
      card.setAttribute("download", filename || "");
    }
    const badge = el("span", "file-card-badge", extLabel(filename));
    const body = el("span", "file-card-body");
    body.appendChild(el("span", "file-card-name", filename || "附件"));
    const meta = [kind.label, fmtSize(size)].filter(Boolean).join(" · ");
    body.appendChild(el("span", "file-card-meta", isUser ? "我发送的" + (meta ? " · " + meta : "") : meta));
    const action = el(preview ? "a" : "span", "file-card-action");
    action.innerHTML = DOWNLOAD_ICON;
    card.append(badge, body, action);
    if (preview) {
      card.setAttribute("role", "button");
      card.tabIndex = 0;
      card.title = "预览";
      action.href = url;
      action.setAttribute("download", filename || "");
      action.title = "下载";
      action.setAttribute("aria-label", "下载 " + (filename || "文件"));
      action.addEventListener("click", (e) => e.stopPropagation());
      const open = () => openFilePreview(filename, url, size);
      card.addEventListener("click", open);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
    }
    return card;
  }

  /* ------------------------------------------------------------ 导出 */

  async function exportReport(reportId, version, fmt, button) {
    const label = button.textContent;
    button.disabled = true;
    button.textContent = "生成中…";
    try {
      const q = "fmt=" + fmt + (version ? "&v=" + version : "");
      const resp = await fetch("api/reports/" + encodeURIComponent(reportId) + "/export?" + q, {
        headers: authHeaders(),
      });
      if (resp.status === 401) {
        onUnauthorized();
        return;
      }
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || "导出失败 HTTP " + resp.status);
      const a = document.createElement("a");
      a.href = data.url;
      a.setAttribute("download", data.filename || "");
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (err) {
      toast(err.message || "导出失败");
    } finally {
      button.disabled = false;
      button.textContent = label;
    }
  }

  let toastTimer = 0;
  function toast(text) {
    let node = document.getElementById("rp-toast");
    if (!node) {
      node = el("div", "rp-toast");
      node.id = "rp-toast";
      document.body.appendChild(node);
    }
    node.textContent = text;
    node.classList.add("is-show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => node.classList.remove("is-show"), 2400);
  }

  /** 面板头部唯一的操作：「导出」，点开选 PDF / Excel（有表格才有）/ 复制文本。 */
  function exportMenu(reportId, version, hasTables, getText) {
    const wrap = el("div", "rp-menu");
    const btn = el("button", "rp-btn is-primary rp-menu-btn", "导出");
    btn.type = "button";
    btn.setAttribute("aria-haspopup", "menu");
    const list = el("div", "rp-menu-list");
    list.setAttribute("role", "menu");
    const close = () => wrap.classList.remove("is-open");
    const item = (label, sub, run) => {
      const it = el("button", "rp-menu-item");
      it.type = "button";
      it.setAttribute("role", "menuitem");
      it.appendChild(el("span", null, label));
      if (sub) it.appendChild(el("small", null, sub));
      it.addEventListener("click", (e) => {
        e.stopPropagation();
        close();
        run();
      });
      list.appendChild(it);
    };
    item("PDF", "排好版的报告", () => exportReport(reportId, version, "pdf", btn));
    if (hasTables) item("Excel", "每张表一个 sheet", () => exportReport(reportId, version, "xlsx", btn));
    item("复制文本", "粘贴到微信 / 邮件", () => copyText(getText()));
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      wrap.classList.toggle("is-open");
    });
    document.addEventListener("click", close);
    wrap.append(btn, list);
    return wrap;
  }

  /* ------------------------------------------------------------ 报告卡片 */

  /** 报告卡片：标题 + 内容缩略（第一张图；没有图就核心观点 / 第一张表）。点开看全文。 */
  function buildReportCard(card) {
    const root = el("div", "report-card");
    root.dataset.reportId = card.report_id;
    root.dataset.version = String(card.version || 1);
    root.setAttribute("role", "button");
    root.tabIndex = 0;

    const head = el("div", "report-card-head");
    const icon = el("span", "report-card-icon");
    icon.innerHTML = REPORT_ICON;
    head.appendChild(icon);
    head.appendChild(el("span", "report-card-kind", "报告"));
    if (card.version > 1) head.appendChild(el("span", "report-card-ver", "已更新 v" + card.version));
    root.appendChild(head);

    root.appendChild(el("div", "report-card-title", card.title || "报告"));
    const thumb = el("div", "report-card-thumb is-loading");
    root.appendChild(thumb);

    const foot = el("div", "report-card-foot", "查看完整报告");
    foot.appendChild(el("span", "report-card-arrow", "›"));
    root.appendChild(foot);

    const open = () => openReport(card.report_id, card.version);
    root.addEventListener("click", open);
    root.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        open();
      }
    });
    watchThumb(thumb, card);
    return root;
  }

  // 缩略图等卡片进入视口再取，历史里一长串报告不会一次全拉
  let thumbObserver = null;
  function watchThumb(thumb, card) {
    if (!("IntersectionObserver" in window)) {
      fillThumb(thumb, card);
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver(
        (entries) => {
          entries.forEach((en) => {
            if (!en.isIntersecting) return;
            thumbObserver.unobserve(en.target);
            fillThumb(en.target, en.target._card);
          });
        },
        { rootMargin: "200px" }
      );
    }
    thumb._card = card;
    thumbObserver.observe(thumb);
  }

  function plainText(md) {
    return String(md || "").replace(/\*\*|__|`/g, "");
  }

  async function fillThumb(thumb, card) {
    let data;
    try {
      data = await fetchReport(card.report_id, card.version);
    } catch (_) {
      thumb.remove();
      return;
    }
    thumb.classList.remove("is-loading");
    const r = data.report;
    const charts = data.charts || {};
    if (r.verdict) thumb.appendChild(el("div", "report-card-verdict", tidyText(r.verdict)));
    // 多对象比较：对决 / 并排卡比第一张图更能说明结论
    for (const sec of numbered(r)) {
      const it = sec.items.find((x) => x.block.type === "entities" && x.block.items.length >= 2 && x.block.metrics.length);
      if (!it) continue;
      const key = String(it.index);
      thumb.classList.add("is-ent");
      thumb.appendChild(renderEntities(it.block, (data.highlights || {})[key], (data.tallies || {})[key], 3));
      return;
    }
    for (const sec of numbered(r)) {
      for (const it of sec.items) {
        const svg = charts[String(it.index)];
        if (it.block.type === "chart" && svg && /^<svg[\s>]/.test(svg)) {
          const box = el("div", "report-card-chart");
          box.innerHTML = svg;
          box.querySelectorAll(".c-hits, .c-dots").forEach((n) => n.remove());
          thumb.appendChild(box);
          thumb.appendChild(el("div", "report-card-cap", (it.label + " " + (it.block.title || "")).trim()));
          return;
        }
      }
    }
    const points = keyPoints(r);
    if (points.length) {
      thumb.classList.add("is-points");
      thumb.appendChild(el("div", "report-card-cap", "核心观点"));
      const ol = el("ol");
      points.slice(0, 3).forEach((p) => ol.appendChild(el("li", null, plainText(p.text))));
      thumb.appendChild(ol);
      return;
    }
    for (const sec of numbered(r)) {
      const it = sec.items.find((x) => x.block.type === "table");
      if (!it) continue;
      const b = it.block;
      const table = el("table");
      const tr = el("tr");
      b.columns.slice(0, 4).forEach((c) => tr.appendChild(el("th", null, c)));
      table.appendChild(tr);
      b.rows.slice(0, 4).forEach((row) => {
        const r2 = el("tr");
        row.slice(0, 4).forEach((v) => r2.appendChild(el("td", null, v == null ? "" : String(v))));
        table.appendChild(r2);
      });
      thumb.classList.add("is-table");
      thumb.appendChild(table);
      return;
    }
    thumb.remove();
  }

  function appendReportCard(card) {
    if (!card || !card.report_id) return null;
    const block = el("div", "assistant-block");
    block.appendChild(buildReportCard(card));
    const typing = document.getElementById("typing");
    if (typing) chatContainer.insertBefore(block, typing);
    else chatContainer.appendChild(block);
    chatContainer.scrollTop = chatContainer.scrollHeight;
    return block;
  }

  /* ------------------------------------------------------------ 面板 */

  // hideTips：本次渲染各图表的「收起提示」，点面板空白处 / 滚动时一起收
  const panel = { root: null, body: null, title: null, meta: null, tools: null, actions: null, current: null, hideTips: [], seq: 0 };

  function ensurePanel() {
    if (panel.root) return panel;
    const mask = el("div", "rp-mask");
    mask.addEventListener("click", closeReport);
    const root = el("aside", "rp");
    root.setAttribute("role", "dialog");
    root.setAttribute("aria-label", "报告");

    const head = el("div", "rp-head");
    const titleWrap = el("div", "rp-titlewrap");
    panel.title = el("h2", "rp-title");
    panel.meta = el("div", "rp-meta");
    titleWrap.append(panel.title, panel.meta);
    const close = el("button", "rp-close", "×");
    close.type = "button";
    close.setAttribute("aria-label", "关闭报告");
    close.addEventListener("click", closeReport);
    panel.actions = el("div", "rp-actions");
    head.append(titleWrap, panel.actions, close);

    panel.tools = el("div", "rp-tools");
    panel.body = el("div", "rp-body");
    const hideTips = () => panel.hideTips.forEach((fn) => fn());
    panel.body.addEventListener("click", hideTips);
    panel.body.addEventListener("scroll", hideTips, { passive: true });
    root.append(head, panel.tools, panel.body);
    document.body.append(mask, root);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && document.body.classList.contains("rp-open")) closeReport();
    });
    panel.root = root;
    return panel;
  }

  function useSide() {
    return !EMBEDDED && window.innerWidth >= WIDE_MIN;
  }

  function closeReport() {
    document.body.classList.remove("rp-open", "rp-side");
    panel.current = null;
    panel.seq += 1; // 让还在加载 / 渲染的预览自行作废
  }

  /** 打开面板并清空，返回本次打开的序号；异步回来时序号变了说明面板已换内容或关了。 */
  function showPanel(label, title) {
    const p = ensurePanel();
    p.seq += 1;
    p.root.setAttribute("aria-label", label);
    document.body.classList.add("rp-open");
    document.body.classList.toggle("rp-side", useSide());
    p.current = null;
    p.hideTips = [];
    p.title.textContent = title;
    p.meta.replaceChildren();
    p.tools.replaceChildren();
    p.actions.replaceChildren();
    p.body.replaceChildren(el("div", "rp-loading", "正在打开…"));
    return p.seq;
  }

  class Unauthorized extends Error {}

  const reportCache = new Map();
  function fetchReport(reportId, version) {
    const key = reportId + "@" + (version || "");
    if (!reportCache.has(key)) {
      const job = fetch("api/reports/" + encodeURIComponent(reportId) + (version ? "?v=" + version : ""), {
        headers: authHeaders(),
      }).then(async (resp) => {
        if (resp.status === 401) throw new Unauthorized();
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(data.detail || "打开失败 HTTP " + resp.status);
        return data;
      });
      job.catch(() => reportCache.delete(key));
      reportCache.set(key, job);
    }
    return reportCache.get(key);
  }

  async function openReport(reportId, version) {
    const seq = showPanel("报告", "加载中…");
    let data;
    try {
      data = await fetchReport(reportId, version);
    } catch (err) {
      if (err instanceof Unauthorized) {
        onUnauthorized();
        return;
      }
      if (seq !== panel.seq) return;
      panel.title.textContent = "报告";
      panel.body.replaceChildren(el("div", "rp-empty", err.message || "打开失败"));
      return;
    }
    if (seq !== panel.seq) return;
    renderReport(data);
  }

  /* ------------------------------------------------------------ 文件预览 */

  const PDFJS_BASE = "assets/vendor/pdfjs-4.10.38/";
  const PDF_MAX_PAGES = 100;
  let pdfjsJob = null;

  /** pdf.js 用到才加载（~400KB + worker），不拖慢首屏。 */
  function loadPdfjs() {
    if (!pdfjsJob) {
      const base = new URL(PDFJS_BASE, document.baseURI).href;
      pdfjsJob = import(base + "pdf.min.mjs").then((lib) => {
        lib.GlobalWorkerOptions.workerSrc = base + "pdf.worker.min.mjs";
        return lib;
      });
      pdfjsJob.catch(() => (pdfjsJob = null));
    }
    return pdfjsJob;
  }

  function downloadLink(filename, url) {
    const a = el("a", "rp-btn", "下载");
    a.href = url;
    a.setAttribute("download", filename || "");
    return a;
  }

  function previewFailed(seq, filename, url, message) {
    if (seq !== panel.seq) return;
    const box = el("div", "rp-empty");
    box.appendChild(el("div", null, message || "预览失败"));
    box.appendChild(downloadLink(filename, url));
    panel.body.replaceChildren(box);
  }

  function openFilePreview(filename, url, size) {
    const seq = showPanel("文件预览", filename || "文件");
    panel.meta.appendChild(el("span", null, fileKind(filename).label));
    if (size) panel.meta.appendChild(el("span", null, fmtSize(size)));
    const dl = downloadLink(filename, url);
    dl.classList.add("is-primary");
    panel.actions.appendChild(dl);
    if (PREVIEW_PDF_RE.test(filename || "")) previewPdf(seq, filename, url);
    else previewSheet(seq, filename, url);
  }

  async function fetchOk(url) {
    const resp = await fetch(url, { headers: authHeaders() });
    if (resp.status === 401) throw new Unauthorized();
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || "HTTP " + resp.status);
    }
    return resp;
  }

  async function previewPdf(seq, filename, url) {
    let doc;
    try {
      const [lib, resp] = await Promise.all([loadPdfjs(), fetchOk(url)]);
      const bytes = new Uint8Array(await resp.arrayBuffer());
      if (seq !== panel.seq) return;
      doc = await lib.getDocument({
        data: bytes,
        cMapUrl: new URL(PDFJS_BASE + "cmaps/", document.baseURI).href,
        cMapPacked: true,
        isEvalSupported: false,
      }).promise;
    } catch (err) {
      if (err instanceof Unauthorized) return onUnauthorized();
      return previewFailed(seq, filename, url, "PDF 预览失败，可以下载后查看");
    }
    if (seq !== panel.seq) return doc.destroy();
    const pages = Math.min(doc.numPages, PDF_MAX_PAGES);
    panel.meta.appendChild(el("span", null, "共 " + doc.numPages + " 页"));
    const first = (await doc.getPage(1)).getViewport({ scale: 1 });
    const wrap = el("div", "pv-pdf");
    const holders = [];
    for (let i = 1; i <= pages; i++) {
      const holder = el("div", "pv-page");
      holder.style.aspectRatio = first.width + " / " + first.height;
      holder.dataset.page = String(i);
      wrap.appendChild(holder);
      holders.push(holder);
    }
    if (doc.numPages > pages) {
      wrap.appendChild(el("div", "pv-note", "仅预览前 " + pages + " 页，完整内容请下载"));
    }
    // 手机上 A4 缩到屏宽字太小：「放大」把页面加宽到 2 倍，横向滑动看，按新宽度重画
    const scroller = el("div", "pv-scroll");
    scroller.appendChild(wrap);
    const zoom = el("button", "rp-btn", "放大");
    zoom.type = "button";
    zoom.addEventListener("click", () => {
      const on = wrap.classList.toggle("is-zoom");
      zoom.textContent = on ? "还原" : "放大";
      holders.forEach((h) => {
        delete h.dataset.drawn;
        if (io) io.observe(h);
      });
      if (!io) holders.forEach((h) => draw(h));
    });
    panel.actions.prepend(zoom);
    panel.body.replaceChildren(scroller);
    panel.body.scrollTop = 0;

    async function draw(holder) {
      if (holder.dataset.drawn || seq !== panel.seq) return;
      holder.dataset.drawn = "1";
      try {
        const page = await doc.getPage(Number(holder.dataset.page));
        const base = page.getViewport({ scale: 1 });
        holder.style.aspectRatio = base.width + " / " + base.height;
        const dpr = Math.min(window.devicePixelRatio || 1, 2);
        const vp = page.getViewport({ scale: (holder.clientWidth * dpr) / base.width });
        const canvas = el("canvas");
        canvas.width = Math.floor(vp.width);
        canvas.height = Math.floor(vp.height);
        await page.render({ canvasContext: canvas.getContext("2d"), viewport: vp }).promise;
        if (seq === panel.seq) holder.replaceChildren(canvas);
      } catch (_) {
        holder.textContent = "第 " + holder.dataset.page + " 页渲染失败";
      }
    }

    let io = null;
    if ("IntersectionObserver" in window) {
      io = new IntersectionObserver(
        (entries) => {
          if (seq !== panel.seq) {
            io.disconnect();
            doc.destroy();
            return;
          }
          entries.forEach((en) => {
            if (en.isIntersecting) {
              io.unobserve(en.target);
              draw(en.target);
            }
          });
        },
        { root: panel.body, rootMargin: "600px 0px" }
      );
      holders.forEach((h) => io.observe(h));
    } else {
      for (const h of holders) await draw(h);
    }
  }

  async function previewSheet(seq, filename, url) {
    let data;
    try {
      const u = new URL(url, document.baseURI);
      u.pathname = u.pathname.replace(/\/download$/, "/preview");
      data = await (await fetchOk(u.href)).json();
    } catch (err) {
      if (err instanceof Unauthorized) return onUnauthorized();
      return previewFailed(seq, filename, url, err.message || "预览失败");
    }
    if (seq !== panel.seq) return;
    const sheets = data.sheets || [];
    if (!sheets.length) return previewFailed(seq, filename, url, "文件里没有工作表");
    const wrap = el("div", "pv-sheet");
    const tabs = el("div", "pv-tabs");
    const view = el("div");
    function show(k) {
      tabs.querySelectorAll(".pv-tab").forEach((t, j) => t.classList.toggle("is-active", j === k));
      view.replaceChildren(sheetTable(sheets[k]));
    }
    if (sheets.length > 1) {
      sheets.forEach((sh, k) => {
        const t = el("button", "pv-tab", sh.name);
        t.type = "button";
        t.addEventListener("click", () => show(k));
        tabs.appendChild(t);
      });
      wrap.appendChild(tabs);
    }
    wrap.appendChild(view);
    panel.body.replaceChildren(wrap);
    panel.body.scrollTop = 0;
    show(0);
  }

  function sheetTable(sheet) {
    const box = el("div");
    const rows = sheet.rows || [];
    if (!rows.length) {
      box.appendChild(el("div", "pv-note", "（空表）"));
      return box;
    }
    const wrap = el("div", "rp-table markdown-body pv-table");
    const table = el("table");
    const thead = el("thead");
    const htr = el("tr");
    rows[0].forEach((c) => htr.appendChild(el("th", null, c)));
    thead.appendChild(htr);
    const tbody = el("tbody");
    rows.slice(1).forEach((row) => {
      const tr = el("tr");
      // 只给显式带正负号的百分数上色，别把所有正数都染红
      row.forEach((c) => tr.appendChild(el("td", /^[+\-−].*%$/.test(c) ? signedClass(c) : "", c)));
      tbody.appendChild(tr);
    });
    table.append(thead, tbody);
    wrap.appendChild(table);
    box.appendChild(wrap);
    if (sheet.truncated) {
      box.appendChild(el("div", "pv-note", "仅预览前 " + rows.length + " 行（共 " + sheet.total_rows + " 行），完整内容请下载"));
    }
    return box;
  }

  /* ------------------------------------------------------------ 数字与段落 */

  // 与 reports/render.py 的 tidy_text 一致：五位数以上的「亿」换成「万亿」
  const YI_RE = /(^|[^\d.,])(\d{1,3}(?:,\d{3})+|\d{5,})(\.\d+)?\s*亿/g;
  function tidyText(text) {
    return String(text == null ? "" : text).replace(YI_RE, (all, pre, int, dec) => {
      const v = Number(int.replace(/,/g, "") + (dec || ""));
      return v < 10000 ? all : pre + String(+(v / 10000).toFixed(2)) + " 万亿";
    });
  }

  /** 单元格显示：与 render.display_cell 一致（18894 → 18,894；文字只换算万亿）。 */
  function cellText(v) {
    if (v == null) return "";
    if (typeof v === "number") return Math.abs(v) >= 10000 ? v.toLocaleString("en-US", { maximumFractionDigits: 6 }) : String(v);
    return tidyText(v);
  }

  // 「**论点。** 展开……」：论点单独一行，展开的话放下面（与 render._claim 一致）
  const CLAIM_MIN_BODY = 8;
  function liftClaims(root) {
    Array.from(root.children).forEach((p) => {
      if (p.tagName !== "P") return;
      let first = p.firstChild;
      while (first && first.nodeType === 3 && !first.textContent.trim()) first = first.nextSibling;
      if (!first || first.nodeName !== "STRONG") return;
      const rest = [];
      for (let n = first.nextSibling; n; n = n.nextSibling) rest.push(n);
      while (rest.length && rest[0].nodeName === "BR") rest.shift();
      if (rest.length && rest[0].nodeType === 3) rest[0].textContent = rest[0].textContent.replace(/^\s*[：:，,]?\s*/, "");
      const bodyLen = rest.reduce((a, n) => a + n.textContent.length, 0);
      if (bodyLen < CLAIM_MIN_BODY) return;
      const claim = el("div", "rp-claim");
      const head = el("div", "rp-claim-head");
      head.append(...Array.from(first.childNodes));
      if (head.lastChild && head.lastChild.nodeType === 3) head.lastChild.textContent = head.lastChild.textContent.replace(/[：:]\s*$/, "");
      const body = el("p", "rp-claim-body");
      body.append(...rest);
      claim.append(head, body);
      p.replaceWith(claim);
    });
  }

  function tallyText(t) {
    if (!t) return "";
    return "胜出项：" + t.wins.map(([n, c]) => n + " " + c).join(" · ") + (t.ties ? "（持平 " + t.ties + "）" : "");
  }

  function cellNumber(v) {
    if (typeof v === "number") return v;
    const m = /^\s*([+\-−–＋]?)\s*(\d[\d,]*(?:\.\d+)?)/.exec(String(v == null ? "" : v));
    if (!m) return null;
    const n = Number(m[2].replace(/,/g, ""));
    return m[1] && /[-−–]/.test(m[1]) ? -n : n;
  }

  const CN_NUM = "一二三四五六七八九十";
  function cnNumber(n) {
    return n <= 10 ? CN_NUM[n - 1] : "十" + CN_NUM[n - 11];
  }

  /** 与服务端 render.numbered 同一口径：章节「一、」，图、表各自全篇连续编号。
   *  旧报告（只有平铺 blocks）当成一个无标题章节。 */
  function numbered(r) {
    const sections = r.sections && r.sections.length ? r.sections : [{ title: "", blocks: r.blocks || [] }];
    let fig = 0;
    let tab = 0;
    let index = 0;
    return sections.map((sec, i) => ({
      heading: sec.title ? cnNumber(i + 1) + "、" + sec.title : "",
      lead: sec.lead || "",
      items: sec.blocks.map((b) => {
        let label = "";
        if (b.type === "table") label = "表 " + ++tab;
        if (b.type === "chart") label = "图 " + ++fig;
        return { block: b, label, index: index++ };
      }),
    }));
  }

  function renderReport(data) {
    const p = panel;
    const r = data.report;
    p.current = r;
    p.title.textContent = r.title;
    const when = new Date((r.created_at || 0) * 1000).toLocaleString("zh-CN", { hour12: false });
    p.meta.replaceChildren();
    const versions = data.versions || [r.version];
    if (versions.length > 1) {
      const sel = el("select", "rp-ver");
      sel.setAttribute("aria-label", "切换版本");
      versions
        .slice()
        .reverse()
        .forEach((v) => {
          const opt = el("option", null, "v" + v + (v === versions[versions.length - 1] ? "（最新）" : ""));
          opt.value = String(v);
          if (v === r.version) opt.selected = true;
          sel.appendChild(opt);
        });
      sel.addEventListener("change", () => openReport(r.report_id, Number(sel.value)));
      p.meta.appendChild(sel);
    } else {
      p.meta.appendChild(el("span", null, "v" + r.version));
    }
    if (r.as_of) p.meta.appendChild(el("span", null, "数据截至 " + r.as_of));
    p.meta.appendChild(el("span", null, when));

    p.hideTips = [];
    const sections = numbered(r);
    const hasTables = sections.some((s) => s.items.some((it) => it.block.type === "table"));
    p.tools.replaceChildren();
    p.actions.replaceChildren(
      exportMenu(r.report_id, r.version, hasTables, () => reportAsText(r, data.risk_note_default))
    );

    const body = el("article", "rp-content");
    if (r.verdict) {
      const v = el("section", "rp-verdict");
      v.appendChild(el("div", "rp-verdict-label", "结论"));
      v.appendChild(el("div", "rp-verdict-text", tidyText(r.verdict)));
      body.appendChild(v);
    }
    // 有结论条时摘要只给卡片 / 转发用，报告头不再重复一遍
    else body.appendChild(el("p", "rp-summary", r.summary));
    if (r.kpis && r.kpis.length) {
      const grid = el("section", "rp-kpis n" + r.kpis.length);
      r.kpis.forEach((k) => {
        const tile = el("div", "rp-kpi");
        tile.appendChild(el("div", "rp-kpi-label", k.label));
        tile.appendChild(el("div", "rp-kpi-value " + kpiClass(k), tidyText(k.value)));
        if (k.note) tile.appendChild(el("div", "rp-kpi-note", k.note));
        grid.appendChild(tile);
      });
      body.appendChild(grid);
    }

    const points = keyPoints(r);
    if (points.length) {
      const box = el("section", "rp-points");
      box.appendChild(el("div", "rp-box-title", "核心观点"));
      const ol = el("ol");
      points.forEach((pt) => {
        const li = el("li");
        if (TONE_LABELS[pt.tone]) li.appendChild(el("span", "rp-tone tone-" + pt.tone, TONE_LABELS[pt.tone]));
        const text = el("span");
        renderMarkdown(text, tidyText(pt.text));
        li.appendChild(text);
        ol.appendChild(li);
      });
      box.appendChild(ol);
      body.appendChild(box);
    }

    const titled = sections.filter((s) => s.heading);
    // 与 render.TOC_MIN 一致：三章以内一屏看完，不出目录
    if (titled.length >= 4) {
      const toc = el("nav", "rp-toc");
      toc.appendChild(el("div", "rp-box-title", "目录"));
      titled.forEach((s) => {
        const a = el("button", "rp-toc-item", s.heading);
        a.type = "button";
        a.addEventListener("click", () => {
          const target = s.node;
          if (target) panel.body.scrollTo({ top: target.offsetTop - 8, behavior: "smooth" });
        });
        toc.appendChild(a);
      });
      body.appendChild(toc);
    }

    sections.forEach((s) => {
      const sec = el("section", "rp-sec");
      if (s.heading) {
        sec.appendChild(el("h2", "rp-sec-title", s.heading));
        s.node = sec;
      }
      if (s.lead) {
        const lead = el("p", "rp-sec-lead");
        renderMarkdown(lead, tidyText(s.lead));
        sec.appendChild(lead);
      }
      s.items.forEach((it) => {
        const key = String(it.index);
        const node = renderBlock(it.block, it.label, {
          svg: (data.charts || {})[key],
          best: (data.highlights || {})[key],
          tally: (data.tallies || {})[key],
        });
        if (node) sec.appendChild(node);
      });
      body.appendChild(sec);
    });

    const risks = [r.risk_note, data.risk_note_default].filter((t) => t && t.trim());
    if (risks.length) {
      const box = el("section", "rp-risk");
      box.appendChild(el("div", "rp-box-title", "风险提示"));
      risks.forEach((t) => box.appendChild(el("p", null, t)));
      body.appendChild(box);
    }
    if (r.sources && r.sources.length) {
      body.appendChild(el("div", "rp-sources", "数据来源：" + r.sources.join("；")));
    }
    p.body.replaceChildren(body);
    p.body.scrollTop = 0;
  }

  function signedClass(v) {
    if (v == null || typeof v === "boolean") return "";
    if (typeof v === "number") return v > 0 ? "num-up" : v < 0 ? "num-down" : "";
    const m = /^\s*([+\-−–＋]?)\s*(\d+(?:\.\d+)?)/.exec(String(v));
    if (!m || Number(m[2]) === 0) return "";
    return /[-−–]/.test(m[1]) ? "num-down" : "num-up";
  }

  /** 图 / 表外壳：上方「图 n 标题」，下方「资料来源」。 */
  function figure(b, label, inner, kind) {
    const fig = el("figure", "rp-fig " + kind);
    const cap = el("figcaption", "rp-fig-cap");
    cap.appendChild(el("b", null, label));
    if (b.title) cap.appendChild(document.createTextNode(b.title));
    fig.appendChild(cap);
    fig.appendChild(inner);
    if (b.source) fig.appendChild(el("div", "rp-fig-src", "资料来源：" + b.source));
    return fig;
  }

  const TONE_LABELS = { plus: "优势", tradeoff: "权衡", risk: "风险", action: "建议" };

  /** 核心观点统一成 {text, tone}：旧报告是纯文字。 */
  function keyPoints(r) {
    return (r.key_points || []).map((p) => (typeof p === "string" ? { text: p, tone: "info" } : p));
  }

  function kpiClass(k) {
    if (k.tone === "up") return "num-up";
    if (k.tone === "down") return "num-down";
    return k.tone === "neutral" ? "" : signedClass(k.value);
  }

  /** extra：{svg 图表, best 胜出格, tally 各赢几项}，都由服务端算好。 */
  function renderBlock(b, label, extra) {
    const { svg, best, tally } = extra || {};
    if (b.type === "markdown") {
      const node = el("div", "rp-md");
      renderMarkdown(node, tidyText(b.text));
      liftClaims(node);
      return node;
    }
    if (b.type === "entities") return renderEntities(b, best, tally);
    if (b.type === "fit") return renderFit(b);
    if (b.type === "conclusion") {
      const node = el("div", "rp-conclusion");
      node.appendChild(el("div", "rp-conclusion-label", "小结"));
      const text = el("div");
      renderMarkdown(text, b.text);
      node.appendChild(text);
      return node;
    }
    if (b.type === "table") {
      const wrap = el("div", "rp-table markdown-body");
      const table = el("table");
      const thead = el("thead");
      const tr = el("tr");
      b.columns.forEach((c) => tr.appendChild(el("th", null, c)));
      thead.appendChild(tr);
      const signed = new Set((b.signed_columns || []).map((c) => b.columns.indexOf(c)));
      const marked = new Set((best || []).map(([i, j]) => i + ":" + j));
      const tbody = el("tbody");
      b.rows.forEach((row, i) => {
        const r = el("tr");
        row.forEach((v, j) => {
          const td = el("td", signed.has(j) ? signedClass(v) : "");
          const text = cellText(v);
          if (marked.has(i + ":" + j)) td.appendChild(el("span", "rp-best", text));
          else td.textContent = text;
          r.appendChild(td);
        });
        tbody.appendChild(r);
      });
      table.append(thead, tbody);
      wrap.appendChild(table);
      if (tally) wrap.appendChild(el("div", "rp-tally", tallyText(tally)));
      return figure(b, label, wrap, "is-table");
    }
    if (b.type === "chart") {
      const node = el("div", "rp-chart");
      // 服务端生成、文字已转义；只认 <svg 开头，别的一律不嵌
      if (svg && /^<svg[\s>]/.test(svg)) {
        node.innerHTML = svg;
        bindChart(node, b);
      } else {
        node.textContent = "（图表无法显示）";
      }
      return figure(b, label, node, "is-chart");
    }
    return null;
  }

  /* ------------------------------------------------------------ 对象卡 / 适合 */

  function entityHead(it) {
    const frag = document.createDocumentFragment();
    frag.appendChild(el("div", "rp-ent-name", it.name));
    if (it.kind || (it.tags && it.tags.length)) {
      const meta = el("div", "rp-ent-meta");
      if (it.kind) meta.appendChild(el("span", "rp-ent-kind", it.kind));
      (it.tags || []).forEach((t) => meta.appendChild(el("span", "rp-ent-tag", t)));
      frag.appendChild(meta);
    }
    if (it.note) frag.appendChild(el("div", "rp-ent-note", tidyText(it.note)));
    return frag;
  }

  /** 对象卡：1 个画像 / 2 个对决 / 3–5 个并排。limit：缩略图里只放前几项指标。 */
  function renderEntities(b, best, tally, limit) {
    const wins = new Set((best || []).map(([m, k]) => m + ":" + k));
    const metrics = limit ? b.metrics.slice(0, limit) : b.metrics;
    const n = b.items.length;
    const root = el("section", "rp-ent " + (n === 1 ? "is-one" : n === 2 ? "is-duel" : "is-grid"));
    if (b.title && !limit) root.appendChild(el("div", "rp-ent-title", b.title));

    if (n === 1) {
      const card = el("div", "rp-ent-card");
      card.appendChild(entityHead(b.items[0]));
      if (metrics.length) {
        const facts = el("div", "rp-ent-facts");
        metrics.forEach((m) => {
          const f = el("div", "rp-ent-fact");
          f.append(el("div", "rp-ent-fact-v", cellText(m.values[0])), el("div", "rp-ent-fact-l", m.label));
          facts.appendChild(f);
        });
        card.appendChild(facts);
      }
      root.appendChild(card);
    } else if (n === 2) {
      const head = el("div", "rp-duel-head");
      b.items.forEach((it, k) => {
        const side = el("div", "rp-duel-side s-" + k);
        side.appendChild(entityHead(it));
        head.appendChild(side);
      });
      head.appendChild(el("div", "rp-duel-vs", "VS"));
      root.appendChild(head);
      if (tally) {
        const [[a, ca], [bn, cb]] = tally.wins;
        const score = el("div", "rp-duel-score");
        score.appendChild(el("b", ca > cb ? "is-win" : "", a + " 胜 " + ca + " 项"));
        if (tally.ties) score.appendChild(el("span", null, "持平 " + tally.ties));
        score.appendChild(el("b", cb > ca ? "is-win" : "", bn + " 胜 " + cb + " 项"));
        root.appendChild(score);
      }
      metrics.forEach((m, mi) => {
        const nums = m.values.map(cellNumber);
        const scale = Math.max(0, ...nums.filter((x) => x != null).map(Math.abs)) || 1;
        const row = el("div", "rp-duel-row");
        const val = (k) => el("div", "rp-duel-val v-" + k + (wins.has(mi + ":" + k) ? " is-win" : ""), cellText(m.values[k]));
        const bar = (k) => {
          const track = el("div", "rp-duel-track t-" + k);
          const fill = el("i", wins.has(mi + ":" + k) ? "is-win" : "");
          fill.style.width = (nums[k] == null ? 0 : Math.round((Math.abs(nums[k]) / scale) * 100)) + "%";
          track.appendChild(fill);
          return track;
        };
        row.append(val(0), bar(0), el("div", "rp-duel-label", m.label), bar(1), val(1));
        root.appendChild(row);
      });
    } else if (!limit) {
      // 三个及以上：一个对象一张卡纵向排。横排成表在手机上要左右滑、表头挤成好几行（PDF 仍是表格）
      if (tally) root.appendChild(el("div", "rp-tally is-top", tallyText(tally)));
      b.items.forEach((it, k) => {
        const card = el("div", "rp-ent-card rp-ent-item");
        const wonCount = tally ? tally.wins[k][1] : 0;
        if (wonCount) card.appendChild(el("span", "rp-ent-won", "胜 " + wonCount + " 项"));
        card.appendChild(entityHead(it));
        if (metrics.length) {
          const facts = el("div", "rp-ent-facts");
          metrics.forEach((m, mi) => {
            const on = wins.has(mi + ":" + k);
            const f = el("div", "rp-ent-fact" + (on ? " is-win" : ""));
            const value = el("div", "rp-ent-fact-v", cellText(m.values[k]));
            if (on) value.appendChild(el("b", null, "最优"));
            f.append(value, el("div", "rp-ent-fact-l", m.label));
            facts.appendChild(f);
          });
          card.appendChild(facts);
        }
        root.appendChild(card);
      });
    } else {
      // 缩略图：小表，只放前几项
      const wrap = el("div", "rp-table markdown-body");
      const table = el("table", "rp-ent-table");
      const tr = el("tr");
      tr.appendChild(el("th"));
      b.items.forEach((it) => {
        const th = el("th", "rp-ent-col");
        th.appendChild(entityHead(it));
        tr.appendChild(th);
      });
      const thead = el("thead");
      thead.appendChild(tr);
      const tbody = el("tbody");
      metrics.forEach((m, mi) => {
        const row = el("tr");
        row.appendChild(el("th", "rp-ent-row", m.label));
        m.values.forEach((v, k) => {
          const td = el("td");
          if (wins.has(mi + ":" + k)) td.appendChild(el("span", "rp-best", cellText(v)));
          else td.textContent = cellText(v);
          row.appendChild(td);
        });
        tbody.appendChild(row);
      });
      table.append(thead, tbody);
      wrap.appendChild(table);
      root.appendChild(wrap);
      if (tally && !limit) root.appendChild(el("div", "rp-tally", tallyText(tally)));
    }
    if (b.source && !limit) root.appendChild(el("div", "rp-fig-src", "资料来源：" + b.source));
    return root;
  }

  function renderFit(b) {
    const root = el("section", "rp-fit");
    if (b.title) root.appendChild(el("div", "rp-ent-title", b.title));
    const cols = el("div", "rp-fit-cols");
    [["good", b.good_label || "适合", b.good], ["bad", b.bad_label || "不适合", b.bad]].forEach(([cls, label, items]) => {
      if (!items || !items.length) return;
      const col = el("div", "rp-fit-col fit-" + cls);
      col.appendChild(el("div", "rp-fit-label", label));
      const ul = el("ul");
      items.forEach((x) => {
        const li = el("li");
        renderMarkdown(li, tidyText(x));
        ul.appendChild(li);
      });
      col.appendChild(ul);
      cols.appendChild(col);
    });
    root.appendChild(cols);
    return root;
  }

  /* ------------------------------------------------------------ 图表交互 */

  // 与 reports/render.py 的 PALETTE 一致
  const PALETTE = ["#8088DB", "#F2BC77", "#83BDE8", "#8FD0BF", "#E8A6B8", "#B7A6E2", "#AEB4BF", "#D7C095", "#A3C98F", "#E6A98A"];

  function fmtValue(v, unit) {
    if (v == null) return "—";
    const s = Math.abs(v) >= 1000 ? v.toLocaleString("zh-CN", { maximumFractionDigits: 0 }) : String(+v.toFixed(4));
    return s + (unit || "");
  }

  /** 服务端 SVG 里留了透明感应区（.c-hit / .c-slice），这里接上悬停 / 点按提示。 */
  function bindChart(node, b) {
    const svg = node.querySelector("svg");
    if (!svg) return;
    const tip = el("div", "rp-tip");
    node.appendChild(tip);
    const guide = svg.querySelector(".c-guide");
    let active = null;

    function mark(i) {
      // 折线：点亮该 x 的圆点；柱状：其余柱子变淡
      svg.querySelectorAll(".c-dots").forEach((g) => {
        g.setAttribute("visibility", g.getAttribute("data-i") === String(i) ? "visible" : "hidden");
      });
      svg.querySelectorAll(".c-bar").forEach((r) => r.classList.toggle("is-on", r.getAttribute("data-i") === String(i)));
      svg.classList.toggle("is-hover", i != null);
    }

    function hide() {
      tip.classList.remove("is-show");
      if (guide) guide.setAttribute("visibility", "hidden");
      if (active) active.classList.remove("is-active");
      active = null;
      mark(null);
    }

    function show(target, clientX, clientY) {
      const i = Number(target.getAttribute("data-i"));
      tip.replaceChildren();
      if (b.chart === "scatter") {
        const pt = b.points[i];
        tip.appendChild(el("div", "rp-tip-head", pt.name));
        [[b.x_label, pt.x], [b.y_label, pt.y]].forEach(([name, v]) => {
          const row = el("div", "rp-tip-row");
          row.append(document.createTextNode(name + "　"), el("b", null, fmtValue(v, b.unit)));
          tip.appendChild(row);
        });
      } else if (b.chart === "percentile") {
        const top = b.series[0].data[i];
        tip.appendChild(el("div", "rp-tip-head", b.x[i] + (b.display && b.display[i] ? "　" + b.display[i] : "")));
        tip.appendChild(el("div", "rp-tip-row", (top <= 50 ? "同类前 " + +top.toFixed(2) : "同类后 " + +(100 - top).toFixed(2)) + "%"));
        if (b.x_label) tip.appendChild(el("div", "rp-tip-row", b.x_label));
      } else if (b.chart === "pie") {
        const values = b.series[0].data;
        const total = values.reduce((a, v) => a + (v > 0 ? v : 0), 0) || 1;
        const v = values[i];
        tip.appendChild(el("div", "rp-tip-head", b.x[i]));
        tip.appendChild(el("div", "rp-tip-row", fmtValue(v, b.unit) + "（" + ((v / total) * 100).toFixed(1) + "%）"));
      } else {
        tip.appendChild(el("div", "rp-tip-head", b.x[i]));
        b.series.forEach((s, k) => {
          const row = el("div", "rp-tip-row");
          const dot = el("i");
          dot.style.background = PALETTE[k % PALETTE.length];
          row.append(dot, document.createTextNode(s.name + "　"));
          row.appendChild(el("b", null, fmtValue(s.data[i], b.unit)));
          tip.appendChild(row);
        });
        if (guide) {
          const cx = target.getAttribute("data-cx");
          guide.setAttribute("x1", cx);
          guide.setAttribute("x2", cx);
          guide.setAttribute("visibility", "visible");
        }
      }
      if (active) active.classList.remove("is-active");
      active = target;
      target.classList.add("is-active");
      mark(i);
      // 提示框跟着指针，靠右时翻到左边，不出图表区
      const box = node.getBoundingClientRect();
      tip.classList.add("is-show");
      const w = tip.offsetWidth;
      let x = clientX - box.left + 12;
      if (x + w > box.width) x = clientX - box.left - w - 12;
      tip.style.left = Math.max(0, x) + "px";
      tip.style.top = Math.max(0, clientY - box.top - tip.offsetHeight - 10) + "px";
    }

    svg.querySelectorAll(".c-hit, .c-slice").forEach((t) => {
      t.addEventListener("pointerenter", (e) => show(t, e.clientX, e.clientY));
      t.addEventListener("pointermove", (e) => show(t, e.clientX, e.clientY));
      t.addEventListener("click", (e) => {
        e.stopPropagation();
        show(t, e.clientX, e.clientY);
      });
    });
    svg.addEventListener("pointerleave", (e) => {
      if (e.pointerType === "mouse") hide();
    });
    panel.hideTips.push(hide);
  }

  /* ------------------------------------------------------------ 复制 */

  function reportAsText(r, riskDefault) {
    const out = [r.title];
    if (r.verdict) out.push("结论：" + r.verdict);
    if (!r.verdict) out.push(r.summary);
    if (r.as_of) out.push("数据截至 " + r.as_of);
    out.push("");
    if (r.kpis && r.kpis.length) {
      r.kpis.forEach((k) => out.push(k.label + "：" + k.value + (k.note ? "（" + k.note + "）" : "")));
      out.push("");
    }
    const points = keyPoints(r);
    if (points.length) {
      out.push("核心观点");
      points.forEach((p, i) => out.push(i + 1 + ". " + (TONE_LABELS[p.tone] ? "［" + TONE_LABELS[p.tone] + "］" : "") + p.text));
      out.push("");
    }
    numbered(r).forEach((s) => {
      if (s.heading) out.push(s.heading, "");
      if (s.lead) out.push(s.lead, "");
      s.items.forEach(({ block: b, label }) => {
        if (b.type === "markdown") out.push(b.text, "");
        else if (b.type === "conclusion") out.push("小结：" + b.text, "");
        else if (b.type === "entities") {
          if (b.title) out.push(b.title);
          out.push("| 指标 | " + b.items.map((it) => it.name).join(" | ") + " |");
          out.push("|" + [0, ...b.items].map(() => " --- ").join("|") + "|");
          b.metrics.forEach((m) => out.push("| " + m.label + " | " + m.values.map(cellText).join(" | ") + " |"));
          out.push("");
        } else if (b.type === "fit") {
          if (b.good && b.good.length) out.push((b.good_label || "适合") + "：" + b.good.join("；"));
          if (b.bad && b.bad.length) out.push((b.bad_label || "不适合") + "：" + b.bad.join("；"));
          out.push("");
        }
        else if (b.type === "table") {
          out.push((label + " " + (b.title || "")).trim());
          out.push("| " + b.columns.join(" | ") + " |");
          out.push("|" + b.columns.map(() => " --- ").join("|") + "|");
          b.rows.forEach((row) => out.push("| " + row.map(cellText).join(" | ") + " |"));
          if (b.source) out.push("资料来源：" + b.source);
          out.push("");
        } else if (b.type === "chart") out.push("［" + (label + " " + (b.title || "")).trim() + "］", "");
      });
    });
    const risks = [r.risk_note, riskDefault].filter((t) => t && t.trim());
    if (risks.length) out.push("风险提示：" + risks.join(" "));
    if (r.sources && r.sources.length) out.push("数据来源：" + r.sources.join("；"));
    return out.join("\n").trim();
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast("已复制");
      return;
    } catch (_) {
      /* 宿主 iframe / 旧 webview 不给 clipboard 权限：退回 execCommand */
    }
    const ta = el("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
      toast("已复制");
    } catch (_) {
      toast("复制失败，请手动选择文字");
    }
    ta.remove();
  }

  window.addEventListener("resize", () => {
    if (document.body.classList.contains("rp-open")) {
      document.body.classList.toggle("rp-side", useSide());
    }
  });

  window.buildFileCard = buildFileCard;
  window.buildReportCard = buildReportCard;
  window.appendReportCard = appendReportCard;
})();
