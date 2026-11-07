import os
from pathlib import Path

import pytest

FIXTURE_KNOWLEDGE = Path(__file__).parent.parent / "fixtures" / "knowledge"


def pytest_configure(config):
    # the person's own knowledge files (~/.config/benchscout/knowledge) never change a result: the user layer is the
    # fixture benches' (fixtures/knowledge), for module-wide fixtures too
    os.environ["BENCHSCOUT_KNOWLEDGE"] = str(FIXTURE_KNOWLEDGE)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.config/benchscout/memory."""
    monkeypatch.setenv("BENCHSCOUT_MEMORY", str(tmp_path / "memory"))
    monkeypatch.setenv("BENCHSCOUT_KNOWLEDGE", str(FIXTURE_KNOWLEDGE))
