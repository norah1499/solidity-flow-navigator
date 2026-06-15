/* flow-progressive.js — v0.5 progressive-expansion renderer.
 *
 * Loaded by flow.html as the default v0.5 renderer. The legacy all-at-once
 * renderer (flow.js) is preserved on disk and selected via `solflow --legacy`.
 *
 * Capabilities:
 *   - Click a source line that contains an outgoing call to expand its
 *     target as a new node; click again to collapse the subtree. Modifiers
 *     attached to a visible function are auto-rendered (no click needed).
 *   - The dagre graph is rebuilt from `visibleIds` on every interaction.
 *   - Soft-blue call-name coloring + SVG edges clickable to collapse.
 *
 * Modifier placement (spec §11.6):
 *   Modifiers and their subtrees are lifted OUT of dagre entirely. After
 *   dagre lays out the body-call graph, each visible modifier is positioned
 *   manually in the upper-left of its parent function node, stacked
 *   vertically in declaration order. The modifier-to-parent arrow is drawn
 *   as a manual SVG path from the signature line (anchored via
 *   `call_site_line`) to the modifier's right edge. The earlier iter-2
 *   reversed-edge trick is gone — manual positioning is simpler and matches
 *   the spec's "upper-left, stacked" rule directly.
 *
 *   Modifier subtree placement (the modifier's body-call children, if
 *   expanded) is a Stage 1 placeholder: a simple vertical stack further
 *   left of the modifier. Stage 2 introduces the side-based dagre layout
 *   that will replace this with proper left-growing layout inheriting the
 *   modifier's left direction (§10.3).
 *
 * Call-line identification:
 *   - The serializer attaches `call_site_line` (1-indexed absolute file
 *     line of the originating call) to every non-root child dict. For
 *     modifier children, the builder populates it via a greppy lookup of
 *     the modifier name in the function header (spec §11.10).
 */

