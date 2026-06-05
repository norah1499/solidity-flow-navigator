"""v0.10.0 Stage 1c probe — verify --expand-all balances first-level sides.

Stage 1b made the bulk-expansion graph visible by capping the flex chain and
widening the d3-zoom floor, but the layout itself remained lopsided: every
first-level branch piled on the right because Rule 1's auto-balance reads an
empty lastNodeRects under bulk expansion and ties resolve right. Stage 1c
adds a global balancing pass over MEASURED first-level extents (spec §10.3
"Expand-all balancing"). This probe verifies the split.

What the probe does:
  1. Opens PoolManager.swap and PoolManager.modifyLiquidity in headless
     Chromium with --expand-all already wired by the server.
  2. After the renderer settles, reads each node's final style.left and
     compares it to the root's center; left-of-root style.left counts as
     "left", right-of-root counts as "right". (Sides aren't exposed
     directly to the DOM, but the cumulative effect of the dagre run with
     the rewritten sideById shows up as final X position relative to
     root.)
  3. Reports the first-level count per side. After Stage 1c the two sides
     should be roughly balanced (neither side carrying every branch);
     before Stage 1c every branch was on the right.

Run from the project root with the server already running on :8123:
    .venv/bin/solflow ../test-repos/v4-core --expand-all --port 8123 &
    .venv/bin/python docs/probes/v0_10_0_stage1c_expand_all_side_balance_probe.py

Sibling to docs/probes/v0_10_0_stage1b_expand_all_probe.py (Stage 1b's
finite-coordinate probe). Both stay on disk as historical reference.
"""

from __future__ import annotations

import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8123"
TARGETS = [
    "PoolManager.swap(PoolKey,SwapParams,bytes)",
    "PoolManager.modifyLiquidity(PoolKey,ModifyLiquidityParams,bytes)",
]


def probe_one(page, target: str):
    print(f"\n=== {target} ===")
    quoted = urllib.parse.quote(target, safe="")
    page.goto(f"{BASE}/flow/{quoted}", wait_until="load")
    page.wait_for_function(
        "() => document.querySelectorAll('#nodes .node').length > 1",
        timeout=30000,
    )
    page.wait_for_timeout(500)

    # First-level children of the root carry data-node-id "0/<index>".
    # Compare each first-level child's left+halfWidth against the root
    # node's left+halfWidth — anything strictly less than root's centre
    # is "left", anything strictly greater is "right". The root itself
    # has data-node-id "0". Also bucket the cumulative VERTICAL EXTENT of
    # each first-level subtree (the dagre span the balancing pass
    # operates on), so we can verify extent-balance, not just count-balance.
    diag = page.evaluate(r"""() => {
        function nodeRect(n) {
            const left = parseFloat(n.style.left) || 0;
            const top = parseFloat(n.style.top) || 0;
            const r = n.getBoundingClientRect();
            return {left, top, w: r.width, h: r.height,
                    cx: left + r.width / 2};
        }
        const root = document.querySelector('#nodes .node[data-node-id="0"]');
        if (!root) return {error: "root not found"};
        const rootCx = nodeRect(root).cx;
        const allNodes = Array.from(document.querySelectorAll('#nodes .node'));
        const firstLevel = allNodes.filter((n) => {
            const id = n.getAttribute('data-node-id') || '';
            return /^0\/\d+$/.test(id);
        });
        function subtreeExtent(rootId) {
            // Walk every node whose data-node-id is rootId or starts with rootId/.
            let minTop = Infinity, maxBot = -Infinity;
            const prefix = rootId + '/';
            allNodes.forEach((n) => {
                const id = n.getAttribute('data-node-id') || '';
                if (id !== rootId && !id.startsWith(prefix)) return;
                const r = nodeRect(n);
                if (r.top < minTop) minTop = r.top;
                if (r.top + r.h > maxBot) maxBot = r.top + r.h;
            });
            return minTop === Infinity ? 0 : maxBot - minTop;
        }
        let left = 0, right = 0, atCenter = 0;
        let leftExtent = 0, rightExtent = 0;
        const ids = {left: [], right: []};
        const perBranch = [];
        firstLevel.forEach((n) => {
            const id = n.getAttribute('data-node-id');
            const r = nodeRect(n);
            const ext = subtreeExtent(id);
            perBranch.push({id, ext: Math.round(ext)});
            if (r.cx < rootCx) {
                left += 1; ids.left.push(id); leftExtent += ext;
            } else if (r.cx > rootCx) {
                right += 1; ids.right.push(id); rightExtent += ext;
            } else {
                atCenter += 1;
            }
        });
        perBranch.sort((a, b) => b.ext - a.ext);
        return {
            root_cx: rootCx,
            first_level_total: firstLevel.length,
            left, right, atCenter,
            left_ids: ids.left.slice(0, 12),
            right_ids: ids.right.slice(0, 12),
            left_extent: Math.round(leftExtent),
            right_extent: Math.round(rightExtent),
            per_branch_sorted: perBranch,
        };
    }""")

    if "error" in diag:
        print(f"  ERROR: {diag['error']}")
        return diag
    print(f"  first-level branches: {diag['first_level_total']}")
    print(f"  left:  {diag['left']:3d}  (sample ids: {diag['left_ids']})")
    print(f"  right: {diag['right']:3d}  (sample ids: {diag['right_ids']})")
    if diag["atCenter"]:
        print(f"  atCenter: {diag['atCenter']}  (unexpected — overlapping root?)")
    # Extent is the metric the balancing pass operates on (subtree
    # vertical pixels), not branch count. A single huge subtree placed
    # opposite many small ones is the algorithmically-correct outcome.
    print(f"  left extent (px):  {diag['left_extent']:>8d}")
    print(f"  right extent (px): {diag['right_extent']:>8d}")
    print(
        "  per-branch extents (top 6, sorted desc): "
        + ", ".join(f"{b['id']}={b['ext']}" for b in diag["per_branch_sorted"][:6])
    )
    if diag["first_level_total"] == 0:
        print("  no first-level branches; nothing to balance")
    elif diag["left"] == 0 or diag["right"] == 0:
        print(
            "  ⚠ UNBALANCED — every first-level branch landed on one side. "
            "Stage 1c balancing did not take effect."
        )
    else:
        count_ratio = min(diag["left"], diag["right"]) / max(
            diag["left"], diag["right"]
        )
        ext_min = min(diag["left_extent"], diag["right_extent"])
        ext_max = max(diag["left_extent"], diag["right_extent"])
        ext_ratio = (ext_min / ext_max) if ext_max else 0.0
        print(f"  count ratio (min/max):  {count_ratio:.2f}")
        print(
            f"  extent ratio (min/max): {ext_ratio:.2f}  "
            "(the metric balancing actually optimizes)"
        )
    return diag


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(viewport={"width": 1600, "height": 1000})
        page = ctx.new_page()
        try:
            for t in TARGETS:
                probe_one(page, t)
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main() or 0)
