"""The intake description has to be true, and it has to stay true.

Two properties make `options_registry` a contract rather than prose, and both are
enforced here rather than asserted in a docstring:

1. every default is a value its own option would accept -- and, decisively, a
   value `pipeline._validate_options` accepts, because a preselected value the
   backend rejects is a form that cannot be submitted; and
2. every option id names something the backend actually consumes, and every
   backend field is named by some option -- neither direction has a fallback,
   because an option with no field cannot be carried into a run and a field with
   no option is a setting no surface can reach.

A third belongs beside them, because it is the one the document gets wrong in the
direction nobody notices:

3. an option is greyed only when nothing behind it can run, and a greyed option
   never leaves a live choice behind it. Over-greying is silent -- the user reads
   a missing feature and stops -- so it is checked structurally, over every option
   in the document, rather than option by option.

The rest of the file pins the probes: availability has to be *computed*, so the
tests take the machine away piece by piece and read what the document then says.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from kilix_playalong import alignment, cli, options_registry, pipeline, source
from kilix_playalong.errors import InvalidInputError, ProviderUnavailableError
from kilix_playalong.options_registry import (
    BACKEND_FIELDS,
    TUNINGS,
    build_options_document,
    tuning_pitches,
)
from kilix_playalong.optionspec import OPTIONS_SCHEMA, Choice, OptionsDocument, OptionSpec
from kilix_playalong.pipeline import (
    LYRIC_SOURCE_CHOICES,
    PipelineOptions,
    _validate_options,
)
from kilix_playalong.providers import basic_pitch, media, separation, transcription
from kilix_playalong.state import STAGE_NAMES

VALID_URL = "https://www.youtube.com/watch?v=abcdefghijk"


def _options(document: OptionsDocument) -> list[OptionSpec]:
    return [option for group in document.groups for option in group.options]


def _choice(document: OptionsDocument, option_id: str, value: str) -> Choice:
    option = document.option(option_id)
    assert option is not None, f"no option {option_id}"
    return next(choice for choice in option.choices if choice.value == value)


def _pipeline_options(document: OptionsDocument, **overrides: Any) -> PipelineOptions:
    """Build `PipelineOptions` out of the document's own defaults, as a surface would."""

    names = {field.name for field in fields(PipelineOptions)}
    values: dict[str, Any] = {}
    for option_id, value in document.defaults().items():
        field_name = BACKEND_FIELDS[option_id]
        if field_name not in names:
            continue
        values[field_name] = tuning_pitches(value) if field_name == "tuning" else value
    values.update(overrides)
    return PipelineOptions(**values)


def _strip_machine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    whisper: bool = True,
    demucs: bool = True,
    guitar: bool = True,
    modules: tuple[str, ...] = ("yt_dlp", "ctranslate2"),
    resolved: str | None = "large-v3",
    cached: str | None = "large-v3",
    tools: tuple[str, ...] = (),
) -> None:
    """Pin every probe the document reads, so the assertions are about one change.

    `resolved` and `cached` are the two answers `resolve_model` gives -- with
    downloads permitted and without -- and they are separate because the document
    asks it both ways and the difference between them is a different sentence on
    the screen. `cached=None` alone is "nothing cached, a download would work";
    both None is "the provider names nothing at all".
    """

    monkeypatch.setattr(transcription, "is_available", lambda: whisper)
    monkeypatch.setattr(separation, "is_available", lambda: demucs)
    monkeypatch.setattr(basic_pitch, "is_available", lambda: guitar)
    monkeypatch.setattr(options_registry, "_has_module", lambda name: name in modules)
    monkeypatch.setattr(transcription, "cuda_available", lambda: False)

    def _resolve(requested: str, *, allow_model_downloads: bool = False, **_: object) -> str:
        if requested != transcription.AUTO_MODEL:
            return requested
        answer = resolved if allow_model_downloads else cached
        if answer is None:
            raise ProviderUnavailableError("no suitable cached faster-whisper model is available")
        return answer

    monkeypatch.setattr(transcription, "resolve_model", _resolve)

    def _require() -> None:
        if tools:
            raise ProviderUnavailableError("missing required media tools: " + ", ".join(tools))

    monkeypatch.setattr(media, "require_media_tools", _require)


# --- invariant 1: a default is a value its own option accepts -------------------


def test_every_default_is_a_valid_value_for_its_own_option() -> None:
    for option in _options(build_options_document()):
        if option.choices:
            values = [choice.value for choice in option.choices]
            assert option.default in values, f"{option.id} default is not one of its choices"
        if option.type == "bool":
            assert isinstance(option.default, bool), option.id
        if option.type in {"int", "float"}:
            assert isinstance(option.default, int | float), option.id
            assert not isinstance(option.default, bool), option.id
            if option.minimum is not None:
                assert option.default >= option.minimum, option.id
            if option.maximum is not None:
                assert option.default <= option.maximum, option.id
        if option.type in {"enum", "tuning"}:
            assert option.choices, f"{option.id} is an enum with nothing to choose"
        if option.type in {"text", "enum", "tuning"}:
            assert isinstance(option.default, str), option.id
        if option.type == "path":
            assert option.default is None or isinstance(option.default, str), option.id


def test_an_available_option_never_preselects_an_unavailable_choice() -> None:
    """Being offered a control whose preselected value is greyed out is a dead end."""

    for allow in (False, True):
        for option in _options(build_options_document(allow_model_downloads=allow)):
            if not option.available or not option.choices:
                continue
            selected = next(c for c in option.choices if c.value == option.default)
            assert selected.available, f"{option.id} preselects the unavailable {selected.value}"


def test_document_defaults_satisfy_the_pipeline_validator() -> None:
    """The strongest form of invariant 1: run the real validator over the real defaults."""

    document = build_options_document()
    options = _pipeline_options(document, url=VALID_URL, rights_confirmed=True)
    _validate_options(options, require_rights=True)


