// Component contract checks against the shipped bundles. SDK widgets are stubs;
// these are not a substitute for a run in the native Hermes Desktop application.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');

function h(type, props, ...children) {
  return { type, props: { ...(props || {}), children: children.length ? children : (props || {}).children } };
}
function nodes(tree) {
  if (tree === null || tree === undefined || typeof tree === 'boolean') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  if (typeof tree !== 'object') return [tree];
  if (typeof tree.type === 'function') return nodes(tree.type(tree.props));
  return [tree, ...nodes(tree.props && tree.props.children)];
}
function text(tree) { return nodes(tree).filter(n => typeof n === 'string' || typeof n === 'number').join(' '); }
const widgets = Object.fromEntries(['Card', 'CardHeader', 'CardTitle', 'CardContent', 'Badge', 'Button', 'Input', 'ScrollArea', 'SearchField', 'Separator', 'EmptyState'].map(k => [k, k]));
function web() {
  const sandbox = { window: { __HERMES_PLUGIN_SDK__: { React: { createElement: h }, components: widgets, hooks: {} },
    __HERMES_PLUGINS__: { register() {} } }, navigator: {}, setTimeout() {} };
  const source = fs.readFileSync(path.join(root, 'dashboard/dist/index.js'), 'utf8')
    .replace('window.__HERMES_PLUGINS__.register("locus-codegraph", LocusDashboardPage);',
      'window.testExports = { LocusDriftInbox, LocusCoverage, LocusSvgGraph, LocusSymbolRow, symbolTarget, repairContext, scanNoticeText };');
  vm.runInNewContext(source, sandbox);
  return sandbox.window.testExports;
}
function desktop(summary, isError = false, scope = {}, queries = []) {
  const sandbox = { ...widgets, h, jsx: (type, props) => h(type, props), jsxs: (type, props) => h(type, props),
    useState: initial => [initial, () => {}], useRef: initial => ({ current: initial }), useEffect() {},
    useValue: atom => atom.value, useQuery: options => {
      queries.push(Array.from(options.queryKey));
      return options.queryKey[1] === 'summary' ? { data: summary, isError } : { data: { nodes: [], edges: [] } };
    },
    host: { state: { cwd: { value: scope.workspace || '/workspace' }, profile: { value: scope.profile || 'default' },
      connectionId: { value: scope.connection || null } } }, queryClient: { invalidateQueries() {} }, haptic() {} };
  let source = fs.readFileSync(path.join(root, 'desktop/plugin.js'), 'utf8').replace(/import\s+\{[\s\S]*?\}\s+from\s+'[^']+'\s*/g, '');
  source = source.slice(0, source.indexOf('export default')) + '\nthis.testExports = { DesktopDriftCard, LocusDesktopPane, LocusStatusChip, MiniBlastSvg, repairContext, scanNoticeText };';
  vm.runInNewContext(source, sandbox);
  return sandbox.testExports;
}
const alert = { id: 4, symbol_name: 'format_result', edited_path: 'lib.py', reason: 'Keyword changed',
  old_signature: '(max_len=80)', new_signature: '(limit=80)', affected_callers: Array.from({ length: 8 }, (_, i) =>
    ({ path: `app${i}.py`, line: i + 10, incompatibility: "unexpected keyword 'max_len'", suggestion: 'Replace max_len= with limit=' })) };
const summary = { file_count: 30, symbol_count: 50, workspace_name: 'demo', open_drift_count: 1,
  drift_alerts: [alert], closed_drift_count: 12, closed_drift_alerts: [{ id: 1, symbol_name: 'old_helper', edited_path: 'old.py' }],
  scan_status: { truncated: true, max_files: 30, last_indexed_at: 123, parse_errors: [{ path: 'broken.py', error: 'SyntaxError line 3' }] } };

test('both shipped inboxes preserve every caller, reason and repair suggestion', () => {
  const webTree = web().LocusDriftInbox({ summary, dismissing: {}, onFocus() {}, onDismiss() {}, onCopied() {} });
  const desktopTree = desktop(summary).DesktopDriftCard({ alert, onPick() {}, onDismiss() {} });
  for (const tree of [webTree, desktopTree]) {
    for (const caller of alert.affected_callers) assert.ok(text(tree).includes(`${caller.path}:${caller.line}`));
    assert.ok(text(tree).includes("unexpected keyword 'max_len'"));
    assert.ok(text(tree).includes('Replace max_len= with limit='));
    assert.match(text(tree), /Dismiss.*does not repair/);
  }
});

