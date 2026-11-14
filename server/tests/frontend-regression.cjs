"use strict";

// Run with: node --test server/tests/frontend-regression.cjs
// Execute production functions, selected by declaration boundaries, with an
// isolated state and controlled promises. No browser or external packages.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const root = path.resolve(__dirname, "../..");
const appPath = path.join(root, "web/prototype/app.js");
const apiPath = path.join(root, "web/prototype/api.js");
const appSource = readFileSync(appPath, "utf8");
const apiSource = readFileSync(apiPath, "utf8");
const answerFunctions = ["questionType", "questionBlankCount", "answerMissing", "questionChoiceValue", "questionInputValue", "questionAnswerText", "refreshAnswerProgress"];

function functionSource(name) {
  const declaration = new RegExp(`^(?:async )?function ${name}\\([^]*?(?=^(?:async )?function |^// ──|^let scene =|^let revealTimer =|^let drag =|^let touchY =|$(?![^]))`, "m");
  const found = appSource.match(declaration);
  assert.ok(found, `Production function ${name} must exist`);
  return found[0];
}

function sourceSection(start, end) {
  const from = appSource.indexOf(start);
  const to = appSource.indexOf(end, from + start.length);
  assert.ok(from >= 0 && to > from, `Production section ${start} must exist`);
  return appSource.slice(from, to);
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function settle() {
  // The legacy handlers launch an async IIFE without returning its promise.
  // setImmediate drains their already-resolved promise continuations.
  await new Promise((resolve) => setImmediate(resolve));
}

function plain(value) {
  return JSON.parse(JSON.stringify(value));
}

function task(id, overrides = {}) {
  return {
    id,
    goal_id: `goal-${id}`,
    course_id: "course-1",
    status: "not_started",
    version: 3,
    questions: [{ id: `${id}-q`, stem: "Choose an answer", options: ["A", "B"], points: 1 }],
    draft_answers: {},
    attempts: [],
    ...overrides,
  };
}

function lease(taskId) {
  return { task_id: taskId, lease_token: "test-lease", fence_token: 7, lease_until: Date.now() / 1000 + 180 };
}

function harness(names, options = {}) {
  const calls = [];
  const intervals = new Map();
  const frames = new Map();
  const nodes = new Map();
  const mediaListeners = new Map();
  let serial = 10;
  const state = {
    screen: "app", role: "student", who: "Test student", live: true,
    transition: "", transitionId: 4, loggingOut: false, loading: false,
    reduce: false, reveal: 1, shifted: true,
    studentPage: "workspace", studentTab: "tasks", taskFilter: "all",
    authStatus: "ready", authTab: "login", authRole: "student", authAccount: "",
    answers: [], currentQ: 0, banner: "", submitted: false,
    liveTask: null, liveTasks: [], liveLease: null, liveGoals: [], liveStudents: [],
    liveConversations: [], liveMessages: [], conversationError: "",
    studentDrafts: {}, draftDirty: false, savedAt: null, selectedAttemptId: null,
    diagnostic: null, diagnosticAnswers: [], mode: "tutor", sheet: null,
    conversations: [], activeChat: 0, railEvents: [],
    ...options.state,
  };
  const view = {
    querySelector: () => null,
    querySelectorAll: (selector) => nodes.get(selector) || [],
    ...options.view,
  };
  const reduceQuery = {
    matches: false,
    addEventListener: (event, callback) => mediaListeners.set(event, callback),
  };
  const record = (name) => (...args) => { calls.push([name, ...args]); };
  const window = {
    scrollTo: record("scrollTo"),
    setTimeout: (callback) => { calls.push(["timeout", callback]); return ++serial; },
    ...options.window,
  };
  const context = vm.createContext({
    state, api: options.api || {}, scene: options.scene || null,
    view, window, reduceQuery,
    stage: { classList: { add: record("stage.add") } },
    document: { querySelector: () => null },
    sample: { className: "sample" },
    AUTH_ROLE_LABEL: { student: "学生", teacher: "教师", admin: "管理员" },
    EYE_OPEN: "", EYE_CLOSED: "",
    leaseTimer: 1, workspaceRequest: 0, revealTimer: 2,
    drag: { moved: true }, touchY: 100,
    crypto: { randomUUID: () => "test-device-id" },
    render: record("render"), toast: record("toast"),
    onAction: record("onAction"), onSubmit: record("onSubmit"),
    teacherLibrary: { bind: record("teacherLibrary.bind"), reset: record("teacherLibrary.reset") },
    teacherComposer: { bind: record("teacherComposer.bind"), reset: record("teacherComposer.reset") },
    resetEvents: record("resetEvents"), mountScene: record("mountScene"),
    fitCanvas: record("fitCanvas"), refreshAnchors: record("refreshAnchors"),
    loadWorkspace: record("loadWorkspace"),
    pollTask: async (...args) => { calls.push(["pollTask", ...args]); },
    createScene: () => null,
    cancelAnimationFrame: (id) => { frames.delete(id); calls.push(["cancelFrame", id]); },
    requestAnimationFrame: (callback) => { const id = ++serial; frames.set(id, callback); return id; },
    clearInterval: (id) => { intervals.delete(id); calls.push(["clearInterval", id]); },
    setInterval: (callback, delay) => { const id = ++serial; intervals.set(id, callback); calls.push(["setInterval", id, delay]); return id; },
    setTimeout: (callback) => { calls.push(["timeout", callback]); return ++serial; },
    performance: { now: () => 0 },
    ...options.globals,
  });
  vm.runInContext([...new Set([...answerFunctions, ...names])].map(functionSource).join("\n"), context, { filename: appPath });
  return { context, state, calls, intervals, frames, nodes, mediaListeners, reduceQuery };
}

const taskFunctions = ["taskFinished", "taskTitle", "updateStudentTask", "rememberStudentDraft", "startLeaseRenewal", "openLiveTask", "selectLiveTask"];

function sceneStub(overrides = {}) {
  const events = [];
  const scene = {};
  for (const name of ["setVerify", "setDenied", "resume", "stop", "reset", "setPull", "setReveal", "setAssembled", "playAssemble", "start", "setReducedMotion"]) {
    scene[name] = (...args) => { events.push([name, ...args]); };
  }
  scene.playDissolve = async (...args) => { events.push(["playDissolve", ...args]); };
  scene.playReassemble = async (...args) => { events.push(["playReassemble", ...args]); };
  return { scene: Object.assign(scene, overrides), events };
}

function apiHarness(fetch) {
  const context = vm.createContext({
    fetch, location: { protocol: "https:", origin: "https://example.invalid" }, URLSearchParams,
  });
  vm.runInContext(apiSource.replace(/^export /gm, "") + "\nglobalThis.productionApi = api;", context, { filename: apiPath });
  return context.productionApi;
}

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, headers: {}, text: async () => payload === undefined ? "" : JSON.stringify(payload) };
}

function controlNode(dataset = {}, properties = {}) {
  const listeners = new Map();
  const attributes = new Map();
  return {
    dataset, value: "", textContent: "", disabled: false, readOnly: false, style: {},
    addEventListener: (event, callback) => listeners.set(event, callback),
    setAttribute: (name, value) => attributes.set(name, value),
    getAttribute: (name) => attributes.get(name),
    fire(event) { return listeners.get(event)?.({ currentTarget: this }); },
    ...properties,
  };
}

