"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const sourcePath = path.resolve(__dirname, "../../web/prototype/admin-workspace.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => value == null ? value : JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const failure = (status, message) => Object.assign(new Error(message), { status });
const user = (id, role = "student", status = "active", extra = {}) => ({ id, username: `login_${id}`, display_name: `姓名${id}`, role, status, ...extra });
const application = (extra = {}) => ({ id: "course-request", applicant_id: "teacher1", name: "数学课程", term: "2026秋", status: "pending", course_id: null, reviewed_by: null, review_note: "", created_at: 1770000000, reviewed_at: null, ...extra });
const limit = (extra = {}) => ({ scope_type: "project", scope_id: "platform", period: null, limit_units: 500, emergency_units: 20, spent_units: 5, reserved_units: 2, overage_units: 0, emergency_exceeded_units: 0, notification_required: false, course_id: null, class_id: null, configured: true, ...extra });

function harness(settings = {}) {
  const calls = [], toasts = [], events = {}, timers = new Map(), copies = [];
  let changed = 0, renders = 0, timerId = 0, workspace;
  const records = {
    accounts: settings.accounts || [user("teacher1", "teacher", "pending"), user("student1"), user("teacher2", "teacher")],
    courses: settings.courses || [application()], budgets: settings.budgets || [],
  };
  const state = { live: true, role: "admin", screen: "app", transitionId: 1, ...settings.state };
  const api = { state: { user: user("admin1", "admin"), ...settings.apiState }, call: async (method, url, body) => {
    calls.push({ method, url, body: plain(body), raw: body });
    return settings.call ? settings.call(method, url, body, fallback) : fallback(method, url, body);
  } };
  const fallback = async (method, url, body) => {
    if (method === "GET" && url === "/auth/teacher-applications") return records.accounts.filter((row) => row.role === "teacher" && row.status === "pending");
    if (method === "GET" && url === "/course-applications") return records.courses;
    if (method === "GET" && url.startsWith("/auth/accounts?")) {
      const query = new URLSearchParams(url.split("?")[1]);
      const selected = records.accounts.filter((row) => (!query.get("role") || row.role === query.get("role")) && (!query.get("status") || row.status === query.get("status")) && (!query.get("query") || `${row.username} ${row.display_name}`.includes(query.get("query"))));
      const size = Number(query.get("limit")), offset = Number(query.get("offset"));
      return { items: selected.slice(offset, offset + size), total: selected.length, limit: size, offset };
    }
    if (method === "GET" && url === "/budgets/limits") return { items: records.budgets, currency: "site-credit", unit: "billing_unit", unit_scale: 1, unit_description: "服务计费整数单位，无现金汇率" };
    if (method === "POST" && url.startsWith("/auth/teacher-applications/")) {
      const id = decodeURIComponent(url.split("/")[3]), row = records.accounts.find((item) => item.id === id);
      row.status = url.endsWith("/approve") ? "active" : "disabled";
      return { user_id: id, status: row.status, review_note: body.note };
    }
    if (method === "POST" && url.endsWith("/review")) {
      const row = records.courses.find((item) => item.id === decodeURIComponent(url.split("/")[2]));
      Object.assign(row, { status: body.approve ? "approved" : "rejected", review_note: body.note, course_id: body.approve ? "real-course" : null });
      return row;
    }
    if (method === "PATCH" && url.endsWith("/status")) {
      const row = records.accounts.find((item) => item.id === decodeURIComponent(url.split("/")[3]));
      row.status = body.status; return row;
    }
    if (method === "POST" && url === "/auth/password-reset-codes") return { user_id: body.user_id, reset_code: "one-time-code-for-test-000001", expires_in_seconds: 3600 };
    if (method === "PUT" && url === "/budgets/limits") {
      const row = limit({ ...body, course_id: null }); records.budgets = [row]; return row;
    }
    throw new Error(`Unexpected request ${method} ${url}`);
  };
  const root = {
    isConnected: true, innerHTML: "", ownerDocument: { activeElement: null },
    contains: (node) => node?.owner === root,
    matches: (selector) => selector === "[data-admin-workspace]",
    closest: (selector) => selector === "[data-admin-workspace]" ? root : null,
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: (name, handler) => { events[name] = handler; },
  };
  const storage = new Proxy({}, { get() { throw new Error("one-time codes must not touch persistent storage"); }, set() { throw new Error("one-time codes must not touch persistent storage"); } });
  const context = vm.createContext({ console, URLSearchParams, localStorage: storage, sessionStorage: storage, navigator: { clipboard: { writeText: async (value) => { if (settings.copyFailure) throw new Error("clipboard denied"); copies.push(value); } } }, setTimeout: (fn) => { timers.set(++timerId, fn); return timerId; }, clearTimeout: (id) => timers.delete(id) });
  vm.runInContext(source.replace("export function createAdminWorkspace", "function createAdminWorkspace") + "\nglobalThis.make = createAdminWorkspace;", context, { filename: sourcePath });
  workspace = context.make({ api, state, toast: (message) => toasts.push(message), render: () => { renders += 1; }, onChanged: settings.noChanged ? undefined : async () => { changed += 1; if (settings.changedLoad) await workspace.load(); if (settings.changedFailure) throw new Error("refresh failed"); } });
  root.innerHTML = workspace.render(); workspace.bind(root);
  function node(dataset, extra = {}) {
    return {
      owner: root, dataset, tagName: "INPUT", type: "text", value: "", ...extra,
      getAttribute: (key) => key.startsWith("data-") ? dataset[key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] ?? null : null,
      closest(selector) {
        if (selector === "[data-admin-workspace]") return this.owner;
        if (selector === "[data-mgmt-action]") return this.dataset.mgmtAction ? this : null;
        if (selector === "[data-mgmt-field]") return this.dataset.mgmtField ? this : null;
        if (selector === "[data-mgmt-form]") return this.dataset.mgmtForm ? this : null;
        return null;
      },
    };
  }
  return {
    workspace, state, api, calls, toasts, root, records, timers, copies, events, node,
    html: () => workspace.render(), changed: () => changed, renders: () => renders,
    hostRender: () => { root.innerHTML = workspace.render(); workspace.bind(root); },
    writes: () => calls.filter((item) => item.method !== "GET"),
    click: (action, value = "") => events.click({ target: node({ mgmtAction: action, mgmtValue: value }), currentTarget: root, stopPropagation() {} }),
    field: (scope, key, value) => events.input({ type: "input", currentTarget: root, target: node({ mgmtScope: scope, mgmtField: key }, typeof value === "boolean" ? { type: "checkbox", checked: value } : { value }) }),
    submit: (name) => events.submit({ target: node({ mgmtForm: name }, { tagName: "FORM" }), currentTarget: root, preventDefault() {}, stopPropagation() {} }),
  };
}

