// Progressive enhancement for the index page (spec §8.3).
//
// The bookmark ribbon toggles are real server-side links to /bookmark/<kind>/<id>
// and work with no JavaScript (the no-JS path reloads the page and returns to the
// clicked row). This script intercepts the click so the toggle happens IN PLACE:
// persist via fetch, refresh the Bookmarked section / shortcut / icon states from
// the server's own freshly-rendered HTML, and compensate scroll so the page stays
// exactly where the auditor was. The server remains the single source of truth —
// nothing about a bookmark's identity or rendering is reconstructed client-side.
(function () {
  "use strict";

  // Sync every toggle's on/off state from the freshly-fetched document. Only the
  // clicked id actually changed, but syncing all is cheap and keeps row toggles
  // and their Bookmarked-section counterparts consistent. Matched by href, which
  // is stable across state (the href encodes the id, not the on/off flag).
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
      a.setAttribute("title", on ? "Remove bookmark" : "Bookmark");
      a.setAttribute("aria-label", on ? "Remove bookmark" : "Bookmark");
    });
  }

  // Replace an existing node with the fresh one, insert it when it newly appears,
  // or remove it when it is gone — driven entirely by the server's render.
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

    // Anchor on a stable main-list element so we can hold the viewport fixed even
    // when the (inline) Bookmarked section above changes height. If the click came
    // from within the Bookmarked section itself, fall back to the first group.
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

  // ----- v0.16.0 index filter (spec §8.3) --------------------------------
  //
  // Narrow the visible entry points as the auditor types — a case-insensitive
  // substring match against each entry's function signature and its contract
  // name. The filter box is server-rendered but `hidden`; this script reveals it,
  // so with JavaScript disabled the box never appears and the full, server-
  // rendered listing stays usable. Scoped to `.index-group`, so the pinned
  // Bookmarked section above is left unfiltered.
  function applyFilter(query) {
    var q = query.trim().toLowerCase();
    var shown = 0;
    document.querySelectorAll(".index-group .entry-row").forEach(function (row) {
      var match = !q;
      if (q) {
        var code = row.querySelector(".entry-link code.src");
        var sig = (code ? code.textContent : "").toLowerCase();
        var block = row.closest(".contract-block");
        var name = (block ? block.getAttribute("data-name") || "" : "").toLowerCase();
        match = sig.indexOf(q) !== -1 || name.indexOf(q) !== -1;
      }
      // Toggle INLINE display, not the `hidden` attribute: .entry-row carries
      // `display: flex`, an author rule that overrides the UA `[hidden] {
      // display: none }` rule, so a hidden attribute would not actually hide the
      // row (it stays flex). An inline style wins over the stylesheet.
      row.style.display = match ? "" : "none";
      if (match) shown += 1;
    });
    // A section / contract / group is visible iff it still holds a visible row;
    // an empty query restores everything (display: "" reverts to the stylesheet).
    function hasVisibleRow(container) {
      var rows = container.querySelectorAll(".entry-row");
      for (var i = 0; i < rows.length; i += 1) {
        if (rows[i].style.display !== "none") return true;
      }
      return false;
    }
    document.querySelectorAll(".index-group .entry-section").forEach(function (sec) {
      sec.style.display = !q || hasVisibleRow(sec) ? "" : "none";
    });
    document.querySelectorAll(".index-group .contract-block").forEach(function (block) {
      block.style.display = !q || hasVisibleRow(block) ? "" : "none";
    });
    document.querySelectorAll(".index-group").forEach(function (group) {
      group.style.display = !q || hasVisibleRow(group) ? "" : "none";
    });
    var count = document.getElementById("entry-filter-count");
    if (count) count.textContent = q ? shown + " shown" : "";
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

  // ----- v0.18.0 index Bindings panel (spec §8.3, §13.2) -----------------
  //
  // Each interface row is a real GET form to /bind/ with a "Set" submit, so it
  // works with no JavaScript. This enhances it: auto-apply on `change` (and hide
  // the now-redundant "Set" buttons), and collapse a long list to the first few
  // rows behind a "Show all" toggle. With JavaScript disabled the full list and
  // every "Set" button render server-side, so no binding is unreachable.
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
  initFilter();
  initBindings();
})();
