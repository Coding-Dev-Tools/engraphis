"""Regression (2026-07-20 audit): the outbox must not retain the rendered email body
(raw license keys / reset+invite links) after a message is handed to the provider; a
FAILED message keeps its body so an operator requeue can still retry it."""
import threading

import pytest

from engraphis import email_outbox


def test_fulfillment_retention_claim_hashes_oversize_or_unsafe_ids_stably():
    for fulfillment_id in ("x" * 300, "order:unsafe\nidentifier"):
        first = email_outbox.fulfillment_retention_claim(fulfillment_id)
        assert first == email_outbox.fulfillment_retention_claim(fulfillment_id)
        assert first.startswith("ful:sha256:")
        assert len(first) <= email_outbox.MAX_IDEMPOTENCY_KEY_CHARS
        assert not any(ord(char) < 32 or ord(char) == 127 for char in first)


def test_body_and_reply_to_cleared_after_successful_send():
    mid = email_outbox.enqueue(
        "license", "buyer@x.co", "Your key",
        "Your key:\n\n    ENGR1.secretpayload.sig\n\n"
        "reset https://x/#reset_token=abc",
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


def test_real_license_body_retained_only_until_fulfillment_commit(monkeypatch, tmp_path):
    from engraphis import billing
    from engraphis.inspector.webhooks import _license_email_text

    monkeypatch.setenv("ENGRAPHIS_RELAY_DB", str(tmp_path / "relay.db"))
    monkeypatch.setenv("ENGRAPHIS_WEBHOOK_STATE", str(tmp_path / "webhooks.db"))
    claim = "ful:order:retention-test"
    body = _license_email_text("ENGR1.payload.signature", "Pro")
    mid = email_outbox.enqueue(
        "purchase_license", "buyer@x.co", "Your key", body,
        idempotency_key="purchase-license:retention-test",
        retention_claim=claim)
    assert email_outbox.deliver_now(
        mid, lambda *_args: ("resend", "pm_retention"))
    conn = email_outbox._connect()
    try:
        assert "ENGR1.payload.signature" in conn.execute(
            "SELECT text_body FROM email_outbox WHERE id=?", (mid,)).fetchone()[0]
    finally:
        conn.close()
    assert billing.claim_webhook(claim) == "claimed"
    billing.complete_webhook(claim)
    assert email_outbox.redact_finalized_retention_claims() == 1
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT text_body,retention_claim FROM email_outbox WHERE id=?", (mid,)
        ).fetchone()
        assert row["text_body"] == "" and row["retention_claim"] == ""
    finally:
        conn.close()


def test_existing_blank_retention_column_is_backfilled_after_restart():
    mid = email_outbox.enqueue(
        "purchase_license", "buyer@x.co", "Your key", "ENGR1.payload.signature",
        idempotency_key="purchase-license:legacy-order")

    # The column already exists, modeling a host death after ALTER TABLE but before
    # the migration UPDATE. A later connection must still perform the backfill.
    conn = email_outbox._connect()
    try:
        row = conn.execute(
            "SELECT retention_claim FROM email_outbox WHERE id=?", (mid,)).fetchone()
        assert row["retention_claim"] == "ful:order:legacy-order"
    finally:
        conn.close()


def test_registry_journal_reconciles_after_outbox_copy_was_already_redacted(
        monkeypatch):
    from engraphis import billing
    from engraphis.inspector import license_registry

    claim = "ful:order:journal-cleanup"
    mid = email_outbox.enqueue(
        "purchase_license", "buyer@x.co", "Your key", "ENGR1.payload.signature",
        idempotency_key="purchase-license:journal-cleanup",
        retention_claim=claim)
    assert email_outbox.deliver_now(
        mid, lambda *_args: ("resend", "pm_journal_cleanup"))
    conn = license_registry.connect()
    try:
        conn.execute(
            "INSERT INTO license_fulfillment_keys(retention_claim,license_key,created_at) "
            "VALUES (?,?,?)", (claim, "ENGR1.raw.recovery", 1.0))
        conn.commit()
    finally:
        conn.close()
    assert billing.claim_webhook(claim) == "claimed"
    billing.complete_webhook(claim)

    real_redact = license_registry.redact_fulfillment_key
    monkeypatch.setattr(
        license_registry, "redact_fulfillment_key",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated journal cleanup outage")))
    with pytest.raises(RuntimeError, match="could not reconcile"):
        email_outbox.redact_finalized_retention_claims()
    conn = email_outbox._connect()
    try:
        assert conn.execute(
            "SELECT text_body FROM email_outbox WHERE id=?", (mid,)).fetchone()[0] == ""
        assert conn.execute(
            "SELECT COUNT(*) FROM license_fulfillment_keys WHERE retention_claim=?",
            (claim,)).fetchone()[0] == 1
    finally:
        conn.close()

    # On the next sweep there is no outbox claim left. The registry journal itself
    # must independently nominate the claim for deletion.
    monkeypatch.setattr(license_registry, "redact_fulfillment_key", real_redact)
    assert email_outbox.redact_finalized_retention_claims() == 1
    conn = license_registry.connect()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM license_fulfillment_keys WHERE retention_claim=?",
            (claim,)).fetchone()[0] == 0
    finally:
        conn.close()


