"""User-facing exception hierarchy."""

from __future__ import annotations


class PlayalongError(Exception):
    """An expected failure that the CLI can report without a traceback."""


class InvalidInputError(PlayalongError):
    """Input failed validation.

    Including inside a provider. A value outside a closed set -- a model name that
    is not in ``SUPPORTED_MODELS``, an audio source that is not one of the three --
    is a bad argument wherever it is checked, and the provider that checks it second
    must raise the same class the pipeline raises when it checks it first. The three
    classes below all read as "the provider" from a call site, so which one a
    provider picks for a bad argument used to come down to which module it was
    written in; the rule is here so it does not have to be inferred again.

    It is load-bearing exactly once: ``pipeline._apply_alignment`` catches this and
    ``ProviderUnavailableError`` and *skips* alignment, while ``ProviderFailedError``
    fails the whole stage.
    """


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
