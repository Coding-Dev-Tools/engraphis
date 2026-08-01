"""Documentation contracts for supported LLM and Command Code integrations."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_single_provider_guide_covers_each_supported_setup():
    hub = _read("docs/LLM_PROVIDERS.md")
    for heading in (
        "## OpenAI",
        "## Anthropic Claude",
        "## Google Gemini",
        "## OpenRouter",
        "## Ollama",
        "## Cohere Command",
        "## Other OpenAI-compatible endpoints",
        "## Command Code",
    ):
        assert heading in hub

    for path in (
        "docs/COMMAND_CODE_INTEGRATION.md",
        "docs/OLLAMA.md",
        "docs/providers/OPENAI.md",
        "docs/providers/ANTHROPIC.md",
        "docs/providers/GOOGLE.md",
        "docs/providers/OPENROUTER.md",
        "docs/providers/COHERE_COMMAND.md",
        "docs/providers/OPENAI_COMPATIBLE.md",
    ):
        assert not (ROOT / path).exists(), path


def test_provider_guides_use_the_runtime_provider_contract():
    hub = _read("docs/LLM_PROVIDERS.md")
    for setting in (
        "ENGRAPHIS_LLM_PROVIDER=openai",
        "ENGRAPHIS_LLM_PROVIDER=anthropic",
        "ENGRAPHIS_LLM_PROVIDER=google",
        "ENGRAPHIS_LLM_PROVIDER=openrouter",
    ):
        assert setting in hub

    assert hub.count("ENGRAPHIS_LLM_PROVIDER=custom") >= 4
    assert "https://api.cohere.ai/compatibility/v1" in hub
    assert "native `cohere`" in hub


def test_provider_hub_documents_the_supported_environment_variables():
    hub = _read("docs/LLM_PROVIDERS.md")
    for name in (
        "ENGRAPHIS_LLM_PROVIDER",
        "ENGRAPHIS_LLM_MODEL",
        "ENGRAPHIS_LLM_API_KEY",
        "ENGRAPHIS_LLM_BASE_URL",
        "ENGRAPHIS_LLM_EXTRA_HEADERS",
    ):
        assert name in hub


def test_command_code_guide_covers_mcp_and_provider_api_boundaries():
    guide = _read("docs/LLM_PROVIDERS.md")

    for content in (
        "cmd mcp add --scope local",
        "cmd mcp list",
        "cmd mcp get engraphis",
        "/mcp",
        "`local`",
        "`project`",
        "`user`",
        "disables MCP tools in plan mode",
        "ENGRAPHIS_LLM_PROVIDER=custom",
        "https://api.commandcode.ai/provider/v1",
        '"x-cmd-zdr":"1"',
        "do not select a Claude model",
    ):
        assert content in guide


def test_readme_and_env_example_link_to_the_provider_guides():
    assert "docs/LLM_PROVIDERS.md" in _read("README.md")
    assert "docs/LLM_PROVIDERS.md#command-code" in _read("README.md")
    assert "docs/LLM_PROVIDERS.md" in _read(".env.example")
