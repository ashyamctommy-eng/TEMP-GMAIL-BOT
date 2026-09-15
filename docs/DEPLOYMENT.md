# Deployment

This bot is **one long-running process**. It holds a Telegram long-poll and an
IMAP connection open, polls every `POLL_INTERVAL_SECONDS`, and keeps a SQLite
file in between. That single fact decides which platforms work:

| Platform | Works? | Why |
| --- | --- | --- |
| **Railway** | ✅ Recommended | Runs a persistent worker; add a volume for the SQLite file |
| **Any VPS** (Hostinger, DigitalOcean, Hetzner, Contabo, Hostnin *VPS*) | ✅ Best value | systemd unit included in `deploy/`; root access, your own disk |
| **Docker / Fly / Render (worker) / k8s** | ✅ | `Dockerfile` included |
| **Plesk** (as a Python app / long-running process) | ✅ with care | Needs a persistent process, not a request handler |
| **Shared cPanel hosting** (Hostnin *Cloud/Web Hosting*, Hostinger shared, etc.) | ⚠️ Not recommended | No supported supervisor for background workers; processes get reaped when idle |
| **Serverless / Cloud Functions** | ❌ | Nothing to keep the poll loop alive between invocations |

What every platform needs:

1. **Environment variables** — see `.env.example`. At minimum `BOT_TOKEN`,
   `GMAIL_EMAIL`, `GMAIL_APP_PASSWORD`, `ADMIN_USER_ID`, `FEEDBACK_CHANNEL_ID`.
   The process refuses to start with placeholders, and prints what is missing.
2. **One instance only.** Two copies polling the same mailbox will both write to
   their own database and double-notify. Keep `numReplicas: 1`.
3. **A writable, persistent path** for `DB_PATH` and `LOG_PATH`. On ephemeral
   filesystems (Railway/Render/Fly) that means a **volume**; without one, every
   redeploy wipes aliases and stored messages.
4. **Outbound access** to `api.telegram.org` (HTTPS) and `imap.gmail.com` (IMAPS,
   port 993). Some shared hosts block non-web outbound ports — ask support
   before buying.

---

## Railway

Railway runs the repo as a worker with no config file needed beyond what is
committed (`Procfile`, `railway.json`, `.python-version`).

### 1. Create the service

1. <https://railway.app> → **New Project** → **Deploy from GitHub repo** →
   pick `TEMP-GMAIL-BOT`.
2. Railway reads `requirements.txt` via Nixpacks, then runs
   `python run.py` from `railway.json` (`startCommand`).
3. Under **Settings → Deploy**, make sure **Replicas = 1**.

> This service binds no port. Railway may warn that it could not detect a
> healthcheck or a public domain — that is expected for a worker. Do not
> generate a domain for it; nothing listens on it.

### 2. Environment variables

**Variables → New Variable** (or *Raw Editor* to paste all at once):

```ini
BOT_TOKEN=123456789:AA...
GMAIL_EMAIL=you@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop
ADMIN_USER_ID=123456789
FEEDBACK_CHANNEL_ID=-1001234567890

# Persistence: keep both paths inside the mounted volume.
DB_PATH=/data/bot.db
LOG_PATH=/data/bot.log

MESSAGE_TTL_SECONDS=3600
POLL_INTERVAL_SECONDS=10
```

Railway injects `PORT`; the bot ignores it. Do **not** put `BOT_TOKEN` in the
repo — `.env` is git-ignored on purpose.

### 3. Add the volume (do not skip)

**Service → Variables/Data → Volumes → New Volume**, mount path `/data`, then
confirm `DB_PATH=/data/bot.db`. Without this the SQLite file is recreated empty
on every deploy, so every user's aliases and codes disappear.

### 4. Deploy and verify

```bash
# In the Railway dashboard → Deployments → View Logs, you want to see:
#   gmailbot.app - INFO - starting gmailbot (db=/data/bot.db)
#   gmailbot.db  - INFO - database ready at /data/bot.db (schema v2)
#   gmailbot.mail- INFO - gmail poller started (interval=10s)
#   gmailbot.app - INFO - bot ready
```

Then open Telegram and send `/start` to your bot. If it replies with the welcome
card and photo, everything is wired.

Common Railway failures:

| Log line | Meaning |
| --- | --- |
| `Invalid configuration:` | A variable is missing or malformed; the message lists them |
| `Gmail connection failed: ... AUTHENTICATIONFAILED` | Use an **App Password** (16 chars), not the account password, and enable IMAP in Gmail |
| `could not set the admin command menu: chat not found` | Harmless: it appears until you press `/start` once as the admin |
| Restarts every few minutes | Check the restart policy; a crash loop logs the traceback above it |

