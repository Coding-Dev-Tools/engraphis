"""Public customer-node boundary and secret-redaction regressions."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from engraphis.logging_setup import JsonFormatter


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_json_logging_redacts_message_extras_and_traceback_values():
    # Construct deliberately fake fixture values at runtime. Keeping credential-shaped
    # strings out of committed source avoids false positives in secret scanners while
    # preserving coverage for the recognizer's complete wire shape.
    refresh = "engr_rt_" + "ABCDEFGHIJKLMNOP" + "QRSTUV"
    api_key = "sk_" + "abcdefghijklmnop" + "qrstuvwxyz"
    bearer = "Bearer " + "abcdefghijklmn" + "opqrstuvwxyz"
    try:
        raise RuntimeError("connect failed: " + bearer)
    except RuntimeError:
        record = logging.LogRecord(
            "engraphis.customer", logging.ERROR, __file__, 1,
            "refresh_credential=" + refresh, (),
            __import__("sys").exc_info(),
        )
    record.refresh_credential = refresh
    record.provider = {
        "api_key": api_key,
        "nested": ["Authorization: " + bearer],
    }

    payload = json.loads(JsonFormatter().format(record))
    encoded = json.dumps(payload)
    for raw in (refresh, api_key, bearer.removeprefix("Bearer ")):
        assert raw not in encoded
    assert payload["refresh_credential"] == "[redacted]"
    assert payload["provider"]["api_key"] == "[redacted]"
    assert "[redacted]" in payload["provider"]["nested"][0]
    assert "[redacted]" in payload["exc_info"]


def test_public_customer_node_has_no_stripe_webhook_or_signing_authority():
    """Stripe event verification and dedupe stay in the private billing service."""
    forbidden_paths = (
        "engraphis/stripe_webhooks.py",
        "engraphis/webhooks.py",
        "scripts/stripe_webhook.py",
    )
    assert all(not (ROOT / path).exists() for path in forbidden_paths)
    runtime = "\n".join(
        path.read_text(encoding="utf-8")
        for source_root in (ROOT / "engraphis", ROOT / "scripts")
        for path in source_root.rglob("*.py")
    ).lower()
    for authority in ("stripe.webhook", "stripe-signature", "webhook_secret"):
        assert authority not in runtime


def test_customer_railway_contract_uses_public_readiness_and_secret_token_only():
    template = json.loads((ROOT / "deploy" / "railway-template.json").read_text(encoding="utf-8"))
    deploy = json.loads((ROOT / "railway.json").read_text(encoding="utf-8"))
    variables = template["variables"]

    assert deploy["deploy"]["healthcheckPath"] == "/api/ready"
    assert deploy["deploy"]["healthcheckTimeout"] >= 300
    assert template["service"]["healthcheck"] == "/api/ready"
    assert template["service"]["volume"]["mount_path"] == "/data"
    assert variables["ENGRAPHIS_SERVICE_MODE"]["value"] == "customer"
    assert variables["ENGRAPHIS_API_TOKEN"] == {
        "value": "${{ secret(48) }}",
        "secret": True,
        "prompt": "Railway generates this bearer for the single-user customer node. Keep it private.",
        "required": True,
    }
    for unavailable in (
        "ENGRAPHIS_STRIPE_WEBHOOK_SECRET",
        "ENGRAPHIS_LICENSE_SIGNING_KEY",
        "ENGRAPHIS_TEAM_MODE",
    ):
        assert unavailable not in variables
