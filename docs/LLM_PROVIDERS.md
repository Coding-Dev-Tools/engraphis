# LLM providers and Command Code

Engraphis runs fully locally by default. An LLM is optional and is used only when you opt into
LLM extraction, cited synthesis, structured consolidation, or retention supervision. Memory
storage, local embeddings, conflict resolution, and recall do not require a provider.

This is the complete provider reference. It also covers Command Code both as an MCP coding agent
and as an optional OpenAI-compatible model provider. Command Code and Cohere Command are distinct
products and use different setup paths.

## Contents

- [Choose a provider](#choose-a-provider)
- [Configure once](#configure-once)
- [OpenAI](#openai)
- [Anthropic Claude](#anthropic-claude)
- [Google Gemini](#google-gemini)
- [OpenRouter](#openrouter)
- [Ollama](#ollama)
- [Cohere Command](#cohere-command)
- [Other OpenAI-compatible endpoints](#other-openai-compatible-endpoints)
- [Command Code](#command-code)

## Choose a provider

| Provider | Engraphis mode | Interface |
| --- | --- | --- |
| OpenAI | `openai` | Native OpenAI Chat Completions |
| Anthropic Claude | `anthropic` | Native Anthropic Messages |
| Google Gemini | `google` | Native Gemini `generateContent` |
| OpenRouter | `openrouter` | OpenAI-compatible Chat Completions |
| Ollama | `custom` | Local OpenAI-compatible endpoint |
| Cohere Command | `custom` | Cohere Compatibility API |
| Another compatible endpoint | `custom` | OpenAI-compatible Chat Completions |
| Command Code Provider API | `custom` | OpenAI-compatible Chat Completions |

## Configure once

1. Add one provider's variables to `.env`.
2. Restart the dashboard, server, or MCP process that owns the shared Engraphis database.
3. In **Settings → Connect an LLM**, select **Test connection**. The dashboard picker offers the
   named cloud modes; custom endpoints are configured directly in `.env`.
4. Keep `ENGRAPHIS_EXTRACTOR=none` for fully local ingestion, or explicitly choose `llm` or
   `llm_structured` after the connection succeeds.

Every LLM setup uses these variables:

| Variable | Purpose |
| --- | --- |
| `ENGRAPHIS_LLM_PROVIDER` | One of `openai`, `anthropic`, `google`, `openrouter`, or `custom`. |
| `ENGRAPHIS_LLM_MODEL` | A model identifier accepted by the selected provider and account. |
| `ENGRAPHIS_LLM_API_KEY` | Credential for the provider. It is never returned by the dashboard. |
| `ENGRAPHIS_LLM_BASE_URL` | Needed only to override a default or configure a compatible endpoint. |
| `ENGRAPHIS_LLM_EXTRA_HEADERS` | Optional JSON object of headers required by a compatible endpoint. |

The sample names below are Engraphis runtime defaults, not provider recommendations. Replace them
when your account or deployment uses a different model.

Selecting a provider creates an egress path only for LLM-powered features you enable. With
`ENGRAPHIS_EXTRACTOR=chunk` or `none` and `ENGRAPHIS_RETENTION_SUPERVISOR=none`, normal ingest
and recall stay local. When extraction, synthesis, structured consolidation, or retention
supervision is enabled, the selected provider must receive the necessary text to perform that work.

Provider errors do not expose API keys, configured endpoint URLs, or raw provider responses in the
dashboard. Features that support a local fallback degrade safely when a provider is unavailable;
confirm a successful connection before depending on LLM extraction in a workflow.

## OpenAI

OpenAI uses the native `openai` mode. Leave `ENGRAPHIS_LLM_BASE_URL` unset unless you deliberately
need a compatible proxy.

```dotenv
ENGRAPHIS_LLM_PROVIDER=openai
ENGRAPHIS_LLM_MODEL=gpt-4o-mini
ENGRAPHIS_LLM_API_KEY=<openai-api-key>
```

For available models and API-key administration, use the [OpenAI API documentation](https://platform.openai.com/docs/overview).

## Anthropic Claude

Anthropic Claude uses the native `anthropic` mode and the Anthropic Messages API. Do not configure
it as `custom`; the native mode applies Anthropic's required request shape and headers.

```dotenv
ENGRAPHIS_LLM_PROVIDER=anthropic
ENGRAPHIS_LLM_MODEL=claude-3-5-sonnet-20241022
ENGRAPHIS_LLM_API_KEY=<anthropic-api-key>
```

Leave `ENGRAPHIS_LLM_BASE_URL` unset for the public API. For model and credential details, see
the [Anthropic API documentation](https://docs.anthropic.com/).

## Google Gemini

Google Gemini uses the native `google` mode and the Gemini `generateContent` API. The native mode
puts the API key and system instruction in the API-specific request fields.

```dotenv
ENGRAPHIS_LLM_PROVIDER=google
ENGRAPHIS_LLM_MODEL=gemini-1.5-flash
ENGRAPHIS_LLM_API_KEY=<google-api-key>
```

Leave `ENGRAPHIS_LLM_BASE_URL` unset for the public Gemini API. A service that merely hosts Google
models is not enough for `custom`; it must implement OpenAI Chat Completions. See the
[Gemini API documentation](https://ai.google.dev/gemini-api/docs) for models and credentials.

## OpenRouter

OpenRouter uses the named `openrouter` mode and its OpenAI-compatible request format.

```dotenv
ENGRAPHIS_LLM_PROVIDER=openrouter
ENGRAPHIS_LLM_MODEL=openai/gpt-4o-mini
ENGRAPHIS_LLM_API_KEY=<openrouter-api-key>
```

Leave `ENGRAPHIS_LLM_BASE_URL` unset for OpenRouter's standard endpoint. Set it only when routing
through a compatible proxy. If that proxy needs extra headers, set
`ENGRAPHIS_LLM_EXTRA_HEADERS` to a JSON object, for example
`{"HTTP-Referer":"https://example.com"}`. See the [OpenRouter documentation](https://openrouter.ai/docs).

## Ollama

Ollama is a local, OpenAI-compatible endpoint. It uses `custom`, not a separate `ollama` runtime
mode. Start Ollama and pull a chat model, then replace `<local-model>` below with an installed
model name.

```dotenv
ENGRAPHIS_LLM_PROVIDER=custom
ENGRAPHIS_LLM_MODEL=<local-model>
ENGRAPHIS_LLM_API_KEY=ollama
ENGRAPHIS_LLM_BASE_URL=http://localhost:11434/v1
```

The key must be non-empty because the custom client requires a bearer token. Default local Ollama
does not authenticate it; use a real proxy token if you place Ollama behind an authenticated proxy.
The base URL ends in `/v1` because Engraphis adds `/chat/completions`. Loopback `http` is allowed
for local services; a non-loopback endpoint must use HTTPS.

## Cohere Command

Cohere Command is a model family, not Command Code. Cohere exposes it through the
OpenAI-compatible Compatibility API, so configure Engraphis with `custom`, not an unsupported
native `cohere` provider value.

```dotenv
ENGRAPHIS_LLM_PROVIDER=custom
ENGRAPHIS_LLM_MODEL=<cohere-command-model>
ENGRAPHIS_LLM_API_KEY=<cohere-api-key>
ENGRAPHIS_LLM_BASE_URL=https://api.cohere.ai/compatibility/v1
```

Choose a Command model available to your Cohere account. The base URL is the Compatibility API
root, so Engraphis appends `/chat/completions`. See Cohere's
[Compatibility API documentation](https://docs.cohere.com/docs/compatibility-api).

## Other OpenAI-compatible endpoints

Use `custom` for a self-hosted gateway or compatibility API that accepts bearer authentication and
returns text at `choices[0].message.content`.

```dotenv
ENGRAPHIS_LLM_PROVIDER=custom
ENGRAPHIS_LLM_MODEL=<provider-model>
ENGRAPHIS_LLM_API_KEY=<provider-api-key>
ENGRAPHIS_LLM_BASE_URL=https://provider.example/v1
# ENGRAPHIS_LLM_EXTRA_HEADERS={"Header-Required-By-Provider":"value"}
```

Set the base URL to the API root before `/chat/completions`; Engraphis appends that final path.
The URL must be absolute, use HTTP or HTTPS, and omit embedded credentials, a query string, and a
fragment. HTTP is accepted only for a loopback endpoint such as a local development service.

The custom client sends a model, system and user messages, plus optional temperature and token
limits. Endpoints that implement another protocol, such as Anthropic Messages, need a matching
native mode or adapter. If a test fails, confirm the base URL, model, credential, required headers,
and request and response shapes.

## Command Code

Command Code and Cohere Command are separate products. There are two ways to combine Command Code
with Engraphis: connect its coding agent to Engraphis over MCP, or use Command Provider as an
optional external LLM for Engraphis. These paths are independent.

### Connect the Command Code agent over MCP

Install the MCP surface and initialize a stable database path once:

```bash
pip install "engraphis[mcp]"
engraphis-init
```

`engraphis-init` records an absolute `ENGRAPHIS_DB_PATH`. Use that same path for the dashboard and
the MCP server so memories written by Command Code appear in the same local store. Add a local
server, replacing the path with the one from initialization:

```bash
cmd mcp add --scope local --env ENGRAPHIS_DB_PATH=/absolute/path/to/engraphis.db engraphis -- engraphis-mcp
```

All Command Code options precede the server name, and `--` separates the name from the stdio
command. `engraphis-mcp` runs locally over stdio; normal local use needs no HTTP endpoint or
Engraphis API key.

| Scope | Use it when | Storage |
| --- | --- | --- |
| `local` | The connection is only for you in this project. This is the recommended first setup. | Command Code's per-project local configuration. |
| `project` | The team should share the server definition. | `.mcp.json` in the repository. Do not commit personal database paths or credentials. |
| `user` | The server should be available in all of your projects. | Your Command Code user configuration. |

Use `cmd mcp add --scope project ...` or `cmd mcp add --scope user ...` for another scope. For a
committed project definition, keep machine-specific `ENGRAPHIS_DB_PATH` values outside the
repository or use a team-managed path that is safe to share.

Verify the connection:

```bash
cmd mcp list
cmd mcp get engraphis
```

Start a normal Command Code session with `cmd`, open `/mcp`, and confirm that `engraphis` is
connected and exposes tools. Then ask Command Code: "Call `engraphis_stats` and show me the
result." A response with memory counts confirms the end-to-end connection.

Command Code disables MCP tools in plan mode. Start a tool-enabled session before expecting it to
call `engraphis_recall`, `engraphis_remember`, or another Engraphis tool. For the broader memory
workflow, use the standalone [MCP tool reference](MCP_TOOLS.md) and Command Code's
[MCP documentation](https://commandcode.ai/docs/mcp).

### Use Command Provider as Engraphis's LLM

This optional setup lets Engraphis call Command Provider for LLM-powered features. It is separate
from the MCP connection above.

```dotenv
ENGRAPHIS_LLM_PROVIDER=custom
ENGRAPHIS_LLM_MODEL=<command-provider-chat-model>
ENGRAPHIS_LLM_API_KEY=<command-provider-api-key>
ENGRAPHIS_LLM_BASE_URL=https://api.commandcode.ai/provider/v1
# ENGRAPHIS_LLM_EXTRA_HEADERS={"x-cmd-zdr":"1"}
```

Choose a Command Provider model that accepts OpenAI Chat Completions. Engraphis's `custom` client
adds `/chat/completions` to the base URL, so do not select a Claude model for this configuration:
Command Provider routes Claude models through its Anthropic Messages endpoint instead.

`x-cmd-zdr: 1` is optional. It requests Command Provider's zero-data-retention routing and can
make a request fail when the selected model has no eligible upstream. Test the connection before
turning on LLM extraction or another provider-backed workflow. See the
[Command Provider API documentation](https://commandcode.ai/docs/provider).
