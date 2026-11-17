"use strict";

// Production modules run in a VM with an in-memory API and DOM fixture only.
// No server, database, model, fetch, credential, or storage is used.
// Run with: node --test server/tests/learning-notifications-regression.cjs
const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const sourcePath = path.resolve(__dirname, "../../web/prototype/learning-notifications.js");
const source = readFileSync(sourcePath, "utf8");
const settle = () => new Promise((resolve) => setImmediate(resolve));
const deferred = () => { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; };
const plain = (value) => JSON.parse(JSON.stringify(value));
function decode(value) { return String(value).replace(/&(amp|lt|gt|quot|#39);/g, (_, entity) => ({ amp: "&", lt: "<", gt: ">", quot: '"', "#39": "'" })[entity]); }

// Only the DOM surface used by the production modules is implemented. Unsupported
// selectors fail loudly; compound selectors and delegated events behave as in DOM.
class TestElement {
  constructor(tag, ownerDocument, attributes = {}) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = ownerDocument; this.attributes = { ...attributes };
    this.children = []; this.parts = []; this.parentElement = null; this.listeners = new Map(); this.scrollTop = 0;
    this.disabled = Object.hasOwn(attributes, "disabled"); this.readOnly = Object.hasOwn(attributes, "readonly");
    this.value = attributes.value ?? ""; this.selectionStart = this.selectionEnd = 0;
  }
  get dataset() { return Object.fromEntries(Object.entries(this.attributes).filter(([key]) => key.startsWith("data-")).map(([key, value]) => [key.slice(5).replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), value])); }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentElement?.isConnected); }
  append(child) { child.parentElement = this; this.children.push(child); this.parts.push(child); }
  contains(node) { return node === this || this.children.some((child) => child.contains(node)); }
  get textContent() { return this.parts.map((part) => typeof part === "string" ? part : part.textContent).join(""); }
  clear() { this.children.forEach((child) => { child.parentElement = null; }); this.children = []; this.parts = []; }
  set innerHTML(html) {
    this.ownerDocument.paints += 1; this.clear();
    const stack = [this], voidTags = new Set(["INPUT", "BR", "HR", "IMG", "META", "LINK"]);
    for (const token of String(html).match(/<!--[\s\S]*?-->|<\/?[a-z][^>]*>|[^<]+/gi) || []) {
      if (token.startsWith("<!--")) continue;
      if (token.startsWith("</")) { const tag = token.match(/^<\/([^\s>]+)/)[1].toUpperCase(); for (let i = stack.length - 1; i > 0; i -= 1) if (stack[i].tagName === tag) { stack.length = i; break; } continue; }
      if (token.startsWith("<")) {
        const [, tag, raw] = token.match(/^<([^\s/>]+)([^]*)>$/), attrs = {};
        for (const [, name, quoted, single, bare] of raw.matchAll(/([^\s=/>]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g)) attrs[name] = decode(quoted ?? single ?? bare ?? "");
        const child = new TestElement(tag, this.ownerDocument, attrs); stack.at(-1).append(child); if (!voidTags.has(child.tagName) && !token.endsWith("/>")) stack.push(child);
      } else stack.at(-1).parts.push(decode(token));
    }
    for (const textarea of this.querySelectorAll("textarea")) textarea.value = textarea.textContent;
  }
  getAttribute(name) { return this.attributes[name] ?? null; }
  setAttribute(name, value) { this.attributes[name] = String(value); if (name === "disabled") this.disabled = true; if (name === "readonly") this.readOnly = true; }
  matches(selector) {
    let rest = selector.trim(), matched = true;
    while (rest) {
      const token = rest.match(/^(\*|[a-z][\w-]*)|^\.([\w-]+)|^#([\w-]+)|^\[([\w:-]+)(?:=(?:"([^"]*)"|'([^']*)'|([^\]\s]+)))?\]|^:(disabled|enabled|read-only)/i);
      assert.ok(token, `unsupported fixture selector ${selector}`);
      const [, tag, className, id, attribute, double, single, bare, pseudo] = token;
      if (tag) matched &&= tag === "*" || this.tagName === tag.toUpperCase();
      if (className) matched &&= (this.attributes.class || "").split(/\s+/).includes(className);
      if (id) matched &&= this.attributes.id === id;
      if (attribute) { const value = double ?? single ?? bare; matched &&= Object.hasOwn(this.attributes, attribute) && (value === undefined || this.attributes[attribute] === value); }
      if (pseudo) matched &&= pseudo === "disabled" ? this.disabled : pseudo === "enabled" ? !this.disabled : this.readOnly;
      rest = rest.slice(token[0].length);
    }
    return matched;
  }
  closest(selector) { for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node; return null; }
  querySelectorAll(selector) { const selectors = selector.split(/,\s*/), result = []; const visit = (node) => { for (const child of node.children) { if (selectors.some((item) => child.matches(item))) result.push(child); visit(child); } }; visit(this); return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  addEventListener(type, listener) { const list = this.listeners.get(type) || []; list.push(listener); this.listeners.set(type, list); }
  focus() { if (this.isConnected && !this.disabled) this.ownerDocument.activeElement = this; }
  setSelectionRange(start, end = start) { this.selectionStart = start; this.selectionEnd = end; }
  fire(type, extras = {}) {
    const event = { type, target: this, currentTarget: null, bubbles: true, defaultPrevented: false, stopped: false,
      preventDefault() { this.defaultPrevented = true; }, stopPropagation() { this.stopped = true; }, ...extras };
    const route = [], results = [];
    for (let node = this; node; node = node.parentElement) route.push(node);
    for (const node of route) {
      event.currentTarget = node;
      for (const listener of [...(node.listeners.get(type) || [])]) results.push(listener(event));
      if (!event.bubbles || event.stopped) break;
    }
    event.currentTarget = null;
    // DOM dispatch is synchronous; tests may await async work returned by a handler.
    event.finished = Promise.all(results);
    return event;
  }
}
function makeDocument() { const document = { paints: 0 }; document.body = new TestElement("body", document); document.activeElement = document.body; document.querySelector = (selector) => document.body.querySelector(selector); document.querySelectorAll = (selector) => document.body.querySelectorAll(selector); return document; }
function loadFactory() {
  const context = vm.createContext({ console, URLSearchParams, AbortController, setImmediate, setTimeout, clearTimeout });
  vm.runInContext(source.replace(/^export /gm, "") + "\nglobalThis.production = { createLearningNotifications };", context, { filename: sourcePath });
  return context.production.createLearningNotifications;
}

function harness(options = {}) {
  const state = { live: true, role: "student", transitionId: 1, loggingOut: false };
  const rows = Array.from({ length: options.count ?? 21 }, (_, index) => ({
    id: `notification-${index + 1}`, user_id: "student-1", event_type: index % 2 ? "discussion_teacher_message" : "results_ready",
    resource_type: "task", resource_id: `task-${index + 1}`, payload: { task_id: `task-${index + 1}`, message: "PAYLOAD-PRIVATE-TEXT" }, created_at: 1700000000 + index,
  }));
  rows.push({ id: "notification-other-user", user_id: "student-2", event_type: "results_ready", resource_type: "task", resource_id: "secret-task", payload: { task_id: "secret-task" }, created_at: 1700000100 });
  const calls = [], opened = [], readGates = [], toasts = [], optionsState = { ...options };
  const api = { state: { user: { id: "student-1", role: "student", status: "active" } }, call };
  function confirmRead(id, readAt = "confirmed-read") { const row = rows.find((item) => item.id === id); assert.ok(row); row.read_at = readAt; return plain(row); }
  async function call(method, url, body, requestOptions) {
    const request = { method, url, body: body === undefined ? undefined : plain(body), options: requestOptions, userId: api.state.user?.id }; calls.push(request);
    if (method === "GET" && url.startsWith("/me/notifications?")) {
      const query = new URLSearchParams(url.split("?")[1]);
      const unread = query.get("unread_only") === "true", limit = Number(query.get("limit")), offset = Number(query.get("offset"));
      const response = () => plain(rows.filter((row) => (optionsState.unscoped || row.user_id === request.userId) && (!unread || !row.read_at)).slice(offset, offset + limit));
      return optionsState.get ? optionsState.get({ ...request, query }, response) : response();
    }
    if (method === "POST" && /^\/me\/notifications\/[^/]+\/read$/.test(url)) {
      const id = decodeURIComponent(url.split("/")[3]), row = rows.find((item) => item.id === id && item.user_id === request.userId);
      if (!row) throw new Error("missing notification");
      if (optionsState.readGate) { readGates.push({ id, row }); return optionsState.readGate.promise; }
      if (optionsState.read) return optionsState.read(row, () => confirmRead(id));
      return confirmRead(id);
    }
    throw new Error(`unexpected API request ${method} ${url}`);
  }
  const document = makeDocument(); let instance; let renders = 0;
  const render = () => { renders += 1; document.body.innerHTML = instance.render(); instance.bind(document.body); };
  instance = loadFactory()({ api, state, render, toast: (text) => toasts.push(text), onOpen: async (notification) => { opened.push(plain(notification)); return optionsState.onOpen ? optionsState.onOpen(notification) : true; } });
  render();
  const node = (selector) => { const found = document.querySelector(selector); assert.ok(found, `missing production control ${selector}`); return found; };
  const control = (action, id) => node(`[data-notification-action="${action}"]${id ? `[data-notification-id="${id}"]` : ""}`);
  const click = async (action, id) => { const button = control(action, id); assert.equal(button.disabled, false); await button.fire("click").finished; };
  return { api, state, rows, calls, opened, readGates, toasts, optionsState, confirmRead, notifications: instance, document, render, node, control, click,
    posts: () => calls.filter((call) => call.method === "POST"), count: () => node(".learning-notifications-count").textContent,
    ids: () => document.querySelectorAll("article[data-notification-id]").map((item) => item.dataset.notificationId),
    renders: () => renders, html: () => document.body.textContent };
}

test("isolates even mixed-user API rows; load, panel and nested row expansion never mark read", async () => {
  const h = harness({ unscoped: true }); h.rows.unshift(h.rows.pop());
  assert.equal(h.control("panel").getAttribute("aria-expanded"), "false");
  assert.equal(await h.notifications.load(), true);
  assert.equal(h.count(), "21");
  assert.equal(h.ids().length, 0);
  await h.control("panel").querySelector("span").fire("click").finished;
  assert.equal(h.control("panel").getAttribute("aria-expanded"), "true");
  const summary = h.control("toggle", "notification-1");
  assert.equal(summary.dataset.notificationAction, "toggle");
  await summary.querySelector("strong").fire("click").finished;
  assert.equal(h.control("toggle", "notification-1").getAttribute("aria-expanded"), "true");
  assert.ok(h.control("open", "notification-1"));
  assert.equal(await h.notifications.open("notification-other-user"), false);
  assert.equal(await h.notifications.markRead("notification-other-user"), false);
  assert.doesNotMatch(h.notifications.render(), /secret-task|notification-other-user|PAYLOAD-PRIVATE-TEXT/);
  assert.equal(h.posts().length, 0); assert.equal(h.opened.length, 0);
});

test("opens the exact server locators, strips free text, and reaches all-view page two", async () => {
  const h = harness();
  h.rows[0].payload = { task_id: "task-1", attempt_id: "attempt-1", thread_id: "thread-1", question_version_id: "q:3", student_id: "student-1", message_id: "message-1", message: "PRIVATE-MESSAGE", answers: { q: "PRIVATE-ANSWER" } };
  await h.notifications.load(); await h.click("panel"); await h.click("toggle", "notification-1"); await h.click("open", "notification-1");
  assert.equal(h.opened.length, 1);
  assert.deepEqual(h.opened[0].payload, { task_id: "task-1", attempt_id: "attempt-1", thread_id: "thread-1", question_version_id: "q:3", student_id: "student-1", message_id: "message-1" });
  assert.equal(h.rows[0].read_at, "confirmed-read"); assert.equal(h.count(), "20");
  assert.equal(h.posts().length, 1); assert.equal(h.posts()[0].url, "/me/notifications/notification-1/read");
  assert.equal(h.ids().includes("notification-1"), false);
  await h.click("filter");
  assert.match(h.html(), /第 1 页 · 全部/);
  assert.equal(h.ids().length, 20); assert.equal(h.ids()[0], "notification-1");
  assert.equal(h.control("next").disabled, false, "all view includes the read row and still has 21 rows");
  await h.click("next");
  assert.deepEqual(h.ids(), ["notification-21"]); assert.match(h.html(), /第 2 页 · 全部/); assert.equal(h.control("next").disabled, true);
  assert.ok(h.calls.some((call) => call.url === "/me/notifications?unread_only=false&limit=20&offset=20"));
  await h.click("previous"); assert.equal(h.ids().length, 20);
  await h.click("toggle", "notification-1");
  assert.equal(await h.notifications.markRead("notification-1"), true); assert.equal(h.posts().length, 1, "an already-read row does not POST again");
});

test("only literal true from onOpen permits an automatic read receipt", async () => {
  const h = harness(); await h.notifications.load(); await h.click("panel");
  for (const result of [undefined, null, false, 0, 1, "true", {}, []]) {
    h.optionsState.onOpen = async () => result;
    assert.equal(await h.notifications.open("notification-1"), false);
    assert.equal(h.posts().length, 0); assert.equal(h.count(), "21"); assert.equal(h.rows[0].read_at, undefined);
  }
  h.optionsState.onOpen = async () => { throw new Error("关联记录已撤销访问"); };
  assert.equal(await h.notifications.open("notification-1"), false); assert.equal(h.posts().length, 0);
  assert.deepEqual(h.toasts, ["关联记录已撤销访问"]);
  h.optionsState.onOpen = async () => true;
  assert.equal(await h.notifications.open("notification-1"), true); assert.equal(h.posts().length, 1);
});

test("unread count scans beyond 100 and a page-two read restarts without skipping unseen rows", async () => {
  const h = harness({ count: 121 });
  await h.notifications.load(); await h.click("panel"); assert.equal(h.count(), "121");
  assert.ok(h.calls.some((call) => call.url === "/me/notifications?unread_only=true&limit=100&offset=100"));
  await h.click("next"); assert.equal(h.ids()[0], "notification-21");
  assert.equal(await h.notifications.markRead("notification-21"), true);
  assert.match(h.html(), /第 1 页 · 未读/); assert.equal(h.count(), "120"); assert.equal(h.ids()[0], "notification-1");
  await h.click("next");
  assert.deepEqual(h.ids(), Array.from({ length: 20 }, (_, index) => `notification-${index + 22}`));
  assert.equal(h.control("previous").disabled, false);
});

test("shrinking unread pages backtrack and do not display an empty stale offset", async () => {
  const h = harness(); await h.notifications.load(); await h.click("panel"); await h.click("next");
  assert.deepEqual(h.ids(), ["notification-21"]);
  h.confirmRead("notification-21");
  assert.equal(await h.notifications.load(), true);
  assert.match(h.html(), /第 1 页 · 未读/); assert.equal(h.ids().length, 20); assert.equal(h.count(), "20");
  assert.equal(h.control("previous").disabled, true);
  assert.equal(h.posts().length, 0);
});

test("redacted notifications remain unread until explicit marking and never open cached locators", async () => {
  const h = harness(); h.rows[0].redacted = true; h.rows[0].notice = "关联内容已关闭";
  h.rows[1].payload.redacted = true;
  await h.notifications.load(); await h.click("panel");
  for (const id of ["notification-1", "notification-2"]) {
    await h.click("toggle", id);
    assert.equal(h.control("open", id).disabled, true); assert.equal(h.control("read", id).disabled, false);
    assert.equal(await h.notifications.open(id), false);
  }
  assert.equal(h.opened.length, 0); assert.equal(h.posts().length, 0);
  await h.click("read", "notification-1"); assert.equal(h.count(), "20"); assert.equal(h.opened.length, 0);
  await h.click("filter"); assert.equal(h.control("open", "notification-1").disabled, true);
  assert.match(h.html(), /关联内容已关闭/); assert.equal(await h.notifications.open("notification-1"), false);
  h.rows[0].redacted = false; await h.notifications.load();
  assert.equal(h.control("open", "notification-1").disabled, false);
  assert.equal(await h.notifications.open("notification-1"), true); assert.equal(h.posts().length, 1);
  h.rows[0].payload.redacted = true; await h.notifications.load();
  assert.equal(h.control("open", "notification-1").disabled, true, "new projection must not merge an old access decision");
});

test("unknown, mismatched and failed receipts leave unread state intact until explicit retry", async () => {
  const h = harness(); await h.notifications.load(); await h.click("panel");
  const base = plain(h.rows[0]);
  const replies = [undefined, {}, { id: base.id }, { ...base, id: "wrong", read_at: 1 }, { ...base, user_id: "student-2", read_at: 1 },
    ...[null, false, true, 0, -1, NaN, Infinity, "", " ", {}].map((read_at) => ({ ...base, read_at }))];
  for (const reply of replies) {
    h.optionsState.read = () => reply;
    const before = h.posts().length;
    assert.equal(await h.notifications.markRead(base.id), false);
    await settle(); assert.equal(h.posts().length, before + 1, "unknown receipts must not auto-retry");
    assert.equal(h.count(), "21"); assert.equal(h.rows[0].read_at, undefined); assert.ok(h.ids().includes(base.id));
  }
  h.optionsState.read = () => { throw new Error("连接中断，回执未知"); };
  assert.equal(await h.notifications.markRead(base.id), false); assert.equal(h.count(), "21");
  h.optionsState.read = undefined;
  assert.equal(await h.notifications.markRead(base.id), true); assert.equal(h.count(), "20");
});

test("one pending mark blocks duplicate marks, open, refresh and filter until confirmation", async () => {
  const gate = deferred(), h = harness({ readGate: gate }); await h.notifications.load(); await h.click("panel");
  const pending = h.notifications.markRead("notification-1"); await settle();
  const before = h.calls.length;
  assert.equal(await h.notifications.markRead("notification-1"), false); assert.equal(await h.notifications.markRead("notification-2"), false);
  assert.equal(await h.notifications.open("notification-2"), false); assert.equal(await h.notifications.load(), false);
  assert.equal(h.control("filter").disabled, true); await h.control("filter").fire("click").finished;
  assert.equal(h.calls.length, before); assert.equal(h.count(), "21");
  gate.resolve(h.confirmRead("notification-1")); assert.equal(await pending, true);
  assert.equal(h.posts().length, 1); assert.equal(h.count(), "20");
});

test("a pending resource open also blocks direct marks until the host confirms true", async () => {
  const gate = deferred(), h = harness({ onOpen: () => gate.promise }); await h.notifications.load(); await h.click("panel");
  const pending = h.notifications.open("notification-1"); await settle();
  assert.equal(await h.notifications.open("notification-2"), false);
  assert.equal(await h.notifications.load(), false);
  assert.equal(await h.notifications.markRead("notification-1"), false);
  assert.equal(await h.notifications.markRead("notification-2"), false);
  assert.equal(h.posts().length, 0); assert.equal(h.opened.length, 1);
  gate.resolve(true); assert.equal(await pending, true); assert.equal(h.posts().length, 1);
});

test("confirmed read receipts settle silently during logout and survive a failed logout", async () => {
  const gate = deferred(), h = harness({ readGate: gate }); await h.notifications.load(); await h.click("panel"); await h.click("filter");
  const pending = h.notifications.markRead("notification-1"); await settle();
  h.state.loggingOut = true;
  const paints = h.document.paints, calls = h.calls.length;
  assert.equal(await h.notifications.markRead("notification-2"), false); assert.equal(await h.notifications.open("notification-2"), false);
  assert.equal(await h.notifications.load(), false); assert.equal(h.notifications.toggle(), false);
  gate.resolve(h.confirmRead("notification-1", "confirmed-during-logout")); assert.equal(await pending, true);
  assert.equal(h.rows[0].read_at, "confirmed-during-logout");
  assert.equal(h.document.paints, paints); assert.equal(h.calls.length, calls); assert.deepEqual(h.toasts, []);
  h.state.loggingOut = false; h.render();
  assert.equal(h.count(), "20"); assert.equal(h.node('article[data-notification-id="notification-1"]').matches(".is-unread"), false);
  assert.equal(await h.notifications.markRead("notification-1"), true); assert.equal(h.posts().length, 1);
});

test("logout suppresses failed receipts and late host-open success without losing retryability", async () => {
  const gate = deferred(), h = harness({ readGate: gate }); await h.notifications.load(); await h.click("panel");
  const pending = h.notifications.markRead("notification-1"); h.state.loggingOut = true;
  const paints = h.document.paints; gate.reject(new Error("回执未知")); assert.equal(await pending, false);
  assert.equal(h.document.paints, paints); assert.deepEqual(h.toasts, []);
  h.state.loggingOut = false; h.optionsState.readGate = null; h.render();
  assert.equal(await h.notifications.markRead("notification-1"), true); assert.equal(h.posts().length, 2);
  const openGate = deferred(); h.optionsState.onOpen = () => openGate.promise;
  const opening = h.notifications.open("notification-2"); h.state.loggingOut = true;
  const before = h.document.paints; openGate.resolve(true); assert.equal(await opening, false);
  assert.equal(h.document.paints, before); assert.equal(h.posts().length, 2); assert.equal(h.rows[1].read_at, undefined);
});

test("identity and reset invalidate old write receipts and detached delegated controls", async () => {
  for (const transition of ["identity", "reset"]) {
    const gate = deferred(), h = harness({ readGate: gate }); await h.notifications.load(); await h.click("panel");
    const stale = h.control("toggle", "notification-1"), pending = h.notifications.markRead("notification-1");
    if (transition === "identity") h.api.state.user = { id: "student-2", role: "student", status: "active" };
    else h.notifications.reset();
    h.render(); await h.notifications.load(); await h.click("panel");
    const paints = h.document.paints, calls = h.calls.length, text = h.html();
    await stale.fire("click").finished;
    gate.resolve(h.confirmRead("notification-1", "old-session-receipt")); assert.equal(await pending, false);
    assert.equal(h.document.paints, paints); assert.equal(h.calls.length, calls); assert.equal(h.html(), text);
    if (transition === "identity") { assert.deepEqual(h.ids(), ["notification-other-user"]); assert.equal(h.count(), "1"); }
    else assert.equal(h.count(), "21", "an old receipt must not decrement the reset session");
  }
});

test("reset invalidates a pending host open before it can mark a new session", async () => {
  const gate = deferred(), h = harness({ onOpen: () => gate.promise }); await h.notifications.load(); await h.click("panel");
  const pending = h.notifications.open("notification-1");
  h.notifications.reset(); h.render(); await h.notifications.load(); await h.click("panel");
  const paints = h.document.paints; gate.resolve(true); assert.equal(await pending, false);
  assert.equal(h.posts().length, 0); assert.equal(h.document.paints, paints); assert.equal(h.count(), "21");
});

test("superseded and reset GET requests cannot repaint or replace a newer projection", async () => {
  for (const reset of [false, true]) {
    const h = harness(); await h.notifications.load(); await h.click("panel");
    const gate = deferred(), snapshot = plain(h.rows.filter((row) => row.user_id === "student-1"));
    h.optionsState.get = () => gate.promise;
    const pending = h.notifications.load(), oldCalls = h.calls.slice(-2);
    assert.equal(h.ids().length, 0, "refresh clears the previous projection immediately");
    if (reset) { h.notifications.reset(); h.render(); }
    h.confirmRead("notification-1"); h.optionsState.get = undefined;
    assert.equal(await h.notifications.load(), true); if (reset) await h.click("panel");
    assert.ok(oldCalls.every((call) => call.options.signal.aborted));
    const paints = h.document.paints; gate.resolve(snapshot); assert.equal(await pending, false);
    assert.equal(h.document.paints, paints); assert.equal(h.count(), "20"); assert.equal(h.ids().includes("notification-1"), false);
  }
});

test("refresh failures clear stale access while count-only failures retain the current page", async () => {
  const h = harness(); await h.notifications.load(); await h.click("panel");
  h.optionsState.get = ({ query }, response) => query.get("limit") === "100" ? Promise.reject(new Error("count unavailable")) : response();
  assert.equal(await h.notifications.load(), true); assert.equal(h.ids().length, 20); assert.equal(h.count(), "—");
  assert.match(h.html(), /未读数量暂时无法同步/);
  h.optionsState.get = () => ({ not_items: [] });
  assert.equal(await h.notifications.load(), false); assert.equal(h.ids().length, 0);
  assert.equal(await h.notifications.open("notification-1"), false); assert.equal(await h.notifications.markRead("notification-1"), false); assert.equal(h.posts().length, 0);
  h.optionsState.get = undefined; assert.equal(await h.notifications.load(), true); assert.equal(h.count(), "21");
});

test("repeating count pages fail finitely and inactive identities cannot fetch or write", async () => {
  const h = harness({ count: 101 });
  h.optionsState.get = ({ query }, response) => query.get("limit") === "100" ? plain(h.rows.slice(0, 100)) : response();
  assert.equal(await h.notifications.load(), true); assert.equal(h.count(), "—");
  assert.equal(h.calls.length, 3, "repeated pages must not create an unbounded request loop");
  for (const patch of [{ live: false }, { role: "teacher" }]) {
    Object.assign(h.state, patch); const before = h.calls.length;
    assert.equal(await h.notifications.load(), false); assert.equal(await h.notifications.markRead("notification-1"), false); assert.equal(h.calls.length, before);
    Object.assign(h.state, { live: true, role: "student" });
  }
  h.api.state.user.status = "disabled"; const before = h.calls.length;
  assert.equal(await h.notifications.load(), false); assert.equal(await h.notifications.open("notification-1"), false); assert.equal(h.calls.length, before);
  assert.doesNotMatch(h.notifications.render(), /data-notification-action/);
});

test("insight notifications retain exact locators without copying private results and require successful opening", async () => {
  const h = harness({ count: 1, onOpen: () => false });
  h.state.role = "teacher"; h.api.state.user.role = "teacher";
  Object.assign(h.rows[0], { event_type: "teaching_insight_ready", resource_type: "teaching_insight", resource_id: "insight-1",
    payload: { draft_id: "draft-1", summary: "PRIVATE-SUMMARY", result: { findings: ["PRIVATE-FINDING"] } } });
  await h.notifications.load(); await h.click("panel");
  assert.match(h.html(), /学情总结已生成/);
  assert.equal(await h.notifications.open("notification-1"), false);
  assert.deepEqual(h.opened[0].payload, { draft_id: "draft-1", insight_id: "insight-1" });
  assert.equal(h.posts().length, 0); assert.equal(h.count(), "1");
  assert.doesNotMatch(h.notifications.render(), /PRIVATE-SUMMARY|PRIVATE-FINDING/);
  h.optionsState.onOpen = () => true;
  assert.equal(await h.notifications.open("notification-1"), true);
  assert.equal(h.posts().length, 1); assert.equal(h.count(), "0");
});

test("redacted insight notifications never restore a resource locator from the envelope", async () => {
  const h = harness({ count: 1 });
  h.state.role = "teacher"; h.api.state.user.role = "teacher";
  Object.assign(h.rows[0], { event_type: "teaching_insight_failed", resource_type: "teaching_insight", resource_id: "private-insight",
    payload: { redacted: true, insight_id: "private-insight", draft_id: "private-draft" } });
  await h.notifications.load(); await h.click("panel"); await h.click("toggle", "notification-1");
  assert.match(h.html(), /学情总结未完成/);
  assert.equal(h.control("open", "notification-1").disabled, true);
  assert.equal(await h.notifications.open("notification-1"), false);
  assert.equal(h.opened.length, 0); assert.equal(h.posts().length, 0);
  assert.doesNotMatch(h.notifications.render(), /private-insight|private-draft/);
});

test("teacher class changes preserve notification ownership and do not discard a receipt", async () => {
  const gate = deferred(), h = harness({ readGate: gate }); h.state.role = "teacher"; h.state.classId = "class-1";
  h.api.state.user.role = "teacher";
  await h.notifications.load(); await h.click("panel"); await h.click("filter");
  const pending = h.notifications.markRead("notification-1"); h.state.classId = "class-2";
  gate.resolve(h.confirmRead("notification-1")); assert.equal(await pending, true);
  assert.equal(h.count(), "20"); assert.equal(h.posts().length, 1);
});
