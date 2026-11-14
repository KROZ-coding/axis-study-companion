const stage = document.querySelector("#stage");
const view = document.querySelector("#view");
const toastEl = document.querySelector("#toast");
const reduceQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
// 接口层由 index.html 先加载并挂在 window 上（它要先探一次后端）
const api = window.api;

const sample = {
  className: "七（1）班",
  course: "七年级数学 · 一元一次方程",
  students: [
    {
      id: "lin",
      name: "林夏",
      note: "去括号时漏乘常数项。正式练习前先看讲解。",
      mastery: 46,
      evidence: "4 条有效作答，其中 2 条在提示后完成。最近一次是 11 月 9 日。",
      status: "待补基础",
    },
    {
      id: "zhou",
      name: "周予",
      note: "已能独立移项，卡在含分数系数的检验。",
      mastery: 78,
      evidence: "6 条有效作答，均未看完整解析。覆盖移项和检验。",
      status: "可进入目标题",
    },
  ],
  issues: [
    { title: "去括号漏乘", count: "6 / 18 人", detail: "错在把 3(x − 2) 写成 3x − 2。对应七上 2.2。" },
    { title: "移项不变号", count: "4 / 18 人", detail: "移项后符号未变，随后验算也跳过。" },
  ],
};

const state = {
  screen: "entry",
  shifted: false,
  authTab: "login",
  authRole: "student",
  role: null,
  who: "",
  teacherView: "home",
  studentTab: "tasks",
  studentPage: "home",
  taskFilter: "all",
  transition: "",
  transitionId: 0,
  loggingOut: false,
  openStudent: null,
  drawer: false,
  sheet: null,
  mode: "tutor",
  answers: ["", "", ""],
  currentQ: 0,
  submitted: false,
  seenSolution: false,
  conversations: ["去括号再练一次"],
  activeChat: 0,
  banner: "",
  error: "",
  reduce: reduceQuery.matches,
  pointer: { x: 0.62, y: 0.42 },
  reveal: 0,
  authStatus: "ready",
  authAccount: "",
  attempts: 0,
  serviceError: "",
  formulaIndex: 0,
  clock: { hm: "--:--", sec: "--", date: "" },
  step: 0,
  anchorA: { x: 900, y: 470 },
  anchorB: { x: 560, y: 640 },
  anchorYaw: 0.40,
  anchorPitch: 0.17,
  anchorSpin: 0,
  railEvents: [],
  railLabel: "入口 · 会话未开启",
  fallback: false,
  // 真后端
  live: false,
  loading: false,
  liveError: "",
  courseId: null,
  courseName: "",
  classId: null,
  className: "",
  liveStudents: [],
  liveGoals: [],
  liveTasks: [],
  liveTask: null,
  liveLease: null,
  liveConversations: [],
  liveMessages: [],
  conversationError: "",
  diagnostic: null,
  diagnosticAnswers: [],
  draftDirty: false,
  savedAt: null,
  studentDrafts: {},
  selectedAttemptId: null,
  liveReport: null,
  liveReportFormats: [],
  yaw: 0.5,
  pitch: 0.25,
};

let leaseTimer = 0;
let workspaceRequest = 0;
let toastTimer = 0;

function toast(text) {
  toastEl.textContent = text;
  toastEl.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toastEl.classList.remove("show"), 2200);
}

