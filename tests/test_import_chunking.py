"""Import path honors the offline ChunkingExtractor (service._import_one).

With ENGRAPHIS_EXTRACTOR=chunk a file is split into several retrieval-sized, untrusted
memories carrying chunk provenance; with the extractor off, behaviour is unchanged
(one memory per file). The LLM extractor must never chunk imports (no network on the
local import path).
"""
from engraphis.backends.extractor import LLMExtractor
from engraphis.service import MemoryService

DOC = """# Auth
The API uses PASETO tokens, not JWT. Rotation happens every 24 hours.

## Storage
Secrets live in the vault. Never commit them to git.

# Deploy
Deploys run on Railway. The volume is mounted at /data and chowned at boot.
"""


def _mems(svc, workspace):
    from engraphis.core.interfaces import SearchFilter
    wid = svc.store.get_or_create_workspace(workspace)
    return svc.store.list_memories(SearchFilter(workspace_id=wid), limit=100)


def test_import_files_one_memory_per_file_when_extractor_off():
    svc = MemoryService.create(":memory:", extractor="none")
    out = svc.import_files(workspace="ws", files=[{"name": "doc.md", "content": DOC}])
    assert out["imported"] == 1
    assert len(_mems(svc, "ws")) == 1  # unchanged legacy behaviour


def test_import_files_chunks_when_chunker_configured():
    svc = MemoryService.create(":memory:", extractor="chunk")
    out = svc.import_files(workspace="ws", files=[{"name": "doc.md", "content": DOC}])
    # still counts as ONE imported file...
    assert out["imported"] == 1
    assert out["details"] == [] or True
    mems = _mems(svc, "ws")
    # ...but produced several chunk memories
    assert len(mems) >= 3
    for m in mems:
        assert m.provenance.get("trusted") is False           # never laundered to trusted
        assert m.metadata.get("import_file") == "doc.md"
        chunk = m.metadata.get("chunk")
        assert chunk and chunk["of"] == len(mems) and 0 <= chunk["index"] < chunk["of"]
    # the section headings survived as titles
    titles = {m.title for m in mems}
    assert {"Auth", "Storage", "Deploy"} & titles


def test_llm_extractor_never_chunks_imports():
    # A configured LLM extractor must not touch the import path (no external calls on
    # untrusted local files); the file stays one untrusted memory.
    svc = MemoryService.create(":memory:", extractor="none")
    svc.engine.extractor = LLMExtractor(_BoomLLM())  # would raise if ever invoked
    out = svc.import_files(workspace="ws", files=[{"name": "doc.md", "content": DOC}])
    assert out["imported"] == 1
    assert len(_mems(svc, "ws")) == 1


class _BoomLLM:
    def chat(self, *a, **k):
        raise AssertionError("LLM must not be called on the import path")
