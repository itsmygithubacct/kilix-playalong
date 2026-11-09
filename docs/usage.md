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
