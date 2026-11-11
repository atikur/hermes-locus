// Locus Codegraph Native Hermes Desktop Plugin (@hermes/plugin-sdk, uncompiled ESM, 100% theme-variable styled)
import {
  Badge,
  Button,
  EmptyState,
  PALETTE_AREA,
  PANES_AREA,
  ScrollArea,
  SearchField,
  Separator,
  STATUSBAR_AREAS,
  haptic,
  host,
  queryClient,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { createElement as h, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

function roleStroke(role, hasDrift) {
  if (hasDrift) return 'var(--ui-accent)'
  if (role === 'focus') return 'var(--ui-accent)'
  if (role === 'direct_dependent') return 'var(--ui-text-primary, currentColor)'
  return 'var(--ui-text-tertiary)'
}

function scanNoticeText(result) {
  if (!result || !result.ok) throw new Error((result && result.error) || 'Workspace scan failed')
  // Coverage comes from the live summary. Never leave an old POST diagnostic
  // beside newer coverage after a repair performed through another surface.
  return 'Rescan finished. See Index coverage for current diagnostics; run tests to verify behavior.'
}

async function insertRefChip(token) {
  haptic('tap')
  try {
    const ok = await host.composer.insertText(null, token + ' ', { mode: 'inline' })
    if (ok) await host.composer.focus(null)
    host.notify({ kind: 'info', message: ok ? 'Inserted ' + token + ' into composer' : 'Open a chat composer to insert ' + token })
  } catch (error) {
    host.notify({ kind: 'error', message: 'Could not insert reference: ' + String(error.message || error) })
  }
}

function MiniBlastSvg({ graph, onPick }) {
  if (!graph || !Array.isArray(graph.nodes) || graph.nodes.length === 0) {
    return null
  }
  const byId = {}
  for (const n of graph.nodes) byId[n.id] = n

  return jsx('svg', {
    viewBox: '0 0 880 520',
    className: 'w-full rounded border border-(--ui-stroke-secondary)',
    role: 'group',
    'aria-label': 'Dependency graph; use the file list for the same navigation',
    children: [
      jsx('style', { children: '.locus-native-graph-node:focus { outline: none; } .locus-native-graph-node:focus-visible .locus-native-focus-ring { opacity: 1; }' }),
      jsxs('g', {
        children: (graph.edges || []).map((e, idx) => {
          const s = byId[e.source]
          const t = byId[e.target]
          if (!s || !t) return null
          const active = e.kind === 'blast' || e.kind === 'cycle'
          return jsx(
            'line',
            {
              x1: s.x,
              y1: s.y,
              x2: t.x,
              y2: t.y,
              stroke: active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
              strokeWidth: active ? 2.2 : 1.1,
              strokeOpacity: active ? 0.9 : 0.45
            },
            'edge-' + idx
          )
        })
      }),
      jsxs('g', {
        children: graph.nodes.map(n => {
          const r = n.role === 'focus' ? 14 : 9
          return jsxs(
            'g',
            {
              transform: `translate(${n.x},${n.y})`,
              className: 'locus-native-graph-node',
              style: { cursor: 'pointer' },
              role: 'button',
              tabIndex: 0,
              'aria-label': 'Inspect ' + n.id + (n.has_drift ? '; open drift alert' : ''),
              onKeyDown: event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onPick(n.id) } },
              onClick: () => onPick(n.id),
              children: [
                jsx('circle', { r: 34, fill: 'transparent', 'aria-hidden': true }),
                jsx('circle', { r: r + 9, fill: 'none', stroke: 'var(--ui-text-primary)', strokeWidth: 5,
                  strokeDasharray: '5 3', opacity: 0, className: 'locus-native-focus-ring', 'aria-hidden': true }),
                jsx('circle', {
                  r,
                  fill: n.role === 'focus' || n.has_drift ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
                  stroke: roleStroke(n.role, n.has_drift),
                  strokeWidth: 2
                }),
                jsx('text', {
                  y: r + 30,
                  textAnchor: 'middle',
                  fontSize: 30,
                  fill: 'var(--ui-text-secondary)',
                  children: n.label
                })
              ]
            },
            n.id
          )
        })
      })
    ]
  })
}

function repairContext(alert, workspace) {
  return (workspace ? [`Workspace: ${workspace}`] : []).concat([
    `Review the caller alert for ${alert.symbol_name} in ${alert.edited_path}.`,
    `Signature: ${alert.old_signature} → ${alert.new_signature}`, alert.reason]
    .concat((alert.affected_callers || []).map(caller =>
      `${caller.path}:${caller.line} — ${caller.incompatibility || 'Check against the new signature'}` +
      (caller.suggestion ? `\n${caller.suggestion}` : '')))
    .concat(['Check the suggested repairs, run the relevant tests, and rescan to verify the alerts clear.'])).join('\n')
}

