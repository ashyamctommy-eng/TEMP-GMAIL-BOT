# Gmail setup — App Password + IMAP in 8 steps

The bot logs into your Gmail over **IMAP** with an **App Password**. A normal
Google account password will *not* work: Google blocked "less secure apps" in
2022, so any script that talks to Gmail must use an App Password (or OAuth,
which this bot does not implement).

You need roughly 3 minutes and the Google account you want the bot to read.

---

## Step 1 — Sign in to the right account

Open <https://myaccount.google.com> and make sure you are signed in as the
mailbox that will receive the aliases. Everything below applies to that account.
This is the address you put in `GMAIL_EMAIL`.

## Step 2 — Turn on 2-Step Verification (this is the gate)

**App passwords only exist if 2-Step Verification is enabled.** This is the step
almost everyone misses; without it there is no "App passwords" page at all.

* Direct link: <https://myaccount.google.com/signinoptions/two-step-verification>
* Or: **Security** → *How you sign in to Google* → **2-Step Verification** → finish
  the setup (phone prompt or authenticator app).

> Personal accounts: free and instant.
> Workspace (company/college) accounts: your admin may have to permit it — see
> Troubleshooting below.

## Step 3 — Create the App Password

* Direct link: <https://myaccount.google.com/apppasswords> ← bookmark this, it is
  the one link you will come back to
* Or: **Security** → *How you sign in to Google* → **2-Step Verification** →
  scroll to the very bottom → **App passwords**

Google will ask you to re-enter your password.

## Step 4 — Name it and copy the 16 characters

1. Under **App name**, type something recognisable, e.g. `TempGmail Bot`
   (this is just a label so you can revoke it later without affecting anything
   else).
2. Click **Create**.
3. Google shows a password in **four groups of four**, like:

   ```
   abcd efgh ijkl mnop
   ```

   **Copy it now** — it is displayed only once. If you lose it, just create
   another one (they are unlimited).

   **Paste it exactly as shown, spaces and all** — that is fine:

   ```ini
   GMAIL_APP_PASSWORD=abcd efgh ijkl mnop
   ```

   The bot removes whitespace before using it, so `abcd efgh ijkl mnop` and
   `abcdefghijklmnop` are identical to it. (If you also put this password into
   some *other* tool that does not normalise — an SMTP script, a mail client —
   strip the spaces there.)

This is *not* your Google password, and it cannot be used to sign in to Gmail's
website. It only grants mail access to the app you named.

## Step 5 — Paste it into `.env`

```ini
GMAIL_EMAIL=you@gmail.com
GMAIL_APP_PASSWORD=abcdefghijklmnop     # no spaces needed; 16 characters
```

The bot validates this at startup and refuses to run with a placeholder, so a
typo surfaces immediately instead of as a mysterious login failure. `.env` is
git-ignored — never commit it.

## Step 6 — Enable IMAP in Gmail

The App Password lets you log in; **IMAP has to be switched on as well**, or the
login succeeds and the mailbox looks empty.

Do this in a **desktop browser** — the Gmail phone app has no such setting, and
if you open Gmail in a mobile browser, switch to "Desktop site" first.

* **Fastest:** open <https://mail.google.com/mail/u/0/#settings/fwdandpop> — that
  jumps straight to the tab you need.
* Or by hand: Gmail → ⚙️ gear icon (top right) → **See all settings** → tab
  **Forwarding and POP/IMAP** → section **IMAP access** → select **Enable IMAP**
  → scroll down → **Save Changes**.

It takes effect immediately; no restart of Gmail is needed. If the IMAP options
are greyed out, your Workspace admin has disabled them:
`admin.google.com` → **Apps → Google Workspace → Gmail → End User Access →
POP and IMAP access** → allow IMAP for your organisation.

## Step 7 — Verify before you deploy

From the project root (it reads `.env` or the environment):

```bash
python tools/check_gmail.py
```

It logs in, confirms IMAP is enabled, counts the messages in the mailbox it will
poll, and lists anything recently addressed to your alias pattern. Successful
output looks like:

```
GMAIL_EMAIL       : you@gmail.com
alias pattern     : you+<alias>@gmail.com
IMAP              : imap.gmail.com:993  connected
login             : OK
mailbox           : INBOX (read-only)  · 41 messages
alias mail today  : 2 found
  11:02  "Your Acme ID verification code"   -> you+swiftfalcon@gmail.com
  10:48  "Confirm your email address"       -> you+bravetiger@gmail.com
All good. Start the bot with: python run.py
```

## Step 8 — Start the bot

```bash
python run.py
```

Then send `/start` to your bot in Telegram. If it replies with the welcome card,
you are done. Repeat Steps 3–4 for a *different* mailbox any time you want the
bot to read another inbox — one App Password per mailbox.

---

## Which email does the bot read, exactly?

Aliases are **plus-addressed**: mail to `you+anything@gmail.com` lands in your
normal inbox but keeps the tag. The bot reads the mailbox in `GMAIL_EMAIL` and
groups messages by that tag.

* `ALIAS_ROOT` / `ALIAS_DOMAIN` default to the local part and domain of
  `GMAIL_EMAIL` — change them only if you want tags on a different address.
* **Auto-archiving filters hide your mail from the bot.** The poller reads the
  folder named by `IMAP_MAILBOX` (default `INBOX`). If you have a Gmail filter
  that "skips the inbox" for alias mail, the messages are still in your account
  but not in INBOX — either leave alias mail in the inbox, or point the bot at
  all mail:

  ```ini
  IMAP_MAILBOX=[Gmail]/All Mail
  ```

## Security notes

* The App Password grants full mail read/write access for the account. Revoke it
  any time at <https://myaccount.google.com/apppasswords> — nothing else breaks.
* It is stored in plaintext in `.env` on the server (like any service
  credential). Keep the file out of `public_html`, `chmod 600 .env`, and never
  commit it. If you ever paste one into a chat, treat it as disclosed and
  regenerate.
* The bot opens the mailbox **read-only**: it never marks messages seen, never
  deletes, never sends. Your unread state stays yours.
* Every user of your bot shares this one mailbox — the aliases are only
  logically separated. A mailbox compromise exposes every user's codes. If this
  grows, give each tenant their own mailbox.

---

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `AUTHENTICATIONFAILED Invalid credentials` | You used the account password instead of the App Password, the password is mistyped, or it was revoked. Create a new one (Step 3). |
| The password seems accepted but login returns `Application-specific password required` | Exactly the "less secure apps" block — you must use an App Password, not the account password. |
| **No "App passwords" option anywhere** | 2-Step Verification is off (Step 2). For Workspace accounts, the admin must allow it: `admin.google.com` → **Security → Authentication → Less secure apps / App passwords**. Accounts in Google's **Advanced Protection Program** cannot use App Passwords at all — use a different mailbox. |
| Login OK, but `/otp` stays empty and no mail arrives | IMAP is off (Step 6); or the mail is not addressed to `you+tag@gmail.com`; or a filter moved it out of the folder named by `IMAP_MAILBOX` (see above); or the message is older than `MESSAGE_TTL_SECONDS` (default 1 hour, measured from when the bot stored it). |
| `could not resolve host imap.gmail.com` / connection times out | Outbound port **993** is blocked by your host. This is common on shared hosting — ask support, or use a VPS. |
| It worked, then stopped after a few weeks | Expected on shared hosting that reaps long-running processes; the log will show reconnect attempts. See `docs/DEPLOYMENT.md`. |
| Workspace admin cannot enable App Passwords | Use OAuth2 (the bot does not implement it) or a mailbox outside the organisation. |
