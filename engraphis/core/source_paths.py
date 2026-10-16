"""Lexical exclusions shared by local and uploaded document sources."""
from __future__ import annotations

import re


SENSITIVE_FILENAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_dsa",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "pgpass.conf",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "tokens", "tokens.json",
    "secrets", "secrets.json", "secret", "secret.json",
}


def sensitive_filename(name: str) -> bool:
    """Preserve the document-source credential filename policy."""
    lowered = name.casefold()
    return (lowered in SENSITIVE_FILENAMES or lowered.startswith(".env.")
            or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
            or bool(re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", lowered)))


def sensitive_path(path: str) -> bool:
    """Check every selected component without resolving or touching the filesystem.

    Both separators are interpreted lexically, including for uploaded Windows paths.
    Generic hidden directories are left to each scanner's existing relative policy;
    an allowed root under a benign hidden worktree remains usable.
    """
    return any(sensitive_filename(part) for part in path.replace("\\", "/").split("/") if part)
