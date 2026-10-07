/* Whirlpool UI — Logik.
 * Live-Daten per WebSocket, Befehle per REST, /api/status als Fallback
 * (Diagnose, Verbindungsstatus), /api/history für die Verlaufsgrafik.
 * Theme: Hell/Dunkel nach Sonnenstand (München), wie bei der Wallbox.
 */
const $ = (id) => document.getElementById(id);

const STATUS_LABELS = { off: 'Aus', standby: 'Bereit', circulation: 'Umwälzung', heating: 'Heizt', ozone: 'Ozon', unknown: 'Unbekannt' };
const JET_LABELS = { off: 'Aus', low: 'Niedrig', high: 'Hoch' };
const COLOR_INDEX = { auto: 1, red: 2, green: 3, yellow: 4, blue: 5, purple: 6, cyan: 7, white: 8 };
const LIGHT_DOT = {
  auto: 'conic-gradient(#ef4444, #eab308, #22c55e, #06b6d4, #3b82f6, #a855f7, #ef4444)',
  red: '#ef4444', green: '#22c55e', yellow: '#eab308', blue: '#3b82f6', purple: '#a855f7', cyan: '#06b6d4', white: '#f8fafc',
};
const COLOR_NAME = Object.fromEntries(Object.entries(COLOR_INDEX).map(([n, i]) => [i, n]));
const DOT_OFF = 'rgba(255,255,255,.35)';
const OZONE_DOT_ON = '#d9b3ff';
const BUBBLES_ANIMATED = false;   // true: Bubbles im Hero steigen auf, solange die Düsen laufen
const STALE_MS = 15000;
const OPT_MS = 14000;    // Backend versucht bis 6x (je ~2,5 s)
const OPT_LONG_MS = 40000;   // Düsen (schrittweise) und Lichtfarbe: Controller reagiert verzögert
const DRAFT_MS = 30000;

let caps = null;
let latest = null;          // letzte Spa-Daten
let status = null;          // letzte /api/status-Antwort
let lastDataTs = 0;         // Zeitpunkt des letzten frischen Frames (Client-Zeit)
let lastBroadcastCount = null;
let hist = { t: [], v: [], s: [] };
let ctx = null;
let ws = null;
const opt = {};             // optimistische Werte nach Befehlen
let draft = null;           // Sollwert-Entwurf (Slider)
let draftUntil = 0;

/* ── Theme (Sonnenstand München, Code aus der Wallbox-UI) ───────────── */

let manualTheme = false;
let isDark = false;

function getSunTimes() {
  const lat = 48.14, lng = 11.58;
  const now = new Date();
  const JD = Math.floor(now / 86400000) + 2440587.5;
  const n = Math.ceil(JD - 2451545.0 + 0.0008);
  const Js = n - lng / 360;
  const M = (357.5291 + 0.98560028 * Js) % 360;
  const Mr = M * Math.PI / 180;
  const C = 1.9148 * Math.sin(Mr) + 0.02 * Math.sin(2 * Mr) + 0.0003 * Math.sin(3 * Mr);
  const lam = (M + C + 180 + 102.9372) % 360;
  const lamR = lam * Math.PI / 180;
  const Jt = 2451545.0 + Js + 0.0053 * Math.sin(Mr) - 0.0069 * Math.sin(2 * lamR);
  const sinD = Math.sin(lamR) * Math.sin(23.4397 * Math.PI / 180);
  const cosD = Math.cos(Math.asin(sinD));
  const cosW = (Math.sin(-0.833 * Math.PI / 180) - Math.sin(lat * Math.PI / 180) * sinD) / (Math.cos(lat * Math.PI / 180) * cosD);
  if (cosW < -1 || cosW > 1) return { rise: 6, set: 20 };
  const W = Math.acos(cosW) * 180 / Math.PI;
  const Jrise = (Jt - W / 360) - 2440587.5;
  const Jset = (Jt + W / 360) - 2440587.5;
  const toLocal = (jd) => {
    const d = new Date(jd * 86400000);
    return d.getUTCHours() + now.getTimezoneOffset() / -60 + d.getUTCMinutes() / 60;
  };
  return { rise: toLocal(Jrise), set: toLocal(Jset) };
}

