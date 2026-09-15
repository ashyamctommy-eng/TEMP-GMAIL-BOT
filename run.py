#!/usr/bin/env python3
"""Process entry point.

Deployment notes (this replaces the original ``handler(event)`` function, which
could never work: it called ``asyncio.run`` inside a request handler, so it
would block a web worker forever and never return a response):

* **systemd / supervisor** — run ``python run.py`` as a service
  (see deploy/gmailbot.service).
* **Plesk** — create a Python application with a startup file of ``run.py``, or
  add an "Additional deployment action"/scheduled restart that keeps the process
  alive; a Telegram bot is a long-running process, not a request/response app.
* **Docker / Kubernetes** — same command; the process handles SIGTERM and exits
  cleanly.
"""

from __future__ import annotations

import sys

from gmailbot.app import main

if __name__ == "__main__":
    sys.exit(main())
