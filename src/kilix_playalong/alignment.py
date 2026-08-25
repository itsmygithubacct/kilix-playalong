"""Forced alignment: the user's own lyric sheet, timed by a transcript of the song.

The case this exists for is the common one. The user already has the right
words -- album sleeve, lyrics site, their own typing -- and only the timing is
missing. Spreading those lines evenly across the duration, which is all an
untimed import can do unaided, is wrong by ten seconds before the first chorus,
and a play-along surface that is ten seconds out is worse than no lyrics at all.

So: transcribe the audio for its *timings*, keep the user's words, and transfer
one onto the other. This module is the transfer, and nothing else. It runs no
model, opens no file and knows nothing about a project: text in, cues out. That
is what makes every failure below reproducible in a unit test.

Two entry points carry it, and they are the two the pipeline imports:
``align_lines``, a sheet and a word-timed transcript in, and
``hypothesis_from_cues``, which turns a line-timed caption track into a
transcript good enough to be one. ``align_reference_text`` is ``align_lines``
over a whole document rather than a list of lines; nothing in the package calls
it and the tests do.

Scoring scheme
--------------
Alignment is a global Needleman-Wunsch over normalised tokens with a linear gap
penalty. Both knobs are stated as what they *mean* rather than as tuned numbers:

* ``MATCH_THRESHOLD`` is the similarity at which pairing two tokens is exactly
  as good as leaving both unpaired. Above it the alignment pairs them, below it
  it prefers two gaps. That single comparison is the whole decision, because
  with a linear gap penalty a pair's contribution is ``score(a, b) - 2 * GAP``.
* ``SIMILARITY_SLOPE`` is how strongly the alignment prefers the better of two
  competing pairings; it is set so that an identical pair scores exactly +1.0.

Partial credit matters because a transcriber *mishears* far more often than it
skips: "shining" comes back as "shinin'", "seven" as "heaven". Similarity is
``0.75 * (1 - levenshtein / longest) + 0.25 * (shared prefix over the combined
length)``, which keeps morphological variants together ("shining"/"shine"
scores 0.60) and genuinely different words apart ("shine"/"sign" scores 0.36).
A pair below the threshold is never taken: transferring a timing from a word
that is not the same word is worse than admitting the word was not measured.

Cost
----
Memory is O(R x H) in reference and hypothesis tokens: one float row pair and
one traceback byte per cell. Both sides are capped at ``MAX_TOKENS`` and their
product at ``MAX_ALIGNMENT_CELLS`` (a 6 MB traceback), and an input over either
cap raises rather than being truncated -- a silently truncated hypothesis
misaligns the entire tail of the song, which is precisely the failure the user
would not be able to see.

Time is *not* bounded by those two caps. Filling a cell is cheap; measuring how
alike the two tokens behind it are is not, and that measurement is the term that
runs away: under the memory cap alone, 3000 x 1900 all-distinct nine-letter
tokens ask for 5,700,000 scored pairs over 461,700,000 characters. So the
comparison work has its own cap, ``MAX_COMPARISON_CELLS`` -- and what that cap
holds is an *estimate of the clock*, not a count of anything the alignment does.

A pair costs a call before it costs a character. Scoring one is a dict lookup,
three integer bounds, a shared-prefix walk and a bit-parallel pass over the
hypothesis token, and only the last of those gets shorter as the tokens do.
Charging a pair the product of its two tokens' lengths -- which is all this
counted before -- prices the pass and gives the call away, so the shorter the
tokens the more of the real cost goes uncharged. Per pair, over vocabularies
where nothing prunes and every pair is measured and taken, the one DP cell each
pair's own column costs included:

    characters   1     2     3     4     5     6     8     9    12    16    24    32
    us a pair  2.62  4.49  5.22  6.93  8.00  8.62  9.68 11.13 14.28 18.40 26.79 34.42

Between two characters and nine the charge grows twentyfold and the clock two
and a half times. That is why the ceiling used to be set by *short* tokens over
an alphabet the letter mask cannot separate rather than by long anagrams, and
why lengthening the tokens could never reach it. Each scored pair is now charged
``COMPARISON_PAIR_CELLS`` -- fifty cells -- before either token's length is
counted at all. Fifty is calibrated, not derived: no one number makes those two
curves parallel, and this is the one that leaves the worst disagreement over the
whole shape space as small as it gets. Fitted to the table above, anything
from about thirty-six to sixty-four gives the same ceiling to within a few per
cent, and fifty is the middle of that range.

The estimate is also worked out before any of the work is done. ``_comparison_cost``
prices exactly the rows the fill will build -- one per distinct reference token,
plus one for every repeat the row cache had no room to keep -- so an input over
the cap is refused for the price of walking the two token lists. It used to be
counted as the rows were built, which meant an input a hair over the cap was
ground through to within one row of the whole cap's worth of work before being
turned down: the refusal cost what the cap existed to prevent.

What the estimate is worth in seconds. At the cap, the worst shape a sweep over
token length, alphabet size and both dimensions found runs at 0.10 us an
estimated cell. An ordinary vocabulary runs at a fifteenth of that, because the
letter mask throws most of its pairs out before anything is measured: 1000 x 1000
distinct five-character tokens drawn at random from a-z are estimated at exactly
75,000,000 cells and align in **0.78 s**. The estimate charges what a pair could
cost, never what it will, and that gap is the price of deciding before the work
instead of during it.

Where the number came from. The budget is ten seconds of wall clock for the
whole alignment -- the point where a wait on a screen with nothing to cancel
stops reading as work and starts reading as a hang, and this stage runs on an
intake path with the user watching. The DP fill takes up to **1.6 s** of that at
the memory cap (6,000,000 cells at 0.26 us), leaving 8.4 s for the scoring. A
sweep at a trial cap of 90,000,000 measured its worst shape at **11.56 s**,
which is 0.111 us an estimated cell above the fill; 8.4 s at that rate is
75,000,000 cells, and that is where the cap sits.

What that buys, measured. The sweep repeated at the cap tops out at **9.14 s**:
five-character reference tokens against ten-character hypothesis ones sharing a
four-character prefix, 750,000 pairs scored and 6,000,000 cells filled, with
nothing anywhere that any bound can prune. The plateau around it is broad and
flat -- 9.03 s at four against six, 8.97 s at four against eight, 8.10 s at five
against five, 7.12 s at two against two, 4.35 s at one against one, 3.65 s at
thirty-two against thirty-two. The same sweep run under the old character count
and its 40,000,000 cap measured **27.58 s** at its worst, on two-character
reference tokens
against three-character hypothesis ones: six million pairs for 36,000,000
counted cells, comfortably inside that cap. So this machine's ceiling is now a
third of what it was, and it is now set by a shape near the middle of the length
range rather than by the shortest tokens available.

What the cap admits. Real lyrics repeat, and repetition is free in this estimate
on both sides, so real material sits well inside it. Synthetic sheets drawn from
a dictionary with a transcript mishearing one word in six, measured end to end
through ``align_lines``:

* a 3000-token sheet, 699 distinct tokens averaging 5.15 characters, against a
  2000-token transcript -- twenty minutes of dense, barely repeating singing:
  13,659,980 characters and 513,499 pairs, 39,334,930 estimated cells, **1.80 s**.
* the densest long song the size caps leave room for -- a 2500-token sheet whose
  900 distinct tokens average 5.17 characters, against a 2400-token transcript:
  20,349,492 characters and 763,020 pairs, 58,500,492 estimated cells,
  **2.03 s**. This is the shape the cap is drawn around, and it sits at 78% of
  it.
* the same two sheets in dictionary-length words rather than sung ones (6.6
  characters a token, which is prose): 49,207,822 and 72,714,208 estimated cells,
  **1.99 s** and **2.09 s**. Both still inside.

The estimate nearly doubles across those four and the clock moves by under three
tenths of a second, which is the letter mask doing its work -- and is why the cap
has to be drawn against the worst vocabulary at a given estimate rather than
against these.

What the pair charge refuses that the character count admitted:

* the class it was aimed at, short tokens in bulk. 1500 distinct three-character
  reference tokens against 1000 distinct ones is 13,500,000 characters -- under
  a fifth of the cap -- and 1,500,000 pairs, so 88,500,000 estimated cells, and
  it is refused. Over an alphabet the mask cannot separate that shape measures
  **7.61 s**; drawn at random from a-z the same shape measures **1.25 s**,
  because the mask prunes it. Telling those two apart means comparing every
  pair's masks, which is most of what the cheap one costs in the first place --
  1.25 s of the 7.61 s -- so an estimate taken before the work charges for the
  dearer one.
* one shape of real material, at the very top of the size range: a 2800-token
  sheet with 1000 distinct 6.6-character tokens against a 2100-token transcript.
  38,385,244 characters, inside the old 40,000,000 cap, and 82,741,044 estimated
  cells now. It aligned in **2.17 s**. That is prose-shaped rather than
  lyric-shaped -- sung English averages nearer four characters a token, and the
  same sheet in sung-length words is estimated at 58,500,492 -- but it is the
  honest cost of the change, and the first place to look if a real sheet is ever
  refused.

What it now admits that the character count refused: long tokens in small
numbers. 250 x 250 distinct thirty-two-character tokens is 64,000,000
characters, over the old cap, and 67,125,000 estimated cells, inside this one --
**2.02 s** of work, which is the thing the cap was always meant to be reading.

Where the cap bites, in closed form: the estimate is (the distinct reference
tokens' lengths, summed) x (the distinct hypothesis tokens' lengths, summed),
plus fifty for every distinct pair, with a row's charge repeated for each
occurrence the row cache had no room to keep. Repetition is free on both sides;
distinctness is the whole cost. At five characters a token, about what the
sheets above average, the cap is reached at a million distinct pairs -- a
thousand distinct sheet tokens against a thousand distinct transcript ones.

Why it cannot simply be tighter. The dense long song above is estimated at
58,500,492 cells and takes 2.03 s; the worst vocabulary at that same estimate
takes nearly four times as long. That ratio is the whole of what the letter mask
saves on real material, and it sets the floor here: any cap generous enough for
a dense long song is generous enough for about four times its cost in material
built to defeat the bounds, so a ceiling much under nine seconds means refusing
real sheets. Halving this cap would refuse the second sheet above outright.
Lifting it instead: the previous round measured 1000 x 6000 distinct
thirty-two-character tokens -- what the memory cap alone admits -- at 156 s on
its machine, and the per-pair figures above put the same shape near 200 s on
this one. If some surface ever needs a tighter ceiling than nine seconds,
``MAX_COMPARISON_CELLS`` is the number to move, and what a tighter one costs is
the long song rather than the pathological input.

How to read every figure above. Each is wall clock on one machine -- the one
this round was measured on -- taken as the best of two or three runs of the
shape, and with other work on the machine throughout. The songs are timed end to
end through ``align_lines``; the synthetic shapes through ``_aligned_pairs``,
which is the fill and the scoring and nothing else. Another CPU will give
other numbers: the previous round's figures, taken on another machine, run about
a fifth to a quarter faster than this round's at the same token lengths, some of
which is the machine and some the shape, which is exactly why these are one
sample rather than constants. No test asserts any of them, because a clock is
not a thing to assert in CI. What is worth carrying away is the ceiling, the
ratios, and which way each estimate errs.

What comes out
--------------
Cues follow the *reference's* own line breaks, never the transcript's segment
boundaries: the user's line breaks are the lyric's real phrasing, and Whisper's
segments are an artefact of its decode window.

Each reference word carries where its timing came from -- ``matched`` (an exact
token match), ``near`` (a partial-credit match, so the time is measured but the
transcriber heard a different word) or ``interpolated`` (nothing matched; the
time was placed between the nearest measured neighbours). Interpolated timings
are never presented as measured, and ``AlignmentReport`` says how many there
are so a caller can decide to fall back to whatever it had before. The
thresholds for that decision are ``USABLE_*``/``GOOD_*`` below, and the report
answers it directly with ``grade`` and ``usable``.

Calibration -- what "usable" is measured against, and what it is not
--------------------------------------------------------------------
``usable`` promises one thing: False means the caller should keep the timing it
already had. For an untimed sheet that is the even spread, so the promise is
testable, and it was tested rather than assumed. A 152-case sweep over synthetic
songs with recorded ground truth -- eight songs with intros, solos and varying
line density, corrupted seven ways, plus transcripts that stop or start part way
through, holes of 8 to 64 words, and a wrong-language transcript -- gives:

* Every one of the 37 cases where the even spread really was closer to the
  truth was caught by ``USABLE_MATCHED_FRACTION`` or ``USABLE_UNALIGNED_RUN``.
  Those two carry the verdict; that is measured, not assumed.
* ``mean_displacement`` caught none of the 37 that the other two did not. The
  only alignments it condemned on its own were ones measurably *better* than
  the fallback: on a 420-word sheet with an instrumental in the middle and the
  words either side of it never transcribed, the shipped 2.5 s threshold
  condemned three cases reporting 2.6 s to 5.4 s whose true errors were 0.86 s,
  0.85 s and 1.71 s -- against an even spread costing 18.5 s to 38.6 s. So the
  threshold now sits at 12.0 s, where the number means something on its own
  terms (a mean worst-case error longer than a sung line) rather than where it
  second-guesses the two gates that do the work.
* It does real work in ``grade`` at 0.5 s, which is not the same question. On
  that same sheet, four words missed either side of a 90-second instrumental
  leave 99% of words measured and a longest run of four -- both comfortably
  "good" -- with a true error of 0.22 s that only ``GOOD_MEAN_DISPLACEMENT``
  notices. Across those runs the bound came out at about three times the true
  error, and below the 0.5 s threshold the true error stayed under 0.15 s.
* Those sweeps corrupt the transcript's *words*. Its *times* matter too, now
  that a measured word the monotone pass moved counts towards the number, so
  that was swept separately: the same 160 cases with overlapping word spans
  throughout and a rising share of words inheriting their segment's end (what
  a missing word end becomes on this app's own transcribe path). At one such
  word per song, and at 0.2%, 1% and 5% of words, nothing changed -- no
  alignment anywhere in those four sweeps was condemned by the displacement
  gate alone while still beating the even spread, and none escaped ``usable``
  while the spread was better. Only when *every* word loses its end does the
  reported number cross 12.0 s on alignments that are still better than the
  spread: 40 of 160 there, with the sweep's median report at 11.6 s and true
  errors of about the same size. That is not a mis-report -- the timing really
  is ten seconds out -- but a caller who would rather have a bad alignment than
  a bad even spread should know that ``usable`` turns that one down.

What this does not establish: the corruption model is synthetic single-word
substitution and deletion, not real faster-whisper output, and no measurement
against real audio exists anywhere in this module's history. The thresholds are
calibrated against a model of the failure, which is better than being asserted
and worse than being measured on the thing itself. Treat them as provisional in
that specific way, and re-run the sweep against real transcripts when there are
any to run it against.

Every number in ``AlignmentReport`` is a count, a fraction or a duration, and
``summary`` is those numbers plus a grade from a three-word vocabulary. Neither
carries lyric text, a path or a URL. That is what lets the pipeline write the
whole report into the project manifest and put ``summary`` into the single line
of detail a run reports while it works, neither of which has a redaction pass.

Known limits, stated rather than hidden
---------------------------------------
* A global alignment cannot reorder. If the singer takes verse two first, one of
  the two blocks keeps real timings and the other is interpolated; the output
  stays ordered and the report shows the loss.
* Tokens are whitespace-delimited, so a script written without spaces (Chinese,
  Japanese) aligns line-by-line at best.
* OPEN, not handled: ``mean_displacement`` bounds the error *given the measured
  words either side of a guess are the right ones*, and a token the sheet
  repeats can be paired with the wrong occurrence of itself at no cost to the
  alignment score. Both pairings score identically and nothing in the token
  stream separates them, so the traceback picks one. When it picks wrong, the
  guessed run collapses into a hole a few milliseconds wide and the bound
  collapses with it. Measured: a fifty-one-word sheet, forty-three of them
  timed, one repeated token either side of a seven-word hole ->
  ``mean_displacement`` 0.012s, ``longest_unaligned_run`` 8, grade "fair",
  ``usable`` True, and eight words placed at 8.4s that were sung somewhere
  before 70s. None of the three thresholds catches it; ``usable`` is wrong
  there and this module currently has no way to know. The reproduction is kept
  in ``test_a_repeated_token_can_be_matched_to_the_wrong_occurrence...`` so that
  the limit stays visible and any future fix has a case to prove itself on.
"""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Literal

