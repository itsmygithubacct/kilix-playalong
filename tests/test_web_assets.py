from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

WEB_ROOT = Path(__file__).parents[1] / "src" / "kilix_playalong" / "web"

ASSET_NAMES = ("index.html", "app.js", "styles.css")

# Injection sinks and scheme-relative references, matched as bare substrings.
FORBIDDEN_CONSTRUCTS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "DOMParser",
    "srcdoc",
    "javascript:",
    'src="//',
    "src='//",
    'href="//',
    "href='//",
    'fetch("//',
    "fetch('//",
    "@import",
    "url(//",
)

# Absolute external origins are scanned separately from the list above. A bare "https://"
# substring also matches an XML namespace declaration and a spec URL inside a comment,
# neither of which the browser ever fetches; failing the security test on those would
# report "index.html uses https://" for text that is inert.
#
# These patterns strip comments by regex, not by tokenising JS and CSS, so they are
# defeatable by source that is trying: a "/*" inside a string literal, or a template
# literal whose line begins with "//", blanks the text that follows it. That is why the
# shipped bytes are ALSO scanned raw against INERT_ORIGIN_ALLOWLIST below, where stripping
# plays no part; this pair is defence in depth, and the control that holds regardless is
# the server's default-src 'self' CSP.
_COMMENTS = (
    re.compile(r"/\*.*?\*/", re.S),  # CSS and JS block comments
    re.compile(r"(?m)^[ \t]*//.*$"),  # JS line comments
    re.compile(r"<!--.*?-->", re.S),  # HTML comments
)
_INERT = (re.compile(r"""xmlns(?::[\w-]+)?\s*=\s*(["'])[^"']*\1"""),)
_EXTERNAL_ORIGIN = re.compile(r"(?:https?|wss?)://[^\s\"'`)<>]*")

# Origins allowed to appear anywhere in the shipped bytes, comment or not. It is empty:
# no asset mentions an off-origin URL at all today. A spec link added to a comment
# tomorrow has to be added here as well, which keeps that decision a reviewed one rather
# than something the comment stripper decides on its own.
INERT_ORIGIN_ALLOWLIST: frozenset[str] = frozenset()

VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track"}
)


class _DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.external_assets: list[str] = []
        self.ids: set[str] = set()
        self.hidden_ids: set[str] = set()
        self.classes: dict[str, str] = {}
        self.parents: dict[str, str] = {}
        self._open: list[tuple[str, str | None]] = []

    def _record(self, tag: str, attrs: list[tuple[str, str | None]]) -> str | None:
        values = dict(attrs)
        candidate = values.get("src") or values.get("href")
        if (
            tag in {"script", "link", "img", "audio"}
            and candidate
            and candidate.startswith(("http://", "https://", "//"))
        ):
            self.external_assets.append(candidate)
        identifier = values.get("id")
        if identifier:
            self.ids.add(identifier)
            if "hidden" in values:
                self.hidden_ids.add(identifier)
            self.classes[identifier] = values.get("class") or ""
            for _tag, ancestor in reversed(self._open):
                if ancestor:
                    self.parents[identifier] = ancestor
                    break
        return identifier

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        identifier = self._record(tag, attrs)
        if tag not in VOID_TAGS:
            self._open.append((tag, identifier))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._record(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._open) - 1, -1, -1):
            if self._open[index][0] == tag:
                del self._open[index:]
                return


def _asset(name: str) -> str:
    return (WEB_ROOT / name).read_text()


def _document() -> _DocumentParser:
    parser = _DocumentParser()
    parser.feed(_asset("index.html"))
    return parser


def _function(source: str, header: str) -> str:
    start = source.index(header)
    depth = 0
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"unbalanced braces after {header}")


def _calls(source: str, name: str) -> list[str]:
    """The argument text of every `name(...)` call, skipping the definition itself."""
    found: list[str] = []
    for match in re.finditer(rf"(?<![\w.]){re.escape(name)}\(", source):
        if source[: match.start()].rstrip().endswith("function"):
            continue
        opening = match.end() - 1
        depth = 0
        for index in range(opening, len(source)):
            if source[index] == "(":
                depth += 1
            elif source[index] == ")":
                depth -= 1
                if depth == 0:
                    found.append(source[opening + 1 : index])
                    break
        else:
            raise AssertionError(f"unbalanced parentheses after {name}(")
    return found


def _css_rule(source: str, selector: str) -> str:
    match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", source)
    assert match is not None, f"no rule for {selector}"
    return match.group(1)


def _print_block(source: str) -> str:
    return _function(source, "@media print {")


def _squashed(source: str) -> str:
    return re.sub(r"\s+", " ", source)


def _constant(source: str, name: str) -> float:
    match = re.search(rf"^const {name} = ([0-9.]+);", source, re.M)
    assert match is not None, f"app.js no longer defines {name}"
    return float(match.group(1))


def _live_text(source: str) -> str:
    for pattern in _COMMENTS + _INERT:
        source = pattern.sub(" ", source)
    return source


def _external_references(source: str) -> list[str]:
    return _EXTERNAL_ORIGIN.findall(_live_text(source))


# --------------------------------------------------------------------------------------
# Behavioural harness.
#
# The assertions below this point run the shipped app.js under node against a DOM and
# media stub, so they pin what the code does rather than how it is spelled: a rewrite
# that keeps the behaviour keeps the tests, and a rewrite that loses it fails them.
# The stub owns the clock and every readyState, so starvation, recovery and export
# probing are all deterministic.
# --------------------------------------------------------------------------------------

_STUB_JS = """'use strict';
let __clock = 0;
const __nodes = new Map();

class __Stub {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.className = '';
    this.textContent = '';
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this.title = '';
    this.href = '';
    this.tabIndex = 0;
    // Real CSSStyleDeclaration: plain properties plus setProperty, which is how the lane
    // hands the stylesheet the one vertical number JavaScript owns (its row count).
    this.style = {
      setProperty(name, value) { this[name] = String(value); },
      removeProperty(name) { delete this[name]; },
      getPropertyValue(name) { return name in this ? this[name] : ''; },
    };
    this.dataset = {};
    this.children = [];
    this.attributes = {};
    this.handlers = {};
    this.offsetTop = 0;
    this.offsetHeight = 0;
    this.clientHeight = 0;
    this.clientWidth = 0;
    this.scrollHeight = 0;
    this.scrollTop = 0;
    const owned = new Set();
    this.classList = {
      add: (name) => owned.add(name),
      remove: (name) => owned.delete(name),
      contains: (name) => owned.has(name),
      toggle: (name, on) => (on ? owned.add(name) : owned.delete(name)),
    };
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return name in this.attributes ? this.attributes[name] : null; }
  removeAttribute(name) { delete this.attributes[name]; }
  append(...nodes) { nodes.forEach((node) => this.children.push(node)); }
  replaceChildren(...nodes) { this.children = nodes.slice(); }
  remove() {}
  querySelector() { return null; }
  querySelectorAll() { return []; }
  scrollTo() {}
  addEventListener(name, handler) {
    (this.handlers[name] = this.handlers[name] || []).push(handler);
  }
  fire(name) { (this.handlers[name] || []).forEach((handler) => handler({ type: name })); }
}

// A recording 2d context. The shipped code must work with no canvas at all -- that is
// what #neck-canvas is left as below, and what the whole node harness proves -- but a
// scenario that wants to check WHERE the neck draws its fret wires can hand a node one of
// these and read the ops back.
class __Ctx {
  constructor(owner) {
    this.owner = owner;
    this.ops = [];
    this.fillStyle = '';
    this.strokeStyle = '';
    this.lineWidth = 1;
    this.globalAlpha = 1;
    this.font = '';
    this.textAlign = '';
    this.textBaseline = '';
  }
  record(op, args) {
    this.ops.push({ op, args, lineWidth: this.lineWidth, fillStyle: this.fillStyle,
      strokeStyle: this.strokeStyle, alpha: this.globalAlpha });
  }
  setTransform(...args) { this.record('setTransform', args); }
  clearRect(...args) { this.record('clearRect', args); }
  fillRect(...args) { this.record('fillRect', args); }
  strokeRect(...args) { this.record('strokeRect', args); }
  beginPath() { this.record('beginPath', []); }
  moveTo(...args) { this.record('moveTo', args); }
  lineTo(...args) { this.record('lineTo', args); }
  bezierCurveTo(...args) { this.record('bezierCurveTo', args); }
  arc(...args) { this.record('arc', args); }
  stroke() { this.record('stroke', []); }
  fill() { this.record('fill', []); }
  fillText(...args) { this.record('fillText', args); }
  drawImage(...args) { this.record('drawImage', args.slice(1)); }
  createLinearGradient(...args) {
    this.record('gradient', args);
    return { addColorStop: () => {} };
  }
}

class __Canvas extends __Stub {
  constructor() {
    super('canvas');
    this.width = 0;
    this.height = 0;
    this.context = new __Ctx(this);
    __canvases.push(this);
  }
  getContext() { return this.context; }
}

const __canvases = [];
// Turn a plain stub node into a drawable canvas of a stated CSS size.
const __canvasify = (node, width, height) => {
  node.clientWidth = width;
  node.clientHeight = height;
  node.width = 0;
  node.height = 0;
  node.context = new __Ctx(node);
  node.getContext = () => node.context;
  return node;
};

class __Media extends __Stub {
  constructor() {
    super('audio');
    this.currentTime = 0;
    this.readyState = 0;
    this.seeking = false;
    this.paused = true;
    this.volume = 1;
    this.playbackRate = 1;
    this.preservesPitch = true;
    this.duration = 120;
    this.src = '';
  }
  play() { this.paused = false; return Promise.resolve(); }
  pause() { this.paused = true; }
}

const __media = [];
const document = {
  baseURI: 'http://127.0.0.1:8080/tok/',
  querySelector(selector) {
    if (!__nodes.has(selector)) __nodes.set(selector, new __Stub('div'));
    return __nodes.get(selector);
  },
  createElement(tag) {
    if (tag === 'canvas') return new __Canvas();
    if (tag !== 'audio') return new __Stub(tag);
    const node = new __Media();
    __media.push(node);
    return node;
  },
  createTextNode(value) { return { textContent: String(value) }; },
};

const window = {
  matchMedia: () => ({ matches: false }),
  location: { pathname: '/tok/', origin: 'http://127.0.0.1:8080' },
  localStorage: {
    store: {},
    getItem(key) { return key in this.store ? this.store[key] : null; },
    setItem(key, value) { this.store[key] = String(value); },
  },
  requestAnimationFrame: () => 1,
  cancelAnimationFrame: () => {},
  addEventListener: () => {},
  performance: { now: () => __clock },
  opened: [],
  open(...args) { window.opened.push(args); return null; },
};

const __exports = { 'export/print': 200, 'export/tab.txt': 200, 'export/guitar.mid': 200 };
const __payload = {
  schema: 'kilix.playalong.web/v1',
  project: { id: 'song', title: 'Song', artist: 'Artist', duration: 120 },
  tracks: [
    { id: 'guitar', label: 'Guitar', kind: 'guitar', defaultMuted: false,
      url: '/tok/media/guitar' },
    { id: 'drums', label: 'Drums', kind: 'drums', defaultMuted: false, url: '/tok/media/drums' },
  ],
  lyricsUrl: '/tok/api/lyrics',
  tabUrl: '/tok/api/tab',
  printUrl: '/tok/export/print',
  asciiTabUrl: '/tok/export/tab.txt',
  midiUrl: '/tok/export/guitar.mid',
};

const __json = (value, status = 200) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => value,
});

// The lyrics document the app is served. Mutable, so a scenario can put a different
// provenance in it and reload the project; no cues by default, which is the state the
// placeholder assertions below expect.
const __lyrics = { schema: 'kilix.playalong.lyrics/v1', cues: [] };

// The tab document the app is served, mutable for the same reason: one string and no
// events by default, which is the shape every assertion written before the fretboard
// existed expects, and which proves the neck copes with a degenerate tuning.
const __tab = { tuning: { midi: [40], labels: ['E'] }, events: [] };

const fetch = async (url) => {
  const path = String(url).replace('http://127.0.0.1:8080/tok/', '');
  if (path === 'api/project') return __json(__payload);
  if (path === 'api/lyrics') return __json(__lyrics);
  if (path === 'api/tab') return __json(__tab);
  if (path in __exports) return __json({}, __exports[path]);
  return __json({}, 404);
};

const __settle = async (turns = 24) => {
  for (let index = 0; index < turns; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 0));
  }
};
const __advance = (ms) => { __clock += ms; };
const __report = (value) => { process.stdout.write(`\\n__RESULT__${JSON.stringify(value)}\\n`); };
"""

