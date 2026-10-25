# Release qualification

## What a release requires

Every PyPI publication, GitHub release write and repair in
`.github/workflows/release.yml` requires:

1. **Green release gates.** The tag must match `pyproject.toml` and sit on protected
   `main`, and every build job must pass: the full test suite on Python 3.9–3.14, the
   offline evals, Pyright, CodeQL, the dependency audits, the browser, Pi-extension
   and container gates, the installed-artifact smokes, and the two-builder
   reproducibility check. A repair reuses a retained tag run whose build and
   evidence jobs passed, and re-verifies its exact artifact and evidence hashes.
2. **Approval of the protected `release-qualification` environment.** The
   `publish`, `github-release` and `github-release-repair` jobs always run in this
   environment, so a required reviewer has to approve each one before it can write.

That is the whole requirement for a patch release.

The owner-signed full-product qualification described below is **opt-in**. Set the
repository (or `release-qualification` environment) variable
`REQUIRE_SIGNED_QUALIFICATION` to `true` to switch it on. Once it is on, each of the
four publication-write points also verifies a signed 11-gate envelope before it
writes. Use this for a major release, or for any release that comes with public
claims backed by the pilot, usability or hosted-journey evidence. If the variable is
unset, or set to anything other than `true` (GitHub compares it without regard to
case), the four verification steps are skipped. The verifier then never runs, the
four qualification secrets are not read, and the public release evidence lists
only the automated checks that actually ran. A skipped qualification is not
recorded as a pass.

To turn signed qualification on:

```console
gh variable set REQUIRE_SIGNED_QUALIFICATION --body true --repo Coding-Dev-Tools/engraphis
```

Delete the variable, or set it to `false`, to return to patch-release mode.
Once the variable is `true`, the workflow fails closed if the qualification
configuration is missing, malformed, expired or inconsistent with the selected
source and distribution bytes.

v1.7.6, v1.7.8 and v1.7.9 were published through single-use, owner-authorized
waiver inputs that were hard-coded for those three retained candidates. Those
inputs have been removed, because the opt-in variable now covers the same case
without a workflow change for every release. The release notes for those
versions and the git history keep the disclosures.

## Signed qualification (opt-in)

The public verifier is `scripts/verify_release_qualification.py`. It verifies
Ed25519 signatures using `cryptography==50.0.0` in release jobs. It contains no
signing operation, key generator, private ledger reader or provider integration.
Its input is a content-free approval; private evidence stays with its accountable
owners. A valid signature establishes the configured authority's assertion, not
independent proof that each observation happened.

### Owner setup for signed qualification

Step 1 is required for every release. Steps 2–4 are needed only before you set
`REQUIRE_SIGNED_QUALIFICATION=true`.

1. Create and protect the GitHub environment `release-qualification`. Restrict its
   deployment branches/tags to the protected release sources, require an authorized
   reviewer and protect changes to the workflow and its configuration.
2. Select an owner-controlled Ed25519 signing authority through the existing
   private release procedure. Keep its private key outside the public repository,
   workflow variables, artifacts and runners. Record custody, authorized operators,
   key rotation and revocation in the private release register.
3. Freeze the complete product candidate, validate its private ledger using
   `check_release_readiness.py --require-release`, and review the actual mandatory
   acceptance evidence. Qualify the exact wheel and source archive produced from
   the final engine commit. A build/check-only workflow may produce these artifacts
   before a release tag is published; later builds must reproduce the approved bytes.
4. Use the private authority to issue the envelope below with a bounded UTC
   validity window and explicit release approval. Set these four values as GitHub
   **environment secrets** in `release-qualification` for the selected candidate:

| Variable | Required value |
| --- | --- |
| `ENGRAPHIS_RELEASE_VERIFY_KEY` | Canonical standard base64 of the authority's raw 32-byte Ed25519 public key. |
| `ENGRAPHIS_RELEASE_QUALIFICATION` | The signed JSON envelope below; at most 16 KiB. |
| `ENGRAPHIS_RELEASE_CANDIDATE_ID` | The expected full-product candidate ID from the validated private ledger. |
| `ENGRAPHIS_RELEASE_LEDGER_SHA256` | SHA-256 of the exact private ledger file bytes the owner reviewed. |

