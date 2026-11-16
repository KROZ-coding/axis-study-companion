// Formal teacher management. Drafts and one-time codes belong only to this module instance.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const rows = (value) => {
  const result = Array.isArray(value) ? value : value?.items;
  if (!Array.isArray(result)) throw new Error("服务返回的列表不完整，请刷新核对。");
  return result;
};
const identifier = (value) => typeof value === "string" && value.length > 0;
const encode = (value) => encodeURIComponent(value);
const statusLabel = { pending: "待激活", active: "有效", disabled: "已停用" };
const applicationLabel = { pending: "待审核", approved: "已通过", rejected: "未通过" };
const notice = (message, tone = "info") => message ? `<div class="mgmt-notice mgmt-notice-${tone}" role="${tone === "error" ? "alert" : "status"}">${esc(message)}</div>` : "";
const button = (action, label, { disabled = false, primary = false, danger = false, id = "" } = {}) => `<button type="button" class="mgmt-button${primary ? " mgmt-button-primary" : ""}${danger ? " mgmt-button-danger" : ""}" data-mgmt-action="${esc(action)}"${id ? ` data-mgmt-id="${esc(id)}"` : ""}${disabled ? " disabled" : ""}>${esc(label)}</button>`;
const field = (scope, name, label, value, attrs = "") => `<label class="mgmt-field">${esc(label)}<input data-mgmt-scope="${esc(scope)}" data-mgmt-field="${esc(name)}" name="${esc(scope)}-${esc(name)}" value="${esc(value)}" ${attrs}></label>`;
const section = () => ({ items: [], ready: false, error: "" });
const stamp = (value) => {
  if (typeof value !== "number" || !Number.isFinite(value)) return "时间未提供";
  return new Date(value * 1000).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};

