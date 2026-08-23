'use strict';

/*
 * Kilix Playalong's browser surface is deliberately dependency-free. The
 * server supplies capability-token-prefixed, same-origin URLs; the browser
 * only reads those URLs and never sends project data elsewhere.
 */

const app = document.querySelector('#app');
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

const elements = {
  loading: document.querySelector('#loading-state'),
  loadingMessage: document.querySelector('#loading-message'),
  error: document.querySelector('#error-state'),
  errorMessage: document.querySelector('#error-message'),
  retry: document.querySelector('#retry-button'),
  empty: document.querySelector('#empty-state'),
  emptyMessage: document.querySelector('#empty-message'),
  shell: document.querySelector('#player-shell'),
  connection: document.querySelector('#connection-status'),
  projectKicker: document.querySelector('#project-kicker'),
  title: document.querySelector('#project-title'),
  artist: document.querySelector('#project-artist'),
  heroDuration: document.querySelector('#hero-duration'),
  notice: document.querySelector('#inline-notice'),
  print: document.querySelector('#print-button'),
  printable: document.querySelector('#printable-link'),
  trackList: document.querySelector('#track-list'),
  play: document.querySelector('#play-button'),
  playGlyph: document.querySelector('.play-glyph'),
  timeline: document.querySelector('#timeline'),
  elapsed: document.querySelector('#elapsed-time'),
  remaining: document.querySelector('#remaining-time'),
  transportState: document.querySelector('#transport-state'),
  backFive: document.querySelector('#back-five'),
  forwardFive: document.querySelector('#forward-five'),
  resetPosition: document.querySelector('#reset-position'),
  rate: document.querySelector('#playback-rate'),
  vocals: document.querySelector('#toggle-vocals'),
  lyricsToggle: document.querySelector('#toggle-lyrics'),
  lyricsCard: document.querySelector('#lyrics-card'),
  lyricsStatus: document.querySelector('#lyrics-status'),
  lyricsViewport: document.querySelector('#lyrics-viewport'),
  lyricsList: document.querySelector('#lyrics-list'),
  lyricsEmpty: document.querySelector('#lyrics-empty'),
  lyricsHiddenNote: document.querySelector('#lyrics-hidden-note'),
  tabStatus: document.querySelector('#tab-status'),
  tabViewport: document.querySelector('#tab-viewport'),
  tabGrid: document.querySelector('#tab-grid'),
  tabGutter: document.querySelector('#tab-gutter'),
  tabEmpty: document.querySelector('#tab-empty'),
  tuning: document.querySelector('#tuning-label'),
  fret: document.querySelector('#fret-label'),
  tabPosition: document.querySelector('#tab-position'),
  ascii: document.querySelector('#ascii-download'),
  midi: document.querySelector('#midi-download'),
};

const DEFAULT_TUNING = [
  { midi: 40, label: 'E' },
  { midi: 45, label: 'A' },
  { midi: 50, label: 'D' },
  { midi: 55, label: 'G' },
  { midi: 59, label: 'B' },
  { midi: 64, label: 'E' },
];

const HAVE_METADATA = 1;
const HAVE_FUTURE_DATA = 3;

// Drift the corrector is allowed to leave standing while the transport runs.
const DRIFT_TOLERANCE = 0.12;
// How far off state.position a stem may be and still keep its buffer on resume. Two
// stems can sit on opposite sides of the position, so the spread a resume may leave is
// twice this: 5 ms keeps that at 10 ms, an order of magnitude below DRIFT_TOLERANCE and
// under the ~20 ms at which two attacks are heard as a flam. The relation is the
// invariant — a resume must never preserve drift the corrector merely tolerated.
const RESUME_ALIGNMENT = 0.005;
// How long a stem may sit below HAVE_FUTURE_DATA while the transport runs before it
// counts as a real shortfall rather than a seek settling. A seek into fully buffered
// data settles in ~10 ms; 500 ms is two orders above that, and short enough that the
// transport never claims to be playing through a silence a listener would notice.
const STARVATION_GRACE_MS = 500;

const NOTICE_TRACK = 'track';
const NOTICE_TRANSPORT = 'transport';
const NOTICE_LAYER = 'layer';
// One bar, three standing slots, highest priority first. A transient transport message
// covers a standing track or layer message and uncovers it again when it clears, so a
// permanently dead stem is never silently forgotten after a stall recovers.
// NOTICE_TRANSPORT is the only transient slot, and so the only one whose raisers need a
// clearer on every path that ends the wait: NOTICE_TRACK is derived from track.error by
// refreshTrackNotice and NOTICE_LAYER from a layer that would not load, and both stay
// true until resetNotices runs on the next project load. The transport slot is raised by
// commitBufferingPause and by playAll's failure branch, and cleared by
// resumeWhenStemsReady, by playAll on success, and by pauseAll.
const NOTICE_ORDER = [NOTICE_TRANSPORT, NOTICE_TRACK, NOTICE_LAYER];

const state = {
  project: null,
  tracks: [],
  audio: [],
  lyrics: [],
  lyricsResolved: false,
  tab: null,
  tabEvents: [],
  tabStringCount: DEFAULT_TUNING.length,
  duration: 0,
  position: 0,
  rate: 1,
  playing: false,
  playToken: 0,
  resumeAfterBuffering: false,
  masterIndex: 0,
  vocalsEnabled: true,
  lyricsVisible: true,
  activeCue: -1,
  activeTabEvent: -1,
  frame: 0,
  noticeKind: '',
  notices: {},
  preferenceKey: '',
  preferencesTouched: false,
  printUrl: '',
  printReady: false,
  exportToken: 0,
};

