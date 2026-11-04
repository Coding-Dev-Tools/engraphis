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
CONTRACT_PATH = ROOT / "engraphis" / "entitlement_contract.json"
CONTRACT_SCHEMA = "engraphis-entitlement-contract/v1"
TEAM_ONLY_CAPABILITIES = frozenset({
    "hosted_multi_user_roles",
    "hosted_team_audit_export",
    "hosted_scoped_agent_tokens",
})
AUTOMATION_ALIASES = frozenset({"consolidation", "dreaming"})
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


def _read_contract(errors: list[str]) -> dict:
    """Load the checked-in entitlement mirror, reporting rather than raising."""

    try:
        contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        _fail(errors, "entitlement contract is missing or unreadable: %s" % exc)
        return {}
    if not isinstance(contract, dict) or contract.get("schema") != CONTRACT_SCHEMA:
        _fail(errors, "entitlement contract schema must be " + CONTRACT_SCHEMA)
        return contract if isinstance(contract, dict) else {}
    return contract


def _check_entitlement_contract(manifest: dict, errors: list[str]) -> dict:
    """Validate the single plan→feature mirror against the public commercial manifest.

    The client reads this file at runtime, so the release gate has to prove it is complete
    before a paid capability can silently disappear from a customer's dashboard.
    """

    contract = _read_contract(errors)
    if not contract:
        return contract
    plans = contract.get("plans")
    features = contract.get("features")
    if not isinstance(plans, dict) or set(plans) != {"free", "pro", "team"}:
        _fail(errors, "entitlement contract plans must name free, pro and team")
        return contract
    if not isinstance(features, dict) or not features:
        _fail(errors, "entitlement contract features must be a non-empty object")
        return contract

    for plan in ("free", "pro", "team"):
        names = plans.get(plan)
        if not isinstance(names, list) or len(names) != len(set(names)):
            _fail(errors, "entitlement contract plans.%s must be a unique list" % plan)
            continue
        for name in names:
            spec = features.get(name)
            if not isinstance(spec, dict) or spec.get("status") != "available":
                _fail(
                    errors,
                    "entitlement contract plans.%s names a feature that is not "
                    "available: %s" % (plan, name),
                )
                continue
            # ``plan`` is the minimum grant; Team inherits every Pro feature.
            declared_plan = spec.get("plan")
            allowed = declared_plan in ("pro", "team") if plan == "team" else declared_plan == plan
            if not allowed:
                _fail(
                    errors,
                    "entitlement contract plans.%s names %s, which declares plan %r"
                    % (plan, name, declared_plan),
                )
    if not set(plans["pro"]).issubset(set(plans["team"])):
        _fail(errors, "entitlement contract team plan must include every pro feature")

    for name, spec in features.items():
        if not isinstance(spec, dict):
            _fail(errors, "entitlement contract feature %s must be an object" % name)
            continue
        if spec.get("plan") not in {"free", "pro", "team"}:
            _fail(errors, "entitlement contract feature %s names an unknown plan" % name)
        if not isinstance(spec.get("label"), str) or not spec["label"].strip():
            _fail(errors, "entitlement contract feature %s needs a non-empty label" % name)
        if spec.get("status") not in {"available", "free", "declared"}:
            _fail(errors, "entitlement contract feature %s has an unknown status" % name)
        if spec.get("status") == "declared" and any(
            name in names for names in plans.values()
        ):
            _fail(errors, "entitlement contract declares but still grants %s" % name)
        if spec.get("status") == "available":
            if name not in plans.get(spec.get("plan"), ()):
                _fail(
                    errors,
                    "entitlement contract omits available feature %s from its plan" % name,
                )
            if spec.get("plan") == "pro" and name not in plans.get("team", ()):
                _fail(
                    errors,
                    "entitlement contract omits pro feature %s from the team plan" % name,
                )

    included = contract.get("included_services", {})
    if not isinstance(included, dict):
        _fail(errors, "entitlement contract included_services must be an object")
    else:
        for name, spec in included.items():
            if not isinstance(spec, dict):
                _fail(errors, "included service %s must be an object" % name)
                continue
            if spec.get("plan") not in {"free", "pro", "team"}:
                _fail(errors, "included service %s names an unknown plan" % name)
            if not isinstance(spec.get("label"), str) or not spec["label"].strip():
                _fail(errors, "included service %s needs a non-empty label" % name)
            calls = spec.get("calls_per_person_per_day")
            if calls is not None and (
                not isinstance(calls, int) or isinstance(calls, bool) or calls <= 0
            ):
                _fail(
                    errors,
                    "included service %s calls_per_person_per_day must be positive"
                    % name,
                )

    # Local workspace export is free and documented free. It must never re-enter the paid
    # vocabulary, or a Pro/Team row starts advertising something the free tier owns.
    export = features.get("export")
    if not isinstance(export, dict) or export.get("plan") != "free" or export.get("status") != "free":
        _fail(errors, "entitlement contract must keep local export on the free plan")
    if "export" in plans.get("pro", ()) or "export" in plans.get("team", ()):
        _fail(errors, "entitlement contract must not advertise local export as paid")

    # Every hosted capability the manifest turns on must resolve deliberately.
    for name, enabled in (manifest.get("features") or {}).items():
        if not enabled or not name.startswith("hosted_") or name == "hosted_authorization":
            continue
        key = name.replace("hosted_opt_in_cloud_", "")
        spec = features.get(key)
        if not isinstance(spec, dict):
            _fail(errors, "manifest sells %s but the entitlement contract omits it" % name)
            continue
        expected = "team" if name in TEAM_ONLY_CAPABILITIES else "pro"
        if spec.get("plan") != expected:
            _fail(errors, "manifest capability %s must resolve to %s" % (name, expected))

    limits = contract.get("limits") or {}
    trial = manifest.get("trial") or {}
    if limits.get("trial_days_by_plan") != trial.get("days_by_plan"):
        _fail(errors, "entitlement contract trial days differ from the manifest")
    lifecycle = manifest.get("entitlement_lifecycle") or {}
    if limits.get("grace_hours") != lifecycle.get("max_grace_hours"):
        _fail(errors, "entitlement contract grace hours differ from the manifest")
    return contract


