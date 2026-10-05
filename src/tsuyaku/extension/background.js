// Background page: switches Tsuyaku on and off (the native host that runs the models) and routes
// messages between YouTube's pages and it.
//
//   YouTube tab (watch.js)  ─┐                       ┌─ speech recognition + subtitle translation
//   YouTube chat (chat.js)  ─┼─ ports ─ this page ─ native messaging ─┼─ chat translation
//   popup                   ─┘                       └─ message box (English → Japanese)
'use strict';

const HOST = 'tsuyaku';
// What each kind of page may ask the host to do.
const ALLOWED = {
  watch: new Set(['watch', 'unwatch', 'audio']),
  chat: new Set(['chatHello', 'chat', 'compose', 'cancelCompose']),
  popup: new Set(['stats']),
  options: new Set(['settings', 'saveSettings', 'saveGlossary', 'cloudModels', 'testCompose']),
};

const state = {
  power: 'off', // off | starting | on
  epoch: 0, // bumps every time Tsuyaku is switched on: pages send their context again
  error: '', // why it stopped or could not start
  notInstalled: false, // Firefox could not find the native host
  host: null, // the host's latest status: models, downloads, settings
};
let host = null;
let nextPid = 1;
const ports = new Map(); // pid -> {port, kind, tab}

// ------------------------------------------------------------------ on / off
function powerOn() {
  if (host) return;
  Object.assign(state, { power: 'starting', error: '', notInstalled: false, host: null });
  state.epoch += 1;
  try {
    host = browser.runtime.connectNative(HOST);
  } catch (e) {
    stopped(String(e && e.message || e));
    return;
  }
  host.onMessage.addListener(onHostMessage);
  host.onDisconnect.addListener((p) => {
    if (p !== host) return;
    host = null;
    const err = p.error ? p.error.message : '';
    stopped(state.power === 'off' ? '' : (err || 'Tsuyaku stopped unexpectedly (see tsuyaku-host.log in the Tsuyaku logs folder).'));
  });
  changed();
}

function powerOff() {
  const h = host;
  host = null;
  stopped('');
  if (h) h.disconnect(); // the host sees its input close, stops the models and exits
}

function stopped(error) {
  Object.assign(state, { power: 'off', host: null, error, notInstalled: /No such native application/i.test(error) });
  changed();
}

function hostSend(msg) {
  if (!host) return;
  try {
    host.postMessage(msg);
  } catch (e) {
    console.warn('tsuyaku: could not send to the host', e);
  }
}

function onHostMessage(msg) {
  if (!msg || typeof msg !== 'object') return;
  if (msg.type === 'status') {
    state.power = 'on';
    state.host = msg;
    changed();
    return;
  }
  const entry = ports.get(msg.pid);
  if (entry) entry.port.postMessage(msg);
}

// ------------------------------------------------------------------ pages and popup
function statusMessage() {
  return { type: 'status', status: { power: state.power, epoch: state.epoch, error: state.error,
    notInstalled: state.notInstalled, host: state.host } };
}

function changed() {
  const msg = statusMessage();
  for (const { port } of ports.values()) {
    try { port.postMessage(msg); } catch (e) { /* closing */ }
  }
  updateBadge();
}

browser.runtime.onConnect.addListener((port) => {
  const pid = nextPid++;
  const entry = { port, kind: port.name, tab: port.sender && port.sender.tab ? port.sender.tab.id : -1 };
  ports.set(pid, entry);
  port.onMessage.addListener((msg) => {
    if (!msg || typeof msg !== 'object') return;
    if (entry.kind === 'popup' || entry.kind === 'options') {
      if (msg.type === 'power') return (msg.on ? powerOn : powerOff)();
      if (msg.type === 'setup') return hostSend({ type: 'setup' });
    }
    const allowed = ALLOWED[entry.kind];
    // A page's tab is the one it runs in; the popup asks about the tab it was opened over.
    const tab = entry.kind === 'popup' ? Number(msg.tab) : entry.tab;
    if (allowed && allowed.has(msg.type)) hostSend(Object.assign({}, msg, { pid, tab }));
  });
  port.onDisconnect.addListener(() => {
    ports.delete(pid);
    if (entry.kind === 'watch' || entry.kind === 'chat') hostSend({ type: 'closed', pid });
  });
  port.postMessage(statusMessage());
});

// ------------------------------------------------------------------ toolbar badge
function updateBadge() {
  const h = state.host;
  let text = '';
  let color = '#5f6368';
  let title = 'Tsuyaku: off (click to switch on)';
  if (state.power === 'off' && state.error) {
    text = '!';
    color = '#d93025';
    title = 'Tsuyaku: ' + state.error;
  } else if (state.power === 'starting' || (h && (h.asr.state === 'loading' || h.llm.state === 'loading'))) {
    text = '…';
    title = 'Tsuyaku: starting the models…';
  } else if (h && (h.missing.length || h.installing)) {
    text = '↓';
    color = '#1a73e8';
    title = h.installing ? 'Tsuyaku: downloading models…' : 'Tsuyaku: models need to be downloaded';
  } else if (h && (h.asr.state === 'error' || h.llm.state === 'error')) {
    text = '!';
    color = '#d93025';
    title = 'Tsuyaku: ' + (h.asr.state === 'error' ? h.asr.text : h.llm.text);
  } else if (state.power === 'on') {
    text = 'ON';
    color = '#188038';
    title = 'Tsuyaku: on';
  }
  browser.browserAction.setBadgeText({ text });
  browser.browserAction.setBadgeBackgroundColor({ color });
  browser.browserAction.setTitle({ title });
}

updateBadge();
tsyPrefs().then((prefs) => { if (prefs.autoStart) powerOn(); });
