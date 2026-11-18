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
  teacherCourses: [],
  teacherContextVersion: 0,
  reviewSelection: null,
  liveGoals: [],
  liveTasks: [],
  liveTask: null,
  liveLease: null,
  liveConversations: [],
  liveMessages: [],
  conversationError: "",
  activeConversationId: null,
  chatMessages: [],
  chatDrafts: {},
  chatContexts: {},
  chatDraft: "",
  chatPolicy: null,
  chatPolicyLoading: false,
  chatPolicyError: "",
  chatContext: null,
  chatResponseError: "",
  chatOlderCursor: null,
  chatHasOlder: false,
  chatLoadingOlder: false,
  chatPendingSends: {},
  pendingChatCreate: null,
  chatCreating: false,
  chatRenameOpen: false,
  chatRenameTitle: "",
  chatRenameError: "",
  chatRenaming: false,
  chatArchived: false,
  chatHasMoreConversations: false,
  chatSending: false,
  hintPreparing: null,
  diagnostic: null,
  diagnosticAnswers: [],
  diagnosticUnknown: [],
  diagnosticDrafts: {},
  diagnosticCommand: null,
  studentWrites: {},
  studentWriteEpoch: 0,
  studentComposing: false,
  studentSubmitBusy: false,
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
let chatEpoch = 0;
const chatStreamClosers = new Map();
const chatReadControllers = new Set();
let chatListSequence = 0;
let chatPostSequence = 0;
let chatComposing = false;
let chatRenderPending = false;

const teacherLibrary = window.createTeacherLibrary({
  api, state, render, toast,
  onPrepareGoal: (options = {}) => teacherComposer.open(options),
});
const teacherComposer = window.createTeacherPlanning({
  api, state, render, toast,
  onPublished: () => loadWorkspace(),
  onLibrary: (options = {}) => {
    state.teacherView = "materials";
    render();
    teacherLibrary.load(options);
  },
});
const adminWorkspace = window.createAdminWorkspace({
  api, state, render, toast,
  onChanged: () => loadWorkspace(),
});
const teacherManagement = window.createTeacherManagement({
  api, state, render, toast,
  onChanged: async ({ classId, courseId } = {}) => {
    if (state.role !== "teacher" || state.loggingOut) return;
    if (courseId !== undefined || classId !== undefined) {
      state.teacherContextVersion += 1;
      if (courseId !== undefined) state.courseId = courseId;
      if (classId !== undefined) state.classId = classId;
      state.openStudent = null; state.liveStudents = []; state.liveGoals = [];
      teacherLibrary.reset(); teacherComposer.reset();
      resetLearningWorkspaces();
    }
    await loadWorkspace();
  },
});
const teacherReview = window.createTeacherReview({
  api, state, render, toast,
  onChanged: () => learningNotifications.load(),
  onSelectionChange: (selection) => {
    if (state.role !== "teacher" || state.loggingOut) return;
    state.reviewSelection = { goalId: selection.goalId, taskId: selection.taskId, attemptId: selection.attemptId, studentId: selection.studentId };
  },
  onReports: (selection) => openTeacherReports(selection),
});
const teacherReportWorkspace = window.createTeacherReports({ api, state, render, toast });
const teacherCompetition = window.createTeacherCompetition({ api, state, render, toast });
const teacherInsightsWorkspace = window.createTeacherInsights({
  api, state, render, toast,
  onChanged: () => learningNotifications.load(),
});
const studentFeedback = window.createStudentFeedback({
  api, state, render, toast,
  onChanged: () => learningNotifications.load(),
});
const learningNotifications = window.createLearningNotifications({
  api, state, render, toast,
  onOpen: (notification) => openLearningNotification(notification),
});
let notificationOpenSequence = 0;

function resetLearningWorkspaces({ preserveNotifications = false } = {}) {
  if (!preserveNotifications) notificationOpenSequence += 1;
  state.reviewSelection = null;
  teacherReview.reset();
  teacherReportWorkspace.reset();
  teacherCompetition.reset();
  teacherInsightsWorkspace.reset();
  studentFeedback.reset();
  if (!preserveNotifications) learningNotifications.reset();
}

function leaveTeacherPage(nextView) {
  if (state.teacherView === nextView) return true;
  if (state.teacherView === "management") return teacherManagement.leave();
  if (state.teacherView === "review") return teacherReview.leave();
  if (state.teacherView === "reports") return teacherReportWorkspace.leave();
  if (state.teacherView === "insights") return teacherInsightsWorkspace.leave();
  if (state.teacherView === "competition") return teacherCompetition.leave();
  return true;
}

async function openTeacherReview(selection = {}, { notificationSequence } = {}) {
  if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading || state.drawer) return false;
  if (!leaveTeacherPage("review")) return false;
  if (notificationSequence === undefined) notificationOpenSequence += 1;
  else if (notificationSequence !== notificationOpenSequence) return false;
  state.teacherView = "review";
  render();
  return teacherReview.openSelection(selection);
}

async function openTeacherReports(selection = state.reviewSelection || {}) {
  if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading || state.drawer) return false;
  if (!leaveTeacherPage("reports")) return false;
  notificationOpenSequence += 1;
  state.teacherView = "reports";
  render();
  return teacherReportWorkspace.openSelection({ goalId: selection.goalId, studentId: selection.studentId });
}

async function openTeacherInsights(selection = state.reviewSelection || {}, { notificationSequence } = {}) {
  if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading || state.drawer) return false;
  if (!leaveTeacherPage("insights")) return false;
  if (notificationSequence === undefined) notificationOpenSequence += 1;
  else if (notificationSequence !== notificationOpenSequence) return false;
  state.teacherView = "insights";
  render();
  return teacherInsightsWorkspace.openSelection({ goalId: selection.goalId, insightId: selection.insightId, draftId: selection.draftId });
}

async function openStudentFeedback(selection = {}, { notificationSequence } = {}) {
  if (state.role !== "student" || state.loggingOut || state.transition || state.loading) return false;
  if ((state.studentPage === "workspace" || state.studentPage === "diagnostic") && !leaveStudentWork()) return false;
  if (state.studentTab === "feedback" && !studentFeedback.leave()) return false;
  if (notificationSequence === undefined) notificationOpenSequence += 1;
  else if (notificationSequence !== notificationOpenSequence) return false;
  closeStudentChatStream();
  setChatRoute(null);
  state.sheet = null;
  state.studentTab = "feedback";
  state.studentPage = "home";
  render();
  return studentFeedback.openSelection(selection);
}

async function openLearningNotification(notification) {
  if (!state.live || state.loggingOut || state.transition || state.loading || state.drawer) return false;
  const payload = notification?.payload || notification || {};
  const taskId = payload.task_id, insightId = payload.insight_id;
  if ((!taskId && !insightId) || notification?.redacted || payload.redacted) {
    toast("这条通知没有可打开的学习记录，请刷新通知");
    return false;
  }
  const sequence = ++notificationOpenSequence;
  const generation = state.transitionId;
  const role = state.role;
  const actorId = api.state.user?.id;
  const current = () => sequence === notificationOpenSequence && generation === state.transitionId
    && role === state.role && actorId === api.state.user?.id && !state.loggingOut;
  try {
    if (insightId) {
      if (role !== "teacher") return false;
      const insight = await api.call("GET", `/teaching-insights/${encodeURIComponent(insightId)}`);
      if (!current()) return false;
      if (insight.id !== insightId || !insight.origin_goal_id || !insight.class_id || !insight.course_id) {
        toast("通知对应的总结暂时不可用");
        return false;
      }
      const entries = await api.teacherClasses();
      if (!current()) return false;
      const entry = entries.find((item) => item.course.id === insight.course_id && item.classes.some((classroom) => classroom.id === insight.class_id));
      if (!entry) { toast("当前账号已无权查看通知对应的班级或总结"); return false; }
      state.teacherCourses = entries;
      if (state.classId !== insight.class_id || state.courseId !== insight.course_id) {
        const changed = await selectTeacherClass(insight.class_id, { notificationSequence: sequence });
        if (!changed || !current()) return false;
      }
      const opened = await openTeacherInsights({ goalId: insight.origin_goal_id, insightId, draftId: payload.draft_id }, { notificationSequence: sequence });
      return current() && opened === true;
    }
    if (role === "student") {
      const opened = await openStudentFeedback({ taskId, attemptId: payload.attempt_id, threadId: payload.thread_id, questionVersionId: payload.question_version_id }, { notificationSequence: sequence });
      return current() && opened === true;
    }
    if (role !== "teacher") return false;
    const full = await api.task(taskId);
    if (!current()) return false;
    if (payload.attempt_id && !full.attempts?.some((attempt) => attempt.id === payload.attempt_id)) {
      toast("通知对应的提交记录暂时不可用");
      return false;
    }
    const entries = await api.teacherClasses();
    if (!current()) return false;
    const entry = entries.find((item) => item.course.id === full.course_id && item.classes.some((classroom) => classroom.id === full.class_id));
    if (!entry || full.id !== taskId) { toast("当前账号已无权查看通知对应的班级或任务"); return false; }
    state.teacherCourses = entries;
    if (state.classId !== full.class_id) {
      const changed = await selectTeacherClass(full.class_id, { notificationSequence: sequence });
      if (!changed || !current()) return false;
    }
    const opened = await openTeacherReview({ goalId: full.goal_id, taskId: full.id, attemptId: payload.attempt_id, studentId: full.student_id, threadId: payload.thread_id, messageId: payload.message_id }, { notificationSequence: sequence });
    return current() && opened === true;
  } catch (error) {
    if (current()) toast(error?.message || "通知对应的记录暂时无法打开");
    return false;
  }
}

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
  view.innerHTML = `<div class="app admin-app">
    <aside class="side" aria-label="平台管理">
      <div class="brand">伴星导学</div>
      <p class="mgmt-side-title">平台管理</p>
      <p class="muted">审核教师与课程，管理账号和使用额度。</p>
      <div class="spacer"></div>
      <p class="who muted">${escapeHtml(state.who)} · 管理员</p>
      <button class="nav-btn" type="button" data-action="logout" ${state.loggingOut ? "disabled" : ""}>${state.loggingOut ? "正在退出" : "安全退出"}</button>
    </aside>
    <main class="main" id="admin-main" tabindex="-1" aria-labelledby="admin-title">
      <h1 class="page" id="admin-title">平台管理</h1>
      <p class="muted">教师申请、课程审核与账号状态以正式服务的记录为准。</p>
      ${adminWorkspace.render()}
    </main>
  </div>`;
}

function renderTeacher() {
  const page = {
    home: teacherHome,
    student: teacherStudent,
    review: () => teacherReview.render(),
    materials: teacherMaterials,
    competition: () => teacherCompetition.render(),
    reports: teacherReports,
    insights: () => teacherInsightsWorkspace.render(),
    management: () => teacherManagement.render(),
  }[state.teacherView] || teacherHome;
  view.innerHTML = `
    <div class="app teacher-app" ${state.drawer ? "inert" : ""}>
      <aside class="side">
        <div class="brand">伴星导学</div>
        ${nav("home", "班级学情")}${nav("review", "批改与反馈")}${nav("student", "学生依据")}${nav("materials", "题库与材料")}${nav("competition", "学情分析")}${nav("reports", "阶段成果")}${nav("insights", "总结与下一轮")}${nav("management", "课程与班级")}
        <div class="spacer"></div>
        <p class="who muted">${escapeHtml(state.who)} · ${escapeHtml(state.className || "教师工作区")}</p>
        <button class="nav-btn" data-action="logout">退出</button>
      </aside>
      <main class="main" id="teacher-main" tabindex="-1">${state.teacherView === "management" ? "" : teacherContext()}${state.live ? learningNotifications.render() : ""}${page()}</main>
    </div>
    ${state.drawer ? drawer() : ""}`;
}

function nav(id, label) {
  return `<button class="nav-btn" data-view="${id}" aria-current="${state.teacherView === id ? "page" : "false"}">${label}</button>`;
}

function teacherContext() {
  const entries = state.teacherCourses || [];
  if (!entries.some((item) => item.classes.length)) return "";
  return `<div class="teacher-context"><label class="field">课程与班级<select data-teacher-class aria-label="课程与班级" ${state.loading ? "disabled" : ""}>${entries.map(({ course, classes }) => classes.length ? `<optgroup label="${escapeHtml(course.name)}">${classes.map((classroom) => `<option value="${escapeHtml(classroom.id)}" ${state.classId === classroom.id ? "selected" : ""}>${escapeHtml(course.name)} / ${escapeHtml(classroom.name)}</option>`).join("")}</optgroup>` : "").join("")}</select></label><p class="muted">题库按课程共享，发布范围由所选班级和学生确定。</p></div>`;
}

async function selectTeacherClass(classId, { notificationSequence } = {}) {
  const entry = state.teacherCourses.find((item) => item.classes.some((classroom) => classroom.id === classId));
  if (!entry || state.role !== "teacher" || state.loading || state.drawer || state.loggingOut || state.transition) return false;
  if (!leaveTeacherPage(null)) { render(); return false; }
  const classroom = entry.classes.find((item) => item.id === classId);
  const generation = state.transitionId;
  state.teacherContextVersion += 1;
  const context = state.teacherContextVersion;
  state.courseId = entry.course.id; state.courseName = entry.course.name;
  state.classId = classroom.id; state.className = classroom.name;
  state.openStudent = null; state.liveStudents = []; state.liveGoals = [];
  teacherLibrary.reset(); teacherComposer.reset(); teacherManagement.reset();
  // A notification owns its read receipt across a class switch in the same session.
  // Ordinary class navigation invalidates any earlier notification opening.
  const preserveNotifications = notificationSequence !== undefined && notificationSequence === notificationOpenSequence;
  resetLearningWorkspaces({ preserveNotifications });
  await loadWorkspace();
  if (generation !== state.transitionId || context !== state.teacherContextVersion || state.loggingOut || state.role !== "teacher"
      || (notificationSequence !== undefined && notificationSequence !== notificationOpenSequence)) return false;
  if (state.teacherView === "materials") teacherLibrary.load();
  return state.classId === classId && !state.liveError;
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
      <button class="btn" data-action="reload">再读一次</button>
      <button class="btn" data-view="management">课程与班级</button>`;
  }
  if (!state.classId) {
    return `<h1 class="page">从一门课程开始</h1>
      <p class="muted">${state.courseId ? "课程已准备好，建好班级后即可邀请学生并布置学习任务。" : "申请课程，审核通过后创建班级，再为学生发放激活码。"}</p>
      <button class="btn primary" data-view="management">管理课程与班级</button>`;
  }
  const goal = state.liveGoals[0];
  const students = state.liveStudents;
  const scored = students.filter((item) => item.mastery !== null);
  // 本轮范围只统计实际拿到任务的学生；勾选发布后未选中的学生不属于“涉及学生”。
  const participants = students.filter((item) => item.task);
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
        <p>${participants.length ? `本轮共 ${participants.length} 位学生，其中 ${scored.length} 人已有评分记录。` : `本轮任务名单暂未读到，其中 ${scored.length} 人已有评分记录。`}</p>
        <p class="muted">班级共 ${students.length} 人；未勾选的学生不会收到这一轮任务。</p>
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
      <button class="btn primary" data-action="open-drawer" data-goal-student="${escapeHtml(student.id)}">给${escapeHtml(student.name)}安排</button>
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
        <button class="btn" data-action="open-drawer" data-goal-student="${escapeHtml(student.id)}">按此依据出目标</button>
      </section>
    </div>
    <h2 class="section">历史作答与短评</h2>
    <section class="panel"><p>选择具体任务和提交记录后，可以逐题批改、查看更正记录并发送短评。</p><button class="btn primary" data-action="open-review" data-student-id="${escapeHtml(student.id)}"${goal ? ` data-goal-id="${escapeHtml(goal.id)}"` : ""}>查看作答与反馈</button></section>`;
}

function teacherMaterials() {
  return teacherLibrary.render();
}

function teacherReports() {
  return teacherReportWorkspace.render();
}

function drawer() {
  return teacherComposer.render();
}

