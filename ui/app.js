/* Endfield RotPilot · Web 前端 */
const $ = (s) => document.querySelector(s);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const api = {
  async _req(url, opt) {
    // 偶发的连接断开很常见（并发请求 / 页面刚刷新），重试一次不算断连
    for (let i = 0; i < 2; i++) {
      try {
        const r = await fetch(url, opt);
        hideErr();
        return await r.json();
      } catch (e) {
        if (i === 1) { apiFail(); throw e; }
        await sleep(150);
      }
    }
  },
  get(p) {
    return this._req(p);
  },
  post(p, body) {
    return this._req(p, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {}),
    });
  },
};

const state = {
  team: ["", "", "", ""],
  steps: [],
  teams: [],
  holdMs: 280,
  ops: [],
  sel: null,
  selSet: [],
  loadedTeam: null,
  comps: [],
  compFilter: null,
  caret: null,      // 光标：插在第几个位置
  pickSlot: 0,
  pickElem: "",
  quick: false,
};

/* ---------------- 撤销栈 ---------------- */
const undoStack = [];
const redoStack = [];

function snapshot() {
  const now = JSON.stringify(state.steps);
  // 和上一次一模一样就不重复入栈，避免“撤销没反应”
  if (undoStack.length && undoStack[undoStack.length - 1] === now) return;
  undoStack.push(now);
  if (undoStack.length > 120) undoStack.shift();
  redoStack.length = 0;          // 新操作会清空重做栈
}

function _restore(stepsJSON) {
  state.steps = JSON.parse(stepsJSON);
  state.sel = null;
  state.selSet = [];
  state.caret = state.caret == null ? null : Math.min(state.caret, state.steps.length);
  renderSeq();
  persistSoon();
}

function undo() {
  // 栈顶如果就是当前状态，先弹掉：否则按下去看着像“没反应”
  const cur = JSON.stringify(state.steps);
  while (undoStack.length && undoStack[undoStack.length - 1] === cur) undoStack.pop();
  if (!undoStack.length) return toast("没有可撤销的操作了");
  redoStack.push(cur);
  _restore(undoStack.pop());
  toast("已撤销 · Ctrl+Y 可重做");
}

function redo() {
  // 同理：重做栈顶和当前一样就没得重做
  const cur = JSON.stringify(state.steps);
  while (redoStack.length && redoStack[redoStack.length - 1] === cur) redoStack.pop();
  if (!redoStack.length) return toast("没有可重做的操作");
  // 压回去之前先去掉和栈顶重复的，免得来回点几轮以后撤销又要多按几下
  if (undoStack.length && undoStack[undoStack.length - 1] === cur) { /* 已经是栈顶 */ }
  else undoStack.push(cur);
  _restore(redoStack.pop());
  toast("已恢复");
}


/* ---------------- 运行条透明度 ---------------- */
// 界面上用百分比（10~100），后台收 0.10~1.00
const pctToAlpha = (p) => Math.max(0.10, Math.min(1, Number(p) / 100));
const alphaToPct = (a) => Math.round(Math.max(0.10, Math.min(1, Number(a) || 0.94)) * 100);

function syncAlphaUI() {
  const pct = alphaToPct(state.opacity);
  const r = $("#alphaRange");
  const v = $("#alphaVal");
  const n = $("#setAlpha");
  if (r && document.activeElement !== r) r.value = pct;
  if (v) v.textContent = pct + "%";
  if (n && document.activeElement !== n) n.value = (Number(state.opacity) || 0.94).toFixed(2);
}

// 拖动滑块：先就地改数字，再防抖推给后台（运行条在跑就立刻变）
let alphaTimer = null;
function pushAlpha(pct) {
  const v = pctToAlpha(pct);
  state.opacity = v;
  syncAlphaUI();
  clearTimeout(alphaTimer);
  alphaTimer = setTimeout(async () => {
    try {
      const r = await api.post("/api/overlay/alpha", { opacity: v });
      if (r && typeof r.opacity === "number") { state.opacity = r.opacity; syncAlphaUI(); }
    } catch (e) { /* 后台没起也不影响下次保存 */ }
  }, 90);
}

function bindAlphaControls() {
  const r = $("#alphaRange");
  if (r) {
    r.value = alphaToPct(state.opacity);
    r.addEventListener("input", () => pushAlpha(r.value));
    r.addEventListener("change", () => { pushAlpha(r.value); persistSoon(); });
  }
  syncAlphaUI();
}

/* ---------------- 心跳 / 关窗通知 ---------------- */
// 每 4 秒报一次“我还活着”：后台靠它判断主窗口是否还在（关窗后要自动收摊），
// 顺便在断线时挂红条、顺手报告有没有管理员权限（没权限就读不到游戏按键）。
let _adminWarned = false;

async function heartbeat() {
  try {
    const r = await fetch("/api/whoami", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    let d = null;
    try { d = await r.json(); } catch (e) { /* 老后台没有 JSON */ }
    if (d && typeof d.admin === "boolean") {
      const st = $("#adminState");
      const hint = $("#adminHint");
      if (st) st.textContent = d.admin ? "管理员权限 ✓（能读到游戏按键）" : "未获管理员权限";
      if (hint) {
        hint.textContent = d.admin ? "" :
          "没拿到管理员权限，游戏里的按键读不到。处理：关掉程序，右键「启动.bat」→ 以管理员身份运行，弹窗点「是」。";
      }
      if (!d.admin && !_adminWarned) {
        _adminWarned = true;
        toast("没拿到管理员权限：游戏按键可能读不到（重启时点「是」）", 5000);
      }
    }
    if (typeof hideErr === "function") hideErr();
  } catch (e) {
    apiFail();
  }
}
setInterval(heartbeat, 4000);

// 关掉主窗口 → 告诉后台收摊（运行条没在跑就直接退出，黑窗口一起关）。
// 用 sendBeacon：页面销毁时也能发出去。
let saidBye = false;
function sayBye() {
  if (saidBye) return;
  saidBye = true;
  try {
    const blob = new Blob([JSON.stringify({})], { type: "application/json" });
    if (navigator.sendBeacon && navigator.sendBeacon("/api/bye", blob)) return;
  } catch (e) { /* 走下面的 fetch */ }
  try {
    fetch("/api/bye", { method: "POST", body: "{}", keepalive: true });
  } catch (e) { /* 后台可能已经没了 */ }
}
window.addEventListener("pagehide", sayBye);
window.addEventListener("beforeunload", sayBye);


/* ---------------- 显示屏幕 ---------------- */
// 主窗口和运行条都摆到「游戏所在那块屏」；这里只负责把当前情况说清楚 + 立即生效。
async function refreshScreenHint() {
  const el = $("#screenHint");
  const sel = $("#setScreen");
  if (sel) sel.value = state.screenMode || "game";
  if (!el) return;
  try {
    const d = await api.get("/api/screens");
    const ms = d.monitors || [];
    if (!ms.length) { el.textContent = "没检测到显示器信息"; return; }
    const tag = (i) => (ms[i] && ms[i].primary ? "主屏" : "副屏");
    const desc = ms.map((m, i) => {
      const w = m.rect[2] - m.rect[0], h = m.rect[3] - m.rect[1];
      const sc = m.scale && Math.abs(m.scale - 1) > 0.01 ? " " + Math.round(m.scale * 100) + "%" : "";
      return `${tag(i)} ${w}×${h}${sc}`;
    }).join(" · ");
    const g = d.game || {};
    let gs;
    if (g.found && g.sure) gs = `游戏在${tag(g.screen)}`;
    else if (g.found) gs = "没认出游戏窗口（会沿用上次那块屏；游戏开着时再进来看一次）";
    else gs = "暂时没找到游戏窗口（会先沿用上次那块屏）";
    const cur = d.current >= 0 ? `当前窗口在${tag(d.current)}。` : "";
    el.textContent = `检测到 ${ms.length} 块屏：${desc}。${gs}。${cur}`;
  } catch (e) {
    el.textContent = "取不到显示器信息（后台没响应）";
  }
}

async function applyScreenMode(mode, quiet) {
  state.screenMode = mode || "game";
  try {
    const r = await api.post("/api/screen", { mode: state.screenMode });
    if (r && r.error) { toast("切换失败：" + r.error); return; }
    if (!quiet) {
      const m = { game: "跟随游戏窗口", primary: "主屏", secondary: "副屏" }[state.screenMode] || state.screenMode;
      toast("显示屏幕：" + m);
    }
  } catch (e) {
    if (!quiet) toast("切换失败：后台没响应");
  }
  refreshScreenHint();
}

/* ---------------- 提示 ---------------- */
let toastTimer = null;
function toast(msg, ms) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("on");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("on"), ms || 1600);
}

/* ---------------- 数据 ---------------- */
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let updatingNow = false;   // 更新时后台会退出，别弹「进程没有响应」

const LINKS = {
  bili: "https://space.bilibili.com/495412309",
  biliName: "若枫鹿栀",
  docs: "https://docs.qq.com/sheet/DWlNLS1NRdXNHZVJr?tab=BB08J2",
  pan: "",
};


// ---- 键位录制：点一下输入框，按想要的键 ----
function keyName(e) {
  const k = e.key;
  if (k === " " ) return "SPACE";
  if (k === "Escape") return "ESC";
  if (k === "Backspace") return "";
  if (k === "Tab") return "TAB";
  if (k === "Enter") return "ENTER";
  if (k === "Shift" || k === "Control" || k === "Alt") return "";
  if (/^F\d{1,2}$/.test(k)) return k.toUpperCase();
  if (k.length === 1) return k.toUpperCase();
  return k.toUpperCase();
}

function bindKeyInputs() {
  document.querySelectorAll(".keyin").forEach((el) => {
    el.addEventListener("keydown", (e) => {
      e.preventDefault();
      e.stopPropagation();
      // 退格 / Delete 用来清空这个键位（终结技、连携这种可留空）
      if (e.key === "Backspace" || e.key === "Delete") {
        el.value = "";
        el.dataset.raw = "";
        el.blur();
        toast("已清空该键位");
        return;
      }
      const name = keyName(e);
      if (name === "") return;          // 纯修饰键：忽略
      el.value = name;
      el.blur();
    });
    el.addEventListener("click", () => el.select());
    // 鼠标侧键：M4 = 后退键 / M5 = 前进键
    el.addEventListener("mousedown", (e) => {
      if (e.button === 3 || e.button === 4) {
        e.preventDefault();
        e.stopPropagation();
        el.value = e.button === 3 ? "M4" : "M5";
        el.blur();
        toast(`已记录${e.button === 3 ? "鼠标侧键 1（M4）" : "鼠标侧键 2（M5）"}`);
      }
    });
    // 右键清空这个键位（保存后按默认值处理）
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      el.value = "";
      toast("已清空该键位");
    });
  });
}


// ---- 手柄：设备检测 + 按键学习 ----
const DS_BUTTONS = {
  1: "□", 2: "×", 3: "○", 4: "△",
  5: "L1", 6: "R1", 7: "L2", 8: "R2",
  9: "SHARE", 10: "OPTIONS", 11: "L3", 12: "R3",
  13: "PS", 14: "触摸板",
};
const XB_BUTTONS = {
  A: "A", B: "B", X: "X", Y: "Y",
  LB: "LB", RB: "RB", LT: "LT", RT: "RT",
  START: "START", BACK: "BACK", LS: "LS", RS: "RS",
  UP: "十字上", DOWN: "十字下", LEFT: "十字左", RIGHT: "十字右",
};

const padLabel = (id) => {
  if (!id) return "";
  const m = /^winmm:B(\d+)$/.exec(id);
  if (m) {
    const n = Number(m[1]);
    return DS_BUTTONS[n] || ("按钮" + n);
  }
  const p = /^winmm:(POV_(?:UP|DOWN|LEFT|RIGHT))$/.exec(id);
  if (p) return { POV_UP: "十字上", POV_DOWN: "十字下", POV_LEFT: "十字左", POV_RIGHT: "十字右" }[p[1]];
  const x = /^xinput:(\w+)$/.exec(id);
  if (x) return XB_BUTTONS[x[1]] || x[1];
  return id;
};

function setPadInput(el, raw) {
  if (!el) return;
  el.dataset.raw = raw || "";
  const txt = raw ? padLabel(raw) : "";
  el.value = txt;
  el.title = raw || "";
  markPadGlyph(el, txt);
}

// 面键符号用统一大小的图形显示，避免 □△○× 字面大小参差
const PAD_OPTS = {
  ds: [
    ["", "未设置"],
    ["winmm:B5", "L1"],
    ["winmm:B6", "R1"],
    ["winmm:B7", "L2（左扳机）"],
  ],
  xbox: [
    ["", "未设置"],
    ["xinput:LB", "LB"],
    ["xinput:RB", "RB"],
    ["xinput:LT", "LT（左扳机）"],
  ],
};

// 连携技可以单独选，Xbox 上允许用面键 A/B/X/Y
const PAD_LINK_OPTS = {
  ds: PAD_OPTS.ds,
  xbox: [
    ["", "未设置"],
    ["xinput:A", "A"],
    ["xinput:B", "B"],
    ["xinput:X", "X"],
    ["xinput:Y", "Y"],
    ["xinput:LB", "LB"],
    ["xinput:RB", "RB"],
    ["xinput:LT", "LT（左扳机）"],
  ],
};

const PAD_SYM = { "□": "sq", "△": "tri", "○": "cir", "×": "cro" };

function markPadGlyph(el, txt) {
  const sym = PAD_SYM[txt];
  if (sym) {
    el.dataset.sym = sym;
    el.classList.add("filled");
  } else {
    delete el.dataset.sym;
    el.classList.remove("filled");
  }
}

function applyPadPreset(type, silent) {
  if (type === "xbox") {
    state.padType = "xbox";
    state.pad.modifier = "xinput:LB";
    state.pad.link = "xinput:RB";
    setPadInput($("#padSlot1"), "xinput:X");
    setPadInput($("#padSlot2"), "xinput:Y");
    setPadInput($("#padSlot3"), "xinput:B");
    setPadInput($("#padSlot4"), "xinput:A");
  } else {
    state.padType = "ds";
    state.pad.modifier = "winmm:B5";
    state.pad.link = "winmm:B6";
    setPadInput($("#padSlot1"), "winmm:B1");
    setPadInput($("#padSlot2"), "winmm:B4");
    setPadInput($("#padSlot3"), "winmm:B3");
    setPadInput($("#padSlot4"), "winmm:B2");
  }
  syncPadSelects();
  if (!silent) toast("已套用 " + (type === "xbox" ? "Xbox" : "DS") + " 手柄预设，记得点保存");
}

function guessPadType(names) {
  const s = (names || []).join(" ").toLowerCase();
  if (s.includes("xbox") || s.includes("xinput")) return "xbox";
  if (s.includes("dualsense") || s.includes("wireless controller") ||
      s.includes("ps5") || s.includes("ps4") || s.includes("dualshock")) return "ds";
  return "";
}

async function refreshPadDevices() {
  const el = $("#padDevices");
  if (!el) return;
  try {
    const r = await api.get("/api/pad_state");
    const list = (r && r.devices) || [];
    el.textContent = list.length ? list.join(" / ") : "未检测到手柄";
    const guess = guessPadType(list);
    if (guess) {
      const slots = (state.pad && state.pad.slots) || [];
      const hasWinmm = slots.some((s) => String(s).startsWith("winmm:"));
      const hasXinput = slots.some((s) => String(s).startsWith("xinput:"));
      const mismatch = (guess === "xbox" && hasWinmm && !hasXinput) ||
                       (guess === "ds" && hasXinput && !hasWinmm);
      if (mismatch || (guess !== state.padType && !hasWinmm && !hasXinput)) {
        applyPadPreset(guess, true);
        toast("检测到 " + (guess === "xbox" ? "Xbox" : "DS") + " 手柄，已自动配置");
      } else if (guess !== state.padType) {
        state.padType = guess;
        syncPadSelects();
      }
    }
  } catch (e) {
    el.textContent = "检测失败";
  }
}

let _padLearnTimer = null;
function learnPadKey(input) {
  if (_padLearnTimer) { clearInterval(_padLearnTimer); _padLearnTimer = null; }
  document.querySelectorAll(".padin").forEach((el) => el.classList.remove("learning"));
  input.classList.add("learning");
  input.value = "按键…";
  let baseline = null;

  _padLearnTimer = setInterval(async () => {
    try {
      const r = await api.get("/api/pad_state");
      const pressed = (r && r.pressed) || [];
      if (baseline === null) { baseline = new Set(pressed); return; }
      const fresh = pressed.filter((id) => !baseline.has(id));
      if (fresh.length) {
        setPadInput(input, fresh[0]);
        input.classList.remove("learning");
        clearInterval(_padLearnTimer);
        _padLearnTimer = null;
        toast("已记录：" + padLabel(fresh[0]));
      }
    } catch (e) { /* 忽略 */ }
  }, 120);
}

