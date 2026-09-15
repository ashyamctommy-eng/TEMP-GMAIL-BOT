"""HTML mail -> plain text.

The original stored raw HTML when a message had no ``text/plain`` part, so the
"body preview" users saw was ``<html><body><p>Code: ...`` including tracking
pixels -- and OTP/link regexes ran over markup. Reproduced in evidence/.

A tiny, dependency-free ``HTMLParser`` keeps the important part: anchor text is
kept together with its URL, so a "Verify your email" button still yields a link.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

_BLOCK_TAGS = {
    "p", "div", "br", "tr", "table", "li", "ul", "ol", "h1", "h2", "h3", "h4",
    "h5", "h6", "section", "article", "header", "footer", "blockquote", "pre",
}
_SKIP_TAGS = {"script", "style", "head", "title", "meta", "link", "noscript"}
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0
        self._link: list[str] = []
        self._link_href: str | None = None

    # -- tag handling
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self._link_href = html.unescape(href.strip())
                self._link = []
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag == "img":
            alt = (dict(attrs).get("alt") or "").strip()
            if alt:
                self.parts.append(f" {alt} ")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "a":
            text = " ".join("".join(self._link).split())
            href = self._link_href
            if href:
                # Keep an absolute-URL check; ignore javascript:/mailto: noise
                if text and href.lower().startswith(("http://", "https://")):
                    self.parts.append(f" {text} <{href}> ")
                elif href.lower().startswith(("http://", "https://")):
                    self.parts.append(f" <{href}> ")
                elif text:
                    self.parts.append(f" {text} ")
            self._link = []
            self._link_href = None
            return
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._link_href is not None:
            self._link.append(data)
        else:
            self.parts.append(data)


def html_to_text(markup: str) -> str:
    """Convert HTML to readable text with inline ``<url>`` annotations."""
    if not markup:
        return ""
    parser = _TextExtractor()
    try:
        parser.feed(markup)
        parser.close()
    except Exception:  # malformed markup must never break mail parsing
        pass
    text = html.unescape("".join(parser.parts))
    text = _WS_RE.sub(" ", text)
    text = _BLANK_RE.sub("\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def strip_html(markup: str) -> str:
    """Text only, no URL annotations (for OTP matching)."""
    return re.sub(r"<[^>]+>", " ", html.unescape(markup or ""))