def _check_cloud_contract(cloud: dict, manifest: dict, errors: list[str]) -> None:
    """Compare the checked-in mirror with the private control plane's contract.

    The private service publishes only a secret-free contract (no price ids, no keys). When
    it names ``hosted_entitlements``, the plan→feature sets must match the client mirror
    exactly; a new server grant that never reached this repository is a release blocker,
    not something a paying customer discovers as a lock.
    """

    if not isinstance(cloud, dict):
        _fail(errors, "cloud contract must be an object")
        return
    # The private service has published both shapes: the original schema_version 1
    # contract and the later engraphis-public-offer/v1 document. Accept either, then
    # apply the same trial and entitlement-table comparisons to both.
    if not (
        cloud.get("schema_version") == 1
        or cloud.get("schema") == "engraphis-public-offer/v1"
    ):
        _fail(
            errors,
            "cloud contract schema must be schema_version 1 or "
            "engraphis-public-offer/v1",
        )
        return
    if (cloud.get("trial") or {}).get("days_by_plan") != (manifest.get("trial") or {}).get(
        "days_by_plan"
    ):
        _fail(errors, "cloud trial authority differs from the public per-plan contract")
    hosted = cloud.get("hosted_entitlements")
    if hosted is None:
        return
    if not isinstance(hosted, dict):
        _fail(errors, "cloud contract hosted_entitlements must be an object")
        return
    contract = _read_contract(errors)
    if not contract:
        return
    for plan in ("free", "pro", "team"):
        local = set((contract.get("plans") or {}).get(plan) or ()) - AUTOMATION_ALIASES
        remote = set(hosted.get(plan) or ())
        if local != remote:
            _fail(
                errors,
                "entitlement contract plans.%s differs from the private cloud contract: "
                "client=%s cloud=%s" % (plan, sorted(local), sorted(remote)),
            )


