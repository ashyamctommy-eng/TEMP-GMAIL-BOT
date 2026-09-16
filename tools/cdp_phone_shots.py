#!/usr/bin/env python3
"""Take phone-sized screenshots of named screens from a rendered preview.

    python tools/cdp_phone_shots.py "file://$PWD/preview/preview.html" preview/shots

Each shot is a real viewport capture (390x844 CSS px at 2x) scrolled to the
element containing a given snippet -- i.e. what a phone actually shows, rather
than a 4000px-tall column crop.
"""

from __future__ import annotations

import base64
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cdp_shot import CDP, page_target  # noqa: E402

VIEWPORT = (390, 844)
SCALE = 2.0

#: (filename, snippet to scroll to). "__FIRST__" means the first chat card.
SHOTS: list[tuple[str, str]] = [
    # the welcome screen (the very first card is the join gate when one is required)
    ("01-start", "Generate unlimited Gmail aliases"),
    ("02-alias-ready", "Alias ready"),
    ("03-otp-alert", "New code"),
    ("04-codes-list", "Recent codes"),
    ("05-messages", "message(s)"),
    ("06-feedback-channel", "Feedback ID"),
    ("07-join-gate", "Join required"),
    ("08-admin-panel", "Admin panel"),
    ("09-claude-magic-link", "secure link to Claude.ai"),
]

# Squeeze the desktop preview layout down to phone width for the screenshots.
PHONE_CSS = """
<style id="phone-shot">
  body { padding: 6px !important; }
  .wrap { gap: 10px !important; max-width: none !important; }
  .phone { width: 100% !important; border-radius: 12px !important; }
  header.page, footer.page { display: none !important; }
</style>
"""

FIND_JS = """
(() => {
  const snippet = %s;
  // The bot renders its own chrome in the brand font (Mathematical Bold Italic),
  // so searches run against the plain-ASCII form of the text.
  const plain = (text) => [...text].map((ch) => {
    const c = ch.codePointAt(0);
    if (c >= 0x1D468 && c <= 0x1D481) return String.fromCharCode(65 + c - 0x1D468);
    if (c >= 0x1D482 && c <= 0x1D49B) return String.fromCharCode(97 + c - 0x1D482);
    if (c >= 0x1D7CE && c <= 0x1D7D7) return String.fromCharCode(48 + c - 0x1D7CE);
    return ch;
  }).join('');
  document.querySelectorAll('#phone-shot').forEach(node => node.remove());
  document.head.insertAdjacentHTML('beforeend', %s);
  // The cards use overflow:hidden, which still lets *programmatic* scrolling
  // move their content. scrollIntoView would scroll the card instead of the
  // page, so reset any inner offset and scroll the window to an absolute Y.
  document.querySelectorAll('.phone, .chat').forEach(node => { node.scrollTop = 0; });
  window.scrollTo(0, 0);

  const cards = [...document.querySelectorAll('.phone')];
  let target = null;
  if (snippet === '__FIRST__') {
    target = cards[0];
  } else {
    target = [...document.querySelectorAll('.bubble, .btn')].find(
      node => plain(node.textContent).includes(snippet)
    ) || null;
  }
  if (!target) return JSON.stringify({ok: false});

  const box = target.getBoundingClientRect();
  const offset = snippet === '__FIRST__' ? 0 : 70; // keep the header above the bubble
  window.scrollTo(0, Math.max(0, window.scrollY + box.top - offset));
  const card = target.closest('.phone') || target;
  const after = card.getBoundingClientRect();
  return JSON.stringify({
    ok: true,
    top: Math.round(box.top - offset),
    card_top: Math.round(after.top),
    height: Math.round(after.height),
  });
})()
"""


def main() -> int:
    url, out_dir = sys.argv[1], Path(sys.argv[2])
    out_dir.mkdir(parents=True, exist_ok=True)

    cdp = CDP(page_target()["webSocketDebuggerUrl"])
    written = []
    try:
        cdp.send("Page.enable")
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            width=VIEWPORT[0],
            height=VIEWPORT[1],
            deviceScaleFactor=SCALE,
            mobile=False,
        )
        cdp.send("Page.navigate", url=url)
        time.sleep(2.0)

        for name, snippet in SHOTS:
            result = cdp.send(
                "Runtime.evaluate",
                expression=FIND_JS % (json.dumps(snippet), json.dumps(PHONE_CSS)),
                returnByValue=True,
            )
            info = json.loads(result["result"]["value"])
            if not info.get("ok"):
                print(f"skip {name}: snippet {snippet!r} not found")
                continue
            time.sleep(0.35)
            shot = cdp.send("Page.captureScreenshot", format="png")
            path = out_dir / f"{name}.png"
            path.write_bytes(base64.b64decode(shot["data"]))
            written.append((name, path.stat().st_size // 1024, info))
    finally:
        cdp.close()

    for name, size_kb, info in written:
        print(f"{name}.png  {size_kb} KB  (card top={info['top']} height={info['height']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
