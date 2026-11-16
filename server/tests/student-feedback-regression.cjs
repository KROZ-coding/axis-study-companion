"use strict";

// Production modules run in a VM with an in-memory API and DOM fixture only.
// No server, database, model, fetch, credential, or storage is used.
// Run with: node --test server/tests/student-feedback-regression.cjs
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const sourcePath = path.resolve(__dirname, "../../web/prototype/student-feedback.js");
const source = readFileSync(sourcePath, "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const plain = (value) => JSON.parse(JSON.stringify(value));
function decode(value) { return String(value).replace(/&(amp|lt|gt|quot|#39);/g, (_, entity) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[entity]); }

// Standalone counterpart of the notification fixture: data attributes, compound
// selectors, connected nodes and bubbled delegated events follow DOM semantics.
class TestElement {
  constructor(tag, ownerDocument, attributes = {}) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = ownerDocument; this.attributes = { ...attributes };
    this.children = []; this.parts = []; this.parentElement = null; this.listeners = new Map(); this.scrollTop = 0;
    this.disabled = Object.hasOwn(attributes, "disabled"); this.readOnly = Object.hasOwn(attributes, "readonly");
    this.value = attributes.value ?? ""; this.selectionStart = this.selectionEnd = 0;
  }
  get dataset() { return Object.fromEntries(Object.entries(this.attributes).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])); }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  append(child) { child.parentElement = this; this.children.push(child); this.parts.push(child); }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  get textContent() { return this.parts.map((part) => typeof part === "string" ? part : part.textContent).join(""); }
  clear() { this.children.forEach((child) => { child.parentElement = null; }); this.children = []; this.parts = []; }
  set innerHTML(html) {
    this.ownerDocument.paints += 1; this.clear();
    const stack = [this], voidTags = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
    for (const token of String(html).match(/<!--[\s\S]*?-->|<\/?[a-z][^>]*>|[^<]+/gi) || []) {
      if (token.startsWith("<!--")) continue;
      if (token.startsWith("</")) { const tag = token.match(/^<\/([^\s>]+)/)[1].toUpperCase(); for (let i = stack.length - 1; i > 0; i -= 1) if (stack[i].tagName === tag) { stack.length = i; break; } continue; }
      if (token.startsWith("<")) {
        const [, tag, raw] = token.match(/^<([^\s/>]+)([^]*)>$/), attrs = {};
        for (const [, name, quoted, single, bare] of raw.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[name] = decode(quoted ?? single ?? bare ?? "");
        const child = new TestElement(tag, this.ownerDocument, attrs); stack.at(-1).append(child); if (!voidTags.has(child.tagName) && !token.endsWith("/>")) stack.push(child);
      } else stack.at(-1).parts.push(decode(token));
    }
    for (const textarea of this.querySelectorAll("textarea")) textarea.value = textarea.textContent;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "disabled") this.disabled = true; if (name === "readonly") this.readOnly = true; }
  matches(selector) {
    let rest = selector.trim(), matched = true;
    while (rest) {
      const token = rest.match(/^(\*|[a-z][\w-]*)|^\.([\w-]+)|^#([\w-]+)|^\[([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?\]|^:(disabled|enabled|read-only)/i);
      assert.ok(token, `unsupported fixture selector ${selector}`);
      const [, tag, className, id, attribute, double, single, bare, pseudo] = token;
      if (tag) matched &&= tag === "*" || this.tagName === tag.toUpperCase();
      if (className) matched &&= (this.attributes.class || "").split(/\s+/).includes(className);
      if (id) matched &&= this.attributes.id === id;
      if (attribute) { const value = double ?? single ?? bare; matched &&= Object.hasOwn(this.attributes, attribute) && (value === undefined || this.attributes[attribute] === value); }
      if (pseudo) matched &&= pseudo === "disabled" ? this.disabled : pseudo === "enabled" ? !this.disabled : this.readOnly;
      rest = rest.slice(token[0].length);
    }
    return matched;
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { const selectors = selector.split(/,\s*/), result = []; const visit = (node) => { for (const child of node.children) { if (selectors.some((item) => child.matches(item))) result.push(child); visit(child); } }; visit(this); return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) { const list = this.listeners.get(type) || []; list.push(listener); this.listeners.set(type, list); }
  focus() { if (this.isConnected && !this.disabled) this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end = start) { this.selectionStart = start; this.selectionEnd = end; }
  fire(type, extras = {}) {
    const event = { type, target: this, currentTarget: null, bubbles: true, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extras };
    const route = [], results = [];
    for (let node = this; node; node = node.parentElement) route.push(node);
    for (const node of route) {
      event.currentTarget = node;
      for (const listener of [...(node.listeners.get(type) || [])]) results.push(listener(event));
      if (!event.bubbles || event.stopped) break;
    }
    event.currentTarget = null; event.finished = Promise.all(results); return event;
  }
}
function makeDocument() { const document = { paints: 0 }; document.body = new TestElement("body", document); document.activeElement = document.body; document.querySelector = (selector) => document.body.querySelector(selector); document.querySelectorAll = (selector) => document.body.querySelectorAll(selector); return document; }
function loadFactory() {
  let serial = 0;
  const context = vm.createContext({ console, URLSearchParams, AbortController, crypto: { randomUUID: () => `regression-${++serial}` }, setImmediate, setTimeout, clearTimeout });
  vm.runInContext(source.replace(/^export /gm, "") + "\nglobalThis.production = { createStudentFeedback };", context, { filename: sourcePath });
  return context.production.createStudentFeedback;
}

function harness(options = {}) {
  const currentTask = {
    id: "task-current", student_id: "student-1", title: "当前反馈任务", status: "results_ready", answer_released: true,
    attempts: [{ id: "attempt-current", attempt_no: 1, kind: "first", status: "graded", revision: 2, total_score: 0, max_score: 10, answers: { q1: "A" }, items: [{ question_id: "q1", score: 0 }] }],
    questions: [{ id: "q1", version: 1, question_version_id: "q1:1", stem: "当前题干", points: 10, explanation: "服务器允许展示的解析" }],
  };
  const oldTask = {
    id: "task-history", student_id: "student-1", title: "历史反馈任务", status: "closed", answer_released: false,
    attempts: [{ id: "attempt-history", attempt_no: 1, kind: "first", revision: 1, status: "submitted", answers: { old: "HIDDEN-HISTORY-ANSWER" }, items: [{ question_id: "old", score: 7 }] }],
    questions: [{ id: "old", version: 1, question_version_id: "old:1", stem: "历史题干", points: 7 }],
  };
  const currentThread = { id: "thread-current", student_id: "student-1", task_id: currentTask.id, attempt_id: "attempt-current", question_version_id: "q1:1", grading_revision: 2, title: "教师短评", can_reply: true };
  const oldThread = { id: "thread-history", student_id: "student-1", task_id: oldTask.id, attempt_id: "attempt-history", question_version_id: "old:1", grading_revision: 1, title: "教师短评", can_reply: true };
  const state = { live: true, role: "student", studentTab: "feedback", transitionId: 1, courseId: "course-1", classId: "class-1", loggingOut: false };
  const tasks = new Map([currentTask, oldTask].map((row) => [row.id, row])), threads = new Map([currentThread, oldThread].map((row) => [row.id, row]));
  const messages = {
    [currentThread.id]: [{ id: "teacher-current-hidden", thread_id: currentThread.id, author_id: "teacher-1", author_role: "teacher", text: "HIDDEN-TEACHER-TEXT", redacted: true, delivery: "deferred", notice: "教师短评将在本题结果开放后显示。" }],
    [oldThread.id]: [{ id: "teacher-history-hidden", thread_id: oldThread.id, author_id: "teacher-1", author_role: "teacher", text: "HIDDEN-HISTORY-TEACHER", redacted: true, delivery: "deferred", notice: "历史结果尚未开放。" }],
  };
  const calls = [], replyBodies = [], appealBodies = [], toasts = [], changes = [], appeals = new Map(), replyReceipts = new Map(), appealReceipts = new Map();
  const optionsState = { ...options };
  const api = { state: { user: { id: "student-1", role: "student", status: "active" } }, call };
  function confirmReply(threadId, body, authorId = "student-1") {
    if (!replyReceipts.has(body.request_id)) {
      const message = { id: `message-${replyReceipts.size + 1}`, thread_id: threadId, author_id: authorId, author_role: "student", text: body.text, delivery: "delivered" };
      messages[threadId].push(message); replyReceipts.set(body.request_id, message);
    }
    return plain(replyReceipts.get(body.request_id));
  }
  function confirmAppeal(attemptId, body, studentId = "student-1") {
    if (!appealReceipts.has(body.request_id)) {
      const task = [...tasks.values()].find((row) => row.attempts.some((attempt) => attempt.id === attemptId)); assert.ok(task);
      const id = `appeal-${appealReceipts.size + 1}`, row = { id, task_id: task.id, attempt_id: attemptId, student_id: studentId, question_id: body.question_id, grading_revision: body.grading_revision, reason: body.reason, status: "pending" };
      appeals.set(task.id, [...(appeals.get(task.id) || []), row]); appealReceipts.set(body.request_id, { id });
    }
    return plain(appealReceipts.get(body.request_id));
  }
  function response(url) {
    if (url.startsWith("/tasks?")) {
      const query = new URLSearchParams(url.split("?")[1]);
      const rows = optionsState.taskRows || [...tasks.values()].map((row) => ({ id: row.id, goal_id: row.goal_id, student_id: row.student_id, title: row.title, goal_title: row.goal_title, name: row.name, status: row.status, answers: { leak: "LIST-ANSWER-LEAK" }, attempts: [{ id: "bad-list-attempt", total_score: 100 }], review: "LIST-REVIEW-LEAK" }));
      return plain(rows.slice(Number(query.get("offset")), Number(query.get("offset")) + Number(query.get("limit"))));
    }
    if (/^\/tasks\/[^/]+$/.test(url)) return plain(tasks.get(decodeURIComponent(url.split("/")[2])) || null);
    if (/^\/tasks\/[^/]+\/appeals$/.test(url)) return { items: plain(appeals.get(decodeURIComponent(url.split("/")[2])) || []) };
    if (url.startsWith("/companion/discussion-threads?")) {
      const query = new URLSearchParams(url.split("?")[1]);
      const rows = optionsState.threadRows || [...threads.values()].filter((row) => row.task_id === query.get("task_id"));
      return plain(rows.slice(Number(query.get("offset")), Number(query.get("offset")) + Number(query.get("limit"))));
    }
    if (/^\/companion\/discussion-threads\/[^/]+$/.test(url)) {
      const id = decodeURIComponent(url.split("/").at(-1));
      return threads.has(id) ? { ...plain(threads.get(id)), messages: plain(messages[id] || []) } : null;
    }
    throw new Error(`unexpected GET ${url}`);
  }
  async function call(method, url, body, requestOptions) {
    const request = { method, url, body: body === undefined ? undefined : plain(body), options: requestOptions }; calls.push(request);
    if (method === "GET") return optionsState.get ? optionsState.get(request, () => response(url)) : response(url);
    if (method === "POST" && /^\/companion\/discussion-threads\/[^/]+\/messages$/.test(url)) {
      replyBodies.push(plain(body)); const threadId = decodeURIComponent(url.split("/")[3]), actorId = api.state.user.id;
      if (optionsState.replyGate) return optionsState.replyGate.promise;
      const confirm = () => confirmReply(threadId, body, actorId);
      return optionsState.reply ? optionsState.reply(request, confirm) : confirm();
    }
    if (method === "POST" && /^\/attempts\/[^/]+\/appeals$/.test(url)) {
      appealBodies.push(plain(body)); const attemptId = decodeURIComponent(url.split("/")[2]), actorId = api.state.user.id;
      if (optionsState.appealGate) return optionsState.appealGate.promise;
      const confirm = () => confirmAppeal(attemptId, body, actorId);
      return optionsState.appeal ? optionsState.appeal(request, confirm) : confirm();
    }
    throw new Error(`unexpected API request ${method} ${url}`);
  }
  const document = makeDocument(); let instance, renders = 0;
  const render = () => { renders += 1; document.body.innerHTML = instance.render(); instance.bind(document.body); };
  instance = (options.factory || loadFactory())({ api, state, render, toast: (text) => toasts.push(text), onChanged: async (selection) => { changes.push(plain(selection)); if (optionsState.changedError) throw new Error("notification refresh failed"); } });
  render();
  const node = (selector) => { const found = document.querySelector(selector); assert.ok(found, `missing production control ${selector}`); return found; };
  const replyForm = () => node('form[data-feedback-form="reply"][data-feedback-thread-id="thread-current"]');
  const appealForm = () => node('form[data-feedback-form="appeal"][data-feedback-attempt-id="attempt-current"][data-feedback-question-id="q1"]');
  const open = () => instance.openSelection({ taskId: "task-current", attemptId: "attempt-current" });
  const appeal = (patch = {}) => instance.submitAppeal({ taskId: "task-current", attemptId: "attempt-current", questionId: "q1", gradingRevision: 2, reason: "分数需要复核", ...patch });
  return { currentTask, oldTask, currentThread, oldThread, tasks, threads, messages, appeals, replyReceipts, appealReceipts, api, state, calls, replyBodies, appealBodies, optionsState, toasts, changes,
    confirmReply, confirmAppeal, document, feedback: instance, render, node, replyForm, appealForm, open, appeal, posts: () => calls.filter((call) => call.method === "POST"), renders: () => renders, html: () => document.body.textContent };
}
function addThread(h, patch, text) { const thread = { ...h.currentThread, ...patch }; h.threads.set(thread.id, thread); h.messages[thread.id] = [{ id: `message-${thread.id}`, thread_id: thread.id, author_id: "teacher-1", author_role: "teacher", text, redacted: false, delivery: "delivered" }]; return thread; }
async function fill(form, text) { const field = form.querySelector("textarea"); assert.ok(field); assert.equal(field.readOnly, false); field.value = text; await field.fire("input").finished; }
async function submit(form) { const event = form.fire("submit"); assert.equal(event.defaultPrevented, true); await event.finished; }
function write(h, kind, text) { return kind === "reply" ? h.feedback.sendReply("thread-current", text || "待确认回复") : h.appeal({ reason: text || "待确认异议" }); }
function confirm(h, kind) { return kind === "reply" ? h.confirmReply("thread-current", h.replyBodies.at(-1)) : h.confirmAppeal("attempt-current", h.appealBodies.at(-1)); }

test("loads current and historical tasks through delegated controls and uses detail projections only", async () => {
  const h = harness(); assert.equal(await h.feedback.load(), true);
  assert.ok(h.calls.some((call) => call.url === "/tasks?limit=100&offset=0"));
  assert.ok(h.calls.some((call) => call.url === "/tasks/task-current"));
  assert.match(h.html(), /当前题干/); assert.match(h.html(), /0 \/ 10 分/);
  assert.doesNotMatch(h.html(), /LIST-ANSWER-LEAK|LIST-REVIEW-LEAK|HIDDEN-TEACHER-TEXT|bad-list-attempt/);
  assert.match(h.html(), /教师短评将在本题结果开放后显示/);
  await h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]').querySelector("strong").fire("click").finished;
  assert.match(h.html(), /历史题干/); assert.match(h.html(), /历史结果尚未开放/);
  assert.doesNotMatch(h.html(), /当前题干|HIDDEN-HISTORY-ANSWER|HIDDEN-HISTORY-TEACHER/);
  assert.equal(h.posts().length, 0);
});

test("native ESM cold load renders the task catalog before detail completion and after host remount", async () => {
  // Keep the original export/module scope: no VM export stripping or host globals.
  const { createStudentFeedback } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  const h = harness({ factory: createStudentFeedback }), detailGate = deferred();
  delete h.currentTask.title; delete h.oldTask.title;
  h.currentTask.goal_id = "goal-current"; h.oldTask.goal_id = "goal-history";
  h.state.liveGoals = [{ id: "goal-current", title: "快捷导航的学习目标" }, { id: "goal-history", title: "历史目标" }];
  h.state.liveTasks = [plain(h.currentTask), plain(h.oldTask)];
  h.optionsState.taskRows = [{ ...plain(h.currentTask), id: "foreign-task", student_id: "student-other", title: "FOREIGN-CATALOG" }, h.currentTask, h.oldTask];
  h.optionsState.get = ({ url }, response) => {
    if (url.startsWith("/tasks?")) return { items: response() };
    return url === "/tasks/task-current" ? detailGate.promise : response();
  };
  h.state.studentTab = "tasks"; h.feedback.reset(); h.document.body.innerHTML = "";
  h.state.studentTab = "feedback"; h.render();
  assert.equal(h.document.querySelector('[data-feedback-action="select-task"]'), null);
  assert.equal(h.document.querySelector(".feedback-detail-head"), null);
  const pending = h.feedback.load(); await settle();
  assert.equal(h.calls[0].url, "/tasks?limit=100&offset=0");
  // The host can rebuild its page while the automatic first-task GET is pending.
  h.render();
  assert.equal(h.document.querySelector(".feedback-detail-head"), null, "no selected detail has been returned yet");
  assert.deepEqual(h.document.querySelectorAll('[data-feedback-action="select-task"]').map((node) => node.querySelector("strong").textContent), ["快捷导航的学习目标", "历史目标"]);
  assert.equal(h.document.querySelector('[role="alert"]'), null);
  assert.equal(h.document.querySelector('[data-feedback-action="select-task"][data-feedback-task-id="foreign-task"]'), null);
  detailGate.resolve(plain(h.currentTask)); assert.equal(await pending, true);
  assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, "快捷导航的学习目标");
  assert.equal(h.document.querySelector('[role="alert"]'), null);
  await h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]').querySelector("strong").fire("click").finished;
  assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, "历史目标");
  assert.equal(h.feedback.leave(), true); h.state.studentTab = "tasks"; h.document.body.innerHTML = "";
  h.state.studentTab = "feedback"; h.render(); h.optionsState.get = undefined;
  assert.equal(await h.feedback.load(), true); h.render();
  assert.equal(h.document.querySelectorAll('[data-feedback-action="select-task"]').length, 2);
  assert.equal(h.document.querySelector('[role="alert"]'), null); assert.equal(h.posts().length, 0);
});

