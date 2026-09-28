"""A successful build cannot bypass the owner's candidate-specific release approval."""
from __future__ import annotations

import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.check_release_readiness import LEADERSHIP_GATES, RELEASE_GATES
from scripts.verify_release_qualification import (
    QualificationError, SCHEMA, main, signing_bytes, verify_qualification,
)


@pytest.fixture
def qualification(tmp_path):
    pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # Public synthetic test material, held in memory only. It is not a release key,
    # and no production signing operation is implemented by the shipped verifier.
    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
    public = base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw,
    )).decode("ascii")
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "engraphis-1.2.3-py3-none-any.whl").write_bytes(b"fixture wheel")
    (dist / "engraphis-1.2.3.tar.gz").write_bytes(b"fixture source")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = {
        "engine_commit": "a" * 40,
        "distributions": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in dist.iterdir()},
        "candidate_id": "b" * 64, "ledger_sha256": "c" * 64,
        "release_gates": dict.fromkeys(RELEASE_GATES, "PASS"),
        "issued_at": (now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        "expires_at": (now + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "release_approved": True,
    }

    def receipt(changed=None):
        data = payload if changed is None else changed
        return json.dumps({"schema": SCHEMA, "payload": data,
                           "signature": base64.b64encode(key.sign(signing_bytes(data))).decode("ascii")})

    return {
        "receipt": receipt, "payload": payload, "public": public,
        "arguments": {"commit": "a" * 40, "tag": "v1.2.3", "distribution_directory": dist,
                      "candidate_id": "b" * 64, "ledger_sha256": "c" * 64, "now": now},
    }


def test_exact_signed_release_approval_does_not_require_leadership(qualification):
    item = qualification
    result = verify_qualification(item["receipt"](), item["public"], **item["arguments"])
    assert result["status"] == "PASS"
    assert not set(LEADERSHIP_GATES) & set(item["payload"]["release_gates"])
    assert "signature" not in result


@pytest.mark.parametrize("field,value", [
    ("engine_commit", "d" * 40), ("candidate_id", "d" * 64), ("ledger_sha256", "d" * 64),
    ("release_approved", False), ("release_approved", "true"),
    ("issued_at", "2999-01-01T00:00:00Z"), ("expires_at", "2000-01-01T00:00:00Z"),
    ("issued_at", "2026-01-01T00:00:00+00:00"), ("expires_at", "2026-99-01T00:00:00Z"),
    ("private_notes", "must never be accepted into this public contract"),
])
def test_signed_but_incompatible_receipt_is_rejected(qualification, field, value):
    item = qualification
    payload = deepcopy(item["payload"])
    payload[field] = value
    with pytest.raises(QualificationError):
        verify_qualification(item["receipt"](payload), item["public"], **item["arguments"])


@pytest.mark.parametrize("change", ["missing", "failed", "unverified", "extra", "boolean"])
def test_all_required_gates_are_explicit_and_exact(qualification, change):
    item = qualification
    payload = deepcopy(item["payload"])
    gates = payload["release_gates"]
    if change == "missing":
        gates.pop("pilot")
    elif change == "extra":
        gates["unknown"] = "PASS"
    else:
        gates["pilot"] = {"failed": "FAIL", "unverified": "UNVERIFIED", "boolean": True}[change]
    with pytest.raises(QualificationError, match="mandatory release gate"):
        verify_qualification(item["receipt"](payload), item["public"], **item["arguments"])


@pytest.mark.parametrize("change", ["changed_bytes", "missing_file", "extra_file", "wrong_map"])
def test_approval_is_bound_to_actual_complete_distribution_set(qualification, change):
    item = qualification
    payload = deepcopy(item["payload"])
    dist = item["arguments"]["distribution_directory"]
    wheel = next(dist.glob("*.whl"))
    if change == "changed_bytes":
        wheel.write_bytes(b"a different build")
    elif change == "missing_file":
        wheel.unlink()
    elif change == "extra_file":
        (dist / "unexpected.txt").write_text("extra", encoding="utf-8")
    else:
        payload["distributions"][wheel.name] = "d" * 64
    with pytest.raises(QualificationError):
        verify_qualification(item["receipt"](payload), item["public"], **item["arguments"])


def test_signature_tampering_and_wrong_authority_fail(qualification):
    item = qualification
    receipt = json.loads(item["receipt"]())
    receipt["payload"]["ledger_sha256"] = "d" * 64
    with pytest.raises(QualificationError, match="signature verification"):
        verify_qualification(json.dumps(receipt), item["public"], **item["arguments"])
    with pytest.raises(QualificationError, match="signature verification"):
        verify_qualification(item["receipt"](), base64.b64encode(bytes(32)).decode("ascii"),
                             **item["arguments"])


@pytest.mark.parametrize("field", ["receipt", "public", "candidate_id", "ledger_sha256"])
def test_missing_protected_configuration_fails(qualification, field):
    item = qualification
    arguments = dict(item["arguments"])
    if field in arguments:
        arguments[field] = ""
    with pytest.raises(QualificationError):
        verify_qualification("" if field == "receipt" else item["receipt"](),
                             "" if field == "public" else item["public"], **arguments)


def test_exact_expiry_boundary_and_reversed_window_fail(qualification):
    item = qualification
    payload = deepcopy(item["payload"])
    payload["expires_at"] = item["arguments"]["now"].isoformat().replace("+00:00", "Z")
    with pytest.raises(QualificationError, match="currently valid"):
        verify_qualification(item["receipt"](payload), item["public"], **item["arguments"])
    payload["expires_at"] = payload["issued_at"]
    with pytest.raises(QualificationError, match="currently valid"):
        verify_qualification(item["receipt"](payload), item["public"], **item["arguments"])


@pytest.mark.parametrize("raw", ['{"schema":1,"schema":2}', '{"payload":NaN}',
                                 '{"payload":1e9999}', "[", "x" * 16385])
def test_malformed_receipts_fail_without_echoing_payload(qualification, raw):
    with pytest.raises(QualificationError):
        verify_qualification(raw, qualification["public"], **qualification["arguments"])


def test_cli_requires_configuration_and_never_prints_receipt(qualification, monkeypatch, capsys):
    item = qualification
    configuration = {
        "ENGRAPHIS_RELEASE_QUALIFICATION": item["receipt"](),
        "ENGRAPHIS_RELEASE_VERIFY_KEY": item["public"],
        "ENGRAPHIS_RELEASE_CANDIDATE_ID": "b" * 64,
        "ENGRAPHIS_RELEASE_LEDGER_SHA256": "c" * 64,
    }
    arguments = ["--dist", str(item["arguments"]["distribution_directory"]),
                 "--commit", "a" * 40, "--tag", "v1.2.3"]
    for name in configuration:
        monkeypatch.delenv(name, raising=False)
    assert main(arguments) == 1
    for name, value in configuration.items():
        monkeypatch.setenv(name, value)
    assert main(arguments) == 0
    output = capsys.readouterr()
    assert item["public"] not in output.out + output.err
    assert configuration["ENGRAPHIS_RELEASE_QUALIFICATION"] not in output.out + output.err


def test_publication_writes_require_qualification_except_scoped_release_waivers():
    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    dispatch = workflow.get("on", workflow.get(True, {})).get("workflow_dispatch", {})
    waiver_condition = "inputs.waive_v176_qualification || inputs.waive_v178_qualification"
    for input_name in ("waive_v176_qualification", "waive_v178_qualification"):
        waiver_input = dispatch.get("inputs", {}).get(input_name, {})
        assert waiver_input.get("type") == "boolean"
        assert waiver_input.get("default") is False
    for name, expected_writes in (("publish", 1), ("github-release", 1), ("github-release-repair", 2)):
        job = workflow["jobs"][name]
        assert job["environment"] == "release-qualification"
        checked = False
        writes = 0
        for step in job["steps"]:
            if step.get("name") == "Disclose the qualification waiver before PyPI repair":
                # This conditional write publishes only the exception notice. The
                # ordinary path still needs its signature before any distribution.
                assert name == "github-release-repair"
                assert step.get("if") == waiver_condition
                assert "verified-dist/*" not in step["run"]
                assert "release-evidence/*" not in step["run"]
                assert "true:false:v1.7.6:6a441a75c8dd159607fa3933da83f600864b9146" in step["run"]
                assert "false:true:v1.7.8:dce68e1602e580cd51b71e26db2ab04238df7df4" in step["run"]
                assert step["env"]["WAIVE_V176"] == "${{ inputs.waive_v176_qualification }}"
                assert step["env"]["WAIVE_V178"] == "${{ inputs.waive_v178_qualification }}"
                continue
            if "scripts.verify_release_qualification" in step.get("run", ""):
                if name == "github-release-repair":
                    assert step.get("if") == (
                        "${{ !inputs.waive_v176_qualification && !inputs.waive_v178_qualification }}"
                    )
                else:
                    assert "if" not in step
                assert not step.get("continue-on-error", False)
                required = {
                    "ENGRAPHIS_RELEASE_QUALIFICATION", "ENGRAPHIS_RELEASE_VERIFY_KEY",
                    "ENGRAPHIS_RELEASE_CANDIDATE_ID", "ENGRAPHIS_RELEASE_LEDGER_SHA256",
                }
                assert set(step["env"]) >= required
                for key in required:
                    # Actions prints variable-backed step environment values in public
                    # logs. Only the protected secret context masks these fields.
                    assert step["env"][key] == "${{ secrets." + key + " }}"
                assert '--commit "$ENGRAPHIS_REPAIR_COMMIT"' in step["run"] if name.endswith("repair") else (
                    '--commit "$GITHUB_SHA"' in step["run"])
                checked = True
            if (step.get("uses", "").startswith("pypa/gh-action-pypi-publish@")
                    or "gh release upload" in step.get("run", "")
                    or "gh release create" in step.get("run", "")):
                assert checked, name + " contains an unqualified publication write"
                checked = False
                writes += 1
        assert writes == expected_writes
    assert workflow["jobs"]["publish"]["needs"] == "release-evidence"
    assert workflow["jobs"]["github-release"]["needs"] == "publish"
    repair_steps = workflow["jobs"]["github-release-repair"]["steps"]
    waiver_guard = next(step for step in repair_steps
                        if step.get("name") == "Enforce and record the release-specific qualification waiver")
    assert waiver_guard.get("if") == waiver_condition
    assert "true:false:v1.7.6|false:true:v1.7.8" in waiver_guard["run"]
    assert waiver_guard["env"]["WAIVE_V176"] == "${{ inputs.waive_v176_qualification }}"
    assert waiver_guard["env"]["WAIVE_V178"] == "${{ inputs.waive_v178_qualification }}"
    disclosure = next(step for step in repair_steps
                      if step.get("name") == "Disclose the qualification waiver before PyPI repair")
    publication = next(step for step in repair_steps
                       if step.get("name") == "Publish only missing verified distributions")
    assert repair_steps.index(disclosure) < repair_steps.index(publication)
    assert '--json isDraft --jq .isDraft)" = "false"' in disclosure["run"]
    repair = next(step for step in repair_steps if step.get("name") == "Repair GitHub Release")
    assert repair["env"]["WAIVE_QUALIFICATION"] == "${{ " + waiver_condition + " }}"
    assert repair["run"].index("gh release edit") < repair["run"].index("gh release upload")
    assert '"${notes_args[@]}"' in repair["run"].split("gh release create", 1)[1]
    assert "${{ vars.ENGRAPHIS_RELEASE_" not in (
        root / ".github/workflows/release.yml"
    ).read_text(encoding="utf-8")


@pytest.mark.skipif(os.name == "nt", reason="release workflow executes in Linux bash")
@pytest.mark.parametrize("existing,edit_fails", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("tag", ["v1.7.6", "v1.7.8"])
def test_waiver_disclosure_cannot_follow_github_publication(tmp_path, existing, edit_fails, tag):
    yaml = pytest.importorskip("yaml")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    repair = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                  if step.get("name") == "Repair GitHub Release")
    executable = tmp_path / "gh"
    executable.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  view)
    if [ "$3" = --repo ]; then printf 'v1.7.8\\n'; exit 0; fi
    if [ "$EXISTING" != true ]; then exit 1; fi
    printf 'Existing release notes\\n'
    ;;
  edit)
    if [ "$EDIT_FAILS" = true ]; then exit 7; fi
    ;;
