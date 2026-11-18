// Organizer inputs and frozen analyses stay scoped to the current authenticated course.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const list = (value) => Array.isArray(value) ? value : [];
const FIELDS = {
  title: "分析名称", id: "编号", student_id: "学生", student_ids: "学生编号", class_id: "班级", class_ids: "班级编号", node_id: "节点", node_ids: "测验节点", topic_id: "主题编号", topic_ids: "主题编号", topic: "主题", objective: "主题目标", indicators: "观察指标与材料", indicator_id: "指标编号", indicator_ids: "指标编号", indicator_label: "观察指标", label: "名称或内容", description: "说明", date: "日期", grade: "年级",
  available_questions: "题目数", current_events: "当前事件", scored_events: "有效评分事件", eligible_scored_events: "有效评分事件", earned_points: "得分合计", scored_max_points: "有效满分", score_percentage: "得分百分比", scored_point_weighted_percentage: "按分值加权百分比", scored_event_coverage: "有效评分覆盖率", event_coverage: "事件覆盖率", scored_question_coverage: "评分题目覆盖率", student_count_denominator: "人数分母", possible_event_denominator: "预期事件分母", expected_event_denominator: "预期事件分母", common_question_ids: "共同题目", common_question_count_denominator: "共同题数分母", class_equal_question_mean_percentage: "共同题等权百分比", group_kind: "范围类型", questions: "逐题数据", question_metrics: "共同题逐题统计",
  reason: "依据说明", action: "建议行动", sequence: "实施步骤", hint: "分步提示", observe: "后续观察", references: "相关材料与练习", evidence: "证据", evidence_refs: "证据定位", evidence_ref: "证据定位", status: "状态", role: "案例类型", selection: "选择方式", limitations: "解释边界", order: "顺序", filename: "文件名", sha256: "SHA256", bytes: "字节数", message: "说明", code: "问题类别", severity: "级别", refs: "原始记录位置", source: "出处", source_ref: "来源定位", mapping_note: "映射说明", material_mappings: "人工确认映射", material_id: "材料编号", material_status: "材料关联状态", mapping_status: "映射状态", mapping_actor_confirmed: "已由教师确认", mapping: "映射方式", kind: "材料类型", items: "要求清单",
  audience: "建议对象", focus: "学习重点", observation_plan: "观察计划", observations: "观察记录", analysis: "案例分析", recommendations: "学习建议", recommendation_ids: "关联建议编号", question_id: "题目编号", question_ids: "题目编号", final_answer: "学生最终作答", solution_process: "解答过程", rubric_scores: "评分点得分", rubric: "评分标准", rubric_observations: "评分点观察", question_type: "题型", score: "得分", max_score: "满分", question_max_score: "题目满分", conditions: "观察条件", record_fields: "应记录字段", success_criteria: "观察标准", eligible_events: "有效评分事件", eligible_event_denominator: "预期事件分母", students_with_evidence: "有评分证据的学生数", student_denominator: "学生人数分母", questions_available: "可用题数", partial_credit_events: "部分得分事件数", automatic: "自动建议", selection_reason: "选择依据",
  record_id: "记录编号", event_id: "作答事件编号", revision: "修订版本", source_id: "来源编号", file_sha256: "原文件 SHA256", table: "原始数据表", row: "原始行号", standard_ref: "课标整理编号", standard_page: "课标页码", textbook_page: "教材正文页", learning_material_ids: "学习卡编号", operator_mapped_textbook_ids: "教师确认教材编号",
  node_date: "测验日期", node_description: "节点说明", node_expected_question_count: "节点原定题数", selected_roster_count: "所选花名册人数", topics: "测量主题", recorded_events: "当前事件数", scored_event_denominator: "评分事件分母", coverage_note: "覆盖计算说明", class_comparison_method: "班级比较方法", cross_node_comparison: "跨节点可比", eligible_student_count: "有评分的学生数", eligible_event_count: "有效评分事件数", class_student_count_denominator: "班级人数分母",
  scoring: "评分计算", coverage: "覆盖计算", independence_and_assistance: "独立完成与帮助使用", comparability: "可比条件", mastery: "解释范围", recommendation_order: "个人建议排序", materials: "材料引用范围", follow_up: "后续观察", permission: "使用权限", permissions: "使用权限", allowed: "允许使用", note: "说明", notes: "说明", content: "内容", name: "名称", type: "类型", required: "必需", columns: "字段", schema_version: "结构版本", fields: "字段", format: "格式", provenance: "来源追踪", source_permissions: "来源与使用权限", input_schema: "输入结构", output_schema: "输出结构", input_sha256: "输入摘要", source_sha256: "来源摘要", created_at: "创建时间", confirmed_at: "冻结时间", confirmation_actor: "确认人", formula_version: "计算版本",
  fact: "可核对事实", recommendation_id: "关联建议编号", rubric_id: "评分点编号", excluded_current_evidence_refs: "异常当前记录定位", origin: "来源类别", publisher_verified: "已核验出版来源", license_status: "许可状态", external_processing_permission: "外部处理许可", redistribution_permission: "再分发许可", external_processing_performed: "已执行外部处理", field: "字段", meaning: "含义", envelope: "整体结构", sections: "成果分节", common_fields: "通用字段", links: "数据关联规则", csv: "CSV 格式", encoding: "编码", nested_fields: "嵌套字段", long_cells: "长单元格处理", original_values: "原始值保留", step: "步骤", instruction: "操作说明", run_mapping: "本次人工映射", material_refs: "材料来源", materials_used: "本次使用材料", operation_guide: "操作与交付说明",
};
const VALUES = { automatic: "自动建议", operator_selected: "教师手选确认", explicit_source_mapping: "来源明确关联", observed_question: "已有作答题目", individual: "学生个人", selected_cohort: "所选学生群体", class: "班级", selected_group: "自选组", evidence_insufficient: "证据不足", evidence_observed: "已有观察证据", partial_credit_observed: "观察到部分得分", rubric_shortfall_observed: "观察到评分点未满分", scored_evidence_observed: "已有有效评分证据", mapped_material_available: "已有确认关联的学习材料", pending_learning_material_mapping: "学习材料映射待确认", source_mapped: "来源已明确关联", pending_operator_mapping: "待教师确认映射", not_collected: "未采集", operator_uploaded_organizer_table: "操作者上传的学情数据表", info: "说明", warning: "需核对", error: "错误" };
const LABELS = {
  requirements_and_material_mappings: "学习要求与材料", individuals_by_topic_and_node: "个体表现", class_and_selected_group_performance: "班级与分组", comparable_class_metrics: "同节点共同题目比较", representative_cases: "代表学生案例", individual_learning_recommendations: "代表学生逐生学习建议", evidence_based_recommendations: "所选群体指标建议", node_coverage_and_limits: "测量范围与限制", methods: "计算方法", materials_used: "本次材料与实际出处", field_dictionary: "字段说明", data_notes: "数据说明", input_schema: "输入结构", output_schema: "输出结构", source_permissions: "来源与使用权限", operation_guide: "操作与交付说明", sources: "输入来源",
};
const label = (key) => FIELDS[key] || key;
const text = (value, key = "") => value == null || value === "" ? "未提供" : typeof value === "boolean" ? (value ? "是" : "否") : ["status", "selection", "mapping", "mapping_status", "material_status", "group_kind", "audience", "severity", "kind", "origin", "license_status", "external_processing_permission", "redistribution_permission"].includes(key) ? (VALUES[value] || String(value)) : String(value);

