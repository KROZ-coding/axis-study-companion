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
};

let toastTimer = 0;
let glass = null;

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
}

function renderEntry() {
  view.innerHTML = `
    <section class="entry ${state.shifted ? "shifted" : ""}">
      <div class="entry-copy">
        <p class="mark">智慧学伴</p>
        <h1>把下一道题，放在看得懂的位置。</h1>
        <p class="lede">同一学习目标下，先看每人已有的依据，再决定讲解、练习和提示。这里是可点击原型，内容为虚构样例。</p>
        ${state.shifted ? "" : '<button class="enter" data-action="shift">进入</button>'}
        <div><span class="sample-flag">虚构样例 · 不是实时模型</span></div>
      </div>
      <form class="auth-card" data-action="auth" hidden>
        <h2>用账号进入</h2>
        <p class="muted">师生同一入口。身份由账号决定，不能在这里自行选择身份。</p>
        <div class="tabs" role="tablist">
          ${tab("login", "登录")}${tab("apply", "教师申请")}${tab("activate", "激活 / 重置")}
        </div>
        ${authFields()}
        <p class="error-line" role="alert">${escapeHtml(state.error)}</p>
        <div class="row">
          <button class="btn primary" type="submit">${state.authTab === "apply" ? "提交申请" : state.authTab === "activate" ? "设置密码" : "登录"}</button>
          <button class="btn" type="button" data-action="back-entry">返回</button>
        </div>
      </form>
    </section>`;
  const form = view.querySelector(".auth-card");
  if (state.shifted) form.hidden = false;
}

function tab(id, label) {
  return `<button class="chip" type="button" role="tab" aria-pressed="${state.authTab === id}" data-tab="${id}">${label}</button>`;
}

function authFields() {
  if (state.authTab === "apply") {
    return `<label class="field">姓名<input name="name" required value="陈宁"></label>
      <label class="field">学校邮箱<input name="email" type="email" required value="chen@school.example"></label>
      <label class="field">任教课程<input name="course" required value="七年级数学"></label>`;
  }
  if (state.authTab === "activate") {
    return `<label class="field">账号<input name="account" required value="linxia"></label>
      <label class="field">一次性码<input name="code" required placeholder="6 位码"></label>
      <label class="field">新密码<input name="password" type="password" required minlength="8"></label>`;
  }
  return `<label class="field">账号<input name="account" required autocomplete="username" placeholder="teacher 或 linxia"></label>
    <label class="field">密码<input name="password" type="password" required autocomplete="current-password"></label>
    <p class="muted">原型账号：teacher / linxia。密码随意；输入 expired 看过期码，disabled 看停用。</p>`;
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
  view.querySelectorAll("[data-action]").forEach((node) => node.addEventListener("click", onAction));
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
}

