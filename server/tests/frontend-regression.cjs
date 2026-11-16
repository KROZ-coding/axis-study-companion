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
    activeConversationId: null, activeConversation: null, chatMessages: [], chatDrafts: {}, chatContexts: {},
    chatDraft: "", chatContext: null, chatPolicy: null, chatPolicyLoading: false, chatPolicyError: "",
    chatResponseError: "", chatOlderCursor: null, chatHasOlder: false, chatLoadingOlder: false,
    chatPendingSends: {}, pendingChatCreate: null, chatCreating: false,
    chatRenameOpen: false, chatRenameTitle: "", chatRenameError: "", chatRenaming: false, chatArchived: false,
    chatHasMoreConversations: false, chatSending: false,
    studentDrafts: {}, draftDirty: false, savedAt: null, selectedAttemptId: null,
    diagnostic: null, diagnosticAnswers: [], mode: "tutor", sheet: null,
    conversations: [], activeChat: 0, railEvents: [],
    ...options.state,
  };
  const view = {
    isConnected: true,
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
    chatEpoch: 0, chatListSequence: 0, chatPostSequence: 0,
    chatStreamClosers: new Map(), chatReadControllers: new Set(), chatComposing: false, chatRenderPending: false,
    AbortController,
    drag: { moved: true }, touchY: 100,
    crypto: { randomUUID: () => `test-id-${++serial}` },
    render: record("render"), toast: record("toast"),
    onAction: record("onAction"), onSubmit: record("onSubmit"),
    teacherLibrary: { bind: record("teacherLibrary.bind"), reset: record("teacherLibrary.reset") },
    teacherComposer: { bind: record("teacherComposer.bind"), reset: record("teacherComposer.reset") },
    adminWorkspace: { bind: record("adminWorkspace.bind"), reset: record("adminWorkspace.reset"), load: record("adminWorkspace.load"), render: () => '<section data-admin-workspace>管理页面</section>' },
    teacherManagement: { bind: record("teacherManagement.bind"), reset: record("teacherManagement.reset"), leave: () => { calls.push(["teacherManagement.leave"]); return true; }, load: record("teacherManagement.load"), render: () => '<section data-teacher-management>班级管理</section>' },
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
  vm.runInContext([...new Set([...answerFunctions, "chatRoute", "setChatRoute", "closeStudentChatStream", ...names])].map(functionSource).join("\n"), context, { filename: appPath });
  return { context, state, calls, intervals, frames, nodes, mediaListeners, reduceQuery };
}

const taskFunctions = ["taskFinished", "taskTitle", "updateStudentTask", "rememberStudentDraft", "startLeaseRenewal", "openLiveTask", "selectLiveTask"];
const chatFunctions = ["closeChatResponse", "chatResponseById", "chatResponsePaused", "chatResponseStatus", "chatResponseControls", "actOnChatResponse", "updateChatResponse", "connectChatResponse", "renderStudentChatIfActive", "mergeChatMessages", "syncConversationHistory", "loadConversation", "loadConversationList", "loadOlderChatMessages", "refreshCompanionPolicy", "readConversation", "chatConversation", "renameStudentConversation"];
const chatSendFunctions = [...chatFunctions, "newChatRequest", "prepareChatMessage", "recordChatHelp", "sendStudentMessage", "sendContextChat"];

function chatHarness(names = [], options = {}) {
  return harness([...chatSendFunctions, ...names], {
    ...options,
    state: { studentPage: "chat", studentTab: "companion", activeConversationId: "chat-a", ...options.state },
  });
}

function chatMessage(id, response = {}, overrides = {}) {
  return {
    id, role: "user", text: `Question ${id}`, created_at: 1,
    response: { response_id: `response-${id}`, status: "completed", text: "", generation: 1, last_event_id: 0, ...response },
    ...overrides,
  };
}

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

