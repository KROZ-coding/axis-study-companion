"use strict";

// Execute the production module and its rendered event handlers. Only the HTTP
// boundary and DOM are substituted; all records are temporary in-memory data.
// Run: node --test server/tests/teacher-review-regression.cjs
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-review.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const fault = (status, message = "测试请求失败") => Object.assign(new Error(message), { status });
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const decode = (text) => String(text).replace(/&(amp|lt|gt|quot|#39);/g, (_, entity) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[entity]);

// Parse production HTML, including ancestry, detached nodes and disabled
// fieldsets, so the actual bind() containment/permission guards are exercised.
class Element {
  constructor(tag, document, attrs = {}) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = document; this.attrs = attrs;
    this.children = []; this.parts = []; this.parentElement = null; this.listeners = new Map();
    this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value]));
    this.value = attrs.value ?? ""; this.checked = "checked" in attrs; this.scrollTop = 0;
  }
  get type() { return this.attrs.type || (this.tagName === "TEXTAREA" ? "textarea" : "text"); }
  get disabled() { return "disabled" in this.attrs; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  get textContent() { return this.parts.map((part) => typeof part === "string" ? part : part.textContent).join(""); }
  getAttribute(key) { return this.attrs[key] ?? null; }
  append(child) { child.parentElement = this; this.children.push(child); this.parts.push(child); return child; }
  contains(node) { for (let current = node; current; current = current.parentElement) if (current === this) return true; return false; }
  matches(selector) {
    return selector.split(",").some((part) => {
      if (part.trim() === ":disabled") return this.disabled || Boolean(this.closest("fieldset[disabled]"));
      const match = part.trim().match(/^(\w+)?(?:\[([^=\]]+)(?:=["']?([^"'\]]*)["']?)?\])?$/);
      return Boolean(match && (!match[1] || this.tagName === match[1].toUpperCase()) && (!match[2] || (match[2] in this.attrs && (match[3] == null || this.attrs[match[2]] === match[3]))));
    });
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(name, listener) { const handlers = this.listeners.get(name) || []; handlers.push(listener); this.listeners.set(name, handlers); }
  focus() { if (this.isConnected && !this.matches(":disabled")) this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
  get innerHTML() { return this.markup || ""; }
  set innerHTML(html) {
    this.markup = html;
    if (this.contains(this.ownerDocument.activeElement)) this.ownerDocument.activeElement = this.ownerDocument.body;
    this.children.forEach((child) => { child.parentElement = null; }); this.children = []; this.parts = [];
    const stack = [this], voids = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
    for (const token of String(html).match(/<!--[^]*?-->|<\/?[a-z][^>]*>|[^<]+/gi) || []) {
      if (token.startsWith("<!--")) continue;
      if (token.startsWith("</")) {
        const tag = token.match(/^<\/([^\s>]+)/)[1].toUpperCase();
        for (let i = stack.length - 1; i > 0; i -= 1) if (stack[i].tagName === tag) { stack.length = i; break; }
      } else if (token.startsWith("<")) {
        const [, tag, raw] = token.match(/^<([^\s/>]+)([^]*)>$/), attrs = {};
        for (const [, name, quoted, single, bare] of raw.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[name] = decode(quoted ?? single ?? bare ?? "");
        const child = stack.at(-1).append(new Element(tag, this.ownerDocument, attrs)); if (!voids.has(child.tagName)) stack.push(child);
      } else stack.at(-1).parts.push(decode(token));
    }
    this.querySelectorAll("textarea").forEach((node) => { node.value = node.textContent; });
    this.querySelectorAll("select").forEach((node) => { node.value = (node.children.find((child) => "selected" in child.attrs) || node.children[0])?.value || ""; });
  }
}

function records() {
  const scope = { course_id: "course-1", class_id: "class-1" };
  const model = { classroom: { id: "class-1", ...scope, can_manage: true }, goals: [
    { id: "goal-current", ...scope, title: "当前目标", status: "active", version: 3 },
    { id: "goal-history", ...scope, title: "历史目标", status: "closed", version: 7 },
  ], tasks: [], reviews: {}, histories: {}, threads: [], messages: {}, appeals: [], effects: [] };
  function addTask(id, goalId, studentId, attempts) {
    const task = { id, ...scope, goal_id: goalId, student_id: studentId, student_name: studentId, version: 4, round_no: 1,
      status: attempts.at(-1).status === "graded" ? "results_ready" : "awaiting_manual", submission_closed: false,
      deadline_at: Date.now() / 1000 + 3600, first_attempt_id: attempts[0]?.id, attempts: plain(attempts) };
    model.tasks.push(task);
    for (const attempt of attempts) {
      const graded = attempt.status === "graded";
      model.reviews[attempt.id] = { ...scope, goal_id: goalId, task_id: id, student_id: studentId, task_status: task.status,
        task_version: task.version, can_grade: true, results_released: graded, submission_closed: false,
        attempt: { attempt_no: 1, kind: "first", submitted_at: 1700000000, max_score: 5, ...attempt, ...(graded ? { total_score: 4 } : {}) }, items: [
          { question_id: "choice", question_version_id: "choice:1", status: "graded", score_units: 200, score: 2, answer: 1,
            question: { id: "choice", version: 1, type: "single_choice", stem: "冻结选择题", options: ["选项甲", "选项乙"], answer: 1, points: 2, points_units: 200, rubric: [] },
            original_grading: { score_units: 200, why: "原始客观评分" }, result: { score_units: 200, why: "客观题依据" }, can_manual_grade: false, can_correct: graded },
          { question_id: "text", question_version_id: "text:2", status: graded ? "graded" : "manual_pending", score_units: graded ? 200 : null, score: graded ? 2 : null,
            answer: "学生作答 <script>不可执行</script>", question: { id: "text", version: 2, type: "text", stem: `冻结论述题 ${attempt.id}`, answer: "冻结参考答案", points: 3, points_units: 300, rubric: [{ point: "说明过程", score_units: 300 }] },
            original_grading: graded ? { score_units: 200, why: "原始论述评分" } : {}, result: graded ? { score_units: 200, why: "论述评分依据" } : {}, can_manual_grade: !graded, can_correct: graded },
        ] };
      model.histories[attempt.id] = [];
    }
    return task;
  }
  addTask("task-current", "goal-current", "student-1", [{ id: "attempt-current", revision: 1, status: "review_required" }]);
  addTask("task-peer", "goal-current", "student-2", [{ id: "attempt-peer", revision: 2, status: "graded" }]);
  addTask("task-history", "goal-history", "student-1", [
    { id: "attempt-history", revision: 2, status: "graded", attempt_no: 1, kind: "first" },
    { id: "attempt-retry", revision: 4, status: "graded", attempt_no: 2, kind: "retry" },
  ]);
  model.appeals.push({ id: "appeal-current", task_id: "task-current", attempt_id: "attempt-current", student_id: "student-1", question_id: "text", question_version_id: "text:2",
    grading_revision: 1, current_revision: 1, reason: "请核对论证过程", status: "awaiting_teacher", can_resolve: true, result: {} });
  return model;
}

function fixture(options = {}) {
  const model = records(); options.prepare?.(model); model.messagePageFailure = options.messagePageFailure || null;
  const state = { live: true, role: "teacher", screen: "app", teacherView: "review", courseId: "course-1", classId: "class-1", transitionId: 1,
    loggingOut: false, teacherCourses: [{ id: "course-1", classes: [{ id: "class-1", can_manage: true }] }], ...options.state };
  const calls = [], changes = [], selections = [], reports = [], toasts = [], unexpected = [], ledger = new Map();
  let serial = 0, renders = 0, instance;
  const document = {}; document.body = new Element("body", document); document.activeElement = document.body;
  const host = document.body.append(new Element("main", document));
  const keys = (body, expected) => { assert.deepEqual(Object.keys(body).sort(), expected.sort()); assert.match(body.request_id, /^[A-Za-z0-9_.:-]{1,100}$/); };
  const appealView = (appeal) => ({ ...appeal, current_revision: model.reviews[appeal.attempt_id].attempt.revision,
    results_released: model.reviews[appeal.attempt_id].results_released, can_resolve: model.classroom.can_manage && appeal.status === "awaiting_teacher" });
  // The real detail route delegates to list_discussion_messages() with its
  // default limit=500; callers must use the existing messages endpoint for more.
  const threadView = (thread) => ({ ...thread, can_reply: thread.can_reply ?? model.classroom.can_manage, messages: (model.messages[thread.id] || []).slice(0, 500) });
  async function fallback(method, url, body) {
    const [pathname, query = ""] = url.split("?"), args = new URLSearchParams(query);
    const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent), [kind, id, action] = parts;
    if (method === "GET") {
      assert.equal(body, undefined);
      if (kind === "classes" && id === model.classroom.id) return plain(model.classroom);
      if (pathname === "/companion/discussion-presets") return [{ key: "check_work", text: "请核对推导过程。" }, { key: "response_failed", text: "不应作为提示语" }];
      if ((kind === "goals" || kind === "tasks") && !id) {
        assert.equal(args.get("class_id"), state.classId); assert.equal(args.get("limit"), "100");
        const list = kind === "goals" ? model.goals : model.tasks.filter((row) => row.goal_id === args.get("goal_id"));
        const offset = Number(args.get("offset")); return { items: plain(list.slice(offset, offset + 100)) };
      }
      if (kind === "tasks" && action === "appeals") return { items: model.appeals.filter((row) => row.task_id === id).map((row) => plain(appealView(row))) };
      if (kind === "tasks" && parts.length === 2) { const task = model.tasks.find((row) => row.id === id); if (!task) throw fault(404); return plain(task); }
      if (kind === "attempts" && action === "teacher-review") { if (!model.reviews[id]) throw fault(404); return plain(model.reviews[id]); }
      if (kind === "attempts" && action === "corrections") return { items: plain(model.histories[id] || []) };
      if (kind === "appeals" && parts.length === 2) { const appeal = model.appeals.find((row) => row.id === id); if (!appeal) throw fault(404); return plain(appealView(appeal)); }
      if (pathname === "/companion/discussion-threads") {
        assert.equal(args.get("class_id"), state.classId); const offset = Number(args.get("offset"));
        return plain(model.threads.filter((row) => row.task_id === args.get("task_id")).slice(offset, offset + 100));
      }
      if (kind === "companion" && id === "discussion-threads" && parts.length === 3) {
        const thread = model.threads.find((row) => row.id === action); if (!thread) throw fault(404); return plain(threadView(thread));
      }
      if (kind === "companion" && id === "discussion-threads" && parts.length === 4 && parts[3] === "messages") {
        const thread = model.threads.find((row) => row.id === action); if (!thread) throw fault(404);
        const limit = Number(args.get("limit") || 500), offset = Number(args.get("offset") || 0);
        assert.ok(Number.isInteger(limit) && limit > 0 && limit <= 500); assert.ok(Number.isInteger(offset) && offset >= 0);
        if (model.messagePageFailure) throw fault(model.messagePageFailure);
        return plain((model.messages[thread.id] || []).slice(offset, offset + limit));
      }
    } else if (method === "POST") {
      assert.equal(model.classroom.can_manage, true, "read-only classes must never receive writes");
      const key = `${pathname}:${body.request_id}`, previous = ledger.get(key);
      if (previous) { assert.deepEqual(plain(body), previous.body, "replays must use the identical body"); return plain(previous.result); }
      let result;
      if (kind === "attempts" && action === "items") {
        keys(body, ["request_id", "expected_revision", "score_units", "reason"]);
        const review = model.reviews[id], item = review.items.find((row) => row.question_id === parts[3]);
        if (body.expected_revision !== review.attempt.revision) throw fault(409, "评分版本已经变化");
        assert.ok(item); assert.ok(Number.isSafeInteger(body.score_units) && body.score_units >= 0 && body.score_units <= item.question.points_units);
        assert.ok(body.reason.trim());
        if (parts[4] === "manual-grade") { assert.equal(review.attempt.status, "review_required"); assert.equal(item.status, "manual_pending"); }
        else { assert.equal(parts[4], "corrections"); assert.equal(review.attempt.status, "graded"); }
        const prior = item.score_units, revision = ++review.attempt.revision;
        if (parts[4] === "corrections") model.histories[id].push({ id: `correction-${++serial}`, attempt_id: id, question_id: item.question_id, prior_score_units: prior,
          corrected_score_units: body.score_units, grading_revision: revision, reason: body.reason, created_at: 1700000100 });
        Object.assign(item, { status: "graded", score_units: body.score_units, score: body.score_units / 100, result: { score_units: body.score_units, source: "teacher", why: body.reason }, can_manual_grade: false });
        if (review.items.every((row) => row.status === "graded")) {
          review.attempt.status = "graded"; review.attempt.total_score = review.items.reduce((sum, row) => sum + row.score_units, 0) / 100;
          review.results_released = true; review.items.forEach((row) => { row.can_correct = true; });
        }
        const task = model.tasks.find((row) => row.id === review.task_id); Object.assign(task.attempts.find((row) => row.id === id), plain(review.attempt));
        // Exact public receipt from LearningLifecycle._review_score, via v1_learning.py.
        result = { attempt_id: id, question_id: item.question_id, grading_revision: revision, score_units: body.score_units,
          score: body.score_units / 100, total_score: review.items.reduce((sum, row) => sum + (row.score_units || 0), 0) / 100, prior_score: prior == null ? null : prior / 100 };
      } else if (kind === "tasks" && ["deadline-extensions", "close"].includes(action)) {
        keys(body, ["request_id", "expected_version", "reason", ...(action === "deadline-extensions" ? ["new_deadline"] : [])]);
        const task = model.tasks.find((row) => row.id === id);
        if (task.version !== body.expected_version || task.submission_closed) throw fault(409);
        if (action === "close" && ["grading", "submitted"].includes(task.status)) throw fault(409);
        if (action === "deadline-extensions") { assert.ok(body.new_deadline > task.deadline_at); task.deadline_at = body.new_deadline; }
        else Object.assign(task, { submission_closed: true, submission_closed_at: 1700000200 });
        task.version += 1; result = plain(task);
      } else if (kind === "goals" && action === "plan") {
        keys(body, ["request_id", "expected_version", "action", "reason"]); const goal = model.goals.find((row) => row.id === id);
        if (goal.version !== body.expected_version) throw fault(409);
        assert.equal(body.action, "close"); goal.status = "closed"; goal.version += 1;
        result = { goal_id: id, version: goal.version, action: "close", reason: body.reason, future_round_count: null, new_end_at: null };
      } else if (kind === "appeals" && action === "resolve") {
        keys(body, ["request_id", "expected_revision", "reason"]); const appeal = model.appeals.find((row) => row.id === id);
        if (body.expected_revision !== model.reviews[appeal.attempt_id].attempt.revision || appeal.status !== "awaiting_teacher") throw fault(409);
        appeal.status = "resolved"; appeal.result = { resolution: { grading_revision: body.expected_revision, reason: body.reason } }; result = appealView(appeal);
      } else if (pathname === "/companion/discussion-threads") {
        keys(body, ["request_id", "task_id", "attempt_id", "question_version_id", "grading_revision", "title"]);
        const review = model.reviews[body.attempt_id]; assert.equal(body.task_id, review.task_id);
        assert.ok(review.items.some((row) => row.question_version_id === body.question_version_id)); assert.ok(body.grading_revision <= review.attempt.revision);
        let thread = model.threads.find((row) => ["task_id", "attempt_id", "question_version_id", "grading_revision"].every((field) => row[field] === body[field]));
        if (!thread) { thread = { ...body, id: `thread-${++serial}`, class_id: review.class_id, course_id: review.course_id, student_id: review.student_id }; model.threads.push(thread); }
        result = thread;
      } else if (kind === "companion" && id === "discussion-threads" && parts[3] === "messages") {
        keys(body, ["request_id", "text", ...(body.preset_key ? ["preset_key"] : [])]); const thread = model.threads.find((row) => row.id === action);
        assert.ok(thread); assert.notEqual(thread.can_reply, false); assert.ok(body.preset_key || body.text.trim());
        result = { id: `message-${++serial}`, thread_id: action, author_role: "teacher", text: body.preset_key ? "请核对推导过程。" : body.text,
          delivery: body.preset_key || model.reviews[thread.attempt_id].results_released ? "delivered" : "deferred", created_at: 1700000300 };
        (model.messages[action] ||= []).push(plain(result));
      }
      if (result) { model.effects.push({ url, body: plain(body) }); ledger.set(key, { body: plain(body), result: plain(result) }); return plain(result); }
    }
    unexpected.push(`${method} ${url}`); throw new Error(`Unexpected fakeAPI request ${method} ${url}`);
  }
  const api = { state: { user: { id: "teacher-1", role: "teacher", status: "active", ...options.user } }, call: (method, url, body, requestOptions) => {
    assert.ok(url.startsWith("/") && !url.startsWith("//") && !url.startsWith("/api/"), "only relative v1 API calls are permitted");
    calls.push({ method, url, body: plain(body), options: requestOptions });
    return options.call ? options.call(method, url, body, fallback, model, requestOptions) : fallback(method, url, body);
  } };
  const blocked = () => { throw new Error("Network, storage and real models are forbidden in this suite"); };
  const context = vm.createContext({ AbortController, crypto: { randomUUID: () => `fake-${++serial}` },
    fetch: blocked, WebSocket: blocked, XMLHttpRequest: blocked, localStorage: new Proxy({}, { get: blocked }), sessionStorage: new Proxy({}, { get: blocked }),
    console: { log: blocked, warn: blocked, error: blocked } });
  vm.runInContext(source.replace(/^export function createTeacherReview/m, "function createTeacherReview") + "\nglobalThis.makeReview = createTeacherReview;", context, { filename: sourcePath });
  instance = context.makeReview({ api, state, render: () => { renders += 1; mount(); }, toast: (text) => toasts.push(text),
    onSelectionChange: (selection) => { selections.push(plain(selection)); return options.onSelectionChange?.(selection); },
    onChanged: async (selection) => { changes.push(plain(selection)); return options.onChanged?.(selection); }, onReports: (selection) => reports.push(plain(selection)) });
  function mount() { host.innerHTML = instance.render(); instance.bind(host); }
  const root = () => host.querySelector("[data-teacher-review]");
  const find = (attribute, value, id) => root().querySelectorAll(`[${attribute}]`).find((node) => node.getAttribute(attribute) === value && (id == null || node.dataset.reviewId === id));
  async function dispatch(target, type, bound = root()) {
    assert.ok(target, `production ${type} target exists`);
    const event = { type, target, preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; } };
    await Promise.all((bound.listeners.get(type) || []).map((listener) => listener(event))); return event;
  }
  function input(scope, name, id) { return root().querySelectorAll("[data-review-field]").find((node) => node.dataset.reviewScope === scope && node.dataset.reviewField === name && (id == null || node.dataset.reviewId === id)); }
  mount();
  return { review: instance, model, state, api, calls, changes, selections, reports, toasts, unexpected, document, host, root, mount, input, dispatch,
    renders: () => renders, text: () => root().textContent, html: () => root().innerHTML, writes: () => calls.filter((call) => call.method !== "GET"),
    form: (kind, id) => find("data-review-form", kind, id), action: (kind, id) => find("data-review-action", kind, id),
    click: (kind, id) => dispatch(find("data-review-action", kind, id), "click"), submit: (kind, id) => dispatch(find("data-review-form", kind, id), "submit"),
    field: (scope, name, value, id) => { const node = input(scope, name, id); assert.ok(node, `production field ${scope}/${name}/${id || ""} exists`);
      if (node.type === "checkbox") node.checked = Boolean(value); else node.value = value; node.focus();
      return dispatch(node, node.type === "checkbox" || node.tagName === "SELECT" ? "change" : "input"); },
    select: (name, value, id) => { const node = find("data-review-field", name, id); assert.ok(node); node.value = value; return dispatch(node, "change"); },
    forge: (kind, id) => dispatch(root().append(new Element("form", document, { "data-review-form": kind, "data-review-id": id || "" })), "submit"),
  };
}
async function grade(h, score = "2.25", reason = "按冻结量表复核", id = "text") {
  await h.field("grade", "score", score, id); await h.field("grade", "reason", reason, id); await h.field("grade", "confirmed", true, id);
}
async function comment(h, text = "教师保留的短评", id = "text") {
  await h.field("discussion", "text", text, id); await h.field("discussion", "confirmed", true, id);
}
async function management(h, kind) { await h.field("manage", "reason", "核对后调整后续安排", kind); await h.field("manage", "confirmed", true, kind); }
function thread(model, id = "thread-history", attemptId = "attempt-retry", revision = 3) {
  const review = model.reviews[attemptId], row = { id, task_id: review.task_id, attempt_id: attemptId, question_version_id: "text:2", grading_revision: revision,
    class_id: review.class_id, course_id: review.course_id, student_id: review.student_id, title: "教师历史短评" };
  model.threads.push(row); model.messages[id] = [{ id: `${id}-message`, thread_id: id, text: "旧修订消息", author_role: "teacher", created_at: 1700000000 }]; return row;
}

