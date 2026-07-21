"""Central configuration — all values sourced from env with safe defaults."""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from contextlib import contextmanager
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

try:
    from dotenv import load_dotenv
    # ``engraphis-init`` writes the configuration contract to ``./.env``. Calling
    # ``load_dotenv()`` without a path makes python-dotenv search from this module's
    # installed location, so a wheel install silently ignored the file it had just told
    # the user to create. Load only the process working directory (no parent traversal),
    # and retain python-dotenv's default ``override=False`` so an explicit environment
    # always wins.
    load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"))
except Exception:
    pass

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DB_NOTICES = set()


def _default_db_path(root: Path = _PROJECT_ROOT, *, os_name: Optional[str] = None,
                     platform: Optional[str] = None, environ: Optional[dict] = None,
                     home: Optional[Path] = None) -> str:
    """Default DB location. In a source checkout: ``<repo>/engraphis.db`` (dev behavior,
    unchanged). Installed into site-/dist-packages: a per-user data directory instead —
    a DB inside site-packages is invisible to the user, contradicts the printed
    "./engraphis.db", and is silently DELETED by ``pip install -U`` / uninstall.
    Pure function of *root* so both branches are unit-testable."""
    parts = {p.lower() for p in root.parts}
    if "site-packages" not in parts and "dist-packages" not in parts:
        return str(root / "engraphis.db")
    os_name = os.name if os_name is None else os_name
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else home
    if os_name == "nt":
        win_home = PureWindowsPath(str(home))
        base = PureWindowsPath(
            environ.get("LOCALAPPDATA") or (win_home / "AppData" / "Local")
        )
    elif platform == "darwin":
        posix_home = PurePosixPath(str(home).replace("\\", "/"))
        base = posix_home / "Library" / "Application Support"
    else:
        posix_home = PurePosixPath(str(home).replace("\\", "/"))
        base = PurePosixPath(
            environ.get("XDG_DATA_HOME") or (posix_home / ".local" / "share")
        )
    return str(base / "engraphis" / "engraphis.db")


def _db_notice(key: str, message: str) -> None:
    """Emit a migration/collision notice once per process, on stderr only."""
    if key not in _DEFAULT_DB_NOTICES:
        _DEFAULT_DB_NOTICES.add(key)
        print("[engraphis] %s" % message, file=sys.stderr)


def _backup_sqlite(src: Path, dst: Path) -> None:
    """Create and validate a consistent SQLite backup at *dst*.

    SQLite's backup API includes committed WAL content; copying only the main file can
    silently drop recent writes. The source remains untouched for rollback/recovery.
    """
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dst), timeout=30)
    try:
        source.execute("PRAGMA query_only=ON")
        source.backup(target)
        check = target.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("backup integrity check failed")
        target.commit()
    finally:
        target.close()
        source.close()


