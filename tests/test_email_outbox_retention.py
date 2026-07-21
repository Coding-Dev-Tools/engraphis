"""Regression (2026-07-20 audit): the outbox must not retain the rendered email body
(raw license keys / reset+invite links) after a message is handed to the provider; a
FAILED message keeps its body so an operator requeue can still retry it."""
from engraphis import email_outbox


def test_body_and_reply_to_cleared_after_successful_send():
    mid = email_outbox.enqueue(
        "license", "buyer@x.co", "Your key",
        "key ENGR1.secretpayload.sig reset https://x/#reset_token=abc",
        reply_to="support@x.co", idempotency_key="k-ret-1")
    assert email_outbox.deliver_now(
        mid, lambda to, subj, body, reply, mid_: ("resend", "pm_1"))
    conn = email_outbox._connect()
    try:
        row = conn.execute("SELECT status,text_body,reply_to FROM email_outbox WHERE id=?",
                           (mid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] in ("sent", "delivered", "bounced", "complained")
    assert row["text_body"] == "" and row["reply_to"] is None


def test_failed_message_retains_body_for_retry():
    mid = email_outbox.enqueue("license", "b@x.co", "s", "important ENGR1.k.k body",
                               idempotency_key="k-ret-2", max_attempts=1)

    def boom(to, subj, body, reply, mid_):
        raise RuntimeError("provider down")

    try:
        email_outbox.deliver_now(mid, boom)
    except RuntimeError:
        pass
    conn = email_outbox._connect()
    try:
        row = conn.execute("SELECT status,text_body FROM email_outbox WHERE id=?",
                           (mid,)).fetchone()
    finally:
        conn.close()
    assert row["status"] == "failed" and "ENGR1" in row["text_body"]