function discussionHistory(model, { count = 501, id = "thread-many", attemptId = "attempt-retry", revision = 3 } = {}) {
  const row = thread(model, id, attemptId, revision);
  model.messages[id] = Array.from({ length: count }, (_, index) => ({ id: `${id}-message-${index + 1}`, thread_id: id,
    author_role: "student", text: `第${index + 1}条学生回复`, redacted: false, created_at: 1700000000 + index }));
  return row;
}

// The factory is never reimplemented or replaced in these tests.
test("a notification for message 501 is successful only after that reply is rendered from message pagination", async () => {
  const h = fixture({ prepare: (model) => discussionHistory(model) });
  const opened = await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" });
  assert.equal(opened, true); assert.match(h.text(), /第501条学生回复/);
  assert.equal(h.root().querySelectorAll('[data-review-message="thread-many-message-501"]').length, 1);
  assert.ok(h.calls.some((call) => call.method === "GET" && call.url.includes("/discussion-threads/thread-many/messages?") && call.url.includes("offset=500")));
  assert.deepEqual(plain(h.state.reviewSelection), { goalId: "goal-history", taskId: "task-history", attemptId: "attempt-retry", studentId: "student-1" });
  assert.equal(h.writes().length, 0); assert.equal(h.reports.length, 0);
});