function text(value, fallback = '') {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function number(value, fallback = 0) {
  return Number.isFinite(Number(value)) ? Number(value) : fallback;
}

function clamp(value, low, high) {
  return Math.min(high, Math.max(low, value));
}

function formatTime(seconds) {
  const total = Math.max(0, Math.floor(number(seconds)));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const remainder = total % 60;
  if (hours > 0) {
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
  }
  return `${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`;
}

function endpoint(value) {
  if (typeof value !== 'string' || !value || value.startsWith('//')) return null;
  let parsed;
  try {
    parsed = new URL(value, document.baseURI);
  } catch (_error) {
    return null;
  }
  const basePath = window.location.pathname.endsWith('/')
    ? window.location.pathname
    : `${window.location.pathname}/`;
  if (parsed.origin !== window.location.origin || !parsed.pathname.startsWith(basePath)) {
    return null;
  }
  return parsed.href;
}

async function readJson(url, label) {
  const safeUrl = endpoint(url);
  if (!safeUrl) throw new Error(`${label} returned an unsafe or missing URL.`);
  const response = await fetch(safeUrl, {
    method: 'GET',
    headers: { Accept: 'application/json' },
    credentials: 'same-origin',
  });
  if (!response.ok) throw new Error(`${label} could not be loaded (${response.status}).`);
  return response.json();
}

function showOnly(view) {
  elements.loading.hidden = view !== 'loading';
  elements.error.hidden = view !== 'error';
  elements.empty.hidden = view !== 'empty';
  elements.shell.hidden = view !== 'player';
}

function setConnection(label, tone = 'neutral') {
  elements.connection.replaceChildren();
  const dot = document.createElement('span');
  dot.className = `status-dot status-${tone}`;
  dot.setAttribute('aria-hidden', 'true');
  elements.connection.append(dot, document.createTextNode(label));
}

function renderNotice() {
  // The bar always shows the highest-priority slot that is still true, so clearing a
  // transient message restores the standing one instead of blanking over it.
  const kind = NOTICE_ORDER.find((name) => state.notices[name]) || '';
  state.noticeKind = kind;
  const entry = kind ? state.notices[kind] : null;
  if (!entry) {
    elements.notice.hidden = true;
    elements.notice.replaceChildren();
    return;
  }
  elements.notice.className = `notice-bar notice-${entry.tone}`;
  elements.notice.textContent = entry.message;
  elements.notice.hidden = false;
}

function showNotice(message, tone, kind) {
  // kind is required and has to be one of NOTICE_ORDER: renderNotice reads those three
  // slots and nothing else, so a message filed under any other name would be stored and
  // never shown. test_every_notice_is_filed_in_a_slot_the_bar_renders pins every call
  // site rather than a default doing it silently.
  if (!message) {
    clearNotice(kind);
    return;
  }
  state.notices[kind] = { message, tone };
  renderNotice();
}

function clearNotice(kind) {
  if (!state.notices[kind]) return;
  delete state.notices[kind];
  renderNotice();
}

function resetNotices() {
  state.notices = {};
  renderNotice();
}

function preferenceDefaults() {
  return {
    rate: 1,
    vocalsEnabled: true,
    lyricsVisible: true,
    tracks: {},
  };
}

function loadPreferences(projectId) {
  state.preferenceKey = `kilix-playalong:prefs:${encodeURIComponent(projectId)}`;
  const defaults = preferenceDefaults();
  try {
    const raw = window.localStorage.getItem(state.preferenceKey);
    if (!raw) return defaults;
    const saved = JSON.parse(raw);
    if (!saved || typeof saved !== 'object') return defaults;
    const preferences = { ...defaults, ...saved, tracks: { ...defaults.tracks, ...(saved.tracks || {}) } };
    preferences.rate = [0.5, 0.75, 1, 1.25, 1.5].includes(Number(preferences.rate))
      ? Number(preferences.rate)
      : 1;
    preferences.vocalsEnabled = preferences.vocalsEnabled !== false;
    preferences.lyricsVisible = preferences.lyricsVisible !== false;
    return preferences;
  } catch (_error) {
    return defaults;
  }
}

function savePreferences() {
  if (!state.preferenceKey || !state.preferencesTouched) return;
  const tracks = {};
  state.tracks.forEach((track) => {
    // Only a deliberate deviation is stored, so a changed server default still wins.
    // volume has no server-side default to deviate from today; the day the manifest
    // grows one, it has to move under the same rule or it will freeze the same way.
    tracks[track.id] = track.muted === Boolean(track.defaultMuted)
      ? { volume: track.volume }
      : { muted: track.muted, volume: track.volume };
  });
  try {
    window.localStorage.setItem(state.preferenceKey, JSON.stringify({
      rate: state.rate,
      vocalsEnabled: state.vocalsEnabled,
      lyricsVisible: state.lyricsVisible,
      tracks,
    }));
  } catch (_error) {
    // Storage is a convenience; playback remains usable when it is blocked.
  }
}

function rememberPreferences() {
  state.preferencesTouched = true;
  savePreferences();
}

function isVocalTrack(track) {
  return track.kind === 'vocals' || track.kind === 'vocal' || track.id === 'vocals';
}

function applyPreferences(preferences) {
  state.rate = preferences.rate;
  state.vocalsEnabled = preferences.vocalsEnabled;
  state.lyricsVisible = preferences.lyricsVisible;
  state.tracks.forEach((track) => {
    const saved = preferences.tracks[track.id];
    track.volume = clamp(number(saved && saved.volume, 1), 0, 1);
    track.muted = saved && Object.prototype.hasOwnProperty.call(saved, 'muted')
      ? Boolean(saved.muted)
      : Boolean(track.defaultMuted);
  });
  elements.rate.value = String(state.rate);
}

function setButtonState(button, on, labelOn, labelOff) {
  button.classList.toggle('is-on', on);
  button.setAttribute('aria-pressed', String(on));
  const label = on ? labelOn : labelOff;
  const copy = button.querySelector('.toggle-copy');
  if (copy) copy.textContent = label;
}

function updateLayerControls() {
  const vocalTracks = state.tracks.filter(isVocalTrack);
  const vocalsOn = vocalTracks.length > 0 && vocalTracks.some((track) => !track.muted);
  state.vocalsEnabled = vocalsOn;
  elements.vocals.disabled = vocalTracks.length === 0;
  setButtonState(elements.vocals, vocalsOn, 'Guide vocals', 'Guide vocals off');
  setButtonState(elements.lyricsToggle, state.lyricsVisible, 'Lyrics on screen', 'Lyrics hidden');
  elements.lyricsCard.classList.toggle('layer-hidden', !state.lyricsVisible);
  updateLyricsPlaceholders();
}

function updateLyricsPlaceholders() {
  // Both placeholders are attribute-gated, like every other conditional block in the
  // document, so at most one of them can ever be in the accessibility tree even if the
  // stylesheet never loads. The CSS rules only style what these attributes allow.
  elements.lyricsHiddenNote.hidden = state.lyricsVisible;
  // 'No timed lyrics' is a verdict, not a loading state: it waits for the fetch.
  elements.lyricsEmpty.hidden = !(
    state.lyricsVisible && state.lyricsResolved && !state.lyrics.length
  );
}

function updateAudioVolume(track) {
  const record = state.audio.find((item) => item.id === track.id);
  if (record) record.element.volume = track.muted ? 0 : track.volume;
}

function applyMuteButton(track) {
  const button = track.muteButton;
  if (!button) return;
  button.setAttribute('aria-pressed', String(track.muted));
  button.setAttribute('aria-label', `${track.muted ? 'Unmute' : 'Mute'} ${track.label}`);
  button.textContent = track.muted ? 'MUTED' : 'LIVE';
}

function updateTrackMute(track, muted) {
  track.muted = muted;
  updateAudioVolume(track);
  applyMuteButton(track);
  updateLayerControls();
  rememberPreferences();
}

function makeTrackControl(track) {
  const item = document.createElement('div');
  item.className = 'track-control';
  item.dataset.trackId = track.id;

  const top = document.createElement('div');
  top.className = 'track-control-top';
  const label = document.createElement('span');
  label.className = 'track-label';
  label.textContent = track.label;
  const kind = document.createElement('span');
  kind.className = 'track-kind';
  kind.textContent = text(track.kind, 'audio');
  top.append(label, kind);

  const row = document.createElement('div');
  row.className = 'track-control-row';
  const mute = document.createElement('button');
  mute.className = 'mute-button';
  mute.type = 'button';
  mute.disabled = Boolean(track.error);
  mute.addEventListener('click', () => updateTrackMute(track, !track.muted));
  track.muteButton = mute;
  applyMuteButton(track);

  const volumeLabel = document.createElement('label');
  volumeLabel.className = 'volume-wrap';
  const volume = document.createElement('input');
  volume.type = 'range';
  volume.min = '0';
  volume.max = '1';
  volume.step = '0.01';
  volume.value = String(track.volume);
  volume.setAttribute('aria-label', `${track.label} volume`);
  volume.disabled = Boolean(track.error);
  volume.addEventListener('input', () => {
    track.volume = clamp(number(volume.value, 1), 0, 1);
    updateAudioVolume(track);
    rememberPreferences();
  });
  track.volumeInput = volume;
  volumeLabel.append(volume);
  row.append(mute, volumeLabel);
  item.append(top, row);
  return item;
}

function applyTrackAvailability(track) {
  // Updated in place rather than by re-rendering #track-list: a stem failing while the
  // user is on a mute button would otherwise take the focus with it, which is the same
  // defect F27 closed for mute clicks.
  if (track.muteButton) track.muteButton.disabled = Boolean(track.error);
  if (track.volumeInput) track.volumeInput.disabled = Boolean(track.error);
}

function renderTrackControls() {
  elements.trackList.replaceChildren();
  if (!state.tracks.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small-copy';
    empty.textContent = 'No audio stems are available.';
    elements.trackList.append(empty);
    return;
  }
  state.tracks.forEach((track) => elements.trackList.append(makeTrackControl(track)));
}

function createAudioTracks() {
  state.audio.forEach((record) => record.element.remove());
  state.audio = [];
  state.tracks.forEach((track) => {
    const url = endpoint(track.url);
    if (!url) {
      track.error = 'Audio URL is unavailable.';
      // The same treatment a stem that fails later gets: its mute button and volume
      // slider would otherwise stay live over a stem the bar already names as
      // unloadable. The track controls are built before this runs, so both exist.
      applyTrackAvailability(track);
      return;
    }
    const audio = document.createElement('audio');
    audio.preload = 'auto';
    audio.setAttribute('aria-hidden', 'true');
    audio.tabIndex = -1;
    audio.src = url;
    audio.playbackRate = state.rate;
    audio.preservesPitch = true;
    audio.volume = track.muted ? 0 : track.volume;
    const record = {
      id: track.id,
      label: track.label,
      element: audio,
      failed: false,
      waiting: false,
      starvedSince: 0,
    };
    audio.addEventListener('loadedmetadata', () => {
      // A stem longer than the manifest duration widens the timeline and the tab lane
      // for every stem. That is deliberate: a stem the transport cannot reach is worse
      // than a lane with tail padding.
      if (Number.isFinite(audio.duration) && audio.duration > state.duration) {
        state.duration = audio.duration;
        updateDurationDisplay();
      }
    });
    audio.addEventListener('error', () => {
      record.failed = true;
      record.starvedSince = 0;
      record.waiting = false;
      track.error = 'Audio could not be loaded.';
      refreshTrackNotice();
      applyTrackAvailability(track);
      chooseMaster();
      if (!playableAudio().length) pauseAll();
      else resumeWhenStemsReady();
    });
    audio.addEventListener('waiting', () => pauseForBuffering(record));
    audio.addEventListener('stalled', () => pauseForBuffering(record));
    audio.addEventListener('canplay', () => resumeAfterBuffering(record));
    audio.addEventListener('playing', () => resumeAfterBuffering(record));
    audio.addEventListener('ended', () => {
      if (state.audio[state.masterIndex] && state.audio[state.masterIndex].element === audio) {
        pauseAll();
        state.position = state.duration;
        updatePlaybackUi();
      }
    });
    app.append(audio);
    state.audio.push(record);
  });
  chooseMaster();
  // Covers the stems rejected above for an unusable URL, which raise no 'error' event.
  refreshTrackNotice();
}

function playableAudio() {
  return state.audio.filter((record) => !record.failed);
}

function refreshTrackNotice() {
  // Derived from every failed stem rather than from the one that failed last, so a
  // second failure cannot erase the first, and the bar cannot claim other tracks
  // remain available when none do.
  const labels = state.tracks.filter((track) => track.error).map((track) => track.label);
  if (!labels.length) {
    clearNotice(NOTICE_TRACK);
    return;
  }
  const names = labels.length === 1
    ? labels[0]
    : `${labels.slice(0, -1).join(', ')} and ${labels[labels.length - 1]}`;
  const remainder = playableAudio().length
    ? 'Other tracks remain available.'
    : 'No audio remains for this project.';
  showNotice(`${names} could not be loaded. ${remainder}`, 'warning', NOTICE_TRACK);
}

function chooseMaster() {
  const next = state.audio.findIndex((record) => !record.failed);
  state.masterIndex = next >= 0 ? next : 0;
}

function pauseForBuffering(record) {
  const audio = record.element;
  if (audio.readyState >= HAVE_FUTURE_DATA) return;
  // 'waiting' is only trustworthy when no seek is in flight: every seek drops readyState
  // to HAVE_METADATA and raises 'waiting' even when it lands in fully buffered data.
  // Seeks are judged by checkStemStarvation instead, on how long the shortfall lasts —
  // measured, a seek into a hole can raise no event at all, so no listener may be the
  // only detector. Outside a seek the audio has already stopped: pause at once.
  if (audio.seeking) return;
  commitBufferingPause(record);
}

function checkStemStarvation(now) {
  // Polled every frame while the transport runs, because the media element is not
  // obliged to tell us: a stem starved by a seek into an unbuffered region can sit at
  // HAVE_METADATA indefinitely without a single 'waiting' or 'stalled' event.
  // The poll rides requestAnimationFrame, which a background tab throttles or suspends
  // while its audio keeps playing. While hidden, the 'waiting' and 'stalled' listeners
  // remain the detector for every underrun that raises an event, and a starvation that
  // raises none is caught within a grace window of the tab coming back on screen.
  playableAudio().forEach((record) => {
    if (record.element.readyState >= HAVE_FUTURE_DATA) {
      record.starvedSince = 0;
      return;
    }
    if (!record.starvedSince) {
      record.starvedSince = now;
      return;
    }
    if (now - record.starvedSince >= STARVATION_GRACE_MS) commitBufferingPause(record);
  });
}

function commitBufferingPause(record) {
  // This wait has no timeout. A stem that never recovers keeps the whole group paused for
  // the rest of the session: nothing re-fetches it, the player view carries no retry
  // control, and pressing Play does not escape it — either the play promise rejects and
  // the bar asks for every stem to be ready, or the starvation poll commits this pause
  // again about STARVATION_GRACE_MS later, because the stem is still below
  // HAVE_FUTURE_DATA. Resuming without it would run the group silent where that stem
  // should sound, so the pause is deliberate, but the absence of any way out short of a
  // reload is a known limitation and not a handled case. A stem that fails outright is the
  // other story: it raises 'error', and the bar moves to the track slot — through
  // resumeWhenStemsReady while other stems are still playable, through pauseAll when none
  // are.
  record.starvedSince = 0;
  record.waiting = true;
  if (!state.playing) return;
  state.playing = false;
  state.resumeAfterBuffering = true;
  state.audio.forEach((item) => item.element.pause());
  setConnection('Paused for buffering', 'warning');
  showNotice(
    `${record.label} is buffering. Playback paused to keep every stem aligned.`,
    'warning',
    NOTICE_TRANSPORT,
  );
  updatePlaybackUi();
}

function resumeAfterBuffering(record) {
  if (record.element.readyState < HAVE_FUTURE_DATA) return;
  record.starvedSince = 0;
  record.waiting = false;
  resumeWhenStemsReady();
}

function resumeWhenStemsReady() {
  if (!state.resumeAfterBuffering) return;
  if (playableAudio().some((item) => item.waiting)) return;
  state.resumeAfterBuffering = false;
  clearNotice(NOTICE_TRANSPORT);
  if (!state.playing) void playAll();
}

function updateDurationDisplay() {
  elements.timeline.max = String(Math.max(0, state.duration));
  elements.heroDuration.textContent = formatTime(state.duration);
  elements.remaining.textContent = state.duration ? `−${formatTime(state.duration - state.position)}` : '--:--';
}

function setPosition(value, shouldSeek = true) {
  state.position = clamp(number(value, 0), 0, state.duration || Number.MAX_SAFE_INTEGER);
  if (shouldSeek) {
    state.audio.forEach((record) => {
      try {
        record.element.currentTime = state.position;
      } catch (_error) {
        // The browser may reject a seek before metadata is available.
      }
    });
  }
  updatePlaybackUi();
}

function updatePlaybackUi() {
  elements.timeline.value = String(state.position);
  elements.elapsed.textContent = formatTime(state.position);
  elements.remaining.textContent = state.duration ? `−${formatTime(state.duration - state.position)}` : '--:--';
  elements.playGlyph.textContent = state.playing ? '❚❚' : '▶';
  elements.play.setAttribute('aria-label', state.playing ? 'Pause' : 'Play');
  elements.play.title = `${state.playing ? 'Pause' : 'Play'} (Space)`;
  elements.transportState.textContent = state.playing ? 'Playing' : (state.position ? 'Paused' : 'Ready');
  elements.timeline.disabled = !state.duration;
  updateLyrics(state.position);
  updateTab(state.position);
}

function updateAllPlaybackRates() {
  state.audio.forEach((record) => { record.element.playbackRate = state.rate; });
}

function pauseAll() {
  state.playing = false;
  state.resumeAfterBuffering = false;
  state.audio.forEach((record) => record.element.pause());
  // A transport message may outlive the moment it was raised only while the app is still
  // waiting for the condition it describes, and this is where it stops waiting — the line
  // above is the app giving up on the resume. Clearing the slot here uncovers whatever
  // track or layer message is standing: without it, the last playable stem dying during a
  // buffering pause (the 'error' handler routes to pauseAll) would leave "…is buffering"
  // on screen for the rest of the session over a stem that will never sound again.
  clearNotice(NOTICE_TRANSPORT);
  setConnection('Paused locally', 'good');
  updatePlaybackUi();
  savePreferences();
}

async function playAll() {
  const records = playableAudio();
  if (!records.length) return;
  if (state.duration && state.position >= state.duration - 0.05) setPosition(0);
  updateAllPlaybackRates();
  records.forEach((record) => {
    // Re-seeking a stem that is already in place would drop its buffer, but every stem
    // the corrector was allowed to leave adrift must be pulled back here: RESUME_ALIGNMENT
    // is an order of magnitude tighter than DRIFT_TOLERANCE precisely so that a resume
    // cannot restart two stems an audible distance apart.
    if (Math.abs(record.element.currentTime - state.position) <= RESUME_ALIGNMENT) return;
    try { record.element.currentTime = state.position; } catch (_error) { /* wait for metadata */ }
  });
  // The elements un-pause synchronously, so the transport must say so before the
  // play promises settle; a stall or an explicit pause reconciles the state below.
  state.playToken += 1;
  const token = state.playToken;
  state.playing = true;
  setConnection('Playing locally', 'good');
  updatePlaybackUi();
  const results = await Promise.all(records.map((record) => record.element.play().then(
    () => true,
    () => false,
  )));
  if (token !== state.playToken || !state.playing) return;
  if (!results.every(Boolean)) {
    state.playing = false;
    records.forEach((record) => record.element.pause());
    setConnection('Audio not ready', 'warning');
    // The one transport message that is not tied to an event: nothing is being waited on
    // here, so it stands until the next Play press replaces or clears it, or until
    // pauseAll clears it. It can therefore sit over a track failure that arrives after
    // it — bounded, because it stays true meanwhile (playback did not start) and it asks
    // for the one action that ends it.
    showNotice(
      'Every available stem must be ready before playback starts. Wait a moment and try again.',
      'warning',
      NOTICE_TRANSPORT,
    );
    updatePlaybackUi();
    return;
  }
  clearNotice(NOTICE_TRANSPORT);
  updatePlaybackUi();
}

function togglePlayback() {
  if (state.playing) pauseAll();
  else void playAll();
}

function seekBy(delta) {
  setPosition(state.position + delta);
}

function correctAudioDrift(masterTime) {
  state.audio.forEach((record, index) => {
    if (record.failed) return;
    const audio = record.element;
    if (!Number.isFinite(audio.currentTime)) return;
    // A starved stem sitting at HAVE_METADATA is the one that most needs re-seeking to
    // master time, so only a stem with no timeline at all is skipped. A seek already in
    // flight is left alone instead of being restarted every frame, which would keep it
    // from ever completing.
    if (audio.readyState < HAVE_METADATA || audio.seeking) return;
    if (index === state.masterIndex) {
      audio.playbackRate = state.rate;
      return;
    }
    const drift = audio.currentTime - masterTime;
    if (Math.abs(drift) > DRIFT_TOLERANCE) {
      try { audio.currentTime = masterTime; } catch (_error) { /* retry on the next frame */ }
      audio.playbackRate = state.rate;
    } else {
      audio.playbackRate = clamp(state.rate - drift * 0.18, state.rate * 0.985, state.rate * 1.015);
    }
  });
}

function updateMasterTimeline() {
  if (!state.playing) return;
  let master = state.audio[state.masterIndex];
  if (!master || master.failed) {
    chooseMaster();
    master = state.audio[state.masterIndex];
  }
  const masterTime = master && Number.isFinite(master.element.currentTime)
    ? master.element.currentTime
    : state.position;
  state.position = clamp(masterTime, 0, state.duration || Number.MAX_SAFE_INTEGER);
  correctAudioDrift(state.position);
  checkStemStarvation(window.performance.now());
  updatePlaybackUi();
}

function animationFrame() {
  updateMasterTimeline();
  state.frame = window.requestAnimationFrame(animationFrame);
}

function cueWords(cue) {
  const words = Array.isArray(cue.words) ? cue.words : [];
  if (!words.length) return document.createTextNode(text(cue.text, 'Untimed lyric cue'));
  const fragment = document.createDocumentFragment();
  words.forEach((word, index) => {
    const span = document.createElement('span');
    span.className = 'lyric-word';
    span.dataset.start = String(number(word.start));
    span.dataset.end = String(number(word.end, number(word.start)));
    span.textContent = text(word.text, '');
    fragment.append(span);
    if (index < words.length - 1) fragment.append(document.createTextNode(' '));
  });
  return fragment;
}

function renderLyrics() {
  elements.lyricsList.replaceChildren();
  state.lyricsResolved = true;
  updateLyricsPlaceholders();
  if (!state.lyrics.length) {
    elements.lyricsStatus.textContent = 'No timing available';
    return;
  }
  elements.lyricsStatus.textContent = `${state.lyrics.length} timed cues`;
  state.lyrics.forEach((cue, index) => {
    const item = document.createElement('li');
    item.className = 'lyric-item';
    item.dataset.index = String(index);
    const button = document.createElement('button');
    button.className = 'lyric-cue';
    button.type = 'button';
    button.dataset.start = String(number(cue.start));
    button.setAttribute('aria-label', `Seek to lyric at ${formatTime(cue.start)}`);
    button.append(cueWords(cue));
    button.addEventListener('click', () => setPosition(number(cue.start)));
    item.append(button);
    elements.lyricsList.append(item);
  });
}

function lyricIndexAt(time) {
  let low = 0;
  let high = state.lyrics.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    const cue = state.lyrics[middle];
    if (time < number(cue.start)) high = middle - 1;
    else {
      if (time <= number(cue.end, cue.start)) found = middle;
      low = middle + 1;
    }
  }
  return found;
}

