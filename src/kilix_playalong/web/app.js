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
  tabStatus: document.querySelector('#tab-status'),
  tabViewport: document.querySelector('#tab-viewport'),
  tabGrid: document.querySelector('#tab-grid'),
  tabEmpty: document.querySelector('#tab-empty'),
  tuning: document.querySelector('#tuning-label'),
  fret: document.querySelector('#fret-label'),
  tabPosition: document.querySelector('#tab-position'),
  ascii: document.querySelector('#ascii-download'),
  midi: document.querySelector('#midi-download'),
};

const state = {
  project: null,
  tracks: [],
  audio: [],
  lyrics: [],
  tab: null,
  tabEvents: [],
  duration: 0,
  position: 0,
  rate: 1,
  playing: false,
  masterIndex: 0,
  vocalsEnabled: true,
  lyricsVisible: true,
  activeCue: -1,
  activeTabEvent: -1,
  frame: 0,
  preferenceKey: '',
};

const DEFAULT_TUNING = [
  { midi: 40, label: 'E' },
  { midi: 45, label: 'A' },
  { midi: 50, label: 'D' },
  { midi: 55, label: 'G' },
  { midi: 59, label: 'B' },
  { midi: 64, label: 'E' },
];

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

function showNotice(message, tone = 'info') {
  if (!message) {
    elements.notice.hidden = true;
    elements.notice.replaceChildren();
    return;
  }
  elements.notice.className = `notice-bar notice-${tone}`;
  elements.notice.textContent = message;
  elements.notice.hidden = false;
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
  if (!state.preferenceKey) return;
  const tracks = {};
  state.tracks.forEach((track) => {
    tracks[track.id] = { muted: track.muted, volume: track.volume };
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
  savePreferences();
}

function updateAudioVolume(track) {
  const record = state.audio.find((item) => item.id === track.id);
  if (record) record.element.volume = track.muted ? 0 : track.volume;
}

function updateTrackMute(track, muted) {
  track.muted = muted;
  updateAudioVolume(track);
  renderTrackControls();
  updateLayerControls();
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
  mute.setAttribute('aria-label', `${track.muted ? 'Unmute' : 'Mute'} ${track.label}`);
  mute.setAttribute('aria-pressed', String(track.muted));
  mute.textContent = track.muted ? 'MUTED' : 'LIVE';
  mute.disabled = Boolean(track.error);
  mute.addEventListener('click', () => updateTrackMute(track, !track.muted));

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
    savePreferences();
  });
  volumeLabel.append(volume);
  row.append(mute, volumeLabel);
  item.append(top, row);
  return item;
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
    const record = { id: track.id, element: audio, failed: false };
    audio.addEventListener('loadedmetadata', () => {
      if (state.masterIndex === state.audio.findIndex((record) => record.id === track.id)) {
        if (Number.isFinite(audio.duration) && audio.duration > 0) {
          state.duration = Math.max(state.duration, audio.duration);
          updateDurationDisplay();
        }
      }
    });
    audio.addEventListener('error', () => {
      record.failed = true;
      track.error = 'Audio could not be loaded.';
      showNotice(`${track.label} could not be loaded. Other tracks remain available.`, 'warning');
      renderTrackControls();
      chooseMaster();
      if (!playableAudio().length) pauseAll();
    });
    audio.addEventListener('waiting', () => pauseForBuffering(track.label));
    audio.addEventListener('stalled', () => pauseForBuffering(track.label));
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
}

function playableAudio() {
  return state.audio.filter((record) => !record.failed);
}

function chooseMaster() {
  const next = state.audio.findIndex((record) => !record.failed);
  state.masterIndex = next >= 0 ? next : 0;
}

