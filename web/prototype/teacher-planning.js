const TYPES = [
  { key: "single_choice", label: "单选", minutes: 2 },
  { key: "fill", label: "填空", minutes: 3 },
  { key: "judge", label: "判断", minutes: 1 },
  { key: "text", label: "作答", minutes: 6 },
];
const LEVELS = [
  { key: "light", label: "轻度", ratio: 25 },
  { key: "balanced", label: "平衡", ratio: 50 },
  { key: "heavy", label: "重度", ratio: 75 },
];
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const requestId = () => `teacher-${crypto.randomUUID()}`;

export function commonQuestionCount(count, level, students = 2) {
  if (!Number.isInteger(count) || count < 1 || students < 1) return 0;
  return students === 1 ? count : Math.ceil(count * (LEVELS.find((item) => item.key === level)?.ratio || 50) / 100);
}

export function goalAdvice(config, students) {
  const chosen = students.filter((student) => config.student_ids.includes(student.id));
  const total = TYPES.reduce((sum, type) => sum + (Number(config.question_types[type.key]) || 0), 0);
  const minutes = TYPES.reduce((sum, type) => sum + (Number(config.question_types[type.key]) || 0) * type.minutes, 0);
  const pending = chosen.filter((student) => !Number.isFinite(student.mastery));
  const basic = chosen.filter((student) => Number.isFinite(student.mastery) && student.mastery < 50);
  const common = commonQuestionCount(Number(config.question_count), config.common_level, chosen.length);
  const structure = TYPES.filter((type) => config.question_types[type.key] > 0).map((type) => `${type.label} ${config.question_types[type.key]} 道`).join("、");
  const messages = [];
  if (!chosen.length) messages.push("先勾选参加本次学习的学生，建议会随对象更新。");
  else {
    messages.push(`围绕「${config.title.trim() || "待填写的学习目标"}」，为 ${chosen.length} 位学生安排每人 ${config.question_count} 道题：${structure || "尚未配置题型"}。`);
    if (pending.length) messages.push(`${pending.map((student) => student.name).join("、")}暂无足够评分依据，先用本次作答了解情况。`);
    if (basic.length) messages.push(`${basic.map((student) => student.name).join("、")}最近已评分记录的得分比例较低，建议先复习目标对应的材料，再独立作答。`);
    if (!pending.length && !basic.length) messages.push("可先独立尝试，再根据逐题反馈安排复习。已有得分比例不等同于知识点掌握概率。");
    messages.push(chosen.length === 1 ? "当前仅选一人，全部题目进入这位学生的个人卷。" : `计划至少 ${common} 道共同题用于对照，其余题目从同题型、同分值题池分别选取。`);
  }
  if (total !== Number(config.question_count)) messages.push(`题型合计 ${total} 道，与每人题量 ${config.question_count} 道不一致，请调整。`);
  return { selectedCount: chosen.length, total, minutes, common, messages };
}

export function validateGoalConfig(config) {
  const errors = [];
  if (!config.title.trim()) errors.push("请填写学习目标。");
  if (!config.course_id || !config.class_id) errors.push("请先选择有班级的课程。");
  if (!config.student_ids.length) errors.push("请至少勾选一位有效学生。");
  if (!Number.isInteger(config.question_count) || config.question_count < 1 || config.question_count > 100) errors.push("每人题量应为 1–100 的整数。");
  const counts = TYPES.map((type) => config.question_types[type.key]);
  if (counts.some((count) => !Number.isInteger(count) || count < 0 || count > 100)) errors.push("各题型数量应为 0–100 的整数。");
  if (counts.reduce((sum, count) => sum + count, 0) !== config.question_count) errors.push("四类题型数量之和必须等于每人题量。");
  if (config.scheduled_at && config.deadline_at && config.scheduled_at >= config.deadline_at) errors.push("开放时间必须早于截止时间。");
  if (config.deadline_at && config.deadline_at <= Date.now() / 1000) errors.push("截止时间应晚于现在。");
  return errors;
}

