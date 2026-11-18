"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const { File } = require("node:buffer");
const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-library.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };

function fixtureQuestion(id = "q1", overrides = {}) {
  return { question_id: id, version: 1, status: "approved", content: { type: "single_choice", stem: `题目 ${id}`, options: ["零", "一"], answer: 0, points: 1, explanation: "解释", knowledge_points: ["kp1"], difficulty: "easy", source_anchors: [{ file_id: "m1", block_id: "b1", anchor: "p:1" }], ...overrides }, usage: { task_count: 0, student_count: 0, average_score: null, average_score_rate: null } };
}

// Minimal DOM fixture for observable focus/inert behavior; it parses the production markup on each repaint.
function domSurface(events) {
  const doc = { activeElement: null, hidden: false };
  const style = () => { const values = new Map(); return {
    getPropertyValue: (key) => values.get(key)?.[0] || "",
    getPropertyPriority: (key) => values.get(key)?.[1] || "",
    setProperty: (key, value, priority = "") => values.set(key, [value, priority]),
    removeProperty: (key) => values.delete(key),
  }; };
  class Element {
    constructor(tag, attrs = {}) { this.tagName = tag.toUpperCase(); this.attrs = attrs; this.children = []; this.parentElement = null; this.ownerDocument = doc; this.style = style(); this.value = attrs.value || ""; this.checked = "checked" in attrs; this.scrollTop = 0; this.dataset = Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])); }
    get className() { return this.attrs.class || ""; }
    get type() { return this.attrs.type || (this.tagName === "TEXTAREA" ? "textarea" : "text"); }
    get disabled() { return "disabled" in this.attrs; }
    get inert() { return "inert" in this.attrs; }
    set inert(value) { if (value) this.attrs.inert = ""; else delete this.attrs.inert; }
    get isConnected() { return this === doc.documentElement || Boolean(this.parentElement?.isConnected); }
    getAttribute(key) { return this.attrs[key] ?? null; }
    contains(node) { for (let item = node; item; item = item.parentElement) if (item === this) return true; return false; }
    matches(selector) {
      return selector.split(",").some((part) => {
        const value = part.trim();
        if (value === ":disabled") return this.disabled || Boolean(this.closest("fieldset[disabled]"));
        if (value.includes(" ")) { const at = value.lastIndexOf(" "); return this.matches(value.slice(at + 1)) && Boolean(this.parentElement?.closest(value.slice(0, at))); }
        if (value.startsWith(".")) return this.className.split(/\s+/).includes(value.slice(1));
        const found = value.match(/^(\w+)?(?:\[([^=\]]+)(?:=["']?([^"'\]]+)["']?)?\])?$/);
        return Boolean(found && (!found[1] || this.tagName === found[1].toUpperCase()) && (!found[2] || (found[2] in this.attrs && (found[3] == null || this.attrs[found[2]] === found[3]))));
      });
    }
    closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
    querySelectorAll(selector) { return this.children.flatMap((child) => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    getClientRects() { return this.isConnected ? [{}] : []; }
    focus() { if (!this.isConnected || this.matches(":disabled") || this.closest("[inert]")) return; doc.activeElement = this; if (surface?.contains(this)) events.focusin?.({ target: this }); }
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    addEventListener(name, fn) { if (this === surface) events[name] = fn; }
    append(child) { child.parentElement = this; this.children.push(child); return child; }
    set innerHTML(html) {
      this.markup = html;
      if (this.contains(doc.activeElement)) doc.activeElement = doc.body;
      for (const child of this.children) child.parentElement = null;
      this.children = [];
      const stack = [this], voids = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
      for (const token of html.matchAll(/<\/?([a-z][\w-]*)\b([^>]*)>/gi)) {
        const tag = token[1].toUpperCase();
        if (token[0].startsWith("</")) { if (!voids.has(tag) && stack.length > 1) stack.pop(); continue; }
        const attrs = {};
        for (const match of token[2].matchAll(/([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[match[1]] = match[2] ?? match[3] ?? match[4] ?? "";
        const child = stack.at(-1).append(new Element(tag, attrs));
        if (!voids.has(tag)) stack.push(child);
      }
    }
    get innerHTML() { return this.markup || ""; }
  }
  let surface;
  doc.documentElement = new Element("html"); doc.body = doc.documentElement.append(new Element("body"));
  const host = doc.body.append(new Element("div", { class: "teacher-app" }));
  const sidebar = host.append(new Element("aside"));
  const preexisting = sidebar.append(new Element("button", { inert: "" }));
  const main = host.append(new Element("main"));
  const context = main.append(new Element("div", { class: "teacher-context" }));
  surface = main.append(new Element("section", { class: "teacher-library", "data-library-root": "" }));
  const unrelated = doc.body.append(new Element("div", { class: "other-workspace" }));
  doc.activeElement = doc.body;
  doc.documentElement.style.setProperty("overflow", "auto", "important");
  doc.body.style.setProperty("overflow", "clip");
  return { root: surface, doc, host, sidebar, preexisting, context, unrelated };
}

function harness(options = {}) {
  const calls = [], toasts = [], events = {}, timers = new Map();
  let timerSerial = 0, appRenders = 0, commandSerial = 0;
  const state = { live: true, role: "teacher", courseId: "c1", classId: "class1", courseName: "课程一", className: "一班", teacherView: "materials", transitionId: 1, teacherContextVersion: 1, liveStudents: [], ...options.state };
  const material = { id: "m1", title: "真实材料", revision: 1, status: "confirmed", rights_confirmed: true, allow_generation: true, external_processing_allowed: true, license_code: "own", class_id: null, ...options.material };
  const doc = { material, blocks: [{ id: "b1", file_id: "m1", anchor: "p:1", text: "真实来源原文", status: "confirmed", kind: "text" }], issues: [] };
  const fallback = async (method, url, body) => {
    if (url.includes("knowledge-points")) return { items: [{ id: "kp1", title: "已确认知识点", status: "confirmed" }, { id: "kp-draft", title: "未确认知识点", status: "draft" }] };
    if (url.includes("/question-library")) return { items: options.questions || [], total: (options.questions || []).length, facets: { knowledge_points: [{ value: "kp1", count: 1 }] } };
    if (url.includes("/materials?")) return { items: [material] };
    if (url === "/materials/m1") return doc;
    if (url.includes("/question-jobs") && method === "GET") return { items: options.jobs || [], total: (options.jobs || []).length };
    if (method === "POST") return { job: { id: "job-new", status: "pending", stage: "review", queue_status: "not_dispatched", input: body } };
    return {};
  };
  const api = {
    state: { user: { id: "teacher-1" } },
    call: async (method, url, body) => { calls.push([method, url, body]); return options.call ? options.call(method, url, body, fallback) : fallback(method, url, body); },
    upload: async (...args) => { calls.push(["UPLOAD", ...args]); return options.upload ? options.upload(...args) : {}; },
  };
  const dom = options.dom ? domSurface(events) : null;
  const root = dom?.root || {
    isConnected: true, innerHTML: "", ownerDocument: { activeElement: null, hidden: false },
    matches: (selector) => selector === "[data-library-root]", contains: () => true,
    querySelector: () => null, querySelectorAll: () => [],
    addEventListener: (name, handler) => { events[name] = handler; },
  };
  const context = vm.createContext({ URLSearchParams, FormData, File, console, crypto: { randomUUID: () => `test-command-${++commandSerial}` }, setTimeout: (fn) => { timers.set(++timerSerial, fn); return timerSerial; }, clearTimeout: (id) => timers.delete(id) });
  vm.runInContext(source.replace("export function createTeacherLibrary", "function createTeacherLibrary") + "\nglobalThis.makeLibrary = createTeacherLibrary;", context, { filename: sourcePath });
  const library = context.makeLibrary({ api, state, render: () => { appRenders += 1; }, toast: (message) => toasts.push(message), onPrepareGoal: options.onPrepareGoal || (() => {}) });
  root.innerHTML = dom ? library.render().replace(/^<section[^>]+>/, "").replace(/<\/section>$/, "") : library.render(); library.bind(root);
  const node = (dataset = {}, extras = {}) => ({ dataset, ...extras, closest() { return this; } });
  return {
    state, api, root, library, calls, toasts, timers, doc, dom,
    keydown: (key, shiftKey = false) => { const event = { key, shiftKey, prevented: false, preventDefault() { this.prevented = true; } }; events.keydown(event); return event; },
    hostRender: () => { root.innerHTML = library.render().replace(/^<section[^>]+>/, "").replace(/<\/section>$/, ""); library.bind(root); },
    html: () => library.render(), appRenders: () => appRenders,
    click: (action, value) => {
      const target = dom ? root.querySelectorAll("[data-library-action]").find((item) => item.dataset.libraryAction === action && (value == null || item.dataset.libraryValue === value)) : node({ libraryAction: action, libraryValue: value });
      assert.ok(target, `action ${action} exists`); target.focus?.(); return events.click({ target });
    },
    field: (key, value, extra = {}, type = "change") => {
      const scope = extra.scope || "modal";
      if (dom) {
        const target = root.querySelectorAll("[data-library-field]").find((item) => item.dataset.libraryField === key && (extra.item == null || item.dataset.libraryValue === extra.item));
        assert.ok(target, `field ${key} exists`); target.value = value; if (extra.checked != null) target.checked = extra.checked; if (extra.files) target.files = extra.files; target.focus();
        return events[type]({ type, target });
      }
      return events[type]({ type, target: node({ libraryField: key, libraryScope: scope, libraryValue: extra.item }, { value, type: extra.type || "text", checked: extra.checked, files: extra.files, ...extra }) });
    },
    submit: (kind, value) => events.submit({ preventDefault() {}, target: node({ libraryForm: kind, libraryValue: value }) }),
  };
}

async function fillQuestion(h, type = "single_choice", answer = "A") {
  await h.click("manual");
  h.field("type", type);
  h.field("stem", "手动录入的真实题干");
  h.field("options", "第一选项\n第二选项");
  h.field("answer", answer);
  h.field("knowledge_points", "", { type: "checkbox", item: "kp1", checked: true });
  await h.field("source_material_id", "m1");
  h.field("source_block_ids", "", { type: "checkbox", item: "b1", checked: true });
}

test("offline library makes no API calls and has no fabricated records or host action attributes", async () => {
  const h = harness({ state: { live: false } });
  await h.library.load();
  assert.equal(h.calls.length, 0);
  assert.match(h.html(), /正式服务未连接/);
  assert.doesNotMatch(source, /\bdata-(?:action|view)=/);
});

test("context switch discards delayed old course responses and clears draft and selection", async () => {
  const old = deferred();
  const h = harness({ call: (method, url, body, fallback) => url.includes("/c1/question-library") ? old.promise : url.includes("/c2/question-library") ? { items: [fixtureQuestion("new-course")], total: 1 } : fallback(method, url, body) });
  const pending = h.library.load();
  h.state.courseId = "c2"; h.state.classId = "class2"; h.state.teacherContextVersion += 1;
  await h.library.load();
  old.resolve({ items: [fixtureQuestion("secret-old-course")], total: 1 });
  await pending;
  assert.match(h.html(), /new-course/);
  assert.doesNotMatch(h.html(), /secret-old-course/);
  await h.click("manual"); h.field("stem", "私有草稿");
  h.api.state.user = { id: "teacher-2" };
  assert.doesNotMatch(h.html(), /私有草稿|library-dialog-title/);
});

test("latest filter response wins and the server receives type, pagination, and current class", async () => {
  const delayed = deferred(); let reads = 0;
  const h = harness({ call: (method, url, body, fallback) => {
    if (url.includes("question-library")) { reads += 1; if (reads === 2) return delayed.promise; if (reads === 3) return { items: [fixtureQuestion("latest")], total: 1 }; }
    return fallback(method, url, body);
  } });
  await h.library.load();
  h.field("question_type", "judge", { scope: "filters" }); h.field("q", "第一轮", { scope: "filters" });
  const first = h.submit("filters");
  h.field("q", "第二轮", { scope: "filters" }); await h.submit("filters");
  delayed.resolve({ items: [fixtureQuestion("stale")], total: 1 }); await first;
  const queries = h.calls.filter(([, url]) => url.includes("question-library"));
  const params = new URLSearchParams(queries.at(-1)[1].split("?")[1]);
  assert.equal(params.get("type"), "judge"); assert.equal(params.get("class_id"), "class1"); assert.equal(params.get("offset"), "0");
  assert.match(h.html(), /latest/); assert.doesNotMatch(h.html(), /题目 stale/);
});

test("manual candidate requires explicit external consent, preserves option zero, and sends confirmed knowledge IDs", async () => {
  const h = harness(); await h.library.load(); await fillQuestion(h);
  await h.submit("question");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  assert.match(h.html(), /请明确勾选允许外部模型处理/);
  assert.doesNotMatch(h.html(), /未确认知识点/);
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true }); await h.submit("question");
  const call = h.calls.find(([method, url]) => method === "POST" && url.endsWith("question-candidates"));
  assert.ok(call); assert.equal(call[2].question.answer, 0);
  assert.deepEqual(plain(call[2].question.knowledge_points), ["kp1"]);
  assert.deepEqual(plain(call[2].source_block_ids), ["b1"]); assert.equal(call[2].external_processing_allowed, true);
  assert.equal(call[2].question.status, undefined); assert.equal(h.appRenders(), 0);
});

test("judge false survives input and no candidate request can use an unconfirmed knowledge ID", async () => {
  const h = harness(); await h.library.load(); await fillQuestion(h, "judge", "false");
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true });
  h.field("knowledge_points", "", { type: "checkbox", item: "kp-draft", checked: true });
  await h.submit("question"); assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  h.field("knowledge_points", "", { type: "checkbox", item: "kp-draft", checked: false }); await h.submit("question");
  const call = h.calls.find(([method]) => method === "POST"); assert.equal(call[2].question.answer, false);
});

