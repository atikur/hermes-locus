/* Locus Web Dashboard Plugin Bundle — Interactive SVG Graph, Blast-Radius Radar & Drift Guard */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK || !window.__HERMES_PLUGINS__) return;

  const React = SDK.React;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;
  const { Card, CardHeader, CardTitle, CardContent, Badge, Button, Input } = SDK.components;

  function roleColor(role, hasDrift, inCycle) {
    if (hasDrift) return "var(--color-destructive, #ef4444)";
    if (role === "focus") return "var(--color-primary, #38bdf8)";
    if (role === "direct_dependent") return "var(--color-warning, #f59e0b)";
    if (role === "transitive_dependent") return "var(--color-accent, #a78bfa)";
    if (role === "covering_test" || role === "test") return "var(--color-success, #10b981)";
    if (inCycle) return "var(--color-destructive, #f43f5e)";
    return "var(--color-muted-foreground, #94a3b8)";
  }

  function riskBadgeVariant(risk) {
    if (risk === "CRITICAL" || risk === "HIGH") return "destructive";
    if (risk === "MEDIUM") return "secondary";
    return "outline";
  }

  function copyChip(text, setCopied) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        setCopied(text);
        setTimeout(function () {
          setCopied("");
        }, 1800);
      });
    }
  }

  /* Page-scoped slot on /sessions: surfaces open signature drift alerts right where sessions live */
  function LocusSessionsDriftBanner() {
    const [summary, setSummary] = useState(null);
    useEffect(function () {
      let alive = true;
      let inFlight = false;
      function refresh() {
        if (inFlight) return;
        inFlight = true;
        SDK.fetchJSON("/api/plugins/locus/summary")
          .then(function (d) { if (alive) setSummary(d); })
          .catch(function () {})
          .finally(function () { inFlight = false; });
      }
      refresh();
      const timer = setInterval(refresh, 5000);
      return function () {
        alive = false;
        clearInterval(timer);
      };
    }, []);

    if (!summary || !summary.open_drift_count) return null;
    return h(
      Card,
      { className: "mb-3" },
      h(
        CardContent,
        { className: "py-2.5 px-4 flex items-center justify-between gap-3 text-xs" },
        h(
          "div",
          { className: "flex items-center gap-2" },
          h(Badge, { variant: "destructive" }, "Locus Drift Guard"),
          h(
            "span",
            null,
            summary.open_drift_count +
              " unresolved signature drift alert(s) in " +
              (summary.workspace_name || "workspace") +
              " — open the Locus Graph tab to inspect affected callers."
          )
        ),
        h("a", { href: "/locus", className: "underline font-medium" }, "Inspect Graph →")
      )
    );
  }

  function LocusSvgGraph(props) {
    const graph = props.graph;
    const focus = props.focus;
    const onSelectNode = props.onSelectNode;

    if (!graph || !graph.nodes || graph.nodes.length === 0) {
      return h(
        "div",
        { className: "p-8 text-center text-sm opacity-70" },
        "No indexed source files found in this workspace yet."
      );
    }

    const nodeById = {};
    graph.nodes.forEach(function (n) {
      nodeById[n.id] = n;
    });

    const hasFocus = Boolean(graph.focus_impact && graph.focus_impact.resolved_file);

    return h(
      "div",
      { className: "locus-canvas-wrap" },
      h(
        "svg",
        {
          viewBox: "0 0 880 520",
          className: "locus-svg",
          role: "img",
          "aria-label": "Locus interactive codebase dependency and blast-radius graph",
        },
        h(
          "defs",
          null,
          h(
            "marker",
            {
              id: "locus-arrow-normal",
              viewBox: "0 0 10 10",
              refX: "9",
              refY: "5",
              markerWidth: "5",
              markerHeight: "5",
              orient: "auto-start-reverse",
            },
            h("path", { d: "M 0 1 L 10 5 L 0 9 z", fill: "currentColor", opacity: "0.38" })
          ),
          h(
            "marker",
            {
              id: "locus-arrow-blast",
              viewBox: "0 0 10 10",
              refX: "9",
              refY: "5",
              markerWidth: "6",
              markerHeight: "6",
              orient: "auto-start-reverse",
            },
            h("path", { d: "M 0 1 L 10 5 L 0 9 z", fill: "var(--color-warning, #f59e0b)" })
          ),
          h(
            "marker",
            {
              id: "locus-arrow-cycle",
              viewBox: "0 0 10 10",
              refX: "9",
              refY: "5",
              markerWidth: "6",
              markerHeight: "6",
              orient: "auto-start-reverse",
            },
            h("path", { d: "M 0 1 L 10 5 L 0 9 z", fill: "var(--color-destructive, #ef4444)" })
          )
        ),

        /* Concentric radar rings when inspecting a blast-radius target */
        hasFocus &&
          h(
            "g",
            { opacity: "0.35" },
            h("ellipse", {
              cx: "440",
              cy: "260",
              rx: "165",
              ry: "120",
              fill: "none",
              stroke: "var(--color-warning, #f59e0b)",
              strokeDasharray: "4 4",
            }),
            h(
              "text",
              {
                x: "440",
                y: "134",
                textAnchor: "middle",
                fontSize: "10",
                fill: "currentColor",
              },
              "HOP 1 · DIRECT DEPENDENTS"
            ),
            h("ellipse", {
              cx: "440",
              cy: "260",
              rx: "285",
              ry: "185",
              fill: "none",
              stroke: "var(--color-accent, #a78bfa)",
              strokeDasharray: "3 5",
            }),
            h(
              "text",
              {
                x: "440",
                y: "69",
                textAnchor: "middle",
                fontSize: "10",
                fill: "currentColor",
              },
              "HOPS 2–3 · TRANSITIVE BLAST RADIUS & COVERING TESTS"
            )
          ),

        /* Directed edges */
        h(
          "g",
          null,
          graph.edges.map(function (e, idx) {
            const s = nodeById[e.source];
            const t = nodeById[e.target];
            if (!s || !t) return null;
            const dx = t.x - s.x;
            const dy = t.y - s.y;
            const dist = Math.max(1, Math.hypot(dx, dy));
            const pad = 14;
            const x1 = s.x + (dx / dist) * pad;
            const y1 = s.y + (dy / dist) * pad;
            const x2 = t.x - (dx / dist) * (pad + 4);
            const y2 = t.y - (dy / dist) * (pad + 4);
            const mx = (x1 + x2) / 2 - (dy / dist) * 18;
            const my = (y1 + y2) / 2 + (dx / dist) * 18;

            let stroke = "currentColor";
            let strokeOpacity = hasFocus ? "0.14" : "0.24";
            let strokeWidth = "1.2";
            let marker = "url(#locus-arrow-normal)";
            let cls = "";

            if (e.kind === "cycle") {
              stroke = "var(--color-destructive, #ef4444)";
              strokeOpacity = "0.85";
              strokeWidth = "2";
              marker = "url(#locus-arrow-cycle)";
              cls = "locus-edge-cycle";
            } else if (e.kind === "blast") {
              stroke = "var(--color-warning, #f59e0b)";
              strokeOpacity = "0.82";
              strokeWidth = "1.9";
              marker = "url(#locus-arrow-blast)";
            }

            return h("path", {
              key: "e-" + idx,
              d: "M " + x1 + " " + y1 + " Q " + mx + " " + my + " " + x2 + " " + y2,
              fill: "none",
              stroke: stroke,
              strokeOpacity: strokeOpacity,
              strokeWidth: strokeWidth,
              markerEnd: marker,
              className: cls,
            });
          })
        ),

        /* Nodes */
        h(
          "g",
          null,
          graph.nodes.map(function (n) {
            const r = 7 + Math.round((n.weight || 0.2) * 9);
            const fill = roleColor(n.role, n.has_drift, n.in_cycle);
            const isSelected = focus && (focus === n.id || (graph.focus_impact && graph.focus_impact.resolved_file === n.id));
            return h(
              "g",
              {
                key: n.id,
                transform: "translate(" + n.x + "," + n.y + ")",
                className: "locus-node-group",
                onClick: function () {
                  onSelectNode(n.id);
                },
              },
              (n.has_drift || n.recently_edited || isSelected) &&
                h("circle", {
                  r: r + 6,
                  fill: "none",
                  stroke: fill,
                  strokeWidth: "1.8",
                  className: "locus-pulse-ring",
                }),
              h("circle", {
                r: r,
                fill: fill,
                fillOpacity: isSelected || n.role === "focus" ? "0.95" : "0.78",
                stroke: "var(--color-card, #0f172a)",
                strokeWidth: "2",
                className: "locus-node-circle",
              }),
              h(
                "text",
                {
                  y: r + 13,
                  textAnchor: "middle",
                  fontSize: isSelected ? "11.5" : "10.2",
                  fontWeight: isSelected || n.role === "focus" ? "700" : "500",
                  fill: "currentColor",
                },
                n.label
              ),
              h(
                "title",
                null,
                n.id +
                  " · fan-in: " +
                  n.fan_in +
                  " · fan-out: " +
                  n.fan_out +
                  " · " +
                  n.lines +
                  " lines"
              )
            );
          })
        )
      ),
      h(
        "div",
        { className: "locus-legend" },
        h(
          "span",
          { className: "locus-legend-item" },
          h("span", { className: "locus-dot", style: { background: "var(--color-primary, #38bdf8)" } }),
          "Focused Target"
        ),
        h(
          "span",
          { className: "locus-legend-item" },
          h("span", { className: "locus-dot", style: { background: "var(--color-warning, #f59e0b)" } }),
          "Hop 1 Direct Dependent"
        ),
        h(
          "span",
          { className: "locus-legend-item" },
          h("span", { className: "locus-dot", style: { background: "var(--color-accent, #a78bfa)" } }),
          "Hop 2–3 Transitive"
        ),
        h(
          "span",
          { className: "locus-legend-item" },
          h("span", { className: "locus-dot", style: { background: "var(--color-success, #10b981)" } }),
          "Covering Test"
        ),
        h(
          "span",
          { className: "locus-legend-item" },
          h("span", { className: "locus-dot", style: { background: "var(--color-destructive, #ef4444)" } }),
          "Cycle / Signature Drift"
        ),
        h(
          "span",
          { className: "ml-auto opacity-75" },
          "Click any module node to compute live transitive blast radius"
        )
      )
    );
  }

  function LocusDashboardPage() {
    const [workspace, setWorkspace] = useState("");
    const [wsInput, setWsInput] = useState("");
    const [focus, setFocus] = useState("");
    const [activePanel, setActivePanel] = useState("impact");
    const [summary, setSummary] = useState(null);
    const [graph, setGraph] = useState(null);
    const [symQuery, setSymQuery] = useState("");
    const [symList, setSymList] = useState([]);
    const [symDetail, setSymDetail] = useState(null);
    const [loading, setLoading] = useState(true);
    const [busyScan, setBusyScan] = useState(false);
    const [copied, setCopied] = useState("");
    const [errMsg, setErrMsg] = useState("");
    const loadSequence = useRef(0);
    const loadInFlight = useRef(false);
    const symbolSearchSequence = useRef(0);
    const symbolDetailSequence = useRef(0);

    const loadAll = useCallback(
      function (ws, foc, background) {
        if (background && loadInFlight.current) return;
        const sequence = ++loadSequence.current;
        loadInFlight.current = true;
        if (!background) {
          setLoading(true);
          setErrMsg("");
        }
        const qsWs = ws ? "?workspace=" + encodeURIComponent(ws) : "";
        const qsGraph =
          "?max_nodes=36" +
          (ws ? "&workspace=" + encodeURIComponent(ws) : "") +
          (foc ? "&focus=" + encodeURIComponent(foc) : "");

        Promise.all([
          SDK.fetchJSON("/api/plugins/locus/summary" + qsWs),
          SDK.fetchJSON("/api/plugins/locus/graph" + qsGraph),
          background ? Promise.resolve(null) : SDK.fetchJSON("/api/plugins/locus/symbols?limit=20" + (ws ? "&workspace=" + encodeURIComponent(ws) : "")),
        ])
          .then(function (res) {
            if (sequence !== loadSequence.current) return;
            setSummary(res[0]);
            setGraph(res[1]);
            if (!background) setSymList((res[2] && res[2].symbols) || []);
            if (!ws && res[0] && res[0].workspace) {
              setWsInput(res[0].workspace);
              setWorkspace(res[0].workspace);
            }
            setLoading(false);
          })
          .catch(function (err) {
            if (sequence !== loadSequence.current) return;
            setErrMsg(String((err && err.message) || err || "Failed to load Locus API"));
            setLoading(false);
          })
          .finally(function () {
            if (sequence === loadSequence.current) loadInFlight.current = false;
          });
      },
      []
    );

    useEffect(
      function () {
        loadAll(workspace, focus);
        const timer = setInterval(function () { loadAll(workspace, focus, true); }, 5000);
        return function () {
          clearInterval(timer);
          loadSequence.current++;
          loadInFlight.current = false;
        };
      },
      [workspace, focus, loadAll]
    );

    useEffect(function () {
      setFocus("");
      setSymDetail(null);
      setSymQuery("");
      symbolSearchSequence.current++;
      symbolDetailSequence.current++;
      return function () {
        symbolSearchSequence.current++;
        symbolDetailSequence.current++;
      };
    }, [workspace]);

    function handleSearchSymbols(q) {
      const sequence = ++symbolSearchSequence.current;
      setSymQuery(q);
      const qs =
        "?limit=25&q=" +
        encodeURIComponent(q) +
        (workspace ? "&workspace=" + encodeURIComponent(workspace) : "");
      SDK.fetchJSON("/api/plugins/locus/symbols" + qs)
        .then(function (d) {
          if (sequence !== symbolSearchSequence.current) return;
          setSymList((d && d.symbols) || []);
        })
        .catch(function () {});
    }

    function handleInspectSymbol(name) {
      const sequence = ++symbolDetailSequence.current;
      setActivePanel("symbols");
      const qs =
        "?name=" +
        encodeURIComponent(name) +
        (workspace ? "&workspace=" + encodeURIComponent(workspace) : "");
      SDK.fetchJSON("/api/plugins/locus/symbol" + qs)
        .then(function (d) {
          if (sequence !== symbolDetailSequence.current) return;
          if (d && d.ok) setSymDetail(d);
        })
        .catch(function () {});
    }

    function handleReindex() {
      setBusyScan(true);
      const targetWs = wsInput.trim() || workspace;
      SDK.fetchJSON("/api/plugins/locus/reindex", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: targetWs, force: true }),
      })
        .then(function (res) {
          setBusyScan(false);
          if (res && res.root_path) {
            setWorkspace(res.root_path);
            loadAll(res.root_path, focus);
          }
        })
        .catch(function (err) {
          setBusyScan(false);
          setErrMsg(String((err && err.message) || err));
        });
    }

    function handleResolveDrift(alertId) {
      SDK.fetchJSON("/api/plugins/locus/resolve-drift", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_id: alertId }),
      }).then(function () {
        loadAll(workspace, focus);
      });
    }

    const impact = graph && graph.focus_impact;

    return h(
      "div",
      { className: "locus-root" },

      /* Header Bar */
      h(
        Card,
        null,
        h(
          CardContent,
          { className: "py-3.5 px-4 flex flex-wrap items-center justify-between gap-3" },
          h(
            "div",
            { className: "flex flex-col gap-0.5" },
            h(
              "div",
              { className: "flex items-center gap-2" },
              h("span", { className: "text-base font-bold" }, "Locus Architectural Intelligence"),
              h(Badge, { variant: "outline" }, "AST + Blast Radius + Drift Guard"),
              copied && h(Badge, { variant: "secondary" }, "Copied " + copied)
            ),
            h(
              "span",
              { className: "text-xs opacity-75" },
              "Click any module node to trace reverse dependencies & covering tests, or copy @sym: / @impact: prompt chips."
            )
          ),
          h(
            "div",
            { className: "flex flex-wrap items-center gap-2" },
            summary &&
              summary.workspaces &&
              summary.workspaces.length > 1 &&
              h(
                "select",
                {
                  className: "text-xs px-2 py-1.5 rounded border bg-transparent",
                  value: workspace || summary.workspace || "",
                  onChange: function (ev) {
                    setFocus("");
                    setWorkspace(ev.target.value);
                    setWsInput(ev.target.value);
                  },
                },
                summary.workspaces.map(function (w) {
                  return h(
                    "option",
                    { key: w.workspace_id, value: w.root_path },
                    w.name + " (" + w.file_count + " files)"
                  );
                })
              ),
            h(Input, {
              value: wsInput,
              onChange: function (ev) {
                setWsInput(ev.target.value);
              },
              placeholder: "Workspace path…",
              className: "w-64 text-xs h-8",
            }),
            h(
              Button,
              {
                size: "sm",
                onClick: handleReindex,
                disabled: busyScan,
              },
              busyScan ? "Scanning…" : "Scan / Re-Index"
            ),
            focus &&
              h(
                Button,
                {
                  size: "sm",
                  variant: "outline",
                  onClick: function () {
                    setFocus("");
                  },
                },
                "Clear Focus"
              )
          )
        )
      ),

      errMsg &&
        h(
          Card,
          null,
          h(CardContent, { className: "py-2.5 px-4 text-xs text-red-400" }, "Locus API notice: " + errMsg)
        ),

      /* KPI Strip */
      summary &&
        h(
          "div",
          { className: "locus-kpi-grid" },
          h(
            "div",
            { className: "locus-kpi-card" },
            h("div", { className: "locus-kpi-value" }, summary.file_count),
            h("div", { className: "locus-kpi-label" }, "Indexed Source Files")
          ),
          h(
            "div",
            { className: "locus-kpi-card" },
            h("div", { className: "locus-kpi-value" }, summary.symbol_count),
            h("div", { className: "locus-kpi-label" }, "AST Symbols & Signatures")
          ),
          h(
            "div",
            { className: "locus-kpi-card" },
            h("div", { className: "locus-kpi-value" }, summary.edge_count),
            h("div", { className: "locus-kpi-label" }, "Cross-File Call/Import Edges")
          ),
          h(
            "div",
            { className: "locus-kpi-card" },
            h("div", { className: "locus-kpi-value" }, summary.cycle_count),
            h("div", { className: "locus-kpi-label" }, "Circular Import Chains")
          ),
          h(
            "div",
            { className: "locus-kpi-card" },
            h(
              "div",
              {
                className: "locus-kpi-value",
                style: summary.open_drift_count
                  ? { color: "var(--color-destructive, #ef4444)" }
                  : undefined,
              },
              summary.open_drift_count
            ),
            h("div", { className: "locus-kpi-label" }, "Open Signature Drift Alerts")
          )
        ),

      /* Main 2-Column Workspace: Interactive SVG Canvas + Tabbed Inspector */
      h(
        "div",
        { className: "locus-main-grid" },

        /* Left: Interactive SVG Graph */
        h(
          Card,
          null,
          h(
            CardHeader,
            { className: "pb-2 flex flex-row items-center justify-between" },
            h(
              CardTitle,
              { className: "text-sm font-semibold" },
              impact
                ? "Blast-Radius Radar · " + impact.resolved_file
                : "PageRank Dependency Topology (" + ((summary && summary.workspace_name) || "workspace") + ")"
            ),
            impact &&
              h(
                "div",
                { className: "flex items-center gap-2" },
                h(
                  Badge,
                  { variant: riskBadgeVariant(impact.risk_level) },
                  impact.risk_level + " RISK · " + impact.impact_score + "/100"
                ),
                h(
                  Button,
                  {
                    size: "sm",
                    variant: "outline",
                    onClick: function () {
                      copyChip("@impact:" + impact.resolved_file, setCopied);
                    },
                  },
                  "Copy @impact chip"
                )
              )
          ),
          h(
            CardContent,
            null,
            loading && !graph
              ? h("div", { className: "p-10 text-center text-sm opacity-70" }, "Computing AST topology…")
              : h(LocusSvgGraph, {
                  graph: graph,
                  focus: focus,
                  onSelectNode: function (nodeId) {
                    setFocus(nodeId);
                    setActivePanel("impact");
                  },
                })
          )
        ),

        /* Right: Inspector Panel */
        h(
          Card,
          null,
          h(
            CardHeader,
            { className: "pb-2" },
            h(
              "div",
              { className: "flex flex-wrap gap-1.5" },
              [
                ["impact", "Blast Radius"],
                ["symbols", "Symbols"],
                ["health", "Cycles & Drift"],
                ["activity", "Live Feed"],
              ].map(function (pair) {
                const id = pair[0];
                const label = pair[1];
                return h(
                  Button,
                  {
                    key: id,
                    size: "sm",
                    variant: activePanel === id ? "default" : "outline",
                    onClick: function () {
                      setActivePanel(id);
                    },
                  },
                  label
                );
              })
            )
          ),
          h(
            CardContent,
            null,

            /* Panel 1: Blast Radius & Central Modules */
            activePanel === "impact" &&
              (impact
                ? h(
                    "div",
                    { className: "flex flex-col gap-3 text-xs" },
                    h(
                      "div",
                      { className: "flex items-center justify-between" },
                      h("span", { className: "font-semibold text-sm" }, impact.resolved_file),
                      h(
                        Badge,
                        { variant: riskBadgeVariant(impact.risk_level) },
                        impact.risk_level + " (" + impact.impact_score + "/100)"
                      )
                    ),
                    h(
                      "div",
                      { className: "opacity-80" },
                      "Hop-1 Dependents: " +
                        impact.direct_dependents.length +
                        " · Transitive: " +
                        impact.transitive_dependents.length +
                        " · Covering Tests: " +
                        impact.covering_tests.length
                    ),
                    impact.direct_dependents.length > 0 &&
                      h(
                        "div",
                        null,
                        h("div", { className: "font-semibold mb-1" }, "Direct Dependents (Hop 1)"),
                        h(
                          "div",
                          { className: "locus-scroll-list" },
                          impact.direct_dependents.map(function (d) {
                            return h(
                              "button",
                              {
                                key: d.rel_path,
                                className: "locus-row-btn",
                                onClick: function () {
                                  setFocus(d.rel_path);
                                },
                              },
                              h("div", { className: "font-mono font-medium" }, d.rel_path),
                              h(
                                "div",
                                { className: "opacity-70" },
                                "fan-in " + d.fan_in + " · " + d.line_count + " lines"
                              )
                            );
                          })
                        )
                      ),
                    impact.transitive_dependents.length > 0 &&
                      h(
                        "div",
                        null,
                        h("div", { className: "font-semibold mb-1" }, "Transitive Blast Radius (Hops 2–3)"),
                        h(
                          "div",
                          { className: "locus-scroll-list" },
                          impact.transitive_dependents.map(function (d) {
                            return h(
                              "button",
                              {
                                key: d.rel_path,
                                className: "locus-row-btn",
                                onClick: function () {
                                  setFocus(d.rel_path);
                                },
                              },
                              h("div", { className: "font-mono" }, "[hop " + d.depth + "] " + d.rel_path),
                              h("div", { className: "opacity-70" }, "via " + d.via)
                            );
                          })
                        )
                      ),
                    h(
                      "div",
                      null,
                      h("div", { className: "font-semibold mb-1" }, "Recommended Verification Tests"),
                      impact.covering_tests.length > 0
                        ? impact.covering_tests.map(function (t) {
                            return h(
                              "div",
                              { key: t.rel_path, className: "font-mono py-0.5 text-emerald-400" },
                              "✓ " + t.rel_path
                            );
                          })
                        : h(
                            "div",
                            { className: "opacity-75" },
                            "No covering test files detected in reverse dependency graph."
                          )
                    )
                  )
                : h(
                    "div",
                    { className: "flex flex-col gap-2 text-xs" },
                    h(
                      "div",
                      { className: "opacity-75 mb-1" },
                      "Top PageRank Hub Modules — click any module to inspect its blast radius:"
                    ),
                    h(
                      "div",
                      { className: "locus-scroll-list" },
                      ((summary && summary.top_modules) || []).map(function (m, idx) {
                        return h(
                          "button",
                          {
                            key: m.rel_path,
                            className: "locus-row-btn",
                            onClick: function () {
                              setFocus(m.rel_path);
                            },
                          },
                          h(
                            "div",
                            { className: "flex items-center justify-between" },
                            h("span", { className: "font-mono font-semibold" }, idx + 1 + ". " + m.rel_path),
                            h(Badge, { variant: "outline" }, "fan-in " + m.fan_in)
                          ),
                          h(
                            "div",
                            { className: "opacity-70 mt-0.5" },
                            m.lang + " · " + m.line_count + " lines · PR " + (m.pagerank * 100).toFixed(1) + "%"
                          )
                        );
                      })
                    )
                  )),

            /* Panel 2: Symbol Search & Source Snippet Inspector */
            activePanel === "symbols" &&
              h(
                "div",
                { className: "flex flex-col gap-2.5 text-xs" },
                h(Input, {
                  value: symQuery,
                  onChange: function (ev) {
                    handleSearchSymbols(ev.target.value);
                  },
                  placeholder: "Search functions, classes, methods…",
                  className: "text-xs h-8",
                }),
                symDetail &&
                  symDetail.symbol &&
                  h(
                    "div",
                    { className: "flex flex-col gap-1.5 pb-2 border-b" },
                    h(
                      "div",
                      { className: "flex items-center justify-between gap-2" },
                      h("span", { className: "font-mono font-bold text-sm" }, symDetail.symbol.qualified_name),
                      h(
                        Button,
                        {
                          size: "sm",
                          variant: "outline",
                          onClick: function () {
                            copyChip("@sym:" + symDetail.symbol.qualified_name, setCopied);
                          },
                        },
                        "Copy @sym chip"
                      )
                    ),
                    h(
                      "div",
                      { className: "opacity-75 font-mono" },
                      symDetail.symbol.rel_path + ":" + symDetail.symbol.line_start + " · " + symDetail.symbol.signature
                    ),
                    h(
                      "div",
                      { className: "opacity-80" },
                      "Incoming callers: " +
                        (symDetail.callers ? symDetail.callers.length : 0) +
                        " · Outgoing calls: " +
                        (symDetail.callees ? symDetail.callees.length : 0)
                    ),
                    symDetail.snippet && h("pre", { className: "locus-code-excerpt" }, symDetail.snippet)
                  ),
                h(
                  "div",
                  { className: "locus-scroll-list" },
                  symList.map(function (s) {
                    return h(
                      "button",
                      {
                        key: s.symbol_id,
                        className: "locus-row-btn",
                        onClick: function () {
                          handleInspectSymbol(s.qualified_name);
                        },
                      },
                      h(
                        "div",
                        { className: "flex items-center justify-between" },
                        h("span", { className: "font-mono font-semibold" }, s.qualified_name),
                        h("span", { className: "opacity-65" }, s.kind)
                      ),
                      h("div", { className: "opacity-70 font-mono truncate" }, s.rel_path + ":" + s.line_start)
                    );
                  })
                )
              ),

            /* Panel 3: Circular Dependencies & Signature Drift Guard */
            activePanel === "health" &&
              h(
                "div",
                { className: "flex flex-col gap-3 text-xs locus-scroll-list" },
                h("div", { className: "font-semibold" }, "Open Signature Drift Alerts"),
                summary && summary.drift_alerts && summary.drift_alerts.length > 0
                  ? summary.drift_alerts.map(function (d) {
                      return h(
                        "div",
                        { key: d.id, className: "locus-row-btn" },
                        h(
                          "div",
                          { className: "flex items-center justify-between gap-2" },
                          h("span", { className: "font-mono font-bold" }, d.symbol_name),
                          h(
                            Button,
                            {
                              size: "sm",
                              variant: "outline",
                              onClick: function () {
                                handleResolveDrift(d.id);
                              },
                            },
                            "Dismiss"
                          )
                        ),
                        h("div", { className: "mt-1 opacity-85" }, d.reason),
                        h(
                          "div",
                          { className: "mt-1 font-mono opacity-70" },
                          d.old_signature + " → " + d.new_signature
                        )
                      );
                    })
                  : h("div", { className: "opacity-75" }, "Zero unresolved signature drift alerts."),
                h("div", { className: "font-semibold mt-2" }, "Circular Import Chains (Tarjan SCC)"),
                summary && summary.cycles && summary.cycles.length > 0
                  ? summary.cycles.map(function (cyc, idx) {
                      return h(
                        "div",
                        { key: "cyc-" + idx, className: "locus-row-btn font-mono" },
                        cyc.join(" → ")
                      );
                    })
                  : h("div", { className: "opacity-75" }, "Acyclic module graph (0 import cycles).")
              ),

            /* Panel 4: Live Session Edit & Scan Activity */
            activePanel === "activity" &&
              h(
                "div",
                { className: "locus-scroll-list text-xs" },
                summary && summary.recent_activity && summary.recent_activity.length > 0
                  ? summary.recent_activity.map(function (ev) {
                      return h(
                        "div",
                        { key: ev.id, className: "locus-row-btn" },
                        h(
                          "div",
                          { className: "flex items-center justify-between" },
                          h(Badge, { variant: "outline" }, ev.event_type),
                          h(
                            "span",
                            { className: "opacity-65" },
                            SDK.utils && SDK.utils.timeAgo ? SDK.utils.timeAgo(ev.created_at) : ""
                          )
                        ),
                        h("div", { className: "mt-1" }, ev.summary)
                      );
                    })
                  : h("div", { className: "opacity-75" }, "No graph activity recorded yet.")
              )
          )
        )
      )
    );
  }

  window.__HERMES_PLUGINS__.register("locus", LocusDashboardPage);
  if (typeof window.__HERMES_PLUGINS__.registerSlot === "function") {
    window.__HERMES_PLUGINS__.registerSlot("locus", "sessions:top", LocusSessionsDriftBanner);
  }
})();
