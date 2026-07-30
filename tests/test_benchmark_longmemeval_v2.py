import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from eval.longmemeval_v2 import (
    EngraphisLongMemEvalV2Memory,
    _context_items_with_budget,
    _trajectory_text,
)
from eval import run_longmemeval_v2
from eval.run_longmemeval_v2 import (
    PINNED_LONGMEMEVAL_V2_REVISION,
    PINNED_READER_MODEL,
    PINNED_READER_REVISION,
    pin_official_reader_processor,
    verify_official_checkout,
)


def test_v2_adapter_implements_official_insert_query_text_contract():
    memory = EngraphisLongMemEvalV2Memory(context_k=3)
    memory.insert({
        "trajectory_id": "t-1",
        "steps": [{"observation": "The billing page has an export button."}],
    })
    context = memory.query("Where is the export button?")
    assert context
    assert all(item["type"] == "text" and item["value"] for item in context)
    assert "export button" in context[0]["value"]


def test_v2_adapter_query_is_observational_and_writes_no_receipt():
    memory = EngraphisLongMemEvalV2Memory(context_k=3)
    memory.insert({"id": "receipt-free", "text": "The billing page has an export button."})
    before_receipts = memory.service.store.conn.execute(
        "SELECT COUNT(*) FROM operation_receipts"
    ).fetchone()[0]
    before_memory = memory.service.store.conn.execute(
        "SELECT access_count, stability FROM memories"
    ).fetchone()

    memory.query("Where is the export button?")

    after_receipts = memory.service.store.conn.execute(
        "SELECT COUNT(*) FROM operation_receipts"
    ).fetchone()[0]
    after_memory = memory.service.store.conn.execute(
        "SELECT access_count, stability FROM memories"
    ).fetchone()
    assert after_receipts == before_receipts
    assert tuple(after_memory) == tuple(before_memory)


def test_v2_adapter_indexes_late_trajectory_states_without_full_history_duplication():
    memory = EngraphisLongMemEvalV2Memory(context_k=4, retrieval_profile="lexical")
    memory.insert({
        "trajectory_id": "long",
        "states": [
            {"observation": "Earlier state " + "noise " * 600},
            {"observation": "Late state: the recovery code is cobalt-owl-74."},
        ],
    })

    rows = memory.service.store.conn.execute(
        "SELECT title, content, metadata FROM memories ORDER BY title"
    ).fetchall()
    assert len(rows) >= 2
    assert all(len(row["content"]) <= 1800 for row in rows)
    assert all("state:" in row["title"] and "part:" in row["title"] for row in rows)
    assert sum("cobalt-owl-74" in row["content"] for row in rows) == 1
    assert all("Earlier state" not in row["content"] or "Late state" not in row["content"] for row in rows)

    context = memory.query("What is the recovery code?")
    assert any("cobalt-owl-74" in item["value"] for item in context)


def test_v2_adapter_flattens_common_trajectory_shapes_without_model_calls():
    assert _trajectory_text({"text": "direct record"}) == "direct record"
    assert "action: click export" in _trajectory_text({"steps": [{"action": "click export"}]})


def test_v2_adapter_flattens_official_states_and_legacy_content_shapes():
    state_text = _trajectory_text({
        "states": [{
            "accessibility_tree": "button Export",
            "text": "Billing page",
            "action": "click export",
            "thought": "look for export",
            "thoughts": ["confirm result"],
            "url": "https://example.test/billing",
        }],
    })
    assert "accessibility_tree: button Export" in state_text
    assert "thought: confirm result" in state_text
    assert "url: https://example.test/billing" in state_text

    legacy_text = _trajectory_text({
        "content": [{
            "observation": {"text": "Export is in the Billing toolbar."},
            "action": "click toolbar export",
        }],
    })
    assert "observation.text: Export is in the Billing toolbar." in legacy_text
    assert "action: click toolbar export" in legacy_text


