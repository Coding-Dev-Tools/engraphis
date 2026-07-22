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
3. A member accepts the invitation and creates a scoped agent/device credential.
4. The member configures their agent with the hosted URL and the one-time credential.
5. The hosted service rechecks organization membership, role, scopes, entitlement version, and
   workspace binding on every request.

Members consume named seats; devices do not. Disabling a member or releasing their seat revokes
their hosted access without distributing an account-wide license key.

The hosted onboarding flow provides the exact endpoint and client snippet for the member's
organization. Do not substitute the URL of a public self-hosted image: that image intentionally
has no Team identity backend.

## Credential lifecycle

Hosted access uses short-lived access tokens plus rotating refresh credentials. A refresh family
has an absolute lifetime and rotation never extends it. Only credential hashes are stored by the
service; the raw replacement is returned once and must be kept in an owner-only local state file
or secrets manager.

Customer-side environment variables are documented in [`.env.example`](../.env.example). Prefer
the onboarding-created `~/.engraphis/cloud_session.json` over long-lived environment secrets.

## Trial and grace

The no-card Team trial starts after email confirmation and lasts **exactly 3 active days**.
`workspace_write_grace` is a distinct local availability state, capped at **24 hours**. It never
extends the trial, hosted agent access, Team membership, seats, Cloud Sync, or managed compute.

## Security notes

- Use only the HTTPS endpoint shown by the official hosted dashboard.
- Never put refresh credentials, access tokens, or account keys in a repository or support log.
- Bind every hosted credential to the intended organization and workspace.
- Give automation the minimum scopes it needs and revoke unused devices.
- Keep local and hosted responsibilities clear: the public client transports authorized
  requests; the private control plane owns identity, seats, policy, and revocation.

See [Licensing](LICENSING.md) for the source/service boundary and [Cloud Sync](SYNC.md) for the
relay client contract.
