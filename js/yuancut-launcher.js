/**
 * ComfyUI-YuanCut —— YuanCut 启动器
 *
 * 行为与 ComfyUI-OpenCut 一致：
 *  1. 不自动打开；点节点里的「打开 YuanCut」、双击节点、右键菜单或顶栏按钮才打开。
 *  2. 关闭后 iframe 实例保留在内存里，再次打开秒开，编辑状态与项目都不丢。
 *  3. 第一次打开时才真正请求 /yuancut/...，之前不产生任何加载。
 *
 * 纯 ES module，不需要打包；ComfyUI 会把 js/ 目录挂到
 * /extensions/ComfyUI-YuanCut/ 下并自动加载其中的 .js。
 */
import { app } from "../../../scripts/app.js";

const NODE_CLASS = "YuanCut";

/** 插件界面固定中文，内置编辑器也固定打开 zh 语言 */
const BASE_URL = "/yuancut/zh";
const HOME_URL = `${BASE_URL}/projects/`;

const TEXT = {
  open: "打开 YuanCut",
  nodeHint: "点击打开 YuanCut 视频编辑器，进入后是所有项目",
  reload: "重新加载",
  reloadTitle: "重新加载当前页面（未保存的编辑会丢失）",
  projects: "项目",
  projectsTitle: "返回项目列表",
  tab: "新标签页",
  tabTitle: "在新标签页打开 YuanCut",
  close: "关闭",
  closeTitle: "关闭（之后可再次打开，编辑状态保留）",
  menu: "打开 YuanCut 编辑器",
  dialogLabel: "YuanCut 视频编辑器",
  tag: "视频编辑器",
};

const STYLE_ID = "yuancut-launcher-style";
const OVERLAY_ID = "yuancut-launcher-overlay";

let overlay = null;
let frame = null;
let frameLoaded = false;
let restoreFocus = null;

