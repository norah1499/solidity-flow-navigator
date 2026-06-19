"""v0.10.0 Stage 1b probe — diagnose --expand-all rendering nothing on large trees.

Symptom: opening a v4-core Flow page with --expand-all causes the graph to
flash and then vanish. Hypothesis: a relayout pass writes non-finite
coordinates (NaN / Infinity) into the dagre nodes or the per-node placement,
which then propagates into the layout bbox; applyFit divides by NaN/0 and
collapses the view.

This probe:
  1. Loads PoolManager.swap (and modifyLiquidity) in headless Chromium with
     the flow page already served by `solflow --expand-all`.
  2. Before the renderer runs, injects a wrapper around `dagre.layout` that
     snapshots every node's (x, y, width, height) BEFORE and AFTER
     dagre.layout returns.
  3. After the page settles, calls into the page to inspect the final state:
     - each visible .node's style.left / style.top
     - the dagre #graph element's width/height
     - the SVG #edges viewBox
     - the recorded dagre snapshots
  4. Reports any non-finite values, and the first pass at which they appear.

Run from the project root with the server already running on :8123:
    .venv/bin/solflow ../test-repos/v4-core --expand-all --port 8123 &
    .venv/bin/python docs/probes/v0_10_0_stage1b_expand_all_probe.py

This file is a development artifact; it is intentionally not part of the
pytest suite. Probes live under docs/probes/ and are
preserved across releases as historical reference.
"""

from __future__ import annotations

import math
import sys
import urllib.parse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8123"
TARGETS = [
    "PoolManager.swap(PoolKey,SwapParams,bytes)",
    "PoolManager.modifyLiquidity(PoolKey,ModifyLiquidityParams,bytes)",
]

# Inject before the page's flow-progressive.js runs. Wraps dagre.layout so we
# can see the (x, y, width, height) of every node immediately after dagre
# finishes its layout pass. Also wraps Element.prototype.getBoundingClientRect
# on .node and .src-line elements so we can spot zero-width / zero-height
# measurements that would feed garbage into the anchor pass.
PROBE_SCRIPT = r"""
window.__probe = {
  dagre_after: null,
  dagre_calls: 0,
  rect_failures: [],
  src_line_offsets: [],
  finished: false,
};

function patch() {
  if (!window.dagre || !dagre.layout || dagre.layout.__patched) return;
  const orig = dagre.layout;
  function patched(g, opts) {
    const ret = orig.call(this, g, opts);
    window.__probe.dagre_calls += 1;
    const snap = [];
    g.nodes().forEach((id) => {
      const n = g.node(id);
      snap.push({
        id,
        x: n.x,
        y: n.y,
        width: n.width,
        height: n.height,
        x_finite: Number.isFinite(n.x),
        y_finite: Number.isFinite(n.y),
      });
    });
    window.__probe.dagre_after = snap;
    return ret;
  }
  patched.__patched = true;
  dagre.layout = patched;
}

const t0 = setInterval(() => {
  patch();
  if (window.dagre && dagre.layout.__patched) clearInterval(t0);
}, 5);
"""


def finite_summary(values):
    bad = [v for v in values if not math.isfinite(v)]
    if not bad:
        finite = [v for v in values if math.isfinite(v)]
        lo = min(finite, default=0)
        hi = max(finite, default=0)
        return f"all finite (n={len(values)}, min={lo:.2f}, max={hi:.2f})"
    return f"NON-FINITE: {len(bad)}/{len(values)} values ({bad[:5]}...)"