export function createTeacherManagement({ api, state, render: renderApp = () => {}, toast = () => {}, onChanged }) {
  let context = "", epoch = 0, readSequence = 0, root = null;
  let current = fresh();
  const listeners = new WeakSet();
  const bindings = new WeakMap();

  function fresh(tab = "courses") {
    return {
      tab, loading: false, switching: false, pending: null, uncertain: null, confirmation: null,
      error: "", message: "", code: null, copied: false,
      courses: section(), applications: section(), allClasses: section(), classes: section(), members: section(), budgets: section(),
      courseId: "", classId: "", currency: "", unitDescription: "", budgetAccess: false,
      application: { name: "", term: "" }, classroom: { name: "", owner_teacher_id: "" },
      student: { username: "", display_name: "" }, member: { user_id: "" }, budgetDrafts: new Map(),
    };
  }

  const user = () => api?.state?.user;
  const isTeacher = () => user()?.role === "teacher";
  const sessionActive = () => state.live === true && ["teacher", "admin"].includes(state.role)
    && user()?.role === state.role && identifier(user()?.id) && user()?.status === "active";
  const active = () => sessionActive() && !state.loggingOut;
  function observeLogout() {
    if (!state.loggingOut) return;
    current.code = null; current.copied = false;
    // A failed logout must not erase the command or later reveal a code from that command.
    if (current.pending) current.pending.discardCode = true;
  }
  const key = () => JSON.stringify([state.live, state.role, state.transitionId, state.courseId || "", state.classId || "",
    user()?.id || "", user()?.role || "", user()?.status || ""]);
  function syncContext() {
    const next = key();
    if (next !== context) {
      context = next; epoch += 1; readSequence += 1;
      current = fresh(current.tab);
    }
    observeLogout();
    return epoch;
  }
  // Logout closes interaction, but an in-flight receipt still settles against the same session.
  const valid = (token, data = current) => token === epoch && data === current && context === key() && sessionActive();
  function reset() {
    epoch += 1; readSequence += 1; context = ""; current = fresh();
    if (root?.isConnected) root.innerHTML = "";
    root = null;
  }
  function leave() {
    syncContext();
    if (current.pending || current.uncertain || current.switching) {
      const message = current.pending ? "操作正在提交，请等待结果后再离开教学管理。"
        : current.uncertain ? "上一次操作结果尚未核对，请完成“先核对上一次操作”后再离开。"
          : "班级正在切换，请等待完成后再离开教学管理。";
      current.error = message; repaint(); toast(message);
      return false;
    }
    reset();
    return true;
  }
  const course = () => current.courses.items.find((item) => item.id === current.courseId);
  const classroom = () => current.classes.items.find((item) => item.id === current.classId);
  const member = (id) => current.members.items.find((item) => item.user_id === id);
  const canCreateClass = () => active() && current.courses.ready && course()?.can_create_classes === true;
  const canManageClass = () => active() && current.classes.ready && classroom()?.can_manage === true;
  const canManageMembers = () => canManageClass() && current.members.ready;
  const canSetBudget = () => canManageMembers() && current.budgets.ready && current.budgetAccess === true && Boolean(current.currency);
  const locked = () => Boolean(current.loading || current.switching || current.pending || current.uncertain);
  const editingLocked = () => Boolean(current.loading || current.switching || current.pending);
  const draftLocked = () => Boolean(current.switching || current.pending);
  const classPath = (suffix = "") => `/classes/${encode(current.classId)}${suffix}`;
  const findBudget = (id) => current.budgets.items.find((item) => item.scope_type === "student_daily" && item.scope_id === id
    && item.class_id === current.classId && item.course_id === current.courseId && item.period == null && item.configured === true);
  const amountText = (value) => Number.isSafeInteger(value) && value >= 0 ? String(value) : "未提供";

  function focusIdentity(node) {
    if (!node?.getAttribute) return null;
    return ["data-mgmt-field", "data-mgmt-scope", "data-mgmt-action", "data-mgmt-id", "name"].map((name) => node.getAttribute(name));
  }
  function repaint() {
    if (context !== key() || !sessionActive()) return;
    observeLogout();
    if (!root?.isConnected) { renderApp(); return; }
    const focused = root.ownerDocument?.activeElement;
    const identity = root.contains?.(focused) ? focusIdentity(focused) : null;
    const start = focused?.selectionStart, end = focused?.selectionEnd;
    root.innerHTML = renderInner();
    bind(root);
    if (identity?.some((part) => part != null)) {
      const target = Array.from(root.querySelectorAll("[data-mgmt-field], [data-mgmt-action], button[type=submit]"))
        .find((node) => JSON.stringify(focusIdentity(node)) === JSON.stringify(identity));
      if (target && !target.disabled && !target.matches?.(":disabled")) {
        target.focus?.({ preventScroll: true });
        if (typeof start === "number" && target.setSelectionRange && ["text", "search"].includes(target.type)) {
          try { target.setSelectionRange(start, end); } catch { /* Selection is unavailable for some input types. */ }
        }
      }
    }
  }

  function acceptList(target, result, filter = () => true) {
    target.items = []; target.ready = false; target.error = "";
    try {
      if (result.status === "rejected") throw result.reason;
      target.items = rows(result.value).filter(filter); target.ready = true;
    } catch (error) { target.error = error?.message || "读取失败，请刷新重试。"; }
  }

  async function load() {
    const token = syncContext(), data = current;
    if (!active()) { if (state.loggingOut) repaint(); return; }
    if (data.pending) return;
    const sequence = ++readSequence;
    const live = () => valid(token, data) && sequence === readSequence;
    data.loading = true; data.switching = false;
    repaint();
    try {
      const result = await Promise.allSettled([
        api.call("GET", "/courses"), api.call("GET", "/course-applications"), api.call("GET", "/classes"),
      ]);
      if (!live()) return;
      acceptList(data.courses, result[0], (item) => identifier(item?.id));
      acceptList(data.applications, result[1], (item) => identifier(item?.id) && item.applicant_id === user().id);
      acceptList(data.allClasses, result[2], (item) => identifier(item?.id) && identifier(item?.course_id));
      // Class rights are independent of course-material rights. Never invent material-write permission.
      for (const item of data.allClasses.items) {
        if (!data.courses.items.some((entry) => entry.id === item.course_id)) {
          data.courses.items.push({ id: item.course_id, name: "班级所属课程", term: "", can_manage_materials: false });
        }
      }
      const chosenCourse = data.courses.items.find((item) => item.id === (state.courseId || data.courseId)) || data.courses.items[0];
      const nextCourse = chosenCourse?.id || "";
      if (data.courseId && data.courseId !== nextCourse) clearClassDrafts(data);
      data.courseId = nextCourse;
      data.classes = section(); data.members = section(); data.budgets = section(); data.currency = ""; data.unitDescription = ""; data.budgetAccess = false;
      if (!nextCourse) { data.classId = ""; data.code = null; return; }
      const classResult = await Promise.allSettled([api.call("GET", `/courses/${encode(nextCourse)}/classes`)]);
      if (!live()) return;
      acceptList(data.classes, classResult[0], (item) => identifier(item?.id) && item.course_id === nextCourse);
      if (!data.classes.ready) { data.classId = ""; data.code = null; return; }
      const chosenClass = data.classes.items.find((item) => item.id === (state.classId || data.classId)) || data.classes.items[0];
      const nextClass = chosenClass?.id || "";
      if (data.classId && data.classId !== nextClass) clearClassDrafts(data);
      data.classId = nextClass;
      if (!nextClass) { data.code = null; return; }
      if (chosenClass.can_manage !== true) data.code = null;
      // A manage collaborator may administer students but cannot even read the owner-only budget endpoint.
      data.budgetAccess = chosenClass.can_set_student_budgets === true;
      const details = await Promise.allSettled([
        api.call("GET", `/classes/${encode(nextClass)}/members`),
        ...(data.budgetAccess ? [api.call("GET", `/budgets/limits?class_id=${encode(nextClass)}`)] : []),
      ]);
      if (!live()) return;
      acceptList(data.members, details[0], (item) => identifier(item?.user_id));
      if (data.budgetAccess) {
        acceptList(data.budgets, details[1], (item) => item?.scope_type === "student_daily" && identifier(item.scope_id));
        if (data.budgets.ready) {
          const payload = details[1].value;
          data.unitDescription = typeof payload.unit_description === "string" ? payload.unit_description : "";
          if (typeof payload.currency === "string" && payload.currency.trim() && payload.unit === "billing_unit" && payload.unit_scale === 1) {
            data.currency = payload.currency.trim();
          } else data.budgets.error = "额度币种或单位信息不完整，暂不能设置日额。请联系管理员确认配置。";
        }
      }
      if (data.code) {
        const target = data.members.items.find((item) => item.user_id === data.code.userId);
        if (!data.members.ready || !target || target.status !== (data.code.kind === "activation" ? "pending" : "active")) data.code = null;
      }
      if (data.uncertain?.kind === "transfer") {
        const destination = await Promise.allSettled([api.call("GET", `/classes/${encode(data.uncertain.body.target_class_id)}/members`)]);
        if (!live()) return;
        data.uncertain.destination = section();
        acceptList(data.uncertain.destination, destination[0]);
      }
    } finally {
      if (live()) {
        data.loading = false;
        reconcileLoadedResult(data);
        repaint();
      }
    }
  }

  function clearClassDrafts(data) {
    data.code = null; data.copied = false; data.confirmation = null;
    data.student = { username: "", display_name: "" }; data.member = { user_id: "" }; data.budgetDrafts.clear();
  }

  function reconcileLoadedResult(data) {
    const pending = data.uncertain;
    if (!pending) return;
    pending.checked = false; pending.found = false;
    const payload = pending.body;
    if (pending.kind === "application" && data.applications.ready) {
      pending.checked = true; pending.found = data.applications.items.some((item) => item.name === payload.name && item.term === payload.term && !pending.before.includes(item.id));
    } else if (pending.kind === "class" && data.classes.ready) {
      pending.checked = true; pending.found = data.classes.items.some((item) => item.name === payload.name && item.owner_teacher_id === payload.owner_teacher_id && !pending.before.includes(item.id));
    } else if (data.members.ready && pending.classId === data.classId) {
      if (pending.kind === "student") {
        pending.checked = true; pending.found = data.members.items.some((item) => item.username?.toLocaleLowerCase() === payload.username.toLocaleLowerCase());
      } else if (["add", "remove"].includes(pending.kind)) {
        pending.checked = true; pending.found = data.members.items.some((item) => item.user_id === pending.userId) === (pending.kind === "add");
      } else if (["activation", "reset"].includes(pending.kind)) {
        pending.checked = true;
      } else if (pending.kind === "transfer" && pending.destination?.ready) {
        pending.checked = true; pending.found = !data.members.items.some((item) => item.user_id === pending.userId)
          && pending.destination.items.some((item) => item.user_id === pending.userId);
      } else if (pending.kind === "budget" && data.budgets.ready) {
        pending.checked = true; pending.found = findBudget(pending.userId)?.limit_units === payload.limit_units;
      }
    }
    pending.result = pending.found ? "已查询到与本次操作一致的记录，请核对下方列表。"
      : ["activation", "reset"].includes(pending.kind) && pending.checked
        ? "已刷新学生状态。历史一次性码无法查询，请先确认学生是否已收到；如仍需要，请返回名单后重新确认签发。"
        : pending.checked ? "查询完成，暂未查到与本次操作一致的记录。请核对列表和输入，再决定是否重新提交。"
          : "尚未完成结果核对，请先刷新查询；输入已保留。";
  }

  function showError(message) { current.error = message; repaint(); }
  function validateForm(kind) {
    if (kind === "application") {
      if (!isTeacher()) return "只有有效教师账号可以申请课程。";
      if (!current.applications.ready) return "请先刷新本人申请记录，再提交新申请。";
      const { name, term } = current.application;
      if (!name.trim() || name.trim().length > 200 || !term.trim() || term.trim().length > 64) return "请填写课程名称（最多 200 字）和学期（最多 64 字）。";
      if (current.applications.items.some((item) => item.name === name.trim() && item.term === term.trim() && ["pending", "approved"].includes(item.status))) return "同名同学期的申请已在审核中或已通过，请查看申请记录。";
    } else if (kind === "class") {
      if (!canCreateClass()) return "当前没有这门课程的建班权限。";
      if (!current.classes.ready) return "请先刷新班级列表，再创建班级。";
      const owner = isTeacher() ? user().id : current.classroom.owner_teacher_id.trim();
      if (!current.classroom.name.trim() || current.classroom.name.trim().length > 100) return "请填写班级名称，最多 100 字。";
      if (!owner || owner.length > 100 || (!isTeacher() && owner === user().id)) return "请填写有效教师的账号 ID，管理员不能作为班级负责人。";
      if (current.classes.items.some((item) => item.name === current.classroom.name.trim() && item.owner_teacher_id === owner)) return "这位负责人的同名班级已存在，请先核对班级列表。";
    } else if (kind === "student") {
      if (!canManageMembers()) return "当前没有班级成员管理权限，请刷新核对。";
      if (!/^[A-Za-z0-9_.-]{3,32}$/.test(current.student.username.trim())) return "登录名须为 3–32 位字母、数字、下划线、点或连字符。";
      if (!current.student.display_name.trim() || current.student.display_name.trim().length > 64) return "请填写学生姓名，最多 64 字。";
      if (current.members.items.some((item) => item.username?.toLocaleLowerCase() === current.student.username.trim().toLocaleLowerCase())) return "这个登录名已在班级中，请从名单查看状态或重发激活码。";
    } else if (kind === "add") {
      if (!canManageMembers()) return "当前没有班级成员管理权限，请刷新核对。";
      if (!current.member.user_id.trim() || current.member.user_id.trim().length > 100) return "请填写学生账号 ID。";
      if (member(current.member.user_id.trim())) return "这位学生已经在当前班级。";
    }
    return "";
  }

  async function submit(kind, id = "") {
    syncContext();
    if (!active() || locked()) return;
    const problem = validateForm(kind);
    if (problem) { showError(problem); return; }
    if (kind === "application") return mutate({ kind, method: "POST", url: "/course-applications", body: { name: current.application.name.trim(), term: current.application.term.trim() }, before: current.applications.items.map((item) => item.id) });
    if (kind === "class") return mutate({ kind, method: "POST", url: `/courses/${encode(current.courseId)}/classes`, body: { name: current.classroom.name.trim(), owner_teacher_id: isTeacher() ? user().id : current.classroom.owner_teacher_id.trim() }, before: current.classes.items.map((item) => item.id) });
    if (kind === "student") return mutate({ kind, method: "POST", url: "/auth/students", body: { username: current.student.username.trim(), display_name: current.student.display_name.trim(), course_id: current.courseId, class_id: current.classId } });
    if (kind === "add") return mutate({ kind, method: "POST", url: classPath("/members"), userId: current.member.user_id.trim(), body: { user_id: current.member.user_id.trim() } });
    if (kind === "budget") {
      if (!canSetBudget() || !member(id) || member(id).status === "disabled") { showError("当前不能修改这位学生的日额，请核对币种、成员和班级负责人权限。"); return; }
      const raw = current.budgetDrafts.has(id) ? current.budgetDrafts.get(id) : String(findBudget(id)?.limit_units ?? "");
      if (!/^\d+$/.test(raw.trim()) || !Number.isSafeInteger(Number(raw))) { showError("请填写不小于 0 的安全整数额度，不接受小数或留空。"); return; }
      const limit = Number(raw);
      if (findBudget(id)?.limit_units === limit) { current.message = "额度未变化，无需重复保存。"; repaint(); return; }
      return mutate({ kind, method: "PUT", url: "/budgets/limits", userId: id, body: { scope_type: "student_daily", scope_id: id, class_id: current.classId, limit_units: limit, emergency_units: 0, period: null } });
    }
  }

  function openConfirmation(kind, id) {
    if (locked() || !canManageMembers()) return;
    const target = member(id);
    if (!target || (kind === "activation" && target.status !== "pending") || (kind === "reset" && target.status !== "active")) return;
    if (kind === "transfer" && target.status === "disabled") return;
    current.confirmation = { kind, userId: id, label: target.display_name || target.username, target_class_id: "" };
    current.error = ""; repaint();
  }

  async function confirm() {
    if (locked() || !canManageMembers()) return;
    const selected = current.confirmation;
    if (!selected) return;
    const target = member(selected.userId);
    if (!target) { showError("该学生已不在当前名单，请刷新。"); return; }
    if (["activation", "reset"].includes(selected.kind)) {
      if (target.status !== (selected.kind === "activation" ? "pending" : "active")) { showError("学生状态已变化，请刷新后再操作。"); return; }
      return mutate({ kind: selected.kind, method: "POST", userId: selected.userId, label: selected.label,
        url: selected.kind === "activation" ? "/auth/activation-codes" : "/auth/password-reset-codes",
        body: { user_id: selected.userId, course_id: current.courseId, class_id: current.classId } });
    }
    if (selected.kind === "remove") return mutate({ kind: "remove", method: "DELETE", url: classPath(`/members/${encode(selected.userId)}`), userId: selected.userId });
    if (selected.kind === "transfer") {
      const destination = current.allClasses.items.find((item) => item.id === selected.target_class_id && item.id !== current.classId && item.course_id === current.courseId && item.can_manage === true);
      if (!current.allClasses.ready || !destination || target.status === "disabled") { showError("请选择本课程中另一个可管理的班级。"); return; }
      return mutate({ kind: "transfer", method: "POST", url: classPath(`/members/${encode(selected.userId)}/transfer`), userId: selected.userId, body: { target_class_id: destination.id } });
    }
  }

  function unknownResult(error) {
    const status = Number(error?.status);
    return !Number.isFinite(status) || status === 0 || status >= 500 || [408, 425].includes(status);
  }
  function needResponse(condition) {
    if (!condition) throw new Error("服务未返回完整结果，请先刷新查询。");
  }
  function saveCode(data, response, command, kind, id) {
    const value = kind === "activation" ? response?.activation_code : response?.reset_code;
    needResponse(identifier(value) && identifier(id) && Number.isFinite(response?.expires_in_seconds) && response.expires_in_seconds > 0);
    data.code = state.loggingOut || command.discardCode ? null
      : { value, kind, userId: id, label: command.label || command.body.display_name || member(id)?.display_name || id, expiresAt: Date.now() + response.expires_in_seconds * 1000 };
    data.copied = false;
  }
  function applySuccess(data, response, command) {
    const { kind, body, userId } = command;
    if (kind === "application") {
      needResponse(identifier(response?.id) && response.applicant_id === user().id);
      data.applications.items.push(response); data.application = { name: "", term: "" }; return "课程申请已提交，请等待管理员审核。";
    }
    if (kind === "class") {
      needResponse(identifier(response?.id) && response.course_id === data.courseId && response.owner_teacher_id === body.owner_teacher_id);
      data.classroom = { name: "", owner_teacher_id: "" }; data.classes.items.push(response); return "班级已创建。";
    }
    if (kind === "student") {
      saveCode(data, response, command, "activation", response?.student_id);
      data.members.items.push({ user_id: response.student_id, username: body.username, display_name: body.display_name, status: "pending" });
      data.student = { username: "", display_name: "" };
      return data.code ? "学生已创建，请将激活码私下交给本人。" : "学生已创建；退出期间已清除激活码。请刷新核对学生状态，如需激活码，再明确选择重发。";
    }
    if (["activation", "reset"].includes(kind)) {
      needResponse(response?.user_id === userId); saveCode(data, response, command, kind, userId);
      if (!data.code) return "一次性码已签发；退出期间已清除显示。请刷新核对学生状态，如仍需要，再明确确认签发。";
      return kind === "activation" ? "激活码已签发，请私下交给学生本人。" : "口令重设码已签发，请私下交给学生本人。";
    }
    if (kind === "add") { needResponse(response?.user_id === userId && response.class_id === data.classId); data.member = { user_id: "" }; return "学生已加入班级。"; }
    if (kind === "transfer") needResponse(response?.user_id === userId && response.class_id === body.target_class_id);
    if (["remove", "transfer"].includes(kind)) {
      data.members.items = data.members.items.filter((item) => item.user_id !== userId);
      if (data.code?.userId === userId) data.code = null;
      return kind === "remove" ? "学生已移出当前班级。" : "学生已转入所选班级。";
    }
    if (kind === "budget") {
      needResponse(response?.scope_type === "student_daily" && response.scope_id === userId && response.class_id === data.classId && response.period == null && response.limit_units === body.limit_units);
      data.budgets.items = data.budgets.items.filter((item) => !(item.scope_id === userId && item.class_id === data.classId && item.period == null));
      data.budgets.items.push(response); data.budgetDrafts.delete(userId); return "学生日额已保存。";
    }
    return "操作已完成。";
  }

  async function mutate(command) {
    if (locked() || !active()) return;
    const token = epoch, data = current;
    command = { ...command, classId: data.classId, courseId: data.courseId };
    data.pending = command; data.error = ""; data.message = "";
    if (["student", "activation", "reset"].includes(command.kind)) { data.code = null; data.copied = false; }
    repaint();
    let response;
    try {
      response = await api.call(command.method, command.url, command.body);
      if (!valid(token, data) || data.pending !== command) return;
      data.message = applySuccess(data, response, command);
    } catch (error) {
      if (!valid(token, data) || data.pending !== command) return;
      data.pending = null;
      if (unknownResult(error)) {
        data.uncertain = { ...command, checked: false, found: false, result: "尚未完成结果核对，请先刷新查询；输入已保留。" };
        data.confirmation = null;
        data.error = "提交结果暂无法确认。请先刷新核对，不要直接重复提交。";
      } else {
        data.error = error?.message || "操作未完成，输入已保留。";
        if ([401, 403].includes(Number(error?.status))) {
          data.code = null; data.confirmation = null;
          data.classes.ready = false; data.budgetAccess = false;
          if (command.kind === "class") data.courses.ready = false;
        }
      }
      repaint(); return;
    }
    if (!valid(token, data) || data.pending !== command) return;
    data.pending = null; data.confirmation = null; data.error = "";
    const selection = { courseId: command.courseId || null, classId: command.kind === "class" ? response.id : command.classId || null };
    if (state.loggingOut) {
      data.message += " 退出期间未刷新列表；若退出失败，请点击刷新核对。";
      repaint(); return;
    }
    repaint();
    // Refresh failures do not turn a confirmed write into a retryable write. load() never calls onChanged.
    try {
      if (typeof onChanged === "function") await onChanged(selection);
      else await load();
    } catch {
      if (valid(token, data)) { data.message += " 列表刷新未完成，请点击刷新核对。"; repaint(); }
    }
  }

  async function choose(scope, id) {
    if (editingLocked() || current.uncertain) return;
    const token = epoch, data = current;
    let selection;
    if (scope === "course") {
      if (!data.courses.items.some((item) => item.id === id) || id === data.courseId) return;
      selection = { courseId: id, classId: null };
    } else {
      if (!data.classes.items.some((item) => item.id === id) || id === data.classId) return;
      selection = { courseId: data.courseId, classId: id };
    }
    data.code = null; data.confirmation = null; data.switching = true; repaint();
    try {
      if (typeof onChanged === "function") await onChanged(selection);
      else { state.courseId = selection.courseId; state.classId = selection.classId; await load(); }
    } catch {
      if (valid(token, data)) { data.error = "切换未完成，请刷新后重新选择。"; data.switching = false; repaint(); }
    } finally {
      if (valid(token, data)) { data.switching = false; repaint(); }
    }
  }

  function tab(name) {
    if (!["courses", "members", "budgets"].includes(name) || name === current.tab || current.pending || current.switching) return;
    const wasLoading = current.loading;
    epoch += 1; readSequence += 1; current.loading = false;
    current.tab = name; current.code = null; current.copied = false; current.confirmation = null; current.error = ""; current.message = "";
    repaint();
    if (wasLoading) return load();
  }

  async function copyCode() {
    const token = epoch, data = current, saved = data.code;
    if (!active() || !saved || saved.expiresAt <= Date.now() || !valid(token, data)) return;
    if (!globalThis.navigator?.clipboard?.writeText) { showError("浏览器未允许复制，请选中一次性码手动复制。"); return; }
    try {
      await globalThis.navigator.clipboard.writeText(saved.value);
      observeLogout();
      if (active() && valid(token, data) && data.code === saved) { data.copied = true; repaint(); toast("一次性码已复制，请仅交给学生本人。"); }
    } catch {
      observeLogout();
      if (active() && valid(token, data) && data.code === saved) showError("复制未完成，请选中一次性码手动复制。");
    }
  }

  function acknowledge() {
    const pending = current.uncertain;
    if (!pending?.checked || current.loading) return;
    if (pending.found) {
      if (pending.kind === "application") current.application = { name: "", term: "" };
      if (pending.kind === "class") current.classroom = { name: "", owner_teacher_id: "" };
      if (pending.kind === "student") current.student = { username: "", display_name: "" };
      if (pending.kind === "add") current.member = { user_id: "" };
      if (pending.kind === "budget") current.budgetDrafts.delete(pending.userId);
    }
    current.uncertain = null; current.error = ""; current.message = "已完成结果核对，请根据最新列表继续操作。"; repaint();
  }

  function bind(container) {
    const surface = container?.matches?.("[data-teacher-management]") ? container : container?.querySelector?.("[data-teacher-management]");
    if (!surface) return;
    root = surface; bindings.set(surface, { context, epoch });
    if (listeners.has(surface)) return;
    listeners.add(surface);
    const allowed = (event) => {
      if (state.loggingOut) { observeLogout(); repaint(); return false; }
      return root === surface && surface.isConnected && surface.contains?.(event.target)
        && bindings.get(surface)?.context === key() && bindings.get(surface)?.epoch === epoch && active();
    };
    surface.addEventListener("click", (event) => {
      if (!allowed(event)) return;
      const target = event.target.closest?.("[data-mgmt-action]");
      if (!target || !surface.contains(target) || target.disabled || target.matches?.(":disabled")) return;
      const action = target.dataset.mgmtAction, id = target.dataset.mgmtId;
      if (action === "tab") return tab(id);
      if (action === "refresh" || action === "reconcile") { if (!current.pending && !current.loading && !current.switching) return load(); return; }
      if (action === "acknowledge") return acknowledge();
      if (action === "clear-code") { current.code = null; current.copied = false; repaint(); return; }
      if (action === "copy-code") return copyCode();
      if (action === "cancel") { if (!current.pending) { current.confirmation = null; repaint(); } return; }
      if (action === "confirm") return confirm();
      if (["activation", "reset", "remove", "transfer"].includes(action)) return openConfirmation(action, id);
    });
    const onField = (event) => {
      if (!allowed(event)) return;
      const target = event.target.closest?.("[data-mgmt-field]");
      if (!target || !surface.contains(target) || target.disabled || target.matches?.(":disabled") || draftLocked()) return;
      const scope = target.dataset.mgmtScope, name = target.dataset.mgmtField;
      if (scope === "selection") { if (event.type === "change") return choose(name, target.value); return; }
      if (scope === "budget" && name === "limit_units" && identifier(target.dataset.mgmtId)) { current.budgetDrafts.set(target.dataset.mgmtId, target.value); return; }
      if (scope === "confirmation" && name === "target_class_id" && current.confirmation) { current.confirmation.target_class_id = target.value; return; }
      const allowedFields = { application: ["name", "term"], classroom: ["name", "owner_teacher_id"], student: ["username", "display_name"], member: ["user_id"] };
      if (allowedFields[scope]?.includes(name)) current[scope][name] = target.value;
    };
    surface.addEventListener("input", onField);
    surface.addEventListener("change", onField);
    surface.addEventListener("submit", (event) => {
      const form = event.target.closest?.("[data-mgmt-form]");
      if (!form || !surface.contains(form)) return;
      event.preventDefault();
      if (allowed(event)) return submit(form.dataset.mgmtForm, form.dataset.mgmtId);
    });
  }

  function selectorHtml() {
    return `<div class="mgmt-row mgmt-context">
      <label class="mgmt-field">课程<select data-mgmt-scope="selection" data-mgmt-field="course" aria-label="管理课程"${locked() || !current.courses.items.length ? " disabled" : ""}>${current.courses.items.length ? current.courses.items.map((item) => `<option value="${esc(item.id)}"${item.id === current.courseId ? " selected" : ""}>${esc(item.name)}${item.term ? `（${esc(item.term)}）` : ""}</option>`).join("") : '<option value="">暂无可访问课程</option>'}</select></label>
      <label class="mgmt-field">班级<select data-mgmt-scope="selection" data-mgmt-field="class" aria-label="管理班级"${locked() || !current.classes.items.length ? " disabled" : ""}>${current.classes.items.length ? current.classes.items.map((item) => `<option value="${esc(item.id)}"${item.id === current.classId ? " selected" : ""}>${esc(item.name)}${item.can_manage === true ? "" : "（只读）"}</option>`).join("") : '<option value="">暂无可访问班级</option>'}</select></label>
      ${button("refresh", current.loading ? "刷新中…" : "刷新", { disabled: editingLocked() })}</div>`;
  }
  function applicationsHtml() {
    const records = [...current.applications.items].reverse();
    return `<section class="mgmt-panel"><h3>本人课程申请</h3>${notice(current.applications.error, "error")}
      ${isTeacher() ? `<form class="mgmt-form" data-mgmt-form="application"><fieldset${draftLocked() ? " disabled" : ""}>
        ${field("application", "name", "课程名称", current.application.name, 'maxlength="200" required autocomplete="off"')}
        ${field("application", "term", "学期", current.application.term, 'maxlength="64" required placeholder="例如：2026 年秋季" autocomplete="off"')}
        <p class="mgmt-muted">管理员审核通过后，课程会出现在上方列表中。</p>
        <button class="mgmt-button mgmt-button-primary" type="submit"${locked() || !current.applications.ready ? " disabled" : ""}>${current.pending?.kind === "application" ? "提交中…" : "申请课程"}</button>
      </fieldset></form>` : '<p class="mgmt-muted">课程申请由有效教师账号提交。</p>'}
      ${records.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>本人已提交的申请</caption><thead><tr><th scope="col">课程 / 学期</th><th scope="col">状态</th><th scope="col">审核说明</th></tr></thead><tbody>${records.map((item) => `<tr><td><strong>${esc(item.name)}</strong><small>${esc(item.term)} · ${esc(stamp(item.created_at))}</small></td><td><span class="mgmt-badge">${esc(applicationLabel[item.status] || "状态未提供")}</span></td><td>${esc(item.review_note || "暂无说明")}</td></tr>`).join("")}</tbody></table></div>` : current.applications.ready ? '<p class="mgmt-empty">还没有提交过课程申请。</p>' : ""}</section>`;
  }
  function classesHtml() {
    if (!current.courseId) return `<section class="mgmt-panel"><h3>创建班级</h3><p class="mgmt-empty">还没有可访问的课程。先提交课程申请，通过后即可建班。</p></section>`;
    return `<section class="mgmt-panel"><h3>${esc(course()?.name || "当前课程")}的班级</h3>${notice(current.classes.error, "error")}
      ${current.classes.ready && !current.classes.items.length ? '<p class="mgmt-empty">当前课程还没有你可访问的班级。有建班权限时，可在下方创建。</p>' : ""}
      ${current.classes.items.length ? `<ul class="mgmt-list">${current.classes.items.map((item) => `<li><strong>${esc(item.name)}</strong><span class="mgmt-badge">${item.can_manage === true ? "可管理成员" : "只读名单"}</span></li>`).join("")}</ul>` : ""}
      ${canCreateClass() ? `<form class="mgmt-form" data-mgmt-form="class"><fieldset${draftLocked() ? " disabled" : ""}>
        ${field("classroom", "name", "新班级名称", current.classroom.name, 'maxlength="100" required autocomplete="off"')}
        ${isTeacher() ? `<p class="mgmt-muted">负责人：${esc(user().display_name || user().username)}（本人）。</p>` : field("classroom", "owner_teacher_id", "负责人教师账号 ID", current.classroom.owner_teacher_id, 'maxlength="100" required autocomplete="off"')}
        <button class="mgmt-button mgmt-button-primary" type="submit"${locked() || !current.classes.ready ? " disabled" : ""}>${current.pending?.kind === "class" ? "创建中…" : "创建班级"}</button>
      </fieldset></form>` : '<p class="mgmt-muted">当前没有课程建班权限。课程材料管理权与班级成员管理权分别授权。</p>'}</section>`;
  }
  function noClassHtml() {
    return `<section class="mgmt-panel"><h3>先选择班级</h3><p class="mgmt-empty">${current.courseId ? "这门课还没有可访问班级。可在“课程与申请”中建班，或请班级负责人授予访问权限。" : "还没有可访问课程。可先在“课程与申请”中提交申请。"}</p>${button("tab", "课程与申请", { id: "courses" })}</section>`;
  }
  function membersHtml() {
    if (!current.classId) return noClassHtml();
    const writable = canManageMembers();
    const targets = current.allClasses.ready && current.allClasses.items.some((item) => item.id !== current.classId && item.course_id === current.courseId && item.can_manage === true);
    return `<section class="mgmt-panel"><h3>${esc(classroom()?.name || "当前班级")}学生名单</h3>${notice(current.members.error, "error")}
      ${!canManageClass() ? notice("当前为只读协作，可查看名单；成员、一次性码和日额的写入入口已关闭。") : ""}
      ${current.members.items.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>本班学生与账号状态</caption><thead><tr><th scope="col">学生</th><th scope="col">登录名 / 账号 ID</th><th scope="col">状态</th>${writable ? '<th scope="col">操作</th>' : ""}</tr></thead><tbody>${current.members.items.map((item) => `<tr><td>${esc(item.display_name)}</td><td>${esc(item.username)}<small>${esc(item.user_id)}</small></td><td><span class="mgmt-badge">${esc(statusLabel[item.status] || "状态未提供")}</span></td>${writable ? `<td><div class="mgmt-actions">${item.status === "pending" ? button("activation", "重发激活码", { id: item.user_id, disabled: locked() }) : item.status === "active" ? button("reset", "签发口令重设码", { id: item.user_id, disabled: locked() }) : ""}${targets && item.status !== "disabled" ? button("transfer", "转班", { id: item.user_id, disabled: locked() }) : ""}${button("remove", "移出班级", { id: item.user_id, danger: true, disabled: locked() })}</div></td>` : ""}</tr>`).join("")}</tbody></table></div>` : current.members.ready ? '<p class="mgmt-empty">班级暂无学生。有管理权限时，可创建学生或加入已有学生。</p>' : ""}</section>
      ${writable ? `<div class="mgmt-grid"><section class="mgmt-panel"><h3>创建学生</h3><form class="mgmt-form" data-mgmt-form="student"><fieldset${draftLocked() ? " disabled" : ""}>
        ${field("student", "display_name", "学生姓名", current.student.display_name, 'maxlength="64" required autocomplete="off"')}
        ${field("student", "username", "登录名", current.student.username, 'minlength="3" maxlength="32" pattern="[A-Za-z0-9_.\\-]+" required autocomplete="off" autocapitalize="none" spellcheck="false"')}
        <p class="mgmt-muted">登录名使用 3–32 位字母、数字、下划线、点或连字符。创建后由学生用一次性激活码自行设置口令。</p>
        <button type="submit" class="mgmt-button mgmt-button-primary"${locked() ? " disabled" : ""}>${current.pending?.kind === "student" ? "创建中…" : "创建并生成激活码"}</button>
      </fieldset></form></section><section class="mgmt-panel"><h3>加入已有学生</h3><form class="mgmt-form" data-mgmt-form="add"><fieldset${draftLocked() ? " disabled" : ""}>
        ${field("member", "user_id", "学生账号 ID", current.member.user_id, 'maxlength="100" required autocomplete="off"')}
        <p class="mgmt-muted">教师只能加入已在本人其他可管理班级的学生。新账号请使用“创建学生”。</p>
        <button type="submit" class="mgmt-button"${locked() ? " disabled" : ""}>加入当前班级</button>
      </fieldset></form></section></div>` : ""}`;
  }
  function budgetsHtml() {
    if (!current.classId) return noClassHtml();
    if (!current.budgetAccess) return `<section class="mgmt-panel"><h3>班级学生日额</h3>${notice("学生日额由班级负责人或管理员查看和设置；班级协作管理权限不包含日额权限。")}</section>`;
    const writable = canSetBudget();
    return `<section class="mgmt-panel"><h3>班级学生日额</h3>${notice(current.budgets.error || current.members.error, "error")}
      ${!current.currency ? notice("额度币种或单位尚未确认，暂不能填写或保存额度。请刷新或联系管理员确认配置。", "warn") : `<p class="mgmt-muted">币种：${esc(current.currency)}。每日日额使用整数计费单位，1 单位按服务原值提交，不换算金额。${esc(current.unitDescription)} 每日日额持续生效；0 表示不提供该项可用额度，平台或课程的更低上限仍同时生效。</p>`}
      ${current.members.items.length ? `<div class="mgmt-table-wrap"><table class="mgmt-table"><caption>学生的本班每日日额</caption><thead><tr><th scope="col">学生</th><th scope="col">本班每日上限</th>${writable ? '<th scope="col">设置整数额度</th>' : ""}</tr></thead><tbody>${current.members.items.map((item) => { const limit = findBudget(item.user_id); const value = current.budgetDrafts.has(item.user_id) ? current.budgetDrafts.get(item.user_id) : limit ? String(limit.limit_units) : ""; return `<tr><td>${esc(item.display_name)}<small>${esc(statusLabel[item.status] || "状态未提供")}</small></td><td>${!current.budgets.ready ? "额度尚未读取" : limit ? `${esc(amountText(limit.limit_units))} ${esc(current.currency)}` : "尚未配置本班日额"}</td>${writable ? `<td>${item.status === "disabled" ? "已停用，不可设置" : `<form class="mgmt-row mgmt-budget-form" data-mgmt-form="budget" data-mgmt-id="${esc(item.user_id)}"><label class="mgmt-field"><span class="mgmt-muted">${esc(item.display_name)}的日额</span><input type="text" inputmode="numeric" pattern="[0-9]+" data-mgmt-scope="budget" data-mgmt-field="limit_units" data-mgmt-id="${esc(item.user_id)}" value="${esc(value)}" required autocomplete="off"${draftLocked() ? " disabled" : ""}></label><button type="submit" class="mgmt-button"${locked() ? " disabled" : ""}>保存日额</button></form>`}</td>` : ""}</tr>`; }).join("")}</tbody></table></div>` : current.members.ready ? '<p class="mgmt-empty">班级暂无学生，加入学生后再设置日额。</p>' : ""}
      ${current.budgets.items.some((item) => item.class_id !== current.classId || item.period != null) ? '<p class="mgmt-muted">列表仅编辑本班持续生效的每日上限。平台、课程或指定日期的限制不会被当作本班默认值。</p>' : ""}</section>`;
  }
  function confirmationHtml() {
    const value = current.confirmation;
    if (!value) return "";
    const descriptions = {
      remove: "移出后，该学生将失去当前班级的访问权限。学生账号不会被删除。",
      transfer: "转班会移出当前班级，并加入所选班级。只能转至同课程内你可管理的班级。",
      activation: "为待激活学生签发新的激活码。请先确认本人需要，再私下交付一次性码。",
      reset: "为有效学生签发口令重设码。请先核实学生身份，再私下交付一次性码。",
    };
    const targets = current.allClasses.items.filter((item) => item.id !== current.classId && item.course_id === current.courseId && item.can_manage === true);
    return `<section class="mgmt-panel mgmt-confirm" aria-labelledby="mgmt-confirm-title"><h3 id="mgmt-confirm-title">确认操作：${esc(value.label)}</h3><p>${esc(descriptions[value.kind])}</p>
      ${value.kind === "transfer" ? `<label class="mgmt-field">目标班级<select data-mgmt-scope="confirmation" data-mgmt-field="target_class_id"${editingLocked() ? " disabled" : ""}><option value="">请选择班级</option>${targets.map((item) => `<option value="${esc(item.id)}"${item.id === value.target_class_id ? " selected" : ""}>${esc(item.name)}</option>`).join("")}</select></label>` : ""}
      <div class="mgmt-actions">${button("confirm", current.pending ? "处理中…" : "确认操作", { primary: true, disabled: locked() })}${button("cancel", "取消", { disabled: Boolean(current.pending) })}</div></section>`;
  }
  function codeHtml() {
    const saved = current.code;
    if (!saved) return "";
    if (saved.expiresAt <= Date.now()) { current.code = null; return notice("刚才显示的一次性码已过期，请重新核对学生状态后签发。", "warn"); }
    return `<section class="mgmt-panel mgmt-code" aria-labelledby="mgmt-code-title"><h3 id="mgmt-code-title">${esc(saved.label)}的${saved.kind === "activation" ? "激活码" : "口令重设码"}</h3>
      <p class="mgmt-muted">仅在当前页面显示，刷新浏览器或离开本页后无法找回。请私下交给学生本人；清除显示不会撤销已经签发的码。</p>
      <label class="mgmt-field">一次性码<input type="text" data-mgmt-code value="${esc(saved.value)}" readonly autocomplete="off" spellcheck="false" aria-label="一次性码"></label>
      <p class="mgmt-muted">有效至 ${esc(new Date(saved.expiresAt).toLocaleString("zh-CN"))}</p><div class="mgmt-actions">${button("copy-code", current.copied ? "已复制，可再次复制" : "复制一次性码")}${button("clear-code", "清除显示")}</div></section>`;
  }
  function uncertainHtml() {
    const value = current.uncertain;
    if (!value) return "";
    return `<section class="mgmt-panel mgmt-reconcile"><h3>先核对上一次操作</h3><p>${esc(value.result)}</p>${value.kind === "student" && value.found ? '<p class="mgmt-muted">若学生已创建但激活码未收到，请在名单中明确选择“重发激活码”，不要再次创建账号。</p>' : ""}<div class="mgmt-actions">${button("reconcile", "刷新查询结果", { disabled: editingLocked() })}${button("acknowledge", "我已核对，返回操作", { disabled: !value.checked || editingLocked() })}</div></section>`;
  }
  function renderInner() {
    observeLogout();
    if (state.loggingOut) return notice("正在退出，教学管理操作已暂停，一次性码已清除。若退出失败，可返回此页核对操作结果。");
    if (!active()) return notice(state.live ? "请使用有效教师账号登录后管理课程和班级。" : "正式服务未连接，暂不能管理课程和班级。", "warn");
    return `<header class="mgmt-header"><div><h2>教学管理</h2><p class="mgmt-muted">申请课程、管理班级与学生，设置每日日额。</p></div></header>
      <nav class="mgmt-tabs" aria-label="教师管理栏目">${[["courses", "课程与申请"], ["members", "班级与学生"], ["budgets", "学生日额"]].map(([id, label]) => `<button type="button" class="mgmt-button${current.tab === id ? " mgmt-button-primary" : ""}" data-mgmt-action="tab" data-mgmt-id="${id}" aria-current="${current.tab === id ? "page" : "false"}"${current.pending || current.switching ? " disabled" : ""}>${label}</button>`).join("")}</nav>
      ${selectorHtml()}${current.loading ? notice("正在读取课程、班级与学生资料…") : ""}${notice(current.courses.error, "error")}${notice(current.allClasses.error, "warn")}${notice(current.error, "error")}${notice(current.message, "ok")}${uncertainHtml()}${codeHtml()}${confirmationHtml()}
      ${current.tab === "courses" ? `<div class="mgmt-grid">${applicationsHtml()}${classesHtml()}</div>` : current.tab === "members" ? membersHtml() : budgetsHtml()}`;
  }
  function renderManagement() {
    syncContext();
    return `<section class="teacher-management" data-teacher-management>${renderInner()}</section>`;
  }
  return { render: renderManagement, bind, load, leave, reset };
}
