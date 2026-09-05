"""engraphis-init - one command from `pip install` to a configured, agent-connected setup.

Closes the biggest first-run gap: with no configuration, an installed build puts its
database in the platform user-data directory, where most people never think to look.
This command writes the process-selected trusted config file with an explicit absolute
DB path and a local API token, then prints exact MCP snippets to paste into Codex,
Claude Code / Cursor / Cline / Zed.

    engraphis-init                 # write ~/.engraphis/config.env
    engraphis-init --db ~/mem.db   # choose the database location
    engraphis-init --token         # compatibility flag: new configs always receive a local API token
    engraphis-init --encrypted     # require SQLCipher and provision a private DB key file
    engraphis-init --force         # overwrite the trusted config file
    engraphis-init --check         # doctor: verify install, extras, DB writability
    engraphis-init --prefetch      # pre-cache embedding model weights for instant MCP startup

Non-interactive by design (no prompts): safe in scripts, CI, and agent shells.
"""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any, Optional

from engraphis.backends.encrypted_db import connector_from_env
from engraphis.private_state import (
    atomic_private_text,
    ensure_owner_private_dir,
    read_private_text,
)


_HEX64 = set("0123456789abcdef")


def _ok(label: str, detail: str = "") -> None:
    print(f"  [ok]      {label}" + (f" - {detail}" if detail else ""))


def _miss(label: str, detail: str = "") -> None:
    print(f"  [--]      {label}" + (f" - {detail}" if detail else ""))


def _fail(label: str, detail: str = "") -> None:
    print(f"  [FAIL]    {label}" + (f" - {detail}" if detail else ""))


def _try_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


