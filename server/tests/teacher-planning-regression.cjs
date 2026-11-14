"use strict";

// Run with: node --test server/tests/teacher-planning-regression.cjs
// Load the entire production module. The DOM below models only its selectors,
// control values, listeners and node replacement; no network or model is used.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-planning.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function decode(value) {
  return String(value).replace(/&(amp|lt|gt|quot|#39);/g, (_, entity) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[entity]);
}

// The parser consumes production HTML, so missing controls or listeners fail
// tests instead of being supplied by a fixture unrelated to the rendered UI.
class TestElement {
  constructor(tagName, ownerDocument, attributes = {}) {
    this.tagName = tagName.toUpperCase();
    this.ownerDocument = ownerDocument;
    this.attributes = { ...attributes };
    this.dataset = Object.fromEntries(Object.entries(attributes).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value]));
    this.parentElement = null;
    this.children = [];
    this.parts = [];
    this.listeners = new Map();
    this.value = attributes.value ?? "";
    this.type = attributes.type || (this.tagName === "INPUT" ? "text" : "");
    this.disabled = "disabled" in attributes;
    this.checked = "checked" in attributes;
    this.hidden = "hidden" in attributes;
    this.open = "open" in attributes;
    this.inert = false;
    this.selectionStart = this.selectionEnd = 0;
    this.classes = new Set((attributes.class || "").split(/\s+/).filter(Boolean));
    this.classList = { add: (...names) => names.forEach((name) => this.classes.add(name)), remove: (...names) => names.forEach((name) => this.classes.delete(name)), contains: (name) => this.classes.has(name) };
  }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  append(child) { child.parentElement = this; this.children.push(child); this.parts.push(child); }
  get textContent() { return this.parts.map((part) => typeof part === "string" ? part : part.textContent).join(""); }
  set textContent(value) { this.clear(); this.parts.push(String(value)); }
  clear() {
    if (this.children.some((child) => child.contains(this.ownerDocument.activeElement))) this.ownerDocument.activeElement = this.ownerDocument.body;
    this.children.forEach((child) => { child.parentElement = null; });
    this.children = []; this.parts = [];
  }
  set innerHTML(html) {
    this.clear();
    const stack = [this];
    const voidTags = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
    for (const token of String(html).match(/<!--[\s\S]*?-->|<\/?[a-z][^>]*>|[^<]+/gi) || []) {
      if (token.startsWith("<!--")) continue;
      if (token.startsWith("</")) {
        const tag = token.match(/^<\/([^\s>]+)/)[1].toUpperCase();
        for (let index = stack.length - 1; index > 0; index -= 1) if (stack[index].tagName === tag) { stack.length = index; break; }
      } else if (token.startsWith("<")) {
        const [, tag, raw] = token.match(/^<([^\s/>]+)([^]*)>$/);
        const attributes = {};
        for (const [, name, quoted, single, bare] of raw.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attributes[name] = decode(quoted ?? single ?? bare ?? "");
        const child = new TestElement(tag, this.ownerDocument, attributes);
        stack.at(-1).append(child);
        if (!voidTags.has(child.tagName) && !token.endsWith("/>")) stack.push(child);
      } else stack.at(-1).parts.push(decode(token));
    }
    for (const input of this.querySelectorAll("textarea")) input.value = input.textContent;
    for (const select of this.querySelectorAll("select")) {
      const options = select.querySelectorAll("option");
      const selected = options.find((option) => "selected" in option.attributes) || options[0];
      select.value = selected?.attributes.value ?? selected?.textContent ?? "";
    }
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  matches(selector) {
    if (selector.endsWith(":not(:disabled)")) return !this.effectivelyDisabled() && this.matches(selector.slice(0, -":not(:disabled)".length));
    if (selector.startsWith("#")) return this.attributes.id === selector.slice(1);
    if (selector.startsWith(".")) return this.classes.has(selector.slice(1));
    if (selector.startsWith("[")) {
      const match = selector.match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
      return Boolean(match && match[1] in this.attributes && (match[2] === undefined || this.attributes[match[1]] === match[2]));
    }
    return this.tagName === selector.toUpperCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(/,\s*/);
    const result = [];
    const visit = (parent) => parent.children.forEach((child) => { if (selectors.some((item) => child.matches(item))) result.push(child); visit(child); });
    visit(this);
    return result;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  effectivelyDisabled() {
    for (let node = this; node; node = node.parentElement) if (node.disabled && (node === this || node.tagName === "FIELDSET")) return true;
    return false;
  }
  addEventListener(type, listener) { const listeners = this.listeners.get(type) || []; listeners.push(listener); this.listeners.set(type, listeners); }
  focus() { if (this.isConnected && !this.effectivelyDisabled()) this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end = start) { this.selectionStart = start; this.selectionEnd = end; }
  getClientRects() { return this.isConnected && !this.hidden ? [{}] : []; }
  fire(type, extras = {}) {
    assert.ok(this.isConnected, `${this.tagName} event target must still belong to the rendered DOM`);
    assert.ok(!this.effectivelyDisabled(), `${this.tagName} ${type} event must not bypass disabled controls`);
    const listeners = [...(this.listeners.get(type) || [])];
    assert.ok(listeners.length, `production must bind ${type} on ${JSON.stringify(this.attributes)}`);
    const event = { type, target: this, currentTarget: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...extras };
    listeners.forEach((listener) => listener(event));
    return event;
  }
}

function testDocument() {
  const document = {};
  document.body = new TestElement("body", document);
  document.activeElement = document.body;
  document.querySelector = (selector) => document.body.querySelector(selector);
  document.querySelectorAll = (selector) => document.body.querySelectorAll(selector);
  return document;
}

function validConfig(overrides = {}) {
  return {
    course_id: "course-1", class_id: "class-1", title: "独立完成目标", description: "保留自己的推导",
    student_ids: ["zero", "pending"], question_count: 3,
    question_types: { single_choice: 1, fill: 1, judge: 1, text: 0 }, common_level: "balanced",
    difficulty: "any", knowledge_points: [], deadline_at: null, scheduled_at: null,
    allow_late: false, allow_retries: true, allow_retry_after_deadline: false,
    ...overrides,
  };
}

function previewFor(config, overrides = {}) {
  return {
    ready: true, preview_token: "preview-token", errors: [], deficits: [], warnings: [],
    question_count: config.question_count, common_count: config.student_ids.length === 1 ? config.question_count : Math.ceil(config.question_count / 2),
    actual_common_ratio: config.student_ids.length === 1 ? 100 : 66.6667, total_points: config.question_count,
    students: config.student_ids.map((id) => ({ student_id: id, display_name: id, question_count: config.question_count, total_score: config.question_count, questions: [{ type: "judge", stem: `真实题干-${id}`, points: 1 }] })),
    ...overrides,
  };
}

function harness(options = {}) {
  const document = testDocument();
  const calls = [], messages = [], libraries = [], published = [];
  let serial = 0, renders = 0, planner;
  const state = {
    live: true, role: "teacher", drawer: false, transitionId: 1, teacherContextVersion: 1,
    courseId: "course-1", classId: "class-1", courseName: "课程一", className: "一班",
    liveGoals: [{ title: "独立完成目标" }],
    liveStudents: [
      { id: "zero", name: "零分学生", mastery: 0, active: true },
      { id: "pending", name: "待评学生", mastery: null, active: true },
      { id: "high", name: "已评学生", mastery: 85, active: true },
      { id: "inactive", name: "离班学生", mastery: 0, active: false },
    ],
    ...options.state,
  };
  const context = vm.createContext({ document, crypto: { randomUUID: () => `test-command-${++serial}` } });
  vm.runInContext(source.replace(/^export /gm, "") + "\nglobalThis.production = { commonQuestionCount, goalAdvice, validateGoalConfig, createTeacherPlanning };", context, { filename: sourcePath });
  const fallback = (method, url, body) => {
    if (method === "GET" && url.endsWith("/knowledge-points")) return { items: [{ id: "kp1", title: "已确认知识点", status: "confirmed" }, { id: "kp-hidden", title: "待确认知识点", status: "draft" }] };
    if (method === "GET" && url.startsWith("/teaching-plans/drafts?")) return { items: options.savedDrafts || [] };
    if (method === "POST" && url === "/teaching-plans/preview") return previewFor(body);
    if (method === "POST" && url === "/teaching-plans/drafts") return { id: "saved-plan", config: plain(body.config) };
    if (method === "POST" && url === "/teaching-plans/prepare") return { status: "ready", preview: previewFor(body.config) };
    if (method === "POST" && url === "/teaching-plans/publish") return { status: "published", task_ids: body.config.student_ids.map((id) => `task-${id}`) };
    throw new Error(`unexpected API request ${method} ${url}`);
  };
  const api = { call: async (method, url, body) => {
    calls.push({ method, url, body: body === undefined ? undefined : plain(body) });
    return options.call ? options.call(method, url, body, fallback) : fallback(method, url, body);
  } };
  const render = () => {
    renders += 1;
    document.body.innerHTML = `<main class="app"><button data-action="open-drawer">学习目标</button></main>${state.drawer ? planner.render() : ""}`;
    planner.bind(document.body);
  };
  planner = context.production.createTeacherPlanning({ api, state, render, toast: (message) => messages.push(message), onLibrary: (value) => libraries.push(plain(value)), onPublished: async (value) => { published.push(plain(value)); if (options.onPublished) await options.onPublished(value); } });
  render();
  const node = (selector) => { const found = document.querySelector(selector); assert.ok(found, `production must render ${selector}`); return found; };
  const field = (name, value) => {
    const input = node(`[data-goal-field="${name}"]`);
    if (input.type === "checkbox") input.checked = value; else input.value = String(value);
    input.fire(input.type === "checkbox" || input.tagName === "SELECT" ? "change" : "input");
    return input;
  };
  return {
    planner, state, document, calls, messages, libraries, published, exports: context.production,
    render, node, field, html: () => planner.render(), renders: () => renders,
    posts: (endpoint) => calls.filter((call) => call.method === "POST" && (!endpoint || call.url === `/teaching-plans/${endpoint}`)),
    click: (action) => node(`[data-goal-action="${action}"]`).fire("click"),
    submit: () => node("[data-goal-form]").fire("submit"),
    member: (id, checked) => { const input = node(`[data-goal-member="${id}"]`); input.checked = checked; input.fire("change"); },
    typeCount: (type, value) => { const input = node(`[data-goal-type="${type}"]`); input.value = String(value); input.fire("input"); },
    level: (level) => { const input = node(`[data-goal-level="${level}"]`); input.checked = true; input.fire("change"); },
    knowledge: (id, checked) => { const input = node(`[data-goal-knowledge="${id}"]`); input.checked = checked; input.fire("change"); },
  };
}

test("common question levels round upward and a single selected student always receives 100 percent", () => {
  const { commonQuestionCount } = harness().exports;
  for (const [count, expected] of [[1, [1, 1, 1]], [3, [1, 2, 3]], [5, [2, 3, 4]], [7, [2, 4, 6]]]) {
    ["light", "balanced", "heavy"].forEach((level, index) => {
      assert.equal(commonQuestionCount(count, level, 2), expected[index], `${count}/${level}`);
      assert.equal(commonQuestionCount(count, level, 1), count, `single/${count}/${level}`);
    });
  }
  assert.equal(commonQuestionCount(3, "light", 0), 0);
  assert.equal(commonQuestionCount(0, "heavy", 2), 0);
  assert.equal(commonQuestionCount(1.5, "balanced", 2), 0);
});

test("configuration validates nonempty scope, integer counts, matching totals and scheduling order", () => {
  const { validateGoalConfig } = harness().exports;
  assert.deepEqual(plain(validateGoalConfig(validConfig())), []);
  const samples = [
    [{ title: " \n " }, /学习目标/], [{ course_id: "" }, /课程/], [{ class_id: "" }, /课程/], [{ student_ids: [] }, /学生/],
    [{ question_count: 0 }, /1–100/], [{ question_count: 101 }, /1–100/], [{ question_count: 1.5 }, /整数/],
    [{ question_types: { single_choice: 1, fill: -1, judge: 3, text: 0 } }, /各题型数量/],
    [{ question_types: { single_choice: 1, fill: 0.5, judge: 1.5, text: 0 } }, /各题型数量/],
    [{ question_types: { single_choice: 1, fill: 1, judge: 0, text: 0 } }, /之和必须等于/],
    [{ scheduled_at: 4000000100, deadline_at: 4000000000 }, /早于截止/], [{ deadline_at: 1 }, /晚于现在/],
  ];
  for (const [changes, message] of samples) assert.match(validateGoalConfig(validConfig(changes)).join(" "), message);
  const boundary = validConfig({ question_count: 100, question_types: { single_choice: 25, fill: 25, judge: 25, text: 25 } });
  assert.deepEqual(plain(validateGoalConfig(boundary)), []);
});

test("advice distinguishes a real zero score from missing evidence and uses only selected students", async () => {
  const h = harness();
  const config = validConfig();
  const advice = h.exports.goalAdvice(config, h.state.liveStudents);
  assert.equal(advice.selectedCount, 2);
  assert.equal(advice.total, 3);
  assert.equal(advice.minutes, 6);
  assert.ok(advice.messages.some((message) => message.includes("零分学生") && message.includes("比例较低")));
  assert.ok(advice.messages.some((message) => message.includes("待评学生") && message.includes("暂无足够评分依据")));
  assert.ok(!advice.messages.some((message) => message.includes("离班学生")));
  const pending = h.exports.goalAdvice(validConfig({ student_ids: ["pending"] }), h.state.liveStudents);
  assert.equal(pending.common, 3);
  assert.ok(!pending.messages.some((message) => message.includes("比例较低")));
  await h.planner.open();
  assert.match(h.node('[data-goal-member="zero"]').parentElement.textContent, /最近已评分记录 0%/);
  assert.match(h.node('[data-goal-member="pending"]').parentElement.textContent, /待评估/);
  assert.doesNotMatch(h.node('[data-goal-member="pending"]').parentElement.textContent, /0%/);
});

test("only checked active students reach preview and publish, with the preview token and matching snapshot", async () => {
  const h = harness(); await h.planner.open();
  assert.equal(h.document.querySelector('[data-goal-member="inactive"]'), null);
  h.member("pending", false);
  h.click("preview"); await settle();
  const expected = h.posts("preview")[0].body;
  assert.deepEqual(expected.student_ids, ["zero", "high"]);
  assert.equal(h.node("[data-goal-publish]").disabled, false);
  h.submit(); await settle();
  const published = h.posts("publish");
  assert.equal(published.length, 1);
  assert.deepEqual(published[0].body.config, expected);
  assert.equal(published[0].body.preview_token, "preview-token");
  assert.ok(published[0].body.request_id);
  assert.equal(h.published.length, 1);
  assert.equal(h.state.drawer, false);
  assert.equal(h.html(), "");
  assert.ok(h.messages.some((message) => message.includes("2 位学生")));
});

test("single-student entry, search, clear and all selection maintain a real recipient set", async () => {
  const h = harness(); await h.planner.open({ studentId: "zero" });
  assert.equal(h.node('[data-goal-member="zero"]').checked, true);
  assert.equal(h.node('[data-goal-member="pending"]').checked, false);
  assert.match(h.node("[data-goal-common]").textContent, /一位学生.*全部题目/);
  const search = h.node("[data-goal-search]"); search.value = "待评"; search.fire("input");
  assert.equal(h.node('[data-goal-member="zero"]').parentElement.hidden, true);
  h.click("preview"); await settle();
  assert.deepEqual(h.posts("preview")[0].body.student_ids, ["zero"], "filtering names does not alter selection");
  assert.match(h.node("[data-goal-readiness]").textContent, /100%/);
  h.click("none"); h.click("preview"); await settle();
  assert.equal(h.posts("preview").length, 1);
  assert.match(h.node("[data-goal-readiness]").textContent, /至少勾选/);
  h.click("all"); h.click("preview"); await settle();
  assert.deepEqual(h.posts("preview")[1].body.student_ids, ["zero", "pending", "high"]);
});

test("invalid per-person totals block requests and balancing restores a valid four-type configuration", async () => {
  const h = harness(); await h.planner.open();
  h.field("question_count", 5);
  h.click("preview"); await settle();
  assert.equal(h.posts().length, 0);
  assert.match(h.node("[data-goal-readiness]").textContent, /之和必须等于/);
  h.click("balance-types");
  assert.deepEqual(["single_choice", "fill", "judge", "text"].map((type) => Number(h.node(`[data-goal-type="${type}"]`).value)), [2, 1, 1, 1]);
  h.click("preview"); await settle();
  assert.equal(h.posts("preview")[0].body.question_count, 5);
  h.typeCount("fill", 1.5); h.click("preview"); await settle();
  assert.equal(h.posts("preview").length, 1);
  assert.match(h.node("[data-goal-readiness]").textContent, /各题型数量/);
});

test("save and preview failures keep the full draft through host DOM rebuilds and retry safely", async () => {
  for (const action of ["save", "preview"]) {
    let rejected = true;
    const h = harness({ call: (method, url, body, fallback) => {
      if (method === "POST" && rejected) throw new Error("临时断网，未完成");
      return fallback(method, url, body);
    } });
    await h.planner.open();
    h.field("title", "保留 <目标>"); h.field("description", "第一行\n第二行 & 公式");
    h.member("pending", false); h.knowledge("kp1", true); h.level("heavy");
    h.click(action); await settle();
    assert.match(h.node("[data-goal-readiness]").textContent, /临时断网/);
    assert.equal(h.node('[data-goal-field="title"]').value, "保留 <目标>");
    assert.equal(h.node('[data-goal-field="description"]').value, "第一行\n第二行 & 公式");
    assert.equal(h.node('[data-goal-member="pending"]').checked, false);
    assert.equal(h.node('[data-goal-knowledge="kp1"]').checked, true);
    h.render();
    assert.equal(h.node('[data-goal-field="title"]').value, "保留 <目标>");
    rejected = false; h.click(action); await settle();
    const writes = h.posts(action === "save" ? "drafts" : "preview");
    assert.equal(writes.length, 2);
    assert.deepEqual(writes[0].body, writes[1].body, "retry must keep the draft and write idempotency key unchanged");
    assert.equal(h.posts("publish").length, 0);
  }
});

test("every configuration edit invalidates an earlier preview and blocks submit until checked again", async () => {
  const mutations = [
    (h) => h.field("title", "修改目标"), (h) => h.field("description", "补充要求"),
    (h) => h.field("difficulty", "easy"), (h) => h.field("deadline_at", ""),
    (h) => h.field("scheduled_at", "2099-01-02T12:00"), (h) => h.field("allow_late", true),
    (h) => h.field("allow_retries", false), (h) => h.field("allow_retry_after_deadline", true),
    (h) => h.member("pending", false), (h) => h.level("light"), (h) => h.knowledge("kp1", true),
    (h) => { h.typeCount("single_choice", 0); h.typeCount("fill", 2); },
    (h) => { h.field("question_count", 4); h.typeCount("text", 1); },
  ];
  for (const mutate of mutations) {
    const h = harness(); await h.planner.open();
    h.click("preview"); await settle();
    assert.equal(h.node("[data-goal-publish]").disabled, false);
    mutate(h);
    assert.equal(h.node("[data-goal-publish]").disabled, true);
    assert.doesNotMatch(h.node("[data-goal-readiness]").textContent, /备题已齐/);
    h.submit(); await settle();
    assert.equal(h.posts("publish").length, 0);
  }
});

test("prepare saves and opens the library without publishing, even when its response is ready", async () => {
  for (const ready of [false, true]) {
    const h = harness({ call: (method, url, body, fallback) => {
      if (url === "/teaching-plans/preview") return previewFor(body, { ready: false, preview_token: null, deficits: [{ question_type: "fill", required: 2, available: 0 }] });
      if (url === "/teaching-plans/prepare") return { status: ready ? "ready" : "needs_preparation", preview: previewFor(body.config, { ready, deficits: ready ? [] : [{ question_type: "fill", required: 2, available: 0 }] }) };
      return fallback(method, url, body);
    } });
    await h.planner.open(); h.click("preview"); await settle();
    h.click("prepare"); await settle();
    assert.equal(h.posts("prepare").length, 1);
    assert.ok(h.posts("prepare")[0].body.request_id);
    assert.equal(h.posts("publish").length, 0);
    assert.equal(h.published.length, 0);
    assert.equal(h.libraries.length, 1);
    assert.deepEqual(h.libraries[0].config, h.posts("prepare")[0].body.config);
    assert.equal(h.state.drawer, false);
    assert.ok(h.messages.some((message) => message.includes(ready ? "检查后发布" : "补齐题目")));
  }
});

test("missing readiness or preview token blocks publication at both DOM and submit handler", async () => {
  for (const preview of [null, { ready: false, preview_token: "unexpected-token" }, { ready: true, preview_token: null }, { ready: true, preview_token: "" }]) {
    const h = harness({ call: (method, url, body, fallback) => url === "/teaching-plans/preview" ? previewFor(body, preview) : fallback(method, url, body) });
    await h.planner.open();
    if (preview) { h.click("preview"); await settle(); }
    assert.equal(h.node("[data-goal-publish]").disabled, true);
    const event = h.submit(); await settle();
    assert.equal(event.defaultPrevented, true);
    assert.equal(h.posts("publish").length, 0);
    assert.match(h.node("[data-goal-readiness]").textContent, /请先检查备题/);
  }
});

test("stale or expired publish errors keep input but clear the token and require a fresh preview", async () => {
  for (const code of ["preview_stale", "preview_expired", "config_changed"]) {
    const h = harness({ call: (method, url, body, fallback) => {
      if (url === "/teaching-plans/publish") throw Object.assign(new Error("题卷已变化，请重新检查"), { code });
      return fallback(method, url, body);
    } });
    await h.planner.open(); h.field("description", "不要丢失的说明");
    h.click("preview"); await settle(); h.submit(); await settle();
    assert.equal(h.posts("publish").length, 1);
    assert.equal(h.node('[data-goal-field="description"]').value, "不要丢失的说明");
    assert.equal(h.node("[data-goal-publish]").disabled, true);
    h.submit(); await settle();
    assert.equal(h.posts("publish").length, 1);
    assert.equal(h.published.length, 0);
  }
});

test("double form submission cannot duplicate an in-flight publish", async () => {
  const gate = deferred();
  const h = harness({ call: (method, url, body, fallback) => url === "/teaching-plans/publish" ? gate.promise : fallback(method, url, body) });
  await h.planner.open(); h.click("preview"); await settle();
  h.submit(); h.submit();
  assert.equal(h.posts("publish").length, 1);
  assert.equal(h.node("[data-goal-publish]").disabled, true);
  gate.resolve({ status: "published" }); await settle();
  assert.equal(h.published.length, 1);
});

test("delayed old course knowledge and saved drafts cannot replace the newly opened context", async () => {
  const knowledge = deferred(), drafts = deferred();
  const h = harness({ call: (method, url, body, fallback) => {
    if (url === "/courses/course-1/knowledge-points") return knowledge.promise;
    if (url === "/teaching-plans/drafts?class_id=class-1") return drafts.promise;
    if (url === "/courses/course-2/knowledge-points") return { items: [{ id: "new-kp", title: "新课程知识点", status: "confirmed" }] };
    return fallback(method, url, body);
  } });
  const first = h.planner.open();
  h.field("title", "旧课程私有草稿");
  h.state.courseId = "course-2"; h.state.classId = "class-2"; h.state.courseName = "课程二";
  await h.planner.open({ fresh: true, title: "新课程目标" });
  const currentInput = h.node('[data-goal-field="title"]'); currentInput.focus();
  knowledge.resolve({ items: [{ id: "old-kp", title: "旧知识点不应显示", status: "confirmed" }] });
  drafts.resolve({ items: [{ id: "old-draft", config: validConfig({ title: "旧草稿不应显示" }) }] });
  await first;
  assert.equal(h.node('[data-goal-field="title"]'), currentInput);
  assert.equal(h.document.activeElement, currentInput);
  assert.match(h.html(), /新课程目标|新课程知识点/);
  assert.doesNotMatch(h.html(), /旧课程私有草稿|旧知识点不应显示|旧草稿不应显示/);
});

test("late preview, save, prepare and publish completions cannot change a different course or session", async () => {
  for (const action of ["preview", "save", "prepare", "publish"]) {
    const gate = deferred();
    let hold = false;
    const endpoint = action === "save" ? "drafts" : action;
    const h = harness({ call: (method, url, body, fallback) => {
      if (method === "POST" && hold && url === `/teaching-plans/${endpoint}`) return gate.promise;
      if (action === "prepare" && url === "/teaching-plans/preview") return previewFor(body, { ready: false, preview_token: null, deficits: [{ question_type: "fill", required: 1, available: 0 }] });
      return fallback(method, url, body);
    } });
    await h.planner.open();
    if (action === "publish" || action === "prepare") { h.click("preview"); await settle(); }
    hold = true;
    if (action === "publish") h.submit(); else h.click(action);
    assert.equal(h.posts(endpoint).length, 1, action);
    h.state.transitionId += 1;
    h.state.courseId = "next-course"; h.state.classId = "next-class";
    h.planner.reset(); await h.planner.open({ fresh: true, title: "新上下文目标" });
    const before = h.renders();
    gate.resolve(action === "preview" ? previewFor(validConfig(), { warnings: ["旧预览不应显示"] }) : action === "save" ? { id: "old-save", config: validConfig({ title: "旧保存不应显示" }) } : action === "prepare" ? { status: "ready", preview: previewFor(validConfig()) } : { status: "published" });
    await settle();
    assert.equal(h.node('[data-goal-field="title"]').value, "新上下文目标", action);
    assert.equal(h.state.drawer, true, action);
    assert.equal(h.renders(), before, action);
    assert.equal(h.libraries.length, 0, action);
    assert.equal(h.published.length, 0, action);
    assert.doesNotMatch(h.html(), /旧预览不应显示|旧保存不应显示/);
  }
});

test("late context refresh updates only its own region and preserves typed value, selection and focus", async () => {
  const knowledge = deferred(), drafts = deferred();
  const h = harness({ call: (method, url, body, fallback) => url.endsWith("/knowledge-points") ? knowledge.promise : method === "GET" && url.includes("/drafts?") ? drafts.promise : fallback(method, url, body) });
  const opening = h.planner.open();
  const title = h.field("title", "正在输入的目标 <x>");
  title.focus(); title.setSelectionRange(3, 6);
  const description = h.field("description", "正在输入\n保留换行");
  const before = h.renders();
  knowledge.resolve({ items: [{ id: "kp1", title: "异步知识点", status: "confirmed" }] });
  drafts.resolve({ items: [{ id: "stored", config: validConfig({ title: "可以恢复的草稿" }) }] });
  await opening;
  assert.equal(h.renders(), before, "context completion must not rebuild the host page");
  assert.equal(h.node('[data-goal-field="title"]'), title);
  assert.equal(h.node('[data-goal-field="description"]'), description);
  assert.equal(h.document.activeElement, title);
  assert.equal(title.value, "正在输入的目标 <x>");
  assert.deepEqual([title.selectionStart, title.selectionEnd], [3, 6]);
  h.member("pending", false); h.typeCount("text", 1); h.field("question_count", 4);
  assert.equal(h.node('[data-goal-field="title"]'), title, "summary edits must not replace input nodes");
  assert.equal(h.document.activeElement, title);
  h.knowledge("kp1", true);
  h.click("save"); await settle();
  const config = h.posts("drafts")[0].body.config;
  assert.equal(config.title, "正在输入的目标 <x>");
  assert.equal(config.description, "正在输入\n保留换行");
  assert.deepEqual(config.knowledge_points, ["kp1"]);
  assert.equal(h.node('[data-goal-field="title"]').value, config.title, "busy and completion renders preserve values");
});

test("restoring a saved draft excludes removed students and invalidates the previous preview", async () => {
  const h = harness({ savedDrafts: [{ id: "stored", config: validConfig({ title: "已保存 <目标>", student_ids: ["zero", "inactive", "removed"], description: "原始说明" }) }] });
  await h.planner.open(); h.click("preview"); await settle();
  const restore = h.node("[data-goal-restore]"); restore.value = "stored"; restore.fire("change");
  assert.equal(h.node('[data-goal-field="title"]').value, "已保存 <目标>");
  assert.equal(h.node('[data-goal-field="description"]').value, "原始说明");
  assert.equal(h.node("[data-goal-publish]").disabled, true);
  h.click("preview"); await settle();
  assert.deepEqual(h.posts("preview").at(-1).body.student_ids, ["zero"]);
  assert.equal(h.node('[data-goal-member="pending"]').checked, false);
});

test("reopening a fresh draft during a request does not leave its controls permanently busy", async () => {
  const gate = deferred();
  let hold = true;
  const h = harness({ call: (method, url, body, fallback) => hold && url === "/teaching-plans/preview" ? gate.promise : fallback(method, url, body) });
  await h.planner.open(); h.click("preview");
  h.planner.close();
  await h.planner.open({ fresh: true, title: "新建目标" });
  gate.resolve(previewFor(validConfig())); await settle(); hold = false;
  assert.equal(h.node('[data-goal-action="preview"]').disabled, false, "obsolete request must release busy state for the new draft");
  h.click("preview"); await settle();
  assert.equal(h.posts("preview").length, 2);
  assert.equal(h.posts("preview")[1].body.title, "新建目标");
});
