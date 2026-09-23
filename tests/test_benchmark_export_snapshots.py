import importlib

import pytest


@pytest.mark.parametrize("name", [
    "vector_scale_storage", "vector_scan_plan", "fts_insert_scaling",
    "native_coverage_scaling", "engine_capacity",
])
@pytest.mark.parametrize("changed", ["dataset", "producer"])
def test_benchmark_exporters_reject_sources_changed_during_envelope_construction(
    tmp_path, monkeypatch, name, changed,
):
    if name == "native_coverage_scaling":
        pytest.importorskip("sqlite_vec")
    module = importlib.import_module("eval." + name)
    replacement = tmp_path / "changed.py"
    replacement.write_text("# bytes that did not produce these results\n", encoding="utf-8")
    original = module.report_envelope

    def late_source_change(**kwargs):
        if changed == "dataset":
            kwargs["dataset_path"] = replacement
        else:
            kwargs["source_paths"] = [replacement, *kwargs["source_paths"][1:]]
        return original(**kwargs)

    monkeypatch.setattr(module, "report_envelope", late_source_change)
    with pytest.raises(ValueError, match="evaluated source snapshot"):
        if name == "engine_capacity":
            monkeypatch.setattr(module, "_repeat", lambda *args: {"operations": []})
            module.run_cell(module.Cell())
        elif name == "vector_scale_storage":
            module.run_file_backed([3], dim=4, queries=1, iterations=1, warmups=0,
                                   k=1, concurrencies=[1], mixed_writes=0, batch_size=2)
        else:
            module.run_comparison([3], dim=4, batch_size=2)