async function openTeacher(h, approve = false) {
  await h.workspace.load(); await h.click(approve ? "teacher-approve" : "teacher-reject", "teacher1");
}
async function openAccount(h, action = "reset-code", id = "student1") {
  await h.workspace.load(); await h.click("tab", "accounts"); await h.click(action, id);
}
async function issueCode(h) { await openAccount(h); h.field("accounts", "confirmed", true); await h.submit("accounts"); }
async function openPlatform(h) { await h.workspace.load(); await h.click("tab", "budgets"); await h.click("platform-budget"); }

test("offline and non-administrator sessions have no calls or fabricated records", async () => {
  for (const state of [{ live: false }, { role: "student" }, { role: "teacher" }]) {
    const h = harness({ state }); await h.workspace.load();
    assert.equal(h.calls.length, 0); assert.match(h.html(), /正式服务未连接|仅已登录的管理员/);
    assert.doesNotMatch(h.html(), /data-mgmt-form=|数学课程|姓名teacher1/);
  }
  const absent = harness({ apiState: { user: null } }); await absent.workspace.load(); assert.equal(absent.calls.length, 0);
  assert.doesNotMatch(source, /\bdata-(?:action|view)=|<h1\b|activation-codes|activation_code/);
});

test("load paints its asynchronous results and does not notify onChanged", async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => url === "/auth/teacher-applications" ? gate.promise : fallback(method, url, body) });
  const loading = h.workspace.load(); assert.match(h.root.innerHTML, /正在读取教师申请/);
  gate.resolve([user("teacher-new", "teacher", "pending")]); await loading;
  assert.match(h.root.innerHTML, /姓名teacher-new/); assert.equal(h.changed(), 0); assert.equal(h.calls.length, 4);
});

test("teacher rejection requires reason and confirmation, retaining invalid input", async () => {
  const h = harness(); await openTeacher(h);
  h.field("teachers", "note", "资料需要补齐"); await h.submit("teachers");
  assert.equal(h.writes().length, 0); assert.match(h.html(), /资料需要补齐/); assert.match(h.html(), /勾选确认/);
  h.field("teachers", "confirmed", true); h.field("teachers", "note", "   "); await h.submit("teachers");
  assert.equal(h.writes().length, 0); assert.match(h.html(), /拒绝理由/);
  h.field("teachers", "note", "理".repeat(501)); await h.submit("teachers"); assert.equal(h.writes().length, 0);
  h.field("teachers", "note", "  资料需要补齐  "); await h.submit("teachers");
  assert.deepEqual(h.writes()[0].body, { note: "资料需要补齐" }); assert.match(h.writes()[0].url, /teacher1\/reject$/);
  assert.doesNotMatch(h.html(), /data-mgmt-form="teachers"/); assert.equal(h.changed(), 1); assert.match(h.html(), /理由已记录/);
});

test("teacher approval reports direct activation with the existing password and no invented code", async () => {
  const h = harness({ changedLoad: true }); await openTeacher(h, true); h.field("teachers", "confirmed", true); await h.submit("teachers");
  assert.equal(h.writes().length, 1); assert.deepEqual(h.writes()[0].body, { note: "" });
  assert.match(h.html(), /账号已启用，可使用申请时设置的口令登录/); assert.doesNotMatch(h.html(), /data-mgmt-code|激活码已/);
  assert.equal(h.changed(), 1); assert.equal(h.calls.filter((item) => item.method === "GET").length, 8);
});

test("server validation failure keeps reason and form and permits corrected explicit submission", async () => {
  let count = 0;
  const h = harness({ call: (method, url, body, fallback) => { if (method === "POST" && ++count === 1) throw failure(422, "审核说明格式错误"); return fallback(method, url, body); } });
  await openTeacher(h); h.field("teachers", "confirmed", true); h.field("teachers", "note", "原始原因"); await h.submit("teachers");
  assert.match(h.html(), /审核说明格式错误/); assert.match(h.html(), /原始原因/); assert.match(h.html(), /data-mgmt-form="teachers"/); assert.equal(h.changed(), 0);
  h.field("teachers", "note", "修正原因"); await h.submit("teachers");
  assert.equal(h.writes().length, 2); assert.equal(h.writes()[1].body.note, "修正原因"); assert.equal(h.changed(), 1);
});

