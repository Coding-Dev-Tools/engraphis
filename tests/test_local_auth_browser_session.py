"""Signed browser-session and one-time sign-in link contracts."""
from __future__ import annotations

import pytest

from engraphis.local_auth import (
    browser_handoff,
    browser_handoff_ok,
    browser_session,
    browser_session_ok,
)


def test_browser_session_is_signed_bounded_and_token_specific() -> None:
    token = "deployment-token-with-enough-entropy"
    session = browser_session(token, now=1_000)

    assert token not in session
    assert browser_session_ok(session, token, now=1_100)
    assert not browser_session_ok(session, "another-token", now=1_100)
    assert not browser_session_ok(session, token, now=1_000 + 12 * 60 * 60 + 1)


def test_browser_session_rejects_tampering_and_far_future_timestamps() -> None:
    token = "deployment-token-with-enough-entropy"
    session = browser_session(token, now=1_000)
    prefix, issued, signature = session.split(".")

    assert not browser_session_ok(
        "%s.%s.%s" % (prefix, issued, signature[:-1] + "x"),
        token,
        now=1_100,
    )
    assert not browser_session_ok(browser_session(token, now=2_000), token, now=1_000)


def test_sign_in_codes_are_single_use_material_bound_to_the_process() -> None:
    token = "deployment-token-with-enough-entropy"
    epoch = b"process-epoch-one"

    code, expires_at = browser_handoff(token, epoch, now=1_000, ttl=600)
    assert token not in code
    assert expires_at == 1_600
    assert browser_handoff_ok(code, token, epoch, now=1_100) == (code.split(".")[2], 1_600)

    # A code is useless to any other token or after a server restart (new epoch).
    assert browser_handoff_ok(code, "another-token", epoch, now=1_100) is None
    assert browser_handoff_ok(code, token, b"process-epoch-two", now=1_100) is None
    # It expires, and never survives tampering with its payload.
    assert browser_handoff_ok(code, token, epoch, now=1_601) is None
    parts = code.split(".")
    assert browser_handoff_ok(
        ".".join(parts[:2] + [parts[2] + "x", parts[3]]), token, epoch, now=1_100,
    ) is None
    assert browser_handoff_ok(
        ".".join(parts[:3] + [parts[3][:-1] + "x"]), token, epoch, now=1_100,
    ) is None


def test_sign_in_codes_require_a_configured_token() -> None:
    with pytest.raises(ValueError):
        browser_handoff("", b"epoch", now=1_000)
    with pytest.raises(ValueError):
        browser_handoff("token", b"", now=1_000)
    assert browser_handoff_ok("egh1.1.a.b", "", b"epoch", now=0) is None