function studentIcon(name) {
  const paths = {
    tasks: '<rect x="5" y="4" width="14" height="17" rx="2"/><path d="M9 4V2h6v2M9 10h6M9 14h6M9 18h3"/>',
    companion: '<path d="M20 11.5a8 8 0 0 1-8 8H5l-3 2v-7a8 8 0 1 1 18-3Z"/><path d="M8 11h.01M12 11h.01M16 11h.01"/>',
    feedback: '<path d="M4 4h16v12H9l-5 4V4Z"/><path d="M8 8h8M8 12h5"/>',
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
  return Boolean(task?.submission_closed) || ["submitted", "grading", "results_ready", "awaiting_manual", "closed"].includes(task?.status);
}

function taskStatus(task) {
  if (task.submission_closed) return "已关闭提交";
  if (task.diagnostic_required) return "待完成短诊";
  if (task.scheduled_at && task.scheduled_at * 1000 > Date.now()) return "尚未开放";
  return ({ results_ready: "已完成", awaiting_manual: "等待教师评分", closed: "已结束", submitted: "正在评卷", grading: "正在评卷" })[task.status]
    || (Object.keys(task.draft_answers || {}).length ? "作答中" : "待开始");
}

function taskQuestionCount(task) {
  return task.question_count ?? task.questions?.length ?? 0;
}

function studentNav(id, label) {
  const active = state.studentPage === "home" || state.studentPage === "chat" ? state.studentTab === id
    : id === (state.studentPage === "result" ? "mine" : "tasks");
  const count = id === "tasks" ? state.liveTasks.filter((task) => !taskFinished(task)).length : 0;
  return `<button class="student-nav-item" data-tab="${id}" aria-current="${active ? "page" : "false"}">${studentIcon(id)}<span>${label}</span>${count ? `<em>${count}</em>` : ""}</button>`;
}

function renderStudent() {
  const page = state.studentPage;
  const section = page === "workspace" ? "任务作答" : page === "diagnostic" ? "独立短诊"
    : page === "result" ? "任务反馈" : ({ tasks: "我的任务", companion: "学伴", mine: "我的学习", feedback: "教师反馈" })[state.studentTab];
  const name = state.who || "同学";
  const navItems = studentNav("tasks", "我的任务") + studentNav("companion", "学伴") + studentNav("mine", "我的学习") + studentNav("feedback", "教师反馈");
  const logoutOpen = state.sheet === "logout";
  view.innerHTML = `
    <div class="student-app" data-student-shell>
      <aside class="student-sidebar" ${logoutOpen ? "inert" : ""}>
        <div class="student-brand"><svg class="student-brand-mark" viewBox="0 0 48 48" fill="none" aria-hidden="true"><path d="M9 34 23 8l15 26M16 24h15" stroke="currentColor" stroke-width="3"/><ellipse cx="24" cy="25" rx="22" ry="8" transform="rotate(-26 24 25)" stroke="currentColor" stroke-width="1.2"/><circle cx="40" cy="16" r="3" fill="currentColor"/></svg><span><strong>伴星导学</strong><small>学习工作台</small></span></div>
        <nav class="student-nav" aria-label="学生导航">${navItems}</nav>
        <div class="student-sidebar-note"><span>每一步，都有记录。</span><p>从独立作答到读懂反馈，<br>把进步留在自己的学习中。</p></div>
        <div class="student-account"><span class="student-avatar" aria-hidden="true">${escapeHtml(Array.from(name)[0])}</span><div><strong>${escapeHtml(name)}</strong><small>学生账号</small></div><button class="student-signout" data-action="logout" aria-label="退出登录" ${state.loggingOut ? "disabled" : ""}>${studentIcon("logout")}</button></div>
      </aside>
      <div class="student-desk" ${logoutOpen ? "inert" : ""}>
        <header class="student-topbar"><div class="student-breadcrumb">学习空间 <span>/</span> ${section}</div><div class="student-session"><i></i>${state.loading ? "正在同步" : "正式学习记录"}<time>${new Date().toLocaleDateString("zh-CN", { month: "long", day: "numeric", weekday: "long" })}</time></div></header>
        <div class="student-columns">
          <main class="student-main" id="student-main" tabindex="-1" aria-label="${section}">
            ${state.live ? learningNotifications.render() : ""}
            ${page === "workspace" ? workspace() : page === "diagnostic" ? diagnosticPage() : page === "result" ? resultPage() : studentTab()}
          </main>
          <aside class="student-context" aria-label="学习信息">${studentContext()}</aside>
        </div>
      </div>
      <nav class="student-mobile-nav" aria-label="学生快捷导航" ${logoutOpen ? "inert" : ""}>${navItems}<button class="student-nav-item" data-action="logout" aria-label="退出登录" ${state.loggingOut ? "disabled" : ""}>${studentIcon("logout")}<span>退出</span></button></nav>
      ${state.sheet ? sheet() : ""}
    </div>`;
}

function studentHeading(title, text, refresh = false) {
  return `<header class="student-page-heading"><div><p class="student-date">${escapeHtml(state.courseName || "自己的节奏，清晰的每一步")}</p><h1>${escapeHtml(title)}</h1><p>${escapeHtml(text)}</p></div>${refresh ? `<button class="btn" data-action="reload" ${state.loading ? "disabled" : ""}>${studentIcon("refresh")}刷新任务</button>` : ""}</header>`;
}

function studentTab() {
  if (state.studentTab === "tasks") return tasks();
  if (state.studentTab === "mine") return mine();
  if (state.studentTab === "feedback") return studentFeedback.render();
  return companion();
}

function studentContext() {
  const task = state.liveTask;
  const completed = state.liveTasks.filter((item) => item.status === "results_ready").length;
  const goal = state.liveGoals.find((item) => item.id === task?.goal_id);
  const workspace = state.studentPage === "workspace";
  const questions = task?.questions || [];
  const answered = workspace ? questions.filter((question, index) => !answerMissing(state.answers[index], question)).length : 0;
  return `<section class="student-context-block"><p class="student-context-label">${workspace ? "作答进度" : "学习概况"}</p><h2${workspace ? " data-answer-progress" : ""}>${workspace ? `${answered} / ${questions.length} 题` : "让进步看得见"}</h2>
      ${workspace ? `<div class="student-progress-line"><span data-answer-progress-bar style="width:${questions.length ? answered / questions.length * 100 : 0}%"></span></div><div class="q-nav" aria-label="题目导航">${questions.map((question, index) => `<button class="chip" data-q="${index}" aria-label="第 ${index + 1} 题${answerMissing(state.answers[index], question) ? "，未作答" : "，已作答"}" aria-pressed="${state.currentQ === index}">${index + 1}</button>`).join("")}</div><p class="student-draft-state" role="status">${escapeHtml(studentSaveLabel())}</p>`
        : `<div class="student-stat-pair"><div><strong>${state.liveTasks.length}</strong><small>已分配任务</small></div><div><strong>${completed}</strong><small>已完成任务</small></div></div>`}
    </section>
    <section class="student-context-block"><p class="student-context-label">${task ? "当前学习目标" : "学习提示"}</p><h2>${escapeHtml(task ? taskTitle(task) : "从一个任务开始")}</h2>${goal?.description ? `<p class="student-context-note">${escapeHtml(goal.description)}</p>` : ""}
      ${task ? `<dl><div><dt>题目数量</dt><dd>${taskQuestionCount(task)} 题</dd></div><div><dt>截止时间</dt><dd>${studentDate(task.deadline_at, true)}</dd></div><div><dt>当前状态</dt><dd>${taskStatus(task)}</dd></div><div><dt>重做</dt><dd>${task.submission_closed ? "已关闭提交，不可重做" : task.allow_retries ? "教师允许" : "本轮未开放"}</dd></div></dl>` : '<p class="student-context-note">老师分配任务后，会显示在这里。尚无记录时不推算掌握程度。</p>'}
    </section>
    <section class="student-context-block"><p class="student-context-label">学习记录的约定</p><ul class="student-evidence-list"><li>${studentIcon("check")}<span>首次成绩单独保留</span></li><li>${studentIcon("check")}<span>评分完成后开放解析</span></li><li>${studentIcon("check")}<span>提示与独立作答分开记录</span></li></ul><p class="student-context-note">每个判断都来自你的实际记录，记录不足时保持待评估。</p></section>`;
}

function companion() {
  const conversations = state.liveConversations;
  if (!state.live) return `${studentHeading("学伴", "当前服务不可用，正式对话暂时无法连接。")}
    <div class="state-banner warn" role="status">离线样例不发送消息，也不会伪装成真实回答。</div>`;
  if (state.activeConversationId) return chatConversation();
  if (state.chatContext?.taskId) return newTaskChat();
  return `${studentHeading("学伴", "从当前任务出发，找到下一步。")}
    <section class="student-companion"><div class="student-companion-heading">${studentIcon("companion")}<h2>学伴对话</h2><p>回答会依照当前学习任务的开放范围提供。</p></div>
      <div class="student-policy-banner ${state.chatPolicy?.allow_generation ? "allowed" : "guided"}" role="status">${escapeHtml(companionPolicyLabel())}</div>
      ${state.chatPolicyError ? `<div class="state-banner warn" role="status">${escapeHtml(state.chatPolicyError)}</div>` : ""}
      <form class="student-new-chat" data-action="new-chat"><label for="student-new-chat-title">会话名称</label><input id="student-new-chat-title" name="title" maxlength="200" value="${escapeHtml(state.pendingChatCreate?.title || "")}" placeholder="例如：一元一次方程"><button type="submit" class="btn primary" ${state.chatCreating ? "disabled" : ""}>${state.chatCreating ? "正在创建…" : "新建对话"}</button></form>
      <div class="student-companion-actions"><button type="button" class="btn" data-action="toggle-archived">${state.chatArchived ? "查看进行中的对话" : "查看已归档"}</button><button type="button" class="btn" data-tab="tasks">返回任务</button></div>
    </section>
    <div class="student-section-head"><h2>我的对话记录</h2><small>${conversations.length} 个会话</small></div>
    ${state.conversationError ? `<div class="state-banner warn" role="alert">${escapeHtml(state.conversationError)}<button type="button" class="btn" data-action="reload-conversations">重新读取</button></div>` : ""}
    ${conversations.length ? `<div class="student-task-list">${conversations.map((item) => `<article class="student-task-row"><span class="student-task-state">${studentIcon("companion")}</span><div class="student-task-copy"><h3>${escapeHtml(item.title || "学习对话")}</h3><p>${item.updated_at ? studentDate(item.updated_at, true) : "已保存的对话"}${item.status === "archived" ? " · 已归档" : ""}</p></div><button type="button" class="btn" data-conversation="${escapeHtml(item.id)}">${item.status === "archived" ? "恢复并继续" : "继续对话"}</button></article>`).join("")}</div>` : state.conversationError ? "" : '<section class="student-empty"><h2>还没有对话记录</h2><p>新建一段对话，学习记录会自动保存。</p></section>'}
    ${state.chatHasMoreConversations ? '<button type="button" class="btn student-load-older" data-action="more-conversations">加载更多会话</button>' : ""}`;
}

function newTaskChat() {
  const context = state.chatContext;
  if (context?.intent === "hint") return restrictedHintRequestView(context);
  const task = state.liveTasks.find((item) => item.id === context.taskId) || state.liveTask;
  const question = task?.questions?.find((item) => questionQuestionVersion(item) === context.questionVersionId);
  const text = context.prompt || state.chatDraft || "";
  return `${studentHeading("问学伴", "检查题目与自己的尝试，再决定发送哪些内容。")}
    <section class="student-chat-context-form">
      <div class="student-policy-banner ${state.chatPolicy?.allow_generation ? "allowed" : "guided"}" role="status">${escapeHtml(companionPolicyLabel())}</div>
      <div class="student-chat-context"><span>${escapeHtml(taskTitle(task || {}))} · 当前题目</span><p>${escapeHtml(question?.stem || context.stem || "任务中的一道题")}</p>
        ${context.options ? `<p class="student-chat-options">${escapeHtml(context.options)}</p>` : ""}</div>
      <form class="student-compose student-context-compose" data-action="send-context">
        <label class="sr-only" for="student-context-prompt">检查并编辑要发送的内容</label>
        <textarea id="student-context-prompt" name="text" rows="7" maxlength="20000" data-chat-input ${context.pending ? "readonly" : ""}>${escapeHtml(context.pending?.text || text)}</textarea>
        ${context.pending ? '<p class="student-compose-note">发送结果待确认，重试会使用同一请求与内容。</p>' : ""}
        <div class="student-context-compose-actions"><button type="button" class="btn" data-action="back-to-task">返回作答</button><button type="submit" class="btn primary" ${state.chatCreating || state.chatSending || state.chatPolicyLoading ? "disabled" : ""}>${state.chatCreating ? "正在准备对话…" : state.chatSending ? "正在发送…" : context.pending ? "用同一请求重试" : "新建对话并发送"}</button></div>
      </form>
      ${state.chatResponseError ? `<div class="state-banner warn" role="alert">${escapeHtml(state.chatResponseError)}<button type="button" class="btn" data-action="retry-context-send">重试发送</button></div>` : ""}
    </section>`;
}

function companionPolicyLabel(policy = state.chatPolicy) {
  if (state.chatPolicyLoading) return "正在核验当前学习范围…";
  if (!policy) return "暂时无法核验学习范围，请稍后重试。";
  if (policy.diagnostic) return "首诊期间仅提供独立尝试引导，不提供解题提示。";
  if (state.chatContext?.intent === "hint") return policy.allow_hint_content
    ? "本题尝试已保存，可查看知识提示或下一步方向；不提供答案。"
    : "当前题目提示暂未开放，可查看经服务器核验的已有记录。";
  if (policy.allow_generation) return "当前所有任务均已开放，可自由提问。";
  if (!policy.attempted) return "先独立尝试并保存作答；当前仅提供安全引导。";
  return "仍有任务未完成；当前仅提供安全引导，不生成题目解答。";
}

function chatConversation() {
  const conversation = state.liveConversations.find((item) => item.id === state.activeConversationId) || state.activeConversation;
  const task = state.chatContext?.taskId ? state.liveTasks.find((item) => item.id === state.chatContext.taskId) || state.liveTask : null;
  const messages = state.chatMessages.filter((item) => item.role === "user").map((item) => {
    const response = item.response ? projectChatResponse(item.response) : null;
    const userText = item.redacted ? item.notice || "当前内容不可查看。" : item.text ?? item.content ?? "";
    const rows = [`<article class="student-message user" data-message-id="${escapeHtml(item.id || item.localId || "")}"><strong>我</strong><p>${escapeHtml(userText)}</p></article>`];
    if (!response && item.role === "user") rows.push('<article class="student-message assistant pending"><strong>学伴</strong><p>正在等待服务确认…</p></article>');
    if (response) {
      const body = response.redacted ? response.notice || "内容暂未开放。" : response.text || "";
      const status = chatResponseStatus(response);
      rows.push(`<article class="student-message assistant${["failed", "cancelled", "revoked"].includes(response.status) ? " failed" : ""}" data-response-id="${escapeHtml(response.response_id || "")}" data-response-status="${escapeHtml(response.status)}"><strong data-response-title>${escapeHtml(chatResponseTitle(response))}</strong><p data-response-text>${escapeHtml(body)}</p><small data-response-status role="status">${escapeHtml(status)}</small>${response.redacted ? '<p class="student-policy-notice">回答范围已按当前任务规则收起。</p>' : ""}${chatResponseControls(response)}</article>`);
    }
    return rows.join("");
  }).join("");
  const pending = state.chatPendingSends?.[state.activeConversationId];
  const responding = state.chatMessages.some((item) => item.response?.response_id && chatResponseActive(item.response));
  const paused = state.chatMessages.some((item) => item.response && chatResponsePaused(item.response));
  return `${studentHeading(conversation?.title || "学习对话", "对话记录会保存在当前账号。")}
    <section class="student-chat">
      <header class="student-chat-toolbar"><div class="student-chat-toolbar-leading"><button type="button" class="btn" data-action="chat-back">${studentIcon("arrow")}全部对话</button>${state.chatContext?.taskId ? '<button type="button" class="btn" data-action="back-to-task">返回作答</button>' : ""}</div><div class="student-chat-toolbar-actions"><button type="button" class="btn" data-action="rename-conversation" aria-expanded="${state.chatRenameOpen}">重命名</button><button type="button" class="btn" data-action="new-conversation">新建</button><button type="button" class="btn" data-action="archive-conversation">归档</button></div></header>
      ${state.chatRenameOpen ? `<form class="student-chat-rename" data-action="rename-chat"><label for="student-chat-rename-title">会话名称</label><input id="student-chat-rename-title" name="title" maxlength="200" value="${escapeHtml(state.chatRenameTitle)}" data-chat-rename-input ${state.chatRenaming ? "disabled" : ""}>${state.chatRenameError ? `<p class="student-chat-rename-error" role="alert">${escapeHtml(state.chatRenameError)}</p>` : ""}<div><button type="button" class="btn" data-action="cancel-rename" ${state.chatRenaming ? "disabled" : ""}>取消</button><button type="submit" class="btn primary" ${state.chatRenaming ? "disabled" : ""}>${state.chatRenaming ? "正在保存…" : "保存名称"}</button></div></form>` : ""}
      <div class="student-policy-banner ${state.chatPolicy?.allow_generation ? "allowed" : "guided"}" role="status">${escapeHtml(companionPolicyLabel())}</div>
      ${state.chatContext?.questionVersionId ? `<div class="student-chat-context"><span>当前题目</span><p>${escapeHtml(task?.questions?.find((question) => questionQuestionVersion(question) === state.chatContext.questionVersionId)?.stem || state.chatContext.stem || "任务中的一道题")}</p></div>` : ""}
      ${state.chatResponseError ? `<div class="state-banner warn" role="alert">${escapeHtml(state.chatResponseError)}<button type="button" class="btn" data-action="${pending ? "retry-chat-send" : "reload-conversation"}">${pending ? "重试原请求" : "恢复记录"}</button></div>` : ""}
      ${state.chatPolicyError ? `<div class="state-banner warn" role="status">${escapeHtml(state.chatPolicyError)}</div>` : ""}
      <div class="student-chat-thread" data-chat-thread aria-live="polite" aria-relevant="additions text">
        ${state.chatHasOlder ? '<button type="button" class="btn student-load-older" data-action="load-older-messages" data-loading="' + String(state.chatLoadingOlder) + '" ' + (state.chatLoadingOlder ? "disabled" : "") + '>加载更早的消息</button>' : ""}
        ${messages || '<p class="student-empty-chat">这段对话还没有消息。写下你正在思考的问题。</p>'}
      </div>
      ${state.chatContext?.intent === "hint" ? '<p class="student-compose-note">这是本题的受限提示记录。返回作答修改尝试或请求另一级提示；自由聊天仍遵循原学习范围。</p>' : `<form class="student-compose" data-action="send" data-conversation-form="${escapeHtml(state.activeConversationId)}">
        <label class="sr-only" for="student-chat-input">发送给学伴</label><textarea id="student-chat-input" name="text" rows="2" maxlength="20000" placeholder="写下你的问题或想法…" data-chat-input ${pending ? "readonly" : ""}>${escapeHtml(pending?.text || state.chatDraft)}</textarea>
        <button type="submit" class="btn primary" ${state.chatSending || responding ? "disabled" : ""}>${studentIcon("arrow")}${state.chatSending ? "发送中…" : pending ? "重试" : paused ? "回答已暂停" : responding ? "回答中…" : "发送"}</button>
      </form>`}
      ${state.chatPendingSends?.[state.activeConversationId] ? '<p class="student-compose-note">发送结果未确认。重试会沿用同一请求与内容，不会重复创建消息。</p>' : ""}
    </section>`;
}

function questionQuestionVersion(question) {
  return question?.question_version_id || (question?.id && question?.version ? `${question.id}:${question.version}` : "");
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
  const upcoming = !task.submission_closed && task.scheduled_at && task.scheduled_at * 1000 > Date.now();
  const label = task.submission_closed ? "查看记录" : upcoming ? "等待开放" : task.diagnostic_required ? "开始短诊" : taskFinished(task) ? "查看结果"
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
    <div class="student-task-list">${tasks.length ? tasks.map((item) => `<article class="student-task-row" data-selected="${task.id === item.id}"><span class="student-task-state">${studentIcon(taskFinished(item) ? "check" : "tasks")}</span><div class="student-task-copy"><h3>${escapeHtml(taskTitle(item))}</h3><p>${taskQuestionCount(item)} 道题 · ${studentDate(item.deadline_at, true)}</p></div><div class="student-task-side"><strong>${taskStatus(item)}</strong><small>${item.submission_closed ? "已关闭提交，保留历史记录" : item.allow_retries ? "可在评分后重做" : "首次作答单独记录"}</small></div>${studentTaskButton(item)}</article>`).join("") : '<section class="student-empty"><h2>这个分类暂时没有任务</h2><p>可以切换到“全部”查看其他任务。</p></section>'}</div>`;
}

function mine() {
  const records = state.liveTasks.flatMap((task) => (task.attempts || []).map((attempt) => ({ task, attempt })))
    .sort((a, b) => (b.attempt.submitted_at || 0) - (a.attempt.submitted_at || 0));
  return `${studentHeading("我的学习", "先看具体的作答与反馈，让每一步进步都有来处。")}
    ${state.liveError ? `<div class="state-banner bad">${escapeHtml(state.liveError)}</div>` : ""}
    <div class="student-section-head"><h2>作答记录</h2><small>${records.length} 次真实提交</small></div>
    ${records.length ? `<div class="student-record-list">${records.map(({ task, attempt }) => `<article><div><span class="tag${attempt.status === "graded" ? " ok" : " warn"}">${attempt.kind === "first" ? "首次作答" : "重做"}</span><h3>${escapeHtml(taskTitle(task))}</h3><p>${studentDate(attempt.submitted_at, true)} · ${attempt.independent_evidence ? "独立作答记录" : "复习作答记录"}</p></div><div><strong>${attempt.total_score !== undefined ? `${attempt.total_score} / ${attempt.max_score}` : "等待评分"}</strong><p>${attempt.status === "graded" ? "已完成评分" : "完整评分后开放反馈"}</p></div><button class="btn" data-action="open-task" data-task-id="${escapeHtml(task.id)}" data-attempt-id="${escapeHtml(attempt.id)}">查看反馈</button></article>`).join("")}</div>` : '<section class="student-empty">' + studentIcon("mine") + '<h2>第一份学习记录，等你写下</h2><p>完成任务并交卷后，首次成绩、重做与具体反馈会保存在这里。</p><button class="btn primary" data-tab="tasks">去看我的任务</button></section>'}
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

function questionType(question) {
  return question?.type || (question?.options?.length ? "single_choice" : "text");
}

function questionBlankCount(question) {
  if (questionType(question) !== "fill") return 1;
  // 空数只来自公开作答格式，参考答案数组也可能只是等价答案，不能用于推断。
  const count = question.blank_count ?? question.answer_format?.blank_count;
  return Number.isInteger(count) && count > 0 && count <= 32 ? count : 1;
}

function answerMissing(value, question) {
  if (value === undefined || value === null || typeof value === "string" && !value.trim()) return true;
  const count = questionBlankCount(question);
  if (count > 1 && (!Array.isArray(value) || value.length !== count)) return true;
  return Array.isArray(value) && (!value.length || Array.from(value).some((item) => answerMissing(item)));
}

function questionChoiceValue(question, chosen, value) {
  const type = questionType(question);
  if (type === "judge") return value === "true" ? true : value === "false" ? false : undefined;
  const index = Number(value);
  if (!["single_choice", "multi_choice"].includes(type) || !Number.isInteger(index)
    || index < 0 || index >= (question?.options?.length || 0)) return undefined;
  if (type !== "multi_choice") return index;
  const selected = Array.isArray(chosen) ? chosen : [];
  return selected.includes(index) ? selected.filter((item) => item !== index) : [...selected, index].sort((a, b) => a - b);
}

function questionInputValue(question, chosen, value, blank) {
  const count = questionBlankCount(question);
  if (count === 1) return value;
  const index = Number(blank);
  if (!Number.isInteger(index) || index < 0 || index >= count) return undefined;
  return Array.from({ length: count }, (_, position) => position === index ? value : String((Array.isArray(chosen) ? chosen[position] : "") ?? ""));
}

function questionAnswerText(question, value) {
  const type = questionType(question);
  if (type === "fill" && questionBlankCount(question) > 1 && Array.isArray(value)) {
    return Array.from({ length: questionBlankCount(question) }, (_, index) => `第 ${index + 1} 空：${answerMissing(value[index]) ? "未作答" : String(value[index])}`).join("；");
  }
  if (answerMissing(value)) return "未作答";
  if (type === "judge") return value === true ? "正确" : value === false ? "错误" : String(value);
  if (type === "single_choice" || type === "multi_choice") {
    const optionText = (index) => Number.isInteger(index) && question.options?.[index] !== undefined
      ? `${String.fromCharCode(65 + index)}. ${question.options[index]}` : String(index);
    return (Array.isArray(value) ? value : [value]).map(optionText).join("；");
  }
  return Array.isArray(value) ? value.map(String).join(type === "fill" ? " 或 " : "；") : String(value);
}

function refreshAnswerProgress(diagnostic = false) {
  const questions = (diagnostic ? state.diagnostic : state.liveTask)?.questions || [];
  const answers = diagnostic ? state.diagnosticAnswers : state.answers;
  const answered = questions.filter((question, index) => !answerMissing(answers[index], question)).length;
  const count = view.querySelector("[data-answer-progress]");
  const progress = view.querySelector("[data-answer-progress-bar]");
  if (!diagnostic && count) count.textContent = `${answered} / ${questions.length} 题`;
  if (!diagnostic && progress) progress.style.width = `${questions.length ? answered / questions.length * 100 : 0}%`;
  view.querySelectorAll(diagnostic ? '[aria-label="短诊题目"] [data-q]' : '[aria-label="题目导航"] [data-q]').forEach((node) => {
    const index = Number(node.dataset.q);
    const unknown = diagnostic && state.diagnosticUnknown?.includes(questions[index]?.id);
    const complete = !answerMissing(answers[index], questions[index]);
    node.setAttribute("aria-label", `第 ${index + 1} 题${unknown ? "，暂时不会" : complete ? "，已作答" : "，未作答"}`);
    if (diagnostic) node.textContent = `${index + 1}${unknown ? " · 暂时不会" : complete ? " · 已答" : ""}`;
  });
}

/** 连上后端时的作答区：题目、选项、保存、交卷都是真的。 */
function questionChoices(question, chosen, locked, diagnostic = false) {
  const type = questionType(question);
  const target = diagnostic ? "diagnostic" : "task";
  const choose = diagnostic ? "data-diagnostic-choose" : "data-choose";
  if (type === "judge") {
    return `<div class="options" role="group" aria-label="判断正误">${[true, false].map((value) => `<button type="button" class="option" ${choose}="${value}" aria-pressed="${chosen === value}" ${locked ? "disabled" : ""}><span>${value ? "正确" : "错误"}</span></button>`).join("")}</div>`;
  }
  if (type === "single_choice" || type === "multi_choice") {
    const multiple = type === "multi_choice";
    return `${multiple ? '<p class="student-compose-note">请选择所有符合的选项，再次点击可以取消。</p>' : ""}<div class="options" role="group" aria-label="${multiple ? "选择答案，可多选" : "选择答案"}">${(question.options || []).map((option, index) => `<button type="button" class="option" ${choose}="${index}" aria-pressed="${multiple ? Array.isArray(chosen) && chosen.includes(index) : chosen === index}" ${locked ? "disabled" : ""}><b>${String.fromCharCode(65 + index)}</b><span>${escapeHtml(option)}</span></button>`).join("")}</div>`;
  }
  if (type === "fill") {
    const count = questionBlankCount(question);
    return `<div class="student-fill-answers">${Array.from({ length: count }, (_, index) => `<label class="field">${count > 1 ? `第 ${index + 1} 空` : diagnostic ? "独立填空" : "填空作答"}<input type="text" data-answer-input="${target}"${count > 1 ? ` data-answer-blank="${index}"` : ""} value="${escapeHtml((count > 1 ? Array.isArray(chosen) ? chosen[index] : "" : chosen) ?? "")}" placeholder="填写数值、公式或文字" autocomplete="off" spellcheck="false" ${locked ? "readonly" : ""}></label>`).join("")}</div>`;
  }
  return `<label class="field">${diagnostic ? "独立作答" : "这一题的作答"}<textarea data-answer-input="${target}" rows="5" ${locked ? "readonly" : ""}>${escapeHtml(chosen ?? "")}</textarea></label>`;
}

function workspaceLive() {
  const task = state.liveTask;
  const questions = task?.questions || [];
  if (!questions.length) return `${studentHeading("任务作答", "正式卷还没有开放。")}
    <div class="state-banner warn">${escapeHtml(task?.notice || "没有可作答的题目，请返回任务列表。")}</div><button class="btn" data-tab="tasks">返回任务</button>`;
  const locked = studentAnswerLocked();
  const write = state.studentWrites?.[task.id];
  const current = Math.min(state.currentQ, questions.length - 1);
  const question = questions[current];
  return `<div class="student-workspace">
      <header class="student-workspace-head"><button class="btn" data-tab="tasks">返回任务</button><span class="tag">${state.mode === "focus" ? "专注作答" : "独立尝试"}</span><button class="linkish" data-action="toggle-mode">${state.mode === "focus" ? "显示学习提示" : "进入专注模式"}</button></header>
      ${studentHeading(taskTitle(task), "按自己的节奏作答，保存后可以下次继续。")}
      ${task.submission_closed ? '<div class="state-banner warn">老师已关闭提交，当前作答只读。已保存的记录仍可查看。</div>' : ""}
      <section class="student-question-card" aria-label="第 ${current + 1} 题"><div class="student-question-meta"><span>第 ${current + 1} 题 / 共 ${questions.length} 题</span><span>${question.points ?? "—"} 分</span></div><h2 class="formula">${escapeHtml(question.stem)}</h2>
        <div class="state-banner warn" data-draft-recovery ${write?.blocked ? "" : "hidden"}><p>版本或编辑权限已变化。本页输入仍保留，选择恢复方式后才能继续保存。</p><button class="btn" data-action="recover-draft">保留本页作答并接管</button><button class="btn" data-action="reload-server-draft">放弃本页作答，读取服务器草稿</button></div>
        ${questionChoices(question, state.answers[current], locked)}
        <div class="student-question-footer"><div class="row"><button class="btn" data-q="${current - 1}" ${current === 0 || state.loading ? "disabled" : ""}>上一题</button><button class="btn" data-q="${current + 1}" ${current === questions.length - 1 || state.loading ? "disabled" : ""}>下一题</button></div><span class="student-draft-state">${escapeHtml(studentSaveLabel())}</span></div>
      </section>
      ${state.mode === "tutor" ? restrictedHintControls() : ""}
        <div class="student-focus-actions"><button class="btn" data-action="save-draft" ${locked ? "disabled" : ""}>保存进度</button><button class="btn" data-action="ask-companion" ${locked ? "disabled" : ""}>${studentIcon("companion")}问学伴</button><button class="btn primary" data-action="submit" ${locked ? "disabled" : ""}>确认交卷</button></div>

    </div>`;
}

function diagnosticPage() {
  const diagnostic = state.diagnostic;
  const questions = diagnostic?.questions || [];
  const current = Math.min(state.currentQ, Math.max(0, questions.length - 1));
  const question = questions[current], unknown = state.diagnosticUnknown || [];
  return `${studentHeading("先做一次独立短诊", "每题作答或明确选择“暂时不会”。短诊不计入任务成绩，完成后开放正式卷。")}
    <p class="state-banner warn" role="status">短诊未提交内容仅保留在本页内存，尚未保存到服务器；刷新、关闭页面或退出会丢失。</p>
    ${state.diagnosticCommand ? '<p class="state-banner warn" role="status">短诊结果待确认，作答已锁定；重试沿用原请求与内容。</p>' : ""}
    ${question ? `<div class="q-nav" aria-label="短诊题目">${questions.map((item, index) => `<button class="chip" data-q="${index}" aria-pressed="${current === index}">${index + 1}${unknown.includes(item.id) ? " · 暂时不会" : !answerMissing(state.diagnosticAnswers[index], item) ? " · 已答" : ""}</button>`).join("")}</div><section class="student-question-card"><div class="student-question-meta"><span>第 ${current + 1} / ${questions.length} 题</span><span>独立短诊</span></div><h2 class="formula">${escapeHtml(question.stem)}</h2>${questionChoices(question, state.diagnosticAnswers[current], studentAnswerLocked(true), true)}<button type="button" class="btn" data-diagnostic-unknown aria-pressed="${unknown.includes(question.id)}" ${studentAnswerLocked(true) ? "disabled" : ""}>暂时不会</button><p class="student-compose-note">选择“暂时不会”会清空本题作答；重新作答会取消该标记。</p></section><div class="student-focus-actions"><button class="btn" data-tab="tasks">返回任务</button><button class="btn primary" data-action="submit-diagnostic" ${state.loading || state.studentComposing ? "disabled" : ""}>${state.loading ? "正在提交" : state.diagnosticCommand ? "重试原短诊请求" : "提交短诊"}</button></div>` : '<div class="state-banner warn">暂时没有短诊题目，请返回任务后重试。</div><button class="btn" data-tab="tasks">返回任务</button>'}`;
}

/** 连上后端时的结果页：分数与逐题对错都来自真实评卷。 */
function resultPageLive() {
  const task = state.liveTask;
  const attempts = task?.attempts || [];
  const attempt = state.selectedAttemptId ? attempts.find((item) => item.id === state.selectedAttemptId) : attempts[attempts.length - 1];
  const refreshNotice = state.banner === "grading-refresh-failed"
    ? "评分状态暂时未读到，自动刷新已暂停。后台仍会继续处理，请点击“刷新结果”重新读取。"
    : state.banner === "grading-refresh-stopped" ? "评分仍在后台处理，自动刷新已暂停，请稍后点击“刷新结果”。" : "";
  const heading = `<div class="student-workspace-head"><button class="btn" data-tab="tasks">返回任务</button><span class="tag">${taskStatus(task || {})}</span></div>${studentHeading(task ? taskTitle(task) : "本轮结果", "把每次作答和具体反馈放在一起看。")}${refreshNotice ? `<div class="state-banner warn" role="status">${refreshNotice}</div>` : ""}`;
  if (!task || !attempt) return `${heading}<div class="state-banner warn">${state.selectedAttemptId ? "这次作答记录暂时不可用，请返回我的学习后重试。" : task?.submission_closed ? "老师已关闭提交，这份任务没有已交卷记录。已保存的草稿继续保留。" : "还在评卷，成绩稍后出现在这里。"}</div><button class="btn" data-action="refresh-task" ${state.loading ? "disabled" : ""}>刷新结果</button>`;
  const feedbackLink = `<div class="student-focus-actions"><button class="btn" data-action="open-feedback" data-task-id="${escapeHtml(task.id)}" data-attempt-id="${escapeHtml(attempt.id)}">教师短评与评分异议</button></div>`;
  const summary = `<section class="student-result-summary"><div class="student-score">${attempt.total_score ?? "—"}<small> / ${attempt.max_score ?? "—"}</small></div><div><h2>${attempt.kind === "first" ? "首次成绩" : "重做成绩"}</h2><p>${attempt.independent_evidence ? "独立作答与提示记录分开保留。" : "这次是复习记录，首次成绩保持不变。"}</p><small>${studentDate(attempt.submitted_at, true)}</small></div></section>`;
  if (!task.answer_released) return `${heading}${summary}<div class="state-banner warn">整轮评分完成前，不显示对错和解析。</div>${feedbackLink}<button class="btn" data-action="refresh-task" ${state.loading ? "disabled" : ""}>刷新结果</button>`;
  const byId = Object.fromEntries((attempt.items || []).map((item) => [item.question_id, item]));
  return `${heading}${summary}<div class="student-section-head"><h2>逐题反馈</h2><small>${task.questions?.length || 0} 道题</small></div><div class="student-result-list">${(task.questions || []).map((question, index) => {
    const item = byId[question.id] || {};
    const yours = attempt.answers?.[question.id];
    const answerText = (value) => questionAnswerText(question, value);
    return `<section class="panel"><div class="student-result-heading"><h3>第 ${index + 1} 题</h3><span class="tag${item.score === undefined ? " warn" : item.score >= question.points ? " ok" : ""}">${item.score === undefined ? "待评分" : `${item.score} / ${question.points} 分`}</span></div><p class="formula">${escapeHtml(question.stem)}</p><p class="muted">你的作答：${escapeHtml(answerText(yours))}</p>${question.answer !== undefined ? `<p>参考答案：${escapeHtml(answerText(question.answer))}</p>` : ""}${item.result?.why ? `<p class="muted">判定：${escapeHtml(item.result.why)}</p>` : ""}${question.explanation ? `<p>${escapeHtml(question.explanation)}</p>` : ""}</section>`;
  }).join("")}</div>${feedbackLink}${task.status === "results_ready" && task.allow_retries && !task.submission_closed ? '<button class="btn" data-action="retry">再做一次，保留首次成绩</button>' : ""}`;
}

function sheet() {
  if (state.sheet === "logout") return `<div class="drawer-back" data-action="cancel-logout" aria-hidden="true"></div><section class="sheet student-logout-dialog" role="dialog" aria-modal="true" aria-labelledby="student-logout-title" aria-describedby="student-logout-note" tabindex="-1"><h2 class="section" id="student-logout-title">退出登录？</h2><p id="student-logout-note">仍有仅保留在本页的作答、未发送消息或未确认的聊天请求。退出成功后，这些本页内容会丢失，已保存的学习记录会保留。</p><div class="row"><button class="btn" data-action="cancel-logout">继续编辑</button><button class="btn primary" data-action="confirm-logout">确认退出</button></div></section>`;
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
      if (state.studentPage === "workspace" || state.studentPage === "diagnostic") {
        if (!leaveStudentWork()) return;
      }
      if (state.studentTab === "feedback" && !studentFeedback.leave()) return;
      notificationOpenSequence += 1;
      state.sheet = null;
      if (state.studentTab === "companion") closeStudentChatStream();
      state.studentTab = node.dataset.tab;
      state.studentPage = "home";
      if (state.studentTab !== "companion") setChatRoute(null);
      if (state.studentTab === "companion") {
        if (state.activeConversationId) {
          loadConversation(state.activeConversationId);
          return;
        }
        loadConversationList();
        refreshCompanionPolicy();
      }
      if (state.studentTab === "feedback") studentFeedback.load();
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
    if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading) return;
    if (!leaveTeacherPage(node.dataset.view)) return;
    notificationOpenSequence += 1;
    state.teacherView = node.dataset.view;
    render();
    if (state.teacherView === "competition") teacherCompetition.load();
    if (state.teacherView === "materials") teacherLibrary.load();
    if (state.teacherView === "management") teacherManagement.load();
    if (state.teacherView === "review") teacherReview.load();
    if (state.teacherView === "reports") teacherReportWorkspace.load();
    if (state.teacherView === "insights") teacherInsightsWorkspace.load();
  }));
  view.querySelectorAll("[data-teacher-class]").forEach((node) => node.addEventListener("change", () => selectTeacherClass(node.value)));
  view.querySelectorAll("[data-open-student]").forEach((node) => node.addEventListener("click", () => {
    if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading || !leaveTeacherPage("student")) return;
    notificationOpenSequence += 1;
    state.openStudent = node.dataset.openStudent;
    state.teacherView = "student";
    render();
  }));
  view.querySelectorAll("[data-q]").forEach((node) => node.addEventListener("click", () => {
    if (state.loading || state.studentComposing || state.studentSubmitBusy || state.hintPreparing || node.disabled) return;
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
    if (studentAnswerLocked() || node.disabled) return;
    const question = state.liveTask?.questions?.[state.currentQ];
    const value = questionChoiceValue(question, state.answers[state.currentQ], node.dataset.choose);
    if (value === undefined) return;
    state.answers[state.currentQ] = value;
    rememberStudentDraft();
    render();
  }));
  view.querySelectorAll("[data-diagnostic-choose]").forEach((node) => node.addEventListener("click", () => {
    if (studentAnswerLocked(true) || node.disabled) return;
    const question = state.diagnostic?.questions?.[state.currentQ];
    const value = questionChoiceValue(question, state.diagnosticAnswers[state.currentQ], node.dataset.diagnosticChoose);
    if (value === undefined) return;
    state.diagnosticUnknown = (state.diagnosticUnknown || []).filter((id) => id !== question.id);
    state.diagnosticAnswers[state.currentQ] = value;
    rememberDiagnosticDraft();
    render();
  }));
  view.querySelectorAll("[data-diagnostic-unknown]").forEach((node) => node.addEventListener("click", () => {
    if (studentAnswerLocked(true) || node.disabled || !state.diagnostic) return;
    const question = state.diagnostic.questions[state.currentQ];
    const unknown = new Set(state.diagnosticUnknown || []);
    if (unknown.has(question.id)) unknown.delete(question.id);
    else { unknown.add(question.id); state.diagnosticAnswers[state.currentQ] = ""; }
    state.diagnosticUnknown = [...unknown];
    rememberDiagnosticDraft();
    render();
  }));
  view.querySelectorAll("[data-answer-input]").forEach((node) => node.addEventListener("input", () => {
    const diagnostic = node.dataset.answerInput === "diagnostic";
    if (studentAnswerLocked(diagnostic) || node.disabled || node.readOnly) return;
    const questions = (diagnostic ? state.diagnostic : state.liveTask)?.questions || [];
    const answers = diagnostic ? state.diagnosticAnswers : state.answers;
    const value = questionInputValue(questions[state.currentQ], answers[state.currentQ], node.value, node.dataset.answerBlank);
    if (value === undefined) return;
    answers[state.currentQ] = value;
    if (diagnostic) {
      state.diagnosticUnknown = (state.diagnosticUnknown || []).filter((id) => id !== questions[state.currentQ]?.id);
      rememberDiagnosticDraft();
    } else rememberStudentDraft();
    refreshAnswerProgress(diagnostic);
    if (!diagnostic) refreshStudentSaveStatus();
  }));
  view.querySelectorAll("[data-answer-input]").forEach((node) => {
    const taskId = state.liveTask?.id, diagnosticId = state.diagnostic?.id, generation = state.transitionId;
    const diagnostic = node.dataset.answerInput === "diagnostic", index = state.currentQ;
    const current = () => generation === state.transitionId && state.liveTask?.id === taskId
      && state.currentQ === index && (!diagnostic || state.diagnostic?.id === diagnosticId);
    node.addEventListener("compositionstart", () => {
      if (!current() || studentAnswerLocked(diagnostic) || node.disabled || node.readOnly) return;
      state.studentComposing = true;
      clearStudentSaveTimers(state.studentWrites?.[taskId]);
      refreshStudentSaveStatus();
    });
    node.addEventListener("compositionend", () => {
      if (!current()) return;
      state.studentComposing = false;
      if (!studentAnswerLocked(diagnostic) && !node.disabled && !node.readOnly) {
        const answers = diagnostic ? state.diagnosticAnswers : state.answers;
        const question = (diagnostic ? state.diagnostic : state.liveTask)?.questions?.[index];
        const value = questionInputValue(question, answers[index], node.value, node.dataset.answerBlank);
        if (value !== undefined) {
          answers[index] = value;
          if (diagnostic) {
            state.diagnosticUnknown = (state.diagnosticUnknown || []).filter((id) => id !== question.id);
            rememberDiagnosticDraft();
          } else rememberStudentDraft();
        }
      }
      refreshAnswerProgress(diagnostic);
      refreshStudentSaveStatus();
    });
  });
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
  view.querySelectorAll("form").forEach((form) => {
    if (!form.closest?.("[data-admin-workspace], [data-teacher-management], [data-teacher-review], [data-teacher-reports], [data-teacher-competition], [data-student-feedback], [data-learning-notifications]")) form.addEventListener("submit", onSubmit);
  });
  view.querySelectorAll("[data-chat-rename-input]").forEach((input) => input.addEventListener("input", () => {
    state.chatRenameTitle = input.value;
    state.chatRenameError = "";
  }));
  view.querySelectorAll("[data-chat-input]").forEach((input) => {
    const rememberDraft = () => {
      if (input.readOnly) return;
      state.chatDraft = input.value;
      state.chatDrafts[state.activeConversationId || "__new"] = input.value;
      if (!state.activeConversationId && state.chatContext) state.chatContext.prompt = input.value;
    };
    input.addEventListener("input", rememberDraft);
    input.addEventListener("compositionstart", () => { chatComposing = true; });
    input.addEventListener("compositionend", () => {
      // 最终 input 事件可能晚于 compositionend；先保留最终值，再重绘排队的回答。
      rememberDraft();
      chatComposing = false;
      if (chatRenderPending) renderStudentChatIfActive(chatEpoch);
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing && !chatComposing && event.keyCode !== 229) {
        event.preventDefault();
        input.form?.requestSubmit();
      }
    });
  });
  // 眼睛按钮：原生密码字段配合方块遮蔽，不重渲染，输入不会丢。
  view.querySelectorAll('[data-action="toggle-peek"]').forEach((node) => node.addEventListener("click", () => {
    const bar = node.closest(".passbar");
    const input = bar ? bar.querySelector("input") : null;
    if (!input || input.disabled) return;
    const masked = !bar.classList.contains("masked");
    input.type = masked ? "password" : "text";
    bar.classList.toggle("masked", masked);
    node.setAttribute("aria-pressed", String(masked));
    node.setAttribute("aria-label", masked ? "显示口令" : "隐藏口令");
    node.innerHTML = masked ? EYE_CLOSED : EYE_OPEN;
  }));
  teacherLibrary.bind(view);
  teacherCompetition.bind(view);
  teacherComposer.bind(view);
  adminWorkspace.bind(view);
  teacherManagement.bind(view);
  teacherReview.bind(view);
  teacherReportWorkspace.bind(view);
  teacherInsightsWorkspace.bind(view);
  studentFeedback.bind(view);
  learningNotifications.bind(view);
  const logoutDialog = view.querySelector(".student-logout-dialog");
  if (logoutDialog) {
    const cancel = logoutDialog.querySelector('[data-action="cancel-logout"]');
    const confirm = logoutDialog.querySelector('[data-action="confirm-logout"]');
    cancel.focus({ preventScroll: true });
    logoutDialog.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); onAction({ currentTarget: cancel }); }
      if (event.key === "Tab" && event.shiftKey && document.activeElement === cancel) { event.preventDefault(); confirm.focus(); }
      else if (event.key === "Tab" && !event.shiftKey && document.activeElement === confirm) { event.preventDefault(); cancel.focus(); }
    });
  }
  if (state.live && state.role === "student") refreshStudentSaveStatus();
}

