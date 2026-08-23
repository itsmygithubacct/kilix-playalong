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
    this.style = {};
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

const fetch = async (url) => {
  const path = String(url).replace('http://127.0.0.1:8080/tok/', '');
  if (path === 'api/project') return __json(__payload);
  if (path === 'api/lyrics') return __json({ schema: 'kilix.playalong.lyrics/v1', cues: [] });
  if (path === 'api/tab') return __json({ tuning: { midi: [40], labels: ['E'] }, events: [] });
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