function apiHarness(fetch, globals = {}) {
  const context = vm.createContext({
    fetch, location: { protocol: "https:", origin: "https://example.invalid" }, URLSearchParams,
    ...globals,
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
    fire(event, properties = {}) { return listeners.get(event)?.({ currentTarget: this, ...properties }); },
    ...properties,
  };
}

function chatDom(responseId = "response-one") {
  const document = { activeElement: null, querySelector: () => null };
  let input, thread, body, status, responseRoot;
  let renders = 0, submits = 0;
  const replaceNodes = (height) => {
    input = controlNode({}, {
      value: "Draft being edited", selectionStart: 3, selectionEnd: 8, scrollTop: 19,
      form: { requestSubmit: () => { submits += 1; } },
      focus() { document.activeElement = this; },
      setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; },
    });
    thread = { scrollTop: 120, scrollHeight: height, clientHeight: 100 };
    body = { textContent: "", set innerHTML(value) { assert.fail(`Response body received HTML: ${value}`); } };
    status = { textContent: "" };
    responseRoot = {
      dataset: { responseId }, classList: { toggle() {} },
      querySelector: (selector) => selector === "[data-response-text]" ? body : selector === "[data-response-status]" ? status : null,
      set innerHTML(value) { assert.fail(`Response root received HTML: ${value}`); },
    };
  };
  replaceNodes(1000);
  return {
    document,
    view: {
      querySelector: (selector) => selector === "[data-chat-input]" ? input : selector === "[data-chat-thread]" ? thread : null,
      querySelectorAll: (selector) => selector === "[data-response-id]" ? [responseRoot] : selector === "[data-chat-input]" ? [input] : [],
    },
    render() { renders += 1; document.activeElement = null; replaceNodes(1200); },
    get input() { return input; }, get thread() { return thread; }, get body() { return body; },
    get renders() { return renders; }, get submits() { return submits; },
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
  assert.equal(await h.context.saveLiveDraft(), true);
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

test("reopening a task uses the server draft unless a genuinely dirty local draft exists", async () => {
  const current = task("draft-source", { questions: [{ id: "first", stem: "First", options: ["A", "B"] }, { id: "second", stem: "Second", options: ["A", "B"] }], draft_answers: { first: 1 } });
  const h = harness(taskFunctions, {
    state: { liveTask: current, liveTasks: [current], studentDrafts: { "draft-source": { answers: [0, 1], dirty: false } } },
    api: { task: async () => plain(current), acquireLease: async () => lease(current.id) },
  });
  await h.context.openLiveTask();
  assert.deepEqual(plain(h.state.answers), [1, ""], "a clean cache must not shadow the authoritative server draft");
  h.state.studentDrafts[current.id] = { answers: [0, "local partial"], dirty: true };
  await h.context.openLiveTask();
  assert.deepEqual(plain(h.state.answers), [0, "local partial"], "an unsaved local draft remains available after reopening");
  assert.equal(h.state.draftDirty, true);
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

test("admin workspace loads only management data and renders a dedicated role boundary", async () => {
  const h = harness(["loadWorkspace", "renderAdmin", "escapeHtml"], {
    state: { role: "admin", who: "<Admin>", live: true },
    globals: { loadTeacher: () => assert.fail("admin fetched teacher learning data"), loadStudent: () => assert.fail("admin fetched student learning data") },
  });
  await h.context.loadWorkspace();
  assert.equal(h.calls.filter(([name]) => name === "adminWorkspace.load").length, 1);
  h.context.renderAdmin();
  assert.match(h.context.view.innerHTML, /data-admin-workspace/);
  assert.match(h.context.view.innerHTML, /&lt;Admin&gt;/);
  assert.doesNotMatch(h.context.view.innerHTML, /data-teacher-management|教师审核与账号管理尚未接入/);
});

test("management navigation clears one-time-code state when leaving and loads when entering", async () => {
  const h = harness(["bind"], { state: { role: "teacher", teacherView: "management" } });
  const home = controlNode({ view: "home" });
  const management = controlNode({ view: "management" });
  h.nodes.set("[data-view]", [home, management]);
  h.context.bind();
  home.fire("click");
  assert.equal(h.state.teacherView, "home");
  assert.equal(h.calls.filter(([name]) => name === "teacherManagement.leave").length, 1);
  assert.equal(h.calls.filter(([name]) => name === "teacherManagement.reset").length, 0);
  management.fire("click");
  assert.equal(h.state.teacherView, "management");
  assert.equal(h.calls.filter(([name]) => name === "teacherManagement.load").length, 1);
  h.state.loggingOut = true;
  home.fire("click");
  assert.equal(h.state.teacherView, "management");
});

test("management navigation keeps the current view when a write result is unresolved", () => {
  const h = harness(["bind"], { state: { role: "teacher", teacherView: "management" } });
  const home = controlNode({ view: "home" });
  h.nodes.set("[data-view]", [home]);
  h.context.teacherManagement.leave = () => false;
  h.context.bind();
  const renders = h.calls.filter(([name]) => name === "render").length;
  home.fire("click");
  assert.equal(h.state.teacherView, "management");
  assert.equal(h.calls.filter(([name]) => name === "render").length, renders);
  assert.equal(h.calls.filter(([name]) => name === "teacherManagement.reset").length, 0);
  h.context.teacherManagement.leave = () => true;
  home.fire("click");
  assert.equal(h.state.teacherView, "home");
});

test("empty course and empty class states clear prior records and offer management", async () => {
  for (const courses of [[], [{ course: { id: "new-course", name: "New course" }, classes: [] }]]) {
    const h = harness(["loadTeacher", "teacherHomeLive", "escapeHtml"], {
      state: { role: "teacher", courseId: "old-course", classId: "old-class", liveStudents: [{ id: "private" }], liveGoals: [{ id: "old" }], openStudent: "private" },
      api: { teacherClasses: async () => courses, classMembers: () => assert.fail("no class to read") },
    });
    await h.context.loadTeacher();
    assert.equal(h.state.classId, null);
    assert.equal(h.state.openStudent, null);
    assert.deepEqual(plain(h.state.liveStudents), []);
    assert.deepEqual(plain(h.state.liveGoals), []);
    assert.match(h.context.teacherHomeLive(), /data-view="management"/);
  }
});

test("teacher course list keeps empty courses and independently authorized classes", async () => {
  const requests = [];
  const api = apiHarness(async (url) => {
    requests.push(url);
    if (url === "/api/v1/courses") return response(200, [{ id: "empty", name: "No classes", can_create_classes: true }]);
    if (url === "/api/v1/classes") return response(200, [{ id: "collaborative", course_id: "hidden-course", name: "Shared class", can_manage: false }]);
    assert.fail(`Unexpected request ${url}`);
  });
  const result = plain(await api.teacherClasses());
  assert.equal(result[0].course.id, "empty");
  assert.deepEqual(result[0].classes, []);
  assert.equal(result[1].classes[0].id, "collaborative");
  assert.equal(result[1].course.can_manage_materials, false);
  assert.equal(result[1].course.can_create_classes, false);
  assert.deepEqual(requests.sort(), ["/api/v1/classes", "/api/v1/courses"]);
});

test("teacher class-list failures stay errors instead of masquerading as an empty school", async () => {
  const api = apiHarness(async (url) => url.endsWith("/classes") ? response(503, { detail: { code: "temporarily_unavailable", message: "read failed" } }) : response(200, []));
  await assert.rejects(api.teacherClasses(), (error) => error.status === 503);
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

test("conversation records display escaped content and honor redaction without duplicating assistant bubbles", async () => {
  const conversation = { id: "chat-1", title: '<img src=x onerror="bad">' };
  const h = harness([...chatFunctions, "escapeHtml", "chatConversation", "companionPolicyLabel", "questionQuestionVersion"], {
    state: { liveConversations: [conversation] },
    globals: { studentHeading: (title) => `<h1>${String(title).replaceAll("<", "&lt;").replaceAll(">", "&gt;").replaceAll('"', "&quot;")}</h1>`, studentIcon: () => "" },
    api: {
      getConversation: async () => conversation,
      companionAnswerPolicy: async () => ({ allow_generation: false }),
      conversationMessages: async () => [
        { id: "user-1", role: "user", text: '<script>bad()</script>&"', response: { response_id: "response-1", status: "completed", text: "WITHHELD-PRIVATE-TEXT", redacted: true, notice: "内容暂未开放" } },
        { id: "assistant-1", role: "assistant", text: "WITHHELD-PRIVATE-TEXT", redacted: true, notice: "内容暂未开放" },
      ],
    },
  });
  await h.context.readConversation("chat-1");
  assert.equal(h.state.studentPage, "chat");
  assert.equal(h.state.activeConversationId, "chat-1");
  const markup = h.context.chatConversation();
  assert.match(markup, /&lt;img src=x onerror=&quot;bad&quot;&gt;/);
  assert.match(markup, /&lt;script&gt;bad\(\)&lt;\/script&gt;&amp;&quot;/);
  assert.match(markup, /内容暂未开放/);
  assert.equal([...markup.matchAll(/class="student-message assistant/g)].length, 1);
  assert.ok(!markup.includes("<script>"));
  assert.ok(!markup.includes("WITHHELD-PRIVATE-TEXT"));
});

test("late conversation reads are aborted and cannot restore private messages after logout", async () => {
  const gate = deferred();
  let signal;
  const h = harness([...chatFunctions, "finishLogout"], {
    state: { reduce: true, liveConversations: [{ id: "private-chat" }] },
    api: {
      getConversation: async () => ({ id: "private-chat" }),
      conversationMessages: (id, options) => { signal = options.signal; return gate.promise; },
    },
  });
  const pending = h.context.readConversation("private-chat");
  await h.context.finishLogout();
  assert.equal(signal.aborted, true);
  gate.resolve([{ id: "private-user", role: "user", text: "private message" }]);
  await pending;
  assert.equal(h.state.activeConversationId, null);
  assert.equal(h.state.activeConversation, null);
  assert.deepEqual(plain(h.state.liveMessages), []);
  assert.deepEqual(plain(h.state.chatMessages), []);
});

test("an uncertain message POST retries its captured text and task without duplicating the optimistic row", async () => {
  const retry = deferred();
  const posts = [], policies = [];
  const originalText = "My first attempt\nPlease check the next step";
  const h = chatHarness([], {
    state: { chatDraft: `  ${originalText}  `, chatContext: { taskId: "task-a", questionVersionId: "question-a:3", questionId: "question-a" } },
    api: {
      companionAnswerPolicy: async (scope) => { policies.push(scope); return { help_recordable: false }; },
      postMessage: (...args) => {
        posts.push(plain(args));
        return posts.length === 1 ? Promise.reject(new Error("POST result unknown")) : retry.promise;
      },
    },
  });
  await h.context.sendStudentMessage();
  const pending = h.state.chatPendingSends["chat-a"];
  assert.ok(pending);
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].localId, pending.requestId);
  assert.equal(h.state.chatMessages[0].text, originalText);
  assert.equal(h.state.chatSending, false);
  assert.match(h.state.chatResponseError, /unknown/);

  h.state.chatDraft = "A later, different draft";
  h.state.chatContext = { taskId: "task-b", questionVersionId: "question-b:9" };
  const sending = h.context.sendStudentMessage();
  await settle();
  assert.equal(h.state.chatMessages.length, 1, "retry must reuse the existing optimistic row");
  assert.deepEqual(posts[1], posts[0]);
  assert.deepEqual(posts[0], ["chat-a", originalText, { taskId: "task-a", questionVersionId: "question-a:3", requestId: pending.requestId }]);
  assert.deepEqual(policies.map(({ taskId, questionVersionId }) => [taskId, questionVersionId]), [["task-a", "question-a:3"], ["task-a", "question-a:3"]]);
  retry.resolve({ message_id: "accepted-user", response_id: "accepted-response", status: "completed" });
  await sending;
  assert.equal(h.state.chatPendingSends["chat-a"], undefined);
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].id, "accepted-user");
  assert.equal(h.state.chatMessages[0].client_request_id, pending.requestId);
  assert.equal(h.state.chatMessages[0].localId, undefined);
});

test("pending sends stay with their conversations and successful retries clean up only that conversation", async () => {
  const posts = [];
  const attempts = new Map();
  const h = chatHarness([], {
    state: { chatDraft: "Draft A", chatDrafts: { "chat-a": "Draft A", "chat-b": "Draft B" } },
    api: {
      companionAnswerPolicy: async () => ({ help_recordable: false }),
      getConversation: async (id) => ({ id }), conversationMessages: async () => [],
      postMessage: async (id, text, options) => {
        posts.push(plain([id, text, options]));
        const count = (attempts.get(id) || 0) + 1;
        attempts.set(id, count);
        if (count === 1) throw new Error("unknown result");
        return { message_id: `accepted-${id}`, response_id: `response-${id}`, status: "completed" };
      },
    },
  });
  await h.context.sendStudentMessage();
  const a = h.state.chatPendingSends["chat-a"];
  await h.context.loadConversation("chat-b");
  assert.equal(h.state.chatMessages.length, 0, "A's optimistic row cannot appear in B");
  assert.equal(h.state.chatDraft, "Draft B");
  await h.context.sendStudentMessage();
  const b = h.state.chatPendingSends["chat-b"];
  assert.notEqual(a.requestId, b.requestId);
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].localId, b.requestId);
  await h.context.sendStudentMessage();
  assert.equal(h.state.chatPendingSends["chat-b"], undefined);
  assert.equal(h.state.chatPendingSends["chat-a"], a);
  assert.equal(h.state.chatMessages[0].id, "accepted-chat-b");
  await h.context.loadConversation("chat-a");
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].localId, a.requestId);
  assert.equal(h.state.chatMessages[0].text, "Draft A");
  await h.context.sendStudentMessage();
  assert.deepEqual(plain(h.state.chatPendingSends), {});
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].id, "accepted-chat-a");
  assert.deepEqual(posts[0], posts[3]);
  assert.deepEqual(posts[1], posts[2]);
});

