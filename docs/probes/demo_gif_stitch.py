"""Stitch the captured frames into docs/demo.gif (slideshow, hard cuts).

Run with the archive venv python (has Pillow); reads /tmp/solflow_gif/frames/,
writes /tmp/solflow_gif/demo.gif (then copy to docs/demo.gif):
  .../private-archive/.venv/bin/python docs/probes/demo_gif_stitch.py [WIDTH] [XFADE]
"""

from __future__ import annotations

import pathlib
import sys

from PIL import Image

FRAMES = pathlib.Path("/tmp/solflow_gif/frames")
OUT = pathlib.Path("/tmp/solflow_gif/demo.gif")

W = int(sys.argv[1]) if len(sys.argv) > 1 else 1400
H = round(W * 1073 / 1680)
XFADE = int(sys.argv[2]) if len(sys.argv) > 2 else 0

# Source captures are 3496x2232. Toggle sits top-right; this is the
# aspect-correct crop the zoom-to-toggle beat lands on (source px).
ZOOM_TARGET_W = 1300

# (spec, hold_ms). spec: filename | ("ZOOM", filename, t)  with t in [0,1]
SEQ = [
    # terminal intro (slowed down per feedback)
    ("01_term_typing.png", 1000),
    ("02_term_full.png", 1200),
    ("03_term_run.png", 2600),
    # light walkthrough (kept)
    ("04_index_light.png", 2000),
    ("05_swap_root_light.png", 1900),
    ("07_swap_expanded_light.png", 2200),
    ("08_swap_full_light.png", 2300),
    # "select dark mode" beat: full light index -> zoom toward toggle -> Dark
    ("04_index_light.png", 1400),
    (("ZOOM", "04_index_light.png", 0.5), 650),
    (("ZOOM", "04_index_light.png", 1.0), 1300),
    (("ZOOM", "10_index_dark.png", 1.0), 1700),
    # dark trio (kept)
    ("10_index_dark.png", 1900),
    ("11_swap_expanded_dark.png", 2200),
    ("12_swap_full_dark.png", 5000),  # long final hold = clear ending
]


def fullbleed(img: Image.Image) -> Image.Image:
    return img.convert("RGB").resize((W, H), Image.LANCZOS)


def zoom_crop(img: Image.Image, t: float) -> Image.Image:
    """Crop interpolating from the full frame (t=0) to a top-right,
    toggle-focused, aspect-correct box (t=1), then scale to the canvas.
    Both endpoint boxes share the canvas aspect, so no distortion mid-zoom."""
    im = img.convert("RGB")
    sw, sh = im.size
    th = round(ZOOM_TARGET_W * H / W)
    tx0, ty0, tx1, ty1 = sw - ZOOM_TARGET_W, 0, sw, th
    x0 = round((1 - t) * 0 + t * tx0)
    y0 = round((1 - t) * 0 + t * ty0)
    x1 = round((1 - t) * sw + t * tx1)
    y1 = round((1 - t) * sh + t * ty1)
    return im.crop((x0, y0, x1, y1)).resize((W, H), Image.LANCZOS)


def load(spec) -> Image.Image:
    if isinstance(spec, tuple) and spec[0] == "ZOOM":
        return zoom_crop(Image.open(FRAMES / spec[1]), spec[2])
    return fullbleed(Image.open(FRAMES / spec))


def main() -> None:
    keys = [(load(s), hold) for s, hold in SEQ]
    frames: list[Image.Image] = []
    durations: list[int] = []
    for i, (img, hold) in enumerate(keys):
        frames.append(img)
        durations.append(hold)
        if XFADE and i < len(keys) - 1:
            nxt = keys[i + 1][0]
            for k in range(1, XFADE + 1):
                frames.append(Image.blend(img, nxt, k / (XFADE + 1)))
                durations.append(70)

    pframes = [f.convert("P", palette=Image.ADAPTIVE, colors=256) for f in frames]
    # loop=0 → infinite loop (README hero convention). The long final-frame
    # hold (see SEQ) rests on the dark-tree finale before looping back to the
    # light terminal, so the wrap reads as a clean restart, not a continuation.
    pframes[0].save(
        OUT,
        save_all=True,
        append_images=pframes[1:],
        duration=durations,
        loop=0,
        optimize=True,
        disposal=2,
    )
    kb = OUT.stat().st_size / 1024
    print(f"wrote {OUT} : {W}x{H}, {len(frames)} frames, {kb:.0f} KB")


if __name__ == "__main__":
    main()
