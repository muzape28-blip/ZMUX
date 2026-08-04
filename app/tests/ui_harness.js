/*
 * Behavioral test harness for the ZMUX terminal UI (app/templates/terminal.html).
 *
 * The code under test is the REAL inline script, extracted verbatim from
 * terminal.html at runtime — never a copy. It executes inside a Node `vm`
 * context against a minimal DOM/xterm/WebSocket surface (the browser APIs
 * the script touches) plus a deterministic manual clock, so touch holds,
 * timers, and scroll flows replay exactly.
 *
 * Usage:  node ui_harness.js <path-to-terminal.html>
 * Output: TAP-style lines; exit code 1 on any failure.
 */
'use strict';

const fs = require('fs');
const vm = require('vm');

/* ------------------------------------------------------------------ *
 * Extract the main inline <script> from the template.
 * ------------------------------------------------------------------ */
function extractMainScript(htmlPath) {
  const html = fs.readFileSync(htmlPath, 'utf8');
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  const main = blocks.find(b => b.includes('const AUTH_TOKEN'));
  if (!main) throw new Error('main inline script not found in ' + htmlPath);
  return main
    .replace('{{ ws_port }}', '5999')
    .replace('{{ auth_token }}', 'test-token');
}

/* ------------------------------------------------------------------ *
 * Deterministic clock: every setTimeout/setInterval in the UI runs on
 * this queue; scenarios advance time explicitly.
 * ------------------------------------------------------------------ */
function makeClock() {
  const timers = new Map();
  let nextId = 1;
  const clock = {
    now: 0,
    setTimeout(fn, ms = 0) { const id = nextId++; timers.set(id, { fn, due: this.now + ms, interval: 0 }); return id; },
    clearTimeout(id) { timers.delete(id); },
    setInterval(fn, ms = 0) { const id = nextId++; timers.set(id, { fn, due: this.now + ms, interval: ms }); return id; },
    clearInterval(id) { timers.delete(id); },
    advance(ms) {
      const target = this.now + ms;
      for (;;) {
        let dueId = null, due = null;
        for (const [id, t] of timers) {
          if (t.due <= target && (due === null || t.due < due.due || (t.due === due.due && id < dueId))) { due = t; dueId = id; }
        }
        if (due === null) break;
        this.now = due.due;
        if (due.interval) due.due = this.now + due.interval; else timers.delete(dueId);
        due.fn();
      }
      this.now = target;
    },
  };
  return clock;
}

/* ------------------------------------------------------------------ *
 * Minimal DOM — just the surface the UI script uses.
 * ------------------------------------------------------------------ */
class ClassList {
  constructor(el) { this.el = el; }
  _set() { return new Set(this.el._className.split(/\s+/).filter(Boolean)); }
  _write(s) { this.el._className = [...s].join(' '); }
  add(...cs) { const s = this._set(); cs.forEach(c => s.add(c)); this._write(s); }
  remove(...cs) { const s = this._set(); cs.forEach(c => s.delete(c)); this._write(s); }
  toggle(c, force) {
    const s = this._set();
    const want = force === undefined ? !s.has(c) : !!force;
    if (want) s.add(c); else s.delete(c);
    this._write(s);
    return want;
  }
  contains(c) { return this._set().has(c); }
}

class FakeElement {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.id = '';
    this._className = '';
    this.children = [];
    this.parent = null;
    this.listeners = {};
    this.style = {};
    this.title = '';
    this.type = '';
    this.textContent = '';
    this._innerHTML = '';
    this.classList = new ClassList(this);
    this.clientWidth = 400;      // fitTerminal measures these
    this.clientHeight = 600;
  }
  get className() { return this._className; }
  set className(v) { this._className = String(v || ''); }
  get innerHTML() { return this._innerHTML; }
  set innerHTML(v) { this._innerHTML = String(v); if (v === '') this.children = []; }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  removeChild(child) { this.children = this.children.filter(c => c !== child); child.parent = null; return child; }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    this.listeners[type] = (this.listeners[type] || []).filter(f => f !== fn);
  }
  dispatchEvent(evt) {
    if (!evt.target) evt.target = this;
    if (!evt.preventDefault) evt.preventDefault = () => { evt.defaultPrevented = true; };
    if (!evt.stopPropagation) evt.stopPropagation = () => { evt._stopped = true; };
    for (const fn of [...(this.listeners[evt.type] || [])]) fn.call(this, evt);
    return true;
  }
  click() { this.dispatchEvent({ type: 'click' }); }
  focus() { this.focused = true; }
  scrollIntoView() { this.scrolledIntoView = true; }
  getBoundingClientRect() { return { width: 8, height: 17, top: 0, left: 0, right: 8, bottom: 17 }; }
  _matches(sel) {
    // Supports ".class", ".a.b", "#id" — all the script asks for.
    if (sel[0] === '#') return this.id === sel.slice(1);
    const classes = sel.split('.').filter(Boolean);
    return classes.every(c => this.classList.contains(c));
  }
  querySelector(sel) {
    for (const child of this.children) {
      if (child._matches(sel)) return child;
      const hit = child.querySelector(sel);
      if (hit) return hit;
    }
    return null;
  }
}

