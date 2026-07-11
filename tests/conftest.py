import pytest
from engraphis import licensing

@pytest.fixture(autouse=True)
def mock_licensing_files(tmp_path):
    # Re-route the licensing keys and trial JSON files to a temporary path
    # to avoid reading or writing to the host user's actual ~/.engraphis directory.
    licensing._LICENSE_FILE = tmp_path / "license.key"
    licensing._TRIAL_FILE = tmp_path / "trial.json"
    
    # Reset cached license state to prevent cross-test pollution
    licensing._cached = None
    licensing._cache_error = ""
    
    yield
    
    # Reset again after the test runs
    licensing._cached = None
    licensing._cache_error = ""
