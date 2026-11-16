import os
from pathlib import Path

import pytest
from hypothesis import settings

# property tests explore the same examples on every run: a result never depends on luck, and a failure reproduces as it
# came (deterministic checks where possible: the person's preference, 2026-11-16)
settings.register_profile("deterministic", derandomize=True)
settings.load_profile("deterministic")

FIXTURE_KNOWLEDGE = Path(__file__).parent.parent / "fixtures" / "knowledge"


def pytest_configure(config):
    # the person's own knowledge files (~/.config/benchscout/knowledge) never change a result: the user layer is the
    # fixture benches' (fixtures/knowledge), for module-wide fixtures too
    os.environ["BENCHSCOUT_KNOWLEDGE"] = str(FIXTURE_KNOWLEDGE)


@pytest.fixture(autouse=True)
def isolated_memory(tmp_path, monkeypatch):
    """Tests never read or write the real ~/.config/benchscout/memory, and each starts from the same knowledge.

    The loaded knowledge is process-wide: a test that loaded other layers (a folder's, an empty user layer) left them
    for whichever test ran next, so a result depended on the order (audit 2026-11-07, T22)."""
    from benchscout import knowledge as K

    monkeypatch.setenv("BENCHSCOUT_MEMORY", str(tmp_path / "memory"))
    monkeypatch.setenv("BENCHSCOUT_KNOWLEDGE", str(FIXTURE_KNOWLEDGE))
    K.load()
