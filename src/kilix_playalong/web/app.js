'use strict';

/*
 * Kilix Playalong's browser surface is deliberately dependency-free. The
 * server supplies capability-token-prefixed, same-origin URLs; the browser
 * only reads those URLs and never sends project data elsewhere.
 *
 * The fretboard, the chord names and the hand-position box are not invented
 * here. docs/FRETBOARD.md defines them, the native C11 surface is held to the
 * same definition, and tests/fixtures/fretboard_vectors.json is the
 * machine-checkable form both must reproduce. Every value this file computes
 * that the document defines is spelled the way the document spells it and is
 * pinned by the conformance runner in tests/test_web_assets.py, so anything
 * this file gets wrong about them is a test failure rather than a picture
 * nobody compares -- which is how the string numbering drifted once already.
 */

const app = document.querySelector('#app');
// Read as a query rather than a boolean, so a change to the OS setting mid-session is
// honoured: the previous build read `.matches` once at load and then ignored it forever.
const motionQuery = typeof window.matchMedia === 'function'
  ? window.matchMedia('(prefers-reduced-motion: reduce)')
  : null;

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
  minimap: document.querySelector('#minimap'),
  elapsed: document.querySelector('#elapsed-time'),
  remaining: document.querySelector('#remaining-time'),
  transportState: document.querySelector('#transport-state'),
  backFive: document.querySelector('#back-five'),
  forwardFive: document.querySelector('#forward-five'),
  resetPosition: document.querySelector('#reset-position'),
  rate: document.querySelector('#rate-range'),
  rateValue: document.querySelector('#rate-value'),
  loopIn: document.querySelector('#loop-in'),
  loopOut: document.querySelector('#loop-out'),
  loopClear: document.querySelector('#loop-clear'),
  loopReadout: document.querySelector('#loop-readout'),
  preRoll: document.querySelector('#preroll'),
  ladder: document.querySelector('#ladder-toggle'),
  vocals: document.querySelector('#toggle-vocals'),
  lyricsToggle: document.querySelector('#toggle-lyrics'),
  guitarToggle: document.querySelector('#toggle-guitar'),
  tabToggle: document.querySelector('#toggle-tab'),
  neckToggle: document.querySelector('#toggle-neck'),
  stringOrder: document.querySelector('#string-order'),
  handed: document.querySelector('#handed-toggle'),
  motion: document.querySelector('#motion-select'),
  fingers: document.querySelector('#fingers-toggle'),
  zoom: document.querySelector('#zoom-select'),
  overlay: document.querySelector('#shortcut-overlay'),
  overlayClose: document.querySelector('#shortcut-close'),
  lyricsCard: document.querySelector('#lyrics-card'),
  lyricsStatus: document.querySelector('#lyrics-status'),
  lyricsViewport: document.querySelector('#lyrics-viewport'),
  lyricsList: document.querySelector('#lyrics-list'),
  lyricsEmpty: document.querySelector('#lyrics-empty'),
  lyricsTiming: document.querySelector('#lyrics-timing'),
  lyricsTimingTag: document.querySelector('#lyrics-timing-tag'),
  lyricsTimingDetail: document.querySelector('#lyrics-timing-detail'),
  lyricsHiddenNote: document.querySelector('#lyrics-hidden-note'),
  neckCard: document.querySelector('#neck-card'),
  neckCanvas: document.querySelector('#neck-canvas'),
  neckEmpty: document.querySelector('#neck-empty'),
  neckHiddenNote: document.querySelector('#neck-hidden-note'),
  neckStrip: document.querySelector('#neck-static-strip'),
  chordReadout: document.querySelector('#chord-readout'),
  chordNotes: document.querySelector('#chord-notes'),
  positionReadout: document.querySelector('#position-readout'),
  nextReadout: document.querySelector('#next-readout'),
  tabCard: document.querySelector('#tab-card'),
  tabStatus: document.querySelector('#tab-status'),
  tabViewport: document.querySelector('#tab-viewport'),
  tabGrid: document.querySelector('#tab-grid'),
  tabGutter: document.querySelector('#tab-gutter'),
  tabEmpty: document.querySelector('#tab-empty'),
  tabHiddenNote: document.querySelector('#tab-hidden-note'),
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

// What a lyrics document may say about where its own timing came from, as
// kilix.playalong.lyrics/v1 defines it: 'authored' -- the source carried per-line stamps;
// 'measured' -- forced alignment placed the words against the audio, and an `alignment`
// object then says how much of that was measured rather than guessed; 'estimated' -- the
// spans were invented by spreading the text across the duration. The field is optional and
// every document written before it existed carries none of it, so `readTiming` returns null
// for anything outside this list and the panel then renders exactly as it did then.
const TIMING_KINDS = ['authored', 'measured', 'estimated'];

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
// Loop messages are deliberately NOT here: a refused loop end is an answer to the key the
// user just pressed and belongs beside the loop controls, not in a bar that would cover a
// dead stem with it.
const NOTICE_ORDER = [NOTICE_TRANSPORT, NOTICE_TRACK, NOTICE_LAYER];

/* ------------------------------------------------------------------------------------
 * The fretboard contract: docs/FRETBOARD.md.
 *
 * Pure functions of numbers, no DOM, no state. These are the values the native surface
 * computes independently and tests/fixtures/fretboard_vectors.json pins for both.
 * ---------------------------------------------------------------------------------- */

// d(n): the distance of fret n from the nut as a fraction of scale length. Defined on
// reals, so d(12) is exactly 0.5 and 1/d(1) is 17.81715..., the "rule of 18" the old
// hand-tuned ramps approximated. Geometric, not linear: evenly spaced fret wires are the
// single thing that makes a drawn fretboard look fake.
function fretDistance(n) {
  return 1 - Math.pow(2, -n / 12);
}

// cell(n): the middle of the space BEHIND fret n, which is where a finger goes and where
// an inlay goes. Putting either on the wire shifts every marker toward the bridge.
function cellCentre(n) {
  return (fretDistance(n - 1) + fretDistance(n)) / 2;
}

// u(n, N): a surface showing frets 0..N scales by the last fret it draws, so the drawing
// fills its box at any neck length and u(N, N) is exactly 1.
function displayNormalized(n, highest) {
  return fretDistance(n) / fretDistance(highest);
}

const INLAY_SINGLE = [3, 5, 7, 9, 15, 17, 19, 21];
const INLAY_DOUBLE = [12, 24];

// Light electric set, index 0 = low E. The true gauge ratio is 4.6:1, which at any
// sensible line weight either makes the high e sub-pixel or the low E a slab; the square
// root compresses it to 2.14:1, which still grades all six visibly.
const STRING_GAUGE_IN = [0.046, 0.036, 0.026, 0.017, 0.013, 0.010];

function stringWidthRatio(apiIndex) {
  // A tuning with more strings than the reference set keeps the thinnest gauge for the
  // extras rather than reading past the end of the table.
  const index = clamp(Math.round(apiIndex), 0, STRING_GAUGE_IN.length - 1);
  return Math.sqrt(STRING_GAUGE_IN[index] / 0.010);
}

// 'high-e-top' is tablature's order and the default; 'low-e-top' is what a player sees
// looking down at their own instrument. One preference governs the fretboard AND the tab
// lane, because two pictures of one instrument disagreeing about string order in the same
// window is a worse defect than either convention is a flaw.
const ORIENTATIONS = ['high-e-top', 'low-e-top'];

function orientationRow(orientation, apiIndex, count) {
  return orientation === 'low-e-top' ? apiIndex : count - 1 - apiIndex;
}

// A {string, fret} from guitar-tab.json as a point in the normalised neck box: x from 0
// at the nut to 1 at the highest displayed fret, y from 0 at the top edge to 1 at the
// bottom. An open string is marked AT THE NUT: there is no finger, and the nut is the
// thing stopping the string. `pitch` is never consulted here — it is redundant with
// {string, fret} and, if the artifact ever disagrees with itself, the position wins.
function positionX(fret, highest) {
  return fret <= 0 ? 0 : cellCentre(fret) / fretDistance(highest);
}

function positionY(orientation, apiIndex, count) {
  return (orientationRow(orientation, apiIndex, count) + 0.5) / count;
}

function positionPoint(orientation, apiIndex, fret, count, highest) {
  return {
    x: positionX(fret, highest),
    y: positionY(orientation, apiIndex, count),
  };
}

// Sharps only, the same twelve names src/kilix_playalong/tablature.py spells tuning
// labels with. Correct enharmonics need a key signature and nothing in this pipeline
// produces one, so being uniformly sharp beats inventing flats for half the chords.
const PITCH_NAMES = ['C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B'];

// Table order is rank order. It tops out at four pitch classes, so five or more distinct
// classes never match and always return nothing: from automatic transcription a fifth
// class is more often a neighbouring melody note than a real extension.
const CHORD_TEMPLATES = [
  { intervals: [0, 4, 7], suffix: '' },
  { intervals: [0, 3, 7], suffix: 'm' },
  { intervals: [0, 4, 7, 10], suffix: '7' },
  { intervals: [0, 3, 7, 10], suffix: 'm7' },
  { intervals: [0, 4, 7, 11], suffix: 'maj7' },
  { intervals: [0, 4, 7, 9], suffix: '6' },
  { intervals: [0, 3, 7, 9], suffix: 'm6' },
  { intervals: [0, 5, 7], suffix: 'sus4' },
  { intervals: [0, 2, 7], suffix: 'sus2' },
  { intervals: [0, 2, 4, 7], suffix: 'add9' },
  { intervals: [0, 2, 3, 7], suffix: 'madd9' },
  { intervals: [0, 3, 6], suffix: 'dim' },
  { intervals: [0, 4, 8], suffix: 'aug' },
  { intervals: [0, 3, 6, 10], suffix: 'm7b5' },
  { intervals: [0, 3, 6, 9], suffix: 'dim7' },
];

function pitchClass(pitch) {
  return ((Math.round(pitch) % 12) + 12) % 12;
}

function chordSpelling(root, suffix, bassPc) {
  const name = PITCH_NAMES[root] + suffix;
  return root === bassPc ? name : `${name}/${PITCH_NAMES[bassPc]}`;
}

function sameIntervalSet(left, right) {
  if (left.length !== right.length) return false;
  return left.every((value, index) => value === right[index]);
}

// The MIDI pitches sounding at one instant to a chord name, or nothing. A wrong name is
// worse than no name: automatic transcription emits two-note fragments, a note plus its
// own octave, and melody notes landing on triads, and this refuses all of them. On the
// 937-event reference song it refuses 143 of the 438 multi-pitch-class events, which is
// the design working rather than a gap to close.
function chordName(pitches) {
  const list = (Array.isArray(pitches) ? pitches : [])
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value));
  if (!list.length) return null;
  // The one and only place register matters: the bass is the lowest MIDI pitch, read
  // before the set reduction, so input order is irrelevant and octave doubling collapses.
  const bassPc = pitchClass(Math.min.apply(null, list));
  const pcs = Array.from(new Set(list.map(pitchClass))).sort((left, right) => left - right);
  if (pcs.length < 2) return null;
  if (pcs.length === 2) {
    // A fifth, or a fourth which is a fifth upside down. Every other dyad is refused: a
    // major third belongs to C, Cmaj7, Am, Am7 and Fmaj7 alike, so naming it is a guess.
    const interval = (pcs[1] - pcs[0] + 12) % 12;
    if (interval === 7) return chordSpelling(pcs[0], '5', bassPc);
    if (interval === 5) return chordSpelling(pcs[1], '5', bassPc);
    return null;
  }
  const matches = [];
  pcs.forEach((root) => {
    const intervals = pcs
      .map((pc) => (pc - root + 12) % 12)
      .sort((left, right) => left - right);
    CHORD_TEMPLATES.forEach((template, index) => {
      if (sameIntervalSet(intervals, template.intervals)) {
        matches.push({ root, rank: index + 1, suffix: template.suffix });
      }
    });
  });
  if (!matches.length) return null;
  // Bass first, then quality rank, then root. The bass dominating is the point: the same
  // four notes are C6 or Am7 and the player's bass note says which. Element three never
  // decides a winner — checked exhaustively — but is specified so the sort is total and
  // no implementation depends on its language's sort stability.
  matches.sort((left, right) => (
    (left.root === bassPc ? 0 : 1) - (right.root === bassPc ? 0 : 1)
    || left.rank - right.rank
    || left.root - right.root
  ));
  return chordSpelling(matches[0].root, matches[0].suffix, bassPc);
}