test("native ESM unmounted cold load uses the host render callback for catalog and detail", async () => {
  const { createStudentFeedback } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);
  const h = harness({ factory: createStudentFeedback });
  delete h.currentTask.title; h.currentTask.goal_id = "goal-current";
  h.state.liveGoals = [{ id: "goal-current", title: "首次进入反馈" }];
  h.feedback.reset(); h.document.body.innerHTML = "";
  const renders = h.renders();
  assert.equal(await h.feedback.load(), true);
  assert.ok(h.renders() > renders, "load must mount through the supplied host renderer");
  assert.equal(h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-current"]').querySelector("strong").textContent, "首次进入反馈");
  assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, "首次进入反馈");
  assert.equal(h.document.querySelector('[role="alert"]'), null); assert.equal(h.calls.length, 5); assert.equal(h.posts().length, 0);
});

test("uses the loaded goal title for titleless TaskDTO rows and selected detail without extra requests", async () => {
  const h = harness(); delete h.currentTask.title; delete h.oldTask.title;
  h.currentTask.goal_id = "goal-current"; h.oldTask.goal_id = "goal-history";
  h.state.liveGoals = [{ id: "goal-current", title: "整数运算 <巩固> & 检查" }, { id: "goal-history", title: "历史学习目标" }];
  h.state.liveTasks = [plain(h.currentTask), plain(h.oldTask)];
  const title = (id) => h.node(`[data-feedback-action="select-task"][data-feedback-task-id="${id}"]`).querySelector("strong").textContent;
  assert.equal(await h.feedback.load(), true);
  assert.equal(title("task-current"), "整数运算 <巩固> & 检查"); assert.equal(title("task-history"), "历史学习目标");
  assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, "整数运算 <巩固> & 检查");
  assert.equal(h.calls.length, 5, "title lookup uses only the task/list/thread/appeal reads already required");
  await h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]').querySelector("strong").fire("click").finished;
  assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, "历史学习目标");
  const before = h.calls.length; h.state.liveGoals[0].title = "已更新的学习目标"; h.render();
  assert.equal(title("task-current"), "已更新的学习目标", "navigation resolves the current loaded goal, not a cached fallback");
  assert.equal(h.calls.length, before); assert.equal(h.posts().length, 0);
});

