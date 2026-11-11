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
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

function roleStroke(role, hasDrift) {
  if (hasDrift) return 'var(--ui-accent)'
  if (role === 'focus') return 'var(--ui-accent)'
  if (role === 'direct_dependent') return 'var(--ui-text-primary, currentColor)'
  return 'var(--ui-text-tertiary)'
}

function scanNoticeText(result) {
  if (!result || !result.ok) throw new Error((result && result.error) || 'Workspace scan failed')
  const notices = []
  if (result.parse_errors && result.parse_errors.length) {
    notices.push(`${result.parse_errors.length} source file(s) could not be parsed; any previously indexed version was retained.`)
    notices.push(...result.parse_errors.slice(0, 3).map(item => `${item.path}: ${item.error}`))
  }
  if (result.truncated) notices.push(`Scan reached the ${result.max_files} file limit; the graph is incomplete.`)
  return notices.join(' ')
}

async function insertRefChip(token) {
  haptic('tap')
  const ok = await host.composer.insertText(null, token + ' ', { mode: 'inline' })
  if (ok) {
    host.composer.focus(null)
    host.notify({ kind: 'info', message: 'Inserted ' + token + ' into composer' })
  } else {
    host.notify({ kind: 'info', message: 'Open a chat composer to insert ' + token })
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
    children: [
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
              style: { cursor: 'pointer' },
              onClick: () => onPick(n.id),
              children: [
                jsx('circle', {
                  r,
                  fill: n.role === 'focus' || n.has_drift ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)',
                  stroke: roleStroke(n.role, n.has_drift),
                  strokeWidth: 2
                }),
                jsx('text', {
                  y: r + 14,
                  textAnchor: 'middle',
                  fontSize: 13,
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

function LocusDesktopPane({ ctx }) {
  const workspace = useValue(host.state.cwd)
  const [focus, setFocus] = useState('')
  const [filter, setFilter] = useState('')
  const [scanNotice, setScanNotice] = useState('')
  const view = useRef({ workspace, mounted: true })
  view.current.workspace = workspace
  useEffect(() => {
    view.current.mounted = true
    setFocus(''); setFilter(''); setScanNotice('')
    return () => { view.current.mounted = false }
  }, [workspace])
  const wsQuery = workspace ? '?workspace=' + encodeURIComponent(workspace) : ''

  const summaryQuery = useQuery({
    queryKey: [ctx.source, 'summary', workspace],
    queryFn: () => ctx.rest('/summary' + wsQuery),
    refetchInterval: 15000
  })

  const graphQuery = useQuery({
    queryKey: [ctx.source, 'graph', workspace, focus],
    queryFn: () =>
      ctx.rest('/graph?max_nodes=24' + (workspace ? '&workspace=' + encodeURIComponent(workspace) : '') +
        (focus ? '&focus=' + encodeURIComponent(focus) : '')),
    refetchInterval: 15000
  })

  if (summaryQuery.isError) {
    return jsx('div', {
      className: 'flex h-full flex-col justify-center p-4',
      children: jsx(EmptyState, {
        title: 'Locus Codegraph backend inactive',
        description: 'Enable the Locus Codegraph Python plugin in Capabilities → Plugins to stream AST blast-radius graphs.'
      })
    })
  }

  const summary = summaryQuery.data
  const graph = graphQuery.data
  const impact = graph && graph.focus_impact
  const modules = ((summary && summary.top_modules) || []).filter(
    m => !filter || m.rel_path.toLowerCase().includes(filter.toLowerCase())
  )

  async function triggerReindex() {
    haptic('tap')
    try {
      const result = await ctx.rest('/reindex', { method: 'POST', body: { force: true, workspace } })
      const notice = scanNoticeText(result)
      queryClient.invalidateQueries({ queryKey: [ctx.source] })
      if (!view.current.mounted || view.current.workspace !== workspace) return
      setScanNotice(notice)
      host.notify({ kind: 'info', message: notice || 'Locus Codegraph workspace graph re-indexed' })
    } catch (err) {
      if (!view.current.mounted || view.current.workspace !== workspace) return
      host.notify({
        kind: 'error',
        message: 'Locus Codegraph re-index failed: ' + String((err && err.message) || err)
      })
    }
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2.5 p-3 text-xs',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between gap-2',
        children: [
          jsxs('div', {
            className: 'flex flex-col',
            children: [
              jsx('span', {
                className: 'font-semibold text-sm text-(--ui-text-secondary)',
                children: (summary && summary.workspace_name) || 'Locus Codegraph'
              }),
              jsx('span', {
                className: 'text-(--ui-text-tertiary)',
                children: summary
                  ? `${summary.file_count} files · ${summary.symbol_count} symbols · ${summary.edge_count} edges`
                  : 'Loading workspace AST…'
              })
            ]
          }),
          jsx(Button, {
            size: 'sm',
            variant: 'outline',
            onClick: () => void triggerReindex(),
            children: 'Rescan'
          })
        ]
      }),

      summary &&
        summary.open_drift_count > 0 &&
        jsxs('div', {
          className: 'rounded border border-(--ui-accent) p-2 flex items-center justify-between gap-2',
          children: [
            jsx('span', {
              className: 'text-(--ui-text-secondary) font-medium',
              children: `${summary.open_drift_count} open signature drift alert(s)`
            }),
            jsx(Button, {
              size: 'sm',
              onClick: () => void insertRefChip('@hotspots:drift'),
              children: 'Attach @hotspots:drift'
            })
          ]
        }),

      scanNotice && jsx('div', {
        role: 'status',
        style: { border: '1px solid var(--ui-stroke-secondary)', padding: 8, borderRadius: 6 },
        children: scanNotice
      }),

      jsx(MiniBlastSvg, {
        graph,
        onPick: id => {
          haptic('tap')
          setFocus(id)
        }
      }),

      impact &&
        jsxs('div', {
          className: 'rounded border border-(--ui-stroke-secondary) p-2 flex flex-col gap-1.5',
          children: [
            jsxs('div', {
              className: 'flex items-center justify-between gap-1',
              children: [
                jsx('span', {
                  className: 'font-mono font-medium text-(--ui-text-secondary) truncate',
                  children: impact.resolved_file
                }),
                jsx(Badge, {
                  children: `${impact.risk_level} · ${impact.impact_score}`
                })
              ]
            }),
            jsx('div', {
              className: 'text-(--ui-text-tertiary)',
              children: `Hop-1: ${impact.direct_dependents.length} · Transitive: ${impact.transitive_dependents.length} · Tests: ${impact.covering_tests.length}`
            }),
            jsxs('div', {
              className: 'flex items-center gap-1.5 pt-0.5',
              children: [
                jsx(Button, {
                  size: 'sm',
                  onClick: () => void insertRefChip('@impact:' + impact.resolved_file),
                  children: 'Insert @impact'
                }),
                jsx(Button, {
                  size: 'sm',
                  variant: 'outline',
                  onClick: () => setFocus(''),
                  children: 'Reset'
                })
              ]
            })
          ]
        }),

      jsx(Separator, {}),

      jsx(SearchField, {
        value: filter,
        onChange: setFilter,
        placeholder: 'Filter ranked modules…'
      }),

      jsx(ScrollArea, {
        className: 'flex-1',
        children: jsxs('div', {
          className: 'flex flex-col gap-1 pr-1',
          children: modules.map(mod =>
            jsxs(
              'div',
              {
                className:
                  'flex items-center justify-between gap-2 rounded border border-(--ui-stroke-secondary) px-2 py-1.5',
                children: [
                  jsxs('button', {
                    type: 'button',
                    className: 'flex-1 text-left truncate',
                    onClick: () => {
                      haptic('tap')
                      setFocus(mod.rel_path)
                    },
                    children: [
                      jsx('div', {
                        className: 'font-mono text-(--ui-text-secondary) truncate',
                        children: mod.rel_path
                      }),
                      jsx('div', {
                        className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
                        children: `fan-in ${mod.fan_in} · ${mod.line_count}L`
                      })
                    ]
                  }),
                  jsx(Button, {
                    size: 'sm',
                    variant: 'ghost',
                    onClick: () => void insertRefChip('@impact:' + mod.rel_path),
                    children: '@impact'
                  })
                ]
              },
              mod.rel_path
            )
          )
        })
      })
    ]
  })
}

function LocusStatusChip({ ctx }) {
  const workspace = useValue(host.state.cwd)
  const { data } = useQuery({
    queryKey: [ctx.source, 'summary', workspace],
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
        data: { placement: 'right', width: '310px' },
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
