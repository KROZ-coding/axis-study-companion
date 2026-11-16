const PAGE_SIZE = 100;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const encode = (value) => encodeURIComponent(String(value));
const hasId = (value) => typeof value === "string" && value.trim().length > 0;
const finiteNumber = (value) => typeof value === "number" && Number.isFinite(value);
const normalizeVersion = (value) => String(value ?? "").replace("@", ":");
const questionVersion = (question) => normalizeVersion(question?.question_version_id
  || (hasId(question?.id) && Number.isInteger(question?.version) ? `${question.id}:${question.version}` : question?.id || ""));
const taskStatusLabel = {
  not_started: "待开始", in_progress: "作答中", submitted: "已交卷", grading: "正在评卷",
  awaiting_manual: "等待教师评分", results_ready: "评分完成", closed: "已结束",
};
const attemptKindLabel = { first: "首次作答", retry: "重做", practice: "练习" };
const appealStatusLabel = {
  pending: "处理中", awaiting_teacher: "等待教师处理", approved: "已支持", accepted: "已支持",
  rejected: "未支持", declined: "未支持", resolved: "已处理", closed: "已结束",
};
const emptySelection = () => ({ taskId: "", attemptId: "", threadId: "", questionVersionId: "" });
const rowsOf = (value) => {
  const rows = Array.isArray(value) ? value : value?.items;
  if (!Array.isArray(rows)) throw new Error("记录暂未读全，请刷新重试。");
  return rows;
};
const userFor = (api, state) => api?.state && Object.prototype.hasOwnProperty.call(api.state, "user")
  ? api.state.user : state?.user;
