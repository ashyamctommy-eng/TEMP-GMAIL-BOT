"""Optional AI fallback for OTP extraction (OpenRouter).

Off by default (``USE_AI_OTP_FALLBACK=true`` to enable). Two changes from the
original, which called this inline inside the IMAP thread with a 30s timeout:

* it runs only after the deterministic, offline scorer failed to find a code,
* it is wrapped in a short timeout and its result is validated as a 4-8 digit
  code before it is accepted.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

PROMPT = (
    "Extract the one-time password / verification code from this email. "
    "Reply with the digits only, nothing else. If there is no code, reply NONE.\n\n"
    "Email:\n{body}"
)

_MODEL = "openai/gpt-4o-mini"
_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


def redact_url(url: str) -> str:
    """Strip everything secret from a URL before it leaves the machine.

    Magic links and verification links *are* credentials, so the judge never sees
    a fragment, a query value or a path segment long enough to be a token: only
    the scheme, host, first path segment and the parameter *names*.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return ""
    if not parsed.netloc:
        return ""
    path = "/" + "/".join(
        segment if len(segment) <= 24 else "<redacted>"
        for segment in (parsed.path or "").strip("/").split("/")
        if segment is not None
    ).rstrip("/")
    names = sorted({key for key in parse_qs(parsed.query)})
    query = "&".join(f"{name}=<redacted>" for name in names)
    redacted = f"{parsed.scheme}://{parsed.netloc}{path if path != '/' else ''}"
    if query:
        redacted += f"?{query}"
    if parsed.fragment:
        redacted += "#<redacted>"
    return redacted


JUDGE_PROMPT = (
    "You are classifying links from an email so a bot can pick the ONE link the "
    "reader should click. Bulk senders wrap the real link in a click tracker: a "
    "URL on their own domain that redirects to the destination. The real link is "
    "usually on the sender's service domain (e.g. app.example.com/magic-link) "
    "while wrappers look like url1234.mail.example.com/ls/click?upn=... or "
    "click.example.com/e/c/...\n\n"
    "Links (tokens already redacted, unknown sender):\n{links}\n\n"
    "Reply with ONE JSON object only:\n"
    '{{"real": <index of the link to show, 0-based>, '
    '"wrappers": [<indexes that are click wrappers>]}}\n'
    "If you cannot tell, reply {{\"real\": -1, \"wrappers\": []}}."
)


def make_link_judge(
    api_key: str | None, *, timeout: float = 8.0, model: str | None = None
):
    """Return ``judge(urls) -> (real_index, wrapper_indexes)`` or ``None``.

    Only ever sent redacted URLs (see :func:`redact_url`). Any failure returns
    ``(-1, ())``: the deterministic ranking stands and no pattern is learned.
    """
    if not api_key:
        return None
    import requests

    chosen_model = model or "openai/gpt-4o-mini"

    def judge(urls: list[str]) -> tuple[int, tuple[int, ...]]:
        redacted = [redact_url(url) for url in urls]
        numbered = "\n".join(f"{index}. {value}" for index, value in enumerate(redacted))
        try:
            response = requests.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": chosen_model,
                    "messages": [
                        {"role": "user", "content": JUDGE_PROMPT.format(links=numbered)}
                    ],
                    "max_tokens": 80,
                    "temperature": 0,
                },
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("link judge request failed: %s", exc)
            return -1, ()
        if response.status_code != 200:
            logger.warning("link judge HTTP %s", response.status_code)
            return -1, ()
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("unexpected link judge response: %s", exc)
            return -1, ()
        match = re.search(r"\{.*\}", content or "", re.DOTALL)
        if not match:
            return -1, ()
        try:
            parsed = json.loads(match.group(0))
            real = int(parsed.get("real", -1))
            wrappers = tuple(
                int(index)
                for index in parsed.get("wrappers", [])
                if isinstance(index, (int, float, str)) and str(index).lstrip("-").isdigit()
            )
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("could not parse link judge output: %s", exc)
            return -1, ()
        if not 0 <= real < len(urls):
            real = -1
        wrappers = tuple(index for index in wrappers if 0 <= index < len(urls))
        logger.info("link judge: real=%s wrappers=%s", real, wrappers)
        return real, wrappers

    return judge


def make_openrouter_lookup(api_key: str | None, *, timeout: float = 8.0):
    """Return a ``lookup(body) -> code | None`` callable, or ``None``."""
    if not api_key:
        return None
    import requests

    def lookup(body: str) -> str | None:
        try:
            response = requests.post(
                _ENDPOINT,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "messages": [
                        {"role": "user", "content": PROMPT.format(body=body[:4000])}
                    ],
                    "max_tokens": 16,
                    "temperature": 0,
                },
                timeout=timeout,
            )
        except Exception as exc:  # noqa: BLE001 - the caller treats this as "no code"
            logger.warning("AI lookup request failed: %s", exc)
            return None
        if response.status_code != 200:
            logger.warning("AI lookup HTTP %s", response.status_code)
            return None
        try:
            content = response.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning("unexpected AI response: %s", exc)
            return None
        match = re.search(r"\d{4,8}", content or "")
        return match.group(0) if match else None

    return lookup
