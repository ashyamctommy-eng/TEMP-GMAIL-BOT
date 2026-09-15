# How I'd improve this code

You asked what I'd do with autonomy. Short answer: **fix the silent data loss
first, then the mail-loss logic, then delete the guesswork.** Everything below
is already implemented, running, and pinned by tests in this repo (123 tests,
`python -m pytest`).

I did not just read the file. I executed it against its own functions to check
each claim, because a review that guesses wrong wastes your time. Reproductions
live in `evidence/`; the raw numbers are quoted below.

---

## 0. Verdict in one paragraph

The feature set is genuinely good — alias generation, UID-less polling, OTP and
verification-link detection, copy buttons, admin tooling. The problem is that
almost every one of those features has a *silent* failure mode that looks like
"the bot is just slow today": ~50% of incoming mail is dropped under
concurrency, non-alias mail is re-fetched forever, PDF-level unread state
decides whether you ever see a code, OTP detection returns order numbers, and
the "Copy Link" button hands users a truncated URL. Silent failures are the
worst class of bug in a bot nobody is watching, so that is where I'd spend the
first hour.

Priority order I used:

1. **P0 — correctness/safety**: things that lose mail or leak data.
2. **P1 — reliability/operations**: things that make it die quietly at 3am.
3. **P2 — architecture/UX**: things that make the next change expensive.

---

## P0 — correctness and safety

### P0-1 · Import-time crash on missing config, and placeholder secrets

```python
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "Enter_your_admin_id_here"))
```

Run the file as shipped and it dies before `main()`: `ValueError: invalid
literal for int() with base 10: 'Enter_your_admin_id_here'`. Meanwhile
`BOT_TOKEN = os.getenv("BOT_TOKEN", "Enter_your_bot_tokken_here")` means the
process happily starts with a junk token and fails with a confusing Telegram
error instead of telling you what's missing. DB and log paths come from
`os.getcwd()`, so the same bot in a different working directory silently uses a
**different database**.

**Now**: `Config.from_env()` collects *all* problems and raises one
`ConfigError` listing them, validates the token shape and the 16-char App
Password, and resolves every path against the project root. Missing config
prints an actionable message and exits 2. → `tests/test_config.py`

### P0-2 · ~50% of incoming mail silently discarded (worst bug in the file)

One `sqlite3.Connection(..., check_same_thread=False)` is shared by the IMAP
poller thread and the asyncio event loop, with no lock, no WAL, no per-thread
connection. `add_message` does `INSERT` + `commit()` from the poller while
handlers read and commit from the loop thread. Reproduced with 2 writers + 1
reader, 400 inserts each:

```
errors during concurrent access: ["OperationalError('cannot commit - no transaction is active')"]
expected 800 rows, got 408  -> lost 392 writes      # 399 and 397 lost in later runs
journal_mode: delete
indexes: ['sqlite_autoindex_aliases_1']
```

Roughly **half** the writes (392 / 399 / 397 across three runs), with the error
swallowed inside a per-message `try` — so from the user's side, OTP emails just
never arrive. `journal_mode: delete` is the default, and there is not one index
on `messages`.

**Now**: per-thread connections (`threading.local`), WAL, `synchronous=NORMAL`,
a process-wide write lock, `PRAGMA user_version` migrations and the indexes the
read paths actually use. Regression test asserts **800/800 rows and no
exceptions**. → `tests/test_db.py::test_concurrent_writers_do_not_lose_messages`

### P0-3 · The same OTP could be delivered to two users

`add_message` resolves the alias with:

```sql
INSERT INTO messages (alias_id, ...) SELECT alias_id, ... FROM aliases WHERE alias_name = ? AND active = TRUE
```

That matches **every** row with that name. `aliases.alias_name` is globally
`UNIQUE`, so today only one row exists — but that constraint is what silently
blocks every other user (`test_db.py` reproduces: user 2 is refused a name user
1 took, which also leaks that it exists). Any future relaxation of that
constraint (or a manual DB edit) turns this into one user reading another's
codes. The design is "one mailbox, alias name = owner", so I made the ownership
explicit: `alias_owner()`, a single-row resolve, and SQL-level ownership checks
on every read (`message_secret`, `alias_messages`, `mark_seen`). Test proves
user 2 gets nothing for user 1's message even with a valid message id.
→ `test_db.py::test_mail_never_leaks_between_users`

### P0-4 · Non-alias mail re-fetched every 5 seconds, forever

```python
if alias_match:
    self.imap.store(email_id, '+FLAGS', '\\Seen')
else:
    logger.info("No alias match ...")   # never marked seen
```

The search is `UNSEEN`, so every unrelated unread email in that Gmail account is
fetched, parsed and logged again on every cycle for as long as it stays unread.
On a personal mailbox that is hundreds of messages every 5 seconds.

**Now**: UID-cursor polling. A `meta.last_uid` row advances after each message is
committed (per message, so a crash neither loses nor duplicates mail), and
unmatched mail simply advances the cursor. → `test_mail.py::test_unmatched_mail_is_not_refetched_forever`