test("write lock freezes the command and prevents duplicate submit, edits, navigation, and other writes", async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => method === "POST" ? gate.promise : fallback(method, url, body) });
  await openTeacher(h); h.field("teachers", "note", "冻结原因"); h.field("teachers", "confirmed", true);
  const pending = h.submit("teachers"); h.field("teachers", "note", "替换原因"); await h.submit("teachers"); await h.click("tab", "accounts"); await h.click("close-editor", "teachers");
  assert.equal(h.writes().length, 1); assert.ok(Object.isFrozen(h.writes()[0].raw)); assert.equal(h.writes()[0].body.note, "冻结原因");
  assert.match(h.root.innerHTML, /fieldset class="mgmt-fields" disabled/); assert.match(h.html(), /冻结原因/); assert.doesNotMatch(h.html(), /替换原因/);
  gate.resolve({ user_id: "teacher1", status: "disabled", review_note: "冻结原因" }); await pending;
  assert.equal(h.changed(), 1); assert.doesNotMatch(h.html(), /data-mgmt-form="teachers"/);
});

test("unknown mutation result stays locked across close, refresh, and reopen, and recheck only reads", async () => {
  const h = harness({ call: (method, url, body, fallback) => { if (method === "POST") throw failure(0, "连接在发送后中断"); return fallback(method, url, body); } });
  await openTeacher(h); h.field("teachers", "note", "保留理由"); h.field("teachers", "confirmed", true); await h.submit("teachers");
  assert.match(h.html(), /提交结果尚未确认/); assert.match(h.html(), /保留理由/); assert.equal(h.changed(), 0);
  await h.submit("teachers"); await h.click("recheck", "teachers"); assert.equal(h.writes().length, 1); assert.match(h.html(), /重新查询显示当前状态/);
  await h.click("close-editor", "teachers"); await h.click("refresh", "teachers"); await h.click("teacher-reject", "teacher1");
  h.field("teachers", "confirmed", true); h.field("teachers", "note", "第二次原因"); await h.submit("teachers");
  assert.equal(h.writes().length, 1); assert.match(h.html(), /提交结果尚未确认/);
});

for (const status of [null, 408, 500]) test(`ambiguous ${status ?? "untyped"} failures never automatically repeat a POST`, async () => {
  const h = harness({ call: (method, url, body, fallback) => { if (method === "POST") throw status == null ? new Error("连接中断") : failure(status, "无法确认"); return fallback(method, url, body); } });
  await openTeacher(h, true); h.field("teachers", "confirmed", true); await h.submit("teachers"); await h.submit("teachers");
  assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0); assert.match(h.html(), /不会自动重发/);
});

test("malformed successful review response never clears the form or pretends success", async () => {
  const h = harness({ call: (method, url, body, fallback) => method === "POST" ? { user_id: "wrong", status: "disabled" } : fallback(method, url, body) });
  await openTeacher(h); h.field("teachers", "note", "仍要保留"); h.field("teachers", "confirmed", true); await h.submit("teachers");
  assert.match(h.html(), /仍要保留/); assert.match(h.html(), /提交结果尚未确认/); assert.equal(h.changed(), 0); assert.equal(h.toasts.length, 0);
});

test("delayed read from an old transition cannot populate the next administrator session", async () => {
  const gate = deferred(); let reads = 0;
  const h = harness({ call: (method, url, body, fallback) => url === "/auth/teacher-applications" && ++reads === 1 ? gate.promise : fallback(method, url, body) });
  const pending = h.workspace.load(); h.state.transitionId += 1; h.api.state.user = user("admin2", "admin"); await h.workspace.load();
  gate.resolve([user("OLD-PRIVATE", "teacher", "pending")]); await pending;
  assert.doesNotMatch(h.html(), /OLD-PRIVATE/); assert.match(h.html(), /姓名teacher1/); assert.equal(h.changed(), 0);
});

for (const change of ["transition", "identity", "session", "reset"]) test(`late code response cannot survive ${change} change or call host hooks`, async () => {
  const gate = deferred();
  const h = harness({ state: { session: { id: "session1" } }, call: (method, url, body, fallback) => method === "POST" ? gate.promise : fallback(method, url, body) });
  await openAccount(h); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  if (change === "transition") h.state.transitionId += 1;
  if (change === "identity") h.api.state.user = user("admin2", "admin");
  if (change === "session") h.state.session = { id: "session2" };
  if (change === "reset") h.workspace.reset();
  gate.resolve({ user_id: "student1", reset_code: "OLD-PRIVATE-CODE-000000001", expires_in_seconds: 3600 }); await pending;
  assert.doesNotMatch(h.html(), /OLD-PRIVATE-CODE/); assert.equal(h.toasts.length, 0); assert.equal(h.changed(), 0); assert.equal(h.timers.size, 0);
});

test("failed logout preserves the in-flight code command and blocks a duplicate until its late receipt", async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => method === "POST" ? gate.promise : fallback(method, url, body) });
  await openAccount(h); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  h.state.loggingOut = true; h.hostRender();
  const requests = h.calls.length;
  await h.workspace.load(); await h.click("tab", "teachers"); await h.click("close-editor", "accounts"); await h.submit("accounts");
  assert.equal(h.calls.length, requests); assert.match(h.root.innerHTML, /正在退出/);
  h.state.loggingOut = false; h.hostRender();
  assert.match(h.html(), /data-mgmt-form="accounts"><fieldset class="mgmt-fields" disabled/);
  assert.match(h.html(), /正在提交/); await h.submit("accounts"); assert.equal(h.writes().length, 1);
  gate.resolve({ user_id: "student1", reset_code: "MUST-NEVER-RETURN-AFTER-LOGOUT", expires_in_seconds: 3600 }); await pending;
  assert.match(h.html(), /口令重设码已签发/); assert.match(h.html(), /代码已丢弃且不会恢复显示/);
  assert.doesNotMatch(h.html(), /MUST-NEVER-RETURN|data-mgmt-code|data-mgmt-form="accounts"/); assert.equal(h.timers.size, 0);
  await h.click("copy-code"); assert.deepEqual(h.copies, []); assert.equal(h.writes().length, 1);
});