async function finishLogout() {
  const generation = ++state.transitionId;
  workspaceRequest += 1;
  teacherLibrary.reset();
  teacherComposer.reset();
  adminWorkspace.reset();
  teacherManagement.reset();
  resetLearningWorkspaces();
  resetStudentWrites();
  closeStudentChatStream();
  setChatRoute(null);
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
    activeConversationId: null, chatMessages: [], chatDrafts: {}, chatContexts: {}, chatDraft: "", chatContext: null,
    chatPolicy: null, chatPolicyLoading: false, chatPolicyError: "", chatResponseError: "",
    chatOlderCursor: null, chatHasOlder: false, chatLoadingOlder: false, chatPendingSends: {},
    pendingChatCreate: null, chatCreating: false, chatRenameOpen: false, chatRenameTitle: "", chatRenameError: "", chatRenaming: false,
    chatArchived: false, chatHasMoreConversations: false, chatSending: false,
    conversations: [], activeChat: 0, openStudent: null, teacherView: "home", goalLabel: "", exportName: "",
    liveReportFormats: [], liveError: "", loading: false, courseId: null, courseName: "", classId: null, className: "",
    teacherCourses: [], teacherContextVersion: 0, reviewSelection: null,
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
  if (action === "cancel-logout") {
    if (state.sheet !== "logout" || state.loggingOut) return;
    state.sheet = null;
    render();
    view.querySelector("[data-chat-input]")?.focus({ preventScroll: true });
    return;
  }
  if (action === "toggle-peek") return; // 由 bind 里的专用处理器接管，避免整屏重渲染丢输入
  if (action === "reveal") {
    animateReveal(1);
    return;
  }
  if (action === "logout" || action === "confirm-logout") {
    if (state.loggingOut || state.transition || action === "confirm-logout" && state.sheet !== "logout") return;
    if (!leaveStudentWork({ logout: true, confirmed: action === "confirm-logout" })) return;
    const generation = state.transitionId;
    state.sheet = null;
    state.loggingOut = true;
    render();
    if (state.live) {
      api.logout().then(() => {
        if (generation === state.transitionId && state.loggingOut) finishLogout();
      }).catch((error) => {
        if (generation !== state.transitionId) return;
        state.loggingOut = false;
        resumeStudentWork();
        toast(error && error.message ? error.message : "未能结束服务端会话，请重试");
        render();
      });
    } else {
      finishLogout();
    }
    return;
  }
  if (action === "open-review") {
    openTeacherReview({ studentId: event.currentTarget.dataset.studentId, goalId: event.currentTarget.dataset.goalId });
    return;
  }
  if (action === "open-feedback") {
    openStudentFeedback({ taskId: event.currentTarget.dataset.taskId, attemptId: event.currentTarget.dataset.attemptId });
    return;
  }
  if (action === "open-drawer") {
    if (state.role !== "teacher" || state.loggingOut || state.transition || state.loading || !leaveTeacherPage(null)) return;
    teacherComposer.open({ studentId: event.currentTarget.dataset.goalStudent || null });
    return;
  }
  if (action === "close-drawer") { teacherComposer.close(); return; }
  if (action === "reload") {
    if (state.role === "student" && !leaveStudentWork()) return;
    loadWorkspace();
    return;
  }
  if (action === "recover-draft") { recoverStudentDraft(); return; }
  if (action === "reload-server-draft") { recoverStudentDraft({ server: true }); return; }
  if (action === "new-conversation") {
    closeStudentChatStream();
    state.chatContext = null;
    state.chatDraft = "";
    state.chatRenameOpen = false;
    state.chatRenameTitle = "";
    state.chatRenameError = "";
    createStudentConversation("学习对话");
    return;
  }
  if (action === "back-to-task") {
    closeStudentChatStream();
    setChatRoute(null);
    const taskId = state.chatContext?.taskId;
    state.studentTab = "tasks";
    if (taskId) { selectLiveTask(taskId); return; }
    state.studentPage = "workspace";
    render();
    return;
  }
  if (action === "retry-context-send") { sendContextChat(); return; }
  if (action === "chat-back") {
    closeStudentChatStream();
    setChatRoute(null);
    state.activeConversationId = null;
    state.activeConversation = null;
    state.chatMessages = [];
    state.chatContext = null;
    state.chatDraft = "";
    state.chatRenameOpen = false;
    state.chatRenameTitle = "";
    state.chatRenameError = "";
    state.studentPage = "home";
    state.studentTab = "companion";
    state.chatPolicyLoading = true;
    loadConversationList();
    refreshCompanionPolicy();
    render();
    return;
  }
  if (action === "archive-conversation") { archiveStudentConversation(); return; }
  if (action === "rename-conversation") {
    const id = state.activeConversationId;
    if (!id || state.chatRenaming) return;
    const current = state.liveConversations.find((item) => item.id === id) || state.activeConversation;
    state.chatRenameOpen = true;
    state.chatRenameTitle = current?.title || "学习对话";
    state.chatRenameError = "";
    renderStudentChatIfActive(chatEpoch);
    view.querySelector("[data-chat-rename-input]")?.focus({ preventScroll: true });
    return;
  }
  if (action === "cancel-rename") {
    if (state.chatRenaming) return;
    state.chatRenameOpen = false;
    state.chatRenameTitle = "";
    state.chatRenameError = "";
    renderStudentChatIfActive(chatEpoch);
    return;
  }

  if (action === "toggle-archived") {
    state.chatArchived = !state.chatArchived;
    loadConversationList();
    return;
  }
  if (action === "retry-chat-send") { sendStudentMessage(); return; }
  if (action === "reload-conversation") { loadConversation(state.activeConversationId); return; }
  if (["refresh-chat-response", "cancel-chat-response", "resume-chat-response"].includes(action)) {
    actOnChatResponse(event.currentTarget.dataset.responseId, action);
    return;
  }
  if (action === "more-conversations") { loadConversationList({ more: true }); return; }
  if (action === "reload-conversations") { loadConversationList(); refreshCompanionPolicy(); return; }
  if (action === "load-older-messages") { loadOlderChatMessages(); return; }
  if (action === "ask-companion") { openChatFromTask(); return; }
  if (action === "request-hint") { requestRestrictedHint(Number(event.currentTarget.dataset.hintLevel)); return; }
  if (action === "retry-hint") { sendContextChat(); return; }
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
      selectLiveTask(event.currentTarget.dataset.taskId || state.liveTask?.id, event.currentTarget.dataset.attemptId);
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
  if (action === "comment") {
    toast("请进入批改与反馈，选定一次提交后发送短评");
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
    if (!state.live) return;
    state.chatDraft = String(data.get("text") || "");
    state.chatDrafts[state.activeConversationId || "__new"] = state.chatDraft;
    sendStudentMessage();
    return;
  }
  if (action === "send-context") {
    if (!state.chatContext || state.chatSending || state.chatCreating) return;
    state.chatDraft = String(data.get("text") || "");
    state.chatContext.prompt = state.chatDraft;
    sendContextChat();
    return;
  }
  if (action === "rename-chat") {
    renameStudentConversation(String(data.get("title") || ""));
    return;
  }
  if (action === "new-chat") {
    const title = String(data.get("title") || "").trim();
    if (!title) return;
    createStudentConversation(title);
  }
}

