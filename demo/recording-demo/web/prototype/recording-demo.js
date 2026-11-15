// Optional recording tools. Nothing is mounted until the server explicitly enables demo mode.
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
const list = (value) => Array.isArray(value) ? value : value?.items || [];
const requestId = () => `recording-${crypto.randomUUID()}`;

export function learningSuggestion(task, attempt) {
  if (!task?.answer_released || typeof attempt?.total_score !== "number" || !(attempt.max_score > 0)) return null;
  const items = new Map((attempt.items || []).map((item) => [item.question_id, item]));
  const missed = (task.questions || []).flatMap((question, index) => {
    const score = items.get(question.id)?.score;
    return typeof score === "number" && score < question.points ? [index + 1] : [];
  });
  const ratio = attempt.total_score / attempt.max_score;
  return {
    summary: missed.length ? `第 ${missed.join("、")} 题还有失分，先对照自己的作答定位从哪一步开始不同。`
      : "这次已评分题目完成较好。换一道同类题独立完成，才能继续观察方法是否稳定。",
    next: ratio < 0.5 ? "先复习等式两边同做运算与分配律。每次只改一步，再把结果代回原式检查。"
      : ratio < 1 ? "重做失分题时写出中间步骤，重点检查去括号、移项符号和最后的代入检验。"
        : "下一次可尝试含括号或分数系数的变式题，说明每一步为什么保持等式成立。",
    observation: "后续观察：下一轮先独立尝试，比较相同知识点的失分位置与提示使用情况。本次分数不能单独说明长期掌握程度。",
  };
}