### 5. Updating

Push to `main` — Railway redeploys automatically. The volume keeps the database,
and the schema migration is additive, so an upgrade never needs a manual step.

---

## VPS (systemd) — cheapest to run long-term

```bash
sudo adduser --system --group --home /opt/gmailbot gmailbot
sudo -u gmailbot git clone https://github.com/ashyamctommy-eng/TEMP-GMAIL-BOT.git /opt/gmailbot
cd /opt/gmailbot
sudo -u gmailbot python3 -m venv .venv
sudo -u gmailbot .venv/bin/pip install -r requirements.txt
sudo -u gmailbot cp .env.example .env && sudo -u gmailbot nano .env   # fill it in
sudo mkdir -p /opt/gmailbot/data && sudo chown gmailbot:gmailbot /opt/gmailbot/data

sudo cp deploy/gmailbot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gmailbot
journalctl -u gmailbot -f
```

`deploy/gmailbot.service` already sets `Restart=always`, `ReadWritePaths=/opt/gmailbot/data`
and `ProtectSystem=full`. It sends `SIGTERM`, which the app handles: the IMAP
poller stops and the database closes cleanly.

---

## Docker

```bash
docker build -t tempgmailbot .
docker run -d --name tempgmailbot --restart unless-stopped \
  --env-file .env \
  -e DB_PATH=/data/bot.db -e LOG_PATH=/data/bot.log \
  -v tempgmailbot-data:/data \
  tempgmailbot
```

---

## Plesk

Plesk can host this, but only as a **persistent process**, not through
`handler(event)`-style request handling (the original file had such a handler;
`asyncio.run()` inside a web request blocks the worker forever and never
responds — that is why it is gone).

* Either run it as a **systemd service** on the Plesk server (see above), or
* use the *Node.js/Python* app type with a supervisor / scheduled restart, and
  point the startup command at `run.py`.

---

## Shared cPanel hosting (this is the Hostnin case)

**Short answer: no — Hostnin's Cloud/Web Hosting plans are cPanel shared hosting
(LiteSpeed, PHP-first, Node.js as a separate product) and this bot does not fit
that model.** Concretely:

1. **There is no supported supervisor for a background worker.** cPanel's
   "Setup Python App" runs your code through Phusion Passenger, which starts the
   app on an HTTP request and **stops it when idle**. This bot has nothing to
   request it — it must already be running to receive Telegram updates.
2. **Idle/limit reapers.** Shared hosting (CloudLinux + LVE) kills long-lived
   processes that exceed CPU/process limits, and a process holding an IMAP
   connection for hours looks exactly like that.
3. **SQLite on shared storage.** Works until two processes touch it or the
   account is throttled mid-write; then you get `database is locked` or a
   half-written transaction.
4. **Outbound ports.** Many shared plans only permit HTTP(S) outbound. IMAPS
   (993) to Gmail may be blocked — worth asking Hostnin support specifically:
   *"can a Python process on my plan make an outbound TLS connection to
   imap.gmail.com:993 and stay running 24/7?"* If the answer to the second half
   is no, stop there.
5. **Hostnin does sell VPS plans** (with root access). That is the right product
   from them for this: it is the first-class case, above.

### Last resort on shared cPanel (fragile, expect to babysit)

If you only have shared hosting and want to experiment, a cron keep-alive is the
only mechanism available. It works *sometimes*:

```bash
# 1. Create a virtualenv and install (cPanel → Setup Python App, or in SSH)
cd ~/bots/gmailbot
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in; keep it OUTSIDE public_html

# 2. Cron (cPanel → Cron Jobs), every single minute:
* * * * * /usr/bin/flock -n /tmp/gmailbot.lock ~/bots/gmailbot/.venv/bin/python ~/bots/gmailbot/run.py >> ~/bots/gmailbot/data/cron.log 2>&1
```

`flock` guarantees only one copy runs, so the bot survives until the host reaps
it — then the next minute's cron starts it again. Expect gaps in delivery while
it is dead, and treat any "users can't sign up / codes are late" report as
suspected reaping. Keep the database and `.env` **outside `public_html`** so they
are not web-accessible.

Given the cost of a small VPS versus the time spent fighting the reaper, the VPS
(or Railway) route is the better trade.