test("thread-only openings and ordinary thread selection cover replies beyond the detail's first 500", async () => {
  const h = fixture({ prepare: (model) => discussionHistory(model, { count: 1001 }) });
  assert.equal(await h.review.openSelection({ threadId: "thread-many" }), true); assert.match(h.text(), /第1001条学生回复/);
  await h.review.openSelection({ attemptId: "attempt-history" }); await h.review.openSelection({ attemptId: "attempt-retry" });
  await h.select("thread", "thread-many", "text"); assert.match(h.text(), /第1001条学生回复/);
  assert.equal(h.writes().length, 0);
});

test("message page failures and lost access cannot report notification success or change the original selection", async () => {
  for (const status of [503, 401, 403, 404]) {
    const h = fixture({ prepare: (model) => discussionHistory(model) });
    await h.review.load(); const prior = plain(h.state.reviewSelection), selectionCount = h.selections.length;
    h.model.messagePageFailure = status;
    assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), false);
    assert.deepEqual(plain(h.state.reviewSelection), prior); assert.equal(h.selections.length, selectionCount);
    assert.equal(h.writes().length, 0); assert.doesNotMatch(h.text(), /第501条学生回复/);
    if (status === 503) {
      h.model.messagePageFailure = null;
      assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), true);
      assert.match(h.text(), /第501条学生回复/);
    } else assert.doesNotMatch(h.text(), /冻结参考答案|第1条学生回复/);
  }
});

