# Fretboard contract

Two surfaces draw the same guitar: the native C11 renderer under `src/native`
and the browser player under `src/kilix_playalong/web`. They share no code. This
document is the single definition both implement, and
`tests/fixtures/fretboard_vectors.json` is the machine-checkable form of it. If
the two surfaces ever disagree about a number on this page, the fixture decides.

This exists because the two surfaces have already disagreed once: the tab lane
numbered strings from the low E and the tooltip numbered them from the high e.
Anything below that both surfaces compute independently is a place that bug can
happen again, so every such value is pinned by a vector.

All measured numbers on this page come from the 937-event reference
transcription described under [Measured behaviour](#measured-behaviour). They
are facts about that song, not guarantees about every song.

## 1. Fret geometry

A fret divides the string so the remaining length sounds a semitone higher.
Twelve semitones halve the string, so each fret sits at

```
d(n) = 1 - 2^(-n/12)
```

where `d(n)` is the distance of fret `n` from the nut **as a fraction of scale
length**. Scale length is 25.5 in for reference; only ratios reach the screen,
so no implementation needs the inch value.

The spacing is geometric, not linear. Frets crowd together going up the neck,
and each fret is the same *ratio* of the remaining string, not the same
distance. Evenly spaced fret wires are the single thing that makes a drawn
fretboard look fake, so this formula is mandatory — no implementation may
approximate it with a linear or hand-tuned ramp.

Two identities are exact and worth asserting in code: `d(12) = 0.5` and
`d(24) = 0.75`.

The often-quoted "each fret is 1/17.817 of the remaining string" is an
approximation of `1/d(1) = 17.81715375`, accurate to 8.6e-06 relative. Use the
exponential form; the constant is folklore and is recorded here only so nobody
reintroduces it as a shortcut.

### Fret positions, frets 0-24

`d(n)` as a fraction of scale length, to six decimals.

| n | d(n) | n | d(n) | n | d(n) |
| --- | --- | --- | --- | --- | --- |
| 0 | 0.000000 | 9 | 0.405396 | 17 | 0.625423 |
| 1 | 0.056126 | 10 | 0.438769 | 18 | 0.646447 |
| 2 | 0.109101 | 11 | 0.470268 | 19 | 0.666290 |
| 3 | 0.159104 | 12 | 0.500000 | 20 | 0.685020 |
| 4 | 0.206299 | 13 | 0.528063 | 21 | 0.702698 |
| 5 | 0.250846 | 14 | 0.554551 | 22 | 0.719384 |
| 6 | 0.292893 | 15 | 0.579552 | 23 | 0.735134 |
| 7 | 0.332580 | 16 | 0.603150 | 24 | 0.750000 |
| 8 | 0.370039 | | | | |

Fixture: `geometry.fret_positions`, tolerance 1e-9. The fixture carries up to
twelve significant digits (the exact values write shorter: `0.5`, `0.75`). Near
0.5 a tolerance of 1e-9 is about 9e6 ULPs, so any plausible difference between
two `pow()` implementations passes, while still being 500x tighter than the
5e-7 needed to pin all six printed decimals.

### Cell centres

A fretted note is not played *on* a fret wire, it is played in the space behind
it. The centre of the space for fret `n` is

```
cell(n) = (d(n-1) + d(n)) / 2      for n >= 1
```

`cell(n)` is where a finger dot goes and where an inlay goes. It is the
arithmetic midpoint of the two bounding wires. The musically exact half-semitone
point is `d(n - 0.5)`, which is slightly further from the nut; the arithmetic
midpoint differs from it by at most 0.000405 of scale length, at the first fret,
falling to 0.000107 by fret 24. That is a fraction of a pixel at any size either
surface draws, and the arithmetic form is what fret-slot layouts use. Fixture:
`geometry.cell_centres`.

### Display normalisation

Drawing the full 24-fret neck at `d(n)` would use only 75% of the available
width. A surface that shows frets 0..N scales by the last fret it draws:

```
u(n, N) = d(n) / d(N)
```

so `u(N, N) = 1.0` and the drawing fills its box at any neck length. `N` is a
rendering choice — the native lane and the browser may show different amounts of
neck — but for a given `N` both must produce identical `u`. Fixture:
`geometry.display_normalized` for N in {12, 15, 20, 24}.

## 2. Inlays

Position markers sit at **frets 3, 5, 7, 9, 15, 17, 19 and 21** as a single dot,
and at **frets 12 and 24** as a double dot.

An inlay is named for a fret but does not sit on that fret's wire. It sits in the
middle of the space behind it, at `cell(n)` — the same point a finger occupies.
This is the detail that most drawn fretboards get wrong: putting the dot on the
wire shifts every marker toward the bridge and makes the neck read as
mis-spaced even when the wires themselves are correct.

| Fret | Kind | Centre `cell(n)` | Between wires |
| --- | --- | --- | --- |
| 3 | single | 0.134102 | 0.109101 – 0.159104 |
| 5 | single | 0.228573 | 0.206299 – 0.250846 |
| 7 | single | 0.312737 | 0.292893 – 0.332580 |
| 9 | single | 0.387718 | 0.370039 – 0.405396 |
| 12 | double | 0.485134 | 0.470268 – 0.500000 |
| 15 | single | 0.567051 | 0.554551 – 0.579552 |
| 17 | single | 0.614286 | 0.603150 – 0.625423 |
| 19 | single | 0.656368 | 0.646447 – 0.666290 |
| 21 | single | 0.693859 | 0.685020 – 0.702698 |
| 24 | double | 0.742567 | 0.735134 – 0.750000 |

A single dot is centred across the neck's width. A double dot is two marks
placed symmetrically about the centre line; their separation is a rendering
choice and is not pinned. Only inlays with `n <= N` are drawn. Fixture:
`inlays.cases`.

## 3. Strings

### Order

`guitar-tab.json` indexes strings from **0 = low E, ascending in pitch**, and
every position in the artifact uses that index. This document calls it
`api_index`. It never changes and no surface may redefine it.

| `api_index` | Label | Open MIDI | Player number |
| --- | --- | --- | --- |
| 0 | E | 40 | 6 |
| 1 | A | 45 | 5 |
| 2 | D | 50 | 4 |
| 3 | G | 55 | 3 |
| 4 | B | 59 | 2 |
| 5 | e | 64 | 1 |

Guitarists count the other way — the high e is "string 1". Any number shown to a
person uses `player_number = string_count - api_index`. Any array index uses
`api_index`. The two must never be mixed, and the conversion must be one named
function per surface, not an inline subtraction at each call site. Both surfaces
already have it (`player_string_number()` in `src/native/kpa_ui.c`,
`stringNumber()` in `src/kilix_playalong/web/app.js`); the fretboard uses those,
it does not add a third.

### Thickness

The low E is thickest and the high e thinnest. Using a light electric set as the
reference gauge, rendered width ratio is

```
width_ratio(s) = sqrt(gauge(s) / 0.010)
```

| `api_index` | Label | Gauge (in) | Raw ratio | `width_ratio` |
| --- | --- | --- | --- | --- |
| 0 | E | .046 | 4.600 | 2.144761 |
| 1 | A | .036 | 3.600 | 1.897367 |
| 2 | D | .026 | 2.600 | 1.612452 |
| 3 | G | .017 | 1.700 | 1.303840 |
| 4 | B | .013 | 1.300 | 1.140175 |
| 5 | e | .010 | 1.000 | 1.000000 |

The square root is deliberate. The true gauge ratio is 4.6:1, which at any
sensible line weight either makes the high e sub-pixel or makes the low E a
slab. Compressing to 2.14:1 keeps all six strings visibly graded and distinct at
small sizes, which is the property the drawing actually needs.

Rendered width is `max(1, round(base_width * width_ratio(s)))`. `base_width` is
a per-surface choice — the native renderer works in raster pixels inside terminal
cells, the browser in CSS pixels — so it is not pinned. `width_ratio` is pinned.
Fixture: `strings.cases`.

### Orientation

Tablature puts the high e on top. A player looking down at their own instrument
sees the low E nearest them, so a photo-realistic neck puts the low E on top.
Both conventions ship in real software and neither is wrong.

**`orientation` is a user preference with two values, `"high-e-top"` (default)
and `"low-e-top"`, and it governs the tab lane and the fretboard together.**

The default is `"high-e-top"` because that is what both surfaces already draw in
the tab lane, and because the fretboard and the tab lane appear on the same
screen. Two pictures of one instrument disagreeing about string order in the
same window is a worse defect than either convention is a flaw — that is exactly
the class of bug this document exists to prevent. So the preference flips both
or neither; a surface that applies it to only one of the two is broken.

```
row(api_index, count) = count - 1 - api_index     when "high-e-top"
row(api_index, count) = api_index                 when "low-e-top"
```

`row` is a display row, 0 at the top. Fixture: `orientation.cases`, both values.

## 4. Position to point

A `{string, fret, pitch}` from `guitar-tab.json` becomes a point in a normalised
neck box: `x` from 0 at the nut to 1 at the highest displayed fret `N`, `y` from
0 at the top edge to 1 at the bottom.

```
x = 0                       when fret == 0
x = cell(fret) / d(N)       when fret >= 1
y = (row(api_index, count) + 0.5) / count
```

An open string is marked **at the nut**, not in the space behind fret 1: there is
no finger, and the nut is the thing stopping the string. The `+ 0.5` puts a
string on the centre line of its row rather than on the boundary between rows.

`pitch` is carried in the artifact but is **not** used to place the point. It is
redundant with `{string, fret}` — `pitch == tuning.midi[string] + fret` — and is
used only for chord naming (§5) and for verifying an artifact. A surface must
never derive `x` or `y` from `pitch`; if the two ever disagree, the artifact is
malformed and the position wins for drawing.

Fixture: `orientation.cases` gives `row` and `point` for both orientations across
open strings, fretted notes, both outer strings, and four values of `N`.

## 5. Chord naming

Input is the set of MIDI pitches sounding at one instant — the `pitch` values of
one event's `positions`, at most six. Output is a name or nothing.

A wrong name is worse than no name. Automatic transcription emits shapes that are
not chords: two-note fragments, a note plus its own octave, a melody note landing
on top of a triad. The algorithm below is built to refuse those, and on the
reference song it refuses 143 of the 438 multi-pitch-class events. That refusal
rate is the design working, not a gap to close.

### Step 1 — reduce

```
bass_pc = min(pitches) % 12
pcs     = sorted(set(p % 12 for p in pitches))
```

`bass_pc` is taken from the lowest **MIDI pitch**, before the set reduction. This
is the one and only place register matters; everything after this is pitch
classes. Input order is irrelevant — the bass is the lowest pitch, not the first
element.

Octave doubling therefore collapses. `[48, 60, 64, 67]` and `[60, 64, 67]` are
both `C`: the doubled root adds no pitch class and changes no name. `[60, 72]`
reduces to a single pitch class and is not a chord at all. On the reference song
90 events are multi-note but collapse to one pitch class this way, and all 90
correctly return nothing.

If `len(pcs) < 2`, return nothing.

### Step 2 — dyads

Two pitch classes carry a name only when the interval is a perfect fifth or its
inversion. Let `a`, `b` be the two classes and `iv = (b - a) % 12`.

- `iv == 7` — root is `a`; name is `<root>5`.
- `iv == 5` — the fourth is a fifth upside down, so the root is `b`; name is
  `<root>5`, and the bass is the fifth, so it takes a slash (§5.5).
- anything else — return nothing.

Every other dyad is refused because it does not determine a chord. A major third
C–E belongs to C, Cmaj7, Am, Am7, Fmaj7 and more; naming it "C" is a guess wearing a
label. On the reference song this refuses 133 third and sixth dyads, 8 second
dyads and 1 tritone.

### Step 3 — templates

For three or more pitch classes, try **every** pitch class in the set as a
candidate root. For candidate `r`, the interval set is
`I = { (pc - r) % 12 for pc in pcs }`, which always contains 0. A candidate
matches a quality when `I` **equals** the template exactly — not a superset, not a
subset.

| Quality | Intervals | Suffix | Rank |
| --- | --- | --- | --- |
| major | 0 4 7 | *(none)* | 1 |
| minor | 0 3 7 | `m` | 2 |
| dominant 7 | 0 4 7 10 | `7` | 3 |
| minor 7 | 0 3 7 10 | `m7` | 4 |
| major 7 | 0 4 7 11 | `maj7` | 5 |
| 6 | 0 4 7 9 | `6` | 6 |
| minor 6 | 0 3 7 9 | `m6` | 7 |
| sus4 | 0 5 7 | `sus4` | 8 |
| sus2 | 0 2 7 | `sus2` | 9 |
| add9 | 0 2 4 7 | `add9` | 10 |
| minor add9 | 0 2 3 7 | `madd9` | 11 |
| diminished | 0 3 6 | `dim` | 12 |
| augmented | 0 4 8 | `aug` | 13 |
| half-diminished | 0 3 6 10 | `m7b5` | 14 |
| diminished 7 | 0 3 6 9 | `dim7` | 15 |

Exact matching is what makes the refusals happen. A major triad with a semitone
neighbour glued on, `[60, 61, 64, 67]`, matches nothing at any root and returns
nothing — which is right, because it is a C chord with a passing note in it, not a
chord with a name.

The table tops out at four pitch classes. **Five or more distinct pitch classes
never match and always return nothing.** `[60, 64, 67, 71, 74]` is a legitimate
Cmaj9 and is still refused: in this pipeline a fifth pitch class arriving from
automatic transcription is more often a neighbouring melody note or an
octave-error artifact than a real extension, and the honest answer is silence.
The reference song contains no event with five or more distinct pitch classes at
all, so this branch is covered by synthetic vectors only.

### Step 4 — choosing the root

Several candidates can match at once, from two directions. Symmetric shapes match
at many roots: augmented at three, diminished 7 at four. And some templates are
rotations of each other — `C6` and `Am7` are the same four pitch classes, and
`Csus2` and `Gsus4` are the same three.

Sort every match by this key and take the first:

1. **`0` if the candidate root equals `bass_pc`, else `1`.**
2. Quality rank from the table above, ascending.
3. Root pitch class, ascending.

The bass dominating is the point. A guitarist who wants a chord heard as `C6`
puts C in the bass, and one who wants `Am7` puts A there — the same four notes,
and the bass note is the player telling you which. So `[60, 64, 67, 69]` is `C6`
and `[57, 60, 64, 67]` is `Am7`, from one rule rather than two special cases. The
same rule resolves `Csus2` against `Gsus4`, and picks `Caug` out of the three
roots that fit `[60, 64, 68]`.

Rank only decides cases where the bass matches no template at all. There sus4
beats sus2 and `m7` beats `6` because those are the more common readings in
guitar transcription; the ordering is a convention chosen for determinism, and
its only hard requirement is that both surfaces use it.

Key element 3 never decides a winner. Only two templates match at more than one
root, `aug` and `dim7`, because only those are transpositionally symmetric — and
in both, *every* pitch class in the set is a matching root, the bass included. So
exactly one candidate scores 0 on element 1 and takes it outright. Checked
exhaustively over all 3- to 6-note pitch-class sets and every choice of bass
within each: element 3 breaks a tie in 0 of them. It is specified anyway, so the
sort is total and no implementation can depend on its language's sort stability.

### Step 5 — spelling and slash

Pitch classes are spelled with sharps, from the same twelve-name table
`src/kilix_playalong/tablature.py` already uses for tuning labels:

```
C  C#  D  D#  E  F  F#  G  G#  A  A#  B
```

So the flat-key chord in the reference song is written `A#`, not `Bb`. This is
deliberate. Correct enharmonic spelling needs a key signature, and nothing in
this pipeline produces one; inventing flats for some chords and not others would
be less consistent than being uniformly sharp, and would disagree with the string
labels on the same screen.

When the winning root's pitch class is not `bass_pc`, append `/` and the bass
name: `C/E`, `Am/E`, `Dm/F`, `C5/G`, `Gsus4/D`. When they are equal, no slash.

### Worked examples from the reference song

| t (s) | Positions (string, fret) | Pitches | Name |
| --- | --- | --- | --- |
| 3.101 | (1,3) (3,0) (4,1) | 48 55 60 | `C5` |
| 9.084 | (2,3) (3,3) | 53 58 | `A#5/F` |
| 110.423 | (0,12) (1,15) (3,0) | 52 55 60 | `C/E` |
| 10.931 | (2,0) (3,0) (4,1) | 50 55 60 | `Gsus4/D` |
| 202.619 | (1,3) (2,2) (3,0) (5,7) | 48 52 55 71 | `Cmaj7` |
| 69.705 | (0,8) (1,8) (2,7) (3,5) (4,6) (5,8) | 48 53 57 60 65 72 | `F/C` |
| 109.215 | (0,6) (1,8) (2,8) (3,5) (4,3) (5,6) | 46 53 58 60 62 70 | `A#add9` |
| 131.417 | (2,0) (3,5) (5,5) | 50 60 69 | *(nothing)* |
| 145.903 | (2,4) (3,5) | 54 60 | *(nothing)* |
| 11.303 | (1,3) (2,2) | 48 52 | *(nothing)* |

`131.417` is root, fifth and minor seventh with no third: `D7` and `Dm7` are
equally consistent with it, so nothing is the only honest output.

Fixture: `chords.cases` — 42 cases read from the reference transcription and 33
synthetic, of which 18 must return `null`.

## 6. Hand position

The hand marker answers "where on the neck am I about to be", so it is computed
over a window around the playhead, not from the current event.

```
window = [t - 0.5, t + 1.5]        seconds
```

An event is in the window when `event.end > t - 0.5 && event.start < t + 1.5`.
The window leans forward on purpose: the marker has to arrive before the player
does, or it is telling them where they already were. 0.5 s back and 1.5 s forward
is 2 s total, about six notes at the reference song's 0.348 s median duration.

```
fretted = sorted(f for f in window_frets if f >= 1)
if fretted is empty:  no hand position
c  = fretted[(len(fretted) - 1) // 2]        # lower median
lo = c - 2
hi = c + 2
if lo < 1:         hi += 1 - lo;        lo = 1
if hi > max_fret:  lo -= hi - max_fret; hi = max_fret; if lo < 1: lo = 1
```

**Open strings are excluded.** Fret 0 needs no hand, and it is not at a position —
it is the nut doing the work. Including it would drag every window's low edge to
the nut and report first position for a passage played at the twelfth fret with
one open drone under it. This exclusion is why `[0, 0, 5, 7]` gives `[3, 7]`
rather than `[1, 5]`.

**The box is a fixed five frets, placed by the median, not stretched to cover
everything.** A naive min-to-max span over this window on the reference song has
a median width of 7 frets, a 90th percentile of 12 and a maximum of 19 — which is
not a hand, it is the whole neck. A hand covers about five frets, so the box is
five frets wide and the median puts it where most of the notes are. Outliers fall
outside it, correctly: `[2, 3, 15, 16]` reports `[1, 5]`, because two notes at the
15th fret in a two-second window is a reach or a transcription error, not the
hand moving there.

The lower median (`(len - 1) // 2`, not an average of the two middle values) is
specified so the result is always an integer fret and both languages agree without
a rounding rule.

Measured on the reference song, sampling every 100 ms: the box contains **81.9%**
of the fretted notes in its own window and changes **0.65 times per second**. The
remaining 18.1% are the outliers the box is designed to leave out. A surface may
show notes outside the box; it must not resize the box to include them.

When there is no hand position — a rest, or a passage of open strings only — the
marker is hidden rather than moved. `null` means hide. On the reference song this
happens in 3 of 2161 sampled frames.

Fixture: `hand_position.cases` — 9 windows from the reference song and 11
synthetic cases covering the empty window, the all-open window, clamping at both
ends of the neck, and outliers on both sides of the median.

## Measured behaviour

Everything above was checked against `tab/guitar-tab.json` from the reference
project, schema `kilix.playalong.tab/v1`: 937 events, last event ending at
215.283 s, standard tuning `[40, 45, 50, 55, 59, 64]`, `max_fret` 20, observed
frets 0-19, durations 0.094 s to 6.052 s with a median of 0.348 s.

Polyphony, by number of positions per event:

| Positions | 1 | 2 | 3 | 4 | 5 | 6 |
| --- | --- | --- | --- | --- | --- | --- |
| Events | 409 | 290 | 145 | 63 | 26 | 4 |

528 events carry simultaneous notes. Naming results:

| Outcome | Events |
| --- | --- |
| One pitch class — not a chord | 499 (409 single notes, 90 octave doublings) |
| Named | 295 |
| Refused | 143 |

Of the 438 events with two or more distinct pitch classes, **295 are named
(67.4%)** across 31 distinct names. The 143 refusals are 133 third and sixth
dyads, 8 second dyads, 1 tritone dyad, and 1 triad with no third.

The song's 31 names are: `C5` `A#5` `C` `A#` `C5/G` `D5` `F` `A#5/F` `Fsus2`
`F5` `A#/F` `C/E` `G5/D` `Dm` `Csus4` `Gsus4/D` `Dsus2` `F/C` `G5` `D5/A`
`Fadd9` `Am/E` `Gm` `Dm/F` `A#add9` `F/A` `Csus2` `Cm` `C/G` `Cmaj7` `F5/C`.

That list is worth reading as a warning. A real transcription exercises power
chords, triads, inversions, sus chords and add9, and exactly one seventh chord.
It contains no `7`, `m7`, `6`, `m6`, `dim`, `aug`, `m7b5`, `dim7` or `madd9` — so
those nine templates are covered by synthetic vectors only, and the first real
song that does contain them will be the first time that code runs on real input.

No event in the reference song has five or more distinct pitch classes, so the
five-pitch-class refusal in §5.3 is likewise synthetic-only.

## Conformance

`tests/fixtures/fretboard_vectors.json` is the contract. Both surfaces must
reproduce every value in it.

| Fixture section | Pins |
| --- | --- |
| `geometry.fret_positions` | `d(n)`, n 0-24, tolerance 1e-9 |
| `geometry.cell_centres` | `cell(n)`, n 1-24 |
| `geometry.display_normalized` | `u(n, N)` for N in 12, 15, 20, 24 |
| `geometry.identities` | `d(12) = 0.5`, `d(24) = 0.75`, `1/d(1)` |
| `inlays.cases` | which frets, single or double, centre |
| `strings.cases` | api index, label, open pitch, player number, width ratio |
| `orientation.cases` | `row` and `{x, y}` for both orientations |
| `chords.cases` | 75 pitch sets to a name or `null` |
| `hand_position.cases` | 20 fret windows to a box or `null` |

A chord case's `expect` of `null` is as binding as any name. An implementation
that returns a name there fails the fixture, and it should — inventing a name for
`[54, 60]` is the defect this whole section exists to prevent.

Vectors are values, not test code. Each surface writes its own runner: the C
suite parses the fixture with the existing `kpa_json` reader, and the browser
suite fetches it same-origin. Neither may hardcode a value this file defines.
