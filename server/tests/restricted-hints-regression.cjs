"use strict";

// Actual API transport, controlled snapshots/events/timers; no browser or network.
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const apiPath = path.resolve(__dirname, "../../web/prototype/api.js");
const apiSource = readFileSync(apiPath, "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));
const plain = (value) => JSON.parse(JSON.stringify(value));
const snapshot = (overrides = {}) => ({ response_id: "hint-response", conversation_id: "hint-chat", job_id: "hint-job",
  generation: 1, last_event_id: 4, status: "completed", intent: "hint", hint_level: 1, hint_metadata: null,
  redacted: true, text: "", job: { status: "running" }, ...overrides });

function harness(read, { sse = true } = {}) {
  const sources = [], requests = [], timers = new Map(), events = [];
  let serial = 0;
  class EventSource {
    constructor() { this.listeners = new Map(); this.closed = 0; sources.push(this); }
    addEventListener(type, callback) { this.listeners.set(type, callback); }
    send(type, data, id) { this.listeners.get(type)?.({ data: JSON.stringify(data), lastEventId: String(id) }); }
    close() { this.closed += 1; }
  }
  const context = vm.createContext({
    location: { protocol: "https:", origin: "https://school.invalid" }, URLSearchParams, AbortController,
    ...(sse ? { EventSource } : {}),
    fetch: async (url, options) => {
      requests.push({ url, options });
      const result = await read(url, requests.length);
      return { ok: (result.status || 200) < 400, status: result.status || 200, headers: {},
        text: async () => JSON.stringify(result.body) };
    },
    setTimeout(callback, delay) { const id = ++serial; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  vm.runInContext(apiSource.replace(/^export /gm, "") + "\nglobalThis.productionApi = api;", context, { filename: apiPath });
  const handlers = {};
  for (const type of ["snapshot", "end", "revoked", "error", "reset", "delta", "onClosed"]) {
    handlers[type] = (data, meta) => events.push({ type, data: plain(data), meta: meta && plain(meta) });
  }
  const close = context.productionApi.openResponseStream("hint-response", handlers);
  return {
    sources, requests, timers, events, close,
    of: (type) => events.filter((event) => event.type === type),
    async next() {
      assert.equal(timers.size, 1, "Recovery must have exactly one bounded poll timer");
      const [id, timer] = [...timers][0];
      assert.ok(timer.delay >= 500 && timer.delay <= 5000);
      timers.delete(id);
      timer.callback();
      await settle();
    },
  };
}

test("completed hint snapshots keep SSE open for queued, leased, running and retry_wait delivery jobs", async () => {
  for (const status of ["queued", "leased", "running", "retry_wait"]) {
    const initial = snapshot({ job: { status } });
    const h = harness(async () => ({ body: initial }));
    await settle();
    assert.equal(h.of("snapshot").length, 1, status);
    assert.equal(h.of("onClosed").length, 0, status);
    assert.equal(h.sources.length, 1, status);
    const source = h.sources[0];
    const revised = { ...initial, hint_level: 2, hint_metadata: { status: "validated" } };
    source.send("snapshot", revised, 4);
    assert.equal(h.of("snapshot").length, 2, "Same-cursor metadata and level changes must be emitted");
    assert.equal(h.of("snapshot")[1].data.hint_level, 2);
    source.send("snapshot", revised, 4);
    assert.equal(h.of("snapshot").length, 2, "Identical snapshots are still deduplicated");
    const delivered = { ...revised, redacted: false, text: "approved fixed template", job: { status: "succeeded" }, hint_metadata: { status: "validated_delivered", evidence_id: "evidence-1" } };
    source.send("snapshot", delivered, 4);
    assert.equal(h.of("snapshot").at(-1).data.text, "approved fixed template");
    assert.equal(h.of("snapshot").at(-1).data.hint_metadata.evidence_id, "evidence-1");
    assert.equal(h.of("onClosed").length, 1);
    assert.equal(source.closed, 1);
    assert.equal(h.timers.size, 0);
    h.close();
    assert.equal(h.of("onClosed").length, 1);
  }
});

test("hint end events poll the original response until same-cursor durable delivery is confirmed", async () => {
  let reads = 0;
  const h = harness(async (url) => {
    assert.equal(url, "/api/v1/companion/responses/hint-response", "Terminal recovery reads the original snapshot only");
    reads += 1;
    return { body: reads === 1 ? snapshot({ status: "streaming", last_event_id: 3 })
      : reads === 2 ? snapshot()
        : snapshot({ redacted: false, text: "confirmed direction", job: { status: "succeeded" }, hint_metadata: { status: "validated_delivered" } }) };
  });
  await settle();
  h.sources[0].send("end", { response_id: "hint-response", generation: 1, status: "completed", intent: "hint", hint_level: 1 }, 4);
  await settle();
  assert.equal(reads, 2);
  assert.equal(h.of("end").length, 0, "Consumer must not close before authoritative delivery");
  assert.equal(h.of("onClosed").length, 0);
  await h.next();
  assert.equal(reads, 3);
  assert.equal(h.of("end").length, 1);
  assert.equal(h.of("end")[0].data.text, "confirmed direction");
  assert.equal(h.of("end")[0].data.hint_metadata.status, "validated_delivered");
  assert.equal(h.of("onClosed").length, 1);
  assert.equal(h.timers.size, 0);
});

test("poll-only completed hint recovery projects fallback metadata and closes on terminal settlement", async () => {
  let reads = 0;
  const h = harness(async (url) => {
    if (url.includes("/events")) return { body: [] };
    reads += 1;
    return { body: reads < 3 ? snapshot({ job: { status: "retry_wait" } })
      : snapshot({ redacted: false, text: "safe independent-work guidance", content_type: "safe_guidance", hint_metadata: { status: "fallback" }, job: { status: "succeeded" } }) };
  }, { sse: false });
  await settle();
  assert.equal(h.of("onClosed").length, 0);
  await h.next();
  assert.equal(h.of("snapshot").at(-1).data.content_type, "safe_guidance");
  assert.equal(h.of("snapshot").at(-1).data.hint_metadata.status, "fallback");
  assert.equal(h.of("onClosed").length, 1);
  assert.equal(h.timers.size, 0);
});

test("revocation during completed hint recovery clears content and cancels all future polling", async () => {
  let reads = 0;
  const h = harness(async (url) => {
    if (url.includes("/events")) return { body: [] };
    reads += 1;
    return reads < 3 ? { body: snapshot() }
      : { status: 403, body: { detail: { code: "access_revoked", message: "access removed" } } };
  }, { sse: false });
  await settle();
  await h.next();
  assert.equal(h.of("revoked").length, 1);
  assert.equal(h.of("revoked")[0].data.text, "");
  assert.equal(h.of("revoked")[0].data.redacted, true);
  assert.equal(h.of("onClosed")[0].data.reason, "revoked");
  assert.equal(h.timers.size, 0);
  const count = h.requests.length;
  h.close();
  await settle();
  assert.equal(h.requests.length, count);
});