test("a missing, redacted or foreign-thread target message cannot acknowledge a notification", async () => {
  for (const corruption of ["missing", "redacted", "foreign"]) {
    const h = fixture({ prepare: (model) => {
      discussionHistory(model); const target = model.messages["thread-many"][500];
      if (corruption === "missing") model.messages["thread-many"].pop();
      if (corruption === "redacted") { target.redacted = true; target.notice = "不可显示的目标消息"; }
      if (corruption === "foreign") target.thread_id = "another-thread";
    } });
    await h.review.load(); const prior = plain(h.state.reviewSelection);
    assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), false);
    assert.deepEqual(plain(h.state.reviewSelection), prior); assert.equal(h.writes().length, 0);
    assert.doesNotMatch(h.text(), /第501条学生回复|不可显示的目标消息/);
  }
});

test("a failed notification page preserves the original thread, selection and unsent draft binding", async () => {
  const h = fixture({ prepare: (model) => { thread(model, "thread-original", "attempt-current", 1); discussionHistory(model); } });
  await h.review.openSelection({ threadId: "thread-original" }); await comment(h, "原线程未发送内容");
  const prior = plain(h.state.reviewSelection); h.model.messagePageFailure = 503;
  assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), false);
  assert.deepEqual(plain(h.state.reviewSelection), prior);
  assert.equal(h.input("discussion", "text", "text")?.value, "原线程未发送内容");
  const selected = h.root().querySelectorAll('[data-review-field="thread"]').find((node) => node.dataset.reviewId === "text");
  assert.equal(selected?.value, "thread-original"); assert.equal(h.writes().length, 0);
  h.model.messagePageFailure = null; await h.review.load({ force: true });
  await h.field("discussion", "confirmed", true, "text"); await h.submit("discussion", "text");
  assert.equal(h.writes().length, 1); assert.equal(h.writes()[0].url, "/companion/discussion-threads/thread-original/messages");
  assert.equal(h.writes()[0].body.text, "原线程未发送内容");
});

test("late message pages cannot acknowledge or repaint after logout, reset, leave or a newer selection", async () => {
  for (const interrupt of ["logout", "reset", "leave", "selection"]) {
    for (const failure of [false, true]) {
      const gate = deferred(); let waiting = false;
      const h = fixture({ prepare: (model) => discussionHistory(model, { count: 1001 }), call: async (method, url, body, next) => {
        const result = await next(method, url, body);
        if (url.includes("/thread-many/messages?")) { waiting = true; await gate.promise; if (failure) throw fault(404); }
        return result;
      } });
      await h.review.load(); const pending = h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-1001" });
      await settle(); assert.equal(waiting, true);
      if (interrupt === "logout") { h.state.loggingOut = true; h.mount(); }
      if (interrupt === "reset") { h.review.reset(); h.mount(); await h.review.openSelection({ attemptId: "attempt-peer" }); }
      if (interrupt === "leave") { assert.equal(h.review.leave(), true); }
      if (interrupt === "selection") await h.review.openSelection({ attemptId: "attempt-peer" });
      const before = { text: h.text(), selection: plain(h.state.reviewSelection), calls: h.calls.length, changes: h.changes.length, toasts: h.toasts.length };
      gate.resolve(); assert.equal(await pending, false);
      assert.equal(h.text(), before.text); assert.deepEqual(plain(h.state.reviewSelection), before.selection);
      assert.equal(h.calls.length, before.calls); assert.equal(h.changes.length, before.changes); assert.equal(h.toasts.length, before.toasts);
      assert.equal(h.writes().length, 0); assert.ok(h.calls.some((call) => call.url.includes("/thread-many/messages?") && call.options?.signal?.aborted));
    }
  }
});

test("duplicate message pages fail closed and mismatched details cannot supply notification content", async () => {
  for (const corruption of ["repeated", "id", "question", "revision"]) {
    const h = fixture({ prepare: (model) => discussionHistory(model), call: async (method, url, body, next, model) => {
      const result = await next(method, url, body);
      if (corruption === "repeated" && url.includes("/thread-many/messages?")) return plain(model.messages["thread-many"].slice(0, 500));
      if (url === "/companion/discussion-threads/thread-many") {
        if (corruption === "id") return { ...result, id: "another-thread" };
        if (corruption === "question") return { ...result, question_version_id: "choice:1" };
        if (corruption === "revision") return { ...result, grading_revision: 99 };
      }
      return result;
    } });
    assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), false);
    assert.equal(h.writes().length, 0); assert.equal(h.selections.length, 0);
    if (corruption === "repeated") assert.equal(h.calls.filter((call) => call.url.includes("/thread-many/messages?")).length, 1);
  }
});

test("directly authorized notification threads remain visible when a list snapshot omits them", async () => {
  const h = fixture({ prepare: (model) => discussionHistory(model), call: (method, url, body, next) => {
    return url.startsWith("/companion/discussion-threads?") ? [] : next(method, url, body);
  } });
  assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), true);
  assert.equal(h.root().querySelectorAll('[data-review-message="thread-many-message-501"]').length, 1);
  await comment(h); await h.submit("discussion", "text");
  assert.equal(h.writes().length, 1); assert.equal(h.writes()[0].url, "/companion/discussion-threads/thread-many/messages");
});

