// Progressive enhancement for the index page (spec §8.3).
//
// The index is fully usable with JavaScript disabled: pin toggles are real
// links, the Bindings panel is real forms, the listing renders server-side in
// the §8.3 order, and the theme control is plain links. This script layers the
// redesign's interactive niceties on top — pin-in-place, the text filter, facet
// pills, client-side sort, per-contract / Reads collapse, the Bindings card
// fold, keyboard navigation, and sidebar scroll-spy — none of which the no-JS
// baseline depends on. The server remains the single source of truth for every
// bookmark/binding/viewed fact; nothing about identity or rendering is
// reconstructed client-side.
(function () {
  "use strict";

  // ----- shared client-side view state -----------------------------------
  // filter substring, the active facet, and the active sort. All three are
  // JS-only; with the script absent the listing stays in its server order.
  var state = { filter: "", facet: "all", sort: "size" };

  // ===== v0.15.0 pin toggle, in place ====================================
  //
  // The pin toggles are real server-side links to /bookmark/<kind>/<id> and
  // work with no JavaScript (the no-JS path reloads and returns to the clicked
  // row). This intercepts the click so the toggle happens IN PLACE: persist via
  // fetch, refresh the Pinned section / shortcut / icon state from the server's
  // own freshly-rendered HTML, and compensate scroll so the page stays put.

  // Sync every toggle's on/off state from the freshly-fetched document. Matched
  // by href, which is stable across state (it encodes the id, not the flag).
  function syncToggleStates(doc) {
    var fresh = {};
    doc.querySelectorAll(".bookmark-toggle").forEach(function (a) {
      fresh[a.getAttribute("href")] = a.classList.contains("is-on");
    });
    document.querySelectorAll(".bookmark-toggle").forEach(function (a) {
      var on = fresh[a.getAttribute("href")];
      if (on === undefined) return;
      a.classList.toggle("is-on", on);
      a.setAttribute("aria-pressed", on ? "true" : "false");
      a.setAttribute("title", on ? "Unpin" : "Pin");
      a.setAttribute("aria-label", on ? "Unpin" : "Pin");
    });
  }

  // Replace an existing node with the fresh one, insert it when it newly
  // appears, or remove it when it is gone — driven by the server's render.
  function reconcile(selector, doc, insertBeforeSelector) {
    var current = document.querySelector(selector);
    var next = doc.querySelector(selector);
    if (current && next) {
      current.replaceWith(document.importNode(next, true));
    } else if (next && !current) {
      var imported = document.importNode(next, true);
      var ref = insertBeforeSelector
        ? document.querySelector(insertBeforeSelector)
        : null;
      if (ref) {
        ref.parentNode.insertBefore(imported, ref);
      } else {
        document.querySelector(".site-main").appendChild(imported);
      }
    } else if (current && !next) {
      current.remove();
    }
  }

  function onClick(e) {
    var toggle = e.target.closest(".bookmark-toggle");
    if (!toggle) return;
    // Let modified clicks (open in new tab, etc.) behave normally.
    if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    if (toggle.dataset.busy) return;
    toggle.dataset.busy = "1";

    // Anchor on a stable main-list element so the viewport stays fixed even when
    // the (inline) Pinned section above changes height. If the click came from
    // within the Pinned section itself, fall back to the first group.
    var anchor = toggle.closest(".entry-row, .contract-block");
    if (!anchor || anchor.closest(".index-bookmarked")) {
      anchor = document.querySelector(".index-group");
    }
    var beforeTop = anchor ? anchor.getBoundingClientRect().top : 0;

    fetch(toggle.href, { credentials: "same-origin" })
      .then(function (r) {
        return r.text();
      })
      .then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        reconcile(".index-bookmarked", doc, ".index-group");
        reconcile(".bookmark-jump", doc, null);
        syncToggleStates(doc);
        // Keep the auditor's view fixed despite any height change above. Force
        // an instant adjustment — scroll-behavior: smooth (set globally for
        // anchor links) would otherwise animate this correction, defeating it.
        if (anchor && anchor.isConnected) {
          var delta = anchor.getBoundingClientRect().top - beforeTop;
          if (delta) {
            window.scrollBy({ top: delta, left: 0, behavior: "instant" });
          }
        }
      })
      .catch(function () {
        // Network or parse failure → fall back to the plain navigation so the
        // click still works (the server link is the no-JS path).
        window.location.href = toggle.href;
      })
      .finally(function () {
        delete toggle.dataset.busy;
      });
  }

  document.addEventListener("click", onClick);

  // ===== filter + facets + sort (the control bar) ========================
  //
  // One unified pass narrows the listing by the active facet AND the filter
  // substring, hiding empty sections / contracts / groups and dimming the
  // matching sidebar navigator rows. The facet pills and sort control are
  // server-rendered but `hidden`; this script reveals and wires them, so with
  // JavaScript disabled they never appear and the full listing stays usable.

  // Build a contract-name → navigator-row map once (scroll-spy + filter dim).
  var navByName = {};

  function buildNavMap() {
    document.querySelectorAll(".nav-row").forEach(function (n) {
      navByName[n.getAttribute("data-nav")] = n;
    });
  }

  function rowMatchesFacet(row) {
    var f = state.facet;
    if (f === "all") return true;
    var kind = row.getAttribute("data-kind");
    if (f === "write") return kind === "write";
    if (f === "read") return kind === "read";
    // Unresolved spans both Writes and Reads.
    if (f === "unresolved") return row.getAttribute("data-unresolved") === "1";
    // No modifiers / With modifiers are mutating-only: a Read is neither.
    if (f === "unguarded") {
      return kind === "write" && row.getAttribute("data-guarded") === "0";
    }
    if (f === "guarded") {
      return kind === "write" && row.getAttribute("data-guarded") === "1";
    }
    return true;
  }

  function rowMatchesFilter(row, q) {
    if (!q) return true;
    var code = row.querySelector(".entry-link code.src");
    var sig = (code ? code.textContent : "").toLowerCase();
    var block = row.closest(".contract-block");
    var name = (block ? block.getAttribute("data-name") || "" : "").toLowerCase();
    return sig.indexOf(q) !== -1 || name.indexOf(q) !== -1;
  }

  function hasVisibleRow(container) {
    var rows = container.querySelectorAll(".entry-row");
    for (var i = 0; i < rows.length; i += 1) {
      if (rows[i].style.display !== "none") return true;
    }
    return false;
  }

  // The single render pass for filter + facet. Toggles INLINE display, not the
  // `hidden` attribute: .entry-row carries `display: flex`, which overrides the
  // UA `[hidden] { display: none }` rule, so a hidden attribute would not hide
  // the row. An inline style wins over the stylesheet.
  function applyView() {
    var q = state.filter.trim().toLowerCase();
    var active = !!q || state.facet !== "all";
    var shown = 0;
    var total = 0;
    document.querySelectorAll(".index-group .entry-row").forEach(function (row) {
      total += 1;
      var match = rowMatchesFacet(row) && rowMatchesFilter(row, q);
      row.style.display = match ? "" : "none";
      if (match) shown += 1;
    });
    // A section / contract / group is visible iff it still holds a visible row;
    // no active narrowing restores everything (display: "" reverts to the sheet).
    document
      .querySelectorAll(".index-group .entry-section")
      .forEach(function (sec) {
        sec.style.display = !active || hasVisibleRow(sec) ? "" : "none";
      });
    document
      .querySelectorAll(".index-group .contract-block")
      .forEach(function (block) {
        var visible = !active || hasVisibleRow(block);
        block.style.display = visible ? "" : "none";
        // Dim the sidebar navigator row for a fully filtered-out contract.
        var nav = navByName[block.getAttribute("data-name")];
        if (nav) nav.classList.toggle("is-dim", active && !visible);
      });
    document.querySelectorAll(".index-group").forEach(function (group) {
      group.style.display = !active || hasVisibleRow(group) ? "" : "none";
    });
    var count = document.getElementById("entry-filter-count");
    if (count) count.textContent = active ? shown + " of " + total : "";
    // The keyboard selection may now point at a hidden row.
    ensureSelectionVisible();
  }

  // Kept as a named entry point (the filter input calls it); delegates to the
  // unified pass so facet + filter compose.
  function applyFilter(query) {
    state.filter = query;
    applyView();
  }

  function initFilter() {
    var box = document.querySelector(".index-filter");
    if (!box) return;
    box.hidden = false; // reveal the control (no-JS users never see it)
    var input = document.getElementById("entry-filter");
    var clear = document.getElementById("entry-filter-clear");
    if (input) {
      input.addEventListener("input", function () {
        applyFilter(input.value);
      });
    }
    if (input && clear) {
      clear.addEventListener("click", function () {
        input.value = "";
        applyFilter("");
        input.focus();
      });
    }
  }

  function initFacets() {
    var bar = document.getElementById("facet-row");
    if (!bar) return;
    bar.hidden = false;
    bar.querySelectorAll(".facet").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var f = btn.getAttribute("data-facet");
        // Clicking the active (non-All) facet returns to All.
        if (f === state.facet && f !== "all") f = "all";
        state.facet = f;
        bar.querySelectorAll(".facet").forEach(function (b) {
          b.classList.toggle(
            "is-active",
            b.getAttribute("data-facet") === state.facet
          );
        });
        applyView();
      });
    });
  }

  // Reorder the listing by the active sort, at BOTH levels (entry points within
  // each Writes/Reads list, AND contracts within each group + their navigator
  // rows), staying within each group divider so a Dependencies contract never
  // sorts up into Project. Three sorts:
  //   Size  = descending total node count incl. subnodes (the server's audit-
  //           weight order, §8.3) — the DEFAULT, so the heaviest call surface
  //           leads; a contract's size is data-nodes, an entry's is its own.
  //   Name  = alphabetical (contract name / signature).
  //   Depth = descending max call depth (a contract's is the max over entries).
  // Name tiebreaks the numeric sorts.
  function numAttr(el, attr) {
    return parseInt(el.getAttribute(attr), 10) || 0;
  }

  // The numeric metric an entry sorts by under the active sort.
  function entryMetric(el) {
    return numAttr(el, state.sort === "size" ? "data-nodes" : "data-depth");
  }

  // A contract's max entry depth (for the Depth sort).
  function contractDepths() {
    var byName = {};
    document.querySelectorAll(".contract-block").forEach(function (block) {
      var d = 0;
      block.querySelectorAll(".entry-row").forEach(function (r) {
        d = Math.max(d, numAttr(r, "data-depth"));
      });
      byName[block.getAttribute("data-name")] = d;
    });
    return byName;
  }

  // Compare a (name, metric) pair: Size/Depth sort descending by metric (name
  // tiebreak); Name sorts alphabetically (the metric is ignored).
  function bySort(na, nb, numA, numB) {
    if (state.sort !== "name" && numB !== numA) return numB - numA;
    var la = (na || "").toLowerCase();
    var lb = (nb || "").toLowerCase();
    return la < lb ? -1 : la > lb ? 1 : 0;
  }

  function applySort() {
    // 1. entry points within each Writes/Reads list.
    document.querySelectorAll(".index-group .entry-list").forEach(function (list) {
      var rows = Array.prototype.slice.call(list.children).filter(function (el) {
        return el.classList && el.classList.contains("entry-row");
      });
      rows.sort(function (a, b) {
        return bySort(
          a.getAttribute("data-fn") || "",
          b.getAttribute("data-fn") || "",
          entryMetric(a),
          entryMetric(b)
        );
      });
      rows.forEach(function (r) {
        list.appendChild(r);
      });
    });

    var depths = contractDepths();
    // A contract's metric: its node count for Size, its max entry depth for
    // Depth (the block carries data-nodes; depth is computed above).
    function contractMetric(name, block) {
      return state.sort === "size" ? numAttr(block, "data-nodes") : depths[name] || 0;
    }

    // 2. contracts within each group (the group heading stays first; sorted
    //    blocks are re-appended after it).
    document.querySelectorAll(".index-group").forEach(function (group) {
      var blocks = Array.prototype.slice.call(
        group.querySelectorAll(".contract-block")
      );
      blocks.sort(function (a, b) {
        var na = a.getAttribute("data-name");
        var nb = b.getAttribute("data-name");
        return bySort(na, nb, contractMetric(na, a), contractMetric(nb, b));
      });
      blocks.forEach(function (b) {
        group.appendChild(b);
      });
    });

    // 3. navigator rows within each group segment (rows are flat siblings under
    //    the .index-nav, interleaved with .index-nav-group labels). Nav rows
    //    carry no node count, so look it up from the matching contract block.
    var nodesByName = {};
    document.querySelectorAll(".contract-block").forEach(function (block) {
      nodesByName[block.getAttribute("data-name")] = numAttr(block, "data-nodes");
    });
    function navMetric(name) {
      return state.sort === "size" ? nodesByName[name] || 0 : depths[name] || 0;
    }
    var nav = document.querySelector(".index-nav");
    if (!nav) return;
    var kids = Array.prototype.slice.call(nav.children);
    var i = 0;
    while (i < kids.length) {
      if (kids[i].classList.contains("index-nav-group")) {
        var label = kids[i];
        var rows = [];
        var j = i + 1;
        while (j < kids.length && kids[j].classList.contains("nav-row")) {
          rows.push(kids[j]);
          j += 1;
        }
        rows.sort(function (a, b) {
          var na = a.getAttribute("data-nav");
          var nb = b.getAttribute("data-nav");
          return bySort(na, nb, navMetric(na), navMetric(nb));
        });
        var anchor = label;
        rows.forEach(function (r) {
          anchor.after(r);
          anchor = r;
        });
        i = j;
      } else {
        i += 1;
      }
    }
  }

  function initSort() {
    var ctrl = document.getElementById("sort-control");
    if (!ctrl) return;
    ctrl.hidden = false;
    // No initial sort: the server already renders in node-count (heaviest call
    // tree first) order (§8.3), which is the default we want, so leave it
    // untouched until the auditor opts into Name or Depth. ``state.sort`` starts
    // as the "size" sentinel meaning exactly that server order — it has no
    // segment button (it is the un-clicked default), so neither pill is active
    // on load. Clicking Name/Depth reorders; reload returns to node-count.
    ctrl.querySelectorAll(".sort-seg").forEach(function (btn) {
      btn.addEventListener("click", function () {
        state.sort = btn.getAttribute("data-sort");
        ctrl.querySelectorAll(".sort-seg").forEach(function (b) {
          b.classList.toggle(
            "is-active",
            b.getAttribute("data-sort") === state.sort
          );
        });
        applySort();
      });
    });
  }

  // ===== per-contract + Reads collapse ===================================
  function initCollapse() {
    document.querySelectorAll("[data-toggle-contract]").forEach(function (h) {
      h.addEventListener("click", function (e) {
        // Let the pin toggle and any link inside the header do their own thing.
        if (e.target.closest(".bookmark-toggle, a")) return;
        var block = h.closest(".contract-block");
        var collapsed = block.toggleAttribute("data-collapsed");
        h.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
      h.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          if (e.target.closest(".bookmark-toggle, a")) return;
          e.preventDefault();
          h.click();
        }
      });
    });
    document.querySelectorAll("[data-toggle-reads]").forEach(function (h) {
      h.addEventListener("click", function () {
        var sec = h.closest(".entry-section");
        var collapsed = sec.toggleAttribute("data-collapsed");
        h.setAttribute("aria-expanded", collapsed ? "false" : "true");
      });
      h.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          h.click();
        }
      });
    });
  }

  // ===== sidebar scroll-spy ==============================================
  function initScrollSpy() {
    var blocks = Array.prototype.slice.call(
      document.querySelectorAll(".contract-block")
    );
    if (!blocks.length || !("IntersectionObserver" in window)) return;
    var onscreen = {};
    var obs = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (en) {
          var name = en.target.getAttribute("data-name");
          if (en.isIntersecting) onscreen[name] = true;
          else delete onscreen[name];
        });
        // Highlight the topmost on-screen contract section.
        var active = null;
        var bestTop = Infinity;
        blocks.forEach(function (b) {
          var name = b.getAttribute("data-name");
          if (!onscreen[name]) return;
          var top = b.getBoundingClientRect().top;
          if (top < bestTop) {
            bestTop = top;
            active = name;
          }
        });
        Object.keys(navByName).forEach(function (name) {
          navByName[name].classList.toggle("is-active", name === active);
        });
      },
      { rootMargin: "0px 0px -70% 0px", threshold: 0 }
    );
    blocks.forEach(function (b) {
      obs.observe(b);
    });
  }

  // ===== keyboard navigation =============================================
  // Ignored while a form control is focused (except `/` and Esc).
  var selected = null;

  function visibleEntryRows() {
    return Array.prototype.slice
      .call(document.querySelectorAll(".index-group .entry-row"))
      .filter(function (r) {
        // offsetParent is null for a display:none row or one inside a collapsed
        // (display:none) contract body / Reads section — exactly what to skip.
        return r.offsetParent !== null;
      });
  }

  function setSelected(row) {
    if (selected) selected.classList.remove("is-selected");
    selected = row || null;
    if (selected) {
      selected.classList.add("is-selected");
      selected.scrollIntoView({ block: "nearest" });
    }
  }

  function moveSelection(delta) {
    var rows = visibleEntryRows();
    if (!rows.length) return;
    var idx = selected ? rows.indexOf(selected) : -1;
    idx += delta;
    if (idx < 0) idx = 0;
    if (idx >= rows.length) idx = rows.length - 1;
    setSelected(rows[idx]);
  }

  function ensureSelectionVisible() {
    if (selected && selected.offsetParent === null) {
      selected.classList.remove("is-selected");
      selected = null;
    }
  }

  function clearFacet() {
    if (state.facet === "all") return false;
    state.facet = "all";
    var bar = document.getElementById("facet-row");
    if (bar) {
      bar.querySelectorAll(".facet").forEach(function (b) {
        b.classList.toggle("is-active", b.getAttribute("data-facet") === "all");
      });
    }
    applyView();
    return true;
  }

  function onKey(e) {
    var ae = document.activeElement;
    var inInput =
      ae &&
      (ae.tagName === "INPUT" ||
        ae.tagName === "TEXTAREA" ||
        ae.tagName === "SELECT");

    if (e.key === "/") {
      if (!inInput) {
        e.preventDefault();
        var inp = document.getElementById("entry-filter");
        if (inp) inp.focus();
      }
      return;
    }
    if (e.key === "Escape") {
      if (inInput) {
        ae.blur();
        return;
      }
      // Clear facet first, then the filter (Esc walks back the narrowing).
      if (clearFacet()) return;
      if (state.filter) {
        var box = document.getElementById("entry-filter");
        if (box) box.value = "";
        applyFilter("");
      }
      return;
    }
    if (inInput) return; // other keys do nothing while typing

    if (e.key === "j" || e.key === "ArrowDown") {
      e.preventDefault();
      moveSelection(1);
    } else if (e.key === "k" || e.key === "ArrowUp") {
      e.preventDefault();
      moveSelection(-1);
    } else if (e.key === "Enter") {
      if (selected) {
        var link = selected.querySelector(".entry-link");
        // A real navigation, so the server records the view (the dimmed marker).
        if (link) window.location.href = link.href;
      }
    } else if (e.key === "b") {
      if (selected) {
        var pin = selected.querySelector(".bookmark-toggle");
        if (pin) pin.click(); // reuses the in-place toggle above
      }
    }
  }

  function initKeys() {
    document.addEventListener("keydown", onKey);
    var hint = document.querySelector(".index-keys");
    if (hint) hint.hidden = false;
  }

  // ===== Bindings card: fold + auto-apply + show-all =====================
  //
  // The fold state persists in localStorage (default open for first-time
  // users); this is purely the panel's open/closed view state — the bindings
  // themselves still persist through the existing Save → solflow.toml flow.
  var BINDINGS_OPEN_KEY = "solflow_bindings_open";

  function setBindingsOpen(card, head, open) {
    if (open) card.setAttribute("data-open", "");
    else card.removeAttribute("data-open");
    if (head) head.setAttribute("aria-expanded", open ? "true" : "false");
    try {
      localStorage.setItem(BINDINGS_OPEN_KEY, open ? "1" : "0");
    } catch (err) {
      /* private mode / disabled storage — the fold just won't persist */
    }
  }

  function initBindingsFold() {
    var card = document.getElementById("bindings");
    if (!card) return;
    var head = card.querySelector(".index-bindings-head");
    var stored = null;
    try {
      stored = localStorage.getItem(BINDINGS_OPEN_KEY);
    } catch (err) {
      stored = null;
    }
    var open = stored === null ? true : stored === "1";
    setBindingsOpen(card, head, open);
    if (head) {
      head.addEventListener("click", function () {
        setBindingsOpen(card, head, !card.hasAttribute("data-open"));
      });
      head.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          head.click();
        }
      });
    }
  }

  // Each interface row is a real GET form to /bind/ with a "Set" submit, so it
  // works with no JavaScript. This enhances it: auto-apply on `change` (and hide
  // the now-redundant "Set" button), and collapse a long list to the first few
  // rows behind a "Show all" toggle.
  var BINDINGS_VISIBLE = 3;

  function initBindings() {
    var list = document.getElementById("bindings-list");
    if (!list) return;

    list.querySelectorAll(".bindings-form").forEach(function (form) {
      var select = form.querySelector(".bindings-select");
      var setBtn = form.querySelector(".bindings-set");
      if (setBtn) setBtn.style.display = "none";
      if (select) {
        select.addEventListener("change", function () {
          form.submit();
        });
      }
    });

    var rows = Array.prototype.slice.call(
      list.querySelectorAll("[data-bind-row]")
    );
    if (rows.length <= BINDINGS_VISIBLE) return;

    var hidden = rows.slice(BINDINGS_VISIBLE);
    var expanded = false;
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "bindings-showall";

    function render() {
      hidden.forEach(function (row) {
        row.style.display = expanded ? "" : "none";
      });
      toggle.textContent = expanded
        ? "Show fewer"
        : "Show all (" + rows.length + ")";
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    }

    toggle.addEventListener("click", function () {
      expanded = !expanded;
      render();
    });
    list.parentNode.insertBefore(toggle, list.nextSibling);
    render();
  }

  // The script is defer-loaded, so the DOM is parsed by the time this runs.
  buildNavMap();
  initFilter();
  initFacets();
  initSort();
  initCollapse();
  initScrollSpy();
  initKeys();
  initBindingsFold();
  initBindings();
})();