(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const ANIM_MS = 250;

  // ----- read embedded JSON ----------------------------------------------

  const dataEl = document.getElementById("flow-data");
  if (!dataEl) {
    console.error("flow-progressive.js: #flow-data not found");
    return;
  }
  const flow = JSON.parse(dataEl.textContent);

  // v0.10.0 (spec §10.2 "Full expansion"). When the Flask app was built with
  // expand_all=True (CLI `--expand-all`), the template emits
  // data-expand-all="true" on #flow-data and the renderer expands every
  // call site recursively at init instead of showing only the root.
  // Per-line edge anchoring, the §10.3 direction model, and the v0.9
  // reorder/anchor passes all apply identically — this is the same
  // progressive renderer with a different initial expansion state.
  const EXPAND_ALL = dataEl.dataset.expandAll === "true";

  // ----- DOM handles ------------------------------------------------------

  const frame = document.getElementById("graph-frame");
  const graph = document.getElementById("graph");
  const nodesLayer = document.getElementById("nodes");
  const edgesLayer = document.getElementById("edges");
  const resetButton = document.getElementById("reset-view");

  // ----- ID assignment + index -------------------------------------------

  const nodesById = new Map(); // id -> JSON node

  function indexTree(node, id) {
    node.__id = id;
    nodesById.set(id, node);
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c, i) => indexTree(c, id + "/" + i));
  }
  indexTree(flow.root, "0");

  function parentIdOf(id) {
    const i = id.lastIndexOf("/");
    return i === -1 ? null : id.slice(0, i);
  }

  function isModifierNode(node) {
    return node && node.node_type === "function" && node.is_modifier;
  }

  // ----- state ------------------------------------------------------------

  const visibleIds = new Set();
  const domById = new Map(); // visible ID -> rendered DOM element

  // sideById tracks bidirectional layout (spec §10.3). Populated for every
  // visible non-root non-modifier node. First-level children of the root
  // auto-balance by comparing vertical extent of each side's currently-
  // visible subtree (from the previous layout's nodeRects); deeper children
  // inherit their parent's side; modifier subtree descendants are always
  // "left" (inheriting the modifier's implicit upper-left placement).
  // Modifiers themselves do not participate in this model — they have a
  // separate manual upper-left placement (spec §11.6).
  const sideById = new Map(); // id -> "left" | "right"
  let lastNodeRects = new Map(); // module-level snapshot of last layout for auto-balance

  // ----- DOM rendering ---------------------------------------------------

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = text;
    return e;
  }

  // v0.6 short-form titles (spec §10.2 "Node title display"). Strip the
  // parameter type list from a canonical function signature and replace it
  // with `(...)`, e.g. `borrow(address,uint256,uint16,address)` →
  // `borrow(...)`. The full signature stays available in the rendered source
  // body below the title and in the data model (`full_name`,
  // `entry_point_invoker_canonical_name`) for routing and overload
  // disambiguation. Mirrors the same helper in flow.js (legacy renderer).
  function shortenSignature(name) {
    if (typeof name !== "string") return name;
    const i = name.indexOf("(");
    if (i === -1) return name;
    return name.slice(0, i) + "(...)";
  }

  // v0.17.0 (spec §10.2, §13.2): inline bind control on interface-call nodes.
  // When the serializer attached `node.binding`, render a small dropdown so the
  // auditor can resolve the interface into a concrete contract directly on the
  // node — the in-context counterpart to the index Bindings panel. Selecting an
  // option navigates to /bind (the same single global binding) with
  // ?next=<this flow> so the server rebuilds and the page reloads with the node
  // resolved (the expansion state is restored from localStorage on reload).
  function appendBindingControl(wrap, node) {
    if (!node.binding) return;
    const b = node.binding;
    const ctrl = el("div", "node-bind");
    ctrl.appendChild(
      el("span", "node-bind-label", "resolve " + b.interface + " →"),
    );
    const sel = el("select", "node-bind-select");
    const none = el("option", null, "— unbound —");
    none.value = "__none__";
    if (!b.bound_to) none.selected = true;
    sel.appendChild(none);
    let hasBound = false;
    (b.candidates || []).forEach((c) => {
      const o = el("option", null, c);
      o.value = c;
      if (b.bound_to === c) {
        o.selected = true;
        hasBound = true;
      }
      sel.appendChild(o);
    });
    if (b.bound_to && !hasBound) {
      const o = el("option", null, b.bound_to);
      o.value = b.bound_to;
      o.selected = true;
      sel.appendChild(o);
    }
    // Keep the graph's pan/zoom and node click handlers out of the native
    // select interaction.
    sel.addEventListener("mousedown", (e) => e.stopPropagation());
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", () => {
      window.location.href =
        "/bind/" +
        encodeURIComponent(b.interface) +
        "?contract=" +
        encodeURIComponent(sel.value) +
        "&next=" +
        encodeURIComponent(location.pathname);
    });
    ctrl.appendChild(sel);
    wrap.appendChild(ctrl);
  }

  function renderFunctionNode(node) {
    const wrap = el(
      "div",
      "node node--function" + (node.is_modifier ? " node--modifier" : ""),
    );

    // v0.7.0 title bar (spec §10.2). The short-form title lands in its own
    // row above the source body, painted on a slightly darker cream than
    // the node interior with a thin dark-ink rule separating it from the
    // body. v0.10.4 title qualification (spec §10.2): library and external
    // children are titled by the contract that DECLARES them — titling
    // SafeTransferLib.safeTransfer or IOracle.price by the Flow's invoker
    // would misstate the trust boundary. Internal children (and the root
    // and modifiers, call_kind null) keep the invoked-via qualification of
    // the v0.9 inheritance model.
    const declarerQualified = node.call_kind === "library" || node.call_kind === "external";
    const titleContract = declarerQualified
      ? node.declarer_contract_name
      : node.invoked_via_contract_name;
    const titleBar = el("div", "node-title-bar");
    titleBar.appendChild(
      el("code", null, titleContract + "." + shortenSignature(node.full_name)),
    );
    wrap.appendChild(titleBar);

    // Badges row: only emitted when at least one badge applies. An empty
    // .node-head would otherwise carry its `margin-bottom: 0.5rem` for no
    // visible content. v0.10.4 relation badges (spec §10.2): at most one of
    // `external call` / `library` / `inherited from {declarer}` — the
    // inherited badge fires only for internal relations, no longer on any
    // declarer/invoker mismatch (which mislabeled library and bound
    // interface calls as inheritance).
    const badges = el("span", "node-badges");
    if (node.is_modifier) badges.appendChild(el("span", "badge badge-modifier", "modifier"));
    if (node.invoked_via_super) badges.appendChild(el("span", "badge badge-super", "super"));
    if (node.bound_via) {
      // v0.17.0 (spec §13.2): a bound node is a cross-contract call resolved
      // into a concrete contract via a user binding. It REPLACES the generic
      // `external call` badge with a specific one naming the binding, so the
      // asserted (not statically proven, P4) nature stays visible.
      const b = el(
        "span",
        "badge badge-bound",
        "bound: " +
          node.bound_via.interface_name +
          " → " +
          node.bound_via.contract_name,
      );
      b.setAttribute(
        "title",
        "interface " +
          node.bound_via.interface_name +
          " resolved to " +
          node.bound_via.contract_name +
          " via a user binding — an asserted assumption, not a statically proven call",
      );
      badges.appendChild(b);
    } else if (node.call_kind === "external") {
      const b = el("span", "badge badge-external-call", "external call");
      b.setAttribute("title", "high-level call onto another contract or interface");
      badges.appendChild(b);
    } else if (node.call_kind === "library") {
      const b = el("span", "badge badge-library", "library");
      b.setAttribute("title", "library call — declared on " + node.declarer_contract_name);
      badges.appendChild(b);
    } else if (node.invoked_via_contract_name !== node.declarer_contract_name) {
      const b = el(
        "span",
        "badge badge-inherited",
        "inherited from " + node.declarer_contract_name,
      );
      b.setAttribute(
        "title",
        "declared on " + node.declarer_contract_name + ", reached via inheritance",
      );
      badges.appendChild(b);
    }
    if (badges.children.length > 0) {
      const head = el("div", "node-head");
      head.appendChild(badges);
      wrap.appendChild(head);
    }

    if (node.source_html) {
      // v0.7.0 line-number gutter (spec §10.2). One <span class="line-num">
      // per source line, aligned row-for-row with the <pre> via a shared
      // `line-height: 1.65`. The gutter sits BESIDE the <pre> (inside a
      // flex .node-body-wrap), not inside it, so per-line edge anchoring
      // via `.src-line[data-line=N]` `getBoundingClientRect` measurements
      // remains undisturbed — the <pre>'s internal offsetTop chain is
      // unchanged, only the surrounding .node grows vertically by the
      // title bar height (which dagre absorbs via getBoundingClientRect).
      const html = node.source_html;
      const sourceLineCount = html.split("\n").length;
      const baseLine = node.source_location.lines[0];

      const bodyWrap = el("div", "node-body-wrap");
      const gutter = el("div", "line-gutter");
      for (let i = 0; i < sourceLineCount; i++) {
        gutter.appendChild(el("span", "line-num", String(baseLine + i)));
      }
      bodyWrap.appendChild(gutter);

      const pre = el("pre", "node-body src");
      pre.innerHTML = wrapCallLines(node);
      colorizeCallNamesAcrossLines(pre, node);
      bodyWrap.appendChild(pre);

      wrap.appendChild(bodyWrap);
    }

    if (node.builtins_used && node.builtins_used.length) {
      // v0.10.4 dedup display (spec §11.7): first-appearance order with
      // occurrence counts — "require(bool,string) × 6" instead of six
      // verbatim repeats. The underlying tuple keeps order and duplicates;
      // this is a pure display choice.
      const counts = new Map();
      node.builtins_used.forEach((b) => counts.set(b, (counts.get(b) || 0) + 1));
      const parts = [];
      counts.forEach((n, b) => parts.push(n > 1 ? b + " × " + n : b));
      wrap.appendChild(el("div", "node-builtins", "builtins: " + parts.join(", ")));
    }

    appendBindingControl(wrap, node);
    return wrap;
  }

  function renderUnresolvedNode(node) {
    const wrap = el("div", "node node--unresolved node--pill");
    // v0.6.1: short-form title with full descriptor preserved in the
    // native browser tooltip via the wrap's `title` attribute. Unlike
    // FunctionNode, an Unresolved node has no source body to fall back
    // on, so the tooltip is the only place the full signature can be
    // recovered. shortenSignature is idempotent on already-short shapes
    // like `<address>.call(...)`.
    const fullDescriptor = node.descriptor || "(unknown)";
    wrap.setAttribute("title", fullDescriptor);
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    title.appendChild(el("code", null, shortenSignature(fullDescriptor)));
    head.appendChild(title);

    const badges = el("span", "node-badges");
    badges.appendChild(el("span", "badge badge-unresolved", node.reason));
    head.appendChild(badges);
    wrap.appendChild(head);
    appendBindingControl(wrap, node);
    return wrap;
  }

  function renderExternalNode(node) {
    const wrap = el("div", "node node--external node--pill");
    // v0.6.1: short-form title with full canonical signature preserved
    // in the native browser tooltip. Same motivation as Unresolved —
    // External nodes have no source body to fall back on.
    const fullName = node.target_canonical_name || node.target_function_name || "";
    if (fullName) wrap.setAttribute("title", fullName);
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    if (node.target_contract_name) {
      title.appendChild(el("code", null, shortenSignature(node.target_canonical_name)));
    } else {
      // Free function / `using for` wrapper — no declarer contract per
      // §11.10. target_canonical_name has no contract prefix but still
      // carries the (args) shape; shortenSignature preserves the
      // call-site `(...)` indicator that bare target_function_name lacks.
      title.appendChild(el("code", null, shortenSignature(node.target_canonical_name || node.target_function_name)));
      title.appendChild(document.createTextNode(" "));
      title.appendChild(el("span", "node-no-contract", "[no contract]"));
    }
    head.appendChild(title);
    head.appendChild(el("span", "node-path", node.source_path));
    wrap.appendChild(head);
    return wrap;
  }

  function renderNode(node) {
    if (node.node_type === "function") return renderFunctionNode(node);
    if (node.node_type === "unresolved") return renderUnresolvedNode(node);
    if (node.node_type === "external") return renderExternalNode(node);
    throw new Error("flow-progressive.js: unknown node_type " + node.node_type);
  }

  // ----- call-line wrapping ----------------------------------------------

  function wrapCallLines(funcNode) {
    const html = funcNode.source_html || "";
    if (!html) return "";

    const childByLine = new Map(); // lineIdx (0-based in source_code) -> [childId, ...]
    const baseLine = funcNode.source_location.lines[0];
    (funcNode.children || []).forEach((c) => {
      if (c.call_site_line == null) return;
      const rel = c.call_site_line - baseLine;
      if (rel < 0) return;
      const arr = childByLine.get(rel) || [];
      arr.push(c.__id);
      childByLine.set(rel, arr);
    });

    const lines = html.split("\n");
    const wrapped = lines.map((lineHtml, i) => {
      const ids = childByLine.get(i);
      if (ids && ids.length > 0) {
        return (
          '<span class="src-line src-line--call" data-line="' +
          i +
          '" data-child-ids="' +
          ids.join(",") +
          '">' +
          lineHtml +
          "</span>"
        );
      }
      return '<span class="src-line" data-line="' + i + '">' + lineHtml + "</span>";
    });
    return wrapped.join("\n");
  }

  // Colorize the call-name token inside each .src-line--call. Walks text
  // nodes, matches against the known children's names for that line, and
  // wraps each match in <span class="src-call-name">. Pygments often emits
  // call sites as bare text (no enclosing <span>), so DOM walking after
  // insertion is the simplest reliable approach.
  function colorizeCallNamesAcrossLines(pre, funcNode) {
    const baseLine = funcNode.source_location.lines[0];
    const namesByLine = new Map(); // lineIdx -> Set<name>
    (funcNode.children || []).forEach((c) => {
      if (c.call_site_line == null) return;
      const rel = c.call_site_line - baseLine;
      if (rel < 0) return;
      const name = nameForCall(c);
      if (!name) return;
      const set = namesByLine.get(rel) || new Set();
      set.add(name);
      namesByLine.set(rel, set);
    });
    pre.querySelectorAll(".src-line--call").forEach((lineSpan) => {
      const idx = parseInt(lineSpan.getAttribute("data-line"), 10);
      const names = namesByLine.get(idx);
      if (!names || names.size === 0) return;
      colorizeNamesInSubtree(lineSpan, names);
    });
  }

  // Return the human name of the call target for this child. Used as the
  // search key when colorizing the in-line call-name token.
  function nameForCall(child) {
    if (child.node_type === "function") return child.name;
    if (child.node_type === "external") return child.target_function_name;
    if (child.node_type === "unresolved") {
      // descriptors look like "ITransferable.foo(...)" or "<address>.call(...)" —
      // the call name token in source would be the function name only.
      // Strip "<...>." prefix and "(...)" suffix.
      const d = child.descriptor || "";
      const m = d.match(/([A-Za-z_][A-Za-z0-9_]*)\s*\(/);
      return m ? m[1] : null;
    }
    return null;
  }

  function colorizeNamesInSubtree(root, names) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const pending = [];
    while (walker.nextNode()) {
      const text = walker.currentNode;
      // Build a regex from all names (already-escaped via simple identifier shape)
      // and tag every match for replacement below.
      const matches = [];
      const content = text.textContent;
      names.forEach((name) => {
        const re = new RegExp("\\b" + escapeRegex(name) + "\\b", "g");
        let m;
        while ((m = re.exec(content)) !== null) {
          matches.push({ start: m.index, end: m.index + name.length, name });
        }
      });
      if (matches.length === 0) continue;
      matches.sort((a, b) => a.start - b.start);
      // Reject overlapping matches (keep first)
      const kept = [];
      let cursor = -1;
      matches.forEach((m) => {
        if (m.start >= cursor) {
          kept.push(m);
          cursor = m.end;
        }
      });
      pending.push({ text, content, matches: kept });
    }
    pending.forEach(({ text, content, matches }) => {
      const frag = document.createDocumentFragment();
      let pos = 0;
      matches.forEach((m) => {
        if (m.start > pos) frag.appendChild(document.createTextNode(content.slice(pos, m.start)));
        const span = document.createElement("span");
        span.className = "src-call-name";
        span.textContent = m.name;
        frag.appendChild(span);
        pos = m.end;
      });
      if (pos < content.length) frag.appendChild(document.createTextNode(content.slice(pos)));
      text.parentNode.replaceChild(frag, text);
    });
  }

  function escapeRegex(s) {
    return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  // ----- visibility mutation ---------------------------------------------

  function showNode(id) {
    if (visibleIds.has(id)) return;
    const node = nodesById.get(id);
    const dom = renderNode(node);
    dom.setAttribute("data-node-id", id);
    nodesLayer.appendChild(dom);
    domById.set(id, dom);
    visibleIds.add(id);
    assignSide(id);
  }

  function expandImplicit(id) {
    if (visibleIds.has(id)) return; // idempotent
    showNode(id);
    const node = nodesById.get(id);
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c) => {
      if (isModifierNode(c)) expandImplicit(c.__id);
    });
  }

  // v0.10.0 (spec §10.2 "Full expansion"). Recursively expand every child of
  // every function node — equivalent to clicking every expandable call site
  // in depth-first order, including the modifier auto-expand that
  // expandImplicit already does on a single function. The visibility set is
  // populated up front; a single `relayout(null)` pass after this returns
  // handles all positioning. Terminal nodes (external, unresolved) carry no
  // children, so recursion stops naturally there.
  function expandAllRecursive(id) {
    // Show this node if it isn't already, then ALWAYS recurse into its children.
    // The recurse-even-when-visible behavior matters for the v0.16.0 "Expand all"
    // control, which runs from a partially-expanded tree (the root is already
    // visible): a guard that returned early on a visible node would stop before
    // reaching the hidden descendants. At init (the --expand-all path) nothing is
    // visible yet, so this is byte-identical to the prior guarded version.
    const node = nodesById.get(id);
    if (!visibleIds.has(id)) showNode(id);
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c) => {
      expandAllRecursive(c.__id);
    });
  }

  // ----- v0.16.0 persisted expansion state (spec §10.2) -------------------
  //
  // Remember which call sites are expanded, per-Flow, in localStorage so that
  // returning to a Flow restores them. Stored as the full visible set minus the
  // root. A per-browser, client-side view convenience only — distinct from the
  // server-cookie bookmarks/viewed markers; carries no analysis meaning. The key
  // is the entry's url id (data-flow-id on #flow-data), falling back to the path.
  const STORAGE_KEY =
    "solflow:expanded:" + (dataEl.dataset.flowId || location.pathname);

  function persistExpansion() {
    try {
      const ids = [];
      visibleIds.forEach((id) => {
        if (id !== flow.root.__id) ids.push(id);
      });
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(ids));
    } catch (_err) {
      // localStorage unavailable (private mode, quota, disabled) — persistence
      // is a convenience and must never break interaction.
    }
  }

  // Re-show the call sites a prior visit saved. Returns true if any node beyond
  // the default implicit set was restored, so the caller can run the expand-all
  // side-balancing. Graceful: stale ids (source changed) and orphans whose
  // parent was not restored are skipped, and unreadable storage is a no-op.
  function restoreExpansion() {
    let raw;
    try {
      raw = window.localStorage.getItem(STORAGE_KEY);
    } catch (_err) {
      return false;
    }
    if (!raw) return false;
    let saved;
    try {
      saved = JSON.parse(raw);
    } catch (_err) {
      return false;
    }
    if (!Array.isArray(saved)) return false;
    // Ancestor-first (shallower ids before deeper) so a node's parent is already
    // visible when we show it. ID depth = number of "/" segments.
    saved.sort(
      (a, b) => String(a).split("/").length - String(b).split("/").length,
    );
    let restored = false;
    saved.forEach((id) => {
      if (typeof id !== "string") return;
      if (visibleIds.has(id)) return;
      if (!nodesById.has(id)) return; // stale id — source changed
      const parent = parentIdOf(id);
      if (parent && !visibleIds.has(parent)) return; // orphan — parent not restored
      showNode(id);
      restored = true;
    });
    return restored;
  }

  // ----- v0.10.0 Stage 1c (spec §10.3 "Expand-all balancing") -------------
  //
  // The incremental click path uses Rule 1's lastNodeRects-driven
  // auto-balance: at each first-level expansion, the lighter side wins.
  // Under --expand-all, every node is added BEFORE the first layout, so
  // assignSide reads an empty lastNodeRects for the very first first-level
  // child and ties resolve right (§10.3 item 1's documented tie-break);
  // each subsequent first-level child inherits the same empty state and
  // also lands right; the whole tree piles up on one side. The fix is a
  // single global balancing pass over MEASURED extents, performed before
  // any DOM is positioned: expand → measure (no visible apply) → balance
  // → re-run with balanced sides → apply once. Deeper-node inheritance
  // (Rule 2) and modifier placement (Rule 3) are unchanged.

  // Measure-only dagre pass: build a fresh dagre graph from the current
  // visibleIds + sideById, run dagre.layout, and return per-first-level
  // subtree vertical extents (top-to-bottom span across the subtree's
  // descendants). Mirrors the same dagre wiring relayout() uses, minus
  // the reorder/shift/manual passes and the DOM mutation — the absolute Y
  // values don't matter for balancing; only each first-level subtree's
  // OWN vertical span does, and that span is determined by dagre's BFS
  // ranking (which the reorder pass doesn't change in total span).
  function measureFirstLevelExtents() {
    const modIds = new Set();
    visibleIds.forEach((id) => {
      if (isModifierNode(nodesById.get(id))) modIds.add(id);
    });
    function inManual(id) {
      for (const mid of modIds) {
        if (id === mid || id.startsWith(mid + "/")) return true;
      }
      return false;
    }
    const zoomScale = currentScale();
    const g = new dagre.graphlib.Graph({ multigraph: false });
    g.setGraph({
      rankdir: "LR",
      nodesep: 30,
      ranksep: 80,
      marginx: 24,
      marginy: 24,
    });
    g.setDefaultEdgeLabel(() => ({}));
    visibleIds.forEach((id) => {
      if (inManual(id)) return;
      const rect = domById.get(id).getBoundingClientRect();
      g.setNode(id, {
        width: Math.max(80, Math.ceil(rect.width / zoomScale)),
        height: Math.max(40, Math.ceil(rect.height / zoomScale)),
      });
    });
    visibleIds.forEach((id) => {
      if (inManual(id)) return;
      const pid = parentIdOf(id);
      if (pid === null || !visibleIds.has(pid)) return;
      if (inManual(pid)) return;
      if (sideById.get(id) === "left") g.setEdge(id, pid);
      else g.setEdge(pid, id);
    });
    dagre.layout(g);

    const rootId = flow.root.__id;
    const extents = [];
    visibleIds.forEach((id) => {
      if (parentIdOf(id) !== rootId) return;
      if (isModifierNode(nodesById.get(id))) return;
      let minY = Infinity;
      let maxY = -Infinity;
      const prefix = id + "/";
      visibleIds.forEach((vid) => {
        if (vid !== id && !vid.startsWith(prefix)) return;
        if (!g.hasNode(vid)) return; // modifier-subtree descendants
        const n = g.node(vid);
        const top = n.y - n.height / 2;
        const bot = n.y + n.height / 2;
        if (top < minY) minY = top;
        if (bot > maxY) maxY = bot;
      });
      extents.push({ id, extent: minY === Infinity ? 0 : maxY - minY });
    });
    return extents;
  }

  // Greedy "longest-extent first to lighter side" partition (spec §10.3
  // "Expand-all balancing"). Tie-break matches incremental Rule 1: the
  // right side wins when totals are equal. Stable secondary tie-break on
  // id keeps the assignment deterministic across reloads. Returns
  // Map<firstLevelId, "left"|"right">.
  function balanceFirstLevelSides(extents) {
    const sorted = [...extents].sort((a, b) => {
      if (b.extent !== a.extent) return b.extent - a.extent;
      return a.id < b.id ? -1 : a.id > b.id ? 1 : 0;
    });
    const result = new Map();
    let leftSum = 0;
    let rightSum = 0;
    for (const { id, extent } of sorted) {
      if (rightSum <= leftSum) {
        result.set(id, "right");
        rightSum += extent;
      } else {
        result.set(id, "left");
        leftSum += extent;
      }
    }
    return result;
  }

  // Apply a balanced first-level side map to sideById. First-level branches
  // get their new side directly; deeper non-modifier-subtree descendants
  // inherit the first-level ancestor's side (Rule 2). Modifier subtree
  // descendants remain "left" (Rule 3 — unchanged). Modifier nodes
  // themselves have no sideById entry (manual placement). Idempotent: a
  // first-level id whose entry is missing from newFirstLevelSides keeps
  // its previously-assigned side (defensive — shouldn't happen since
  // measureFirstLevelExtents enumerates every first-level child).
  function applyBalancedSides(newFirstLevelSides) {
    const rootId = flow.root.__id;
    visibleIds.forEach((id) => {
      if (id === rootId) return;
      const node = nodesById.get(id);
      if (isModifierNode(node)) return;
      if (inModifierSubtree(id)) {
        sideById.set(id, "left");
        return;
      }
      let walker = id;
      while (parentIdOf(walker) !== rootId) {
        walker = parentIdOf(walker);
      }
      const newSide = newFirstLevelSides.get(walker);
      if (newSide) sideById.set(id, newSide);
    });
  }

  // v0.9 direction-model side assignment (spec §10.3).
  //
  // Three rules, applied in order, no decisions deferred to runtime
  // feasibility checks:
  //
  //   Rule 1 (first-level expansions, §10.3 item 1) — children of the
  //   root auto-balance by vertical extent: measure each side's
  //   currently-sided non-modifier-subtree descendants of the root
  //   (lastNodeRects snapshot), place the new child on the side with
  //   less extent. Ties → right.
  //
  //   Rule 2 (deeper expansions, §10.3 item 2) — inherit the immediate
  //   parent's side. Once a subtree starts on a side at first level, all
  //   descendants stay on that side. v0.5 inheritance restored after
  //   v0.6's per-parent auto-balance proved disorienting for chained
  //   call sequences (zigzag across sides broke the eye's chain).
  //
  //   Rule 3 (modifier subtree descendants, §10.3 item 3) — modifier
  //   itself has no sideById entry (manual upper-left placement,
  //   §11.6). Modifier subtree descendants are treated as if the
  //   modifier were a left-side parent: they inherit "left" structurally
  //   and extend leftward from the modifier's position.
  //
  // The "no retroactive reposition" invariant from v0.5/v0.6 is
  // preserved: sideById entries are write-once. Auto-balance recomputes
  // its metric at each first-level expansion to reflect the current
  // visual state, but does not move previously-placed first-level
  // subtrees across sides.
  function assignSide(id) {
    if (id === flow.root.__id) return;
    const node = nodesById.get(id);
    if (isModifierNode(node)) return; // modifier itself: manual placement
    if (sideById.has(id)) return; // idempotent

    // Rule 3: any descendant of a modifier is structurally left.
    if (inModifierSubtree(id)) {
      sideById.set(id, "left");
      return;
    }

    const pid = parentIdOf(id);

    // Rule 1: first-level child of root → auto-balance by extent.
    if (pid === flow.root.__id) {
      const lExt = rootSideExtent("left");
      const rExt = rootSideExtent("right");
      sideById.set(id, rExt <= lExt ? "right" : "left");
      return;
    }

    // Rule 2: deeper expansion → inherit parent.
    sideById.set(id, sideById.get(pid) || "right");
  }

  // Root-only side-extent metric for Rule 1 (spec §10.3 item 1).
  // Measures the vertical span of each side's currently-sided
  // descendants of the root, excluding modifier subtrees (which sit
  // upper-left and are not part of the left/right balance). Returns 0
  // when no descendants exist on that side yet (first expansion of the
  // session → ties resolve right per the spec).
  function rootSideExtent(side) {
    let minY = Infinity;
    let maxY = -Infinity;
    sideById.forEach((s, vid) => {
      if (s !== side) return;
      if (inModifierSubtree(vid)) return;
      const r = lastNodeRects.get(vid);
      if (!r) return;
      if (r.top < minY) minY = r.top;
      if (r.top + r.h > maxY) maxY = r.top + r.h;
    });
    return minY === Infinity ? 0 : maxY - minY;
  }

  // Walk up tree: is `id` inside any modifier's subtree (or itself a modifier)?
  function inModifierSubtree(id) {
    let walker = id;
    while (walker !== null) {
      if (isModifierNode(nodesById.get(walker))) return true;
      walker = parentIdOf(walker);
    }
    return false;
  }

  // Collapse `id` and every visible descendant. Tree-position IDs make
  // descendant detection lexical. Fades the removed DOM out, then detaches
  // it on the transition end.
  function collapse(id) {
    if (id === flow.root.__id) return; // safety: root can't be closed
    if (!visibleIds.has(id)) return;
    const prefix = id + "/";
    const toRemove = [];
    visibleIds.forEach((vid) => {
      if (vid === id || vid.startsWith(prefix)) toRemove.push(vid);
    });
    toRemove.forEach((rid) => {
      const dom = domById.get(rid);
      if (!dom) return;
      d3.select(dom)
        .interrupt()
        .transition()
        .duration(ANIM_MS)
        .style("opacity", "0")
        .on("end", function () {
          this.remove();
        });
      domById.delete(rid);
      visibleIds.delete(rid);
      sideById.delete(rid);
    });
  }

  // After any visible-set mutation that affects line expansion state, refresh
  // the .src-line--expanded class on every visible function node's call lines.
  // A line is "expanded" iff at least one of its children is currently visible.
  function refreshExpandedLineState() {
    domById.forEach((dom, id) => {
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return;
      dom.querySelectorAll(".src-line--call").forEach((span) => {
        const ids = (span.getAttribute("data-child-ids") || "").split(",").filter(Boolean);
        const anyVisible = ids.some((cid) => visibleIds.has(cid));
        span.classList.toggle("src-line--expanded", anyVisible);
      });
    });
  }

  // ----- click handlers --------------------------------------------------

  // Call-line click: toggle. If ANY of the line's children is currently
  // visible (via a non-implicit click, i.e. not auto-modifier), collapse
  // those visible children. Otherwise expand all of the line's children.
  nodesLayer.addEventListener("click", (e) => {
    const target = e.target.closest(".src-line--call");
    if (!target) return;
    const ids = (target.getAttribute("data-child-ids") || "").split(",").filter(Boolean);
    if (ids.length === 0) return;
    const expanded = ids.filter((cid) => visibleIds.has(cid));
    const snapshot = snapshotPositions();
    if (expanded.length > 0) {
      expanded.forEach((cid) => collapse(cid));
      refreshExpandedLineState();
      layoutBox = relayout(snapshot);
      // Collapse never pans (spec §10.2 item 2).
    } else {
      const fresh = ids.filter((cid) => !visibleIds.has(cid));
      ids.forEach((cid) => expandImplicit(cid));
      refreshExpandedLineState();
      layoutBox = relayout(snapshot);
      panNewNodesIntoView(fresh);
    }
    persistExpansion(); // v0.16.0: remember this expansion across visits
  });

  // Edge-click delegation lives further down, after `edgesGroup` is
  // initialized. Each rendered path carries `data-child-id` = the visual
  // arrow target (tree child for regular edges, modifier for reversed
  // edges); clicking it collapses that node's subtree.

  function snapshotPositions() {
    const snap = new Map();
    domById.forEach((dom, id) => {
      snap.set(id, {
        left: parseFloat(dom.style.left) || 0,
        top: parseFloat(dom.style.top) || 0,
      });
    });
    return snap;
  }

  // v0.10.4 minimal pan after expansion (spec §10.2 item 2). When the union
  // rect of the newly materialized nodes is not fully inside #graph-frame,
  // pan by the minimal translation that reveals it — animated, zoom level
  // untouched. Without this, a child expanded near the viewport edge lands
  // entirely off-screen with no visual feedback (Morpho Blue borrow at
  // narrow widths in the v0.10.3 eval: child at x=761 in a 721 px frame).
  // Clamp order gives the union's top-left priority when it is larger than
  // the frame, so the title bar and incoming edge are what gets revealed.
  // Relayout sets new nodes' style.left/top synchronously (the fade-in
  // branch), so the rects are readable immediately after relayout returns.
  function panNewNodesIntoView(newIds) {
    if (!newIds || newIds.length === 0) return;
    let left = Infinity;
    let top = Infinity;
    let right = -Infinity;
    let bottom = -Infinity;
    newIds.forEach((id) => {
      const dom = domById.get(id);
      if (!dom) return;
      const l = parseFloat(dom.style.left) || 0;
      const t = parseFloat(dom.style.top) || 0;
      left = Math.min(left, l);
      top = Math.min(top, t);
      right = Math.max(right, l + dom.offsetWidth);
      bottom = Math.max(bottom, t + dom.offsetHeight);
    });
    if (left === Infinity) return;

    // Graph coords → frame viewport coords through the current transform.
    const t = d3.zoomTransform(frame);
    const vx0 = left * t.k + t.x;
    const vy0 = top * t.k + t.y;
    const vx1 = right * t.k + t.x;
    const vy1 = bottom * t.k + t.y;
    const fr = frame.getBoundingClientRect();
    const pad = 16;

    let dxv = 0;
    let dyv = 0;
    if (vx1 > fr.width - pad) dxv = fr.width - pad - vx1;
    if (vx0 + dxv < pad) dxv = pad - vx0; // top-left wins if union > frame
    if (vy1 > fr.height - pad) dyv = fr.height - pad - vy1;
    if (vy0 + dyv < pad) dyv = pad - vy0;
    if (dxv === 0 && dyv === 0) return;

    // translateBy takes deltas in pre-scale coordinates.
    frameSel
      .transition()
      .duration(ANIM_MS)
      .call(zoom.translateBy, dxv / t.k, dyv / t.k);
  }

  // ----- edge anchoring --------------------------------------------------

  // Return both x-edges and the y-center of a source line within parent.
  function lineOffsetInParent(parentDom, lineIdx) {
    const span = parentDom.querySelector('.src-line[data-line="' + lineIdx + '"]');
    if (!span) return null;
    return {
      xLeft: 0,
      xRight: parentDom.offsetWidth,
      y: span.offsetTop + span.offsetHeight / 2,
    };
  }

  // ----- layout -----------------------------------------------------------

  let edgesGroup = edgesLayer.querySelector("g.edges-group");
  if (!edgesGroup) {
    edgesGroup = document.createElementNS(SVG_NS, "g");
    edgesGroup.setAttribute("class", "edges-group");
    edgesLayer.appendChild(edgesGroup);
  }

  // Edge-click delegation (real wiring; the sentinel above is just a label
  // so an earlier reader knows where the wiring lives).
  edgesGroup.addEventListener("click", (e) => {
    const path = e.target.closest("path.edge");
    if (!path) return;
    const childId = path.getAttribute("data-child-id");
    if (!childId) return;
    const snapshot = snapshotPositions();
    collapse(childId);
    refreshExpandedLineState();
    layoutBox = relayout(snapshot);
    persistExpansion(); // v0.16.0: remember this collapse across visits
  });

  const line = d3
    .line()
    .x((p) => p.x)
    .y((p) => p.y)
    .curve(d3.curveBasis);

  // Modifier placement constants (spec §11.6).
  const MODIFIER_GAP_X = 24; // px between modifier's right edge and parent's left edge
  const MODIFIER_GAP_Y = 8;  // px between stacked modifiers / subtree nodes

  function relayout(oldPositions) {
    graph.dataset.state = "measuring";

    const currentZoomScale = currentScale();

    // Classify each visible id: modifiers and their descendants are
    // positioned manually (not via dagre); everything else goes into dagre.
    const modifierIds = new Set();
    visibleIds.forEach((id) => {
      if (isModifierNode(nodesById.get(id))) modifierIds.add(id);
    });
    function inManualLayout(id) {
      for (const mid of modifierIds) {
        if (id === mid || id.startsWith(mid + "/")) return true;
      }
      return false;
    }

    function isReversedEdge(v, w) {
      // Tree-position prefix check: if w is a descendant of v in the tree,
      // the dagre edge (v, w) is in its natural direction. Otherwise the
      // edge was registered as (child, parent) to place child on the left.
      return !w.startsWith(v + "/");
    }

    function measureDom(id) {
      const dom = domById.get(id);
      const rect = dom.getBoundingClientRect();
      return {
        w: Math.max(80, Math.ceil(rect.width / currentZoomScale)),
        h: Math.max(40, Math.ceil(rect.height / currentZoomScale)),
      };
    }

    const g = new dagre.graphlib.Graph({ multigraph: false });
    g.setGraph({
      rankdir: "LR",
      nodesep: 30,
      ranksep: 80,
      marginx: 24,
      marginy: 24,
    });
    g.setDefaultEdgeLabel(() => ({}));

    visibleIds.forEach((id) => {
      if (inManualLayout(id)) return;
      const { w, h } = measureDom(id);
      g.setNode(id, { width: w, height: h });
    });

    visibleIds.forEach((id) => {
      if (inManualLayout(id)) return;
      const pid = parentIdOf(id);
      if (pid === null || !visibleIds.has(pid)) return;
      if (inManualLayout(pid)) return; // modifier-subtree children stay in manual layout
      const side = sideById.get(id);
      if (side === "left") {
        g.setEdge(id, pid); // reversed: dagre's LR layout places child left of parent
      } else {
        g.setEdge(pid, id);
      }
    });

    dagre.layout(g);

    // v0.9.1 (spec §10.3): within-rank source-line ordering, grouped by
    // parent, with cascading inter-group order and parent-anchored Y
    // placement. dagre's barycenter optimization overrides the source-line
    // order Layer 2 establishes at the data-model boundary (v0.6.1's
    // children.sort by call_site_line in builder._process_calls). The
    // symptom: when a parent has several body callees at the same rank
    // whose call sites span non-contiguous source lines, dagre may place a
    // late-source-line callee above earlier ones, and the edge from the
    // earlier line then crosses the edges to its in-between siblings.
    // Concrete case: Morpho _accrueInterest's rank-2 children (borrowRate
    // line 487, wTaylorCompounded 488, wMulDown 488, toUint128 489+) —
    // dagre placed toUint128 above borrowRate, crossing wTaylorCompounded
    // and wMulDown.
    //
    // Fix has three dimensions:
    //
    //   (a) INTRA-GROUP order — within each parent's children at a rank,
    //   re-sequence by call_site_line ascending (stable; null-line
    //   entries sort after).
    //
    //   (b) INTER-GROUP order — when a rank holds children of multiple
    //   parents, order the groups by their parent's finalized Y from the
    //   previous rank. Cascade top-down so depth N's parent Y is settled
    //   BEFORE depth N+1 is reordered. This stops dagre's pre-reorder
    //   envelopes from inverting against newly-repositioned parents.
    //
    //   (c) Y PLACEMENT — anchor each child at its parent's call-site-line
    //   Y (the same per-line anchor pts[0] uses for edge rendering), then
    //   run one order-preserving forward separation sweep so adjacent
    //   children clear each other by NODESEP. Anchoring keeps edges level
    //   with their call site; the forward sweep guarantees no overlap
    //   without reordering, so the crossing fix from (a) and (b) is
    //   preserved. Stage 1b's alternative (pack all groups consecutively
    //   from the rank's topmost Y) ordered groups correctly but pulled
    //   late-parent groups to the top of the rank, producing near-
    //   vertical edges. Anchor-then-separate replaces top-packing.
    //
    // CRITICAL: groups are NEVER interleaved across parents. A flat source-
    // line sort across a multi-parent rank trades same-rank crossings for
    // new cross-parent crossings; per-parent grouping is the correct rule
    // (spec §10.3).
    //
    // Top-down processing: bucket dagre nodes by tree depth, process
    // depths shallow-to-deep, so each parent's Y is finalized before its
    // children are reordered. Within a depth level, sub-bucket by dagre x
    // (LR rankdir = one rank per x).
    //
    // Edge polylines were routed by dagre using pre-reorder positions; we
    // translate each polyline uniformly by the tree-child's reorder delta
    // so the child end of the polyline lands at the child's new Y. The
    // parent end of the polyline is overridden to the parent's signature
    // line at render time (pts[0] override), so misalignment at the parent
    // end is invisible.
    //
    // Modifier subtree descendants are NOT in dagre (inManualLayout) — the
    // manual placement pass below already iterates Layer-2-ordered
    // children, so modifier subtree ordering is already correct.
    const reorderDelta = new Map();
    g.nodes().forEach((id) => reorderDelta.set(id, 0));
    (function reorderRanksBySourceLine() {
      const NODESEP = g.graph().nodesep || 30;
      // Build tree-depth map for every dagre node. Root (and any defensive
      // missing-parent case) gets depth 0; everything else accumulates
      // 1 + parent's depth via lexical-prefix parentIdOf.
      const depthById = new Map();
      function depth(id) {
        if (depthById.has(id)) return depthById.get(id);
        const pid = parentIdOf(id);
        if (pid === null || !g.hasNode(pid)) {
          depthById.set(id, 0);
          return 0;
        }
        const d = 1 + depth(pid);
        depthById.set(id, d);
        return d;
      }
      g.nodes().forEach((id) => depth(id));
      // Bucket by depth so we can process shallow-to-deep (each rank's
      // parent-Y values are then finalized before children are reordered).
      const byDepth = new Map();
      g.nodes().forEach((id) => {
        const d = depthById.get(id);
        const arr = byDepth.get(d) || [];
        arr.push(id);
        byDepth.set(d, arr);
      });
      const sortedDepths = [...byDepth.keys()].sort((a, b) => a - b);
      for (const d of sortedDepths) {
        if (d === 0) continue; // root: no parent to inherit Y from
        const nodes = byDepth.get(d);
        // Sub-bucket by rank (same x in LR layout — each side has its own
        // rank per depth, since left children sit at x < parent and right
        // children at x > parent).
        const byRank = new Map();
        nodes.forEach((id) => {
          const x = g.node(id).x;
          const arr = byRank.get(x) || [];
          arr.push(id);
          byRank.set(x, arr);
        });
        byRank.forEach((rankNodes) => {
          if (rankNodes.length < 2) return;
          // Group by tree parent (via the lexical id prefix of parentIdOf).
          const byParent = new Map();
          rankNodes.forEach((id) => {
            const pid = parentIdOf(id);
            const arr = byParent.get(pid) || [];
            arr.push(id);
            byParent.set(pid, arr);
          });
          // (a) INTRA-GROUP: sort children of each parent by call_site_line
          // ascending (stable). Null call_site_line entries keep relative
          // order (mirrors Layer 2 stable-sort tie-break: null sorts after).
          byParent.forEach((kids) => {
            if (kids.length < 2) return;
            kids.sort((a, b) => {
              const la = nodesById.get(a).call_site_line;
              const lb = nodesById.get(b).call_site_line;
              if (la == null && lb == null) return 0;
              if (la == null) return 1;
              if (lb == null) return -1;
              return la - lb;
            });
          });
          // (b) INTER-GROUP: order parent groups by parent's finalized Y
          // from the previous rank. Parents missing from dagre (defensive
          // — should not happen for body-call ranks) sort last. Stable
          // tie-break on lexical pid keeps deterministic order when two
          // parents share Y (rare; barycenter usually disambiguates).
          const parentIds = [...byParent.keys()];
          parentIds.sort((p1, p2) => {
            const n1 = g.node(p1);
            const n2 = g.node(p2);
            const y1 = n1 ? n1.y : Infinity;
            const y2 = n2 ? n2.y : Infinity;
            if (y1 !== y2) return y1 - y2;
            return p1 < p2 ? -1 : p1 > p2 ? 1 : 0;
          });
          // (c) Y PLACEMENT — anchor-then-separate.
          //
          // For each child in the new order, compute an ideal Y center
          // from the parent's call-site-line anchor (the same per-line
          // offset pts[0] uses for edge rendering). Falls back to
          // parent center Y when call_site_line is missing or the line
          // span can't be found in the parent DOM.
          //
          // Then run one forward separation sweep top-to-bottom: child's
          // top = max(ideal_top, prev_bottom + NODESEP). Never reorder
          // during separation — the (a)/(b) crossing fix depends on
          // monotonic order. The first child sits exactly at its ideal;
          // each subsequent child sits at its ideal OR pushed down only
          // as far as needed to clear the previous child by NODESEP.
          //
          // Effect: edges leave their parent's call-site line and arrive
          // at children that sit roughly level with that line, so a
          // rank's bottom child no longer floats at the top of the rank
          // (Stage 1b's top-packing artifact). Late-source-line children
          // may be pushed slightly below their ideal when prior siblings'
          // anchors crowded together, but they remain ordered.
          const ordered = [];
          parentIds.forEach((pid) => {
            ordered.push(...byParent.get(pid));
          });
          let prevBottom = -Infinity;
          ordered.forEach((id) => {
            const n = g.node(id);
            const pid = parentIdOf(id);
            const pDagre = g.node(pid);
            // Ideal center Y: parent's call-site-line anchor for this
            // child, else parent center.
            let idealCenter = pDagre ? pDagre.y : n.y;
            if (pDagre) {
              const childNode = nodesById.get(id);
              const parentNode = nodesById.get(pid);
              if (
                parentNode &&
                parentNode.node_type === "function" &&
                parentNode.source_location &&
                childNode.call_site_line != null
              ) {
                const baseLine = parentNode.source_location.lines[0];
                const rel = childNode.call_site_line - baseLine;
                const pDom = domById.get(pid);
                if (pDom) {
                  const off = lineOffsetInParent(pDom, rel);
                  if (off) {
                    idealCenter = pDagre.y - pDagre.height / 2 + off.y;
                  }
                }
              }
            }
            let top = idealCenter - n.height / 2;
            if (prevBottom + NODESEP > top) {
              top = prevBottom + NODESEP;
            }
            const newY = top + n.height / 2;
            const oldY = n.y;
            n.y = newY;
            reorderDelta.set(id, newY - oldY);
            prevBottom = top + n.height;
          });
        });
      }
      // Translate edge polylines by the tree-child's reorder delta so the
      // child end of each polyline lands at the child's new Y.
      g.edges().forEach((e) => {
        const reversed = !e.w.startsWith(e.v + "/");
        const treeChildId = reversed ? e.v : e.w;
        const delta = reorderDelta.get(treeChildId) || 0;
        if (delta !== 0) {
          const edge = g.edge(e);
          edge.points.forEach((p) => {
            p.y += delta;
          });
        }
      });
    })();

    // Modifier grouping needs to happen BEFORE node-rect construction (and
    // before edge-shift application) so we know each parent's modifier-zone
    // height ahead of computing the v0.5.1 left-side shift below.
    const modifiersByParent = new Map();
    modifierIds.forEach((mid) => {
      const pid = parentIdOf(mid);
      if (!pid || !visibleIds.has(pid)) return;
      const arr = modifiersByParent.get(pid) || [];
      arr.push(mid);
      modifiersByParent.set(pid, arr);
    });
    modifiersByParent.forEach((arr, pid) => {
      arr.sort((a, b) => {
        const ai = parseInt(a.slice(pid.length + 1), 10);
        const bi = parseInt(b.slice(pid.length + 1), 10);
        return ai - bi;
      });
    });

    // v0.5.1 / v0.5.2 / v0.6: left-side modifier-overlap shift.
    //
    // Modifiers are placed manually at upper-left of their parent, stacked
    // downward from parent.top. Dagre places left-side body-call children
    // at roughly the same Y as the parent — so a left-side child visually
    // intersects the modifier zone and hides it. Shift the child DOWN by
    // exactly enough to clear the parent's modifier zone:
    //
    //     extra = max(0, parent.top + parent_modifier_zone + GAP - child.top)
    //
    // where parent.top and child.top are dagre's pre-shift Y of the top
    // edges (n.y - n.height/2). The parent-shift baseline cancels: with
    // childShift = parentShift + extra, the post-shift child.top always
    // clears parent_modifier_zone. The cascade composes identically:
    // descendants inherit parentShift and add their own per-node extra.
    //
    // v0.5.1 used a fixed extra (assuming dagre never places a left child
    // above parent — false for tall children); v0.5.2 fixed that with the
    // per-node minimum-clear formula above.
    //
    // v0.6 (3e): parent_modifier_zone now includes the FULL modifier
    // cluster vertical extent. Stage 3d promotes modifier subtrees from
    // the v0.5 vertical-column placeholder into per-parent-auto-balanced
    // manual layout — a modifier with a deeply-expanded subtree no longer
    // occupies a slim column; it has horizontal spread, but its vertical
    // span (cluster height) is what the left-side body-call shift must
    // clear. Pre-v0.6 the zone summed only the modifier nodes' own
    // heights, leaving body-call children to collide with modifier-subtree
    // descendants when those subtrees grew tall (e.g., Sablier
    // createWithDurationsLT with noDelegateCall → _preventDelegateCall
    // expanded: ~66,000 px² overlap with the left-side body-call child).
    //
    // Right-side subtrees are untouched (extra = 0 for side === "right";
    // the modifier zone is upper-left, no overlap on the right).

    // 3d/3e helper: recursive cluster-height computation used both here
    // (for the zone calculation that drives the body-call shift) and below
    // (for the manual cluster placement pass). Mirrors the per-side
    // sibling-stacking logic in placeCluster so the predicted height
    // matches the actual placement.
    // v0.9 (spec §10.3 item 3): modifier subtree descendants are all
    // structurally "left" via Rule 3 in assignSide, so this height metric
    // is a single leftward stack — no per-side bucketing. The function
    // only ever operates on the modifier subtree (called from the modifier
    // zone calculation below and from placeClusterSubtree); v0.6's left/
    // right bucketing existed because the v0.6 modifier subtree could
    // place descendants on either side under the no-cross constraint.
    // That constraint is gone; descendants extend leftward only.
    function subtreeHeightManual(id) {
      const selfSize = measureDom(id);
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return selfSize.h;
      const kids = (node.children || []).filter((c) => visibleIds.has(c.__id));
      if (kids.length === 0) return selfSize.h;
      let total = 0;
      kids.forEach((c) => {
        total += subtreeHeightManual(c.__id);
      });
      if (kids.length > 1) total += (kids.length - 1) * MODIFIER_GAP_Y;
      return Math.max(selfSize.h, total);
    }

    const modZoneByParent = new Map(); // pid -> total stacked modifier-cluster height
    modifiersByParent.forEach((modIds, pid) => {
      let total = 0;
      modIds.forEach((mid, i) => {
        total += subtreeHeightManual(mid); // 3e: full cluster, not just mod node
        if (i < modIds.length - 1) total += MODIFIER_GAP_Y;
      });
      modZoneByParent.set(pid, total);
    });

    // v0.9.2: topmost left-side direct child top per parent, in dagre
    // (post-reorder) coordinates. Drives the group-delta modifier shift
    // below, so multiple left-side children of one parent clear the
    // modifier zone by a single shared delta instead of each clamping to
    // the zone bottom (which collapsed them). Keyed by immediate parent via
    // parentIdOf. Modifier-subtree descendants (inManualLayout) are
    // excluded since they are not dagre nodes.
    const leftGroupTopByParent = new Map(); // pid -> min dagre top among left-side direct children
    visibleIds.forEach((id) => {
      if (id === flow.root.__id) return;
      if (inManualLayout(id)) return;
      if (sideById.get(id) !== "left") return;
      const pid = parentIdOf(id);
      if (!pid) return;
      const cd = g.node(id);
      if (!cd) return;
      const top = cd.y - cd.height / 2;
      const cur = leftGroupTopByParent.get(pid);
      if (cur === undefined || top < cur) leftGroupTopByParent.set(pid, top);
    });

    const shiftById = new Map();
    function computeShifts(id) {
      let nodeShift;
      if (id === flow.root.__id) {
        nodeShift = 0;
      } else {
        const pid = parentIdOf(id);
        const parentShift = shiftById.get(pid) || 0;
        const side = sideById.get(id);
        const parentZone = modZoneByParent.get(pid) || 0;
        let extra = 0;
        if (side === "left" && parentZone > 0) {
          const parentDagre = g.node(pid);
          if (parentDagre) {
            const parentTop = parentDagre.y - parentDagre.height / 2;
            // v0.9.2: clear the zone using the TOPMOST left-side sibling's
            // top, not this child's own top, so every left child of `pid`
            // gets the same delta. The per-node childTop form (v0.5.2)
            // mapped every left child above the zone bottom to the same
            // absolute Y (parentTop + zone + GAP), which collapsed 2+ left
            // siblings onto each other after the reorder pass had separated
            // them (PoolManager.swap: toId + _getPool both left under a
            // two-modifier zone). A uniform group delta clears the zone for
            // the topmost sibling and preserves the reorder separation for
            // the rest. Reduces to the old formula when there is one left
            // child (groupTop equals childTop), so single-child cases
            // (Sablier createWithDurationsLT) are unchanged.
            const groupTop = leftGroupTopByParent.get(pid);
            if (groupTop !== undefined) {
              extra = Math.max(0, parentTop + parentZone + MODIFIER_GAP_Y - groupTop);
            }
          }
        }
        nodeShift = parentShift + extra;
      }
      shiftById.set(id, nodeShift);
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return;
      (node.children || []).forEach((c) => {
        if (!visibleIds.has(c.__id)) return;
        if (inManualLayout(c.__id)) return; // modifier subtree — manual layout, not dagre
        computeShifts(c.__id);
      });
    }
    computeShifts(flow.root.__id);

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const nodeRects = new Map();
    g.nodes().forEach((id) => {
      const n = g.node(id);
      const shift = shiftById.get(id) || 0;
      const left = n.x - n.width / 2;
      const top = n.y - n.height / 2 + shift;
      nodeRects.set(id, { x: n.x, y: n.y + shift, w: n.width, h: n.height, left, top });
      if (left < minX) minX = left;
      if (top < minY) minY = top;
      if (left + n.width > maxX) maxX = left + n.width;
      if (top + n.height > maxY) maxY = top + n.height;
    });
    g.edges().forEach((e) => {
      const edge = g.edge(e);
      // Apply the destination child's shift to every point on the polyline.
      // For edges entirely within a shifted subtree, source and destination
      // share the same shift, so this is a uniform translate. For boundary
      // edges (unshifted parent → shifted child) the parent end of the
      // polyline lands below the parent's actual position, but pts[0] is
      // overridden to the parent's signature line during edge rendering, so
      // the rendered curve still leaves from the correct anchor and sweeps
      // down to the shifted child.
      const reversed = !e.w.startsWith(e.v + "/");
      const treeChildId = reversed ? e.v : e.w;
      const edgeShift = shiftById.get(treeChildId) || 0;
      if (edgeShift > 0) {
        edge.points.forEach((p) => {
          p.y += edgeShift;
        });
      }
      edge.points.forEach((p) => {
        if (p.x < minX) minX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.x > maxX) maxX = p.x;
        if (p.y > maxY) maxY = p.y;
      });
    });

    // Manual layout pass for modifier clusters (spec §10.3 item 3, §11.6).
    //
    // For each visible function with one or more visible modifier children,
    // stack the modifier clusters vertically in the upper-left of the
    // parent in declaration order. Each cluster = the modifier itself
    // (anchored at parent's left edge minus MODIFIER_GAP_X) plus its
    // visible body-call subtree, placed recursively leftward per §10.3
    // item 3 (modifier subtree descendants always extend leftward,
    // inheriting the modifier's structural "left" side via Rule 3 in
    // assignSide).
    //
    // Modifier subtree descendants stay OUT of dagre — the "lifted
    // entirely out of the dagre graph" architecture (§11.6) is preserved.
    // Under v0.9 inheritance, descendants no longer dance between sides
    // via the v0.6 no-cross constraint; they're always leftward, so the
    // placement loop is a simple leftward-stacking pass.
    //
    // Band separation (spec §10.3 item 3 last clause): the modifier
    // cluster occupies the Y range [parent.top, parent.top +
    // subtreeHeightManual(modifier)] — i.e., from the parent's top edge
    // downward through the modifier's full leftward-extending subtree.
    // The parent's body-call LEFT subtree would otherwise collide with
    // this range (its dagre-assigned Y is near parent.top too). The
    // collision is resolved by `computeShifts` above, which adds
    // `extra = max(0, parent.top + parentZone + GAP - child.top)` to
    // every left-side body-call descendant's Y. After the shift, body-
    // call left descendants sit at Y >= parent.top + parentZone + GAP —
    // below the modifier cluster's bottom. Right-side body callees never
    // collide with the modifier subtree (modifier extends leftward only)
    // so the shift correctly fires only for `side === "left"`.

    function recordRect(id, left, top, w, h) {
      nodeRects.set(id, { x: left + w / 2, y: top + h / 2, w, h, left, top });
      if (left < minX) minX = left;
      if (top < minY) minY = top;
      if (left + w > maxX) maxX = left + w;
      if (top + h > maxY) maxY = top + h;
    }

    // Recursively place a modifier-cluster subtree starting at `id` whose
    // anchor rectangle is (left, top, w, h). All children extend leftward
    // (§10.3 item 3), stacked vertically in source order. Uses
    // subtreeHeightManual to reserve vertical space for each child's full
    // subtree before stacking the next sibling.
    function placeClusterSubtree(id, left, top, w, h) {
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return;
      const kids = (node.children || []).filter((c) => visibleIds.has(c.__id));
      if (kids.length === 0) return;
      let stackTop = top;
      kids.forEach((c) => {
        const cid = c.__id;
        const cSize = measureDom(cid);
        const cLeft = left - MODIFIER_GAP_X - cSize.w;
        const cTop = stackTop;
        recordRect(cid, cLeft, cTop, cSize.w, cSize.h);
        placeClusterSubtree(cid, cLeft, cTop, cSize.w, cSize.h);
        stackTop = cTop + subtreeHeightManual(cid) + MODIFIER_GAP_Y;
      });
    }

    modifiersByParent.forEach((modIds, pid) => {
      const parentRect = nodeRects.get(pid);
      if (!parentRect) return;
      let stackY = parentRect.top;
      modIds.forEach((mid) => {
        const { w, h } = measureDom(mid);
        const left = parentRect.left - w - MODIFIER_GAP_X;
        const top = stackY;
        recordRect(mid, left, top, w, h);
        // Place the modifier's body-call subtree leftward per §10.3
        // item 3 (descendants structurally "left" via Rule 3).
        placeClusterSubtree(mid, left, top, w, h);
        // Advance the next modifier in the stack past this entire cluster.
        stackY = top + subtreeHeightManual(mid) + MODIFIER_GAP_Y;
      });
    });

    const dx = -minX + 8;
    const dy = -minY + 8;
    const layoutWidth = Math.ceil(maxX - minX) + 16;
    const layoutHeight = Math.ceil(maxY - minY) + 16;

    graph.style.width = layoutWidth + "px";
    graph.style.height = layoutHeight + "px";
    edgesLayer.setAttribute("width", layoutWidth);
    edgesLayer.setAttribute("height", layoutHeight);
    edgesLayer.setAttribute("viewBox", "0 0 " + layoutWidth + " " + layoutHeight);

    visibleIds.forEach((id) => {
      const r = nodeRects.get(id);
      if (!r) return;
      const targetLeft = r.left + dx;
      const targetTop = r.top + dy;
      const dom = domById.get(id);
      dom.style.width = r.w + "px";

      const old = oldPositions ? oldPositions.get(id) : null;
      if (old) {
        // v0.10.4: transition opacity to 1 alongside position. The
        // .interrupt() here kills any in-flight entrance fade from a
        // previous relayout; before this fix, a node whose fade was
        // interrupted (two expansions within ANIM_MS of each other)
        // froze at its mid-fade opacity forever — visible as edges
        // pointing at invisible nodes (Morpho liquidate, v0.10.3 eval).
        d3.select(dom)
          .interrupt()
          .transition()
          .duration(ANIM_MS)
          .style("left", targetLeft + "px")
          .style("top", targetTop + "px")
          .style("opacity", "1");
      } else {
        dom.style.left = targetLeft + "px";
        dom.style.top = targetTop + "px";
        dom.style.opacity = "0";
        d3.select(dom).interrupt().transition().duration(ANIM_MS).style("opacity", "1");
      }
    });

    edgesGroup.innerHTML = "";
    edgesGroup.setAttribute("transform", "");

    // Dagre-routed edges. Left-side children are registered as
    // setEdge(child, parent) so dagre's LR layout places them on the left;
    // we reverse the point list so the arrowhead still lands at the child
    // (matching the "parent CALLS child" semantic).
    g.edges().forEach((e) => {
      const edge = g.edge(e);
      const reversed = isReversedEdge(e.v, e.w);
      const treeParentId = reversed ? e.w : e.v;
      const treeChildId = reversed ? e.v : e.w;
      const treeParentNode = nodesById.get(treeParentId);
      const treeChildNode = nodesById.get(treeChildId);
      const treeParentDom = domById.get(treeParentId);
      const treeParentRect = nodeRects.get(treeParentId);

      let pts = edge.points.map((p) => ({ x: p.x + dx, y: p.y + dy }));
      if (reversed) pts = pts.reverse();

      if (treeParentNode.node_type === "function" && treeChildNode.call_site_line != null) {
        const baseLine = treeParentNode.source_location.lines[0];
        const rel = treeChildNode.call_site_line - baseLine;
        const off = lineOffsetInParent(treeParentDom, rel);
        if (off) {
          pts[0] = {
            x: treeParentRect.left + dx + (reversed ? off.xLeft : off.xRight),
            y: treeParentRect.top + dy + off.y,
          };
        }
      }

      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", line(pts));
      path.setAttribute("class", "edge edge-clickable");
      path.setAttribute("marker-end", "url(#arrowhead)");
      path.setAttribute("data-child-id", treeChildId);
      edgesGroup.appendChild(path);
    });

    // Manual edges for modifier clusters (spec §10.3 item 3).
    //
    // Two sub-cases under v0.9 inheritance:
    //  (a) parent → modifier: parent's signature line (where the modifier
    //      name appears) on the LEFT edge, to the modifier's right edge.
    //  (b) modifier → its body-call subtree (recursively): the child is
    //      to the LEFT of the parent per Rule 3, so the edge anchors at
    //      the parent's left edge (per-line if call_site_line is known)
    //      and arrives at the child's right edge. No side branching —
    //      v0.6's left/right anchor selection collapsed into a single
    //      leftward path when modifier subtree descendants became
    //      structurally "left".
    visibleIds.forEach((id) => {
      if (!inManualLayout(id)) return;
      const pid = parentIdOf(id);
      if (!pid || !visibleIds.has(pid)) return;
      const childRect = nodeRects.get(id);
      const parentRect = nodeRects.get(pid);
      if (!childRect || !parentRect) return;
      const childNode = nodesById.get(id);
      const parentNode = nodesById.get(pid);
      const parentDom = domById.get(pid);

      let parentY = parentRect.top + dy + parentRect.h / 2;
      if (parentNode && parentNode.node_type === "function" && childNode.call_site_line != null) {
        const baseLine = parentNode.source_location.lines[0];
        const rel = childNode.call_site_line - baseLine;
        const off = lineOffsetInParent(parentDom, rel);
        if (off) parentY = parentRect.top + dy + off.y;
      }
      // (a) and (b) collapse to the same geometry: parent's left edge
      // (per-line anchored when known) → child's right edge. The modifier
      // sits to the left of its parent; the modifier's own body-call
      // descendants sit further left of the modifier — same direction.
      const from = { x: parentRect.left + dx, y: parentY };
      const to = {
        x: childRect.left + dx + childRect.w,
        y: childRect.top + dy + childRect.h / 2,
      };
      const mid = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };

      const path = document.createElementNS(SVG_NS, "path");
      path.setAttribute("d", line([from, mid, to]));
      path.setAttribute(
        "class",
        "edge edge-clickable" + (modifierIds.has(id) ? " edge-modifier" : ""),
      );
      path.setAttribute("marker-end", "url(#arrowhead)");
      path.setAttribute("data-child-id", id);
      edgesGroup.appendChild(path);
    });

    refreshExpandedLineState();
    lastNodeRects = nodeRects; // snapshot for next layout's auto-balance metric
    graph.dataset.state = "ready";
    return { width: layoutWidth, height: layoutHeight };
  }

  // ----- fit-to-frame + d3-zoom ------------------------------------------

  // v0.10.0 Stage 1b: the standard interactive zoom-out floor of 0.05 is
  // fine for interactively-built flows where layoutBox stays within a few
  // viewport multiples, but --expand-all on a large tree (Uniswap V4
  // PoolManager.swap → ~9645 × 133771 px layout) needs a tighter fit
  // scale than 0.05 to bring the whole tree into view. We compute the
  // required fit floor lazily per layout and widen scaleExtent's lower
  // bound only when needed. The upper bound (4×) is unchanged. Interactive
  // zoom remains clamped to scaleExtent, so on normal flows the floor
  // stays at 0.05.
  const ZOOM_MIN_DEFAULT = 0.05;
  const ZOOM_MAX = 4;

  function fitTransform() {
    const fr = frame.getBoundingClientRect();
    const padding = 16;
    const sx = (fr.width - padding * 2) / layoutBox.width;
    const sy = (fr.height - padding * 2) / layoutBox.height;
    const fitScale = Math.min(sx, sy);
    // Lower the zoom floor whenever the required fit dips below the
    // default floor. Without this, fitScale ≈ 0.0065 on swap gets clamped
    // back up to 0.05 by scaleExtent, leaving every node off-screen.
    const newMin = Math.min(ZOOM_MIN_DEFAULT, fitScale);
    zoom.scaleExtent([newMin, ZOOM_MAX]);
    const scale = Math.min(1, fitScale);
    const tx = (fr.width - layoutBox.width * scale) / 2;
    const ty = padding;
    return d3.zoomIdentity.translate(tx, ty).scale(scale);
  }

  const zoom = d3
    .zoom()
    .scaleExtent([ZOOM_MIN_DEFAULT, ZOOM_MAX])
    .on("zoom", (event) => {
      const t = event.transform;
      graph.style.transform = "translate(" + t.x + "px, " + t.y + "px) scale(" + t.k + ")";
    });

  const frameSel = d3.select(frame);
  frameSel.call(zoom);

  function currentScale() {
    return d3.zoomTransform(frame).k || 1;
  }

  function applyFit(animated) {
    const t = fitTransform();
    if (animated) {
      frameSel.transition().duration(220).call(zoom.transform, t);
    } else {
      frameSel.call(zoom.transform, t);
    }
  }

  // ----- initial render ---------------------------------------------------

  // v0.10.0: --expand-all picks the full-tree initial state; the default
  // remains root + auto-rendered modifiers. Either way, a single VISIBLE
  // relayout and fit pass run afterwards — the rest of the renderer is
  // unchanged. The click path is byte-for-byte the same as before
  // Stage 1c; only the expand-all initial render adds the measure +
  // balance + rewrite-sides step before the visible relayout.
  if (EXPAND_ALL) {
    expandAllRecursive(flow.root.__id);
    // Stage 1c (spec §10.3 "Expand-all balancing"). Without this the
    // bulk expansion leaves every first-level branch on the right
    // (Rule 1 against empty lastNodeRects) and the graph renders as a
    // single tall vertical pile. Measure subtree extents from a no-DOM
    // dagre pass, balance the first-level sides in one greedy pass,
    // then let the regular relayout below do the visible layout.
    const extents = measureFirstLevelExtents();
    const balanced = balanceFirstLevelSides(extents);
    applyBalancedSides(balanced);
  } else {
    expandImplicit(flow.root.__id);
    // v0.16.0 (spec §10.2 "Persisted expansion state"): restore the auditor's
    // saved expansion. If anything was restored, balance first-level sides the
    // same way --expand-all does — otherwise restored first-level branches pile
    // on the right against an empty lastNodeRects, exactly the Stage 1c case.
    if (restoreExpansion()) {
      const extents = measureFirstLevelExtents();
      const balanced = balanceFirstLevelSides(extents);
      applyBalancedSides(balanced);
    }
  }
  let layoutBox = relayout(null);
  applyFit(false);

  if (resetButton) {
    resetButton.addEventListener("click", () => applyFit(true));
  }

  // v0.16.0 (spec §10.2 "Expand-all / collapse-all controls"): whole-tree
  // controls in the flow header, a convenience over per-call-site clicks. Both
  // persist the resulting expansion (spec "Persisted expansion state").
  function expandAll() {
    if (EXPAND_ALL && visibleIds.size === nodesById.size) {
      applyFit(true); // already fully expanded — just re-fit
      return;
    }
    const snapshot = snapshotPositions();
    expandAllRecursive(flow.root.__id);
    const extents = measureFirstLevelExtents();
    const balanced = balanceFirstLevelSides(extents);
    applyBalancedSides(balanced);
    refreshExpandedLineState();
    layoutBox = relayout(snapshot);
    applyFit(true);
    persistExpansion();
  }

  function collapseAll() {
    const snapshot = snapshotPositions();
    // Collapse every first-level non-modifier child of the root; the root and
    // its auto-rendered modifier subtrees remain — the default first-load state.
    const rootId = flow.root.__id;
    const targets = [];
    visibleIds.forEach((id) => {
      if (parentIdOf(id) === rootId && !isModifierNode(nodesById.get(id))) {
        targets.push(id);
      }
    });
    targets.forEach((id) => collapse(id));
    refreshExpandedLineState();
    layoutBox = relayout(snapshot);
    applyFit(true);
    persistExpansion();
  }

  const expandAllBtn = document.getElementById("expand-all-btn");
  if (expandAllBtn) expandAllBtn.addEventListener("click", expandAll);
  const collapseAllBtn = document.getElementById("collapse-all-btn");
  if (collapseAllBtn) collapseAllBtn.addEventListener("click", collapseAll);

  window.addEventListener("keydown", (e) => {
    const k = e.key;
    const isFit = k === "0" || k === "r" || k === "R";
    const isExpand = k === "e" || k === "E";
    const isCollapse = k === "c" || k === "C";
    if (!isFit && !isExpand && !isCollapse) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (isExpand) expandAll();
    else if (isCollapse) collapseAll();
    else applyFit(true);
  });

  // v0.15.0: make the in-app "← index" link restore scroll position like the
  // browser's own Back button. A plain <a href="/"> does a fresh load that
  // renders the index at the top, losing the auditor's place; a history
  // navigation instead reuses the per-entry scroll position the browser saved.
  // When this flow page was reached FROM the index, route a plain left-click
  // through history.back() so that saved scroll is restored. Anything else —
  // a direct load / new-tab open (no index in history), a modified click
  // (open-in-new-tab), or a non-index referrer — falls through to the normal
  // href navigation. This handler lives only on the flow page; the index stays
  // JavaScript-free (spec §8.3).
  const backLink = document.querySelector(".back-link");
  if (backLink) {
    backLink.addEventListener("click", (e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      let cameFromIndex = false;
      try {
        const ref = new URL(document.referrer);
        cameFromIndex =
          ref.origin === window.location.origin && ref.pathname === "/";
      } catch (_err) {
        cameFromIndex = false;
      }
      if (cameFromIndex && window.history.length > 1) {
        e.preventDefault();
        window.history.back();
      }
    });
  }

  // v0.15.0: toggle the entry's bookmark in place rather than reloading the flow
  // page (a reload would re-run the entire dagre layout). The toggle is a plain
  // server-side link (no-JS fallback); here we persist via fetch and flip the
  // icon, leaving the graph untouched.
  const bookmarkToggle = document.querySelector(".flow-nav .bookmark-toggle");
  if (bookmarkToggle) {
    bookmarkToggle.addEventListener("click", (e) => {
      if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
      e.preventDefault();
      const on = !bookmarkToggle.classList.contains("is-on");
      fetch(bookmarkToggle.href, { credentials: "same-origin" })
        .then(() => {
          bookmarkToggle.classList.toggle("is-on", on);
          bookmarkToggle.setAttribute("aria-pressed", on ? "true" : "false");
          const label = on ? "Remove bookmark" : "Bookmark this entry point";
          bookmarkToggle.setAttribute("title", label);
          bookmarkToggle.setAttribute("aria-label", label);
        })
        .catch(() => {
          window.location.href = bookmarkToggle.href;
        });
    });
  }
})();