### P0-5 · Opening the email on your phone made the bot miss the OTP

Because delivery detection depends on `\Seen`, any message the user reads in
Gmail first is invisible to the bot — which is the normal case for someone whose
OTP arrives as a push notification. The bot's entire purpose, defeated by a
race with the user's phone.

**Now**: the mailbox is opened `readonly=True` and nothing is ever flagged; the
UID cursor is the source of truth, so unread state stays entirely yours.
→ `test_mail.py::test_poll_stores_mail_and_advances_cursor` asserts
`select` is read-only and `STORE` is never called.

### P0-6 · Free text created aliases, and commands were interpreted loosely

`handle_text_message` treats any text matching `^[a-zA-Z0-9_-]{1,20}$` as an
alias name. Say "hi" to the bot and you get an alias called `hi`. Verified:
`'hi' -> True` → alias created.

**Now**: a confirmation step ("Did you mean to create `hi`?") with a button, plus
reserved names (`otp`, `view`, …) that would otherwise collide with callback
routing. → `test_handlers.py::test_greeting_does_not_create_an_alias`

### P0-7 · Untrusted text interpolated into Markdown (broken sends + spoofing)

Email subjects and bodies, and user feedback, are pasted into `parse_mode='Markdown'`
with no escaping. That is why so many sends need a `try/except` that re-sends the
same text without formatting, and it lets a user forge the admin-facing header:

```python
user_info = f"👤 **User Information:**\n🔗 Username: @{user.username or 'N/A'}\n🆔 User ID: {user.id}\n"
```

A user cannot change the *values* (they come from Telegram), but they can inject
markdown that renders a fake "User Information" block inside their own feedback
text, so the admin cannot tell which is which. Also `[Link 1]({link[:50]}...)`
renders a **broken URL**, and the "Copy Link" button copies `verification_links[0][:30]`
— a 30-character prefix of the URL, i.e. never a working link.

**Now**: all rendering goes through one module using `parse_mode=HTML` with a
single escaping pass; OTPs are `<code>` (tap-to-copy in every client); links use
the **full** URL in `href` with a short *label*; user content is always escaped
inside a `<blockquote>`. → `test_content.py::test_links_keep_the_full_href_with_a_short_label`,
`test_content.py::test_feedback_forwarding_escapes_body`

### P0-8 · The AI OTP fallback stalled mail collection

`_extract_otp_ai` did a blocking `requests.post(..., timeout=30)` inside the IMAP
polling thread. One slow call = 30 seconds with no mail fetched.

**Now**: off by default, injectable, only consulted after the deterministic
scorer fails, 8s timeout, result validated as 4–8 digits. → `gmailbot/ai.py`,
`test_otp.py::test_extractor_uses_ai_fallback_only_when_regex_fails`

---

## P1 — reliability and operations

* **OTP detection returned order numbers.** `r'\b\d{6}\b'` was tried *first*, so
  on a shipping notification the original returns `'5691'` — a 4-digit order id.
  Now candidates are *scored*: labelled codes first, then code-then-label, then
  `123-456` SMS style, then bare digits only with positive context
  ("verify"/"code") and no negative context ("order"/"zip"/"total"/"tracking"/
  year). Returning `None` is strictly better than returning the wrong code,
  because a wrong code burns the user's attempt.
  → `test_otp.py` (9 real-code cases, 10 false-positive cases)

* **HTML-only mail was stored raw.** When a message had no `text/plain` part the
  original stored `'<html><body><p>Code: ...'` as the "body" and ran regexes over
  markup. Now a small `HTMLParser` converts to text and keeps anchor text
  together with its URL, so "Verify your email" buttons still produce links.
  → `test_mail.py::test_html_only_mail_is_converted_to_text`

* **No flood control on `/broadcast`.** A tight `await` loop with no delay gets
  the bot rate-limited/banned on any real user base. Now 20 msg/s with
  `RetryAfter` handling, and failures are counted and reported.
  → `test_handlers.py::test_broadcast_reports_delivery`

* **Unbounded memory growth.** `self.user_data` accumulated
  `otp_{message_id}` entries per user and was never pruned, so it grew for the
  lifetime of the process — and it was per-process state, so after a restart
  every "copy" button silently did nothing. Now secrets are fetched from SQLite
  by message id with an ownership check; there is no cache to leak.
  → `db.message_secret()` + `test_db.py::test_mail_never_leaks_between_users`

* **Feedback channel probed on every press.** Each `/feedback` ran `get_chat` +
  `get_chat_member` + a test *send* + a *delete* — an unauthenticated test
  message in the admin channel every time, plus the Pillow dependency just to
  build a throwaway image. Now the permission probe is cached (10 min) and
  invalidated on failure; three `/feedback` presses = 1 API call, 0 test
  messages. → `test_handlers.py::test_feedback_permission_probe_is_cached`

* **Timezone-free timestamps.** `TIMESTAMP DEFAULT CURRENT_TIMESTAMP` gives
  naive UTC strings that get re-parsed with a hard-coded format (crashes on
  microseconds). Now fixed-width UTC ISO-8601 everywhere, with a legacy parser
  for old rows, so text ordering equals chronological ordering.

