#!/usr/bin/env python3
"""Crop each preview frame (phone card) into its own PNG for the README.

    python tools/cdp_frames.py "file://$PWD/preview/preview.html" preview/preview.png preview

Uses the DOM to find each ``.phone`` card, then crops the full-page screenshot at
the device pixel ratio, so the crops are exact rather than eyeballed offsets.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp_shot import CDP, capture, page_target  # noqa: E402

from PIL import Image  # noqa: E402

RECTS_JS = """
JSON.stringify([...document.querySelectorAll('.phone')].map((node, index) => {
  const box = node.getBoundingClientRect();
  const label = node.querySelector('.phone-head .label');
  return {
    index: index + 1,
    label: label ? label.textContent.trim() : '',
    left: box.left + window.scrollX,
    top: box.top + window.scrollY,
    width: box.width,
    height: box.height,
  };
}))
"""


def main() -> int:
    url, png_path, out_dir = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    width, height = capture(url, png_path, 1460, 2.0, 2.0)

    cdp = CDP(page_target()["webSocketDebuggerUrl"])
    try:
        result = cdp.send(
            "Runtime.evaluate", expression=RECTS_JS, returnByValue=True
        )
    finally:
        cdp.close()
    frames = json.loads(result["result"]["value"])

    image = Image.open(png_path)
    scale = image.width / width
    written = []
    for frame in frames:
        box = (
            int(frame["left"] * scale),
            int(frame["top"] * scale),
            int((frame["left"] + frame["width"]) * scale),
            int((frame["top"] + frame["height"]) * scale),
        )
        crop = image.crop(box)
        name = out_dir / f"frame-{frame['index']:02d}.png"
        crop.save(name, optimize=True)
        written.append((name.name, frame["label"], crop.size))
    for name, label, size in written:
        print(f"{name}  {size[0]}x{size[1]}  {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