test("history reconciles client_request_id rather than consuming an identical earlier question", async () => {
  let messages = [chatMessage("earlier", {}, { text: "Same question", client_request_id: "another-request" })];
  const h = chatHarness([], {
    api: {
      conversationMessages: async () => messages, getConversation: async (id) => ({ id }),
      companionAnswerPolicy: async () => ({ help_recordable: false }),
    },
  });
  const pending = h.context.newChatRequest("Same question", "chat-a");
  h.state.chatPendingSends["chat-a"] = pending;
  await h.context.loadConversation("chat-a");
  assert.equal(h.state.chatPendingSends["chat-a"], pending, "text equality is not a receipt");
  assert.equal(h.state.chatMessages.length, 2);
  assert.equal(h.state.chatMessages.filter((row) => row.localId === pending.requestId).length, 1);
  messages = [messages[0], chatMessage("accepted", {}, { text: "Same question", client_request_id: pending.requestId, created_at: pending.createdAt })];
  await h.context.syncConversationHistory("chat-a");
  assert.equal(h.state.chatPendingSends["chat-a"], undefined);
  assert.equal(h.state.chatMessages.length, 2, "receipt replaces the optimistic row, not the earlier message");
  assert.deepEqual(plain(h.state.chatMessages.map((row) => row.id)), ["earlier", "accepted"]);
  assert.ok(h.state.chatMessages.every((row) => !row.localId));
  for (const draft of ["Same question", "A newer unsent draft"]) {
    const next = h.context.newChatRequest("Same question", "chat-a");
    h.state.chatPendingSends["chat-a"] = next;
    h.state.chatDraft = draft;
    h.state.chatDrafts["chat-a"] = draft;
    h.state.chatDrafts["other-chat"] = "Other conversation draft";
    h.context.mergeChatMessages([]);
    h.context.mergeChatMessages([chatMessage(`confirmed-${next.requestId}`, {}, { text: "Same question", client_request_id: next.requestId })]);
    assert.equal(h.state.chatPendingSends["chat-a"], undefined);
    assert.equal(h.state.chatDraft, draft === next.text ? "" : draft);
    assert.equal(h.state.chatDrafts["chat-a"], draft === next.text ? "" : draft);
    assert.equal(h.state.chatDrafts["other-chat"], "Other conversation draft");
  }
});

test("history merging never rolls response generation or cursor backward and a redacted snapshot clears text", () => {
  let handlers;
  const h = chatHarness([], {
    state: { chatMessages: [chatMessage("one", { status: "streaming", generation: 2, last_event_id: 20, text: "newest text" })] },
    api: { openResponseStream: (id, callbacks) => { handlers = callbacks; return () => {}; } },
  });
  h.context.mergeChatMessages([chatMessage("one", { generation: 1, last_event_id: 100, text: "old generation" })]);
  h.context.mergeChatMessages([chatMessage("one", { generation: 2, last_event_id: 19, text: "old cursor" })]);
  assert.equal(h.state.chatMessages[0].response.text, "newest text");
  assert.equal(h.state.chatMessages[0].response.last_event_id, 20);
  h.context.mergeChatMessages([chatMessage("one", { status: "streaming", generation: 3, last_event_id: 1, text: "new generation" })]);
  assert.equal(h.state.chatMessages[0].response.text, "new generation");
  assert.equal(h.state.chatMessages[0].response.generation, 3);
  h.context.connectChatResponse("response-one", 1);
  handlers.snapshot({ generation: 2, last_event_id: 0, status: "completed", text: "WITHHELD", redacted: true, notice: "范围已收起" }, { id: 0, generation: 2 });
  assert.equal(h.state.chatMessages[0].response.redacted, true);
  assert.equal(h.state.chatMessages[0].response.text, "", "authoritative redaction must erase a previously streamed body");
  assert.equal(h.state.chatMessages[0].response.notice, "范围已收起");
});

test("loading history resumes each pending or streaming response with its own cursor and terminal cleanup", async () => {
  const streams = new Map();
  const messages = [
    chatMessage("queued", { status: "pending", last_event_id: 3 }),
    chatMessage("live", { status: "streaming", generation: 2, last_event_id: 17, text: "Existing prefix" }),
    chatMessage("done", { status: "completed", last_event_id: 8 }),
  ];
  const h = chatHarness([], {
    api: {
      getConversation: async (id) => ({ id }), conversationMessages: async () => messages,
      companionAnswerPolicy: async () => ({ help_recordable: false }),
      openResponseStream: (id, handlers, options) => {
        const stream = { handlers, options, closes: 0 };
        streams.set(id, stream);
        return () => { stream.closes += 1; };
      },
    },
  });
  await h.context.loadConversation("chat-a");
  assert.deepEqual([...streams.keys()], ["response-queued", "response-live"]);
  const queued = streams.get("response-queued"), live = streams.get("response-live");
  assert.deepEqual(plain(queued.options), { after: 3, generation: 1, text: "" });
  assert.deepEqual(plain(live.options), { after: 17, generation: 2, text: "Existing prefix" });
  h.context.connectChatResponse("response-live", 0);
  assert.equal(streams.get("response-live"), live, "an already-connected response cannot open a duplicate stream");
  queued.handlers.delta({ text: "Queued answer" }, { id: 4, generation: 1 });
  live.handlers.delta({ text: " + next" }, { id: 18, generation: 2 });
  assert.equal(h.context.chatResponseById("response-queued").text, "Queued answer");
  assert.equal(h.context.chatResponseById("response-live").text, "Existing prefix + next");
  messages[0] = chatMessage("queued", { status: "completed", last_event_id: 5, text: "Queued answer" });
  queued.handlers.end(messages[0].response, { id: 5, generation: 1 });
  queued.handlers.onClosed();
  assert.equal(queued.closes, 1);
  assert.equal(live.closes, 0);
  assert.equal(h.context.chatStreamClosers.has("response-queued"), false);
  assert.equal(h.context.chatStreamClosers.has("response-live"), true);
  await settle();
  h.context.closeStudentChatStream();
  assert.equal(live.closes, 1);
  assert.equal(h.context.chatStreamClosers.size, 0);
});