esac
""", encoding="utf-8")
    executable.chmod(0o700)
    script = tmp_path / "repair.sh"
    script.write_text(repair["run"], encoding="utf-8")
    calls_path = tmp_path / "calls.txt"
    result = subprocess.run([bash, str(script)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=20, env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                             "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": tag,
                                             "WAIVE_QUALIFICATION": "true", "GH_REPO": "test/repo",
                                             "GH_RUN_URL": "https://example.test/run/1", "GH_CALLS": str(calls_path),
                                             "EXISTING": str(existing).lower(), "EDIT_FAILS": str(edit_fails).lower()})
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert result.returncode == (7 if edit_fails else 0), result.stderr
    if existing:
        edits = [index for index, call in enumerate(calls)
                 if call.startswith("release edit ") and "--notes-file " in call]
        uploads = [index for index, call in enumerate(calls) if call.startswith("release upload ")]
        assert len(edits) == 1
        assert not uploads if edit_fails else len(uploads) == 1 and edits[0] < uploads[0]
        notes = (tmp_path / "release-notes.md").read_text(encoding="utf-8")
        assert notes.startswith("Existing release notes")
    else:
        creation = next(call for call in calls if call.startswith("release create "))
        assert "--notes-file " in creation
        assert not any(call.startswith("release edit ") for call in calls)
        notes = (tmp_path / "release-waiver.md").read_text(encoding="utf-8")
    assert "Mandatory full-product gates are not represented as passed." in notes
    assert "https://example.test/run/1" in notes


@pytest.mark.parametrize("candidate,latest,expected", [
    ("v1.7.6", "v1.7.8", "false"), ("v1.7.8", "v1.7.6", "true"),
    ("v1.7.8", "v1.7.8", "true"), ("v1.7.8", "v1.7.10", "false"),
    ("v1.7.8", "", None), ("v1.7.8", "v1.7.9-rc1", None),
    ("v1.7.8", "unknown", None), ("v1.07.8", "v1.7.6", None),
])
def test_waiver_latest_comparison_is_numeric_and_fails_closed(candidate, latest, expected):
    yaml = pytest.importorskip("yaml")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    repair = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                  if step.get("name") == "Repair GitHub Release")
    code = repair["run"].split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    result = subprocess.run([sys.executable, "-c", code, candidate, latest],
                            capture_output=True, text=True, timeout=10)
    if expected is None:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == expected


@pytest.mark.skipif(os.name == "nt", reason="release workflow executes in Linux bash")
@pytest.mark.parametrize("latest,lookup_fails,expected", [
    ("v1.7.4", False, True), ("v1.7.8", False, True),
    ("v1.7.9", False, False), ("v1.7.10", False, False),
    ("", True, None), ("unknown", False, None),
])
@pytest.mark.parametrize("existing", [False, True])
def test_waiver_repair_preserves_newer_latest(tmp_path, latest, lookup_fails, expected, existing):
    yaml = pytest.importorskip("yaml")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    repair = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                  if step.get("name") == "Repair GitHub Release")
    executable = tmp_path / "gh"
    executable.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$GH_CALLS"
if [ "$2" = view ]; then
  if [ "$3" = --repo ]; then
    if [ "$LOOKUP_FAILS" = true ]; then exit 7; fi
    printf '%s\\n' "$CURRENT_LATEST"
  else
    if [ "$EXISTING" != true ]; then exit 1; fi
    printf 'Existing release notes\\n'
  fi
fi
""", encoding="utf-8")
    executable.chmod(0o700)
    script = tmp_path / "repair.sh"
    script.write_text(repair["run"], encoding="utf-8")
    calls_path = tmp_path / "calls.txt"
    result = subprocess.run([bash, str(script)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=20, env={**os.environ,
                                "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": "v1.7.8",
                                "WAIVE_QUALIFICATION": "true", "GH_REPO": "test/repo",
                                "GH_RUN_URL": "https://example.test/run/1", "GH_CALLS": str(calls_path),
                                "CURRENT_LATEST": latest, "LOOKUP_FAILS": str(lookup_fails).lower(),
                                "EXISTING": str(existing).lower()})
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    writes = [call for call in calls if call.startswith(("release edit", "release create", "release upload"))]
    if expected is None:
        assert result.returncode != 0
        assert writes == []
    else:
        assert result.returncode == 0, result.stderr
        assert writes
        assert any(call.endswith(" --latest") for call in writes) is expected
        if not existing and not expected:
            assert writes[-1].endswith(" --latest=false")


@pytest.mark.skipif(os.name == "nt", reason="release workflow executes in Linux bash")
@pytest.mark.parametrize("existing,edit_fails,draft", [
    (False, False, False), (True, False, False), (True, True, False), (True, False, True),
])
@pytest.mark.parametrize("tag,commit", [
    ("v1.7.6", "6a441a75c8dd159607fa3933da83f600864b9146"),
    ("v1.7.8", "dce68e1602e580cd51b71e26db2ab04238df7df4"),
])
def test_public_waiver_notice_precedes_pypi_even_if_later_repair_fails(
    tmp_path, existing, edit_fails, draft, tag, commit,
):
    yaml = pytest.importorskip("yaml")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    disclosure = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                      if step.get("name") == "Disclose the qualification waiver before PyPI repair")
    executable = tmp_path / "gh"
    executable.write_text("""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> "$GH_CALLS"
case "$2" in
  view)
    if [[ "$*" == *"--json isDraft"* ]]; then
      printf '%s\\n' "$DRAFT"
    else
      if [ "$EXISTING" != true ]; then exit 1; fi
      printf 'Existing release notes\\n'
    fi
    ;;
  edit)
    if [ "$EDIT_FAILS" = true ]; then exit 7; fi
    ;;
esac
""", encoding="utf-8")
    executable.chmod(0o700)
    script = tmp_path / "disclose.sh"
    # A later publication/verification failure must leave the already public
    # notice in place; a failed notice write must prevent publication altogether.
    script.write_text(disclosure["run"] + '\nprintf "pypi-publication\\n" >> "$GH_CALLS"\nexit 9\n',
                      encoding="utf-8")
    calls_path = tmp_path / "calls.txt"
    environment = {**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                   "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": tag, "GH_REPO": "test/repo",
                   "ENGRAPHIS_REPAIR_COMMIT": commit,
                   "WAIVE_V176": str(tag == "v1.7.6").lower(),
                   "WAIVE_V178": str(tag == "v1.7.8").lower(),
                   "GH_RUN_URL": "https://example.test/run/1", "GH_CALLS": str(calls_path),
                   "EXISTING": str(existing).lower(), "EDIT_FAILS": str(edit_fails).lower(),
                   "DRAFT": str(draft).lower()}
    result = subprocess.run([bash, str(script)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=20, env=environment)
    calls = calls_path.read_text(encoding="utf-8").splitlines()
    assert result.returncode == (7 if edit_fails else 1 if draft else 9), result.stderr
    notice = next(index for index, call in enumerate(calls) if "--notes-file " in call)
    if edit_fails or draft:
        assert "pypi-publication" not in calls
    else:
        assert notice < calls.index("pypi-publication")
    assert not any("verified-dist/" in call or "release-evidence/" in call for call in calls)
    if not existing:
        assert "--latest=false" in calls[notice]
    notes = (tmp_path / "release-waiver.md").read_text(encoding="utf-8")
    assert "Mandatory full-product gates are not represented as passed." in notes
    assert environment["ENGRAPHIS_REPAIR_COMMIT"] in notes


@pytest.mark.skipif(os.name == "nt", reason="release workflow executes in Linux bash")
@pytest.mark.parametrize("tag,commit,v176,v178", [
    ("v1.7.7", "6a441a75c8dd159607fa3933da83f600864b9146", "true", "false"),
    ("v1.7.6", "a" * 40, "true", "false"),
    ("v1.7.6", "", "true", "false"),
    ("v1.7.8", "a" * 40, "false", "true"),
    ("v1.7.8", "", "false", "true"),
    ("v1.7.8", "dce68e1602e580cd51b71e26db2ab04238df7df4", "true", "false"),
    ("v1.7.6", "6a441a75c8dd159607fa3933da83f600864b9146", "false", "true"),
    ("v1.7.8", "dce68e1602e580cd51b71e26db2ab04238df7df4", "true", "true"),
    ("v1.7.8", "dce68e1602e580cd51b71e26db2ab04238df7df4", "false", "false"),
])
def test_waiver_rejects_a_different_retained_candidate_before_any_public_write(tmp_path, tag, commit, v176, v178):
    yaml = pytest.importorskip("yaml")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    disclosure = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                      if step.get("name") == "Disclose the qualification waiver before PyPI repair")
    executable = tmp_path / "gh"
    executable.write_text('#!/usr/bin/env bash\nprintf "unexpected call\\n" >> "$GH_CALLS"\n',
                          encoding="utf-8")
    executable.chmod(0o700)
    script = tmp_path / "disclose.sh"
    script.write_text(disclosure["run"], encoding="utf-8")
    calls_path = tmp_path / "calls.txt"
    result = subprocess.run([bash, str(script)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=20, env={**os.environ, "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
                                             "RUNNER_TEMP": str(tmp_path), "RELEASE_TAG": tag,
                                             "ENGRAPHIS_REPAIR_COMMIT": commit, "GH_REPO": "test/repo",
                                             "WAIVE_V176": v176, "WAIVE_V178": v178,
                                             "GH_CALLS": str(calls_path)})
    assert result.returncode != 0
    assert not calls_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="release workflow executes in Linux bash")
