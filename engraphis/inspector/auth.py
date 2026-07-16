"""Team-mode auth for the Memory Inspector (Pro feature ``team``).

Design constraints, in order:

* **On by default (opt-out).** Only ``ENGRAPHIS_TEAM_MODE=0`` (or false/no/off) disables
  it. A ``team`` license is still required to *add seats* beyond the first admin (the
  bootstrap admin is created unconditionally); without one the module is constructed but
  reports upgrade-required (``team_locked``) rather than enabling auth — the single-user
  Inspector is untouched except for that signal.
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
import threading
import time
from functools import wraps
from typing import Optional

PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD_LEN = 10
SESSION_TTL_SECONDS = 12 * 3600
LOCKOUT_FAILS = 5           # failures within LOCKOUT_WINDOW …
LOCKOUT_WINDOW = 900        # … lock the account for LOCKOUT_SECONDS
LOCKOUT_SECONDS = 60
RESET_TOKEN_TTL_SECONDS = 1800   # a "forgot password" link is single-use, 30 min
RESET_REQUEST_MAX = 3            # … and throttled per-email so it can't mail-bomb
RESET_REQUEST_WINDOW = 3600      # an inbox (independent of the login lockout above)
MAX_THROTTLE_KEYS = 10_000       # bound unique-email memory use under credential spraying
MAX_SESSIONS_PER_USER = 20
MAX_ACTIVE_API_TOKENS = 100
MAX_REVOKED_API_TOKENS = 100

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
CREATE TABLE IF NOT EXISTS password_resets (
    token_hash TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reset_user ON password_resets(user_id);
-- Per-user API tokens (agent connect): a Team member mints a long-lived bearer token
-- from the dashboard and pastes it into their agent's config. Only the SHA-256 hash is
-- stored (like session tokens), so a leaked users DB contains no usable secrets.
CREATE TABLE IF NOT EXISTS api_tokens (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    label        TEXT NOT NULL DEFAULT '',
    token_hash   TEXT NOT NULL UNIQUE,
    created_at   REAL NOT NULL,
    last_used_at REAL,
    revoked      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_api_token_user ON api_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_api_token_hash ON api_tokens(token_hash);
CREATE TABLE IF NOT EXISTS audit_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    actor_id    TEXT,
    actor_email TEXT,
    action      TEXT NOT NULL,
    target      TEXT,
    detail      TEXT,
    ip          TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_events(ts);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_events(actor_id);
"""


class AuthError(ValueError):
    """User-facing auth failure; message is safe to surface."""


def _serialized(method):
    """Serialize access to AuthStore's shared SQLite connection and in-memory throttles."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapped


def role_at_least(role: str, minimum: str) -> bool:
    return _ROLE_RANK.get(role, -1) >= _ROLE_RANK.get(minimum, 99)


def min_role(method: str, path: str) -> str:
    """Least role allowed to touch ``path`` in team mode. Server-side is the source of
    truth — the UI merely hides what this table already refuses. Shared by every app
    that mounts team-mode auth (``inspector/app.py``, ``dashboard_app.py``) so the
    policy can't drift between them."""
    if path == "/api/sync/auto":
        # Team auto-sync is an account-wide control, so CHANGING it is admin-only ("admins
        # get more options"); anyone signed in may READ the current state (the dashboard
        # renders the toggle disabled for non-admins). GET stays viewer, writes are admin.
        return "admin" if method != "GET" else "viewer"
    if method == "POST" and path == "/api/auth/token":
        return "viewer"
    if path in ("/api/intent/recall", "/api/code/path", "/api/code/impact"):
        return "viewer"
    if path in (
        "/api/code/index", "/api/workspaces/import-folder", "/api/resources/postgres",
        "/api/sync/run",
    ):
        # These operations read server-local files or make a caller-selected outbound
        # connection, or mutate every shared workspace through the account-wide relay.
        # Only an administrator may choose those sources/actions.
        return "admin"
    if path.startswith("/api/auth/users") or path.startswith("/api/auth/audit") \
            or path == "/api/auth/overview" or path in (
            "/api/license/activate", "/api/license/trial", "/api/license/team-trial",
            "/api/export", "/api/consolidate"):
        return "admin"
    if method == "POST":            # pin / forget / correct — audited governance (member+)
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
    # Generated tokens are ASCII, but cookies/Authorization headers are untrusted input.
    # UTF-8 keeps malformed non-ASCII credentials on the normal "not found" path instead
    # of raising UnicodeEncodeError and turning an auth failure into a 500.
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def _validate_password(password: str) -> None:
    """Enforce a reasonable password policy — NIST SP 800-63B-aligned: length is
    the primary factor, with mixed character classes to resist dictionary attacks."""
    if not isinstance(password, str) or len(password) < MIN_PASSWORD_LEN:
        raise AuthError("password must be at least %d characters" % MIN_PASSWORD_LEN)
    if not any(c.isupper() for c in password) and not any(c.isdigit() for c in password) \
            and not any(not c.isalnum() for c in password):
        raise AuthError("password must include at least one uppercase letter, digit, "
                        "or special character")


