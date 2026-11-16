/* Teacher review integration.
 * Requires only api.call(method, path, body, { signal }?). All paths are /api/v1 relative.
 * render() returns one [data-teacher-review] section; bind(container) finds that section.
 * load({force:false}?) and openSelection({goalId,taskId,attemptId,studentId,threadId,messageId}?)
 * resolve to boolean. A messageId requires its threadId and a rendered, visible message.
 * Empty/partial selections are supported; explicit IDs must match
 * server records. The host selects the class before calling either method.
 * onSelectionChange receives the four shared IDs once per successful changed selection.
 * onReports is a button action only; onChanged is a notification, never a host reload.
 * leave() blocks pending/unknown writes. Otherwise it clears private records and drafts,
 * retaining only the four selection IDs for this context. reset() clears those IDs too.
 * loggingOut suspends interaction/reads, but does not discard an in-flight command.
 * A failed logout can resume it; only reset/context changes invalidate its receipt.
 */
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const encode = (value) => encodeURIComponent(value);
const identifier = (value) => typeof value === "string" && value.length > 0;
const positiveInteger = (value) => Number.isSafeInteger(value) && value > 0;
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const emptySelection = () => ({ goalId: "", taskId: "", attemptId: "", studentId: "" });
const cleanSelection = (value = {}) => Object.fromEntries(Object.keys(emptySelection()).map((key) => [key, identifier(value?.[key]) ? value[key] : ""]));
const equalSelection = (left, right) => Object.keys(emptySelection()).every((key) => (left?.[key] || "") === (right?.[key] || ""));
const rows = (value) => {
  const items = Array.isArray(value) ? value : value?.items;
  if (!Array.isArray(items)) throw new Error("服务未返回完整列表，请重新读取。");
  return items;
};
const unknownOutcome = (error) => !Number.isFinite(Number(error?.status)) || !Number(error?.status)
  || Number(error.status) >= 500 || [408, 425].includes(Number(error.status));
const newId = () => `review-${globalThis.crypto.randomUUID()}`;
const statusLabels = { active: "进行中", draft: "草稿", closed: "已结束", not_started: "未开始", in_progress: "作答中",
  submitted: "等待评分", grading: "正在评分", awaiting_manual: "待教师评分", review_required: "待教师评分",
  results_ready: "评分完成", graded: "已评分", pending: "等待处理", manual_pending: "待人工评分",
  awaiting_teacher: "待教师处理", resolved: "已结案" };
const statusText = (value) => statusLabels[value] || value || "状态未提供";
const scoreText = (value) => finite(value) ? String(value) : "待评分";
const dateText = (value) => finite(value) ? new Date(value * 1000).toLocaleString("zh-CN", {
  year: "numeric", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit",
}) : "未提供";
const attemptText = (attempt) => `${({ first: "首次作答", retry: "重做", practice_retry: "练习重做" })[attempt?.kind] || "提交"} · 第 ${attempt?.attempt_no ?? "—"} 次`;
const plainText = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
const STALE = Symbol("stale review request");

function answerText(value, question = {}, reference = false) {
  if (value === null || value === undefined || value === "") return reference ? "参考答案未提供" : "未作答";
  if (typeof value === "boolean") return value ? "正确" : "错误";
  const choices = question.options || [];
  const choice = (answer) => {
    const index = typeof answer === "number" ? answer : choices.findIndex((option) => typeof option === "object" && (option.id === answer || option.value === answer));
    const option = choices[index];
    return option === undefined ? plainText(answer) : `${String.fromCharCode(65 + index)}. ${plainText(typeof option === "object" ? option.text ?? option.label ?? option.value : option)}`;
  };
  if (["single_choice", "multi_choice"].includes(question.type)) return (Array.isArray(value) ? value : [value]).map(choice).join("；");
  if (Array.isArray(value)) {
    const count = question.blank_count ?? question.answer_format?.blank_count;
    return Number.isSafeInteger(count) && count > 1 ? value.map((answer, index) => `第 ${index + 1} 空：${plainText(answer)}`).join("；")
      : value.map(plainText).join(reference && question.type === "fill" ? " 或 " : "；");
  }
  return plainText(value);
}
function scoreUnits(raw) {
  const text = String(raw ?? "").trim();
  if (!/^\d+(?:\.\d{1,2})?$/.test(text)) return null;
  const [whole, fraction = ""] = text.split(".");
  const units = Number(whole) * 100 + Number(fraction.padEnd(2, "0"));
  return Number.isSafeInteger(units) ? units : null;
}
const itemScore = (item) => Number.isSafeInteger(item?.score_units) ? item.score_units / 100 : finite(item?.score) ? item.score : null;
const itemMaximum = (item) => Number.isSafeInteger(item?.question?.points_units) ? item.question.points_units
  : finite(item?.question?.points) ? Math.round(item.question.points * 100) : null;
const pageState = () => ({ items: [], offset: 0, more: true, loaded: false });
function fresh(selection = emptySelection()) {
  return { selection: cleanSelection(selection), goals: pageState(), taskPages: new Map(), task: null, review: null, history: [],
    threads: [], threadDetails: new Map(), appeals: [], appealDetails: new Map(), presets: [], classroom: null,
    drafts: new Map(), commentDrafts: new Map(), managementDrafts: new Map(), appealDrafts: new Map(),
    reading: false, loaded: false, suspended: false, error: "", notice: "", command: null,
    studentFilter: "", needsRefresh: false, logoutObserved: false, revoked: false };
}

