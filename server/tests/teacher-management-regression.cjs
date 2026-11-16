"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-management.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const error = (status, message) => Object.assign(new Error(message), { status });
const unescape = (value) => value.replace(/&(?:amp|lt|gt|quot|#39);/g, (item) => ({ "&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'" }[item]));

// A small DOM fixture parses production markup so handlers must pass real containment/disabled checks.
function surface() {
  const events = {}, doc = { activeElement: null };
  class Element {
    constructor(tag, attrs = {}) {
      this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.parentElement = null;
      this.ownerDocument = doc; this.value = attrs.value || "";
      this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value]));
    }
    get isConnected() { return this === doc.documentElement || Boolean(this.parentElement?.isConnected); }
    get disabled() { return "disabled" in this.attrs; }
    get type() { return this.attrs.type || "text"; }
    getAttribute(key) { return this.attrs[key] ?? null; }
    contains(node) { for (let current = node; current; current = current.parentElement) if (current === this) return true; return false; }
    matches(selector) {
      return selector.split(",").some((part) => {
        const value = part.trim();
        if (value === ":disabled") return this.disabled || Boolean(this.closest("fieldset[disabled]"));
        const match = value.match(/^(\w+)?(?:\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\])?$/);
        return Boolean(match && (!match[1] || match[1].toUpperCase() === this.tagName) && (!match[2] || (match[2] in this.attrs && (match[3] == null || match[3] === this.attrs[match[2]]))));
      });
    }
    closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
    querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    append(child) { child.parentElement = this; this.children.push(child); return child; }
    focus() { if (this.isConnected && !this.matches(":disabled")) doc.activeElement = this; }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    addEventListener(name, handler) { if (this === root) events[name] = handler; }
    set innerHTML(html) {
      this.markup = html;
      if (this.contains(doc.activeElement)) doc.activeElement = doc.documentElement;
      for (const child of this.children) child.parentElement = null;
      this.children = [];
      const stack = [this], voids = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
      for (const token of html.matchAll(/<\/?([a-z][\w-]*)\b([^>]*)>/gi)) {
        const tag = token[1].toUpperCase();
        if (token[0].startsWith("</")) { if (!voids.has(tag) && stack.length > 1) stack.pop(); continue; }
        const attrs = {};
        for (const match of token[2].matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[match[1]] = unescape(match[2] ?? match[3] ?? match[4] ?? "");
        const child = stack.at(-1).append(new Element(tag, attrs));
        if (!voids.has(tag)) stack.push(child);
      }
    }
    get innerHTML() { return this.markup || ""; }
  }
  doc.documentElement = new Element("html");
  const root = doc.documentElement.append(new Element("section", { "data-teacher-management": "" }));
  doc.activeElement = doc.documentElement;
  return { root, doc, events, Element };
}

