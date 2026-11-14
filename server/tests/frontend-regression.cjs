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
  vm.runInContext(names.map(functionSource).join("\n"), context, { filename: appPath });
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
