"""OTP extraction: the original returned an order number as the code."""

from __future__ import annotations

import textwrap

import pytest

from gmailbot.otp import OtpExtractor, extract_otp, find_candidates

# The exact body that made the original return '5691' (see evidence/).
ORIGINAL_FALSE_POSITIVE = textwrap.dedent(
    """
    Your order 5691 has shipped. It will arrive at 90210 on Thursday.
    Thanks for shopping with us. Total: $49.99
    """
)


@pytest.mark.parametrize(
    "body, expected",
    [
        ("Your verification code is 483920. It expires in 10 minutes.", "483920"),
        ("Your code: 1234", "1234"),
        ("OTP 918273 is valid for 5 minutes", "918273"),
        ("123456 is your code. Do not share it.", "123456"),
        ("Your WhatsApp code is 123-456", "123456"),
        ("Use code [77123] to sign in", "77123"),
        ("<html><body><p>Code: <b>480912</b></p></body></html>", "480912"),
        ("Login code: 000123", "000123"),
        ("Your one-time passcode is 5566", "5566"),
    ],
)
def test_real_codes_are_found(body, expected):
    assert extract_otp(body) == expected


@pytest.mark.parametrize(
    "body",
    [
        ORIGINAL_FALSE_POSITIVE,
        "Order #12345 total $56.78",
        "Your parcel 90210 was delivered",
        "Call us on 555-123-4567",
        "Invoice 2026 due date 12/03",
        "© 2026 Acme Corp. All rights reserved.",
        "Tracking: 1Z999AA10123456784",
        "You have 12 points",
        "No numbers here at all",
        "",
    ],
)
def test_no_false_positives(body):
    """Returning a wrong code burns the user's attempt; None is better."""
    assert extract_otp(body) is None


def test_labelled_code_beats_bare_number():
    body = "Your order 123456 shipped. Your verification code is 778899."
    assert extract_otp(body) == "778899"


def test_candidates_are_ranked():
    candidates = find_candidates("Order 12345. Your code is 987654.")
    assert [c.code for c in candidates][0] == "987654"
    assert candidates[0].score > candidates[-1].score


def test_extractor_uses_ai_fallback_only_when_regex_fails():
    calls: list[str] = []

    def lookup(body: str) -> str | None:
        calls.append(body)
        return "424242"

    extractor = OtpExtractor(ai_lookup=lookup)
    assert extractor.extract("Your code is 111111") == "111111"
    assert calls == [], "regex hit must not call the network"

    # With a fallback that *does* return a code, the fallback result is used.
    assert extractor.extract("nothing numeric here") == "424242"
    assert calls == ["nothing numeric here"]


def test_ai_fallback_rejects_non_codes():
    extractor = OtpExtractor(ai_lookup=lambda body: "I cannot help with that")
    assert extractor.extract("nothing here") is None


def test_ai_fallback_errors_are_swallowed():
    def boom(body: str) -> str | None:
        raise RuntimeError("network down")

    assert OtpExtractor(ai_lookup=boom).extract("nothing here") is None