test("synchronous terminal and close callbacks cannot leak a stream or remove its replacement handle", async () => {
  for (const callback of ["end", "onClosed"]) {
    const streams = [];
    const h = chatHarness([], {
      state: { chatMessages: [chatMessage("one", { status: "streaming", text: "Prefix", last_event_id: 4 })] },
      api: {
        conversationMessages: async () => [],
        openResponseStream: (id, handlers) => {
          const stream = { handlers, closes: 0 };
          streams.push(stream);
          if (streams.length === 1) {
            if (callback === "end") handlers.end({ status: "completed", text: "Final", last_event_id: 5 }, { id: 5, generation: 1 });
            else handlers.onClosed();
          }
          return () => { stream.closes += 1; };
        },
      },
    });
    h.context.connectChatResponse("response-one", 4);
    assert.equal(streams[0].closes, 1, `${callback}: close returned after a synchronous callback must still run`);
    assert.equal(h.context.chatStreamClosers.size, 0, callback);
    await settle();
    h.context.connectChatResponse("response-one", 5);
    assert.equal(streams.length, 2, callback);
    const replacement = h.context.chatStreamClosers.get("response-one");
    streams[0].handlers.onClosed();
    streams[0].handlers.end({ status: "completed", text: "Late old final" }, { id: 100, generation: 1 });
    assert.equal(h.context.chatStreamClosers.get("response-one"), replacement, `${callback}: old callbacks must not delete the new handle`);
    assert.equal(streams[1].closes, 0, callback);
    streams[1].handlers.delta({ text: " Fresh" }, { id: 6, generation: 1 });
    assert.ok(h.context.chatResponseById("response-one").text.endsWith(" Fresh"), callback);
    assert.ok(!h.context.chatResponseById("response-one").text.includes("Late old final"), callback);
    h.context.closeStudentChatStream();
    assert.equal(streams[1].closes, 1, callback);
    assert.equal(h.context.chatStreamClosers.size, 0, callback);
  }
});

test("leaving chat or logging out aborts every read and ignores late streams, history and conversation lists", async () => {
  for (const exit of ["navigation", "logout"]) {
    const history = deferred(), list = deferred();
    const signals = [];
    let handlers, closes = 0;
    const h = chatHarness(["bind", "finishLogout"], {
      state: { reduce: true, chatMessages: [chatMessage("private", { status: "streaming", text: "Private prefix" })] },
      api: {
        conversationMessages: (id, options) => { signals.push(options.signal); return history.promise; },
        conversations: (options) => { signals.push(options.signal); return list.promise; },
        openResponseStream: (id, callbacks) => { handlers = callbacks; return () => { closes += 1; }; },
      },
    });
    h.context.connectChatResponse("response-private");
    const readingHistory = h.context.syncConversationHistory("chat-a");
    const readingList = h.context.loadConversationList();
    if (exit === "navigation") {
      const tab = controlNode({ tab: "tasks" });
      h.nodes.set("[data-tab]", [tab]);
      h.context.bind();
      tab.fire("click");
      assert.equal(h.state.studentTab, "tasks");
      assert.equal(h.state.studentPage, "home");
    } else await h.context.finishLogout();
    assert.equal(signals.length, 2, exit);
    assert.ok(signals.every((signal) => signal.aborted), exit);
    assert.equal(h.context.chatReadControllers.size, 0, exit);
    assert.equal(h.context.chatStreamClosers.size, 0, exit);
    assert.equal(closes, 1, exit);
    const afterExit = plain(h.state);
    const renderCount = h.calls.filter(([name]) => name === "render").length;
    handlers.delta({ text: "PRIVATE LATE DELTA" }, { id: 50, generation: 1 });
    handlers.snapshot({ text: "PRIVATE LATE SNAPSHOT", last_event_id: 51 }, { id: 51, generation: 1 });
    handlers.end({ text: "PRIVATE LATE FINAL", status: "completed" }, { id: 52, generation: 1 });
    handlers.error(new Error("late stream error"));
    handlers.revoked();
    handlers.onClosed();
    history.resolve([chatMessage("late-private", { text: "PRIVATE LATE HISTORY" })]);
    list.resolve([{ id: "private-list", title: "PRIVATE LATE TITLE" }]);
    await Promise.all([readingHistory, readingList]);
    assert.deepEqual(plain(h.state), afterExit, `${exit}: ignored late callbacks must not mutate the next page`);
    assert.equal(h.calls.filter(([name]) => name === "render").length, renderCount, exit);
    assert.equal(closes, 1, exit);
  }
});

test("late POST receipts cannot clear the next draft or reopen chat after navigation or logout", async () => {
  for (const exit of ["navigation", "logout"]) {
    const receipt = deferred();
    let posts = 0, openedStreams = 0;
    const h = chatHarness(["bind", "finishLogout"], {
      state: { reduce: true, chatDraft: "Original pending draft" },
      api: {
        companionAnswerPolicy: async () => ({ help_recordable: false }),
        postMessage: () => { posts += 1; return receipt.promise; },
        openResponseStream: () => { openedStreams += 1; return () => {}; },
      },
    });
    const sending = h.context.sendStudentMessage();
    await settle();
    assert.equal(posts, 1, exit);
    if (exit === "navigation") {
      const tab = controlNode({ tab: "tasks" });
      h.nodes.set("[data-tab]", [tab]);
      h.context.bind();
      tab.fire("click");
      h.state.chatDraft = "Next page draft";
    } else await h.context.finishLogout();
    const afterExit = plain(h.state);
    const renders = h.calls.filter(([name]) => name === "render").length;
    receipt.resolve({ message_id: "late-user", response_id: "late-response", status: "pending" });
    await sending;
    assert.deepEqual(plain(h.state), afterExit, exit);
    assert.equal(h.calls.filter(([name]) => name === "render").length, renders, exit);
    assert.equal(openedStreams, 0, exit);
    assert.equal(h.state.chatSending, false, exit);
    if (exit === "logout") assert.deepEqual(plain(h.state.chatPendingSends), {});
    else assert.ok(h.state.chatPendingSends["chat-a"], "navigation retains the uncertain request for history reconciliation");
  }
});

test("quick conversation switching restores only its own context and derives task scope from response history", async () => {
  const oldMessages = deferred(), oldConversation = deferred();
  const policies = [], signals = [];
  const contextA = { taskId: "task-a", questionVersionId: "question-a:1", courseId: "course-a" };
  const h = chatHarness([], {
    state: { chatContext: contextA, chatContexts: { "chat-a": contextA }, chatDrafts: { "chat-a": "Draft A", "chat-b": "Draft B" } },
    api: {
      getConversation: (id, options) => {
        signals.push(options.signal);
        return id === "chat-a" ? oldConversation.promise : Promise.resolve({ id, course_id: "course-b" });
      },
      conversationMessages: (id, options) => {
        signals.push(options.signal);
        return id === "chat-a" ? oldMessages.promise : Promise.resolve([
          chatMessage("b-old", { task_id: "task-old", question_version_id: "question-old:2" }, { created_at: 1 }),
          chatMessage("b-new", { task_id: "task-b", question_version_id: "question-b:7" }, { created_at: 2 }),
        ]);
      },
      companionAnswerPolicy: async (scope) => { policies.push(scope); return { help_recordable: false }; },
    },
  });
  const readingA = h.context.loadConversation("chat-a");
  const readingB = h.context.loadConversation("chat-b");
  assert.equal(h.state.chatContext, null, "the new conversation must not inherit the previous task while loading");
  assert.equal(h.state.chatDraft, "Draft B");
  await readingB;
  assert.deepEqual(plain(h.state.chatContext), { taskId: "task-b", questionVersionId: "question-b:7", courseId: "course-b" });
  assert.equal(h.state.chatContexts["chat-b"], h.state.chatContext);
  assert.equal(h.state.chatContexts["chat-a"], contextA);
  assert.ok(signals.slice(0, 2).every((signal) => signal.aborted));
  const beforeLate = plain(h.state);
  oldMessages.resolve([chatMessage("a-private", { task_id: "task-a", text: "Private old response" })]);
  oldConversation.resolve({ id: "chat-a", course_id: "course-a" });
  await readingA;
  assert.deepEqual(plain(h.state), beforeLate);
  assert.deepEqual(policies.map(({ taskId, questionVersionId }) => [taskId, questionVersionId]), [["task-b", "question-b:7"]]);
});