function isDayTime() {
  const sun = getSunTimes();
  const now = new Date();
  const h = now.getHours() + now.getMinutes() / 60;
  return h >= sun.rise && h < sun.set;
}

function applyTheme(dark) {
  isDark = dark;
  document.documentElement.className = dark ? 'dark' : '';
  $('ti').innerHTML = dark ? '&#9790;' : '&#9728;';
  $('tl').textContent = dark ? 'Dunkel' : 'Hell';
  drawChart();
}

$('themeBtn').addEventListener('click', () => { manualTheme = true; applyTheme(!isDark); });
applyTheme(!isDayTime());
setInterval(() => { if (!manualTheme) applyTheme(!isDayTime()); }, 15000);

/* ── Toast / Fehlerleiste ───────────────────────────────────────────── */

let toastTimer = null;
function showToast(msg, err) {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast' + (err ? ' err' : '');
  setTimeout(() => t.classList.add('show'), 10);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove('show'), 3500);
}

function setBanner(text, bad) {
  const b = $('eb');
  if (!text) { b.hidden = true; b.innerHTML = ''; return; }
  b.hidden = false;
  b.innerHTML = '<div class="er' + (bad ? ' bad' : '') + '"><span></span><button class="rb" type="button">Erneut</button></div>';
  b.querySelector('span').textContent = '⚠ ' + text;
  b.querySelector('button').addEventListener('click', refreshAll);
}

/* ── Daten ──────────────────────────────────────────────────────────── */

function eff(key) {
  const o = opt[key];
  if (o && Date.now() < o.until) return o.val;
  return latest ? latest[key] : undefined;
}

function setOpt(key, val, ms) { opt[key] = { val, until: Date.now() + (ms || OPT_MS) }; }

function optPending(key) {
  const o = opt[key];
  return !!(o && Date.now() < o.until && (!latest || latest[key] !== o.val));
}

function pruneOpt() {
  if (!latest) return;
  for (const k of Object.keys(opt)) {
    if (latest[k] === opt[k].val) delete opt[k];
  }
}

// Optimistischer Wert abgelaufen, Controller hat nicht reagiert: melden und zurücksetzen
const OPT_LABELS = { jets: 'Düsen', light_color_index: 'Lichtfarbe', light: 'Licht', heater_enabled: 'Heizung', setpoint: 'Solltemperatur', ozone_mode: 'Ozon-Modus', heater_mode: 'Heizungsmodus', ozone_active: 'Ozon' };
function checkOptExpiry() {
  let changed = false;
  for (const k of Object.keys(opt)) {
    if (Date.now() >= opt[k].until) {
      if (latest && latest[k] !== opt[k].val) showToast((OPT_LABELS[k] || k) + ': vom Controller nicht bestätigt', true);
      delete opt[k];
      changed = true;
    }
  }
  if (changed) render();
}

function onData(data) {
  if (!data) return;
  latest = data;
  lastDataTs = Date.now();
  pruneOpt();
  render();
}