/* YuanCut 的暗色设计令牌：background hsl(0 0% 7%)、border hsl(0 0% 16%)、primary #009dff */
const CSS = `
#${OVERLAY_ID} {
  position: fixed;
  inset: 0;
  z-index: 100000;
  display: none;
  align-items: center;
  justify-content: center;
  padding: 0;
  background: rgba(0, 0, 0, 0.72);
  backdrop-filter: blur(4px);
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
}
#${OVERLAY_ID}.is-open { display: flex; }

#${OVERLAY_ID} .yuancut-panel {
  display: flex;
  flex-direction: column;
  width: 100%;
  height: 100%;
  overflow: hidden;
  border: 1px solid hsl(0 0% 16%);
  border: 0;
  border-radius: 0;
  background: hsl(0 0% 7%);
  box-shadow: 0 32px 90px rgba(0, 0, 0, 0.7), 0 0 0 1px rgba(0, 157, 255, 0.06);
}

#${OVERLAY_ID} .yuancut-bar {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  height: 46px;
  padding: 0 12px 0 14px;
  border-bottom: 1px solid hsl(0 0% 16%);
  background: hsl(0 0% 9%);
  color: hsl(0 0% 92%);
  font-size: 13px;
  font-weight: 500;
  user-select: none;
}

#${OVERLAY_ID} .yuancut-brand { display: flex; align-items: center; gap: 10px; min-width: 0; }
#${OVERLAY_ID} .yuancut-mark {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: #009dff;
  box-shadow: 0 0 10px rgba(0, 157, 255, 0.75);
}
#${OVERLAY_ID} .yuancut-name { font-size: 13px; font-weight: 600; letter-spacing: 0.2px; }
#${OVERLAY_ID} .yuancut-tag {
  padding: 2px 7px;
  border: 1px solid hsl(204 100% 18%);
  border-radius: 999px;
  background: hsl(204 100% 10%);
  color: hsl(200 98% 66%);
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}

#${OVERLAY_ID} .yuancut-tools { display: flex; align-items: center; gap: 4px; }
#${OVERLAY_ID} .yuancut-btn {
  padding: 6px 11px;
  border: 1px solid transparent;
  border-radius: 8px;
  background: transparent;
  color: hsl(0 0% 72%);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.12s ease, color 0.12s ease, border-color 0.12s ease;
}
#${OVERLAY_ID} .yuancut-btn:hover { background: hsl(0 0% 16%); color: hsl(0 0% 96%); }
#${OVERLAY_ID} .yuancut-btn:active { background: hsl(0 0% 20%); }
#${OVERLAY_ID} .yuancut-btn:focus-visible { outline: 2px solid rgba(0, 157, 255, 0.55); outline-offset: 1px; }
#${OVERLAY_ID} .yuancut-btn.is-danger:hover {
  border-color: hsl(0 72% 40%);
  background: hsl(0 72% 32%);
  color: #fff;
}

#${OVERLAY_ID} .yuancut-frame-wrap {
  position: relative;
  flex: 1 1 auto;
  min-height: 0;
}
#${OVERLAY_ID} .yuancut-frame {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  border: 0;
  display: block;
  background: hsl(0 0% 7%);
}
#${OVERLAY_ID} .yuancut-loading {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 14px;
  background: hsl(0 0% 7%);
  color: hsl(0 0% 72%);
  font-size: 13px;
}
#${OVERLAY_ID} .yuancut-loading[hidden] { display: none; }
#${OVERLAY_ID} .yuancut-spinner {
  width: 22px;
  height: 22px;
  border: 2px solid hsl(0 0% 22%);
  border-top-color: #009dff;
  border-radius: 50%;
  animation: yuancut-spin 0.8s linear infinite;
}
@keyframes yuancut-spin { to { transform: rotate(360deg); } }

.yuancut-node-box {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 2px;
  font-family: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color: hsl(0 0% 62%);
  user-select: none;
}
.yuancut-node-box .yuancut-node-hint {
  font-size: 11px;
  line-height: 1.4;
  opacity: 0.8;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.yuancut-node-box .yuancut-node-open {
  padding: 8px 12px;
  border: 1px solid rgba(0, 157, 255, 0.55);
  border-radius: 8px;
  background: #009dff;
  color: #fff;
  font: inherit;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  transition: filter 0.12s ease, transform 0.06s ease;
}
.yuancut-node-box .yuancut-node-open:hover { filter: brightness(1.1); }
.yuancut-node-box .yuancut-node-open:active { filter: brightness(0.92); transform: translateY(1px); }
.yuancut-node-box .yuancut-node-open:focus-visible { outline: 2px solid rgba(0, 157, 255, 0.5); outline-offset: 2px; }
`;

function injectStyle() {
  if (!document.head || document.getElementById(STYLE_ID)) return;
  const style = document.createElement("style");
  style.id = STYLE_ID;
  style.textContent = CSS;
  document.head.appendChild(style);
}

// 样式必须在节点第一次渲染前就位：以前只在打开编辑器（ensureOverlay）时才注入，
// 结果新拖出来的节点先以「无样式」状态出现，按钮被挤到节点外面看不见。
if (document.head) {
  injectStyle();
} else {
  document.addEventListener("DOMContentLoaded", injectStyle, { once: true });
}

let loadingEl = null;
let loadingTimer = null;
let navAckTimer = null;

/** 加载提示：延迟 150ms 再显示，避免快速加载时闪一下 */
function showLoading() {
  if (!loadingEl || !loadingEl.hidden) return;
  clearTimeout(loadingTimer);
  loadingTimer = setTimeout(() => {
    if (loadingEl) loadingEl.hidden = false;
  }, 150);
}

function hideLoading() {
  clearTimeout(loadingTimer);
  loadingTimer = null;
  if (loadingEl) loadingEl.hidden = true;
}

/**
 * 把可选扩展的前端脚本注入 iframe。
 * iframe 是常驻的（关闭只是隐藏），部署扩展之前打开过的页面会一直用旧 HTML，
 * 服务端注入的 <script> 就不会出现；这里在每次加载/打开时补一次，幂等。
 */