setInterval(async () => {
  const el = $("#padLive");
  if (!el || !el.offsetParent) return;      // 设置弹窗没开就不轮询
  try {
    const r = await api.get("/api/pad_state");
    const pressed = (r && r.pressed) || [];
    el.textContent = pressed.length ? pressed.map(padLabel).join(" + ") : "—";
    el.style.color = pressed.length ? "#c0392b" : "";
  } catch (e) { /* ignore */ }
}, 400);

function bindPadInputs() {
  document.querySelectorAll(".padin:not(.padslot)").forEach((el) => {
    el.addEventListener("click", () => learnPadKey(el));
    el.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      el.value = "";
      el.title = "";
      toast("已清空");
    });
  });
  
  const ds = $("#padDs");
  if (ds) ds.addEventListener("click", () => applyPadPreset("ds"));
  const xbox = $("#padXbox");
  if (xbox) xbox.addEventListener("click", () => applyPadPreset("xbox"));
  const btn = $("#padRefresh");
  if (btn) btn.addEventListener("click", refreshPadDevices);
}


function applyModeUI() {
  // 键鼠和手柄同时启用，两个区都显示
  const kb = $("#kbSection");
  const pd = $("#padSection");
  if (kb) kb.style.display = "";
  if (pd) pd.style.display = "";
}

let _apiFails = 0;
let _apiTimer = null;

function apiFail() {
  _apiFails += 1;
  // 重试之后还失败，连续 3 次才提示（避免保存时闪一下红条）
  if (_apiFails >= 3) {
    if (!updatingNow) showErr("后台进程没有响应。处理：关掉黑色命令行窗口，重新双击 启动.bat。");
  }
  if (!_apiTimer) {
    _apiTimer = setInterval(async () => {
      try {
        await fetch("/api/whoami", { cache: "no-store" });
        _apiFails = 0;
        hideErr();
        clearInterval(_apiTimer);
        _apiTimer = null;
      } catch (e) {
        // 继续等
      }
    }, 4000);
  }
}

function showErr(msg) {
  let bar = document.getElementById("errBar");
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "errBar";
    bar.style.cssText = "position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#b03a2e;" +
      "color:#fff;font:700 12px/1.6 'Microsoft YaHei UI';padding:6px 12px;text-align:center;";
    document.body.appendChild(bar);
  }
  bar.textContent = msg;
  bar.style.display = "block";
}

function hideErr() {
  _apiFails = 0;
  const bar = document.getElementById("errBar");
  if (bar) bar.style.display = "none";
}

/* ---------------- 关于（版本 / 来源 / 更新说明） ---------------- */
let _aboutCache = null;

async function loadAbout(force) {
  if (_aboutCache && !force) return _aboutCache;
  try {
    _aboutCache = await api.get("/api/about");
  } catch (e) {
    _aboutCache = null;
  }
  return _aboutCache;
}

async function fillAbout() {
  const a = await loadAbout();
  const ver = $("#aboutVer");
  if (!a) {
    if (ver) ver.textContent = "读不到版本信息";
    return;
  }
  if (ver) ver.textContent = `${a.version || "未知"}（${a.build || "-"} 构建）`;
}

function showNotesPanel(title, text) {
  let box = document.getElementById("notesBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "notesBox";
    box.style.cssText = "position:fixed;right:16px;bottom:16px;width:min(680px,90vw);" +
      "max-height:60vh;z-index:9998;background:#1e1f22;color:#e6e6e6;border:1px solid #444;" +
      "border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.45);display:flex;flex-direction:column;overflow:hidden;";
    box.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;' +
      'background:#2a2c30;font:700 12px \'Microsoft YaHei UI\'">' +
      '<span id="notesTitle" style="flex:1"></span>' +
      '<button id="notesCopy" class="mini" type="button">复制</button>' +
      '<button id="notesClose" class="mini" type="button">关闭</button></div>' +
      '<pre id="notesText" style="margin:0;padding:12px;overflow:auto;white-space:pre-wrap;' +
      'font:12px/1.6 \'Microsoft YaHei UI\'"></pre>';
    document.body.appendChild(box);
    box.querySelector("#notesClose").onclick = () => { box.style.display = "none"; };
    box.querySelector("#notesCopy").onclick = async () => {
      try {
        await navigator.clipboard.writeText(box.querySelector("#notesText").textContent || "");
        toast("已复制");
      } catch (e) { toast("复制失败，手动选中吧"); }
    };
  }
  box.querySelector("#notesTitle").textContent = title || "更新说明";
  box.querySelector("#notesText").textContent = text || "";
  box.style.display = "flex";
}

async function showAboutNotes() {
  const a = await loadAbout(true);
  if (!a || !a.notes) return toast("这个版本没带更新说明");
  showNotesPanel(`${a.version || ""} 更新了什么`, a.notes);
}

/* ---------------- 最近日志（出问题时能截图给作者） ---------------- */
async function showLastLog() {
  let box = document.getElementById("logBox");
  if (!box) {
    box = document.createElement("div");
    box.id = "logBox";
    box.style.cssText = "position:fixed;right:16px;bottom:16px;width:min(620px,86vw);" +
      "max-height:52vh;z-index:9998;background:#1e1f22;color:#e6e6e6;border:1px solid #444;" +
      "border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,.45);display:flex;flex-direction:column;overflow:hidden;";
    box.innerHTML = '<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;' +
      'background:#2a2c30;font:700 12px \'Microsoft YaHei UI\'">' +
      '<span style="flex:1">最近日志（末尾 40 行）</span>' +
      '<button id="logCopy" class="mini" type="button">复制</button>' +
      '<button id="logClose" class="mini" type="button">关闭</button></div>' +
      '<pre id="logText" style="margin:0;padding:10px;overflow:auto;white-space:pre-wrap;' +
      'font:12px/1.5 Consolas,monospace"></pre>';
    document.body.appendChild(box);
    box.querySelector("#logClose").onclick = () => { box.style.display = "none"; };
    box.querySelector("#logCopy").onclick = async () => {
      const t = box.querySelector("#logText").textContent || "";
      try { await navigator.clipboard.writeText(t); toast("日志已复制"); }
      catch (e) { toast("复制失败，手动选中吧"); }
    };
  }
  box.style.display = "flex";
  const pre = box.querySelector("#logText");
  pre.textContent = "读取中…";
  try {
    const d = await api.get("/api/last-error");
    pre.textContent = (d && d.log) || "（日志是空的，说明还没出过错）";
  } catch (e) {
    pre.textContent = "取不到日志：" + e.message;
  }
}

async function boot() {
  const cfg = await api.get("/api/state");

  if (cfg.links) {
    LINKS.bili = cfg.links.bili || LINKS.bili;
    LINKS.biliName = cfg.links.biliName || LINKS.biliName;
    LINKS.pan = cfg.links.pan || LINKS.pan;
  }

  const rawTeam = Array.isArray(cfg.team) ? cfg.team : [];
  state.team = [0, 1, 2, 3].map((i) => (typeof rawTeam[i] === "string" ? rawTeam[i] : ""));
  state.steps = Array.isArray(cfg.steps) ? cfg.steps : [];
  state.teams = Array.isArray(cfg.teams) ? cfg.teams : [];
  state.holdMs = cfg.holdMs ?? 280;
  state.opacity = cfg.opacity ?? 0.94;
  state.iconSize = cfg.iconSize ?? 60;
  state.uiScale = Number(cfg.uiScale) > 0 ? Number(cfg.uiScale) : 1;
  state.nextPreview = cfg.nextPreview !== 0 && cfg.nextPreview !== false;
  state.autoExit = cfg.autoExit === 1 || cfg.autoExit === true;
  state.overlayY = cfg.overlayY ?? 150;
  state.screenMode = cfg.screenMode || "game";
  state.overlayX = cfg.overlayX ?? null;
  state.inputMode = cfg.inputMode === "pad" ? "pad" : "kb";
  state.padType = cfg.padType === "xbox" ? "xbox" : "ds";
  state.comps = Array.isArray(cfg.comps) ? cfg.comps : [];
  state.loopCount = Number(cfg.loopCount || 0);
  state.loopFrom = Number(cfg.loopFrom || 0);
  state.loopTo = Number(cfg.loopTo || 0);
  state.displayMode = cfg.displayMode === "pad" ? "pad" : "kb";
  const st = cfg.overlayStyle === "classic" ? "box" : (cfg.overlayStyle || "focus");
  state.overlayStyle = ["focus", "box", "both"].includes(st) ? st : "focus";
  state.pad = Object.assign(
    {
      modifier: "winmm:B5",
      slots: ["winmm:B1", "winmm:B4", "winmm:B3", "winmm:B2"],
      link: "winmm:B6", exit: "", reset: "",
    },
    cfg.pad || {}
  );
  state.keys = Object.assign(
    { skill: ["1", "2", "3", "4"], ult: ["", "", "", ""], link: "E", exit: "F8", reset: "F9" },
    cfg.keys || {}
  );

  try { $("#biliName").textContent = LINKS.biliName; } catch (e) {}

  state.ops = await api.get("/api/ops");
  bindKeyInputs();
  bindAlphaControls();
  bindPadInputs();
  renderSlots();
  renderSeq();
  renderComps();          // 以前漏了这句：重启后编队库显示 0 套，数据其实还在
  renderLib();
}

const opInfo = (name) => state.ops.find((o) => o.name === name) || null;
const cardUrl = (name) => `/img/card?name=${encodeURIComponent(name)}`;
const iconUrl = (name, kind, slot) =>
  `/img/icon?name=${encodeURIComponent(name || "")}&kind=${kind}&slot=${slot}`;

// 只要这一位有干员，就让后端出图（缺素材时后端只画底层）
function hasIcon(name, kind) {
  return !!name;
}

/* ---------------- 编队槽 ---------------- */
function swapSlots(a, b) {
  if (a === b) return;
  if (a < 0 || b < 0 || a > 3 || b > 3) return;
  snapshot();                       // 可撤销
  const t = state.team.slice();
  [t[a], t[b]] = [t[b], t[a]];
  state.team = t;
  // 轴里的技能跟着人走：位置号换了，战技/终结技的键位也要一起换。
  // （只换 op 会让「条上写的是谁」和「实际按的是哪个键」对不上，跑的时候放错人。）
  state.steps.forEach((s) => {
    if (typeof s.op !== "number") return;
    const old = s.op;
    let op = old;
    if (op === a + 1) op = b + 1;
    else if (op === b + 1) op = a + 1;
    if (op === old) return;
    s.op = op;
    const kk = String(s.key == null ? "" : s.key);
    if (/^[1-4]$/.test(kk)) {          // 战技 / 终结技：键位就是位置
      if (kk === String(a + 1)) s.key = String(b + 1);
      else if (kk === String(b + 1)) s.key = String(a + 1);
    }
    s.who = state.team[op - 1] || "";
    s.label = labelOf(s);
  });
  renderSlots(); renderSeq(); persistSoon();
  toast(`已交换 ${a + 1} ↔ ${b + 1}，轴里的按键跟着换`);
}

function renderSlots() {
  const box = $("#slots");
  box.innerHTML = "";
  state.team.forEach((rawName, i) => {
    const name = (rawName || "").trim();
    const d = document.createElement("div");
    d.className = "slot" + (name ? " filled" : "");
    d.innerHTML = `<span class="no">${i + 1}</span>`;
    if (name) {
      const img = document.createElement("img");
      img.draggable = false;
      img.src = cardUrl(name);
      img.onerror = () => {
        img.remove();
        const e2 = document.createElement("span");
        e2.className = "empty";
        e2.textContent = "选择干员";
        d.appendChild(e2);
      };
      d.appendChild(img);
    } else {
      const e = document.createElement("span");
      e.className = "empty";
      e.textContent = "选择干员";
      d.appendChild(e);
    }
    d.onclick = () => openPicker(i, false);
    d.draggable = true;
    d.dataset.i = i;
    d.addEventListener("dragstart", (ev) => {
      ev.dataTransfer.setData("text/plain", String(i));
      ev.dataTransfer.effectAllowed = "move";
    });
    d.addEventListener("dragover", (ev) => {
      ev.preventDefault();
      ev.dataTransfer.dropEffect = "move";
      d.classList.add("drag-over");
    });
    d.addEventListener("dragleave", () => d.classList.remove("drag-over"));
    d.addEventListener("drop", (ev) => {
      ev.preventDefault();
      d.classList.remove("drag-over");
      const from = parseInt(ev.dataTransfer.getData("text/plain"), 10);
      if (!isNaN(from)) swapSlots(from, i);
    });
    box.appendChild(d);
  });
  $("#teamHint").textContent = `${state.team.filter(Boolean).length} / 4 名干员`;
  renderAdder();
}

/* ---------------- 选人 ---------------- */
function openPicker(slot, quick) {
  state.pickSlot = slot;
  state.quick = !!quick;
  state.pickElem = "";
  $("#pickerTitle").textContent = quick ? "快捷编队 · 点卡片加入" : `选择第 ${slot + 1} 位干员`;
  $("#picker").classList.remove("hidden");
  renderTabs();
  renderGrid();
}

function elems() {
  const out = [];
  state.ops.forEach((o) => { if (o.element && !out.includes(o.element)) out.push(o.element); });
  return ["全部", ...out];
}

function renderTabs() {
  const box = $("#pickerTabs");
  box.innerHTML = "";
  elems().forEach((e) => {
    const b = document.createElement("div");
    b.className = "tab" + ((state.pickElem || "全部") === e ? " on" : "");
    b.textContent = e;
    b.onclick = () => { state.pickElem = e === "全部" ? "" : e; renderTabs(); renderGrid(); };
    box.appendChild(b);
  });
}

function renderGrid() {
  const box = $("#pickerGrid");
  box.innerHTML = "";
  state.ops
    .filter((o) => !state.pickElem || o.element === state.pickElem)
    .slice()
    .sort((a, b) => {
      // 最近导入的排最前（按导入先后），其余保持原有顺序不动
      const ra = a.recent == null ? -1 : a.recent;
      const rb = b.recent == null ? -1 : b.recent;
      if (ra >= 0 && rb >= 0) return ra - rb;
      if (ra >= 0) return -1;
      if (rb >= 0) return 1;
      return (a.slot || 0) - (b.slot || 0);
    })
    .forEach((o) => {
      const c = document.createElement("div");
      c.className = "card";
      const img = document.createElement("img");
      img.src = cardUrl(o.name);
      img.alt = o.name;
      // 卡面加载失败时显示名字，不要留个破图标
      img.onerror = () => {
        img.remove();
        const ph = document.createElement("div");
        ph.className = "card-missing";
        ph.textContent = o.name;
        c.appendChild(ph);
      };
      c.appendChild(img);
      c.onclick = () => pickOperator(o.name);
      box.appendChild(c);
    });
}

function pickOperator(name) {
  if (state.quick) {
    const i = state.team.indexOf(name);
    if (i >= 0) state.team[i] = "";
    else {
      const empty = state.team.indexOf("");
      const slot = empty >= 0 ? empty : 0;
      state.team[slot] = name;
      state.team.forEach((n, k) => { if (k !== slot && n === name) state.team[k] = ""; });
    }
    renderSlots();
    renderSeq();        // 立刻刷新右边的图标
    persistSoon();
    return;
  }
  // 点同一个干员 = 取消这个位置的选择
  if ((state.team[state.pickSlot] || "") === name) {
    state.team[state.pickSlot] = "";
    renderSlots();
    renderSeq();
    closePicker();
    toast(`已取消第 ${state.pickSlot + 1} 位`);
    persistSoon();
    return;
  }
  const old = state.team.indexOf(name);
  if (old >= 0 && old !== state.pickSlot) state.team[old] = "";
  state.team[state.pickSlot] = name;
  renderSlots();
  closePicker();
  renderSeq();          // 图标要跟着换
  persistSoon();
}

const closePicker = () => $("#picker").classList.add("hidden");

/* ---------------- 序列 ---------------- */
function labelOf(s) {
  const op = s.op || 1;
  const who = state.team[op - 1] || `${op} 号`;
  if (String(s.key).toUpperCase() === "E") return `${who} · 连携技`;
  return `${who} · ${s.hold ? "终结技" : "战技"}`;
}

function addSkill(key, hold, slot) {
  snapshot();
  const op = slot || (String(key).toUpperCase() === "E" ? 1 : Number(key));
  const s = { key: String(key), hold: !!hold, op, who: state.team[op - 1] || "" };
  s.label = labelOf(s);
  let at = state.caret == null ? state.steps.length : state.caret;
  at = Math.max(0, Math.min(state.steps.length, at));
  state.steps.splice(at, 0, s);
  state.caret = at + 1;        // 光标后移
  state.sel = null;
  renderSeq();
  persistSoon();
}