function fixture(options = {}) {
  const dom = surface(), calls = [], changes = [], toasts = [], copied = [], logs = [], storage = [];
  const state = { live: true, role: "teacher", transitionId: 1, courseId: "c1", classId: "cl1", teacherView: "management", ...options.state };
  const user = { id: "t1", username: "teacher", display_name: "本人教师", role: state.role, status: "active", ...options.user };
  const course = { id: "c1", name: "高等数学", term: "2026 年秋季", teacher_id: "t1", can_manage_materials: true, can_create_classes: true, can_manage_collaborators: true };
  const classroom = { id: "cl1", course_id: "c1", name: "一班", owner_teacher_id: "t1", can_manage: true, can_manage_permissions: true, can_set_student_budgets: true };
  const model = {
    courses: plain(options.courses ?? [course]), classes: plain(options.classes ?? [classroom]), applications: plain(options.applications ?? []),
    members: plain(options.members ?? { cl1: [
      { user_id: "s-pending", username: "pending.student", display_name: "待激活同学", status: "pending" },
      { user_id: "s-active", username: "active.student", display_name: "有效同学", status: "active" },
      { user_id: "s-disabled", username: "disabled.student", display_name: "停用同学", status: "disabled" },
    ] }),
    budgets: plain(options.budgets ?? []), currency: options.currency === undefined ? "site-credit" : options.currency,
    unit: options.unit === undefined ? "billing_unit" : options.unit, unit_scale: options.unitScale === undefined ? 1 : options.unitScale,
  };
  let serial = 0, appRenders = 0, management;
  const makeLimit = (body) => ({ ...body, course_id: model.classes.find((item) => item.id === body.class_id)?.course_id, configured: true, spent_units: 0, reserved_units: 0, overage_units: 0, emergency_exceeded_units: 0, notification_required: false });
  async function fallback(method, url, body) {
    if (method === "GET") {
      if (url === "/courses") return plain(model.courses);
      if (url === "/classes") return plain(model.classes);
      if (url === "/course-applications") return plain(model.applications);
      let match;
      if ((match = url.match(/^\/courses\/([^/]+)\/classes$/))) return plain(model.classes.filter((item) => item.course_id === decodeURIComponent(match[1])));
      if ((match = url.match(/^\/classes\/([^/]+)\/members$/))) return plain(model.members[decodeURIComponent(match[1])] || []);
      if (url.startsWith("/budgets/limits?")) return { items: plain(model.budgets), currency: model.currency, unit: model.unit, unit_scale: model.unit_scale, unit_description: "整数计费单位" };
      throw new Error(`Unexpected read ${url}`);
    }
    if (url === "/course-applications" && method === "POST") {
      const record = { ...body, id: `a${++serial}`, applicant_id: user.id, status: "pending", review_note: "", course_id: null, created_at: 1700000000 };
      model.applications.push(record); return plain(record);
    }
    const createClass = url.match(/^\/courses\/([^/]+)\/classes$/);
    if (createClass && method === "POST") {
      const record = { ...body, id: `new-class-${++serial}`, course_id: decodeURIComponent(createClass[1]), can_manage: true, can_set_student_budgets: true };
      model.classes.push(record); return plain(record);
    }
    if (url === "/auth/students") {
      const id = `new-student-${++serial}`;
      (model.members[body.class_id] ||= []).push({ user_id: id, username: body.username, display_name: body.display_name, status: "pending" });
      return { student_id: id, activation_code: `CREATED-SECRET-${serial}`, expires_in_seconds: 86400 };
    }
    if (url === "/auth/activation-codes") return { user_id: body.user_id, activation_code: `ACTIVATION-SECRET-${++serial}`, expires_in_seconds: 86400 };
    if (url === "/auth/password-reset-codes") return { user_id: body.user_id, reset_code: `RESET-SECRET-${++serial}`, expires_in_seconds: 3600 };
    const membership = url.match(/^\/classes\/([^/]+)\/members(?:\/([^/]+))?(\/transfer)?$/);
    if (membership) {
      const classId = decodeURIComponent(membership[1]), memberId = membership[2] && decodeURIComponent(membership[2]);
      if (method === "POST" && !memberId) {
        (model.members[classId] ||= []).push({ user_id: body.user_id, username: "existing.student", display_name: "已有学生", status: "active" });
        return { course_id: model.classes.find((item) => item.id === classId)?.course_id, class_id: classId, user_id: body.user_id };
      }
      if (method === "DELETE" || membership[3]) {
        const student = model.members[classId].find((item) => item.user_id === memberId);
        model.members[classId] = model.members[classId].filter((item) => item.user_id !== memberId);
        if (membership[3]) { (model.members[body.target_class_id] ||= []).push(student); return { course_id: "c1", class_id: body.target_class_id, user_id: memberId }; }
        return null;
      }
    }
    if (url === "/budgets/limits" && method === "PUT") {
      const record = makeLimit(body);
      model.budgets = [...model.budgets.filter((item) => item.scope_id !== body.scope_id || item.class_id !== body.class_id || item.period != null), record];
      return plain(record);
    }
    throw new Error(`Unexpected write ${method} ${url}`);
  }
  const api = { state: { user }, call: (method, url, body) => {
    calls.push([method, url, body === undefined ? undefined : plain(body)]);
    return options.call ? options.call(method, url, body, fallback, model) : fallback(method, url, body);
  } };
  const blockedStorage = new Proxy({}, { get: (_, property) => { storage.push(property); throw new Error("Persistent storage must not be accessed"); } });
  const context = vm.createContext({ URLSearchParams, console: { log: (...args) => logs.push(args), error: (...args) => logs.push(args), warn: (...args) => logs.push(args) }, navigator: { clipboard: { writeText: async (text) => { copied.push(text); if (options.copy) await options.copy(text); } } }, localStorage: blockedStorage, sessionStorage: blockedStorage });
  vm.runInContext(source.replace("export function createTeacherManagement", "function createTeacherManagement") + "\nglobalThis.makeManagement = createTeacherManagement;", context, { filename: sourcePath });
  management = context.makeManagement({ api, state, toast: (text) => toasts.push(text), render: () => { appRenders += 1; }, onChanged: async (selection) => {
    changes.push(plain(selection));
    if (options.onChanged) return options.onChanged(selection, { management, state, api, model });
    if (options.refreshOnChange !== false) {
      state.courseId = selection.courseId; state.classId = selection.classId;
      await management.load();
    }
  } });
  const hostRender = () => { dom.root.innerHTML = management.render().replace(/^<section[^>]*>/, "").replace(/<\/section>$/, ""); management.bind(dom.root); };
  hostRender();
  function action(name, id) { return dom.root.querySelectorAll("[data-mgmt-action]").find((item) => item.dataset.mgmtAction === name && (id == null || item.dataset.mgmtId === id)); }
  function form(name, id) { return dom.root.querySelectorAll("[data-mgmt-form]").find((item) => item.dataset.mgmtForm === name && (id == null || item.dataset.mgmtId === id)); }
  function input(scope, name, id) { return dom.root.querySelectorAll("[data-mgmt-field]").find((item) => item.dataset.mgmtScope === scope && item.dataset.mgmtField === name && (id == null || item.dataset.mgmtId === id)); }
  return {
    management, state, api, model, calls, changes, toasts, copied, logs, storage, dom, hostRender, action, form, input,
    html: () => management.render(), writes: () => calls.filter(([method]) => method !== "GET"), appRenders: () => appRenders,
    click: (name, id) => { const target = action(name, id); assert.ok(target, `action ${name}/${id || ""} exists`); return dom.events.click({ target }); },
    field: (scope, name, value, id, type = "input") => { const target = input(scope, name, id); assert.ok(target, `input ${scope}/${name} exists`); target.value = value; target.focus(); return dom.events[type]({ target, type }); },
    submit: (name, id) => { const target = form(name, id); assert.ok(target, `form ${name}/${id || ""} exists`); return dom.events.submit({ target, preventDefault() {} }); },
    dispatchStale: (target, event = "click") => dom.events[event]({ target, preventDefault() {} }),
    forgeAction: (name, id) => { const target = dom.root.append(new dom.Element("button", { "data-mgmt-action": name, "data-mgmt-id": id })); return dom.events.click({ target }); },
    forgeForm: (name, id) => { const target = dom.root.append(new dom.Element("form", { "data-mgmt-form": name, "data-mgmt-id": id })); return dom.events.submit({ target, preventDefault() {} }); },
    makeLimit,
  };
}