def cmd_check(*, json_output: bool = False) -> int:
    """Doctor: report what's installed and whether the configured DB is usable."""
    checks: list[dict[str, str]] = []

    def report(code: str, status: str, label: str, detail: str = "") -> None:
        checks.append({"code": code, "status": status, "label": label, "detail": detail})

    def fail_database(exc: Exception) -> None:
        message = str(exc).lower()
        code = "database_locked" if "locked" in message or "busy" in message else "database_unwritable"
        detail = "Close the process holding the database lock and retry." if code == "database_locked" else (
            "Check the database path, directory permissions, free disk space and configured encryption key."
        )
        report(code, "fail", "database writable", detail)

    if _try_import("numpy") is None:
        report("numpy", "fail", "numpy (required core)", "pip install numpy")
    else:
        report("numpy", "ok", "numpy (required core)")

    for mod, label, hint in [
        ("mcp", "MCP server extra", 'pip install "engraphis[mcp]"'),
        ("fastapi", "REST/Inspector extra", 'pip install "engraphis[server]"'),
        ("sentence_transformers", "real embeddings",
         "optional - deterministic offline embedder is the fallback"),
        ("tree_sitter", "AST code indexing",
         "optional - regex code indexer is the fallback"),
    ]:
        available = _try_import(mod) is not None
        report(mod, "ok" if available else "optional", label, "" if available else hint)

    if _try_import("pytesseract") is not None:
        ocr = shutil.which("tesseract") is not None
        report("ocr_executable", "ok" if ocr else "optional", "OCR executable",
               "" if ocr else "Install Tesseract to enable image OCR; other document formats remain available.")

    from engraphis.config import settings
    db_name = str(settings.db_path)
    db = Path(db_name).expanduser()
    conn: Any = None
    database_stage = "connection"
    try:
        if db_name != ":memory:":
            db.parent.mkdir(parents=True, exist_ok=True)
        connector = connector_from_env()
        conn = (
            connector(str(db))
            if connector is not None
            else sqlite3.connect(str(db), timeout=2.0)
        )
        conn.execute("PRAGMA busy_timeout=2000")
        conn.execute("PRAGMA user_version").fetchone()
        report("database_readable", "ok", "database readable", str(db))
        # A TEMP table would only probe the temp database. Exercise the main database
        # under a transaction, then roll back both schema and data unconditionally.
        probe = "_engraphis_doctor_" + secrets.token_hex(12)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(f'CREATE TABLE "{probe}" (value INTEGER)')
            conn.execute(f'INSERT INTO "{probe}" (value) VALUES (1)')
        finally:
            conn.rollback()
        report("database_writable", "ok", "database writable", str(db))
        database_stage = "schema"
        from engraphis.core.schema import SCHEMA_VERSION
        has_migrations = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if has_migrations:
            version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] or 0
            if version > SCHEMA_VERSION:
                report("schema_newer", "fail", "database schema",
                       "This database requires a newer Engraphis version. Upgrade before opening it.")
            else:
                report("database_schema", "ok", "database schema",
                       f"version {version}; supported through {SCHEMA_VERSION}")
        else:
            report("database_schema", "optional", "database schema",
                   "Not initialized; the engine will initialize the database on first use.")
    except Exception as exc:
        if database_stage == "schema":
            report("database_schema", "fail", "database schema",
                   "The schema could not be verified. Check this database with the matching Engraphis version.")
        else:
            fail_database(exc)
    finally:
        if conn is not None:
            conn.close()

    report("local_core", "ok", "local core", "single-user features available without a hosted subscription")
    if settings.api_token:
        report("browser_approval", "ok", "source review", "Use engraphis-dashboard to open an authenticated local browser.")
    else:
        report("browser_approval", "optional", "source review",
               "Prompt approval needs a local API token. Set ENGRAPHIS_API_TOKEN in your private config, "
               "restart the dashboard and open it with engraphis-dashboard. Tokenless browsing remains available.")
    try:
        from engraphis.cloud_session import configured
        if configured(require_compute=False):
            report("cloud", "ok", "Engraphis Cloud", "installation connected")
        else:
            report("cloud", "optional", "Engraphis Cloud", "not connected (optional for the local core)")
    except Exception:
        report("cloud", "optional", "Engraphis Cloud", "saved session unavailable; reconnect if needed")

    try:
        from engraphis.backends.embedder_st import get_embedder
        emb = get_embedder(settings.embed_model or None, dim=settings.embed_dim or 384,
                           require_exact=bool(settings.embed_model))
        emb.embed(["engraphis doctor check"])
        report("embedder", "ok", "embedder functional", f"{type(emb).__name__} ({getattr(emb, 'dim', 384)}d)")
    except Exception as exc:
        report("embedder", "fail", "embedder functional",
               f"{type(exc).__name__}: check the configured model and its dependencies, or select offline embeddings.")

    failures = sum(check["status"] == "fail" for check in checks)
    if json_output:
        print(json.dumps({"schema_version": 1, "python": sys.version.split()[0],
                          "ok": failures == 0, "failures": failures, "checks": checks}))
    else:
        print(f"engraphis doctor - python {sys.version.split()[0]}")
        for check in checks:
            {"ok": _ok, "optional": _miss, "fail": _fail}[check["status"]](check["label"], check["detail"])
        print("all good" if failures == 0 else f"{failures} problem(s) found")
    return 0 if failures == 0 else 1


def cmd_prefetch() -> int:
    """Download and warm up the configured embedding model ahead of time."""
    from engraphis.config import settings
    model_name = (settings.embed_model or "").strip()
    if not model_name:
        print("  [--] No remote embedding model configured; deterministic offline embedder is active.")
        return 0
    print(f"engraphis prefetch - model '{model_name}'")
    try:
        from engraphis.backends.embedder_st import get_embedder
        emb = get_embedder(model_name, dim=settings.embed_dim or 384, require_exact=True)
        emb.embed(["engraphis prefetch warmup"])
        _ok("model prefetch", f"{type(emb).__name__} ({getattr(emb, 'dim', 384)}d) ready")
        return 0
    except Exception as exc:
        _fail("model prefetch", f"{type(exc).__name__}: {exc}")
        return 1


def _env_content(db_path: Path, token: str, key_path: Optional[Path] = None) -> str:
    lines = [
        "# Engraphis - generated by engraphis-init. Full reference: .env.example",
        f"ENGRAPHIS_DB_PATH={db_path}",
    ]
    if token:
        lines += [
            "# Bearer token required by the REST server & Inspector APIs:",
            f"ENGRAPHIS_API_TOKEN={token}",
        ]
    if key_path is not None:
        lines += [
            "# SQLCipher database key file, generated with owner-only permissions:",
            f"ENGRAPHIS_DB_KEY_FILE={key_path}",
        ]
    lines += [
        "# Pro and Team are hosted. Connect through the Engraphis Cloud account portal;",
        "# never paste access or refresh credentials into this configuration file.",
        "# ENGRAPHIS_CLOUD_CONTROL_URL=https://control.example.com",
        "# ENGRAPHIS_CLOUD_COMPUTE_URL=https://compute.example.com",
    ]
    return "\n".join(lines) + "\n"


