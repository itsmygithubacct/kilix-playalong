from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def private_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    state = tmp_path / "state"
    monkeypatch.setenv("KILIX_PLAYALONG_DATA_HOME", str(data))
    monkeypatch.setenv("KILIX_PLAYALONG_CACHE_HOME", str(cache))
    monkeypatch.setenv("KILIX_PLAYALONG_STATE_HOME", str(state))
    return data