async function studentDraft(h, username = "new.student", displayName = "新同学") {
  await h.click("tab", "members"); h.field("student", "username", username); h.field("student", "display_name", displayName);
}

async function until(predicate) {
  for (let index = 0; index < 30; index += 1) { if (predicate()) return; await Promise.resolve(); }
  assert.ok(predicate(), "the expected asynchronous request was reached");
}

test("offline, unauthenticated, inactive, and student contexts perform no API requests", async () => {
  for (const options of [{ state: { live: false } }, { state: { role: "student" } }, { user: { id: "" } }, { user: { status: "disabled" } }]) {
    const h = fixture(options); await h.management.load(); assert.equal(h.calls.length, 0); assert.doesNotMatch(h.html(), /data-mgmt-form=/);
  }
  assert.doesNotMatch(source, /\bdata-(?:action|view)=/);
  assert.doesNotMatch(source, /\b(?:localStorage|sessionStorage|indexedDB|console)\b/);
});

test("no-course teachers can submit applications and see only their own status and escaped review notes", async () => {
  const h = fixture({ state: { courseId: null, classId: null }, courses: [], classes: [], members: {}, applications: [
    { id: "mine", applicant_id: "t1", name: "原有课程", term: "春季", status: "rejected", review_note: "<script>复核</script>" },
    { id: "other", applicant_id: "other-teacher", name: "别人的申请", term: "秋季", status: "pending" },
  ] });
  await h.management.load(); assert.match(h.dom.root.innerHTML, /还没有可访问的课程/); assert.equal(h.changes.length, 0);
  assert.doesNotMatch(h.html(), /别人的申请|<script>/); assert.match(h.html(), /&lt;script&gt;复核/);
  h.field("application", "name", "新课程"); h.field("application", "term", "2027 春季"); await h.submit("application");
  assert.deepEqual(h.writes()[0], ["POST", "/course-applications", { name: "新课程", term: "2027 春季" }]);
  assert.equal(h.changes.length, 1); assert.match(h.dom.root.innerHTML, /待审核/); assert.equal(h.appRenders(), 0);
});

test("a material manager cannot infer class-creation authority; an explicit course capability permits first class creation", async () => {
  const h = fixture({ state: { classId: null }, classes: [], members: {} });
  await h.management.load(); assert.ok(h.form("class")); assert.match(h.html(), /还没有你可访问的班级/);
  h.field("classroom", "name", "第一班"); await h.submit("class");
  assert.deepEqual(h.writes()[0], ["POST", "/courses/c1/classes", { name: "第一班", owner_teacher_id: "t1" }]);
  assert.equal(h.changes[0].courseId, "c1"); assert.match(h.changes[0].classId, /new-class-/);
  const denied = fixture({ classes: [] }); denied.model.courses[0].can_create_classes = false;
  await denied.management.load(); assert.equal(denied.form("class"), undefined); await denied.forgeForm("class"); assert.equal(denied.writes().length, 0);
});

test("course selection preserves a null class ID for a real course with no classes", async () => {
  const h = fixture(); h.model.courses.push({ id: "empty", name: "无班课程", term: "秋季", can_create_classes: true });
  await h.management.load(); await h.field("selection", "course", "empty", undefined, "change");
  assert.deepEqual(h.changes, [{ courseId: "empty", classId: null }]); assert.equal(h.state.classId, null);
  assert.ok(h.form("class")); assert.match(h.html(), /无班课程的班级/);
});

test("independent class visibility supports roster management without material access", async () => {
  const h = fixture({ courses: [] }); await h.management.load(); await h.click("tab", "members");
  assert.match(h.html(), /班级所属课程/); assert.match(h.html(), /有效同学/); assert.ok(h.form("student"));
  await h.click("tab", "courses"); assert.equal(h.form("class"), undefined);
});