function localTime(value) {
  if (!value) return "";
  const date = new Date(value * 1000);
  const pad = (number) => String(number).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function createTeacherPlanning({ api, state, render, toast, onPublished, onLibrary }) {
  let draft = null;
  let preview = null;
  let busy = "";
  let error = "";
  let notice = "";
  let membersOpen = false;
  let search = "";
  let knowledge = [];
  let savedDrafts = [];
  let contextKey = "";
  let revision = 0;
  let generation = 0;
  let commands = {};
  let opener = null;
  let focusNext = false;
  let contextError = "";

  const key = () => `${state.transitionId}:${state.courseId}:${state.classId}`;
  const students = () => (state.liveStudents || []).filter((student) => student.active !== false);
  const config = () => ({ ...draft, student_ids: [...draft.student_ids], question_types: { ...draft.question_types }, knowledge_points: [...draft.knowledge_points] });
  const invalidate = () => { preview = null; busy = ""; error = ""; notice = ""; revision += 1; commands = {}; };

  function initial(options = {}) {
    const deadline = new Date();
    deadline.setDate(deadline.getDate() + 1);
    deadline.setHours(20, 0, 0, 0);
    const types = options.question_types || { single_choice: 1, fill: 1, judge: 1, text: 0 };
    return {
      course_id: state.courseId, class_id: state.classId,
      title: options.title || state.liveGoals?.[0]?.title || "", description: "",
      student_ids: options.studentId ? students().filter((student) => student.id === options.studentId).map((student) => student.id) : students().map((student) => student.id),
      question_count: TYPES.reduce((sum, type) => sum + (types[type.key] || 0), 0),
      question_types: Object.fromEntries(TYPES.map((type) => [type.key, types[type.key] || 0])),
      common_level: "balanced", difficulty: "any", knowledge_points: options.knowledge_points || [],
      deadline_at: deadline.getTime() / 1000, scheduled_at: null,
      allow_late: false, allow_retries: true, allow_retry_after_deadline: false,
    };
  }

  function reset() {
    generation += 1;
    draft = null; preview = null; busy = ""; error = ""; notice = "";
    membersOpen = false; search = ""; knowledge = []; savedDrafts = [];
    contextKey = ""; commands = {}; contextError = "";
  }

  async function open(options = {}) {
    if (!state.live || !state.courseId || !state.classId) { toast("请先载入课程和班级。"); return; }
    opener = document.activeElement;
    if (contextKey !== key()) reset();
    contextKey = key();
    if (!draft || options.studentId || options.question_types || options.knowledge_points || options.fresh) {
      draft = initial(options);
      invalidate();
    }
    const validIds = new Set(students().map((student) => student.id));
    const selected = draft.student_ids.filter((id) => validIds.has(id));
    if (selected.length !== draft.student_ids.length) { draft.student_ids = selected; invalidate(); }
    state.drawer = true;
    focusNext = true;
    render();
    const expected = ++generation;
    const scope = contextKey;
    const results = await Promise.allSettled([
      api.call("GET", `/courses/${encodeURIComponent(state.courseId)}/knowledge-points`),
      api.call("GET", `/teaching-plans/drafts?class_id=${encodeURIComponent(state.classId)}`),
    ]);
    if (expected !== generation || scope !== key()) return;
    if (results[0].status === "fulfilled") knowledge = (results[0].value?.items || []).filter((item) => item.status === "confirmed");
    if (results[1].status === "fulfilled") savedDrafts = results[1].value?.items || [];
    contextError = results.some((result) => result.status === "rejected") ? "知识点或已存草稿暂未读全，可以继续填写，稍后重新打开重试。" : "";
    // Context loading must not replace an input the teacher is currently typing into.
    const context = document.querySelector("[data-goal-context]");
    if (context && state.drawer) { context.innerHTML = contextHtml(); bindContext(context); }
  }

  function close() {
    if (busy === "publish") return;
    state.drawer = false;
    render();
    if (opener?.isConnected) opener.focus({ preventScroll: true });
    else document.querySelector('[data-action="open-drawer"]')?.focus({ preventScroll: true });
  }

  function memberSummary() {
    const chosen = students().filter((student) => draft.student_ids.includes(student.id));
    if (!chosen.length) return "请选择学生";
    if (chosen.length === students().length) return `${state.className} · 全部 ${chosen.length} 人`;
    return `${chosen.slice(0, 2).map((student) => student.name).join("、")}${chosen.length > 2 ? "等" : ""} · ${chosen.length} 人`;
  }

  function memberHtml() {
    const available = students();
    return `<div class="goal-members-tools"><input type="search" data-goal-search placeholder="搜索学生姓名" aria-label="搜索学生姓名" value="${esc(search)}"><button type="button" class="linkish" data-goal-action="all">全选</button><button type="button" class="linkish" data-goal-action="none">清空</button></div>
      <div class="goal-member-list">${available.map((student) => `<label class="goal-member" data-goal-member-row="${esc(student.name.toLocaleLowerCase())}" ${search && !student.name.toLocaleLowerCase().includes(search.toLocaleLowerCase()) ? "hidden" : ""}><input type="checkbox" data-goal-member="${esc(student.id)}" ${draft.student_ids.includes(student.id) ? "checked" : ""}><span><strong>${esc(student.name)}</strong><small>${Number.isFinite(student.mastery) ? `最近已评分记录 ${student.mastery}%` : "待评估"}</small></span></label>`).join("") || '<p class="muted">当前没有已激活的在班学生。</p>'}</div>
      <p class="muted goal-member-empty" data-goal-no-match hidden>没有找到这个姓名。</p>`;
  }

  function contextHtml() {
    return `${contextError ? `<p class="muted">${esc(contextError)}</p>` : ""}
      ${savedDrafts.length ? `<label class="field goal-saved">继续已存草稿<select data-goal-restore aria-label="继续已存草稿"><option value="">当前填写内容</option>${savedDrafts.map((item) => `<option value="${esc(item.id)}">${esc(item.config?.title || "未命名目标")} · ${item.config?.student_ids?.length || 0} 人</option>`).join("")}</select></label>` : ""}
      ${knowledge.length ? `<fieldset class="goal-knowledge"><legend>知识点范围 <small>不勾选时使用课程内符合条件的题目</small></legend><div>${knowledge.map((item) => `<label><input type="checkbox" data-goal-knowledge="${esc(item.id)}" ${draft.knowledge_points.includes(item.id) ? "checked" : ""}>${esc(item.title || item.code)}</label>`).join("")}</div></fieldset>` : '<p class="muted">当前未选择知识点，按本课程题库备题；可在课程材料中维护知识点。</p>'}`;
  }

  function adviceHtml() {
    const advice = goalAdvice(draft, students());
    return `<div class="goal-advice-head"><h3>安排建议</h3><span>预计 ${advice.minutes} 分钟 / 人</span></div>${advice.messages.map((message) => `<p>${esc(message)}</p>`).join("")}<small>根据目标、所选学生的已评分记录和题型配置自动整理；时间按题型估算，可按实际情况调整。</small>`;
  }

  function readinessHtml() {
    if (error) return `<div class="state-banner bad" role="alert">${esc(error)}</div>`;
    if (notice) return `<div class="state-banner ok" role="status">${esc(notice)}</div>`;
    if (!preview) return '<p class="muted">填写完成后检查备题，查看每位学生的题卷和实际共同题数。</p>';
    const problems = [...(preview.errors || []), ...(preview.deficits || []).map((item) => ({ message: `${TYPES.find((type) => type.key === item.question_type)?.label || item.question_type}题：需要 ${item.required} 道，可用 ${item.available} 道${item.reason ? `。${item.reason}` : ""}` }))];
    return `<div class="state-banner ${preview.ready ? "ok" : "warn"}" role="status"><strong>${preview.ready ? "备题已齐，可以发布" : "还不能发布"}</strong>${problems.length ? `<ul>${problems.map((item) => `<li>${esc(typeof item === "string" ? item : item.message)}</li>`).join("")}</ul>` : ""}${preview.ready ? `<p>每人 ${preview.question_count ?? draft.question_count} 道 / ${preview.total_points ?? preview.scores?.total_score ?? "—"} 分；实际共同 ${preview.common_count} 道（${preview.actual_common_ratio}%）。</p>` : ""}</div>
      ${(preview.warnings || []).map((item) => `<p class="goal-warning">${esc(typeof item === "string" ? item : item.message)}</p>`).join("")}
      ${preview.ready ? `<details class="goal-papers"><summary>查看 ${preview.students?.length || 0} 位学生的题卷</summary>${(preview.students || []).map((student) => `<section><h4>${esc(student.display_name || student.student_id)} <small>${student.question_count ?? draft.question_count} 道 / ${student.total_score ?? preview.total_points ?? "—"} 分</small></h4>${student.suggestion?.message ? `<p class="muted">${esc(student.suggestion.message)}</p>` : ""}<ol>${(student.questions || []).map((question) => `<li><span class="tag">${esc(TYPES.find((type) => type.key === question.type)?.label || question.type)}</span> ${esc(question.stem)} <small>${question.points ?? "—"} 分</small></li>`).join("")}</ol></section>`).join("")}</details>` : '<button class="btn" type="button" data-goal-action="prepare">保存草稿并去题库备题</button>'}`;
  }

  function html() {
    if (!draft) return "";
    const advice = goalAdvice(draft, students());
    return `<div class="drawer-back" data-goal-action="close" aria-hidden="true"></div><aside class="drawer goal-drawer" role="dialog" aria-modal="true" aria-labelledby="drawer-title" tabindex="-1">
      <header class="goal-header"><div><h2 id="drawer-title">学习目标</h2><p>${esc(state.courseName)} / ${esc(state.className)}</p></div><button class="icon-btn" type="button" data-goal-action="close" aria-label="关闭学习目标" ${busy === "publish" ? "disabled" : ""}>关闭</button></header>
      <form data-goal-form><fieldset class="goal-fields" ${busy ? "disabled" : ""}>
      <label class="field">目标<input name="goal-title" data-goal-field="title" value="${esc(draft.title)}" maxlength="160" required placeholder="学生完成后应能做到什么"></label>
      <label class="field">补充要求 <span class="goal-optional">选填</span><textarea data-goal-field="description" rows="2" maxlength="10000" placeholder="范围、作答要求或给学生的说明">${esc(draft.description)}</textarea></label>
      <div class="goal-object"><span id="goal-object-label">对象</span><details class="goal-members" ${membersOpen ? "open" : ""}><summary aria-labelledby="goal-object-label goal-member-summary"><span id="goal-member-summary">${esc(memberSummary())}</span><span aria-hidden="true">⌄</span></summary><div class="goal-members-menu">${memberHtml()}</div></details><small>勾选参加本次学习的学生，未勾选的学生不会收到任务。</small></div>
      <div data-goal-context>${contextHtml()}</div>
      <div class="goal-config-row"><label class="field">每人题量<input type="number" data-goal-field="question_count" value="${draft.question_count}" min="1" max="100" step="1" required></label><label class="field">题目难度<select data-goal-field="difficulty"><option value="any" ${draft.difficulty === "any" ? "selected" : ""}>不限，按可用题库</option><option value="easy" ${draft.difficulty === "easy" ? "selected" : ""}>基础</option><option value="medium" ${draft.difficulty === "medium" ? "selected" : ""}>适中</option><option value="hard" ${draft.difficulty === "hard" ? "selected" : ""}>进阶</option></select></label></div>
      <fieldset class="goal-type-group"><legend>题型结构 <span data-goal-total>合计 ${advice.total} / ${draft.question_count} 道</span></legend><div class="goal-types">${TYPES.map((type) => `<label>${type.label}<span><input type="number" data-goal-type="${type.key}" value="${draft.question_types[type.key]}" min="0" max="100" step="1" aria-label="${type.label}题数量"><small>道</small></span></label>`).join("")}</div><button type="button" class="linkish" data-goal-action="balance-types">按每人题量均分四类题型</button></fieldset>
      <fieldset class="goal-level-group"><legend>共同题档位</legend><div class="goal-levels">${LEVELS.map((level) => `<label class="goal-level"><input type="radio" name="common-level" data-goal-level="${level.key}" value="${level.key}" ${draft.common_level === level.key ? "checked" : ""}><span><strong>${level.label}</strong><small>至少 ${level.ratio}% 共同题</small></span></label>`).join("")}</div><p data-goal-common class="muted">${advice.selectedCount === 1 ? "一位学生：全部题目进入个人卷。" : `当前计划至少 ${advice.common} 道共同题；不足一题向上取整。`}</p></fieldset>
      <section class="goal-advice" data-goal-advice aria-live="polite">${adviceHtml()}</section>
      <div class="goal-config-row"><label class="field">开放时间<input type="datetime-local" data-goal-field="scheduled_at" value="${localTime(draft.scheduled_at)}"><small>留空表示发布后立即开放</small></label><label class="field">截止时间<input type="datetime-local" data-goal-field="deadline_at" value="${localTime(draft.deadline_at)}"><small>留空表示不设截止时间</small></label></div>
      <div class="goal-policy"><label><input type="checkbox" data-goal-field="allow_late" ${draft.allow_late ? "checked" : ""}>截止后允许迟交，迟交会标明</label><label><input type="checkbox" data-goal-field="allow_retries" ${draft.allow_retries ? "checked" : ""}>允许重做，保留首次成绩</label><label><input type="checkbox" data-goal-field="allow_retry_after_deadline" ${draft.allow_retry_after_deadline ? "checked" : ""} ${!draft.allow_retries ? "disabled" : ""}>截止后仍允许重做</label></div>
      </fieldset><section data-goal-readiness>${readinessHtml()}</section>
      <footer class="goal-footer"><p>本次发布一轮任务。题目和评分规则在发布时固定。</p><div><button class="btn" type="button" data-goal-action="save" ${busy ? "disabled" : ""}>${busy === "save" ? "保存中…" : "保存草稿"}</button><button class="btn" type="button" data-goal-action="preview" ${busy ? "disabled" : ""}>${busy === "preview" ? "检查中…" : "检查备题"}</button><button class="btn primary" type="submit" data-goal-publish ${busy || !preview?.ready || !preview?.preview_token ? "disabled" : ""}>${busy === "publish" ? "发布中…" : draft.scheduled_at ? "确认定时发布" : "确认发布"}</button></div></footer>
      </form></aside>`;
  }

  function updateSummaries(root) {
    const advice = goalAdvice(draft, students());
    const updates = {
      "#goal-member-summary": esc(memberSummary()),
      "[data-goal-total]": `合计 ${advice.total} / ${draft.question_count} 道`,
      "[data-goal-common]": advice.selectedCount === 1 ? "一位学生：全部题目进入个人卷。" : `当前计划至少 ${advice.common} 道共同题；不足一题向上取整。`,
      "[data-goal-advice]": adviceHtml(),
      "[data-goal-readiness]": readinessHtml(),
    };
    for (const [selector, value] of Object.entries(updates)) {
      const element = root.querySelector(selector);
      if (element) element.innerHTML = value;
    }
    const button = root.querySelector("[data-goal-publish]");
    if (button) { button.disabled = true; button.textContent = draft.scheduled_at ? "确认定时发布" : "确认发布"; }
  }

  async function execute(action) {
    if (busy || !draft) return;
    const errors = validateGoalConfig(draft);
    if (errors.length) { error = errors.join(" "); render(); return; }
    const expected = revision;
    const scope = key();
    const snapshot = config();
    const token = preview?.preview_token;
    if (action === "publish" && (!preview?.ready || !token)) { error = "请先检查备题，确认每位学生的题卷。"; render(); return; }
    busy = action; error = ""; notice = "";
    const command = commands[action] ||= requestId();
    render();
    try {
      const response = action === "preview"
        ? await api.call("POST", "/teaching-plans/preview", snapshot)
        : await api.call("POST", `/teaching-plans/${action === "save" ? "drafts" : action}`, { config: snapshot, request_id: command, ...(action === "publish" ? { preview_token: token } : {}) });
      if (scope !== key() || expected !== revision) return;
      if (action === "preview") preview = response;
      if (action === "save") {
        notice = "草稿已保存，可从“继续已存草稿”恢复。";
        savedDrafts = [response, ...savedDrafts.filter((item) => item.id !== response.id)];
      }
      if (action === "prepare") {
        preview = response.preview || null;
        state.drawer = false;
        onLibrary({ config: snapshot, deficits: response.preview?.deficits || [] });
        toast(response.status === "ready" ? "题目已备齐，返回学习目标检查后发布。" : "草稿已保存，请从已确认的材料补齐题目。 ");
      }
      if (action === "publish") {
        const count = snapshot.student_ids.length;
        draft = null; preview = null; state.drawer = false;
        toast(response.status === "scheduled" ? `已为 ${count} 位学生安排定时任务。` : `已向 ${count} 位学生发布任务。`);
        await onPublished(response);
      }
    } catch (failure) {
      if (scope === key() && expected === revision) {
        error = failure?.message || "操作未完成，填写内容已保留，请重试。";
        if (["preview_stale", "preview_expired", "config_changed"].includes(failure?.code)) preview = null;
      }
    } finally {
      if (scope === key() && expected === revision) { busy = ""; render(); }
    }
  }

  function bindContext(root) {
    root.querySelectorAll("[data-goal-knowledge]").forEach((input) => input.addEventListener("change", () => {
      const selected = new Set(draft.knowledge_points);
      if (input.checked) selected.add(input.dataset.goalKnowledge); else selected.delete(input.dataset.goalKnowledge);
      draft.knowledge_points = [...selected]; invalidate(); updateSummaries(document);
    }));
    root.querySelector("[data-goal-restore]")?.addEventListener("change", (event) => {
      const selected = savedDrafts.find((item) => item.id === event.target.value);
      if (!selected?.config) return;
      draft = JSON.parse(JSON.stringify(selected.config));
      const valid = new Set(students().map((student) => student.id));
      draft.student_ids = draft.student_ids.filter((id) => valid.has(id));
      invalidate(); notice = "已恢复草稿；发布前会重新检查学生范围与可用题目。"; render();
    });
  }

  function bind(root) {
    if (!state.drawer || !draft) { document.body.classList.remove("goal-modal-open"); return; }
    document.body.classList.add("goal-modal-open");
    const panel = root.querySelector(".goal-drawer");
    if (!panel) return;
    if (focusNext) { panel.focus({ preventScroll: true }); focusNext = false; }
    const background = root.querySelector(".app");
    if (background) background.inert = true;
    panel.addEventListener("keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); close(); }
      if (event.key !== "Tab") return;
      const nodes = [...panel.querySelectorAll('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [tabindex="0"]')].filter((node) => node.getClientRects().length);
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && (document.activeElement === first || document.activeElement === panel)) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    root.querySelectorAll("[data-goal-action]").forEach((button) => button.addEventListener("click", () => {
      const action = button.dataset.goalAction;
      if (action === "close") { close(); return; }
      if (busy) return;
      if (action === "all" || action === "none") {
        draft.student_ids = action === "all" ? students().map((student) => student.id) : [];
        invalidate();
        panel.querySelectorAll("[data-goal-member]").forEach((input) => { input.checked = draft.student_ids.includes(input.dataset.goalMember); });
        updateSummaries(root);
      } else if (action === "balance-types") {
        const count = Number(draft.question_count);
        if (!Number.isInteger(count) || count < 1 || count > 100) { toast("请先填写 1–100 的每人题量。"); return; }
        TYPES.forEach((type, index) => { draft.question_types[type.key] = Math.floor(count / TYPES.length) + (index < count % TYPES.length ? 1 : 0); });
        invalidate(); render();
      } else execute(action);
    }));
    panel.querySelector("[data-goal-form]")?.addEventListener("submit", (event) => { event.preventDefault(); execute("publish"); });
    panel.querySelector(".goal-members")?.addEventListener("toggle", (event) => { membersOpen = event.target.open; });
    panel.querySelector("[data-goal-search]")?.addEventListener("input", (event) => {
      search = event.target.value;
      let matches = 0;
      panel.querySelectorAll("[data-goal-member-row]").forEach((row) => { row.hidden = !row.dataset.goalMemberRow.includes(search.toLocaleLowerCase()); if (!row.hidden) matches += 1; });
      panel.querySelector("[data-goal-no-match]").hidden = matches > 0;
    });
    panel.querySelectorAll("[data-goal-member]").forEach((input) => input.addEventListener("change", () => {
      const chosen = new Set(draft.student_ids);
      if (input.checked) chosen.add(input.dataset.goalMember); else chosen.delete(input.dataset.goalMember);
      draft.student_ids = [...chosen]; invalidate(); updateSummaries(root);
    }));
    panel.querySelectorAll("[data-goal-type]").forEach((input) => input.addEventListener("input", () => {
      draft.question_types[input.dataset.goalType] = input.value === "" ? 0 : Number(input.value);
      invalidate(); updateSummaries(root);
    }));
    panel.querySelectorAll("[data-goal-level]").forEach((input) => input.addEventListener("change", () => {
      draft.common_level = input.dataset.goalLevel; invalidate(); updateSummaries(root);
    }));
    panel.querySelectorAll("[data-goal-field]").forEach((input) => input.addEventListener(input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input", () => {
      const field = input.dataset.goalField;
      draft[field] = input.type === "checkbox" ? input.checked : field === "question_count" ? Number(input.value) : ["deadline_at", "scheduled_at"].includes(field) ? (input.value ? new Date(input.value).getTime() / 1000 : null) : input.value;
      if (field === "allow_retries") {
        if (!draft.allow_retries) draft.allow_retry_after_deadline = false;
        const after = panel.querySelector('[data-goal-field="allow_retry_after_deadline"]');
        after.disabled = !draft.allow_retries; after.checked = draft.allow_retry_after_deadline;
      }
      invalidate(); updateSummaries(root);
    }));
    bindContext(panel);
  }

  return { open, close, render: html, bind, reset };
}