function render() {
  const d = latest;
  const ist = d ? d.current_temperature : null;
  const soll = d ? eff('setpoint') : null;

  $('iv').innerHTML = (ist != null ? ist : '–') + '<sup>°C</sup>';
  $('sv').innerHTML = (soll != null ? soll : '–') + ' <span>°C</span>';

  if (d) {
    const st = d.status || 'unknown';
    $('ht').textContent = STATUS_LABELS[st] || st;
    $('hd').style.background = st === 'heating' ? '#ffb36b' : (st === 'standby' || st === 'circulation' || st === 'ozone') ? '#6ee49a' : 'rgba(255,255,255,.35)';

    const jets = d.jets || 'off';   // tatsächlicher Zustand; das Ziel (pending) wird separat markiert
    $('jt').textContent = JET_LABELS[jets] || jets;
    $('jd').style.background = jets === 'off' ? 'rgba(255,255,255,.35)' : '#6ee49a';
    const jetsTarget = optPending('jets') ? opt.jets.val : null;
    document.querySelectorAll('#jetsSeg button').forEach((b) => {
      b.classList.toggle('active', b.dataset.target === jets);
      b.classList.toggle('pend', b.dataset.target === jetsTarget);
    });

    // Betriebsart: im zeitgesteuerten Betrieb ist die Heizung nicht manuell bedienbar
    const hmode = eff('heater_mode');
    const timerMode = hmode === 'auto';
    $('modeSeg').hidden = hmode === undefined;
    const hmTarget = optPending('heater_mode') ? opt.heater_mode.val : null;
    document.querySelectorAll('#modeSeg button').forEach((b) => {
      b.classList.toggle('active', b.dataset.mode === (latest ? latest.heater_mode : hmode));
      b.classList.toggle('pend', b.dataset.mode === hmTarget);
    });
    const heater = !timerMode && !!eff('heater_enabled');
    $('heaterBtn').className = 'pb ' + (heater ? (st === 'heating' ? 'warm' : 'on2') : 'off');
    $('heaterLbl').textContent = heater ? 'Heizung aus' : 'Heizung ein';
    $('heaterBtn').title = timerMode ? 'Zeitgesteuerter Betrieb aktiv: erst auf Manuell umschalten' : '';

    const light = !!eff('light');
    $('lightBtn').className = 'pb ' + (light ? 'lt-on' : 'off');
    $('lightLbl').textContent = light ? 'Licht aus' : 'Licht ein';

    const idx = d.light_color_index;   // tatsächlich
    const idxTarget = optPending('light_color_index') ? opt.light_color_index.val : null;
    document.querySelectorAll('#swatches .swatch').forEach((s) => {
      s.classList.toggle('sel', light && Number(s.dataset.index) === idx);
      s.classList.toggle('pend', idxTarget !== null && Number(s.dataset.index) === idxTarget);
    });

    // Hero-Chips: Punkt farbig = an, grau = aus
    $('clDot').style.background = light ? (LIGHT_DOT[COLOR_NAME[idx]] || '#ffd36b') : DOT_OFF;
    $('bubbles').classList.toggle('run', BUBBLES_ANIMATED && jets !== 'off');

    if (d.ozone_mode !== undefined) {
      $('ozoneCard').hidden = false;
      const manual = eff('ozone_mode') === 'manual';
      $('chipOzone').hidden = false;
      $('coTxt').textContent = 'Ozon ' + (manual ? 'manuell' : 'auto');
      $('coDot').style.background = eff('ozone_active') ? OZONE_DOT_ON : DOT_OFF;
      $('ozoneMode').checked = manual;
      $('ozoneManualRow').hidden = !manual;
      $('ozoneManual').checked = !!eff('ozone_active');
    }

    if (typeof renderModes === 'function' && !editing) renderModes();

    // Sollwert-Slider nur nachführen, solange kein Entwurf aktiv ist
    if (draft === null || Date.now() > draftUntil) {
      draft = null;
      if (soll != null) { $('sl').value = soll; $('sv2').textContent = soll; }
    }
    $('dgClock').textContent = d.spa_datetime ? new Date(d.spa_datetime).toLocaleString('de-DE') : '–';
  }
  updateConnectionUI();
}

function isFresh() { return Date.now() - lastDataTs < STALE_MS; }

function updateConnectionUI() {
  const sd = $('sd');
  const bridge = status ? status.bridge : null;
  if (status && status.unsupported_board_version != null) {
    setBanner('Board-Version 0x' + Number(status.unsupported_board_version).toString(16).toUpperCase() + ' wird nicht unterstützt', true);
  } else if (status && !status.connected) {
    setBanner('Bridge nicht erreichbar' + (bridge ? ' (' + bridge + ')' : ''), true);
  } else if (!isFresh()) {
    setBanner('Keine Daten vom Bus – Verkabelung (A/B, DE/RE) prüfen', false);
  } else {
    setBanner(null);
  }
  if (isFresh()) {
    sd.className = 'sd on';
    $('cl').textContent = (bridge || '') + (status && status.model ? ' · ' + status.model : '');
  } else if (status && !status.connected) {
    sd.className = 'sd err';
    $('cl').textContent = 'Nicht verbunden';
  } else {
    sd.className = 'sd';
    $('cl').textContent = status ? 'Keine Daten' : 'Verbinde…';
  }
  const dis = !isFresh();
  ['heaterBtn', 'lightBtn', 'applyTemp'].forEach((id) => { $(id).disabled = dis; });
  $('heaterBtn').disabled = dis || eff('heater_mode') === 'auto';
  $('dgLast').textContent = lastDataTs ? new Date(lastDataTs).toLocaleTimeString('de-DE') : '–';
}

