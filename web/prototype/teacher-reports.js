// Reports keep server-owned revisions and retry an uncertain generation with the
// original scope, source revision and idempotency key. Private data stays in memory.
const SCOPES = { class: "班级阶段学情", student: "学生个人学习", goal: "学习目标执行", question_bank: "题库质量与使用" };
const FORMATS = { html: "网页 HTML", pdf: "PDF", xlsx: "Excel XLSX", json: "JSON", csv: "CSV 压缩包" };
const METRICS = { students: "学生", goals: "学习目标", question_versions: "题目版本", evidence: "作答证据", mastery_rows: "评估条目", insufficient_evidence_rows: "待评估条目", usage_rows: "使用记录" };
const TABLE_LABELS = { summary: "报告概览", students: "学生", goals: "学习目标", questions: "题目版本", question_usage: "题目使用情况", mastery: "掌握度证据", evidence: "作答证据", sources: "材料出处", rules: "规则与说明" };
const PAGE_SIZE = 100, HISTORY_PAGE_SIZE = 10;
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));
const id = (value) => typeof value === "string" ? value.trim() : "";
const list = (value) => Array.isArray(value) ? value : [];
const clone = (value) => JSON.parse(JSON.stringify(value));
const sameScope = (left, right) => Boolean(left && right && ["kind", "course_id", "class_id", "student_id", "goal_id", "question_bank_id"].every((key) => (left[key] || "") === (right[key] || "")));
const unknownResult = (error) => !Number.isFinite(Number(error?.status)) || Number(error?.status) === 0 || Number(error?.status) >= 500 || [408, 425].includes(Number(error?.status));
const stamp = (value) => Number.isFinite(value) ? new Date(value * 1000).toLocaleString("zh-CN") : "时间未提供";
const number = (value) => Number.isFinite(value) ? String(value) : "—";
const STALE = Symbol("stale report read");