test("conversation rename is accessible, preserves retryable errors, and rejects stale completions", async () => {
  const update = deferred();
  const input = { value: "  新名称  ", addEventListener: () => {} };
  const renders = [];
  const h = chatHarness(["escapeHtml", "studentHeading", "studentIcon", "studentDate", "taskTitle"], {
    state: { liveConversations: [{ id: "chat-a", title: "旧名称" }], activeConversation: { id: "chat-a", title: "旧名称" } },
    view: { querySelector: (selector) => selector === "[data-chat-rename-input]" ? input : null },
    globals: { render: () => { renders.push(h.state.chatRenameOpen); } },
    api: { renameConversation: (id, title) => { assert.equal(id, "chat-a"); assert.equal(title, "新名称"); return update.promise; } },
  });
  h.state.chatRenameOpen = true;
  const pending = h.context.renameStudentConversation(input.value);
  assert.equal(h.state.chatRenaming, true);
  assert.equal(h.state.chatRenameTitle, "新名称");
  update.reject(new Error("rename unavailable"));
  await pending;
  assert.equal(h.state.chatRenameOpen, true);
  assert.equal(h.state.chatRenameError, "rename unavailable");
  assert.equal(h.state.chatRenaming, false);

  const staleUpdate = deferred();
  h.context.api.renameConversation = (id, title) => staleUpdate.promise;
  h.state.chatRenameError = "";
  const stale = h.context.renameStudentConversation("不会覆盖新会话");
  h.state.activeConversationId = "chat-b";
  const snapshot = plain(h.state);
  staleUpdate.resolve({ id: "chat-a", title: "旧会话迟到结果" });
  await stale;
  assert.deepEqual(plain(h.state), snapshot);
  assert.ok(renders.length >= 2);
});

test("returning from task-bound chat reloads the same task and reacquires its answer draft", async () => {
  const current = task("task-a", { draft_answers: { "task-a-q": 1 } });
  let reads = 0, leases = 0;
  const h = harness([...taskFunctions, "onAction"], {
    state: { studentPage: "chat", studentTab: "companion", liveTask: current, liveTasks: [current], answers: [], chatContext: { taskId: "task-a" } },
    globals: { studentHeading: () => "", studentIcon: () => "", studentDate: () => "", taskStatus: () => "" },
    api: {
      task: async () => { reads += 1; return plain(current); },
      acquireLease: async () => { leases += 1; return lease("task-a"); },
    },
  });
  h.context.onAction({ currentTarget: { dataset: { action: "back-to-task" } } });
  await settle();
  await settle();
  assert.equal(reads, 2, "selection and lease acquisition both read the authoritative task snapshot");
  assert.equal(leases, 1, "returning reacquires the task's current editing lease");
  assert.equal(h.state.studentPage, "workspace");
  assert.deepEqual(plain(h.state.answers), [1]);
  assert.equal(h.state.liveLease.task_id, "task-a");
});

test("conversation rename markup exposes a labeled inline form and task conversations can return to the attempt", () => {
  const taskContext = { taskId: "task-a", questionVersionId: "question-a:1", stem: "Current question" };
  const h = chatHarness(["escapeHtml", "studentHeading", "studentIcon", "studentDate", "taskTitle", "questionQuestionVersion", "companionPolicyLabel"], {
    state: {
      liveConversations: [{ id: "chat-a", title: "学习对话" }],
      activeConversation: { id: "chat-a", title: "学习对话" },
      chatContext: taskContext, chatRenameOpen: true, chatRenameTitle: "编辑后的名称",
      chatPolicy: { attempted: true, allow_generation: false },
    },
    globals: { studentHeading: () => "<h1>对话</h1>", studentIcon: () => "", taskTitle: () => "Current task" },
  });
  const markup = h.context.chatConversation();
  assert.match(markup, /data-action="rename-chat"/);
  assert.match(markup, /label for="student-chat-rename-title">会话名称/);
  assert.match(markup, /value="编辑后的名称"/);
  assert.match(markup, /data-action="back-to-task">返回作答/);
  assert.ok(!markup.includes("window.prompt"));
});

test("unknown outcomes show pause controls and cancellation unlocks the conversation while preserving its next draft", async () => {
  let handlers, closes = 0, cancellations = 0;
  const h = chatHarness(["escapeHtml", "companionPolicyLabel"], {
    state: { chatDraft: "next draft", chatMessages: [chatMessage("one", { status: "streaming", text: "partial", job_id: "job-one" })] },
    globals: { studentHeading: () => "", studentIcon: () => "" },
    api: {
      openResponseStream: (id, callbacks) => { handlers = callbacks; return () => { closes += 1; }; },
      cancelCompanionJob: async (id) => { assert.equal(id, "job-one"); cancellations += 1; },
      getResponse: async () => ({ response_id: "response-one", job_id: "job-one", status: "cancelled", text: "", generation: 1, last_event_id: 3,
        job: { status: "reconciling", cancel_requested: true, reconciliation_required: true, cost_pending: true } }),
    },
  });
  h.context.connectChatResponse("response-one");
  handlers.snapshot({ response_id: "response-one", job_id: "job-one", status: "streaming", text: "partial", generation: 1, last_event_id: 2,
    job: { status: "reconciling", cancel_requested: false, reconciliation_required: true, cost_pending: true } }, { id: 2 });
  assert.equal(closes, 1, "a paused response closes its stream instead of waiting indefinitely");
  assert.match(h.context.chatResponseStatus(h.state.chatMessages[0].response), /核对调用结果/);
  let markup = h.context.chatConversation();
  assert.match(markup, /data-action="cancel-chat-response"/);
  assert.match(markup, /data-action="refresh-chat-response"/);
  assert.ok(!markup.includes('data-action="resume-chat-response"'));
  assert.match(markup, /回答已暂停/);
  await h.context.actOnChatResponse("response-one", "resume-chat-response");
  assert.equal(cancellations, 0);
  await h.context.actOnChatResponse("response-one", "cancel-chat-response");
  assert.equal(cancellations, 1);
  assert.equal(h.state.chatMessages[0].response.status, "cancelled");
  assert.equal(h.state.chatDraft, "next draft");
  markup = h.context.chatConversation();
  assert.ok(!markup.includes('data-action="cancel-chat-response"'));
  assert.ok(!markup.includes('class="btn primary" disabled'));
});

test("paid recovery is explicit, waits for settlement, and resumes only the existing response", async () => {
  const actions = [], streams = [];
  const job = { status: "awaiting_confirmation", cancel_requested: false, reconciliation_required: false, cost_pending: true };
  const h = chatHarness(["escapeHtml"], {
    state: { chatMessages: [chatMessage("one", { status: "streaming", job_id: "job-one", job })] },
    api: {
      resumeCompanionJob: async (id) => { actions.push(id); },
      getResponse: async () => ({ response_id: "response-one", job_id: "job-one", status: "streaming", text: "partial", generation: 1, last_event_id: 2, job: { status: "queued" } }),
      openResponseStream: (id, callbacks, options) => { streams.push([id, options]); return () => {}; },
    },
  });
  assert.ok(!h.context.chatResponseControls(h.state.chatMessages[0].response).includes("确认恢复"));
  await h.context.actOnChatResponse("response-one", "resume-chat-response");
  assert.deepEqual(actions, []);
  job.cost_pending = false;
  assert.match(h.context.chatResponseControls(h.state.chatMessages[0].response), /确认恢复（可能产生费用）/);
  await h.context.actOnChatResponse("response-one", "resume-chat-response");
  assert.deepEqual(actions, ["job-one"]);
  assert.equal(streams.length, 1);
  assert.equal(streams[0][0], "response-one");
  assert.equal(streams[0][1].after, 2);
});