async function insertRepairContext(alert, workspace) {
  try {
    const ok = await host.composer.insertText(null, repairContext(alert, workspace) + '\n', { mode: 'inline' })
    if (ok) await host.composer.focus(null)
    host.notify({ kind: 'info', message: ok ? 'Repair context added to your draft. Review it before sending.' : 'Open a chat composer to insert repair context.' })
  } catch (error) {
    host.notify({ kind: 'error', message: 'Could not insert repair context: ' + String(error.message || error) })
  }
}

const panelStyle = { border: '1px solid var(--ui-stroke-secondary)', borderRadius: 7, padding: 9, overflowWrap: 'anywhere' }
const mutedStyle = { color: 'var(--ui-text-tertiary)', fontSize: 11, lineHeight: 1.5 }

function DesktopDriftCard({ alert, workspace, onPick, onDismiss, dismissing }) {
  const callers = alert.affected_callers || []
  return h('article', { style: { ...panelStyle, borderLeft: '3px solid var(--ui-accent)' }, 'aria-label': 'Open caller alert for ' + alert.symbol_name },
    h('strong', { className: 'font-mono' }, alert.symbol_name),
    h('div', { style: mutedStyle, className: 'font-mono' }, alert.edited_path),
    alert.severity === 'warning' && h(Badge, null, 'Needs verification'),
    h('p', { style: { margin: '6px 0' } }, alert.reason),
    h('div', { style: { ...mutedStyle, padding: 6, background: 'var(--ui-bg-secondary)' } },
      h('div', null, 'Before ', h('code', null, alert.old_signature)),
      h('div', null, 'After ', h('code', null, alert.new_signature))),
    h('details', { open: true, style: { margin: '7px 0' } },
      h('summary', { style: { cursor: 'pointer' } }, `${callers.length} affected call${callers.length === 1 ? '' : 's'}`),
      h('ol', { style: { paddingLeft: 16, maxHeight: 300, overflowY: 'auto' } }, callers.map((caller, i) =>
        h('li', { key: `${caller.path}:${caller.line}:${i}`, style: { padding: '6px 0', borderBottom: '1px solid var(--ui-stroke-secondary)' } },
          h('button', { type: 'button', className: 'font-mono', style: { textAlign: 'left', textDecoration: 'underline', overflowWrap: 'anywhere' },
            title: 'Inspect dependencies for ' + caller.path, onClick: () => onPick(caller.path) }, `${caller.path}:${caller.line}`),
          h('div', null, caller.incompatibility || 'Review this call against the new signature.'),
          caller.suggestion && h('div', { style: { marginTop: 3 } }, h('span', { style: mutedStyle }, 'Suggested repair: '), caller.suggestion))))),
    !callers.length && h('p', { style: mutedStyle }, 'Caller details unavailable; inspect the symbol before dismissing.'),
    h('div', { style: { display: 'flex', flexWrap: 'wrap', gap: 6 } },
      h(Button, { size: 'sm', onClick: () => void insertRepairContext(alert, workspace) }, 'Add repair context'),
      h(Button, { size: 'sm', variant: 'outline', disabled: dismissing, onClick: () => onDismiss(alert.id) }, dismissing ? 'Dismissing…' : 'Dismiss')),
    h('p', { style: { ...mutedStyle, margin: '6px 0 0' } }, 'Dismiss hides the alert; it does not repair code or verify tests.'))
}