function bindQuestion(h, diagnostic = false) {
  const question = (diagnostic ? h.state.diagnostic : h.state.liveTask).questions[h.state.currentQ];
  const chosen = (diagnostic ? h.state.diagnosticAnswers : h.state.answers)[h.state.currentQ];
  const locked = h.state.loading || !diagnostic && h.state.banner === "takeover";
  const markup = h.context.questionChoices(question, chosen, locked, diagnostic);
  const controls = [...markup.matchAll(/<(?:button|input|textarea)\b([^>]*)>/g)].map(([, attributes]) => {
    const dataset = {};
    for (const [, key, value] of attributes.matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
      dataset[key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
    }
    const node = controlNode(dataset, { disabled: /\bdisabled\b/.test(attributes), readOnly: /\breadonly\b/.test(attributes) });
    const pressed = attributes.match(/aria-pressed="([^"]*)"/);
    if (pressed) node.setAttribute("aria-pressed", pressed[1]);
    return node;
  });
  for (const [selector, key] of [["[data-choose]", "choose"], ["[data-diagnostic-choose]", "diagnosticChoose"], ["[data-answer-input]", "answerInput"]]) {
    h.nodes.set(selector, controls.filter((node) => key in node.dataset));
  }
  h.context.bind();
  return { markup, controls };
}

test("first multiple-choice option (numeric 0) is saved before submit with the server's saved version", async () => {
  const current = task("zero");
  const saved = deferred();
  const submitted = deferred();
  const requests = [];
  const h = harness(["taskTitle", "updateStudentTask", "submitPaper"], {
    state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [0], draftDirty: true, studentDrafts: { zero: { answers: [0], dirty: true } } },
    api: {
      saveDraft: (...args) => { requests.push(["save", ...args]); return saved.promise; },
      submitTask: (...args) => { requests.push(["submit", ...args]); return submitted.promise; },
    },
  });
  h.context.submitPaper();
  assert.equal(requests.length, 1, "submit must wait for draft persistence");
  assert.deepEqual(plain(requests[0][4]), { "zero-q": 0 });
  assert.equal(h.state.submitted, false);
  saved.resolve({ version: 9 });
  await settle();
  assert.equal(requests[1][0], "submit");
  assert.equal(requests[1][1], "zero");
  assert.equal(requests[1][2], 9, "submit must use the returned version, not the initial task version");
  assert.equal(h.state.studentPage, "workspace", "show the result only after the server accepts submit");
  submitted.resolve({ attempt_id: "attempt-1" });
  await settle();
  assert.equal(h.state.submitted, true);
  assert.equal(h.state.studentPage, "result");
  assert.equal(h.state.liveLease, null);
  assert.equal(h.state.liveTask.status, "submitted");
  assert.equal(h.state.studentDrafts.zero, undefined);
});

test("blank text blocks submission and selects the missing question while numeric 0 stays answered", () => {
  const current = task("missing", { questions: [{ id: "one" }, { id: "two" }] });
  let writes = 0;
  const h = harness(["submitPaper"], {
    state: { liveTask: current, answers: [0, "  \n "] },
    api: { saveDraft: () => { writes += 1; } },
  });
  h.context.submitPaper();
  assert.equal(writes, 0);
  assert.equal(h.state.currentQ, 1);
  assert.equal(h.state.submitted, false);
});

test("save progress preserves numeric 0, omits unanswered entries and applies the saved version", async () => {
  const current = task("draft", { questions: [{ id: "one" }, { id: "two" }, { id: "three" }] });
  const requests = [];
  const h = harness(["taskTitle", "updateStudentTask", "saveLiveDraft"], {
    state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [0, "", "written answer"], draftDirty: true, studentDrafts: { draft: { dirty: true } } },
    api: { saveDraft: async (...args) => { requests.push(args); return { version: 12, status: "in_progress" }; } },
  });
  await h.context.saveLiveDraft();
  assert.deepEqual(plain(requests[0][3]), { one: 0, three: "written answer" });
  assert.equal(h.state.liveTask.version, 12);
  assert.equal(h.state.draftDirty, false);
  assert.equal(h.state.studentDrafts.draft, undefined);
  assert.ok(h.state.savedAt);
});

test("a rejected save retains answers and dirty draft, and never starts submission", async () => {
  const current = task("failed-save");
  let submits = 0;
  const h = harness(["taskTitle", "updateStudentTask", "submitPaper"], {
    state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [0], draftDirty: true, studentDrafts: { "failed-save": { answers: [0], dirty: true } } },
    api: { saveDraft: async () => { throw new Error("offline"); }, submitTask: () => { submits += 1; } },
  });
  h.context.submitPaper();
  await settle();
  assert.equal(submits, 0);
  assert.deepEqual(plain(h.state.answers), [0]);
  assert.equal(h.state.draftDirty, true);
  assert.deepEqual(plain(h.state.studentDrafts["failed-save"].answers), [0]);
  assert.equal(h.state.banner, "save-fail");
  assert.equal(h.state.loading, false);
});

test("judge false, first option zero, multiple selections and filled blanks survive both save and submit", async () => {
  const questions = [
    { id: "judge", type: "judge", options: [] },
    { id: "single", type: "single_choice", options: ["A", "B"] },
    { id: "multi", type: "multi_choice", options: ["A", "B", "C"] },
    { id: "fill", type: "fill", options: [] },
    { id: "blanks", type: "fill", blank_count: 2, options: [] },
  ];
  const answers = [false, 0, [0, 2], "0", ["0", "x^2"]];
  const payload = { judge: false, single: 0, multi: [0, 2], fill: "0", blanks: ["0", "x^2"] };
  for (const operation of ["saveLiveDraft", "submitPaper"]) {
    const current = task(operation, { questions });
    const requests = [];
    const h = harness(["taskTitle", "updateStudentTask", operation], {
      state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers, draftDirty: true },
      api: {
        saveDraft: async (...args) => { requests.push(["save", ...args]); return { version: 14 }; },
        submitTask: async (...args) => { requests.push(["submit", ...args]); },
      },
    });
    await h.context[operation]();
    await settle();
    assert.deepEqual(plain(requests[0][4]), payload, operation);
    assert.equal(h.state.draftDirty, false, operation);
    if (operation === "submitPaper") {
      assert.equal(requests[1][2], 14);
      assert.equal(h.state.submitted, true);
    }
  }
});

test("task and diagnostic submission reject whitespace, empty selections and incomplete public fill slots", async () => {
  const samples = [
    [{ type: "text" }, null],
    [{ type: "fill" }, " \n\t "],
    [{ type: "short_answer" }, " \n "],
    [{ type: "case" }, "\t"],
    [{ type: "multi_choice", options: ["A", "B"] }, []],
    [{ type: "fill", blank_count: 2 }, ["x", "  "]],
    [{ type: "fill", answer_format: { blank_count: 2 } }, ["x"]],
    [{ type: "fill", blank_count: 2 }, "x"],
  ];
  for (const [shape, value] of samples) {
    const questions = [{ id: "judge", type: "judge" }, { id: "missing", ...shape }];
    const current = task("incomplete", { questions });
    for (const operation of ["submitPaper", "submitLiveDiagnostic"]) {
      let writes = 0;
      const h = harness([operation], {
        state: { liveTask: current, liveLease: lease(current.id), answers: [false, value], diagnostic: { id: "diagnostic", questions }, diagnosticAnswers: [false, value] },
        api: { saveDraft: () => { writes += 1; }, submitDiagnostic: () => { writes += 1; } },
      });
      await h.context[operation]();
      assert.equal(writes, 0, `${operation}: ${JSON.stringify(value)}`);
      assert.equal(h.state.currentQ, 1, operation);
      assert.equal(h.state.submitted, false);
      assert.ok(h.calls.some(([name, text]) => name === "toast" && text === "第 2 题还没作答"));
    }
  }
});

