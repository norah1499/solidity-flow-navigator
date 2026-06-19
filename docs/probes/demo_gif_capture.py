"""Capture Retina frames for the refreshed docs/demo.gif.

Run with the ARCHIVE venv python (has Playwright + Chromium); writes PNGs to
/tmp/solflow_gif/frames/ (scratch):
  .../solidity-flow-navigator-private-archive/.venv/bin/python docs/probes/demo_gif_capture.py

One server expected up beforehand (project venv):
  S1 = http://127.0.0.1:8137  (normal progressive)

The demo subject is Uniswap V4's PoolManager.swap. Its --expand-all tree is
~9645x133771 px (see flow-progressive.js fit notes), which fits only as an
unreadable sliver, so the "overview" beat shows a deep but bounded expansion
(expand_swap_overview) instead of the whole tree.
"""

from __future__ import annotations

import pathlib

from playwright.sync_api import sync_playwright

OUT = pathlib.Path("/tmp/solflow_gif/frames")
OUT.mkdir(parents=True, exist_ok=True)

S1 = "http://127.0.0.1:8137"
SWAP = "/flow/PoolManager.swap(PoolKey,SwapParams,bytes)"

VIEW = {"width": 1748, "height": 1116}
DSF = 2


PROMPT = (
    '<span class="prompt">norah@mac</span> '
    '<span class="path">~/work</span> <span class="sig">%</span>'
)


