// Administrator records and drafts belong to this instance, never to browser storage.
const ROOT = "[data-admin-workspace]";
const TABS = { teachers: "教师申请", courses: "课程申请", accounts: "账号管理", budgets: "额度管理" };
const ROLES = { teacher: "教师", student: "学生", admin: "管理员" };
const STATUSES = { active: "已启用", pending: "待审核 / 激活", disabled: "已停用" };
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const integer = (value) => Number.isSafeInteger(value) && value >= 0;
const notice = (message, tone = "info") => `<div class="mgmt-notice mgmt-notice-${tone}" role="${tone === "error" ? "alert" : "status"}">${e(message)}</div>`;
const button = (action, label, { value = "", disabled = false, primary = false, danger = false } = {}) => `<button type="button" class="mgmt-button${primary ? " mgmt-button-primary" : ""}${danger ? " mgmt-button-danger" : ""}" data-mgmt-action="${e(action)}" data-mgmt-value="${e(value)}" ${disabled ? "disabled" : ""}>${e(label)}</button>`;
const options = (values, selected, empty) => `${empty == null ? "" : `<option value="">${e(empty)}</option>`}${Object.entries(values).map(([value, label]) => `<option value="${e(value)}" ${value === selected ? "selected" : ""}>${e(label)}</option>`).join("")}`;
const badge = (label, tone = "") => `<span class="mgmt-badge${tone ? ` mgmt-badge-${tone}` : ""}">${e(label)}</span>`;
const stamp = (value) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "时间未提供";
  const date = new Date(value * 1000);
  return Number.isNaN(date.getTime()) ? "时间未提供" : date.toLocaleString("zh-CN");
};
const invalidResponse = () => Object.assign(new Error("服务端未返回可核验的完整结果，请重新查询。"), { uncertain: true });
const account = (row) => {
  if (!row || typeof row.id !== "string" || !row.id || typeof row.username !== "string" || typeof row.display_name !== "string" || !Object.hasOwn(ROLES, row.role) || !Object.hasOwn(STATUSES, row.status)) throw invalidResponse();
  return { id: row.id, username: row.username, display_name: row.display_name, role: row.role, status: row.status };
};
const accountPage = (data) => {
  if (!Array.isArray(data?.items) || !integer(data.total) || !integer(data.limit) || data.limit < 1 || data.limit > 100 || !integer(data.offset) || data.items.length > data.limit || data.total < data.items.length) throw invalidResponse();
  return { items: data.items.map(account), total: data.total, limit: data.limit, offset: data.offset };
};
const course = (row) => {
  if (!row || typeof row.id !== "string" || !row.id || typeof row.name !== "string" || typeof row.term !== "string" || typeof row.applicant_id !== "string" || !["pending", "approved", "rejected"].includes(row.status) || typeof row.review_note !== "string") throw invalidResponse();
  return Object.fromEntries(["id", "applicant_id", "name", "term", "status", "course_id", "reviewed_by", "review_note", "created_at", "reviewed_at"].map((key) => [key, row[key]]));
};
const budget = (row) => {
  if (!row || !["project", "student_daily", "teaching"].includes(row.scope_type) || typeof row.scope_id !== "string" || !row.scope_id || !["limit_units", "emergency_units", "spent_units", "reserved_units", "overage_units", "emergency_exceeded_units"].every((key) => integer(row[key])) || typeof row.configured !== "boolean") throw invalidResponse();
  return Object.fromEntries(["scope_type", "scope_id", "period", "limit_units", "emergency_units", "spent_units", "reserved_units", "overage_units", "emergency_exceeded_units", "notification_required", "course_id", "class_id", "configured"].map((key) => [key, row[key]]));
};
const budgetKey = (row) => JSON.stringify([row.scope_type, row.scope_id, row.period || null, row.course_id || null, row.class_id || null]);
const editableBudget = (row) => !row.period && !row.course_id && !row.class_id && (row.scope_type === "student_daily" || row.scope_type === "project" && row.scope_id === "platform");

