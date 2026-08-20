"""User-facing exception hierarchy."""

from __future__ import annotations


class PlayalongError(Exception):
    """An expected failure that the CLI can report without a traceback."""


class InvalidInputError(PlayalongError):
    """Input failed validation."""


class RightsConfirmationRequired(InvalidInputError):
    """Remote media processing was requested without an explicit rights decision."""


class ProviderUnavailableError(PlayalongError):
    """An optional executable, Python package, or model is unavailable."""


class ProviderFailedError(PlayalongError):
    """A bounded provider process failed."""


class ProjectNotFoundError(PlayalongError):
    """A project id or path does not resolve to a project."""


class CorruptProjectError(PlayalongError):
    """A stored project does not satisfy the versioned manifest contract."""
