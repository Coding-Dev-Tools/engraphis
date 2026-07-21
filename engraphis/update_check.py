"""Update reminder — tell operators when a newer Engraphis release is available.

Design goals (why this module looks the way it does):

* **Fail-silent.** A version check is a convenience, never a dependency. Any network
  error, malformed payload, or unwritable cache degrades to "no update known" and never
  raises into a request handler, the server banner, or an MCP call.
* **Opt-out.** ``ENGRAPHIS_UPDATE_CHECK=0`` disables all network activity. The dashboard,
  startup log, and MCP notice then simply report ``enabled=False``.
* **Cheap + shared.** One disk cache (default 24h TTL) backs all three surfaces
  (dashboard banner, startup log, MCP notice) so opening the dashboard does not re-hit
  the network, and the server boot path never blocks on it.
* **Stdlib-only, config-free import.** Like :mod:`engraphis.netutil`, this stays importable
  without dragging in the heavy config/server stack, and reads its knobs straight from the
  environment so it is trivially unit-testable offline.

The default source is the GitHub *releases/latest* endpoint for the project repo, which
excludes drafts/pre-releases server-side. ``ENGRAPHIS_UPDATE_URL`` overrides it with any
endpoint returning a GitHub-release, PyPI, or ``{"version": ..., "url": ...}`` payload.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

try:  # installed distribution → real version; source tree → pinned fallback
    from engraphis import __version__ as CURRENT_VERSION
except Exception:  # pragma: no cover - engraphis always importable in practice
    CURRENT_VERSION = "0"

# ── tunables ──────────────────────────────────────────────────────────────────
DEFAULT_REPO = "Coding-Dev-Tools/engraphis"
CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_TIMEOUT = 3.5          # keep short: never stall an interactive request
_MAX_BYTES = 512 * 1024        # cap the response body we are willing to read
_FALSY = {"0", "false", "no", "off", "disable", "disabled"}

_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_refreshing = False


# ── configuration (read straight from the environment) ────────────────────────
def enabled() -> bool:
    """Update checks are on by default; any falsy ``ENGRAPHIS_UPDATE_CHECK`` disables them."""
    return os.environ.get("ENGRAPHIS_UPDATE_CHECK", "1").strip().lower() not in _FALSY


def _endpoint() -> str:
    override = os.environ.get("ENGRAPHIS_UPDATE_URL", "").strip()
    if override:
        return override
    repo = os.environ.get("ENGRAPHIS_UPDATE_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    return "https://api.github.com/repos/%s/releases/latest" % repo


def _cache_path() -> Optional[str]:
    """A per-user cache file. Prefer sitting next to the DB (already a writable user-data
    dir); fall back to the OS temp dir. Returns ``None`` only if nothing is writable."""
    override = os.environ.get("ENGRAPHIS_UPDATE_CACHE", "").strip()
    if override:
        return override
    candidates = []
    try:  # optional: keep the cache with the rest of the user's engraphis state
        from engraphis.config import settings

        db_dir = os.path.dirname(os.path.abspath(settings.db_path))
        if db_dir:
            candidates.append(os.path.join(db_dir, ".engraphis_update_check.json"))
    except Exception:  # noqa: BLE001 - config unavailable/misconfigured → temp dir
        pass
    candidates.append(os.path.join(tempfile.gettempdir(), "engraphis_update_check.json"))
    return candidates[0] if candidates else None


# ── version comparison (pure, offline-testable) ───────────────────────────────
def parse_version(text: object) -> Optional[tuple]:
    """Return the leading numeric release tuple of a version string, or ``None``.

    Tolerates a ``v`` prefix and ignores any pre-release/build suffix so ``"v1.2.3-rc1"``
    and ``"1.2.3"`` both parse to ``(1, 2, 3)``. Non-versions parse to ``None``.
    """
    if not isinstance(text, str):
        return None
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", text)
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def is_newer(latest: object, current: object) -> bool:
    """True iff *latest* is a strictly greater release than *current* (zero-padded compare)."""
    lv, cv = parse_version(latest), parse_version(current)
    if lv is None or cv is None:
        return False
    width = max(len(lv), len(cv))
    lv += (0,) * (width - len(lv))
    cv += (0,) * (width - len(cv))
    return lv > cv


# ── network ───────────────────────────────────────────────────────────────────
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Block redirects entirely — a configured update URL must resolve where it points,
    so a crafted 30x cannot bounce the probe at an internal/unexpected host (SSRF guard)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _parse_release_payload(data: dict) -> Optional[dict]:
    """Normalize a GitHub-release / PyPI / generic JSON payload to ``{version, url}``.

    Returns ``None`` for drafts, pre-releases, or payloads without a usable version.
    """
    if not isinstance(data, dict):
        return None
    # GitHub releases/latest
    if "tag_name" in data:
        if data.get("draft") or data.get("prerelease"):
            return None
        version = data.get("tag_name") or data.get("name") or ""
        url = data.get("html_url") or ""
        return {"version": str(version), "url": str(url)}
    # PyPI /pypi/<pkg>/json
    info = data.get("info")
    if isinstance(info, dict) and info.get("version"):
        version = str(info["version"])
        url = info.get("project_url") or info.get("home_page") \
            or ("https://pypi.org/project/engraphis/%s/" % version)
        return {"version": version, "url": str(url)}
    # generic {"version": ..., "url": ...}
    if data.get("version"):
        return {"version": str(data["version"]),
                "url": str(data.get("url") or data.get("html_url") or "")}
    return None


def _fetch(url: str, timeout: float) -> Optional[dict]:
    """Fetch and normalize the latest-release payload. Returns ``None`` on any failure.

    Only ``https`` (or loopback ``http``) endpoints are contacted; redirects are blocked.
    """
    scheme, _, rest = url.partition("://")
    scheme = scheme.lower()
    host = rest.split("/", 1)[0].split("@")[-1].split(":", 1)[0].lower()
    loopback = host in ("localhost", "127.0.0.1", "::1", "[::1]")
    if scheme != "https" and not (scheme == "http" and loopback):
        return None
    req = urllib.request.Request(url, headers={
        "User-Agent": "Engraphis/%s update-check" % CURRENT_VERSION,
        "Accept": "application/vnd.github+json, application/json;q=0.9, */*;q=0.1",
    })
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:  # nosec B310 - scheme checked above
            raw = resp.read(_MAX_BYTES + 1)
        if len(raw) > _MAX_BYTES:
            return None
        return _parse_release_payload(json.loads(raw.decode("utf-8")))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError,
            TimeoutError, OSError):
        return None


# ── cache ─────────────────────────────────────────────────────────────────────
def _read_cache() -> dict:
    path = _cache_path()
    if not path:
        return {}
    try:
        with _CACHE_LOCK, open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(latest: str, url: str, error: str = "") -> None:
    path = _cache_path()
    if not path:
        return
    payload = {"latest": latest, "url": url, "error": error, "checked_at": time.time()}
    try:
        with _CACHE_LOCK:
            tmp = "%s.%d.tmp" % (path, os.getpid())
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, path)
    except OSError:
        pass  # unwritable cache is fine; we just re-probe next time


def _snapshot_from_cache(cache: dict) -> dict:
    """Build a public snapshot, recomputing ``update_available`` against the *live*
    installed version so an upgrade clears the banner immediately (no TTL wait)."""
    latest = str(cache.get("latest") or "")
    return {
        "enabled": True,
        "current": CURRENT_VERSION,
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest, CURRENT_VERSION),
        "url": str(cache.get("url") or ""),
        "checked_at": float(cache.get("checked_at") or 0.0),
        "error": str(cache.get("error") or ""),
    }


def _disabled_snapshot() -> dict:
    return {"enabled": False, "current": CURRENT_VERSION, "latest": "",
            "update_available": False, "url": "", "checked_at": 0.0, "error": ""}


# ── public API ────────────────────────────────────────────────────────────────
def check(force: bool = False, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return the current update snapshot, hitting the network only when the cache is
    stale (or *force*). Always safe to call; never raises."""
    if not enabled():
        return _disabled_snapshot()
    cache = _read_cache()
    fresh = (time.time() - float(cache.get("checked_at") or 0.0)) < CACHE_TTL_SECONDS
    if cache and fresh and not force:
        return _snapshot_from_cache(cache)
    result = _fetch(_endpoint(), timeout)
    if result is None:
        # Preserve the last good answer; only stamp the failure if we had nothing.
        _write_cache(str(cache.get("latest") or ""), str(cache.get("url") or ""),
                     error="update check unavailable")
        return _snapshot_from_cache(_read_cache())
    _write_cache(result["version"], result.get("url", ""), error="")
    return _snapshot_from_cache(_read_cache())


