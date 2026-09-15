# 𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕

I'm **PORIOT PKEMOI** (CTO). I built this bot: it gives you Gmail plus-aliases
(`you+alias@gmail.com`), watches the mailbox over IMAP, and sends each new email
to you in Telegram with the OTP code and verification links already extracted.

**Dev contact:** Telegram [@Poriot_ke](https://t.me/Poriot_ke) · Channel [@nativecodes](https://t.me/nativecodes)

## Screenshots

<p align="center">
  <img src="preview/shots/01-start.png" width="235" alt="Welcome screen">
  <img src="preview/shots/02-alias-ready.png" width="235" alt="New alias">
  <img src="preview/shots/03-otp-alert.png" width="235" alt="OTP alert">
</p>
<p align="center">
  <img src="preview/shots/04-codes-list.png" width="235" alt="Recent codes">
  <img src="preview/shots/05-messages.png" width="235" alt="Messages for an alias">
  <img src="preview/shots/06-feedback-channel.png" width="235" alt="Admin feedback channel">
</p>

Rendered from the bot's own output (`preview/preview.png` = full session), not
mockups. They show rendering and logic — a live run is what proves your token,
channel and Gmail password.

## Quick start

You need four things:

1. **Bot token** — [@BotFather](https://t.me/BotFather) → `/newbot`.
2. **Your Telegram user id** — [@userinfobot](https://t.me/userinfobot). Gets `/ban`, `/unban`, `/broadcast`, `/stats`.
3. **A feedback channel id** (e.g. `-1001234567890`) with the bot added as admin or member.
4. **Gmail App Password** — [docs/GMAIL_SETUP.md](docs/GMAIL_SETUP.md). A normal
   account password will not work. There is no IMAP switch to enable.

```bash
pip install -r requirements.txt
cp .env.example .env        # fill in the four values above
python tools/check_gmail.py # verifies Gmail before you start
python run.py
```

Then send `/start` to your bot.

## Commands

The bot registers these with Telegram on startup — no BotFather setup.

| Command | What it does |
| --- | --- |
| `/start`, `/help` | welcome + menu (with brand photo) |
| `/generate [name]` | new alias, random or your own |
| `/history` | your aliases, delete/restore |
| `/view <alias> [page]` | messages for one alias |
| `/otp` | recent codes and links (with brand photo) |
| `/delete <alias>` | deactivate an alias |
| `/feedback` | send text or a screenshot to the admin |
| `/cancel` | leave feedback mode |
| `/stats`, `/ban`, `/unban`, `/broadcast` | admin only |

Send a plain word and it asks before creating an alias.

## Settings

Everything is env config — I never hardcode it.

| Variable | Default | Purpose |
| --- | --- | --- |
| `BOT_TOKEN` | — | from BotFather |
| `GMAIL_EMAIL` | — | mailbox that receives the aliases |
| `GMAIL_APP_PASSWORD` | — | 16 characters; spaces are stripped, so paste as Google shows it |
| `ADMIN_USER_ID` | — | your Telegram id |
| `FEEDBACK_CHANNEL_ID` | — | where `/feedback` lands |
| `DB_PATH` / `LOG_PATH` | `data/` | keep these on a volume in the cloud |
| `MESSAGE_TTL_SECONDS` | `3600` | how long codes stay readable |
| `POLL_INTERVAL_SECONDS` | `10` | IMAP poll interval |
| `INITIAL_LOOKBACK_DAYS` | `1` | how far back the first poll looks |
| `MAX_ALIASES_PER_USER` / `GENERATE_PER_HOUR` | `25` / `20` | per-user limits |
| `IMAP_MAILBOX` | `INBOX` | set `[Gmail]/All Mail` if a filter archives alias mail |
| `BOT_BRAND_NAME` | `𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕` | name in the welcome, description and captions |
| `BOT_CREDIT` | `𝙋𝙤𝙧𝙞𝙤𝙩_𝙠𝙚` | rendered as `Bot by: …` on every message |
| `BOT_PHOTO_PATH` / `SEND_BRAND_PHOTO` | bundled image / `true` | photo on `/start` and OTP alerts |
| `OPENROUTER_API_KEY` / `USE_AI_OTP_FALLBACK` | off | optional AI fallback when regex finds no code |

## How it works

Alias mail is matched by tag, stored in SQLite, and pushed to the owner of that
alias. Polling tracks the last IMAP UID and opens the mailbox **read-only** — your
unread state is never touched, and a message you open on your phone first still
reaches the bot. Duplicates are dropped by RFC `Message-ID`, codes expire after
`MESSAGE_TTL_SECONDS`, and OTP detection is scored, so a wrong code is never
preferred over no code.

## Deploy

| Platform | Works? |
| --- | --- |
| Railway | ✅ easiest — see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) |
| Any VPS | ✅ cheapest long-term (`deploy/gmailbot.service`) |
| Docker / Fly / Render worker | ✅ `Dockerfile` included |
| Plesk | ✅ as a persistent process |
| Shared cPanel (Hostnin Cloud/Web, etc.) | ⚠️ no — no supervisor for background workers |
| Serverless | ❌ nothing keeps the poll loop alive |

It is one long-running process: **1 replica**, and persist `DB_PATH` on a volume.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q            # 147 passed, no network or token needed
```

Covers config validation, the SQLite concurrency regression, alias ownership,
IMAP cursor behaviour, OTP true/false positives, escaping, and the full
Gmail → Telegram pipeline. `evidence/reproduce_bug_report.py` shows the bugs the
original single-file version had.

---

**Dev:** PORIOT PKEMOI, CTO — Telegram [@Poriot_ke](https://t.me/Poriot_ke) ·
Channel [@nativecodes](https://t.me/nativecodes)
