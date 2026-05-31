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

  function renderFunctionNode(node) {
    const wrap = el(
      "div",
      "node node--function" + (node.is_modifier ? " node--modifier" : ""),
    );

    // v0.7.0 title bar (spec §10.2). The short-form title lands in its own
    // row above the source body, painted on a slightly darker cream than
    // the node interior with a thin dark-ink rule separating it from the
    // body. Mirrors the same construction in flow.js (legacy renderer).
    const titleBar = el("div", "node-title-bar");
    titleBar.appendChild(
      el(
        "code",
        null,
        node.invoked_via_contract_name + "." + shortenSignature(node.full_name),
      ),
    );
    wrap.appendChild(titleBar);

    // Badges row: only emitted when at least one badge applies. An empty
    // .node-head would otherwise carry its `margin-bottom: 0.5rem` for no
    // visible content.
    const badges = el("span", "node-badges");
    if (node.is_modifier) badges.appendChild(el("span", "badge badge-modifier", "modifier"));
    if (node.invoked_via_super) badges.appendChild(el("span", "badge badge-super", "super"));
    if (node.invoked_via_contract_name !== node.declarer_contract_name) {
      badges.appendChild(
        el("span", "badge badge-inherited", "inherited from " + node.declarer_contract_name),
      );
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
      wrap.appendChild(el("div", "node-builtins", "builtins: " + node.builtins_used.join(", ")));
    }

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
    } else {
      ids.forEach((cid) => expandImplicit(cid));
    }
    refreshExpandedLineState();
    layoutBox = relayout(snapshot);
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
          const childDagre = g.node(id);
          if (parentDagre && childDagre) {
            const parentTop = parentDagre.y - parentDagre.height / 2;
            const childTop = childDagre.y - childDagre.height / 2;
            extra = Math.max(0, parentTop + parentZone + MODIFIER_GAP_Y - childTop);
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
        d3.select(dom)
          .interrupt()
          .transition()
          .duration(ANIM_MS)
          .style("left", targetLeft + "px")
          .style("top", targetTop + "px");
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

  function fitTransform() {
    const fr = frame.getBoundingClientRect();
    const padding = 16;
    const sx = (fr.width - padding * 2) / layoutBox.width;
    const sy = (fr.height - padding * 2) / layoutBox.height;
    const scale = Math.min(1, Math.min(sx, sy));
    const tx = (fr.width - layoutBox.width * scale) / 2;
    const ty = padding;
    return d3.zoomIdentity.translate(tx, ty).scale(scale);
  }

  const zoom = d3
    .zoom()
    .scaleExtent([0.05, 4])
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

  expandImplicit(flow.root.__id);
  let layoutBox = relayout(null);
  applyFit(false);

  if (resetButton) {
    resetButton.addEventListener("click", () => applyFit(true));
  }

  window.addEventListener("keydown", (e) => {
    if (e.key !== "0" && e.key !== "r" && e.key !== "R") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    applyFit(true);
  });
})();
