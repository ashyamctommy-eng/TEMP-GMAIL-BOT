# 𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕

A Telegram bot that issues Gmail plus-aliases (`you+alias@gmail.com`), watches the
mailbox over IMAP, and pushes each new message back to the user — with OTP codes
and verification links already pulled out.

**Bot by: 𝙋𝙤𝙧𝙞𝙤𝙩_𝙠𝙚** · every message the bot sends carries that credit line.

This is a refactor of a single 2,194-line `GmailBot.py`. The original still runs;
what changed and why is in **[docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md)**, and
every fix is pinned by a test.

```
gmailbot/
  config.py      env + .env loading, validation, path resolution
  db.py          SQLite: per-thread conns, WAL, migrations, ownership rules
  aliases.py     alias generation (9 named formats) + validation
  otp.py         ranked OTP extraction (labelled > contextual > bare)
  links.py       verification-link extraction, redirect unwrapping
  htmltext.py    HTML mail -> text, keeping anchor text with its URL
  mail.py        IMAP parsing + UID-cursor polling
  notify.py      thread -> asyncio hand-off, retries, flood control, limits
  formatting.py  every screen, escaped, clamped to Telegram's limits
  callbacks.py   callback-data encoding (64-byte safe, parsed as untrusted)
  handlers.py    Telegram handlers, one send path
  app.py         wiring + graceful shutdown
tests/           123 tests, no network, no token, no Gmail account
```

## Quick start

```bash
pip install -r requirements.txt
cp .env.example .env          # then fill it in
python -m pytest -q           # sanity check: 123 passed
python run.py
```

You need:

1. **A bot token** from [@BotFather](https://t.me/BotFather).
2. **Your numeric user id** (from [@userinfobot](https://t.me/userinfobot)) —
   this account gets `/ban`, `/unban`, `/broadcast`, `/stats`.
3. **A feedback channel id** where `/feedback` is delivered, with the bot added
   as an admin or a member that may post.
4. **Gmail IMAP + an App Password**: enable 2-step verification, then create an
   App Password (16 characters — a normal account password will not work), and
   enable IMAP in Gmail settings.

Missing or malformed configuration fails at startup with a list of exactly what
to fix; the process never starts with placeholder secrets.

## Deploying

This is a long-running process, not a web app. The original shipped a
`handler(event)` function for Plesk that called `asyncio.run(...)` — as a request
handler it would block a worker forever and never return.

* **systemd** — `deploy/gmailbot.service` (copy to `/etc/systemd/system/`,
  adjust paths, `systemctl enable --now gmailbot`).
* **Plesk** — create a Python app whose startup file is `run.py`, or use a
  supervisor/cron-restart action that keeps the process alive.
* **Docker/k8s** — same command; the process handles SIGTERM, stops the IMAP
  poller and closes the database cleanly.

Logs go to `$LOG_PATH` (default `data/bot.log`, rotating at 5 MB × 3) and to
stderr, which is what the container/service manager captures.

## How it works

```
Telegram ──/generate──▶ aliases table (one owner per alias name)
Gmail IMAP ──poll every N s──▶ parse ──▶ OTP + links ──▶ messages table
                                              │
                                              └──▶ notifier ──▶ Telegram alert
```

* **Polling is cursor-based, not flag-based.** The last processed IMAP `UID`
  lives in the database, the mailbox is opened read-only, and nothing is ever
  marked `\Seen`. Your unread state stays yours, and a message you open on your
  phone first is still picked up. It also means unrelated mail is never fetched
  twice.
* **De-duplication is on the RFC `Message-ID`**, so a restart or a re-run cannot
  double-post a code.
* **OTP detection scores rather than guesses.** Labelled codes win; bare numbers
  are only accepted with supporting context and no contradicting context
  ("order", "zip", "total", "tracking"). No code is better than a wrong code.
* **Messages expire** after `MESSAGE_TTL_SECONDS`; a write lock plus WAL means
  the poller and the handlers never corrupt or drop each other's writes.

## Branding

Everything cosmetic lives in config, so you never have to edit Python to change it:

| Env var | Default | What it does |
| --- | --- | --- |
| `BOT_BRAND_NAME` | `𝑻𝒆𝒎𝒑 𝑮𝒎𝒂𝒊𝒍 𝑩𝒐𝒕` | name shown in the welcome screen, descriptions and captions |
| `BOT_CREDIT` | `𝙋𝙤𝙧𝙞𝙤𝙩_𝙠𝙚` | rendered as `Bot by: …`, appended to **every** message |
| `BOT_PHOTO_PATH` | `gmailbot/assets/tempgmail.jpg` | image attached to `/start` and OTP alerts (`none` disables) |
| `SEND_BRAND_PHOTO` | `true` | master switch for the photo |

Notes worth knowing before you change them:

* The defaults use Unicode *styled* fonts (Mathematical Alphanumeric Symbols).
  They are ordinary characters, so a client whose font lacks those glyphs shows
  boxes — swap `BOT_BRAND_NAME`/`BOT_CREDIT` for plain text if you care about
  that (or about screen readers), no code change needed.
* Telegram counts length in **UTF-16 units**, and every styled character costs
  two. The clamping logic measures that way, so a branded message can never
  exceed the 4096-character limit or the 1024-character caption limit.
* The footer is applied once, in one place (`formatting.finalize`), for command
  replies *and* for push alerts. A photo's caption is capped, so a long OTP alert
  goes out as a short branded caption **plus** the full text — it is never
  truncated, because that could drop the code itself.
* The asset is an original illustration generated for this project, not a scraped
  logo — Gmail's marks are Google trademarks and shipping them in your repo is a
  liability.

## Commands are pre-registered

`app.post_init` pushes the command list to Telegram on every startup, so users
see the `/` menu and the blue **Menu** button without you configuring anything in
@BotFather. Bot descriptions (the text shown on the bot's profile before a user
types) are set the same way. Admin commands (`/stats`, `/ban`, `/unban`,
`/broadcast`) are published only to the owner's chat via a scoped command list,
so they do not clutter anyone else's menu. `tests/test_app.py` asserts that every
registered command is advertised and that the descriptions fit Telegram's limits.

## Behaviour worth knowing

| Command | Notes |
| --- | --- |
| `/start`, `/help` | welcome + menu, sent with the brand photo |
| `/generate [name]` | random alias, or a name you choose; rate-limited per user |
| `/history` | your aliases, with delete/restore |
| `/view <alias> [page]` | messages for one alias, paginated |
| `/otp` | recent codes and links, paginated, sent with the brand photo |
| `/delete <alias>` | deactivate (mail is dropped, alias can be restored) |
| `/feedback` | text or screenshot to the admin channel |
| `/ban`, `/unban`, `/broadcast`, `/stats` | admin only |

Plain text is **not** treated as an alias: the bot asks first. Aliases named
after commands (`otp`, `view`, …) are rejected as reserved.

## Security notes

* Only the owner of an alias can read its mail or reveal its codes; ownership is
  enforced in SQL, so a forged callback cannot reach someone else's message.
* All user-controlled content is HTML-escaped exactly once before it is sent.
* Secrets are fetched from the database on demand, never cached in memory.
* The mailbox is opened read-only; the bot never modifies Gmail state.

## Credits

* **Dev / maintainer:** **𝙋𝙤𝙧𝙞𝙤𝙩_𝙠𝙚**
* Original single-file bot: `evidence/original_GmailBot.py` (kept byte-identical
  so `evidence/reproduce_bug_report.py` can show the before/after)
* Refactor, tests and documentation: built on the review in
  [docs/IMPROVEMENTS.md](docs/IMPROVEMENTS.md)
* Brand illustration: generated for this project (no third-party assets)

If you build on this, keep the credit line intact or overridable — it is
configured as `BOT_CREDIT`, not hardcoded.

## Honest limitations

* **One Gmail mailbox serves every user.** Isolation between users is logical,
  not physical: a compromised mailbox exposes everyone's codes, and alias names
  are a shared namespace (first come, first served). If this needs to scale or
  hold anything sensitive, that is the first thing to redesign.
* Determining *which* platform a code belongs to, and whether using aliases to
  register there complies with that platform's terms, is your call — the bot
  only reads the mailbox you point it at.

## Preview

`preview/preview.png` is a rendered session — but not a mockup. It is produced by
driving the real `BotHandlers` and `GmailPoller` against the fake Telegram/IMAP
transports from `tests/`, capturing exactly what the bot emits, and rendering
those strings in a Telegram-style layout:

```bash
python evidence/preview_session.py   # real code, fake transports -> preview/session.json
python evidence/render_preview.py    # -> preview/preview.html
python tools/cdp_shot.py "file://$PWD/preview/preview.html" preview/preview.png --width 1460
```

What it shows: onboarding, alias creation, four ingested emails (`/start`,
`/generate`, push alerts, `/otp`, `/view`, `/history`), button presses, the
"did you mean to create an alias?" confirmation for stray text, feedback
delivery into the admin channel, and the admin's `/stats` + `/broadcast`.

**Caveat:** this is the bot's real output over a simulated Gmail mailbox. It runs
no HTTP calls and needs no bot token, so it proves the rendering and the logic —
not that your Telegram account, channel ids and Gmail app password work. Only a
live run does that.

## Tests

```bash
python -m pytest -q                              # all tests
python -m pytest tests/test_db.py -q             # the concurrency regression
python evidence/reproduce_bug_report.py          # BEFORE numbers, from the original file
```
