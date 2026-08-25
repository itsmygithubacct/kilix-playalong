"""Private XDG path resolution and project-id validation."""

from __future__ import annotations

import os
import re
from contextlib import suppress
from pathlib import Path

from .errors import CorruptProjectError, InvalidInputError, ProjectNotFoundError

APP_ID = "kilix-playalong"
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,63}$")


def _absolute_env(name: str) -> Path | None:
    value = os.environ.get(name)
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        raise InvalidInputError(f"{name} must be an absolute path")
    return path


def _xdg(name: str, fallback: Path) -> Path:
    configured = _absolute_env(name)
    return configured if configured is not None else fallback


def data_home() -> Path:
    override = _absolute_env("KILIX_PLAYALONG_DATA_HOME")
    if override is not None:
        return override
    home = Path.home()
    return _xdg("XDG_DATA_HOME", home / ".local" / "share") / APP_ID


def cache_home() -> Path:
    override = _absolute_env("KILIX_PLAYALONG_CACHE_HOME")
    if override is not None:
        return override
    return _xdg("XDG_CACHE_HOME", Path.home() / ".cache") / APP_ID


def ensure_private_directory(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    with suppress(OSError):
        path.chmod(0o700)
    return path


def projects_home() -> Path:
    return ensure_private_directory(data_home() / "projects")


def validate_project_id(project_id: str) -> str:
    if not _PROJECT_ID.fullmatch(project_id):
        raise InvalidInputError("project id must be 8-64 lowercase letters, digits, or hyphens")
    return project_id


def project_directory(project_id: str, *, must_exist: bool = False) -> Path:
    path = projects_home() / validate_project_id(project_id)
    if must_exist and not path.is_dir():
        raise ProjectNotFoundError(f"no project named {project_id}")
    return path


def resolve_project(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve(strict=False)
        if not resolved.is_dir():
            raise ProjectNotFoundError(f"project directory does not exist: {value}")
        return resolved
    return project_directory(value, must_exist=True)


def project_artifact(project_dir: Path, relative: str) -> Path:
    if not relative or "\x00" in relative:
        raise CorruptProjectError("project contains an invalid artifact path")
    item = Path(relative)
    if item.is_absolute():
        raise CorruptProjectError("project contains an absolute artifact path")
    root = project_dir.resolve()
    candidate = (root / item).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise CorruptProjectError("project artifact escapes its private directory") from error
    return candidate
