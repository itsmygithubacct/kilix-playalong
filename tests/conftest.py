from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def private_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    cache = tmp_path / "cache"
    monkeypatch.setenv("KILIX_PLAYALONG_DATA_HOME", str(data))
    monkeypatch.setenv("KILIX_PLAYALONG_CACHE_HOME", str(cache))
    return data


class FakeLicensedStore:
    """A Content root that answers like ``kilix models list`` and ``verify``.

    Autouse, so no test reaches the real Kilix on the machine running the suite.
    Every licensed model starts installed; a test removes one with ``remove``, or
    makes verification refuse with ``refuse``.
    """

    def __init__(self, root: Path) -> None:
        from kilix_playalong import licensed

        self.root = root
        self.calls: list[tuple[str, ...]] = []
        self.refused: set[str] = set()
        self.absent_kilix = False
        for model in licensed.ALL:
            self.add(model)

    def add(self, model: object) -> Path:
        marker = self.root / "assets" / model.asset_id / model.marker  # type: ignore[attr-defined]
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"licensed bytes")
        return marker

    def remove(self, model: object) -> None:
        import shutil

        shutil.rmtree(self.root / "assets" / model.asset_id, ignore_errors=True)  # type: ignore[attr-defined]

    def refuse(self, model: object) -> None:
        self.refused.add(model.asset_id)  # type: ignore[attr-defined]

    def answer(self, arguments: list[str], *, timeout: float) -> tuple[int, object]:
        self.calls.append(tuple(arguments))
        if self.absent_kilix:
            raise FileNotFoundError("kilix")
        if arguments == ["list"]:
            return 0, {"root": str(self.root), "models": []}
        if arguments[0] == "verify":
            from kilix_playalong import licensed

            asset = arguments[1]
            model = next(item for item in licensed.ALL if item.asset_id == asset)
            present = (self.root / "assets" / asset / model.marker).is_file()
            if asset in self.refused or not present:
                return 1, None
            return 0, {"verified": asset, "root": str(self.root)}
        raise AssertionError(f"unexpected kilix models call: {arguments}")


@pytest.fixture(autouse=True)
def licensed_store(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[FakeLicensedStore]:
    from kilix_playalong import licensed

    # Outside tmp_path: several tests list exactly what they wrote there.
    store = FakeLicensedStore(tmp_path_factory.mktemp("content-root"))
    licensed.forget_root()
    monkeypatch.setattr(licensed, "_kilix_json", store.answer)
    yield store
    licensed.forget_root()