def _check_repository(manifest: dict, errors: list[str]) -> None:
    if manifest.get("schema") != "engraphis-commercial/v2":
        _fail(errors, "commercial manifest schema must be engraphis-commercial/v2")
    if manifest.get("account_portal") != "https://api.engraphis.com/account":
        _fail(errors, "account portal must be the canonical authenticated control-plane URL")
    if "managed_dashboard" in manifest:
        _fail(errors, "commercial manifest must not advertise a separate managed dashboard")
    mapping = _checkout_mapping(manifest, errors)
    expected_units = {"free": "installation", "pro": "owner", "team": "seat"}
    for plan, expected_unit in expected_units.items():
        plan_data = manifest.get("plans", {}).get(plan, {})
        if plan_data.get("billing_unit") != expected_unit:
            _fail(errors, "%s billing unit must be %s" % (plan, expected_unit))

    # GA pricing. The website branch of this check runs only when --website-root
    # is passed (release CI passes it when a website checkout is present) -- without
    # the flag published prices would be unguarded.
    expected_prices = {"free": (0, 0), "pro": (10, 100), "team": (20, 200)}
    for plan, (monthly, annual) in expected_prices.items():
        plan_data = manifest.get("plans", {}).get(plan, {})
        found = (plan_data.get("monthly_usd"), plan_data.get("annual_usd"))
        if any(not isinstance(value, int) or isinstance(value, bool) for value in found):
            _fail(errors, "%s prices must be whole USD integers" % plan)
            continue
        if found != (monthly, annual):
            _fail(
                errors,
                "%s price drifted from GA truth: expected %s, found %s"
                % (plan, (monthly, annual), found),
            )
        # A paid plan billed annually must stay a sane multiple of its monthly rate;
        # a transposed or mistyped digit would otherwise ship as a real discount.
        if monthly and not (10 * monthly <= annual <= 12 * monthly):
            _fail(errors, "%s annual price is not a sane multiple of monthly" % plan)

    expected_trial = {"days": 7, "card_required": False, "plans": ["pro", "team"],
                      "days_by_plan": {"pro": 7, "team": 14}}
    trial = manifest.get("trial", {})
    for key, value in expected_trial.items():
        if trial.get(key) != value:
            _fail(errors, "trial.%s must be %r" % (key, value))

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

    _check_entitlement_contract(manifest, errors)

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
        "no phone-home",
        "sso/rbac",
        "it never leaves your machine",
    ):
        if unsupported in lower:
            _fail(errors, "website advertises unsupported claim: %s" % unsupported)
    if re.search(r"\bsla\b", public_text, re.IGNORECASE):
        _fail(errors, "website advertises unsupported claim: SLA")
    required = [
        "v" + manifest["version"],
        "%d-day" % manifest["trial"]["days"],
        "$%d <span>/ mo / owner" % manifest["plans"]["pro"]["monthly_usd"],
        "$%d <span>/ seat / mo" % manifest["plans"]["team"]["monthly_usd"],
    ]
    for claim in required:
        if claim not in text:
            _fail(errors, "website is missing manifest claim: %s" % claim)
    for plan, days in manifest["trial"]["days_by_plan"].items():
        if not re.search(r'plan=' + plan + r'&interval=monthly#billing[^>]*>\s*' + str(days) + r'-day free trial', text):
            _fail(errors, "website trial copy does not match " + plan)
    if re.search(r"<strong>29</strong>\s*<span>tools", text):
        _fail(errors, "website advertises a stale MCP tool count")
    for plan in ("pro", "team"):
        for interval, product in manifest["plans"][plan]["products"].items():
            if product["checkout_url"] not in text:
                _fail(errors, "website is missing %s %s checkout" % (plan, interval))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--website-root", type=Path)
    parser.add_argument("--cloud-contract", type=Path, help="Secret-free cloud product contract JSON")
    args = parser.parse_args()
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    errors: list[str] = []
    _check_repository(manifest, errors)
    if args.cloud_contract:
        cloud = json.loads(args.cloud_contract.read_text(encoding="utf-8-sig"))
        _check_cloud_contract(cloud, manifest, errors)
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