test("message notification success requires a connected rendered target and a valid thread locator", async () => {
  const h = fixture({ prepare: (model) => discussionHistory(model) });
  assert.equal(await h.review.openSelection({ messageId: "thread-many-message-501" }), false); assert.equal(h.calls.length, 0);
  h.host.parentElement = null;
  assert.equal(await h.review.openSelection({ threadId: "thread-many", messageId: "thread-many-message-501" }), false);
  assert.equal(h.writes().length, 0);
});

test("inactive, offline, logged-out, student and incomplete contexts cannot read or write", async () => {
  for (const options of [{ state: { live: false } }, { state: { role: "student" } }, { state: { screen: "login" } }, { state: { loggingOut: true } },
    { state: { classId: "" } }, { user: { id: "" } }, { user: { status: "disabled" } }, { user: { role: "student" } }]) {
    const h = fixture(options); assert.equal(await h.review.load(), false); await h.forge("grade", "text"); assert.equal(h.calls.length, 0);
  }
});

test("real markup binding renders frozen evidence and accepts the actual grading_revision receipt once", async () => {
  const h = fixture(); assert.equal(await h.review.load(), true);
  assert.match(h.text(), /冻结参考答案/); assert.match(h.text(), /B\. 选项乙/); assert.match(h.text(), /待评分/);
  assert.doesNotMatch(h.html(), /<script>/); assert.equal(h.form("grade", "choice"), undefined); assert.ok(h.form("grade", "text"));
  h.review.bind(h.host); h.review.bind(h.root()); await grade(h); await h.submit("grade", "text");
  assert.equal(h.writes().length, 1); assert.equal(h.writes()[0].url, "/attempts/attempt-current/items/text/manual-grade");
  assert.deepEqual(Object.assign({}, h.writes()[0].body, { request_id: "id" }), { request_id: "id", expected_revision: 1, score_units: 225, reason: "按冻结量表复核" });
  assert.equal(h.changes.length, 1); assert.match(h.text(), /人工评分已保存/); assert.equal(Boolean(h.action("retry")), false);
  assert.equal(h.model.reviews["attempt-current"].attempt.total_score, 4.25); assert.equal(h.reports.length, 0); assert.equal(h.unexpected.length, 0);
});

test("historical retry correction preserves selection, original evidence and correction history without opening reports", async () => {
  const h = fixture(); assert.equal(await h.review.openSelection({ attemptId: "attempt-retry" }), true);
  assert.deepEqual(plain(h.state.reviewSelection), { goalId: "goal-history", taskId: "task-history", attemptId: "attempt-retry", studentId: "student-1" });
  await grade(h, "0", "确认为零分"); await h.submit("grade", "text");
  assert.equal(h.writes()[0].url, "/attempts/attempt-retry/items/text/corrections"); assert.equal(h.writes()[0].body.expected_revision, 4);
  assert.match(h.text(), /原始论述评分/); assert.match(h.text(), /2 → 0 分/); assert.equal(h.state.reviewSelection.attemptId, "attempt-retry");
  assert.equal(h.reports.length, 0); assert.equal(h.selections.length, 1);
  await h.click("reports"); assert.deepEqual(h.reports, [plain(h.state.reviewSelection)]);
});

test("goal, task, student, attempt and notification partial selections resolve real server bindings", async () => {
  for (const [selection, expected] of [[{ goalId: "goal-history" }, "attempt-history"], [{ taskId: "task-history" }, "attempt-history"],
    [{ studentId: "student-2" }, "attempt-peer"], [{ attemptId: "attempt-retry" }, "attempt-retry"], [{ threadId: "thread-history" }, "attempt-retry"]]) {
    const h = fixture({ prepare: (model) => thread(model) }); assert.equal(await h.review.openSelection(selection), true);
    assert.equal(h.state.reviewSelection.attemptId, expected); assert.equal(h.reports.length, 0);
    if (selection.threadId) assert.match(h.text(), /旧修订消息/);
  }
  const h = fixture(); await h.review.openSelection({ attemptId: "attempt-retry" });
  await h.review.openSelection({ goalId: "goal-history", studentId: "student-1" }); assert.equal(h.state.reviewSelection.attemptId, "attempt-retry");
  await h.review.openSelection({ goalId: "goal-current" }); assert.equal(h.state.reviewSelection.taskId, "task-current");
  await h.select("student", "student-2"); assert.equal(h.state.reviewSelection.taskId, "task-peer");
});

test("explicit mismatched IDs and foreign teacher-review projections never become a writable selection", async () => {
  for (const selection of [{ goalId: "goal-current", attemptId: "attempt-retry" }, { taskId: "task-current", studentId: "student-2" },
    { taskId: "task-current", attemptId: "missing" }, { goalId: "missing" }, { threadId: "thread-history", attemptId: "attempt-current" }]) {
    const h = fixture({ prepare: (model) => thread(model) }); assert.equal(await h.review.openSelection(selection), false);
    assert.equal(h.form("grade", "text"), undefined); assert.equal(h.selections.length, 0); assert.equal(h.writes().length, 0);
  }
  const h = fixture({ prepare: (model) => { model.reviews["attempt-current"].class_id = "foreign-class"; } });
  assert.equal(await h.review.load(), false); assert.doesNotMatch(h.text(), /冻结参考答案/);
});

test("partial selections traverse history pages beyond 100 goals, tasks and discussion threads", async () => {
  const h = fixture({ prepare: (model) => {
    const old = model.goals.find((row) => row.id === "goal-history"), task = model.tasks.find((row) => row.id === "task-history");
    model.goals = [...Array.from({ length: 100 }, (_, i) => ({ ...model.goals[0], id: `goal-${i}` })), old];
    model.tasks = [...Array.from({ length: 100 }, (_, i) => ({ ...task, id: `task-${i}`, student_id: `peer-${i}` })), { ...task, student_id: "student-1" }];
    for (let i = 0; i < 101; i += 1) thread(model, `thread-${i}`, "attempt-history", 1);
  } });
  assert.equal(await h.review.openSelection({ goalId: "goal-history", studentId: "student-1" }), true);
  assert.equal(h.state.reviewSelection.taskId, "task-history");
  for (const prefix of ["/goals?", "/tasks?", "/companion/discussion-threads?"]) {
    assert.ok(h.calls.some((call) => call.url.startsWith(prefix) && call.url.endsWith("offset=100")), `${prefix} reaches the next page`);
  }
  await h.select("thread", "thread-100", "text"); assert.match(h.text(), /旧修订消息/);
});

test("load-more controls advance list pages and repeated pages stop instead of looping", async () => {
  const h = fixture({ prepare: (model) => { model.goals.push(...Array.from({ length: 99 }, (_, i) => ({ ...model.goals[0], id: `extra-${i}` }))); } });
  await h.review.load(); assert.ok(h.action("more-goals")); await h.click("more-goals"); assert.equal(h.action("more-goals"), undefined); assert.match(h.text(), /101 个目标/);
  const broken = fixture({ prepare: (model) => { model.goals = Array.from({ length: 100 }, (_, i) => ({ ...model.goals[0], id: `page-${i}` })); },
    call: (method, url, body, next, model) => url.startsWith("/goals?") ? { items: plain(model.goals) } : next(method, url, body) });
  assert.equal(await broken.review.openSelection({ goalId: "missing" }), false); assert.match(broken.text(), /分页没有前进/);
  assert.equal(broken.calls.filter((call) => call.url.startsWith("/goals?")).length, 2);
});

