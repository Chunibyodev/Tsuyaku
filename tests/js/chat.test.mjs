// The extension's YouTube chat scripts (common.js + chat-bridge.js + chat.js) in jsdom, against a
// mock of YouTube's live chat page, with a fake background page answering like Tsuyaku does.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { afterEach, test } from 'node:test';
import vm from 'node:vm';
import { JSDOM } from 'jsdom';

const root = new URL('../../', import.meta.url);
const read = (p) => readFileSync(new URL(p, root), 'utf8');
const SCRIPTS = ['common.js', 'content/chat-bridge.js', 'content/chat.js']
  .map((f) => read(`src/tsuyaku/extension/${f}`));
const PAGE = read('tests/data/mock_live_chat.html');

const ready = { power: 'on', epoch: 1, host: { llm: { state: 'ready' }, compose: true, config: { debounceMs: 30, versions: 3 } } };
const loading = { power: 'on', epoch: 1, host: { llm: { state: 'loading' }, compose: false, config: {} } };

const opened = [];
afterEach(() => { while (opened.length) opened.pop().close(); }); // stops chat.js's timers

/** A chat page with the extension's scripts; `host` answers what the page sends. */
function open({ status = ready, host, statusDelay = 0 } = {}) {
  const dom = new JSDOM(PAGE, { url: 'https://www.youtube.com/live_chat?v=abcdefghijk', runScripts: 'dangerously', pretendToBeVisual: true });
  const { window } = dom;
  opened.push(window);
  // jsdom has no editing commands: chat.js falls back to setting the text and an input event.
  Object.defineProperty(window.HTMLElement.prototype, 'isContentEditable', {
    get() { return this.hasAttribute('contenteditable'); },
  });
  window.document.execCommand = () => false;
  const sent = [];
  const listeners = [];
  const port = {
    postMessage(msg) {
      sent.push(msg);
      if (host) setTimeout(() => host(msg, deliver), 5);
    },
    onMessage: { addListener: (fn) => listeners.push(fn) },
    onDisconnect: { addListener() {} },
  };
  const deliver = (msg) => listeners.forEach((fn) => fn(msg));
  const store = {};
  window.browser = {
    runtime: { connect: () => port },
    storage: {
      local: {
        get: async () => ({ ...store }),
        set: async (v) => Object.assign(store, v),
      },
      onChanged: { addListener() {} },
    },
  };
  const ctx = dom.getInternalVMContext();
  for (const src of SCRIPTS) new vm.Script(src).runInContext(ctx);
  // The background page sends the state on connect (a message: it can come after the first scan).
  if (statusDelay) setTimeout(() => deliver({ type: 'status', status }), statusDelay);
  else deliver({ type: 'status', status });
  return { window, document: window.document, sent, deliver };
}

/** Answers like Tsuyaku: "EN:" + the Japanese; English is left alone. */
function fakeHost(msg, deliver) {
  if (msg.type === 'chat') {
    for (const { key, text } of msg.items) {
      const jp = /[぀-ヿ一-鿿]/.test(text);
      deliver({ type: 'chat', key, text: jp ? `EN:${text}` : '', state: jp ? 'done' : 'none' });
    }
  } else if (msg.type === 'compose') {
    deliver({ type: 'composed', seq: msg.seq, result: {
      english: msg.text, done: true, source: 'local model', note: '',
      versions: [{ ja: 'がんばって！', back: 'Good luck!' }, { ja: '頑張ってね！', back: 'Do your best!' }],
    } });
  }
}

async function until(fn, ms = 2000) {
  const end = Date.now() + ms;
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() > end) throw new Error('timed out waiting for ' + fn);
    await new Promise((r) => setTimeout(r, 10));
  }
}

test('messages are translated in place; clicking one shows the Japanese', async () => {
  const { document, window } = open({ host: fakeHost });
  const en = await until(() => document.querySelector('.tsy-en'));
  assert.ok(en.textContent.startsWith('EN:こんばんは'));
  assert.ok(en.querySelector('img'), 'the channel emote comes back as an image');
  assert.equal(document.querySelectorAll('.tsy-en').length, 1, 'English chat is left alone');
  const jp = document.querySelector('#message');
  assert.ok(jp.classList.contains('tsy-hidden'));
  en.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  assert.equal(en.hidden, true);
  assert.equal(jp.classList.contains('tsy-hidden'), false);
});

test('nothing is sent until the translator is ready, then the waiting messages are translated', async () => {
  const page = open({ status: loading, host: fakeHost, statusDelay: 100 });
  await new Promise((r) => setTimeout(r, 300));
  assert.equal(page.sent.filter((m) => m.type === 'chat').length, 0);
  assert.equal(page.document.querySelector('.tsy-en'), null);
  page.deliver({ type: 'status', status: ready });
  const en = await until(() => page.document.querySelector('.tsy-en'));
  assert.ok(en.textContent.startsWith('EN:'));
});

test('"off" (translator not running) is asked again once it is ready', async () => {
  let running = false;
  const page = open({ host: (msg, deliver) => {
    if (msg.type !== 'chat') return;
    if (running) return fakeHost(msg, deliver);
    for (const { key } of msg.items) deliver({ type: 'chat', key, text: '', state: 'off' });
  } });
  await until(() => page.sent.some((m) => m.type === 'chat'));
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(page.document.querySelector('.tsy-en'), null);
  running = true;
  page.deliver({ type: 'status', status: loading });
  page.deliver({ type: 'status', status: ready });
  await until(() => page.document.querySelector('.tsy-en'));
});

test('message box: versions with back-translations, Tab puts the Japanese in, Enter sends', async () => {
  const { document, window } = open({ host: fakeHost });
  const input = document.getElementById('input');
  input.focus();
  input.textContent = 'good luck';
  input.dispatchEvent(new window.InputEvent('input', { bubbles: true }));
  const rows = await until(() => {
    const r = [...document.querySelectorAll('#tsy-composer .tsy-row')];
    return r.length === 2 && !document.getElementById('tsy-composer').hidden ? r : null;
  });
  assert.ok(rows[0].textContent.includes('がんばって！') && rows[0].textContent.includes('Good luck!'));
  const key = (k) => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
  key('ArrowDown');
  key('Tab');
  assert.equal(input.textContent, '頑張ってね！');
  assert.equal(window.__sent, undefined, 'Tab never sends');
  key('Enter');
  assert.equal(window.__sent, '頑張ってね！');
});

test('Escape closes the suggestions so Enter sends what was typed', async () => {
  const { document, window } = open({ host: fakeHost });
  const input = document.getElementById('input');
  input.focus();
  input.textContent = 'hello from London';
  input.dispatchEvent(new window.InputEvent('input', { bubbles: true }));
  await until(() => document.querySelector('#tsy-composer .tsy-row'));
  const key = (k) => input.dispatchEvent(new window.KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true }));
  key('Escape');
  assert.equal(document.getElementById('tsy-composer').hidden, true);
  key('Enter');
  assert.equal(window.__sent, 'hello from London');
});
