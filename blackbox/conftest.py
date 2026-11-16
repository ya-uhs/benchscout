"""Each test gets its own World (harness.py): a fake bench, a workspace, memory and a core of its own."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from harness import World  # noqa: E402


@pytest.fixture
def world():
    w = World()
    yield w
    w.close()