@pytest.mark.parametrize("option_id", ["max_duration", "max_fret"])
def test_numeric_bounds_match_the_pipeline_validator(option_id: str) -> None:
    """The advertised range must be the range the backend enforces, at both ends."""

    document = build_options_document()
    option = document.option(option_id)
    assert option is not None and option.minimum is not None and option.maximum is not None
    field = BACKEND_FIELDS[option_id]
    cast = int if option.type == "int" else float

    for accepted in (option.minimum, option.maximum):
        _validate_options(
            _pipeline_options(document, url=VALID_URL, **{field: cast(accepted)}),
            require_rights=False,
        )
    for rejected in (option.minimum - 1, option.maximum + 1):
        with pytest.raises(InvalidInputError):
            _validate_options(
                _pipeline_options(document, url=VALID_URL, **{field: cast(rejected)}),
                require_rights=False,
            )


def test_every_offered_choice_is_a_value_the_backend_accepts() -> None:
    """Availability decides what is *selectable*; validity decides what is submittable.

    A greyed-out choice still has to be a legal value -- the option can become
    available on another machine, or after one `uv sync` -- so every choice is
    checked, not only the ones this machine can run.
    """

    document = build_options_document()
    names = {field.name for field in fields(PipelineOptions)}
    for option in _options(document):
        field = BACKEND_FIELDS[option.id]
        if field not in names or not option.choices:
            continue
        for choice in option.choices:
            value = tuning_pitches(choice.value) if field == "tuning" else choice.value
            _validate_options(
                _pipeline_options(document, url=VALID_URL, **{field: value}),
                require_rights=False,
            )


def test_every_named_tuning_is_a_tuning_the_backend_accepts() -> None:
    document = build_options_document()
    option = document.option("tuning")
    assert option is not None
    for choice in option.choices:
        _validate_options(
            _pipeline_options(document, url=VALID_URL, tuning=tuning_pitches(choice.value)),
            require_rights=False,
        )


# --- invariant 2: an option id names something the backend consumes -------------


def test_every_option_id_names_a_pipeline_field() -> None:
    """Invariant 2 against the real dataclass, asserted outright.

    It used to be "or admits it cannot" -- an option with no field described itself
    as unavailable and named the field it waited for. That was the right shape
    while three streams were still landing fields, and this is what replaced it,
    on both sides: a test whose only live branch is `continue` is not a check, and
    neither is production code whose other branch needs a build this test has
    already failed. What is asserted is the thing a surface depends on: post
    `defaults()` at `PipelineOptions` and every key lands.
    """

    names = {field.name for field in fields(PipelineOptions)}
    missing = sorted(
        f"{option_id} -> {field_name}"
        for option_id, field_name in BACKEND_FIELDS.items()
        if field_name not in names
    )
    assert not missing, (
        f"option ids with no backing field: {missing}. Add the field, or take the option "
        "out: the document describes it as available and a surface will post it."
    )


def test_the_document_and_the_backend_describe_the_same_fields() -> None:
    """The correspondence, asserted rather than described.

    Two directions, two different bugs. An option id with no field is a control
    that cannot be carried into a run, and the test above is that direction. A
    field with no option is this one: a backend setting no surface offers and no
    `--help` mentions, which is how a feature ships unreachable. It becomes
    checkable the moment `PipelineOptions` grows, and it fails rather than being
    noticed.
    """

    names = {field.name for field in fields(PipelineOptions)}
    undescribed = sorted(names - set(BACKEND_FIELDS.values()))
    assert not undescribed, (
        f"PipelineOptions carries fields no option describes: {undescribed}. "
        "Add them to BACKEND_FIELDS and to the document, or no surface can set them."
    )


def test_the_exclusive_set_is_the_one_the_backend_refuses() -> None:
    """`OptionGroup.exclusive` is a claim about `source_spec`, so it is run through it.

    The description is only worth having if it names exactly the combination the
    backend rejects: a set the backend tolerates would have a surface refusing
    input that is legal, and a rejected combination the document does not name is
    the `InvalidInputError`-after-submit this field exists to avoid. Both halves
    are driven -- the pair together raises, and each arm alone does not -- so the
    set cannot drift from the rule while staying green.

    Any *further* exclusive set fails here rather than passing untested: a
    relation nothing checks is the kind of claim this module does not make.
    """

    document = build_options_document()
    declared = [(group.id, tuple(ids)) for group in document.groups for ids in group.exclusive]
    assert declared == [("source", ("url", "source_path"))], (
        f"undriven exclusive sets: {declared}. Add each one's enforcement check here."
    )

    filled: dict[str, Any] = {"url": VALID_URL, "source_path": Path("/tmp/song.mp3")}
    with pytest.raises(InvalidInputError):
        _pipeline_options(document, **filled).source_spec()
    for option_id, value in filled.items():
        # One arm at a time is legal, which is what makes the pair a contradiction
        # rather than two bad values.
        _pipeline_options(document, **{option_id: value}).source_spec()


def test_the_lyric_sources_offered_are_the_backend_vocabulary() -> None:
    """A lyric source the backend accepts and the form never offers is unreachable.

    The choice *values* are now `pipeline`'s own constants, so a rename cannot
    make them disagree. A new member of `LYRIC_SOURCE_CHOICES` still can: the
    backend would accept it, `--lyrics-source` would document it, and neither
    surface would ever show it. That is the direction a shared constant does not
    cover, so it is the direction this asserts.
    """

    option = build_options_document().option("lyrics_source")
    assert option is not None
    assert [choice.value for choice in option.choices] == list(LYRIC_SOURCE_CHOICES), (
        "the lyric sources on the form are not the ones the backend takes"
    )


