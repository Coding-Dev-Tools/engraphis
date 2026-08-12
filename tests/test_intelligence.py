from engraphis.engines import intelligence


def test_auto_categorize_preserves_nested_split_objects(monkeypatch):
    response = (
        'Here is the classification:\n'
        '```json\n'
        '{"memory_type":"semantic","confidence":0.9,"should_split":true,'
        '"reason":"two facts","splits":[{"title":"One","content":"A",'
        '"memory_type":"semantic","metadata":{"source":"llm"}}]}\n'
        '```\n'
    )

    class FakeLLM:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def chat(self, *_args, **_kwargs):
            return response

    monkeypatch.setattr(intelligence, "LLMClient", FakeLLM)

    result = intelligence.auto_categorize("A and B")

    assert result["should_split"] is True
    assert result["splits"] == [{
        "title": "One",
        "content": "A",
        "memory_type": "semantic",
        "metadata": {"source": "llm"},
    }]