function addHeavy() {
  snapshot();
  let at = state.caret == null ? state.steps.length : state.caret;
  at = Math.max(0, Math.min(state.steps.length, at));
  state.steps.splice(at, 0, { heavy: true });
  state.caret = at + 1;
  state.sel = null;
  renderSeq();
  persistSoon();
}

function renderAdder() {
  // 一组：标签在左（下方可带修饰键），按钮各自带数字，中间放 +
  const buildGroup = (box, label, mod, items) => {
    box.innerHTML = "";
    const pre = document.createElement("div");
    pre.className = "adder-prefix";
    const lb = document.createElement("span");
    lb.className = "adder-label";
    lb.textContent = label;
    pre.appendChild(lb);
    if (mod) {
      const md = document.createElement("span");
      md.className = "adder-mod";
      md.textContent = mod;
      pre.appendChild(md);
    }
    box.appendChild(pre);

    if (mod) {
      const plus = document.createElement("span");
      plus.className = "adder-plus";
      plus.textContent = "+";
      box.appendChild(plus);
    }

    items.forEach(({ btn, hint }) => {
      const col = document.createElement("div");
      col.className = "adder-col";
      col.appendChild(btn);
      const k = document.createElement("span");
      k.className = "adder-key";
      k.textContent = hint;
      col.appendChild(k);
      box.appendChild(col);
    });
  };

  const mk = (key, hold) => {
    const op = Number(key);
    const name = state.team[op - 1] || "";
    const kind = hold ? "ult" : "skill";
    const b = document.createElement("button");
    b.className = "skill-btn" + (hold ? " ult" : "");
    b.dataset.key = key;
    if (hold) b.dataset.hold = "1";
    if (name) {
      b.innerHTML = `<img src="${iconUrl(name, kind, op)}" alt="">`;
    } else {
      b.textContent = key;
    }
    b.onclick = () => addSkill(key, hold);
    return b;
  };

  buildGroup($("#adderSkill"), "战技", "Q",
    [1, 2, 3, 4].map((k) => ({ btn: mk(k, false), hint: String(k) })));

  buildGroup($("#adderUlt"), "终结技", "Alt",
    [1, 2, 3, 4].map((k) => ({ btn: mk(k, true), hint: String(k) })));

  const linkItems = [1, 2, 3, 4].map((slot) => {
    const name = state.team[slot - 1] || "";
    const b = document.createElement("button");
    b.className = "skill-btn";
    if (name) {
      b.innerHTML = `<img src="${iconUrl(name, "link", slot)}" alt="">`;
    } else {
      b.textContent = slot;
    }
    b.title = `连携技 · 第 ${slot} 位（E 然后按 ${slot}）`;
    b.onclick = () => addSkill("E", false, slot);
    return { btn: b, hint: String(slot) };
  });
  buildGroup($("#adderLink"), "连携技", "E", linkItems);

  // 重击：保留原按钮，补一个 H 说明
  const heavyBox = $("#adderHeavy");
  const heavyBtn = $("#btnHeavy");
  if (heavyBox && heavyBtn) {
    heavyBox.innerHTML = "";
    const pre = document.createElement("div");
    pre.className = "adder-prefix";
    const lb = document.createElement("span");
    lb.className = "adder-label";
    lb.textContent = "重击";
    pre.appendChild(lb);
    heavyBox.appendChild(pre);

    const col = document.createElement("div");
    col.className = "adder-col";
    col.appendChild(heavyBtn);
    const hk = document.createElement("span");
    hk.className = "adder-key";
    hk.textContent = "H";
    col.appendChild(hk);
    heavyBox.appendChild(col);
  }
}

function addNote() {
  const i = state.sel;
  // 没选中任何格子 → 插入一条「纯注释」（只在运行条上显示提示，不需要按键）
  if (i == null || i >= state.steps.length) {
    const txt = prompt("写一条注释（只是提示，不用按键）：", "");
    if (txt === null) return;
    const s = txt.trim();
    if (!s) return;
    snapshot();
    let at = state.caret == null ? state.steps.length : state.caret;
    at = Math.max(0, Math.min(state.steps.length, at));
    state.steps.splice(at, 0, { note: s });
    state.caret = at + 1;
    state.sel = null;
    renderSeq();
    persistSoon();
    toast("已插入一条注释");
    return;
  }
  const cur = state.steps[i].note || "";
  const txt = prompt("给这个技能加注释：", cur);
  if (txt === null) return;
  snapshot();
  state.steps[i].note = txt.trim();
  renderSeq();
  persistSoon();
}

function moveSel(dir) {
  const list = selIdx();
  if (!list.length) return toast("先选中技能");
  const n = state.steps.length;
  if ((dir < 0 && list[0] <= 0) || (dir > 0 && list[list.length - 1] >= n - 1)) return;
  snapshot();
  const picked = list.map((i) => state.steps[i]);
  const rest = state.steps.filter((_, i) => !list.includes(i));
  const at = Math.max(0, Math.min(rest.length, list[0] + dir));
  state.steps = [...rest.slice(0, at), ...picked, ...rest.slice(at)];
  state.selSet = picked.map((_, k) => at + k);
  state.sel = state.selSet[state.selSet.length - 1];
  renderSeq();
  persistSoon();
}

function padType() {
  return state.padType === "xbox" ? "xbox" : "ds";
}

function fillPadSelect(el, list) {
  if (!el) return;
  const cur = el.value;
  el.innerHTML = "";
  (list || PAD_OPTS[padType()]).forEach(([v, label]) => {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = label;
    el.appendChild(o);
  });
  if ([...el.options].some((o) => o.value === cur)) el.value = cur;
}

function syncPadSelects() {
  const m = $("#padMod");
  const l = $("#padLink");
  if (!m || !l) return;
  fillPadSelect(m, PAD_OPTS[padType()]);
  fillPadSelect(l, PAD_LINK_OPTS[padType()]);
  m.value = (state.pad && state.pad.modifier) || "";
  l.value = (state.pad && state.pad.link) || "";
  if (m.value === "" && state.pad && state.pad.modifier) {
    // 换类型后原绑定不适用，清掉避免用错键
    state.pad.modifier = "";
  }
  if (l.value === "" && state.pad && state.pad.link) {
    state.pad.link = "";
  }
}

/* ---------------- 设置：左侧导航分页 ---------------- */
let _setPane = "overlay";

function showSettingsPane(name) {
  if (!name) name = "overlay";
  const panes = document.querySelectorAll("#setPanes .set-pane");
  let hit = false;
  panes.forEach((p) => {
    const on = p.dataset.pane === name;
    p.hidden = !on;
    if (on) hit = true;
  });
  if (!hit) {                        // 名字对不上（改过 HTML）：退回第一页
    const first = panes[0];
    if (first) { first.hidden = false; name = first.dataset.pane; }
  }
  document.querySelectorAll("#setNav button").forEach((b) => {
    b.classList.toggle("on", b.dataset.pane === name);
  });
  _setPane = name;
  const box = $("#setPanes");
  if (box) box.scrollTop = 0;
}

document.querySelectorAll("#setNav button").forEach((b) => {
  b.onclick = () => showSettingsPane(b.dataset.pane);
});

function openSettings() {
  showSettingsPane(_setPane);

  $("#setHold").value = state.holdMs;
  $("#setAlpha").value = state.opacity ?? 0.94;
  $("#setIcon").value = state.iconSize ?? 60;
  fillAbout();
  if ($("#setScale")) $("#setScale").value = (state.uiScale && state.uiScale !== 1) ? state.uiScale : "";
  if ($("#nextPrevOn")) $("#nextPrevOn").checked = state.nextPreview !== false;
  if ($("#setAutoExit")) $("#setAutoExit").checked = state.autoExit === true;
  if ($("#nextPrevOff")) $("#nextPrevOff").checked = state.nextPreview === false;
  if ($("#setScreen")) $("#setScreen").value = state.screenMode || "game";
  refreshScreenHint();
  $("#setOY").value = state.overlayY ?? 150;
  $("#setOX").value = state.overlayX == null ? "" : state.overlayX;
  if ($("#setLoop")) $("#setLoop").value = state.loopCount == null ? 0 : state.loopCount;
  if ($("#setLoopFrom")) $("#setLoopFrom").value = state.loopFrom || 0;
  if ($("#setLoopTo")) $("#setLoopTo").value = state.loopTo || 0;
  const segs = (state.steps || []).filter((x) => x.divider).length + 1;
  if ($("#loopHint")) {
    $("#loopHint").textContent =
      `当前共 ${segs} 段。想让「第2段 → 第3段」反复打，就填 2 和 3；` +
      `只想打完后从某段再开始，就把起点填那段、终点留 0。循环次数是总共跑几遍。`;
  }
  if ($("#modeDisplayKb")) $("#modeDisplayKb").checked = state.displayMode !== "pad";
  if ($("#modeDisplayPad")) $("#modeDisplayPad").checked = state.displayMode === "pad";
  for (let i = 0; i < 4; i++) {
    $("#setSkill" + (i + 1)).value = state.keys.skill[i] || "";
    $("#setUlt" + (i + 1)).value = state.keys.ult[i] || "";
  }
  $("#setLink").value = state.keys.link || "";
  $("#setExit").value = state.keys.exit || "";
  $("#setReset").value = state.keys.reset || "";
  if ($("#setLoopToggle")) $("#setLoopToggle").value = state.keys.loopToggle || "";
  if ($("#setLoopJump")) $("#setLoopJump").value = state.keys.loopJump || "";
  if ($("#setAlphaDown")) $("#setAlphaDown").value = state.keys.alphaDown || "";
  if ($("#setAlphaUp")) $("#setAlphaUp").value = state.keys.alphaUp || "";

  $("#modeStyleFocus").checked = (state.overlayStyle || "focus") === "focus";
  $("#modeStyleBox").checked = state.overlayStyle === "box";
  $("#modeStyleBoth").checked = state.overlayStyle === "both";

  const pad = state.pad || {};
  const slots = pad.slots || ["", "", "", ""];
  for (let i = 0; i < 4; i++) setPadInput($("#padSlot" + (i + 1)), slots[i] || "");
  syncPadSelects();
  refreshPadDevices();
  applyModeUI();
  $("#setModal").classList.remove("hidden");
}

function saveSettings() {
  const h = parseInt($("#setHold").value, 10);
  const a = parseFloat($("#setAlpha").value);
  state.holdMs = Math.max(80, Math.min(2000, isNaN(h) ? 280 : h));
  state.opacity = Math.max(0.3, Math.min(1, isNaN(a) ? 0.94 : a));

  const sz = parseInt($("#setIcon").value, 10);
  const oy = parseInt($("#setOY").value, 10);
  const oxRaw = $("#setOX").value.trim();
  const ox = oxRaw === "" ? null : parseInt(oxRaw, 10);

  state.iconSize = Math.max(32, Math.min(140, isNaN(sz) ? 60 : sz));
  const scRaw = parseFloat($("#setScale") ? $("#setScale").value : "");
  state.uiScale = isNaN(scRaw) ? 1 : Math.max(0.7, Math.min(1.5, scRaw));
  state.nextPreview = !($("#nextPrevOff") && $("#nextPrevOff").checked);
  state.overlayY = Math.max(0, Math.min(2000, isNaN(oy) ? 150 : oy));
  state.overlayX = (ox == null || isNaN(ox) || ox < 0) ? null : ox;

  state.displayMode = ($("#modeDisplayPad") && $("#modeDisplayPad").checked) ? "pad" : "kb";

  const lc = parseInt($("#setLoop") ? $("#setLoop").value : "0", 10);
  state.loopCount = isNaN(lc) ? 0 : Math.max(-1, Math.min(999, lc));
  const lf = parseInt($("#setLoopFrom") ? $("#setLoopFrom").value : "0", 10);
  const lt = parseInt($("#setLoopTo") ? $("#setLoopTo").value : "0", 10);
  state.loopFrom = isNaN(lf) ? 0 : Math.max(0, Math.min(99, lf));
  state.loopTo = isNaN(lt) ? 0 : Math.max(0, Math.min(99, lt));

  const ks = state.keys || {};
  ks.skill = [1, 2, 3, 4].map((i) => ($("#setSkill" + i).value || String(i)).toUpperCase());
  ks.ult = [1, 2, 3, 4].map((i) => ($("#setUlt" + i).value || "").toUpperCase());
  ks.link = ($("#setLink").value || "E").toUpperCase();
  ks.exit = ($("#setExit").value || "F8").toUpperCase();
  ks.reset = ($("#setReset").value || "F9").toUpperCase();
  ks.loopToggle = ($("#setLoopToggle").value || "F10").toUpperCase();
  ks.loopJump = ($("#setLoopJump").value || "F11").toUpperCase();
  ks.alphaDown = ($("#setAlphaDown").value || "F6").toUpperCase();
  ks.alphaUp = ($("#setAlphaUp").value || "F7").toUpperCase();
  state.keys = ks;

  state.overlayStyle = $("#modeStyleBoth").checked ? "both"
    : ($("#modeStyleBox").checked ? "box" : "focus");
  const raw = (sel) => {
    const el = $(sel);
    return (el && el.dataset && el.dataset.raw) ? el.dataset.raw : "";
  };
  state.pad = {
    modifier: ($("#padMod").value || ""),
    slots: [1, 2, 3, 4].map((i) => raw("#padSlot" + i)),
    link: ($("#padLink").value || ""),
    exit: "",
    reset: "",
  };
  if ($("#setAutoExit")) state.autoExit = $("#setAutoExit").checked;

  const newMode = $("#setScreen") ? $("#setScreen").value : (state.screenMode || "game");
  const modeChanged = newMode !== (state.screenMode || "game");
  state.screenMode = newMode;
  state.padType = padType();
  persist();
  // 运行条正在跑的话，透明度改完立刻生效，不用重新开始
  api.post("/api/overlay/alpha", { opacity: state.opacity }).catch(() => {});
  syncAlphaUI();
  api.post("/api/state", { overlayX: state.overlayX, overlayY: state.overlayY }).catch(() => {});
  if (modeChanged) applyScreenMode(state.screenMode, true);
  toast("已保存");
}

function clearSeq() {
  if (!state.steps.length) return;
  if (!confirm("清空整条技能轴？")) return;
  snapshot();
  state.steps = [];
  state.sel = null;
  state.caret = null;
  state.loadedTeam = null;      // 清空后算“新编队”，保存时不再问覆盖
  renderSeq();
  api.post("/api/state", { steps: [], force: true });
  toast("已清空，保存时会当作新编队");
}

function addDivider() {
  const name = prompt("分段名称（可留空）：", "分段");
  if (name === null) return;
  snapshot();
  const at = state.caret == null ? state.steps.length : state.caret;
  state.steps.splice(at, 0, { divider: true, name: name.trim() });
  state.caret = at + 1;
  renderSeq();
  persistSoon();
}

function addBreak() {
  snapshot();
  const at = state.caret == null ? state.steps.length : state.caret;
  state.steps.splice(at, 0, { br: true });
  state.caret = at + 1;
  renderSeq();
  persistSoon();
}

function deleteAt(i) {
  if (i == null || i < 0 || i >= state.steps.length) return false;
  snapshot();
  // 反复记号：删起点=整组一起删；删终点=只删它（最后一个就带上起点）
  const idxs = repDeleteSet(i).slice().sort((a, b) => b - a);
  idxs.forEach((k) => state.steps.splice(k, 1));
  if (state.caret != null) {
    state.caret -= idxs.filter((k) => k < state.caret).length;
    state.caret = Math.max(0, Math.min(state.steps.length, state.caret));
  }
  state.sel = null;
  renderSeq();
  persistSoon();
  return true;
}

function selIdx() {
  const set = new Set(state.selSet || []);
  if (state.sel != null) set.add(state.sel);
  return [...set].filter((i) => i >= 0 && i < state.steps.length).sort((a, b) => a - b);
}

function toggleSel(i, additive) {
  if (!additive) {
    state.sel = i;
    state.selSet = [i];
  } else {
    const s = new Set(state.selSet || []);
    if (s.has(i)) s.delete(i); else s.add(i);
    state.selSet = [...s];
    state.sel = state.selSet.length ? state.selSet[state.selSet.length - 1] : null;
  }
}

function deleteSel() {
  const raw = selIdx();
  if (!raw.length) return toast("先选中技能");
  // 反复记号连另一半一起删
  const set = new Set(raw);
  raw.forEach((i) => { repDeleteSet(i).forEach((k) => set.add(k)); });
  const list = [...set].sort((a, b) => a - b);
  snapshot();
  for (let k = list.length - 1; k >= 0; k--) state.steps.splice(list[k], 1);
  state.sel = null;
  state.selSet = [];
  state.caret = null;
  renderSeq();
  persistSoon();
  toast("已删除 " + list.length + " 项");
}

