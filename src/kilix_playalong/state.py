"""Versioned project manifests persisted through the shared kilix-state module."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from kilix_state import CorruptStateError, StateNotFoundError, Store

from . import PROJECT_SCHEMA
from .errors import CorruptProjectError
from .paths import project_artifact
from .types import Artifact, ProjectManifest, Stage
from .util import canonical_json, sha256_file, utc_now

MANIFEST_NAME = "project.state"
MAX_MANIFEST_SIZE = 2 * 1024 * 1024
STAGE_NAMES = (
    "download",
    "normalize",
    "separate",
    "lyrics",
    "transcribe-guitar",
    "tablature",
    "export",
)


def _pending_stage() -> Stage:
    return {
        "status": "pending",
        "started_at": None,
        "finished_at": None,
        "provider": None,
        "artifacts": [],
        "error": None,
    }


def new_manifest(
    project_id: str,
    *,
    url_sha256: str,
    rights_statement: str,
    title: str = "",
    artist: str = "",
    model: str = "htdemucs_6s",
    language: str = "auto",
    whisper_model: str = "small",
    device: str = "auto",
    max_duration: float = 30 * 60,
    tuning: tuple[int, ...] = (40, 45, 50, 55, 59, 64),
    max_fret: int = 20,
) -> ProjectManifest:
    now = utc_now()
    return {
        "schema": PROJECT_SCHEMA,
        "id": project_id,
        "title": title,
        "artist": artist,
        "created_at": now,
        "updated_at": now,
        "source": {
            "kind": "youtube",
            "url_sha256": url_sha256,
            "authorization": {
                "confirmed": True,
                "confirmed_at": now,
                "statement": rights_statement,
            },
        },
        "settings": {
            "separation_model": model,
            "language": language,
            "whisper_model": whisper_model,
            "device": device,
            "max_duration": max_duration,
            "tuning": list(tuning),
            "max_fret": max_fret,
        },
        "stages": {name: _pending_stage() for name in STAGE_NAMES},
        "tracks": [],
        "lyrics": None,
        "tablature": None,
    }


def _manifest_store(project_dir: Path) -> Store:
    return Store(
        absolute_path=project_dir / MANIFEST_NAME,
        max_payload=MAX_MANIFEST_SIZE,
    )


def _valid_artifact(value: object) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("path"), str)
        and isinstance(value.get("sha256"), str)
        and len(value["sha256"]) == 64
        and isinstance(value.get("size"), int)
        and value["size"] >= 0
    )


def _valid_stage(value: object) -> bool:
    return (
        isinstance(value, dict)
        and value.get("status") in {"pending", "running", "done", "error"}
        and isinstance(value.get("artifacts"), list)
        and all(_valid_artifact(item) for item in value["artifacts"])
        and (value.get("provider") is None or isinstance(value.get("provider"), str))
        and (value.get("error") is None or isinstance(value.get("error"), str))
    )


def _valid_track(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    return (
        all(isinstance(value.get(name), str) for name in ("id", "label", "kind", "path", "sha256"))
        and isinstance(value.get("size"), int)
        and value["size"] >= 0
        and isinstance(value.get("default_muted"), bool)
    )


def _valid_manifest(value: object) -> bool:
    if not isinstance(value, dict) or value.get("schema") != PROJECT_SCHEMA:
        return False
    if not all(
        isinstance(value.get(name), str)
        for name in ("id", "title", "artist", "created_at", "updated_at")
    ):
        return False
    source = value.get("source")
    settings = value.get("settings")
    stages = value.get("stages")
    tracks = value.get("tracks")
    if (
        not isinstance(source, dict)
        or not isinstance(settings, dict)
        or not isinstance(stages, dict)
        or set(stages) != set(STAGE_NAMES)
        or not all(_valid_stage(stages.get(name)) for name in STAGE_NAMES)
        or not isinstance(tracks, list)
        or not all(_valid_track(track) for track in tracks)
    ):
        return False
    return all(
        value.get(name) is None or isinstance(value.get(name), dict)
        for name in ("lyrics", "tablature")
    )


def save_manifest(project_dir: Path, manifest: ProjectManifest) -> None:
    manifest["updated_at"] = utc_now()
    payload = canonical_json(manifest)
    with _manifest_store(project_dir) as store:
        store.save(payload)


def load_manifest(project_dir: Path) -> ProjectManifest:
    try:
        with _manifest_store(project_dir) as store:
            payload = store.load()
    except StateNotFoundError as error:
        raise CorruptProjectError(f"project has no {MANIFEST_NAME}") from error
    except CorruptStateError as error:
        raise CorruptProjectError("project manifest failed its integrity check") from error
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CorruptProjectError("project manifest is not valid UTF-8 JSON") from error
    if not _valid_manifest(value):
        raise CorruptProjectError("project manifest does not satisfy its versioned schema")
    return cast(ProjectManifest, value)


def begin_stage(
    manifest: ProjectManifest,
    name: str,
    provider: str,
    *,
    fingerprint: str,
) -> None:
    stage = manifest["stages"][name]
    stage.pop("note", None)
    stage["status"] = "running"
    stage["started_at"] = utc_now()
    stage["finished_at"] = None
    stage["provider"] = provider
    stage["artifacts"] = []
    stage["error"] = None
    stage["fingerprint"] = fingerprint


def finish_stage(
    project_dir: Path,
    manifest: ProjectManifest,
    name: str,
    paths: list[Path],
    *,
    note: str = "",
) -> None:
    artifacts: list[Artifact] = []
    for path in paths:
        relative = path.resolve().relative_to(project_dir.resolve()).as_posix()
        artifacts.append(
            {"path": relative, "sha256": sha256_file(path), "size": path.stat().st_size}
        )
    stage = manifest["stages"][name]
    stage["status"] = "done"
    stage["finished_at"] = utc_now()
    stage["artifacts"] = artifacts
    stage["error"] = None
    if note:
        stage["note"] = note


def fail_stage(manifest: ProjectManifest, name: str, message: str) -> None:
    stage = manifest["stages"][name]
    stage["status"] = "error"
    stage["finished_at"] = utc_now()
    stage["error"] = message


def stage_is_current(
    project_dir: Path,
    manifest: ProjectManifest,
    name: str,
    *,
    fingerprint: str,
) -> bool:
    stage = manifest["stages"].get(name)
    if not stage or stage["status"] != "done" or stage.get("fingerprint") != fingerprint:
        return False
    for artifact in stage["artifacts"]:
        path = project_artifact(project_dir, artifact["path"])
        if not path.is_file() or path.stat().st_size != artifact["size"]:
            return False
        if sha256_file(path) != artifact["sha256"]:
            return False
    return True
