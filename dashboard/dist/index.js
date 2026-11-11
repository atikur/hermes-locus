/* Locus Codegraph Web Dashboard Plugin Bundle — Interactive SVG Graph, Blast-Radius Radar & Drift Guard */
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
      }).catch(function () { setCopied("Copy unavailable — select the text instead"); });
    } else setCopied("Copy unavailable — select the text instead");
  }

  function repairContext(alert, workspace) {
    return (workspace ? ["Workspace: " + workspace] : []).concat([
      "Review the caller alert for " + alert.symbol_name + " in " + alert.edited_path + ".",
      "Signature: " + alert.old_signature + " → " + alert.new_signature,
      alert.reason].concat((alert.affected_callers || []).map(function (caller) {
        return caller.path + ":" + caller.line + " — " + (caller.incompatibility || "Check against the new signature") +
          (caller.suggestion ? "\n" + caller.suggestion : "");
      })).concat(["Check the suggested repairs, run the relevant tests, and rescan to verify the alerts clear."])).join("\n");
  }

  function symbolTarget(symbol) {
    return symbol.symbol_id || symbol.rel_path + "::" + symbol.qualified_name;
  }

  function scanNoticeText(result) {
    if (!result || !result.ok) throw new Error((result && result.error) || "Workspace scan failed");
    return "Rescan finished. See Index coverage for current diagnostics; run tests to verify behavior.";
  }

  function LocusSymbolRow({ symbol, onInspect }) {
    return h("button", { type: "button", className: "locus-codegraph-row-btn",
      onClick: function () { onInspect(symbolTarget(symbol)); } },
      h("div", { className: "flex items-center justify-between" },
        h("span", { className: "font-mono font-semibold" }, symbol.qualified_name),
        h("span", { className: "opacity-65" }, symbol.kind)),
      h("div", { className: "opacity-70 font-mono truncate" }, symbol.rel_path + ":" + symbol.line_start));
  }

  function LocusDriftInbox(props) {
    const summary = props.summary;
    if (!summary) return h("div", { className: "locus-codegraph-empty", role: "status" },
      props.error ? "Drift status is unavailable. Retry loading the workspace." : "Loading caller checks…");
    const alerts = summary.drift_alerts || [];
    const count = summary.open_drift_count || 0;
    const closed = summary.closed_drift_alerts || [];
    return h(Card, { className: "locus-codegraph-inbox" },
      h(CardHeader, { className: "pb-2" },
        h("div", { className: "locus-codegraph-inbox-heading" },
          h(CardTitle, { className: "text-sm font-semibold" }, "Caller checks"),
          h(Badge, { variant: count ? "destructive" : "outline" }, count ? count + " open" : "No open alerts")),
        h("p", { className: "locus-codegraph-muted" }, count
          ? "Review these call sites before continuing. Repair the source, then rescan to check the new calls."
          : "No unresolved caller alerts are recorded. Run tests to verify behavior.")),
      h(CardContent, null,
        props.stale && h("div", { className: "locus-codegraph-notice", role: "status" }, "Showing the last loaded results; refresh failed."),
        count > alerts.length && h("p", { className: "locus-codegraph-notice" },
          "Showing the newest " + alerts.length + " of " + count + " open alerts. Earlier alerts appear as these are repaired or dismissed."),
        h("div", { className: "locus-codegraph-alert-list" }, alerts.map(function (d) {
          const callers = d.affected_callers || [];
          return h("article", { key: d.id, className: "locus-codegraph-alert", "aria-label": "Open caller alert for " + d.symbol_name },
            h("div", { className: "locus-codegraph-inbox-heading" },
              h("div", null, h("strong", { className: "font-mono" }, d.symbol_name),
                h("div", { className: "locus-codegraph-muted font-mono" }, d.edited_path)),
              h(Badge, { variant: "outline" }, callers.length + " affected call" + (callers.length === 1 ? "" : "s"))),
            d.severity === "warning" && h(Badge, { variant: "outline" }, "Needs verification"),
            h("p", null, d.reason),
            h("div", { className: "locus-codegraph-signature" },
              h("div", null, h("span", { className: "locus-codegraph-muted" }, "Before "), h("code", null, d.old_signature)),
              h("div", null, h("span", { className: "locus-codegraph-muted" }, "After  "), h("code", null, d.new_signature))),
            h("ol", { className: "locus-codegraph-callers", "aria-label": "Affected call sites" }, callers.map(function (caller, index) {
              return h("li", { key: caller.path + ":" + caller.line + ":" + index },
                h("button", { type: "button", className: "locus-codegraph-location", onClick: function () { props.onFocus(caller.path); },
                  title: "Inspect dependencies for " + caller.path }, caller.path + ":" + caller.line),
                h("div", null, caller.incompatibility || "Review this call against the new signature."),
                caller.suggestion && h("div", { className: "locus-codegraph-suggestion" },
                  h("span", { className: "locus-codegraph-muted" }, "Suggested repair: "), caller.suggestion));
            })),
            !callers.length && h("p", { className: "locus-codegraph-muted" }, "Caller details are unavailable; inspect the changed symbol before dismissing."),
            h("div", { className: "locus-codegraph-alert-actions" },
              h(Button, { size: "sm", variant: "outline", onClick: function () { copyChip(repairContext(d, summary.workspace), props.onCopied); } }, "Copy repair context"),
              h(Button, { size: "sm", variant: "ghost", disabled: Boolean(props.dismissing[d.id]),
                onClick: function () { props.onDismiss(d.id); } }, props.dismissing[d.id] ? "Dismissing…" : "Dismiss alert")),
            h("p", { className: "locus-codegraph-muted" }, "Dismiss only hides this alert; it does not repair code or verify tests."));
        })),
        closed.length > 0 && h("details", { className: "locus-codegraph-closed" },
          h("summary", null, "Closed alerts · " + summary.closed_drift_count),
          h("p", { className: "locus-codegraph-muted" }, "Closed means automatically cleared or manually dismissed. It does not mean tests passed."),
          summary.closed_drift_count > closed.length && h("p", { className: "locus-codegraph-muted" }, "Showing the newest " + closed.length + " closed records."),
          h("ul", null, closed.map(function (d) { return h("li", { key: d.id },
            h("strong", { className: "font-mono" }, d.symbol_name), " · ", h("code", null, d.edited_path)); })))));
  }

  function LocusCoverage({ summary }) {
    if (!summary) return null;
    const scan = summary.scan_status || {};
    const errors = scan.parse_errors || [];
    const staleFiles = scan.stale_files || [];
    return h("div", { className: "locus-codegraph-coverage", role: "status" },
      h("span", null, "Index coverage: " + summary.file_count + " source files. "),
      scan.truncated && h("strong", null, "File limit reached (" + scan.max_files + "); results are incomplete. "),
      !scan.last_indexed_at && h("span", null, "Coverage details unavailable; rescan to refresh. "),
      scan.last_indexed_at && h("span", { className: "locus-codegraph-muted" }, "Last scan " + new Date(scan.last_indexed_at * 1000).toLocaleString() + ". "),
      h("span", { className: "locus-codegraph-muted" }, "Python gets call-binding checks; other languages use structural indexing."),
      errors.length > 0 && h("details", null,
        h("summary", null, errors.length + " file(s) could not be parsed; previous valid symbols may be retained"),
        h("ul", null, errors.map(function (item) { return h("li", { key: item.path }, h("code", null, item.path), ": ", item.error); }))),
      staleFiles.length > 0 && h("details", { open: true },
        h("summary", null, staleFiles.length + " previously indexed file(s) could not be checked; coverage is incomplete"),
        h("p", null, "Retained symbols and caller evidence are stale. Skipping a file does not mean its callers are repaired."),
        staleFiles.length > 20 && h("p", null, "Showing 20 of " + staleFiles.length + " stale files."),
        h("ul", null, staleFiles.slice(0, 20).map(function (item) { return h("li", { key: item.path }, h("code", null, item.path), ": ", item.reason); }))));
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
        SDK.fetchJSON("/api/plugins/locus-codegraph/summary")
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
          h(Badge, { variant: "destructive" }, "Locus Codegraph Drift Guard"),
          h(
            "span",
            null,
            summary.open_drift_count +
              " unresolved signature drift alert(s) in " +
              (summary.workspace_name || "workspace") +
              " — open the Locus Codegraph tab to inspect affected callers."
          )
        ),
        h("a", { href: "/locus-codegraph", className: "underline font-medium" }, "Inspect Graph →")
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
      { className: "locus-codegraph-canvas-wrap" },
      h(
        "svg",
        {
          viewBox: "0 0 880 520",
          className: "locus-codegraph-svg",
          role: "group",
          "aria-label": "Locus interactive codebase dependency and blast-radius graph",
        },
        h(
          "defs",
          null,
          h(
            "marker",
            {
              id: "locus-codegraph-arrow-normal",
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
              id: "locus-codegraph-arrow-blast",
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
              id: "locus-codegraph-arrow-cycle",
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
              "HOPS 2–3 · TRANSITIVE BLAST RADIUS & SUGGESTED TESTS"
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
            let marker = "url(#locus-codegraph-arrow-normal)";
            let cls = "";

            if (e.kind === "cycle") {
              stroke = "var(--color-destructive, #ef4444)";
              strokeOpacity = "0.85";
              strokeWidth = "2";
              marker = "url(#locus-codegraph-arrow-cycle)";
              cls = "locus-codegraph-edge-cycle";
            } else if (e.kind === "blast") {
              stroke = "var(--color-warning, #f59e0b)";
              strokeOpacity = "0.82";
              strokeWidth = "1.9";
              marker = "url(#locus-codegraph-arrow-blast)";
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
                className: "locus-codegraph-node-group",
                tabIndex: 0,
                role: "button",
                "aria-label": "Inspect " + n.id + (n.has_drift ? "; open drift alert" : ""),
                "aria-pressed": Boolean(isSelected),
                onKeyDown: function (event) {
                  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); onSelectNode(n.id); }
                },
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
                  className: "locus-codegraph-pulse-ring",
                }),
              h("circle", {
                r: r,
                fill: fill,
                fillOpacity: isSelected || n.role === "focus" ? "0.95" : "0.78",
                stroke: "var(--color-card, #0f172a)",
                strokeWidth: "2",
                className: "locus-codegraph-node-circle",
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
        { className: "locus-codegraph-legend" },
        h(
          "span",
          { className: "locus-codegraph-legend-item" },
          h("span", { className: "locus-codegraph-dot", style: { background: "var(--color-primary, #38bdf8)" } }),
          "Focused Target"
        ),
        h(
          "span",
          { className: "locus-codegraph-legend-item" },
          h("span", { className: "locus-codegraph-dot", style: { background: "var(--color-warning, #f59e0b)" } }),
          "Hop 1 Direct Dependent"
        ),
        h(
          "span",
          { className: "locus-codegraph-legend-item" },
          h("span", { className: "locus-codegraph-dot", style: { background: "var(--color-accent, #a78bfa)" } }),
          "Hop 2–3 Transitive"
        ),
        h(
          "span",
          { className: "locus-codegraph-legend-item" },
          h("span", { className: "locus-codegraph-dot", style: { background: "var(--color-success, #10b981)" } }),
          "Suggested Test"
        ),
        h(
          "span",
          { className: "locus-codegraph-legend-item" },
          h("span", { className: "locus-codegraph-dot", style: { background: "var(--color-destructive, #ef4444)" } }),
          "Cycle / Signature Drift"
        ),
        h(
          "span",
          { className: "ml-auto opacity-75" },
          "Select a file to inspect dependencies; the file list offers the same navigation"
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
    const [symbolChoices, setSymbolChoices] = useState([]);
    const [loading, setLoading] = useState(true);
    const [busyScan, setBusyScan] = useState(false);
    const [copied, setCopied] = useState("");
    const [errMsg, setErrMsg] = useState("");
    const [loadError, setLoadError] = useState("");
    const [scanNotice, setScanNotice] = useState(null);
    const [dismissing, setDismissing] = useState({});
    const [graphView, setGraphView] = useState("graph");
    const loadSequence = useRef(0);
    const loadInFlight = useRef(false);
    const symbolSearchSequence = useRef(0);
    const symbolDetailSequence = useRef(0);
    const mutationGeneration = useRef(0);
    const currentView = useRef({ workspace: workspace, focus: focus });
    currentView.current = { workspace: workspace, focus: focus };

    const loadAll = useCallback(
      function (ws, foc, background) {
        if (background && loadInFlight.current) return;
        const sequence = ++loadSequence.current;
        loadInFlight.current = true;
        if (!background) {
          setLoading(true);
          setLoadError("");
        }
        const qsWs = ws ? "?workspace=" + encodeURIComponent(ws) : "";
        const qsGraph =
          "?max_nodes=36" +
          (ws ? "&workspace=" + encodeURIComponent(ws) : "") +
          (foc ? "&focus=" + encodeURIComponent(foc) : "");

        Promise.all([
          SDK.fetchJSON("/api/plugins/locus-codegraph/summary" + qsWs),
          SDK.fetchJSON("/api/plugins/locus-codegraph/graph" + qsGraph),
          background ? Promise.resolve(null) : SDK.fetchJSON("/api/plugins/locus-codegraph/symbols?limit=20" + (ws ? "&workspace=" + encodeURIComponent(ws) : "")),
        ])
          .then(function (res) {
            if (sequence !== loadSequence.current) return;
            setLoadError("");
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
            setLoadError(String((err && err.message) || err || "Failed to load Locus API"));
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
      mutationGeneration.current++;
      setFocus("");
      setSymDetail(null);
      setSymbolChoices([]);
      setSymQuery("");
      setBusyScan(false);
      setDismissing({});
      setSummary(null);
      setGraph(null);
      setErrMsg("");
      symbolSearchSequence.current++;
      symbolDetailSequence.current++;
      return function () {
        mutationGeneration.current++;
        symbolSearchSequence.current++;
        symbolDetailSequence.current++;
      };
    }, [workspace]);

    function handleSearchSymbols(q) {
      const sequence = ++symbolSearchSequence.current;
      setSymQuery(q);
      setSymbolChoices([]);
      const qs =
        "?limit=25&q=" +
        encodeURIComponent(q) +
        (workspace ? "&workspace=" + encodeURIComponent(workspace) : "");
      SDK.fetchJSON("/api/plugins/locus-codegraph/symbols" + qs)
        .then(function (d) {
          if (sequence !== symbolSearchSequence.current) return;
          setSymList((d && d.symbols) || []);
        })
        .catch(function (err) {
          if (sequence === symbolSearchSequence.current) setErrMsg("Symbol search failed: " + String((err && err.message) || err));
        });
    }

    function handleInspectSymbol(name) {
      const sequence = ++symbolDetailSequence.current;
      setActivePanel("symbols");
      setSymDetail(null);
      setSymbolChoices([]);
      setErrMsg("");
      const qs =
        "?name=" +
        encodeURIComponent(name) +
        (workspace ? "&workspace=" + encodeURIComponent(workspace) : "");
      SDK.fetchJSON("/api/plugins/locus-codegraph/symbol" + qs)
        .then(function (d) {
          if (sequence !== symbolDetailSequence.current) return;
          if (d && d.ok) setSymDetail(d);
          else if (d && d.candidates && d.candidates.length) setSymbolChoices(d.candidates);
          else setErrMsg((d && d.error) || "Symbol could not be found in the current index.");
        })
        .catch(function (err) {
          if (sequence === symbolDetailSequence.current) setErrMsg(String((err && err.message) || err));
        });
    }

    function handleReindex() {
      const generation = mutationGeneration.current;
      setBusyScan(true);
      setErrMsg("");
      setScanNotice(null);
      const targetWs = wsInput.trim() || workspace;
      SDK.fetchJSON("/api/plugins/locus-codegraph/reindex", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workspace: targetWs, force: true }),
      })
        .then(function (res) {
          if (generation !== mutationGeneration.current) return;
          setBusyScan(false);
          setScanNotice({ workspace: res && res.root_path, text: scanNoticeText(res) });
          if (res && res.root_path) {
            setWorkspace(res.root_path);
            setWsInput(res.root_path);
            loadAll(res.root_path, currentView.current.focus);
          }
        })
        .catch(function (err) {
          if (generation !== mutationGeneration.current) return;
          setBusyScan(false);
          setErrMsg(String((err && err.message) || err));
        });
    }

    function handleResolveDrift(alertId) {
      const generation = mutationGeneration.current;
      if (dismissing[alertId]) return;
      setDismissing(function (current) { return Object.assign({}, current, { [alertId]: true }); });
      setErrMsg("");
      SDK.fetchJSON("/api/plugins/locus-codegraph/resolve-drift", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ alert_id: alertId, workspace: workspace }),
      }).then(function (res) {
        if (generation !== mutationGeneration.current) return;
        if (!res || !res.ok) throw new Error((res && res.error) || "Drift alert could not be dismissed.");
        loadAll(currentView.current.workspace, currentView.current.focus);
      }).catch(function (err) {
        if (generation === mutationGeneration.current) setErrMsg(String((err && err.message) || err));
      }).finally(function () {
        if (generation === mutationGeneration.current) setDismissing(function (current) { const next = Object.assign({}, current); delete next[alertId]; return next; });
      });
    }

    const impact = graph && graph.focus_impact;

    return h(
      "div",
      { className: "locus-codegraph-root" },

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
              h("span", { className: "text-base font-bold" }, "Locus Codegraph"),
              h(Badge, { variant: "outline" }, "Caller checks · Dependency map"),
              copied && h(Badge, { variant: "secondary", role: "status" }, copied.indexOf("Copy unavailable") === 0 ? copied : "Copied " + copied)
            ),
            h(
              "span",
              { className: "text-xs opacity-75" },
              "Catch incompatible callers while Hermes edits. Inspect the evidence, repair the source, and verify."
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
                  "aria-label": "Indexed workspace",
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
              "aria-label": "Workspace path to scan",
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

      (errMsg || loadError) &&
        h(
          Card,
          null,
          h(CardContent, { className: "py-2.5 px-4 text-xs", role: "alert" },
            errMsg || "Could not refresh the workspace: " + loadError, " ",
            h(Button, { size: "sm", variant: "outline", onClick: function () { setErrMsg(""); loadAll(workspace, focus); } }, "Retry"))
        ),

      scanNotice && scanNotice.workspace === workspace && scanNotice.text && h(Card, null,
        h(CardContent, { className: "py-2.5 px-4 text-xs", role: "status" }, scanNotice.text)),

      h(LocusDriftInbox, { summary: summary, error: loadError, stale: Boolean(loadError), dismissing: dismissing,
        onDismiss: handleResolveDrift, onCopied: function (value) { setCopied(value ? (value.indexOf("Copy unavailable") === 0 ? value : "repair context") : ""); },
        onFocus: function (path) { setFocus(path); setActivePanel("impact"); } }),
      h(LocusCoverage, { summary: summary }),

      /* KPI Strip */
      summary &&
        h(
          "div",
          { className: "locus-codegraph-kpi-grid" },
          h(
            "div",
            { className: "locus-codegraph-kpi-card" },
            h("div", { className: "locus-codegraph-kpi-value" }, summary.file_count),
            h("div", { className: "locus-codegraph-kpi-label" }, "Indexed Source Files")
          ),
          h(
            "div",
            { className: "locus-codegraph-kpi-card" },
            h("div", { className: "locus-codegraph-kpi-value" }, summary.symbol_count),
            h("div", { className: "locus-codegraph-kpi-label" }, "Indexed Symbols & Signatures")
          ),
          h(
            "div",
            { className: "locus-codegraph-kpi-card" },
            h("div", { className: "locus-codegraph-kpi-value" }, summary.edge_count),
            h("div", { className: "locus-codegraph-kpi-label" }, "Cross-File Call/Import Edges")
          ),
          h(
            "div",
            { className: "locus-codegraph-kpi-card" },
            h("div", { className: "locus-codegraph-kpi-value" }, summary.cycle_count),
            h("div", { className: "locus-codegraph-kpi-label" }, "Circular Import Chains")
          ),
          h(
            "div",
            { className: "locus-codegraph-kpi-card" },
            h(
              "div",
              {
                className: "locus-codegraph-kpi-value",
                style: summary.open_drift_count
                  ? { color: "var(--color-destructive, #ef4444)" }
                  : undefined,
              },
              summary.open_drift_count
            ),
            h("div", { className: "locus-codegraph-kpi-label" }, "Open Signature Drift Alerts")
          )
        ),

      /* Main 2-Column Workspace: Interactive SVG Canvas + Tabbed Inspector */
      h(
        "div",
        { className: "locus-codegraph-main-grid" },

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
                ? "Change impact · " + impact.resolved_file
                : "Dependency map (" + ((summary && summary.workspace_name) || "workspace") + ")"
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
            h("div", { className: "locus-codegraph-graph-toolbar", role: "group", "aria-label": "Dependency view" },
              h(Button, { size: "sm", variant: graphView === "graph" ? "default" : "outline", "aria-pressed": graphView === "graph",
                onClick: function () { setGraphView("graph"); } }, "Graph"),
              h(Button, { size: "sm", variant: graphView === "list" ? "default" : "outline", "aria-pressed": graphView === "list",
                onClick: function () { setGraphView("list"); } }, "File list"),
              graph && h("span", { className: "locus-codegraph-muted" }, "Showing " + (graph.nodes || []).length + " of " + ((summary && summary.file_count) || 0) + " indexed files")),
            loading && !graph
              ? h("div", { className: "p-10 text-center text-sm opacity-70", role: "status" }, "Loading dependency graph…")
              : graphView === "list" && graph ? h("div", { className: "locus-codegraph-scroll-list" },
                  (graph.nodes || []).map(function (node) { return h("button", { type: "button", key: node.id,
                    className: "locus-codegraph-row-btn", "aria-pressed": focus === node.id,
                    onClick: function () { setFocus(node.id); setActivePanel("impact"); } },
                    h("code", null, node.id), h("div", { className: "locus-codegraph-muted" },
                      "Incoming " + node.fan_in + " · Outgoing " + node.fan_out + (node.has_drift ? " · Open drift alert" : ""))); }))
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
                ["impact", "Change impact"],
                ["symbols", "Symbols"],
                ["health", "Import cycles"],
                ["activity", "Live Feed"],
              ].map(function (pair) {
                const id = pair[0];
                const label = pair[1];
                return h(
                  Button,
                  {
                    key: id,
                    size: "sm",
                    "aria-pressed": activePanel === id,
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
                        " · Suggested Tests: " +
                        impact.covering_tests.length
                    ),
                    impact.direct_dependents.length > 0 &&
                      h(
                        "div",
                        null,
                        h("div", { className: "font-semibold mb-1" }, "Direct Dependents (Hop 1)"),
                        h(
                          "div",
                          { className: "locus-codegraph-scroll-list" },
                          impact.direct_dependents.map(function (d) {
                            return h(
                              "button",
                              {
                                key: d.rel_path,
                                className: "locus-codegraph-row-btn",
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
                          { className: "locus-codegraph-scroll-list" },
                          impact.transitive_dependents.map(function (d) {
                            return h(
                              "button",
                              {
                                key: d.rel_path,
                                className: "locus-codegraph-row-btn",
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
                              t.rel_path
                            );
                          })
                        : h(
                            "div",
                            { className: "opacity-75" },
                            "No tests suggested by the dependency graph. This does not establish test coverage."
                          )
                    )
                  )
                : h(
                    "div",
                    { className: "flex flex-col gap-2 text-xs" },
                    h(
                      "div",
                      { className: "opacity-75 mb-1" },
                      "Modules with the most structural influence. Select one to inspect what depends on it:"
                    ),
                    h(
                      "div",
                      { className: "locus-codegraph-scroll-list" },
                      ((summary && summary.top_modules) || []).map(function (m, idx) {
                        return h(
                          "button",
                          {
                            key: m.rel_path,
                            className: "locus-codegraph-row-btn",
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
                  "aria-label": "Search symbols",
                  className: "text-xs h-8",
                }),
                symbolChoices.length > 0 && h("div", { className: "locus-codegraph-notice" },
                  h("p", null, "More than one symbol matches. Choose an exact location:"),
                  symbolChoices.map(function (choice) { return h("button", { type: "button", key: choice,
                    className: "locus-codegraph-row-btn font-mono", onClick: function () { handleInspectSymbol(choice); } }, choice); })),
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
                            copyChip("@sym:" + symbolTarget(symDetail.symbol), setCopied);
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
                    symDetail.snippet && h("pre", { className: "locus-codegraph-code-excerpt" }, symDetail.snippet)
                  ),
                h(
                  "div",
                  { className: "locus-codegraph-scroll-list" },
                  symList.map(function (s) {
                    return h(LocusSymbolRow, { key: s.symbol_id, symbol: s, onInspect: handleInspectSymbol });
                  })
                ),
                symQuery && !symList.length && h("p", { className: "locus-codegraph-muted" }, "No symbols match this search.")
              ),

            /* Panel 3: Circular Dependencies & Signature Drift Guard */
            activePanel === "health" &&
              h(
                "div",
                { className: "flex flex-col gap-3 text-xs locus-codegraph-scroll-list" },
                h("div", { className: "font-semibold mt-2" }, "Circular import chains"),
                summary && summary.cycles && summary.cycles.length > 0
                  ? summary.cycles.map(function (cyc, idx) {
                      return h(
                        "div",
                        { key: "cyc-" + idx, className: "locus-codegraph-row-btn font-mono" },
                        cyc.join(" → ")
                      );
                    })
                  : h("div", { className: "opacity-75" }, "Acyclic module graph (0 import cycles).")
              ),

            /* Panel 4: Live Session Edit & Scan Activity */
            activePanel === "activity" &&
              h(
                "div",
                { className: "locus-codegraph-scroll-list text-xs" },
                summary && summary.recent_activity && summary.recent_activity.length > 0
                  ? summary.recent_activity.map(function (ev) {
                      return h(
                        "div",
                        { key: ev.id, className: "locus-codegraph-row-btn" },
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

  window.__HERMES_PLUGINS__.register("locus-codegraph", LocusDashboardPage);
  if (typeof window.__HERMES_PLUGINS__.registerSlot === "function") {
    window.__HERMES_PLUGINS__.registerSlot("locus-codegraph", "sessions:top", LocusSessionsDriftBanner);
  }
})();
