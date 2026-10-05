// Runs in YouTube's live chat (youtube.com/live_chat… and live_chat_replay…, usually the frame
// next to the video).
//
// 1. Every chat message is translated in place: it shows in English, click it to see the
//    Japanese (hover shows the other language). Super Chats, stickers, polls, emotes, the
//    ticker and sending are all YouTube's own.
// 2. Message box: type English in YouTube's chat input; ~1 s after you stop, Japanese versions
//    (each with an English back-translation) appear above it. Tab or Enter puts the selected
//    one in the input, Enter again sends it through YouTube.
(() => {
  if (window.__tsuyakuChat || typeof window.tsuyakuConnect !== 'function') return;
  window.__tsuyakuChat = true;

  // ------------------------------------------------------------------ bridge
  // The translator, through the extension's background page (chat-bridge.js, loaded first).
  let bridge = null;
  // Nothing is sent for translation until the bridge says the translator is ready.
  let cfg = { translate: false, compose: true, debounceMs: 1000, tone: 'casual', versions: 3, maxLength: 200 };
  const pending = [];
  let flushTimer = 0;

  window.tsuyakuConnect({
    translated: onTranslated,
    composed: onComposed,
    configChanged: (json) => applyConfig(JSON.parse(json)),
  }, (b) => {
    bridge = b;
    bridge.config((json) => {
      applyConfig(JSON.parse(json));
      scheduleScan();
      flush();
    });
  });

  function applyConfig(next) {
    const wasTranslating = cfg.translate;
    cfg = Object.assign(cfg, next);
    if (cfg.translate && !wasTranslating) {
      // Translate what's on screen now.
      for (const r of records.values()) if (r.state === 'off') request(r);
    }
    if (!cfg.compose) hidePanel();
    renderPanel();
  }

  // ------------------------------------------------------------------ messages
  const MESSAGE = [
    'yt-live-chat-text-message-renderer #message',
    'yt-live-chat-paid-message-renderer #message',
    'yt-live-chat-membership-item-renderer #message',
  ].join(',');
  const records = new Map(); // key -> record
  let counter = 0;
  let showAllJp = false;

  function extract(msg) {
    let text = '';
    const emotes = {};
    for (const node of msg.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.textContent;
      } else if (node.tagName === 'IMG') {
        const alt = node.getAttribute('alt') || '';
        if (/^:[^\s:]+:$/.test(alt)) { // channel emote: keep its code so it can be put back
          emotes[alt] = node;
          text += ` ${alt} `;
        } else {
          text += alt; // regular emoji
        }
      } else {
        text += node.textContent || '';
      }
    }
    return { text: text.replace(/\s+/g, ' ').trim(), emotes };
  }

  function scan() {
    for (const msg of document.querySelectorAll(MESSAGE)) {
      if (msg.dataset.tsy) continue;
      const key = 'm' + (++counter);
      msg.dataset.tsy = key;
      const { text, emotes } = extract(msg);
      const r = { key, msg, text, emotes, state: 'none', en: '', jp: showAllJp, span: null, btn: null };
      records.set(key, r);
      if (text) request(r);
    }
    if (counter % 50 === 0) cleanup();
  }

  function request(r) {
    if (!cfg.translate) { r.state = 'off'; return; }
    r.state = 'pending';
    pending.push({ key: r.key, text: r.text });
    if (!flushTimer) flushTimer = setTimeout(flush, 120);
  }

  function flush() {
    flushTimer = 0;
    if (!bridge || !pending.length) return;
    bridge.translate(JSON.stringify(pending.splice(0)));
  }

  function cleanup() {
    for (const [key, r] of records) if (!r.msg.isConnected) records.delete(key);
  }

  function onTranslated(key, text, state) {
    const r = records.get(key);
    if (!r || !r.msg.isConnected) return;
    if (r.btn) { r.btn.remove(); r.btn = null; }
    if (state === 'done' && text) {
      r.state = 'done';
      r.en = text;
      render(r);
    } else if (state === 'skipped') {
      // Too far behind (or failed): offer a button instead of translating everything late.
      r.state = 'skipped';
      const btn = document.createElement('span');
      btn.className = 'tsy-translate-btn';
      btn.textContent = '文A';
      btn.title = 'Translate';
      btn.dataset.tsyKey = key;
      r.msg.after(btn);
      r.btn = btn;
    } else if (state === 'off') {
      r.state = 'off'; // the translator wasn't running: asked again when it is
    } else {
      r.state = 'none'; // not Japanese, or nothing to translate
    }
  }

  function englishNodes(r) {
    const frag = document.createDocumentFragment();
    for (const part of r.en.split(/(:[^\s:]+:)/)) {
      if (!part) continue;
      const img = r.emotes[part];
      frag.appendChild(img ? img.cloneNode(true) : document.createTextNode(part));
    }
    return frag;
  }

  function render(r) {
    if (!r.span) {
      const span = document.createElement('span');
      span.className = r.msg.className + ' tsy-en';
      span.setAttribute('dir', 'auto');
      span.dataset.tsyKey = r.key;
      r.msg.after(span);
      r.span = span;
    }
    r.span.replaceChildren(englishNodes(r));
    r.span.title = r.text;
    r.msg.title = r.en;
    show(r);
  }

  function show(r) {
    if (!r.span) return;
    r.span.hidden = r.jp;
    r.msg.classList.toggle('tsy-hidden', !r.jp);
  }

  // Click a translated message to flip between English and Japanese. (Capture phase: YouTube's
  // own "click a message" menu still opens from the author name and avatar.)
  document.addEventListener('click', (ev) => {
    const t = ev.target;
    if (!(t instanceof Element)) return;
    const btn = t.closest('.tsy-translate-btn');
    if (btn) {
      const r = records.get(btn.dataset.tsyKey);
      if (r && bridge) {
        btn.textContent = '…';
        bridge.translateNow(r.key, r.text);
      }
      ev.preventDefault();
      ev.stopImmediatePropagation();
      return;
    }
    const el = t.closest('.tsy-en, [data-tsy]');
    if (!el || t.closest('a')) return;
    const r = records.get(el.dataset.tsyKey || el.dataset.tsy);
    if (!r || r.state !== 'done') return;
    r.jp = !r.jp;
    show(r);
    ev.preventDefault();
    ev.stopImmediatePropagation();
  }, true);

  // Moderators deleting a message, or YouTube re-rendering one, changes its text.
  function recheck(msg) {
    const r = records.get(msg.dataset.tsy);
    if (!r) return;
    const { text, emotes } = extract(msg);
    if (text === r.text) return;
    if (r.span) { r.span.remove(); r.span = null; }
    if (r.btn) { r.btn.remove(); r.btn = null; }
    msg.classList.remove('tsy-hidden');
    msg.title = '';
    Object.assign(r, { text, emotes, en: '', state: 'none' });
    if (text) request(r);
  }

  let scanTimer = 0;
  function scheduleScan() {
    if (!scanTimer) scanTimer = setTimeout(() => { scanTimer = 0; scan(); }, 40);
  }

  new MutationObserver((mutations) => {
    let added = false;
    for (const m of mutations) {
      const target = m.target.nodeType === Node.ELEMENT_NODE ? m.target : m.target.parentElement;
      const msg = target && target.closest && target.closest('[data-tsy]');
      if (msg) recheck(msg);
      if (m.addedNodes.length) added = true;
    }
    if (added) scheduleScan();
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });

  // "EN / JP" switch in the chat header flips every message.
  function addHeaderSwitch() {
    if (document.getElementById('tsy-lang-switch')) return;
    const header = document.querySelector('yt-live-chat-header-renderer');
    if (!header) return;
    const b = document.createElement('button');
    b.id = 'tsy-lang-switch';
    b.title = 'Show all messages in Japanese / English (click a message to flip just that one)';
    const label = () => { b.textContent = showAllJp ? 'JP' : 'EN'; };
    label();
    b.addEventListener('click', (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      showAllJp = !showAllJp;
      label();
      for (const r of records.values()) { r.jp = showAllJp; show(r); }
    });
    const actions = header.querySelector('#action-buttons');
    const menu = header.querySelector('#live-chat-header-context-menu');
    if (actions) actions.prepend(b);
    else if (menu && menu.parentElement) menu.parentElement.insertBefore(b, menu);
    else header.appendChild(b);
  }

  // ------------------------------------------------------------------ message box
  const EDITABLE = '[contenteditable="true"], [contenteditable=""], [contenteditable="plaintext-only"], textarea';
  let editor = null;
  let compose = null; // {english, seq, versions:[{ja, back}], selected, done, error, source, note}
  let composeSeq = 0;
  let composeTimer = 0;
  let dismissedFor = '';
  let retries = 0;
  let notice = '';
  const panel = document.createElement('div');
  panel.id = 'tsy-composer';
  panel.hidden = true;

  function editorFor(node) {
    const el = node instanceof Element ? node.closest(EDITABLE) : null;
    if (!el) return null;
    // Not the emoji search box or other pickers.
    if (el.closest('yt-emoji-picker-renderer, #emoji-picker, #search-panel, yt-live-chat-poll-editor-panel-renderer')) return null;
    return el;
  }

  function editorText(el) {
    if (!el) return '';
    if ('value' in el && el.tagName !== 'DIV') return el.value || '';
    let out = '';
    for (const node of el.childNodes) {
      if (node.nodeType === Node.TEXT_NODE) out += node.textContent;
      else if (node.tagName === 'IMG') out += node.getAttribute('alt') || '';
      else out += node.textContent || '';
    }
    return out;
  }

  function isJapanese(text) {
    const jp = (text.match(/[぀-ヿ㐀-鿿ｦ-ﾟ]/g) || []).length;
    const latin = (text.match(/[A-Za-z]/g) || []).length;
    return jp > 0 && jp * 2 >= latin;
  }

  document.addEventListener('focusin', (ev) => {
    const el = editorFor(ev.target);
    if (el) editor = el;
  }, true);

  document.addEventListener('focusout', (ev) => {
    if (ev.target === editor) setTimeout(() => {
      if (editor && document.activeElement !== editor && !panel.contains(document.activeElement)) hidePanel();
    }, 150);
  }, true);

  document.addEventListener('input', (ev) => {
    const el = editorFor(ev.target);
    if (!el) return;
    editor = el;
    onEditorInput();
  }, true);

  function onEditorInput() {
    clearTimeout(composeTimer);
    const text = editorText(editor).trim();
    notice = '';
    if (!text || !cfg.compose || isJapanese(text) || text === dismissedFor) {
      if (!text) dismissedFor = '';
      cancelCompose();
      hidePanel();
      return;
    }
    if (compose && compose.english === text) return;
    if (compose) { compose.stale = true; renderPanel(); }
    composeTimer = setTimeout(() => startCompose(text, 0), cfg.debounceMs);
  }

  function startCompose(text, retry) {
    if (!bridge) return;
    retries = retry;
    composeSeq += 1;
    compose = { english: text, seq: composeSeq, versions: [], selected: 0, done: false, error: '', source: '', note: '' };
    showPanel();
    bridge.compose(composeSeq, text, cfg.tone, retry);
  }

  function cancelCompose() {
    if (compose && bridge && !compose.done) bridge.cancelCompose(compose.seq);
    compose = null;
  }

  function onComposed(seq, json) {
    if (!compose || seq !== compose.seq) return;
    const data = JSON.parse(json);
    const selected = compose.selected;
    Object.assign(compose, data);
    compose.selected = Math.min(selected, Math.max(0, compose.versions.length - 1));
    renderPanel();
  }

  function insert(text) {
    const el = editor;
    if (!el) return;
    el.focus();
    let ok = false;
    if (el.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(el);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
      ok = document.execCommand('insertText', false, text);
      if (!ok) {
        el.textContent = text;
        el.dispatchEvent(new InputEvent('input', { bubbles: true, inputType: 'insertText', data: text }));
      }
    } else {
      el.select();
      ok = document.execCommand('insertText', false, text);
      if (!ok) {
        el.value = text;
        el.dispatchEvent(new Event('input', { bubbles: true }));
      }
    }
    compose = null;
    hidePanel();
  }

  function selectedJa() {
    const v = compose && compose.versions[compose.selected];
    return v && v.ja ? v.ja : '';
  }

  document.addEventListener('keydown', (ev) => {
    if (!editor || !editorFor(ev.target) || panel.hidden || !compose) return;
    const text = editorText(editor).trim();
    const current = compose.english === text;
    const stop = () => { ev.preventDefault(); ev.stopImmediatePropagation(); };
    if (ev.key === 'Escape') {
      dismissedFor = text;
      cancelCompose();
      hidePanel();
      stop();
    } else if ((ev.key === 'Tab' || ev.key === 'Enter') && !ev.shiftKey) {
      // While the panel is open, Enter never sends the English by accident.
      const ja = selectedJa();
      if (ja && current) {
        insert(ja);
      } else {
        if (!current) { // typed more since: translate now instead of waiting
          clearTimeout(composeTimer);
          startCompose(text, 0);
        }
        notice = 'Still translating… (Esc to send what you typed)';
        renderPanel();
      }
      stop();
    } else if ((ev.key === 'ArrowDown' || ev.key === 'ArrowUp') && compose.versions.length > 1) {
      const n = compose.versions.length;
      compose.selected = (compose.selected + (ev.key === 'ArrowDown' ? 1 : n - 1)) % n;
      renderPanel();
      stop();
    }
  }, true);

  // Keep focus in the chat input while clicking the panel.
  panel.addEventListener('mousedown', (ev) => ev.preventDefault());
  panel.addEventListener('click', (ev) => {
    const t = ev.target;
    const tone = t.closest('[data-tone]');
    const row = t.closest('[data-index]');
    if (tone) {
      cfg.tone = tone.dataset.tone;
      if (bridge) bridge.setTone(cfg.tone);
      if (compose) startCompose(compose.english, 0);
      renderPanel();
    } else if (t.closest('.tsy-retry')) {
      if (compose) startCompose(compose.english, retries + 1);
    } else if (t.closest('.tsy-close')) {
      dismissedFor = compose ? compose.english : '';
      cancelCompose();
      hidePanel();
    } else if (row && compose) {
      compose.selected = Number(row.dataset.index);
      const ja = selectedJa();
      if (ja) insert(ja);
    }
  });

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function placePanel() {
    if (panel.hidden || !editor) return;
    const anchor = editor.closest('#input-panel, yt-live-chat-message-input-renderer') || editor;
    const rect = anchor.getBoundingClientRect();
    panel.style.bottom = Math.max(8, window.innerHeight - rect.top + 6) + 'px';
  }

  function showPanel() {
    if (!panel.isConnected) document.body.appendChild(panel);
    panel.hidden = false;
    renderPanel();
  }

  function hidePanel() {
    panel.hidden = true;
  }

  function renderPanel() {
    if (panel.hidden || !compose) return;
    const head = el('div', 'tsy-head');
    head.appendChild(el('span', 'tsy-title', 'Japanese'));
    const tones = el('span', 'tsy-tones');
    for (const [key, label] of [['casual', 'Casual'], ['polite', 'Polite'], ['fan', 'Fan']]) {
      const b = el('button', 'tsy-tone' + (cfg.tone === key ? ' tsy-on' : ''), label);
      b.dataset.tone = key;
      tones.appendChild(b);
    }
    head.appendChild(tones);
    head.appendChild(el('span', 'tsy-spacer'));
    const retry = el('button', 'tsy-retry', '↻');
    retry.title = 'Different wording';
    head.appendChild(retry);
    const close = el('button', 'tsy-close', '✕');
    close.title = 'Close (Esc) - Enter then sends what you typed';
    head.appendChild(close);

    const list = el('div', 'tsy-list');
    if (compose.error) list.appendChild(el('div', 'tsy-error', compose.error));
    if (!compose.versions.length && !compose.error) list.appendChild(el('div', 'tsy-wait', 'Translating…'));
    compose.versions.forEach((v, i) => {
      const row = el('div', 'tsy-row' + (i === compose.selected ? ' tsy-selected' : ''));
      row.dataset.index = String(i);
      const ja = el('div', 'tsy-ja', v.ja);
      if (v.ja.length > cfg.maxLength) ja.appendChild(el('span', 'tsy-long', ` (${v.ja.length}/${cfg.maxLength})`));
      row.appendChild(ja);
      row.appendChild(el('div', 'tsy-back', v.back ? '↩ ' + v.back : (compose.done ? '' : '↩ …')));
      list.appendChild(row);
    });
    if (!compose.done && compose.versions.length === 1 && cfg.versions > 1) {
      list.appendChild(el('div', 'tsy-wait', 'More versions…'));
    }

    const foot = el('div', 'tsy-foot');
    const tips = compose.stale ? 'Updating when you stop typing…'
      : 'Tab/Enter: use it · Enter again: send' + (compose.versions.length > 1 ? ' · ↑↓ choose' : '') + ' · Esc: send as typed';
    foot.appendChild(el('span', '', notice || tips));
    if (compose.source) foot.appendChild(el('span', 'tsy-source', compose.source));
    const children = [head, list];
    if (compose.note) children.push(el('div', 'tsy-note', compose.note));
    children.push(foot);
    panel.replaceChildren(...children);
    placePanel();
  }

  window.addEventListener('resize', placePanel);

  // ------------------------------------------------------------------ styles
  const style = document.createElement('style');
  style.id = 'tsuyaku-chat-style';
  style.textContent = `
    #message.tsy-hidden { display: none !important; }
    .tsy-en { cursor: pointer; }
    .tsy-en:hover, [data-tsy]:not(.tsy-hidden):hover { text-decoration: underline dotted rgba(127,127,127,.6); }
    .tsy-translate-btn {
      cursor: pointer; margin-left: 6px; padding: 0 5px; border-radius: 8px; font-size: 11px;
      color: var(--yt-live-chat-secondary-text-color, #888); border: 1px solid rgba(127,127,127,.4);
    }
    #tsy-lang-switch {
      margin: 0 6px; padding: 2px 8px; border-radius: 12px; cursor: pointer; font: 600 12px Roboto, Arial, sans-serif;
      color: var(--yt-live-chat-primary-text-color, #fff); background: rgba(127,127,127,.18); border: 1px solid rgba(127,127,127,.35);
    }
    #tsy-composer {
      position: fixed; left: 8px; right: 8px; z-index: 99999; max-height: 55vh; overflow: auto;
      background: var(--yt-live-chat-background-color, #212121); color: var(--yt-live-chat-primary-text-color, #fff);
      border: 1px solid rgba(127,127,127,.35); border-radius: 10px; box-shadow: 0 6px 24px rgba(0,0,0,.45);
      font: 13px Roboto, "Yu Gothic UI", "Meiryo", Arial, sans-serif; padding: 6px 0;
    }
    #tsy-composer[hidden] { display: none; }
    #tsy-composer button {
      background: transparent; color: inherit; border: 1px solid rgba(127,127,127,.35); border-radius: 12px;
      padding: 1px 8px; cursor: pointer; font: inherit; font-size: 12px; margin-left: 4px;
    }
    #tsy-composer .tsy-head { display: flex; align-items: center; padding: 0 10px 4px; gap: 2px; }
    #tsy-composer .tsy-title { font-weight: 600; margin-right: 6px; }
    #tsy-composer .tsy-spacer { flex: 1; }
    #tsy-composer .tsy-tone.tsy-on { background: #3ea6ff; border-color: #3ea6ff; color: #0f0f0f; }
    #tsy-composer .tsy-row { padding: 5px 10px; cursor: pointer; border-left: 3px solid transparent; }
    #tsy-composer .tsy-row:hover { background: rgba(127,127,127,.12); }
    #tsy-composer .tsy-row.tsy-selected { background: rgba(62,166,255,.16); border-left-color: #3ea6ff; }
    #tsy-composer .tsy-ja { font-size: 15px; line-height: 1.35; }
    #tsy-composer .tsy-back { font-size: 12px; color: var(--yt-live-chat-secondary-text-color, #aaa); margin-top: 1px; }
    #tsy-composer .tsy-long { color: #ff6b6b; font-size: 11px; }
    #tsy-composer .tsy-wait { padding: 5px 10px; color: var(--yt-live-chat-secondary-text-color, #aaa); }
    #tsy-composer .tsy-error { padding: 5px 10px; color: #ff6b6b; }
    #tsy-composer .tsy-note { padding: 2px 10px; font-size: 11px; color: #f5c518; }
    #tsy-composer .tsy-foot {
      display: flex; justify-content: space-between; gap: 8px; padding: 4px 10px 0; font-size: 11px;
      color: var(--yt-live-chat-secondary-text-color, #aaa);
    }
  `;
  (document.head || document.documentElement).appendChild(style);

  scheduleScan();
  setInterval(addHeaderSwitch, 2000);
  addHeaderSwitch();
})();