function backspace() {
  // 有选中就先删选中的，符合大多数人习惯
  if (selIdx().length) {
    deleteSel();
    return;
  }
  if (state.caret == null) {
    if (state.steps.length) { state.caret = state.steps.length; }
    else return;
  }
  if (state.caret <= 0) return;
  deleteAt(state.caret - 1);
}


/* ---------------- 反复记号（乐谱式：可嵌套、一个起点可带多个终点） ---------------- */
// 同一个「组」里：第一个记号是起点，后面可以挂好几个终点。
// 每个「起点→终点」都是独立的一段：跑到那个终点就跳回起点，各自记各自的次数。
// 嵌套：被别的段严格包住就算内层，内层在外层每一遍里都重新计数。
// 配对：点一个未配对记号（变虚框）→ 点另一个；已配对的起点还能再接新的终点。
let repPend = null;          // 等配对的那个记号（存对象本身，插删步骤也不会配错）

const REP_COLORS = ["#c9a227", "#2f7fd0", "#7a4fb5", "#1f8a70"];

function repGroupIdxs(gid) {
  const out = [];
  state.steps.forEach((s, k) => { if (s && s.rep && s.link === gid) out.push(k); });
  return out;
}

// 反复次数：-1 = 无限循环，其余 1~99（0 / 非法按 2）
function repTimes(v) {
  const n = parseInt(v, 10);
  if (isNaN(n) || n === 0) return 2;
  if (n < 0) return -1;
  return Math.min(99, n);
}

function repTimesText(n) {
  return n < 0 ? "×∞" : "×" + n;
}

function repTimesWords(n) {
  return n < 0 ? "无限循环（按「停止运行」或 F8 才停）" : "打 " + n + " 遍";
}

function repId() {
  return "r" + Date.now().toString(36) + Math.floor(Math.random() * 9999).toString(36);
}

// 这一组的起点在哪（没有就 -1）
function repGroupStart(gid) {
  if (!gid) return -1;
  for (let k = 0; k < state.steps.length; k++) {
    const s = state.steps[k];
    if (s && s.rep && s.link === gid && !s.end) return k;
  }
  return -1;
}

// 这一组挂的所有终点
function repGroupEnds(gid) {
  const out = [];
  if (!gid) return out;
  state.steps.forEach((s, k) => {
    if (s && s.rep && s.link === gid && s.end) out.push(k);
  });
  return out;
}

// 删这个记号时要连带删哪些（起点删=整组；终点删=只删它，删光了连起点一起删）
function repDeleteSet(i) {
  const s = state.steps[i];
  if (!s || !s.rep || !s.link) return [i];
  const st = repGroupStart(s.link);
  const ends = repGroupEnds(s.link);
  if (st === i) return [i].concat(ends);
  if (ends.length <= 1) return st >= 0 ? [i, st] : [i];
  return [i];
}

// 所有「起点→终点」区间（每个终点一条），带各自的次数和层数
function repPairs() {
  const pairs = [];
  state.steps.forEach((s, i) => {
    if (!s || !s.rep || !s.link || !s.end) return;
    const st = repGroupStart(s.link);
    if (st < 0 || st >= i) return;
    pairs.push({ gid: s.link, s: st, e: i, times: repTimes(s.times) });
  });
  // 层数 = 被多少个「别的区间」严格包住
  pairs.forEach((p) => {
    let d = 0;
    pairs.forEach((o) => {
      if (o === p) return;
      if (o.s <= p.s && o.e >= p.e && (o.s < p.s || o.e > p.e)) d++;
    });
    p.depth = d;
  });
  return pairs;
}

// 每个记号该显示成什么样
function repScan() {
  const pairs = repPairs();
  const info = new Map();
  const byEnd = new Map();
  pairs.forEach((p) => byEnd.set(p.e, p));

  state.steps.forEach((s, i) => {
    if (!s || !s.rep) return;
    if (!s.link) { info.set(i, { depth: 0, role: "loose" }); return; }
    if (!s.end) {
      const mine = pairs.filter((p) => p.s === i);
      const outer = mine.length ? Math.min.apply(null, mine.map((p) => p.depth)) : 0;
      let nearest = 0;
      mine.forEach((p) => {
        const n = state.steps.slice(i + 1, p.e)
          .filter((o) => o && !o.divider && !o.br && !o.rep).length;
        if (!nearest || n < nearest) nearest = n;
      });
      info.set(i, { depth: outer, role: "start", pairs: mine, ends: mine.length, size: nearest });
      return;
    }
    const p = byEnd.get(i);
    if (!p) { info.set(i, { depth: 0, role: "end" }); return; }
    let cnt = 0;
    for (let q = p.s + 1; q < i; q++) {
      const o = state.steps[q];
      if (!o || o.divider || o.br || o.rep) continue;
      cnt++;
    }
    info.set(i, { depth: p.depth, role: "end", partner: p.s, size: cnt, times: p.times });
  });
  return info;
}

// 新加一段会不会和已有的交叉（交叉了运行顺序就没法算）
function repCross(s, e, gid) {
  return repPairs().some((p) => {
    if (gid && p.gid === gid) return false;      // 同一组的多个终点不算交叉
    return (s < p.s && p.s < e && e < p.e) || (p.s < s && s < p.e && p.e < e);
  });
}

function makeRepRow(s, i, info) {
  const it = info.get(i) || { depth: 0, role: s.link ? (s.end ? "end" : "start") : "loose" };
  const depth = Math.min(it.depth || 0, REP_COLORS.length - 1);
  const linked = it.role === "start" || it.role === "end";
  const isStart = it.role === "start";
  const n = repTimes(it.times != null ? it.times : s.times);

  const row = document.createElement("div");
  row.className = "rep " + (linked ? "on" : "off")
    + (isStart ? " start" : (linked ? " end" : ""))
    + (state.sel === i ? " sel" : "")
    + (repPend === s ? " pend" : "");
  row.dataset.i = i;
  row.style.setProperty("--rc", REP_COLORS[depth]);
  row.style.setProperty("--ind", (depth * 18) + "px");
  row.title = linked
    ? (isStart ? "反复起点：跑到配对的终点就跳回这里（可以再挂多个终点）"
               : "反复终点：跑到这里就跳回起点（双击改次数）")
    : "未配对：点它，再点另一个记号，就配成一对";

  const tag = document.createElement("span");
  tag.className = "rep-tag";
  tag.textContent = !linked ? "↻ 反复（未配对）" : (isStart ? "↻ 反复起点" : "↺ 反复终点");
  row.appendChild(tag);

  if (linked) {
    if (isStart) {
      // 起点只显示“挂了几个终点”，次数在各个终点上改
      if ((it.ends || 0) > 1) {
        const pill = document.createElement("span");
        pill.className = "rep-pill";
        pill.textContent = (it.ends) + " 个终点";
        row.appendChild(pill);
      }
      const sp = document.createElement("span");
      sp.className = "rep-span";
      const n2 = it.ends || 0;
      sp.textContent = n2 > 1
        ? "↓ 到最近终点为止" + (it.size ? "（" + it.size + " 步）" : "")
        : "↓ 到终点为止" + (it.size ? "（" + it.size + " 步）" : "");
      row.appendChild(sp);

      const addEnd = document.createElement("b");
      addEnd.className = "rep-quick";
      addEnd.textContent = "＋ 终点";
      addEnd.title = "再挂一个终点：点它，再点下面那个未配对的记号";
      addEnd.onclick = (e) => {
        e.stopPropagation();
        repPend = (repPend === s) ? null : s;
        renderSeq();
        if (repPend) toast("再点一个未配对的记号，就挂到这个起点下面");
      };
      row.appendChild(addEnd);
    } else {
      const box = document.createElement("span");
      box.className = "rep-times";
      const mk = (txt, tip, d) => {
        const b = document.createElement("b");
        b.textContent = txt;
        b.title = tip;
        b.onclick = (e) => {
          e.stopPropagation();
          setEndTimes(i, n < 0 ? 2 : n + d);   // 无限时先回到 2 遍
        };
        return b;
      };
      const val = document.createElement("i");
      val.textContent = repTimesText(n);
      val.title = n < 0
        ? "无限循环：一直反复，按 F8 / 停止运行才停"
        : "含第一遍：×3 = 这一段一共打 3 遍（点 ∞ 可无限）";
      if (n < 0) val.classList.add("inf");
      box.appendChild(mk("－", n < 0 ? "从无限回到 2 遍" : "少打一遍", -1));
      box.appendChild(val);
      box.appendChild(mk("＋", n < 0 ? "从无限回到 2 遍" : "多打一遍", 1));
      // ∞ 按钮：一点就在「无限」和「2 遍」之间切
      const inf = document.createElement("b");
      inf.textContent = "∞";
      inf.title = n < 0 ? "点一下取消无限（回到 2 遍）" : "点一下改成无限循环";
      inf.className = "rep-inf" + (n < 0 ? " on" : "");
      inf.onclick = (e) => {
        e.stopPropagation();
        setEndTimes(i, n < 0 ? 2 : -1);
      };
      box.appendChild(inf);
      row.appendChild(box);

      const sp = document.createElement("span");
      sp.className = "rep-span";
      sp.textContent = "↑ 跳回起点（第 " + (depth + 1) + " 层）" + (it.size ? "，这段 " + it.size + " 步" : "");
      row.appendChild(sp);
    }
  } else {
    const hint = document.createElement("span");
    hint.className = "rep-span";
    hint.textContent = "点我 → 再点另一个，配成一对";
    row.appendChild(hint);
    const up = document.createElement("b");
    up.className = "rep-quick";
    up.textContent = "↑ 配上方";
    up.onclick = (e) => { e.stopPropagation(); pairNearest(i, -1); };
    const dn = document.createElement("b");
    dn.className = "rep-quick";
    dn.textContent = "↓ 配下方";
    dn.onclick = (e) => { e.stopPropagation(); pairNearest(i, 1); };
    row.appendChild(up);
    row.appendChild(dn);
  }

  const x = document.createElement("span");
  x.className = "rep-x";
  x.textContent = "×";
  x.title = isStart && (it.ends || 0) > 1 ? "删除整个反复（含所有终点）" : "删除（成对删除）";
  x.onclick = (e) => {
    e.stopPropagation();
    snapshot();
    const idxs = repDeleteSet(i).slice().sort((a, b) => b - a);
    idxs.forEach((k) => state.steps.splice(k, 1));
    repPend = null;
    state.sel = null;
    state.selSet = [];
    renderSeq();
    persistSoon();
    toast(idxs.length > 2 ? "已删除整组反复（" + idxs.length + " 个记号）"
                          : (idxs.length === 2 ? "已删除这对反复记号" : "已删除反复记号"));
  };
  row.appendChild(x);

  row.onclick = (e) => {
    e.stopPropagation();
    if (!linked) {
      if (repPend === s) { repPend = null; renderSeq(); return; }
      if (repPend == null) {
        repPend = s;
        renderSeq();
        toast("再点另一个记号：点一个「起点」就能挂在它下面，点一个未配对的就新建一对");
        return;
      }
      const prev = repPend;
      const pi = state.steps.indexOf(prev);
      if (pi < 0 || !prev.rep) { repPend = s; renderSeq(); return; }
      linkReps(pi, i);
      return;
    }
    // 已配对的记号：如果手上正拿着一个未配对的，就连过来（起点/终点都行）
    if (repPend === s) { repPend = null; renderSeq(); return; }
    if (repPend) {
      const pi = state.steps.indexOf(repPend);
      if (pi >= 0 && repPend.rep && !repPend.link) { linkReps(i, pi); return; }
      repPend = null;
    }
    state.sel = i;
    state.selSet = [i];
    state.caret = i + 1;
    renderSeq();
  };

  if (linked) {
    const pairIdx = isStart ? null : it.partner;
    row.onmouseenter = () => {
      let a = i, b = pairIdx;
      if (isStart) {
        const mine = repPairs().filter((p) => p.s === i);
        if (!mine.length) return;
        b = Math.max.apply(null, mine.map((p) => p.e));   // 起点：点亮到最远的终点
      }
      const lo = Math.min(a, b), hi = Math.max(a, b);
      document.querySelectorAll("#seq [data-i]").forEach((el) => {
        const k = Number(el.dataset.i);
        if (k >= lo && k <= hi) el.classList.add("rep-hi");
      });
    };
    row.onmouseleave = () => {
      document.querySelectorAll(".rep-hi").forEach((el) => el.classList.remove("rep-hi"));
    };
  }

  row.ondblclick = (e) => {
    e.stopPropagation();
    if (!linked) return;
    if (isStart) { toast("次数在每个终点上改（追着终点那行点 －/＋）"); return; }
    const cur = repTimes(s.times);
    const v = prompt("这一段打几遍？（含第一遍，3 = 共打 3 遍；填 -1 或 ∞ = 无限循环）",
                     cur < 0 ? "-1" : String(cur));
    if (v === null) return;
    const txt = String(v).trim().toLowerCase();
    if (txt === "∞" || txt === "inf" || txt === "无限") { setEndTimes(i, -1); return; }
    const num = parseInt(txt, 10);
    if (isNaN(num) || num === 0) return toast("次数填 1 以上，或 -1 表示无限");
    setEndTimes(i, num);
  };

  return row;
}

// 次数记在终点上（一段一个数）
function setEndTimes(i, times) {
  const s = state.steps[i];
  if (!s || !s.rep) return;
  const n = repTimes(times);
  const st = repGroupStart(s.link);
  snapshot();
  s.times = n;
  if (st >= 0) state.steps[st].times = n;   // 起点同步，新增终点时沿用
  renderSeq();
  persistSoon();
  if (n < 0) toast("这一段改成无限循环（F8 才停）");
  else toast("这一段打 " + n + " 遍");
}

function linkReps(i, j) {
  repPend = null;
  const a = state.steps[i], b = state.steps[j];
  if (!a || !b || !a.rep || !b.rep || i === j) { renderSeq(); return; }

  const aFree = !a.link, bFree = !b.link;
  if (!aFree && !bFree) {
    renderSeq();
    toast("这两个都已经配过对了");
    return;
  }

  let gid = "";
  let sIdx = -1, eIdx = -1;

  if (aFree && bFree) {
    // 全新的两组：上边的当起点、下边的当终点
    sIdx = Math.min(i, j);
    eIdx = Math.max(i, j);
    gid = repId();
  } else {
    // 已有那一组：给它再挂一个终点
    const fixed = aFree ? b : a;
    const freeIdx = aFree ? i : j;
    gid = fixed.link;
    const st = repGroupStart(gid);
    if (st < 0) { renderSeq(); toast("这一组的起点不见了，先把它们删掉重配"); return; }
    if (freeIdx <= st) {
      renderSeq();
      toast("终点要放在起点下面（第 " + (st + 1) + " 行之后）");
      return;
    }
    sIdx = st;
    eIdx = freeIdx;
  }

  const inside = state.steps.slice(sIdx + 1, eIdx)
    .filter((o) => o && !o.divider && !o.br && !o.rep).length;
  if (!inside) {
    renderSeq();
    toast("这两个记号之间没有技能，配了也不会反复");
    return;
  }
  if (repCross(sIdx, eIdx, aFree && bFree ? "" : gid)) {
    renderSeq();
    toast("这一段会和已有的反复交叉，换个起点/终点吧");
    return;
  }

  snapshot();
  const prevTimes = repTimes(state.steps[sIdx].times);
  const other = repGroupEnds(gid).map((k) => repTimes(state.steps[k].times));
  const times = other.length ? other[0] : prevTimes;

  state.steps[sIdx].link = gid;
  state.steps[sIdx].end = false;
  state.steps[sIdx].times = times;
  state.steps[eIdx].link = gid;
  state.steps[eIdx].end = true;
  state.steps[eIdx].times = times;

  renderSeq();
  persistSoon();

  const isNew = (other.length === 0);
  const v = prompt((isNew ? "这一段打几遍？" : "新挂的这个终点打几遍？") +
                   "（含第一遍，3 = 共打 3 遍；填 -1 或 ∞ = 无限循环）",
                   times < 0 ? "-1" : String(times));
  const txt = String(v == null ? "" : v).trim().toLowerCase();
  if (txt === "∞" || txt === "inf" || txt === "无限") setEndTimes(eIdx, -1);
  else {
    const num = parseInt(txt, 10);
    if (!isNaN(num) && num !== 0) setEndTimes(eIdx, num);
    else toast(isNew ? "已配对：这一段" + repTimesWords(times)
                     : "已挂上第 " + (other.length + 1) + " 个终点（" + repTimesWords(times) + "）");
  }
}

