"""Fail CI when product code, deployment docs, or website claims drift from GA truth."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "engraphis" / "commercial_manifest.json"


def _fail(errors: list[str], message: str) -> None:
    errors.append(message)


def _product_mapping(manifest: dict, errors: list[str]) -> dict:
    """Validate billing identities and return env -> (plan, interval)."""
    mapping = {}
    ids = set()
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
            product_id = product.get("id")
            env_name = product.get("env")
            checkout_url = product.get("checkout_url")
            if not isinstance(product_id, str) or not re.fullmatch(
                    r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", product_id):
                _fail(errors, "%s %s product id is not a canonical UUID" % (plan, interval))
                continue
            if product_id in ids:
                _fail(errors, "Polar product id is duplicated: %s" % product_id)
            ids.add(product_id)
            if not isinstance(env_name, str) or not re.fullmatch(r"POLAR_[A-Z_]+_PRODUCT_ID",
                                                                  env_name):
                _fail(errors, "%s %s product env is invalid" % (plan, interval))
                continue
            if env_name in mapping:
                _fail(errors, "Polar product environment is duplicated: %s" % env_name)
            mapping[env_name] = (plan, interval)
            if not isinstance(checkout_url, str):
                _fail(errors, "%s %s checkout URL is missing" % (plan, interval))
                continue
            parsed = urlsplit(checkout_url)
            query = parse_qs(parsed.query)
            if parsed.scheme != "https" or parsed.netloc != "buy.polar.sh" \
                    or query.get("product_id") != [product_id]:
                _fail(errors, "%s %s checkout URL does not match its product id" % (
                    plan, interval))
            if checkout_url in checkout_urls:
                _fail(errors, "Polar checkout URL is duplicated: %s" % checkout_url)
            checkout_urls.add(checkout_url)
    return mapping


def _check_repository(manifest: dict, errors: list[str]) -> None:
    if manifest.get("schema") != "engraphis-commercial/v1":
        _fail(errors, "commercial manifest schema must be engraphis-commercial/v1")
    mapping = _product_mapping(manifest, errors)
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

    template = json.loads((ROOT / "deploy" / "railway-template.json").read_text(
        encoding="utf-8"))
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
    if not local_api.get("required") or not local_api.get("secret") \
            or local_api.get("value") != "${{ secret(48) }}":
        _fail(errors, "Railway template must generate a secret local API token")

    from engraphis.commercial import PRODUCT_ENV, expected_product_ids
    expected = expected_product_ids()
    if mapping != PRODUCT_ENV:
        _fail(errors, "Polar product environment mapping drifted from manifest")
    if set(expected) != set(mapping) or any(
            expected[env].get("id") != manifest["plans"][plan]["products"][interval]["id"]
            for env, (plan, interval) in mapping.items() if env in expected):
        _fail(errors, "commercial product catalog parser drifted from manifest")


def _check_website(manifest: dict, website: Path, errors: list[str]) -> None:
    files = [website / name for name in ("index.html", "product.html", "about.html",
                                          "contact.html")]
    missing = [str(path) for path in files if not path.is_file()]
    if missing:
        _fail(errors, "website files missing: " + ", ".join(missing))
        return
    website_manifest = website / "commercial_manifest.json"
    if (not website_manifest.is_file()
            or json.loads(website_manifest.read_text(encoding="utf-8")) != manifest):
        _fail(errors, "website commercial manifest copy differs from canonical manifest")
    text = "\n".join(path.read_text(encoding="utf-8") for path in files)
    public_text = "\n".join(
        path.read_text(encoding="utf-8") for path in website.rglob("*.html")
    )
    lower = public_text.lower()
    for unsupported in (
            "end-to-end encrypted", "no phone-home", "sso/rbac",
            "it never leaves your machine"):
        if unsupported in lower:
            _fail(errors, "website advertises unsupported claim: %s" % unsupported)
    if re.search(r"\bsla\b", public_text, re.IGNORECASE):
        _fail(errors, "website advertises unsupported claim: SLA")
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
        for product in manifest["plans"][plan]["products"].values():
            if product["checkout_url"] not in text:
                _fail(errors, "website is missing %s checkout %s" % (
                    plan, product["id"]))
    if re.search(r'href="[^"]*polar[^"]*"[^>]*>[^<]*trial', text, re.IGNORECASE):
        _fail(errors, "trial links must use deployment onboarding, not Polar")


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