test("title lookup retains read task fields and safely falls back when no exact loaded goal exists", async () => {
  const h = harness(); h.currentTask.goal_id = "goal-current";
  h.state.liveGoals = [{ id: "goal-other", title: "UNRELATED-GOAL" }];
  h.state.liveTasks = [{ id: "task-current", student_id: "student-2", goal_id: "goal-other", title: "FOREIGN-CACHED-TITLE" }];
  for (const [field, value] of [["title", "任务专用标题"], ["goal_title", "已读任务的目标标题"], ["name", "已读任务名称"], [null, "学习任务 · task-cur"]]) {
    delete h.currentTask.title; delete h.currentTask.goal_title; delete h.currentTask.name;
    if (field) h.currentTask[field] = value;
    assert.equal(await h.feedback.load(), true);
    assert.equal(h.node(".feedback-detail-head").querySelector("h2").textContent, value);
    assert.equal(h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-current"]').querySelector("strong").textContent, value);
    assert.doesNotMatch(h.html(), /UNRELATED-GOAL|FOREIGN-CACHED-TITLE/);
  }
  assert.equal(h.calls.length, 20, "four refreshes add no title-specific API requests");
  h.api.state.user = { id: "student-2", role: "student", status: "active" }; h.render();
  assert.equal(h.document.querySelector(".feedback-detail-head"), null);
  assert.equal(h.document.querySelector('[data-feedback-action="select-task"]'), null);
});

test("formats choice indices, arrays and judge booleans from the selected frozen question", async () => {
  const h = harness();
  h.currentTask.questions = [
    { id: "q1", version: 1, type: "single_choice", stem: "2 + 2 = ?", options: ["3", "4", "5"], points: 10 },
    { id: "multi", version: 2, type: "multi_choice", stem: "多选题", options: ["<第一项>", "第二项", "第三项 & 检查"], points: 5 },
    { id: "judge-true", version: 1, type: "judge", stem: "判断一", options: [], points: 1 },
    { id: "judge-false", version: 1, type: "judge", stem: "判断二", options: [], points: 1 },
    { id: "legacy", version: 1, stem: "已有选项的兼容题", options: ["旧甲", "旧乙"], points: 1 },
  ];
  h.currentTask.attempts[0].answers = { q1: 1, multi: [0, 2], "judge-true": true, "judge-false": false, legacy: 0 };
  h.currentTask.attempts.push({ id: "attempt-retry", kind: "retry", attempt_no: 2, revision: 1, answers: { q1: 2, multi: [1], "judge-true": false, "judge-false": true, legacy: 1 }, items: [] });
  h.state.liveTasks = [{ ...plain(h.currentTask), questions: [{ id: "q1", type: "single_choice", options: ["UNFROZEN-A", "UNFROZEN-B", "UNFROZEN-C"] }] }];
  const answer = (id, version = 1) => h.node(`[data-feedback-question-version-id="${id}:${version}"]`).querySelectorAll("p").find((node) => node.textContent.startsWith("你的作答："))?.textContent;
  assert.equal(await h.open(), true);
  assert.equal(answer("q1"), "你的作答：B. 4"); assert.equal(answer("multi", 2), "你的作答：A. <第一项>；C. 第三项 & 检查");
  assert.equal(answer("judge-true"), "你的作答：正确"); assert.equal(answer("judge-false"), "你的作答：错误"); assert.equal(answer("legacy"), "你的作答：A. 旧甲");
  assert.doesNotMatch(h.html(), /UNFROZEN-/);
  await h.node('[data-feedback-action="select-attempt"][data-feedback-attempt-id="attempt-retry"]').querySelector("span").fire("click").finished;
  assert.equal(answer("q1"), "你的作答：C. 5"); assert.equal(answer("multi", 2), "你的作答：B. 第二项");
  assert.equal(answer("judge-true"), "你的作答：错误"); assert.equal(answer("judge-false"), "你的作答：正确");
  h.currentTask.status = "in_progress"; h.currentTask.answer_released = false; assert.equal(await h.feedback.load(), true);
  assert.equal(answer("q1"), undefined); assert.equal(answer("multi", 2), undefined); assert.equal(answer("judge-false"), undefined);
  assert.equal(h.posts().length, 0);
});

test("labels fill blanks from public format only and preserves missing answers and free text", async () => {
  const h = harness();
  const cases = [
    { id: "blanks", type: "fill", blank_count: 3, value: ["甲", "", "丙"], text: "第 1 空：甲；第 2 空：未作答；第 3 空：丙" },
    { id: "format", type: "fill", answer_format: { blank_count: 2 }, value: ["第一空"], text: "第 1 空：第一空；第 2 空：未作答" },
    { id: "equivalent", type: "fill", answer: ["PROTECTED-KEY-ONE", "PROTECTED-KEY-TWO"], value: ["北京", "北京市"], text: "北京 或 北京市" },
    { id: "empty-blanks", type: "fill", blank_count: 2, value: [], text: "第 1 空：未作答；第 2 空：未作答" },
    { id: "empty", type: "fill", value: " \n ", text: "未作答" },
    { id: "empty-choice", type: "multi_choice", options: ["甲", "乙"], value: [], text: "未作答" },
    { id: "empty-judge", type: "judge", value: null, text: "未作答" },
    { id: "short", type: "short_answer", value: '<img src=x> & "原文"', text: '<img src=x> & "原文"' },
    { id: "case", type: "case", value: "先观察；再解释", text: "先观察；再解释" },
  ];
  h.currentTask.questions = cases.map(({ id, type, blank_count, answer_format, answer, options }) => ({ id, version: 1, type, blank_count, answer_format, answer, options: options || [], stem: `题目 ${id}`, points: 1 }));
  h.currentTask.attempts[0].answers = Object.fromEntries(cases.map(({ id, value }) => [id, value]));
  assert.equal(await h.open(), true);
  for (const { id, text } of cases) {
    const paragraph = h.node(`[data-feedback-question-version-id="${id}:1"]`).querySelectorAll("p").find((node) => node.textContent.startsWith("你的作答："));
    assert.equal(paragraph?.textContent, `你的作答：${text}`, id);
  }
  assert.equal(h.document.querySelector("img"), null); assert.doesNotMatch(h.html(), /PROTECTED-KEY-/);
  const before = h.calls.length; h.render(); assert.equal(h.calls.length, before); assert.equal(h.posts().length, 0);
});

test("binds exact task, attempt, thread and question version instead of falling back to latest", async () => {
  const h = harness();
  h.currentTask.questions.push({ id: "q2", version: 2, question_version_id: "q2:2", stem: "第二题", points: 5 });
  h.currentTask.attempts[0].items.push({ question_id: "q2", score: 2 });
  h.currentTask.attempts.push({ id: "attempt-retry", kind: "retry", attempt_no: 2, revision: 3, total_score: 7, max_score: 15, answers: { q1: "RETRY-ANSWER" }, items: [{ question_id: "q1", score: 7 }] });
  addThread(h, { id: "thread-current" }, "FIRST-Q1"); addThread(h, { id: "thread-second", question_version_id: "q2:2" }, "FIRST-Q2");
  addThread(h, { id: "thread-retry", attempt_id: "attempt-retry" }, "RETRY-Q1");
  assert.equal(await h.feedback.openSelection({ threadId: "thread-current", questionVersionId: "q1@1" }), true);
  const q1 = h.node('.feedback-question[data-feedback-question-version-id="q1:1"]'), q2 = h.node('.feedback-question[data-feedback-question-version-id="q2:2"]');
  assert.equal(q1.matches(".is-selected"), true); assert.match(q1.textContent, /FIRST-Q1/); assert.doesNotMatch(q1.textContent, /FIRST-Q2|RETRY-Q1/);
  assert.match(q2.textContent, /FIRST-Q2/); assert.doesNotMatch(q2.textContent, /FIRST-Q1|RETRY-Q1/);
  assert.equal(h.node('[data-feedback-thread="thread-current"]').matches(".is-selected"), true);
  await h.node('[data-feedback-action="select-attempt"][data-feedback-attempt-id="attempt-retry"]').querySelector("span").fire("click").finished;
  assert.match(h.html(), /RETRY-Q1|RETRY-ANSWER/); assert.doesNotMatch(h.html(), /FIRST-Q1|FIRST-Q2/);
  assert.equal(await h.feedback.sendReply("thread-current", "不能回复另一轮"), false);
  assert.equal(await h.feedback.sendReply("thread-retry", "本轮回复"), true);
  assert.equal(h.posts().at(-1).url, "/companion/discussion-threads/thread-retry/messages");
  for (const selection of [
    { taskId: "task-current", threadId: "thread-history" },
    { taskId: "task-current", attemptId: "attempt-retry", threadId: "thread-current" },
    { taskId: "task-current", threadId: "thread-current", questionVersionId: "q2:2" },
    { taskId: "task-current", attemptId: "missing" },
    { taskId: "task-current", questionVersionId: "q1:99" },
    { attemptId: "attempt-current" },
  ]) {
    assert.equal(await h.feedback.openSelection(selection), false, JSON.stringify(selection));
    assert.equal(h.document.querySelector(".feedback-question"), null);
  }
  assert.equal(h.posts().length, 1);
});

test("rejects foreign task/thread details and excludes foreign or other-attempt list rows", async () => {
  const h = harness();
  h.optionsState.threadRows = [h.currentThread, { ...h.currentThread, id: "foreign-thread", student_id: "student-2" }, { ...h.currentThread, id: "wrong-attempt", attempt_id: "attempt-other" }, { ...h.currentThread, id: "wrong-task", task_id: "other-task" }];
  h.tasks.set("foreign-task", { ...plain(h.oldTask), id: "foreign-task", student_id: "student-2", title: "FOREIGN-TITLE" });
  assert.equal(await h.open(), true); assert.doesNotMatch(h.html(), /FOREIGN-TITLE/);
  assert.equal(h.calls.some((call) => /\/(foreign-thread|wrong-attempt|wrong-task)$/.test(call.url)), false);
  for (const patch of [{ id: "wrong-thread" }, { student_id: "student-2" }, { attempt_id: "other-attempt" }, { task_id: "other-task" }, { messages: null }]) {
    h.optionsState.get = ({ url }, response) => url === "/companion/discussion-threads/thread-current" ? { ...response(), ...patch } : response();
    assert.equal(await h.open(), false); assert.equal(h.document.querySelector(".feedback-question"), null);
    assert.equal(await h.feedback.sendReply("thread-current", "无权限"), false);
  }
  h.optionsState.get = undefined;
  assert.equal(await h.feedback.openSelection({ taskId: "foreign-task" }), false);
  assert.doesNotMatch(h.html(), /FOREIGN-TITLE/); assert.equal(h.posts().length, 0);
});

test("task and discussion pagination read beyond the first 100 rows", async () => {
  const h = harness();
  h.optionsState.taskRows = [h.currentTask, ...Array.from({ length: 99 }, (_, index) => ({ ...h.oldTask, id: `history-${index}` })), h.oldTask];
  for (let index = 0; index < 100; index += 1) addThread(h, { id: `paged-thread-${index}` }, `短评 ${index}`);
  assert.equal(await h.open(), true);
  assert.ok(h.calls.some((call) => call.url === "/tasks?limit=100&offset=100"));
  assert.ok(h.calls.some((call) => call.url === "/companion/discussion-threads?task_id=task-current&limit=100&offset=100"));
  assert.ok(h.node('[data-feedback-thread="paged-thread-99"]'));
  assert.equal(h.document.querySelectorAll("article[data-feedback-thread]").length, 101);
  assert.ok(h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]'));
});

test("malformed or repeating pages fail closed instead of retaining stale detail or looping", async () => {
  const h = harness(); await h.open();
  h.optionsState.get = ({ url }, response) => url.startsWith("/tasks?") ? { items: null } : response();
  assert.equal(await h.feedback.load(), false); assert.equal(h.document.querySelector(".feedback-question"), null);
  const rows = Array.from({ length: 100 }, (_, index) => ({ ...h.oldTask, id: `repeated-${index}` }));
  const before = h.calls.length; h.optionsState.get = () => rows;
  assert.equal(await h.feedback.load(), false); assert.equal(h.calls.length - before, 2);
  assert.match(h.html(), /分页未能前进/); assert.equal(h.posts().length, 0);
});

test("release, redo and re-release replace teacher messages, answers, grades and appeal projections", async () => {
  const h = harness(); addThread(h, { id: "thread-current" }, "PUBLISHED-TEACHER");
  h.currentTask.questions[0].explanation = "PUBLISHED-EXPLANATION"; h.currentTask.attempts[0].answers.q1 = "PUBLISHED-ANSWER";
  h.appeals.set("task-current", [{ id: "appeal-existing", task_id: "task-current", attempt_id: "attempt-current", student_id: "student-1", question_id: "q1", grading_revision: 2, reason: "PUBLISHED-REASON", status: "awaiting_teacher", result: { review: { explanation: "PUBLISHED-REVIEW" } } }]);
  await h.open();
  for (const text of ["PUBLISHED-TEACHER", "PUBLISHED-EXPLANATION", "PUBLISHED-ANSWER", "PUBLISHED-REASON", "PUBLISHED-REVIEW"]) assert.ok(h.html().includes(text));
  h.currentTask.status = "in_progress"; h.currentTask.answer_released = false;
  h.currentTask.attempts.push({ id: "attempt-retry", kind: "retry", attempt_no: 2, revision: 0, status: "in_progress", answers: { q1: "UNRELEASED-RETRY-ANSWER" } });
  h.messages["thread-current"][0].redacted = true; h.messages["thread-current"][0].delivery = "deferred";
  h.appeals.get("task-current")[0].redacted = true;
  assert.equal(await h.feedback.load(), true);
  assert.doesNotMatch(h.html(), /PUBLISHED-|UNRELEASED-RETRY-ANSWER/);
  assert.match(h.node(".feedback-score").textContent, /待评分/);
  assert.match(h.html(), /首次作答.*0 \/ 10 分/, "the published first total is preserved during a retry");
  assert.equal(h.document.querySelector('[data-feedback-form="appeal"]'), null);
  await h.node('[data-feedback-action="select-attempt"][data-feedback-attempt-id="attempt-retry"]').fire("click").finished;
  assert.equal(h.document.querySelector('[data-feedback-thread="thread-current"]'), null);
  assert.doesNotMatch(h.html(), /PUBLISHED-|UNRELEASED-RETRY-ANSWER/);
  h.currentTask.status = "results_ready"; h.currentTask.answer_released = true; h.currentTask.questions[0].explanation = "NEW-EXPLANATION";
  Object.assign(h.currentTask.attempts[1], { revision: 1, status: "graded", answers: { q1: "NEW-ANSWER" }, items: [{ question_id: "q1", score: 8 }], total_score: 8, max_score: 10 });
  addThread(h, { id: "thread-retry", attempt_id: "attempt-retry", grading_revision: 1 }, "NEW-TEACHER");
  assert.equal(await h.feedback.load(), true);
  for (const text of ["NEW-EXPLANATION", "NEW-ANSWER", "NEW-TEACHER"]) assert.ok(h.html().includes(text));
  assert.doesNotMatch(h.html(), /PUBLISHED-|UNRELEASED-/);
  assert.equal(h.node('[data-feedback-form="appeal"]').dataset.feedbackAttemptId, "attempt-retry");
});

test("current can_reply must be literal true and denied refresh removes previously writable content", async () => {
  const h = harness();
  for (const permission of [undefined, false, "true", 1]) {
    h.currentThread.can_reply = permission; assert.equal(await h.open(), true);
    assert.equal(h.document.querySelector('[data-feedback-form="reply"]'), null);
    assert.equal(await h.feedback.sendReply("thread-current", "禁止发送"), false);
  }
  h.currentThread.can_reply = true; assert.equal(await h.feedback.load(), true);
  await fill(h.replyForm(), '问题 <img src=x> & "说明"'); await submit(h.replyForm());
  assert.equal(h.replyBodies.length, 1); assert.equal(h.replyBodies[0].text, '问题 <img src=x> & "说明"');
  assert.equal(h.document.querySelector("img"), null, "message text is escaped instead of becoming markup");
  h.optionsState.get = ({ url }, response) => { if (url === "/companion/discussion-threads/thread-current") throw new Error("当前讨论已无访问权限"); return response(); };
  assert.equal(await h.feedback.load(), false); assert.equal(h.document.querySelector('[data-feedback-form="reply"]'), null);
  assert.equal(await h.feedback.sendReply("thread-current", "旧权限不能写入"), false); assert.equal(h.replyBodies.length, 1);
});

test("appeals use the selected task, attempt, item and current grading revision exactly", async () => {
  const h = harness(); h.currentTask.questions.push({ id: "q2", version: 1, stem: "未评分题", points: 5 }); await h.open();
  for (const patch of [{ taskId: "task-history" }, { attemptId: "attempt-history" }, { questionId: "missing" }, { questionId: "q2" }, { gradingRevision: 1 }, { gradingRevision: 0 }, { gradingRevision: 2.5 }, { gradingRevision: NaN }]) assert.equal(await h.appeal(patch), false);
  assert.equal(h.document.querySelector('[data-feedback-form="appeal"][data-feedback-question-id="q2"]'), null); assert.equal(h.posts().length, 0);
  h.currentTask.attempts[0].items.push({ question_id: "q2", score: 1 }); await h.feedback.load();
  const form = h.node('[data-feedback-form="appeal"][data-feedback-question-id="q2"]');
  assert.equal(form.dataset.feedbackTaskId, "task-current"); assert.equal(form.dataset.feedbackAttemptId, "attempt-current"); assert.equal(form.dataset.feedbackRevision, "2");
  await fill(form, "第二题需要复核"); await submit(form);
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].url, "/attempts/attempt-current/appeals");
  assert.deepEqual(h.appealBodies[0], { request_id: h.appealBodies[0].request_id, question_id: "q2", grading_revision: 2, reason: "第二题需要复核" });
  assert.equal(await h.appeal({ questionId: "q2" }), false, "confirmed appeal cannot be submitted twice");
});