function buildDocument() {
  const body = new FakeElement('body');
  const byId = {};
  const mk = (tag, id, parent) => {
    const el = new FakeElement(tag);
    if (id) { el.id = id; byId[id] = el; }
    (parent || body).appendChild(el);
    return el;
  };
  const boot = mk('div', 'boot');
  mk('img', null, boot);
  mk('div', null, boot);
  mk('div', 'boot-status', boot);
  const topbar = mk('div', 'topbar');
  const title = mk('span', null, topbar); title.className = 'title';
  const toggle = mk('button', 'keys-toggle', topbar); toggle.className = 'active';
  const status = mk('span', 'status', topbar);
  mk('span', 'status-dot', status);
  mk('span', 'status-text', status);
  mk('div', 'tabs');
  mk('div', 'terminal-container');
  mk('div', 'toolbar');
  return {
    body,
    documentElement: new FakeElement('html'),
    getElementById: (id) => byId[id] || null,
    createElement: (tag) => new FakeElement(tag),
  };
}

/* ------------------------------------------------------------------ *
 * xterm.js Terminal stub: records writes/resizes; exposes _emitScroll
 * so scenarios can reproduce exact viewport offsets (incl. reflow and
 * clear-collapse jumps that real xterm produces asynchronously).
 * ------------------------------------------------------------------ */
class FakeTerminal {
  constructor(options) {
    this.options = options || {};
    this.buffer = { active: { baseY: 0 } };
    this.writes = [];
    this.resizes = [];
    this.scrollTop = 0;
    this._scrollCb = null;
    this._dataCb = null;
  }
  open(container) {
    const screen = new FakeElement('div'); screen.className = 'xterm-screen';
    this.viewport = new FakeElement('div'); this.viewport.className = 'xterm-viewport';
    const xterm = new FakeElement('div'); xterm.className = 'xterm';
    xterm.appendChild(this.viewport);
    xterm.appendChild(screen);
    container.appendChild(xterm);
  }
  onData(cb) { this._dataCb = cb; }
  onScroll(cb) { this._scrollCb = cb; }
  write(data) { this.writes.push(data); }
  resize(cols, rows) { this.resizes.push([cols, rows]); this.cols = cols; this.rows = rows; }
  focus() { this.focused = true; }
  scrollToBottom() { this._emitScroll(this.buffer.active.baseY); }
  _emitScroll(pos) { this.scrollTop = pos; if (this._scrollCb) this._scrollCb(pos); }
}

function makeWebSocketClass() {
  const instances = [];
  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.sent = [];
      this.readyState = FakeWebSocket.CONNECTING;
      instances.push(this);
    }
    send(data) { this.sent.push(data); }
    close() { this.readyState = FakeWebSocket.CLOSED; if (this.onclose) this.onclose(); }
    open() { this.readyState = FakeWebSocket.OPEN; if (this.onopen) this.onopen(); }
  }
  FakeWebSocket.CONNECTING = 0; FakeWebSocket.OPEN = 1;
  FakeWebSocket.CLOSING = 2; FakeWebSocket.CLOSED = 3;
  FakeWebSocket.instances = instances;
  return FakeWebSocket;
}

function makeLocalStorage(seed) {
  const map = new Map(Object.entries(seed || {}));
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => { map.set(k, String(v)); },
    removeItem: (k) => { map.delete(k); },
    clear: () => map.clear(),
    _map: map,
  };
}

/* ------------------------------------------------------------------ *
 * Load the real UI script into a fresh vm context and return handles.
 * ------------------------------------------------------------------ */
