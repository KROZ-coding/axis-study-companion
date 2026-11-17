"use strict";
// Pure in-memory DOM/API regressions. No server, database, fetch or model calls.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");
const sourcePath = path.resolve(__dirname, "../../web/prototype/teacher-insights.js");
const source = readFileSync(sourcePath, "utf8");
const plain = (value) => value === undefined ? undefined : JSON.parse(JSON.stringify(value));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const settle = () => new Promise((resolve) => setImmediate(resolve));
const failure = (status, code = "error") => Object.assign(new Error(code), { status, code });
const decode = (value) => value.replace(/&(amp|lt|gt|quot|#39);/g, (_, key) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" }[key]));

function surface() {
  let html = "";
  const nodes = [], events = {};
  const matches = (node, selector) => selector.split(",").some((part) => {
    const attr = part.trim().match(/^\[([^=\]]+)(?:="([^"]*)")?\]$/);
    return attr && Object.hasOwn(node.attrs, attr[1]) && (attr[2] === undefined || node.attrs[attr[1]] === attr[2]);
  });
  const root = { isConnected: true, matches: (selector) => selector === "[data-teacher-insights]", contains: (node) => nodes.includes(node),
    addEventListener: (kind, callback) => { events[kind] = callback; }, querySelector: (selector) => nodes.find((node) => matches(node, selector)) || null,
    querySelectorAll: (selector) => nodes.filter((node) => matches(node, selector)) };
  Object.defineProperty(root, "innerHTML", { get: () => html, set: (value) => {
    html = value; nodes.length = 0;
    for (const match of value.matchAll(/<(button|input|select|textarea|section|legend)\b([^>]*)>/g)) {
      const attrs = {};
      for (const attr of match[2].matchAll(/([^\s=]+)(?:="([^"]*)")?/g)) attrs[attr[1]] = decode(attr[2] || "");
      const node = { attrs, tagName: match[1].toUpperCase(), type: attrs.type, disabled: Object.hasOwn(attrs, "disabled"),
        value: attrs.value || "", checked: Object.hasOwn(attrs, "checked"),
        dataset: Object.fromEntries(Object.entries(attrs).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])),
        closest(selector) { return selector === "[data-teacher-insights]" ? root : selector === "[data-insight-action]" && this.dataset.insightAction ? this : null; },
      };
      nodes.push(node);
    }
  } });
  return { root, nodes, events };
}

function fixture(options = {}) {
  const dom = surface(), calls = [], changes = [], toasts = [];
  const state = { live: true, role: "teacher", courseId: "course", classId: "class", transitionId: 1,
    reviewSelection: { goalId: "goal-1", taskId: "shared-task", studentId: "shared-student" }, ...options.state };
  const model = { goals: Array.from({ length: options.goalCount || 2 }, (_, index) => ({ id: `goal-${index + 1}`, class_id: "class", course_id: "course", title: `历史目标 ${index + 1}` })),
    insights: new Map(), jobs: new Map(), drafts: new Map(), revisions: new Map(), commands: new Map(), reports: [], serial: 0, published: 0, bounded: 0 };
  function revision(goalId, suffix = "current") {
    const value = { id: `revision-${goalId}-${suffix}`, report_id: `report-${goalId}`, revision_number: suffix === "old" ? 1 : 2,
      source_revision: suffix === "old" ? 8 : 10, snapshot_sha256: (suffix === "old" ? "a" : "b").repeat(64), created_at: 1700000000,
      snapshot: { schema_version: "report.snapshot.v1", scope: { kind: "goal", goal_id: goalId, course_id: "course", class_id: "class" } } };
    model.revisions.set(value.id, value); return value;
  }
  for (const goal of model.goals) {
    revision(goal.id, "old"); const current = revision(goal.id);
    model.reports.push({ id: current.report_id, scope: current.snapshot.scope, current_revision_id: current.id });
  }
  function insight(goalId = "goal-1", overrides = {}) {
    const rev = model.revisions.get(`revision-${goalId}-current`), insightId = `insight-${++model.serial}`;
    const fact = { student_code: "S001", status: "insufficient_evidence", percent: null, independent_families: 0, observed_families: 0, evidence_refs: ["mastery:S001"] };
    const value = { id: insightId, job_id: `job-${insightId}`, report_id: rev.report_id, source_report_revision: rev.id, snapshot_sha256: rev.snapshot_sha256,
      source_revision: rev.source_revision, current_source_revision: 10, origin_goal_id: goalId, goal_version: 4, class_id: "class", course_id: "course",
      status: "completed", generation: 1, input_hash: "c".repeat(64), facts: { student_count: 1, status_counts: { mastered: 0, developing: 0, needs_practice: 0, insufficient_evidence: 1 }, students: [fact] },
      error: null, stale: false, created_at: 1700000000, updated_at: 1700000000,
      student_codes: { S001: "student" }, evidence_index: { "report:scope": { table: "goals", goal_id: goalId }, "report:rule": { table: "rules" }, "report:students": { table: "students" }, "mastery:S001": { table: "mastery", student_id: "student", evidence_refs: [] } },
      result: { schema_version: "teaching.insight.result.v1", origin: "ai", interpretation_mode: "validated_selection", input_hash: "c".repeat(64), summary: `合成总结 ${goalId}`,
        evidence_refs: ["report:scope", "report:rule", "report:students"], findings: [{ ...fact, explanation: "独立证据不足，请继续收集首次作答。" }], limitations: ["目标层面解释", "尚未校准的规则"] }, ...overrides };
    model.insights.set(value.id, value);
    model.jobs.set(value.job_id, { job_id: value.job_id, kind: "teaching_insight", resource_type: "teaching_insight", resource_id: value.id,
      status: options.jobStatus || (value.status === "completed" ? "succeeded" : value.status === "pending" ? "queued" : value.status), attempts: 1, max_attempts: 3,
      cancel_requested: false, cost_pending: false, reconciliation_required: false, created_at: 1700000000 });
    return value;
  }
  function draft(insightId) {
    const base = model.insights.get(insightId), value = { id: `draft-${++model.serial}`, insight_id: insightId, origin_goal_id: base.origin_goal_id,
      source_report_revision: base.source_report_revision, goal_version: base.goal_version, class_id: "class", course_id: "course", next_round: 2,
      revision: 1, status: "draft", paper_overrides: {}, basis: { students: { student: base.facts.students[0] } }, bounded_plan: null, current_goal_version: 4,
      config: { course_id: "course", class_id: "class", title: "延续原目标", description: "", student_ids: ["student"], question_count: 1,
        question_types: { single_choice: 1, fill: 0, judge: 0, text: 0 }, common_level: "balanced", difficulty: "any", knowledge_points: [],
        scheduled_at: null, deadline_at: null, allow_late: false, allow_retries: true, allow_retry_after_deadline: false } };
    model.drafts.set(value.id, value); return value;
  }
  function preview(value) {
    const questions = Object.entries(value.config.question_types).flatMap(([kind, count]) => Array.from({ length: count }, (_, index) => ({
      question_id: `question-${kind}-${index}`, version: 1, type: kind, stem: `完整题干 ${kind} ${index}`, options: kind === "single_choice" ? ["甲", "乙"] : [],
      points: 1, points_units: 100, source_anchors: [{ file_id: "material", block_id: "block", anchor: "第 3 页" }], common: true,
    })));
    return { draft_id: value.id, draft_revision: value.revision, origin_goal_id: value.origin_goal_id, insight_id: value.insight_id, next_round: value.next_round,
      config: plain(value.config), allocation_mode: "deterministic_teacher_confirmed", ready: true, readiness: "ready", errors: [], deficits: [], warnings: [],
      students: value.config.student_ids.map((student_id) => ({ student_id, display_name: "合成学生", questions: plain(questions), total_score: questions.length })),
      questions, common_questions: questions, question_count: value.config.question_count, common_count: questions.length,
      preview_token: `preview-${value.id}-${value.revision}`, preview_expires_at: Date.now() / 1000 + 600 };
  }
  if (options.saved !== false) insight("goal-1", options.insight || {});
  const fallback = {
    goals: async (classId, params) => plain(model.goals.slice(params.offset, params.offset + params.limit)),
    classMembers: async () => [{ user_id: "student", display_name: "合成学生" }],
    teacherClasses: async () => [{ course: { id: "course" }, classes: [{ id: "class", can_manage: options.manage !== false }] }],
    reportList: async () => plain(model.reports),
    reportRevisions: async (reportId) => [...model.revisions.values()].filter((item) => item.report_id === reportId).map(plain),
    reportRevision: async (reportId, revisionId) => plain(model.revisions.get(revisionId)),
    call: async (method, url, payload) => {
      const [pathname, search = ""] = url.split("?"), parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
      const params = new URLSearchParams(search);
      if (parts[0] === "courses" && parts[2] === "question-library") return { items: [{ question_id: "future-question", version: 1, course_id: "course", status: "approved", content: { type: "single_choice", stem: "后续审核题", options: ["甲", "乙"], answer: 0, source_anchors: [] } }], total: 1 };
      if (parts[0] === "jobs") {
        const job = model.jobs.get(parts[1]); if (!job) throw failure(404, "job_not_found");
        if (method === "POST") {
          if (parts[2] === "cancel") { job.cancel_requested = true; job.status = "cancelled"; model.insights.get(job.resource_id).status = "cancelled"; }
          if (parts[2] === "resume") { assert.equal(payload.confirm_paid, true); job.status = "queued"; model.insights.get(job.resource_id).status = "pending"; }
        }
        return plain(job);
      }
      assert.equal(parts[0], "teaching-insights", `unexpected path ${url}`);
      if (parts.length === 1 && method === "GET") return { items: [...model.insights.values()].filter((item) => item.origin_goal_id === params.get("goal_id")).map(plain) };
      const key = payload?.request_id;
      if (key && model.commands.has(key)) return plain(model.commands.get(key));
      let result;
      if (parts.length === 1 && method === "POST") {
        const rev = model.revisions.get(payload.source_report_revision);
        result = insight(rev.snapshot.scope.goal_id, { report_id: rev.report_id, source_report_revision: rev.id, source_revision: rev.source_revision, snapshot_sha256: rev.snapshot_sha256,
          status: "pending", result: null });
      } else if (parts[1] !== "next-round-drafts" && parts.length === 2 && method === "GET") {
        result = model.insights.get(parts[1]); if (!result) throw failure(404);
      } else if (parts[2] === "next-round-drafts" && method === "POST") result = draft(parts[1]);
      else if (parts[1] === "next-round-drafts") {
        const value = model.drafts.get(parts[2]); if (!value) throw failure(404);
        if (method === "GET") result = value;
        else if (parts.length === 3 && method === "PUT") {
          if (payload.expected_revision !== value.revision) throw failure(409, "draft_revision_conflict");
          value.revision += 1; value.config = plain(payload.config); value.paper_overrides = plain(payload.paper_overrides); result = value;
        } else if (parts[3] === "preview") result = preview(value);
        else if (parts[3] === "publish") {
          if (value.status === "published" || payload.expected_revision !== value.revision) throw failure(409, "draft_already_published");
          assert.equal(payload.confirm, true); assert.equal(payload.preview_token, `preview-${value.id}-${value.revision}`);
          result = { draft_id: value.id, insight_id: value.insight_id, goal_id: value.origin_goal_id, origin_goal_id: value.origin_goal_id,
            round_id: `round-${++model.published}`, round_no: value.next_round, source_report_revision: value.source_report_revision };
          value.status = "published"; value.revision += 1; value.current_goal_version += 1; value.published_result = plain(result);
        } else if (parts[3] === "bounded-plan") {
          assert.equal(value.status, "published"); assert.equal(payload.confirm, true); assert.equal(payload.expected_goal_version, value.current_goal_version);
          model.bounded += 1; result = { goal_id: value.origin_goal_id, goal_version: value.current_goal_version + 1, status: "active", source_draft_id: value.id,
            future_round_count: payload.future_round_count, remaining_rounds: payload.future_round_count, interval_seconds: payload.interval_seconds,
            ends_at: payload.ends_at, question_pool: plain(payload.question_pool) }; value.bounded_plan = result;
        }
      }
      assert.ok(result, `unexpected call ${method} ${url}`);
      if (key) model.commands.set(key, plain(result)); return plain(result);
    },
  };
  const api = { state: { user: { id: "teacher", role: "teacher", status: "active" } } };
  for (const [name, fn] of Object.entries(fallback)) api[name] = (...args) => {
    calls.push({ name, args: args.map(plain) }); return options[name] ? options[name](...args, fn) : fn(...args);
  };
  const context = vm.createContext({ AbortController, URLSearchParams, crypto: { randomUUID: (() => { let count = 0; return () => `memory-${++count}`; })() } });
  vm.runInContext(source.replace("export function createTeacherInsights", "function createTeacherInsights") + "\nglobalThis.factory = createTeacherInsights;", context, { filename: sourcePath });
  let instance, renderCount = 0;
  const render = () => { renderCount += 1; dom.root.innerHTML = instance.render(); instance.bind(dom.root); };
  instance = context.factory({ api, state, render, toast: (value) => toasts.push(value), onChanged: (value) => changes.push(plain(value)) }); render();
  function node(key, value) { const item = dom.nodes.find((item) => item.dataset[key] === value); assert.ok(item, `missing ${key}=${value}`); return item; }
  const event = (target) => ({ target, preventDefault() {}, stopPropagation() {} });
  const click = (action, value) => {
    const target = dom.nodes.find((item) => item.dataset.insightAction === action && (value === undefined || item.dataset.insightId === value));
    assert.ok(target, `missing action ${action}`); return dom.events.click(event(target));
  };
  const change = (key, value, input) => { const target = node(key, value); if (typeof input === "boolean") target.checked = input; else target.value = input; return dom.events.change(event(target)); };
  const input = (key, value, content) => { const target = node(key, value); target.value = content; return dom.events.input(event(target)); };
  return { instance, state, api, model, dom, calls, changes, toasts, render, node, click, change, input, insight, draft, preview, fallback,
    html: () => instance.render(), renders: () => renderCount, writes: (suffix = "") => calls.filter((call) => call.name === "call" && ["POST", "PUT"].includes(call.args[0]) && call.args[1].endsWith(suffix)) };
}

async function readyDraft(h) { await h.instance.load(); assert.equal(await h.click("draft-create"), true); }
async function publish(h) { await readyDraft(h); assert.equal(await h.click("draft-preview"), true); h.change("insightConfirm", "publish", true); assert.equal(await h.click("publish"), true); }

test("independent historical catalog pages fully and preserves unrelated shared review IDs", async () => {
  const h = fixture({ goalCount: 101 });
  assert.equal(await h.instance.openSelection({ goalId: "goal-101" }), true);
  assert.deepEqual(h.calls.filter((call) => call.name === "goals").map((call) => call.args[1].offset), [0, 100]);
  assert.equal(h.state.reviewSelection.goalId, "goal-101"); assert.equal(h.state.reviewSelection.taskId, "shared-task");
  assert.match(h.html(), /历史目标 101/); assert.equal(await h.instance.openSelection({ goalId: "missing" }), false);
  assert.doesNotMatch(h.html(), /合成总结/);
});

test("generation freezes revision and request ID, locks duplicate clicks and replays unknown unchanged", async () => {
  const gate = deferred(); let attempt = 0;
  const h = fixture({ saved: false, call: (method, url, payload, ...rest) => {
    if (method === "POST" && url === "/teaching-insights" && ++attempt === 1) return gate.promise;
    return rest.at(-1)(method, url, payload);
  } });
  await h.instance.load(); await h.change("insightSelection", "revision", "revision-goal-1-old");
  h.change("insightConfirm", "generate", true); const pending = h.click("generate");
  await h.click("generate"); assert.equal(h.writes("/teaching-insights").length, 1); assert.equal(h.instance.leave(), false);
  assert.equal(await h.instance.openSelection({ goalId: "goal-2" }), false); gate.reject(failure(0)); assert.equal(await pending, false);
  assert.equal(h.instance.leave(), false); assert.match(h.html(), /原请求结果未知/);
  assert.equal(await h.click("retry"), true); const writes = h.writes("/teaching-insights");
  assert.deepEqual(writes[0].args, writes[1].args); assert.equal(writes[1].args[2].source_report_revision, "revision-goal-1-old");
  assert.equal(h.instance.leave(), true);
});

test("history and detail refresh are GET-only, stale is explicit and empty evidence is not zero", async () => {
  const h = fixture({ insight: { stale: true } }); await h.instance.load();
  assert.match(h.html(), /旧数据 · 冻结历史证据/); assert.match(h.html(), /证据不足/); assert.doesNotMatch(h.html(), /<td>0%<\/td>/);
  await h.click("detail"); await h.click("refresh"); assert.equal(h.writes().length, 0);
});

test("read-only teacher can inspect saved facts but cannot create, cancel, restore or publish", async () => {
  const h = fixture({ manage: false }); await h.instance.load(); assert.match(h.html(), /合成总结 goal-1/); assert.match(h.html(), /只读权限/);
  assert.equal(h.node("insightAction", "draft-create").disabled, true); await h.click("draft-create"); assert.equal(h.writes().length, 0);
  assert.equal(h.dom.nodes.some((item) => item.dataset.insightAction === "generate"), false);
});

test("fee flags block resume; explicit paid confirmation is sent, cancellation is explicit", async () => {
  const h = fixture({ insight: { status: "failed", result: null }, jobStatus: "failed" }); await h.instance.load();
  assert.equal(h.node("insightAction", "resume").disabled, true); await h.click("resume"); assert.equal(h.writes().length, 0);
  h.change("insightConfirm", "resume", true); assert.equal(await h.click("resume"), true);
  assert.equal(h.writes("/resume")[0].args[2].confirm_paid, true);
  assert.equal(await h.click("cancel"), true); await h.click("detail"); assert.match(h.html(), /已取消/);
  assert.equal(h.dom.nodes.some((item) => item.dataset.insightAction === "resume"), false);
  const h2 = fixture({ insight: { status: "failed", result: null }, jobStatus: "awaiting_confirmation" });
  [...h2.model.jobs.values()][0].cost_pending = true; await h2.instance.load();
  assert.match(h2.html(), /费用待核对/); assert.equal(h2.dom.nodes.some((item) => item.dataset.insightAction === "resume"), false);
});

test("invalid model output and cancelled jobs never offer paid resume", async () => {
  for (const overrides of [{ status: "cancelled", result: null }, { status: "failed", result: null, error: { code: "invalid_model_output", message: "需重新生成" } }]) {
    const h = fixture({ insight: overrides, jobStatus: "failed" }); await h.instance.load();
    assert.equal(h.dom.nodes.some((item) => item.dataset.insightAction === "resume"), false);
  }
});

test("uncertain paid recovery reads its original job and never automatically resends POST", async () => {
  let first = true;
  const h = fixture({ insight: { status: "failed", result: null }, jobStatus: "failed", call: async (method, url, payload, ...rest) => {
    const fallback = rest.at(-1); const result = await fallback(method, url, payload);
    if (url.endsWith("/resume") && first) { first = false; throw failure(0); } return result;
  } });
  await h.instance.load(); h.change("insightConfirm", "resume", true); assert.equal(await h.click("resume"), false);
  assert.equal(h.instance.leave(), false); assert.equal(await h.click("retry"), true);
  assert.equal(h.writes("/resume").length, 1); assert.equal(h.instance.leave(), true);
});

test("late scope reads cannot replace a newer explicit selection", async () => {
  const gate = deferred(); let hold = false;
  const h = fixture({ call: (method, url, payload, ...rest) => {
    const fallback = rest.at(-1); if (hold && method === "GET" && url.includes("goal_id=goal-1")) return gate.promise;
    return fallback(method, url, payload);
  } });
  await h.instance.load(); hold = true; const old = h.instance.openSelection({ goalId: "goal-1" }); await settle();
  assert.equal(await h.instance.openSelection({ goalId: "goal-2" }), true); gate.resolve({ items: [...h.model.insights.values()].map(plain) });
  assert.equal(await old, false); assert.equal(h.state.reviewSelection.goalId, "goal-2"); assert.doesNotMatch(h.html(), /合成总结 goal-1/);
});

test("notification insight ID derives its goal and cross-class identities invalidate cached facts", async () => {
  const h = fixture(); const other = h.insight("goal-2");
  assert.equal(await h.instance.openSelection({ insightId: other.id }), true); assert.equal(h.state.reviewSelection.goalId, "goal-2");
  h.state.classId = "other-class"; h.render(); assert.doesNotMatch(h.html(), /合成总结 goal-2/); assert.equal(h.writes().length, 0);
});

test("logout suspends new actions, quietly settles sent writes and preserves identity", async () => {
  const gate = deferred(); const h = fixture({ saved: false, call: (method, url, payload, ...rest) => method === "POST" && url === "/teaching-insights" ? gate.promise : rest.at(-1)(method, url, payload) });
  await h.instance.load(); h.change("insightConfirm", "generate", true); const pending = h.click("generate");
  h.state.loggingOut = true; h.render(); const renders = h.renders(), calls = h.calls.length;
  assert.equal(await h.instance.openSelection({ goalId: "goal-2" }), false);
  gate.resolve(h.insight("goal-1", { status: "pending", result: null })); assert.equal(await pending, true);
  assert.equal(h.renders(), renders); assert.equal(h.calls.length, calls); assert.equal(h.changes.length, 0);
  h.state.loggingOut = false; h.render(); assert.match(h.html(), /等待生成/); assert.equal(h.instance.leave(), true);
});

test("reset and actor transition discard late write receipts and stale DOM events", async () => {
  for (const reset of [true, false]) {
    const gate = deferred(); const h = fixture({ saved: false, call: (method, url, payload, ...rest) => method === "POST" && url === "/teaching-insights" ? gate.promise : rest.at(-1)(method, url, payload) });
    await h.instance.load(); h.change("insightConfirm", "generate", true); const oldButton = h.node("insightAction", "generate"), pending = h.click("generate");
    if (reset) h.instance.reset(); else { h.api.state.user.id = "another-teacher"; h.render(); }
    gate.resolve(h.insight()); assert.equal(await pending, false); assert.doesNotMatch(h.html(), /合成总结/); assert.equal(h.changes.length, 0);
    h.dom.events.click({ target: oldButton, preventDefault() {} }); assert.equal(h.writes().length, 1);
  }
});

test("authorization failures on insight or job clear all private caches", async () => {
  for (const status of [401, 403, 404]) {
    let deny = false;
    const h = fixture({ call: (method, url, payload, ...rest) => deny && url.startsWith("/jobs/") ? Promise.reject(failure(status)) : rest.at(-1)(method, url, payload) });
    await h.instance.load(); deny = true; assert.equal(await h.click("detail"), false);
    assert.match(h.html(), /已清除.*私有缓存/); assert.doesNotMatch(h.html(), /合成总结|合成学生|报告校验值/);
    assert.equal(await h.instance.load({ force: true }), false);
  }
});

test("strict result schemas and fact/evidence bindings reject mismatches without rendering raw model HTML", async () => {
  for (const mutate of [
    (row) => { row.result.schema_version = "unknown"; },
    (row) => { row.result.findings[0].percent = 99; },
    (row) => { row.result.findings[0].evidence_refs = ["made-up"]; },
    (row) => { row.result.input_hash = "d".repeat(64); },
  ]) {
    const h = fixture(); const item = [...h.model.insights.values()][0]; mutate(item);
    assert.equal(await h.instance.load(), false); assert.doesNotMatch(h.html(), /合成总结 goal-1/); assert.match(h.html(), /不匹配|不一致/);
  }
  const h = fixture(); const item = [...h.model.insights.values()][0]; item.result.summary = '<img src=x onerror="bad()">';
  item.result.findings[0].explanation = "<script>bad()</script>"; await h.instance.load();
  assert.match(h.html(), /&lt;img/); assert.match(h.html(), /&lt;script/); assert.doesNotMatch(h.html(), /<script>|<img src=x/);
});

test("malformed successful creation remains unknown and preserves the original replay payload", async () => {
  let bad = true;
  const h = fixture({ saved: false, call: (method, url, payload, ...rest) => bad && method === "POST" && url === "/teaching-insights" ? { id: "wrong" } : rest.at(-1)(method, url, payload) });
  await h.instance.load(); h.change("insightConfirm", "generate", true); assert.equal(await h.click("generate"), false);
  assert.equal(h.instance.leave(), false); bad = false; assert.equal(await h.click("retry"), true);
  assert.deepEqual(h.writes()[0].args[2], h.writes()[1].args[2]);
});

test("draft CAS saves exact config and count totals, editing invalidates preview and confirmation", async () => {
  const h = fixture(); await readyDraft(h); await h.click("draft-preview");
  assert.match(h.html(), /完整题干/); assert.match(h.html(), /第 3 页/); h.change("insightConfirm", "publish", true);
  h.change("insightType", "fill", "2"); assert.doesNotMatch(h.html(), /完整题干/); assert.equal(h.node("insightAction", "publish").disabled, true);
  assert.equal(await h.click("draft-save"), true); const save = h.writes().find((call) => call.args[0] === "PUT");
  assert.equal(save.args[2].expected_revision, 1); assert.equal(save.args[2].config.question_count, 3); assert.equal(save.args[2].config.question_types.fill, 2);
  await h.click("draft-preview"); assert.match(h.html(), /修订 2 的题卷/); assert.equal(h.node("insightAction", "publish").disabled, true);
});

test("draft save retries unknown CAS with identical request, and confirmed conflict requires refresh", async () => {
  let lost = true;
  const h = fixture({ call: async (method, url, payload, ...rest) => {
    const result = await rest.at(-1)(method, url, payload); if (method === "PUT" && lost) { lost = false; throw failure(0); } return result;
  } });
  await readyDraft(h); h.change("insightField", "allow_late", true); assert.equal(await h.click("draft-save"), false);
  assert.equal(await h.click("retry"), true); const saves = h.writes().filter((call) => call.args[0] === "PUT"); assert.deepEqual(saves[0].args, saves[1].args);
  const h2 = fixture({ call: (method, url, payload, ...rest) => method === "PUT" ? Promise.reject(failure(409, "draft_revision_conflict")) : rest.at(-1)(method, url, payload) });
  await readyDraft(h2); h2.change("insightField", "allow_late", true); assert.equal(await h2.click("draft-save"), false);
  assert.match(h2.html(), /draft_revision_conflict/); assert.equal(h2.node("insightAction", "draft-preview").disabled, true);
  assert.equal(h2.instance.leave(), true);
});

test("preview binding mismatch, shortages and unfinished previous round block publication", async () => {
  for (const kind of ["wrong-revision", "deficit", "previous-round", "expired"]) {
    const h = fixture({ call: async (method, url, payload, ...rest) => {
      const result = await rest.at(-1)(method, url, payload);
      if (url.endsWith("/preview")) {
        if (kind === "wrong-revision") result.draft_revision += 1;
        if (kind === "expired") result.preview_expires_at = 1;
        if (kind === "deficit") { result.ready = false; result.readiness = "blocked"; result.deficits = [{ question_type: "single_choice", required: 1, available: 0 }]; result.preview_token = null; }
        if (kind === "previous-round") { result.ready = false; result.readiness = "blocked"; result.errors = [{ code: "previous_round_incomplete", message: "前轮仍有未完成首次作答" }]; result.preview_token = null; }
      } return result;
    } });
    await readyDraft(h); await h.click("draft-preview"); assert.equal(h.node("insightAction", "publish").disabled, true);
    await h.click("publish"); assert.equal(h.model.published, 0);
  }
});

test("explicit publishing is idempotently replayed, never creates another goal or a duplicate round", async () => {
  const gate = deferred(); let first = true;
  const h = fixture({ call: async (method, url, payload, ...rest) => {
    const result = await rest.at(-1)(method, url, payload);
    if (url.endsWith("/publish") && first) { first = false; await gate.promise; } return result;
  } });
  await readyDraft(h); await h.click("draft-preview"); await h.click("publish"); assert.equal(h.model.published, 0);
  h.change("insightConfirm", "publish", true); const pending = h.click("publish"); await settle(); await h.click("publish");
  assert.equal(h.model.published, 1); gate.reject(failure(0)); assert.equal(await pending, false);
  assert.equal(await h.click("retry"), true); assert.equal(h.model.published, 1);
  const writes = h.writes("/publish"); assert.deepEqual(writes[0].args, writes[1].args);
  assert.match(h.html(), /已发布到原目标 goal-1 的第 2 轮/); assert.equal(h.dom.nodes.some((item) => item.dataset.insightAction === "publish"), false);
  assert.equal(h.writes().some((call) => call.args[1] === "/goals"), false);
  assert.equal(h.changes.at(-1).publication.goal_id, "goal-1");
});

test("bounded planning is unavailable before publish and requires blank-by-default explicit controls", async () => {
  const h = fixture(); await publish(h); assert.equal(h.model.bounded, 0);
  assert.equal(await h.click("bounded-load"), true);
  assert.equal(h.node("insightBounded", "future_round_count").value, ""); assert.equal(h.node("insightAction", "bounded").disabled, true);
  h.change("insightPool", "future-question@1", true); h.change("insightBounded", "future_round_count", "2");
  h.change("insightBounded", "interval_seconds", "60"); h.change("insightBounded", "ends_at", "2099-01-02T10:00");
  h.change("insightConfirm", "bounded", true); assert.equal(await h.click("bounded"), true); assert.equal(h.model.bounded, 1);
  const payload = h.writes("/bounded-plan")[0].args[2]; assert.equal(payload.expected_goal_version, 5); assert.equal(payload.confirm, true);
  assert.deepEqual(payload.question_pool, [{ question_id: "future-question", version: 1 }]); assert.match(h.html(), /剩余 2 轮/);
});

test("published draft can be restored by explicit IDs without POSTing or losing server state", async () => {
  const h = fixture(); await publish(h); const value = [...h.model.drafts.values()][0], writes = h.writes().length;
  assert.equal(h.instance.leave(), true); assert.equal(await h.instance.openSelection({ insightId: value.insight_id, draftId: value.id }), true);
  assert.match(h.html(), /已发布到原目标/); assert.equal(h.writes().length, writes);
});

test("input invalidates preview immediately without unmounting the focused editor", async () => {
  const h = fixture(); await readyDraft(h); await h.click("draft-preview"); h.change("insightConfirm", "publish", true);
  const editor = h.node("insightType", "fill"), renders = h.renders();
  assert.equal(h.input("insightType", "fill", "2"), true);
  assert.equal(h.renders(), renders); assert.equal(h.node("insightType", "fill"), editor);
  assert.equal(h.node("insightAction", "publish").disabled, true);
  assert.equal(h.node("insightConfirm", "publish").checked, false); assert.equal(h.node("insightConfirm", "publish").disabled, true);
  assert.equal(h.node("insightAction", "draft-save").disabled, false);
  assert.equal(h.node("insightAction", "draft-preview").disabled, true);
  assert.equal(h.node("insightPreview", "").textContent, "配置已修改，请保存后重新预览。");
  assert.match(h.node("insightCount", "").textContent, /每人题量 3 道/);
  await h.click("publish"); assert.equal(h.model.published, 0);
  assert.equal(await h.click("draft-save"), true); assert.equal(h.writes().find((call) => call.args[0] === "PUT").args[2].config.question_types.fill, 2);
});

test("logout interrupts a fee reconciliation read without stranding its original command", async () => {
  const gate = deferred(); let loseResume = true, holdRead = false;
  const h = fixture({ insight: { status: "failed", result: null }, jobStatus: "failed", call: async (method, url, payload, ...rest) => {
    if (method === "GET" && url.startsWith("/jobs/") && holdRead) return gate.promise;
    const result = await rest.at(-1)(method, url, payload);
    if (url.endsWith("/resume") && loseResume) { loseResume = false; throw failure(0); }
    return result;
  } });
  await h.instance.load(); h.change("insightConfirm", "resume", true); assert.equal(await h.click("resume"), false);
  holdRead = true; const pending = h.click("retry"); await settle();
  h.state.loggingOut = true; h.render(); const renders = h.renders(), callbacks = h.changes.length;
  gate.resolve(plain([...h.model.jobs.values()][0])); assert.equal(await pending, false);
  assert.equal(h.renders(), renders); assert.equal(h.changes.length, callbacks);
  h.state.loggingOut = false; h.render(); assert.equal(h.node("insightAction", "retry").disabled, false);
  holdRead = false; assert.equal(await h.click("retry"), true); assert.equal(h.writes("/resume").length, 1);
  assert.equal(h.instance.leave(), true);
});

test("insight draft references restore latest and selected drafts through GET-only reads", async () => {
  const h = fixture({ manage: false }), insight = [...h.model.insights.values()][0];
  const first = h.draft(insight.id), latest = h.draft(insight.id); latest.config.title = "最新已存草稿"; first.config.title = "另一已存草稿";
  const reference = (draft) => Object.fromEntries(["id", "revision", "status", "next_round", "origin_goal_id", "goal_version", "source_report_revision"].map((key) => [key, draft[key]]));
  insight.next_round_drafts = [reference(latest), reference(first)];
  assert.equal(await h.instance.openSelection({ insightId: insight.id }), true); assert.match(h.html(), /最新已存草稿/);
  assert.equal(h.node("insightSelection", "draft").disabled, false);
  assert.equal(await h.change("insightSelection", "draft", first.id), true); assert.match(h.html(), /另一已存草稿/);
  assert.equal(h.node("insightAction", "draft-save").disabled, true); assert.equal(h.writes().length, 0);
  assert.equal(h.instance.leave(), true); assert.equal(await h.instance.openSelection({ insightId: insight.id }), true); assert.match(h.html(), /最新已存草稿/);
  insight.next_round_drafts[0].origin_goal_id = "goal-2";
  assert.equal(await h.instance.load({ force: true }), false); assert.doesNotMatch(h.html(), /最新已存草稿|另一已存草稿/);
});
