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
})();