test("goal deficits prefill at most ten questions but do not authorize or start generation", async () => {
  const h = harness();
  await h.library.load({ config: { title: "补齐目标", difficulty: "hard", knowledge_points: ["kp1"] }, deficits: [{ question_type: "fill", required: 18, available: 4 }] });
  const html = h.html();
  assert.match(html, /补齐目标/); assert.match(html, /本批次 10 题/); assert.match(html, /name="count_fill"[^>]*value="10"/);
  assert.equal(h.calls.filter(([method]) => method !== "GET").length, 0);
  assert.doesNotMatch(html, /name="external_processing_allowed"[^>]*checked/);
  assert.match(html, /data-library-value="kp1"[^>]*checked/);
});

test("import preview stays local, excludes invalid and duplicate rows, and reports confirm failures", async () => {
  const preview = { import_id: "imp1", rows: [
    { row_number: 2, status: "valid", question: { stem: "可送审题" }, errors: [], duplicate: null },
    { row_number: 3, status: "duplicate", question: { stem: "重复题" }, errors: [], duplicate: { scope: "file" } },
    { row_number: 4, status: "invalid", question: null, errors: ["答案格式错误"], duplicate: null },
  ], summary: { total: 3, valid: 1, duplicate: 1, invalid: 1 } };
  const h = harness({ upload: async () => preview, call: (method, url, body, fallback) => url.endsWith("/imp1/confirm") ? { jobs: [], failures: [{ row_number: 2, error: "来源权限已变化" }], accepted_count: 0, failed_count: 1 } : fallback(method, url, body) });
  await h.library.load(); await h.click("import");
  h.field("import_file", "", { type: "file", files: [new File(["[]"], "questions.json")] });
  await h.field("source_material_id", "m1");
  h.field("source_block_ids", "", { type: "checkbox", item: "b1", checked: true }); await h.submit("import-preview");
  const upload = h.calls.find(([method]) => method === "UPLOAD"); assert.equal(upload[2].get("external_processing_allowed"), null);
  assert.deepEqual(JSON.parse(upload[2].get("source_block_ids")), ["b1"]);
  assert.match(h.html(), /重复 1/); assert.match(h.html(), /答案格式错误/);
  await h.submit("import-confirm"); assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true }); await h.submit("import-confirm");
  const call = h.calls.find(([method]) => method === "POST"); assert.deepEqual(plain(call[2].row_numbers), [2]);
  assert.match(h.html(), /已送审 <strong>0/); assert.match(h.html(), /来源权限已变化/);
});

