# Using Playalong

Creating, resuming and playing projects, exporting a songbook, the player
keys, and where projects are stored. This text was moved from the README; the
[README](../README.md) is the short entry point.

## Create and play

```bash
uv run --frozen kilix-playalong models install
uv run --frozen kilix-playalong create \
  'https://www.youtube.com/watch?v=VIDEO_ID' \
  --i-have-rights

uv run --frozen kilix-playalong list
uv run --frozen kilix-playalong serve song-PROJECT_ID
```

Supply known lyrics or choose another tuning when appropriate:

```bash
uv run --frozen kilix-playalong create 'https://youtu.be/VIDEO_ID' \
  --i-have-rights \
  --lyrics song.lrc \
  --tuning drop-d \
  --max-fret 22
```

The default `--whisper-model auto` picks `large-v3` when it is installed and
CUDA or at least 10 GiB of memory is available, and `medium.en` otherwise.
`medium.en` transcribes English only; a project that asks for another language
needs `large-v3`. An explicit model name always overrides this selection.

## Where the lyrics come from

`--lyrics-source auto`, the default, takes the first of these that answers:

1. a lyrics file passed with `--lyrics`;
2. an `.lrc` sidecar beside a local source file;
3. a lyrics tag inside a local source file;
4. the video's own caption track;
5. published lyrics from [LRCLIB](https://lrclib.net), looked up by title and
   artist;
6. a faster-whisper transcription of the vocals.

Each can be named instead: `file`, `sidecar`, `embedded`, `captions`, `lrclib`
or `transcribe`. A named source that has nothing to offer fails rather than
falling back to another one.

The LRCLIB lookup is the only lyric route that uses the network. It sends an
HTTPS request carrying the title (with asides such as "(Official Video)" and a
leading "Artist - " removed) and the artist, with a `kilix-playalong/VERSION`
User-Agent and nothing else. An entry is used only when its title and artist
match and its length is within 10 seconds of the normalized audio
(`media/normalized.wav`). A source's listed length is often a whole number of
seconds, which is enough to pick a different recording; if the normalized file
cannot be read, that listed length is used instead. The chosen entry is still
parsed against the listed length. When that finds nothing and the title reads
"Song - Artist" (or "Artist - Song"), one second request sends the whole title
instead, because a video's artist is often only its uploader; the entry's artist
may then match either half of the title. The chosen entry is saved privately in
the project as `source/lyrics-lrclib.json`, and a resume reuses it without asking
again; a miss is remembered for the same title and artist. When LRCLIB cannot be
reached, `auto` transcribes instead and the lyrics stage note says why. A later
`auto` run keeps that finished transcription and does not ask again: a resume, a
rename, and a project that was made offline. A change that re-runs the lyrics
stage, such as `--audio-source`, does ask, and `--lyrics-source lrclib` always
does. `--no-lyrics-lookup` keeps `auto` off the network, and `--lyrics-source
transcribe` never contacts LRCLIB.

LRCLIB supplies the words; faster-whisper still runs, to time each word against
the singing. If that alignment cannot run or is rejected, the entry's own line
timings are kept, or, for an entry without them, the lines are spread evenly.
The manifest records which under `lyrics.lookup.timing` (`aligned`, `lrc-lines`
or `estimated`), beside the entry id, the query and when it was retrieved; the
lyrics carry the source id `lrclib:ID`, with `-aligned` once the words are
timed.

The create command prints the project id and printable export path. An
interrupted project resumes from verified artifacts:

```bash
uv run --frozen kilix-playalong resume song-PROJECT_ID
```

## Export a guitar songbook

```bash
uv run --frozen kilix-playalong songbook song-PROJECT_ID --output ./my-songbook
```

Open `my-songbook/index.html` in a browser. Keep its MP3 files beside it when
copying the folder to another computer. This command uses existing project
artifacts and FFmpeg; it does not download media or run the ML models again.
Use `--no-audio` for a smaller chart suitable for printing.

Lyrics appear beside the corresponding tab phrases, with note names under each
fret. Use **Edit section markers** to add verses, choruses, bridges, and outros,
set their start times, or remove markers. New projects begin with one **Song**
section; section boundaries are not automatically classified. Click a fret or
phrase to play that moment, switch between the full song and isolated guitar,
slow playback, or loop a section. Each phrase has its own **Loop** button for
repeating a single line. The player's loop button stops either kind of loop;
seeking outside its range clears it. Section edits keep an active section loop
inside its updated boundaries. **Print / Save PDF** prints the chart.

The lyric Edit buttons and section editor save corrections in browser storage
when available. Download corrections to back them up or transfer them. Corrections
are tied to the transcript and initial arrangement: a regenerated transcript
cannot silently apply an older line's edit to a different line. These browser
edits do not change the project's source files. Storage behavior for `file:`
pages varies by browser, so keep an exported corrections file.

To start with corrected lyrics and known section markers:

```bash
uv run --frozen kilix-playalong songbook song-PROJECT_ID \
  --lyrics corrected.lrc --arrangement sections.json --output ./my-songbook
```

`sections.json` contains increasing starts in seconds; the first must be zero:

```json
{"sections":[
  {"id":"intro","label":"Intro","kind":"intro","start":0},
  {"id":"verse-1","label":"Verse 1","kind":"verse","start":12},
  {"id":"chorus-1","label":"Chorus 1","kind":"chorus","start":40}
]}
```

The exporter validates times, tuning, and fret/pitch consistency before replacing
an existing page. Audio conversions finish before the new page is published.
Old audio files may remain after exporting changed recordings; remove them only
after confirming no retained page uses them.

## Keyboard controls for the multitrack player

| Key | Action |
| --- | --- |
| Space | Play or pause |
| Left / Right | Seek five seconds |
| `V` | Toggle vocals |
| `L` | Show or hide lyrics |

## Storage and recovery

Projects live below `${XDG_DATA_HOME:-$HOME/.local/share}/kilix-playalong/projects`.
Model caches use `${XDG_CACHE_HOME:-$HOME/.cache}/kilix-playalong`. Directories
are private to the current user. Each stage records its provider, timestamps,
artifact sizes, and SHA-256 digests. Resume skips only artifacts that still
match; a changed artifact invalidates every downstream stage.

The project manifest uses `kilix.playalong.project/v1`. Timed lyrics and tab use
`kilix.playalong.lyrics/v1` and `kilix.playalong.tab/v1`. Uninstalling the code
does not delete projects or exports.