@pytest.mark.parametrize("tag,v176,v178,allowed", [
    ("v1.7.6", "true", "false", True),
    ("v1.7.8", "false", "true", True),
    ("v1.7.6", "true", "true", False),
    ("v1.7.8", "true", "true", False),
    ("v1.7.8", "true", "false", False),
    ("v1.7.6", "false", "true", False),
    ("v1.7.9", "false", "true", False),
    ("v1.7.8", "false", "false", False),
])
def test_waiver_input_guard_rejects_ambiguous_or_unapproved_requests(tmp_path, tag, v176, v178, allowed):
    yaml = pytest.importorskip("yaml")
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash is unavailable")
    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/release.yml").read_text(encoding="utf-8"))
    guard = next(step for step in workflow["jobs"]["github-release-repair"]["steps"]
                 if step.get("name") == "Enforce and record the release-specific qualification waiver")
    script = tmp_path / "guard.sh"
    script.write_text(guard["run"], encoding="utf-8")
    summary = tmp_path / "summary.md"
    result = subprocess.run([bash, str(script)], cwd=tmp_path, capture_output=True, text=True,
                            timeout=20, env={**os.environ, "RELEASE_TAG": tag,
                                             "WAIVE_V176": v176, "WAIVE_V178": v178,
                                             "GH_ACTOR": "test-actor", "GH_RUN_URL": "https://example.test/run/1",
                                             "GITHUB_STEP_SUMMARY": str(summary)})
    assert (result.returncode == 0) is allowed, result.stderr
    assert summary.exists() is allowed
    if allowed:
        assert f"Tag: `{tag}`" in summary.read_text(encoding="utf-8")
        assert "Triggered by: `test-actor`" in summary.read_text(encoding="utf-8")