def test_backend_fields_describe_exactly_the_document() -> None:
    ids = [option.id for option in _options(build_options_document())]
    assert sorted(ids) == sorted(BACKEND_FIELDS), "BACKEND_FIELDS and the document disagree"
    # Two options pointed at one field is the same bug in the other direction: the
    # second silently overwrites the first on the way into the backend.
    assert len(set(BACKEND_FIELDS.values())) == len(BACKEND_FIELDS)


def test_defaults_that_a_pipeline_field_already_has_match_that_field() -> None:
    """Two places holding a default is how the surfaces start disagreeing.

    `tuning` is the one deliberate translation -- the option's value is a name so
    that it can be a member of `choices` -- so it is compared through the same
    resolver a surface would use.

    A *resolved* default is allowed to differ, and has to: `audio_source` moves to
    the full mix on a machine with no Demucs, and `whisper_model` moves off `auto`
    when `auto` has nothing to load, precisely because the constant would not run
    here. What is not allowed is differing quietly -- so a divergence must be
    flagged `default_is_resolved` and carry the note that explains it. Silent
    drift, the bug this test was written for, still fails.
    """

    document = build_options_document()
    defaults = document.defaults()
    for field in fields(PipelineOptions):
        option_id = next((i for i, f in BACKEND_FIELDS.items() if f == field.name), None)
        if option_id is None or field.default is field.default_factory:
            continue
        value = defaults[option_id]
        if field.name == "tuning":
            assert tuning_pitches(value) == field.default
            continue
        if value == field.default:
            continue
        option = document.option(option_id)
        assert option is not None
        assert option.default_is_resolved and option.resolved_note, (
            f"{option_id} has drifted from PipelineOptions without saying it resolved"
        )


# --- invariant 3: greying is bounded in both directions -------------------------


#: Machines to sweep the structural rules over. This tree can currently run
#: everything, so the document it produces here has no greyed option in it at all
#: and a rule about greyed options checked against it checks nothing. Each entry
#: takes one thing away; the last takes everything.
_DEGRADED_MACHINES: tuple[tuple[str, dict[str, Any]], ...] = (
    ("no faster-whisper", {"whisper": False}),
    ("no Demucs", {"demucs": False}),
    ("no Basic Pitch", {"guitar": False}),
    ("no yt-dlp", {"modules": ("ctranslate2",)}),
    ("no ctranslate2", {"modules": ("yt_dlp",)}),
    ("no ffprobe", {"tools": ("ffprobe",)}),
    ("nothing cached", {"cached": None}),
    ("no model under either answer", {"cached": None, "resolved": None}),
    (
        "a bare machine",
        {
            "whisper": False,
            "demucs": False,
            "guitar": False,
            "modules": (),
            "tools": ("ffmpeg", "ffprobe"),
            "cached": None,
            "resolved": None,
        },
    ),
)


def _degraded_documents(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[str, OptionsDocument]]:
    """Every machine in the sweep, each described under both download answers."""

    for label, machine in _DEGRADED_MACHINES:
        with monkeypatch.context() as patch:
            _strip_machine(patch, **machine)
            for allow in (False, True):
                document = build_options_document(allow_model_downloads=allow)
                yield f"{label} (downloads={allow})", document


