"use strict";

const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-reports.js");
const source = readFileSync(sourcePath, "utf8");
const apiSource = readFileSync(path.resolve(__dirname, "../../web/prototype/api.js"), "utf8");
const plain = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const error = (status, code = "error") => Object.assign(new Error(code), { status, code });
const decode = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (_, key) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[key]));

function surface() {
  const events = {}, frames = [], nodes = []; let html = "";
  const root = { isConnected: true, matches: (selector) => selector === "[data-teacher-reports]", addEventListener: (type, fn) => { events[type] = fn; }, contains: (target) => nodes.includes(target), querySelector: (selector) => selector === "[data-report-preview-frame]" ? frames[0] : null };
  Object.defineProperty(root, "innerHTML", { get: () => html, set: (value) => {
    html = value; frames.length = 0; nodes.length = 0;
    for (const match of html.matchAll(/<(button|select|iframe)\b([^>]*)>/g)) {
      const attrs = {};
      for (const attr of match[2].matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) attrs[attr[1]] = decode(attr[2] || "");
      const node = { attrs, disabled: "disabled" in attrs, value: "", dataset: Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])), closest() { return this; }, matches(selector) { return selector === ":disabled" && this.disabled; }, setAttribute(key, value) { this.attrs[key] = value; }, removeAttribute(key) { delete this.attrs[key]; } };
      nodes.push(node); if (match[1] === "iframe") frames.push(node);
    }
  } });
  return { root, nodes, frames, events };
}

function fixture(options = {}) {
  const dom = surface(), calls = [], toasts = [], changes = [], downloads = [], revoked = [];
  const state = { live: true, role: "teacher", courseId: "course", classId: "class", transitionId: 1, teacherContextVersion: 1, ...options.state };
  const member = { user_id: "student", display_name: "合成学生" };
  const model = { goals: Array.from({ length: options.goalCount || 2 }, (_, index) => ({ id: `goal-${index + 1}`, title: `目标 ${index + 1}`, class_id: "class" })), source: 10, reports: [], revisions: new Map(), serial: 0 };
  const rules = { schema_version: "report.snapshot.v1", default_rule_version: "mastery.v1", export_formats: ["html", "pdf", "xlsx", "json", "csv"] };
  function revision(scope, sourceRevision = model.source) {
    const reportId = `report-${scope.kind}-${scope.goal_id || scope.student_id || scope.question_bank_id || "class"}`;
    const value = { id: `revision-${++model.serial}`, report_id: reportId, revision_number: model.serial, source_revision: sourceRevision,
      rule_version: rules.default_rule_version, snapshot_sha256: "a".repeat(64), created_at: 1700000000, is_current: true,
      snapshot: { scope: plain(scope), title: `REPORT ${scope.kind}`, summary: { students: 1, evidence: 0 }, tables: { mastery: { columns: [{ key: "percent", label: "掌握度" }], rows: [{ percent: null }] } } } };
    model.revisions.set(value.id, value);
    model.reports = [{ id: reportId, scope: plain(scope), current_revision_id: value.id, rule_version: value.rule_version }, ...model.reports.filter((row) => row.id !== reportId)];
    return plain(value);
  }
  const defaultScope = { kind: "class", course_id: "course", class_id: "class" };
  if (options.saved) { revision(defaultScope, 8); revision(defaultScope, 9); }
  const fallback = {
    goals: async (classId, params) => plain(model.goals.slice(params.offset, params.offset + params.limit)),
    classMembers: async () => [plain(member)],
    teacherClasses: async () => [{ course: { id: "course", can_manage_materials: options.manage !== false }, classes: state.classId ? [{ id: "class", can_manage: options.manage !== false }] : [] }],
    reportFormats: async () => plain(rules), reportList: async () => plain(model.reports),
    reportSource: async (scope) => ({ scope: plain(scope), source_revision: model.source, schema_version: rules.schema_version }),
    reportRevisions: async (reportId) => [...model.revisions.values()].filter((item) => item.report_id === reportId).reverse().map(plain),
    reportRevision: async (reportId, revisionId) => plain(model.revisions.get(revisionId || model.reports.find((row) => row.id === reportId).current_revision_id)),
    reportPreview: async (reportId, revisionId) => `<h1>PREVIEW ${revisionId}</h1><script>unsafe()</script>`,
    generateReport: async (scope, key, prepared) => revision(scope, prepared.expected_source_revision),
    reportDownload: async (reportId, format, revisionId) => ({ blob: new Blob([revisionId]), filename: `fixed.${format === "csv" ? "zip" : format}` }),
  };
  const api = { state: { user: { id: "teacher", role: "teacher", status: "active" } } };
  for (const name of Object.keys(fallback)) api[name] = (...args) => {
    calls.push({ name, args: args.map((arg) => arg && typeof arg === "object" ? plain(arg) : arg) });
    return options[name] ? options[name](...args, fallback[name]) : fallback[name](...args);
  };
  let instance, renders = 0, uuid = 0;
  const document = { body: { appendChild() {} }, createElement: () => ({ click() { downloads.push({ href: this.href, filename: this.download }); }, remove() {} }) };
  const context = vm.createContext({ AbortController, URLSearchParams, Blob, document, setTimeout, crypto: { randomUUID: () => `unique-${++uuid}` }, URL: { createObjectURL: () => `blob:${uuid}`, revokeObjectURL: (url) => revoked.push(url) } });
  vm.runInContext(source.replace("export function createTeacherReports", "function createTeacherReports") + "\nglobalThis.factory = createTeacherReports;", context, { filename: sourcePath });
  const render = () => { renders += 1; dom.root.innerHTML = instance.render(); instance.bind(dom.root); };
  instance = context.factory({ api, state, render, toast: (value) => toasts.push(value), onChanged: (value) => changes.push(plain(value)) });
  render();
  function node(key, value) { const found = dom.nodes.find((node) => node.dataset[key] === value); assert.ok(found, `${key}=${value} exists`); return found; }
  const click = (key, value) => { const target = node(key, value); return dom.events.click({ target, preventDefault() {} }); };
  return { instance, state, api, model, calls, dom, toasts, changes, downloads, revoked, render, node, click, revision, defaultScope, renders: () => renders,
    change: (kind, value) => { const target = node("reportSelection", kind); target.value = value; return dom.events.change({ target }); },
    count: (name) => calls.filter((call) => call.name === name).length, html: () => instance.render() };
}