async function enterWorkspace(role, displayName, { live, message }) {
  const generation = ++state.transitionId;
  resetStudentWrites();
  adminWorkspace.reset();
  teacherManagement.reset();
  resetLearningWorkspaces();
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
  view.querySelector(role === "admin" ? "#admin-main" : role === "teacher" ? "#teacher-main" : "#student-main")?.focus({ preventScroll: true });
  if (live) {
    await loadWorkspace();
    if (generation === state.transitionId && role === "student" && chatRoute()) await loadConversation(chatRoute());
  }
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
  if (!state.live || !state.role || state.loggingOut) return;
  if (state.role === "admin") {
    await adminWorkspace.load();
    return;
  }
  const request = ++workspaceRequest;
  const generation = state.transitionId;
  const role = state.role;
  const actorId = api.state.user?.id;
  const current = () => request === workspaceRequest && generation === state.transitionId
    && role === state.role && actorId === api.state.user?.id;
  state.loading = true;
  state.liveError = "";
  render();
  try {
    if (role === "teacher") await loadTeacher();
    else await loadStudent();
    if (!current() || state.loggingOut) return;
    state.loading = false;
    if (role === "teacher") {
      if (state.teacherView === "competition") await teacherCompetition.load();
      if (state.teacherView === "management") await teacherManagement.load();
      if (state.teacherView === "review") await teacherReview.load();
      if (state.teacherView === "reports") await teacherReportWorkspace.load();
      if (state.teacherView === "insights") await teacherInsightsWorkspace.load();
    } else if (state.studentTab === "feedback") await studentFeedback.load();
    if (current() && !state.loggingOut) await learningNotifications.load();
  } catch (error) {
    if (current() && !state.loggingOut) state.liveError = error?.message || "读取学习记录时出错了";
  } finally {
    if (current()) {
      state.loading = false;
      if (!state.loggingOut) render();
    }
  }
}

