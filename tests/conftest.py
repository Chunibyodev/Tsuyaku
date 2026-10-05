import os
import tempfile

import pytest

# Keep tests away from the real user config/data directories.
os.environ.setdefault("TSUYAKU_HOME", tempfile.mkdtemp(prefix="tsuyaku-test-"))


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("TSUYAKU_HOME", str(tmp_path))
    return tmp_path
