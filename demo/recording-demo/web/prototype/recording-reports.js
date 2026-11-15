// 演示实例的成果页复用正式报告快照和下载接口。
const TITLES = { class: "班级阶段学情", student: "学生个人学习", goal: "学习目标执行", question_bank: "题库质量与使用" };
const FORMATS = { html: "网页", pdf: "PDF", xlsx: "Excel", json: "JSON", csv: "CSV" };
const METRICS = { students: "学生", goals: "学习目标", question_versions: "题目版本", evidence: "作答证据", mastery_rows: "评估条目", insufficient_evidence_rows: "待评估条目", usage_rows: "使用记录" };
const LABELS = { independent: "独立作答", hinted: "使用提示", answer_exposed: "已见解析", redo: "重做", unknown: "未知", sufficient: "证据充分", insufficient: "证据不足", insufficient_evidence: "待评估", graded: "已评分", approved: "已审查", draft: "草稿", quarantined: "待复核" };
const TABLES = { mastery: "学习判断与帮助", evidence: "逐题作答证据", question_usage: "题目使用情况", questions: "题目版本", sources: "材料出处", rules: "规则与判断边界" };
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const rows = (snapshot, key) => snapshot?.tables?.[key]?.rows || [];
const date = (value) => value ? new Date(Number(value) * 1000).toLocaleString("zh-CN") : "—";