from .errors import InvalidInputError
from .text import fold_accents
from .types import LyricCue, LyricWord

__all__ = [
    "AlignedLine",
    "AlignedWord",
    "AlignmentReport",
    "AlignmentResult",
    "align_lines",
    "align_reference_text",
    "comparison_tokens",
    "hypothesis_from_cues",
    "token_similarity",
]

# --------------------------------------------------------------------------- #
# Bounds
# --------------------------------------------------------------------------- #

#: Characters of reference text considered at all. Well above any real lyric
#: sheet; here so that normalising a pasted novel is not the failure mode.
MAX_REFERENCE_CHARS = 262_144
#: Comparison tokens per side. 6000 is roughly forty minutes of dense singing.
MAX_TOKENS = 6_000
#: Dynamic-programming cells. One traceback byte each, so this is also the
#: traceback's size in bytes. This bounds the alignment's *memory*; it is a
#: weak bound on its time, because filling a cell is cheap and comparing the
#: two tokens behind it is not. MAX_COMPARISON_CELLS is what bounds the time.
MAX_ALIGNMENT_CELLS = 6_000_000
#: Comparison cells: what scoring the whole alignment is estimated to cost,
#: summed over every distinct token pair it will score -- the product of the two
#: tokens' lengths, plus COMPARISON_PAIR_CELLS for the pair itself. Scoring is
#: the half of the work that can run into minutes, and this is what stops it.
#: The number is derived from a time budget rather than chosen: see "Cost" in
#: the module docstring for the budget, the measurement it was converted with,
#: and where the estimate is furthest from the clock.
MAX_COMPARISON_CELLS = 75_000_000
#: What one scored pair costs before either token's length is counted: the call,
#: the bounds test and the shared-prefix walk, none of which get shorter as the
#: tokens do. Charging only the characters priced the pass over a token and gave
#: the call away, which is what let short tokens run the clock up while the
#: count stayed small. Calibrated, not derived -- "Cost" has the sweep.
COMPARISON_PAIR_CELLS = 50
#: Score rows kept for reuse across repeated reference tokens. Bounded so that a
#: long song against a long sheet cannot trade unbounded memory for speed.
_MAX_CACHED_CELLS = 2_000_000