test("score, reason and confirmation validation prevent invalid and accidentally reconfirmed writes", async () => {
  const h = fixture(); await h.review.load();
  for (const score of ["", "-1", "3.01", "1.234", "1e2", "Infinity"]) { await grade(h, score); await h.submit("grade", "text"); }
  await grade(h, "1", " "); await h.submit("grade", "text"); await grade(h, "1", "x".repeat(2001)); await h.submit("grade", "text");
  await grade(h); await h.field("grade", "reason", "修改后的依据", "text"); assert.equal(h.input("grade", "confirmed", "text").checked, false);
  await h.submit("grade", "text"); assert.equal(h.writes().length, 0);
});

test("unknown outcomes only retry explicitly with the identical frozen body and lock navigation", async () => {
  for (const status of [undefined, 500, 408, 425]) {
    let failed = false;
    const h = fixture({ call: async (method, url, body, next) => {
      const result = await next(method, url, body);
      if (method === "POST" && !failed) { failed = true; throw fault(status); } return result;
    } });
    await h.review.load(); await grade(h); await h.submit("grade", "text"); const first = h.writes()[0];
    assert.ok(h.action("retry")); assert.equal(h.review.leave(), false); assert.equal(await h.review.openSelection({ taskId: "task-history" }), false);
    await h.review.load({ force: true }); await h.field("grade", "score", "0", "text"); await h.forge("grade", "text"); await settle();
    assert.equal(h.writes().length, 1); assert.equal(h.changes.length, 0); assert.equal(h.reports.length, 0);
    await h.click("retry"); assert.deepEqual(h.writes()[1], first); assert.equal(h.model.effects.length, 1); assert.equal(h.changes.length, 1);
    assert.equal(Boolean(h.action("retry")), false); assert.equal(h.state.reviewSelection.attemptId, "attempt-current");
  }
});

test("a mismatched grading receipt stays unknown instead of acknowledging a different question", async () => {
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); return method === "POST" ? { ...result, revision: 2, question_id: "wrong-question" } : result;
  } });
  await h.review.load(); await grade(h); await h.submit("grade", "text"); assert.ok(h.action("retry")); assert.equal(h.changes.length, 0);
});

test("revision conflicts reread evidence, retain drafts and require confirmation for a new request", async () => {
  const h = fixture(); await h.review.load(); await grade(h);
  h.model.reviews["attempt-current"].attempt.revision = 2; await h.submit("grade", "text");
  assert.equal(h.writes().length, 1); assert.equal(h.input("grade", "score", "text").value, "2.25");
  assert.equal(h.input("grade", "reason", "text").value, "按冻结量表复核"); assert.equal(h.input("grade", "confirmed", "text").checked, false);
  await h.submit("grade", "text"); assert.equal(h.writes().length, 1);
  await h.field("grade", "confirmed", true, "text"); await h.submit("grade", "text");
  assert.equal(h.writes()[1].body.expected_revision, 2); assert.notEqual(h.writes()[1].body.request_id, h.writes()[0].body.request_id);
  assert.equal(h.changes.length, 1); assert.equal(h.reports.length, 0);
});

test("a confirmed save followed by a failed refresh or host hook never becomes another POST", async () => {
  let saved = false;
  const h = fixture({ call: async (method, url, body, next) => {
    if (saved && method === "GET" && url.startsWith("/classes/")) throw fault(503);
    const result = await next(method, url, body); if (method === "POST") saved = true; return result;
  }, onChanged: () => { throw new Error("host refresh failed"); } });
  await h.review.load(); await grade(h); await h.submit("grade", "text");
  assert.equal(Boolean(h.action("retry")), false); assert.match(h.text(), /不要重复提交/); assert.equal(h.changes.length, 1);
  await h.forge("grade", "text"); assert.equal(h.writes().length, 1); assert.equal(h.reports.length, 0);
});

test("free comments and safe presets use real frozen bindings and server delivery receipts", async () => {
  for (const preset of [false, true]) {
    const h = fixture(); await h.review.load();
    if (preset) { await h.field("discussion", "preset", "check_work", "text"); await h.field("discussion", "confirmed", true, "text"); }
    else await comment(h);
    await h.submit("discussion", "text"); const [create, message] = h.writes();
    assert.deepEqual(Object.assign({}, create.body, { request_id: "id" }), { request_id: "id", task_id: "task-current", attempt_id: "attempt-current", question_version_id: "text:2", grading_revision: 1, title: "教师短评" });
    assert.equal(message.url, `/companion/discussion-threads/${h.model.threads[0].id}/messages`);
    assert.deepEqual(Object.assign({}, message.body, { request_id: "id" }), preset ? { request_id: "id", text: "", preset_key: "check_work" } : { request_id: "id", text: "教师保留的短评" });
    assert.match(h.text(), preset ? /送达/ : /自由短评将在整轮评分开放后/); assert.doesNotMatch(h.text(), /不应作为提示语/);
    assert.equal(h.changes.length, 1); assert.equal(h.reports.length, 0);
  }
});

test("lost thread-creation receipts retry the same creation then send the message once", async () => {
  let failed = false;
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body);
    if (method === "POST" && url === "/companion/discussion-threads" && !failed) { failed = true; throw fault(503); } return result;
  } });
  await h.review.load(); await comment(h); await h.submit("discussion", "text"); assert.equal(h.writes().length, 1);
  await h.click("retry"); assert.deepEqual(h.writes()[1], h.writes()[0]); assert.equal(h.model.threads.length, 1);
  assert.equal(h.writes().filter((call) => call.url.endsWith("/messages")).length, 1); assert.equal(h.changes.length, 1);
});

test("message failure after thread creation retries the original message without recreating its thread", async () => {
  let failed = false;
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); if (method === "POST" && url.endsWith("/messages") && !failed) { failed = true; throw fault(503); } return result;
  } });
  await h.review.load(); await comment(h); await h.submit("discussion", "text"); assert.equal(h.writes().length, 2);
  await h.click("retry"); assert.equal(h.writes().length, 3); assert.deepEqual(h.writes()[2], h.writes()[1]);
  assert.equal(h.model.threads.length, 1); assert.equal(h.model.messages[h.model.threads[0].id].length, 1); assert.equal(h.reports.length, 0);
});

test("definite message rejection retains text and restores its unchanged body after confirmation", async () => {
  let failed = false;
  const h = fixture({ call: (method, url, body, next) => {
    if (method === "POST" && url.endsWith("/messages") && !failed) { failed = true; throw fault(422, "暂未发送"); } return next(method, url, body);
  } });
  await h.review.load(); await comment(h); await h.submit("discussion", "text");
  assert.equal(h.input("discussion", "text", "text").value, "教师保留的短评"); assert.equal(h.input("discussion", "confirmed", "text").checked, false);
  assert.equal(Boolean(h.action("retry")), false); await h.field("discussion", "confirmed", true, "text"); await h.submit("discussion", "text");
  assert.equal(h.writes().length, 3); assert.deepEqual(h.writes()[2], h.writes()[1]); assert.equal(h.model.threads.length, 1);
});

