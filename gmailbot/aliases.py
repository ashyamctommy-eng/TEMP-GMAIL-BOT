"""Alias generation and validation.

Fixes over the original (see docs/IMPROVEMENTS.md):

* P2-1  Formats are a registry that reports which generator produced the alias.
  The original generated the alias and then *guessed* its format from a regex
  chain; ``_detect_alias_format`` labelled 40% of aliases "Leet Style" because
  its character class ``[4e1i0st]`` matches most English words.
* P2-2  ``_is_unique_enough`` rejected its own ``adjective+noun`` format 100% of
  the time (``^[a-z]+$`` was on the "common patterns" blocklist), so a third of
  generations silently fell through to the UUID fallback.
* P2-3  Uniqueness is now checked against the database with a retry, instead of
  "try three times and hope".
"""

from __future__ import annotations

import random
import re
import string
from dataclasses import dataclass
from typing import Callable, Iterable, Iterator

# Gmail plus-address tags: letters, digits, dot, underscore, hyphen.
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,28}[a-z0-9]$")
MIN_LENGTH = 3
MAX_LENGTH = 30

#: Words that would collide with commands or with callback-data routing.
RESERVED = {
    "otp", "start", "help", "generate", "history", "view", "delete", "feedback",
    "cancel", "ban", "unban", "broadcast", "stats", "admin", "new", "menu",
    "postmaster", "abuse", "mailer-daemon", "no-reply", "noreply",
}

_ADJECTIVES = (
    "brave", "clever", "swift", "quick", "smart", "wise", "bold", "calm",
    "cool", "epic", "fast", "free", "fresh", "glad", "good", "great", "happy",
    "kind", "nice", "proud", "safe", "strong", "true", "wild", "young", "eager",
    "gentle", "honest", "lucky", "noble", "polite", "quiet", "rare", "rich",
    "sharp", "silly", "tiny", "vast", "warm",
)
_NOUNS = (
    "tiger", "eagle", "dragon", "phoenix", "fox", "wolf", "bear", "lion", "hawk",
    "falcon", "owl", "raven", "crow", "swan", "dove", "star", "moon", "sun",
    "cloud", "storm", "wave", "fire", "ice", "wind", "earth", "water", "shadow",
    "light", "crystal", "diamond", "pearl", "ruby", "emerald", "sapphire",
    "ninja", "samurai", "warrior", "knight", "hero", "legend",
)
_WORDS = _NOUNS + _ADJECTIVES + (
    "thunder", "lightning", "rocket", "quantum", "cosmic", "mystic", "ancient",
    "future", "golden", "silver", "bronze", "platinum", "titanium", "rapid",
    "instant", "sudden", "magic",
)
_CONSONANTS = "bcdfghjklmnpqrstvwxyz"
_VOWELS = "aeiou"
_HEX = "0123456789abcdef"
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_LEET_MAP = {"a": "4", "e": "3", "i": "1", "o": "0", "s": "5", "t": "7", "g": "9", "z": "2"}


def normalize(name: str) -> str:
    """Canonical form used for storage and lookup."""
    return name.strip().lower()


def is_valid(name: str) -> bool:
    return bool(ALIAS_RE.match(normalize(name)))


def validation_error(name: str) -> str | None:
    """Human-readable reason, or ``None`` when the name is fine."""
    candidate = name.strip()
    if not candidate:
        return "Alias is empty."
    if len(candidate) < MIN_LENGTH:
        return f"Alias must be at least {MIN_LENGTH} characters."
    if len(candidate) > MAX_LENGTH:
        return f"Alias must be at most {MAX_LENGTH} characters."
    if not ALIAS_RE.match(candidate.lower()):
        return (
            "Use letters, digits, dots, hyphens and underscores only, and start and "
            "end with a letter or digit."
        )
    if ".." in candidate or "--" in candidate or "__" in candidate:
        return "Avoid repeated separators like '..', '--' or '__'."
    if candidate.lower() in RESERVED:
        return f"'{candidate.lower()}' is reserved. Pick something else."
    return None


def looks_degenerate(candidate: str) -> bool:
    """Reject outputs that are technically valid but look like machine noise.

    Deliberately narrower than the original blocklist: plain lowercase words
    ("bravetiger") are *good* aliases and must not be rejected.
    """
    if len(candidate) < MIN_LENGTH:
        return True
    if len(set(candidate)) < max(3, len(candidate) * 0.34):
        return True
    if re.search(r"(.)\1{3,}", candidate):  # aaaa
        return True
    lowered = candidate.lower()
    if any(seq in lowered for seq in ("1234", "abcd", "qwerty", "asdf", "zxcv")):
        return True
    return False


@dataclass(frozen=True)
class GeneratedAlias:
    name: str
    format: str


def _word_number(rng: random.Random) -> str:
    return f"{rng.choice(_WORDS)}{rng.randint(100, 9999)}"


def _adjective_noun(rng: random.Random) -> str:
    return f"{rng.choice(_ADJECTIVES)}{rng.choice(_NOUNS)}"