export function createAdminWorkspace({ api, state, render: renderApp, toast = () => {}, onChanged }) {
  let generation = 0, context = "", session, root = null, expiryTimer = null;
  let secretEpoch = 0, logoutObserved = false;
  let current = fresh();
  const listeners = new WeakSet();
  const focusSelector = "[data-mgmt-field], [data-mgmt-action], [data-mgmt-form] button[type=submit]";

  function section() { return { items: [], loaded: false, loading: false, error: "", seq: 0, promise: null, requestUrl: "" }; }
  function fresh() {
    return {
      tab: "teachers", teachers: section(), courses: section(),
      accounts: { ...section(), total: 0, limit: 50, offset: 0, filters: { query: "", role: "", status: "" }, applied: { query: "", role: "", status: "" }, filterError: "" },
      budgets: { ...section(), currency: "", unit: "", unit_scale: null, unit_description: "" },
      editors: { teachers: null, courses: null, accounts: null, budgets: null },
      locks: new Map(), busy: null, secret: null, feedback: "", feedbackTone: "info", accessError: "", selfDisabled: false,
    };
  }
  function contextKey() {
    const user = api?.state?.user;
    return JSON.stringify([state.live, state.role, state.transitionId, state.screen, user?.id || "", user?.role || "", user?.status || "", state.session?.id, state.session?.generation]);
  }
  function sessionActive() {
    const user = api?.state?.user;
    return state.live === true && state.role === "admin" && Boolean(user?.id) && (!user.role || user.role === "admin") && !["disabled", "pending"].includes(user.status) && (!state.screen || state.screen === "app");
  }
  function active() { return sessionActive() && !state.loggingOut; }
  function clearSecret() {
    if (expiryTimer != null) clearTimeout(expiryTimer);
    expiryTimer = null;
    if (current.secret) current.secret.code = "";
    current.secret = null;
  }
  function observeLogout() {
    if (state.loggingOut) {
      // A failed logout keeps the session and its locks, but must never restore an issued or in-flight code.
      if (!logoutObserved) secretEpoch += 1;
      clearSecret();
    }
    logoutObserved = Boolean(state.loggingOut);
  }
  function syncContext() {
    const next = contextKey();
    if (next !== context || session !== state.session) {
      clearSecret(); generation += 1; context = next; session = state.session; current = fresh();
      if (root) root.innerHTML = "";
    }
    observeLogout();
    return generation;
  }
  function valid(token) { return token === generation && context === contextKey() && session === state.session && sessionActive(); }
  function reset() {
    clearSecret(); generation += 1; secretEpoch += 1; logoutObserved = false; context = ""; session = undefined; current = fresh();
    if (root) root.innerHTML = "";
    root = null;
  }
  function errorText(error) { return error?.message || "请求未完成，填写内容已保留。"; }
  function denied(error) {
    if (![401, 403].includes(error?.status)) return;
    clearSecret(); current.accessError = error.status === 401 ? "管理员会话已失效，请退出后重新登录。" : "当前账号已无权访问管理员功能，请重新核对登录身份。";
    for (const name of Object.keys(TABS)) { current[name].items = []; current[name].loaded = false; }
  }
  function focusIdentity(node) {
    if (!node?.getAttribute) return null;
    return ["data-mgmt-field", "data-mgmt-scope", "data-mgmt-action", "data-mgmt-value"].map((key) => node.getAttribute(key)).concat(node.tagName, node.closest?.("[data-mgmt-form]")?.getAttribute("data-mgmt-form") || "").join("\u0000");
  }
  function repaint() {
    if (context !== contextKey() || session !== state.session) return;
    if (!root?.isConnected) { if (active() && typeof renderApp === "function") renderApp(); return; }
    const focused = root.ownerDocument?.activeElement;
    const identity = focused && root.contains(focused) ? focusIdentity(focused) : null;
    const start = focused?.selectionStart, end = focused?.selectionEnd;
    root.innerHTML = renderInner();
    if (identity) {
      const next = Array.from(root.querySelectorAll(focusSelector)).find((node) => focusIdentity(node) === identity);
      if (next && !next.disabled && !next.matches?.(":disabled")) {
        next.focus?.({ preventScroll: true });
        if (typeof start === "number" && next.setSelectionRange) { try { next.setSelectionRange(start, end); } catch { /* Selects and buttons have no text selection. */ } }
      }
    }
  }
  function renderWorkspace() { syncContext(); return `<section class="mgmt-workspace" data-admin-workspace>${renderInner()}</section>`; }
  function renderInner() {
    observeLogout();
    if (state.loggingOut && sessionActive()) return notice("正在退出，管理员操作暂不可用。一次性码已清除，未确认请求不会重复提交。");
    if (!active()) return notice(state.live ? "仅已登录的管理员可访问此工作区。" : "正式服务未连接，连接后才能读取管理员工作区。", "warn");
    if (current.accessError) return `${notice(current.accessError, "error")}${current.selfDisabled ? "" : button("reload", "重新检查权限")}`;
    if (current.secret && Date.now() >= current.secret.expiresAt) clearSecret();
    return `<nav class="mgmt-tabs" aria-label="管理员功能">${Object.entries(TABS).map(([key, label]) => `<button type="button" class="mgmt-tab" data-mgmt-action="tab" data-mgmt-value="${key}" aria-current="${key === current.tab ? "page" : "false"}" ${current.busy ? "disabled" : ""}>${label}</button>`).join("")}</nav>
      ${current.feedback ? notice(current.feedback, current.feedbackTone) : ""}${renderSecret()}
      <div class="mgmt-surface">${({ teachers: renderTeachers, courses: renderCourses, accounts: renderAccounts, budgets: renderBudgets })[current.tab]()}</div>`;
  }
  function toolbar(name, title, description, actions = "") {
    return `<header class="mgmt-toolbar"><div><h2 id="mgmt-${name}-title">${e(title)}</h2><p class="mgmt-muted">${e(description)}</p></div><div class="mgmt-actions">${actions}${button("refresh", "刷新", { value: name, disabled: Boolean(current.busy || current[name].loading) })}</div></header>`;
  }
  function readState(name, emptyText) {
    const data = current[name];
    if (data.error) return notice(`${data.error}${data.loaded ? " 下方保留上次成功读取的记录。" : ""}`, "error");
    if (data.loading) return `<p class="mgmt-muted" role="status">正在读取${e(TABS[name])}…</p>`;
    if (!data.loaded) return `<div class="mgmt-empty">尚未读取${e(TABS[name])}，请点击刷新。</div>`;
    return data.items.length ? "" : `<div class="mgmt-empty">${e(emptyText)}</div>`;
  }
  function accountName(row) { return `<strong>${e(row.display_name || row.username)}</strong><small class="mgmt-muted">${e(row.username)}</small><small class="mgmt-id">${e(row.id)}</small>`; }
  function rowDisabled(name) { return Boolean(current.busy || current[name].loading || current[name].error); }
  function renderTeachers() {
    const data = current.teachers;
    return `${toolbar("teachers", "待审核教师申请", "通过后账号直接启用，申请人使用申请时设置的口令登录。拒绝须填写理由。")}${readState("teachers", "暂无待审核教师申请。")}
      ${data.items.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>待审核教师账号</caption><thead><tr><th scope="col">教师 / 登录账号</th><th scope="col">状态</th><th scope="col">审核</th></tr></thead><tbody>${data.items.map((row) => `<tr><th scope="row">${accountName(row)}</th><td>${badge("待审核")}</td><td><div class="mgmt-actions">${button("teacher-approve", "通过", { value: row.id, disabled: rowDisabled("teachers") })}${button("teacher-reject", "拒绝", { value: row.id, danger: true, disabled: rowDisabled("teachers") })}</div></td></tr>`).join("")}</tbody></table></div>` : ""}${renderEditor("teachers")}`;
  }
  function renderCourses() {
    const pending = current.courses.items.filter((row) => row.status === "pending"), history = current.courses.items.filter((row) => row.status !== "pending");
    return `${toolbar("courses", "课程申请审核", "通过后创建课程并确定申请人为课程负责人；拒绝须记录理由。")}${readState("courses", "暂无课程申请。")}
      ${pending.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>待审核课程申请</caption><thead><tr><th scope="col">课程</th><th scope="col">学期</th><th scope="col">申请人 / 时间</th><th scope="col">审核</th></tr></thead><tbody>${pending.map((row) => `<tr><th scope="row">${e(row.name)}<small class="mgmt-id">${e(row.id)}</small></th><td>${e(row.term)}</td><td><span class="mgmt-id">${e(row.applicant_id)}</span><small class="mgmt-muted">${e(stamp(row.created_at))}</small></td><td><div class="mgmt-actions">${button("course-approve", "通过", { value: row.id, disabled: rowDisabled("courses") })}${button("course-reject", "拒绝", { value: row.id, danger: true, disabled: rowDisabled("courses") })}</div></td></tr>`).join("")}</tbody></table></div>` : current.courses.loaded && !current.courses.error && history.length ? '<div class="mgmt-empty">暂无待审核课程申请。</div>' : ""}
      ${renderEditor("courses")}${history.length ? `<details class="mgmt-history"><summary>已审核课程申请（${history.length}）</summary><div class="mgmt-table-wrap"><table class="mgmt-table"><thead><tr><th scope="col">课程 / 学期</th><th scope="col">结果</th><th scope="col">审核说明</th></tr></thead><tbody>${history.map((row) => `<tr><th scope="row">${e(row.name)}<small class="mgmt-muted">${e(row.term)}</small></th><td>${badge(row.status === "approved" ? "已通过" : "已拒绝", row.status === "approved" ? "ok" : "warn")}${row.course_id ? `<small class="mgmt-id">${e(row.course_id)}</small>` : ""}</td><td class="mgmt-prewrap">${e(row.review_note || "未填写说明")}</td></tr>`).join("")}</tbody></table></div></details>` : ""}`;
  }
  function field(scope, key, label, value, opts = {}) {
    const attrs = `name="${e(key)}" data-mgmt-field="${e(key)}" data-mgmt-scope="${e(scope)}" ${opts.required ? "required" : ""}`;
    const control = opts.options ? `<select ${attrs}>${options(opts.options, value, opts.empty)}</select>` : opts.textarea ? `<textarea ${attrs} rows="3" maxlength="${opts.max || 500}">${e(value)}</textarea>` : `<input ${attrs} type="${opts.type || "text"}" value="${e(value)}" ${opts.numeric ? 'inputmode="numeric" pattern="[0-9]+"' : ""} ${opts.max ? `maxlength="${opts.max}"` : ""}>`;
    return `<label class="mgmt-field"><span>${e(label)}</span>${control}${opts.hint ? `<small>${e(opts.hint)}</small>` : ""}</label>`;
  }
  function renderAccounts() {
    const data = current.accounts, filters = data.filters;
    return `${toolbar("accounts", "账号管理", "按登录账号或姓名查找。待审核或待激活账号须完成对应流程，不能直接恢复。")}
      <form class="mgmt-filters" data-mgmt-form="filters"><fieldset class="mgmt-fields" ${current.busy ? "disabled" : ""}><div class="mgmt-form-grid">${field("filters", "query", "搜索登录账号或姓名", filters.query, { type: "search", max: 200 })}${field("filters", "role", "角色", filters.role, { options: ROLES, empty: "全部角色" })}${field("filters", "status", "状态", filters.status, { options: STATUSES, empty: "全部状态" })}<div class="mgmt-actions"><button type="submit" class="mgmt-button mgmt-button-primary">查找</button>${button("clear-filters", "重置筛选")}</div></div></fieldset></form>
      ${data.filterError ? notice(data.filterError, "error") : ""}${readState("accounts", Object.values(data.applied).some(Boolean) ? "没有匹配的账号，请调整搜索或筛选条件。" : "暂无可显示的账号。")}
      ${data.loaded ? `<p class="mgmt-muted">找到 ${e(data.total)} 个账号，每页 ${e(data.limit)} 个。</p>` : ""}
      ${data.items.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>账号查询结果</caption><thead><tr><th scope="col">姓名 / 登录账号</th><th scope="col">角色</th><th scope="col">状态</th><th scope="col">管理</th></tr></thead><tbody>${data.items.map((row) => `<tr><th scope="row">${accountName(row)}${row.id === api.state.user.id ? '<small class="mgmt-muted">当前登录账号</small>' : ""}</th><td>${e(ROLES[row.role])}</td><td>${badge(row.status === "pending" ? (row.role === "teacher" ? "待审核" : "待激活") : STATUSES[row.status], row.status === "active" ? "ok" : "warn")}</td><td><div class="mgmt-actions">${row.status === "active" ? `${button("account-disable", "停用", { value: row.id, danger: true, disabled: rowDisabled("accounts") })}${button("reset-code", "发放口令重设码", { value: row.id, disabled: rowDisabled("accounts") })}` : row.status === "disabled" ? button("account-restore", "恢复", { value: row.id, disabled: rowDisabled("accounts") }) : row.role === "teacher" ? button("pending-teachers", "前往教师审核", { disabled: Boolean(current.busy) }) : '<span class="mgmt-muted">由班级教师完成激活码交付</span>'}${row.role === "student" ? button("student-budget", "设置全局日额", { value: row.id, disabled: rowDisabled("accounts") }) : ""}</div></td></tr>`).join("")}</tbody></table></div>` : ""}
      ${data.loaded ? `<div class="mgmt-pagination"><span>第 ${Math.floor(data.offset / data.limit) + 1} 页 / 共 ${Math.max(1, Math.ceil(data.total / data.limit))} 页</span><div class="mgmt-actions">${button("page", "上一页", { value: "prev", disabled: Boolean(current.busy || data.loading || data.offset === 0) })}${button("page", "下一页", { value: "next", disabled: Boolean(current.busy || data.loading || data.offset + data.limit >= data.total) })}</div></div>` : ""}${renderEditor("accounts")}`;
  }
  function budgetMetadata() { const data = current.budgets; return data.loaded && data.currency && data.unit === "billing_unit" && data.unit_scale === 1; }
  function units(value) { return integer(value) ? value.toLocaleString("zh-CN") : "未提供"; }
  function budgetTitle(row) {
    if (row.scope_type === "project" && row.scope_id === "platform") return "平台总额";
    if (row.scope_type === "student_daily") return `${row.class_id ? "班级学生日额" : row.course_id ? "课程学生日额" : "学生全局日额"} · ${row.scope_id}`;
    return `${row.scope_type === "teaching" ? "教学额度" : "项目额度"} · ${row.scope_id}`;
  }
  function renderBudgets() {
    const data = current.budgets;
    return `${toolbar("budgets", "额度管理", "调整平台总额和学生全局日额。设置上限不会充值；班级策略和按日使用记录在此只读。", `${button("platform-budget", "设置平台总额", { disabled: Boolean(current.busy || !budgetMetadata() || data.loading || data.error) })}${button("find-student-budget", "查找学生设置日额", { disabled: Boolean(current.busy) })}`)}
      ${readState("budgets", "尚未配置额度策略。空列表不表示已有可用额度。")}${data.loaded ? notice(`计费币种：${data.currency}。按原始整数计费单位显示，单位倍率为 ${data.unit_scale}。${data.unit_description ? ` ${data.unit_description}` : ""} 不换算为人民币或其他现金金额。`) : ""}
      ${data.items.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>服务端已存额度策略与使用记录（整数计费单位）</caption><thead><tr><th scope="col">范围 / 周期</th><th scope="col">上限 / 应急额</th><th scope="col">已用 / 预留</th><th scope="col">超额</th><th scope="col">管理</th></tr></thead><tbody>${data.items.map((row, index) => `<tr><th scope="row">${e(budgetTitle(row))}<small class="mgmt-muted">${e(row.period || (row.scope_type === "student_daily" ? "默认每日上限" : "累计上限"))}</small>${row.class_id ? `<small class="mgmt-id">班级 ${e(row.class_id)}</small>` : ""}${!row.configured ? badge("未配置", "warn") : ""}</th><td>${units(row.limit_units)}<small class="mgmt-muted">应急 ${units(row.emergency_units)}</small></td><td>${units(row.spent_units)}<small class="mgmt-muted">预留 ${units(row.reserved_units)}</small></td><td>${units(row.overage_units)}${row.emergency_exceeded_units ? `<small class="mgmt-muted">超出应急额 ${units(row.emergency_exceeded_units)}</small>` : ""}${row.notification_required ? badge("需关注", "warn") : ""}</td><td>${editableBudget(row) ? button("edit-budget", "调整", { value: String(index), disabled: rowDisabled("budgets") || !budgetMetadata() }) : '<span class="mgmt-muted">只读</span>'}</td></tr>`).join("")}</tbody></table></div>` : ""}${renderEditor("budgets")}`;
  }
  function confirmation(scope, value, label) { return `<label class="mgmt-check"><input type="checkbox" name="confirmed" data-mgmt-field="confirmed" data-mgmt-scope="${scope}" ${value ? "checked" : ""} required><span>${e(label)}</span></label>`; }
  function renderEditor(name) {
    const editor = current.editors[name];
    if (!editor) return "";
    const target = editor.target, blocked = Boolean(current.busy || editor.checking || editor.lock || editor.completed);
    let title, explanation, controls, confirmLabel, submitLabel;
    if (name === "teachers" || name === "courses") {
      const subject = name === "teachers" ? `${target.display_name}（${target.username}）` : `${target.name}（${target.term}）`;
      title = `${editor.approve ? "通过" : "拒绝"}${name === "teachers" ? "教师" : "课程"}申请`;
      explanation = `${subject}。${editor.approve ? name === "teachers" ? "通过后该账号可使用申请时设置的口令登录。" : "通过后将创建课程，并设置申请人为课程负责人。" : "拒绝理由会保存到审核记录。"}`;
      controls = field(name, "note", editor.approve ? "审核说明（选填，最多 500 字）" : "拒绝理由（必填，最多 500 字）", editor.data.note, { textarea: true, required: !editor.approve, max: 500 });
      confirmLabel = `我已核对申请，确认${editor.approve ? "通过" : "拒绝"}。`; submitLabel = `确认${editor.approve ? "通过" : "拒绝"}`;
    } else if (name === "accounts") {
      title = editor.kind === "code" ? "发放口令重设码" : editor.status === "disabled" ? "停用账号" : "恢复账号";
      explanation = `${target.display_name}（${target.username}），${ROLES[target.role]}。${editor.kind === "code" ? "签发后须将一次性码交给本人；本人使用代码设置新口令后才完成重设。" : editor.status === "disabled" ? "停用会使该账号不能继续登录，并使既有会话失效。" : "恢复后该账号可使用原口令登录。"}${target.id === api.state.user.id && editor.status === "disabled" ? " 这是当前登录账号，停用后需退出并使用其他管理员账号登录。" : ""}`;
      controls = `<p class="mgmt-id">账号编号 ${e(target.id)}</p>`; confirmLabel = `我已核对账号与身份，确认${title}。`; submitLabel = `确认${title}`;
    } else {
      title = `调整${editor.label || budgetTitle(target)}`;
      explanation = `币种 ${current.budgets.currency || "尚未读取"}，填写原始整数计费单位。${target.scope_type === "student_daily" ? "这是跨课程的默认每日上限；不设置班级范围，应急额固定为 0。" : "这是 platform 平台累计上限。"}`;
      controls = `<div class="mgmt-form-grid">${field(name, "limit_units", "额度上限", editor.data.limit_units, { numeric: true, required: true, max: 16, hint: "非负整数；0 表示上限为 0，不表示不限额。" })}${target.scope_type === "project" ? field(name, "emergency_units", "应急额度", editor.data.emergency_units, { numeric: true, required: true, max: 16 }) : ""}</div>`;
      confirmLabel = "我已核对额度对象、范围和计费单位，确认保存。"; submitLabel = "保存额度";
    }
    return `<section class="mgmt-editor" aria-labelledby="mgmt-${name}-editor-title"><header class="mgmt-toolbar"><h3 id="mgmt-${name}-editor-title">${e(title)}</h3>${button("close-editor", "关闭", { value: name, disabled: Boolean(current.busy || editor.checking) })}</header><p class="mgmt-muted">${e(explanation)}</p>
      ${editor.error ? notice(editor.error, "error") : ""}${editor.completed ? notice(editor.checkMessage, "ok") : ""}${editor.lock ? `${notice(editor.checkMessage || "提交结果尚未确认，已停止重复提交。请先重新查询当前状态。", "warn")}${button("recheck", editor.checking ? "正在查询…" : "重新查询结果", { value: name, disabled: Boolean(current.busy || editor.checking) })}` : ""}
      <form data-mgmt-form="${name}"><fieldset class="mgmt-fields" ${blocked ? "disabled" : ""}>${controls}${confirmation(name, editor.data.confirmed, confirmLabel)}<div class="mgmt-form-footer"><button type="submit" class="mgmt-button ${editor.approve === false || editor.status === "disabled" ? "mgmt-button-danger" : "mgmt-button-primary"}" ${blocked ? "disabled" : ""}>${current.busy?.section === name ? "正在提交…" : e(submitLabel)}</button>${editor.lock ? '<span class="mgmt-muted">本次请求不会自动重发，填写内容已保留。</span>' : ""}</div></fieldset></form></section>`;
  }
  function renderSecret() {
    const secret = current.secret;
    if (!secret) return "";
    return `<section class="mgmt-secret" aria-labelledby="mgmt-secret-title"><h3 id="mgmt-secret-title">口令重设码已签发</h3><p>${e(secret.target.display_name)}（${e(secret.target.username)}）</p><label class="mgmt-field"><span>一次性口令重设码</span><textarea class="mgmt-code" data-mgmt-code readonly rows="2" autocomplete="off" spellcheck="false">${e(secret.code)}</textarea></label><p class="mgmt-muted">有效时长 ${e(secret.ttl)} 秒；预计到期 ${e(stamp(secret.expiresAt / 1000))}。账号口令尚未更改。</p><p class="mgmt-muted">请交给账号本人。代码只在当前页面会话中保留，切换页签或退出时清除；清除显示不会撤销已签发代码，也不会清空系统剪贴板。</p><div class="mgmt-actions">${button("copy-code", "复制代码")}${button("clear-code", "清除显示")}</div>${secret.copyMessage ? notice(secret.copyMessage, secret.copyFailed ? "warn" : "info") : ""}</section>`;
  }

  function urlFor(name) {
    if (name === "teachers") return "/auth/teacher-applications";
    if (name === "courses") return "/course-applications";
    if (name === "budgets") return "/budgets/limits";
    const data = current.accounts;
    return `/auth/accounts?${new URLSearchParams({ ...data.applied, limit: String(data.limit), offset: String(data.offset) })}`;
  }
  async function loadSection(name, token = generation) {
    if (!valid(token) || !active() || current.selfDisabled) return false;
    const data = current[name], url = urlFor(name);
    if (data.loading && data.requestUrl === url) return data.promise;
    const sequence = ++data.seq;
    data.loading = true; data.error = ""; data.requestUrl = url; repaint();
    const task = (async () => {
      try {
        const response = await api.call("GET", url);
        if (!valid(token) || data.seq !== sequence || current.accessError) return false;
        if (name === "accounts") Object.assign(data, accountPage(response));
        else if (name === "budgets") {
          if (!Array.isArray(response?.items) || typeof response.currency !== "string" || !response.currency.trim() || response.unit !== "billing_unit" || response.unit_scale !== 1) throw invalidResponse();
          data.items = response.items.map(budget); data.currency = response.currency; data.unit = response.unit; data.unit_scale = response.unit_scale; data.unit_description = typeof response.unit_description === "string" ? response.unit_description : "";
        } else {
          if (!Array.isArray(response)) throw invalidResponse();
          data.items = response.map(name === "teachers" ? account : course);
          if (name === "teachers" && data.items.some((row) => row.role !== "teacher" || row.status !== "pending")) throw invalidResponse();
        }
        data.loaded = true; return true;
      } catch (error) {
        if (valid(token) && data.seq === sequence) { data.error = errorText(error); denied(error); }
        return false;
      } finally {
        if (valid(token) && data.seq === sequence) { data.loading = false; data.promise = null; repaint(); }
      }
    })();
    data.promise = task;
    return task;
  }
  async function load() {
    const token = syncContext();
    if (!active() || current.selfDisabled) { repaint(); return; }
    current.accessError = "";
    await Promise.all(Object.keys(TABS).map((name) => loadSection(name, token)));
    if (valid(token)) repaint();
    // A host refresh calls load(). Only confirmed mutations call onChanged.
  }
  function bind(container) {
    syncContext();
    const next = container?.matches?.(ROOT) ? container : container?.querySelector?.(ROOT);
    root = next || null;
    if (!next || listeners.has(next)) return;
    listeners.add(next);
    next.addEventListener("click", onClick);
    next.addEventListener("input", onField);
    next.addEventListener("change", onField);
    next.addEventListener("submit", onSubmit);
  }
  function owned(node, event) { return node && root?.contains(node) && (!event.currentTarget || event.currentTarget === root) && node.closest?.(ROOT) === root; }
  function eventReady() {
    if (context !== contextKey() || session !== state.session) { syncContext(); repaint(); return false; }
    observeLogout();
    return active() && !current.accessError && !current.selfDisabled;
  }
  function onField(event) {
    const node = event.target?.closest?.("[data-mgmt-field]");
    if (!owned(node, event) || !eventReady() || current.busy) return;
    const key = node.dataset.mgmtField, scope = node.dataset.mgmtScope;
    if (scope === "filters") { if (Object.hasOwn(current.accounts.filters, key)) current.accounts.filters[key] = node.value; return; }
    const editor = current.editors[scope];
    if (!editor || editor.lock || editor.checking || editor.completed || !Object.hasOwn(editor.data, key)) return;
    editor.data[key] = node.type === "checkbox" ? node.checked : node.value;
  }
  function openEditor(name, props) {
    const key = commandKey(name, props);
    const previous = current.editors[name];
    const data = previous && !previous.completed && previous.key === key && previous.kind === props.kind && previous.approve === props.approve && previous.status === props.status ? previous.data : props.data;
    current.editors[name] = { ...props, data, key, error: "", checking: false, completed: false, lock: current.locks.get(key) || null };
    current.feedback = ""; repaint();
    root?.querySelector(".mgmt-editor input, .mgmt-editor textarea")?.focus?.({ preventScroll: true });
  }
  function commandKey(name, editor) {
    return name === "budgets" ? `budgets:${budgetKey(editor.target)}` : `${name}:${editor.kind === "code" ? "code:" : ""}${editor.target.id}`;
  }
  async function openBudget(target, label) {
    const token = generation;
    if (!current.budgets.loaded) await loadSection("budgets", token);
    if (!valid(token) || !active() || current.accessError) return;
    if (!budgetMetadata() || current.budgets.error) { current.feedback = "额度元数据尚未成功读取，请先刷新额度管理。"; current.feedbackTone = "error"; current.tab = "budgets"; repaint(); return; }
    const saved = current.budgets.items.find((row) => budgetKey(row) === budgetKey(target));
    clearSecret(); current.tab = "budgets";
    openEditor("budgets", { target: { scope_type: target.scope_type, scope_id: target.scope_id, period: null, class_id: null, course_id: null }, label, data: { limit_units: saved?.configured ? String(saved.limit_units) : "", emergency_units: saved?.configured ? String(saved.emergency_units) : "0", confirmed: false } });
  }
  async function onClick(event) {
    const node = event.target?.closest?.("[data-mgmt-action]");
    if (!owned(node, event) || node.disabled) return;
    event.stopPropagation?.();
    const action = node.dataset.mgmtAction, value = node.dataset.mgmtValue;
    if (action === "reload" && active() && context === contextKey() && session === state.session) { await load(); return; }
    if (!eventReady()) return;
    if (action === "clear-code") { clearSecret(); repaint(); return; }
    if (action === "copy-code") { await copyCode(); return; }
    if (current.busy) return;
    if (action === "tab" && Object.hasOwn(TABS, value)) {
      if (value !== current.tab) { clearSecret(); current.feedback = ""; current.tab = value; }
      repaint(); if (!current[value].loaded) await loadSection(value); return;
    }
    if (action === "refresh" && Object.hasOwn(TABS, value)) { await loadSection(value); return; }
    if (action === "close-editor" && current.editors[value] && !current.editors[value].checking) { current.editors[value] = null; repaint(); return; }
    if (action === "recheck" && current.editors[value]) { await recheck(value); return; }
    if (action === "pending-teachers") { clearSecret(); current.tab = "teachers"; repaint(); await loadSection("teachers"); return; }
    if (action === "find-student-budget") {
      clearSecret(); current.tab = "accounts"; current.accounts.filters = { query: "", role: "student", status: "" }; await searchAccounts(); return;
    }
    if (action === "clear-filters") { current.accounts.filters = { query: "", role: "", status: "" }; await searchAccounts(); return; }
    if (action === "page" && !current.accounts.loading) {
      const data = current.accounts;
      if (value === "prev" && data.offset > 0) data.offset = Math.max(0, data.offset - data.limit);
      else if (value === "next" && data.offset + data.limit < data.total) data.offset += data.limit;
      else return;
      await loadSection("accounts"); return;
    }
    if (action === "platform-budget") { await openBudget({ scope_type: "project", scope_id: "platform" }, "平台总额"); return; }
    if (action === "edit-budget") {
      const row = current.budgets.items[Number(value)];
      if (row && editableBudget(row) && !rowDisabled("budgets")) await openBudget(row);
      return;
    }
    if (["teacher-approve", "teacher-reject", "course-approve", "course-reject"].includes(action)) {
      const name = action.startsWith("teacher") ? "teachers" : "courses", row = current[name].items.find((item) => item.id === value && item.status === "pending");
      if (row && !rowDisabled(name)) openEditor(name, { target: { ...row }, approve: action.endsWith("approve"), data: { note: "", confirmed: false } });
      return;
    }
    const row = current.accounts.items.find((item) => item.id === value);
    if (!row || rowDisabled("accounts")) return;
    if (action === "student-budget" && row.role === "student") { await openBudget({ scope_type: "student_daily", scope_id: row.id }, `学生全局日额：${row.display_name}（${row.username}）`); return; }
    if (action === "reset-code" && row.status === "active") {
      if (current.secret) { toast("请先交付并清除当前一次性码，再签发新码。"); return; }
      openEditor("accounts", { target: { ...row }, kind: "code", data: { confirmed: false } });
    } else if (action === "account-disable" && row.status === "active" || action === "account-restore" && row.status === "disabled") {
      openEditor("accounts", { target: { ...row }, kind: "status", status: action === "account-disable" ? "disabled" : "active", data: { confirmed: false } });
    }
  }
  async function searchAccounts() {
    const data = current.accounts, filters = data.filters;
    if (filters.query.trim().length > 200 || filters.role && !Object.hasOwn(ROLES, filters.role) || filters.status && !Object.hasOwn(STATUSES, filters.status)) { data.filterError = "请使用有效筛选条件，搜索内容最多 200 字。"; repaint(); return; }
    data.filterError = ""; data.applied = { query: filters.query.trim(), role: filters.role, status: filters.status }; data.offset = 0;
    await loadSection("accounts");
  }
  function buildCommand(name, editor) {
    if (!editor.data.confirmed) throw new Error("请先核对操作对象并勾选确认。");
    let method = "POST", path, body, expected;
    if (name === "teachers" || name === "courses") {
      if (!current[name].items.some((row) => row.id === editor.target.id && row.status === "pending") || current[name].error || current[name].loading) throw new Error("待审核状态尚未确认或已变化，请先刷新列表。填写内容已保留。");
      const note = String(editor.data.note || "").trim();
      if (Array.from(note).length > 500 || !editor.approve && !note) throw new Error(editor.approve ? "审核说明最多 500 字。" : "请填写 1–500 字的拒绝理由。");
      path = name === "teachers" ? `/auth/teacher-applications/${encodeURIComponent(editor.target.id)}/${editor.approve ? "approve" : "reject"}` : `/course-applications/${encodeURIComponent(editor.target.id)}/review`;
      body = name === "teachers" ? { note } : { approve: editor.approve, note };
      expected = name === "teachers" ? editor.approve ? "active" : "disabled" : editor.approve ? "approved" : "rejected";
    } else if (name === "accounts") {
      const row = current.accounts.items.find((item) => item.id === editor.target.id);
      if (!row || row.status !== editor.target.status || row.role !== editor.target.role || current.accounts.error || current.accounts.loading) throw new Error("账号状态尚未确认或已变化，请先重新查找并核对账号。");
      if (editor.kind === "code") {
        if (row.status !== "active" || current.secret) throw new Error("只能为已启用账号签发口令重设码，请先清除已有代码显示。");
        path = "/auth/password-reset-codes"; body = { user_id: row.id };
      } else {
        if (!["active", "disabled"].includes(row.status)) throw new Error("待审核或待激活账号不能直接停用或恢复。");
        method = "PATCH"; path = `/auth/users/${encodeURIComponent(row.id)}/status`; body = { status: editor.status }; expected = editor.status;
      }
    } else {
      if (!budgetMetadata() || current.budgets.error || current.budgets.loading || !editableBudget(editor.target)) throw new Error("请先成功读取计费单位和额度范围，再保存额度。");
      const readInteger = (value) => { const text = String(value).trim(); if (!/^\d+$/.test(text) || !integer(Number(text))) throw new Error("额度须为非负安全整数，不接受小数、负数或科学计数法。"); return Number(text); };
      const limit = readInteger(editor.data.limit_units), emergency = editor.target.scope_type === "student_daily" ? 0 : readInteger(editor.data.emergency_units);
      if (!integer(limit + emergency)) throw new Error("额度上限与应急额度之和超出可精确表示的整数范围。");
      method = "PUT"; path = "/budgets/limits";
      body = { scope_type: editor.target.scope_type, scope_id: editor.target.scope_id, class_id: null, period: null, limit_units: limit, emergency_units: emergency };
    }
    return Object.freeze({ method, path, body: Object.freeze(body), expected, section: name, key: editor.key, kind: editor.kind, target: Object.freeze({ ...editor.target }) });
  }
  async function onSubmit(event) {
    const form = event.target?.closest?.("[data-mgmt-form]");
    if (!owned(form, event)) return;
    event.preventDefault(); event.stopPropagation?.();
    if (!eventReady() || current.busy) return;
    const name = form.dataset.mgmtForm;
    if (name === "filters") { await searchAccounts(); return; }
    const editor = current.editors[name];
    if (!editor || editor.lock || editor.checking || editor.completed) return;
    let command;
    try { command = buildCommand(name, editor); }
    catch (error) { editor.error = errorText(error); repaint(); return; }
    const token = generation, codeEpoch = secretEpoch;
    current.busy = command; editor.error = ""; current.feedback = ""; repaint();
    let confirmed = false;
    try {
      const result = await api.call(command.method, command.path, command.body);
      if (!valid(token) || current.editors[name] !== editor) return;
      observeLogout();
      applyResult(command, result, codeEpoch);
      confirmed = true; current.editors[name] = null; current.locks.delete(command.key); current.feedbackTone = "ok";
      if (active()) toast(current.feedback);
      repaint();
    } catch (error) {
      if (valid(token) && current.editors[name] === editor) {
        editor.error = errorText(error);
        if (error?.uncertain || !Number.isFinite(error?.status) || error.status === 0 || error.status === 408 || error.status >= 500) {
          const lock = { command }; current.locks.set(command.key, lock); editor.lock = lock;
          editor.checkMessage = "提交结果尚未确认，已停止重复提交。请先重新查询当前状态。";
        }
        denied(error);
      }
    } finally {
      if (valid(token) && current.busy === command) { current.busy = null; repaint(); }
    }
    if (confirmed && valid(token) && active()) {
      try { if (typeof onChanged === "function") await onChanged(); else await loadSection(name, token); }
      catch { if (valid(token)) { current.feedback += " 相关记录刷新未完成，请手动刷新核对。"; current.feedbackTone = "warn"; repaint(); } }
    }
  }
  function applyResult(command, response, codeEpoch) {
    const { section: name, target, body, expected } = command;
    if (name === "teachers") {
      if (response?.user_id !== target.id || response.status !== expected || response.review_note !== body.note) throw invalidResponse();
      current.teachers.items = current.teachers.items.filter((row) => row.id !== target.id);
      current.feedback = expected === "active" ? "教师申请已通过，账号已启用，可使用申请时设置的口令登录。" : "教师申请已拒绝，理由已记录。";
    } else if (name === "courses") {
      const row = course(response);
      if (row.id !== target.id || row.status !== expected || row.review_note !== body.note || expected === "approved" && !row.course_id) throw invalidResponse();
      current.courses.items = current.courses.items.map((item) => item.id === row.id ? row : item);
      current.feedback = expected === "approved" ? `课程申请已通过，课程“${row.name}”已创建。` : "课程申请已拒绝，理由已记录。";
    } else if (name === "accounts" && command.kind === "code") {
      if (response?.user_id !== target.id || typeof response.reset_code !== "string" || response.reset_code.length < 20 || response.reset_code.length > 256 || !integer(response.expires_in_seconds) || response.expires_in_seconds < 1 || response.expires_in_seconds > 2147483) throw invalidResponse();
      clearSecret();
      if (codeEpoch !== secretEpoch || state.loggingOut) {
        current.feedback = "口令重设码已签发；因已开始退出，代码已丢弃且不会恢复显示。账号口令尚未更改，请核对签发记录。";
        return;
      }
      const secret = { code: response.reset_code, target, ttl: response.expires_in_seconds, expiresAt: Date.now() + response.expires_in_seconds * 1000, copyMessage: "" };
      current.secret = secret;
      const token = generation;
      expiryTimer = setTimeout(() => { if (valid(token) && current.secret === secret) { clearSecret(); repaint(); } }, secret.ttl * 1000);
      current.feedback = "口令重设码已签发，请交付给账号本人。账号口令尚未更改。";
    } else if (name === "accounts") {
      const row = account(response);
      if (row.id !== target.id || row.status !== expected || row.role !== target.role) throw invalidResponse();
      current.accounts.items = current.accounts.items.map((item) => item.id === row.id ? row : item);
      current.feedback = expected === "disabled" ? "账号已停用。" : "账号已恢复。";
      if (row.id === api.state.user.id && row.status === "disabled") { clearSecret(); current.selfDisabled = true; current.accessError = "当前账号已停用，请退出并使用其他管理员账号登录。"; }
    } else {
      const row = budget(response);
      if (budgetKey(row) !== budgetKey(body) || row.limit_units !== body.limit_units || row.emergency_units !== body.emergency_units || !row.configured) throw invalidResponse();
      current.budgets.items = [...current.budgets.items.filter((item) => budgetKey(item) !== budgetKey(row)), row];
      current.feedback = "额度上限已保存，未进行充值或现金兑换。";
    }
  }
  function completeRecheck(editor, command, message) {
    // Release the resource for a fresh command, but retire this editor so it cannot replay the old one.
    if (current.locks.get(command.key) === editor.lock) current.locks.delete(command.key);
    editor.lock = null; editor.completed = true; editor.data.confirmed = false; editor.checkMessage = message;
  }
  async function recheck(name) {
    const editor = current.editors[name];
    if (!editor?.lock || editor.checking || current.busy) return;
    const command = editor.lock.command, token = generation;
    editor.checking = true; editor.error = ""; repaint();
    try {
      let found;
      if (name === "teachers" || name === "accounts") {
        const query = new URLSearchParams({ query: command.target.username, role: command.target.role, status: "", limit: "100", offset: "0" });
        const page = accountPage(await api.call("GET", `/auth/accounts?${query}`));
        found = page.items.find((row) => row.id === command.target.id);
      } else if (name === "courses") {
        const result = await api.call("GET", "/course-applications");
        if (!Array.isArray(result)) throw invalidResponse();
        found = result.map(course).find((row) => row.id === command.target.id);
      } else {
        const result = await api.call("GET", "/budgets/limits");
        if (!Array.isArray(result?.items)) throw invalidResponse();
        found = result.items.map(budget).find((row) => budgetKey(row) === budgetKey(command.body));
      }
      if (!valid(token) || current.editors[name] !== editor) return;
      if (command.kind === "code") editor.checkMessage = `${found ? `账号当前${STATUSES[found.status]}。` : "尚未查到目标账号。"}账号查询不能取回一次性码或确认签发结果，请核对审计记录。本次会话不会重复签发。`;
      else if (!found) editor.checkMessage = "当前查询未找到该记录，不能确认原操作结果。请稍后重新查询，不会重复提交。";
      else if (name === "budgets") {
        if (found.limit_units === command.body.limit_units && found.emergency_units === command.body.emergency_units && found.configured) {
          current.budgets.items = [...current.budgets.items.filter((row) => budgetKey(row) !== budgetKey(found)), found];
          completeRecheck(editor, command, "当前存储额度与提交值一致。本次操作已结束，旧表单不能再次提交；如需继续调整，请关闭后重新打开并确认新额度。");
        } else editor.checkMessage = `当前上限 ${units(found.limit_units)}、应急额 ${units(found.emergency_units)}，与提交值尚不一致。请稍后重新查询，不会重复提交。`;
      } else {
        const label = name === "courses" ? { pending: "待审核", approved: "已通过", rejected: "已拒绝" }[found.status] : STATUSES[found.status];
        editor.checkMessage = `重新查询显示当前状态为“${label}”。${found.status === command.expected ? "目标状态已满足，本次请求不再提交；原请求及审核说明以审计记录为准。可关闭表单。" : "原操作结果仍需核对，请稍后重新查询，不会重复提交。"}`;
        if (name === "teachers" && found.status !== "pending") current.teachers.items = current.teachers.items.filter((row) => row.id !== found.id);
        if (name === "courses" || name === "accounts") current[name].items = current[name].items.map((row) => row.id === found.id ? found : row);
        if (name === "accounts" && command.kind === "status" && found.status === command.expected && found.role === command.target.role) {
          completeRecheck(editor, command, `重新查询显示当前状态为“${label}”，目标状态已满足。本次操作已结束，旧表单不能再次提交；后续操作请关闭后重新打开并确认。`);
          if (found.id === api.state.user.id && found.status === "disabled") { clearSecret(); current.selfDisabled = true; current.accessError = "当前账号已停用，请退出并使用其他管理员账号登录。"; }
        }
      }
    } catch (error) { if (valid(token) && current.editors[name] === editor) { editor.error = errorText(error); denied(error); } }
    finally { if (valid(token) && current.editors[name] === editor) { editor.checking = false; repaint(); } }
  }
  async function copyCode() {
    const secret = current.secret, token = generation;
    if (!secret) return;
    if (Date.now() >= secret.expiresAt) { clearSecret(); repaint(); return; }
    try {
      if (!globalThis.navigator?.clipboard?.writeText) throw new Error("clipboard unavailable");
      await globalThis.navigator.clipboard.writeText(secret.code);
      if (valid(token) && current.secret === secret) { secret.copyMessage = "代码已复制。交付后请清除本页显示，并留意系统剪贴板中的副本。"; secret.copyFailed = false; repaint(); }
    } catch {
      if (valid(token) && current.secret === secret) { secret.copyMessage = "自动复制未完成，请选中代码后手动复制。"; secret.copyFailed = true; repaint(); root?.querySelector("[data-mgmt-code]")?.select?.(); }
    }
  }

  return { render: renderWorkspace, bind, load, reset };
}
