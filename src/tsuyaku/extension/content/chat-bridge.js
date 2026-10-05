// Connects chat.js to the extension's background page, which forwards to Tsuyaku. Loaded just
// before chat.js in YouTube's chat frame.
(() => {
  'use strict';
  if (window.tsuyakuConnect) return;

  const port = browser.runtime.connect({ name: 'chat' });
  const video = new URLSearchParams(location.search).get('v') || ''; // pop-out chat
  let status = null;
  let prefs = Object.assign({}, TSY_DEFAULTS);
  let handlers = null;
  let epoch = -1;
  const loaded = tsyPrefs().then((p) => { prefs = p; });

  function config() {
    const host = status && status.power === 'on' ? status.host : null;
    const c = (host && host.config) || {};
    return JSON.stringify({
      translate: !!(host && host.llm.state === 'ready') && prefs.chatTranslate,
      compose: !!(host && host.compose) && prefs.messageBox,
      debounceMs: c.debounceMs || 1000,
      tone: prefs.tone,
      versions: c.versions || 3,
      maxLength: 200,
    });
  }

  function changed() {
    if (handlers) handlers.configChanged(config());
  }

  port.onMessage.addListener((msg) => {
    if (msg.type === 'status') {
      status = msg.status;
      if (status.power !== 'off' && status.epoch !== epoch) {
        epoch = status.epoch; // (re)introduce this chat, so the popup can show its state
        port.postMessage({ type: 'chatHello', video });
      }
      changed();
    } else if (msg.type === 'chat' && handlers) {
      handlers.translated(msg.key, msg.text || '', msg.state);
    } else if (msg.type === 'composed' && handlers) {
      handlers.composed(msg.seq, JSON.stringify(msg.result));
    }
  });

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    for (const [key, { newValue }] of Object.entries(changes)) {
      if (key in prefs) prefs[key] = newValue === undefined ? TSY_DEFAULTS[key] : newValue;
    }
    changed();
  });

  // What chat.js calls: on = {translated, composed, configChanged}; ready(bridge) gets the methods.
  window.tsuyakuConnect = (on, ready) => {
    handlers = on;
    const send = (msg) => port.postMessage(Object.assign(msg, { video }));
    ready({
      config: (callback) => { loaded.then(() => callback(config())); },
      translate: (itemsJson) => send({ type: 'chat', items: JSON.parse(itemsJson), force: false }),
      translateNow: (key, text) => send({ type: 'chat', items: [{ key, text }], force: true }),
      compose: (seq, text, tone, retry) => send({ type: 'compose', seq, text, tone, retry }),
      cancelCompose: (seq) => send({ type: 'cancelCompose', seq }),
      setTone: (tone) => { prefs.tone = tone; browser.storage.local.set({ tone }); },
    });
  };
})();