test("successful code receipt during logout is recorded without host reload and its code stays discarded after failure", async () => {
  const gate = deferred();
  const h = harness({ changedLoad: true, call: (method, url, body, fallback) => method === "POST" ? gate.promise : fallback(method, url, body) });
  await openAccount(h); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  h.state.loggingOut = true; h.hostRender(); const requests = h.calls.length;
  gate.resolve({ user_id: "student1", reset_code: "DISCARDED-DURING-LOGOUT-0001", expires_in_seconds: 3600 }); await pending;
  assert.equal(h.calls.length, requests); assert.equal(h.changed(), 0); assert.equal(h.toasts.length, 0); assert.equal(h.renders(), 0); assert.equal(h.timers.size, 0);
  assert.match(h.root.innerHTML, /正在退出/); assert.doesNotMatch(h.root.innerHTML, /DISCARDED-DURING|data-mgmt-code/);
  h.state.loggingOut = false; h.hostRender();
  assert.match(h.html(), /口令重设码已签发/); assert.match(h.html(), /代码已丢弃且不会恢复显示/);
  assert.doesNotMatch(h.html(), /DISCARDED-DURING|data-mgmt-form="accounts"|data-mgmt-code/);
  await h.submit("accounts"); assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0);
});

test("logout start clears an existing code and expiry timer while a failed logout preserves other drafts", async () => {
  const h = harness(); await h.workspace.load(); await h.click("teacher-reject", "teacher1");
  h.field("teachers", "note", "退出失败后仍需保留的拒绝理由"); h.field("teachers", "confirmed", true);
  await h.click("tab", "accounts"); await h.click("reset-code", "student1"); h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.equal(h.timers.size, 1); assert.match(h.html(), /one-time-code-for-test/);
  h.state.loggingOut = true; h.hostRender(); assert.equal(h.timers.size, 0); assert.doesNotMatch(h.root.innerHTML, /one-time-code-for-test/);
  await h.click("copy-code"); assert.deepEqual(h.copies, []);
  h.state.loggingOut = false; h.hostRender();
  assert.doesNotMatch(h.html(), /one-time-code-for-test/); await h.click("tab", "teachers");
  assert.match(h.html(), /退出失败后仍需保留的拒绝理由/); assert.match(h.html(), /name="confirmed"[^>]*checked/); assert.equal(h.timers.size, 0);
});

for (const timing of ["before", "during", "after"]) test(`unknown code outcome ${timing} a failed logout preserves its resource lock across recheck and reopen`, async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => method === "POST" ? gate.promise : fallback(method, url, body) });
  await openAccount(h); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  if (timing === "before") { gate.reject(failure(0, "签码结果未知")); await pending; }
  h.state.loggingOut = true; h.hostRender();
  if (timing === "during") { gate.reject(failure(500, "签码结果未知")); await pending; }
  h.state.loggingOut = false; h.hostRender();
  if (timing === "after") { gate.reject(failure(0, "签码结果未知")); await pending; }
  assert.match(h.html(), /签码结果未知/); assert.match(h.html(), /提交结果尚未确认/); assert.match(h.html(), /name="confirmed"[^>]*checked/);
  await h.submit("accounts"); await h.click("recheck", "accounts");
  assert.match(h.html(), /账号查询不能取回一次性码或确认签发结果/);
  await h.click("close-editor", "accounts"); await h.click("reset-code", "student1"); h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0); assert.equal(h.timers.size, 0); assert.match(h.html(), /提交结果尚未确认/);
});

test("definite review failure during logout preserves its reason and allows correction after logout fails", async () => {
  const gate = deferred(); let writes = 0;
  const h = harness({ call: (method, url, body, fallback) => method === "POST" && ++writes === 1 ? gate.promise : fallback(method, url, body) });
  await openTeacher(h); h.field("teachers", "note", "保存原始审核原因"); h.field("teachers", "confirmed", true); const pending = h.submit("teachers");
  h.state.loggingOut = true; h.hostRender(); gate.reject(failure(422, "审核原因格式错误")); await pending;
  assert.equal(h.changed(), 0); assert.equal(h.toasts.length, 0);
  h.state.loggingOut = false; h.hostRender();
  assert.match(h.html(), /保存原始审核原因/); assert.match(h.html(), /审核原因格式错误/);
  assert.doesNotMatch(h.html(), /data-mgmt-action="recheck"/);
  h.field("teachers", "note", "修正后的审核原因"); await h.submit("teachers");
  assert.equal(h.writes().length, 2); assert.equal(h.writes()[1].body.note, "修正后的审核原因"); assert.equal(h.changed(), 1);
});

test("successful status receipt during logout updates the original session without starting host refresh", async () => {
  const gate = deferred();
  const h = harness({ changedLoad: true, call: (method, url, body, fallback) => method === "PATCH" ? gate.promise : fallback(method, url, body) });
  await openAccount(h, "account-disable"); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  h.state.loggingOut = true; h.hostRender(); const requests = h.calls.length;
  gate.resolve(user("student1", "student", "disabled")); await pending;
  assert.equal(h.changed(), 0); assert.equal(h.calls.length, requests); assert.equal(h.toasts.length, 0);
  h.state.loggingOut = false; h.hostRender();
  assert.match(h.html(), /账号已停用/); assert.doesNotMatch(h.html(), /data-mgmt-form="accounts"/);
  assert.match(h.html(), /data-mgmt-action="account-restore" data-mgmt-value="student1"/); assert.equal(h.writes().length, 1);
});

