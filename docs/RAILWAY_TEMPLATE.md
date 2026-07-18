# Publishing the Engraphis Railway template

This is the spec for a real, publishable Railway template. It exists because the
"Deploy on Railway" button that used to be in the README **did not work**, and the
replacement has to be created through Railway's UI — it cannot be committed as a file.

## Why the old button failed

```
https://railway.app/new?template=https://raw.githubusercontent.com/.../railway.json
```

Railway ignores `?template=<url>`. Verified 2026-07-18 by loading that exact URL: it
renders the generic **New Project** chooser, and the `/new/template?template=<repo>`
variant renders the public template marketplace. Neither one references Engraphis.

The confusion is understandable — both files are called "template-ish" — but they are
different objects:

| | `railway.json` (in this repo) | A Railway *template* |
|---|---|---|
| What it is | Per-service build + deploy config | A publishable project blueprint |
| Where it lives | The repo | Railway's servers, with a template code |
| Can declare env vars | No | Yes, with descriptions + defaults |
| Can declare a volume | No | Yes |
| Referenced by | Railway, after a service exists | `railway.com/deploy/<code>` |

So `railway.json` is correct and should stay; it just cannot pre-configure anything a
new user needs. Everything the operator must supply had to be done by hand — which is
exactly the friction a one-click button is supposed to remove.

## What the template must declare

Create it at **Railway → project → Settings → Create Template**, from this repo, then
add the following. The Dockerfile already bakes `ENGRAPHIS_PORT`, `ENGRAPHIS_DB_PATH`,
`HF_HOME`, and `ENGRAPHIS_STATE_DIR`; `ENGRAPHIS_HOST` is derived at runtime by
`docker-entrypoint.sh`. None of those belong in the template.

### Volume (required)

| Mount path | Why |
|---|---|
| `/data` | Holds `engraphis.db`, `.engraphis/` (activated key, machine id, trial state, **revocation registry**), and the cached embedding model. Without it every redeploy loses the license and re-downloads the model into the healthcheck race. |

Offer at least 3 GiB by default: the relay permits 2 GiB per account, and the database,
registry, and model cache require headroom on the same volume.

### Variables

| Variable | Required | Default to offer | Description to show the user |
|---|---|---|---|
| `ENGRAPHIS_FORWARDED_ALLOW_IPS` | yes | `*` | Trust Railway's proxy for client scheme/IP. Without it, session cookies don't get the `Secure` flag and per-IP limits misread the caller. |
| `ENGRAPHIS_DASHBOARD_URL` | yes | `https://${{RAILWAY_PUBLIC_DOMAIN}}` | Public URL of this instance. Used for invite and password-reset links and for the MCP allowed-hosts list. |
| `ENGRAPHIS_RELAY_URL` | yes | `https://${{RAILWAY_PUBLIC_DOMAIN}}` | This deployment's relay URL for local Pro clients; do not substitute the vendor hostname for a customer deployment. |
| `ENGRAPHIS_CLOUD_URL` | yes | `https://team.engraphis.com` | Managed issuer used for revocable leases, hosted trials, and fallback invite delivery while sync uses this customer deployment. |
| `ENGRAPHIS_LICENSE_KEY` | no | *(empty)* | Your Pro/Team key. A confirmed hosted trial key must be copied here and redeployed before first-admin setup. |
| `ENGRAPHIS_API_TOKEN` | yes | *(user-generated secret)* | Proof of deployment ownership during hosted trial and remote first-admin setup, and an optional service credential afterward. The setup fields ask for it; it may be removed after the admin exists if no service automation uses it. |

### Vendor-only — do NOT put these in the public template

`ENGRAPHIS_VENDOR_SIGNING_KEY`, `ENGRAPHIS_VENDOR_ADMIN_TOKEN`,
`ENGRAPHIS_RELAY_PUBLIC_URL`, `POLAR_WEBHOOK_SECRET`, `POLAR_ORGANIZATION_ID`,
`ENGRAPHIS_RESEND_API_KEY`, `ENGRAPHIS_RELAY_DB`, `ENGRAPHIS_LEASE_TTL_HOURS`.

These belong only to the machine that *sells* Engraphis (team.engraphis.com). A
customer's self-hosted instance is a client of that relay, never a second issuer — it
does not hold the vendor private key and could not sign a valid lease anyway.

## After publishing

Railway issues a template code. Put the button back in `README.md`, replacing the
"Deploy on Railway (5-minute guide)" link:

```markdown
[![Deploy on Railway](https://railway.com/button.svg)](https://railway.com/deploy/<TEMPLATE_CODE>)
```

Then verify it the way the broken one should have been verified: open the URL in a
logged-out browser and confirm the page names Engraphis and lists the `/data` volume and
the two required variables. A template that renders the generic project chooser is not
deployed correctly, regardless of what the URL looks like.