test("saving omits blank and incomplete answers without stripping zero, false or multi-choice arrays", async () => {
  const questions = [
    { id: "judge", type: "judge" }, { id: "single", type: "single_choice", options: ["A", "B"] },
    { id: "multi", type: "multi_choice", options: ["A", "B", "C"] },
    { id: "empty-multi", type: "multi_choice", options: ["A", "B"] },
    { id: "blank-fill", type: "fill" }, { id: "blank-text", type: "text" },
    { id: "incomplete-fill", type: "fill", blank_count: 2 },
    { id: "complete-fill", type: "fill", answer_format: { blank_count: 2 } },
  ];
  const current = task("save-types", { questions });
  let payload;
  const h = harness([...taskFunctions, "saveLiveDraft"], {
    state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [false, 0, [0, 2], [], " \n ", null, ["0", ""], ["0", "x+1"]] },
    api: {
      saveDraft: async (id, version, token, answers) => { payload = answers; return { version: 6 }; },
      task: async () => ({ ...current, version: 6, draft_answers: plain(payload) }),
    },
  });
  await h.context.saveLiveDraft();
  assert.deepEqual(plain(payload), { judge: false, single: 0, multi: [0, 2], "complete-fill": ["0", "x+1"] });
  assert.deepEqual(plain(h.state.answers[6]), ["0", ""], "incomplete local input is not rewritten");
  assert.equal(h.state.draftDirty, true, "a partial fill is still unsaved even when other answers are saved");
  assert.deepEqual(plain(h.state.studentDrafts[current.id].answers[6]), ["0", ""]);
  assert.ok(h.calls.some(([name, text]) => name === "toast" && text.includes("未填完")));
  h.state.answers = [];
  await h.context.openLiveTask();
  assert.deepEqual(plain(h.state.answers[6]), ["0", ""], "reopening must preserve the partial fill omitted by the server");
  h.state.answers[6] = ["0", "x"];
  await h.context.saveLiveDraft();
  assert.deepEqual(plain(payload["incomplete-fill"]), ["0", "x"]);
  assert.equal(h.state.draftDirty, false);
  assert.equal(h.state.studentDrafts[current.id], undefined);
});

test("diagnostic submission preserves false, zero, multi-choice and fill arrays before opening formal work", async () => {
  const questions = [
    { id: "judge", type: "judge" }, { id: "single", type: "single_choice", options: ["A", "B"] },
    { id: "multi", type: "multi_choice", options: ["A", "B"] },
    { id: "fill", type: "fill", answer_format: { blank_count: 2 } },
  ];
  const current = task("diagnostic", { questions, diagnostic_required: true });
  const gate = deferred();
  const requests = [];
  const h = harness(["taskTitle", "updateStudentTask", "submitLiveDiagnostic"], {
    state: { liveTask: current, liveTasks: [current], studentPage: "diagnostic", diagnostic: { id: "short-check", questions }, diagnosticAnswers: [false, 0, [0, 1], ["0", "x/2"]] },
    api: {
      submitDiagnostic: (...args) => { requests.push(["submit", ...args]); return gate.promise; },
      task: async (id) => { requests.push(["task", id]); return { ...current, diagnostic_required: false }; },
    },
  });
  const pending = h.context.submitLiveDiagnostic();
  assert.equal(requests.length, 1);
  assert.deepEqual(plain(requests[0][2]), { judge: false, single: 0, multi: [0, 1], fill: ["0", "x/2"] });
  assert.equal(h.state.studentPage, "diagnostic");
  gate.resolve({});
  await pending;
  assert.equal(h.state.diagnostic, null);
  assert.equal(h.state.studentPage, "home");
  assert.equal(h.state.liveTask.diagnostic_required, false);
});

test("question controls follow explicit type, retain selected zero and false, and escape rendered values", () => {
  const h = harness(["escapeHtml", "questionChoices"]);
  const render = (question, value, locked = false, diagnostic = false) => h.context.questionChoices(question, value, locked, diagnostic);
  const single = render({ type: "single_choice", options: ['<img onerror="bad">', "B"] }, 0);
  assert.match(single, /data-choose="0" aria-pressed="true"/);
  assert.match(single, /&lt;img onerror=&quot;bad&quot;&gt;/);
  const multi = render({ type: "multi_choice", options: ["A", "B", "C"] }, [0, 2]);
  assert.equal((multi.match(/aria-pressed="true"/g) || []).length, 2);
  assert.match(multi, /可多选/);
  const judge = render({ type: "judge", options: ["not a choice"] }, false, false, true);
  assert.match(judge, /data-diagnostic-choose="false" aria-pressed="true"/);
  assert.match(judge, />正确<|>正确<\/span>/);
  assert.match(judge, />错误<|>错误<\/span>/);
  assert.ok(!judge.includes("not a choice"));
  const fill = render({ type: "fill", options: ["not a choice"] }, '0" onfocus="bad');
  assert.match(fill, /<input type="text" data-answer-input="task"/);
  assert.match(fill, /value="0&quot; onfocus=&quot;bad"/);
  assert.ok(!fill.includes("textarea") && !fill.includes("data-choose"));
  for (const type of ["text", "short_answer", "case"]) {
    const text = render({ type, options: ["not a choice"] }, '</textarea><script>bad()</script>');
    assert.match(text, /<textarea data-answer-input="task"/);
    assert.match(text, /&lt;\/textarea&gt;&lt;script&gt;bad\(\)&lt;\/script&gt;/);
    assert.ok(!text.includes("data-choose"));
  }
  assert.match(render({ options: ["A", "B"] }, 0), /data-choose="0" aria-pressed="true"/, "older questions without type still work");
});

test("fill slots use only bounded public counts, never the protected answer key or chosen array length", () => {
  const h = harness(["escapeHtml", "questionChoices"]);
  const protectedQuestion = (metadata = {}) => Object.defineProperties({ type: "fill", ...metadata }, {
    answer: { get() { throw new Error("protected answer was read"); } },
    explanation: { get() { throw new Error("protected explanation was read"); } },
  });
  for (const metadata of [{ blank_count: 3 }, { answer_format: { blank_count: 3 } }]) {
    const question = protectedQuestion(metadata);
    const markup = h.context.questionChoices(question, ["0", "x^2", "a/b"], false);
    assert.equal((markup.match(/<input\b/g) || []).length, 3);
    assert.match(markup, /data-answer-blank="0" value="0"/);
    assert.match(markup, /data-answer-blank="2" value="a\/b"/);
    assert.equal(h.context.answerMissing(["0", "x^2", "a/b"], question), false);
    assert.equal(h.context.answerMissing(["0", "x^2", " "], question), true);
  }
  for (const metadata of [{}, { answer_format: "text" }, { blank_count: -1 }, { blank_count: 1.5 }, { blank_count: 1000000 }, { answer_format: { blank_count: "3" } }]) {
    const markup = h.context.questionChoices(protectedQuestion(metadata), ["one", "two", "three"], false);
    assert.equal((markup.match(/<input\b/g) || []).length, 1, JSON.stringify(metadata));
  }
  const alternatives = h.context.questionChoices({ type: "fill", answer: ["PRIVATE-ONE", "PRIVATE-TWO"], explanation: "PRIVATE-EXPLANATION" }, "", false);
  assert.equal((alternatives.match(/<input\b/g) || []).length, 1);
  assert.ok(!alternatives.includes("PRIVATE"));
});