def test_v2_adapter_enforces_injected_reader_token_budget_and_exposes_metadata():
    def character_tokens(text):
        return len(text)

    memory = EngraphisLongMemEvalV2Memory(
        context_k=3,
        max_context_tokens=24,
        tokenizer=character_tokens,
        tokenizer_identity="test.characters.v1",
        retrieval_profile="lexical",
    )
    memory.insert({"id": "long", "text": "export button " * 30})
    context = memory.query("where is the export button?", query_image="ignored")
    assert sum(character_tokens(item["value"]) for item in context) <= 24
    query_metadata = memory.post_query_hook(
        query="where is the export button?",
        query_image=None,
        memory_context=context,
    )
    assert query_metadata["returned_context_tokens"] <= 24
    assert query_metadata["returned_context_items"] == len(context)
    assert query_metadata["tokenizer"] == "test.characters.v1"
    assert "query" not in query_metadata
    assert memory.metadata == {
        "memory_type": "engraphis",
        "context_k": 3,
        "max_context_tokens": 24,
        "budget_curve_status": "single_operating_point",
        "required_budget_matrix": [256, 512, 1024, 2048, 4096],
        "tokenizer": "test.characters.v1",
        "token_budget_method": "deterministic_estimate",
        "token_budget_scope": "per_context_item_content_excluding_prompt_framing",
        "retrieval_profile": "lexical",
        "embed_model": "deterministic",
        "embed_revision": None,
        "vector_backend": "numpy",
        "response_mode": "compact",
    }


def test_v2_adapter_reports_only_sources_in_reader_budgeted_items(monkeypatch):
    memory = EngraphisLongMemEvalV2Memory(
        context_k=2,
        max_context_tokens=14,
        tokenizer=len,
        tokenizer_identity="test.characters.v1",
    )
    monkeypatch.setattr(
        memory.service,
        "recall",
        lambda *_args, **_kwargs: {
            "context": "[1] first item\n\n[2] second item",
            "packed_sources": [{"id": "mem_first"}, {"id": "mem_second"}],
            "usage": {},
        },
    )

    items = memory.query("which item?")
    metadata = memory.post_query_hook(
        query="which item?", query_image=None, memory_context=items,
    )

    assert [item["value"] for item in items] == ["[1] first item"]
    assert metadata["source_ids"] == ["mem_first"]


def test_v2_adapter_returns_separate_context_items_for_official_prefix_truncation():
    items = _context_items_with_budget(
        "[1] first\nalpha\n\n[2] second\nbeta",
        budget=31,
        count=len,
    )

    assert [item["type"] for item in items] == ["text", "text"]
    assert items[0]["value"].startswith("[1]")
    assert items[1]["value"].startswith("[2]")
    assert sum(len(item["value"]) for item in items) <= 31


def test_v2_adapter_requires_a_positive_context_budget():
    with pytest.raises(ValueError, match="max_context_tokens"):
        EngraphisLongMemEvalV2Memory(max_context_tokens=0)


def test_v2_adapter_accepts_official_memory_params_and_persists_sqlite_state(tmp_path):
    memory = EngraphisLongMemEvalV2Memory({
        "context_k": 2,
        "max_context_tokens": 96,
        "tokenizer_identity": "official.reader.v1",
        "require_exact_reader_tokenizer": False,
        "reader_tokenizer_model": None,
        "reader_tokenizer_revision": None,
        "retrieval_profile": "lexical",
    })
    assert memory.memory_params == {
        "context_k": 2,
        "max_context_tokens": 96,
        "tokenizer_identity": "official.reader.v1",
        "require_exact_reader_tokenizer": False,
        "reader_tokenizer_model": None,
        "reader_tokenizer_revision": None,
        "retrieval_profile": "lexical",
        "embed_model": None,
        "embed_revision": None,
        "vector_backend": "numpy",
    }
    memory.insert({"id": "saved", "text": "The export button is in the billing toolbar."})
    memory.save_memory(tmp_path)
    assert (tmp_path / "engraphis.sqlite").is_file()
    saved_config = json.loads((tmp_path / "memory_config.json").read_text(encoding="utf-8"))
    restored = EngraphisLongMemEvalV2Memory(saved_config["memory_params"])
    restored._load_backend(tmp_path)
    assert "billing toolbar" in restored.query("where is the export button?")[0]["value"]


