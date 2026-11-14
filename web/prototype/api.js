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

async function request(method, path, { body, expect = 200, extraHeaders = {} } = {}) {
  // 调用方一律传 /auth/login 这样的相对路径，统一补上 /api/v1。
  // 已经带前缀的（比如导出链接）原样使用。
  const url = path.startsWith("/api/") ? path : `${API_BASE}${path}`;
  const options = {
    method,
    credentials: "same-origin",
    headers: { ...originHeaders(), ...extraHeaders },
  };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch (error) {
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
  await post("/auth/logout", undefined, { expect: 204 });
  state.user = null;
}

/** 教师端：拿到自己带得动的课程与班级。 */
async function teacherClasses() {
  const courses = await get("/courses");
  const list = Array.isArray(courses) ? courses : courses.items || [];
  const out = [];
  for (const course of list) {
    let classes = [];
    try {
      classes = await get(`/courses/${course.id}/classes`);
      classes = Array.isArray(classes) ? classes : classes.items || [];
    } catch {
      classes = [];
    }
    out.push({ course, classes });
  }
  return out;
}

async function classMembers(classId) {
  const data = await get(`/classes/${classId}/members`);
  return Array.isArray(data) ? data : data.items || [];
}

async function goals(classId) {
  const query = classId ? `?class_id=${encodeURIComponent(classId)}` : "";
  const data = await get(`/goals${query}`);
  return data.items || [];
}

async function goalEvidence(goalId, studentId) {
  const query = studentId ? `?student_id=${encodeURIComponent(studentId)}` : "";
  return get(`/goals/${goalId}/evidence${query}`);
}

/** 学生端：自己的任务列表。 */
async function myTasks(goalId) {
  const query = goalId ? `?goal_id=${encodeURIComponent(goalId)}` : "";
  const data = await get(`/tasks${query}`);
  return data.items || [];
}

const task = (taskId) => get(`/tasks/${encodeURIComponent(taskId)}`);
const courses = () => get("/courses").then((data) => Array.isArray(data) ? data : data.items || []);
const conversations = () => get("/companion/conversations").then((data) => Array.isArray(data) ? data : data.items || []);
const conversationMessages = (id) => get(`/companion/conversations/${encodeURIComponent(id)}/messages`)
  .then((data) => Array.isArray(data) ? data : data.items || []);
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

async function submitDiagnostic(diagnosticId, answers, requestId) {
  return post(`/diagnostics/${encodeURIComponent(diagnosticId)}/submit`, {
    request_id: requestId,
    answers,
    unknown_question_ids: [],
  });
}

async function answerPolicy(taskId) {
  return get(`/tasks/${encodeURIComponent(taskId)}/answer-policy`);
}

async function helpEvent(taskId, payload, requestId) {
  return post(`/tasks/${encodeURIComponent(taskId)}/help-events`, { request_id: requestId, ...payload });
}

/** 教师端报告：先取源修订，再生成，最后拿到可下载的导出链接。 */
async function reportFormats() {
  return get("/reports/rules");
}

async function reportSource(scope) {
  const params = new URLSearchParams(scope).toString();
  return get(`/reports/source-revision?${params}`);
}

async function generateReport(scope, key) {
  const rules = await reportFormats();
  const source = await reportSource(scope);
  return request("POST", "/reports/generate", {
    body: {
      scope,
      schema_version: rules.schema_version,
      rule_version: rules.default_rule_version,
      expected_source_revision: source.source_revision,
    },
    extraHeaders: { "Idempotency-Key": key },
  }).then((r) => r.data);
}

const exportUrl = (reportId, format) =>
  `${API_BASE}/reports/${encodeURIComponent(reportId)}/export/${format}`;

export const api = {
  state, probe, currentUser, login, logout,
  applyTeacherAccount, activateAccount, resetPassword,
  teacherClasses, classMembers, goals, goalEvidence,
  myTasks, task, courses, conversations, conversationMessages, retryTask,
  acquireLease, renewLease, saveDraft, submitTask,
  startDiagnostic, submitDiagnostic, answerPolicy, helpEvent,
  reportFormats, reportSource, generateReport, exportUrl,
  ApiError,
};

export const isLive = () => state.live;
export const liveUser = () => state.user;
