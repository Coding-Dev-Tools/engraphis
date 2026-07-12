import pytest
from engraphis import licensing

# Opt the licensing module into honoring ENGRAPHIS_LICENSE_PUBKEY, which is otherwise
# dead in a shipped process. Set at import time so it covers both collection and
# execution. This is the ONLY place that flips the switch — production never imports
# this conftest, so the vendor-key override stays non-overridable in the field.
licensing._TEST_MODE_PUBKEY_OVERRIDE = True


@pytest.fixture(autouse=True)
def mock_licensing_files(tmp_path):
    # Re-route the licensing keys and trial JSON files to a temporary path
    # to avoid reading or writing to the host user's actual ~/.engraphis directory.
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing._TRIAL_FILE = tmp_path / "trial.json"
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