test("loads all historical goals and uses whole-goal scope without student or attempt filters", async () => {
  const h = fixture({ goalCount: 101 });
  assert.equal(await h.instance.openSelection({ goalId: "goal-101", studentId: "student", attemptId: "attempt" }), true);
  assert.deepEqual(h.calls.filter((call) => call.name === "goals").map((call) => call.args[1].offset), [0, 100]);
  const source = h.calls.find((call) => call.name === "reportSource");
  assert.deepEqual(source.args[0], { kind: "goal", course_id: "course", class_id: "class", goal_id: "goal-101" });
  assert.match(h.html(), /目标 101/);
  assert.equal(await h.instance.openSelection({ goalId: "missing" }), false);
  assert.match(h.html(), /指定学习目标不存在/);
});

test("question bank is course-scoped and works without a class", async () => {
  const h = fixture({ state: { classId: null } });
  await h.click("reportKind", "question_bank");
  const source = h.calls.find((call) => call.name === "reportSource");
  assert.deepEqual(source.args[0], { kind: "question_bank", course_id: "course", question_bank_id: "course" });
  assert.equal(h.count("goals"), 0);
  assert.equal(await h.click("reportAction", "generate"), true);
  assert.match(h.html(), /整个课程题库/);
});

test("read collaborators view stored revisions but cannot create or read a create-only source", async () => {
  const h = fixture({ saved: true, manage: false }); await h.instance.load();
  assert.equal(h.count("reportSource"), 0); assert.match(h.html(), /REPORT class/);
  assert.equal(h.node("reportAction", "generate").disabled, true);
  await h.click("reportAction", "generate"); assert.equal(h.count("generateReport"), 0);
});

test("generation locks scope and leave, retries exactly one command, and rejects duplicate clicks", async () => {
  let attempts = 0; const gate = deferred();
  const h = fixture({ generateReport: async (scope, key, prepared, fallback) => { attempts += 1; if (attempts === 1) return gate.promise; return fallback(scope, key, prepared); } });
  await h.instance.load(); const pending = h.click("reportAction", "generate");
  assert.equal(h.instance.leave(), false); assert.equal(await h.instance.openSelection({ goalId: "goal-2" }), false);
  await h.click("reportAction", "generate"); assert.equal(h.count("generateReport"), 1);
  gate.reject(error(0)); assert.equal(await pending, false);
  h.model.source = 99;
  assert.equal(h.instance.leave(), false); assert.equal(await h.click("reportAction", "retry"), true);
  const writes = h.calls.filter((call) => call.name === "generateReport");
  assert.deepEqual(writes[0].args, writes[1].args); assert.equal(writes[1].args[2].expected_source_revision, 10);
  assert.equal(h.instance.leave(), true);
});

