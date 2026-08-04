# Engraphis for Hermes

`engraphis/` is a native Hermes memory-provider plugin. Hermes discovers copied
providers in `~/.hermes/plugins/<name>/`; this repository does not install the
plugin or change Hermes configuration automatically.

Install Engraphis into the Python environment that Hermes uses, copy this provider,
then choose it in Hermes:

```bash
~/.hermes/hermes-agent/venv/bin/python -m pip install engraphis
cp -r integrations/hermes/engraphis ~/.hermes/plugins/engraphis
hermes memory setup
hermes memory status
```

Select `engraphis` in the picker. The provider automatically recalls approved,
scoped memories before turns and records bounded turn history locally. Its direct
tools are `engraphis_search`, `engraphis_store`, and `engraphis_erase`.

By default, it uses the dependency-free local embedder if no cached local semantic
model is available. It never downloads a model. To use an installed local model,
set `ENGRAPHIS_HERMES_EMBED_MODEL` to `local:/absolute/model/path` or to a cached
model identifier before starting Hermes. Set it to `deterministic` to force lexical
hashing.

The adapter reads the standard `ENGRAPHIS_DB_PATH` and can share that local database
with the dashboard and MCP server. Scope defaults are deliberately narrow and can be
configured before launch:

```bash
export ENGRAPHIS_HERMES_WORKSPACE=personal
export ENGRAPHIS_HERMES_REPO=my-project
```

For encrypted storage, configure Engraphis's existing SQLCipher option in the Hermes
environment before launch. Secrets are rejected at write time. `engraphis_erase` maps
to Engraphis's audited secure erase operation, which permanently removes a selected
record and leaves a sync tombstone so it is not restored by a later sync.