The jobs always enter the protected `release-qualification` environment. Only the
on/off switch `REQUIRE_SIGNED_QUALIFICATION` is a variable. Do not use GitHub
Actions variables for the four values above: Actions logs render variable-backed
step environment values, including a signed receipt, even when the verifier itself
does not print them. Migrate any existing variable-backed configuration to protected
environment secrets and remove the variable copies. Missing secrets fail closed.
No private signing key or raw evidence belongs in the workflow secrets or runner.
The verifier reads the four values from its process environment and never prints
them. Review earlier public logs for exposed receipts; revoke a still-valid receipt
before issuing a new candidate approval.

While signed qualification is on, you can revoke future attempts by removing the
receipt or rotating the configured authority. Cancel any publication job that is
already running separately. An expired approval needs a fresh owner review and a
new issuance. When the variable is on, repair has the same requirement, even if
the selected historical release used the older evidence format.

### Signed contract

The JSON envelope has exactly `schema`, `payload` and `signature`. Duplicate keys,
unknown fields, numeric JSON values and noncanonical base64 are rejected. The
schema is `engraphis-release-qualification/v1`; signature is standard base64 of
the 64-byte Ed25519 signature. The payload has exactly these fields:

| Payload field | Contract |
| --- | --- |
| `engine_commit` | Lowercase 40-character Git commit, equal to the pushed tag commit or the peeled repair tag commit. |
| `distributions` | Exact filename-to-SHA-256 map for one `engraphis` wheel and one source archive, matching the release tag's version and the files about to be published. |
| `candidate_id` | Lowercase 64-character full-product candidate ID, equal to the independently configured expected ID. |
| `ledger_sha256` | Lowercase SHA-256 of the private ledger bytes, equal to the independently configured expected digest. |
| `release_gates` | Exactly the mandatory release gate inventory below, every value the string `PASS`. |
| `issued_at` | UTC ISO timestamp ending in `Z`, already reached by the verification clock. |
| `expires_at` | UTC ISO timestamp ending in `Z`, later than issuance and strictly later than verification time. |
| `release_approved` | JSON boolean `true`, reflecting an explicit owner decision. |

The required gates are `automated`, `memory_integrity`, `installed_journeys`,
`capacity`, `responsiveness`, `resource_stability`, `recovery`, `hosted_journeys`,
`independent_quality`, `usability`, and `pilot`. The authoritative inventory is
`RELEASE_GATES` in `scripts/check_release_readiness.py`. Leadership gates
`competitive_coding` and `external_benchmarks` remain separate and are not included
in this release-only approval. Missing mandatory evidence cannot be represented
as a passing qualification.

Sign the exact byte sequence returned by
`scripts.verify_release_qualification.signing_bytes(payload)`:

```text
ASCII("engraphis-release-qualification/v1\n")
+ ASCII(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   ensure_ascii=True, allow_nan=False))
```

There is no trailing newline after the JSON payload. Envelope whitespace does not
affect the signed payload. Private tooling should consume this encoding contract
and keep signing outside the public tree. No sample key is a trusted authority.

With the four variables configured, the read-only verification command is:

```console
python -m scripts.verify_release_qualification --dist dist --commit FULL_COMMIT --tag vVERSION
```

When the variable is `true`, the normal workflow checks before both the PyPI write
and the GitHub write. Repair first selects a matching historical push run and
verifies the exact hashes of its public distributions and evidence. It then checks
the current owner approval before both repair writes, using the peeled release tag
commit and never the commit of the repair workflow's `main` checkout. Repair keeps
those artifact and evidence checks whether or not signed qualification is on.

## Public installed evidence

New release evidence requires all six installed surface cells: MCP and dashboard
on Windows, macOS and Linux, each in a fresh Python 3.11 environment. The separate
base-package cells remain mandatory workflow jobs. The surface reports describe
deterministic offline memory journeys, not downloaded semantic-model qualification
or the wider install/upgrade acceptance gate.

`release_evidence.py --installed-journeys installed-journey-inputs` validates each
surface's completed milestones, installed-package flag, platform, package version,
package-source digest and actual wheel SHA-256. It publishes three allowlisted
files per cell: journey JSON, wheel identity JSON and the complete pinned dependency
inventory. The dependency inventory replaces the runner's local wheel URI with
`engraphis==VERSION`; all other entries must be valid public package/version pins.
Raw logs, extra report fields and incomplete matrices are rejected.

The format-3 public manifest records hashes of both the original captures and the
normalized published files. The 18 flat files are included in GitHub release assets.
Repair verifies all indexed file hashes when this matrix is present. Historical
format-3 manifests without the optional matrix remain readable. When signed
qualification is on, they still need a fresh signed full-product approval before
any publication or repair write.
