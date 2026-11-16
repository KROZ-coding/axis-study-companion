const PAGE_SIZE = 20;
const COUNT_PAGE_SIZE = 100;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({
  "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
}[character]));
const encode = (value) => encodeURIComponent(String(value));
const hasId = (value) => typeof value === "string" && value.trim().length > 0;
const userFor = (api, state) => api?.state && Object.prototype.hasOwnProperty.call(api.state, "user")
  ? api.state.user : state?.user;
const EVENT_LABELS = {
  task_published: "任务已发布", task_opened: "任务已开放", results_ready: "成绩已开放", grading_completed: "成绩已开放",
  grading_manual_required: "等待教师处理", discussion_message: "反馈与讨论有新消息", discussion_reply: "反馈与讨论有新消息",
  discussion_teacher_message: "教师发来了短评", discussion_student_reply: "学生回复了讨论",
  appeal_received: "收到评分异议", appeal_review_ready: "评分异议有处理结果",
  teacher_judgment_recorded: "教师更新了学习记录", question_status_changed: "题目状态有变化",
};
const LOCATORS = ["task_id", "attempt_id", "thread_id", "question_version_id", "question_id", "class_id", "course_id", "goal_id", "student_id", "appeal_id", "message_id"];
const confirmedRead = (value) => (typeof value === "number" && Number.isFinite(value) && value > 0) || (typeof value === "string" && value.trim().length > 0);
function makeState() {
  return {
    notifications: [], collapsed: true, unreadOnly: true, offset: 0, limit: PAGE_SIZE,
    unreadCount: null, hasMore: false, loading: false, error: "", message: "",
    expanded: new Set(), pendingRead: new Set(), pendingOpen: new Set(),
  };
}
function notificationTime(value) {
  if (!value) return "时间未提供";
  const numeric = Number(value), date = Number.isFinite(numeric) ? new Date(numeric * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? "时间未提供" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export function createLearningNotifications({ api, state, render: renderApp = () => {}, toast = () => {}, onOpen = () => {} } = {}) {
  let context = "", epoch = 0, readSequence = 0, controller = null, root = null, focusSnapshot = null;
  let current = makeState();
  const bindings = new WeakMap();
  function sessionKey() {
    const user = userFor(api, state);
    // Changing a teacher's class does not change ownership of their notifications.
    // loggingOut locks interaction without discarding the current session's receipts.
    return JSON.stringify([state?.live, state?.role, state?.transitionId, user?.id, user?.role, user?.status]);
  }
  function identityActive() {
    const user = userFor(api, state);
    return state?.live === true && ["student", "teacher"].includes(state?.role) && hasId(user?.id)
      && user.role === state.role && user.status === "active";
  }
  const active = () => identityActive() && !state?.loggingOut;
  const sessionCurrent = (token, data) => token === epoch && data === current && context === sessionKey() && identityActive();
  const busy = () => current.pendingRead.size > 0 || current.pendingOpen.size > 0;
  function cancelReads() {
    readSequence += 1;
    controller?.abort();
    controller = null;
    current.loading = false;
  }
  function syncContext() {
    if (context !== sessionKey()) {
      cancelReads();
      context = sessionKey();
      epoch += 1;
      current = makeState();
      focusSnapshot = null;
    }
    return epoch;
  }
  function reset() {
    cancelReads();
    epoch += 1;
    context = "";
    current = makeState();
    focusSnapshot = null;
    if (root) root.innerHTML = "";
    root = null;
  }
  function project(item) {
    if (!hasId(item?.id) || item.user_id !== userFor(api, state)?.id) return null;
    const redacted = item.redacted === true || item.payload?.redacted === true;
    const source = !redacted && item.payload && typeof item.payload === "object" && !Array.isArray(item.payload) ? item.payload : {};
    const payload = Object.fromEntries(LOCATORS.filter((key) => hasId(source[key])).map((key) => [key, source[key]]));
    if (!redacted) {
      if (!payload.question_version_id && hasId(source.question_version)) payload.question_version_id = source.question_version;
      if (!payload.task_id && item.resource_type === "task" && hasId(item.resource_id)) payload.task_id = item.resource_id;
      if (!payload.thread_id && ["thread", "discussion_thread"].includes(item.resource_type) && hasId(item.resource_id)) payload.thread_id = item.resource_id;
    }
    // Keep routing locators only. A notification is never a second copy of answers or messages.
    return {
      id: item.id, user_id: item.user_id, event_type: item.event_type,
      resource_type: item.resource_type, resource_id: item.resource_id, payload,
      read_at: confirmedRead(item.read_at) ? item.read_at : null, created_at: item.created_at,
      redacted, notice: typeof item.notice === "string" ? item.notice : "",
    };
  }
  function captureFocus() {
    const binding = root && bindings.get(root), focused = root?.ownerDocument?.activeElement;
    if (!binding || binding.token !== epoch || binding.data !== current || root.isConnected === false) return;
    focusSnapshot = root.contains?.(focused) ? [focused.getAttribute("data-notification-action"), focused.getAttribute("data-notification-id")] : null;
  }
  function restoreFocus() {
    if (!focusSnapshot || !root) return;
    let target = Array.from(root.querySelectorAll?.("button") || []).find((node) => JSON.stringify([node.getAttribute("data-notification-action"), node.getAttribute("data-notification-id")]) === JSON.stringify(focusSnapshot));
    if (!target || target.disabled) target = root.querySelector?.('[data-notification-action="panel"]');
    target?.focus?.({ preventScroll: true });
  }
  function repaint() {
    if (context !== sessionKey() || !active()) return;
    captureFocus();
    if (root && root.isConnected !== false) {
      const scrollTop = root.scrollTop;
      root.innerHTML = contentHtml();
      root.scrollTop = scrollTop;
      bind(root);
    } else renderApp();
    restoreFocus();
  }
  async function call(method, path, body, options) {
    if (typeof api?.call !== "function") throw new Error("正式服务接口未配置");
    return api.call(method, path, body, options);
  }
  const readCurrent = (read) => sessionCurrent(read.token, read.data) && read.sequence === readSequence && !read.signal.aborted;
  async function readPage(unreadOnly, offset, limit, read) {
    if (!readCurrent(read) || !active()) throw new Error("读取已结束");
    const query = new URLSearchParams({ unread_only: String(unreadOnly), limit: String(limit), offset: String(offset) });
    const result = await call("GET", `/me/notifications?${query}`, undefined, { signal: read.signal });
    if (!readCurrent(read)) throw new Error("读取已结束");
    const raw = Array.isArray(result) ? result : result?.items;
    if (!Array.isArray(raw)) throw new Error("通知暂未读全，请刷新重试。");
    return { length: raw.length, rows: raw.map(project).filter((row) => row && (!unreadOnly || !row.read_at)) };
  }
  async function unreadCount(read) {
    const ids = new Set();
    for (let offset = 0; ; offset += COUNT_PAGE_SIZE) {
      const page = await readPage(true, offset, COUNT_PAGE_SIZE, read);
      const before = ids.size;
      page.rows.forEach((row) => ids.add(row.id));
      if (page.length < COUNT_PAGE_SIZE) return ids.size;
      if (before === ids.size) throw new Error("未读数量暂时无法同步，请稍后刷新。");
    }
  }
  async function load(options = {}) {
    syncContext();
    if (!active() || busy()) return false;
    cancelReads();
    controller = new AbortController();
    const data = current, read = { token: epoch, data, sequence: readSequence, signal: controller.signal };
    if (options.resetPage === true) data.offset = 0;
    const unreadOnly = data.unreadOnly;
    data.loading = true;
    data.error = data.message = "";
    // A failed refresh must not leave a previously accessible notification projection on screen.
    data.notifications = [];
    data.hasMore = false;
    data.unreadCount = null;
    repaint();
    try {
      const [pageResult, countResult] = await Promise.allSettled([readPage(unreadOnly, data.offset, data.limit, read), unreadCount(read)]);
      if (!readCurrent(read)) return false;
      if (pageResult.status === "rejected") throw pageResult.reason;
      let page = pageResult.value;
      // A shrinking unread queue may no longer have this offset.
      while (!page.length && data.offset > 0 && active()) {
        data.offset = Math.max(0, data.offset - data.limit);
        page = await readPage(unreadOnly, data.offset, data.limit, read);
      }
      if (!readCurrent(read)) return false;
      data.notifications = page.rows;
      data.hasMore = page.length >= data.limit;
      if (countResult.status === "fulfilled") data.unreadCount = countResult.value;
      else data.error = "当前页已刷新，未读数量暂时无法同步。";
      return active();
    } catch (error) {
      if (readCurrent(read)) data.error = error?.message || "通知暂时无法读取，请刷新重试。";
      return false;
    } finally {
      if (readCurrent(read)) { data.loading = false; repaint(); }
    }
  }
  async function markRead(id) {
    syncContext();
    if (!active() || busy() || !hasId(id)) return false;
    const notification = current.notifications.find((row) => row.id === id);
    if (!notification) return false;
    if (notification.read_at) return true;
    cancelReads();
    const token = epoch, data = current;
    data.pendingRead.add(id);
    data.error = "";
    repaint();
    try {
      const response = project(await call("POST", `/me/notifications/${encode(id)}/read`));
      if (!sessionCurrent(token, data)) return false;
      if (!response || response.id !== id || !response.read_at) throw new Error("服务器尚未确认已读，请重试标记。");
      data.notifications = data.notifications.map((row) => row.id === id ? response : row);
      if (typeof data.unreadCount === "number") data.unreadCount = Math.max(0, data.unreadCount - 1);
      data.message = "通知已标为已读。";
      // Do not splice an offset-based unread page: doing so would skip the next
      // unseen row. Re-read from the first page after a confirmed mark instead.
      if (data.unreadOnly) { data.offset = 0; data.notifications = []; data.hasMore = false; }
      data.pendingRead.delete(id);
      if (active()) {
        repaint();
        if (data.unreadOnly && !data.pendingOpen.size) await load({ resetPage: true });
      }
      return true;
    } catch (error) {
      if (sessionCurrent(token, data)) {
        data.pendingRead.delete(id);
        data.error = error?.message || "通知未标为已读，请重试。";
        repaint();
      }
      return false;
    }
  }
  async function openNotification(id) {
    syncContext();
    if (!active() || busy()) return false;
    const notification = current.notifications.find((row) => row.id === id);
    if (!notification || notification.redacted) return false;
    const token = epoch, data = current;
    data.pendingOpen.add(id);
    data.expanded.add(id);
    repaint();
    try {
      // Opening/expanding/loading alone is not a read receipt. The host must
      // explicitly confirm that the scoped resource was successfully read.
      const opened = await onOpen({ ...notification, payload: { ...notification.payload } });
      if (!sessionCurrent(token, data) || !active() || opened !== true) return false;
      data.pendingOpen.delete(id);
      return await markRead(id);
    } catch (error) {
      if (sessionCurrent(token, data)) {
        data.error = error?.message || "关联内容暂时无法打开。";
        if (active()) { repaint(); toast(data.error); }
      }
      return false;
    } finally {
      if (sessionCurrent(token, data)) {
        data.pendingOpen.delete(id);
        if (active()) repaint();
      }
    }
  }
  // No id toggles the whole panel (initially collapsed); an id toggles one row.
  function toggle(id) {
    syncContext();
    if (!active()) return false;
    if (id === undefined) current.collapsed = !current.collapsed;
    else {
      if (!current.notifications.some((row) => row.id === id)) return false;
      if (current.expanded.has(id)) current.expanded.delete(id); else current.expanded.add(id);
    }
    repaint();
    return true;
  }
  function notificationHtml(item) {
    const expanded = current.expanded.has(item.id), unread = !item.read_at;
    const reading = current.pendingRead.has(item.id), opening = current.pendingOpen.has(item.id);
    const disabled = !active() || busy();
    return `<article class="learning-notification${unread ? " is-unread" : ""}${expanded ? " is-expanded" : ""}" data-notification-id="${esc(item.id)}"><button type="button" class="learning-notification-summary" data-notification-action="toggle" data-notification-id="${esc(item.id)}" aria-expanded="${expanded}" aria-controls="notification-detail-${esc(item.id)}" ${!active() ? "disabled" : ""}><span class="learning-notification-dot" aria-hidden="true"></span><span class="learning-notification-copy"><strong>${esc(EVENT_LABELS[item.event_type] || "学习记录有更新")}</strong><small>${esc(notificationTime(item.created_at))} · ${unread ? "未读" : "已读"}</small></span><span aria-hidden="true" class="learning-notification-chevron">${expanded ? "−" : "+"}</span></button>${expanded ? `<div class="learning-notification-detail" id="notification-detail-${esc(item.id)}"><p>${esc(item.redacted ? item.notice || "关联内容当前不可访问，标为已读不会开放内容。" : "打开关联页面，查看这条学习记录的更新。")}</p><div class="learning-notification-actions"><button type="button" class="feedback-button primary" data-notification-action="open" data-notification-id="${esc(item.id)}" ${disabled || item.redacted ? "disabled" : ""}>${opening ? "正在打开…" : "打开关联内容"}</button>${unread ? `<button type="button" class="feedback-button" data-notification-action="read" data-notification-id="${esc(item.id)}" ${disabled ? "disabled" : ""}>${reading ? "标记中…" : "标为已读"}</button>` : ""}</div></div>` : ""}</article>`;
  }
  function contentHtml() {
    if (!identityActive()) return "";
    const disabled = current.loading || busy() || !active(), mode = current.unreadOnly ? "未读" : "全部";
    return `<header class="learning-notifications-header"><h2><button type="button" class="learning-notifications-toggle" data-notification-action="panel" aria-expanded="${!current.collapsed}" aria-controls="learning-notifications-content" ${!active() ? "disabled" : ""}>学习通知 <span class="learning-notifications-count" aria-label="未读通知数">${esc(current.unreadCount ?? "—")}</span><span class="learning-notifications-toggle-label">${current.collapsed ? "展开" : "收起"}</span></button></h2>${!current.collapsed ? `<div class="learning-notifications-actions"><button type="button" class="feedback-button" data-notification-action="filter" aria-pressed="${current.unreadOnly}" ${disabled ? "disabled" : ""}>${current.unreadOnly ? "查看全部" : "只看未读"}</button><button type="button" class="feedback-button" data-notification-action="refresh" ${disabled ? "disabled" : ""}>${current.loading ? "刷新中…" : "刷新"}</button></div>` : ""}</header>${current.error ? `<p class="feedback-error" role="alert">${esc(current.error)}</p>` : ""}${!current.collapsed ? `<div id="learning-notifications-content" class="learning-notifications-content" aria-busy="${current.loading}">${current.message ? `<p class="feedback-success" role="status">${esc(current.message)}</p>` : ""}<div class="learning-notification-list">${current.notifications.length ? current.notifications.map(notificationHtml).join("") : `<p class="feedback-muted">${current.loading ? "正在读取通知…" : current.unreadOnly ? "没有未读通知。" : "还没有通知记录。"}</p>`}</div><footer class="learning-notifications-footer"><button type="button" class="feedback-button" data-notification-action="previous" ${disabled || current.offset <= 0 ? "disabled" : ""}>上一页</button><span>第 ${Math.floor(current.offset / current.limit) + 1} 页 · ${mode}</span><button type="button" class="feedback-button" data-notification-action="next" ${disabled || !current.hasMore ? "disabled" : ""}>下一页</button></footer></div>` : ""}`;
  }
  function renderNotifications() {
    syncContext();
    captureFocus();
    return `<section class="learning-notifications" data-learning-notifications aria-label="学习通知">${contentHtml()}</section>`;
  }
  function bind(container) {
    const surface = container?.matches?.("[data-learning-notifications]") ? container : container?.querySelector?.("[data-learning-notifications]");
    if (!surface) return;
    root = surface;
    const old = bindings.get(surface);
    bindings.set(surface, { token: epoch, data: current });
    if (old) { restoreFocus(); return; }
    surface.addEventListener("click", (event) => {
      const binding = bindings.get(surface);
      if (root !== surface || surface.isConnected === false || !surface.contains(event.target) || !sessionCurrent(binding.token, binding.data) || !active()) return;
      const target = event.target.closest?.("[data-notification-action]");
      if (!target || target.disabled) return;
      const action = target.dataset.notificationAction, id = target.dataset.notificationId;
      if (action === "panel") return toggle();
      if (action === "toggle") return toggle(id);
      if (action === "read") return markRead(id);
      if (action === "open") return openNotification(id);
      if (busy()) return;
      if (action === "filter") { current.unreadOnly = !current.unreadOnly; return load({ resetPage: true }); }
      if (action === "refresh") return load();
      if (action === "previous" && current.offset > 0) { current.offset = Math.max(0, current.offset - current.limit); return load(); }
      if (action === "next" && current.hasMore) { current.offset += current.limit; return load(); }
    });
    restoreFocus();
  }
  return { render: renderNotifications, bind, load, reset, markRead, open: openNotification, toggle };
}