_EPILOGUE_JS = """
const __app = {
  state,
  elements,
  app,
  media: __media,
  settle: __settle,
  advance: __advance,
  report: __report,
  exportStatus: __exports,
  window,
  playAll,
  pauseAll,
  setPosition,
  updateMasterTimeline,
  toggleLyrics,
  canvases: __canvases,
  canvasify: __canvasify,
  // The fretboard contract, docs/FRETBOARD.md. These are the functions the fixture pins.
  fretDistance,
  cellCentre,
  displayNormalized,
  stringWidthRatio,
  stringNumber,
  orientationRow,
  positionPoint,
  chordName,
  handPositionBox,
  INLAY_SINGLE,
  INLAY_DOUBLE,
  NECK_PALETTE,
  STRING_HUES,
  LANE_SPEEDS,
  PLAYHEAD_FRACTION,
  RATE_STEP,
  RATE_MIN,
  RATE_MAX,
  LOOP_MIN_LENGTH,
  renderTab,
  updateTab,
  updateNeckModel,
  updatePlaybackUi,
  paintSurfaces,
  animationFrame,
  neckAnimating,
  neckContext,
  neckReadAhead,
  laneFutureSeconds,
  handleShortcut,
  toggleGuitar,
  toggleTabLane,
  toggleNeck,
  setOrientation,
  setMotionPreference,
  setLoopStart,
  setLoopEnd,
  clearLoop,
  trackIsAudible,
  updateTrackMute,
  updateTrackSolo,
  RESUME_ALIGNMENT: typeof RESUME_ALIGNMENT === 'undefined' ? null : RESUME_ALIGNMENT,
  DRIFT_TOLERANCE: typeof DRIFT_TOLERANCE === 'undefined' ? null : DRIFT_TOLERANCE,
  STARVATION_GRACE_MS:
    typeof STARVATION_GRACE_MS === 'undefined' ? null : STARVATION_GRACE_MS,
};
"""

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is not installed")