def probe_one(page, target: str):
    print(f"\n=== {target} ===")
    quoted = urllib.parse.quote(target, safe="")
    url = f"{BASE}/flow/{quoted}"
    # Inject probe BEFORE any script on the page runs so it patches dagre.layout.
    page.add_init_script(PROBE_SCRIPT)
    page.goto(url, wait_until="load")

    # Give the initial render a moment to settle.
    page.wait_for_function(
        "() => window.__probe && window.__probe.dagre_after !== null",
        timeout=30000,
    )
    # ANIM_MS in the JS is 250 — wait past it so transitions settle.
    page.wait_for_timeout(500)

    diag = page.evaluate(r"""() => {
        const probe = window.__probe;
        const nodes = Array.from(document.querySelectorAll('#nodes .node'));
        const styles = nodes.map((n) => ({
          id: n.getAttribute('data-node-id'),
          left: parseFloat(n.style.left),
          top: parseFloat(n.style.top),
          width: parseFloat(n.style.width),
          opacity: parseFloat(n.style.opacity || '1'),
        }));
        const graph = document.getElementById('graph');
        const edges = document.getElementById('edges');
        const transform = graph ? graph.style.transform : null;
        const frame = document.getElementById('graph-frame');
        const frameRect = frame ? frame.getBoundingClientRect() : null;
        const graphRect = graph ? graph.getBoundingClientRect() : null;
        const docRect = document.documentElement.getBoundingClientRect();
        return {
          node_count: nodes.length,
          styles,
          graph_width: graph && graph.style.width,
          graph_height: graph && graph.style.height,
          edges_viewbox: edges && edges.getAttribute('viewBox'),
          transform,
          frame_rect: frameRect
            ? {w: frameRect.width, h: frameRect.height, x: frameRect.x, y: frameRect.y}
            : null,
          graph_rect: graphRect
            ? {w: graphRect.width, h: graphRect.height, x: graphRect.x, y: graphRect.y}
            : null,
          doc_rect: {w: docRect.width, h: docRect.height},
          viewport: {w: window.innerWidth, h: window.innerHeight},
          dagre_calls: probe.dagre_calls,
          dagre_after: probe.dagre_after,
        };
    }""")

    print(f"  dagre_calls: {diag['dagre_calls']}")
    print(f"  node DOM count: {diag['node_count']}")
    print(f"  graph size: {diag['graph_width']} x {diag['graph_height']}")
    print(f"  edges viewBox: {diag['edges_viewbox']}")
    print(f"  graph transform: {diag['transform']}")
    print(f"  viewport: {diag['viewport']}")
    print(f"  doc rect: {diag['doc_rect']}")
    print(f"  frame rect: {diag['frame_rect']}")
    print(f"  graph rect: {diag['graph_rect']}")

    if diag["dagre_after"]:
        xs = [n["x"] for n in diag["dagre_after"]]
        ys = [n["y"] for n in diag["dagre_after"]]
        ws = [n["width"] for n in diag["dagre_after"]]
        hs = [n["height"] for n in diag["dagre_after"]]
        print(f"  dagre nodes: {len(diag['dagre_after'])}")
        print(f"    x: {finite_summary(xs)}")
        print(f"    y: {finite_summary(ys)}")
        print(f"    width: {finite_summary(ws)}")
        print(f"    height: {finite_summary(hs)}")
        bad_x = [n for n in diag["dagre_after"] if not n["x_finite"]]
        bad_y = [n for n in diag["dagre_after"] if not n["y_finite"]]
        if bad_x or bad_y:
            print("    BAD post-dagre nodes:")
            for n in (bad_x + bad_y)[:10]:
                print(f"      {n}")
    else:
        print("  dagre_after was never populated!")

    # Inspect the FINAL written style.left / style.top — this captures the
    # cumulative effect of the v0.9 reorder + group-delta + manual passes.
    lefts = [s["left"] for s in diag["styles"]]
    tops = [s["top"] for s in diag["styles"]]
    print(f"  style.left: {finite_summary(lefts)}")
    print(f"  style.top:  {finite_summary(tops)}")
    bad_styles = [
        s
        for s in diag["styles"]
        if not (math.isfinite(s["left"]) and math.isfinite(s["top"]))
    ]
    if bad_styles:
        print("  BAD style entries (showing up to 10):")
        for s in bad_styles[:10]:
            print(f"    {s}")
    # If graph size is non-finite (NaN px), applyFit divides by it and produces
    # a NaN transform, which is the symptom the user sees.
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
