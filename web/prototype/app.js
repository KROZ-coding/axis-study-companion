const stage = document.querySelector("#stage");
const view = document.querySelector("#view");
const toastEl = document.querySelector("#toast");
const reduceQuery = window.matchMedia("(prefers-reduced-motion: reduce)");

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
  role: null,
  who: "",
  teacherView: "home",
  studentTab: "companion",
  studentPage: "home",
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
  authInput: "",
  attempts: 0,
  capsLock: false,
  clock: { hm: "--:--", sec: "--", date: "" },
  step: 0,
  anchorA: { x: 900, y: 470 },
  anchorB: { x: 560, y: 640 },
  anchorYaw: 0.40,
  anchorPitch: 0.17,
  anchorSpin: 0,
  railLog: ["等待账号口令", "会话未开启"],
  railLabel: "入口 · 会话未开启",
  fallback: false,
  yaw: 0.5,
  pitch: 0.25,
};

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
  if (state.screen === "entry") renderEntry();
  else if (state.role === "teacher") renderTeacher();
  else renderStudent();
  bind();
  if (state.screen === "entry") {
    fitCanvas();
    refreshAnchors();
  }
}

const AUTH_COPY = {
  login: {
    title: "从上次停下的地方继续",
    lede: "老师回到班级，学生回到自己的题。进哪一边，由账号决定。",
    submit: "请求进入",
    placeholder: "输入账号，Enter 确认",
  },
  activate: {
    title: "第一次来，先设一个密码",
    lede: "用老师给你的一次性码激活。忘了密码，也从这里重设。",
    submit: "激活",
    placeholder: "账号 · 一次性码 · 新密码",
  },
  apply: {
    title: "申请教师账号",
    lede: "提交后等待审核，通过前不能进入工作台。",
    submit: "提交申请",
    placeholder: "姓名 · 学校邮箱 · 任教课程",
  },
};
const AUTH_STATUS = { ready: "就绪", checking: "核对中", denied: "未通过", sent: "已提交" };
const AUTH_ROLE = { login: "学员", activate: "新学员", apply: "申请人" };
const AUTH_STATE_LABEL = { denied: "未通过", checking: "核对中", sent: "已提交" };

