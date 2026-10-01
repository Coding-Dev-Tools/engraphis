# Dashboard vendor updates

The dashboard serves committed browser bundles. The force-graph npm dependency
records their reviewed upstream version; changing that dependency alone does not
update the served code and breaks the version/provenance checks.

The reviewed force-graph version is 1.51.4. All three shipped copies contain two
local CSP changes that disable runtime stylesheet insertion. Equivalent static
rules live in the dashboard stylesheets. The bundle also has an exact upstream
commit, npm integrity, source lock and dependency license inventory.

The [upstream 1.51.5 comparison](https://github.com/vasturiano/force-graph/compare/v1.51.4...v1.51.5)
changes examples, development tooling, package version and yarn lock, with no
graph runtime source change. Retaining the reviewed bundle avoids presenting a
metadata-only dependency bump as a shipped runtime upgrade.

Dependabot ignores automatic force-graph version updates. The three
`version-update:semver-*` rules do not disable security updates. A security update
still needs the complete vendor review below before merge.

For a deliberate force-graph upgrade:

1. Verify the exact upstream release artifact, npm integrity, source commit and
   source lock. Review the runtime diff and the locked dependency closure.
2. Preserve both CSP patches and their equivalent static CSS. Update the bundles
   and license copies in `engraphis/dashboard_assets/vendor/`,
   `engraphis/classic_assets/vendor/` and `engraphis/static/vendor/` together.
3. Regenerate the vendor manifest's version, source and hashes, plus the complete
   locked dependency license inventory in `deploy/`. Record actual artifact and
   source-lock hashes; do not relabel the previous bundle or inventory.
4. Update `package.json`, `package-lock.json`, `NOTICE`, and the explicit license
   inventory/source-lock packaging references in `MANIFEST.in`, `pyproject.toml`
   and `scripts/verify_distribution_contents.py`. Update the corresponding exact
   provenance assertions in `tests/test_packaging.py` to the reviewed new inputs.
5. Run the vendor integrity, packaging and asset externalization tests. Run the
   browser graph/CSP suite against the real vendored bundle, then build a wheel
   and source archive and verify them with `scripts/verify_distribution_contents.py`.

Keep version, hash, license and CSP checks enabled throughout the upgrade. No
current dependency pin or upstream release alone proves those validations.