test("read collaborators see rosters but cannot mutate; manage collaborators never query owner-only budgets", async () => {
  for (const manage of [false, true]) {
    const h = fixture(); Object.assign(h.model.classes[0], { owner_teacher_id: "other-teacher", can_manage: manage, can_set_student_budgets: false });
    await h.management.load(); await h.click("tab", "members"); assert.match(h.html(), /有效同学/);
    assert.equal(h.calls.some(([, url]) => url.startsWith("/budgets/limits")), false);
    if (!manage) {
      assert.equal(h.form("student"), undefined); assert.equal(h.action("reset", "s-active"), undefined);
      await h.forgeForm("student"); await h.forgeAction("activation", "s-pending"); await h.forgeAction("confirm");
    }
    await h.click("tab", "budgets"); assert.match(h.html(), /班级负责人或管理员查看和设置/);
    await h.forgeForm("budget", "s-active"); assert.equal(h.writes().length, 0);
  }
});

test("admin cannot accidentally create a class owned by an admin or submit a teacher application", async () => {
  const h = fixture({ state: { role: "admin" }, user: { id: "admin1" } }); await h.management.load();
  assert.equal(h.form("application"), undefined); await h.forgeForm("application");
  h.field("classroom", "name", "管理员代建"); h.field("classroom", "owner_teacher_id", "admin1"); await h.submit("class");
  assert.equal(h.writes().length, 0); assert.match(h.html(), /管理员不能作为班级负责人/);
  h.field("classroom", "owner_teacher_id", "t-valid"); await h.submit("class"); assert.equal(h.writes()[0][2].owner_teacher_id, "t-valid");
});

test("student creation is locked against rapid submissions and same-context reload keeps its one-time code", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/students" ? gate.promise.then(() => fallback(method, url, body)) : fallback(method, url, body) });
  await h.management.load(); await studentDraft(h);
  const pending = h.submit("student"); await h.submit("student"); await h.management.load();
  assert.equal(h.writes().length, 1); gate.resolve(); await pending;
  assert.equal(h.changes.length, 1); assert.match(h.dom.root.innerHTML, /CREATED-SECRET-1/);
  await h.management.load(); assert.match(h.dom.root.innerHTML, /CREATED-SECRET-1/); assert.equal(h.changes.length, 1);
  await h.click("copy-code"); assert.deepEqual(h.copied, ["CREATED-SECRET-1"]); assert.equal(h.storage.length, 0); assert.equal(h.logs.length, 0);
  assert.doesNotMatch(h.toasts.join(" "), /CREATED-SECRET/); await h.click("clear-code"); assert.doesNotMatch(h.html(), /CREATED-SECRET/);
});

test("only pending students get activation codes and only active students get reset codes; issuing requires confirmation", async () => {
  const h = fixture(); await h.management.load(); await h.click("tab", "members");
  assert.ok(h.action("activation", "s-pending")); assert.equal(h.action("reset", "s-pending"), undefined);
  assert.ok(h.action("reset", "s-active")); assert.equal(h.action("activation", "s-active"), undefined);
  assert.equal(h.action("reset", "s-disabled"), undefined); assert.equal(h.action("activation", "s-disabled"), undefined);
  await h.forgeAction("activation", "s-active"); await h.forgeAction("reset", "s-disabled"); await h.forgeAction("confirm"); assert.equal(h.writes().length, 0);
  await h.click("activation", "s-pending"); assert.equal(h.writes().length, 0); await h.click("confirm");
  assert.deepEqual(h.writes()[0], ["POST", "/auth/activation-codes", { user_id: "s-pending", course_id: "c1", class_id: "cl1" }]);
  assert.match(h.html(), /ACTIVATION-SECRET/);
  await h.click("reset", "s-active"); await h.click("confirm");
  assert.deepEqual(h.writes()[1], ["POST", "/auth/password-reset-codes", { user_id: "s-active", course_id: "c1", class_id: "cl1" }]);
  assert.match(h.html(), /RESET-SECRET/); assert.doesNotMatch(h.html(), /ACTIVATION-SECRET/);
});

test("same-context loads preserve failed form input and do not duplicate host callbacks", async () => {
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/students" ? Promise.reject(error(409, "登录名已被使用")) : fallback(method, url, body) });
  await h.management.load(); await studentDraft(h, "keep.me", "保留姓名"); await h.submit("student");
  h.hostRender(); await h.management.load();
  assert.equal(h.input("student", "username").value, "keep.me"); assert.equal(h.input("student", "display_name").value, "保留姓名");
  assert.match(h.html(), /登录名已被使用/); assert.equal(h.changes.length, 0); assert.equal(h.writes().length, 1);
});

test("unknown student result blocks repeat creation until a read confirms the account, without auto-retrying the POST", async () => {
  const h = fixture({ call: async (method, url, body, fallback) => {
    if (url === "/auth/students") { await fallback(method, url, body); throw error(0, "连接中断"); }
    return fallback(method, url, body);
  } });
  await h.management.load(); await studentDraft(h); await h.submit("student"); await h.submit("student");
  assert.equal(h.writes().length, 1); assert.equal(h.changes.length, 0); assert.match(h.html(), /提交结果暂无法确认/);
  await h.click("acknowledge"); await h.submit("student"); assert.equal(h.writes().length, 1);
  await h.click("reconcile"); assert.match(h.html(), /已查询到与本次操作一致的记录/);
  assert.match(h.html(), /重发激活码/); assert.doesNotMatch(h.html(), /CREATED-SECRET/);
  await h.click("acknowledge"); assert.equal(h.input("student", "username").value, ""); assert.equal(h.writes().length, 1);
});