function LocusDesktopPane({ ctx }) {
  const workspace = useValue(host.state.cwd)
  // REST follows the active gateway/profile. Never reuse another scope's cached alert IDs.
  const profile = useValue(host.state.profile || host.state.cwd)
  const connection = useValue(host.state.connectionId || host.state.cwd)
  const [focus, setFocus] = useState('')
  const [filter, setFilter] = useState('')
  const [scanNotice, setScanNotice] = useState('')
  const [busyScan, setBusyScan] = useState(false)
  const [error, setError] = useState('')
  const [dismissing, setDismissing] = useState({})
  const [viewMode, setViewMode] = useState('list')
  const view = useRef({ workspace, generation: 0 })
  view.current.workspace = workspace
  useEffect(() => {
    view.current.generation++
    setFocus(''); setFilter(''); setScanNotice(''); setBusyScan(false); setError(''); setDismissing({})
    return () => { view.current.generation++ }
  }, [workspace, profile, connection])
  const wsQuery = workspace ? '?workspace=' + encodeURIComponent(workspace) : ''
  const summaryQuery = useQuery({
    queryKey: [ctx.source, 'summary', connection, profile, workspace],
    queryFn: () => ctx.rest('/summary' + wsQuery),
    refetchInterval: 5000
  })
  const graphQuery = useQuery({
    queryKey: [ctx.source, 'graph', connection, profile, workspace, focus],
    queryFn: () => ctx.rest('/graph?max_nodes=24' + (workspace ? '&workspace=' + encodeURIComponent(workspace) : '') +
      (focus ? '&focus=' + encodeURIComponent(focus) : '')),
    refetchInterval: 15000
  })
  const summary = summaryQuery.data
  const graph = graphQuery.data
  const impact = graph && graph.focus_impact
  const alerts = (summary && summary.drift_alerts) || []
  const closed = (summary && summary.closed_drift_alerts) || []
  const scan = (summary && summary.scan_status) || {}
  const parseErrors = scan.parse_errors || []
  const staleFiles = scan.stale_files || []
  const modules = ((graph && graph.nodes) || []).filter(m => !filter || m.id.toLowerCase().includes(filter.toLowerCase()))
  function refresh() { setError(''); queryClient.invalidateQueries({ queryKey: [ctx.source] }) }

  async function triggerReindex() {
    if (busyScan) return
    const generation = view.current.generation
    setBusyScan(true); setError(''); setScanNotice('')
    haptic('tap')
    try {
      const result = await ctx.rest('/reindex', { method: 'POST', body: { force: true, workspace: workspace || '' } })
      const notice = scanNoticeText(result)
      refresh()
      if (view.current.generation !== generation) return
      setScanNotice(notice)
    } catch (err) {
      if (view.current.generation === generation) setError('Rescan failed: ' + String((err && err.message) || err))
    } finally {
      if (view.current.generation === generation) setBusyScan(false)
    }
  }

  async function dismissAlert(id) {
    if (dismissing[id]) return
    const generation = view.current.generation
    setDismissing(current => ({ ...current, [id]: true })); setError('')
    try {
      const result = await ctx.rest('/resolve-drift', { method: 'POST', body: { alert_id: id, workspace: workspace || '' } })
      if (!result || !result.ok) throw new Error((result && result.error) || 'Dismissal failed')
      refresh()
    } catch (err) {
      if (view.current.generation === generation) setError('Could not dismiss alert: ' + String((err && err.message) || err))
    } finally {
      if (view.current.generation === generation) setDismissing(current => { const next = { ...current }; delete next[id]; return next })
    }
  }

  return h('div', { className: 'flex h-full flex-col text-xs', style: { minWidth: 0 } },
    h('div', { style: { padding: 12, borderBottom: '1px solid var(--ui-stroke-secondary)', display: 'flex', justifyContent: 'space-between', gap: 8 } },
      h('div', { style: { minWidth: 0 } }, h('strong', { style: { fontSize: 14 } }, (summary && summary.workspace_name) || 'Locus Codegraph'),
        h('div', { style: mutedStyle }, summary ? `${summary.file_count} files · ${summary.symbol_count} symbols` : 'Loading workspace…')),
      h(Button, { size: 'sm', variant: 'outline', disabled: busyScan, onClick: () => void triggerReindex() }, busyScan ? 'Scanning…' : 'Rescan')),
    h(ScrollArea, { className: 'flex-1', style: { minHeight: 0 } },
      h('div', { style: { padding: 12, display: 'flex', flexDirection: 'column', gap: 10 } },
        (error || summaryQuery.isError || graphQuery.isError) && h('div', { style: panelStyle, role: 'alert' },
          error || (summary ? 'Refresh failed. The displayed results may be stale.' : 'Workspace data unavailable. Check the Python plugin is enabled.'),
          h('div', { style: { marginTop: 6 } }, h(Button, { size: 'sm', variant: 'outline', onClick: refresh }, 'Retry'))),
        scanNotice && h('div', { style: panelStyle, role: 'status' }, scanNotice),
        h('section', { 'aria-label': 'Caller checks', style: { display: 'flex', flexDirection: 'column', gap: 8 } },
          h('div', { style: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 6 } },
            h('strong', { style: { fontSize: 13 } }, 'Caller checks'),
            summary && h(Badge, null, `${summary.open_drift_count} open`)),
          !summary && h('p', { role: 'status', style: mutedStyle }, summaryQuery.isError ? 'Caller status unavailable.' : 'Loading caller checks…'),
          summary && !summary.open_drift_count && h('div', { style: panelStyle },
            h('strong', null, 'No open caller alerts'),
            h('p', { style: { ...mutedStyle, marginTop: 4 } }, 'No unresolved caller alerts are recorded. Run tests to verify behavior.')),
          summary && summary.open_drift_count > alerts.length && h('p', { style: mutedStyle },
            `Showing ${alerts.length} of ${summary.open_drift_count} open alerts. Earlier alerts appear as these are repaired or dismissed.`),
          alerts.map(alert => h(DesktopDriftCard, { key: alert.id, alert, workspace: summary.workspace || workspace, onPick: setFocus, onDismiss: dismissAlert, dismissing: Boolean(dismissing[alert.id]) })),
          closed.length > 0 && h('details', { style: panelStyle },
            h('summary', { style: { cursor: 'pointer' } }, `Closed alerts · ${summary.closed_drift_count}`),
            h('p', { style: mutedStyle }, 'Automatically cleared or manually dismissed. Closed does not mean tests passed.'),
            summary.closed_drift_count > closed.length && h('p', { style: mutedStyle }, `Showing the newest ${closed.length} closed records.`),
            h('ul', { style: { paddingLeft: 14 } }, closed.map(alert => h('li', { key: alert.id, style: { margin: '5px 0', overflowWrap: 'anywhere' } },
              h('strong', { className: 'font-mono' }, alert.symbol_name), h('div', { style: mutedStyle }, alert.edited_path))))),
        summary && h('details', { style: panelStyle, open: Boolean(scan.truncated || parseErrors.length || staleFiles.length) },
          h('summary', { style: { cursor: 'pointer' } }, `Index coverage · ${summary.file_count} files${scan.truncated || parseErrors.length || staleFiles.length ? ' · incomplete' : ''}`),
          scan.truncated && h('p', null, `File limit reached (${scan.max_files}); results are incomplete.`),
          !scan.last_indexed_at && h('p', { style: mutedStyle }, 'Coverage details unavailable; rescan to refresh.'),
          scan.last_indexed_at && h('p', { style: mutedStyle }, 'Last scan ' + new Date(scan.last_indexed_at * 1000).toLocaleString()),
          h('p', { style: mutedStyle }, 'Python gets call-binding checks; other languages use structural indexing.'),
          parseErrors.length > 0 && h('div', null, `${parseErrors.length} file(s) could not be parsed; previous valid symbols may be retained.`,
            h('ul', { style: { paddingLeft: 14 } }, parseErrors.map(item => h('li', { key: item.path }, h('code', null, item.path), ': ', item.error)))),
          staleFiles.length > 0 && h('div', null, `${staleFiles.length} previously indexed file(s) could not be checked; coverage is incomplete.`,
            h('p', null, 'Retained symbols and caller evidence are stale. Skipping a file does not mean its callers are repaired.'),
            staleFiles.length > 20 && h('p', null, `Showing 20 of ${staleFiles.length} stale files.`),
            h('ul', { style: { paddingLeft: 14 } }, staleFiles.slice(0, 20).map(item => h('li', { key: item.path, style: { overflowWrap: 'anywhere' } }, h('code', null, item.path), ': ', item.reason))))),
        h(Separator, null),
        h('strong', { style: { fontSize: 13 } }, 'Explore dependencies'),
        h('div', { style: { display: 'flex', gap: 6 }, role: 'group', 'aria-label': 'Dependency view' },
          h(Button, { size: 'sm', variant: viewMode === 'list' ? 'default' : 'outline', 'aria-pressed': viewMode === 'list', onClick: () => setViewMode('list') }, 'File list'),
          h(Button, { size: 'sm', variant: viewMode === 'graph' ? 'default' : 'outline', 'aria-pressed': viewMode === 'graph', onClick: () => setViewMode('graph') }, 'Graph')),
        viewMode === 'graph' && h(MiniBlastSvg, { graph, onPick: setFocus }),
        graph && h('p', { style: mutedStyle }, `Showing ${(graph.nodes || []).length} of ${(summary && summary.file_count) || 0} indexed files.`),
        impact && h('div', { style: panelStyle },
          h('strong', { className: 'font-mono' }, impact.resolved_file),
          h('p', { style: mutedStyle }, `${impact.direct_dependents.length} direct · ${impact.transitive_dependents.length} transitive · ${impact.covering_tests.length} suggested tests`),
          h('div', { style: { display: 'flex', gap: 6 } },
            h(Button, { size: 'sm', onClick: () => void insertRefChip('@impact:' + impact.resolved_file) }, 'Add @impact'),
            h(Button, { size: 'sm', variant: 'outline', onClick: () => setFocus('') }, 'Clear focus'))),
        viewMode === 'list' && h('div', null,
          h(SearchField, { value: filter, onChange: setFilter, placeholder: 'Filter visible files…', 'aria-label': 'Filter visible files' }),
          !modules.length && h('p', { style: mutedStyle }, graphQuery.isError ? 'File list unavailable.' : graph ? (filter ? 'No visible files match.' : 'No source files indexed. Scan a workspace to begin.') : 'Loading files…'),
          modules.map(mod => h('div', { key: mod.id, style: { ...panelStyle, marginTop: 5, display: 'flex', gap: 5 } },
            h('button', { type: 'button', style: { textAlign: 'left', minWidth: 0, flex: 1 }, 'aria-pressed': focus === mod.id, onClick: () => setFocus(mod.id) },
              h('div', { className: 'font-mono', style: { overflowWrap: 'anywhere' } }, mod.id),
              h('div', { style: mutedStyle }, `Incoming ${mod.fan_in} · Outgoing ${mod.fan_out}${mod.has_drift ? ' · Open drift' : ''}`)),
            h(Button, { size: 'sm', variant: 'ghost', 'aria-label': 'Add impact for ' + mod.id, onClick: () => void insertRefChip('@impact:' + mod.id) }, '@impact'))))))))
}

