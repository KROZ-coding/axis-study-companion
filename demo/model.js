/* 样例记录生成与规则诊断。页面上的数字全部由这里的记录计算，能回到输入。 */
(function (XB) {
  'use strict';

  // 固定种子，保证每次打开样例都一样
  let seed = 20261104;
  const rand = () => { seed = (seed * 1664525 + 1013904223) >>> 0; return seed / 4294967296; };
  const between = (a, b) => a + Math.floor(rand() * (b - a + 1));
  const pick = (arr) => arr[Math.floor(rand() * arr.length)];

  XB.CLASS_NAME = '七年级（样例）1班';
  XB.ME = 'S07'; // 学生视角与入口例题使用的样例学生
  XB.AS_OF = '2026-11-03';
  XB.MIN_N = 3; // 少于 3 条有效记录不下结论

  XB.students = Array.from({ length: 50 }, (_, i) => {
    const no = String(i + 1).padStart(2, '0');
    return { id: 'S' + no, name: '学生' + no };
  });
  XB.studentById = Object.fromEntries(XB.students.map((s) => [s.id, s]));

  // ---- 材料：每个错误类型一份讲解和一道例题；每个知识点三份练习、一个微课、一份易错整理；每个主题一份小结
  const itemsOf = (kp) => XB.items.filter((it) => it.kp === kp);
  const materials = [];
  const addM = (m) => materials.push({ id: 'M-' + String(materials.length + 1).padStart(2, '0'), forItem: null, ...m });
  XB.kps.forEach((kp) => {
    const src = '样例教材 ' + kp.ref;
    itemsOf(kp.id).forEach((it) => {
      addM({ kp: kp.id, topic: kp.topic, type: '讲解', title: it.material, source: src, forItem: it.id,
        body: [it.desc + '。', it.why, '对照：' + it.steps[it.wrong] + '　→　' + it.fix[0]] });
      addM({ kp: kp.id, topic: kp.topic, type: '例题', title: '例题：' + it.stem, source: src, forItem: it.id,
        body: [it.stem, '正确过程：' + it.fix.join('　')] });
    });
    ['基础', '变式', '综合'].forEach((lv, i) => addM({ kp: kp.id, topic: kp.topic, type: '练习', title: kp.name + ' · ' + lv + '练习',
      source: src, body: ['围绕「' + kp.name + '」的' + lv + '练习，共 ' + (6 - i) + ' 题。', '第 1 题：' + XB.practice[kp.id].stem] }));
    addM({ kp: kp.id, topic: kp.topic, type: '微课', title: kp.name + ' · 微课', source: src,
      body: ['讲解视频，约 ' + between(4, 8) + ' 分钟。样例只登记条目，不附视频文件。'] });
    addM({ kp: kp.id, topic: kp.topic, type: '易错', title: kp.name + ' · 易错整理', source: src,
      body: itemsOf(kp.id).map((it) => it.error + '：' + it.desc + '。') });
  });
  XB.topics.forEach((t, i) => addM({ kp: null, topic: t.id, type: '小结', title: t.name + ' · 单元小结',
    source: '样例教材 七上第' + '一二三'[i] + '章',
    body: ['本单元知识点：' + XB.kps.filter((k) => k.topic === t.id).map((k) => k.name).join('、') + '。'] }));
  XB.materials = materials;
  XB.matById = Object.fromEntries(materials.map((m) => [m.id, m]));

  // ---- 作答记录
  // 先给每个学生定一个样例画像，再按画像写出对错条数，让班级里各种结论都有人
  const DAY0 = Date.UTC(2026, 9, 8); // 10 月 8 日开始
  const dateOf = (d) => new Date(DAY0 + d * 864e5).toISOString().slice(0, 10);
  const shuffle = (arr) => {
    for (let i = arr.length - 1; i > 0; i--) { const j = Math.floor(rand() * (i + 1)); [arr[i], arr[j]] = [arr[j], arr[i]]; }
    return arr;
  };
  const recs = [];
  const write = (sid, kp, n, right, lastDay = 26) => {
    const used = new Set(); // 同一学生同一题同一天只写一条，去重只去掉导入时混入的重复行
    shuffle(Array.from({ length: n }, (_, k) => k < right)).forEach((correct) => {
      let item, d;
      do { item = pick(itemsOf(kp)).id; d = between(0, lastDay); } while (used.has(item + d));
      used.add(item + d);
      recs.push({ sid, kp, item, correct, date: dateOf(d) });
    });
  };
  // 每种结论对应的 [记录条数, 答对条数]，与下面的判定规则一致
  const plan = {
    ok: () => { const n = rand() < 0.15 ? 5 : 3; return [n, n === 5 ? between(4, 5) : n]; },
    shaky: () => { const n = between(3, 5); return [n, between(Math.ceil(n / 2), Math.ceil(n * 0.8) - 1)]; },
    weak: () => { const n = between(4, 6); return [n, between(0, Math.ceil(n / 2) - 1)]; },
    none: () => { const n = between(0, 2); return [n, between(0, n)]; },
  };
  XB.students.forEach((s) => {
    if (s.id === XB.ME) return;
    const r = rand();
    const order = shuffle(XB.kps.map((k) => k.id));
    const want = {};
    if (r < 0.3) {
      want[order[0]] = 'weak';
      if (rand() < 0.5) want[order[1]] = 'shaky';
    } else if (r < 0.7) {
      want[order[0]] = 'shaky';
    }
    XB.kps.forEach((kp) => {
      const [n, right] = plan[want[kp.id] || (rand() < 0.5 ? 'none' : 'ok')]();
      write(s.id, kp.id, n, right);
    });
  });
  // 入口例题的学生：记录固定写入，和入口、学生视角的说法一致
  [['K1', 2, 2], ['K2', 2, 1], ['K3', 1, 1], ['K6', 3, 3], ['K7', 1, 1], ['K8', 1, 0]]
    .forEach(([kp, n, right]) => write(XB.ME, kp, n, right, 24));
  [['Q-502', true, 4], ['Q-501', false, 7], ['Q-502', false, 13], ['Q-501', true, 16], ['Q-501', false, 21], ['Q-501', false, 26]]
    .forEach(([item, correct, d]) => recs.push({ sid: XB.ME, kp: 'K5', item, correct, date: dateOf(d) }));

  // 模拟导入时混入的重复行，按 学生+题目+日期+结果 去重
  const raw = recs.concat(Array.from({ length: Math.round(recs.length * 0.02) }, () => ({ ...pick(recs) })));
  const seen = new Set();
  const valid = raw.filter((r) => {
    const key = [r.sid, r.item, r.date, r.correct].join('|');
    return seen.has(key) ? false : (seen.add(key), true);
  });
  valid.sort((a, b) => a.date.localeCompare(b.date) || a.sid.localeCompare(b.sid));
  valid.forEach((r, i) => { r.id = 'R-' + String(i + 1).padStart(4, '0'); });
  XB.rawCount = raw.length;
  XB.records = valid;
  XB.dupRemoved = raw.length - valid.length;

  const idx = {};
  valid.forEach((r) => { ((idx[r.sid] ||= {})[r.kp] ||= []).push(r); });
  XB.recordsOf = (sid) => valid.filter((r) => r.sid === sid);

  // ---- 规则诊断
  XB.STATUS = {
    weak: { label: '薄弱', rank: 0 },
    shaky: { label: '待巩固', rank: 1 },
    none: { label: '依据不足', rank: 2 },
    ok: { label: '已掌握', rank: 3 },
  };
  XB.RULE = '有效记录少于 3 条显示“依据不足”；正确率低于 50% 为薄弱，50% 至 80% 为待巩固，80% 及以上为已掌握。';

  XB.stat = (sid, kp) => {
    const rs = (idx[sid] && idx[sid][kp]) || [];
    const n = rs.length;
    const right = rs.filter((r) => r.correct).length;
    const status = n < XB.MIN_N ? 'none' : right / n < 0.5 ? 'weak' : right / n < 0.8 ? 'shaky' : 'ok';
    const wrongs = rs.filter((r) => !r.correct);
    // 出现最多的错误；次数相同时取最近一次
    const count = {};
    let topItem = null;
    wrongs.forEach((r) => {
      count[r.item] = (count[r.item] || 0) + 1;
      if (!topItem || count[r.item] >= count[topItem]) topItem = r.item;
    });
    return { sid, kp, n, right, status, records: rs, wrongs, topItem, topCount: topItem ? count[topItem] : 0,
      lastWrong: wrongs[wrongs.length - 1] || null };
  };
  XB.rate = (st) => (st && st.n ? st.right / st.n : 1);
  XB.missingNote = (st) => (st.n === 0 ? '没有这个知识点的作答记录' : `只有 ${st.n} 条记录，至少需要 ${XB.MIN_N} 条`);

  // 在给定知识点范围内，找最需要处理的一个
  XB.focus = (sid, kpIds = XB.kps.map((k) => k.id)) => {
    const stats = kpIds.map((k) => XB.stat(sid, k));
    const need = stats.filter((s) => s.status === 'weak' || s.status === 'shaky')
      .sort((a, b) => XB.STATUS[a.status].rank - XB.STATUS[b.status].rank || XB.rate(a) - XB.rate(b) || b.n - a.n);
    const none = stats.filter((s) => s.status === 'none');
    const overall = need.length ? need[0].status : none.length === stats.length ? 'none' : 'ok';
    return { focus: need[0] || null, need, none, stats, overall };
  };

  // 建议：最常见错误对应的讲解；没有错误时给该知识点的基础练习
  XB.suggest = (st) => materials.find((m) => st.topItem && m.forItem === st.topItem && m.type === '讲解')
    || materials.find((m) => m.kp === st.kp && m.type === '练习');

  XB.recommended = {};
  XB.students.forEach((s) => {
    const f = XB.focus(s.id).focus;
    if (f) (XB.recommended[XB.suggest(f).id] ||= []).push(s.id);
  });
})(window.XB);