function centreCue(item) {
  // Only the lyrics viewport may move: scrollIntoView would walk every scrollable
  // ancestor and drag the rest of the workspace out of view.
  const viewport = elements.lyricsViewport;
  const limit = Math.max(0, viewport.scrollHeight - viewport.clientHeight);
  const centred = item.offsetTop - (viewport.clientHeight - item.offsetHeight) / 2;
  viewport.scrollTo({ top: clamp(centred, 0, limit), behavior: reducedMotion ? 'auto' : 'smooth' });
}

function updateLyrics(time) {
  if (!state.lyrics.length) return;
  const active = lyricIndexAt(time);
  if (active !== state.activeCue) {
    const old = elements.lyricsList.querySelector('.is-active');
    if (old) {
      old.classList.remove('is-active');
      old.querySelectorAll('.lyric-word').forEach((word) => {
        word.classList.remove('is-sung', 'is-current');
      });
    }
    const next = elements.lyricsList.querySelector(`[data-index="${active}"]`);
    if (next) {
      next.classList.add('is-active');
      if (state.playing) centreCue(next);
    }
    state.activeCue = active;
  }
  const activeItem = elements.lyricsList.querySelector('.is-active');
  if (!activeItem) return;
  activeItem.querySelectorAll('.lyric-word').forEach((word) => {
    const start = number(word.dataset.start);
    const end = number(word.dataset.end, start);
    word.classList.toggle('is-sung', time >= end);
    word.classList.toggle('is-current', time >= start && time <= end);
  });
}

