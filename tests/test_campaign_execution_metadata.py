"""Public execution metadata must not serialize arbitrary caller or probe output."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from eval import benchmark_campaign as campaign


_SENTINEL = "DO_NOT_PERSIST_SECRET"


def _valid_metadata(**overrides: object) -> dict:
    value = {
        "transport": "codex_oauth",
        "provider": campaign.OAUTH_PROVIDER,
        "executable": "codex.exe",
        "resolved_executable": "codex.exe",
        "executable_sha256": "a" * 64,
        "version": "codex-cli 0.149.0",
        "instruction_sha256": "b" * 64,
        "global_instruction_sha256": "b" * 64,
        "forced_login_method": "chatgpt",
        "requested_model": campaign.MODEL,
        "effective_model": campaign.MODEL,
        "reasoning_effort": "medium",
        "allow_provider_model_fallback": False,
        "request_max_retries": 0,
        "stream_max_retries": 0,
        "supports_websockets": False,
    }
    value.update(overrides)
    return value


def _metadata_from_probe(monkeypatch, tmp_path: Path, output: str) -> tuple[dict, list[dict]]:
    executable = tmp_path / "codex.exe"
    executable.write_bytes(b"fixture executable")
    home = tmp_path / "home"
    home.mkdir()
    calls: list[dict] = []

    def fake_check_output(args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return output

    monkeypatch.setattr(campaign.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(campaign.subprocess, "check_output", fake_check_output)
    return campaign._codex_execution_metadata(executable=str(executable)), calls


@pytest.mark.parametrize(
    "override",
    [
        {},
        {"version": _SENTINEL},
        {"password": _SENTINEL},
        {"nested": {"secret": _SENTINEL}},
    ],
    ids=["empty", "allowed-field-sentinel", "unknown-secret-field", "nested-secret"],
)
def test_manifest_rejects_every_caller_override_before_construction(
    override, tmp_path, monkeypatch
) -> None:
    constructed: list[bool] = []
    monkeypatch.setattr(
        campaign,
        "_codex_execution_metadata",
        lambda: constructed.append(True) or _valid_metadata(),
    )

    with pytest.raises(ValueError, match="caller-supplied OAuth configuration is unsupported"):
        campaign.make_manifest(
            embed_model="test",
            embed_revision="a" * 40,
            dependency_lock=tmp_path / "missing-lock.json",
            oauth_configuration=override,
        )
    assert constructed == []


@pytest.mark.parametrize(
    "output",
    [
        "codex-cli 0.149.0\nwarning banner",
        "codex-cli 0.149.0 " + _SENTINEL,
        "",
        "codex-cli",
        "v0.149.0",
        "x" * 129,
    ],
    ids=["banner", "same-line-sentinel", "empty", "missing-version", "wrong-prefix", "oversized"],
)
def test_malformed_cli_output_becomes_unconfigured_and_is_not_retained(
    output, monkeypatch, tmp_path
) -> None:
    metadata, calls = _metadata_from_probe(monkeypatch, tmp_path, output)

    assert metadata["version"] == "unconfigured"
    assert _SENTINEL not in json.dumps(metadata, sort_keys=True)
    assert len(calls) == 1
    assert calls[0]["args"][-1] == "--version"
    assert calls[0]["kwargs"] == {
        "text": True,
        "stderr": campaign.subprocess.STDOUT,
        "timeout": 10,
    }


def test_valid_cli_output_is_retained_only_as_the_complete_version(monkeypatch, tmp_path) -> None:
    metadata, calls = _metadata_from_probe(monkeypatch, tmp_path, "codex-cli 0.149.0\n")

    assert metadata["version"] == "codex-cli 0.149.0"
    assert campaign._is_codex_version(metadata["version"])
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("value", "valid"),
    [
        ("codex-cli 0.149.0", True),
        ("codex-cli 1.2.3-alpha.1+build.7", True),
        ("codex-cli 1.2", False),
        ("codex-cli 1.2.3\nwarning", False),
        ("codex-cli 1.2.3 " + _SENTINEL, False),
        ("x" * 129, False),
        (None, False),
    ],
    ids=["stable", "semver-build", "short", "multiline", "sentinel", "oversized", "non-string"],
)
def test_codex_version_grammar(value, valid) -> None:
    assert campaign._is_codex_version(value) is valid


def test_execution_metadata_reads_only_public_probe_inputs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("ENGRAPHIS_CODEX_TOKEN", _SENTINEL)
    monkeypatch.setenv("OPENAI_API_KEY", _SENTINEL)
    metadata, calls = _metadata_from_probe(monkeypatch, tmp_path, "codex-cli 0.149.0")

    assert set(metadata) <= campaign._TRANSPORT_METADATA_FIELDS
    assert _SENTINEL not in json.dumps(metadata, sort_keys=True)
    assert metadata["executable"] == "codex.exe"
    assert metadata["resolved_executable"] == "codex.exe"
    assert len(metadata["executable_sha256"]) == 64
    assert metadata["instruction_sha256"] == metadata["global_instruction_sha256"] == "0" * 64
    assert calls[0]["args"][1:] == ["--version"]

    executable = tmp_path / "codex.exe"
    config = tmp_path / "home" / ".codex"
    config.mkdir()
    instruction = config / "AGENTS.md"
    instruction.write_text("Private instructions " + _SENTINEL, encoding="utf-8")
    (config / "auth.json").write_text(json.dumps({"access_token": _SENTINEL}), encoding="utf-8")
    read_paths = []
    original_open = Path.open

    def guarded_open(path, *args, **kwargs):
        assert path in (executable, instruction), "metadata probe must not read account files"
        read_paths.append(path)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    metadata = campaign._codex_execution_metadata(executable=str(executable))
    assert set(read_paths) == {executable, instruction}
    assert metadata["instruction_sha256"] == metadata["global_instruction_sha256"] != "0" * 64
    assert _SENTINEL not in json.dumps(metadata, sort_keys=True)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.pop("version"),
        lambda value: value.update(secret=_SENTINEL),
        lambda value: value.update(version={"secret": _SENTINEL}),
        lambda value: value.update(allow_provider_model_fallback=1),
        lambda value: value.update(request_max_retries=False),
        lambda value: value.update(executable="C:\\private\\codex.exe", resolved_executable="C:\\private\\codex.exe"),
        lambda value: value.update(executable="../codex.exe", resolved_executable="../codex.exe"),
        lambda value: value.update(executable="codex/evil", resolved_executable="codex/evil"),
        lambda value: value.update(executable="", resolved_executable=""),
        lambda value: value.update(executable_sha256="A" * 64),
        lambda value: value.update(executable_sha256="a" * 63),
        lambda value: value.update(instruction_sha256="c" * 64),
        lambda value: value.update(version="codex-cli 1.2"),
        lambda value: value.update(version="codex-cli 1.2.3\nwarning"),
        lambda value: value.update(attempt_timeout_seconds=True),
        lambda value: value.update(attempt_timeout_seconds=29),
        lambda value: value.update(attempt_timeout_seconds=601),
        lambda value: value.update(attempt_timeout_seconds=float("nan")),
        lambda value: value.update(attempt_timeout_seconds=10 ** 1000),
        lambda value: value.update(attempt_timeout_seconds=float("inf")),
    ],
    ids=[
        "missing-required",
        "unknown-field",
        "nested-value",
        "integer-for-bool",
        "bool-for-integer",
        "absolute-executable",
        "parent-executable",
        "separator-executable",
        "empty-executable",
        "uppercase-hash",
        "short-hash",
        "instruction-mismatch",
        "short-version",
        "version-banner",
        "bool-timeout",
        "short-timeout",
        "long-timeout",
        "nan-timeout",
        "huge-timeout",
        "infinite-timeout",
    ],
)
def test_execution_metadata_validator_rejects_malformed_values(mutation) -> None:
    value = _valid_metadata()
    mutation(value)
    with pytest.raises(ValueError):
        campaign._validate_execution_metadata(value)


def test_execution_metadata_validator_accepts_omitted_timeout_and_unconfigured_probes() -> None:
    campaign._validate_execution_metadata(_valid_metadata())
    campaign._validate_execution_metadata(_valid_metadata(attempt_timeout_seconds=120))
    campaign._validate_execution_metadata(
        _valid_metadata(version="unconfigured", executable_sha256="d" * 64)
    )
    campaign._validate_execution_metadata(
        _valid_metadata(
            executable="unconfigured",
            resolved_executable="unconfigured",
            version="unconfigured",
            executable_sha256="0" * 64,
            instruction_sha256="0" * 64,
            global_instruction_sha256="0" * 64,
        )
    )


def test_make_manifest_validates_metadata_before_binding(tmp_path, monkeypatch) -> None:
    invalid = _valid_metadata(version=_SENTINEL)
    digest_calls: list[object] = []
    monkeypatch.setattr(campaign, "_codex_execution_metadata", lambda: invalid)
    monkeypatch.setattr(campaign, "digest", lambda value: digest_calls.append(value) or "digest")

    with pytest.raises(ValueError, match="version"):
        campaign.make_manifest(
            embed_model="test",
            embed_revision="a" * 40,
            dependency_lock=tmp_path / "missing-lock.json",
        )
    assert digest_calls == []


def test_prepare_rejects_invalid_metadata_before_creating_public_files(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(campaign, "_codex_execution_metadata", lambda: _valid_metadata(version=_SENTINEL))
    for name in ("MEM0_TELEMETRY", "GRAPHITI_TELEMETRY_ENABLED", "HF_HUB_DISABLE_TELEMETRY"):
        monkeypatch.setenv(name, "false")
    manifest_path = tmp_path / "manifest.json"
    companion_path = tmp_path / "companion.json"
    result = campaign.main([
        "--prepare", "--manifest", str(manifest_path), "--companion", str(companion_path),
        "--dependency-lock", str(tmp_path / "missing-lock.json"),
    ])
    assert result == 1
    assert not manifest_path.exists() and not companion_path.exists()
    assert _SENTINEL not in capsys.readouterr().err


def test_unconfigured_probe_cannot_pass_live_validation(tmp_path, monkeypatch) -> None:
    metadata = _valid_metadata(version="unconfigured")
    monkeypatch.setattr(campaign, "_codex_execution_metadata", lambda **kwargs: copy.deepcopy(metadata))
    monkeypatch.setattr(campaign, "source_snapshot", lambda: {})
    monkeypatch.setattr(campaign.shutil, "which", lambda _: None)
    lock = tmp_path / "environment.json"
    lock.write_text("{}", encoding="utf-8")
    manifest, companion = campaign.make_manifest(
        embed_model="test", embed_revision="a" * 40, dependency_lock=lock,
    )
    with pytest.raises(ValueError, match="executable is unavailable"):
        campaign.validate_manifest(manifest, companion, dependency_lock=lock, live=True)


def test_valid_offline_manifest_roundtrip_and_rehashed_invalid_metadata_rejection(
    tmp_path, monkeypatch
) -> None:
    metadata = _valid_metadata()
    monkeypatch.setattr(campaign, "_codex_execution_metadata", lambda: copy.deepcopy(metadata))
    monkeypatch.setattr(campaign, "source_snapshot", lambda: {})
    lock = tmp_path / "environment.json"
    lock.write_text("{}", encoding="utf-8")

    manifest, companion = campaign.make_manifest(
        embed_model="test",
        embed_revision="a" * 40,
        dependency_lock=lock,
    )
    manifest_path = tmp_path / "manifest.json"
    companion_path = tmp_path / "companion.json"
    campaign._save_new(manifest_path, manifest)
    campaign._save_new(companion_path, companion)

    loaded_manifest = campaign._read(manifest_path)
    loaded_companion = campaign._read(companion_path)
    assert loaded_manifest["oauth"] == metadata
    assert _SENTINEL not in manifest_path.read_text(encoding="utf-8")
    campaign.validate_manifest(loaded_manifest, loaded_companion, live=False)

    tampered = copy.deepcopy(loaded_manifest)
    tampered["oauth"]["version"] = _SENTINEL
    tampered["binding_sha256"] = campaign.digest(
        {key: value for key, value in tampered.items() if key != "binding_sha256"}
    )
    tampered_companion = tmp_path / "tampered-companion.json"
    tampered_manifest = tmp_path / "tampered-manifest.json"
    campaign._save_new(tampered_manifest, tampered)
    campaign._save_new(tampered_companion, loaded_companion)
    with pytest.raises(ValueError, match="version"):
        campaign.validate_manifest(
            campaign._read(tampered_manifest),
            campaign._read(tampered_companion),
            live=False,
        )
