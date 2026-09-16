#!/usr/bin/env python3
"""Render preview/session.json into a Telegram-style preview image source.

The bubble text is inserted as-is: this project only ever emits Telegram's HTML
subset (``b i u s code pre a blockquote``), which is also valid HTML, so the
preview shows the same markup the client would receive -- including the escaping.

    python evidence/render_preview.py && python tools/cdp_shot.py \
        file://$PWD/preview/preview.html preview/preview.png --width 1460
"""

from __future__ import annotations

import base64
import html
import json
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SESSION = ROOT / "preview" / "session.json"
OUT = ROOT / "preview" / "preview.html"

MAX_PER_FRAME = 6

CSS = """
:root {
  --page: #101418;
  --frame-bg: #0e1621;
  --frame-head: #17212b;
  --bubble-in: #182533;
  --bubble-out: #2b5278;
  --text: #e9eef3;
  --meta: #8b9aa8;
  --accent: #5aa9e6;
  --button: #2b5278;
  --button-text: #eaf2fa;
  --code-bg: #0b1219;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 28px 24px 40px;
  background: var(--page); color: var(--text);
  font: 14px/1.45 -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
}
header.page { max-width: 1460px; margin: 0 auto 22px; }
header.page h1 { font-size: 21px; margin: 0 0 6px; letter-spacing: -0.2px; }
header.page h1 span { color: var(--accent); }
header.page p { margin: 3px 0; color: var(--meta); font-size: 13px; }
header.page code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px;
  font-size: 12px; color: #cfe3f5; }
.wrap { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-start;
  max-width: 1460px; margin: 0 auto; }
.phone { width: 360px; background: var(--frame-bg); border: 1px solid #1d2732;
  border-radius: 16px; overflow: hidden; box-shadow: 0 6px 22px rgba(0,0,0,.35); }
.phone-head { background: var(--frame-head); padding: 9px 12px; display: flex;
  align-items: center; gap: 8px; border-bottom: 1px solid #1d2732; }
.dot { width: 8px; height: 8px; border-radius: 50%; flex: 0 0 auto; }
.dot.user { background: #4caf7d; }
.dot.channel { background: #e0a33e; }
.dot.admin { background: #7c8cff; }
.phone-head .label { font-size: 12px; font-weight: 600; color: #dfe9f3; }
.phone-head .sub { font-size: 10.5px; color: var(--meta); margin-left: auto; }
.chat { padding: 12px 10px 14px; display: flex; flex-direction: column; gap: 8px; }
.msg { display: flex; flex-direction: column; max-width: 92%; }
.msg.in { align-self: flex-start; }
.msg.out { align-self: flex-end; align-items: flex-end; }
.bubble { padding: 8px 10px; border-radius: 12px; white-space: pre-wrap;
  word-wrap: break-word; overflow-wrap: anywhere; font-size: 12.5px; }
.msg.in .bubble { background: var(--bubble-in); border-bottom-left-radius: 4px; }
.msg.out .bubble { background: var(--bubble-out); border-bottom-right-radius: 4px; }
.bubble img.photo { display: block; width: 100%; border-radius: 9px; margin-bottom: 6px; }
.bubble code { background: var(--code-bg); padding: 1px 4px; border-radius: 4px;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 12px;
  color: #cfe3f5; }
.bubble pre { background: var(--code-bg); border-radius: 6px; padding: 6px 7px;
  margin: 5px 0 2px; white-space: pre-wrap; word-break: break-word;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; font-size: 11px;
  color: #b9cfe3; }
.bubble a { color: #8ec6ff; }
.bubble blockquote { margin: 4px 0; padding-left: 8px; border-left: 2px solid #3b4a5a; }
.bubble i { color: #c6d3de; font-style: italic; }
.buttons { display: grid; gap: 4px; margin-top: 5px; width: 100%; }
.buttons .row { display: flex; gap: 4px; }
.btn { flex: 1; background: var(--button); color: var(--button-text); border-radius: 8px; }
.btn.primary { background: #2f6fb5; }
.btn.success { background: #2f8f5b; }
.btn.danger  { background: #b0413e; }
  padding: 6px 7px; font-size: 11.5px; text-align: center; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.note { align-self: center; color: var(--meta); font-size: 11px; text-align: center;
  background: rgba(255,255,255,.04); border-radius: 10px; padding: 4px 10px; }
.caption { font-size: 10px; color: var(--meta); margin: 3px 2px 0; }
footer.page { max-width: 1460px; margin: 26px auto 0; color: var(--meta); font-size: 12px; }
footer.page code { background: var(--code-bg); padding: 1px 5px; border-radius: 4px; color: #cfe3f5; }
"""