const taskNavigation = (task) => ({ id: task.id, goal_id: task.goal_id, title: task.title, goal_title: task.goal_title, name: task.name, status: task.status });
function taskTitle(task, goals) {
  // Explicit inputs keep catalog and detail rendering independent of factory or
  // host globals. goals contains only the workspace's already-loaded projection.
  const goal = hasId(task?.goal_id) && Array.isArray(goals)
    ? goals.find((item) => item.id === task.goal_id) : null;
  return task?.title || task?.goal_title || task?.name || goal?.title || `学习任务 · ${String(task?.id || "").slice(0, 8)}`;
}
const scoreText = (score, max) => finiteNumber(score) ? `${score}${finiteNumber(max) ? ` / ${max}` : ""} 分` : "待评分";
const appealKey = (attemptId, questionId, revision) => JSON.stringify([attemptId, questionId, Number(revision)]);
function requestId(prefix) {
  return `${prefix}-${globalThis.crypto?.randomUUID?.() || `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`}`;
}
function answerMissing(value) {
  return value === undefined || value === null || (typeof value === "string" && !value.trim())
    || (Array.isArray(value) && (!value.length || Array.from(value).some(answerMissing)));
}
function answerText(question, value) {
  // Match the host's questionAnswerText using this task's frozen question only.
  const type = question?.type || (question?.options?.length ? "single_choice" : "text");
  const count = question?.blank_count ?? question?.answer_format?.blank_count;
  const blanks = Number.isInteger(count) && count > 0 && count <= 32 ? count : 1;
  if (type === "fill" && blanks > 1 && Array.isArray(value)) {
    return Array.from({ length: blanks }, (_, index) => `第 ${index + 1} 空：${answerMissing(value[index]) ? "未作答" : String(value[index])}`).join("；");
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
function resultText(value, depth = 0) {
  if (depth > 5 || value === null || value === undefined) return "";
  if (typeof value !== "object") return String(value);
  if (Array.isArray(value)) return value.map((part) => resultText(part, depth + 1)).filter(Boolean).join("；");
  const labels = { review: "复核意见", reason: "说明", message: "说明", text: "说明", decision: "结论", conclusion: "结论", explanation: "解释", feedback: "反馈", comment: "意见", score: "建议分数", suggested_score: "建议分数" };
  return Object.entries(labels).filter(([key]) => Object.prototype.hasOwnProperty.call(value, key))
    .map(([key, label]) => { const text = resultText(value[key], depth + 1); return text ? `${label}：${text}` : ""; }).filter(Boolean).join("\n");
}

export function createStudentFeedback({ api, state, render: renderApp = () => {}, toast = () => {}, onChanged } = {}) {
  let context = "", epoch = 0, readSequence = 0, root = null, controller = null;
  let focusSnapshot = null, composing = false, repaintQueued = false, away = false;
  let current = fresh();
  const bindings = new WeakMap();
  function fresh() {
    return {
      tasks: [], listed: false, task: null, threads: [], appeals: [], selection: emptySelection(),
      loading: false, error: "", message: "",
      replyDrafts: new Map(), replyPending: new Map(), replyErrors: new Map(),
      appealDrafts: new Map(), appealPending: new Map(), appealErrors: new Map(), confirmedAppeals: new Map(),
    };
  }
  function sessionKey() {
    const user = userFor(api, state);
    // loggingOut only pauses interaction; a failed logout retains command receipts.
    return JSON.stringify([state?.live, state?.role, state?.transitionId, state?.courseId, state?.classId, user?.id, user?.role, user?.status]);
  }
  function identityActive() {
    const user = userFor(api, state);
    return state?.live === true && state?.role === "student" && hasId(user?.id)
      && user.role === "student" && user.status === "active";
  }
  const active = () => identityActive() && !state?.loggingOut;
  const sessionCurrent = (token, data) => token === epoch && data === current && context === sessionKey() && identityActive();
  const visible = () => !away && (state?.studentTab === undefined || state.studentTab === "feedback");
  function cancelReads() {
    readSequence += 1;
    controller?.abort();
    controller = null;
    current.loading = false;
  }
  function syncContext() {
    if (context !== sessionKey()) {
      cancelReads();
      context = sessionKey();
      epoch += 1;
      current = fresh();
      focusSnapshot = null;
      composing = repaintQueued = false;
    }
    return epoch;
  }
  function reset() {
    cancelReads();
    epoch += 1;
    context = "";
    current = fresh();
    focusSnapshot = null;
    composing = repaintQueued = false;
    away = false;
    if (root) root.innerHTML = "";
    root = null;
  }
  const pendingWrites = () => current.replyPending.size > 0 || current.appealPending.size > 0;
  function blockPending() {
    if (!pendingWrites()) return false;
    current.error = "提交结果尚未确认，请先重试确认；输入内容已保留。";
    repaint();
    return true;
  }
  function leave() {
    syncContext();
    if (blockPending()) {
      if (active()) toast(current.error);
      return false;
    }
    cancelReads();
    away = true;
    return true;
  }
  function focusIdentity(node) {
    return node?.getAttribute ? [node.tagName, ...["id", "data-feedback-action", "data-feedback-task-id", "data-feedback-attempt-id", "data-feedback-thread-id", "data-feedback-field"].map((key) => node.getAttribute(key))] : null;
  }
  function captureView() {
    const binding = root && bindings.get(root);
    if (!binding || binding.token !== epoch || binding.data !== current || root.isConnected === false) return;
    for (const field of root.querySelectorAll?.("textarea") || []) saveInput(field);
    const focused = root.ownerDocument?.activeElement;
    focusSnapshot = root.contains?.(focused) ? { identity: focusIdentity(focused), start: focused.selectionStart, end: focused.selectionEnd } : null;
  }
  function restoreFocus() {
    if (!focusSnapshot || !root) return;
    const target = Array.from(root.querySelectorAll?.("button, textarea") || []).find((node) => JSON.stringify(focusIdentity(node)) === JSON.stringify(focusSnapshot.identity));
    if (target && !target.disabled) {
      target.focus?.({ preventScroll: true });
      if (typeof focusSnapshot.start === "number" && target.setSelectionRange) {
        try { target.setSelectionRange(focusSnapshot.start, focusSnapshot.end); } catch { /* A button has no text selection. */ }
      }
    }
  }
  function repaint() {
    if (context !== sessionKey() || !active() || !visible()) return;
    if (composing) { repaintQueued = true; return; }
    captureView();
    if (root && root.isConnected !== false) {
      const scrollTop = root.scrollTop;
      root.innerHTML = contentHtml();
      root.scrollTop = scrollTop;
      bind(root);
    } else renderApp();
    restoreFocus();
  }
  async function call(method, path, body, options) {
    if (typeof api?.call !== "function") throw new Error("正式服务接口未配置");
    return api.call(method, path, body, options);
  }
  const readCurrent = (read) => sessionCurrent(read.token, read.data) && read.sequence === readSequence && !read.signal.aborted;
  function assertRead(read, starting = false) {
    if (!readCurrent(read) || (starting && !active())) throw new Error("读取已结束");
  }
  async function get(path, read) {
    assertRead(read, true);
    const result = await call("GET", path, undefined, { signal: read.signal });
    assertRead(read);
    return result;
  }
  async function allPages(path, read) {
    const rows = [], seen = new Set();
    for (let offset = 0; ; offset += PAGE_SIZE) {
      const chunk = rowsOf(await get(`${path}${path.includes("?") ? "&" : "?"}limit=${PAGE_SIZE}&offset=${offset}`, read));
      let added = 0;
      for (const row of chunk) {
        if (!hasId(row?.id)) throw new Error("记录标识不完整，请刷新重试。");
        if (!seen.has(row.id)) { seen.add(row.id); rows.push(row); added += 1; }
      }
      if (chunk.length < PAGE_SIZE) return rows;
      if (!added) throw new Error("记录分页未能前进，请稍后刷新。");
    }
  }
  function ownTask(task, id) {
    return task?.id === id && task?.student_id === userFor(api, state)?.id;
  }
  function ownThread(thread, id, taskId) {
    return thread?.id === id && thread?.student_id === userFor(api, state)?.id && hasId(thread?.task_id)
      && (!taskId || thread.task_id === taskId) && hasId(thread?.attempt_id) && hasId(thread?.question_version_id);
  }
  function attemptFor(task = current.task, id = current.selection.attemptId) {
    const attempts = Array.isArray(task?.attempts) ? task.attempts : [];
    return id ? attempts.find((attempt) => attempt.id === id) || null : attempts.at(-1) || null;
  }
  function matchingThreads(question) {
    return current.threads.filter((thread) => thread.task_id === current.task?.id && thread.attempt_id === current.selection.attemptId
      && (!question || normalizeVersion(thread.question_version_id) === questionVersion(question)));
  }
  function clearProjection() {
    current.task = null;
    current.threads = [];
    current.appeals = [];
  }
  async function readSelection(selection, read) {
    let chosenThread = null;
    if (selection.threadId) {
      chosenThread = await get(`/companion/discussion-threads/${encode(selection.threadId)}`, read);
      if (!ownThread(chosenThread, selection.threadId, selection.taskId)) throw new Error("讨论记录与所选任务不匹配。");
      if ((selection.attemptId && chosenThread.attempt_id !== selection.attemptId)
        || (selection.questionVersionId && normalizeVersion(chosenThread.question_version_id) !== normalizeVersion(selection.questionVersionId))) throw new Error("讨论记录与所选作答或题目不匹配。");
      selection = { ...selection, taskId: chosenThread.task_id, attemptId: chosenThread.attempt_id, questionVersionId: normalizeVersion(chosenThread.question_version_id) };
    }
    if (!selection.taskId) return !selection.attemptId && !selection.questionVersionId;
    const [task, threadRows, appealRows] = await Promise.all([
      get(`/tasks/${encode(selection.taskId)}`, read),
      allPages(`/companion/discussion-threads?task_id=${encode(selection.taskId)}`, read),
      get(`/tasks/${encode(selection.taskId)}/appeals`, read).then(rowsOf),
    ]);
    if (!ownTask(task, selection.taskId)) throw new Error("当前账号无法查看这份任务。");
    const attempt = attemptFor(task, selection.attemptId);
    if (selection.attemptId && !attempt) throw new Error("所选作答记录暂时不可用，请重新选择。");
    selection = { ...selection, attemptId: attempt?.id || "" };
    const candidates = threadRows.filter((thread) => ownThread(thread, thread.id, task.id) && thread.attempt_id === selection.attemptId);
    if (chosenThread && !candidates.some((row) => row.id === chosenThread.id)) candidates.push(chosenThread);
    const threads = await Promise.all(candidates.map(async (row) => {
      const detail = row.id === chosenThread?.id ? chosenThread : await get(`/companion/discussion-threads/${encode(row.id)}`, read);
      if (!ownThread(detail, row.id, task.id) || detail.attempt_id !== selection.attemptId || !Array.isArray(detail.messages)) throw new Error("讨论详情暂未读全，请刷新重试。");
      // Never merge a list row or previous detail into a new server projection.
      return detail;
    }));
    assertRead(read);
    if (selection.questionVersionId && !(task.questions || []).some((question) => questionVersion(question) === normalizeVersion(selection.questionVersionId))
      && !threads.some((thread) => normalizeVersion(thread.question_version_id) === normalizeVersion(selection.questionVersionId))) throw new Error("所选题目版本暂时不可用。");
    const attempts = new Set((task.attempts || []).map((item) => item.id));
    read.data.task = task;
    read.data.selection = selection;
    read.data.threads = threads;
    read.data.appeals = appealRows.filter((item) => attempts.has(item.attempt_id) && (!item.student_id || item.student_id === userFor(api, state).id));
    return true;
  }
  async function refresh(selection, includeList) {
    cancelReads();
    controller = new AbortController();
    const read = { token: epoch, data: current, sequence: readSequence, signal: controller.signal };
    const data = current;
    clearProjection();
    data.loading = true;
    data.error = "";
    data.selection = { ...selection };
    if (includeList) { data.tasks = []; data.listed = false; }
    repaint();
    try {
      if (includeList) {
        const rows = await allPages("/tasks", read);
        // The list supplies navigation only. Answers, grades and attempts come from GET /tasks/{id}.
        data.tasks = rows.filter((row) => row.student_id === userFor(api, state).id).map(taskNavigation);
        data.listed = true;
      }
      if (!active()) return false;
      if (!selection.taskId && !selection.threadId && !selection.attemptId && !selection.questionVersionId && data.tasks.length) selection = { ...selection, taskId: data.tasks[0].id };
      const success = await readSelection(selection, read);
      if (!readCurrent(read)) return false;
      if (data.task && !data.tasks.some((row) => row.id === data.task.id)) data.tasks.push(taskNavigation(data.task));
      return success && active();
    } catch (error) {
      if (readCurrent(read)) {
        clearProjection();
        data.error = error?.message || "反馈暂时无法读取，请刷新重试。";
      }
      return false;
    } finally {
      if (readCurrent(read)) { data.loading = false; repaint(); }
    }
  }
  async function load() {
    syncContext();
    if (!active() || blockPending()) return false;
    away = false;
    return refresh({ ...current.selection }, true);
  }
  async function openSelection(selection = {}) {
    syncContext();
    if (!active() || blockPending()) return false;
    away = false;
    const next = Object.fromEntries(Object.keys(emptySelection()).map((key) => [key, hasId(selection[key]) ? selection[key] : ""]));
    current.message = "";
    return refresh(next, !current.listed);
  }
  const writeCurrent = (write) => sessionCurrent(write.token, write.data);
  const sameSelection = (write) => writeCurrent(write) && JSON.stringify(write.selection) === JSON.stringify(current.selection);
  async function afterWrite(write, message) {
    if (!sameSelection(write)) return;
    current.message = message;
    if (!active() || !visible()) return;
    // Confirmed commands only trigger GET refreshes. Read failures never replay a POST.
    await refresh({ ...write.selection }, false);
    if (sameSelection(write) && active() && visible() && typeof onChanged === "function") {
      try { await onChanged({ ...write.selection }); } catch { /* Notification refresh is independent of the confirmed write. */ }
    }
  }
  async function sendReply(threadId, suppliedText) {
    syncContext();
    if (!active() || away || current.loading) return false;
    const thread = current.threads.find((item) => item.id === threadId && item.task_id === current.selection.taskId && item.attempt_id === current.selection.attemptId);
    if (!thread || thread.can_reply !== true) return false;
    const old = current.replyPending.get(threadId);
    if (old?.inFlight || (!old && pendingWrites())) return false;
    const text = old?.payload.text ?? String(suppliedText ?? current.replyDrafts.get(threadId) ?? "");
    if (!text.trim() || text.length > 4000) {
      current.replyErrors.set(threadId, "请填写 1–4000 字的回复。"); repaint(); return false;
    }
    const pending = old || { payload: { request_id: requestId("feedback-message"), text }, inFlight: false };
    pending.inFlight = true;
    current.replyPending.set(threadId, pending);
    current.replyDrafts.set(threadId, text);
    current.replyErrors.delete(threadId);
    current.error = "";
    const write = { token: epoch, data: current, selection: { ...current.selection } };
    repaint();
    try {
      const result = await call("POST", `/companion/discussion-threads/${encode(threadId)}/messages`, { ...pending.payload });
      if (!writeCurrent(write)) return false;
      if (!hasId(result?.id) || result.thread_id !== threadId || result.author_id !== userFor(api, state).id || result.author_role !== "student") throw new Error("发送结果尚未确认，请用同一请求重试。");
      current.replyPending.delete(threadId);
      current.replyDrafts.delete(threadId);
      current.replyErrors.delete(threadId);
      await afterWrite(write, "回复已发送。");
      return true;
    } catch (error) {
      if (writeCurrent(write)) {
        pending.inFlight = false;
        current.replyErrors.set(threadId, error?.message || "发送结果尚未确认，草稿已保留。请重试确认。");
        if (sameSelection(write)) repaint();
      }
      return false;
    }
  }
  function appealFor(taskId, attemptId, questionId, revision) {
    return current.task?.id === taskId ? current.appeals.find((row) => row.attempt_id === attemptId && row.question_id === questionId && Number(row.grading_revision) === Number(revision)) : null;
  }
  async function submitAppeal({ taskId, attemptId, questionId, gradingRevision, reason } = {}) {
    syncContext();
    if (!active() || away || current.loading) return false;
    const task = current.task, revision = Number(gradingRevision), attempt = attemptFor(task, attemptId);
    const key = appealKey(attemptId, questionId, revision);
    if (!ownTask(task, taskId) || taskId !== current.selection.taskId || attemptId !== current.selection.attemptId
      || task.status !== "results_ready" || !attempt || !Number.isInteger(revision) || revision < 1 || Number(attempt.revision) !== revision
      || !(task.questions || []).some((question) => question.id === questionId)
      || !(attempt.items || []).some((item) => item.question_id === questionId)
      || appealFor(taskId, attemptId, questionId, revision) || current.confirmedAppeals.has(key)) return false;
    const old = current.appealPending.get(key);
    if (old?.inFlight || (!old && pendingWrites())) return false;
    const text = old?.payload.reason ?? String(reason ?? current.appealDrafts.get(key) ?? "");
    if (!text.trim() || text.length > 2000) {
      current.appealErrors.set(key, "请填写 1–2000 字的复核原因。"); repaint(); return false;
    }
    const pending = old || { payload: { request_id: requestId("feedback-appeal"), question_id: questionId, grading_revision: revision, reason: text }, inFlight: false };
    pending.inFlight = true;
    current.appealPending.set(key, pending);
    current.appealDrafts.set(key, text);
    current.appealErrors.delete(key);
    current.error = "";
    const write = { token: epoch, data: current, selection: { ...current.selection } };
    repaint();
    try {
      const result = await call("POST", `/attempts/${encode(attemptId)}/appeals`, { ...pending.payload });
      if (!writeCurrent(write)) return false;
      if (!hasId(result?.id)) throw new Error("提交结果尚未确认，请用同一请求重试。");
      current.confirmedAppeals.set(key, result.id);
      current.appealPending.delete(key);
      current.appealDrafts.delete(key);
      current.appealErrors.delete(key);
      await afterWrite(write, "评分异议已提交，处理结果会显示在题目下方。");
      return true;
    } catch (error) {
      if (writeCurrent(write)) {
        pending.inFlight = false;
        current.appealErrors.set(key, error?.message || "提交结果尚未确认，原因已保留。请重试确认。");
        if (sameSelection(write)) repaint();
      }
      return false;
    }
  }
  function messageHtml(message) {
    const hidden = message?.redacted === true || message?.delivery === "deferred";
    if (hidden) return `<p class="feedback-message-redacted">${esc(message.notice || "教师短评将在本题结果开放后显示。")}</p>`;
    return `<p>${esc(message?.text || "（空消息）")}</p>`;
  }
  function threadHtml(thread) {
    const pending = current.replyPending.get(thread.id);
    const draft = pending?.payload.text ?? current.replyDrafts.get(thread.id) ?? "";
    const error = current.replyErrors.get(thread.id);
    const selected = current.selection.threadId === thread.id;
    const disabled = !active() || current.loading || pending?.inFlight || (!pending && pendingWrites());
    return `<article class="feedback-thread${selected ? " is-selected" : ""}" data-feedback-thread="${esc(thread.id)}"><header><div><strong>${esc(thread.title || "教师短评")}</strong><small>题目 ${esc(thread.question_version_id)}</small></div>${selected ? '<span class="feedback-badge">当前定位</span>' : ""}</header><div class="feedback-messages">${thread.messages.length ? thread.messages.map((message) => `<div class="feedback-message${message.author_role === "student" ? " is-student" : ""}"><strong>${message.author_role === "student" ? "我" : "教师"}</strong>${messageHtml(message)}</div>`).join("") : '<p class="feedback-muted">这条讨论还没有消息。</p>'}</div>${thread.can_reply === true ? `<form class="feedback-reply" data-feedback-form="reply" data-feedback-thread-id="${esc(thread.id)}"><label for="feedback-reply-${esc(thread.id)}">回复教师</label><textarea id="feedback-reply-${esc(thread.id)}" data-feedback-field="reply" data-feedback-thread-id="${esc(thread.id)}" maxlength="4000" rows="3" ${pending || !active() ? "readonly" : ""} placeholder="写下你的问题或尝试">${esc(draft)}</textarea>${error ? `<p class="feedback-error" role="alert">${esc(error)}</p>` : ""}<button type="submit" class="feedback-button primary" ${disabled ? "disabled" : ""}>${pending?.inFlight ? "发送中…" : pending ? "重试确认发送" : "发送回复"}</button>${pending && !pending.inFlight ? '<small class="feedback-help">重试将确认这次发送，已保留的内容不会重复创建消息。</small>' : ""}</form>` : '<p class="feedback-muted">当前讨论只可阅读。</p>'}</article>`;
  }
  function appealHtml(task, attempt, question) {
    if (!attempt || !hasId(question.id) || !Number.isInteger(attempt.revision) || attempt.revision < 1) return "";
    const key = appealKey(attempt.id, question.id, attempt.revision);
    const existing = appealFor(task.id, attempt.id, question.id, attempt.revision);
    if (existing) {
      const hidden = existing.redacted === true;
      const result = hidden ? "" : resultText(existing.result);
      return `<div class="feedback-appeal-result"><strong>${esc(appealStatusLabel[existing.status] || "已提交")}</strong>${!hidden && existing.reason ? `<p>你的说明：${esc(existing.reason)}</p>` : ""}<p>${hidden ? esc(existing.notice || "复核结果将在开放后显示。") : result ? `处理结果：${esc(result)}` : "处理结果暂未返回。"}</p>${existing.status === "awaiting_teacher" ? '<p>复核意见等待教师确认，正式成绩以已发布记录为准。</p>' : ""}</div>`;
    }
    if (current.confirmedAppeals.has(key)) return '<div class="feedback-appeal-result">异议已提交。刷新后查看处理状态。</div>';
    if (task.status !== "results_ready" || !(attempt.items || []).some((item) => item.question_id === question.id)) return "";
    const pending = current.appealPending.get(key), error = current.appealErrors.get(key);
    const reason = pending?.payload.reason ?? current.appealDrafts.get(key) ?? "";
    const disabled = !active() || current.loading || pending?.inFlight || (!pending && pendingWrites());
    const fieldId = `feedback-appeal-${encode(key)}`;
    return `<form class="feedback-appeal" data-feedback-form="appeal" data-feedback-task-id="${esc(task.id)}" data-feedback-attempt-id="${esc(attempt.id)}" data-feedback-question-id="${esc(question.id)}" data-feedback-revision="${attempt.revision}"><label for="${esc(fieldId)}">评分异议说明</label><textarea id="${esc(fieldId)}" data-feedback-field="appeal-reason" data-feedback-appeal-key="${esc(key)}" maxlength="2000" rows="3" ${pending || !active() ? "readonly" : ""} placeholder="请说明希望教师复核的地方">${esc(reason)}</textarea>${error ? `<p class="feedback-error" role="alert">${esc(error)}</p>` : ""}<button type="submit" class="feedback-button" ${disabled ? "disabled" : ""}>${pending?.inFlight ? "提交中…" : pending ? "重试确认提交" : "提交评分异议"}</button>${pending && !pending.inFlight ? '<small class="feedback-help">重试将确认这次提交，已保留的原因不会重复创建异议。</small>' : ""}</form>`;
  }
  function questionHtml(task, attempt, question, index) {
    const version = questionVersion(question), threads = matchingThreads(question);
    const released = task.answer_released === true;
    const score = released ? attempt?.items?.find((item) => item.question_id === question.id)?.score : undefined;
    const selected = normalizeVersion(current.selection.questionVersionId) === version;
    const answer = released && Object.prototype.hasOwnProperty.call(attempt?.answers || {}, question.id)
      ? `<p class="feedback-muted">你的作答：${esc(answerText(question, attempt.answers[question.id]))}</p>` : "";
    return `<section class="feedback-question${selected ? " is-selected" : ""}" data-feedback-question-version-id="${esc(version)}"><header><div><span class="feedback-kicker">第 ${index + 1} 题${selected ? " · 当前定位" : ""}</span><h3>${esc(question.stem || "题目内容暂未提供")}</h3></div><span class="feedback-score">${esc(scoreText(score, question.points))}</span></header>${answer}${released && question.explanation ? `<p>解析：${esc(question.explanation)}</p>` : ""}${threads.length ? `<div class="feedback-thread-list">${threads.map(threadHtml).join("")}</div>` : '<p class="feedback-muted">这道题还没有教师短评。</p>'}${appealHtml(task, attempt, question)}</section>`;
  }
  function taskListHtml() {
    if (!current.tasks.length) return `<p class="feedback-muted">${current.loading ? "正在读取当前和历史任务…" : "还没有可查看的任务反馈。"}</p>`;
    const disabled = !active() || pendingWrites();
    return `<div class="feedback-task-list">${current.tasks.map((row) => {
      const selected = current.selection.taskId === row.id;
      const task = selected && current.task?.id === row.id ? current.task : row;
      const attempts = selected ? task.attempts || [] : [];
      return `<article class="feedback-task-row${selected ? " is-selected" : ""}"><button type="button" class="feedback-task-button" data-feedback-action="select-task" data-feedback-task-id="${esc(row.id)}" aria-current="${selected ? "page" : "false"}" ${disabled ? "disabled" : ""}><strong>${esc(taskTitle(task, state?.liveGoals))}</strong><span>${esc(taskStatusLabel[task.status] || "状态未提供")}</span></button>${attempts.length ? `<div class="feedback-attempts" aria-label="作答记录">${attempts.map((attempt) => `<button type="button" class="feedback-attempt-button${current.selection.attemptId === attempt.id ? " is-selected" : ""}" data-feedback-action="select-attempt" data-feedback-task-id="${esc(row.id)}" data-feedback-attempt-id="${esc(attempt.id)}" aria-pressed="${current.selection.attemptId === attempt.id}" ${disabled ? "disabled" : ""}><span>${esc(attemptKindLabel[attempt.kind] || "作答记录")} ${esc(attempt.attempt_no || "")}</span><strong>${esc(scoreText(attempt.total_score, attempt.max_score))}</strong></button>`).join("")}</div>` : ""}</article>`;
    }).join("")}</div>`;
  }
  function detailHtml() {
    const task = current.task;
    if (!task) return `<div class="feedback-empty"><h2>${current.loading ? "正在读取反馈…" : "选择一份任务"}</h2><p>选择当前或历史任务，查看对应作答、教师短评和评分异议。</p></div>`;
    const attempt = attemptFor(), questions = Array.isArray(task.questions) ? task.questions : [];
    const otherThreads = matchingThreads().filter((thread) => !questions.some((question) => questionVersion(question) === normalizeVersion(thread.question_version_id)));
    return `<header class="feedback-detail-head"><div><p class="feedback-kicker">${esc(taskStatusLabel[task.status] || "学习任务")}</p><h2>${esc(taskTitle(task, state?.liveGoals))}</h2><p>${attempt ? `${esc(attemptKindLabel[attempt.kind] || "作答记录")} · ${esc(scoreText(attempt.total_score, attempt.max_score))}` : "尚无作答记录"}</p></div></header>${task.answer_released !== true ? '<div class="feedback-policy" role="status">本题结果尚未开放，解析和教师短评会在开放后显示。</div>' : ""}${questions.map((question, index) => questionHtml(task, attempt, question, index)).join("")}${otherThreads.map(threadHtml).join("")}${!questions.length && !otherThreads.length ? '<p class="feedback-muted">暂时没有可展示的题目反馈。</p>' : ""}`;
  }
  function contentHtml() {
    if (!identityActive()) return '<div class="feedback-empty"><h2>反馈与讨论</h2><p>请连接正式服务并使用有效学生账号登录。</p></div>';
    return `<header class="feedback-header"><div><h1>反馈与讨论</h1><p>查看任务反馈，回复教师短评，或对已开放成绩提出异议。</p></div><button type="button" class="feedback-button" data-feedback-action="refresh" ${current.loading || pendingWrites() || !active() ? "disabled" : ""}>${current.loading ? "同步中…" : "刷新"}</button></header>${current.error ? `<div class="feedback-error-banner" role="alert">${esc(current.error)}</div>` : ""}${current.message ? `<div class="feedback-success" role="status">${esc(current.message)}</div>` : ""}<div class="feedback-layout"><section class="feedback-tasks"><div class="feedback-section-heading"><h2>任务与作答</h2><small>当前与历史记录</small></div>${taskListHtml()}</section><section class="feedback-detail" aria-busy="${current.loading}">${detailHtml()}</section></div>`;
  }
  function renderFeedback() {
    syncContext();
    captureView();
    return `<section class="learning-feedback student-feedback" data-student-feedback>${contentHtml()}</section>`;
  }
  function saveInput(field) {
    if (field.disabled || field.readOnly) return;
    if (field.dataset?.feedbackField === "reply" && !current.replyPending.has(field.dataset.feedbackThreadId)) current.replyDrafts.set(field.dataset.feedbackThreadId, field.value);
    if (field.dataset?.feedbackField === "appeal-reason" && !current.appealPending.has(field.dataset.feedbackAppealKey)) current.appealDrafts.set(field.dataset.feedbackAppealKey, field.value);
  }
  function bind(container) {
    const surface = container?.matches?.("[data-student-feedback]") ? container : container?.querySelector?.("[data-student-feedback]");
    if (!surface) return;
    root = surface;
    const previous = bindings.get(surface);
    bindings.set(surface, { token: epoch, data: current });
    if (previous) { restoreFocus(); return; }
    const allowed = (event) => {
      const binding = bindings.get(surface);
      return surface === root && surface.isConnected !== false && surface.contains(event.target)
        && sessionCurrent(binding.token, binding.data) && active() && !away;
    };
    surface.addEventListener("click", (event) => {
      if (!allowed(event)) return;
      const target = event.target.closest?.("[data-feedback-action]");
      if (!target || target.disabled) return;
      if (target.dataset.feedbackAction === "refresh") return load();
      if (target.dataset.feedbackAction === "select-task") return openSelection({ taskId: target.dataset.feedbackTaskId });
      if (target.dataset.feedbackAction === "select-attempt") return openSelection({ taskId: target.dataset.feedbackTaskId, attemptId: target.dataset.feedbackAttemptId });
    });
    surface.addEventListener("input", (event) => { if (allowed(event)) saveInput(event.target); });
    surface.addEventListener("compositionstart", (event) => { if (allowed(event)) composing = true; });
    surface.addEventListener("compositionend", (event) => {
      if (!allowed(event)) return;
      saveInput(event.target);
      composing = false;
      if (repaintQueued) { repaintQueued = false; repaint(); }
    });
    surface.addEventListener("submit", (event) => {
      const form = event.target.closest?.("[data-feedback-form]");
      if (!form || !surface.contains(form)) return;
      event.preventDefault();
      if (!allowed(event)) return;
      const field = form.querySelector("textarea");
      if (form.dataset.feedbackForm === "reply") return sendReply(form.dataset.feedbackThreadId, field?.value);
      if (form.dataset.feedbackForm === "appeal") return submitAppeal({ taskId: form.dataset.feedbackTaskId, attemptId: form.dataset.feedbackAttemptId, questionId: form.dataset.feedbackQuestionId, gradingRevision: form.dataset.feedbackRevision, reason: field?.value });
    });
    restoreFocus();
  }
  return { render: renderFeedback, bind, load, reset, leave, openSelection, sendReply, submitAppeal };
}
