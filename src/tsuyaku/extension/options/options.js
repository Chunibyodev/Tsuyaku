// Settings page: Tsuyaku's settings (config.toml) and glossary, read and saved through the host.
'use strict';

const port = browser.runtime.connect({ name: 'options' });
const $ = (id) => document.getElementById(id);
const form = $('form');
let status = null;
let data = null; // the host's settings payload
let cloud = {}; // provider -> {key, model}, kept while switching providers
let shownProvider = 'local';
let glossary = { scope: null, edits: {} };

port.onMessage.addListener((msg) => {
  if (msg.type === 'status') {
    status = msg.status;
    const on = status.power === 'on' && status.host;
    $('off').hidden = !!on;
    $('off-text').textContent = status.power === 'starting' ? 'Tsuyaku is starting…'
      : 'Tsuyaku is off. Turn it on to see and change its settings.';
    $('power').hidden = status.power !== 'off';
    if (on && !data) port.postMessage({ type: 'settings' });
    if (!on) { data = null; form.hidden = true; $('glossary').hidden = true; }
  } else if (msg.type === 'settings') {
    data = msg;
    fill();
  } else if (msg.type === 'saved') {
    const out = msg.glossary ? $('glossary-result') : $('save-result');
    if (!msg.ok) return result(out, msg.error, false);
    let text = 'Saved.';
    if (msg.restarting && msg.restarting.length) text += ` Restarting the ${msg.restarting.join(' and the ')}…`;
    if (msg.missing && msg.missing.length) text += ` Download ${msg.missing.join(', ')} from the Tsuyaku button.`;
    result(out, text, true);
  } else if (msg.type === 'cloudModels') {
    if (!msg.ok) return result($('test-result'), msg.error, false);
    fillCloudModels(msg.models, msg.best, cloud[shownProvider].model);
    result($('test-result'), `${msg.models.length} models available.`, true);
  } else if (msg.type === 'testCompose') {
    if (!msg.ok) return result($('test-result'), msg.error, false);
    result($('test-result'), `✓ ${msg.ja}  ↩ ${msg.back}  (${msg.source}, ${msg.seconds} s)`, true);
  }
});

$('power').addEventListener('click', () => port.postMessage({ type: 'power', on: true }));

function result(el, text, ok) {
  el.textContent = text || '';
  el.className = 'result ' + (ok ? 'ok' : 'bad');
}

function option(select, value, label) {
  const o = document.createElement('option');
  o.value = value;
  o.textContent = label;
  select.appendChild(o);
  return o;
}

// ------------------------------------------------------------------ form <-> settings
function fields() {
  return [...form.querySelectorAll('[name*="."]')];
}

function fill() {
  const s = data.settings;
  for (const el of fields()) {
    const [section, key] = el.name.split('.');
    const value = s[section][key];
    if (el.type === 'checkbox') el.checked = !!value;
    else if (el.type === 'radio') el.checked = el.value === value;
    else el.value = value;
  }
  // Whisper model: a preset or anything else.
  const wp = $('whisper-preset');
  wp.replaceChildren();
  for (const p of data.presets.whisper) option(wp, p.model, p.label);
  option(wp, '', 'Custom…');
  const knownWhisper = data.presets.whisper.some((p) => p.model === s.asr.whisper_model);
  wp.value = knownWhisper ? s.asr.whisper_model : '';
  $('whisper-custom').value = knownWhisper ? '' : s.asr.whisper_model;
  // Translation model: a preset, or repo/file/path.
  const lp = $('llm-preset');
  lp.replaceChildren();
  const groups = {};
  data.presets.llm.forEach((p, i) => {
    if (!groups[p.group]) {
      groups[p.group] = document.createElement('optgroup');
      groups[p.group].label = p.group;
      lp.appendChild(groups[p.group]);
    }
    option(groups[p.group], String(i), p.label);
  });
  option(lp, 'custom', 'Custom…');
  const idx = data.presets.llm.findIndex((p) => p.repo === s.llm.model_repo && p.file === s.llm.model_file);
  lp.value = idx >= 0 && !s.llm.model_path ? String(idx) : 'custom';
  // Message box: one key and model per provider.
  const prov = $('provider');
  prov.replaceChildren();
  option(prov, 'local', 'Local model (on your GPU, private)');
  for (const p of data.providers) option(prov, p.key, `${p.name} — free tier, needs an API key`);
  prov.value = s.compose.provider;
  cloud = {};
  for (const p of data.providers) cloud[p.key] = { key: s.compose[`${p.key}_key`] || '', model: s.compose[`${p.key}_model`] || '' };
  shownProvider = s.compose.provider;
  showProvider();
  update();
  fillGlossary();
  form.hidden = false;
  $('glossary').hidden = false;
}

function update() {
  const whisper = form.querySelector('[name="asr.engine"]:checked')?.value === 'whisper';
  const managed = form.querySelector('[name="llm.manage_server"]').checked;
  for (const el of document.querySelectorAll('[data-show="whisper"]')) el.hidden = !whisper;
  for (const el of document.querySelectorAll('[data-show="managed"]')) el.hidden = !managed;
  for (const el of document.querySelectorAll('[data-show="external"]')) el.hidden = managed;
  $('whisper-custom').hidden = $('whisper-preset').value !== '';
  $('llm-custom').hidden = $('llm-preset').value !== 'custom';
  describeModel(whisper);
}

