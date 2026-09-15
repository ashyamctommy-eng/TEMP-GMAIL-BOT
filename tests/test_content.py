"""Links, aliases and rendering."""

from __future__ import annotations

import random

import pytest

from gmailbot import callbacks as cb
from gmailbot import formatting as fmt
from gmailbot.aliases import (
    FORMATS,
    MAX_LENGTH,
    AliasGenerator,
    label_for,
    looks_degenerate,
    validation_error,
)
from gmailbot.links import extract_links, extract_link_urls
from gmailbot.models import Alias, Message, utcnow

# ------------------------------------------------------------------- links
def test_html_entities_are_decoded():
    body = 'Click <a href="https://acme.test/v?token=1&amp;user=2">here</a>'
    urls = extract_link_urls(body)
    assert urls == ["https://acme.test/v?token=1&user=2"]


def test_redirect_wrappers_are_unwrapped():
    body = "https://t.acme.test/c/abc?url=https%3A%2F%2Fapp.acme.test%2Fverify%3Fcode%3D9"
    assert "https://app.acme.test/verify?code=9" in extract_link_urls(body)


def test_urls_are_never_truncated():
    url = "https://accounts.example.com/verify?token=" + "a" * 120
    links = extract_links(url)
    assert links[0].url == url


def test_tracking_and_legal_links_are_filtered():
    body = """
    <img src="https://cdn.acme.test/pixel.gif">
    https://acme.test/unsubscribe?u=1
    https://facebook.com/acme
    https://acme.test/privacy-policy
    https://acme.test/verify?token=abc
    """
    urls = extract_link_urls(body)
    assert urls[0].startswith("https://acme.test/verify")
    assert len(urls) == 1


def test_verification_links_rank_first():
    body = """
    https://acme.test/blog/how-we-work
    https://acme.test/confirm?token=xyz
    """
    links = extract_links(body)
    assert links[0].url.startswith("https://acme.test/confirm")


# ----------------------------------------------------------------- aliases
def test_every_format_is_reachable():
    generator = AliasGenerator(rng=random.Random(7))
    seen = {generator.generate().format for _ in range(4000)}
    assert seen == set(FORMATS)


def test_adjective_noun_is_not_rejected_by_its_own_rule():
    """The original's `^[a-z]+$` blocklist rejected this format 100% of the time."""
    generator = AliasGenerator(rng=random.Random(3), formats=["adjective+noun"])
    formats = {generator.generate().format for _ in range(200)}
    assert formats == {"adjective+noun"}


def test_generation_is_deterministic_with_a_seed():
    first = [AliasGenerator(rng=random.Random(9)).generate().name for _ in range(5)]
    second = [AliasGenerator(rng=random.Random(9)).generate().name for _ in range(5)]
    assert first == second


def test_generated_aliases_are_valid_and_unique_against_the_db():
    taken = {"bravetiger", "swifteagle"}
    generator = AliasGenerator(rng=random.Random(11), taken=taken.__contains__)
    names = {generator.generate().name for _ in range(500)}
    assert not (names & taken)
    assert all(validation_error(name) is None for name in names)


def test_avoid_list_is_honoured():
    generator = AliasGenerator(rng=random.Random(5))
    generated = generator.generate().name
    again = generator.generate(avoid={generated}).name
    assert again != generated


@pytest.mark.parametrize(
    "name, ok",
    [
        ("tiger123", True),
        ("a.b_c-d", True),
        ("swift_tiger", True),
        ("hi", False),  # too short
        ("x" * (MAX_LENGTH + 1), False),
        ("has space", False),
        ("emoji😀", False),
        ("-leading", False),
        ("trailing-", False),
        ("otp", False),  # reserved: collides with callback routing
        ("view", False),
        ("a..b", False),
    ],
)
def test_validation(name, ok):
    assert (validation_error(name) is None) is ok


def test_degenerate_filter_keeps_readable_aliases():
    assert not looks_degenerate("bravetiger")
    assert not looks_degenerate("swift-tiger-42")
    assert looks_degenerate("aaaaaaa")
    assert looks_degenerate("1234")