def _write_env(
    path: Path,
    content: str,
    *,
    owner_private_parent: bool = False,
) -> None:
    """Atomically replace one private configuration or key file."""
    if owner_private_parent:
        ensure_owner_private_dir(path.parent)
    atomic_private_text(path, content)


def _key_path_for(db_path: Path) -> Path:
    """Return the private sidecar key location for a newly encrypted database."""
    return db_path.with_name(f".{db_path.name}.key")


def _private_file_content(path: Path) -> str:
    """Read an existing generated key without printing its contents."""
    try:
        value = (read_private_text(path, max_bytes=128) or "").strip()
    except OSError as exc:
        raise RuntimeError(f"could not read database key file {path}: {exc}") from exc
    if len(value) != 64 or any(character not in _HEX64 for character in value.casefold()):
        raise RuntimeError(
            f"database key file {path} must contain exactly 32 random bytes encoded as hex"
        )
    return value


def _provision_db_key(db_path: Path) -> Path:
    """Create or validate a sidecar SQLCipher key outside the trusted config.

    An existing database without this key is intentionally rejected.  Silently attaching a
    fresh key would make an existing plaintext database inaccessible and could tempt a user
    to overwrite it.  SQLCipher conversion is a separate, deliberate migration operation.
    """
    key_path = _key_path_for(db_path)
    if key_path.exists():
        _private_file_content(key_path)
        return key_path
    if db_path.exists():
        raise RuntimeError(
            "refusing to enable encryption for an existing database without its key file; "
            "migrate the database to SQLCipher first or choose a new --db path"
        )
    _write_env(key_path, secrets.token_hex(32) + "\n")
    return key_path


def _read_existing_env(env_file: Path) -> str:
    """Read one bounded, owner-only trusted config snapshot."""
    return read_private_text(
        env_file,
        max_bytes=1024 * 1024,
        owner_only=True,
    ) or ""


def _existing_env_value(content: str, name: str) -> str:
    """Read one simple assignment from trusted config content."""
    for line in content.splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("\"'")
    return ""


def _existing_db_path(env_file: Path, content: str, fallback: Path) -> Path:
    """Read the simple ENGRAPHIS_DB_PATH assignment emitted by this command."""
    raw = _existing_env_value(content, "ENGRAPHIS_DB_PATH")
    if raw:
        configured = Path(raw).expanduser()
        return (
            configured
            if configured.is_absolute()
            else (env_file.parent / configured).resolve()
        )
    return fallback


