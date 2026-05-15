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
    const head = el("div", "node-head");
    head.appendChild(
      el(
        "span",
        "node-title",
        node.invoked_via_contract_name + "." + shortenSignature(node.full_name),
      ),
    );

    const badges = el("span", "node-badges");
    if (node.is_modifier) badges.appendChild(el("span", "badge badge-modifier", "modifier"));
    if (node.invoked_via_super) badges.appendChild(el("span", "badge badge-super", "super"));
    if (node.invoked_via_contract_name !== node.declarer_contract_name) {
      badges.appendChild(
        el("span", "badge badge-inherited", "inherited from " + node.declarer_contract_name),
      );
    }
    head.appendChild(badges);
    wrap.appendChild(head);

    if (node.source_html) {
      const pre = el("pre", "node-body src");
      pre.innerHTML = wrapCallLines(node);
      colorizeCallNamesAcrossLines(pre, node);
      wrap.appendChild(pre);
    }

    if (node.builtins_used && node.builtins_used.length) {
      wrap.appendChild(el("div", "node-builtins", "builtins: " + node.builtins_used.join(", ")));
    }

    return wrap;
  }

  function renderUnresolvedNode(node) {
    const wrap = el("div", "node node--unresolved node--pill");
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    title.appendChild(el("code", null, node.descriptor || "(unknown)"));
    head.appendChild(title);

    const badges = el("span", "node-badges");
    badges.appendChild(el("span", "badge badge-unresolved", node.reason));
    head.appendChild(badges);
    wrap.appendChild(head);
    return wrap;
  }

  function renderExternalNode(node) {
    const wrap = el("div", "node node--external node--pill");
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    if (node.target_contract_name) {
      title.appendChild(el("code", null, node.target_canonical_name));
    } else {
      title.appendChild(el("code", null, node.target_function_name));
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

  // v0.6 direction-model side assignment (spec §10.3 Rule 2).
  //
  // Replaces v0.5's strict inheritance. For every visible non-root
  // non-modifier node we evaluate both candidate sides relative to the
  // immediate parent:
  //   - feasibility = placing the node on that side does not introduce
  //     a crossing with any currently-visible edge (no-cross constraint);
  //   - if both feasible → pick the side with less vertical extent in the
  //     parent's own subtree (per-parent auto-balance; ties → right);
  //   - if only one feasible → pick it;
  //   - if neither feasible → inherit the parent's own side relative to its
  //     own ancestor (fallback that guarantees a placement exists).
  //
  // Modifier-subtree descendants (3d) go through the same algorithm — the
  // no-cross constraint typically biases them leftward emergent from the
  // parent body sitting to the modifier's right, not from a hard-coded rule.
  //
  // Modifiers themselves still get no side entry (manual upper-left
  // placement separately per §11.6, unchanged from v0.5).
  function assignSide(id) {
    if (id === flow.root.__id) return;
    const node = nodesById.get(id);
    if (isModifierNode(node)) return;
    if (sideById.has(id)) return; // idempotent — spec: no retroactive reposition

    const pid = parentIdOf(id);

    // Phase 1: evaluate feasibility on each candidate side.
    const leftFeasible = isFeasibleSide(id, "left");
    const rightFeasible = isFeasibleSide(id, "right");

    let side;
    if (leftFeasible && rightFeasible) {
      // Both feasible → pick less-extent side (ties → right).
      const lExt = parentSideExtent(pid, "left");
      const rExt = parentSideExtent(pid, "right");
      side = rExt <= lExt ? "right" : "left";
    } else if (leftFeasible) {
      side = "left";
    } else if (rightFeasible) {
      side = "right";
    } else {
      // Fallback: inherit parent's own side relative to its ancestor.
      side = inheritedSide(pid);
    }

    sideById.set(id, side);
  }

  // Per-parent vertical extent of one side's currently-visible descendants,
  // measured from the previous layout snapshot (lastNodeRects). Returns 0
  // when no descendants exist on that side yet.
  //
  // For the root, the metric scopes to all first-level non-modifier-subtree
  // sided descendants (matching v0.5's root auto-balance). For non-root
  // parents, the metric scopes to descendants whose branch starts on the
  // queried side relative to this parent.
  function parentSideExtent(pid, side) {
    if (pid === flow.root.__id) {
      // Root metric: same as v0.5 — measure the visible subtree extent on
      // each side of the root, excluding modifier subtrees (which sit
      // upper-left of root and are not in the left/right balance).
      let minY = Infinity, maxY = -Infinity;
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
    // Non-root parent: descendants of pid whose branch starts on the
    // queried side of pid. "Branch starts on side S" means the ancestor of
    // the descendant that is a direct child of pid has sideById === S.
    const prefix = pid + "/";
    let minY = Infinity, maxY = -Infinity;
    visibleIds.forEach((vid) => {
      if (vid === pid) return;
      if (!vid.startsWith(prefix)) return;
      const direct = directChildOf(pid, vid);
      if (!direct) return;
      if ((sideById.get(direct) || "right") !== side) return;
      const r = lastNodeRects.get(vid);
      if (!r) return;
      if (r.top < minY) minY = r.top;
      if (r.top + r.h > maxY) maxY = r.top + r.h;
    });
    return minY === Infinity ? 0 : maxY - minY;
  }

  // Walk vid up to the ancestor that is a direct child of pid. Returns
  // that ancestor's id, or null if vid is not a descendant of pid.
  function directChildOf(pid, vid) {
    if (!vid.startsWith(pid + "/")) return null;
    const rest = vid.slice(pid.length + 1);
    const slash = rest.indexOf("/");
    return slash === -1 ? vid : pid + "/" + rest.slice(0, slash);
  }

  // Inheritance fallback (spec §10.3 Rule 2 last bullet).
  // Returns the side the new node should inherit when neither candidate is
  // feasible. Walks up: if parent is non-root non-modifier with a side,
  // use it; if parent is a modifier, use "left" (modifier subtree implicit
  // direction); if parent is root, use root auto-balance (the v0.5 default
  // when the algorithm runs out of context).
  function inheritedSide(pid) {
    if (pid === flow.root.__id) {
      const lExt = parentSideExtent(pid, "left");
      const rExt = parentSideExtent(pid, "right");
      return rExt <= lExt ? "right" : "left";
    }
    const pnode = nodesById.get(pid);
    if (isModifierNode(pnode)) return "left";
    return sideById.get(pid) || "right";
  }

  // No-cross feasibility predictor (spec §10.3 Rule 2).
  //
  // Snapshot-based approximation: predict where the new node would land if
  // placed on `side` of its parent, draw a straight line from the parent's
  // anchor on that side to the predicted node center, and test that line
  // against every currently-visible edge's straight-line proxy. Returns
  // true if zero intersections.
  //
  // Approximation is conservative: dagre's curves bend within the
  // straight-line bounding rectangle, so a straight-line miss implies a
  // curve miss; a straight-line hit may or may not imply a curve hit but
  // the algorithm errs on the side of avoiding the configuration. The
  // alternative (full trial layout) would be exact but cost an extra
  // dagre.layout() call per assignment.
  //
  // On the very first relayout (no snapshot yet) every side is feasible —
  // there are no existing edges to cross.
  function isFeasibleSide(id, side) {
    if (lastNodeRects.size === 0) return true;
    const pid = parentIdOf(id);
    const parentRect = lastNodeRects.get(pid);
    if (!parentRect) return true;
    const newRect = predictedRect(id, side);
    if (!newRect) return true;
    const newEdge = predictedEdge(parentRect, newRect, side);
    for (const e of currentVisibleEdgeProxies()) {
      if (segmentsIntersect(newEdge.p1, newEdge.p2, e.p1, e.p2)) return false;
    }
    return true;
  }

  // Where would `id` land if placed on `side` of its parent, using the
  // previous layout snapshot? We use an estimated size (the actual size
  // depends on DOM measurement which happens inside relayout). For
  // already-rendered nodes, we use measureDom; for not-yet-rendered ones we
  // estimate ~node defaults.
  function predictedRect(id, side) {
    const pid = parentIdOf(id);
    const parentRect = lastNodeRects.get(pid);
    if (!parentRect) return null;
    const sizeEst = predictedSize(id);
    // X: side-relative ranksep from parent's edge.
    const RANKSEP = 80;
    const left = side === "left"
      ? parentRect.left - RANKSEP - sizeEst.w
      : parentRect.left + parentRect.w + RANKSEP;
    // Y: below the existing subtree on that side, or at parent's center if empty.
    let bottomOnSide = parentRect.top + parentRect.h / 2 - sizeEst.h / 2;
    const prefix = pid + "/";
    visibleIds.forEach((vid) => {
      if (vid === pid) return;
      if (!vid.startsWith(prefix)) return;
      const direct = directChildOf(pid, vid);
      if (!direct) return;
      if ((sideById.get(direct) || "right") !== side) return;
      const r = lastNodeRects.get(vid);
      if (!r) return;
      if (r.top + r.h + 30 > bottomOnSide) bottomOnSide = r.top + r.h + 30; // nodesep
    });
    return { left, top: bottomOnSide, w: sizeEst.w, h: sizeEst.h };
  }

  function predictedSize(id) {
    const dom = domById.get(id);
    if (dom) {
      const r = dom.getBoundingClientRect();
      return { w: Math.max(80, Math.ceil(r.width)), h: Math.max(40, Math.ceil(r.height)) };
    }
    return { w: 240, h: 100 }; // pre-render defaults; rough but only used for prediction
  }

  function predictedEdge(parentRect, newRect, side) {
    const px = side === "left" ? parentRect.left : parentRect.left + parentRect.w;
    const py = parentRect.top + parentRect.h / 2;
    const cx = newRect.left + newRect.w / 2;
    const cy = newRect.top + newRect.h / 2;
    return { p1: { x: px, y: py }, p2: { x: cx, y: cy } };
  }

  // Snapshot-derived edge list. Each visible non-root node contributes one
  // straight-line proxy from its parent's anchor on the assigned side to
  // the node's center. Modifier edges are also included (parent → modifier
  // at the modifier's right edge).
  function currentVisibleEdgeProxies() {
    const out = [];
    visibleIds.forEach((vid) => {
      if (vid === flow.root.__id) return;
      const pid = parentIdOf(vid);
      if (!pid || !visibleIds.has(pid)) return;
      const pRect = lastNodeRects.get(pid);
      const cRect = lastNodeRects.get(vid);
      if (!pRect || !cRect) return;
      const node = nodesById.get(vid);
      if (isModifierNode(node)) {
        out.push({
          p1: { x: pRect.left, y: pRect.top + pRect.h / 2 },
          p2: { x: cRect.left + cRect.w, y: cRect.top + cRect.h / 2 },
        });
        return;
      }
      const side = sideById.get(vid) || "right";
      const px = side === "left" ? pRect.left : pRect.left + pRect.w;
      out.push({
        p1: { x: px, y: pRect.top + pRect.h / 2 },
        p2: { x: cRect.left + cRect.w / 2, y: cRect.top + cRect.h / 2 },
      });
    });
    return out;
  }

  // Standard 4-orientation segment-intersection test. Returns true iff the
  // open segments p1q1 and p2q2 properly cross (sharing an endpoint is not
  // a crossing, nor is collinear touching).
  function segmentsIntersect(p1, q1, p2, q2) {
    const o = (a, b, c) =>
      Math.sign((b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x));
    const o1 = o(p1, q1, p2);
    const o2 = o(p1, q1, q2);
    const o3 = o(p2, q2, p1);
    const o4 = o(p2, q2, q1);
    return o1 !== 0 && o2 !== 0 && o3 !== 0 && o4 !== 0 && o1 !== o2 && o3 !== o4;
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
    function subtreeHeightManual(id) {
      const selfSize = measureDom(id);
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return selfSize.h;
      const kids = (node.children || []).filter((c) => visibleIds.has(c.__id));
      if (kids.length === 0) return selfSize.h;
      let leftTotal = 0, rightTotal = 0;
      let leftCount = 0, rightCount = 0;
      kids.forEach((c) => {
        const s = sideById.get(c.__id) || "left"; // modifier subtree default → left
        const childH = subtreeHeightManual(c.__id);
        if (s === "left") { leftTotal += childH; leftCount++; }
        else              { rightTotal += childH; rightCount++; }
      });
      if (leftCount > 1) leftTotal += (leftCount - 1) * MODIFIER_GAP_Y;
      if (rightCount > 1) rightTotal += (rightCount - 1) * MODIFIER_GAP_Y;
      return Math.max(selfSize.h, leftTotal, rightTotal);
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

    // Manual layout pass for modifier clusters (v0.6).
    //
    // For each visible function with one or more visible modifier children,
    // stack the modifier clusters vertically in the upper-left of the
    // parent in declaration order. Each cluster = the modifier itself
    // (upper-left anchor) plus its visible body-call subtree, placed
    // recursively using the per-parent auto-balance + no-cross direction
    // model (§10.3 Rule 2, applied identically to body-call subtrees).
    //
    // Modifier subtree descendants stay OUT of dagre — v0.5's "lifted
    // entirely out of the dagre graph" architecture (§11.6) is preserved.
    // The change vs. v0.5 is that they are no longer rendered as a manual
    // vertical column straight down from the modifier; instead the
    // sideById assignment (already computed in showNode) drives per-parent
    // placement, with siblings stacked vertically per side. The no-cross
    // constraint typically biases them leftward (right-side placements
    // would intersect body-call edges from the parent), but the layout is
    // no longer hard-locked left.

    function recordRect(id, left, top, w, h) {
      nodeRects.set(id, { x: left + w / 2, y: top + h / 2, w, h, left, top });
      if (left < minX) minX = left;
      if (top < minY) minY = top;
      if (left + w > maxX) maxX = left + w;
      if (top + h > maxY) maxY = top + h;
    }

    // Recursively place a modifier-cluster subtree starting at `id` whose
    // anchor rectangle is (left, top, w, h). Children of `id` are placed
    // per their sideById assignment, stacked vertically within the side.
    // Uses subtreeHeightManual (defined above for zone-height calc) to
    // reserve vertical space for each child's full subtree before stacking
    // the next sibling.
    function placeClusterSubtree(id, left, top, w, h) {
      const node = nodesById.get(id);
      if (!node || node.node_type !== "function") return;
      const kids = (node.children || []).filter((c) => visibleIds.has(c.__id));
      if (kids.length === 0) return;
      let leftStackTop = top;
      let rightStackTop = top;
      kids.forEach((c) => {
        const cid = c.__id;
        const side = sideById.get(cid) || "left"; // modifier subtree default
        const cSize = measureDom(cid);
        let cLeft, cTop;
        if (side === "left") {
          cLeft = left - MODIFIER_GAP_X - cSize.w;
          cTop = leftStackTop;
        } else {
          cLeft = left + w + MODIFIER_GAP_X;
          cTop = rightStackTop;
        }
        recordRect(cid, cLeft, cTop, cSize.w, cSize.h);
        placeClusterSubtree(cid, cLeft, cTop, cSize.w, cSize.h);
        const stride = subtreeHeightManual(cid) + MODIFIER_GAP_Y;
        if (side === "left") leftStackTop = cTop + stride;
        else                 rightStackTop = cTop + stride;
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
        // Place the modifier's body-call subtree (v0.6 promotion) using
        // per-parent auto-balanced sides. v0.5 placed these as a vertical
        // column straight down; v0.6 distributes per the side model.
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

    // Manual edges for modifier clusters.
    //
    // Three sub-cases:
    //  (a) parent → modifier: parent's signature line (where the modifier
    //      name appears) on the LEFT edge, to the modifier's right edge.
    //  (b) modifier → its body-call child: similar to a body-call edge —
    //      anchor at the parent's signature line for the call (using
    //      call_site_line), on the side facing the child (left or right
    //      per sideById), to the child's opposite-side edge.
    //  (c) deeper modifier-subtree edges: same as (b).
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

      let from, to;
      if (modifierIds.has(id)) {
        // (a) Parent → modifier. Parent end at signature line containing
        // the modifier name; modifier end at modifier's right edge.
        let parentY = parentRect.top + dy + parentRect.h / 2;
        if (parentNode.node_type === "function" && childNode.call_site_line != null) {
          const baseLine = parentNode.source_location.lines[0];
          const rel = childNode.call_site_line - baseLine;
          const off = lineOffsetInParent(parentDom, rel);
          if (off) parentY = parentRect.top + dy + off.y;
        }
        from = { x: parentRect.left + dx, y: parentY };
        to = { x: childRect.left + dx + childRect.w, y: childRect.top + dy + childRect.h / 2 };
      } else {
        // (b)/(c) Modifier-subtree descendant. Side-driven anchor on the
        // parent (left or right edge), per-line if call_site_line is known,
        // pointing to the child's opposite edge.
        const childSide = sideById.get(id) || "left";
        const parentAnchorX = childSide === "left"
          ? parentRect.left + dx
          : parentRect.left + dx + parentRect.w;
        let parentY = parentRect.top + dy + parentRect.h / 2;
        if (parentNode && parentNode.node_type === "function" && childNode.call_site_line != null) {
          const baseLine = parentNode.source_location.lines[0];
          const rel = childNode.call_site_line - baseLine;
          const off = lineOffsetInParent(parentDom, rel);
          if (off) parentY = parentRect.top + dy + off.y;
        }
        from = { x: parentAnchorX, y: parentY };
        to = {
          x: childSide === "left"
            ? childRect.left + dx + childRect.w
            : childRect.left + dx,
          y: childRect.top + dy + childRect.h / 2,
        };
      }
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