test("bulk operations require a confirmation, use quarantined, retain partial failures, and stop on identity change", async () => {
  const rows = [fixtureQuestion("q1"), fixtureQuestion("q2")];
  const h = harness({ questions: rows, call: (method, url, body, fallback) => { if (method === "POST" && url.includes("/q2/")) throw new Error("当前版本不可操作"); return fallback(method, url, body); } });
  await h.library.load(); h.field("select-page", "", { type: "checkbox", checked: true }); await h.click("bulk-pause");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0); await h.submit("bulk");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  h.field("confirmed", "", { type: "checkbox", checked: true }); await h.submit("bulk");
  const writes = h.calls.filter(([method]) => method === "POST"); assert.equal(writes.length, 2); assert.equal(writes[0][2].status, "quarantined"); assert.ok(writes[0][2].request_id);
  assert.match(h.html(), /部分版本未完成/); assert.match(h.html(), /当前版本不可操作/);
  const gate = deferred(); let count = 0;
  const stale = harness({ questions: rows, call: (method, url, body, fallback) => { if (method === "POST") { count += 1; return gate.promise; } return fallback(method, url, body); } });
  await stale.library.load(); stale.field("select-page", "", { type: "checkbox", checked: true }); await stale.click("bulk-retire"); stale.field("confirmed", "", { type: "checkbox", checked: true });
  const pending = stale.submit("bulk"); stale.state.transitionId += 1; stale.api.state.user.id = "teacher-new"; gate.resolve({}); await pending;
  assert.equal(count, 1, "the second status write must not run with a new user's session");
});

