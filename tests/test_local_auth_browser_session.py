"""Signed browser-session contract for token-protected customer dashboards."""
from __future__ import annotations

from engraphis.local_auth import browser_session, browser_session_ok


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
