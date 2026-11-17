/* Teacher insights integration. Private records stay in memory.
 * Host owns navigation and calls load/openSelection after selecting the class.
 * openSelection({goalId?, insightId?}) shares only reviewSelection.goalId.
 * leave() blocks an in-flight/uncertain command; reset() invalidates all receipts.
 * loggingOut disables interaction without changing command identity. Hooks are
 * notifications only and must not be used to replay writes or reload this module.
 */
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (letter) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[letter]));
const text = (value) => typeof value === "string" ? value : value == null ? "" : JSON.stringify(value, null, 2);
const id = (value) => typeof value === "string" && value.trim() ? value.trim() : "";
const object = (value) => Boolean(value && typeof value === "object" && !Array.isArray(value));
const positive = (value) => Number.isSafeInteger(value) && value > 0;
const nonnegative = (value) => Number.isSafeInteger(value) && value >= 0;
const hash = (value) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value);
const copy = (value) => JSON.parse(JSON.stringify(value));
const canonical = (value) => Array.isArray(value) ? value.map(canonical) : object(value) ? Object.fromEntries(Object.keys(value).sort().map((key) => [key, canonical(value[key])])) : value;
const equal = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
const enc = encodeURIComponent;
const need = (condition, message = "服务返回的记录格式不一致，请刷新核对。") => { if (!condition) throw new Error(message); };
const rows = (value) => { const result = Array.isArray(value) ? value : value?.items; need(Array.isArray(result), "服务未返回完整列表，请重新读取。"); return result; };
const uncertain = (error) => !Number.isFinite(Number(error?.status)) || Number(error?.status) === 0 || Number(error?.status) >= 500 || [408, 425].includes(Number(error?.status));
const dateText = (value) => typeof value === "number" && Number.isFinite(value) ? new Date(value * 1000).toLocaleString("zh-CN") : "未提供";
const localTime = (value) => { if (value == null) return ""; const date = new Date(value * 1000); return Number.isFinite(date.getTime()) ? new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16) : ""; };
const STATUSES = { pending: "等待生成", running: "生成中", completed: "已完成", failed: "生成失败", cancelled: "已取消", queued: "等待执行", leased: "等待执行", retry_wait: "等待重试", succeeded: "任务完成", awaiting_confirmation: "等待确认恢复", reconciling: "费用待核对" };
const MASTERY = { mastered: "达到目标", developing: "继续巩固", needs_practice: "需要练习", insufficient_evidence: "证据不足" };
const TYPES = { single_choice: "选择题", fill: "填空题", judge: "判断题", text: "简答题" };
const PAGE_SIZE = 100, HISTORY_SIZE = 10, STALE = Symbol("stale insight read");
const BASE = "/teaching-insights";
const DRAFTS = "/teaching-insights/next-round-drafts";
const fresh = (goalId = "") => ({ goalId, insightId: "", goals: [], members: [], classroom: null, reports: [], revisions: [], revision: null,
  insights: [], insight: null, job: null, historyPage: 0, draft: null, edit: null, dirty: false, preview: null, published: null,
  reading: false, loaded: false, suspended: false, revoked: false, needsRefresh: false, logoutObserved: false,
  error: "", notice: "", command: null, generationConfirmed: false, resumeConfirmed: false, publishConfirmed: false,
  bounded: null, boundedEdit: { future_round_count: "", interval_seconds: "", ends_at: "" }, boundedConfirmed: false,
  pool: [], poolOffset: 0, poolMore: true, poolLoaded: false, poolSelection: [] });