test("detail shows escaped source text and nullable real usage, not fabricated zero averages", async () => {
  const row = fixtureQuestion();
  const h = harness({ questions: [row], call: (method, url, body, fallback) => url.includes("/versions/1") ? { question: row, sources: [{ file_id: "m1", block_id: "b1", anchor: "p:1" }], reviews: { independent_solution: { answer: 0, reasoning: "<script>untrusted</script>", solvable: true } }, usage: { task_count: 0, graded_count: 0, average_score: null, average_score_rate: null }, versions: [{ version: 1, status: "approved" }] } : fallback(method, url, body) });
  await h.library.load(); await h.click("question-detail", "q1@1");
  assert.match(h.html(), /真实来源原文/); assert.match(h.html(), /暂无记录/); assert.match(h.html(), /&lt;script&gt;untrusted&lt;\/script&gt;/); assert.doesNotMatch(h.html(), /<script>/);
});

test("failed candidate preserves draft through host rerenders and displays the actual error", async () => {
  const h = harness({ call: (method, url, body, fallback) => { if (method === "POST") throw new Error("模型未配置"); return fallback(method, url, body); } });
  await h.library.load(); await fillQuestion(h); h.field("external_processing_allowed", "", { type: "checkbox", checked: true }); await h.submit("question");
  assert.match(h.html(), /模型未配置/); assert.match(h.html(), /手动录入的真实题干/); assert.match(h.html(), /data-library-form="question"/);
  assert.equal(h.appRenders(), 0);
});