def _run_scenario(tmp_path: Path, name: str, scenario: str) -> dict[str, Any]:
    script = tmp_path / f"{name}.js"
    script.write_text(_STUB_JS + _asset("app.js") + _EPILOGUE_JS + scenario)
    result = subprocess.run(
        ["node", str(script)],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"{result.stdout[-2000:]}\n{result.stderr[-2000:]}"
    marker = [line for line in result.stdout.splitlines() if line.startswith("__RESULT__")]
    assert marker, f"scenario produced no result\n{result.stdout[-2000:]}{result.stderr[-2000:]}"
    parsed = json.loads(marker[-1][len("__RESULT__") :])
    assert isinstance(parsed, dict)
    return parsed


def test_web_surface_has_no_external_assets_or_unsafe_html_sinks() -> None:
    document = _document()
    assert document.external_assets == []
    for name in ASSET_NAMES:
        source = _asset(name)
        for unsafe in FORBIDDEN_CONSTRUCTS:
            assert unsafe not in source, f"{name} uses the unsafe construct {unsafe!r}"
        live = _external_references(source)
        assert not live, f"{name} references live external origins: {live}"
    script = _asset("app.js")
    assert "api/project" in script
    assert "localStorage" in script
    assert "requestAnimationFrame" in script


def test_forbidden_construct_scan_catches_known_injection_sinks() -> None:
    samples = (
        "node.innerHTML = value;",
        'node.insertAdjacentHTML("beforeend", value);',
        "document.write(value);",
        "eval(value);",
        "const render = new Function(value);",
        '<iframe srcdoc="<b>x</b>"></iframe>',
        "new DOMParser().parseFromString(value, 'text/html');",
        '<img src="//cdn.example.net/pixel.png">',
        'fetch("//example.net/collect", {method: "POST"});',
        "@import url(//fonts.example.net/face.css);",
        '<a href="javascript:alert(1)">x</a>',
    )
    for sample in samples:
        assert any(unsafe in sample for unsafe in FORBIDDEN_CONSTRUCTS), sample


def test_external_origin_scan_reads_live_references_not_inert_text() -> None:
    live = (
        '<script src="https://cdn.example.net/x.js"></script>',
        "fetch('http://example.net/collect');",
        "const channel = new WebSocket('wss://example.net/relay');",
        "background-image: url(https://example.net/pixel.png);",
        "const endpoint = 'https://example.net/api';",
    )
    for sample in live:
        assert _external_references(sample), f"missed a live reference: {sample}"
    # Inert by construction: the browser fetches none of these, so a security test that
    # rejected them would only teach the next author to delete the explanation.
    inert = (
        "// readyState values: https://html.spec.whatwg.org/#dom-media-readystate",
        "/* Layering follows https://www.w3.org/TR/css-cascade-5/ */",
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 8 8"></svg>',
        "<!-- prior art: https://example.net/notes -->",
    )
    for sample in inert:
        assert not _external_references(sample), f"flagged inert text: {sample}"


def test_no_shipped_asset_mentions_an_external_origin_at_all() -> None:
    # The comment stripper the test above relies on is a regex and can be defeated by
    # source that hides a URL behind a "/*" in a string literal. Nothing hides from this
    # one: it reads the bytes the browser gets, comments included, and the allowlist it
    # compares against is empty.
    for name in ASSET_NAMES:
        raw = set(_EXTERNAL_ORIGIN.findall(_asset(name)))
        unlisted = sorted(raw - INERT_ORIGIN_ALLOWLIST)
        assert not unlisted, f"{name} mentions {unlisted}"


def test_every_notice_is_filed_in_a_slot_the_bar_renders() -> None:
    # showNotice takes the slot as a required argument and has no default, so a call site
    # that forgot it would write a message the bar never reads and never shows. Nothing in
    # the browser would report that, which is why every call site is checked here.
    script = _asset("app.js")
    slots = set(re.findall(r"^const (NOTICE_\w+) = '", script, re.M))
    assert slots == {"NOTICE_TRACK", "NOTICE_TRANSPORT", "NOTICE_LAYER"}
    order = re.search(r"const NOTICE_ORDER = \[([^\]]*)\]", script)
    assert order is not None
    assert set(order.group(1).replace(",", " ").split()) == slots
    shown = _calls(script, "showNotice")
    assert len(shown) >= 4, shown
    for call in shown:
        slot = call.rstrip().rstrip(",").rsplit(",", 1)[-1].strip()
        assert slot in slots, f"showNotice does not name a rendered slot: {call!r}"
    for call in _calls(script, "clearNotice"):
        # The one non-literal is showNotice forwarding its own argument for an empty
        # message; every other call has to name the slot it clears.
        assert call.strip() in slots | {"kind"}, f"clearNotice clears {call!r}"


def test_element_lookups_resolve_in_the_document() -> None:
    document = _document()
    referenced = set(re.findall(r"querySelector\('#([\w-]+)'\)", _asset("app.js")))
    assert referenced
    assert referenced <= document.ids


def test_shortcuts_ignore_modifier_combinations() -> None:
    handler = _function(_asset("app.js"), "function handleShortcut(event) {")
    assert "if (event.ctrlKey || event.metaKey || event.altKey) return;" in handler
    assert handler.index("target.isContentEditable") < handler.index("event.ctrlKey")
    assert handler.index("event.ctrlKey") < handler.index("event.code === 'Space'")
    # Shift+Space is the browser's page-up and stays the browser's; shift is not blanket
    # ignored, because '+' is Shift+'=' on a US layout and must keep nudging the position.
    assert "if (event.code === 'Space' && !event.shiftKey) {" in handler
    assert "event.shiftKey" not in handler.split("event.code === 'Space'")[0]
    assert "if (event.key === '+' || event.key === '=') {" in handler


def test_resume_alignment_is_far_tighter_than_the_tolerated_drift() -> None:
    script = _asset("app.js")
    resume = _constant(script, "RESUME_ALIGNMENT")
    tolerance = _constant(script, "DRIFT_TOLERANCE")
    # Two stems may sit on opposite sides of state.position, so a resume can leave twice
    # RESUME_ALIGNMENT of spread. That spread has to stay an order of magnitude under the
    # drift the corrector tolerates, or resuming re-creates drift the corrector permitted.
    assert 2 * resume <= tolerance / 10, (
        f"a resume may leave {2 * resume:.3f}s of spread against a {tolerance}s tolerance"
    )
    assert "<= RESUME_ALIGNMENT) return;" in script
    assert "> DRIFT_TOLERANCE) {" in script


@needs_node
def test_resume_realigns_stems_the_drift_corrector_left_apart(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "resume_alignment",
        """
(async () => {
  const { state, media, settle, playAll, pauseAll, setPosition } = __app;
  await settle();
  media.forEach((element) => { element.readyState = 4; });
  await playAll();
  setPosition(30);
  pauseAll();
  // 45 ms apart is a state real playback reaches: the corrector deliberately tolerates
  // up to DRIFT_TOLERANCE, so pausing can leave the stems anywhere inside it.
  media[0].currentTime = 30;
  media[1].currentTime = 30.045;
  await playAll();
  await settle(4);
  const times = media.map((element) => element.currentTime);
  __app.report({
    spread_ms: Math.round(1000 * (Math.max(...times) - Math.min(...times))),
    position: state.position,
    playing: state.playing,
  });
})();
""",
    )
    assert result["playing"] is True
    assert result["spread_ms"] <= 10, f"resume left the stems {result['spread_ms']} ms apart"


@needs_node
def test_a_starved_stem_pauses_the_group_even_with_no_media_event(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "starvation",
        """
(async () => {
  const { state, elements, media, settle, playAll, updateMasterTimeline, advance } = __app;
  await settle();
  media.forEach((element) => { element.readyState = 4; });
  await playAll();
  const notice = () => (elements.notice.hidden ? '' : elements.notice.textContent);
  const snap = () => ({ playing: state.playing, notice: notice() });
  const out = {};
  // A seek that settles: readyState dips to HAVE_METADATA and 'waiting' fires even when
  // the target is fully buffered. Nothing may pause, or every seek flickers.
  media[1].seeking = true;
  media[1].readyState = 1;
  media[1].fire('waiting');
  out.on_settling_seek_event = snap();
  advance(200);
  updateMasterTimeline();
  out.inside_grace = snap();
  media[1].seeking = false;
  media[1].readyState = 4;
  media[1].fire('canplay');
  advance(400);
  updateMasterTimeline();
  out.after_settled_seek = snap();
  // A seek into an unbuffered region raises the same event, but stays starved.
  media[1].seeking = true;
  media[1].readyState = 1;
  media[1].fire('waiting');
  advance(200);
  updateMasterTimeline();
  out.hole_inside_grace = snap();
  advance(600);
  updateMasterTimeline();
  out.hole_after_grace = { ...snap(), paused: media.map((element) => element.paused) };
  media[1].seeking = false;
  media[1].readyState = 4;
  media[1].fire('canplay');
  await settle(8);
  out.recovered = { ...snap(), paused: media.map((element) => element.paused) };
  // Measured in Chrome: a stem starved by a seek can sit at HAVE_METADATA without ever
  // raising 'waiting' or 'stalled', so no listener may be the only detector.
  media[1].readyState = 1;
  media[1].seeking = true;
  advance(200);
  updateMasterTimeline();
  out.silent_hole_inside_grace = snap();
  advance(600);
  updateMasterTimeline();
  out.silent_hole_after_grace = snap();
  __app.report(out);
})();
""",
    )
    assert result["on_settling_seek_event"] == {"playing": True, "notice": ""}
    assert result["inside_grace"] == {"playing": True, "notice": ""}
    assert result["after_settled_seek"] == {"playing": True, "notice": ""}
    assert result["hole_inside_grace"] == {"playing": True, "notice": ""}
    assert result["hole_after_grace"]["playing"] is False
    assert "is buffering" in result["hole_after_grace"]["notice"]
    assert result["hole_after_grace"]["paused"] == [True, True]
    assert result["recovered"] == {"playing": True, "notice": "", "paused": [False, False]}
    assert result["silent_hole_inside_grace"] == {"playing": True, "notice": ""}
    assert result["silent_hole_after_grace"]["playing"] is False
    assert "is buffering" in result["silent_hole_after_grace"]["notice"]


@needs_node
def test_recovering_from_a_stall_restores_the_standing_track_failure(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "notice_restore",
        """
(async () => {
  const { state, elements, media, settle, playAll } = __app;
  await settle();
  media.forEach((element) => { element.readyState = 4; });
  await playAll();
  const notice = () => (elements.notice.hidden ? '' : elements.notice.textContent);
  const out = {};
  media[1].fire('error');
  await settle(4);
  out.after_track_failure = { notice: notice(), kind: state.noticeKind };
  media[0].readyState = 1;
  media[0].seeking = false;
  media[0].fire('waiting');
  out.during_stall = { notice: notice(), kind: state.noticeKind, playing: state.playing };
  media[0].readyState = 4;
  media[0].fire('canplay');
  await settle(8);
  out.after_recovery = {
    notice: notice(),
    kind: state.noticeKind,
    playing: state.playing,
    failed: state.audio.filter((record) => record.failed).map((record) => record.id),
  };
  __app.report(out);
})();
""",
    )
    assert result["after_track_failure"]["kind"] == "track"
    assert "Drums could not be loaded" in result["after_track_failure"]["notice"]
    assert result["during_stall"]["kind"] == "transport"
    assert result["during_stall"]["playing"] is False
    # The stem is dead for the rest of the session; a recovered stall must not leave the
    # user with a clean bar over a track that will never make another sound.
    assert result["after_recovery"]["playing"] is True
    assert result["after_recovery"]["failed"] == ["drums"]
    assert result["after_recovery"]["kind"] == "track"
    assert "Drums could not be loaded" in result["after_recovery"]["notice"]


@needs_node
def test_a_buffering_notice_never_outlives_the_wait_that_raised_it(tmp_path: Path) -> None:
    # The mirror of the test above: there the track message stands first and a stall covers
    # it, here the stall stands first and the failure arrives underneath. Both orderings
    # have to end on the truth, and closing either one must not reopen the other — so the
    # leg where the app is genuinely still waiting is asserted here too.
    result = _run_scenario(
        tmp_path,
        "notice_transport_expiry",
        """
(async () => {
  const { state, elements, media, settle, playAll } = __app;
  await settle();
  media.forEach((element) => { element.readyState = 4; });
  await playAll();
  const notice = () => (elements.notice.hidden ? '' : elements.notice.textContent);
  const snap = () => ({ notice: notice(), kind: state.noticeKind, playing: state.playing });
  const out = {};
  // Both stems stall, so the transport slot is taken before anything has failed.
  media.forEach((element) => { element.readyState = 1; element.seeking = false; });
  media[0].fire('waiting');
  media[1].fire('waiting');
  out.during_stall = snap();
  // Drums dies while Guitar is still buffering: the app is still waiting for Guitar, so
  // the buffering message is still true and must keep covering the track message.
  media[1].fire('error');
  await settle(4);
  out.one_dead_still_waiting = {
    ...snap(),
    track: state.notices.track ? state.notices.track.message : '',
  };
  // Guitar recovers. The wait is over, and the bar owes the user the dead stem.
  media[0].readyState = 4;
  media[0].fire('canplay');
  await settle(8);
  out.after_recovery = { ...snap(), paused: media[0].paused };
  __app.report(out);
})();
""",
    )
    assert result["during_stall"]["kind"] == "transport"
    assert "is buffering" in result["during_stall"]["notice"]
    assert result["during_stall"]["playing"] is False
    assert result["one_dead_still_waiting"]["kind"] == "transport"
    assert "is buffering" in result["one_dead_still_waiting"]["notice"]
    assert "Drums could not be loaded" in result["one_dead_still_waiting"]["track"]
    assert result["after_recovery"]["kind"] == "track"
    assert "Drums could not be loaded" in result["after_recovery"]["notice"]
    assert result["after_recovery"]["playing"] is True
    assert result["after_recovery"]["paused"] is False


@needs_node
def test_the_last_stem_dying_ends_the_buffering_message(tmp_path: Path) -> None:
    # pauseAll is where the app stops waiting for a buffer, and the error handler routes
    # there when the last playable stem dies. A buffering message left standing then is
    # terminal: the transport reads Paused, the bar tells the user to wait for a buffer
    # that will never fill, and no later event exists to correct it.
    result = _run_scenario(
        tmp_path,
        "notice_transport_last_stem",
        """
(async () => {
  const { state, elements, media, settle, playAll } = __app;
  await settle();
  media.forEach((element) => { element.readyState = 4; });
  await playAll();
  const notice = () => (elements.notice.hidden ? '' : elements.notice.textContent);
  const snap = () => ({ notice: notice(), kind: state.noticeKind, playing: state.playing });
  const out = {};
  media.forEach((element) => { element.readyState = 1; element.seeking = false; });
  media[0].fire('waiting');
  media[1].fire('waiting');
  out.during_stall = snap();
  media[1].fire('error');
  await settle(4);
  out.one_dead = snap();
  media[0].fire('error');
  await settle(4);
  out.all_dead = {
    ...snap(),
    resuming: state.resumeAfterBuffering,
    transport_slot: Boolean(state.notices.transport),
    paused: media.map((element) => element.paused),
  };
  __app.report(out);
})();
""",
    )
    assert "is buffering" in result["during_stall"]["notice"]
    assert "is buffering" in result["one_dead"]["notice"]
    assert result["all_dead"]["transport_slot"] is False
    assert result["all_dead"]["resuming"] is False
    assert result["all_dead"]["kind"] == "track"
    assert "Guitar and Drums could not be loaded" in result["all_dead"]["notice"]
    assert "No audio remains for this project" in result["all_dead"]["notice"]
    assert "is buffering" not in result["all_dead"]["notice"]
    assert result["all_dead"]["playing"] is False
    assert result["all_dead"]["paused"] == [True, True]


@needs_node
def test_a_stem_with_an_unusable_url_loses_its_controls_too(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "rejected_url_controls",
        """
(async () => {
  const { state, elements, settle } = __app;
  await settle();
  // Off-origin, so endpoint() rejects it: no element is created and no 'error' event ever
  // fires, which is the path that used to leave the controls live. Reached through the
  // retry button so the whole project is rebuilt around the unusable URL.
  __payload.tracks[1].url = 'https://example.net/media/drums';
  elements.retry.fire('click');
  await settle(16);
  const guitar = state.tracks.find((track) => track.id === 'guitar');
  const drums = state.tracks.find((track) => track.id === 'drums');
  __app.report({
    notice: elements.notice.hidden ? '' : elements.notice.textContent,
    drums_error: drums.error,
    drums_mute_disabled: drums.muteButton.disabled,
    drums_volume_disabled: drums.volumeInput.disabled,
    guitar_mute_disabled: guitar.muteButton.disabled,
  });
})();
""",
    )
    # The bar already named it; the controls have to agree, or the user is invited to mute
    # and balance a stem that has no audio element behind it at all.
    assert "Drums could not be loaded" in result["notice"]
    assert result["drums_error"] == "Audio URL is unavailable."
    assert result["drums_mute_disabled"] is True
    assert result["drums_volume_disabled"] is True
    assert result["guitar_mute_disabled"] is False


@needs_node
@pytest.mark.parametrize(
    ("print_status", "ascii_status", "expect_print", "expect_ascii"),
    [(200, 200, True, True), (404, 200, False, True), (200, 404, True, False)],
)
def test_only_exports_the_server_really_has_are_offered(
    tmp_path: Path,
    print_status: int,
    ascii_status: int,
    expect_print: bool,
    expect_ascii: bool,
) -> None:
    # The manifest advertises print, ASCII tab and MIDI unconditionally, and the pipeline
    # skips the export stage whenever lyrics or tab are missing, so a URL is not evidence
    # that a file exists. Every offered control has to be probed.
    result = _run_scenario(
        tmp_path,
        f"exports_{print_status}_{ascii_status}",
        """
(async () => {
  const { state, elements, app, settle, window: browser } = __app;
  __app.exportStatus['export/print'] = PRINT_STATUS;
  __app.exportStatus['export/tab.txt'] = ASCII_STATUS;
  await settle();
  elements.print.fire('click');
  await settle(2);
  __app.report({
    print_hidden: elements.print.hidden,
    printable_hidden: elements.printable.hidden,
    ascii_hidden: elements.ascii.hidden,
    midi_hidden: elements.midi.hidden,
    print_export_ready: app.classList.contains('print-export-ready'),
    opened: browser.opened.map((args) => args[0]),
    print_url: state.printUrl,
  });
})();
""".replace("PRINT_STATUS", str(print_status)).replace("ASCII_STATUS", str(ascii_status)),
    )
    assert result["print_hidden"] is not expect_print
    assert result["printable_hidden"] is not expect_print
    assert result["print_export_ready"] is expect_print
    assert result["ascii_hidden"] is not expect_ascii
    assert result["midi_hidden"] is False
    # Never hand the browser a URL that 404s: no tab, and no print stylesheet that hides
    # the on-screen lane in favour of an export that was never written.
    assert result["opened"] == ([result["print_url"]] if expect_print else [])


@needs_node
def test_lyrics_placeholders_gate_themselves_by_attribute(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "lyrics_placeholders",
        """
(async () => {
  const { elements, settle, toggleLyrics } = __app;
  await settle();
  const pick = () => ({
    hiddenNote: elements.lyricsHiddenNote ? elements.lyricsHiddenNote.hidden : null,
    emptyNote: elements.lyricsEmpty ? elements.lyricsEmpty.hidden : null,
  });
  const out = { lyrics_on: pick() };
  toggleLyrics();
  out.lyrics_off = pick();
  toggleLyrics();
  out.lyrics_on_again = pick();
  __app.report(out);
})();
""",
    )
    # The fixture has no cues, so with the layer on the empty placeholder is the live one.
    assert result["lyrics_on"] == {"hiddenNote": True, "emptyNote": False}
    assert result["lyrics_off"] == {"hiddenNote": False, "emptyNote": True}
    assert result["lyrics_on_again"] == {"hiddenNote": True, "emptyNote": False}


# The cues every provenance scenario is served with. Two lines are enough to make the
# panel non-empty, which is one of the three conditions the note gates itself on.
_TIMED_CUES = [
    {"start": 0.0, "end": 2.0, "text": "the first line"},
    {"start": 2.0, "end": 4.0, "text": "the second line"},
]

# One measured report, reused so the expected wording below can be read against exactly
# these numbers: 0.921 matched, 3 words interpolated, a 0.417 s mean displacement.
_MEASURED = {"matched_fraction": 0.921, "interpolated_words": 3, "mean_displacement": 0.417}


@needs_node
@pytest.mark.parametrize(
    ("case", "document", "expected"),
    [
        (
            "authored",
            {"timing": "authored", "cues": _TIMED_CUES},
            {
                "hidden": False,
                "tone": "timing-good",
                "tag": "Timing from the source",
                "contains": ["came with the words"],
                "absent": ["approximate", "matched", "%"],
            },
        ),
        (
            "measured",
            {"timing": "measured", "alignment": {**_MEASURED, "usable": True}, "cues": _TIMED_CUES},
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                # 92, not 93: a share of matched words is rounded down so the panel never
                # claims more of the text was measured than the aligner reported. 0.42, not
                # 0.41: mean_displacement is an upper bound and rounds the other way.
                "contains": ["92% of the words", "3 words matched nothing", "up to 0.42s"],
                "absent": ["unusable", "93%", "0.41"],
            },
        ),
        (
            "measured_with_nothing_guessed",
            {
                "timing": "measured",
                "alignment": {
                    "matched_fraction": 1.0,
                    "interpolated_words": 0,
                    "mean_displacement": 0.0,
                    "usable": True,
                },
                "cues": _TIMED_CUES,
            },
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                "contains": ["100% of the words"],
                # No word was guessed and the reported bound is zero, so there is no
                # interpolation clause and no "up to 0.00s" for the reader to decode.
                "absent": ["matched nothing", "0.00", "did not say"],
            },
        ),
        (
            "measured_with_one_guessed_word",
            {
                "timing": "measured",
                "alignment": {
                    "matched_fraction": 0.99,
                    "interpolated_words": 1,
                    "mean_displacement": 0.004,
                    "usable": True,
                },
                "cues": _TIMED_CUES,
            },
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                # A bound below a hundredth of a second is still a bound: it rounds up to
                # 0.01s rather than down to nothing.
                "contains": ["1 word matched nothing", "sits between", "up to 0.01s"],
                "absent": ["1 words", "0.00s"],
            },
        ),
        (
            "measured_rated_unusable",
            {
                "timing": "measured",
                "alignment": {**_MEASURED, "usable": False},
                "cues": _TIMED_CUES,
            },
            {
                "hidden": False,
                "tone": "timing-warn",
                "tag": "Alignment rated unusable",
                "contains": ["92% of the words", "unusable", "approximate"],
                "absent": [],
            },
        ),
        (
            "measured_without_a_report",
            {"timing": "measured", "alignment": None, "cues": _TIMED_CUES},
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                "contains": ["did not say how much of that timing was measured"],
                "absent": ["%", "unusable"],
            },
        ),
        (
            "measured_with_an_unreadable_report",
            {"timing": "measured", "alignment": "fair", "cues": _TIMED_CUES},
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                "contains": ["did not say how much of that timing was measured"],
                "absent": ["%", "unusable"],
            },
        ),
        (
            "estimated",
            {"timing": "estimated", "alignment": None, "cues": _TIMED_CUES},
            {
                "hidden": False,
                "tone": "timing-warn",
                "tag": "Approximate timing",
                "contains": ["spread evenly", "seconds away"],
                "absent": ["matched", "%"],
            },
        ),
        (
            "measured_with_hostile_report_values",
            {
                "timing": "measured",
                "alignment": {
                    "matched_fraction": "<img src=x onerror=alert(1)>",
                    "interpolated_words": "12; DROP",
                    "mean_displacement": "NaN",
                    # Only a real boolean is the aligner's verdict; the string is not one.
                    "usable": "false",
                },
                "cues": _TIMED_CUES,
            },
            {
                "hidden": False,
                "tone": "timing-info",
                "tag": "Aligned to the audio",
                # Every clause is a literal in app.js with a number formatted into it, so a
                # field carrying text instead of a number costs the clause and nothing else
                # reaches the panel.
                "contains": ["did not say how much of that timing was measured"],
                "absent": ["<img", "onerror", "DROP", "NaN", "unusable"],
            },
        ),
        # A document from before the field existed has to render exactly as it did then.
        ("no_timing_field", {"cues": _TIMED_CUES}, {"hidden": True}),
        # ...and so does one whose timing is a value this build does not define.
        ("unknown_timing_value", {"timing": "guessed", "cues": _TIMED_CUES}, {"hidden": True}),
        # Nothing is highlighted with no cues, so there is no highlight to explain.
        ("no_cues", {"timing": "estimated", "cues": []}, {"hidden": True}),
    ],
)
def test_the_lyrics_panel_says_where_its_timing_came_from(
    tmp_path: Path,
    case: str,
    document: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    result = _run_scenario(
        tmp_path,
        f"lyrics_timing_{case}",
        """
(async () => {
  const { elements, settle, toggleLyrics } = __app;
  await settle();
  Object.keys(__lyrics).forEach((key) => { delete __lyrics[key]; });
  Object.assign(__lyrics, DOCUMENT);
  elements.retry.fire('click');
  await settle(16);
  const read = () => ({
    hidden: elements.lyricsTiming.hidden,
    tone: elements.lyricsTiming.className,
    tag: elements.lyricsTimingTag.textContent,
    detail: elements.lyricsTimingDetail.textContent,
  });
  const out = { loaded: read(), cues: __app.state.lyrics.length };
  toggleLyrics();
  out.layer_off = read();
  toggleLyrics();
  out.layer_on = read();
  __app.report(out);
})();
""".replace("DOCUMENT", json.dumps({"schema": "kilix.playalong.lyrics/v1", **document})),
    )
    loaded = result["loaded"]
    assert loaded["hidden"] is expected["hidden"]
    if expected["hidden"]:
        # Hidden means carrying nothing: an empty note in the accessibility tree would
        # still be read out, and a stale tone class would colour the next document.
        assert loaded["tag"] == ""
        assert loaded["detail"] == ""
        assert loaded["tone"] == "timing-note"
    else:
        assert loaded["tone"] == f"timing-note {expected['tone']}"
        assert loaded["tag"] == expected["tag"]
        for fragment in expected["contains"]:
            assert fragment in loaded["detail"], loaded["detail"]
        for fragment in expected["absent"]:
            assert fragment not in loaded["detail"], loaded["detail"]
    # The note explains a highlight, so it leaves with the layer that draws one and comes
    # back saying exactly what it said before.
    assert result["layer_off"]["hidden"] is True
    assert result["layer_off"]["detail"] == ""
    assert result["layer_on"] == loaded


def test_the_timing_note_is_attribute_gated_inside_the_lyrics_card() -> None:
    document = _document()
    assert document.parents.get("lyrics-timing") == "lyrics-card"
    assert document.parents.get("lyrics-timing-tag") == "lyrics-timing"
    assert document.parents.get("lyrics-timing-detail") == "lyrics-timing"
    # Like every other conditional block in this document it gates itself with the
    # attribute, so a document with no timing field shows nothing even with no stylesheet.
    assert "lyrics-timing" in document.hidden_ids


def test_the_timing_note_rounds_each_alignment_number_away_from_flattery() -> None:
    copy = _function(_asset("app.js"), "function timingCopy(timing) {")
    # matched_fraction is the share the panel claims was measured: floored, so 0.929 reads
    # 92% and never 93%. mean_displacement is a mean worst case: ceiled, so a reported
    # 0.411 s reads 0.42s and never 0.41s. Both directions understate the alignment.
    #
    # The floor carries an epsilon, matching kpa_ui.c: 0.29 * 100 is 28.999999999999996 in
    # a double and a bare floor turns 29% into 28%, which made the two surfaces describe
    # one document differently. This asserts the PROPERTY rather than the spelling -- the
    # nudge must be far below one percent, so it can only recover what the multiplication
    # lost and can never round a real 28.6 up.
    assert re.search(r"Math\.floor\(matched \* 100(?: \+ 1e-\d+)?\)", copy)
    epsilon = re.search(r"Math\.floor\(matched \* 100 \+ (1e-\d+)\)", copy)
    if epsilon is not None:
        assert float(epsilon.group(1)) < 0.001
    assert "Math.ceil(bound * 100) / 100" in copy
    assert "Math.round(matched" not in copy
    assert "bound.toFixed(" not in copy


def test_a_document_written_before_the_field_still_warns_about_a_guess() -> None:
    """The installed base is the only reason this warning matters at all.

    A project finished before ``timing`` existed resumes entirely cached -- the lyrics
    stage keys on its inputs, not its output, so nothing rewrites the document. Without
    a fallback the one user the warning is for, whose highlight was spread across the
    duration rather than measured, is the only user who never sees it.

    Only the warning is inferred, never the reassurance: a document with no
    ``-estimated`` tail returns null and the panel stays hidden, because "the timing came
    with the source" is a positive claim and the absence of a tail cannot make it. The
    native reader takes the same position, so the two surfaces agree on one file.
    """

    reader = _function(_asset("app.js"), "function readTiming(value) {")
    assert "endsWith('-estimated')" in reader
    assert "'estimated'" in reader


def test_the_timing_note_prints_with_the_panel_it_belongs_to() -> None:
    printing = _squashed(_print_block(_asset("styles.css")))
    assert ".timing-note {" in printing
    # Only the live status pill is screen-only; nothing added here may hide the lyrics
    # panel, the tab lane or the print notes that the print test above pins.
    assert not re.search(r"(?:^|[{};]) *\.timing-note *\{[^}]*display: none", printing)


def test_active_cue_scrolls_only_its_own_viewport() -> None:
    script = _asset("app.js")
    assert "scrollIntoView(" not in script
    centre = _function(script, "function centreCue(item) {")
    assert "const viewport = elements.lyricsViewport;" in centre
    assert "viewport.scrollTo({" in centre
    assert "if (state.playing) centreCue(next);" in script
    # scroll-margin only ever affected scrollIntoView, which no longer runs anywhere.
    assert "scroll-margin" not in _asset("styles.css")


def test_tab_strings_are_numbered_from_the_high_e() -> None:
    script = _asset("app.js")
    assert "return state.tabStringCount - sourceString;" in script
    for stale in ("sourceString + 1", "stringIndex + 1", "number(position.string) + 1"):
        assert stale not in script
    assert "`String ${stringNumber(stringIndex)}, fret ${note.textContent}`" in script
    assert "text(labels[sourceString], `S${stringNumber(sourceString)}`)" in script
    assert "`S${stringNumber(number(position.string))}:${number(position.fret)}`" in script
    render = _function(script, "function renderTab() {")
    assert "const sourceString = rowCount - 1 - displayRow;" in render


def test_string_gutter_is_pinned_outside_the_moving_lane() -> None:
    document = _document()
    assert document.parents.get("tab-gutter") == "tab-viewport"
    script = _asset("app.js")
    render = _function(script, "function renderTab() {")
    assert "elements.tabGutter.append(label);" in render
    assert "elements.tabGutter.replaceChildren();" in render
    assert "line.append(label)" not in script
    assert "position: absolute" in _css_rule(_asset("styles.css"), ".tab-gutter")


def test_print_styles_replace_the_lane_only_when_an_export_exists() -> None:
    script = _asset("app.js")
    assert "window.print(" not in script
    assert "window.open(state.printUrl, '_blank', 'noopener');" in script
    assert "method: 'HEAD'" in _function(script, "async function exportExists(url) {")
    printing = _squashed(_print_block(_asset("styles.css")))
    # Every consequence hangs off one state class being PRESENT, so the lane can never be
    # hidden by a stylesheet that believes in an export the page could not find, and the
    # note that explains an unreplaced lane is the default rather than a second condition
    # keyed on a different element.
    assert ".print-export-ready .tab-viewport { display: none !important; }" in printing
    assert ".print-export-ready .print-note-export { display: block; }" in printing
    assert ".print-export-ready .print-note-partial { display: none; }" in printing
    assert re.search(r"(?:^|[{};]) *\.print-note-partial *\{ display: block; \}", printing)
    # ...and no unconditional rule hides the lane or shows the export note alongside them.
    assert not re.search(r"(?:^|[{};]) *\.tab-viewport *\{", printing)
    assert not re.search(r"(?:^|[{};]) *\.print-note *\{[^}]*display", printing)
    assert not re.search(r"(?:^|[{};]) *\.print-note-export *\{", printing)
    html = _asset("index.html")
    assert 'class="print-note print-note-export"' in html
    assert 'class="print-note print-note-partial"' in html


def test_transport_state_is_set_before_the_play_promises_settle() -> None:
    body = _function(_asset("app.js"), "async function playAll() {")
    assert body.index("state.playing = true;") < body.index("await Promise.all(")
    assert "if (token !== state.playToken || !state.playing) return;" in body
    assert body.index("await Promise.all(") < body.index("if (token !== state.playToken")
    assert "clearNotice(NOTICE_TRANSPORT);" in body


def test_mute_buttons_update_in_place() -> None:
    script = _asset("app.js")
    mute = _function(script, "function updateTrackMute(track, muted) {")
    assert "renderTrackControls" not in mute
    assert "applyMuteButton(track);" in mute
    vocals = _function(script, "function toggleVocals() {")
    assert "renderTrackControls" not in vocals
    assert "applyMuteButton(track);" in vocals
    apply_button = _function(script, "function applyMuteButton(track) {")
    assert "button.setAttribute('aria-pressed', String(track.muted));" in apply_button
    # The audio error handler is the other path that used to rebuild the whole list.
    create = _function(script, "function createAudioTracks() {")
    assert "renderTrackControls" not in create
    assert "applyTrackAvailability(track);" in create


@needs_node
def test_a_failing_stem_leaves_the_track_controls_in_place(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "failing_stem_focus",
        """
(async () => {
  const { state, elements, media, settle } = __app;
  await settle();
  const before = elements.trackList.children.slice();
  const drums = state.tracks.find((track) => track.id === 'drums');
  const button = drums.muteButton;
  media[1].fire('error');
  await settle(4);
  __app.report({
    controls_rebuilt: elements.trackList.children.some(
      (node, index) => node !== before[index]) || elements.trackList.children.length
        !== before.length,
    same_button: drums.muteButton === button,
    mute_disabled: button.disabled,
    volume_disabled: drums.volumeInput.disabled,
    other_track_enabled: state.tracks[0].muteButton.disabled,
  });
})();
""",
    )
    # Rebuilding #track-list moves focus off whatever the user was on, which is exactly
    # what F27 closed for the click path; the error path must not reopen it.
    assert result["controls_rebuilt"] is False
    assert result["same_button"] is True
    assert result["mute_disabled"] is True
    assert result["volume_disabled"] is True
    assert result["other_track_enabled"] is False


def test_preferences_are_written_only_after_a_deliberate_change() -> None:
    script = _asset("app.js")
    save = _function(script, "function savePreferences() {")
    assert "if (!state.preferenceKey || !state.preferencesTouched) return;" in save
    assert "track.muted === Boolean(track.defaultMuted)" in save
    assert "savePreferences" not in _function(script, "function updateLayerControls() {")
    assert "state.preferencesTouched = true;" in _function(
        script, "function rememberPreferences() {"
    )
    for header in (
        "function updateTrackMute(track, muted) {",
        "function toggleVocals() {",
        "function toggleLyrics() {",
    ):
        assert "rememberPreferences();" in _function(script, header)


def test_every_stem_can_raise_the_timeline_duration() -> None:
    script = _asset("app.js")
    create = _function(script, "function createAudioTracks() {")
    assert "state.masterIndex ===" not in create
    assert "audio.duration > state.duration" in create
    drift = _function(script, "function correctAudioDrift(masterTime) {")
    assert "master.element.currentTime" not in drift


def test_hidden_lyrics_layer_shows_its_own_placeholder() -> None:
    styles = _asset("styles.css")
    assert ".layer-hidden .content-empty { display: flex; }" not in styles
    assert ".lyrics-card.layer-hidden .content-empty { display: none; }" in styles
    assert ".lyrics-card.layer-hidden .layer-note { display: flex; }" in styles
    assert ".layer-note { display: none; }" in styles
    document = _document()
    assert document.parents.get("lyrics-hidden-note") == "lyrics-viewport"
    assert "Lyrics are hidden" in _asset("index.html")
    # Every conditional block in the document gates itself with the attribute, so the
    # right one shows even when the stylesheet does not load.
    assert {"lyrics-empty", "lyrics-hidden-note"} <= document.hidden_ids


def test_tab_lane_transform_is_not_transitioned() -> None:
    assert "transition" not in _css_rule(_asset("styles.css"), ".tab-world")


@needs_node
def test_browser_script_parses_in_node() -> None:
    result = subprocess.run(
        ["node", "--check", str(WEB_ROOT / "app.js")],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------------------
# The shared fretboard contract.
#
# docs/FRETBOARD.md defines the geometry, the chord names, the string order and the hand
# position; tests/fixtures/fretboard_vectors.json is its machine-checkable form; and the
# native C11 surface implements the same definition from the same fixture. These runners
# feed the fixture's values to the shipped app.js and compare. Neither surface may
# hardcode a value the document defines, so nothing below is written out by hand here --
# every expected number is read from the fixture.
# --------------------------------------------------------------------------------------

FIXTURE = Path(__file__).parent / "fixtures" / "fretboard_vectors.json"
FRETBOARD_DOC = Path(__file__).parents[1] / "docs" / "FRETBOARD.md"
NATIVE_UI = Path(__file__).parents[1] / "src" / "native" / "kpa_ui.c"


def _vectors() -> dict[str, Any]:
    parsed = json.loads(FIXTURE.read_text())
    assert isinstance(parsed, dict)
    return parsed


def _native_define(name: str) -> float:
    match = re.search(rf"^#define {name} +([0-9.]+)f?\b", NATIVE_UI.read_text(), re.M)
    assert match is not None, f"kpa_ui.c no longer defines {name}"
    return float(match.group(1))


def _tab_document(events: list[dict[str, Any]], max_fret: int = 20) -> dict[str, Any]:
    return {
        "schema": "kilix.playalong.tab/v1",
        "tuning": {
            "midi": [40, 45, 50, 55, 59, 64],
            "labels": ["E", "A", "D", "G", "B", "e"],
            "max_fret": max_fret,
        },
        "events": events,
    }


def _event(start: float, end: float, positions: list[tuple[int, int]]) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "positions": [
            {"string": string, "fret": fret, "pitch": [40, 45, 50, 55, 59, 64][string] + fret}
            for string, fret in positions
        ],
    }


_SONG = _tab_document(
    [
        _event(0.5, 0.9, [(1, 3), (3, 0), (4, 1)]),
        _event(1.2, 1.6, [(0, 5)]),
        _event(2.0, 2.4, [(2, 7), (3, 7)]),
        _event(3.0, 3.4, [(5, 12)]),
    ]
)


@needs_node
def test_the_fret_geometry_reproduces_every_shared_vector(tmp_path: Path) -> None:
    vectors = _vectors()
    result = _run_scenario(
        tmp_path,
        "fretboard_geometry",
        """
(async () => {
  const V = VECTORS;
  const { state, settle } = __app;
  await settle();
  const tol = V.geometry.tolerance;
  const near = (left, right, bound) => Math.abs(left - right) <= bound;
  const fails = [];
  V.geometry.fret_positions.forEach((entry) => {
    if (!near(__app.fretDistance(entry[0]), entry[1], tol)) fails.push(`d(${entry[0]})`);
  });
  V.geometry.cell_centres.forEach((entry) => {
    if (!near(__app.cellCentre(entry[0]), entry[1], tol)) fails.push(`cell(${entry[0]})`);
  });
  V.geometry.display_normalized.forEach((band) => {
    band.u.forEach((entry) => {
      const value = __app.displayNormalized(entry[0], band.highest_displayed_fret);
      if (!near(value, entry[1], tol)) fails.push(`u(${entry[0]},${band.highest_displayed_fret})`);
    });
  });
  V.geometry.identities.forEach((identity) => {
    const value = identity.expr === 'd(12)' ? __app.fretDistance(12)
      : identity.expr === 'd(24)' ? __app.fretDistance(24) : 1 / __app.fretDistance(1);
    if (!near(value, identity.expect, identity.tolerance)) fails.push(identity.expr);
  });
  V.inlays.cases.forEach((entry) => {
    const kind = __app.INLAY_DOUBLE.includes(entry.fret) ? 'double'
      : __app.INLAY_SINGLE.includes(entry.fret) ? 'single' : 'none';
    if (kind !== entry.kind) fails.push(`inlay kind ${entry.fret}`);
    const centre = __app.cellCentre(entry.fret);
    if (!near(centre, entry.centre, tol)) fails.push(`inlay at ${entry.fret}`);
  });
  state.tabStringCount = 6;
  V.strings.cases.forEach((entry) => {
    if (!near(__app.stringWidthRatio(entry.api_index), entry.width_ratio, 1e-9)) {
      fails.push(`width ${entry.api_index}`);
    }
    if (__app.stringNumber(entry.api_index) !== entry.player_number) {
      fails.push(`player number ${entry.api_index}`);
    }
  });
  V.orientation.cases.forEach((entry, index) => {
    const row = __app.orientationRow(entry.orientation, entry.api_index, entry.string_count);
    if (row !== entry.row) {
      fails.push(`row ${index}`);
    }
    const point = __app.positionPoint(entry.orientation, entry.api_index, entry.fret,
      entry.string_count, entry.highest_displayed_fret);
    if (!near(point.x, entry.point.x, tol) || !near(point.y, entry.point.y, tol)) {
      fails.push(`point ${index}`);
    }
  });
  __app.report({ fails, checked: V.geometry.fret_positions.length + V.orientation.cases.length });
})();
""".replace("VECTORS", json.dumps(_vectors())),
    )
    assert result["fails"] == []
    assert result["checked"] == len(vectors["geometry"]["fret_positions"]) + len(
        vectors["orientation"]["cases"]
    )


@needs_node
def test_every_shared_chord_vector_names_or_refuses_the_same_way(tmp_path: Path) -> None:
    # A `null` expectation is as binding as a name: 18 of the 75 vectors are refusals, and
    # inventing a name for a two-note fragment is the defect the algorithm exists to stop.
    vectors = _vectors()
    result = _run_scenario(
        tmp_path,
        "fretboard_chords",
        """
(async () => {
  const V = VECTORS;
  await __app.settle();
  const fails = [];
  let refused = 0;
  V.chords.cases.forEach((entry) => {
    const want = entry.expect === undefined ? null : entry.expect;
    if (want === null) refused += 1;
    const got = __app.chordName(entry.pitches);
    if (got !== want) {
      const wanted = JSON.stringify(want);
      fails.push(`${JSON.stringify(entry.pitches)} -> ${JSON.stringify(got)} want ${wanted}`);
    }
  });
  // Input order must not matter: the bass is the lowest pitch, not the first element.
  const shuffled = __app.chordName([67, 60, 64]);
  __app.report({ fails, refused, cases: V.chords.cases.length, shuffled });
})();
""".replace("VECTORS", json.dumps(_vectors())),
    )
    assert result["fails"] == []
    assert result["cases"] == len(vectors["chords"]["cases"])
    assert result["refused"] == sum(
        1 for case in vectors["chords"]["cases"] if case.get("expect") is None
    )
    assert result["shuffled"] == "C"


@needs_node
def test_the_hand_position_box_reproduces_every_shared_vector(tmp_path: Path) -> None:
    vectors = _vectors()
    result = _run_scenario(
        tmp_path,
        "fretboard_hand",
        """
(async () => {
  const V = VECTORS;
  await __app.settle();
  const fails = [];
  V.hand_position.cases.forEach((entry, index) => {
    const got = __app.handPositionBox(entry.frets, V.hand_position.max_fret);
    const want = entry.expect === undefined ? null : entry.expect;
    const same = want === null
      ? got === null
      : Boolean(got) && got[0] === want[0] && got[1] === want[1];
    if (!same) fails.push(`${index}: ${JSON.stringify(got)} want ${JSON.stringify(want)}`);
  });
  // Whatever it is handed, the box is the stated width and stays on the neck: a naive
  // min-to-max span over the same windows has a median of 7 frets and a maximum of 19.
  const widths = new Set();
  let offNeck = 0;
  for (let seed = 1; seed <= 400; seed += 1) {
    const frets = [];
    for (let step = 0; step < 1 + (seed % 7); step += 1) {
      frets.push((seed * (step + 3)) % 21);
    }
    const box = __app.handPositionBox(frets, V.hand_position.max_fret);
    if (!box) continue;
    widths.add(box[1] - box[0] + 1);
    if (box[0] < 1 || box[1] > V.hand_position.max_fret) offNeck += 1;
  }
  __app.report({ fails, cases: V.hand_position.cases.length, widths: [...widths], offNeck });
})();
""".replace("VECTORS", json.dumps(_vectors())),
    )
    assert result["fails"] == []
    assert result["cases"] == len(vectors["hand_position"]["cases"])
    assert result["widths"] == [vectors["hand_position"]["box_frets"]]
    assert result["offNeck"] == 0


@needs_node
def test_the_drawn_fret_wires_sit_where_the_contract_puts_them(tmp_path: Path) -> None:
    # Not the pure function this time but the picture: the x the neck actually strokes each
    # wire at. Compared as ratios, so the assertion knows nothing about the card's padding
    # and cannot be satisfied by a linear ramp that happens to share two endpoints.
    result = _run_scenario(
        tmp_path,
        "fretboard_wires",
        """
(async () => {
  const { state, elements, settle, canvasify, paintSurfaces } = __app;
  await settle();
  Object.assign(__tab, SONG);
  elements.retry.fire('click');
  await settle(16);
  canvasify(elements.neckCanvas, 900, 300);
  elements.tabViewport.clientWidth = 900;
  paintSurfaces();
  const board = __app.canvases.filter((item) => item.width > 0)[0];
  const ops = board.context.ops;
  const stop = ops.findIndex((entry) => entry.op === 'arc');
  const wires = [];
  for (let index = 0; index < stop - 1; index += 1) {
    const from = ops[index];
    const to = ops[index + 1];
    if (from.op !== 'moveTo' || to.op !== 'lineTo') continue;
    if (Math.abs(from.args[0] - to.args[0]) > 1e-9) continue;
    if (Math.abs(from.args[1] - to.args[1]) < 20) continue;
    wires.push(from.args[0]);
  }
  __app.report({ wires, highest: state.neckHighestFret, ops: ops.length });
})();
""".replace("SONG", json.dumps(_SONG)),
    )
    wires = result["wires"]
    highest = result["highest"]
    assert len(wires) == highest, wires
    span = wires[-1] - wires[0]

    def distance(fret: int) -> float:
        return 1 - 2 ** (-fret / 12)

    expected_span = distance(highest) - distance(1)
    for index, x in enumerate(wires):
        fret = index + 1
        ratio = (x - wires[0]) / span
        assert abs(ratio - (distance(fret) - distance(1)) / expected_span) < 1e-9, fret
    # And the crowding is real, to the exact ratio the geometry demands: the cells the
    # drawing steps through shrink by 2**(1/12) each time, so any twelve frets apart are
    # exactly 2:1. A linear ramp would put this at 1.0.
    first = wires[1] - wires[0]
    last = wires[-1] - wires[-2]
    assert abs(first / last - 2 ** ((highest - 2) / 12)) < 1e-9, (first, last, highest)
    for index in range(len(wires) - 13):
        near = wires[index + 1] - wires[index]
        far = wires[index + 13] - wires[index + 12]
        assert abs(near / far - 2.0) < 1e-9, index


@needs_node
def test_a_note_onset_renders_exactly_under_the_playhead(tmp_path: Path) -> None:
    # The regression. The lane used to translate by (clientWidth / 2 - 48) while
    # .tab-playhead sat at left: 50%, so every onset was drawn 48 px -- two thirds of a
    # second -- to the left of the line that claimed to mark it, while the is-active class
    # was computed from time and was right. The picture and the highlight now agree.
    result = _run_scenario(
        tmp_path,
        "playhead_alignment",
        """
(async () => {
  const { state, elements, settle } = __app;
  await settle();
  Object.assign(__tab, SONG);
  elements.tabViewport.clientWidth = 800;
  elements.retry.fire('click');
  await settle(16);
  const rendered = (eventIndex, time) => {
    __app.updateTab(time);
    const shift = Number(/translateX\\((-?[0-9.]+)px\\)/.exec(state.tabWorld.style.transform)[1]);
    const left = Number(/(-?[0-9.]+)px/.exec(state.tabNotes[eventIndex][0].style.left)[1]);
    return shift + left;
  };
  const head = elements.tabViewport.clientWidth * __app.PLAYHEAD_FRACTION;
  const starts = state.tabEvents.map((event) => Number(event.start));
  __app.report({
    head,
    at_each_onset: starts.map((start, index) => rendered(index, start)),
    one_second_early: rendered(2, starts[2] - 1),
    pps: state.lanePps,
    active_at_onset: (() => { __app.updateTab(starts[2]); return state.activeTabEvent; })(),
  });
})();
""".replace("SONG", json.dumps(_SONG)),
    )
    for drawn in result["at_each_onset"]:
        assert abs(drawn - result["head"]) < 1e-9, (drawn, result["head"])
    # A second before the onset the note is exactly one second of lane to the right of it.
    assert abs(result["one_second_early"] - result["head"] - result["pps"]) < 1e-9
    assert result["active_at_onset"] == 2


def test_the_lane_speed_and_playhead_match_the_native_surface() -> None:
    script = _asset("app.js")
    # 90 px/s and a playhead a quarter of the way in are the native lane's numbers, so a
    # player who moves between the terminal and the browser reads the same picture.
    assert f"normal: {int(_native_define('KPA_UI_LANE_PPS'))}" in script
    assert "const PLAYHEAD_FRACTION = 0.25;" in script
    assert "left: 25%" in _css_rule(_asset("styles.css"), ".tab-playhead")
    assert (
        "const center = number(elements.tabViewport.clientWidth, 0) * PLAYHEAD_FRACTION;" in script
    )
    # ...and the literal 72 that used to be written at all three lane sites is gone.
    assert "* 72}px" not in script


def test_the_rate_control_matches_the_native_step_and_bounds() -> None:
    script = _asset("app.js")
    assert _constant(script, "RATE_STEP") == _native_define("KPA_UI_RATE_STEP")
    assert _constant(script, "RATE_MIN") == _native_define("KPA_UI_RATE_MIN")
    assert _constant(script, "RATE_MAX") == _native_define("KPA_UI_RATE_MAX")
    assert _constant(script, "GAIN_STEP") == _native_define("KPA_UI_GAIN_STEP")
    html = _squashed(_asset("index.html"))
    assert 'id="rate-range" type="range" min="0.5" max="1.5" step="0.05"' in html
    # preservesPitch stays on: a guitar part transposed by a semitone is worse than one
    # playing at full speed, which is the position the native surface takes as well.
    assert "audio.preservesPitch = true;" in script


@needs_node
def test_the_corridor_never_reaches_past_the_lanes_own_future(tmp_path: Path) -> None:
    # The neck's approach and the lane's right-hand side are two pictures of the same
    # seconds. Reaching further than the lane would put a note on the neck that the lane
    # says has not arrived yet.
    result = _run_scenario(
        tmp_path,
        "corridor_horizon",
        """
(async () => {
  const { elements, settle } = __app;
  await settle();
  const widths = [320, 600, 900, 1600];
  const speeds = Object.values(__app.LANE_SPEEDS);
  const rows = [];
  speeds.forEach((pps) => {
    __app.state.lanePps = pps;
    widths.forEach((width) => {
      elements.tabViewport.clientWidth = width;
      rows.push({ pps, width, lane: __app.laneFutureSeconds(), neck: __app.neckReadAhead() });
    });
  });
  __app.report({ rows });
})();
""",
    )
    for row in result["rows"]:
        assert row["neck"] <= row["lane"] + 1e-9, row
        assert row["neck"] > 0, row
    # On a wide lane the corridor stops at its own limit rather than stretching to seven
    # seconds of approach, which no amount of foreshortening keeps readable.
    wide = next(row for row in result["rows"] if row["width"] == 1600 and row["pps"] == 90)
    assert wide["neck"] == 2.5
    narrow = next(row for row in result["rows"] if row["width"] == 320 and row["pps"] == 144)
    assert narrow["neck"] == narrow["lane"]


@needs_node
def test_one_preference_flips_the_neck_and_the_lane_together(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "orientation_flip",
        """
(async () => {
  const { state, app, settle, canvasify, paintSurfaces } = __app;
  await settle();
  Object.assign(__tab, SONG);
  __app.elements.retry.fire('click');
  await settle(16);
  canvasify(__app.elements.neckCanvas, 900, 300);
  __app.elements.tabViewport.clientWidth = 900;
  const domOrder = () => state.tabWorld.children
    .filter((node) => node.className === 'tab-rows')[0].children
    .map((row) => row.dataset.string);
  const read = () => {
    __app.canvases.length = 0;
    paintSurfaces();
    return {
      orientation: state.orientation,
      lane_class: app.classList.contains('strings-low-e-top'),
      dom_rows: domOrder(),
      neck_row_of_low_e: __app.orientationRow(state.orientation, 0, 6),
      neck_row_of_high_e: __app.orientationRow(state.orientation, 5, 6),
    };
  };
  const out = { tab_order: read() };
  __app.setOrientation('low-e-top');
  out.player_order = read();
  __app.setOrientation('high-e-top');
  out.back_again = read();
  __app.setOrientation('nonsense');
  out.rejected = read();
  __app.report(out);
})();
""".replace("SONG", json.dumps(_SONG)),
    )
    tab_order = result["tab_order"]
    player = result["player_order"]
    assert tab_order["neck_row_of_low_e"] == 5 and tab_order["neck_row_of_high_e"] == 0
    assert player["neck_row_of_low_e"] == 0 and player["neck_row_of_high_e"] == 5
    assert tab_order["lane_class"] is False and player["lane_class"] is True
    # The DOM order never moves: the lane flips in CSS alone, so the two views read one
    # preference from one place and cannot end up disagreeing about which string is which.
    assert tab_order["dom_rows"] == player["dom_rows"] == ["5", "4", "3", "2", "1", "0"]
    assert result["back_again"] == tab_order
    assert result["rejected"] == tab_order
    styles = _asset("styles.css")
    flip = _css_rule(styles, "#app.strings-low-e-top .tab-rows, #app.strings-low-e-top .tab-gutter")
    assert "column-reverse" in flip


@needs_node
def test_the_whole_fretboard_is_inert_without_a_canvas_context(tmp_path: Path) -> None:
    # Every entry point into the canvas is feature-guarded, which is why the fourteen
    # scenarios written before the fretboard existed still run unchanged against a stub
    # that has no getContext, no ResizeObserver, no devicePixelRatio and no
    # getComputedStyle. Nothing may throw, and the lane must still carry the whole job.
    result = _run_scenario(
        tmp_path,
        "neck_without_canvas",
        """
(async () => {
  const { state, elements, settle } = __app;
  await settle();
  Object.assign(__tab, SONG);
  elements.retry.fire('click');
  await settle(16);
  const before = state.neckPaints;
  __app.paintSurfaces();
  __app.updatePlaybackUi();
  __app.setPosition(2.1);
  __app.paintSurfaces();
  __app.report({
    context: __app.neckContext(),
    canvases_created: __app.canvases.length,
    paints_scheduled: state.neckPaints - before,
    threw: false,
    empty_hidden: elements.neckEmpty.hidden,
    empty_text: elements.neckEmpty.textContent,
    chord: elements.chordReadout.textContent,
    lane_events: state.tabEvents.length,
    tab_position: elements.tabPosition.textContent,
  });
})();
""".replace("SONG", json.dumps(_SONG)),
    )
    assert result["context"] is None
    assert result["canvases_created"] == 0
    assert result["paints_scheduled"] == 2
    assert result["empty_hidden"] is False
    assert "no 2d canvas" in result["empty_text"]
    # The lane is unaffected: it read the same document and is still the whole part.
    assert result["lane_events"] == len(_SONG["events"])
    assert result["chord"] != ""
    assert result["tab_position"] != ""


@needs_node
def test_a_still_page_schedules_no_repaint(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "neck_scheduling",
        """
(async () => {
  const { state, media, settle } = __app;
  await settle();
  Object.assign(__tab, SONG);
  __app.elements.retry.fire('click');
  await settle(16);
  __app.setMotionPreference('off');
  __app.animationFrame();
  const paused = state.neckPaints;
  for (let frame = 0; frame < 12; frame += 1) __app.animationFrame();
  const stillPaused = state.neckPaints;
  media.forEach((element) => { element.readyState = 4; });
  await __app.playAll();
  const playing = state.neckPaints;
  for (let frame = 0; frame < 12; frame += 1) {
    media[0].currentTime += 0.016;
    __app.animationFrame();
  }
  __app.report({
    while_paused: stillPaused - paused,
    while_playing: state.neckPaints - playing,
    animating_when_still: __app.neckAnimating(),
    dirty_when_still: state.neckDirty,
  });
})();
""".replace("SONG", json.dumps(_SONG)),
    )
    # Paused, motion off, nothing decaying: twelve frames, not one repaint.
    assert result["while_paused"] == 0
    assert result["dirty_when_still"] is False
    # Playing still repaints -- the position changed, so the hand box, the dots and the
    # next-up strip all have to follow. Motion off costs the tween, not the truth.
    assert result["while_playing"] == 12


@needs_node
def test_the_guitar_and_lane_keys_keep_audio_and_display_apart(tmp_path: Path) -> None:
    # The symmetry the native help text promises: "lyrics and vocals are separate ... the
    # same holds for the tab lane and the guitar stem". Neither member of a pair may touch
    # the other's state, in either direction.
    result = _run_scenario(
        tmp_path,
        "audio_display_split",
        """
(async () => {
  const { state, media, settle } = __app;
  await settle();
  const guitar = () => state.tracks.find((track) => track.id === 'guitar');
  const snap = () => ({
    guitar_muted: guitar().muted,
    guitar_volume: media[0].volume,
    tab_visible: state.tabVisible,
    neck_visible: state.neckVisible,
    lyrics_visible: state.lyricsVisible,
  });
  const out = { start: snap() };
  __app.toggleGuitar();
  out.after_g = snap();
  __app.toggleTabLane();
  out.after_t = snap();
  __app.toggleNeck();
  out.after_f = snap();
  __app.toggleGuitar();
  out.after_g_again = snap();
  __app.report(out);
})();
""",
    )
    assert result["start"] == {
        "guitar_muted": False,
        "guitar_volume": 1,
        "tab_visible": True,
        "neck_visible": True,
        "lyrics_visible": True,
    }
    # g is audio only.
    assert result["after_g"]["guitar_muted"] is True
    assert result["after_g"]["guitar_volume"] == 0
    assert result["after_g"]["tab_visible"] is True
    assert result["after_g"]["neck_visible"] is True
    # t and f are display only.
    assert result["after_t"]["tab_visible"] is False
    assert result["after_t"]["guitar_muted"] is True
    assert result["after_f"]["neck_visible"] is False
    assert result["after_f"]["guitar_muted"] is True
    assert result["after_g_again"]["guitar_muted"] is False
    assert result["after_g_again"]["tab_visible"] is False
    script = _asset("app.js")
    assert "state.tabVisible" not in _function(script, "function toggleGuitar() {")
    assert "muted" not in _function(script, "function toggleTabLane() {")
    assert "muted" not in _function(script, "function toggleNeck() {")


@needs_node
def test_a_solo_silences_every_track_that_is_not_soloed(tmp_path: Path) -> None:
    # kpa_ui.c's rule, verbatim: audible = !muted && (!any_solo || soloed). It is a
    # property of the whole mixer, so one solo press has to be re-applied to every stem --
    # applying it only to the pressed one would leave the others sounding.
    result = _run_scenario(
        tmp_path,
        "solo_rule",
        """
(async () => {
  const { state, media, settle } = __app;
  await settle();
  const volumes = () => media.map((element) => element.volume);
  const out = { start: volumes() };
  const guitar = state.tracks[0];
  const drums = state.tracks[1];
  __app.updateTrackSolo(drums, true);
  out.drums_soloed = volumes();
  __app.updateTrackSolo(guitar, true);
  out.both_soloed = volumes();
  __app.updateTrackMute(guitar, true);
  out.soloed_but_muted = volumes();
  __app.updateTrackSolo(drums, false);
  __app.updateTrackSolo(guitar, false);
  __app.updateTrackMute(guitar, false);
  out.cleared = volumes();
  __app.report(out);
})();
""",
    )
    assert result["start"] == [1, 1]
    assert result["drums_soloed"] == [0, 1]
    assert result["both_soloed"] == [1, 1]
    # Mute still wins over solo, exactly as it does in the terminal.
    assert result["soloed_but_muted"] == [0, 1]
    assert result["cleared"] == [1, 1]
    audible = _function(_asset("app.js"), "function trackIsAudible(track) {")
    assert "!track.muted && (!solo || track.soloed)" in audible


@needs_node
def test_a_loop_wraps_through_set_position_and_refuses_an_unusable_one(tmp_path: Path) -> None:
    result = _run_scenario(
        tmp_path,
        "loop_rules",
        """
(async () => {
  const { state, elements, media, settle } = __app;
  await settle();
  const read = () => ({
    active: state.loop.active,
    start: state.loop.start,
    end: state.loop.end,
    readout: elements.loopReadout.textContent,
    notice: elements.notice.hidden ? '' : elements.notice.textContent,
  });
  const out = {};
  __app.setPosition(30);
  __app.setLoopStart();
  out.fresh_start = read();
  __app.setPosition(30.1);
  __app.setLoopEnd();
  out.too_short = read();
  __app.setPosition(20);
  __app.setLoopEnd();
  out.end_before_start = read();
  __app.setPosition(42);
  __app.setLoopEnd();
  out.usable = read();
  media.forEach((element) => { element.readyState = 4; });
  await __app.playAll();
  media[0].currentTime = 42.5;
  __app.updateMasterTimeline();
  out.after_wrap = { ...read(), position: state.position, seeks: media.map((e) => e.currentTime) };
  state.preRoll = 2;
  media[0].currentTime = 43;
  __app.updateMasterTimeline();
  out.with_preroll = { position: state.position };
  __app.clearLoop();
  out.cleared = read();
  __app.report(out);
})();
""",
    )
    # The native rule: a start with no end downstream of it gets a one second loop.
    assert result["fresh_start"]["active"] is True
    assert result["fresh_start"]["end"] - result["fresh_start"]["start"] == 1.0
    # Too short to seek six media elements around.
    assert result["too_short"]["active"] is False
    assert "0.25 s" in result["too_short"]["readout"]
    # The native surface's exact sentence, so both refuse the same thing in the same words.
    assert result["end_before_start"]["readout"] == "a loop end must come after its start"
    assert result["end_before_start"]["active"] is False
    # ...and never in the notice bar, which is owed to dead stems and stalled transports.
    assert result["too_short"]["notice"] == ""
    assert result["end_before_start"]["notice"] == ""
    assert result["usable"]["active"] is True
    # The wrap goes through setPosition, so it is the seek path the drift corrector and
    # the starvation poll already understand: every stem moved, not just the master.
    assert result["after_wrap"]["position"] == 30
    assert result["after_wrap"]["seeks"] == [30, 30]
    assert result["with_preroll"]["position"] == 28
    assert result["cleared"]["active"] is False
    assert result["cleared"]["readout"] == "Loop off"
    assert "setPosition(Math.max(0, state.loop.start - state.preRoll));" in _function(
        _asset("app.js"), "function wrapLoop() {"
    )


def test_the_canvas_palette_is_also_declared_in_the_stylesheet() -> None:
    # A canvas cannot read a custom property, and the node harness has no getComputedStyle
    # to ask with, so the hex values are written twice on purpose. This is what stops the
    # two copies drifting into two different pictures.
    script = _asset("app.js")
    styles = _asset("styles.css")
    palette = _function(script, "const NECK_PALETTE = {")
    hues = re.search(r"const STRING_HUES = \[([^\]]*)\]", script)
    assert hues is not None
    colours = re.findall(r"#[0-9a-f]{6}", palette) + re.findall(r"#[0-9a-f]{6}", hues.group(1))
    assert len(colours) == 21, colours
    for colour in colours:
        assert colour in styles, f"{colour} is in app.js but not in styles.css"
    # Every one of them is declared as a custom property, so the DOM chrome around the
    # canvas can use the same values without a third copy.
    for colour in colours:
        assert re.search(rf"--[a-z0-9-]+: {colour};", styles), colour


def test_the_lane_owns_its_row_geometry_in_css_variables() -> None:
    styles = _asset("styles.css")
    script = _asset("app.js")
    viewport = _css_rule(styles, ".tab-viewport")
    assert "--tab-row-h: 39px;" in viewport
    assert "--tab-note-h: 25px;" in viewport
    assert "--tab-row-h: 26px" in _css_rule(styles, ".tab-viewport.lane-compact")
    # One variable drives the row, the note inside it and the gutter label beside it, so
    # the pinned gutter cannot drift out of line with the rows it names.
    assert "height: var(--tab-row-h)" in _css_rule(styles, ".tab-row")
    assert "height: var(--tab-row-h)" in _css_rule(styles, ".tab-string-label")
    note = _css_rule(styles, ".tab-note")
    assert "top: calc((var(--tab-row-h) - var(--tab-note-h)) / 2)" in note
    assert "height: var(--tab-note-h)" in note
    # JavaScript owns the time axis and nothing else.
    render = _function(script, "function renderTab() {")
    for vertical in ("style.top", "style.height"):
        assert vertical not in render, vertical


def test_the_shortcut_card_states_the_keys_the_handler_really_binds() -> None:
    html = _asset("index.html")
    handler = _function(_asset("app.js"), "function handleShortcut(event) {")
    # The old card claimed the minus and plus keys nudge the position. They move the
    # selected stem's level now, and a card that still said otherwise would be a lie
    # printed on the page.
    assert "nudge position" not in html
    assert "selected stem level" in html
    for key in ("[", "]", "M", "S", "G", "T", "F", "?"):
        assert f"<kbd>{key}</kbd>" in html, key
    for bound in ("'['", "']'", "'Backspace'", "'m'", "'s'", "'g'", "'t'", "'f'", "'?'"):
        assert bound in handler, bound
    # The two deliberate divergences from the terminal are stated rather than left to be
    # rediscovered: Tab is the browser's and q is not this app's to take.
    overlay = _squashed(html)
    assert "the focused stem is the selected one" in overlay
    assert "a browser tab is not this app's to close" in overlay
    assert "'q'" not in handler


@needs_node
def test_the_neck_still_draws_a_guitar_with_no_tab_at_all(tmp_path: Path) -> None:
    # No tab is not no instrument: the strings, the frets and the inlays are properties of
    # the tuning, and the card says what it is showing rather than going blank.
    result = _run_scenario(
        tmp_path,
        "neck_without_tab",
        """
(async () => {
  const { state, elements, settle, canvasify, paintSurfaces } = __app;
  await settle();
  canvasify(elements.neckCanvas, 900, 300);
  paintSurfaces();
  const board = __app.canvases.filter((item) => item.width > 0)[0];
  __app.report({
    events: state.tabEvents.length,
    strings: state.tabStringCount,
    highest: state.neckHighestFret,
    drew_a_board: Boolean(board) && board.context.ops.length > 20,
    empty_hidden: elements.neckEmpty.hidden,
    empty_text: elements.neckEmpty.textContent,
    chord: elements.chordReadout.textContent,
    next: elements.nextReadout.textContent,
    position: elements.positionReadout.textContent,
  });
})();
""",
    )
    # The stub's tab document is one string and no events, which is also the degenerate
    # tuning every older scenario is served: nothing throws and the default six strings
    # stand in.
    assert result["events"] == 0
    assert result["strings"] == 6
    assert result["highest"] == 12
    assert result["drew_a_board"] is True
    assert result["empty_hidden"] is False
    assert "No tab events loaded" in result["empty_text"]
    assert result["chord"] == "silence"
    assert result["next"] == "no tab loaded"
    assert "No hand position" in result["position"]