test("appeal display ignores other attempts, questions, revisions and students", async () => {
  const h = harness();
  const appeal = { id: "old-appeal", task_id: "task-current", attempt_id: "attempt-current", student_id: "student-1", question_id: "q1", grading_revision: 2, status: "pending", reason: "UNRELATED-REASON" };
  h.appeals.set("task-current", [{ ...appeal, attempt_id: "attempt-history" }, { ...appeal, question_id: "q-other" }, { ...appeal, grading_revision: 1 }, { ...appeal, student_id: "student-2" }]);
  await h.open(); assert.doesNotMatch(h.html(), /UNRELATED-REASON/); assert.ok(h.appealForm());
  h.appeals.get("task-current").push({ ...appeal, id: "current-appeal", reason: "本轮说明", status: "awaiting_teacher", result: { review: { reason: "等待教师核验" } } });
  await h.feedback.load(); assert.match(h.html(), /本轮说明/); assert.match(h.html(), /等待教师核验/); assert.match(h.html(), /正式成绩以已发布记录为准/);
  assert.equal(await h.appeal(), false); assert.equal(h.document.querySelector('[data-feedback-form="appeal"]'), null); assert.equal(h.posts().length, 0);
});

test("reply retries preserve the exact id and text after a committed response is lost", async () => {
  const h = harness(); let loseReply = true;
  h.optionsState.reply = (_, confirmResponse) => { const receipt = confirmResponse(); if (loseReply) { loseReply = false; throw new Error("回执丢失"); } return receipt; };
  await h.open(); await fill(h.replyForm(), "第一次说明 & 保留原文"); await submit(h.replyForm());
  const first = plain(h.replyBodies[0]); assert.match(first.request_id, /^feedback-message-/);
  assert.equal(h.replyForm().querySelector("textarea").readOnly, true); assert.equal(h.replyForm().querySelector("textarea").value, first.text);
  assert.match(h.replyForm().textContent, /重试确认发送/); assert.equal(h.feedback.leave(), false);
  const before = h.calls.length; assert.equal(await h.feedback.load(), false); assert.equal(await h.feedback.openSelection({ taskId: "task-history" }), false); assert.equal(await h.appeal(), false);
  await settle(); assert.equal(h.calls.length, before); assert.equal(h.replyBodies.length, 1);
  const oldForm = h.replyForm(); h.render(); await oldForm.fire("submit").finished; assert.equal(h.replyBodies.length, 1);
  assert.equal(await h.feedback.sendReply("thread-current", "改写不能替换原请求"), true);
  assert.deepEqual(h.replyBodies[1], first); assert.equal(h.replyReceipts.size, 1);
  assert.equal(h.messages["thread-current"].filter((message) => message.author_role === "student").length, 1);
  assert.equal(h.changes.length, 1); assert.equal(h.feedback.leave(), true);
});

