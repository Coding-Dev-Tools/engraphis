"""The Automation tab exposes the dreaming knobs and wires them to the API fields.

A static-content guard (no server needed) so the controls can't silently drop out of
the dashboard and desync from the `/api/automation` policy fields
(`dream` / `dream_min_new` / `dream_idle_minutes`).
"""
from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "engraphis" / "static" / "index.html"


def test_automation_form_renders_dream_controls():
    html = INDEX.read_text(encoding="utf-8")
    for el in ("au-dream", "au-dream-min", "au-dream-idle", "au-infer"):
        assert f'id="{el}"' in html, el


def test_save_body_posts_dream_fields():
    html = INDEX.read_text(encoding="utf-8")
    assert "dream:document.getElementById('au-dream').checked" in html
    assert "dream_min_new:Number(document.getElementById('au-dream-min').value)" in html
    # idle must NOT be coerced with `|| default` (0 is a valid, meaningful value). It is
    # no longer the last field (infer follows), so assert the bare expression substring.
    assert "dream_idle_minutes:Number(document.getElementById('au-dream-idle').value)," in html
    # the inference toggle is posted too, so it can't desync from the /api/automation field
    assert "infer:document.getElementById('au-infer').checked" in html
