"""Fail CI when the published Compose contract drifts from its invariants.

The default ``docker-compose.yml`` is the zero-token local quickstart and the only
Compose file a fresh clone boots. ``docker-compose.lan.yml`` is the sole LAN overlay
and *must* require ``ENGRAPHIS_API_TOKEN`` before publishing on any non-loopback
interface. A silent edit to either file -- a renamed service, a port mapping that no
longer binds 127.0.0.1, a dropped ``/data`` volume -- would otherwise ship as a real
change to every customer who runs ``docker compose up``. This validator encodes the
invariants the rest of the release surface (evidence, docs, Railway template) assumes.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = ROOT / "docker-compose.yml"
LAN_COMPOSE_PATH = ROOT / "docker-compose.lan.yml"


class ComposeContractError(ValueError):
    """A Compose contract invariant is violated."""


def _read(path: Path) -> str:
    if not path.is_file():
        raise ComposeContractError("Compose file is missing: %s" % path.name)
    return path.read_text(encoding="utf-8")


def _require(text: str, needle: str, message: str) -> None:
    if needle not in text:
        raise ComposeContractError(message)


def _reject(text: str, needle: str, message: str) -> None:
    if needle in text:
        raise ComposeContractError(message)


def validate_default_compose(text: str) -> None:
    """The default Compose file is the zero-token local quickstart."""
    # Single service, named ``engraphis``, built from the repository root. No
    # separate ``engraphis-api`` v1 shadow may reappear here.
    _require(text, "services:", "Compose file must declare a services block")
    _require(text, "\n  engraphis:\n", "Compose file must expose the 'engraphis' service")
    _reject(text, "engraphis-api:", "v1 'engraphis-api' service must not ship in the v2 Compose file")
    _require(text, "build: .", "Compose service must build from the repository root")
    _require(text, 'command: ["engraphis-dashboard", "--no-open"]',
             "Compose service must launch the v2 dashboard in headless mode")

    # Loopback-only port mapping. The LAN overlay is the *only* path that may
    # publish on 0.0.0.0, and it does so via the !override tag below.
    _require(
        text,
        '"127.0.0.1:${ENGRAPHIS_COMPOSE_PORT:-8700}:${ENGRAPHIS_COMPOSE_PORT:-8700}"',
        "Default Compose port mapping must bind 127.0.0.1 via ENGRAPHIS_COMPOSE_PORT",
    )
    _reject(
        text,
        '"0.0.0.0:',
        "Default Compose file must not publish on 0.0.0.0 (use docker-compose.lan.yml)",
    )

    # Container-bound environment: generic desktop .env values must not leak in.
    _require(text, "ENGRAPHIS_HOST: 0.0.0.0",
             "Compose service must bind 0.0.0.0 inside the container")
    _reject(text, "ENGRAPHIS_COMPOSE_HOST",
            "Compose file must not reference the removed ENGRAPHIS_COMPOSE_HOST variable")
    _require(text, "PORT: ${ENGRAPHIS_COMPOSE_PORT:-8700}",
             "Compose service must pin PORT via ENGRAPHIS_COMPOSE_PORT")
    _require(text, "ENGRAPHIS_PORT: ${ENGRAPHIS_COMPOSE_PORT:-8700}",
             "Compose service must pin ENGRAPHIS_PORT via ENGRAPHIS_COMPOSE_PORT")

    # Persistence. The customer database and the customer-side cloud session live
    # below /data on a named volume; neither may move to a bind mount or /tmp.
    _require(text, "ENGRAPHIS_DB_PATH: /data/engraphis.db",
             "Compose service must persist the database at /data/engraphis.db")
    _require(text, "ENGRAPHIS_STATE_DIR: /data/.engraphis",
             "Compose service must persist the customer-side state at /data/.engraphis")
    _require(text, "engraphis-data:/data",
             "Compose service must mount the engraphis-data named volume at /data")
    _require(text, "\nvolumes:\n  engraphis-data:\n",
             "Compose file must declare the engraphis-data named volume")

    # Optional .env: a fresh clone has no .env (it is gitignored) and `docker
    # compose up` must still boot. The object form with required: false is what
    # makes that true on Compose v2.24.4+.
    _require(text, "path: .env",
             "Compose env_file must reference .env")
    _require(text, "required: false",
             "Compose env_file must be optional (required: false)")

    # The default quickstart never sets an API token; that is the LAN overlay's job.
    # Only reject actual env-var assignments (``ENGRAPHIS_API_TOKEN:`` as a YAML key),
    # not prose/comment references that explain the LAN overlay's contract.
    for line in text.splitlines():
        stripped = line.lstrip()
        if stripped.startswith("#"):
            continue
        if "ENGRAPHIS_API_TOKEN:" in line or "ENGRAPHIS_API_TOKEN=" in line:
            raise ComposeContractError(
                "Default Compose file must not set ENGRAPHIS_API_TOKEN"
            )

    # Restart policy: the dashboard is a long-running service, not a batch job.
    _require(text, "restart: unless-stopped",
             "Compose service must restart unless explicitly stopped")


def validate_lan_overlay(text: str) -> None:
    """The LAN overlay must require a token and replace (not append to) the port mapping."""
    # The !override tag is what makes this a replacement rather than an append.
    # Without it, the LAN overlay would publish *both* the loopback and the
    # 0.0.0.0 mapping, and the zero-token default would remain reachable.
    _require(text, "ports: !override",
             "LAN overlay must use the !override tag to replace the port mapping")
    _require(
        text,
        '"0.0.0.0:${ENGRAPHIS_COMPOSE_PORT:-8700}:${ENGRAPHIS_COMPOSE_PORT:-8700}"',
        "LAN overlay must publish on 0.0.0.0 via ENGRAPHIS_COMPOSE_PORT",
    )
    # The :? parameter expansion is what makes Compose fail *before* the
    # container starts when the operator forgot to set a token. A plain
    # ${ENGRAPHIS_API_TOKEN} would silently start an unauthenticated LAN service.
    _require(
        text,
        "ENGRAPHIS_API_TOKEN: ${ENGRAPHIS_API_TOKEN:?Set a strong ENGRAPHIS_API_TOKEN for LAN use}",
        "LAN overlay must require ENGRAPHIS_API_TOKEN via the :? parameter expansion",
    )
    # A LAN overlay that also declared a default token would bake a secret into
    # a file that ends up in source control and every mirrored copy of it.
    _reject(text, "ENGRAPHIS_API_TOKEN:-",
            "LAN overlay must not supply a default ENGRAPHIS_API_TOKEN value")


def validate_contract(
    compose_path: Path = COMPOSE_PATH,
    lan_path: Path = LAN_COMPOSE_PATH,
) -> None:
    """Run every Compose-contract invariant; raise on the first failure."""
    validate_default_compose(_read(compose_path))
    validate_lan_overlay(_read(lan_path))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compose", type=Path, default=COMPOSE_PATH)
    parser.add_argument("--lan", type=Path, default=LAN_COMPOSE_PATH)
    args = parser.parse_args(argv)
    try:
        validate_contract(args.compose, args.lan)
    except ComposeContractError as exc:
        print("compose contract: %s" % exc, file=sys.stderr)
        return 1
    print("compose contract: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