function pauseForBuffering(label) {
  if (!state.playing) return;
  state.playing = false;
  state.audio.forEach((record) => record.element.pause());
  setConnection('Paused for buffering', 'warning');
  showNotice(`${label} is buffering. Playback paused to keep every stem aligned.`, 'warning');
  updatePlaybackUi();
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
  state.audio.forEach((record) => record.element.pause());
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
    try { record.element.currentTime = state.position; } catch (_error) { /* wait for metadata */ }
  });
  const results = await Promise.all(records.map((record) => record.element.play().then(
    () => true,
    () => false,
  )));
  if (!results.every(Boolean)) {
    state.playing = false;
    records.forEach((record) => record.element.pause());
    setConnection('Audio not ready', 'warning');
    showNotice(
      'Every available stem must be ready before playback starts. Wait a moment and try again.',
      'warning',
    );
    updatePlaybackUi();
    return;
  }
  state.playing = true;
  setConnection('Playing locally', 'good');
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
  const master = state.audio[state.masterIndex];
  state.audio.forEach((record, index) => {
    if (record.failed) return;
    const audio = record.element;
    if (!Number.isFinite(audio.currentTime)) return;
    if (index === state.masterIndex) {
      audio.playbackRate = state.rate;
      return;
    }
    const drift = audio.currentTime - masterTime;
    if (Math.abs(drift) > 0.12) {
      try { audio.currentTime = masterTime; } catch (_error) { /* retry on the next frame */ }
      audio.playbackRate = state.rate;
    } else {
      audio.playbackRate = clamp(state.rate - drift * 0.18, state.rate * 0.985, state.rate * 1.015);
    }
  });
  if (!master || !Number.isFinite(master.element.currentTime)) return;
  if (Math.abs(master.element.currentTime - masterTime) > 0.25) {
    try { master.element.currentTime = masterTime; } catch (_error) { /* no-op */ }
  }
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
  if (!state.lyrics.length) {
    elements.lyricsEmpty.hidden = false;
    elements.lyricsStatus.textContent = 'No timing available';
    return;
  }
  elements.lyricsEmpty.hidden = true;
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
      if (state.playing) {
        next.scrollIntoView({ block: 'center', behavior: reducedMotion ? 'auto' : 'smooth' });
      }
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

function renderTab() {
  elements.tabGrid.replaceChildren();
  state.activeTabEvent = -1;
  if (!state.tab || !state.tab.events.length) {
    elements.tabEmpty.hidden = false;
    elements.tabStatus.textContent = 'No tab available';
    elements.tuning.textContent = 'Tuning unavailable';
    elements.fret.textContent = 'Max fret --';
    return;
  }
  elements.tabEmpty.hidden = true;
  state.tabEvents = state.tab.events;
  const labels = state.tab.tuning.labels;
  const rowCount = Math.max(6, labels.length, state.tab.tuning.midi.length);
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
    const line = document.createElement('span');
    line.className = 'tab-string-line';
    const label = document.createElement('span');
    label.className = 'tab-string-label';
    label.textContent = text(labels[sourceString], `S${sourceString + 1}`);
    line.append(label);
    row.append(line);
    world.append(row);
    lineRows[sourceString] = row;
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
      note.title = `String ${stringIndex + 1}, fret ${note.textContent}`;
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
    const frets = positions.map((position) => `S${number(position.string) + 1}:${number(position.fret)}`).join(' · ');
    elements.tabPosition.textContent = frets || `Event at ${formatTime(event.start)}`;
  } else {
    elements.tabPosition.textContent = state.playing ? 'Listen for the next note' : 'Ready to play';
  }
}

function configureDownload(anchor, value, label) {
  const safeUrl = endpoint(value);
  if (!safeUrl) {
    anchor.hidden = true;
    anchor.removeAttribute('href');
    return;
  }
  anchor.href = safeUrl;
  anchor.hidden = false;
  anchor.setAttribute('aria-label', `Download ${label}`);
}

function configureProject(data) {
  if (!data || data.schema !== 'kilix.playalong.web/v1' || !data.project) {
    throw new Error('The project API returned an unsupported schema.');
  }
  const project = data.project;
  const projectId = text(project.id, 'private-project');
  state.project = project;
  state.lyrics = [];
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
  configureDownload(elements.printable, data.printUrl, 'printable mode');
  configureDownload(elements.ascii, data.asciiTabUrl, 'ASCII tab');
  configureDownload(elements.midi, data.midiUrl, 'MIDI');
  elements.print.hidden = !data.printUrl;
  updateLayerControls();
}

function toggleVocals() {
  const next = !state.vocalsEnabled;
  state.tracks.filter(isVocalTrack).forEach((track) => {
    track.muted = !next;
    updateAudioVolume(track);
  });
  renderTrackControls();
  updateLayerControls();
}

function toggleLyrics() {
  state.lyricsVisible = !state.lyricsVisible;
  updateLayerControls();
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
    showNotice('Some practice layers are unavailable. Audio playback is still ready.', 'warning');
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
  if (event.code === 'Space') {
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
  savePreferences();
});
elements.vocals.addEventListener('click', toggleVocals);
elements.lyricsToggle.addEventListener('click', toggleLyrics);
elements.print.addEventListener('click', () => window.print());
elements.retry.addEventListener('click', () => void loadProject());
window.addEventListener('keydown', handleShortcut);
window.addEventListener('beforeunload', () => {
  if (state.frame) window.cancelAnimationFrame(state.frame);
  state.audio.forEach((record) => record.element.pause());
  savePreferences();
});

void loadProject();
state.frame = window.requestAnimationFrame(animationFrame);
