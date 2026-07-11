"""Team-mode auth for the Memory Inspector (Pro feature ``team``).

Design constraints, in order:

* **Off by default.** Without ``ENGRAPHIS_TEAM_MODE=1`` *and* a ``team`` license this
  module is never constructed; the single-user Inspector is untouched.
* **stdlib only** — PBKDF2-HMAC-SHA256 (:data:`PBKDF2_ITERATIONS`), ``secrets`` tokens,
  SQLite. Session tokens are stored **hashed** (SHA-256): a leaked users DB does not
  yield usable cookies. Passwords: ≥ :data:`MIN_PASSWORD_LEN` chars, per-user salt.
* **Server-side roles.** viewer < member < admin, enforced by the HTTP layer on every
  request (`engraphis/inspector/app.py`); the UI only *hides* what the server already
  refuses.
* **Single-process posture** (same as the rest of the Inspector): the login throttle is
  in-memory, which is exactly right until multi-process hosting exists.

Users live in a *separate* SQLite file next to the memory DB (``<db>.users.db``) — auth
state is not memory state, and backup/restore of one must not drag the other along.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from typing import Optional

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 10
SESSION_TTL_SECONDS = 12 * 3600
LOCKOUT_FAILS = 5           # failures within LOCKOUT_WINDOW …
LOCKOUT_WINDOW = 900        # … lock the account for LOCKOUT_SECONDS
LOCKOUT_SECONDS = 60

ROLES = ("viewer", "member", "admin")
_ROLE_RANK = {r: i for i, r in enumerate(ROLES)}

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,64}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    email      TEXT NOT NULL UNIQUE,
    name       TEXT DEFAULT '',
    role       TEXT NOT NULL CHECK (role IN ('viewer','member','admin')),
    pw_hash    TEXT NOT NULL,
    created_at REAL NOT NULL,
    disabled   INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sess_user ON auth_sessions(user_id);
"""


class AuthError(ValueError):
    """User-facing auth failure; message is safe to surface."""


def role_at_least(role: str, minimum: str) -> bool:
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(minimum, 99)


def min_role(method: str, path: str) -> str:
    """Least role allowed to touch ``path`` in team mode. Server-side is the source of
    truth — the UI merely hides what this table already refuses. Shared by every app
    that mounts team-mode auth (``inspector/app.py``, ``dashboard_app.py``) so the
    policy can't drift between them."""
    if path.startswith("/api/auth/users") or path in (
            "/api/license/activate", "/api/export", "/api/consolidate"):
        return "admin"
    if method == "POST":            # pin / forget / correct — audited governance
        return "member"
    return "viewer"


def _hash_password(password: str, *, iterations: int, salt: Optional[bytes] = None) -> str:
    salt = salt if salt is not None else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt.hex(), digest.hex())


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algo, iters, salt_hex, digest_hex = encoded.split("$")
        if algo != "pbkdf2_sha256":
            return False
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(iters))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