function loadUI(scriptSource, options = {}) {
  const clock = makeClock();
  const documentObj = buildDocument();
  const WebSocketStub = makeWebSocketClass();
  const localStorage = makeLocalStorage(options.localStorage);
  const vibrateCalls = [];
  const terminals = [];
  const TerminalStub = function (opts) {
    const t = new FakeTerminal(opts);
    terminals.push(t);
    return t;
  };
  const windowObj = {
    location: { hostname: '127.0.0.1' },
    addEventListener: (type, fn) => { (windowObj.listeners[type] = windowObj.listeners[type] || []).push(fn); },
    listeners: {},
  };
  const sandbox = {
    console,
    document: documentObj,
    window: windowObj,
    navigator: { vibrate: (pattern) => { vibrateCalls.push(pattern); return true; } },
    localStorage,
    WebSocket: WebSocketStub,
    Terminal: TerminalStub,
    Blob,
    performance: { now: () => clock.now },
    setTimeout: clock.setTimeout.bind(clock),
    clearTimeout: clock.clearTimeout.bind(clock),
    setInterval: clock.setInterval.bind(clock),
    clearInterval: clock.clearInterval.bind(clock),
  };
  vm.createContext(sandbox);
  vm.runInContext(scriptSource, sandbox, { filename: 'terminal.html.inline.js' });
  return {
    clock, document: documentObj, localStorage, vibrateCalls, terminals,
    WebSocketStub, windowObj, sandbox,
    read: (expr) => vm.runInContext(expr, sandbox),
    term: () => terminals[terminals.length - 1],
    ws: () => WebSocketStub.instances[WebSocketStub.instances.length - 1],
    byId: (id) => documentObj.getElementById(id),
  };
}

/* ------------------------------------------------------------------ *
 * Tiny test runner (TAP output).
 * ------------------------------------------------------------------ */
const results = [];
let seq = 0;
function ok(pass, name, detail) {
  seq += 1;
  results.push(pass);
  console.log(`${pass ? 'ok' : 'not ok'} ${seq} - ${name}${pass ? '' : (detail ? ' :: ' + detail : '')}`);
}
const flush = () => new Promise((resolve) => setImmediate(resolve));

/* Synthetic touch helpers — the exact event shapes the handlers read. */
function touchStart(el, x = 10, y = 10) { el.dispatchEvent({ type: 'touchstart', touches: [{ clientX: x, clientY: y }] }); }
function touchMove(el, x, y) { el.dispatchEvent({ type: 'touchmove', touches: [{ clientX: x, clientY: y }] }); }
function touchEnd(el) { el.dispatchEvent({ type: 'touchend', touches: [] }); }
function tap(el) { touchStart(el); touchEnd(el); el.dispatchEvent({ type: 'click' }); }

function sessionsState(ids, active) {
  return JSON.stringify({
    type: 'sessions',
    sessions: ids.map((id) => ({ id, busy: false })),
    active: active || ids[0],
    max: 8,
  });
}
function sentActions(ws) {
  return ws.sent.filter((s) => typeof s === 'string' && s.startsWith('{')).map((s) => JSON.parse(s));
}

