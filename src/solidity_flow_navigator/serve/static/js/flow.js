/* flow.js — Layer 3 frontend: read embedded Flow JSON, lay out with dagre,
 * pan/zoom with d3, render HTML nodes over an SVG edge layer.
 *
 * Pipeline:
 *   1. parse #flow-data
 *   2. assign stable IDs by walking the tree depth-first; build dagre graph
 *   3. render every node DOM element invisibly inside #nodes
 *   4. measure each rendered element, set width/height on the dagre node
 *   5. dagre.layout(g) — produces (x, y) centres + edge polylines
 *   6. position the rendered nodes (left = x - w/2, top = y - h/2),
 *      draw edges into #edges, then make nodes visible
 *   7. compute the laid-out bounding box and apply a d3-zoom fit transform
 *   8. wire the Reset View button to replay the fit transform
 *
 * Visibility flicker is avoided by leaving the #graph wrapper invisible
 * until step 6 finishes, so the user never sees the pre-layout overlap.
 */

(function () {
  "use strict";

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

  // ----- ID strategy ------------------------------------------------------
  //
  // Tree-position IDs ("0", "0/0", "0/1/2") are stable, debuggable, and
  // independent of node-content collisions (e.g. an UnresolvedNode appearing
  // multiple times in different subtrees). The root is always "0".

  function assignIds(node, id) {
    node.__id = id;
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c, i) => assignIds(c, id + "/" + i));
  }
  assignIds(flow.root, "0");

  // ----- DOM rendering ----------------------------------------------------

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
      // source_html is server-rendered Pygments output we trust.
      pre.innerHTML = node.source_html;
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

  // ----- build dagre graph + insert DOM (invisible) ----------------------

  const g = new dagre.graphlib.Graph({ multigraph: false });
  g.setGraph({
    rankdir: "TB",
    nodesep: 28,
    ranksep: 48,
    marginx: 24,
    marginy: 24,
  });
  g.setDefaultEdgeLabel(() => ({}));

  // domByGraphId stores rendered DOM so we can position them after layout.
  const domByGraphId = new Map();

  function addNodeToGraph(node) {
    const dom = renderNode(node);
    dom.setAttribute("data-node-id", node.__id);
    nodesLayer.appendChild(dom);
    domByGraphId.set(node.__id, dom);
    g.setNode(node.__id, { dom });
  }

  function walkAndAdd(node) {
    addNodeToGraph(node);
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c) => {
      walkAndAdd(c);
      g.setEdge(node.__id, c.__id);
    });
  }
  walkAndAdd(flow.root);

  // ----- measure ---------------------------------------------------------
  //
  // Nodes are inserted invisibly (via #graph[data-state=measuring]) at left:0
  // top:0 so the browser still computes their natural width/height. We read
  // those once and feed them back into the dagre node objects.

  graph.dataset.state = "measuring";

  domByGraphId.forEach((dom, id) => {
    const rect = dom.getBoundingClientRect();
    const node = g.node(id);
    node.width = Math.max(80, Math.ceil(rect.width));
    node.height = Math.max(40, Math.ceil(rect.height));
  });

  // ----- layout ----------------------------------------------------------

  dagre.layout(g);

  // ----- position nodes --------------------------------------------------

  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;

  g.nodes().forEach((id) => {
    const n = g.node(id);
    const dom = domByGraphId.get(id);
    const left = n.x - n.width / 2;
    const top = n.y - n.height / 2;
    dom.style.left = left + "px";
    dom.style.top = top + "px";
    // Pin the measured width so flex/wrap-driven layout can't reflow now
    // that the node has a position. Height is allowed to follow content.
    dom.style.width = n.width + "px";
    if (left < minX) minX = left;
    if (top < minY) minY = top;
    if (left + n.width > maxX) maxX = left + n.width;
    if (top + n.height > maxY) maxY = top + n.height;
  });

  // ----- draw edges ------------------------------------------------------

  const line = d3
    .line()
    .x((p) => p.x)
    .y((p) => p.y)
    .curve(d3.curveBasis);

  // d3 attaches paths to a child <g>; keep <defs> intact.
  let edgesGroup = edgesLayer.querySelector("g.edges-group");
  if (!edgesGroup) {
    edgesGroup = document.createElementNS("http://www.w3.org/2000/svg", "g");
    edgesGroup.setAttribute("class", "edges-group");
    edgesLayer.appendChild(edgesGroup);
  } else {
    edgesGroup.innerHTML = "";
  }

  g.edges().forEach((e) => {
    const edge = g.edge(e);
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", line(edge.points));
    path.setAttribute("class", "edge");
    path.setAttribute("marker-end", "url(#arrowhead)");
    edgesGroup.appendChild(path);
    // Track edge bounds too, in case edge routing extends past node bbox.
    edge.points.forEach((p) => {
      if (p.x < minX) minX = p.x;
      if (p.y < minY) minY = p.y;
      if (p.x > maxX) maxX = p.x;
      if (p.y > maxY) maxY = p.y;
    });
  });

  // ----- size SVG layer to laid-out content ------------------------------

  const layoutWidth = Math.ceil(maxX - minX) + 16;
  const layoutHeight = Math.ceil(maxY - minY) + 16;
  // Translate everything so (minX, minY) maps to (0, 0). Apply an offset
  // transform on the edge group AND shift node positions by (-minX, -minY).
  const dx = -minX + 8;
  const dy = -minY + 8;
  edgesGroup.setAttribute("transform", `translate(${dx}, ${dy})`);
  domByGraphId.forEach((dom) => {
    dom.style.left = parseFloat(dom.style.left) + dx + "px";
    dom.style.top = parseFloat(dom.style.top) + dy + "px";
  });

  graph.style.width = layoutWidth + "px";
  graph.style.height = layoutHeight + "px";
  edgesLayer.setAttribute("width", layoutWidth);
  edgesLayer.setAttribute("height", layoutHeight);
  edgesLayer.setAttribute("viewBox", `0 0 ${layoutWidth} ${layoutHeight}`);

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
