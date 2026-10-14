"""The public Automation tab edits hosted dreaming policy, never local inference.

A static-content guard (no server needed) so the controls can't silently drop out of
the dashboard and desync from the `/api/automation` policy fields
(`dream_enabled` / `dream_min_new` / `dream_idle_minutes`).
"""
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "engraphis" / "dashboard_assets"
SCRIPT = ASSETS / "ledger.js"
INDEX = ASSETS / "index.html"


def test_automation_form_renders_dream_controls():
    script = SCRIPT.read_text(encoding="utf-8")
    for el in ("automation-dream", "automation-dream-min", "automation-dream-idle"):
        assert "'%s'" % el in script, el
    # Inference is offered only as a hosted proposal policy, never as local inference.
    assert ("automationCheckbox('automation-infer', 'Allow hosted relationship inference "
            "proposals', policy.infer)") in script
    assert "submits a bounded snapshot of this workspace’s normal and sensitive memory content" \
        in script
    # Consent travels with the workspace approval. The dashboard must never name the operator
    # override, so the string cannot appear anywhere the browser could render it.
    for asset in (SCRIPT, INDEX):
        assert "ENGRAPHIS_MANAGED_COMPUTE_CONSENT" not in asset.read_text(encoding="utf-8")


def test_save_body_posts_dream_fields():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "dream_enabled: byId('automation-dream').checked" in script
    assert "dream_min_new: Math.max(1, Number(byId('automation-dream-min').value) || 1)" in script
    # Zero idle minutes is a valid hosted policy value, so it must survive the coercion.
    assert ("dream_idle_minutes: Math.max(0, Number(byId('automation-dream-idle').value) || 0)"
            in script)
    assert "infer: byId('automation-infer').checked," in script
