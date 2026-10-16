"""Lexical exclusions shared by local and uploaded document sources."""
from __future__ import annotations

import re


SENSITIVE_FILENAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_dsa",
    ".netrc", "_netrc", ".npmrc", ".pypirc", ".git-credentials", ".pgpass", "pgpass.conf",
    ".aws", ".ssh", ".gnupg", ".azure", ".kube", ".docker", ".engraphis",
    "id_ecdsa", "id_ed25519", "authorized_keys", "known_hosts",
    "recovery-codes", "recovery_codes", "tokens", "tokens.json",
    "secrets", "secrets.json", "secret", "secret.json",
}
SENSITIVE_DIRECTORIES = SENSITIVE_FILENAMES | {
    "credential", "token", ".credential", ".credentials", ".secrets", ".secret", ".token", ".tokens",
}


def sensitive_directory(name: str) -> bool:
    """Exclude known credential components without broad word-substring matching."""
    lowered = name.casefold()
    return (lowered in SENSITIVE_DIRECTORIES or lowered.startswith(".env.")
            or lowered.endswith((".pem", ".key", ".p12", ".pfx"))
            or bool(re.fullmatch(r"\.?recovery[-_ ]?codes?", lowered)))


def sensitive_filename(name: str) -> bool:
    """Keep the broader credential heuristic for actual file basenames."""
    return sensitive_directory(name) or bool(
        re.search(r"(?:credential|recovery[-_ ]?code|secret|token)", name.casefold())
    )


def sensitive_path(path: str, *, is_directory: bool = False) -> bool:
    """Check selected components lexically, treating a known directory root explicitly.

    Both separators are interpreted lexically, including for uploaded Windows paths.
    Generic hidden directories are left to each scanner's existing relative policy;
    an allowed root under a benign hidden worktree remains usable.
    """
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    directories = parts if is_directory else parts[:-1]
    return any(sensitive_directory(part) for part in directories) or (
        bool(parts) and not is_directory and sensitive_filename(parts[-1])
    )