test("every question type locks its controls for task and diagnostic rendering", () => {
  const h = harness(["escapeHtml", "questionChoices"]);
  for (const diagnostic of [false, true]) {
    for (const type of ["single_choice", "multi_choice", "judge", "fill", "text", "short_answer", "case"]) {
      const question = { type, options: ["A", "B"], ...(type === "fill" ? { blank_count: 2 } : {}) };
      const markup = h.context.questionChoices(question, undefined, true, diagnostic);
      const controls = [...markup.matchAll(/<(button|input|textarea)\b([^>]*)>/g)];
      assert.ok(controls.length > 0, type);
      for (const [, tag, attributes] of controls) assert.match(attributes, tag === "button" ? /\bdisabled\b/ : /\breadonly\b/, type);
    }
  }
});

test("choice bindings keep boolean false, zero and toggled arrays through repeated page renders", () => {
  for (const diagnostic of [false, true]) {
    const questions = [
      { id: "judge", type: "judge", options: [] },
      { id: "single", type: "single_choice", options: ["A", "B"] },
      { id: "multi", type: "multi_choice", options: ["A", "B", "C"] },
    ];
    const current = task("choices", { questions });
    const h = harness(["escapeHtml", "questionChoices", "bind", "rememberStudentDraft"], {
      state: { liveTask: current, diagnostic: { questions }, answers: ["", "", ""], diagnosticAnswers: ["", "", ""] },
    });
    const answers = diagnostic ? h.state.diagnosticAnswers : h.state.answers;
    const key = diagnostic ? "diagnosticChoose" : "choose";
    const click = (value) => bindQuestion(h, diagnostic).controls.find((node) => node.dataset[key] === String(value)).fire("click");
    click(false);
    assert.equal(answers[0], false);
    assert.match(bindQuestion(h, diagnostic).markup, /choose="false" aria-pressed="true"/);
    click(true);
    assert.equal(answers[0], true);
    click(false);
    assert.equal(answers[0], false);
    h.state.currentQ = 1;
    click(0);
    assert.equal(answers[1], 0);
    h.state.currentQ = 2;
    click(2);
    const previous = answers[2];
    click(0);
    assert.deepEqual(plain(previous), [2], "toggling must not mutate an earlier draft snapshot");
    assert.deepEqual(plain(answers[2]), [0, 2]);
    assert.equal((bindQuestion(h, diagnostic).markup.match(/aria-pressed="true"/g) || []).length, 2);
    click(2);
    click(0);
    assert.deepEqual(plain(answers[2]), []);
    assert.equal(h.context.answerMissing(answers[2], questions[2]), true);
    if (diagnostic) assert.equal(h.state.draftDirty, false);
    else assert.deepEqual(plain(h.state.studentDrafts.choices.answers), [false, 0, []]);
    assert.equal(h.calls.filter(([name]) => name === "render").length, 8);
  }
});

test("fill and textarea input bindings preserve focus by updating only values and progress", () => {
  for (const diagnostic of [false, true]) {
    const questions = [
      { id: "fill", type: "fill", answer_format: { blank_count: 2 } },
      { id: "text", type: "short_answer" },
    ];
    const current = task("inputs", { questions });
    const title = controlNode();
    const bar = controlNode();
    const navigation = [controlNode({ q: "0" }), controlNode({ q: "1" })];
    const status = controlNode();
    const h = harness(["escapeHtml", "questionChoices", "bind", "rememberStudentDraft"], {
      state: { liveTask: current, diagnostic: { questions }, answers: ["", ""], diagnosticAnswers: ["", ""] },
      view: { querySelector: (selector) => ({ "[data-answer-progress]": title, "[data-answer-progress-bar]": bar })[selector] || null },
    });
    h.nodes.set(diagnostic ? '[aria-label="短诊题目"] [data-q]' : '[aria-label="题目导航"] [data-q]', navigation);
    h.nodes.set(".student-draft-state", [status]);
    const answers = diagnostic ? h.state.diagnosticAnswers : h.state.answers;
    const fields = bindQuestion(h, diagnostic).controls;
    fields[0].value = "0";
    fields[0].fire("input");
    const partial = answers[0];
    assert.deepEqual(plain(partial), ["0", ""]);
    fields[1].value = "x^2";
    fields[1].fire("input");
    assert.deepEqual(plain(answers[0]), ["0", "x^2"]);
    assert.deepEqual(plain(partial), ["0", ""], "typing must not mutate a previous multi-blank snapshot");
    assert.equal(navigation[0].getAttribute("aria-label"), "第 1 题，已作答");
    if (diagnostic) assert.equal(navigation[0].textContent, "1 · 已答");
    else {
      assert.equal(title.textContent, "1 / 2 题");
      assert.equal(bar.style.width, "50%");
      assert.equal(status.textContent, "有未保存的作答");
    }
    fields[1].value = " \n ";
    fields[1].fire("input");
    assert.equal(navigation[0].getAttribute("aria-label"), "第 1 题，未作答");
    fields[1].value = "a/b";
    fields[1].fire("input");
    assert.match(bindQuestion(h, diagnostic).markup, /data-answer-blank="1" value="a\/b"/);
    h.state.currentQ = 1;
    const text = bindQuestion(h, diagnostic).controls[0];
    text.value = "First line\nSecond line";
    text.fire("input");
    assert.equal(answers[1], "First line\nSecond line");
    assert.equal(navigation[1].getAttribute("aria-label"), "第 2 题，已作答");
    if (!diagnostic) {
      assert.equal(title.textContent, "2 / 2 题");
      assert.deepEqual(plain(h.state.studentDrafts.inputs.answers), [["0", "a/b"], "First line\nSecond line"]);
    }
    assert.equal(h.calls.some(([name]) => name === "render"), false, "input must not rebuild the focused control");
  }
});

test("answer event handlers ignore disabled, read-only, loading and taken-over controls", () => {
  for (const diagnostic of [false, true]) {
    for (const kind of ["judge", "fill"]) {
      for (const lock of diagnostic ? ["disabled", "loading", ...(kind === "fill" ? ["readOnly"] : [])] : ["disabled", "loading", "takeover", ...(kind === "fill" ? ["readOnly"] : [])]) {
        const questions = [{ id: "locked", type: kind }];
        const h = harness(["escapeHtml", "questionChoices", "bind", "rememberStudentDraft"], {
          state: { liveTask: task("locked", { questions }), diagnostic: { questions }, answers: [""], diagnosticAnswers: [""] },
        });
        const node = bindQuestion(h, diagnostic).controls[0];
        if (lock === "loading") h.state.loading = true;
        else if (lock === "takeover") h.state.banner = "takeover";
        else node[lock] = true;
        node.value = "must not save";
        node.fire(kind === "judge" ? "click" : "input");
        assert.deepEqual(plain(h.state.answers), [""], `${diagnostic}/${kind}/${lock}`);
        assert.deepEqual(plain(h.state.diagnosticAnswers), [""], `${diagnostic}/${kind}/${lock}`);
        assert.equal(h.state.draftDirty, false);
        assert.equal(h.calls.some(([name]) => name === "render"), false);
      }
    }
  }
});

test("student context counts false and zero but excludes blank, empty and partially filled answers", () => {
  const questions = [
    { id: "judge", type: "judge" }, { id: "single", type: "single_choice", options: ["A", "B"] },
    { id: "fill", type: "fill" }, { id: "multi", type: "multi_choice", options: ["A", "B"] },
    { id: "partial", type: "fill", blank_count: 2 }, { id: "complete", type: "fill", blank_count: 2 },
    { id: "text", type: "text" },
  ];
  const current = task("progress", { questions });
  const h = harness(["escapeHtml", "studentContext", "taskTitle", "taskQuestionCount"], {
    state: { liveTask: current, liveTasks: [current], answers: [false, 0, " \n ", [], ["0", ""], ["0", "x+1"], null, "stale extra answer"] },
    globals: { studentDate: () => "", studentIcon: () => "", taskStatus: () => "" },
  });
  const markup = h.context.studentContext();
  assert.match(markup, /data-answer-progress>3 \/ 7 题/);
  assert.match(markup, /data-q="0" aria-label="第 1 题，已作答"/);
  assert.match(markup, /data-q="1" aria-label="第 2 题，已作答"/);
  assert.match(markup, /data-q="4" aria-label="第 5 题，未作答"/);
  assert.match(markup, /data-q="5" aria-label="第 6 题，已作答"/);
});