export function createRecordingDemo({ api, getContext, onChange, beforeSwitch, onReset, onError }) {
  let status = null, root = null, busy = false, error = "", compact = true, steps = false, epoch = 0;
  const hints = new Map(), discussions = new Map();
  const chat = { messages: [], draft: "", busy: false, error: "" };
  const enabled = () => status?.enabled === true;
  const context = () => getContext?.() || {};
  const tell = (message) => onError?.(message);
  const hintKey = (task, question) => `${api.state.user?.id}:${task?.id}:${question?.id}`;
  const hintState = (task, question) => {
    const key = hintKey(task, question);
    if (!hints.has(key)) hints.set(key, { open: false, messages: [], draft: "", busy: false, error: "" });
    return hints.get(key);
  };

  async function refreshStatus({ silent = false } = {}) {
    try {
      const next = await api.call("GET", "/api/demo/status");
      status = next?.enabled === true ? next : null;
      mount();
      onChange?.(status);
      return status;
    } catch (failure) {
      if (failure.status === 404 || !status) {
        status = null;
        root?.remove(); root = null;
        document.body.classList.remove("recording-enabled");
      } else if (!silent) tell(failure.message || "演示状态读取失败");
      return null;
    }
  }

  function mount() {
    if (!enabled()) {
      root?.remove(); root = null;
      document.body.classList.remove("recording-enabled");
      return;
    }
    if (!root) {
      root = document.createElement("aside");
      root.id = "recording-demo";
      root.setAttribute("aria-label", "演示工具");
      document.body.prepend(root);
      root.addEventListener("click", toolbarAction);
    }
    document.body.classList.add("recording-enabled");
    const user = api.state.user;
    root.innerHTML = `<div class="recording-bar"><span class="recording-indicator">演示环境</span><span class="recording-current">${esc(user?.display_name || "选择演示身份")}</span><span class="recording-truth">真实学习记录 · 辅导来源单独标注</span><button type="button" data-recording-action="toggle" aria-expanded="${!compact}" aria-controls="recording-tools">${compact ? "演示工具" : "收起"}</button></div>
      ${!compact ? `<div id="recording-tools" class="recording-tools"><div class="recording-personas" aria-label="切换演示身份">${list(status.personas).map((person) => `<button type="button" data-recording-persona="${esc(person.key)}" aria-pressed="${user?.id ? user.id === (person.id || person.user_id) || user.display_name === person.name : false}" ${busy ? "disabled" : ""}>${esc(person.role === "teacher" ? "教师 " : "学生 ")}${esc(person.name)}</button>`).join("")}</div><div class="recording-commands"><button type="button" data-recording-action="new-round" ${busy || user?.role !== "teacher" ? "disabled" : ""}>${busy ? "正在同步…" : "开始新一轮"}</button><button type="button" data-recording-action="steps" aria-expanded="${steps}">操作步骤</button></div><p class="recording-note">新一轮保留已有成绩和短评；由教师创建。切换身份前先保存当前作答。</p>${steps ? `<ol class="recording-steps"><li>教师查看目标，开始新一轮。</li><li>林夏完成独立短诊，可标记“暂时不会”。</li><li>正式作答先写一步，再打开演示辅导，继续追问。</li><li>交卷查看真实评分与演示建议。</li><li>切换周予展示独立作答，再回教师查看依据、发送短评。</li><li>学生回复短评，教师生成并下载阶段报告。</li></ol>` : ""}${error ? `<p class="recording-error" role="alert">${esc(error)}</p>` : ""}</div>` : ""}`;
  }

  async function toolbarAction(event) {
    const button = event.target.closest("button");
    if (!button || button.disabled) return;
    const action = button.dataset.recordingAction;
    if (action === "toggle") { compact = !compact; mount(); return; }
    if (action === "steps") { steps = !steps; mount(); return; }
    if (busy) return;
    const persona = button.dataset.recordingPersona;
    if (!persona && action !== "new-round") return;
    busy = true; error = ""; mount();
    try {
      if (await beforeSwitch?.() === false) throw new Error("当前作答尚未保存，请处理后再切换。");
      if (persona) {
        await api.call("POST", "/api/demo/login", { persona });
        onReset?.();
        window.location.reload();
        return;
      }
      const response = await api.call("POST", "/api/demo/new-round", {});
      const next = response?.status?.enabled === true ? response.status : response;
      if (next?.enabled === true) status = next;
      reset(); onReset?.();
      if (!await refreshStatus()) throw new Error("新一轮已创建，但状态未读取完成。请刷新页面查看，勿重复创建。");
      window.location.reload();
    } catch (failure) {
      error = failure.message || "操作没有完成，请重试";
      tell(error);
    } finally { busy = false; mount(); }
  }

  function renderHint(task, question, locked = false) {
    if (!enabled() || !task || !question || task.diagnostic_required || context().page === "diagnostic") return "";
    const item = hintState(task, question);
    return `<section class="recording-hint" data-recording-hint><div class="recording-panel-head"><h3>演示辅导</h3><span class="recording-label">逐步提示 · 不代替作答</span></div>${item.open ? `<p class="recording-note">先尝试并保存本题。辅导依据真实作答状态开放，使用模拟脚本时会明确标注。</p><div class="recording-messages" role="log" aria-live="polite">${item.messages.map((message) => `<article class="recording-message ${message.role === "user" ? "is-user" : ""}"><strong>${message.role === "user" ? "我的追问" : message.source === "simulation" ? "演示辅导 / 模拟" : "演示辅导 / 在线"}</strong><p>${esc(message.text)}</p></article>`).join("")}</div>${item.error ? `<p class="recording-error" role="alert">${esc(item.error)}</p>` : ""}<form data-recording-hint-form><label for="recording-hint-input">卡在哪一步？</label><textarea id="recording-hint-input" name="message" maxlength="1200" rows="2" ${item.busy || locked ? "disabled" : ""} placeholder="例如：我展开括号后，不确定下一步怎么做">${esc(item.draft)}</textarea><div class="recording-commands"><button type="submit" class="btn" ${item.busy || locked ? "disabled" : ""}>${item.busy ? "正在获取提示" : item.messages.length ? "继续追问" : "要一步提示"}</button><button type="button" class="btn" data-recording-hint-close>返回作答</button></div></form>` : `<p class="recording-note">先独立尝试，卡住时再展开。收起辅导不会清空当前作答。</p><button type="button" class="btn" data-recording-hint-open ${locked ? "disabled" : ""}>展开演示辅导</button>`}</section>`;
  }

  function openHint(task, question) {
    if (!enabled() || task?.diagnostic_required || context().page === "diagnostic" || !question) return;
    hintState(task, question).open = true;
    onChange?.(status);
    document.querySelector("[data-recording-hint]")?.scrollIntoView({ block: "nearest", behavior: "auto" });
  }

  async function sendHint(task, question, message) {
    const item = hintState(task, question);
    if (item.busy || !enabled()) return;
    const generation = epoch;
    item.busy = true; item.error = ""; item.draft = message;
    onChange?.(status);
    try {
      if (await context().saveDraft?.() === false) throw new Error("请先保存本题的尝试，再获取提示。");
      if (generation !== epoch) return;
      const reply = await api.call("POST", "/api/demo/hint", { task_id: task.id, question_id: question.id, message: message.trim() || (item.messages.length ? "请继续给我下一步提示" : "我已经尝试了，请给我一步提示") });
      if (generation !== epoch) return;
      if (!reply?.text || !["simulation", "live"].includes(reply.source)) throw new Error("辅导没有返回有效内容，请重试。");
      item.messages.push({ role: "user", text: message.trim() || "请给我一步提示" }, { role: "assistant", text: reply.text, source: reply.source });
      item.draft = "";
    } catch (failure) { if (generation === epoch) item.error = failure.message || "提示获取失败，当前作答仍保留。"; }
    finally { if (generation === epoch) { item.busy = false; onChange?.(status); } }
  }

  function simulationChatEnabled() { return enabled() && status.capabilities?.chat === "simulation"; }

  function renderChat() {
    if (!simulationChatEnabled()) return "";
    return `<section class="recording-hint" data-recording-chat><div class="recording-panel-head"><h2>学伴对话</h2><span class="recording-label">演示学伴 / 模拟</span></div><p class="recording-note">一次性返回模拟学习建议，不会显示虚构的生成进度。此处对话仅保留在当前页面，任务提示请从作答页进入。</p><div class="recording-messages" role="log" aria-live="polite">${chat.messages.map((message) => `<article class="recording-message ${message.role === "user" ? "is-user" : ""}"><strong>${message.role === "user" ? "我" : "演示学伴 / 模拟"}</strong><p>${esc(message.text)}</p></article>`).join("")}</div>${chat.error ? `<p class="recording-error" role="alert">${esc(chat.error)}</p>` : ""}<form data-recording-chat-form><label for="recording-chat-input">想一起想清楚什么？</label><textarea id="recording-chat-input" name="message" rows="3" required maxlength="2000" ${chat.busy ? "disabled" : ""} placeholder="例如：怎样检查一元一次方程的解？">${esc(chat.draft)}</textarea><div class="recording-commands"><button type="submit" class="btn primary" ${chat.busy ? "disabled" : ""}>${chat.busy ? "正在请求" : "发送"}</button></div></form></section>`;
  }

  async function sendChat() {
    if (chat.busy || !chat.draft.trim() || !simulationChatEnabled()) return;
    const generation = epoch, text = chat.draft.trim();
    chat.busy = true; chat.error = ""; onChange?.(status);
    try {
      const reply = await api.call("POST", "/api/demo/chat", { message: text });
      if (generation !== epoch) return;
      if (!reply?.text || reply.source !== "simulation") throw new Error("模拟学伴未返回有效内容，请重试。");
      chat.messages.push({ role: "user", text }, { role: "assistant", text: reply.text });
      chat.draft = "";
    } catch (failure) { if (generation === epoch) chat.error = failure.message || "发送未成功，文字已保留。"; }
    finally { if (generation === epoch) { chat.busy = false; onChange?.(status); } }
  }

  function renderFeedback(task, attempt) {
    if (!enabled()) return "";
    const advice = learningSuggestion(task, attempt);
    if (!advice) return "";
    return `<section class="recording-feedback"><div class="recording-panel-head"><h2>接下来怎样学</h2><span class="recording-label">演示建议</span></div><p>${esc(advice.summary)}</p><p>${esc(advice.next)}</p><p class="recording-note">${esc(advice.observation)}</p></section>`;
  }

  function discussionState(taskId) {
    const key = `${api.state.user?.id}:${taskId}`;
    if (!discussions.has(key)) discussions.set(key, { taskId, loaded: false, busy: false, threads: [], error: "", draft: "", questionId: "", pending: null });
    return discussions.get(key);
  }

  function renderDiscussion(taskId, role = api.state.user?.role) {
    if (!enabled() || !taskId) return `<p class="muted">完成本轮作答后，可以围绕真实题目发送短评。</p>`;
    const item = discussionState(taskId);
    return `<section class="panel recording-discussion" data-recording-discussion="${esc(taskId)}"><div class="recording-panel-head"><h3>${role === "teacher" ? "教师短评" : "与老师交流"}</h3><button type="button" class="linkish" data-recording-discussion-refresh ${item.busy ? "disabled" : ""}>刷新短评</button></div>${!item.loaded && !item.error ? '<p class="muted">正在读取真实短评…</p>' : ""}${item.error ? `<p class="recording-error" role="alert">${esc(item.error)}</p>` : ""}<div class="recording-messages">${item.threads.map((thread) => `<article><strong>${esc(thread.title || "教师短评")}</strong>${list(thread.messages).map((message) => `<div class="recording-message ${message.author_role === "student" ? "is-user" : ""}"><strong>${message.author_role === "teacher" ? "老师" : "学生"}</strong><p>${esc(message.redacted ? message.notice || "评分完成后开放短评内容" : message.text)}</p></div>`).join("")}</article>`).join("") || (item.loaded ? '<p class="muted">还没有短评。发送后双方可在这里继续交流。</p>' : "")}</div>${role === "teacher" || item.threads.length ? `<form data-recording-discussion-form><label class="field">${role === "teacher" ? "围绕这次作答写短评" : "回复老师"}<textarea name="comment" required maxlength="2000" rows="3" ${item.busy ? "disabled" : ""} placeholder="写下具体的观察或下一步计划">${esc(item.draft)}</textarea></label>${role === "teacher" && item.task?.questions?.length ? `<label class="field">关联题目<select name="question_id">${item.task.questions.map((question, index) => `<option value="${esc(question.id)}" ${question.id === item.questionId ? "selected" : ""}>第 ${index + 1} 题 · ${esc(question.stem.slice(0, 40))}</option>`).join("")}</select></label>` : ""}<button type="submit" class="btn primary" ${item.busy || !item.loaded ? "disabled" : ""}>${item.busy ? "正在保存" : role === "teacher" ? "发送短评" : "回复老师"}</button></form>` : ""}</section>`;
  }

  function paintDiscussion(item) {
    const node = Array.from(document.querySelectorAll("[data-recording-discussion]")).find((entry) => entry.dataset.recordingDiscussion === item.taskId);
    if (!node) return;
    node.outerHTML = renderDiscussion(item.taskId);
    bindDiscussions();
  }

  async function loadDiscussion(item) {
    if (item.busy) return;
    const generation = epoch;
    item.busy = true; item.error = "";
    try {
      const query = new URLSearchParams({ task_id: item.taskId });
      if (api.state.user?.role === "teacher") query.set("class_id", status.class_id);
      const [threads, task] = await Promise.all([api.call("GET", `/companion/discussion-threads?${query}`), api.task(item.taskId)]);
      const details = await Promise.all(list(threads).map((thread) => api.call("GET", `/companion/discussion-threads/${encodeURIComponent(thread.id)}`)));
      if (generation !== epoch) return;
      item.threads = details; item.task = task; item.loaded = true;
      item.questionId ||= task.questions?.[0]?.id || "";
    } catch (failure) { if (generation === epoch) item.error = failure.message || "短评读取失败，请刷新重试。"; }
    finally { if (generation === epoch) { item.busy = false; paintDiscussion(item); } }
  }

  async function sendDiscussion(item) {
    if (item.busy || !item.draft.trim()) return;
    const generation = epoch;
    item.busy = true; item.error = ""; paintDiscussion(item);
    try {
      const text = item.draft.trim();
      const teacher = api.state.user?.role === "teacher";
      const attempt = item.task?.attempts?.at(-1);
      const question = item.task?.questions?.find((entry) => entry.id === item.questionId);
      if (teacher && (!attempt || !question)) throw new Error("学生还没有提交本轮作答，完成提交后再发送题目短评。");
      const signature = JSON.stringify([text, item.questionId, attempt?.id, attempt?.revision]);
      if (!item.pending || item.pending.signature !== signature) item.pending = { signature, createId: requestId(), sendId: requestId(), thread: null };
      const pending = item.pending;
      let thread = pending.thread || (teacher ? item.threads.find((entry) => entry.attempt_id === attempt.id && entry.question_version_id === `${question.id}:${question.version}` && entry.grading_revision === attempt.revision) : item.threads.at(-1));
      if (!thread && teacher) thread = await api.call("POST", "/companion/discussion-threads", { task_id: item.taskId, attempt_id: attempt.id, question_version_id: `${question.id}:${question.version}`, grading_revision: attempt.revision, title: "本轮学习短评", request_id: pending.createId });
      if (generation !== epoch) return;
      if (!thread?.id) throw new Error("没有可回复的短评，请刷新后重试。");
      pending.thread = thread;
      const message = await api.call("POST", `/companion/discussion-threads/${encodeURIComponent(thread.id)}/messages`, { text, request_id: pending.sendId });
      if (generation !== epoch) return;
      if (!message?.id) throw new Error("短评保存结果未确认，请重试。");
      item.draft = ""; item.pending = null;
      tell(teacher ? "短评已保存，没有改动学生答案" : "回复已保存，老师可以看到");
    } catch (failure) { if (generation === epoch) item.error = failure.message || "发送未确认，文字已保留，可重试。"; }
    finally {
      if (generation === epoch) {
        item.busy = false; paintDiscussion(item);
        if (!item.error) loadDiscussion(item);
      }
    }
  }

  function bindDiscussions() {
    document.querySelectorAll("[data-recording-discussion]").forEach((node) => {
      if (node.dataset.bound) return;
      node.dataset.bound = "true";
      const item = discussionState(node.dataset.recordingDiscussion);
      node.querySelector("[data-recording-discussion-refresh]")?.addEventListener("click", () => loadDiscussion(item));
      node.querySelector("textarea")?.addEventListener("input", (event) => { item.draft = event.target.value; });
      node.querySelector("select")?.addEventListener("change", (event) => { item.questionId = event.target.value; });
      node.querySelector("form")?.addEventListener("submit", (event) => { event.preventDefault(); sendDiscussion(item); });
      if (!item.loaded && !item.busy && !item.error) loadDiscussion(item);
    });
  }

  function bind() {
    if (!enabled()) return;
    const { task, question, locked } = context();
    const node = document.querySelector("[data-recording-hint]");
    if (node && !node.dataset.bound && task && question) {
      node.dataset.bound = "true";
      const item = hintState(task, question);
      node.querySelector("[data-recording-hint-open]")?.addEventListener("click", () => openHint(task, question));
      node.querySelector("[data-recording-hint-close]")?.addEventListener("click", () => { item.open = false; onChange?.(status); });
      node.querySelector("textarea")?.addEventListener("input", (event) => { item.draft = event.target.value; });
      node.querySelector("form")?.addEventListener("submit", (event) => { event.preventDefault(); if (!locked) sendHint(task, question, item.draft); });
    }
    const chatNode = document.querySelector("[data-recording-chat]");
    if (chatNode && !chatNode.dataset.bound) {
      chatNode.dataset.bound = "true";
      chatNode.querySelector("textarea")?.addEventListener("input", (event) => { chat.draft = event.target.value; });
      chatNode.querySelector("form")?.addEventListener("submit", (event) => { event.preventDefault(); sendChat(); });
    }
    bindDiscussions();
    mount();
  }

  function reset() {
    epoch += 1; hints.clear(); discussions.clear();
    Object.assign(chat, { messages: [], draft: "", busy: false, error: "" });
  }
  return { get status() { return status; }, enabled, refreshStatus, mount, bind, reset, renderHint, openHint, renderFeedback, renderDiscussion, simulationChatEnabled, renderChat };
}