test("a pending response action ignores duplicate clicks and cannot update another conversation", async () => {
  const pending = deferred();
  let calls = 0, reads = 0;
  const h = chatHarness([], {
    state: { chatMessages: [chatMessage("one", { status: "streaming", job_id: "job-one", job: { status: "reconciling" } })] },
    api: {
      cancelCompanionJob: () => { calls += 1; return pending.promise; },
      getResponse: async () => { reads += 1; throw new Error("must not read"); },
    },
  });
  const first = h.context.actOnChatResponse("response-one", "cancel-chat-response");
  await h.context.actOnChatResponse("response-one", "cancel-chat-response");
  assert.equal(calls, 1);
  h.context.closeStudentChatStream();
  h.state.activeConversationId = "chat-b";
  h.state.chatMessages = [];
  const before = plain(h.state);
  pending.resolve({ status: "cancelled" });
  await first;
  assert.equal(reads, 0);
  assert.deepEqual(plain(h.state), before);
});

test("message pagination appends and deduplicates older rows while refresh keeps the established older cursor", async () => {
  const latest = Array.from({ length: 100 }, (_, index) => chatMessage(`m-${100 + index}`, {}, { created_at: 100 + index }));
  const older = Array.from({ length: 100 }, (_, index) => chatMessage(`m-${index + 1}`, {}, { created_at: index + 1 }));
  const requests = [];
  let failOlder = false, height = 1000;
  const thread = { scrollTop: 0, scrollHeight: height, clientHeight: 200 };
  const h = chatHarness([], {
    view: { querySelector: (selector) => selector === "[data-chat-thread]" ? thread : null },
    globals: { render: () => { thread.scrollHeight = height; } },
    api: {
      getConversation: async (id) => ({ id }), companionAnswerPolicy: async () => ({ help_recordable: false }),
      conversationMessages: async (id, options) => {
        requests.push(options);
        if (options.before) {
          if (failOlder) throw new Error("older page unavailable");
          height = 2000;
          return older;
        }
        return latest;
      },
    },
  });
  await h.context.loadConversation("chat-a");
  assert.equal(h.state.chatHasOlder, true);
  assert.equal(h.state.chatOlderCursor, "m-100");
  thread.scrollTop = 80;
  await h.context.loadOlderChatMessages();
  assert.equal(requests[1].before, "m-100");
  assert.equal(requests[1].limit, 100);
  assert.equal(h.state.chatMessages.length, 199, "the boundary row must not appear twice");
  assert.equal(h.state.chatMessages[0].id, "m-1");
  assert.equal(h.state.chatMessages.at(-1).id, "m-199");
  assert.equal(h.state.chatOlderCursor, "m-1");
  assert.equal(thread.scrollTop, 1080, "prepending preserves the reading position instead of jumping to the bottom");
  await h.context.syncConversationHistory("chat-a");
  assert.equal(h.state.chatMessages.length, 199);
  assert.equal(h.state.chatOlderCursor, "m-1", "latest-history recovery must not discard the older-page cursor");
  failOlder = true;
  await h.context.loadOlderChatMessages();
  assert.equal(requests.at(-1).before, "m-1");
  assert.equal(h.state.chatOlderCursor, "m-1", "failed pagination must retry the same boundary");
  assert.equal(h.state.chatLoadingOlder, false);
  assert.equal(h.state.chatMessages.length, 199);
  assert.match(h.state.chatResponseError, /older page unavailable/);
});

test("conversation pagination appends unique rows, rejects stale list responses and retains records on failure", async () => {
  const first = Array.from({ length: 100 }, (_, index) => ({ id: `chat-${index}`, title: `Conversation ${index}` }));
  const second = [{ id: "chat-99" }, { id: "chat-100" }];
  const stale = deferred();
  const requests = [];
  const h = chatHarness(["escapeHtml", "companion", "companionPolicyLabel"], {
    state: { studentPage: "home", activeConversationId: null },
    globals: { studentHeading: () => "", studentIcon: () => "", studentDate: () => "" },
    api: { conversations: async (options) => {
      requests.push(options);
      if (requests.length === 1) return first;
      if (requests.length === 2) return second;
      if (requests.length === 3) return stale.promise;
      if (requests.length === 4) return [{ id: "archived", title: "Archived record", status: "archived" }];
      throw new Error("list unavailable");
    } },
  });
  await h.context.loadConversationList();
  assert.equal(h.state.chatHasMoreConversations, true);
  assert.match(h.context.companion(), /data-action="more-conversations"/);
  await h.context.loadConversationList({ more: true });
  assert.equal(requests[1].offset, 100);
  assert.equal(h.state.liveConversations.length, 101);
  assert.equal(h.state.liveConversations.filter(({ id }) => id === "chat-99").length, 1);
  assert.equal(h.state.chatHasMoreConversations, false);
  const readingStale = h.context.loadConversationList();
  h.state.chatArchived = true;
  await h.context.loadConversationList();
  stale.resolve([{ id: "stale-active" }]);
  await readingStale;
  assert.deepEqual(plain(h.state.liveConversations), [{ id: "archived", title: "Archived record", status: "archived" }]);
  assert.equal(requests[3].archived, true);
  await h.context.loadConversationList();
  assert.equal(h.state.liveConversations[0].id, "archived");
  assert.match(h.state.conversationError, /list unavailable/);
  const markup = h.context.companion();
  assert.match(markup, /Archived record/);
  assert.match(markup, /data-action="reload-conversations"/);
});

test("context sending reuses its created conversation and captured request after an uncertain POST", async () => {
  const context = { taskId: "task-a", questionVersionId: "question-a:4", questionId: "question-a", courseId: "course-a", stem: "Original problem" };
  const creates = [], posts = [], policies = [];
  const conversation = { id: "created-chat", title: "Saved context conversation" };
  const h = chatHarness([], {
    state: { activeConversationId: null, studentPage: "home", chatContext: context, chatContexts: { __new: context }, chatDraft: "Original context prompt" },
    api: {
      companionAnswerPolicy: async (scope) => { policies.push(scope); return { help_recordable: false }; },
      createConversation: async (...args) => { creates.push(plain(args)); return conversation; },
      postMessage: async (...args) => {
        posts.push(plain(args));
        if (posts.length === 1) throw new Error("acceptance unknown");
        return { message_id: "accepted-context", response_id: "context-response", status: "completed" };
      },
    },
  });
  await h.context.sendContextChat();
  const pending = context.pending;
  assert.ok(pending);
  assert.equal(context.conversation, conversation);
  assert.equal(h.state.activeConversationId, null);
  assert.equal(h.state.chatDraft, "Original context prompt");
  assert.equal(h.state.chatSending, false);
  assert.equal(h.state.chatCreating, false);
  context.taskId = "later-task";
  context.questionVersionId = "later-question:9";
  context.courseId = "later-course";
  h.state.chatDraft = "Later edited prompt";
  await h.context.sendContextChat();
  assert.equal(creates.length, 1, "retry cannot create a second conversation");
  assert.equal(creates[0][2], "course-a");
  assert.deepEqual(posts[1], posts[0]);
  assert.deepEqual(posts[0], ["created-chat", "Original context prompt", { taskId: "task-a", questionVersionId: "question-a:4", requestId: pending.requestId }]);
  assert.deepEqual(policies.map(({ taskId, questionVersionId }) => [taskId, questionVersionId]), [["task-a", "question-a:4"], ["task-a", "question-a:4"]]);
  assert.equal(h.state.activeConversationId, "created-chat");
  assert.equal(h.state.studentPage, "chat");
  assert.equal(h.state.chatMessages.length, 1);
  assert.equal(h.state.chatMessages[0].client_request_id, pending.requestId);
  assert.equal(context.pending, null);
  assert.equal(h.state.chatContexts.__new, undefined);
  assert.equal(h.state.chatContexts["created-chat"], context);
});

