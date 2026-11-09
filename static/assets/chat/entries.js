/* 快捷入口：欢迎区、「您可以这样问我」、分类标签栏、底部抽屉。
 * 设计见 docs/对话页升级方案.md §2，Figma 画板 3001:3716。
 *
 * 内容来自 GET api/chat/entries（管理后台可改）；拿不到或为空就整块不显示，聊天照常。
 * 依赖 app.js 的全局：chatContainer / messageInput / authHeaders / sendMessage / autoGrow /
 * isStreaming，以及它派发的 csagent:identity / csagent:history / csagent:turn 事件。
 */
(function () {
  "use strict";

  const PLACEHOLDER = "XXX";
  // Figma 3002:6161：可改的问题行尾是铅笔
  const PENCIL =
    '<svg class="qe-q-edit" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.2" ' +
    'stroke-linejoin="round" aria-hidden="true"><path d="M10.6 2.6l2.8 2.8-7.9 7.9-3.3.5.5-3.3z"/><path d="M9.2 4l2.8 2.8"/></svg>';

  /* 标签栏图标：Figma 的图标还没导出，先用同风格的线性图标占位（§6）。 */
  const ICONS = {
    search:
      '<circle cx="7" cy="7" r="4.5"/><path d="M10.5 10.5 14 14M2 2h3M2 4.5h1.5"/>',
    chart: '<path d="M2 13h12M3 10l3-3 2.5 2L13 4"/>',
    person: '<circle cx="8" cy="5" r="2.5"/><path d="M3 14c.6-2.8 2.6-4.2 5-4.2s4.4 1.4 5 4.2"/>',
    building: '<path d="M3 14V4l5-2v12M8 6h5v8M5 6h1M5 9h1M10.5 9h1M10.5 11.5h1M1.5 14h13"/>',
    target: '<circle cx="8" cy="8" r="6"/><circle cx="8" cy="8" r="2"/>',
  };

  function icon(name) {
    const body = ICONS[name] || ICONS.chart;
    return (
      '<svg class="qe-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" ' +
      'stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      body +
      "</svg>"
    );
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  const state = {
    data: null, // QuickEntries | null
    version: -1,
    loadingFor: "",
    // null = 还不知道（历史没回来），true/false = 有没有消息
    hasMessages: null,
    groupCursor: 0,
  };

  let heroEl = null;
  let groupsEl = null;
  let chipBarEl = null;
  let maskEl = null;
  let sheetEl = null;

  /* ---------------------------------------------------------------- 取数 */

  async function fetchEntries() {
    const uid = Identity.userId;
    if (!uid || state.loadingFor === uid) return;
    state.loadingFor = uid;
    try {
      const resp = await fetch("api/chat/entries", { headers: authHeaders() });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const payload = await resp.json();
      if (payload.version === state.version) return;
      state.version = payload.version;
      state.data = payload.entries || null;
      render();
    } catch (e) {
      state.loadingFor = "";
      console.warn("[csagent] 快捷入口加载失败：", e);
    }
  }

  /* ---------------------------------------------------------------- 提问 */

  // 分类没配 fill_label 时按它的联想类型给占位提示（线上配置可能是加这个字段之前保存的）
  const ENTITY_SLOT = { fund: "基金名称", manager: "经理姓名", company: "公司名称", private: "产品名称" };

  function slotLabel(cat) {
    return (cat && (cat.fill_label || ENTITY_SLOT[cat.entity])) || "名称";
  }

  /** 占位在界面上显示成「[基金名称]」。 */
  function fillSlot(label) {
    return "[" + (label || "名称") + "]";
  }

  // 输入框里还留着没改的「[xx]」占位：不联想
  const SLOT_RE = /\[[^\]\s]{1,8}\]/;

  /** 两种问题：带 XXX 的填进输入框并选中占位，让用户直接打名称覆盖；否则直接发出去。 */
  function ask(question, label) {
    closeSheet();
    const at = question.indexOf(PLACEHOLDER);
    const slot = fillSlot(label);
    const text = at >= 0 ? question.replace(PLACEHOLDER, slot) : question;
    messageInput.value = text;
    autoGrow();
    if (at >= 0) {
      messageInput.focus();
      messageInput.setSelectionRange(at, at + slot.length);
      return;
    }
    if (typeof isStreaming !== "undefined" && isStreaming) return;
    sendMessage();
  }

  /* ---------------------------------------------------------------- 欢迎区 */

  function renderHero(welcome) {
    heroEl?.remove();
    heroEl = null;
    if (!welcome || !welcome.title) return;
    heroEl = el("section", "qe-hero");
    const img = el("img", "qe-hero-img");
    img.src = "assets/mascot/lizi-full.png";
    img.alt = "";
    img.decoding = "async";
    const text = el("div", "qe-hero-text");
    text.appendChild(el("h1", "qe-hero-title", welcome.title));
    if (welcome.subtitle) text.appendChild(el("p", "qe-hero-sub", welcome.subtitle));
    heroEl.append(img, text);
    // 放在聊天区最前；历史回填按 .message 等节点定位插入点，不会插到它前面
    const gate = document.getElementById("auth-gate");
    chatContainer.insertBefore(heroEl, gate ? gate.nextSibling : chatContainer.firstChild);
  }

  function renderGroups(welcome) {
    groupsEl?.remove();
    groupsEl = null;
    const groups = (welcome && welcome.groups) || [];
    if (!groups.length) return;

    groupsEl = el("section", "qe-groups");
    const head = el("div", "qe-groups-head");
    head.appendChild(el("span", "qe-groups-title", "您可以这样问我"));
    const more = el("button", "qe-groups-more");
    more.type = "button";
    more.innerHTML =
      '<svg class="qe-icon" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" ' +
      'stroke-linecap="round" aria-hidden="true"><path d="M13.5 6.5A5.5 5.5 0 0 0 3 5M2.5 9.5A5.5 5.5 0 0 0 13 11"/>' +
      '<path d="M3 2v3h3M13 14v-3h-3"/></svg><span>换一批</span>';
    more.hidden = !groups.some((g) => g.questions.length > 1);
    more.addEventListener("click", () => {
      state.groupCursor += 1;
      fillGroups();
    });
    head.appendChild(more);
    groupsEl.appendChild(head);

    groups.forEach((g) => {
      const item = el("div", "qe-group");
      const label = el("div", "qe-group-label");
      label.appendChild(el("span", "qe-group-badge", "问"));
      label.appendChild(el("span", null, g.label));
      const card = el("button", "qe-group-q");
      card.type = "button";
      card.addEventListener("click", () => ask(card.dataset.q || ""));
      item.append(label, card);
      groupsEl.appendChild(item);
    });
    fillGroups();

    const anchor = heroEl ? heroEl.nextSibling : null;
    chatContainer.insertBefore(groupsEl, anchor);
    updateGroupsVisibility();
  }

  function fillGroups() {
    if (!groupsEl || !state.data) return;
    const groups = state.data.welcome.groups;
    groupsEl.querySelectorAll(".qe-group-q").forEach((card, i) => {
      const qs = groups[i].questions;
      const q = qs[state.groupCursor % qs.length];
      card.dataset.q = q;
      card.textContent = q;
    });
  }

  function updateGroupsVisibility() {
    if (!groupsEl) return;
    // 历史还没回来时先不显示，免得有历史的用户看到一闪
    groupsEl.hidden = state.hasMessages !== false;
  }

  /* ---------------------------------------------------------------- 标签栏 */

  function renderChipBar(data) {
    chipBarEl?.remove();
    chipBarEl = null;
    const cats = (data && data.categories) || [];
    if (!cats.length) return;

    chipBarEl = el("nav", "qe-chips");
    chipBarEl.setAttribute("aria-label", "快捷入口");
    const capability = chipButton("search", "能查什么", openCapabilitySheet);
    capability.classList.add("is-primary");
    chipBarEl.appendChild(capability);
    cats.forEach((c) => {
      chipBarEl.appendChild(chipButton(c.icon, c.label, () => openCategorySheet(c.key, "")));
    });
    const composer = document.querySelector(".composer");
    const inner = composer.querySelector(".composer-inner");
    composer.insertBefore(chipBarEl, inner);
  }

  function chipButton(iconName, label, onClick) {
    const b = el("button", "qe-chip");
    b.type = "button";
    b.innerHTML = icon(iconName);
    b.appendChild(el("span", null, label));
    b.addEventListener("click", onClick);
    return b;
  }

  /* ---------------------------------------------------------------- 抽屉 */

  function ensureSheet() {
    if (sheetEl) return;
    maskEl = el("div", "qe-mask");
    maskEl.addEventListener("click", closeSheet);
    sheetEl = el("section", "qe-sheet");
    sheetEl.setAttribute("role", "dialog");
    sheetEl.setAttribute("aria-modal", "true");
    document.body.append(maskEl, sheetEl);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeSheet();
    });
  }

  function openSheet(title, intro, body) {
    ensureSheet();
    sheetEl.replaceChildren();
    const head = el("header", "qe-sheet-head");
    const h = el("h2", "qe-sheet-title", title);
    h.id = "qe-sheet-title";
    sheetEl.setAttribute("aria-labelledby", h.id);
    const close = el("button", "qe-sheet-close");
    close.type = "button";
    close.setAttribute("aria-label", "关闭");
    close.innerHTML =
      '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.3" aria-hidden="true">' +
      '<circle cx="8" cy="8" r="6.5"/><path d="M5.8 5.8l4.4 4.4M10.2 5.8l-4.4 4.4"/></svg>';
    close.addEventListener("click", closeSheet);
    head.append(h, close);
    sheetEl.appendChild(head);
    if (intro) sheetEl.appendChild(el("p", "qe-sheet-intro", intro));
    const scroller = el("div", "qe-sheet-body");
    scroller.appendChild(body);
    sheetEl.appendChild(scroller);
    // 先让元素进入布局再加类，过渡动画才会生效
    requestAnimationFrame(() => document.body.classList.add("qe-sheet-open"));
    close.focus({ preventScroll: true });
  }

  function closeSheet() {
    document.body.classList.remove("qe-sheet-open");
  }

  /** 能查什么：分类 → 能力胶囊 + 关键词。点胶囊进该分类抽屉并选中它。 */
  function openCapabilitySheet() {
    const data = state.data;
    const body = el("div", "qe-cap");
    data.categories.forEach((c) => {
      const sec = el("section", "qe-cap-section");
      const h = el("h3", "qe-cap-title");
      h.innerHTML = icon(c.icon);
      h.appendChild(el("span", null, c.label));
      sec.appendChild(h);
      c.tags.forEach((t) => {
        const row = el("div", "qe-cap-row");
        const pill = el("button", "qe-pill is-active", t.label);
        pill.type = "button";
        pill.addEventListener("click", () => openCategorySheet(c.key, t.label));
        const kws = el("div", "qe-cap-kws");
        t.keywords.forEach((k) => kws.appendChild(el("span", null, k)));
        row.append(pill, kws);
        sec.appendChild(row);
      });
      body.appendChild(sec);
    });
    openSheet("能查什么", data.capability_intro, body);
  }

  /** 分类抽屉：顶部能力胶囊只做展示（不可点）+ 猜你想问。
   *  从「能查什么」点某个胶囊进来时只列它的问题，其余情况列该分类全部问题。 */
  function openCategorySheet(key, selected) {
    const cat = state.data.categories.find((c) => c.key === key);
    if (!cat) return;
    const body = el("div", "qe-cat");
    const pills = el("div", "qe-pills");
    const hint = el("p", "qe-cat-hint", "猜你想问：");
    const list = el("ul", "qe-qlist");
    const current = selected || "";

    function draw() {
      const tags = current ? cat.tags.filter((t) => t.label === current) : cat.tags;
      const seen = new Set();
      list.replaceChildren();
      tags.forEach((t) =>
        t.questions.forEach((q) => {
          if (seen.has(q)) return;
          seen.add(q);
          const li = el("li");
          // 带占位的问题：铅笔图标，点了填进输入框改；其余：›，点了直接发
          const fill = q.includes(PLACEHOLDER);
          const b = el("button", fill ? "qe-q is-fill" : "qe-q");
          b.type = "button";
          const shown = fill ? q.replace(PLACEHOLDER, fillSlot(slotLabel(cat))) : q;
          b.appendChild(el("span", null, shown));
          if (fill) {
            b.insertAdjacentHTML("beforeend", PENCIL);
            b.setAttribute("aria-label", "填入输入框修改：" + shown);
          }
          b.addEventListener("click", () => ask(q, slotLabel(cat)));
          li.appendChild(b);
          list.appendChild(li);
        })
      );
    }

    cat.tags.forEach((t) => {
      pills.appendChild(el("span", "qe-pill qe-pill-static" + (t.label === current ? " is-active" : ""), t.label));
    });
    body.append(pills, hint, list);
    draw();
    openSheet(cat.label, "", body);
  }

  /* ---------------------------------------------------------------- 关键词联想 */

  // 输入里认出基金 / 经理 / 公司名时，输入框上方浮出 3–5 条候选（Figma 3001:5385，§2.4）
  let suggestEl = null;
  let suggestTimer = 0;
  let suggestSeq = 0;

  function hideSuggest() {
    clearTimeout(suggestTimer);
    suggestSeq += 1; // 作废飞行中的请求
    if (suggestEl) suggestEl.hidden = true;
  }

  function onInputForSuggest() {
    clearTimeout(suggestTimer);
    const q = messageInput.value.trim();
    if (!state.data || q.length < 2 || q.length > 100 || q.includes(PLACEHOLDER) || SLOT_RE.test(q)) {
      hideSuggest();
      return;
    }
    suggestTimer = setTimeout(() => fetchSuggest(q), 300);
  }

  async function fetchSuggest(q) {
    const seq = ++suggestSeq;
    try {
      const resp = await fetch("api/chat/suggest?q=" + encodeURIComponent(q), { headers: authHeaders() });
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const data = await resp.json();
      // 慢请求回来时输入已经变了，就别画旧的
      if (seq !== suggestSeq || messageInput.value.trim() !== q) return;
      renderSuggest(data.name || "", data.suggestions || []);
    } catch (e) {
      if (seq === suggestSeq) hideSuggest();
    }
  }

  function renderSuggest(name, items) {
    if (!items.length || (typeof isStreaming !== "undefined" && isStreaming)) {
      hideSuggest();
      return;
    }
    if (!suggestEl) {
      suggestEl = el("ul", "qe-suggest");
      suggestEl.setAttribute("role", "listbox");
      suggestEl.setAttribute("aria-label", "联想问题");
      const composer = document.querySelector(".composer");
      composer.insertBefore(suggestEl, composer.firstChild);
    }
    suggestEl.replaceChildren();
    items.forEach((text) => {
      const li = el("li");
      li.setAttribute("role", "option");
      const b = el("button", "qe-suggest-item");
      b.type = "button";
      const at = name ? text.indexOf(name) : -1;
      if (at >= 0) {
        b.append(text.slice(0, at), el("em", null, name), text.slice(at + name.length));
      } else {
        b.textContent = text;
      }
      // mousedown 先于输入框 blur，用它免得点下去之前列表就收起了
      b.addEventListener("mousedown", (e) => e.preventDefault());
      b.addEventListener("click", () => {
        hideSuggest();
        messageInput.value = text;
        autoGrow();
        sendMessage();
      });
      li.appendChild(b);
      suggestEl.appendChild(li);
    });
    suggestEl.hidden = false;
  }

  messageInput.addEventListener("input", onInputForSuggest);
  messageInput.addEventListener("blur", () => setTimeout(hideSuggest, 150));
  messageInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSuggest();
  });

  /* ---------------------------------------------------------------- 组装 */

  function render() {
    const data = state.data;
    closeSheet();
    renderHero(data && data.welcome);
    renderGroups(data && data.welcome);
    renderChipBar(data);
    if (data && data.welcome && data.welcome.placeholder) {
      messageInput.placeholder = data.welcome.placeholder;
    }
  }

  document.addEventListener("csagent:identity", fetchEntries);
  document.addEventListener("csagent:history", (e) => {
    state.hasMessages = Boolean(e.detail && e.detail.count);
    updateGroupsVisibility();
  });
  document.addEventListener("csagent:turn", () => {
    state.hasMessages = true;
    updateGroupsVisibility();
    hideSuggest();
  });
  // app.js 早于本文件执行；身份可能已经就绪（联调登录很快），补拉一次
  if (Identity.ready) fetchEntries();
})();