test("queue action buttons obey explicit server capability flags and paid resume needs confirmation", async () => {
  const jobs = [{ id: "job1", status: "pending", stage: "review", queue_status: "failed", queue_job_id: "content:job1", can_cancel: false, can_resume: true, requires_paid_confirmation: true, input: {} }];
  const h = harness({ jobs }); await h.library.load(); await h.click("tab", "jobs");
  assert.doesNotMatch(h.html(), /data-library-action="cancel-job"/); assert.match(h.html(), /data-library-action="resume-job"/);
  await h.click("resume-job", "job1"); await h.submit("job-action"); assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  h.field("confirmed", "", { type: "checkbox", checked: true }); await h.submit("job-action");
  const call = h.calls.find(([method]) => method === "POST"); assert.equal(call[1], "/jobs/content%3Ajob1/resume"); assert.equal(call[2].confirm_paid, true);
});

test("material paste sends explicit rights flags and becomes a pending block review, never auto-confirms", async () => {
  const pendingDoc = { material: { id: "pending", revision: 1, title: "粘贴原文", status: "parsed", license_code: "own", rights_confirmed: true, visibility: "private" }, blocks: [{ id: "pending-b", anchor: "text:1", status: "draft", kind: "text", text: "教材原文" }], issues: [] };
  const h = harness({ upload: async () => pendingDoc }); await h.library.load(); await h.click("paste-material");
  h.field("title", "粘贴原文"); h.field("paste_text", "教材原文"); h.field("license_code", "own"); h.field("rights_confirmed", "", { type: "checkbox", checked: true });
  await h.submit("material-upload"); const upload = h.calls.find(([method]) => method === "UPLOAD");
  assert.equal(upload[2].get("external_processing_allowed"), "false"); assert.equal(upload[2].get("rights_confirmed"), "true"); assert.equal(upload[2].get("class_id"), "class1");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0); assert.match(h.html(), /核对课程材料/); assert.match(h.html(), /pending-b/);
});

for (const kind of ["manual", "generate", "edit"]) {
  test(`${kind} reuses the request key for unchanged valid payloads, changes it with payloads and resets it for a new modal`, async () => {
    const row = fixtureQuestion();
    const h = harness({ questions: [row], call: (method, url, body, fallback) => {
      if (method === "POST") throw new Error("连接中断，提交结果未知");
      if (url.includes("/questions/q1/versions/1")) return { question: row, sources: [], reviews: {}, versions: [] };
      return fallback(method, url, body);
    } });
    await h.library.load();
    const open = async () => {
      if (kind === "manual") await fillQuestion(h);
      else if (kind === "generate") await h.click("generate");
      else { await h.click("question-detail", "q1@1"); await h.click("edit-question"); }
      await h.field("source_material_id", "m1");
      h.field("source_block_ids", "", { type: "checkbox", item: "b1", checked: true });
      h.field("external_processing_allowed", "", { type: "checkbox", checked: true });
    };
    await open();
    const form = kind === "generate" ? "generate" : "question";
    await h.submit(form); await h.submit(form);
    let writes = h.calls.filter(([method]) => method === "POST");
    assert.equal(writes.length, 2);
    assert.match(writes[0][2].request_id, /^[A-Za-z0-9_.:-]{1,100}$/);
    assert.equal(writes[0][2].request_id, writes[1][2].request_id);
    // Picker text is not part of the submitted business payload.
    h.field("source_search", "原文"); await h.submit(form);
    writes = h.calls.filter(([method]) => method === "POST");
    assert.equal(writes[2][2].request_id, writes[0][2].request_id);
    const changedField = kind === "generate" ? "instruction" : "stem";
    const original = kind === "generate" ? "" : writes[0][2].question.stem;
    h.field(changedField, "不同的有效内容"); await h.submit(form);
    writes = h.calls.filter(([method]) => method === "POST");
    assert.notEqual(writes[3][2].request_id, writes[0][2].request_id);
    h.field(changedField, original); await h.submit(form);
    writes = h.calls.filter(([method]) => method === "POST");
    assert.equal(writes[4][2].request_id, writes[0][2].request_id, "restoring a payload retains its unresolved request key");
    await h.click("discard-modal"); await open(); await h.submit(form);
    writes = h.calls.filter(([method]) => method === "POST");
    assert.notEqual(writes.at(-1)[2].request_id, writes[0][2].request_id);
    if (kind === "edit") { assert.match(writes[0][1], /\/questions\/q1\/edit-jobs$/); assert.equal(writes[0][2].expected_version, 1); }
  });
}

test("text questions require rubric before any POST while fill keeps single-blank acceptable alternatives", async () => {
  const h = harness(); await h.library.load(); await fillQuestion(h, "text", "完整作答");
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true });
  assert.match(h.html(), /name="rubric"[^>]*required/);
  assert.match(h.html(), /作答题必须填写评分细则/);
  await h.submit("question");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  h.field("rubric", "说明正确 | 1"); await h.submit("question");
  const written = h.calls.find(([method]) => method === "POST");
  assert.deepEqual(plain(written[2].question.rubric), [{ point: "说明正确", score_units: 100 }]);
  await fillQuestion(h, "fill", "1/2\n0.5");
  assert.match(h.html(), /单空；可接受的答案每行一个/);
  assert.doesNotMatch(h.html(), /name="rubric"[^>]*required/);
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true }); await h.submit("question");
  const fill = h.calls.filter(([method]) => method === "POST").at(-1);
  assert.deepEqual(plain(fill[2].question.answer), ["1/2", "0.5"]);
});