test("help recording uses one stable unversioned question request and failed recording blocks the message POST", async () => {
  for (const scope of [
    { questionId: "stable-question", questionVersionId: "version-alias:7", expected: "stable-question" },
    { questionVersionId: "restored-question@8", expected: "restored-question" },
  ]) {
    const events = [], posts = [], order = [];
    let helpAttempts = 0;
    const h = chatHarness([], {
      state: { chatDraft: "Please inspect my attempt", chatContext: { taskId: "task-a", ...scope } },
      api: {
        companionAnswerPolicy: async () => { order.push("policy"); return { help_recordable: true }; },
        helpEvent: async (...args) => {
          events.push(plain(args)); order.push("help");
          if (++helpAttempts === 1) throw new Error("help unavailable");
        },
        postMessage: async (...args) => {
          posts.push(plain(args)); order.push("post");
          if (posts.length === 1) throw new Error("POST outcome unknown");
          return { message_id: "accepted", response_id: "answer", status: "completed" };
        },
      },
    });
    await h.context.sendStudentMessage();
    const pending = h.state.chatPendingSends["chat-a"];
    assert.equal(posts.length, 0);
    assert.equal(pending.help.recorded, false);
    assert.equal(h.state.chatDraft, "Please inspect my attempt");
    assert.match(h.state.chatResponseError, /help unavailable/);
    h.state.chatContext = { taskId: "changed-task", questionId: "changed-question", questionVersionId: "changed-question:90" };
    await h.context.sendStudentMessage();
    assert.equal(posts.length, 1);
    assert.equal(pending.help.recorded, true);
    assert.deepEqual(events[1], events[0], "help retry must retain both request ID and question scope");
    assert.deepEqual(events[0], ["task-a", {
      question_id: scope.expected,
      payload: { help_type: "hint", source: "companion", request_semantics: "requested" },
    }, pending.help.requestId]);
    await h.context.sendStudentMessage();
    assert.equal(events.length, 2, "an acknowledged help event must not be recorded again after POST failure");
    assert.deepEqual(posts[1], posts[0]);
    assert.deepEqual(order, ["policy", "help", "policy", "help", "post", "policy", "post"]);
    assert.equal(h.state.chatPendingSends["chat-a"], undefined);
    assert.equal(h.state.chatMessages.length, 1);
  }
});

test("failed draft persistence returns false and blocks policy, help recording and the chat POST with input retained", async () => {
  const current = task("task-a");
  const writes = [], results = [];
  const h = chatHarness(["taskTitle", "updateStudentTask", "saveLiveDraft"], {
    state: {
      liveTask: current, liveTasks: [current], liveLease: lease(current.id), answers: [0], draftDirty: true,
      chatContext: { taskId: current.id, questionId: "task-a-q", questionVersionId: "task-a-q:3" },
      chatDraft: "Check my selected first option", studentDrafts: { "task-a": { answers: [0], dirty: true } },
    },
    api: {
      saveDraft: async () => { writes.push("save"); throw new Error("draft unavailable"); },
      companionAnswerPolicy: async () => { writes.push("policy"); return { help_recordable: true }; },
      helpEvent: async () => { writes.push("help"); }, postMessage: async () => { writes.push("post"); },
    },
  });
  const save = h.context.saveLiveDraft;
  h.context.saveLiveDraft = async () => { const saved = await save(); results.push(saved); return saved; };
  await h.context.sendStudentMessage();
  assert.deepEqual(writes, ["save"]);
  assert.deepEqual(results, [false]);
  assert.deepEqual(plain(h.state.answers), [0]);
  assert.equal(h.state.draftDirty, true);
  assert.equal(h.state.studentDrafts["task-a"].dirty, true);
  assert.equal(h.state.chatDraft, "Check my selected first option");
  assert.ok(h.state.chatPendingSends["chat-a"]);
  assert.equal(h.state.chatSending, false);
  assert.equal(h.state.loading, false);
  assert.equal(h.state.banner, "save-fail");
  assert.match(h.state.chatResponseError, /作答尚未保存/);
});

test("no-attempt and diagnostic preflights permit safe-guidance POSTs without recording help", async () => {
  for (const policy of [
    { allow_generation: false, attempted: false, diagnostic: false, help_recordable: false },
    { allow_generation: false, attempted: true, diagnostic: true, help_recordable: false },
  ]) {
    const calls = [];
    const h = chatHarness([], {
      state: { chatDraft: "How should I start my independent attempt?", chatContext: { taskId: "task-a", questionId: "question-a", questionVersionId: "question-a:3" } },
      api: {
        companionAnswerPolicy: async () => { calls.push("policy"); return policy; },
        helpEvent: async () => { calls.push("help"); },
        postMessage: async () => { calls.push("post"); return { message_id: "safe-user", response_id: "safe-response", status: "completed" }; },
      },
    });
    await h.context.sendStudentMessage();
    assert.deepEqual(calls, ["policy", "post"]);
    assert.equal(h.state.chatPolicy, policy);
    assert.equal(h.state.chatMessages.length, 1);
    assert.equal(h.state.chatMessages[0].id, "safe-user");
    assert.equal(h.state.chatResponseError, "");
    assert.deepEqual(plain(h.state.chatPendingSends), {});
  }
});

test("stream deltas use textContent without rerendering the input and preserve a user's upward scroll", () => {
  const dom = chatDom();
  let handlers;
  const h = chatHarness([], {
    state: { chatMessages: [chatMessage("one", { status: "streaming", text: "Prefix " })] },
    view: dom.view, globals: { document: dom.document, render: () => dom.render() },
    api: { openResponseStream: (id, callbacks) => { handlers = callbacks; return () => {}; } },
  });
  const original = dom.input;
  dom.document.activeElement = original;
  h.context.connectChatResponse("response-one");
  const untrusted = '<img src=x onerror="bad()"><script>bad()</script>';
  handlers.delta({ text: untrusted }, { id: 2, generation: 1 });
  assert.equal(dom.body.textContent, `Prefix ${untrusted}`);
  assert.equal(h.state.chatMessages[0].response.text, `Prefix ${untrusted}`);
  assert.equal(dom.renders, 0, "each token must update the response body rather than replace the form");
  assert.equal(dom.input, original);
  assert.equal(dom.document.activeElement, original);
  assert.equal(original.value, "Draft being edited");
  assert.equal(original.selectionStart, 3);
  assert.equal(original.selectionEnd, 8);
  assert.equal(original.scrollTop, 19);
  assert.equal(dom.thread.scrollTop, 120);
  dom.thread.scrollTop = 880;
  handlers.delta({ text: " next" }, { id: 3, generation: 1 });
  assert.equal(dom.thread.scrollTop, dom.thread.scrollHeight, "a reader already at the bottom follows new text");
  assert.equal(dom.renders, 0);
});

test("composition Enter never submits and snapshots defer rerender until composition ends with selection and scroll restored", () => {
  const dom = chatDom();
  let handlers;
  const h = chatHarness(["bind"], {
    state: { chatMessages: [chatMessage("one", { status: "streaming" })] },
    view: dom.view, globals: { document: dom.document, render: () => dom.render() },
    api: { openResponseStream: (id, callbacks) => { handlers = callbacks; return () => {}; } },
  });
  h.context.bind();
  h.context.connectChatResponse("response-one");
  const input = dom.input;
  dom.document.activeElement = input;
  let prevented = 0;
  const enter = { key: "Enter", preventDefault: () => { prevented += 1; } };
  input.fire("compositionstart");
  input.fire("keydown", enter);
  assert.equal(dom.submits, 0);
  handlers.snapshot({ text: "Full response", status: "streaming", last_event_id: 7 }, { id: 7, generation: 1 });
  assert.equal(h.state.chatMessages[0].response.text, "Full response");
  assert.equal(dom.renders, 0);
  assert.equal(h.context.chatRenderPending, true);
  assert.equal(dom.input, input);
  assert.equal(dom.document.activeElement, input);
  input.fire("compositionend");
  assert.equal(dom.renders, 1);
  assert.equal(h.context.chatRenderPending, false);
  assert.notEqual(dom.input, input);
  assert.equal(dom.document.activeElement, dom.input);
  assert.equal(dom.input.selectionStart, 3);
  assert.equal(dom.input.selectionEnd, 8);
  assert.equal(dom.input.scrollTop, 19);
  assert.equal(dom.thread.scrollTop, 120);
  h.context.bind();
  dom.input.fire("keydown", { ...enter, isComposing: true });
  dom.input.fire("keydown", { ...enter, keyCode: 229 });
  dom.input.fire("keydown", { ...enter, shiftKey: true });
  assert.equal(dom.submits, 0);
  assert.equal(prevented, 0);
  dom.input.fire("keydown", enter);
  assert.equal(dom.submits, 1);
  assert.equal(prevented, 1);

  h.state.studentPage = "home";
  h.state.chatContext = { taskId: "task-a" };
  h.context.renderStudentChatIfActive(h.context.chatEpoch);
  assert.equal(dom.renders, 2, "the task-context form on companion home also supports guarded rerenders");
  h.state.studentTab = "tasks";
  h.context.renderStudentChatIfActive(h.context.chatEpoch);
  h.state.studentTab = "companion";
  h.state.role = "teacher";
  h.context.renderStudentChatIfActive(h.context.chatEpoch);
  assert.equal(dom.renders, 2, "chat rerenders cannot replace another role or tab");
});

