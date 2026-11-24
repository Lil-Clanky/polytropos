import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for sub in ("shared", "plugins", "scripts", "tests"):
    sys.path.insert(0, str(ROOT / sub))

from fake_openai import FakeOpenAI  # noqa: E402
from sync_shared import sync  # noqa: E402


def pytest_sessionstart(session):
    sync(ROOT)


@pytest.fixture
def fake_openai():
    with FakeOpenAI() as fake:
        yield fake


@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Path.home() on Windows
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))  # Windows default of Hermes and Polytropos
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / ".local" / "state"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / ".cache"))
    return tmp_path
