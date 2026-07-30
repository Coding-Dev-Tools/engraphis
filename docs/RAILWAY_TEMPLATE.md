# Railway customer-node publication runbook

Any Railway template published from this repository must be described as a **free single-user
customer node**. It must not claim to deploy Engraphis Cloud, Pro/Team server features, a license
issuer, relay, managed compute, Auto Dreaming, Auto Consolidation, or Team identity.

## Required template shape

- Source: `Coding-Dev-Tools/engraphis`, branch `main`, `Dockerfile` build.
- Service mode: `customer`.
- Persistent volume: `/data`.
- Health check: `/api/ready`.
- `ENGRAPHIS_DASHBOARD_URL` derived from Railway's generated public domain (override it with the
  canonical HTTPS custom domain once one is active so public MCP origin checks remain strict).
- Generated local API bearer supplied as `ENGRAPHIS_API_TOKEN`.
- No vendor signer, billing, mail, Team-admin, relay-storage, or worker secrets.

Hosted customer endpoint variables may be exposed as optional inputs, but a refresh credential
must be injected as a secret or mounted owner-only state file. Managed compute is enabled by
default once the deployed installation is connected to Engraphis Cloud; connecting accepts the
terms that cover it, and a local-only node with no cloud session is never allowed. Expose
`ENGRAPHIS_MANAGED_COMPUTE_CONSENT` only as an optional operator override (`0` to opt a
connected installation back out), not as a required opt-in input.

## Publish gate

1. Run the complete public repository CI gate.
2. Deploy from a logged-out Railway account.
3. Confirm the template copy says “free customer node” and links to official hosted plans.
4. Verify authentication, persistent storage, CSP, and recovery behavior.
5. Add the assigned Railway template URL to the README only after that acceptance test succeeds.

Do not substitute `railway.app/new?template=<raw railway.json>`; `railway.json` is per-service
build configuration, not a marketplace template descriptor.
