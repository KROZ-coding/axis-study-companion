/* 统计与建议来自服务端；界面更新保留输入、焦点和阅读位置。 */
(function (XB) {
  'use strict';

  const $ = (selector, root = document) => root.querySelector(selector);
  const el = (tag, attrs, ...children) => {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value == null || value === false) continue;
      if (key === 'class') node.className = value;
      else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
    children.flat().forEach((child) => {
      if (child != null && child !== false) node.append(child.nodeType ? child : document.createTextNode(child));
    });
    return node;
  };
  const status = (key) => el('span', { class: `st st-${key}` }, XB.STATUS[key].label);
  const focusKey = (key) => ({ 'data-focus': key });
  const state = {
    role: null, view: 'entry', sid: XB.ME, kp: 'K5',
    matTopic: 'all', matType: 'all', matQuery: '', matId: null, matPage: 0,
    clsTopic: 'T2', clsStatus: 'weak', clsQuery: '', openSid: XB.ME,
    demo: 'normal', answers: { ...XB.answers }, saving: false, reduce: false,
  };
  const NAV = {
    teacher: [['班级学情', 'class'], ['学生诊断', 'student'], ['学习材料', 'materials'], ['个人报告', 'report']],
    student: [['我的诊断', 'student'], ['建议材料', 'materials'], ['我的报告', 'report']],
  };
  const main = $('#main');
  const systemMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  let manualReduce = false;
  try { manualReduce = localStorage.getItem('study-reduce-motion') === 'true'; } catch (_) { /* Storage may be unavailable. */ }
  let keyboardInput = false;
  let chromeKey = '';
  let entryPlayed = false;
  let toastTimer;
  const animated = new Set();

  document.addEventListener('keydown', () => { keyboardInput = true; }, true);
  document.addEventListener('pointerdown', () => { keyboardInput = false; }, true);

  function reveal(node, distance = 5, duration = 200) {
    if (!node || state.reduce || keyboardInput || !node.animate) return;
    node.getAnimations().forEach((animation) => animation.cancel());
    const animation = node.animate([
      { opacity: 0, transform: `translateY(${distance}px)` },
      { opacity: 1, transform: 'translateY(0)' },
    ], { duration, easing: 'cubic-bezier(0.23,1,0.32,1)' });
    animated.add(animation);
    animation.finished.catch(() => {}).finally(() => animated.delete(animation));
  }

  function toast(text) {
    $('#toast').textContent = text;
    $('#toast').classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => $('#toast').classList.remove('show'), 2800);
  }

  function preserve(update) {
    const active = document.activeElement;
    const key = active?.dataset.focus;
    const top = main.scrollTop;
    const pageTop = window.scrollY;
    update();
    if (key && !active.isConnected) {
      [...main.querySelectorAll('[data-focus]')].find((node) => node.dataset.focus === key)?.focus({ preventScroll: true });
    }
    main.scrollTop = top;
    if (window.scrollY !== pageTop) window.scrollTo({ top: pageTop, behavior: 'instant' });
  }

  function searchField(label, key, value, placeholder, update) {
    let composing = false;
    const input = el('input', {
      type: 'search', value, placeholder, ...focusKey(key),
      oncompositionstart: () => { composing = true; },
      oncompositionend: (event) => { composing = false; update(event.target.value); },
      oninput: (event) => { if (!composing && !event.isComposing) update(event.target.value); },
    });
    return el('label', { class: 'search-field' }, el('span', {}, label), input);
  }

  function ruleDisclosure() {
    return el('details', { class: 'rule-details' }, el('summary', {}, '判断依据'), el('p', {}, XB.RULE));
  }

  function exportControl() {
    const params = new URLSearchParams(state.view === 'class' ? { topic: state.clsTopic } : { student_id: state.sid });
    return el('details', { class: 'export-control' }, el('summary', {}, '导出成果'),
      el('div', { class: 'export-panel' },
        el('p', { class: 'muted' }, state.view === 'class' ? '当前主题 · 全班学生' : '当前学生 · 全部主题'),
        ...['JSON', 'CSV'].map((format) => el('a', {
          href: `/api/study/export.${format.toLowerCase()}?${params}`, download: `study-results.${format.toLowerCase()}`,
        }, `下载 ${format}`))));
  }

  function viewHeader(title, description, action) {
    return el('header', { class: 'view-head' },
      el('div', {}, el('h1', { class: 'view-title' }, title), el('p', { class: 'view-meta' }, description)), action);
  }

  function renderChrome() {
    $('#top-role').textContent = state.role === 'teacher' ? '教师视角' : `学生视角 · ${XB.studentById[state.sid].name}`;
    $('#demo-role').textContent = state.role === 'teacher' ? '切换到学生' : '切换到教师';
    const key = `${state.role}:${state.sid}`;
    if (chromeKey !== key) {
      chromeKey = key;
      $('#sidenav').replaceChildren(el('p', { class: 'nav-group' }, state.role === 'teacher' ? XB.CLASS_NAME : '我的学习'),
        ...NAV[state.role].map(([label, view]) => el('button', {
          class: 'nav-item', 'data-view': view, onclick: () => go(view),
        }, label)));
    }
    $('#sidenav').querySelectorAll('[data-view]').forEach((button) => {
      if (button.dataset.view === state.view) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    });
  }

  function stateBlock() {
    if (state.demo === 'loading') return el('section', { class: 'state', role: 'status' },
      el('h1', {}, '正在读取记录'), el('div', { class: 'skeleton', 'aria-hidden': 'true' }, el('i'), el('i'), el('i')),
      el('p', { class: 'muted' }, '演示状态，可在设置中切回正常页面。'));
    const failed = state.demo === 'error';
    return el('section', { class: 'state', role: failed ? 'alert' : 'status' },
      el('h1', {}, failed ? '记录暂时无法读取' : '还没有学习记录'),
      el('p', { class: 'muted' }, failed ? '请重试，已有练习答案不会被清除。' : '有记录之后，这里会显示学习依据和建议。'),
      el('button', { class: 'btn', onclick: () => {
        state.demo = 'normal'; $('#demo-state').value = 'normal'; render();
      } }, failed ? '重试' : '返回样例'));
  }

  function render({ navigate = false, target = null } = {}) {
    renderChrome();
    const update = () => main.replaceChildren(state.demo === 'normal' ? views[state.view]() : stateBlock());
    if (navigate) {
      update();
      main.scrollTop = 0;
      window.scrollTo({ top: 0, behavior: 'instant' });
      main.focus({ preventScroll: true });
      reveal(main.firstElementChild, 7, 230);
    } else preserve(update);
    if (target) {
      const node = document.getElementById(target);
      node?.scrollIntoView({ block: 'start', behavior: state.reduce || keyboardInput ? 'instant' : 'smooth' });
      node?.focus({ preventScroll: true });
    }
  }

  /* 材料列表与预览只更新结果区，检索框与筛选控件始终保留。 */
  function materialCatalog() {
    if (state.role === 'teacher') return XB.materials;
    const diagnosis = XB.focus(state.sid);
    const stats = [...diagnosis.need, ...diagnosis.none, ...diagnosis.stats];
    const ids = new Set();
    return stats.map((stat) => XB.suggest(stat)).filter((material) => {
      if (!material || ids.has(material.id)) return false;
      ids.add(material.id);
      return true;
    });
  }

  function viewMaterials() {
    const student = state.role === 'student';
    const count = el('span', { class: 'result-count', role: 'status' });
    const results = el('div', { class: 'material-results' });
    const filters = el('div', { class: 'filters' },
      el('label', {}, el('span', {}, '主题'), el('select', {
        ...focusKey('material-topic'), onchange: (event) => { state.matTopic = event.target.value; state.matPage = 0; updateResults(); },
      }, el('option', { value: 'all' }, '全部主题'), ...XB.topics.map((topic) => el('option', {
        value: topic.id, selected: state.matTopic === topic.id,
      }, topic.name)))),
      el('label', {}, el('span', {}, '类型'), el('select', {
        ...focusKey('material-type'), onchange: (event) => { state.matType = event.target.value; state.matPage = 0; updateResults(); },
      }, el('option', { value: 'all' }, '全部类型'), ...['讲解', '例题', '练习', '微课', '易错', '小结'].map((type) =>
        el('option', { value: type, selected: state.matType === type }, type)))),
      searchField('搜索材料', 'material-query', state.matQuery, '输入标题或内容', (value) => {
        state.matQuery = value; state.matPage = 0; updateResults();
      }), count);

    function updateResults({ previewMotion = false } = {}) {
      const query = state.matQuery.trim().toLocaleLowerCase();
      const list = materialCatalog().filter((material) =>
        (state.matTopic === 'all' || material.topic === state.matTopic) &&
        (state.matType === 'all' || material.type === state.matType) &&
        (!query || (material.title + material.body.join('')).toLocaleLowerCase().includes(query)));
      const pageSize = 8;
      const pages = Math.max(1, Math.ceil(list.length / pageSize));
      state.matPage = Math.max(0, Math.min(state.matPage, pages - 1));
      const page = list.slice(state.matPage * pageSize, (state.matPage + 1) * pageSize);
      const current = page.find((material) => material.id === state.matId) || page[0];
      state.matId = current?.id || null;
      count.textContent = `${list.length} 份材料`;
      preserve(() => {
        if (!current) {
          results.replaceChildren(el('div', { class: 'state' }, el('h2', {}, '没有找到相关材料'), el('p', { class: 'muted' }, '试试其他关键词或筛选条件。')));
          return;
        }
        const catalog = el('div', { class: 'material-catalog' },
          el('div', { class: 'list-heading' }, el('h2', {}, student ? '为你选择的材料' : '材料目录'), el('span', { class: 'muted' }, '选择后阅读')),
          el('div', { class: 'material-list' }, ...page.map((material) => el('button', {
            class: 'material-row', 'aria-pressed': material.id === current.id ? 'true' : 'false',
            ...focusKey(`material-${material.id}`), onclick: () => {
              state.matId = material.id;
              updateResults({ previewMotion: true });
              if (window.matchMedia('(max-width: 860px)').matches) {
                const preview = $('.preview', results);
                preview.scrollIntoView({ block: 'start', behavior: state.reduce || keyboardInput ? 'instant' : 'smooth' });
                preview.focus({ preventScroll: true });
              }
            },
          }, el('span', { class: 'material-type' }, material.type),
          el('span', { class: 'material-name' }, material.title, el('span', { class: 'sub' }, material.kp ? XB.kpById[material.kp].name : XB.topicById[material.topic].name)),
          el('span', { class: 'material-indicator', 'aria-hidden': 'true' }, material.id === current.id ? '正在阅读' : '阅读')))),
          el('div', { class: 'pager' },
            el('span', {}, `${state.matPage + 1} / ${pages}`),
            el('button', { class: 'btn btn-sm', ...focusKey('material-prev'), disabled: state.matPage === 0,
              onclick: () => { state.matPage--; state.matId = null; updateResults(); } }, '上一页'),
            el('button', { class: 'btn btn-sm', ...focusKey('material-next'), disabled: state.matPage === pages - 1,
              onclick: () => { state.matPage++; state.matId = null; updateResults(); } }, '下一页')));
        const preview = el('article', { class: 'preview', 'aria-label': '材料正文', tabindex: '-1' },
          el('p', { class: 'preview-meta' }, `${current.type} · ${current.source}`),
          el('h2', {}, current.title), ...current.body.map((text) => el('p', {}, text)),
          current.kp ? el('button', { class: 'link preview-action', onclick: () => {
            if (student) openStudent(state.sid, current.kp, 'practice-title');
            else { state.clsTopic = XB.kpById[current.kp].topic; state.clsStatus = 'all'; state.openSid = null; go('class'); }
          } }, student ? '回到对应练习' : '查看相关班级学情') : null);
        results.replaceChildren(el('div', { class: 'mat-layout' }, catalog, preview));
        if (previewMotion) reveal(preview, 3, 160);
      });
    }
    updateResults();
    return el('section', { class: 'view' }, viewHeader(student ? '建议材料' : '学习材料', student
      ? '按当前学习情况排序，先看讲解，再做练习。' : `${XB.materials.length} 份材料 · 按主题与知识点查找`), filters, results);
  }

  /* 班级页保留筛选栏，展开依据时只替换表格内容。 */
  function viewClass() {
    const count = el('span', { class: 'result-count', role: 'status' });
    const chips = el('div', { class: 'chips', role: 'group', 'aria-label': '按学习情况筛选' });
    const results = el('div', { class: 'class-results' });
    const exports = exportControl();
    const filters = el('div', { class: 'filters' },
      el('label', {}, el('span', {}, '主题'), el('select', { ...focusKey('class-topic'), onchange: (event) => {
        state.clsTopic = event.target.value; state.openSid = null; updateResults();
        const nextExport = exportControl();
        exportHolder.replaceWith(nextExport);
        exportHolder = nextExport;
      } }, el('option', { value: 'all' }, '全部主题'), ...XB.topics.map((topic) => el('option', {
        value: topic.id, selected: state.clsTopic === topic.id,
      }, topic.name)))),
      searchField('查找学生', 'class-query', state.clsQuery, '姓名或编号', (value) => { state.clsQuery = value; updateResults(); }), count);
    let exportHolder = exports;

    function evidence(student, diagnosis) {
      const stat = diagnosis.focus;
      const body = el('div', { class: 'evidence-body', id: `evidence-${student.id}` });
      if (!stat) {
        body.append(el('p', {}, diagnosis.none.length ? `还有 ${diagnosis.none.length} 个知识点依据不足，先补充记录。` : '当前范围的知识点均达到演示规则中的已掌握。'),
          el('button', { class: 'link', onclick: () => openStudent(student.id) }, '查看完整诊断'));
        return body;
      }
      const item = XB.itemById[stat.topItem];
      const material = XB.suggest(stat);
      body.append(el('div', { class: 'evidence-heading' }, el('h3', {}, item.error), el('span', { class: 'muted' }, `出现 ${stat.topCount} 次`)),
        el('ol', { class: 'evidence-list' }, ...stat.wrongs.slice(-3).reverse().map((record) => el('li', {},
          el('time', { datetime: record.date }, record.date), el('span', {}, XB.itemById[record.item].stem), el('span', { class: 'record-id' }, record.id)))),
        el('div', { class: 'evidence-actions' },
          el('button', { class: 'btn btn-sm', onclick: () => openStudent(student.id, stat.kp) }, '查看作答步骤'),
          el('button', { class: 'link', onclick: () => openMaterial(material.id) }, '阅读对应讲解')));
      return body;
    }

    function updateResults(expandedSid = null) {
      const kpIds = (state.clsTopic === 'all' ? XB.kps : XB.kps.filter((kp) => kp.topic === state.clsTopic)).map((kp) => kp.id);
      const all = XB.students.map((student) => ({ student, diagnosis: XB.focus(student.id, kpIds) }));
      const query = state.clsQuery.trim().toLocaleLowerCase();
      const rows = all.filter(({ student, diagnosis }) =>
        (state.clsStatus === 'all' || diagnosis.overall === state.clsStatus) &&
        (!query || (student.name + student.id).toLocaleLowerCase().includes(query)))
        .sort((a, b) => XB.STATUS[a.diagnosis.overall].rank - XB.STATUS[b.diagnosis.overall].rank || a.student.id.localeCompare(b.student.id));
      count.textContent = `${rows.length} / ${all.length} 人`;
      const counts = { all: all.length };
      all.forEach(({ diagnosis }) => { counts[diagnosis.overall] = (counts[diagnosis.overall] || 0) + 1; });
      preserve(() => {
        chips.replaceChildren(...[['all', '全部'], ['weak', '薄弱'], ['shaky', '待巩固'], ['none', '依据不足'], ['ok', '已掌握']].map(([key, label]) =>
          el('button', { class: 'chip', 'aria-pressed': state.clsStatus === key ? 'true' : 'false', ...focusKey(`status-${key}`),
            onclick: () => { state.clsStatus = key; state.openSid = null; updateResults(); } }, label, el('span', {}, String(counts[key] || 0)))));
        if (!rows.length) {
          results.replaceChildren(el('div', { class: 'state' }, el('h2', {}, '没有符合条件的学生'), el('p', { class: 'muted' }, '换一个筛选条件，或清空搜索。')));
          return;
        }
        results.replaceChildren(el('div', { class: 'table-wrap' }, el('table', { class: 'class-table' },
          el('thead', {}, el('tr', {}, ...['学生', '当前结论', '优先知识点 / 最近错误', '答对 / 有效', '建议材料'].map((text) => el('th', { scope: 'col' }, text)))),
          el('tbody', {}, ...rows.flatMap(({ student, diagnosis }) => {
            const stat = diagnosis.focus;
            const open = state.openSid === student.id;
            const toggle = () => { state.openSid = open ? null : student.id; updateResults(open ? null : student.id); };
            const row = el('tr', { class: open ? 'student-row is-open' : 'student-row' },
              el('th', { scope: 'row' }, el('button', { class: 'student-toggle', 'aria-expanded': String(open),
                'aria-controls': open ? `evidence-${student.id}` : null, ...focusKey(`student-${student.id}`), onclick: toggle },
                el('span', {}, student.name), el('span', { class: 'disclosure', 'aria-hidden': 'true' }, open ? '收起依据 −' : '查看依据 +'))),
              el('td', { class: 'student-status', 'data-label': '当前结论' }, status(diagnosis.overall)),
              el('td', { class: 'student-priority', 'data-label': '优先知识点' }, stat
                ? el('span', {}, XB.kpById[stat.kp].name, el('span', { class: 'sub' }, XB.itemById[stat.lastWrong.item].error))
                : el('span', { class: 'muted' }, diagnosis.none.length ? `${diagnosis.none.length} 个知识点缺少记录` : '继续巩固')),
              el('td', { class: 'num', 'data-label': '答对 / 有效' }, stat ? `${stat.right} / ${stat.n}` : '—'),
              el('td', { class: 'student-material', 'data-label': '建议材料' }, stat
                ? el('button', { class: 'link', onclick: () => openMaterial(XB.suggest(stat).id) }, XB.suggest(stat).title) : '—'));
            return open ? [row, el('tr', { class: 'evidence' }, el('td', { colspan: '5' }, evidence(student, diagnosis)))] : [row];
          })))));
        if (expandedSid) reveal(document.getElementById(`evidence-${expandedSid}`) || $(`#evidence-${expandedSid}`, results), 4, 180);
      });
    }
    updateResults();
    return el('section', { class: 'view' }, viewHeader('班级学情', `${XB.CLASS_NAME} · ${XB.students.length} 人 · ${XB.AS_OF}`, exportHolder),
      filters, chips, results, ruleDisclosure());
  }

  function openStudent(sid, kp, target = null) {
    state.sid = sid;
    state.kp = kp || XB.focus(sid).focus?.kp || state.kp;
    go('student', { target, force: true });
  }

  function openMaterial(id) {
    state.matTopic = 'all'; state.matType = 'all'; state.matQuery = ''; state.matId = id;
    const index = materialCatalog().findIndex((material) => material.id === id);
    state.matPage = Math.floor(Math.max(0, index) / 8);
    go('materials', { force: true });
  }

  function viewStudent() {
    const student = XB.studentById[state.sid];
    const stat = XB.stat(state.sid, state.kp);
    const kp = XB.kpById[state.kp];
    const item = stat.lastWrong ? XB.itemById[stat.lastWrong.item] : XB.items.find((question) => question.kp === state.kp);
    const material = XB.suggest(stat);
    const practice = XB.practice[state.kp];
    const hasEvidence = Boolean(stat.lastWrong);
    const work = el('article', { class: 'work' },
      el('p', { class: 'work-meta' }, hasEvidence ? `${stat.lastWrong.date} · ${item.id}` : '讲解示例 · 非该学生作答'),
      el('h2', { class: 'hand hand-stem' }, item.stem),
      el('ol', { class: 'hand hand-steps' }, ...(hasEvidence ? item.steps.map((text, index) => el('li', {},
        el('span', { class: index === item.wrong ? 'step-text is-error' : 'step-text' }, text))) : [])),
      el('div', { class: 'correction' }, el('h3', {}, hasEvidence ? '这一步怎么改' : '对照解法'),
        ...item.fix.map((text) => el('p', { class: 'hand' }, text)), el('p', {}, item.why)),
      !hasEvidence ? el('p', { class: 'muted' }, '当前没有错误记录可供定位，这个例子只用于讲解。') : null);
    const next = el('section', { class: 'next', 'aria-labelledby': 'practice-title' });

    function updatePractice(animate = false) {
      const picked = state.answers[student.id + practice.id];
      preserve(() => next.replaceChildren(
        el('div', { class: 'section-heading' }, el('h2', { id: 'practice-title', tabindex: '-1' }, '做一道题，检查这一步'), el('span', { class: 'muted' }, practice.id)),
        el('p', { class: 'practice-stem' }, practice.stem),
        el('div', { class: 'options', role: 'group', 'aria-label': '选择答案' }, ...practice.options.map((option, index) => {
          const judged = picked != null;
          const classes = ['option', picked === index ? 'is-checked' : '', judged && index === practice.answer ? 'is-right' : '',
            judged && picked === index && index !== practice.answer ? 'is-wrong' : ''].filter(Boolean).join(' ');
          return el('button', { class: classes, 'aria-pressed': String(picked === index), 'aria-disabled': String(state.saving),
            ...focusKey(`answer-${practice.id}-${index}`), onclick: async () => {
              if (state.saving) return;
              state.saving = true;
              updatePractice();
              try {
                const response = await fetch(`/api/study/practice/${encodeURIComponent(student.id)}/${encodeURIComponent(practice.id)}`, {
                  method: 'POST', headers: { 'Content-Type': 'application/json' },
                  body: JSON.stringify({ answer_index: index }), signal: AbortSignal.timeout(15000),
                });
                if (!response.ok) throw new Error('答案没有保存，请再试一次。');
                state.answers[student.id + practice.id] = index;
              } catch (error) { toast(error.message); }
              finally {
                state.saving = false;
                if (state.view === 'student') {
                  if (next.isConnected) updatePractice(true);
                  else render();
                }
              }
            } }, el('span', { class: 'option-key' }, 'ABCD'[index]), option,
            judged && picked === index ? el('span', { class: 'answer-state' }, index === practice.answer ? '正确' : '再想想') : null);
        })),
        el('p', { class: `answer-note ${picked === practice.answer ? 'st-ok' : ''}`, role: 'status' }, state.saving ? '正在保存答案…'
          : picked == null ? '选择后查看解析，答案会保存在本机。'
            : `${picked === practice.answer ? '答对了。' : `正确答案是 ${'ABCD'[practice.answer]}。`}${practice.why}`),
        picked != null ? el('p', { class: 'practice-note' }, '已保存。本次练习与历史诊断分开记录。') : null));
      if (animate) reveal($('.answer-note', next), 3, 160);
    }
    updatePractice();
    const learning = el('div', { class: 'learning-column' }, work,
      el('section', { class: 'recommended-reading' },
        el('div', {}, el('p', { class: 'muted' }, '先看这份材料'), el('h2', {}, material.title),
          el('p', { class: 'muted' }, `${material.type} · ${material.source}`)),
        el('button', { class: 'btn', onclick: () => openMaterial(material.id) }, '阅读讲解')), next);
    const summary = el('aside', { class: 'diagnosis-summary', 'aria-label': '当前知识点的学习依据' },
      el('h2', {}, '当前依据'), status(stat.status),
      el('p', { class: 'evidence-total' }, el('strong', {}, `${stat.right} / ${stat.n}`), ' 答对 / 有效记录'),
      el('p', { class: 'muted' }, stat.status === 'none' ? XB.missingNote(stat) : `记录截至 ${XB.AS_OF}`),
      hasEvidence ? el('p', {}, el('span', { class: 'muted' }, '最近错误'), el('strong', { class: 'summary-error' }, item.error)) : null,
      el('p', { class: 'muted' }, stat.status === 'none' ? '先补充证据，材料仅供学习，不表示错误归因。' : '一次错误用于定位步骤，不推断学习原因。'), ruleDisclosure());
    return el('section', { class: 'view student-view' },
      viewHeader(state.role === 'teacher' ? `${student.name} 的诊断` : '我的诊断', `${kp.name} · ${kp.ref}`,
        state.role === 'teacher' ? el('label', { class: 'student-select' }, '学生', el('select', {
          ...focusKey('student-select'), onchange: (event) => openStudent(event.target.value),
        }, ...XB.students.map((one) => el('option', { value: one.id, selected: one.id === state.sid }, one.name)))) : null),
      el('div', { class: 'kp-strip', role: 'group', 'aria-label': '选择知识点' }, ...XB.kps.map((one) => el('button', {
        class: 'kp-pill', 'aria-pressed': String(one.id === state.kp), ...focusKey(`kp-${one.id}`),
        onclick: () => { state.kp = one.id; render(); reveal($('.diag'), 4, 180); },
      }, one.name, el('span', { class: `kp-status st-${XB.stat(state.sid, one.id).status}` }, XB.STATUS[XB.stat(state.sid, one.id).status].label)))),
      el('div', { class: 'diag' }, learning, summary));
  }

  function viewReport() {
    const student = XB.studentById[state.sid];
    const diagnosis = XB.focus(state.sid);
    const stat = diagnosis.focus;
    const material = stat ? XB.suggest(stat) : XB.suggest(XB.stat(state.sid, state.kp));
    const item = stat ? XB.itemById[stat.topItem] : null;
    const conclusion = stat ? `先巩固${XB.kpById[stat.kp].name}。`
      : diagnosis.none.length ? '补充记录，再作判断。' : '继续巩固已有掌握。';
    const body = el('article', { class: 'report' },
      viewHeader(state.role === 'teacher' ? `${student.name} 的学习报告` : '我的学习报告', `${XB.CLASS_NAME} · ${XB.AS_OF}`, exportControl()),
      el('section', { class: 'report-conclusion' }, el('h2', {}, conclusion),
        el('p', {}, stat ? `${stat.n} 条有效记录中答对 ${stat.right} 条，当前为${XB.STATUS[stat.status].label}。先看对应讲解，再用下一题检查步骤。`
          : diagnosis.none.length ? `还有 ${diagnosis.none.length} 个知识点依据不足，暂不判断全部掌握。` : '当前范围均达到演示规则中的已掌握。')),
      stat ? el('section', { class: 'report-section' }, el('h2', {}, '判断来自哪些记录'),
        el('dl', { class: 'basis' },
          el('div', {}, el('dt', {}, '反复出现的错误'), el('dd', {}, `${item.error}，出现 ${stat.topCount} 次。`)),
          el('div', {}, el('dt', {}, '需要留意的步骤'), el('dd', {}, item.why)),
          el('div', {}, el('dt', {}, '记录范围'), el('dd', {}, `${stat.records[0].date} 至 ${stat.records[stat.records.length - 1].date} · ${stat.n} 条有效记录`)))) : null,
      el('section', { class: 'report-section report-actions' }, el('h2', {}, '接下来怎么做'),
        el('button', { class: 'reading-link', onclick: () => openMaterial(material.id) }, el('span', { class: 'muted' }, '阅读讲解'),
          el('strong', {}, material.title), el('span', {}, '打开材料 →')),
        el('button', { class: 'btn btn-primary', onclick: () => openStudent(student.id, stat?.kp || state.kp, 'practice-title') }, '做下一题')),
      diagnosis.none.length ? el('details', { class: 'report-missing' }, el('summary', {}, `${diagnosis.none.length} 个知识点依据不足`),
        el('ul', {}, ...diagnosis.none.map((one) => el('li', {}, XB.kpById[one.kp].name, el('span', { class: 'muted' }, XB.missingNote(one)))))) : null,
      el('footer', { class: 'report-foot' }, ruleDisclosure(), state.role === 'teacher'
        ? el('button', { class: 'link', onclick: () => go('class') }, '回到班级') : null));
    return el('section', { class: 'view' }, body);
  }

  const views = { materials: viewMaterials, class: viewClass, student: viewStudent, report: viewReport };
  function go(view, { target = null, force = false } = {}) {
    $('#settings').open = false;
    if (state.view === view && !force) return;
    animated.forEach((animation) => animation.cancel());
    state.view = view;
    render({ navigate: true, target });
  }

  function enterWorkbench(role) {
    if (state.role !== role) {
      state.matTopic = 'all'; state.matType = 'all'; state.matQuery = ''; state.matId = null; state.matPage = 0;
    }
    state.role = role;
    if (role === 'student') { state.sid = XB.ME; state.kp = XB.focus(XB.ME).focus?.kp || 'K5'; }
    $('#entry').hidden = true;
    $('#shell').hidden = false;
    go(role === 'teacher' ? 'class' : 'student', { force: true });
  }

  function playEntry() {
    $('#entry').hidden = false;
    if (!entryPlayed && !state.reduce && !keyboardInput) {
      reveal($('.entry-intro'), 7, 280);
      reveal($('.entry-body'), 5, 280);
    }
    entryPlayed = true;
  }

  function applyMotion() {
    state.reduce = manualReduce || systemMotion.matches;
    document.body.classList.toggle('reduce-motion', state.reduce);
    $('#demo-reduce').checked = state.reduce;
    $('#demo-reduce').disabled = systemMotion.matches;
    $('#motion-hint').textContent = systemMotion.matches ? '系统已开启减少动态效果。' : '同时遵循系统的动态效果偏好。';
    if (state.reduce) animated.forEach((animation) => animation.cancel());
  }

  document.querySelectorAll('[data-enter]').forEach((button) => button.addEventListener('click', () => enterWorkbench(button.dataset.enter)));
  $('[data-go="entry"]').addEventListener('click', () => {
    animated.forEach((animation) => animation.cancel());
    $('#shell').hidden = true; state.view = 'entry'; playEntry();
  });
  $('#demo-role').addEventListener('click', () => enterWorkbench(state.role === 'teacher' ? 'student' : 'teacher'));
  $('#demo-state').addEventListener('change', (event) => { state.demo = event.target.value; $('#settings').open = false; render({ navigate: true }); });
  $('#demo-reduce').addEventListener('change', (event) => {
    manualReduce = event.target.checked;
    try { localStorage.setItem('study-reduce-motion', String(manualReduce)); } catch (_) { /* Preference still works for this visit. */ }
    applyMotion();
  });
  systemMotion.addEventListener('change', applyMotion);
  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    const details = event.target.closest('details[open]') || document.querySelector('.settings[open], .export-control[open]');
    if (details) {
      event.preventDefault();
      details.open = false;
      $('summary', details)?.focus({ preventScroll: true });
    }
  });
  document.addEventListener('pointerdown', (event) => {
    document.querySelectorAll('.settings[open], .export-control[open]').forEach((details) => {
      if (!details.contains(event.target)) details.open = false;
    });
  });
  $('#boot-status')?.remove();
  applyMotion();
  playEntry();
})(window.XB);