// 和上下最近的未配对记号快速配对
function pairNearest(i, dir) {
  for (let k = i + dir; k >= 0 && k < state.steps.length; k += dir) {
    const s = state.steps[k];
    if (s && s.rep && s.link) break;          // 中间夹着已配对的，不跨过去
    if (s && s.rep && !s.link) { linkReps(i, k); return; }
  }
  toast(dir < 0 ? "上方没有可配对的记号了" : "下方没有可配对的记号了");
}

function addRepeat() {
  const at = state.caret == null ? state.steps.length : state.caret;
  snapshot();
  state.steps.splice(at, 0, { rep: 1, link: "", end: false, times: 2 });
  state.caret = at + 1;
  state.sel = at;
  state.selSet = [at];
  renderSeq();
  persistSoon();
  toast("已插入反复记号：再插一个，然后点它 → 点另一个，就配成一对");
}

/* ---------------- 序列渲染 ---------------- */
function renderSeq() {
  const box = $("#seq");
  box.innerHTML = "";
  // 轴被清空 = 已经在做新的了，保存时不再问“覆盖”
  if (!state.steps.length) state.loadedTeam = null;

  // 光标层（每次重建，避免被 innerHTML 清掉）
  const line = document.createElement("div");
  line.className = "caret-line";
  line.id = "caretLine";
  box.appendChild(line);

  let n = 0;
  const repInfo = repScan();
  state.steps.forEach((s, i) => {
    if (s.rep) {
      box.appendChild(makeRepRow(s, i, repInfo));
      return;
    }
    if (s.br) {
      const b = document.createElement("div");
      b.className = "break" + (state.sel === i ? " sel" : "");
      b.dataset.i = i;
      b.title = "换行（按这个位置点击，光标会落到下一行）";
      b.onclick = (e) => {
        e.stopPropagation();
        state.sel = i;
        state.selSet = [i];
        state.caret = i + 1;      // 光标落到换行后面
        renderSeq();
      };
      const lab = document.createElement("span");
      lab.className = "brlabel";
      lab.textContent = "↵ 换行";
      b.appendChild(lab);
      const x = document.createElement("span");
      x.className = "brdel";
      x.textContent = "×";
      x.title = "删除这个换行";
      x.onclick = (e) => {
        e.stopPropagation();
        deleteAt(i);
      };
      b.appendChild(x);
      box.appendChild(b);
      return;
    }
    if (s.note && !s.key && !s.heavy && !s.divider) {
      const nt = document.createElement("div");
      nt.className = "note-chip";
      nt.dataset.i = i;
      nt.textContent = s.note;
      nt.onclick = (e) => { e.stopPropagation(); toggleSel(i, e.ctrlKey || e.metaKey); renderSeq(); };
      nt.ondblclick = (e) => {
        e.stopPropagation();
        const txt = prompt("修改注释：", s.note);
        if (txt !== null) {
          snapshot();
          s.note = txt.trim();
          renderSeq();
          persistSoon();
        }
      };
      box.appendChild(nt);
      return;
    }
    if (s.heavy) {
      const wrap = document.createElement("div");
      wrap.className = "cell-col";
      if (s.note) {
        const nt = document.createElement("div");
        nt.className = "note-top";
        nt.textContent = s.note;
        nt.title = s.note;
        nt.onclick = (e) => { e.stopPropagation(); toggleSel(i, e.ctrlKey || e.metaKey); renderSeq(); };
        nt.ondblclick = (e) => {
          e.stopPropagation();
          const txt = prompt("修改注释：", s.note);
          if (txt !== null) {
            snapshot();
            s.note = txt.trim();
            renderSeq();
            persistSoon();
          }
        };
        wrap.appendChild(nt);
      }
      const h = document.createElement("div");
      const inSelH = (state.selSet || []).includes(i) || state.sel === i;
      h.className = "cell heavy" + (inSelH ? " sel" : "");
      h.dataset.i = i;
      h.innerHTML = `<img src="/img/heavy" alt=""><span class="n">重击</span>`;
      h.onclick = (e) => {
        e.stopPropagation();
        toggleSel(i, e.ctrlKey || e.metaKey);
        state.caret = i + 1;
        renderSeq();
      };
      // 整块（包括上方注释区域）都能点选，避免点偏了选不中
      wrap.onclick = (e) => {
        if (e.target !== wrap) return;
        e.stopPropagation();
        toggleSel(i, e.ctrlKey || e.metaKey);
        state.caret = i + 1;
        renderSeq();
      };
      wrap.appendChild(h);
      box.appendChild(wrap);
      return;
    }
    if (s.divider) {
      const d = document.createElement("div");
      d.className = "divider";
      d.dataset.i = i;
      d.innerHTML = `<span class="bar">∥</span><span>${esc((s.name || "分段").slice(0, 6))}</span>`;
      d.onclick = (e) => { e.stopPropagation(); toggleSel(i, e.ctrlKey || e.metaKey); renderSeq(); };
      d.ondblclick = (e) => {
        e.stopPropagation();
        const name = prompt("分段名称：", s.name || "");
        if (name !== null) {
          snapshot();
          s.name = name.trim();
          renderSeq();
          persistSoon();
        }
      };
      box.appendChild(d);
      return;
    }

    n += 1;
    const kind = s.hold ? "ult" : (String(s.key).toUpperCase() === "E" ? "link" : "skill");
    const name = state.team[(s.op || 1) - 1] || s.who || "";
    const c = document.createElement("div");
    const inSel = (state.selSet || []).includes(i) || state.sel === i;
    c.className = "cell" + (inSel ? " sel" : "");

    // 每个技能外面套一层竖排容器：注释在上，图标在下
    const wrap = document.createElement("div");
    wrap.className = "cell-col";
    if (s.note) {
      const nt = document.createElement("div");
      nt.className = "note-top";
      nt.textContent = s.note;
      nt.title = s.note;
      nt.onclick = (e) => { e.stopPropagation(); toggleSel(i, e.ctrlKey || e.metaKey); renderSeq(); };
      nt.ondblclick = (e) => {
        e.stopPropagation();
        const txt = prompt("修改注释：", s.note);
        if (txt !== null) {
          snapshot();
          s.note = txt.trim();
          renderSeq();
          persistSoon();
        }
      };
      wrap.appendChild(nt);
    }
    wrap.appendChild(c);
    c.dataset.i = i;

    if (hasIcon(name, kind)) {
      c.innerHTML = `<img src="${iconUrl(name, kind, s.op || 1)}" alt=""><span class="n">${n}</span>`;
    } else {
      // 没有素材 → 空白占位
      c.classList.add("blank");
      c.innerHTML = `<span class="icon-empty"></span><span class="n">${n}</span>`;
    }

    c.onclick = (e) => {
      e.stopPropagation();
      const additive = e.ctrlKey || e.metaKey;
      if (additive) {
        toggleSel(i, true);
        state.caret = i;
      } else if (state.sel === i && !(state.selSet || []).length) {
        state.sel = null;
        state.caret = null;
        state.selSet = [];
      } else {
        toggleSel(i, false);
        state.caret = i + 1;     // 光标落在它后面，Backspace 先删它
      }
      renderSeq();
    };
    // 双击不做任何事（避免误删技能）
    c.ondblclick = (e) => { e.stopPropagation(); };
    // 拖动排序：自己处理鼠标（不用浏览器原生拖拽，避免“搜索文本”弹条）
    c.draggable = false;
    c.onmousedown = (e) => {
      if (e.button !== 0) return;
      beginDrag(i, e);
    };
    box.appendChild(wrap);
  });

  $("#seqHint").textContent = `${state.steps.filter((s) => !s.br && !s.divider).length} 步`;
  requestAnimationFrame(positionCaret);
}

/* ---------------- 拖动排序（自定义，不用原生 HTML5 拖拽） ---------------- */
let _dragFrom = null, _dragging = false, _dragStartXY = null, _dropTarget = null;

let _ghostEl = null;



/* ---------------- 拖动排序（鼠标事件，边界吸附） ---------------- */
let _drag = null;

function beginDrag(i, e) {
  const cell = document.querySelector(`.cell[data-i="${i}"]`);
  let fx = 0.5, fy = 0.5;
  if (cell) {
    const r = cell.getBoundingClientRect();
    fx = Math.min(1, Math.max(0, (e.clientX - r.left) / r.width));
    fy = Math.min(1, Math.max(0, (e.clientY - r.top) / r.height));
  }
  _drag = { i, x0: e.clientX, y0: e.clientY, active: false, ghost: null, target: null, fx, fy };
  window.addEventListener("mousemove", onDragMove);
  window.addEventListener("mouseup", onDragUp, { once: true });
}

function onDragMove(e) {
  const d = _drag;
  if (!d) return;

  if (!d.active) {
    if (Math.abs(e.clientX - d.x0) + Math.abs(e.clientY - d.y0) < 6) return;
    d.active = true;
    document.body.classList.add("dragging-seq");
    const cell = document.querySelector(`.cell[data-i="${d.i}"]`);
    if (cell) {
      cell.classList.add("src-dragging");
      const g = cell.cloneNode(true);
      g.classList.add("drag-ghost");
      g.classList.remove("sel");
      document.body.appendChild(g);
      d.ghost = g;
    }
  }

  if (d.ghost) {
    const w = d.ghost.offsetWidth || 58;
    const h = d.ghost.offsetHeight || 74;
    d.ghost.style.left = (e.clientX - d.fx * w) + "px";
    d.ghost.style.top = (e.clientY - d.fy * h) + "px";
  }

  const hit = dropTargetAt(e.clientX, e.clientY);
  d.target = hit ? hit.i : null;
}

function onDragUp(e) {
  window.removeEventListener("mousemove", onDragMove);
  const d = _drag;
  _drag = null;
  document.body.classList.remove("dragging-seq");
  document.querySelectorAll(".src-dragging").forEach((x) => x.classList.remove("src-dragging"));
  if (d && d.ghost) d.ghost.remove();

  if (!d || !d.active || d.target == null) return;
  const from = d.i;
  let to = d.target;
  if (to === from || to === from + 1) return;
  snapshot();
  const item = state.steps.splice(from, 1)[0];
  if (from < to) to -= 1;
  state.steps.splice(to, 0, item);
  state.sel = to;
  state.caret = to;
  renderSeq();
  persistSoon();
}

function dropTargetAt(x, y) {
  const cells = cellList();
  if (!cells.length) return null;

  // 先找最近的一行
  let rowY = cells[0].r.top + cells[0].r.height / 2;
  let best = 1e9;
  cells.forEach((c) => {
    const cy = c.r.top + c.r.height / 2;
    const dist = Math.abs(y - cy);
    if (dist < best) { best = dist; rowY = cy; }
  });
  const row = cells.filter((c) => Math.abs(c.r.top + c.r.height / 2 - rowY) < 40);
  if (!row.length) return null;

  // 边界：每个 item 的左边（插到它前面）和右边（插到它后面）
  const bounds = [];
  row.forEach((c) => {
    bounds.push({ x: c.r.left, i: c.i, top: c.r.top, h: c.r.height });
    bounds.push({ x: c.r.right, i: c.i + 1, top: c.r.top, h: c.r.height });
  });

  let pick = bounds[0];
  bounds.forEach((b) => { if (Math.abs(x - b.x) < Math.abs(x - pick.x)) pick = b; });
  return pick;
}





/* ---------------- 光标 ---------------- */

function cellList() {
  // 技能格、分段、换行都算「可插入锚点」
  return [...$("#seq").querySelectorAll(".cell, .divider, .break")].map((c) => ({
    i: Number(c.dataset.i),
    r: c.getBoundingClientRect(),
    el: c,
  })).sort((a, b) => a.i - b.i);
}

// 把光标画在“第 caret 个位置”的边界上
function positionCaret() {
  const line = $("#caretLine");
  if (!line) return;
  if (state.caret == null) { line.style.display = "none"; return; }

  const cells = cellList();
  if (!cells.length) { line.style.display = "none"; return; }

  const seqRect = $("#seq").getBoundingClientRect();
  let anchor = null, rightSide = false;
  for (const c of cells) {
    if (c.i === state.caret) { anchor = c; rightSide = false; break; }
    if (c.i === state.caret - 1) { anchor = c; rightSide = true; }
  }
  if (!anchor) { anchor = cells[cells.length - 1]; rightSide = true; }

  const r = anchor.r;
  const isBreak = anchor.el && anchor.el.classList.contains("break");
  line.style.display = "block";
  if (isBreak) {
    // 换行后面：光标直接画到下一行行首
    line.style.left = "0px";
    line.style.top = (r.bottom - seqRect.top + 2) + "px";
    line.style.height = "28px";
    return;
  }
  const x = (rightSide ? r.right : r.left) - seqRect.left;
  line.style.left = (x - 1) + "px";
  line.style.top = (r.top - seqRect.top + 6) + "px";
  line.style.height = Math.max(24, r.height - 18) + "px";
}

// 按坐标算最近的插入边界（与拖动落点同一套算法）
function caretFromPoint(clientX, clientY) {
  const hit = dropTargetAt(clientX, clientY);
  state.caret = hit ? hit.i : state.steps.length;
  state.sel = null;
}

function positionOfCaret() {
  const cells = cellList();
  if (!cells.length) return { x: 0, y: 0 };
  let anchor = null, rightSide = false;
  for (const c of cells) {
    if (c.i === state.caret) { anchor = c; rightSide = false; break; }
    if (c.i === state.caret - 1) { anchor = c; rightSide = true; }
  }
  if (!anchor) { anchor = cells[cells.length - 1]; rightSide = true; }
  const r = anchor.r;
  return { x: rightSide ? r.right : r.left, y: r.top + r.height / 2 };   // 视口坐标
}

function moveCaret(dx, dy) {
  if (state.caret == null) state.caret = state.steps.length;

  if (dx) {
    state.caret = Math.max(0, Math.min(state.steps.length, state.caret + dx));
    positionCaret();
    return;
  }

  const cells = cellList();
  if (!cells.length) return;
  const cur = positionOfCaret();
  const below = dy > 0;

  // 按 top 分行
  const rows = [];
  cells.forEach((c) => {
    const key = Math.round(c.r.top / 10);
    let row = rows.find((r) => r.key === key);
    if (!row) { row = { key, top: c.r.top, cells: [] }; rows.push(row); }
    row.cells.push(c);
  });
  rows.sort((a, b) => a.top - b.top);

  // 找当前行
  let curRow = 0, bestD = 1e9;
  rows.forEach((r, idx) => {
    const d = Math.abs(r.top + 30 - cur.y);
    if (d < bestD) { bestD = d; curRow = idx; }
  });

  const targetRow = rows[curRow + (below ? 1 : -1)];
  if (!targetRow) return;                       // 已经是最上/最下一行

  // 目标行里，离当前 x 最近的格子
  let pick = targetRow.cells[0], pd = 1e9;
  targetRow.cells.forEach((c) => {
    const d = Math.abs((c.r.left + c.r.right) / 2 - cur.x);
    if (d < pd) { pd = d; pick = c; }
  });
  state.caret = pick.i + (cur.x > (pick.r.left + pick.r.right) / 2 ? 1 : 0);
  positionCaret();
}

/* ---------------- 复制 / 剪切 / 粘贴 ---------------- */
let clip = [];

function copySel() {
  const list = selIdx();
  if (!list.length) return toast("先选中技能");
  const inSel = new Set(list);
  clip = list.map((i) => {
    const o = JSON.parse(JSON.stringify(state.steps[i]));
    // 反复记号只复制了这一组的一部分 → 降级成未配对，粘出来不会串到原来的配对上
    if (o && o.rep && o.link) {
      const g = repGroupIdxs(o.link);
      if (!g.length || !g.every((k) => inSel.has(k))) { o.link = ""; o.end = false; }
    }
    return o;
  });
  toast("已复制 " + list.length + " 项");
}

function cutSel() {
  const list = selIdx();
  if (!list.length) return toast("先选中技能");
  snapshot();
  clip = list.map((i) => JSON.parse(JSON.stringify(state.steps[i])));
  const set = new Set(list);
  list.forEach((i) => { repDeleteSet(i).forEach((k) => set.add(k)); });
  const all = [...set].sort((a, b) => a - b);
  for (let k = all.length - 1; k >= 0; k--) state.steps.splice(all[k], 1);
  state.sel = null;
  state.selSet = [];
  renderSeq();
  persistSoon();
  toast("已剪切 " + list.length + " 项");
}

