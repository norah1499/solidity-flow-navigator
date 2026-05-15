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

  function renderFunctionNode(node) {
    const wrap = el(
      "div",
      "node node--function" + (node.is_modifier ? " node--modifier" : ""),
    );
    const head = el("div", "node-head");
    head.appendChild(
      el("span", "node-title", node.invoked_via_contract_name + "." + node.full_name),
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

  // Direction-model side assignment (spec §10.3). Sets sideById[id] for
  // every visible non-root non-modifier node:
  //   - walks up the tree from id's parent;
  //   - if a modifier is encountered first → side = "left" (modifier subtree);
  //   - if a sided ancestor is encountered → inherit its side;
  //   - if the root is reached → auto-balance against current vertical extents.
  // Modifiers themselves get no side entry (manual placement separately).
  function assignSide(id) {
    if (id === flow.root.__id) return;
    const node = nodesById.get(id);
    if (isModifierNode(node)) return;
    if (sideById.has(id)) return; // idempotent

    let walker = parentIdOf(id);
    while (walker !== null) {
      if (walker === flow.root.__id) {
        sideById.set(id, autoBalanceSide());
        return;
      }
      const w = nodesById.get(walker);
      if (isModifierNode(w)) {
        sideById.set(id, "left");
        return;
      }
      if (sideById.has(walker)) {
        sideById.set(id, sideById.get(walker));
        return;
      }
      walker = parentIdOf(walker);
    }
    // Walked off the top without hitting root — defensive fallback.
    sideById.set(id, "right");
  }

  // Compare vertical extent of left-side vs right-side first-level subtrees
  // of the root in the previous layout. Returns "right" on tie or empty data
  // (spec §10.3: "Ties resolve in favor of the right side by default").
  // Modifier subtrees do not participate in the balance metric (they're
  // always upper-left, not part of left-vs-right balance).
  function autoBalanceSide() {
    const extent = (target) => {
      let minY = Infinity, maxY = -Infinity;
      sideById.forEach((s, vid) => {
        if (s !== target) return;
        if (inModifierSubtree(vid)) return;
        const r = lastNodeRects.get(vid);
        if (!r) return;
        if (r.top < minY) minY = r.top;
        if (r.top + r.h > maxY) maxY = r.top + r.h;
      });
      return minY === Infinity ? 0 : maxY - minY;
    };
    return extent("right") <= extent("left") ? "right" : "left";
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

    let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
    const nodeRects = new Map();
    g.nodes().forEach((id) => {
      const n = g.node(id);
      const left = n.x - n.width / 2;
      const top = n.y - n.height / 2;
      nodeRects.set(id, { x: n.x, y: n.y, w: n.width, h: n.height, left, top });
      if (left < minX) minX = left;
      if (top < minY) minY = top;
      if (left + n.width > maxX) maxX = left + n.width;
      if (top + n.height > maxY) maxY = top + n.height;
    });
    g.edges().forEach((e) => {
      const edge = g.edge(e);
      edge.points.forEach((p) => {
        if (p.x < minX) minX = p.x;
        if (p.y < minY) minY = p.y;
        if (p.x > maxX) maxX = p.x;
        if (p.y > maxY) maxY = p.y;
      });
    });

    // Manual layout pass for modifiers + their subtrees.
    //
    // For each visible function with one or more visible modifier children,
    // stack the modifiers vertically in the upper-left of the parent in
    // declaration order. Modifier subtrees (Stage 1 placeholder) drop
    // straight down beneath the modifier in a simple column — Stage 2 will
    // route them through dagre as left-side children using the side model.
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

    function recordRect(id, left, top, w, h) {
      nodeRects.set(id, { x: left + w / 2, y: top + h / 2, w, h, left, top });
      if (left < minX) minX = left;
      if (top < minY) minY = top;
      if (left + w > maxX) maxX = left + w;
      if (top + h > maxY) maxY = top + h;
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
        stackY = top + h + MODIFIER_GAP_Y;

        // Stage 1 placeholder for modifier subtrees: vertical column below
        // the modifier. Visit in tree-order so deeper descendants stack
        // beneath their visible ancestors.
        const subDescendants = [];
        visibleIds.forEach((vid) => {
          if (vid === mid) return;
          if (vid.startsWith(mid + "/")) subDescendants.push(vid);
        });
        subDescendants.sort();
        let subY = top + h + MODIFIER_GAP_Y;
        subDescendants.forEach((vid) => {
          const sz = measureDom(vid);
          recordRect(vid, left, subY, sz.w, sz.h);
          subY = subY + sz.h + MODIFIER_GAP_Y;
        });
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

    // Manual edges for modifiers (parent signature line → modifier right
    // edge) and for any expanded modifier-subtree children (Stage 1
    // placeholder: from the tree parent's center to the child's top).
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
        // Modifier child: anchor parent end at the signature line containing
        // the modifier name; modifier end at the modifier's right edge.
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
        // Modifier-subtree descendant (Stage 1 placeholder).
        from = {
          x: parentRect.left + dx + parentRect.w / 2,
          y: parentRect.top + dy + parentRect.h,
        };
        to = {
          x: childRect.left + dx + childRect.w / 2,
          y: childRect.top + dy,
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