def test_checked_in_official_memory_config_pins_embedding_backend(monkeypatch):
    config_path = Path(__file__).resolve().parents[1] / "eval" / "configs" / "longmemeval_v2_engraphis.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["memory_type"] == "engraphis"
    created = {}

    def create_service(path, **kwargs):
        created.update({"path": path, **kwargs})
        return SimpleNamespace(engine=SimpleNamespace(embedder=SimpleNamespace(
            model_name=kwargs["embed_model"],
            revision=kwargs["embed_revision"],
        )))

    class ExactTokenizer:
        def encode(self, text):
            return list(text)

    tokenizer_request = {}

    def load_tokenizer(model, revision):
        tokenizer_request.update({"model": model, "revision": revision})
        return ExactTokenizer()

    monkeypatch.setattr("eval.longmemeval_v2.MemoryService.create", create_service)
    monkeypatch.setattr("eval.longmemeval_v2._load_pinned_reader_tokenizer", load_tokenizer)
    memory = EngraphisLongMemEvalV2Memory(config["memory_params"])
    assert memory.max_context_tokens == 1024
    assert memory.retrieval_profile == "balanced"
    assert config["memory_params"]["require_exact_reader_tokenizer"] is True
    assert config["memory_params"]["reader_tokenizer_model"] == "Qwen/Qwen3.5-9B"
    assert config["memory_params"]["reader_tokenizer_revision"] == (
        "c202236235762e1c871ad0ccb60c8ee5ba337b9a"
    )
    assert config["memory_params"]["tokenizer_identity"] != "engraphis.regex.v1"
    assert memory.embed_model == "Qwen/Qwen3-Embedding-8B"
    assert memory.embed_revision == "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af"
    assert created["embed_model"] == memory.embed_model
    assert created["embed_revision"] == memory.embed_revision
    assert created["vector_backend"] == "numpy"
    assert memory.require_exact_reader_tokenizer is True
    assert memory.metadata["token_budget_method"] == "pinned_reader_content_tokenizer"
    assert memory.metadata["budget_curve_status"] == "single_operating_point"
    assert tokenizer_request == {
        "model": "Qwen/Qwen3.5-9B",
        "revision": "c202236235762e1c871ad0ccb60c8ee5ba337b9a",
    }


def test_v2_adapter_rejects_silent_deterministic_embedding_fallback(monkeypatch):
    params = {
        "embed_model": "Qwen/Qwen3-Embedding-8B",
        "embed_revision": "1d8ad4ca9b3dd8059ad90a75d4983776a23d44af",
    }
    fallback = SimpleNamespace(
        engine=SimpleNamespace(embedder=SimpleNamespace())
    )
    monkeypatch.setattr(
        "eval.longmemeval_v2.MemoryService.create",
        lambda *args, **kwargs: fallback,
    )

    with pytest.raises(RuntimeError, match="canonical fallback is forbidden"):
        EngraphisLongMemEvalV2Memory(params)


def test_v2_adapter_fails_closed_when_canonical_reader_tokenizer_is_not_verified(monkeypatch):
    params = {
        "require_exact_reader_tokenizer": True,
        "reader_tokenizer_model": "Qwen/Qwen3.5-9B",
        "reader_tokenizer_revision": "a" * 40,
        "tokenizer_identity": "Qwen/Qwen3.5-9B@" + "a" * 40,
    }

    def unavailable(model, revision):
        raise ValueError("reader tokenizer unavailable")

    monkeypatch.setattr("eval.longmemeval_v2._load_pinned_reader_tokenizer", unavailable)
    with pytest.raises(ValueError, match="reader tokenizer unavailable"):
        EngraphisLongMemEvalV2Memory(params)

    with pytest.raises(ValueError, match="requires reader_tokenizer_model"):
        EngraphisLongMemEvalV2Memory({"require_exact_reader_tokenizer": True})
    with pytest.raises(ValueError, match="requires the pinned reader tokenizer identity"):
        EngraphisLongMemEvalV2Memory(
            params,
            tokenizer_identity="engraphis.regex.v1",
        )
    with pytest.raises(ValueError, match="does not accept an injected replacement"):
        EngraphisLongMemEvalV2Memory(
            params,
            tokenizer=lambda text: len(text),
        )


def test_v2_adapter_requires_immutable_embedding_revision():
    with pytest.raises(ValueError, match="immutable"):
        EngraphisLongMemEvalV2Memory(
            embed_model="Qwen/Qwen3-Embedding-8B",
            embed_revision="main",
        )


