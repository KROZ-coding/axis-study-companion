// 前端统一通过该模块访问正式 API；无法连接时会明确禁止认证操作。
// 启动时探测 /health，返回 mode: platform 才启用账号与业务请求。

const API_BASE = "/api/v1";

const state = {
  live: false,        // 后端是否可用
  mode: "sample",     // "live" | "sample"
  user: null,         // { id, username, display_name, role }
  courseId: null,
  classId: null,
  lastError: "",
};

/** 同源写请求必须带 Origin，后端据此拒绝跨站提交。 */
function originHeaders() {
  const headers = { Accept: "application/json" };
  if (location.protocol === "http:" || location.protocol === "https:") {
    headers.Origin = location.origin;
  }
  return headers;
}

class ApiError extends Error {
  constructor(status, code, message, retryable = false) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryable = retryable;
  }
}

/** 把后端各种错误体统一成 ApiError，渲染层只需要看 message / retryable。 */
function toApiError(status, payload) {
  const detail = payload && payload.detail;
  if (typeof detail === "string") return new ApiError(status, "error", detail);
  if (detail && typeof detail === "object" && !Array.isArray(detail)) {
    return new ApiError(status, detail.code || "error", detail.message || "请求未完成", Boolean(detail.retryable));
  }
  if (Array.isArray(detail)) {
    const first = detail[0] || {};
    return new ApiError(status, "invalid_input", first.msg || "提交的内容不完整", false);
  }
  return new ApiError(status, "error", "请求未完成", status >= 500);
}

async function request(method, path, { body, expect = 200, extraHeaders = {}, signal } = {}) {
  // 调用方一律传 /auth/login 这样的相对路径，统一补上 /api/v1。
  // 已经带前缀的（比如导出链接）原样使用。
  const url = path.startsWith("/api/") ? path : `${API_BASE}${path}`;
  const options = {
    method,
    credentials: "same-origin",
    headers: { ...originHeaders(), ...extraHeaders },
    ...(signal ? { signal } : {}),
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
    if (signal?.aborted || error?.name === "AbortError") throw error;
    throw new ApiError(0, "network_unavailable", "连不上服务，请确认后端已启动", true);
  }
  const text = await response.text();
  let payload = null;
  if (text) {
    try { payload = JSON.parse(text); } catch { payload = null; }
  }
  if (!response.ok) throw toApiError(response.status, payload);
  if (expect && response.status !== expect && !(expect === 200 && response.status === 200)) {
    // 201/202 也是成功，不在这里报错
  }
  return { status: response.status, data: payload, headers: response.headers };
}

const get = (path, options) => request("GET", path, options).then((r) => r.data);
const post = (path, body, options = {}) => request("POST", path, { ...options, body }).then((r) => r.data);
const put = (path, body, options = {}) => request("PUT", path, { ...options, body }).then((r) => r.data);
const call = (method, path, body, options = {}) => request(method, path, { ...options, body }).then((result) => result.data);

async function upload(path, formData) {
  const url = path.startsWith("/api/") ? path : `${API_BASE}${path}`;
  let response;
  try {
    response = await fetch(url, { method: "POST", credentials: "same-origin", headers: originHeaders(), body: formData });
  } catch {
    throw new ApiError(0, "network_unavailable", "上传未完成，请确认服务可用后重试", true);
  }
  let payload = null;
  try { payload = await response.json(); } catch { /* Empty successful responses carry no body. */ }
  if (!response.ok) throw toApiError(response.status, payload);
  return payload;
}

/** 探一次后端。失败就当成"没有后端"，不让整个原型崩掉。 */
async function probe() {
  try {
    const response = await fetch("/health", { credentials: "same-origin" });
    if (!response.ok) {
      state.live = false;
      state.mode = "sample";
      state.lastError = `正式服务暂不可用（${response.status}）`;
      return { live: false, reason: state.lastError };
    }
    const data = await response.json();
    const live = data && data.mode === "platform";
    state.live = live;
    state.mode = live ? "live" : "sample";
    state.lastError = live ? "" : "当前服务未启用正式账号认证";
    return { live, health: data, reason: state.lastError };
  } catch {
    state.live = false;
    state.mode = "sample";
    state.lastError = "无法连接正式服务，请检查网络或稍后重试";
    return { live: false, reason: state.lastError };
  }
}