function renderEntry() {
  const open = state.reveal > 0.5;
  state.shifted = open;
  const rv = state.reveal.toFixed(3);
  view.innerHTML = `
    <section class="entry ${open ? "shifted" : ""}" style="--reveal:${rv}">
      <div class="entry-canvas">
        ${state.fallback ? fallbackSvg() : ""}
        <div class="entry-layer keep rail" aria-hidden="true">
          <div class="rail-rule" style="--reveal:${rv}"></div>
          <div class="rail-marker" style="--reveal:${rv}"></div>
          <div class="rail-body" style="--reveal:${rv}">
            <i class="rail-bar"></i>
            <i class="rail-dash" style="top:358px"></i>
            <div class="rail-chip" style="left:44px; top:396px">智慧学伴 · 学习记录</div>
            <p class="rail-micro" style="top:416px">每一次作答、提示与材料引用都留在原处。判断写在依据旁边，记录不够时就写记录不够。</p>
            <div class="rail-chip" style="left:44px; top:632px">访问记录</div>
            <div class="rail-log" style="top:656px">
              ${state.railLog.map((row) => `<div class="row"><i></i>${escapeHtml(row)}</div>`).join("")}
            </div>
            <i class="rail-dash" style="left:44px; top:852px"></i>
            <i class="rail-hair" style="left:66px; top:853px"></i>
            <div class="rail-hints" style="top:872px">
              <span><b>↓</b>展开登录</span><span><b>拖</b>转动装置</span>
              <span><b>Tab</b>切换栏位</span><span><b>Enter</b>确认</span>
            </div>
            <i class="rail-foot" style="top:946px"></i>
            <div class="rail-label" style="left:71px; top:944px">${escapeHtml(state.railLabel)}</div>
          </div>
        </div>

        <div class="entry-layer keep brand" aria-hidden="true">
          <div class="brand-l1">智慧学伴</div>
          <div class="brand-l2"><span class="axis">AXIS</span></div>
          <div class="brand-l3">LEARNING EVIDENCE OS</div>
          <div class="brand-rule"></div>
        </div>

        <div class="entry-layer keep chrono" aria-hidden="true">
          <div class="chrono-steps">
            ${[0, 1, 2, 3, 4].map((i) => `<div class="step-node ${i === state.step ? "on" : ""}"><i></i></div>`).join("")}
          </div>
          <div class="chrono-time">
            <span class="chrono-hm">${state.clock.hm}</span><span class="chrono-sec">${state.clock.sec}</span>
          </div>
          <div class="chrono-date">${state.clock.date}</div>
          <div class="chrono-term"><i></i>本地演示 · 虚构样例</div>
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
          <p class="eyebrow">七年级数学 · 一元一次方程</p>
          <h1>把下一道题，放在看得懂的位置。</h1>
          <p class="lede">同一学习目标下，先看每人已有的依据，再决定讲解、练习和提示。这里是可点击原型，内容为虚构样例。</p>
          <span class="sample-flag">虚构样例 · 不是实时模型</span>
        </div>

        <div class="entry-layer entry-foot">
          <button class="scroll-hint" type="button" data-action="reveal" ${open ? "inert" : ""}>
            <span class="wheel" aria-hidden="true"><i></i></span>
            <span class="hint-main">向下滑动登录</span>
            <span class="hint-sub">拖动金属可以转动它</span>
          </button>
          <div class="entry-signature"><span>POWERED BY</span><b>AXIS</b><i></i></div>
        </div>

        ${authCard(open)}
      </div>
    </section>`;
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
  if (!root) return;
  root.querySelector(".chrono-hm").textContent = state.clock.hm;
  root.querySelector(".chrono-sec").textContent = state.clock.sec;
  root.querySelector(".chrono-date").textContent = state.clock.date;
}