// Where the hand is about to be, from the frets in a window around the playhead. Open
// strings are excluded: fret 0 needs no hand, and counting it would report first position
// for a passage at the twelfth fret with one open drone under it. The box is a fixed five
// frets placed by the lower median, never stretched to cover outliers — a naive min-to-max
// span over this window has a median width of 7 frets and a maximum of 19, which is not a
// hand, it is the whole neck. null means hide the marker, not move it.
function handPositionBox(frets, maxFret) {
  const fretted = (Array.isArray(frets) ? frets : [])
    .map((value) => Number(value))
    .filter((value) => Number.isFinite(value) && value >= 1)
    .sort((left, right) => left - right);
  if (!fretted.length) return null;
  const centre = fretted[Math.floor((fretted.length - 1) / 2)];
  let low = centre - 2;
  let high = centre + 2;
  if (low < 1) {
    high += 1 - low;
    low = 1;
  }
  if (high > maxFret) {
    low -= high - maxFret;
    high = maxFret;
    if (low < 1) low = 1;
  }
  return [low, high];
}

const ROMAN = [
  'I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X', 'XI', 'XII',
  'XIII', 'XIV', 'XV', 'XVI', 'XVII', 'XVIII', 'XIX', 'XX', 'XXI', 'XXII', 'XXIII', 'XXIV',
];

function romanNumeral(fret) {
  const index = Math.round(fret) - 1;
  return index >= 0 && index < ROMAN.length ? ROMAN[index] : String(Math.round(fret));
}

/* ------------------------------------------------------------------------------------
 * Canvas palette.
 *
 * A canvas cannot read a custom property, and the node harness these files are tested
 * under has no getComputedStyle to ask with. So the hex values live here, the same values
 * are declared as custom properties in styles.css, and
 * test_neck_palette_is_also_declared_in_the_stylesheet makes drift between the two a test
 * failure rather than a slowly diverging picture.
 * ---------------------------------------------------------------------------------- */
const NECK_PALETTE = {
  boardTop: '#211a15',
  boardBottom: '#34261c',
  boardEdge: '#0b0906',
  grain: '#3f2e22',
  fretLow: '#8f9a94',
  fretHigh: '#cbd3cf',
  nut: '#e8e2d2',
  inlay: '#dfe6df',
  wound: '#b08a5e',
  plain: '#cfd6d2',
  sounding: '#f3d58b',
  ghost: '#91edbd',
  box: '#2eaa75',
  rail: '#63766b',
  numeral: '#91a59a',
};

// Six fixed hues for the approach corridor, index 0 = low E. They vary in lightness as
// well as hue so the common CVD types still separate them, and nothing is encoded by
// colour alone: the string is already carried by the row on the neck and in the lane.
const STRING_HUES = ['#d1603d', '#e2a03f', '#c9c95e', '#6fcf97', '#56a8e0', '#b58cf0'];

// Lane speeds in CSS pixels per second. 90 is the native surface's KPA_UI_LANE_PPS, so
// the default lane runs at exactly the speed the terminal draws.
const LANE_SPEEDS = { slow: 56, normal: 90, fast: 144 };
// The playhead sits a quarter of the way in, matching the native lane's
// head_x = lane_x0 + (right - lane_x0) / 4, so three quarters of the lane is the future.
const PLAYHEAD_FRACTION = 0.25;

// Corridor: how far ahead it reaches, and how small the far edge is drawn.
const READ_AHEAD_MAX = 2.5;
const CORRIDOR_SCALE_MIN = 0.34;
const CORRIDOR_H = 110;
const CORRIDOR_H_SHORT = 70;
// An attack flash has to be gone before the next note can arrive: the shortest measured
// note in the reference transcription is 0.094 s.
const IMPACT_DECAY = 0.09;
const IMPACT_RING = 0.18;
const IMPACT_LIMIT = 12;
// A string is drawn vibrating at a visual 9 Hz plus a per-string offset. The real
// fundamentals are 82-330 Hz and would alias into a stroboscopic mess at 60 fps, so this
// is a legible stand-in for vibration and not the pitch.
const VIBRATION_HZ = 9;
// The window the hand-position box is computed over, from docs/FRETBOARD.md §6.
const HAND_WINDOW_BACK = 0.5;
const HAND_WINDOW_FORWARD = 1.5;
// Loop rules, matching the native surface: a fresh loop start with no end gets a one
// second end (kpa_ui.c), and a loop shorter than this is refused rather than seeking six
// media elements several times a second.
const LOOP_DEFAULT_LENGTH = 1.0;
const LOOP_MIN_LENGTH = 0.25;
const RATE_STEP = 0.05;
const RATE_MIN = 0.5;
const RATE_MAX = 1.5;
const GAIN_STEP = 0.05;

const state = {
  project: null,
  tracks: [],
  audio: [],
  lyrics: [],
  lyricsResolved: false,
  lyricsTiming: null,
  tab: null,
  tabEvents: [],
  tabStringCount: DEFAULT_TUNING.length,
  tabNotes: [],
  tabWorld: null,
  tabLoop: null,
  tabMaxDuration: 1,
  duration: 0,
  position: 0,
  rate: 1,
  playing: false,
  playToken: 0,
  resumeAfterBuffering: false,
  masterIndex: 0,
  selectedTrack: 0,
  vocalsEnabled: true,
  guitarEnabled: true,
  lyricsVisible: true,
  tabVisible: true,
  neckVisible: true,
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
  // Display preferences. orientation governs the neck and the lane together.
  orientation: 'high-e-top',
  handed: 'right',
  lanePps: LANE_SPEEDS.normal,
  motionPref: 'auto',
  showFingers: false,
  // Loop and practice.
  loop: { start: 0, end: 0, active: false },
  preRoll: 0,
  ladder: false,
  ladderTarget: 1,
  loopMessage: '',
  // Neck rendering.
  neckDirty: true,
  neckDrawable: false,
  neckPaints: 0,
  neckHighestFret: 12,
  neckStatic: null,
  neckStaticKey: '',
  impacts: [],
  handBox: null,
  sounding: [],
  approach: [],
  nextEvent: -1,
  activeEvent: -1,
  lastModelTime: 0,
  chordText: '',
  readoutKey: '',
  minimapBitmap: null,
  minimapKey: '',
  overlayOpen: false,
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

// The OS setting and the page's own preference, read together on every use so a change to
// either takes effect without a reload.
function motionAllowed() {
  if (state.motionPref === 'off') return false;
  return !(motionQuery && motionQuery.matches);
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
    tabVisible: true,
    neckVisible: true,
    orientation: 'high-e-top',
    handed: 'right',
    lanePps: LANE_SPEEDS.normal,
    motionPref: 'auto',
    showFingers: false,
    preRoll: 0,
    ladder: false,
    loop: { start: 0, end: 0, active: false },
    tracks: {},
  };
}

