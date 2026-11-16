"use strict";

// Run with: node --test server/tests/companion-api-regression.cjs
// Exercise the actual browser API module with isolated fetch, EventSource and
// timers. No browser, external packages, real network, or real retry delays.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const apiPath = path.resolve(__dirname, "../../web/prototype/api.js");
const apiSource = readFileSync(apiPath, "utf8");
const responseId = "answer/one";
const responsePath = "/api/v1/companion/responses/answer%2Fone";
const plain = (value) => JSON.parse(JSON.stringify(value));
const settle = () => new Promise((resolve) => setImmediate(resolve));

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, headers: {},
    text: async () => payload === undefined ? "" : JSON.stringify(payload) };
}

function snapshot(overrides = {}) {
  return { response_id: responseId, conversation_id: "conversation-1", job_id: "job-1",
    status: "streaming", generation: 1, text: "", redacted: false, last_event_id: 0, ...overrides };
}

function stored(id, type, data, generation = 1) {
  return { event_id: id, response_id: responseId, event_type: type, generation, data };
}

function harness(fetch, { eventSource = true, abortController = true, sourceThrows = false } = {}) {
  const requests = [], sources = [], timers = new Map();
  let serial = 0, now = 0;
  class FakeEventSource {
    constructor(url, options) {
      if (sourceThrows) throw new Error("SSE unavailable");
      this.url = url;
      this.options = options;
      this.readyState = 1;
      this.listeners = new Map();
      this.closeCount = 0;
      sources.push(this);
    }
    addEventListener(type, callback) { this.listeners.set(type, callback); }
    send(type, data, id = "") {
      this.listeners.get(type)?.({ data: JSON.stringify(data), lastEventId: String(id) });
    }
    raw(type, data, id = "") { this.listeners.get(type)?.({ data, lastEventId: String(id) }); }
    error(state = 0) { this.readyState = state; this.onerror?.({ type: "error" }); }
    close() { this.closeCount += 1; this.readyState = 2; }
  }
  const context = vm.createContext({
    location: { protocol: "https:", origin: "https://school.invalid" }, URLSearchParams,
    ...(abortController ? { AbortController } : {}),
    ...(eventSource ? { EventSource: FakeEventSource } : {}),
    fetch: async (url, options) => {
      const request = { url, options };
      requests.push(request);
      return fetch(url, options, requests.length);
    },
    setTimeout(callback, delay) {
      const id = ++serial;
      timers.set(id, { callback, due: now + delay, delay });
      return id;
    },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(apiSource.replace(/^export /gm, "") + "\nglobalThis.productionApi = api;", context, { filename: apiPath });
  return {
    api: context.productionApi, requests, sources, timers,
    delays: () => [...timers.values()].map((timer) => timer.due - now),
    async flush() { await settle(); },
    async next() {
      assert.ok(timers.size, "A retry/poll timer must be scheduled");
      const [id, timer] = [...timers].sort((a, b) => a[1].due - b[1].due)[0];
      timers.delete(id);
      now = timer.due;
      timer.callback();
      await settle();
    },
  };
}

function observer(initialText = "") {
  const events = [];
  const view = { text: initialText, status: "streaming" };
  const handlers = {};
  for (const type of ["start", "reset", "delta", "end", "snapshot", "revoked", "error"]) {
    handlers[type] = (data, meta) => {
      events.push({ type, data, meta });
      if (type === "reset" || type === "revoked") view.text = "";
      if (type === "delta") view.text = data.redacted ? "" : view.text + data.text;
      if (type === "snapshot") view.text = data.text;
      if (data?.status) view.status = data.status;
    };
  }
  handlers.onClosed = (meta) => events.push({ type: "closed", meta });
  return { events, handlers, view, of: (type) => events.filter((event) => event.type === type) };
}

async function fallback(h) {
  assert.equal(h.sources.length, 1, "Initial snapshot should establish one SSE connection");
  h.sources[0].error();
  h.sources[0].error();
  await h.flush();
}

test("conversation reads preserve pagination/encoding, list normalization and optional cancellation", async () => {
  const abort = new AbortController();
  const h = harness(async (url) => response(200, url.includes("messages")
    ? { items: [{ id: "message-1" }] } : url.includes("?") ? [{ id: "conversation-1" }] : { id: "a/b" }));
  assert.deepEqual(plain(await h.api.conversations()), [{ id: "conversation-1" }]);
  await h.api.conversations({ archived: true, limit: 25, offset: 50, signal: abort.signal });
  assert.equal((await h.api.getConversation("a/b", { signal: abort.signal })).id, "a/b");
  assert.deepEqual(plain(await h.api.conversationMessages("a/b", {
    after: "first message", before: "last/message", latest: true, limit: 30, signal: abort.signal,
  })), [{ id: "message-1" }]);
  await h.api.conversationMessages("a/b");
  await h.api.companionAnswerPolicy({ taskId: "task/a", questionVersionId: "q:2", signal: abort.signal });
  await h.api.companionAnswerPolicy();
  assert.deepEqual(h.requests.map((request) => request.url), [
    "/api/v1/companion/conversations?archived=false&limit=100&offset=0",
    "/api/v1/companion/conversations?archived=true&limit=25&offset=50",
    "/api/v1/companion/conversations/a%2Fb",
    "/api/v1/companion/conversations/a%2Fb/messages?limit=30&after=first+message&before=last%2Fmessage&latest=true",
    "/api/v1/companion/conversations/a%2Fb/messages?limit=100",
    "/api/v1/companion/answer-policy?task_id=task%2Fa&question_version_id=q%3A2",
    "/api/v1/companion/answer-policy",
  ]);
  for (const index of [1, 2, 3, 5]) assert.equal(h.requests[index].options.signal, abort.signal);
  assert.ok(h.requests.every(({ options }) => options.credentials === "same-origin"));
});

test("conversation writes keep exact caller keys and serialize immutable message payloads once", async () => {
  const pending = deferred();
  const h = harness(async (url) => url.endsWith("/messages") ? pending.promise : response(201, { id: "conversation-1" }));
  await h.api.createConversation("Math", "create-key", "course/1");
  await h.api.renameConversation("a/b", "New title");
  await h.api.archiveConversation("a/b");
  await h.api.restoreConversation("a/b");
  const options = { requestId: "message-key", taskId: "task-1", questionVersionId: "q:1" };
  const posted = h.api.postMessage("a/b", "  original input\n", options);
  options.requestId = "mutated-key";
  options.taskId = "another-task";
  pending.resolve(response(202, { response_id: "stable-response", message_id: "stable-message", job_id: "stable-job" }));
  assert.equal((await posted).response_id, "stable-response");
  assert.deepEqual(h.requests.map(({ options }) => options.method), ["POST", "PATCH", "POST", "POST", "POST"]);
  assert.deepEqual(JSON.parse(h.requests[0].options.body), { title: "Math", request_id: "create-key", course_id: "course/1" });
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { title: "New title" });
  assert.equal(h.requests[2].url, "/api/v1/companion/conversations/a%2Fb/archive");
  assert.equal(h.requests[3].url, "/api/v1/companion/conversations/a%2Fb/restore");
  assert.deepEqual(JSON.parse(h.requests[4].options.body), {
    text: "  original input\n", request_id: "message-key", task_id: "task-1", question_version_id: "q:1",
  });
  assert.ok(h.requests.every(({ options }) => options.headers.Origin === "https://school.invalid"));
  assert.equal(h.requests.length, 5, "Message receipt must not start a second POST");
});

test("response and events reads retain response IDs, fixed page size, and fetch signals", async () => {
  const abort = new AbortController();
  const h = harness(async () => response(200, snapshot()));
  const result = await h.api.getResponse(responseId, { signal: abort.signal });
  await h.api.responseEvents(responseId, 1234, { signal: abort.signal });
  assert.equal(result.job_id, "job-1");
  assert.deepEqual(h.requests.map(({ url }) => url), [responsePath, `${responsePath}/events?after=1234&limit=1000`]);
  assert.ok(h.requests.every(({ options }) => options.signal === abort.signal));
});

test("cancelled navigation reads preserve AbortError instead of reporting a network outage", async () => {
  const abort = new AbortController();
  const error = new DOMException("Cancelled", "AbortError");
  const h = harness(async () => { abort.abort(); throw error; });
  await assert.rejects(h.api.getConversation("one", { signal: abort.signal }), (caught) => caught === error);
});

test("snapshot preflight repairs an unknown generation, stale prefix, and cursor beyond the tail", async () => {
  const h = harness(async () => response(200, snapshot({ generation: 3, text: "new prefix", last_event_id: 71 })));
  const o = observer("old prefix");
  const close = h.api.openResponseStream(responseId, o.handlers, { after: 9999, text: "old prefix" });
  assert.equal(h.requests.length, 0, "Opening must return its close handle before any callback");
  await h.flush();
  assert.equal(o.view.text, "new prefix");
  assert.equal(o.of("reset").length, 1);
  assert.equal(o.of("snapshot")[0].meta.id, 71);
  assert.equal(o.of("snapshot")[0].data.last_event_id, 71);
  assert.equal(h.sources[0].url, `${responsePath}/stream?after=71`);
  assert.deepEqual(plain(h.sources[0].options), { withCredentials: true });
  assert.deepEqual([...h.sources[0].listeners.keys()], ["start", "reset", "delta", "end", "snapshot", "revoked"]);
  close();
  assert.equal(h.sources[0].closeCount, 1);
  assert.equal(o.of("closed")[0].meta.reason, "closed");
});

test("SSE ignores duplicate IDs and fenced generations, clearing text before a new generation delta", async () => {
  const h = harness(async () => response(200, snapshot({ text: "A", last_event_id: 10 })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const source = h.sources[0];
  source.send("start", { generation: 1, status: "streaming" }, 11);
  source.send("delta", { generation: 1, text: "B" }, 12);
  source.send("delta", { generation: 1, text: "B" }, 12);
  source.send("delta", { generation: 1, text: "late" }, 11);
  assert.equal(o.view.text, "AB");
  source.send("delta", { generation: 2, text: "fresh" }, 20);
  assert.equal(o.view.text, "fresh");
  assert.deepEqual(o.events.slice(-2).map(({ type }) => type), ["reset", "delta"]);
  source.send("delta", { generation: 1, text: "fenced" }, 30);
  source.send("reset", { generation: 2, status: "streaming" }, 21);
  source.send("reset", { generation: 2, status: "streaming" }, 21);
  source.send("delta", { generation: 2, text: "replacement" }, 22);
  assert.equal(o.view.text, "replacement");
  assert.equal(o.of("delta").length, 3);
  assert.equal(o.of("reset").length, 2);
  assert.equal(o.of("start").length, 1);
  close();
});

test("SSE snapshots replace text once, accept policy changes at the same cursor, and reject stale snapshots", async () => {
  const h = harness(async () => response(200, snapshot({ text: "prefix", last_event_id: 5 })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const source = h.sources[0];
  source.send("snapshot", snapshot({ text: "replacement", last_event_id: 8 }), 8);
  source.send("snapshot", snapshot({ text: "replacement", last_event_id: 8 }), 8);
  source.send("snapshot", snapshot({ text: "older", last_event_id: 7 }), 7);
  assert.equal(o.view.text, "replacement");
  assert.equal(o.of("snapshot").length, 2);
  source.send("snapshot", snapshot({ text: "must hide", redacted: true, notice: "Policy changed", last_event_id: 8 }), 8);
  assert.equal(o.view.text, "");
  source.send("delta", { generation: 1, text: "cannot repair a hidden prefix" }, 9);
  assert.equal(o.view.text, "");
  assert.equal(o.of("delta")[0].data.redacted, true);
  source.send("snapshot", snapshot({ text: "allowed full answer", last_event_id: 9 }), 9);
  assert.equal(o.view.text, "allowed full answer");
  assert.equal(o.of("delta").length, 1, "Snapshots must never manufacture append callbacks");
  close();
});

test("same-cursor job changes expose reconciliation and confirmation without replaying text", async () => {
  const h = harness(async () => response(200, snapshot({ text: "prefix", last_event_id: 5, job: { status: "running" } })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const job = { status: "reconciling", cancel_requested: false, reconciliation_required: true, cost_pending: true };
  h.sources[0].send("snapshot", snapshot({ text: "prefix", last_event_id: 5, job }), 5);
  h.sources[0].send("snapshot", snapshot({ text: "prefix", last_event_id: 5, job }), 5);
  assert.equal(o.of("snapshot").length, 2);
  assert.equal(o.of("snapshot").at(-1).data.job.status, "reconciling");
  h.sources[0].send("snapshot", snapshot({ text: "prefix", last_event_id: 5,
    job: { status: "awaiting_confirmation", cancel_requested: false, reconciliation_required: false, cost_pending: false } }), 5);
  assert.equal(o.of("snapshot").length, 3);
  assert.equal(o.view.text, "prefix");
  assert.equal(o.of("delta").length, 0);
  close();
});

test("companion cancel and explicit paid recovery address the same encoded job without posting another message", async () => {
  const h = harness(async () => response(200, { status: "queued" }));
  await h.api.cancelCompanionJob("job/one");
  await h.api.resumeCompanionJob("job/one");
  assert.deepEqual(h.requests.map(({ url }) => url), ["/api/v1/jobs/job%2Fone/cancel", "/api/v1/jobs/job%2Fone/resume"]);
  assert.ok(h.requests.every(({ options }) => options.method === "POST"));
  assert.deepEqual(JSON.parse(h.requests[1].options.body), { confirm_paid: true });
});

test("SSE snapshot frame IDs are accepted when the backend supplies its cursor in the frame", async () => {
  const h = harness(async () => response(200, snapshot({ text: "A", last_event_id: 1 })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const data = snapshot({ text: "AB" });
  delete data.last_event_id;
  h.sources[0].send("snapshot", data, 3);
  assert.equal(o.of("snapshot").at(-1).data.last_event_id, 3);
  assert.equal(o.view.text, "AB");
  close();
});

test("terminal tail preflight is authoritative beyond 1000 events without replay or an SSE connection", async () => {
  const h = harness(async () => response(200, snapshot({ status: "completed", text: "The whole final answer", last_event_id: 5001 })));
  const o = observer("partial");
  const close = h.api.openResponseStream(responseId, o.handlers, { after: 5001, generation: 1, text: "partial" });
  await h.flush();
  assert.equal(o.view.text, "The whole final answer");
  assert.equal(o.of("snapshot")[0].meta.id, 5001);
  assert.equal(o.of("delta").length, 0);
  assert.equal(h.sources.length, 0);
  assert.equal(h.requests.length, 1);
  assert.equal(o.of("closed")[0].meta.reason, "terminal");
  close();
  assert.equal(o.of("closed").length, 1);
});

for (const status of ["failed", "cancelled"]) {
  test(`terminal ${status} end fetches and replaces the prefix before notifying end`, async () => {
    let reads = 0;
    const h = harness(async () => response(200, ++reads === 1
      ? snapshot({ text: "unsafe partial", last_event_id: 2 })
      : snapshot({ status, text: "backend partial must be hidden", content: "backend partial", last_event_id: 3 })));
    const o = observer();
    const close = h.api.openResponseStream(responseId, o.handlers);
    await h.flush();
    h.sources[0].send("end", { generation: 1, status }, 3);
    assert.equal(o.of("end").length, 0, "End must wait for authoritative projection");
    await h.flush();
    assert.equal(o.view.text, "");
    assert.deepEqual(o.events.slice(-3).map(({ type }) => type), ["snapshot", "end", "closed"]);
    assert.equal(o.of("snapshot").at(-1).data.content, "");
    assert.equal(o.of("end")[0].data.text, "");
    assert.equal(o.of("closed")[0].meta.reason, "terminal");
    assert.equal(h.sources[0].closeCount, 1);
    assert.equal(h.timers.size, 0);
    close();
  });
}

test("terminal redacted snapshot clears all text and closes even without an end event", async () => {
  const h = harness(async () => response(200, snapshot({ text: "visible", last_event_id: 2 })));
  const o = observer();
  h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  h.sources[0].send("snapshot", snapshot({ status: "completed", redacted: true,
    text: "hidden", content: "hidden", notice: "Access policy", last_event_id: 3 }), 3);
  assert.equal(o.view.text, "");
  assert.equal(o.of("snapshot").at(-1).data.content, "");
  assert.equal(o.of("closed")[0].meta.reason, "terminal");
  assert.equal(h.timers.size, 0);
});

test("one SSE reconnect deduplicates replay; a second error falls back to the same response without POST", async () => {
  let reads = 0, pages = 0;
  const h = harness(async (url) => {
    if (url.includes("/events?")) {
      pages += 1;
      assert.equal(new URL(url, "https://school.invalid").searchParams.get("after"), pages === 1 ? "2" : "4");
      return response(200, pages === 1 ? [stored(2, "delta", { text: "B" }), stored(3, "delta", { text: "C" })] : []);
    }
    return response(200, ++reads === 1 ? snapshot({ text: "A", last_event_id: 1 }) : snapshot({ text: "ABCD", last_event_id: 4 }));
  });
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const source = h.sources[0];
  source.send("delta", { generation: 1, text: "B" }, 2);
  source.error();
  assert.equal(h.requests.length, 1, "First native reconnect does not launch concurrent polling");
  source.send("delta", { generation: 1, text: "B" }, 2);
  assert.equal(o.view.text, "AB");
  source.error();
  source.error();
  await h.flush();
  assert.equal(o.view.text, "ABCD");
  assert.deepEqual(o.of("delta").map(({ data }) => data.text), ["B", "C"]);
  assert.equal(o.of("snapshot").at(-1).meta.id, 4);
  source.send("delta", { generation: 1, text: "late SSE" }, 100);
  assert.equal(o.view.text, "ABCD");
  await h.next();
  assert.equal(o.view.text, "ABCD");
  assert.equal(o.of("delta").length, 2);
  assert.equal(h.sources.length, 1);
  assert.equal(source.closeCount, 1);
  assert.ok(h.requests.every(({ options }) => options.method === "GET"));
  close();
});

test("polling drains more than 1000 events before the final authoritative snapshot", async () => {
  const first = [stored(1, "start", { status: "streaming" })];
  for (let id = 2; id <= 1000; id += 1) first.push(stored(id, "delta", { text: "x" }));
  const second = [];
  for (let id = 1001; id <= 1201; id += 1) second.push(stored(id, "delta", { text: "x" }));
  second.push(stored(1202, "end", { status: "completed", message_id: "assistant-1" }));
  let reads = 0;
  const cursors = [];
  const h = harness(async (url) => {
    if (url.includes("/events?")) {
      const after = Number(new URL(url, "https://school.invalid").searchParams.get("after"));
      cursors.push(after);
      return response(200, after === 0 ? first : second);
    }
    return response(200, ++reads === 1 ? snapshot() : snapshot({ status: "completed", text: "x".repeat(1200), last_event_id: 1202 }));
  });
  const o = observer();
  h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  await fallback(h);
  assert.deepEqual(cursors, [0, 1000]);
  assert.equal(o.of("delta").length, 1200);
  assert.equal(o.view.text, "x".repeat(1200));
  assert.equal(o.of("snapshot").at(-1).meta.id, 1202);
  assert.equal(o.of("end")[0].data.message_id, "assistant-1");
  assert.deepEqual(o.events.slice(-3).map(({ type }) => type), ["snapshot", "end", "closed"]);
  assert.equal(h.timers.size, 0);
});

test("a text/cursor race advances to the snapshot cursor without duplicating unseen deltas", async () => {
  let reads = 0, pages = 0;
  const cursors = [];
  const h = harness(async (url) => {
    if (url.includes("/events?")) {
      cursors.push(Number(new URL(url, "https://school.invalid").searchParams.get("after")));
      return response(200, ++pages === 1 ? [stored(2, "delta", { text: "B" })]
        : [stored(6, "delta", { text: "F" }), stored(7, "delta", { text: "G" })]);
    }
    reads += 1;
    return response(200, reads === 1 ? snapshot({ text: "A", last_event_id: 1 })
      : snapshot({ text: reads === 2 ? "ABCDEF" : "ABCDEFG", last_event_id: reads === 2 ? 6 : 7 }));
  });
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  await fallback(h);
  assert.equal(o.view.text, "ABCDEF");
  assert.equal(o.of("snapshot").at(-1).data.last_event_id, 6);
  await h.next();
  assert.deepEqual(cursors, [1, 6]);
  assert.deepEqual(o.of("delta").map(({ data }) => data.text), ["B", "G"]);
  assert.equal(o.view.text, "ABCDEFG");
  close();
});

test("poll snapshot generation changes reset old prefixes and use the new generation's tail", async () => {
  let reads = 0, pages = 0;
  const cursors = [];
  const h = harness(async (url) => {
    if (url.includes("/events?")) {
      cursors.push(Number(new URL(url, "https://school.invalid").searchParams.get("after")));
      return response(200, ++pages === 1 ? [stored(5, "delta", { text: " old tail" })]
        : [stored(21, "delta", { text: " tail" }, 2), stored(22, "delta", { text: "fenced" }, 1)]);
    }
    reads += 1;
    return response(200, reads === 1 ? snapshot({ text: "old", last_event_id: 4 })
      : snapshot({ generation: 2, text: reads === 2 ? "new" : "new tail", last_event_id: reads === 2 ? 20 : 21 }));
  });
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers, { after: 4, generation: 1, text: "old" });
  await h.flush();
  await fallback(h);
  assert.equal(o.view.text, "new");
  assert.equal(o.of("reset").at(-1).data.generation, 2);
  assert.equal(o.of("snapshot").at(-1).meta.id, 20);
  await h.next();
  assert.deepEqual(cursors, [4, 20]);
  assert.equal(o.view.text, "new tail");
  assert.equal(o.of("delta").length, 2);
  close();
});

test("poll generation reset events are idempotent and completed snapshot clears a redacted prefix", async () => {
  let reads = 0;
  const h = harness(async (url) => response(200, url.includes("/events?") ? [
    stored(10, "reset", { status: "streaming" }, 2),
    stored(10, "reset", { status: "streaming" }, 2),
    stored(11, "delta", { text: "hidden", redacted: true }, 2),
  ] : ++reads === 1 ? snapshot({ text: "old", last_event_id: 9 })
    : snapshot({ generation: 2, status: "completed", text: "must hide", redacted: true, last_event_id: 12 })));
  const o = observer();
  h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  await fallback(h);
  assert.equal(o.of("reset").length, 1);
  assert.equal(o.view.text, "");
  assert.equal(o.of("closed")[0].meta.reason, "terminal");
});

test("poll retries are bounded exponentially, reset after success, and never resend the original POST", async () => {
  let reads = 0, failedPages = 0, recovered = false;
  const h = harness(async (url) => {
    if (!url.includes("/events?")) { reads += 1; return response(200, snapshot({ last_event_id: 1, text: "A" })); }
    if (recovered) return response(200, []);
    failedPages += 1;
    if (failedPages <= 6) return response(503, { detail: { code: "busy", message: "Try later", retryable: true } });
    recovered = true;
    return response(200, []);
  });
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  await fallback(h);
  const delays = [];
  for (let attempt = 0; attempt < 6; attempt += 1) {
    delays.push(h.delays()[0]);
    await h.next();
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 5000, 5000, 5000]);
  assert.deepEqual(h.delays(), [500]);
  assert.equal(o.of("error").length, 6);
  assert.equal(reads, 2);
  assert.ok(h.requests.every(({ options }) => options.method === "GET"));
  close();
  assert.equal(h.timers.size, 0);
});

for (const status of [401, 403, 404]) {
  test(`preflight ${status} emits revoked once and does not connect or retry`, async () => {
    const h = harness(async () => response(status, { detail: { code: "denied", message: "Access changed" } }));
    const o = observer("old visible text");
    const close = h.api.openResponseStream(responseId, o.handlers);
    await h.flush();
    assert.equal(o.view.text, "");
    assert.equal(o.of("revoked")[0].data.status, status);
    assert.equal(o.of("error").length, 0);
    assert.equal(o.of("closed")[0].meta.reason, "revoked");
    assert.equal(h.requests.length, 1);
    assert.equal(h.sources.length, 0);
    assert.equal(h.timers.size, 0);
    close();
    assert.equal(o.of("closed").length, 1);
  });

  test(`fallback events ${status} revokes and stops instead of retrying`, async () => {
    const h = harness(async (url) => response(url.includes("/events?") ? status : 200,
      url.includes("/events?") ? { detail: "Access changed" } : snapshot({ text: "visible", last_event_id: 2 })));
    const o = observer();
    h.api.openResponseStream(responseId, o.handlers);
    await h.flush();
    await fallback(h);
    assert.equal(o.view.text, "");
    assert.equal(o.of("revoked").length, 1);
    assert.equal(o.of("closed")[0].meta.reason, "revoked");
    assert.equal(h.requests.length, 2);
    assert.equal(h.timers.size, 0);
  });
}

test("an SSE revoked frame closes once and suppresses every late event/error callback", async () => {
  const h = harness(async () => response(200, snapshot({ text: "visible", last_event_id: 2 })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const source = h.sources[0], staleError = source.onerror;
  source.send("revoked", { code: "access_revoked" });
  const count = o.events.length;
  source.send("delta", { generation: 1, text: "late" }, 3);
  source.send("snapshot", snapshot({ text: "late", last_event_id: 4 }), 4);
  staleError();
  close();
  await h.flush();
  assert.equal(o.events.length, count);
  assert.equal(o.view.text, "");
  assert.equal(o.of("closed").length, 1);
  assert.equal(h.requests.length, 1);
  assert.equal(h.timers.size, 0);
});

test("missing JSON snapshot cursors are retried without fabricating a cursor or delta", async () => {
  let reads = 0;
  const h = harness(async () => {
    reads += 1;
    const data = snapshot({ text: "unsafe to append", last_event_id: 99 });
    delete data.last_event_id;
    return response(200, data);
  });
  const o = observer("kept prefix");
  const close = h.api.openResponseStream(responseId, o.handlers, { after: 7, generation: 1, text: "kept prefix" });
  await h.flush();
  assert.equal(o.of("snapshot").length, 0);
  assert.equal(o.of("delta").length, 0);
  assert.equal(o.view.text, "kept prefix");
  assert.equal(h.sources.length, 0);
  assert.equal(reads, 2);
  assert.deepEqual(h.delays(), [1000]);
  close();
});

test("a malformed SSE delta switches to a snapshot without appending corrupt text", async () => {
  let reads = 0;
  const h = harness(async () => response(200, snapshot({ text: ++reads === 1 ? "A" : "AB", last_event_id: reads === 1 ? 1 : 3 })));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  h.sources[0].raw("delta", "{invalid", 2);
  await h.flush();
  assert.equal(o.view.text, "AB");
  assert.equal(o.of("delta").length, 0);
  assert.equal(o.of("snapshot").at(-1).meta.id, 3);
  assert.equal(h.sources[0].closeCount, 1);
  close();
});

test("no EventSource and a constructor failure both recover through read-only polling", async () => {
  for (const options of [{ eventSource: false }, { sourceThrows: true }]) {
    let reads = 0;
    const h = harness(async (url) => response(200, url.includes("/events?") ? [stored(2, "delta", { text: "B" })]
      : snapshot({ status: ++reads === 1 ? "streaming" : "completed", text: reads === 1 ? "A" : "AB", last_event_id: reads === 1 ? 1 : 3 })), options);
    const o = observer();
    h.api.openResponseStream(responseId, o.handlers);
    await h.flush();
    assert.equal(o.view.text, "AB");
    assert.equal(o.of("closed")[0].meta.reason, "terminal");
    assert.ok(h.requests.every(({ options: requestOptions }) => requestOptions.credentials === "same-origin" && requestOptions.method === "GET"));
    assert.equal(h.sources.length, 0);
  }
});

test("close before preflight performs no requests and reports onClosed only once", async () => {
  const h = harness(async () => response(200, snapshot()));
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  close();
  close();
  await h.flush();
  assert.deepEqual(o.events.map(({ type }) => type), ["closed"]);
  assert.equal(h.requests.length, 0);
  assert.equal(h.sources.length, 0);
});

test("close aborts an in-flight preflight and ignores a fetch which resolves after abort", async () => {
  const pending = deferred();
  const h = harness(async () => pending.promise);
  const o = observer();
  const close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  const signal = h.requests[0].options.signal;
  assert.equal(signal.aborted, false);
  close();
  assert.equal(signal.aborted, true);
  pending.resolve(response(200, snapshot({ status: "completed", text: "late", last_event_id: 5 })));
  await h.flush();
  assert.deepEqual(o.events.map(({ type }) => type), ["closed"]);
  assert.equal(h.sources.length, 0);
  assert.equal(h.timers.size, 0);
});

test("close cancels polling, aborts its fetch and rejects late poll callbacks even without AbortController", async () => {
  for (const abortController of [true, false]) {
    const pending = deferred();
    const h = harness(async (url) => url.includes("/events?") ? pending.promise : response(200, snapshot()), { abortController });
    const o = observer();
    const close = h.api.openResponseStream(responseId, o.handlers);
    await h.flush();
    await fallback(h);
    assert.equal(h.requests.length, 2);
    const signal = h.requests[1].options.signal;
    close();
    if (abortController) assert.equal(signal.aborted, true);
    const count = o.events.length;
    pending.resolve(response(200, [stored(1, "delta", { text: "late" })]));
    await h.flush();
    assert.equal(o.events.length, count);
    assert.equal(o.of("delta").length, 0);
    assert.equal(h.requests.length, 2, "A closed events read must not start the snapshot read");
    assert.equal(h.timers.size, 0);
  }
});

test("close in a callback stops remaining events in that same polling page", async () => {
  const h = harness(async (url) => response(200, url.includes("/events?")
    ? [stored(1, "delta", { text: "first" }), stored(2, "delta", { text: "late" })] : snapshot()));
  const o = observer();
  const original = o.handlers.delta;
  let close;
  o.handlers.delta = (...args) => { original(...args); close(); };
  close = h.api.openResponseStream(responseId, o.handlers);
  await h.flush();
  await fallback(h);
  assert.equal(o.view.text, "first");
  assert.equal(o.of("delta").length, 1);
  assert.equal(o.of("closed").length, 1);
  assert.equal(h.requests.length, 2);
  assert.equal(h.timers.size, 0);
});