function onAction(event) {
  const action = event.currentTarget.dataset.action;
  if (action === "shift") {
    state.shifted = true;
    render();
    return;
  }
  if (action === "back-entry") {
    state.shifted = false;
    state.error = "";
    render();
    return;
  }
  if (action === "logout") {
    Object.assign(state, { screen: "entry", shifted: false, role: null, drawer: false, sheet: null, error: "" });
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

function submitAuth(data) {
  const account = String(data.get("account") || data.get("name") || "");
  const secret = String(data.get("password") || data.get("code") || "");
  if (state.authTab === "apply") {
    state.error = "申请已提交，审核通过前不能进入工作台。";
    render();
    return;
  }
  if (secret === "expired") {
    state.error = "这个一次性码已过期，请向老师再要一个。";
    render();
    return;
  }
  if (secret === "disabled" || account === "disabled") {
    state.error = "账号已停用，会话也已失效。";
    render();
    return;
  }
  state.role = account.startsWith("lin") ? "student" : "teacher";
  state.who = state.role === "student" ? "林夏" : "陈宁";
  state.screen = "app";
  state.error = "";
  state.banner = "";
  toast(state.role === "student" ? "进入林夏的学习" : "进入七（1）班");
  render();
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

function mountGlass() {
  if (glass) glass.stop();
  const canvas = document.querySelector("#glass");
  if (!canvas) return;
  glass = startGlass(canvas, state.reduce);
}

function startGlass(canvas, reduce) {
  const gl = canvas.getContext("webgl", { alpha: true, premultipliedAlpha: false });
  if (!gl) {
    canvas.insertAdjacentHTML("afterend", fallbackSvg());
    return { stop() {} };
  }
  const vertex = `attribute vec2 p; void main(){ gl_Position=vec4(p,0.0,1.0); }`;
  const fragment = `precision mediump float; uniform vec2 uRes,uPtr; uniform float uTime,uShift;
    float prism(vec2 uv){ vec2 q=abs(uv); return max(q.x*0.85+uv.y*0.45, -uv.y)-0.34; }
    void main(){ vec2 uv=(gl_FragCoord.xy-0.5*uRes)/uRes.y; uv.x+=uShift*0.55; uv+= (uPtr-0.5)*0.08;
      float a=prism(uv*vec2(1.15,1.0)+vec2(0.18,0.02)); float b=prism(uv*vec2(0.8,1.25)-vec2(0.32,0.12));
      float g=smoothstep(0.02,0.0,a)+0.55*smoothstep(0.025,0.0,b);
      vec3 ice=mix(vec3(0.91,0.97,0.99), vec3(0.45,0.76,0.86), g);
      float spec=smoothstep(0.2,0.0,abs(a+0.01+0.015*sin(uTime)));
      ice+=spec*vec3(0.35,0.45,0.48); gl_FragColor=vec4(ice,0.55+0.3*g); }`;
  const program = link(gl, vertex, fragment);
  if (!program) return { stop() {} };
  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(program, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  gl.useProgram(program);
  const uniforms = {
    res: gl.getUniformLocation(program, "uRes"),
    ptr: gl.getUniformLocation(program, "uPtr"),
    time: gl.getUniformLocation(program, "uTime"),
    shift: gl.getUniformLocation(program, "uShift"),
  };
  let frame = 0;
  let running = true;
  const draw = (now) => {
    if (!running || !canvas.isConnected) return;
    const rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * devicePixelRatio;
    canvas.height = rect.height * devicePixelRatio;
    gl.viewport(0, 0, canvas.width, canvas.height);
    gl.uniform2f(uniforms.res, canvas.width, canvas.height);
    gl.uniform2f(uniforms.ptr, state.pointer.x, 1 - state.pointer.y);
    gl.uniform1f(uniforms.time, reduce ? 0 : now / 1000);
    gl.uniform1f(uniforms.shift, state.shifted ? 1 : 0);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
    if (!reduce) frame = requestAnimationFrame(draw);
  };
  frame = requestAnimationFrame(draw);
  return { stop() { running = false; cancelAnimationFrame(frame); } };
}

function link(gl, vertexSource, fragmentSource) {
  const program = gl.createProgram();
  for (const [type, source] of [[gl.VERTEX_SHADER, vertexSource], [gl.FRAGMENT_SHADER, fragmentSource]]) {
    const shader = gl.createShader(type);
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) return null;
    gl.attachShader(program, shader);
  }
  gl.linkProgram(program);
  return gl.getProgramParameter(program, gl.LINK_STATUS) ? program : null;
}

function fallbackSvg() {
  return `<svg class="fallback" viewBox="0 0 800 600" aria-hidden="true"><polygon points="470,90 650,250 430,430" fill="rgba(255,255,255,.45)" stroke="rgba(31,138,168,.45)"/><polygon points="360,150 560,210 340,390" fill="rgba(170,220,234,.35)"/></svg>`;
}

document.addEventListener("pointermove", (event) => {
  state.pointer = { x: event.clientX / innerWidth, y: event.clientY / innerHeight };
});
reduceQuery.addEventListener("change", () => {
  state.reduce = reduceQuery.matches;
  if (glass) glass.stop();
  glass = startGlass(document.querySelector("#glass"), state.reduce);
});
glass = startGlass(document.querySelector("#glass"), state.reduce);
render();