test("thread permission reads are mandatory and a failed detail read never repeats thread creation", async () => {
  let fail = true;
  const h = fixture({ call: (method, url, body, next) => {
    if (method === "GET" && /^\/companion\/discussion-threads\//.test(url) && fail) { fail = false; throw fault(503); } return next(method, url, body);
  } });
  await h.review.load(); await comment(h); await h.submit("discussion", "text"); assert.equal(h.writes().length, 1);
  await h.click("retry"); assert.equal(h.writes().length, 2); assert.equal(h.writes()[1].url.endsWith("/messages"), true);
  const denied = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); return method === "GET" && /^\/companion\/discussion-threads\//.test(url) ? { ...result, can_reply: false } : result;
  } });
  await denied.review.load(); await comment(denied); await denied.submit("discussion", "text"); assert.equal(denied.writes().length, 1);
});

test("malformed or mismatched thread detail never grants permission to post a message", async () => {
  for (const corrupt of [(row) => ({ ...row, id: "different-thread" }), (row) => { delete row.messages; return row; }]) {
    const h = fixture({ call: async (method, url, body, next) => {
      const result = await next(method, url, body); return method === "GET" && /^\/companion\/discussion-threads\//.test(url) ? corrupt(result) : result;
    } });
    await h.review.load(); await comment(h); await h.submit("discussion", "text");
    assert.equal(h.writes().length, 1); assert.equal(h.changes.length, 0); assert.ok(h.action("retry"));
  }
});

test("refreshing a grading revision invalidates confirmation for an unsent current-revision comment", async () => {
  const h = fixture(); await h.review.load(); await comment(h); h.model.reviews["attempt-current"].attempt.revision = 2;
  await h.review.load({ force: true }); assert.equal(h.input("discussion", "text", "text").value, "教师保留的短评");
  assert.equal(h.input("discussion", "confirmed", "text").checked, false); await h.submit("discussion", "text"); assert.equal(h.writes().length, 0);
});

test("deadline extension, submission closure and goal closure use their distinct versioned endpoints", async () => {
  for (const kind of ["deadline", "close-task", "stop-goal"]) {
    const h = fixture(); await h.review.load();
    if (kind === "deadline") await h.field("manage", "deadline", "2099-01-02T10:30", kind);
    await management(h, kind); await h.submit(kind);
    const call = h.writes()[0]; assert.ok(call); assert.equal(call.body.expected_version, kind === "stop-goal" ? 3 : 4);
    assert.equal(call.url, { deadline: "/tasks/task-current/deadline-extensions", "close-task": "/tasks/task-current/close", "stop-goal": "/goals/goal-current/plan" }[kind]);
    if (kind === "deadline") assert.equal(call.body.new_deadline, new Date("2099-01-02T10:30").getTime() / 1000);
    if (kind === "stop-goal") { assert.equal(call.body.action, "close"); assert.equal(h.model.tasks[0].submission_closed, false); }
    if (kind === "close-task") { assert.equal(h.model.tasks[0].submission_closed, true); assert.equal(h.model.tasks[0].status, "awaiting_manual"); }
    assert.match(h.text(), /冻结参考答案/); assert.equal(h.changes.length, 1); assert.equal(h.reports.length, 0);
  }
});

test("closed submissions, active grading and invalid deadlines cannot be forced into management writes", async () => {
  for (const closed of [false, true]) {
    const h = fixture({ prepare: (model) => { model.tasks[0].submission_closed = closed; model.tasks[0].status = "grading"; } });
    await h.review.load(); await management(h, "close-task"); await h.submit("close-task"); assert.equal(h.writes().length, 0);
    await h.field("manage", "deadline", "2000-01-01T00:00", "deadline"); await management(h, "deadline"); await h.submit("deadline"); assert.equal(h.writes().length, 0);
  }
});

test("management conflicts preserve reasons and require reconfirmation against the new version", async () => {
  const h = fixture(); await h.review.load(); await management(h, "stop-goal"); h.model.goals[0].version += 1;
  await h.submit("stop-goal"); assert.equal(h.input("manage", "confirmed", "stop-goal").checked, false);
  assert.equal(h.input("manage", "reason", "stop-goal").value, "核对后调整后续安排");
  await h.field("manage", "confirmed", true, "stop-goal"); await h.submit("stop-goal");
  assert.equal(h.writes()[1].body.expected_version, 4); assert.notEqual(h.writes()[0].body.request_id, h.writes()[1].body.request_id);
});

test("appeal resolution uses that appeal's attempt revision, refreshes conflicts and records the resolution", async () => {
  const h = fixture({ prepare: (model) => { model.appeals.push({ ...model.appeals[0], id: "appeal-retry", task_id: "task-history", attempt_id: "attempt-retry" }); } });
  await h.review.openSelection({ attemptId: "attempt-history" }); await h.click("appeal", "appeal-retry");
  await h.field("appeal", "reason", "已核对该次重做评分", "appeal-retry"); await h.field("appeal", "confirmed", true, "appeal-retry");
  h.model.reviews["attempt-retry"].attempt.revision = 5; await h.submit("resolve", "appeal-retry");
  assert.equal(h.writes()[0].body.expected_revision, 4); assert.equal(h.input("appeal", "confirmed", "appeal-retry").checked, false);
  assert.equal(h.input("appeal", "reason", "appeal-retry").value, "已核对该次重做评分");
  await h.field("appeal", "confirmed", true, "appeal-retry"); await h.submit("resolve", "appeal-retry");
  assert.equal(h.writes()[1].url, "/appeals/appeal-retry/resolve"); assert.equal(h.writes()[1].body.expected_revision, 5);
  assert.match(h.text(), /此异议已结案/); assert.equal(h.state.reviewSelection.attemptId, "attempt-history"); assert.equal(h.reports.length, 0);
});

test("current-class read capability overrides a different managed class and all forged write controls", async () => {
  for (const serverManage of [false, true]) {
    const h = fixture({ prepare: (model) => { model.classroom.can_manage = serverManage; }, state: { teacherCourses: [{ classes: [
      { id: "other-class", can_manage: true }, { id: "class-1", can_manage: false },
    ] }] } });
    await h.review.load(); await h.click("appeal", "appeal-current"); assert.match(h.text(), /当前为只读权限/);
    assert.equal(h.form("grade", "text"), undefined); assert.equal(h.form("discussion", "text"), undefined); assert.equal(h.form("resolve", "appeal-current"), undefined);
    for (const kind of ["grade", "discussion", "deadline", "close-task", "stop-goal", "resolve"]) await h.forge(kind, kind === "resolve" ? "appeal-current" : "text");
    assert.equal(h.writes().length, 0); assert.equal(h.reports.length, 0);
  }
});

test("pending submissions cannot double-post and late successful logout receipts settle silently", async () => {
  const gate = deferred();
  const h = fixture({ call: async (method, url, body, next) => { const result = await next(method, url, body); if (method === "POST") { await gate.promise; } return result; } });
  await h.review.load(); await grade(h); const pending = h.submit("grade", "text"); await settle();
  await h.forge("grade", "text"); assert.equal(h.writes().length, 1); assert.equal(h.review.leave(), false);
  h.state.loggingOut = true; h.mount(); assert.equal(h.review.leave(), true);
  const counts = { calls: h.calls.length, toasts: h.toasts.length, renders: h.renders() }; gate.resolve(); await pending;
  assert.equal(h.calls.length, counts.calls); assert.equal(h.toasts.length, counts.toasts); assert.equal(h.renders(), counts.renders);
  assert.equal(h.changes.length, 0); assert.equal(h.reports.length, 0);
  h.state.loggingOut = false; h.mount(); await h.review.load(); assert.equal(Boolean(h.action("retry")), false); assert.equal(h.writes().length, 1);
  assert.equal(h.review.leave(), true);
});