test("appeal retries preserve exact reason/revision/id and confirmation is not a grade change", async () => {
  const h = harness(); let loseAppeal = true;
  h.optionsState.appeal = (_, confirmResponse) => { const receipt = confirmResponse(); if (loseAppeal) { loseAppeal = false; throw new Error("提交结果未知"); } return receipt; };
  await h.open(); await fill(h.appealForm(), "请核对评分依据"); await submit(h.appealForm());
  const first = plain(h.appealBodies[0]); assert.match(first.request_id, /^feedback-appeal-/);
  assert.equal(h.appealForm().querySelector("textarea").readOnly, true); assert.equal(h.appealForm().querySelector("textarea").value, first.reason);
  assert.match(h.appealForm().textContent, /重试确认提交/); assert.equal(h.feedback.leave(), false);
  assert.equal(await h.feedback.sendReply("thread-current", "不能另发消息"), false);
  assert.equal(await h.appeal({ reason: "新原因不得替换原原因" }), true);
  assert.deepEqual(h.appealBodies[1], first); assert.equal(h.appealReceipts.size, 1); assert.equal(h.appeals.get("task-current").length, 1);
  assert.equal(h.currentTask.attempts[0].total_score, 0); assert.match(h.html(), /0 \/ 10 分/);
  assert.equal(await h.appeal(), false); assert.equal(h.appealBodies.length, 2); assert.equal(h.feedback.leave(), true);
});