for (const outcome of ["success", "unknown"]) test(`completed logout invalidates a late ${outcome} code receipt even for a new session with the same account`, async () => {
  const gate = deferred(); let writes = 0;
  const h = harness({ call: (method, url, body, fallback) => method === "POST" && ++writes === 1 ? gate.promise : fallback(method, url, body) });
  await openAccount(h); h.field("accounts", "confirmed", true); const pending = h.submit("accounts");
  h.state.loggingOut = true; h.hostRender(); h.workspace.reset();
  assert.equal(h.root.innerHTML, ""); h.state.loggingOut = false; h.state.transitionId += 1; h.api.state.user = user("admin1", "admin"); h.hostRender();
  await h.workspace.load(); await h.click("tab", "accounts"); await h.click("reset-code", "student1"); h.field("accounts", "confirmed", true);
  const requests = h.calls.length;
  if (outcome === "success") gate.resolve({ user_id: "student1", reset_code: "STALE-LOGGED-OUT-CODE-0000001", expires_in_seconds: 3600 });
  else gate.reject(failure(500, "旧会话的签码结果未知"));
  await pending;
  assert.equal(h.calls.length, requests); assert.equal(h.changed(), 0); assert.equal(h.toasts.length, 0); assert.equal(h.timers.size, 0);
  assert.doesNotMatch(h.html(), /STALE-LOGGED-OUT|旧会话的签码结果未知|提交结果尚未确认/); assert.match(h.html(), /data-mgmt-form="accounts"/);
  await h.submit("accounts"); assert.equal(h.writes().length, 2); assert.equal(h.changed(), 1); assert.match(h.html(), /one-time-code-for-test/);
});

test("course review uses the exact review route and boolean/note body; rejection needs a reason", async () => {
  const h = harness(); await h.workspace.load(); await h.click("tab", "courses"); await h.click("course-reject", "course-request");
  h.field("courses", "confirmed", true); await h.submit("courses"); assert.equal(h.writes().length, 0);
  h.field("courses", "note", "请核对教学范围"); await h.submit("courses");
  assert.equal(h.writes()[0].url, "/course-applications/course-request/review"); assert.deepEqual(h.writes()[0].body, { approve: false, note: "请核对教学范围" });
  assert.match(h.html(), /已拒绝/); assert.match(h.html(), /请核对教学范围/); assert.doesNotMatch(h.html(), /data-mgmt-form="courses"/);
});

test("course approval requires a real created course identifier", async () => {
  const h = harness({ call: (method, url, body, fallback) => method === "POST" ? application({ status: "approved", review_note: body.note }) : fallback(method, url, body) });
  await h.workspace.load(); await h.click("tab", "courses"); await h.click("course-approve", "course-request"); h.field("courses", "confirmed", true); await h.submit("courses");
  assert.match(h.html(), /提交结果尚未确认/); assert.equal(h.changed(), 0); assert.doesNotMatch(h.html(), /课程“数学课程”已创建/);
});

test("account search sends literal query, role, status, limit and offset and pages on server totals", async () => {
  const rows = Array.from({ length: 60 }, (_, i) => user(`row${i}`, "student", "active", { display_name: `literal%_姓名${i}` }));
  const h = harness({ accounts: rows }); await h.workspace.load(); await h.click("tab", "accounts");
  h.field("filters", "query", "literal%_"); h.field("filters", "role", "student"); h.field("filters", "status", "active"); await h.submit("filters");
  let query = new URLSearchParams(h.calls.at(-1).url.split("?")[1]);
  assert.equal(query.get("query"), "literal%_"); assert.equal(query.get("role"), "student"); assert.equal(query.get("status"), "active"); assert.equal(query.get("limit"), "50"); assert.equal(query.get("offset"), "0");
  assert.match(h.html(), /找到 60 个账号/); await h.click("page", "next"); query = new URLSearchParams(h.calls.at(-1).url.split("?")[1]); assert.equal(query.get("offset"), "50");
  assert.match(h.html(), /第 2 页/); assert.match(h.html(), /literal%_姓名59/); assert.doesNotMatch(h.html(), /literal%_姓名0</);
  await h.click("clear-filters"); query = new URLSearchParams(h.calls.at(-1).url.split("?")[1]); assert.equal(query.get("offset"), "0"); assert.equal(query.get("role"), "");
});

test("latest account search wins over an earlier slow result and failed search preserves filters", async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => url.includes("query=old") ? gate.promise : url.includes("query=new") ? { items: [user("NEW")], total: 1, limit: 50, offset: 0 } : url.includes("query=failed") ? Promise.reject(failure(503, "查询暂不可用")) : fallback(method, url, body) });
  await h.workspace.load(); await h.click("tab", "accounts"); h.field("filters", "query", "old"); const pending = h.submit("filters");
  h.field("filters", "query", "new"); await h.submit("filters"); gate.resolve({ items: [user("OLD")], total: 1, limit: 50, offset: 0 }); await pending;
  assert.match(h.html(), /姓名NEW/); assert.doesNotMatch(h.html(), /姓名OLD/);
  h.field("filters", "query", "failed"); await h.submit("filters"); assert.match(h.html(), /value="failed"/); assert.match(h.html(), /查询暂不可用/); assert.match(h.html(), /上次成功读取/);
});