# --------------------------------------------------------------------------- #
# Scoring
# --------------------------------------------------------------------------- #

#: Cost of leaving one token unpaired. Only its relation to the pair score
#: matters, which is why the two are defined together below.
GAP_PENALTY = -0.6
#: The similarity at which pairing two tokens is exactly as good as leaving both
#: of them unpaired. This is the alignment's only real decision.
MATCH_THRESHOLD = 0.5
#: How steeply a better pairing is preferred over a worse one. Chosen so that an
#: identical pair scores exactly +1.0, which keeps the numbers readable.
SIMILARITY_SLOPE = 4.4
#: Longest token compared at all. Beyond this the tail cannot change the
#: verdict, and every bound and bitmap below is taken over this window rather
#: than the whole token, so that none of them can disagree with the scoring.
_SIMILARITY_MAX_LENGTH = 32

# --------------------------------------------------------------------------- #
# Layout
# --------------------------------------------------------------------------- #

#: Floor on an interpolated word's span. Small enough to be inaudible, large
#: enough that rounding to milliseconds cannot collapse a word to a point.
MIN_WORD_SECONDS = 0.04
#: Floor on a cue's span, and a degeneracy floor rather than a readability one:
#: it stops rounding to milliseconds collapsing a cue to a point, exactly as
#: MIN_WORD_SECONDS does for a word. The lyrics module has a floor of its own,
#: more than twenty times this one, for how long a line must last to be *read*.
#: That one is applied to spans nobody measured and deliberately never to these,
#: since stretching a measured span would make the file say the singer held a
#: word longer than they did -- `pipeline._apply_alignment` is where the two
#: routes meet and says so. The two are different rules, not one rule twice.
MIN_CUE_SPAN_SECONDS = 0.05
#: Sung seconds per character, used only where measured anchors cannot supply a
#: rate (no anchors at all, or a run running off the end of the transcript).
NOMINAL_SECONDS_PER_CHAR = 0.09
_MIN_SECONDS_PER_CHAR = 0.02
_MAX_SECONDS_PER_CHAR = 0.5

# --------------------------------------------------------------------------- #
# Confidence thresholds -- the "is this good enough to use" contract
# --------------------------------------------------------------------------- #

#: Below any of these the alignment is graded "poor" and `usable` is False: the
#: caller should keep whatever timing it already had. What "usable" is measured
#: against, and what that measurement does not cover, is set out under
#: "Calibration" in the module docstring -- read it before quoting these.
USABLE_MATCHED_FRACTION = 0.55
#: A mean worst-case error longer than a whole sung line: past here the timing
#: no longer says which line is being sung, which is the entire job. Measured
#: rather than assumed -- see "Calibration".
USABLE_MEAN_DISPLACEMENT = 12.0
USABLE_UNALIGNED_RUN = 24
#: Meeting all three of these is graded "good"; in between is "fair".
GOOD_MATCHED_FRACTION = 0.85
#: Half a second is about where a listener sees the highlight land late. This
#: is the one of the three "good" tests that catches an alignment which matched
#: almost everything but whose few misses sit in a long instrumental.
GOOD_MEAN_DISPLACEMENT = 0.5
GOOD_UNALIGNED_RUN = 8

Origin = Literal["matched", "near", "interpolated"]
Grade = Literal["good", "fair", "poor"]

_DIAGONAL = 1
_UP = 2
_LEFT = 3

# --------------------------------------------------------------------------- #
# Normalisation -- for comparison only, never for output
# --------------------------------------------------------------------------- #

#: A digit run or a letter run, apostrophes kept inside a word so that "don't"
#: reaches the contraction table before its apostrophe is dropped.
_PIECE = re.compile(r"\d+|[^\W\d_]+(?:'[^\W\d_]+)*")
#: "[Chorus]", "[Guitar solo]" -- square brackets do not survive into singing.
_SQUARE_MARKER = re.compile(r"^\[[^\[\]]*\]$")
#: "(Verse 2)" is structure; "(I love you)" is a backing vocal. Only the first
#: word tells them apart, so only the first word is consulted.
_ROUND_MARKER = re.compile(r"^\(([^()]*)\)$")
_MARKER_WORDS = frozenset(
    {
        "bridge",
        "breakdown",
        "chorus",
        "coda",
        "hook",
        "instrumental",
        "interlude",
        "intro",
        "outro",
        "prechorus",
        "refrain",
        "repeat",
        "solo",
        "spoken",
        "vamp",
        "verse",
    }
)

