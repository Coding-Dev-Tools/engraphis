import pytest
from engraphis import cloud_license, licensing

# Opt the licensing module into honoring ENGRAPHIS_LICENSE_PUBKEY, which is otherwise
# dead in a shipped process. Set at import time so it covers both collection and
# execution. This is the ONLY place that flips the switch — production never imports
# this conftest, so the vendor-key override stays non-overridable in the field.
licensing._TEST_MODE_PUBKEY_OVERRIDE = True


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_license_gate: exercise the real server-side license gate — skip the default "
        "suite-wide 'approve' stub that unrelated tests rely on to unlock paid features "
        "without a live vendor server.")


@pytest.fixture(autouse=True)
def mock_licensing_files(tmp_path):
    # Re-route the licensing keys and trial JSON files to a temporary path
    # to avoid reading or writing to the host user's actual ~/.engraphis directory.
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing._TRIAL_FILE = tmp_path / "trial.json"
    licensing._TRIAL_STAMP = tmp_path / "trial_used.json"  # advisory "trial used" UI stamp
    # Trial-used tombstones must never touch (or read!) the host's real home/appdata —
    # a developer machine that once used a trial would otherwise fail every trial test.
    licensing._TOMBSTONE_DIRS_OVERRIDE = [tmp_path]
    # And the clock anchor: tests that warp time must not poison the host's real
    # high-water mark (a future-dated anchor would eat real trial/lease time).
    licensing._MONOTONIC_FILE = tmp_path / ".clock_anchor"

    # Reset cached license state to prevent cross-test pollution
    licensing._cached = None
    licensing._cache_error = ""
    licensing._cache_recheck_at = float("inf")

    yield

    # Reset again after the test runs
    licensing._cached = None
    licensing._cache_error = ""
    licensing._cache_recheck_at = float("inf")
    licensing._TOMBSTONE_DIRS_OVERRIDE = None


@pytest.fixture(autouse=True)
def _online_only_isolation(request, monkeypatch, tmp_path):
    """Online-only license enforcement (CHANGELOG 0.8.4) makes every paid key require a
    live, machine-bound vendor lease. Two consequences for the test suite, handled here:

      * Hermetic — no test may reach the production relay. Client-side cloud state (lease
        token, machine id) is rerouted into tmp for every test.
      * Convenience — the vast majority of tests only need "a valid key unlocks features"
        as a precondition; they are not about the license gate. For those, stub the gate
        to approve, so a signature-valid paid key works without standing up a fake server
        (no network, deterministic).

    Tests that ARE about the gate / lease / revocation opt out with
    ``pytestmark = pytest.mark.real_license_gate`` and then drive ``cloud_license.register``
    / ``ENGRAPHIS_CLOUD_URL`` themselves.
    """
    monkeypatch.setattr(cloud_license, "_DIR", tmp_path, raising=False)
    monkeypatch.setattr(cloud_license, "_LEASE_FILE", tmp_path / "lease.sig", raising=False)
    monkeypatch.setattr(cloud_license, "_MACHINE_ID_FILE", tmp_path / "machine_id",
                        raising=False)
    cloud_license._machine_id_cache.clear()
    if request.node.get_closest_marker("real_license_gate"):
        return  # this module exercises the real gate; leave cloud_license.gate intact
    monkeypatch.setattr(cloud_license, "gate",
                        lambda lic, material, base_url=None: (True, ""))
