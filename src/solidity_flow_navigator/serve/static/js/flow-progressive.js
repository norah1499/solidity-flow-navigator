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
  // v0.21.0 (spec §10.2 "Flow minimap"): collapsible overview overlay.
  const minimapEl = document.getElementById("minimap");
  const minimapBody = minimapEl && minimapEl.querySelector(".minimap-body");
  const minimapCanvas = document.getElementById("minimap-canvas");
  const minimapExtent = document.getElementById("minimap-extent");
  const minimapReticle = document.getElementById("minimap-reticle");
  const minimapToggle = document.getElementById("minimap-toggle");
  // v0.22.0 (spec §10.2 "Flow call-tree sidebar"): collapsible left navigator.
  const sidebarEl = document.getElementById("call-tree");
  const calltreeList = document.getElementById("calltree-list");
  const calltreeFilter = document.getElementById("calltree-filter");
  const calltreeFilterClear = document.getElementById("calltree-filter-clear");
  const calltreeFilterCount = document.getElementById("calltree-filter-count");
  const calltreeCollapse = document.getElementById("calltree-collapse");
  const calltreeReopen = document.getElementById("calltree-reopen");
  const calltreeResize = document.getElementById("calltree-resize");
  const maximizeToggle = document.getElementById("maximize-toggle");
  const fullscreenRestore = document.getElementById("fullscreen-restore");

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
  let ctNodeBoxes = new Map(); // id -> {cx, cy} rendered centre (centred-node highlight)

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

  // Tag occurrences of a struct's member type names inside its own source so
  // they take the nested-teal accent (.param-struct-ref), visually linking a
  // field's type to its nested dropdown. The Pygments Solidity lexer leaves
  // USER-defined type names as bare, unwrapped text nodes (direct children of
  // the <pre>) — builtins/field names get <span>s — so we wrap the matching
  // runs of those bare text nodes in a .param-struct-ref span. Operating only on
  // direct text-node children targets exactly those unwrapped type tokens and
  // naturally skips names in comment / field-name spans AND the struct's own
  // name in its `struct State {` header (a `.nv` span, not a direct text node),
  // so no own-name exclusion is needed — which is why a qualified self-named
  // member like `Position.State` (a bare-text ref) does get tagged. `tokens`
  // already carries the bare member names plus, for collision-qualified members,
  // their qualifier segment (e.g. `Position`).
  function colorizeMemberRefs(pre, tokens) {
    const names = Array.from(new Set(tokens.filter(Boolean)));
    if (names.length === 0) return;
    const escaped = names.map((n) => n.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
    const re = new RegExp("\\b(?:" + escaped.join("|") + ")\\b", "g");
    Array.prototype.slice.call(pre.childNodes).forEach((node) => {
      if (node.nodeType !== Node.TEXT_NODE) return;
      const text = node.nodeValue;
      re.lastIndex = 0;
      if (!re.test(text)) return;
      re.lastIndex = 0;
      const frag = document.createDocumentFragment();
      let last = 0;
      let m;
      while ((m = re.exec(text)) !== null) {
        if (m.index > last) {
          frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        }
        const span = el("span", "param-struct-ref");
        span.textContent = m[0];
        frag.appendChild(span);
        last = m.index + m[0].length;
      }
      if (last < text.length) {
        frag.appendChild(document.createTextNode(text.slice(last)));
      }
      node.parentNode.replaceChild(frag, node);
    });
  }

  // v0.19 signature-type panel (spec §10.2). A collapsed "type definitions:"
  // disclosure above the source body. Its children are the types named directly
  // in the signature (signature_type_roots); each struct nests the types it
  // references through its members ABOVE its own source, recursively, so the
  // type graph reads as a tree (e.g. TickInfo nested inside Pool.State). Two
  // distinct types sharing a bare name are shown qualified (Pool.State /
  // Position.State); unique names stay bare. Cycle-safe: a type already on the
  // current ancestor path renders as a leaf (its own source, no re-nesting).
  // Which dropdowns are open persists per-Flow in localStorage, keyed by the
  // node id + the nesting path so the same type under two parents is
  // independent; Expand all / Collapse all toggle every dropdown (setAllTypeDefs).
  // The type bodies are read-only reference: deliberately NOT routed through
  // wrapCallLines / colorizeCallNames. Omitted when the signature names no
  // user-defined type.
  function appendSignatureTypes(wrap, node) {
    const pool = (node.signature_types || []).filter((t) => t && t.source_html);
    const roots = (node.signature_type_roots || []).filter((c) =>
      pool.some((t) => t.canonical_name === c),
    );
    if (roots.length === 0) return;
    const open = readOpenTypeDefs();
    const nodeId = node.__id;
    const byCanon = {};
    pool.forEach((t) => {
      byCanon[t.canonical_name] = t;
    });
    // Qualify a name shared by 2+ DISTINCT types anywhere in the pool; otherwise
    // show the bare name.
    const canonsByName = {};
    pool.forEach((t) => {
      (canonsByName[t.name] = canonsByName[t.name] || new Set()).add(
        t.canonical_name,
      );
    });
    const displayName = (t) =>
      canonsByName[t.name] && canonsByName[t.name].size > 1
        ? t.canonical_name || t.name
        : t.name;
    const kindWord = (t) =>
      t.kind === "enum" ? "enum " : t.kind === "udvt" ? "type " : "struct ";

    // One <details> per type, members nested above its own source.
    function renderType(canonical, ancestors, pathPrefix) {
      const t = byCanon[canonical];
      if (!t) return null;
      const item = el("details", "param-struct-item");
      const key = nodeId + "::" + pathPrefix + canonical;
      item.open = open.has(key);
      item.dataset.tdKey = key;
      item.addEventListener("toggle", () => persistTypeDefOpen(key, item.open));
      item.appendChild(
        el("summary", "param-struct-item-summary", kindWord(t) + displayName(t)),
      );
      // Cycle guard: don't re-nest a type already on the ancestor path.
      const members = ancestors.has(canonical)
        ? []
        : (t.member_type_canonical_names || []).filter((m) => byCanon[m]);
      if (members.length > 0) {
        const nested = el("div", "param-struct-nested");
        const childAncestors = new Set(ancestors);
        childAncestors.add(canonical);
        members.forEach((m) => {
          const child = renderType(m, childAncestors, pathPrefix + canonical + ">");
          if (child) nested.appendChild(child);
        });
        item.appendChild(nested);
      }
      const pre = el(
        "pre",
        "param-struct-body src" +
          (members.length > 0 ? " param-struct-body--after-members" : ""),
      );
      pre.innerHTML = t.source_html;
      // Tag the member type names inside this struct's source: each member's
      // bare name, plus — for a member shown qualified because its bare name
      // collides (e.g. Position.State) — the qualifier segment(s) of its
      // canonical, so the whole qualified reference (Position AND State) tints.
      const refTokens = [];
      members.forEach((m) => {
        const md = byCanon[m];
        refTokens.push(md.name);
        if (canonsByName[md.name] && canonsByName[md.name].size > 1) {
          md.canonical_name
            .split(".")
            .slice(0, -1)
            .forEach((seg) => refTokens.push(seg));
        }
      });
      colorizeMemberRefs(pre, refTokens);
      item.appendChild(pre);
      return item;
    }

    const outer = el("details", "node-param-structs");
    const outerKey = nodeId + "::*";
    outer.open = open.has(outerKey);
    outer.dataset.tdKey = outerKey;
    outer.addEventListener("toggle", () =>
      persistTypeDefOpen(outerKey, outer.open),
    );
    const rootNames = roots
      .map((c) => displayName(byCanon[c]))
      .join(", ");
    outer.appendChild(
      el("summary", "node-param-structs-summary", "type definitions: " + rootNames),
    );
    roots.forEach((c) => {
      const item = renderType(c, new Set(), "");
      if (item) outer.appendChild(item);
    });
    wrap.appendChild(outer);
  }

  // Expand all / Collapse all act on every type-definition dropdown in every
  // rendered node (spec §10.2), alongside the call-site expansion. Keyed by
  // data-td-key so the persisted open-set stays in sync; collapse-all clears the
  // whole set (including dropdowns on nodes not currently in the DOM).
  function setAllTypeDefs(isOpen) {
    const all = document.querySelectorAll("[data-td-key]");
    all.forEach((d) => {
      d.open = isOpen;
    });
    if (isOpen) {
      const set = readOpenTypeDefs();
      all.forEach((d) => set.add(d.dataset.tdKey));
      writeOpenTypeDefs(set);
    } else {
      writeOpenTypeDefs(new Set());
    }
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

    // v0.19 signature-type panel (spec §10.2): collapsed <details> above the
    // source body with the full struct/enum definitions this function's
    // parameters/returns name (and their nested-struct closure).
    appendSignatureTypes(wrap, node);

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

  // Colorize call-name tokens inside FunctionNode source bodies. For each
  // resolved child, find where its call actually appears in the source — an
  // identifier immediately followed by "(" — and wrap that token in
  // <span class="src-call-name">. Scanning the whole body (scanCallSites)
  // rather than only the child's recorded call_site_line is what lets a call
  // written on a CONTINUATION line of a multi-line statement still be colored:
  // Slither records every sub-call of `f(\n  g(),\n  h()\n)` at the statement's
  // first line, so g/h sit on lines the old per-line search never looked at
  // (Aave `BorrowLogic.executeBorrow(... _msgSender(), getPriceOracle() ...)`
  // is the case that exposed this).
  //
  // A call whose name sits inside another call's argument list (the `bar` in
  // `foo(bar(x))`, or g/h above) is additionally tagged `src-call-name--arg`
  // so CSS gives it a distinct color. Interaction is untouched: only the
  // recorded call_site_line is a `.src-line--call` click target, so a nested
  // call still expands together with its statement and clicking its name does
  // nothing special.
  function colorizeCallNamesAcrossLines(pre, funcNode) {
    const childNames = new Set();
    (funcNode.children || []).forEach((c) => {
      const name = nameForCall(c);
      if (name) childNames.add(name);
    });
    if (childNames.size === 0) return;
    const occ = scanCallSites(funcNode.source_code || "").filter((o) =>
      childNames.has(o.name)
    );
    if (occ.length === 0) return;
    const byLine = new Map(); // lineIdx -> [occurrence, ...]
    occ.forEach((o) => {
      const arr = byLine.get(o.line) || [];
      arr.push(o);
      byLine.set(o.line, arr);
    });
    const lineSpans = new Map();
    pre.querySelectorAll(".src-line").forEach((s) => {
      lineSpans.set(parseInt(s.getAttribute("data-line"), 10), s);
    });
    byLine.forEach((occs, lineIdx) => {
      const span = lineSpans.get(lineIdx);
      if (span) wrapOccurrencesInLine(span, occs);
    });
  }

  // Scan a function's source for call sites and their argument-nesting.
  // Returns one record per call site (an identifier immediately followed by
  // "("): { line, col, len, name, nested } — `line` is the 0-based line index
  // (matching `.src-line` data-line), `col`/`len` locate the name token within
  // that line, and `nested` is true when the call sits inside another call's
  // argument list. String, char, and comment spans are skipped. Control-flow
  // keywords (if/for/while/...) do NOT open an argument context, so a call in
  // their parenthesized clause is not marked nested; require/assert/revert are
  // deliberately absent from that list — they are function-like, so a call in
  // their arguments is genuinely nested.
  function scanCallSites(source) {
    const occ = [];
    const stack = []; // true = call paren, false = grouping paren
    let r = 0;
    let c = 0;
    let callDepth = 0;
    let state = "code"; // code | line | block | str | chr
    let prevSig = ""; // last non-whitespace code char
    let curWord = ""; // identifier run currently being read
    let prevWord = ""; // last completed identifier run
    let prevCh = "";
    let esc = false;
    let blockOpen = -1;
    const NON_CALL_KW = new Set([
      "if",
      "for",
      "while",
      "switch",
      "catch",
      "do",
      "return",
      "returns",
    ]);
    for (let k = 0; k < source.length; k++) {
      const ch = source[k];
      if (ch === "\n") {
        r += 1;
        c = 0;
        if (state === "line") state = "code";
        if (curWord !== "") {
          prevWord = curWord;
          curWord = "";
        }
        prevCh = ch;
        continue;
      }
      if (state === "code") {
        const nx = source[k + 1];
        if (ch === "/" && nx === "/") {
          state = "line";
        } else if (ch === "/" && nx === "*") {
          state = "block";
          blockOpen = k;
        } else if (ch === '"') {
          state = "str";
          esc = false;
        } else if (ch === "'") {
          state = "chr";
          esc = false;
        } else if (ch === "(") {
          // Call paren iff it follows a function name: an identifier that is
          // not a control-flow keyword (the word right before "(", allowing a
          // space as in `foo (x)`), or a ")"/"]" (chained / indexed call like
          // `f()(x)` / `arr[i](x)`). Grouping parens (`(a + b)`, `if (...)`)
          // do not count, so calls inside them are not argument-nested.
          const word = curWord !== "" ? curWord : prevWord;
          let isCall;
          if (word !== "") isCall = !NON_CALL_KW.has(word);
          else isCall = prevSig === ")" || prevSig === "]";
          // Record the call-name occurrence only when the name abuts "(" (the
          // form a member/free call always takes). `nested` is the enclosing
          // call depth BEFORE this paren opens.
          if (isCall && curWord !== "") {
            occ.push({
              line: r,
              col: c - curWord.length,
              len: curWord.length,
              name: curWord,
              nested: callDepth >= 1,
            });
          }
          stack.push(isCall);
          if (isCall) callDepth += 1;
        } else if (ch === ")") {
          const wasCall = stack.pop();
          if (wasCall) callDepth = Math.max(0, callDepth - 1);
        }
        if (/[A-Za-z0-9_$]/.test(ch)) {
          curWord += ch;
        } else if (curWord !== "") {
          prevWord = curWord;
          curWord = "";
        }
        if (!/\s/.test(ch)) prevSig = ch;
      } else if (state === "block") {
        if (prevCh === "*" && ch === "/" && k > blockOpen + 1) state = "code";
      } else if (state === "str") {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === '"') state = "code";
      } else if (state === "chr") {
        if (esc) esc = false;
        else if (ch === "\\") esc = true;
        else if (ch === "'") state = "code";
      }
      prevCh = ch;
      c += 1;
    }
    return occ;
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

  // Wrap the given call-site occurrences (each {col, len, nested}) inside one
  // `.src-line` span. Walks the line's text nodes, tracking the running column
  // (HTML entities decode 1:1, so textContent columns equal source columns),
  // and replaces each occurrence's name token with a `<span class="src-call-
  // name">` (plus `--arg` when nested). Occurrences that fall entirely within a
  // single text node are wrapped; an identifier split across Pygments spans
  // (rare for a plain name) is left as-is.
  function wrapOccurrencesInLine(span, occs) {
    occs.sort((a, b) => a.col - b.col);
    const walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT);
    const pending = [];
    let colInLine = 0;
    while (walker.nextNode()) {
      const text = walker.currentNode;
      const content = text.textContent;
      const start = colInLine;
      const end = colInLine + content.length;
      colInLine = end;
      const here = occs.filter((o) => o.col >= start && o.col + o.len <= end);
      if (here.length > 0) pending.push({ text, content, start, here });
    }
    pending.forEach(({ text, content, start, here }) => {
      const frag = document.createDocumentFragment();
      let pos = 0; // local offset within content
      here.forEach((o) => {
        const local = o.col - start;
        if (local < pos) return; // overlap guard
        if (local > pos) {
          frag.appendChild(document.createTextNode(content.slice(pos, local)));
        }
        const nameSpan = document.createElement("span");
        nameSpan.className = o.nested
          ? "src-call-name src-call-name--arg"
          : "src-call-name";
        nameSpan.textContent = content.slice(local, local + o.len);
        frag.appendChild(nameSpan);
        pos = local + o.len;
      });
      if (pos < content.length) frag.appendChild(document.createTextNode(content.slice(pos)));
      text.parentNode.replaceChild(frag, text);
    });
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
    // v0.23.0 (§10.2): the root/entry-point node carries a persistent warm
    // boundary ring. The active node's blue ring is applied separately by
    // applyGraphActiveRing() (it tracks the call-tree selection).
    if (id === flow.root.__id) dom.classList.add("node--entry");
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

  // v0.19: which signature-type panels (outer + per-type inner dropdowns) the
  // auditor has opened, persisted per-Flow so they survive a reload exactly
  // like the expanded call sites above (spec §10.2). Keyed by node id + type so
  // a panel restores on whichever node it belongs to; stale keys (source
  // changed) are simply never matched.
  const TYPEDEFS_KEY =
    "solflow:typedefs:" + (dataEl.dataset.flowId || location.pathname);

  function readOpenTypeDefs() {
    try {
      return new Set(
        JSON.parse(window.localStorage.getItem(TYPEDEFS_KEY) || "[]"),
      );
    } catch (_err) {
      return new Set();
    }
  }

  function writeOpenTypeDefs(set) {
    try {
      window.localStorage.setItem(TYPEDEFS_KEY, JSON.stringify([...set]));
    } catch (_err) {
      // localStorage unavailable — persistence is a convenience, never breaks.
    }
  }

  function persistTypeDefOpen(key, isOpen) {
    const set = readOpenTypeDefs();
    if (isOpen) set.add(key);
    else set.delete(key);
    writeOpenTypeDefs(set);
  }

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
          // No length<2 short-circuit: a node ALONE in its rank must still
          // be anchored. Steps (a) intra-group and (b) inter-group below
          // no-op for a single child/group, but step (c) re-anchors the
          // lone child to its parent's (already-finalized, possibly shifted)
          // call-site-line Y. Without this, a single-call chain hanging off
          // a parent that the forward-separation sweep pushed DOWN keeps
          // dagre's original Y and detaches from the shifted parent — the
          // "deep node flies way up, long stretched edge" artifact, seen at
          // depth 3-4 in v4-core (applyDelta -> NonzeroDeltaCount.decrement,
          // Hooks.callHook -> CustomRevert.bubbleUpAndRevertWith). The pass
          // mutates parent Y in place (n.y, below) but only re-anchors
          // descendants rank-by-rank; a skipped single-node rank never
          // catches up, so it must flow through here too.
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
    // v0.21.0 (spec §10.2): one hook for every relayout path. nodeRects/dx/dy
    // are the exact final positions (not mid-transition dom.style values).
    // v0.22.0: cache each visible node's rendered centre (graph coords) for the
    // centred-node sidebar highlight — read without forcing DOM reflow.
    ctNodeBoxes = new Map();
    nodeRects.forEach((r, id) => {
      ctNodeBoxes.set(id, { cx: r.x + dx, cy: r.y + dy });
    });
    renderMinimap(nodeRects, dx, dy, layoutWidth, layoutHeight);
    buildCallTree(); // v0.22.0: refresh the sidebar from the new visible set
    updateActiveFromViewport(); // highlight the node now at the viewport centre
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
      updateMinimapIndicators(); // v0.21.0: keep the extent + reticle in sync
      updateActiveFromViewport(); // v0.22.0: highlight the centred node in the sidebar
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

  // ----- minimap (spec §10.2 "Flow minimap") ------------------------------
  //
  // A navigation launcher, not a viewport mirror. renderMinimap() draws every
  // visible node as a rectangle, scaled to fit the panel, from the SAME
  // nodeRects/dx/dy the relayout just computed (exact final positions, unlike
  // mid-transition dom.style.left). updateMinimapIndicators() draws two markers
  // from the live d3-zoom transform: a really-faint outline of the true visible
  // region (#minimap-extent) and a FIXED-size aim box at the view centre
  // (#minimap-reticle). Aiming the panel recenters there and zooms IN to a
  // readable level (minimapRelocate), never zooming out. It mirrors the rendered
  // graph only — a view aid, not a map of the full latent tree — so it never
  // touches the expansion model or P4. mmScale/mmOffset/mmW/mmH map graph coords
  // → panel coords. sizeMinimap() makes the panel track the window.
  let mmScale = 0;
  let mmOffsetX = 0;
  let mmOffsetY = 0;
  let mmW = 0;
  let mmH = 0;
  let mmLast = null; // last layout snapshot, so a re-expanded minimap redraws
  const MINIMAP_KEY = "solflow:minimap-collapsed"; // global UI preference
  const MM_TARGET_ZOOM = 0.9; // readable level an aim dives into (tunable)
  const MM_RETICLE_FRAC = 0.12; // reticle side as a fraction of the panel
  const MM_MIN_W = 146;
  const MM_MAX_W = 268; // responsive width clamps
  const MM_MIN_H = 96;
  const MM_MAX_H = 196; // responsive height clamps

  // Size the panel relative to the graph frame, clamped so it never gets tiny
  // or eats the canvas. The body height is set inline; the canvas is 100%/100%.
  function sizeMinimap() {
    if (!minimapEl || !minimapBody) return;
    const fr = frame.getBoundingClientRect();
    if (!fr.width || !fr.height) return;
    // Match the frame's aspect ratio so the panel's shape tracks the window.
    // Size from a fraction of the frame width, then keep both dims in range,
    // re-deriving the other so the aspect holds at the clamp boundaries.
    const aspect = fr.height / fr.width;
    let w = Math.max(MM_MIN_W, Math.min(MM_MAX_W, Math.round(fr.width * 0.17)));
    let h = Math.round(w * aspect);
    if (h < MM_MIN_H) {
      h = MM_MIN_H;
      w = Math.round(h / aspect);
    } else if (h > MM_MAX_H) {
      h = MM_MAX_H;
      w = Math.round(h / aspect);
    }
    w = Math.max(MM_MIN_W, Math.min(MM_MAX_W, w));
    minimapEl.style.width = w + "px";
    minimapBody.style.height = h + "px";
  }

  function renderMinimap(nodeRects, dx, dy, layoutW, layoutH) {
    if (!minimapCanvas) return;
    mmLast = { nodeRects, dx, dy, layoutW, layoutH };
    const cssW = minimapCanvas.clientWidth;
    const cssH = minimapCanvas.clientHeight;
    if (!cssW || !cssH || !layoutW || !layoutH) {
      mmScale = 0; // collapsed/hidden — nothing to draw; mmLast redraws on show
      return;
    }
    const dpr = window.devicePixelRatio || 1;
    minimapCanvas.width = Math.round(cssW * dpr);
    minimapCanvas.height = Math.round(cssH * dpr);
    const ctx = minimapCanvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    const pad = 4;
    mmScale = Math.min((cssW - pad * 2) / layoutW, (cssH - pad * 2) / layoutH);
    mmOffsetX = (cssW - layoutW * mmScale) / 2;
    mmOffsetY = (cssH - layoutH * mmScale) / 2;
    mmW = cssW;
    mmH = cssH;

    ctx.fillStyle =
      getComputedStyle(document.documentElement)
        .getPropertyValue("--mm-node")
        .trim() || "#9a978c";
    nodeRects.forEach((r) => {
      const x = (r.left + dx) * mmScale + mmOffsetX;
      const y = (r.top + dy) * mmScale + mmOffsetY;
      ctx.fillRect(x, y, Math.max(1, r.w * mmScale), Math.max(1, r.h * mmScale));
    });
    updateMinimapIndicators();
  }

  function renderMinimapFromLast() {
    if (mmLast) {
      renderMinimap(mmLast.nodeRects, mmLast.dx, mmLast.dy, mmLast.layoutW, mmLast.layoutH);
    }
  }

  function updateMinimapIndicators() {
    if (!mmScale || !minimapEl || minimapEl.hasAttribute("data-collapsed")) return;
    const t = d3.zoomTransform(frame);
    const fr = frame.getBoundingClientRect();

    // Faint extent: the true visible region projected into the panel, clamped.
    if (minimapExtent) {
      let left = ((0 - t.x) / t.k) * mmScale + mmOffsetX;
      let top = ((0 - t.y) / t.k) * mmScale + mmOffsetY;
      let right = ((fr.width - t.x) / t.k) * mmScale + mmOffsetX;
      let bottom = ((fr.height - t.y) / t.k) * mmScale + mmOffsetY;
      left = Math.max(0, Math.min(mmW, left));
      top = Math.max(0, Math.min(mmH, top));
      right = Math.max(0, Math.min(mmW, right));
      bottom = Math.max(0, Math.min(mmH, bottom));
      minimapExtent.style.left = left + "px";
      minimapExtent.style.top = top + "px";
      minimapExtent.style.width = Math.max(0, right - left) + "px";
      minimapExtent.style.height = Math.max(0, bottom - top) + "px";
    }

    // Fixed reticle: a constant-size box centred on the current view centre.
    if (minimapReticle) {
      const side = Math.max(10, MM_RETICLE_FRAC * Math.min(mmW, mmH));
      const cx = ((fr.width / 2 - t.x) / t.k) * mmScale + mmOffsetX;
      const cy = ((fr.height / 2 - t.y) / t.k) * mmScale + mmOffsetY;
      const left = Math.max(0, Math.min(mmW - side, cx - side / 2));
      const top = Math.max(0, Math.min(mmH - side, cy - side / 2));
      minimapReticle.style.left = left + "px";
      minimapReticle.style.top = top + "px";
      minimapReticle.style.width = side + "px";
      minimapReticle.style.height = side + "px";
    }
  }

  // Aim: recenter on the graph point under (clientX, clientY) and zoom IN to a
  // readable level. "Only zoom in" — if the view is already closer than
  // MM_TARGET_ZOOM, keep that zoom and just pan; never zoom out. Animate a click
  // (the dive); apply drag moves immediately (live scrub at the dived-in zoom).
  function minimapRelocate(clientX, clientY, animate) {
    if (!mmScale) return;
    const rect = minimapCanvas.getBoundingClientRect();
    const gx = (clientX - rect.left - mmOffsetX) / mmScale;
    const gy = (clientY - rect.top - mmOffsetY) / mmScale;
    const k = Math.min(ZOOM_MAX, Math.max(currentScale(), MM_TARGET_ZOOM));
    const fr = frame.getBoundingClientRect();
    const tx = fr.width / 2 - gx * k;
    const ty = fr.height / 2 - gy * k;
    const tform = d3.zoomIdentity.translate(tx, ty).scale(k);
    if (animate) {
      frameSel.transition().duration(220).call(zoom.transform, tform);
    } else {
      frameSel.call(zoom.transform, tform);
    }
  }

  function setMinimapCollapsed(collapsed) {
    if (!minimapEl) return;
    if (collapsed) minimapEl.setAttribute("data-collapsed", "");
    else minimapEl.removeAttribute("data-collapsed");
    if (minimapToggle) {
      minimapToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
      minimapToggle.setAttribute(
        "title",
        (collapsed ? "Show" : "Hide") + " Mini Map (shortcut: m)",
      );
    }
    if (!collapsed) {
      // The body was display:none while collapsed, so the canvas had no size
      // and nothing was drawn; size + redraw from the last layout now visible.
      sizeMinimap();
      renderMinimapFromLast();
      updateMinimapIndicators();
    }
  }

  function toggleMinimap() {
    if (!minimapEl) return;
    const next = !minimapEl.hasAttribute("data-collapsed");
    setMinimapCollapsed(next);
    try {
      localStorage.setItem(MINIMAP_KEY, next ? "1" : "0");
    } catch (_e) {
      /* storage unavailable — toggle still works for this session */
    }
  }

  if (minimapEl && minimapBody && minimapCanvas) {
    // Keep every zoom/pan gesture that starts on the minimap contained to it:
    // d3-zoom lives on #graph-frame (an ancestor), so stopping these events from
    // bubbling means scrolling or pinching over the minimap never zooms the main
    // graph and a minimap drag never starts a frame pan. d3-zoom binds mouse and
    // touch (not pointer) events, so mousedown/touch* must be stopped too. The
    // minimap's own click/drag-to-aim uses pointer events on .minimap-body and is
    // unaffected. wheel + Safari gesture events are also preventDefault'd (the
    // panel has no scroll of its own); touch events are only stopped, not
    // prevented, so they still synthesize the pointer events the aim handler needs.
    ["mousedown", "pointerdown", "dblclick", "touchstart", "touchmove", "touchend"].forEach(
      (type) => minimapEl.addEventListener(type, (e) => e.stopPropagation()),
    );
    ["wheel", "gesturestart", "gesturechange", "gestureend"].forEach((type) =>
      minimapEl.addEventListener(
        type,
        (e) => {
          e.stopPropagation();
          e.preventDefault();
        },
        { passive: false },
      ),
    );

    let mmDragging = false;
    minimapBody.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      mmDragging = true;
      try {
        minimapBody.setPointerCapture(e.pointerId);
      } catch (_e) {
        /* setPointerCapture unsupported — fall back to plain move tracking */
      }
      minimapRelocate(e.clientX, e.clientY, true); // animated dive on click
    });
    minimapBody.addEventListener("pointermove", (e) => {
      if (mmDragging) minimapRelocate(e.clientX, e.clientY, false); // live scrub
    });
    const endDrag = (e) => {
      if (!mmDragging) return;
      mmDragging = false;
      try {
        minimapBody.releasePointerCapture(e.pointerId);
      } catch (_e) {
        /* nothing captured */
      }
    };
    minimapBody.addEventListener("pointerup", endDrag);
    minimapBody.addEventListener("pointercancel", endDrag);

    if (minimapToggle) minimapToggle.addEventListener("click", toggleMinimap);

    // Resize: re-size the panel to the window, then redraw rects + indicators
    // (the graph transform is unchanged, but the panel and visible region change).
    window.addEventListener("resize", () => {
      sizeMinimap();
      renderMinimapFromLast();
      updateMinimapIndicators();
    });

    // Size the panel before the first layout pass, then restore the saved
    // collapsed preference.
    sizeMinimap();
    try {
      if (localStorage.getItem(MINIMAP_KEY) === "1") setMinimapCollapsed(true);
    } catch (_e) {
      /* storage unavailable — default to shown */
    }
  }

  // ----- call-tree sidebar (spec §10.2 "Flow call-tree sidebar") ----------
  //
  // A structural navigator: an indented outline of the OPENED nodes in tree
  // order. One node is "active" (the last opened, or the last sidebar row
  // clicked) and highlighted; beneath it its not-yet-opened direct children
  // render as openable rows that expand in the graph via the same path a
  // call-site click runs. The filter is scoped to the sidebar's own rows —
  // opened nodes + the active node's openable children — and never reaches the
  // project or other entry points (P1). It reflects the rendered graph only and
  // is rebuilt at the end of every relayout (one hook, like the minimap).
  let activeId = flow.root.__id;
  let activePath = new Set(); // ids on the path root → activeId (the active chain)
  const SIDEBAR_KEY = "solflow:calltree-collapsed"; // global UI preference

  // Split a node's label into a muted "Contract." prefix and the function name.
  function ctLabelParts(node) {
    if (node.node_type === "function") {
      const declarerQualified =
        node.call_kind === "library" || node.call_kind === "external";
      const contract = declarerQualified
        ? node.declarer_contract_name
        : node.invoked_via_contract_name;
      return { prefix: contract + ".", name: shortenSignature(node.full_name) };
    }
    if (node.node_type === "external") {
      return {
        prefix: node.target_contract_name ? node.target_contract_name + "." : "",
        name: node.target_function_name + "(...)",
      };
    }
    // unresolved: split the descriptor at the contract/method boundary if any.
    const d = node.descriptor || "unresolved";
    const head = d.indexOf("(") === -1 ? d : d.slice(0, d.indexOf("("));
    const dot = head.lastIndexOf(".");
    if (dot > 0) return { prefix: d.slice(0, dot + 1), name: d.slice(dot + 1) };
    return { prefix: "", name: d };
  }

  function computeActivePath() {
    // activeId plus all its ancestors up to the root.
    const s = new Set();
    let w = activeId;
    while (w !== null && w !== undefined) {
      s.add(w);
      w = parentIdOf(w);
    }
    return s;
  }

  function ctSearchText(node) {
    // Match surface (spec §10.2): function name, contract name, full signature.
    const parts = [];
    if (node.node_type === "function") {
      parts.push(node.name, node.full_name, node.canonical_name,
        node.invoked_via_contract_name, node.declarer_contract_name);
    } else if (node.node_type === "external") {
      parts.push(node.target_function_name, node.target_contract_name,
        node.target_canonical_name);
    } else {
      parts.push(node.descriptor);
    }
    return parts.filter(Boolean).join(" ").toLowerCase();
  }

  function ctTypeClass(node) {
    if (node.node_type === "unresolved") return "calltree-row--unresolved";
    if (node.node_type === "external") return "calltree-row--external";
    if (isModifierNode(node)) return "calltree-row--modifier";
    return "calltree-row--function";
  }

  function ctMakeRow(id, node, depth, openable) {
    const row = el("button", "calltree-row " + ctTypeClass(node));
    row.type = "button";
    row.setAttribute("data-id", id);
    row.setAttribute("data-search", ctSearchText(node));
    if (openable) row.classList.add("calltree-row--open");
    else {
      // Active-path rail + tint, and the selected (viewport-centre) node.
      if (activePath.has(id)) row.classList.add("is-on-path");
      if (id === activeId) row.classList.add("is-selected");
    }
    // Far-left rail (x=0, before the indent), then the disclosure chevron (⌄ open
    // / › collapsed) in a fixed gutter indented per depth, then the label.
    row.appendChild(el("span", "calltree-rail"));
    // Disclosure chevron only for nodes that have children; a childless (leaf)
    // node keeps an empty gutter (nothing to disclose) for alignment.
    const hasChildren =
      node.node_type === "function" && (node.children || []).length > 0;
    const glyph = hasChildren ? (openable ? "›" : "⌄") : "";
    const mark = el("span", "calltree-mark", glyph);
    mark.style.marginLeft = 6 + depth * 16 + "px";
    row.appendChild(mark);
    const parts = ctLabelParts(node);
    const label = el("span", "calltree-label");
    if (parts.prefix) label.appendChild(el("span", "ct-prefix", parts.prefix));
    label.appendChild(el("span", "ct-name", parts.name));
    row.appendChild(label);
    return row;
  }

  function ctRenderInto(frag, id, depth) {
    // Render opened node `id`, then ALL its direct children in source order:
    // opened ones nested, not-yet-opened (non-modifier) ones as openable rows.
    // Showing every opened node's children means opening one child never hides
    // its siblings.
    const node = nodesById.get(id);
    if (!node) return;
    frag.appendChild(ctMakeRow(id, node, depth, false));
    if (node.node_type !== "function") return;
    (node.children || []).forEach((c) => {
      if (visibleIds.has(c.__id)) {
        ctRenderInto(frag, c.__id, depth + 1);
      } else if (!isModifierNode(c)) {
        frag.appendChild(ctMakeRow(c.__id, c, depth + 1, true));
      }
    });
  }

  // v0.23.0 (§10.2): mirror the call-tree's active selection onto the graph as a
  // subtle blue ring. The root keeps its warm entry ring, so the active ring is
  // applied only to a non-root active node. Iterates the visible nodes — cheap
  // for the bounded count a Flow renders.
  function applyGraphActiveRing() {
    const want = activeId !== flow.root.__id ? activeId : null;
    domById.forEach((dom, id) => {
      dom.classList.toggle("node--active", id === want);
    });
  }

  function buildCallTree() {
    if (!calltreeList) return;
    // If the active node was collapsed away, fall back to its nearest visible
    // ancestor (or the root).
    if (!visibleIds.has(activeId)) {
      let walker = activeId;
      while (walker && !visibleIds.has(walker)) walker = parentIdOf(walker);
      activeId = walker || flow.root.__id;
    }
    activePath = computeActivePath();
    applyGraphActiveRing();
    const frag = document.createDocumentFragment();
    ctRenderInto(frag, flow.root.__id, 0);
    calltreeList.replaceChildren(frag);
    applyCallTreeFilter();
  }

  // The visible node whose rendered centre is nearest the viewport centre.
  function centeredNodeId() {
    if (!ctNodeBoxes.size) return null;
    const t = d3.zoomTransform(frame);
    const fr = frame.getBoundingClientRect();
    const cx = (fr.width / 2 - t.x) / t.k;
    const cy = (fr.height / 2 - t.y) / t.k;
    let best = null;
    let bestD = Infinity;
    ctNodeBoxes.forEach((b, id) => {
      const d = (b.cx - cx) * (b.cx - cx) + (b.cy - cy) * (b.cy - cy);
      if (d < bestD) {
        bestD = d;
        best = id;
      }
    });
    return best;
  }

  // v0.22.0: the selected node is the one nearest the viewport centre (a scroll-
  // spy) — not the last opened node. On change, re-light the whole active path.
  function updateActiveFromViewport() {
    const id = centeredNodeId();
    if (!id || id === activeId) return;
    activeId = id;
    applyGraphActiveRing();
    highlightActivePath();
  }

  // Re-apply is-on-path / is-selected to every row from the current activeId,
  // without rebuilding the tree, and scroll the selected row into view.
  function highlightActivePath() {
    if (!calltreeList) return;
    activePath = computeActivePath();
    let sel = null;
    calltreeList.querySelectorAll(".calltree-row").forEach((row) => {
      if (row.classList.contains("calltree-row--open")) {
        row.classList.remove("is-on-path", "is-selected");
        return;
      }
      const rid = row.getAttribute("data-id");
      const onPath = activePath.has(rid);
      row.classList.toggle("is-on-path", onPath);
      const isSel = onPath && rid === activeId;
      row.classList.toggle("is-selected", isSel);
      if (isSel) sel = row;
    });
    if (sel) sel.scrollIntoView({ block: "nearest" });
  }

  function applyCallTreeFilter() {
    if (!calltreeList) return;
    const q = (calltreeFilter ? calltreeFilter.value : "").trim().toLowerCase();
    let shown = 0;
    let total = 0;
    calltreeList.querySelectorAll(".calltree-row").forEach((row) => {
      total += 1;
      const match = !q || (row.getAttribute("data-search") || "").indexOf(q) !== -1;
      row.hidden = !match;
      if (match) shown += 1;
    });
    if (calltreeFilterCount) {
      calltreeFilterCount.textContent = q ? shown + " of " + total : "";
    }
  }

  // Pan (and zoom in if needed) the canvas so node `id` is centred — the same
  // magnify-only-in move the minimap aim uses (spec §10.2 "Flow minimap"). Reads
  // the node's live DOM position (graph coords), which is correct immediately
  // after a relayout for a freshly-opened node, unlike the cached node boxes.
  function centerNodeInView(id) {
    const dom = domById.get(id);
    if (!dom) return;
    // Read the node's real on-screen rect (forces a synchronous reflow, so the
    // position is final even right after a relayout) and invert the current
    // transform to recover its graph coordinates — robust whether or not the
    // freshly-opened node's style.left/top has flushed yet.
    const t = d3.zoomTransform(frame);
    const fr = frame.getBoundingClientRect();
    const b = dom.getBoundingClientRect();
    const cx = (b.left + b.width / 2 - fr.left - t.x) / t.k;
    const cy = (b.top + b.height / 2 - fr.top - t.y) / t.k;
    const k = Math.min(ZOOM_MAX, Math.max(t.k, MM_TARGET_ZOOM));
    const tx = fr.width / 2 - cx * k;
    const ty = fr.height / 2 - cy * k;
    // interrupt() cancels any in-flight zoom transition (e.g. a just-fired
    // pan-into-view) so the centre move wins.
    frameSel
      .interrupt()
      .transition()
      .duration(220)
      .call(zoom.transform, d3.zoomIdentity.translate(tx, ty).scale(k));
  }

  function ctOpen(id, center) {
    // Open a not-yet-visible child in the graph, exactly as a call-site click
    // does (spec §10.2 items 2–5). `center` brings it to the viewport centre (a
    // row click); otherwise it is just panned into view (a chevron click).
    if (visibleIds.has(id)) {
      if (center) centerNodeInView(id);
      return;
    }
    const snapshot = snapshotPositions();
    expandImplicit(id);
    refreshExpandedLineState();
    layoutBox = relayout(snapshot); // relayout's tail rebuilds the sidebar
    if (center) centerNodeInView(id);
    else panNewNodesIntoView([id]);
    persistExpansion();
  }

  function ctCollapse(id) {
    // Clicking an opened row collapses it — a toggle mirroring the call-site
    // click. The root can't be removed, so it collapses its visible (non-
    // modifier) children instead. Collapse never pans (spec §10.2 item 2).
    if (!visibleIds.has(id)) return;
    const snapshot = snapshotPositions();
    if (id === flow.root.__id) {
      (nodesById.get(id).children || []).forEach((c) => {
        if (visibleIds.has(c.__id) && !isModifierNode(c)) collapse(c.__id);
      });
    } else {
      collapse(id);
    }
    refreshExpandedLineState();
    layoutBox = relayout(snapshot); // relayout's tail rebuilds the sidebar
    persistExpansion();
  }

  if (calltreeList) {
    calltreeList.addEventListener("click", (e) => {
      const row = e.target.closest(".calltree-row");
      if (!row) return;
      const id = row.getAttribute("data-id");
      if (!id) return;
      const openable = row.classList.contains("calltree-row--open");
      // The chevron toggles open/collapse (only when it carries a glyph — leaves
      // have none); a click anywhere else on the row centres the node in the
      // canvas (opening it first if it is not yet materialised).
      const mark = row.querySelector(".calltree-mark");
      const onChevron = !!mark && mark.contains(e.target) && mark.textContent !== "";
      if (onChevron) {
        if (openable) ctOpen(id);
        else ctCollapse(id);
      } else if (openable) {
        ctOpen(id, true);
      } else {
        centerNodeInView(id);
      }
    });
  }
  if (calltreeFilter) {
    calltreeFilter.addEventListener("input", applyCallTreeFilter);
    calltreeFilter.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      if (calltreeFilter.value) {
        calltreeFilter.value = "";
        applyCallTreeFilter();
      } else {
        calltreeFilter.blur();
      }
    });
  }
  if (calltreeFilterClear && calltreeFilter) {
    calltreeFilterClear.addEventListener("click", () => {
      calltreeFilter.value = "";
      applyCallTreeFilter();
      calltreeFilter.focus();
    });
  }

  const MAXIMIZED_KEY = "solflow:flow-maximized";

  // Show the right floating control for the current state: reopen-sidebar when
  // the sidebar is collapsed (and not maximized), exit-fullscreen when maximized.
  function updateFloatTools() {
    const maxed = document.body.classList.contains("flow-maximized");
    const collapsed = !!(sidebarEl && sidebarEl.hasAttribute("data-collapsed"));
    if (calltreeReopen) calltreeReopen.hidden = maxed || !collapsed;
    if (fullscreenRestore) fullscreenRestore.hidden = !maxed;
  }

  function setSidebarCollapsed(collapsed) {
    if (!sidebarEl) return;
    if (collapsed) sidebarEl.setAttribute("data-collapsed", "");
    else sidebarEl.removeAttribute("data-collapsed");
    if (calltreeCollapse) {
      calltreeCollapse.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
    updateFloatTools();
    // The graph frame width changed — refresh the minimap (it reads frame size).
    sizeMinimap();
    renderMinimapFromLast();
    updateMinimapIndicators();
  }

  function toggleSidebar() {
    if (!sidebarEl) return;
    const next = !sidebarEl.hasAttribute("data-collapsed");
    setSidebarCollapsed(next);
    try {
      localStorage.setItem(SIDEBAR_KEY, next ? "1" : "0");
    } catch (_e) {
      /* storage unavailable — toggle still works for this session */
    }
  }

  if (calltreeCollapse) calltreeCollapse.addEventListener("click", toggleSidebar);
  if (calltreeReopen) calltreeReopen.addEventListener("click", toggleSidebar);

  // v0.22.0: fullscreen — a focus mode that hides all chrome (header bars + the
  // sidebar) via a class on <body>, keeping the minimap. Because the toolbar's
  // fullscreen button is hidden while maximized, #fullscreen-restore exits.
  function setMaximized(on) {
    document.body.classList.toggle("flow-maximized", on);
    if (maximizeToggle) {
      maximizeToggle.setAttribute("aria-pressed", on ? "true" : "false");
    }
    updateFloatTools();
    // The frame width changes either way — keep the (still-visible) minimap synced.
    sizeMinimap();
    renderMinimapFromLast();
    updateMinimapIndicators();
    try {
      localStorage.setItem(MAXIMIZED_KEY, on ? "1" : "0");
    } catch (_e) {
      /* storage unavailable — focus mode still works for this session */
    }
  }
  function toggleMaximized() {
    setMaximized(!document.body.classList.contains("flow-maximized"));
  }
  if (maximizeToggle) maximizeToggle.addEventListener("click", toggleMaximized);
  if (fullscreenRestore) fullscreenRestore.addEventListener("click", toggleMaximized);
  try {
    if (localStorage.getItem(MAXIMIZED_KEY) === "1") setMaximized(true);
  } catch (_e) {
    /* storage unavailable — default to not maximized */
  }

  // Resizable width (spec §10.2): restore the saved width, then wire the handle.
  const CALLTREE_WIDTH_KEY = "solflow:calltree-width";
  const CT_MIN_W = 220;
  const CT_MAX_W = 560;
  try {
    const w = parseInt(localStorage.getItem(CALLTREE_WIDTH_KEY), 10);
    if (w && sidebarEl) {
      sidebarEl.style.width = Math.max(CT_MIN_W, Math.min(CT_MAX_W, w)) + "px";
    }
  } catch (_e) {
    /* storage unavailable — keep the default width */
  }
  if (calltreeResize && sidebarEl) {
    let rzDragging = false;
    let rzStartX = 0;
    let rzStartW = 0;
    calltreeResize.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      rzDragging = true;
      rzStartX = e.clientX;
      rzStartW = sidebarEl.getBoundingClientRect().width;
      try {
        calltreeResize.setPointerCapture(e.pointerId);
      } catch (_e) {
        /* unsupported — fall back to plain move tracking */
      }
    });
    calltreeResize.addEventListener("pointermove", (e) => {
      if (!rzDragging) return;
      const w = Math.max(CT_MIN_W, Math.min(CT_MAX_W, rzStartW + (e.clientX - rzStartX)));
      sidebarEl.style.width = w + "px";
      // The graph frame width changed — keep the minimap in sync.
      sizeMinimap();
      renderMinimapFromLast();
      updateMinimapIndicators();
    });
    const rzEnd = (e) => {
      if (!rzDragging) return;
      rzDragging = false;
      try {
        calltreeResize.releasePointerCapture(e.pointerId);
      } catch (_e) {
        /* nothing captured */
      }
      try {
        localStorage.setItem(
          CALLTREE_WIDTH_KEY,
          String(Math.round(sidebarEl.getBoundingClientRect().width)),
        );
      } catch (_e) {
        /* storage unavailable — width just won't persist */
      }
    };
    calltreeResize.addEventListener("pointerup", rzEnd);
    calltreeResize.addEventListener("pointercancel", rzEnd);
  }

  // The sidebar is open by default (no data-collapsed in the markup, so it stays
  // open with JS off); collapse it only if the auditor collapsed it on a prior
  // visit. Done before the first layout pass so the initial fit is correct, and
  // updateFloatTools() syncs the reopen button to whatever state we land in.
  try {
    if (localStorage.getItem(SIDEBAR_KEY) === "1") setSidebarCollapsed(true);
  } catch (_e) {
    /* storage unavailable — stay open */
  }
  updateFloatTools();

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
    const snapshot = snapshotPositions();
    expandAllRecursive(flow.root.__id);
    // Open every type-definition dropdown too (spec §10.2) — BEFORE measuring so
    // dagre lays out against the expanded node heights.
    setAllTypeDefs(true);
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
    // Close every type-definition dropdown too (spec §10.2), before relayout so
    // dagre measures the collapsed node heights.
    setAllTypeDefs(false);
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
    const isMinimap = k === "m" || k === "M"; // v0.21.0: toggle the minimap
    const isSidebar = k === "t" || k === "T"; // v0.22.0: toggle the call-tree sidebar
    const isFocusFilter = k === "/"; // v0.22.0: focus the call-tree filter
    const isEsc = k === "Escape"; // v0.22.0: exit fullscreen
    if (!isFit && !isExpand && !isCollapse && !isMinimap && !isSidebar && !isFocusFilter && !isEsc) return;
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (isEsc) {
      if (document.body.classList.contains("flow-maximized")) setMaximized(false);
      return;
    }
    if (isFocusFilter) {
      e.preventDefault();
      if (sidebarEl && sidebarEl.hasAttribute("data-collapsed")) toggleSidebar();
      if (calltreeFilter) calltreeFilter.focus();
      return;
    }
    if (isSidebar) toggleSidebar();
    else if (isMinimap) toggleMinimap();
    else if (isExpand) expandAll();
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

  // v0.15.0: toggle the entry's pin in place rather than reloading the flow
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
          const label = on ? "Unpin" : "Pin this entry point";
          bookmarkToggle.setAttribute("title", label);
          bookmarkToggle.setAttribute("aria-label", label);
        })
        .catch(() => {
          window.location.href = bookmarkToggle.href;
        });
    });
  }
})();
