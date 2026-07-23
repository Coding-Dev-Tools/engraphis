"""Central configuration — all values sourced from env with safe defaults."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import stat
import sys
from contextlib import contextmanager
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from engraphis.private_state import (
    UnsafeStateFile,
    atomic_private_text,
    private_file_stat,
    read_private_text,
)

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
    source_info = private_file_stat(src)
    if private_file_stat(dst, allow_missing=True) is not None:
        raise FileExistsError("database migration stage already exists")
    flags = (
        os.O_RDWR | os.O_CREAT | os.O_EXCL
        | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(str(dst), flags, 0o600)
    created_info = os.fstat(descriptor)
    os.close(descriptor)
    source = sqlite3.connect(str(src), timeout=30)
    target = sqlite3.connect(str(dst), timeout=30)
    try:
        if not _same_identity(source_info, private_file_stat(src)):
            raise UnsafeStateFile("database migration source changed while opening")
        if not _same_identity(created_info, private_file_stat(dst)):
            raise UnsafeStateFile("database migration stage changed while opening")
        source.execute("PRAGMA query_only=ON")
        source.backup(target)
        check = target.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("backup integrity check failed")
        target.commit()
    finally:
        target.close()
        source.close()
    final_info = private_file_stat(dst)
    if not _same_identity(created_info, final_info):
        raise UnsafeStateFile("database migration stage changed while writing")
    descriptor = os.open(
        str(dst), os.O_RDWR | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if not _same_identity(final_info, opened):
            raise UnsafeStateFile("database migration stage changed before flush")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _same_identity(left, right) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _fsync_parent(path: Path) -> None:
    """Persist directory-entry ordering on platforms that expose directory fsync."""
    if os.name == "nt":
        return
    descriptor = os.open(
        str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _unlink_if_identity(path: Path, identity) -> bool:
    try:
        current = os.lstat(str(path))
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(current.st_mode) or not _same_identity(current, identity):
        return False
    path.unlink()
    return True


def _publish_no_replace(source: Path, destination: Path):
    """Atomically publish one same-filesystem stage without replacing a collision."""
    source_info = private_file_stat(source)
    linked = False
    try:
        os.link(str(source), str(destination))
        linked = True
        published = os.lstat(str(destination))
        if not stat.S_ISREG(published.st_mode) or not _same_identity(
                source_info, published):
            raise UnsafeStateFile("database migration publication changed")
        source.unlink()
        durable = os.lstat(str(destination))
        if not _same_identity(source_info, durable):
            raise UnsafeStateFile("database migration publication was replaced")
        _fsync_parent(destination)
        return durable
    except BaseException:
        if linked:
            try:
                if _unlink_if_identity(destination, source_info):
                    _fsync_parent(destination)
            except OSError:
                pass
        raise


def _sqlite_logical_digest(path: Path) -> str:
    """Hash a validated SQLite database's logical dump without logging its contents."""
    private_file_stat(path)
    connection = sqlite3.connect(str(path), timeout=30)
    digest = hashlib.sha256()
    try:
        connection.execute("PRAGMA query_only=ON")
        check = connection.execute("PRAGMA quick_check").fetchone()
        if not check or check[0] != "ok":
            raise sqlite3.DatabaseError("database integrity check failed")
        for statement in connection.iterdump():
            digest.update(statement.encode("utf-8"))
            digest.update(b"\n")
    finally:
        connection.close()
    return digest.hexdigest()


def _cleanup_stale_migration_stages(target: Path) -> None:
    """Remove only this migration's randomized, hard-crash staging artifacts."""
    pattern = re.compile(
        r"^\.%s\.migrating-[0-9a-f]{32}$" % re.escape(target.name))
    try:
        entries = tuple(target.parent.iterdir())
    except OSError:
        return
    for entry in entries:
        if pattern.fullmatch(entry.name):
            try:
                info = os.lstat(str(entry))
                if not stat.S_ISREG(info.st_mode):
                    continue
                if getattr(info, "st_nlink", 1) == 1:
                    entry.unlink()
                    continue
                try:
                    published = os.lstat(str(target))
                except FileNotFoundError:
                    continue
                if _same_identity(info, published):
                    entry.unlink()
            except OSError:
                pass