test("failed reconciliation cannot unlock an uncertain write and a complete empty query requires manual acknowledgment", async () => {
  let failMembers = false;
  const h = fixture({ call: (method, url, body, fallback) => {
    if (url === "/auth/students") return Promise.reject(error(503, "服务暂不可用"));
    if (method === "GET" && /\/members$/.test(url) && failMembers) return Promise.reject(error(503, "名单暂不可用"));
    return fallback(method, url, body);
  } });
  await h.management.load(); await studentDraft(h, "uncertain.student", "输入保留"); await h.submit("student");
  failMembers = true; await h.click("reconcile"); assert.match(h.html(), /尚未完成结果核对/); await h.click("acknowledge");
  assert.match(h.html(), /先核对上一次操作/); assert.equal(h.writes().length, 1);
  failMembers = false; await h.click("reconcile"); assert.match(h.html(), /暂未查到与本次操作一致的记录/);
  await h.submit("student"); assert.equal(h.writes().length, 1); await h.click("acknowledge");
  assert.equal(h.input("student", "username").value, "uncertain.student"); assert.equal(h.input("student", "display_name").value, "输入保留");
});

test("uncertain code issuance never auto-reissues and requires refreshed roster plus a new confirmation", async () => {
  let fail = true;
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" && fail ? Promise.reject(error(0, "断网")) : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending"); await h.click("confirm");
  await h.click("reconcile"); assert.equal(h.writes().length, 1); assert.match(h.html(), /历史一次性码无法查询/);
  await h.click("acknowledge"); fail = false; await h.click("activation", "s-pending"); assert.equal(h.writes().length, 1);
  await h.click("confirm"); assert.equal(h.writes().length, 2); assert.match(h.html(), /ACTIVATION-SECRET/);
});

test("confirmed writes stay confirmed when host refresh fails", async () => {
  const h = fixture({ onChanged: async () => { throw new Error("host reload failed"); } });
  await h.management.load(); await studentDraft(h); await h.submit("student");
  assert.match(h.html(), /列表刷新未完成/); assert.match(h.html(), /CREATED-SECRET/); assert.doesNotMatch(h.html(), /先核对上一次操作/);
  assert.equal(h.changes.length, 1); assert.equal(h.writes().length, 1);
});

test("older course reads cannot overwrite a newer course and latest same-context load wins", async () => {
  const old = deferred(); let oldReads = 0;
  const h = fixture({ call: (method, url, body, fallback) => url === "/courses/c1/classes" && ++oldReads === 1 ? old.promise : fallback(method, url, body) });
  h.model.courses.push({ id: "c2", name: "新课程", can_create_classes: true });
  h.model.classes.push({ id: "cl2", course_id: "c2", name: "新班级", can_manage: true, can_set_student_budgets: false });
  h.model.members.cl2 = [{ user_id: "new", username: "new", display_name: "当前班学生", status: "active" }];
  const pending = h.management.load(); await until(() => h.calls.some(([, url]) => url === "/courses/c1/classes"));
  h.state.courseId = "c2"; h.state.classId = "cl2"; await h.management.load(); await h.click("tab", "members");
  old.resolve([{ id: "old-class", course_id: "c1", name: "旧班私有数据", can_manage: true }]); await pending;
  assert.match(h.dom.root.innerHTML, /当前班学生/); assert.doesNotMatch(h.html(), /旧班私有数据/);
  const earlier = deferred(); let reads = 0;
  const latest = fixture({ call: (method, url, body, fallback) => {
    if (url === "/classes/cl1/members") { reads += 1; if (reads === 1) return earlier.promise; return [{ user_id: "latest", username: "latest", display_name: "最新学生", status: "active" }]; }
    return fallback(method, url, body);
  } });
  const first = latest.management.load(); await until(() => reads === 1); await latest.management.load(); await latest.click("tab", "members");
  earlier.resolve([{ user_id: "stale", username: "stale", display_name: "过期学生", status: "active" }]); await first;
  assert.match(latest.dom.root.innerHTML, /最新学生/); assert.doesNotMatch(latest.html(), /过期学生/);
});

test("late successful and failed mutations after reset or identity changes cannot display codes, callbacks, or errors", async () => {
  for (const mode of ["reset-success", "identity-success", "transition-error", "class-success"]) {
    const gate = deferred();
    const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise : fallback(method, url, body) });
    await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending");
    const oldConfirm = h.action("confirm"), pending = h.click("confirm");
    if (mode.startsWith("reset")) h.management.reset();
    if (mode.startsWith("identity")) h.api.state.user = { ...h.api.state.user, id: "other-session" };
    if (mode.startsWith("transition")) h.state.transitionId += 1;
    if (mode.startsWith("class")) h.state.classId = "another-class";
    h.hostRender();
    if (mode.endsWith("error")) gate.reject(error(500, "旧请求错误"));
    else gate.resolve({ user_id: "s-pending", activation_code: "OLD-PRIVATE-CODE", expires_in_seconds: 86400 });
    await pending; await h.dispatchStale(oldConfirm);
    assert.doesNotMatch(h.dom.root.innerHTML, /OLD-PRIVATE-CODE|旧请求错误/); assert.doesNotMatch(h.html(), /OLD-PRIVATE-CODE/);
    assert.equal(h.changes.length, 0); assert.equal(h.toasts.length, 0); assert.equal(h.writes().length, 1);
  }
});