function ensureExtScript() {
  if (!frame) return;
  let doc = null;
  try {
    doc = frame.contentDocument;
  } catch {
    return;
  }
  if (!doc || !doc.head) return;
  // 每次都换一个版本号强制重新拉取；旧脚本剔除，旧实例靠令牌自动退场
  const version = String(Date.now());
  for (const old of doc.querySelectorAll("script[data-yc-ext], script[src*='/yuancut/ext/ui/']")) {
    old.remove();
  }
  try {
    doc.defaultView.__ycExtToken = null;
  } catch {}
  const script = doc.createElement("script");
  script.type = "module";
  script.src = `/yuancut/ext/ui/ui.js?v=${version}`;
  script.dataset.ycExt = "1";
  script.onerror = () => script.remove();
  doc.head.appendChild(script);
  // 补注入时面板可能已经渲染完，直接触发一次扫描
  setTimeout(() => {
    try {
      frame.contentWindow?.dispatchEvent(new Event("yc-ext-rescan"));
    } catch {}
  }, 300);
}

/** 整页加载某个地址 */
function loadFrame(url) {
  if (!frame) return;
  showLoading();
  frame.src = url;
  frameLoaded = true;
}

/**
 * 站内跳转：交给前端路由，避免 iframe 整页重载（重载要重新解析 ~3MB 脚本）
 * 前端侧由 host-bridge 监听并 push。
 */
function navigateInFrame(route) {
  if (!frameLoaded || !frame?.contentWindow) {
    loadFrame(`${BASE_URL}${route.replace(/^\/zh/, "")}`);
    return;
  }
  try {
    frame.contentWindow.postMessage(
      { type: "yuancut:navigate", path: route },
      window.location.origin,
    );
    // 前端路由没接住（旧版本/异常）就退回整页加载，避免按钮点了没反应
    clearTimeout(navAckTimer);
    navAckTimer = setTimeout(() => {
      try {
        if (!frame.contentWindow.location.pathname.endsWith(route)) {
          loadFrame(`${BASE_URL}/projects/`);
        }
      } catch {
        loadFrame(`${BASE_URL}/projects/`);
      }
    }, 800);
  } catch {
    loadFrame(`${BASE_URL}/projects/`);
  }
}

/** 重新加载 iframe 里当前页面（项目列表或编辑器） */
function reloadFrame() {
  if (!frameLoaded || !frame) return;
  showLoading();
  try {
    frame.contentWindow.location.reload();
  } catch {
    loadFrame(HOME_URL);
  }
}

function onOverlayClick(event) {
  const action = event.target.closest?.("[data-act]")?.dataset.act;
  // 只在明确点了「关闭」时才退出：以前点面板外的空白背景也会关，
  // 全屏后更容易误触（编辑器占满屏幕，用户以为点到的是自己的位置）。
  if (!action) return;
  if (action === "close") {
    closeEditor();
  } else if (action === "reload") {
    reloadFrame();
  } else if (action === "projects") {
    let current = "";
    try {
      current = frame?.contentWindow?.location?.pathname || "";
    } catch {
      current = "";
    }
    if (current.endsWith("/projects/")) return;
    navigateInFrame("/zh/projects/");
  } else if (action === "tab") {
    window.open(HOME_URL, "_blank", "noopener");
  }
}

function onKeyDown(event) {
  if (event.key === "Escape" && overlay?.classList.contains("is-open")) {
    closeEditor();
  }
}