def test_v2_adapter_registers_as_an_official_memory_module_when_available(tmp_path):
    package = tmp_path / "memory_modules"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "memory.py").write_text(
        "MEMORY_TYPES = {}\n"
        "class Memory:\n"
        "    def __init__(self, memory_params): self.memory_params = dict(memory_params)\n"
        "    def configure_runtime(self, **kwargs): pass\n"
        "def register_memory(cls):\n"
        "    MEMORY_TYPES[cls.memory_type] = cls\n"
        "    return cls\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(tmp_path), str(root)])
    program = "\n".join([
        "import memory_modules.memory as official",
        "import eval.longmemeval_v2 as adapter",
        "assert adapter.OFFICIAL_MEMORY_AVAILABLE is True",
        "memory_cls = official.MEMORY_TYPES['engraphis']",
        "assert issubclass(memory_cls, official.Memory)",
        "memory = memory_cls({'context_k': 3, 'max_context_tokens': 64})",
        "assert memory.memory_params['context_k'] == 3",
    ])
    subprocess.run([sys.executable, "-c", program], cwd=root, env=env, check=True)


def test_official_runner_requires_the_exact_pinned_checkout(monkeypatch, tmp_path):
    module = SimpleNamespace(__file__=str(tmp_path / "memory.py"))
    commands = []

    def pinned_check_output(command, **kwargs):
        commands.append(command)
        if command[-1] == "--show-toplevel":
            return str(tmp_path) + "\n"
        return PINNED_LONGMEMEVAL_V2_REVISION + "\n"

    monkeypatch.setattr("eval.run_longmemeval_v2.subprocess.check_output", pinned_check_output)
    verify_official_checkout(module)
    assert commands

    def mismatched_check_output(command, **kwargs):
        if command[-1] == "--show-toplevel":
            return str(tmp_path) + "\n"
        return "a" * 40 + "\n"

    monkeypatch.setattr("eval.run_longmemeval_v2.subprocess.check_output", mismatched_check_output)
    with pytest.raises(SystemExit, match="revision mismatch"):
        verify_official_checkout(module)


def test_official_runner_verifies_checkout_before_registering_or_delegating(monkeypatch):
    memory_module = SimpleNamespace(__file__="C:/official/memory_modules/memory.py")
    calls = []

    def import_module(name):
        calls.append(("import", name))
        return memory_module if name == "memory_modules.memory" else object()

    monkeypatch.setattr(run_longmemeval_v2.importlib, "import_module", import_module)
    monkeypatch.setattr(
        run_longmemeval_v2,
        "verify_official_checkout",
        lambda module: calls.append(("verify", module)),
    )
    monkeypatch.setattr(
        run_longmemeval_v2.runpy,
        "run_module",
        lambda name, run_name: calls.append(("run", name, run_name)),
    )
    monkeypatch.setattr(
        run_longmemeval_v2,
        "pin_official_reader_processor",
        lambda: (lambda: calls.append(("restore_reader",))),
    )

    run_longmemeval_v2.main()

    assert calls == [
        ("import", "memory_modules.memory"),
        ("verify", memory_module),
        ("import", "eval.longmemeval_v2"),
        ("run", "evaluation.harness", "__main__"),
        ("restore_reader",),
    ]


def test_official_runner_forces_the_reader_processor_to_the_pinned_revision(monkeypatch):
    requests = []

    class FakeProcessor:
        @classmethod
        def from_pretrained(cls, model, *args, **kwargs):
            requests.append((model, args, kwargs))
            return object()

    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(AutoProcessor=FakeProcessor))
    restore = pin_official_reader_processor()
    try:
        FakeProcessor.from_pretrained(PINNED_READER_MODEL)
        assert requests == [
            (PINNED_READER_MODEL, (), {"revision": PINNED_READER_REVISION})
        ]
        with pytest.raises(RuntimeError, match="outside the canonical profile"):
            FakeProcessor.from_pretrained(PINNED_READER_MODEL, revision="a" * 40)
        with pytest.raises(RuntimeError, match="only the configured reader processor"):
            FakeProcessor.from_pretrained("different-reader")
    finally:
        restore()