test("chat routes contain only an encoded ID and authenticated refresh restores that conversation after workspace load", async () => {
  const order = [], urls = [];
  const id = "conversation / one?#中文";
  const location = { pathname: "/prototype/index.html", search: "?mode=student", hash: "" };
  const h = chatHarness(["enterWorkspace"], {
    state: { screen: "entry", activeConversationId: null, role: null, reduce: true, chatDraft: "PRIVATE DRAFT", chatContext: { taskId: "PRIVATE TASK" } },
    window: {
      location,
      history: { replaceState: (state, title, url) => { urls.push(url); location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : ""; } },
    },
    globals: { loadWorkspace: async () => { order.push("workspace"); } },
    api: {
      getConversation: async (conversationId) => { order.push(`conversation:${conversationId}`); return { id: conversationId }; },
      conversationMessages: async () => [], companionAnswerPolicy: async () => ({ help_recordable: false }),
    },
  });
  h.context.setChatRoute(id);
  assert.equal(urls[0], `/prototype/index.html?mode=student#chat=${encodeURIComponent(id)}`);
  assert.equal(h.context.chatRoute(), id);
  assert.ok(!urls[0].includes("PRIVATE"));
  location.hash = "#chat=%E0%A4%A";
  assert.equal(h.context.chatRoute(), null, "malformed hash escapes must not break workspace entry");
  location.hash = "#other=unrelated";
  assert.equal(h.context.chatRoute(), null);
  location.hash = `#chat=${encodeURIComponent(id)}`;
  await h.context.enterWorkspace("student", "Learner", { live: true, message: "ready" });
  assert.equal(h.state.screen, "app");
  assert.equal(h.state.activeConversationId, id);
  assert.equal(h.state.studentPage, "chat");
  assert.deepEqual(order, ["workspace", `conversation:${id}`]);
  h.context.setChatRoute(null);
  assert.equal(urls.at(-1), "/prototype/index.html?mode=student");
  assert.equal(h.context.chatRoute(), null);
});

test("a workspace refresh cannot overwrite the policy or conversation list after switching chat context", async () => {
  const list = deferred(), policy = deferred();
  const signals = [];
  const contextA = { taskId: "task-a", questionVersionId: "question-a:1" };
  const policyB = { allow_generation: false, attempted: true, help_recordable: true };
  const h = chatHarness(["taskTitle", "taskFinished", "loadStudent"], {
    state: { chatContext: contextA, chatContexts: { "chat-a": contextA }, liveConversations: [{ id: "chat-a" }, { id: "chat-b" }] },
    api: {
      myTasks: async () => [task("task-a")], goals: async () => [], courses: async () => [],
      conversations: (options) => { signals.push(options.signal); return list.promise; },
      companionAnswerPolicy: (scope) => {
        if (scope.taskId === "task-a") { signals.push(scope.signal); return policy.promise; }
        return Promise.resolve(policyB);
      },
      getConversation: async (id) => ({ id, course_id: "course-b" }),
      conversationMessages: async () => [chatMessage("b-user", { task_id: "task-b", question_version_id: "question-b:7" })],
    },
  });
  const refreshing = h.context.loadStudent();
  await settle();
  assert.equal(signals.length, 2, "workspace chat reads must be tracked for cancellation");
  await h.context.loadConversation("chat-b");
  assert.ok(signals.every((signal) => signal.aborted));
  assert.equal(h.state.chatPolicy, policyB);
  assert.equal(h.state.chatContext.taskId, "task-b");
  const switched = plain(h.state);
  list.resolve([{ id: "stale-private-list" }]);
  policy.resolve({ allow_generation: true, help_recordable: false });
  await refreshing;
  assert.deepEqual(plain(h.state), switched, "a late workspace response must not replace the selected chat's scope or list");
  assert.equal(h.context.chatReadControllers.size, 0);
});

test("pagehide closes chat work and persisted pageshow plus hash navigation restore the current ID route", async () => {
  const listeners = new Map(), reads = [], streams = [];
  const pendingHistory = deferred();
  const location = { pathname: "/prototype/", search: "", hash: "#chat=chat-a" };
  let historySignal, historyCalls = 0;
  const h = chatHarness(["resumeStudentChat", "onAction"], {
    state: { chatMessages: [chatMessage("one", { status: "streaming", text: "Prefix" })] },
    window: {
      location, addEventListener: (name, callback) => listeners.set(name, callback),
      history: { replaceState: (state, title, url) => { location.hash = url.includes("#") ? url.slice(url.indexOf("#")) : ""; } },
    },
    api: {
      getConversation: async (id) => { reads.push(id); return { id }; },
      conversationMessages: (id, options) => {
        if (++historyCalls === 1) { historySignal = options.signal; return pendingHistory.promise; }
        return Promise.resolve([chatMessage(`user-${id}`)]);
      },
      conversations: async () => [], companionAnswerPolicy: async () => ({ help_recordable: false }),
      openResponseStream: (id, handlers) => {
        const stream = { handlers, closes: 0 };
        streams.push(stream);
        return () => { stream.closes += 1; };
      },
    },
  });
  vm.runInContext(sourceSection('window.addEventListener("pagehide",', 'window.addEventListener("resize",'), h.context, { filename: appPath });
  h.context.connectChatResponse("response-one");
  const oldRead = h.context.syncConversationHistory("chat-a");
  listeners.get("pagehide")();
  assert.equal(historySignal.aborted, true);
  assert.equal(streams[0].closes, 1);
  assert.equal(h.context.chatStreamClosers.size, 0);
  location.hash = "#chat=chat-routed";
  listeners.get("pageshow")({ persisted: false });
  assert.deepEqual(reads, []);
  listeners.get("pageshow")({ persisted: true });
  await settle();
  assert.deepEqual(reads, ["chat-routed"], "restoration must use the URL ID ahead of the old cached selection");
  assert.equal(h.state.activeConversationId, "chat-routed");
  const restored = plain(h.state);
  streams[0].handlers.delta({ text: "Late old text" }, { id: 99, generation: 1 });
  pendingHistory.resolve([chatMessage("late-private")]);
  await oldRead;
  assert.deepEqual(plain(h.state), restored);
  location.hash = "#chat=chat-next";
  listeners.get("hashchange")();
  await settle();
  assert.equal(h.state.activeConversationId, "chat-next");
  assert.deepEqual(reads, ["chat-routed", "chat-next"]);
  location.hash = "";
  listeners.get("hashchange")();
  await settle();
  assert.equal(h.state.activeConversationId, null);
  assert.equal(h.state.studentPage, "home");
  assert.equal(h.state.studentTab, "companion");
  h.state.role = "teacher";
  location.hash = "#chat=private-other-role";
  listeners.get("hashchange")();
  listeners.get("pageshow")({ persisted: true });
  await settle();
  assert.deepEqual(reads, ["chat-routed", "chat-next"]);
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