test("released results format booleans, choices, alternatives and fill slots using the selected attempt", () => {
  const questions = [
    { id: "judge", type: "judge", answer: false },
    { id: "single", type: "single_choice", options: ["Zero", "One"], answer: 0 },
    { id: "multi", type: "multi_choice", options: ["Alpha", "Beta", "Gamma"], answer: [0, 2] },
    { id: "fill", type: "fill", answer: ["x+1", "1+x"] },
    { id: "blanks", type: "fill", blank_count: 2, answer: ["0", "x^2"] },
    { id: "text", type: "case", answer: "<script>reference</script>" },
  ].map((question) => ({ stem: question.id, points: 1, ...question }));
  const first = { id: "first", kind: "first", total_score: 6, max_score: 6, answers: { judge: false, single: 0, multi: [0, 2], fill: "0", blanks: ["0", "x^2"], text: "<img onerror='bad'>" } };
  const current = task("feedback", { status: "results_ready", answer_released: true, questions, attempts: [first, { id: "retry", kind: "retry", answers: { judge: true } }] });
  const h = harness(["escapeHtml", "resultPageLive", "taskTitle"], {
    state: { liveTask: current, selectedAttemptId: "first" },
    globals: { studentHeading: () => "", studentDate: () => "", taskStatus: () => "" },
  });
  const markup = h.context.resultPageLive();
  for (const text of ["你的作答：错误", "参考答案：错误", "你的作答：A. Zero", "你的作答：A. Alpha；C. Gamma", "你的作答：0", "参考答案：x+1 或 1+x", "第 1 空：0；第 2 空：x^2", "你的作答：&lt;img onerror=&#39;bad&#39;&gt;", "参考答案：&lt;script&gt;reference&lt;/script&gt;"]) assert.ok(markup.includes(text), text);
  assert.ok(!markup.includes("<script>") && !markup.includes("<img") && !markup.includes("你的作答：正确"));
});

test("question rendering and unreleased results never read protected answer or explanation fields", () => {
  const questions = ["single_choice", "multi_choice", "judge", "fill", "text", "short_answer", "case"].map((type) => Object.defineProperties({ id: type, type, stem: type, options: ["A", "B"], points: 1 }, {
    answer: { get() { throw new Error("protected answer was read"); } },
    explanation: { get() { throw new Error("protected explanation was read"); } },
  }));
  const current = task("withheld", { questions, answer_released: false, attempts: [{ id: "first", kind: "first", total_score: 0, max_score: 7, answers: { fill: "PRIVATE-STUDENT-ANSWER" } }] });
  const h = harness(["escapeHtml", "questionChoices", "resultPageLive", "taskTitle"], {
    state: { liveTask: current, selectedAttemptId: "first" },
    globals: { studentHeading: () => "", studentDate: () => "", taskStatus: () => "" },
  });
  for (const question of questions) assert.doesNotThrow(() => h.context.questionChoices(question, "", false));
  const markup = h.context.resultPageLive();
  assert.match(markup, /整轮评分完成前/);
  assert.ok(!markup.includes("参考答案") && !markup.includes("PRIVATE-STUDENT-ANSWER"));
});

test("login completion waits for dissolve completion before showing the workspace or fetching private data", async () => {
  const animation = deferred();
  const s = sceneStub({ playDissolve: () => animation.promise });
  const h = harness(["enterWorkspace"], { state: { screen: "entry", role: null }, scene: s.scene });
  const pending = h.context.enterWorkspace("student", "Learner", { live: true, message: "ready" });
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.transition, "departing");
  assert.equal(h.calls.some(([name]) => name === "loadWorkspace"), false);
  assert.equal(s.events.some(([name]) => name === "stop"), false);
  animation.resolve();
  await pending;
  assert.equal(h.state.screen, "app");
  assert.equal(h.state.transition, "");
  assert.equal(h.state.authStatus, "ready");
  assert.equal(h.calls.filter(([name]) => name === "loadWorkspace").length, 1);
  assert.equal(s.events.filter(([name]) => name === "stop").length, 1);
});

test("a canceled entry animation cannot reopen a workspace", async () => {
  const animation = deferred();
  const s = sceneStub({ playDissolve: () => animation.promise });
  const h = harness(["enterWorkspace"], { state: { screen: "entry", role: null }, scene: s.scene });
  const pending = h.context.enterWorkspace("student", "Previous learner", { live: true, message: "ready" });
  h.state.transitionId += 1;
  h.state.role = null;
  h.state.who = "";
  animation.resolve();
  await pending;
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.role, null);
  assert.equal(h.calls.some(([name]) => name === "loadWorkspace" || name === "toast"), false);
});

test("live logout waits for server success, resets the scene, and waits for reassembly", async () => {
  const server = deferred();
  const animation = deferred();
  const s = sceneStub({ playReassemble: () => animation.promise });
  let requests = 0;
  const h = harness(["finishLogout", "onAction"], {
    scene: s.scene,
    state: { liveTask: task("private"), liveTasks: [task("private")], answers: [0] },
    api: { logout: () => { requests += 1; return server.promise; } },
  });
  const event = { currentTarget: { dataset: { action: "logout" } } };
  h.context.onAction(event);
  h.context.onAction(event);
  assert.equal(requests, 1, "repeat clicks cannot duplicate logout");
  assert.equal(h.state.loggingOut, true);
  assert.equal(h.state.screen, "app");
  assert.equal(h.state.liveTask.id, "private");
  assert.equal(s.events.length, 0);
  server.resolve();
  await settle();
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.role, null);
  assert.equal(h.state.reveal, 0);
  assert.equal(h.state.shifted, false);
  assert.equal(h.state.transition, "returning");
  assert.deepEqual(plain(s.events[0]), ["reset", { reveal: 0 }]);
  assert.ok(s.events.some(([name]) => name === "resume"));
  assert.deepEqual(plain(h.state.answers), []);
  assert.equal(h.context.drag, null);
  assert.equal(h.context.touchY, null);
  animation.resolve();
  await settle();
  assert.equal(h.state.transition, "");
  assert.deepEqual(s.events.at(-1), ["setPull", 0]);
});

test("failed server logout leaves the authenticated workspace and answers intact", async () => {
  const s = sceneStub();
  const h = harness(["finishLogout", "onAction"], {
    scene: s.scene,
    state: { liveTask: task("private"), answers: [0] },
    api: { logout: async () => { throw new Error("logout failed"); } },
  });
  h.context.onAction({ currentTarget: { dataset: { action: "logout" } } });
  await settle();
  assert.equal(h.state.screen, "app");
  assert.equal(h.state.role, "student");
  assert.equal(h.state.loggingOut, false);
  assert.deepEqual(plain(h.state.answers), [0]);
  assert.equal(s.events.length, 0);
  assert.ok(h.calls.some(([name, message]) => name === "toast" && message === "logout failed"));
});

