# Releasing Engraphis to PyPI

Goal: make `pip install engraphis` work for a stranger. The name is **free** on PyPI as of
2026-07-03. Do this once and Week 2 of the ship plan is done.

There are two paths. **First release → do Path A (manual, 10 min).** Every release after →
Path B is automatic (`release.yml` is already committed; you just publish a GitHub Release).

---

## 0. Pre-flight fixes (5 min, do these first)

- [ ] **Fix the project URLs.** `pyproject.toml [project.urls]` points at
      `github.com/engraphis/engraphis`, but the repo is at `github.com/Coding-Dev-Tools/engraphis`.
      Either update the three URLs to the real remote, **or** create an `engraphis` GitHub org and
      move the repo there (nicer branding, and the URLs already assume it). Broken links on the
      PyPI page look abandoned — fix before publishing.
- [ ] **Confirm version.** `version = "0.1.0"` is fine for the first upload. Bump it for every
      later release (PyPI refuses to overwrite an existing version).
- [ ] **Confirm `LICENSE` and `README.md` exist at repo root** (they do) — PyPI renders the README
      as the project page.
- [ ] Merge your working branch to `main` and push, so you're releasing the real code.

---

## Path A — First release (manual, with an API token)

The first upload has to create the project, so do it by hand once.

```bash
cd C:\GitHub\engraphis
python -m pip install --upgrade build twine
python -m build                      # writes dist/engraphis-0.1.0.tar.gz + .whl
twine check dist/*                   # metadata sanity check — must say PASSED
```

Get a token: log in at https://pypi.org → Account settings → **API tokens** → *Add token*
(scope: "Entire account" for the first upload; you can scope it to the project afterward).

```bash
twine upload dist/*
# username:  __token__
# password:  pypi-AgEI...   (paste the token)
```

Verify in a clean environment:

```bash
python -m venv /tmp/e && /tmp/e/Scripts/activate   # (Windows: \tmp\e\Scripts\activate)
pip install engraphis
engraphis-mcp --help                 # the headline command exists → success
```

Then delete the token if it was account-scoped, and switch to Path B for everything after.

---

## Path B — Every release after (automatic, no tokens)

`release.yml` publishes via **PyPI Trusted Publishing (OIDC)** — no secrets stored. One-time setup:

1. On PyPI → your project → **Manage → Publishing → Add a trusted publisher → GitHub**. Enter:
   - Owner: `Coding-Dev-Tools` (or your `engraphis` org)
   - Repository: `engraphis`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`   ← must match the `environment:` in the workflow
2. In the GitHub repo → **Settings → Environments → New environment → `pypi`**. (Optionally add a
   required-reviewer rule so a release waits for your one-click approval — a nice safety gate.)

To cut a release from then on:

```bash
# bump version in pyproject.toml first, commit, then:
git tag v0.1.1 && git push origin v0.1.1
```

Then on GitHub → **Releases → Draft a new release** → pick the tag → **Publish**. The workflow
builds, runs `twine check`, and publishes to PyPI automatically.

---

## After it's live
- [ ] Update the README install line from `pip install -e .` to `pip install engraphis`
      (and `pip install "engraphis[mcp]"` for the MCP server).
- [ ] Tweet/post the one command. "`pip install engraphis`" *is* the launch — see `LAUNCH-POST.md`.
