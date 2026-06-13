"""Regenerate only the terminal intro frames (01-03).

Light theme (per feedback): the terminal echoes the tool's light palette
(cream surface, dark ink, blue/green accents) so the intro flows seamlessly
into the light index, and the loop-back from the dark finale reads as a clear
restart. Single-source-line content + white-space:pre keeps the prompt on one
line.
"""

from __future__ import annotations

import pathlib

from playwright.sync_api import sync_playwright

OUT = pathlib.Path("/tmp/solflow_gif/frames")
VIEW = {"width": 1748, "height": 1116}
DSF = 2

PROMPT = (
    '<span class="prompt">norah@mac</span> '
    '<span class="path">~/work</span> <span class="sig">%</span>'
)


def html(command: str, *, cursor: bool, banner: bool) -> str:
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


def main() -> None:
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_context(viewport=VIEW, device_scale_factor=DSF).new_page()
        for name, cmd, cur, ban in [
            ("01_term_typing.png", "solflow ../test-repos/mor", True, False),
            ("02_term_full.png", "solflow ../test-repos/morpho-blue", True, False),
            ("03_term_run.png", "solflow ../test-repos/morpho-blue", False, True),
        ]:
            page.set_content(html(cmd, cursor=cur, banner=ban))
            page.screenshot(path=str(OUT / name))
            print("saved", name)
        b.close()
    print("DONE")


if __name__ == "__main__":
    main()