function pasteSel() {
  if (!clip.length) return toast("剪贴板是空的");
  snapshot();

  // 优先插到光标处；没有光标就插在选中项后面；都没有就放末尾
  let at;
  if (state.caret != null) {
    at = state.caret;
  } else if (state.sel != null) {
    at = state.sel + 1;
  } else {
    at = state.steps.length;
  }
  at = Math.max(0, Math.min(state.steps.length, at));

  const items = JSON.parse(JSON.stringify(clip));
  // 成对的反复记号要换新 id，否则会和被复制的那一对搅在一起
  const idMap = {};
  items.forEach((o) => {
    if (o && o.rep && o.link) {
      if (!idMap[o.link]) idMap[o.link] = repId();
      o.link = idMap[o.link];
    }
  });
  state.steps.splice(at, 0, ...items);
  state.sel = at;
  state.caret = at + items.length;   // 光标跟到粘贴内容后面
  renderSeq();
  persistSoon();
  toast(`已粘贴 ${items.length} 个`);
}

function renderComps() {
  const box = $("#comps");
  if (!box) return;
  box.innerHTML = "";
  $("#compCount").textContent = `${state.comps.length} 套`;
  if (!state.comps.length) {
    box.innerHTML = `<span class="sub">还没有保存的编队</span>`;
    return;
  }
  state.comps.forEach((c, i) => {
    const d = document.createElement("div");
    d.className = "lib-item";
    const names = (c.team || []).filter(Boolean).join(" / ") || "空编队";
    d.innerHTML = `<b>${esc(c.name || "编队")}</b><span>${esc(names)}</span><span class="del" title="删除">×</span>`;
    d.onclick = (e) => {
      if (e.target.classList.contains("del")) {
        if (state.compFilter === JSON.stringify(c.team)) state.compFilter = null;
        state.comps.splice(i, 1);
        renderComps(); renderLib(); persistSoon();
        return;
      }
      state.team = (c.team || ["", "", "", ""]).slice(0, 4);
      state.compFilter = JSON.stringify(state.team);
      renderSlots(); renderSeq(); renderComps(); renderLib();
      toast(`已载入编队：${c.name}`);
      persistSoon();
    };
    box.appendChild(d);
  });
}

/* ---------------- 轴库备份 / 恢复（换电脑用） ---------------- */
// 轴库右上「示例」：需要教学轴时手动载入（新装默认不带，不污染你的轴）
async function loadDemoAxes() {
  const r = await api.post("/api/demo", {});
  if (r.error) return toast("载入示例失败：" + r.error);
  const axes = r.axes || [];
  let added = 0, skipped = 0;
  axes.forEach((ax) => {
    if (!ax || !Array.isArray(ax.steps)) return;
    const sig = axisSig(ax.team, ax.steps);
    if (state.teams.some((x) => axisSig(x.team, x.steps) === sig)) { skipped++; return; }
    let name = String(ax.name || "示例").slice(0, 40);
    let k = 2;
    while (state.teams.some((x) => x.name === name)) name = `${String(ax.name).slice(0, 36)} (${k++})`;
    state.teams.push({
      name,
      team: (ax.team || ["", "", "", ""]).slice(0, 4),
      steps: JSON.parse(JSON.stringify(ax.steps)),
      holdMs: ax.holdMs || state.holdMs,
    });
    added++;
  });
  const compName = ensureComp(axes[0] && axes[0].team);
  renderComps();
  renderLib();
  persistSoon();
  if (!added) return toast(skipped ? "示例轴已经在轴库里了" : "没有可载入的示例");
  toast(`已载入 ${added} 条示例轴${compName ? `，编队「${compName}」` : ""}；点条目就能看`);
}