export function createTeacherInsights({ api, state, render: renderApp = () => {}, toast = () => {}, onChanged } = {}) {
  let current = fresh(id(state.reviewSelection?.goalId)), root = null, epoch = 0, sequence = 0, loadingPromise = null;
  const controllers = new Set(), listeners = new WeakSet(), bindings = new WeakMap();
  const actor = () => api?.state?.user;
  const contextKey = () => JSON.stringify([state.live, state.role, state.courseId || "", state.classId || "", state.transitionId ?? 0,
    state.teacherContextVersion ?? 0, actor()?.id || "", actor()?.role || "", actor()?.status || "", state.session?.id || state.sessionId || ""]);
  let context = contextKey();
  const session = () => state.live === true && state.role === "teacher" && actor()?.role === "teacher" && actor()?.status === "active"
    && Boolean(id(actor()?.id)) && (!state.screen || state.screen === "app");
  const active = () => session() && !state.loggingOut && !current.suspended && !current.revoked;
  const valid = (token, data) => token === epoch && data === current && context === contextKey() && session();
  const manage = () => active() && current.classroom?.can_manage === true
    && !(state.teacherCourses || []).some((entry) => (entry.classes || []).some((item) => item.id === state.classId && item.can_manage === false));
  const locked = () => Boolean(current.command || current.reading || current.needsRefresh);
  const paint = () => { if (active()) renderApp(); };
  function stopReads() {
    sequence += 1; controllers.forEach((controller) => controller.abort()); controllers.clear();
    loadingPromise = null; current.reading = false;
  }
  function clearRoot() { if (root?.isConnected) root.innerHTML = ""; root = null; }
  function syncContext() {
    if (context !== contextKey()) {
      stopReads(); clearRoot(); epoch += 1; context = contextKey(); current = fresh();
    }
    if (state.loggingOut && !current.logoutObserved) {
      current.logoutObserved = true; current.needsRefresh = true; stopReads();
      current.generationConfirmed = false; current.resumeConfirmed = false; current.publishConfirmed = false;
    } else if (!state.loggingOut) current.logoutObserved = false;
  }
  function reset() { stopReads(); clearRoot(); epoch += 1; context = contextKey(); current = fresh(); }
  function leave() {
    syncContext();
    if (current.command) {
      current.error = current.command.inFlight ? "操作正在提交，请等待结果后再离开。" : "上次操作结果尚未确认，请用原请求显式重试后再离开。";
      if (active()) toast(current.error); paint(); return false;
    }
    const goalId = current.goalId, dirty = current.dirty;
    stopReads(); clearRoot(); epoch += 1; current = fresh(goalId); current.suspended = true;
    if (dirty && session() && !state.loggingOut) toast("已清除未保存的下一轮输入；已保存草稿仍保留在服务端。");
    return true;
  }
  function revoke() {
    stopReads(); const message = "记录不存在或已不可访问。已清除当前班级的私有缓存，请重新核验账号与班级权限。";
    current = fresh(); current.revoked = true; current.error = message; epoch += 1;
    if (session() && !state.loggingOut && context === contextKey()) renderApp();
  }
  function beginRead() {
    stopReads(); current.reading = true;
    const token = epoch, data = current, serial = sequence;
    return { token, data, serial, live: () => valid(token, data) && serial === sequence && active() };
  }
  async function read(request, operation) {
    if (!request.live()) throw STALE;
    const controller = new AbortController(); controllers.add(controller);
    try { const result = await operation({ signal: controller.signal }); if (!request.live()) throw STALE; return result; }
    finally { controllers.delete(controller); }
  }
  const get = (path, request) => read(request, (options) => api.call("GET", path, undefined, options));
  function failRead(error, request) {
    if (!request.live() || error === STALE || error?.name === "AbortError") return;
    if ([401, 403, 404].includes(Number(error?.status))) { revoke(); return; }
    current.error = error?.message || "读取失败，请刷新核对。"; current.needsRefresh = true;
  }
  function scope(record, goalId = "") {
    need(object(record) && record.class_id === state.classId && record.course_id === state.courseId, "服务记录不属于当前课程和班级，已停止显示。");
    if (goalId) need(record.origin_goal_id === goalId, "服务记录与所选学习目标不一致。");
  }
  function validateRevision(revision, reportId, revisionId, goalId) {
    const wanted = revision?.snapshot?.scope;
    need(id(revision?.id) && revision.id === revisionId && revision.report_id === reportId && positive(revision.revision_number)
      && nonnegative(revision.source_revision) && hash(revision.snapshot_sha256) && revision.snapshot?.schema_version === "report.snapshot.v1"
      && wanted?.kind === "goal" && wanted.goal_id === goalId && wanted.class_id === state.classId && wanted.course_id === state.courseId
      && !wanted.student_id, "冻结报告的范围、修订或数据格式不一致。");
    return revision;
  }
  function validateInsight(value, { goalId = "", insightId = "", command = null } = {}) {
    scope(value, goalId);
    need(id(value.id) && (!insightId || value.id === insightId) && id(value.origin_goal_id) && id(value.job_id)
      && id(value.report_id) && id(value.source_report_revision) && nonnegative(value.source_revision)
      && hash(value.snapshot_sha256) && hash(value.input_hash) && typeof value.stale === "boolean"
      && ["pending", "running", "completed", "failed", "cancelled"].includes(value.status), "总结缺少可核验的来源、状态或旧数据标记。");
    if (command) need(value.report_id === command.payload.report_id && value.source_report_revision === command.payload.source_report_revision
      && value.snapshot_sha256 === command.sourceHash && value.source_revision === command.sourceRevision, "生成回执与原冻结报告不一致。");
    const facts = value.facts, codes = value.student_codes, index = value.evidence_index;
    need(object(facts) && nonnegative(facts.student_count) && Array.isArray(facts.students) && facts.student_count === facts.students.length
      && object(facts.status_counts) && object(codes) && object(index), "总结未返回完整的程序计算事实。");
    const unique = new Set();
    for (const fact of facts.students) {
      need(object(fact) && id(fact.student_code) && !unique.has(fact.student_code) && id(codes[fact.student_code]) && MASTERY[fact.status]
        && (fact.percent === null || typeof fact.percent === "number" && Number.isFinite(fact.percent) && fact.percent >= 0 && fact.percent <= 100)
        && nonnegative(fact.independent_families) && nonnegative(fact.observed_families) && Array.isArray(fact.evidence_refs)
        && fact.evidence_refs.every((ref) => id(ref) && Object.hasOwn(index, ref)), "程序事实或证据引用格式不完整。");
      need(fact.status !== "insufficient_evidence" || fact.percent === null, "证据不足不得显示为零分或确定掌握度。");
      unique.add(fact.student_code);
    }
    for (const status of Object.keys(MASTERY)) need(facts.status_counts[status] === facts.students.filter((item) => item.status === status).length, "事实状态计数不一致。");
    if (value.status === "completed") {
      const result = value.result;
      need(result?.schema_version === "teaching.insight.result.v1" && result.origin === "ai" && result.interpretation_mode === "validated_selection"
        && result.input_hash === value.input_hash && typeof result.summary === "string" && Array.isArray(result.findings)
        && Array.isArray(result.limitations) && result.limitations.every((item) => typeof item === "string")
        && Array.isArray(result.evidence_refs) && result.evidence_refs.length > 0 && result.evidence_refs.every((ref) => Object.hasOwn(index, ref))
        && result.findings.length === facts.students.length, "AI 总结结果的数据格式或冻结证据绑定不匹配，已停止显示解释。");
      const found = new Set();
      for (const finding of result.findings) {
        const fact = facts.students.find((item) => item.student_code === finding?.student_code);
        need(fact && !found.has(finding.student_code) && typeof finding.explanation === "string" && finding.status === fact.status
          && finding.percent === fact.percent && finding.independent_families === fact.independent_families
          && Array.isArray(finding.evidence_refs) && finding.evidence_refs.length > 0
          && finding.evidence_refs.every((ref) => fact.evidence_refs.includes(ref)), "AI 解释与程序事实或证据引用不一致。");
        found.add(finding.student_code);
      }
    }
    if (value.next_round_drafts != null) {
      const drafts = value.next_round_drafts, seen = new Set();
      need(Array.isArray(drafts), "总结的已存草稿引用列表格式不完整。");
      for (const draft of drafts) {
        need(id(draft?.id) && !seen.has(draft.id) && draft.origin_goal_id === value.origin_goal_id
          && draft.source_report_revision === value.source_report_revision && draft.goal_version === value.goal_version
          && positive(draft.revision) && positive(draft.next_round) && draft.next_round > 1 && ["draft", "published"].includes(draft.status),
        "已存草稿引用与当前总结来源不一致。");
        seen.add(draft.id);
      }
    }
    return value;
  }
  function validateJob(job, insight) {
    need(id(job?.job_id) && job.job_id === insight.job_id && id(job.status)
      && (job.resource_id == null || job.resource_id === insight.id)
      && typeof job.cancel_requested === "boolean" && typeof job.cost_pending === "boolean" && typeof job.reconciliation_required === "boolean",
    "任务回执与所选总结不一致。");
    return job;
  }
  function selectGoal(goalId) { current.goalId = goalId; state.reviewSelection = { ...(state.reviewSelection || {}), goalId }; }
  async function catalog(request) {
    const goals = [], seen = new Set(); let offset = 0;
    while (true) {
      const page = rows(await read(request, (options) => api.goals(state.classId, { ...options, offset, limit: PAGE_SIZE })));
      for (const goal of page) {
        need(id(goal?.id) && !seen.has(goal.id) && (!goal.class_id || goal.class_id === state.classId)
          && (!goal.course_id || goal.course_id === state.courseId), "历史目标分页没有前进或范围不一致。");
        seen.add(goal.id); goals.push(goal);
      }
      offset += page.length; if (page.length < PAGE_SIZE) break;
    }
    return goals;
  }
  async function readJob(insight, request) {
    const job = await read(request, (options) => typeof api.getJob === "function" ? api.getJob(insight.job_id, options)
      : api.call("GET", `/jobs/${enc(insight.job_id)}`, undefined, options));
    return validateJob(job, insight);
  }
  async function readRevision(reportId, revisionId, request) {
    current.revision = null; current.generationConfirmed = false;
    const revision = await read(request, (options) => api.reportRevision(reportId, revisionId, options));
    current.revision = validateRevision(revision, reportId, revisionId, current.goalId);
  }
  async function readInsight(insightId, request) {
    current.insight = null; current.job = null; current.resumeConfirmed = false;
    const result = validateInsight(await get(`${BASE}/${enc(insightId)}`, request), { goalId: current.goalId, insightId });
    current.insight = result; current.insightId = result.id;
    current.job = await readJob(result, request);
  }
  async function load({ force = false, selection = null } = {}) {
    syncContext(); current.suspended = false;
    if (!active() || current.command) return false;
    if (loadingPromise && !force) return loadingPromise;
    if (current.loaded && !current.needsRefresh && !force) return true;
    const selectedGoal = selection ? id(selection.goalId) : current.goalId;
    const selectedInsight = selection ? id(selection.insightId) : current.insightId;
    const savedRevision = current.revision?.id || "", savedDraftId = selection ? id(selection.draftId) : current.draft?.id || "";
    const request = beginRead(), data = current;
    data.error = ""; data.notice = ""; data.insight = null; data.job = null; data.revision = null; data.revisions = [];
    data.reports = []; data.insights = []; data.draft = null; data.edit = null; data.dirty = false; data.preview = null; data.published = null;
    data.generationConfirmed = false; data.resumeConfirmed = false; data.publishConfirmed = false;
    data.bounded = null; data.pool = []; data.poolOffset = 0; data.poolMore = true; data.poolLoaded = false; data.poolSelection = []; data.boundedConfirmed = false;
    paint();
    const work = async () => {
      try {
        need(id(state.classId) && id(state.courseId), "请先选择课程和班级。");
        const [access, goals, members] = await Promise.all([
          read(request, (options) => api.teacherClasses(options)), catalog(request), read(request, (options) => api.classMembers(state.classId, options)),
        ]);
        if (!request.live()) return false;
        data.classroom = rows(access).find((entry) => entry.course?.id === state.courseId)?.classes?.find((item) => item.id === state.classId) || null;
        need(data.classroom && typeof data.classroom.can_manage === "boolean", "未读到当前班级权限，请重新核验。");
        data.goals = goals; data.members = rows(members);
        need(data.members.every((item) => id(item?.user_id)), "班级成员列表格式不完整。");
        let opening = null;
        if (selectedInsight) opening = validateInsight(await get(`${BASE}/${enc(selectedInsight)}`, request), { goalId: selectedGoal, insightId: selectedInsight });
        const goalId = opening?.origin_goal_id || selectedGoal || goals[0]?.id || "";
        need(!goalId || goals.some((item) => item.id === goalId), "指定历史目标不存在或不属于当前班级。");
        selectGoal(goalId); data.insightId = ""; data.historyPage = 0;
        if (goalId) {
          const [reports, history] = await Promise.all([
            read(request, (options) => api.reportList(options)),
            get(`${BASE}?class_id=${enc(state.classId)}&goal_id=${enc(goalId)}&limit=100`, request),
          ]);
          data.reports = rows(reports).filter((report) => report.scope?.kind === "goal" && report.scope.goal_id === goalId
            && report.scope.class_id === state.classId && report.scope.course_id === state.courseId && !report.scope.student_id);
          const revisions = await Promise.all(data.reports.map(async (report) => {
            const list = rows(await read(request, (options) => api.reportRevisions(report.id, options)));
            need(list.every((entry) => id(entry.id) && entry.report_id === report.id && positive(entry.revision_number)), "报告历史修订与报告不一致。");
            return list;
          }));
          if (!request.live()) return false;
          data.revisions = revisions.flat();
          data.insights = rows(history).map((item) => validateInsight(item, { goalId }));
          const chosen = data.revisions.find((item) => item.id === savedRevision) || data.revisions.find((item) => data.reports.some((report) => report.current_revision_id === item.id)) || data.revisions[0];
          if (chosen) await readRevision(chosen.report_id, chosen.id, request);
          if (opening) { data.insight = opening; data.insightId = opening.id; data.job = await readJob(opening, request); }
          else if (data.insights[0]) await readInsight(data.insights[0].id, request);
          const draftId = savedDraftId || id(data.insight?.next_round_drafts?.[0]?.id);
          if (draftId && data.insight) await readDraft(draftId, request);
        }
        data.loaded = true; data.needsRefresh = false; return true;
      } catch (error) { failRead(error, request); return false; }
      finally { if (request.live()) { data.reading = false; loadingPromise = null; paint(); } }
    };
    loadingPromise = work(); return loadingPromise;
  }
  async function openSelection(selection = {}) {
    syncContext(); if (!session() || state.loggingOut || current.command || current.revoked) return false;
    current.suspended = false;
    if (id(selection.draftId) && !id(selection.insightId)) return false;
    return load({ force: true, selection: { goalId: id(selection.goalId) || (id(selection.insightId) ? "" : current.goalId), insightId: id(selection.insightId), draftId: id(selection.draftId) } });
  }
  async function openRevision(revisionId) {
    syncContext(); if (!active() || current.command) return false;
    const item = current.revisions.find((revision) => revision.id === revisionId); if (!item) return false;
    const request = beginRead(); current.error = ""; paint();
    try { await readRevision(item.report_id, item.id, request); return request.live(); }
    catch (error) { failRead(error, request); return false; }
    finally { if (request.live()) { current.reading = false; paint(); } }
  }
  async function openDraft(draftId) {
    syncContext(); if (!active() || current.command || !current.insight?.next_round_drafts?.some((item) => item.id === draftId)) return false;
    const request = beginRead(); current.error = ""; current.draft = null; current.edit = null; current.dirty = false;
    current.preview = null; current.published = null; current.publishConfirmed = false; paint();
    try { await readDraft(draftId, request); return request.live(); }
    catch (error) { failRead(error, request); return false; }
    finally { if (request.live()) { current.reading = false; paint(); } }
  }
  async function refreshInsight() {
    syncContext(); if (!active() || locked() || !current.insightId) return false;
    const request = beginRead(); current.error = ""; paint();
    try { await readInsight(current.insightId, request); return request.live(); }
    catch (error) { failRead(error, request); return false; }
    finally { if (request.live()) { current.reading = false; paint(); } }
  }
  function notify(kind, value) {
    if (!active() || typeof onChanged !== "function") return;
    try { Promise.resolve(onChanged({ kind, goalId: current.goalId, insightId: current.insightId, draftId: current.draft?.id || "", ...value })).catch(() => {}); }
    catch { /* Host notification cannot undo a confirmed receipt. */ }
  }
  function command(kind, method, path, payload, extra = {}) {
    stopReads(); current.error = ""; current.notice = "";
    current.command = { kind, method, path, payload: copy(payload), inFlight: false, ...extra };
    return send(current.command);
  }
  async function invoke(command) {
    if (command.kind === "cancel") {
      if (typeof api.cancelJob === "function") return api.cancelJob(command.jobId);
      if (typeof api.cancelCompanionJob === "function") return api.cancelCompanionJob(command.jobId);
    }
    if (command.kind === "resume") {
      if (typeof api.resumeJob === "function") return api.resumeJob(command.jobId, { confirm_paid: true });
      if (typeof api.resumeCompanionJob === "function") return api.resumeCompanionJob(command.jobId);
    }
    return api.call(command.method, command.path, command.payload);
  }
  async function send(pending) {
    syncContext(); const token = epoch, data = current;
    if (!active() || !manage() || data.command !== pending || pending.inFlight) return false;
    pending.inFlight = true; data.error = ""; paint();
    try {
      const result = await invoke(pending);
      if (!valid(token, data) || data.command !== pending) return false;
      accept(pending, result);
      data.command = null; data.notice = ({ generate: "总结请求已保存。刷新状态只读取原任务，不会再次生成。", cancel: "已收到取消请求。请刷新状态和费用，已产生的费用仍需核对。", resume: "恢复请求已确认。请刷新原任务状态。", "draft-create": "下一轮草稿已保存，尚未发布。", "draft-save": "草稿已按所读修订保存，请重新预览。", publish: "已发布到原学习目标的下一轮。" })[pending.kind] || "操作已确认。";
      notify(pending.kind, pending.kind === "publish" ? { publication: copy(result) } : {}); paint(); return true;
    } catch (error) {
      if (!valid(token, data) || data.command !== pending) return false;
      if ([401, 403, 404].includes(Number(error?.status))) { revoke(); return false; }
      if (uncertain(error)) {
        pending.inFlight = false; data.error = "操作结果尚未确认。原请求和内容已保留，请显式核对或重试。";
      } else {
        data.command = null; data.error = error?.message || "操作未完成，请核对后重试。";
        if (Number(error?.status) === 409) { data.needsRefresh = true; data.preview = null; data.publishConfirmed = false; data.error += " 请刷新当前修订并重新核对；不会自动创建新目标。"; }
      }
      paint(); return false;
    }
  }
  function accept(pending, result) {
    if (pending.kind === "generate") {
      const insight = validateInsight(result, { goalId: current.goalId, command: pending });
      current.insight = insight; current.insightId = insight.id; current.job = null;
      current.insights = [insight, ...current.insights.filter((item) => item.id !== insight.id)]; current.historyPage = 0;
      current.draft = null; current.edit = null; current.preview = null; current.published = null; current.dirty = false; current.generationConfirmed = false;
    } else if (["cancel", "resume"].includes(pending.kind)) {
      current.job = validateJob(result, current.insight); current.resumeConfirmed = false;
      // Domain status is read independently; no locally fabricated result state.
    } else acceptDraft(pending, result);
  }
  function generate() {
    syncContext(); if (!manage() || locked() || !current.revision || !current.generationConfirmed) return false;
    const revision = current.revision;
    return command("generate", "POST", BASE, { report_id: revision.report_id, source_report_revision: revision.id, request_id: `insight-${globalThis.crypto.randomUUID()}` },
      { sourceHash: revision.snapshot_sha256, sourceRevision: revision.source_revision });
  }
  const canCancel = () => current.job && !current.job.cancel_requested && !["succeeded", "cancelled"].includes(current.job.status);
  const canResume = () => current.job && current.insight?.status !== "cancelled" && current.insight?.error?.code !== "invalid_model_output"
    && !current.job.cancel_requested && !current.job.cost_pending && !current.job.reconciliation_required
    && ["failed", "awaiting_confirmation"].includes(current.job.status);
  function jobAction(kind) {
    syncContext(); if (!manage() || locked() || !current.insight || (kind === "cancel" ? !canCancel() : !canResume() || !current.resumeConfirmed)) return false;
    const jobId = current.insight.job_id;
    return command(kind, "POST", `/jobs/${enc(jobId)}/${kind}`, kind === "resume" ? { confirm_paid: true } : null, { jobId });
  }
  async function retry() {
    syncContext(); const pending = current.command;
    if (!active() || !pending || pending.inFlight) return false;
    if (["cancel", "resume"].includes(pending.kind)) return reconcileCommand(pending);
    return send(pending);
  }
  async function reconcileCommand(pending) {
    const request = beginRead(); pending.inFlight = true; current.error = ""; paint();
    try {
      const job = await readJob(current.insight, request); current.job = job;
      // Job endpoints have no request ID. Never blindly replay a paid resume.
      current.command = null; current.resumeConfirmed = false;
      current.notice = pending.kind === "resume" && ["failed", "awaiting_confirmation"].includes(job.status)
        ? "已核对原任务状态。若仍需恢复，请重新阅读费用提示并明确确认。"
        : "已核对原任务状态与费用；没有自动重发任务操作。";
      return true;
    } catch (error) { failRead(error, request); return false; }
    finally {
      if (valid(request.token, request.data)) pending.inFlight = false;
      if (request.live()) { current.reading = false; paint(); }
    }
  }
  function validateConfig(config) {
    need(object(config) && config.course_id === state.courseId && config.class_id === state.classId
      && id(config.title) && Array.isArray(config.student_ids) && config.student_ids.length > 0 && config.student_ids.length <= 200
      && config.student_ids.every((item) => id(item)) && new Set(config.student_ids).size === config.student_ids.length,
    "请选择冻结报告中的目标学生，并保留原课程与班级。");
    need(positive(config.question_count) && config.question_count <= 100 && object(config.question_types)
      && Object.keys(TYPES).every((kind) => nonnegative(config.question_types[kind]) && config.question_types[kind] <= 100)
      && Object.keys(TYPES).reduce((total, kind) => total + config.question_types[kind], 0) === config.question_count,
    "每人题量须为 1 至 100 道，且等于四类题型数量之和。");
    need(["light", "balanced", "heavy"].includes(config.common_level) && ["any", "easy", "medium", "hard"].includes(config.difficulty)
      && Array.isArray(config.knowledge_points) && config.knowledge_points.every((item) => id(item)), "草稿题目筛选配置不完整。");
    for (const field of ["deadline_at", "scheduled_at"]) need(config[field] === null || typeof config[field] === "number" && Number.isFinite(config[field]) && config[field] > 0, "请输入有效的开放与截止时间。");
    need(config.scheduled_at === null || config.deadline_at === null || config.scheduled_at < config.deadline_at, "开放时间必须早于截止时间。");
    need(["allow_late", "allow_retries", "allow_retry_after_deadline"].every((field) => typeof config[field] === "boolean")
      && (!config.allow_retry_after_deadline || config.allow_retries), "截止后允许重做须同时允许重做。");
    return config;
  }
  function validateDraft(draft, draftId = "") {
    scope(draft, current.goalId);
    need(id(draft.id) && (!draftId || draft.id === draftId) && draft.insight_id === current.insight?.id
      && draft.source_report_revision === current.insight.source_report_revision && draft.goal_version === current.insight.goal_version
      && positive(draft.revision) && positive(draft.next_round) && draft.next_round > 1 && ["draft", "published"].includes(draft.status)
      && object(draft.paper_overrides) && object(draft.basis?.students), "下一轮草稿的总结、目标或修订绑定不一致。");
    validateConfig(draft.config);
    const students = new Set(Object.values(current.insight.student_codes));
    need(draft.config.student_ids.every((studentId) => students.has(studentId) && Object.hasOwn(draft.basis.students, studentId)), "草稿包含冻结报告范围以外的学生。");
    return draft;
  }
  function validatePublication(value, draft) {
    need(value?.draft_id === draft.id && value.insight_id === draft.insight_id && value.origin_goal_id === draft.origin_goal_id
      && value.goal_id === draft.origin_goal_id && value.round_no === draft.next_round && id(value.round_id)
      && value.source_report_revision === draft.source_report_revision, "发布回执未绑定原目标与预期下一轮，请核对原请求。");
    return value;
  }
  function useDraft(draft) {
    current.draft = draft; current.edit = copy(draft.config); current.dirty = false; current.preview = null; current.publishConfirmed = false;
    current.published = draft.status === "published" && draft.published_result ? validatePublication(draft.published_result, draft) : null;
    current.bounded = draft.bounded_plan || null; current.boundedConfirmed = false;
    current.boundedEdit = { future_round_count: "", interval_seconds: "", ends_at: "" };
    current.pool = []; current.poolOffset = 0; current.poolMore = true; current.poolLoaded = false; current.poolSelection = [];
  }
  async function readDraft(draftId, request) { useDraft(validateDraft(await get(`${DRAFTS}/${enc(draftId)}`, request), draftId)); }
  function acceptDraft(pending, result) {
    if (["draft-create", "draft-save"].includes(pending.kind)) {
      const draft = validateDraft(result, pending.draftId || "");
      if (pending.kind === "draft-save") need(draft.revision === pending.payload.expected_revision + 1 && draft.status === "draft"
        && equal(draft.config, pending.payload.config) && equal(draft.paper_overrides, pending.payload.paper_overrides), "保存回执与原草稿配置或预期修订不一致。");
      useDraft(draft);
    } else if (pending.kind === "publish") {
      current.published = validatePublication(result, current.draft); current.preview = null; current.publishConfirmed = false;
    } else if (pending.kind === "bounded") {
      need(result?.source_draft_id === current.draft.id && result.goal_id === current.goalId && positive(result.goal_version)
        && result.status === "active" && result.future_round_count === pending.payload.future_round_count
        && nonnegative(result.remaining_rounds) && result.interval_seconds === pending.payload.interval_seconds
        && result.ends_at === pending.payload.ends_at && equal(result.question_pool, pending.payload.question_pool), "持续计划回执与原确认内容不一致。");
      current.bounded = result; current.boundedConfirmed = false;
    }
  }
  function validatePreview(preview, draft) {
    need(preview?.draft_id === draft.id && preview.draft_revision === draft.revision && preview.origin_goal_id === draft.origin_goal_id
      && preview.insight_id === draft.insight_id && preview.next_round === draft.next_round && equal(preview.config, draft.config)
      && typeof preview.ready === "boolean" && preview.readiness === (preview.ready ? "ready" : "blocked")
      && [preview.errors, preview.deficits, preview.warnings, preview.students, preview.questions, preview.common_questions].every(Array.isArray),
    "预览未绑定当前草稿修订与原目标，已禁止发布。");
    const students = new Set();
    for (const student of preview.students) {
      need(draft.config.student_ids.includes(student.student_id) && !students.has(student.student_id) && Array.isArray(student.questions), "预览题卷学生与已保存草稿不一致。");
      students.add(student.student_id);
      const questions = new Set();
      for (const question of student.questions) {
        need(id(question.question_id) && positive(question.version) && !questions.has(question.question_id) && TYPES[question.type]
          && typeof question.stem === "string" && Array.isArray(question.options) && Array.isArray(question.source_anchors)
          && typeof question.points === "number" && Number.isFinite(question.points), "预览未返回完整的冻结题目、选项与来源。");
        questions.add(question.question_id);
      }
      if (preview.ready) {
        need(student.questions.length === draft.config.question_count, "预览题量与草稿不一致。");
        for (const kind of Object.keys(TYPES)) need(student.questions.filter((question) => question.type === kind).length === draft.config.question_types[kind], "预览题型与草稿不一致。");
      }
    }
    if (preview.ready) need(students.size === draft.config.student_ids.length && preview.errors.length === 0 && preview.deficits.length === 0
      && id(preview.preview_token) && typeof preview.preview_expires_at === "number" && Number.isFinite(preview.preview_expires_at), "可发布预览缺少完整题卷、有效凭证或仍有阻断项。");
    return preview;
  }
  const draftEditable = () => manage() && !locked() && current.draft?.status === "draft" && !current.published;
  const previewReady = () => current.preview?.ready === true && !current.dirty && current.preview.draft_id === current.draft?.id
    && current.preview.draft_revision === current.draft?.revision && current.preview.preview_expires_at * 1000 > Date.now();
  async function previewDraft() {
    if (!draftEditable() || current.dirty) return false;
    const draft = current.draft, request = beginRead(); current.preview = null; current.publishConfirmed = false; current.error = ""; paint();
    try {
      const preview = await read(request, (options) => api.call("POST", `${DRAFTS}/${enc(draft.id)}/preview`, { expected_revision: draft.revision }, options));
      if (!request.live() || current.draft !== draft || current.dirty) return false;
      current.preview = validatePreview(preview, draft); return true;
    } catch (error) { failRead(error, request); return false; }
    finally { if (request.live()) { current.reading = false; paint(); } }
  }
  function draftAction(action) {
    syncContext();
    if (action === "draft-create") {
      if (!manage() || locked() || current.insight?.status !== "completed" || current.draft) return false;
      return command(action, "POST", `${BASE}/${enc(current.insight.id)}/next-round-drafts`, { request_id: `next-${globalThis.crypto.randomUUID()}` });
    }
    if (["bounded-load", "bounded-more"].includes(action)) return loadPool(action === "bounded-more");
    if (action === "bounded") return createBounded();
    if (!draftEditable()) return false;
    if (action === "draft-save") {
      try {
        validateConfig(current.edit);
        need(current.edit.student_ids.every((studentId) => Object.hasOwn(current.draft.basis.students, studentId)), "只能选择冻结报告中的学生。");
      } catch (error) { current.error = error.message; paint(); return false; }
      return command(action, "PUT", `${DRAFTS}/${enc(current.draft.id)}`, { request_id: `next-save-${globalThis.crypto.randomUUID()}`,
        expected_revision: current.draft.revision, config: copy(current.edit), paper_overrides: copy(current.draft.paper_overrides) }, { draftId: current.draft.id });
    }
    if (action === "draft-preview") return previewDraft();
    if (action === "publish") {
      if (!previewReady() || !current.publishConfirmed) return false;
      return command(action, "POST", `${DRAFTS}/${enc(current.draft.id)}/publish`, { request_id: `next-publish-${globalThis.crypto.randomUUID()}`,
        expected_revision: current.draft.revision, preview_token: current.preview.preview_token, confirm: true });
    }
    return false;
  }
  const poolKey = (question) => `${question.question_id}@${question.version}`;
  const boundedEditable = () => manage() && !locked() && (current.draft?.status === "published" || current.published) && !current.bounded;
  async function loadPool(more = false) {
    if (!boundedEditable() || more && !current.poolMore) return false;
    const draftId = current.draft.id, request = beginRead(); current.error = ""; current.boundedConfirmed = false; paint();
    try {
      // Publish receipts do not carry the current goal CAS token: fetch the draft.
      if (!more) {
        const draft = validateDraft(await get(`${DRAFTS}/${enc(draftId)}`, request), draftId);
        need(draft.status === "published" && positive(draft.current_goal_version), "请先刷新已发布草稿，核对原目标当前版本。");
        useDraft(draft);
        if (current.bounded) return true;
      }
      const offset = more ? current.poolOffset : 0;
      const result = await get(`/courses/${enc(state.courseId)}/question-library?class_id=${enc(state.classId)}&status=approved&limit=100&offset=${offset}`, request);
      const incoming = rows(result), seen = new Set(current.pool.map(poolKey));
      for (const item of incoming) need(id(item.question_id) && positive(item.version) && item.course_id === state.courseId && item.status === "approved"
        && object(item.content) && typeof item.content.stem === "string" && !seen.has(poolKey(item)), "审核题库分页或题目范围不一致。");
      if (more) need(incoming.length > 0 || offset >= result.total, "题库分页未前进，请刷新核对。");
      current.pool = more ? [...current.pool, ...incoming] : incoming;
      current.poolOffset = offset + incoming.length; current.poolLoaded = true;
      current.poolMore = Number.isSafeInteger(result.total) ? current.poolOffset < result.total : incoming.length === 100;
      if (!incoming.length) current.poolMore = false;
      return true;
    } catch (error) { failRead(error, request); return false; }
    finally { if (request.live()) { current.reading = false; paint(); } }
  }
  function createBounded() {
    if (!boundedEditable() || !current.boundedConfirmed || !positive(current.draft.current_goal_version)) return false;
    try {
      const future = Number(current.boundedEdit.future_round_count), interval = Number(current.boundedEdit.interval_seconds);
      const endsAt = new Date(current.boundedEdit.ends_at).getTime() / 1000;
      need(positive(future) && future <= 100 && positive(interval) && interval >= 60 && interval <= 31622400
        && Number.isFinite(endsAt) && endsAt > Date.now() / 1000, "请填写 1 至 100 轮、60 至 31622400 秒的间隔和未来的截止时间。");
      const pool = current.pool.filter((item) => current.poolSelection.includes(poolKey(item)));
      need(pool.length > 0 && pool.length <= 100 && pool.length === current.poolSelection.length, "请选择 1 至 100 道已审核题目作为有限计划题池。");
      return command("bounded", "POST", `${DRAFTS}/${enc(current.draft.id)}/bounded-plan`, {
        request_id: `bounded-${globalThis.crypto.randomUUID()}`, expected_goal_version: current.draft.current_goal_version, confirm: true,
        question_pool: pool.map((item) => ({ question_id: item.question_id, version: item.version })),
        future_round_count: future, interval_seconds: interval, ends_at: endsAt,
      });
    } catch (error) { current.error = error.message; paint(); return false; }
  }
  function updateBounded(node, repaint = true) {
    if (!boundedEditable()) return false;
    const field = node.dataset.insightBounded, questionKey = node.dataset.insightPool;
    if (questionKey) {
      if (!current.pool.some((item) => poolKey(item) === questionKey)) return false;
      current.poolSelection = node.checked ? [...new Set([...current.poolSelection, questionKey])] : current.poolSelection.filter((item) => item !== questionKey);
    } else if (["future_round_count", "interval_seconds", "ends_at"].includes(field)) current.boundedEdit[field] = node.value;
    else return false;
    current.boundedConfirmed = false; current.error = "";
    if (repaint) paint(); else syncEditorControls(); return true;
  }
  function updateDraft(node, repaint = true) {
    if (!draftEditable() || !current.edit || (!node.dataset.insightField && !node.dataset.insightStudent && !node.dataset.insightType)) return false;
    const config = current.edit, field = node.dataset.insightField, studentId = node.dataset.insightStudent, kind = node.dataset.insightType;
    if (studentId) {
      if (!Object.hasOwn(current.draft.basis.students, studentId)) return false;
      config.student_ids = node.checked ? [...new Set([...config.student_ids, studentId])] : config.student_ids.filter((item) => item !== studentId);
    } else if (kind && TYPES[kind]) {
      config.question_types[kind] = node.value === "" ? null : Number(node.value);
      config.question_count = Object.values(config.question_types).every(nonnegative) ? Object.values(config.question_types).reduce((sum, count) => sum + count, 0) : null;
    } else if (["deadline_at", "scheduled_at"].includes(field)) config[field] = node.value ? new Date(node.value).getTime() / 1000 : null;
    else if (["allow_late", "allow_retries", "allow_retry_after_deadline"].includes(field)) {
      config[field] = Boolean(node.checked); if (field === "allow_retries" && !config.allow_retries) config.allow_retry_after_deadline = false;
    } else if (["title", "description", "difficulty", "common_level"].includes(field)) config[field] = node.value;
    else return false;
    current.dirty = true; current.preview = null; current.publishConfirmed = false; current.notice = "配置已修改，请先保存，再重新预览。"; current.error = "";
    if (repaint) paint(); else syncEditorControls(); return true;
  }
  function syncEditorControls() {
    // Keep the focused input mounted while immediately invalidating old preview.
    for (const node of root?.querySelectorAll?.('[data-insight-confirm="publish"], [data-insight-confirm="bounded"]') || []) {
      node.checked = false; if (node.dataset.insightConfirm === "publish") node.disabled = true;
    }
    for (const node of root?.querySelectorAll?.("[data-insight-action]") || []) {
      if (["publish", "bounded"].includes(node.dataset.insightAction)) node.disabled = true;
      if (node.dataset.insightAction === "draft-preview" && current.dirty) node.disabled = true;
      if (node.dataset.insightAction === "draft-save") node.disabled = !draftEditable() || !current.dirty;
    }
    const preview = root?.querySelector?.("[data-insight-preview]");
    if (preview && current.dirty) preview.textContent = "配置已修改，请保存后重新预览。";
    const count = root?.querySelector?.("[data-insight-count]");
    if (count) count.textContent = `每人题量 ${current.edit?.question_count ?? "待核对"} 道（按题型合计）`;
  }
  const disabled = (condition) => condition ? " disabled" : "";
  const button = (action, label, off = false, value = "") => `<button type="button" class="ti-button" data-insight-action="${action}" data-insight-id="${esc(value)}"${disabled(off)}>${esc(label)}</button>`;
  const note = (value, kind = "") => value ? `<div class="ti-notice ${kind}" role="${kind === "error" ? "alert" : "status"}">${esc(value)}</div>` : "";
  const studentName = (studentId) => current.members.find((item) => item.user_id === studentId)?.display_name || current.members.find((item) => item.user_id === studentId)?.username || studentId;
  function refsHtml(refs, insight) {
    return `<ul class="ti-list">${refs.map((ref) => `<li><strong>${esc(ref)}</strong> <span class="ti-muted">${esc(text(insight.evidence_index[ref]))}</span></li>`).join("")}</ul>`;
  }
  function sourceHtml() {
    const revision = current.revision, old = revision && !current.reports.some((report) => report.current_revision_id === revision.id);
    return `<section class="panel ti-panel"><h2>冻结报告与生成依据</h2><div class="ti-selection"><label class="ti-field">学习目标<select data-insight-selection="goal"${disabled(Boolean(current.command))}><option value="">请选择目标</option>${current.goals.map((goal) => `<option value="${esc(goal.id)}"${goal.id === current.goalId ? " selected" : ""}>${esc(goal.title || goal.id)}</option>`).join("")}</select></label>
      <label class="ti-field">已保存报告修订<select data-insight-selection="revision"${disabled(Boolean(current.command) || !current.revisions.length)}><option value="">请选择报告修订</option>${current.revisions.map((item) => `<option value="${esc(item.id)}"${item.id === revision?.id ? " selected" : ""}>第 ${esc(item.revision_number)} 版 · ${esc(dateText(item.created_at))} · ${esc(item.id)}</option>`).join("")}</select></label></div>
      ${revision ? `<dl class="ti-meta"><div><dt>报告修订</dt><dd>${esc(revision.id)}</dd></div><div><dt>学习证据源修订</dt><dd>${esc(revision.source_revision)}</dd></div><div><dt>冻结内容校验值</dt><dd>${esc(revision.snapshot_sha256)}</dd></div></dl>${old ? note("当前选择的是旧报告修订，生成结果将保留旧证据，不代表最新学情。", "warning") : ""}` : '<p class="ti-muted">此目标尚无可用冻结报告，请先在阶段报告中生成并保存目标报告。</p>'}
      <p class="ti-muted">AI 将使用所选已保存报告中的当前已发布证据生成解释（用途：学情总结）。生成可能产生费用；后续读取列表、详情和状态不会再次发起生成。AI 解释不能替代程序计算的事实。</p>
      ${manage() ? `<label class="ti-check ti-confirm"><input type="checkbox" data-insight-confirm="generate"${current.generationConfirmed ? " checked" : ""}${disabled(locked() || !revision)}>已核对冻结报告及${old ? "旧证据" : "证据范围"}，同意产生本次总结费用</label>${button("generate", "创建 AI 学情总结", locked() || !revision || !current.generationConfirmed)}` : '<p class="ti-muted">当前班级为只读权限，可查看已存总结与草稿；创建和发布需要班级管理权限。</p>'}</section>`;
  }
  function historyHtml() {
    const start = current.historyPage * HISTORY_SIZE;
    return `<section class="panel ti-panel"><h2>已保存的总结</h2><div class="ti-history">${current.insights.slice(start, start + HISTORY_SIZE).map((item) => `<button type="button" class="ti-button" data-insight-action="open" data-insight-id="${esc(item.id)}" aria-pressed="${item.id === current.insightId}"${disabled(Boolean(current.command))}>${esc(dateText(item.created_at))} · ${esc(STATUSES[item.status])}${item.stale ? " · 旧数据" : ""} · ${esc(item.id)}</button>`).join("") || '<p class="ti-muted">此目标还没有保存的总结。先选择冻结报告，再确认生成。</p>'}</div>${current.insights.length > HISTORY_SIZE ? `<div class="ti-pagination">${button("previous", "上一页", current.historyPage === 0 || Boolean(current.command))}<span>第 ${current.historyPage + 1} / ${Math.ceil(current.insights.length / HISTORY_SIZE)} 页</span>${button("next", "下一页", start + HISTORY_SIZE >= current.insights.length || Boolean(current.command))}</div>` : ""}${current.insights.length === 100 ? '<p class="ti-muted">当前显示最近 100 条；通知中的总结可按标识直接定位。</p>' : ""}</section>`;
  }
  function jobHtml() {
    const insight = current.insight, job = current.job;
    if (!insight) return "";
    return `<section class="panel ti-panel"><div class="ti-section-head"><h2>生成任务与费用</h2>${button("detail", "刷新状态并核对费用", locked())}</div><dl class="ti-meta"><div><dt>总结状态</dt><dd>${esc(STATUSES[insight.status])}</dd></div><div><dt>任务状态</dt><dd>${job ? esc(STATUSES[job.status] || job.status) : "请刷新核对"}${job?.cancel_requested ? "（取消已请求）" : ""}</dd></div><div><dt>任务标识</dt><dd>${esc(insight.job_id)}</dd></div><div><dt>费用状态</dt><dd>${!job ? "尚未读取" : job.reconciliation_required || job.cost_pending ? "费用待核对，暂不能恢复" : "无待核对标记；本接口未提供费用金额"}</dd></div></dl>
      ${job?.cost_pending || job?.reconciliation_required ? note("调用结果或费用仍需核对，请等待管理端完成核对后刷新。刷新不会发起模型调用。", "warning") : ""}${insight.error ? note(text(insight.error.message || insight.error), "error") : ""}
      ${manage() ? `<div class="ti-actions">${button("cancel", "取消此任务", locked() || !canCancel())}</div>${canResume() ? `<label class="ti-check ti-confirm"><input type="checkbox" data-insight-confirm="resume"${current.resumeConfirmed ? " checked" : ""}${disabled(locked())}>已核对任务与费用，确认恢复可能再次产生费用</label>${button("resume", "确认付费恢复", locked() || !current.resumeConfirmed)}` : ""}` : ""}</section>`;
  }
  function insightHtml() {
    const insight = current.insight; if (!insight) return "";
    const facts = insight.facts, result = insight.status === "completed" ? insight.result : null;
    return `<section class="panel ti-panel"><div class="ti-section-head"><h2>学情总结</h2><span class="ti-tag">${insight.stale ? "旧数据 · 冻结历史证据" : "与当前源修订一致"}</span></div>${insight.stale ? note("学习证据或报告已更新。本总结仍基于下列冻结修订；旧结论不会自动改写。请在报告中保存最新版本后按需生成新总结。", "warning") : ""}
      <dl class="ti-meta"><div><dt>来源报告修订</dt><dd>${esc(insight.source_report_revision)}</dd></div><div><dt>证据源修订 / 当前源修订</dt><dd>${esc(insight.source_revision)} / ${esc(insight.current_source_revision ?? "未提供")}</dd></div><div><dt>报告校验值</dt><dd>${esc(insight.snapshot_sha256)}</dd></div><div><dt>总结输入校验值</dt><dd>${esc(insight.input_hash)}</dd></div></dl>
      <h3>程序计算事实</h3><p class="ti-muted">未取得足够独立作答证据的学生标为“证据不足”，不按零分计入。这里的数值由服务端规则计算，AI 不修改分数与事实。</p><dl class="ti-facts"><div><dt>报告学生数</dt><dd>${esc(facts.student_count)}</dd></div>${Object.entries(MASTERY).map(([key, label]) => `<div><dt>${label}</dt><dd>${esc(facts.status_counts[key])}</dd></div>`).join("")}</dl>
      ${facts.students.length ? `<div class="ti-table-scroll" role="region" aria-label="程序计算的学生学情事实，可横向滚动" tabindex="0"><table><thead><tr><th scope="col">学生</th><th scope="col">状态</th><th scope="col">掌握度</th><th scope="col">独立题族</th><th scope="col">已观察题族</th><th scope="col">证据引用</th></tr></thead><tbody>${facts.students.map((fact) => `<tr><td>${esc(studentName(insight.student_codes[fact.student_code]))}</td><td>${esc(MASTERY[fact.status])}</td><td>${fact.percent === null ? "证据不足" : `${esc(fact.percent)}%`}</td><td>${esc(fact.independent_families)}</td><td>${esc(fact.observed_families)}</td><td>${esc(fact.evidence_refs.join("、"))}</td></tr>`).join("")}</tbody></table></div>` : note("冻结报告没有学生作答事实，证据不足，不能据此声称学生已掌握目标。", "warning")}
      <h3>AI 解释</h3>${result ? `<p class="ti-text">${esc(result.summary)}</p>${result.findings.map((finding) => `<article class="ti-paper"><h3>${esc(studentName(insight.student_codes[finding.student_code]))}</h3><p class="ti-text">${esc(finding.explanation)}</p><details><summary>核对这条解释的证据引用</summary>${refsHtml(finding.evidence_refs, insight)}</details></article>`).join("")}<ul class="ti-list">${result.limitations.map((item) => `<li>${esc(item)}</li>`).join("")}</ul><details><summary>总结依据与证据索引</summary>${refsHtml(result.evidence_refs, insight)}</details>` : '<p class="ti-muted">当前尚无通过冻结证据校验的完整 AI 解释。请查看任务状态；程序事实仍按报告显示。</p>'}
      <p class="ti-muted">下一轮由规则从已审核题库配卷，AI 仅提供解释。教师须编辑、预览完整题卷并明确确认后，才会发布到原目标。</p>${button("draft-create", "从此总结创建下一轮草稿", !manage() || locked() || insight.status !== "completed" || Boolean(current.draft))}${insight.next_round_drafts?.length ? `<label class="ti-field">继续已存下一轮草稿<select data-insight-selection="draft"${disabled(Boolean(current.command) || current.reading)}><option value="">请选择已存草稿</option>${insight.next_round_drafts.map((draft) => `<option value="${esc(draft.id)}"${draft.id === current.draft?.id ? " selected" : ""}>第 ${esc(draft.next_round)} 轮 · 修订 ${esc(draft.revision)} · ${draft.status === "published" ? "已发布" : "草稿"} · ${esc(draft.id)}</option>`).join("")}</select></label>` : ""}</section>`;
  }
  function questionHtml(question) {
    return `<li class="ti-question"><span class="ti-tag">${esc(TYPES[question.type] || question.type)}${question.common ? " · 共同题" : ""}</span><p class="ti-text">${esc(question.stem)}</p>${question.options.length ? `<ol type="A" class="ti-list">${question.options.map((option) => `<li>${esc(text(object(option) ? option.text ?? option.label ?? option.value ?? option : option))}</li>`).join("")}</ol>` : ""}<p class="ti-muted">${esc(question.points)} 分 · 题目 ${esc(question.question_id)} · 版本 ${esc(question.version)}</p><p class="ti-muted">来源：${question.source_anchors.length ? question.source_anchors.map((anchor) => esc(text(anchor))).join("；") : "未记录来源，不推断或补造"}</p>${Object.hasOwn(question, "answer") ? `<p class="ti-muted">参考答案：${esc(text(question.answer))}</p>` : ""}${question.rubric ? `<pre>评分依据：${esc(text(question.rubric))}</pre>` : ""}</li>`;
  }
  function previewHtml() {
    const preview = current.preview; if (!preview) return '<p class="ti-muted">保存当前配置后预览完整题卷。预览不会发布，也不会调用模型生成题目。</p>';
    const messages = [...preview.errors.map((item) => typeof item === "string" ? item : item.message || text(item)),
      ...preview.deficits.map((item) => `${TYPES[item.question_type] || item.question_type}：需要 ${item.required} 道，可用 ${item.available} 道。${item.reason || "请补齐题库后重新预览。"}`)];
    return `<section data-insight-preview aria-label="下一轮题卷预览"><h3>已保存修订 ${esc(preview.draft_revision)} 的题卷预览</h3>${note(preview.ready ? previewReady() ? "题卷已齐。请逐份核对后确认发布。" : "预览已过期，请重新预览。" : "当前还不能发布。请处理下列阻断项后重新保存并预览。", preview.ready && previewReady() ? "" : "warning")}${messages.length ? `<ul class="ti-list">${messages.map((message) => `<li>${esc(message)}</li>`).join("")}</ul>` : ""}${preview.warnings.map((item) => note(typeof item === "string" ? item : item.message || text(item), "warning")).join("")}
      <p class="ti-muted">发布目标：${esc(preview.origin_goal_id)}，第 ${esc(preview.next_round)} 轮。每人 ${esc(preview.question_count)} 道；共同题 ${esc(preview.common_count ?? "未提供")} 道。预览有效至 ${esc(dateText(preview.preview_expires_at))}。</p>
      <div class="ti-papers">${preview.students.map((student) => `<details class="ti-paper"><summary>${esc(student.display_name || studentName(student.student_id))}的完整题卷 · ${esc(student.questions.length)} 道${student.total_score == null ? "" : ` · ${esc(student.total_score)} 分`}</summary>${student.suggestion?.message ? `<p class="ti-muted">${esc(student.suggestion.message)}</p>` : ""}${student.questions.length ? `<ol class="ti-list">${student.questions.map(questionHtml).join("")}</ol>` : '<p class="ti-muted">尚未得到完整题卷，请处理缺题或前轮状态提示。</p>'}</details>`).join("")}</div></section>`;
  }
  function boundedHtml() {
    if (!current.draft || !(current.published || current.draft.status === "published")) return "";
    if (current.bounded) return `<section class="ti-footer"><h3>有限持续计划</h3><p>计划状态：${esc(current.bounded.status)}；剩余 ${esc(current.bounded.remaining_rounds)} 轮；间隔 ${esc(current.bounded.interval_seconds)} 秒；截止 ${esc(dateText(current.bounded.ends_at))}。</p><p class="ti-muted">按已确认的题池和上限执行；需要停止后续计划时，可在作答复核的目标管理中操作。</p></section>`;
    if (!manage()) return '<p class="ti-muted">有限持续计划需要班级管理权限与教师明确确认。</p>';
    const edit = current.boundedEdit, off = !boundedEditable();
    return `<section class="ti-footer"><h3>设置有限持续计划</h3><p class="ti-muted">可选。先核对已审核题池，再明确设置未来轮数、间隔与截止。系统仅在前轮满足条件时安排后续轮次，并检查原目标题族未被使用；缺题或版本冲突时阻断，不会无限调度。</p>${button("bounded-load", current.poolLoaded ? "重新读取审核题池" : "读取审核题池与当前目标版本", off)}
      ${current.poolLoaded ? `<p class="ti-muted">已选择 ${current.poolSelection.length} 道。此列表为审核题库，是否属于原目标未使用题族由服务端最终核对；请逐题审核内容与来源。</p><div class="ti-members">${current.pool.map((item) => `<details><summary>${esc(item.content.stem)}</summary><label class="ti-check"><input type="checkbox" data-insight-pool="${esc(poolKey(item))}"${current.poolSelection.includes(poolKey(item)) ? " checked" : ""}${disabled(off)}>选用此题 · ${esc(TYPES[item.content.type] || item.content.type)} · 版本 ${esc(item.version)}</label><pre class="ti-raw">${esc(text(item.content))}</pre></details>`).join("") || '<p class="ti-muted">没有可选择的已审核题目。</p>'}</div>${current.poolMore ? button("bounded-more", "继续读取审核题库", off) : ""}
      <div class="ti-fields"><label class="ti-field">未来轮数（1 至 100）<input type="number" min="1" max="100" step="1" data-insight-bounded="future_round_count" value="${esc(edit.future_round_count)}"${disabled(off)}></label><label class="ti-field">轮次间隔（秒，至少 60）<input type="number" min="60" max="31622400" step="1" data-insight-bounded="interval_seconds" value="${esc(edit.interval_seconds)}"${disabled(off)}></label><label class="ti-field">计划截止时间（本机时区）<input type="datetime-local" data-insight-bounded="ends_at" value="${esc(edit.ends_at)}"${disabled(off)}></label></div>
      <label class="ti-check ti-confirm"><input type="checkbox" data-insight-confirm="bounded"${current.boundedConfirmed ? " checked" : ""}${disabled(off)}>已逐题核对审核题池及未使用题族要求，确认按填写的轮数、间隔和截止启用有限持续计划</label>${button("bounded", "确认启用有限持续计划", off || !current.boundedConfirmed || !current.poolSelection.length)}` : ""}</section>`;
  }
  function draftHtml() {
    const draft = current.draft, config = current.edit; if (!draft || !config) return "";
    const off = !draftEditable(), basis = Object.keys(draft.basis.students), publication = current.published;
    const field = (name, label, type, value, extra = "") => `<label class="ti-field">${label}<input type="${type}" data-insight-field="${name}" value="${esc(value)}"${extra}${disabled(off)}></label>`;
    return `<section class="panel ti-panel"><div class="ti-section-head"><h2>原目标的下一轮草稿</h2><span class="ti-tag">${publication || draft.status === "published" ? "已发布" : current.dirty ? "有未保存修改" : "已保存"}</span></div>
      <dl class="ti-meta"><div><dt>原学习目标</dt><dd>${esc(draft.origin_goal_id)}</dd></div><div><dt>下一轮 / 草稿修订</dt><dd>第 ${esc(draft.next_round)} 轮 / 修订 ${esc(draft.revision)}</dd></div><div><dt>来源报告修订</dt><dd>${esc(draft.source_report_revision)}</dd></div><div><dt>草稿标识</dt><dd>${esc(draft.id)}</dd></div></dl>
      ${publication ? note(`已发布到原目标 ${publication.goal_id} 的第 ${publication.round_no} 轮。不会再次发布此草稿。`) : ""}${current.insight?.stale ? note("此草稿依据旧报告总结，请核对是否仍适合当前学生；服务器会再次检查原目标版本与前轮状态。", "warning") : ""}
      <p class="ti-muted">AI 提供学情解释，题卷由规则从已审核题库选择。教师逐轮确认；本次发布不会创建另一个学习目标。</p>
      <fieldset${disabled(off)}><legend>参加下一轮的学生</legend><div class="ti-members">${basis.map((studentId) => `<label class="ti-check"><input type="checkbox" data-insight-student="${esc(studentId)}"${config.student_ids.includes(studentId) ? " checked" : ""}${disabled(off)}>${esc(studentName(studentId))}</label>`).join("")}</div></fieldset>
      <div class="ti-fields">${field("title", "本轮目标标题", "text", config.title, ' maxlength="160"')}
      <label class="ti-field">题目难度<select data-insight-field="difficulty"${disabled(off)}>${Object.entries({ any: "不限，按可用题库", easy: "基础", medium: "适中", hard: "进阶" }).map(([value, label]) => `<option value="${value}"${value === config.difficulty ? " selected" : ""}>${label}</option>`).join("")}</select></label>
      <label class="ti-field">补充要求<textarea rows="2" maxlength="10000" data-insight-field="description"${disabled(off)}>${esc(config.description)}</textarea></label>
      <label class="ti-field">共同题档位<select data-insight-field="common_level"${disabled(off)}>${Object.entries({ light: "轻量共同题", balanced: "均衡共同题", heavy: "较多共同题" }).map(([value, label]) => `<option value="${value}"${value === config.common_level ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>
      <fieldset${disabled(off)}><legend data-insight-count>每人题量 ${esc(config.question_count ?? "待核对")} 道（按题型合计）</legend><div class="ti-types">${Object.entries(TYPES).map(([kind, label]) => `<label class="ti-field">${label}<input type="number" min="0" max="100" step="1" data-insight-type="${kind}" value="${esc(config.question_types[kind])}"${disabled(off)}></label>`).join("")}</div><p class="ti-muted">调整各题型数量即可改变每人题量；总量为 1 至 100 道。题型数与总量始终一同保存。</p></fieldset>
      <div class="ti-fields">${field("scheduled_at", "开放时间（本机时区，留空即发布后开放）", "datetime-local", localTime(config.scheduled_at))}${field("deadline_at", "截止时间（本机时区，留空不设截止）", "datetime-local", localTime(config.deadline_at))}</div>
      <div class="ti-policy">${Object.entries({ allow_late: "截止后允许迟交，迟交会标明", allow_retries: "允许重做，保留首次成绩", allow_retry_after_deadline: "截止后仍允许重做" }).map(([name, label]) => `<label class="ti-check"><input type="checkbox" data-insight-field="${name}"${config[name] ? " checked" : ""}${disabled(off || name === "allow_retry_after_deadline" && !config.allow_retries)}>${label}</label>`).join("")}</div>
      ${draft.paper_overrides && Object.keys(draft.paper_overrides).length ? '<p class="ti-muted">此草稿含已保存的教师逐人题目调整，将原样保留；题卷以重新预览的结果为准。</p>' : ""}
      <div class="ti-actions">${button("draft-save", "保存草稿修改", off || !current.dirty)}${button("draft-preview", "预览完整题卷", off || current.dirty)}</div>
      ${publication ? "" : previewHtml()}${manage() && !publication && draft.status === "draft" ? `<footer class="ti-footer"><label class="ti-check ti-confirm"><input type="checkbox" data-insight-confirm="publish"${current.publishConfirmed ? " checked" : ""}${disabled(off || !previewReady())}>已核对所有学生、完整题目与来源，确认发布到原目标第 ${esc(draft.next_round)} 轮</label>${button("publish", "确认发布原目标下一轮", off || !previewReady() || !current.publishConfirmed)}</footer>` : ""}${boundedHtml()}</section>`;
  }
  function html() {
    syncContext();
    let content;
    if (!session()) content = note("请使用已连接的有效教师账号查看学情总结。");
    else if (state.loggingOut) content = note("正在退出，学情总结操作已暂停。原请求仍保留在本次会话；若退出失败，可继续核对。");
    else if (current.revoked) content = note(current.error, "error");
    else if (!id(state.classId) || !id(state.courseId)) content = note("请先选择课程和班级。");
    else content = `<header class="ti-heading"><div><h1>学情总结与下一轮</h1><p class="ti-muted">核对冻结报告中的事实与解释，再安排原学习目标的下一轮练习。</p></div><div class="ti-actions">${button("refresh", current.reading ? "正在读取…" : current.dirty ? "重新读取（放弃未保存输入）" : "刷新列表与详情", Boolean(current.command) || current.reading)}</div></header>${note(current.error, "error")}${note(current.notice)}${current.needsRefresh && !current.command ? note("状态需要重新核对，请刷新后继续。", "warning") : ""}${current.command ? note(current.command.inFlight ? "正在提交，原请求内容已固定，请等待回执。" : "原请求结果未知；不会自动重复提交，也不能切换目标。", "warning") + (!current.command.inFlight ? button("retry", ["cancel", "resume"].includes(current.command.kind) ? "核对原任务状态" : "用同一请求重试") : "") : ""}${current.reading ? '<p role="status" class="ti-muted">正在核对所选目标与冻结记录…</p>' : ""}${sourceHtml()}${historyHtml()}${jobHtml()}${insightHtml()}${draftHtml()}`;
    return `<section class="teacher-insights" data-teacher-insights aria-label="教师学情总结与下一轮">${content}</section>`;
  }
  function bind(container) {
    syncContext(); root = container?.matches?.("[data-teacher-insights]") ? container : container?.querySelector?.("[data-teacher-insights]") || null;
    if (!root) return; bindings.set(root, epoch); if (listeners.has(root)) return;
    listeners.add(root); const bound = root;
    const allowed = (event) => { const boundEpoch = bindings.get(bound); syncContext(); return active() && root === bound && bound.isConnected !== false
      && boundEpoch === epoch && bound.contains(event.target) && event.target.closest?.("[data-teacher-insights]") === bound; };
    bound.addEventListener("click", (event) => {
      if (!allowed(event)) return false;
      const target = event.target.closest?.("[data-insight-action]");
      if (!target || target.disabled || !bound.contains(target)) return false;
      event.preventDefault(); event.stopPropagation?.(); const action = target.dataset.insightAction;
      if (action === "retry") return retry();
      if (current.command) return false;
      if (action === "refresh") return load({ force: true });
      if (action === "generate") return generate();
      if (action === "open") return openSelection({ goalId: current.goalId, insightId: target.dataset.insightId });
      if (action === "detail") return refreshInsight();
      if (action === "cancel" || action === "resume") return jobAction(action);
      if (action === "previous" || action === "next") { current.historyPage += action === "next" ? 1 : -1; paint(); return true; }
      return draftAction(action);
    });
    bound.addEventListener("input", (event) => {
      if (!allowed(event) || event.target.disabled || current.command || event.target.type === "checkbox" || event.target.tagName === "SELECT") return false;
      if (event.target.dataset.insightBounded) return updateBounded(event.target, false);
      return updateDraft(event.target, false);
    });
    bound.addEventListener("change", (event) => {
      if (!allowed(event) || event.target.disabled || current.command) return false;
      const node = event.target;
      if (node.dataset.insightSelection === "goal" && node.value) return openSelection({ goalId: node.value });
      if (node.dataset.insightSelection === "revision" && node.value) return openRevision(node.value);
      if (node.dataset.insightSelection === "draft" && node.value) return openDraft(node.value);
      if (node.dataset.insightConfirm && !locked() && manage()) {
        const key = { generate: "generationConfirmed", resume: "resumeConfirmed", publish: "publishConfirmed", bounded: "boundedConfirmed" }[node.dataset.insightConfirm];
        if (key) { current[key] = Boolean(node.checked); paint(); return true; }
      }
      if (node.dataset.insightBounded || node.dataset.insightPool) return updateBounded(node);
      return updateDraft(node);
    });
  }
  return { render: html, bind, load, reset, leave, openSelection };
}