/* ── Status-Polling (Fallback + Diagnose) ───────────────────────────── */

async function fetchJson(url, ms) {
  const r = await fetch(url, { signal: AbortSignal.timeout(ms || 6000) });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  return r.json();
}

async function pollStatus() {
  try {
    status = await fetchJson('/api/status');
    if (!caps) { caps = status.capabilities; setupCaps(); }
    const st = status.rx_frame_stats || {};
    const bc = st.broadcast || 0;
    if (lastBroadcastCount !== null && bc > lastBroadcastCount && status.data) onData(status.data);
    else if (lastBroadcastCount === null && bc > 0 && status.data) onData(status.data);
    lastBroadcastCount = bc;
    $('dgModel').textContent = status.model || '–';
    $('dgBridge').textContent = status.bridge || '–';
    $('dgFrames').textContent = (st.broadcast || 0) + ' / ' + (st.sync || 0);
    $('dgErr').textContent = (st.crc_error || 0) + ' / ' + (st.unrecognized || 0);
  } catch (e) {
    status = null;
    $('sd').className = 'sd err';
    $('cl').textContent = 'Backend nicht erreichbar';
    setBanner('Backend nicht erreichbar: ' + e.message, true);
    return;
  }
  render();
}

function setupCaps() {
  if (!caps) return;
  const sl = $('sl');
  sl.min = caps.temp_min_c; sl.max = caps.temp_max_c;
  $('slMin').textContent = caps.temp_min_c + '°C';
  $('slMax').textContent = caps.temp_max_c + '°C';
  const colors = caps.supported_light_colors || [];
  if (colors.length) {
    $('colorCard').hidden = false;
    const box = $('swatches');
    box.innerHTML = '';
    colors.forEach((c) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'swatch';
      b.dataset.color = c;
      b.dataset.index = COLOR_INDEX[c] || '';
      b.title = c;
      b.addEventListener('click', () => cmd('/api/light', { on: true, color: c }, 'Lichtfarbe: ' + c, () => { setOpt('light', true); setOpt('light_color_index', COLOR_INDEX[c], OPT_LONG_MS); }));
      box.appendChild(b);
    });
  }
}

/* ── WebSocket ──────────────────────────────────────────────────────── */

function connectWs() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.onmessage = (evt) => {
    try {
      const msg = JSON.parse(evt.data);
      if (msg.type === 'status' && msg.data) onData(msg.data);
    } catch (e) { /* ignorieren */ }
  };
  ws.onclose = () => setTimeout(connectWs, 3000);
  ws.onerror = () => ws.close();
}

/* ── Befehle ────────────────────────────────────────────────────────── */

async function cmd(path, body, okMsg, optimistic) {
  try {
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(6000),
    });
    if (!r.ok) {
      let detail = 'HTTP ' + r.status;
      try { detail = (await r.json()).detail || detail; } catch (e) { /* ignorieren */ }
      throw new Error(detail);
    }
    if (optimistic) optimistic();
    render();
    showToast(okMsg);
  } catch (e) {
    showToast('Fehler: ' + e.message, true);
  }
}

$('heaterBtn').addEventListener('click', () => {
  const on = !eff('heater_enabled');
  if (eff('heater_mode') === 'auto') { showToast('Zeitgesteuerter Betrieb aktiv: erst auf Manuell umschalten', true); return; }
  cmd('/api/heater', { on }, on ? 'Heizung wird eingeschaltet…' : 'Heizung wird ausgeschaltet…', () => setOpt('heater_enabled', on));
});

$('lightBtn').addEventListener('click', () => {
  const on = !eff('light');
  cmd('/api/light', { on }, on ? 'Licht wird eingeschaltet…' : 'Licht wird ausgeschaltet…', () => setOpt('light', on));
});

document.querySelectorAll('#jetsSeg button').forEach((b) => {
  b.addEventListener('click', () => {
    const t = b.dataset.target;
    cmd('/api/jets', { target: t }, 'Düsen → ' + JET_LABELS[t], () => setOpt('jets', t, OPT_LONG_MS));
  });
});

