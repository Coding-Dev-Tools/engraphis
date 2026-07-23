"""The public Automation tab edits hosted dreaming policy, never local inference.

A static-content guard (no server needed) so the controls can't silently drop out of
the dashboard and desync from the `/api/automation` policy fields
(`dream_enabled` / `dream_min_new` / `dream_idle_minutes`).
"""
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "dashboard.js"


def test_automation_form_renders_dream_controls():
    script = SCRIPT.read_text(encoding="utf-8")
    for el in ("au-dream", "au-dream-min", "au-dream-idle"):
        assert f'id="{el}"' in script, el
    assert 'id="au-infer"' not in script
    assert "Requesting managed work uploads the selected workspace’s normal and sensitive memory content" in script


def test_save_body_posts_dream_fields():
    script = SCRIPT.read_text(encoding="utf-8")
    assert "dream_enabled:document.getElementById('au-dream').checked" in script
    assert "dream_min_new:Number(document.getElementById('au-dream-min').value)" in script
    # Idle must not be coerced with ``|| default``: zero is a valid hosted policy value.
    assert "dream_idle_minutes:Number(document.getElementById('au-dream-idle').value)}" \
        in script
    assert "infer:document.getElementById('au-infer').checked" not in script