@contextmanager
def _migration_lock(target: Path):
    """Serialize first-run migration across processes without a third-party lock."""
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(target.parent, 0o700)
    except OSError:
        pass
    lock_path = target.with_name(".%s.migration.lock" % target.name)
    expected = private_file_stat(lock_path, allow_missing=True)
    flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if expected is None:
        try:
            descriptor = os.open(str(lock_path), flags | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            expected = private_file_stat(lock_path)
            descriptor = os.open(str(lock_path), flags)
    else:
        descriptor = os.open(str(lock_path), flags)
    try:
        opened = os.fstat(descriptor)
        current = private_file_stat(lock_path)
        changed = (
            (expected is not None and not _same_identity(expected, opened))
            or not _same_identity(opened, current)
        )
    except BaseException:
        os.close(descriptor)
        raise
    if changed:
        os.close(descriptor)
        raise UnsafeStateFile("migration lock changed while it was opened")
    handle = os.fdopen(descriptor, "r+b")  # held through the context yield
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
    legacy_users = Path(str(legacy) + ".users.db")
    target_users = Path(str(target) + ".users.db")
    target.parent.mkdir(parents=True, exist_ok=True)
    # A power loss can bypass Python cleanup after a complete SQLite backup but before
    # either destination is published. Remove only this migration's random, redundant
    # stages before retrying; the preserved legacy databases remain authoritative.
    _cleanup_stale_migration_stages(target)
    _cleanup_stale_migration_stages(target_users)
    try:
        target_info = private_file_stat(target, allow_missing=True)
        target_users_info = private_file_stat(target_users, allow_missing=True)
    except UnsafeStateFile as exc:
        raise RuntimeError(
            "cannot migrate the pre-1.0 database through a linked or unsafe destination"
        ) from exc
    if target_info is not None:
        if legacy_users.is_file() and target_users_info is None:
            raise RuntimeError(
                "the current database exists without its expected auth companion; set "
                "ENGRAPHIS_DB_PATH explicitly and reconcile the files")
        _db_notice(
            "default-db-collision:%s" % target,
            "both the current database (%s) and preserved pre-1.0 database (%s) exist; "
            "using the current database without merging or overwriting either file"
            % (target, legacy),
        )
        return target

    pairs = []
    if target_users_info is not None:
        # A hard process/host death after the auth publish but before the primary publish
        # leaves exactly this state.  Resume only when the companion is a byte-independent
        # logical match for the still-preserved legacy source; any other collision remains
        # a release-blocking ambiguity.
        if not legacy_users.is_file():
            raise RuntimeError(
                "cannot resume the pre-1.0 migration because an unexpected auth "
                "companion already exists")
        try:
            matches = _sqlite_logical_digest(legacy_users) == \
                _sqlite_logical_digest(target_users)
        except (OSError, sqlite3.Error, UnsafeStateFile) as exc:
            raise RuntimeError(
                "cannot validate the interrupted auth-database migration (%s)" %
                type(exc).__name__) from None
        if not matches:
            raise RuntimeError(
                "cannot resume the pre-1.0 migration because the destination auth "
                "companion does not match the preserved source")
    elif legacy_users.is_file():
        pairs.append((legacy_users, target_users))
    # Publish the primary memory DB last: it is the migration's commit marker. If the
    # process or host dies between the two os.replace calls, the next start will either
    # see both files (complete) or only the auth companion and refuse to continue. The
    # reverse order could expose a primary DB without its users after a hard crash.
    pairs.append((legacy, target))
    if any(private_file_stat(dst, allow_missing=True) is not None for _, dst in pairs):
        raise RuntimeError(
            "cannot migrate the pre-1.0 database because a destination companion "
            "already exists; set ENGRAPHIS_DB_PATH explicitly and reconcile the files"
        )

    staged = []
    installed = []
    try:
        for src, dst in pairs:
            tmp = dst.with_name(".%s.migrating-%s" % (dst.name, uuid.uuid4().hex))
            staged.append((tmp, dst))
            _backup_sqlite(src, tmp)
        for tmp, dst in staged:
            identity = _publish_no_replace(tmp, dst)
            installed.append((dst, identity))
    except Exception as exc:
        # Publishing two databases cannot be one filesystem transaction. If the users DB
        # publish fails after memory succeeds, remove the newly-published copy so the next
        # run retries both from the preserved legacy sources instead of opening half a pair.
        for dst, identity in reversed(installed):
            try:
                if _unlink_if_identity(dst, identity):
                    _fsync_parent(dst)
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


#: Vendor-hosted managed sync service. The account dashboard remains at
#: ``team.engraphis.com``; sync traffic goes to its separate relay endpoint.
DEFAULT_RELAY_URL = "https://relay.engraphis.com"

SERVICE_MODES = ("customer",)
# The public package is a customer data plane and contains no vendor authority or hosted
# relay implementation. Private services are built and deployed from a separate repository.
DEFAULT_SERVICE_MODE = "customer"

# Keys issued before the custom domain migration carry this URL inside their signed
# payload. Preserve the signature, but route that one retired vendor host to the current
# managed service. Arbitrary signed URLs remain authoritative.
RETIRED_RELAY_URLS = frozenset({
    "https://engraphis-production.up.railway.app",
    "https://team.engraphis.com",
})

def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()

def _validate_service_mode(value: str) -> str:
    """Validate service mode against allowed values.

    The public package accepts only ``customer``. Hosted vendor, relay, and worker roles
    live in a private service repository and cannot be enabled through configuration."""
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

    source_stat = private_file_stat(target, allow_missing=True)
    existed = source_stat is not None
    existing = (read_private_text(target, max_bytes=1024 * 1024) or "") if existed else ""
    # Replacing an existing .env through a fresh default-mode file can silently widen
    # permissions from 0600 to 0644 while the preserved lines still contain API keys.
    # Carry the original mode forward; new files start private regardless of umask.
    mode = source_stat.st_mode & 0o777 if source_stat is not None else 0o600
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

    atomic_private_text(
        target, "\n".join(rendered).rstrip() + "\n", mode=mode,
        expected_stat=source_stat)
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
    # The public package is always the customer runtime. Hosted service roles are private.
    service_mode: str = field(
        default_factory=lambda: _validate_service_mode(
            _env("ENGRAPHIS_SERVICE_MODE", DEFAULT_SERVICE_MODE)
        )
    )

    # Managed relay base URL. Client sync uses it when `--relay-url` is omitted. Set an
    # empty ENGRAPHIS_RELAY_URL to require an explicit target.
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

    llm_provider: str = field(
        default_factory=lambda: _env("ENGRAPHIS_LLM_PROVIDER", "openai").lower()
    )
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
    graph_extractor: str = field(
        default_factory=lambda: _env(
            "ENGRAPHIS_GRAPH_EXTRACTOR", "regex"
        ).lower()
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
        return self.service_mode == "customer"


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