class AuthStore:
    """Users + sessions for team mode. One instance per Inspector process."""

    def __init__(self, db_path: str, *, iterations: int = PBKDF2_ITERATIONS) -> None:
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        if db_path != ":memory:":
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower():
                    self.conn.close()
                    raise
            self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.iterations = int(iterations)
        self._failures: dict = {}   # email -> list[fail_ts] (in-memory throttle)
        self._reset_requests: dict = {}   # email -> list[req_ts] (forgot-password throttle)
        self._last_prune: float = 0.0
        self._last_throttle_prune: float = 0.0

    # ── users ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _clean_email(email) -> str:
        email = (email or "").strip().lower()
        if not _EMAIL_RE.match(email):
            raise AuthError("invalid email address")
        return email

    @_serialized
    def create_user(self, email: str, name: str, password: str, role: str,
                    *, seat_limit: Optional[int] = None) -> dict:
        # The very first user (bootstrap admin, called from /api/auth/setup on a
        # zero-user store) is exempt from the license gate. Every user after that
        # still requires an active Team license — this only closes the chicken-and-egg
        # deadlock where you can't get a license (paste a key = requires an admin
        # session; start a trial = requires the relay to be reachable and the trial
        # unclaimed) without already having an admin account, and you can't create
        # that account without a license. 2026-07-14 incident: the frontend was made to
        # auto-start a Team trial before setup, but that still depended on the relay
        # round-trip succeeding, so it was not a real fix — bootstrap must work
        # unconditionally. Adding seats beyond the first is untouched.
        email = self._clean_email(email)
        name = (name or "").strip()[:120]
        if role not in ROLES:
            raise AuthError("role must be one of: %s" % ", ".join(ROLES))
        _validate_password(password)
        # Hash outside the write transaction — PBKDF2 is CPU-bound and must not hold the
        # SQLite write lock that serializes the bootstrap gate below.
        uid = "usr_" + secrets.token_hex(8)
        pw_hash = _hash_password(password, iterations=self.iterations)
        created_at = time.time()
        # Atomic bootstrap gate: hold a write lock (BEGIN IMMEDIATE) from the zero-user
        # check through the INSERT, so two concurrent /api/auth/setup requests can't both
        # observe count==0 and both create an unlicensed admin. UNIQUE(email) only blocks
        # same-email doubles; this closes the different-email race. The license gate, the
        # seat-limit check, and the INSERT commit as one unit; any failure rolls back.
        conn = self.conn
        started = False
        try:
            if not conn.in_transaction:
                conn.execute("BEGIN IMMEDIATE")
                started = True
            if int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]) > 0:
                from engraphis.licensing import require_feature
                require_feature("team")
            if seat_limit is not None and int(conn.execute(
                    "SELECT COUNT(*) FROM users WHERE disabled=0").fetchone()[0]) >= seat_limit:
                raise AuthError(
                    "seat limit reached (%d) — upgrade your Team license for more seats"
                    % seat_limit)
            conn.execute(
                "INSERT INTO users (id, email, name, role, pw_hash, created_at) "
                "VALUES (?,?,?,?,?,?)",
                (uid, email, name, role, pw_hash, created_at))
            if started:
                conn.commit()
        except sqlite3.IntegrityError:
            if started:
                conn.rollback()
            raise AuthError("a user with that email already exists")
        except Exception:
            if started:
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            raise
        return self.get_user(uid)

    @_serialized
    def get_user(self, user_id: str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT id, email, name, role, created_at, disabled FROM users WHERE id=?",
            (user_id,)).fetchone()
        return dict(row) if row else None

    @_serialized
    def list_users(self) -> list:
        return [dict(r) for r in self.conn.execute(
            "SELECT id, email, name, role, created_at, disabled FROM users "
            "ORDER BY created_at")]

    @_serialized
    def count_users(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"])

    @_serialized
    def count_active_users(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE disabled=0").fetchone()["n"])

    @_serialized
    def _count_active_admins(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE role='admin' AND disabled=0"
        ).fetchone()["n"])

    @_serialized
    def update_user(self, user_id: str, *, role: Optional[str] = None,
                    disabled: Optional[bool] = None,
                    seat_limit: Optional[int] = None) -> dict:
        preliminary = self.get_user(user_id)
        if preliminary is None:
            raise AuthError("no such user")
        if disabled is False and bool(preliminary["disabled"]):
            from engraphis.licensing import require_feature
            require_feature("team")
        if role is not None:
            if role not in ROLES:
                raise AuthError("role must be one of: %s" % ", ".join(ROLES))
        conn = self.conn
        started = not conn.in_transaction
        try:
            if started:
                conn.execute("BEGIN IMMEDIATE")
            user = self.get_user(user_id)
            if user is None:
                raise AuthError("no such user")
            # Never let concurrent demotions/disables remove every active admin.
            losing_admin = (
                user["role"] == "admin" and not user["disabled"]
                and ((role is not None and role != "admin") or disabled)
            )
            if losing_admin and self._count_active_admins() <= 1:
                raise AuthError("cannot demote or disable the last active admin")
            # Re-enabling consumes a seat. Keep the count check and update in one write
            # transaction so concurrent admin requests cannot oversubscribe the license.
            reenabling = disabled is False and bool(user["disabled"])
            if reenabling and seat_limit is not None \
                    and self.count_active_users() >= seat_limit:
                raise AuthError(
                    "seat limit reached (%d) — upgrade your Team license for more seats"
                    % seat_limit)
            if role is not None:
                conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
            if disabled is not None:
                conn.execute(
                    "UPDATE users SET disabled=? WHERE id=?",
                    (1 if disabled else 0, user_id),
                )
                if disabled:
                    conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
                    conn.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (user_id,))
                    self._prune_revoked_api_tokens(user_id)
            if started:
                conn.commit()
        except BaseException:
            if started and conn.in_transaction:
                conn.rollback()
            raise
        return self.get_user(user_id)

    @_serialized
    def delete_user(self, user_id: str) -> dict:
        """Permanently remove a team member. Unlike ``disable`` (which flips a flag but
        leaves the row — and its UNIQUE ``email`` — in place), this frees both the
        license seat (``count_active_users()`` recomputes live from the table) *and*
        the email address, so an admin can re-invite the same address after a
        typo'd/bounced invite without a DB edit. Hard delete is intentional: this
        codebase has no soft-delete convention, and ``audit_events`` (recorded by the
        caller with the email captured beforehand) already preserves the history.
        Returns the deleted user's row (pre-delete snapshot) for the caller's audit log."""
        conn = self.conn
        started = not conn.in_transaction
        try:
            if started:
                conn.execute("BEGIN IMMEDIATE")
            user = self.get_user(user_id)
            if user is None:
                raise AuthError("no such user")
            # Same guard as update_user's demote/disable path — deletion is a strictly
            # stronger version of disable, so it must never lock everyone out.
            if user["role"] == "admin" and not user["disabled"] \
                    and self._count_active_admins() <= 1:
                raise AuthError("cannot delete the last active admin")
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM password_resets WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM api_tokens WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            if started:
                conn.commit()
            return user
        except BaseException:
            if started and conn.in_transaction:
                conn.rollback()
            raise

    # ── login throttle (in-memory, per-process) ────────────────────────────────
    def _prune_throttle_maps(self, now: float) -> None:
        periodic = now - self._last_throttle_prune >= 60
        for bucket, window in (
            (self._failures, LOCKOUT_WINDOW),
            (self._reset_requests, RESET_REQUEST_WINDOW),
        ):
            if periodic:
                for email, stamps in list(bucket.items()):
                    live = [stamp for stamp in stamps if now - stamp < window]
                    if live:
                        bucket[email] = live
                    else:
                        bucket.pop(email, None)
            # Dict insertion order is used as a small LRU: callers pop/reinsert a
            # touched key. This keeps a credential-spraying request O(1) once the
            # map reaches its cap instead of sorting ten thousand entries per hit.
            while len(bucket) > MAX_THROTTLE_KEYS:
                bucket.pop(next(iter(bucket)))
        if periodic:
            self._last_throttle_prune = now

    @_serialized
    def _locked_until(self, email: str) -> float:
        now = time.time()
        self._prune_throttle_maps(now)
        fails = [t for t in self._failures.get(email, []) if now - t < LOCKOUT_WINDOW]
        self._failures.pop(email, None)
        if fails:
            self._failures[email] = fails
        if len(fails) >= LOCKOUT_FAILS:
            return fails[-1] + LOCKOUT_SECONDS
        return 0.0

    @_serialized
    def login(self, email: str, password: str, *, ip: Optional[str] = None) -> dict:
        """Verify credentials → new session. Raises :class:`AuthError` (generic message —
        never reveals which of email/password was wrong) or the lockout notice.

        Deliberately does NOT gate on a live Team license (see ``licensing.require_feature
        ("team")``, still enforced by :meth:`create_user`). It used to — 2026-07-12 incident:
        an expired/lapsed license blocked EVERY login, including the admin's own, with no
        recovery path short of hand-minting a new key. Authentication and paid-feature
        entitlement are different concerns: a lapsed license must degrade *what a signed-in
        user can do* (analytics/export/automation/sync/seat growth all still gate on
        ``require_feature`` at their own routes, and ``create_user`` still blocks adding
        seats without a live license), never *whether an already-provisioned account can
        sign back in*. The auth session wall itself (``_auth_gate`` in dashboard_app.py /
        inspector/app.py) is unaffected by this, so unauthenticated requests are still
        refused — this only stops a license hiccup from locking out people who already have
        an account. Existing sessions still cap out at ``SESSION_TTL_SECONDS``."""
        email = self._clean_email(email)
        until = self._locked_until(email)
        if until > time.time():
            self.record_event("login.locked", actor_email=email, ip=ip)
            raise AuthError("too many failed attempts — try again in a minute")
        row = self.conn.execute(
            "SELECT id, pw_hash, disabled FROM users WHERE email=?", (email,)).fetchone()
        # Always run one PBKDF2 even for unknown emails (no user-enumeration timing).
        encoded = row["pw_hash"] if row else _hash_password("x", iterations=self.iterations)
        ok = _verify_password(password or "", encoded)
        if not ok or row is None or row["disabled"]:
            fails = self._failures.pop(email, [])
            fails.append(time.time())
            self._failures[email] = fails
            self._prune_throttle_maps(time.time())
            self.record_event("login.failed", actor_email=email, ip=ip,
                              detail=("account_disabled" if row and row["disabled"]
                                      else "bad_credentials"))
            raise AuthError("invalid email or password")
        self._failures.pop(email, None)
        token = self.create_session(row["id"])
        user = self.get_user(row["id"])
        self.record_event("login.success", actor_id=row["id"], actor_email=email, ip=ip)
        user["token"] = token
        return user

    # ── password reset ("forgot password") ─────────────────────────────────────
    @_serialized
    def request_password_reset(self, email: str) -> Optional[dict]:
        """Issue a single-use password-reset token for *email*, or return ``None``.

        Returns ``None`` both when the address doesn't match an (enabled) account
        AND when the per-email throttle is exceeded — callers MUST treat every
        ``None`` identically (always respond as if the email might have been sent)
        so a client can't enumerate registered users, or fingerprint the throttle,
        by watching for a different HTTP response. Raises :class:`AuthError` only
        for a malformed email string (also caller-swallowable for the same reason).

        Any previously issued, still-unused token for this user is invalidated —
        only the most recent reset link works, so an old email lying around in an
        inbox can't be replayed after a newer one was requested.
        """
        email = self._clean_email(email)
        now = time.time()
        self._prune_throttle_maps(now)
        hits = [t for t in self._reset_requests.get(email, []) if now - t < RESET_REQUEST_WINDOW]
        throttled = len(hits) >= RESET_REQUEST_MAX
        hits.append(now)
        self._reset_requests.pop(email, None)
        self._reset_requests[email] = hits
        self._prune_throttle_maps(now)
        if throttled:
            self.record_event("password_reset.throttled", actor_email=email)
            return None
        row = self.conn.execute(
            "SELECT id, email, name, disabled FROM users WHERE email=?", (email,)).fetchone()
        if row is None or row["disabled"]:
            return None
        self.conn.execute("DELETE FROM password_resets WHERE user_id=? AND used=0", (row["id"],))
        token = secrets.token_urlsafe(32)
        self.conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, used) "
            "VALUES (?,?,?,?,0)",
            (_hash_token(token), row["id"], now, now + RESET_TOKEN_TTL_SECONDS))
        self.conn.commit()
        self.record_event("password_reset.requested", actor_id=row["id"], actor_email=email)
        return {"token": token, "email": row["email"], "name": row["name"]}

    @_serialized
    def reset_password(self, token: str, new_password: str) -> dict:
        """Consume a password-reset token and set *new_password*.

        Raises :class:`AuthError` on an invalid, expired, or already-used token, or
        when the password fails :func:`_validate_password`. On success: every
        existing session for the user is revoked (a token that leaked alongside a
        stolen session, or a session left open on a shared machine, must not
        survive the reset), the login lockout is cleared, and a fresh session is
        created — same shape as :meth:`login`'s return (``user["token"]``) — so the
        caller can sign the user straight back in.
        """
        _validate_password(new_password)
        password_hash = _hash_password(new_password, iterations=self.iterations)
        reset_hash = _hash_token(token)
        new_token = secrets.token_urlsafe(32)
        new_token_hash = _hash_token(new_token)
        now = time.time()
        conn = self.conn
        started = not conn.in_transaction
        try:
            if started:
                conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT user_id, expires_at, used FROM password_resets WHERE token_hash=?",
                (reset_hash,),
            ).fetchone()
            if row is None or row["used"] or row["expires_at"] < now:
                raise AuthError("invalid or expired reset link")
            user = self.get_user(row["user_id"])
            if user is None or user["disabled"]:
                raise AuthError("invalid or expired reset link")
            conn.execute(
                "UPDATE users SET pw_hash=? WHERE id=?",
                (password_hash, user["id"]),
            )
            consumed = conn.execute(
                "UPDATE password_resets SET used=1 "
                "WHERE token_hash=? AND used=0 AND expires_at>=?",
                (reset_hash, now),
            )
            if consumed.rowcount != 1:
                raise AuthError("invalid or expired reset link")
            conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user["id"],))
            conn.execute("UPDATE api_tokens SET revoked=1 WHERE user_id=?", (user["id"],))
            self._prune_revoked_api_tokens(user["id"])
            conn.execute(
                "INSERT INTO auth_sessions "
                "(token_hash, user_id, created_at, expires_at) VALUES (?,?,?,?)",
                (new_token_hash, user["id"], now, now + SESSION_TTL_SECONDS),
            )
            if started:
                conn.commit()
        except BaseException:
            if started and conn.in_transaction:
                conn.rollback()
            raise
        self._failures.pop(user["email"], None)
        self.record_event("password_reset.completed", actor_id=user["id"],
                          actor_email=user["email"])
        result = self.get_user(user["id"])
        result["token"] = new_token
        return result

    # ── sessions (raw token to the client, hash in the DB) ─────────────────────
    @_serialized
    def create_session(self, user_id: str, *, ttl: int = SESSION_TTL_SECONDS) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.conn.execute(
            "INSERT INTO auth_sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?,?,?,?)", (_hash_token(token), user_id, now, now + ttl))
        self.conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
        self.conn.execute(
            "DELETE FROM auth_sessions WHERE token_hash IN ("
            "SELECT token_hash FROM auth_sessions WHERE user_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (user_id, MAX_SESSIONS_PER_USER),
        )
        self.conn.commit()
        return token

    @_serialized
    def resolve_session(self, token: str) -> Optional[dict]:
        if not token:
            return None
        # Prune expired sessions (rate-limited: once per minute — create_session also
        # prunes on new logins, so this covers long-running sessions without new logins).
        now = time.time()
        if now - self._last_prune > 60:
            self.conn.execute("DELETE FROM auth_sessions WHERE expires_at < ?", (now,))
            self.conn.commit()
            self._last_prune = now
        row = self.conn.execute(
            "SELECT s.user_id, s.expires_at, u.disabled FROM auth_sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_hash_token(token),)).fetchone()
        if row is None or row["expires_at"] < time.time() or row["disabled"]:
            return None
        return self.get_user(row["user_id"])

    @_serialized
    def revoke_session(self, token: str) -> None:
        self.conn.execute("DELETE FROM auth_sessions WHERE token_hash=?",
                          (_hash_token(token),))
        self.conn.commit()

    @_serialized
    def revoke_user_sessions(self, user_id: str) -> None:
        self.conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        self.conn.commit()

    # ── per-user API tokens (agent connect) ─────────────────────────────────────
    @_serialized
    def create_api_token(self, user_id: str, *, label: str = "") -> dict:
        """Mint a long-lived per-user bearer token for an agent/automation client.

        The raw token is returned ONCE; only its SHA-256 hash is persisted (see
        :data:`api_tokens`), so a stolen users DB yields no usable secrets. Bound to
        ``user_id``; a disabled user's tokens are refused by :meth:`resolve_api_token`.
        """
        tok = secrets.token_urlsafe(32)
        tid = "tok_" + secrets.token_hex(8)
        now = time.time()
        label = (label or "")[:120]
        active = int(self.conn.execute(
            "SELECT COUNT(*) FROM api_tokens WHERE user_id=? AND revoked=0",
            (user_id,),
        ).fetchone()[0])
        if active >= MAX_ACTIVE_API_TOKENS:
            raise AuthError(
                "active API token limit reached (%d); revoke an old token first"
                % MAX_ACTIVE_API_TOKENS
            )
        self.conn.execute(
            "INSERT INTO api_tokens (id, user_id, label, token_hash, created_at) "
            "VALUES (?,?,?,?,?)", (tid, user_id, label, _hash_token(tok), now))
        self.conn.commit()
        return {"id": tid, "label": label, "created_at": now,
                "last_used_at": None, "revoked": 0, "token": tok}

    @_serialized
    def resolve_api_token(self, token: str) -> Optional[dict]:
        """Resolve a bearer API token to its user, or ``None``. Rejects revoked tokens
        and tokens whose owner is disabled. Best-effort stamps ``last_used_at``."""
        if not token:
            return None
        row = self.conn.execute(
            "SELECT t.id, t.user_id, t.revoked, u.disabled FROM api_tokens t "
            "JOIN users u ON u.id = t.user_id WHERE t.token_hash=?",
            (_hash_token(token),)).fetchone()
        if row is None or row["revoked"] or row["disabled"]:
            return None
        try:
            self.conn.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?",
                              (time.time(), row["id"]))
            self.conn.commit()
        except sqlite3.Error:
            pass
        return self.get_user(row["user_id"])

    @_serialized
    def list_api_tokens(self, user_id: str) -> list:
        rows = self.conn.execute(
            "SELECT id, label, created_at, last_used_at, revoked FROM api_tokens "
            "WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall()
        return [dict(r) for r in rows]

    def _prune_revoked_api_tokens(self, user_id: str) -> None:
        self.conn.execute(
            "DELETE FROM api_tokens WHERE id IN ("
            "SELECT id FROM api_tokens WHERE user_id=? AND revoked=1 "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?)",
            (user_id, MAX_REVOKED_API_TOKENS),
        )

    @_serialized
    def revoke_api_token(self, user_id: str, token_id: str) -> bool:
        """Revoke one of *user_id*'s own tokens (scoped to the caller so a member can't
        revoke another member's). Returns True if a row was affected."""
        cur = self.conn.execute(
            "UPDATE api_tokens SET revoked=1 WHERE id=? AND user_id=? AND revoked=0",
            (token_id, user_id))
        self._prune_revoked_api_tokens(user_id)
        self.conn.commit()
        return cur.rowcount > 0

    # ── team audit log ─────────────────────────────────────────────────────────
    @_serialized
    def record_event(self, action: str, *, actor_id: Optional[str] = None,
                     actor_email: Optional[str] = None, target: Optional[str] = None,
                     detail: Optional[str] = None, ip: Optional[str] = None) -> None:
        """Append one team audit event (login, user CRUD, role change, ...). Best-effort:
        auditing must never break the action it records, so storage errors are swallowed —
        the event is lost, not the request. Admin-only to read (see routes.v2_team)."""
        try:
            self.conn.execute(
                "INSERT INTO audit_events (ts, actor_id, actor_email, action, target, detail, ip) "
                "VALUES (?,?,?,?,?,?,?)",
                (time.time(), str(actor_id)[:200] if actor_id else None,
                 str(actor_email)[:320] if actor_email else None, str(action)[:64],
                 str(target)[:320] if target else None,
                 str(detail)[:1000] if detail else None,
                 str(ip)[:128] if ip else None))
            self.conn.commit()
        except sqlite3.Error:
            pass

    @_serialized
    def list_events(self, *, limit: int = 100, action: Optional[str] = None,
                    actor_id: Optional[str] = None, since: Optional[float] = None) -> list:
        """Most-recent-first audit events, with optional filters. ``limit`` is clamped to
        [1, 1000] so a client can't ask for an unbounded scan."""
        limit = max(1, min(int(limit or 100), 1000))
        sql = ("SELECT id, ts, actor_id, actor_email, action, target, detail, ip "
               "FROM audit_events")
        clauses, params = [], []
        if action:
            clauses.append("action=?")
            params.append(str(action)[:64])
        if actor_id:
            clauses.append("actor_id=?")
            params.append(actor_id)
        if since is not None:
            clauses.append("ts>=?")
            params.append(float(since))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self.conn.execute(sql, params)]

    @_serialized
    def count_events(self) -> int:
        return int(self.conn.execute(
            "SELECT COUNT(*) AS n FROM audit_events").fetchone()["n"])

    @_serialized
    def action_counts(self, *, since: Optional[float] = None) -> dict:
        sql = "SELECT action, COUNT(*) AS n FROM audit_events"
        params: list = []
        if since is not None:
            sql += " WHERE ts>=?"
            params.append(float(since))
        sql += " GROUP BY action ORDER BY n DESC"
        return {r["action"]: r["n"] for r in self.conn.execute(sql, params)}

    @_serialized
    def last_active(self) -> dict:
        """Map user_id -> last successful-login timestamp (for the admin seat overview)."""
        rows = self.conn.execute(
            "SELECT actor_id, MAX(ts) AS ts FROM audit_events "
            "WHERE action='login.success' AND actor_id IS NOT NULL GROUP BY actor_id")
        return {r["actor_id"]: r["ts"] for r in rows}
