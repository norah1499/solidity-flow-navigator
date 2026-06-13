"""Capture Retina frames for the refreshed docs/demo.gif.

Run with the ARCHIVE venv python (has Playwright + Chromium); writes PNGs to
/tmp/solflow_gif/frames/ (scratch):
  .../solidity-flow-navigator-private-archive/.venv/bin/python docs/probes/demo_gif_capture.py

Servers expected up beforehand (project venv):
  S1 = http://127.0.0.1:8137  (normal progressive)
  S2 = http://127.0.0.1:8138  (--expand-all)
"""

from __future__ import annotations

import pathlib

from playwright.sync_api import sync_playwright

OUT = pathlib.Path("/tmp/solflow_gif/frames")
OUT.mkdir(parents=True, exist_ok=True)

S1 = "http://127.0.0.1:8137"
S2 = "http://127.0.0.1:8138"
LIQ = "/flow/Morpho.liquidate(MarketParams,address,uint256,uint256,bytes)"

VIEW = {"width": 1748, "height": 1116}
DSF = 2


def terminal_html(command: str, *, cursor: bool, banner: bool) -> str:
    """A polished macOS-style terminal window rendered for screenshotting."""
    cur = '<span class="cur">&#9611;</span>' if cursor else ""
    banner_html = ""
    if banner:
        banner_html = (
            '<div class="line out">Solidity Flow Navigator running at '
            '<span class="link">http://127.0.0.1:8137</span></div>'
            '<div class="line"><span class="prompt">norah@mac</span>'
            '<span class="path">~/work</span><span class="sig">%</span> '
            '<span class="cur">&#9611;</span></div>'
        )
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
  html,body{{margin:0;height:100%;}}
  body{{background:#14161b;display:flex;align-items:center;justify-content:center;
        font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;}}
  .win{{width:1180px;background:#1b1d23;border-radius:12px;overflow:hidden;
        box-shadow:0 24px 80px rgba(0,0,0,.55);border:1px solid #2a2d36;}}
  .bar{{height:44px;background:#23262e;display:flex;align-items:center;gap:9px;
        padding:0 18px;}}
  .dot{{width:13px;height:13px;border-radius:50%;}}
  .r{{background:#ff5f57;}} .y{{background:#febc2e;}} .g{{background:#28c840;}}
  .title{{color:#8b8f9a;font-size:13px;margin-left:14px;}}
  .body{{padding:26px 30px 34px;font-size:25px;line-height:1.7;color:#e6e8ee;}}
  .line{{white-space:pre-wrap;word-break:break-all;}}
  .prompt{{color:#7fd1b9;}} .path{{color:#7fb3eb;margin:0 .5ch;}}
  .sig{{color:#8b8f9a;}}
  .cmd{{color:#f2f4f8;}}
  .out{{color:#9aa0ad;margin-top:6px;}}
  .link{{color:#7fb3eb;text-decoration:underline;}}
  .cur{{color:#e6e8ee;}}
</style></head><body>
  <div class="win">
    <div class="bar"><span class="dot r"></span><span class="dot y"></span>
      <span class="dot g"></span><span class="title">solflow &#8212; zsh</span></div>
    <div class="body">
      <div class="line"><span class="prompt">norah@mac</span>
        <span class="path">~/work</span><span class="sig">%</span>
        <span class="cmd"> {command}</span>{cur}</div>
      {banner_html}
    </div>
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


def expand_liquidate(page) -> None:
    """Click a few root call sites, then one level deeper in accrueInterest."""
    for i in range(3):
        try:
            page.locator("#nodes .node").first.locator(".src-line--call").nth(i).click(
                timeout=2500
            )
            page.wait_for_timeout(550)
        except Exception as e:
            print("  root click", i, "skip:", e)
    try:
        acc = page.locator('.node--function:has-text("accrueInterest")').first
        acc.locator(".src-line--call").first.click(timeout=2500)
        page.wait_for_timeout(550)
    except Exception as e:
        print("  accrueInterest deeper click skip:", e)
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
            terminal_html("solflow ../test-repos/mor", cursor=True, banner=False)
        )
        grab(page, "01_term_typing.png")
        page.set_content(
            terminal_html(
                "solflow ../test-repos/morpho-blue", cursor=True, banner=False
            )
        )
        grab(page, "02_term_full.png")
        page.set_content(
            terminal_html(
                "solflow ../test-repos/morpho-blue", cursor=False, banner=True
            )
        )
        grab(page, "03_term_run.png")

        # ---- light browser frames (S1 normal) ----
        page.goto(S1 + "/", wait_until="networkidle")
        page.wait_for_timeout(500)
        grab(page, "04_index_light.png")

        page.goto(S1 + LIQ, wait_until="networkidle")
        wait_ready(page)
        grab(page, "05_liquidate_root_light.png")

        # detail: tight element shot of the root node's source
        try:
            page.locator("#nodes .node").first.screenshot(
                path=str(OUT / "06_liquidate_detail_light.png")
            )
            print("saved 06_liquidate_detail_light.png")
        except Exception as e:
            print("  detail element shot skip:", e)
            grab(page, "06_liquidate_detail_light.png")

        # expanded (a few calls + one deeper), fit, full-viewport shot
        page.goto(S1 + LIQ, wait_until="networkidle")
        wait_ready(page)
        expand_liquidate(page)
        grab(page, "07_liquidate_expanded_light.png")

        # full tree zoomed out (S2 expand-all)
        page.goto(S2 + LIQ, wait_until="networkidle")
        wait_ready(page)
        grab(page, "08_liquidate_full_light.png")
        ctx.close()

        # ---- dark frames (real solflow_theme=dark cookie) ----
        dctx = browser.new_context(viewport=VIEW, device_scale_factor=DSF)
        dctx.add_cookies(
            [
                {"name": "solflow_theme", "value": "dark", "url": S1 + "/"},
                {"name": "solflow_theme", "value": "dark", "url": S2 + "/"},
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

        dp.goto(S1 + LIQ, wait_until="networkidle")
        wait_ready(dp)
        expand_liquidate(dp)
        grab(dp, "11_liquidate_expanded_dark.png")

        dp.goto(S2 + LIQ, wait_until="networkidle")
        wait_ready(dp)
        grab(dp, "12_liquidate_full_dark.png")
        dctx.close()

        browser.close()
    print("DONE")


if __name__ == "__main__":
    main()
