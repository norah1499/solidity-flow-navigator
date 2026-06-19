"""Build the 1280x640 GitHub social-preview card (docs/social-preview.png).

Light + minimal: the SolFlow wordmark and tagline on clean cream, over a
zoomed-in, faded crop of the syntax-highlighted PoolManager.swap source.
Reproduces the committed docs/social-preview.png. The card is uploaded manually
in the repo Settings -> Social preview; it is not referenced by the README.

Run with the archive venv python (has Pillow), AFTER demo_gif_capture.py has
produced the light frames in /tmp/solflow_gif/frames/; writes
/tmp/solflow_gif/social-preview.png (then copy to docs/social-preview.png):
  .../private-archive/.venv/bin/python docs/probes/social_preview.py
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
FRAMES = pathlib.Path("/tmp/solflow_gif/frames")
OUT = pathlib.Path("/tmp/solflow_gif/social-preview.png")
CREAM = (247, 245, 238)

BOLD = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
]
REG = [
    "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
]
MONO = [
    "/System/Library/Fonts/SFNSMono.ttf",
    "/System/Library/Fonts/Supplemental/Menlo.ttc",
]


def font(paths: list[str], size: int) -> ImageFont.FreeTypeFont:
    for p in paths:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main() -> None:
    src = Image.open(FRAMES / "05_swap_root_light.png").convert("RGB")
    crop = src.crop((620, 230, 2860, 1350)).resize((W, H), Image.LANCZOS)
    card = crop.convert("RGBA")

    # Fade the code to a faint backdrop, then keep a clean cream text zone on
    # the left (opaque to x=540, fading out by x ~ 1100).
    card = Image.alpha_composite(card, Image.new("RGBA", (W, H), (*CREAM, 96)))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for x in range(W):
        a = 252 if x < 540 else int(max(0, 252 * (1 - (x - 540) / 560)))
        od.line([(x, 0), (x, H)], fill=(*CREAM, a))
    card = Image.alpha_composite(card, ov)

    d = ImageDraw.Draw(card)
    x = 84
    # `solFlow` wordmark (v0.23.0): monospace, `sol` in ink (--fg-text #2c2c2c),
    # `Flow` in blue (--node-mod-border #3a6db0) — matches the app header brand.
    mark = font(MONO, 96)
    d.text((x, 188), "sol", font=mark, fill=(44, 44, 44))
    sol_w = d.textlength("sol", font=mark)
    d.text((x + sol_w, 188), "Flow", font=mark, fill=(58, 109, 176))
    word_w = d.textlength("solFlow", font=mark)
    d.rectangle([x + 2, 322, x + 2 + word_w, 328], fill=(58, 109, 176))
    d.text(
        (x, 360),
        "Read a Solidity contract the way it executes.",
        font=font(REG, 35),
        fill=(60, 58, 54),
    )
    d.text(
        (x, 432),
        "Interactive call-graph Flows, one per entry point.",
        font=font(REG, 25),
        fill=(118, 114, 106),
    )
    d.text((x, 520), "pip install solflow", font=font(MONO, 25), fill=(58, 109, 176))

    card.convert("RGB").save(OUT)
    print(f"wrote {OUT} : {W}x{H}")


if __name__ == "__main__":
    main()