test("reset clears a visible code immediately and a tab change also removes it", async () => {
  const h = fixture(); await h.management.load(); await studentDraft(h); await h.submit("student");
  assert.match(h.html(), /CREATED-SECRET/); await h.click("tab", "courses"); assert.doesNotMatch(h.html(), /CREATED-SECRET/);
  await h.click("tab", "members"); await h.click("activation", "s-pending"); await h.click("confirm");
  assert.match(h.dom.root.innerHTML, /ACTIVATION-SECRET/); h.management.reset(); assert.equal(h.dom.root.innerHTML, "");
  h.hostRender(); await h.management.load(); assert.doesNotMatch(h.html(), /ACTIVATION-SECRET|CREATED-SECRET/);
});

test("permission revocation clears a visible code and forged writes stay blocked", async () => {
  const h = fixture(); await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending"); await h.click("confirm");
  Object.assign(h.model.classes[0], { can_manage: false, can_set_student_budgets: false });
  await h.management.load(); assert.doesNotMatch(h.html(), /ACTIVATION-SECRET/); await h.forgeForm("student");
  assert.equal(h.writes().length, 1); assert.equal(h.form("add"), undefined);
});

test("membership additions, removals and transfers use exact routes and confirmation prevents duplicate removals", async () => {
  const h = fixture(); h.model.classes.push({ id: "cl2", course_id: "c1", name: "二班", can_manage: true, can_set_student_budgets: false });
  await h.management.load(); await h.click("tab", "members");
  h.field("member", "user_id", "existing-id"); await h.submit("add");
  assert.deepEqual(h.writes()[0], ["POST", "/classes/cl1/members", { user_id: "existing-id" }]);
  await h.click("remove", "existing-id"); assert.equal(h.writes().length, 1); await h.click("confirm");
  assert.deepEqual(h.writes()[1], ["DELETE", "/classes/cl1/members/existing-id", undefined]);
  await h.forgeAction("remove", "existing-id"); await h.forgeAction("confirm"); assert.equal(h.writes().length, 2);
  await h.click("transfer", "s-active"); h.field("confirmation", "target_class_id", "cl2", undefined, "change"); await h.click("confirm");
  assert.deepEqual(h.writes()[2], ["POST", "/classes/cl1/members/s-active/transfer", { target_class_id: "cl2" }]);
  assert.equal(h.model.members.cl2[0].user_id, "s-active");
});

test("budget edits need explicit units and currency, preserve integers exactly, and never guess an unconfigured value", async () => {
  const h = fixture(); await h.management.load(); await h.click("tab", "budgets");
  assert.equal(h.input("budget", "limit_units", "s-active").value, ""); assert.match(h.html(), /尚未配置本班日额/);
  h.field("budget", "limit_units", "37", "s-active"); await h.submit("budget", "s-active");
  assert.deepEqual(h.writes()[0], ["PUT", "/budgets/limits", { scope_type: "student_daily", scope_id: "s-active", class_id: "cl1", limit_units: 37, emergency_units: 0, period: null }]);
  await h.submit("budget", "s-active"); assert.equal(h.writes().length, 1);
  for (const value of ["", "-1", "1.5", "1e3", "9007199254740992"]) { h.field("budget", "limit_units", value, "s-active"); await h.submit("budget", "s-active"); }
  assert.equal(h.writes().length, 1);
  h.field("budget", "limit_units", "0", "s-active"); await h.submit("budget", "s-active"); assert.equal(h.writes()[1][2].limit_units, 0);
  assert.equal(h.form("budget", "s-disabled"), undefined);
  for (const options of [{ currency: "" }, { currency: null }, { unit: null }, { unit: "CNY" }, { unitScale: 100 }]) {
    const missing = fixture(options); await missing.management.load(); await missing.click("tab", "budgets");
    assert.equal(missing.form("budget", "s-active"), undefined); await missing.forgeForm("budget", "s-active"); assert.equal(missing.writes().length, 0);
  }
});

