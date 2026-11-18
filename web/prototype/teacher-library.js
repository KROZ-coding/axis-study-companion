// Teacher-only library. All records come from the authenticated API; drafts live in this instance.
const TYPES = { single_choice: "单选题", judge: "判断题", fill: "填空题", text: "作答题", multi_choice: "多选题" };
const STATUSES = { approved: "可用", candidate: "候选", reviewing: "审查中", quarantined: "暂停使用", retired: "已退役" };
const DIFFICULTIES = { easy: "基础", medium: "适中", hard: "进阶" };
const JOB_STATUS = { pending: "等待处理", queued: "排队中", leased: "已接单", running: "处理中", retry_wait: "等待重试", approved: "审查通过，已入库", evaluation_complete: "评测审查完成，未入正式题库", review_pending: "处理结束，尚无通过记录", succeeded: "已完成", completed: "已完成", failed: "失败", cancelled: "已取消", quarantined: "已隔离", awaiting_confirmation: "等待确认", reconciling: "等待核对", budget_blocked: "额度不足", not_dispatched: "等待调度" };
const STAGES = { generate: "生成候选题", review: "独立求解审查", compare: "答案比对", recheck: "独立复核", compare_recheck: "复核答案比对", done: "已完成", quarantined: "隔离待处理", parse: "解析材料" };
const MATERIAL_STATUS = { parsed: "待确认", draft: "待确认", manual_required: "待人工核对", partial: "部分已确认", confirmed: "已确认", blocked: "有待解决疑点" };
const MATERIAL_LIFECYCLE = { active: "启用中", inactive: "已停用", superseded: "已被替换" };
const USAGE_SCOPES = { production: "正式出题", evaluation: "评测验证" };
const lifecycle = (material) => material?.lifecycle_status || "active";
const usageScope = (material) => material?.usage_scope || "production";
const materialName = (material) => material?.title || material?.source_filename || material?.filename || material?.id || "材料";
const lifecycleBadge = (material) => badge(MATERIAL_LIFECYCLE[lifecycle(material)] || "状态未提供", lifecycle(material) === "active" ? "ok" : "warn");
function provenance(material = {}) {
  const records = [["原始文件名（手工标注）", material.source_filename], ["原文位置（手工标注）", material.source_locator], ["来源说明（手工标注）", material.source_note], ["上传文件", material.filename]];
  return `<dl class="library-definition library-provenance">${records.filter(([, value]) => value).map(([label, value]) => `<dt>${label}</dt><dd>${e(value)}</dd>`).join("") || '<dt>来源说明</dt><dd>尚未标注，请对照原始材料核实。</dd>'}</dl>`;
}
const LICENSES = { unknown: "尚未确认", own: "本人原创", permission: "已获授权", cc0: "CC0", cc_by: "CC BY", cc_by_sa: "CC BY-SA", restricted: "限制使用" };
const PERMISSIONS = { rights_confirmed: "我已核实材料的使用权与署名要求", allow_generation: "允许材料用于出题与题目审查", allow_retrieval: "允许材料用于课程检索与讲解", allow_student_view: "允许经单独核对的正文向学生公开", external_processing_allowed: "允许所选内容发送至外部模型服务处理" };
const e = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const list = (value) => Array.isArray(value) ? value : [];
const text = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value);
const asQuestion = (row) => row?.content || row?.question?.content || row?.question || row || {};
const questionId = (row) => row?.question_id || row?.id || "";
const questionKey = (row) => `${questionId(row)}@${row?.version || 1}`;
const jobId = (job) => job?.id || job?.content_job_id || job?.job_id || "";
const stamp = (value) => {
  if (value == null || value === "") return "时间未提供";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? "时间未提供" : date.toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
};
const badge = (label, tone = "") => `<span class="library-badge${tone ? ` library-badge-${tone}` : ""}">${e(label)}</span>`;
const statusBadge = (status) => badge(STATUSES[status] || status || "状态未提供", status === "approved" ? "ok" : ["quarantined", "retired"].includes(status) ? "warn" : "");
const button = (action, label, { primary = false, danger = false, disabled = false, attrs = "" } = {}) => `<button type="button" class="library-button${primary ? " library-button-primary" : ""}${danger ? " library-button-danger" : ""}" data-library-action="${e(action)}" ${disabled ? "disabled" : ""} ${attrs}>${e(label)}</button>`;
const notice = (message, kind = "info") => `<div class="library-notice library-notice-${kind}" role="${kind === "error" ? "alert" : "status"}">${e(message)}</div>`;
const optionList = (values, selected, emptyLabel) => `${emptyLabel !== undefined ? `<option value="">${e(emptyLabel)}</option>` : ""}${Object.entries(values).map(([value, label]) => `<option value="${e(value)}" ${String(selected) === value ? "selected" : ""}>${e(label)}</option>`).join("")}`;