test("account status mutations require explicit confirmation and do not operate on pending accounts", async () => {
  const h = harness(); await openAccount(h, "account-disable"); await h.submit("accounts"); assert.equal(h.writes().length, 0);
  h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.deepEqual(h.writes()[0].body, { status: "disabled" }); assert.equal(h.writes()[0].method, "PATCH"); assert.equal(h.writes()[0].url, "/auth/users/student1/status");
  await h.click("account-restore", "student1"); h.field("accounts", "confirmed", true); await h.submit("accounts"); assert.deepEqual(h.writes()[1].body, { status: "active" });
  await h.click("account-restore", "teacher1"); await h.submit("accounts"); assert.equal(h.writes().length, 2);
});

test("last-administrator denial retains the confirmation; self-disable stops further workspace activity", async () => {
  const h = harness({ accounts: [user("admin1", "admin")], call: (method, url, body, fallback) => { if (method === "PATCH") throw failure(409, "不能停用最后一个可用管理员"); return fallback(method, url, body); } });
  await openAccount(h, "account-disable", "admin1"); h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.match(h.html(), /不能停用最后一个可用管理员/); assert.match(h.html(), /data-mgmt-form="accounts"/); assert.equal(h.changed(), 0);
  const other = harness({ accounts: [user("admin1", "admin"), user("admin2", "admin")] });
  await openAccount(other, "account-disable", "admin1"); other.field("accounts", "confirmed", true); await other.submit("accounts");
  assert.match(other.html(), /当前账号已停用/); const reads = other.calls.length; await other.workspace.load(); await other.click("tab", "teachers"); assert.equal(other.calls.length, reads);
});

test("codes are in memory only, copyable, removable, and reset clears memory, visible DOM and timers", async () => {
  const h = harness(); await issueCode(h);
  assert.match(h.html(), /one-time-code-for-test-000001/); assert.match(h.html(), /账号口令尚未更改/); assert.equal(h.timers.size, 1);
  assert.deepEqual(h.writes()[0].body, { user_id: "student1" }); await h.click("copy-code"); assert.deepEqual(h.copies, ["one-time-code-for-test-000001"]);
  h.workspace.reset(); assert.equal(h.root.innerHTML, ""); assert.equal(h.timers.size, 0); assert.doesNotMatch(h.html(), /one-time-code-for-test/);
  assert.doesNotMatch(JSON.stringify(h.state), /one-time-code/); assert.doesNotMatch(JSON.stringify(h.api.state), /one-time-code/);
  assert.doesNotMatch(source, /\blocalStorage\b|\bsessionStorage\b|console\.(?:log|debug|info)/);
});

test("tab changes and explicit clearing remove code; failed clipboard preserves code with manual instructions", async () => {
  const h = harness({ copyFailure: true }); await issueCode(h); await h.click("copy-code");
  assert.match(h.html(), /自动复制未完成/); assert.match(h.html(), /one-time-code-for-test/);
  await h.click("clear-code"); assert.doesNotMatch(h.html(), /one-time-code-for-test/); assert.equal(h.timers.size, 0);
  const other = harness(); await issueCode(other); await other.click("tab", "teachers"); assert.doesNotMatch(other.html(), /one-time-code-for-test/); assert.equal(other.timers.size, 0);
});

test("unknown code issuance cannot be retried or recovered via an account query", async () => {
  const h = harness({ call: (method, url, body, fallback) => { if (method === "POST") throw failure(0, "签码响应中断"); return fallback(method, url, body); } });
  await issueCode(h); await h.submit("accounts"); await h.click("recheck", "accounts");
  assert.equal(h.writes().length, 1); assert.match(h.html(), /账号查询不能取回一次性码或确认签发结果/); assert.doesNotMatch(h.html(), /data-mgmt-code/);
  await h.click("close-editor", "accounts"); await h.click("reset-code", "student1"); h.field("accounts", "confirmed", true); await h.submit("accounts"); assert.equal(h.writes().length, 1);
});

test("empty budget list means unconfigured, uses server currency and requires raw safe integers", async () => {
  const h = harness(); await openPlatform(h);
  assert.match(h.html(), /尚未配置额度策略/); assert.match(h.html(), /site-credit/); assert.match(h.html(), /name="limit_units"[^>]*value=""/);
  h.field("budgets", "confirmed", true);
  for (const value of ["", "-1", "1.5", "1e3", "9007199254740992"]) { h.field("budgets", "limit_units", value); await h.submit("budgets"); assert.equal(h.writes().length, 0); }
  h.field("budgets", "limit_units", "0"); h.field("budgets", "emergency_units", "0"); await h.submit("budgets");
  assert.deepEqual(h.writes()[0].body, { scope_type: "project", scope_id: "platform", class_id: null, period: null, limit_units: 0, emergency_units: 0 });
  assert.equal(h.writes()[0].method, "PUT"); assert.equal(h.writes()[0].url, "/budgets/limits"); assert.doesNotMatch(h.html(), /data-mgmt-form="budgets"/); assert.match(h.html(), /未进行充值或现金兑换/);
});

test("student global daily policy targets the chosen real account with no class, no date, and zero emergency", async () => {
  const h = harness({ budgets: [limit()] }); await h.workspace.load(); await h.click("tab", "accounts"); await h.click("student-budget", "student1");
  assert.match(h.html(), /学生全局日额：姓名student1/); h.field("budgets", "limit_units", "125"); h.field("budgets", "emergency_units", "999"); h.field("budgets", "confirmed", true); await h.submit("budgets");
  assert.deepEqual(h.writes()[0].body, { scope_type: "student_daily", scope_id: "student1", class_id: null, period: null, limit_units: 125, emergency_units: 0 });
});