test("budget drafts survive validation and request failures and ignore other scopes or dated limits", async () => {
  const h = fixture({ budgets: [
    { scope_type: "student_daily", scope_id: "s-active", class_id: null, course_id: null, period: null, configured: true, limit_units: 100 },
    { scope_type: "student_daily", scope_id: "s-active", class_id: "cl1", course_id: "c1", period: "2026-11-16", configured: true, limit_units: 25 },
    { scope_type: "student_daily", scope_id: "s-active", class_id: "cl1", course_id: "c1", period: null, configured: false, limit_units: 0 },
  ], call: (method, url, body, fallback) => method === "PUT" ? Promise.reject(error(409, "额度配置冲突")) : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "budgets"); assert.equal(h.input("budget", "limit_units", "s-active").value, "");
  h.field("budget", "limit_units", "1234", "s-active"); await h.submit("budget", "s-active"); await h.management.load();
  assert.equal(h.input("budget", "limit_units", "s-active").value, "1234"); assert.match(h.html(), /额度配置冲突/);
});

test("successful read completion refreshes only module content and preserves focus and typed input", async () => {
  const h = fixture(); await h.management.load();
  h.field("application", "name", "尚未提交的课程"); const focused = h.input("application", "name"); focused.setSelectionRange(2, 4);
  await h.management.load(); assert.equal(h.dom.doc.activeElement.dataset.mgmtField, "name");
  assert.equal(h.dom.doc.activeElement.selectionStart, 2); assert.equal(h.input("application", "name").value, "尚未提交的课程");
  assert.equal(h.appRenders(), 0); assert.equal(h.changes.length, 0);
});

test("leave rejects a delayed code POST and cannot clear its lock through host rerender or reload", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise.then(() => fallback(method, url, body)) : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending");
  const pending = h.click("confirm");
  assert.equal(h.management.leave(), false); assert.match(h.toasts.at(-1), /操作正在提交/);
  h.hostRender(); await h.management.load(); await h.forgeAction("activation", "s-pending"); await h.forgeAction("confirm");
  assert.equal(h.writes().length, 1); assert.equal(h.management.leave(), false);
  gate.resolve(); await pending;
  assert.equal(h.changes.length, 1); assert.match(h.dom.root.innerHTML, /ACTIVATION-SECRET/);
  assert.equal(h.management.leave(), true); assert.equal(h.dom.root.innerHTML, "");
  h.hostRender(); await h.management.load(); assert.doesNotMatch(h.html(), /ACTIVATION-SECRET/); assert.equal(h.writes().length, 1);
});

test("leave preserves unknown-result locks until the existing query and acknowledgment finish", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending");
  const pending = h.click("confirm"); assert.equal(h.management.leave(), false);
  gate.reject(error(0, "回执未收到")); await pending;
  assert.equal(h.management.leave(), false); assert.match(h.toasts.at(-1), /尚未核对/);
  h.hostRender(); await h.forgeAction("activation", "s-pending"); await h.forgeAction("confirm"); assert.equal(h.writes().length, 1);
  await h.click("reconcile"); assert.equal(h.management.leave(), false);
  assert.match(h.html(), /历史一次性码无法查询/); await h.click("acknowledge");
  assert.equal(h.management.leave(), true); h.hostRender(); await h.management.load();
  assert.doesNotMatch(h.html(), /先核对上一次操作/); assert.equal(h.writes().length, 1);
});

test("safe leave invalidates old reads without repopulating the departed surface", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/classes/cl1/members" ? gate.promise : fallback(method, url, body) });
  const pending = h.management.load(); await until(() => h.calls.some(([, url]) => url === "/classes/cl1/members"));
  assert.equal(h.management.leave(), true); assert.equal(h.dom.root.innerHTML, "");
  gate.resolve([{ user_id: "old", username: "old", display_name: "离页旧名单", status: "active" }]); await pending;
  assert.equal(h.dom.root.innerHTML, ""); assert.equal(h.appRenders(), 0); assert.equal(h.changes.length, 0);
});

test("logout immediately blocks existing controls and forged submissions before the host repaints", async () => {
  const h = fixture(); await h.management.load(); await studentDraft(h); await h.submit("student");
  const copy = h.action("copy-code"), studentForm = h.form("student");
  assert.match(h.dom.root.innerHTML, /CREATED-SECRET/);
  h.state.loggingOut = true;
  await h.dispatchStale(copy); await h.dispatchStale(studentForm, "submit");
  for (const name of ["application", "class", "student", "add", "budget"]) await h.forgeForm(name, "s-active");
  for (const name of ["activation", "reset", "remove", "transfer", "confirm", "acknowledge", "copy-code"]) await h.forgeAction(name, "s-pending");
  const reads = h.calls.length; await h.management.load(); assert.equal(h.calls.length, reads);
  assert.equal(h.writes().length, 1); assert.equal(h.copied.length, 0);
  assert.match(h.dom.root.innerHTML, /正在退出/); assert.doesNotMatch(h.dom.root.innerHTML, /CREATED-SECRET/);
  h.state.loggingOut = false; h.hostRender(); await h.management.load();
  assert.doesNotMatch(h.html(), /CREATED-SECRET/); assert.ok(h.form("student"));
  await h.click("activation", "s-pending"); await h.click("confirm"); assert.equal(h.writes().length, 2);
});

test("logout render clears visible codes without requiring another management action", async () => {
  const h = fixture(); await h.management.load(); await studentDraft(h); await h.submit("student");
  h.state.loggingOut = true; h.hostRender();
  assert.match(h.dom.root.innerHTML, /正在退出/); assert.doesNotMatch(h.dom.root.innerHTML, /CREATED-SECRET|data-mgmt-form=/);
  h.state.loggingOut = false; h.hostRender(); assert.doesNotMatch(h.dom.root.innerHTML, /CREATED-SECRET/);
});

