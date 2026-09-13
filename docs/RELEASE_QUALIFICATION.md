# Owner-signed release qualification

Every new PyPI publication, GitHub release write, and repair requires a valid
full-product qualification. Passing the public build jobs is necessary but does
not replace the mandatory private readiness evidence. The workflow fails closed
when qualification configuration is missing, malformed, expired or inconsistent
with the selected source and distribution bytes.

The public verifier is `scripts/verify_release_qualification.py`. It verifies
Ed25519 signatures using `cryptography==50.0.0` in release jobs. It contains no
signing operation, key generator, private ledger reader or provider integration.
Its input is a content-free approval; private evidence stays with its accountable
owners. A valid signature establishes the configured authority's assertion, not
independent proof that each observation happened.

## Pending owner setup

These operations have **not been performed** by this source change. Publication
will remain blocked until the release owner completes them.

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
   validity window and explicit release approval. Set these protected environment
   variables together for the selected candidate:

| Variable | Required value |
| --- | --- |
| `ENGRAPHIS_RELEASE_VERIFY_KEY` | Canonical standard base64 of the authority's raw 32-byte Ed25519 public key. |
| `ENGRAPHIS_RELEASE_QUALIFICATION` | The signed JSON envelope below; at most 16 KiB. |
| `ENGRAPHIS_RELEASE_CANDIDATE_ID` | The expected full-product candidate ID from the validated private ledger. |
| `ENGRAPHIS_RELEASE_LEDGER_SHA256` | SHA-256 of the exact private ledger file bytes the owner reviewed. |

GitHub repository variables can supply the same values where the organization's
governance protects them equivalently. The jobs always enter the protected
`release-qualification` environment. No private key or raw evidence belongs in
any of these variables. The verifier reads them from its environment and never
prints the envelope or public key.

To revoke future attempts, remove the receipt or rotate the configured authority;
cancel any already-running publication job separately. An expired approval needs
fresh owner review and issuance. Repair is subject to the same requirement even
when the selected historical release used the older evidence format.

## Signed contract

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

The normal workflow checks before both PyPI and GitHub writes. Repair first selects
a matching historical push run and verifies its exact public distribution/evidence
hashes, then checks the current owner approval before both repair writes. It uses
the peeled release tag commit, never the repair workflow's `main` checkout commit.
No workflow switch makes the qualification optional.

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
format-3 manifests without the optional matrix remain readable, but they still need
a fresh signed full-product approval before any publication or repair write.