document.querySelectorAll('#modeSeg button').forEach((b) => {
  b.addEventListener('click', () => {
    const m = b.dataset.mode;
    if (latest && latest.heater_mode === m) return;
    cmd('/api/heater/mode', { mode: m }, 'Betrieb: ' + (m === 'manual' ? 'manuell' : 'zeitgesteuert'), () => setOpt('heater_mode', m, OPT_LONG_MS));
  });
});

$('ozoneMode').addEventListener('change', (e) => {
  const m = e.target.checked ? 'manual' : 'auto';
  cmd('/api/ozone/mode', { mode: m }, 'Ozon: ' + (m === 'manual' ? 'manuell' : 'automatisch'), () => setOpt('ozone_mode', m));
});

$('ozoneManual').addEventListener('change', (e) => {
  const on = e.target.checked;
  cmd('/api/ozone/manual', { on }, on ? 'Ozon ein' : 'Ozon aus', () => setOpt('ozone_active', on));
});


/* ── Zeitfenster-Helfer ─────────────────────────────────────────────── */
function hm(v) { return v ? String(v[0]).padStart(2, '0') + ':' + String(v[1]).padStart(2, '0') : '00:00'; }
function latestSlots(kind) {
  if (!latest || latest[kind + '_slot1_start'] === undefined) return null;
  return [1, 2].map((n) => ({ start: hm(latest[kind + '_slot' + n + '_start']), end: hm(latest[kind + '_slot' + n + '_end']), enabled: !!latest[kind + '_slot' + n + '_enabled'] }));
}
function sameSlots(a, b) { return JSON.stringify(a) === JSON.stringify(b); }

/* ── Zeitpläne (benannte Voreinstellungen: Zeiten + Solltemperatur) ── */
let modes = [];
let modeApply = { state: 'idle' };
let editing = null;   // { id|null, ... } solange der Editor offen ist
let lastApplyState = 'idle';

async function loadModes() {
  try {
    const r = await fetch('/api/modes', { signal: AbortSignal.timeout(4000) });
    const j = await r.json();
    modes = j.modes; modeApply = j.apply;
    if (lastApplyState === 'running' && modeApply.state !== 'running') {
      showToast(modeApply.state === 'done' ? 'Zeitplan aktiviert' : 'Zeitplan: vom Controller nicht vollständig bestätigt', modeApply.state !== 'done');
    }
    lastApplyState = modeApply.state;
    renderModes();
  } catch (e) { /* Backend kurz weg: nächster Versuch */ }
}

function curMode() {
  if (!latest || latest.setpoint === undefined) return null;
  const heat = latestSlots('heat'), filter = latestSlots('filter');
  if (!heat || !filter) return null;
  return { setpoint: latest.setpoint, heat, filter };
}
function modeMatches(m) {
  const c = curMode();
  return !!c && c.setpoint === m.setpoint && sameSlots(c.heat, m.heat) && sameSlots(c.filter, m.filter);
}
function slotSummary(sl) { return sl.filter((x) => x.enabled).map((x) => x.start + '–' + x.end).join(', ') || 'aus'; }

function renderModes() {
  const card = $('modeCard');
  card.hidden = !curMode() && !modes.length;
  const list = $('modeList');
  list.innerHTML = '';
  const running = modeApply.state === 'running';
  modes.forEach((m) => {
    const row = document.createElement('div');
    row.className = 'mr' + (modeMatches(m) ? ' act' : '');
    const info = document.createElement('div'); info.className = 'mi';
    const b = document.createElement('b'); b.textContent = m.name;
    const sm = document.createElement('small');
    sm.textContent = (running && modeApply.id === m.id)
      ? 'Wird übertragen: ' + modeApply.step + '…'
      : m.setpoint + ' °C · Heizen ' + slotSummary(m.heat) + ' · Filtern ' + slotSummary(m.filter);
    info.append(b, sm);
    const ap = document.createElement('button'); ap.type = 'button'; ap.className = 'mb pri'; ap.textContent = 'Aktivieren';
    ap.disabled = running; ap.addEventListener('click', () => applyMode(m));
    const ed = document.createElement('button'); ed.type = 'button'; ed.className = 'mb'; ed.textContent = 'Bearbeiten';
    ed.addEventListener('click', () => openEditor(m));
    row.append(info, ap, ed);
    list.appendChild(row);
  });
}

