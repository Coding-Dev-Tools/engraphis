"""Provenance trust flags + artifact kinds (agentic-upgrade handoff §3.3).

`remember` now accepts `source`, `trusted`, and `kind`; all three land in
`metadata.provenance` and surface through recall/why so prompt builders can
label untrusted content (memory-poisoning guard) and filter by artifact type.
"""
import pytest

from engraphis.service import MemoryService, ValidationError


def _svc() -> MemoryService:
    return MemoryService.create(":memory:")


def _first_provenance(svc, query, **scope):
    r = svc.why(query, **scope)
    recs = r["answer"] + r.get("supersedes", [])
    assert recs, f"no memories found for {query!r}"
    return recs[0]["provenance"]


def test_defaults_are_trusted_agent():
    s = _svc()
    s.remember("Default provenance fact about zebras.", workspace="acme")
    prov = _first_provenance(s, "zebras", workspace="acme")
    assert prov["source"] == "agent"
    assert prov["trusted"] is True
    assert "kind" not in prov


def test_untrusted_web_content_flagged():
    s = _svc()
    s.remember("Claim from a random web page about quasars.", workspace="acme",
               source="web", trusted=False)
    prov = _first_provenance(s, "quasars", workspace="acme")
    assert prov["source"] == "web"
    assert prov["trusted"] is False


def test_artifact_kind_roundtrip():
    s = _svc()
    s.remember("Council verdict: APPROVE with nits on PR 42.", workspace="acme",
               source="agent:council", kind="council_verdict", trusted=True)
    prov = _first_provenance(s, "council verdict PR 42", workspace="acme")
    assert prov["kind"] == "council_verdict"
    assert prov["source"] == "agent:council"


def test_kind_validation_rejects_garbage():
    s = _svc()
    with pytest.raises(ValidationError):
        s.remember("x" , workspace="acme", kind="bad\x00kind!!")


def test_recall_surfaces_provenance():
    s = _svc()
    s.remember("Untrusted note about pelicans from the web.", workspace="acme",
               source="web", trusted=False)
    r = s.recall("pelicans", workspace="acme")
    assert r["count"] >= 1
    mems = [m for m in r["memories"] if "pelicans" in m["content"]]
    assert mems, "expected the pelican memory in recall results"
    prov = mems[0].get("provenance")
    assert prov is not None, "recall results must carry provenance"
    assert prov["trusted"] is False and prov["source"] == "web"


def test_backward_compat_positional_call():
    # Old call shape (no new kwargs) must behave exactly as before.
    s = _svc()
    out = s.remember("Plain old memory.", workspace="acme")
    assert out["stored"] is True and out["op"] == "add"