function readLoop(saved) {
  if (!saved || typeof saved !== 'object') return { start: 0, end: 0, active: false };
  const start = Math.max(0, number(saved.start, 0));
  const end = Math.max(0, number(saved.end, 0));
  return { start, end, active: Boolean(saved.active) && end - start >= LOOP_MIN_LENGTH };
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
    // The rate control is a slider over [0.50, 1.50] in steps of 0.05, matching the
    // native KPA_UI_RATE_STEP. Every value the old five-entry menu could store is a
    // multiple of 0.05, so widening the validator needs no migration.
    const rate = Math.round(number(preferences.rate, 1) / RATE_STEP) * RATE_STEP;
    preferences.rate = clamp(Number(rate.toFixed(2)), RATE_MIN, RATE_MAX);
    preferences.vocalsEnabled = preferences.vocalsEnabled !== false;
    preferences.lyricsVisible = preferences.lyricsVisible !== false;
    preferences.tabVisible = preferences.tabVisible !== false;
    preferences.neckVisible = preferences.neckVisible !== false;
    preferences.orientation = ORIENTATIONS.includes(preferences.orientation)
      ? preferences.orientation
      : 'high-e-top';
    preferences.handed = preferences.handed === 'left' ? 'left' : 'right';
    preferences.lanePps = Object.values(LANE_SPEEDS).includes(Number(preferences.lanePps))
      ? Number(preferences.lanePps)
      : LANE_SPEEDS.normal;
    preferences.motionPref = preferences.motionPref === 'off' ? 'off' : 'auto';
    preferences.showFingers = preferences.showFingers === true;
    preferences.preRoll = [0, 1, 2].includes(Number(preferences.preRoll))
      ? Number(preferences.preRoll)
      : 0;
    preferences.ladder = preferences.ladder === true;
    preferences.loop = readLoop(preferences.loop);
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
      ? { volume: track.volume, soloed: track.soloed }
      : { muted: track.muted, volume: track.volume, soloed: track.soloed };
  });
  try {
    window.localStorage.setItem(state.preferenceKey, JSON.stringify({
      rate: state.rate,
      vocalsEnabled: state.vocalsEnabled,
      lyricsVisible: state.lyricsVisible,
      tabVisible: state.tabVisible,
      neckVisible: state.neckVisible,
      orientation: state.orientation,
      handed: state.handed,
      lanePps: state.lanePps,
      motionPref: state.motionPref,
      showFingers: state.showFingers,
      preRoll: state.preRoll,
      ladder: state.ladder,
      loop: state.loop,
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

function isGuitarTrack(track) {
  return track.kind === 'guitar' || track.id === 'guitar';
}

function applyPreferences(preferences) {
  state.rate = preferences.rate;
  state.vocalsEnabled = preferences.vocalsEnabled;
  state.lyricsVisible = preferences.lyricsVisible;
  state.tabVisible = preferences.tabVisible;
  state.neckVisible = preferences.neckVisible;
  state.orientation = preferences.orientation;
  state.handed = preferences.handed;
  state.lanePps = preferences.lanePps;
  state.motionPref = preferences.motionPref;
  state.showFingers = preferences.showFingers;
  state.preRoll = preferences.preRoll;
  state.ladder = preferences.ladder;
  state.ladderTarget = preferences.rate;
  state.loop = readLoop(preferences.loop);
  state.tracks.forEach((track) => {
    const saved = preferences.tracks[track.id];
    track.volume = clamp(number(saved && saved.volume, 1), 0, 1);
    track.soloed = Boolean(saved && saved.soloed);
    track.muted = saved && Object.prototype.hasOwnProperty.call(saved, 'muted')
      ? Boolean(saved.muted)
      : Boolean(track.defaultMuted);
  });
  elements.rate.value = String(state.rate);
  elements.stringOrder.value = state.orientation;
  elements.zoom.value = String(state.lanePps);
  elements.motion.value = state.motionPref;
  elements.preRoll.value = String(state.preRoll);
  updateRateReadout();
  applyDisplayClasses();
}

// Orientation and handedness reach the DOM as classes on #app: the lane flips by CSS
// alone, so the DOM order of its rows stays canonical and only the picture changes.
function applyDisplayClasses() {
  app.classList.toggle('strings-low-e-top', state.orientation === 'low-e-top');
  // Handedness is not a class: it mirrors the canvas x mapping only, and nothing in the
  // DOM changes with it. Time still runs left to right in the lane for either hand.
  app.classList.toggle('motion-off', !motionAllowed());
}

function updateLayerControls() {
  const vocalTracks = state.tracks.filter(isVocalTrack);
  const vocalsOn = vocalTracks.length > 0 && vocalTracks.some((track) => !track.muted);
  state.vocalsEnabled = vocalsOn;
  elements.vocals.disabled = vocalTracks.length === 0;
  setButtonState(elements.vocals, vocalsOn, 'Guide vocals', 'Guide vocals off');
  const guitarTracks = state.tracks.filter(isGuitarTrack);
  const guitarOn = guitarTracks.length > 0 && guitarTracks.some((track) => !track.muted);
  state.guitarEnabled = guitarOn;
  elements.guitarToggle.disabled = guitarTracks.length === 0;
  setButtonState(elements.guitarToggle, guitarOn, 'Guitar stem', 'You play the guitar');
  setButtonState(elements.lyricsToggle, state.lyricsVisible, 'Lyrics on screen', 'Lyrics hidden');
  setButtonState(elements.tabToggle, state.tabVisible, 'Tab lane', 'Tab lane hidden');
  setButtonState(elements.neckToggle, state.neckVisible, 'Fretboard', 'Fretboard hidden');
  elements.lyricsCard.classList.toggle('layer-hidden', !state.lyricsVisible);
  elements.tabCard.classList.toggle('layer-hidden', !state.tabVisible);
  elements.neckCard.classList.toggle('layer-hidden', !state.neckVisible);
  // The lane gives up half its row height when the fretboard is on screen beside it, so
  // both fit above the fold on a laptop.
  elements.tabViewport.classList.toggle('lane-compact', state.neckVisible);
  updateLyricsPlaceholders();
  updateTabPlaceholders();
  requestNeckPaint();
}

function setButtonState(button, on, labelOn, labelOff) {
  button.classList.toggle('is-on', on);
  button.setAttribute('aria-pressed', String(on));
  const label = on ? labelOn : labelOff;
  const copy = button.querySelector('.toggle-copy');
  if (copy) copy.textContent = label;
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
  // The timing note answers the same question these placeholders do -- what this panel is
  // showing -- and is gated on the same state, so it is refreshed on the same paths.
  renderLyricsTiming();
}

function updateTabPlaceholders() {
  elements.tabHiddenNote.hidden = state.tabVisible;
  elements.tabEmpty.hidden = !(state.tabVisible && !state.tabEvents.length);
  elements.neckHiddenNote.hidden = state.neckVisible;
  // The neck's own placeholder covers two different truths and says which: no tab to
  // draw, or a browser that would not give this page a 2d context at all.
  const drawable = neckContext() !== null;
  state.neckDrawable = drawable;
  const show = state.neckVisible && (!drawable || !state.tabEvents.length);
  elements.neckEmpty.hidden = !show;
  if (!show) return;
  elements.neckEmpty.textContent = drawable
    ? `No tab events loaded, so this neck is only ${tuningSentence()}.`
    : 'This browser gave the page no 2d canvas, so the fretboard cannot be drawn. '
      + 'The tab lane below carries the whole part.';
}

function tuningSentence() {
  const labels = state.tab && state.tab.tuning ? state.tab.tuning.labels : [];
  const named = labels.length ? labels.slice().reverse().join(' ') : 'E B G D A E';
  return `the strings of ${named}`;
}

function anySolo() {
  return state.tracks.some((track) => track.soloed);
}

// The native surface's audibility rule, kpa_ui.c: a soloed track anywhere silences the
// ones that are not soloed, and a muted track stays silent regardless.
function trackIsAudible(track) {
  const solo = anySolo();
  return !track.muted && (!solo || track.soloed);
}

function updateAudioVolume(track) {
  const record = state.audio.find((item) => item.id === track.id);
  if (record) record.element.volume = trackIsAudible(track) ? track.volume : 0;
}

function updateAllAudioVolumes() {
  // Solo is a property of the whole mixer, so one solo press changes the audibility of
  // every other track and every one of them has to be re-applied.
  state.tracks.forEach(updateAudioVolume);
}

function applyMuteButton(track) {
  const button = track.muteButton;
  if (!button) return;
  button.setAttribute('aria-pressed', String(track.muted));
  button.setAttribute('aria-label', `${track.muted ? 'Unmute' : 'Mute'} ${track.label}`);
  button.textContent = track.muted ? 'MUTED' : 'LIVE';
  applySoloButton(track);
}

function applySoloButton(track) {
  const button = track.soloButton;
  if (!button) return;
  button.setAttribute('aria-pressed', String(track.soloed));
  button.setAttribute('aria-label', `${track.soloed ? 'Unsolo' : 'Solo'} ${track.label}`);
  if (track.levelText) {
    const audible = trackIsAudible(track);
    track.levelText.textContent = audible ? `${Math.round(track.volume * 100)}%` : 'silent';
  }
}

function refreshMixerState() {
  state.tracks.forEach(applySoloButton);
}

function updateTrackMute(track, muted) {
  track.muted = muted;
  updateAllAudioVolumes();
  applyMuteButton(track);
  refreshMixerState();
  updateLayerControls();
  rememberPreferences();
}

function updateTrackSolo(track, soloed) {
  track.soloed = soloed;
  updateAllAudioVolumes();
  refreshMixerState();
  rememberPreferences();
}

function selectTrack(index) {
  if (!state.tracks.length) return;
  state.selectedTrack = clamp(index, 0, state.tracks.length - 1);
  state.tracks.forEach((track, position) => {
    if (!track.control) return;
    const selected = position === state.selectedTrack;
    track.control.classList.toggle('is-selected', selected);
    if (selected) track.control.setAttribute('aria-current', 'true');
    else track.control.removeAttribute('aria-current');
  });
}

function adjustSelectedGain(delta) {
  const track = state.tracks[state.selectedTrack];
  if (!track) return;
  // HTMLMediaElement.volume caps at 1.0. The native mixer goes to 2.0 because it owns the
  // mix; this one cannot boost a quiet stem without an AudioContext, and says so beside
  // the sliders rather than pretending the top of the range is the same thing.
  track.volume = clamp(Number((track.volume + delta).toFixed(2)), 0, 1);
  if (track.volumeInput) track.volumeInput.value = String(track.volume);
  updateAudioVolume(track);
  applySoloButton(track);
  rememberPreferences();
}

function makeTrackControl(track, index) {
  const item = document.createElement('div');
  item.className = 'track-control';
  item.dataset.trackId = track.id;
  item.tabIndex = 0;
  item.setAttribute('role', 'group');
  item.setAttribute('aria-label', `${track.label} stem`);
  // Selection follows focus. Tab is the browser's focus key and can never be a shortcut,
  // so the native surface's "tab cycles stems" becomes "the focused stem is the selected
  // one" here -- the same reachability through the key the platform already owns.
  item.addEventListener('focus', () => selectTrack(index));
  track.control = item;

  const top = document.createElement('div');
  top.className = 'track-control-top';
  const label = document.createElement('span');
  label.className = 'track-label';
  label.textContent = track.label;
  const kind = document.createElement('span');
  kind.className = 'track-kind';
  kind.textContent = text(track.kind, 'audio');
  const level = document.createElement('span');
  level.className = 'track-level';
  track.levelText = level;
  top.append(label, kind, level);

  const row = document.createElement('div');
  row.className = 'track-control-row';
  const mute = document.createElement('button');
  mute.className = 'mute-button';
  mute.type = 'button';
  mute.disabled = Boolean(track.error);
  mute.addEventListener('click', () => updateTrackMute(track, !track.muted));
  track.muteButton = mute;
  const solo = document.createElement('button');
  solo.className = 'solo-button';
  solo.type = 'button';
  solo.textContent = 'SOLO';
  solo.disabled = Boolean(track.error);
  solo.addEventListener('click', () => updateTrackSolo(track, !track.soloed));
  track.soloButton = solo;
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
    applySoloButton(track);
    rememberPreferences();
  });
  track.volumeInput = volume;
  volumeLabel.append(volume);
  row.append(mute, solo, volumeLabel);
  item.append(top, row);
  return item;
}

function applyTrackAvailability(track) {
  // Updated in place rather than by re-rendering #track-list: a stem failing while the
  // user is on a mute button would otherwise take the focus with it, which is the same
  // defect F27 closed for mute clicks.
  if (track.muteButton) track.muteButton.disabled = Boolean(track.error);
  if (track.soloButton) track.soloButton.disabled = Boolean(track.error);
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
  state.tracks.forEach((track, index) => elements.trackList.append(makeTrackControl(track, index)));
  selectTrack(0);
  refreshMixerState();
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
    audio.volume = trackIsAudible(track) ? track.volume : 0;
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
  // raises none is caught within a grace window of the tab coming back on screen. The
  // fretboard rides the same frame callback and freezes with it: while the tab is in the
  // background the neck is stale, and it catches up on the first frame back.
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
  state.minimapKey = '';
  requestNeckPaint();
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
  updateNeckModel(state.position);
  requestNeckPaint();
}

function updateAllPlaybackRates() {
  state.audio.forEach((record) => { record.element.playbackRate = state.rate; });
}

function updateRateReadout() {
  elements.rateValue.textContent = `${Math.round(state.rate * 100)}%`;
}

function setRate(value) {
  const stepped = Math.round(clamp(value, RATE_MIN, RATE_MAX) / RATE_STEP) * RATE_STEP;
  state.rate = Number(stepped.toFixed(2));
  elements.rate.value = String(state.rate);
  updateRateReadout();
  updateAllPlaybackRates();
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
  if (state.loop.active && state.position >= state.loop.end) {
    wrapLoop();
    return;
  }
  correctAudioDrift(state.position);
  checkStemStarvation(window.performance.now());
  updatePlaybackUi();
}

/* ------------------------------------------------------------------------------- loop */

function loopLength() {
  return state.loop.end - state.loop.start;
}

function renderLoopReadout() {
  // Loop messages never reach the notice bar: a refused loop end is the answer to the key
  // just pressed, and covering a dead stem with it would lose the more important message.
  if (state.loopMessage) {
    elements.loopReadout.textContent = state.loopMessage;
    elements.loopReadout.className = 'loop-readout is-warning';
  } else if (state.loop.active) {
    elements.loopReadout.textContent =
      `Loop ${formatTime(state.loop.start)} – ${formatTime(state.loop.end)} `
      + `(${loopLength().toFixed(1)} s)`;
    elements.loopReadout.className = 'loop-readout is-on';
  } else {
    elements.loopReadout.textContent = 'Loop off';
    elements.loopReadout.className = 'loop-readout';
  }
  elements.loopClear.disabled = !state.loop.active && !state.loopMessage;
  updateLoopRegion();
}

function commitLoop(message) {
  state.loopMessage = message;
  renderLoopReadout();
  rememberPreferences();
  requestNeckPaint();
}

function setLoopStart() {
  state.loop.start = clamp(state.position, 0, state.duration || state.position);
  if (state.loop.end <= state.loop.start) {
    // The native rule, kpa_ui.c: a start with no end downstream of it gets one second.
    state.loop.end = clamp(state.loop.start + LOOP_DEFAULT_LENGTH, 0, state.duration || state.position + LOOP_DEFAULT_LENGTH);
  }
  finishLoopEdit();
}

function setLoopEnd() {
  state.loop.end = clamp(state.position, 0, state.duration || state.position);
  finishLoopEdit();
}

function finishLoopEdit() {
  if (state.loop.end <= state.loop.start) {
    state.loop.active = false;
    // The native surface's exact sentence, so the two surfaces refuse the same thing in
    // the same words.
    commitLoop('a loop end must come after its start');
    return;
  }
  if (loopLength() < LOOP_MIN_LENGTH) {
    state.loop.active = false;
    commitLoop(`a loop shorter than ${LOOP_MIN_LENGTH.toFixed(2)} s would re-seek every stem `
      + 'several times a second');
    return;
  }
  state.loop.active = true;
  commitLoop('');
}

function clearLoop() {
  state.loop = { start: 0, end: 0, active: false };
  commitLoop('');
}

function wrapLoop() {
  // The speed ladder is what turns a loop into practice: each pass comes back a little
  // faster until it reaches the rate the user asked for.
  if (state.ladder && state.rate < state.ladderTarget - 1e-9) {
    setRate(Math.min(state.ladderTarget, state.rate + RATE_STEP));
    rememberPreferences();
  }
  // Through setPosition, so a wrap is exactly the seek path the drift corrector and the
  // starvation detector already understand.
  setPosition(Math.max(0, state.loop.start - state.preRoll));
}

function toggleLadder() {
  state.ladder = !state.ladder;
  if (state.ladder) {
    // The rate on screen becomes the target and the first pass starts five steps below
    // it, so the ladder has somewhere to climb from.
    state.ladderTarget = state.rate;
    setRate(Math.max(RATE_MIN, state.ladderTarget - 5 * RATE_STEP));
  } else {
    state.ladderTarget = state.rate;
  }
  setButtonState(elements.ladder, state.ladder, 'Speed ladder', 'Speed ladder off');
  rememberPreferences();
}

function updateLoopRegion() {
  const region = state.tabLoop;
  if (!region) return;
  region.hidden = !state.loop.active;
  region.style.left = `${state.loop.start * state.lanePps}px`;
  region.style.width = `${Math.max(2, loopLength() * state.lanePps)}px`;
}

/* --------------------------------------------------------------------------- minimap */

function canvasContext(canvas, minWidth = 1) {
  // Every entry point into a canvas is feature-guarded: the node harness these files are
  // tested under has no getContext at all, and a card that is hidden has a zero-sized box
  // whose context would only be drawn into and thrown away.
  if (!canvas || typeof canvas.getContext !== 'function') return null;
  if (!canvas.clientWidth || canvas.clientWidth < minWidth || !canvas.clientHeight) return null;
  const context = canvas.getContext('2d');
  return context || null;
}

function devicePixels() {
  // Capped at 2: beyond that the extra pixels cost more than they show on a neck.
  return clamp(Number(window.devicePixelRatio) || 1, 1, 2);
}

function sizeCanvas(canvas, context) {
  const ratio = devicePixels();
  const width = Math.round(canvas.clientWidth * ratio);
  const height = Math.round(canvas.clientHeight * ratio);
  if (canvas.width !== width) canvas.width = width;
  if (canvas.height !== height) canvas.height = height;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  return { width: canvas.clientWidth, height: canvas.clientHeight, ratio };
}

function buildMinimapBitmap(width, height) {
  if (typeof document.createElement !== 'function') return null;
  const bitmap = document.createElement('canvas');
  if (typeof bitmap.getContext !== 'function') return null;
  const ratio = devicePixels();
  bitmap.width = Math.max(1, Math.round(width * ratio));
  bitmap.height = Math.max(1, Math.round(height * ratio));
  const context = bitmap.getContext('2d');
  if (!context) return null;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  const span = Math.max(1, state.duration);
  const columns = Math.max(1, Math.floor(width));
  const counts = new Array(columns).fill(0);
  let peak = 1;
  state.tabEvents.forEach((event) => {
    const column = clamp(Math.floor((number(event.start) / span) * columns), 0, columns - 1);
    counts[column] += 1;
    if (counts[column] > peak) peak = counts[column];
  });
  const barTop = 4;
  const barHeight = height - 12;
  context.fillStyle = NECK_PALETTE.box;
  counts.forEach((count, column) => {
    if (!count) return;
    const tall = Math.max(1.5, (count / peak) * barHeight);
    context.globalAlpha = 0.35 + 0.5 * (count / peak);
    context.fillRect(column, barTop + barHeight - tall, 1, tall);
  });
  context.globalAlpha = 1;
  context.fillStyle = NECK_PALETTE.ghost;
  state.lyrics.forEach((cue) => {
    const x = clamp((number(cue.start) / span) * width, 0, width - 1);
    context.fillRect(x, height - 5, 1, 4);
  });
  return bitmap;
}

function paintMinimap() {
  const context = canvasContext(elements.minimap, 40);
  if (!context) return;
  const box = sizeCanvas(elements.minimap, context);
  const key = `${box.width}x${box.height}x${box.ratio}x${state.tabEvents.length}`
    + `x${state.lyrics.length}x${Math.round(state.duration)}`;
  if (key !== state.minimapKey) {
    state.minimapBitmap = buildMinimapBitmap(box.width, box.height);
    state.minimapKey = key;
  }
  context.clearRect(0, 0, box.width, box.height);
  if (state.minimapBitmap) {
    context.drawImage(state.minimapBitmap, 0, 0, box.width, box.height);
  }
  const span = Math.max(1, state.duration);
  if (state.loop.active) {
    context.fillStyle = NECK_PALETTE.sounding;
    context.globalAlpha = 0.16;
    const left = (state.loop.start / span) * box.width;
    const wide = Math.max(2, (loopLength() / span) * box.width);
    context.fillRect(left, 0, wide, box.height);
    context.globalAlpha = 1;
  }
  const head = (clamp(state.position, 0, span) / span) * box.width;
  context.fillStyle = NECK_PALETTE.sounding;
  context.fillRect(head - 0.5, 0, 1.5, box.height);
}

/* ---------------------------------------------------------------------------- lyrics */

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

function readTiming(value) {
  // Read defensively: this is a document the app fetched, and the only thing it is
  // trusted for here is one of three strings and, for 'measured', an object of numbers.
  if (!value || typeof value !== 'object') return null;
  if (!TIMING_KINDS.includes(value.timing)) {
    // A document written before the field existed. The Python loader falls back to the
    // source id's '-estimated' tail for exactly these, and if this reader does not do the
    // same, the one user the warning is for -- somebody whose project predates the upgrade
    // and whose highlight is a guess -- is the only user who never sees it. Reading a
    // provenance out of that tail is the coupling the field exists to end; it survives
    // here, as there, only for the documents that predate the field.
    const legacy = typeof value.source === 'string' && value.source.endsWith('-estimated');
    return legacy ? { kind: 'estimated', report: null } : null;
  }
  const measured = value.timing === 'measured';
  const report = measured && value.alignment && typeof value.alignment === 'object'
    ? value.alignment
    : null;
  return { kind: value.timing, report };
}

function timingCopy(timing) {
  if (timing.kind === 'authored') {
    return {
      tone: 'timing-good',
      tag: 'Timing from the source',
      detail: 'The timings came with the words, so each line sits where the source put it.',
    };
  }
  if (timing.kind === 'estimated') {
    return {
      tone: 'timing-warn',
      tag: 'Approximate timing',
      detail: 'Nothing timed these words. The lines are spread evenly across the song, so a '
        + 'highlighted line can be seconds away from what you are hearing.',
    };
  }
  // 'measured'. The tag says what happened; the detail is the report's numbers, and each
  // one is spoken only when the report really carries it. A report that is absent, partial
  // or unreadable therefore costs a sentence rather than the whole note.
  const report = timing.report || {};
  const detail = [];
  const matched = Number(report.matched_fraction);
  if (Number.isFinite(matched)) {
    // Floored, so the share claimed as measured is never rounded up into a stronger claim
    // than the aligner made -- but nudged first by an epsilon, because the native surface
    // does the same and the two must not describe one document differently. 0.29 * 100 is
    // 28.999999999999996 in a double, and a bare floor turns 29% into 28% there; it bites
    // 20 of the 125,750 fractions n/d with d <= 500. The nudge is far below one percent, so
    // it can only recover a value the multiplication lost, never round a real 28.6 up.
    const percent = clamp(Math.floor(matched * 100 + 1e-9), 0, 100);
    detail.push(`${percent}% of the words were matched to the recording.`);
  }
  const guessed = Math.round(Number(report.interpolated_words));
  if (Number.isFinite(guessed) && guessed > 0) {
    detail.push(guessed === 1
      ? '1 word matched nothing in the transcript and sits between the words either side of it.'
      : `${guessed} words matched nothing in the transcript and sit between the words either `
        + 'side of them.');
  }
  const bound = Number(report.mean_displacement);
  // mean_displacement is a mean worst case rather than an observed error, so it is spoken
  // as a bound and rounded up: the figure shown is never below the reported one. A bound
  // that rounds to zero is left unsaid -- the aligner measured every word it placed, and
  // "up to 0.00s" would only read as noise.
  const seconds = Number.isFinite(bound) && bound > 0 ? Math.ceil(bound * 100) / 100 : 0;
  if (seconds > 0) {
    detail.push(`On average a word could be up to ${seconds.toFixed(2)}s from where it was sung.`);
  }
  if (!detail.length) {
    detail.push('The document did not say how much of that timing was measured.');
  }
  if (report.usable === false) {
    // The aligner's own verdict, which the pipeline reads as "keep the timing you had".
    // A document that ships measured spans anyway is not second-guessed here; it is
    // reported, because the highlight would otherwise look as confident as any other.
    return {
      tone: 'timing-warn',
      tag: 'Alignment rated unusable',
      detail: `${detail.join(' ')} The aligner rated that unusable, so treat the highlight `
        + 'as approximate.',
    };
  }
  return { tone: 'timing-info', tag: 'Aligned to the audio', detail: detail.join(' ') };
}

function renderLyricsTiming() {
  // Three things have to hold before the note earns its line: the document said where its
  // timing came from, the layer that draws the highlight is on, and there are cues to
  // highlight. Otherwise it is hidden and holds no text and no tone, so a document without
  // the field -- every document written before it existed -- leaves the panel unchanged.
  const show = Boolean(state.lyricsTiming) && state.lyricsVisible && state.lyrics.length > 0;
  elements.lyricsTiming.hidden = !show;
  if (!show) {
    elements.lyricsTiming.className = 'timing-note';
    elements.lyricsTimingTag.textContent = '';
    elements.lyricsTimingDetail.textContent = '';
    return;
  }
  const copy = timingCopy(state.lyricsTiming);
  elements.lyricsTiming.className = `timing-note ${copy.tone}`;
  // textContent, like every other string this app takes from a document it fetched.
  elements.lyricsTimingTag.textContent = copy.tag;
  elements.lyricsTimingDetail.textContent = copy.detail;
}

function renderLyrics() {
  elements.lyricsList.replaceChildren();
  state.lyricsResolved = true;
  updateLyricsPlaceholders();
  state.minimapKey = '';
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
    const loopButton = document.createElement('button');
    loopButton.className = 'lyric-loop';
    loopButton.type = 'button';
    loopButton.textContent = '⟲';
    loopButton.title = 'Loop this line';
    loopButton.setAttribute('aria-label', `Loop the lyric line at ${formatTime(cue.start)}`);
    loopButton.addEventListener('click', () => loopCue(cue));
    item.append(button, loopButton);
    elements.lyricsList.append(item);
  });
}