function normalizeTab(tab) {
  if (!tab || typeof tab !== 'object') return null;
  const tuning = tab.tuning && typeof tab.tuning === 'object' ? tab.tuning : {};
  const midi = Array.isArray(tuning.midi) ? tuning.midi : DEFAULT_TUNING.map((item) => item.midi);
  const labels = Array.isArray(tuning.labels) ? tuning.labels : DEFAULT_TUNING.map((item) => item.label);
  const events = Array.isArray(tab.events) ? tab.events.filter((event) => event && typeof event === 'object') : [];
  return {
    tuning: { midi, labels, maxFret: number(tuning.max_fret, 20) },
    events: events.sort((left, right) => number(left.start) - number(right.start)),
  };
}

function stringNumber(sourceString) {
  // The tab API indexes strings low to high; players count the high e as string 1.
  return state.tabStringCount - sourceString;
}

function renderTab() {
  elements.tabGrid.replaceChildren();
  elements.tabGutter.replaceChildren();
  state.activeTabEvent = -1;
  if (!state.tab || !state.tab.events.length) {
    state.tabStringCount = DEFAULT_TUNING.length;
    elements.tabEmpty.hidden = false;
    elements.tabStatus.textContent = 'No tab available';
    elements.tuning.textContent = 'Tuning unavailable';
    elements.fret.textContent = 'Max fret --';
    return;
  }
  elements.tabEmpty.hidden = true;
  state.tabEvents = state.tab.events;
  const labels = state.tab.tuning.labels;
  const rowCount = Math.max(DEFAULT_TUNING.length, labels.length, state.tab.tuning.midi.length);
  state.tabStringCount = rowCount;
  const world = document.createElement('div');
  world.className = 'tab-world';
  const lastEvent = state.tab.events[state.tab.events.length - 1];
  const tabSeconds = Math.max(state.duration, number(lastEvent.end, lastEvent.start), 1);
  world.style.width = `max(100%, calc(50vw + ${tabSeconds * 72}px))`;
  const lineRows = [];
  for (let displayRow = 0; displayRow < rowCount; displayRow += 1) {
    const sourceString = rowCount - 1 - displayRow;
    const row = document.createElement('div');
    row.className = 'tab-row';
    row.dataset.string = String(sourceString);
    world.append(row);
    lineRows[sourceString] = row;
    const label = document.createElement('span');
    label.className = 'tab-string-label';
    label.textContent = text(labels[sourceString], `S${stringNumber(sourceString)}`);
    elements.tabGutter.append(label);
  }
  state.tabEvents.forEach((event, index) => {
    const positions = Array.isArray(event.positions) ? event.positions : [];
    positions.forEach((position) => {
      const stringIndex = Math.floor(number(position.string, -1));
      const row = lineRows[stringIndex];
      if (!row || stringIndex < 0) return;
      const note = document.createElement('span');
      note.className = 'tab-note';
      note.dataset.eventIndex = String(index);
      note.style.left = `${number(event.start) * 72}px`;
      const duration = Math.max(0.14, number(event.end, number(event.start) + 0.18) - number(event.start));
      note.style.width = `${Math.max(24, duration * 72)}px`;
      note.textContent = String(Math.max(0, Math.floor(number(position.fret, 0))));
      note.title = `String ${stringNumber(stringIndex)}, fret ${note.textContent}`;
      row.append(note);
    });
  });
  elements.tabGrid.append(world);
  const tuningText = labels.slice().reverse().join(' · ') || 'Standard tuning';
  elements.tuning.textContent = tuningText;
  elements.fret.textContent = `Max fret ${state.tab.tuning.maxFret}`;
  elements.tabStatus.textContent = `${state.tabEvents.length} note events`;
}