def test_label_falls_back_to_detection_for_legacy_rows():
    assert label_for("swift_tiger", None) == "Snake Case"
    assert label_for("swift_tiger", "adjective+noun") == "Adjective + Noun"


# -------------------------------------------------------------- formatting
def test_user_content_is_escaped():
    message = Message(
        id=1,
        alias="tiger",
        subject="<b>Injected</b> & <i>bold</i>",
        body="<script>alert(1)</script>",
        received_at=utcnow(),
        seen=False,
        otp="123456",
    )
    text, _ = fmt.otp_digest([message])
    assert "<b>Injected</b>" not in text
    assert "&lt;b&gt;Injected&lt;/b&gt;" in text
    assert "<script>" not in text
    assert "<code>123456</code>" in text


def test_links_keep_the_full_href_with_a_short_label():
    url = "https://accounts.example.com/verify?token=" + "b" * 100
    message = Message(
        id=2,
        alias="tiger",
        subject="Verify",
        body="",
        received_at=utcnow(),
        seen=False,
        links=[url],
    )
    text, markup = fmt.otp_digest([message])
    assert f'href="{url}"' in text
    assert "accounts.example.com" in text
    assert "…" not in text.split('href="')[1].split('"')[0]  # never truncated
    flat = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "s:2:link" in flat


def test_clamp_balances_tags_and_respects_the_limit():
    text = "<b>" + "x" * 5000
    clamped = fmt.clamp(text)
    assert len(clamped) <= fmt.TELEGRAM_TEXT_LIMIT
    assert clamped.endswith("</b>")


def test_clamp_never_cuts_inside_a_tag():
    text = "<pre>" + "y" * 5000 + "</pre>"
    clamped = fmt.clamp(text)
    assert "<pre" in clamped and "</pre>" in clamped
    assert not clamped.endswith("<")


def test_duration_wording():
    assert fmt.duration(3600) == "1 hour"
    assert fmt.duration(7200) == "2 hours"
    assert fmt.duration(900) == "15 minutes"
    assert fmt.duration(45) == "45 seconds"


def test_alias_list_shows_deleted_aliases_as_restorable(config):
    aliases = [
        Alias(name="live", active=True, created_at=utcnow()),
        Alias(name="gone", active=False, created_at=utcnow()),
    ]
    text, markup = fmt.alias_list(config, aliases)
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert any(item.startswith("v:live") for item in data)
    assert "r:gone" in data
    assert "⛔" in text


def test_feedback_forwarding_escapes_body():
    text = fmt.feedback_forward(
        header_lines=["👤 someone"], feedback_id=3, body="<b>fake admin header</b>"
    )
    assert "&lt;b&gt;fake admin header&lt;/b&gt;" in text
    assert "<b>fake admin header</b>" not in text


# -------------------------------------------------------------- callbacks
def test_callback_round_trip():
    data = cb.view_alias("swift_tiger_12", 2)
    assert cb.parse_view(data) == ("swift_tiger_12", 2)
    action, params = cb.parse(cb.reveal_secret(12, "link"))
    assert action == "s" and params == ["12", "link"]


def test_callback_builders_stay_within_the_64_byte_limit():
    long_alias = "a" * MAX_LENGTH
    builders = [
        cb.view_alias(long_alias, 99),
        cb.delete_alias(long_alias),
        cb.restore_alias(long_alias),
        cb.confirm_create(long_alias),
        cb.otp_page(99),
        cb.reveal_secret(999999, "link"),
    ]
    for data in builders:
        assert len(data.encode()) <= cb.MAX_CALLBACK_BYTES


def test_malformed_callback_data_is_survivable():
    assert cb.parse("") == ("", [])
    assert cb.parse("x" * 200) == ("", [])
    assert cb.parse_view("garbage") == (None, 0)
    assert cb.parse_view("v:") == (None, 0)