function authCard(open) {
  const copy = AUTH_COPY[state.authTab];
  const busy = state.authStatus === "checking";
  const name = (state.authAccount || "").toUpperCase() || "尚未登记";
  const stateLabel = AUTH_STATE_LABEL[state.authStatus] || "会话未开启";
  const stateClass = state.authStatus === "denied" ? "alert" : state.authStatus === "sent" ? "ok" : "";
  return `<form class="auth-card ${state.authStatus === "denied" ? "denied" : ""}" data-action="auth" aria-labelledby="auth-title" ${open ? "" : "inert"}>
      <div class="auth-rear" aria-hidden="true"></div>
      <div class="auth-slab" aria-hidden="true">
        <div class="hatch"></div>
        <div class="ghost">${state.authStatus === "checking" ? "SECURE" : "INFO"}</div>
        <div class="ghost-name">${escapeHtml(name)}</div>
        <i class="auth-bev t"></i><i class="auth-bev l"></i><i class="auth-bev b"></i><i class="auth-bev r"></i>
      </div>
      <div class="auth-inner">
        <header class="auth-head">
          <span class="auth-mark"><i aria-hidden="true"></i>档案 · 入口</span>
          <div class="auth-head-right">
            <span class="auth-outline">学习证据系统</span>
            <span class="auth-brandtag">智慧学伴 AXIS</span>
          </div>
        </header>

        <div class="auth-photo" aria-hidden="true">
          <div class="film">
            <svg class="sil" viewBox="0 0 100 124" preserveAspectRatio="none">
              <defs><pattern id="authHatch" width="3.2" height="3.2" patternUnits="userSpaceOnUse"><circle cx="1.6" cy="1.6" r="0.75" fill="#ffffff" fill-opacity="0.16"/></pattern></defs>
              <path d="M 50 14 C 63 14 71 24 71 38 C 71 50 66 60 59 65 L 60 72 C 72 75 86 80 93 90 C 99 99 101 112 101 124 L -1 124 C -1 112 1 99 7 90 C 14 80 28 75 40 72 L 41 65 C 34 60 29 50 29 38 C 29 24 37 14 50 14 Z" fill="#14343f" />
              <path d="M 50 14 C 63 14 71 24 71 38 C 71 50 66 60 59 65 L 60 72 C 72 75 86 80 93 90 C 99 99 101 112 101 124 L -1 124 C -1 112 1 99 7 90 C 14 80 28 75 40 72 L 41 65 C 34 60 29 50 29 38 C 29 24 37 14 50 14 Z" fill="url(#authHatch)" />
            </svg>
            <span class="classified">${busy ? "核对中" : "虚构样例"}</span>
          </div>
          <div class="badge">AX</div>
          <i class="tick" style="right:-3px; top:-3px"></i><i class="tick" style="left:-3px; bottom:-3px"></i><i class="tick" style="right:-3px; bottom:-3px"></i>
        </div>

        <div class="auth-id">
          <div class="auth-rank">
            <span class="auth-role">${AUTH_ROLE[state.authTab]}</span>
            <span class="auth-state ${stateClass}">${stateLabel}</span>
          </div>
          <div class="auth-name"><b>${escapeHtml(name)}</b></div>
          <div class="auth-file">
            <span class="stamp">AXIS</span>
            <div class="info">
              <div class="loaded">学习记录已载入 <i></i></div>
              <div class="mono">■ ${state.authTab === "apply" ? "申请" : "七（1）班"} · ${AUTH_STATUS[state.authStatus]}</div>
            </div>
          </div>
        </div>

        <div class="auth-rings" aria-hidden="true">
          <div class="ring"><i class="${state.capsLock ? "on" : ""}"></i><span>CAPS</span></div>
          <div class="ring"><i class="${state.authAccount ? "on" : ""}"></i><span>账号</span></div>
          <div class="ring"><i class="${state.authTab === "apply" ? "" : "on"}"></i><span>口令</span></div>
        </div>

        <div class="auth-profile">
          <div class="ph">档案信息</div>
          <dl>
            <dt>身份</dt><dd>${AUTH_ROLE[state.authTab]}</dd>
            <dt>范围</dt><dd>${state.authTab === "apply" ? "待审核" : "七（1）班 · 数学"}</dd>
            <dt>状态</dt><dd>${stateLabel}</dd>
            <dt>会话</dt><dd>${open ? "已就绪" : "未开启"}</dd>
          </dl>
          <i class="rule"></i><span class="foot">智慧学伴 AXIS</span>
        </div>

        <div class="auth-main">
          <div class="auth-main-head">
            <span class="label"><i></i>授权口令</span>
            <span class="attempt ${state.authStatus === "denied" ? "hot" : ""}">${state.authStatus === "denied" ? "再来一次" : `第 ${state.attempts + 1} 次`}</span>
          </div>
          <h2 id="auth-title" class="auth-title">${copy.title}</h2>
          <p class="auth-lede">${copy.lede}</p>
          <div class="tabs" role="tablist" aria-label="入口方式">
            <button class="chip" type="button" role="tab" data-tab="login" aria-pressed="${state.authTab === "login"}">账号口令</button>
            <button class="chip" type="button" role="tab" data-tab="activate" aria-pressed="${state.authTab === "activate"}">一次性码</button>
            <button class="chip" type="button" role="tab" data-tab="apply" aria-pressed="${state.authTab === "apply"}">申请账号</button>
          </div>
          ${authFields()}
          <p class="error-line" role="alert">${escapeHtml(state.error)}</p>
          ${state.authTab === "login" ? keyBar(copy, busy) : `<button class="btn primary wide" type="submit" ${busy ? "disabled" : ""}>${busy ? "核对中…" : copy.submit}</button>`}
          <div class="auth-alt">${authLinks()}</div>
          <div class="entry-note">
            ${state.authTab === "login" ? `<details class="proto-note"><summary>原型账号</summary><p>teacher 进教师工作台，linxia 进学生端，口令随意。一次性码填 000000 看过期，账号或口令填 disabled 看停用。</p></details>` : "<span></span>"}
            <button class="linkish" type="button" data-action="back-entry">回到开场</button>
          </div>
        </div>
      </div>
    </form>`;
}