test("a confirmed source conflict needs a refresh and a new command", async () => {
  let first = true;
  const h = fixture({ generateReport: async (scope, key, prepared, fallback) => { if (first) { first = false; throw error(409, "source_revision_conflict"); } return fallback(scope, key, prepared); } });
  await h.instance.load(); await h.click("reportAction", "generate"); assert.equal(h.node("reportAction", "generate").disabled, true);
  h.model.source = 11; await h.click("reportAction", "refresh"); await h.click("reportAction", "generate");
  const writes = h.calls.filter((call) => call.name === "generateReport");
  assert.notEqual(writes[0].args[1], writes[1].args[1]); assert.equal(writes[1].args[2].expected_source_revision, 11);
});

test("malformed successful generation remains uncertain instead of silently losing its request", async () => {
  const h = fixture({ generateReport: async () => ({ id: "wrong", report_id: "wrong", snapshot: { scope: { kind: "student" } } }) });
  await h.instance.load(); assert.equal(await h.click("reportAction", "generate"), false);
  assert.match(h.html(), /用同一请求重试/); assert.equal(h.instance.leave(), false);
});

test("late revision and preview reads cannot overwrite a newer scope selection", async () => {
  const gate = deferred(); let blocked = false;
  const h = fixture({ saved: true, reportRevision: async (reportId, revisionId, opts, fallback) => { if (revisionId === "revision-1" && blocked) return gate.promise; return fallback(reportId, revisionId, opts); } });
  await h.instance.load(); blocked = true;
  const old = h.click("reportRevision", "revision-1"); await settle();
  await h.click("reportKind", "goal");
  gate.resolve(plain(h.model.revisions.get("revision-1"))); assert.equal(await old, false);
  assert.doesNotMatch(h.html(), /REPORT class|PREVIEW revision-1/);
  assert.equal(h.node("reportKind", "goal").attrs["aria-pressed"], "true");
});

test("stored revision selection pins preview and every download, renders empty evidence accurately", async () => {
  const h = fixture({ saved: true }); await h.instance.load(); await h.click("reportRevision", "revision-1");
  assert.match(h.html(), /证据不足/); assert.match(h.html(), /<h1>阶段成果/);
  const frame = h.dom.frames[0]; assert.equal(frame.attrs.sandbox, ""); assert.match(frame.srcdoc, /PREVIEW revision-1/);
  assert.doesNotMatch(h.html(), /<script>unsafe/);
  for (const format of ["html", "pdf", "xlsx", "json", "csv"]) {
    assert.equal(await h.click("reportDownload", format), true);
    const call = h.calls.filter((call) => call.name === "reportDownload").at(-1);
    assert.equal(call.args[2], "revision-1"); assert.equal(call.args[3].snapshotHash, "a".repeat(64));
  }
  assert.equal(h.downloads.at(-1).filename, "fixed.zip");
  const liveFrame = h.dom.frames[0];
  assert.equal(h.instance.leave(), true); assert.equal(liveFrame.srcdoc, ""); assert.equal(h.dom.root.innerHTML, "");
});

test("logout locks new actions but preserves and quietly settles a sent generation", async () => {
  const gate = deferred(); const h = fixture({ generateReport: () => gate.promise });
  await h.instance.load(); const pending = h.click("reportAction", "generate");
  h.state.loggingOut = true; h.render(); const renders = h.renders(), reads = h.count("reportPreview");
  gate.resolve(h.revision(h.defaultScope)); assert.equal(await pending, true);
  assert.equal(h.renders(), renders); assert.equal(h.count("reportPreview"), reads); assert.equal(h.changes.length, 0);
  assert.equal(await h.instance.openSelection({ goalId: "goal-2" }), false);
  h.state.loggingOut = false; h.render(); assert.match(h.html(), /REPORT class/); assert.equal(h.instance.leave(), true);
});

test("reset invalidates a generation receipt and a late download never clicks after logout", async () => {
  const gate = deferred(); const h = fixture({ generateReport: () => gate.promise }); await h.instance.load();
  const pending = h.click("reportAction", "generate"); h.instance.reset(); h.state.classId = "other";
  gate.resolve(h.revision(h.defaultScope)); assert.equal(await pending, false); assert.doesNotMatch(h.html(), /REPORT class/);
  const download = deferred(); const h2 = fixture({ saved: true, reportDownload: () => download.promise }); await h2.instance.load();
  const downloading = h2.click("reportDownload", "pdf"); h2.state.loggingOut = true;
  download.resolve({ blob: new Blob(["pdf"]), filename: "a.pdf" }); assert.equal(await downloading, false); assert.equal(h2.downloads.length, 0);
});

