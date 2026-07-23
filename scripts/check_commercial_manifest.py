"""Fail CI when product code, deployment docs, or website claims drift from GA truth."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "engraphis" / "commercial_manifest.json"
sys.path.insert(0, str(ROOT))


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _checkout_mapping(manifest: dict, errors: list[str]) -> dict:
    """Validate server-owned checkout targets and return (plan, interval) -> URL."""
    mapping = {}
    checkout_urls = set()
    plans = manifest.get("plans") if isinstance(manifest, dict) else None
    if not isinstance(plans, dict):
        _fail(errors, "manifest plans must be an object")
        return mapping
    for plan in ("pro", "team"):
        plan_data = plans.get(plan)
        products = plan_data.get("products") if isinstance(plan_data, dict) else None
        if not isinstance(products, dict) or set(products) != {"monthly", "annual"}:
            _fail(errors, "%s products must contain exactly monthly and annual" % plan)
            continue
        for interval in ("monthly", "annual"):
            product = products[interval]
            if not isinstance(product, dict):
                _fail(errors, "%s %s product must be an object" % (plan, interval))
                continue
            if set(product) != {"provider", "checkout_url"}:
                _fail(
                    errors,
                    "%s %s product exposes unsupported provider data" % (plan, interval),
                )
                continue
            if product.get("provider") != "stripe":
                _fail(errors, "%s %s product is not controlled by Stripe" % (plan, interval))
                continue
            checkout_url = product.get("checkout_url")
            if not isinstance(checkout_url, str):
                _fail(errors, "%s %s checkout URL is missing" % (plan, interval))
                continue
            parsed = urlsplit(checkout_url)
            query = parse_qs(parsed.query)
            if (
                parsed.scheme != "https"
                or parsed.netloc != "api.engraphis.com"
                or parsed.path != "/account"
                or parsed.fragment != "billing"
                or query != {"plan": [plan], "interval": [interval]}
            ):
                _fail(
                    errors,
                    "%s %s checkout URL is not the canonical account portal" % (plan, interval),
                )
                continue
            if checkout_url in checkout_urls:
                _fail(errors, "checkout URL is duplicated: %s" % checkout_url)
            checkout_urls.add(checkout_url)
            mapping[(plan, interval)] = checkout_url
    return mapping


def _check_repository(manifest: dict, errors: list[str]) -> None:
    if manifest.get("schema") != "engraphis-commercial/v2":
        _fail(errors, "commercial manifest schema must be engraphis-commercial/v2")
    mapping = _checkout_mapping(manifest, errors)
    expected_billing = {
        "authority": "stripe",
        "new_subscriptions": "stripe",
        "legacy_providers": [],
        "checkout_mode": "authenticated_server_session",
        "portal_url": "https://api.engraphis.com/account#billing",
        "provider_price_ids_public": False,
    }
    if manifest.get("billing") != expected_billing:
        _fail(errors, "Stripe must be the sole launch billing authority")

    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    if not match or match.group(1) != manifest["version"]:
        _fail(errors, "pyproject version does not match the commercial manifest")

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    live_badge = (
        "[![PyPI version](https://img.shields.io/pypi/v/engraphis.svg)]"
        "(https://pypi.org/project/engraphis/)"
    )
    if live_badge not in readme:
        _fail(errors, "README must use the live PyPI version badge")
    if re.search(r"img\.shields\.io/badge/version-[^\s)]+", readme):
        _fail(errors, "README must not advertise an unpublished hard-coded version badge")

    template = json.loads(
        (ROOT / "deploy" / "railway-template.json").read_text(encoding="utf-8")
    )
    service = template.get("service", {})
    variables = template.get("variables", {})
    if service.get("healthcheck") != "/api/ready":
        _fail(errors, "Railway health check must target /api/ready")
    if service.get("volume", {}).get("mount_path") != "/data":
        _fail(errors, "Railway template must mount a persistent /data volume")
    required_values = {
        "ENGRAPHIS_SERVICE_MODE": "customer",
        "ENGRAPHIS_CLOUD_CONTROL_URL": manifest["control_plane"],
    }
    for name, expected in required_values.items():
        if variables.get(name, {}).get("value") != expected:
            _fail(errors, "Railway variable %s does not match manifest" % name)
    local_api = variables.get("ENGRAPHIS_API_TOKEN", {})
    if (
        not local_api.get("required")
        or not local_api.get("secret")
        or local_api.get("value") != "${{ secret(48) }}"
    ):
        _fail(errors, "Railway template must generate a secret local API token")

    from engraphis.commercial import BILLING_AUTHORITY, expected_checkout_targets

    expected = expected_checkout_targets()
    if BILLING_AUTHORITY != "stripe":
        _fail(errors, "public billing authority drifted from Stripe")
    if set(expected) != set(mapping) or any(
        expected[key].get("provider") != "stripe"
        or expected[key].get("checkout_url") != checkout_url
        for key, checkout_url in mapping.items()
        if key in expected
    ):
        _fail(errors, "commercial checkout catalog parser drifted from manifest")


def _check_website(manifest: dict, website: Path, errors: list[str]) -> None:
    files = [
        website / name
        for name in ("index.html", "product.html", "about.html", "contact.html")
    ]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        _fail(errors, "website files missing: " + ", ".join(missing))
        return
    website_manifest = website / "commercial_manifest.json"
    if not website_manifest.is_file() or (
        json.loads(website_manifest.read_text(encoding="utf-8")) != manifest
    ):
        _fail(errors, "website commercial manifest copy differs from canonical manifest")
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    public_text = "\n".join(
        path.read_text(encoding="utf-8") for path in website.rglob("*.html")
    )
    lower = public_text.lower()
    for unsupported in (
        "end-to-end encrypted",
        "no phone-home",
        "sso/rbac",
        "it never leaves your machine",
    ):
        if unsupported in lower:
            _fail(errors, "website advertises unsupported claim: %s" % unsupported)
    if re.search(r"\bsla\b", public_text, re.IGNORECASE):
        _fail(errors, "website advertises unsupported claim: SLA")
    if "polar" in lower:
        _fail(errors, "website advertises Polar as a launch billing authority")
    required = [
        "v" + manifest["version"],
        "%d-day" % manifest["trial"]["days"],
        "$%d <span>/ mo" % manifest["plans"]["pro"]["monthly_usd"],
        "$%d <span>/ seat / mo" % manifest["plans"]["team"]["monthly_usd"],
    ]
    for claim in required:
        if claim not in text:
            _fail(errors, "website is missing manifest claim: %s" % claim)
    for plan in ("pro", "team"):
        for interval, product in manifest["plans"][plan]["products"].items():
            if product["checkout_url"] not in text:
                _fail(errors, "website is missing %s %s checkout" % (plan, interval))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--website-root", type=Path)
    args = parser.parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    _check_repository(manifest, errors)
    if args.website_root:
        _check_website(manifest, args.website_root.resolve(), errors)
    if errors:
        for error in errors:
            print("commercial manifest check: " + error)
        return 1
    print("commercial manifest check: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
