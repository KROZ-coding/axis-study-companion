const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../../web/prototype/recording-reports.js'), 'utf8').replace('export function createRecordingReports', 'function createRecordingReports');
const flush = () => new Promise((resolve) => setImmediate(resolve));
function fixture() {
  const generate = {}, refresh = {};
  const root = { querySelectorAll: () => [], querySelector: (selector) => selector === '[data-report-generate]' ? generate : selector === '[data-report-refresh]' ? refresh : null };
  let context = { status: { enabled: true, course_id: 'course', class_id: 'class', goal_id: 'goal' }, user: { id: 'teacher', role: 'teacher' } };
  const api = {
    classMembers: async () => [{ user_id: 'student', display_name: '学生' }],
    goals: async () => [{ id: 'goal', title: '<script>bad</script>' }],
    call: async () => ({ items: [] }),
    reportFormats: async () => ({ export_formats: ['html', 'pdf', 'xlsx', 'json', 'csv'] }),
    exportUrl: (id, format) => `/api/v1/reports/${id}/export/${format}`,
    generateReport: async () => ({ id: 'rev', report_id: 'report', revision_number: 1, created_at: 1, snapshot: { title: '<img src=x onerror=x>', summary: { evidence: 1 }, tables: {} } }),
  };
  const sandbox = { document: { querySelector: () => root }, crypto: { randomUUID: () => 'test-key' } };
  vm.runInNewContext(`${source}\nthis.factory = createRecordingReports;`, sandbox);
  const page = sandbox.factory({ api, getContext: () => context, onChange: () => {} });
  return { api, page, generate, setContext: (value) => { context = value; } };
}

test('reports use real generation and pin all exports to the returned revision', async () => {
  const { page, generate } = fixture();
  page.render(); page.bind(); await flush(); page.bind();
  await generate.onclick();
  const html = page.render();
  assert.match(html, /&lt;img src=x onerror=x&gt;/);
  assert.doesNotMatch(html, /<img src=x/);
  assert.match(html, /&lt;script&gt;bad&lt;\/script&gt;/);
  for (const format of ['html', 'pdf', 'xlsx', 'json', 'csv']) assert.ok(html.includes(`/api/v1/reports/report/export/${format}?revision_id=rev`));
});

test('generation failure stays visible without inventing an export', async () => {
  const { api, page, generate } = fixture();
  api.generateReport = async () => { throw new Error('保存失败'); };
  page.render(); page.bind(); await flush(); page.bind(); await generate.onclick();
  assert.match(page.render(), /保存失败/);
  assert.doesNotMatch(page.render(), /revision_id=/);
});

test('role switch discards an outstanding report response', async () => {
  const { api, page, generate, setContext } = fixture();
  let resolve;
  api.generateReport = () => new Promise((done) => { resolve = done; });
  page.render(); page.bind(); await flush(); page.bind();
  const pending = generate.onclick();
  setContext({ status: { enabled: true, class_id: 'other', course_id: 'other', goal_id: 'other' }, user: { id: 'other-teacher', role: 'teacher' } });
  page.render();
  resolve({ id: 'old-revision', snapshot: { title: 'OLD PRIVATE REPORT', summary: {}, tables: {} } });
  await pending;
  assert.doesNotMatch(page.render(), /OLD PRIVATE REPORT/);
});