test("switching budget targets never carries another account's limit or confirmation", async () => {
  const h = harness({ accounts: [user("student1"), user("student2")], budgets: [limit({ scope_type: "student_daily", scope_id: "student1", emergency_units: 0, limit_units: 17 }), limit({ scope_type: "student_daily", scope_id: "student2", emergency_units: 0, limit_units: 91 })] });
  await h.workspace.load(); await h.click("tab", "accounts"); await h.click("student-budget", "student1"); h.field("budgets", "limit_units", "777"); h.field("budgets", "confirmed", true);
  await h.click("tab", "accounts"); await h.click("student-budget", "student2");
  assert.match(h.html(), /name="limit_units"[^>]*value="91"/); assert.doesNotMatch(h.html(), /name="confirmed"[^>]*checked/);
});

test("budget metadata failure blocks saving, and scoped policies remain read-only", async () => {
  const malformed = harness({ call: (method, url, body, fallback) => url === "/budgets/limits" ? { items: [], currency: "CNY" } : fallback(method, url, body) });
  await malformed.workspace.load(); await malformed.click("tab", "budgets"); await malformed.click("platform-budget");
  assert.match(malformed.html(), /额度元数据尚未成功读取/); assert.equal(malformed.writes().length, 0);
  const h = harness({ budgets: [limit({ scope_type: "student_daily", scope_id: "student1", course_id: "c1", class_id: "cl1", emergency_units: 0 }), limit({ scope_type: "student_daily", scope_id: "student1", period: "2026-11-16", emergency_units: 0 })] });
  await h.workspace.load(); await h.click("tab", "budgets"); await h.click("edit-budget", "0"); await h.click("edit-budget", "1");
  assert.doesNotMatch(h.html(), /data-mgmt-form="budgets"/); assert.match(h.html(), /只读/);
});

test("ambiguous budget save preserves input and rechecks only the stored policy", async () => {
  const h = harness({ budgets: [limit()], call: (method, url, body, fallback) => { if (method === "PUT") throw failure(500, "额度响应中断"); return fallback(method, url, body); } });
  await openPlatform(h); h.field("budgets", "limit_units", "500"); h.field("budgets", "emergency_units", "20"); h.field("budgets", "confirmed", true); await h.submit("budgets"); await h.submit("budgets");
  assert.match(h.html(), /value="500"/); await h.click("recheck", "budgets"); assert.match(h.html(), /当前存储额度与提交值一致/); assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0);
});

test("lost receipt for saved budget 500 is confirmed by GET, retires the old form, and allows a fresh save of 600", async () => {
  let puts = 0;
  const h = harness({ budgets: [limit({ limit_units: 100, emergency_units: 0 })], call: async (method, url, body, fallback) => {
    const result = await fallback(method, url, body);
    if (method === "PUT" && ++puts === 1) throw failure(0, "额度已存储但回执丢失");
    return result;
  } });
  await openPlatform(h); h.field("budgets", "limit_units", "500"); h.field("budgets", "emergency_units", "0"); h.field("budgets", "confirmed", true); await h.submit("budgets");
  assert.equal(h.records.budgets[0].limit_units, 500); assert.equal(h.writes().length, 1); assert.match(h.html(), /提交结果尚未确认/);
  await h.click("recheck", "budgets");
  assert.match(h.html(), /当前存储额度与提交值一致/); assert.match(h.html(), /<td>500<small class="mgmt-muted">应急 0/);
  assert.match(h.html(), /data-mgmt-form="budgets"><fieldset class="mgmt-fields" disabled/);
  assert.doesNotMatch(h.html(), /data-mgmt-action="recheck"/); assert.equal(h.changed(), 0);
  const reads = h.calls.length;
  h.field("budgets", "limit_units", "600"); h.field("budgets", "confirmed", true); await h.submit("budgets"); await h.click("recheck", "budgets");
  assert.equal(h.calls.length, reads, "the reconciled editor cannot edit, replay, or recheck the old command");
  assert.match(h.html(), /name="limit_units"[^>]*value="500"/);
  await h.click("close-editor", "budgets"); await h.click("platform-budget");
  assert.match(h.html(), /name="limit_units"[^>]*value="500"/); assert.doesNotMatch(h.html(), /name="confirmed"[^>]*checked/);
  h.field("budgets", "limit_units", "600"); await h.submit("budgets"); assert.equal(h.writes().length, 1, "the new command needs a new confirmation");
  h.field("budgets", "confirmed", true); await h.submit("budgets");
  assert.deepEqual(h.writes().map((item) => item.body.limit_units), [500, 600]); assert.equal(h.records.budgets[0].limit_units, 600);
  assert.equal(h.changed(), 1); assert.doesNotMatch(h.html(), /data-mgmt-form="budgets"/);
});

for (const overrides of [{ limit_units: 499 }, { emergency_units: 21 }, { configured: false }]) test(`budget recheck mismatch ${JSON.stringify(overrides)} retains its lock after closing and reopening`, async () => {
  const h = harness({ budgets: [limit(overrides)], call: (method, url, body, fallback) => {
    if (method === "PUT") throw failure(0, "结果未知");
    return fallback(method, url, body);
  } });
  await openPlatform(h); h.field("budgets", "limit_units", "500"); h.field("budgets", "emergency_units", "20"); h.field("budgets", "confirmed", true); await h.submit("budgets");
  await h.click("recheck", "budgets"); assert.match(h.html(), /与提交值尚不一致/);
  await h.click("close-editor", "budgets"); await h.click("platform-budget");
  h.field("budgets", "limit_units", "600"); h.field("budgets", "confirmed", true); await h.submit("budgets");
  assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0); assert.match(h.html(), /提交结果尚未确认/);
});