async function loadTeacher() {
  const generation = state.transitionId;
  let context = state.teacherContextVersion;
  const actorId = api.state.user?.id;
  const previousClass = state.classId, previousCourse = state.courseId;
  const current = () => generation === state.transitionId && context === state.teacherContextVersion && state.role === "teacher"
    && actorId === api.state.user?.id && !state.loggingOut;
  const clearChangedContext = () => {
    if (previousClass === state.classId && previousCourse === state.courseId) return;
    state.teacherContextVersion += 1; context = state.teacherContextVersion;
    teacherLibrary.reset(); teacherComposer.reset(); teacherManagement.reset();
    resetLearningWorkspaces();
  };
  const courses = await api.teacherClasses();
  if (!current()) return;
  state.teacherCourses = courses;
  const withClasses = courses.find((entry) => entry.course.id === state.courseId)
    || courses.find((entry) => entry.classes.length) || courses[0];
  if (!withClasses) {
    Object.assign(state, { courseId: null, courseName: "", classId: null, className: "", liveStudents: [], liveGoals: [], openStudent: null });
    clearChangedContext();
    return;
  }
  state.courseId = withClasses.course.id;
  state.courseName = withClasses.course.name;
  const classroom = withClasses.classes.find((item) => item.id === state.classId) || withClasses.classes[0];
  if (!classroom) {
    state.classId = null;
    state.className = "";
    state.liveStudents = [];
    state.liveGoals = [];
    state.openStudent = null;
    clearChangedContext();
    return;
  }
  state.classId = classroom.id;
  state.className = classroom.name;
  clearChangedContext();

  const [members, goalList] = await Promise.all([
    api.classMembers(state.classId),
    api.goals(state.classId),
  ]);
  if (!current()) return;
  state.liveGoals = goalList;
  const goal = goalList[0];

  // 任务只读一次；没有分配任务的新成员仍保留在班级列表中。
  const tasks = goal ? await api.myTasks(goal.id).catch(() => []) : [];
  if (!current()) return;
  const students = [];
  for (const member of members) {
    students.push({
      id: member.user_id,
      name: member.display_name || member.username,
      active: member.status === "active",
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
      if (!current()) return;
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
            : entry.mastery >= 80 ? "本轮得分比例较高，可继续练习目标题。"
            : entry.mastery >= 50 ? "本轮仍有失分，建议结合逐题反馈复习。" : "本轮得分比例较低，建议先复习基础材料。";
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
  if (!current()) return;
  state.liveStudents = students;
  state.liveGoals = goalList;
}

async function loadStudent() {
  const generation = state.transitionId, epoch = chatEpoch, context = state.chatContext, actorId = api.state.user?.id;
  const archived = state.chatArchived, sequence = ++chatListSequence;
  const [tasks, goals, courses] = await Promise.all([api.myTasks(), api.goals(), api.courses()]);
  if (generation !== state.transitionId || state.role !== "student" || state.loggingOut || actorId !== api.state.user?.id) return;
  state.liveTasks = tasks;
  state.liveGoals = goals;
  const selected = tasks.find((item) => item.id === state.liveTask?.id)
    || tasks.find((item) => !taskFinished(item)) || tasks[0] || null;
  if (state.liveTask?.id !== selected?.id) {
    state.liveLease = null;
    state.draftDirty = false;
    state.answers = [];
    stopStudentEditing();
  }
  state.liveTask = selected;
  const course = courses.find((item) => item.id === selected?.course_id) || courses[0];
  state.courseName = course?.name || "";
  state.courseId = course?.id || null;
  state.goalLabel = selected ? taskTitle(selected) : "";
  if (epoch !== chatEpoch || sequence !== chatListSequence || archived !== state.chatArchived || context !== state.chatContext) return;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  const current = () => generation === state.transitionId && epoch === chatEpoch && sequence === chatListSequence
    && archived === state.chatArchived && context === state.chatContext && state.role === "student"
    && actorId === api.state.user?.id && !state.loggingOut;
  try {
    const [conversations, policy] = await Promise.all([
      api.conversations({ archived, limit: 100, signal: controller.signal }),
      api.companionAnswerPolicy({ ...context, signal: controller.signal }),
    ]);
    if (!current()) return;
    state.liveConversations = conversations;
    state.chatHasMoreConversations = conversations.length === 100;
    state.chatPolicy = policy;
    state.chatPolicyLoading = false;
    state.conversationError = "";
    state.chatPolicyError = "";
  } catch (error) {
    if (current() && error?.name !== "AbortError") {
      state.conversationError = "对话记录暂时没有读到，可以稍后刷新。";
      state.chatPolicy = null;
      state.chatPolicyLoading = false;
      state.chatPolicyError = "当前学习范围暂时无法核验。";
    }
  } finally { chatReadControllers.delete(controller); }
}

function studentSnapshot(value) {
  return JSON.parse(JSON.stringify(value));
}

function studentWrite(task = state.liveTask) {
  if (!task) return null;
  state.studentWrites ||= {};
  let write = state.studentWrites[task.id];
  if (!write || !studentWriteOwned(write)) {
    write = { taskId: task.id, actorId: api.state.user?.id, generation: state.transitionId,
      epoch: state.studentWriteEpoch || 0, version: task.version, pending: null, submit: null,
      flight: null, timer: null, maxTimer: null, blocked: "", error: "", savedInput: null };
    state.studentWrites[task.id] = write;
  }
  return write;
}

function studentWriteOwned(write) {
  return write && write.actorId === api.state.user?.id && write.generation === state.transitionId
    && write.epoch === (state.studentWriteEpoch || 0) && state.role === "student";
}

function studentWriteCurrent(write) {
  return studentWriteOwned(write) && state.studentWrites?.[write.taskId] === write
    && state.liveTask?.id === write.taskId && !state.loggingOut && !state.studentSuspended;
}

function clearStudentSaveTimers(write) {
  if (!write) return;
  if (write.timer !== null) clearTimeout(write.timer);
  if (write.maxTimer !== null) clearTimeout(write.maxTimer);
  write.timer = null;
  write.maxTimer = null;
}

function stopStudentEditing() {
  clearInterval(leaseTimer);
  leaseTimer = 0;
  for (const write of Object.values(state.studentWrites || {})) clearStudentSaveTimers(write);
}

function resetStudentWrites() {
  stopStudentEditing();
  state.studentWriteEpoch = (state.studentWriteEpoch || 0) + 1;
  state.studentWrites = {};
  state.studentComposing = false;
  state.studentSubmitBusy = false;
  state.hintPreparing = null;
  state.studentSuspended = false;
  state.diagnosticUnknown = [];
  state.diagnosticDrafts = {};
  state.diagnosticCommand = null;
  state.diagnostic = null;
  state.diagnosticAnswers = [];
  state.studentDrafts = {};
  state.answers = [];
  state.draftDirty = false;
  state.savedAt = null;
  state.liveLease = null;
}

function studentAnswerLocked(diagnostic = false) {
  const write = state.studentWrites?.[state.liveTask?.id];
  return state.loading || state.loggingOut || state.studentSuspended || state.studentSubmitBusy || Boolean(state.hintPreparing)
    || (diagnostic ? Boolean(state.diagnosticCommand) : state.banner === "takeover" || Boolean(write?.blocked || write?.submit)
      || Boolean(pendingRestrictedHint()?.hintSent) || state.chatSending && state.chatContext?.intent === "hint"
      || Boolean(state.liveTask?.submission_closed));
}

function studentSaveLabel() {
  const write = state.studentWrites?.[state.liveTask?.id];
  if (write?.submit) return write.flight ? "正在确认交卷结果…" : "交卷结果未确认，请重试原交卷请求；当前不能开始新的保存或交卷。";
  if (write?.blocked) return `${write.error || "版本或编辑权限已变化。"} 本页作答仍保留，请选择恢复方式。`;
  if (write?.flight) return "正在保存到服务器，仍可继续作答…";
  if (write?.pending) return "保存结果未确认，本页输入仍保留；重试会使用同一请求和原作答。";
  if (write?.error) return `${write.error} 本页输入仍保留，请重试保存。`;
  if (state.studentComposing) return "正在输入，完成输入后自动保存。";
  if (state.draftDirty) return "有尚未保存的作答；完整作答会自动保存，未填完的题目仅保留在本页。";
  return state.savedAt ? `服务器已确认保存于 ${new Date(state.savedAt).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" })}`
    : "当前显示服务器草稿；作答后自动保存。";
}

function refreshStudentSaveStatus() {
  const write = state.studentWrites?.[state.liveTask?.id];
  view.querySelectorAll(".student-draft-state").forEach((node) => { node.textContent = studentSaveLabel(); });
  view.querySelectorAll('[data-action="save-draft"]').forEach((node) => {
    node.disabled = Boolean((studentAnswerLocked() && !write?.pending) || write?.flight || state.studentComposing || state.loggingOut || state.studentSubmitBusy);
    node.textContent = write?.pending ? "重试原保存请求" : "保存进度";
  });
  view.querySelectorAll('[data-action="submit"]').forEach((node) => {
    node.disabled = Boolean(state.loading || state.studentSubmitBusy || state.hintPreparing || pendingRestrictedHint() || write?.flight || write?.blocked && !write?.submit || state.loggingOut || state.studentComposing || state.liveTask?.submission_closed);
    node.textContent = write?.submit ? "重试原交卷请求" : state.studentSubmitBusy ? "正在确认保存…" : "确认交卷";
  });
  view.querySelectorAll("[data-draft-recovery]").forEach((node) => { node.hidden = !write?.blocked; });
  view.querySelectorAll("[data-choose]").forEach((node) => { node.disabled = Boolean(studentAnswerLocked()); });
  view.querySelectorAll('[data-answer-input="task"]').forEach((node) => { node.readOnly = Boolean(studentAnswerLocked()); });
  const hint = pendingRestrictedHint();
  view.querySelectorAll('[data-action="request-hint"]').forEach((node) => {
    const level = Number(node.dataset.hintLevel);
    node.disabled = hint ? !restrictedHintScopeCurrent(hint) || hint.pending.hintLevel !== level || state.chatSending || state.chatCreating
      : !restrictedHintAvailable() || state.chatSending || state.chatCreating;
  });
  view.querySelectorAll("[data-hint-status]").forEach((node) => {
    node.textContent = state.hintPreparing ? "正在确认本题作答已保存…" : hint ? "提示请求结果待确认，只能使用原级别与原请求重试。"
      : "提示使用服务器上已保存的本题尝试，结果与交付记录由服务器确认。";
  });
}

function rememberStudentDraft() {
  if (!state.liveTask) return;
  state.draftDirty = true;
  state.studentDrafts[state.liveTask.id] = { answers: studentSnapshot(state.answers), dirty: true };
  studentWrite();
  scheduleStudentSave();
}

function scheduleStudentSave() {
  const write = studentWrite();
  if (!write || !studentWriteCurrent(write) || state.studentComposing || state.studentSubmitBusy
      || state.liveTask.diagnostic_required || state.liveTask.submission_closed || !state.liveLease
      || write.blocked || write.submit || write.flight || !state.draftDirty && !write.pending
      || write.pending?.attempts >= 3 || !write.pending && (write.error || write.savedInput === JSON.stringify(state.answers))) return;
  if (write.timer !== null) clearTimeout(write.timer);
  const run = () => {
    clearStudentSaveTimers(write);
    if (studentWriteCurrent(write)) saveLiveDraft({ automatic: true });
  };
  write.timer = setTimeout(run, write.pending ? 1000 * write.pending.attempts : 700);
  // 连续输入最多等四秒；任何时刻至多一个请求和两个等待计时器。
  if (write.maxTimer === null) write.maxTimer = setTimeout(run, 4000);
}

function diagnosticHasInput() {
  return Boolean(state.diagnostic && ((state.diagnosticUnknown || []).length
    || state.diagnosticAnswers.some((value) => Array.isArray(value) ? value.some((item) => String(item ?? "").length) : value !== "" && value != null)));
}

function rememberDiagnosticDraft() {
  if (!state.diagnostic || !state.liveTask) return;
  state.diagnosticDrafts ||= {};
  const taskId = state.diagnostic.task_id || state.liveTask.id;
  state.diagnosticDrafts[taskId] = { diagnostic: state.diagnostic,
    answers: studentSnapshot(state.diagnosticAnswers), unknown: [...(state.diagnosticUnknown || [])] };
}

function studentWorkPending() {
  return state.studentComposing || chatComposing || state.studentSubmitBusy || state.draftDirty || diagnosticHasInput()
    || Boolean(state.diagnosticCommand) || Object.values(state.studentDrafts || {}).some((draft) => draft.dirty)
    || Object.values(state.diagnosticDrafts || {}).some((draft) => draft.unknown.length || draft.answers.some((value) => value !== "" && value != null))
    || Object.values(state.studentWrites || {}).some((write) => write.flight || write.pending || write.submit)
    || Boolean(state.chatDraft?.trim()) || Object.values(state.chatDrafts || {}).some((draft) => draft?.trim())
    || state.chatSending || state.chatCreating || Boolean(state.pendingChatCreate)
    || Object.values(state.chatPendingSends || {}).some(Boolean) || Boolean(state.chatContext?.pending)
    || Object.values(state.chatContexts || {}).some((context) => context?.pending);
}

function studentConfirm(message) {
  return typeof window.confirm === "function" ? window.confirm(message) : false;
}

function leaveStudentWork({ logout = false, confirmed = false } = {}) {
  if (!state.live || state.role !== "student") return true;
  if (state.studentComposing || chatComposing) { toast("请先完成当前输入，再离开作答页"); return false; }
  if (state.studentSubmitBusy || state.diagnosticCommand || Object.values(state.studentWrites || {}).some((write) => write.flight || write.pending || write.submit)) {
    toast("保存或提交结果尚未确认，请先重试原请求，确认结果后再离开");
    return false;
  }
  const input = view.querySelector("[data-chat-input]");
  if (input && !input.readOnly) {
    state.chatDraft = input.value;
    state.chatDrafts[state.activeConversationId || "__new"] = input.value;
    if (!state.activeConversationId && state.chatContext) state.chatContext.prompt = input.value;
  }
  if (studentWorkPending() && !confirmed) {
    if (logout) {
      state.sheet = "logout";
      render();
      return false;
    }
    if (!studentConfirm("仍有未提交或未保存的作答、未发送消息或未确认的聊天请求，仅保留在本页内存；刷新或关闭页面会丢失。确认暂时离开？")) return false;
  }
  rememberDiagnosticDraft();
  stopStudentEditing();
  return true;
}

function beforeStudentUnload(event) {
  if (!studentWorkPending()) return;
  event.preventDefault();
  event.returnValue = "";
}

function suspendStudentWork() {
  state.studentSuspended = true;
  stopStudentEditing();
}

function resumeStudentWork() {
  state.studentSuspended = false;
  state.studentSubmitBusy = false;
  if (!state.live || state.role !== "student" || state.loggingOut || state.screen !== "app") return;
  if (state.liveLease && !state.liveTask?.diagnostic_required && !state.liveTask?.submission_closed) startLeaseRenewal(state.liveTask.id, state.transitionId);
  scheduleStudentSave();
  refreshStudentSaveStatus();
}

function studentWriteFailed(write, error) {
  write.error = error?.message || "请求结果未确认";
  const status = error?.status;
  if ([401, 403, 404, 409].includes(status)) {
    write.blocked = error.code || "conflict";
    stopStudentEditing();
    state.liveLease = null;
    state.banner = "takeover";
  } else state.banner = "save-fail";
  // 只有明确拒绝的 4xx 才能结束未决命令；网络、5xx 与无效回执均保留原载荷。
  return Number.isInteger(status) && status >= 400 && status < 500 && ![408, 429].includes(status);
}

function studentDraftPayload(task, answers) {
  const payload = {};
  let incomplete = false;
  (task.questions || []).forEach((question, index) => {
    const value = answers[index];
    if (!answerMissing(value, question)) payload[question.id] = value;
    else if (questionBlankCount(question) > 1 && Array.isArray(value) && value.some((item) => !answerMissing(item))) incomplete = true;
  });
  return { answers: studentSnapshot(payload), incomplete };
}

function studentDeviceId() {
  let deviceId = window.__studentDeviceId;
  if (!deviceId) {
    try {
      deviceId = window.sessionStorage.getItem("axis-student-device-id") || `student-${crypto.randomUUID()}`;
      window.sessionStorage.setItem("axis-student-device-id", deviceId);
    } catch { deviceId ||= `student-${crypto.randomUUID()}`; }
    window.__studentDeviceId = deviceId;
  }
  return deviceId;
}

async function recoverStudentDraft({ server = false } = {}) {
  const write = studentWrite(), taskId = state.liveTask?.id;
  if (!write || !write.blocked || write.pending || write.submit || write.flight || state.loading || state.loggingOut) return false;
  if (!studentConfirm(server ? "确认放弃本页未保存的作答，重新读取服务器草稿？"
    : "确认保留本页作答并接管编辑？另一页将失去编辑权，本页作答会覆盖服务器草稿。")) return false;
  const answers = studentSnapshot(state.answers);
  state.loading = true;
  render();
  try {
    const full = await api.task(taskId);
    if (!studentWriteCurrent(write)) return false;
    if (taskFinished(full) || full.diagnostic_required) throw new Error("任务当前不能继续编辑，本页作答仍保留");
    const lease = await api.acquireLease(taskId, studentDeviceId(), { takeover: !server });
    if (!studentWriteCurrent(write)) return false;
    const latest = await api.task(taskId);
    if (!studentWriteCurrent(write)) return false;
    if (taskFinished(latest) || latest.diagnostic_required || lease.task_version !== latest.version) throw new Error("恢复期间任务再次变化，请检查后重试");
    updateStudentTask(latest);
    state.liveLease = lease;
    write.version = latest.version;
    write.blocked = "";
    write.error = "";
    write.savedInput = null;
    state.answers = server ? latest.questions.map((question) => latest.draft_answers?.[question.id] ?? "") : answers;
    state.banner = "";
    state.savedAt = null;
    state.draftDirty = !server;
    if (server) delete state.studentDrafts[taskId];
    else rememberStudentDraft();
    startLeaseRenewal(taskId, state.transitionId);
    return true;
  } catch (error) {
    if (studentWriteCurrent(write)) { write.error = error?.message || "恢复没有完成，本页作答仍保留"; toast(write.error); }
    return false;
  } finally {
    if (studentWriteCurrent(write)) { state.loading = false; render(); }
  }
}

function updateStudentTask(task) {
  state.liveTask = task;
  const index = state.liveTasks.findIndex((item) => item.id === task.id);
  if (index >= 0) state.liveTasks[index] = task;
  else state.liveTasks.push(task);
  state.goalLabel = taskTitle(task);
}

async function selectLiveTask(taskId, attemptId = null) {
  if (state.loading || state.loggingOut || !taskId) return;
  if (!leaveStudentWork()) return;
  if (state.studentTab === "feedback" && !studentFeedback.leave()) return;
  notificationOpenSequence += 1;
  const generation = state.transitionId, actorId = api.state.user?.id;
  const current = () => generation === state.transitionId && actorId === api.state.user?.id && state.role === "student" && !state.loggingOut;
  try {
    state.loading = true;
    render();
    const full = await api.task(taskId);
    if (!current()) return;
    if (attemptId && !(full.attempts || []).some((item) => item.id === attemptId)) {
      toast("这次作答记录暂时不可用，请刷新后重试");
      return;
    }
    const sameTask = state.liveTask?.id === full.id;
    if (!sameTask) {
      stopStudentEditing();
      state.liveLease = null;
      state.draftDirty = false;
      state.savedAt = null;
      state.answers = [];
      state.diagnostic = null;
      state.diagnosticAnswers = [];
      state.diagnosticUnknown = [];
    }
    updateStudentTask(full);
    state.sheet = null;
    state.banner = "";
    state.selectedAttemptId = attemptId;
    state.currentQ = 0;
    state.loading = false;
    if (attemptId || full.submission_closed) state.studentPage = "result";
    else if (full.diagnostic_required) await runDiagnostic();
    else if (taskFinished(full)) state.studentPage = "result";
    else if (full.scheduled_at && full.scheduled_at * 1000 > Date.now()) toast("尚未到任务开放时间");
    else await openLiveTask();
  } catch (error) {
    if (current()) toast(error?.message || "任务读取失败，请重试");
  } finally {
    if (current()) {
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
    if (generation === state.transitionId && state.liveTask?.id === id) {
      updateStudentTask(full);
      state.banner = "";
      if (["submitted", "grading"].includes(full.status)) pollTask(id, generation);
    }
  } catch (error) {
    if (generation === state.transitionId && state.liveTask?.id === id) {
      state.banner = "grading-refresh-failed";
      toast(error?.message || "结果暂时没有读到");
    }
  } finally {
    if (generation === state.transitionId) { state.loading = false; render(); }
  }
}

function chatRoute() {
  const hash = window.location?.hash || "";
  if (!hash.startsWith("#chat=")) return null;
  try { return decodeURIComponent(hash.slice(6)) || null; } catch { return null; }
}

function setChatRoute(id) {
  if (!window.history?.replaceState || !window.location) return;
  window.history.replaceState(null, "", window.location.pathname + window.location.search + (id ? `#chat=${encodeURIComponent(id)}` : ""));
}

function closeChatResponse(responseId) {
  chatStreamClosers.get(responseId)?.();
}

function closeStudentChatStream() {
  chatEpoch += 1;
  chatPostSequence += 1;
  chatListSequence += 1;
  for (const controller of chatReadControllers) controller.abort();
  chatReadControllers.clear();
  for (const close of chatStreamClosers.values()) close();
  chatStreamClosers.clear();
  chatComposing = false;
  chatRenderPending = false;
  state.chatSending = false;
  state.chatCreating = false;
  state.chatLoadingOlder = false;
  state.chatPolicyLoading = false;
}

function resumeStudentChat() {
  if (!state.live || state.screen !== "app" || state.role !== "student" || state.studentTab !== "companion") return;
  const id = chatRoute() || state.activeConversationId;
  if (id) loadConversation(id);
  else { loadConversationList(); refreshCompanionPolicy(); }
}

function chatResponseById(responseId) {
  return state.chatMessages.find((message) => message.response?.response_id === responseId)?.response || null;
}

function hintDeliveryPending(response) {
  return response?.intent === "hint" && response.status === "completed"
    && ["queued", "leased", "running", "retry_wait"].includes(response.job?.status);
}

function chatResponseActive(response) {
  return ["pending", "streaming"].includes(response?.status) || hintDeliveryPending(response);
}

function chatResponsePaused(response) {
  return (chatResponseActive(response) || response.intent === "hint" && response.status === "completed")
    && (["reconciling", "awaiting_confirmation"].includes(response.job?.status)
      || response.intent === "hint" && response.job?.status === "failed");
}

function chatResponseHidden(response) {
  return response.redacted || ["failed", "cancelled", "revoked"].includes(response.status)
    || response.intent === "hint" && (response.status !== "completed" || hintDeliveryPending(response)
      || response.job?.cancel_requested || response.job?.cost_pending || response.job?.reconciliation_required
      || ["reconciling", "awaiting_confirmation", "failed", "cancelled"].includes(response.job?.status));
}

function projectChatResponse(response) {
  const projected = { ...response };
  if (chatResponseHidden(projected)) {
    projected.text = "";
    if (Object.prototype.hasOwnProperty.call(projected, "content")) projected.content = "";
    if (projected.intent === "hint") projected.hint_metadata = null;
  }
  return projected;
}

function chatResponseTitle(response) {
  return response.intent === "hint" ? hintLevelLabel(response.hint_level) : "学伴";
}

function chatResponseStatus(response) {
  if (response.status === "revoked") return "访问权限已变化";
  if (response.status === "failed" || response.intent === "hint" && response.job?.status === "failed") return "本次回答未完成";
  if (response.status === "cancelled" || response.intent === "hint" && response.job?.status === "cancelled") return "本次回答已取消";
  if (response.job?.cancel_requested) return "正在取消本次回答…";
  if (response.job?.status === "reconciling" || response.job?.reconciliation_required) return "回答已暂停，正在核对调用结果；不会自动重复发送。";
  if (response.job?.cost_pending) return "回答已暂停，等待费用核对后可确认恢复。";
  if (response.job?.status === "awaiting_confirmation") return "回答已暂停，请确认恢复或取消。";
  if (hintDeliveryPending(response)) return "提示已生成，正在确认交付记录…";
  if (response.status === "completed") {
    if (response.intent === "hint") {
      if (response.redacted) return "提示当前不可查看，请返回作答或刷新状态。";
      return response.content_type === "safe_guidance" || response.hint_metadata?.status === "fallback"
        ? "安全引导 · 本次未提供题目提示" : "固定模板提示 · 已确认交付";
    }
    return response.content_type === "safe_guidance" ? "学习范围引导" : "";
  }
  if (response.job?.status === "retry_wait") return "正在等待重试原作业…";
  return response.status === "streaming" || response.job?.status === "running" ? "正在回答…" : "正在排队…";
}

function chatResponseControls(response) {
  const hintActive = response.intent === "hint" && chatResponseActive(response);
  if ((!chatResponsePaused(response) && !hintActive) || !response.job_id) return "";
  const id = escapeHtml(response.response_id), disabled = response.acting ? "disabled" : "";
  const canResume = (response.job?.status === "awaiting_confirmation"
    || response.intent === "hint" && response.job?.status === "failed" && chatResponsePaused(response))
    && !response.job.cost_pending && !response.job.reconciliation_required && !response.job.cancel_requested;
  return `<div class="student-chat-toolbar-actions">${response.acting ? '<span role="status">正在处理…</span>' : ""}<button type="button" class="btn" data-action="refresh-chat-response" data-response-id="${id}" ${disabled}>刷新状态</button>${!response.job?.cancel_requested ? `<button type="button" class="btn" data-action="cancel-chat-response" data-response-id="${id}" ${disabled}>取消本次回答</button>` : ""}${canResume ? `<button type="button" class="btn" data-action="resume-chat-response" data-response-id="${id}" ${disabled}>确认恢复（可能产生费用）</button>` : ""}</div>`;
}

async function actOnChatResponse(responseId, action) {
  const response = chatResponseById(responseId);
  if (!response || response.acting || !response.job_id
      || !chatResponsePaused(response) && !(response.intent === "hint" && chatResponseActive(response))) return;
  if (action === "resume-chat-response" && (!(response.job.status === "awaiting_confirmation"
      || response.intent === "hint" && response.job.status === "failed" && chatResponsePaused(response))
      || response.job.cost_pending || response.job.reconciliation_required || response.job.cancel_requested)) return;
  if (action === "resume-chat-response" && !studentConfirm("确认恢复本次回答？恢复可能产生费用，将沿用原作业。")) return;
  if (response.intent === "hint" && action === "cancel-chat-response" && !studentConfirm("确认取消本次提示？已发生的调用仍需完成费用核对。")) return;
  const epoch = chatEpoch, conversationId = state.activeConversationId, actorId = api.state.user?.id, generation = state.transitionId;
  const current = () => epoch === chatEpoch && conversationId === state.activeConversationId && state.role === "student"
    && actorId === api.state.user?.id && generation === state.transitionId && !state.loggingOut;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  updateChatResponse(responseId, { acting: true });
  state.chatResponseError = "";
  renderStudentChatIfActive(epoch);
  try {
    if (action === "cancel-chat-response") await api.cancelCompanionJob(response.job_id);
    if (action === "resume-chat-response") await api.resumeCompanionJob(response.job_id);
    if (!current()) return;
    const snapshot = await api.getResponse(responseId, { signal: controller.signal });
    if (!current()) return;
    updateChatResponse(responseId, { ...snapshot, acting: false });
    if (!chatResponsePaused(snapshot) && chatResponseActive(snapshot)) {
      connectChatResponse(responseId, snapshot.last_event_id || 0);
    }
  } catch (error) {
    if (current() && error?.name !== "AbortError") {
      if ([401, 403, 404].includes(error?.status)) updateChatResponse(responseId, { text: "", redacted: true, status: "revoked" });
      state.chatResponseError = error?.message || "状态暂时未能更新，请刷新确认。";
    }
  } finally {
    chatReadControllers.delete(controller);
    if (current()) {
      updateChatResponse(responseId, { acting: false });
      renderStudentChatIfActive(epoch);
    }
  }
}

function updateChatResponse(responseId, patch) {
  const message = state.chatMessages.find((item) => item.response?.response_id === responseId);
  if (!message) return;
  message.response = projectChatResponse({ ...message.response, ...patch });
  const root = Array.from(view.querySelectorAll("[data-response-id]")).find((node) => node.dataset.responseId === responseId);
  if (!root) return;
  const thread = view.querySelector("[data-chat-thread]");
  const nearBottom = thread && thread.scrollHeight - thread.scrollTop - thread.clientHeight < 64;
  const body = root.querySelector("[data-response-text]");
  const status = root.querySelector("[data-response-status]");
  const title = root.querySelector("[data-response-title]");
  if (title) title.textContent = chatResponseTitle(message.response);
  if (body) body.textContent = message.response.redacted ? message.response.notice || "内容暂未开放。" : message.response.text || "";
  if (status) status.textContent = chatResponseStatus(message.response);
  root.dataset.responseStatus = message.response.status;
  root.classList.toggle("failed", ["failed", "cancelled", "revoked"].includes(message.response.status));
  if (nearBottom) thread.scrollTop = thread.scrollHeight;
}

function connectChatResponse(responseId, after = 0) {
  if (!responseId || !state.activeConversationId || !state.live || typeof api.openResponseStream !== "function"
      || chatStreamClosers.has(responseId)) return;
  const epoch = chatEpoch, conversationId = state.activeConversationId, actorId = api.state.user?.id, generation = state.transitionId;
  let transportClose = null, closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    if (chatStreamClosers.get(responseId) === close) chatStreamClosers.delete(responseId);
    transportClose?.();
  };
  const current = () => !closed && epoch === chatEpoch && conversationId === state.activeConversationId
    && state.studentTab === "companion" && state.role === "student" && actorId === api.state.user?.id
    && generation === state.transitionId && !state.loggingOut;
  chatStreamClosers.set(responseId, close);
  try {
    transportClose = api.openResponseStream(responseId, {
      start(data, meta) {
        if (current()) updateChatResponse(responseId, { ...data, status: "streaming", last_event_id: meta.id });
      },
      reset(data, meta) {
        if (current()) updateChatResponse(responseId, { ...data, text: "", redacted: false, notice: null, status: "streaming", last_event_id: meta.id });
      },
      delta(data, meta) {
        if (!current()) return;
        const response = chatResponseById(responseId);
        if (!response) return;
        updateChatResponse(responseId, { ...data, text: data.redacted ? "" : (response.text || "") + (data.text || ""), status: "streaming", last_event_id: meta.id });
      },
      snapshot(data, meta) {
        if (!current()) return;
        updateChatResponse(responseId, { ...data, text: data.redacted ? "" : data.text || "", last_event_id: data.last_event_id ?? meta.id });
        if (chatResponsePaused(data)) close();
        renderStudentChatIfActive(epoch);
      },
      end(data, meta) {
        if (!current()) return;
        updateChatResponse(responseId, { ...data, last_event_id: data.last_event_id ?? meta.id });
        if (hintDeliveryPending(chatResponseById(responseId))) return;
        close();
        renderStudentChatIfActive(epoch);
        syncConversationHistory(conversationId, epoch);
      },
      revoked() {
        if (!current()) return;
        state.chatResponseError = "会话或任务访问权限已变化，已停止接收回答。";
        updateChatResponse(responseId, { text: "", redacted: true, status: "revoked", notice: state.chatResponseError });
        close();
        renderStudentChatIfActive(epoch);
      },
      error(error) {
        if (!current()) return;
        state.chatResponseError = error?.message || "正在恢复回答连接…";
        renderStudentChatIfActive(epoch);
      },
      onClosed() { close(); },
    }, { after, generation: chatResponseById(responseId)?.generation, text: chatResponseById(responseId)?.text || "" });
    if (closed) transportClose?.();
  } catch (error) {
    close();
    if (epoch === chatEpoch) {
      state.chatResponseError = error?.message || "暂时无法连接回答。";
      renderStudentChatIfActive(epoch);
    }
  }
}

function renderStudentChatIfActive(epoch) {
  if (epoch !== chatEpoch || state.role !== "student" || state.studentTab !== "companion"
      || !["chat", "home"].includes(state.studentPage) || !view.isConnected) return;
  if (chatComposing) { chatRenderPending = true; return; }
  chatRenderPending = false;
  const previousInput = view.querySelector("[data-chat-input]");
  const focused = previousInput === document.activeElement;
  const start = focused ? previousInput.selectionStart : null;
  const end = focused ? previousInput.selectionEnd : null;
  const inputScroll = previousInput?.scrollTop || 0;
  const previousRenameInput = view.querySelector("[data-chat-rename-input]");
  const renameFocused = previousRenameInput === document.activeElement;
  const renameStart = renameFocused ? previousRenameInput.selectionStart : null;
  const renameEnd = renameFocused ? previousRenameInput.selectionEnd : null;
  const thread = view.querySelector("[data-chat-thread]");
  const scroll = thread?.scrollTop || 0;
  const nearBottom = thread && thread.scrollHeight - scroll - thread.clientHeight < 64;
  render();
  const input = view.querySelector("[data-chat-input]");
  if (focused && input) { input.focus({ preventScroll: true }); if (start != null) input.setSelectionRange(start, end); input.scrollTop = inputScroll; }
  if (renameFocused) {
    const renameInput = view.querySelector("[data-chat-rename-input]");
    if (renameInput) {
      renameInput.focus({ preventScroll: true });
      if (renameStart != null) renameInput.setSelectionRange(renameStart, renameEnd);
    }
  }
  const nextThread = view.querySelector("[data-chat-thread]");
  if (nextThread) nextThread.scrollTop = nearBottom ? nextThread.scrollHeight : scroll;
}

function mergeChatMessages(messages) {
  const rows = [...state.chatMessages];
  for (const message of messages) {
    const index = rows.findIndex((item) => item.id === message.id
      || message.client_request_id && (item.localId || item.client_request_id) === message.client_request_id);
    const previous = index >= 0 ? rows[index] : null;
    const incoming = { ...previous, ...message };
    if (previous?.response && message.response && !chatResponseHidden(message.response)) {
      const old = previous.response, next = message.response;
      if ((old.generation || 0) > (next.generation || 0)
          || old.generation === next.generation && (old.last_event_id || 0) > (next.last_event_id || 0)) incoming.response = old;
    }
    if (incoming.response) incoming.response = projectChatResponse(incoming.response);
    delete incoming.localId;
    if (index >= 0) rows[index] = incoming;
    else rows.push(incoming);
    const pending = state.chatPendingSends?.[state.activeConversationId];
    if (pending && message.client_request_id === pending.requestId) {
      delete state.chatPendingSends[state.activeConversationId];
      if (state.chatDraft.trim() === pending.text) state.chatDraft = "";
      if (state.chatDrafts[state.activeConversationId]?.trim() === pending.text) state.chatDrafts[state.activeConversationId] = "";
    }
  }
  const pending = state.chatPendingSends?.[state.activeConversationId];
  if (pending && !rows.some((item) => item.client_request_id === pending.requestId || item.localId === pending.requestId)) {
    rows.push({ localId: pending.requestId, role: "user", text: pending.text, created_at: pending.createdAt,
      response: { status: "pending", text: "", response_id: "" } });
  }
  state.chatMessages = rows.sort((a, b) => (a.created_at || 0) - (b.created_at || 0));
}

async function syncConversationHistory(id, epoch = chatEpoch) {
  const actorId = api.state.user?.id, generation = state.transitionId;
  const current = () => epoch === chatEpoch && id === state.activeConversationId && actorId === api.state.user?.id
    && generation === state.transitionId && state.role === "student" && !state.loggingOut;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  try {
    const messages = await api.conversationMessages(id, { latest: true, limit: 100, signal: controller.signal });
    if (!current()) return;
    mergeChatMessages(messages);
    for (const message of state.chatMessages) {
      const response = message.response;
      if (response && chatResponseActive(response)) connectChatResponse(response.response_id, response.last_event_id || 0);
    }
    renderStudentChatIfActive(epoch);
  } catch (error) {
    if (current() && error?.name !== "AbortError") {
      state.chatResponseError = error?.message || "对话记录暂时没有读到。";
      renderStudentChatIfActive(epoch);
    }
  } finally { chatReadControllers.delete(controller); }
}

async function loadConversation(id, { archived = false } = {}) {
  if (!state.live || !id) return;
  closeStudentChatStream();
  const epoch = chatEpoch, transition = state.transitionId, actorId = api.state.user?.id;
  const current = () => epoch === chatEpoch && transition === state.transitionId && id === state.activeConversationId
    && actorId === api.state.user?.id && state.role === "student" && !state.loggingOut;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  state.activeConversationId = id;
  setChatRoute(id);
  state.chatRenameOpen = false;
  state.chatRenameTitle = "";
  state.chatRenameError = "";
  state.chatRenaming = false;
  state.activeConversation = state.liveConversations.find((item) => item.id === id) || { id, status: archived ? "archived" : "active" };
  state.chatMessages = [];
  state.chatContexts ||= {};
  state.chatContext = state.chatContexts[id] || null;
  state.studentPage = "chat";
  state.studentTab = "companion";
  state.chatDraft = state.chatDrafts[id] || "";
  state.chatOlderCursor = null;
  state.chatHasOlder = false;
  state.chatResponseError = "";
  state.chatPolicy = null;
  state.chatPolicyError = "";
  state.chatPolicyLoading = true;
  mergeChatMessages([]);
  render();
  try {
    if (archived) {
      const restored = await api.restoreConversation(id);
      if (!current()) return;
      state.activeConversation = restored;
    }
    const [messages, conversation] = await Promise.all([
      api.conversationMessages(id, { latest: true, limit: 100, signal: controller.signal }),
      api.getConversation(id, { signal: controller.signal }),
    ]);
    if (!current()) return;
    mergeChatMessages(messages);
    state.liveMessages = messages;
    state.activeConversation = conversation;
    state.chatHasOlder = messages.length >= 100;
    state.chatOlderCursor = messages[0]?.id || null;
    if (!state.chatContext) {
      const latest = [...messages].reverse().find((item) => item.role === "user" && item.response?.task_id);
      if (latest) {
        state.chatContext = { taskId: latest.response.task_id, questionVersionId: latest.response.question_version_id, courseId: conversation.course_id,
          ...(latest.response.intent === "hint" ? { intent: "hint", hintLevel: latest.response.hint_level } : {}) };
        state.chatContexts[id] = state.chatContext;
      }
    }
    renderStudentChatIfActive(epoch);
    for (const message of state.chatMessages) {
      const response = message.response;
      if (response && chatResponseActive(response)) connectChatResponse(response.response_id, response.last_event_id || 0);
    }
    const thread = view.querySelector("[data-chat-thread]");
    if (thread) thread.scrollTop = thread.scrollHeight;
    await refreshCompanionPolicy();
  } catch (error) {
    if (current() && error?.name !== "AbortError") {
      state.chatPolicyLoading = false;
      state.chatResponseError = error?.message || "对话暂时没有读到。";
      renderStudentChatIfActive(epoch);
    }
  } finally { chatReadControllers.delete(controller); }
}

async function refreshCompanionPolicy() {
  if (!state.live) return;
  const epoch = chatEpoch, context = state.chatContext;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  state.chatPolicyLoading = true;
  try {
    const policy = await api.companionAnswerPolicy({ ...context, signal: controller.signal });
    if (epoch !== chatEpoch || context !== state.chatContext || state.role !== "student") return;
    state.chatPolicy = policy;
    state.chatPolicyError = "";
  } catch (error) {
    if (epoch !== chatEpoch || context !== state.chatContext || error?.name === "AbortError") return;
    state.chatPolicy = null;
    state.chatPolicyError = error?.message || "当前学习范围暂时无法核验。";
  } finally {
    chatReadControllers.delete(controller);
    if (epoch === chatEpoch && context === state.chatContext) {
      state.chatPolicyLoading = false;
      if (state.studentPage === "chat") renderStudentChatIfActive(epoch);
      else if (state.studentTab === "companion") render();
    }
  }
}

async function createStudentConversation(title = "学习对话") {
  if (!state.live || state.chatCreating) return;
  const pending = state.pendingChatCreate || { title, requestId: `chat-${crypto.randomUUID()}`, courseId: null };
  state.pendingChatCreate = pending;
  state.chatCreating = true;
  const epoch = chatEpoch;
  render();
  try {
    const conversation = await api.createConversation(pending.title, pending.requestId, pending.courseId);
    if (epoch !== chatEpoch || state.role !== "student") return;
    state.pendingChatCreate = null;
    state.chatCreating = false;
    state.liveConversations = [conversation, ...state.liveConversations.filter((item) => item.id !== conversation.id)];
    state.chatContext = null;
    await loadConversation(conversation.id);
  } catch (error) {
    if (epoch !== chatEpoch) return;
    state.chatCreating = false;
    state.conversationError = error?.message || "新建对话结果未确认，重试会保留原名称与请求。";
    render();
  }
}

async function archiveStudentConversation() {
  const id = state.activeConversationId;
  if (!id || !state.live) return;
  closeStudentChatStream();
  const epoch = chatEpoch;
  try {
    await api.archiveConversation(id);
    if (epoch !== chatEpoch || id !== state.activeConversationId) return;
    setChatRoute(null);
    state.activeConversationId = null;
    state.activeConversation = null;
    state.chatMessages = [];
    state.chatContext = null;
    state.chatRenameOpen = false;
    state.chatRenameTitle = "";
    state.chatRenameError = "";
    state.studentPage = "home";
    state.studentTab = "companion";
    await loadConversationList();
  } catch (error) {
    if (epoch !== chatEpoch) return;
    toast(error?.message || "归档失败，对话仍保留。");
    await loadConversation(id);
  }
}

async function renameStudentConversation(title) {
  const id = state.activeConversationId;
  const value = String(title || "").trim();
  if (!id || !state.live || state.chatRenaming) return;
  if (!value) {
    state.chatRenameError = "请输入会话名称。";
    renderStudentChatIfActive(chatEpoch);
    return;
  }
  const epoch = chatEpoch;
  const current = () => epoch === chatEpoch && id === state.activeConversationId && state.role === "student";
  state.chatRenameTitle = value;
  state.chatRenameError = "";
  state.chatRenaming = true;
  renderStudentChatIfActive(epoch);
  try {
    const updated = await api.renameConversation(id, value);
    if (!current()) return;
    state.liveConversations = state.liveConversations.map((item) => item.id === updated.id ? updated : item);
    state.activeConversation = updated;
    state.chatRenameOpen = false;
    state.chatRenameTitle = "";
  } catch (error) {
    if (!current()) return;
    state.chatRenameError = error?.message || "重命名失败，请重试。";
  } finally {
    if (current()) {
      state.chatRenaming = false;
      renderStudentChatIfActive(epoch);
    }
  }
}

async function loadConversationList({ more = false } = {}) {
  if (!state.live) return;
  const epoch = chatEpoch, sequence = ++chatListSequence, archived = state.chatArchived;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  try {
    const conversations = await api.conversations({ archived, limit: 100, offset: more ? state.liveConversations.length : 0, signal: controller.signal });
    if (epoch !== chatEpoch || sequence !== chatListSequence || state.role !== "student" || archived !== state.chatArchived) return;
    state.liveConversations = more ? [...state.liveConversations, ...conversations.filter((item) => !state.liveConversations.some((row) => row.id === item.id))] : conversations;
    state.chatHasMoreConversations = conversations.length === 100;
    state.conversationError = "";
  } catch (error) {
    if (epoch !== chatEpoch || sequence !== chatListSequence || error?.name === "AbortError") return;
    state.conversationError = error?.message || "对话列表暂时没有读到。";
  } finally {
    chatReadControllers.delete(controller);
    if (epoch === chatEpoch && sequence === chatListSequence) render();
  }
}

async function loadOlderChatMessages() {
  if (state.chatLoadingOlder || !state.chatOlderCursor || !state.activeConversationId) return;
  const id = state.activeConversationId, epoch = chatEpoch, cursor = state.chatOlderCursor;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  const thread = view.querySelector("[data-chat-thread]");
  const oldHeight = thread?.scrollHeight || 0, oldScroll = thread?.scrollTop || 0;
  state.chatLoadingOlder = true;
  renderStudentChatIfActive(epoch);
  try {
    const messages = await api.conversationMessages(id, { before: cursor, limit: 100, signal: controller.signal });
    if (epoch !== chatEpoch || id !== state.activeConversationId) return;
    mergeChatMessages(messages);
    state.chatOlderCursor = messages[0]?.id || null;
    state.chatHasOlder = messages.length >= 100;
  } catch (error) {
    if (epoch === chatEpoch && error?.name !== "AbortError") state.chatResponseError = error?.message || "更早的消息暂时没有读到。";
  } finally {
    chatReadControllers.delete(controller);
    if (epoch === chatEpoch) {
      state.chatLoadingOlder = false;
      renderStudentChatIfActive(epoch);
      const nextThread = view.querySelector("[data-chat-thread]");
      if (nextThread) nextThread.scrollTop = oldScroll + nextThread.scrollHeight - oldHeight;
    }
  }
}

function hintLevelLabel(level) {
  return level === 1 ? "一级 · 知识提示" : level === 2 ? "二级 · 下一步方向" : "受限学习提示";
}

function pendingRestrictedHint(taskId = state.liveTask?.id) {
  return Object.values(state.chatContexts || {}).find((context) => context.intent === "hint" && context.taskId === taskId
    && context.pending && context.actorId === api.state.user?.id && context.transitionId === state.transitionId) || null;
}

function restrictedHintAvailable() {
  const task = state.liveTask, question = task?.questions?.[state.currentQ];
  return Boolean(state.live && state.role === "student" && state.screen === "app" && !state.transition
    && task && question && !task.diagnostic_required && state.studentPage !== "diagnostic" && !taskFinished(task)
    && !studentAnswerLocked() && !state.studentComposing && !chatComposing && !state.hintPreparing
    && questionQuestionVersion(question) && !answerMissing(state.answers[state.currentQ], question));
}

function restrictedHintControls() {
  const task = state.liveTask;
  if (!task || task.diagnostic_required || taskFinished(task)) return "";
  const pending = pendingRestrictedHint(task.id);
  const disabled = (pending ? !restrictedHintScopeCurrent(pending) : !restrictedHintAvailable())
    || state.chatSending || state.chatCreating || state.hintPreparing;
  return `<section class="student-context-block" aria-label="受限学习提示"><h2>先尝试，再看一步提示</h2><p class="student-compose-note">先独立作答。请求前会确认保存，提示只提供知识回忆或下一步方向，不给出答案；首诊不提供提示。</p><div class="student-focus-actions">${[1, 2].map((level) => `<button type="button" class="btn" data-action="request-hint" data-hint-level="${level}" ${disabled || pending && pending.pending.hintLevel !== level ? "disabled" : ""}>${pending?.pending.hintLevel === level ? "重试原提示 · " : ""}${hintLevelLabel(level)}</button>`).join("")}</div><p class="student-compose-note" role="status" data-hint-status>${state.hintPreparing ? "正在确认本题作答已保存…" : pending ? "提示请求结果待确认，只能使用原级别与原请求重试。" : "提示使用服务器上已保存的本题尝试，结果与交付记录由服务器确认。"}</p></section>`;
}

function restrictedHintRequestView(context) {
  const pending = context.pending;
  return `${studentHeading("受限学习提示", hintLevelLabel(pending?.hintLevel || context.hintLevel))}
    <section class="student-chat-context-form"><p class="student-policy-banner guided">仅请求已保存当前题的提示，不开放任意解答。提示不会替你判断答案。</p>
      <p>${escapeHtml(pending?.text || "当前没有待重试的提示请求，请返回作答后重新选择提示级别。")}</p>
      ${state.chatResponseError ? `<div class="state-banner warn" role="alert">${escapeHtml(state.chatResponseError)}</div>` : ""}
      <p class="student-compose-note">${context.hintSent ? "发送结果待确认。重试使用相同任务、题目、级别和请求；当前不能修改本题尝试。" : "发送前需要确认本题尝试已保存。"}</p>
      <div class="student-focus-actions"><button type="button" class="btn" data-action="back-to-task">返回作答</button><button type="button" class="btn primary" data-action="retry-hint" ${state.chatSending || state.chatCreating || !pending ? "disabled" : ""}>${state.chatSending || state.chatCreating ? "正在确认请求…" : "重试原提示请求"}</button></div></section>`;
}

async function requestRestrictedHint(level) {
  if (![1, 2].includes(level) || state.chatSending || state.chatCreating || state.hintPreparing) return false;
  const task = state.liveTask, question = task?.questions?.[state.currentQ];
  const previous = pendingRestrictedHint(task?.id);
  if (previous) {
    if (previous.pending.hintLevel !== level || previous.questionVersionId !== questionQuestionVersion(question)) {
      toast("请返回原题并重试原级别的提示请求"); return false;
    }
    if (!restrictedHintScopeCurrent(previous)) return false;
    closeStudentChatStream();
    state.chatContext = previous;
    state.studentTab = "companion";
    state.studentPage = "home";
    state.activeConversationId = null;
    return sendContextChat();
  }
  if (!restrictedHintAvailable()) { toast("请先完成并保存本题尝试，首诊或提交结果待确认时不能请求提示"); return false; }
  const guard = { actorId: api.state.user?.id, transitionId: state.transitionId, taskId: task.id,
    questionVersionId: questionQuestionVersion(question), questionId: question.id, questionIndex: state.currentQ };
  const page = state.studentPage, tab = state.studentTab, epoch = chatEpoch;
  state.hintPreparing = guard;
  refreshStudentSaveStatus();
  try {
    const saved = await saveLiveDraft();
    if (!restrictedHintScopeCurrent(guard) || page !== state.studentPage || tab !== state.studentTab || epoch !== chatEpoch) return false;
    if (saved !== true) { toast("作答尚未确认保存，提示未发送"); return false; }
    if (state.chatContexts.__new?.pending) { toast("请先确认上一条消息的发送结果"); return false; }
    const context = { ...guard, intent: "hint", hintLevel: level, courseId: task.course_id,
      savedAttempt: JSON.stringify(state.answers[guard.questionIndex]), hintSent: false };
    context.pending = Object.freeze({ requestId: `hint-${crypto.randomUUID()}`, conversationId: null,
      text: level === 1 ? "请提供本题的一级知识提示。" : "请提供本题的二级下一步方向。",
      intent: "hint", hintLevel: level, taskId: task.id, questionVersionId: guard.questionVersionId,
      questionId: question.id, createdAt: Date.now() / 1000 });
    closeStudentChatStream();
    setChatRoute(null);
    state.chatContext = context;
    state.chatContexts.__new = context;
    state.activeConversationId = null;
    state.chatMessages = [];
    state.studentPage = "home";
    state.studentTab = "companion";
    state.chatResponseError = "";
    return await sendContextChat({ savedHint: context.pending });
  } finally {
    if (state.hintPreparing === guard) { state.hintPreparing = null; refreshStudentSaveStatus(); }
  }
}

function restrictedHintScopeCurrent(context) {
  const task = state.liveTask, question = task?.questions?.[state.currentQ], write = state.studentWrites?.[task?.id];
  return context.actorId === api.state.user?.id && context.transitionId === state.transitionId && state.role === "student"
    && state.screen === "app" && !state.loggingOut && !state.transition && !state.loading && !state.studentSuspended
    && task?.id === context.taskId && questionQuestionVersion(question) === context.questionVersionId
    && !task.diagnostic_required && state.studentPage !== "diagnostic" && !taskFinished(task)
    && !state.studentComposing && !chatComposing && !state.studentSubmitBusy && !write?.submit && !write?.blocked
    && state.banner !== "takeover" && !answerMissing(state.answers[state.currentQ], question);
}

async function prepareRestrictedHint(context, current, alreadySaved = false) {
  if (!current() || !restrictedHintScopeCurrent(context)) return false;
  if (context.hintSent && JSON.stringify(state.answers[state.currentQ]) !== context.savedAttempt) {
    throw new Error("本题尝试已变化，请先恢复原提示请求的对话记录，不能替换请求内容。");
  }
  if (!alreadySaved && await saveLiveDraft() !== true) throw new Error("作答尚未确认保存，提示未发送。请返回作答后重试。");
  if (!current() || !restrictedHintScopeCurrent(context) || state.draftDirty) return false;
  const controller = new AbortController();
  chatReadControllers.add(controller);
  try {
    const policy = await api.companionAnswerPolicy({ taskId: context.taskId, questionVersionId: context.questionVersionId, signal: controller.signal });
    if (!current() || !restrictedHintScopeCurrent(context) || state.draftDirty) return false;
    state.chatPolicy = policy;
    state.chatPolicyLoading = false;
    if (!policy?.allow_hint_content || !policy.allowed_hint_levels?.includes(context.pending.hintLevel)) {
      throw new Error("当前题目的受限提示暂未开放，请返回独立作答。");
    }
    return true;
  } finally { chatReadControllers.delete(controller); }
}

function questionPromptContext(task, question, answer) {
  const options = (question.options || []).map((option, index) => `${String.fromCharCode(65 + index)}. ${option}`).join("\n");
  const choice = (index) => Number.isInteger(index) && question.options?.[index] !== undefined
    ? `${String.fromCharCode(65 + index)}. ${question.options[index]}` : String(index);
  const value = Array.isArray(answer) ? answer.map(choice).join("；") : typeof answer === "boolean" ? answer ? "正确" : "错误"
    : typeof answer === "number" ? choice(answer) : answer == null ? "" : String(answer);
  return { taskId: task.id, questionVersionId: questionQuestionVersion(question), questionId: question.id || question.question_id,
    courseId: task.course_id, stem: question.stem || "", options, attempt: value };
}

async function openChatFromTask() {
  const task = state.liveTask, question = task?.questions?.[state.currentQ];
  if (!task || !question || !leaveStudentWork()) return;
  if (pendingRestrictedHint(task.id)) { toast("提示请求结果尚未确认，请先重试原提示请求"); return; }
  closeStudentChatStream();
  setChatRoute(null);
  const attempt = questionPromptContext(task, question, state.answers[state.currentQ]);
  const saved = state.chatContexts.__new;
  state.chatContext = saved?.taskId === attempt.taskId && saved.questionVersionId === attempt.questionVersionId && saved.pending ? saved : attempt;
  const context = state.chatContext;
  context.prompt ||= `我在做这道题：\n${attempt.stem}${attempt.options ? `\n${attempt.options}` : ""}\n\n我的尝试：${attempt.attempt || "（我还没有写下尝试）"}\n\n我想请你帮我检查思路，不要直接告诉我答案。`;
  state.chatContexts.__new = context;
  state.chatDraft = context.pending?.text || context.prompt;
  state.chatDrafts.__new = state.chatDraft;
  state.studentTab = "companion";
  state.studentPage = "home";
  state.activeConversationId = null;
  state.chatMessages = [];
  state.chatResponseError = "";
  state.chatPolicy = null;
  state.chatPolicyLoading = true;
  render();
  refreshCompanionPolicy();
}

function newChatRequest(text, conversationId = null) {
  return Object.freeze({
    requestId: `message-${crypto.randomUUID()}`, conversationId, text,
    taskId: state.chatContext?.taskId || null, questionVersionId: state.chatContext?.questionVersionId || null,
    questionId: state.chatContext?.questionId || null, createdAt: Date.now() / 1000,
    help: { requestId: `help-${crypto.randomUUID()}`, recorded: false },
  });
}

async function prepareChatMessage(pending, current) {
  if (pending.taskId && state.liveTask?.id === pending.taskId && state.draftDirty) {
    const saved = await saveLiveDraft();
    if (!current()) return false;
    if (!saved) throw new Error("作答尚未保存，消息未发送。请返回作答页保存进度后重试。");
  }
  const controller = new AbortController();
  chatReadControllers.add(controller);
  try {
    const policy = await api.companionAnswerPolicy({ taskId: pending.taskId, questionVersionId: pending.questionVersionId, signal: controller.signal });
    if (!current()) return false;
    state.chatPolicy = policy;
    state.chatPolicyLoading = false;
    state.chatPolicyError = "";
    if (policy.help_recordable && !pending.help.recorded) await recordChatHelp(pending);
    return current();
  } finally { chatReadControllers.delete(controller); }
}

async function sendContextChat({ savedHint = null } = {}) {
  if (!state.live || state.chatCreating || state.chatSending || !state.chatContext?.taskId) return;
  const context = state.chatContext;
  const hint = context.intent === "hint";
  if (hint && (!context.pending || !restrictedHintScopeCurrent(context))) return false;
  const text = context.pending?.text || state.chatDraft.trim();
  if (!text) return;
  context.pending ||= newChatRequest(text);
  const pending = context.pending, epoch = chatEpoch, sequence = ++chatPostSequence;
  const actorId = api.state.user?.id, transition = state.transitionId;
  const owned = () => context === state.chatContext && epoch === chatEpoch && sequence === chatPostSequence
    && actorId === api.state.user?.id && transition === state.transitionId && !state.loggingOut && state.role === "student";
  const current = () => owned() && (!hint || restrictedHintScopeCurrent(context));
  state.chatSending = true;
  state.chatPolicyLoading = true;
  state.chatResponseError = "";
  render();
  try {
    if (hint ? !await prepareRestrictedHint(context, current, savedHint === pending) : !await prepareChatMessage(pending, current)) return false;
    state.chatCreating = true;
    render();
    let conversation = context.conversation;
    if (!conversation) {
      context.title ||= hint ? "本题学习提示" : `关于${(context.stem || "这道题").slice(0, 36)}`;
      context.createRequestId ||= `chat-${crypto.randomUUID()}`;
      conversation = await api.createConversation(context.title, context.createRequestId, context.courseId || null);
      if (!current()) return;
      context.conversationId = conversation.id;
      context.conversation = conversation;
      state.liveConversations = [conversation, ...state.liveConversations.filter((item) => item.id !== conversation.id)];
    }
    if (!current()) return;
    state.chatCreating = false;
    render();
    if (hint && (!restrictedHintScopeCurrent(context) || state.draftDirty || state.studentWrites?.[context.taskId]?.pending)) return false;
    if (hint) { context.savedAttempt = JSON.stringify(state.answers[state.currentQ]); context.hintSent = true; }
    const receipt = await api.postMessage(conversation.id, pending.text, {
      taskId: pending.taskId, questionVersionId: pending.questionVersionId, requestId: pending.requestId,
      ...(hint ? { intent: pending.intent, hintLevel: pending.hintLevel } : {}),
    });
    if (hint && (!receipt?.response_id || !receipt.message_id)) throw new Error("提示请求回执未确认，请重试原请求。");
    if (!current()) return;
    state.activeConversationId = conversation.id;
    setChatRoute(conversation.id);
    state.activeConversation = conversation;
    state.chatContexts[conversation.id] = context;
    delete state.chatContexts.__new;
    state.studentPage = "chat";
    state.studentTab = "companion";
    state.chatDrafts[conversation.id] = "";
    delete state.chatDrafts.__new;
    state.chatMessages = [{ id: receipt.message_id, client_request_id: pending.requestId, role: "user", text: pending.text,
      created_at: pending.createdAt, response: { response_id: receipt.response_id, status: receipt.status || "pending", text: "", last_event_id: 0,
        ...(hint ? { intent: "hint", hint_level: pending.hintLevel, task_id: pending.taskId, question_version_id: pending.questionVersionId } : {}) } }];
    state.chatDraft = "";
    context.pending = null;
    connectChatResponse(receipt.response_id, 0);
    return true;
  } catch (error) {
    if (!current()) return;
    if (hint && error?.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
      context.hintSent = false;
      context.pending = null;
    }
    state.chatResponseError = error?.message || "发送结果未确认。重试会沿用同一请求。";
  } finally {
    if (owned()) {
      state.chatCreating = false;
      state.chatSending = false;
      state.chatPolicyLoading = false;
      if (current()) render();
    }
  }
}

async function recordChatHelp(pending) {
  if (!pending.taskId || pending.help.recorded) return;
  const questionId = pending.questionId || pending.questionVersionId?.replace(/[:@][^:@]+$/, "");
  if (!questionId) throw new Error("题目范围尚未核验，求助记录未保存，请从作答页重新打开学伴。");
  try {
    await api.helpEvent(pending.taskId, {
      question_id: questionId,
      payload: { help_type: "hint", source: "companion", request_semantics: "requested" },
    }, pending.help.requestId);
    pending.help.recorded = true;
  } catch (error) {
    throw new Error(`求助记录未保存，消息尚未发送：${error?.message || "请稍后重试"}`);
  }
}

async function sendStudentMessage() {
  const id = state.activeConversationId;
  if (!id || !state.live || state.chatSending || state.chatContext?.intent === "hint") return;
  state.chatPendingSends ||= {};
  let pending = state.chatPendingSends[id];
  if (!pending) {
    if (state.chatMessages.some((item) => item.response?.response_id && chatResponseActive(item.response))) return;
    const text = state.chatDraft.trim();
    if (!text) return;
    pending = newChatRequest(text, id);
    state.chatPendingSends[id] = pending;
  }
  if (pending.conversationId !== id) return;
  const sequence = ++chatPostSequence, epoch = chatEpoch;
  const current = () => sequence === chatPostSequence && epoch === chatEpoch && id === state.activeConversationId;
  state.chatSending = true;
  state.chatPolicyLoading = true;
  state.chatResponseError = "";
  mergeChatMessages([]);
  renderStudentChatIfActive(epoch);
  try {
    if (!await prepareChatMessage(pending, current)) return;
    const receipt = await api.postMessage(id, pending.text, {
      taskId: pending.taskId, questionVersionId: pending.questionVersionId, requestId: pending.requestId,
    });
    if (!current()) return;
    mergeChatMessages([{ id: receipt.message_id, client_request_id: pending.requestId, role: "user", text: pending.text,
      created_at: pending.createdAt, response: { response_id: receipt.response_id, status: receipt.status || "pending", text: "", last_event_id: 0 } }]);
    delete state.chatPendingSends[id];
    state.chatDraft = "";
    state.chatDrafts[id] = "";
    connectChatResponse(receipt.response_id, 0);
  } catch (error) {
    if (!current()) return;
    state.chatResponseError = error?.message || "发送结果未确认。重试会复用同一请求。";
  } finally {
    if (current()) {
      state.chatSending = false;
      state.chatPolicyLoading = false;
      renderStudentChatIfActive(epoch);
    }
  }
}

async function readConversation(id) {
  await loadConversation(id, { archived: state.chatArchived });
}

async function submitPaper() {
  if (state.loading || state.loggingOut || state.studentSubmitBusy || state.studentComposing || state.hintPreparing
      || pendingRestrictedHint() || state.liveTask?.submission_closed) return false;
  const write = state.live && state.liveTask ? studentWrite() : null;
  if (!write?.submit) {
    const count = state.liveTask?.questions?.length ?? state.answers.length;
    const missing = Array.from({ length: count }, (_, index) => state.answers[index])
      .findIndex((value, index) => answerMissing(value, state.liveTask?.questions?.[index]));
    if (missing >= 0) {
      state.currentQ = missing;
      toast(`第 ${missing + 1} 题还没作答`);
      render();
      return false;
    }
  }
  if (write) {
    if (!write.submit && (write.blocked || !state.liveLease || state.banner === "takeover" || state.liveTask.diagnostic_required)) {
      toast("当前没有编辑权限，请先恢复作答"); return false;
    }
    if (write.submit && write.flight) return false;
    state.studentSubmitBusy = true;
    clearStudentSaveTimers(write);
    refreshStudentSaveStatus();
    try {
      if (!write.submit) {
        const input = JSON.stringify(state.answers);
        // 等已有保存完成，再至多补发一次当前快照；输入在确认交卷期间锁定。
        if (write.flight) await write.flight;
        if (!studentWriteCurrent(write) || write.blocked) return false;
        if (write.pending || state.draftDirty || write.savedInput !== input) await saveLiveDraft();
        if (!studentWriteCurrent(write) || write.pending || write.blocked || state.draftDirty
            || write.savedInput !== JSON.stringify(state.answers)) return false;
        if (!studentConfirm("服务器已确认保存当前作答。确认交卷？交卷后本次作答将固定，并进入评分。")) return false;
        write.submit = { requestId: `submit-${crypto.randomUUID()}`, version: write.version, lease: { ...state.liveLease } };
      }
      const command = write.submit;
      write.flight = api.submitTask(write.taskId, command.version, command.lease, command.requestId);
      refreshStudentSaveStatus();
      const submitted = await write.flight;
      if (!studentWriteCurrent(write)) return false;
      if (!submitted || submitted.id !== write.taskId || !Number.isInteger(submitted.version) || submitted.version <= command.version
          || !["submitted", "grading", "results_ready", "awaiting_manual", "closed"].includes(submitted.status)) {
        throw new Error("交卷回执不完整，结果待确认，请重试原交卷请求");
      }
      write.submit = null;
      write.error = "";
      write.version = submitted.version;
      stopStudentEditing();
      state.liveLease = null;
      updateStudentTask(submitted);
      state.submitted = true;
      state.selectedAttemptId = null;
      state.studentPage = "result";
      state.banner = "awaiting";
      toast("已交卷，正在后台评卷");
      render();
      pollTask(write.taskId, write.generation);
      return true;
    } catch (error) {
      if (studentWriteCurrent(write)) {
        if (studentWriteFailed(write, error)) write.submit = null;
        toast(write.submit ? "交卷结果未确认，请重试原交卷请求；不会重复新建提交" : write.error);
      }
      return false;
    } finally {
      write.flight = null;
      if (studentWriteCurrent(write)) {
        state.studentSubmitBusy = false;
        refreshStudentSaveStatus();
        scheduleStudentSave();
      }
    }
  }
  state.submitted = true;
  state.studentPage = "result";
  state.banner = state.banner === "awaiting" ? "awaiting" : "";
  toast("已交卷，成绩以这次快照为准");
  render();
  return true;
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
      if (generation === state.transitionId && state.screen === "app" && state.liveTask?.id === taskId) {
        state.banner = "grading-refresh-failed";
        render();
      }
      return;
    }
  }
  if (generation === state.transitionId && state.screen === "app" && state.liveTask?.id === taskId) {
    state.banner = "grading-refresh-stopped";
    render();
  }
}

/** 学生进工作区时先拿编辑租约；待确认命令和冲突只能由明确恢复操作处理。 */
async function openLiveTask() {
  if (!state.live || !state.liveTask || state.liveTask.submission_closed || state.liveTask.diagnostic_required || state.loggingOut) return false;
  const taskId = state.liveTask.id, write = studentWrite();
  const local = state.studentDrafts[taskId];
  if (local?.dirty) { state.answers = studentSnapshot(local.answers); state.draftDirty = true; }
  if (write.pending || write.submit || write.blocked) {
    state.studentPage = "workspace";
    state.banner = write.blocked ? "takeover" : "save-fail";
    render();
    return false;
  }
  try {
    state.loading = true;
    render();
    const reusable = state.liveLease?.task_id === taskId && state.liveLease.lease_until * 1000 > Date.now();
    const lease = reusable ? state.liveLease : await api.acquireLease(taskId, studentDeviceId());
    if (!studentWriteCurrent(write)) return false;
    const full = await api.task(taskId);
    if (!studentWriteCurrent(write)) return false;
    updateStudentTask(full);
    if (taskFinished(full) || full.diagnostic_required) {
      state.liveLease = null;
      stopStudentEditing();
      state.studentPage = full.diagnostic_required ? "home" : "result";
      return false;
    }
    state.studentPage = "workspace";
    const expected = reusable ? write.version : lease.task_version;
    if (full.version !== expected || local?.dirty && !reusable && lease.task_version !== write.version + 1) {
      studentWriteFailed(write, { status: 409, code: "draft_changed", message: "任务版本已变化，请选择如何恢复作答" });
      return false;
    }
    state.liveLease = lease;
    write.version = full.version;
    const draft = full.draft_answers || {};
    state.answers = local?.dirty ? studentSnapshot(local.answers) : (full.questions || []).map((question) => draft[question.id] ?? "");
    state.draftDirty = Boolean(local?.dirty);
    state.banner = "";
    state.currentQ = 0;
    startLeaseRenewal(taskId, state.transitionId);
    scheduleStudentSave();
    return true;
  } catch (error) {
    if (!studentWriteCurrent(write)) return false;
    studentWriteFailed(write, { status: 409, code: "lease_unavailable", message: error?.message || "另一端正在作答，稍后再试" });
    state.studentPage = "workspace";
    if (!local?.dirty) state.answers = (state.liveTask.questions || []).map((question) => state.liveTask.draft_answers?.[question.id] ?? "");
    toast(write.error);
    return false;
  } finally {
    if (studentWriteCurrent(write)) { state.loading = false; render(); }
  }
}

function startLeaseRenewal(taskId, generation) {
  clearInterval(leaseTimer);
  const write = studentWrite();
  let renewing = false;
  const timer = setInterval(async () => {
    if (timer !== leaseTimer || !studentWriteCurrent(write) || generation !== state.transitionId || !state.liveLease
        || state.liveTask?.id !== taskId || state.liveTask.submission_closed || state.liveTask.diagnostic_required) {
      clearInterval(timer);
      if (leaseTimer === timer) leaseTimer = 0;
      return;
    }
    if (renewing) return;
    const lease = state.liveLease;
    renewing = true;
    try {
      const renewed = await api.renewLease(taskId, lease);
      if (timer === leaseTimer && studentWriteCurrent(write) && state.liveLease === lease) state.liveLease = { ...lease, ...renewed };
    } catch (error) {
      if (timer !== leaseTimer || !studentWriteCurrent(write) || state.liveLease !== lease) return;
      // 临时断网或服务错误不能证明租约已丢失；保留原租约以核对未决保存的回执。
      studentWriteFailed(write, error);
      refreshStudentSaveStatus();
    } finally { renewing = false; }
  }, 60000);
  leaseTimer = timer;
}

/** 首诊没有草稿接口，未提交作答与暂时不会标记只留在本页内存。 */
async function runDiagnostic() {
  if (!state.live || !state.liveTask || state.liveTask.submission_closed || state.loggingOut) return;
  const taskId = state.liveTask.id, write = studentWrite();
  stopStudentEditing();
  state.liveLease = null;
  try {
    state.loading = true;
    render();
    const diagnostic = await api.startDiagnostic(taskId, state.liveTask.version, `diagnostic-${crypto.randomUUID()}`);
    if (!studentWriteCurrent(write)) return;
    const local = state.diagnosticDrafts?.[taskId];
    if (local?.diagnostic.id === diagnostic.id) {
      state.diagnosticAnswers = studentSnapshot(local.answers);
      state.diagnosticUnknown = [...local.unknown];
    } else if (state.diagnostic?.id !== diagnostic.id) {
      state.diagnosticAnswers = diagnostic.questions.map(() => "");
      state.diagnosticUnknown = [];
    }
    state.diagnostic = diagnostic;
    state.currentQ = 0;
    state.studentPage = "diagnostic";
  } catch (error) {
    if (studentWriteCurrent(write)) toast(error?.message || "短诊没有打开");
  } finally {
    if (studentWriteCurrent(write)) { state.loading = false; render(); }
  }
}

async function submitLiveDiagnostic() {
  if (!state.diagnostic || !state.liveTask || state.loading || state.loggingOut || state.studentComposing || state.liveTask.submission_closed) return;
  const questions = state.diagnostic.questions || [], unknown = state.diagnosticUnknown || [];
  if (!state.diagnosticCommand) {
    if (unknown.some((id) => !questions.some((question) => question.id === id))
        || questions.some((question, index) => unknown.includes(question.id) && !answerMissing(state.diagnosticAnswers[index], question))) {
      toast("暂时不会与作答不能同时选择，请检查短诊作答"); return;
    }
    const missing = questions.findIndex((question, index) => !unknown.includes(question.id) && answerMissing(state.diagnosticAnswers[index], question));
    if (missing >= 0) {
      state.currentQ = missing;
      toast(`第 ${missing + 1} 题还没作答`);
      render();
      return;
    }
    state.diagnosticCommand = { diagnosticId: state.diagnostic.id, taskId: state.liveTask.id,
      requestId: `diagnostic-submit-${crypto.randomUUID()}`, unknown: [...unknown], accepted: false,
      answers: studentSnapshot(Object.fromEntries(questions.filter((question) => !unknown.includes(question.id))
        .map((question) => [question.id, state.diagnosticAnswers[questions.indexOf(question)]]))) };
  }
  const command = state.diagnosticCommand, write = studentWrite();
  const current = () => studentWriteCurrent(write) && state.diagnosticCommand === command && state.diagnostic?.id === command.diagnosticId;
  try {
    state.loading = true;
    render();
    if (!command.accepted) {
      await api.submitDiagnostic(command.diagnosticId, command.answers, command.requestId, command.unknown);
      if (!current()) return;
      command.accepted = true;
    }
    const full = await api.task(command.taskId);
    if (!current()) return;
    if (full.diagnostic_required) throw new Error("短诊状态仍待确认，请重试读取");
    updateStudentTask(full);
    write.version = full.version;
    delete state.diagnosticDrafts?.[command.taskId];
    state.diagnostic = null;
    state.diagnosticAnswers = [];
    state.diagnosticUnknown = [];
    state.diagnosticCommand = null;
    state.studentPage = "home";
    state.studentTab = "tasks";
    toast("短诊完成，可以开始正式作答");
  } catch (error) {
    if (!current()) return;
    if (!command.accepted && error?.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) state.diagnosticCommand = null;
    toast(error?.message || "短诊结果未确认，作答仅保留在本页；请重试原请求");
  } finally {
    if (studentWriteCurrent(write)) { state.loading = false; render(); }
  }
}

async function retryLiveTask() {
  if (state.loading || !state.liveTask || state.loggingOut || state.liveTask.submission_closed || state.studentSubmitBusy) return;
  const taskId = state.liveTask.id;
  const write = studentWrite();
  if (write.pending || write.submit || write.flight) { toast("请先确认原保存或交卷请求的结果"); return; }
  const generation = state.transitionId, actorId = api.state.user?.id;
  const current = () => generation === state.transitionId && actorId === api.state.user?.id && state.liveTask?.id === taskId && !state.loggingOut;
  state.loading = true;
  render();
  try {
    const full = await api.retryTask(taskId, state.liveTask.version, `retry-${Date.now()}`);
    if (!current()) return;
    updateStudentTask(full);
    stopStudentEditing();
    state.liveLease = null;
    delete state.studentWrites[taskId];
    delete state.studentDrafts[taskId];
    state.answers = [];
    state.draftDirty = false;
    state.submitted = false;
    state.seenSolution = true;
    state.loading = false;
    const opened = await openLiveTask();
    if (current()) {
      if (opened) toast("已开始重做，首次成绩保持不变");
      else { state.studentPage = "home"; state.studentTab = "tasks"; }
    }
  } catch (error) {
    if (current()) toast(error?.message || "本轮暂时不能重做");
  } finally {
    if (current()) { state.loading = false; render(); }
  }
}

/** 保存草稿：一次只发送一个冻结载荷；未知结果重放原版本、租约与请求 ID。 */
async function saveLiveDraft({ automatic = false } = {}) {
  if (!state.live || !state.liveTask || state.loggingOut || state.studentSuspended || state.studentComposing
      || state.liveTask.diagnostic_required || state.liveTask.submission_closed) return false;
  const task = state.liveTask, write = studentWrite(task);
  if (write.submit || write.blocked && !write.pending) return false;
  if (write.flight) return write.flight;
  if (!write.pending && (!state.liveLease || state.banner === "takeover")) return false;
  clearStudentSaveTimers(write);
  if (!write.pending) {
    const input = studentSnapshot(state.answers), payload = studentDraftPayload(task, input);
    write.pending = { requestId: `draft-${crypto.randomUUID()}`, version: write.version,
      lease: { ...state.liveLease }, ...payload, input, attempts: 0 };
  }
  const command = write.pending;
  command.attempts += 1;
  write.error = "";
  const run = async () => {
    try {
      const saved = await api.saveDraft(task.id, command.version, command.lease, command.answers, command.requestId);
      if (!studentWriteCurrent(write)) return false;
      if (!saved || !Number.isInteger(saved.version) || saved.version <= command.version || saved.id && saved.id !== task.id) {
        throw new Error("服务器保存回执不完整，结果待确认，请重试原请求");
      }
      // 重放可能返回当前任务而非旧快照；别把其他页面的更新当成本次确认。
      if (saved.draft_answers && (Object.keys(saved.draft_answers).length !== Object.keys(command.answers).length
          || Object.keys(command.answers).some((id) => JSON.stringify(saved.draft_answers[id]) !== JSON.stringify(command.answers[id])))
          || taskFinished(saved) || write.blocked || state.liveLease?.fence_token !== command.lease.fence_token
          || state.liveLease?.lease_token !== command.lease.lease_token) {
        write.pending = null;
        studentWriteFailed(write, { status: 409, code: "draft_changed", message: "服务器草稿或编辑权限已经变化" });
        return false;
      }
      write.pending = null;
      write.version = saved.version;
      write.savedInput = JSON.stringify(command.input);
      updateStudentTask({ ...state.liveTask, version: saved.version, status: saved.status || "in_progress", draft_answers: command.answers });
      state.draftDirty = command.incomplete || JSON.stringify(state.answers) !== write.savedInput;
      if (state.draftDirty) state.studentDrafts[task.id] = { answers: studentSnapshot(state.answers), dirty: true };
      else delete state.studentDrafts[task.id];
      state.savedAt = Date.now();
      state.banner = "";
      if (!automatic) toast(command.incomplete ? "完整作答已保存，未填完的题目仍留在本页" : state.draftDirty ? "先前作答已保存，新输入仍待保存" : "进度已保存");
      return !state.draftDirty;
    } catch (error) {
      if (!studentWriteCurrent(write)) return false;
      if (studentWriteFailed(write, error)) write.pending = null;
      if (!automatic) toast(write.error);
      return false;
    }
  };
  write.flight = run();
  const flight = write.flight;
  refreshStudentSaveStatus();
  try { return await flight; }
  finally {
    if (write.flight === flight) write.flight = null;
    if (studentWriteCurrent(write)) { refreshStudentSaveStatus(); scheduleStudentSave(); }
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

window.addEventListener("beforeunload", beforeStudentUnload);
window.addEventListener("pagehide", () => { closeStudentChatStream(); suspendStudentWork(); });
window.addEventListener("pageshow", (event) => { if (event.persisted) { resumeStudentWork(); resumeStudentChat(); } });
window.addEventListener("hashchange", () => {
  if (!state.live || state.screen !== "app" || state.role !== "student") return;
  const id = chatRoute();
  if (id) {
    if ((state.studentPage === "workspace" || state.studentPage === "diagnostic") && !leaveStudentWork()) { setChatRoute(null); return; }
    loadConversation(id);
  }
  else if (state.studentTab === "companion" && state.activeConversationId) {
    onAction({ currentTarget: { dataset: { action: "chat-back" } } });
  }
});

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
