// Toolbar popup: switch Tsuyaku on/off, see the models' state, download them, settings.
'use strict';

const port = browser.runtime.connect({ name: 'popup' });
const $ = (id) => document.getElementById(id);
let status = null;

port.onMessage.addListener((msg) => {
  if (msg.type === 'status') {
    status = msg.status;
    render();
  } else if (msg.type === 'stats') {
    renderStats(msg);
  }
});

// ------------------------------------------------------------------ what happens in this tab
// The tab the popup was opened over (or popup.html?tab=<id>, when opened as a page).
let tabId = Number(new URLSearchParams(location.search).get('tab')) || null;
if (tabId === null) browser.tabs.query({ active: true, currentWindow: true }).then((tabs) => { if (tabs[0]) tabId = tabs[0].id; });
setInterval(() => {
  if (status && status.power === 'on' && tabId !== null) port.postMessage({ type: 'stats', tab: tabId });
}, 1000);

const secs = (x) => (typeof x === 'number' ? x.toFixed(1) + ' s' : '–');

function renderStats(st) {
  const subs = st.subtitles;
  let s = 'No YouTube video in this tab';
  if (subs) {
    if (!subs.lines && !subs.hearing) s = 'Waiting for the video to play…';
    else {
      s = `${subs.lines} line${subs.lines === 1 ? '' : 's'}`;
      if (subs.asr !== undefined && subs.asr !== null) s += ` · speech→text ${secs(subs.asr)} · translation ${secs(subs.translation)}`;
      if (subs.backlog > 2) s += ` · ${subs.backlog} phrases waiting`;
      if (st.device) s += ` · ${st.device}`;
    }
  }
  $('tab-subs').textContent = s;
  const c = st.chat;
  const h = status && status.host;
  let t = 'No chat in this tab';
  if (c && !prefs.chatTranslate) {
    t = 'Chat translation is switched off (below)';
  } else if (c && !c.running) {
    t = h && h.llm.state !== 'ready' ? 'Waiting for the translator to start…' : 'No Japanese messages yet';
  } else if (c) {
    t = `${c.translated} translated · ${c.waiting} waiting`;
    if (c.skipped) t += ` · ${c.skipped} too old (文A to translate)`;
    if (c.batch) t += ` · last ${c.batch[0]} in ${secs(c.batch[1])}`;
  }
  $('tab-chat').textContent = t;
  $('tab').hidden = false;
}

$('power').addEventListener('click', () => {
  if (status) port.postMessage({ type: 'power', on: status.power === 'off' });
});
$('download').addEventListener('click', () => port.postMessage({ type: 'setup' }));
$('settings').addEventListener('click', () => { browser.runtime.openOptionsPage(); window.close(); });

function setPart(name, part) {
  $(`${name}-dot`).className = 'dot ' + part.state;
  $(`${name}-text`).textContent = part.text;
}

function size(bytes) {
  return bytes >= 1e9 ? (bytes / 1e9).toFixed(2) + ' GB' : (bytes / 1e6).toFixed(0) + ' MB';
}

function render() {
  const s = status;
  const h = s.power === 'on' ? s.host : null;
  const on = s.power !== 'off';
  $('power').textContent = on ? 'Turn off' : 'Turn on';
  $('power').setAttribute('aria-pressed', String(on));

  let summary = 'Off';
  if (s.power === 'starting') summary = 'Starting…';
  else if (h) {
    const states = [h.asr.state, h.llm.state];
    if (h.installing) summary = 'Downloading models…';
    else if (states.includes('missing')) summary = 'Models need downloading';
    else if (states.includes('loading')) summary = 'Loading models…';
    else if (states.includes('error')) summary = 'Something failed (see below)';
    else summary = 'On: open a YouTube video or stream';
  }
  $('summary').textContent = summary;

  $('models').hidden = !h;
  if (!h) $('tab').hidden = true;
  if (h) {
    setPart('asr', h.asr);
    setPart('llm', h.llm);
    $('missing').hidden = !(h.missing.length && !h.installing);
    $('missing-list').textContent = h.missing.join(', ');
    $('progress').hidden = !h.installing;
    if (h.progress) {
      const p = h.progress;
      $('progress-fill').style.width = p.total ? (100 * p.done / p.total).toFixed(1) + '%' : '0';
      $('progress-text').textContent = `${p.label}: ${size(p.done)}${p.total ? ' / ' + size(p.total) : ''}`;
    } else {
      $('progress-fill').style.width = '0';
      $('progress-text').textContent = 'Preparing…';
    }
  }

  const err = $('error');
  err.replaceChildren();
  if (s.notInstalled) {
    // The add-on is installed, the program it starts isn't (or isn't connected to Firefox).
    err.append('Tsuyaku’s program isn’t set up on this PC yet. Get it from ');
    const link = document.createElement('a');
    link.href = `${browser.runtime.getManifest().homepage_url}#install`;
    link.target = '_blank';
    link.textContent = 'the Tsuyaku page';
    const code = document.createElement('code');
    code.textContent = 'install.bat';
    err.append(link, ' and run ', code, ' (already installed? run ');
    const cmd = document.createElement('code');
    cmd.textContent = 'uv run tsuyaku firefox';
    err.append(cmd, ' in its folder), then turn Tsuyaku on again.');
  } else if (s.error) {
    err.textContent = s.error;
  } else if (h && h.setupError) {
    err.textContent = 'Download failed: ' + h.setupError;
  }
  err.hidden = !err.childNodes.length;
}

// ------------------------------------------------------------------ settings
let prefs = Object.assign({}, TSY_DEFAULTS);
tsyPrefs().then((loaded) => {
  prefs = loaded;
  for (const input of document.querySelectorAll('[data-pref]')) {
    const key = input.dataset.pref;
    if (input.type === 'checkbox') input.checked = !!prefs[key];
    else input.value = prefs[key];
    input.addEventListener('input', () => {
      const value = input.type === 'checkbox' ? input.checked : Number(input.value);
      prefs[key] = value;
      browser.storage.local.set({ [key]: value });
      if (key === 'subSize') $('size-value').textContent = value + '%';
    });
  }
  $('size-value').textContent = prefs.subSize + '%';
  const tones = document.querySelectorAll('[data-tone]');
  const showTone = (tone) => tones.forEach((b) => b.setAttribute('aria-checked', String(b.dataset.tone === tone)));
  tones.forEach((b) => {
    b.setAttribute('role', 'radio');
    b.addEventListener('click', () => {
      browser.storage.local.set({ tone: b.dataset.tone });
      showTone(b.dataset.tone);
    });
  });
  showTone(prefs.tone);
});
