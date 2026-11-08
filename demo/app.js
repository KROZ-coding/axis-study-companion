/* 智慧学伴演示。五个视图共用一个外壳，切换角色不重载页面。
   签名动效只出现在入口、角色交接、打开报告三个时刻，各播放一次。 */
(function (XB) {
  'use strict';

  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, attrs, ...kids) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') node.className = v;
      else if (k === 'html') node.innerHTML = v;
      else if (k.startsWith('on')) node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) {
      if (kid == null || kid === false) continue;
      node.append(kid.nodeType ? kid : document.createTextNode(kid));
    }
    return node;
  };
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  const stLabel = (key) => `<span class="st st-${key}">${XB.STATUS[key].label}</span>`;
  const kpName = (id) => { const k = XB.kpById[id]; return el('span', {}, k.name, el('span', { class: 'ref' }, k.ref)); };

  const state = {
    role: null, view: 'entry', sid: XB.ME, kp: 'K5',
    matTopic: 'all', matType: 'all', matQuery: '', matId: null, matPage: 0,
    clsTopic: 'all', clsStatus: 'all', clsQuery: '', openSid: null,
    demo: 'normal', reduce: false, reportFresh: false,
    played: { entry: false, handoff: false }, answers: {},
  };

  const NAV = {
    teacher: [
      ['材料与依据', 'materials'],
      ['班级诊断', 'class'],
      ['学生诊断', 'student'],
      ['个人报告', 'report'],
    ],
    student: [
      ['我的诊断', 'student'],
      ['我的报告', 'report'],
      ['建议材料', 'materials'],
    ],
  };

  /* ---------- 提示与状态 ---------- */
  let toastTimer = null;
  const toast = (text) => {
    const t = $('#toast');
    t.textContent = text;
    t.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.classList.remove('show'), 2600);
  };

  const stateBlock = (kind, view) => {
    if (kind === 'loading') {
      const block = el('div', { class: 'state', role: 'status' },
        el('p', { class: 'k' }, '正在读取样例记录'),
        el('div', { class: 'skeleton' }, ...Array.from({ length: 6 }, () => el('i'))));
      if (view === 'materials') {
        block.append(el('div', { class: 'progress' },
          el('span', {}, '导入样例记录'),
          el('span', { class: 'progress-track' }, el('span', { class: 'progress-bar', id: 'imp-bar' })),
          el('span', { id: 'imp-num' }, '0%')));
      }
      return block;
    }
    if (kind === 'error') {
      return el('div', { class: 'state', role: 'alert' },
        el('p', {}, '样例记录没有读出来'),
        el('p', { class: 'muted' }, '页面上的统计都来自这些记录，读不出来就不显示数字。'),
        el('button', { class: 'btn', onclick: () => { $('#demo-state').value = 'normal'; setDemo('normal'); } }, '重试'));
    }
    const hints = {
      materials: ['还没有材料', '导入课程材料后，这里按知识点和错误类型列出。'],
      class: ['这个班还没有作答记录', '导入作业记录后，可以按知识点和结论筛选学生。'],
      student: ['还没有这个学生的记录', '有记录之后，这里显示最近的错题、知识点和建议材料。'],
      report: ['还没有可以写进报告的记录', '报告只根据作答记录生成，没有记录就不下结论。'],
    };
    const [title, next] = hints[view];
    return el('div', { class: 'state' }, el('p', {}, title), el('p', { class: 'muted' }, next));
  };

  /* ---------- 渲染 ---------- */
  const render = () => {
    document.body.classList.toggle('reduce-motion', state.reduce);
    $('#top-role').textContent = state.role === 'teacher'
      ? `教师 · ${XB.CLASS_NAME}` : `学生 · ${XB.studentById[state.sid].name}`;
    $('#demo-role').textContent = state.role === 'teacher' ? '切换到学生' : '切换到教师';

    const nav = $('#sidenav');
    nav.replaceChildren(
      el('p', { class: 'nav-group' }, state.role === 'teacher' ? XB.CLASS_NAME : '我的学情'),
      ...NAV[state.role].map(([label, view]) => el('button', {
        class: 'nav-item', 'aria-current': state.view === view ? 'page' : null,
        onclick: () => go(view),
      }, label, view === 'student' && state.role === 'teacher'
        ? el('span', { class: 'nav-sub' }, XB.studentById[state.sid].name) : null)));

    const main = $('#main');
    main.replaceChildren(state.demo === 'normal' ? views[state.view]() : stateBlock(state.demo, state.view));
    if (state.demo === 'loading' && state.view === 'materials') runImport();
    if (state.view === 'report' && state.reportFresh) {
      state.reportFresh = false;
      $('.report', main)?.classList.add('is-fresh');
    }
    main.focus({ preventScroll: true });
  };

  const views = { materials: viewMaterials, class: viewClass, student: viewStudent, report: viewReport };

  /* ---------- 材料与依据 ---------- */
  const filteredMaterials = () => XB.materials.filter((m) =>
    (state.matTopic === 'all' || m.topic === state.matTopic) &&
    (state.matType === 'all' || m.type === state.matType) &&
    (!state.matQuery || (m.title + m.body.join('')).includes(state.matQuery)));

  function viewMaterials() {
    const list = filteredMaterials();
    const pages = Math.max(1, Math.ceil(list.length / 12));
    state.matPage = Math.min(state.matPage, pages - 1);
    const page = list.slice(state.matPage * 12, state.matPage * 12 + 12);
    const current = XB.matById[state.matId] || page[0] || null;
    const who = current && (XB.recommended[current.id] || []);

    const row = (m) => el('tr', {
      class: 'clickable', 'aria-selected': current && m.id === current.id ? 'true' : null, tabindex: '0',
      onclick: () => { state.matId = m.id; render(); },
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); state.matId = m.id; render(); } },
    },
      el('td', { class: 'nowrap' }, el('span', { class: m.type === '讲解' || m.type === '易错' ? 'mark' : '' }, m.type)),
      el('td', {}, m.title, el('span', { class: 'sub' }, m.source)),
      el('td', { class: 'nowrap' }, m.kp ? XB.kpById[m.kp].name : XB.topicById[m.topic].name),
      el('td', { class: 'num' }, who && m.id === current?.id ? String(who.length) : String((XB.recommended[m.id] || []).length)));

    return el('section', { class: 'view' },
      el('div', { class: 'view-head' },
        el('div', {}, el('h1', { class: 'view-title' }, '材料与依据 ', el('span', { class: 'tag-sample' }, '样例')),
          el('p', { class: 'view-meta' }, `导入 ${XB.rawCount} 条，去掉重复 ${XB.dupRemoved} 条，有效 ${XB.records.length} 条 · 截至 ${XB.AS_OF}`))),
      el('div', { class: 'bar' },
        el('label', {}, '主题', el('select', { onchange: (e) => { state.matTopic = e.target.value; state.matPage = 0; render(); } },
          el('option', { value: 'all' }, '全部'),
          ...XB.topics.map((t) => el('option', { value: t.id, selected: state.matTopic === t.id }, t.name)))),
        el('label', {}, '类型', el('select', { onchange: (e) => { state.matType = e.target.value; state.matPage = 0; render(); } },
          el('option', { value: 'all' }, '全部'),
          ...['讲解', '例题', '练习', '微课', '易错', '小结'].map((t) => el('option', { value: t, selected: state.matType === t }, t)))),
        el('label', {}, '检索', el('input', { type: 'search', value: state.matQuery, placeholder: '标题或正文',
          oninput: (e) => { state.matQuery = e.target.value.trim(); state.matPage = 0; render(); } })),
        el('span', { class: 'bar-end' }, `${list.length} 份`)),
      list.length === 0
        ? el('div', { class: 'state' }, el('p', {}, '没有符合条件的材料'), el('p', { class: 'muted' }, '换一个主题、类型，或清空检索。'))
        : el('div', { class: 'mat-layout' },
          el('div', {},
            el('div', { class: 'table-wrap' }, el('table', {},
              el('thead', {}, el('tr', {}, el('th', {}, '类型'), el('th', {}, '材料'), el('th', {}, '知识点'), el('th', { class: 'num' }, '被建议'))),
              el('tbody', {}, ...page.map(row)))),
            el('div', { class: 'pager' },
              el('button', { class: 'btn btn-sm', disabled: state.matPage === 0, onclick: () => { state.matPage--; render(); } }, '上一页'),
              el('span', {}, `${state.matPage + 1} / ${pages}`),
              el('button', { class: 'btn btn-sm', disabled: state.matPage >= pages - 1, onclick: () => { state.matPage++; render(); } }, '下一页'))),
          current && el('aside', { class: 'preview' },
            el('p', { class: 'preview-type' }, current.type),
            el('h2', {}, current.title),
            el('p', { class: 'k' },
              current.kp ? kpName(current.kp) : XB.topicById[current.topic].name,
              ' · ', current.source),
            ...current.body.map((p) => el('p', {}, p)),
            who && who.length
              ? el('p', { class: 'muted' }, `建议给 ${who.length} 名学生：`, who.slice(0, 6).map((id) => XB.studentById[id].name).join('、'), who.length > 6 ? ' 等' : '')
              : el('p', { class: 'muted' }, '当前没有学生被建议这份材料。'),
            current.kp && el('button', { class: 'btn btn-sm', onclick: () => { state.kp = current.kp; go('class'); } },
              `在班级里看「${XB.kpById[current.kp].name}」`))));
  }

  /* ---------- 班级诊断 ---------- */
  function viewClass() {
    const kpIds = state.clsTopic === 'all' ? XB.kps.map((k) => k.id)
      : XB.kps.filter((k) => k.topic === state.clsTopic).map((k) => k.id);
    const rows = XB.students.map((s) => ({ s, f: XB.focus(s.id, kpIds) })).filter(({ s, f }) =>
      (state.clsStatus === 'all' || f.overall === state.clsStatus) &&
      (!state.clsQuery || s.name.includes(state.clsQuery) || s.id.toLowerCase().includes(state.clsQuery.toLowerCase())));
    rows.sort((a, b) => XB.STATUS[a.f.overall].rank - XB.STATUS[b.f.overall].rank || a.s.id.localeCompare(b.s.id));
    const counts = { all: XB.students.length };
    XB.students.forEach((s) => { const o = XB.focus(s.id, kpIds).overall; counts[o] = (counts[o] || 0) + 1; });

    const chip = (key, label) => el('button', {
      class: 'chip', 'aria-pressed': state.clsStatus === key ? 'true' : 'false',
      onclick: () => { state.clsStatus = key; state.openSid = null; render(); },
    }, label, el('b', {}, String(counts[key] || 0)));

    const evidence = (s, f) => {
      const st = f.focus;
      if (!st) {
        return el('tr', { class: 'evidence' }, el('td', { colspan: '6' }, el('div', { class: 'evidence-body' },
          el('p', {}, f.overall === 'none' ? '范围内的知识点都依据不足，先补记录再下结论。' : '范围内的知识点都已掌握。'),
          el('div', { class: 'evidence-actions' },
            el('button', { class: 'link', onclick: () => openStudent(s.id) }, '看学生诊断')))));
      }
      const item = XB.itemById[st.lastWrong.item];
      const sug = XB.suggest(st);
      return el('tr', { class: 'evidence' }, el('td', { colspan: '6' }, el('div', { class: 'evidence-body' },
        el('h3', {}, `${XB.kpById[st.kp].name} · ${XB.STATUS[st.status].label} · ${st.right}/${st.n}`),
        el('p', { class: 'muted' }, `最多的错误是「${item.error}」，出现 ${st.topCount} 次。判定：${XB.RULE}`),
        el('ol', { class: 'evidence-list' }, ...st.wrongs.slice(-4).reverse().map((r) => el('li', {},
          el('span', { class: 'd' }, r.date),
          el('span', { class: 'wrong' }, XB.itemById[r.item].stem),
          el('span', { class: 'muted' }, r.id)))),
        el('div', { class: 'evidence-actions' },
          el('button', { class: 'link', onclick: () => openStudent(s.id, st.kp) }, '看这道错题'),
          el('button', { class: 'link', onclick: () => openMaterial(sug.id) }, `建议材料：${sug.title}`)))));
    };

    return el('section', { class: 'view' },
      el('div', { class: 'view-head' },
        el('div', {}, el('h1', { class: 'view-title' }, '班级诊断 ', el('span', { class: 'tag-sample' }, '样例')),
          el('p', { class: 'view-meta' }, `${XB.CLASS_NAME} · 50 人 · 截至 ${XB.AS_OF}`))),
      el('div', { class: 'bar' },
        el('label', {}, '主题', el('select', { onchange: (e) => { state.clsTopic = e.target.value; state.openSid = null; render(); } },
          el('option', { value: 'all' }, '全部主题'),
          ...XB.topics.map((t) => el('option', { value: t.id, selected: state.clsTopic === t.id }, t.name)))),
        el('label', {}, '学生', el('input', { type: 'search', value: state.clsQuery, placeholder: '姓名',
          oninput: (e) => { state.clsQuery = e.target.value.trim(); render(); } })),
        el('span', { class: 'bar-end' }, `显示 ${rows.length} 人`)),
      el('div', { class: 'chips' },
        chip('all', '全部'), chip('weak', '薄弱'), chip('shaky', '待巩固'), chip('none', '依据不足'), chip('ok', '已掌握')),
      el('p', { class: 'rule' }, XB.RULE),
      rows.length === 0
        ? el('div', { class: 'state' }, el('p', {}, '没有符合条件的学生'), el('p', { class: 'muted' }, '换一个主题或结论，或清空检索。'))
        : el('div', { class: 'table-wrap' }, el('table', {},
          el('thead', {}, el('tr', {},
            el('th', {}, '学生'), el('th', {}, '结论'), el('th', {}, '最近错误'), el('th', {}, '知识点'),
            el('th', { class: 'num' }, '依据'), el('th', {}, '建议'))),
          el('tbody', {}, ...rows.flatMap(({ s, f }) => {
            const st = f.focus;
            const open = state.openSid === s.id;
            const head = el('tr', {
              class: 'clickable', 'aria-selected': open ? 'true' : null, 'aria-expanded': open ? 'true' : 'false', tabindex: '0',
              onclick: () => { state.openSid = open ? null : s.id; render(); },
              onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); state.openSid = open ? null : s.id; render(); } },
            },
              el('td', { class: 'nowrap' }, s.name),
              el('td', { html: stLabel(f.overall) }),
              el('td', {}, st ? el('span', { class: 'mark' }, XB.itemById[st.lastWrong.item].error) : el('span', { class: 'muted' }, '—'),
                st ? el('span', { class: 'sub' }, st.lastWrong.date) : null),
              el('td', { class: 'nowrap' }, st ? XB.kpById[st.kp].name : el('span', { class: 'muted' }, f.none.length ? `缺 ${f.none.length} 个知识点` : '—')),
              el('td', { class: 'num' }, st ? `${st.right}/${st.n}` : '—'),
              el('td', {}, st ? XB.suggest(st).title : el('span', { class: 'muted' }, '—')));
            return open ? [head, evidence(s, f)] : [head];
          })))));
  }

  /* ---------- 学生个人诊断 ---------- */
  const openStudent = (sid, kp) => {
    state.sid = sid;
    if (kp) state.kp = kp;
    else { const f = XB.focus(sid).focus; if (f) state.kp = f.kp; }
    go('student');
  };
  const openMaterial = (id) => { state.matId = id; state.matTopic = 'all'; state.matType = 'all'; state.matQuery = ''; go('materials'); };

  function viewStudent() {
    const s = XB.studentById[state.sid];
    const st = XB.stat(s.id, state.kp);
    const kp = XB.kpById[state.kp];
    const item = st.lastWrong ? XB.itemById[st.lastWrong.item] : XB.items.find((it) => it.kp === state.kp);
    const sug = XB.suggest(st);
    const prac = XB.practice[state.kp];
    const picked = state.answers[s.id + prac.id];

    const step = (text, i) => el('li', {},
      el('span', { class: 'step-text' }, text,
        i === item.wrong ? el('span', { class: 'strike drawn', 'aria-hidden': 'true' }) : null));

    return el('section', { class: 'view' },
      el('div', { class: 'view-head' },
        el('div', {},
          el('h1', { class: 'view-title' }, state.role === 'teacher' ? `${s.name} 的诊断 ` : '我的诊断 ',
            el('span', { class: 'tag-sample' }, '样例')),
          el('p', { class: 'view-meta' }, `${XB.CLASS_NAME} · 截至 ${XB.AS_OF}`)),
        state.role === 'teacher' ? el('label', { class: 'muted' }, '学生 ',
          el('select', { onchange: (e) => openStudent(e.target.value) },
            ...XB.students.map((st2) => el('option', { value: st2.id, selected: st2.id === s.id }, st2.name)))) : null),
      el('div', { class: 'kp-strip', role: 'group', 'aria-label': '知识点' }, ...XB.kps.map((k) => {
        const one = XB.stat(s.id, k.id);
        return el('button', { class: 'kp-pill', 'aria-pressed': k.id === state.kp ? 'true' : 'false',
          onclick: () => { state.kp = k.id; render(); } },
          el('span', { html: stLabel(one.status) }), ' ', k.name);
      })),
      el('div', { class: 'diag' },
        el('div', { class: 'work' },
          el('p', { class: 'work-meta' }, item.id, ' · ', st.lastWrong ? st.lastWrong.date : '样例题', ' ', el('span', { class: 'tag-sample' }, '样例')),
          el('p', { class: 'hand hand-stem' }, item.stem),
          el('ol', { class: 'hand hand-steps' },
            ...item.steps.map(step),
            ...item.fix.map((t) => el('li', { class: 'fix' }, t))),
          el('p', { class: 'why' }, el('span', { class: 'mark' }, item.error), '。', item.why),
          el('div', { class: 'next' },
            el('h3', {}, '下一题'),
            el('p', {}, prac.stem, el('span', { class: 'ref' }, prac.id)),
            el('div', { class: 'options', role: 'radiogroup', 'aria-label': '选项' }, ...prac.options.map((op, i) => {
              const judged = picked != null;
              const cls = ['option', picked === i ? 'is-checked' : '', judged && i === prac.answer ? 'is-right' : '',
                judged && picked === i && i !== prac.answer ? 'is-wrong' : ''].filter(Boolean).join(' ');
              return el('button', { class: cls, role: 'radio', 'aria-checked': picked === i ? 'true' : 'false',
                onclick: () => { state.answers[s.id + prac.id] = i; render(); } },
                el('span', { class: 'key' }, 'ABCD'[i]), op);
            })),
            picked == null ? el('p', { class: 'muted' }, '选一个答案，马上对答案。')
              : el('p', { class: 'answer-note ' + (picked === prac.answer ? 'st-ok' : 'mark') },
                picked === prac.answer ? '答对了。' : `正确答案是 ${'ABCD'[prac.answer]}。`, prac.why))),
        el('ol', { class: 'trail', 'aria-label': '批改痕迹' },
          el('li', {}, el('span', { class: 'k' }, '错在哪里'), item.steps[item.wrong]),
          el('li', {}, el('span', { class: 'k' }, '错误类型'), item.error, el('span', { class: 'sub' }, item.desc)),
          el('li', {}, el('span', { class: 'k' }, '知识点'), kp.name, el('span', { class: 'ref' }, kp.ref)),
          el('li', {}, el('span', { class: 'k' }, '结论'), el('span', { html: stLabel(st.status) }),
            st.status === 'none' ? el('span', { class: 'sub' }, XB.missingNote(st))
              : el('span', { class: 'sub' }, `${st.right}/${st.n} · ${XB.RULE}`)),
          el('li', {}, el('span', { class: 'k' }, '建议材料'),
            el('button', { class: 'link', onclick: () => openMaterial(sug.id) }, sug.title),
            el('span', { class: 'sub' }, `${sug.type} · ${sug.source}`)))),
    );
  }

  /* ---------- 个人报告 ---------- */
  function viewReport() {
    const s = XB.studentById[state.sid];
    const f = XB.focus(s.id);
    const basis = [];
    if (f.focus) {
      const st = f.focus;
      const item = XB.itemById[st.topItem];
      const sug = XB.suggest(st);
      basis.push(['最需要处理', `${XB.kpById[st.kp].name}（${XB.kpById[st.kp].ref}）${XB.STATUS[st.status].label}，${st.n} 条里对了 ${st.right} 条。`],
        ['最多的错误', `「${item.error}」出现 ${st.topCount} 次，最近一次是 ${st.lastWrong.date} 的 ${item.id}。`],
        ['错在哪一步', `${item.steps[item.wrong]}。${item.why}`],
        ['建议材料', `${sug.title}（${sug.id}）。`],
        ['下一题', `${XB.practice[st.kp].id}：${XB.practice[st.kp].stem}`]);
      if (f.need.length > 1) basis.push(['其余待处理', f.need.slice(1, 4).map((x) => `${XB.kpById[x.kp].name}（${XB.STATUS[x.status].label}，${x.right}/${x.n}）`).join('、') + '。']);
    }
    const noneNames = f.none.map((x) => `${XB.kpById[x.kp].name}（${XB.missingNote(x)}）`);

    const conclusion = !f.focus
      ? (f.overall === 'none'
        ? `${s.name} 目前的记录都不足以判断，先补做再下结论。`
        : `${s.name} 已有记录的知识点都达到已掌握，依据不足的知识点没有计入。`)
      : `${s.name} 当前最需要处理的是${XB.kpById[f.focus.kp].name}：${XB.STATUS[f.focus.status].label}。先看建议材料，再做下一题。`;

    return el('section', { class: 'view' },
      el('article', { class: 'report' },
        el('div', { class: 'report-head' },
          el('h1', { class: 'view-title' }, state.role === 'teacher' ? `${s.name} 的报告 ` : '我的报告 ',
            el('span', { class: 'tag-sample' }, '样例')),
          el('span', { class: 'muted' }, `截至 ${XB.AS_OF}`)),
        el('p', { class: 'conclusion', html: esc(conclusion).replace(/(薄弱|待巩固)/g, '<span class="mark">$1</span>') }),
        basis.length ? el('h2', {}, '依据') : null,
        basis.length ? el('ol', { class: 'basis' }, ...basis.slice(0, 6).map(([k, v]) => el('li', {},
          el('span', { class: 'b-k' }, k), v))) : null,
        noneNames.length ? el('h2', {}, '依据不足，未下结论') : null,
        noneNames.length ? el('p', { class: 'muted' }, noneNames.join('、'), '。') : null,
        el('p', { class: 'action' }, f.focus
          ? el('button', { class: 'btn btn-primary', onclick: () => openStudent(s.id, f.focus.kp) }, '去做下一题')
          : el('button', { class: 'btn', onclick: () => go('materials') }, '先看材料')),
        el('p', { class: 'report-foot' },
          el('span', {}, XB.RULE),
          state.role === 'teacher' ? el('button', { class: 'link', onclick: () => go('class') }, '回到班级') : null)));
  }

  /* ---------- 导航与演示 ---------- */
  const go = (view) => {
    if (state.view === 'entry') enterWorkbench(state.role, view);
    else { state.view = view; render(); }
  };

  const setDemo = (value) => { state.demo = value; render(); };

  let importTimer = null;
  const runImport = () => {
    clearInterval(importTimer);
    let p = 0;
    importTimer = setInterval(() => {
      p = Math.min(100, p + 5);
      const bar = $('#imp-bar'), num = $('#imp-num');
      if (!bar) { clearInterval(importTimer); return; }
      bar.style.width = p + '%';
      num.textContent = p + '%';
      if (p === 100) {
        clearInterval(importTimer);
        toast(`导入完成：有效 ${XB.records.length} 条，去掉重复 ${XB.dupRemoved} 条`);
      }
    }, 120);
  };

  /* ---------- 入口与签名动效 ---------- */
  const enterWorkbench = (role, view) => {
    state.role = role;
    state.view = view || (role === 'teacher' ? 'class' : 'student');
    if (role === 'teacher' && !view) { state.clsTopic = 'T2'; state.clsStatus = 'weak'; state.openSid = XB.ME; }
    $('#entry').hidden = true;
    $('#shell').hidden = false;
    const fresh = state.view === 'report';
    state.reportFresh = fresh;
    render();
    if (!state.reduce && !state.played.handoff) {
      state.played.handoff = true;
      $('#shell').classList.add('handoff');
      setTimeout(() => $('#shell').classList.remove('handoff'), 200);
    }
    if (fresh) toast('报告按现有记录生成');
  };

  const playEntry = () => {
    const entry = $('#entry');
    entry.hidden = false;
    if (state.reduce || state.played.entry) return;
    state.played.entry = true;
    entry.classList.add('is-playing');
    const notes = [...document.querySelectorAll('.note')];
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if (reduce) { entry.classList.remove('is-playing'); drawTrace(false); return; }
    $('#wrong-step').classList.add('on');
    notes.forEach((n, i) => setTimeout(() => n.classList.add('on'), 160 + i * 140));
    setTimeout(() => drawTrace(true), 200);
    setTimeout(() => entry.classList.remove('is-playing'), 900);
  };

  const drawTrace = (animate) => {
    const svg = $('#trace');
    const body = $('.entry-body');
    const step = $('#wrong-step');
    const notes = [...document.querySelectorAll('.note')];
    if (!step || body.clientWidth === 0 || getComputedStyle(svg).display === 'none') return;
    const box = body.getBoundingClientRect();
    const pt = (node, dx) => {
      const r = node.getBoundingClientRect();
      return [r.left - box.left + (dx || 0), r.top - box.top + r.height / 2];
    };
    const a = pt(step, step.getBoundingClientRect().width + 6);
    const b = notes.map((n) => pt(n));
    const endX = (p) => p[0] + 7;
    const d = [`M ${a[0]} ${a[1]}`, ...b.map((p, i) => {
      const prev = i === 0 ? a : b[i - 1];
      const mid = (prev[0] + endX(p)) / 2;
      return `C ${mid} ${prev[1]}, ${mid} ${p[1]}, ${endX(p)} ${p[1]}`;
    })].join(' ');
    svg.replaceChildren();
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', d);
    svg.append(path);
    b.forEach((p) => {
      const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
      c.setAttribute('cx', endX(p)); c.setAttribute('cy', p[1]); c.setAttribute('r', 2.4);
      svg.append(c);
    });
    if (animate) {
      const len = path.getTotalLength();
      path.style.strokeDasharray = String(len);
      path.style.strokeDashoffset = String(len);
      path.style.transition = 'stroke-dashoffset 700ms var(--ease-out)';
      requestAnimationFrame(() => { path.style.strokeDashoffset = '0'; });
    }
  };

  /* ---------- 启动 ---------- */
  document.querySelectorAll('[data-enter]').forEach((b) => b.addEventListener('click', () => enterWorkbench(b.dataset.enter)));
  $('[data-go="entry"]').addEventListener('click', () => {
    $('#shell').hidden = true;
    state.view = 'entry';
    playEntry();
  });
  $('#demo-role').addEventListener('click', () => {
    const role = state.role === 'teacher' ? 'student' : 'teacher';
    state.played.handoff = false;
    const view = NAV[role].some(([, v]) => v === state.view) ? state.view : NAV[role][0][1];
    enterWorkbench(role, view);
    toast(role === 'teacher' ? '已切换到教师视角' : '已切换到学生视角');
  });
  $('#demo-state').addEventListener('change', (e) => setDemo(e.target.value));
  $('#demo-reduce').addEventListener('change', (e) => { state.reduce = e.target.checked; render(); toast(state.reduce ? '已减少动态效果' : '已恢复动态效果'); });

  const sysReduce = window.matchMedia('(prefers-reduced-motion: reduce)');
  const applySys = () => { if (sysReduce.matches) { state.reduce = true; $('#demo-reduce').checked = true; } };
  applySys();
  sysReduce.addEventListener?.('change', applySys);

  playEntry();
  window.addEventListener('resize', () => { if (state.view === 'entry') drawTrace(false); });
})(window.XB);
