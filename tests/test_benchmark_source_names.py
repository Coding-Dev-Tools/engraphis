from copy import deepcopy
from pathlib import Path

import pytest

from eval.benchmark import report_envelope, source_digest, verify_report_snapshot


def _write_sources(tmp_path):
    first = tmp_path / "operator-private" / "core" / "__init__.py"
    second = tmp_path / "operator-private" / "legacy" / "__init__.py"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"core producer\n")
    second.write_bytes(b"legacy producer with different bytes\n")
    return first, second


def _report(tmp_path, source_paths=(), source_names=None):
    dataset = tmp_path / "private-dataset.jsonl"
    dataset.write_text('{"id":"q1"}\n', encoding="utf-8")
    return report_envelope(
        suite="source-name-fixture",
        dataset_path=dataset,
        config={"k": 1},
        records=[],
        source_paths=source_paths,
        source_names=source_names,
        git_commit="abc123",
    )


def test_explicit_source_names_disambiguate_duplicate_basenames_and_bind_bytes(tmp_path):
    first, second = _write_sources(tmp_path)
    names = ["engraphis/core/__init__.py", "engraphis/legacy/__init__.py"]

    report = _report(tmp_path, [first, second], names)
    sources = report["suite"]["sources"]

    assert [item["name"] for item in sources] == names
    assert [(item["sha256"], item["bytes"]) for item in sources] == [
        (source_digest(first)["sha256"], first.stat().st_size),
        (source_digest(second)["sha256"], second.stat().st_size),
    ]
    assert all(not Path(item["name"]).is_absolute() for item in sources)
    assert all(str(tmp_path) not in item["name"] for item in sources)

    expected = [(name, source_digest(path)["sha256"]) for name, path in zip(names, (first, second))]
    assert verify_report_snapshot(
        report, dataset_sha256=report["suite"]["sha256"], sources=expected
    ) is report

    swapped = deepcopy(report)
    swapped["suite"]["sources"] = list(reversed(swapped["suite"]["sources"]))
    with pytest.raises(ValueError, match="source snapshot"):
        verify_report_snapshot(swapped, dataset_sha256=report["suite"]["sha256"], sources=expected)

    dropped = deepcopy(report)
    dropped["suite"]["sources"].pop()
    with pytest.raises(ValueError, match="source snapshot"):
        verify_report_snapshot(dropped, dataset_sha256=report["suite"]["sha256"], sources=expected)


def test_default_source_names_remain_basename_and_digest_compatible(tmp_path):
    first, second = _write_sources(tmp_path)

    report = _report(tmp_path, [first, second])

    assert report["suite"]["sources"] == [source_digest(first), source_digest(second)]
    assert [item["name"] for item in report["suite"]["sources"]] == [
        "__init__.py", "__init__.py"
    ]


@pytest.mark.parametrize("names", [
    [],
    ["engraphis/core.py", "engraphis/other.py"],
    ["/absolute.py"],
    ["C:/drive.py"],
    ["engraphis\\core.py"],
    ["engraphis/./core.py"],
    ["engraphis/../core.py"],
    ["engraphis//core.py"],
    ["engraphis/core\n.py"],
])
def test_explicit_source_names_fail_closed_for_unsafe_or_mismatched_values(tmp_path, names):
    first, _ = _write_sources(tmp_path)

    with pytest.raises(ValueError, match="source_names"):
        _report(tmp_path, [first], names)


def test_explicit_source_names_reject_string_instead_of_treating_it_as_sequence(tmp_path):
    first, _ = _write_sources(tmp_path)

    with pytest.raises(ValueError, match="source_names"):
        _report(tmp_path, [first], "engraphis/core.py")


def test_explicit_source_names_reject_duplicate_labels(tmp_path):
    first, second = _write_sources(tmp_path)

    with pytest.raises(ValueError, match="source_names must be unique"):
        _report(tmp_path, [first, second], ["engraphis/core.py", "engraphis/core.py"])