function escapeHtml(value) {
  return String(value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function render() {
  stage.classList.toggle("show-glass", state.screen === "entry");
  stage.classList.toggle("entry-departing", state.transition === "departing");
  stage.classList.toggle("entry-returning", state.transition === "returning");
  stage.classList.toggle("workspace-visible", state.screen === "app");
  stage.dataset.transition = state.transition;
  stage.setAttribute("aria-busy", String(Boolean(state.transition)));
  if (state.screen === "entry") renderEntry();
  else if (state.role === "admin") renderAdmin();
  else if (state.role === "teacher") renderTeacher();
  else renderStudent();
  bind();
  if (state.screen === "entry") {
    fitCanvas();
    refreshAnchors();
  }
}

const AUTH_COPY = {
  login: { label: "账号验证", submit: "登录" },
  activate: { label: "账号激活", submit: "激活账号" },
  reset: { label: "口令重设", submit: "重设口令" },
  apply: { label: "教师账号申领", submit: "提交审核" },
};
const AUTH_STATE_LABEL = { denied: "未通过", checking: "核对中", sent: "等待审核" };
const AUTH_ROLE_LABEL = { student: "学生", teacher: "教师", admin: "管理员" };
const AUTH_FORMULAS = ["aₙ₊₁ = aₙ + d", "x = (−b ± √Δ) / 2a", "∫₀ᵗ v(τ) dτ = Δx", "∑ᵢ₌₁ⁿ i = n(n + 1) / 2"];
// 口令框的眼睛：默认明文，点击后切换为方块遮蔽（-webkit-text-security: square）
const EYE_OPEN = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>';
const EYE_CLOSED = '<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';
// 侧栏实时监控：身份与会话状态都取自真实本机状态，事件流带真实时间戳
const MON_SESSION = { ready: "未建立", checking: "核验中", denied: "未通过", sent: "待审核" };
const RAIL_EVENT_MAX = 6;

function monitorIdentity() {
  return state.authTab === "apply" ? "教师申领"
    : state.authTab === "activate" ? "学生激活"
      : state.authTab === "reset" ? "口令重设" : AUTH_ROLE_LABEL[state.authRole];
}

function hhmmss() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
}

function resetEvents(texts) {
  state.railEvents = texts.slice(-RAIL_EVENT_MAX).map((text) => ({ t: hhmmss(), text }));
}

function renderEntry() {
  const open = state.reveal > 0.5;
  state.shifted = open;
  const rv = state.reveal.toFixed(3);
  view.innerHTML = `
    <section class="entry ${open ? "shifted" : ""}" style="--reveal:${rv}" ${state.transition ? 'inert aria-hidden="true"' : ""}>
      <div class="entry-canvas">
        ${state.fallback ? fallbackSvg() : ""}
        <div class="entry-layer keep rail" aria-hidden="true">
          <div class="rail-rule" style="--reveal:${rv}"></div>
          <div class="rail-marker" style="--reveal:${rv}"></div>
          <div class="rail-body" style="--reveal:${rv}">
            <i class="rail-bar"></i>
            <i class="rail-dash" style="top:358px"></i>
            <div class="rail-chip" style="left:44px; top:396px">伴星导学 · 学习证据</div>
            <p class="rail-micro" style="top:416px">作答、提示与材料依据逐项留存。每个判断都有出处，尚无记录时如实说明。</p>
            <div class="rail-chip" style="left:44px; top:608px"><i class="mon-led ${state.live ? "on" : ""}" aria-hidden="true"></i>实时监控</div>
            <div class="rail-monitor" style="--reveal:${rv}" aria-hidden="true">
              <div class="mon-grid">
                <span>服务</span><b class="${state.live ? "ok" : ""}">${state.live ? "已连接" : "未连接"}</b>
                <span>身份</span><b>${escapeHtml(monitorIdentity())}</b>
                <span>账号</span><b class="mon-account">${escapeHtml(state.authAccount || "—")}</b>
                <span>会话</span><b>${MON_SESSION[state.authStatus] || "未建立"}</b>
              </div>
              <div class="rail-log">
                ${state.railEvents.map((event) => `<div class="row"><em>${event.t}</em><span>${escapeHtml(event.text)}</span></div>`).join("")}
              </div>
              <span class="mon-cursor"></span>
            </div>
            <i class="rail-dash" style="left:44px; top:852px"></i>
            <i class="rail-hair" style="left:66px; top:853px"></i>
            <i class="rail-foot" style="top:946px"></i>
            <div class="rail-label" style="left:71px; top:944px">${escapeHtml(state.railLabel)}</div>
          </div>
        </div>

        <div class="entry-layer keep brand" aria-hidden="true">
          <div class="brand-l1">伴星导学</div>
          <div class="brand-l2"><span class="axis">AXIS</span></div>
          <div class="brand-l3">LEARNING EVIDENCE OS</div>
          <div class="brand-rule"></div>
        </div>

        <div class="entry-layer keep chrono" aria-hidden="true">
          <div class="chrono-cradle" role="img" aria-label="正在平衡的牛顿摆">
            <span class="cradle-frame"><i></i><i></i><i></i><i></i><i></i></span>
            <span class="cradle-strings"><i></i><i></i><i></i><i></i><i></i></span>
            <span class="cradle-balls"><i></i><i></i><i></i><i></i><i></i></span>
          </div>
          <div class="chrono-time">
            <span class="chrono-hm" style="font-weight:${200 + Math.round(state.reveal * 3) * 100}">${state.clock.hm}</span><span class="chrono-sec">${state.clock.sec}</span>
          </div>
          <div class="chrono-date">${state.clock.date}</div>
          <div class="chrono-term"><i class="${state.live ? "online" : "offline"}"></i>${escapeHtml(state.live ? "正式服务 · 安全登录" : state.serviceError || "正式服务连接中断")}</div>
        </div>

        <div class="entry-layer callouts" aria-hidden="true">
          <svg width="1920" height="1080">
            <line x1="${state.anchorA.x}" y1="${state.anchorA.y}" x2="${state.anchorA.x + 150}" y2="${state.anchorA.y - 130}" />
            <line x1="${state.anchorB.x}" y1="${state.anchorB.y}" x2="${state.anchorB.x - 120}" y2="${state.anchorB.y + 170}" />
            <circle cx="${state.anchorA.x}" cy="${state.anchorA.y}" r="3.5" />
            <circle cx="${state.anchorB.x}" cy="${state.anchorB.y}" r="3.5" />
          </svg>
          <div class="callout-label" style="left:${state.anchorA.x + 158}px; top:${state.anchorA.y - 152}px">
            <span>SECT.01</span><b>学习记录</b><em>每一次作答与原题</em>
          </div>
          <div class="callout-label" style="left:${state.anchorB.x - 208}px; top:${state.anchorB.y + 168}px">
            <span>SECT.02</span><b>判断依据</b><em>可定位 · 可复核</em>
          </div>
        </div>

        <div class="entry-layer entry-copy">
          <p class="eyebrow">七年级数学 · 学习证据系统</p>
          <h1>让每一步学习，都有据可循。</h1>
          <p class="lede">作答、提示与课程材料保留在各自的学习记录中。学生与教师使用正式账号，资料按授权范围访问。</p>
          <span class="sample-flag">安全认证 · 以账号权限为准</span>
        </div>

        <div class="entry-layer entry-foot">
          <button class="scroll-hint" type="button" data-action="reveal" ${open ? "inert" : ""}>
            <span class="wheel" aria-hidden="true"><i></i></span>
            <span class="hint-main">向下滑动登录</span>
            <span class="hint-sub">拖动玻璃调整视角</span>
          </button>
          <div class="entry-signature"><span>POWERED BY</span><b>AXIS</b><i></i></div>
        </div>

        ${authCard(open)}
      </div>
    </section>
    ${state.transition ? `<div class="entry-transition-status" role="status" aria-live="polite"><i aria-hidden="true"></i><span>${state.transition === "returning" ? "会话已结束 · 正在归位" : "身份已核验 · 正在进入学习空间"}</span></div>` : ""}`;
}

/** 顶部时钟：每秒刷新一次，避免整屏重渲染 */
function tickClock() {
  const now = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  const DAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  state.clock = {
    hm: `${pad(now.getHours())}:${pad(now.getMinutes())}`,
    sec: pad(now.getSeconds()),
    date: `${now.getFullYear()} 年 ${now.getMonth() + 1} 月 ${now.getDate()} 日 · ${DAYS[now.getDay()]}`,
  };
  if (state.screen !== "entry") return;
  const root = view.querySelector(".chrono");
  if (root) {
    root.querySelector(".chrono-hm").textContent = state.clock.hm;
    root.querySelector(".chrono-sec").textContent = state.clock.sec;
    root.querySelector(".chrono-date").textContent = state.clock.date;
  }
  // 侧栏监控：每秒对齐一次真实连接/会话状态，不整屏重渲染
  const monitor = view.querySelector(".rail-monitor");
  if (!monitor) return;
  const cells = monitor.querySelectorAll(".mon-grid b");
  if (cells[0]) {
    cells[0].textContent = state.live ? "已连接" : "未连接";
    cells[0].classList.toggle("ok", state.live);
  }
  if (cells[1]) cells[1].textContent = monitorIdentity();
  if (cells[2]) cells[2].textContent = state.authAccount || "—";
  if (cells[3]) cells[3].textContent = MON_SESSION[state.authStatus] || "未建立";
  const led = view.querySelector(".mon-led");
  if (led) led.classList.toggle("on", state.live);
}

function authCard(open) {
  const copy = AUTH_COPY[state.authTab];
  const busy = state.authStatus === "checking";
  const locked = busy || state.authStatus === "sent" || !state.live;
  const displayName = escapeHtml(state.authAccount || "等待身份核验");
  const stateLabel = state.authStatus === "sent" ? "等待管理员审核"
    : state.authStatus === "checking" ? "正在连接正式认证"
      : state.authStatus === "denied" ? "认证未通过" : "尚未建立会话";
  const stateClass = state.authStatus === "denied" ? "alert" : state.authStatus === "sent" ? "ok" : "";
  const identity = state.authTab === "apply" ? "教师申领"
    : state.authTab === "activate" ? "学生激活"
      : state.authTab === "reset" ? "口令重设" : AUTH_ROLE_LABEL[state.authRole];
  const service = state.live ? "正式认证服务已连接" : state.serviceError;
  const formula = AUTH_FORMULAS[state.formulaIndex % AUTH_FORMULAS.length];
  return `<form class="auth-card ${state.authStatus === "denied" ? "denied" : ""}" data-action="auth" aria-label="账号安全认证" ${open ? "" : "inert"}>
      <div class="auth-rear" aria-hidden="true"></div>
      <div class="auth-slab" aria-hidden="true">
        <div class="hatch"></div>
        <div class="ghost">${busy ? "VERIFY" : "AXIS"}</div>
        <div class="ghost-name">${escapeHtml(identity)}</div>
        <i class="auth-bev t"></i><i class="auth-bev l"></i><i class="auth-bev b"></i><i class="auth-bev r"></i>
      </div>
      <div class="auth-inner">
        <header class="auth-head">
          <span class="auth-mark"><i aria-hidden="true"></i>账号 · 安全认证</span>
          <div class="auth-head-right">
            <span class="auth-outline">学习证据系统</span>
            <span class="auth-brandtag">伴星导学 AXIS</span>
          </div>
        </header>

        <div class="auth-photo" aria-hidden="true">
          <div class="film formula-film">
            <span class="formula-index">MATH / AXIS</span>
            <span class="formula-rule"></span>
            <span class="equation-reel" data-equation="${escapeHtml(formula)}">${escapeHtml(formula)}</span>
            <span class="formula-coord">x → f(x)</span>
          </div>
          <div class="badge">∑</div>
          <i class="tick" style="right:-3px; top:-3px"></i><i class="tick" style="left:-3px; bottom:-3px"></i><i class="tick" style="right:-3px; bottom:-3px"></i>
        </div>

        <div class="auth-id">
          <div class="auth-rank">
            <span class="auth-role">${identity}</span>
            <span class="auth-state ${stateClass}">${stateLabel}</span>
          </div>
          <div class="auth-name"><b>${displayName}</b></div>
          <div class="auth-file">
            <span class="stamp">AXIS</span>
            <div class="info">
              <div class="loaded">${state.live ? "等待本人身份核验" : "认证服务暂不可用"} <i></i></div>
              <div class="mono">${state.authStatus === "sent" ? "教师账号 · 等待审核" : "会话 · 尚未建立"}</div>
            </div>
          </div>
        </div>

        <div class="auth-profile">
          <div class="ph">认证状态</div>
          <dl>
            <dt>学生</dt><dd>查看个人任务与记录</dd>
            <dt>教师</dt><dd>查看授权班级与依据</dd>
            <dt>申领</dt><dd>教师账号须经管理员审核</dd>
          </dl>
          <i class="rule"></i><span class="foot">登录后读取授权范围</span>
        </div>

        <div class="auth-main">
          <div class="auth-main-head">
            <span class="label"><i></i>${copy.label}</span>
            <span class="attempt ${state.authStatus === "denied" ? "hot" : ""}">${state.authStatus === "sent" ? "等待审核" : state.authStatus === "denied" ? "请检查信息" : state.live ? "安全连接" : "服务离线"}</span>
          </div>
          <div class="tabs role-tabs" role="group" aria-label="登录身份">
            <button class="chip" type="button" data-role="student" ${busy ? "disabled" : ""} aria-pressed="${state.authRole === "student"}">学生</button>
            <button class="chip" type="button" data-role="teacher" ${busy ? "disabled" : ""} aria-pressed="${state.authRole === "teacher"}">教师</button>
          </div>
          ${authFields()}
          <p class="error-line ${state.live ? "" : "service-offline"}" role="alert">${escapeHtml(state.error || (!state.live ? service : ""))}</p>
          ${state.authTab === "login" ? `<label class="field">登录口令${passBar({ locked, autocomplete: "current-password", label: "登录口令" })}</label>`
            : `<label class="field">${state.authTab === "apply" ? "设置登录口令" : "新口令"}${passBar({ locked, autocomplete: "new-password", label: "新口令", placeholder: "至少 8 位" })}</label>`}
          <button class="btn primary wide auth-submit" type="submit" ${busy || !state.live || state.authStatus === "sent" ? "disabled" : ""}>${busy ? "正在核验…" : state.authStatus === "sent" ? "已提交审核" : copy.submit}</button>
          <div class="tabs subtabs" role="group" aria-label="${state.authRole === "teacher" || state.authTab === "apply" ? "教师入口" : "学生入口"}">
            ${state.authRole === "teacher" || state.authTab === "apply"
              ? `<button class="chip" type="button" data-tab="login" aria-pressed="${state.authTab === "login"}" ${busy ? "disabled" : ""}>口令登录</button>
                 <button class="chip" type="button" data-tab="reset" aria-pressed="${state.authTab === "reset"}" ${busy ? "disabled" : ""}>重设口令</button>
                 <button class="chip" type="button" data-auth-mode="apply" aria-pressed="${state.authTab === "apply"}" ${busy ? "disabled" : ""}>申领账号</button>`
              : `<button class="chip" type="button" data-tab="login" aria-pressed="${state.authTab === "login"}" ${busy ? "disabled" : ""}>口令登录</button>
                 <button class="chip" type="button" data-tab="activate" aria-pressed="${state.authTab === "activate"}" ${busy ? "disabled" : ""}>首次激活</button>
                 <button class="chip" type="button" data-tab="reset" aria-pressed="${state.authTab === "reset"}" ${busy ? "disabled" : ""}>重设口令</button>`}
          </div>
        </div>
      </div>
    </form>`;
}

/** 口令框：默认明文，右侧眼睛切换方块遮蔽；登录按钮独立放在下方。 */
function passBar({ locked, autocomplete, label, placeholder = "" }) {
  return `<span class="passbar${locked ? " disabled" : ""}">
      <input name="password" type="text" required minlength="8" maxlength="128" autocomplete="${autocomplete}"
        aria-label="${label}" ${placeholder ? `placeholder="${placeholder}"` : ""} ${locked ? "disabled" : ""} spellcheck="false">
      <button type="button" class="pass-eye" data-action="toggle-peek" aria-pressed="false" aria-label="隐藏口令" ${locked ? "disabled" : ""}>${EYE_OPEN}</button>
    </span>`;
}

function authFields() {
  const account = escapeHtml(state.authAccount);
  const disabled = !state.live || state.authStatus === "checking" || state.authStatus === "sent" ? "disabled" : "";
  if (state.authTab === "apply") {
    return `<div class="auth-fields">
      <label class="field">登录账号<input name="account" required minlength="3" maxlength="32" pattern="[A-Za-z0-9_.\\-]+" autocomplete="username" value="${account}" ${disabled} placeholder="3–32 位字母、数字或 . _ -"></label>
      <label class="field">姓名<input name="name" required minlength="1" maxlength="64" autocomplete="name" ${disabled} placeholder="真实姓名"></label>
    </div>`;
  }
  if (state.authTab === "activate" || state.authTab === "reset") {
    return `<div class="auth-fields">
      <label class="field">账号<input name="account" required autocomplete="username" value="${account}" ${disabled} placeholder="教师创建的登录账号"></label>
      <label class="field">${state.authTab === "activate" ? "教师提供的一次性激活码" : "教师提供的口令重设码"}<input name="code" required minlength="20" maxlength="256" autocomplete="one-time-code" ${disabled} placeholder="粘贴完整的一次性代码"></label>
    </div>`;
  }
  return `<div class="auth-fields compact">
    <label class="field">账号<input name="account" required minlength="1" maxlength="64" autocomplete="username" value="${account}" ${disabled} placeholder="输入已登记的登录账号"></label>
  </div>`;
}

function renderAdmin() {
  view.innerHTML = `<main class="admin-boundary" aria-labelledby="admin-title">
    <p class="eyebrow">伴星导学 · 管理员会话</p>
    <h1 id="admin-title">此账号仅用于平台管理</h1>
    <p>教师审核与账号管理尚未接入当前工作区。为保护权限范围，管理员账号不会进入教师或学生界面。</p>
    <button class="btn primary" type="button" data-action="logout">安全退出</button>
  </main>`;
}

function renderTeacher() {
  const page = {
    home: teacherHome,
    student: teacherStudent,
    materials: teacherMaterials,
    reports: teacherReports,
  }[state.teacherView];
  view.innerHTML = `
    <div class="app">
      <aside class="side">
        <div class="brand">伴星导学</div>
        ${nav("home", "班级学情")}${nav("student", "学生依据")}${nav("materials", "课程与题目")}${nav("reports", "阶段成果")}
        <div class="spacer"></div>
        <p class="who muted">${escapeHtml(state.who)} · ${escapeHtml(state.className || "教师工作区")}</p>
        <button class="nav-btn" data-action="logout">退出</button>
      </aside>
      <main class="main">${page()}</main>
    </div>
    ${state.drawer ? drawer() : ""}`;
}

function nav(id, label) {
  return `<button class="nav-btn" data-view="${id}" aria-current="${state.teacherView === id ? "page" : "false"}">${label}</button>`;
}

/** 连上后端时的教师首页：全部来自正式接口。 */
function teacherHomeLive() {
  if (state.loading) {
    return `<h1 class="page">正在读取 ${state.className || "班级"}</h1>
      <div class="state-banner">正在从正式服务读取学习记录。</div>`;
  }
  if (state.liveError) {
    return `<h1 class="page">这次没有读全</h1>
      <div class="state-banner bad">${escapeHtml(state.liveError)}</div>
      <button class="btn" data-action="reload">再读一次</button>`;
  }
  const goal = state.liveGoals[0];
  const students = state.liveStudents;
  const scored = students.filter((item) => item.mastery !== null);
  return `
    <div class="top-note">
      <h1 class="page">${escapeHtml(state.className)}</h1>
      <button class="btn primary" data-action="open-drawer">准备学习目标</button>
    </div>
    <p class="muted">${escapeHtml(state.courseName)} · 已连上正式服务，下面的数字来自本机真实记录。</p>
    ${goal ? `<div class="grid two">
      <section class="panel">
        <h3>本轮目标</h3>
        <p>${escapeHtml(goal.title)}</p>
        <p class="muted">${escapeHtml(goal.description || "")}</p>
        <span class="tag ok">${goal.status === "active" ? "进行中" : escapeHtml(goal.status)}</span>
      </section>
      <section class="panel">
        <h3>涉及学生</h3>
        <p>共 ${students.length} 人，其中 ${scored.length} 人已有本轮记录。</p>
        <p class="muted">没有记录的学生显示「待评估」，不会写成 0%。</p>
      </section>
    </div>` : `<div class="state-banner">这个班还没有发布过目标。</div>`}
    <h2 class="section">相关学生</h2>
    <section class="panel">
      ${students.length ? students.map((student) => `
        <article class="student-row">
          <div>
            <strong>${escapeHtml(student.name)}</strong>
            <p class="muted">${escapeHtml(student.note || "还没有本轮作答记录。")}</p>
            <button class="linkish" data-open-student="${escapeHtml(student.id)}">展开依据</button>
          </div>
          <div>
            <div class="pct">${student.mastery === null ? "待评估" : student.mastery + "%"}</div>
            <span class="tag">${escapeHtml(student.status)}</span>
          </div>
        </article>`).join("") : `<p class="muted">这个班还没有学生。</p>`}
    </section>`;
}

function teacherHome() {
  // 连上后端：渲染真实班级与真实记录
  if (state.live) return teacherHomeLive();
  if (state.banner === "empty") {
    return `<h1 class="page">这个班还没有学习记录</h1><p class="lede">先确认课程材料，或给学生发一次目标。</p><button class="btn primary" data-action="open-drawer">准备学习目标</button>`;
  }
  if (state.banner === "loading") {
    return `<h1 class="page">正在整理 ${sample.className}</h1><div class="state-banner">依据已保存，解释还在生成。可以离开，完成后会出现在站内消息。</div>`;
  }
  if (state.banner === "error") {
    return `<h1 class="page">这次整理没有完成</h1><div class="state-banner bad">模型没有返回可用结果。已有作答和成绩仍在，没有用未评分记录补 0 分。</div><button class="btn" data-action="clear-banner">返回已有学情</button>`;
  }
  return `
    <div class="top-note"><h1 class="page">${sample.className}</h1><button class="btn primary" data-action="open-drawer">准备学习目标</button></div>
    <p class="muted">${sample.course} · 虚构样例，数字不是掌握概率。</p>
    <div class="grid two">
      <section class="panel">
        <h3>共同卡住的地方</h3>
        ${sample.issues.map((item, index) => `<article class="issue"><div><strong>${item.title}</strong><p class="muted">${item.detail}</p></div><button class="linkish" data-action="open-drawer" data-issue="${index}">${item.count}</button></article>`).join("")}
      </section>
      <section class="panel">
        <h3>近期变化</h3>
        <p>目标「方程的解与检验」已发布。林夏先看讲解，周予直接做目标题。两人题量都是 3 题。</p>
        <p class="muted">总结截至 11 月 10 日 21:00。之后有 1 份迟交，文字还没重写。</p>
        <button class="btn" data-action="banner" data-banner="loading">查看生成中</button>
        <button class="btn" data-action="banner" data-banner="empty">查看无记录</button>
        <button class="btn" data-action="banner" data-banner="error">查看失败</button>
      </section>
    </div>
    <h2 class="section">相关学生</h2>
    <section class="panel">
      ${sample.students.map((student) => `<article class="student-row"><div><strong>${student.name}</strong><p class="muted">${student.note}</p><button class="linkish" data-open-student="${student.id}">展开依据</button></div><div><div class="pct">${student.mastery}%</div><span class="tag">${student.status}</span></div></article>`).join("")}
    </section>`;
}

function teacherStudent() {
  // 连上后端：显示真实学生与真实依据
  if (state.live) return teacherStudentLive();
  const student = sample.students.find((item) => item.id === (state.openStudent || "lin"));
  return `
    <div class="top-note"><h1 class="page">${student.name}</h1><button class="btn primary" data-action="open-drawer">给${student.name}安排</button></div>
    <div class="grid two">
      <section class="panel">
        <h3>原题与作答</h3>
        <p class="formula">3(x − 2) = 2x + 1</p>
        <p>学生写成 3x − 2 = 2x + 1，再得到 x = 3。</p>
        <p class="muted">帮助：看过一步提示，没看完整解析。这是首次正式作答。</p>
      </section>
      <section class="panel">
        <h3>判断范围</h3>
        <p><span class="pct">${student.mastery}%</span> 综合掌握估计</p>
        <p>${student.evidence}</p>
        <p class="muted">系统计算，不是教师确认值。记录不足时这里会写「待评估」，不会显示 0%。</p>
        <button class="btn" data-action="open-drawer">按此依据出目标</button>
      </section>
    </div>
    <h2 class="section">短评</h2>
    <form class="panel" data-action="comment">
      <label class="field">写给${student.name}<textarea name="comment" required placeholder="只指导，不改答案。"></textarea></label>
      <button class="btn primary" type="submit">发送短评</button>
    </form>`;
}

/** 连上后端时的学生依据页：分数与记录都来自真实评卷。 */
function teacherStudentLive() {
  const id = state.openStudent;
  const student = state.liveStudents.find((item) => item.id === id) || state.liveStudents[0];
  if (!student) {
    return `<h1 class="page">还没有学生</h1><p class="muted">这个班还没有可查看的学生。</p>`;
  }
  const goal = state.liveGoals[0];
  return `
    <div class="top-note">
      <h1 class="page">${escapeHtml(student.name)}</h1>
      <button class="btn primary" data-action="open-drawer">给${escapeHtml(student.name)}安排</button>
    </div>
    <div class="grid two">
      <section class="panel">
        <h3>本轮证据</h3>
        <p>${escapeHtml(student.note || "还没有本轮作答记录。")}</p>
        <p class="muted">${escapeHtml(student.evidence || "记录不足以判断，不显示 0%。")}</p>
        ${student.earned !== undefined
          ? `<p>本轮得分 <strong>${student.earned}</strong> / ${student.possible}（程序核验，可复算）</p>`
          : `<p class="muted">还没有可展示的分数。</p>`}
      </section>
      <section class="panel">
        <h3>判断范围</h3>
        <p><span class="pct">${student.mastery === null ? "待评估" : student.mastery + "%"}</span> 本轮得分比例</p>
        <p class="muted">这是本轮已评分题目的得分比例，不是掌握概率。记录不足时写「待评估」，不显示 0%。</p>
        ${goal ? `<p class="muted">范围：${escapeHtml(goal.title)}</p>` : ""}
        <button class="btn" data-action="open-drawer">按此依据出目标</button>
      </section>
    </div>
    <h2 class="section">短评</h2>
    <form class="panel" data-action="comment">
      <label class="field">写给${escapeHtml(student.name)}<textarea name="comment" required placeholder="只指导，不改答案。"></textarea></label>
      <button class="btn primary" type="submit">发送短评</button>
    </form>`;
}

function teacherMaterials() {
  return `
    <h1 class="page">课程与题目</h1>
    <div class="state-banner warn">扫描页只登记了来源，尚未识别。不能当作已确认材料出题。</div>
    <div class="grid two">
      <section class="panel"><h3>已确认</h3><p>七上 2.2 去括号，页 38–40。锚点可回到原文。</p><span class="tag ok">可用于讲解</span></section>
      <section class="panel"><h3>待核对</h3><p>一题答案与独立求解不一致，已隔离，不再分发。</p><span class="tag bad">暂停使用</span></section>
    </div>`;
}

function teacherReports() {
  return `
    <h1 class="page">阶段成果</h1>
    <p class="muted">四种成果都可以从同一份快照导出。原型只展示入口，不生成文件。</p>
    <div class="grid three">
      ${["班级阶段学情", "学生个人学习", "学习目标执行", "题库质量与使用"].map((name) => `<section class="panel"><h3>${name}</h3><p class="muted">网页阅读已可点开结构。PDF、表格和数据文件使用同一截止时间。</p><button class="btn" data-action="export" data-name="${name}">预览导出</button></section>`).join("")}
    </div>
    <div class="state-banner">11 月 8 日下载的 PDF 仍是旧修订。页面默认展示 11 月 10 日的更正版。</div>`;
}

function drawer() {
  return `
    <div class="drawer-back" data-action="close-drawer"></div>
    <aside class="drawer" role="dialog" aria-labelledby="drawer-title">
      <div class="row" style="justify-content:space-between"><h2 id="drawer-title" class="section" style="margin:0">学习目标</h2><button class="icon-btn" data-action="close-drawer">关闭</button></div>
      <p class="muted">从${state.openStudent ? "学生依据" : "班级问题"}带入。发布后题目和评分规则固定，只能另发。</p>
      <form data-action="publish">
        <label class="field">目标<input name="goal" value="能正确去括号并检验方程的解"></label>
        <label class="field">对象<select name="who"><option>${sample.className} · 18 人</option><option>只给林夏</option></select></label>
        <div class="grid two">
          <label class="field">每人题量<input name="count" value="3"></label>
          <label class="field">共同题比例<input name="ratio" value="1 / 3"></label>
        </div>
        <label class="field">建议<p class="muted">两人题量和难度接近。林夏先看 2.2 讲解，周予直接做含检验的题。预计 18 分钟。</p></label>
        <label class="field"><input type="checkbox" name="late" checked> 截止后允许迟交，迟交会标明</label>
        <label class="field"><input type="checkbox" name="retry"> 截止后允许重做，不改首次成绩</label>
        <div class="state-banner warn">备题还差 1 道已审查的检验题。现在不能发布残缺任务。</div>
        <div class="row"><button class="btn" type="button" data-action="prepare">查看准备状态</button><button class="btn primary" type="submit">确认并等待备齐</button></div>
      </form>
    </aside>`;
}

function studentIcon(name) {
  const paths = {
    tasks: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M9 10h6M9 14h6M9 18h3"/>',
    companion: '<path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-7a8 8 0 1 1 18-3Z"/><path d="M8 11h.01M12 11h.01M16 11h.01"/>',
    mine: '<path d="M4 5.5C7 4 9 4 12 6c3-2 5-2 8-.5V20c-3-1.5-5-1.5-8 .5-3-2-5-2-8-.5ZM12 6v14"/>',
    logout: '<path d="M9 4H5v16h4M14 8l4 4-4 4M9 12h11"/>',
    arrow: '<path d="M5 12h14m-6-6 6 6-6 6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M6 7a7 7 0 0 1 12-1l2 3M4 15l2 3a7 7 0 0 0 12-1"/>',
  };
  return `<svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.tasks}</svg>`;
}

function studentDate(value, withTime = false) {
  if (!value) return "未设截止";
  return new Date(value * 1000).toLocaleString("zh-CN", {
    month: "long", day: "numeric", ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}

function taskTitle(task) {
  const goal = state.liveGoals.find((item) => item.id === task?.goal_id);
  return goal?.title || "学习任务";
}

function taskFinished(task) {
  return ["submitted", "grading", "results_ready", "awaiting_manual", "closed"].includes(task?.status);
}

function taskStatus(task) {
  if (task.diagnostic_required) return "待完成短诊";
  if (task.scheduled_at && task.scheduled_at * 1000 > Date.now()) return "尚未开放";
  return ({ results_ready: "已完成", awaiting_manual: "等待教师评分", closed: "已结束", submitted: "正在评卷", grading: "正在评卷" })[task.status]
    || (Object.keys(task.draft_answers || {}).length ? "作答中" : "待开始");
}

function taskQuestionCount(task) {
  return task.question_count ?? task.questions?.length ?? 0;
}

function studentNav(id, label) {
  const active = state.studentPage === "home" ? state.studentTab === id
    : id === (state.studentPage === "result" ? "mine" : "tasks");
  const count = id === "tasks" ? state.liveTasks.filter((task) => !taskFinished(task)).length : 0;
  return `<button class="student-nav-item" data-tab="${id}" aria-current="${active ? "page" : "false"}">${studentIcon(id)}<span>${label}</span>${count ? `<em>${count}</em>` : ""}</button>`;
}

function renderStudent() {
  const page = state.studentPage;
  const section = page === "workspace" ? "任务作答" : page === "diagnostic" ? "独立短诊"
    : page === "result" ? "任务反馈" : ({ tasks: "我的任务", companion: "学伴", mine: "我的学习" })[state.studentTab];
  const name = state.who || "同学";
  const navItems = studentNav("tasks", "我的任务") + studentNav("companion", "学伴") + studentNav("mine", "我的学习");
  view.innerHTML = `
    <div class="student-app" data-student-shell>
      <aside class="student-sidebar">
        <div class="student-brand"><svg class="student-brand-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true"><path d="M9 34 23 8l15 26M16 24h15" stroke="currentColor" stroke-width="3"/><ellipse cx="24" cy="25" rx="22" ry="8" transform="rotate(-26 24 25)" stroke="currentColor" stroke-width="1.2"/><circle cx="40" cy="16" r="3" fill="currentColor"/></svg><span><strong>伴星导学</strong><small>学习工作台</small></span></div>
        <nav class="student-nav" aria-label="学生导航">${navItems}</nav>
        <div class="student-sidebar-note"><span>每一步，都有记录。</span><p>从独立作答到读懂反馈，<br>把进步留在自己的学习中。</p></div>
        <div class="student-account"><span class="student-avatar" aria-hidden="true">${escapeHtml(Array.from(name)[0])}</span><div><strong>${escapeHtml(name)}</strong><small>学生账号</small></div><button class="student-signout" data-action="logout" aria-label="退出登录" ${state.loggingOut ? "disabled" : ""}>${studentIcon("logout")}</button></div>
      </aside>
      <div class="student-desk">
        <header class="student-topbar"><div class="student-breadcrumb">学习空间 <span>/</span> ${section}</div><div class="student-session"><i></i>${state.loading ? "正在同步" : "正式学习记录"}<time>${new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" })}</time></div></header>
        <div class="student-columns">
          <main class="student-main" id="student-main" tabindex="-1" aria-label="${section}">
            ${page === "workspace" ? workspace() : page === "diagnostic" ? diagnosticPage() : page === "result" ? resultPage() : studentTab()}
          </main>
          <aside class="student-context" aria-label="学习信息">${studentContext()}</aside>
        </div>
      </div>
      <nav class="student-mobile-nav" aria-label="学生快捷导航">${navItems}<button class="student-nav-item" data-action="logout" aria-label="退出登录" ${state.loggingOut ? "disabled" : ""}>${studentIcon("logout")}<span>退出</span></button></nav>
      ${state.sheet ? sheet() : ""}
    </div>`;
}

function studentHeading(title, text, refresh = false) {
  return `<header class="student-page-heading"><div><p class="student-date">${escapeHtml(state.courseName || "自己的节奏，清晰的每一步")}</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(text)}</p></div>${refresh ? `<button class="btn" data-action="reload" ${state.loading ? "disabled" : ""}>${studentIcon("refresh")}刷新任务</button>` : ""}</header>`;
}

function studentTab() {
  if (state.studentTab === "tasks") return tasks();
  if (state.studentTab === "mine") return mine();
  return companion();
}

function studentContext() {
  const task = state.liveTask;
  const completed = state.liveTasks.filter((item) => item.status === "results_ready").length;
  const goal = state.liveGoals.find((item) => item.id === task?.goal_id);
  const workspace = state.studentPage === "workspace";
  const questions = task?.questions || [];
  const answered = workspace ? state.answers.filter((value) => value !== "" && value !== undefined).length : 0;
  return `<section class="student-context-block"><p class="student-context-label">${workspace ? "作答进度" : "学习概况"}</p><h2>${workspace ? `${answered} / ${questions.length} 题` : "让进步看得见"}</h2>
      ${workspace ? `<div class="student-progress-line"><span style="width:${questions.length ? answered / questions.length * 100 : 0}%"></span></div><div class="q-nav" aria-label="题目导航">${questions.map((_, index) => `<button class="chip" data-q="${index}" aria-label="第 ${index + 1} 题${state.answers[index] !== "" && state.answers[index] !== undefined ? "，已作答" : "，未作答"}" aria-pressed="${state.currentQ === index}">${index + 1}</button>`).join("")}</div><p class="student-draft-state" role="status">${state.draftDirty ? "有未同步的选择，请保存进度。" : state.savedAt ? `上次保存于 ${new Date(state.savedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}` : "选择答案后，点击保存进度。"}</p>`
        : `<div class="student-stat-pair"><div><strong>${state.liveTasks.length}</strong><small>已分配任务</small></div><div><strong>${completed}</strong><small>已完成任务</small></div></div>`}
    </section>
    <section class="student-context-block"><p class="student-context-label">${task ? "当前学习目标" : "学习提示"}</p><h2>${escapeHtml(task ? taskTitle(task) : "从一个任务开始")}</h2>${goal?.description ? `<p class="student-context-note">${escapeHtml(goal.description)}</p>` : ""}
      ${task ? `<dl><div><dt>题目数量</dt><dd>${taskQuestionCount(task)} 题</dd></div><div><dt>截止时间</dt><dd>${studentDate(task.deadline_at, true)}</dd></div><div><dt>当前状态</dt><dd>${taskStatus(task)}</dd></div><div><dt>重做</dt><dd>${task.allow_retries ? "教师允许" : "本轮未开放"}</dd></div></dl>` : '<p class="student-context-note">老师分配任务后，会显示在这里。尚无记录时不推算掌握程度。</p>'}
    </section>
    <section class="student-context-block"><p class="student-context-label">学习记录的约定</p><ul class="student-evidence-list"><li>${studentIcon("check")}<span>首次成绩单独保留</span></li><li>${studentIcon("check")}<span>评分完成后开放解析</span></li><li>${studentIcon("check")}<span>提示与独立作答分开记录</span></li></ul><p class="student-context-note">每个判断都来自你的实际记录，记录不足时保持待评估。</p></section>`;
}

function companion() {
  const conversations = state.liveConversations;
  return `${studentHeading("学伴", "从当前任务出发，找到下一步。")}
    <section class="student-companion"><div class="student-companion-heading">${studentIcon("companion")}<h2>今天，想从哪里开始？</h2><p>先独立尝试，再沿着具体反馈继续学习。</p></div>
      <div class="student-suggestion-grid"><button class="student-suggestion" data-tab="tasks">${studentIcon("tasks")}<strong>继续我的任务</strong><small>回到老师分配的练习与保存进度</small></button><button class="student-suggestion" data-tab="mine">${studentIcon("mine")}<strong>回顾作答反馈</strong><small>看清每道题的作答与评分依据</small></button></div>
      <p class="student-compose-note">实时学伴对话尚未在此工作台开放。已有对话可以在下方阅读，任务作答与成绩反馈正常使用。</p>
    </section>
    <div class="student-section-head"><h2>我的对话记录</h2><small>${conversations.length} 个会话</small></div>
    ${state.conversationError ? `<div class="state-banner warn">${escapeHtml(state.conversationError)}</div>` : conversations.length ? `<div class="student-task-list">${conversations.map((item) => `<article class="student-task-row"><span class="student-task-state">${studentIcon("companion")}</span><div class="student-task-copy"><h3>${escapeHtml(item.title || "学习对话")}</h3><p>${item.updated_at ? studentDate(item.updated_at, true) : "已保存的对话"}</p></div><button class="btn" data-conversation="${escapeHtml(item.id)}">阅读记录</button></article>`).join("")}</div>` : '<section class="student-empty"><h2>还没有对话记录</h2><p>先从一份任务开始，学习记录会逐步积累。</p></section>'}`;
}

function tasks() {
  // 连上后端：展示真实任务
  if (state.live) return tasksLive();
  return `
    <div class="phone-body">
      <h1 class="page" style="font-size:32px">任务</h1>
      <section class="panel">
        <h3>方程的解与检验</h3>
        <p class="muted">教师发布 · 3 题 · 今晚 21:30 截止 · 首次成绩单独保留</p>
        <button class="btn primary" data-action="open-task">${state.submitted ? "查看作答" : "继续"}</button>
      </section>
      <section class="panel">
        <h3>自主练习</h3>
        <p class="muted">来自学伴，不计教师任务分。</p>
        <button class="btn" data-action="practice">开始</button>
      </section>
    </div>`;
}

/** 连上后端时的任务页。 */
function studentTaskButton(task, primary = false) {
  const upcoming = task.scheduled_at && task.scheduled_at * 1000 > Date.now();
  const label = upcoming ? "等待开放" : task.diagnostic_required ? "开始短诊" : taskFinished(task) ? "查看结果"
    : state.liveLease && state.liveTask?.id === task.id || Object.keys(task.draft_answers || {}).length ? "继续作答" : "开始作答";
  return `<button class="btn${primary ? " primary" : ""}" data-action="open-task" data-task-id="${escapeHtml(task.id)}" ${state.loading || upcoming ? "disabled" : ""}>${label}${primary ? studentIcon("arrow") : ""}</button>`;
}

function tasksLive() {
  const heading = studentHeading(`你好，${state.who || "同学"}`, "从今天的一小步开始，把每一次尝试留在学习里。", true);
  if (state.loading && !state.liveTasks.length) return `${heading}<div class="state-banner" role="status">正在读取你的任务。</div>`;
  if (state.liveError) return `${heading}<div class="state-banner bad" role="alert">${escapeHtml(state.liveError)}</div><button class="btn" data-action="reload">再读一次</button>`;
  const task = state.liveTask;
  if (!task) return `${heading}<section class="student-empty">${studentIcon("tasks")}<h2>学习，从下一份任务开始</h2><p>老师还没有分配任务。任务发布后会出现在这里。</p><button class="btn" data-action="reload">刷新任务</button></section>`;
  const finished = taskFinished(task);
  const first = task.attempts?.find((attempt) => attempt.kind === "first");
  const tasks = state.liveTasks.filter((item) => state.taskFilter === "all" || (state.taskFilter === "done" ? taskFinished(item) : !taskFinished(item)));
  return `${heading}
    <section class="student-focus" aria-label="当前任务">
      <svg class="student-orbit" viewBox="0 0 300 240" fill="none" aria-hidden="true"><ellipse cx="150" cy="120" rx="120" ry="55" transform="rotate(-35 150 120)"/><ellipse cx="150" cy="120" rx="120" ry="55" transform="rotate(35 150 120)"/><circle cx="150" cy="120" r="68"/><circle cx="242" cy="66" r="6"/><path d="M130 142 150 96l21 46m-33-16h25"/></svg>
      <div class="student-focus-content"><div class="student-task-kicker"><span class="tag${finished ? " ok" : ""}">${taskStatus(task)}</span><span>${finished ? "回顾这次学习" : "接下来，专心完成这一份"}</span></div>
        <h2>${escapeHtml(taskTitle(task))}</h2><p>${task.diagnostic_required ? "先完成一次独立短诊，再开始正式作答。短诊不计入本轮成绩。" : finished ? "每次作答都留下记录。读懂反馈，再决定下一步。" : "先自己尝试，遇到困难再停下来思考。作答进度可以保存。"}</p>
        <div class="student-task-meta"><span>${taskQuestionCount(task)} 道题</span><span>${studentIcon("clock")}${studentDate(task.deadline_at, true)}</span><span>${first?.total_score !== undefined ? `首次成绩 ${first.total_score} / ${first.max_score}` : "首次成绩单独保留"}</span></div>
        <div class="student-focus-actions">${studentTaskButton(task, true)}</div>
      </div>
    </section>
    <div class="student-section-head"><h2>我的任务</h2><div class="row" role="group" aria-label="任务筛选">${[["all", "全部"], ["todo", "待完成"], ["done", "已交卷"]].map(([id, label]) => `<button class="chip" data-task-filter="${id}" aria-pressed="${state.taskFilter === id}">${label}</button>`).join("")}</div></div>
    <div class="student-task-list">${tasks.length ? tasks.map((item) => `<article class="student-task-row" data-selected="${task.id === item.id}"><span class="student-task-state">${studentIcon(taskFinished(item) ? "check" : "tasks")}</span><div class="student-task-copy"><h3>${escapeHtml(taskTitle(item))}</h3><p>${taskQuestionCount(item)} 道题 · ${studentDate(item.deadline_at, true)}</p></div><div class="student-task-side"><strong>${taskStatus(item)}</strong><small>${item.allow_retries ? "可在评分后重做" : "首次作答单独记录"}</small></div>${studentTaskButton(item)}</article>`).join("") : '<section class="student-empty"><h2>这个分类暂时没有任务</h2><p>可以切换到“全部”查看其他任务。</p></section>'}</div>`;
}

function mine() {
  const records = state.liveTasks.flatMap((task) => (task.attempts || []).map((attempt) => ({ task, attempt })))
    .sort((a, b) => (b.attempt.submitted_at || 0) - (a.attempt.submitted_at || 0));
  return `${studentHeading("我的学习", "先看具体的作答与反馈，让每一步进步都有来处。")}
    ${state.liveError ? `<div class="state-banner bad">${escapeHtml(state.liveError)}</div>` : ""}
    <div class="student-section-head"><h2>作答记录</h2><small>${records.length} 次真实提交</small></div>
    ${records.length ? `<div class="student-record-list">${records.map(({ task, attempt }) => `<article><div><span class="tag${attempt.status === "graded" ? " ok" : " warn"}">${attempt.kind === "first" ? "首次作答" : "重做"}</span><h3>${escapeHtml(taskTitle(task))}</h3><p>${studentDate(attempt.submitted_at, true)} · ${attempt.independent_evidence ? "独立作答记录" : "复习作答记录"}</p></div><div><strong>${attempt.total_score !== undefined ? `${attempt.total_score} / ${attempt.max_score}` : "等待评分"}</strong><p>${attempt.status === "graded" ? "已完成评分" : "完整评分后开放反馈"}</p></div><button class="btn" data-action="open-task" data-task-id="${escapeHtml(task.id)}">查看反馈</button></article>`).join("")}</div>` : '<section class="student-empty">' + studentIcon("mine") + '<h2>第一份学习记录，等你写下</h2><p>完成任务并交卷后，首次成绩、重做与具体反馈会保存在这里。</p><button class="btn primary" data-tab="tasks">去看我的任务</button></section>'}
    <section class="student-context-block"><h2>比一个百分比，多看一步</h2><p class="student-context-note">是否独立完成、在哪里需要提示、哪些步骤还不熟悉，都要结合具体作答判断。记录不足时，不生成掌握度。</p></section>`;
}

function workspace() {
  if (state.live) return workspaceLive();
  const prompts = [
    "3(x − 2) = 2x + 1。先写出去括号后的式子。",
    "把含 x 的项移到一边后，系数是多少？",
    "你的解代回原式，左边和右边各是多少？",
  ];
  const locked = state.banner === "takeover";
  return `
    <div class="phone-body">
      <div class="row" style="justify-content:space-between"><span class="tag">${state.mode === "focus" ? "专注作答" : "辅导"}</span><button class="btn" data-action="toggle-mode">${state.mode === "focus" ? "打开辅导" : "只看作答"}</button></div>
      <div class="q-nav">${prompts.map((_, index) => `<button class="chip" data-q="${index}" aria-pressed="${state.currentQ === index}">${index + 1}${state.answers[index] ? "" : " · 未答"}</button>`).join("")}</div>
      <p class="formula">${prompts[state.currentQ]}</p>
      ${locked ? '<div class="state-banner bad">另一台设备正在作答。这里改为只读，最后保存于 20:41。</div>' : ""}
      ${state.banner === "save-fail" ? '<div class="state-banner bad">没有保存成功。输入还在页面上，请再试一次。</div>' : ""}
      <form data-action="answer">
        <label class="field">这一题的作答<textarea name="answer" ${locked ? "readonly" : ""}>${escapeHtml(state.answers[state.currentQ])}</textarea></label>
        <div class="row">
          <button class="btn" type="button" data-action="formula">公式</button>
          <button class="btn" type="button" data-action="voice">语音</button>
          <button class="btn primary" type="submit" ${locked ? "disabled" : ""}>保存这题</button>
        </div>
      </form>
      ${state.mode === "tutor" ? '<div class="bubble">先自己写一步。需要时再要提示，提示不扣这次成绩。</div><button class="btn" data-action="hint">要一步提示</button>' : ""}
      <div class="row" style="margin-top:16px"><button class="btn primary" data-action="submit" ${locked ? "disabled" : ""}>交卷</button></div>
    </div>`;
}

function resultPage() {
  if (state.live) return resultPageLive();
  const waiting = state.banner === "awaiting";
  return `
    <div class="phone-body">
      <h1 class="page" style="font-size:32px">本轮结果</h1>
      ${waiting ? '<div class="state-banner warn">第 2 题等待教师评分。全部完成前不显示对错和解析。</div>' : `
        <section class="panel"><h3>首次成绩 2 / 3</h3><p>第 1 题漏乘。第 3 题检验正确。重做不会改这个成绩。</p></section>
        <section class="panel"><h3>第 1 题</h3><p class="formula">3(x − 2) = 3x − 6</p><p class="muted">${state.seenSolution ? "你已看过解析，再次作答不再当作新的独立依据。" : "解析在交卷后才开放。"}</p></section>`}
      <button class="btn" data-action="retry">再做一次</button>
    </div>`;
}

/** 连上后端时的作答区：题目、选项、保存、交卷都是真的。 */
function questionChoices(question, chosen, locked, diagnostic = false) {
  if (!(question.options || []).length) return `<label class="field">${diagnostic ? "独立作答" : "这一题的作答"}<textarea data-answer-input="${diagnostic ? "diagnostic" : "task"}" rows="5" ${locked ? "readonly" : ""}>${escapeHtml(chosen ?? "")}</textarea></label>`;
  return `<div class="options" role="group" aria-label="选择答案">${question.options.map((option, index) => `<button class="option" ${diagnostic ? "data-diagnostic-choose" : "data-choose"}="${index}" aria-pressed="${chosen === index}" ${locked ? "disabled" : ""}><b>${String.fromCharCode(65 + index)}</b><span>${escapeHtml(option)}</span></button>`).join("")}</div>`;
}

function workspaceLive() {
  const task = state.liveTask;
  const questions = task?.questions || [];
  if (!questions.length) return `${studentHeading("任务作答", "正式卷还没有开放。")}
    <div class="state-banner warn">${escapeHtml(task?.notice || "没有可作答的题目，请返回任务列表。")}</div><button class="btn" data-tab="tasks">返回任务</button>`;
  const locked = state.banner === "takeover" || state.loading;
  const current = Math.min(state.currentQ, questions.length - 1);
  const question = questions[current];
  return `<div class="student-workspace">
      <header class="student-workspace-head"><button class="btn" data-tab="tasks">返回任务</button><span class="tag">${state.mode === "focus" ? "专注作答" : "独立尝试"}</span><button class="linkish" data-action="toggle-mode">${state.mode === "focus" ? "显示学习提示" : "进入专注模式"}</button></header>
      ${studentHeading(taskTitle(task), "按自己的节奏作答，保存后可以下次继续。")}
      <section class="student-question-card" aria-label="第 ${current + 1} 题"><div class="student-question-meta"><span>第 ${current + 1} 题 / 共 ${questions.length} 题</span><span>${question.points ?? "—"} 分</span></div><h2 class="formula">${escapeHtml(question.stem)}</h2>
        ${state.banner === "takeover" ? '<div class="state-banner bad" role="alert">另一台设备正在作答，当前为只读。已输入的选择仍保留在这里。</div>' : ""}
        ${state.banner === "save-fail" ? '<div class="state-banner bad" role="alert">没有保存成功，当前答案仍在页面上，请重新保存。</div>' : ""}
        ${questionChoices(question, state.answers[current], locked)}
        <div class="student-question-footer"><div class="row"><button class="btn" data-q="${current - 1}" ${current === 0 || state.loading ? "disabled" : ""}>上一题</button><button class="btn" data-q="${current + 1}" ${current === questions.length - 1 || state.loading ? "disabled" : ""}>下一题</button></div><span class="student-draft-state">${state.draftDirty ? "有未保存的选择" : "当前进度已同步"}</span></div>
      </section>
      ${state.mode === "tutor" ? '<p class="student-compose-note">先完成自己能确定的步骤。交卷并完成评分后，可以逐题回看反馈与解析。</p>' : ""}
      <div class="student-focus-actions"><button class="btn" data-action="save-draft" ${locked ? "disabled" : ""}>${state.loading ? "正在处理" : "保存进度"}</button><button class="btn primary" data-action="submit" ${locked ? "disabled" : ""}>确认交卷</button></div>
    </div>`;
}

function diagnosticPage() {
  const diagnostic = state.diagnostic;
  const questions = diagnostic?.questions || [];
  const current = Math.min(state.currentQ, Math.max(0, questions.length - 1));
  const question = questions[current];
  return `${studentHeading("先做一次独立短诊", "不计入任务成绩，也不会自动替你选择答案。完成后开放正式卷。")}
    ${question ? `<div class="q-nav" aria-label="短诊题目">${questions.map((_, index) => `<button class="chip" data-q="${index}" aria-pressed="${current === index}">${index + 1}${state.diagnosticAnswers[index] !== "" && state.diagnosticAnswers[index] !== undefined ? " · 已答" : ""}</button>`).join("")}</div><section class="student-question-card"><div class="student-question-meta"><span>第 ${current + 1} / ${questions.length} 题</span><span>独立短诊</span></div><h2 class="formula">${escapeHtml(question.stem)}</h2>${questionChoices(question, state.diagnosticAnswers[current], state.loading, true)}</section><div class="student-focus-actions"><button class="btn" data-tab="tasks">返回任务</button><button class="btn primary" data-action="submit-diagnostic" ${state.loading ? "disabled" : ""}>${state.loading ? "正在提交" : "提交短诊"}</button></div>` : '<div class="state-banner warn">暂时没有短诊题目，请返回任务后重试。</div><button class="btn" data-tab="tasks">返回任务</button>'}`;
}

/** 连上后端时的结果页：分数与逐题对错都来自真实评卷。 */
function resultPageLive() {
  const task = state.liveTask;
  const attempts = task?.attempts || [];
  const attempt = attempts.find((item) => item.id === state.selectedAttemptId) || attempts[attempts.length - 1];
  const heading = `<div class="student-workspace-head"><button class="btn" data-tab="tasks">返回任务</button><span class="tag">${taskStatus(task || {})}</span></div>${studentHeading(task ? taskTitle(task) : "本轮结果", "把每次作答和具体反馈放在一起看。")}`;
  if (!task || !attempt) return `${heading}<div class="state-banner warn">还在评卷，成绩稍后出现在这里。</div><button class="btn" data-action="refresh-task" ${state.loading ? "disabled" : ""}>刷新结果</button>`;
  const summary = `<section class="student-result-summary"><div class="student-score">${attempt.total_score ?? "—"}<small> / ${attempt.max_score ?? "—"}</small></div><div><h2>${attempt.kind === "first" ? "首次成绩" : "重做成绩"}</h2><p>${attempt.independent_evidence ? "独立作答与提示记录分开保留。" : "这次是复习记录，首次成绩保持不变。"}</p><small>${studentDate(attempt.submitted_at, true)}</small></div></section>`;
  if (!task.answer_released) return `${heading}${summary}<div class="state-banner warn">整轮评分完成前，不显示对错和解析。</div><button class="btn" data-action="refresh-task" ${state.loading ? "disabled" : ""}>刷新结果</button>`;
  const byId = Object.fromEntries((attempt.items || []).map((item) => [item.question_id, item]));
  return `${heading}${summary}<div class="student-section-head"><h2>逐题反馈</h2><small>${task.questions?.length || 0} 道题</small></div><div class="student-result-list">${(task.questions || []).map((question, index) => {
    const item = byId[question.id] || {};
    const yours = attempt.answers?.[question.id];
    const answerText = (value) => value === undefined ? "未作答" : question.options?.[value] ?? String(value);
    return `<section class="panel"><div class="student-result-heading"><h3>第 ${index + 1} 题</h3><span class="tag${item.score === undefined ? " warn" : item.score >= question.points ? " ok" : ""}">${item.score === undefined ? "待评分" : `${item.score} / ${question.points} 分`}</span></div><p class="formula">${escapeHtml(question.stem)}</p><p class="muted">你的作答：${escapeHtml(answerText(yours))}</p>${question.answer !== undefined ? `<p>参考答案：${escapeHtml(answerText(question.answer))}</p>` : ""}${item.result?.why ? `<p class="muted">判定：${escapeHtml(item.result.why)}</p>` : ""}${question.explanation ? `<p>${escapeHtml(question.explanation)}</p>` : ""}</section>`;
  }).join("")}</div>${task.status === "results_ready" && task.allow_retries ? '<button class="btn" data-action="retry">再做一次，保留首次成绩</button>' : ""}`;
}

function sheet() {
  const sheets = {
    conversation: `<h2 class="section" id="student-sheet-title">${escapeHtml(state.activeConversation?.title || "学习对话")}</h2><div class="student-conversation">${state.liveMessages.length ? state.liveMessages.map((message) => `<article class="bubble${message.role === "user" ? " me" : ""}"><strong>${message.role === "user" ? "我" : "学伴"}</strong><p>${escapeHtml(message.redacted ? message.notice || "这条内容暂未开放。" : message.content || message.text || "暂无内容")}</p></article>`).join("") : '<p class="muted">这段会话还没有消息。</p>'}</div>`,
    chats: `<h2 class="section">会话</h2>${state.conversations.map((item, index) => `<button class="btn" data-chat="${index}">${item}</button>`).join("")}<form class="field" data-action="new-chat"><input name="title" placeholder="新会话名称" aria-label="新会话名称"><button class="btn" type="submit">新建</button></form>`,
    formula: `<h2 class="section">插入公式</h2><div class="row"><button class="chip" data-insert="a/b">分数</button><button class="chip" data-insert="x^2">乘方</button><button class="chip" data-insert="√x">根号</button></div><p class="muted">插入后仍需你确认，不会直接交卷。</p>`,
    voice: `<h2 class="section">语音转成文字</h2><p>听到的是：二分之一的平方。</p><label class="field">确认后发送<input value="(1/2)^2"></label><button class="btn primary" data-action="confirm-voice">确认文字</button>`,
    explain: `<h2 class="section">去括号</h2><p>3 要同时乘 x 和 −2，所以是 3x − 6。这是讲解，不是这道未交题目的完整评分。</p>`,
    export: `<h2 class="section">${escapeHtml(state.exportName || "导出")}</h2><p>范围：${sample.className}，截止 11 月 10 日 21:00。原型不下载文件。</p><div class="row"><span class="tag">网页</span><span class="tag">PDF</span><span class="tag">Excel</span><span class="tag">JSON</span><span class="tag">CSV</span></div>`,
  };
  return `<div class="drawer-back" data-action="close-sheet" aria-hidden="true"></div><section class="sheet" role="dialog" aria-modal="true" aria-label="${state.sheet === "conversation" ? "学习对话记录" : "学习工具"}" tabindex="-1">${sheets[state.sheet] || ""}<div class="row"><button class="btn" data-action="close-sheet">关闭</button></div></section>`;
}

function stateMenu() {
  const items = state.role === "teacher"
    ? [["", "正常学情"], ["empty", "无记录"], ["loading", "整理中"], ["error", "整理失败"]]
    : [["", "正常"], ["quota", "额度用完"], ["save-fail", "保存失败"], ["takeover", "另一台作答"], ["awaiting", "待教师评分"]];
  return `<details class="proto-states"><summary class="btn">原型状态</summary><div class="panel">${items.map(([id, label]) => `<button class="btn" type="button" data-state="${id}" aria-pressed="${state.banner === id}">${label}</button>`).join("")}<p class="muted">只切换界面，不改变虚构样例的含义。</p></div></details>`;
}

function bind() {
  view.querySelectorAll("[data-action]:not(form)").forEach((node) => node.addEventListener("click", onAction));
  view.querySelectorAll('[data-auth-mode="apply"]').forEach((node) => node.addEventListener("click", () => {
    if (state.authStatus === "checking") return;
    state.authRole = "teacher";
    state.authTab = "apply";
    state.authAccount = "";
    state.authStatus = "ready";
    state.error = "";
    render();
  }));
  view.querySelectorAll("[data-role]").forEach((node) => node.addEventListener("click", () => {
    if (state.authStatus === "checking") return;
    state.authRole = node.dataset.role;
    state.authTab = "login";
    state.authAccount = "";
    state.authStatus = "ready";
    state.error = "";
    render();
  }));
  view.querySelectorAll("[data-tab]").forEach((node) => node.addEventListener("click", () => {
    if (state.role === "student") {
      if (state.loading || state.loggingOut || state.transition) return;
      state.sheet = null;
      state.studentTab = node.dataset.tab;
      state.studentPage = "home";
    } else {
      if (state.authStatus === "checking") return;
      state.authTab = node.dataset.tab;
      if (state.authTab === "activate") state.authRole = "student";
      if (state.authTab === "apply") state.authRole = "teacher";
      state.error = "";
      state.authStatus = "ready";
    }
    render();
  }));
  view.querySelectorAll('.auth-fields input[name="account"]').forEach((input) => input.addEventListener("input", () => {
    state.authAccount = input.value.trim();
    const label = view.querySelector(".auth-name b");
    const marker = view.querySelector(".auth-rings .ring:nth-child(2) i");
    const monitorAccount = view.querySelector(".mon-account");
    if (label) label.textContent = state.authAccount || "等待身份核验";
    if (marker) marker.classList.toggle("on", Boolean(state.authAccount));
    if (monitorAccount) monitorAccount.textContent = state.authAccount || "—";
  }));
  view.querySelectorAll("[data-view]").forEach((node) => node.addEventListener("click", () => {
    state.teacherView = node.dataset.view;
    render();
  }));
  view.querySelectorAll("[data-open-student]").forEach((node) => node.addEventListener("click", () => {
    state.openStudent = node.dataset.openStudent;
    state.teacherView = "student";
    render();
  }));
  view.querySelectorAll("[data-q]").forEach((node) => node.addEventListener("click", () => {
    if (state.loading || node.disabled) return;
    state.currentQ = Math.max(0, Number(node.dataset.q));
    render();
  }));
  view.querySelectorAll("[data-task-filter]").forEach((node) => node.addEventListener("click", () => {
    state.taskFilter = node.dataset.taskFilter;
    render();
  }));
  view.querySelectorAll("[data-conversation]").forEach((node) => node.addEventListener("click", () => {
    readConversation(node.dataset.conversation);
  }));
  // 选中先保留在本页，保存和交卷仍由正式接口确认。
  view.querySelectorAll("[data-choose]").forEach((node) => node.addEventListener("click", () => {
    if (state.loading || state.banner === "takeover") return;
    state.answers[state.currentQ] = Number(node.dataset.choose);
    rememberStudentDraft();
    render();
  }));
  view.querySelectorAll("[data-diagnostic-choose]").forEach((node) => node.addEventListener("click", () => {
    if (state.loading) return;
    state.diagnosticAnswers[state.currentQ] = Number(node.dataset.diagnosticChoose);
    render();
  }));
  view.querySelectorAll("[data-answer-input]").forEach((node) => node.addEventListener("input", () => {
    if (state.loading) return;
    if (node.dataset.answerInput === "diagnostic") state.diagnosticAnswers[state.currentQ] = node.value;
    else {
      state.answers[state.currentQ] = node.value;
      rememberStudentDraft();
      view.querySelectorAll(".student-draft-state").forEach((status) => { status.textContent = "有未保存的作答"; });
    }
  }));
  view.querySelectorAll("[data-state]").forEach((node) => node.addEventListener("click", () => {
    state.banner = node.dataset.state;
    if (state.banner === "save-fail" || state.banner === "takeover") state.studentPage = "workspace";
    if (state.banner === "awaiting") {
      state.submitted = true;
      state.studentPage = "result";
    }
    if (state.banner === "quota") {
      state.studentTab = "companion";
      state.studentPage = "home";
    }
    render();
  }));
  view.querySelectorAll("[data-insert]").forEach((node) => node.addEventListener("click", () => {
    state.answers[state.currentQ] += node.dataset.insert;
    state.sheet = null;
    render();
  }));
  view.querySelectorAll("[data-chat]").forEach((node) => node.addEventListener("click", () => {
    state.activeChat = Number(node.dataset.chat);
    state.sheet = null;
    state.studentTab = "companion";
    render();
  }));
  view.querySelectorAll("form").forEach((form) => form.addEventListener("submit", onSubmit));
  // 眼睛按钮：明文与方块遮蔽之间切换。只改类名与图标，不重渲染，输入不会丢。
  view.querySelectorAll('[data-action="toggle-peek"]').forEach((node) => node.addEventListener("click", () => {
    const bar = node.closest(".passbar");
    const input = bar ? bar.querySelector("input") : null;
    if (!input || input.disabled) return;
    const masked = !bar.classList.contains("masked");
    bar.classList.toggle("masked", masked);
    node.setAttribute("aria-pressed", String(masked));
    node.setAttribute("aria-label", masked ? "显示口令" : "隐藏口令");
    node.innerHTML = masked ? EYE_CLOSED : EYE_OPEN;
  }));
}

async function finishLogout() {
  const generation = ++state.transitionId;
  workspaceRequest += 1;
  clearInterval(leaseTimer);
  leaseTimer = 0;
  cancelAnimationFrame(revealTimer);
  drag = null;
  touchY = null;
  const reveal = state.reduce ? 1 : 0;
  Object.assign(state, {
    screen: "entry", shifted: reveal > 0.5, reveal, authStatus: "ready",
    authAccount: "", authTab: "login", role: null, who: "", drawer: false, sheet: null, error: "",
    transition: state.reduce ? "" : "returning", loggingOut: false,
    studentTab: "tasks", studentPage: "home", taskFilter: "all", currentQ: 0,
    liveTasks: [], liveTask: null, liveLease: null, liveGoals: [], liveStudents: [],
    liveConversations: [], liveMessages: [], activeConversation: null, conversationError: "", studentDrafts: {},
    conversations: [], activeChat: 0, openStudent: null, teacherView: "home", goalLabel: "", exportName: "",
    liveReportFormats: [], liveError: "", loading: false, courseId: null, courseName: "", classId: null, className: "",
    diagnostic: null, diagnosticAnswers: [], answers: [], submitted: false, seenSolution: false,
    draftDirty: false, savedAt: null, selectedAttemptId: null, banner: "", mode: "tutor",
    liveReport: null, railEvents: [], railLabel: "入口 · 正式服务已连接", step: 0,
    anchorYaw: 0.40, anchorPitch: 0.17, anchorSpin: 0,
  });
  resetEvents(["会话已结束", "等待账号口令"]);
  mountScene();
  if (scene) scene.reset({ reveal });
  render();
  window.scrollTo(0, 0);
  if (scene) {
    scene.resume();
    if (!state.reduce) await scene.playReassemble(1000);
  }
  if (generation !== state.transitionId) return;
  state.transition = "";
  if (state.reduce) {
    state.reveal = 1;
    state.shifted = true;
    if (scene) scene.reset({ reveal: 1 });
  }
  if (scene) scene.setPull(state.reveal);
  render();
  const target = view.querySelector(state.reduce ? 'input[name="account"]' : '[data-action="reveal"]');
  target?.focus({ preventScroll: true });
}

function onAction(event) {
  const action = event.currentTarget.dataset.action;
  if (action === "toggle-peek") return; // 由 bind 里的专用处理器接管，避免整屏重渲染丢输入
  if (action === "reveal") {
    animateReveal(1);
    return;
  }
  if (action === "logout") {
    if (state.loggingOut || state.transition) return;
    state.loggingOut = true;
    render();
    if (state.live) {
      api.logout().then(() => finishLogout()).catch((error) => {
        state.loggingOut = false;
        toast(error && error.message ? error.message : "未能结束服务端会话，请重试");
        render();
      });
    } else {
      finishLogout();
    }
    return;
  }
  if (action === "open-drawer") state.drawer = true;
  if (action === "close-drawer") state.drawer = false;
  if (action === "reload") {
    loadWorkspace();
    return;
  }
  if (action === "prepare") toast("还差 1 道检验题，备齐前不会发布");
  if (action === "banner") state.banner = event.currentTarget.dataset.banner;
  if (action === "clear-banner") state.banner = "";
  if (action === "export") {
    state.sheet = "export";
    state.exportName = event.currentTarget.dataset.name;
  }
  if (action === "chats") state.sheet = "chats";
  if (action === "formula") state.sheet = "formula";
  if (action === "voice") state.sheet = "voice";
  if (action === "explain" || action === "find") state.sheet = "explain";
  if (action === "close-sheet") state.sheet = null;
  if (action === "practice") {
    state.studentPage = "workspace";
    state.sheet = null;
  }
  if (action === "open-task") {
    if (state.live) {
      selectLiveTask(event.currentTarget.dataset.taskId || state.liveTask?.id);
      return;
    }
    state.studentPage = "workspace";
    state.sheet = null;
  }
  if (action === "open-diagnostic") {
    if (state.live) { runDiagnostic(); return; }
    state.studentPage = "workspace";
  }
  if (action === "submit-diagnostic") {
    submitLiveDiagnostic();
    return;
  }
  if (action === "save-draft") {
    if (state.live) { saveLiveDraft(); return; }
    state.sheet = null;
  }
  if (action === "refresh-task") {
    if (state.live) { refreshStudentTask(); return; }
    render();
  }
  if (action === "toggle-mode") state.mode = state.mode === "focus" ? "tutor" : "focus";
  if (action === "hint") toast("先写出括号外的数乘到了哪几项");
  if (action === "toggle-mastery") state.banner = state.banner === "mastery" ? "" : "mastery";
  if (action === "confirm-voice") {
    state.sheet = null;
    toast("已确认文字，尚未发送");
  }
  if (action === "submit") submitPaper();
  if (action === "retry") {
    if (state.live) { retryLiveTask(); return; }
    state.submitted = false;
    state.seenSolution = true;
    state.studentPage = "workspace";
    toast("重做保留首次成绩");
  }
  if (!["submit"].includes(action)) render();
}

function onSubmit(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const action = form.dataset.action;
  const data = new FormData(form);
  if (action === "auth") return submitAuth(data);
  if (action === "publish") {
    state.drawer = false;
    toast("已记下目标，等检验题备齐再发布");
    render();
    return;
  }
  if (action === "comment") {
    toast("短评已发送，没有改动学生答案");
    form.reset();
    return;
  }
  if (action === "answer") {
    if (state.banner === "save-fail") {
      toast("仍未保存");
      return;
    }
    state.answers[state.currentQ] = String(data.get("answer") || "");
    toast("这题已保存在原型里");
    render();
    return;
  }
  if (action === "send") {
    if (state.banner === "quota") return;
    toast("原型不会调用模型");
    form.reset();
    return;
  }
  if (action === "new-chat") {
    const title = String(data.get("title") || "").trim();
    if (!title) return;
    state.conversations.unshift(title);
    state.sheet = null;
    state.studentTab = "companion";
    render();
  }
}

async function enterWorkspace(role, displayName, { live, message }) {
  const generation = ++state.transitionId;
  cancelAnimationFrame(revealTimer);
  drag = null;
  touchY = null;
  state.authStatus = "checking";
  state.role = role;
  state.who = displayName;
  state.step = 3;
  state.live = live;
  state.transition = "departing";
  state.studentTab = "tasks";
  state.studentPage = "home";
  state.liveError = "";
  resetEvents(["身份已核验", "正式会话已开启"]);
  state.railLabel = "入口 · 正式会话";
  render();
  if (scene) {
    scene.setVerify(0);
    scene.setDenied(0);
    scene.resume();
    await scene.playDissolve(state.reduce ? 0 : 1500);
  } else if (!state.reduce) {
    const fallback = view.querySelector("svg.fallback");
    if (fallback?.animate) await fallback.animate([
      { opacity: 0.85, transform: "scale(1)" },
      { opacity: 0, transform: "scale(1.04)" },
    ], { duration: 240, easing: "cubic-bezier(0.23, 1, 0.32, 1)", fill: "forwards" }).finished.catch(() => {});
  }
  if (generation !== state.transitionId || state.screen !== "entry") return;
  state.screen = "app";
  state.transition = "";
  state.authStatus = "ready";
  state.banner = "";
  if (scene) scene.stop();
  toast(message);
  render();
  window.scrollTo(0, 0);
  view.querySelector("#student-main")?.focus({ preventScroll: true });
  if (live) loadWorkspace();
}

function failAuth(message) {
  state.authStatus = "denied";
  state.error = message;
  state.attempts += 1;
  resetEvents(["身份核验未通过", "请检查后重试"]);
  state.railLabel = "入口 · 需要检查";
  state.step = 4;
  if (scene) scene.setDenied(1);
  render();
  window.setTimeout(() => { if (scene) scene.setDenied(0); }, 900);
}

function submitAuth(data) {
  if (state.authStatus === "checking" || state.authStatus === "sent") return;
  if (!state.live) return failAuth(state.serviceError || "无法连接正式认证服务，本次未创建账号或会话。");

  const account = String(data.get("account") || "").trim();
  const name = String(data.get("name") || "").trim();
  const password = String(data.get("password") || "");
  const code = String(data.get("code") || "").trim();
  const tab = state.authTab;
  const role = state.authRole;
  state.authAccount = account || state.authAccount;
  state.authStatus = "checking";
  state.error = "";
  resetEvents(["正在核验身份", "等待正式服务"]);
  state.railLabel = "入口 · 核验中";
  state.step = 2;
  if (scene) scene.setVerify(1);
  render();

  (async () => {
    try {
      if (tab === "apply") {
        const result = await api.applyTeacherAccount({ username: account, display_name: name, password });
        state.authStatus = "sent";
        state.error = "";
        resetEvents(["账号已建立", "等待管理员审核"]);
        state.railLabel = "教师申领 · 待审核";
        state.step = 3;
        if (scene) scene.setVerify(0);
        render();
        toast(result.message || "教师账号已提交，审核通过后即可登录");
        return;
      }
      if (tab === "activate" || tab === "reset") {
        const activating = tab === "activate";
        if (activating) await api.activateAccount(code, password);
        else await api.resetPassword(code, password);
        state.authTab = "login";
        state.authRole = activating ? "student" : role;
        state.authAccount = account;
        state.authStatus = "ready";
        state.error = "";
        state.step = 0;
        resetEvents([activating ? "账号已激活" : "口令已重设", "请使用新口令登录"]);
        state.railLabel = "入口 · 等待登录";
        if (scene) scene.setVerify(0);
        render();
        toast(activating ? "账号已激活，请使用新口令登录" : "口令已重设，请使用新口令登录");
        return;
      }
      let user;
      try {
        user = await api.login(account, password, role);
      } catch (error) {
        if (role !== "teacher" || error.code !== "role_mismatch") throw error;
        user = await api.login(account, password, "admin");
      }
      enterWorkspace(user.role, user.display_name,
        { live: true, message: `已验证${AUTH_ROLE_LABEL[user.role] || "账号"}身份` });
    } catch (error) {
      if (scene) scene.setVerify(0);
      const message = error && error.message ? error.message : "身份核验未完成";
      const hint = error && error.retryable ? `${message}，请稍后重试` : message;
      failAuth(hint);
    }
  })();
}

// ── 连真后端：把正式数据拉进来，映射成原型能渲染的形状 ────────────────
// 后端返回的是完整业务对象；渲染层只需要一部分字段。所有映射都在这里做，
// 免得渲染函数里到处判断"这是真数据还是样例"。

async function loadWorkspace() {
  if (!state.live || state.role === "admin" || state.loggingOut) return;
  const request = ++workspaceRequest;
  state.loading = true;
  state.liveError = "";
  render();
  try {
    if (state.role === "teacher" || state.role === "admin") {
      await loadTeacher();
    } else {
      await loadStudent();
    }
  } catch (error) {
    if (request === workspaceRequest) state.liveError = error && error.message ? error.message : "读取学习记录时出错了";
  } finally {
    if (request === workspaceRequest) {
      state.loading = false;
      render();
    }
  }
}

async function loadTeacher() {
  const generation = state.transitionId;
  const courses = await api.teacherClasses();
  if (generation !== state.transitionId || state.role !== "teacher") return;
  const withClasses = courses.find((entry) => entry.classes.length) || courses[0];
  if (!withClasses) {
    state.liveError = "这个账号还没有可访问的课程";
    return;
  }
  state.courseId = withClasses.course.id;
  state.courseName = withClasses.course.name;
  const classroom = withClasses.classes[0];
  if (!classroom) {
    state.liveError = "这门课还没有班级";
    return;
  }
  state.classId = classroom.id;
  state.className = classroom.name;

  const [members, goalList] = await Promise.all([
    api.classMembers(state.classId),
    api.goals(state.classId),
  ]);
  if (generation !== state.transitionId) return;
  state.liveGoals = goalList;
  const goal = goalList[0];

  // 任务只读一次；没有分配任务的新成员仍保留在班级列表中。
  const tasks = goal ? await api.myTasks(goal.id).catch(() => []) : [];
  if (generation !== state.transitionId) return;
  const students = [];
  for (const member of members) {
    students.push({
      id: member.user_id,
      name: member.display_name || member.username,
      status: member.status === "active" ? "在班" : "未激活",
      note: "",
      mastery: null,
      evidence: "",
      task: tasks.find((item) => item.student_id === member.user_id) || null,
      attempt: null,
    });
  }

  // 读取已授权目标的全部依据，再按学生归类，避免向未参与目标的新成员发起无效查询。
  if (goal && students.length) {
    try {
      const evidence = await api.goalEvidence(goal.id);
      if (generation !== state.transitionId) return;
      const items = Array.isArray(evidence) ? evidence : (evidence.items || []);
      for (const entry of students) {
        const scored = items.filter((item) => item.student_id === entry.id && typeof item.score_units === "number" && item.max_score_units);
        if (scored.length) {
          const earned = scored.reduce((sum, item) => sum + item.score_units, 0);
          const possible = scored.reduce((sum, item) => sum + item.max_score_units, 0);
          entry.mastery = possible ? Math.round((earned / possible) * 100) : null;
          entry.earned = earned / 100;
          entry.possible = possible / 100;
          entry.evidence = `本轮 ${scored.length} 条评分记录，来自 ${new Set(scored.map((i) => i.attempt_id)).size} 次作答。`;
          entry.note = entry.mastery === null
            ? "记录不足，先不判断。"
            : entry.mastery >= 80 ? "本轮已能独立完成，可进入目标题。"
            : entry.mastery >= 50 ? "部分掌握，先补讲解再练。" : "需要先补基础，建议先看讲解。";
        } else {
          entry.note = "还没有本轮作答记录。";
          entry.evidence = "记录不足以判断，不显示 0%。";
        }
      }
    } catch {
      for (const entry of students) {
        entry.note = "读取依据时出错，稍后重试。";
      }
    }
  }
  if (generation !== state.transitionId) return;
  state.liveStudents = students;
  state.liveGoals = goalList;
}

async function loadStudent() {
  const generation = state.transitionId;
  const [tasks, goals, courses] = await Promise.all([api.myTasks(), api.goals(), api.courses()]);
  if (generation !== state.transitionId || state.role !== "student") return;
  state.liveTasks = tasks;
  state.liveGoals = goals;
  const selected = tasks.find((item) => item.id === state.liveTask?.id)
    || tasks.find((item) => !taskFinished(item)) || tasks[0] || null;
  if (state.liveTask?.id !== selected?.id) {
    state.liveLease = null;
    state.draftDirty = false;
    state.answers = [];
    clearInterval(leaseTimer);
  }
  state.liveTask = selected;
  const course = courses.find((item) => item.id === selected?.course_id) || courses[0];
  state.courseName = course?.name || "";
  state.courseId = course?.id || null;
  state.goalLabel = selected ? taskTitle(selected) : "";
  try {
    const conversations = await api.conversations();
    if (generation !== state.transitionId) return;
    state.liveConversations = conversations;
    state.conversationError = "";
  } catch {
    if (generation === state.transitionId) state.conversationError = "对话记录暂时没有读到，可以稍后刷新。";
  }
}

function rememberStudentDraft() {
  if (!state.liveTask) return;
  state.draftDirty = true;
  state.studentDrafts[state.liveTask.id] = { answers: [...state.answers], dirty: true };
}

function updateStudentTask(task) {
  state.liveTask = task;
  const index = state.liveTasks.findIndex((item) => item.id === task.id);
  if (index >= 0) state.liveTasks[index] = task;
  else state.liveTasks.push(task);
  state.goalLabel = taskTitle(task);
}

async function selectLiveTask(taskId) {
  if (state.loading || !taskId) return;
  const generation = state.transitionId;
  try {
    state.loading = true;
    render();
    const full = await api.task(taskId);
    if (generation !== state.transitionId) return;
    const sameTask = state.liveTask?.id === full.id;
    if (!sameTask) {
      clearInterval(leaseTimer);
      state.liveLease = null;
      state.draftDirty = false;
      state.savedAt = null;
    }
    updateStudentTask(full);
    state.sheet = null;
    state.banner = "";
    state.selectedAttemptId = null;
    state.currentQ = 0;
    state.loading = false;
    if (full.diagnostic_required) await runDiagnostic();
    else if (taskFinished(full)) state.studentPage = "result";
    else if (full.scheduled_at && full.scheduled_at * 1000 > Date.now()) toast("尚未到任务开放时间");
    else await openLiveTask();
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "任务读取失败，请重试");
  } finally {
    if (generation === state.transitionId) {
      state.loading = false;
      render();
      window.scrollTo(0, 0);
    }
  }
}

async function refreshStudentTask() {
  if (state.loading || !state.liveTask) return;
  const id = state.liveTask.id;
  const generation = state.transitionId;
  state.loading = true;
  render();
  try {
    const full = await api.task(id);
    if (generation === state.transitionId && state.liveTask?.id === id) updateStudentTask(full);
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "结果暂时没有读到");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

async function readConversation(id) {
  const generation = state.transitionId;
  try {
    const messages = await api.conversationMessages(id);
    if (generation !== state.transitionId) return;
    state.liveMessages = messages;
    state.activeConversation = state.liveConversations.find((item) => item.id === id);
    state.sheet = "conversation";
    render();
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "对话记录读取失败");
  }
}

function submitPaper() {
  if (state.loading) return;
  const count = state.liveTask?.questions?.length ?? state.answers.length;
  const missing = Array.from({ length: count }, (_, index) => state.answers[index])
    .findIndex((item) => item === undefined || item === null || typeof item === "string" && !item.trim());
  if (missing >= 0) {
    state.currentQ = missing;
    toast(`第 ${missing + 1} 题还没作答`);
    render();
    return;
  }
  // 连上后端：走真实的交卷接口，等后台评卷
  if (state.live && state.liveTask) {
    if (!state.liveLease || state.banner === "takeover") { toast("当前没有编辑权限，请重新打开任务"); return; }
    const task = state.liveTask;
    const lease = state.liveLease;
    const generation = state.transitionId;
    (async () => {
      try {
        state.loading = true;
        render();
        const payload = {};
        (task.questions || []).forEach((question, index) => { payload[question.id] = state.answers[index]; });
        const saved = await api.saveDraft(task.id, task.version, lease, payload, `pre-submit-${Date.now()}`);
        if (generation !== state.transitionId) return;
        updateStudentTask({ ...task, version: saved.version, draft_answers: payload });
        state.draftDirty = false;
        state.savedAt = Date.now();
        delete state.studentDrafts[task.id];
        await api.submitTask(task.id, saved.version, lease, `submit-${task.id}-${Date.now()}`);
        if (generation !== state.transitionId) return;
        clearInterval(leaseTimer);
        state.liveLease = null;
        updateStudentTask({ ...state.liveTask, status: "submitted" });
        state.submitted = true;
        state.selectedAttemptId = null;
        state.studentPage = "result";
        state.banner = "awaiting";
        state.loading = false;
        toast("已交卷，正在后台评卷");
        render();
        await pollTask(task.id, generation);
      } catch (error) {
        if (generation !== state.transitionId) return;
        state.banner = "save-fail";
        toast(error?.message || "交卷没有成功，可以再试一次");
      } finally {
        if (generation === state.transitionId) { state.loading = false; render(); }
      }
    })();
    return;
  }
  state.submitted = true;
  state.studentPage = "result";
  state.banner = state.banner === "awaiting" ? "awaiting" : "";
  toast("已交卷，成绩以这次快照为准");
  render();
}

/** 等后台评卷完成；不假造进度，就是定时问一次。 */
async function pollTask(taskId, generation, tries = 40) {
  for (let i = 0; i < tries; i += 1) {
    await new Promise((resolve) => setTimeout(resolve, 600));
    if (generation !== state.transitionId || state.screen !== "app" || state.liveTask?.id !== taskId) return;
    try {
      const full = await api.task(taskId);
      if (generation !== state.transitionId || state.liveTask?.id !== taskId) return;
      updateStudentTask(full);
      if (["results_ready", "awaiting_manual", "closed"].includes(full.status)) {
        state.banner = "";
        render();
        return;
      }
    } catch {
      return;
    }
  }
}

/** 学生进工作区时先拿编辑租约——后端规定同一任务同时只允许一端作答。 */
async function openLiveTask() {
  if (!state.live || !state.liveTask) return;
  const taskId = state.liveTask.id;
  const generation = state.transitionId;
  try {
    state.loading = true;
    render();
    const deviceId = window.__studentDeviceId ||= `student-${crypto.randomUUID()}`;
    const lease = state.liveLease?.task_id === taskId && state.liveLease.lease_until * 1000 > Date.now()
      ? state.liveLease : await api.acquireLease(taskId, deviceId);
    const full = await api.task(taskId);
    if (generation !== state.transitionId || state.liveTask?.id !== taskId) return;
    state.liveLease = lease;
    updateStudentTask(full);
    const local = state.studentDrafts[taskId];
    const draft = full.draft_answers || {};
    state.answers = local?.answers || (full.questions || []).map((question) => draft[question.id] ?? "");
    state.draftDirty = Boolean(local?.dirty);
    state.banner = "";
    state.currentQ = 0;
    state.studentPage = "workspace";
    startLeaseRenewal(taskId, generation);
    return true;
  } catch (error) {
    if (generation !== state.transitionId) return;
    state.banner = "takeover";
    toast(error?.message || "另一端正在作答，稍后再试");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

function startLeaseRenewal(taskId, generation) {
  clearInterval(leaseTimer);
  leaseTimer = setInterval(async () => {
    if (generation !== state.transitionId || !state.liveLease || state.liveTask?.id !== taskId) {
      clearInterval(leaseTimer);
      return;
    }
    const lease = state.liveLease;
    try {
      const renewed = await api.renewLease(taskId, lease);
      if (generation === state.transitionId && state.liveLease === lease) state.liveLease = { ...lease, ...renewed };
    } catch (error) {
      if (generation !== state.transitionId || state.liveLease !== lease) return;
      clearInterval(leaseTimer);
      state.liveLease = null;
      state.banner = "takeover";
      toast(error?.message || "编辑连接已断开，当前作答仍保留在页面");
      render();
    }
  }, 60000);
}

/** 首诊独立作答，不自动代答，也不计入正式成绩。 */
async function runDiagnostic() {
  if (!state.live || !state.liveTask) return;
  const taskId = state.liveTask.id;
  const generation = state.transitionId;
  try {
    state.loading = true;
    render();
    const diagnostic = await api.startDiagnostic(taskId, state.liveTask.version, `diagnostic-${Date.now()}`);
    if (generation !== state.transitionId) return;
    if (state.diagnostic?.id !== diagnostic.id) state.diagnosticAnswers = diagnostic.questions.map(() => "");
    state.diagnostic = diagnostic;
    state.currentQ = 0;
    state.studentPage = "diagnostic";
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "短诊没有打开");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

async function submitLiveDiagnostic() {
  if (!state.diagnostic || state.loading) return;
  const questions = state.diagnostic.questions || [];
  const missing = questions.findIndex((_, index) => state.diagnosticAnswers[index] === undefined
    || state.diagnosticAnswers[index] === "");
  if (missing >= 0) {
    state.currentQ = missing;
    toast(`第 ${missing + 1} 题还没作答`);
    render();
    return;
  }
  const generation = state.transitionId;
  const taskId = state.liveTask.id;
  try {
    state.loading = true;
    render();
    const answers = Object.fromEntries(questions.map((question, index) => [question.id, state.diagnosticAnswers[index]]));
    await api.submitDiagnostic(state.diagnostic.id, answers, `diagnostic-submit-${Date.now()}`);
    if (generation !== state.transitionId) return;
    const full = await api.task(taskId);
    if (generation !== state.transitionId) return;
    updateStudentTask(full);
    state.diagnostic = null;
    state.studentPage = "home";
    state.studentTab = "tasks";
    toast("短诊完成，可以开始正式作答");
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "短诊没有提交成功，作答已保留");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

async function retryLiveTask() {
  if (state.loading || !state.liveTask) return;
  const taskId = state.liveTask.id;
  const generation = state.transitionId;
  state.loading = true;
  render();
  try {
    const full = await api.retryTask(taskId, state.liveTask.version, `retry-${Date.now()}`);
    if (generation !== state.transitionId) return;
    updateStudentTask(full);
    state.liveLease = null;
    delete state.studentDrafts[taskId];
    state.answers = [];
    state.draftDirty = false;
    state.submitted = false;
    state.seenSolution = true;
    state.loading = false;
    const opened = await openLiveTask();
    if (generation === state.transitionId && opened) toast("已开始重做，首次成绩保持不变");
  } catch (error) {
    if (generation === state.transitionId) toast(error?.message || "本轮暂时不能重做");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

/** 保存草稿：带版本与租约，后端据此拒绝过期写入。 */
async function saveLiveDraft() {
  if (state.loading || !state.live || !state.liveTask || !state.liveLease) return;
  const task = state.liveTask;
  const generation = state.transitionId;
  state.loading = true;
  render();
  try {
    const payload = {};
    (task.questions || []).forEach((question, index) => {
      const value = state.answers[index];
      if (value !== undefined && value !== "") payload[question.id] = value;
    });
    const saved = await api.saveDraft(task.id, task.version, state.liveLease, payload, `draft-${Date.now()}`);
    if (generation !== state.transitionId || state.liveTask?.id !== task.id) return;
    updateStudentTask({ ...task, version: saved.version, status: saved.status || "in_progress", draft_answers: payload });
    delete state.studentDrafts[task.id];
    state.draftDirty = false;
    state.savedAt = Date.now();
    state.banner = "";
    toast("进度已保存");
  } catch (error) {
    if (generation !== state.transitionId) return;
    state.banner = "save-fail";
    toast(error?.message || "没有保存成功，当前作答仍在页面");
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

// ── 场景 ──────────────────────────────────────────────────────────────
// 液态金属主视觉由 scene.js 负责；这里只管何时创建、何时让位、何时化散。
let scene = null;

function mountScene() {
  const canvas = document.querySelector("#glass");
  if (!canvas || scene || state.fallback) return;
  scene = createScene(canvas, { reduce: () => state.reduce });
  if (!scene) {
    // 无 WebGL：换成一张静态玻璃弧，功能完全不受阻
    stage.classList.add("no-webgl");
    state.fallback = true;
    if (state.screen === "entry") render();
    return;
  }
  scene.setReveal(state.reduce ? 1 : 0);
  if (state.reduce) scene.setAssembled();
  else scene.playAssemble(1700);
  scene.start();
  window.__scene = scene;   // 截图与自动化检查用
}

let revealTimer = 0;

function setReveal(value) {
  if (state.transition) return;
  state.reveal = Math.min(1, Math.max(0, value));
  if (scene) scene.setPull(state.reveal);
  const open = state.reveal > 0.5;
  if (open !== state.shifted || state.screen !== "entry" || !view.querySelector(".entry")) {
    render();
    return;
  }
  const entry = view.querySelector(".entry");
  entry.style.setProperty("--reveal", state.reveal.toFixed(3));
  // 侧栏的刻度、光标、微文案都跟着 reveal 走
  for (const node of entry.querySelectorAll("[style*='--reveal']")) {
    node.style.setProperty("--reveal", state.reveal.toFixed(3));
  }
  // 时钟字重分档加粗（200→500），与字号 calc 同步渐进
  const hm = entry.querySelector(".chrono-hm");
  if (hm) hm.style.fontWeight = String(200 + Math.round(state.reveal * 3) * 100);
  refreshAnchors();
}

function animateReveal(target) {
  if (state.transition) return;
  cancelAnimationFrame(revealTimer);
  if (state.reduce) {
    setReveal(target);
    return;
  }
  const from = state.reveal;
  const started = performance.now();
  const step = (now) => {
    const t = Math.min(1, (now - started) / 700);
    const eased = t < 0.5 ? 4 * t * t * t : 1 - ((-2 * t + 2) ** 3) / 2;
    setReveal(from + (target - from) * eased);
    if (t < 1) revealTimer = requestAnimationFrame(step);
  };
  revealTimer = requestAnimationFrame(step);
}

// 无 WebGL 时的静态替代图形：一段冰蓝玻璃弧，功能完全不受影响
function fallbackSvg() {
  return `<svg class="fallback" viewBox="0 0 800 600" aria-hidden="true">
    <defs>
      <linearGradient id="fx" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0" stop-color="rgba(255,255,255,.92)"/>
        <stop offset="0.5" stop-color="rgba(168,216,236,.66)"/>
        <stop offset="1" stop-color="rgba(31,138,168,.40)"/>
      </linearGradient>
    </defs>
    <path d="M 150 300 C 250 120 400 120 470 250 C 540 380 660 400 700 300"
          fill="none" stroke="url(#fx)" stroke-width="52" stroke-linecap="round"/>
    <path d="M 150 300 C 250 120 400 120 470 250"
          fill="none" stroke="rgba(255,255,255,.75)" stroke-width="6" stroke-linecap="round"/>
  </svg>`;
}

// ── 画布缩放：1920×1080 设计坐标贴到视口 ───────────────────────────────
// 每次 render() 之后都要调用，因为 renderEntry 会重建 .entry-canvas。
// 窄屏不用设计画布（CSS 里已改成文档流），这里要把 --k 清掉。
function fitCanvas() {
  const entry = view.querySelector(".entry-canvas");
  if (!entry) return;
  if (innerWidth <= 900) {
    entry.style.removeProperty("--k");
    return;
  }
  const k = Math.min(innerWidth / 1920, innerHeight / 1080);
  entry.style.setProperty("--k", k.toFixed(4));
}

// ── 金属上两个引导点：把曲线上的位置投到设计坐标 ─────────────────────
function projectAnchor(u) {
  // 与 scene.js 的 curveAt 同一公式，再用同一套相机参数投影
  const t = u * Math.PI * 2;
  const R = 1.0, A = 0.92, D = 0.52;
  const p = [R * Math.cos(t), R * A * Math.sin(t) * Math.cos(t), D * Math.sin(t)];
  const yaw = state.anchorYaw + state.anchorSpin;
  const pitch = state.anchorPitch;
  const cy = Math.cos(yaw), sy = Math.sin(yaw);
  let q = [p[0] * cy + p[2] * sy, p[1], -p[0] * sy + p[2] * cy];
  const cx = Math.cos(pitch), sx = Math.sin(pitch);
  q = [q[0], q[1] * cx - q[2] * sx, q[1] * sx + q[2] * cx];
  const shiftX = -0.06 - state.reveal * 0.46;
  q[0] += shiftX;
  // 与 scene.js 同样的相机距离推导
  const fov = 0.92, aspect = innerWidth / innerHeight;
  const tan = Math.tan(fov / 2);
  const dist = Math.max(1.30 / tan, 1.42 / (tan * aspect)) * 1.06;
  const eye = [0, 0.10 * dist * 0.2, dist];
  const zc = eye[2] - q[2];
  if (zc <= 0.05) return { x: -9999, y: -9999 };
  const ndcX = (q[0] - eye[0]) / (zc * tan * aspect);
  const ndcY = (q[1] - eye[1]) / (zc * tan);
  return { x: (ndcX * 0.5 + 0.5) * 1920, y: (-ndcY * 0.5 + 0.5) * 1080 };
}

function refreshAnchors() {
  if (state.screen !== "entry") return;
  const a = projectAnchor(0.22);
  const b = projectAnchor(0.70);
  state.anchorA = a;
  state.anchorB = b;
  const svg = view.querySelector(".callouts svg");
  if (!svg) return;
  const lines = svg.querySelectorAll("line");
  const dots = svg.querySelectorAll("circle");
  lines[0].setAttribute("x1", a.x); lines[0].setAttribute("y1", a.y);
  lines[0].setAttribute("x2", a.x + 150); lines[0].setAttribute("y2", a.y - 130);
  lines[1].setAttribute("x1", b.x); lines[1].setAttribute("y1", b.y);
  lines[1].setAttribute("x2", b.x - 120); lines[1].setAttribute("y2", b.y + 170);
  dots[0].setAttribute("cx", a.x); dots[0].setAttribute("cy", a.y);
  dots[1].setAttribute("cx", b.x); dots[1].setAttribute("cy", b.y);
  const labels = view.querySelectorAll(".callout-label");
  if (labels[0]) { labels[0].style.left = `${a.x + 158}px`; labels[0].style.top = `${a.y - 152}px`; }
  if (labels[1]) { labels[1].style.left = `${b.x - 208}px`; labels[1].style.top = `${b.y + 168}px`; }
}

// ── 拖拽转动 / 滚轮与触摸推进 ────────────────────────────────────────
let drag = null;
document.addEventListener("pointerdown", (event) => {
  if (state.screen !== "entry" || state.reduce || state.transition) return;
  if (event.target.closest("form, button, a, input, textarea, details")) return;
  drag = { x: event.clientX, y: event.clientY, moved: false };
});
document.addEventListener("pointermove", (event) => {
  if (!drag) return;
  const dx = event.clientX - drag.x;
  const dy = event.clientY - drag.y;
  if (Math.hypot(dx, dy) > 4) drag.moved = true;
  drag.x = event.clientX;
  drag.y = event.clientY;
  if (scene) scene.rotateBy(dx, dy);
});
document.addEventListener("pointerup", (event) => {
  const wasDrag = drag ? drag.moved : false;
  drag = null;
  // 拖过 = 在转装置，不要顺手把它推进去
  if (wasDrag) return;
  if (state.screen !== "entry" || state.reduce || state.transition) return;
  if (state.reveal > 0.5) return;
  if (event.target.closest("form, button, a, input, textarea, details, summary")) return;
  if (event.target.closest(".entry-copy, .entry-foot, .chrono, .rail, .auth-card")) return;
  // 单独点一下玻璃（没有拖动）也推进：转起来之后，点击是最自然的"进去"
  animateReveal(1);
});
document.addEventListener("pointercancel", () => { drag = null; });

document.addEventListener("wheel", (event) => {
  if (state.screen !== "entry") return;
  if (event.target.closest(".auth-card") || (innerWidth <= 900 && state.shifted)) return;
  event.preventDefault();
  if (state.reduce) return;
  // 一格滚轮约 100~120px。除以 1100 让大约七八格才能完全展开，
  // 中间会经过"卡片半透明、还不能输入"的阶段，不会是瞬时跳到底。
  setReveal(state.reveal + event.deltaY / 1100);
}, { passive: false });

let touchY = null;
document.addEventListener("touchstart", (event) => {
  if (state.screen === "entry" && !event.target.closest(".auth-card")) touchY = event.touches[0].clientY;
  else touchY = null;
}, { passive: true });
document.addEventListener("touchmove", (event) => {
  if (touchY === null || state.screen !== "entry" || state.reduce || event.target.closest(".auth-card")) return;
  const next = event.touches[0].clientY;
  setReveal(state.reveal + (touchY - next) / 420);
  touchY = next;
}, { passive: true });
document.addEventListener("touchend", () => { touchY = null; });

window.addEventListener("resize", () => {
  fitCanvas();
  refreshAnchors();
});

reduceQuery.addEventListener("change", () => {
  state.reduce = reduceQuery.matches;
  cancelAnimationFrame(revealTimer);
  drag = null;
  touchY = null;
  if (scene) scene.setReducedMotion(state.reduce);
  if (state.screen === "entry" && !state.transition) {
    if (state.reduce) {
      state.reveal = 1;
      if (scene) scene.reset({ reveal: 1 });
    }
    setReveal(state.reveal);
    render();
    refreshAnchors();
  }
});

// ── 引导点跟随金属的缓慢自转 ─────────────────────────────────────────
setInterval(() => {
  if (state.screen !== "entry" || state.reduce || state.transition) return;
  const now = performance.now() / 1000;
  state.anchorYaw = 0.40 + Math.sin(now * 0.085) * 0.26;
  state.anchorPitch = 0.17 + Math.sin(now * 0.052 + 0.8) * 0.08;
  refreshAnchors();
}, 120);

tickClock();
setInterval(tickClock, 1000);
setInterval(() => {
  if (state.screen !== "entry" || state.reduce || state.transition) return;
  state.formulaIndex = (state.formulaIndex + 1) % AUTH_FORMULAS.length;
  const reel = view.querySelector(".equation-reel");
  if (!reel) return;
  const formula = AUTH_FORMULAS[state.formulaIndex];
  reel.textContent = formula;
  reel.dataset.equation = formula;
}, 2600);
// 减少动态：不做出场动效，直接停在"已展开、可直接登录"的状态。
// 注意 state.reveal 也要一起改——只改 3D 场景的话，DOM 面板仍然是隐藏的。
if (state.reduce) state.reveal = 1;
state.shifted = state.reveal > 0.5;
// 后端探测结果由 index.html 里的 api.probe() 提供
state.live = Boolean(window.api && window.api.state.live);
if (state.live) {
  resetEvents(["已连上正式服务", "等待账号口令"]);
  state.railLabel = "入口 · 已连服务";
  // 已经有会话就直接进工作区，刷新不会掉出来
  window.api.currentUser().then((user) => {
    if (!user) return;
    enterWorkspace(user.role, user.display_name,
      { live: true, message: `已进入${user.display_name ? " " + user.display_name : ""}` });
  });
} else {
  state.serviceError = (window.api && window.api.state.lastError) || "正式认证服务未连接，登录与申领暂不可用";
  resetEvents(["正式服务未连接", "登录暂不可用"]);
  state.railLabel = "入口 · 服务未连接";
}
mountScene();
fitCanvas();
render();
refreshAnchors();
