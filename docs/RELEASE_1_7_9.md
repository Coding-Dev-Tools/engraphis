# Engraphis 1.7.9 release preparation

This candidate packages the reviewed managed Jev client and graph fixes from
Core PRs #238 through #241. Its base is
`f10dbc40de2d8bb2b0b7ab9bddb4870f6115a64c`. Preparing a version and validating its
source does not publish it or enable the Cloud service. The existing 1.7.8
release remains immutable.

## Customer upgrade after publication

Managed Jev requires both this client and an enabled, qualified Cloud deployment.
The published 1.7.8 distribution contains an experimental injected adapter but
does not ship the managed transport or the registered MCP decision tool.

1. Upgrade the Python environment that actually launches MCP. For an MCP
   installation, run `python -m pip install --upgrade "engraphis[mcp]==1.7.9"`
   after this version is published. Preserve any other extras your installation
   needs. Check the installation with `python -m pip show engraphis`.
2. Keep an existing valid saved Cloud session. If the installation is not
   connected, use the account portal's generated `engraphis connect` command in
   that same environment. Never copy a provider key into a customer installation.
3. Set `ENGRAPHIS_DECISION_BACKEND=managed` in the process environment or the
   owner-private `~/.engraphis/config.env`. A searched working-directory `.env`
   is not a trusted configuration source. Restart the MCP host.
4. Classic MCP exposes `engraphis_decide`. In Smart MCP, discover the decision
   action and execute it using the returned schema and `engraphis_execute_action`.
   Every remote request requires literal `allow_remote=true` and a
   `data_classification` of `public` or `internal`. Review the text before sending
   it. Requests classified as secret and recognized secret patterns are rejected;
   `offline_mode=true` prevents remote execution. Pattern filtering cannot detect
   every secret in arbitrary prose.

The service enforces current membership, entitlement, monthly allowance and a
separate fleet cap. A local success, fallback, health response or configured
backend does not establish a successful managed provider request. The account
portal reports availability and usage. Jev advice does not authorize actions or
replace deterministic checks.

## Qualification and publication sequence

1. Freeze the final reviewed source and give all package, plugin and commercial
   version surfaces the same unused version, 1.7.9. Preserve historical evidence;
   generate new offline fixtures and skill checksums for these bytes.
2. Pass the required CI checks on the final commit. The release workflow checks
   that the package version matches `v1.7.9` and that its commit is on protected
   `main`. Merge and tag creation remain release-owner actions.
3. Build and retain the exact wheel and source archive. Verify their manifests,
   dependency/security results, installed-client journeys and reproducibility.
   Local candidate builds are preparation; the final publication artifacts must
   match the signed qualification after the final source is selected.
4. Complete the private full-product ledger, including the selected Cloud/site
   identities, staging acceptance, recovery/rollback evidence and real Pro/Team
   saved-session Jev journeys. Run `check_release_readiness.py --require-release`
   with that ledger and its actual evidence. An unresolved gate stays unresolved.
5. The existing owner-controlled release authority must review the completed
   ledger and issue a fresh, time-bounded Ed25519 qualification for the exact
   commit, distribution hashes, candidate ID and ledger digest. Keep private-key
   custody outside this repository, workflow artifacts and runners. Preparing
   this branch does not create a signing key, select an approver or grant approval.
6. Supply the four protected `release-qualification` environment secrets required
   by [the qualification contract](RELEASE_QUALIFICATION.md), and obtain its
   configured authorized review. Verify the envelope with
   `python -m scripts.verify_release_qualification --dist DIST --commit COMMIT --tag v1.7.9`
   against those independently selected identities before publication.
7. Publish through the existing release workflow. Check PyPI and GitHub artifact
   hashes against the qualified bytes, then verify a fresh customer upgrade and
   update public availability only from the accepted deployment and client.

The old v1.7.6 and v1.7.8 waivers apply only to their named retained candidates.
They cannot qualify or authorize this release. See [release readiness](RELEASE_READINESS.md)
for the mandatory evidence categories and recovery requirements.