def test_registry_reconciliation_pages_past_unfulfilled_journals():
    from engraphis import billing
    from engraphis.inspector import license_registry

    claims = ["ful:bulk:%04d" % index for index in range(1001)]
    conn = license_registry.connect()
    try:
        conn.executemany(
            "INSERT INTO license_fulfillment_keys(retention_claim,license_key,created_at) "
            "VALUES (?,?,?)",
            ((claim, "ENGR1.raw.recovery", float(index))
             for index, claim in enumerate(claims)))
        conn.commit()
    finally:
        conn.close()
    assert billing.claim_webhook(claims[-1]) == "claimed"
    billing.complete_webhook(claims[-1])

    assert email_outbox.redact_finalized_retention_claims() == 1
    conn = license_registry.connect()
    try:
        assert conn.execute(
            "SELECT 1 FROM license_fulfillment_keys WHERE retention_claim=?",
            (claims[0],)).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM license_fulfillment_keys WHERE retention_claim=?",
            (claims[-1],)).fetchone() is None
    finally:
        conn.close()


def test_idempotency_key_collision_fails_closed_across_message_identity():
    email_outbox.enqueue(
        "invitation", "first@example.com", "Invite", "Private body",
        idempotency_key="shared-business-id")

    with pytest.raises(ValueError, match="another message kind or recipient"):
        email_outbox.enqueue(
            "reset", "first@example.com", "Reset", "Different body",
            idempotency_key="shared-business-id")
    with pytest.raises(ValueError, match="another message kind or recipient"):
        email_outbox.enqueue(
            "invitation", "second@example.com", "Invite", "Different body",
            idempotency_key="shared-business-id")


def test_concurrent_idempotency_race_attaches_missing_retention_claim(monkeypatch):
    real_connect = email_outbox._connect
    both_prechecked = threading.Barrier(2)
    blank_inserted = threading.Event()

    class CoordinatedConnection:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, parameters=()):
            if sql.startswith("SELECT id,kind,recipient,retention_claim"):
                result = self.inner.execute(sql, parameters)
                row = result.fetchone()
                if row is None:
                    both_prechecked.wait(timeout=5)
                    # Return a cursor already exhausted by our coordination read.
                    return result
                # The post-IntegrityError lookup must still yield its winner row.
                return self.inner.execute(sql, parameters)
            if sql.startswith("INSERT INTO email_outbox"):
                if threading.current_thread().name == "with-retention":
                    assert blank_inserted.wait(timeout=5)
                result = self.inner.execute(sql, parameters)
                if threading.current_thread().name == "without-retention":
                    blank_inserted.set()
                return result
            return self.inner.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    monkeypatch.setattr(
        email_outbox, "_connect",
        lambda: CoordinatedConnection(real_connect()))
    results = []
    errors = []

    def enqueue_one(claim):
        try:
            results.append(email_outbox.enqueue(
                "purchase_license", "buyer@x.co", "Your key",
                "ENGR1.payload.signature", idempotency_key="purchase-license:race",
                retention_claim=claim))
        except BaseException as exc:
            errors.append(exc)

    without = threading.Thread(
        target=enqueue_one, args=("",), name="without-retention")
    with_claim = threading.Thread(
        target=enqueue_one, args=("ful:order:race",), name="with-retention")
    without.start()
    with_claim.start()
    without.join(timeout=10)
    with_claim.join(timeout=10)

    assert not without.is_alive() and not with_claim.is_alive()
    assert errors == []
    assert len(results) == 2 and len(set(results)) == 1
    conn = real_connect()
    try:
        row = conn.execute(
            "SELECT retention_claim FROM email_outbox WHERE id=?", (results[0],)
        ).fetchone()
        assert row["retention_claim"] == "ful:order:race"
    finally:
        conn.close()