test("reduced-motion login and logout use instant scene completion and leave the form open", async () => {
  const s = sceneStub();
  const h = harness(["enterWorkspace", "finishLogout"], { scene: s.scene, state: { screen: "entry", reduce: true } });
  await h.context.enterWorkspace("student", "Learner", { live: true, message: "ready" });
  assert.equal(h.state.screen, "app");
  const dissolve = s.events.find(([name]) => name === "playDissolve");
  assert.deepEqual(dissolve, ["playDissolve", 0]);
  await h.context.finishLogout();
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.reveal, 1);
  assert.equal(h.state.shifted, true);
  assert.equal(h.state.transition, "");
  assert.equal(s.events.some(([name]) => name === "playReassemble"), false);
  assert.ok(s.events.some(([name, value]) => name === "reset" && value.reveal === 1));
});

test("enabling reduced motion while logout reassembles leaves the login form open", async () => {
  const animation = deferred();
  const s = sceneStub({ playReassemble: () => animation.promise });
  const h = harness(["finishLogout", "setReveal"], { scene: s.scene });
  vm.runInContext(sourceSection('reduceQuery.addEventListener("change",', '// ── 引导点'), h.context, { filename: appPath });
  const pending = h.context.finishLogout();
  assert.equal(h.state.transition, "returning");
  h.reduceQuery.matches = true;
  h.mediaListeners.get("change")();
  animation.resolve();
  await pending;
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.transition, "");
  assert.equal(h.state.reveal, 1);
  assert.equal(h.state.shifted, true);
  assert.ok(s.events.some(([name, value]) => name === "reset" && value.reveal === 1));
  assert.deepEqual(s.events.at(-1), ["setPull", 1]);
});

test("a stale logout reassembly completion cannot mutate the next transition", async () => {
  const animation = deferred();
  const s = sceneStub({ playReassemble: () => animation.promise });
  const h = harness(["finishLogout"], { scene: s.scene });
  const pending = h.context.finishLogout();
  h.state.transitionId += 1;
  h.state.transition = "departing";
  const before = h.calls.filter(([name]) => name === "render").length;
  animation.resolve();
  await pending;
  assert.equal(h.state.transition, "departing");
  assert.equal(h.calls.filter(([name]) => name === "render").length, before);
  assert.equal(s.events.some(([name]) => name === "setPull"), false);
});

test("selecting a task opens that ID and preserves local dirty drafts when switching away and back", async () => {
  const a = task("alpha", { draft_answers: { "alpha-q": 1 } });
  const b = task("beta", { draft_answers: { "beta-q": 1 } });
  const requests = [];
  const h = harness(taskFunctions, {
    state: { studentPage: "home", liveTask: a, liveTasks: [a, b] },
    api: {
      task: async (id) => { requests.push(["read", id]); return plain(id === "alpha" ? a : b); },
      acquireLease: async (id) => { requests.push(["lease", id]); return lease(id); },
    },
  });
  await h.context.selectLiveTask("beta");
  assert.equal(h.state.liveTask.id, "beta");
  assert.equal(h.state.studentPage, "workspace");
  assert.ok(requests.some(([name, id]) => name === "lease" && id === "beta"));
  h.state.answers[0] = 0;
  h.context.rememberStudentDraft();
  await h.context.selectLiveTask("alpha");
  assert.deepEqual(plain(h.state.answers), [1], "answers from beta cannot appear on alpha");
  await h.context.selectLiveTask("beta");
  assert.deepEqual(plain(h.state.answers), [0], "beta's unsaved first option overrides its older server draft");
  assert.equal(h.state.draftDirty, true);
  assert.equal(h.state.liveLease.task_id, "beta");
});

test("opening completed tasks shows their result without acquiring an editing lease", async () => {
  const done = task("done", { status: "results_ready" });
  let leases = 0;
  const h = harness(taskFunctions, {
    state: { liveTask: task("other") },
    api: { task: async () => done, acquireLease: async () => { leases += 1; return lease(done.id); } },
  });
  await h.context.selectLiveTask("done");
  assert.equal(h.state.liveTask.id, "done");
  assert.equal(h.state.studentPage, "result");
  assert.equal(leases, 0);
});

test("each learning record opens its own submission, including while a retry is in progress", async () => {
  for (const status of ["results_ready", "in_progress"]) {
    const current = task("history", {
      status, answer_released: status === "results_ready",
      attempts: [
        { id: "first", kind: "first", status: "graded", total_score: 0, max_score: 1, submitted_at: 10 },
        { id: "retry", kind: "retry", status: "graded", total_score: 1, max_score: 1, submitted_at: 20 },
      ],
    });
    let leases = 0;
    const h = harness([...taskFunctions, "escapeHtml", "mine", "resultPageLive", "onAction"], {
      state: { liveTask: current, liveTasks: [current], studentPage: "home" },
      globals: { studentHeading: () => "", studentDate: () => "", studentIcon: () => "", taskStatus: () => "" },
      api: { task: async () => plain(current), acquireLease: async () => { leases += 1; return lease(current.id); } },
    });
    const markup = h.context.mine();
    const buttons = [...markup.matchAll(/<button\b([^>]+)>查看反馈<\/button>/g)];
    assert.equal(buttons.length, 2);
    for (const button of buttons) {
      const dataset = {};
      for (const [, key, value] of button[1].matchAll(/data-([a-z-]+)="([^"]*)"/g)) {
        dataset[key.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase())] = value;
      }
      assert.ok(dataset.attemptId, "every feedback button must identify its submission");
      h.context.onAction({ currentTarget: { dataset } });
      await settle();
      assert.equal(h.state.studentPage, "result", "past feedback must not open the current retry editor");
      const result = h.context.resultPageLive();
      assert.match(result, dataset.attemptId === "first" ? /首次成绩/ : /重做成绩/);
      assert.match(result, new RegExp(`<div class="student-score">${dataset.attemptId === "first" ? 0 : 1}<small>`));
      if (!current.answer_released) assert.ok(!result.includes("参考答案"));
    }
    assert.equal(leases, 0, "reading history must not acquire a draft lease");
  }
});

test("an unavailable historical submission never falls back to someone else's latest record", async () => {
  const current = task("history", { status: "results_ready", attempts: [{ id: "latest", kind: "retry", total_score: 1, max_score: 1 }] });
  const h = harness([...taskFunctions, "resultPageLive"], {
    state: { liveTask: current, liveTasks: [current], studentPage: "home" },
    globals: { studentHeading: () => "", studentDate: () => "", taskStatus: () => "" },
    api: { task: async () => current },
  });
  await h.context.selectLiveTask("history", "removed-attempt");
  assert.equal(h.state.studentPage, "home");
  assert.ok(h.calls.some(([name, message]) => name === "toast" && message.includes("记录")));
  h.state.selectedAttemptId = "removed-attempt";
  assert.ok(!h.context.resultPageLive().includes("重做成绩"));
});

test("reloading the same tab reopens its draft immediately without taking over another device", async () => {
  const storage = new Map();
  const sessionStorage = { getItem: (key) => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value) };
  let owner;
  for (const device of ["first-load", "after-reload"]) {
    const current = task("reload", { draft_answers: { "reload-q": 0 } });
    const h = harness(taskFunctions, {
      state: { liveTask: current, liveTasks: [current], studentPage: "home" },
      window: { sessionStorage },
      globals: { crypto: { randomUUID: () => device } },
      api: {
        acquireLease: async (id, deviceId, options) => {
          assert.ok(!options?.takeover, "refresh cannot force a different device out");
          if (owner && deviceId !== owner) throw new Error("another device currently owns the draft lease");
          owner = deviceId;
          return lease(id);
        },
        task: async () => current,
      },
    });
    await h.context.openLiveTask();
    assert.equal(h.state.studentPage, "workspace", "a reload must recover the same tab's valid lease owner");
    assert.deepEqual(plain(h.state.answers), [0]);
  }
  assert.equal(storage.size, 1, "persist only the anonymous tab identifier, not answers or lease tokens");
});