export function createTeacherLibrary({ api, state, render: renderApp, toast = () => {}, onPrepareGoal = () => {} }) {
  let serial = 0;
  let epoch = 0;
  let context = "";
  let root = null;
  let pollTimer = null;
  let current = fresh();
  const listeners = new WeakSet();
  const inertBackground = new Map();
  let scrollLocks = [];
  let openingFocus = null;
  let focusAfterClose = null;
  const focusSelector = "[data-library-field], [data-library-action], [data-library-form] button[type=submit]";

  function focusIdentity(node) {
    if (!node?.getAttribute) return null;
    const field = node.getAttribute("data-library-field"), action = node.getAttribute("data-library-action");
    const form = !field && !action ? node.closest?.("[data-library-form]") : null;
    if (!field && !action && !form) return null;
    return { field, action, scope: node.getAttribute("data-library-scope"), value: node.getAttribute("data-library-value"), form: form?.getAttribute("data-library-form") || null, formValue: form?.getAttribute("data-library-value") || null, tag: node.tagName, className: node.className };
  }

  function sameFocus(node, identity) {
    const other = focusIdentity(node);
    return other && ["field", "action", "scope", "value", "form", "formValue", "tag", "className"].every((key) => other[key] === identity[key]);
  }

  function focusSnapshot(node) {
    if (!node) return null;
    const identity = focusIdentity(node);
    const peers = identity && root ? Array.from(root.querySelectorAll(focusSelector)).filter((item) => sameFocus(item, identity)) : [];
    return { node, identity, index: Math.max(0, peers.indexOf(node)), start: typeof node.selectionStart === "number" ? node.selectionStart : null, end: typeof node.selectionEnd === "number" ? node.selectionEnd : null };
  }

  function restoreFocus(snapshot, within = root) {
    if (!snapshot || !within) return false;
    let target = snapshot.node?.isConnected && within.contains?.(snapshot.node) ? snapshot.node : null;
    if (!target && snapshot.identity) {
      const peers = Array.from(within.querySelectorAll(focusSelector)).filter((node) => sameFocus(node, snapshot.identity));
      target = peers[snapshot.index] || peers[0];
    }
    if (!target || target.disabled || target.matches?.(":disabled") || target.closest?.("[inert]")) return false;
    target.focus?.({ preventScroll: true });
    if (snapshot.start != null && target.setSelectionRange && (target.tagName === "TEXTAREA" || ["text", "search", "url", "tel", "password"].includes(target.type))) {
      try { target.setSelectionRange(snapshot.start, snapshot.end); } catch { /* Some input types cannot restore a text selection. */ }
    }
    return true;
  }

  // Only siblings outside the modal's ancestor path become inert; never disable its containing app.
  function syncModalEffects() {
    const overlay = active() && current.modal && root?.isConnected ? root.querySelector(".library-overlay") : null;
    const host = root?.closest?.(".teacher-app") || root;
    if (!overlay || root?.closest?.("[inert]")) { releaseModalEffects(); return; }
    const desired = new Set();
    for (let branch = overlay; branch && branch !== host; branch = branch.parentElement) {
      const parent = branch.parentElement;
      if (!parent) break;
      for (const sibling of parent.children) if (sibling !== branch) desired.add(sibling);
    }
    for (const [node, previous] of inertBackground) {
      if (!desired.has(node)) { node.inert = previous; inertBackground.delete(node); }
    }
    for (const node of desired) {
      if (!inertBackground.has(node)) { inertBackground.set(node, Boolean(node.inert)); node.inert = true; }
    }
    if (!scrollLocks.length) {
      const doc = root.ownerDocument;
      const targets = [...new Set([doc?.documentElement, doc?.body].filter((node) => node?.style))];
      scrollLocks = targets.map((node) => {
        const previous = node.style.getPropertyValue("overflow"), priority = node.style.getPropertyPriority("overflow");
        node.style.setProperty("overflow", "hidden");
        return { node, previous, priority };
      });
    }
  }

  function releaseModalEffects() {
    for (const [node, previous] of inertBackground) node.inert = previous;
    inertBackground.clear();
    for (const { node, previous, priority } of scrollLocks) {
      if (node.style.getPropertyValue("overflow") === "hidden") {
        if (previous) node.style.setProperty("overflow", previous, priority);
        else node.style.removeProperty("overflow");
      }
    }
    scrollLocks = [];
  }

  function focusModal() {
    const dialog = root?.querySelector(".library-drawer");
    if (!dialog || dialog.closest?.("[inert]")) return;
    const focused = root.ownerDocument?.activeElement;
    if (!dialog.contains(focused)) {
      if (!restoreFocus(current.modal?.lastFocus, dialog)) dialog.focus?.({ preventScroll: true });
    }
  }

  function dismissModal() {
    focusAfterClose = current.restoreFocus;
    current.restoreFocus = null;
    current.modal = null;
    releaseModalEffects();
  }

  function fresh() {
    return {
      tab: "questions", filters: { q: "", question_type: "", status: "", knowledge_point: "", difficulty: "" },
      applied: { q: "", question_type: "", status: "", knowledge_point: "", difficulty: "" },
      questions: { items: [], total: 0, limit: 12, offset: 0, facets: {}, loaded: false, loading: false, error: "", seq: 0 },
      materials: { items: [], offset: 0, limit: 100, hasMore: false, loaded: false, loading: false, error: "", seq: 0, lifecycle: "", usage_scope: "" },
      replacements: new Map(),
      jobs: { items: [], total: null, offset: 0, limit: 20, loaded: false, loading: false, error: "", seq: 0 },
      knowledge: { items: [], loaded: false, loading: false, error: "", seq: 0 },
      selected: new Map(), sourceDocs: new Map(), modal: null, restoreFocus: null, pageError: "",
    };
  }

  function contextKey() {
    return JSON.stringify([Boolean(state.live), state.role, state.courseId, state.classId, state.transitionId, state.teacherContextVersion,
      api?.state?.user?.id || state.user?.id || state.who || ""]);
  }

  function syncContext() {
    const next = contextKey();
    if (next !== context) {
      context = next;
      epoch += 1;
      stopPoll();
      releaseModalEffects(); openingFocus = null; focusAfterClose = null;
      current = fresh();
    }
    return epoch;
  }

  function valid(token) { return token === epoch && context === contextKey() && active(); }
  function active() { return state.live === true && ["teacher", "admin"].includes(state.role) && Boolean(state.courseId); }
  function path(part) { return `/courses/${encodeURIComponent(state.courseId)}/${part}`; }
  function classQuery(params) { if (state.classId) params.set("class_id", state.classId); return params; }
  function classBody() { return state.classId ? { class_id: state.classId } : {}; }
  function errorText(error) { return error?.message || "请求未完成，请稍后重试。"; }
  function requestId() { return globalThis.crypto?.randomUUID?.() || `library-${Date.now()}-${++serial}`; }
  function retryPayload(modal, url, payload) {
    const fingerprint = JSON.stringify([url, payload]);
    modal.commands ||= new Map();
    if (!modal.commands.has(fingerprint)) modal.commands.set(fingerprint, requestId());
    return { ...payload, request_id: modal.commands.get(fingerprint) };
  }
  function stopPoll() { if (pollTimer != null) clearTimeout(pollTimer); pollTimer = null; }
  function reset() { stopPoll(); releaseModalEffects(); epoch += 1; context = ""; current = fresh(); root = null; openingFocus = null; focusAfterClose = null; }

  // Refresh only this surface. In particular, never rebuild the app's learning-goal drawer during polling.
  function repaint() {
    if (!root?.isConnected || !active() || context !== contextKey()) return;
    const focused = root.ownerDocument?.activeElement;
    const snapshot = focused && root.contains(focused) ? focusSnapshot(focused) : null;
    const dialog = root.querySelector(".library-drawer");
    if (current.modal && dialog?.contains(focused)) current.modal.lastFocus = snapshot;
    const scroll = root.querySelector(".library-drawer-body")?.scrollTop;
    root.innerHTML = renderInner();
    bind(root);
    if (current.modal) {
      restoreFocus(snapshot, root.querySelector(".library-drawer"));
      focusModal();
    } else if (focusAfterClose) {
      if (!restoreFocus(focusAfterClose)) root.querySelector(`[data-library-action="${current.tab === "questions" ? "manual" : current.tab === "materials" ? "upload-material" : "generate"}"]`)?.focus?.({ preventScroll: true });
      focusAfterClose = null;
    } else restoreFocus(snapshot);
    if (scroll != null) { const body = root.querySelector(".library-drawer-body"); if (body) body.scrollTop = scroll; }
  }

  async function load({ force = false, config, deficits } = {}) {
    const token = syncContext();
    if (!active()) return;
    if (config || deficits) openGeneration(config || {}, deficits || []);
    const work = [loadQuestions(force, token), loadMaterials(force, token), loadJobs(force, token), loadKnowledge(force, token)];
    await Promise.all(work);
    if (valid(token)) schedulePoll();
  }

  function openGeneration(config = {}, deficits = []) {
    const types = ["single_choice", "judge", "fill", "text"];
    const counts = Object.fromEntries(types.map((type) => [type, 0]));
    const missing = list(deficits);
    if (missing.length) {
      for (const item of missing) {
        if (types.includes(item.question_type)) counts[item.question_type] += Math.max(0, Number(item.missing ?? item.deficit ?? (Number(item.required || 0) - Number(item.available || 0))) || 0);
      }
    } else {
      for (const type of types) counts[type] = Math.max(0, Math.floor(Number(config.question_types?.[type]) || 0));
    }
    if (!Object.values(counts).some(Boolean)) counts.single_choice = 1;
    const requested = Object.values(counts).reduce((sum, count) => sum + count, 0);
    let remaining = 10;
    for (const type of types) { counts[type] = Math.min(remaining, counts[type]); remaining -= counts[type]; }
    const note = config.title || missing.length ? `已带入${config.title ? `“${config.title}”的` : "学习目标的"}备题要求${requested > 10 ? `。共需补充 ${requested} 题，已预填本批次 10 题，其余请分批创建` : ""}。请核对题型数量和来源，再明确同意外发处理。` : "";
    newModal("generate", { ...Object.fromEntries(types.map((type) => [`count_${type}`, counts[type]])), difficulty: DIFFICULTIES[config.difficulty] ? config.difficulty : "medium", knowledge_points: [...list(config.knowledge_points)], instruction: config.title ? `围绕学习目标：${config.title}` : "", source_block_ids: [], source_material_id: "", source_search: "", intent: "production", external_processing_allowed: false }, { prefillNote: note });
  }

  async function loadQuestions(force = false, token = syncContext()) {
    const section = current.questions;
    if (!active() || (section.loaded && !force) || (section.loading && !force)) return;
    const seq = ++section.seq;
    section.loading = true; section.error = "";
    repaint();
    try {
      const params = classQuery(new URLSearchParams({ limit: String(section.limit), offset: String(section.offset) }));
      for (const [key, value] of Object.entries(current.applied)) if (value) params.set(key === "question_type" ? "type" : key, value);
      const data = await api.call("GET", `${path("question-library")}?${params}`);
      if (!valid(token) || section.seq !== seq) return;
      section.items = list(data?.items); section.total = Number(data?.total ?? section.items.length);
      section.facets = data?.facets || {}; section.loaded = true;
      // Selections bind exact versions and survive pagination, filtering, and newer versions.
      for (const row of section.items) if (current.selected.has(questionKey(row))) current.selected.set(questionKey(row), row);
    } catch (error) { if (valid(token) && section.seq === seq) section.error = errorText(error); }
    finally { if (valid(token) && section.seq === seq) { section.loading = false; repaint(); } }
  }

  async function loadMaterials(force = false, token = syncContext(), append = false) {
    const section = current.materials;
    if (!active() || (section.loaded && !force) || (section.loading && !force)) return;
    const seq = ++section.seq;
    section.loading = true; section.error = "";
    const offset = append ? section.offset + section.limit : 0;
    repaint();
    try {
      const data = await api.call("GET", `${path("materials")}?limit=${section.limit}&offset=${offset}`);
      if (!valid(token) || section.seq !== seq) return;
      const rows = list(data?.items);
      const scoped = rows.filter((row) => !row.class_id || row.class_id === state.classId);
      section.items = append ? [...new Map([...section.items, ...scoped].map((row) => [row.id, row])).values()] : scoped;
      for (const material of scoped) {
        const cached = current.sourceDocs.get(material.id);
        if (cached) current.sourceDocs.set(material.id, { ...cached, material });
      }
      section.offset = offset; section.hasMore = rows.length === section.limit; section.loaded = true;
    } catch (error) { if (valid(token) && section.seq === seq) section.error = errorText(error); }
    finally { if (valid(token) && section.seq === seq) { section.loading = false; repaint(); } }
  }

  async function loadJobs(force = false, token = syncContext(), quiet = false) {
    const section = current.jobs;
    if (!active() || (section.loaded && !force) || (section.loading && !force)) return;
    const seq = ++section.seq;
    section.loading = true; section.error = "";
    if (!quiet) repaint();
    try {
      const params = classQuery(new URLSearchParams({ limit: String(section.limit), offset: String(section.offset) }));
      const data = await api.call("GET", `${path("question-jobs")}?${params}`);
      if (!valid(token) || section.seq !== seq) return;
      section.items = list(data?.items); section.total = data?.total ?? null; section.loaded = true;
    } catch (error) { if (valid(token) && section.seq === seq) section.error = errorText(error); }
    finally {
      if (valid(token) && section.seq === seq) {
        section.loading = false;
        // Polling does not replace a form, IME composition, or a different app drawer.
        if (!quiet || (!current.modal && !state.drawer && current.tab === "jobs")) repaint();
      }
    }
  }

  async function loadKnowledge(force = false, token = syncContext()) {
    const section = current.knowledge;
    if (!active() || (section.loaded && !force) || (section.loading && !force)) return;
    const seq = ++section.seq;
    section.loading = true; section.error = "";
    try {
      const data = await api.call("GET", path("knowledge-points"));
      if (!valid(token) || section.seq !== seq) return;
      section.items = list(data?.items).filter((item) => item.status === "confirmed"); section.loaded = true;
    } catch (error) { if (valid(token) && section.seq === seq) section.error = errorText(error); }
    finally { if (valid(token) && section.seq === seq) { section.loading = false; repaint(); } }
  }

  function knowledgeTitle(id) { const item = current.knowledge.items.find((point) => point.id === id); return item ? `${item.code ? `${item.code} · ` : ""}${item.title}` : id; }

  function renderKnowledge(modal) {
    const section = current.knowledge, selected = list(modal.data.knowledge_points);
    const known = new Set(section.items.map((item) => item.id));
    const unknown = selected.filter((id) => !known.has(id));
    return `<fieldset class="library-knowledge"><legend>知识点（可选）</legend><p class="library-muted">仅选择本课程已确认的知识点，用于后续配卷。</p>${section.loading ? '<p role="status">正在读取已确认知识点…</p>' : ""}${section.error ? `${notice(section.error, "error")}${button("refresh-knowledge", "重试读取知识点")}` : ""}<div class="library-knowledge-options">${section.items.map((item) => check("knowledge_points", knowledgeTitle(item.id), { value: item.id, checked: selected.includes(item.id) })).join("") || (!section.loading && !section.error ? '<p class="library-muted">本课程暂无已确认知识点，可暂不选择。</p>' : "")}${unknown.map((id) => check("knowledge_points", `${id}（不在当前已确认列表，请取消选择）`, { value: id, checked: true })).join("")}</div></fieldset>`;
  }

  function confirmedKnowledge(d) {
    const selected = [...new Set(list(d.knowledge_points))];
    const ids = new Set(current.knowledge.items.map((item) => item.id));
    if (selected.some((id) => !ids.has(id))) throw new Error("所选知识点未确认或已不可用，请从已确认知识点中重新选择。");
    return selected;
  }

  function schedulePoll() {
    stopPoll();
    if (!root?.isConnected || !active() || current.tab !== "jobs" || !current.jobs.items.some(isPending)) return;
    const token = epoch;
    pollTimer = setTimeout(async () => {
      pollTimer = null;
      if (!valid(token) || !root?.isConnected || current.tab !== "jobs") return;
      if (!current.modal && !state.drawer && !root.ownerDocument?.hidden) await loadJobs(true, token, true);
      if (valid(token)) schedulePoll();
    }, 8000);
  }

  function queueInfo(job) { return job.queue_job || job.queue || {}; }
  function queueId(job) { return job.queue_job_id || queueInfo(job).job_id || null; }
  function jobStatus(job) {
    if (["quarantined", "failed", "cancelled"].includes(job.status)) return job.status;
    if (job.status === "completed" && list(job.published_versions || job.state?.published_versions).length) return job.input?.intent === "evaluation" ? "evaluation_complete" : "approved";
    const queued = job.queue_status || queueInfo(job).status;
    return ["succeeded", "completed"].includes(queued || job.status) ? "review_pending" : queued || job.status;
  }
  function isPending(job) { return ["pending", "queued", "leased", "running", "retry_wait", "not_dispatched"].includes(jobStatus(job)); }
  function jobReason(job) { return job.reason || job.error || job.state?.quarantine_reason || job.queue_reason || queueInfo(job).reason || ""; }

  function renderLibrary() {
    syncContext();
    return `<section class="teacher-library" data-library-root>${renderInner()}</section>`;
  }

  function renderInner() {
    const heading = `<header class="library-heading"><div><p class="library-context">${e(state.courseName || "当前课程")}${state.className ? ` / ${e(state.className)}` : ""}</p><h1>题库与材料</h1><p class="library-muted">从课程原文到可用题目，保留来源、审查与每一次修订。</p></div>${button("prepare-goal", "准备学习目标", { disabled: !active() || !state.classId })}</header>`;
    if (!active()) return `${heading}${notice(state.live ? "请选择有权限的课程后查看题库。" : "正式服务未连接。这里不展示示例题目，请连接服务后重新进入。", "warn")}`;
    return `${heading}<nav class="library-tabs" aria-label="题库与材料工作区">${[["questions", "题库"], ["materials", "课程材料"], ["jobs", "备题任务"]].map(([tab, label]) => `<button type="button" data-library-action="tab" data-library-value="${tab}" class="library-tab" aria-current="${current.tab === tab ? "page" : "false"}">${label}</button>`).join("")}</nav>
      ${current.pageError ? notice(current.pageError, "error") : ""}
      <div class="library-surface">${current.tab === "questions" ? renderQuestions() : current.tab === "materials" ? renderMaterials() : renderJobs()}</div>
      ${current.modal ? renderModal() : ""}`;
  }

  function field(scope, key, label, options = {}) {
    const data = scope === "filters" ? current.filters : current.modal?.data || {};
    const attrs = `name="${e(key)}" data-library-field="${e(key)}" data-library-scope="${e(scope)}" ${options.required ? "required" : ""} ${options.disabled ? "disabled" : ""}`;
    const value = data[key] ?? "";
    let control;
    if (options.options) control = `<select ${attrs}>${optionList(options.options, value, options.emptyLabel)}</select>`;
    else if (options.textarea) control = `<textarea ${attrs} rows="${options.rows || 4}" maxlength="${options.max || 8000}" ${options.placeholder ? `placeholder="${e(options.placeholder)}"` : ""}>${e(value)}</textarea>`;
    else control = `<input ${attrs} type="${options.type || "text"}" ${options.type === "file" ? `accept="${e(options.accept || "")}"` : `value="${e(value)}"`} ${options.min != null ? `min="${options.min}"` : ""} ${options.max != null ? (options.type === "number" ? `max="${options.max}"` : `maxlength="${options.max}"`) : ""} ${options.step ? `step="${options.step}"` : ""} ${options.placeholder ? `placeholder="${e(options.placeholder)}"` : ""}>`;
    return `<label class="library-field${options.wide ? " library-field-wide" : ""}"><span>${e(label)}${options.required ? " <span class=\"library-required\">必填</span>" : ""}</span>${control}${options.hint ? `<small>${e(options.hint)}</small>` : ""}</label>`;
  }

  function check(key, label, { required = false, value, checked, disabled = false } = {}) {
    const chosen = checked !== undefined ? checked : Boolean(current.modal?.data?.[key]);
    return `<label class="library-check"><input type="checkbox" name="${e(key)}" data-library-field="${e(key)}" data-library-scope="modal" ${value !== undefined ? `data-library-value="${e(value)}"` : ""} ${chosen ? "checked" : ""} ${required ? "required" : ""} ${disabled ? "disabled" : ""}><span>${e(label)}</span></label>`;
  }

  function renderQuestions() {
    const section = current.questions;
    const knowledge = section.facets.knowledge_points || section.facets.knowledge_point || [];
    const facetIds = Array.isArray(knowledge) ? knowledge.map((value) => typeof value === "string" ? value : value.value || value.id || value.name || value.knowledge_point || value.title).filter(Boolean) : Object.keys(knowledge);
    const knowledgeIds = [...new Set([...current.knowledge.items.map((item) => item.id), ...facetIds, ...(current.filters.knowledge_point ? [current.filters.knowledge_point] : [])])];
    const knowledgeOptions = Object.fromEntries(knowledgeIds.map((id) => [id, knowledgeTitle(id)]));
    return `<div class="library-toolbar"><div><h2>课程题库</h2><p class="library-muted">只有审查通过的题目才可用于备题；手动录入和导入同样经过审查。</p></div><div class="library-actions">${button("manual", "手动录题", { primary: true })}${button("import", "导入题目")}${button("generate", "AI 生成")}</div></div>
      <form class="library-filters" data-library-form="filters">
        ${field("filters", "q", "搜索题目", { type: "search", max: 200, placeholder: "题干、解析或知识点" })}
        ${field("filters", "question_type", "题型", { options: TYPES, emptyLabel: "全部题型" })}
        ${field("filters", "status", "状态", { options: STATUSES, emptyLabel: "全部状态" })}
        ${field("filters", "knowledge_point", "知识点", { options: knowledgeOptions, emptyLabel: "全部知识点" })}
        ${field("filters", "difficulty", "难度", { options: DIFFICULTIES, emptyLabel: "全部难度" })}
        <div class="library-filter-actions"><button class="library-button library-button-primary" type="submit" ${section.loading ? "disabled" : ""}>查找</button>${button("clear-filters", "重置")}</div>
      </form>
      ${section.error ? `${notice(section.error, "error")}${button("refresh-questions", "重新读取")}` : ""}
      <div class="library-list-summary"><span>${section.loaded ? `找到 ${e(section.total)} 个题目版本` : "尚未读取题库"}${section.loading ? " · 正在读取" : ""}</span>${button("refresh-questions", "刷新", { disabled: section.loading })}</div>
      ${current.selected.size ? `<div class="library-selection" role="status"><strong>已选 ${current.selected.size} 个版本</strong><div class="library-actions">${button("export-questions", "导出所选完整题目", { primary: true })}${button("bulk-pause", "暂停使用")}${button("bulk-retire", "退役", { danger: true })}${button("clear-selection", "取消选择")}</div></div>` : ""}
      ${section.loading && !section.loaded ? `<div class="library-empty" aria-busy="true">正在读取真实题库…</div>` : section.items.length ? `<div class="library-question-list" aria-busy="${section.loading}"><label class="library-select-page"><input type="checkbox" data-library-field="select-page" ${section.items.every((row) => current.selected.has(questionKey(row))) ? "checked" : ""}>选择当前页的题目版本</label>${section.items.map(renderQuestionRow).join("")}</div>` : !section.error ? `<div class="library-empty"><h3>${Object.values(current.applied).some(Boolean) ? "没有匹配的题目" : "课程题库还没有题目"}</h3><p>${Object.values(current.applied).some(Boolean) ? "调整筛选条件，再查找一次。" : "可以手动录题、下载模板导入，或先确认课程材料后生成。"}</p><div class="library-actions">${button("manual", "手动录题", { primary: true })}${button("import", "下载模板并导入")}</div></div>` : ""}
      ${section.loaded ? pagination("questions", section.offset, section.limit, section.total, section.loading) : ""}`;
  }

  function renderQuestionRow(row) {
    const q = asQuestion(row), id = questionId(row), key = questionKey(row);
    const knowledge = list(q.knowledge_points || row.knowledge_points);
    const usage = row.usage || {};
    const usageCount = usage.task_count;
    return `<article class="library-question"><label class="library-row-select"><input type="checkbox" data-library-field="select-question" data-library-value="${e(key)}" aria-label="选择题目 ${e(q.stem || id)}" ${current.selected.has(key) ? "checked" : ""}></label><div class="library-question-main"><div class="library-meta">${badge(TYPES[q.type || row.question_type] || "题型未提供")}${statusBadge(row.status || q.status)}<span>版本 ${e(row.version || 1)}</span>${(q.difficulty || row.difficulty) ? `<span>${e(DIFFICULTIES[q.difficulty || row.difficulty] || q.difficulty || row.difficulty)}</span>` : ""}</div><button class="library-question-title" type="button" data-library-action="question-detail" data-library-value="${e(key)}">${e(q.stem || row.stem || "查看题目内容")}</button><div class="library-question-foot"><span>${knowledge.length ? knowledge.map((kp) => e(typeof kp === "object" ? knowledgeTitle(kp.id || kp.value) || kp.title || kp.code : knowledgeTitle(kp))).join("、") : "未标注知识点"}</span><span>${usageCount == null ? "使用记录未提供" : `用于 ${e(usageCount)} 份任务`}</span></div></div>${button("question-detail", "查看详情", { attrs: `data-library-value="${e(key)}"` })}</article>`;
  }

  function pagination(name, offset, limit, total, loading) {
    const next = total == null ? (name === "jobs" ? current.jobs.items.length === limit : true) : offset + limit < total;
    return `<div class="library-pagination"><span>第 ${Math.floor(offset / limit) + 1} 页${total == null ? "" : ` / 共 ${Math.max(1, Math.ceil(total / limit))} 页`}</span><div class="library-actions">${button("page", "上一页", { disabled: loading || offset === 0, attrs: `data-library-value="${name}:prev"` })}${button("page", "下一页", { disabled: loading || !next, attrs: `data-library-value="${name}:next"` })}</div></div>`;
  }

  function renderMaterials() {
    const section = current.materials;
    const visible = section.items.filter((material) => (!section.lifecycle || lifecycle(material) === section.lifecycle) && (!section.usage_scope || usageScope(material) === section.usage_scope));
    return `<div class="library-toolbar"><div><h2>课程材料</h2><p class="library-muted">上传或粘贴原文，核对文字与使用权限后再确认。停用和已被替换的材料保留记录，不再作为新任务来源。</p></div><div class="library-actions">${button("upload-material", "上传材料", { primary: true })}${button("paste-material", "粘贴原文")}${button("refresh-materials", "刷新", { disabled: section.loading })}</div></div>
      <div class="library-material-filters"><label class="library-field"><span>材料使用状态</span><select data-library-field="material-lifecycle-filter">${optionList(MATERIAL_LIFECYCLE, section.lifecycle, "全部状态")}</select></label><label class="library-field"><span>材料用途</span><select data-library-field="material-usage-filter">${optionList(USAGE_SCOPES, section.usage_scope, "全部用途")}</select></label><p class="library-muted">在已读取的 ${section.items.length} 份材料中显示 ${visible.length} 份${section.hasMore ? "；还有材料尚未读取，请加载更多。" : "。"}</p></div>
      ${section.error ? notice(section.error, "error") : ""}
      ${section.loading && !section.loaded ? '<div class="library-empty" aria-busy="true">正在读取课程材料…</div>' : visible.length ? `<div class="library-material-list">${visible.map((material) => `<article class="library-material"><div class="library-material-mark" aria-hidden="true">${e((material.format || "文档").toUpperCase())}</div><div class="library-material-main"><div class="library-meta">${lifecycleBadge(material)}${badge(MATERIAL_STATUS[material.status] || material.status, material.status === "confirmed" ? "ok" : "warn")}${badge(USAGE_SCOPES[usageScope(material)] || "用途未提供")}<span>${material.class_id ? "当前班级" : "课程共用"}</span><span>修订 ${e(material.revision)}</span></div><h3>${e(materialName(material))}</h3><p class="library-muted">${material.rights_confirmed ? "使用权已确认" : "使用权待确认"}${material.allow_generation ? " · 允许出题" : ""}${material.external_processing_allowed ? " · 允许外发处理" : ""}</p>${provenance(material)}${material.replacement_material_id ? `<p class="library-muted">替代材料编号：${e(material.replacement_material_id)}</p>` : ""}</div>${button("material-detail", "核对原文与管理", { attrs: `data-library-value="${e(material.id)}"` })}</article>`).join("")}</div>` : !section.error ? `<div class="library-empty"><h3>${section.items.length ? "没有匹配状态与用途的已读取材料" : "先添加一份课程原文"}</h3><p>${section.items.length ? "调整筛选条件，或继续加载材料。" : "材料确认前不会成为出题依据，也不会自动向学生公开。"}</p></div>` : ""}
      ${section.hasMore ? button("more-materials", "加载更多材料", { disabled: section.loading }) : ""}`;
  }

  function renderJobs() {
    const section = current.jobs;
    return `<div class="library-toolbar"><div><h2>备题任务</h2><p class="library-muted">查看生成、独立审查与复核的实际进度。未完成或失败的任务不计入可用题目。</p></div><div class="library-actions">${button("generate", "AI 生成", { primary: true })}${button("refresh-jobs", "刷新进度", { disabled: section.loading })}</div></div>
      ${section.error ? notice(section.error, "error") : ""}
      ${section.loading && !section.loaded ? '<div class="library-empty" aria-busy="true">正在读取备题任务…</div>' : section.items.length ? `<div class="library-job-list">${section.items.map(renderJob).join("")}</div>` : !section.error ? '<div class="library-empty"><h3>还没有备题任务</h3><p>手动录题、确认导入或 AI 生成后，审查进度会出现在这里。</p></div>' : ""}
      ${section.loaded ? pagination("jobs", section.offset, section.limit, section.total, section.loading) : ""}`;
  }

  function renderJob(job) {
    const status = jobStatus(job), queue = queueInfo(job), reason = jobReason(job);
    const name = job.kind === "question_edit" ? "题目修订审查" : job.input?.candidates ? "导入 / 手动题目审查" : "题目生成与审查";
    const published = list(job.published_versions || job.state?.published_versions);
    const resumable = job.can_resume === true;
    const unmetered = Object.values(job.state?.calls || {}).some((call) => call.billing_mode === "unmetered_competition");
    const cancellable = job.can_cancel === true;
    return `<article class="library-job"><div class="library-job-header"><div><h3>${e(name)}</h3><p class="library-muted">${e(stamp(job.created_at))}</p></div>${badge(JOB_STATUS[status] || status || "状态未提供", status === "approved" ? "ok" : ["failed", "quarantined", "awaiting_confirmation", "review_pending"].includes(status) ? "warn" : "")}</div><p><strong>当前阶段</strong> ${e(STAGES[job.stage] || job.stage || "等待任务调度")}</p>${reason ? notice(text(reason), "warn") : ""}${queue.cost_pending ? `<p class="library-muted">${unmetered ? "调用用量仍待核对，以服务端记录为准。" : "调用用量或费用仍待核对，以服务端记录为准。"}</p>` : ""}${queue.reconciliation_required ? notice("此任务需要核对调用结果，请联系管理员处理后再恢复。", "warn") : ""}${published.length ? `<div class="library-actions">${published.map((row) => button("published-question", `查看${status === "evaluation_complete" ? "评测候选题" : "已通过题目"} · 版本 ${row.version}`, { attrs: `data-library-value="${e(questionKey(row))}"` })).join("")}</div>` : ""}<details class="library-record"><summary>任务记录</summary><dl class="library-definition"><dt>任务编号</dt><dd>${e(jobId(job))}</dd><dt>审查修订</dt><dd>${e(job.revision ?? "未提供")}</dd><dt>处理来源数</dt><dd>${list(job.input?.source_block_ids).length || "未提供"}</dd></dl></details><div class="library-actions">${button("refresh-jobs", "刷新")}${queueId(job) && cancellable ? button("cancel-job", "取消任务", { attrs: `data-library-value="${e(jobId(job))}"` }) : ""}${queueId(job) && resumable ? button("resume-job", "确认后恢复", { attrs: `data-library-value="${e(jobId(job))}"` }) : ""}</div></article>`;
  }

  function newModal(type, data = {}, extra = {}) {
    if (!current.modal) current.restoreFocus = openingFocus || focusSnapshot(root?.ownerDocument?.activeElement);
    openingFocus = null;
    current.modal = { type, id: ++serial, data, busy: false, loading: false, error: "", dirty: false, ...extra };
    repaint();
    focusModal();
    return current.modal;
  }

  function renderModal() {
    const modal = current.modal;
    const titles = { manual: "手动录题", edit: "修订题目", import: "导入题目", generate: "AI 生成题目", upload: "上传课程材料", paste: "粘贴课程原文", material: "核对课程材料", detail: "题目详情", bulk: "确认批量操作", job: "确认任务操作", lifecycle: "确认材料使用状态", supersede: "确认材料替换", export: "导出所选完整题目" };
    return `<div class="library-overlay"><div class="library-backdrop" aria-hidden="true"></div><aside class="library-drawer" role="dialog" aria-modal="true" aria-labelledby="library-dialog-title" tabindex="-1"><header class="library-drawer-head"><div><h2 id="library-dialog-title">${titles[modal.type] || "题库"}</h2><p class="library-muted">${e(state.courseName || "当前课程")}${state.className ? ` / ${e(state.className)}` : ""}</p></div>${button("close-modal", "关闭", { disabled: modal.busy })}</header><div class="library-drawer-body">${modal.error ? notice(modal.error, "error") : ""}${modal.discardPrompt ? `<div class="library-discard" role="alert"><p>关闭后，本次未提交的内容将丢弃。</p><div class="library-actions">${button("keep-editing", "继续编辑", { primary: true })}${button("discard-modal", "丢弃并关闭", { danger: true })}</div>` : ""}${modal.loading ? '<div class="library-empty" aria-busy="true">正在读取内容…</div>' : renderModalBody(modal)}</div></aside></div>`;
  }

  function renderModalBody(modal) {
    if (["manual", "edit"].includes(modal.type)) return renderQuestionForm(modal);
    if (modal.type === "generate") return renderGenerate(modal);
    if (modal.type === "import") return renderImport(modal);
    if (["upload", "paste"].includes(modal.type)) return renderUpload(modal);
    if (modal.type === "material") return renderMaterialDetail(modal);
    if (["lifecycle", "supersede"].includes(modal.type)) return renderMaterialAction(modal);
    if (modal.type === "export") return renderExport(modal);
    if (modal.type === "detail") return renderQuestionDetail(modal);
    if (modal.type === "bulk") return renderBulk(modal);
    if (modal.type === "job") return renderJobAction(modal);
    return "";
  }

  function formFooter(label, modal) {
    return `<div class="library-form-footer"><p class="library-muted">${modal.busy ? "正在提交，请勿重复操作。" : "请核对所选内容后提交。"}</p><button class="library-button library-button-primary" type="submit" ${modal.busy ? "disabled" : ""}>${modal.busy ? "正在提交…" : e(label)}</button></div>`;
  }

  function renderQuestionForm(modal) {
    const d = modal.data, choice = ["single_choice", "multi_choice"].includes(d.type);
    return `<p class="library-muted">${modal.type === "edit" ? `原版本 ${e(modal.original.version)} 保持不变；新版本通过独立审查后才可使用。` : "录入后进入独立审查，不直接成为可用题目。"}</p><form data-library-form="question"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}><div class="library-form-grid">${field("modal", "type", "题型", { options: TYPES, required: true })}${field("modal", "points", "分值", { type: "number", min: 0.01, max: 10000, step: "0.01", required: true })}${field("modal", "difficulty", "难度", { options: DIFFICULTIES, emptyLabel: "未标注" })}</div>${renderKnowledge(modal)}
      ${field("modal", "stem", "题干", { textarea: true, rows: 5, required: true })}
      ${choice ? field("modal", "options", "选项（每行一个，按 A、B、C… 顺序）", { textarea: true, rows: 5, max: 24000, required: true, hint: "至少 2 个、至多 12 个不重复选项；无需输入字母编号。" }) : ""}
      ${d.type === "judge" ? field("modal", "answer", "正确答案", { options: { true: "正确", false: "错误" }, emptyLabel: "请选择", required: true }) : field("modal", "answer", choice ? (d.type === "multi_choice" ? "正确选项字母（如 A,C）" : "正确选项字母（如 A）") : d.type === "fill" ? "参考答案（单空；可接受的答案每行一个）" : "参考答案", { textarea: !choice, required: true, rows: 3 })}
      ${["fill", "text"].includes(d.type) ? field("modal", "answer_format", "答案格式", { options: { text: "文本", math: "数学表达式" } }) : ""}
      ${field("modal", "explanation", "解析", { textarea: true, rows: 4 })}
      ${field("modal", "rubric", "评分细则", { textarea: true, rows: 4, max: 40000, required: d.type === "text", placeholder: "得分点 | 分值，每行一项", hint: `${d.type === "text" ? "作答题必须填写评分细则。" : ""}例如：列式正确 | 2。各项分值之和须等于题目分值。` })}
      ${renderSources(modal)}${check("external_processing_allowed", "我同意将本题及所选来源发送至外部模型服务，进行独立求解与审查。", { required: true })}${formFooter(modal.type === "edit" ? "提交新版本审查" : "提交候选题审查", modal)}</fieldset></form>`;
  }

  function sourceIntent(d) { return d.intent || "production"; }
  function eligibleMaterials(localPreview = false, intent = "production") { return current.materials.items.filter((m) => lifecycle(m) === "active" && usageScope(m) === intent && m.rights_confirmed && m.allow_generation && (localPreview || m.external_processing_allowed) && ["confirmed", "partial"].includes(m.status)); }
  function sourceBlock(id) { for (const doc of current.sourceDocs.values()) { const found = list(doc.blocks).find((block) => block.id === id); if (found) return { ...found, material: doc.material, validationError: doc.validationError }; } return null; }
  function sourceIssue(d, { localPreview = false } = {}) {
    if (!["production", "evaluation"].includes(sourceIntent(d))) return "请选择正式出题或评测验证用途。";
    for (const id of list(d.source_block_ids)) {
      const block = sourceBlock(id), material = block?.material;
      if (!block || !material) return `来源 ${id} 的信息尚未读全，请重试读取或明确移除该来源。已选来源仍保留。`;
      if (block.validationError) return `来源“${materialName(material)}”未能重新核验：${block.validationError}。请重试读取或明确移除，已选来源仍保留。`;
      if (lifecycle(material) !== "active") return `来源“${materialName(material)} / ${block.anchor}”${MATERIAL_LIFECYCLE[lifecycle(material)] || "当前不可用"}，不能用于新任务。请明确移除或选择已确认的替代来源；已选来源仍保留。`;
      if (usageScope(material) !== sourceIntent(d)) return `来源“${materialName(material)}”仅用于${USAGE_SCOPES[usageScope(material)] || "其他用途"}，与当前${USAGE_SCOPES[sourceIntent(d)]}用途不符。请核对并移除或更换，已选来源仍保留。`;
      if (block.status !== "confirmed" || !["confirmed", "partial"].includes(material.status) || !material.rights_confirmed || !material.allow_generation || (material.course_id && material.course_id !== state.courseId) || (material.class_id && material.class_id !== state.classId)) return `来源“${materialName(material)} / ${block.anchor}”未确认、许可或适用范围已变化，请核对后更换。已选来源仍保留。`;
      if (!localPreview && material.external_processing_allowed !== true) return `来源“${materialName(material)}”未获外发许可，当前仅可本地预览。请更换已获许可的来源；勾选本次送审许可不会更改材料授权。`;
    }
    return "";
  }
  function sourcePermissionIssue(d) { return sourceIssue(d); }

  function renderSources(modal) {
    const d = modal.data, ids = list(d.source_block_ids), localPreview = modal.type === "import", materials = eligibleMaterials(localPreview, sourceIntent(d));
    const doc = current.sourceDocs.get(d.source_material_id);
    const query = String(d.source_search || "").toLocaleLowerCase();
    const blocks = materials.some((material) => material.id === d.source_material_id) ? list(doc?.blocks).filter((block) => block.status === "confirmed" && (!query || `${block.anchor} ${block.text}`.toLocaleLowerCase().includes(query))) : [];
    const issue = sourceIssue(d, { localPreview });
    return `<section class="library-source-picker"><h3>选择出题与审查来源 <span class="library-muted">${ids.length} / 8</span></h3>${["generate", "manual"].includes(modal.type) ? field("modal", "intent", "任务用途", { options: USAGE_SCOPES, required: true, hint: "正式出题只使用正式材料；评测验证只使用评测材料。切换用途会保留已选来源并提示冲突。" }) : `<p class="library-muted">当前用途：${e(USAGE_SCOPES[sourceIntent(d)])}</p>`}<p class="library-muted">${localPreview ? "本地预览可选择用途匹配、启用中、已确认且允许出题的材料，无需材料外发许可。预览不会改变授权；独立审查前仍须核验全部来源的外发许可。" : "仅列出用途匹配、启用中、已确认、允许出题且允许外发处理的材料。提交前会重新核验全部已选原文。"}</p>${issue ? notice(issue, "warn") : ""}${button("refresh-sources", "重新核验已选来源", { disabled: modal.busy || modal.sourcesLoading })}${current.materials.error ? notice(current.materials.error, "error") : ""}${current.materials.loading ? '<p role="status">正在读取来源材料…</p>' : ""}${materials.length ? `${field("modal", "source_material_id", "来源材料", { options: Object.fromEntries(materials.map((m) => [m.id, `${m.title || m.filename || m.id}${localPreview && !m.external_processing_allowed ? "（仅本地预览）" : ""}`])), emptyLabel: "选择材料查看原文块" })}${field("modal", "source_search", "在当前材料中查找", { type: "search", max: 200, placeholder: "输入原文关键词" })}${modal.sourcesLoading ? '<p role="status">正在读取来源原文…</p>' : d.source_material_id ? `<div class="library-source-list">${blocks.map((block) => `<div class="library-source-block">${check("source_block_ids", `${block.anchor} · ${block.text.slice(0, 100)}`, { value: block.id, checked: ids.includes(block.id), disabled: ids.length >= 8 && !ids.includes(block.id) })}<details><summary>核对完整原文与来源</summary>${provenance(doc.material)}<div class="library-original">${e(block.text)}</div></details></div>`).join("") || '<p class="library-muted">当前材料没有匹配的已确认原文块。</p>'}</div>` : ""}` : `<p class="library-muted">当前没有可用来源。请先在课程材料中核对并确认原文与权限。</p>`}${current.materials.hasMore ? button("more-materials", "加载更多来源材料", { disabled: current.materials.loading }) : ""}${ids.length ? `<div class="library-selected-sources"><strong>已选来源</strong>${ids.map((id) => { const block = sourceBlock(id); return `<div><span>${e(block ? `${materialName(block.material)} / ${block.anchor} · ${MATERIAL_LIFECYCLE[lifecycle(block.material)] || lifecycle(block.material)} · ${USAGE_SCOPES[usageScope(block.material)] || usageScope(block.material)}` : id)}</span>${button("remove-source", "移除", { attrs: `data-library-value="${e(id)}"` })}</div>`; }).join("")}</div>` : ""}</section>`;
  }

  function renderGenerate(modal) {
    const total = ["single_choice", "judge", "fill", "text"].reduce((sum, type) => sum + Number(modal.data[`count_${type}`] || 0), 0);
    return `${modal.prefillNote ? notice(modal.prefillNote) : ""}<p class="library-muted">按题型数量生成候选题，再进行独立求解、答案比对与必要复核。单次合计 1–10 题。</p><form data-library-form="generate"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}><div class="library-count-grid">${["single_choice", "judge", "fill", "text"].map((type) => field("modal", `count_${type}`, TYPES[type], { type: "number", min: 0, max: 10, step: 1, required: true })).join("")}</div><p class="library-count-total" data-library-count-total>合计 ${e(total)} 题</p><div class="library-form-grid">${field("modal", "difficulty", "难度", { options: DIFFICULTIES, required: true })}</div>${renderKnowledge(modal)}${field("modal", "instruction", "出题要求", { textarea: true, max: 2000, rows: 3, placeholder: "补充范围、情境或需要避免的内容" })}${renderSources(modal)}${check("external_processing_allowed", "我同意将所选来源和出题要求发送至外部模型服务，生成并独立审查题目。", { required: true })}${formFooter("创建备题任务", modal)}</fieldset></form>`;
  }

  function renderImport(modal) {
    if (modal.confirmation) {
      const result = modal.confirmation;
      return `<h3>导入提交结果</h3><p>已送审 <strong>${e(result.accepted_count ?? list(result.jobs).length)}</strong> 行，未送审 <strong>${e(result.failed_count ?? list(result.failures).length)}</strong> 行。</p>${list(result.failures).map((failure) => notice(`第 ${failure.row_number} 行：${failure.error}`, "error")).join("")}<p class="library-muted">已送审行仍需独立审查；未送审行请按提示修改文件后重新预览。不会把失败行当作成功导入。</p><div class="library-actions">${button("import-jobs", "查看审查任务", { primary: true })}${button("import-again", "修改文件后重新预览")}</div>`;
    }
    const preview = modal.preview;
    const rows = list(preview?.rows || preview?.items);
    const selected = list(modal.data.row_numbers);
    const permissionIssue = sourcePermissionIssue(modal.data);
    return `<p class="library-muted">使用固定模板填写 CSV、JSON 或 XLSX，最多 100 题、2 MiB。预览只在本地服务校验；确认提交后才会进入外部审查。</p><div class="library-template-links"><strong>下载导入模板</strong>${["csv", "json", "xlsx"].map((format) => `<a class="library-button" href="/api/v1${path(`question-imports/template?format=${format}`)}" download> ${format.toUpperCase()} 模板</a>`).join("")}</div><form data-library-form="import-preview"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "import_file", "题目文件", { type: "file", accept: ".csv,.json,.xlsx", hint: modal.data.import_file?.name ? `已保留所选文件：${modal.data.import_file.name}` : "仅接受固定模板，解析失败与重复题会在预览中显示。" })}${renderSources(modal)}<div class="library-form-footer"><button type="submit" class="library-button" ${modal.busy ? "disabled" : ""}>${modal.busy ? "正在校验…" : "本地校验并预览"}</button></div></fieldset></form>${preview ? `<section class="library-import-preview"><h3>导入预览</h3>${preview.summary ? `<p class="library-muted">${renderImportSummary(preview.summary)}</p>` : ""}${list(preview.errors).map((error) => notice(text(error.message || error), "warn")).join("")}${rows.length ? `<div class="library-import-rows">${rows.map((row) => { const number = row.row_number ?? row.row; const usable = importRowUsable(row); const q = asQuestion(row); const issues = row.errors || row.issues || []; return `<article><div class="library-meta">${check("row_numbers", `第 ${number} 行`, { value: number, checked: selected.includes(Number(number)), disabled: !usable || modal.busy })}${badge(usable ? "可送审" : row.duplicate || row.status === "duplicate" ? "重复" : "校验失败", usable ? "ok" : "warn")}</div><p>${e(q.stem || row.stem || "未解析出题干")}</p>${row.message ? `<p class="library-muted">${e(row.message)}</p>` : ""}${list(issues).map((issue) => `<p class="library-inline-error">${e(typeof issue === "string" ? issue : issue.message || issue.code || text(issue))}</p>`).join("")}${row.duplicate ? `<p class="library-inline-error">检测到重复题，请修改后重新预览。</p>` : ""}</article>`; }).join("")}</div>` : notice("预览没有返回可提交行，请检查模板与文件内容。", "warn")}${permissionIssue ? notice(permissionIssue, "warn") : ""}<form data-library-form="import-confirm"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${check("external_processing_allowed", "我已核对所选行，同意将题目及所选来源发送至外部模型服务独立审查。", { required: true })}<p>已选 <strong>${selected.length}</strong> 行；校验失败与重复行不会提交。</p><button class="library-button library-button-primary" type="submit" ${modal.busy || !selected.length || permissionIssue ? "disabled" : ""}>${modal.busy ? "正在提交…" : "确认并提交审查"}</button></fieldset></form></section>` : ""}`;
  }

  function importRowUsable(row) { return row.status === "valid" && !row.duplicate && list(row.errors).length === 0 && Number.isInteger(Number(row.row_number)) && Number(row.row_number) >= 1; }
  function renderImportSummary(summary) {
    const labels = { total: "总行数", total_rows: "总行数", valid: "通过", valid_rows: "通过", invalid: "失败", invalid_rows: "失败", duplicate: "重复", duplicates: "重复", duplicate_rows: "重复" };
    return Object.entries(summary).filter(([key, value]) => labels[key] && typeof value === "number").map(([key, value]) => `${labels[key]} ${e(value)}`).join("，") || "请逐行核对预览内容。";
  }

  function renderPermissions() {
    return `<div class="library-form-grid">${field("modal", "license_code", "使用许可", { options: LICENSES, required: true })}${field("modal", "license_note", "授权与署名说明", { max: 2000, hint: "已获授权、CC BY、CC BY-SA 必须填写。" })}</div><div class="library-permissions">${Object.entries(PERMISSIONS).map(([key, label]) => check(key, label)).join("")}</div>`;
  }

  function renderUpload(modal) {
    return `${modal.replacementOf ? notice(`正在为“${materialName(modal.replacementOf)}”添加修正版。新材料确认后仍需明确执行替换；当前材料保持原状态。`) : ""}<p class="library-muted">${modal.type === "paste" ? "粘贴内容按所选格式保存为原文。CSV 会按逻辑记录解析，TXT 按文本分块；请保留完整表头和引号，并标注实际来源。" : "支持 TXT、PDF、DOCX、PPTX、XLSX、CSV、XML 与图片，单个文件不超过 20 MB。"} 上传不会自动确认或公开材料。</p><form data-library-form="material-upload"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "title", "材料名称", { max: 240, required: true })}${modal.type === "paste" ? `${field("modal", "paste_format", "原文格式", { options: { txt: "文本原文（TXT）", csv: "完整 CSV 原文（保留表头与引号）" }, required: true })}${field("modal", "paste_text", "课程原文", { textarea: true, rows: 10, max: 2000000, required: true })}` : field("modal", "material_file", "材料文件", { type: "file", accept: ".txt,.md,.markdown,.pdf,.docx,.pptx,.xlsx,.xml,.csv,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp", hint: modal.data.material_file?.name ? `已保留所选文件：${modal.data.material_file.name}` : "扫描页与图片不会被当作已识别文字。" })}<section class="library-source-metadata"><h3>原始来源信息</h3><p class="library-muted">仅填写自己已核对的信息。原文位置可填写实际页码、表格或幻灯片位置；未核实的内容请留空。</p>${field("modal", "source_filename", "原始文件名（手工标注）", { max: 240, placeholder: "如课程资料的实际文件名" })}${field("modal", "source_locator", "原文位置（手工标注）", { max: 2000, placeholder: "如已核对的页码、表格或幻灯片位置" })}${field("modal", "source_note", "来源说明（手工标注）", { textarea: true, rows: 2, max: 2000, placeholder: "可记录材料提供方、节选范围或转录说明" })}</section><div class="library-form-grid">${field("modal", "usage_scope", "材料用途", { options: USAGE_SCOPES, required: true, hint: "评测验证材料仅供评测任务使用，不进入正式出题来源。" })}${field("modal", "scope", "材料范围", { options: { course: "本课程共用", ...(state.classId ? { class: "仅当前班级" } : {}) } })}${field("modal", "visibility", "可见范围", { options: { private: "仅本人草稿", shared: "课程共享（负责人确认）" } })}</div>${renderPermissions()}${formFooter("上传并查看待确认块", modal)}</fieldset></form>`;
  }

  function materialData(doc) {
    const m = doc.material || {};
    return { title: m.title, license_code: m.license_code || "unknown", license_note: m.license_note || "", ...Object.fromEntries(Object.keys(PERMISSIONS).map((key) => [key, Boolean(m[key])])), block_ids: [], public_block_ids: [], public_content_attested: false, originals_checked: false, note: "", issues: {} };
  }

  function replacementCandidates(material) {
    return current.materials.items.filter((row) => row.id !== material.id && lifecycle(row) === "active" && row.status === "confirmed" && row.rights_confirmed && usageScope(row) === usageScope(material) && row.visibility === material.visibility && (row.class_id || null) === (material.class_id || null));
  }

  function renderMaterialManagement(modal, material) {
    const pending = current.replacements.get(material.id);
    return `<section class="library-material-management"><h3>材料使用与修正版</h3><p class="library-muted">已确认原文和许可保持固定。停用可阻止新任务引用；替换后旧材料保留，已有题目继续记录原来源。</p>${lifecycle(material) === "superseded" ? notice(`此材料已被替换${material.replacement_material_id ? `，替代材料编号：${material.replacement_material_id}` : ""}，不能重新启用。`, "warn") : `<div class="library-actions">${button("material-lifecycle", lifecycle(material) === "active" ? "停用材料" : "重新启用材料", { disabled: modal.busy || material.can_manage_lifecycle !== true })}${button("material-replace", "选择已确认材料替换", { disabled: modal.busy || material.can_replace !== true })}${button("upload-replacement", "上传修正版", { disabled: modal.busy || material.can_replace !== true })}${button("paste-replacement", "粘贴修正版原文", { disabled: modal.busy || material.can_replace !== true })}</div>`}${pending ? `${notice(`此修正版用于替换“${e(materialName(pending))}”。请先核对并确认原文，再完成替换。`)}${button("finish-replacement", "确认后替换原材料", { primary: true, disabled: modal.busy || lifecycle(material) !== "active" || material.status !== "confirmed" })}` : ""}</section>`;
  }

  function renderMaterialAction(modal) {
    const material = modal.material;
    if (modal.type === "lifecycle") return `<h3>${e(materialName(material))}</h3><div class="library-meta">${lifecycleBadge(material)}<span>修订 ${e(material.revision)}</span></div>${provenance(material)}<p class="library-muted">${modal.targetLifecycle === "inactive" ? "停用后，此材料不再用于新出题、检索或学生访问。历史题目的来源记录保留。" : "启用后，只有已确认、用途匹配且获得对应许可的原文块才可重新用于新任务。"}</p><form data-library-form="material-lifecycle"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "note", "操作备注", { textarea: true, rows: 2, max: 2000 })}${check("confirmed", `我确认${modal.targetLifecycle === "inactive" ? "停用" : "启用"}以上材料修订。`, { required: true })}${formFooter(modal.targetLifecycle === "inactive" ? "确认停用材料" : "确认启用材料", modal)}</fieldset></form>`;
    const candidates = replacementCandidates(material), replacement = modal.replacementDoc?.material;
    return `<h3>替换“${e(materialName(material))}”</h3><p class="library-muted">旧材料修订 ${e(material.revision)}。仅可选择同范围、同用途、启用且已有确认原文的新材料。成功后旧材料停止用于新任务；替换关系保留，不能撤销为启用。</p>${provenance(material)}<form data-library-form="material-supersede"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "replacement_material_id", "替代材料", { options: Object.fromEntries(candidates.map((row) => [row.id, `${materialName(row)} · 修订 ${row.revision}`])), emptyLabel: "请选择已确认的替代材料", required: true })}${modal.replacementLoading ? '<p role="status">正在核对替代材料…</p>' : replacement ? `<article class="library-original-block"><h3>${e(materialName(replacement))}</h3><div class="library-meta">${lifecycleBadge(replacement)}${badge(USAGE_SCOPES[usageScope(replacement)])}<span>修订 ${e(replacement.revision)}</span></div>${provenance(replacement)}<p class="library-muted">已确认原文块：${list(modal.replacementDoc.blocks).filter((block) => block.status === "confirmed").length}</p></article>` : '<p class="library-muted">如需新建修正版，请返回材料详情，上传或粘贴并确认后再替换。</p>'}${current.materials.hasMore ? button("more-materials", "加载更多替代材料", { disabled: current.materials.loading }) : ""}${field("modal", "note", "替换说明", { textarea: true, rows: 2, max: 2000 })}${check("confirmed", "我已核对以上材料、原文来源和修订，确认以所选材料替换旧材料。", { required: true })}${formFooter("确认替换材料", modal)}</fieldset></form>`;
  }

  function renderMaterialDetail(modal) {
    const doc = modal.doc, material = doc?.material;
    if (!material) return `${notice("材料详情未读取成功。", "warn")}${button("retry-modal", "重新读取")}`;
    const blocks = list(doc.blocks), issues = list(doc.issues), editable = lifecycle(material) === "active" && !blocks.some((block) => block.status === "confirmed");
    return `<div class="library-detail-title"><h3>${e(materialName(material))}</h3><div class="library-meta">${lifecycleBadge(material)}${badge(MATERIAL_STATUS[material.status] || material.status)}${badge(USAGE_SCOPES[usageScope(material)] || "用途未提供")}<span>修订 ${e(material.revision)}</span><span>${e(LICENSES[material.license_code] || material.license_code)}</span></div>${provenance(material)}</div>${renderMaterialManagement(modal, material)}${editable ? `<details class="library-record" ${material.rights_confirmed ? "" : "open"}><summary>核对与更新使用权限</summary><form data-library-form="material-permissions"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "title", "材料名称", { required: true, max: 240 })}${renderPermissions()}<button class="library-button" type="submit">保存权限与名称</button></fieldset></form></details>` : '<p class="library-muted">材料已有确认块，原文和许可已固定。需要修改时请上传新材料。</p>'}
      ${issues.length ? `<section class="library-detail-section"><h3>解析疑点</h3>${issues.map((issue) => renderIssue(issue, modal, blocks)).join("")}</section>` : ""}
      <section class="library-detail-section"><h3>原文块核对</h3><p class="library-muted">请与原文件对照后选择。答案与解析不应勾选为学生公开正文。</p><form data-library-form="material-confirm"><fieldset class="library-form-fields" ${modal.busy || lifecycle(material) !== "active" ? "disabled" : ""}>${blocks.map((block) => {
        const blocked = block.status !== "draft" || !block.text?.trim() || issues.some((issue) => issue.status === "open" && issue.severity !== "warning" && (!issue.block_id || issue.block_id === block.id));
        const chosen = modal.data.block_ids.includes(block.id);
        return `<article class="library-original-block"><div class="library-meta"><strong>${e(block.anchor)}</strong>${badge(block.status === "confirmed" ? "已确认" : blocked ? "待解决疑点" : "待确认", block.status === "confirmed" ? "ok" : "warn")}<span>${e(block.kind)}</span></div><div class="library-original">${e(block.text || "未提取到文字，请先解决疑点并人工转录。")}</div>${block.status !== "confirmed" ? `<div class="library-block-actions">${check("block_ids", "已核对，选择确认此块", { value: block.id, checked: chosen, disabled: blocked })}${material.visibility === "shared" && material.allow_student_view && ["text", "table"].includes(block.kind) ? check("public_block_ids", "向学生公开此块", { value: block.id, checked: modal.data.public_block_ids.includes(block.id), disabled: !chosen || blocked }) : ""}</div>` : `<p class="library-muted">${block.student_visible ? "学生可见" : "仅课程教师使用"}</p>`}</article>`;
      }).join("") || '<p class="library-muted">材料没有可核对的原文块。</p>'}${blocks.some((block) => block.status !== "confirmed") ? `${check("originals_checked", "我已对照原文件核实所选文字和来源锚点。", { required: true })}${modal.data.public_block_ids.length ? check("public_content_attested", "我确认向学生公开的块不含答案、解析或个人身份信息。", { required: true }) : ""}${field("modal", "note", "核对备注", { textarea: true, rows: 2, max: 2000 })}<div class="library-form-footer"><span>已选择 ${modal.data.block_ids.length} 个待确认块</span><button type="submit" class="library-button library-button-primary" ${modal.busy || !modal.data.block_ids.length ? "disabled" : ""}>确认所选原文块</button></div>` : '<p class="library-muted">全部原文块已确认。</p>'}</fieldset></form></section>`;
  }

  function renderIssue(issue, modal, blocks) {
    const block = blocks.find((item) => item.id === issue.block_id);
    const saved = modal.data.issues[issue.id] || {};
    if (lifecycle(modal.doc.material) !== "active") return `<article class="library-issue"><p>${e(issue.message || issue.code)}</p><p class="library-muted">此材料当前不可编辑，请使用启用中的修正版。</p></article>`;
    return `<article class="library-issue"><div class="library-meta">${badge(issue.status === "resolved" ? "已解决" : "待解决", issue.status === "resolved" ? "ok" : "warn")}<span>${e(block?.anchor || "整份材料")}</span></div><p>${e(issue.message || issue.code)}</p>${issue.status === "resolved" ? `<p class="library-muted">${e(issue.resolution)}</p>` : issue.code === "source_permission_required" ? '<p class="library-muted">请先在上方核实并保存使用许可。</p>' : !issue.block_id || block?.status === "confirmed" ? '<p class="library-muted">此疑点不能直接改写已确认内容，请核对材料或上传修正版。</p>' : `<form data-library-form="resolve-issue" data-library-value="${e(issue.id)}"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}><label class="library-field"><span>处理说明（必填）</span><input required maxlength="2000" data-library-field="issue-resolution" data-library-value="${e(issue.id)}" value="${e(saved.resolution)}"></label><label class="library-field"><span>${issue.severity === "manual_required" ? "人工转录原文（必填）" : "替换文字（可选）"}</span><textarea ${issue.severity === "manual_required" ? "required" : ""} maxlength="100000" rows="4" data-library-field="issue-replacement" data-library-value="${e(issue.id)}">${e(saved.replacement_text)}</textarea></label><button type="submit" class="library-button">保存疑点处理</button></fieldset></form>`}</article>`;
  }

  function renderQuestionDetail(modal) {
    const detail = modal.detail;
    if (!detail) return `${notice("题目详情未读取成功。", "warn")}${button("retry-modal", "重新读取")}`;
    const row = detail.question || detail, q = asQuestion(row), sources = list(detail.sources || detail.source_blocks || q.source_anchors);
    const review = detail.reviews || detail.review_evidence || q.review_evidence;
    const versions = list(detail.versions), usage = detail.usage || row.usage;
    return `<div class="library-detail-title"><div class="library-meta">${badge(TYPES[q.type] || q.type)}${statusBadge(row.status || q.status)}<span>版本 ${e(row.version || modal.version)}</span><span>${e(q.points ?? "未提供")} 分</span></div><h3>${e(q.stem)}</h3></div>${list(q.options).length ? `<ol class="library-options" type="A">${q.options.map((value) => `<li>${e(value)}</li>`).join("")}</ol>` : ""}<section class="library-detail-section"><h3>参考答案</h3><div class="library-original">${e(answerText(q))}</div><h3>解析</h3><div class="library-original">${e(q.explanation || "未提供解析")}</div></section><section class="library-detail-section"><h3>评分细则</h3>${list(q.rubric).length ? `<ul class="library-rubric">${q.rubric.map((item) => `<li><span>${e(item.point)}</span><strong>${e(Number(item.score_units) / 100)} 分</strong></li>`).join("")}</ul>` : '<p class="library-muted">此版本未提供分项评分细则。</p>'}</section><section class="library-detail-section"><h3>来源锚点与原文</h3>${sources.length ? sources.map((source) => `<article class="library-original-block"><div class="library-meta"><strong>${e(source.anchor || source.block?.anchor || "来源锚点未提供")}</strong>${source.license_code ? badge(LICENSES[source.license_code] || source.license_code) : ""}</div><p class="library-muted">${e(source.title || source.material?.title || source.file_id || "")}${source.source_revision ? ` / 修订 ${e(source.source_revision)}` : ""}</p><div class="library-original">${e(source.text ?? source.block?.text ?? "此来源原文暂不可见，请核对来源材料权限。")}</div>${source.license_note ? `<p class="library-muted">${e(source.license_note)}</p>` : ""}</article>`).join("") : '<p class="library-muted">没有返回可核验来源，请勿推断来源已通过核对。</p>'}</section><section class="library-detail-section"><h3>独立审查记录</h3>${review ? renderReview(review) : '<p class="library-muted">此版本没有提供审查证据。</p>'}</section><section class="library-detail-section"><h3>真实使用记录</h3>${renderUsage(usage)}</section><section class="library-detail-section"><h3>版本记录</h3>${versions.length ? versions.map((version) => `<div class="library-version"><span>版本 ${e(version.version)}</span>${statusBadge(version.status)}${button("detail-version", "查看", { attrs: `data-library-value="${e(version.version)}"`, disabled: Number(version.version) === Number(row.version || modal.version) })}</div>`).join("") : '<p class="library-muted">没有返回其他版本。</p>'}</section><div class="library-actions">${button("edit-question", "修订为新版本", { primary: true })}${button("detail-pause", "暂停使用", { disabled: ["quarantined", "retired"].includes(row.status) })}${button("detail-retire", "退役", { danger: true, disabled: row.status === "retired" })}${button("prepare-from-question", "按此知识点准备目标", { disabled: !state.classId })}</div>`;
  }

  function renderUsage(usage) {
    if (!usage || !Object.keys(usage).length) return '<p class="library-muted">服务端尚未提供使用统计。</p>';
    const metrics = [["task_count", "使用任务数"], ["student_count", "作答学生数"], ["attempt_count", "作答次数"], ["graded_count", "已评分次数"], ["first_attempt_count", "首次作答次数"], ["first_graded_count", "首次已评分次数"], ["average_score", "平均得分"], ["average_score_rate", "平均得分率"], ["first_average_score", "首次平均得分"]];
    return `<dl class="library-usage">${metrics.filter(([key]) => key in usage).map(([key, label]) => {
      const value = usage[key];
      const display = value == null ? "暂无记录" : key === "average_score_rate" ? `${(Number(value) * 100).toFixed(1)}%` : typeof value === "number" && !Number.isInteger(value) ? value.toFixed(2) : value;
      return `<div><dt>${label}</dt><dd>${e(display)}</dd></div>`;
    }).join("")}</dl><p class="library-muted library-usage-note">${usage.last_used_at == null ? "尚无使用时间记录。" : `最近使用：${e(stamp(usage.last_used_at))}`} 均值只使用已评分记录；未评分不按零分计算。</p>`;
  }

  function answerText(q) {
    if (q.type === "single_choice" && Number.isInteger(q.answer)) return `${String.fromCharCode(65 + q.answer)}. ${list(q.options)[q.answer] || ""}`;
    if (q.type === "multi_choice" && Array.isArray(q.answer)) return q.answer.map((index) => `${String.fromCharCode(65 + index)}. ${list(q.options)[index] || ""}`).join("\n");
    if (q.type === "judge") return q.answer === true ? "正确" : q.answer === false ? "错误" : "答案未提供";
    return Array.isArray(q.answer) ? q.answer.join("\n") : text(q.answer) || "答案未提供";
  }

  function renderReview(review) {
    const solution = review.independent_solution;
    return `${solution ? `<dl class="library-definition"><dt>独立求解答案</dt><dd>${e(text(solution.answer))}</dd><dt>可独立求解</dt><dd>${solution.solvable === true ? "是" : solution.solvable === false ? "否" : "未提供"}</dd><dt>求解依据</dt><dd class="library-original">${e(solution.reasoning || "未提供")}</dd><dt>复核次数</dt><dd>${e(review.recheck_count ?? "未提供")}</dd></dl>` : ""}<details class="library-record"><summary>查看完整审查记录</summary><pre>${e(JSON.stringify(review, null, 2))}</pre></details>`;
  }

  function renderExport(modal) {
    return `<p>按下列顺序导出 <strong>${modal.rows.length}</strong> 个明确的题目版本。翻页、筛选或新增版本不会改变本次清单。</p><p class="library-muted">教师题目包包含题干、选项、参考答案、解析、评分细则、来源与审查记录，请保管好下载文件。任何版本不可读取时，本次导出整体失败。</p><ol class="library-confirm-list">${modal.rows.map((row, index) => `<li><strong>${index + 1}. ${e(asQuestion(row).stem || questionId(row))}</strong><span>${e(questionId(row))} · 版本 ${e(row.version || 1)}</span></li>`).join("")}</ol><form data-library-form="question-export"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${field("modal", "format", "题目包格式", { options: { json: "JSON（完整结构）", csv: "CSV（表格）", html: "HTML（阅读与打印）" }, required: true })}${check("confirmed", "我已核对所选题目与版本，确认下载含答案及审查记录的教师题目包。", { required: true })}${formFooter("下载完整题目包", modal)}</fieldset></form>`;
  }

  function renderBulk(modal) {
    const retiring = modal.status === "retired";
    return `<p>${retiring ? "退役" : "暂停使用"}以下 <strong>${modal.rows.length}</strong> 个题目版本。它们将不再用于后续备题；已发布任务保留原版本。</p><ul class="library-confirm-list">${modal.rows.map((row) => `<li><strong>${e(asQuestion(row).stem || questionId(row))}</strong><span>版本 ${e(row.version)} · ${e(STATUSES[row.status] || row.status)}</span></li>`).join("")}</ul>${modal.results?.length ? `<div>${modal.results.map((result) => notice(`${result.label}：${result.ok ? "已完成" : result.error}`, result.ok ? "info" : "error")).join("")}</div>` : ""}<form data-library-form="bulk"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${check("confirmed", `我已核对以上题目和版本，确认${retiring ? "退役" : "暂停使用"}。`, { required: true })}<button type="submit" class="library-button ${retiring ? "library-button-danger" : "library-button-primary"}" ${modal.busy ? "disabled" : ""}>${modal.busy ? "正在逐项处理…" : `确认${retiring ? "退役" : "暂停使用"}`}</button></fieldset></form>`;
  }

  function renderJobAction(modal) {
    const resume = modal.operation === "resume";
    return `<p>${resume ? "恢复将重新尝试处理，可能产生新的模型调用费用。已取消的任务不能恢复。" : "取消会停止后续处理；已经发生的模型调用仍可能计费。取消后不能恢复本次请求。"}</p><p class="library-muted">任务 ${e(jobId(modal.job))}</p><form data-library-form="job-action"><fieldset class="library-form-fields" ${modal.busy ? "disabled" : ""}>${check("confirmed", resume ? "我确认恢复任务，并同意可能产生的新调用费用。" : "我确认取消这项备题任务。", { required: true })}${formFooter(resume ? "确认恢复" : "确认取消", modal)}</fieldset></form>`;
  }

  async function openQuestion(id, version = 1) {
    const modal = newModal("detail", {}, { loading: true, questionId: id, version });
    const token = epoch;
    try {
      const params = classQuery(new URLSearchParams());
      const detail = await api.call("GET", `${path(`questions/${encodeURIComponent(id)}/versions/${version}`)}${params.size ? `?${params}` : ""}`);
      if (!valid(token) || current.modal !== modal) return;
      modal.detail = detail;
      // Older detail projections may carry anchors without their text. Read the actual material, never fabricate excerpts.
      const row = detail.question || detail, q = asQuestion(row);
      const sources = list(detail.sources || detail.source_blocks || q.source_anchors);
      if (sources.some((source) => source.text == null && source.block?.text == null && source.file_id)) {
        const fileIds = [...new Set(sources.filter((source) => source.file_id && source.text == null).map((source) => source.file_id))];
        const docs = await Promise.all(fileIds.map(async (fileId) => {
          try { return [fileId, await api.call("GET", `/materials/${encodeURIComponent(fileId)}`)]; } catch { return [fileId, null]; }
        }));
        if (!valid(token) || current.modal !== modal) return;
        const byId = new Map(docs);
        detail.sources = sources.map((source) => { const doc = byId.get(source.file_id); const block = list(doc?.blocks).find((item) => item.id === source.block_id); return { ...source, ...(block ? { text: block.text, material: doc.material } : {}) }; });
      }
    } catch (error) { if (valid(token) && current.modal === modal) modal.error = errorText(error); }
    finally { if (valid(token) && current.modal === modal) { modal.loading = false; repaint(); } }
  }

  async function openMaterial(id, existing) {
    const modal = newModal("material", {}, { loading: true, materialId: id });
    const token = epoch;
    try {
      const doc = existing || await api.call("GET", `/materials/${encodeURIComponent(id)}`);
      if (!valid(token) || current.modal !== modal) return;
      if (doc?.material?.id !== id) throw new Error("材料编号未核对一致，请重新读取。");
      modal.doc = doc; modal.data = materialData(doc); rememberMaterial(doc);
    } catch (error) { if (valid(token) && current.modal === modal) modal.error = errorText(error); }
    finally { if (valid(token) && current.modal === modal) { modal.loading = false; repaint(); } }
  }

  async function loadSource(id, modal) {
    if (!id || current.sourceDocs.has(id)) { modal.sourcesLoading = false; repaint(); return; }
    const token = epoch;
    modal.sourcesLoading = true; modal.error = ""; repaint();
    try {
      const doc = await api.call("GET", `/materials/${encodeURIComponent(id)}`);
      if (!valid(token) || current.modal !== modal) return;
      if (doc?.material?.id !== id) throw new Error("来源材料编号未核对一致，请重新读取。");
      rememberMaterial(doc);
    } catch (error) { if (valid(token) && current.modal === modal) modal.error = errorText(error); }
    finally { if (valid(token) && current.modal === modal && modal.data.source_material_id === id) { modal.sourcesLoading = false; repaint(); } }
  }

  function openQuestionForm(original) {
    const q = original ? asQuestion(original) : {};
    const d = { type: q.type || "single_choice", answer_format: q.answer_format || "text", points: q.points ?? 1, difficulty: q.difficulty || "", knowledge_points: [...list(q.knowledge_points)], stem: q.stem || "", options: list(q.options).join("\n"), answer: q.type === "single_choice" ? String.fromCharCode(65 + q.answer) : q.type === "multi_choice" ? list(q.answer).map((index) => String.fromCharCode(65 + index)).join(",") : q.type === "judge" ? String(q.answer) : Array.isArray(q.answer) ? q.answer.join("\n") : q.answer || "", explanation: q.explanation || "", rubric: list(q.rubric).map((item) => `${item.point} | ${Number(item.score_units) / 100}`).join("\n"), source_block_ids: list(q.source_anchors).map((source) => source.block_id).filter(Boolean), source_material_id: "", source_search: "", external_processing_allowed: false, intent: q.generation_intent || q.usage_scope || "production" };
    const modal = newModal(original ? "edit" : "manual", d, { original });
    if (original) {
      const token = epoch;
      void Promise.all([...new Set(list(q.source_anchors).map((source) => source.file_id).filter(Boolean))].map(async (id) => {
        try {
          const doc = await api.call("GET", `/materials/${encodeURIComponent(id)}`);
          if (valid(token) && current.modal === modal) { current.sourceDocs.set(id, doc); repaint(); }
        } catch (error) { if (valid(token) && current.modal === modal) { modal.error = errorText(error); repaint(); } }
      }));
    }
    void loadMaterials();
    void loadKnowledge();
  }

  function openUpload(paste, replacementOf = null) {
    newModal(paste ? "paste" : "upload", { title: "", paste_text: "", paste_format: "txt", source_filename: replacementOf?.source_filename || "", source_locator: replacementOf?.source_locator || "", source_note: replacementOf ? `修正版，拟替换 ${materialName(replacementOf)}。${replacementOf.source_note || ""}`.trim() : "", usage_scope: usageScope(replacementOf), scope: replacementOf ? (replacementOf.class_id ? "class" : "course") : state.classId ? "class" : "course", visibility: replacementOf?.visibility || "private", license_code: "unknown", license_note: "", ...Object.fromEntries(Object.keys(PERMISSIONS).map((key) => [key, false])) }, { replacementOf });
  }

  function rememberMaterial(doc) {
    if (!doc?.material?.id) throw new Error("服务端未返回完整材料信息，请重新读取。");
    current.sourceDocs.set(doc.material.id, doc);
    const index = current.materials.items.findIndex((item) => item.id === doc.material.id);
    if (index >= 0) current.materials.items[index] = doc.material;
    else current.materials.items.push(doc.material);
  }

  async function openSupersede(material, replacementDoc = null) {
    const modal = newModal("supersede", { replacement_material_id: replacementDoc?.material?.id || "", note: "", confirmed: false }, { material, replacementDoc });
    if (replacementDoc) rememberMaterial(replacementDoc);
    await loadMaterials(true);
    return modal;
  }

  async function loadReplacement(id, modal) {
    modal.replacementDoc = null; modal.data.confirmed = false;
    if (!id) { repaint(); return; }
    const token = epoch;
    modal.replacementLoading = true; modal.error = ""; repaint();
    try {
      const doc = await api.call("GET", `/materials/${encodeURIComponent(id)}`);
      if (!valid(token) || current.modal !== modal || modal.data.replacement_material_id !== id) return;
      if (doc?.material?.id !== id) throw new Error("替代材料编号未核对一致，请重新选择。");
      rememberMaterial(doc); modal.replacementDoc = doc;
    } catch (error) { if (valid(token) && current.modal === modal && modal.data.replacement_material_id === id) modal.error = errorText(error); }
    finally { if (valid(token) && current.modal === modal && modal.data.replacement_material_id === id) { modal.replacementLoading = false; repaint(); } }
  }

  function closeModal(discard = false) {
    if (current.modal?.busy) return;
    if (current.modal?.dirty && !discard) { current.modal.discardPrompt = true; repaint(); return; }
    dismissModal(); repaint();
    schedulePoll();
  }

  function bind(container) {
    syncContext();
    const next = container?.matches?.("[data-library-root]") ? container : container?.querySelector?.("[data-library-root]");
    if (!next) { releaseModalEffects(); root = null; stopPoll(); return; }
    root = next;
    if (!listeners.has(next)) {
      listeners.add(next);
      next.addEventListener("click", onClick);
      next.addEventListener("input", onField);
      next.addEventListener("change", onField);
      next.addEventListener("submit", onSubmit);
      next.addEventListener("keydown", onKeydown);
      next.addEventListener("focusin", (event) => {
        if (current.modal && root?.querySelector(".library-drawer")?.contains(event.target)) current.modal.lastFocus = focusSnapshot(event.target);
      });
    }
    syncModalEffects();
    if (current.modal) focusModal();
    schedulePoll();
  }

  function onKeydown(event) {
    const modal = root?.querySelector(".library-drawer");
    if (!modal || !current.modal) return;
    if (event.key === "Escape") { event.preventDefault(); closeModal(); return; }
    if (event.key !== "Tab") return;
    const controls = Array.from(modal.querySelectorAll('button, a[href], input, textarea, select, summary, [tabindex="0"]')).filter((node) => !node.matches(":disabled") && !node.closest("[hidden], [inert]") && node.getClientRects().length);
    const first = controls[0], last = controls[controls.length - 1], focused = root.ownerDocument?.activeElement;
    if (!first) { event.preventDefault(); modal.focus(); }
    else if (event.shiftKey && (focused === first || focused === modal || !modal.contains(focused))) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && (focused === last || focused === modal || !modal.contains(focused))) { event.preventDefault(); first.focus(); }
  }

  function onField(event) {
    const node = event.target?.closest?.("[data-library-field]");
    if (!node || !root?.contains(node) || !active() || context !== contextKey()) return;
    const key = node.dataset.libraryField, value = node.dataset.libraryValue;
    if (key === "select-question") {
      const row = current.questions.items.find((item) => questionKey(item) === value);
      if (row) {
        if (node.checked && !current.selected.has(value) && current.selected.size >= 100) { node.checked = false; toast("每次最多选择 100 个题目版本，请先导出或取消部分选择。"); return; }
        node.checked ? current.selected.set(value, row) : current.selected.delete(value);
      }
      if (event.type === "change") repaint();
      return;
    }
    if (key === "select-page") {
      const additions = current.questions.items.filter((row) => !current.selected.has(questionKey(row)));
      if (node.checked && current.selected.size + additions.length > 100) { node.checked = false; toast("选择当前页将超过 100 个版本，请逐项选择或先导出已有选择。"); return; }
      for (const row of current.questions.items) node.checked ? current.selected.set(questionKey(row), row) : current.selected.delete(questionKey(row));
      if (event.type === "change") repaint();
      return;
    }
    if (["material-lifecycle-filter", "material-usage-filter"].includes(key)) { current.materials[key === "material-lifecycle-filter" ? "lifecycle" : "usage_scope"] = node.value; if (event.type === "change") repaint(); return; }
    if (node.dataset.libraryScope === "filters") { current.filters[key] = node.value; return; }
    const modal = current.modal;
    if (!modal || modal.busy) return;
    const d = modal.data;
    if (["source_block_ids", "block_ids", "public_block_ids", "row_numbers", "knowledge_points"].includes(key)) {
      const item = key === "row_numbers" ? Number(value) : value;
      const values = new Set(list(d[key]));
      if (key === "source_block_ids" && node.checked && !values.has(item) && values.size >= 8) { node.checked = false; toast("最多选择 8 个来源块"); return; }
      node.checked ? values.add(item) : values.delete(item);
      d[key] = [...values];
      if (key === "block_ids") d.public_block_ids = list(d.public_block_ids).filter((id) => values.has(id));
      if (key === "source_block_ids") clearPreview(modal);
    } else if (key.startsWith("issue-")) {
      d.issues[value] ||= {};
      d.issues[value][key === "issue-resolution" ? "resolution" : "replacement_text"] = node.value;
    } else if (node.type === "file") { d[key] = node.files?.[0] || null; clearPreview(modal); }
    else {
      const previous = d[key];
      d[key] = node.type === "checkbox" ? node.checked : node.value;
      if (key === "type" && previous !== node.value) { d.answer = ""; }
    }
    if (!["source_search", "source_material_id"].includes(key)) modal.dirty = true;
    if (key === "source_material_id" && event.type === "change") return loadSource(node.value, modal);
    if (key === "replacement_material_id" && event.type === "change") return loadReplacement(node.value, modal);
    if (key === "intent") { d.external_processing_allowed = false; clearPreview(modal); }
    else if (key === "format") d.confirmed = false;
    if (event.type === "change" && (["type", "intent", "format", "source_block_ids", "block_ids", "public_block_ids", "row_numbers", "source_search"].includes(key) || node.type === "file")) repaint();
    if (key.startsWith("count_")) {
      const total = ["single_choice", "judge", "fill", "text"].reduce((sum, type) => sum + Number(d[`count_${type}`] || 0), 0);
      const display = root.querySelector("[data-library-count-total]"); if (display) display.textContent = `合计 ${total} 题`;
    }
  }

  function clearPreview(modal) {
    if (modal.type === "import" && modal.preview) { modal.preview = null; modal.data.row_numbers = []; modal.data.external_processing_allowed = false; }
  }

  async function onClick(event) {
    const node = event.target?.closest?.("[data-library-action]");
    if (!node || !root?.contains(node) || node.disabled) return;
    const action = node.dataset.libraryAction, value = node.dataset.libraryValue;
    if (context !== contextKey()) { syncContext(); repaint(); return; }
    if (!active()) return;
    if (current.modal?.busy && !["refresh-jobs"].includes(action)) return;
    if (!current.modal) openingFocus = focusSnapshot(node);
    if (action === "tab") { current.tab = value; repaint(); if (value === "jobs") await loadJobs(true); else if (value === "materials") await loadMaterials(); else await loadQuestions(); schedulePoll(); }
    else if (action === "prepare-goal") { if (state.classId) onPrepareGoal({}); }
    else if (action === "refresh-questions") await loadQuestions(true);
    else if (action === "refresh-materials") await loadMaterials(true);
    else if (action === "refresh-knowledge") await loadKnowledge(true);
    else if (action === "more-materials") await loadMaterials(true, epoch, true);
    else if (action === "refresh-jobs") { await loadJobs(true); schedulePoll(); }
    else if (action === "clear-filters") { current.filters = { q: "", question_type: "", status: "", knowledge_point: "", difficulty: "" }; current.applied = { ...current.filters }; current.questions.offset = 0; await loadQuestions(true); }
    else if (action === "page") { const [name, direction] = value.split(":"); const section = current[name]; section.offset = Math.max(0, section.offset + (direction === "next" ? section.limit : -section.limit)); if (name === "questions") await loadQuestions(true); else await loadJobs(true); }
    else if (action === "clear-selection") { current.selected.clear(); repaint(); }
    else if (action === "manual") openQuestionForm();
    else if (action === "generate") { openGeneration(); await Promise.all([loadMaterials(), loadKnowledge()]); }
    else if (action === "import") { newModal("import", { source_block_ids: [], source_material_id: "", source_search: "", row_numbers: [], external_processing_allowed: false }); await loadMaterials(); }
    else if (action === "import-jobs") { dismissModal(); current.tab = "jobs"; repaint(); await loadJobs(true); schedulePoll(); }
    else if (action === "import-again") { const modal = current.modal; modal.confirmation = null; modal.preview = null; modal.data.row_numbers = []; modal.data.external_processing_allowed = false; repaint(); }
    else if (action === "upload-material") openUpload(false);
    else if (action === "paste-material") openUpload(true);
    else if (action === "material-detail") await openMaterial(value);
    else if (action === "material-lifecycle") { const material = current.modal.doc.material; if (lifecycle(material) !== "superseded") newModal("lifecycle", { note: "", confirmed: false }, { material, targetLifecycle: lifecycle(material) === "active" ? "inactive" : "active" }); }
    else if (action === "material-replace") await openSupersede(current.modal.doc.material);
    else if (["upload-replacement", "paste-replacement"].includes(action)) openUpload(action === "paste-replacement", current.modal.doc.material);
    else if (action === "finish-replacement") { const doc = current.modal.doc, old = current.replacements.get(doc.material.id); if (old) await openSupersede(old, doc); }
    else if (action === "export-questions") { const rows = [...current.selected.values()]; if (rows.length) newModal("export", { format: "json", confirmed: false }, { rows }); }
    else if (action === "refresh-sources") {
      const modal = current.modal, token = epoch; modal.busy = true; modal.error = ""; repaint();
      try { await revalidateSources(modal, token, { localPreview: modal.type === "import" }); if (valid(token) && current.modal === modal) toast("已重新核验全部已选来源。"); }
      catch (error) { if (valid(token) && current.modal === modal) modal.error = errorText(error); }
      finally { if (valid(token) && current.modal === modal) { modal.busy = false; repaint(); } }
    }
    else if (["question-detail", "published-question"].includes(action)) { const at = value.lastIndexOf("@"); await openQuestion(value.slice(0, at), Number(value.slice(at + 1))); }
    else if (action === "close-modal") closeModal();
    else if (action === "discard-modal") closeModal(true);
    else if (action === "keep-editing") { current.modal.discardPrompt = false; repaint(); }
    else if (action === "remove-source") { const modal = current.modal; modal.data.source_block_ids = modal.data.source_block_ids.filter((id) => id !== value); modal.dirty = true; clearPreview(modal); repaint(); }
    else if (action === "detail-version") await openQuestion(current.modal.questionId, Number(value));
    else if (action === "retry-modal") { const modal = current.modal; if (modal.type === "material") await openMaterial(modal.materialId); else await openQuestion(modal.questionId, modal.version); }
    else if (action === "edit-question") { const modal = current.modal, row = modal.detail.question || modal.detail; openQuestionForm({ ...row, question_id: questionId(row) || modal.questionId, version: row.version || modal.version }); }
    else if (action === "prepare-from-question") { const q = asQuestion(current.modal.detail.question || current.modal.detail); dismissModal(); repaint(); onPrepareGoal({ knowledge_points: list(q.knowledge_points), question_types: ["single_choice", "judge", "fill", "text"].includes(q.type) ? { [q.type]: 1 } : undefined }); }
    else if (["bulk-pause", "bulk-retire", "detail-pause", "detail-retire"].includes(action)) {
      const detail = current.modal;
      const rows = action.startsWith("detail") ? [{ ...(detail.detail.question || detail.detail), question_id: detail.questionId, version: detail.version }] : [...current.selected.values()];
      if (rows.length) newModal("bulk", { confirmed: false }, { status: action.endsWith("retire") ? "retired" : "quarantined", rows, requestIds: new Map() });
    } else if (["cancel-job", "resume-job"].includes(action)) { const job = current.jobs.items.find((item) => jobId(item) === value); if (job) newModal("job", { confirmed: false }, { job, operation: action === "resume-job" ? "resume" : "cancel" }); }
  }

  async function onSubmit(event) {
    const form = event.target?.closest?.("[data-library-form]");
    if (!form || !root?.contains(form)) return;
    event.preventDefault();
    if (context !== contextKey() || !active()) { syncContext(); repaint(); return; }
    const kind = form.dataset.libraryForm;
    if (kind === "filters") { current.applied = Object.fromEntries(Object.entries(current.filters).map(([key, value]) => [key, String(value).trim()])); current.questions.offset = 0; await loadQuestions(true); return; }
    const modal = current.modal;
    if (!modal || modal.busy) return;
    modal.busy = true; modal.error = "";
    const token = epoch;
    repaint();
    try {
      if (kind === "question") await submitQuestion(modal, token);
      else if (kind === "generate") await submitGeneration(modal, token);
      else if (kind === "import-preview") await previewImport(modal, token);
      else if (kind === "import-confirm") await confirmImport(modal, token);
      else if (kind === "material-upload") await uploadMaterial(modal, token);
      else if (kind === "material-permissions") await savePermissions(modal, token);
      else if (kind === "material-confirm") await confirmMaterial(modal, token);
      else if (kind === "material-lifecycle") await submitMaterialLifecycle(modal, token);
      else if (kind === "material-supersede") await submitMaterialSupersede(modal, token);
      else if (kind === "question-export") await exportQuestions(modal, token);
      else if (kind === "resolve-issue") await resolveIssue(modal, form.dataset.libraryValue, token);
      else if (kind === "bulk") await submitBulk(modal, token);
      else if (kind === "job-action") await submitJobAction(modal, token);
    } catch (error) { if (valid(token) && current.modal === modal) modal.error = errorText(error); }
    finally { if (valid(token) && current.modal === modal) { modal.busy = false; repaint(); } }
  }

  function requireSources(d, { localPreview = false } = {}) {
    const ids = list(d.source_block_ids);
    if (!ids.length || ids.length > 8 || new Set(ids).size !== ids.length) throw new Error("请选择 1–8 个已确认且允许出题的来源块。");
    const issue = sourceIssue(d, { localPreview });
    if (issue) throw new Error(issue);
    return [...ids];
  }

  async function revalidateSources(modal, token, { localPreview = false } = {}) {
    const ids = list(modal.data.source_block_ids);
    if (!ids.length || ids.length > 8 || new Set(ids).size !== ids.length) throw new Error("请选择 1–8 个已确认且允许出题的来源块。");
    const fileIds = [...new Set(ids.map((id) => {
      const block = sourceBlock(id);
      if (!block?.material?.id) throw new Error(`来源 ${id} 的信息尚未读全，无法核验。请重新选择；已选来源仍保留。`);
      return block.material.id;
    }))];
    const results = await Promise.all(fileIds.map(async (id) => {
      try { return { id, doc: await api.call("GET", `/materials/${encodeURIComponent(id)}`) }; }
      catch (error) { return { id, error }; }
    }));
    if (!valid(token) || current.modal !== modal) return null;
    for (const result of results) {
      if (result.error || result.doc?.material?.id !== result.id) {
        const previous = current.sourceDocs.get(result.id);
        if (previous) current.sourceDocs.set(result.id, { ...previous, validationError: result.error ? errorText(result.error) : "材料编号不一致" });
        throw new Error(`无法重新核验来源“${materialName(current.materials.items.find((row) => row.id === result.id))}”：${result.error ? errorText(result.error) : "返回材料编号不一致"}。未提交任务，已选来源仍保留，请重读或明确移除。`);
      }
      rememberMaterial(result.doc);
    }
    return requireSources(modal.data, { localPreview });
  }
  function requireExternal(d) { if (d.external_processing_allowed !== true) throw new Error("请明确勾选允许外部模型处理后再提交审查。"); }
  function requireLicense(d) {
    if (d.rights_confirmed && ["unknown", "restricted"].includes(d.license_code)) throw new Error("请先选择可使用的许可，再确认材料使用权。");
    if (d.rights_confirmed && ["permission", "cc_by", "cc_by_sa"].includes(d.license_code) && !String(d.license_note || "").trim()) throw new Error("请填写授权或署名说明。");
  }

  function questionPayload(d) {
    const q = { type: d.type, stem: String(d.stem || "").trim(), options: [], answer: "", explanation: d.explanation || "", points: Number(d.points), rubric: [] };
    if (!q.stem || q.stem.length > 8000) throw new Error("请填写 1–8000 字的题干。");
    if (!Number.isFinite(q.points) || q.points <= 0 || q.points > 10000 || Math.abs(q.points * 100 - Math.round(q.points * 100)) > 1e-6) throw new Error("分值须大于 0，且最多保留两位小数。");
    if (["single_choice", "multi_choice"].includes(q.type)) {
      q.options = String(d.options || "").split(/\r?\n/).map((value) => value.trim()).filter(Boolean);
      if (q.options.length < 2 || q.options.length > 12 || new Set(q.options.map((value) => value.normalize("NFKC").toLocaleLowerCase().replace(/\s+/g, " "))).size !== q.options.length || q.options.some((value) => value.length > 2000)) throw new Error("请填写 2–12 个不重复选项，每个选项不超过 2000 字。");
      const answers = String(d.answer || "").trim().toUpperCase().split(/[，,、\s]+/).filter(Boolean);
      if (!answers.length || (q.type === "single_choice" && answers.length !== 1) || answers.some((value) => !/^[A-L]$/.test(value)) || new Set(answers).size !== answers.length) throw new Error("请用选项字母填写正确答案；多选用逗号分隔且不能重复。");
      const indexes = answers.map((value) => value.charCodeAt(0) - 65);
      if (indexes.some((index) => index >= q.options.length)) throw new Error("正确答案引用了不存在的选项。");
      q.answer = q.type === "single_choice" ? indexes[0] : indexes.sort((a, b) => a - b);
    } else if (q.type === "judge") {
      if (!["true", "false"].includes(d.answer)) throw new Error("请选择判断题的正确答案。");
      q.answer = d.answer === "true";
    } else {
      const answer = String(d.answer || "").trim();
      if (!answer) throw new Error("请填写参考答案。");
      q.answer = q.type === "fill" && answer.includes("\n") ? answer.split(/\r?\n/).map((value) => value.trim()).filter(Boolean) : answer;
    }
    for (const line of String(d.rubric || "").split(/\r?\n/).filter((value) => value.trim())) {
      const parts = line.split(/[|｜]/), score = Number(parts.pop()?.trim()), point = parts.join("|").trim();
      if (!point || !Number.isFinite(score) || score < 0 || Math.abs(score * 100 - Math.round(score * 100)) > 1e-6) throw new Error("评分细则请按“得分点 | 分值”逐行填写，分值最多两位小数。");
      q.rubric.push({ point, score_units: Math.round(score * 100) });
    }
    if (q.type === "text" && !q.rubric.length) throw new Error("作答题必须填写评分细则，请按“得分点 | 分值”逐行填写，合计须等于题目分值。");
    if (q.rubric.length && q.rubric.reduce((sum, item) => sum + item.score_units, 0) !== Math.round(q.points * 100)) throw new Error("评分细则的分值之和必须等于题目分值。");
    if (d.difficulty) q.difficulty = d.difficulty;
    if (["fill", "text"].includes(q.type)) q.answer_format = d.answer_format || "text";
    q.knowledge_points = confirmedKnowledge(d);
    return q;
  }

  async function submitQuestion(modal, token) {
    const d = modal.data;
    requireExternal(d);
    const question = questionPayload(d);
    const sources = await revalidateSources(modal, token);
    if (!sources || !valid(token) || current.modal !== modal) return;
    const payload = { question, source_block_ids: sources, intent: sourceIntent(d), external_processing_allowed: true, ...classBody() };
    const url = modal.type === "edit" ? path(`questions/${encodeURIComponent(questionId(modal.original))}/edit-jobs`) : path("question-candidates");
    if (modal.type === "edit") payload.expected_version = Number(modal.original.version);
    const result = await api.call("POST", url, retryPayload(modal, url, payload));
    if (valid(token) && current.modal === modal) await acceptedJob(result, token, "已提交候选题，等待独立审查。");
  }

  async function submitGeneration(modal, token) {
    const d = modal.data;
    requireExternal(d);
    const types = Object.fromEntries(["single_choice", "judge", "fill", "text"].map((type) => [type, Number(d[`count_${type}`] || 0)]));
    const total = Object.values(types).reduce((sum, count) => sum + count, 0);
    if (Object.values(types).some((count) => !Number.isInteger(count) || count < 0) || total < 1 || total > 10) throw new Error("题型数量须为非负整数，合计 1–10 题。");
    const points = confirmedKnowledge(d);
    const sources = await revalidateSources(modal, token);
    if (!sources || !valid(token) || current.modal !== modal) return;
    const payload = { source_block_ids: sources, intent: sourceIntent(d), count: total, question_types: types, difficulty: d.difficulty, knowledge_points: points, instruction: d.instruction || "", external_processing_allowed: true, ...classBody() };
    const url = path("question-jobs");
    const result = await api.call("POST", url, retryPayload(modal, url, payload));
    if (valid(token) && current.modal === modal) await acceptedJob(result, token, "备题任务已创建，请在任务页查看真实进度。");
  }

  async function acceptedJob(result, token, message) {
    const job = result?.job;
    dismissModal(); current.tab = "jobs"; current.jobs.offset = 0;
    if (job) current.jobs.items = [job, ...current.jobs.items.filter((item) => jobId(item) !== jobId(job))];
    toast(message); repaint();
    await loadJobs(true, token);
    if (valid(token)) { void loadQuestions(true, token); schedulePoll(); }
  }

  async function previewImport(modal, token) {
    const d = modal.data, file = d.import_file;
    if (!file || !/\.(csv|json|xlsx)$/i.test(file.name)) throw new Error("请选择 CSV、JSON 或固定模板 XLSX 题目文件。");
    if (!file.size || file.size > 2 * 1024 * 1024) throw new Error("题目文件不能为空，且不能超过 2 MiB；每次最多导入 100 道题。");
    const sources = await revalidateSources(modal, token, { localPreview: true });
    if (!sources || !valid(token) || current.modal !== modal) return;
    const form = new FormData(); form.append("file", file); form.append("source_block_ids", JSON.stringify(sources));
    if (state.classId) form.append("class_id", state.classId);
    const preview = await api.upload(path("question-imports/preview"), form);
    if (!valid(token) || current.modal !== modal) return;
    modal.preview = preview; modal.data.row_numbers = list(preview.rows || preview.items).filter(importRowUsable).map((row) => Number(row.row_number ?? row.row)); modal.data.external_processing_allowed = false;
  }

  async function confirmImport(modal, token) {
    requireExternal(modal.data);
    const sources = await revalidateSources(modal, token);
    if (!sources || !valid(token) || current.modal !== modal) return;
    const id = modal.preview?.import_id || modal.preview?.id;
    if (!id || !list(modal.data.row_numbers).length) throw new Error("请先预览并选择至少一行校验通过的题目。");
    const result = await api.call("POST", path(`question-imports/${encodeURIComponent(id)}/confirm`), { external_processing_allowed: true, row_numbers: [...modal.data.row_numbers] });
    if (!valid(token) || current.modal !== modal) return;
    if (!Array.isArray(result?.jobs) || !Array.isArray(result?.failures)) throw new Error("服务端未返回完整导入结果，请保留本次预览并重试确认以核对结果。");
    modal.confirmation = result; modal.dirty = false; modal.data.external_processing_allowed = false;
    toast(`已送审 ${result.accepted_count ?? result.jobs.length} 行，请核对导入结果。`);
    await loadJobs(true, token);
  }

  async function uploadMaterial(modal, token) {
    const d = modal.data;
    requireLicense(d);
    let file = d.material_file;
    if (modal.type === "paste") {
      if (!String(d.paste_text || "").trim()) throw new Error("请先粘贴课程原文。");
      if (!["txt", "csv"].includes(d.paste_format)) throw new Error("请选择原文格式。");
      const extension = d.paste_format, declared = String(d.source_filename || "").trim();
      const filename = declared && !/[\\/\x00-\x1f]/.test(declared) && declared.toLowerCase().endsWith(`.${extension}`) ? declared : `pasted-material.${extension}`;
      file = new File([d.paste_text], filename, { type: `${extension === "csv" ? "text/csv" : "text/plain"};charset=utf-8` });
    }
    if (!file?.size || file.size > 20 * 1024 * 1024) throw new Error("请选择非空材料文件，单个文件不能超过 20 MB。");
    if (!String(d.title || "").trim()) throw new Error("请填写材料名称。");
    const form = new FormData(); form.append("file", file); form.append("title", d.title.trim()); form.append("visibility", d.visibility);
    form.append("license_code", d.license_code); form.append("license_note", d.license_note || "");
    if (!["production", "evaluation"].includes(d.usage_scope)) throw new Error("请选择材料用途。");
    form.append("usage_scope", d.usage_scope);
    for (const key of ["source_filename", "source_locator", "source_note"]) {
      const value = String(d[key] || "").trim();
      if (value.length > (key === "source_filename" ? 240 : 2000)) throw new Error("来源信息超过长度限制，请缩短后再上传。");
      if (value) form.append(key, value);
    }
    for (const key of Object.keys(PERMISSIONS)) form.append(key, String(d[key] === true));
    if (d.scope === "class" && state.classId) form.append("class_id", state.classId);
    const doc = await api.upload(path("materials"), form);
    if (!valid(token) || current.modal !== modal) return;
    if (!doc?.material?.id) throw new Error("服务端未返回材料编号，请刷新材料列表核对上传结果。");
    if (modal.replacementOf) current.replacements.set(doc.material.id, modal.replacementOf);
    toast("材料已上传，请核对原文块后确认。");
    current.tab = "materials";
    await openMaterial(doc.material.id, doc);
    if (valid(token)) void loadMaterials(true, token);
  }

  async function reloadMaterialModal(modal, token, materialId) {
    const doc = await api.call("GET", `/materials/${encodeURIComponent(materialId)}`);
    if (!valid(token) || current.modal !== modal) return;
    if (doc?.material?.id !== materialId) throw new Error("材料编号未核对一致，请重新读取。");
    modal.doc = doc; modal.data = materialData(doc); modal.dirty = false; rememberMaterial(doc);
    void loadMaterials(true, token);
  }

  async function savePermissions(modal, token) {
    requireLicense(modal.data);
    const material = modal.doc.material, d = modal.data;
    await api.call("PATCH", `/materials/${encodeURIComponent(material.id)}`, { expected_revision: material.revision, title: d.title, license_code: d.license_code, license_note: d.license_note || "", ...Object.fromEntries(Object.keys(PERMISSIONS).map((key) => [key, d[key] === true])) });
    if (!valid(token) || current.modal !== modal) return;
    toast("材料权限已保存，请继续核对原文。"); await reloadMaterialModal(modal, token, material.id);
  }

  async function confirmMaterial(modal, token) {
    const d = modal.data, material = modal.doc.material;
    if (!d.originals_checked || !d.block_ids.length) throw new Error("请选择原文块并确认已对照原文件核实。");
    if (!material.rights_confirmed) throw new Error("请先核实并保存材料使用权限。");
    if (d.public_block_ids.length && !d.public_content_attested) throw new Error("公开正文前，请明确确认不含答案和个人身份信息。");
    await api.call("POST", `/materials/${encodeURIComponent(material.id)}/confirm`, { expected_revision: material.revision, block_ids: [...d.block_ids], public_block_ids: [...d.public_block_ids], public_content_attested: d.public_content_attested === true, note: d.note || "" });
    if (!valid(token) || current.modal !== modal) return;
    toast("所选原文块已确认。"); await reloadMaterialModal(modal, token, material.id);
  }

  async function resolveIssue(modal, id, token) {
    const d = modal.data.issues[id] || {}, material = modal.doc.material;
    if (!String(d.resolution || "").trim()) throw new Error("请填写疑点处理说明。");
    await api.call("POST", `/materials/${encodeURIComponent(material.id)}/issues/${encodeURIComponent(id)}/resolve`, { expected_revision: material.revision, resolution: d.resolution.trim(), ...(String(d.replacement_text || "").trim() ? { replacement_text: d.replacement_text.trim() } : {}) });
    if (!valid(token) || current.modal !== modal) return;
    toast("疑点处理已保存，请再次核对原文。"); await reloadMaterialModal(modal, token, material.id);
  }

  async function submitMaterialLifecycle(modal, token) {
    const material = modal.material;
    if (!modal.data.confirmed) throw new Error("请先确认材料使用状态变更。");
    if (lifecycle(material) === "superseded") throw new Error("已被替换的材料不能重新启用。");
    const url = `/materials/${encodeURIComponent(material.id)}/lifecycle`;
    await api.call("POST", url, retryPayload(modal, url, { expected_revision: material.revision, lifecycle_status: modal.targetLifecycle, ...(String(modal.data.note || "").trim() ? { note: String(modal.data.note).trim() } : {}) }));
    if (!valid(token) || current.modal !== modal) return;
    const doc = await api.call("GET", `/materials/${encodeURIComponent(material.id)}`);
    if (!valid(token) || current.modal !== modal) return;
    if (doc?.material?.id !== material.id || lifecycle(doc.material) !== modal.targetLifecycle) throw new Error("服务端尚未确认预期材料状态，请刷新核对修订。");
    rememberMaterial(doc); dismissModal(); toast(modal.targetLifecycle === "active" ? "材料已重新启用。" : "材料已停用；已有题目来源记录保留。");
    await loadMaterials(true, token); if (valid(token)) repaint();
  }

  async function submitMaterialSupersede(modal, token) {
    const old = modal.material, replacement = modal.replacementDoc?.material, data = modal.data;
    if (!data.confirmed) throw new Error("请先勾选确认替换材料。");
    if (!replacement || !modal.replacementDoc || modal.replacementLoading) throw new Error("请先读取并核对替代材料。");
    if (!["active", "inactive"].includes(lifecycle(old)) || lifecycle(replacement) !== "active" || replacement.status !== "confirmed"
        || !replacement.rights_confirmed || usageScope(replacement) !== usageScope(old) || replacement.visibility !== old.visibility
        || (replacement.class_id || null) !== (old.class_id || null)) throw new Error("替代材料必须启用、已确认、用途和范围相同且使用权已核实。");
    const url = `/materials/${encodeURIComponent(old.id)}/supersede`;
    await api.call("POST", url, retryPayload(modal, url, { expected_revision: old.revision, replacement_material_id: replacement.id, expected_replacement_revision: replacement.revision, ...(String(data.note || "").trim() ? { note: String(data.note).trim() } : {}) }));
    if (!valid(token) || current.modal !== modal) return;
    const [oldDoc, replacementDoc] = await Promise.all([old.id, replacement.id].map(async (id) => {
      const doc = await api.call("GET", `/materials/${encodeURIComponent(id)}`);
      if (doc?.material?.id !== id) throw new Error("材料编号未核对一致，请重新读取。");
      return doc;
    }));
    if (!valid(token) || current.modal !== modal) return;
    if (lifecycle(oldDoc.material) !== "superseded" || oldDoc.material.replacement_material_id !== replacement.id
        || lifecycle(replacementDoc.material) !== "active") throw new Error("服务端未确认完整替换关系，请刷新两份材料后核对；未显示替换成功。");
    rememberMaterial(oldDoc); rememberMaterial(replacementDoc);
    current.replacements.delete(replacement.id); dismissModal(); toast("材料替换已确认；旧材料历史与来源记录仍保留。");
    await loadMaterials(true, token); if (valid(token)) repaint();
  }

  async function exportQuestions(modal, token) {
    if (!modal.data.confirmed) throw new Error("请核对题目版本并确认下载含答案的题目包。");
    const selection = modal.rows.map((row) => ({ question_id: questionId(row), version: Number(row.version) }));
    const result = await api.questionLibraryExport(state.courseId, selection, modal.data.format, state.classId || undefined);
    if (!valid(token) || current.modal !== modal) return;
    if (!result?.blob || result.count !== selection.length) throw new Error("题目包未完整核对，未开始下载。");
    const url = URL.createObjectURL(result.blob);
    const link = root.ownerDocument.createElement("a");
    link.href = url; link.download = result.filename; link.hidden = true;
    root.ownerDocument.body.appendChild(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    modal.dirty = false; modal.data.confirmed = false;
    toast(`已下载 ${result.count} 个题目版本，请打开文件核对。`);
  }

  async function submitBulk(modal, token) {
    if (!modal.data.confirmed) throw new Error("请核对题目和版本后勾选确认。");
    const results = [], failed = [];
    for (const row of modal.rows) {
      if (!valid(token) || current.modal !== modal) return;
      const key = questionKey(row);
      if (!modal.requestIds.has(key)) modal.requestIds.set(key, requestId());
      try {
        await api.call("POST", `/questions/${encodeURIComponent(questionId(row))}/versions/${row.version}/status`, { status: modal.status, request_id: modal.requestIds.get(key) });
        if (!valid(token) || current.modal !== modal) return;
        current.selected.delete(key); results.push({ ok: true, label: `${asQuestion(row).stem || questionId(row)}（版本 ${row.version}）` });
      } catch (error) {
        if (!valid(token) || current.modal !== modal) return;
        failed.push(row); results.push({ ok: false, label: `${asQuestion(row).stem || questionId(row)}（版本 ${row.version}）`, error: errorText(error) });
        if ([401, 403].includes(error?.status)) { failed.push(...modal.rows.slice(modal.rows.indexOf(row) + 1)); break; }
      }
    }
    if (!valid(token) || current.modal !== modal) return;
    if (failed.length) { modal.results = results; modal.rows = failed; modal.data.confirmed = false; modal.error = "部分版本未完成，成功项已保留；请核对失败项后重试。"; }
    else { dismissModal(); toast(`已${modal.status === "retired" ? "退役" : "暂停使用"} ${results.length} 个题目版本。`); }
    await loadQuestions(true, token);
  }

  async function submitJobAction(modal, token) {
    if (!modal.data.confirmed) throw new Error("请先勾选确认此任务操作。");
    const id = queueId(modal.job);
    if (!id) throw new Error("此任务尚未提供可操作的调度编号，请刷新后重试。");
    await api.call("POST", `/jobs/${encodeURIComponent(id)}/${modal.operation}`, modal.operation === "resume" ? { confirm_paid: true } : undefined);
    if (!valid(token) || current.modal !== modal) return;
    dismissModal(); toast(modal.operation === "resume" ? "已请求恢复，请查看实际任务状态。" : "已请求取消，请查看实际任务状态。");
    await loadJobs(true, token); if (valid(token)) schedulePoll();
  }

  // renderApp belongs to the host. Keep it unused here so polling cannot reset unrelated forms.
  void renderApp;
  return { render: renderLibrary, bind, load, reset };
}
