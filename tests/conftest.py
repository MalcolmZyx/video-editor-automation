import pytest


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path_factory, monkeypatch):
    """Song-analysis cache goes to a temp folder, never the real user profile."""
    monkeypatch.setenv("YEARVIDEO_CACHE", str(tmp_path_factory.mktemp("cache")))
