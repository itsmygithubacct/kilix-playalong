from __future__ import annotations

from pathlib import Path

import pytest

from kilix_playalong.errors import ProviderUnavailableError
from kilix_playalong.providers import transcription


@pytest.mark.parametrize(
    ("available_gib", "expected"),
    [
        (16, "large-v3"),
        (8, "large-v3-turbo"),
        (4, "medium"),
        (2, "small"),
    ],
)
def test_auto_model_adapts_to_available_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available_gib: int,
    expected: str,
) -> None:
    monkeypatch.setattr(transcription, "_cuda_available", lambda: False)
    monkeypatch.setattr(
        transcription,
        "_available_memory_bytes",
        lambda: available_gib * 1024**3,
    )

    assert (
        transcription.resolve_model(
            "auto",
            device="auto",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == expected
    )


def test_auto_model_prefers_large_v3_on_cuda(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_cuda_available", lambda: True)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 1024**3)

    assert (
        transcription.resolve_model(
            "auto",
            device="auto",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == "large-v3"
    )


def test_auto_model_uses_strongest_compatible_cached_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_cuda_available", lambda: False)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 16 * 1024**3)
    snapshot = (
        tmp_path / "models--Systran--faster-whisper-medium" / "snapshots" / "fixture-revision"
    )
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"fixture")

    assert (
        transcription.resolve_model(
            "auto",
            device="cpu",
            model_cache=tmp_path,
            allow_model_downloads=False,
        )
        == "medium"
    )


def test_auto_model_requires_download_permission_without_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(transcription, "_cuda_available", lambda: False)
    monkeypatch.setattr(transcription, "_available_memory_bytes", lambda: 16 * 1024**3)

    with pytest.raises(ProviderUnavailableError, match="--allow-model-downloads"):
        transcription.resolve_model(
            "auto",
            device="cpu",
            model_cache=tmp_path,
            allow_model_downloads=False,
        )


def test_explicit_model_is_never_replaced(tmp_path: Path) -> None:
    assert (
        transcription.resolve_model(
            "small",
            device="cuda",
            model_cache=tmp_path,
            allow_model_downloads=True,
        )
        == "small"
    )
