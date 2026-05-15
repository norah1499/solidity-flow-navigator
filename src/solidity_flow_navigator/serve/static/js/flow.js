/* flow.js — Layer 3 frontend, legacy all-at-once renderer.
 *
 * Loaded when `solflow --legacy` is in effect; flow-progressive.js is the
 * default. Legacy renders the entire Flow tree at page load: dagre lays
 * out all body-call children top-to-bottom, modifiers are lifted out of
 * dagre and placed manually in the upper-left of their parent (spec §11.6),
 * and every edge originates from the parent's call_site_line (spec §10.2,
 * "Per-line edge anchoring" — shared by both renderers).
 *
 * Pipeline:
 *   1. parse #flow-data, index every node by tree-position id
 *   2. classify modifiers + modifier descendants as manual-layout
 *   3. render every node DOM element inside #nodes (invisible during
 *      measurement); function nodes wrap each source line in
 *      `<span class="src-line" data-line=i>` so edge anchoring can resolve
 *      a call_site_line to an in-node Y
 *   4. dagre receives only non-manual-layout nodes and their edges
 *   5. dagre.layout() → (x, y) centres + edge polylines
 *   6. manual placement pass: modifiers stack vertically upper-left of
 *      their parent in declaration order; modifier subtree descendants
 *      stack straight down beneath the modifier (Stage 1 placeholder per
 *      §11.10, matching flow-progressive.js)
 *   7. translate everything to the (0,0) origin, set DOM positions, draw
 *      edges with pts[0] overridden to the parent's call-site line; manual
 *      edges (parent → modifier, modifier → subtree descendant) drawn
 *      with explicit anchors
 *   8. compute laid-out bounding box, fit transform via d3-zoom
 *
 * Visibility flicker is avoided by leaving #graph at data-state="measuring"
 * (visibility: hidden) until step 7 completes.
 *
 * Per-line anchoring + modifier placement are intentionally implemented in
 * parallel to flow-progressive.js rather than via a shared module — see the
 * v0.5.1 sprint notes (HANDOFF.md) for the decide-and-flag rationale.
 */