async function scenarioTabs(script) {
  const ui = loadUI(script);
  const ws = ui.ws();
  ws.open();
  ui.clock.advance(400);               // flush boot timers (first fitTerminal)

  ws.onmessage({ data: sessionsState(['s1', 's2']) });
  const tabsBar = ui.byId('tabs');
  const tabs = tabsBar.children.filter((c) => c.classList.contains('tab'));
  ok(tabs.length === 2, 'tabs: two sessions render two tabs', `got ${tabs.length}`);
  ok(tabsBar.children.length === 3, 'tabs: "+" button appended after tabs');
  ok(!tabs.some((t) => t.textContent || t.querySelector('.close')),
    'tabs: no dedicated × close affordance remains');

  // Plain tap switches sessions.
  tap(tabs[1]);
  let actions = sentActions(ws);
  ok(actions.some((a) => a.action === 'session.switch' && a.id === 's2'),
    'tabs: plain tap sends session.switch');
  ok(!actions.some((a) => a.action === 'session.close'),
    'tabs: plain tap never closes');

  // Hold ~1.5s: visual feedback first, then auto-close.
  const before = sentActions(ws).length;
  touchStart(tabs[0]);
  ok(tabs[0].classList.contains('holding'), 'tabs: hold start adds visual "holding" feedback');
  ui.clock.advance(1499);
  ok(sentActions(ws).length === before, 'tabs: close does NOT fire before 1.5s');
  ui.clock.advance(2);
  actions = sentActions(ws);
  ok(actions.some((a) => a.action === 'session.close' && a.id === 's1'),
    'tabs: holding 1.5s sends session.close');
  ok(ui.vibrateCalls.includes(40), 'tabs: navigator.vibrate(40) fired on hold-close');
  ok(!tabs[0].classList.contains('holding'), 'tabs: holding state cleared after close fires');

  // Lifting the finger produces a click; it must be swallowed.
  touchEnd(tabs[0]);
  tabs[0].dispatchEvent({ type: 'click' });
  actions = sentActions(ws);
  ok(!actions.some((a) => a.action === 'session.switch' && a.id === 's1'),
    'tabs: click after hold-close is suppressed (no switch-back)');

  // A fresh tap right after the suppression window still works.
  ui.clock.advance(500);
  tap(tabs[1]);
  actions = sentActions(ws);
  ok(actions.filter((a) => a.action === 'session.switch' && a.id === 's2').length === 2,
    'tabs: later taps keep switching after suppression expired');

  // Finger slides away (scrolling the strip): hold cancels.
  const sentBeforeSlide = sentActions(ws).length;
  touchStart(tabs[1], 10, 10);
  ui.clock.advance(400);
  touchMove(tabs[1], 40, 12);          // 30px horizontal: scroll gesture
  ok(!tabs[1].classList.contains('holding'), 'tabs: finger slide lifts the "holding" state');
  ui.clock.advance(1500);
  ok(sentActions(ws).length === sentBeforeSlide, 'tabs: slid-off hold never closes');
  touchEnd(tabs[1]);
  tabs[1].dispatchEvent({ type: 'click' });
  actions = sentActions(ws);
  ok(actions.some((a) => a.action === 'session.switch' && a.id === 's2'),
    'tabs: tap after cancelled hold still switches');

  // Small jitter (< threshold) does not cancel a hold; releasing does.
  const closesS1Before = sentActions(ws).filter((a) => a.action === 'session.close' && a.id === 's1').length;
  touchStart(tabs[0], 10, 10);
  touchMove(tabs[0], 14, 15);          // 4-5px jitter
  ok(tabs[0].classList.contains('holding'), 'tabs: sub-threshold jitter keeps the hold armed');
  touchEnd(tabs[0]);                   // released well before 1.5s
  ui.clock.advance(2000);
  const closesS1After = sentActions(ws).filter((a) => a.action === 'session.close' && a.id === 's1').length;
  ok(closesS1After === closesS1Before, 'tabs: released-before-1.5s hold is cancelled');

  return results.every(Boolean);
}

async function scenarioKeysBar(script) {
  // Default: visible, persisted on toggle, terminal refitted.
  let ui = loadUI(script);
  const ws = ui.ws();
  ws.open();
  const toolbar = ui.byId('toolbar');
  const toggleBtn = ui.byId('keys-toggle');
  ok(!!toggleBtn, 'keysbar: topbar has a toggle button next to the title');
  ok(toggleBtn.parent === ui.byId('topbar'), 'keysbar: toggle lives inside #topbar');
  ok(!toolbar.classList.contains('hidden'), 'keysbar: visible by default');
  const resizesBefore = ui.term().resizes.length;
  toggleBtn.click();
  ok(toolbar.classList.contains('hidden'), 'keysbar: toggle hides the bar');
  ok(toggleBtn.classList.contains('active') === false, 'keysbar: toggle button reflects hidden state');
  ok(ui.localStorage.getItem('zmux.keysBar.visible') === '0', 'keysbar: hidden state persisted to localStorage');
  ok(ui.term().resizes.length > resizesBefore, 'keysbar: fitTerminal() ran after hiding');
  toggleBtn.click();
  ok(!toolbar.classList.contains('hidden'), 'keysbar: second toggle shows the bar again');
  ok(ui.localStorage.getItem('zmux.keysBar.visible') === '1', 'keysbar: visible state persisted');

  // Reload with a stored "hidden" preference: boot must apply it.
  ui = loadUI(script, { localStorage: { 'zmux.keysBar.visible': '0' } });
  ok(ui.byId('toolbar').classList.contains('hidden'), 'keysbar: stored hidden preference applies at boot');
  ok(!ui.byId('keys-toggle').classList.contains('active'), 'keysbar: toggle renders inactive at boot when hidden');
  return results.every(Boolean);
}

