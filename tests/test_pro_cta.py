from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SUPPORT_COPY = "Support continued Engraphis development with Pro."
CTA_PARAMS = (
    "utm_source=engraphis",
    "utm_medium=",
    "utm_campaign=pro_conversion",
    "utm_content=",
)
CTA_LABELS = (
    "Subscribe to Pro",
    "Update billing",
    "Open Engraphis Cloud",
)


def test_dashboard_shells_share_the_pro_cta_contract():
    ledger = (ROOT / "engraphis" / "dashboard_assets" / "ledger.js").read_text(encoding="utf-8")
    classic = (ROOT / "engraphis" / "classic_assets" / "dashboard.js").read_text(encoding="utf-8")
    static = (ROOT / "engraphis" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert classic == static
    for shell in (ledger, classic):
        assert SUPPORT_COPY in shell
        assert "utm_source" in shell
        assert "utm_campaign" in shell
        assert all(label in shell for label in CTA_LABELS)
    assert "Start 3-day ${name} trial" in ledger
    assert "Start ${TRIAL_DAYS}-day ${name} trial" in classic


def test_public_pro_ctas_use_documentation_attribution():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    hosted_plans = (ROOT / "docs" / "HOSTED_PLANS.md").read_text(encoding="utf-8")

    assert readme.count("pro_conversion") >= 2
    assert "utm_medium=docs" in readme
    assert "utm_content=readme_intro" in readme
    assert "utm_content=readme_pricing" in readme
    assert "utm_medium=docs" in hosted_plans
    assert "utm_content=hosted_plans_pricing" in hosted_plans
    for document in (readme, hosted_plans):
        assert all(parameter in document for parameter in CTA_PARAMS)

    for heading in (
        "## What Engraphis gives an agent",
        "### See the behavior in reproducible fixtures",
        "## Free forever vs. hosted plans",
    ):
        assert heading in readme