async function modeReq(method, path, body) {
  const r = await fetch(path, { method, headers: { 'Content-Type': 'application/json' }, body: body ? JSON.stringify(body) : undefined, signal: AbortSignal.timeout(6000) });
  if (!r.ok) { let d = 'HTTP ' + r.status; try { d = (await r.json()).detail || d; } catch (e) { /* ignorieren */ } throw new Error(d); }
  return r.json();
}

async function applyMode(m) {
  try {
    await modeReq('POST', '/api/modes/' + m.id + '/apply');
    modeApply = { id: m.id, state: 'running', step: 'Heizzeiten' }; lastApplyState = 'running';
    renderModes();
    const t = setInterval(async () => { await loadModes(); if (modeApply.state !== 'running') clearInterval(t); }, 1500);
  } catch (e) { showToast('Fehler: ' + e.message, true); }
}

function openEditor(m) {
  const cur = curMode();
  const base = m || { id: null, name: '', setpoint: cur ? cur.setpoint : 32, heat: cur ? cur.heat : [], filter: cur ? cur.filter : [] };
  editing = base.id || 'new';
  const ed = $('modeEd'); ed.hidden = false; ed.innerHTML = '';
  const nm = document.createElement('div'); nm.className = 'fl';
  nm.innerHTML = '<input type="text" id="meName" maxlength="40" placeholder="Name des Zeitplans"><span></span>';
  const tp = document.createElement('div'); tp.className = 'fl';
  tp.innerHTML = '<span>Solltemperatur (°C)</span><input type="number" id="meTemp" min="10" max="40" step="1">';
  ed.append(nm, tp);
  [['heat', 'Heizen'], ['filter', 'Filtern']].forEach(([kind, label]) => {
    const t = document.createElement('div'); t.className = 'sk'; t.textContent = label; ed.appendChild(t);
    [1, 2].forEach((n) => {
      const r = document.createElement('div'); r.className = 'sr';
      r.innerHTML = '<span>Zeitfenster ' + n + '</span><label class="switch"><input type="checkbox" id="me_e_' + kind + n + '"><span class="slider"></span></label>' +
        '<div class="tm"><input type="time" id="me_s_' + kind + n + '"><span>–</span><input type="time" id="me_n_' + kind + n + '"></div>';
      ed.appendChild(r);
      const sl = (base[kind] && base[kind][n - 1]) || { start: '00:00', end: '00:00', enabled: false };
      r.querySelector('#me_e_' + kind + n).checked = sl.enabled;
      r.querySelector('#me_s_' + kind + n).value = sl.start;
      r.querySelector('#me_n_' + kind + n).value = sl.end;
    });
  });
  $('meName').value = base.name; $('meTemp').value = base.setpoint;
  const act = document.createElement('div'); act.className = 'ea';
  const save = document.createElement('button'); save.type = 'button'; save.className = 'mb pri'; save.textContent = 'Speichern';
  const cancel = document.createElement('button'); cancel.type = 'button'; cancel.className = 'mb'; cancel.textContent = 'Abbrechen';
  act.append(save, cancel);
  if (m) { const del = document.createElement('button'); del.type = 'button'; del.className = 'mb'; del.textContent = 'Löschen'; del.addEventListener('click', () => deleteMode(m)); act.appendChild(del); }
  ed.appendChild(act);
  save.addEventListener('click', saveMode);
  cancel.addEventListener('click', closeEditor);
  $('modeNew').hidden = true;
  ed.scrollIntoView({ block: 'nearest' });
}

function closeEditor() { editing = null; $('modeEd').hidden = true; $('modeEd').innerHTML = ''; $('modeNew').hidden = false; }

function readEditor() {
  const slots = (kind) => [1, 2].map((n) => ({ start: $('me_s_' + kind + n).value || '00:00', end: $('me_n_' + kind + n).value || '00:00', enabled: $('me_e_' + kind + n).checked }));
  return { name: $('meName').value.trim(), setpoint: parseInt($('meTemp').value, 10), heat: slots('heat'), filter: slots('filter') };
}