test("unknown or wrong reply receipts keep the same explicit command pending", async () => {
  const h = harness(); await h.open();
  const base = { id: "receipt", thread_id: "thread-current", author_id: "student-1", author_role: "student" };
  for (const receipt of [undefined, {}, { ...base, id: " " }, { ...base, thread_id: "thread-history" }, { ...base, author_id: "student-2" }, { ...base, author_role: "teacher" }]) {
    h.optionsState.reply = () => receipt;
    assert.equal(await h.feedback.sendReply("thread-current", "原始消息"), false); assert.equal(h.feedback.leave(), false);
    const posts = h.posts().length; await settle(); assert.equal(h.posts().length, posts);
    assert.deepEqual(h.replyBodies.at(-1), h.replyBodies[0]);
  }
  h.optionsState.reply = undefined; assert.equal(await h.feedback.sendReply("thread-current", "不能覆盖"), true);
  assert.deepEqual(h.replyBodies.at(-1), h.replyBodies[0]); assert.equal(h.changes.length, 1);
});

test("appeal confirmation requires a nonempty id and malformed receipts remain explicitly retryable", async () => {
  const h = harness(); await h.open();
  for (const receipt of [undefined, {}, { id: "" }, { id: " " }, { id: 123 }]) {
    h.optionsState.appeal = () => receipt;
    assert.equal(await h.appeal(), false); assert.equal(h.feedback.leave(), false);
    assert.deepEqual(h.appealBodies.at(-1), h.appealBodies[0]);
  }
  h.optionsState.appeal = undefined; assert.equal(await h.appeal({ reason: "不能替换" }), true);
  assert.deepEqual(h.appealBodies.at(-1), h.appealBodies[0]); assert.equal(h.changes.length, 1);
});

