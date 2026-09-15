"""Optional AI fallback for OTP extraction (OpenRouter).

Off by default (``USE_AI_OTP_FALLBACK=true`` to enable). Two changes from the
original, which called this inline inside the IMAP thread with a 30s timeout:

* it runs only after the deterministic, offline scorer failed to find a code,
* it is wrapped in a short timeout and its result is validated as a 4-8 digit
  code before it is accepted.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

PROMPT = (
    "Extract the one-time password / verification code from this email. "
    "Reply with the digits only, nothing else. If there is no code, reply NONE.\n\n"
    "Email:\n{body}"
)

_MODEL = "openai/gpt-4o-mini"
_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"


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