def snapshot() -> dict:
    """Non-blocking best-known snapshot for hot paths (bootstrap, startup, MCP).

    Returns whatever the cache holds immediately and, if it is stale/missing, kicks a
    single background refresh so the *next* read is current. Never performs network I/O
    on the calling thread.
    """
    if not enabled():
        return _disabled_snapshot()
    cache = _read_cache()
    fresh = cache and (time.time() - float(cache.get("checked_at") or 0.0)) < CACHE_TTL_SECONDS
    if not fresh:
        refresh_in_background()
    return _snapshot_from_cache(cache)


def refresh_in_background(timeout: float = DEFAULT_TIMEOUT) -> None:
    """Warm the cache on a daemon thread, at most one refresh in flight. Fail-silent."""
    global _refreshing
    if not enabled():
        return
    with _REFRESH_LOCK:
        if _refreshing:
            return
        _refreshing = True

    def _run() -> None:
        global _refreshing
        try:
            check(force=True, timeout=timeout)
        except Exception:  # noqa: BLE001 - background best-effort, never surface
            pass
        finally:
            with _REFRESH_LOCK:
                _refreshing = False

    threading.Thread(target=_run, name="engraphis-update-check", daemon=True).start()


def notice_line(snap: Optional[dict] = None) -> Optional[str]:
    """One-line human notice, or ``None`` when no update is available / checks are off."""
    snap = snap if snap is not None else snapshot()
    if not snap.get("enabled") or not snap.get("update_available"):
        return None
    latest, current = snap.get("latest") or "?", snap.get("current") or "?"
    url = snap.get("url") or ""
    tail = " — %s" % url if url else ""
    return ("Engraphis %s is available (you have %s). Upgrade: pip install -U engraphis%s"
            % (latest, current, tail))


def emit_startup_notice(emit: Optional[Callable[[str], None]] = None,
                        timeout: float = DEFAULT_TIMEOUT) -> None:
    """Fire-and-forget: emit a one-line "update available" notice shortly after startup.

    Runs the (cache-respecting) check on a daemon thread so server boot is never blocked or
    delayed by the network. *emit* defaults to a stderr print; pass a logger method (e.g.
    ``logger.info``) to route it into structured logs. Fail-silent and a no-op when checks
    are disabled or no update is available.
    """
    if not enabled():
        return
    printer = emit if emit is not None else (
        lambda line: print("[engraphis] %s" % line, file=sys.stderr))

    def _run() -> None:
        try:
            line = notice_line(check(timeout=timeout))
        except Exception:  # noqa: BLE001 - never surface from a background notice
            return
        if line:
            try:
                printer(line)
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=_run, name="engraphis-update-notice", daemon=True).start()