test("pending reply or appeal blocks duplicate and competing writes, navigation and refresh", async () => {
  for (const kind of ["reply", "appeal"]) {
    const gate = deferred(), h = harness({ [`${kind}Gate`]: gate }); await h.open();
    const pending = write(h, kind); await settle(); const before = h.calls.length;
    assert.equal(await write(h, kind), false); assert.equal(await write(h, kind === "reply" ? "appeal" : "reply"), false);
    assert.equal(h.feedback.leave(), false); assert.equal(await h.feedback.load(), false); assert.equal(await h.feedback.openSelection({ taskId: "task-history" }), false);
    assert.equal(h.node('[data-feedback-action="refresh"]').disabled, true);
    assert.equal(h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]').disabled, true);
    await h.node('[data-feedback-action="select-task"][data-feedback-task-id="task-history"]').fire("click").finished;
    assert.equal(h.calls.length, before); assert.equal(h.posts().length, 1);
    gate.resolve(confirm(h, kind)); assert.equal(await pending, true); assert.equal(h.posts().length, 1); assert.equal(h.feedback.leave(), true);
  }
});

test("confirmed commands never replay a POST when their follow-up GET or onChanged fails", async () => {
  for (const kind of ["reply", "appeal"]) {
    const h = harness({ changedError: true }); await h.open();
    h.optionsState[kind] = (_, confirmResponse) => {
      const receipt = confirmResponse(); h.optionsState.get = ({ url }, response) => { if (url === "/tasks/task-current") throw new Error("详情同步失败"); return response(); }; return receipt;
    };
    assert.equal(await write(h, kind), true); assert.equal(h.posts().length, 1); assert.match(h.html(), /详情同步失败/);
    assert.equal(h.feedback.leave(), true); await settle(); assert.equal(h.posts().length, 1);
    h.optionsState.get = undefined; assert.equal(await h.feedback.load(), true); assert.equal(h.posts().length, 1);
    if (kind === "appeal") assert.equal(await h.appeal(), false);
  }
});

test("text boundaries reject empty/oversize requests locally without locking a command", async () => {
  const h = harness(); await h.open();
  for (const text of ["", " \n ", "字".repeat(4001)]) assert.equal(await h.feedback.sendReply("thread-current", text), false);
  for (const reason of ["", " \n ", "字".repeat(2001)]) assert.equal(await h.appeal({ reason }), false);
  assert.equal(h.posts().length, 0); assert.equal(h.feedback.leave(), true);
  await h.open(); assert.equal(await h.feedback.sendReply("thread-current", "字".repeat(4000)), true);
  assert.equal(await h.appeal({ reason: "字".repeat(2000) }), true); assert.equal(h.posts().length, 2);
});

test("reply and appeal receipts settle during logout without repaint, GET, toast or onChanged", async () => {
  for (const kind of ["reply", "appeal"]) {
    const gate = deferred(), h = harness({ [`${kind}Gate`]: gate }); await h.open();
    const pending = write(h, kind); await settle(); h.state.loggingOut = true;
    const paints = h.document.paints, calls = h.calls.length;
    assert.equal(h.feedback.leave(), false); assert.equal(await write(h, kind), false); assert.equal(await h.feedback.load(), false);
    gate.resolve(confirm(h, kind)); assert.equal(await pending, true);
    assert.equal(h.document.paints, paints); assert.equal(h.calls.length, calls); assert.deepEqual(h.toasts, []); assert.deepEqual(h.changes, []);
    h.state.loggingOut = false; h.render(); assert.equal(h.feedback.leave(), true);
    if (kind === "appeal") assert.equal(await h.appeal(), false);
  }
});