def _trusted_env_file() -> Path:
    """Return the process-fixed private configuration path."""
    from engraphis.config import trusted_env_path

    return trusted_env_path()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="engraphis-init", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default="engraphis.db",
                    help="database file (default: ./engraphis.db)")
    ap.add_argument("--token", action="store_true",
                    help="compatibility flag: new configuration always receives a local API token")
    encryption = ap.add_mutually_exclusive_group()
    encryption.add_argument(
        "--encrypted", action="store_true",
        help="require SQLCipher and generate a private 32-byte database key file",
    )
    encryption.add_argument(
        "--no-encryption", action="store_true",
        help="do not enable SQLCipher even when its driver is installed",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="overwrite the existing trusted config file",
    )
    ap.add_argument("--check", action="store_true",
                    help="doctor mode: verify the installation without writing config")
    ap.add_argument("--json", action="store_true", help="emit structured doctor diagnostics (requires --check)")
    ap.add_argument("--extras", help="record installed capabilities for future updates, e.g. server,mcp or none")
    ap.add_argument("--prefetch", action="store_true",
                    help="pre-cache the configured embedding model for instant MCP startup")
    args = ap.parse_args(argv)
    if args.json and not args.check:
        ap.error("--json requires --check")

    if args.check:
        return cmd_check(json_output=args.json)
    if args.prefetch:
        return cmd_prefetch()

    db_path = Path(args.db).expanduser().resolve()
    try:
        env_file = _trusted_env_file()
    except (OSError, RuntimeError, ValueError) as exc:
        _fail("trusted configuration", str(exc))
        return 1
    # Approval is intentionally restricted to an authenticated local browser. Give
    # new setups a usable review path while leaving every existing config untouched.
    token = secrets.token_urlsafe(24)
    from scripts.installation_profile import normalize_extras, write_profile
    try:
        selected_extras = normalize_extras(args.extras) if args.extras is not None else None
    except ValueError as exc:
        ap.error(str(exc))
    sqlcipher_available = _try_import("sqlcipher3") is not None
    if args.encrypted and not sqlcipher_available:
        _fail("SQLCipher encryption", 'install it with: pip install "engraphis[encryption]"')
        return 1
    use_encryption = (args.encrypted or sqlcipher_available) and not args.no_encryption
    key_path: Optional[Path] = None

    if env_file.exists() and not args.force:
        try:
            existing_env = _read_existing_env(env_file)
        except OSError as exc:
            _fail("trusted configuration", str(exc))
            return 1
        print(
            f"trusted config already exists at {env_file} - kept "
            "(use --force to overwrite)."
        )
        db_path = _existing_db_path(env_file, existing_env, db_path)
        existing_key = _existing_env_value(existing_env, "ENGRAPHIS_DB_KEY_FILE")
        if existing_key:
            key_path = Path(existing_key).expanduser()
    else:
        if use_encryption:
            try:
                key_path = _provision_db_key(db_path)
            except RuntimeError as exc:
                _fail("SQLCipher encryption", str(exc))
                return 1
        try:
            _write_env(
                env_file,
                _env_content(db_path, token, key_path),
                owner_private_parent=True,
            )
        except OSError as exc:
            _fail("trusted configuration", str(exc))
            return 1
        print(f"wrote {env_file}")
        print(f"  database -> {db_path}")
        if db_path.parent == Path.cwd():
            print("  note: this database path is pinned to the current directory; "
                  "runtime tools will use this pinned path (ENGRAPHIS_DB_PATH "
                  "overrides it).")
        if key_path is not None:
            print(f"  encryption -> SQLCipher key file {key_path}")
        elif not args.no_encryption:
            _miss("SQLCipher encryption", 'not installed; use --encrypted after pip install "engraphis[encryption]"')
        if token:
            print("  api token -> generated (in trusted config; send as 'Authorization: Bearer ...')")

    if selected_extras is not None:
        try:
            write_profile(selected_extras, config_path=env_file)
        except OSError:
            _fail("installation profile", "Could not record capabilities in the private configuration directory.")
            return 1
        print("  update capabilities -> " + (",".join(selected_extras) or "base package only"))

    mcp_env = {"ENGRAPHIS_DB_PATH": str(db_path)}
    if key_path is not None:
        mcp_env["ENGRAPHIS_DB_KEY_FILE"] = str(key_path)
    snippet = {"mcpServers": {"engraphis": {
        "command": "engraphis-mcp",
        "env": mcp_env,
    }}}
    print("\nConnect your agent - Claude Code:")
    command = f'  claude mcp add engraphis --env ENGRAPHIS_DB_PATH="{db_path}"'
    if key_path is not None:
        command += f' --env ENGRAPHIS_DB_KEY_FILE="{key_path}"'
    print(command + " -- engraphis-mcp")
    print("\nConnect your agent - Codex:")
    codex_command = f'  codex mcp add engraphis --env ENGRAPHIS_DB_PATH="{db_path}"'
    if key_path is not None:
        codex_command += f' --env ENGRAPHIS_DB_KEY_FILE="{key_path}"'
    print(codex_command + " -- engraphis-mcp")
    print("\nCursor / Cline / Zed / Windsurf (mcp config):")
    print(json.dumps(snippet, indent=2))
    print("\nNext steps:")
    print("  engraphis-dashboard      # product UI on http://127.0.0.1:8700")
    print("  engraphis-init --check   # verify the install")
    print("  In the dashboard: create a workspace, save one project decision, review its source and Approve for prompt.")
    print("  Then Ask about the decision to see its cited source.")
    print("  Open its citation to review the source; edit the record when the decision changes.")
    print("  Free forever at the core - start the 3-day Pro trial or subscribe at "
          "https://api.engraphis.com/account?plan=pro&interval=monthly#billing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
