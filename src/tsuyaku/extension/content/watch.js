// YouTube pages (top frame): English subtitles on YouTube's own player.
//
// While Tsuyaku is on and a video plays, its sound is tapped with Web Audio (it keeps playing
// through the tap) and sent to Tsuyaku, which recognises and translates it. The lines come back
// as cues on the video's own timeline and are drawn inside the player, so they also show in
// fullscreen. Click a subtitle to see the Japanese.
(() => {
  'use strict';
  if (window.__tsuyakuWatch) return;
  window.__tsuyakuWatch = true;

  const MIN_DISPLAY = 1.8; // seconds a line stays at least
  const HOLD = 1.2; // seconds a line stays after its speech ends
  const MAX_LINES = 2;
  const BUFFER = 4096; // samples per captured buffer (~85 ms at 48 kHz)

  let prefs = Object.assign({}, TSY_DEFAULTS);
  let status = null;
  let epoch = -1;
  const port = browser.runtime.connect({ name: 'watch' });

  // ------------------------------------------------------------------ page
  function currentVideoId() {
    // The watch page also lives at other URLs (e.g. youtube.com/@channel/live).
    const flexy = document.querySelector('ytd-watch-flexy');
    if (flexy && !flexy.hidden && /^[\w-]{11}$/.test(flexy.getAttribute('video-id') || '')) {
      return flexy.getAttribute('video-id');
    }
    if (location.pathname === '/watch') return new URLSearchParams(location.search).get('v') || '';
    const m = location.pathname.match(/^\/live\/([\w-]{11})/);
    return m ? m[1] : '';
  }

  function moviePlayer() {
    return document.querySelector('ytd-watch-flexy #movie_player') || document.getElementById('movie_player');
  }

  function mainVideo() {
    const p = moviePlayer();
    return p ? p.querySelector('video') : null;
  }

  function isAd() {
    const p = moviePlayer();
    return !!(p && (p.classList.contains('ad-showing') || p.classList.contains('ad-interrupting')));
  }

  function host() {
    return status && status.power === 'on' ? status.host : null;
  }

  function hostState(part) {
    const h = host();
    return h ? h[part].state : 'off';
  }

  // Title and channel help the translator with names (and pick the channel's glossary).
  function streamInfo(id) {
    const info = { video: id, title: '', channel: '', channelId: '', live: false };
    try {
      const p = moviePlayer();
      const page = p && p.wrappedJSObject ? p.wrappedJSObject : p; // YouTube's own player API
      const details = page && typeof page.getPlayerResponse === 'function' ? page.getPlayerResponse().videoDetails : null;
      if (details && details.videoId === id) {
        info.title = String(details.title || '');
        info.channel = String(details.author || '');
        info.channelId = String(details.channelId || '');
        info.live = !!details.isLive;
      }
    } catch (e) { /* fall back to the page text */ }
    if (!info.title) {
      const h1 = document.querySelector('ytd-watch-metadata h1');
      info.title = (h1 ? h1.textContent : document.title.replace(/ - YouTube$/, '')).trim();
    }
    if (!info.channel) {
      const a = document.querySelector('ytd-watch-metadata ytd-channel-name a, #owner #channel-name a');
      if (a) info.channel = a.textContent.trim();
    }
    return info;
  }

  let sentContext = '';
  function sendContext(id) {
    if (!host()) return;
    const msg = id ? Object.assign({ type: 'watch' }, streamInfo(id)) : { type: 'unwatch' };
    const key = JSON.stringify(msg);
    if (key !== sentContext) {
      sentContext = key;
      port.postMessage(msg);
    }
  }

  // ------------------------------------------------------------------ audio
  let ctx = null;
  let tapped = null; // the <video> whose sound runs through our AudioContext
  let processor = null;
  let capturing = false;
  let fresh = true; // the next buffer starts a new stretch (seek, pause, ad, jump to live)
  let lastEnd = null;
  let silentSince = 0;
  let blocked = ''; // why Tsuyaku can't hear the video
  let blockedSince = 0;

  function tap(video) {
    if (tapped === video) return true;
    if (!ctx) {
      try {
        ctx = new AudioContext();
      } catch (e) {
        setBlocked('Web Audio is not available in this window.');
        return false;
      }
      ctx.addEventListener('statechange', () => {
        if (ctx.state === 'suspended' && tapped) ctx.resume().catch(() => {});
      });
    }
    // Only route the sound through Web Audio once the context really runs: while suspended
    // it would mute the video.
    if (ctx.state !== 'running') {
      ctx.resume().catch(() => {});
      setBlocked('Click the video once so Tsuyaku can hear it.');
      return false;
    }
    let source;
    try {
      source = ctx.createMediaElementSource(video);
    } catch (e) {
      setBlocked("Another extension is already using this video's sound.");
      return false;
    }
    source.connect(ctx.destination); // the video keeps playing as before
    if (!processor) {
      processor = ctx.createScriptProcessor(BUFFER, 1, 1);
      processor.onaudioprocess = onAudio;
      const mute = ctx.createGain(); // the processor only runs when connected; keep it silent
      mute.gain.value = 0;
      processor.connect(mute);
      mute.connect(ctx.destination);
    }
    source.connect(processor);
    tapped = video;
    setBlocked('');
    return true;
  }

  function setBlocked(why) {
    if (why && !blocked) blockedSince = performance.now();
    blocked = why;
  }

  function onAudio(ev) {
    const v = tapped;
    if (!capturing || !v || v.paused || v.seeking || v.ended || isAd()) {
      fresh = true;
      return;
    }
    const data = ev.inputBuffer.getChannelData(0);
    const rate = ev.inputBuffer.sampleRate;
    const speed = v.playbackRate || 1;
    const end = v.currentTime;
    const start = end - (data.length / rate) * speed;
    const cont = !fresh && lastEnd !== null && Math.abs(start - lastEnd) < 0.5;
    fresh = false;
    lastEnd = end;
    const pcm = new Int16Array(data.length);
    let peak = 0;
    for (let i = 0; i < data.length; i++) {
      const s = data[i] < -1 ? -1 : data[i] > 1 ? 1 : data[i];
      pcm[i] = s * 32767;
      if (s > peak) peak = s; else if (-s > peak) peak = -s;
    }
    if (peak > 0) silentSince = 0; else if (!silentSince) silentSince = performance.now();
    // vol: Firefox applies the player's volume before this tap; Tsuyaku turns it back up.
    port.postMessage({ type: 'audio', t: start, speed, rate, cont, vol: v.muted ? 0 : v.volume, pcm: toBase64(pcm) });
  }

  function toBase64(int16) {
    const bytes = new Uint8Array(int16.buffer);
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    return btoa(bin);
  }

  // ------------------------------------------------------------------ cues
  const cues = new Map(); // id -> cue
  let cueVideo = '';

  function onCue(msg) {
    if (msg.video !== currentVideoId()) return;
    const c = msg.cue;
    const old = cues.get(c.id);
    if (old) {
      Object.assign(old, c);
    } else {
      cues.set(c.id, c);
      if (cues.size > 300) cues.delete(cues.keys().next().value);
    }
    render();
  }

  function cueText(c) {
    return c.en || (c.status === 'error' ? c.jp : '');
  }

  // Which lines to show at video time t. Lines arrive after their speech (recognition and
  // translation take a moment), so each one stays long enough to be read once it appears.
  function visible(t, now) {
    const shown = [];
    for (const c of [...cues.values()].slice(-40)) {
      const text = cueText(c);
      if (!text || c.status === 'merged' || t + 0.05 < c.start) continue;
      const words = text.split(/\s+/).filter(Boolean).length;
      const reading = Math.min(4, 0.6 + words * 0.22);
      const until = Math.max(c.end + HOLD, c.start + reading);
      if (c.appeared === undefined) c.appeared = now;
      if (t <= until || now - c.appeared < Math.max(MIN_DISPLAY, reading)) shown.push(c);
    }
    return shown.slice(-MAX_LINES);
  }

  // ------------------------------------------------------------------ drawing
  const overlay = document.createElement('div');
  overlay.className = 'tsy-subs';
  const note = document.createElement('div');
  note.className = 'tsy-note';
  note.hidden = true;
  let lastDrawn = '';

  for (const type of ['mousedown', 'mouseup', 'dblclick']) {
    overlay.addEventListener(type, (ev) => {
      if (ev.target.closest('.tsy-cue')) ev.stopPropagation(); // not YouTube's play/pause
    });
  }
  overlay.addEventListener('click', (ev) => {
    const line = ev.target.closest('.tsy-cue');
    if (!line) return;
    ev.preventDefault();
    ev.stopPropagation();
    const c = cues.get(Number(line.dataset.id));
    if (c) {
      c.showJp = !(c.showJp !== undefined ? c.showJp : prefs.showJp);
      render();
    }
  });

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function block(c) {
    const div = el('div', 'tsy-cue');
    div.dataset.id = String(c.id);
    const jp = c.showJp !== undefined ? c.showJp : prefs.showJp;
    if (jp && c.en && c.jp) div.appendChild(el('div', 'tsy-jp', c.jp));
    div.appendChild(el('div', c.en ? 'tsy-en' : 'tsy-en tsy-untranslated', cueText(c)));
    div.title = c.en ? 'Click to see the Japanese' : '';
    return div;
  }

  function render() {
    const p = moviePlayer();
    const v = mainVideo();
    if (!p || !v) return;
    if (overlay.parentElement !== p) p.appendChild(overlay);
    if (note.parentElement !== p) p.appendChild(note);
    const on = !!host() && prefs.subtitles && !!currentVideoId();
    const shown = on ? visible(v.currentTime, performance.now() / 1000) : [];
    const px = Math.max(14, Math.min(64, p.clientHeight * 0.045)) * (prefs.subSize / 100);
    const key = px.toFixed(1) + shown.map((c) => `|${c.id}:${c.status}:${c.en}:${c.showJp}:${prefs.showJp}`).join('');
    if (key !== lastDrawn) {
      lastDrawn = key;
      overlay.style.fontSize = px.toFixed(1) + 'px';
      overlay.replaceChildren(...shown.map(block));
    }
    renderNote(v);
  }

  function renderNote(v) {
    let text = '';
    if (host() && prefs.subtitles && currentVideoId()) {
      const asr = hostState('asr');
      if (asr === 'missing') text = 'Tsuyaku: the models are not downloaded yet (Tsuyaku button in the toolbar).';
      else if (asr === 'loading' || asr === 'off') text = 'Tsuyaku: loading the speech model…';
      else if (asr === 'error') text = 'Tsuyaku: the speech model failed to load (see the Tsuyaku button).';
      // (A new AudioContext takes a moment to start: only mention it if it doesn't.)
      else if (blocked && !v.paused && performance.now() - blockedSince > 2000) text = 'Tsuyaku: ' + blocked;
      else if (capturing && silentSince && performance.now() - silentSince > 3000 && (v.muted || v.volume === 0)) {
        text = "Tsuyaku can't hear the video while it is muted: unmute it (turn the volume down instead).";
      }
    } else if (status && status.power === 'starting' && currentVideoId()) {
      text = 'Tsuyaku: starting…';
    }
    if (note.textContent !== text) note.textContent = text;
    note.hidden = !text;
  }

  // A button next to YouTube's CC button switches the English subtitles on and off.
  const button = document.createElement('button');
  button.className = 'ytp-button tsy-button';
  button.title = 'Tsuyaku English subtitles';
  button.appendChild(el('span', 'tsy-button-label', 'EN'));
  button.addEventListener('click', (ev) => {
    ev.stopPropagation();
    browser.storage.local.set({ subtitles: !prefs.subtitles });
  });

  function placeButton() {
    button.hidden = !host();
    button.setAttribute('aria-pressed', String(!!prefs.subtitles));
    if (button.isConnected) return;
    try {
      const cc = document.querySelector('#movie_player .ytp-subtitles-button');
      const bar = document.querySelector('#movie_player .ytp-right-controls');
      if (cc && cc.parentElement) cc.parentElement.insertBefore(button, cc);
      else if (bar) bar.prepend(button);
    } catch (e) { /* YouTube changed its control bar: the popup still has the switch */ }
  }

  // ------------------------------------------------------------------ keeping in step
  function sync() {
    const id = currentVideoId();
    if (id !== cueVideo) {
      cues.clear();
      cueVideo = id;
      fresh = true;
    }
    sendContext(id);
    const v = mainVideo();
    capturing = false;
    if (hostState('asr') === 'ready' && prefs.subtitles && id && v) {
      // Tap only while it plays: by then the page may use sound (autoplay or a click).
      capturing = tapped === v || (!v.paused && tap(v));
    }
    placeButton();
    render();
  }

  port.onMessage.addListener((msg) => {
    if (msg.type === 'status') {
      status = msg.status;
      if (status.epoch !== epoch) { // switched on again: Tsuyaku knows nothing about this page yet
        epoch = status.epoch;
        sentContext = '';
        cues.clear();
      }
      sync();
    } else if (msg.type === 'cue') {
      onCue(msg);
    }
  });

  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== 'local') return;
    for (const [key, { newValue }] of Object.entries(changes)) {
      if (key in prefs) prefs[key] = newValue === undefined ? TSY_DEFAULTS[key] : newValue;
    }
    lastDrawn = '';
    sync();
  });

  for (const type of ['pointerdown', 'keydown']) {
    window.addEventListener(type, () => {
      if (ctx && ctx.state !== 'running') ctx.resume().then(sync, () => {});
    }, true);
  }
  document.addEventListener('yt-navigate-finish', () => setTimeout(sync, 0));
  document.addEventListener('play', () => setTimeout(sync, 0), true);
  document.addEventListener('seeking', () => { fresh = true; }, true);

  tsyPrefs().then((p) => { prefs = p; sync(); });
  setInterval(sync, 1000);
  setInterval(render, 200);
})();