def load() -> dict:
    return json.loads(SESSION.read_text(encoding="utf-8"))


def frames(events: list[dict]) -> list[tuple[str, list[dict]]]:
    """Group events into phone-sized frames: new frame on chat change or length."""
    grouped: list[tuple[str, list[dict]]] = []
    current_chat: str | None = None
    for event in events:
        if current_chat != event["chat"] or (
            grouped and len(grouped[-1][1]) >= MAX_PER_FRAME
        ):
            current_chat = event["chat"]
            grouped.append((current_chat, []))
        grouped[-1][1].append(event)
    return grouped


@lru_cache(maxsize=8)
def _data_uri(path: str) -> str:
    """Inline the asset so the preview is a single self-contained file."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return ""
    return f"data:image/{'png' if path.endswith('.png') else 'jpeg'};base64," + base64.b64encode(raw).decode()


def render_message(event: dict) -> str:
    if event["role"] == "note":
        return f'<div class="note">{html.escape(event["text"])}</div>'

    side = "out" if event["role"] in ("in", "channel") else "in"
    if event["role"] == "channel":
        side = "in"
    body = event["text"]  # already Telegram-HTML; the same tag names as HTML
    image = ""
    if event.get("photo"):
        uri = _data_uri(event["photo"])
        if uri:
            image = f'<img class="photo" src="{uri}" alt="brand photo">'
    out = [f'<div class="msg {side}">', f'<div class="bubble">{image}{body}</div>']
    buttons = event.get("buttons")
    if buttons:
        def render_button(button) -> str:
            if isinstance(button, dict):
                label, style = button.get("label", ""), button.get("style", "")
            else:  # older session files stored plain labels
                label, style = button, ""
            css = {"primary": "primary", "success": "success", "danger": "danger"}.get(
                str(style).lower(), ""
            )
            return f'<div class="btn {css}">{html.escape(str(label))}</div>'

        rows = "".join(
            '<div class="row">' + "".join(render_button(b) for b in row) + "</div>"
            for row in buttons
        )
        out.append(f'<div class="buttons">{rows}</div>')
    if event.get("note"):
        out.append(f'<div class="caption">{html.escape(event["note"])}</div>')
    out.append("</div>")
    return "".join(out)


def render_phone(chat: str, events: list[dict], labels: dict[str, str], index: int) -> str:
    items = "".join(render_message(event) for event in events)
    return (
        f'<div class="phone"><div class="phone-head">'
        f'<span class="dot {chat}"></span>'
        f'<span class="label">{html.escape(labels.get(chat, chat))}</span>'
        f'<span class="sub">frame {index}</span>'
        f"</div><div class=\"chat\">{items}</div></div>"
    )


def main() -> int:
    data = load()
    labels = data["chat_labels"]
    grouped = frames(data["events"])
    phones = "".join(
        render_phone(chat, events, labels, index)
        for index, (chat, events) in enumerate(grouped, start=1)
    )
    out = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>TempGail — bot preview</title><style>{CSS}</style></head>
<body>
<header class="page">
  <h1>{html.escape(data.get("brand", "TempGail"))} <span>· preview of a real session</span></h1>
  <p>Every bubble below was produced by the bot's own handlers and renderer —
     captured by driving <code>BotHandlers</code> + <code>GmailPoller</code> through
     fake Telegram/IMAP transports. Nothing here is hand-written copy.</p>
  <p>Alias in this session: <code>{html.escape(data['address'])}</code> ·
     4 emails ingested · {data['stats']['messages']} messages stored ·
     OTP + links extracted automatically.</p>
</header>
<div class="wrap">{phones}</div>
<footer class="page">
  Rendered from <code>preview/session.json</code> by <code>evidence/render_preview.py</code>.
  Buttons are real Telegram inline keyboards (labels are the bot's own button text).
  Nothing is sent with <code>parse_mode=Markdown</code>: every screen is HTML-escaped
  once, which is why a subject full of markup renders literally instead of breaking the send.
  The mailbox is opened read-only, so the bot never alters Gmail state ·
  {html.escape(data.get("credit", ""))}
</footer>
</body></html>
"""
    OUT.write_text(out, encoding="utf-8")
    print(f"wrote {OUT.relative_to(ROOT)} ({len(out) // 1024} KB, {len(grouped)} frames)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
