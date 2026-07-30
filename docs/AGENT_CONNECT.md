# Agent Connect

The public Engraphis dashboard is a single-user local application. It does not mount Team
accounts, invitations, roles, seats, organization audit, or per-member token administration.
Those capabilities live in **Engraphis Team Cloud**.

## Local agents remain free

For one person on one machine, run the local MCP server. No hosted account is required:

```bash
pip install "engraphis[mcp]"
engraphis-init
claude mcp add engraphis -- engraphis-mcp
```

The local server exposes the same memory semantics while keeping the database on your machine.
Use `ENGRAPHIS_API_TOKEN` only when protecting a local HTTP surface; it is not a Team identity or
seat credential.

## Connect through Team Cloud

Use the official hosted dashboard when several people or remote agents need one managed
organization:

1. The organization owner starts Team or purchases a subscription in Engraphis Cloud.
2. The owner invites named members and assigns roles in the hosted dashboard.
3. An organization owner or admin generates a one-time **connect token** (`engr_ct_…`) for
   their own connected machine. The account portal shows the exact command to run.
4. That owner or admin runs the command on the machine being connected (see below).
5. The hosted service rechecks organization membership, role, scopes, entitlement version, and
   workspace binding on every request.

Members consume named seats; devices do not. Disabling a member or releasing their seat revokes
their hosted access without distributing an account-wide license key.

The hosted onboarding flow provides the exact endpoint and client snippet for the member's
organization. Do not substitute the URL of a public self-hosted image: that image intentionally
has no Team identity backend.

## Connect a machine: `engraphis connect`

Copy the command from your account portal and run it on the machine you are connecting:

```bash
engraphis connect --token engr_ct_...
```

That redeems the token against `POST /v1/devices/connect` on the control plane and writes the
owner-only session file `~/.engraphis/cloud_session.json` (mode `0600`). The dashboard, the MCP
server, and Cloud Sync all read that file, so no environment secret is needed afterwards. Rerun
`engraphis connect` with a fresh token on every machine you want connected.

Useful options:

| Option | Effect |
| --- | --- |
| `--token -` | Read the token from stdin, so it never enters shell history. |
| `--workspace WS_ID` | Bind this device to a single workspace. |
| `--label TEXT` | Name this installation in your account portal. |
| `--device-name TEXT` | Override the device name (defaults to the hostname). |
| `--control-url URL` | Point at a non-default control plane. |
| `--compute-url URL` | Set the managed compute endpoint (also `ENGRAPHIS_CLOUD_COMPUTE_URL`). |
| `--json` | Print a redacted, machine-readable summary. |

The same command is installed as `engraphis-connect`, matching the other `engraphis-*` scripts.

Connect tokens are **single-use and short-lived**. The service answers every refusal, whether expired,
already redeemed, or never valid, with the same `401`, so the client reports all three
possibilities and the fix is always the same: generate a new token in the account portal. A `402`
means the subscription itself has lapsed; fix billing rather than the token.

Because the token is single-use, the client checks that it can actually write the session file
*before* redeeming it. If the state directory is not writable, or `cloud_session.json` has been
replaced by a symlink, a hard link, or a directory, the command fails immediately, names the path
to fix, and sends nothing. Your token is untouched, so you can correct the path and rerun the
same command rather than issuing a new token.

The token is a credential. It is sent in the request body and nowhere else; it is never
printed, never logged, and never written to disk. What *is* written is the rotating refresh
credential the service returns, which is why the session file is owner-only.

The command also mints a stable per-installation identity at `~/.engraphis/client_identity.json`
(random ULIDs, not a hardware fingerprint) so reconnecting the same machine updates the existing
installation instead of registering a new device every time. Both files honour
`ENGRAPHIS_STATE_DIR`; a distinct state directory is a distinct installation.

## Credential lifecycle

Hosted access uses short-lived access tokens plus rotating refresh credentials. A refresh family
has an absolute lifetime and rotation never extends it. Only credential hashes are stored by the
service; the raw replacement is returned once and must be kept in an owner-only local state file
or secrets manager.

Customer-side environment variables are documented in [`.env.example`](../.env.example). Prefer
the `~/.engraphis/cloud_session.json` that `engraphis connect --token` writes over long-lived
environment secrets: it holds a rotating credential, it is owner-only, and it is the path the
client keeps up to date on its own. Environment secrets are for non-interactive deployments that
cannot run the connect command.

## Trial and grace

The no-card Team trial starts after email confirmation and lasts **exactly 3 active days**.
`workspace_write_grace` is a private-control-plane account-continuity state capped at **24
hours**. It never extends the trial, hosted agent access, Team membership, seats, Cloud Sync, or
managed compute, and it does not restrict the free local MCP server.

## Security notes

- Use only the HTTPS endpoint shown by the official hosted dashboard.
- Never put refresh credentials, access tokens, or account keys in a repository or support log.
- Bind every hosted credential to the intended organization and workspace.
- Give automation the minimum scopes it needs and revoke unused devices.
- Keep local and hosted responsibilities clear: the public client transports authorized
  requests; the private control plane owns identity, seats, policy, and revocation.

See [Licensing](LICENSING.md) for the source/service boundary and [Cloud Sync](SYNC.md) for the
relay client contract.
