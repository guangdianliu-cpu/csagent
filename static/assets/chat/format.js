/* 回答排版的后处理（docs/对话页升级方案.md §2.7）：渲染完 Markdown 后，
 * 把带正负号的百分数按红涨绿跌上色，如 +107.2%、-18.05%、−0.7%。
 *
 * app.js 的 renderMarkdown 每次渲染后调用 enhanceAnswer(el)；流式打字时会反复调，
 * 所以只做一次 DOM 遍历，不碰代码块。
 */

/* 不用后行断言（?<!）：iOS 16.4 以前的 WebKit 不认，整个脚本会解析失败。
 * 第 1 组是前一个字符（或开头），保证 2019-12-30、3-5% 里的减号不被当成负号。 */
const SIGNED_PCT_RE = /(^|[^0-9A-Za-z.%])([+＋\-−–])(\s?\d+(?:\.\d+)?\s?%)/g;
const SKIP_TAGS = { CODE: 1, PRE: 1, SCRIPT: 1, STYLE: 1 };

function colorSignedPercents(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      for (let p = node.parentNode; p && p !== root; p = p.parentNode) {
        if (SKIP_TAGS[p.nodeName] || (p.classList && (p.classList.contains("num-up") || p.classList.contains("num-down")))) {
          return NodeFilter.FILTER_REJECT;
        }
      }
      SIGNED_PCT_RE.lastIndex = 0;
      return SIGNED_PCT_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_SKIP;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  nodes.forEach((node) => {
    const text = node.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    SIGNED_PCT_RE.lastIndex = 0;
    let m;
    while ((m = SIGNED_PCT_RE.exec(text))) {
      const start = m.index + m[1].length;
      frag.appendChild(document.createTextNode(text.slice(last, start)));
      const span = document.createElement("span");
      span.className = m[2] === "+" || m[2] === "＋" ? "num-up" : "num-down";
      span.textContent = m[2] + m[3];
      frag.appendChild(span);
      last = start + m[2].length + m[3].length;
    }
    frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  });
}

/* 短单元格（序号、数值、短标签）不折行，免得窄屏下被挤成一两个字一行；长说明照常折行 */
const SHORT_CELL_MAX = 10;

/** 表格套一层横滚容器：表格本身撑满宽度，列多了在容器里横滚，页面不横滚。 */
function wrapTables(root) {
  root.querySelectorAll("table").forEach((t) => {
    t.querySelectorAll("td").forEach((td) => {
      if (td.textContent.trim().length <= SHORT_CELL_MAX) td.classList.add("nw");
    });
    const parent = t.parentNode;
    if (!parent || (parent.classList && (parent.classList.contains("md-table") || parent.classList.contains("rp-table")))) return;
    const box = document.createElement("div");
    box.className = "md-table";
    parent.insertBefore(box, t);
    box.appendChild(t);
  });
}

/* 窄屏（手机、弹小窗）下表格放不下才改成卡片：一行一张卡，「表头：值」竖排；放得下的仍是表格 */
const CARD_MAX_WIDTH = 520;
// 序号列允许空格子（末行「合计」常留空）
const INDEX_RE = /^(\d{1,3}\.?)?$/;
// 卡片左侧标签位置有限：「档位规则（methodology-基金公司）」只留「档位规则」
const HEAD_NOTE_RE = /\s*[（(][^）)]*[）)]\s*$/;

function labelCells(t) {
  const heads = Array.from(t.querySelectorAll("thead th"))
    .map((th) => th.textContent.trim().replace(HEAD_NOTE_RE, "") || th.textContent.trim());
  const rows = Array.from(t.querySelectorAll("tbody tr"));
  // 首列只是序号（1、2、3…）时，卡片标题用「序号. 第二列」，不单独占一行
  const indexCol = heads.length > 2 && rows.length > 0 &&
    rows.every((tr) => tr.cells[0] && INDEX_RE.test(tr.cells[0].textContent.trim()));
  rows.forEach((tr) => {
    Array.from(tr.cells).forEach((td, i) => {
      td.dataset.label = heads[i] || "";
      td.classList.toggle("card-title", indexCol ? i === 1 : i === 0);
      td.classList.toggle("card-index", indexCol && i === 0);
      td.classList.toggle("card-empty", !td.textContent.trim());
    });
    const idx = indexCol && tr.cells[0].textContent.trim().replace(/\.$/, "");
    if (idx && tr.cells[1]) tr.cells[1].dataset.index = idx;
  });
}

function fitTable(box) {
  const t = box.querySelector("table");
  if (!t) return;
  box.classList.remove("is-cards");
  if (box.clientWidth === 0 || box.clientWidth > CARD_MAX_WIDTH) return;
  if (t.scrollWidth > box.clientWidth + 1) {
    labelCells(t);
    box.classList.add("is-cards");
  }
}

/* 渲染时表格可能还没挂到页面上（宽度为 0，如回填历史），转屏、弹窗拖宽后宽度也会变：
 * 盯住每个容器的宽度，变了再判断一次。切卡片只改高度，不会自己触发循环。 */
const fitWidths = new WeakMap();
const fitObserver = typeof ResizeObserver === "function"
  ? new ResizeObserver((entries) => {
    entries.forEach((e) => {
      const w = Math.round(e.contentRect.width);
      if (fitWidths.get(e.target) === w) return;
      fitWidths.set(e.target, w);
      fitTable(e.target);
    });
  })
  : null;

function fitTables(root) {
  root.querySelectorAll(".md-table").forEach((box) => {
    fitTable(box);
    if (fitObserver && !fitWidths.has(box)) {
      fitWidths.set(box, -1);
      fitObserver.observe(box);
    }
  });
}

function enhanceAnswer(el) {
  try {
    wrapTables(el);
    fitTables(el);
    colorSignedPercents(el);
  } catch (e) {
    // 排版增强失败不影响正文
    console.warn("[csagent] 回答排版后处理失败：", e);
  }
}
