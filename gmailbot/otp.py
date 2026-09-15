"""OTP extraction.

The original tried ``r'\\b\\d{6}\\b'`` *first*, so any 6-digit number anywhere in
the mail won: order numbers, ZIP codes, invoice totals. On a real shipping
notification it returned ``'5691'`` (a 4-digit order id) as the "OTP"
(reproduced in evidence/).

This module scores candidates instead of taking the first regex hit:

1. labelled codes ("verification code: 123456") score highest,
2. then code-then-label ("123456 is your code"),
3. then separated groups ("123-456", common in SMS-style mail),
4. bare 4-8 digit numbers are only accepted when the surrounding text contains
   a positive signal *and* no negative signal (order/zip/total/tracking/year).

If nothing clears the threshold the answer is ``None``. Returning no code is
strictly better than returning a wrong one: a wrong code silently burns the
user's attempt.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

_LABEL = (
    r"(?:otp|one[\s\-]?time(?:\s+(?:password|passcode|pin|code))?"
    r"|verification\s+code|verify\s+code|security\s+code|auth(?:entication)?\s+code"
    r"|login\s+code|sign[\s\-]?in\s+code|access\s+code|confirmation\s+code"
    r"|sms\s+code|code|pin|passcode|password|token)"
)

# (pattern, score, group). Higher score wins; ties break on earliest position.
_PATTERNS: tuple[tuple[str, int, int], ...] = (
    # "code: 123456", "OTP is 4839" -- label then digits
    (rf"{_LABEL}[^\d\n]{{0,24}}?(\d{{4,8}})(?!\d)", 100, 1),
    # "123456 is your verification code" -- digits then label
    (rf"(?<![\d\-])(\d{{4,8}})(?![\d\-])[^\d\n]{{0,24}}?{_LABEL}", 95, 1),
    # "123-456" / "123 456" SMS style
    (r"(?<![\d\-])(\d{3})[-\s](\d{3})(?![\s\-]?\d)", 80, 0),
    # bracketed or quoted: [483920] (483920) "483920"
    (r"[\[\(\{\"'](\d{4,8})[\]\)\}\"']", 75, 1),
)

#: Bare numbers need surrounding evidence.
_BARE_PATTERN = r"(?<![\d\-])(\d{4,8})(?![\d\-])"
_BARE_SCORE = 40

_POSITIVE = (
    "otp", "code", "pin", "passcode", "verify", "verification", "confirm",
    "authenticate", "authentication", "log in", "login", "sign in", "sign-in",
    "one-time", "one time", "2fa", "two-factor", "security", "access",
)
_NEGATIVE = (
    "order", "invoice", "receipt", "total", "subtotal", "amount", "price",
    "usd", "eur", "gbp", "$", "€", "£", "tracking", "shipment", "shipped",
    "delivery", "delivers", "zip", "postal", "postcode", "phone", "tel",
    "call", "mobile", "fax", "weight", "kg", "lb", "qty", "quantity",
    "items", "item", "discount", "vat", "tax", "ref", "reference", "ticket",
    "booking", "flight", "seat", "account number", "iban", "swift", "bic",
    "unsubscribe", "copyright", "reserved", "cvv", "expires", "valid thru",
    "points", "miles", "balance", "salary",
)

_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_CONTEXT_WINDOW = 45


@dataclass(frozen=True)
class Candidate:
    code: str
    score: int
    position: int
    source: str

    @property
    def accepted(self) -> bool:
        return self.score >= OTP_MIN_SCORE


#: Minimum score to return a code at all.
OTP_MIN_SCORE = 50


def _context(text: str, start: int, end: int) -> str:
    window = text[max(0, start - _CONTEXT_WINDOW) : end + _CONTEXT_WINDOW].lower()
    return window


def _context_score(context: str) -> int:
    score = 0
    if any(word in context for word in _POSITIVE):
        score += 40
    if any(word in context for word in _NEGATIVE):
        score -= 70
    return score


def find_candidates(text: str) -> list[Candidate]:
    """All plausible codes in ``text``, best first."""
    if not text:
        return []
    candidates: list[Candidate] = []

    for pattern, score, group in _PATTERNS:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            raw = match.group(group) if group else "".join(match.groups())
            digits = re.sub(r"\D", "", raw or "")
            if not 4 <= len(digits) <= 8:
                continue
            if _YEAR_RE.match(digits) and score < 90:
                continue
            candidates.append(
                Candidate(digits, score, match.start(), f"pattern:{pattern[:24]}")
            )

    for match in re.finditer(_BARE_PATTERN, text):
        digits = match.group(1)
        if _YEAR_RE.match(digits):
            continue
        score = _BARE_SCORE + _context_score(_context(text, match.start(), match.end()))
        candidates.append(Candidate(digits, score, match.start(), "bare"))

    # Keep the highest score per code, then sort by score / position.
    best: dict[str, Candidate] = {}
    for candidate in candidates:
        current = best.get(candidate.code)
        if current is None or candidate.score > current.score:
            best[candidate.code] = candidate
    return sorted(best.values(), key=lambda c: (-c.score, c.position))


def extract_otp(text: str, *, min_score: int = OTP_MIN_SCORE) -> str | None:
    """Return the most likely OTP, or ``None`` when nothing is convincing."""
    for candidate in find_candidates(text):
        if candidate.score >= min_score:
            return candidate.code
    return None


class OtpExtractor:
    """Regex extractor with an optional, explicitly-opt-in AI fallback.

    The AI path is off by default and injectable, so it can be tested without
    network access. In the original it ran inline inside the IMAP polling thread
    with a 30-second ``requests`` timeout, stalling mail collection.
    """

    def __init__(
        self,
        *,
        ai_lookup: Callable[[str], str | None] | None = None,
        min_score: int = OTP_MIN_SCORE,
    ) -> None:
        self.ai_lookup = ai_lookup
        self.min_score = min_score

    def extract(self, text: str) -> str | None:
        code = extract_otp(text, min_score=self.min_score)
        if code is not None:
            return code
        if self.ai_lookup is None:
            return None
        try:
            guess = self.ai_lookup(text[:2000])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("AI OTP fallback failed: %s", exc)
            return None
        guess = (guess or "").strip()
        if guess.isdigit() and 4 <= len(guess) <= 8:
            logger.info("AI fallback produced a code")
            return guess
        return None

    def extract_all(self, text: str) -> Iterable[Candidate]:
        return find_candidates(text)