async function saveMode() {
  const body = readEditor();
  if (!body.name) { showToast('Bitte einen Namen eingeben', true); return; }
  if (!(body.setpoint >= 10 && body.setpoint <= 40)) { showToast('Solltemperatur 10–40 °C', true); return; }
  try {
    await modeReq(editing === 'new' ? 'POST' : 'PUT', editing === 'new' ? '/api/modes' : '/api/modes/' + editing, body);
    closeEditor(); await loadModes(); showToast('Zeitplan gespeichert');
  } catch (e) { showToast('Fehler: ' + e.message, true); }
}

async function deleteMode(m) {
  if (!confirm('Zeitplan "' + m.name + '" löschen?')) return;
  try { await modeReq('DELETE', '/api/modes/' + m.id); closeEditor(); await loadModes(); showToast('Zeitplan gelöscht'); }
  catch (e) { showToast('Fehler: ' + e.message, true); }
}

$('modeNew').addEventListener('click', () => openEditor(null));
loadModes();
setInterval(loadModes, 10000);


/* Uhrzeit des Controllers mit der Browser-Uhr abgleichen */
function localIso(d) {
  const p2 = (n) => String(n).padStart(2, '0');
  return d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate()) + 'T' + p2(d.getHours()) + ':' + p2(d.getMinutes()) + ':' + p2(d.getSeconds());
}
$('syncTime').addEventListener('click', async () => {
  const btn = $('syncTime');
  btn.disabled = true;
  try {
    const r = await fetch('/api/time/sync', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ iso: localIso(new Date()) }), signal: AbortSignal.timeout(6000) });
    if (!r.ok) { let d = 'HTTP ' + r.status; try { d = (await r.json()).detail || d; } catch (e) { /* ignorieren */ } throw new Error(d); }
    showToast('Uhrzeit wird synchronisiert…');
    for (let i = 0; i < 20; i++) {
      await new Promise((res) => setTimeout(res, 1000));
      const st = await (await fetch('/api/time/sync')).json();
      if (st.state === 'done') { showToast('Uhrzeit synchronisiert'); break; }
      if (st.state === 'failed') { showToast('Uhrzeit: vom Controller nicht bestätigt', true); break; }
    }
  } catch (e) {
    showToast('Fehler: ' + e.message, true);
  } finally {
    btn.disabled = false;
  }
});


/* Domoticz-Status in der Diagnose */
async function loadDz() {
  try {
    const j = await (await fetch('/api/domoticz', { signal: AbortSignal.timeout(4000) })).json();
    const t = j.last_run ? new Date(j.last_run).toLocaleTimeString('de-DE') : '';
    $('dgDz').textContent = !j.enabled ? (j.message || 'aus') : (j.last_run ? t + ' · ' + j.sent + ' Werte · ' + j.message : j.message);
  } catch (e) { /* ignorieren */ }
}
loadDz();
setInterval(loadDz, 15000);

/* Solltemperatur: erst einstellen, dann "übernehmen" (ein Bus-Befehl) */

function setDraft(v) {
  const min = caps ? caps.temp_min_c : 10, max = caps ? caps.temp_max_c : 40;
  v = Math.max(min, Math.min(max, v));
  draft = v; draftUntil = Date.now() + DRAFT_MS;
  $('sl').value = v; $('sv2').textContent = v;
}
$('sl').addEventListener('input', (e) => setDraft(parseInt(e.target.value, 10)));
$('tDown').addEventListener('click', () => setDraft(parseInt($('sl').value, 10) - 1));
$('tUp').addEventListener('click', () => setDraft(parseInt($('sl').value, 10) + 1));
$('applyTemp').addEventListener('click', () => {
  const t = parseInt($('sl').value, 10);
  cmd('/api/temperature', { celsius: t }, 'Solltemperatur auf ' + t + '°C gesetzt', () => { setOpt('setpoint', t); draft = null; });
});

/* ── Verlaufsgrafik (Stil der Sauna-UI) ─────────────────────────────── */

function initChart() { ctx = $('cv').getContext('2d'); drawChart(); }