test("knowledge filter and question rows show confirmed titles but submit IDs and retain legacy facets", async () => {
  const row = fixtureQuestion("knowledge", { knowledge_points: ["kp1", "legacy-id"] });
  const h = harness({ call: (method, url, body, fallback) => url.includes("question-library") ? { items: [row], total: 1, facets: { knowledge_points: [{ value: "kp1", count: 1 }, { value: "legacy-id", count: 1 }] } } : fallback(method, url, body) });
  await h.library.load(); const html = h.html();
  assert.match(html, /<select name="knowledge_point"/);
  assert.match(html, /<option value="kp1"[^>]*>已确认知识点<\/option>/);
  assert.match(html, /<option value="legacy-id"[^>]*>legacy-id<\/option>/);
  assert.match(html, /library-question-foot"><span>已确认知识点、legacy-id<\/span>/);
  h.field("knowledge_point", "kp1", { scope: "filters" }); await h.submit("filters");
  const url = h.calls.filter(([, url]) => url.includes("question-library")).at(-1)[1];
  assert.equal(new URLSearchParams(url.split("?")[1]).get("knowledge_point"), "kp1");
});

test("local import accepts non-external source rights but cannot turn preview or a checkbox into review authorization", async () => {
  const preview = { import_id: "local-only", rows: [{ row_number: 1, status: "valid", errors: [], duplicate: null, question: { stem: "本地题目" } }], summary: { total: 1, valid: 1, invalid: 0, duplicate: 0 } };
  const h = harness({ material: { external_processing_allowed: false }, upload: async () => preview });
  await h.library.load(); await h.click("import");
  assert.match(h.html(), /真实材料（仅本地预览）/);
  await h.field("source_material_id", "m1");
  h.field("source_block_ids", "", { type: "checkbox", item: "b1", checked: true });
  h.field("import_file", "", { type: "file", files: [new File(["[]"], "local.json")] });
  await h.submit("import-preview");
  assert.equal(h.calls.filter(([method]) => method === "UPLOAD").length, 1);
  assert.match(h.html(), /未获外发许可/);
  assert.match(h.html(), /disabled>确认并提交审查/);
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true }); await h.submit("import-confirm");
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
  assert.equal(h.doc.material.external_processing_allowed, false);
  await h.click("discard-modal"); await h.click("manual");
  assert.doesNotMatch(h.html(), /<option value="m1"/);
});

test("leased preparation jobs keep polling until the reviewed content is published", async () => {
  const jobs = [{ id: "leased-job", status: "pending", stage: "review", queue_status: "leased", input: {} }];
  const h = harness({ jobs }); await h.library.load(); await h.click("tab", "jobs");
  assert.match(h.html(), /已接单/);
  assert.equal(h.timers.size, 1);
  const [id, tick] = [...h.timers.entries()][0]; h.timers.delete(id);
  jobs[0] = { ...jobs[0], status: "completed", queue_status: "succeeded", state: { published_versions: [{ question_id: "q1", version: 1 }] } };
  const before = h.calls.length; await tick();
  assert.ok(h.calls.slice(before).some(([method, url]) => method === "GET" && url.includes("/question-jobs")));
  assert.match(h.html(), /审查通过，已入库/);
  assert.equal(h.timers.size, 0);
  h.library.reset();
});

test("content terminal status outranks queue success and only completed jobs with published versions are approved", async () => {
  const cases = [
    [{ status: "quarantined", queue_status: "succeeded" }, "已隔离", "warn"],
    [{ status: "failed", queue_status: "succeeded" }, "失败", "warn"],
    [{ status: "cancelled", queue_status: "succeeded" }, "已取消", null],
    [{ status: "completed", queue_status: "succeeded", state: { published_versions: [{ question_id: "q1", version: 1 }] } }, "审查通过，已入库", "ok"],
    [{ status: "completed", queue_status: "succeeded", state: { published_versions: [] } }, "处理结束，尚无通过记录", "warn"],
    [{ status: "pending", queue_status: "succeeded", state: { published_versions: [{ question_id: "q1", version: 1 }] } }, "处理结束，尚无通过记录", "warn"],
    [{ status: "pending", queue_status: "not_dispatched" }, "等待调度", null],
  ];
  for (const [fields, label, tone] of cases) {
    const h = harness({ jobs: [{ id: "state-case", stage: "review", input: {}, ...fields }] });
    await h.library.load(); await h.click("tab", "jobs");
    const header = h.html().match(/<div class="library-job-header">[\s\S]*?<\/span>/)?.[0];
    assert.ok(header?.includes(label), `expected ${label}`);
    if (tone) assert.ok(header.includes(`library-badge-${tone}`));
    if (label !== "审查通过，已入库") assert.ok(!header.includes("审查通过，已入库"));
    h.library.reset();
  }
});