for (const status of ["active", "disabled"]) test(`reconciled account change from ${status} ends the old form and allows the opposite action`, async () => {
  const target = status === "active" ? "disabled" : "active";
  const action = status === "active" ? "account-disable" : "account-restore";
  const opposite = target === "disabled" ? "account-restore" : "account-disable";
  let patches = 0;
  const h = harness({ accounts: [user("student1", "student", status)], call: async (method, url, body, fallback) => {
    const result = await fallback(method, url, body);
    if (method === "PATCH" && ++patches === 1) throw failure(0, "账号状态已更新但回执丢失");
    return result;
  } });
  await openAccount(h, action); h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.equal(h.records.accounts[0].status, target); await h.click("recheck", "accounts");
  assert.match(h.html(), /目标状态已满足/); assert.doesNotMatch(h.html(), /data-mgmt-action="recheck"/);
  h.field("accounts", "confirmed", true); await h.submit("accounts"); assert.equal(h.writes().length, 1);
  await h.click("close-editor", "accounts"); await h.click(opposite, "student1");
  assert.match(h.html(), /data-mgmt-form="accounts"/); assert.doesNotMatch(h.html(), /name="confirmed"[^>]*checked/);
  await h.submit("accounts"); assert.equal(h.writes().length, 1);
  h.field("accounts", "confirmed", true); await h.submit("accounts");
  assert.deepEqual(h.writes().map((item) => item.body.status), [target, status]); assert.equal(h.records.accounts[0].status, status); assert.equal(h.changed(), 1);
});

test("account status mismatch cannot release the old mutation lock", async () => {
  const h = harness({ call: (method, url, body, fallback) => { if (method === "PATCH") throw failure(0, "账号状态结果未知"); return fallback(method, url, body); } });
  await openAccount(h, "account-disable"); h.field("accounts", "confirmed", true); await h.submit("accounts"); await h.click("recheck", "accounts");
  assert.match(h.html(), /原操作结果仍需核对/); await h.click("close-editor", "accounts"); await h.click("account-disable", "student1");
  h.field("accounts", "confirmed", true); await h.submit("accounts"); assert.equal(h.writes().length, 1); assert.equal(h.changed(), 0);
});

test("permission revocation removes records and visible codes and prevents other parallel reads restoring them", async () => {
  let revoke = false;
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => { if (revoke && url === "/auth/teacher-applications") throw failure(403, "管理员权限已撤销"); if (revoke && url.startsWith("/auth/accounts?")) return gate.promise; return fallback(method, url, body); } });
  await issueCode(h); revoke = true; const loading = h.workspace.load(); gate.resolve({ items: [user("PRIVATE")], total: 1, limit: 50, offset: 0 }); await loading;
  assert.match(h.html(), /已无权访问管理员功能/); assert.doesNotMatch(h.html(), /PRIVATE|one-time-code|姓名teacher1/); assert.equal(h.timers.size, 0);
  const count = h.calls.length; await h.click("reset-code", "student1"); await h.submit("accounts"); assert.equal(h.calls.length, count);
});

test("untrusted values are escaped and account projections never render unsafe extra fields", async () => {
  const h = harness({ accounts: [user("teacher1", "teacher", "pending", { display_name: '<script>name</script>', password_hash: "SHOULD-NOT-APPEAR", reset_code: "PRIVATE-EXTRA-CODE" })] });
  await h.workspace.load(); assert.match(h.html(), /&lt;script&gt;name&lt;\/script&gt;/); assert.doesNotMatch(h.html(), /<script>|SHOULD-NOT-APPEAR|PRIVATE-EXTRA-CODE/);
  await h.click("teacher-reject", "teacher1"); h.field("teachers", "note", '<img src=x onerror="bad()">'); assert.match(h.html(), /&lt;img/); assert.doesNotMatch(h.html(), /<img/);
});

test("empty lists and malformed pages do not become fabricated success or empty-state results", async () => {
  const h = harness({ accounts: [], courses: [], budgets: [] }); await h.workspace.load(); assert.match(h.html(), /暂无待审核教师申请/);
  await h.click("tab", "courses"); assert.match(h.html(), /暂无课程申请/); await h.click("tab", "accounts"); assert.match(h.html(), /暂无可显示的账号/);
  const bad = harness({ call: (method, url, body, fallback) => url.startsWith("/auth/accounts?") ? {} : fallback(method, url, body) });
  await bad.workspace.load(); await bad.click("tab", "accounts"); assert.match(bad.html(), /未返回可核验/); assert.doesNotMatch(bad.html(), /暂无可显示的账号|找到 0 个账号/);
});

test("delegated events stay inside the bound workspace and old roots cannot trigger commands", async () => {
  const h = harness(); await h.workspace.load();
  const forged = h.node({ mgmtAction: "teacher-approve", mgmtValue: "teacher1" }); forged.owner = {};
  await h.events.click({ target: forged, currentTarget: h.root }); assert.doesNotMatch(h.html(), /data-mgmt-form="teachers"/);
  const oldHandler = h.events.click; const oldRoot = h.root; h.workspace.bind({ querySelector: () => null });
  await oldHandler({ target: h.node({ mgmtAction: "teacher-approve", mgmtValue: "teacher1" }), currentTarget: oldRoot }); assert.equal(h.writes().length, 0);
});

test("failed host refresh cannot turn a confirmed mutation into a second submission", async () => {
  const h = harness({ changedFailure: true }); await openTeacher(h, true); h.field("teachers", "confirmed", true); await h.submit("teachers");
  assert.match(h.html(), /相关记录刷新未完成/); assert.doesNotMatch(h.html(), /data-mgmt-form="teachers"/); await h.submit("teachers"); assert.equal(h.writes().length, 1);
});