test("independent tabs get different editing identities and storage failure does not block drafting", async () => {
  const ids = [];
  for (const tab of ["alpha", "beta", "blocked-storage"]) {
    const values = new Map();
    const unavailable = tab === "blocked-storage";
    const current = task(tab);
    const h = harness(taskFunctions, {
      state: { liveTask: current, liveTasks: [current] },
      window: { sessionStorage: {
        getItem: (key) => { if (unavailable) throw new Error("storage disabled"); return values.get(key) ?? null; },
        setItem: (key, value) => { if (unavailable) throw new Error("storage disabled"); values.set(key, value); },
      } },
      globals: { crypto: { randomUUID: () => tab } },
      api: { acquireLease: async (id, deviceId) => { ids.push(deviceId); return lease(id); }, task: async () => current },
    });
    await h.context.openLiveTask();
    assert.equal(h.state.studentPage, "workspace");
  }
  assert.equal(new Set(ids).size, 3);
});

test("retry uses the current task version, clears only that task's draft and preserves first-attempt history", async () => {
  const first = { id: "first-attempt", kind: "first", total_score: 0, max_score: 1 };
  const current = task("retry", { status: "results_ready", allow_retries: true, version: 11, attempts: [first] });
  const reopened = task("retry", { version: 12, attempts: [first] });
  const requests = [];
  const h = harness([...taskFunctions, "retryLiveTask"], {
    state: { liveTask: current, liveTasks: [current], answers: [1], submitted: true, studentPage: "result", studentDrafts: { retry: { answers: [1], dirty: true }, other: { answers: [0], dirty: true } } },
    api: {
      retryTask: async (...args) => { requests.push(args); return reopened; },
      task: async () => plain(reopened),
      acquireLease: async (id) => lease(id),
    },
  });
  await h.context.retryLiveTask();
  assert.equal(requests[0][0], "retry");
  assert.equal(requests[0][1], 11);
  assert.equal(h.state.studentPage, "workspace");
  assert.equal(h.state.submitted, false);
  assert.deepEqual(plain(h.state.answers), [""]);
  assert.equal(h.state.studentDrafts.retry, undefined);
  assert.deepEqual(plain(h.state.studentDrafts.other.answers), [0]);
  assert.equal(h.state.liveTask.attempts[0].total_score, 0);
  assert.equal(h.state.liveTask.attempts[0].id, "first-attempt");
});

test("late task-selection responses cannot repopulate user data after logout", async () => {
  const read = deferred();
  const h = harness([...taskFunctions, "finishLogout"], {
    state: { liveTask: task("old"), liveTasks: [task("old")], reduce: true },
    api: { task: () => read.promise },
  });
  const pending = h.context.selectLiveTask("old");
  await h.context.finishLogout();
  read.resolve(task("old"));
  await pending;
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.liveTask, null);
  assert.deepEqual(plain(h.state.liveTasks), []);
  assert.equal(h.state.loading, false);
});

test("late save and submit continuations cannot write into the logged-out entry", async () => {
  for (const stage of ["save", "submit"]) {
    const current = task("old");
    const gate = deferred();
    let submits = 0;
    const h = harness(["taskTitle", "updateStudentTask", "submitPaper", "finishLogout"], {
      state: { liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [0], reduce: true },
      api: {
        saveDraft: () => stage === "save" ? gate.promise : Promise.resolve({ version: 8 }),
        submitTask: () => { submits += 1; return gate.promise; },
      },
    });
    h.context.submitPaper();
    if (stage === "submit") await settle();
    await h.context.finishLogout();
    gate.resolve({ version: 8 });
    await settle();
    assert.equal(h.state.screen, "entry", stage);
    assert.equal(h.state.liveTask, null, stage);
    assert.equal(h.state.submitted, false, stage);
    assert.equal(h.state.banner, "", stage);
    assert.equal(submits, stage === "save" ? 0 : 1);
  }
});

test("late student list loading cannot restore the previous student's data after logout", async () => {
  const read = deferred();
  const h = harness(["taskTitle", "taskFinished", "loadStudent", "finishLogout"], {
    state: { reduce: true },
    api: { myTasks: () => read.promise, goals: async () => [], courses: async () => [], conversations: async () => [] },
  });
  const pending = h.context.loadStudent();
  await h.context.finishLogout();
  read.resolve([task("private")]);
  await pending;
  assert.equal(h.state.liveTask, null);
  assert.deepEqual(plain(h.state.liveTasks), []);
});

test("logout removes conversation metadata, selected student, and all private workspace caches", async () => {
  const h = harness(["finishLogout"], {
    state: {
      reduce: true, who: "Private user", authAccount: "private-account", openStudent: "private-student-id",
      activeConversation: { id: "private-chat-id", title: "Private conversation title" },
      goalLabel: "Private learning goal", exportName: "Private report title",
      teacherView: "student", conversations: ["Private legacy conversation"], activeChat: 3,
      liveReportFormats: ["private-report-format"],
      liveTask: task("private"), liveTasks: [task("private")], liveGoals: [{ id: "private-goal" }],
      liveStudents: [{ id: "private-student-id" }], liveLease: lease("private"),
      liveConversations: [{ id: "private-chat-id" }], liveMessages: [{ content: "private message" }],
      diagnostic: { id: "private-diagnostic" }, diagnosticAnswers: [0],
      studentDrafts: { private: { answers: [0], dirty: true } }, answers: [0],
      liveReport: { id: "private-report" }, courseId: "private-course", classId: "private-class",
      courseName: "Private course name", className: "Private class name",
    },
  });
  await h.context.finishLogout();
  for (const key of ["who", "authAccount", "courseName", "className", "goalLabel", "exportName"]) {
    assert.ok(!h.state[key], `${key} must not retain the previous user's data`);
  }
  for (const key of ["role", "openStudent", "activeConversation", "liveTask", "liveLease", "diagnostic", "liveReport", "courseId", "classId"]) {
    assert.ok(h.state[key] == null, `${key} must not retain the previous user's data`);
  }
  for (const key of ["liveTasks", "liveGoals", "liveStudents", "liveConversations", "liveMessages", "answers", "diagnosticAnswers", "conversations", "liveReportFormats"]) {
    assert.deepEqual(plain(h.state[key]), [], key);
  }
  assert.deepEqual(plain(h.state.studentDrafts), {});
  assert.equal(h.state.teacherView, "home");
  assert.equal(h.state.activeChat, 0);
});

test("late teacher data cannot restore the previous teacher's private records after logout", async () => {
  for (const pauseAt of ["courses", "members", "evidence"]) {
    const gate = deferred();
    const courses = [{ course: { id: "private-course", name: "Private course" }, classes: [{ id: "private-class", name: "Private class" }] }];
    const members = [{ user_id: "private-student", display_name: "Private student", status: "active" }];
    const goals = [{ id: "private-goal", title: "Private goal" }];
    const evidence = [{ student_id: "private-student", score_units: 0, max_score_units: 100, attempt_id: "private-attempt" }];
    const h = harness(["loadTeacher", "finishLogout"], {
      state: { role: "teacher", reduce: true },
      api: {
        teacherClasses: () => pauseAt === "courses" ? gate.promise : Promise.resolve(courses),
        classMembers: () => pauseAt === "members" ? gate.promise : Promise.resolve(members),
        goals: async () => goals,
        myTasks: async () => [],
        goalEvidence: () => pauseAt === "evidence" ? gate.promise : Promise.resolve(evidence),
      },
    });
    const pending = h.context.loadTeacher();
    await settle();
    await h.context.finishLogout();
    gate.resolve({ courses, members, evidence }[pauseAt]);
    await pending;
    assert.equal(h.state.screen, "entry", pauseAt);
    assert.equal(h.state.courseId, null, `courseId after ${pauseAt}`);
    assert.equal(h.state.classId, null, `classId after ${pauseAt}`);
    assert.deepEqual(plain(h.state.liveGoals), [], `goals after ${pauseAt}`);
    assert.deepEqual(plain(h.state.liveStudents), [], `students after ${pauseAt}`);
  }
});