function ensureOverlay() {
  if (overlay) return overlay;

  injectStyle();

  overlay = document.createElement("div");
  overlay.id = OVERLAY_ID;
  overlay.innerHTML = `
    <div class="yuancut-panel" role="dialog" aria-modal="true" aria-label="${TEXT.dialogLabel}">
      <div class="yuancut-bar">
        <div class="yuancut-brand">
          <span class="yuancut-mark"></span>
          <span class="yuancut-name">YuanCut</span>
          <span class="yuancut-tag">${TEXT.tag}</span>
        </div>
        <div class="yuancut-tools">
          <button type="button" class="yuancut-btn" data-act="projects" title="${TEXT.projectsTitle}">${TEXT.projects}</button>
          <button type="button" class="yuancut-btn" data-act="reload" title="${TEXT.reloadTitle}">${TEXT.reload}</button>
          <button type="button" class="yuancut-btn" data-act="tab" title="${TEXT.tabTitle}">${TEXT.tab}</button>
          <button type="button" class="yuancut-btn is-danger" data-act="close" title="${TEXT.closeTitle}">${TEXT.close}</button>
        </div>
      </div>
      <div class="yuancut-frame-wrap">
        <iframe class="yuancut-frame" title="YuanCut" src="about:blank" allow="clipboard-read; clipboard-write; fullscreen"></iframe>
        <div class="yuancut-loading" hidden>
          <span class="yuancut-spinner"></span>
          <span>正在加载 YuanCut…</span>
        </div>
      </div>
    </div>`;
  document.body.appendChild(overlay);

  frame = overlay.querySelector(".yuancut-frame");
  loadingEl = overlay.querySelector(".yuancut-loading");
  frame.addEventListener("load", () => {
    hideLoading();
    ensureExtScript();
  });
  window.addEventListener("message", (event) => {
    if (event.data?.type === "yuancut:navigated") {
      clearTimeout(navAckTimer);
      navAckTimer = null;
      hideLoading();
    }
  });
  overlay.addEventListener("click", onOverlayClick);
  document.addEventListener("keydown", onKeyDown, true);
  // 面板是全屏的，所有指针/滚轮事件都在这里吃掉，别让 ComfyUI 画布
  // 把「在编辑器里的一次点击」当成画布操作（会拖节点、取消选中，甚至把弹层顶掉）。
  for (const name of ["pointerdown", "pointerup", "mousedown", "mouseup", "wheel", "contextmenu"]) {
    overlay.addEventListener(name, (event) => event.stopPropagation(), {
      passive: name === "wheel",
    });
  }

  return overlay;
}

/** 打开编辑器（第一次调用时才真正加载 /yuancut/...） */
export function openEditor() {
  ensureOverlay();
  if (!frameLoaded) {
    loadFrame(HOME_URL);
  } else {
    // iframe 里可能是部署扩展之前加载的旧页面：打开时再补一次注入
    ensureExtScript();
  }
  restoreFocus = document.activeElement;
  overlay.classList.add("is-open");
  overlay.querySelector('[data-act="close"]')?.focus();
}

/** 关掉弹层时把里面的播放停掉：iframe 是常驻的，只隐藏的话声音还在放 */
function pauseFrame() {
  // 1) 直接暂停 DOM 里的媒体（同源 iframe，拿得到 document）
  try {
    const doc = frame?.contentWindow?.document;
    doc?.querySelectorAll("video,audio").forEach((media) => {
      try {
        media.pause();
      } catch {
        /* 有些元素不允许 pause */
      }
    });
  } catch {
    /* iframe 还没加载好 */
  }
  // 2) 通知编辑器把「播放状态」一起停掉，否则再打开会带着播放中状态跳一下
  try {
    frame?.contentWindow?.postMessage(
      { type: "yuancut:command", command: "pause" },
      "*",
    );
  } catch {
    /* ignore */
  }
}

/** 关闭编辑器；iframe 保留，下次打开立即恢复上次的编辑状态 */
export function closeEditor() {
  if (!overlay) return;
  pauseFrame();
  overlay.classList.remove("is-open");
  if (restoreFocus?.focus) {
    try {
      restoreFocus.focus();
    } catch {
      /* 元素可能已被移除 */
    }
  }
  restoreFocus = null;
}
function applyTitle(node) {
  if (!node.title || node.title === NODE_CLASS) node.title = "YuanCut 视频编辑器";
}

