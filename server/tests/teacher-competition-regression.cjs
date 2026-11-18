"use strict";
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const file = resolve(__dirname, "../../web/prototype/teacher-competition.js");
const code = readFileSync(file, "utf8").replace("export function createTeacherCompetition", "function createTeacherCompetition");
const later = () => { let resolve; const promise = new Promise((yes) => { resolve = yes; }); return { promise, resolve }; };
function harness(call) {
  const calls = [], events = {};
  const state = { live: true, role: "teacher", courseId: "course-a", classId: "class-a", teacherContextVersion: 1 };
  const api = { state: { user: { id: "teacher-a" } }, call: async (...args) => { calls.push(args); return call ? call(...args) : args[1] === "/competition/schema" ? { tables: [{ filename: "records.csv", required: true, columns: ["identifier"] }] } : { items: [] }; } };
  const context = vm.createContext({ console, FormData, setTimeout, crypto: require("node:crypto").webcrypto });
  vm.runInContext(code + "\nglobalThis.factory = createTeacherCompetition;", context);
  const app = context.factory({ api, state });
  const root = { matches: () => true, addEventListener: (name, handler) => { events[name] = handler; } };
  const bind = () => app.bind(root);
  bind();
  const field = (name, value, extras = {}) => { bind(); const target = { value, ...extras, dataset: { competitionField: name, ...extras.dataset }, closest() { return this; } }; events.change({ type: "change", target }); };
  const click = (action) => { bind(); const target = { dataset: { competitionAction: action }, closest() { return this; } }; return events.click({ target }); };
  return { app, calls, state, api, field, click, events, bind };
}
test("disconnected competition does not call services or fabricate data", async () => {
  const h = harness(); h.state.live = false; await h.app.load();
  assert.equal(h.calls.length, 0); assert.match(h.app.render(), /请连接正式服务/); assert.match(h.app.render(), /学情分析/);
});
test("late response from an old course cannot expose its history", async () => {
  const old = later();
  const h = harness((method, url) => url.includes("course-a") ? old.promise : url === "/competition/schema" ? { tables: [] } : { items: [] });
  const loading = h.app.load(); h.state.courseId = "course-b"; await h.app.load();
  old.resolve({ items: [{ id: "private-old", title: "OLD_COURSE_PRIVATE" }] }); await loading;
  assert.doesNotMatch(h.app.render(), /OLD_COURSE_PRIVATE/);
});
test("unsaved text requires explicit discard even after a second navigation attempt", async () => {
  const h = harness(); await h.app.load(); h.app.render(); h.field("mode", "paste"); h.field("title", "本次新任务");
  h.field("csv", "identifier\n001", { dataset: { competitionTable: "records.csv" } });
  assert.equal(h.app.leave(), false); assert.equal(h.app.leave(), false);
  assert.match(h.app.render(), /001/); h.click("keep"); assert.equal(h.app.leave(), false);
  h.click("discard"); assert.equal(h.app.leave(), true); assert.doesNotMatch(h.app.render(), /001/);
});
test("pasted input is sent unchanged and new preview retains the exact server run", async () => {
  let sent;
  const h = harness((method, url, body) => {
    if (method === "POST") { sent = body; return { run: { id: "run-new", course_id: "course-a", title: body.title, status: "preview" }, preview: { summary: {}, options: {}, issues: [] } }; }
    return url === "/competition/schema" ? { tables: [{ filename: "records.csv", required: true, columns: ["identifier"] }] } : { items: [] };
  });
  await h.app.load(); h.app.render(); h.field("mode", "paste"); h.field("title", "新任务");
  const input = 'identifier,answer\n001,"原始中文,换行\n第二行"'; h.field("csv", input, { dataset: { competitionTable: "records.csv" } });
  h.bind(); h.events.submit({ preventDefault() {}, target: { closest: () => true } });
  await new Promise(setImmediate); assert.equal(sent.csv_texts["records.csv"], input);
  assert.match(h.app.render(), /run-new/); assert.equal(h.app.leave(), true);
});