function authLinks() {
  if (state.authTab !== "login") return `<button class="linkish" type="button" data-tab="login">已有账号，直接登录</button>`;
  return `<button class="linkish" type="button" data-tab="activate">第一次来？用一次性码激活</button>
    <button class="linkish" type="button" data-tab="apply">老师还没有账号</button>`;
}

/** 口令条：登录时就是密码输入本身 —— 每敲一个字符落一个白格。
 *  它不是第二个密码框，而是把这个密码框画成档案里的授权键条。 */
function keyBar(copy, busy) {
  return `<div class="keybar">
      <div class="keybar-cells" aria-hidden="true">
        ${state.authInput
          ? Array.from({ length: Math.min(state.authInput.length, 22) }, () => '<i class="keybar-cell"></i>').join("")
          : `<span class="keybar-ghost">${copy.placeholder}</span>`}
      </div>
      <input name="password" type="password" aria-label="口令" ${busy ? "disabled" : ""} autocomplete="current-password" />
      <button type="submit" ${busy ? "disabled" : ""}>${busy ? "核对中" : copy.submit}<span aria-hidden="true">→</span></button>
    </div>`;
}

function authFields() {
  const account = escapeHtml(state.authAccount);
  if (state.authTab === "apply") {
    return `<div class="auth-fields">
      <label class="field">姓名<input name="name" required autocomplete="name" value="陈宁"></label>
      <label class="field">学校邮箱<input name="email" type="email" required autocomplete="email" value="chen@school.example"></label>
      <label class="field">任教课程<input name="course" required value="七年级数学"></label>
    </div>`;
  }
  if (state.authTab === "activate") {
    return `<div class="auth-fields">
      <label class="field">账号<input name="account" required autocomplete="username" value="${account || "linxia"}"></label>
      <label class="field">一次性码<input name="code" required autocomplete="one-time-code" placeholder="6 位码"></label>
      <label class="field">新密码<input name="password" type="password" required minlength="8" autocomplete="new-password" placeholder="至少 8 位"></label>
    </div>`;
  }
  // 登录：账号在这里，口令在下方那条授权键条里（见 keyBar）
  return `<div class="auth-fields compact">
    <label class="field">账号<input name="account" required autocomplete="username" value="${account}" placeholder="老师发给你的账号"></label>
  </div>`;
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
        <div class="brand">智慧学伴</div>
        ${nav("home", "班级学情")}${nav("student", "学生依据")}${nav("materials", "课程与题目")}${nav("reports", "阶段成果")}
        <div class="spacer"></div>
        <p class="who muted">${escapeHtml(state.who)} · ${sample.className}</p>
        <button class="nav-btn" data-action="logout">退出</button>
      </aside>
      <main class="main">${page()}</main>
    </div>
    ${state.drawer ? drawer() : ""}
    <div class="preview-switch"><button class="btn" data-action="as-student">看学生手机流程</button></div>
    ${stateMenu()}`;
}

function nav(id, label) {
  return `<button class="nav-btn" data-view="${id}" aria-current="${state.teacherView === id ? "page" : "false"}">${label}</button>`;
}

function teacherHome() {
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

function renderStudent() {
  const page = state.studentPage;
  view.innerHTML = `
    <div class="phone-frame">
      <header class="phone-top row" style="justify-content:space-between">
        <button class="icon-btn" data-action="chats" aria-label="会话">会话</button>
        <strong>智慧学伴</strong>
        <button class="linkish" data-action="logout">退出</button>
      </header>
      ${page === "workspace" ? workspace() : page === "result" ? resultPage() : studentTab()}
      ${state.sheet ? sheet() : ""}
      <nav class="dock" aria-label="学生导航">
        ${dock("companion", "学伴")}${dock("tasks", "任务")}${dock("mine", "我的学习")}
      </nav>
    </div>
    ${stateMenu()}`;
}

function dock(id, label) {
  return `<button data-tab="${id}" aria-current="${state.studentTab === id && state.studentPage === "home" ? "page" : "false"}">${label}</button>`;
}

function studentTab() {
  if (state.studentTab === "tasks") return tasks();
  if (state.studentTab === "mine") return mine();
  return companion();
}

function companion() {
  return `
    <div class="phone-body">
      <p class="muted">上次停在「去括号再练一次」。虚构对话，不是实时模型。</p>
      <div class="bubble">去括号时，括号外的数要乘到每一项。要先听一遍，还是直接做题？</div>
      <div class="bubble me">我还是会漏掉后面的数。</div>
      <div class="row">
        <button class="btn" data-action="explain">先听讲解</button>
        <button class="btn primary" data-action="practice">直接练习</button>
        <button class="btn" data-action="find">帮我找问题</button>
      </div>
      ${state.banner === "quota" ? '<div class="state-banner bad">今日自由对话额度已用完。已交卷的评分仍会继续。</div>' : ""}
      <form class="field" data-action="send"><input name="text" placeholder="说说卡在哪里" aria-label="发给学伴"></form>
    </div>`;
}

function tasks() {
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

function mine() {
  return `
    <div class="phone-body">
      <h1 class="page" style="font-size:32px">我的学习</h1>
      <section class="panel"><h3>去括号</h3><p>先看具体反馈：常数项还没乘到。数字可以再展开。</p><button class="linkish" data-action="toggle-mastery">展开 46%</button>${state.banner === "mastery" ? "<p>4 条有效作答，不足以说已经稳定。不是正确率。</p>" : ""}</section>
      <section class="panel"><h3>收藏</h3><p class="muted">一道未交卷题目的解析已收起，交卷后恢复。</p></section>
    </div>`;
}

function workspace() {
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

function sheet() {
  const sheets = {
    chats: `<h2 class="section">会话</h2>${state.conversations.map((item, index) => `<button class="btn" data-chat="${index}">${item}</button>`).join("")}<form class="field" data-action="new-chat"><input name="title" placeholder="新会话名称" aria-label="新会话名称"><button class="btn" type="submit">新建</button></form>`,
    formula: `<h2 class="section">插入公式</h2><div class="row"><button class="chip" data-insert="a/b">分数</button><button class="chip" data-insert="x^2">乘方</button><button class="chip" data-insert="√x">根号</button></div><p class="muted">插入后仍需你确认，不会直接交卷。</p>`,
    voice: `<h2 class="section">语音转成文字</h2><p>听到的是：二分之一的平方。</p><label class="field">确认后发送<input value="(1/2)^2"></label><button class="btn primary" data-action="confirm-voice">确认文字</button>`,
    explain: `<h2 class="section">去括号</h2><p>3 要同时乘 x 和 −2，所以是 3x − 6。这是讲解，不是这道未交题目的完整评分。</p>`,
    export: `<h2 class="section">${escapeHtml(state.exportName || "导出")}</h2><p>范围：${sample.className}，截止 11 月 10 日 21:00。原型不下载文件。</p><div class="row"><span class="tag">网页</span><span class="tag">PDF</span><span class="tag">Excel</span><span class="tag">JSON</span><span class="tag">CSV</span></div>`,
  };
  return `<div class="drawer-back" data-action="close-sheet"></div><section class="sheet" role="dialog">${sheets[state.sheet] || ""}<div class="row"><button class="btn" data-action="close-sheet">关闭</button></div></section>`;
}

function stateMenu() {
  const items = state.role === "teacher"
    ? [["", "正常学情"], ["empty", "无记录"], ["loading", "整理中"], ["error", "整理失败"]]
    : [["", "正常"], ["quota", "额度用完"], ["save-fail", "保存失败"], ["takeover", "另一台作答"], ["awaiting", "待教师评分"]];
  return `<details class="proto-states"><summary class="btn">原型状态</summary><div class="panel">${items.map(([id, label]) => `<button class="btn" type="button" data-state="${id}" aria-pressed="${state.banner === id}">${label}</button>`).join("")}<p class="muted">只切换界面，不改变虚构样例的含义。</p></div></details>`;
}

function bind() {
  view.querySelectorAll("[data-action]:not(form)").forEach((node) => node.addEventListener("click", onAction));
  view.querySelectorAll("[data-tab]").forEach((node) => node.addEventListener("click", () => {
    if (state.role === "student") {
      state.studentTab = node.dataset.tab;
      state.studentPage = "home";
    } else state.authTab = node.dataset.tab;
    state.error = "";
    render();
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
    state.currentQ = Number(node.dataset.q);
    render();
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
  // 口令条：每敲一个字符就落一个白格，反映的是条内那个密码输入本身
  const keyInput = view.querySelector(".keybar input");
  if (keyInput) {
    keyInput.addEventListener("input", () => {
      state.authInput = keyInput.value;
      const cells = view.querySelector(".keybar-cells");
      if (!cells) return;
      const shown = Array.from({ length: Math.min(state.authInput.length, 22) }, () => '<i class="keybar-cell"></i>').join("");
      cells.innerHTML = shown || `<span class="keybar-ghost">${AUTH_COPY[state.authTab].placeholder}</span>`;
    });
    keyInput.addEventListener("keyup", (event) => {
      state.capsLock = typeof event.getModifierState === "function" && event.getModifierState("CapsLock");
    });
  }
}

function onAction(event) {
  const action = event.currentTarget.dataset.action;
  if (action === "reveal") {
    animateReveal(1);
    return;
  }
  if (action === "back-entry") {
    state.error = "";
    state.authStatus = "ready";
    state.authInput = "";
    animateReveal(0);
    return;
  }
  if (action === "logout") {
    Object.assign(state, {
      screen: "entry", shifted: false, reveal: 0, authStatus: "ready",
      authAccount: "", authInput: "", role: null, drawer: false, sheet: null, error: "",
      railLog: ["等待账号口令", "会话未开启"], railLabel: "入口 · 会话未开启", step: 0,
    });
    if (scene) scene.setReveal(0);
    render();
    return;
  }
  if (action === "open-drawer") state.drawer = true;
  if (action === "close-drawer") state.drawer = false;
  if (action === "prepare") toast("还差 1 道检验题，备齐前不会发布");
  if (action === "as-student") {
    state.role = "student";
    state.who = "林夏";
    state.studentPage = "home";
    state.studentTab = "companion";
  }
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
  if (action === "practice" || action === "open-task") {
    state.studentPage = "workspace";
    state.sheet = null;
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

// 登录与激活共用同一条口令输入：登录时它同时充当账号，
// 激活/申请走下方各自的独立字段，所以这里要按 tab 取不同的值。
function readKeyValue(data) {
  const key = String(data.get("key") || "").trim();
  const account = String(data.get("account") || "").trim();
  const name = String(data.get("name") || "").trim();
  return { key, account, name };
}

/** 口令条里的输入就是密码；登录时它同时充当"账号不填时的兜底标识" */
function readPassword(data) {
  return String(data.get("password") || data.get("key") || "");
}

function submitAuth(data) {
  if (state.authStatus === "checking") return;
  const { key, account, name } = readKeyValue(data);
  const password = readPassword(data);
  const code = String(data.get("code") || "");
  const who = account || key || name;
  state.authAccount = account || key || name;
  const fail = (message) => {
    state.authStatus = "denied";
    state.error = message;
    state.attempts += 1;
    state.railLog = [`第 ${state.attempts} 次未通过`, "会话未开启"];
    state.step = 4;
    if (scene) scene.setDenied(1);
    render();
    window.setTimeout(() => { if (scene) scene.setDenied(0); }, 900);
  };
  if (state.authTab === "apply") {
    state.authStatus = "sent";
    state.error = "";
    state.railLog = ["申请已提交", "等待审核"];
    state.railLabel = "申请 · 待审核";
    state.step = 3;
    toast("申请已提交，审核通过前不能进入工作台");
    render();
    return;
  }
  if (state.authTab === "activate" && (code === "expired" || code === "000000")) {
    return fail("这个一次性码已过期，请向老师再要一个。");
  }
  if (password === "disabled" || who === "disabled" || key === "disabled") {
    state.step = 4;
    return fail("账号已停用，会话也已失效。");
  }
  state.authStatus = "checking";
  state.error = "";
  state.railLog = ["正在核对账号", "会话未开启"];
  state.railLabel = "入口 · 核对中";
  state.step = 2;
  if (scene) scene.setVerify(1);
  render();
  window.setTimeout(() => {
    state.authStatus = "ready";
    state.role = who.startsWith("lin") ? "student" : "teacher";
    state.who = state.role === "student" ? "林夏" : "陈宁";
    state.step = 3;
    if (scene) {
      scene.setVerify(0);
      scene.playDissolve(900);
    }
    window.setTimeout(() => {
      state.screen = "app";
      state.banner = "";
      toast(state.role === "student" ? "进入林夏的学习" : "进入七（1）班");
      render();
      if (scene) {
        scene.setReveal(0);
        scene.setAssembled();
      }
    }, state.reduce ? 0 : 700);
  }, state.reduce ? 0 : 900);
}

function submitPaper() {
  const missing = state.answers.findIndex((item) => !item.trim());
  if (missing >= 0) {
    state.currentQ = missing;
    toast(`第 ${missing + 1} 题还没作答`);
    render();
    return;
  }
  state.submitted = true;
  state.studentPage = "result";
  state.banner = state.banner === "awaiting" ? "awaiting" : "";
  toast("已交卷，成绩以这次快照为准");
  render();
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
  refreshAnchors();
}

function animateReveal(target) {
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
  if (state.screen !== "entry" || state.reduce) return;
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
  if (state.screen !== "entry" || state.reduce) return;
  if (state.reveal > 0.5) return;
  if (event.target.closest("form, button, a, input, textarea, details, summary")) return;
  if (event.target.closest(".entry-copy, .entry-foot, .chrono, .rail, .auth-card")) return;
  // 单独点一下玻璃（没有拖动）也推进：转起来之后，点击是最自然的"进去"
  animateReveal(1);
});
document.addEventListener("pointercancel", () => { drag = null; });

document.addEventListener("wheel", (event) => {
  if (state.screen !== "entry") return;
  if (event.target.closest(".auth-card")) return;
  event.preventDefault();
  if (state.reduce) return;
  // 一格滚轮约 100~120px。除以 1100 让大约七八格才能完全展开，
  // 中间会经过"卡片半透明、还不能输入"的阶段，不会是瞬时跳到底。
  setReveal(state.reveal + event.deltaY / 1100);
}, { passive: false });

let touchY = null;
document.addEventListener("touchstart", (event) => {
  if (state.screen === "entry") touchY = event.touches[0].clientY;
}, { passive: true });
document.addEventListener("touchmove", (event) => {
  if (touchY === null || state.screen !== "entry" || state.reduce) return;
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
  if (scene) {
    scene.stop();
    scene = null;
  }
  if (state.screen === "entry") {
    const canvas = document.querySelector("#glass");
    if (canvas) canvas.getContext("webgl") && canvas.width > 0;
    mountScene();
    setReveal(state.reduce ? 1 : state.reveal);
    render();
    refreshAnchors();
  }
});

// ── 引导点跟随金属的缓慢自转 ─────────────────────────────────────────
setInterval(() => {
  if (state.screen !== "entry" || state.reduce) return;
  const now = performance.now() / 1000;
  state.anchorYaw = 0.40 + Math.sin(now * 0.085) * 0.26;
  state.anchorPitch = 0.17 + Math.sin(now * 0.052 + 0.8) * 0.08;
  refreshAnchors();
}, 120);

tickClock();
setInterval(tickClock, 1000);
// 减少动态：不做出场动效，直接停在"已展开、可直接登录"的状态。
// 注意 state.reveal 也要一起改——只改 3D 场景的话，DOM 面板仍然是隐藏的。
if (state.reduce) state.reveal = 1;
state.shifted = state.reveal > 0.5;
mountScene();
fitCanvas();
render();
refreshAnchors();