test("logout failure keeps a pending issuance locked and its later success settles without revealing the cleared code", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise.then(() => fallback(method, url, body)) : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending");
  const pending = h.click("confirm");
  h.state.loggingOut = true; h.hostRender(); await h.management.load();
  h.state.loggingOut = false; h.hostRender(); await h.management.load();
  assert.equal(h.management.leave(), false); await h.forgeAction("confirm"); assert.equal(h.writes().length, 1);
  gate.resolve(); await pending;
  assert.doesNotMatch(h.dom.root.innerHTML, /ACTIVATION-SECRET/); assert.match(h.html(), /一次性码已签发；退出期间已清除显示/);
  assert.equal(h.changes.length, 1); assert.equal(h.management.leave(), true);
});

test("a successful receipt during logout settles the command without starting host reload or exposing its code", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/students" ? gate.promise.then(() => fallback(method, url, body)) : fallback(method, url, body) });
  await h.management.load(); await studentDraft(h); const pending = h.submit("student");
  h.state.loggingOut = true; h.hostRender(); const count = h.calls.length;
  gate.resolve(); await pending;
  assert.equal(h.calls.length, count); assert.equal(h.changes.length, 0); assert.doesNotMatch(h.dom.root.innerHTML, /CREATED-SECRET/);
  h.state.loggingOut = false; h.hostRender();
  assert.match(h.html(), /学生已创建；退出期间已清除激活码/); assert.doesNotMatch(h.html(), /CREATED-SECRET/);
  assert.equal(h.input("student", "username").value, ""); await h.click("refresh");
  assert.match(h.html(), /新同学/); assert.equal(h.writes().length, 1); assert.equal(h.management.leave(), true);
});

test("unknown receipts during logout preserve their lock and recover through read plus acknowledgment after logout fails", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise : fallback(method, url, body) });
  await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending"); const pending = h.click("confirm");
  h.state.loggingOut = true; h.hostRender(); gate.reject(error(503, "回执未知")); await pending;
  assert.doesNotMatch(h.dom.root.innerHTML, /SECRET/); await h.forgeAction("reconcile"); await h.forgeAction("acknowledge");
  h.state.loggingOut = false; h.hostRender();
  assert.equal(h.management.leave(), false); assert.match(h.html(), /先核对上一次操作/);
  await h.forgeAction("activation", "s-pending"); await h.forgeAction("confirm"); assert.equal(h.writes().length, 1);
  await h.click("reconcile"); assert.equal(h.management.leave(), false); await h.click("acknowledge");
  assert.equal(h.management.leave(), true); assert.equal(h.changes.length, 0);
});

test("existing uncertainty and drafts survive logout attempts and forbidden acknowledgment during logout", async () => {
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/students" ? Promise.reject(error(0, "结果未知")) : fallback(method, url, body) });
  await h.management.load(); await studentDraft(h, "uncertain.keep", "保留草稿"); await h.submit("student");
  await h.click("reconcile"); assert.equal(h.action("acknowledge").disabled, false);
  h.state.loggingOut = true; h.hostRender(); await h.forgeAction("acknowledge"); await h.forgeForm("student");
  h.state.loggingOut = false; h.hostRender();
  assert.equal(h.management.leave(), false); assert.equal(h.input("student", "username").value, "uncertain.keep");
  assert.equal(h.input("student", "display_name").value, "保留草稿"); assert.equal(h.writes().length, 1);
  await h.click("acknowledge"); assert.equal(h.management.leave(), true);
});

test("known failure during logout releases only the pending lock and preserves input for a failed logout", async () => {
  const gate = deferred();
  const h = fixture({ call: (method, url, body, fallback) => url === "/auth/students" ? gate.promise : fallback(method, url, body) });
  await h.management.load(); await studentDraft(h, "existing.name", "保留姓名"); const pending = h.submit("student");
  h.state.loggingOut = true; h.hostRender(); gate.reject(error(409, "登录名冲突")); await pending;
  h.state.loggingOut = false; h.hostRender();
  assert.equal(h.input("student", "username").value, "existing.name"); assert.equal(h.input("student", "display_name").value, "保留姓名");
  assert.match(h.html(), /登录名冲突/); assert.doesNotMatch(h.html(), /先核对上一次操作/); assert.equal(h.management.leave(), true);
});

test("logout completion resets identity and rejects late success or failure receipts", async () => {
  for (const succeeds of [true, false]) {
    const gate = deferred();
    const h = fixture({ call: (method, url, body, fallback) => url === "/auth/activation-codes" ? gate.promise : fallback(method, url, body) });
    await h.management.load(); await h.click("tab", "members"); await h.click("activation", "s-pending"); const pending = h.click("confirm");
    h.state.loggingOut = true; h.hostRender(); h.management.reset();
    h.state.transitionId += 1; h.api.state.user = null; h.state.loggingOut = false; h.hostRender();
    if (succeeds) gate.resolve({ user_id: "s-pending", activation_code: "ENDED-SESSION-SECRET", expires_in_seconds: 86400 });
    else gate.reject(error(500, "退出旧错误"));
    await pending;
    assert.doesNotMatch(h.dom.root.innerHTML, /ENDED-SESSION-SECRET|退出旧错误|先核对上一次操作/);
    assert.equal(h.changes.length, 0); assert.equal(h.toasts.length, 0); assert.equal(h.writes().length, 1);
  }
});
