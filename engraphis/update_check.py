"""Update reminder — tell operators when a newer Engraphis release is available.

Design goals (why this module looks the way it does):

* **Fail-silent.** A version check is a convenience, never a dependency. Any network
  error, malformed payload, or unwritable cache degrades to "no update known" and never
  raises into a request handler, the server banner, or an MCP call.
* **Explicit opt-in.** Update checks are disabled unless ``ENGRAPHIS_UPDATE_CHECK`` is
  one of the recognized affirmative values (``1``, ``true``, ``yes``, ``on``, ``enable``,
  or ``enabled``). With the default setting, the dashboard, startup log, and MCP notice
  simply report ``enabled=False`` and make no network request.
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
import ipaddress
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit

# Stdlib-only itself (see the module docstring): importing it keeps this module free of
# the config/server stack while giving the probe the package's vetted HTTPS connector.
from engraphis.hosted_client import build_pinned_https_opener
from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    ensure_owner_private_dir,
    read_private_text,
)

try:  # installed distribution → real version; source tree → pinned fallback
    from engraphis import __version__ as CURRENT_VERSION
except Exception:  # pragma: no cover - engraphis always importable in practice
    CURRENT_VERSION = "0"

# ── tunables ──────────────────────────────────────────────────────────────────
DEFAULT_REPO = "Coding-Dev-Tools/engraphis"
CACHE_TTL_SECONDS = 24 * 3600
DEFAULT_TIMEOUT = 3.5          # keep short: never stall an interactive request
_MAX_BYTES = 512 * 1024        # cap the response body we are willing to read
_MAX_CACHE_BYTES = 64 * 1024
_MAX_CACHE_TTL_SECONDS = 366 * 24 * 3600
_MAX_VERSION_TEXT = 256
_MAX_VERSION_PARTS = 16
_MAX_VERSION_DIGITS = 9
_MAX_RELEASE_URL = 2048
_TRUTHY = {"1", "true", "yes", "on", "enable", "enabled"}

_CACHE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()
_refreshing = False


# ── configuration (read straight from the environment) ────────────────────────
def enabled() -> bool:
    """Return true only when the operator explicitly enables update checks.

    A local installation must not contact a release endpoint merely because it was
    launched. ``ENGRAPHIS_UPDATE_CHECK`` opts into the cached, fail-silent reminder
    only when it is one of the recognized affirmative values; every unset, falsy,
    misspelled, or arbitrary value keeps the process fully local.
    """
    return os.environ.get("ENGRAPHIS_UPDATE_CHECK", "0").strip().lower() in _TRUTHY


def _cache_ttl_seconds() -> int:
    """Return the documented bounded cache duration, never a pathname."""
    raw = os.environ.get("ENGRAPHIS_UPDATE_CACHE", "").strip()
    if not raw:
        return CACHE_TTL_SECONDS
    try:
        value = int(raw, 10)
    except (TypeError, ValueError, OverflowError):
        return CACHE_TTL_SECONDS
    return value if 1 <= value <= _MAX_CACHE_TTL_SECONDS else CACHE_TTL_SECONDS


def _endpoint() -> str:
    override = os.environ.get("ENGRAPHIS_UPDATE_URL", "").strip()
    if override:
        return override
    repo = os.environ.get("ENGRAPHIS_UPDATE_REPO", DEFAULT_REPO).strip() or DEFAULT_REPO
    return "https://api.github.com/repos/%s/releases/latest" % repo


def _cache_path() -> Optional[str]:
    """Return the fixed owner-private update cache leaf."""
    configured = os.environ.get("ENGRAPHIS_STATE_DIR", "").strip()
    try:
        base = Path(configured).expanduser() if configured else Path.home() / ".engraphis"
    except (OSError, RuntimeError):
        return None
    return str(base / "update_check.json")


# ── version comparison (pure, offline-testable) ───────────────────────────────
def parse_version(text: object) -> Optional[tuple]:
    """Return the leading numeric release tuple of a version string, or ``None``.

    Tolerates a ``v`` prefix and ignores any pre-release/build suffix so ``"v1.2.3-rc1"``
    and ``"1.2.3"`` both parse to ``(1, 2, 3)``. Non-versions parse to ``None``.
    """
    if not isinstance(text, str):
        return None
    if len(text) > _MAX_VERSION_TEXT:
        return None
    m = re.match(r"\s*[vV]?(\d+(?:\.\d+)*)", text)
    if not m:
        return None
    parts = m.group(1).split(".")
    if len(parts) > _MAX_VERSION_PARTS or any(
        len(part) > _MAX_VERSION_DIGITS for part in parts
    ):
        return None
    return tuple(int(part) for part in parts)


_RELEASE_VERSION = re.compile(
    r"[vV]?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)


def _safe_release_version(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_VERSION_TEXT
        or _RELEASE_VERSION.fullmatch(normalized) is None
        or parse_version(normalized) is None
    ):
        return None
    return normalized


def _safe_release_url(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > _MAX_RELEASE_URL
        or "\\" in normalized
        or any(
            ord(character) < 0x21 or ord(character) > 0x7E
            for character in normalized
        )
    ):
        return ""
    try:
        parts = urlsplit(normalized)
        _ = parts.port
    except ValueError:
        return ""
    if (
        parts.scheme.lower() not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
    ):
        return ""
    return normalized


def _safe_display_text(value: object, *, max_chars: int = 256) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if len(normalized) > max_chars or any(
        ord(character) < 0x20 or ord(character) == 0x7F
        for character in normalized
    ):
        return ""
    return normalized


def _normalized_release(version: object, url: object) -> Optional[dict]:
    normalized = _safe_release_version(version)
    if normalized is None:
        return None
    return {"version": normalized, "url": _safe_release_url(url)}


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


def _parse_release_payload(data: object) -> Optional[dict]:
    """Normalize a GitHub-release / PyPI / generic JSON payload safely."""
    if not isinstance(data, dict):
        return None
    if "tag_name" in data:
        if data.get("draft") or data.get("prerelease"):
            return None
        return _normalized_release(
            data.get("tag_name") or data.get("name") or "",
            data.get("html_url") or "",
        )
    info = data.get("info")
    if isinstance(info, dict) and info.get("version"):
        version = _safe_release_version(info["version"])
        if version is None:
            return None
        url = (
            info.get("project_url")
            or info.get("home_page")
            or ("https://pypi.org/project/engraphis/%s/" % version)
        )
        return _normalized_release(version, url)
    if data.get("version"):
        return _normalized_release(
            data["version"],
            data.get("url") or data.get("html_url") or "",
        )
    return None


def _fetch(url: str, timeout: float) -> Optional[dict]:
    """Fetch and normalize the latest-release payload. Returns ``None`` on any failure.

    Only ``https`` (or loopback ``http``) endpoints are contacted; redirects are blocked.
    """
    if not isinstance(url, str) or "\\" in url or any(
        ord(character) <= 0x20 or ord(character) == 0x7F for character in url
    ):
        return None
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        host = parsed.hostname or ""
    except (TypeError, ValueError):
        return None
    if not host or parsed.username is not None or parsed.password is not None:
        return None
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
        literal_loopback = address.is_loopback
    except ValueError:
        literal_loopback = False
    scheme = parsed.scheme.casefold()
    if scheme != "https" and not (scheme == "http" and literal_loopback):
        return None
    # ``ENGRAPHIS_UPDATE_URL`` makes this endpoint operator-controllable, so the probe
    # gets the same pinned HTTPS opener every other outbound client uses (hosted_client,
    # cloud_session, sync_relay): the vetted address is the one actually dialled, which
    # rejects private/reserved targets and closes the DNS-rebinding window between the
    # scheme check above and the connect. HTTP is allowed only for literal loopback
    # addresses because urllib's ordinary HTTP handler cannot pin a hostname lookup.
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Engraphis/%s update-check" % CURRENT_VERSION,
            "Accept": "application/vnd.github+json, application/json;q=0.9, */*;q=0.1",
        })
        opener = build_pinned_https_opener(_NoRedirect())
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
        with _CACHE_LOCK:
            raw = read_private_text(
                Path(path), max_bytes=_MAX_CACHE_BYTES, allow_missing=True,
            )
        data = json.loads(raw) if raw else {}
        return data if isinstance(data, dict) else {}
    except (UnsafeStateFile, OSError, ValueError, RecursionError):
        return {}


def _write_cache(latest: str, url: str, error: str = "") -> None:
    path = _cache_path()
    if not path:
        return
    payload = {
        "latest": _safe_release_version(latest) or "",
        "url": _safe_release_url(url),
        "error": _safe_display_text(error),
        "checked_at": time.time(),
    }
    try:
        with _CACHE_LOCK:
            cache_path = Path(path)
            ensure_owner_private_dir(cache_path.parent)
            atomic_private_text(
                cache_path,
                json.dumps(
                    payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                )
                + "\n",
            )
    except (UnsafeStateFile, OSError, ValueError):
        pass  # unwritable cache is fine; we just re-probe next time


def _checked_at(cache: dict) -> float:
    try:
        value = float(cache.get("checked_at") or 0.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


def _snapshot_from_cache(cache: dict) -> dict:
    """Build a sanitized snapshot against the live installed version."""
    latest = _safe_release_version(cache.get("latest")) or ""
    return {
        "enabled": True,
        "current": _safe_release_version(CURRENT_VERSION) or "",
        "latest": latest,
        "update_available": bool(latest) and is_newer(latest, CURRENT_VERSION),
        "url": _safe_release_url(cache.get("url")),
        "checked_at": _checked_at(cache),
        "error": _safe_display_text(cache.get("error")),
    }


def _disabled_snapshot() -> dict:
    return {
        "enabled": False,
        "current": _safe_release_version(CURRENT_VERSION) or "",
        "latest": "",
        "update_available": False,
        "url": "",
        "checked_at": 0.0,
        "error": "",
    }


# ── public API ────────────────────────────────────────────────────────────────
def check(force: bool = False, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Return the current update snapshot, hitting the network only when the cache is
    stale (or *force*). Always safe to call; never raises."""
    if not enabled():
        return _disabled_snapshot()
    cache = _read_cache()
    fresh = (time.time() - _checked_at(cache)) < _cache_ttl_seconds()
    if cache and fresh and not force:
        return _snapshot_from_cache(cache)
    try:
        result = _fetch(_endpoint(), timeout)
    except Exception:  # noqa: BLE001 — update checks are explicitly fail-silent
        result = None
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
    fresh = cache and (time.time() - _checked_at(cache)) < _cache_ttl_seconds()
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
    """One control-free human notice, or ``None`` when no safe update is known."""
    snap = snap if snap is not None else snapshot()
    if not snap.get("enabled") or not snap.get("update_available"):
        return None
    latest = _safe_release_version(snap.get("latest"))
    current = _safe_release_version(snap.get("current"))
    if latest is None or current is None:
        return None
    url = _safe_release_url(snap.get("url"))
    tail = " — %s" % url if url else ""
    return (
        "Engraphis %s is available (you have %s). Upgrade: pip install -U engraphis%s"
        % (latest, current, tail)
    )


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


def emit_cli_notice(emit: Optional[Callable[[str], None]] = None,
                    timeout: float = DEFAULT_TIMEOUT) -> None:
    """Print an update notice for a short-lived terminal command.

    This only reads the cached snapshot on the calling thread. A stale or missing cache
    schedules its refresh in the background, so an offline local command never waits on
    the update endpoint. A short-lived command may therefore show a newly discovered
    update on its next invocation rather than delaying its primary operation.
    """
    if not enabled():
        return
    printer = emit if emit is not None else (
        lambda line: print("[engraphis] %s" % line, file=sys.stderr))
    try:
        line = notice_line(snapshot())
    except Exception:  # noqa: BLE001 - a convenience feature must never break the CLI
        return
    if line:
        try:
            printer(line)
        except Exception:  # noqa: BLE001
            pass