function libBackup() {
  const data = {
    app: "EndfieldRotPilot",
    kind: "axis-library",
    format: 1,
    exportedAt: new Date().toISOString(),
    teams: JSON.parse(JSON.stringify(state.teams || [])),
    comps: JSON.parse(JSON.stringify(state.comps || [])),
  };
  try {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `RotPilot轴库_${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast(`已备份 ${data.teams.length} 条轴、${data.comps.length} 套编队`);
  } catch (e) {
    toast("备份失败：" + e.message);
  }
}

async function libRestoreFile(ev) {
  const f = ev.target.files && ev.target.files[0];
  ev.target.value = "";                       // 同一个文件能再选一次
  if (!f) return;
  try {
    const d = JSON.parse(await f.text());
    const teams = Array.isArray(d.teams) ? d.teams : (Array.isArray(d) ? d : null);
    if (!teams) return toast("这个文件里没有轴库数据");
    const comps = Array.isArray(d.comps) ? d.comps : [];
    // 合并：同名的加序号，绝不覆盖你现有的轴
    let added = 0;
    let skipped = 0;
    teams.forEach((t) => {
      if (!t || !Array.isArray(t.steps)) return;
      const team = (t.team || ["", "", "", ""]).slice(0, 4);
      const sig = axisSig(team, t.steps);
      // 库里已经有内容一模一样的：跳过，别每次恢复都多一份
      if (state.teams.some((x) => axisSig(x.team, x.steps) === sig)) { skipped++; return; }
      const base = String(t.name || "未命名").slice(0, 40);
      let name = base;
      let k = 2;
      while (state.teams.some((x) => x.name === name)) name = `${base.slice(0, 36)} (${k++})`;
      state.teams.push({
        name,
        team,
        steps: JSON.parse(JSON.stringify(t.steps)),   // 深拷贝，别和文件内容共用一个数组
        holdMs: t.holdMs || state.holdMs,
      });
      added++;
    });
    comps.forEach((c) => {
      if (!c || !Array.isArray(c.team)) return;
      const key = JSON.stringify(c.team.slice(0, 4));
      if (!(state.comps || []).some((x) => JSON.stringify((x.team || []).slice(0, 4)) === key)) {
        state.comps.push({ name: String(c.name || "编队").slice(0, 40), team: c.team.slice(0, 4) });
      }
    });
    renderLib();
    renderComps();
    persistSoon();
    toast(added ? `恢复了 ${added} 条轴` + (skipped ? `，${skipped} 条已经有了` : "")
                : (skipped ? "这些轴库里都有了，没有重复添加" : "没有找到可恢复的轴"));
  } catch (e) {
    toast("文件读不出来：" + e.message);
  }
}

function renderLib() {
  const box = $("#lib");
  box.innerHTML = "";
  let shown = state.compFilter
    ? state.teams.filter((t) => JSON.stringify((t.team || ["", "", "", ""]).slice(0, 4)) === state.compFilter)
    : state.teams;
  // 搜索：轴名或编队里的干员名，大小写都算
  const q = (state.libQuery || "").trim().toLowerCase();
  if (q) {
    shown = shown.filter((t) =>
      String(t.name || "").toLowerCase().includes(q) ||
      (t.team || []).some((n) => String(n || "").toLowerCase().includes(q)));
  }
  $("#libCount").textContent = `${shown.length} 套`;
  if (!shown.length) {
    const tip = q ? "没找到匹配的轴"
      : (state.compFilter ? "这套编队还没有保存的轴" : "还没有保存的轴");
    box.innerHTML = `<span class="sub">${tip}</span>`;
    return;
  }
  shown.forEach((t) => {
    const i = state.teams.indexOf(t);
    const d = document.createElement("div");
    d.className = "lib-item";
    d.innerHTML = `<b>${esc(t.name || "未命名")}</b><span>${(t.steps || []).length} 步</span>
      <span class="del" title="删除">×</span>`;
    d.onclick = (e) => {
      if (e.target.classList.contains("del")) {
        if (state.loadedTeam === state.teams[i].name) state.loadedTeam = null;
        state.teams.splice(i, 1);
        renderLib();
        persistSoon();
        return;
      }
      snapshot();
      state.loadedTeam = t.name || null;
      state.team = (t.team || ["", "", "", ""]).slice(0, 4);
      state.steps = JSON.parse(JSON.stringify(t.steps || []));
      state.sel = null;
      state.caret = null;
      renderSlots();
      renderSeq();
      toast(`已载入：${t.name}`);
      persistSoon();
    };
    // 双击重命名
    d.ondblclick = (e) => {
      e.stopPropagation();
      const oldName = t.name || "未命名";
      const name = prompt("重命名这个编队：", oldName);
      if (name === null) return;
      const finalName = name.trim();
      if (!finalName) return toast("名字不能为空");
      if (finalName === oldName) return;
      if (state.teams.some((x, k) => k !== i && x.name === finalName)) {
        return toast("已经有同名编队了");
      }
      t.name = finalName;
      if (state.loadedTeam === oldName) state.loadedTeam = finalName;
      renderLib();
      persistSoon();
      toast(`已改名为：${finalName}`);
    };
    box.appendChild(d);
  });
}

/* ---------------- 保存 / 分享 / 运行 ---------------- */
let saveTimer = null;
function persistSoon() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(persist, 350);
}
const persist = () => api.post("/api/state", {
  team: state.team, steps: state.steps, teams: state.teams, holdMs: state.holdMs,
  opacity: state.opacity, iconSize: state.iconSize,
  uiScale: state.uiScale || 1, nextPreview: state.nextPreview === false ? 0 : 1,
  autoExit: state.autoExit ? 1 : 0,
  keys: state.keys,
  screenMode: state.screenMode || "game",
  loopCount: state.loopCount || 0, comps: state.comps || [],
  loopFrom: state.loopFrom || 0, loopTo: state.loopTo || 0,
  displayMode: state.displayMode || "kb",
  inputMode: state.inputMode, pad: state.pad, overlayStyle: state.overlayStyle,
  padType: state.padType,
});

let shareTimer = null;

// 名字直接取“当前轴的名字”，弹窗里还能改；改完自动重新生成
function shareDefaultName() {
  return (state.loadedTeam || `轴 ${new Date().toLocaleDateString().replace(/\//g, "-")}`).slice(0, 40);
}

// 导出格式：RP2 兼容 / RP3 压缩
const SHARE_MODE_HINT = {
  rp2: "RP2（兼容）：连注释一起发，作者原版和所有老版本都能导入，" +
       "只是它们看不到轴名；新版本能把轴名和注释都还原。",
  rp3: "RP3（压缩）：内容全在（注释也在），码短一截；" +
       "只有新版本（1.3.3 及以后）能读，老版本会报“版本不支持”。",
};
const SHARE_MODE_LABEL = { rp2: "RP2 兼容", rp3: "RP3 压缩" };

function shareMode() {
  const el = $("#shareMode");
  return (el && el.value) || "rp2";
}

function shareParams() {
  return { fmt: shareMode() };
}

function refreshShareHint() {
  const el = $("#shareVerHint");
  if (el) el.textContent = SHARE_MODE_HINT[shareMode()] || "";
}

async function buildShareCode() {
  let axisName = ($("#shareName").value || "").trim().slice(0, 40);
  // 名字留空就自动补一个：以前留空导出的码没有轴名，对方导入后不会存进轴库
  if (!axisName) {
    axisName = autoAxisName(state.team);
    if ($("#shareName")) $("#shareName").value = axisName;
  }
  const p = shareParams();
  const r = await api.post("/api/share",
    { team: state.team, steps: state.steps, axisName, holdMs: state.holdMs, ...p });
  if (r.error) { toast("生成失败：" + r.error); return; }
  $("#shareText").value = r.code;
  const steps = state.steps.filter((s) => !s.br && !s.divider && !s.rep).length;
  $("#shareLen").textContent =
    `${r.code.length} 字符 · ${steps} 步 · ${SHARE_MODE_LABEL[shareMode()] || ""} · 名字：${axisName}`;
  // 编队里有人不在干员名单里：导出的码会把这位写成空位，必须提醒
  if (r.warn) {
    $("#shareLen").textContent += `　⚠ ${r.warn}`;
    toast(r.warn, 7000);
  }
}

// 导出 JSON：不经过分享码，原样存一份，随时能看能改（也方便贴给别人看内容）
function exportJson() {
  const name = ($("#shareName").value || "").trim().slice(0, 40) || "未命名轴";
  const data = {
    app: "EndfieldRotPilot",
    format: "rotpilot-axis/1",
    axisName: name,
    exportedAt: new Date().toISOString(),
    holdMs: state.holdMs,
    team: [...state.team],
    steps: JSON.parse(JSON.stringify(state.steps)),
  };
  try {
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = name.replace(/[\\/:*?"<>|]/g, "_") + ".json";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast("已导出 JSON 文件");
  } catch (e) {
    toast("导出失败：" + e.message);
  }
}

async function openShare() {
  if (!state.steps.length) return toast("先排一条轴");
  $("#shareName").value = state.loadedTeam || shareDefaultName();
  refreshShareHint();
  await buildShareCode();
  $("#shareModal").classList.remove("hidden");
}

let _parsedImport = null;      // 解析过的分享码（点过预览就缓存，点导入直接用）

async function parseImportCode() {
  const code = $("#importText").value.trim();
  if (!code) { toast("先粘贴分享码"); return null; }
  const r = await api.post("/api/import", { code });
  if (r.error) { $("#importMsg").textContent = "解析失败：" + r.error; return null; }
  $("#importMsg").textContent = "";
  _parsedImport = r;
  return r;
}

// 预览：跟编轴时一模一样的格子（同一套 CSS：cell / divider / rep / note-chip）
function renderPreview(steps, team, box) {
  box.innerHTML = "";
  let n = 0;
  steps.forEach((s) => {
    if (s.rep) {
      const row = document.createElement("div");
      row.className = "rep on " + (s.end ? "end" : "start");
      row.style.setProperty("--rc", (typeof REP_COLORS !== "undefined" && REP_COLORS[0]) || "#e0b400");
      row.style.setProperty("--ind", "0px");
      const tag = document.createElement("span");
      tag.className = "rep-tag";
      tag.textContent = s.end ? "↺ 反复终点" : "↻ 反复起点";
      row.appendChild(tag);
      const pill = document.createElement("span");
      pill.className = "rep-pill";
      pill.textContent = s.times === -1 ? "×∞" : "×" + (s.times || 2);
      row.appendChild(pill);
      box.appendChild(row);
      return;
    }
    if (s.br) return;
    if (s.divider) {
      const d = document.createElement("div");
      d.className = "divider";
      d.innerHTML = `<span class="bar">∥</span><span>${esc((s.name || "分段").slice(0, 6))}</span>`;
      box.appendChild(d);
      return;
    }
    if (s.note && !s.key && !s.heavy) {
      const nt = document.createElement("div");
      nt.className = "note-chip";
      nt.textContent = s.note;
      nt.title = s.note;
      box.appendChild(nt);
      return;
    }
    const wrap = document.createElement("div");
    wrap.className = "cell-col";
    if (s.note) {
      const nt = document.createElement("div");
      nt.className = "note-top";
      nt.textContent = s.note;
      nt.title = s.note;
      wrap.appendChild(nt);
    }
    const c = document.createElement("div");
    if (s.heavy) {
      c.className = "cell heavy";
      c.innerHTML = `<img src="/img/heavy" alt=""><span class="n">重击</span>`;
    } else {
      n += 1;
      const op = s.op || 1;
      const name = team[op - 1] || s.who || "";
      const kind = s.hold ? "ult" : (String(s.key || "").toUpperCase() === "E" ? "link" : "skill");
      c.className = "cell";
      c.innerHTML = `<img src="${iconUrl(name, kind, op)}" alt=""><span class="n">${n}</span>`;
      const img = c.querySelector("img");
      if (img) {
        // 本地没这个干员的素材时，别留个破图
        img.onerror = () => {
          img.remove();
          c.classList.add("blank");
          const ph = document.createElement("span");
          ph.className = "icon-empty";
          c.insertBefore(ph, c.firstChild);
        };
      }
    }
    wrap.appendChild(c);
    box.appendChild(wrap);
  });
}

function showImportPreview(r) {
  const steps = r.steps || [];
  const team = (r.team || ["", "", "", ""]).slice(0, 4);
  const nRep = steps.filter((s) => s.rep && !s.end).length;
  const nNote = steps.filter((s) => s.note).length;
  const nDiv = steps.filter((s) => s.divider).length;
  const nSkill = steps.filter((s) => s.key || s.heavy).length;
  const nm = String(r.name || "").trim() || "未命名";
  const who = team.filter(Boolean).join(" / ") || "（没带编队）";
  $("#importPreviewMeta").textContent =
    `编队：${who}　·　「${nm}」　·　${steps.length} 格（技能 ${nSkill}）· ` +
    `${nDiv} 段 · ${nRep ? nRep + " 处反复" : "无反复"} · ${nNote} 条注释`;
  $("#importPreviewWarn").textContent = r.warn || "";
  renderPreview(steps, team, $("#importPreview"));
  $("#importPreviewBox").classList.remove("hidden");
}

async function doPreview() {
  const r = await parseImportCode();
  if (!r) return;
  showImportPreview(r);
}

async function doImport() {
  const r = _parsedImport || await parseImportCode();
  if (!r) return;
  applyImport(r);
}

function closeImport() {
  _parsedImport = null;
  $("#importPreviewBox").classList.add("hidden");
  $("#importPreview").innerHTML = "";
  $("#importText").value = "";
  $("#importMsg").textContent = "";
  $("#importModal").classList.add("hidden");
}

// 一条轴的“指纹”：编队 + 每一步的关键内容，用来判断库里是不是已经有同一条
function axisSig(team, steps) {
  const tm = (team || []).slice(0, 4).map((x) => String(x || ""));
  const st = (steps || []).map((x) => {
    if (x.divider) return "d" + (x.name || "");
    if (x.rep) return `r${x.link || ""}${x.end ? 1 : 0}${x.times}`;
    if (x.br) return "b";
    if (x.heavy) return "h" + (x.note || "");
    if (x.note && !x.key) return "n" + x.note;
    return `${String(x.key || "").toUpperCase()}${x.hold ? "H" : ""}${x.op || 1}` +
           (x.note ? "|" + x.note : "");
  });
  return JSON.stringify([tm, st]);
}

// 码里没带名字时，也得给它一个认得出来的名字（不然导进来就叫“未命名”）
function autoAxisName(team) {
  const who = (team || []).filter(Boolean);
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  const stamp = `${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
  return `${who.length ? who.join("+") : "导入的轴"} · ${stamp}`.slice(0, 40);
}

// 编队库自动登记：以前一套编队要不要存，取决于码里有没有轴名，同一个编队会时有时无
function ensureComp(team) {
  const real = (team || []).slice(0, 4);
  if (!real.filter(Boolean).length) return "";
  const key = JSON.stringify(real);
  if ((state.comps || []).some((c) => JSON.stringify((c.team || []).slice(0, 4)) === key)) return "";
  const base = (real.filter(Boolean).join(" · ") || "空编队").slice(0, 40);
  let name = base;
  let k = 2;
  while (state.comps.some((c) => c.name === name)) name = `${base.slice(0, 36)} (${k++})`;
  state.comps.push({ name, team: real });
  return name;
}

function applyImport(r) {
  const team = (r.team || ["", "", "", ""]).slice(0, 4);
  const steps = r.steps || [];
  if (!steps.length) {                // 空轴不导：导了等于把当前轴清掉，还存不回去
    closeImport();
    return toast("这条码里没有技能格，没有导入（当前轴没动）");
  }
  snapshot();                       // 导入前记一份，Ctrl+Z 能退回原来的轴

  state.team = team;
  state.steps = steps;
  state.sel = null;
  state.caret = null;
  // 长按毫秒跟着这条轴走，不然别人调过 280→180 的轴，你这边还按 280 判
  if (r.holdMs) {
    const h = Math.max(80, Math.min(2000, Number(r.holdMs) || state.holdMs));
    state.holdMs = h;
    if ($("#setHold")) $("#setHold").value = h;
  }

  // —— 关键：导入的轴一律进轴库，不再看码里有没有名字 ——
  // 以前只有带名字的码才入库，不带名字的只是顶掉当前轴；连着导两条，前一条就没了。
  const sig = axisSig(team, state.steps);
  const same = state.teams.find((x) => axisSig(x.team, x.steps) === sig);
  let savedName = "";
  let added = 0;
  if (same) {
    savedName = same.name || "未命名";
  } else {
    const want = (String(r.name || "").trim() || autoAxisName(team)).slice(0, 40);
    let name = want;
    let k = 2;
    while (state.teams.some((x) => x.name === name)) name = `${want.slice(0, 36)} (${k++})`;
    state.teams.push({
      name,
      team: [...team],
      steps: JSON.parse(JSON.stringify(state.steps)),
      holdMs: state.holdMs,
    });
    savedName = name;
    added = 1;
  }
  const compAdded = ensureComp(team);
  state.loadedTeam = savedName;     // 之后按「保存」改的是这一条，不会误覆盖到别的轴

  // 编队筛选会把别的编队的轴藏起来 —— 刚导进来的那条就“看不见”，看着像没存
  state.compFilter = null;

  renderSlots();
  renderComps();
  renderLib();
  renderSeq();
  closeImport();

  const bits = [`已导入 ${state.steps.length} 格`];
  bits.push(added ? `存进轴库「${savedName}」` : `轴库里已有，直接用「${savedName}」`);
  if (compAdded) bits.push(`顺带存了编队「${compAdded}」`);
  if (r.warn) toast(`${r.warn}（${bits.join("，")}）`, 8000);
  else toast(bits.join("，") + "　Ctrl+Z 可撤销", 5000);
  persistSoon();
}

async function runOverlay() {
  if (!state.steps.length) return toast("先排一条轴");
  await persist();
  const r = await api.post("/api/run", { team: state.team, steps: state.steps });
  if (r.warn) toast(r.warn, 6000);
  else toast(r.ok ? "运行中 · 按 F8 结束" : (r.why || "已经在运行了"));
}

/* ---------------- 事件 ---------------- */
$("#shareName").addEventListener("input", () => {
  clearTimeout(shareTimer);
  shareTimer = setTimeout(buildShareCode, 250);
});
$("#shareName").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.stopPropagation(); buildShareCode(); }
});

$("#btnQQ").onclick = () => {
  window.open("https://qm.qq.com/q/WE6wkkVTaY", "_blank");
};

// 作者的共享文档（轴库 / 干员资料）：顶栏、分享弹窗、导入弹窗都能打开
function openDocs() {
  if (!LINKS.docs) return toast("还没填共享文档地址");
  window.open(LINKS.docs, "_blank");
}
if ($("#btnDocs")) $("#btnDocs").onclick = openDocs;

if ($("#shareDocs")) $("#shareDocs").onclick = openDocs;
if ($("#importDocs")) $("#importDocs").onclick = openDocs;

$("#btnBili").onclick = () => {
  if (!LINKS.bili) return toast("还没填 B 站地址");
  window.open(LINKS.bili, "_blank");
};

$("#btnPan").onclick = panMenu;

/* ---------------- 新版本 / 新干员 菜单 + GitHub 更新 ---------------- */
/* ---------------- 新版本 / 新干员 ---------------- */
let updTimer = null;

function updStop() {
  if (updTimer) { clearInterval(updTimer); updTimer = null; }
}

function fmtMB(n) {
  if (!n) return "未知大小";
  return (n / 1048576).toFixed(1) + " MB";
}

function panMenu() {
  let box = document.getElementById("panMenuBox");
  if (box) { box.remove(); return; }
  const btn = document.getElementById("btnPan");
  const r = btn.getBoundingClientRect();
  box = document.createElement("div");
  box.id = "panMenuBox";
  box.style.cssText = "position:fixed;z-index:9999;background:#1e1f22;border:1px solid #444;" +
    "border-radius:8px;box-shadow:0 10px 28px rgba(0,0,0,.45);padding:6px;min-width:200px;" +
    "left:" + Math.max(8, r.left - 80) + "px;top:" + (r.bottom + 6) + "px;";
  const mk = (label, sub, fn) => {
    const b = document.createElement("button");
    b.className = "mini";
    b.style.cssText = "display:block;width:100%;text-align:left;margin:2px 0;padding:8px 10px;" +
      "font:600 12px 'Microsoft YaHei UI';";
    b.innerHTML = `<b>${label}</b><span style="display:block;font-weight:400;color:#9aa0a6;font-size:11px;margin-top:2px">${sub}</span>`;
    b.onclick = () => { box.remove(); fn(); };
    box.appendChild(b);
  };
  mk("GitHub 更新", "检查新版本并一键更新", checkUpdate);
  mk("更新干员", "列出所有干员包，自己选要装哪些", checkOpsPack);
  document.body.appendChild(box);
  setTimeout(() => document.addEventListener("click", function off(e) {
    if (box && !box.contains(e.target) && e.target.id !== "btnPan") { box.remove(); }
    document.removeEventListener("click", off);
  }), 0);
}

async function checkUpdate() {
  openUpdPanel("检查更新中…", "正在连接 GitHub…", []);
  let r;
  try { r = await api.get("/api/update/check"); }
  catch (e) { return openUpdPanel("检查失败", "连不上后台", []); }
  if (!r || !r.ok) {
    return openUpdPanel("检查更新失败", (r && r.error) || "无法访问 GitHub", []);
  }
  const warn = r.warning ? "\n\n（" + r.warning + "）" : "";
  if (!r.newer) {
    return openUpdPanel("已是最新版本",
      `当前 ${r.current}\n最新 ${r.latest}${warn}`, []);
  }
  if (!r.url) {
    return openUpdPanel(`发现新版本 ${r.latest}`,
      `当前：${r.current}　最新：${r.latest}\n\n这个 Release 没带 zip 附件，去页面手动下载。${warn}`,
      [["打开 Release 页面", () => { if (r.page) window.open(r.page, "_blank"); }]]);
  }
  openUpdPanel(`发现新版本 ${r.latest}`,
    `当前：${r.current}　最新：${r.latest}\n大小：${fmtMB(r.size)}${warn}\n\n` +
    (r.notes || "（这版没写更新说明）"),
    [
      ["下载并更新", () => startUpdate(r.url, r.size)],
      ["打开 Release 页面", () => { if (r.page) window.open(r.page, "_blank"); }],
    ]);
}

function openUpdPanel(title, text, actions) {
  updStop();
  let box = document.getElementById("updBox");
  if (box) box.remove();
  box = document.createElement("div");
  box.id = "updBox";
  box.style.cssText = "position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);z-index:9999;" +
    "width:min(660px,92vw);max-height:78vh;background:#1e1f22;color:#e6e6e6;border:1px solid #444;" +
    "border-radius:10px;box-shadow:0 16px 40px rgba(0,0,0,.5);display:flex;flex-direction:column;overflow:hidden;";
  box.innerHTML =
    '<div style="padding:10px 12px;background:#2a2c30;font:700 13px \'Microsoft YaHei UI\';display:flex;align-items:center;gap:8px">' +
      '<span id="updTitle" style="flex:1"></span><button id="updClose" class="mini">关闭</button></div>' +
    '<pre id="updText" style="margin:0;padding:12px;overflow:auto;white-space:pre-wrap;font:12px/1.7 \'Microsoft YaHei UI\';flex:1"></pre>' +
    '<div id="updList" style="display:none;padding:6px 12px;overflow:auto;flex:1;flex-direction:column;gap:4px"></div>' +
    '<div id="updProg" style="display:none;padding:0 12px 8px"><div style="height:8px;background:#333;border-radius:4px;overflow:hidden">' +
      '<div id="updBar" style="height:100%;width:0;background:#ffd400;transition:width .2s"></div></div></div>' +
    '<div id="updBtns" style="padding:10px 12px;display:flex;gap:8px;justify-content:flex-end;border-top:1px solid #333"></div>';
  document.body.appendChild(box);
  box.querySelector("#updTitle").textContent = title;
  box.querySelector("#updText").textContent = text || "";
  box.querySelector("#updClose").onclick = () => { updStop(); box.remove(); };
  const btns = box.querySelector("#updBtns");
  (actions || []).forEach(([label, fn]) => {
    const b = document.createElement("button");
    b.className = "mini";
    b.textContent = label;
    b.onclick = fn;
    btns.appendChild(b);
  });
  return box;
}

async function startUpdate(url, size) {
  if (!url) return toast("这个 Release 没带 zip");
  const box = openUpdPanel("正在下载更新…", "准备下载…", []);
  box.querySelector("#updProg").style.display = "block";
  let r;
  try { r = await api.post("/api/update/download", { url, size }); }
  catch (e) { r = { ok: false, error: String(e) }; }
  if (!r || !r.ok) {
    return openUpdPanel("下载没启动", (r && r.error) || "未知错误",
      [["重试", () => startUpdate(url, size)]]);
  }
  updStop();
  updTimer = setInterval(async () => {
    let p;
    try { p = await api.get("/api/update/progress"); } catch (e) { return; }
    if (!p || typeof p.state !== "string") {
      updStop();
      return openUpdPanel("下载失败",
        "后台没有更新接口。\n\n常见原因：还有旧版本程序在跑（端口被它占着）。\n" +
        "处理：把所有窗口和黑框关掉，重新双击 启动.bat。", []);
    }
    const bar = document.getElementById("updBar");
    const txt = document.getElementById("updText");
    if (p.state === "downloading") {
      const total = p.total || size || 0;
      const pct = total ? Math.min(99, Math.round(p.received / total * 100)) : 0;
      if (bar) bar.style.width = pct + "%";
      if (txt) txt.textContent = `已下载 ${(p.received / 1048576).toFixed(1)} MB` +
        (total ? ` / ${(total / 1048576).toFixed(1)} MB（${pct}%）` : "");
      return;
    }
    updStop();
    if (p.state === "error") {
      return openUpdPanel("下载失败", p.error || "未知错误",
        [["重试", () => startUpdate(url, size)]]);
    }
    if (!p.exists) {
      return openUpdPanel("下载失败", "文件没落盘：\n" + (p.path || "（没有路径）") +
        "\n大小：" + (p.size || 0) + " 字节", [["重试", () => startUpdate(url, size)]]);
    }
    if (bar) bar.style.width = "100%";
    if (txt) txt.textContent = "下载完成（" + fmtMB(p.size) + "）\n\n" +
      "点「立即更新」后程序会关闭、解压覆盖、自动重启。\nconfig.json 会保留。\n\n" +
      "保存位置：" + p.path;
    const btns = document.getElementById("updBtns");
    if (!btns) return;
    btns.innerHTML = "";
    const b = document.createElement("button");
    b.className = "mini";
    b.textContent = "立即更新";
    b.onclick = async () => {
      b.disabled = true;
      b.textContent = "正在更新…";
      updatingNow = true;
      let res;
      try { res = await api.post("/api/update/apply", {}); }
      catch (e) { res = { ok: false, error: String(e) }; }
      if (!res || !res.ok) {
        updatingNow = false;
        return openUpdPanel("更新没启动", "原因：" + ((res && res.error) || "未知错误") +
          "\n\n可以重试，或手动解压：\n" + (p.path || "%TEMP%\\rotpilot_update\\update.zip"),
          [["重试", () => startUpdate(url, size)]]);
      }
      const t = document.getElementById("updText");
      if (t) t.textContent = "正在更新：解压 → 覆盖 → 改名 → 重启。\n窗口会自动关闭，稍等几十秒。";
      const bs = document.getElementById("updBtns");
      if (bs) bs.innerHTML = "";
    };
    btns.appendChild(b);
  }, 500);
}

/* ---------------- 干员包：列出全部，自己勾 ---------------- */
async function checkOpsPack() {
  openUpdPanel("正在找干员包…", "读取所有 Release…", []);
  let r;
  try { r = await api.get("/api/update/operators/list"); }
  catch (e) { return openUpdPanel("检查失败", "连不上后台", []); }
  if (!r || !r.ok) {
    return openUpdPanel("检查失败", (r && r.error) || "无法访问 GitHub", []);
  }
  if (!r.items || !r.items.length) {
    return openUpdPanel("没有找到干员包",
      "所有 Release 里都没有 operators 开头的 zip 附件。\n\n" +
      "要求：附件名以 operators 开头、.zip 结尾。", []);
  }
  openOpsList(r.items);
}

function openOpsList(items) {
  const box = openUpdPanel("选择要导入的干员包", "", []);
  const pre = box.querySelector("#updText");
  pre.style.display = "none";
  const list = box.querySelector("#updList");
  list.style.display = "flex";
  list.innerHTML = "";
  items.forEach((it, i) => {
    const row = document.createElement("label");
    row.style.cssText = "display:flex;gap:8px;align-items:flex-start;padding:6px 8px;" +
      "border:1px solid #333;border-radius:6px;cursor:pointer;font:12px 'Microsoft YaHei UI';";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !it.installed;
    cb.dataset.idx = String(i);
    cb.style.marginTop = "2px";
    const info = document.createElement("div");
    info.style.cssText = "flex:1;line-height:1.5";
    info.innerHTML =
      `<b>${it.name}</b>${it.installed ? ' <span style="color:#7ee787">（已装过）</span>' : ""}` +
      `<span style="display:block;color:#9aa0a6">${fmtMB(it.size)}　Release：${it.release || "-"}` +
      `　${(it.published || "").slice(0, 10)}</span>`;
    row.appendChild(cb);
    row.appendChild(info);
    list.appendChild(row);
  });
  const btns = box.querySelector("#updBtns");
  btns.innerHTML = "";
  const mkBtn = (label, fn) => {
    const b = document.createElement("button");
    b.className = "mini";
    b.textContent = label;
    b.onclick = fn;
    btns.appendChild(b);
    return b;
  };
  mkBtn("全选", () => list.querySelectorAll("input[type=checkbox]").forEach(c => c.checked = true));
  mkBtn("全不选", () => list.querySelectorAll("input[type=checkbox]").forEach(c => c.checked = false));
  mkBtn("导入所选", () => {
    const picked = [...list.querySelectorAll("input[type=checkbox]")]
      .filter(c => c.checked)
      .map(c => items[Number(c.dataset.idx)]);
    if (!picked.length) return toast("一个都没选");
    importOpsSelected(picked);
  });
}

async function importOpsSelected(picked) {
  openUpdPanel("正在导入干员包…", `0 / ${picked.length}`, []);
  const box = document.getElementById("updBox");
  if (box) {
    box.querySelector("#updProg").style.display = "block";
    box.querySelector("#updBar").style.width = "30%";
  }
  let j;
  try {
    j = await api.post("/api/update/operators/apply",
      { items: picked.map(x => ({ url: x.url, name: x.name })) });
  } catch (e) {
    return openUpdPanel("导入失败", String(e), []);
  }
  if (!j || !j.results) {
    return openUpdPanel("导入失败", (j && j.error) || "后台没有返回结果", []);
  }
  const okList = j.results.filter(x => x.ok);
  const badList = j.results.filter(x => !x.ok);
  if (okList.length) {
    state.ops = await api.get("/api/ops");
    renderAdder(); renderSlots(); renderSeq();
    if (!$("#picker").classList.contains("hidden")) { renderTabs(); renderGrid(); }
  }
  const lines = [];
  okList.forEach(x => lines.push(`✅ ${x.name}：导入 ${x.count || 0} 名` +
    (x.names && x.names.length ? `（${x.names.join("、")}）` : "")));
  badList.forEach(x => lines.push(`❌ ${x.name}：${x.error || "失败"}`));
  openUpdPanel(`导入完成：成功 ${okList.length} / ${j.results.length}`,
    lines.join("\n"), [["好", () => { const b = document.getElementById("updBox"); if (b) b.remove(); }]]);
}

$("#btnAddOp").onclick = () => $("#opFile").click();

$("#opFile").onchange = async (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  if (!f) return;
  toast(`正在导入 ${f.name} …`);
  try {
    const buf = await f.arrayBuffer();
    const r = await fetch("/api/import_operator", {
      method: "POST",
      headers: { "Content-Type": "application/zip", "X-Filename": encodeURIComponent(f.name) },
      body: buf,
    });
    const j = await r.json();
    if (!j.ok) { toast("导入失败：" + (j.error || "未知错误")); return; }
    try { $("#biliName").textContent = LINKS.biliName; } catch (e) {}
  state.ops = await api.get("/api/ops");
    renderAdder();
    renderSlots();
    renderSeq();
    if (!$("#picker").classList.contains("hidden")) { renderTabs(); renderGrid(); }
    toast(`已添加干员：${j.name}（${j.element || ""} ${j.class || ""}）`);
  } catch (err) {
    toast("导入失败：" + err);
  }
};

$("#btnQuick").onclick = () => openPicker(0, true);
$("#btnSaveComp").onclick = () => {
  const team = [...state.team];
  if (!team.filter(Boolean).length) return toast("编队是空的");
  const key = JSON.stringify(team);
  const had = (state.comps || []).find((c) => JSON.stringify((c.team || []).slice(0, 4)) === key);
  if (had) return toast(`这套编队已经在库里了：「${had.name}」`);
  const base = team.filter(Boolean).join(" · ").slice(0, 40);
  const name = prompt("给这套编队起个名字：", base);
  if (name === null) return;
  let finalName = (name.trim() || base).slice(0, 40);
  let k = 2;
  while (state.comps.some((c) => c.name === finalName)) {
    finalName = `${(name.trim() || base).slice(0, 36)} (${k++})`;
  }
  state.comps.push({ name: finalName, team });
  renderComps();
  persistSoon();
  toast(`已保存编队：${finalName}`);
};
$("#btnCompAll").onclick = () => {
  state.compFilter = null;
  renderLib();
  toast("显示全部轴");
};

$("#btnClearTeam").onclick = () => {
  state.team = ["", "", "", ""];
  renderSlots();
  renderSeq();
  toast("已清空编队");
  persistSoon();
};
$("#pickerClose").onclick = closePicker;
$("#pickerDone").onclick = closePicker;
$("#pickerClear").onclick = () => {
  state.team = ["", "", "", ""];
  renderSlots();
  renderSeq();
  persistSoon();
  toast("已清空编队");
};
$("#picker").onclick = (e) => { if (e.target.id === "picker") closePicker(); };

document.querySelectorAll(".skill-btn[data-key]").forEach((b) => {
  b.onclick = () => addSkill(b.dataset.key, b.dataset.hold === "1");
});
$("#btnDivider").onclick = addDivider;
$("#btnNote").onclick = addNote;
$("#btnClearSeq").onclick = clearSeq;
$("#btnHeavy").onclick = addHeavy;
$("#btnUndo").onclick = undo;
$("#btnRedo").onclick = redo;
$("#btnLeft").onclick = () => moveSel(-1);
$("#btnRight").onclick = () => moveSel(1);
$("#btnSettings").onclick = openSettings;
$("#setClose").onclick = () => $("#setModal").classList.add("hidden");
if ($("#setClose2")) $("#setClose2").onclick = () => $("#setModal").classList.add("hidden");
$("#setSave").onclick = saveSettings;

/* ---------------- 补丁导入 ---------------- */
async function applyPatchFile(file) {
  if (!file) return;
  if (!file.name.toLowerCase().endsWith(".zip")) return toast("请选择 .zip 补丁包");
  toast("正在导入补丁…");
  try {
    const buf = await file.arrayBuffer();
    const res = await fetch("/api/patch_import", {
      method: "POST",
      headers: { "Content-Type": "application/zip" },
      body: buf,
    });
    const r = await res.json();
    if (!r.ok) return toast("导入失败：" + (r.error || "未知错误"));
    const n = (r.files || []).length;
    if (r.skipped && r.skipped.length) {
      toast(`已更新 ${n} 个文件，跳过 ${r.skipped.length} 个（runtime 或非法路径）`);
    } else {
      toast(`已更新 ${n} 个文件，正在刷新…`);
    }
    if (r.restart) {
      setTimeout(async () => {
        if (confirm("这次补丁改到了程序本体，需要重启才能生效。\n现在重启？")) {
          await api.post("/api/restart", {});
        } else {
          toast("请手动关掉黑框后重新启动");
        }
      }, 600);
    } else {
      setTimeout(() => location.reload(), 700);
    }
  } catch (e) {
    toast("导入出错：" + e.message);
  }
}

$("#btnQuitAll").onclick = async () => {
  if (!confirm("退出整个程序？后台 Python 也会一起结束。")) return;
  try { await api.post("/api/quit", {}); } catch (e) {}
  toast("已退出");
  setTimeout(() => window.close(), 400);
};

$("#btnPatchImport").onclick = () => $("#patchFile").click();
$("#patchFile").onchange = (e) => {
  const f = e.target.files && e.target.files[0];
  e.target.value = "";
  applyPatchFile(f);
};

$("#btnPatchRestore").onclick = async () => {
  if (!confirm("还原上一次补丁覆盖过的文件？")) return;
  try {
    const r = await api.post("/api/patch_restore", {});
    if (!r || !r.ok) return toast((r && r.error) || "没有可还原的备份");
    toast(`已还原 ${(r.restored || []).length} 个文件`);
    if (r.restart) {
      setTimeout(async () => {
        if (confirm("还原涉及程序本体，需要重启。现在重启？")) {
          await api.post("/api/restart", {});
        }
      }, 600);
    } else {
      setTimeout(() => location.reload(), 700);
    }
  } catch (e) {
    toast("还原出错：" + e.message);
  }
};
$("#btnBreak").onclick = addBreak;
$("#btnRepeat").onclick = addRepeat;
$("#btnDelete").onclick = deleteSel;

$("#btnShare").onclick = openShare;
$("#shareClose").onclick = () => $("#shareModal").classList.add("hidden");
$("#shareCopy").onclick = async () => {
  await navigator.clipboard.writeText($("#shareText").value);
  toast("已复制");
};
// 切换导出格式：说明文字跟着变，码当场重生成
if ($("#shareMode")) {
  $("#shareMode").addEventListener("change", () => {
    refreshShareHint();
    buildShareCode();
  });
}
if ($("#btnExportJson")) $("#btnExportJson").onclick = exportJson;
$("#btnImport").onclick = () => {
  closeImport();                       // 先清干净，别把上次的预览带进来
  $("#importModal").classList.remove("hidden");
};
$("#importClose").onclick = closeImport;
$("#importOk").onclick = doImport;
if ($("#importPreviewBtn")) $("#importPreviewBtn").onclick = doPreview;
// 码一改，之前解析的结果就作废，预览也收起来
if ($("#importText")) $("#importText").addEventListener("input", () => {
  _parsedImport = null;
  $("#importMsg").textContent = "";
  if ($("#importPreviewBox")) $("#importPreviewBox").classList.add("hidden");
});

// 设置里「查看最近日志」：出问题时把末尾日志抓出来，能直接复制发给作者
if ($("#btnLastLog")) $("#btnLastLog").onclick = showLastLog;
if ($("#btnAboutNotes")) $("#btnAboutNotes").onclick = showAboutNotes;



// 轴库备份 / 恢复
if ($("#libSearch")) $("#libSearch").addEventListener("input", (e) => {
  state.libQuery = e.target.value;
  renderLib();
});
if ($("#btnLibBackup")) $("#btnLibBackup").onclick = libBackup;
if ($("#btnLibRestore")) $("#btnLibRestore").onclick = () => $("#libFile").click();
if ($("#btnLibDemo")) $("#btnLibDemo").onclick = loadDemoAxes;
if ($("#libFile")) $("#libFile").addEventListener("change", libRestoreFile);

$("#btnSave").onclick = async () => {
  const loaded = state.loadedTeam;
  const cur = loaded ? state.teams.find((x) => x.name === loaded) : null;

  if (cur) {
    // 内容没变：别弹框，也别白写一遍
    if (axisSig(cur.team, cur.steps) === axisSig(state.team, state.steps)) {
      await persist();
      return toast(`「${loaded}」已经是最新的，没有改动`);
    }
    const overwrite = confirm(
      `编队库里的「${loaded}」是 ${(cur.steps || []).length} 格，当前轴是 ${state.steps.length} 格。\n\n` +
      `确定 = 用当前轴覆盖「${loaded}」（原来的内容会被替换）\n` +
      `取消 = 另存为一条新轴（「${loaded}」原样保留）`);
    if (overwrite) {
      snapshot();                    // 覆盖前记一份，Ctrl+Z 能退回来
      const idx = state.teams.findIndex((x) => x.name === loaded);
      state.teams[idx] = {
        name: loaded,
        team: [...state.team],
        steps: JSON.parse(JSON.stringify(state.steps)),
        holdMs: state.holdMs,
      };
      renderLib();
      await persist();
      return toast(`已覆盖「${loaded}」　Ctrl+Z 可撤销`);
    }
  }

  await persist();
  const dft = state.teams.length + 1;
  const guess = loaded ? `${loaded} 副本` : autoAxisName(state.team);
  const name = prompt("存进技能轴库？给它起个名字（取消则不存）", guess);
  if (name === null) { toast("已保存当前进度"); return; }
  const finalName = (name.trim() || `轴 ${dft}`).slice(0, 40);
  state.teams.push({
    name: finalName,
    team: [...state.team],
    steps: JSON.parse(JSON.stringify(state.steps)),
    holdMs: state.holdMs,
  });
  state.loadedTeam = finalName;
  renderLib();
  await persist();
  toast(`已存入技能轴库：${finalName}`);
};

if ($("#setScreen")) {
  $("#setScreen").onchange = () => applyScreenMode($("#setScreen").value);
}

$("#btnStop").onclick = async () => {
  const r = await api.post("/api/stop", {});
  if (r && r.ok) toast("已停止运行");
  else toast("当前没有在运行");
};

$("#btnRun").onclick = runOverlay;

// 点序列区空白 → 放光标
$("#seq").addEventListener("click", (e) => {
  if (e.target.closest(".cell") || e.target.closest(".divider")) return;
  const r = $("#seq").getBoundingClientRect();
  if (e.clientY < r.top || e.clientY > r.bottom + 40) return;
  caretFromPoint(e.clientX, e.clientY);
  renderSeq();
});
$(".seq-wrap").addEventListener("click", (e) => {
  if (e.target !== e.currentTarget) return;
  caretFromPoint(e.clientX, e.clientY);
  renderSeq();
});


// 再兜一层：浏览器原生 copy/cut/paste 事件（有些情况下 keydown 收不到）
document.addEventListener("copy", (e) => {
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  e.preventDefault();
  copySel();
});
document.addEventListener("cut", (e) => {
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  e.preventDefault();
  cutSel();
});
document.addEventListener("paste", (e) => {
  if (["INPUT", "TEXTAREA"].includes(e.target.tagName)) return;
  e.preventDefault();
  pasteSel();
});

// Q / E 和 Alt 一样：按住不放，再按 1~4 才生效（松开就失效）
const _held = { q: false, e: false };
window.addEventListener("blur", () => { _held.q = false; _held.e = false; });

document.addEventListener("keyup", (e) => {
  const low = (e.key || "").toLowerCase();
  if (low === "q") _held.q = false;
  if (low === "e") _held.e = false;
});

document.addEventListener("keydown", (e) => {
  if (["TEXTAREA", "INPUT"].includes(e.target.tagName)) return;
  const k = e.key;
  const lower = k.toLowerCase();

  // 快速输入：1~4 战技 / Alt+1~4 终结技 / E→1~4 连携 / H 重击
  if (!e.ctrlKey && !e.metaKey) {
    if (/^[1-4]$/.test(k)) {
      if (e.altKey) {
        e.preventDefault();
        addSkill(k, true);
        return;
      }
      if (_held.e) {
        e.preventDefault();
        addSkill("E", false, Number(k));
        return;
      }
      if (_held.q) {
        e.preventDefault();
        addSkill(k, false);
        return;
      }
      // 单独按数字什么都不做（要按住 Q / E，或 Alt+数字）
      return;
    }
    if (lower === "h" && !e.altKey) {
      e.preventDefault();
      addHeavy();
      return;
    }
    if (lower === "e" && !e.altKey) {
      e.preventDefault();
      _held.e = true;
      return;
    }
    if (lower === "q" && !e.altKey) {
      e.preventDefault();
      _held.q = true;
      return;
    }
  }

  if (e.key === "Enter") { e.preventDefault(); addBreak(); return; }
  if (e.key === "Backspace") { e.preventDefault(); backspace(); return; }
  if (e.key === "Delete") { e.preventDefault(); deleteSel(); return; }
  if (e.key === "ArrowLeft") { e.preventDefault(); moveCaret(-1, 0); return; }
  if (e.key === "ArrowRight") { e.preventDefault(); moveCaret(1, 0); return; }
  if (e.key === "ArrowUp") { e.preventDefault(); moveCaret(0, -1); return; }
  if (e.key === "ArrowDown") { e.preventDefault(); moveCaret(0, 1); return; }
  if (e.ctrlKey && e.shiftKey && e.key.toLowerCase() === "z") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "y") { e.preventDefault(); redo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "c") { e.preventDefault(); copySel(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "x") { e.preventDefault(); cutSel(); return; }
  if (e.ctrlKey && e.key.toLowerCase() === "v") { e.preventDefault(); pasteSel(); return; }
  if (e.key === "Escape") { state.sel = null; state.caret = null; renderSeq(); }
});

window.addEventListener("resize", () => requestAnimationFrame(positionCaret));

const _m = new URLSearchParams(location.search);
if (_m.has("caret")) state.caret = Number(_m.get("caret"));
if (_m.has("picker")) setTimeout(() => openPicker(Number(_m.get("picker")) || 0, false), 300);
if (_m.has("settings")) setTimeout(() => { try { openSettings(); } catch (e) { console.error(e); } }, 500);
// 禁止浏览器把拖拽当成“选文字 / 搜索文本”
document.addEventListener("selectstart", (e) => {
  const t = e.target;
  if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA")) return;
  e.preventDefault();
});
document.addEventListener("dragstart", (e) => {
  // 编队槽要能拖动换位置，别被「禁止拖图片」拦掉
  if (e.target && e.target.closest && e.target.closest(".slot")) return;
  if (e.target && e.target.tagName === "IMG") e.preventDefault();
});

boot();
// 立刻问一次权限状态（设置面板里那行“管理员权限 ✓ / 未获权限”要马上是对的）
setTimeout(() => { if (typeof heartbeat === "function") heartbeat(); }, 600);
