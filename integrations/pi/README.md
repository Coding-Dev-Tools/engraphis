# Engraphis for Pi

`@engraphis/pi` is the first-party [Pi](https://pi.dev) extension for durable,
local-first Engraphis memory. It lazily launches the existing `engraphis-mcp`
server on stdio when a memory tool is used, and exposes the same six-tool Smart
MCP surface as native Pi tools. This keeps the extension zero-configuration:
routine memory work is direct, while advanced capabilities are discovered and
executed automatically through the gateway.

It exposes the Smart MCP tools as direct Pi tools:

- `engraphis_session`
- `engraphis_recall_context`
- `engraphis_remember`
- `engraphis_discover_actions`
- `engraphis_execute_read`
- `engraphis_execute_action`

For an advanced need, Pi calls `engraphis_discover_actions` and then uses the
returned capability ID and schema digest with `engraphis_execute_read` or
`engraphis_execute_action`. No profile, tool allowlist, or manual switch to the
Classic server is required. The gateway validates the capability again before it
runs it.

## Install

Install Engraphis 1.4.x with Python 3.10 or later. Version 1.4.0 introduced the
six-tool Smart MCP contract required by this extension:

```bash
python -m pip install --upgrade "engraphis[mcp]>=1.4.0,<2"
```

When published, install the Pi package:

```bash
pi install npm:@engraphis/pi
```

The extension is tested with Pi 0.83.x, Node 22.19 or later, and Engraphis
1.4.x. Pi supplies its own Pi and TypeBox runtime modules, following Pi's package
contract; the extension checks the required Smart MCP tool names when it opens
the local server and reports an actionable compatibility error if they are absent.

Pin, update, or remove the npm package with Pi's package manager:

```bash
pi install npm:@engraphis/pi@0.1.0
pi update npm:@engraphis/pi
pi remove npm:@engraphis/pi
```

For development from this checkout:

```bash
pi install /absolute/path/to/engraphis/integrations/pi
```

Restart Pi and open `/extensions` to verify that `@engraphis/pi` is loaded. The
extension launches its local MCP bridge on demand; it does not add a project MCP configuration.

## Configuration

Set `ENGRAPHIS_DB_PATH` in the environment that starts Pi so its memories use
the same local database as the dashboard and other agents:

```bash
export ENGRAPHIS_DB_PATH="$HOME/.local/share/engraphis/engraphis.db"
pi
```

PowerShell:

```powershell
$env:ENGRAPHIS_DB_PATH = "$HOME\AppData\Local\Engraphis\engraphis.db"
pi
```

If `engraphis-mcp` is not on `PATH`, set `ENGRAPHIS_MCP_COMMAND` to its absolute
console-script path before launching Pi. The extension deliberately does not write
project MCP configuration files or embed database paths and credentials in source.

Set `ENGRAPHIS_WORKSPACE` and (optionally) `ENGRAPHIS_REPO` to provide default scopes
for routine Smart tools. Model-supplied values always take precedence.

## Trust model

Like every Pi extension, this code runs with your local user permissions. Install
only the official package or a reviewed checkout; `ENGRAPHIS_MCP_COMMAND` should
likewise point only to a trusted local executable.

Every advanced state-changing action requires an explicit Pi confirmation dialog.
The extension fails closed in non-interactive Pi modes that cannot present that
dialog, and consumes each discovered action capability after one approval attempt.
Routine session and pending-review memory writes remain available directly.

Pi supplies the Pi and TypeBox runtime modules. The package deliberately declares
them as optional peers, so installing `@engraphis/pi` does not add a duplicate Pi
runtime to your extension directory.

Engraphis MCP writes enter the normal pending-review boundary. A successful
`engraphis_remember` call does not make unreviewed text prompt-eligible; approve it
through the Engraphis dashboard or interactive approval command before expecting it
in normal recall. This behavior is intentional and unchanged by the Pi extension.

## Development

```bash
npm install --ignore-scripts
npm run verify
```

`npm run verify` type-checks the package, runs its configuration tests, and previews
the publish tarball. The package pins the MCP SDK; update it only with a compatibility
test against the supported Pi and Engraphis releases.