/** What the chosen model is good at, and whether it fits the GPU next to the speech model. */
function describeModel(whisper) {
  const about = $('llm-about');
  const p = data.presets.llm[Number($('llm-preset').value)];
  about.classList.remove('warn');
  if (!p) { about.textContent = ''; return; }
  const speech = whisper && form.querySelector('[name="asr.device"]').value !== 'cpu' ? 1.5 : 0;
  const need = Math.ceil((p.gpuGb + speech) * 2) / 2;
  let text = `${p.note} Needs about ${need} GB of GPU memory${speech ? ' together with the speech model' : ''}.`;
  const gpu = data.gpu;
  if (gpu && need > gpu.gb - 0.5) { // Windows and Firefox need some too
    text += ` Your ${gpu.name} has ${gpu.gb} GB: part of the model will stay in system memory, which is slower.`;
    about.classList.add('warn');
  } else if (gpu) {
    text += ` Your ${gpu.name} has ${gpu.gb} GB.`;
  }
  about.textContent = text;
}

form.addEventListener('change', update);

$('llm-preset').addEventListener('change', () => {
  const p = data.presets.llm[Number($('llm-preset').value)];
  if (!p) return;
  form.querySelector('[name="llm.model_repo"]').value = p.repo;
  form.querySelector('[name="llm.model_file"]').value = p.file;
  form.querySelector('[name="llm.model_path"]').value = '';
});

function collect() {
  const out = {};
  for (const el of fields()) {
    if (el.type === 'radio' && !el.checked) continue;
    const [section, key] = el.name.split('.');
    out[section] = out[section] || {};
    out[section][key] = el.type === 'checkbox' ? el.checked : el.type === 'number' ? Number(el.value) : el.value;
  }
  out.asr.whisper_model = $('whisper-preset').value || $('whisper-custom').value.trim();
  storeCloud();
  for (const [key, v] of Object.entries(cloud)) {
    out.compose[`${key}_key`] = v.key;
    out.compose[`${key}_model`] = v.model;
  }
  return out;
}

form.addEventListener('submit', (ev) => {
  ev.preventDefault();
  result($('save-result'), 'Saving…', true);
  port.postMessage({ type: 'saveSettings', settings: collect() });
});

// ------------------------------------------------------------------ cloud models
function storeCloud() {
  if (!cloud[shownProvider]) return;
  cloud[shownProvider].key = $('cloud-key').value.trim();
  cloud[shownProvider].model = $('cloud-model').value;
}

function showProvider() {
  const key = $('provider').value;
  const p = data.providers.find((x) => x.key === key);
  for (const el of document.querySelectorAll('[data-show="cloud"]')) el.hidden = !p;
  shownProvider = key;
  if (!p) return;
  $('cloud-key').value = cloud[key].key;
  $('key-link').href = p.keyUrl;
  $('provider-note').textContent = p.note + ' What you type is sent to this provider.';
  fillCloudModels(cloud[key].model ? [cloud[key].model] : [], '', cloud[key].model);
}

function fillCloudModels(models, best, current) {
  const sel = $('cloud-model');
  sel.replaceChildren();
  option(sel, '', best ? `Automatic (now: ${best})` : 'Automatic (best available)');
  for (const m of models) option(sel, m, m);
  sel.value = models.includes(current) ? current : '';
}

$('provider').addEventListener('change', () => {
  storeCloud();
  showProvider();
});

$('load-models').addEventListener('click', () => {
  result($('test-result'), 'Loading models…', true);
  port.postMessage({ type: 'cloudModels', provider: $('provider').value, key: $('cloud-key').value.trim() });
});

$('test').addEventListener('click', () => {
  result($('test-result'), 'Translating “Good luck with the stream! I\'ll be watching.”…', true);
  port.postMessage({ type: 'testCompose', provider: $('provider').value, key: $('cloud-key').value.trim(),
    model: $('cloud-model').value, tone: form.querySelector('[name="compose.tone"]').value });
});

// ------------------------------------------------------------------ glossary
function fillGlossary() {
  const scope = $('scope');
  scope.replaceChildren();
  option(scope, '', 'All streams');
  for (const ch of data.glossary.channels) option(scope, ch.id, ch.name);
  glossary = { scope: null, edits: {} };
  showScope();
}

function scopeEntries(id) {
  if (glossary.edits[id]) return glossary.edits[id];
  if (!id) return data.glossary.global;
  const ch = data.glossary.channels.find((c) => c.id === id);
  return ch ? ch.entries : [];
}

function tableEntries() {
  return [...$('entries').rows].map((row) => {
    const [jp, en, note] = [...row.querySelectorAll('input')].map((i) => i.value.trim());
    return { jp, en, note };
  }).filter((e) => e.jp || e.en || e.note);
}

function addRow(e) {
  const row = $('entries').insertRow();
  for (const key of ['jp', 'en', 'note']) {
    const input = document.createElement('input');
    input.type = 'text';
    input.value = e[key] || '';
    row.insertCell().appendChild(input);
  }
  const del = document.createElement('button');
  del.type = 'button';
  del.textContent = '✕';
  del.title = 'Remove';
  del.addEventListener('click', () => row.remove());
  row.insertCell().appendChild(del);
}

function showScope() {
  if (glossary.scope !== null) glossary.edits[glossary.scope] = tableEntries();
  glossary.scope = $('scope').value;
  $('entries').replaceChildren();
  for (const e of scopeEntries(glossary.scope)) addRow(e);
  result($('glossary-result'), '', true);
}

$('scope').addEventListener('change', showScope);
$('add-entry').addEventListener('click', () => addRow({}));
$('save-glossary').addEventListener('click', () => {
  const entries = tableEntries();
  glossary.edits[glossary.scope] = entries;
  const scope = $('scope');
  port.postMessage({ type: 'saveGlossary', scope: scope.value, name: scope.selectedOptions[0].textContent, entries });
});