test("refresh removes no-longer-authorized stored data, and denied reads fail closed", async () => {
  const h = fixture({ saved: true }); await h.instance.load(); h.model.reports = [];
  await h.click("reportAction", "refresh"); assert.doesNotMatch(h.html(), /REPORT class/);
  const h2 = fixture({ reportList: () => Promise.reject(error(403)) }); await h2.instance.load(); assert.match(h2.html(), /无权访问/);
});

test("a failed report history read clears the previous revision before showing the error", async () => {
  let fail = false;
  const h = fixture({ saved: true, reportRevisions: (reportId, opts, fallback) => fail ? Promise.reject(error(500, "history_unavailable")) : fallback(reportId, opts) });
  await h.instance.load(); assert.match(h.html(), /REPORT class/); fail = true;
  assert.equal(await h.click("reportId", h.model.reports[0].id), false);
  assert.doesNotMatch(h.html(), /REPORT class|data-report-preview-frame|data-report-download/);
  assert.match(h.html(), /history_unavailable/);
});

test("preview and download authorization failures remove cached private report content", async () => {
  for (const status of [401, 403, 404]) {
    const preview = fixture({ saved: true, reportPreview: () => Promise.reject(error(status)) });
    assert.equal(await preview.instance.load(), false);
    assert.match(preview.html(), /无权访问/); assert.doesNotMatch(preview.html(), /REPORT class|data-report-download/);
    const download = fixture({ saved: true, reportDownload: () => Promise.reject(error(status)) });
    await download.instance.load(); assert.equal(await download.click("reportDownload", "pdf"), false);
    assert.match(download.html(), /无权访问/); assert.doesNotMatch(download.html(), /REPORT class|data-report-download/);
    assert.equal(download.downloads.length, 0);
  }
});

function apiFixture(fetch) {
  const context = vm.createContext({ fetch, URLSearchParams, location: { protocol: "http:", origin: "http://127.0.0.1" } });
  vm.runInContext(apiSource.replace(/^export /gm, "") + "\nglobalThis.productionApi = api;", context);
  return context.productionApi;
}

test("API prepared report retries avoid source reads and preserve body and key", async () => {
  const calls = []; const api = apiFixture(async (url, options) => { calls.push({ url, options }); return new Response("{}", { status: 201 }); });
  const prepared = { schema_version: "report.snapshot.v1", rule_version: "v1", expected_source_revision: 12 }, scope = { kind: "class", class_id: "cl" };
  await api.generateReport(scope, "same-request", prepared); await api.generateReport(scope, "same-request", prepared);
  assert.equal(calls.length, 2); assert.deepEqual(calls[0], calls[1]); assert.equal(calls[0].url, "/api/v1/reports");
  assert.equal(calls[0].options.headers["Idempotency-Key"], "same-request"); assert.equal(JSON.parse(calls[0].options.body).expected_source_revision, 12);
});

test("API exports validate revision and hash, encode IDs, and supply real extensions", async () => {
  const calls = []; const headers = { "X-Report-Revision": "rev /?", "X-Report-Snapshot-SHA256": "hash", "Content-Disposition": "attachment; filename*=UTF-8''%E6%8A%A5%E5%91%8A.zip" };
  const api = apiFixture(async (url) => { calls.push(url); return new Response("REPORT", { status: 200, headers }); });
  const result = await api.reportDownload("id /?", "csv", "rev /?", { snapshotHash: "hash" });
  assert.equal(result.filename, "报告.zip"); assert.match(calls[0], /id%20%2F%3F\/export\/csv\?revision_id=rev\+%2F%3F$/);
  assert.equal(await api.reportPreview("id", "rev /?", { snapshotHash: "hash" }), "REPORT");
  await assert.rejects(api.reportPreview("id", "different"), { code: "report_revision_mismatch" });
  await assert.rejects(api.reportDownload("id", "pdf", "rev /?", { snapshotHash: "wrong" }), { code: "report_revision_mismatch" });
  await assert.rejects(api.reportDownload("id", "pdf", ""), { code: "report_revision_required" });
});

test("API reports genuine missing export dependencies and keeps abort distinct", async () => {
  const api = apiFixture(async () => new Response(JSON.stringify({ detail: { code: "export_dependency_unavailable", message: "缺少 PDF 导出依赖" } }), { status: 503 }));
  await assert.rejects(api.reportDownload("id", "pdf", "rev"), { status: 503, code: "export_dependency_unavailable", message: "缺少 PDF 导出依赖" });
  const aborted = Object.assign(new Error("aborted"), { name: "AbortError" });
  const api2 = apiFixture(async () => { throw aborted; }); await assert.rejects(api2.reportPreview("id", "rev"), (error) => error === aborted);
});