function loopCue(cue) {
  // 55 lyric cues are the only real structure in this data: the tab has no rest longer
  // than 0.8 s anywhere in 937 events, so phrase boundaries cannot be found in it.
  const start = number(cue.start);
  const end = Math.max(start + LOOP_DEFAULT_LENGTH, number(cue.end, start + LOOP_DEFAULT_LENGTH));
  state.loop.start = clamp(start, 0, state.duration || start);
  state.loop.end = clamp(end, 0, state.duration || end);
  finishLoopEdit();
  setPosition(Math.max(0, state.loop.start - state.preRoll));
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
  viewport.scrollTo({ top: clamp(centred, 0, limit), behavior: motionAllowed() ? 'smooth' : 'auto' });
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

/* -------------------------------------------------------------------------- tab lane */

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

function eventPositions(event) {
  return Array.isArray(event.positions) ? event.positions : [];
}

function eventEnd(event) {
  return number(event.end, number(event.start) + 0.18);
}

function buildRuler(seconds) {
  const ruler = document.createElement('div');
  ruler.className = 'tab-ruler';
  ruler.setAttribute('aria-hidden', 'true');
  // Seconds, not bars. The tab document carries start, end and positions and nothing
  // else: there is no tempo anywhere in this pipeline, so a beat grid would be invented.
  for (let second = 0; second <= Math.ceil(seconds); second += 1) {
    const tick = document.createElement('span');
    const labelled = second % 5 === 0;
    tick.className = labelled ? 'tab-tick tab-tick-major' : 'tab-tick';
    tick.style.left = `${second * state.lanePps}px`;
    if (labelled) tick.textContent = formatTime(second);
    ruler.append(tick);
  }
  return ruler;
}

function renderTab() {
  elements.tabGrid.replaceChildren();
  elements.tabGutter.replaceChildren();
  state.activeTabEvent = -1;
  state.tabNotes = [];
  state.tabWorld = null;
  state.tabLoop = null;
  if (!state.tab || !state.tab.events.length) {
    state.tabEvents = [];
    state.tabStringCount = DEFAULT_TUNING.length;
    elements.tabStatus.textContent = 'No tab available';
    elements.tuning.textContent = 'Tuning unavailable';
    elements.fret.textContent = 'Max fret --';
    prepareNeck();
    updateTabPlaceholders();
    return;
  }
  state.tabEvents = state.tab.events;
  const labels = state.tab.tuning.labels;
  const rowCount = Math.max(DEFAULT_TUNING.length, labels.length, state.tab.tuning.midi.length);
  state.tabStringCount = rowCount;
  const world = document.createElement('div');
  world.className = 'tab-world';
  const lastEvent = state.tab.events[state.tab.events.length - 1];
  const tabSeconds = Math.max(state.duration, number(lastEvent.end, lastEvent.start), 1);
  // The trailing pad is the part of the lane that is still the future when the playhead
  // reaches the last note: the head sits PLAYHEAD_FRACTION in, so the rest of the
  // viewport has to exist to the right of the final event.
  world.style.width = `max(100%, calc(75vw + ${tabSeconds * state.lanePps}px))`;
  world.append(buildRuler(tabSeconds));
  const loopRegion = document.createElement('div');
  loopRegion.className = 'tab-loop';
  loopRegion.hidden = true;
  world.append(loopRegion);
  state.tabLoop = loopRegion;
  // JavaScript owns the time axis and the number of rows; the stylesheet owns every
  // vertical size, so the gutter, the rows and the notes inside them cannot drift apart.
  elements.tabViewport.style.setProperty('--tab-rows', String(rowCount));
  const rows = document.createElement('div');
  rows.className = 'tab-rows';
  const lineRows = [];
  for (let displayRow = 0; displayRow < rowCount; displayRow += 1) {
    // The DOM order stays canonical -- row 0 is the highest string, tablature's order --
    // and the low-e-top preference flips the picture in CSS alone. One arithmetic, one
    // place, and the two views of the instrument can never end up disagreeing.
    const sourceString = rowCount - 1 - displayRow;
    const row = document.createElement('div');
    row.className = 'tab-row';
    row.dataset.string = String(sourceString);
    rows.append(row);
    lineRows[sourceString] = row;
    const label = document.createElement('span');
    label.className = 'tab-string-label';
    const name = document.createElement('strong');
    name.textContent = text(labels[sourceString], `S${stringNumber(sourceString)}`);
    const player = document.createElement('span');
    player.className = 'tab-string-number';
    player.textContent = String(stringNumber(sourceString));
    label.append(name, player);
    elements.tabGutter.append(label);
  }
  state.tabEvents.forEach((event, index) => {
    const positions = eventPositions(event);
    const notes = [];
    positions.forEach((position) => {
      const stringIndex = Math.floor(number(position.string, -1));
      const row = lineRows[stringIndex];
      if (!row || stringIndex < 0) return;
      const note = document.createElement('span');
      note.className = 'tab-note';
      note.dataset.eventIndex = String(index);
      note.dataset.start = String(number(event.start));
      note.style.left = `${number(event.start) * state.lanePps}px`;
      const duration = Math.max(0.14, eventEnd(event) - number(event.start));
      note.style.width = `${Math.max(24, duration * state.lanePps)}px`;
      note.textContent = String(Math.max(0, Math.floor(number(position.fret, 0))));
      note.title = `String ${stringNumber(stringIndex)}, fret ${note.textContent}`;
      row.append(note);
      notes.push(note);
    });
    state.tabNotes[index] = notes;
  });
  world.append(rows);
  elements.tabGrid.append(world);
  state.tabWorld = world;
  const tuningText = labels.slice().reverse().join(' · ') || 'Standard tuning';
  elements.tuning.textContent = tuningText;
  elements.fret.textContent = `Max fret ${state.tab.tuning.maxFret}`;
  elements.tabStatus.textContent = `${state.tabEvents.length} note events`;
  prepareNeck();
  updateTabPlaceholders();
  updateLoopRegion();
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
  return time <= eventEnd(event) ? candidate : -1;
}

// The pixels-per-second the lane runs at, and the seconds of future its right-hand side
// shows. The corridor reads ahead over the same span, so nothing can appear on the neck's
// approach that is not also on the lane.
function laneFutureSeconds() {
  const width = number(elements.tabViewport.clientWidth, 0);
  if (!width) return READ_AHEAD_MAX;
  return (width * (1 - PLAYHEAD_FRACTION)) / state.lanePps;
}

function neckReadAhead() {
  return Math.min(READ_AHEAD_MAX, laneFutureSeconds());
}

function setActiveTabEvent(active) {
  if (active === state.activeTabEvent) return;
  // Only the two events that changed are touched. Re-scanning every note span -- 1830 of
  // them on the reference song -- on each of 937 changes was the previous cost.
  const previous = state.tabNotes[state.activeTabEvent];
  if (previous) previous.forEach((note) => note.classList.remove('is-active'));
  const next = state.tabNotes[active];
  if (next) next.forEach((note) => note.classList.add('is-active'));
  state.activeTabEvent = active;
}

function updateTab(time) {
  const world = state.tabWorld;
  if (!world || !state.tabEvents.length) return;
  const active = tabIndexAt(time);
  setActiveTabEvent(active);
  // The onset of an event lands exactly on the playhead: the world is translated by
  // (head - time * pps) and a note's own left is (start * pps), so at time == start the
  // note's left edge is at head. The line and the highlight now agree; they did not
  // before, because the translation carried a 48 px correction the line did not.
  const center = number(elements.tabViewport.clientWidth, 0) * PLAYHEAD_FRACTION;
  world.style.transform = `translateX(${center - time * state.lanePps}px)`;
  if (active >= 0) {
    const event = state.tabEvents[active];
    const positions = eventPositions(event);
    const frets = positions.map(
      (position) => `S${stringNumber(number(position.string))}:${number(position.fret)}`,
    ).join(' · ');
    elements.tabPosition.textContent = frets || `Event at ${formatTime(event.start)}`;
  } else {
    elements.tabPosition.textContent = state.playing ? 'Listen for the next note' : 'Ready to play';
  }
}

function seekFromLane(event) {
  const target = event.target;
  if (target && target.dataset && target.dataset.start !== undefined) {
    setPosition(number(target.dataset.start));
    return;
  }
  const viewport = elements.tabViewport;
  if (typeof viewport.getBoundingClientRect !== 'function') return;
  const box = viewport.getBoundingClientRect();
  const offset = number(event.clientX, box.left) - box.left;
  setPosition(state.position + (offset - box.width * PLAYHEAD_FRACTION) / state.lanePps);
}

/* ------------------------------------------------------------------------- fretboard */

function requestNeckPaint() {
  state.neckDirty = true;
}

function neckContext() {
  return canvasContext(elements.neckCanvas, 60);
}

function prepareNeck() {
  let maxUsed = 0;
  let maxDuration = 0.25;
  state.tabEvents.forEach((event) => {
    maxDuration = Math.max(maxDuration, eventEnd(event) - number(event.start));
    eventPositions(event).forEach((position) => {
      maxUsed = Math.max(maxUsed, Math.floor(number(position.fret, 0)));
    });
  });
  state.tabMaxDuration = maxDuration;
  const maxFret = state.tab ? Math.max(12, Math.round(state.tab.tuning.maxFret)) : 12;
  // The board is anchored at the nut and reaches one fret past the highest the song uses,
  // so x is exactly the contract's cell(fret) / d(N) and both surfaces put a note at the
  // same fraction of the board. A window that slid up the neck as the hand moved would
  // need an x mapping docs/FRETBOARD.md does not define, and the two would drift; the
  // hand-position box carries "where am I" instead.
  state.neckHighestFret = clamp(Math.max(maxUsed + 1, 12), 12, maxFret);
  state.neckStaticKey = '';
  state.impacts = [];
  state.readoutKey = '';
  state.lastModelTime = state.position;
  requestNeckPaint();
}

function firstEventNear(time) {
  // Binary search back far enough that an event which started before the window can still
  // be sounding inside it: the longest note in the reference song is 6.05 s.
  const back = time - number(state.tabMaxDuration, 1) - HAND_WINDOW_BACK;
  let low = 0;
  let high = state.tabEvents.length - 1;
  let found = state.tabEvents.length;
  while (low <= high) {
    const middle = Math.floor((low + high) / 2);
    if (number(state.tabEvents[middle].start) >= back) {
      found = middle;
      high = middle - 1;
    } else {
      low = middle + 1;
    }
  }
  return found;
}

function pushImpacts(event) {
  eventPositions(event).forEach((position) => {
    if (state.impacts.length >= IMPACT_LIMIT) return;
    const start = number(event.start);
    state.impacts.push({
      string: Math.floor(number(position.string, 0)),
      fret: Math.max(0, Math.floor(number(position.fret, 0))),
      at: start,
      // The flash never outlives the note that caused it, and never runs past the ring.
      until: Math.max(start + IMPACT_RING, Math.min(eventEnd(event), start + 0.6)),
    });
  });
}

function updateNeckModel(time) {
  const events = state.tabEvents;
  state.sounding = [];
  state.approach = [];
  state.nextEvent = -1;
  state.activeEvent = -1;
  if (!events.length) {
    state.handBox = null;
    state.impacts = [];
    state.lastModelTime = time;
    updateReadouts(time);
    return;
  }
  const from = time - HAND_WINDOW_BACK;
  const to = time + HAND_WINDOW_FORWARD;
  const readAhead = neckReadAhead();
  const horizon = Math.max(to, time + readAhead);
  // A seek is not a performance: crossing 40 seconds of onsets in one frame must not fire
  // 40 attack flashes at once.
  const crossed = motionAllowed() && Math.abs(time - number(state.lastModelTime, time)) <= 0.5;
  const frets = [];
  for (let index = firstEventNear(time); index < events.length; index += 1) {
    const event = events[index];
    const start = number(event.start);
    if (start > horizon) break;
    const end = eventEnd(event);
    if (end > from && start < to) {
      eventPositions(event).forEach((position) => {
        frets.push(Math.floor(number(position.fret, 0)));
      });
    }
    if (start <= time && time <= end) state.sounding.push(index);
    if (start > time) {
      if (state.nextEvent < 0) state.nextEvent = index;
      if (start - time <= readAhead) state.approach.push(index);
    }
    if (crossed && start > state.lastModelTime && start <= time) pushImpacts(event);
  }
  state.handBox = handPositionBox(frets, state.tab ? state.tab.tuning.maxFret : 20);
  // Events are sorted by start, so the last one sounding is the one with the greatest
  // start at or before t -- the same event tabIndexAt marks active in the lane. One rule,
  // one answer, and the name beside the neck is the name of the shape the lane highlights.
  state.activeEvent = state.sounding.length ? state.sounding[state.sounding.length - 1] : -1;
  state.lastModelTime = time;
  state.impacts = state.impacts.filter((impact) => time >= impact.at && time < impact.until);
  updateReadouts(time);
}

function openPitch(apiIndex) {
  const midi = state.tab && Array.isArray(state.tab.tuning.midi) ? state.tab.tuning.midi : [];
  return number(midi[apiIndex], NaN);
}

// The pitches of ONE event -- the event the lane highlights -- because that is what
// docs/FRETBOARD.md names and what the native surface will hand its own namer. The union
// of everything still ringing is a different set and would produce a different name: at
// 69.735 s of the reference song a single fretted F from the previous event overlaps the
// six-note shape, and naming the union calls it F where the shape is F/C. Two surfaces
// printing different names for one instant is exactly what the shared contract exists to
// stop, so the ringing note is still DRAWN on the neck and is not part of the name.
function chordPitches() {
  const event = state.activeEvent >= 0 ? state.tabEvents[state.activeEvent] : null;
  if (!event) return [];
  const pitches = [];
  eventPositions(event).forEach((position) => {
    // Derived from {string, fret} and the tuning rather than read from the artifact's
    // `pitch`: the two are the same by construction, and when an artifact disagrees with
    // itself the position is what the neck and the lane are already drawing.
    const open = openPitch(Math.floor(number(position.string, -1)));
    if (Number.isFinite(open)) pitches.push(open + Math.max(0, Math.floor(number(position.fret, 0))));
  });
  return pitches;
}

function eventSummary(index) {
  const event = state.tabEvents[index];
  if (!event) return '';
  const positions = eventPositions(event);
  const frets = positions
    .slice()
    .sort((left, right) => number(right.string) - number(left.string))
    .map((position) => Math.max(0, Math.floor(number(position.fret, 0))))
    .join(' ');
  const count = positions.length === 1 ? '1 note' : `${positions.length} notes`;
  return `${count} · ${frets}`;
}

function updateReadouts(time) {
  const next = state.nextEvent >= 0 ? state.tabEvents[state.nextEvent] : null;
  const countdown = next ? Math.max(0, Math.round((number(next.start) - time) * 10)) : -1;
  // Keyed on the underlying event rather than the frame, so the DOM is written when the
  // music changes and at most ten times a second for the countdown -- never 60.
  const key = `${state.activeEvent}|${state.nextEvent}|${countdown}`
    + `|${state.handBox ? state.handBox.join('-') : ''}|${motionAllowed()}`;
  if (key === state.readoutKey) return;
  state.readoutKey = key;

  const pitches = chordPitches();
  const name = chordName(pitches);
  elements.chordReadout.textContent = name || (pitches.length ? '—' : 'silence');
  const seen = [];
  pitches.slice().sort((left, right) => left - right).forEach((pitch) => {
    const spelled = PITCH_NAMES[pitchClass(pitch)];
    if (!seen.includes(spelled)) seen.push(spelled);
  });
  elements.chordNotes.textContent = seen.length
    ? `${seen.join(' ')} · from the notes in this shape`
    : 'nothing sounding';

  if (state.handBox) {
    elements.positionReadout.textContent =
      `Position ${romanNumeral(state.handBox[0])} · frets ${state.handBox[0]}–${state.handBox[1]}`;
  } else {
    elements.positionReadout.textContent = 'No hand position · open strings or a rest';
  }

  if (next) {
    const gap = Math.max(0, number(next.start) - time);
    elements.nextReadout.textContent = `next in ${gap.toFixed(1)} s · ${eventSummary(state.nextEvent)}`;
  } else {
    elements.nextReadout.textContent = state.tabEvents.length ? 'no more notes' : 'no tab loaded';
  }

  // Under reduced motion the corridor is not drawn at all, so the same information is
  // spelled out instead: what is next, and how long there is to get there.
  const still = !motionAllowed();
  elements.neckStrip.hidden = !still;
  if (!still) return;
  const chips = [];
  for (let step = 0; step < 2; step += 1) {
    const index = state.nextEvent >= 0 ? state.nextEvent + step : -1;
    if (index < 0 || index >= state.tabEvents.length) break;
    const chip = document.createElement('span');
    chip.className = 'neck-chip';
    const gap = Math.max(0, number(state.tabEvents[index].start) - time);
    chip.textContent = `in ${gap.toFixed(1)} s · ${eventSummary(index)}`;
    chips.push(chip);
  }
  elements.neckStrip.replaceChildren(...chips);
}

/* ----------------------------------------------------------------- fretboard drawing */

function neckGeometry(width, height) {
  const count = Math.max(1, state.tabStringCount);
  const corridor = height >= 250 ? CORRIDOR_H : CORRIDOR_H_SHORT;
  const numbers = 24;
  // Room to the left of the nut for the open-string rings, which sit ON the nut.
  const padLeft = 30;
  const padRight = 12;
  const boardWidth = Math.max(60, width - padLeft - padRight);
  const top = corridor;
  const bottom = Math.max(top + 40, height - numbers);
  const band = bottom - top;
  return {
    width,
    height,
    count,
    corridor,
    top,
    bottom,
    band,
    spacing: band / count,
    centre: top + band / 2,
    // How far a note travels between the far plane at y = 0 and the string it lands on.
    // The last stretch of that travel is over the board, which is exactly what an
    // approaching note does: it arrives at the fret rather than stopping short of it.
    rise: top + band / 2,
    padLeft,
    boardWidth,
    highest: state.neckHighestFret,
  };
}

function neckX(geometry, u) {
  const x = geometry.padLeft + u * geometry.boardWidth;
  // Handedness mirrors the x mapping only. Mirroring the canvas transform instead would
  // mirror every fret number with it.
  return state.handed === 'left' ? geometry.width - x : x;
}

function wireX(geometry, fret) {
  return neckX(geometry, displayNormalized(fret, geometry.highest));
}

function dotX(geometry, fret) {
  // An open string is marked at the nut; a fretted note sits at the middle of the space
  // behind the wire, which is where the finger goes. Through positionX, which is the
  // half of positionPoint the fixture pins -- the drawing and the vector-checked function
  // are the same arithmetic rather than two copies of it that could drift.
  return neckX(geometry, positionX(fret, geometry.highest));
}

function stringY(geometry, apiIndex) {
  return geometry.top + positionY(state.orientation, apiIndex, geometry.count) * geometry.band;
}

function stringWidthPx(geometry, apiIndex) {
  // Rendered width is the contract's max(1, round(base * width_ratio)); base scales with
  // the string spacing so the grading survives a narrow card.
  const base = clamp(geometry.spacing * 0.068, 1, 2.4);
  return Math.max(1, Math.round(base * stringWidthRatio(apiIndex)));
}

function buildNeckStatic(geometry, ratio) {
  if (typeof document.createElement !== 'function') return null;
  const bitmap = document.createElement('canvas');
  if (typeof bitmap.getContext !== 'function') return null;
  bitmap.width = Math.max(1, Math.round(geometry.width * ratio));
  bitmap.height = Math.max(1, Math.round(geometry.height * ratio));
  const context = bitmap.getContext('2d');
  if (!context) return null;
  context.setTransform(ratio, 0, 0, ratio, 0, 0);

  const board = context.createLinearGradient(0, geometry.top, 0, geometry.bottom);
  board.addColorStop(0, NECK_PALETTE.boardTop);
  board.addColorStop(1, NECK_PALETTE.boardBottom);
  context.fillStyle = board;
  context.fillRect(0, geometry.top, geometry.width, geometry.band);

  context.strokeStyle = NECK_PALETTE.grain;
  context.globalAlpha = 0.5;
  context.lineWidth = 1;
  for (let stroke = 0; stroke < 8; stroke += 1) {
    const y = geometry.top + ((stroke + 0.5) / 8) * geometry.band;
    context.beginPath();
    context.moveTo(0, y);
    context.bezierCurveTo(
      geometry.width * 0.3, y + (stroke % 2 ? 2.5 : -2.5),
      geometry.width * 0.7, y + (stroke % 2 ? -2.5 : 2.5),
      geometry.width, y,
    );
    context.stroke();
  }
  context.globalAlpha = 1;

  context.fillStyle = NECK_PALETTE.boardEdge;
  context.fillRect(0, geometry.top - 2, geometry.width, 2);
  context.fillRect(0, geometry.bottom, geometry.width, 2);

  // Fret wires. Geometric spacing: the cells crowd by exactly 2:1 across any twelve
  // frets, which is what stops a drawn neck from reading as a grid.
  const wire = context.createLinearGradient(0, geometry.top, 0, geometry.bottom);
  wire.addColorStop(0, NECK_PALETTE.fretHigh);
  wire.addColorStop(0.5, NECK_PALETTE.fretLow);
  wire.addColorStop(1, NECK_PALETTE.fretHigh);
  context.strokeStyle = wire;
  context.lineWidth = 2;
  for (let fret = 1; fret <= geometry.highest; fret += 1) {
    const x = wireX(geometry, fret);
    context.beginPath();
    context.moveTo(x, geometry.top);
    context.lineTo(x, geometry.bottom);
    context.stroke();
  }
  // The nut is bone, not wire, and it is what stops an open string.
  context.fillStyle = NECK_PALETTE.nut;
  const nutX = wireX(geometry, 0);
  context.fillRect(nutX - (state.handed === 'left' ? 0 : 5), geometry.top - 2, 5, geometry.band + 4);

  // Inlays sit at the cell centre, never on the wire: on the wire every marker shifts
  // toward the bridge and the neck reads as mis-spaced even when the wires are right.
  context.fillStyle = NECK_PALETTE.inlay;
  context.globalAlpha = 0.55;
  const radius = clamp(geometry.spacing * 0.17, 2, 6);
  INLAY_SINGLE.forEach((fret) => {
    if (fret > geometry.highest) return;
    context.beginPath();
    context.arc(dotX(geometry, fret), geometry.centre, radius, 0, Math.PI * 2);
    context.fill();
  });
  INLAY_DOUBLE.forEach((fret) => {
    if (fret > geometry.highest) return;
    const offset = geometry.band * 0.24;
    [-offset, offset].forEach((delta) => {
      context.beginPath();
      context.arc(dotX(geometry, fret), geometry.centre + delta, radius, 0, Math.PI * 2);
      context.fill();
    });
  });
  context.globalAlpha = 1;

  // Fret numbers under the board, drawn only where a cell is wide enough to hold one.
  context.fillStyle = NECK_PALETTE.numeral;
  context.font = '700 10px ui-sans-serif, system-ui, sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'top';
  for (let fret = 1; fret <= geometry.highest; fret += 1) {
    const cell = Math.abs(wireX(geometry, fret) - wireX(geometry, fret - 1));
    if (cell < 14) continue;
    context.fillText(String(fret), dotX(geometry, fret), geometry.bottom + 5);
  }

  // The fret lines continue up the corridor toward the vanishing point, so an approaching
  // note is read against the same frets it will land on.
  if (geometry.corridor > 0) {
    const vanishX = neckX(geometry, 0.5);
    // The rail is drawn between the far plane and the board's top edge only; below that
    // the fret wire itself carries the line.
    const nearScale = 1 - (1 - CORRIDOR_SCALE_MIN) * ((geometry.centre - geometry.top) / geometry.rise);
    context.strokeStyle = NECK_PALETTE.rail;
    context.globalAlpha = 0.28;
    context.lineWidth = 1;
    for (let fret = 0; fret <= geometry.highest; fret += 1) {
      const x = wireX(geometry, fret);
      context.beginPath();
      context.moveTo(vanishX + (x - vanishX) * nearScale, geometry.top);
      context.lineTo(vanishX + (x - vanishX) * CORRIDOR_SCALE_MIN, 0);
      context.stroke();
    }
    context.globalAlpha = 1;
  }
  return bitmap;
}

function corridorPoint(geometry, dt, readAhead, apiIndex, fret) {
  // Foreshortening: a note readAhead seconds away is drawn at CORRIDOR_SCALE_MIN of full
  // size and dt = 0 lands exactly on the string. The per-string offset shrinks with the
  // rest, so two adjacent strings are still about a quarter of their spacing apart at the
  // far edge -- enough, with fixed per-string colours, to read a barre as a barre.
  const horizon = readAhead * CORRIDOR_SCALE_MIN / (1 - CORRIDOR_SCALE_MIN);
  const scale = 1 / (1 + Math.max(0, dt) / horizon);
  const vanishX = neckX(geometry, 0.5);
  const row = orientationRow(state.orientation, apiIndex, geometry.count);
  const base = geometry.centre - ((1 - scale) / (1 - CORRIDOR_SCALE_MIN)) * geometry.rise;
  return {
    x: vanishX + (dotX(geometry, fret) - vanishX) * scale,
    y: base + (row - (geometry.count - 1) / 2) * geometry.spacing * scale,
    scale,
  };
}

function stringAmplitude(apiIndex, time, geometry) {
  if (!motionAllowed()) return null;
  let best = null;
  state.impacts.forEach((impact) => {
    if (impact.string !== apiIndex) return;
    const age = time - impact.at;
    if (age < 0) return;
    const amplitude = 0.35 * geometry.spacing * Math.exp(-age / IMPACT_DECAY);
    if (!best || amplitude > best.amplitude) best = { amplitude, fret: impact.fret, age };
  });
  return best && best.amplitude > 0.3 ? best : null;
}

function paintStrings(context, geometry, time) {
  const near = state.handed === 'left' ? geometry.width : 0;
  const far = state.handed === 'left' ? 0 : geometry.width;
  for (let api = 0; api < geometry.count; api += 1) {
    const y = stringY(geometry, api);
    // Wound versus plain steel is visible on a real instrument, so it is drawn. Which
    // strings are wound is a property of the set, not of the contract.
    const wound = api < Math.floor(geometry.count / 2);
    context.strokeStyle = wound ? NECK_PALETTE.wound : NECK_PALETTE.plain;
    context.lineWidth = stringWidthPx(geometry, api);
    const buzz = stringAmplitude(api, time, geometry);
    context.beginPath();
    if (!buzz) {
      context.moveTo(near, y);
      context.lineTo(far, y);
    } else {
      const anchor = wireX(geometry, buzz.fret);
      context.moveTo(near, y);
      context.lineTo(anchor, y);
      // A visual 9 Hz plus a per-string offset, NOT the string's pitch: the real
      // fundamentals are 82-330 Hz and would alias into a stroboscopic mess at 60 fps.
      const phase = time * VIBRATION_HZ * Math.PI * 2 + api * 1.1;
      for (let step = 1; step <= 24; step += 1) {
        const ratio = step / 24;
        context.lineTo(
          anchor + (far - anchor) * ratio,
          y + Math.sin(Math.PI * ratio) * Math.sin(phase) * buzz.amplitude,
        );
      }
    }
    context.stroke();
  }
}

function paintHandBox(context, geometry) {
  if (!state.handBox) return;
  const [low, high] = state.handBox;
  const left = wireX(geometry, Math.max(0, low - 1));
  const right = wireX(geometry, Math.min(geometry.highest, high));
  context.fillStyle = NECK_PALETTE.box;
  context.globalAlpha = 0.16;
  context.fillRect(Math.min(left, right), geometry.top, Math.abs(right - left), geometry.band);
  context.globalAlpha = 1;
  // The numeral goes where the box starts, in the strip under the board -- and the static
  // bitmap's Arabic number for that fret is cleared first, because two labels for one fret
  // in two alphabets is worse than either alone.
  const numeralX = dotX(geometry, low);
  context.clearRect(numeralX - 13, geometry.bottom + 2, 26, geometry.height - geometry.bottom - 2);
  context.fillStyle = NECK_PALETTE.ghost;
  context.font = '800 11px ui-sans-serif, system-ui, sans-serif';
  context.textAlign = 'center';
  context.textBaseline = 'top';
  context.fillText(romanNumeral(low), numeralX, geometry.bottom + 5);
}

function paintCorridor(context, geometry, time, readAhead) {
  if (!motionAllowed() || geometry.corridor <= 0) return;
  // Farthest first, so a nearer shape draws over the ones behind it.
  for (let position = state.approach.length - 1; position >= 0; position -= 1) {
    const index = state.approach[position];
    const event = state.tabEvents[index];
    const start = number(event.start);
    const dt = start - time;
    const length = Math.max(0.06, eventEnd(event) - start);
    const heads = [];
    eventPositions(event).forEach((entry) => {
      const api = Math.floor(number(entry.string, -1));
      if (api < 0 || api >= geometry.count) return;
      const fret = Math.max(0, Math.floor(number(entry.fret, 0)));
      const head = corridorPoint(geometry, dt, readAhead, api, fret);
      const tail = corridorPoint(geometry, dt + length, readAhead, api, fret);
      const hue = STRING_HUES[api % STRING_HUES.length];
      // The sustain runs back up the corridor from the head: the attack arrives first and
      // the body of the note follows it in.
      context.strokeStyle = hue;
      context.globalAlpha = 0.26;
      context.lineWidth = Math.max(1, 4.5 * head.scale);
      context.beginPath();
      context.moveTo(head.x, head.y);
      context.lineTo(tail.x, tail.y);
      context.stroke();
      context.globalAlpha = 1;
      const size = 14 * head.scale;
      context.fillStyle = hue;
      context.fillRect(head.x - size / 2, head.y - size / 2, size, size);
      context.strokeStyle = NECK_PALETTE.ghost;
      context.lineWidth = 1;
      context.strokeRect(head.x - size / 2, head.y - size / 2, size, size);
      if (head.scale > 0.75) {
        context.fillStyle = '#12190f';
        context.font = '800 9px ui-sans-serif, system-ui, sans-serif';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(String(fret), head.x, head.y);
      }
      heads.push({ ...head, row: orientationRow(state.orientation, api, geometry.count) });
    });
    // One faint line through the shape, so a chord reads as a shape while it is still in
    // the distance rather than as loose dots. Only for shapes near enough to resolve: far
    // out, six of these crossing each other is noise rather than information. Joined in
    // row order, so the line runs down the strings instead of zig-zagging through
    // whatever order the artifact happened to list the positions in.
    if (heads.length > 1 && heads[0].scale > 0.45) {
      const ordered = heads.slice().sort((left, right) => left.row - right.row);
      context.strokeStyle = NECK_PALETTE.ghost;
      context.globalAlpha = 0.24;
      context.lineWidth = 1;
      context.beginPath();
      context.moveTo(ordered[0].x, ordered[0].y);
      ordered.slice(1).forEach((point) => context.lineTo(point.x, point.y));
      context.stroke();
      context.globalAlpha = 1;
    }
  }
}

function fingerFor(fret) {
  if (!state.handBox || fret < 1) return 0;
  return clamp(fret - state.handBox[0] + 1, 1, 4);
}

function paintSoundingNotes(context, geometry, time) {
  const cell = Math.abs(wireX(geometry, geometry.highest) - wireX(geometry, geometry.highest - 1));
  const radius = clamp(geometry.spacing * 0.36, 5, 13);
  const held = [];
  state.sounding.forEach((index) => {
    eventPositions(state.tabEvents[index]).forEach((entry) => {
      const api = Math.floor(number(entry.string, -1));
      if (api < 0 || api >= geometry.count) return;
      const fret = Math.max(0, Math.floor(number(entry.fret, 0)));
      held.push(api);
      const x = dotX(geometry, fret);
      const y = stringY(geometry, api);
      context.beginPath();
      context.arc(x, y, radius, 0, Math.PI * 2);
      if (fret === 0) {
        // Open: a hollow ring on the nut, the way a chord chart marks it.
        context.strokeStyle = NECK_PALETTE.sounding;
        context.lineWidth = 2;
        context.stroke();
        return;
      }
      context.fillStyle = NECK_PALETTE.sounding;
      context.fill();
      if (radius >= 8 && cell >= 18) {
        context.fillStyle = '#241d08';
        context.font = '800 11px ui-sans-serif, system-ui, sans-serif';
        context.textAlign = 'center';
        context.textBaseline = 'middle';
        context.fillText(state.showFingers ? String(fingerFor(fret)) : String(fret), x, y);
      }
    });
  });
  // A muted string is only worth marking on a real shape: on a one or two note event the
  // other four strings are simply not being played and the crosses would be noise.
  const shape = state.activeEvent >= 0
    && eventPositions(state.tabEvents[state.activeEvent]).length >= 3;
  if (shape) {
    const nutX = wireX(geometry, 0);
    const offset = state.handed === 'left' ? 14 : -14;
    context.strokeStyle = NECK_PALETTE.rail;
    context.lineWidth = 1.5;
    for (let api = 0; api < geometry.count; api += 1) {
      if (held.includes(api)) continue;
      const y = stringY(geometry, api);
      const x = nutX + offset;
      context.beginPath();
      context.moveTo(x - 4, y - 4);
      context.lineTo(x + 4, y + 4);
      context.moveTo(x + 4, y - 4);
      context.lineTo(x - 4, y + 4);
      context.stroke();
    }
  }
  if (state.nextEvent >= 0) {
    const event = state.tabEvents[state.nextEvent];
    const dt = Math.max(0, number(event.start) - time);
    const readAhead = neckReadAhead();
    const closed = clamp(1 - dt / Math.max(0.001, readAhead), 0, 1);
    eventPositions(event).forEach((entry) => {
      const api = Math.floor(number(entry.string, -1));
      if (api < 0 || api >= geometry.count) return;
      const fret = Math.max(0, Math.floor(number(entry.fret, 0)));
      const x = dotX(geometry, fret);
      const y = stringY(geometry, api);
      context.strokeStyle = NECK_PALETTE.ghost;
      context.globalAlpha = 0.55;
      context.lineWidth = 1.5;
      context.beginPath();
      context.arc(x, y, radius, 0, Math.PI * 2);
      context.stroke();
      // The countdown arc closes as the shape arrives: the fingers know where to go
      // before the note does.
      context.globalAlpha = 1;
      context.lineWidth = 2.5;
      context.beginPath();
      context.arc(x, y, radius, -Math.PI / 2, -Math.PI / 2 + closed * Math.PI * 2);
      context.stroke();
    });
  }
  if (!motionAllowed()) return;
  state.impacts.forEach((impact) => {
    const age = time - impact.at;
    if (age < 0 || age > IMPACT_RING) return;
    const ratio = age / IMPACT_RING;
    context.strokeStyle = NECK_PALETTE.sounding;
    context.globalAlpha = 1 - ratio;
    context.lineWidth = 2;
    context.beginPath();
    context.arc(
      dotX(geometry, impact.fret),
      stringY(geometry, impact.string),
      radius + ratio * radius * 1.6,
      0,
      Math.PI * 2,
    );
    context.stroke();
    context.globalAlpha = 1;
  });
}

/*
 * One frame of the neck: a blit of the cached board, the hand-position wash, six strings,
 * the approach corridor, the sounding dots, the next shape's rings and any decaying
 * impacts. The board itself -- wires, inlays, numbers, rails -- is drawn once into an
 * offscreen bitmap and rebuilt only when the size, the pixel ratio, the fret window, the
 * string count, the orientation or the handedness changes.
 *
 * Measured, not estimated. Four runs of 1800 frames each, spanning the whole 215 s of the
 * 937-event reference transcription, at a 670x300 CSS-pixel canvas in headless Chromium at
 * devicePixelRatio 1: this function together with updateNeckModel and paintMinimap cost a
 * median of 0.5-0.7 ms and a 95th percentile of 0.9-1.1 ms per frame, issuing a mean of
 * 267 and a peak of 446 context calls (those two counts are deterministic). The lane's own
 * per-frame work measured 0.6-0.9 ms median over the same song.
 *
 * The worst single frame ranged from 8 ms to 28 ms across those four runs on a machine
 * that was busy with other work, so no worst case is claimed here: the medians say this
 * fits inside a 16.7 ms frame with room to spare on that machine, and nothing measured
 * here is a promise about a slower one.
 */
function paintNeck() {
  const context = neckContext();
  if (!context) return;
  const box = sizeCanvas(elements.neckCanvas, context);
  const geometry = neckGeometry(box.width, box.height);
  const key = `${box.width}x${box.height}x${box.ratio}x${geometry.highest}`
    + `x${geometry.count}x${state.orientation}x${state.handed}`;
  if (key !== state.neckStaticKey) {
    state.neckStatic = buildNeckStatic(geometry, box.ratio);
    state.neckStaticKey = key;
  }
  const time = state.position;
  context.clearRect(0, 0, box.width, box.height);
  if (state.neckStatic) context.drawImage(state.neckStatic, 0, 0, box.width, box.height);
  paintHandBox(context, geometry);
  paintStrings(context, geometry, time);
  paintCorridor(context, geometry, time, neckReadAhead());
  paintSoundingNotes(context, geometry, time);
}

function neckAnimating() {
  if (!motionAllowed()) return false;
  if (state.impacts.length) return true;
  return state.playing && state.neckVisible;
}

function paintSurfaces() {
  state.neckDirty = false;
  state.neckPaints += 1;
  // A card that was hidden, or a canvas that had no box yet, has no context to draw into;
  // when that changes the placeholder over it is answering a different question and has to
  // be rewritten. Cheap, and only on the frame the answer actually changes.
  if ((neckContext() !== null) !== state.neckDrawable) updateTabPlaceholders();
  paintNeck();
  paintMinimap();
}

function animationFrame() {
  updateMasterTimeline();
  // One loop, not two: the transport already owns a frame callback, and the neck rides it
  // rather than racing a second one. Paused with no impacts and no state change, this
  // paints nothing at all.
  if (state.neckDirty || neckAnimating()) paintSurfaces();
  state.frame = window.requestAnimationFrame(animationFrame);
}

/* --------------------------------------------------------------------------- exports */

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
  state.lyricsTiming = null;
  state.tab = null;
  state.tabEvents = [];
  state.tabNotes = [];
  state.tabWorld = null;
  state.tabLoop = null;
  state.activeCue = -1;
  state.activeTabEvent = -1;
  state.impacts = [];
  state.sounding = [];
  state.approach = [];
  state.nextEvent = -1;
  state.handBox = null;
  state.readoutKey = '';
  state.minimapKey = '';
  state.loopMessage = '';
  state.duration = Math.max(0, number(project.duration, 0));
  state.tracks = Array.isArray(data.tracks)
    ? data.tracks.filter((track) => track && text(track.id)).map((track) => ({
      id: text(track.id),
      label: text(track.label, 'Audio stem'),
      kind: text(track.kind, 'audio'),
      defaultMuted: Boolean(track.defaultMuted),
      muted: Boolean(track.defaultMuted),
      soloed: false,
      volume: 1,
      url: track.url,
      error: '',
      control: null,
      muteButton: null,
      soloButton: null,
      volumeInput: null,
      levelText: null,
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
  renderLoopReadout();
  updateDisplayControls();
  updateLayerControls();
}

/* --------------------------------------------------------------------------- toggles */

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

function toggleGuitar() {
  // Audio and only audio: the tab lane is a display layer and is not consulted here.
  // The pair completes the symmetry the native help text already promises -- lyrics and
  // vocals are separate, and so are the tab lane and the guitar stem.
  const next = !state.guitarEnabled;
  state.tracks.filter(isGuitarTrack).forEach((track) => {
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

function toggleTabLane() {
  // A display change and only a display change: no track is touched.
  state.tabVisible = !state.tabVisible;
  updateLayerControls();
  rememberPreferences();
}

function toggleNeck() {
  state.neckVisible = !state.neckVisible;
  updateLayerControls();
  rememberPreferences();
}

function updateDisplayControls() {
  setButtonState(elements.handed, state.handed === 'left', 'Left-handed', 'Right-handed');
  setButtonState(elements.fingers, state.showFingers, 'Suggested fingers', 'Fret numbers');
  setButtonState(elements.ladder, state.ladder, 'Speed ladder', 'Speed ladder off');
  elements.stringOrder.value = state.orientation;
  elements.zoom.value = String(state.lanePps);
  elements.motion.value = state.motionPref;
  elements.preRoll.value = String(state.preRoll);
}

function setOrientation(value) {
  // One preference, both views. A build that flipped the fretboard without the lane would
  // put two pictures of one instrument on one screen disagreeing about which string is
  // which, which is the exact defect docs/FRETBOARD.md exists to prevent.
  state.orientation = ORIENTATIONS.includes(value) ? value : 'high-e-top';
  applyDisplayClasses();
  // The select is usually the thing that raised this, but not always -- a restored
  // preference and a rejected value both land here too, and a control showing one order
  // over a lane drawn in the other is the same class of lie this preference exists to end.
  updateDisplayControls();
  requestNeckPaint();
  rememberPreferences();
}

function setHanded(value) {
  state.handed = value === 'left' ? 'left' : 'right';
  applyDisplayClasses();
  updateDisplayControls();
  requestNeckPaint();
  rememberPreferences();
}

function setLaneSpeed(value) {
  state.lanePps = Object.values(LANE_SPEEDS).includes(Number(value))
    ? Number(value)
    : LANE_SPEEDS.normal;
  // Every note's left and width is in pixels, so the lane is rebuilt rather than scaled.
  renderTab();
  updateTab(state.position);
  rememberPreferences();
}

function setMotionPreference(value) {
  state.motionPref = value === 'off' ? 'off' : 'auto';
  applyDisplayClasses();
  state.impacts = [];
  state.readoutKey = '';
  updateNeckModel(state.position);
  requestNeckPaint();
  rememberPreferences();
}

function toggleFingers() {
  state.showFingers = !state.showFingers;
  updateDisplayControls();
  requestNeckPaint();
  rememberPreferences();
}

function openShortcuts(open) {
  state.overlayOpen = open;
  elements.overlay.hidden = !open;
  const target = open ? elements.overlayClose : elements.play;
  if (target && typeof target.focus === 'function') target.focus();
}

/* --------------------------------------------------------------------------- loading */

async function loadOptionalDocuments(data) {
  const results = await Promise.allSettled([
    data.lyricsUrl ? readJson(data.lyricsUrl, 'Timed lyrics') : Promise.resolve(null),
    data.tabUrl ? readJson(data.tabUrl, 'Tablature') : Promise.resolve(null),
  ]);
  const lyricsResult = results[0];
  const tabResult = results[1];
  if (lyricsResult.status === 'fulfilled' && lyricsResult.value) {
    state.lyricsTiming = readTiming(lyricsResult.value);
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

/* ------------------------------------------------------------------------- shortcuts */

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
    return;
  }
  const lower = typeof event.key === 'string' ? event.key.toLowerCase() : '';
  const step = event.shiftKey ? 30 : 5;
  if (event.key === 'ArrowLeft') {
    event.preventDefault();
    seekBy(-step);
  } else if (event.key === 'ArrowRight') {
    event.preventDefault();
    seekBy(step);
  } else if (event.key === '-' || event.key === '_') {
    event.preventDefault();
    adjustSelectedGain(-GAIN_STEP);
  } else if (event.key === '+' || event.key === '=') {
    event.preventDefault();
    adjustSelectedGain(GAIN_STEP);
  } else if (event.key === '[') {
    event.preventDefault();
    setLoopStart();
  } else if (event.key === ']') {
    event.preventDefault();
    setLoopEnd();
  } else if (event.key === 'Backspace') {
    // Prevented, or the browser walks back through its history instead.
    event.preventDefault();
    clearLoop();
  } else if (event.key >= '1' && event.key <= '6' && event.key.length === 1) {
    event.preventDefault();
    selectTrack(Number(event.key) - 1);
  } else if (event.key === ',' || event.key === '<') {
    event.preventDefault();
    setRate(state.rate - RATE_STEP);
    state.ladderTarget = state.rate;
    rememberPreferences();
  } else if (event.key === '.' || event.key === '>') {
    event.preventDefault();
    setRate(state.rate + RATE_STEP);
    state.ladderTarget = state.rate;
    rememberPreferences();
  } else if (event.key === '?' || (event.key === '/' && event.shiftKey)) {
    event.preventDefault();
    openShortcuts(!state.overlayOpen);
  } else if (event.key === 'Escape') {
    if (!state.overlayOpen) return;
    event.preventDefault();
    openShortcuts(false);
  } else if (lower === 'm') {
    event.preventDefault();
    const track = state.tracks[state.selectedTrack];
    if (track) updateTrackMute(track, !track.muted);
  } else if (lower === 's') {
    event.preventDefault();
    const track = state.tracks[state.selectedTrack];
    if (track) updateTrackSolo(track, !track.soloed);
  } else if (lower === 'v') {
    event.preventDefault();
    toggleVocals();
  } else if (lower === 'l') {
    event.preventDefault();
    toggleLyrics();
  } else if (lower === 'g') {
    event.preventDefault();
    toggleGuitar();
  } else if (lower === 't') {
    event.preventDefault();
    toggleTabLane();
  } else if (lower === 'f') {
    event.preventDefault();
    toggleNeck();
  }
}

/* ----------------------------------------------------------------------------- wiring */

elements.play.addEventListener('click', togglePlayback);
elements.backFive.addEventListener('click', () => seekBy(-5));
elements.forwardFive.addEventListener('click', () => seekBy(5));
elements.resetPosition.addEventListener('click', () => setPosition(0));
elements.timeline.addEventListener('input', () => setPosition(number(elements.timeline.value)));
elements.rate.addEventListener('input', () => {
  setRate(number(elements.rate.value, 1));
  state.ladderTarget = state.rate;
  rememberPreferences();
});
elements.vocals.addEventListener('click', toggleVocals);
elements.lyricsToggle.addEventListener('click', toggleLyrics);
elements.guitarToggle.addEventListener('click', toggleGuitar);
elements.tabToggle.addEventListener('click', toggleTabLane);
elements.neckToggle.addEventListener('click', toggleNeck);
elements.stringOrder.addEventListener('change', () => setOrientation(elements.stringOrder.value));
elements.handed.addEventListener('click', () => setHanded(state.handed === 'left' ? 'right' : 'left'));
elements.zoom.addEventListener('change', () => setLaneSpeed(elements.zoom.value));
elements.motion.addEventListener('change', () => setMotionPreference(elements.motion.value));
elements.fingers.addEventListener('click', toggleFingers);
elements.ladder.addEventListener('click', toggleLadder);
elements.loopIn.addEventListener('click', setLoopStart);
elements.loopOut.addEventListener('click', setLoopEnd);
elements.loopClear.addEventListener('click', clearLoop);
elements.preRoll.addEventListener('change', () => {
  state.preRoll = clamp(number(elements.preRoll.value, 0), 0, 2);
  rememberPreferences();
});
elements.tabGrid.addEventListener('click', seekFromLane);
elements.overlayClose.addEventListener('click', () => openShortcuts(false));
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

if (motionQuery && typeof motionQuery.addEventListener === 'function') {
  motionQuery.addEventListener('change', () => {
    applyDisplayClasses();
    state.impacts = [];
    state.readoutKey = '';
    requestNeckPaint();
  });
}

if (typeof ResizeObserver === 'function' && elements.neckCanvas) {
  const observer = new ResizeObserver(() => requestNeckPaint());
  observer.observe(elements.neckCanvas);
} else {
  // No ResizeObserver: a debounced resize listener is the fallback, and a repaint is
  // cheap enough that missing the last few pixels of a drag costs nothing.
  let pending = 0;
  window.addEventListener('resize', () => {
    if (pending) clearTimeout(pending);
    pending = setTimeout(() => {
      pending = 0;
      requestNeckPaint();
    }, 150);
  });
}

applyDisplayClasses();
void loadProject();
state.frame = window.requestAnimationFrame(animationFrame);