export function createTeacherReview({ api, state, render: renderApp = () => {}, toast = () => {}, onChanged, onSelectionChange, onReports }) {
  let current = fresh(state.reviewSelection), root = null, epoch = 0, readSequence = 0, loadingPromise = null;
  const controllers = new Set(), listeners = new WeakSet(), bindings = new WeakMap();
  const user = () => api?.state?.user;
  const contextKey = () => JSON.stringify([state.live, state.role, state.courseId || "", state.classId || "", state.transitionId ?? 0,
    user()?.id || "", user()?.role || "", user()?.status || "", state.session?.id || state.sessionId || ""]);
  let context = contextKey();
  const teacherSession = () => state.live === true && state.role === "teacher" && user()?.role === "teacher"
    && user()?.status === "active" && identifier(user()?.id) && (!state.screen || state.screen === "app");
  const active = () => teacherSession() && !state.loggingOut && !current.suspended && !current.revoked;
  const scopeReady = () => identifier(state.classId) && identifier(state.courseId);
  const valid = (token, data) => epoch === token && current === data && context === contextKey() && teacherSession();
  const stopReads = () => { readSequence += 1; controllers.forEach((controller) => controller.abort()); controllers.clear(); loadingPromise = null; current.reading = false; };
  function syncContext() {
    if (context !== contextKey()) {
      stopReads(); epoch += 1; context = contextKey(); current = fresh();
      state.reviewSelection = emptySelection();
      if (root?.isConnected) root.innerHTML = "";
    }
    if (state.loggingOut && !current.logoutObserved) {
      current.logoutObserved = true; current.needsRefresh = true; stopReads();
    } else if (!state.loggingOut) current.logoutObserved = false;
  }
  function reset() {
    stopReads(); epoch += 1; context = contextKey(); current = fresh(); state.reviewSelection = emptySelection();
    if (root?.isConnected) root.innerHTML = "";
    root = null;
  }
  function leave() {
    syncContext();
    if (state.loggingOut) return true;
    if (current.command) {
      const message = current.command.inFlight ? "操作正在提交，请等待结果后再离开复核。"
        : "上次操作结果尚未确认，请使用原请求显式重试后再离开。输入已保留。";
      current.error = message; paint(); toast(message); return false;
    }
    const selection = current.selection, hadDraft = [...current.drafts.values(), ...current.commentDrafts.values(), ...current.managementDrafts.values(), ...current.appealDrafts.values()]
      .some((draft) => draft.dirty);
    stopReads(); epoch += 1; current = fresh(selection); current.suspended = true;
    if (root?.isConnected) root.innerHTML = "";
    root = null;
    if (hadDraft) toast("已清除未提交的评分、短评和管理输入；保留当前历史选择。");
    return true;
  }
  const hostClass = () => (state.teacherCourses || []).flatMap((entry) => entry.classes || []).find((item) => item.id === state.classId);
  const manage = () => active() && current.classroom?.can_manage === true && hostClass()?.can_manage !== false;
  const writeLocked = () => Boolean(current.command || current.reading || current.needsRefresh);
  const goal = () => current.goals.items.find((item) => item.id === current.selection.goalId);
  const tasks = () => current.taskPages.get(current.selection.goalId)?.items || [];
  const studentName = (id, task = null) => task?.student_name || (state.liveStudents || []).find((item) => item.id === id || item.user_id === id)?.name
    || (state.liveStudents || []).find((item) => item.id === id || item.user_id === id)?.display_name || id || "未提供学生";
  const itemById = (id) => current.review?.items.find((item) => item.question_id === id);
  const draftKey = (item) => `${current.selection.attemptId}:${item.question_id}`;
  const mayGrade = (item) => manage() && current.review?.can_grade === true && (current.review.attempt.status === "review_required"
    ? item.status === "manual_pending" && item.can_manual_grade === true
    : current.review.attempt.status === "graded" && item.can_correct === true);
  const manual = () => current.review?.attempt.status === "review_required";
  function notifySelection(selection) {
    const next = cleanSelection(selection), changed = !equalSelection(state.reviewSelection, next);
    current.selection = next; state.reviewSelection = { ...next };
    if (changed && active() && typeof onSelectionChange === "function") {
      try { Promise.resolve(onSelectionChange({ ...next })).catch(() => {}); } catch { /* A host hook cannot invalidate a selected record. */ }
    }
  }
  const need = (condition, message) => { if (!condition) throw new Error(message); };
  function validateScope(record) {
    need(record && typeof record === "object", "服务返回的记录不完整。");
    if (record.class_id != null) need(record.class_id === state.classId, "记录不属于当前班级，已停止显示。");
    if (record.course_id != null) need(record.course_id === state.courseId, "记录不属于当前课程，已停止显示。");
  }
  function validateReview(review, selection = {}) {
    validateScope(review);
    need(review.class_id === state.classId && review.course_id === state.courseId && identifier(review.task_id)
      && identifier(review.goal_id) && identifier(review.student_id) && identifier(review.attempt?.id)
      && positiveInteger(review.attempt.revision) && Array.isArray(review.items) && typeof review.can_grade === "boolean",
    "教师复核记录缺少可核验的提交绑定或权限。");
    const actual = { goalId: review.goal_id, taskId: review.task_id, attemptId: review.attempt.id, studentId: review.student_id };
    for (const key of Object.keys(actual)) if (selection[key]) need(selection[key] === actual[key], "教师复核记录与所选提交不一致。");
    const unique = new Set();
    for (const item of review.items) {
      need(identifier(item?.question_id) && identifier(item.question_version_id) && item.question && typeof item.question === "object"
        && !unique.has(item.question_id), "逐题复核记录缺少冻结题目或含重复题目。");
      unique.add(item.question_id);
    }
    return review;
  }
  function validateThread(thread, selection, item = null, revision = null) {
    validateScope(thread);
    need(identifier(thread.id) && thread.task_id === selection.taskId && thread.attempt_id === selection.attemptId
      && (!thread.student_id || thread.student_id === selection.studentId) && identifier(thread.question_version_id)
      && positiveInteger(thread.grading_revision), "短评线程与当前提交绑定不一致。");
    if (item) need(thread.question_version_id === item.question_version_id, "短评线程不属于当前冻结题目。");
    if (revision != null) need(thread.grading_revision === revision, "短评线程的评分修订不一致。");
    return thread;
  }
  function validateAppeal(appeal, taskId = current.selection.taskId) {
    validateScope(appeal);
    need(identifier(appeal.id) && appeal.task_id === taskId && identifier(appeal.attempt_id)
      && identifier(appeal.question_id) && positiveInteger(appeal.current_revision) && typeof appeal.can_resolve === "boolean",
    "异议详情缺少可核验的提交绑定或当前修订。");
    return appeal;
  }
  function beginRead() {
    stopReads(); current.reading = true;
    const token = epoch, data = current, sequence = readSequence;
    return { token, data, sequence, live: () => valid(token, data) && sequence === readSequence && !state.loggingOut && !data.suspended };
  }
  async function get(path, read) {
    if (!read.live()) throw STALE;
    const controller = typeof AbortController === "function" ? new AbortController() : null;
    if (controller) controllers.add(controller);
    try {
      const result = await api.call("GET", path, undefined, controller ? { signal: controller.signal } : {});
      if (!read.live()) throw STALE;
      return result;
    } finally { if (controller) controllers.delete(controller); }
  }
  function revokeAccess() {
    current.revoked = true; current.needsRefresh = true; current.classroom = null; current.review = null; current.task = null;
    current.history = []; current.threads = []; current.threadDetails.clear(); current.appeals = []; current.appealDetails.clear();
    current.goals = pageState(); current.taskPages.clear(); current.drafts.clear(); current.commentDrafts.clear(); current.managementDrafts.clear(); current.appealDrafts.clear();
    // Keep any command ledger until reset, but never expose its payload after revocation.
    current.error = "复核记录不存在或已不可访问，请重新核验账号和班级权限。";
  }
  function handleReadError(error, read) {
    if (!read.live() || error === STALE || error?.name === "AbortError") return;
    current.error = error?.message || "记录读取失败，请重试。";
    // Learning routes deliberately mask revoked access as a missing resource.
    if ([401, 403, 404].includes(Number(error?.status))) revokeAccess();
  }
  async function getPage(page, path, read, { goalId = "" } = {}) {
    const offset = page.offset;
    const result = await get(`${path}&limit=100&offset=${offset}`, read);
    const incoming = rows(result);
    const seen = new Set(page.items.map((item) => item.id));
    for (const item of incoming) {
      validateScope(item);
      need(identifier(item.id) && (!goalId || item.goal_id === goalId), "分页记录与所选目标不一致。");
    }
    const added = incoming.filter((item) => !seen.has(item.id));
    need(!incoming.length || added.length > 0, "分页没有前进，请刷新核对；已停止重复读取。");
    page.items.push(...added); page.offset += incoming.length; page.loaded = true;
    page.more = Number.isSafeInteger(result?.total) ? page.offset < result.total : incoming.length === 100;
    if (!incoming.length) page.more = false;
    return page;
  }
  const goalsPath = () => `/goals?class_id=${encode(state.classId)}`;
  const tasksPath = (goalId) => `/tasks?class_id=${encode(state.classId)}&goal_id=${encode(goalId)}`;
  async function ensureGoal(goalId, read) {
    if (!current.goals.loaded) await getPage(current.goals, goalsPath(), read);
    while (goalId && !current.goals.items.some((item) => item.id === goalId) && current.goals.more) await getPage(current.goals, goalsPath(), read);
    const selected = goalId ? current.goals.items.find((item) => item.id === goalId) : current.goals.items[0];
    if (goalId) need(selected, "指定历史目标不存在或不在当前班级范围。");
    return selected;
  }
  async function ensureTasks(goalId, desired, read, explicitStudent = false) {
    if (!current.taskPages.has(goalId)) current.taskPages.set(goalId, pageState());
    const page = current.taskPages.get(goalId);
    if (!page.loaded) await getPage(page, tasksPath(goalId), read, { goalId });
    const matches = (item) => desired.taskId ? item.id === desired.taskId : desired.studentId ? item.student_id === desired.studentId : true;
    while (!page.items.some(matches) && page.more) await getPage(page, tasksPath(goalId), read, { goalId });
    const selected = page.items.find(matches);
    if (desired.taskId || explicitStudent) need(selected, "指定学生或任务不在当前目标范围。");
    return selected || page.items[0];
  }
  async function readThreads(selection, read) {
    const page = pageState();
    while (page.more) await getPage(page, `/companion/discussion-threads?class_id=${encode(state.classId)}&task_id=${encode(selection.taskId)}`, read);
    for (const thread of page.items) need(thread.task_id === selection.taskId, "短评列表包含不属于当前任务的线程。");
    return page.items;
  }
  async function readThreadDetail(threadId, selection, read, { item = null, revision = null, messageId = "" } = {}) {
    const detail = validateThread(await get(`/companion/discussion-threads/${encode(threadId)}`, read), selection, item, revision);
    need(detail.id === threadId && Array.isArray(detail.messages) && typeof detail.can_reply === "boolean",
      "短评详情缺少对应线程、消息或回复权限。");
    const messages = [], seen = new Set();
    const append = (page) => {
      const before = seen.size;
      for (const message of page) {
        need(identifier(message?.id) && message.thread_id === threadId && typeof message.text === "string",
          "短评消息与所选线程不一致，已停止读取。");
        if (!seen.has(message.id)) { seen.add(message.id); messages.push(message); }
      }
      need(!page.length || seen.size > before, "短评消息分页没有前进，请刷新后重试。");
    };
    // The detail route contains the first 500 messages, in the same order as
    // the messages route. Read every remaining page before publishing the detail.
    let page = detail.messages, offset = page.length;
    append(page);
    while (page.length === 500) {
      page = rows(await get(`/companion/discussion-threads/${encode(threadId)}/messages?limit=500&offset=${offset}`, read));
      append(page); offset += page.length;
    }
    if (messageId) need(messages.some((message) => message.id === messageId && !message.redacted && message.text.trim()),
      "通知对应的短评消息不存在或当前不可见，请刷新后重试。");
    return { ...detail, messages };
  }
  async function readBundle(selection, read, initialReview = null, threadId = "", messageId = "") {
    const [review, history, threads, appeals] = await Promise.all([
      initialReview || get(`/attempts/${encode(selection.attemptId)}/teacher-review`, read),
      get(`/attempts/${encode(selection.attemptId)}/corrections`, read), readThreads(selection, read),
      get(`/tasks/${encode(selection.taskId)}/appeals`, read),
    ]);
    if (!read.live()) throw STALE;
    validateReview(review, selection);
    const correctionRows = rows(history), appealRows = rows(appeals), details = new Map();
    for (const item of correctionRows) if (item.attempt_id) need(item.attempt_id === selection.attemptId, "更正历史不属于当前提交。");
    const wanted = threads.filter((thread) => thread.attempt_id === selection.attemptId && thread.grading_revision === review.attempt.revision
      && review.items.some((item) => item.question_version_id === thread.question_version_id));
    if (threadId && !wanted.some((thread) => thread.id === threadId)) wanted.push(threads.find((thread) => thread.id === threadId) || { id: threadId });
    await Promise.all(wanted.map(async (thread) => {
      const detail = await readThreadDetail(thread.id, selection, read, {
        item: thread.question_version_id ? { question_version_id: thread.question_version_id } : null,
        revision: thread.grading_revision ?? null, messageId: thread.id === threadId ? messageId : "",
      });
      need(detail.grading_revision <= review.attempt.revision
        && review.items.some((item) => item.question_version_id === detail.question_version_id), "短评详情与所选题目或评分修订不一致。");
      details.set(detail.id, detail);
    }));
    if (!read.live()) throw STALE;
    // A notification's directly authorized thread may be absent from a changing
    // paged list. It must still be a selectable thread so its messages are shown.
    if (threadId && !threads.some((thread) => thread.id === threadId)) threads.push(details.get(threadId));
    return { review, history: correctionRows, threads, details, appeals: appealRows };
  }
  function invalidateConfirmations(review) {
    const revision = review?.attempt?.revision;
    for (const [key, draft] of current.drafts) if (key.startsWith(`${review?.attempt.id}:`) && draft.revision !== revision) {
      draft.confirmed = false; draft.revision = revision;
      if (draft.dirty) draft.notice = "评分依据已更新，输入已保留；请重新核对并勾选确认。";
    }
    // A reread can change the revision or the default thread beneath an unsent
    // comment. Keep its text, but require confirmation of the newly read binding.
    for (const [key, draft] of current.commentDrafts) if (key.startsWith(`${review?.attempt.id}:`)) draft.confirmed = false;
    for (const draft of current.appealDrafts.values()) draft.confirmed = false;
  }
  async function openSelection(input = {}, options = {}) {
    syncContext();
    if (!teacherSession() || !scopeReady() || state.loggingOut || current.revoked) return false;
    if (current.command) { current.error = "请先确认上次操作的结果，再切换学生、目标或提交批次。"; paint(); return false; }
    if (input.messageId != null && (!identifier(input.messageId) || !identifier(input.threadId))) {
      current.error = "通知缺少有效的短评线程或消息标识，请刷新通知后重试。"; paint(); return false;
    }
    const previous = cleanSelection(current.selection), supplied = cleanSelection(input);
    const explicit = (key) => identifier(input?.[key]);
    const hasInput = Object.values(supplied).some(Boolean) || identifier(input?.threadId);
    if (!options.force && current.loaded && !current.needsRefresh && (!hasInput || Object.keys(supplied).every((key) => !supplied[key] || supplied[key] === previous[key])) && !input.threadId) return true;
    current.suspended = false;
    const read = beginRead(); current.error = "";
    let desired = hasInput ? { ...supplied } : { ...previous };
    // A goal/student-only link may reuse a legal previous task, never a task from another goal/student.
    if (hasInput && (!supplied.goalId || supplied.goalId === previous.goalId) && (!supplied.studentId || supplied.studentId === previous.studentId)
        && !supplied.taskId && !supplied.attemptId && !input.threadId) desired = { ...previous, ...Object.fromEntries(Object.entries(supplied).filter(([, value]) => value)) };
    // Keep the existing notification destination and its drafts until every
    // message page validates; a failed notification must not erase that context.
    if (!input.threadId && !equalSelection(desired, previous)) { current.review = null; current.history = []; current.threads = []; current.threadDetails.clear(); current.appeals = []; current.appealDetails.clear(); }
    paint();
    try {
      if (!current.classroom || options.force) {
        const [classroom, presets] = await Promise.all([
          get(`/classes/${encode(state.classId)}`, read),
          get("/companion/discussion-presets", read).catch((error) => { if (error === STALE || [401, 403].includes(Number(error?.status))) throw error; return []; }),
        ]);
        validateScope(classroom); need(classroom.id === state.classId && typeof classroom.can_manage === "boolean", "班级权限尚未成功核验。");
        current.classroom = classroom; current.presets = rows(presets).filter((item) => identifier(item?.key) && typeof item.text === "string" && item.key !== "response_failed");
      }
      if (options.force) { current.goals = pageState(); current.taskPages = new Map(); }
      let initialReview = null;
      if (input.threadId) {
        const thread = await get(`/companion/discussion-threads/${encode(input.threadId)}`, read);
        validateScope(thread); need(thread.id === input.threadId, "通知线程与指定记录不一致。");
        for (const [field, value] of [["taskId", thread.task_id], ["attemptId", thread.attempt_id], ["studentId", thread.student_id]]) {
          need(identifier(value) && (!supplied[field] || supplied[field] === value), "通知线程与指定提交不一致。"); desired[field] = value;
        }
      }
      if (desired.attemptId && (!desired.taskId || !desired.goalId)) {
        initialReview = validateReview(await get(`/attempts/${encode(desired.attemptId)}/teacher-review`, read), desired);
        desired = { goalId: initialReview.goal_id, taskId: initialReview.task_id, attemptId: initialReview.attempt.id, studentId: initialReview.student_id };
      }
      if (desired.taskId && !desired.goalId) {
        const task = await get(`/tasks/${encode(desired.taskId)}`, read); validateScope(task);
        need(task.id === desired.taskId && identifier(task.goal_id) && (!desired.studentId || task.student_id === desired.studentId), "任务与指定学生不一致。");
        desired.goalId = task.goal_id; desired.studentId = task.student_id;
      }
      let selectedGoal;
      try { selectedGoal = await ensureGoal(desired.goalId, read); }
      catch (error) { if (hasInput || error === STALE || error?.status) throw error; desired = emptySelection(); selectedGoal = await ensureGoal("", read); }
      if (!selectedGoal) {
        current.task = null; current.review = null; current.history = []; current.threads = []; current.appeals = [];
        current.loaded = true; current.needsRefresh = false; notifySelection(emptySelection()); return true;
      }
      let taskRow;
      try { taskRow = await ensureTasks(selectedGoal.id, desired, read, explicit("studentId")); }
      catch (error) { if (hasInput || error === STALE || error?.status) throw error; desired.taskId = desired.attemptId = ""; taskRow = await ensureTasks(selectedGoal.id, desired, read); }
      if (!taskRow) {
        current.task = null; current.review = null; current.history = []; current.threads = []; current.appeals = [];
        current.loaded = true; current.needsRefresh = false; notifySelection({ goalId: selectedGoal.id }); return true;
      }
      const task = await get(`/tasks/${encode(taskRow.id)}`, read); validateScope(task);
      need(task.id === taskRow.id && task.goal_id === selectedGoal.id && identifier(task.student_id) && Array.isArray(task.attempts)
        && (!supplied.studentId || supplied.studentId === task.student_id), "任务详情与所选目标或学生不一致。");
      let attempt = task.attempts.find((item) => item.id === desired.attemptId);
      if (explicit("attemptId") || input.threadId) need(attempt, "指定提交批次不属于这个任务。");
      attempt ||= task.attempts.find((item) => item.id === task.first_attempt_id) || task.attempts[0];
      const chosen = { goalId: selectedGoal.id, taskId: task.id, attemptId: attempt?.id || "", studentId: task.student_id };
      if (attempt) {
        const bundle = await readBundle(chosen, read, initialReview, input.threadId || "", input.messageId || "");
        if (!read.live()) return false;
        current.review = bundle.review; current.history = bundle.history; current.threads = bundle.threads;
        current.threadDetails = bundle.details; current.appeals = bundle.appeals; current.appealDetails.clear();
        invalidateConfirmations(bundle.review);
        if (input.threadId) {
          const detail = bundle.details.get(input.threadId), item = bundle.review.items.find((entry) => entry.question_version_id === detail.question_version_id);
          const key = `${attempt.id}:${item.question_id}`;
          const draft = current.commentDrafts.get(key) || { text: "", preset: "", confirmed: false };
          draft.threadId = detail.id; current.commentDrafts.set(key, draft);
        }
      } else {
        current.review = null; current.history = []; current.threads = []; current.threadDetails.clear(); current.appealDetails.clear();
        current.appeals = rows(await get(`/tasks/${encode(task.id)}/appeals`, read));
      }
      if (!read.live()) return false;
      current.task = task; const index = current.taskPages.get(selectedGoal.id).items.findIndex((item) => item.id === task.id);
      current.taskPages.get(selectedGoal.id).items[index] = task;
      current.loaded = true; current.needsRefresh = false; notifySelection(chosen);
      if (input.messageId) {
        current.reading = false; paint();
        return read.live() && root?.isConnected === true && [...root.querySelectorAll("[data-review-message]")].some((node) =>
          node.getAttribute("data-review-message") === input.messageId && node.getAttribute("data-review-thread") === input.threadId
          && node.getAttribute("data-review-visible") === "true");
      }
      return true;
    } catch (error) { handleReadError(error, read); current.needsRefresh = read.live() ? true : current.needsRefresh; return false; }
    finally { if (read.live()) { current.reading = false; paint(); } }
  }
  function load(options = {}) {
    syncContext();
    if (loadingPromise && !options.force) return loadingPromise;
    const promise = openSelection({}, options); loadingPromise = promise;
    promise.finally(() => { if (loadingPromise === promise) loadingPromise = null; });
    return promise;
  }
  async function more(kind) {
    syncContext(); if (!active() || current.command || current.reading) return false;
    const page = kind === "goals" ? current.goals : current.taskPages.get(current.selection.goalId);
    if (!page?.more) return false;
    const read = beginRead(); paint();
    try { await getPage(page, kind === "goals" ? goalsPath() : tasksPath(current.selection.goalId), read,
      kind === "goals" ? {} : { goalId: current.selection.goalId }); return true; }
    catch (error) { handleReadError(error, read); return false; }
    finally { if (read.live()) { current.reading = false; paint(); } }
  }
  function gradeDraft(item) {
    const key = draftKey(item);
    if (!current.drafts.has(key)) current.drafts.set(key, { score: itemScore(item) == null ? "" : String(itemScore(item)), reason: "", confirmed: false, revision: current.review.attempt.revision, dirty: false, notice: "" });
    return current.drafts.get(key);
  }
  function commentDraft(item) {
    const key = draftKey(item);
    if (!current.commentDrafts.has(key)) current.commentDrafts.set(key, { text: "", preset: "", threadId: "", confirmed: false, dirty: false });
    return current.commentDrafts.get(key);
  }
  function matchingThreads(item) { return current.threads.filter((thread) => thread.attempt_id === current.selection.attemptId && thread.question_version_id === item.question_version_id); }
  function selectedThread(item) {
    const draft = commentDraft(item);
    return matchingThreads(item).find((thread) => thread.id === draft.threadId)
      || (!draft.threadId ? matchingThreads(item).find((thread) => thread.grading_revision === current.review.attempt.revision) : null);
  }
  function managementDraft(kind) {
    const key = `${kind}:${kind === "stop-goal" ? current.selection.goalId : current.selection.taskId}`;
    if (!current.managementDrafts.has(key)) current.managementDrafts.set(key, { reason: "", deadline: "", confirmed: false, dirty: false });
    return current.managementDrafts.get(key);
  }
  function appealDraft(id) {
    if (!current.appealDrafts.has(id)) current.appealDrafts.set(id, { reason: "", confirmed: false, dirty: false });
    return current.appealDrafts.get(id);
  }
  const failInput = (message) => { current.error = message; paint(); return false; };
  function reasonError(draft) { return !draft.reason.trim() || draft.reason.trim().length > 2000 ? "请填写 1–2000 字的处理依据。" : !draft.confirmed ? "请核对依据并勾选确认后提交。" : ""; }
  function commandBase(kind, draft) { return { kind, draft, selection: Object.freeze({ ...current.selection }), token: epoch, data: current, inFlight: false, stage: "ready" }; }
  async function submitGrade(questionId) {
    syncContext(); const item = itemById(questionId);
    if (!item || !mayGrade(item) || writeLocked()) return false;
    const draft = gradeDraft(item), units = scoreUnits(draft.score), maximum = itemMaximum(item), problem = reasonError(draft);
    if (units == null || maximum == null || units > maximum) return failInput("请填写 0 到该题满分之间的分数，最多两位小数。");
    if (problem) return failInput(problem);
    const kind = manual() ? "manual-grade" : "correction";
    const command = { ...commandBase(kind, draft), questionId, draftKey: draftKey(item), path: `/attempts/${encode(current.selection.attemptId)}/items/${encode(questionId)}/${kind === "correction" ? "corrections" : kind}`,
      body: Object.freeze({ request_id: newId(), expected_revision: current.review.attempt.revision, score_units: units, reason: draft.reason.trim() }) };
    return execute(command);
  }
  async function submitManagement(kind) {
    syncContext(); if (!manage() || writeLocked() || !current.task) return false;
    const draft = managementDraft(kind), problem = reasonError(draft);
    if (problem) return failInput(problem);
    let path, body;
    if (kind === "stop-goal") {
      if (!goal() || goal().status === "closed" || !positiveInteger(goal().version)) return failInput("当前目标没有可停止的后续计划，或版本尚未读全。");
      path = `/goals/${encode(goal().id)}/plan`;
      body = { request_id: newId(), expected_version: goal().version, action: "close", reason: draft.reason.trim() };
    } else {
      if (current.task.submission_closed || !positiveInteger(current.task.version)) return failInput("该任务已关闭提交，或当前任务版本尚未读全。");
      path = `/tasks/${encode(current.task.id)}/${kind === "deadline" ? "deadline-extensions" : "close"}`;
      body = { request_id: newId(), expected_version: current.task.version, reason: draft.reason.trim() };
      if (kind === "deadline") {
        const seconds = new Date(draft.deadline).getTime() / 1000;
        if (!finite(seconds) || seconds <= Date.now() / 1000 || (finite(current.task.deadline_at) && seconds <= current.task.deadline_at)) return failInput("延期时间须晚于现在和当前截止时间。");
        body.new_deadline = seconds;
      } else if (["submitted", "grading"].includes(current.task.status)) return failInput("正在评分的任务暂不能关闭提交，请等待评分完成后再核对。");
    }
    return execute({ ...commandBase(kind, draft), path, body: Object.freeze(body) });
  }
  async function openAppeal(id) {
    syncContext(); if (!active() || current.command || current.reading || !current.appeals.some((item) => item.id === id)) return false;
    const read = beginRead(); paint();
    try {
      const detail = validateAppeal(await get(`/appeals/${encode(id)}`, read));
      need(detail.id === id && current.task.attempts.some((attempt) => attempt.id === detail.attempt_id)
        && detail.student_id === current.selection.studentId, "异议详情与当前学生或任务不一致。");
      const prior = current.appealDetails.get(id); if (prior?.current_revision !== detail.current_revision) appealDraft(id).confirmed = false;
      current.appealDetails.set(id, detail); return true;
    } catch (error) { handleReadError(error, read); return false; }
    finally { if (read.live()) { current.reading = false; paint(); } }
  }
  async function resolveAppeal(id) {
    syncContext(); const detail = current.appealDetails.get(id);
    if (!manage() || writeLocked() || !detail || detail.can_resolve !== true || detail.status !== "awaiting_teacher") return false;
    const draft = appealDraft(id), problem = reasonError(draft); if (problem) return failInput(problem);
    return execute({ ...commandBase("resolve", draft), appealId: id, appealAttemptId: detail.attempt_id,
      path: `/appeals/${encode(id)}/resolve`, body: Object.freeze({ request_id: newId(), expected_revision: detail.current_revision, reason: draft.reason.trim() }) });
  }
  async function openThread(questionId, threadId) {
    syncContext(); const item = itemById(questionId);
    if (!active() || !item || current.command || current.reading) return false;
    const draft = commentDraft(item);
    if (!threadId) { draft.threadId = ""; draft.confirmed = false; paint(); return true; }
    const selected = matchingThreads(item).find((thread) => thread.id === threadId);
    if (!selected) return false;
    const read = beginRead(); paint();
    try {
      const detail = await readThreadDetail(threadId, current.selection, read, { item, revision: selected.grading_revision });
      if (!read.live()) return false;
      current.threadDetails.set(threadId, detail); draft.threadId = threadId; draft.confirmed = false; return true;
    } catch (error) { handleReadError(error, read); return false; }
    finally { if (read.live()) { current.reading = false; paint(); } }
  }
  async function submitComment(questionId) {
    syncContext(); const item = itemById(questionId);
    if (!manage() || !item || writeLocked()) return false;
    const draft = commentDraft(item), thread = selectedThread(item), detail = thread && current.threadDetails.get(thread.id);
    if (draft.threadId && !thread) return failInput("原选短评线程已不可用，请重新选择线程并核对后确认；原文已保留。");
    if (thread && detail?.can_reply !== true) return failInput("当前线程不能回复，或回复权限尚未读全。");
    const preset = draft.preset ? current.presets.find((entry) => entry.key === draft.preset) : null;
    if (draft.preset && !preset) return failInput("该安全提示语已不可用，请重新选择。");
    if (!preset && (!draft.text.trim() || draft.text.trim().length > 20000)) return failInput("请填写 1–20000 字的短评，或选择安全提示语。");
    if (!draft.confirmed) return failInput("请核对题目、提交批次与短评内容，并勾选确认。");
    const revision = thread?.grading_revision ?? current.review.attempt.revision;
    const binding = { task_id: current.selection.taskId, attempt_id: current.selection.attemptId, question_version_id: item.question_version_id, grading_revision: revision };
    // Creating a thread changes selectedThread(), not the teacher's message.
    // Keep recovery stable across that transition, but never across bindings.
    const signature = JSON.stringify([binding, draft.text, draft.preset]);
    const recovery = draft.recovery?.signature === signature && draft.recovery.threadId === thread?.id ? draft.recovery : null;
    const command = { ...commandBase("discussion", draft), questionId, draftKey: draftKey(item), binding: Object.freeze(binding), signature,
      threadId: recovery?.threadId || thread?.id || "", phase: recovery?.threadId || thread?.id ? "message" : "create",
      createBody: recovery?.createBody || Object.freeze({ request_id: newId(), ...binding, title: "教师短评" }),
      messageBody: recovery?.messageBody || Object.freeze({ request_id: newId(), text: preset ? "" : draft.text.trim(), ...(preset ? { preset_key: preset.key } : {}) }) };
    return execute(command);
  }
  function receiptValid(result, command) {
    if (command.kind === "manual-grade" || command.kind === "correction") return result && positiveInteger(result.grading_revision)
      && result.grading_revision === command.body.expected_revision + 1 && result.attempt_id === command.selection.attemptId
      && result.question_id === command.questionId && result.score_units === command.body.score_units;
    if (command.kind === "close-task") return result?.id === command.selection.taskId && result.submission_closed === true && positiveInteger(result.version);
    if (command.kind === "deadline") return (result?.id === command.selection.taskId || result?.task_id === command.selection.taskId) && positiveInteger(result.version);
    if (command.kind === "stop-goal") return result?.action === "close" && positiveInteger(result.version) && (!result.goal_id || result.goal_id === command.selection.goalId);
    if (command.kind === "resolve") return result?.id === command.appealId && result.task_id === command.selection.taskId
      && result.attempt_id === command.appealAttemptId && result.status === "resolved" && positiveInteger(result.current_revision);
    return identifier(result?.id) && result.thread_id === command.threadId;
  }
  async function sendDiscussionCommand(command) {
    const alive = () => valid(command.token, command.data) && current.command === command;
    if (command.phase === "create") {
      const thread = await api.call("POST", "/companion/discussion-threads", command.createBody);
      if (!alive()) throw STALE;
      validateThread(thread, command.selection, { question_version_id: command.binding.question_version_id }, command.binding.grading_revision);
      command.threadId = thread.id; command.phase = "message";
      if (!current.threads.some((entry) => entry.id === thread.id)) current.threads.push(thread);
    }
    if (state.loggingOut) { command.stage = "paused"; command.inFlight = false; return null; }
    // A creation receipt does not grant permission to reply. Validate the detail,
    // including can_reply, before proceeding to the already-frozen message.
    let detail = current.threadDetails.get(command.threadId);
    if (!detail) {
      detail = await api.call("GET", `/companion/discussion-threads/${encode(command.threadId)}`);
      if (!alive()) throw STALE;
      validateThread(detail, command.selection, { question_version_id: command.binding.question_version_id }, command.binding.grading_revision);
      need(detail.id === command.threadId && Array.isArray(detail.messages) && typeof detail.can_reply === "boolean",
        "短评详情缺少对应线程、消息或回复权限，原请求已保留。");
      current.threadDetails.set(detail.id, detail);
    }
    if (state.loggingOut) { command.stage = "paused"; command.inFlight = false; return null; }
    if (detail.can_reply !== true) throw Object.assign(new Error("当前线程不允许回复，短评原文已保留。"), { status: 403 });
    return api.call("POST", `/companion/discussion-threads/${encode(command.threadId)}/messages`, command.messageBody);
  }
  async function execute(command, retry = false) {
    syncContext();
    if (!manage() || command.inFlight || (current.command && (!retry || current.command !== command))
        || !valid(command.token, command.data)) return false;
    if (retry && !["unknown", "paused"].includes(command.stage)) return false;
    current.command = command; command.inFlight = true; command.stage = "sending"; current.error = ""; current.notice = ""; paint();
    let result;
    try {
      result = command.kind === "discussion" ? await sendDiscussionCommand(command) : await api.call("POST", command.path, command.body);
      if (!valid(command.token, command.data) || current.command !== command) return false;
      if (command.stage === "paused") return false;
      need(receiptValid(result, command), "服务未返回可核验的保存回执；保留原请求供显式重试。");
    } catch (error) {
      if (error === STALE || !valid(command.token, command.data) || current.command !== command) return false;
      command.inFlight = false;
      if (unknownOutcome(error)) {
        command.stage = "unknown"; current.error = "提交结果尚未确认。输入和原请求已锁定；只会在点击“使用原请求重试”后重发相同内容。";
      } else {
        current.command = null; command.draft.confirmed = false;
        if (command.kind === "discussion" && command.threadId) command.draft.recovery = { signature: command.signature, threadId: command.threadId, createBody: command.createBody, messageBody: command.messageBody };
        current.error = error?.message || "操作未保存，输入已保留。";
        if (Number(error.status) === 409) {
          current.needsRefresh = true;
          if (!state.loggingOut) {
            await load({ force: true });
            if (!valid(command.token, command.data)) return false;
            if (command.kind === "resolve" && !current.needsRefresh) await openAppeal(command.appealId);
            current.error = "版本或状态已变化，已重新读取依据。输入已保留；请核对后重新勾选确认，再提交新请求。";
          }
        } else if ([401, 403, 404].includes(Number(error.status))) revokeAccess();
      }
      if (!state.loggingOut) paint();
      return false;
    } finally {
      if (valid(command.token, command.data)) command.inFlight = false;
    }
    if (!valid(command.token, command.data)) return false;
    current.command = null;
    if (["manual-grade", "correction"].includes(command.kind)) current.drafts.delete(command.draftKey);
    else if (command.kind === "discussion") current.commentDrafts.delete(command.draftKey);
    else if (command.kind === "resolve") current.appealDrafts.delete(command.appealId);
    else { command.draft.reason = ""; command.draft.deadline = ""; command.draft.confirmed = false; command.draft.dirty = false; }
    current.needsRefresh = true;
    current.notice = ({ "manual-grade": "人工评分已保存。", correction: "评分更正已保存，原结果保留在历史中。", discussion: "短评已保存。",
      deadline: "任务截止时间已延长。", "close-task": "已关闭此任务的后续提交，已有作答和成绩继续保留。", "stop-goal": "已停止目标后续计划，已发布任务仍保留。", resolve: "异议已结案，处理依据已记录。" })[command.kind];
    if (state.loggingOut) return true;
    // A read/hook failure never turns a confirmed mutation back into an unknown POST.
    const refreshed = await load({ force: true });
    if (!valid(command.token, command.data) || state.loggingOut) return true;
    if (command.kind === "resolve" && refreshed) await openAppeal(command.appealId);
    if (!valid(command.token, command.data) || state.loggingOut) return true;
    if (!refreshed) current.notice += " 依据刷新未完成，请重新读取；不要重复提交。";
    if (command.kind === "discussion") current.notice += result.delivery === "deferred" ? " 自由短评将在整轮评分开放后向学生展示。" : " 已按服务返回的可见范围送达。";
    paint(); toast(current.notice);
    if (typeof onChanged === "function") {
      try { await onChanged({ ...command.selection, kind: command.kind }); }
      catch { if (valid(command.token, command.data) && !state.loggingOut) { current.notice += " 班级通知刷新未完成。"; paint(); } }
    }
    return true;
  }
  async function retryCommand() {
    syncContext(); const command = current.command;
    if (!command || command.inFlight || state.loggingOut) return false;
    return execute(command, true);
  }
  function focusedIdentity(node) { return ["data-review-field", "data-review-scope", "data-review-id", "data-review-action", "name"].map((name) => node?.getAttribute?.(name) ?? null); }
  function paint() {
    if (context !== contextKey() || state.loggingOut || current.suspended) return;
    if (!root?.isConnected) { if (!state.teacherView || state.teacherView === "review") renderApp(); return; }
    const focused = root.ownerDocument?.activeElement, identity = root.contains?.(focused) ? focusedIdentity(focused) : null;
    const start = focused?.selectionStart, end = focused?.selectionEnd, scroll = focused?.scrollTop;
    root.innerHTML = innerHtml(); bind(root);
    if (identity?.some((value) => value !== null)) {
      const target = [...root.querySelectorAll("[data-review-field], [data-review-action], button[type=submit]")].find((node) => JSON.stringify(focusedIdentity(node)) === JSON.stringify(identity));
      if (target && !target.disabled) {
        target.focus?.({ preventScroll: true });
        if (typeof start === "number" && target.setSelectionRange) try { target.setSelectionRange(start, end); } catch { /* Number/datetime controls have no selection range. */ }
        if (scroll != null) target.scrollTop = scroll;
      }
    }
  }
  const note = (text, error = false) => text ? `<div class="tr-notice${error ? " tr-error" : ""}" role="${error ? "alert" : "status"}">${esc(text)}</div>` : "";
  const button = (action, label, id = "", disabled = false) => `<button type="button" class="tr-button" data-review-action="${esc(action)}" data-review-id="${esc(id)}"${disabled ? " disabled" : ""}>${esc(label)}</button>`;
  const field = (scope, id, name, label, value, attributes = "") => `<label class="tr-field">${esc(label)}<input name="${esc(name)}" data-review-scope="${scope}" data-review-id="${esc(id)}" data-review-field="${name}" value="${esc(value)}" ${attributes}></label>`;
  const textField = (scope, id, name, label, value, maximum = 2000) => `<label class="tr-field">${esc(label)}<textarea name="${esc(name)}" rows="3" maxlength="${maximum}" data-review-scope="${scope}" data-review-id="${esc(id)}" data-review-field="${name}">${esc(value)}</textarea></label>`;
  const confirmField = (scope, id, draft, label = "已核对本次处理内容与依据") => `<label class="tr-confirm"><input type="checkbox" name="confirmed" data-review-scope="${scope}" data-review-id="${esc(id)}" data-review-field="confirmed" ${draft.confirmed ? "checked" : ""}>${esc(label)}</label>`;
  const disabled = (condition) => condition ? " disabled" : "";
  function selectorsHtml() {
    const selected = current.selection, page = current.taskPages.get(selected.goalId), taskRows = page?.items || [];
    const students = [...new Map(taskRows.map((task) => [task.student_id, studentName(task.student_id, task)])).entries()];
    const shown = current.studentFilter ? taskRows.filter((task) => task.student_id === current.studentFilter) : taskRows;
    const locked = Boolean(current.command);
    return `<section class="tr-selector" aria-label="选择历史提交"><div class="tr-selector-grid">
      <label class="tr-field">历史学习目标<select data-review-field="goal"${disabled(locked || !current.goals.items.length)}><option value="">选择目标</option>${current.goals.items.map((item) => `<option value="${esc(item.id)}" ${item.id === selected.goalId ? "selected" : ""}>${esc(item.title || item.id)} · ${esc(statusText(item.status))}</option>`).join("")}</select></label>
      <label class="tr-field">学生范围<select data-review-field="student"${disabled(locked || !students.length)}><option value="">全部已读取的学生</option>${students.map(([id, name]) => `<option value="${esc(id)}" ${id === current.studentFilter ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></label>
      <label class="tr-field">目标内任务<select data-review-field="task"${disabled(locked || !shown.length)}><option value="">选择任务</option>${shown.map((item) => `<option value="${esc(item.id)}" ${item.id === selected.taskId ? "selected" : ""}>${esc(studentName(item.student_id, item))} · 第 ${esc(item.round_no || "—")} 轮 · ${esc(statusText(item.status))}</option>`).join("")}</select></label>
      <label class="tr-field">提交批次<select data-review-field="attempt"${disabled(locked || !current.task?.attempts.length)}><option value="">选择提交批次</option>${(current.task?.attempts || []).map((item) => `<option value="${esc(item.id)}" ${item.id === selected.attemptId ? "selected" : ""}>${esc(attemptText(item))} · ${esc(statusText(item.status))} · ${esc(dateText(item.submitted_at))}</option>`).join("")}</select></label>
      </div><div class="tr-actions">${current.goals.more && current.goals.loaded ? button("more-goals", "加载更多历史目标", "", locked || current.reading) : ""}${page?.more ? button("more-tasks", "加载更多任务与学生", "", locked || current.reading) : ""}<span class="tr-muted">已读取 ${current.goals.items.length} 个目标、当前目标 ${taskRows.length} 个任务。${page?.more ? "还有任务尚未加载。" : ""}</span></div></section>`;
  }
  function gradingEvidence(value, label) {
    if (!value || (typeof value === "object" && !Object.keys(value).length)) return `<p class="tr-muted">${esc(label)}尚未提供。</p>`;
    if (typeof value === "string") return `<p class="tr-text">${esc(value)}</p>`;
    const lines = [];
    if (value.method || value.source) lines.push(`评分方式：${value.method || value.source}`);
    if (value.status) lines.push(`状态：${statusText(value.status)}`);
    for (const key of ["reason", "explanation", "feedback", "rationale", "why"]) if (value[key]) lines.push(plainText(value[key]));
    for (const entry of value.per_point || value.rubric_scores || []) lines.push(`${entry.point || entry.label || "评分点"}：${finite(entry.score_units) ? entry.score_units / 100 : "待评分"} 分；${entry.why || entry.reason || ""}`);
    return `<p class="tr-text">${esc(lines.length ? lines.join("\n") : plainText(value))}</p>`;
  }
  function questionHtml(item, index) {
    const question = item.question, draft = gradeDraft(item), allowed = mayGrade(item), maximum = itemMaximum(item);
    const history = current.history.filter((entry) => entry.question_id === item.question_id);
    return `<article class="tr-question" data-review-question="${esc(item.question_id)}"><header class="tr-question-head"><h3>第 ${index + 1} 题</h3><span class="tr-badge">${esc(statusText(item.status))}</span><strong>${esc(scoreText(itemScore(item)))}${maximum == null ? "" : ` / ${maximum / 100} 分`}</strong></header>
      <div class="tr-question-columns"><section class="tr-evidence"><h4>冻结题干</h4><p class="tr-text">${esc(question.stem || "题干未提供")}</p>${question.options?.length ? `<ol class="tr-options">${question.options.map((option) => `<li>${esc(typeof option === "object" ? option.text ?? option.label ?? plainText(option) : option)}</li>`).join("")}</ol>` : ""}
      <h4>学生答案</h4><p class="tr-answer tr-text">${esc(answerText(item.answer, question))}</p><h4>参考答案</h4><p class="tr-text">${esc(answerText(question.answer ?? question.reference_answer, question, true))}</p>${question.explanation ? `<p class="tr-text tr-muted">${esc(question.explanation)}</p>` : ""}
      ${question.rubric?.length ? `<h4>冻结评分量表</h4><ul>${question.rubric.map((entry) => `<li>${esc(entry.point || entry.label || plainText(entry))}${Number.isSafeInteger(entry.score_units) ? ` · ${entry.score_units / 100} 分` : ""}</li>`).join("")}</ul>` : ""}</section>
      <section class="tr-marking"><h4>当前评分依据</h4>${gradingEvidence(item.result, "评分依据")}<details><summary>原始评分记录</summary>${gradingEvidence(item.original_grading, "原始评分")}</details>
      ${allowed ? `<form data-review-form="grade" data-review-id="${esc(item.question_id)}"><fieldset${disabled(writeLocked())}><legend>${manual() ? "人工评分" : "更正评分"}</legend>${field("grade", item.question_id, "score", "本题分数", draft.score, `type="text" inputmode="decimal" autocomplete="off" aria-describedby="tr-score-${index}"`)}<p id="tr-score-${index}" class="tr-muted">允许 0–${maximum / 100} 分，最多两位小数。</p>${textField("grade", item.question_id, "reason", "评分依据", draft.reason)}${draft.notice ? note(draft.notice) : ""}${confirmField("grade", item.question_id, draft, "已核对当前修订的分数与评分依据")}<button class="tr-button tr-primary" type="submit">${manual() ? "保存人工评分" : "保存更正并保留原记录"}</button></fieldset></form>`
        : `<p class="tr-muted">${!manage() ? "当前为只读权限，不能修改分数。" : current.review.attempt.status === "review_required" && item.status === "graded" ? "本题已有评分。整份提交评分完成后，才能追加更正。" : "本题当前状态不允许人工评分或更正。"}</p>`}
      <details${history.length ? " open" : ""}><summary>本题更正历史（${history.length}）</summary>${history.length ? `<ol class="tr-history">${history.map((entry) => `<li><strong>${esc(scoreText(finite(entry.prior_score_units) ? entry.prior_score_units / 100 : null))} → ${esc(scoreText(finite(entry.corrected_score_units) ? entry.corrected_score_units / 100 : null))} 分</strong><p class="tr-text">${esc(entry.reason)}</p><small>${esc(dateText(entry.created_at))} · 修订 ${esc(entry.grading_revision ?? entry.revision ?? "—")}</small></li>`).join("")}</ol>` : '<p class="tr-muted">尚无更正记录。</p>'}</details></section></div>${discussionHtml(item)}</article>`;
  }
  function discussionHtml(item) {
    const draft = commentDraft(item), thread = selectedThread(item), detail = thread && current.threadDetails.get(thread.id), candidates = matchingThreads(item);
    const canReply = manage() && (!thread || detail?.can_reply === true);
    return `<section class="tr-discussion"><h4>围绕本题交流</h4><p class="tr-muted">绑定${esc(attemptText(current.review.attempt))}与冻结题目。${!current.review.results_released ? "整轮结果尚未开放：自由短评延迟展示；安全提示语可立即展示。" : "当前整轮结果已开放。"}</p>
      ${candidates.length ? `<label class="tr-field">短评线程<select data-review-field="thread" data-review-id="${esc(item.question_id)}"${disabled(current.command || current.reading)}><option value="">当前评分修订 ${current.review.attempt.revision}</option>${candidates.map((entry) => `<option value="${esc(entry.id)}" ${entry.id === draft.threadId ? "selected" : ""}>${esc(entry.title || "教师短评")} · 修订 ${entry.grading_revision}</option>`).join("")}</select></label>` : ""}
      ${detail ? `<ol class="tr-messages">${detail.messages.map((message) => `<li data-review-message="${esc(message.id)}" data-review-thread="${esc(detail.id)}" data-review-visible="${!message.redacted && Boolean(message.text?.trim())}"><strong>${message.author_role === "student" ? "学生" : "教师"}</strong><p class="tr-text">${esc(message.redacted ? message.notice || "内容暂未开放。" : message.text)}</p><small>${esc(dateText(message.created_at))}${message.delivery === "deferred" ? " · 记录为延迟展示，实际可见范围由服务核验" : ""}</small></li>`).join("") || '<li class="tr-muted">此线程尚无消息。</li>'}</ol>` : thread ? '<p class="tr-muted">线程详情尚未读取，发送前必须核验回复权限。</p>' : '<p class="tr-muted">当前评分修订还没有短评。</p>'}
      ${manage() ? `<form data-review-form="discussion" data-review-id="${esc(item.question_id)}"><fieldset${disabled(writeLocked() || !canReply)}><legend>${thread ? "继续回复" : "发送教师短评"}</legend><label class="tr-field">发送方式<select data-review-scope="discussion" data-review-id="${esc(item.question_id)}" data-review-field="preset"><option value="">自由短评</option>${current.presets.map((preset) => `<option value="${esc(preset.key)}" ${draft.preset === preset.key ? "selected" : ""}>${esc(preset.text)}</option>`).join("")}</select></label>${draft.preset ? `<p class="tr-text">${esc(current.presets.find((entry) => entry.key === draft.preset)?.text || "提示语已不可用")}</p>` : textField("discussion", item.question_id, "text", "写给学生", draft.text, 20000)}${confirmField("discussion", item.question_id, draft, "已核对本题、提交批次和短评内容")}<button type="submit" class="tr-button">${draft.recovery ? "恢复发送保留的短评" : "发送短评"}</button></fieldset></form>${!canReply ? '<p class="tr-muted">当前线程不允许回复。</p>' : ""}` : '<p class="tr-muted">当前为只读权限，不能发送短评。</p>'}</section>`;
  }
  function managementHtml() {
    if (!current.task) return "";
    const task = current.task;
    return `<section class="tr-panel"><h2>任务与后续计划</h2><p>截止时间：${esc(dateText(task.deadline_at))}。${task.submission_closed ? `已关闭提交（${esc(dateText(task.submission_closed_at))}）；已有作答与评分仍可复核。` : "当前尚未关闭提交。"}</p>
      ${manage() ? `<div class="tr-management">${["deadline", "close-task", "stop-goal"].map((kind) => {
        const draft = managementDraft(kind), label = { deadline: "延长此任务截止时间", "close-task": "关闭此任务的后续提交", "stop-goal": "停止目标后续计划" }[kind];
        const unavailable = kind === "stop-goal" ? goal()?.status === "closed" : task.submission_closed || (kind === "close-task" && ["grading", "submitted"].includes(task.status));
        return `<details><summary>${label}</summary><p class="tr-muted">${kind === "stop-goal" ? "停止这个学习目标后续轮次安排。不会关闭已发布任务的提交，也不删除已有成绩。" : kind === "close-task" ? "阻止当前学生继续提交这个任务；任务原评分状态和历史结果保留。正在评分时不能关闭。" : "只调整所选任务的截止时间；时间按本机时区输入。"}</p><form data-review-form="${kind}"><fieldset${disabled(writeLocked() || unavailable)}><legend class="tr-sr-only">${label}</legend>${kind === "deadline" ? field("manage", kind, "deadline", "新的截止时间", draft.deadline, 'type="datetime-local" required') : ""}${textField("manage", kind, "reason", "处理依据", draft.reason)}${confirmField("manage", kind, draft)}<button type="submit" class="tr-button">${label}</button></fieldset></form></details>`;
      }).join("")}</div>` : '<p class="tr-muted">当前班级为只读权限，不能延期、关闭提交或停止计划。</p>'}</section>`;
  }
  function appealsHtml() {
    if (!current.task) return "";
    return `<section class="tr-panel"><h2>学生异议</h2><p class="tr-muted">先核对对应提交的评分；需要改分时先保存更正，再读取最新异议详情并结案。</p>${current.appeals.length ? current.appeals.map((appeal) => {
      const detail = current.appealDetails.get(appeal.id), draft = appealDraft(appeal.id);
      return `<article class="tr-appeal"><header><strong>${esc(statusText(detail?.status || appeal.status))}</strong><span>提交 ${esc(appeal.attempt_id)} · 题目 ${esc(appeal.question_id)}</span></header><p class="tr-text">${esc(detail?.reason || appeal.reason)}</p><div class="tr-actions">${button("appeal", detail ? "刷新异议详情" : "查看异议详情", appeal.id, current.reading || current.command)}${button("appeal-attempt", "定位异议对应提交", appeal.id, current.command)}</div>
      ${detail ? `<p class="tr-muted">提出时修订 ${detail.grading_revision}；当前修订 ${detail.current_revision}。${detail.results_released ? "结果已开放。" : "结果尚未全部开放。"}</p>${gradingEvidence(detail.result, "复核结果")}${detail.status === "resolved" ? '<p class="tr-muted">此异议已结案。</p>' : manage() && detail.can_resolve === true && detail.status === "awaiting_teacher" ? `<form data-review-form="resolve" data-review-id="${esc(appeal.id)}"><fieldset${disabled(writeLocked())}><legend>教师结案</legend>${textField("appeal", appeal.id, "reason", "结案依据", draft.reason)}${confirmField("appeal", appeal.id, draft, "已核对当前修订，并确认异议处理完毕")}<button type="submit" class="tr-button">记录依据并结案</button></fieldset></form>` : '<p class="tr-muted">当前状态或权限不允许结案；只有待教师处理的异议可以结案。</p>'}` : ""}</article>`;
    }).join("") : '<p class="tr-muted">当前任务没有异议记录。</p>'}</section>`;
  }
  function innerHtml() {
    if (!teacherSession()) return note("请使用已连接的有效教师账号查看复核记录。");
    if (state.loggingOut) return note("正在退出，复核操作已暂停。原请求保留在当前会话；若退出失败，可继续核对结果。");
    if (current.revoked) return note(current.error, true);
    if (!scopeReady()) return note("请先选择课程和班级。");
    const review = current.review, command = current.command;
    return `<header class="tr-heading"><div><h1>作答复核</h1><p>从历史目标定位每次提交，按题目核对作答、评分和更正依据。</p></div><div class="tr-actions">${button("refresh", current.reading ? "正在读取…" : "刷新记录", "", current.reading || command?.inFlight)}${onReports && current.selection.goalId ? button("reports", "查看阶段报告", "", Boolean(command)) : ""}</div></header>
      ${note(current.error, true)}${note(current.notice)}${command ? note(command.inFlight ? "正在提交。请求内容已固定，请等待结果。" : "原请求尚未确认；重试将使用相同请求标识和内容，不会自动重复提交。") + (!command.inFlight ? button("retry", command.stage === "paused" ? "继续原短评请求" : "使用原请求重试") : "") : ""}
      ${selectorsHtml()}${current.reading ? '<p class="tr-loading" role="status">正在核对所选记录…</p>' : ""}
      ${current.task ? `<section class="tr-summary"><h2>${esc(studentName(current.selection.studentId, current.task))}</h2><p>${esc(goal()?.title || "当前目标")} · ${esc(statusText(current.task.status))}${current.task.submission_closed ? " · 后续提交已关闭" : ""}</p>${review ? `<dl><div><dt>提交批次</dt><dd>${esc(attemptText(review.attempt))}</dd></div><div><dt>评分修订</dt><dd>${review.attempt.revision}</dd></div><div><dt>已评分题目</dt><dd>${review.items.filter((item) => item.status === "graded").length} / ${review.items.length}</dd></div><div><dt>本次总分</dt><dd>${review.attempt.status === "graded" && finite(review.attempt.total_score) ? `${review.attempt.total_score} / ${finite(review.attempt.max_score) ? review.attempt.max_score : "—"}` : "尚未全部评分"}</dd></div></dl>${!review.results_released ? '<p class="tr-muted">未评分的题目不按零分计入；学生完整反馈仍以整轮开放状态为准。</p>' : ""}` : '<p class="tr-muted">该任务还没有提交批次。</p>'}</section>` : !current.reading ? '<section class="tr-panel"><h2>尚无可查看的提交</h2><p>选择历史目标和任务；如果列表尚未读全，可继续加载。</p></section>' : ""}
      ${review ? `<div class="tr-questions">${review.items.map(questionHtml).join("")}</div>` : ""}${appealsHtml()}${managementHtml()}`;
  }
  function html() {
    syncContext();
    return `<section class="teacher-review" data-teacher-review aria-label="教师作答复核">${innerHtml()}</section>`;
  }
  function bind(container) {
    syncContext();
    const next = container?.matches?.("[data-teacher-review]") ? container : container?.querySelector?.("[data-teacher-review]");
    root = next || null;
    if (!root) return;
    bindings.set(root, epoch);
    if (listeners.has(root)) return;
    listeners.add(root); const bound = root;
    const eventAllowed = (event) => {
      const boundEpoch = bindings.get(bound); syncContext();
      return root === bound && bound.isConnected !== false && boundEpoch === epoch && active()
        && bound.contains(event.target) && event.target.closest?.("[data-teacher-review]") === bound;
    };
    const update = (event) => {
      if (!eventAllowed(event) || event.target.disabled || current.command || current.reading) return;
      const node = event.target, { reviewScope: scope, reviewId: id, reviewField: name } = node.dataset;
      if (!name || !scope || !manage()) return;
      let draft;
      if (scope === "grade") { const item = itemById(id); if (!item || !mayGrade(item)) return; draft = gradeDraft(item); }
      if (scope === "discussion") { const item = itemById(id); if (!item) return; const thread = selectedThread(item); if (thread && current.threadDetails.get(thread.id)?.can_reply !== true) return; draft = commentDraft(item); }
      if (scope === "manage" && ["deadline", "close-task", "stop-goal"].includes(id)) draft = managementDraft(id);
      if (scope === "appeal") { const detail = current.appealDetails.get(id); if (detail?.can_resolve !== true || detail.status !== "awaiting_teacher") return; draft = appealDraft(id); }
      if (!draft || !["score", "reason", "text", "preset", "deadline", "confirmed"].includes(name)) return;
      draft[name] = name === "confirmed" ? Boolean(node.checked) : node.value;
      draft.dirty = true; if (name !== "confirmed") draft.confirmed = false;
      // Text input remains in place. A preset changes which editor is visible.
      if (name === "preset") paint();
      else if (name !== "confirmed") bound.querySelectorAll('input[data-review-field="confirmed"]').forEach((input) => {
        if (input.dataset.reviewScope === scope && input.dataset.reviewId === id) input.checked = false;
      });
    };
    bound.addEventListener("input", (event) => { if (event.target.type !== "checkbox" && event.target.tagName !== "SELECT") update(event); });
    bound.addEventListener("change", async (event) => {
      if (!eventAllowed(event) || event.target.disabled) return;
      const node = event.target, field = node.dataset.reviewField;
      if (node.dataset.reviewScope) { update(event); return; }
      if (current.command) return;
      if (field === "goal" && node.value) { current.studentFilter = ""; return openSelection({ goalId: node.value }); }
      if (field === "student") {
        current.studentFilter = node.value;
        if (node.value) return openSelection({ goalId: current.selection.goalId, studentId: node.value });
        paint(); return;
      }
      if (field === "task" && node.value) return openSelection({ goalId: current.selection.goalId, taskId: node.value });
      if (field === "attempt" && node.value) return openSelection({ ...current.selection, attemptId: node.value });
      if (field === "thread") return openThread(node.dataset.reviewId, node.value);
    });
    bound.addEventListener("submit", async (event) => {
      const form = event.target.closest?.("[data-review-form]");
      if (!form || !eventAllowed(event)) return;
      event.preventDefault(); event.stopPropagation();
      if (writeLocked()) return;
      const kind = form.dataset.reviewForm, id = form.dataset.reviewId;
      if (kind === "grade") return submitGrade(id);
      if (kind === "discussion") return submitComment(id);
      if (kind === "resolve") return resolveAppeal(id);
      if (["deadline", "close-task", "stop-goal"].includes(kind)) return submitManagement(kind);
    });
    bound.addEventListener("click", async (event) => {
      if (!eventAllowed(event)) return;
      const target = event.target.closest?.("[data-review-action]");
      if (!target || target.disabled || !bound.contains(target)) return;
      event.stopPropagation(); const action = target.dataset.reviewAction, id = target.dataset.reviewId;
      if (action === "retry") return retryCommand();
      if (current.command) return;
      if (action === "refresh") return load({ force: true });
      if (action === "more-goals") return more("goals");
      if (action === "more-tasks") return more("tasks");
      if (action === "appeal") return openAppeal(id);
      if (action === "appeal-attempt") { const appeal = current.appeals.find((entry) => entry.id === id); if (appeal) return openSelection({ ...current.selection, attemptId: appeal.attempt_id }); }
      if (action === "reports" && current.selection.goalId && typeof onReports === "function") return onReports({ ...current.selection });
    });
  }
  return { render: html, bind, load, reset, leave, openSelection };
}
