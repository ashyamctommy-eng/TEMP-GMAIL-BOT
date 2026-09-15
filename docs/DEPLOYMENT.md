# Deployment

One long-running process: it holds a Telegram long-poll and an IMAP connection
open. So it needs a **worker**, not a web handler.

**Every platform needs:**

1. The env vars from `.env.example` (`BOT_TOKEN`, `GMAIL_EMAIL`,
   `GMAIL_APP_PASSWORD`, `ADMIN_USER_ID`, `FEEDBACK_CHANNEL_ID`). Optional:
   `FORCE_JOIN` + `REQUIRED_CHANNELS` to make users join a channel first — you can
   also add channels from inside Telegram with `/addchannel`.
2. **1 replica** — two copies on one mailbox double-notify.
3. A persistent path for `DB_PATH` / `LOG_PATH` (a volume in the cloud), or your
   aliases and codes are wiped on each deploy.
4. Outbound access to `api.telegram.org` (HTTPS) and `imap.gmail.com:993`.

| Platform | Works? |
| --- | --- |
| Railway | ✅ easiest |
| Any VPS | ✅ cheapest long-term |
| Docker / Fly / Render worker / k8s | ✅ |
| Plesk | ✅ as a persistent process, not a request handler |
| Shared cPanel (Hostnin Cloud/Web Hosting, Hostinger shared…) | ⚠️ no |
| Serverless / Cloud Functions | ❌ |

---

## Railway

Committed config does the work: `railway.json`, `Procfile`, `.python-version`.

1. **New Project → Deploy from GitHub repo** → `TEMP-GMAIL-BOT`.
2. **Variables** — add the required set, plus:
   ```ini
   DB_PATH=/data/bot.db
   LOG_PATH=/data/bot.log
   ```
3. **Volumes → New Volume**, mount at `/data`. Skip this and every redeploy wipes
   the database.
4. **Settings → Deploy → Replicas = 1.**
5. It binds no port — ignore the "no healthcheck / no public domain" hints.

Startup log should read:

```
starting gmailbot (db=/data/bot.db)
database ready at /data/bot.db (schema v2)
gmail poller started (interval=10s)
bot ready
```

Then `/start` in Telegram.

| Log line | Fix |
| --- | --- |
| `Invalid configuration:` | the missing variables are listed |
| `login rejected` / `AUTHENTICATIONFAILED` | wrong App Password — see [GMAIL_SETUP.md](GMAIL_SETUP.md) |
| `could not set the admin command menu` | harmless until you `/start` once as admin |
| crash loop | read the traceback above the restart |

Updates: push to `main`, Railway redeploys. Migrations are additive — no manual step.

---

## VPS (systemd)

```bash
sudo adduser --system --group --home /opt/gmailbot gmailbot
sudo -u gmailbot git clone https://github.com/ashyamctommy-eng/TEMP-GMAIL-BOT.git /opt/gmailbot
cd /opt/gmailbot
sudo -u gmailbot python3 -m venv .venv
sudo -u gmailbot .venv/bin/pip install -r requirements.txt
sudo -u gmailbot cp .env.example .env && sudo -u gmailbot nano .env
sudo mkdir -p /opt/gmailbot/data && sudo chown gmailbot:gmailbot /opt/gmailbot/data

sudo cp deploy/gmailbot.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now gmailbot
journalctl -u gmailbot -f
```

The unit restarts on failure and sends `SIGTERM`, which the bot handles cleanly.

## Docker

```bash
docker build -t tempgmailbot .
docker run -d --name tempgmailbot --restart unless-stopped --env-file .env \
  -e DB_PATH=/data/bot.db -v tempgmailbot-data:/data tempgmailbot
```

## Plesk

Run it as a systemd service on the server (above), or as a Python app with a
supervisor/restart action pointing at `run.py`.

---

## Shared cPanel (Hostnin Cloud/Web Hosting)

Won't work properly: there's no supervisor for background workers, Passenger stops
apps when idle, SQLite on shared storage locks under contention, and IMAP 993 is
often blocked.

Ask support: *"Can a Python process make an outbound TLS connection to
`imap.gmail.com:993` and stay running 24/7?"* If no, use a VPS or Railway.
Hostnin's **VPS** plans are the right product from them.

Last resort on shared cPanel — cron keep-alive, fragile:

```bash
# cPanel → Cron Jobs, every minute
* * * * * /usr/bin/flock -n /tmp/gmailbot.lock ~/bots/gmailbot/.venv/bin/python ~/bots/gmailbot/run.py >> ~/bots/gmailbot/data/cron.log 2>&1
```

Keep `.env` and the database outside `public_html`.

---

**Dev:** PORIOT PKEMOI, CTO — Telegram [@Poriot_ke](https://t.me/Poriot_ke) ·
Channel [@nativecodes](https://t.me/nativecodes)