test("CSV paste preserves exact text and filename and remains an unconfirmed material", async () => {
  const pendingDoc = { material: { id: "pasted", status: "parsed", revision: 1, title: "原件" }, blocks: [], issues: [] };
  const h = harness({ upload: async () => pendingDoc });
  await h.library.load(); await h.click("paste-material");
  const original = '材料编号,题干或内容\r\nX01,"多行\n原文"\r\n';
  h.field("title", "原件"); h.field("paste_format", "csv"); h.field("paste_text", original);
  h.field("source_filename", "题目与材料.csv"); h.field("license_code", "permission");
  h.field("license_note", "本机已核对"); h.field("rights_confirmed", "", { type: "checkbox", checked: true });
  await h.submit("material-upload");
  const file = h.calls.find(([method]) => method === "UPLOAD")[2].get("file");
  assert.equal(file.name, "题目与材料.csv"); assert.equal(await file.text(), original);
  assert.equal(h.calls.filter(([method]) => method === "POST").length, 0);
});

test("material supersede accepts active and inactive originals and verifies both returned materials", async () => {
  for (const lifecycle_status of ["active", "inactive"]) {
    const replacement = { id: "m2", title: "修正版", revision: 4, lifecycle_status: "active", status: "confirmed", rights_confirmed: true, usage_scope: "production", visibility: "private", class_id: null };
    const replacementDoc = { material: replacement, blocks: [{ id: "b2", status: "confirmed" }], issues: [] };
    let replaced = false;
    const h = harness({ material: { lifecycle_status, can_replace: true, visibility: "private", usage_scope: "production", revision: 3 }, call: (method, url, body, fallback) => {
      if (url.includes("/materials?")) return { items: [h.doc.material, replacement] };
      if (url === "/materials/m2") return replacementDoc;
      if (method === "POST" && url === "/materials/m1/supersede") { replaced = true; return {}; }
      if (method === "GET" && url === "/materials/m1" && replaced) return { ...h.doc, material: { ...h.doc.material, lifecycle_status: "superseded", replacement_material_id: "m2", revision: 4 } };
      return fallback(method, url, body);
    } });
    await h.library.load(); await h.click("tab", "materials"); await h.click("material-detail", "m1");
    assert.doesNotMatch(h.html().match(/<button[^>]*data-library-action="material-replace"[^>]*>/)?.[0] || "missing", /disabled|missing/);
    await h.click("material-replace"); await h.field("replacement_material_id", "m2");
    await h.submit("material-supersede");
    assert.equal(replaced, false, "explicit confirmation remains required");
    h.field("confirmed", "", { type: "checkbox", checked: true });
    await h.submit("material-supersede");
    const request = h.calls.find(([method, url]) => method === "POST" && url.endsWith("/supersede"));
    assert.ok(request, `${lifecycle_status} original can be replaced`);
    assert.equal(request[2].expected_revision, 3); assert.equal(request[2].expected_replacement_revision, 4);
    assert.equal(request[2].replacement_material_id, "m2");
    assert.ok(h.toasts.includes("材料替换已确认；旧材料历史与来源记录仍保留。"));
    assert.equal(h.calls.filter(([method, url]) => method === "GET" && url === "/materials/m2").length, 2);
    h.library.reset();
  }
});

test("material supersede still rejects replaced originals and ineligible replacements", async () => {
  for (const invalid of [{ old: "superseded" }, { old: "unknown" }, { lifecycle_status: "inactive" }, { status: "parsed" }, { rights_confirmed: false }, { usage_scope: "evaluation" }, { visibility: "course" }, { class_id: "other-class" }]) {
    const replacementDoc = { material: { id: "m2", revision: 1, lifecycle_status: "active", status: "confirmed", rights_confirmed: true, usage_scope: "production", visibility: "private", class_id: null, ...invalid }, blocks: [], issues: [] };
    const h = harness({ material: { lifecycle_status: "inactive", visibility: "private", usage_scope: "production" }, call: (method, url, body, fallback) => url === "/materials/m2" ? replacementDoc : fallback(method, url, body) });
    await h.library.load(); await h.click("material-detail", "m1"); await h.click("material-replace");
    await h.field("replacement_material_id", "m2");
    if (invalid.old) h.doc.material.lifecycle_status = invalid.old;
    h.field("confirmed", "", { type: "checkbox", checked: true }); await h.submit("material-supersede");
    assert.equal(h.calls.filter(([method]) => method === "POST").length, 0, JSON.stringify(invalid));
    assert.match(h.html(), /替代材料必须启用/);
    h.library.reset();
  }
});