function buildNodeUI(node) {
  if (node.__yuancutLauncher) return;
  node.__yuancutLauncher = true;

  applyTitle(node);

  const box = document.createElement("div");
  box.className = "yuancut-node-box";
  box.innerHTML = `
    <div class="yuancut-node-hint">${TEXT.nodeHint}</div>
    <button type="button" class="yuancut-node-open">${TEXT.open}</button>`;

  const hint = box.querySelector(".yuancut-node-hint");
  const button = box.querySelector(".yuancut-node-open");

  // 关键布局直接写在元素上：万一外部样式表还没注入，按钮也不会被挤出节点（首帧就完整）
  box.style.cssText =
    "display:flex;flex-direction:column;gap:8px;padding:2px;box-sizing:border-box;width:100%;";
  if (hint) {
    hint.style.cssText =
      "font-size:11px;line-height:1.4;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;";
  }
  button.style.cssText =
    "padding:8px 12px;border:1px solid rgba(0,157,255,0.55);border-radius:8px;" +
    "background:#009dff;color:#fff;font-size:13px;font-weight:600;cursor:pointer;" +
    "font-family:inherit;line-height:1.2;width:100%;box-sizing:border-box;";

  button.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    openEditor();
  });
  // 阻止在按钮上拖拽时被画布当成节点拖动
  box.addEventListener("pointerdown", (event) => event.stopPropagation());
  box.addEventListener("wheel", (event) => event.stopPropagation(), { passive: true });

  if (typeof node.addDOMWidget === "function") {
    node.addDOMWidget("yuancut_launch", "yuancut-launch", box, {
      getValue: () => "",
      setValue: () => {},
      computeSize: (width) => [width || 280, 78],
      hideOnZoom: false,
    });
  }

  // 双击节点 = 打开
  const previousDblClick = node.onDblClick;
  node.onDblClick = function (...args) {
    if (typeof previousDblClick === "function") previousDblClick.apply(this, args);
    openEditor();
  };

  // 右键菜单入口
  const previousMenu = node.getExtraMenuOptions;
  node.getExtraMenuOptions = function (canvas, options) {
    if (typeof previousMenu === "function") previousMenu.apply(this, arguments);
    options.push({ content: TEXT.menu, callback: () => openEditor() });
  };

  // 节点最小尺寸：创建时立刻按内容算好。
  // 以前靠 setTimeout(0/150) 补尺寸，头一两百毫秒按钮被裁在节点外面（只看到文字）。
  const MIN_WIDTH = 300;
  const MIN_HEIGHT = 150;
  const ensureSize = () => {
    if (!node.size || typeof node.size[0] !== "number") return;

    let needWidth = MIN_WIDTH;
    let needHeight = MIN_HEIGHT;
    if (typeof node.computeSize === "function") {
      const computed = node.computeSize();
      if (Array.isArray(computed)) {
        needWidth = Math.max(needWidth, Number(computed[0]) || 0);
        needHeight = Math.max(needHeight, Number(computed[1]) || 0);
      }
    }

    const width = Math.max(node.size[0] || 0, needWidth);
    const height = Math.max(node.size[1] || 0, needHeight);
    if (Math.abs(width - node.size[0]) < 0.5 && Math.abs(height - node.size[1]) < 0.5) return;
    node.setSize([width, height]);
    app.graph?.setDirtyCanvas?.(true, true);
  };

  injectStyle();
  ensureSize();
  if (typeof requestAnimationFrame === "function") requestAnimationFrame(ensureSize);

  const previousOnAdded = node.onAdded;
  node.onAdded = function (...args) {
    if (typeof previousOnAdded === "function") previousOnAdded.apply(this, args);
    ensureSize();
  };

  const previousOnConfigure = node.onConfigure;
  node.onConfigure = function (...args) {
    if (typeof previousOnConfigure === "function") previousOnConfigure.apply(this, args);
    ensureSize();
    applyTitle(this);
  };
}

app.registerExtension({
  name: "ComfyUI.YuanCut.NodeLauncher",
  async nodeCreated(node) {
    if (node.comfyClass !== NODE_CLASS) return;
    buildNodeUI(node);
  },
});

app.registerExtension({
  name: "ComfyUI.YuanCut.TopMenu",
  async setup() {
    const ComfyButton = window.comfyAPI?.button?.ComfyButton;
    if (!ComfyButton) return;
    const menu = app.menu?.settingsGroup;
    if (!menu) return;
    try {
      menu.append(
        new ComfyButton({
          icon: "video",
          tooltip: "打开 YuanCut 视频编辑器",
          content: "YuanCut",
          action: openEditor,
        }),
      );
    } catch (error) {
      console.warn("[YuanCut] failed to add top menu button", error);
    }
  },
});

// 方便在控制台里调试：ComfyUI.YuanCut.open()
window.ComfyUI = window.ComfyUI || {};
window.ComfyUI.YuanCut = { open: openEditor, close: closeEditor };