test("failed logout retains unknown commands and stable explicit retries for both write types", async () => {
  for (const kind of ["reply", "appeal"]) {
    const gate = deferred(), h = harness({ [`${kind}Gate`]: gate }); await h.open();
    const pending = write(h, kind); h.state.loggingOut = true; const paints = h.document.paints;
    gate.reject(new Error("回执仍未知")); assert.equal(await pending, false);
    assert.equal(h.document.paints, paints); assert.deepEqual(h.toasts, []); assert.deepEqual(h.changes, []);
    h.state.loggingOut = false; h.render(); assert.equal(h.feedback.leave(), false);
    h.optionsState[`${kind}Gate`] = null; assert.equal(await write(h, kind, "重试改写被忽略"), true);
    const bodies = kind === "reply" ? h.replyBodies : h.appealBodies;
    assert.deepEqual(bodies[1], bodies[0]); assert.equal(h.posts().length, 2); assert.equal(h.feedback.leave(), true);
  }
});

test("identity, context and reset invalidate pending write completions and old DOM controls", async () => {
  for (const kind of ["reply", "appeal"]) {
    for (const transition of ["identity", "class", "reset"]) {
      const gate = deferred(), h = harness({ [`${kind}Gate`]: gate }); await h.open();
      const staleForm = h.replyForm(), pending = write(h, kind);
      if (transition === "identity") h.api.state.user = { id: "student-2", role: "student", status: "active" };
      if (transition === "class") h.state.classId = "class-2";
      if (transition === "reset") h.feedback.reset();
      h.render();
      const paints = h.document.paints, calls = h.calls.length;
      await staleForm.fire("submit").finished;
      gate.resolve(confirm(h, kind)); assert.equal(await pending, false);
      assert.equal(h.document.paints, paints); assert.equal(h.calls.length, calls); assert.deepEqual(h.changes, []); assert.deepEqual(h.toasts, []);
      assert.doesNotMatch(h.html(), /待确认回复|待确认异议/);
    }
  }
});

test("a pre-reset receipt cannot unlock or erase a new pending command in the same account", async () => {
  const oldGate = deferred(), newGate = deferred(), h = harness({ replyGate: oldGate }); await h.open();
  const old = h.feedback.sendReply("thread-current", "旧请求"), oldBody = plain(h.replyBodies[0]);
  h.feedback.reset(); h.render(); h.optionsState.replyGate = newGate; await h.open();
  const pending = h.feedback.sendReply("thread-current", "新请求"), newBody = plain(h.replyBodies[1]);
  assert.notEqual(oldBody.request_id, newBody.request_id);
  oldGate.resolve(h.confirmReply("thread-current", oldBody)); assert.equal(await old, false);
  assert.equal(h.feedback.leave(), false); assert.equal(h.replyForm().querySelector("textarea").value, "新请求");
  assert.equal(h.replyForm().querySelector("button").disabled, true); assert.equal(h.changes.length, 0);
  newGate.resolve(h.confirmReply("thread-current", newBody)); assert.equal(await pending, true); assert.equal(h.changes.length, 1); assert.equal(h.feedback.leave(), true);
});

test("leaving cancels reads and prevents detached or hidden controls from writing", async () => {
  const h = harness(); await h.open(); const staleForm = h.replyForm(), gate = deferred(), snapshot = plain(h.currentTask);
  h.optionsState.get = ({ url }, response) => url === "/tasks/task-current" ? gate.promise : response();
  const pending = h.feedback.load(); await settle();
  const signal = h.calls.findLast((call) => call.url === "/tasks/task-current").options.signal;
  assert.equal(h.feedback.leave(), true); assert.equal(signal.aborted, true);
  const paints = h.document.paints, calls = h.calls.length;
  await staleForm.fire("submit").finished; assert.equal(await h.feedback.sendReply("thread-current", "离页不能发送"), false); assert.equal(await h.appeal(), false);
  gate.resolve(snapshot); assert.equal(await pending, false); assert.equal(h.document.paints, paints); assert.equal(h.calls.length, calls);
  h.optionsState.get = undefined; assert.equal(await h.open(), true); assert.ok(h.replyForm());
});

test("overlapping selections cannot merge old task reads into a newly selected history", async () => {
  const h = harness(), gate = deferred(), snapshot = plain(h.currentTask);
  h.optionsState.get = ({ url }, response) => url === "/tasks/task-current" ? gate.promise : response();
  const old = h.open(); await settle();
  const oldSignal = h.calls.find((call) => call.url === "/tasks/task-current").options.signal;
  assert.equal(await h.feedback.openSelection({ taskId: "task-history", attemptId: "attempt-history", threadId: "thread-history", questionVersionId: "old:1" }), true);
  assert.equal(oldSignal.aborted, true); const paints = h.document.paints;
  gate.resolve(snapshot); assert.equal(await old, false);
  assert.equal(h.document.paints, paints); assert.match(h.html(), /历史题干/); assert.doesNotMatch(h.html(), /当前题干/);
  assert.equal(h.calls.some((call) => call.url === "/companion/discussion-threads/thread-current"), false, "aborted reads do not start new detail requests");
});

test("reset and changed identity invalidate reads even when the fake server ignores abort", async () => {
  for (const transition of ["identity", "reset"]) {
    const h = harness(), gate = deferred(), snapshot = plain(h.currentTask);
    h.optionsState.get = ({ url }, response) => url === "/tasks/task-current" ? gate.promise : response();
    const old = h.open(); await settle();
    if (transition === "identity") h.api.state.user = { id: "student-2", role: "student", status: "active" }; else h.feedback.reset();
    h.render(); const paints = h.document.paints; gate.resolve(snapshot); assert.equal(await old, false);
    assert.equal(h.document.paints, paints); assert.doesNotMatch(h.html(), /当前题干|服务器允许展示的解析/);
  }
});

test("inactive identity and current release status block writes without trusting cached forms", async () => {
  const h = harness(); await h.open();
  h.currentTask.status = "in_progress"; h.currentTask.answer_released = false; await h.feedback.load();
  assert.equal(await h.appeal(), false); assert.equal(h.document.querySelector('[data-feedback-form="appeal"]'), null);
  for (const transition of [() => { h.state.live = false; }, () => { h.state.role = "teacher"; }, () => { h.api.state.user.status = "disabled"; }]) {
    Object.assign(h.state, { live: true, role: "student" }); h.api.state.user.status = "active"; transition();
    const before = h.calls.length;
    assert.equal(await h.feedback.load(), false); assert.equal(await h.feedback.sendReply("thread-current", "缓存权限无效"), false); assert.equal(await h.appeal(), false);
    assert.equal(h.calls.length, before); assert.doesNotMatch(h.feedback.render(), /data-feedback-form/);
  }
});
