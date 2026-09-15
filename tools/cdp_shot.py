#!/usr/bin/env python3
"""Full-page screenshot helper for the sandbox Chrome (CDP over websocket).

Usage:
    python tools/cdp_shot.py <url> <output.png> [--width 1400] [--scale 2] [--wait 1.5]

Why not the browser MCP screenshot tool: its output is viewport-sized. Preview
images of a chat transcript are several thousand pixels tall, so we ask Chrome
for a capture beyond the viewport instead of stitching scroll positions.
"""

from __future__ import annotations

import argparse
import base64
import json
import time
import urllib.request

import websocket

DEBUG_HOST = "127.0.0.1:9222"


def page_target() -> dict:
    with urllib.request.urlopen(f"http://{DEBUG_HOST}/json", timeout=10) as response:
        targets = json.load(response)
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        # Open one if Chrome started with no page.
        with urllib.request.urlopen(f"http://{DEBUG_HOST}/json/new?about:blank", timeout=10) as resp:
            return json.load(resp)
    return pages[0]


class CDP:
    def __init__(self, ws_url: str) -> None:
        # suppress_origin: Chrome rejects websocket upgrades that carry an
        # Origin header it was not started with (--remote-allow-origins).
        self.ws = websocket.create_connection(
            ws_url, timeout=60, suppress_origin=True
        )
        self.counter = 0

    def send(self, method: str, **params) -> dict:
        self.counter += 1
        message_id = self.counter
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params}))
        while True:
            payload = json.loads(self.ws.recv())
            if payload.get("id") == message_id:
                if "error" in payload:
                    raise RuntimeError(f"{method} failed: {payload['error']}")
                return payload.get("result", {})

    def close(self) -> None:
        self.ws.close()


def capture(url: str, out_path: str, width: int, scale: float, wait: float) -> tuple[int, int]:
    target = page_target()
    cdp = CDP(target["webSocketDebuggerUrl"])
    try:
        cdp.send("Page.enable")
        # Pin the width, then measure the content so the capture covers it all.
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=1000,
            deviceScaleFactor=scale,
            mobile=False,
        )
        cdp.send("Page.navigate", url=url)
        time.sleep(wait)

        metrics = cdp.send(
            "Runtime.evaluate",
            expression=(
                "JSON.stringify({w: document.documentElement.scrollWidth,"
                " h: document.documentElement.scrollHeight})"
            ),
            returnByValue=True,
        )
        size = json.loads(metrics["result"]["value"])
        height = min(int(size["h"]) + 40, 30000)  # protocol safety cap
        cdp.send(
            "Emulation.setDeviceMetricsOverride",
            width=width,
            height=height,
            deviceScaleFactor=scale,
            mobile=False,
        )
        time.sleep(0.6)
        shot = cdp.send(
            "Page.captureScreenshot",
            format="png",
            captureBeyondViewport=True,
        )
        with open(out_path, "wb") as handle:
            handle.write(base64.b64decode(shot["data"]))
        return width, height
    finally:
        cdp.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("out")
    parser.add_argument("--width", type=int, default=1400)
    parser.add_argument("--scale", type=float, default=2.0)
    parser.add_argument("--wait", type=float, default=1.5)
    args = parser.parse_args()
    width, height = capture(args.url, args.out, args.width, args.scale, args.wait)
    print(f"wrote {args.out} ({width}x{height} css px @{args.scale}x)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