/** 已登录则返回当前用户，否则 null。
 *  刷新页面时没有会话是正常情况，用静默请求，不让它变成控制台错误。 */
async function currentUser() {
  if (!state.live) return null;
  try {
    const response = await fetch(`${API_BASE}/auth/me`, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (response.status === 401) {
      state.user = null;
      return null;
    }
    if (!response.ok) return null;
    const user = await response.json();
    state.user = user;
    return user;
  } catch {
    state.user = null;
    return null;
  }
}

async function login(username, password, role) {
  const data = await post("/auth/login", { username: username.trim(), password, ...(role ? { role } : {}) });
  state.user = data.user;
  return data.user;
}

const applyTeacherAccount = (payload) => post("/auth/teacher-applications", payload, { expect: 202 });
const activateAccount = (code, password) => post("/auth/activate", { code, password }, { expect: 204 });
const resetPassword = (code, password) => post("/auth/password-reset", { code, password }, { expect: 204 });

async function logout() {
  const user = state.user;
  await post("/auth/logout", undefined, { expect: 204 });
  if (state.user === user) state.user = null;
}

/** 教师端：拿到自己带得动的课程与班级。 */
async function teacherClasses() {
  const [courseData, classData] = await Promise.all([get("/courses"), get("/classes")]);
  const courseList = Array.isArray(courseData) ? courseData : courseData.items || [];
  const classList = Array.isArray(classData) ? classData : classData.items || [];
  const out = courseList.map((course) => ({ course, classes: classList.filter((item) => item.course_id === course.id) }));
  // 班级协作与课程材料分别授权，只有班级权限的教师仍能进入自己的班级。
  for (const classroom of classList) {
    let entry = out.find((item) => item.course.id === classroom.course_id);
    if (!entry) {
      entry = { course: { id: classroom.course_id, name: "班级所属课程", can_manage_materials: false, can_create_classes: false }, classes: [] };
      out.push(entry);
    }
    if (!entry.classes.some((item) => item.id === classroom.id)) entry.classes.push(classroom);
  }
  return out;
}

async function classMembers(classId, { signal } = {}) {
  const data = await get(`/classes/${encodeURIComponent(classId)}/members`, { signal });
  return Array.isArray(data) ? data : data.items || [];
}

async function goals(classId, { limit, offset, signal } = {}) {
  const query = new URLSearchParams();
  if (classId) query.set("class_id", classId);
  if (limit !== undefined) query.set("limit", String(limit));
  if (offset !== undefined) query.set("offset", String(offset));
  const data = await get(`/goals${query.size ? `?${query}` : ""}`, { signal });
  return data.items || [];
}

async function goalEvidence(goalId, studentId) {
  const query = studentId ? `?student_id=${encodeURIComponent(studentId)}` : "";
  return get(`/goals/${goalId}/evidence${query}`);
}

/** 学生端：自己的任务列表。 */
async function myTasks(goalId, { classId, limit, offset, signal } = {}) {
  const query = new URLSearchParams();
  if (goalId) query.set("goal_id", goalId);
  if (classId) query.set("class_id", classId);
  if (limit !== undefined) query.set("limit", String(limit));
  if (offset !== undefined) query.set("offset", String(offset));
  const data = await get(`/tasks${query.size ? `?${query}` : ""}`, { signal });
  return data.items || [];
}

const task = (taskId, { signal } = {}) => get(`/tasks/${encodeURIComponent(taskId)}`, { signal });
const courses = () => get("/courses").then((data) => Array.isArray(data) ? data : data.items || []);
const conversations = ({ archived = false, limit = 100, offset = 0, signal } = {}) => {
  const query = new URLSearchParams({ archived: String(archived), limit: String(limit), offset: String(offset) });
  return get(`/companion/conversations?${query}`, { signal }).then((data) => Array.isArray(data) ? data : data.items || []);
};
const getConversation = (id, { signal } = {}) => get(`/companion/conversations/${encodeURIComponent(id)}`, { signal });
const conversationMessages = (id, { after, before, latest = false, limit = 100, signal } = {}) => {
  const query = new URLSearchParams({ limit: String(limit) });
  if (after) query.set("after", after);
  if (before) query.set("before", before);
  if (latest) query.set("latest", "true");
  return get(`/companion/conversations/${encodeURIComponent(id)}/messages?${query}`, { signal })
    .then((data) => Array.isArray(data) ? data : data.items || []);
};
const createConversation = (title, requestId, courseId) => post("/companion/conversations", {
  title, request_id: requestId, ...(courseId ? { course_id: courseId } : {}),
}, { expect: 201 });
const renameConversation = (id, title) => call("PATCH", `/companion/conversations/${encodeURIComponent(id)}`, { title });
const archiveConversation = (id) => post(`/companion/conversations/${encodeURIComponent(id)}/archive`, undefined);
const restoreConversation = (id) => post(`/companion/conversations/${encodeURIComponent(id)}/restore`, undefined);
// Serialize the caller's request ID and input immediately. Recovery only reads the
// returned response ID; it never repeats this POST or creates a replacement key.
const postMessage = (conversationId, text, { taskId, questionVersionId, requestId, intent, hintLevel } = {}) => post(
  `/companion/conversations/${encodeURIComponent(conversationId)}/messages`,
  { text, request_id: requestId, ...(taskId ? { task_id: taskId } : {}),
    ...(questionVersionId ? { question_version_id: questionVersionId } : {}),
    ...(intent ? { intent } : {}), ...(hintLevel !== undefined ? { hint_level: hintLevel } : {}) },
  { expect: 202 },
);
const getResponse = (responseId, { signal } = {}) => get(`/companion/responses/${encodeURIComponent(responseId)}`, { signal });
const cancelCompanionJob = (jobId) => post(`/jobs/${encodeURIComponent(jobId)}/cancel`, undefined);
const resumeCompanionJob = (jobId) => post(`/jobs/${encodeURIComponent(jobId)}/resume`, { confirm_paid: true });
const responseEvents = (responseId, after = 0, { signal } = {}) => get(
  `/companion/responses/${encodeURIComponent(responseId)}/events?after=${encodeURIComponent(after)}&limit=1000`, { signal },
);
const companionAnswerPolicy = ({ taskId, questionVersionId, signal } = {}) => {
  const query = new URLSearchParams();
  if (taskId) query.set("task_id", taskId);
  if (questionVersionId) query.set("question_version_id", questionVersionId);
  return get(`/companion/answer-policy${query.size ? `?${query}` : ""}`, { signal });
};

/**
 * Named event handlers receive (data, { id, generation }); snapshots replace the
 * whole text and their id is the backend's atomic last_event_id, never the last
 * page we happened to read. onClosed(meta) runs once after all final callbacks.
 * The returned close() cancels reads/timers and prevents subsequent callbacks.
 */
function openResponseStream(responseId, handlers = {}, { after = 0, generation = null, text = "" } = {}) {
  const eventId = (value) => {
    if (value === null || value === undefined || value === "") return null;
    const number = Number(value);
    return Number.isSafeInteger(number) && number >= 0 ? number : null;
  };
  const generationId = (value) => Number.isSafeInteger(value) && value >= 0 ? value : null;
  const terminal = (status) => ["completed", "failed", "cancelled"].includes(status);
  const controller = typeof globalThis.AbortController === "function" ? new globalThis.AbortController() : null;
  const readOptions = controller ? { signal: controller.signal } : {};
  let active = true, source = null, timer = null, failures = 0;
  let cursor = eventId(after) ?? 0;
  let lastGeneration = generationId(generation);
  let lastText = typeof text === "string" ? text : "";
  let lastRedacted = false, snapshotKey = "", snapshotInitialized = false, polling = false, terminalEvent = null;

  const meta = () => ({ id: cursor, generation: lastGeneration });
  const emit = (type, data) => {
    if (active && typeof handlers[type] === "function") handlers[type](data, meta());
  };
  const stopSource = () => {
    if (!source) return;
    source.onerror = null;
    source.close();
    source = null;
  };
  const close = (reason = "closed") => {
    if (!active) return;
    active = false;
    if (timer !== null) clearTimeout(timer);
    timer = null;
    stopSource();
    controller?.abort();
    handlers.onClosed?.({ ...meta(), response_id: responseId, reason });
  };
  const revoked = (data = {}) => {
    if (!active) return;
    lastText = "";
    lastRedacted = true;
    try { emit("revoked", { ...data, response_id: responseId, text: "", redacted: true }); }
    finally { close("revoked"); }
  };
  const resetGeneration = (nextGeneration, id = cursor) => {
    lastGeneration = nextGeneration;
    lastText = "";
    lastRedacted = false;
    cursor = id;
    snapshotKey = "";
    terminalEvent = null;
    emit("reset", { response_id: responseId, generation: nextGeneration, text: "", status: "streaming" });
  };
  const invalidSnapshot = () => new ApiError(0, "invalid_response_snapshot", "回答状态暂时未能同步，正在重试", true);

  const applySnapshot = (response, frameId = null) => {
    if (!active) return false;
    if (!response || typeof response !== "object" || Array.isArray(response)
        || response.response_id && response.response_id !== responseId) throw invalidSnapshot();
    const nextGeneration = generationId(response.generation);
    // A snapshot must carry the cursor read with its text. EventSource supplies
    // it in id as well; JSON polling must never invent one from an events page.
    const id = eventId(response.last_event_id) ?? eventId(frameId);
    if (nextGeneration === null || id === null || typeof response.text !== "string") throw invalidSnapshot();
    if (snapshotInitialized && lastGeneration !== null && nextGeneration < lastGeneration) return false;
    if (snapshotInitialized && lastGeneration === nextGeneration && id < cursor) return false;
    snapshotInitialized = true;
    const mustReset = lastGeneration !== nextGeneration && (lastGeneration !== null || lastText !== "" || cursor > 0);
    if (mustReset) resetGeneration(nextGeneration, id);
    else lastGeneration = nextGeneration;
    if (!active) return false;
    cursor = id;
    lastRedacted = Boolean(response.redacted);
    lastText = lastRedacted || ["failed", "cancelled"].includes(response.status) ? "" : response.text;
    const snapshot = { ...response, response_id: responseId, generation: lastGeneration, last_event_id: cursor, text: lastText };
    if (Object.prototype.hasOwnProperty.call(snapshot, "content")) snapshot.content = lastText;
    const key = JSON.stringify([cursor, lastGeneration, snapshot.status, lastText, lastRedacted,
      snapshot.notice, snapshot.error, snapshot.message_id, snapshot.assistant_message_id, snapshot.content_type,
      snapshot.intent, snapshot.hint_level, snapshot.hint_metadata,
      snapshot.job?.status, snapshot.job?.cancel_requested, snapshot.job?.reconciliation_required, snapshot.job?.cost_pending]);
    if (key !== snapshotKey) {
      snapshotKey = key;
      emit("snapshot", snapshot);
    }
    if (!active) return true;
    const hintDeliveryPending = snapshot.intent === "hint" && snapshot.status === "completed"
      && ["queued", "leased", "running", "retry_wait"].includes(snapshot.job?.status);
    if (terminal(snapshot.status) && !hintDeliveryPending) {
      // Give consumers authoritative text before end: an end handler may close
      // immediately, and failed/cancelled responses must not retain a prefix.
      if (terminalEvent && terminalEvent.generation === lastGeneration) {
        emit("end", { ...terminalEvent.data, ...snapshot });
      }
      close("terminal");
    }
    return true;
  };

  const applyEvent = (type, raw, rawId) => {
    if (!active) return;
    if (!raw || typeof raw !== "object" || Array.isArray(raw)
        || raw.response_id && raw.response_id !== responseId) throw invalidSnapshot();
    if (type === "revoked") { revoked(raw); return; }
    if (type === "snapshot") { applySnapshot(raw, rawId); return; }
    const id = eventId(rawId), nextGeneration = generationId(raw.generation);
    if (id === null || id === 0 || nextGeneration === null) throw invalidSnapshot();
    if (lastGeneration !== null && nextGeneration < lastGeneration) return;
    if (id <= cursor) return;
    const changed = nextGeneration !== lastGeneration;
    if (changed) {
      const mustReset = lastGeneration !== null || lastText !== "" || cursor > 0;
      lastGeneration = nextGeneration;
      if (mustReset && type !== "reset") resetGeneration(nextGeneration);
      else if (mustReset || type === "reset") {
        lastText = "";
        lastRedacted = false;
        snapshotKey = "";
        terminalEvent = null;
      }
    }
    if (!active) return;
    cursor = id;
    const data = { ...raw, response_id: responseId, generation: lastGeneration };
    if (type === "reset") {
      lastText = "";
      lastRedacted = false;
      snapshotKey = "";
      terminalEvent = null;
      emit(type, { ...data, text: "" });
    } else if (type === "delta") {
      // A policy-redacted prefix cannot be repaired by appending a later delta.
      // Only a full snapshot can make the content visible again.
      if (data.redacted || lastRedacted) {
        lastText = "";
        lastRedacted = true;
        emit(type, { ...data, text: "", redacted: true });
      } else {
        if (typeof data.text !== "string") throw invalidSnapshot();
        lastText += data.text;
        emit(type, data);
      }
    } else if (type === "end" && terminal(data.status)) {
      terminalEvent = { generation: lastGeneration, data };
      if (data.redacted || ["failed", "cancelled"].includes(data.status)) {
        lastText = "";
        lastRedacted = Boolean(data.redacted);
      }
      // Confirm the terminal projection, including permission/answer policy,
      // through GET before notifying a consumer which may close in end().
      startPolling(true);
    } else {
      emit(type, data);
    }
  };

  const readSnapshot = async () => {
    const response = await getResponse(responseId, readOptions);
    if (active) applySnapshot(response);
  };
  const schedule = (delay) => {
    if (!active) return;
    timer = setTimeout(() => { timer = null; poll(); }, delay);
  };
  const poll = async (snapshotOnly = false) => {
    if (!active) return;
    try {
      if (!snapshotOnly && !terminalEvent) {
        // Drain full pages before checking status. Bound a busy producer to ten
        // pages per turn; its atomic snapshot safely advances to the true tail.
        for (let page = 0; active && page < 10; page += 1) {
          const from = cursor;
          const result = await responseEvents(responseId, from, readOptions);
          if (!active) return;
          const events = Array.isArray(result) ? result : result?.items;
          if (!Array.isArray(events)) throw invalidSnapshot();
          for (const event of events) {
            if (!active) return;
            if (!event || !["start", "reset", "delta", "end", "snapshot", "revoked"].includes(event.event_type)) throw invalidSnapshot();
            applyEvent(event.event_type, { ...(event.data || {}), response_id: event.response_id,
              generation: event.generation }, event.event_id);
            if (terminalEvent) break;
          }
          if (!active || terminalEvent || events.length < 1000 || cursor <= from) break;
        }
      }
      if (!active) return;
      await readSnapshot();
      if (!active) return;
      failures = 0;
      schedule(500);
    } catch (error) {
      if (!active) return;
      if ([401, 403, 404].includes(error?.status)) {
        revoked({ code: error.code || "access_revoked", status: error.status, notice: error.message });
        return;
      }
      emit("error", error);
      if (!active) return;
      failures = Math.min(failures + 1, 5);
      schedule(Math.min(500 * (2 ** failures), 5000));
    }
  };
  function startPolling(snapshotOnly = false) {
    if (!active) return;
    stopSource();
    if (polling) return;
    polling = true;
    poll(snapshotOnly);
  }

  const connect = () => {
    if (!active) return;
    if (typeof globalThis.EventSource !== "function") { startPolling(); return; }
    const url = `${API_BASE}/companion/responses/${encodeURIComponent(responseId)}/stream?after=${encodeURIComponent(cursor)}`;
    try { source = new globalThis.EventSource(url, { withCredentials: true }); }
    catch { startPolling(); return; }
    const connectedSource = source;
    let errors = 0;
    for (const type of ["start", "reset", "delta", "end", "snapshot", "revoked"]) {
      connectedSource.addEventListener(type, (event) => {
        if (!active || source !== connectedSource) return;
        try {
          const data = JSON.parse(event.data);
          applyEvent(type, data, event.lastEventId);
        } catch (error) {
          emit("error", error instanceof ApiError ? error : invalidSnapshot());
          startPolling(true);
          return;
        }
        errors = 0;
      });
    }
    connectedSource.onerror = () => {
      if (!active || source !== connectedSource) return;
      // One native reconnect is allowed; a closed source or repeated failures
      // switches permanently to bounded polling of this same response.
      if (connectedSource.readyState === 2 || ++errors >= 2) startPolling();
    };
  };

  // Start asynchronously so the caller can register close() before onClosed or
  // any terminal callbacks. The initial atomic snapshot repairs tail resumes,
  // unknown generations and stale history before deltas can be appended.
  Promise.resolve().then(async () => {
    if (!active) return;
    try {
      await readSnapshot();
      if (active) connect();
    } catch (error) {
      if (!active) return;
      if ([401, 403, 404].includes(error?.status)) {
        revoked({ code: error.code || "access_revoked", status: error.status, notice: error.message });
      } else {
        emit("error", error);
        startPolling(true);
      }
    }
  });
  return () => close();
}
const retryTask = (taskId, version, requestId) => post(`/tasks/${encodeURIComponent(taskId)}/retry`, {
  expected_version: version, request_id: requestId,
});

/** 拿编辑租约。同一任务同一时刻只有一端能写，这是后端的并发契约。 */
async function acquireLease(taskId, deviceId, { takeover = false, seconds = 180 } = {}) {
  return post(`/tasks/${encodeURIComponent(taskId)}/edit-leases`, {
    device_id: deviceId, takeover, lease_seconds: seconds,
  });
}

async function renewLease(taskId, lease) {
  return post(`/tasks/${encodeURIComponent(taskId)}/edit-leases/renew`, {
    lease_token: lease.lease_token, fence_token: lease.fence_token, lease_seconds: 180,
  });
}

async function saveDraft(taskId, version, lease, answers, requestId) {
  return put(`/tasks/${encodeURIComponent(taskId)}/draft`, {
    expected_version: version,
    request_id: requestId,
    lease_token: lease.lease_token,
    fence_token: lease.fence_token,
    answers,
  });
}

async function submitTask(taskId, version, lease, requestId) {
  return post(`/tasks/${encodeURIComponent(taskId)}/submit`, {
    expected_version: version,
    request_id: requestId,
    lease_token: lease.lease_token,
    fence_token: lease.fence_token,
  }, { expect: 202 });
}

async function startDiagnostic(taskId, version, requestId) {
  return post(`/tasks/${encodeURIComponent(taskId)}/diagnostics`, {
    request_id: requestId, expected_version: version,
  });
}

async function submitDiagnostic(diagnosticId, answers, requestId, unknownQuestionIds = []) {
  return post(`/diagnostics/${encodeURIComponent(diagnosticId)}/submit`, {
    request_id: requestId,
    answers,
    unknown_question_ids: unknownQuestionIds,
  });
}

async function answerPolicy(taskId) {
  return get(`/tasks/${encodeURIComponent(taskId)}/answer-policy`);
}

async function helpEvent(taskId, payload, requestId) {
  return post(`/tasks/${encodeURIComponent(taskId)}/help-events`, { request_id: requestId, ...payload });
}

/** 报告生成重试沿用已确认的源修订，预览和下载固定到同一冻结版本。 */
const reportFormats = ({ signal } = {}) => get("/reports/rules", { signal });

function reportSource(scope, { signal } = {}) {
  const params = new URLSearchParams();
  for (const [key, value] of Object.entries(scope)) {
    if (value !== undefined && value !== null && value !== "") params.set(key, String(value));
  }
  return get(`/reports/source-revision?${params}`, { signal });
}

const reportList = ({ signal } = {}) => get("/reports", { signal }).then((data) => data.items || []);
const reportRevisions = (reportId, { signal } = {}) =>
  get(`/reports/${encodeURIComponent(reportId)}/revisions`, { signal }).then((data) => data.items || []);
const reportRevision = (reportId, revisionId, { signal } = {}) =>
  get(`/reports/${encodeURIComponent(reportId)}${revisionId ? `/revisions/${encodeURIComponent(revisionId)}` : ""}`, { signal });

async function generateReport(scope, key, prepared) {
  let input = prepared;
  if (!input) {
    const rules = await reportFormats();
    const source = await reportSource(scope);
    input = { schema_version: rules.schema_version, rule_version: rules.default_rule_version, expected_source_revision: source.source_revision };
  }
  return request("POST", "/reports", {
    body: {
      scope,
      schema_version: input.schema_version,
      rule_version: input.rule_version,
      expected_source_revision: input.expected_source_revision,
    },
    extraHeaders: { "Idempotency-Key": key },
  }).then((r) => r.data);
}

function exportUrl(reportId, format, revisionId) {
  const path = `${API_BASE}/reports/${encodeURIComponent(reportId)}/export/${encodeURIComponent(format)}`;
  return revisionId ? `${path}?${new URLSearchParams({ revision_id: revisionId })}` : path;
}

async function reportExport(reportId, format, revisionId, { signal, snapshotHash } = {}) {
  if (!revisionId) throw new ApiError(422, "report_revision_required", "请先选择报告版本");
  if (!["html", "pdf", "xlsx", "json", "csv"].includes(format)) throw new ApiError(422, "invalid_format", "报告格式不支持");
  let response;
  try {
    response = await fetch(exportUrl(reportId, format, revisionId), {
      credentials: "same-origin", headers: { Accept: format === "html" ? "text/html" : "application/octet-stream" },
      ...(signal ? { signal } : {}),
    });
  } catch (error) {
    if (signal?.aborted || error?.name === "AbortError") throw error;
    throw new ApiError(0, "network_unavailable", "报告文件没有读到，请重试", true);
  }
  if (!response.ok) {
    let payload = null;
    try { payload = await response.json(); } catch { /* Export errors may have an empty body. */ }
    throw toApiError(response.status, payload);
  }
  if (response.headers.get("X-Report-Revision") !== revisionId
      || (snapshotHash && response.headers.get("X-Report-Snapshot-SHA256") !== snapshotHash)) {
    throw new ApiError(409, "report_revision_mismatch", "报告版本未核对一致，请重新读取");
  }
  return response;
}

async function reportPreview(reportId, revisionId, options = {}) {
  const response = await reportExport(reportId, "html", revisionId, options);
  return response.text();
}

async function reportDownload(reportId, format, revisionId, options = {}) {
  const response = await reportExport(reportId, format, revisionId, options);
  const disposition = response.headers.get("Content-Disposition") || "";
  const encoded = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  const quoted = disposition.match(/filename="([^"]+)"/i);
  let name = quoted?.[1] || "";
  try { if (encoded) name = decodeURIComponent(encoded[1]); } catch { /* Use the local filename fallback. */ }
  const extension = format === "csv" ? "zip" : format;
  const fallback = `report-${revisionId}.${extension}`;
  // A response filename is only a display name, never a path.
  name = name.replace(/[\\/\\\\:\x00-\x1f\x7f]/g, "_").replace(/^\.+/, "");
  if (!name.toLowerCase().endsWith(`.${extension}`)) name = fallback;
  return { blob: await response.blob(), filename: name || fallback };
}

export const api = {
  state, probe, currentUser, login, logout, call, upload,
  applyTeacherAccount, activateAccount, resetPassword,
  teacherClasses, classMembers, goals, goalEvidence,
  myTasks, task, courses, conversations, getConversation, conversationMessages, createConversation, renameConversation, archiveConversation,
  restoreConversation, postMessage, getResponse, cancelCompanionJob, resumeCompanionJob, responseEvents, companionAnswerPolicy, openResponseStream, retryTask,
  acquireLease, renewLease, saveDraft, submitTask,
  startDiagnostic, submitDiagnostic, answerPolicy, helpEvent,
  reportFormats, reportSource, reportList, reportRevisions, reportRevision, generateReport, exportUrl, reportPreview, reportDownload,
  ApiError,
};

export const isLive = () => state.live;
export const liveUser = () => state.user;