function tabIndexAt(time) {
  let low = 0;
  let high = state.tabEvents.length - 1;
  let candidate = -1;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (number(state.tabEvents[middle].start) <= time) {
      candidate = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  if (candidate < 0) return -1;
  const event = state.tabEvents[candidate];
  return time <= number(event.end, number(event.start) + 0.18) ? candidate : -1;
}

function updateTab(time) {
  const world = elements.tabGrid.querySelector('.tab-world');
  if (!world || !state.tabEvents.length) return;
  const active = tabIndexAt(time);
  if (active !== state.activeTabEvent) {
    elements.tabGrid.querySelectorAll('.tab-note').forEach((note) => {
      note.classList.toggle('is-active', Number(note.dataset.eventIndex) === active);
    });
    state.activeTabEvent = active;
  }
  const center = Math.max(0, elements.tabViewport.clientWidth / 2 - 48);
  world.style.transform = `translateX(${center - time * 72}px)`;
  if (active >= 0) {
    const event = state.tabEvents[active];
    const positions = Array.isArray(event.positions) ? event.positions : [];
    const frets = positions.map(
      (position) => `S${stringNumber(number(position.string))}:${number(position.fret)}`,
    ).join(' · ');
    elements.tabPosition.textContent = frets || `Event at ${formatTime(event.start)}`;
  } else {
    elements.tabPosition.textContent = state.playing ? 'Listen for the next note' : 'Ready to play';
  }
}

function configureDownload(anchor, value, label) {
  const safeUrl = endpoint(value);
  // Every export stays hidden until a HEAD probe proves the file is really there: the
  // manifest advertises print, ASCII tab and MIDI unconditionally, so an export the
  // pipeline never wrote would otherwise hand the user a 404.
  anchor.hidden = true;
  if (!safeUrl) {
    anchor.removeAttribute('href');
    return safeUrl;
  }
  anchor.href = safeUrl;
  anchor.setAttribute('aria-label', `Download ${label}`);
  return safeUrl;
}

function setPrintAvailability(ready) {
  state.printReady = ready;
  elements.print.hidden = !ready;
  // state.printUrl is non-empty whenever ready is true, so the anchor always has an href.
  elements.printable.hidden = !ready;
  // The print stylesheet may hide the on-screen lane only when a complete export exists
  // to replace it; without one the page prints the part of the lane it has, as before.
  app.classList.toggle('print-export-ready', ready);
}

async function exportExists(url) {
  // endpoint() only proves a URL is same-origin and in scope; it cannot prove the
  // resource exists. Nothing but the server can answer that.
  if (!url) return false;
  try {
    const response = await fetch(url, { method: 'HEAD', credentials: 'same-origin' });
    return response.ok;
  } catch (_error) {
    return false;
  }
}

async function confirmExports(asciiUrl, midiUrl, token) {
  const [printReady, asciiReady, midiReady] = await Promise.all([
    exportExists(state.printUrl),
    exportExists(asciiUrl),
    exportExists(midiUrl),
  ]);
  // A retry or a second project must not have its controls rewritten by a stale probe.
  if (token !== state.exportToken) return;
  setPrintAvailability(printReady);
  elements.ascii.hidden = !asciiReady;
  elements.midi.hidden = !midiReady;
}

function configureProject(data) {
  if (!data || data.schema !== 'kilix.playalong.web/v1' || !data.project) {
    throw new Error('The project API returned an unsupported schema.');
  }
  const project = data.project;
  const projectId = text(project.id, 'private-project');
  // A retry must not inherit the previous attempt's standing notices.
  resetNotices();
  state.project = project;
  state.lyrics = [];
  state.lyricsResolved = false;
  state.tab = null;
  state.tabEvents = [];
  state.activeCue = -1;
  state.activeTabEvent = -1;
  state.duration = Math.max(0, number(project.duration, 0));
  state.tracks = Array.isArray(data.tracks)
    ? data.tracks.filter((track) => track && text(track.id)).map((track) => ({
      id: text(track.id),
      label: text(track.label, 'Audio stem'),
      kind: text(track.kind, 'audio'),
      defaultMuted: Boolean(track.defaultMuted),
      muted: Boolean(track.defaultMuted),
      volume: 1,
      url: track.url,
      error: '',
      muteButton: null,
      volumeInput: null,
    }))
    : [];
  const preferences = loadPreferences(projectId);
  applyPreferences(preferences);
  elements.title.textContent = text(project.title, 'Untitled practice session');
  elements.artist.textContent = text(project.artist, 'Artist not provided');
  elements.projectKicker.textContent = state.tracks.length ? 'Local play-along' : 'Empty project';
  updateDurationDisplay();
  renderTrackControls();
  createAudioTracks();
  state.printUrl = configureDownload(elements.printable, data.printUrl, 'printable mode') || '';
  const asciiUrl = configureDownload(elements.ascii, data.asciiTabUrl, 'ASCII tab') || '';
  const midiUrl = configureDownload(elements.midi, data.midiUrl, 'MIDI') || '';
  setPrintAvailability(false);
  state.exportToken += 1;
  void confirmExports(asciiUrl, midiUrl, state.exportToken);
  updateLayerControls();
}

function toggleVocals() {
  const next = !state.vocalsEnabled;
  state.tracks.filter(isVocalTrack).forEach((track) => {
    track.muted = !next;
    updateAudioVolume(track);
    applyMuteButton(track);
  });
  updateLayerControls();
  rememberPreferences();
}

function toggleLyrics() {
  state.lyricsVisible = !state.lyricsVisible;
  updateLayerControls();
  rememberPreferences();
}

async function loadOptionalDocuments(data) {
  const results = await Promise.allSettled([
    data.lyricsUrl ? readJson(data.lyricsUrl, 'Timed lyrics') : Promise.resolve(null),
    data.tabUrl ? readJson(data.tabUrl, 'Tablature') : Promise.resolve(null),
  ]);
  const lyricsResult = results[0];
  const tabResult = results[1];
  if (lyricsResult.status === 'fulfilled' && lyricsResult.value) {
    const cues = Array.isArray(lyricsResult.value.cues) ? lyricsResult.value.cues : [];
    state.lyrics = cues.filter((cue) => cue && Number.isFinite(Number(cue.start))).sort(
      (left, right) => number(left.start) - number(right.start),
    );
  }
  if (tabResult.status === 'fulfilled') state.tab = normalizeTab(tabResult.value);
  renderLyrics();
  renderTab();
  const failures = [lyricsResult, tabResult].filter((result) => result.status === 'rejected');
  if (failures.length) {
    showNotice(
      'Some practice layers are unavailable. Audio playback is still ready.',
      'warning',
      NOTICE_LAYER,
    );
  }
}

async function loadProject() {
  showOnly('loading');
  setConnection('Loading project');
  elements.loadingMessage.textContent = 'Reading the project manifest and arranging its tracks.';
  try {
    const response = await fetch('api/project', {
      method: 'GET',
      headers: { Accept: 'application/json' },
      credentials: 'same-origin',
    });
    if (!response.ok) throw new Error(`Project API returned ${response.status}.`);
    const data = await response.json();
    configureProject(data);
    if (!state.tracks.length || !state.audio.length) {
      elements.emptyMessage.textContent = 'Return to the creator and add a valid audio artifact before opening the player.';
      showOnly('empty');
      setConnection('No playable audio', 'warning');
      return;
    }
    showOnly('player');
    setConnection('Ready locally', 'good');
    await loadOptionalDocuments(data);
    updatePlaybackUi();
  } catch (error) {
    elements.errorMessage.textContent = error instanceof Error ? error.message : 'The project could not be loaded.';
    showOnly('error');
    setConnection('Load error', 'bad');
  }
}

function handleShortcut(event) {
  const target = event.target;
  if (target instanceof HTMLElement && (
    target.matches('button, a, input, select, textarea, [contenteditable="true"]') ||
    target.isContentEditable
  )) return;
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // Shift is not blanket-ignored: '+' is Shift+'=' on a US layout. Only Space needs it,
  // so that Shift+Space keeps scrolling the page up.
  if (event.code === 'Space' && !event.shiftKey) {
    event.preventDefault();
    togglePlayback();
  } else if (event.key === 'ArrowLeft') {
    event.preventDefault();
    seekBy(-5);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    seekBy(5);
  } else if (event.key === '-' || event.key === '_') {
    event.preventDefault();
    seekBy(-5);
  } else if (event.key === '+' || event.key === '=') {
    event.preventDefault();
    seekBy(5);
  } else if (event.key.toLowerCase() === 'v') {
    event.preventDefault();
    toggleVocals();
  } else if (event.key.toLowerCase() === 'l') {
    event.preventDefault();
    toggleLyrics();
  }
}

elements.play.addEventListener('click', togglePlayback);
elements.backFive.addEventListener('click', () => seekBy(-5));
elements.forwardFive.addEventListener('click', () => seekBy(5));
elements.resetPosition.addEventListener('click', () => setPosition(0));
elements.timeline.addEventListener('input', () => setPosition(number(elements.timeline.value)));
elements.rate.addEventListener('change', () => {
  state.rate = clamp(number(elements.rate.value, 1), 0.5, 1.5);
  updateAllPlaybackRates();
  rememberPreferences();
});
elements.vocals.addEventListener('click', toggleVocals);
elements.lyricsToggle.addEventListener('click', toggleLyrics);
elements.print.addEventListener('click', () => {
  // The rolling lane is one non-wrapping row, so only the printable export can
  // carry the whole song onto paper — and only once a probe has confirmed it exists.
  // That probe runs once per project load: an export written afterwards stays hidden until
  // the project is loaded again, and one deleted afterwards is still opened here.
  if (!state.printReady || !state.printUrl) return;
  window.open(state.printUrl, '_blank', 'noopener');
});
elements.retry.addEventListener('click', () => void loadProject());
window.addEventListener('keydown', handleShortcut);
window.addEventListener('beforeunload', () => {
  if (state.frame) window.cancelAnimationFrame(state.frame);
  state.audio.forEach((record) => record.element.pause());
  savePreferences();
});

void loadProject();
state.frame = window.requestAnimationFrame(animationFrame);
