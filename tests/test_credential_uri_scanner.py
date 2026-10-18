"""Authenticated-URI span compatibility and bounded malformed-input scanning."""

import time

import pytest

from engraphis.core.secrets import (
    SecretDetectedError,
    _credential_uri_spans,
    redact_secrets,
    reject_secrets,
    secret_kind,
)


@pytest.mark.parametrize(("text", "redacted"), [
    ("smtp://user:p@host.invalid/path", "<redacted>host.invalid/path"),
    ("smtp://:p@host.invalid", "<redacted>host.invalid"),
    ("smtp://f'ix:p'ass@host.invalid", "<redacted>host.invalid"),
    ("smtp://user:p@part@host.invalid", "<redacted>host.invalid"),
    ("custom+mail-2.0://user:p@host.invalid", "<redacted>host.invalid"),
    ("İ://user:p@host.invalid", "<redacted>host.invalid"),
    ("ı://user:p@host.invalid", "<redacted>host.invalid"),
    ("ſ://user:p@host.invalid", "<redacted>host.invalid"),
    ("K://user:p@host.invalid", "<redacted>host.invalid"),
    ("pre_smtp://user:p@host.invalid", "pre_<redacted>host.invalid"),
    ("[smtp://user:p@host.invalid]", "[<redacted>host.invalid]"),
    ('"smtp://user:p@host.invalid"', '"<redacted>host.invalid"'),
    ("a:///smtp://user:p@host.invalid", "a:///<redacted>host.invalid"),
    ("a://b://c://user:p@host.invalid", "a://b://<redacted>host.invalid"),
    ("smtp://u:p@one.invalid\u2003https://:q@two.invalid",
     "<redacted>one.invalid\u2003<redacted>two.invalid"),
    ("smtp://u:p@one.invalid?next=https://u:q@two.invalid",
     "<redacted>one.invalid?next=<redacted>two.invalid"),
])
def test_exact_uri_redaction_and_capture_boundaries(text, redacted):
    assert list(_credential_uri_spans(text))
    assert secret_kind(text) == "credential-bearing connection URI"
    assert redact_secrets(text) == redacted
    assert redact_secrets(redacted) == redacted
    with pytest.raises(SecretDetectedError) as caught:
        reject_secrets([("content", text)])
    assert str(caught.value) == (
        "potential credential-bearing connection URI detected in content; "
        "redact it before storing memory"
    )


@pytest.mark.parametrize("text", [
    "1smtp://u:p@host.invalid", "+smtp://u:p@host.invalid",
    ".smtp://u:p@host.invalid", "-smtp://u:p@host.invalid",
    "a://smtp://u:p@host.invalid", "smtp:///u:p@host.invalid",
    "smtp://u:p\u2003@host.invalid", "smtp://u:p?@host.invalid",
    "smtp://u:p#@host.invalid", 'smtp://u:p"@host.invalid',
    "smtp://u:@host.invalid", "smtp://u@host.invalid",
    "smtp://u:<redacted>@host.invalid", "smtp://u:withheld@host.invalid",
])
def test_no_new_matches_inside_consumed_authorities_or_invalid_schemes(text):
    assert list(_credential_uri_spans(text)) == []
    assert secret_kind(text) is None
    assert redact_secrets(text) == text


@pytest.mark.parametrize("separator", [" ", "\t", "\n", "\r", "\v", "\f", "\x85", "\u00a0", "\u2028"])
def test_unicode_whitespace_ends_authority_before_userinfo(separator):
    text = "smtp://u:p" + separator + "@host.invalid"
    assert list(_credential_uri_spans(text)) == []


@pytest.mark.parametrize("kind", ["plain", "near_miss", "repeated", "nested"])
def test_long_no_match_inputs_have_a_bounded_scan(kind):
    content = {
        "plain": "a" * 1_000_000,
        "near_miss": "a" * 1_000_000 + ":/",
        "repeated": "a://" * 100_000,
        "nested": "a://" + "a" * 1_000_000 + "://",
    }[kind]
    started = time.process_time()
    assert list(_credential_uri_spans(content)) == []
    assert time.process_time() - started < 3


def test_long_scheme_keeps_the_full_original_redaction_span():
    text = "prefix " + "custom-" * 20_000 + "://:p@host.invalid/path suffix"
    assert list(_credential_uri_spans(text)) == [(7, len(text) - len("host.invalid/path suffix"))]
    assert redact_secrets(text) == "prefix <redacted>host.invalid/path suffix"
