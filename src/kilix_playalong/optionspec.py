"""The frontend contract: every backend option, its default, and whether it can run here.

Both surfaces render their intake screen from this one description, so an option
cannot exist in the browser studio and be missing from the native surface, and a
default cannot mean one thing in one place and something else in the other. The
CLI takes its defaults from here too, which is what keeps `--help` honest.

A descriptor carries three separable facts that the surfaces keep confusing when
they are merged:

* what the option *is* - identity, type, choices, help;
* what it defaults to *here*, which may depend on the machine (a GPU changes the
  transcription default) and is therefore resolved, not hardcoded; and
* whether it can run *at all* on this machine, with a reason when it cannot.

The third is why `available` and `unavailable_reason` exist. A surface must be
able to show an option that cannot run - greyed out with the reason - rather than
silently hide it, because a hidden option looks like a missing feature and sends
people to the issue tracker instead of to `uv sync --all-extras`.

Relations *between* options are the one thing none of the three can express, and
`OptionGroup.exclusive` is the only one described here. It earns the field by
being enforced: the backend refuses the combination, so the description can be
checked against the refusal rather than asserted. Relevance -- an option that is
merely ignored beside some value of another -- is deliberately absent, because
nothing refuses it and a claim with no enforcement behind it is the kind this
module exists not to make.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

OPTIONS_SCHEMA = "kilix.playalong.options/v1"

OptionType = Literal["enum", "bool", "int", "float", "text", "path", "tuning"]

#: Stage identifiers an option can affect, matching state.STAGE_NAMES plus the
#: pseudo-stage "source" for intake decisions made before any stage runs.
OptionStage = Literal[
    "source",
    "download",
    "normalize",
    "separate",
    "lyrics",
    "transcribe-guitar",
    "tablature",
    "export",
]


@dataclass(frozen=True)
class Choice:
    """One selectable value of an enum option.

    `available` is per-choice on purpose: "transcribe with faster-whisper" can be
    unavailable while "use the supplied file" is fine, and an enum whose
    availability is only declared at the option level cannot express that.
    """

    value: str
    label: str
    help: str = ""
    available: bool = True
    unavailable_reason: str | None = None

    def as_json(self) -> dict[str, object]:
        return {
            "value": self.value,
            "label": self.label,
            "help": self.help,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True)
class OptionSpec:
    """One backend option, described completely enough to render without guessing."""

    id: str
    label: str
    type: OptionType
    default: object
    stage: OptionStage
    help: str = ""
    choices: tuple[Choice, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    #: Advanced options are rendered behind a disclosure. Everything remains
    #: reachable; this only decides what a first-time screen leads with.
    advanced: bool = False
    available: bool = True
    unavailable_reason: str | None = None
    #: True when the resolved default came from inspecting this machine rather
    #: than from a constant, so a surface can say "auto (large-v3 here)".
    default_is_resolved: bool = False
    resolved_note: str = ""

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "default": self.default,
            "stage": self.stage,
            "help": self.help,
            "choices": [choice.as_json() for choice in self.choices],
            "minimum": self.minimum,
            "maximum": self.maximum,
            "unit": self.unit,
            "advanced": self.advanced,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "default_is_resolved": self.default_is_resolved,
            "resolved_note": self.resolved_note,
        }


@dataclass(frozen=True)
class OptionGroup:
    """A rendered section of the intake screen."""

    id: str
    label: str
    help: str = ""
    options: tuple[OptionSpec, ...] = ()
    #: Sets of option ids of which at most one may be filled. This is the relation
    #: `available` cannot carry: filling both arms of the source union is a
    #: contradiction rather than an invalid value, so greying either arm before the
    #: user has typed anything would describe as impossible a form that is not yet
    #: wrong. Stated here, a surface can refuse the second entry as it is made;
    #: left unstated, the only way to learn the rule is to post and read the
    #: backend's `InvalidInputError` back, which is how each surface comes to
    #: invent its own wording of it -- `pipeline.PipelineOptions.source_spec` and
    #: `cli` already refuse this one in two sentences that differ.
    exclusive: tuple[tuple[str, ...], ...] = ()

    def as_json(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "help": self.help,
            "options": [option.as_json() for option in self.options],
            "exclusive": [list(ids) for ids in self.exclusive],
        }


@dataclass(frozen=True)
class OptionsDocument:
    """The whole contract, as one serialisable document."""

    groups: tuple[OptionGroup, ...] = field(default_factory=tuple)

    def as_json(self) -> dict[str, object]:
        return {
            "schema": OPTIONS_SCHEMA,
            "groups": [group.as_json() for group in self.groups],
        }

    def option(self, option_id: str) -> OptionSpec | None:
        for group in self.groups:
            for option in group.options:
                if option.id == option_id:
                    return option
        return None

    def defaults(self) -> dict[str, object]:
        """Every option's resolved default, keyed by id.

        This is the single place a surface or the CLI should get defaults from.
        Two sources of defaults is how the browser and the native surface would
        start disagreeing about what "auto" means.
        """

        return {option.id: option.default for group in self.groups for option in group.options}