test('caller navigation uses the source path, and dismissal passes only the alert id', () => {
  for (const surface of ['web', 'desktop']) {
    let picked, dismissed;
    const tree = surface === 'web' ? web().LocusDriftInbox({ summary, dismissing: {}, onFocus: value => picked = value, onDismiss: value => dismissed = value })
      : desktop(summary).DesktopDriftCard({ alert, onPick: value => picked = value, onDismiss: value => dismissed = value });
    const all = nodes(tree);
    all.find(n => n.type === 'button' && text(n).includes('app7.py:17')).props.onClick();
    all.find(n => n.type === 'Button' && /^Dismiss/.test(text(n))).props.onClick();
    assert.equal(picked, 'app7.py'); assert.equal(dismissed, 4);
  }
});

test('empty, unavailable, closed and truncated states do not imply verified repair', () => {
  const empty = { ...summary, open_drift_count: 0, drift_alerts: [] };
  assert.match(text(web().LocusDriftInbox({ summary: empty, dismissing: {} })), /does not mean tests passed/);
  assert.match(text(desktop(empty).LocusDesktopPane({ ctx: { source: 'locus' } })), /Closed does not mean tests passed/);
  for (const tree of [web().LocusDriftInbox({ summary: empty, dismissing: {} }),
    desktop(empty).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.match(text(tree), /No unresolved caller alerts are recorded/);
    assert.doesNotMatch(text(tree), /No incompatible callers/);
  }
  assert.doesNotMatch(text(web().LocusDriftInbox({ error: 'offline' })), /No open alerts/);
  assert.doesNotMatch(text(desktop(null, true).LocusDesktopPane({ ctx: { source: 'locus' } })), /No open caller alerts/);
  const capped = { ...summary, open_drift_count: 25 };
  assert.match(text(web().LocusDriftInbox({ summary: capped, dismissing: {} })), /newest 1 of 25/);
  assert.match(text(desktop(capped).LocusDesktopPane({ ctx: { source: 'locus' } })), /Showing 1 of 25/);
});

test('scan incompleteness and retained parse errors survive into both views', () => {
  for (const tree of [web().LocusCoverage({ summary }), desktop(summary).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.match(text(tree), /File limit reached \(30\)/);
    assert.match(text(tree), /broken\.py/);
    assert.match(text(tree), /previous valid symbols may be retained/);
  }
  const clean = { ...summary, scan_status: { ...summary.scan_status, parse_errors: [], truncated: false } };
  for (const tree of [web().LocusCoverage({ summary: clean }), desktop(clean).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.doesNotMatch(text(tree), /broken\.py|File limit reached/);
  }
  for (const api of [web(), desktop(summary)]) {
    const notice = api.scanNoticeText({ ok: true, ...summary.scan_status });
    assert.match(notice, /Index coverage for current diagnostics/);
    assert.doesNotMatch(notice, /could not be parsed|File limit reached|broken\.py/);
    assert.throws(() => api.scanNoticeText({ ok: false, error: 'offline' }), /offline/);
  }
});

test('skipped retained sources remain explicitly stale on both surfaces until rechecked', () => {
  const stale = { ...summary, scan_status: { last_indexed_at: 123, truncated: false, parse_errors: [],
    stale_files: [{ path: 'oversized.py', reason: 'Source exceeds the file size limit.' }] } };
  for (const tree of [web().LocusCoverage({ summary: stale }), desktop(stale).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.match(text(tree), /1 previously indexed file\(s\) could not be checked/);
    assert.match(text(tree), /coverage is incomplete/);
    assert.match(text(tree), /Retained symbols and caller evidence are stale/);
    assert.match(text(tree), /does not mean its callers are repaired/);
    assert.match(text(tree), /oversized\.py.*Source exceeds the file size limit/);
    assert.ok(nodes(tree).some(n => n.type === 'details' && n.props.open && /coverage is incomplete/.test(text(n))));
  }
  const refreshed = { ...stale, scan_status: { ...stale.scan_status, stale_files: [] } };
  for (const tree of [web().LocusCoverage({ summary: refreshed }), desktop(refreshed).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.doesNotMatch(text(tree), /oversized\.py|evidence are stale|coverage is incomplete/);
  }
  const many = { ...stale, scan_status: { ...stale.scan_status, stale_files: Array.from({ length: 25 }, (_, i) =>
    ({ path: `stale${i}.py`, reason: 'Source is now excluded.' })) } };
  for (const tree of [web().LocusCoverage({ summary: many }), desktop(many).LocusDesktopPane({ ctx: { source: 'locus' } })]) {
    assert.match(text(tree), /25 previously indexed file\(s\)/);
    assert.match(text(tree), /Showing 20 of 25 stale files/);
    assert.match(text(tree), /stale19\.py/);
    assert.doesNotMatch(text(tree), /stale20\.py|stale24\.py/);
  }
});

test('graph nodes are keyboard-operable on both surfaces', () => {
  const graph = { nodes: [{ id: 'lib.py', label: 'lib', x: 100, y: 100, role: 'focus', fan_in: 2, fan_out: 1, lines: 8 }], edges: [] };
  for (const surface of ['web', 'desktop']) {
    let picked, prevented = 0;
    const tree = surface === 'web' ? web().LocusSvgGraph({ graph, focus: 'lib.py', onSelectNode: value => picked = value })
      : desktop(summary).MiniBlastSvg({ graph, onPick: value => picked = value });
    const node = nodes(tree).find(n => n.props && n.props.role === 'button');
    assert.equal(node.props.tabIndex, 0);
    node.props.onKeyDown({ key: ' ', preventDefault() { prevented++; } });
    assert.equal(picked, 'lib.py'); assert.equal(prevented, 1);
    assert.doesNotMatch(text(tree), /COVERING TESTS/);
    if (surface === 'desktop') {
      assert.ok(nodes(tree).find(n => n.type === 'text').props.fontSize >= 28, 'labels remain readable in a 360px pane');
      assert.ok(nodes(node).some(n => n.props && n.props.className === 'locus-native-focus-ring'));
      assert.match(text(nodes(tree).find(n => n.type === 'style')), /focus-visible/);
    }
  }
});

test('repair drafts include the complete evidence and request verification', () => {
  const a = web().repairContext(alert), b = desktop(summary).repairContext(alert);
  assert.equal(a, b);
  assert.match(a, /app7\.py:17/);
  assert.match(a, /run the relevant tests/);
  assert.ok(a.includes('\n'));
  assert.equal(web().repairContext(alert, '/other/workspace'), desktop(summary).repairContext(alert, '/other/workspace'));
  assert.match(web().repairContext(alert, '/other/workspace'), /^Workspace: \/other\/workspace\n/);
});

test('uncertain callers stay visibly distinct from proven binding failures', () => {
  const uncertain = { ...alert, severity: 'warning', reason: 'Call binding changed and needs verification.',
    affected_callers: [{ path: 'dynamic.py', line: 5, analysis_status: 'needs_verification',
      incompatibility: 'The imported target is no longer statically resolved.' }] };
  const data = { ...summary, drift_alerts: [uncertain] };
  const webApi = web(), nativeApi = desktop(data);
  for (const tree of [webApi.LocusDriftInbox({ summary: data, dismissing: {} }),
    nativeApi.DesktopDriftCard({ alert: uncertain })]) {
    assert.match(text(tree), /Needs verification/);
    assert.match(text(tree), /no longer statically resolved/);
  }
  for (const draft of [webApi.repairContext(uncertain), nativeApi.repairContext(uncertain)]) {
    assert.doesNotMatch(draft, /incompatible callers/);
    assert.match(draft, /needs verification/);
  }
});

test('native cache keys isolate profiles and gateway connections sharing the same workspace', () => {
  const all = [];
  for (const scope of [{ profile: 'one', connection: 'local' }, { profile: 'two', connection: 'local' }, { profile: 'two', connection: 'remote' }]) {
    const queries = [], api = desktop(summary, false, scope, queries), ctx = { source: 'locus' };
    api.LocusDesktopPane({ ctx }); api.LocusStatusChip({ ctx });
    assert.deepEqual(queries[0], queries[2], 'pane and status chip share the same scoped summary');
    all.push(queries);
  }
  assert.equal(new Set(all.map(queries => JSON.stringify(queries[0]))).size, 3);
  assert.equal(new Set(all.map(queries => JSON.stringify(queries[1]))).size, 3);
});

test('identically named symbol rows inspect distinct exact targets and copyable references', () => {
  const api = web();
  for (const file of ['one/service.py', 'two/service.py']) {
    let selected;
    const symbol = { symbol_id: `${file}::send`, qualified_name: 'send', rel_path: file, line_start: 1, kind: 'function' };
    const row = api.LocusSymbolRow({ symbol, onInspect: value => selected = value });
    row.props.onClick();
    assert.equal(selected, `${file}::send`);
    assert.equal('@sym:' + api.symbolTarget(symbol), `@sym:${file}::send`);
    assert.match(text(row), /send/);
    assert.ok(text(row).includes(`${file}:1`));
  }
});