export function createRecordingReports({ api, getContext, onChange = () => {} }) {
  const state = { identity: "", loaded: false, loading: false, generating: false, error: "", kind: "goal", student: "", goal: "", members: [], goals: [], reports: [], revision: null, formats: Object.keys(FORMATS), generation: 0 };
  const context = () => getContext?.() || {};
  const changed = () => { if (document.querySelector("[data-recording-reports]")) onChange(); };
  function reset() {
    state.generation += 1;
    Object.assign(state, { loaded: false, loading: false, generating: false, error: "", members: [], goals: [], reports: [], revision: null, student: "", goal: "" });
  }
  function sync() {
    const { status = {}, user = {} } = context();
    const key = [user?.id, status.course_id, status.class_id, status.goal_id].join(":");
    if (state.identity !== key) { reset(); state.identity = key; state.goal = status.goal_id || ""; }
    return { status, user };
  }
  async function load() {
    const { status, user } = sync();
    if (!status?.enabled || user?.role !== "teacher" || state.loading || state.loaded) return;
    const generation = state.generation;
    state.loading = true;
    try {
      const [members, goals, reports, rules] = await Promise.all([
        api.classMembers(status.class_id), api.goals(status.class_id), api.call("GET", "/reports"), api.reportFormats(),
      ]);
      if (generation !== state.generation) return;
      state.members = members;
      state.goals = goals;
      state.reports = reports.items || [];
      state.formats = (rules.export_formats || Object.keys(FORMATS)).filter((format) => FORMATS[format]);
      state.student = members[0]?.user_id || "";
      state.goal = goals.some((goal) => goal.id === state.goal) ? state.goal : goals[0]?.id || "";
      state.loaded = true;
      state.error = "";
    } catch (error) { if (generation === state.generation) state.error = error.message || "报告范围加载失败，请重试。"; }
    finally { if (generation === state.generation) { state.loading = false; changed(); } }
  }
  function scope() {
    const { status } = context();
    const value = { kind: state.kind, course_id: status.course_id, class_id: status.class_id };
    if (state.kind === "goal") { if (!state.goal) throw new Error("请先发布学习目标。"); value.goal_id = state.goal; }
    if (state.kind === "student") { if (!state.student) throw new Error("请先选择学生。"); value.student_id = state.student; }
    if (state.kind === "question_bank") value.question_bank_id = status.question_bank_id || status.course_id;
    return value;
  }
  async function generate() {
    if (state.generating) return;
    const generation = state.generation;
    state.generating = true; state.error = ""; changed();
    try {
      const revision = await api.generateReport(scope(), `demo-report-${crypto.randomUUID()}`);
      if (generation !== state.generation) return;
      const reports = await api.call("GET", "/reports");
      if (generation !== state.generation) return;
      state.revision = revision;
      state.reports = reports.items || [];
    } catch (error) { if (generation === state.generation) state.error = error.message || "报告生成失败，请重试。"; }
    finally { if (generation === state.generation) { state.generating = false; changed(); } }
  }
  async function open(reportId) {
    const generation = state.generation;
    state.error = "";
    try {
      const revision = await api.call("GET", `/reports/${encodeURIComponent(reportId)}`);
      if (generation === state.generation) { state.revision = revision; changed(); }
    } catch (error) { if (generation === state.generation) { state.error = error.message; changed(); } }
  }
  function value(key, input, snapshot) {
    if (input === null || input === undefined) return "待评估";
    if (key === "student_id") return rows(snapshot, "students").find((student) => student.student_id === input)?.name || state.members.find((member) => member.user_id === input)?.display_name || input;
    if (key === "goal_id") return rows(snapshot, "goals").find((goal) => goal.goal_id === input)?.title || input;
    if (key === "observed_at") return date(input);
    if (key === "score_ratio") return `${Math.round(Number(input) * 100)}%`;
    if (key === "percent") return `${input}%`;
    if (typeof input === "boolean") return input ? "是" : "否";
    if (Array.isArray(input)) return input.map((item) => typeof item === "object" ? JSON.stringify(item) : item).join("、") || "—";
    if (typeof input === "object") return JSON.stringify(input);
    return LABELS[input] || input;
  }
  function table(snapshot, name) {
    const table = snapshot.tables?.[name];
    if (!table) return "";
    const preferred = { mastery: ["student_id", "goal_id", "percent", "status", "independent_observations", "hinted_observations"], evidence: ["student_id", "question_id", "attempt_number", "exposure", "score_ratio", "status"], questions: ["question_id", "version", "status"], question_usage: ["question_id", "usage_count", "independent_count", "assisted_count"], sources: ["title", "locator"] };
    const columns = table.columns.filter((column) => !preferred[name] || preferred[name].includes(column.key));
    return `<details class="recording-report-table" ${["mastery", "question_usage"].includes(name) ? "open" : ""}><summary>${escape(TABLES[name])}<span>${table.rows.length} 条</span></summary>${table.rows.length ? `<div class="recording-report-scroll"><table><thead><tr>${columns.map((column) => `<th scope="col">${escape(column.label)}</th>`).join("")}</tr></thead><tbody>${table.rows.map((row) => `<tr>${columns.map((column) => `<td>${escape(value(column.key, row[column.key], snapshot))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : '<p class="muted">尚无此类记录；未完成的任务不会计作零分。</p>'}</details>`;
  }
  function result() {
    const revision = state.revision;
    if (!revision) return `<section class="panel recording-report-empty"><h2>把这次学习变成可核对的成果</h2><p>选择报告范围，点击“生成报告”。学生提交、提示使用和评分记录会汇入同一份结果；尚无依据时保留“待评估”。</p></section>`;
    const snapshot = revision.snapshot;
    return `<article class="panel recording-report-result"><div class="recording-report-heading"><div><h2>${escape(snapshot.title)}</h2><p class="muted">修订 ${escape(revision.revision_number)} · 生成于 ${escape(date(revision.created_at))}</p></div><span class="tag">依据实际保存记录生成</span></div>
      <p class="recording-report-disclosure">本报告使用虚构演示课程和演示账号的实际操作记录。演示辅导与规则模拟评分不代表真实模型调用，也不证明长期学习效果。</p>
      <dl class="recording-report-metrics">${Object.entries(snapshot.summary || {}).map(([key, count]) => `<div><dt>${escape(METRICS[key] || key)}</dt><dd>${escape(count)}</dd></div>`).join("")}</dl>
      <div class="recording-report-downloads" aria-label="下载当前报告">${state.formats.map((format) => `<a class="btn" href="${escape(api.exportUrl(revision.report_id, format))}?revision_id=${encodeURIComponent(revision.id)}" download>${FORMATS[format]}</a>`).join("")}</div>
      <p class="muted">五种输出来自同一份报告快照。继续作答后请重新生成，旧文件保留原记录。</p>
      ${["mastery", "evidence", "question_usage", "questions", "sources", "rules"].map((name) => table(snapshot, name)).join("")}
    </article>`;
  }
  function render() {
    sync();
    return `<div data-recording-reports><h1 class="page">阶段成果</h1><p class="muted">从学习过程回到证据：比较独立作答、提示使用与本轮结果。</p>
      <section class="panel recording-report-controls"><div class="recording-report-kinds" role="group" aria-label="报告类型">${Object.entries(TITLES).map(([kind, title]) => `<button class="btn ${state.kind === kind ? "primary" : ""}" data-report-kind="${kind}" aria-pressed="${state.kind === kind}">${title}</button>`).join("")}</div>
      <div class="recording-report-selection">${state.kind === "student" ? `<label class="field">学生<select data-report-student>${state.members.map((member) => `<option value="${escape(member.user_id)}" ${member.user_id === state.student ? "selected" : ""}>${escape(member.display_name || member.username)}</option>`).join("")}</select></label>` : ""}${state.kind === "goal" ? `<label class="field">学习目标<select data-report-goal>${state.goals.map((goal) => `<option value="${escape(goal.id)}" ${goal.id === state.goal ? "selected" : ""}>${escape(goal.title)}</option>`).join("")}</select></label>` : ""}<button class="btn primary" data-report-generate ${!state.loaded || state.generating ? "disabled" : ""}>${state.generating ? "正在生成…" : "生成报告"}</button><button class="btn" data-report-refresh ${state.loading ? "disabled" : ""}>${state.loading ? "正在加载…" : "刷新范围"}</button></div>
      ${state.error ? `<p role="alert" class="recording-report-error">${escape(state.error)}</p>` : ""}</section>
      ${result()}
      ${state.reports.length ? `<details class="panel recording-report-history"><summary>已保存报告（${state.reports.length}）</summary><div>${state.reports.map((report) => `<button class="btn" data-report-open="${escape(report.id)}">${escape(TITLES[report.scope?.kind] || "报告")} · ${escape(date(report.updated_at))}</button>`).join("")}</div></details>` : ""}</div>`;
  }
  function bind() {
    const root = document.querySelector("[data-recording-reports]");
    if (!root) return;
    root.querySelectorAll("[data-report-kind]").forEach((button) => { button.onclick = () => { state.kind = button.dataset.reportKind; state.revision = null; changed(); }; });
    const student = root.querySelector("[data-report-student]");
    if (student) student.onchange = () => { state.student = student.value; state.revision = null; changed(); };
    const goal = root.querySelector("[data-report-goal]");
    if (goal) goal.onchange = () => { state.goal = goal.value; state.revision = null; changed(); };
    root.querySelector("[data-report-generate]").onclick = generate;
    root.querySelector("[data-report-refresh]").onclick = () => { state.loaded = false; load(); };
    root.querySelectorAll("[data-report-open]").forEach((button) => { button.onclick = () => open(button.dataset.reportOpen); });
    if (!state.loaded && !state.loading && !state.error) load();
  }
  return { render, bind, reset };
}