function valueText(key, value, snapshot) {
  if (value == null || value === "") return key === "percent" ? "证据不足" : "—";
  if (key === "student_id") return list(snapshot.tables?.students?.rows).find((row) => row.student_id === value)?.name || String(value);
  if (key === "goal_id") return list(snapshot.tables?.goals?.rows).find((row) => row.goal_id === value)?.title || String(value);
  if (key === "goal_ids" && Array.isArray(value)) return value.map((item) => valueText("goal_id", item, snapshot)).join("、");
  if (key === "observed_at") return stamp(value);
  if (key === "score_ratio" && Number.isFinite(value)) return `${Math.round(value * 100)}%`;
  if (key === "percent" && Number.isFinite(value)) return `${value}%`;
  if (typeof value === "boolean") return value ? "是" : "否";
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

export function createTeacherReports({ api, state, render: renderApp = () => {}, toast = () => {}, onChanged } = {}) {
  let root = null, epoch = 0, sequence = 0, context = "", loadingPromise = null;
  const controllers = new Set(), downloadUrls = new Set(), bindings = new WeakSet();
  let current = fresh();

  function fresh(selection = {}) {
    return {
      kind: selection.kind || "class", goalId: id(selection.goalId), studentId: id(selection.studentId),
      goals: [], members: [], access: [], rules: null, source: null, allReports: [], reports: [], revisions: [], revision: null,
      selectedReportId: "", historyPage: 0, previewHtml: "", previewError: "", previewLoading: false,
      loading: false, loaded: false, opening: false, downloading: false, error: "", message: "", command: null,
      suspended: false, revoked: false,
    };
  }
  const actor = () => api?.state?.user;
  const contextKey = () => JSON.stringify([state.live, state.role, state.transitionId, state.teacherContextVersion, state.courseId, state.classId, actor()?.id, actor()?.role, actor()?.status]);
  const session = () => state.live === true && state.role === "teacher" && actor()?.role === "teacher" && actor()?.status === "active" && Boolean(actor()?.id);
  const active = () => session() && !state.loggingOut && !current.suspended && !current.revoked;
  const valid = (token, data) => token === epoch && data === current && context === contextKey() && session();
  const busy = () => Boolean(current.command);
  const paint = () => { if (active()) renderApp(); };
  function stopReads() {
    sequence += 1;
    controllers.forEach((controller) => controller.abort()); controllers.clear();
    loadingPromise = null;
    current.loading = false; current.opening = false; current.previewLoading = false; current.downloading = false;
  }
  function clearPreview() {
    current.previewHtml = ""; current.previewError = ""; current.previewLoading = false;
    const frame = root?.querySelector?.("[data-report-preview-frame]");
    if (frame) { frame.srcdoc = ""; frame.removeAttribute?.("src"); }
  }
  function release() {
    stopReads(); clearPreview();
    downloadUrls.forEach((url) => URL.revokeObjectURL(url)); downloadUrls.clear();
    if (root?.isConnected) root.innerHTML = "";
    root = null;
  }
  function syncContext() {
    if (context !== contextKey()) {
      release(); epoch += 1; context = contextKey();
      const selection = state.reviewSelection || {};
      current = fresh({ ...selection, kind: selection.goalId ? "goal" : selection.studentId ? "student" : "class" });
    }
  }
  function reset() { release(); epoch += 1; context = ""; current = fresh(); }
  function leave() {
    syncContext();
    if (busy()) {
      current.error = current.command.inFlight ? "报告正在生成，请等待结果后再离开。" : "生成结果尚未确认，请用同一请求重试后再离开。";
      if (active()) toast(current.error);
      paint(); return false;
    }
    const selection = { kind: current.kind, goalId: current.goalId, studentId: current.studentId };
    release(); epoch += 1; current = fresh(selection); current.suspended = true;
    return true;
  }
  function scope() {
    if (!id(state.courseId)) throw new Error("请先选择有权限的课程。");
    // The current data model has one course-level question bank. Its identifier is
    // the course ID; adding class_id here would falsely imply a narrower scope.
    if (current.kind === "question_bank") return { kind: "question_bank", course_id: state.courseId, question_bank_id: state.courseId };
    if (!id(state.classId)) throw new Error("请先选择有权限的班级。");
    const result = { kind: current.kind, course_id: state.courseId, class_id: state.classId };
    if (current.kind === "student") {
      if (!current.members.some((member) => member.user_id === current.studentId)) throw new Error("请选择当前班级的学生。");
      result.student_id = current.studentId;
    }
    if (current.kind === "goal") {
      if (!current.goals.some((goal) => goal.id === current.goalId)) throw new Error("指定学习目标不存在或不在当前班级。");
      result.goal_id = current.goalId;
    }
    return result;
  }
  const safeScope = () => { try { return scope(); } catch { return null; } };
  function canCreate() {
    const entry = current.access.find((item) => item.course.id === state.courseId);
    return current.kind === "question_bank" ? entry?.course.can_manage_materials === true
      : entry?.classes.some((item) => item.id === state.classId && item.can_manage === true) === true;
  }
  const formats = () => list(current.rules?.export_formats).filter((format, index, values) => FORMATS[format] && values.indexOf(format) === index);
  const sourceChanged = () => current.source && current.revision && current.source.source_revision > current.revision.source_revision;
  function beginRead() {
    stopReads();
    const token = epoch, data = current, serial = sequence;
    return { token, data, serial, live: () => valid(token, data) && serial === sequence && active() };
  }
  async function read(name, args, request) {
    if (!request.live()) throw STALE;
    const controller = new AbortController(); controllers.add(controller);
    try {
      const result = await api[name](...args, { signal: controller.signal });
      if (!request.live()) throw STALE;
      return result;
    } finally { controllers.delete(controller); }
  }
  function failRead(error, request) {
    if (!request.live() || error === STALE || error?.name === "AbortError") return;
    current.error = error?.message || "报告读取失败，请重试。";
    if ([401, 403, 404].includes(Number(error?.status))) {
      current.revoked = true; clearPreview(); current.revision = null; current.revisions = []; current.reports = [];
      current.allReports = []; current.members = []; current.goals = []; current.source = null;
      // Repaint once to remove records after an explicit authorization failure.
      if (valid(request.token, request.data) && !state.loggingOut) renderApp();
    }
  }
  function validateRevision(revision, wanted, reportId = "", revisionId = "") {
    if (!id(revision?.id) || !id(revision?.report_id) || !Number.isSafeInteger(revision.source_revision)
        || !Number.isSafeInteger(revision.revision_number) || revision.revision_number < 1
        || !id(revision.snapshot_sha256) || !sameScope(revision.snapshot?.scope, wanted)
        || (reportId && revision.report_id !== reportId) || (revisionId && revision.id !== revisionId)) {
      throw new Error("服务返回的报告范围或修订不一致，请重新核对。");
    }
    return revision;
  }
  async function catalog(request) {
    if (!state.classId) { current.goals = []; current.members = []; return; }
    const goals = [], seen = new Set(); let offset = 0;
    while (true) {
      const controller = new AbortController(); controllers.add(controller);
      let page;
      try { page = await api.goals(state.classId, { limit: PAGE_SIZE, offset, signal: controller.signal }); }
      finally { controllers.delete(controller); }
      if (!request.live()) throw STALE;
      if (!Array.isArray(page)) throw new Error("学习目标列表格式不完整。");
      for (const goal of page) {
        if (!id(goal.id) || seen.has(goal.id) || (goal.class_id && goal.class_id !== state.classId)) throw new Error("历史目标分页未前进或班级不一致，请刷新。");
        goals.push(goal); seen.add(goal.id);
      }
      offset += page.length;
      if (page.length < PAGE_SIZE) break;
    }
    const members = await read("classMembers", [state.classId], request);
    if (!Array.isArray(members)) throw new Error("班级成员列表格式不完整。");
    current.goals = goals;
    current.members = members.filter((member) => id(member.user_id));
    if (!current.goalId) current.goalId = goals[0]?.id || "";
    if (!current.studentId) current.studentId = current.members[0]?.user_id || "";
  }
  async function preview(request) {
    if (!request.live() || !current.revision || !formats().includes("html")) return;
    const revision = current.revision;
    current.previewLoading = true; current.previewError = ""; paint();
    try {
      const controller = new AbortController(); controllers.add(controller);
      let html;
      try { html = await api.reportPreview(revision.report_id, revision.id, { signal: controller.signal, snapshotHash: revision.snapshot_sha256 }); }
      finally { controllers.delete(controller); }
      if (!request.live() || current.revision !== revision) return;
      if (typeof html !== "string") throw new Error("报告预览不是有效的网页文本。");
      current.previewHtml = html;
    } catch (error) {
      if (request.live() && error?.name !== "AbortError") {
        clearPreview();
        if ([401, 403, 404].includes(Number(error?.status))) failRead(error, request);
        else current.previewError = error?.message || "预览暂时不可用，请重新读取版本。";
      }
    } finally { if (request.live()) { current.previewLoading = false; paint(); } }
  }
  async function readRevision(reportId, revisionId, wanted, request) {
    clearPreview(); current.revision = null; current.opening = true; paint();
    const revision = await read("reportRevision", [reportId, revisionId], request);
    validateRevision(revision, wanted, reportId, revisionId);
    current.revision = revision; current.selectedReportId = reportId;
    const index = current.revisions.findIndex((item) => item.id === revision.id);
    if (index >= 0) current.historyPage = Math.floor(index / HISTORY_PAGE_SIZE);
    await preview(request);
  }
  async function readReport(report, wanted, request) {
    clearPreview(); current.revision = null;
    current.selectedReportId = report.id; current.revisions = []; current.historyPage = 0; current.opening = true; paint();
    const revisions = await read("reportRevisions", [report.id], request);
    if (!Array.isArray(revisions) || revisions.some((item) => !id(item.id) || item.report_id !== report.id)) throw new Error("报告历史修订与所选报告不一致。");
    current.revisions = revisions;
    const revisionId = id(report.current_revision_id) || revisions[0]?.id;
    if (revisionId) await readRevision(report.id, revisionId, wanted, request);
  }
  async function load({ force = false } = {}) {
    syncContext(); current.suspended = false;
    if (!active() || busy()) return false;
    if (loadingPromise && !force) return loadingPromise;
    if (current.loaded && !force) return true;
    const request = beginRead(), data = current;
    data.loading = true; data.error = ""; data.message = ""; paint();
    const work = async () => {
      try {
        if (!id(state.courseId)) throw new Error("请先选择有权限的课程。");
        const [rules, allReports, access] = await Promise.all([
          read("reportFormats", [], request), read("reportList", [], request), read("teacherClasses", [], request), catalog(request),
        ]);
        if (!request.live()) return false;
        data.rules = rules; data.allReports = list(allReports); data.access = list(access);
        if (!formats().length) throw new Error("服务未返回支持的报告格式，请刷新。");
        const wanted = scope();
        data.reports = data.allReports.filter((report) => sameScope(report.scope, wanted));
        if (data.revision && !data.reports.some((report) => report.id === data.revision.report_id)) {
          clearPreview(); data.revision = null; data.revisions = []; data.selectedReportId = "";
        }
        data.source = null;
        if (canCreate()) {
          const source = await read("reportSource", [wanted], request);
          if (!sameScope(source?.scope, wanted) || !Number.isSafeInteger(source.source_revision)) throw new Error("报告来源与当前范围不一致。");
          data.source = source;
        }
        data.loaded = true;
        if (data.revision && !sameScope(data.revision.snapshot?.scope, wanted)) { data.revision = null; clearPreview(); }
        if (!data.revision && data.reports[0]) await readReport(data.reports[0], wanted, request);
        else if (data.revision) {
          const selected = data.revision;
          await readRevision(selected.report_id, selected.id, wanted, request);
        }
        return request.live();
      } catch (error) { failRead(error, request); return false; }
      finally { if (valid(request.token, data) && request.serial === sequence) { data.loading = false; data.opening = false; loadingPromise = null; paint(); } }
    };
    loadingPromise = work();
    return loadingPromise;
  }
  function clearSelection() {
    stopReads(); clearPreview(); current.loaded = false; current.source = null;
    current.reports = []; current.revisions = []; current.selectedReportId = ""; current.revision = null;
    current.error = ""; current.message = ""; current.historyPage = 0;
  }
  async function openSelection(selection = {}) {
    syncContext();
    if (!session() || state.loggingOut || busy()) return false;
    current.suspended = false;
    const goalId = id(selection.goalId), studentId = id(selection.studentId);
    if (goalId) { current.goalId = goalId; current.kind = "goal"; }
    else if (studentId) { current.studentId = studentId; current.kind = "student"; }
    clearSelection();
    return load({ force: true });
  }
  async function changeScope(kind, value) {
    syncContext();
    if (!active() || busy() || !SCOPES[kind]) return false;
    current.kind = kind;
    if (value !== undefined && kind === "goal") current.goalId = id(value);
    if (value !== undefined && kind === "student") current.studentId = id(value);
    clearSelection();
    return load({ force: true });
  }
  async function openReport(reportId, revisionId) {
    syncContext();
    if (!active() || busy()) return false;
    const report = current.reports.find((item) => item.id === reportId), wanted = safeScope();
    if (!report || !wanted || (revisionId && !current.revisions.some((item) => item.report_id === reportId && item.id === revisionId))) return false;
    const request = beginRead(); current.opening = true; current.error = "";
    try {
      if (revisionId) await readRevision(reportId, revisionId, wanted, request);
      else await readReport(report, wanted, request);
      return request.live();
    } catch (error) { failRead(error, request); return false; }
    finally { if (valid(request.token, request.data) && request.serial === sequence) { current.opening = false; paint(); } }
  }
  function mergeRevision(revision) {
    const meta = { id: revision.report_id, scope: clone(revision.snapshot.scope), rule_version: revision.rule_version,
      current_revision_id: revision.id, latest_source_revision: revision.source_revision, updated_at: revision.created_at };
    current.allReports = [meta, ...current.allReports.filter((report) => report.id !== meta.id)];
    current.reports = current.allReports.filter((report) => sameScope(report.scope, revision.snapshot.scope));
    current.revisions = [revision, ...current.revisions.filter((item) => item.report_id === revision.report_id && item.id !== revision.id).map((item) => ({ ...item, is_current: false }))];
    current.historyPage = 0; current.selectedReportId = revision.report_id;
  }
  async function send(command) {
    const token = epoch, data = current;
    if (!active() || command.inFlight || data.command !== command) return false;
    command.inFlight = true; data.error = ""; data.message = "正在生成报告…"; paint();
    try {
      const revision = await api.generateReport(command.scope, command.key, command.prepared);
      if (!valid(token, data) || data.command !== command) return false;
      validateRevision(revision, command.scope);
      if (revision.source_revision !== command.prepared.expected_source_revision || revision.rule_version !== command.prepared.rule_version) throw new Error("报告来源或规则版本与生成请求不一致。");
      clearPreview(); data.revision = revision; mergeRevision(revision);
      data.command = null; data.message = "报告已保存。预览和下载均使用这个固定版本。";
      if (active()) {
        const request = beginRead(); paint();
        // Read the complete history, including revisions created in another session.
        try { await readReport(current.reports.find((report) => report.id === revision.report_id), command.scope, request); }
        catch (error) { failRead(error, request); }
        finally { if (request.live()) { data.opening = false; paint(); } }
        if (request.live() && onChanged) {
          try { await onChanged({ reportId: revision.report_id, revisionId: revision.id, scope: clone(command.scope) }); }
          catch { /* A host refresh cannot undo a confirmed report. */ }
        }
      }
      return true;
    } catch (error) {
      if (!valid(token, data) || data.command !== command) return false;
      data.message = "";
      if (unknownResult(error)) {
        command.inFlight = false;
        data.error = "生成结果尚未确认。请点击“用同一请求重试”核对原请求。";
      } else {
        data.command = null;
        data.error = error?.message || "报告生成失败，已保存版本仍保留。";
        if (Number(error?.status) === 409) data.source = null;
        if ([401, 403, 404].includes(Number(error?.status))) {
          clearPreview(); data.revision = null; data.revisions = []; data.reports = []; data.allReports = []; data.source = null;
        }
      }
      paint(); return false;
    }
  }
  async function generate(retry = false) {
    syncContext();
    if (!active() || current.loading || current.opening) return false;
    if (current.command) return retry && !current.command.inFlight ? send(current.command) : false;
    if (retry || !canCreate() || !current.rules || !current.source) return false;
    const wanted = safeScope();
    if (!wanted || !sameScope(current.source.scope, wanted)) return false;
    const command = { key: `report-${globalThis.crypto.randomUUID()}`, scope: clone(wanted), inFlight: false,
      prepared: { schema_version: current.rules.schema_version, rule_version: current.rules.default_rule_version, expected_source_revision: current.source.source_revision } };
    stopReads(); current.command = command;
    return send(command);
  }
  async function download(format) {
    syncContext();
    const data = current, revision = data.revision;
    if (!active() || busy() || data.downloading || !revision || !formats().includes(format)) return false;
    const token = epoch, serial = sequence, controller = new AbortController(); controllers.add(controller);
    const live = () => valid(token, data) && serial === sequence && active() && data.revision === revision;
    data.downloading = format; data.error = ""; paint();
    try {
      const result = await api.reportDownload(revision.report_id, format, revision.id, { signal: controller.signal, snapshotHash: revision.snapshot_sha256 });
      if (!live()) return false;
      const url = URL.createObjectURL(result.blob); downloadUrls.add(url);
      try {
        const link = document.createElement("a"); link.href = url; link.download = result.filename; link.rel = "noopener";
        document.body.appendChild(link); link.click(); link.remove();
      } finally { setTimeout(() => { URL.revokeObjectURL(url); downloadUrls.delete(url); }, 0); }
      return true;
    } catch (error) {
      if (live() && error?.name !== "AbortError") {
        data.error = error?.message || "下载未完成，请重试。";
        if ([401, 403, 404].includes(Number(error?.status))) failRead(error, { live, token, data });
      }
      return false;
    }
    finally { controllers.delete(controller); if (valid(token, data) && serial === sequence) { data.downloading = false; paint(); } }
  }
  function notice() {
    if (current.command) return `<div class="teacher-report-notice" role="status">${current.command.inFlight ? "正在生成报告，请等待结果。" : `生成结果尚未确认。<button type="button" class="teacher-report-button primary" data-report-action="retry">用同一请求重试</button>`}</div>`;
    if (current.error) return `<div class="teacher-report-notice teacher-report-error" role="alert">${esc(current.error)}</div>`;
    if (sourceChanged()) return '<div class="teacher-report-notice" role="status">学习证据已更新。当前报告仍保留原有内容，重新生成会保存新的修订。</div>';
    return current.message ? `<p class="teacher-report-notice" role="status">${esc(current.message)}</p>` : "";
  }
  function selectionHtml() {
    const disabled = busy() || state.loggingOut ? "disabled" : "";
    if (current.kind === "student") return `<label class="teacher-report-field">学生<select data-report-selection="student" ${disabled}><option value="">请选择学生</option>${current.members.map((member) => `<option value="${esc(member.user_id)}" ${member.user_id === current.studentId ? "selected" : ""}>${esc(member.display_name || member.username || member.user_id)}</option>`).join("")}</select></label>`;
    if (current.kind === "goal") return `<label class="teacher-report-field">学习目标<select data-report-selection="goal" ${disabled}><option value="">请选择学习目标</option>${current.goals.map((goal) => `<option value="${esc(goal.id)}" ${goal.id === current.goalId ? "selected" : ""}>${esc(goal.title || goal.id)}</option>`).join("")}</select></label>`;
    return `<p class="teacher-report-selection-note">${current.kind === "question_bank" ? "汇总整个课程题库的质量与使用情况，不限于当前班级。" : "汇总当前班级的学生、目标与已评分作答。"}</p>`;
  }
  function historyHtml() {
    if (!current.reports.length) return '<p class="teacher-report-muted">这个范围还没有已保存的报告。</p>';
    const start = current.historyPage * HISTORY_PAGE_SIZE;
    return `<div class="teacher-report-saved-list">${current.reports.map((report) => `<button type="button" class="teacher-report-button${current.selectedReportId === report.id ? " selected" : ""}" data-report-id="${esc(report.id)}" ${busy() || state.loggingOut ? "disabled" : ""}>${esc(SCOPES[report.scope.kind])} · ${esc(report.rule_version)} · 查看历史</button>`).join("")}</div>${current.revisions.length ? `<div class="teacher-report-revision-list">${current.revisions.slice(start, start + HISTORY_PAGE_SIZE).map((revision) => `<button type="button" class="teacher-report-revision${current.revision?.id === revision.id ? " selected" : ""}" data-report-id="${esc(revision.report_id)}" data-report-revision="${esc(revision.id)}" ${busy() || state.loggingOut ? "disabled" : ""}><strong>修订 ${esc(number(revision.revision_number))}${revision.is_current ? " · 当前" : ""}</strong><span>数据版本 ${esc(number(revision.source_revision))}</span><span>${esc(stamp(revision.created_at))}</span></button>`).join("")}</div>` : ""}${current.revisions.length > HISTORY_PAGE_SIZE ? `<div class="teacher-report-pagination"><button type="button" class="teacher-report-button" data-report-action="prev" ${start === 0 ? "disabled" : ""}>上一页</button><span>${current.historyPage + 1} / ${Math.ceil(current.revisions.length / HISTORY_PAGE_SIZE)}</span><button type="button" class="teacher-report-button" data-report-action="next" ${start + HISTORY_PAGE_SIZE >= current.revisions.length ? "disabled" : ""}>下一页</button></div>` : ""}`;
  }
  function tableHtml(snapshot, name, table) {
    const rows = list(table?.rows), columns = list(table?.columns);
    if (!columns.length) return "";
    return `<details class="teacher-report-table" ${["mastery", "question_usage"].includes(name) ? "open" : ""}><summary>${esc(TABLE_LABELS[name] || name)} · ${rows.length} 条</summary>${rows.length ? `<div class="teacher-report-table-scroll"><table><thead><tr>${columns.map((column) => `<th scope="col">${esc(column.label || column.key)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr>${columns.map((column) => `<td>${esc(valueText(column.key, row[column.key], snapshot))}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : '<p class="teacher-report-muted">暂无记录；未完成的作答不会计作零分。</p>'}</details>`;
  }
  function resultHtml() {
    const revision = current.revision;
    if (!revision) return `<section class="panel teacher-report-empty"><h2>${current.opening ? "正在读取报告版本…" : "查看或生成报告"}</h2><p>选择范围，查看已保存的历史版本，或从当前数据生成一份报告。</p></section>`;
    const snapshot = revision.snapshot;
    const metrics = Object.entries(snapshot.summary || {}).filter(([, value]) => Number.isFinite(value));
    return `<article class="panel teacher-report-result"><header class="teacher-report-result-head"><div><h2>${esc(snapshot.title || SCOPES[snapshot.scope.kind])}</h2><p class="teacher-report-muted">修订 ${esc(revision.revision_number)} · ${esc(stamp(revision.created_at))}<br>数据版本 ${esc(revision.source_revision)} · 规则 ${esc(revision.rule_version)}</p></div><span class="teacher-report-tag">已保存快照</span></header>${metrics.length ? `<dl class="teacher-report-metrics">${metrics.map(([key, value]) => `<div><dt>${esc(METRICS[key] || key)}</dt><dd>${esc(value)}</dd></div>`).join("")}</dl>` : ""}<section class="teacher-report-preview"><h3>报告预览</h3>${current.previewLoading ? '<p role="status">正在读取此版本的预览…</p>' : current.previewError ? `<p class="teacher-report-error" role="alert">${esc(current.previewError)}</p>` : current.previewHtml ? '<iframe data-report-preview-frame sandbox="" referrerpolicy="no-referrer" title="报告 HTML 预览"></iframe>' : '<p class="teacher-report-muted">暂无预览。</p>'}</section><section class="teacher-report-downloads"><h3>下载此版本</h3><p class="teacher-report-muted">所有格式使用上方同一份报告；以后改分不会改变已保存版本。</p><div class="teacher-report-download-buttons">${formats().map((format) => `<button type="button" class="teacher-report-button" data-report-download="${format}" ${busy() || current.downloading || state.loggingOut ? "disabled" : ""}>${current.downloading === format ? "下载中…" : FORMATS[format]}</button>`).join("")}</div></section>${Object.entries(snapshot.tables || {}).map(([name, table]) => tableHtml(snapshot, name, table)).join("")}</article>`;
  }
  function render() {
    syncContext();
    const heading = `<header class="teacher-report-heading"><p class="teacher-report-kicker">${esc(state.courseName || "当前课程")}${state.className ? ` / ${esc(state.className)}` : ""}</p><h1>阶段成果</h1><p class="teacher-report-muted">查看学习证据，保存报告，比较历史版本。</p></header>`;
    if (!session() || current.revoked) return `<section class="teacher-reports" data-teacher-reports>${heading}<p role="status">${current.revoked ? "当前已无权访问这些报告，请重新核验权限。" : "请连接正式服务并使用有效教师账号查看报告。"}</p></section>`;
    const locked = busy() || state.loggingOut;
    return `<section class="teacher-reports" data-teacher-reports>${heading}<section class="panel teacher-report-controls"><div class="teacher-report-scope-tabs" role="group" aria-label="报告范围">${Object.entries(SCOPES).map(([kind, label]) => `<button type="button" class="teacher-report-button${current.kind === kind ? " primary" : ""}" data-report-kind="${kind}" aria-pressed="${current.kind === kind}" ${locked ? "disabled" : ""}>${label}</button>`).join("")}</div><div class="teacher-report-selection">${selectionHtml()}<button type="button" class="teacher-report-button primary" data-report-action="generate" ${locked || current.loading || current.opening || !current.source || !canCreate() ? "disabled" : ""}>生成报告</button><button type="button" class="teacher-report-button" data-report-action="refresh" ${locked || current.loading ? "disabled" : ""}>${current.loading ? "读取中…" : "刷新数据"}</button></div><p class="teacher-report-scope-help">${current.kind === "question_bank" ? "课程级题库报告不包含个人作答原文或个人分数。" : "报告纳入所选范围内全部任务的已评分证据。未交、待评分和迟交状态清单不在本报告内；不按单次提交或日期筛选。"}</p>${current.loaded && !canCreate() ? '<p class="teacher-report-muted">当前权限可查看已有报告，生成报告需要相应管理权限。</p>' : ""}<dl class="teacher-report-source-meta"><div><dt>当前数据版本</dt><dd>${esc(number(current.source?.source_revision))}</dd></div><div><dt>规则版本</dt><dd>${esc(current.rules?.default_rule_version || "未读到")}</dd></div></dl>${notice()}</section><section class="panel teacher-report-stored"><h2>已存版本</h2>${historyHtml()}</section>${resultHtml()}</section>`;
  }
  function bind(container) {
    const surface = container?.matches?.("[data-teacher-reports]") ? container : container?.querySelector?.("[data-teacher-reports]");
    if (!surface) return;
    root = surface;
    const allowed = (event) => root === surface && surface.isConnected !== false && surface.contains(event.target) && active();
    const frame = surface.querySelector?.("[data-report-preview-frame]");
    if (frame && current.previewHtml && active()) {
      frame.setAttribute("sandbox", ""); frame.setAttribute("referrerpolicy", "no-referrer");
      frame.srcdoc = current.previewHtml;
    }
    if (bindings.has(surface)) return;
    bindings.add(surface);
    surface.addEventListener("click", (event) => {
      syncContext(); if (!allowed(event)) return;
      const target = event.target.closest?.("[data-report-action], [data-report-kind], [data-report-id], [data-report-download]");
      if (!target || target.disabled || target.matches?.(":disabled")) return;
      event.preventDefault();
      if (target.dataset.reportKind) return changeScope(target.dataset.reportKind);
      if (target.dataset.reportId) return openReport(target.dataset.reportId, target.dataset.reportRevision);
      if (target.dataset.reportDownload) return download(target.dataset.reportDownload);
      const action = target.dataset.reportAction;
      if (action === "generate" || action === "retry") return generate(action === "retry");
      if (action === "refresh" && !busy()) return load({ force: true });
      if (action === "prev") current.historyPage = Math.max(0, current.historyPage - 1);
      if (action === "next") current.historyPage = Math.min(Math.ceil(current.revisions.length / HISTORY_PAGE_SIZE) - 1, current.historyPage + 1);
      paint();
    });
    surface.addEventListener("change", (event) => {
      syncContext(); if (!allowed(event)) return;
      const target = event.target.closest?.("[data-report-selection]");
      if (target && !target.disabled) return changeScope(target.dataset.reportSelection, target.value);
    });
  }
  return { render, bind, load, reset, leave, openSelection };
}
