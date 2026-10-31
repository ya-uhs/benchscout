import pytest


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.config/benchscout/memory."""
    monkeypatch.setenv("BENCHSCOUT_MEMORY", str(tmp_path / "memory"))
