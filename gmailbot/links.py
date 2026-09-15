"""Verification-link extraction.

Fixes over the original:

* HTML entities are decoded (``&amp;`` -> ``&``); the original emitted URLs that
  failed to load whenever a link carried two query parameters.
* Redirect wrappers are unwrapped (``?url=https%3A%2F%2F...``), which is how
  most marketing senders wrap verification links.
* Nothing is truncated. The original rendered ``[Link 1]({link[:50]}...)`` -- a
  *broken* URL inside a Markdown link -- and the "Copy Link" button copied a
  30-character prefix of the URL.
* Tracking pixels, social, legal and unsubscribe URLs are filtered out with
  word boundaries (the original used a bare ``re.search``, so ``privacy``
  matched every ``.../privacy-policy?utm_source=`` link *and* unrelated hosts).
"""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)}]+", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_ANGLE_RE = re.compile(r"<((?:https?)://[^>]+)>", re.IGNORECASE)

_VERIFY_WORDS = (
    "verify", "verification", "confirm", "confirmation", "activate", "activation",
    "auth", "authenticate", "login", "log-in", "signin", "sign-in", "magic",
    "token", "code", "otp", "click", "redirect", "continue", "proceed", "access",
    "reset", "recover", "unlock", "validate", "accept", "invitation", "join",
)
_BAD_HOSTS = (
    "facebook.com", "twitter.com", "x.com", "instagram.com", "youtube.com",
    "linkedin.com", "tiktok.com", "pinterest.com", "whatsapp.com", "telegram.org",
)
_BAD_PATHS = (
    "/unsubscribe", "/preferences", "/privacy", "/terms", "/legal", "/cookies",
    "/help", "/support", "/contact", "/about", "/abuse", "/optout", "/opt-out",
    "/pixel", "/open", "/track", "/beacon", "/images/", "/img/", "/assets/",
    "/static/", "/logo", "/banner", "/spacer",
)
_BAD_EXT = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".ico", ".css", ".js",
    ".woff", ".woff2", ".pdf", ".zip",
)
_REDIRECT_KEYS = ("url", "u", "q", "target", "redirect", "redirect_uri", "continue", "dest", "link")
_TOKEN_KEYS = (
    "token", "code", "otp", "key", "hash", "sig", "signature", "verify",
    "confirmation", "confirm", "auth", "id", "ticket", "secret",
)


@dataclass(frozen=True)
class Link:
    url: str
    score: int
    label: str = ""

    @property
    def host(self) -> str:
        return urlparse(self.url).netloc or ""

    def short_label(self, limit: int = 48) -> str:
        """Short *display* text. The URL itself is never truncated."""
        host = self.host
        if not host:
            return self.url[:limit]
        return host if len(host) <= limit else host[: limit - 1] + "…"


def _unwrap_redirect(url: str, *, depth: int = 2) -> str:
    """Follow ``?url=<encoded target>`` style wrappers."""
    current = url
    for _ in range(depth):
        try:
            parsed = urlparse(current)
        except ValueError:
            return current
        if not parsed.query:
            return current
        params = parse_qs(parsed.query)
        for key in _REDIRECT_KEYS:
            for value in params.get(key, []):
                candidate = unquote(value)
                if candidate.startswith(("http://", "https://")) and candidate != current:
                    logger.debug("unwrapped redirector %s -> %s", current, candidate)
                    current = candidate
                    break
            else:
                continue
            break
        else:
            return current
    return current


def _clean(url: str) -> str:
    cleaned = html.unescape(url.strip())
    cleaned = cleaned.rstrip('.,;:!?)]}"\'')
    # Strip a trailing ">" left over from angle-bracket markup.
    return cleaned.rstrip(">")


def _score(url: str) -> int:
    score = 0
    try:
        parsed = urlparse(url)
    except ValueError:
        return -100
    if parsed.scheme not in ("http", "https"):
        return -100
    host = parsed.netloc.lower()
    path = (parsed.path or "").lower()
    query = parsed.query.lower()
    haystack = f"{path}?{query}"

    if any(bad in host for bad in _BAD_HOSTS):
        score -= 50
    if any(bad in path for bad in _BAD_PATHS):
        score -= 50
    if path.endswith(_BAD_EXT):
        score -= 50

    if any(word in haystack for word in _VERIFY_WORDS):
        score += 6
    params = parse_qs(parsed.query)
    if any(key.lower() in _TOKEN_KEYS for key in params):
        score += 5
    if len(query) > 40:
        score += 1
    if not parsed.path or parsed.path == "/":
        score -= 3
    if "utm_" in query and len(haystack) < 80:
        score -= 1
    if "unsubscribe" in haystack:
        score -= 40
    return score


def extract_links(text: str, *, limit: int = 10, min_score: int = 1) -> list[Link]:
    """Verification links first, best score first, de-duplicated, never cut."""
    if not text:
        return []
    raw: list[str] = []
    raw.extend(_ANGLE_RE.findall(text))
    raw.extend(match.group(1) for match in _HREF_RE.finditer(text))
    raw.extend(_URL_RE.findall(text))

    seen: set[str] = set()
    links: list[Link] = []
    for candidate in raw:
        cleaned = _clean(candidate)
        if len(cleaned) < 12:
            continue
        unwrapped = _clean(_unwrap_redirect(cleaned))
        if unwrapped in seen or cleaned in seen:
            continue
        seen.add(cleaned)
        seen.add(unwrapped)
        score = _score(unwrapped)
        if score < min_score:
            continue
        links.append(Link(url=unwrapped, score=score))

    links.sort(key=lambda link: -link.score)
    return links[:limit]


def extract_link_urls(text: str, *, limit: int = 10) -> list[str]:
    return [link.url for link in extract_links(text, limit=limit)]