test("evaluation completion stays a candidate and editing retains evaluation intent", async () => {
  const row = fixtureQuestion("q1", { generation_intent: "evaluation", usage_scope: "evaluation" });
  row.status = "candidate";
  const jobs = [{ id: "evaluation-job", status: "completed", stage: "done", input: { intent: "evaluation" },
    state: { published_versions: [{ question_id: "q1", version: 1 }] } }];
  const h = harness({ questions: [row], jobs, material: { usage_scope: "evaluation" },
    call: (method, url, body, fallback) => url.includes("/versions/1") ? { question: row, sources: [], reviews: {}, versions: [] } : fallback(method, url, body) });
  await h.library.load(); await h.click("tab", "jobs");
  assert.match(h.html(), /评测审查完成，未入正式题库/); assert.doesNotMatch(h.html(), /审查通过，已入库/);
  await h.click("tab", "questions"); await h.click("question-detail", "q1@1"); await h.click("edit-question");
  assert.match(h.html(), /当前用途：评测验证/);
  await h.field("source_material_id", "m1");
  h.field("external_processing_allowed", "", { type: "checkbox", checked: true });
  await h.submit("question");
  const request = h.calls.find(([method, url]) => method === "POST" && url.endsWith("edit-jobs"));
  assert.ok(request); assert.equal(request[2].intent, "evaluation");
});

test("detail loading and repaint retain modal focus, close restores its exact entry, and background locks are reversible", async () => {
  const pending = deferred();
  const row = fixtureQuestion();
  const h = harness({ dom: true, questions: [row], call: (method, url, body, fallback) => url.includes("/questions/q1/versions/1") ? pending.promise : fallback(method, url, body) });
  await h.library.load();
  const opening = h.click("question-detail", "q1@1");
  assert.equal(h.dom.doc.activeElement.className, "library-drawer");
  assert.equal(h.dom.sidebar.inert, true); assert.equal(h.dom.context.inert, true);
  assert.equal(h.dom.host.inert, false); assert.equal(h.root.inert, false);
  assert.equal(h.dom.unrelated.inert, false, "other workspace elements are outside the host isolation boundary");
  assert.equal(h.dom.doc.body.style.getPropertyValue("overflow"), "hidden");
  pending.resolve({ question: row, sources: [], reviews: {}, versions: [] }); await opening;
  assert.ok(h.root.querySelector(".library-drawer").contains(h.dom.doc.activeElement), "async detail completion must not strand focus on body");
  const close = h.root.querySelector('[data-library-action="close-modal"]'); close.focus();
  assert.equal(h.keydown("Tab", true).prevented, true);
  assert.equal(h.dom.doc.activeElement.dataset.libraryAction, "prepare-from-question", "Shift+Tab wraps inside the drawer");
  assert.equal(h.keydown("Tab").prevented, true);
  assert.equal(h.dom.doc.activeElement.dataset.libraryAction, "close-modal");
  await h.click("close-modal");
  assert.equal(h.dom.doc.activeElement.dataset.libraryAction, "question-detail");
  assert.equal(h.dom.doc.activeElement.dataset.libraryValue, "q1@1");
  assert.equal(h.dom.doc.activeElement.className, "library-question-title", "restore the actual opener, not another action with the same value");
  assert.equal(h.dom.sidebar.inert, false); assert.equal(h.dom.context.inert, false); assert.equal(h.dom.preexisting.inert, true);
  assert.equal(h.dom.doc.documentElement.style.getPropertyValue("overflow"), "auto");
  assert.equal(h.dom.doc.documentElement.style.getPropertyPriority("overflow"), "important");
  assert.equal(h.dom.doc.body.style.getPropertyValue("overflow"), "clip");
});

test("type switch and host rerender preserve field focus and selection; reset and unbind always remove owned locks", async () => {
  const h = harness({ dom: true }); await h.library.load(); await h.click("manual");
  h.field("type", "text");
  assert.equal(h.dom.doc.activeElement.dataset.libraryField, "type");
  h.field("stem", "正在编辑的题干", {}, "input");
  const textarea = h.dom.doc.activeElement; textarea.setSelectionRange(2, 5);
  // focusin remembers the field; host render must recover that field even after its node is detached.
  h.hostRender();
  assert.equal(h.dom.doc.activeElement.dataset.libraryField, "stem");
  assert.match(h.html(), /正在编辑的题干/);
  h.field("type", "judge");
  assert.equal(h.dom.doc.activeElement.dataset.libraryField, "type");
  await h.click("close-modal");
  assert.ok(h.root.querySelector(".library-drawer").contains(h.dom.doc.activeElement));
  assert.equal(h.dom.sidebar.inert, true, "discard confirmation keeps the background isolated");
  await h.click("discard-modal");
  assert.equal(h.dom.doc.activeElement.dataset.libraryAction, "manual"); assert.equal(h.dom.sidebar.inert, false);
  await h.click("manual"); h.library.reset();
  assert.equal(h.dom.sidebar.inert, false); assert.equal(h.dom.doc.body.style.getPropertyValue("overflow"), "clip");
  h.hostRender(); await h.click("manual");
  h.library.bind({ querySelector: () => null });
  assert.equal(h.dom.sidebar.inert, false); assert.equal(h.dom.doc.body.style.getPropertyValue("overflow"), "clip");
});