(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";

  // ----- read embedded JSON ----------------------------------------------

  const dataEl = document.getElementById("flow-data");
  if (!dataEl) {
    console.error("flow.js: #flow-data not found");
    return;
  }
  const flow = JSON.parse(dataEl.textContent);

  // ----- DOM handles ------------------------------------------------------

  const frame = document.getElementById("graph-frame");
  const graph = document.getElementById("graph");
  const nodesLayer = document.getElementById("nodes");
  const edgesLayer = document.getElementById("edges");
  const resetButton = document.getElementById("reset-view");

  // ----- ID strategy + tree index ----------------------------------------
  //
  // Tree-position IDs ("0", "0/0", "0/1/2") are stable, debuggable, and
  // independent of node-content collisions. `nodesById` gives O(1) lookup
  // of a node's JSON from its ID — used by edge anchoring (to read
  // call_site_line and source_location) and modifier classification.

  const nodesById = new Map();

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

  // ----- modifier classification -----------------------------------------
  //
  // Modifiers and any of their descendants are lifted out of dagre and
  // positioned manually (upper-left of parent for modifiers, vertical
  // column below the modifier for modifier subtrees). This mirrors
  // flow-progressive.js exactly — the renderers differ only in the
  // rendering trigger (page-load vs. click).

  const modifierIds = new Set();
  nodesById.forEach((node, id) => {
    if (isModifierNode(node)) modifierIds.add(id);
  });

  function inManualLayout(id) {
    for (const mid of modifierIds) {
      if (id === mid || id.startsWith(mid + "/")) return true;
    }
    return false;
  }

  // ----- DOM rendering ---------------------------------------------------

  function el(tag, className, text) {
    const e = document.createElement(tag);
    if (className) e.className = className;
    if (text != null) e.textContent = text;
    return e;
  }

  // Wrap each source-code line in `<span class="src-line" data-line=i>` so
  // we can compute the Y of any line within a rendered node — used by edge
  // anchoring (per-line origination). Mirrors the per-line wrapping in
  // flow-progressive.js but without the `--call` interaction class, since
  // legacy renders everything at once and source lines are not click targets.
  function wrapSourceLines(sourceHtml) {
    if (!sourceHtml) return "";
    return sourceHtml
      .split("\n")
      .map((lineHtml, i) =>
        '<span class="src-line" data-line="' + i + '">' + lineHtml + "</span>",
      )
      .join("\n");
  }

  function renderFunctionNode(node) {
    const wrap = el(
      "div",
      "node node--function" + (node.is_modifier ? " node--modifier" : ""),
    );
    const head = el("div", "node-head");
    head.appendChild(el("span", "node-title", node.invoked_via_contract_name + "." + node.full_name));

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
      pre.innerHTML = wrapSourceLines(node.source_html);
      wrap.appendChild(pre);
    }

    if (node.builtins_used && node.builtins_used.length) {
      wrap.appendChild(el("div", "node-builtins", "builtins: " + node.builtins_used.join(", ")));
    }

    return wrap;
  }

  function renderUnresolvedNode(node) {
    const wrap = el("div", "node node--unresolved");
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    title.appendChild(el("code", null, node.descriptor || "(unknown)"));
    head.appendChild(title);

    const badges = el("span", "node-badges");
    badges.appendChild(el("span", "badge badge-unresolved", "unresolved: " + node.reason));
    head.appendChild(badges);
    wrap.appendChild(head);

    const meta = el("div", "node-meta");
    meta.appendChild(document.createTextNode("kind: "));
    meta.appendChild(el("code", null, node.raw_kind));
    if (node.raw_subkind) {
      meta.appendChild(document.createTextNode(" / "));
      meta.appendChild(el("code", null, node.raw_subkind));
    }
    wrap.appendChild(meta);
    return wrap;
  }

  function renderExternalNode(node) {
    const wrap = el("div", "node node--external");
    const head = el("div", "node-head");
    const title = el("span", "node-title");
    if (node.target_contract_name) {
      title.appendChild(el("code", null, node.target_canonical_name));
    } else {
      // Free function / `using for` wrapper — no declarer contract per
      // §11.10. target_canonical_name carries no contract prefix, so we
      // render the bare function name and label the missing contract
      // explicitly rather than leaving a blank slot in the title.
      title.appendChild(el("code", null, node.target_function_name));
      title.appendChild(document.createTextNode(" "));
      title.appendChild(el("span", "node-no-contract", "[no contract]"));
    }
    head.appendChild(title);

    const badges = el("span", "node-badges");
    badges.appendChild(el("span", "badge badge-external", "external"));
    head.appendChild(badges);
    wrap.appendChild(head);

    const meta = el("div", "node-meta");
    meta.appendChild(el("span", "node-path", node.source_path));
    wrap.appendChild(meta);
    return wrap;
  }

  function renderNode(node) {
    if (node.node_type === "function") return renderFunctionNode(node);
    if (node.node_type === "unresolved") return renderUnresolvedNode(node);
    if (node.node_type === "external") return renderExternalNode(node);
    throw new Error("flow.js: unknown node_type " + node.node_type);
  }

  // ----- build DOM tree + dagre graph -----------------------------------
  //
  // Every node gets a DOM element. Only non-manual-layout nodes (i.e.,
  // not a modifier and not a modifier-subtree descendant) participate in
  // dagre; their parent→child edges are dagre edges too, with the same
  // exclusion. The remaining edges (parent→modifier and within-modifier-
  // subtree) are drawn manually below.

  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setGraph({
    rankdir: "TB",
    nodesep: 28,
    ranksep: 48,
    marginx: 24,
    marginy: 24,
  });
  g.setDefaultEdgeLabel(() => ({}));

  const domByGraphId = new Map();

  function walkAndAdd(node) {
    const dom = renderNode(node);
    dom.setAttribute("data-node-id", node.__id);
    nodesLayer.appendChild(dom);
    domByGraphId.set(node.__id, dom);

    if (!inManualLayout(node.__id)) {
      g.setNode(node.__id, { dom });
    }

    if (node.node_type !== "function") return;
    (node.children || []).forEach((c) => {
      walkAndAdd(c);
      if (!inManualLayout(node.__id) && !inManualLayout(c.__id)) {
        g.setEdge(node.__id, c.__id);
      }
    });
  }
  walkAndAdd(flow.root);

  // ----- measure ---------------------------------------------------------
  //
  // Nodes are inserted invisibly (via #graph[data-state=measuring]) at
  // left:0 top:0 so the browser still computes their natural width/height.
  // We measure every DOM element (dagre and manual-layout alike) — the
  // manual-layout ones still need a size for the modifier-column geometry.

  graph.dataset.state = "measuring";

  function measureDom(id) {
    const dom = domByGraphId.get(id);
    const rect = dom.getBoundingClientRect();
    return {
      w: Math.max(80, Math.ceil(rect.width)),
      h: Math.max(40, Math.ceil(rect.height)),
    };
  }

  g.nodes().forEach((id) => {
    const { w, h } = measureDom(id);
    const n = g.node(id);
    n.width = w;
    n.height = h;
  });

  // ----- layout ----------------------------------------------------------

  dagre.layout(g);

  // ----- build nodeRects from dagre, then run manual placement -----------

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

  // Modifiers are placed manually upper-left of their parent in declaration
  // order; modifier subtree descendants (if any) stack straight down beneath
  // the modifier in a simple column. Spec §11.6 + §11.10 placeholder, same
  // as flow-progressive.js.
  const MODIFIER_GAP_X = 24;
  const MODIFIER_GAP_Y = 8;

  const modifiersByParent = new Map();
  modifierIds.forEach((mid) => {
    const pid = parentIdOf(mid);
    if (!pid || !nodesById.has(pid)) return;
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

      const subDescendants = [];
      nodesById.forEach((_node, vid) => {
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

  // ----- size SVG layer, translate to (0,0) origin, position DOM --------

  const dx = -minX + 8;
  const dy = -minY + 8;
  const layoutWidth = Math.ceil(maxX - minX) + 16;
  const layoutHeight = Math.ceil(maxY - minY) + 16;

  graph.style.width = layoutWidth + "px";
  graph.style.height = layoutHeight + "px";
  edgesLayer.setAttribute("width", layoutWidth);
  edgesLayer.setAttribute("height", layoutHeight);
  edgesLayer.setAttribute("viewBox", `0 0 ${layoutWidth} ${layoutHeight}`);

  domByGraphId.forEach((dom, id) => {
    const r = nodeRects.get(id);
    if (!r) return;
    dom.style.left = (r.left + dx) + "px";
    dom.style.top = (r.top + dy) + "px";
    // Pin the measured width so flex/wrap-driven layout can't reflow now
    // that the node has a position. Height is allowed to follow content.
    dom.style.width = r.w + "px";
  });

  // ----- draw edges ------------------------------------------------------

  const line = d3.line().x((p) => p.x).y((p) => p.y).curve(d3.curveBasis);

  let edgesGroup = edgesLayer.querySelector("g.edges-group");
  if (!edgesGroup) {
    edgesGroup = document.createElementNS(SVG_NS, "g");
    edgesGroup.setAttribute("class", "edges-group");
    edgesLayer.appendChild(edgesGroup);
  } else {
    edgesGroup.innerHTML = "";
  }

  // Given a parent DOM and a parent-relative line index, return the line's
  // x-extents and y-center within the parent. Returns null if the line
  // span isn't found (e.g. call_site_line falls outside the rendered
  // source slice). Identical to flow-progressive.js — see decide-and-flag
  // note at top of file for why these helpers are kept parallel rather
  // than extracted.
  function lineOffsetInParent(parentDom, lineIdx) {
    const span = parentDom.querySelector('.src-line[data-line="' + lineIdx + '"]');
    if (!span) return null;
    return {
      xLeft: 0,
      xRight: parentDom.offsetWidth,
      y: span.offsetTop + span.offsetHeight / 2,
    };
  }

  // Dagre edges: parent → body-call child. Override pts[0] to the parent's
  // call-site line (parent.right_edge, line.y) so each arrow originates
  // from the source line of the call rather than the parent's centre/edge.
  g.edges().forEach((e) => {
    const edge = g.edge(e);
    const parentId = e.v;
    const childId = e.w;
    const parentNode = nodesById.get(parentId);
    const childNode = nodesById.get(childId);
    const parentDom = domByGraphId.get(parentId);
    const parentRect = nodeRects.get(parentId);

    let pts = edge.points.map((p) => ({ x: p.x + dx, y: p.y + dy }));

    if (parentNode.node_type === "function" && childNode.call_site_line != null) {
      const baseLine = parentNode.source_location.lines[0];
      const rel = childNode.call_site_line - baseLine;
      const off = lineOffsetInParent(parentDom, rel);
      if (off) {
        pts[0] = {
          x: parentRect.left + dx + off.xRight,
          y: parentRect.top + dy + off.y,
        };
      }
    }

    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", line(pts));
    path.setAttribute("class", "edge");
    path.setAttribute("marker-end", "url(#arrowhead)");
    edgesGroup.appendChild(path);
  });

  // Manual edges: parent → modifier (anchored at the signature line
  // carrying the modifier name), and modifier → modifier-subtree
  // descendant (vertical drop, parent-center bottom to child-center top).
  function drawManualEdge(parentId, childId) {
    const parentRect = nodeRects.get(parentId);
    const childRect = nodeRects.get(childId);
    if (!parentRect || !childRect) return;
    const parentNode = nodesById.get(parentId);
    const childNode = nodesById.get(childId);
    const parentDom = domByGraphId.get(parentId);

    let from, to;
    if (isModifierNode(childNode)) {
      let parentY = parentRect.top + dy + parentRect.h / 2;
      if (parentNode.node_type === "function" && childNode.call_site_line != null) {
        const baseLine = parentNode.source_location.lines[0];
        const rel = childNode.call_site_line - baseLine;
        const off = lineOffsetInParent(parentDom, rel);
        if (off) parentY = parentRect.top + dy + off.y;
      }
      from = { x: parentRect.left + dx, y: parentY };
      to = {
        x: childRect.left + dx + childRect.w,
        y: childRect.top + dy + childRect.h / 2,
      };
    } else {
      from = {
        x: parentRect.left + dx + parentRect.w / 2,
        y: parentRect.top + dy + parentRect.h,
      };
      to = {
        x: childRect.left + dx + childRect.w / 2,
        y: childRect.top + dy,
      };
    }
    const midPt = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 };

    const path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", line([from, midPt, to]));
    path.setAttribute(
      "class",
      "edge" + (isModifierNode(childNode) ? " edge-modifier" : ""),
    );
    path.setAttribute("marker-end", "url(#arrowhead)");
    edgesGroup.appendChild(path);
  }

  nodesById.forEach((_node, id) => {
    if (!inManualLayout(id)) return;
    const pid = parentIdOf(id);
    if (!pid) return;
    drawManualEdge(pid, id);
  });

  // ----- reveal + d3-zoom -----------------------------------------------

  graph.dataset.state = "ready";

  function fitTransform() {
    const fr = frame.getBoundingClientRect();
    const padding = 16;
    const sx = (fr.width - padding * 2) / layoutWidth;
    const sy = (fr.height - padding * 2) / layoutHeight;
    const scale = Math.min(1, Math.min(sx, sy));
    const tx = (fr.width - layoutWidth * scale) / 2;
    const ty = padding;
    return d3.zoomIdentity.translate(tx, ty).scale(scale);
  }

  const zoom = d3
    .zoom()
    .scaleExtent([0.05, 4])
    .on("zoom", (event) => {
      const t = event.transform;
      graph.style.transform = `translate(${t.x}px, ${t.y}px) scale(${t.k})`;
    });

  const frameSel = d3.select(frame);
  frameSel.call(zoom);

  function applyFit(animated) {
    const t = fitTransform();
    if (animated) {
      frameSel.transition().duration(220).call(zoom.transform, t);
    } else {
      frameSel.call(zoom.transform, t);
    }
  }

  applyFit(false);

  if (resetButton) {
    resetButton.addEventListener("click", () => applyFit(true));
  }

  // Reset View keybindings: `0` and `r`. Suppressed when an editable
  // element is focused so they don't hijack typing in any future
  // input/textarea on the page.
  window.addEventListener("keydown", (e) => {
    if (e.key !== "0" && e.key !== "r" && e.key !== "R") return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    applyFit(true);
  });

  // Preserve the user's current zoom on resize. The d3-zoom transform is
  // applied to the inner #graph element, which is laid out in absolute
  // coordinates — resizing the frame just changes the viewport over that
  // content, no retransform required. The user can hit Reset View (button
  // or `0`/`r`) to recentre if they want fit-to-frame back.
})();
