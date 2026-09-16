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

import fnmatch
import html
import logging
import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger(__name__)

_URL_RE = re.compile(r"https?://[^\s<>\"'`\]\)}]+", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']?([^"'\s>]+)""", re.IGNORECASE)
_ANGLE_RE = re.compile(r"<((?:https?)://[^>]+)>", re.IGNORECASE)

#: "click" is deliberately absent: it made Mailchimp-style click trackers
#: (``/ls/click?upn=…``) outrank the actual magic link they wrap.
_VERIFY_WORDS = (
    "verify", "verification", "confirm", "confirmation", "activate", "activation",
    "auth", "authenticate", "login", "log-in", "signin", "sign-in", "magic",
    "token", "code", "otp", "redirect", "continue", "proceed", "access",
    "reset", "recover", "unlock", "validate", "accept", "invitation", "join",
)

#: Links whose whole purpose is to be clicked through to somewhere else. The
#: destination is the link the user wants, so these are ranked last and hidden
#: when a real link is present.
_TRACKER_HOST_RE = re.compile(
    r"(?:^|\.)url\d+\.[a-z0-9.-]+$"            # url8792.mail.anthropic.com
    r"|(?:^|\.)(?:list-manage\.com|mailchi\.mp|sendgrid\.net|mandrillapp\.com"
    r"|mailgun\.org|sparkpostmail\.com|amazonses\.com|postmarkapp\.com"
    r"|hubspotlinks\.com|hubspotemail\.net|customeriomail\.com|braze\.com"
    r"|createsend\.com|cmail\d*\.[a-z0-9.-]+)$",
    re.IGNORECASE,
)
_TRACKER_PATH_RE = re.compile(
    r"^/(?:ls/click|e/c/|track/click|t/c/|wf/click|click/|cl0/)", re.IGNORECASE
)
_TRACKER_PARAMS = ("upn", "mkt_tok", "_hsenc", "_hsmi", "vero_id", "sc_cid")
#: "magic link" style URLs are the single most valuable thing in these emails.
_MAGIC_WORDS = ("magic", "passwordless", "signin", "sign-in", "login", "log-in")
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
    tracker: bool = False

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


def learn_host_pattern(url: str) -> str | None:
    """Generalise a host into a reusable glob: url8792.mail.x -> url*.mail.x.

    Bulk senders rotate the digits per campaign, so learning the exact host
    would mean learning nothing the next time.
    """
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return None
    if not host:
        return None
    labels = host.split(".")
    generalised = [re.sub(r"\d+", "*", label) for label in labels]
    pattern = ".".join(generalised)
    return pattern if pattern != host or any("*" in label for label in generalised) else host


def _matches_learned(host: str, learned: Iterable[str]) -> str | None:
    for pattern in learned:
        if host == pattern or fnmatch.fnmatch(host, pattern):
            return pattern
    return None


def _is_tracker(url: str, learned: Iterable[str] = ()) -> bool:
    """Is ``url`` a click wrapper?

    Built-in rules are checked first, then any host pattern learned from the AI
    judge or added by the admin (see :func:`_matches_learned`).
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    host = parsed.netloc.lower()
    if _TRACKER_HOST_RE.search(host):
        return True
    if _TRACKER_PATH_RE.match(parsed.path or ""):
        return True
    params = {key.lower() for key in parse_qs(parsed.query)}
    if params & set(_TRACKER_PARAMS):
        return True
    return _matches_learned(host, learned) is not None


def _score(url: str, learned: Iterable[str] = ()) -> int:
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
    if any(word in haystack for word in _MAGIC_WORDS):
        score += 8
    # Magic links carry their token in the fragment.
    if parsed.fragment:
        score += 4
    if _is_tracker(url, learned):
        score -= 40
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


def extract_links(
    text: str,
    *,
    limit: int = 10,
    min_score: int = 1,
    learned: Iterable[str] = (),
) -> list[Link]:
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
        score = _score(unwrapped, learned)
        tracker = _is_tracker(unwrapped, learned)
        # A tracker scores far below the floor on purpose, but it still redirects
        # to the real destination: keep it as a last-resort candidate so a mail
        # whose only URL is wrapped is not left with nothing clickable.
        if score < min_score and not tracker:
            continue
        links.append(Link(url=unwrapped, score=score, tracker=tracker))

    links.sort(key=lambda link: (-link.score, link.url))
    real = [link for link in links if not link.tracker]
    # A click tracker is only worth showing when there is nothing better: the
    # destination it wraps is what the user actually needs.
    return (real or links)[:limit]


def extract_link_urls(text: str, *, limit: int = 10, learned: Iterable[str] = ()) -> list[str]:
    return [link.url for link in extract_links(text, limit=limit, learned=learned)]