test("retry does not announce success when opening the editing lease failed", async () => {
  const current = task("retry", { status: "results_ready", allow_retries: true });
  const h = harness([...taskFunctions, "retryLiveTask"], {
    state: { liveTask: current, liveTasks: [current], studentPage: "result" },
    api: {
      retryTask: async () => task("retry", { version: 4 }),
      acquireLease: async () => { throw new Error("lease unavailable"); },
    },
  });
  await h.context.retryLiveTask();
  assert.equal(h.state.liveLease, null);
  assert.notEqual(h.state.studentPage, "workspace");
  const messages = h.calls.filter(([name]) => name === "toast").map(([, message]) => message);
  assert.ok(messages.includes("lease unavailable"));
  assert.ok(!messages.some((message) => message.includes("已开始重做")), "failed opening must not replace the error with a success message");
});

test("a retry awaiting a lease cannot announce success after logout", async () => {
  const gate = deferred();
  const current = task("retry", { status: "results_ready", allow_retries: true });
  const h = harness([...taskFunctions, "retryLiveTask", "finishLogout"], {
    state: { liveTask: current, liveTasks: [current], reduce: true },
    api: { retryTask: async () => task("retry", { version: 4 }), acquireLease: () => gate.promise, task: async () => task("retry", { version: 4 }) },
  });
  const pending = h.context.retryLiveTask();
  await settle();
  await h.context.finishLogout();
  const before = h.calls.filter(([name]) => name === "toast").length;
  gate.resolve(lease("retry"));
  await pending;
  assert.equal(h.state.screen, "entry");
  assert.equal(h.state.liveTask, null);
  assert.equal(h.calls.filter(([name]) => name === "toast").length, before, "a canceled retry must not report success on the login screen");
});

test("conversation records display escaped content and honor redaction without exposing withheld text", async () => {
  const h = harness(["escapeHtml", "readConversation", "sheet"], {
    state: { liveConversations: [{ id: "chat-1", title: '<img src=x onerror="bad">' }] },
    api: { conversationMessages: async () => [
      { role: "user", content: '<script>bad()</script>&"' },
      { role: "assistant", content: "WITHHELD-PRIVATE-TEXT", redacted: true, notice: "内容暂未开放" },
    ] },
  });
  await h.context.readConversation("chat-1");
  assert.equal(h.state.sheet, "conversation");
  const markup = h.context.sheet();
  assert.match(markup, /role="dialog"/);
  assert.match(markup, /aria-modal="true"/);
  assert.match(markup, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
  assert.match(markup, /&lt;script&gt;bad\(\)&lt;\/script&gt;&amp;&quot;/);
  assert.match(markup, /内容暂未开放/);
  assert.ok(!markup.includes("<script>"));
  assert.ok(!markup.includes("WITHHELD-PRIVATE-TEXT"));
});

test("late conversation messages cannot reopen a private sheet after logout", async () => {
  const gate = deferred();
  const h = harness(["readConversation", "finishLogout"], {
    state: { reduce: true, liveConversations: [{ id: "private-chat" }] },
    api: { conversationMessages: () => gate.promise },
  });
  const pending = h.context.readConversation("private-chat");
  await h.context.finishLogout();
  gate.resolve([{ role: "user", content: "private message" }]);
  await pending;
  assert.equal(h.state.sheet, null);
  assert.equal(h.state.activeConversation, null);
  assert.deepEqual(plain(h.state.liveMessages), []);
});

test("API logout preserves its user until the server succeeds and sends a same-origin request", async () => {
  const gate = deferred();
  let request;
  const api = apiHarness((...args) => { request = args; return gate.promise; });
  api.state.user = { id: "current-user" };
  const pending = api.logout();
  assert.equal(api.state.user.id, "current-user");
  assert.equal(request[0], "/api/v1/auth/logout");
  assert.equal(request[1].method, "POST");
  assert.equal(request[1].credentials, "same-origin");
  assert.equal(request[1].headers.Origin, "https://example.invalid");
  gate.resolve(response(204));
  await pending;
  assert.equal(api.state.user, null);
});

test("API logout failures retain its current user", async () => {
  const api = apiHarness(async () => response(503, { detail: { code: "unavailable", message: "Try later", retryable: true } }));
  api.state.user = { id: "current-user" };
  await assert.rejects(api.logout(), (error) => error.code === "unavailable" && error.retryable);
  assert.equal(api.state.user.id, "current-user");
});

test("API draft and retry wrappers preserve numeric answers, versions and encoded task IDs", async () => {
  const requests = [];
  const api = apiHarness(async (...args) => { requests.push(args); return response(200, { version: 10 }); });
  const editLease = lease("task / one");
  await api.saveDraft("task / one", 7, editLease, { question: 0 }, "save-request");
  await api.retryTask("task / one", 10, "retry-request");
  assert.equal(requests[0][0], "/api/v1/tasks/task%20%2F%20one/draft");
  const saved = JSON.parse(requests[0][1].body);
  assert.equal(saved.answers.question, 0);
  assert.equal(saved.expected_version, 7);
  assert.equal(saved.lease_token, editLease.lease_token);
  assert.equal(saved.fence_token, editLease.fence_token);
  assert.equal(requests[1][0], "/api/v1/tasks/task%20%2F%20one/retry");
  assert.deepEqual(JSON.parse(requests[1][1].body), { expected_version: 10, request_id: "retry-request" });
});

test("changing to reduced motion opens entry without starting a stopped workspace renderer", () => {
  for (const screen of ["entry", "app"]) {
    const s = sceneStub();
    const h = harness(["setReveal"], { scene: s.scene, state: { screen, reveal: 0, shifted: false } });
    vm.runInContext(sourceSection('reduceQuery.addEventListener("change",', '// ── 引导点'), h.context, { filename: appPath });
    h.reduceQuery.matches = true;
    h.mediaListeners.get("change")();
    assert.equal(h.state.reduce, true);
    assert.equal(h.context.drag, null);
    assert.equal(h.context.touchY, null);
    assert.deepEqual(s.events[0], ["setReducedMotion", true]);
    if (screen === "entry") {
      assert.equal(h.state.reveal, 1);
      assert.ok(s.events.some(([name, value]) => name === "reset" && value.reveal === 1));
    } else {
      assert.equal(s.events.some(([name]) => name === "reset" || name === "resume" || name === "start"), false);
    }
  }
});

test("mountScene reuses an existing renderer and does not retry a known WebGL fallback", () => {
  for (const fallback of [false, true]) {
    const s = sceneStub();
    let creates = 0;
    const h = harness(["mountScene"], {
      scene: fallback ? null : s.scene,
      state: { fallback },
      globals: { document: { querySelector: () => ({}) }, createScene: () => { creates += 1; return s.scene; } },
    });
    h.context.mountScene();
    assert.equal(creates, 0);
    assert.equal(s.events.length, 0);
  }
});
