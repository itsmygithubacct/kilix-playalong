# Architecture

Kilix Playalong is a local batch pipeline followed by a read-only loopback web
player. The batch side owns acquisition and analysis; the browser never starts
jobs, reads arbitrary files, or talks to third-party services.

## Project flow

| Stage | Provider | Stable output |
| --- | --- | --- |
| Download | locked `yt-dlp` module | one source media file and optional VTT captions |
| Normalize | FFmpeg | 44.1 kHz stereo PCM WAV |
| Separate | pinned Demucs revision | independently addressable lossless stems |
| Lyrics | captions, supplied text, or faster-whisper | versioned timed-cue JSON |
| Transcribe guitar | Basic Pitch ONNX child | MIDI and stable note-event JSON |
| Tablature | internal deterministic dynamic program | timed string/fret JSON and ASCII tab |
| Export | internal renderer | self-contained printable HTML |

Every stage records its provider, settings fingerprint, timestamps, artifact
size, and SHA-256 digest in `project.state`. Resume accepts a cached stage only
when its fingerprint and every artifact still match. A changed input or setting
invalidates the affected stage and all downstream stages.

Provider calls run as argument vectors without a shell, in separate process
groups with timeouts, incrementally bounded diagnostics, private caches, a
minimal allowlisted environment, a disposable private home, and path/URL
redaction. The optional heavyweight Python providers run in child interpreters
so their imports and failures do not destabilize the command process. Provider
wrappers must pass any required cache or module path explicitly.

## Private project data

Projects use the XDG data directory and have this logical shape:

```text
projects/song-…/
  project.state
  source/
  media/normalized.wav
  stems/{vocals,drums,bass,guitar,piano,other}.wav
  lyrics/lyrics.json
  midi/{guitar.mid,guitar-notes.json}
  tab/guitar-tab.json
  exports/{guitar-tab.txt,playalong.html}
```

Directories are created with mode `0700` and artifacts with mode `0600`.
Manifests are atomically persisted with integrity checking through the shared
`kilix-state` module. The original URL exists only inside this private manifest;
normal CLI inspection omits it.

## Fingering inference

Note events within a short onset window become chord candidates. For each pitch,
the engine enumerates playable string/fret positions for the selected tuning and
fret limit, rejects duplicate-string and excessive-span combinations, and then
uses dynamic programming across time. The cost favors small fret spans, modest
hand positions, compact string use, stable pitch-to-string assignment, and small
position shifts. Identical input and settings therefore produce identical tab.

This is a practical fingering estimate, not score understanding: bends, slides,
harmonics, alternate voices, and performance technique are not inferred.

## Player clock

The web player creates one audio element per stem. One ready stem is the master
timeline; play, pause, seek, and rate changes are applied as a transaction to all
elements. A lightweight correction loop brings stems that exceed the drift
threshold back to the master time. Buffering is treated as a group condition so
one stalled stem cannot quietly leave the mix out of phase.

Lyrics and tab are indexed by their start times and selected against that same
clock. Muting vocals or any individual track changes only its audible level, not
its timeline, preserving synchronization when it is restored.

## Loopback boundary

`serve` binds only to `127.0.0.1` and creates a new high-entropy URL capability
for each launch. The server validates `Host`, accepts only `GET` and `HEAD` on a
small route table, supports byte ranges for stems, disables caching and
referrers, and sends a restrictive Content Security Policy. The browser client
uses same-origin data only and constructs user-controlled text with DOM text
nodes rather than HTML injection.
