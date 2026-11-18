# What Playalong provides

The detailed feature list, including the intake and download limits. This text
was moved from the README; the [README](../README.md) is the short entry
point.

## What it provides

- YouTube intake through a locked `yt-dlp`. Non-HTTPS or non-YouTube hosts, URLs
  carrying credentials or a non-standard port, and playlist URLs are rejected
  before any network call at all; the path is percent-decoded, lowercased and
  re-split before that compare, so `//playlist` and `/%70laylist` are caught
  there too, not only a plain `/playlist` or a `/watch?list=` with no `v=`.
  Live and upcoming streams, a source with no finite positive duration, songs
  longer than 30 minutes, and a playlist that reached the extractor by a
  spelling the pre-network gate does not model are rejected from the metadata
  document, before any media is fetched. A `watch?v=ID&list=LIST` URL is
  narrowed to the single requested video by `--no-playlist`, never expanded.
- Downloads carry a 512 MiB `--max-filesize` ceiling, and the finished file is
  re-checked against that same limit on disk. yt-dlp can refuse a transfer up
  front only where the server declares the size; a segmented download is bounded
  while it runs by the 30-minute duration gate and the `bestaudio/best` format
  choice rather than by any byte counter, so an oversized segmented source is
  rejected only once its bytes have been written.
- Six-stem Demucs separation (`vocals`, `drums`, `bass`, `guitar`, `piano`, and
  `other`) with a common playback clock plus mute and level controls for every
  stem.
- Timed YouTube captions when available, user-supplied LRC/SRT/VTT/plain text,
  published lyrics looked up on LRCLIB by title, artist and length (optional,
  source-labelled, and timed against the singing), or local faster-whisper
  transcription of the isolated vocal stem.
- Basic Pitch ONNX transcription of the isolated guitar stem to MIDI, followed
  by deterministic string/fret inference for standard, Drop D, or DADGAD tuning.
- A local play-along page with synchronized lyrics and tab highlighting,
  selectable lyrics, tempo control, seeking, keyboard control, and a vocals-off
  practice preset.
- Printable HTML and ASCII tab exports. Printed lyrics and tab can each be
  hidden independently.
- A portable songbook with lyrics beside the tab, note names, detected chord
  labels, editable verse/chorus markers, section loops, and full-song/guitar audio.
- Resumable, checksummed stages and private XDG storage backed by the shared
  `kilix-state` library.