@contextmanager
def _migration_lock(target: Path):
    """Serialize first-run migration across processes without a third-party lock."""
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(".%s.migration.lock" % target.name)
    handle = open(lock_path, "a+b")  # noqa: SIM115 - held through the context yield
    locked = False
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _prepare_installed_db_default_unlocked(root: Path, target: Path) -> Path:
    """Preserve the unsafe pre-1.0 installed default when moving to user data.

    Engraphis 0.9.7 placed ``engraphis.db`` next to site-packages. A 1.0 process must
    not quietly open a new empty database while that file still contains user data.
    Migrate the memory and companion auth databases through staged SQLite backups,
    preserving the legacy files. A collision never overwrites either side.
    """
    legacy = root / "engraphis.db"
    if not legacy.is_file():
        return target
    if target.exists():
        _db_notice(
            "default-db-collision:%s" % target,
            "both the current database (%s) and preserved pre-1.0 database (%s) exist; "
            "using the current database without merging or overwriting either file"
            % (target, legacy),
        )
        return target

    pairs = []
    legacy_users = Path(str(legacy) + ".users.db")
    target_users = Path(str(target) + ".users.db")
    if legacy_users.is_file():
        pairs.append((legacy_users, target_users))
    # Publish the primary memory DB last: it is the migration's commit marker. If the
    # process or host dies between the two os.replace calls, the next start will either
    # see both files (complete) or only the auth companion and refuse to continue. The
    # reverse order could expose a primary DB without its users after a hard crash.
    pairs.append((legacy, target))
    if any(dst.exists() for _, dst in pairs):
        raise RuntimeError(
            "cannot migrate the pre-1.0 database because a destination companion "
            "already exists; set ENGRAPHIS_DB_PATH explicitly and reconcile the files"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    staged = []
    installed = []
    try:
        for src, dst in pairs:
            tmp = dst.with_name(".%s.migrating-%s" % (dst.name, uuid.uuid4().hex))
            staged.append((tmp, dst))
            _backup_sqlite(src, tmp)
        for tmp, dst in staged:
            os.replace(str(tmp), str(dst))
            installed.append(dst)
    except Exception as exc:
        # Publishing two databases cannot be one filesystem transaction. If the users DB
        # publish fails after memory succeeds, remove the newly-published copy so the next
        # run retries both from the preserved legacy sources instead of opening half a pair.
        for dst in reversed(installed):
            try:
                dst.unlink()
            except OSError:
                pass
        for tmp, _ in staged:
            try:
                tmp.unlink()
            except OSError:
                pass
        raise RuntimeError(
            "could not migrate the preserved pre-1.0 database at %s; no new database "
            "was opened. Set ENGRAPHIS_DB_PATH to that file to recover (%s)" %
            (legacy, exc)
        ) from None

    _db_notice(
        "default-db-migrated:%s" % target,
        "copied the preserved pre-1.0 database from %s to %s; the original remains "
        "untouched" % (legacy, target),
    )
    return target


def _prepare_installed_db_default(root: Path, target: Path) -> Path:
    """Run the preservation-first migration under a cross-process file lock.

    The unlocked implementation publishes both the memory and auth databases. Without
    serialization, two simultaneous first starts could overwrite or roll back each
    other's destination between the initial collision check and ``os.replace``.
    """
    legacy = root / "engraphis.db"
    if not legacy.is_file() or target.exists():
        return _prepare_installed_db_default_unlocked(root, target)
    with _migration_lock(target):
        return _prepare_installed_db_default_unlocked(root, target)


def _configured_db_path(root: Path = _PROJECT_ROOT) -> str:
    """Resolve an explicit override or prepare the safe installed default."""
    configured = _env("ENGRAPHIS_DB_PATH", "")
    if configured:
        return configured
    target = Path(_default_db_path(root))
    parts = {p.lower() for p in root.parts}
    if "site-packages" in parts or "dist-packages" in parts:
        target = _prepare_installed_db_default(root, target)
    return str(target)


#: Vendor-hosted managed sync service. Customer deployments normally override this with
#: their own dashboard URL; local Pro clients retain the managed default.
DEFAULT_RELAY_URL = "https://team.engraphis.com"

#: Isolated commercial control plane for paid-license leases, trials, fulfillment, and
#: transactional mail. Keeping this distinct from the dashboard removes the signing seed
#: and billing webhook secret from the customer-facing memory service.
DEFAULT_LICENSE_SERVER_URL = "https://license.engraphis.com"

SERVICE_MODES = ("customer", "vendor", "combined")

# Keys issued before the custom domain migration carry this URL inside their signed
# payload. Preserve the signature, but route that one retired vendor host to the current
# managed service. Arbitrary signed URLs remain authoritative.
RETIRED_RELAY_URLS = frozenset({
    "https://engraphis-production.up.railway.app",
})

# Existing signed keys point at the old combined host. License verification may migrate
# that exact vendor URL without altering arbitrary customer-signed endpoints.
RETIRED_LICENSE_SERVER_URLS = frozenset({
    "https://team.engraphis.com",
    "https://engraphis-production.up.railway.app",
})


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _validate_service_mode(value: str) -> str:
    """Validate service mode against allowed values.

    An explicitly-set invalid value exits the process rather than silently falling back
    to "combined" — a typo'd ENGRAPHIS_SERVICE_MODE silently becoming "combined" would
    merge the vendor and customer trust domains on a misconfigured deploy. Unset (the
    caller's own default of "combined") is always valid and never reaches this branch."""
    normalized = (value or "").strip().lower()
    if normalized not in SERVICE_MODES:
        print(f"[engraphis] invalid ENGRAPHIS_SERVICE_MODE '{value}' "
              f"(expected one of {', '.join(SERVICE_MODES)}); refusing to start with an "
              f"ambiguous trust boundary.", file=sys.stderr)
        sys.exit(1)
    return normalized


def _env_int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except ValueError:
        return default


_FALSY_ENV = {"0", "false", "no", "off", "disable", "disabled"}


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in _FALSY_ENV


def persist_project_env(values: dict[str, str], path: Optional[Path] = None) -> Path:
    """Upsert non-secret runtime settings in the project-local ``.env`` atomically.

    Dashboard controls use this for settings that must survive a restart. Explicit
    process-environment values still remain authoritative on the next launch because
    python-dotenv loads with ``override=False``.
    """
    target = Path(path) if path is not None else Path.cwd() / ".env"
    clean: dict[str, str] = {}
    for key, value in values.items():
        name = str(key or "").strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            raise ValueError("environment setting names must be uppercase identifiers")
        text = str(value)
        if "\n" in text or "\r" in text:
            raise ValueError("environment setting values must be single-line")
        clean[name] = text

    existed = target.exists()
    existing = target.read_text(encoding="utf-8") if existed else ""
    # Replacing an existing .env through a fresh default-mode file can silently widen
    # permissions from 0600 to 0644 while the preserved lines still contain API keys.
    # Carry the original mode forward; new files start private regardless of umask.
    try:
        mode = target.stat().st_mode & 0o777 if existed else 0o600
    except OSError:
        mode = 0o600
    lines = existing.splitlines()
    found: set[str] = set()
    rendered: list[str] = []
    for line in lines:
        match = re.match(r"^(\s*)(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
        if match and match.group(2) in clean:
            key = match.group(2)
            if key not in found:
                rendered.append(f"{match.group(1)}{key}={clean[key]}")
                found.add(key)
            continue
        rendered.append(line)
    if rendered and rendered[-1].strip():
        rendered.append("")
    for key, value in clean.items():
        if key not in found:
            rendered.append(f"{key}={value}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(
        f".{target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(rendered).rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
        except OSError:
            pass
        os.replace(temporary, target)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return target


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("ENGRAPHIS_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("ENGRAPHIS_PORT", 8700))

    # Optional bearer token. When non-empty, the REST API requires
    # `Authorization: Bearer <token>` on protected routes; health and the page shell stay public.
    api_token: str = field(default_factory=lambda: _env("ENGRAPHIS_API_TOKEN", ""))
    # Comma-separated CORS allow-list. Defaults to loopback only (local-first).
    cors_origins: list = field(
        default_factory=lambda: _parse_origins(_env("ENGRAPHIS_CORS_ORIGINS", ""),
                                               _env_int("ENGRAPHIS_PORT", 8700))
    )
    # Optional server-side workspace binding — the hard multi-tenant isolation boundary.
    # When non-empty, MemoryService refuses any read or write whose
    # workspace is not in this comma-separated allow-list, so knowing or guessing a
    # workspace name is not enough to reach it. Empty = unrestricted (single-tenant local).
    allowed_workspaces: list = field(
        default_factory=lambda: _parse_csv(_env("ENGRAPHIS_WORKSPACES", ""))
    )
    # Team auth is ON by default (opt-out); set ENGRAPHIS_TEAM_MODE=0/false/no/off to
    # disable it. A Team license gates paid capabilities and additional seats, while an
    # existing user store keeps its login wall even if entitlement later lapses.
    team_mode: bool = field(
        default_factory=lambda: _env("ENGRAPHIS_TEAM_MODE", "").lower()
        not in ("0", "false", "no", "off")
    )

    # Production roles are isolated. ``combined`` preserves local development and legacy
    # self-host behavior; the official Railway template sets ``customer`` and the vendor
    # control plane sets ``vendor``.
    service_mode: str = field(
        default_factory=lambda: _validate_service_mode(_env("ENGRAPHIS_SERVICE_MODE", "combined"))
    )

    # Managed relay base URL. Client sync uses it when `--relay-url` is omitted, and paid
    # license flows fall back to it when a signed key or explicit cloud override supplies
    # no URL. Set an empty ENGRAPHIS_RELAY_URL to require an explicit target.
    relay_url: str = field(default_factory=lambda: _env(
        "ENGRAPHIS_RELAY_URL", DEFAULT_RELAY_URL))

    db_path: str = field(
        default_factory=_configured_db_path
    )

    embed_model: str = field(
        default_factory=lambda: _env(
            "ENGRAPHIS_EMBED_MODEL",
            "sentence-transformers/all-MiniLM-L6-v2",
        )
    )
    embed_dim: Optional[int] = field(
        default_factory=lambda: (
            _env_int("ENGRAPHIS_EMBED_DIM", 384) or None
        )
    )

    # Fact extraction on the v2 write path: "none" (default — store text as given),
    # "chunk" (deterministic, offline structure-aware chunking — knobs
    # ENGRAPHIS_CHUNK_TOKENS/_OVERLAP/_MAX), or "llm" (distill raw text into discrete
    # facts via the configured LLM before storing).
    extractor: str = field(default_factory=lambda: _env("ENGRAPHIS_EXTRACTOR", "none").lower())

    llm_provider: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_PROVIDER", "openai").lower())
    llm_model: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_MODEL", "gpt-4o-mini"))
    llm_api_key: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_API_KEY", ""))
    llm_base_url: str = field(default_factory=lambda: _env("ENGRAPHIS_LLM_BASE_URL", ""))
    llm_extra_headers: dict = field(
        default_factory=lambda: _parse_headers(_env("ENGRAPHIS_LLM_EXTRA_HEADERS", ""))
    )
    # OFF by default (opt-in): a successful dashboard connection test enables
    # schema-validated extraction ONLY while the user has turned extraction on (the
    # Settings On/Off control, or ENGRAPHIS_LLM_AUTO_EXTRACT=1) — so a mere connection
    # test never silently starts provider egress of ingested content.
    llm_auto_extract: bool = field(
        default_factory=lambda: _env("ENGRAPHIS_LLM_AUTO_EXTRACT", "0").lower()
        not in ("0", "false", "no", "off")
    )

    # Optional cross-encoder reranker model. Empty (default) -> IdentityReranker (offline).
    rerank_model: str = field(default_factory=lambda: _env("ENGRAPHIS_RERANK_MODEL", ""))

    # Graph extractor for the knowledge-graph tab: "regex" (default) = dependency-free
    # heuristic NER, no API key, populated on every ingest; "none" disables graph
    # population. Defaults on so the Graph tab works out of the box for every install.
    graph_extractor: str = field(default_factory=lambda: _env("ENGRAPHIS_GRAPH_EXTRACTOR", "regex").lower())
    # Analytical Galaxy v2 is the validated default; setting the rollout flag to 0
    # restores the legacy ForceGraph surface for one compatibility release.
    graph_ui_v2: bool = field(
        default_factory=lambda: _env("ENGRAPHIS_GRAPH_UI_V2", "1").lower()
        not in ("0", "false", "no", "off")
    )

    # Optional host-LLM importance/retention classification. "none" keeps the fully
    # deterministic local write path; "llm" asks the configured provider for a bounded
    # ephemeral/normal/critical signal and degrades safely on any failure.
    retention_supervisor: str = field(
        default_factory=lambda: _env("ENGRAPHIS_RETENTION_SUPERVISOR", "none").lower()
    )

    loop_interval: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_INTERVAL", 60))
    loop_top_k: int = field(default_factory=lambda: _env_int("ENGRAPHIS_LOOP_TOP_K", 20))
    decay_halflife_days: float = field(
        default_factory=lambda: _env_float("ENGRAPHIS_DECAY_HALFLIFE_DAYS", 7.0)
    )

    # Optional in-process rate limiting for the v1 REST API (per-client-IP sliding window).
    # 0 = disabled (default), matching the loopback-first posture; set both to enable.
    rate_limit: int = field(default_factory=lambda: _env_int("ENGRAPHIS_RATE_LIMIT", 0))
    rate_window: int = field(default_factory=lambda: _env_int("ENGRAPHIS_RATE_WINDOW", 60))

    # Update reminder: check the newest published release and surface it in the dashboard,
    # server startup log, and MCP. On by default; ``ENGRAPHIS_UPDATE_CHECK=0`` opts out and
    # stops all network activity. ``ENGRAPHIS_UPDATE_URL`` overrides the default GitHub
    # releases source (see engraphis.update_check, the runtime authority for both knobs).
    update_check: bool = field(
        default_factory=lambda: _env_bool("ENGRAPHIS_UPDATE_CHECK", True))
    update_check_url: str = field(
        default_factory=lambda: _env("ENGRAPHIS_UPDATE_URL", ""))

    @property
    def base_url(self) -> str:
        """Connectable local base URL (wildcard binds map to loopback, IPv6 literals are
        bracketed — ``host='::'`` must not yield the malformed ``http://:::8700``)."""
        from engraphis.netutil import display_base_url
        return display_base_url(self.host, self.port)

    @property
    def customer_service(self) -> bool:
        return self.service_mode in ("customer", "combined")

    @property
    def vendor_service(self) -> bool:
        return self.service_mode in ("vendor", "combined")


def _parse_headers(raw: str) -> dict:
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _parse_origins(raw: str, port: int = 8700) -> list:
    """CORS allow-list. Empty -> loopback on the CONFIGURED port (safe local-first default).

    Deriving the default from ``port`` means running the dashboard on a non-default
    ENGRAPHIS_PORT doesn't lock its own origin out of the CORS allow-list."""
    if not raw.strip():
        return ["http://127.0.0.1:%d" % port, "http://localhost:%d" % port]
    return [o.strip() for o in raw.split(",") if o.strip()]


def _parse_csv(raw: str) -> list:
    """Generic comma-separated allow-list. Empty -> [] (no restriction)."""
    return [item.strip() for item in raw.split(",") if item.strip()]


settings = Settings()


def canonicalize_relay_url(url: str) -> str:
    """Normalize a relay URL and migrate known retired vendor hosts."""
    normalized = (url or "").strip().rstrip("/")
    return DEFAULT_RELAY_URL if normalized in RETIRED_RELAY_URLS else normalized


def canonicalize_license_server_url(url: str) -> str:
    """Normalize a license-server URL and migrate the retired combined host."""
    normalized = (url or "").strip().rstrip("/")
    return (DEFAULT_LICENSE_SERVER_URL
            if normalized in RETIRED_LICENSE_SERVER_URLS else normalized)


def resolve_license_server_url(signed_url: str = "") -> str:
    """Resolve the license server, including known vendor-host migrations."""
    override = canonicalize_license_server_url(_env("ENGRAPHIS_CLOUD_URL", ""))
    signed = canonicalize_license_server_url(signed_url)
    return override or signed or DEFAULT_LICENSE_SERVER_URL