test("unknown receipts during logout retain the original command for explicit recovery after logout failure", async () => {
  const gate = deferred(); let fail = true;
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); if (method === "POST" && fail) { fail = false; await gate.promise; throw fault(503); } return result;
  } });
  await h.review.load(); await grade(h); const pending = h.submit("grade", "text"); await settle(); h.state.loggingOut = true; h.mount();
  gate.resolve(); await pending; assert.equal(h.changes.length, 0); assert.equal(h.toasts.length, 0);
  h.state.loggingOut = false; h.mount(); assert.ok(h.action("retry")); await h.click("retry");
  assert.deepEqual(h.writes()[1], h.writes()[0]); assert.equal(h.changes.length, 1);
});

test("logout pauses a created thread before the permission read or message and resumes only explicitly", async () => {
  const gate = deferred();
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); if (method === "POST" && url === "/companion/discussion-threads") await gate.promise; return result;
  } });
  await h.review.load(); await comment(h); const pending = h.submit("discussion", "text"); await settle();
  h.state.loggingOut = true; h.mount(); const count = h.calls.length; gate.resolve(); await pending;
  assert.equal(h.calls.length, count); assert.equal(h.writes().length, 1); assert.equal(h.toasts.length, 0);
  h.state.loggingOut = false; h.mount(); assert.match(h.text(), /继续原短评请求/); await h.review.load(); assert.equal(h.writes().length, 1);
  await h.click("retry"); assert.equal(h.writes().length, 2); assert.ok(h.writes()[1].url.endsWith("/messages")); assert.equal(h.model.threads.length, 1);
});

test("reset isolates late writes and stale bound events from the new selection", async () => {
  const gate = deferred();
  const h = fixture({ call: async (method, url, body, next) => { const result = await next(method, url, body); if (method === "POST") await gate.promise; return result; } });
  await h.review.load(); await grade(h); const oldRoot = h.root(), oldForm = h.form("grade", "text");
  const pending = h.submit("grade", "text"); await settle(); h.review.reset(); h.mount(); await h.review.openSelection({ attemptId: "attempt-retry" });
  const before = { text: h.text(), selection: plain(h.state.reviewSelection), changes: h.changes.length, toasts: h.toasts.length, calls: h.calls.length };
  gate.resolve(); await pending; await h.dispatch(oldForm, "submit", oldRoot);
  assert.equal(h.text(), before.text); assert.deepEqual(plain(h.state.reviewSelection), before.selection);
  assert.equal(h.changes.length, before.changes); assert.equal(h.toasts.length, before.toasts); assert.equal(h.calls.length, before.calls); assert.equal(h.reports.length, 0);
});

test("reset after thread creation cannot continue the old message or repopulate cleared drafts", async () => {
  const gate = deferred();
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); if (method === "POST" && url === "/companion/discussion-threads") await gate.promise; return result;
  } });
  await h.review.load(); await comment(h); const pending = h.submit("discussion", "text"); await settle();
  h.review.reset(); h.mount(); gate.resolve(); await pending; assert.equal(h.writes().length, 1); assert.equal(h.changes.length, 0);
  assert.deepEqual(plain(h.state.reviewSelection), { goalId: "", taskId: "", attemptId: "", studentId: "" }); assert.doesNotMatch(h.text(), /教师保留的短评/);
});

test("a newer selection and reset ignore stale reads even when the transport ignores AbortSignal", async () => {
  const gate = deferred(); let wait = true;
  const h = fixture({ call: async (method, url, body, next) => {
    const result = await next(method, url, body); if (url === "/attempts/attempt-current/teacher-review" && wait) { wait = false; await gate.promise; } return result;
  } });
  const pending = h.review.load(); await settle(); await h.review.openSelection({ attemptId: "attempt-retry" });
  gate.resolve(); assert.equal(await pending, false); assert.equal(h.state.reviewSelection.attemptId, "attempt-retry");
  assert.match(h.text(), /冻结论述题 attempt-retry/); assert.doesNotMatch(h.text(), /冻结论述题 attempt-current/);
  assert.ok(h.calls.some((call) => call.options?.signal?.aborted));
});

test("revoked access including the learning API's masked 404 clears private review evidence", async () => {
  for (const status of [401, 403, 404]) {
    let revoke = false;
    const h = fixture({ call: (method, url, body, next) => { if (revoke && url.startsWith("/classes/")) throw fault(status); return next(method, url, body); } });
    await h.review.load(); await grade(h); revoke = true; assert.equal(await h.review.load({ force: true }), false);
    assert.doesNotMatch(h.text(), /冻结参考答案|学生作答|按冻结量表复核/); await h.forge("grade", "text"); assert.equal(h.writes().length, 0);
  }
});

test("write access failures clear private records and cannot leave a usable stale grading form", async () => {
  for (const status of [401, 403, 404]) {
    const h = fixture({ call: (method, url, body, next) => { if (method === "POST") throw fault(status); return next(method, url, body); } });
    await h.review.load(); await grade(h); await h.submit("grade", "text");
    assert.doesNotMatch(h.text(), /冻结参考答案|学生作答|按冻结量表复核/);
    await h.forge("grade", "text"); assert.equal(h.writes().length, 1); assert.equal(h.changes.length, 0);
  }
});

test("late rejected reads after reset cannot revoke or lock the new context", async () => {
  const gate = deferred(); let rejectOld = true;
  const h = fixture({ call: async (method, url, body, next) => {
    if (url === "/attempts/attempt-current/teacher-review" && rejectOld) { rejectOld = false; await gate.promise; throw fault(404); }
    return next(method, url, body);
  } });
  const pending = h.review.load(); await settle(); h.review.reset(); h.mount(); await h.review.openSelection({ attemptId: "attempt-retry" });
  gate.resolve(); assert.equal(await pending, false); await grade(h); await h.submit("grade", "text");
  assert.equal(h.changes.length, 1); assert.equal(h.state.reviewSelection.attemptId, "attempt-retry");
});

test("a disappearing selected old thread cannot silently redirect its confirmed draft to a new revision", async () => {
  const h = fixture({ prepare: (model) => thread(model) }); await h.review.openSelection({ threadId: "thread-history" });
  await comment(h); h.model.threads = []; await h.review.load({ force: true });
  assert.equal(h.input("discussion", "text", "text").value, "教师保留的短评");
  await h.field("discussion", "confirmed", true, "text"); await h.submit("discussion", "text");
  assert.equal(h.writes().length, 0); assert.match(h.text(), /线程.*(?:不可用|不存在|重新选择)/);
});

test("ordinary leave preserves only selection and discards private drafts until a fresh load", async () => {
  const h = fixture(); await h.review.openSelection({ attemptId: "attempt-retry" }); await grade(h); await comment(h);
  assert.equal(h.review.leave(), true); assert.equal(h.root().textContent, ""); assert.equal(h.state.reviewSelection.attemptId, "attempt-retry");
  h.mount(); await h.review.load(); assert.equal(h.input("grade", "reason", "text").value, ""); assert.equal(h.input("discussion", "text", "text").value, "");
  assert.equal(h.state.reviewSelection.attemptId, "attempt-retry"); assert.equal(h.reports.length, 0);
});