def test_a_greyed_control_never_leaves_a_live_choice_behind(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surface disables a greyed control, so a live choice inside it is unreachable.

    Which makes any escape the reason names unreachable too -- an option that says
    "select a model whose weights you already have" while every way of selecting
    one is behind a disabled control is telling the user to do something the
    screen forbids. Structural, over every option of every machine in the sweep,
    because it was reintroduced one option at a time: `device`, `tuning`,
    `lyrics_source` and `audio_source` all carried live choices under a greyed
    control before `_greyed_choices` existed.
    """

    greyed = 0
    for label, document in _degraded_documents(monkeypatch):
        for option in _options(document):
            if option.available:
                continue
            greyed += 1
            for choice in option.choices:
                assert not choice.available, (
                    f"{label}: {option.id} is greyed but offers a live {choice.value}"
                )
                assert choice.unavailable_reason, f"{label}: {option.id}/{choice.value} is silent"
    assert greyed > 10, f"only {greyed} greyed options in the sweep; this checked nothing"


def test_an_available_option_always_leaves_something_to_pick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other direction: an enum with nothing selectable is a greyed control lying."""

    for label, document in _degraded_documents(monkeypatch):
        for option in _options(document):
            if not option.available or not option.choices:
                continue
            assert any(choice.available for choice in option.choices), (
                f"{label}: {option.id} claims to be available with no choice that is"
            )


def test_no_machine_preselects_a_choice_it_greyed_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`test_an_available_option_never_preselects_an_unavailable_choice`, swept.

    The document this tree produces has one greyed choice in it, so the invariant
    is only tested against one shape of machine unless it is run against the rest.
    """

    for label, document in _degraded_documents(monkeypatch):
        for option in _options(document):
            if not option.available or not option.choices:
                continue
            selected = next(c for c in option.choices if c.value == option.default)
            assert selected.available, f"{label}: {option.id} preselects a greyed {selected.value}"


def test_a_blocked_auto_still_offers_the_models_its_reason_points_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Over-greying, closed at the one place it happens, with the escape made real.

    `auto` scans four of the sixteen models; "nothing `auto` scans is cached" is
    therefore not "nothing is cached", and a user holding `distil-large-v3`
    weights can transcribe today. Greying the whole control tells them the feature
    is missing. So the option stays available, only `auto` is greyed, and the
    escape its reason names -- picking a model yourself -- is asserted to be on
    the screen rather than described as being there.
    """

    _strip_machine(monkeypatch, cached=None)
    option = build_options_document().option("whisper_model")
    assert option is not None
    assert option.available, "a named model still runs here; greying the control hides it"

    auto = next(c for c in option.choices if c.value == transcription.AUTO_MODEL)
    assert not auto.available and auto.unavailable_reason is not None
    assert "select a model whose weights you already have" in auto.unavailable_reason

    # The escape, as a fact about the document rather than a sentence in it.
    selectable = [c.value for c in option.choices if c.available]
    assert transcription.AUTO_MODEL not in selectable
    assert set(selectable) == set(transcription.SUPPORTED_MODELS)
    assert option.default in selectable, "the escape is offered but nothing is preselected"
    assert option.default_is_resolved and option.resolved_note
    # ...and the preselection is not sold as something that will run.
    assert "not cached" in option.resolved_note


def test_a_blocked_auto_does_not_grey_transcription_as_a_lyric_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same over-greying, one option over, in the one state that can show it.

    faster-whisper installed, downloads off, nothing `auto` scans cached, a model
    still nameable: this is the only machine on which `_Machine.whisper_reason`
    and `_Machine.transcription_reason` disagree, and the two are asserted to
    disagree here so the rest of this test cannot pass by both being None. The
    "Transcribe the singing" arm and the audio-source control read the weaker of
    the two, because a named model transcribes here and greying them would tell a
    user holding `distil-large-v3` weights that the feature is gone.

    The suite's other assertion on this choice
    (`test_what_to_transcribe_dies_with_transcription`) runs on `whisper=False`,
    where both reasons are the same `_WHISPER_MISSING` string: it cannot see which
    of them the arm reads. This can.
    """

    _strip_machine(monkeypatch, cached=None)
    machine = options_registry._probe(allow_model_downloads=False)
    assert machine.whisper_reason is not None, "not the state this test is about"
    assert machine.transcription_reason is None, "not the state this test is about"

    document = build_options_document()
    transcribe = _choice(document, "lyrics_source", "transcribe")
    assert transcribe.available, "a named model still transcribes here"
    assert transcribe.unavailable_reason is None

    audio = document.option("audio_source")
    assert audio is not None and audio.available and audio.unavailable_reason is None


def test_the_gap_between_auto_and_the_model_list_is_real() -> None:
    """The whole case for not greying the control, as a fact about the provider.

    `auto` picks from `QUALITY_ORDER`; the option offers `SUPPORTED_MODELS`. If
    those two were the same set, "nothing `auto` scans is cached" *would* mean
    "nothing is cached" and greying the control would be right. They are not, and
    the docstrings that say so name the counts, so the counts are pinned here: a
    provider that widens `auto` to the whole list should fail this and take the
    prose with it.
    """

    assert set(transcription.QUALITY_ORDER) < set(transcription.SUPPORTED_MODELS)
    assert len(transcription.QUALITY_ORDER) == 4, "update the 'four of the sixteen' prose"
    assert len(transcription.SUPPORTED_MODELS) == 16, "update the 'four of the sixteen' prose"
    # The example the module docstring uses, and the reason it is not a corner:
    # every distilled model is outside what `auto` looks at.
    assert "distil-large-v3" in transcription.SUPPORTED_MODELS
    assert "distil-large-v3" not in transcription.QUALITY_ORDER


def test_nothing_transcribable_greys_the_model_control_and_all_of_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other side of the same rule: when nothing runs, nothing stays lit."""

    _strip_machine(monkeypatch, whisper=False)
    option = build_options_document().option("whisper_model")
    assert option is not None and not option.available
    assert all(not choice.available for choice in option.choices)
    assert option.default == transcription.AUTO_MODEL
    assert not option.default_is_resolved and option.resolved_note == ""


def test_a_provider_that_answers_nothing_does_not_blame_the_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ "Allow downloads" is only the fix when allowing them would actually help."""

    _strip_machine(monkeypatch, cached=None, resolved=None)
    option = build_options_document().option("whisper_model")
    assert option is not None and not option.available
    assert option.unavailable_reason is not None
    assert "doctor" in option.unavailable_reason
    assert "Allow model downloads" not in option.unavailable_reason


def test_what_to_transcribe_dies_with_transcription(monkeypatch: pytest.MonkeyPatch) -> None:
    """An audio source for a transcription that cannot happen is a resolved note about nothing."""

    _strip_machine(monkeypatch, whisper=False)
    document = build_options_document()

    audio = document.option("audio_source")
    assert audio is not None and not audio.available
    assert audio.unavailable_reason is not None
    assert "faster-whisper" in audio.unavailable_reason

    assert not _choice(document, "lyrics_source", "transcribe").available


def test_a_lyric_source_is_only_as_available_as_the_arm_that_feeds_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An embedded tag and a sidecar are read off a local file. No file arm, no tag."""

    _strip_machine(monkeypatch)
    with_arm = build_options_document().option("lyrics_source")
    assert with_arm is not None
    assert all(
        c.available for c in with_arm.choices if c.value in {"embedded", "sidecar", "captions"}
    )

    # Take the local-file arm away and watch the two sources that can only be fed
    # by it go with it. Injected rather than provoked: every machine probe that
    # can grey the file arm greys the link arm with it, so a machine cannot show
    # this rule on its own and the alternative to injecting it is not testing it.
    # The identity of the sentence is the assertion -- one expression reaches all
    # three controls, so the folding cannot be half-done.
    _strip_machine(monkeypatch)
    arm_gone = "no local file can be read on this machine"
    monkeypatch.setattr(options_registry, "_file_source_reason", lambda machine: arm_gone)
    document = build_options_document()
    source = document.option("lyrics_source")
    assert source is not None and source.available
    for value in ("embedded", "sidecar"):
        choice = next(c for c in source.choices if c.value == value)
        assert not choice.available, f"{value} is offered with no local file to read it from"
        assert choice.unavailable_reason == arm_gone
    assert next(c for c in source.choices if c.value == "file").available
    assert next(c for c in source.choices if c.value == "captions").available
    file_arm = document.option("source_path")
    assert file_arm is not None and file_arm.unavailable_reason == arm_gone

    # And captions go with the link arm for the same reason.
    _strip_machine(monkeypatch, tools=("ffprobe",))
    captions = _choice(build_options_document(), "lyrics_source", "captions")
    assert not captions.available
    assert captions.unavailable_reason is not None and "ffprobe" in captions.unavailable_reason


def test_the_automatic_lyric_choice_dies_with_its_last_arm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`auto` degrades through the arms; with every arm greyed it degrades to nothing.

    No machine can reach this today -- the supplied-file arm has no gate on this
    build, so something is always available -- so the arms are replaced directly
    rather than waiting for a machine that cannot exist yet. Without that, the
    rule would be a comment, and a mutation making `auto` unconditional would live
    here forever.
    """

    _strip_machine(monkeypatch)
    monkeypatch.setattr(
        options_registry,
        "_lyrics_source_arms",
        lambda machine: (
            Choice(value="file", label="A file I supply", available=False, unavailable_reason="no"),
        ),
    )
    source = build_options_document().option("lyrics_source")
    assert source is not None
    auto = next(c for c in source.choices if c.value == "auto")
    assert not auto.available, "'auto' offers a fallback to nothing"
    assert auto.unavailable_reason is not None and "nothing" in auto.unavailable_reason


# --- the document as a document -------------------------------------------------


def test_defaults_returns_every_id_exactly_once() -> None:
    document = build_options_document()
    ids = [option.id for option in _options(document)]
    assert len(ids) == len(set(ids)), "two options share an id and one silently wins"
    assert sorted(document.defaults()) == sorted(ids)


def test_every_option_names_a_stage_that_exists() -> None:
    """`stage` tells a surface which part of the run an option changes."""

    stages = {"source", *STAGE_NAMES}
    for option in _options(build_options_document()):
        assert option.stage in stages, f"{option.id} points at no stage of the pipeline"


def test_the_same_machine_describes_itself_the_same_way_twice() -> None:
    """Nothing in the document may depend on when it was built."""

    assert build_options_document().as_json() == build_options_document().as_json()


def test_cli_defaults_agree_with_the_document() -> None:
    """One description of a default, or the two surfaces and `--help` drift apart.

    The CLI now takes its defaults from this document rather than from constants
    of its own (`cli._default`), with one named exception: `cli._ADAPTIVE_DEFAULTS`
    keeps `--whisper-model auto` where the form substitutes a concrete model. So
    what this checks is no longer a copy but that exception -- a divergence is
    permitted only where the document itself says it resolved and says to what,
    and any other one is a screen disagreeing with `--help`.
    """

    document = build_options_document()
    arguments = cli.build_parser().parse_args(["create", VALID_URL])
    pairs = {
        "language": arguments.language,
        "model": arguments.model,
        "whisper_model": arguments.whisper_model,
        "device": arguments.device,
        "tuning": arguments.tuning,
        "max_fret": arguments.max_fret,
        "lyrics_path": arguments.lyrics,
        "title": arguments.title,
        "artist": arguments.artist,
        "allow_model_downloads": arguments.allow_model_downloads,
        "rights_confirmed": arguments.i_have_rights,
    }
    for option_id, cli_value in pairs.items():
        option = document.option(option_id)
        assert option is not None
        if cli_value == option.default:
            continue
        # The CLI holds constants; the document resolves. They may differ only
        # where the document says it resolved and says what it resolved to --
        # `--whisper-model auto` on a machine with nothing cached is exactly that.
        # An unresolved default that has drifted still fails here.
        assert option.default_is_resolved and option.resolved_note, (
            f"{option_id}: --help says {cli_value!r}, the document says {option.default!r}"
        )
    defaults = document.defaults()
    # The one unit conversion in the whole document, and the reason `max_duration`
    # is described in the seconds the backend takes rather than in the minutes the
    # CLI flag spells: a surface that renders minutes has to convert on purpose.
    assert arguments.max_duration_minutes * 60 == defaults["max_duration"]


def test_document_round_trips_through_as_json() -> None:
    document = build_options_document()
    payload = document.as_json()
    assert payload["schema"] == OPTIONS_SCHEMA
    assert json.loads(json.dumps(payload)) == payload
    groups = payload["groups"]
    assert isinstance(groups, list) and groups
    assert [group["id"] for group in groups] == [group.id for group in document.groups]


def test_every_option_carries_a_reason_exactly_when_it_is_unavailable() -> None:
    for option in _options(build_options_document()):
        assert option.available == (option.unavailable_reason is None), option.id
        assert option.available or option.unavailable_reason
        for choice in option.choices:
            assert choice.available == (choice.unavailable_reason is None), choice.value


def test_describing_this_machine_writes_nothing(private_homes: Path) -> None:
    """Opening an intake screen must not create directories under the user's home."""

    cache = private_homes.parent / "cache"
    # Directly, first: `model_cache_path` is otherwise only reached from
    # inside `_auto_model`, behind `transcription.is_available()`, so on a machine
    # without the transcribe extra the assertions below would pass vacuously and a
    # stray `ensure_private_directory` would ride out of here unnoticed.
    assert not transcription.model_cache_path().exists(), "naming the cache made it"
    build_options_document()
    build_options_document(allow_model_downloads=True)
    assert not cache.exists(), "describing the machine created the model cache"
    assert not private_homes.exists(), "describing the machine created the project store"


# --- availability is measured, not asserted -------------------------------------


def test_a_missing_faster_whisper_reads_differently_from_missing_weights(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two failures, two fixes: `uv sync` and the download consent are not the same."""

    _strip_machine(monkeypatch, whisper=False)
    missing_extra = build_options_document().option("whisper_model")
    assert missing_extra is not None and not missing_extra.available
    assert missing_extra.unavailable_reason is not None
    assert "uv sync" in missing_extra.unavailable_reason

    # Nothing cached, but a download would fetch something. The control stays
    # live -- see `test_a_blocked_auto_still_offers_the_models_its_reason_points_at`
    # -- so this sentence belongs to the `auto` choice, not to the option.
    _strip_machine(monkeypatch, cached=None)
    option = build_options_document().option("whisper_model")
    assert option is not None and option.available
    no_weights = next(c for c in option.choices if c.value == transcription.AUTO_MODEL)
    assert not no_weights.available
    assert no_weights.unavailable_reason is not None
    assert "download" in no_weights.unavailable_reason

    # Different is not enough: each reason has to name its own fix and only its
    # own. A sentence carrying both sends a user with the extra installed off to
    # reinstall it, which is the wrong command dressed as a helpful one.
    assert "uv sync" not in no_weights.unavailable_reason
    assert "download" not in missing_extra.unavailable_reason


def test_permitting_downloads_reresolves_the_model_it_could_not_choose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consent toggle changes availability, not only choice -- so it re-resolves."""

    calls: list[bool] = []

    def _resolve(requested: str, *, allow_model_downloads: bool, **_: object) -> str:
        calls.append(allow_model_downloads)
        if not allow_model_downloads:
            raise ProviderUnavailableError("no suitable cached faster-whisper model")
        return "large-v3"

    _strip_machine(monkeypatch)
    monkeypatch.setattr(transcription, "resolve_model", _resolve)

    offline = build_options_document().option("whisper_model")
    assert offline is not None and offline.available
    offline_auto = next(c for c in offline.choices if c.value == transcription.AUTO_MODEL)
    assert not offline_auto.available

    online = build_options_document(allow_model_downloads=True)
    model = online.option("whisper_model")
    assert model is not None and model.available
    assert model.default == transcription.AUTO_MODEL
    assert model.default_is_resolved and "large-v3" in model.resolved_note
    assert "download" in model.resolved_note
    assert True in calls and False in calls, "the provider owns the policy; it must be asked"
    # The document stays self-consistent: it reports the answer it was resolved under.
    assert online.defaults()["allow_model_downloads"] is True


def test_an_unobtainable_model_is_a_reason_and_never_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _explode(*_: object, **__: object) -> str:
        raise ProviderUnavailableError("nothing here")

    _strip_machine(monkeypatch)
    monkeypatch.setattr(transcription, "resolve_model", _explode)
    document = build_options_document(allow_model_downloads=True)
    option = document.option("whisper_model")
    assert option is not None and not option.available
    assert option.unavailable_reason is not None and "doctor" in option.unavailable_reason
    assert not option.default_is_resolved and option.resolved_note == ""


def test_the_resolved_note_says_what_auto_means_here(monkeypatch: pytest.MonkeyPatch) -> None:
    def _resolve(requested: str, *, allow_model_downloads: bool, **_: object) -> str:
        return "large-v3" if allow_model_downloads else "small"

    _strip_machine(monkeypatch)
    monkeypatch.setattr(transcription, "resolve_model", _resolve)
    option = build_options_document().option("whisper_model")
    assert option is not None and option.available and option.default_is_resolved
    assert "small" in option.resolved_note and "large-v3" in option.resolved_note


def test_missing_demucs_greys_separation_and_moves_transcription_to_the_mix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The resolved default has to be one that runs, not one that reads well."""

    _strip_machine(monkeypatch, demucs=False)
    document = build_options_document()

    model = document.option("model")
    assert model is not None and not model.available
    assert model.unavailable_reason is not None and "Demucs" in model.unavailable_reason

    audio = document.option("audio_source")
    assert audio is not None and audio.available
    assert audio.default == transcription.AUDIO_SOURCE_MIX and audio.default_is_resolved
    stemless = {transcription.AUDIO_SOURCE_VOCALS, transcription.AUDIO_SOURCE_AUTO}
    assert {c.value for c in audio.choices if not c.available} == stemless

    _strip_machine(monkeypatch, demucs=True)
    with_demucs = build_options_document().option("audio_source")
    assert with_demucs is not None
    assert with_demucs.default == transcription.DEFAULT_AUDIO_SOURCE
    assert all(choice.available for choice in with_demucs.choices)


def test_missing_basic_pitch_greys_the_tab_options(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_machine(monkeypatch, guitar=False)
    document = build_options_document()
    for option_id in ("tuning", "max_fret"):
        option = document.option(option_id)
        assert option is not None and not option.available
        assert option.unavailable_reason is not None
        assert "Basic Pitch" in option.unavailable_reason


def test_missing_media_tools_grey_both_ways_of_giving_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _strip_machine(monkeypatch, tools=("ffprobe",))
    document = build_options_document()
    for option_id in ("url", "source_path"):
        option = document.option(option_id)
        assert option is not None and not option.available
        assert option.unavailable_reason is not None
        assert "ffprobe" in option.unavailable_reason


def test_missing_yt_dlp_greys_the_link_and_its_captions(monkeypatch: pytest.MonkeyPatch) -> None:
    _strip_machine(monkeypatch, modules=("ctranslate2",))
    document = build_options_document()

    url = document.option("url")
    assert url is not None and not url.available
    assert url.unavailable_reason is not None and "yt-dlp" in url.unavailable_reason

    source = document.option("lyrics_source")
    assert source is not None
    captions = next(choice for choice in source.choices if choice.value == "captions")
    assert not captions.available
    assert captions.unavailable_reason is not None and "yt-dlp" in captions.unavailable_reason
    # ...and the automatic choice survives, because it degrades rather than fails.
    assert next(c for c in source.choices if c.value == "auto").available


@pytest.mark.parametrize(
    ("modules", "cuda", "available", "resolved"),
    [
        (("ctranslate2",), False, False, True),
        (("ctranslate2",), True, True, True),
        ((), False, True, False),
    ],
)
def test_cuda_is_only_greyed_out_when_this_machine_can_answer(
    monkeypatch: pytest.MonkeyPatch,
    modules: tuple[str, ...],
    cuda: bool,
    available: bool,
    resolved: bool,
) -> None:
    """ "No CUDA" and "cannot tell" are different claims; only one greys the choice."""

    _strip_machine(monkeypatch, modules=modules)
    monkeypatch.setattr(transcription, "cuda_available", lambda: cuda)
    option = build_options_document().option("device")
    assert option is not None
    choice = next(c for c in option.choices if c.value == "cuda")
    assert choice.available is available
    assert option.default_is_resolved is resolved
    assert option.default == "auto"


def test_alignment_is_offered_because_it_ships_here_not_because_a_probe_said_so() -> None:
    """The one option whose default used to move on a probe of this same package.

    `align_supplied_text` was resolved from a `hasattr` lookup in `alignment`, and
    read `False` with a note saying no aligner was available here. There is no
    machine that answers that: `pipeline` does `from .alignment import align_lines`
    at module level, so an `alignment` without it cannot be imported, let alone
    described. What is left is a plain default, and the two things that would be
    lies about it are what this asserts -- it must not claim to have been resolved
    from this machine, and it must not be greyed.
    """

    option = build_options_document().option("align_supplied_text")
    assert option is not None
    assert option.default is True
    assert option.available and option.unavailable_reason is None
    assert not option.default_is_resolved and not option.resolved_note, (
        "a constant described as resolved from this machine is the claim that was removed"
    )
    # ...and the reason it is a constant, stated as the binding it rests on:
    # `pipeline` does `from .alignment import align_lines` at module level, so a
    # tree without it fails to import long before it can describe anything.
    assert vars(pipeline)["align_lines"] is alignment.align_lines, (
        "pipeline no longer binds align_lines at import time; the aligner is a probe again"
    )


# --- the pieces this module mirrors ---------------------------------------------


def test_every_english_only_model_says_so() -> None:
    """`distil-medium.en` is distilled *and* English-only; the second one ruins a song.

    The help used to return on the first matching branch, so the four distilled
    English-only models advertised their cost and never mentioned the language
    they cannot hear.
    """

    document = build_options_document()
    option = document.option("whisper_model")
    assert option is not None
    english_only = [c for c in option.choices if c.value.endswith(".en")]
    assert len(english_only) >= 4, "the .en models vanished; this test is measuring nothing"
    for choice in english_only:
        assert "English only" in choice.help, f"{choice.value} does not say it is English-only"
    for choice in option.choices:
        if choice.value.startswith("distil-"):
            assert "distilled" in choice.help, choice.value


def test_the_file_size_ceiling_on_the_screen_is_the_one_that_refuses() -> None:
    """A typed-in "512 MiB" is a sentence that goes stale silently; this one cannot.

    `source.inspect_file` refuses a larger file in `source.format_size`'s spelling,
    so the help quotes that call rather than a literal. Asserting the rendered
    substring is what makes raising `MAX_FILE_BYTES` a change to both at once, and
    it is also the second caller that keeps `format_size` from being a helper with
    one use and no check on its output.
    """

    option = build_options_document().option("source_path")
    assert option is not None
    assert source.format_size(source.MAX_FILE_BYTES) in option.help, (
        "the file arm's help does not name the size limit that refuses the file"
    )
    # The spelling is the provider's own, not a coincidence of this machine.
    assert source.format_size(source.MAX_FILE_BYTES) == "512M"


def test_the_model_list_is_ordered() -> None:
    """`SUPPORTED_MODELS` is a frozenset: unsorted, the screen reorders per process."""

    option = build_options_document().option("whisper_model")
    assert option is not None
    named = [c.value for c in option.choices if c.value != transcription.AUTO_MODEL]
    assert named == sorted(named)
    assert option.choices[0].value == transcription.AUTO_MODEL, "'auto' leads the list"


def test_named_tunings_match_the_cli() -> None:
    """One mapping now, not two -- so this checks the surface, not the copy.

    `cli` imports `TUNINGS` from here; that landed while this suite was being
    written, so comparing the two mappings would now be comparing an object with
    itself and would pass however either side drifted. What is still worth
    checking is what the command line does with the names: each one is accepted by
    `--tuning` and reaches `PipelineOptions.tuning` as this module's pitches, and a
    name that is not in this mapping is refused by argparse's own `choices` before
    it can reach the backend. (The one-way edge is unchanged and is still a rule:
    `cli` may import this module, this module may never import `cli`.)
    """

    parser = cli.build_parser()
    document = build_options_document()
    for name, pitches in TUNINGS.items():
        arguments = parser.parse_args(["create", VALID_URL, "--tuning", name])
        assert arguments.tuning == name
        assert cli._pipeline_options(arguments, document).tuning == pitches

    with pytest.raises(SystemExit):
        parser.parse_args(["create", VALID_URL, "--tuning", "open-g"])


def test_whisper_cache_matches_the_pipeline(
    private_homes: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The directory this module probes is the one weights actually land in.

    There used to be three spellings of the path -- this module's, the pipeline's
    and `transcribe`'s own -- and two thirds of this test was two literals being
    compared. `transcription.model_cache_path` is now the only spelling, so what
    is left to check is the half that was never a mirror: that `transcribe`
    really hands *that* function's answer to the worker. Were it to stop, this
    module would probe directory A and write "'auto' selects small here: the
    strongest model whose weights are cached" while the run read directory B and
    found nothing -- a probe stating an availability that is not true, which is
    the failure this file exists to prevent.

    So it is read out of the real call rather than assumed: `resolve_model`, the
    first thing `transcribe` hands the path to, is replaced with a capture that
    stops the run there, before any subprocess. What is captured is
    `transcribe`'s own `model_cache` local -- the single value that becomes both
    `--cache` and `HF_HOME` -- at the moment it is computed; a later edit that
    kept the local and passed something else to the worker would be outside this
    check.
    """

    captured: list[Path] = []

    def _capture(model: str, **keywords: Any) -> str:
        captured.append(Path(str(keywords["model_cache"])))
        raise ProviderUnavailableError("captured: this test wants the path, not a transcript")

    monkeypatch.setattr(transcription, "is_available", lambda: True)
    monkeypatch.setattr(transcription, "resolve_model", _capture)
    stem = tmp_path / "vocals.wav"
    stem.write_bytes(b"\0")
    with pytest.raises(ProviderUnavailableError):
        transcription.transcribe(stem, tmp_path / "lyrics.json")

    assert captured == [transcription.model_cache_path()], (
        "the directory the worker is given is not the one this module probes"
    )


def test_the_cuda_probe_this_module_reads_still_exists() -> None:
    """A rename in the provider must fail here, not silently grey out every GPU."""

    assert isinstance(transcription.cuda_available(), bool)


#: Candidate tunings, legal and not, spanning every clause of the backend's rule:
#: length, range, distinctness and order. Every one is a `tuple[int, ...]`, which
#: is what `PipelineOptions.tuning` is typed as, so both sides of
#: `test_tuning_pitches_accepts_exactly_what_the_pipeline_accepts` can be asked
#: about all of them without one of them meeting a `TypeError` instead of a
#: verdict.
_TUNING_CANDIDATES: tuple[tuple[int, ...], ...] = (
    (40, 45, 50, 55, 59, 64),
    (38, 45, 50, 55, 59, 64),
    (38, 45, 50, 55, 57, 62),
    (0, 1, 2, 3, 4, 127),
    (64, 59, 55, 50, 45, 40),
    (40, 45, 50, 55, 64, 59),
    (40, 40, 50, 55, 59, 64),
    (40, 45, 50, 55, 59, 59),
    (-1, 45, 50, 55, 59, 64),
    (40, 45, 50, 55, 59, 128),
    (40, 45, 50, 55, 59),
    (40, 45, 50, 55, 59, 64, 69),
    (),
)


def test_tuning_pitches_accepts_exactly_what_the_pipeline_accepts() -> None:
    """The rule is the backend's rule, so the two are run over one corpus.

    `tuning_pitches` returning a tuple is the documented signal that a value is
    legal -- the intake screen offers free six-pitch entry on the strength of it.
    A value it waves through and `create_project` then refuses is the "a form
    ships a value the backend rejects" bug this module exists to prevent, and it
    is what a drop tuning entered low-to-high used to be. The two now share a body
    -- `tuning_pitches` calls `pipeline.valid_tuning`, which is what
    `_validate_options` calls -- so this is no longer a mirror check. It is what
    holds *end to end*: through `_pipeline_options`, `PipelineOptions` and the
    validator's own ordering, none of which the shared predicate can speak for.

    "Exactly" is over *integer* tunings, which is what the corpus and
    `PipelineOptions.tuning` are typed as. Outside it the two do differ, in the
    safe direction and measured, not assumed: a six-float tuple is rejected here
    and accepted by `_validate_options`, and a tuple of strings is rejected here
    and reaches that validator as a `TypeError`. This check does not cover that.
    """

    for candidate in _TUNING_CANDIDATES:
        try:
            tuning_pitches(candidate)
        except InvalidInputError:
            registry_accepts = False
        else:
            registry_accepts = True

        try:
            _validate_options(
                _pipeline_options(build_options_document(), url=VALID_URL, tuning=candidate),
                require_rights=False,
            )
        except InvalidInputError:
            backend_accepts = False
        else:
            backend_accepts = True

        assert registry_accepts == backend_accepts, (
            f"{candidate!r}: the intake screen says {registry_accepts}, "
            f"the backend says {backend_accepts}"
        )


def test_tuning_pitches_accepts_names_and_pitches_and_nothing_else() -> None:
    assert tuning_pitches("dadgad") == (38, 45, 50, 55, 57, 62)
    assert tuning_pitches([40, 45, 50, 55, 59, 64]) == (40, 45, 50, 55, 59, 64)
    rejected: tuple[object, ...] = (
        "open-g",
        [40, 45, 50],
        [40, 45, 50, 55, 59, 900],
        # Six pitches in range, and still refused downstream: a drop tuning typed
        # high-to-low, and one with a doubled string.
        [64, 59, 55, 50, 45, 40],
        [40, 40, 50, 55, 59, 64],
        [40, 45, 50, 55, 59, True],
        True,
        None,
    )
    for value in rejected:
        with pytest.raises(InvalidInputError):
            tuning_pitches(value)


class _LazyTuning:
    """A `Sequence` that counts being read, so materialisation is observable.

    `range(10**10)` makes the same point and cannot be used to make it: against a
    `tuple(value)` that runs before the length check it allocates ten billion
    integers and the test process is killed before it can assert anything. This
    reports the same fact in seven elements.
    """

    def __init__(self, length: int) -> None:
        self.length = length
        self.reads = 0

    def __len__(self) -> int:
        return self.length

    def __getitem__(self, index: int) -> int:
        self.reads += 1
        return 40 + index

    def __iter__(self) -> Iterator[int]:
        for index in range(self.length):
            yield self[index]


Sequence.register(_LazyTuning)


def test_a_lazy_sequence_is_bounded_before_it_is_materialised() -> None:
    """A length check that runs after `tuple()` has already paid for the length.

    `range` and any other lazy `Sequence` reach here as a `Sequence`, so bounding
    the input means testing `len` first and not the length of what `tuple()`
    produced.
    """

    lazy = _LazyTuning(7)
    with pytest.raises(InvalidInputError):
        tuning_pitches(lazy)
    assert lazy.reads == 0, "the sequence was materialised before its length was checked"

    # A sequence of the right length is materialised, of course -- that is the
    # point of it -- so the bound is on the length alone.
    legal = _LazyTuning(6)
    assert tuning_pitches(legal) == (40, 41, 42, 43, 44, 45)
