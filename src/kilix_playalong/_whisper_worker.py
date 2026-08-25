"""Heavy faster-whisper worker; emits the app's stable timed-lyrics schema.

This process is spawned only by ``providers/transcription.py``, through
``runner.run_command``, with a fixed argv, an allowlisted environment and a
disposable HOME. Every decoding knob below is a constant in this file: there is
deliberately no way for a caller to hand this worker an arbitrary argument,
module, model path or environment variable, so "tuned for singing" cannot be
turned back into "tuned for dictation" from outside.

Whisper's decoding defaults were fitted to *speech*. This worker is given
*singing*, usually over instruments, and the two failures that matter there are
(a) inventing a verse over an instrumental section and (b) looping one phrase
for tens of seconds. Neither is caught by a single mechanism, so they are
attacked in three layers, cheapest first:

  1. decoding options that make the loop and the phantom less likely at all
     (``transcribe_options``);
  2. a Silero VAD configured for *instrumental gaps* rather than the sub-second
     pauses of conversation (``VAD_PARAMETERS``), so the eight-bar solo is never
     shown to the model in the first place; and
  3. a post-filter over the cues that survive anyway (``filter_cues``), which is
     the only layer that can see the finished transcript and therefore the only
     one that can recognise a thirty-second loop as a loop.

Every threshold is a named constant with the cost of getting it wrong written
next to it, because each one is a trade against real lyrics: filtering that is
too eager deletes the quiet bridge, and filtering that is too shy ships
"Thanks for watching!" as a lyric.

WHAT IS MEASURED, and what is argument only. Layer 3 is measured, on real
weights and real audio and twice independently: the tuned decoder still emits a
confidently-scored looping cue on loop-prone material, and ``filter_cues`` is
what stops it reaching the document. So the layering is observed, not merely
plausible. Layers 1 and 2 are argued from mechanism only: no run shows that
``no_speech_threshold``, the temperature ladder,
``hallucination_silence_threshold`` or the VAD retuning improves transcription
accuracy, cue segmentation or word alignment on sung audio, and no comment below
should be read as claiming otherwise. One consequence is known and unbounded: on
one such recording the combination shipped *zero* cues where the untuned decoder
shipped garbage -- better, and still evidence that it can empty a transcript of
audio that does contain singing. Nothing in the suite bounds how often that
happens.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

from . import LYRICS_SCHEMA
from .providers.transcription import (
    AUDIO_SOURCE_AUTO,
    AUDIO_SOURCE_CHOICES,
    AUDIO_SOURCE_MIX,
    AUDIO_SOURCE_VOCALS,
    DEFAULT_AUDIO_SOURCE,
    format_receipt,
)
from .text import fold_accents
from .util import private_write

#: What Whisper's feature extractor and the Silero VAD both expect. Decoding the
#: audio once here, at this rate, lets the language detector, the VAD and the
#: decoder share one array instead of each re-decoding the file.
SAMPLE_RATE = 16000

# --------------------------------------------------------------------------
# Decoding options.
# --------------------------------------------------------------------------

#: Beam width. 5 is faster-whisper's default and the point where quality stops
#: paying for itself; a wider beam mostly buys longer runs, a narrower one loses
#: the second-choice word that sung diction depends on.
BEAM_SIZE = 5

#: The temperature fallback ladder, spelled out rather than inherited. A window
#: whose decode trips one of the thresholds below is retried at the next
#: temperature. Ending at 1.0 matters for singing: a chorus that the greedy pass
#: renders as a repetition loop usually resolves at 0.6-1.0, and a ladder that
#: stops early leaves that window either looped or empty. The cost is wall-clock
#: -- a pathological window can be decoded six times.
TEMPERATURE_LADDER = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)

#: gzip compression ratio above which a window's text is treated as degenerate
#: and retried hotter. Left at faster-whisper's 2.4 *on purpose*: real choruses
#: compress well ("na na na na", a hook repeated four times), so tightening this
#: for music would send genuine, correctly-transcribed refrains around the
#: temperature ladder and eventually drop them. Loosening it lets longer loops
#: through instead. The repetition that this deliberately does not catch is
#: caught after the fact, by ``has_runaway_repetition``, which counts tokens
#: rather than measuring entropy and so can tell a hook from a loop.
COMPRESSION_RATIO_THRESHOLD = 2.4

#: Average token log-probability below which a window is treated as failed.
#: Left at -1.0. Sung audio is systematically less confident than speech, so
#: raising this to "tighten hallucination control" would discard exactly the
#: material we are here for -- quiet bridges, whispered lines, heavy vibrato --
#: while lowering it admits more phantom text. The singing-specific tightening
#: is done with the two knobs that do not scale with vocal difficulty: the
#: no-speech probability below, and the VAD.
LOG_PROB_THRESHOLD = -1.0

#: A window is dropped as silence when its no-speech probability exceeds this
#: *and* its average log-probability is below ``LOG_PROB_THRESHOLD``. Lowered
#: from faster-whisper's 0.6 to 0.5 because on music the "silence" being
#: mistaken for speech is an instrumental passage, which is where invented
#: verses come from; 0.5 drops a window the model is merely ambivalent about.
#: The cost is a real sung line that is both quiet and unconfident -- an
#: effect-drenched ad-lib, say -- which at 0.5 is discarded and at 0.6 kept.
NO_SPEECH_THRESHOLD = 0.5

#: With word timestamps on, faster-whisper re-seeks past a silent stretch longer
#: than this when the words it just produced look like a hallucination. Two
#: seconds is under a bar of rest at 90 BPM (2.7 s) and well over the pause
#: between sung phrases, so it fires on instrumental gaps and not inside a
#: verse. Lower and it re-seeks inside legato phrasing, losing trailing words;
#: higher and a short instrumental break keeps its invented line.
HALLUCINATION_SILENCE_THRESHOLD = 2.0

#: Left at the neutral 1.0 / 0 on purpose. Both of these suppress repeated
#: tokens during search, which is the correct fix for a dictation loop and the
#: wrong one for music: a song's chorus *is* an exact repeat, and penalising it
#: makes the model invent a variation rather than transcribe the refrain. Loop
#: suppression is done by ``condition_on_previous_text=False`` and the
#: post-filter, neither of which can rewrite a legitimate lyric.
REPETITION_PENALTY = 1.0
NO_REPEAT_NGRAM_SIZE = 0

# --------------------------------------------------------------------------
# Voice activity detection.
# --------------------------------------------------------------------------

#: Silero VAD settings, handed both to the decoder (so it is never shown
#: non-vocal audio) and to ``speech_spans`` (so the post-filter can ask where the
#: singing actually was), which is why they live in one dictionary.
#:
#: Three of the six are CHANGED from faster-whisper's defaults for music:
#: ``min_speech_duration_ms``, ``max_speech_duration_s``, ``min_silence_duration_ms``.
#: The other three are PINNED -- written out, but exactly what the library would
#: have used anyway. Pinning is worth doing because the rules in this file reason
#: about the numbers (the 30 s claim below is arithmetic on ``speech_pad_ms``, and
#: the post-filter's spans are only comparable to the decoder's while both sides
#: see one fixed set), and an unstated default can move under a version bump
#: without this file noticing. It is not tuning, and is not described as tuning.
#: ``test_the_vad_dictionary_pins_what_it_says_it_pins`` re-checks every pin
#: against the installed faster-whisper -- the two it declares by value directly,
#: and ``neg_threshold``, which it declares as None and derives, against the
#: derivation read out of the installed source and against the real VAD run with
#: and without the pin -- so a version bump that moves one of these fails there.
VAD_PARAMETERS: dict[str, float] = {
    # PINNED at Silero's own default. Sung vocals over a band score lower than
    # clean speech, so raising this clips line ends and drops quiet lines;
    # lowering it promotes instrumental transients -- a cymbal, a bent string --
    # to "speech" and hands the decoder the exact audio that phantom verses come
    # from. No reason was found to move it, in either direction.
    "threshold": 0.5,
    # PINNED, and computed rather than declared upstream: faster-whisper leaves
    # this None and then uses ``max(threshold - 0.15, 0.01)``, which is this
    # number. Stating it changes no behaviour; it is here because the hysteresis
    # matters to the rules below -- once speech has started it continues until
    # confidence falls below this, which is what stops a held note whose
    # confidence dips mid-word from being cut in two -- and a value that only
    # exists as an upstream fallback can change without this file noticing.
    "neg_threshold": 0.35,
    # CHANGED from 0. Speech islands shorter than this are discarded. A
    # quarter-second floor removes isolated percussive hits and breaths that
    # survive the threshold. The cost is a genuine one-syllable interjection
    # ("Hey!") shorter than 250 ms, which is dropped with them.
    "min_speech_duration_ms": 250,
    # CHANGED from inf. No VAD chunk may exceed Whisper's own 30 s decode
    # window, so a chunk can never straddle two windows. Rarely binding once the
    # silence rule below splits on rests; when it does bind it splits at the last
    # pause it can find.
    "max_speech_duration_s": 30.0,
    # CHANGED from 2000, and the most music-specific of the three. This is how
    # long non-speech must last before a vocal chunk is closed, i.e. how much
    # instrumental audio gets absorbed into a chunk rather than excluded from it.
    # faster-whisper's 2000 ms is a conversational turn; in a song it swallows a
    # whole bar of rest and feeds it to the decoder. 700 ms sits above a
    # within-phrase breath (200-400 ms) and below a bar of rest at 90 BPM
    # (2.7 s). Lower and a sung line with a rest in it is split, losing context
    # across the split; higher and the instrumental gap comes back.
    "min_silence_duration_ms": 700,
    # PINNED at faster-whisper's default. Padding kept on each side of a chunk.
    # Sung onsets have a slower attack than spoken ones, so trimming this would
    # clip the first consonant of a line; enlarging it would re-admit the
    # instrumental audio the rule above just removed. Both of those are reasons
    # to leave the default alone, not evidence that 400 ms was chosen for music.
    "speech_pad_ms": 400,
}

# --------------------------------------------------------------------------
# Post-filter.
# --------------------------------------------------------------------------

#: The repetition bound *inside* one cue, counted over repeated phrases rather
#: than repeated words. A word-level rule is not enough and this was measured,
#: not assumed: on a deliberately loop-prone recording the tuned decoder emitted
#: one 23-second cue reading "hold on, hold on, ..." seventy-five times over,
#: in which no token ever repeats twice in a row and no single token exceeds
#: half the cue. A phrase-aware rule sees that; a token-aware one does not.
#:
#: Sixteen is the honest number, and it is a trade that cannot be won outright.
#: A sung chant really can repeat ("na na na na ...", "hold on, hold on ..."),
#: so any bound deletes some genuine chant; a decoder loop, by contrast, fills
#: the entire decode window and repeats *hundreds* of times, so any bound in the
#: tens catches it. Sixteen keeps every chant a person is likely to have written
#: down and drops what no one sang. What it costs when it is wrong: a genuine
#: seventeen-times chant loses that cue -- a low-information line -- and a
#: thirteen-times loop ships as lyrics.
#:
#: Both sides of that sentence are under test, at the boundary rather than near
#: it: ``test_a_chant_is_kept_to_exactly_the_declared_bound`` keeps a sixteen-fold
#: chant and drops the seventeen-fold one, so the constant cannot drift in either
#: direction with the suite green.
MAX_PHRASE_REPEATS = 16

#: Longest phrase, in tokens, checked for repetition. One covers "na na na",
#: two covers "hold on, hold on", and four reaches a short sung line. Beyond
#: four a repeated block is a repeated *lyric*, which the cross-cue rule below
#: is the right place to judge, and the scan cost grows with no benefit.
MAX_PHRASE_TOKENS = 4

#: How many consecutive cues may carry identical text before the rest of the run
#: is dropped. This is the same failure seen from above: the decoder emits the
#: same line as segment after segment rather than inside one segment. At typical
#: segment lengths twelve identical consecutive cues is one to two minutes of a
#: single unvarying line, which is a loop; the *first* twelve are kept, so a
#: song that really does repeat a line thirteen times loses only the thirteenth
#: onwards rather than the whole refrain.
MAX_IDENTICAL_CUE_RUN = 12

#: Cues whose entire text is one of these are Whisper's stock phantoms, learned
#: from subtitle training data and emitted over music with high confidence. The
#: match is exact on folded text (case, accents, punctuation and spacing
#: removed) rather than a substring, because "bye", "you" and "thank you" are
#: all plausible lyrics and a substring rule would delete them. That
#: conservatism is the cost: a phantom with an unusual suffix survives here and
#: has to be caught by the VAD or the repetition rules instead.
_PHANTOM_PHRASES = (
    "Thanks for watching!",
    "Thanks for watching",
    "Thank you for watching!",
    "Thank you for watching.",
    "Thanks for watching, and I'll see you in the next video.",
    "Please subscribe to my channel",
    "Subscribe to my channel",
    "Don't forget to subscribe",
    "Like and subscribe",
    "Please like and subscribe",
    "Subtitles by the Amara.org community",
    "Sous-titres réalisés par la communauté d'Amara.org",
    "Sous-titrage Société Radio-Canada",
    "Untertitelung aufgrund der Amara.org-Community",
    "Subtítulos realizados por la comunidad de Amara.org",
    "Legendas pela comunidade Amara.org",
    "字幕由Amara.org社区提供",
    "ご視聴ありがとうございました",
    "ご清聴ありがとうございました",
    "시청해주셔서 감사합니다",
    "Продолжение следует...",
    "Transcription by ESO Translation by —",
    "www.mooji.org",
)

#: The one substring rule, and the reason it is safe: the caption-farm domain
#: cannot occur in a sung lyric, and it appears in a dozen localised phantoms
#: that are not worth enumerating one by one.
_PHANTOM_SUBSTRINGS = ("amara org",)

# --------------------------------------------------------------------------
# Audio-source selection (``--audio-source auto``).
# --------------------------------------------------------------------------

#: Score floor for a candidate that transcribed nothing, so comparisons never
#: see a NaN or an empty mean. Well below any real average log-probability.
_SCORE_FLOOR = -10.0

#: A candidate that covers less than this fraction of the other candidate's
#: sung duration loses regardless of confidence. Average log-probability is
#: coverage-blind: a pass that finds four confident lines and misses the rest of
#: the song outscores a pass that transcribes all of it slightly less certainly,
#: and the four-line transcript is plainly the worse product. 0.6 tolerates the
#: normal disagreement between a stem and a full mix about where singing starts
#: and stops, while rejecting a pass that only found a fragment.
MIN_RELATIVE_COVERAGE = 0.6

#: How much better, in average log-probability per token, the full mix must be
#: before it displaces the vocal stem. Ties go to the stem: it is the
#: deliberate default, and flipping the audio source on noise would make the
#: same job produce different lyrics on consecutive runs. 0.05 nats is small
#: next to the gap a genuinely better source produces (typically 0.2 or more)
#: and large next to run-to-run jitter, which is nil here because decoding is
#: deterministic at temperature 0 -- the margin guards against a *real but
#: meaningless* difference, not a random one.
SOURCE_SCORE_MARGIN = 0.05

# --------------------------------------------------------------------------
# Language.
# --------------------------------------------------------------------------

#: How many 30-second windows *of detected speech* the language detector may
#: look at. faster-whisper's default of 1 means "decide from the first 30
#: seconds", which for a song is often an instrumental intro or a wordless hook,
#: and a language decided from that is a coin toss that then mis-transcribes the
#: whole track. Four windows is two minutes of actual singing. The cost is up to
#: three extra encoder passes, which is negligible beside decoding.
LANGUAGE_DETECTION_SEGMENTS = 4

#: Detection stops early at the first window this confident, so the four-window
#: budget is only spent on genuinely ambiguous material. Left at
#: faster-whisper's default; raising it buys little and costs the extra passes
#: on every song.
LANGUAGE_DETECTION_THRESHOLD = 0.5

#: What ``fold`` collapses to spaces once ``text.fold_accents`` has respelled the
#: apostrophes. The order matters and is the whole reason that call comes first:
#: ``\w`` calls U+02BC MODIFIER LETTER APOSTROPHE a word character, being category
#: Lm, so stripping punctuation before respelling leaves "don\u02bct" one token
#: while every other apostrophe spelling becomes two. That was not cosmetic --
#: "Don\u02bct forget to subscribe" matched nothing in ``PHANTOM_PHRASES`` and
#: shipped a caption-farm hallucination as a lyric under the one spelling in six
#: the filter could not see.
_PUNCTUATION = re.compile(r"[^\w\s]")


@dataclass(frozen=True)
class RawCue:
    """One decoded segment, before the post-filter and before serialisation."""

    start: float
    end: float
    text: str
    words: tuple[tuple[float, float, str], ...]
    avg_logprob: float

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def as_json(self) -> dict[str, object]:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "text": self.text,
            "words": [
                {"start": round(start, 3), "end": round(end, 3), "text": text}
                for start, end, text in self.words
            ],
        }


@dataclass(frozen=True)
class Attempt:
    """One completed transcription pass over one audio source."""

    audio_source: str
    cues: tuple[RawCue, ...]
    language: str
    language_from: str
    language_confidence: float

    @property
    def covered(self) -> float:
        """Seconds of audio the surviving cues actually cover."""
        return sum(cue.duration for cue in self.cues)

    @property
    def score(self) -> float:
        """Average segment log-probability, weighted by covered duration.

        The stated criterion for ``--audio-source auto``. Weighting by duration
        rather than by cue count stops a spray of short, confident fragments
        from outranking a long, well-transcribed verse; ``MIN_RELATIVE_COVERAGE``
        then handles the part that no per-token average can see, namely that one
        candidate may have skipped half the song.
        """
        covered = self.covered
        if covered <= 0:
            return _SCORE_FLOOR
        weighted = sum(cue.avg_logprob * cue.duration for cue in self.cues)
        return weighted / covered


def _finite(value: object, default: float) -> float:
    """Coerce a provider-supplied number to a usable float.

    Segment fields come from a third-party library across a version boundary;
    None and -inf both appear in the wild for ``avg_logprob``, and either one
    poisons every comparison downstream if it is taken at face value.

    ``bool`` is refused before the conversion rather than caught after it,
    because it is the only type that converts *silently*: everything else a
    segment field could hold either is a number or raises, while ``float(True)``
    is 1.0 -- an impossible log-probability that would make one cue look
    maximally confident and could win an ``auto`` comparison outright.
    ``alignment._finite`` refuses it for the same reason, so the two modules
    guarding the same kind of boundary now give the same answer.
    """
    if isinstance(value, bool):
        return default
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return default
    return number if math.isfinite(number) else default


def fold(text: str) -> str:
    """Normalise text for comparison: case, accents, punctuation and spacing.

    Applied to both sides of every text comparison in this module, so the
    phantom list can be written the way a person would read it.

    Case, accents and apostrophe spelling are ``text.fold_accents``, which is also
    the head of ``alignment.comparison_tokens``. Sharing it is what makes this
    filter and the aligner demonstrably normalise on one basis rather than on two
    that happen to look alike -- they did not, before: the two modules kept
    separate apostrophe classes and this one was missing U+02BC.

    The tail is deliberately *not* ``comparison_tokens``, which goes on to expand
    digits and contractions ("1985" -> "nineteen eighty five", "ain't" -> "is
    not"). That expansion is right for matching a lyric sheet against a
    transcript of it and wrong here, because ``has_runaway_repetition`` counts a
    repeating phrase's *period* in these tokens: expanding a unit like "1985
    ain't" from three tokens to five pushes it past ``MAX_PHRASE_TOKENS``, and a
    forty-fold loop of it then reads as no loop at all. Measured on exactly that
    input -- ``fold`` and ``MAX_PHRASE_TOKENS`` are tuned as a pair, and folding
    the aligner's way would quietly weaken the only measured layer of the three.
    """
    return " ".join(_PUNCTUATION.sub(" ", fold_accents(text)).split())


PHANTOM_PHRASES = frozenset(fold(phrase) for phrase in _PHANTOM_PHRASES)


def is_phantom_phrase(text: str) -> bool:
    """True when a cue is one of Whisper's stock caption-farm hallucinations."""
    folded = fold(text)
    if not folded:
        return True
    if folded in PHANTOM_PHRASES:
        return True
    return any(needle in folded for needle in _PHANTOM_SUBSTRINGS)


def max_phrase_repeats(tokens: Sequence[str], size: int) -> int:
    """Longest run of one ``size``-token phrase repeating back to back.

    Counted by period: a stretch where every token equals the token ``size``
    places earlier is, by definition, that phrase repeating. Linear in the cue
    for each size, which matters because a runaway cue is exactly the long one.
    """
    if size <= 0 or len(tokens) <= size:
        return 1
    longest = 0
    run = 0
    for index in range(len(tokens) - size):
        run = run + 1 if tokens[index] == tokens[index + size] else 0
        longest = max(longest, run)
    return longest // size + 1


def has_runaway_repetition(text: str) -> bool:
    """True when a cue repeats a phrase past what a lyric plausibly would."""
    tokens = fold(text).split()
    if not tokens:
        return False
    return any(
        max_phrase_repeats(tokens, size) > MAX_PHRASE_REPEATS
        for size in range(1, MAX_PHRASE_TOKENS + 1)
    )


def overlaps_speech(start: float, end: float, spans: Sequence[tuple[float, float]]) -> bool:
    """True when a cue shares any time at all with a detected speech span."""
    return any(min(end, span_end) > max(start, span_start) for span_start, span_end in spans)


def _drop_repeated_runs(cues: Sequence[RawCue]) -> list[RawCue]:
    """Trim a run of identical consecutive cues to ``MAX_IDENTICAL_CUE_RUN``."""
    kept: list[RawCue] = []
    run_text = ""
    run_length = 0
    for cue in cues:
        folded = fold(cue.text)
        run_length = run_length + 1 if folded == run_text else 1
        run_text = folded
        if run_length <= MAX_IDENTICAL_CUE_RUN:
            kept.append(cue)
    return kept


def filter_cues(cues: Sequence[RawCue], spans: Sequence[tuple[float, float]]) -> list[RawCue]:
    """Drop the cues that are artefacts rather than lyrics.

    An empty ``spans`` disables the speech-overlap rule rather than deleting
    every cue. The two ways to get there are opposite in meaning -- a genuinely
    instrumental track, where the decoder was handed nothing and produced
    nothing anyway, and a VAD that failed to load -- and this module cannot tell
    them apart, so it declines to throw a whole transcript away on the guess.
    """
    kept: list[RawCue] = []
    for cue in cues:
        if is_phantom_phrase(cue.text):
            continue
        if has_runaway_repetition(cue.text):
            continue
        if spans and not overlaps_speech(cue.start, cue.end, spans):
            continue
        kept.append(cue)
    return _drop_repeated_runs(kept)


def choose_attempt(attempts: Sequence[Attempt]) -> tuple[Attempt, str]:
    """Pick the transcription pass to keep, and say why.

    ``attempts[0]`` is the configured primary -- the vocal stem when both were
    run -- and wins every tie, so the choice is deterministic and the default
    audio source is never displaced by noise. The rules, in order:

    * a candidate with no surviving cues loses ("empty");
    * a candidate covering less than ``MIN_RELATIVE_COVERAGE`` of the other's
      sung duration loses whatever its confidence ("coverage");
    * otherwise the higher weighted average log-probability wins, and only by
      more than ``SOURCE_SCORE_MARGIN`` ("score"); within the margin the primary
      keeps it ("tie").
    """
    primary = attempts[0]
    if len(attempts) == 1:
        return primary, "single"
    challenger = attempts[1]
    if not challenger.cues:
        return primary, "empty"
    if not primary.cues:
        return challenger, "empty"
    if challenger.covered < MIN_RELATIVE_COVERAGE * primary.covered:
        return primary, "coverage"
    if primary.covered < MIN_RELATIVE_COVERAGE * challenger.covered:
        return challenger, "coverage"
    if challenger.score > primary.score + SOURCE_SCORE_MARGIN:
        return challenger, "score"
    if primary.score > challenger.score + SOURCE_SCORE_MARGIN:
        return primary, "score"
    return primary, "tie"


def transcribe_options(language: str, vad_options: object) -> dict[str, object]:
    """The exact keyword arguments handed to ``WhisperModel.transcribe``.

    ``task`` is pinned to "transcribe" and ``language`` is always a concrete
    tag. Both matter for a product that promised lyrics: left to itself Whisper
    will translate a foreign-language song into English and report success, and
    it will also change its mind about the language partway through a track.

    ``condition_on_previous_text=False`` is the single most effective line here.
    Feeding each window the previous window's text is what turns one bad
    transcription into thirty seconds of the same phrase, because the loop
    conditions on itself. Switching it off costs cross-line context -- a
    pronoun or a proper noun that spans a window boundary is slightly more
    likely to be wrong -- and buys the elimination of the failure mode that
    makes a transcript unusable rather than merely imperfect.
    """
    return {
        "language": language,
        "task": "transcribe",
        "beam_size": BEAM_SIZE,
        "temperature": TEMPERATURE_LADDER,
        "condition_on_previous_text": False,
        "compression_ratio_threshold": COMPRESSION_RATIO_THRESHOLD,
        "log_prob_threshold": LOG_PROB_THRESHOLD,
        "no_speech_threshold": NO_SPEECH_THRESHOLD,
        "hallucination_silence_threshold": HALLUCINATION_SILENCE_THRESHOLD,
        "repetition_penalty": REPETITION_PENALTY,
        "no_repeat_ngram_size": NO_REPEAT_NGRAM_SIZE,
        # Never seed the decoder. An initial prompt is a documented amplifier of
        # exactly the hallucinations above, and it is also the one place a
        # caller-supplied string could reach the model.
        "initial_prompt": None,
        "prefix": None,
        "vad_filter": True,
        "vad_parameters": vad_options,
        "word_timestamps": True,
    }


def speech_spans(
    vad: ModuleType, audio: object, vad_options: object
) -> tuple[tuple[float, float], ...]:
    """Return detected speech spans in seconds.

    Run with the same options the decoder is given, over the same decoded array,
    so these are the spans the decoder actually saw and the post-filter is not
    judging cues against a different VAD than produced them.
    """
    spans: list[tuple[float, float]] = []
    for chunk in vad.get_speech_timestamps(audio, vad_options):
        start = _finite(chunk["start"], 0.0) / SAMPLE_RATE
        end = _finite(chunk["end"], 0.0) / SAMPLE_RATE
        if end > start:
            spans.append((start, end))
    return tuple(spans)


def _resolve_language(
    model: object,
    audio: object,
    vad_options: object,
    requested: str | None,
    multilingual: bool,
) -> tuple[str, str, float]:
    """Return (language, how it was decided, confidence in that language).

    Detection is run even when the caller pinned a language, and the confidence
    recorded is the detector's probability for the language actually used. A
    pinned tag the audio does not support therefore shows up in the receipt as a
    low confidence instead of vanishing, which is the difference between a
    debuggable transcript and a mysterious one.
    """
    if not multilingual:
        # An English-only model has nothing to detect; saying so is honest,
        # where reporting 1.00 confidence from a detector that never ran is not.
        return "en", "model", 1.0
    detected, probability, all_probabilities = model.detect_language(  # type: ignore[attr-defined]
        audio=audio,
        vad_filter=True,
        vad_parameters=vad_options,
        language_detection_segments=LANGUAGE_DETECTION_SEGMENTS,
        language_detection_threshold=LANGUAGE_DETECTION_THRESHOLD,
    )
    detected = str(detected)
    confidence = _finite(probability, 0.0)
    if requested is None:
        return detected, "detected", confidence
    if requested == detected:
        return requested, "requested", confidence
    scores = {str(name): _finite(value, 0.0) for name, value in all_probabilities}
    return requested, "requested", scores.get(requested, 0.0)


def _decode(model: object, audio: object, language: str, vad_options: object) -> list[RawCue]:
    """Run one decoding pass and collect its cues."""
    segments, _info = model.transcribe(  # type: ignore[attr-defined]
        audio, **transcribe_options(language, vad_options)
    )
    cues: list[RawCue] = []
    for segment in segments:
        text = str(segment.text).strip()
        if not text:
            continue
        start = _finite(segment.start, 0.0)
        end = _finite(segment.end, start)
        words = tuple(
            (
                _finite(word.start, start),
                _finite(word.end, end),
                str(word.word).strip(),
            )
            for word in (segment.words or [])
            if str(word.word).strip()
        )
        cues.append(
            RawCue(
                start=start,
                end=end,
                text=text,
                words=words,
                # A window whose confidence is missing or infinite is treated as
                # barely acceptable rather than as excellent, so a broken field
                # cannot win an ``auto`` comparison.
                avg_logprob=_finite(segment.avg_logprob, LOG_PROB_THRESHOLD),
            )
        )
    return cues


def _audio_plan(arguments: argparse.Namespace) -> tuple[tuple[str, Path], ...]:
    """Which sources to transcribe, primary first."""
    if arguments.audio_source == AUDIO_SOURCE_VOCALS:
        return ((AUDIO_SOURCE_VOCALS, arguments.source),)
    if arguments.audio_source == AUDIO_SOURCE_MIX:
        return ((AUDIO_SOURCE_MIX, arguments.mix),)
    return ((AUDIO_SOURCE_VOCALS, arguments.source), (AUDIO_SOURCE_MIX, arguments.mix))


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    # Kept immediately after the two positionals: the provider builds this argv
    # and the pipeline's tests locate the output path relative to `--model`.
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--language")
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument(
        "--audio-source", default=DEFAULT_AUDIO_SOURCE, choices=AUDIO_SOURCE_CHOICES
    )
    parser.add_argument("--mix", type=Path)
    arguments = parser.parse_args(argv)
    if arguments.audio_source != AUDIO_SOURCE_VOCALS and arguments.mix is None:
        parser.error(f"--audio-source {arguments.audio_source} requires --mix")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parse_arguments(argv)

    faster_whisper = importlib.import_module("faster_whisper")
    # The VAD helpers are not re-exported from the package root, and this worker
    # deliberately imports faster-whisper lazily so that the module itself stays
    # importable -- and unit-testable -- on a machine with no transcribe extra.
    vad = importlib.import_module("faster_whisper.vad")
    vad_options = vad.VadOptions(**VAD_PARAMETERS)

    compute_type = "int8" if arguments.device == "cpu" else "default"
    model = faster_whisper.WhisperModel(
        arguments.model,
        device=arguments.device,
        compute_type=compute_type,
        download_root=str(arguments.cache),
    )
    supported = tuple(str(item) for item in model.supported_languages)
    multilingual = len(supported) > 1
    requested: str | None = arguments.language or None
    if requested is not None and requested not in supported:
        # The provider already checked the tag's shape; only the model knows
        # whether it can do that language, and an `.en` model cannot do any.
        print(
            f"model {arguments.model} does not support language {requested}",
            file=sys.stderr,
        )
        return 2

    attempts: list[Attempt] = []
    for label, path in _audio_plan(arguments):
        audio = faster_whisper.decode_audio(str(path), sampling_rate=SAMPLE_RATE)
        spans = speech_spans(vad, audio, vad_options)
        language, language_from, confidence = _resolve_language(
            model, audio, vad_options, requested, multilingual
        )
        cues = filter_cues(_decode(model, audio, language, vad_options), spans)
        attempts.append(
            Attempt(
                audio_source=label,
                cues=tuple(cues),
                language=language,
                language_from=language_from,
                language_confidence=confidence,
            )
        )

    winner, reason = choose_attempt(attempts)
    audio_from = f"auto:{reason}" if arguments.audio_source == AUDIO_SOURCE_AUTO else "requested"
    document = {
        "schema": LYRICS_SCHEMA,
        "source": format_receipt(
            model=arguments.model,
            audio_source=winner.audio_source,
            audio_from=audio_from,
            language=winner.language,
            language_from=winner.language_from,
            language_confidence=winner.language_confidence,
        ),
        "language": winner.language,
        "cues": [cue.as_json() for cue in winner.cues],
    }
    # Written 0600 from the first byte rather than chmod-ed afterwards: this is
    # the user's lyrics, and the window between create and chmod is a window in
    # which they are world-readable.
    private_write(
        arguments.output,
        (json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