async function loadHistory() {
  try {
    const h = await fetchJson('/api/history');
    hist = { t: [], v: [], s: [] };
    (h.points || []).forEach((p) => {
      const d = new Date(p.t * 1000);
      hist.t.push(String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'));
      hist.v.push(p.v == null ? NaN : p.v);
      hist.s.push(p.s == null ? NaN : p.s);
    });
  } catch (e) { /* Verlauf optional */ }
  drawChart();
}

function drawChart() {
  const c = $('cv');
  if (!ctx) return;
  const W = c.offsetWidth, He = c.offsetHeight, dpr = window.devicePixelRatio || 1;
  if (!W || !He) return;
  c.width = W * dpr; c.height = He * dpr;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  const cG = isDark ? 'rgba(255,255,255,.06)' : 'rgba(0,0,0,.06)';
  const cL = isDark ? '#66706d' : '#939b99';
  const cI = isDark ? '#4fb3c4' : '#17808f';
  const cS = isDark ? 'rgba(79,179,196,.4)' : 'rgba(23,128,143,.35)';
  const cF = isDark ? 'rgba(79,179,196,.14)' : 'rgba(23,128,143,.09)';
  const cD = isDark ? '#1a1f1e' : '#fff';
  ctx.clearRect(0, 0, W, He);
  const n = hist.v.length;
  if (n < 2) { $('ce').style.display = 'flex'; $('ce').textContent = 'Warte auf Messwerte… (1 Wert pro Minute)'; return; }
  $('ce').style.display = 'none';

  const P = { t: 12, r: 20, b: 28, l: 36 }, cW = W - P.l - P.r, cH = He - P.t - P.b;
  const all = hist.v.concat(hist.s).filter((x) => !isNaN(x));
  let lo = Math.min.apply(null, all), hi = Math.max.apply(null, all);
  const rng = hi - lo || 4;
  lo = Math.floor((lo - rng * .15) / 2) * 2;
  hi = Math.ceil((hi + rng * .15) / 2) * 2;
  const sp = hi - lo || 4;
  const xf = (i) => P.l + (i / (n - 1)) * cW;
  const yf = (v) => P.t + (1 - (v - lo) / sp) * cH;

  ctx.font = '11px sans-serif';
  for (let s = 0; s <= 4; s++) {
    const vv = lo + (sp / 4) * s, yy = yf(vv);
    ctx.strokeStyle = cG; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(P.l, yy); ctx.lineTo(P.l + cW, yy); ctx.stroke();
    ctx.fillStyle = cL; ctx.textAlign = 'right';
    ctx.fillText(Math.round(vv * 10) / 10 + '°', P.l - 4, yy + 4);
  }
  ctx.fillStyle = cL; ctx.textAlign = 'center';
  const ls = Math.max(1, Math.floor(n / 5));
  for (let i = 0; i < n; i += ls) ctx.fillText(hist.t[i], xf(i), He - P.b + 14);

  const path = (arr) => {
    let first = true;
    for (let i = 0; i < n; i++) {
      if (isNaN(arr[i])) continue;
      if (first) { ctx.moveTo(xf(i), yf(arr[i])); first = false; } else ctx.lineTo(xf(i), yf(arr[i]));
    }
  };
  ctx.strokeStyle = cS; ctx.lineWidth = 1.5; ctx.setLineDash([4, 4]);
  ctx.beginPath(); path(hist.s); ctx.stroke(); ctx.setLineDash([]);

  ctx.beginPath(); path(hist.v);
  ctx.lineTo(xf(n - 1), P.t + cH); ctx.lineTo(xf(0), P.t + cH); ctx.closePath();
  ctx.fillStyle = cF; ctx.fill();

  ctx.strokeStyle = cI; ctx.lineWidth = 2; ctx.lineJoin = 'round';
  ctx.beginPath(); path(hist.v); ctx.stroke();

  const lv = hist.v[n - 1];
  if (!isNaN(lv)) {
    ctx.fillStyle = cI; ctx.beginPath(); ctx.arc(xf(n - 1), yf(lv), 4, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = cD; ctx.beginPath(); ctx.arc(xf(n - 1), yf(lv), 2, 0, Math.PI * 2); ctx.fill();
  }
}

window.addEventListener('resize', drawChart);

/* ── Start ──────────────────────────────────────────────────────────── */

function refreshAll() { pollStatus(); loadHistory(); }

document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });

setTimeout(() => { initChart(); refreshAll(); connectWs(); }, 50);
setInterval(pollStatus, 10000);
setInterval(loadHistory, 60000);
setInterval(updateConnectionUI, 5000);
setInterval(checkOptExpiry, 1000);