#: Expanded rather than merely stripped, because a transcriber writes "do not"
#: where the sleeve writes "don't" and both must reach the same tokens. Bare
#: forms are listed alongside apostrophised ones: sloppy sheets and transcripts
#: both drop the apostrophe, and only unambiguous bare forms are included --
#: "were", "well", "ill" and "id" are ordinary words and are deliberately absent.
_CONTRACTIONS: dict[str, tuple[str, ...]] = {
    "ain't": ("is", "not"),
    "aint": ("is", "not"),
    "aren't": ("are", "not"),
    "arent": ("are", "not"),
    "can't": ("can", "not"),
    "cannot": ("can", "not"),
    "cant": ("can", "not"),
    "couldn't": ("could", "not"),
    "couldnt": ("could", "not"),
    "didn't": ("did", "not"),
    "didnt": ("did", "not"),
    "doesn't": ("does", "not"),
    "doesnt": ("does", "not"),
    "don't": ("do", "not"),
    "dont": ("do", "not"),
    "gimme": ("give", "me"),
    "gonna": ("going", "to"),
    "gotta": ("got", "to"),
    "hadn't": ("had", "not"),
    "hadnt": ("had", "not"),
    "hasn't": ("has", "not"),
    "hasnt": ("has", "not"),
    "haven't": ("have", "not"),
    "havent": ("have", "not"),
    "he'd": ("he", "would"),
    "he'll": ("he", "will"),
    "he's": ("he", "is"),
    "hes": ("he", "is"),
    "how's": ("how", "is"),
    "i'd": ("i", "would"),
    "i'll": ("i", "will"),
    "i'm": ("i", "am"),
    "i've": ("i", "have"),
    "im": ("i", "am"),
    "isn't": ("is", "not"),
    "isnt": ("is", "not"),
    "it'll": ("it", "will"),
    "it's": ("it", "is"),
    "its": ("it", "is"),
    "ive": ("i", "have"),
    "kinda": ("kind", "of"),
    "lemme": ("let", "me"),
    "let's": ("let", "us"),
    "lets": ("let", "us"),
    "outta": ("out", "of"),
    "shan't": ("shall", "not"),
    "she'd": ("she", "would"),
    "she'll": ("she", "will"),
    "she's": ("she", "is"),
    "shes": ("she", "is"),
    "shouldn't": ("should", "not"),
    "shouldnt": ("should", "not"),
    "that's": ("that", "is"),
    "thats": ("that", "is"),
    "there's": ("there", "is"),
    "theres": ("there", "is"),
    "they'd": ("they", "would"),
    "they'll": ("they", "will"),
    "they're": ("they", "are"),
    "they've": ("they", "have"),
    "theyre": ("they", "are"),
    "theyve": ("they", "have"),
    "til": ("until",),
    "wanna": ("want", "to"),
    "wasn't": ("was", "not"),
    "wasnt": ("was", "not"),
    "we'd": ("we", "would"),
    "we'll": ("we", "will"),
    "we're": ("we", "are"),
    "we've": ("we", "have"),
    "weren't": ("were", "not"),
    "werent": ("were", "not"),
    "weve": ("we", "have"),
    "what's": ("what", "is"),
    "whats": ("what", "is"),
    "where's": ("where", "is"),
    "who's": ("who", "is"),
    "won't": ("will", "not"),
    "wont": ("will", "not"),
    "wouldn't": ("would", "not"),
    "wouldnt": ("would", "not"),
    "you'd": ("you", "would"),
    "you'll": ("you", "will"),
    "you're": ("you", "are"),
    "you've": ("you", "have"),
    "youre": ("you", "are"),
    "youve": ("you", "have"),
}

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
)
_TEENS = (
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = (
    "",
    "",
    "twenty",
    "thirty",
    "forty",
    "fifty",
    "sixty",
    "seventy",
    "eighty",
    "ninety",
)


def _below_hundred(value: int) -> tuple[str, ...]:
    if value < 10:
        return (_ONES[value],)
    if value < 20:
        return (_TEENS[value - 10],)
    tens, ones = divmod(value, 10)
    return (_TENS[tens],) if ones == 0 else (_TENS[tens], _ONES[ones])


def _below_thousand(value: int) -> tuple[str, ...]:
    if value < 100:
        return _below_hundred(value)
    hundreds, rest = divmod(value, 100)
    words = (_ONES[hundreds], "hundred")
    return words if rest == 0 else words + _below_hundred(rest)


def _number_tokens(digits: str) -> tuple[str, ...]:
    """A digit run as the words a singer would sing.

    Years are the case worth special-casing: 1985 is sung "nineteen eighty
    five", never "one thousand nine hundred and eighty five". That form is taken
    for 1100 to 2099 and only while the last two digits are ten or more, so 1900
    does come back as "one thousand nine hundred" and 2000 as "two thousand".
    A leading zero, ten digits or more, or any value of a million or more is
    read digit by digit: "1000000" gives "one zero zero zero zero zero zero",
    not "one million".

    Both of those are misreadings and both are left alone. A mis-tokenised
    number costs a handful of token pairs out of the hundreds an alignment
    scores, while changing how numbers are read moves every alignment that
    contains one -- a poor trade for a case a lyric sheet meets once a song.
    """

    if len(digits) > 1 and digits[0] == "0":
        return tuple(_ONES[int(digit)] for digit in digits)
    if len(digits) > 9:
        return tuple(_ONES[int(digit)] for digit in digits)
    value = int(digits)
    if len(digits) == 4 and 1100 <= value <= 2099 and value % 100 >= 10:
        high, low = divmod(value, 100)
        return _below_hundred(high) + _below_hundred(low)
    if value < 1000:
        return _below_thousand(value)
    if value < 1_000_000:
        thousands, rest = divmod(value, 1000)
        words = (*_below_thousand(thousands), "thousand")
        return words if rest == 0 else words + _below_thousand(rest)
    return tuple(_ONES[int(digit)] for digit in digits)


def comparison_tokens(value: str) -> tuple[str, ...]:
    """The tokens a piece of text is *compared* by. Never what is displayed.

    Case, accents, punctuation and contraction spelling all differ between a
    lyric sheet and a transcript of the same line without the line differing at
    all, so they are folded away here, and nowhere else on the path from this
    function's input to this module's output. The caller's original string is
    what reaches the output; getting this backwards is what turns "Don't" into
    "dont" on screen.

    Not the only fold in the package, and shared with the other one exactly as
    far as the two agree. `text.fold_accents` -- case, accents, apostrophe
    spelling -- is common to both and is one implementation because it is one
    policy; what differs is the tail. `_whisper_worker.fold` collapses
    punctuation to spaces and stops, where this expands digits and contractions
    into the words a singer sings, and the two tails are deliberately not merged:
    the worker counts a repetition loop's *period* in surface tokens, so
    expanding "1985 ain't" from three tokens to five would push a real loop past
    its `MAX_PHRASE_TOKENS`. That function's docstring has the measurement.
    """

    tokens: list[str] = []
    for piece in _PIECE.findall(fold_accents(value).replace("&", " and ")):
        # `isdecimal`, not `isdigit`, and the difference is a crash. `_PIECE`'s digit
        # run is `\d+`, which is exactly the Nd category; `isdigit` is also true for
        # No -- ETHIOPIC DIGIT EIGHT, NEW TAI LUE THAM DIGIT ONE, 69 codepoints in
        # all that survive NFKD -- and those reach the word branch of `_PIECE` and
        # then `int()`, which raises `ValueError` on them. A bare `ValueError` out of
        # this module is not a refusal a caller can act on: `pipeline._apply_alignment`
        # catches `InvalidInputError` to skip alignment and would fail the whole
        # lyrics stage on this instead, over one character in a lyric sheet. Matching
        # the predicate to the pattern that produced the piece is the fix; an Ethiopic
        # numeral is then compared as the character it is, like any other letter.
        if piece[0].isdecimal():
            tokens.extend(_number_tokens(piece))
            continue
        expansion = _CONTRACTIONS.get(piece)
        if expansion is not None:
            tokens.extend(expansion)
            continue
        cleaned = piece.replace("'", "")
        if cleaned:
            tokens.append(cleaned)
    return tuple(tokens)


def _pattern(token: str) -> dict[str, int]:
    """Which positions of `token` each of its characters occupies, one bit each.

    Built once per token and reused for every distance measured against it,
    which is the whole saving: the row builder pays one pass over its reference
    token, and each of the hundreds of distances that follow then costs one pass
    over the *other* token and nothing more.
    """

    pattern: dict[str, int] = {}
    bit = 1
    for char in token:
        pattern[char] = pattern.get(char, 0) | bit
        bit <<= 1
    return pattern


def _distance(pattern: dict[str, int], length: int, other: str) -> int:
    """Levenshtein distance between `other` and the token `pattern` was built from.

    Hyyro's bit-parallel form of Myers' algorithm, and the reason this module
    is not quadratic in token length. The textbook fill carries a column of
    distances and touches every one of the ``length x len(other)`` cells; this
    carries the same column as two bit vectors -- which of its cells stand one
    above the cell before them, and which stand one below -- and advances the
    whole column in a fixed number of integer operations however tall it is. So
    the loop runs once per character of `other` rather than once per cell, and
    only the running distance in the last row has to be tracked at all.

    Names follow the paper so the code can be checked against it: `high` and
    `low` are its VP and VN, `carry` and `reach` its Xh and Xv, `rise` and
    `fall` its PH and MH.

    Python's integers are arbitrary precision, so this is exact at any length
    rather than only up to a machine word -- it merely stops being constant-time
    per character past one. Both callers compare a window of at most
    ``_SIMILARITY_MAX_LENGTH`` characters, so in practice every value below fits
    a single word and no operation on one has to carry between words.
    """

    if length == 0:
        return len(other)
    full = (1 << length) - 1
    last = 1 << (length - 1)
    high = full
    low = 0
    distance = length
    occurrences = pattern.get
    for char in other:
        equal = occurrences(char, 0)
        reach = equal | low
        carry = (((equal & high) + high) ^ high) | equal
        rise = low | ~(carry | high)
        fall = high & carry
        if rise & last:
            distance += 1
        if fall & last:
            distance -= 1
        rise = ((rise << 1) | 1) & full
        fall = (fall << 1) & full
        high = (fall | ~(reach | rise)) & full
        low = rise & reach
    return distance


def _shared_prefix(left: str, right: str) -> int:
    """How many leading characters the two tokens agree on."""

    shared = 0
    for left_char, right_char in zip(left, right, strict=False):
        if left_char != right_char:
            break
        shared += 1
    return shared


def _similarity(distance: int, shared: int, left_length: int, right_length: int) -> float:
    """The scoring rule itself, over measurements already taken.

    Kept apart from `token_similarity` so that the row builder, which reaches
    the same numbers by a cheaper route, can apply the same rule rather than
    carry a second copy of it. The 0.75/0.25 split lives here and nowhere else:
    the row builder's prefilter is that split rearranged against
    MATCH_THRESHOLD, so moving either number here means re-deriving it there.
    """

    longest = left_length if left_length > right_length else right_length
    ratio = 1.0 - distance / longest
    prefix = 2 * shared / (left_length + right_length)
    return max(0.0, 0.75 * ratio + 0.25 * prefix)


def token_similarity(left: str, right: str) -> float:
    """How alike two comparison tokens are, in [0, 1].

    Edit distance carries most of it; the shared prefix carries the rest,
    because sung endings are what a transcriber drops ("shinin'" for "shining",
    "walkin" for "walking") and a prefix term keeps those pairs together
    without also pulling in words that merely happen to be short.

    Below MATCH_THRESHOLD the exact value carries no meaning and the function is
    free to shortcut to 0.0: nothing downstream tells one rejected pair from
    another, and a pair that scores below the two gaps it would replace is never
    in an optimal alignment.
    """

    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    left = left[:_SIMILARITY_MAX_LENGTH]
    right = right[:_SIMILARITY_MAX_LENGTH]
    longest = max(len(left), len(right))
    # Edit distance is at least the length difference, so a pair this uneven
    # cannot reach MATCH_THRESHOLD however its prefix scores. Skipping it here
    # cannot change the alignment: the pair is rejected either way.
    if abs(len(left) - len(right)) * 3 > longest * 2:
        return 0.0
    return _similarity(
        _distance(_pattern(left), len(left), right),
        _shared_prefix(left, right),
        len(left),
        len(right),
    )


def _letter_mask(token: str) -> int:
    """A 64-bit sketch of which characters a token contains.

    Every distinct character of one token that the other lacks costs at least
    one edit, so the popcount of the difference is a lower bound on the edit
    distance -- and a bound is all the row builder needs to skip a pair that
    cannot reach MATCH_THRESHOLD. It holds in both directions and the row
    builder takes the larger, since one edit can answer a character missing from
    each side but not two from the same side. Folding the character space into
    64 bits only makes the bound weaker, never wrong, so a collision costs a
    wasted comparison rather than a missed match.
    """

    mask = 0
    for char in token:
        mask |= 1 << (ord(char) & 63)
    return mask


def _pair_score(similarity: float) -> float:
    """What pairing two tokens is worth, relative to leaving both unpaired.

    With a linear gap penalty, pairing replaces exactly two gaps, so the pair is
    worth taking precisely when this exceeds ``2 * GAP_PENALTY`` -- which by
    construction happens at ``MATCH_THRESHOLD``.
    """

    return 2 * GAP_PENALTY + SIMILARITY_SLOPE * (similarity - MATCH_THRESHOLD)


_UNRELATED_SCORE = _pair_score(0.0)
#: What a pair worth exactly MATCH_THRESHOLD scores, which is by construction
#: what two gaps are worth. Nothing below this can be on an optimal path -- see
#: the fill in `_aligned_pairs`, which is where that is used and proved.
_MIN_TAKEABLE_SCORE = _pair_score(MATCH_THRESHOLD)


# --------------------------------------------------------------------------- #
# The two inputs
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Reference:
    """The user's sheet: its lines and words verbatim, its tokens for comparison."""

    lines: tuple[str, ...]
    words: tuple[str, ...]
    word_line: tuple[int, ...]
    tokens: tuple[str, ...]
    token_word: tuple[int, ...]
    #: Words carrying at least one comparison token. A word of pure punctuation
    #: can never match anything, so it is timed like any other word but kept out
    #: of the confidence arithmetic, where it would only dilute the answer.
    alignable: tuple[int, ...]
    markers_dropped: int


@dataclass(frozen=True)
class _HypothesisWord:
    text: str
    start: float
    end: float


def _is_marker(line: str) -> bool:
    if _SQUARE_MARKER.match(line):
        return True
    match = _ROUND_MARKER.match(line)
    if match is None:
        return False
    inner = comparison_tokens(match.group(1))
    return bool(inner) and inner[0] in _MARKER_WORDS


def _build_reference(lines: Sequence[str]) -> _Reference:
    kept: list[str] = []
    words: list[str] = []
    word_line: list[int] = []
    tokens: list[str] = []
    token_word: list[int] = []
    alignable: list[int] = []
    markers = 0
    characters = 0
    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        characters += len(line)
        if characters > MAX_REFERENCE_CHARS:
            raise InvalidInputError(
                f"the reference lyrics exceed the {MAX_REFERENCE_CHARS} character limit"
            )
        if _is_marker(line):
            markers += 1
            continue
        pieces = line.split()
        if not pieces:
            continue
        index = len(kept)
        kept.append(line)
        for piece in pieces:
            word_index = len(words)
            words.append(piece)
            word_line.append(index)
            piece_tokens = comparison_tokens(piece)
            if piece_tokens:
                alignable.append(word_index)
            for token in piece_tokens:
                tokens.append(token)
                token_word.append(word_index)
    if not alignable:
        raise InvalidInputError("the reference lyrics contain no words to align")
    if len(tokens) > MAX_TOKENS:
        raise InvalidInputError(
            f"the reference lyrics are too long to align "
            f"({len(tokens)} tokens; the limit is {MAX_TOKENS})"
        )
    return _Reference(
        lines=tuple(kept),
        words=tuple(words),
        word_line=tuple(word_line),
        tokens=tuple(tokens),
        token_word=tuple(token_word),
        alignable=tuple(alignable),
        markers_dropped=markers,
    )


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _prepare_hypothesis(words: Sequence[LyricWord]) -> tuple[list[_HypothesisWord], int]:
    """Drop what cannot be trusted, sort what remains.

    A transcriber's word list arrives with the odd None-substituted bound and,
    after a VAD re-seek, the odd word that begins before the one before it.
    Alignment is order-preserving, so the order it is given is the order it
    believes; sorting here is what stops one stray word from mistiming a verse.
    """

    prepared: list[_HypothesisWord] = []
    ignored = 0
    for word in words:
        text = word.get("text", "")
        start = _finite(word.get("start"))
        end = _finite(word.get("end"))
        if not isinstance(text, str) or not text.strip() or start is None or end is None:
            ignored += 1
            continue
        if start < 0:
            ignored += 1
            continue
        prepared.append(_HypothesisWord(text=text.strip(), start=start, end=max(start, end)))
    prepared.sort(key=lambda word: (word.start, word.end))
    return prepared, ignored


def _tokenize(words: Sequence[str]) -> tuple[list[str], list[int]]:
    tokens: list[str] = []
    owners: list[int] = []
    for index, word in enumerate(words):
        for token in comparison_tokens(word):
            tokens.append(token)
            owners.append(index)
    return tokens, owners


# --------------------------------------------------------------------------- #
# Needleman-Wunsch
# --------------------------------------------------------------------------- #


def _comparison_cost(
    row_lengths: Sequence[int], hypothesis_width: int, hypothesis_tokens: int
) -> int:
    """What scoring `row_lengths` rows will cost, in comparison cells.

    `row_lengths` is the length of each score row the fill will build, in order,
    repeats included; `hypothesis_width` is the summed length of the distinct
    hypothesis tokens and `hypothesis_tokens` how many there are. A row scores
    at most one pair per distinct hypothesis token, and each of those pairs
    costs a call whatever the two tokens' lengths are, so a row's charge is its
    own length across every hypothesis token's characters *plus*
    COMPARISON_PAIR_CELLS a pair.

    An estimate of the clock rather than a count of anything, and it errs in both
    directions by amounts "Cost" in the module docstring measures. On the pairs
    themselves it is deliberately an upper bound: the identity shortcut and the
    row builder's own distance bounds only ever remove pairs from a row, and
    none of that can be foreseen from lengths alone.
    """

    return sum(row_lengths) * hypothesis_width + COMPARISON_PAIR_CELLS * hypothesis_tokens * len(
        row_lengths
    )


def _aligned_pairs(reference: Sequence[str], hypothesis: Sequence[str]) -> list[tuple[int, int]]:
    """Optimal global alignment, as the list of (reference, hypothesis) pairs.

    Two caps for two shapes of cost, both measured under "Cost" in the module
    docstring: MAX_ALIGNMENT_CELLS bounds the memory at one traceback byte a
    cell, and MAX_COMPARISON_CELLS bounds the scoring, which is the half that
    can run away. Two things hold the scoring down. Score rows are built per
    *distinct* reference token and reused, so a chorus sung five times costs one
    row rather than five -- which is why real lyrics, which repeat heavily, sit
    nowhere near either cap. And the whole of the scoring is priced before any
    of it is done, so a vocabulary that defeats the reuse is refused for the
    price of reading it rather than ground through and refused at the end.
    """

    rows = len(reference)
    columns = len(hypothesis)
    if rows == 0 or columns == 0:
        return []
    if rows * columns > MAX_ALIGNMENT_CELLS:
        raise InvalidInputError(
            f"the lyrics and the transcript are too long to align together "
            f"({rows} x {columns} token cells; the limit is {MAX_ALIGNMENT_CELLS})"
        )

    vocabulary: dict[str, int] = {}
    reference_ids = [vocabulary.setdefault(token, len(vocabulary)) for token in reference]
    hypothesis_ids = [vocabulary.setdefault(token, len(vocabulary)) for token in hypothesis]
    # Held as exactly what token_similarity compares -- its truncated view -- so
    # that the bounds and the scoring below cannot disagree with it about a
    # pair. Two tokens differing only past the window collapse to one string
    # here and score 1.0, which is what token_similarity gives them as well;
    # what keeps them out of the *exact* count is token equality in
    # `align_lines`, not anything here.
    terms = [""] * len(vocabulary)
    for token, identifier in vocabulary.items():
        terms[identifier] = token[:_SIMILARITY_MAX_LENGTH]
    masks = [_letter_mask(term) for term in terms]
    lengths = [len(term) for term in terms]
    heads = [term[:1] for term in terms]
    positions: dict[int, list[int]] = {}
    for column, identifier in enumerate(hypothesis_ids):
        positions.setdefault(identifier, []).append(column)

    cache: dict[int, list[float]] = {}
    cached_cells = 0
    # Widest a single score row's scoring work can be: this reference token
    # against every distinct hypothesis token. It is an upper bound on what is
    # actually done -- the identity shortcut and the row's own distance bounds
    # only ever remove pairs from it.
    hypothesis_width = sum(lengths[identifier] for identifier in positions)

    # Every row the fill will build, in the order it builds them: one per
    # distinct reference token, and one more for every repeat of a token the
    # cache had no room to keep, because that row is genuinely recomputed. The
    # bookkeeping is `score_row`'s own, run ahead of it -- a row is priced
    # exactly when it would be built and kept exactly when it would be kept --
    # so the rows priced are the rows built, and the only slack in the estimate
    # is inside a row, where the bounds below throw pairs out.
    row_lengths: list[int] = []
    keeping: set[int] = set()
    keeping_cells = 0
    for identifier in reference_ids:
        if identifier in keeping:
            continue
        row_lengths.append(lengths[identifier])
        if keeping_cells + columns <= _MAX_CACHED_CELLS:
            keeping.add(identifier)
            keeping_cells += columns
    estimated_cells = _comparison_cost(row_lengths, hypothesis_width, len(positions))
    if estimated_cells > MAX_COMPARISON_CELLS:
        raise InvalidInputError(
            f"the lyrics and the transcript are too different to align together "
            f"({estimated_cells} estimated token comparison cells; the limit is "
            f"{MAX_COMPARISON_CELLS})"
        )

    def score_row(identifier: int) -> list[float]:
        nonlocal cached_cells
        existing = cache.get(identifier)
        if existing is not None:
            return existing
        # Everything is unrelated until shown otherwise, and a pair below
        # MATCH_THRESHOLD is left at that floor. In exact arithmetic the floor
        # would only be tidiness -- the optimum never contains a pair worth less
        # than the two gaps it replaces, so a rejected pair's own score could be
        # anything. In floating point it does more than that, and the fill below
        # leans on it. A pair at similarity 0.49999999999999994 -- five
        # characters, one shared, two edits apart -- scores exactly one ulp below
        # 2 * GAP_PENALTY, and past about four in magnitude -- seven gaps into
        # any row -- that ulp is finer than the grid the sum rounds onto, so
        # `corner + score` and the cell above can land on the same double, and
        # often do: measured, in an alignment that reaches it, corner
        # -10.719999999999995 plus that score gives -11.919999999999995, and so
        # does corner plus two gaps. The `>=`
        # tie-break takes the diagonal from there, pairing two tokens
        # MATCH_THRESHOLD rejects. Dropping the floor and the fill's matching
        # skip together moved the traceback in 133 of 700 random alignments over
        # a vocabulary of such pairs; one of them is pinned in
        # `test_a_pair_below_the_threshold_is_never_taken_even_where_the...`.
        # The floor is what keeps every rejected pair far enough below two gaps
        # that no rounding can reach that tie, and the floored answer is the
        # right one of the two.
        row = [_UNRELATED_SCORE] * columns
        term = terms[identifier]
        mask = masks[identifier]
        length = lengths[identifier]
        head = heads[identifier]
        pattern = _pattern(term)
        for other, where in positions.items():
            if other == identifier:
                similarity = 1.0
            else:
                other_term = terms[other]
                other_length = lengths[other]
                longest = length if length > other_length else other_length
                # Three O(1) lower bounds on the edit distance -- the characters
                # either token has that the other has none of, counted each way,
                # and the difference in length -- against the largest distance
                # at which this pair could still be taken. `_similarity` reaches
                # MATCH_THRESHOLD only while 3 * distance <= longest * (1 +
                # prefix), so the test below is that inequality with the bound
                # standing in for the distance and its denominators cleared.
                # It is exact in two ways that matter: the arithmetic is integer,
                # so a pair landing precisely on the threshold is scored rather
                # than dropped, and the prefix term is the pair's own, not the
                # 1.0 an identical pair would have. Most pairs differ in their
                # first character, where the prefix is exactly 0 and the slack
                # is half what assuming the maximum would allow. Nothing this
                # drops could have reached the threshold, so the alignment is
                # the same alignment.
                at_least = (mask & ~masks[other]).bit_count()
                reverse = (masks[other] & ~mask).bit_count()
                if reverse > at_least:
                    at_least = reverse
                difference = length - other_length
                if difference < 0:
                    difference = -difference
                if difference > at_least:
                    at_least = difference
                shared = _shared_prefix(term, other_term) if head == heads[other] else 0
                total = length + other_length
                if 3 * at_least * total > longest * (total + 2 * shared):
                    continue
                similarity = _similarity(
                    _distance(pattern, length, other_term), shared, length, other_length
                )
            if similarity < MATCH_THRESHOLD:
                continue
            value = _pair_score(similarity)
            for column in where:
                row[column] = value
        if cached_cells + columns <= _MAX_CACHED_CELLS:
            cache[identifier] = row
            cached_cells += columns
        return row

    # The fill, written for the interpreter rather than for the reader: the
    # constants sit in locals and the cell diagonally back is carried forward
    # rather than re-indexed. That and the skip below are worth an eighth of
    # this loop where half its cells hold a real pair and a fifth where almost
    # none do -- and it is now the larger part of an ordinary song's alignment,
    # about three quarters of the 3000-word sheet under "Cost".
    #
    # Most cells never need their diagonal computed at all. Every row's own
    # recurrence gives `previous[column] >= previous[column - 1] + GAP_PENALTY`
    # (going across is always one of the options it took the best of), so
    # `above >= diagonal + (2 * GAP_PENALTY - score)`: a pair worth less than
    # two gaps loses to the cell above it whatever else is going on, and cannot
    # be on an optimal path. `score_row` leaves every rejected pair at
    # _UNRELATED_SCORE, far below that, so the test below skips the arithmetic
    # for all of them and changes no decision -- including the ties, since the
    # branch it skips is one a floored pair provably loses. It is the floor that
    # makes it provable in floating point and not only in exact arithmetic; the
    # comment in `score_row` has the pair that shows why.
    gap = GAP_PENALTY
    takeable = _MIN_TAKEABLE_SCORE
    previous = [gap * column for column in range(columns + 1)]
    pointers: list[bytearray] = []
    for row_index in range(rows):
        scores = score_row(reference_ids[row_index])
        current = [previous[0] + gap] + [0.0] * columns
        pointer = bytearray(columns + 1)
        pointer[0] = _UP
        left = current[0]
        corner = previous[0]
        for column in range(1, columns + 1):
            up = previous[column]
            above = up + gap
            across = left + gap
            score = scores[column - 1]
            if score < takeable:
                if above >= across:
                    best, mark = above, _UP
                else:
                    best, mark = across, _LEFT
            else:
                diagonal = corner + score
                if diagonal >= above and diagonal >= across:
                    best, mark = diagonal, _DIAGONAL
                elif above >= across:
                    best, mark = above, _UP
                else:
                    best, mark = across, _LEFT
            corner = up
            current[column] = best
            pointer[column] = mark
            left = best
        pointers.append(pointer)
        previous = current

    pairs: list[tuple[int, int]] = []
    row_index, column = rows, columns
    while row_index > 0 and column > 0:
        mark = pointers[row_index - 1][column]
        if mark == _DIAGONAL:
            pairs.append((row_index - 1, column - 1))
            row_index -= 1
            column -= 1
        elif mark == _UP:
            row_index -= 1
        else:
            column -= 1
    pairs.reverse()
    return pairs


# --------------------------------------------------------------------------- #
# What comes out
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class AlignedWord:
    """One word of the user's sheet, with a time and where that time came from."""

    text: str
    start: float
    end: float
    #: "matched": an exact token match donated this time. "near": a partial
    #: match did, so the time is measured but the transcriber heard a different
    #: word. "interpolated": nothing matched and the time was placed between the
    #: nearest measured neighbours. The third is a guess and must never be
    #: rendered, exported or reported as if it were the first.
    origin: Origin


@dataclass(frozen=True)
class AlignedLine:
    """One line of the user's sheet, its own line break preserved."""

    text: str
    start: float
    end: float
    words: tuple[AlignedWord, ...]

    @property
    def interpolated_words(self) -> int:
        return sum(1 for word in self.words if word.origin == "interpolated")


@dataclass(frozen=True)
class AlignmentReport:
    """How much of this alignment was measured, and how much was guessed.

    Counts, fractions and durations only: no lyric text, no path, no URL. That
    is deliberate and it is relied on: the pipeline writes the whole of
    `as_json` into the project manifest, and `summary` into the one line of
    detail a run reports while it works. Neither has a redaction pass.
    """

    reference_words: int
    alignable_words: int
    hypothesis_words: int
    hypothesis_words_ignored: int
    exact_words: int
    near_words: int
    interpolated_words: int
    matched_fraction: float
    exact_fraction: float
    hypothesis_used_fraction: float
    #: Mean worst-case placement error, in seconds, over the alignable words.
    #: A guessed word lies somewhere inside the unmeasured stretch between the
    #: measured words either side of it, so the furthest its start can be from
    #: the truth is the distance to that stretch's far end, and that is what is
    #: averaged for it: an upper bound on the error, not an observation of it --
    #: with no ground truth, how far the guess could be out is the only honest
    #: thing to report.
    #:
    #: A measured word contributes zero, because the transcript timed it --
    #: except where the monotone pass had to move it later, when it contributes
    #: exactly how far it was moved. That case is not exotic: this app's own
    #: transcriber substitutes the *segment's* end when faster-whisper omits a
    #: word end, and one such word drags every measured word behind it to the
    #: segment end. Reporting zero there said "mean uncertainty 0.00s" over a
    #: mean error of 8.9 s, which is why the move is counted -- see
    #: `test_a_measured_word_the_monotone_pass_moved_is_not_reported_as_certain`.
    #:
    #: The bound holds while the anchors either side of a guess are right. Two
    #: cases make the number an estimate rather than a bound, both of them a
    #: missing `audio_duration`: a run off the end of the transcript, where
    #: nothing here knows how much recording the run had to fit into, and a
    #: transcript that matched nothing at all, where the sheet is spread across
    #: a nominal duration invented from its own length. The second always
    #: grades "poor" on `matched_fraction` alone, so it decides nothing.
    mean_displacement: float
    #: Longest consecutive stretch of alignable reference words that nothing in
    #: the transcript matched. One long stretch is far worse than the same
    #: number of words missing one at a time, and the mean hides the difference.
    longest_unaligned_run: int
    marker_lines_dropped: int
    grade: Grade
    #: False when the caller should keep the timing it already had. The
    #: thresholds are USABLE_MATCHED_FRACTION, USABLE_MEAN_DISPLACEMENT and
    #: USABLE_UNALIGNED_RUN, all module constants so that a caller can quote
    #: them rather than reproduce them. Which of the three does the work, and
    #: what the sweep behind them did and did not cover, is under "Calibration"
    #: in the module docstring -- worth reading before trusting this field, and
    #: before changing any of the three.
    usable: bool

    def summary(self) -> str:
        return (
            f"timed {self.exact_words + self.near_words} of {self.alignable_words} "
            f"from the transcript ({self.interpolated_words} placed by interpolation), "
            f"longest unmeasured stretch {self.longest_unaligned_run}, "
            f"mean uncertainty {self.mean_displacement:.2f}s, alignment {self.grade}"
        )

    def as_json(self) -> dict[str, object]:
        return {
            "reference_words": self.reference_words,
            "alignable_words": self.alignable_words,
            "hypothesis_words": self.hypothesis_words,
            "hypothesis_words_ignored": self.hypothesis_words_ignored,
            "exact_words": self.exact_words,
            "near_words": self.near_words,
            "interpolated_words": self.interpolated_words,
            "matched_fraction": self.matched_fraction,
            "exact_fraction": self.exact_fraction,
            "hypothesis_used_fraction": self.hypothesis_used_fraction,
            "mean_displacement": self.mean_displacement,
            "longest_unaligned_run": self.longest_unaligned_run,
            "marker_lines_dropped": self.marker_lines_dropped,
            "grade": self.grade,
            "usable": self.usable,
        }


@dataclass(frozen=True)
class AlignmentResult:
    """The user's lines, timed, plus how much of that timing was measured."""

    lines: tuple[AlignedLine, ...]
    report: AlignmentReport

    def cues(self) -> list[LyricCue]:
        """The shape the rest of the app stores and both surfaces render.

        `LyricWord` has nowhere to say a time was interpolated, so this view
        drops that distinction -- and it is still the view the pipeline takes,
        knowingly. Carrying `origin` any further is a change to LYRICS_SCHEMA
        and to the C reader on the other side of it, which belongs with the
        surface that would draw it; `pipeline._apply_alignment` records that
        decision. Where the distinction has to survive inside Python, it is on
        `lines` per word and in `annotated_cues`.
        """

        result: list[LyricCue] = []
        for line in self.lines:
            words: list[LyricWord] = [
                {"start": word.start, "end": word.end, "text": word.text} for word in line.words
            ]
            result.append({"start": line.start, "end": line.end, "text": line.text, "words": words})
        return result

    def annotated_cues(self) -> list[dict[str, object]]:
        """`cues`, with each word's origin and each line's guessed-word count.

        Nothing in the package calls this; `cues` is what the pipeline writes,
        for the schema reason above. It is kept because `origin` is a per-word
        fact this module is careful to get right, and a surface that ever dims a
        guessed word wants it in exactly this shape.
        """

        result: list[dict[str, object]] = []
        for line in self.lines:
            result.append(
                {
                    "start": line.start,
                    "end": line.end,
                    "text": line.text,
                    "interpolated_words": line.interpolated_words,
                    "words": [
                        {
                            "start": word.start,
                            "end": word.end,
                            "text": word.text,
                            "origin": word.origin,
                        }
                        for word in line.words
                    ],
                }
            )
        return result


def _grade(matched_fraction: float, mean_displacement: float, longest_run: int) -> Grade:
    if (
        matched_fraction < USABLE_MATCHED_FRACTION
        or mean_displacement > USABLE_MEAN_DISPLACEMENT
        or longest_run > USABLE_UNALIGNED_RUN
    ):
        return "poor"
    if (
        matched_fraction >= GOOD_MATCHED_FRACTION
        and mean_displacement <= GOOD_MEAN_DISPLACEMENT
        and longest_run <= GOOD_UNALIGNED_RUN
    ):
        return "good"
    return "fair"


# --------------------------------------------------------------------------- #
# Laying the reference out in time
# --------------------------------------------------------------------------- #


def _weight(word: str) -> int:
    """A word's share of a span, by the characters actually sung."""

    return max(1, sum(1 for char in word if char.isalnum()))


def _seconds_per_char(
    anchors: Sequence[tuple[int, tuple[float, float]]],
    weights: Sequence[int],
) -> float:
    """The singing rate this performance actually shows, where it can be seen.

    Only used to place words outside the measured region. Bounded at both ends
    so that one bad anchor pair cannot stretch an interpolated run across the
    whole song, or squash it to nothing.
    """

    if len(anchors) < 2:
        return NOMINAL_SECONDS_PER_CHAR
    first_index, first_span = anchors[0]
    last_index, last_span = anchors[-1]
    span = last_span[1] - first_span[0]
    characters = sum(weights[first_index : last_index + 1])
    if span <= 0 or characters <= 0:
        return NOMINAL_SECONDS_PER_CHAR
    return min(_MAX_SECONDS_PER_CHAR, max(_MIN_SECONDS_PER_CHAR, span / characters))


def _fill_run(
    indices: Sequence[int],
    left: float | None,
    right: float | None,
    *,
    weights: Sequence[int],
    times: list[list[float]],
    stretch: list[tuple[float, float] | None],
    rate: float,
    audio_duration: float | None,
) -> None:
    """Place a stretch of words the transcript never matched.

    Between two anchors the stretch is fitted to the gap, weighted by word
    length. Off either end there is no gap to fit, so the words are laid out at
    the rate the rest of the song sang at -- compressed to reach the end of the
    audio if there is room for them, and allowed to run past it if there is not.
    Running past a duration the caller can trim is honest; claiming three
    hundred words fit in the last second is not.

    Each placed word also records the interval its true time is known to lie
    in, which is what `_displacements` turns into a bound on the error. That
    interval is the run's own two anchors where it has two. Off the front the
    lower edge is 0.0, because nothing is sung before the recording starts. Off
    the back the upper edge is whichever reaches further: `audio_duration` where
    the caller gave one, or the end of the words themselves. It has to be the
    further of the two, because a run too long for the remaining audio is laid
    out past it, and an edge that stopped at the duration would sit behind words
    it is supposed to bound: with `audio_duration` 2.0 and sixty words to place,
    the edge used is 18.4 s, not 2.0, which is pinned in
    `test_the_bound_off_the_end_follows_words_laid_out_past_the_audio`. With no
    duration given it is the words' own reach and nothing else, and there the
    number is an estimate rather than a bound, since nothing here knows where
    the recording ends.
    """

    lengths = [max(MIN_WORD_SECONDS, weights[index] * rate) for index in indices]
    total = sum(lengths)
    if left is not None and right is not None:
        span = right - left
        if span <= 0:
            # The anchors leave no room at all: the words happened at that
            # instant as far as anything measured can tell -- a zero-width
            # interval, so a guess here is as good as a measurement.
            for index in indices:
                times[index] = [left, left]
                stretch[index] = (left, left)
            return
        cursor = left
        for index, length in zip(indices, lengths, strict=True):
            share = span * length / total
            times[index] = [cursor, cursor + share]
            stretch[index] = (left, right)
            cursor += share
        return
    if left is not None:
        scale = 1.0
        if audio_duration is not None:
            available = audio_duration - left
            if len(indices) * MIN_WORD_SECONDS <= available < total:
                scale = available / total
        horizon = left + total * scale
        if audio_duration is not None and audio_duration > horizon:
            horizon = audio_duration
        cursor = left
        for index, length in zip(indices, lengths, strict=True):
            share = length * scale
            times[index] = [cursor, cursor + share]
            stretch[index] = (left, horizon)
            cursor += share
        return
    if right is not None:
        scale = 1.0
        if len(indices) * MIN_WORD_SECONDS <= right < total:
            scale = right / total
        cursor = max(0.0, right - total * scale)
        for index, length in zip(indices, lengths, strict=True):
            share = length * scale
            times[index] = [cursor, cursor + share]
            stretch[index] = (0.0, right)
            cursor += share


def _displacements(
    times: Sequence[Sequence[float]],
    stretch: Sequence[tuple[float, float] | None],
) -> list[float]:
    """Per word, the farthest its assigned start can be from the true one.

    A guessed word lies somewhere inside the unmeasured stretch recorded for it,
    so the worst its placement can be wrong by is the distance to whichever end
    of that stretch is further away. Taking the *far* end is the whole point --
    the near one is zero for the first word of every run, which is the single
    commonest guess there is, and would report perfect confidence on it.

    A measured word's recorded stretch is the single instant the transcript
    timed it at, so it contributes zero -- unless the monotone pass moved it,
    when it contributes exactly how far it was moved. That part of the number
    is an observation rather than a bound: the word demonstrably ended up that
    far from the only time anything measured said it happened.

    Computed after the monotone pass, so it measures where each word actually
    ended up rather than where the layout first put it.

    Every word reaches here with a stretch: `_lay_out` gives one to each anchor
    and `_fill_run` to every word between or beyond them. The `None` skip below
    is for the list's initial state, not for a case the layout can produce.
    """

    result = [0.0] * len(times)
    for index, bounds in enumerate(stretch):
        if bounds is None:
            continue
        low, high = bounds
        start = times[index][0]
        result[index] = max(start - low, high - start, 0.0)
    return result


def _lay_out(
    reference: _Reference,
    measured: Sequence[tuple[float, float] | None],
    audio_duration: float | None,
) -> tuple[list[list[float]], list[float]]:
    """Every reference word's span, and how far each one had to be guessed."""

    count = len(reference.words)
    weights = [_weight(word) for word in reference.words]
    times: list[list[float]] = [[0.0, 0.0] for _ in range(count)]
    #: Per word, the interval its true time is known to lie in, or None where
    #: the transcript measured it and there is nothing to bound.
    stretch: list[tuple[float, float] | None] = [None] * count
    anchors = [(index, span) for index, span in enumerate(measured) if span is not None]

    if not anchors:
        # Nothing matched at all -- a wrong-language or empty transcript. Spread
        # the sheet the way an untimed import would, and let the report say the
        # result is unusable rather than pretend this is an alignment.
        total = float(sum(weights))
        span = (
            audio_duration
            if audio_duration is not None
            else max(total * NOMINAL_SECONDS_PER_CHAR, MIN_CUE_SPAN_SECONDS)
        )
        cursor = 0.0
        for index, weight in enumerate(weights):
            share = span * weight / total
            times[index] = [cursor, cursor + share]
            # Nothing was measured, so every word could be anywhere in it.
            stretch[index] = (0.0, span)
            cursor += share
        return times, _displacements(times, stretch)

    for index, bounds in anchors:
        times[index] = [bounds[0], bounds[1]]
        # A measured word's interval is the instant the transcript put it at.
        # The monotone pass below can still move it later -- an overlapping or
        # inconsistent transcript cannot be laid out in order as given -- and
        # recording that instant here is what makes `_displacements` report the
        # move instead of reporting zero uncertainty on a word that was moved
        # by seconds.
        stretch[index] = (bounds[0], bounds[0])
    rate = _seconds_per_char(anchors, weights)
    previous_index = -1
    previous_end: float | None = None
    for index, bounds in anchors:
        if index > previous_index + 1:
            _fill_run(
                range(previous_index + 1, index),
                previous_end,
                bounds[0],
                weights=weights,
                times=times,
                stretch=stretch,
                rate=rate,
                audio_duration=audio_duration,
            )
        previous_index = index
        previous_end = bounds[1]
    if previous_index + 1 < count:
        _fill_run(
            range(previous_index + 1, count),
            previous_end,
            None,
            weights=weights,
            times=times,
            stretch=stretch,
            rate=rate,
            audio_duration=audio_duration,
        )

    # One forward pass to make the result monotone. It fires when the transcript
    # itself is inconsistent, or when more words have to fit than measured time
    # allows; a measured word can be nudged later, never earlier, and only by
    # what the words in front of it need.
    cursor = 0.0
    for index in range(count):
        start = max(times[index][0], cursor)
        end = max(times[index][1], start)
        times[index] = [start, end]
        cursor = end
    return times, _displacements(times, stretch)


# --------------------------------------------------------------------------- #
# The entry points
# --------------------------------------------------------------------------- #


def align_reference_text(
    reference_text: str,
    hypothesis_words: Sequence[LyricWord],
    *,
    audio_duration: float | None = None,
) -> AlignmentResult:
    """Align a whole untimed lyric document against a word-timed transcript."""

    return align_lines(reference_text.splitlines(), hypothesis_words, audio_duration=audio_duration)


def align_lines(
    lines: Sequence[str],
    hypothesis_words: Sequence[LyricWord],
    *,
    audio_duration: float | None = None,
) -> AlignmentResult:
    """Time the user's lines from a transcript of the same audio.

    `lines` is what `lyrics.LyricsDocument.lines` carries for a source with no
    timing of its own. `hypothesis_words` is a word-timed transcript of the same
    audio -- Whisper's words, or `hypothesis_from_cues` over a caption track.
    `audio_duration` is optional and used only to place words that fall outside
    everything the transcript measured.
    """

    if audio_duration is not None and (not math.isfinite(audio_duration) or audio_duration <= 0):
        raise InvalidInputError("the audio duration must be finite and positive")

    reference = _build_reference(lines)
    hypothesis, ignored = _prepare_hypothesis(hypothesis_words)
    hypothesis_tokens, hypothesis_owner = _tokenize([word.text for word in hypothesis])
    if len(hypothesis_tokens) > MAX_TOKENS:
        raise InvalidInputError(
            f"the transcript is too long to align "
            f"({len(hypothesis_tokens)} tokens; the limit is {MAX_TOKENS})"
        )

    pairs = _aligned_pairs(reference.tokens, hypothesis_tokens)

    word_count = len(reference.words)
    exact = [True] * word_count
    matched_owners: list[list[int]] = [[] for _ in range(word_count)]
    used: set[int] = set()
    # Every pair the traceback returns is at or above MATCH_THRESHOLD, and no
    # re-check here can add to that. A sub-threshold pair scores below the two
    # gaps it replaces, and replacing it *with* those two gaps is always a legal
    # alignment, so it cannot be in an optimal one. `score_row` is where that is
    # enforced, and where a change to the scoring would have to be made.
    for reference_token, hypothesis_token in pairs:
        word_index = reference.token_word[reference_token]
        owner = hypothesis_owner[hypothesis_token]
        matched_owners[word_index].append(owner)
        used.add(owner)
        # "Exact" is token equality, not a similarity of 1.0: the scoring
        # compares a bounded window of each token, and two long tokens that
        # agree inside that window are still two different words.
        if reference.tokens[reference_token] != hypothesis_tokens[hypothesis_token]:
            exact[word_index] = False

    measured: list[tuple[float, float] | None] = [None] * word_count
    for index, owners in enumerate(matched_owners):
        if not owners:
            continue
        measured[index] = (
            min(hypothesis[owner].start for owner in owners),
            max(hypothesis[owner].end for owner in owners),
        )

    times, displacement = _lay_out(reference, measured, audio_duration)

    grouped: list[list[AlignedWord]] = [[] for _ in reference.lines]
    for index, word in enumerate(reference.words):
        origin: Origin = (
            "interpolated" if measured[index] is None else ("matched" if exact[index] else "near")
        )
        grouped[reference.word_line[index]].append(
            AlignedWord(
                text=word,
                start=round(times[index][0], 3),
                end=round(times[index][1], 3),
                origin=origin,
            )
        )

    for position, members in enumerate(grouped):
        # A cue rounded to the same millisecond at both ends is a point, not a
        # span, and nothing can render or seek to a point. Give it the smallest
        # span that is still one -- but never past where the next line starts,
        # because overlapping the next line is a worse defect than a short cue
        # and a floor this small is not worth one.
        span = members[-1].end - members[0].start
        if span >= MIN_CUE_SPAN_SECONDS:
            continue
        target = members[0].start + MIN_CUE_SPAN_SECONDS
        if position + 1 < len(grouped):
            target = min(target, max(grouped[position + 1][0].start, members[-1].end))
        if target > members[-1].end:
            members[-1] = replace(members[-1], end=round(target, 3))

    aligned_lines = tuple(
        AlignedLine(
            text=text,
            start=grouped[position][0].start,
            end=grouped[position][-1].end,
            words=tuple(grouped[position]),
        )
        for position, text in enumerate(reference.lines)
    )

    alignable = reference.alignable
    matched = [index for index in alignable if measured[index] is not None]
    exact_words = sum(1 for index in matched if exact[index])
    longest_run = 0
    run = 0
    for index in alignable:
        if measured[index] is None:
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 0
    mean_displacement = sum(displacement[index] for index in alignable) / len(alignable)
    matched_fraction = len(matched) / len(alignable)
    grade = _grade(matched_fraction, mean_displacement, longest_run)
    report = AlignmentReport(
        reference_words=word_count,
        alignable_words=len(alignable),
        hypothesis_words=len(hypothesis),
        hypothesis_words_ignored=ignored,
        exact_words=exact_words,
        near_words=len(matched) - exact_words,
        interpolated_words=word_count - len(matched),
        matched_fraction=round(matched_fraction, 4),
        exact_fraction=round(exact_words / len(alignable), 4),
        hypothesis_used_fraction=round(len(used) / len(hypothesis), 4) if hypothesis else 0.0,
        mean_displacement=round(mean_displacement, 3),
        longest_unaligned_run=longest_run,
        marker_lines_dropped=reference.markers_dropped,
        grade=grade,
        usable=grade != "poor",
    )
    return AlignmentResult(lines=aligned_lines, report=report)


# --------------------------------------------------------------------------- #
# Building a hypothesis out of something coarser than words
# --------------------------------------------------------------------------- #


def hypothesis_from_cues(cues: Sequence[LyricCue]) -> list[LyricWord]:
    """A word-timed hypothesis from cues, using their words where they have any.

    A caption track is timed per line, not per word, and is still a far better
    hypothesis than nothing: aligning a sheet against it gives line-level
    accuracy, which beats a spread across the whole song by the length of a
    song. For a cue with no words of its own the line is spread inside its own
    span -- never outside it, so the estimate cannot claim more than the caption
    actually said.
    """

    words: list[LyricWord] = []
    for cue in cues:
        start = _finite(cue.get("start"))
        end = _finite(cue.get("end"))
        if start is None or end is None or start < 0:
            continue
        end = max(start, end)
        before = len(words)
        for word in cue.get("words") or []:
            text = word.get("text", "")
            word_start = _finite(word.get("start"))
            word_end = _finite(word.get("end"))
            if not isinstance(text, str) or not text.strip():
                continue
            if word_start is None or word_end is None or word_start < 0:
                continue
            words.append(
                {
                    "start": word_start,
                    "end": max(word_start, word_end),
                    "text": text.strip(),
                }
            )
        if len(words) > before:
            continue
        text_value = cue.get("text", "")
        pieces = text_value.split() if isinstance(text_value, str) else []
        if not pieces:
            continue
        weights = [_weight(piece) for piece in pieces]
        total = float(sum(weights))
        span = max(0.0, end - start)
        cursor = start
        for piece, weight in zip(pieces, weights, strict=True):
            share = span * weight / total
            words.append(
                {"start": round(cursor, 3), "end": round(cursor + share, 3), "text": piece}
            )
            cursor += share
    return words
