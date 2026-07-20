"""Post-deploy smoke test for the cloud license + relay endpoints on a live host.

Confirms the license endpoints are actually mounted and behaving on your deployment
(Railway/Fly/etc.) — a 404 here means the routers didn't mount, which is a launch blocker.

Usage:
    python -m scripts.smoke_cloud https://your-host                # public checks only
    python -m scripts.smoke_cloud https://your-host --key ENGR1... # + register round-trip
    python -m scripts.smoke_cloud https://your-host --key ... --admin-token $TOK --revoke
        (also exercises revoke; only do this against a throwaway key)

Exit code 0 = all checks passed, 1 = a check failed. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from engraphis.cloud_license import validate_cloud_base_url


def _req(method, url, *, data=None, headers=None, timeout=15):
    body = json.dumps(data).encode() if data is not None else None
    h = {"Content-Type": "application/json"} if data is not None else {}
    h.update(headers or {})
    req = urllib.request.Request(url, data=body, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # nosec B310
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}
    except Exception as e:
        return None, {"_error": str(e)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--key", default="")
    ap.add_argument("--admin-token", default="")
    ap.add_argument("--revoke", action="store_true")
    args = ap.parse_args(argv)
    try:
        base = validate_cloud_base_url(args.base_url)
    except ValueError as exc:
        ap.error(str(exc))
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        ok = ok and cond
        print(("PASS " if cond else "FAIL ") + name + (" — " + detail if detail else ""))

    # 1) endpoints mounted: public verify returns JSON {"known": false} for an unknown id
    st, body = _req("GET", base + "/license/v1/verify/smoke-unknown-key")
    check("verify endpoint mounted", st == 200 and body.get("known") is False,
          "status=%s body=%s" % (st, body))

    # 2) register requires a real key (no key → 400/402, not 404/500)
    st, body = _req("POST", base + "/license/v1/register", data={"machine_id": "smoke"})
    check("register endpoint mounted + rejects missing key", st in (400, 402),
          "status=%s" % st)

    # 3) optional: full register round-trip with a real key
    if args.key:
        st, body = _req("POST", base + "/license/v1/register",
                        data={"key": args.key, "machine_id": "smoke-device"})
        check("register issues a lease for a valid key",
              st == 200 and bool(body.get("lease")), "status=%s" % st)
        # (key_id isn't returned by register; deriving it via verify-by-email is out of
        # scope for this smoke test — see the --revoke path below for the auth-only check.)

    # 4) optional: revoke round-trip (needs admin token + a key_id via email lookup)
    if args.revoke and args.admin_token and args.key:
        # find the key_id via the admin keys lookup by decoding the key's email is non-trivial
        # here; instead exercise revoke auth: without token must be 401
        st_noauth, _ = _req("POST", base + "/license/v1/revoke/deadbeef0000")
        check("revoke rejects missing admin token", st_noauth == 401, "status=%s" % st_noauth)
        st_auth, _ = _req("POST", base + "/license/v1/revoke/deadbeef0000",
                          headers={"Authorization": "Bearer " + args.admin_token})
        check("revoke accepts admin token", st_auth == 200, "status=%s" % st_auth)

    print("\n" + ("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