* **Silent failure of `handler(event)`.** The "Plesk entry point" calls
  `asyncio.run(bot.run())`, a loop that never returns — as a request handler it
  would block a worker forever and never respond. Removed; `run.py` documents
  the three ways to actually keep it alive, and `post_shutdown` stops the poller
  and closes the DB so restarts are clean.

---

## P2 — architecture and UX

* **One 2,194-line file.** Split into `config / db / aliases / otp / links /
  htmltext / mail / notify / formatting / callbacks / handlers / app`, with
  constructor injection. Why this pays off, concretely: the OTP listing
  was written **twice** (`/otp` and the `view_otp` callback) and the message
  listing twice as well — they had already drifted (one showed 3 links, the
  other 2). One renderer now.

* **Formats were guessed, not tracked.** `_detect_alias_format` labels 40% of
  generated aliases "Leet Style" because its class `[4e1i0st]` matches most
  English words (`tiger123` → "Leet Style", `zokita42` → "Leet Style"). The
  generator now stores the format it used in the database — you already knew the
  answer, so stop reverse-engineering it.

* **The generator rejected its own output.** `_is_unique_enough` blocklists
  `^[a-z]+$`, which is exactly `adjective+noun` — 0% of that format's output
  passes, so a third of generations silently fell through to the hex fallback
  while the UI still advertised "10+ formats". Now: 9 named formats, all
  reachable (asserted), with a narrow degeneracy filter that keeps readable
  aliases. → `test_content.py::test_every_format_is_reachable`

* **Tuple unpacking everywhere.** `msg_id, alias, subject, body, received_at,
  seen, otp_code, verification_links = msg` in several places; a schema change
  breaks every caller silently. Now typed dataclasses (`Alias`, `Message`,
  `StoredMessage`, `AliasAddResult`).

* **Callback data built by string concatenation with `_` separators**, which is
  ambiguous for the `snake_case` aliases the generator itself produces. Now
  `:`-separated builders with a 64-byte assertion test, and a parser that treats
  client input as untrusted.

* **Blocking SQLite inside `async def` handlers.** Fine at 5 users, stalls the
  bot's polling loop under contention. Now `asyncio.to_thread` for DB work.

* **No rate limiting on `/generate`** — a single user could grow the alias table
  without bound. Now per-user sliding-window limits (`GENERATE_PER_HOUR`) plus a
  per-user alias cap.

---

## Checked, and *not* a bug

Being wrong in a review is expensive, so here are the two hypotheses I tested
and discarded:

* `add_message` gates on `cursor.rowcount` after `INSERT ... SELECT`. I expected
  `-1` and silently unsaved mail. **Measured**: 1 on insert, 0 on no-match —
  correct.
* I suspected `_is_unique_enough` was producing collisions. **Measured**: the
  final result is always "unique" because the uuid fallback masks it. The real
  bug was the format collapse described above, not collisions.

---

## What I did not change, on purpose

I kept the *product decisions* and only fixed the engineering, because you own
those calls:

1. **One Gmail mailbox for all users.** Aliases are logical isolation only;
   everyone's OTPs land in one account. A compromised mailbox, or a Gmail
   password reset, exposes every user's codes at once, and Gmail's own ToS is
   your exposure, not mine to decide. If this grows, the durable fix is one
   mailbox per user (or per tenant) with `alias_owner` scoped to it — the
   `UNIQUE(alias_name)` constraint is the single line that encodes today's
   design.
2. **1-hour message TTL** (original comment says "for testing"). Now a config
   value (`MESSAGE_TTL_SECONDS`) so it's a decision, not a stray literal.
3. **Soft delete** for aliases. Restoring is friendlier than losing the name,
   and the namespace is shared, so re-claiming would be a race.
4. **Legacy DB compatibility.** The refactor reads databases created by the
   original file (adds the new columns in place) so you can deploy without a
   migration script. → `test_db.py::test_legacy_database_is_migrated`

---

## How to verify

```bash
cd projects/gmailbot
pip install -r requirements.txt
python -m pytest -q            # 123 passed
python -m pyflakes gmailbot tests run.py
python evidence/reproduce_bug_report.py   # runs the BEFORE numbers against your original file
```

`tests/` covers config validation, the DB concurrency regression, ownership and
de-duplication, IMAP cursor behaviour (including "no `\Seen` writes"), OTP
true/false positives, link unwrapping and escaping, callback-data limits, and
the full Gmail-bytes → Telegram-alert pipeline — all with fakes, no network.

## Rollout I'd follow

1. **Deploy the refactor against the same Gmail account and the same `bot.db`.**
   The migration is additive and tested against a legacy schema.
2. Keep the old file running side by side for a day if you want a safety net;
   both can read the same DB, but only one should poll IMAP.
3. Watch `bot.log` for `pruned`, `duplicate mail ignored` and `feedback channel`
   lines — those are the three places the old code was blind.
4. Then decide on the two open product questions above (mailbox isolation, TTL).