def terminal_html(command: str, *, cursor: bool, banner: bool) -> str:
    """A clean macOS-style terminal in the tool's LIGHT palette (cream surface,
    dark ink, blue/green accents), so the intro flows seamlessly into the light
    index and the loop-back from the dark finale reads as a clear restart.
    Single source line + white-space:pre keeps the prompt on one line."""
    cur = '<span class="cur">&#9611;</span>' if cursor else ""
    cmd_line = (
        f'<div class="line">{PROMPT}<span class="cmd"> {command}</span>{cur}</div>'
    )
    banner_html = ""
    if banner:
        banner_html = (
            '<div class="line out">Solidity Flow Navigator running at '
            '<span class="link">http://127.0.0.1:8137</span></div>'
            f'<div class="line">{PROMPT} <span class="cur">&#9611;</span></div>'
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;height:100%;}}
  body{{background:#efeae0;display:flex;align-items:center;justify-content:center;
        font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}}
  .win{{width:1180px;background:#fbfaf6;border-radius:12px;overflow:hidden;
        box-shadow:0 18px 60px rgba(60,52,40,.18);border:1px solid #d8d4c8;}}
  .bar{{height:44px;background:#efeadf;display:flex;align-items:center;gap:9px;
        padding:0 18px;border-bottom:1px solid #e3ddcf;}}
  .dot{{width:13px;height:13px;border-radius:50%;}}
  .r{{background:#ff5f57;}} .y{{background:#febc2e;}} .g{{background:#28c840;}}
  .title{{color:#9a9486;font-size:13px;margin-left:14px;}}
  .body{{padding:30px 30px 36px;font-size:25px;line-height:1.85;color:#2c2c2c;}}
  .line{{white-space:pre;}}
  .prompt{{color:#2f7d5b;}} .path{{color:#2f6db0;}} .sig{{color:#9a9486;}}
  .cmd{{color:#1f1f1f;}} .out{{color:#6b6a64;}}
  .link{{color:#2f6db0;text-decoration:underline;}} .cur{{color:#2c2c2c;}}
</style></head><body>
  <div class="win">
    <div class="bar"><span class="dot r"></span><span class="dot y"></span>
      <span class="dot g"></span><span class="title">solflow &#8212; zsh</span></div>
    <div class="body">{cmd_line}{banner_html}</div>
  </div>
</body></html>"""


def grab(page, name: str) -> None:
    page.screenshot(path=str(OUT / name))
    print("saved", name)


def wait_ready(page) -> None:
    try:
        page.wait_for_selector('#graph[data-state="ready"]', timeout=9000)
    except Exception as e:
        print("  (ready wait skipped:", e, ")")
    page.wait_for_timeout(700)


def expand_swap_overview(page) -> None:
    """Build a bushy, readable bird's-eye of the swap flow, then fit. Collapses
    to the root first (so the result is independent of any persisted state),
    then drills: every root call site, _swap -> Pool.swap, Pool.swap's swap-step
    math, and the beforeSwap hook subtree. The true --expand-all tree is far too
    tall to read when fitted (~9645x133771 px), so this shows a rich slice (~30
    nodes) that still fills the canvas legibly. Clicks are dispatched onto the
    call-line spans (delegated handler), immune to pan/zoom and sidebar overlay."""
    try:
        page.locator("#collapse-all-btn").click(timeout=2000)
        page.wait_for_timeout(600)
    except Exception as e:
        print("  collapse-all skip:", e)
    drill = """(a) => {
        const {marker, limit} = a;
        const ns = [...document.querySelectorAll('#nodes .node')];
        const node = marker === null
            ? ns[0]
            : ns.find(n => n !== ns[0] && (n.textContent || '').includes(marker));
        if (!node) return -1;
        let n = 0;
        for (const l of node.querySelectorAll('.src-line--call')) {
            if (l.classList.contains('src-line--expanded')) continue;
            l.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            n++;
            if (limit && n >= limit) break;
        }
        return n;
    }"""
    steps = [
        ("root", {"marker": None, "limit": 0}),
        ("_swap", {"marker": "pool.swap(", "limit": 0}),
        ("Pool.swap", {"marker": "computeSwapStep", "limit": 6}),
        ("beforeSwap", {"marker": "callHook(", "limit": 4}),
    ]
    for label, arg in steps:
        print("  overview drill", label, "opened:", page.evaluate(drill, arg))
        page.wait_for_timeout(700)
    try:
        page.locator("#reset-view").click(timeout=2500)
        page.wait_for_timeout(1300)
    except Exception as e:
        print("  fit skip:", e)


def expand_swap(page) -> None:
    """Expand the interesting swap subtree, then fit. Clicks are dispatched
    straight onto the call-line spans (the nodes-layer handler is delegated and
    reads data-child-ids), so expansion is immune to pan/zoom hit-testing and
    the call-tree sidebar overlay. Opens the library-mediated beforeSwap call
    (which bottoms out in an unresolved low-level hook call) and the _swap
    helper at the root, then one level deeper into pool.swap."""
    opened = page.evaluate("""() => {
            const root = document.querySelector('#nodes .node');
            if (!root) return 0;
            let n = 0;
            for (const l of root.querySelectorAll('.src-line--call')) {
                const t = l.textContent || '';
                if (t.includes('beforeSwap(') || t.includes('_swap(')) {
                    l.dispatchEvent(new MouseEvent('click', {bubbles: true}));
                    n++;
                }
            }
            return n;
        }""")
    print("  root call lines opened:", opened)
    page.wait_for_timeout(700)
    # one level deeper into Pool.swap. The _swap node is the one whose body
    # calls pool.swap(); the call spans two source lines, so the clickable
    # call site is its first .src-line--call (the assignment line), not the
    # line literally reading "pool.swap(".
    deeper = page.evaluate("""() => {
            const nodes = [...document.querySelectorAll('#nodes .node')];
            const swapNode = nodes.find(n => (n.textContent || '').includes('pool.swap('));
            if (!swapNode) return false;
            const line = swapNode.querySelector('.src-line--call');
            if (!line) return false;
            line.dispatchEvent(new MouseEvent('click', {bubbles: true}));
            return true;
        }""")
    print("  pool.swap deeper opened:", deeper)
    page.wait_for_timeout(700)
    try:
        page.locator("#reset-view").click(timeout=2000)
        page.wait_for_timeout(900)
    except Exception as e:
        print("  reset-view skip:", e)


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()

        # ---- terminal intro frames (light context, HTML rendered) ----
        ctx = browser.new_context(viewport=VIEW, device_scale_factor=DSF)
        page = ctx.new_page()
        page.set_content(
            terminal_html("solflow ../test-repos/v4-", cursor=True, banner=False)
        )
        grab(page, "01_term_typing.png")
        page.set_content(
            terminal_html("solflow ../test-repos/v4-core", cursor=True, banner=False)
        )
        grab(page, "02_term_full.png")
        page.set_content(
            terminal_html("solflow ../test-repos/v4-core", cursor=False, banner=True)
        )
        grab(page, "03_term_run.png")

        # ---- light browser frames (S1 normal) ----
        page.goto(S1 + "/", wait_until="networkidle")
        page.wait_for_timeout(500)
        grab(page, "04_index_light.png")

        page.goto(S1 + SWAP, wait_until="networkidle")
        wait_ready(page)
        grab(page, "05_swap_root_light.png")

        # detail: the root node's source with breathing room around it (a clip
        # padded beyond the node box, so it reads as zoomed-out, not cropped tight)
        try:
            box = page.locator("#nodes .node").first.bounding_box()
            pad_x, pad_y = 170, 120
            cx = max(0, box["x"] - pad_x)
            cy = max(0, box["y"] - pad_y)
            clip = {
                "x": cx,
                "y": cy,
                "width": min(VIEW["width"] - cx, box["width"] + 2 * pad_x),
                "height": min(VIEW["height"] - cy, box["height"] + 2 * pad_y),
            }
            page.screenshot(path=str(OUT / "06_swap_detail_light.png"), clip=clip)
            print("saved 06_swap_detail_light.png")
        except Exception as e:
            print("  detail clip skip:", e)
            grab(page, "06_swap_detail_light.png")

        # expanded (a few calls + one deeper), fit, full-viewport shot
        page.goto(S1 + SWAP, wait_until="networkidle")
        wait_ready(page)
        expand_swap(page)
        grab(page, "07_swap_expanded_light.png")

        # bird's-eye: a deep, bushy slice of the swap tree, fit to frame
        page.goto(S1 + SWAP, wait_until="networkidle")
        wait_ready(page)
        expand_swap_overview(page)
        grab(page, "08_swap_full_light.png")
        ctx.close()

        # ---- dark frames (real solflow_theme=dark cookie) ----
        dctx = browser.new_context(viewport=VIEW, device_scale_factor=DSF)
        dctx.add_cookies(
            [
                {"name": "solflow_theme", "value": "dark", "url": S1 + "/"},
            ]
        )
        dp = dctx.new_page()

        # theme control close-up (dark index header)
        dp.goto(S1 + "/", wait_until="networkidle")
        dp.wait_for_timeout(500)
        try:
            dp.locator(".site-header").screenshot(
                path=str(OUT / "09_theme_control_dark.png")
            )
            print("saved 09_theme_control_dark.png")
        except Exception as e:
            print("  header shot skip:", e)
        grab(dp, "10_index_dark.png")

        dp.goto(S1 + SWAP, wait_until="networkidle")
        wait_ready(dp)
        expand_swap(dp)
        grab(dp, "11_swap_expanded_dark.png")

        dp.goto(S1 + SWAP, wait_until="networkidle")
        wait_ready(dp)
        expand_swap_overview(dp)
        grab(dp, "12_swap_full_dark.png")
        dctx.close()

        browser.close()
    print("DONE")


if __name__ == "__main__":
    main()
