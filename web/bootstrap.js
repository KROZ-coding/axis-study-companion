(async function () {
  'use strict';

  const response = await fetch('/api/study/sample', { headers: { Accept: 'application/json' } });
  if (!response.ok) throw new Error('样例数据加载失败');
  const data = await response.json();
  const XB = window.XB = {};
  Object.assign(XB, {
    CLASS_NAME: data.className, ME: data.me, AS_OF: data.asOf, MIN_N: data.minN, RULE: data.rule,
    topics: data.topics, kps: data.kps, items: data.items, practice: data.practice, students: data.students,
    records: data.records, materials: data.materials, rawCount: data.rawCount, dupRemoved: data.dupRemoved,
    STATUS: data.status, recommended: data.recommended,
    answers: Object.fromEntries(Object.entries(data.practiceAnswers).flatMap(([sid, answers]) =>
      Object.entries(answers).map(([id, answer]) => [sid + id, answer]))),
  });
  const byId = (items) => Object.fromEntries(items.map((item) => [item.id, item]));
  XB.topicById = byId(XB.topics);
  XB.kpById = byId(XB.kps);
  XB.itemById = byId(XB.items);
  XB.studentById = byId(XB.students);
  XB.matById = byId(XB.materials);
  XB.stat = (sid, kp) => data.stats[sid][kp];
  XB.focus = (sid, kpIds = XB.kps.map((kp) => kp.id)) => data.focus[sid][kpIds.join(',')];
  XB.suggest = (stat) => data.suggestions[stat.sid][stat.kp];
  XB.missingNote = (stat) => stat.n === 0 ? '没有这个知识点的作答记录' : `只有 ${stat.n} 条记录，至少需要 ${XB.MIN_N} 条`;

  const app = document.createElement('script');
  app.src = '/assets/app.js';
  app.onerror = () => showError();
  document.body.append(app);

  function showError() {
    document.body.replaceChildren();
    const message = document.createElement('main');
    message.setAttribute('role', 'alert');
    message.className = 'state';
    message.textContent = '智慧学伴样例数据暂不可用，请检查服务后刷新。';
    document.body.append(message);
  }
})().catch((error) => {
  document.body.replaceChildren();
  const message = document.createElement('main');
  message.setAttribute('role', 'alert');
  message.className = 'state';
  message.textContent = '智慧学伴样例数据暂不可用，请检查服务后刷新。';
  document.body.append(message);
  console.error(error);
});
