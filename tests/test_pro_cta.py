import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SUPPORT_COPY = "Support continued Engraphis development with Pro."
CTA_PARAMS = (
    "utm_source=engraphis",
    "utm_medium=",
    "utm_campaign=pro_conversion",
    "utm_content=",
)
CTA_LABELS = (
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
    # The dashboard shells share the dynamic CTA implementation, while the
    # compatibility shell still matches the generated source exactly.
    for shell in (ledger, classic):
        assert "Subscribe to ${name}" in shell


def _function(script, name):
    """Extract the actual declaration and stop at the next peer declaration."""
    match = re.search(r"^(?P<indent> *)function " + re.escape(name) + r"\(", script, re.MULTILINE)
    assert match is not None, f"missing production helper: {name}"
    end = re.search(r"^" + re.escape(match["indent"]) + r"(?:async )?function ",
                    script[match.end():], re.MULTILINE)
    return script[match.start():match.end() + end.start() if end else len(script)].rstrip()


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required to run the UI")
@pytest.mark.parametrize("shell", ["ledger", "classic"])
def test_trial_ctas_use_each_disclosed_plan_duration_without_guessing_team(tmp_path, shell):
    """Execute both shipped helpers: a Pro fallback must never become a Team promise."""
    if shell == "ledger":
        script = (ROOT / "engraphis/dashboard_assets/ledger.js").read_text(encoding="utf-8")
        functions = ("licenseAccessState", "licensePlanKey", "licenseTrialAvailable",
                     "licenseHasHostedAccess", "licenseTrialDays", "hostedCta")
    else:
        script = (ROOT / "engraphis/classic_assets/dashboard.js").read_text(encoding="utf-8")
        functions = ("licAccessState", "licPlanKey", "licTrialAvailable",
                     "licAccessLive", "licTrialDays", "hostedCta")
    cases = [
        ({"trial_days": 3, "days_by_plan": {"pro": 3, "team": 10}},
         ["Start 3-day Pro trial", "Start 10-day Team trial"]),
        ({"trial_days": 3, "days_by_plan": {"pro": 5, "team": 17}},
         ["Start 5-day Pro trial", "Start 17-day Team trial"]),
        ({"trial_days": 3}, ["Start 3-day Pro trial", "Start Team trial"]),
        ({"trial_days": 3, "days_by_plan": {"team": "10"}},
         ["Start 3-day Pro trial", "Start Team trial"]),
        ({"trial_days": 3, "days_by_plan": {"team": True}},
         ["Start 3-day Pro trial", "Start Team trial"]),
        ({"trial_days": 3, "days_by_plan": {"team": 0}},
         ["Start 3-day Pro trial", "Start Team trial"]),
        ({}, ["Start Pro trial", "Start Team trial"]),
    ]
    # Isolate display semantics; real checkout routing is covered by the browser suite.
    setup = """
const state = {license:null};
let LIC = null;
function hostedPlanUrl(plan,trial){return `https://example.test/?plan=${plan}&trial=${trial}`}
function hostedAccountUrl(){return 'https://example.test/account'}
"""
    driver = """
const output = JSON.parse(process.argv[2]).map(trial => {
  LIC = state.license = {plan:'local', plan_source:'local', access_state:'inactive',
                         trial:{available:true, ...trial}};
  return ['pro','team'].map(plan => hostedCta(plan).label);
});
process.stdout.write(JSON.stringify(output));
"""
    runner = tmp_path / "trial_ctas.js"
    runner.write_text("\n".join([setup, *(_function(script, name) for name in functions), driver]),
                      encoding="utf-8")
    result = subprocess.run(["node", str(runner), json.dumps([trial for trial, _ in cases])],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [expected for _, expected in cases]


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
        "## Free forever vs. hosted plans",
    ):
        assert heading in readme
