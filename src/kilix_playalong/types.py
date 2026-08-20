"""Typed JSON shapes used across process and browser boundaries."""

from __future__ import annotations

from typing import Literal, TypedDict

from typing_extensions import NotRequired

StageStatus = Literal["pending", "running", "done", "error"]


class Artifact(TypedDict):
    path: str
    sha256: str
    size: int


class Stage(TypedDict):
    status: StageStatus
    started_at: str | None
    finished_at: str | None
    provider: str | None
    artifacts: list[Artifact]
    error: str | None
    note: NotRequired[str]
    fingerprint: NotRequired[str]


class AudioTrack(TypedDict):
    id: str
    label: str
    kind: str
    path: str
    sha256: str
    size: int
    default_muted: bool


class LyricWord(TypedDict):
    start: float
    end: float
    text: str


class LyricCue(TypedDict):
    start: float
    end: float
    text: str
    words: list[LyricWord]


class TabPosition(TypedDict):
    string: int
    fret: int
    pitch: int


class TabEvent(TypedDict):
    start: float
    end: float
    positions: list[TabPosition]


class ProjectManifest(TypedDict):
    schema: str
    id: str
    title: str
    artist: str
    created_at: str
    updated_at: str
    source: dict[str, object]
    settings: dict[str, object]
    stages: dict[str, Stage]
    tracks: list[AudioTrack]
    lyrics: dict[str, object] | None
    tablature: dict[str, object] | None