async function scenarioScroll(script) {
  const ui = loadUI(script);
  const ws = ui.ws();
  ws.open();
  ui.clock.advance(400);               // boot-screen hide + first fitTerminal
  const term = ui.term();

  // Output at the bottom follows live.
  term.buffer.active.baseY = 10;
  term._emitScroll(10);
  ws.onmessage({ data: new Blob([Buffer.from('$ echo hi\r\nhi\r\n')]) });
  await flush();
  ui.clock.advance(20);              // deferred scrollFollow (nextFrame)
  ok(term.writes.length === 1, 'scroll: binary output frame written to xterm');
  ok(term.scrollTop === 10, 'scroll: output at bottom keeps viewport at bottom');

  // User wheels up to read: follow pauses.
  term.viewport.dispatchEvent({ type: 'wheel', deltaY: -120 });
  term._emitScroll(4);                 // xterm reports the user's position
  ok(ui.read('userScrolledUp') === true, 'scroll: wheel-up gesture marks "reading history"');
  ws.onmessage({ data: new Blob([Buffer.from('more output\r\n')]) });
  await flush();
  ok(term.scrollTop === 4, 'scroll: new output does NOT yank the reader to the bottom');

  // Scrolling back down to the bottom re-arms follow by itself.
  term._emitScroll(10);
  ok(ui.read('userScrolledUp') === false, 'scroll: reaching the bottom re-arms follow');
  ws.onmessage({ data: new Blob([Buffer.from('again\r\n')]) });
  await flush();
  ui.clock.advance(20);
  ok(term.scrollTop === 10, 'scroll: follow resumes after manual return to bottom');

  // Touch-drag down (earlier output) also marks reading; touch up does not.
  term.viewport.dispatchEvent({ type: 'touchstart', touches: [{ clientX: 5, clientY: 100 }] });
  term.viewport.dispatchEvent({ type: 'touchmove', touches: [{ clientX: 5, clientY: 130 }] });
  term._emitScroll(6);
  ok(ui.read('userScrolledUp') === true, 'scroll: touch drag toward history marks "reading"');

  // Regression: a `\x1b[2J` payload (clear / session switch) used to LOOK
  // like a scroll-up (scrollTop drops) and latch follow OFF forever.
  ws.onmessage({ data: new Blob([Buffer.concat([Buffer.from('\x1b[2J\x1b[H'), Buffer.from('prompt$ ')])]) });
  await flush();
  ui.clock.advance(20);
  ok(term.writes.some((w) => typeof w !== 'string' && w[0] === 0x1b && w[1] === 0x5b && w[2] === 0x32 && w[3] === 0x4a),
    'scroll: clear payload written to xterm');
  ok(ui.read('userScrolledUp') === false, 'scroll: \\x1b[2J payload resets "reading" latch');
  ok(term.scrollTop === term.buffer.active.baseY,
    'scroll: \\x1b[2J payload snaps viewport to live output');

  // Resize reflow (baseY shrinks, scrollTop drops: classic false latch).
  term.buffer.active.baseY = 10;
  term.buffer.active.baseY = 6;        // reflow folds history lines
  term._emitScroll(6);                 // programmatic offset shift
  ws.onmessage({ data: new Blob([Buffer.from('post-resize\r\n')]) });
  await flush();
  ui.clock.advance(20);
  ok(ui.read('userScrolledUp') === false, 'scroll: resize reflow does NOT latch "reading"');
  ok(term.scrollTop === 6, 'scroll: output still follows after resize');

  // Text frames that are plain output (not control JSON) also follow.
  term.viewport.dispatchEvent({ type: 'wheel', deltaY: -100 });
  term._emitScroll(2);
  ws.onmessage({ data: 'plain text output\r\n' });
  await flush();
  ui.clock.advance(20);
  ok(term.writes.some((w) => w === 'plain text output\r\n'), 'scroll: plain text frame written');
  ok(term.scrollTop === 2, 'scroll: text output respects "reading" too');
  ws.onmessage({ data: 'cleared via text\x1b[2Jdone' });
  await flush();
  ok(ui.read('userScrolledUp') === false, 'scroll: text-frame \\x1b[2J also resets the latch');

  // Control JSON is never rendered as terminal text.
  ws.onmessage({ data: sessionsState(['s1']) });
  ok(!term.writes.some((w) => typeof w === 'string' && w.includes('"type":"sessions"')),
    'scroll: sessions control frames are swallowed by the control handler');
  return results.every(Boolean);
}