export function createTeacherCompetition({ api, state, render = () => {}, toast = () => {} }) {
  let root = null, context = "", epoch = 0, sequence = 0;
  const bindings = new WeakSet();
  const fresh = () => ({ schema: null, history: [], detail: null, title: "", mode: "files", files: [], texts: {}, nodes: [], classes: [], students: null, representatives: null, groups: [], mappings: Object.create(null), pages: {}, open: {}, error: "", busy: false, dirty: false, discard: false, confirmed: false, loaded: false, pendingCreate: null, recovery: "" });
  let current = fresh();
  const key = () => JSON.stringify([state.live, state.role, state.courseId, state.classId, state.teacherContextVersion, state.transitionId, api?.state?.user?.id]);
  const active = () => state.live && state.role === "teacher" && !state.loggingOut && Boolean(state.courseId);
  function sync() { if (context !== key()) { epoch += 1; sequence += 1; context = key(); current = fresh(); root = null; } }
  function valid(token) { return token === epoch && context === key() && active(); }
  function reset() { epoch += 1; sequence += 1; context = ""; current = fresh(); root = null; }
  function paint() {
    for (const node of root?.querySelectorAll?.("details[data-competition-detail]") || []) current.open[node.dataset.competitionDetail] = node.open;
    if (active()) render();
  }
  function leave() {
    sync();
    if (current.busy) { current.error = "正在处理请求，请等待结果后再离开。"; paint(); return false; }
    if (current.pendingCreate) { current.error = "新建结果尚未核实，请先按请求编号读取状态或安全重试。"; paint(); return false; }
    if (current.dirty) { current.discard = true; current.error = "本次输入或分析范围尚未保存。继续编辑，或明确丢弃后再离开。"; paint(); return false; }
    current.discard = false; return true;
  }
  globalThis.addEventListener?.("beforeunload", (event) => {
    if (active() && context === key() && (current.dirty || current.pendingCreate || current.busy)) { event.preventDefault(); event.returnValue = ""; }
  });
  async function load() {
    sync(); if (!active() || current.busy || current.loaded) return;
    const token = epoch, serial = ++sequence; current.busy = true; current.error = ""; paint();
    try {
      const [schema, history] = await Promise.all([api.call("GET", "/competition/schema"), api.call("GET", `/competition/runs?course_id=${encodeURIComponent(state.courseId)}`)]);
      if (!valid(token) || serial !== sequence) return;
      current.schema = schema; current.history = list(history.items); current.loaded = true;
    } catch (error) { if (valid(token)) current.error = error.message || "学情分析入口读取失败。"; }
    finally { if (valid(token) && serial === sequence) { current.busy = false; paint(); } }
  }
  const button = (action, title, disabled = false, primary = false) => `<button type="button" class="btn${primary ? " primary" : ""}" data-competition-action="${esc(action)}" ${disabled || current.busy ? "disabled" : ""}>${esc(title)}</button>`;
  const details = (key, title, content, open = false) => `<details data-competition-detail="${esc(key)}" ${(current.open[key] ?? open) ? "open" : ""}><summary>${esc(title)}</summary>${content}</details>`;
  function paged(values, key, draw, size = 40) {
    const rows = list(values), pages = Math.max(1, Math.ceil(rows.length / size));
    const page = Math.min(Math.max(0, Number(current.pages[key]) || 0), pages - 1); current.pages[key] = page;
    const from = page * size, visible = rows.slice(from, from + size);
    return draw(visible, from) + (pages > 1 ? `<nav class="competition-pager" aria-label="列表分页"><span>${from + 1}–${Math.min(from + size, rows.length)} / ${rows.length}</span><button type="button" class="btn" data-competition-page="${esc(key)}" data-page="${page - 1}" ${page === 0 || current.busy ? "disabled" : ""}>上一页</button><span>第 ${page + 1} / ${pages} 页</span><button type="button" class="btn" data-competition-page="${esc(key)}" data-page="${page + 1}" ${page + 1 >= pages || current.busy ? "disabled" : ""}>下一页</button></nav>` : "");
  }
  function human(value, key = "", path = "nested", depth = 0) {
    if (value == null || value === "") return '<span class="muted">未提供</span>';
    if (typeof value !== "object") {
      if (typeof value === "number" && ["event_coverage", "scored_event_coverage", "scored_question_coverage"].includes(key)) return `${esc(Number((value * 100).toFixed(2)))}%`;
      if (typeof value === "number" && key.endsWith("percentage")) return `${esc(value)}%`;
      return esc(text(value, key));
    }
    if (depth > 9) return details(path, "更多层级数据（原文）", `<pre>${esc(JSON.stringify(value, null, 2))}</pre>`);
    if (Array.isArray(value)) {
      if (!value.length) return '<span class="muted">无</span>';
      const body = paged(value, path, (rows, from) => `<ol class="competition-evidence-list" start="${from + 1}">${rows.map((item, index) => `<li>${human(item, key, `${path}.${from + index}`, depth + 1)}</li>`).join("")}</ol>`, 12);
      return details(path, `${label(key) || "记录"}（${value.length} 项）`, body);
    }
    const entries = Object.entries(value);
    if (!entries.length) return '<span class="muted">无</span>';
    return paged(entries, `${path}.fields`, (rows) => `<dl class="competition-kv">${rows.map(([name, item]) => `<div><dt>${esc(label(name))}</dt><dd>${human(item, name, `${path}.${name}`, depth + 1)}</dd></div>`).join("")}</dl>`);
  }
  function table(values, columns, path) {
    const rows = list(values);
    if (!rows.length) return '<p class="muted">本次范围没有相应记录。</p>';
    const keys = columns || [...new Set(rows.flatMap((row) => Object.keys(row || {})))];
    return paged(rows, path, (page, from) => `<div class="competition-scroll" tabindex="0" role="region" aria-label="${esc(path.startsWith("preview") ? "输入数据明细" : "分析数据明细")}"><table><thead><tr>${keys.map((name) => `<th scope="col">${esc(label(name))}</th>`).join("")}</tr></thead><tbody>${page.map((row, index) => `<tr>${keys.map((name) => `<td>${human(row?.[name], name, `${path}.${from + index}.${name}`)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
  }
  const options = () => current.detail?.preview?.options || {};
  function studentsInClasses() { return list(options().students).filter((student) => current.classes.includes(student.class_id)); }
  function selectedStudents() { return studentsInClasses().filter((student) => current.students == null || current.students.includes(student.id)); }
  function materials() {
    const loaded = new Map(list(options().materials).map((material) => [material.id, material]));
    return list(options().unmapped_textbooks).map((material) => ({ ...material, ...loaded.get(material.id) }));
  }
  function indicatorsFor(topic, material) {
    return list(options().indicators).filter((item) => item.selectable !== false && item.topic_id === topic && (!list(material?.indicator_ids).length || material.indicator_ids.includes(item.id)));
  }
  const mapped = () => Object.values(current.mappings);
  function dirty() { current.dirty = true; current.confirmed = false; current.discard = false; current.error = ""; }
  function renderInput() {
    const tables = list(current.schema?.tables), limits = current.schema?.limits || {};
    return `<section><h2>1. 选择本次输入</h2><p class="muted">每次导入新的学情数据，保留原值、来源和输入摘要。已有分析不会覆盖；导入记录不会写成平台学生的新作答。</p><label class="field">分析名称<input data-competition-field="title" maxlength="160" value="${esc(current.title)}" required></label><label class="field">输入方式<select data-competition-field="mode"><option value="files" ${current.mode === "files" ? "selected" : ""}>选择 CSV 或 Excel 文件</option><option value="paste" ${current.mode === "paste" ? "selected" : ""}>粘贴 CSV 原文</option></select></label>${current.mode === "files" ? `<label class="field">学情数据文件<input type="file" multiple accept=".csv,.xlsx" data-competition-field="files"></label><p>${current.files.length ? `已保留 ${current.files.length} 份文件：${current.files.map((file) => esc(file.name)).join("、")}` : "选择学情数据 CSV 表格，或包含对应工作表的 Excel 文件。"}</p><p class="muted">最多 ${esc(limits.max_files || 8)} 份文件，单文件 ${esc((limits.max_file_bytes || 12582912) / 1048576)} MB，合计 ${esc((limits.max_total_bytes || 25165824) / 1048576)} MB。</p>` : tables.map((entry) => details(`input.${entry.filename}`, `${entry.filename}${entry.required ? "（必需）" : "（可选）"}`, `<label class="field">${esc(entry.filename)} 原文<textarea data-competition-field="csv" data-competition-table="${esc(entry.filename)}" rows="5" spellcheck="false">${esc(current.texts[entry.filename] || "")}</textarea></label><p class="muted">表头：${list(entry.columns).map(esc).join("、")}</p>`, entry.required)).join("")}<div class="competition-actions"><button class="btn primary" type="submit">检查输入并保存预览</button></div></section>`;
  }
  function choices(items, field, selected, path, description = (item) => item.description || item.class_id || "") {
    if (!items.length) return '<p class="muted">本次范围没有可选记录。</p>';
    return paged(items, path, (rows) => `<div class="competition-options">${rows.map((item) => `<label><input type="checkbox" data-competition-field="${esc(field)}" value="${esc(item.id)}" ${selected.includes(item.id) ? "checked" : ""} ${item.selectable === false || current.busy ? "disabled" : ""}><span><strong>${esc(item.id)}</strong>${description(item) ? `<small>${esc(description(item))}</small>` : ""}</span></label>`).join("")}</div>`, 60);
  }
  function scopeErrors() {
    const roster = selectedStudents(), allowed = new Set(roster.map((student) => student.id)), allInClasses = new Set(studentsInClasses().map((student) => student.id));
    const errors = [];
    if (!current.nodes.length || !current.classes.length || !roster.length) errors.push("至少选择一个测验节点、班级和学生。");
    if (current.students?.some((id) => !allInClasses.has(id))) errors.push("学生子集中有编号不属于已选班级，请移除超范围编号或调整班级。");
    if (current.representatives && (current.representatives.length > 3 || current.representatives.some((id) => !allowed.has(id)))) errors.push("手选代表必须在当前学生范围内，且最多三名。");
    const groupIds = new Set();
    for (const group of current.groups) {
      if (!/^[^\s\x00-\x1f]{1,120}$/.test(group.id) || groupIds.has(group.id) || list(options().classes).some((item) => item.id === group.id) || !group.label.trim() || group.label.trim().length > 100 || /[\x00-\x1f]/.test(group.label) || !group.student_ids.length || group.student_ids.some((id) => !allowed.has(id))) { errors.push("每个自选组需唯一编号、名称及至少一名当前范围内学生；组编号不能与班级编号相同。"); break; }
      groupIds.add(group.id);
    }
    const known = new Map(materials().map((material) => [material.id, material]));
    for (const mapping of mapped()) {
      const material = known.get(mapping.material_id);
      if (!material || material.selectable === false || !mapping.topic_id || (material.topic_id && material.topic_id !== mapping.topic_id) || !mapping.indicator_ids.length || mapping.indicator_ids.some((id) => !indicatorsFor(mapping.topic_id, material).some((indicator) => indicator.id === id))) { errors.push("每项人工映射需选择有效主题及至少一个对应指标，且不能覆盖原材料已声明的关联。"); break; }
    }
    return errors;
  }
  function groupEditor() {
    const allowed = new Set(selectedStudents().map((student) => student.id));
    return paged(current.groups, "scope.groups", (groups, from) => groups.map((group, offset) => {
      const index = from + offset;
      const members = list(options().students).filter((student) => allowed.has(student.id) || group.student_ids.includes(student.id));
      return `<article class="competition-group"><header><h4>自选组 ${index + 1}</h4>${button(`remove-group:${index}`, "移除此组")}</header><div class="competition-two-fields"><label class="field">组编号<input data-competition-field="group-id" data-group-index="${index}" maxlength="120" value="${esc(group.id)}" placeholder="填写本次自选组的唯一编号"></label><label class="field">组名称<input data-competition-field="group-label" data-group-index="${index}" maxlength="100" value="${esc(group.label)}"></label></div>${details(`scope.group.${index}`, `选择组员（已选 ${group.student_ids.length} 人）`, choices(members, `group-members:${index}`, group.student_ids, `scope.group.${index}.members`, (student) => `${student.class_id}${allowed.has(student.id) ? "" : " · 已超出学生范围，请取消"}`))}</article>`;
    }).join(""), 10) + `<div class="competition-actions">${button("add-group", "添加自选组", current.groups.length >= 100)}</div>`;
  }
  function mappingEditor() {
    const candidates = materials(), allTopics = [...new Set(list(options().indicators).map((item) => item.topic_id))];
    const measuredTopics = new Set(list(options().nodes).filter((node) => current.nodes.includes(node.id)).flatMap((node) => list(node.topic_ids)));
    return `<p class="muted">以下教材尚未明确关联指标。核对原件后选择主题和观察指标；不勾选的候选材料保持未映射。已有来源关联的学习卡与题目会沿用原始关联。</p>${candidates.length ? paged(candidates, "scope.materials", (rows) => rows.map((material) => {
      const mapping = current.mappings[material.id];
      const topics = material.topic_id ? allTopics.filter((topic) => topic === material.topic_id) : allTopics;
      const indicators = mapping ? indicatorsFor(mapping.topic_id, material) : [];
      return `<article class="competition-material"><header><div><h4>${esc(material.id)}</h4><p>${esc(material.label || "未提供材料内容")}</p><p class="muted">${esc(material.source || "未提供出处")}${material.mapping_status ? ` · ${esc(text(material.mapping_status, "status"))}` : ""}</p></div><label class="competition-toggle"><input type="checkbox" data-competition-field="mapping-active" data-material-id="${esc(material.id)}" ${mapping ? "checked" : ""} ${current.busy || material.selectable === false ? "disabled" : ""}>人工确认映射</label></header>${material.source_ref ? details(`mapping.${material.id}.source`, "查看来源定位", human(material.source_ref, "source_ref", `mapping.${material.id}.source.value`)) : ""}${mapping ? `<label class="field">主题<select data-competition-field="mapping-topic" data-material-id="${esc(material.id)}"><option value="">选择导入的主题</option>${topics.map((topic) => `<option value="${esc(topic)}" ${mapping.topic_id === topic ? "selected" : ""}>${esc(topic)}${measuredTopics.has(topic) ? "（所选节点测量）" : "（所选节点未测量）"}</option>`).join("")}</select></label><fieldset class="competition-indicators" ${!mapping.topic_id ? "disabled" : ""}><legend>确认观察指标（至少一项）</legend>${indicators.length ? paged(indicators, `mapping.${material.id}.indicators`, (items) => items.map((indicator) => `<label><input type="checkbox" data-competition-field="mapping-indicator" data-material-id="${esc(material.id)}" value="${esc(indicator.id)}" ${mapping.indicator_ids.includes(indicator.id) ? "checked" : ""}>${esc(indicator.id)} · ${esc(indicator.label)}</label>`).join("")) : '<p class="muted">请先选择有效主题。</p>'}</fieldset>` : ""}</article>`;
    }).join(""), 10) : '<p class="muted">本次导入没有待人工映射的教材候选。</p>'}${list(options().materials).length ? details("scope.material-catalog", "查看全部导入材料与来源关联", table(options().materials, ["id", "kind", "topic_id", "indicator_ids", "label", "mapping_status", "source", "source_ref"], "scope.material-catalog.rows")) : ""}`;
  }
  function scopeSummary() {
    const roster = selectedStudents(), selected = new Set(roster.map((student) => student.id));
    const summary = [
      { label: "测验节点", message: current.nodes },
      { label: "班级与纳入人数", message: current.classes.map((id) => ({ class_id: id, student_count_denominator: roster.filter((student) => student.class_id === id).length })) },
      { label: `学生范围：${roster.length} 人${current.students == null ? "（所选班级全部学生）" : "（指定学生子集）"}`, message: roster.map((student) => ({ student_id: student.id, class_id: student.class_id })) },
      { label: "代表学生", message: current.representatives == null ? "按最终范围自动建议，最多三名；冻结时重算并保存实际人选。" : current.representatives.length ? current.representatives.map((id) => ({ student_id: id, status: selected.has(id) ? "已在分析范围" : "超出分析范围，请修正" })) : "明确不选择代表案例" },
      { label: `自选组：${current.groups.length} 组`, message: current.groups },
      { label: `人工教材映射：${mapped().length} 项`, message: mapped() },
    ];
    return `<div class="competition-scope-summary">${table(summary, ["label", "message"], "scope.summary")}</div>`;
  }
  function preview() {
    const p = current.detail.preview || {}, summary = p.summary || {}, op = p.options || {};
    const frozen = current.detail.run.status === "confirmed", roster = selectedStudents();
    const reps = current.representatives || [], allowed = new Set(roster.map((student) => student.id));
    const errors = frozen ? [] : scopeErrors();
    const studentChoices = list(op.students).filter((student) => current.classes.includes(student.class_id) || current.students?.includes(student.id));
    const repChoices = list(op.students).filter((student) => allowed.has(student.id) || reps.includes(student.id));
    const suggestions = list(op.auto_suggestions?.length ? op.auto_suggestions : p.auto_suggestions);
    return `<section><h2>2. 核对数据与确认范围</h2><p>学生 ${esc(summary.students)} · 班级 ${esc(summary.classes)} · 当前事件 ${esc(summary.current_events)} · 有效已评分 ${esc(summary.eligible_scored_events)} · 历史修订 ${esc(summary.superseded_records)} · 排除或未评分 ${esc(summary.excluded_records)}</p>${table(Object.entries(summary.table_rows || {}).map(([name, count]) => ({ label: name, current_events: count })), ["label", "current_events"], "preview.counts")}${details("preview.sources", "输入文件与摘要", table(p.sources, ["filename", "bytes", "sha256"], "preview.sources.rows"))}${details("preview.issues", `问题清单（${list(p.issues).length}）`, table(p.issues, ["code", "severity", "message", "refs"], "preview.issues.rows"))}${details("preview.dictionary", "数据说明与字段字典", table(p.notes, undefined, "preview.notes") + table(p.field_dictionary, undefined, "preview.dictionary.rows"))}${frozen ? '<p class="competition-frozen-note">本次范围已冻结。实际学生、代表、分组和映射见成果中的冻结范围。</p>' : `<fieldset ${current.busy ? "disabled" : ""}><h3>测量节点</h3>${choices(list(op.nodes), "nodes", current.nodes, "scope.nodes", (node) => [node.date, node.description, `测量主题：${list(node.topic_ids).join("、")}`].filter(Boolean).join(" · "))}<h3>分析班级</h3>${choices(list(op.classes), "classes", current.classes, "scope.classes", (item) => `${item.student_count} 名导入学生`)}<h3>学生范围</h3><label class="field">选择方式<select data-competition-field="student-mode"><option value="all" ${current.students == null ? "selected" : ""}>纳入所选班级全部学生</option><option value="subset" ${current.students != null ? "selected" : ""}>只纳入勾选的学生</option></select></label>${choices(studentChoices, "students", current.students == null ? roster.map((student) => student.id) : current.students, "scope.students", (student) => `${student.class_id}${current.classes.includes(student.class_id) ? "" : " · 超出已选班级，请取消"}`)}<h3>自选组</h3><p class="muted">在当前学生范围内选组员，组可以跨所选班级。组统计沿用相同节点题集。</p>${groupEditor()}<h3>教材映射</h3>${mappingEditor()}<h3>代表学生（最多三名）</h3><label class="field">选择方式<select data-competition-field="representative-mode"><option value="automatic" ${current.representatives == null ? "selected" : ""}>按最终范围自动建议</option><option value="manual" ${current.representatives != null ? "selected" : ""}>手动选择或明确不选</option></select></label>${current.representatives == null ? `<p class="muted">初始导入范围建议：${esc(suggestions.map((item) => item.student_id).join("、") || "尚无建议")}。变更节点或学生范围后会在冻结时重算，不将初始建议当作最终人选。</p>` : `<p class="muted">手选编号会跨页面重绘保留。范围变化后，超出范围的人选需取消或调整学生范围。</p>${choices(repChoices, "representatives", reps, "scope.representatives", (student) => `${student.class_id}${allowed.has(student.id) ? "" : " · 超出学生范围，请取消"}`)}`}<h3>确认前核对完整范围</h3>${scopeSummary()}${errors.length ? `<ul class="competition-inline-error">${errors.map((error) => `<li>${esc(error)}</li>`).join("")}</ul>` : ""}<label class="competition-confirm"><input type="checkbox" data-competition-field="confirmed" ${current.confirmed ? "checked" : ""}>我已核对来源、问题清单和上述范围，确认冻结本次结果。</label><div class="competition-actions">${button("confirm", "确认范围并生成成果", !current.confirmed || errors.length > 0, true)}</div></fieldset>`}</section>`;
  }
  function recommendation(item, path) {
    return `<article class="competition-recommendation"><header><div><h4>${esc(item.student_id || "指标建议")} · ${esc(item.indicator_label || item.indicator_id || "学习建议")}</h4><p>${[item.class_id, item.node_id, item.topic_id, item.id].filter(Boolean).map(esc).join(" · ")}</p></div><span>${esc(text(item.status, "status"))}</span></header><p><strong>依据：</strong>${esc(item.reason || "未提供")}</p><p><strong>行动：</strong>${esc(item.action || "未提供")}</p><div class="competition-recommendation-grid"><div><h5>实施步骤</h5>${human(item.sequence, "sequence", `${path}.sequence`)}</div><div><h5>分步提示</h5>${human(item.hint, "hint", `${path}.hint`)}</div><div><h5>后续观察</h5>${human(item.observe, "observe", `${path}.observe`)}</div></div>${details(`${path}.detail`, "证据、材料与观察计划", human({ evidence: item.evidence, references: item.references, focus: item.focus, material_status: item.material_status, observation_plan: item.observation_plan }, "", `${path}.details`))}</article>`;
  }
  function cases(rows) {
    if (!rows.length) return '<p class="muted">本次范围未选择代表学生。</p>';
    return paged(rows, "results.cases", (items, from) => items.map((item, index) => {
      const path = `case.${from + index}`;
      return `<article class="competition-case"><header><div><h3>${esc(item.student_id)}</h3><p>${esc(item.class_id)} · ${esc(item.role || "代表案例")}</p></div><span>案例 ${from + index + 1}</span></header><p>${esc(item.reason || item.selection_reason || "")}</p>${item.analysis != null ? `<h4>案例分析</h4>${human(item.analysis, "analysis", `${path}.analysis`)}` : ""}<h4>作答与评分证据（${list(item.evidence).length} 条）</h4>${paged(list(item.evidence), `${path}.evidence`, (evidence, start) => evidence.map((record, offset) => `<article class="competition-answer"><h5>${esc(record.node_id)} · ${esc(record.question_id)} · ${esc(record.question_type || "未提供题型")}</h5><p><strong>状态：</strong>${esc(text(record.status, "status"))}　<strong>得分：</strong>${esc(text(record.score))} / ${esc(text(record.question_max_score))}</p><dl class="competition-answer-text"><div><dt>学生最终作答</dt><dd>${esc(text(record.final_answer))}</dd></div><div><dt>解答过程</dt><dd>${esc(text(record.solution_process))}</dd></div></dl>${details(`${path}.record.${start + offset}`, "核对评分点与原始证据定位", human({ rubric_scores: record.rubric_scores, rubric: record.rubric, evidence_ref: record.evidence_ref }, "", `${path}.record.${start + offset}.value`))}</article>`).join(""), 8)}${list(item.recommendation_ids).length ? `<p><strong>关联建议编号：</strong>${human(item.recommendation_ids, "recommendation_ids", `${path}.ids`)}</p>` : ""}${list(item.recommendations).length ? `<h4>对应学生学习建议</h4>${paged(item.recommendations, `${path}.recommendations`, (recs, start) => recs.map((rec, offset) => recommendation(rec, `${path}.recommendation.${start + offset}`)).join(""), 8)}` : ""}${item.observations != null ? `<h4>后续观察</h4>${human(item.observations, "observations", `${path}.observations`)}` : ""}${list(item.excluded_current_evidence_refs).length ? details(`${path}.excluded-evidence`, `异常当前记录定位（${item.excluded_current_evidence_refs.length} 条，不用于诊断）`, human(item.excluded_current_evidence_refs, "excluded_current_evidence_refs", `${path}.excluded-evidence.value`)) : ""}${item.limitations != null ? `<h4>解释边界</h4>${human(item.limitations, "limitations", `${path}.limitations`)}` : ""}</article>`;
    }).join(""), 3);
  }
  function results() {
    const result = current.detail?.result; if (!result) return "";
    const summary = result.summary || {}, sections = result.sections || {}, saved = result.scope || {};
    const mappingRows = Array.isArray(saved.material_mappings) ? saved.material_mappings : Object.entries(saved.material_mappings || {}).map(([material_id, row]) => ({ material_id, ...row }));
    const scopeRows = [{ label: "冻结节点", message: saved.node_ids }, { label: "冻结班级", message: saved.class_ids }, { label: `学生：${list(saved.student_ids).length} 人`, message: saved.student_ids }, { label: "实际代表学生", message: saved.representative_student_ids }, { label: `自选组：${list(saved.groups).length} 组`, message: saved.groups }, { label: `人工教材映射：${mappingRows.length} 项`, message: mappingRows }];
    const rendered = Object.entries(LABELS).map(([name, title]) => {
      const section = sections[name]; let body;
      if (name === "requirements_and_material_mappings") {
        const mappings = Array.isArray(section?.material_mappings) ? section.material_mappings : Object.entries(section?.material_mappings || {}).map(([material_id, row]) => ({ material_id, ...row }));
        body = table(list(section?.items), ["topic_id", "topic", "objective", "indicators"], "results.requirements") + `<p>${esc(section?.mapping_note || "")}</p><h4>已确认的人工映射</h4>` + table(mappings, ["material_id", "topic_id", "indicator_ids", "mapping_actor_confirmed", "source_ref"], "results.mappings") + details("results.unmapped", "原始待确认教材候选", table(list(section?.unmapped_textbook_candidates), ["id", "label", "source"], "results.unmapped.rows"));
      } else if (name === "representative_cases") body = cases(list(section));
      else if (["individual_learning_recommendations", "evidence_based_recommendations"].includes(name)) body = list(section).length ? paged(section, `results.${name}`, (rows, from) => rows.map((item, index) => recommendation(item, `results.${name}.${from + index}`)).join(""), 8) : '<p class="muted">本次范围没有相应学习建议。</p>';
      else if (name === "methods") body = human(section || {}, "", "results.methods.value");
      else {
        const columns = name === "individuals_by_topic_and_node" ? ["student_id", "class_id", "node_id", "topic", "scored_events", "expected_event_denominator", "earned_points", "scored_max_points", "score_percentage", "indicators", "evidence_refs"]
          : name === "class_and_selected_group_performance" ? ["id", "label", "group_kind", "student_ids", "node_id", "student_count_denominator", "eligible_scored_events", "possible_event_denominator", "earned_points", "scored_max_points", "scored_point_weighted_percentage", "scored_event_coverage", "questions", "evidence_refs"]
            : name === "comparable_class_metrics" ? ["node_id", "class_id", "common_question_ids", "common_question_count_denominator", "student_count_denominator", "possible_event_denominator", "current_events", "scored_events", "event_coverage", "scored_event_coverage", "class_equal_question_mean_percentage", "question_metrics", "evidence_refs"] : undefined;
        body = Array.isArray(section) ? table(section, columns, `results.${name}.rows`) : human(section, name, `results.${name}.value`);
      }
      return details(`results.${name}`, title + (Array.isArray(section) ? `（${section.length}）` : ""), body, ["requirements_and_material_mappings", "representative_cases", "individual_learning_recommendations"].includes(name));
    }).join("");
    return `<section><h2>3. 复核成果与导出</h2><p>当前范围 ${esc(summary.scope?.student_count_denominator)} 人 · ${esc(summary.current_event_count)} 个事件 · ${esc(summary.eligible_scored_event_count)} 个有效评分事件</p><h3>冻结的完整范围</h3>${table(scopeRows, ["label", "message"], "results.scope")}<p class="muted">统计由程序按来源数据计算。独立完成、帮助使用及具体错因未由分数推断；建议以现有记录为依据，不作为掌握度或因果效果结论。</p><div class="competition-actions">${["json", "zip", "html"].map((format) => button(`download:${format}`, `下载 ${format === "zip" ? "CSV 与说明 ZIP" : format.toUpperCase()}`)).join("")}</div>${rendered}</section>`;
  }
  function recovery() {
    if (!current.pendingCreate) return "";
    return `<section class="competition-recovery" role="status"><h2>新建结果待核对</h2><p>原输入与请求编号已保留。先读取保存状态；仍未找到时可安全重试同一请求。核对完成前不能修改输入或另建分析。</p><p><strong>请求编号：</strong><code>${esc(current.pendingCreate.requestId)}</code></p>${current.recovery ? `<p>${esc(current.recovery)}</p>` : ""}<div class="competition-actions">${button("recover-create", "按请求编号读取状态")}${button("retry-create", "用原请求安全重试", false, true)}</div></section>`;
  }
  function view() {
    sync();
    if (!active()) return '<section data-teacher-competition><h1 class="page">学情分析</h1><p>请连接正式服务并选择有权限的课程。</p></section>';
    return `<section data-teacher-competition><header class="competition-header"><div><h1 class="page">学情分析</h1><p>新输入 → 问题与范围确认 → 分析成果 → 保存导出</p></div><span>${esc(state.courseId)}</span></header>${current.error ? `<p role="alert" class="competition-error">${esc(current.error)}</p>` : ""}${current.discard ? `<div class="competition-discard"><p>选择继续编辑，或明确丢弃尚未保存的内容。</p><div class="competition-actions">${button("keep", "继续编辑")}${button("discard", "丢弃未保存内容")}</div></div>` : ""}${current.busy ? '<p role="status">正在处理，请勿重复提交…</p>' : ""}${recovery()}<div class="competition-actions">${button("new", "新建分析", Boolean(current.pendingCreate))}${button("refresh", "刷新分析记录", Boolean(current.pendingCreate))}</div>${details("history", `分析记录（${current.history.length}）`, current.history.length ? paged(current.history, "history.rows", (rows) => `<div class="competition-history-list">${rows.map((run) => `<div>${button(`open:${run.id}`, `${run.title} · ${run.status === "confirmed" ? "已冻结" : "待确认"}`, Boolean(current.pendingCreate))}<span>${esc(run.created_at || "")}</span></div>`).join("")}</div>`, 20) : '<p class="muted">当前课程暂无分析记录。</p>')}${current.detail ? `<p class="competition-run">${esc(current.detail.run.title)} · ${esc(current.detail.run.id)}</p>${preview()}${results()}` : `<form data-competition-form><fieldset ${current.busy || current.pendingCreate ? "disabled" : ""}>${renderInput()}</fieldset></form>`}</section>`;
  }
  async function command(action) {
    sync(); if (!active() || current.busy) return;
    const token = epoch; current.busy = true; current.error = ""; paint();
    try { await action(token); }
    catch (error) { if (valid(token)) current.error = error.message || "处理失败，请保留输入后重试。"; }
    finally { if (valid(token)) { current.busy = false; paint(); } }
  }
  function accept(detail, newRun = false) {
    if (detail?.run?.course_id !== state.courseId || !detail.preview || !detail.run.id) throw new Error("服务返回的课程或分析记录不一致。");
    current.detail = detail; current.dirty = false; current.discard = false; current.confirmed = false; current.pendingCreate = null; current.recovery = "";
    current.history = [detail.run, ...current.history.filter((row) => row.id !== detail.run.id)]; current.pages = {}; current.open = {};
    const saved = detail.result?.scope;
    current.nodes = saved ? list(saved.node_ids) : list(detail.preview.options?.nodes).filter((node) => node.selectable !== false).map((node) => node.id);
    current.classes = saved ? list(saved.class_ids) : list(detail.preview.options?.classes).map((item) => item.id);
    current.students = saved ? list(saved.student_ids) : null; current.representatives = saved ? list(saved.representative_student_ids) : null;
    current.groups = saved ? list(saved.groups).map((group) => ({ id: group.id, label: group.label, student_ids: [...list(group.student_ids)] })) : [];
    const mappings = saved?.material_mappings || {};
    current.mappings = Array.isArray(mappings) ? Object.fromEntries(mappings.map((mapping) => [mapping.material_id, { ...mapping, indicator_ids: [...list(mapping.indicator_ids)] }])) : Object.fromEntries(Object.entries(mappings).map(([material_id, mapping]) => [material_id, { material_id, topic_id: mapping.topic_id, indicator_ids: [...list(mapping.indicator_ids)] }]));
    Object.setPrototypeOf(current.mappings, null);
    if (newRun) { current.files = []; current.texts = {}; current.title = ""; }
  }
  function makeRequest() {
    if (!current.title.trim()) throw new Error("请填写分析名称。");
    if (!current.schema) throw new Error("请先读取输入格式，再保存预览。");
    if (current.mode === "files") {
      const limits = current.schema.limits || {};
      if (!current.files.length) throw new Error("请选择学情数据文件。");
      if (current.files.length > (limits.max_files || 8) || current.files.some((file) => !/\.(csv|xlsx)$/i.test(file.name))) throw new Error("请选择数量符合限制的 CSV 或 XLSX 文件。");
      if (new Set(current.files.map((file) => file.name)).size !== current.files.length) throw new Error("不能上传同名文件，请核对后重新选择。");
      if (current.files.some((file) => file.size > (limits.max_file_bytes || 12582912)) || current.files.reduce((sum, file) => sum + file.size, 0) > (limits.max_total_bytes || 25165824)) throw new Error("单文件或合计大小超过限制，请调整文件后重试。");
    } else if (list(current.schema.tables).filter((entry) => entry.required).some((entry) => !String(current.texts[entry.filename] || "").trim())) throw new Error("请粘贴全部必需表格的原文。");
    const random = globalThis.crypto;
    const requestId = random?.randomUUID ? random.randomUUID() : random?.getRandomValues ? [...random.getRandomValues(new Uint8Array(16))].map((byte) => byte.toString(16).padStart(2, "0")).join("") : null;
    if (!requestId) throw new Error("浏览器无法生成安全请求编号，请在支持安全随机数的浏览器中重试。");
    return { requestId, courseId: state.courseId, title: current.title.trim(), mode: current.mode, files: [...current.files], texts: Object.fromEntries(Object.entries(current.texts).filter(([, value]) => value.trim())) };
  }
  async function postRequest(pending, token) {
    let detail;
    if (pending.mode === "files") {
      const form = new FormData(); form.append("course_id", pending.courseId); form.append("title", pending.title); form.append("request_id", pending.requestId); pending.files.forEach((file) => form.append("files", file));
      detail = await api.upload("/competition/runs/upload", form);
    } else detail = await api.call("POST", "/competition/runs", { course_id: pending.courseId, title: pending.title, request_id: pending.requestId, csv_texts: pending.texts });
    if (valid(token)) { accept(detail, true); toast("输入与预览已保存，请核对范围后确认。"); }
  }
  async function recoverRequest(pending, token) {
    try {
      const detail = await api.call("GET", `/competition/runs/by-request/${encodeURIComponent(pending.requestId)}?course_id=${encodeURIComponent(pending.courseId)}`);
      if (valid(token)) { accept(detail, true); toast("已按原请求编号读取到保存结果。"); }
      return true;
    } catch (error) {
      if (valid(token)) current.recovery = error.status === 404 ? "尚未找到已保存结果。可用相同请求编号和原输入安全重试。" : `读取状态未完成：${error.message || "服务未返回明确结果"}。请稍后重读或重试原请求。`;
      return false;
    }
  }
  async function create(token, retry = false) {
    if (retry) {
      const pending = current.pendingCreate;
      if (!pending || pending.courseId !== state.courseId) throw new Error("请返回原请求对应课程核对创建状态。");
      if (await recoverRequest(pending, token) || !valid(token)) return;
      try { await postRequest(pending, token); }
      catch (error) { if (valid(token)) { current.pendingCreate = pending; current.recovery = "原请求重试未得到明确成功结果。请求编号及原输入仍保留，请继续读取状态。"; } throw error; }
    } else {
      const pending = makeRequest(); current.pendingCreate = pending; current.dirty = true; current.recovery = "";
      try { await postRequest(pending, token); }
      catch (error) {
        if (!valid(token)) return;
        if ([400, 401, 403, 404, 413, 415, 422].includes(error.status)) { current.pendingCreate = null; throw error; }
        current.recovery = "创建结果不确定，保留原请求编号与输入。";
        if (!(await recoverRequest(pending, token))) throw new Error(`${error.message || "创建响应未收到"}。请按请求编号核对或安全重试原请求。`);
      }
    }
  }
  async function download(format, token) {
    const detail = current.detail, run = detail.run, expectedType = { json: "application/json", html: "text/html", zip: "application/zip" }[format];
    if (!expectedType || run.status !== "confirmed") throw new Error("请先冻结分析，再选择支持的导出格式。");
    const response = await fetch(`/api/v1/competition/runs/${encodeURIComponent(run.id)}/export/${format}`, { credentials: "same-origin", headers: { Accept: expectedType } });
    if (!response.ok) { let error; try { error = await response.json(); } catch { /* Preserve fixed failure text. */ } throw new Error(error?.detail?.message || "导出失败，请重新读取分析记录。"); }
    if ((response.headers.get("Content-Type") || "").split(";")[0].trim().toLowerCase() !== expectedType) throw new Error("导出文件格式与请求不一致，文件未下载。");
    if (response.headers.get("X-Competition-Run-Id") !== run.id || response.headers.get("X-Competition-Input-SHA256") !== detail.preview.input_sha256) throw new Error("导出分析记录与输入摘要不匹配，文件未下载。");
    const bytes = await response.arrayBuffer(), expected = response.headers.get("X-Content-SHA256");
    const hash = [...new Uint8Array(await crypto.subtle.digest("SHA-256", bytes))].map((byte) => byte.toString(16).padStart(2, "0")).join("");
    if (hash !== expected) throw new Error("导出内容摘要不匹配，文件未下载。");
    if (!valid(token)) return;
    const blob = new Blob([bytes], { type: expectedType }), url = URL.createObjectURL(blob), link = root.ownerDocument.createElement("a");
    link.href = url; link.download = `competition-${run.id}.${format}`; link.hidden = true; root.ownerDocument.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    toast("已下载，请重新打开并核对本次范围与结果。");
  }
  function discard() {
    const detail = current.detail;
    current.files = []; current.texts = {}; current.title = ""; current.error = ""; current.discard = false;
    if (detail) accept(detail); else { current.dirty = false; current.confirmed = false; }
  }
  function bind(host) {
    root = host?.matches?.("[data-teacher-competition]") ? host : host?.querySelector?.("[data-teacher-competition]");
    if (!root || bindings.has(root)) return; bindings.add(root);
    root.addEventListener("input", field);
    root.addEventListener("change", field);
    root.addEventListener("submit", (event) => { if (!event.target.closest?.("[data-competition-form]")) return; event.preventDefault(); if (!current.pendingCreate) command((token) => create(token)); });
    root.addEventListener("click", (event) => {
      const page = event.target.closest?.("[data-competition-page]");
      if (page && !page.disabled && !current.busy && page.dataset.competitionPage) { current.pages[page.dataset.competitionPage] = Number(page.dataset.page); paint(); return; }
      const node = event.target.closest?.("[data-competition-action]"); if (!node || !node.dataset.competitionAction || node.disabled || current.busy) return;
      const action = node.dataset.competitionAction;
      if (action === "keep") { current.discard = false; current.error = ""; paint(); }
      else if (action === "discard") { if (!current.pendingCreate) { discard(); paint(); } }
      else if (["new", "refresh"].includes(action) || action.startsWith("open:")) {
        if (current.pendingCreate) { current.error = "请先按原请求编号核对创建状态。"; paint(); return; }
        if (current.dirty) { current.discard = true; current.error = "请先保存或明确丢弃未保存输入与分析范围，再切换分析。"; paint(); return; }
        if (action === "new") { const schema = current.schema, history = current.history, loaded = current.loaded; current = fresh(); Object.assign(current, { schema, history, loaded }); paint(); }
        else if (action === "refresh") { current.loaded = false; load(); }
        else command(async (token) => { const detail = await api.call("GET", `/competition/runs/${encodeURIComponent(action.slice(5))}`); if (valid(token)) accept(detail); });
      } else if (action === "add-group") { if (current.groups.length < 100 && current.detail?.run.status !== "confirmed") { current.groups.push({ id: "", label: "", student_ids: [] }); dirty(); paint(); } }
      else if (action.startsWith("remove-group:")) { const index = Number(action.slice(13)); if (current.groups[index] && current.detail?.run.status !== "confirmed") { current.groups.splice(index, 1); dirty(); paint(); } }
      else if (action === "recover-create") command((token) => current.pendingCreate && recoverRequest(current.pendingCreate, token));
      else if (action === "retry-create") command((token) => create(token, true));
      else if (action === "confirm") command(async (token) => {
        if (!current.confirmed || current.detail?.run.status === "confirmed") throw new Error("请先核对范围并勾选确认。");
        const errors = scopeErrors(); if (errors.length) throw new Error(errors.join(" "));
        const scope = { node_ids: [...current.nodes], class_ids: [...current.classes], student_ids: selectedStudents().map((student) => student.id), groups: current.groups.map((group) => ({ id: group.id, label: group.label.trim(), student_ids: [...group.student_ids] })), material_mappings: mapped().map(({ material_id, topic_id, indicator_ids }) => ({ material_id, topic_id, indicator_ids: [...indicator_ids] })) };
        if (current.representatives != null) scope.representative_student_ids = [...current.representatives];
        const detail = await api.call("POST", `/competition/runs/${encodeURIComponent(current.detail.run.id)}/confirm`, { expected_revision: current.detail.run.revision, scope });
        if (valid(token)) { accept(detail); toast("范围与成果已冻结，后续可从分析记录读取。"); }
      });
      else if (action.startsWith("download:")) command((token) => download(action.slice(9), token));
    });
  }
  function field(event) {
    const node = event.target.closest?.("[data-competition-field]"); if (!node || current.busy || current.pendingCreate) return;
    const name = node.dataset.competitionField; if (!name) return;
    if (event.type === "input" && (node.type === "checkbox" || node.type === "file" || node.tagName === "SELECT")) return;
    if (current.detail?.run.status === "confirmed") return;
    if (["nodes", "classes", "students", "representatives"].includes(name) || name.startsWith("group-members:")) {
      if (name === "students") {
        const values = new Set(current.students == null ? selectedStudents().map((student) => student.id) : current.students); node.checked ? values.add(node.value) : values.delete(node.value); current.students = [...values];
      } else if (name.startsWith("group-members:")) {
        const group = current.groups[Number(name.slice(14))]; if (!group) return;
        const values = new Set(group.student_ids); node.checked ? values.add(node.value) : values.delete(node.value); group.student_ids = [...values];
      } else { const values = new Set(current[name] || []); node.checked ? values.add(node.value) : values.delete(node.value); current[name] = [...values]; }
      dirty(); if (event.type === "change") paint();
    } else if (name === "student-mode") { current.students = node.value === "all" ? null : studentsInClasses().map((student) => student.id); dirty(); paint(); }
    else if (name === "representative-mode") { current.representatives = node.value === "automatic" ? null : []; dirty(); paint(); }
    else if (name === "confirmed") { current.confirmed = node.checked; if (event.type === "change") paint(); }
    else if (name === "mapping-active") {
      const material = materials().find((item) => item.id === node.dataset.materialId); if (!material || material.selectable === false) return;
      if (node.checked) current.mappings[material.id] = { material_id: material.id, topic_id: material.topic_id || "", indicator_ids: [...list(material.indicator_ids)] }; else delete current.mappings[material.id];
      dirty(); paint();
    } else if (name === "mapping-topic") {
      const mapping = current.mappings[node.dataset.materialId]; if (!mapping) return;
      mapping.topic_id = node.value; mapping.indicator_ids = []; dirty(); paint();
    } else if (name === "mapping-indicator") {
      const mapping = current.mappings[node.dataset.materialId]; if (!mapping) return;
      const values = new Set(mapping.indicator_ids); node.checked ? values.add(node.value) : values.delete(node.value); mapping.indicator_ids = [...values]; dirty(); if (event.type === "change") paint();
    } else if (["group-id", "group-label"].includes(name)) {
      const group = current.groups[Number(node.dataset.groupIndex)]; if (!group) return; group[name === "group-id" ? "id" : "label"] = node.value; dirty(); if (event.type === "change") paint();
    } else if (name === "files") { current.files = Array.from(node.files || []); dirty(); if (event.type === "change") paint(); }
    else if (name === "csv") { current.texts[node.dataset.competitionTable] = node.value; dirty(); }
    else if (["title", "mode"].includes(name)) { current[name] = node.value; dirty(); if (name === "mode" && event.type === "change") paint(); }
  }
  return { render: view, bind, load, reset, leave };
}