def _mixed(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits + "ABCDEFGHJKLMNPQRSTUVWXYZ"
    length = rng.randint(7, 10)
    while True:
        candidate = "".join(rng.choices(alphabet, k=length))
        if any(c.isdigit() for c in candidate) and any(c.isalpha() for c in candidate):
            return candidate


def _hexish(rng: random.Random) -> str:
    return "".join(rng.choices(_HEX, k=rng.randint(7, 9)))


def _grouped(rng: random.Random) -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "-".join(
        "".join(rng.choices(alphabet, k=rng.randint(2, 4))) for _ in range(3)
    )


def _timestampish(rng: random.Random) -> str:
    return f"{rng.choice(_MONTHS)}{rng.randint(1, 28)}-{rng.randint(100, 9999)}"


def _pronounceable(rng: random.Random) -> str:
    length = rng.randint(6, 8)
    parts = [
        rng.choice(_CONSONANTS) + rng.choice(_VOWELS) + rng.choice(_CONSONANTS)
        for _ in range(max(2, length // 3))
    ]
    return f"{''.join(parts)}{rng.randint(10, 99)}"


def _leet(rng: random.Random) -> str:
    base = rng.choice(("hacker", "program", "coder", "system", "network", "packet", "kernel"))
    out = "".join(
        _LEET_MAP[ch] if ch in _LEET_MAP and rng.random() < 0.5 else ch for ch in base
    )
    return f"{out}{rng.randint(10, 99)}"


def _camel(rng: random.Random) -> str:
    return f"{rng.choice(_ADJECTIVES)}{rng.choice(_NOUNS).capitalize()}"


#: name -> generator. Adding a format here is the only change needed; the label
#: shown to users is carried alongside the value, never re-derived.
FORMATS: dict[str, Callable[[random.Random], str]] = {
    "word+number": _word_number,
    "adjective+noun": _adjective_noun,
    "mixed": _mixed,
    "hex": _hexish,
    "grouped": _grouped,
    "timestamp": _timestampish,
    "pronounceable": _pronounceable,
    "leet": _leet,
    "camel": _camel,
}

FORMAT_LABELS = {
    "word+number": "Word + Number",
    "adjective+noun": "Adjective + Noun",
    "mixed": "Mixed Characters",
    "hex": "Hexadecimal",
    "grouped": "Grouped",
    "timestamp": "Month + Day + Number",
    "pronounceable": "Pronounceable",
    "leet": "Leet Style",
    "camel": "Camel Case",
}


class AliasGenerator:
    """Generates aliases, optionally checking them against existing ones.

    ``rng`` is injectable so tests can be deterministic; ``taken`` lets the
    caller pass ``db.alias_owner`` (or nothing at all).
    """

    def __init__(
        self,
        *,
        rng: random.Random | None = None,
        taken: Callable[[str], bool] | None = None,
        max_attempts: int = 12,
        formats: Iterable[str] | None = None,
    ) -> None:
        self._rng = rng or random.Random()
        self._taken = taken or (lambda _name: False)
        self._max_attempts = max_attempts
        self._formats = list(formats or FORMATS)

    def generate(self, *, avoid: set[str] | None = None) -> GeneratedAlias:
        blocked = {normalize(name) for name in (avoid or set())}
        last: GeneratedAlias | None = None
        for _ in range(self._max_attempts):
            fmt = self._rng.choice(self._formats)
            candidate = FORMATS[fmt](self._rng).lower()
            last = GeneratedAlias(candidate, fmt)
            if not is_valid(candidate) or looks_degenerate(candidate):
                continue
            if candidate in blocked or self._taken(candidate):
                continue
            return last
        # Last resort: guarantee uniqueness with random digits rather than
        # pretending the pretty format worked.
        while True:
            candidate = f"{self._rng.choice(_NOUNS)}{self._rng.randint(100000, 999999)}"
            if candidate not in blocked and not self._taken(candidate):
                return GeneratedAlias(candidate, "word+number")


def detect_format(name: str) -> str:
    """Best-effort label for aliases we did not generate (e.g. legacy rows).

    Only used as a fallback when the stored ``format`` column is empty, because
    guessing is exactly what made the original misleading.
    """
    candidate = normalize(name)
    if "_" in candidate:
        return "Snake Case"
    if any(c.isupper() for c in name):
        return "Camel Case"
    if "-" in candidate:
        return "Timestamp / Grouped"
    if candidate.isalpha():
        return "Word"
    if re.fullmatch(r"[0-9a-f]+", candidate):
        return "Hexadecimal"
    if any(ch.isdigit() for ch in candidate) and any(ch.isalpha() for ch in candidate):
        return "Word + Number"
    return "Custom"


def label_for(name: str, stored: str | None) -> str:
    if stored:
        return FORMAT_LABELS.get(stored, stored)
    return detect_format(name)


def iter_formats() -> Iterator[str]:
    return iter(FORMATS)
