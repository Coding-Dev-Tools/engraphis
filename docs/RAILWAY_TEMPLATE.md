# Railway template publication runbook

[`deploy/railway-template.json`](../deploy/railway-template.json) is a reviewable composer
worksheet and the source of truth for the public Railway template. It is not a
Railway-importable JSON schema: enter or verify these settings in Railway's template
composer (or generate a template from the matching staging project). `railway.json`
configures a service after creation; it is not itself a marketplace template.

## Required template shape

- Source: `Coding-Dev-Tools/engraphis`, branch `main`, `Dockerfile` build.
- Service mode: `customer`.
- Persistent volume: `/data`.
- Health check: `/api/ready`.
- Public domain references:
  `ENGRAPHIS_DASHBOARD_URL=https://${{RAILWAY_PUBLIC_DOMAIN}}` and the same value for
  `ENGRAPHIS_RELAY_URL`.
- Managed license service: `ENGRAPHIS_CLOUD_URL=https://license.engraphis.com`.
- Generated ownership secret: `ENGRAPHIS_DEPLOYMENT_TOKEN=${{ secret(48) }}`. Copy it
  into the hosted onboarding wizard, then seal it after the first admin is created.

Vendor signer, Polar, vendor-admin, and Engraphis-operated email secrets must not appear in
the template.

## Publish gate

1. Run `python scripts/check_commercial_manifest.py` and the complete repository CI gate.
2. Create the Railway template from a staging project matching the descriptor exactly.
3. Deploy it from a logged-out Railway account and complete the acceptance checklist in
   [`HOSTING_RAILWAY.md`](HOSTING_RAILWAY.md).
4. Record the assigned `https://railway.com/deploy/<code>` URL.
5. Add that exact URL to the README only after the logged-out test succeeds.

Do not substitute `railway.app/new?template=<raw railway.json>`; Railway ignores that shape
and opens a generic project chooser.