function LocusStatusChip({ ctx }) {
  const workspace = useValue(host.state.cwd)
  const profile = useValue(host.state.profile || host.state.cwd)
  const connection = useValue(host.state.connectionId || host.state.cwd)
  const { data } = useQuery({
    queryKey: [ctx.source, 'summary', connection, profile, workspace],
    queryFn: () => ctx.rest('/summary' + (workspace ? '?workspace=' + encodeURIComponent(workspace) : '')),
    refetchInterval: 20000
  })

  const label = data
    ? data.open_drift_count > 0
      ? `codegraph · ${data.open_drift_count} drift`
      : `codegraph · ${data.file_count} files`
    : 'codegraph'

  return jsx('button', {
    type: 'button',
    className: 'px-1.5 text-[0.6875rem] text-(--ui-text-tertiary)',
    onClick: () => {
      haptic('tap')
      queryClient.invalidateQueries({ queryKey: [ctx.source] })
      if (data) {
        host.notify({
          kind: 'info',
          message: `Locus Codegraph: ${data.file_count} files, ${data.symbol_count} symbols, ${data.open_drift_count} open drift alert(s)`
        })
      }
    },
    children: label
  })
}

export default {
  id: 'locus-codegraph',
  name: 'Locus Codegraph',
  defaultEnabled: true,
  register(ctx) {
    ctx.onEvent('plugin.locus-codegraph.graph.updated', () => {
      queryClient.invalidateQueries({ queryKey: [ctx.source] })
    })

    ctx.registerMany([
      {
        id: 'pane',
        area: PANES_AREA,
        title: 'Locus Codegraph',
        data: { placement: 'right', width: '360px' },
        render: () => jsx(LocusDesktopPane, { ctx })
      },
      {
        id: 'status-chip',
        area: STATUSBAR_AREAS.right,
        order: 125,
        render: () => jsx(LocusStatusChip, { ctx })
      },
      {
        id: 'reindex',
        area: PALETTE_AREA,
        data: {
          id: 'locus-codegraph.reindex',
          label: 'Locus Codegraph: Re-index Workspace Codebase Graph',
          keywords: ['locus', 'ast', 'graph', 'blast', 'reindex'],
          run: async () => {
            try {
              const result = await ctx.rest('/reindex', { method: 'POST', body: { force: true, workspace: host.state.cwd.get() } })
              const notice = scanNoticeText(result)
              queryClient.invalidateQueries({ queryKey: [ctx.source] })
              host.notify({ kind: 'info', message: notice || 'Locus Codegraph re-indexed' })
            } catch (err) {
              host.notify({ kind: 'error', message: 'Locus Codegraph re-index failed: ' + String((err && err.message) || err) })
            }
          }
        }
      },
      {
        id: 'insert-hotspots',
        area: PALETTE_AREA,
        data: {
          id: 'locus-codegraph.insertHotspots',
          label: 'Locus Codegraph: Insert @hotspots:all into Chat Composer',
          keywords: ['locus', 'hotspots', 'cycles', 'drift'],
          run: () => void insertRefChip('@hotspots:all')
        }
      }
    ])
  }
}