async function scenarioSmoothness(script) {
  const ui = loadUI(script);
  const ws = ui.ws();
  ws.open();
  ui.clock.advance(400);               // boot-screen hide + first fitTerminal
  const term = ui.term();

  // LF-only output must be treated as CRLF (the messy/unaligned-edge fix).
  ok(term.options.convertEol === true,
    'smooth: convertEol enabled (bare LF renders as CRLF)');

  // Writes are batched per frame: N messages in the same tick produce ONE
  // merged term.write, not N renders (the stutter fix).
  ws.onmessage({ data: new Blob([new Uint8Array([0x61, 0x62])]) });   // "ab"
  ws.onmessage({ data: new Blob([new Uint8Array([0x0d, 0x0a])]) });   // CRLF
  await flush();                       // drain the arrayBuffer microtasks
  ok(term.writes.length === 0, 'smooth: writes deferred until the next frame');
  ui.clock.advance(20);
  ok(term.writes.length === 1, 'smooth: one merged write per frame',
    `writes=${term.writes.length}`);
  const merged = Array.from(term.writes[0] || []);
  ok(merged.length === 4 && merged[0] === 0x61 && merged[1] === 0x62 &&
     merged[2] === 0x0d && merged[3] === 0x0a,
    'smooth: binary chunks merged in order', JSON.stringify(merged));

  // Typing while reading history snaps back to live output (the "only
  // catches up after the first keystroke" complaint).
  term.buffer.active.baseY = 10;
  term.viewport.dispatchEvent({ type: 'wheel', deltaY: -120 });
  term._emitScroll(4);
  ok(ui.read('userScrolledUp') === true, 'smooth: reading-history latch armed');
  term._dataCb('e');
  ok(ui.read('userScrolledUp') === false, 'smooth: typing un-latches reading');
  ok(term.scrollTop === 10, 'smooth: typing snaps viewport to the bottom');

  // Keyboard open (visualViewport shrinks) snaps to live output so the
  // prompt is not hidden behind the IME until the user types.
  term.buffer.active.baseY = 12;
  term._emitScroll(3);
  ui.windowObj.visualViewport = { height: 320 };
  ui.read('fitToVisualViewport()');
  ok(ui.read('userScrolledUp') === false, 'smooth: keyboard open un-latches reading');
  ok(term.scrollTop === 12, 'smooth: keyboard open snaps to the prompt line');

  // A `\x1b[3J` (scrollback clear) also snaps back to live output.
  term.buffer.active.baseY = 9;
  term.viewport.dispatchEvent({ type: 'wheel', deltaY: -120 });
  term._emitScroll(2);
  ws.onmessage({ data: new Blob([Buffer.from('\x1b[3J')]) });
  await flush();
  ui.clock.advance(20);
  ok(ui.read('userScrolledUp') === false, 'smooth: \\x1b[3J resets the reading latch');
  ok(term.scrollTop === 9, 'smooth: \\x1b[3J snaps to live output');

  // And a plain-text frame still renders verbatim (batching must not mangle
  // text frames).
  ws.onmessage({ data: 'plain\r\n' });
  await flush();
  ui.clock.advance(20);
  ok(term.writes.some((w) => w === 'plain\r\n'), 'smooth: text frames render verbatim');
  return results.every(Boolean);
}

async function main() {
  const htmlPath = process.argv[2];
  if (!htmlPath) { console.error('usage: node ui_harness.js <terminal.html>'); process.exit(2); }
  const script = extractMainScript(htmlPath);

  await scenarioTabs(script);
  await scenarioKeysBar(script);
  await scenarioScroll(script);
  await scenarioSmoothness(script);

  const failed = results.filter((r) => !r).length;
  console.log(`1..${results.length}`);
  console.log(`# ${results.length - failed}/${results.length} passed`);
  process.exit(failed ? 1 : 0);
}

main().catch((err) => { console.error(err); process.exit(1); });