class AuthStore:
    """Users + sessions for team mode. One instance per Inspector process."""

    def __init__(self, db_path: str, *, iterations: int = PBKDF2_ITERATIONS) -> None:
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self.iterations = int(iterations)
        self._failures: dict = {}   # email -> list[fail_ts] (in-memory throttle)
        self._last_prune: float = 0.0

    # ── users ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _clean_email(email) -> str:
        email = (email or "").strip().lower()
        if not _EMAIL_RE.match(email):
            raise AuthError("invalid email address")
        return email

    def create_user(self, email: str, name: str, password: str, role: str,
                    *, seat_limit: Optional[int] = None) -> dict:
        from engraphis.licensing import require_feature
        require_feature("team")
        email = self._clean_email(email)
        name = (name or "").strip()[:120]
        if role not in ROLES:
            raise AuthError("role must be one of: %s" % ", ".join(ROLES))
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
            raise AuthError("password must be at least %d characters" % MIN_PASSWORD_LEN)
        if seat_limit is not None and self.count_active_users() >= seat_limit:
            raise AuthError(
                "seat limit reached (%d) — upgrade your Team license for more seats"
                % seat_limit)
        uid = "usr_" + secrets.token_hex(8)
        try:
            self.conn.execute(
                "INSERT INTO users (id, email, name, role, pw_hash, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uid, email, name, role,
                 _hash_password(password, iterations=self.iterations), time.time()))
            self.conn.commit()
        except sqlite3.IntegrityError:
            raise AuthError("a user with that email already exists")
        return self.get_user(uid)

    def get_user(self, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, email, name, role, created_at, disabled FROM users WHERE id=?",
            (user_id,)).fetchone()
        return dict(row) if row else None

    def list_users(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, email, name, role, created_at, disabled FROM users "
            "ORDER BY created_at")]

    def count_users(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    def count_active_users(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE disabled=0").fetchone()["n"])

    def _count_active_admins(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        ).fetchone()["n"])

    def update_user(self, user_id: str, *, role: Optional[str] = None,
                    disabled: Optional[bool] = None,
                    seat_limit: Optional[int] = None) -> dict:
        user = self.get_user(user_id)
        if user is None:
            raise AuthError("no such user")
        # Never let the last active admin lock everyone out.
        losing_admin = (user["role"] == "admin" and not user["disabled"] and
                        ((role is not None and role != "admin") or disabled))
        if losing_admin and self._count_active_admins() <= 1:
            raise AuthError("cannot demote or disable the last active admin")
        # Re-enabling a disabled user consumes a seat, exactly like creating one, so it must
        # honour the same licensed cap. Without this a team could exceed its paid seats via
        # disable → add a replacement → re-enable the original — a path create_user's own
        # seat check can't see.
        reenabling = disabled is False and bool(user["disabled"])
        if reenabling and seat_limit is not None and self.count_active_users() >= seat_limit:
            raise AuthError(
                "seat limit reached (%d) — upgrade your Team license for more seats"
                % seat_limit)
        if role is not None:
            if role not in ROLES:
                raise AuthError("role must be one of: %s" % ", ".join(ROLES))
            self.conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
        if disabled is not None:
            self.conn.execute("UPDATE users SET disabled=? WHERE id=?",
                              (1 if disabled else 0, user_id))
            if disabled:
                self.revoke_user_sessions(user_id)
        self.conn.commit()
        return self.get_user(user_id)

    # ── login throttle (in-memory, per-process) ────────────────────────────────
    def _locked_until(self, email: str) -> float:
        now = time.time()
        fails = [t for t in self._failures.get(email, []) if now - t < LOCKOUT_WINDOW]
        self._failures[email] = fails
        if len(fails) >= LOCKOUT_FAILS:
            return fails[-1] + LOCKOUT_SECONDS
        return 0.0

    def login(self, email: str, password: str) -> dict:
        """Verify credentials → new session. Raises :class:`AuthError` (generic message —
        never reveals which of email/password was wrong) or the lockout notice."""
        email = self._clean_email(email)
        until = self._locked_until(email)
        if until > time.time():
            raise AuthError("too many failed attempts — try again in a minute")
        row = self.conn.execute(
            "SELECT id, pw_hash, disabled FROM users WHERE email=?", (email,)).fetchone()
        # Always run one PBKDF2 even for unknown emails (no user-enumeration timing).
        encoded = row["pw_hash"] if row else _hash_password("x", iterations=self.iterations)
        ok = _verify_password(password or "", encoded)
        if not ok or row is None or row["disabled"]:
            self._failures.setdefault(email, []).append(time.time())
            raise AuthError("invalid email or password")
        self._failures.pop(email, None)
        token = self.create_session(row["id"])
        user = self.get_user(row["id"])
        user["token"] = token
        return user

    # ── sessions (raw token to the client, hash in the DB) ─────────────────────
    def create_session(self, user_id: str, *, ttl: int = SESSION_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)", (_hash_token(token), user_id, now, now + ttl))
        self.conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        self.conn.commit()
        return token

    def resolve_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        row = self.conn.execute(
            "SELECT s.user_id, s.expires_at, u.disabled FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_hash_token(token),)).fetchone()
        if row is None or row["expires_at"] < time.time() or row["disabled"]:
            return None
        return self.get_user(row["user_id"])

    def revoke_session(self, token: str) -> None:
        self.conn.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                          (_hash_token(token),))
        self.conn.commit()

    def revoke_user_sessions(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        self.conn.commit()
