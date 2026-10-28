// F1 Engineer dashboard: telemetry comparison (up to 3 laps, same track), timing, race, live, engineer chat.
const $ = (id) => document.getElementById(id);
const fmt = (s) => { if (!s) return "-"; const m = Math.floor(s / 60); const r = (s - m * 60).toFixed(3).padStart(6, "0"); return m ? `${m}:${r}` : r; };
const sgn = (v, d = 3) => (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const LAPCOL = () => [css("--lap1"), css("--lap2"), css("--lap3")];
const GRID = "#20232a", AXIS = "#6f7480";
let OFFLINE = true;

// ---------------------------------------------------------------- helpers
function interp(x, xs, ys) {
  let lo = 0, hi = xs.length - 1; if (x <= xs[0]) return ys[0]; if (x >= xs[hi]) return ys[hi];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  return ys[lo] + (x - xs[lo]) / (xs[hi] - xs[lo]) * (ys[hi] - ys[lo]);
}
function nearestIdx(x, xs) {
  let lo = 0, hi = xs.length - 1; if (x <= xs[0]) return 0; if (x >= xs[hi]) return hi;
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  return x - xs[lo] < xs[hi] - x ? lo : hi;
}
const tAt = (lap, d) => interp(d, lap.trace.dist, lap.trace.t);
const TYRE = { soft: "S", medium: "M", hard: "H", inter: "I", intermediate: "I", wet: "W" };
const tyreBadge = (c, age) => c ? `<span class="tyre ${esc(String(c).toLowerCase().replace("intermediate", "inter"))}" title="${esc(c)}${age != null ? `, ${age} laps old` : ""}">${TYRE[String(c).toLowerCase()] || "?"}</span>` : `<span class="muted">-</span>`;
const isGhost = (l) => /personal best|rival|ghost/i.test(l.car || "");
const lapLabel = (l) => l.car === "Player" ? `Lap ${l.lap}` : isGhost(l) ? "PB ghost" : `${(l.car || "Car").split(" ").pop()} L${l.lap}`;
const EXPANDED = new Set();   // sessions whose other-driver laps are unfolded
const sessDate = (ts) => ts ? new Date(ts * 1000).toLocaleString([], { weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
function fitCanvas(c, hCss) {
  const dpr = devicePixelRatio || 1, w = c.clientWidth || 400, h = hCss || +c.dataset.h || c.clientHeight || w;
  c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.font = "600 11px Titillium Web, sans-serif"; g.textBaseline = "middle";
  return { g, w, h };
}
function axes(yl, splits) {
  const a = { stroke: AXIS, grid: { stroke: GRID, width: 1 }, ticks: { stroke: GRID, width: 1 }, font: "11px Titillium Web" };
  return [{ ...a, values: (u, v) => v.map((m) => m >= 1000 ? (m / 1000).toFixed(1) + " km" : m + " m") },
    { ...a, size: 46, label: yl, labelSize: 14, labelFont: "600 11px Titillium Web", ...(splits ? { splits: () => splits } : {}) }];
}

// ---------------------------------------------------------------- tabs
const TABS = ["telemetry", "timing", "race", "live", "engineer"];
function showTab(t) {
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === t));
  TABS.forEach((k) => $("tab-" + k).classList.toggle("hidden", k !== t));
  if (t === "telemetry") requestAnimationFrame(resizeAll);
  if (t === "timing") drawTiming();
  if (t === "race") drawRace();
  if (t === "engineer") loadMemory();
}
document.querySelectorAll("#tabs button").forEach((b) => b.onclick = () => { history.replaceState(null, "", "#" + b.dataset.tab + (b.dataset.tab === "telemetry" && SEL.length ? "?laps=" + SEL.map((s) => s.id).join(",") : "")); showTab(b.dataset.tab); });

// ---------------------------------------------------------------- live
let lastReportKey = null, wasImporting = false;
function tempColor(t) { return t < 80 ? "#1f3550" : t < 95 ? "#1f4a3a" : t < 108 ? "#2c4a1f" : "#5a2420"; }
function renderState(s) {
  OFFLINE = !!s.offline;
  $("importing").classList.toggle("hidden", !s.importing);
  if (s.importing) $("importing").textContent = `Importing recording ${s.importing}…`;
  if (wasImporting && !s.importing) loadLaps();
  wasImporting = !!s.importing;
  $("dot").classList.toggle("on", !!s.connected);
  $("conn-t").textContent = s.offline ? "review" : s.connected ? "live" : "waiting for game";
  $("liveoff").classList.toggle("hidden", !s.offline);
  if (s.offline) return;
  $("h-game").textContent = s.game || "-"; $("h-track").textContent = s.track || "-"; $("h-mode").textContent = s.mode || "-";
  $("h-input").textContent = s.input_device || "-";
  if (s.lap) {
    const d = s.lap.delta;
    $("delta").textContent = d === undefined ? "-" : (d > 0 ? "+" : "") + d.toFixed(3);
    $("delta").className = "big " + (d === undefined ? "" : d > 0 ? "neg" : "pos");
    $("lapinfo").textContent = `Lap ${s.lap.num} · ${fmt(s.lap.time)} · last ${fmt(s.lap.last)} · best ${fmt(s.lap.best)}${s.lap.invalid ? " · invalid" : ""} · P${s.lap.position}`;
  }
  if (s.car) {
    $("speed").textContent = s.car.speed; $("gear").textContent = s.car.gear <= 0 ? (s.car.gear < 0 ? "R" : "N") : s.car.gear;
    $("rev").style.width = Math.min(100, s.car.rpm / 125) + "%";
    $("thr").style.width = s.car.throttle * 100 + "%"; $("brk").style.width = s.car.brake * 100 + "%";
    const names = ["RL", "RR", "FL", "FR"], order = [2, 3, 0, 1], wear = (s.status && s.status.tyre_wear) || [];
    $("tyres").innerHTML = order.map((i) => `<div class="tyrebox" style="background:${tempColor(s.car.tyre_surface[i])}"><small>${names[i]}</small><b>${s.car.tyre_surface[i]}°</b><small>inner ${s.car.tyre_inner[i]}° · ${wear[i] ?? "-"}%</small></div>`).join("");
  }
  if (s.status) {
    $("compound").innerHTML = `${tyreBadge(s.status.compound)} ${s.status.tyre_age} laps`;
    $("fuel").textContent = (s.status.fuel_laps > 0 ? "+" : "") + s.status.fuel_laps + " laps";
    $("ers").textContent = s.status.ers_pct + "%"; $("tc").textContent = ["off", "medium", "full"][s.status.tc] ?? "-";
    $("abs").textContent = s.status.abs ? "on" : "off";
  }
  if (s.race && s.race.position) {
    const r = s.race;
    $("racebox").innerHTML = `P${r.position} · ahead ${r.ahead ? esc(r.ahead.name) + " " + (r.ahead.gap_s ?? "-") + "s" : "-"} · behind ${r.behind ? esc(r.behind.name) + " " + (r.behind.gap_s ?? "-") + "s" : "-"}`;
  }
  const rep = s.last_report;
  if (rep && rep.lap_num + ":" + rep.lap_time !== lastReportKey) {
    lastReportKey = rep.lap_num + ":" + rep.lap_time;
    $("report").innerHTML = `<div class="sub">Lap ${rep.lap_num} ${fmt(rep.lap_time)} vs ${esc(rep.ref_label)} (${sgn(rep.delta)})</div>
      <table class="grid-table"><thead><tr><th>Corner</th><th>Δ s</th><th>Apex</th><th>Why</th></tr></thead><tbody>${rep.corners.map((c) =>
      `<tr><td class="label">${esc(c.corner)}</td><td class="${c.time_loss > 0.02 ? "slower" : c.time_loss < -0.02 ? "faster" : ""}">${sgn(c.time_loss)}</td><td>${c.min_speed}</td><td class="muted" style="text-align:left;font:400 12px var(--font)">${esc(c.causes.slice(0, 2).join("; "))}</td></tr>`).join("")}</tbody></table>`;
  }
  if (s.plan) {
    $("plan-goal").textContent = s.plan.goal;
    $("plan").innerHTML = `<table class="grid-table"><tbody>${s.plan.steps.map((p, i) => `<tr><td class="label" style="white-space:normal">${i + 1 === s.plan.step && !s.plan.finished ? "▶ " : ""}${esc(p.text)}</td><td class="${p.done ? "faster" : "muted"}">${p.done ? "done" : `${p.attempts}/${p.laps}`}</td><td class="muted">${p.best ?? ""}</td></tr>`).join("")}</tbody></table>`;
  }
  if (s.advice && s.advice.length) $("advice").innerHTML = s.advice.map((a) => `<li><b>${esc(a.change)}</b><br><small>${esc(a.why)}</small></li>`).join("");
}
function addRadio(ev) {
  for (const box of [$("radio-live"), $("radio-all")]) {
    const d = document.createElement("div");
    d.className = "msg " + (ev.role || "engineer"); d.textContent = ev.text;
    box.appendChild(d); while (box.children.length > 200) box.removeChild(box.firstChild);
    box.scrollTop = box.scrollHeight;
  }
}
function connect() {
  const ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (m) => {
    const j = JSON.parse(m.data);
    if (j.state) renderState(j.state);
    if (j.event && j.event.type === "radio") addRadio(j.event);
    if (j.event && j.event.type === "lap") loadLaps();
  };
  ws.onclose = () => { $("dot").classList.remove("on"); $("conn-t").textContent = "reconnecting"; setTimeout(connect, 1500); };
}
$("chatform").onsubmit = async (e) => {
  e.preventDefault(); const q = $("q").value.trim(); if (!q) return; $("q").value = "";
  if (OFFLINE) addRadio({ role: "driver", text: q });
  const th = document.createElement("div"); th.className = "msg thinking"; th.textContent = "…"; $("radio-all").appendChild(th);
  try {
    const r = await (await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }) })).json();
    if (OFFLINE) addRadio({ role: "engineer", text: r.answer });
  } catch (err) { addRadio({ role: "engineer", text: "Radio's down: " + err }); } finally { th.remove(); }
};

// ---------------------------------------------------------------- memory
async function loadMemory() {
  let m = {}; try { m = await (await fetch("/api/memory")).json(); } catch { return; }
  const prefs = Object.entries(m.preferences || {}), notes = m.notes || [];
  $("memory").innerHTML = (prefs.length || notes.length ? "" : '<div class="sub">Nothing yet. Tell him things like "coasting is fine for me on keyboard" or "my goal is a 1:27 at Silverstone".</div>') +
    prefs.map(([t, p]) => `<div class="mem"><span class="tag">${esc(t.replace("_", " "))}</span><div><b>${esc(p.text)}</b><small>since ${esc(p.since)}${p.history && p.history.length ? ` · replaced "${esc(p.history[p.history.length - 1].text)}"` : ""}</small></div><button class="btn" data-forget="${esc(t)}">Forget</button></div>`).join("") +
    notes.slice().reverse().map((n) => `<div class="mem"><span class="tag note">note</span><div><b>${esc(n.note)}</b><small>${esc(n.date)}</small></div><button class="btn" data-forget="${esc(n.note)}">Forget</button></div>`).join("");
  document.querySelectorAll("[data-forget]").forEach((b) => b.onclick = async () => { await fetch("/api/memory?what=" + encodeURIComponent(b.dataset.forget), { method: "DELETE" }); loadMemory(); });
}

// ---------------------------------------------------------------- laps & sessions
let LAPS = [], SESSIONS = [], cache = new Map();
let SEL = [];        // [{id, slot}] slot = fixed colour slot while selected (colour follows the lap, not its rank)
async function loadLaps() {
  try { LAPS = await (await fetch("/api/laps")).json(); } catch { return; }
  const by = new Map();
  for (const l of LAPS) { if (!by.has(l.session)) by.set(l.session, { id: l.session, track: l.track, type: l.type, created: l.created, laps: [] }); by.get(l.session).laps.push(l); }
  SESSIONS = [...by.values()].sort((a, b) => b.created - a.created);
  for (const s of SESSIONS) s.laps.sort((a, b) => (b.car === "Player") - (a.car === "Player") || a.lap - b.lap || a.id - b.id);
  renderLapList(); fillSessionSelects();
}
function sectorClasses(laps) {   // F1 timing: purple session best, green personal improvement, yellow slower
  const out = new Map(), withS = laps.filter((l) => l.sectors && l.car === "Player").sort((a, b) => a.lap - b.lap || a.id - b.id);
  const best = [0, 1, 2].map((k) => Math.min(...withS.map((l) => l.sectors[k] || 1e9)));
  const run = [1e9, 1e9, 1e9];
  for (const l of withS) out.set(l.id, l.sectors.map((v, k) => { const c = v === best[k] ? "p" : v < run[k] ? "g" : "y"; run[k] = Math.min(run[k], v); return c; }));
  return out;
}
const selTrack = () => SEL.length ? LAPS.find((l) => l.id === SEL[0].id)?.track : null;
function renderLapList() {
  const track = selTrack();
  $("lapgroups").innerHTML = SESSIONS.map((s) => {
    const off = track && s.track !== track, sc = sectorClasses(s.laps);
    const best = s.laps.filter((l) => l.lap_time > 0).reduce((b, l) => !b || l.lap_time < b.lap_time ? l : b, null);
    return `<div class="sess ${off ? "off" : ""}" title="${off ? "Different track: comparing laps from different tracks makes no sense" : ""}">
      <div class="sess-h"><b>${esc(s.track || "?")}</b><small>${esc(s.type || "")}<br>${sessDate(s.created)}</small></div>` +
      (() => {
        const mine = s.laps.filter((l) => l.car === "Player" || isGhost(l)), others = s.laps.filter((l) => !mine.includes(l));
        const shown = EXPANDED.has(s.id) ? [...mine, ...others.sort((a, b) => a.lap_time - b.lap_time)] : mine;
        return shown.map((l) => lapRow(l, s, sc, best, off)).join("") + (others.length
          ? `<button class="btn more" data-sess="${esc(s.id)}">${EXPANDED.has(s.id) ? "Hide" : "Show"} other drivers (${others.length} laps)</button>` : "");
      })() + "</div>";
  }).join("") || '<div class="sub">No laps yet. Drive with the game running, or import a recording.</div>';
  document.querySelectorAll(".lap").forEach((row) => row.onclick = () => toggleLap(+row.dataset.id));
  document.querySelectorAll(".more").forEach((b) => b.onclick = () => { const k = b.dataset.sess; EXPANDED.has(k) ? EXPANDED.delete(k) : EXPANDED.add(k); renderLapList(); });
}
function lapRow(l, s, sc, best, off) {
        const sel = SEL.find((x) => x.id === l.id), col = sel ? LAPCOL()[sel.slot] : null;
        return `<div class="lap ${sel ? "sel" : ""} ${off ? "disabled" : ""}" data-id="${l.id}">
          <span class="chk" style="${col ? `background:${col};border-color:${col}` : ""}"></span>
          <span class="n">${l.car === "Player" ? "L" + l.lap : isGhost(l) ? '<span class="ghost-tag">PB</span>' : `<span class="ghost-tag" title="${esc(l.car)}">${esc((l.car || "?").split(" ").pop().slice(0, 3).toUpperCase())}</span>`}</span>
          <span class="t ${best && best.id === l.id ? "best" : ""} ${l.valid ? "" : "inv"}">${fmt(l.lap_time)}</span>
          ${tyreBadge(l.compound, l.tyre_age)}
          <span class="meta">${sc.has(l.id) ? `<span class="secs">${sc.get(l.id).map((c) => `<i class="${c}"></i>`).join("")}</span>` : ""}
            <span class="inp">${l.input === "keyboard" ? "keyboard" : l.input ? "pad/wheel" : l.car === "Player" ? "" : isGhost(l) ? "ghost" : "AI"}</span></span></div>`;
}
async function toggleLap(id) {
  const lap = LAPS.find((l) => l.id === id), track = selTrack();
  if (!lap || (track && lap.track !== track)) return;           // never compare across tracks
  const i = SEL.findIndex((s) => s.id === id);
  if (i >= 0) SEL.splice(i, 1);
  else {
    if (SEL.length >= 3) SEL.shift();
    const used = new Set(SEL.map((s) => s.slot));
    SEL.push({ id, slot: [0, 1, 2].find((k) => !used.has(k)) });
  }
  history.replaceState(null, "", "#telemetry?laps=" + SEL.map((s) => s.id).join(","));
  renderLapList(); await drawTelemetry();
}
async function getLap(id) {
  if (!cache.has(id)) { const l = await (await fetch("/api/lap/" + id)).json(); l.id = id; l.corners = l.corners || []; cache.set(id, l); }
  return cache.get(id);
}

// ---------------------------------------------------------------- telemetry
let LAPSD = [], BASE = null, X = null, SEG = [], plots = [], syncing = false;
function segments(lap) {
  const c = lap.corners, L = lap.track_length || lap.trace.dist[lap.trace.dist.length - 1];
  if (!c.length) return [];
  const b = [0, ...c.slice(1).map((x, i) => (c[i].apex + x.apex) / 2), L], seen = {};
  return c.map((x, i) => { seen[x.name] = (seen[x.name] || 0) + 1; return { name: x.name + (seen[x.name] > 1 ? " " + seen[x.name] : ""), apex: x.apex, s: b[i], e: b[i + 1] }; });
}
function cornerStats(lap, seg) {
  const t = lap.trace, d = t.dist;
  const i0 = nearestIdx(Math.max(seg.s, seg.apex - 120), d), i1 = nearestIdx(Math.min(seg.e, seg.apex + 120), d);
  let mi = i0; for (let i = i0; i <= i1; i++) if (t.speed[i] < t.speed[mi]) mi = i;
  const apexD = d[mi]; let brake = null;
  for (let i = nearestIdx(seg.s, d); i <= mi; i++) if (t.brake[i] > 0.1) { brake = apexD - d[i]; break; }
  return { vmin: t.speed[mi], gear: Math.round(t.gear[mi]), brake, time: tAt(lap, seg.e) - tAt(lap, seg.s) };
}
function lapStats(lap) {
  const t = lap.trace, n = t.dist.length, step = n > 1 ? t.dist[1] - t.dist[0] : 4;
  let top = 0, low = 1e9, ft = 0, br = 0, coast = 0, shifts = 0, glat = 0;
  for (let i = 0; i < n; i++) {
    top = Math.max(top, t.speed[i]); if (t.speed[i] > 5) low = Math.min(low, t.speed[i]);
    if (t.throttle[i] > 0.98) ft++; if (t.brake[i] > 0.05) br++;
    if (t.throttle[i] < 0.05 && t.brake[i] < 0.05 && t.speed[i] > 60) coast += step;
    if (i && Math.round(t.gear[i]) !== Math.round(t.gear[i - 1])) shifts++;
    if (t.glat) glat = Math.max(glat, Math.abs(t.glat[i]));
  }
  return { top, low, avg: t.dist[n - 1] / lap.lap_time * 3.6, ft: ft / n * 100, br: br / n * 100, coast, shifts, glat };
}
const colorOf = (lap) => LAPCOL()[SEL.find((s) => s.id === lap.id).slot];

async function drawTelemetry() {
  plots.forEach((p) => p.destroy()); plots = []; $("charts").innerHTML = "";
  $("tele-empty").classList.toggle("hidden", SEL.length > 0); $("tele-content").classList.toggle("hidden", !SEL.length);
  if (!SEL.length) { $("cmpbar").innerHTML = ""; return; }
  LAPSD = await Promise.all(SEL.map((s) => getLap(s.id)));
  BASE = LAPSD.reduce((b, l) => !b || l.lap_time < b.lap_time ? l : b, null);   // the fastest is the baseline
  X = BASE.trace.dist; SEG = segments(BASE);
  const meta = LAPS.find((l) => l.id === BASE.id) || {};
  if (OFFLINE) { $("h-track").textContent = meta.track || "-"; $("h-mode").textContent = meta.type || "-"; $("h-input").textContent = BASE.input || "-"; }
  drawCmpbar(); drawSummary(); drawMinis(); drawCharts(); drawCorners(); drawMap(); cursorAt(null); drawGG(); drawNotes();
}
function drawCmpbar() {
  $("cmpbar").innerHTML = LAPSD.map((l) => {
    const d = l.lap_time - BASE.lap_time, m = LAPS.find((x) => x.id === l.id) || {};
    return `<div class="chip"><span class="stripe" style="background:${colorOf(l)}"></span>
      <div class="info"><span class="tm">${fmt(l.lap_time)}</span><small>${lapLabel(m)} · ${esc(sessDate(m.created))}</small></div>
      ${tyreBadge(l.compound || m.compound, l.tyre_age ?? m.tyre_age)}
      <span class="delta" style="color:${l === BASE ? "var(--purple)" : "var(--text-2)"}">${l === BASE ? "fastest" : sgn(d)}</span>
      ${l.valid === false ? '<small>invalid</small>' : ""}<button title="Remove" data-rm="${l.id}">×</button></div>`;
  }).join("") + `<div class="quick"><button class="btn" id="btnBest">+ session best</button><button class="btn" id="btnOther" title="Your best lap on this track from a different session">+ best from another session</button><button class="btn" id="btnReset">Reset zoom</button></div>`;
  document.querySelectorAll("[data-rm]").forEach((b) => b.onclick = () => toggleLap(+b.dataset.rm));
  $("btnReset").onclick = () => zoom(null);
  $("btnOther").onclick = () => {
    const base = LAPS.find((l) => l.id === SEL[0].id);
    const other = LAPS.filter((l) => l.track === base.track && l.session !== base.session && l.car === "Player" && l.lap_time > 0 && !SEL.find((s) => s.id === l.id))
      .sort((a, b) => (b.valid - a.valid) || a.lap_time - b.lap_time)[0];
    if (other) toggleLap(other.id);
  };
  $("btnBest").onclick = () => {
    const sess = LAPS.find((l) => l.id === SEL[0].id)?.session;
    const best = LAPS.filter((l) => l.session === sess && l.lap_time > 0 && !SEL.find((s) => s.id === l.id)).sort((a, b) => a.lap_time - b.lap_time)[0];
    if (best) toggleLap(best.id);
  };
  $("legend").innerHTML = LAPSD.map((l) => `<span><i class="swatch" style="background:${colorOf(l)}"></i>${lapLabel(LAPS.find((x) => x.id === l.id) || {})}</span>`).join("");
}
function drawSummary() {
  const st = LAPSD.map(lapStats), metaOf = (l) => LAPS.find((x) => x.id === l.id) || {};
  const head = `<thead><tr><th></th>${LAPSD.map((l) => `<th><i class="swatch" style="background:${colorOf(l)}"></i>${lapLabel(metaOf(l))}</th>`).join("")}</tr></thead>`;
  const best = (vals, lower = true) => { const v = vals.filter((x) => x != null && isFinite(x)); return v.length > 1 ? (lower ? Math.min(...v) : Math.max(...v)) : null; };
  const row = (name, vals, f, lower = true) => {
    const b = best(vals, lower);
    return `<tr><td class="label">${name}</td>${vals.map((v) => `<td class="${b != null && v === b ? "p" : ""}">${v == null ? "-" : f(v)}</td>`).join("")}</tr>`;
  };
  const sec = (k) => LAPSD.map((l) => (l.sectors || metaOf(l).sectors || [])[k] / 1000 || null);
  $("summary").innerHTML = head + "<tbody>" +
    row("Lap time", LAPSD.map((l) => l.lap_time), fmt) +
    row("Gap", LAPSD.map((l) => l.lap_time - BASE.lap_time), (v) => v === 0 ? "-" : sgn(v)) +
    row("Sector 1", sec(0), (v) => v.toFixed(3)) + row("Sector 2", sec(1), (v) => v.toFixed(3)) + row("Sector 3", sec(2), (v) => v.toFixed(3)) +
    `<tr><td class="label">Tyre</td>${LAPSD.map((l) => `<td>${tyreBadge(l.compound || metaOf(l).compound, l.tyre_age ?? metaOf(l).tyre_age)}${(l.tyre_age ?? metaOf(l).tyre_age) != null ? ` <small>${l.tyre_age ?? metaOf(l).tyre_age} laps</small>` : ""}</td>`).join("")}</tr>` +
    row("Top speed", st.map((s) => s.top), (v) => v.toFixed(0) + " km/h", false) +
    row("Slowest corner", st.map((s) => s.low), (v) => v.toFixed(0) + " km/h", false) +
    row("Avg speed", st.map((s) => s.avg), (v) => v.toFixed(1) + " km/h", false) +
    row("Full throttle", st.map((s) => s.ft), (v) => v.toFixed(0) + "%", false) +
    row("On the brakes", st.map((s) => s.br), (v) => v.toFixed(0) + "%") +
    row("Coasting", st.map((s) => s.coast), (v) => v.toFixed(0) + " m") +
    row("Gear shifts", st.map((s) => s.shifts), (v) => v) +
    (LAPSD[0].trace.glat ? row("Peak lateral G", st.map((s) => s.glat), (v) => v.toFixed(2), false) : "") +
    `<tr><td class="label">Input</td>${LAPSD.map((l) => `<td>${esc(l.input || "ghost")}</td>`).join("")}</tr>` +
    `<tr><td class="label">Valid</td>${LAPSD.map((l) => `<td class="${l.valid === false ? "slower" : ""}">${l.valid === false ? "no" : "yes"}</td>`).join("")}</tr></tbody>`;
}
function drawMinis() {
  const L = X[X.length - 1];
  $("minis-note").textContent = LAPSD.length > 1 ? "each segment coloured by the fastest selected lap" : "segment times";
  $("minis").innerHTML = SEG.map((s, i) => {
    const times = LAPSD.map((l) => tAt(l, s.e) - tAt(l, s.s)), k = times.indexOf(Math.min(...times));
    const col = LAPSD.length > 1 ? colorOf(LAPSD[k]) : "#2a2e37";
    const gap = LAPSD.length > 1 ? Math.min(...times.filter((_, j) => j !== k)) - times[k] : null;
    const title = `${s.name}: ` + LAPSD.map((l, j) => `${lapLabel(LAPS.find((x) => x.id === l.id) || {})} ${times[j].toFixed(3)}s`).join(" · ");
    const tag = gap == null ? ` ${times[0].toFixed(2)}` : gap >= 0.005 ? ` −${gap.toFixed(2)}` : "";
    return `<div data-i="${i}" style="flex:${(s.e - s.s) / L};background:${gap != null && gap < 0.005 ? "#2a2e37" : col};color:${LAPSD.length > 1 && !(gap < 0.005) ? "#0b0c0f" : "var(--text-2)"}" title="${esc(title)}">${esc(s.name)}${tag}</div>`;
  }).join("");
  document.querySelectorAll("#minis div").forEach((el) => el.onclick = () => { const s = SEG[+el.dataset.i]; zoom([s.s, s.e]); });
}
function zoom(range) {
  syncing = true;
  plots.forEach((p) => p.setScale("x", range ? { min: range[0], max: range[1] } : { min: X[0], max: X[X.length - 1] }));
  syncing = false;
}
function bandsHook(u) {
  const { ctx, bbox } = u; ctx.save();
  SEG.forEach((s, i) => {
    const x0 = u.valToPos(s.s, "x", true), x1 = u.valToPos(s.e, "x", true);
    if (i % 2) { ctx.fillStyle = "rgba(255,255,255,0.022)"; ctx.fillRect(x0, bbox.top, x1 - x0, bbox.height); }
  });
  ctx.restore();
}
function labelsHook(u) {
  const { ctx, bbox } = u, dpr = devicePixelRatio || 1; ctx.save();
  ctx.fillStyle = "#a9adb6"; ctx.font = `600 ${11 * dpr}px Titillium Web, sans-serif`; ctx.textAlign = "center";
  SEG.forEach((s, i) => { const xa = u.valToPos(s.apex, "x", true); if (xa >= bbox.left && xa <= bbox.left + bbox.width) ctx.fillText(s.name, xa, bbox.top + (i % 2 ? 26 : 12) * dpr); });
  ctx.restore();
}
function drawCharts() {
  const w = () => Math.max(320, $("charts").clientWidth);
  const add = (title, series, data, h, extra = {}) => {
    const el = document.createElement("div"); el.className = "chart"; $("charts").appendChild(el);
    const t = document.createElement("div"); t.className = "ctitle"; t.textContent = title; el.appendChild(t);
    plots.push(new uPlot({
      width: w(), height: h, series: [{}, ...series], axes: axes(extra.yl, extra.splits), legend: { show: false },
      padding: [18, 8, 0, 0],
      scales: { x: { time: false }, y: extra.y || {} },
      cursor: { sync: { key: "tele", setSeries: false }, drag: { x: true, y: false }, points: { size: 7 } },
      hooks: { drawAxes: [bandsHook], ...(extra.labels ? { draw: [labelsHook] } : {}),
        setScale: [(u, k) => { if (k !== "x" || syncing) return; syncing = true; plots.forEach((o) => o !== u && o.setScale("x", { min: u.scales.x.min, max: u.scales.x.max })); syncing = false; }],
        setCursor: [(u) => cursorAt(u.cursor.idx)] },
    }, data, el));
  };
  const per = (k, f = (v) => v, step = false) => {
    const series = [], data = [X];
    for (const l of LAPSD) {
      if (!l.trace[k]) continue;
      data.push(l === BASE ? l.trace[k].map(f) : X.map((d) => f(step ? l.trace[k][nearestIdx(d, l.trace.dist)] : interp(d, l.trace.dist, l.trace[k]))));
      series.push({ stroke: colorOf(l), width: 1.6, points: { show: false }, ...(step ? { paths: uPlot.paths.stepped({ align: 1 }) } : {}) });
    }
    return [series, data];
  };
  if (LAPSD.length > 1) {
    const series = [{ stroke: "#3a3e47", width: 1 }], data = [X, X.map(() => 0)];
    for (const l of LAPSD) if (l !== BASE) { data.push(X.map((d, i) => tAt(l, d) - BASE.trace.t[i])); series.push({ stroke: colorOf(l), width: 2 }); }
    add("Gap to fastest (s)", series, data, 140, { yl: "s" });
  }
  let [s, d] = per("speed"); add("Speed", s, d, 240, { yl: "km/h", labels: true });
  [s, d] = per("throttle", (v) => v * 100); add("Throttle", s, d, 100, { yl: "%", y: { range: [0, 105] }, splits: [0, 50, 100] });
  [s, d] = per("brake", (v) => v * 100); add("Brake", s, d, 90, { yl: "%", y: { range: [0, 105] }, splits: [0, 50, 100] });
  [s, d] = per("gear", (v) => v, true); add("Gear", s, d, 110, { yl: "gear", y: { range: [0, 8.5] }, splits: [2, 4, 6, 8] });
  if (BASE.trace.rpm) { [s, d] = per("rpm"); add("RPM", s, d, 110, { yl: "rpm" }); }
  [s, d] = per("steer"); add("Steering", s, d, 100, { yl: "", y: { range: [-1.05, 1.05] }, splits: [-1, 0, 1] });
  if (BASE.trace.glat) { [s, d] = per("glat"); add("Lateral G", s, d, 100, { yl: "g" }); }
  if (BASE.trace.glon) { [s, d] = per("glon"); add("Longitudinal G", s, d, 100, { yl: "g" }); }
}
function drawCorners() {
  if (!SEG.length) { $("corners").innerHTML = '<tr><td class="muted">Corner detection needs a full lap.</td></tr>'; return; }
  const head = `<thead><tr><th>Corner</th>${LAPSD.map((l) => `<th><i class="swatch" style="background:${colorOf(l)}"></i>${l === BASE ? "time" : "Δ"}</th>`).join("")}<th>Apex km/h</th></tr></thead>`;
  const rows = SEG.map((sg, i) => {
    const st = LAPSD.map((l) => cornerStats(l, sg)), bt = st[LAPSD.indexOf(BASE)].time, fastest = Math.min(...st.map((x) => x.time));
    return `<tr data-i="${i}"><td class="label">${esc(sg.name)}</td>${st.map((x, j) => LAPSD[j] === BASE
      ? `<td class="${LAPSD.length > 1 && x.time === fastest ? "p" : ""}">${x.time.toFixed(2)}</td>`
      : `<td class="${x.time - bt < -0.01 ? "faster" : x.time - bt > 0.01 ? "slower" : "muted"}">${sgn(x.time - bt, 2)}</td>`).join("")}
      <td>${st.map((x) => x.vmin.toFixed(0)).join(" / ")}</td></tr>`;
  }).join("");
  $("corners").innerHTML = head + "<tbody>" + rows + "</tbody>";
  document.querySelectorAll("#corners tbody tr").forEach((tr) => tr.onclick = () => { const s = SEG[+tr.dataset.i]; zoom([s.s, s.e]); });
}
function drawNotes() {
  const r = BASE.report, m = LAPS.find((x) => x.id === BASE.id) || {};
  $("notes-for").textContent = r ? `${lapLabel(m)} vs ${r.ref_label}` : "";
  if (!r) { $("notes").innerHTML = '<p class="muted">No engineer analysis stored for this lap (reference or ghost laps are the yardstick).</p>'; return; }
  const losses = (r.losses || []).slice(0, 3).map((c) => `<b>${esc(c.corner)}</b> +${c.time_loss.toFixed(2)}s: ${esc((c.causes || []).slice(0, 2).join(", "))}`);
  const gains = (r.gains || []).slice(0, 2).map((c) => `<b>${esc(c.corner)}</b> ${c.time_loss.toFixed(2)}s`);
  $("notes").innerHTML = `<p>${fmt(r.lap_time)}, ${sgn(r.delta)} vs ${esc(r.ref_label)}${r.valid ? "" : ", deleted for track limits"}.</p>` +
    (losses.length ? `<p>Time lost: ${losses.join("; ")}.</p>` : "") + (gains.length ? `<p>Quicker in ${gains.join(", ")}.</p>` : "") +
    ((r.mistakes || []).length ? `<p>Mistakes: ${esc(r.mistakes.join(", "))}.</p>` : "");
}

// ---- track map: correct handedness (game z points down the screen) and rotated to fit the panel
let mapBase = null, mapGeo = null, MAPMODE = "speed";
document.querySelectorAll("#mapmode button").forEach((b) => b.onclick = () => {
  MAPMODE = b.dataset.m; document.querySelectorAll("#mapmode button").forEach((x) => x.classList.toggle("on", x === b)); drawMap(); cursorAt(null);
});
function mapTransform(t, w, h, pad) {
  // principal axis -> horizontal (rotation only, so the track is never mirrored)
  const n = t.x.length, mx = t.x.reduce((a, b) => a + b, 0) / n, mz = t.z.reduce((a, b) => a + b, 0) / n;
  let sxx = 0, szz = 0, sxz = 0;
  for (let i = 0; i < n; i++) { const dx = t.x[i] - mx, dz = t.z[i] - mz; sxx += dx * dx; szz += dz * dz; sxz += dx * dz; }
  const th = 0.5 * Math.atan2(2 * sxz, sxx - szz), c = Math.cos(-th), s = Math.sin(-th);
  const R = (x, z) => { const dx = x - mx, dz = z - mz; return [dx * c - dz * s, dx * s + dz * c]; };
  let minx = 1e9, maxx = -1e9, miny = 1e9, maxy = -1e9;
  for (let i = 0; i < n; i++) { const [a, b] = R(t.x[i], t.z[i]); minx = Math.min(minx, a); maxx = Math.max(maxx, a); miny = Math.min(miny, b); maxy = Math.max(maxy, b); }
  const sc = Math.min((w - 2 * pad) / (maxx - minx), (h - 2 * pad) / (maxy - miny));
  const ox = (w - (maxx - minx) * sc) / 2, oy = (h - (maxy - miny) * sc) / 2;
  return (x, z) => { const [a, b] = R(x, z); return [ox + (a - minx) * sc, oy + (b - miny) * sc]; };
}
function ramp(v) {   // one-hue sequential ramp (slow = deep blue, fast = pale)
  const stops = [[24, 79, 149], [57, 135, 229], [134, 182, 239], [205, 226, 251]], x = Math.max(0, Math.min(1, v)) * 3, i = Math.min(2, Math.floor(x)), f = x - i;
  const a = stops[i], b = stops[i + 1]; return `rgb(${a.map((c, k) => Math.round(c + (b[k] - c) * f)).join(",")})`;
}
function drawMap() {
  const c = $("map"); if (!BASE || !BASE.trace.x) { mapBase = null; return; }
  const { g, w, h } = fitCanvas(c, (c.clientWidth || 400) / 1.35), t = BASE.trace;
  const P = mapTransform(t, w, h, 26); mapGeo = { P };
  g.lineCap = "round"; g.lineJoin = "round";
  g.strokeStyle = "#262a32"; g.lineWidth = 12; g.beginPath(); t.x.forEach((x, i) => { const [a, b] = P(x, t.z[i]); i ? g.lineTo(a, b) : g.moveTo(a, b); }); g.stroke();
  let col, legend = "";
  if (MAPMODE === "gear") {
    col = (i) => ramp((Math.round(t.gear[i]) - 1) / 7);
    legend = "gear " + [1, 2, 3, 4, 5, 6, 7, 8].map((k) => `<span style="color:${ramp((k - 1) / 7)}">■</span>${k}`).join(" ");
  } else if (MAPMODE === "pedals") {
    col = (i) => t.brake[i] > 0.05 ? "#e10600" : t.throttle[i] > 0.98 ? "#2fd17a" : t.throttle[i] > 0.05 ? "#f2c94c" : "#6f7480";
    legend = '<span style="color:#2fd17a">■</span> full throttle &nbsp;<span style="color:#f2c94c">■</span> partial &nbsp;<span style="color:#e10600">■</span> braking &nbsp;<span style="color:#6f7480">■</span> coasting';
  } else if (MAPMODE === "fastest" && LAPSD.length > 1) {
    const win = 25;   // ~100 m windows: who gains time here
    const dl = LAPSD.map((l) => X.map((d) => tAt(l, d)));
    col = (i) => { const a = Math.max(0, i - win), b = Math.min(X.length - 1, i + win); let k = 0, bv = 1e9;
      dl.forEach((arr, j) => { const v = arr[b] - arr[a]; if (v < bv) { bv = v; k = j; } }); return colorOf(LAPSD[k]); };
    legend = "colour = the selected lap that is fastest through each part of the track";
  } else {
    const lo = Math.min(...t.speed), hi = Math.max(...t.speed);
    col = (i) => ramp((t.speed[i] - lo) / (hi - lo));
    legend = `speed: <span style="color:${ramp(0)}">■</span> ${lo.toFixed(0)} → <span style="color:${ramp(1)}">■</span> ${hi.toFixed(0)} km/h`;
  }
  g.lineWidth = 5;
  for (let i = 1; i < t.x.length; i++) { g.strokeStyle = col(i); g.beginPath(); g.moveTo(...P(t.x[i - 1], t.z[i - 1])); g.lineTo(...P(t.x[i], t.z[i])); g.stroke(); }
  const [sx, sy] = P(t.x[0], t.z[0]); g.fillStyle = "#f0efec"; g.fillRect(sx - 7, sy - 2, 14, 4);
  const [ax, ay] = P(t.x[6], t.z[6]); g.strokeStyle = "#f0efec"; g.lineWidth = 2; g.beginPath(); g.moveTo(sx, sy); g.lineTo(ax, ay); g.stroke();   // direction tick
  g.fillStyle = "#a9adb6"; g.textAlign = "center"; g.font = "600 11px Titillium Web, sans-serif";
  SEG.forEach((s) => { const i = nearestIdx(s.apex, X), [a, b] = P(t.x[i], t.z[i]); g.fillText(s.name, a, b - 11); });
  $("maplegend").innerHTML = legend;
  mapBase = g.getImageData(0, 0, c.width, c.height);
}
function cursorAt(idx) {
  const c = $("map"), g = c.getContext("2d");
  if (mapBase) { g.setTransform(1, 0, 0, 1, 0, 0); g.putImageData(mapBase, 0, 0); g.setTransform(devicePixelRatio || 1, 0, 0, devicePixelRatio || 1, 0, 0); }
  if (idx == null || !BASE || !mapGeo) { $("readout").innerHTML = '<div style="grid-column:1/-1"><small>Hover the charts to scrub the lap</small></div>'; return; }
  const d = X[idx];
  LAPSD.forEach((l) => {
    if (!l.trace.x) return; const [a, b] = mapGeo.P(interp(d, l.trace.dist, l.trace.x), interp(d, l.trace.dist, l.trace.z));
    g.fillStyle = colorOf(l); g.strokeStyle = "#0d0f13"; g.lineWidth = 2; g.beginPath(); g.arc(a, b, 6, 0, 7); g.fill(); g.stroke();
  });
  const at = (l, k) => l.trace[k][nearestIdx(d, l.trace.dist)];
  const corner = SEG.find((s) => d >= s.s && d < s.e);
  const vals = (f) => LAPSD.map((l) => `<span style="color:${colorOf(l)}">${f(l)}</span>`).join(" ");
  $("readout").innerHTML = `<div><small>Distance</small><b>${d.toFixed(0)} m</b></div><div><small>Corner</small><b>${esc(corner ? corner.name : "-")}</b></div>
    <div><small>Speed</small><b>${vals((l) => at(l, "speed").toFixed(0))}</b></div><div><small>Gear</small><b>${vals((l) => Math.round(at(l, "gear")))}</b></div>
    <div><small>Thr / Brk</small><b>${vals((l) => `${(at(l, "throttle") * 100).toFixed(0)}/${(at(l, "brake") * 100).toFixed(0)}`)}</b></div>
    <div><small>Gap</small><b>${vals((l) => l === BASE ? "0.00" : sgn(tAt(l, d) - BASE.trace.t[idx], 2))}</b></div>`;
}
function drawGG() {
  const c = $("gg"), { g, w, h } = fitCanvas(c, c.clientWidth || 380), R = Math.min(w, h) / 2 - 16, cx = w / 2, cy = h / 2;
  const laps = LAPSD.filter((l) => l.trace.glat && l.trace.glon);
  if (!laps.length) { g.fillStyle = "#6f7480"; g.textAlign = "center"; g.fillText("no G-force data (ghost laps)", cx, cy); return; }
  const all = laps.flatMap((l) => [...l.trace.glat.map(Math.abs), ...l.trace.glon.map(Math.abs)]).sort((a, b) => a - b);
  const m = Math.ceil(Math.max(2, all[Math.floor(all.length * 0.995)])), S = R / m;
  g.strokeStyle = GRID; g.fillStyle = AXIS; g.textAlign = "left";
  for (let k = 1; k <= m; k++) { g.beginPath(); g.arc(cx, cy, k * S, 0, 7); g.stroke(); g.fillText(k + "g", cx + k * S * 0.71 + 3, cy - k * S * 0.71); }
  g.beginPath(); g.moveTo(cx - R, cy); g.lineTo(cx + R, cy); g.moveTo(cx, cy - R); g.lineTo(cx, cy + R); g.stroke();
  g.textAlign = "center"; g.fillText("braking", cx, cy + R + 9); g.fillText("acceleration", cx, cy - R - 8);
  laps.forEach((l) => { g.fillStyle = colorOf(l) + "55";
    for (let i = 0; i < l.trace.glat.length; i++) { const a = l.trace.glat[i], b = l.trace.glon[i]; if (Math.abs(a) <= m && Math.abs(b) <= m) g.fillRect(cx + a * S - 1, cy - b * S - 1, 2.2, 2.2); } });
}
function resizeAll() { if (!SEL.length) return; const w = Math.max(320, $("charts").clientWidth); plots.forEach((p) => p.setSize({ width: w, height: p.height })); drawMap(); cursorAt(null); drawGG(); }
window.addEventListener("resize", () => { resizeAll(); if (!$("tab-timing").classList.contains("hidden")) drawTiming(); });

// ---------------------------------------------------------------- timing
function fillSessionSelects() {
  const opt = (s) => `<option value="${esc(s.id)}">${esc(s.track)} · ${esc(s.type)} · ${sessDate(s.created)}</option>`;
  const ts = $("timing-session"), rs = $("race-session"), curT = ts.value, curR = rs.value;
  ts.innerHTML = SESSIONS.map(opt).join(""); if (curT) ts.value = curT;
  const races = SESSIONS.filter((s) => (s.type || "").startsWith("Race"));
  rs.innerHTML = races.map(opt).join(""); if (curR) rs.value = curR;
}
$("timing-session").onchange = () => drawTiming();
$("race-session").onchange = () => drawRace();
async function drawTiming() {
  const s = SESSIONS.find((x) => x.id === $("timing-session").value) || SESSIONS[0]; if (!s) return;
  const laps = s.laps.filter((l) => l.car === "Player" && l.lap_time > 0).sort((a, b) => a.lap - b.lap || a.id - b.id);
  const sc = sectorClasses(laps), best = Math.min(...laps.map((l) => l.lap_time));
  const withS = laps.filter((l) => l.sectors), bestS = [0, 1, 2].map((k) => Math.min(...withS.map((l) => l.sectors[k])));
  $("timing").innerHTML = `<thead><tr><th>Lap</th><th>Time</th><th>Gap</th><th>S1</th><th>S2</th><th>S3</th><th>Tyre</th><th>Input</th><th>Status</th></tr></thead><tbody>` +
    laps.map((l) => `<tr data-id="${l.id}" style="cursor:pointer"><td class="lapn">L${l.lap}</td>
      <td class="${l.lap_time === best ? "p" : ""}">${fmt(l.lap_time)}</td><td class="muted">${l.lap_time === best ? "-" : sgn(l.lap_time - best)}</td>
      ${[0, 1, 2].map((k) => `<td class="${sc.get(l.id)?.[k] || "muted"}">${l.sectors ? (l.sectors[k] / 1000).toFixed(3) : "-"}</td>`).join("")}
      <td>${tyreBadge(l.compound, l.tyre_age)}${l.tyre_age != null ? ` <small>${l.tyre_age}</small>` : ""}</td>
      <td class="muted">${esc(l.input || "-")}</td><td class="${l.valid ? "" : "slower"}">${l.valid ? "valid" : "deleted"}</td></tr>`).join("") +
    (withS.length ? `<tr class="ideal"><td>Ideal</td><td>${fmt(bestS.reduce((a, b) => a + b, 0) / 1000)}</td><td class="muted">${sgn(bestS.reduce((a, b) => a + b, 0) / 1000 - best)}</td>${bestS.map((v) => `<td>${(v / 1000).toFixed(3)}</td>`).join("")}<td colspan="3" class="muted" style="text-align:left;font:400 12px var(--font)">best three sectors combined</td></tr>` : "") + "</tbody>";
  document.querySelectorAll("#timing tbody tr[data-id]").forEach((tr) => tr.onclick = () => { SEL = []; toggleLap(+tr.dataset.id); document.querySelector('#tabs button[data-tab="telemetry"]').click(); });
  // lap-time progression, markers show the tyre
  { const { g, w, h } = fitCanvas($("c-laps")), pad = 52;
    if (laps.length) {
      const ts = laps.map((l) => l.lap_time), lo = Math.min(...ts) - 0.4, hi = Math.min(Math.max(...ts), Math.min(...ts) * 1.08) + 0.4;
      const Xp = (i) => pad + (laps.length === 1 ? (w - pad) / 2 : i * (w - pad - 24) / (laps.length - 1)), Y = (v) => 14 + (Math.min(v, hi) - lo) / (hi - lo) * (h - 46);
      g.strokeStyle = GRID; g.fillStyle = AXIS; g.textAlign = "right";
      for (let k = 0; k <= 3; k++) { const v = lo + (hi - lo) * k / 3; g.beginPath(); g.moveTo(pad, Y(v)); g.lineTo(w, Y(v)); g.stroke(); g.fillText(fmt(v), pad - 6, Y(v)); }
      g.strokeStyle = "#6f7480"; g.lineWidth = 1.5; g.beginPath(); laps.forEach((l, i) => i ? g.lineTo(Xp(i), Y(l.lap_time)) : g.moveTo(Xp(i), Y(l.lap_time))); g.stroke();
      const tc = { soft: "#e10600", medium: "#f5c518", hard: "#e8e8e8", inter: "#3fb950", wet: "#2f80ed" };
      laps.forEach((l, i) => {
        g.fillStyle = "#0b0c0f"; g.strokeStyle = tc[(l.compound || "").toLowerCase()] || "#6f7480"; g.lineWidth = 3;
        g.beginPath(); g.arc(Xp(i), Y(l.lap_time), 6, 0, 7); g.fill(); g.stroke();
        g.fillStyle = l.lap_time === best ? "#b26bff" : AXIS; g.textAlign = "center"; g.fillText("L" + l.lap + (l.valid ? "" : "✗"), Xp(i), h - 12);
      });
    } else { g.fillStyle = AXIS; g.fillText("no laps", 10, 20); }
  }
  // mini-sectors: every lap's segment times, F1 colours
  const tbl = $("minisectors");
  if (laps.length < 1) { tbl.innerHTML = ""; return; }
  tbl.innerHTML = '<tbody><tr><td class="muted">Loading laps…</td></tr></tbody>';
  const det = await Promise.all(laps.map((l) => getLap(l.id)));
  const base = det.reduce((b, l) => !b || l.lap_time < b.lap_time ? l : b, null), segs = segments(base);
  if (!segs.length) { tbl.innerHTML = '<tbody><tr><td class="muted">Needs a full lap with corners.</td></tr></tbody>'; return; }
  const times = det.map((l) => segs.map((sg) => tAt(l, sg.e) - tAt(l, sg.s)));
  const bestSeg = segs.map((_, j) => Math.min(...times.map((r) => r[j])));
  const run = segs.map(() => 1e9);
  tbl.innerHTML = `<thead><tr><th>Lap</th>${segs.map((s) => `<th>${esc(s.name)}</th>`).join("")}</tr></thead><tbody>` +
    det.map((l, i) => `<tr><td class="lapn">L${laps[i].lap}</td>${times[i].map((v, j) => { const c = v === bestSeg[j] ? "p" : v < run[j] ? "g" : "y"; run[j] = Math.min(run[j], v); return `<td class="${c}">${v.toFixed(2)}</td>`; }).join("")}</tr>`).join("") +
    `<tr class="ideal"><td>Best</td>${bestSeg.map((v) => `<td class="p">${v.toFixed(2)}</td>`).join("")}</tr></tbody>`;
  $("ms-note").textContent = `ideal lap from best mini-sectors: ${fmt(bestSeg.reduce((a, b) => a + b, 0))}`;
}

// ---------------------------------------------------------------- race
let racePlots = [];
async function drawRace() {
  racePlots.forEach((p) => p.destroy()); racePlots = [];
  ["r-pos", "r-gap", "r-laps", "r-stints", "r-wear"].forEach((id) => $(id).innerHTML = "");
  const id = $("race-session").value, rows = id ? await (await fetch("/api/race/" + encodeURIComponent(id))).json() : [];
  $("race-empty").classList.toggle("hidden", rows.length > 0); $("race-content").classList.toggle("hidden", !rows.length);
  if (!rows.length) return;
  const laps = rows.map((r) => r.lap), w = () => Math.max(320, $("r-pos").clientWidth);
  const ax = (yl) => [{ stroke: AXIS, grid: { stroke: GRID }, ticks: { stroke: GRID }, font: "11px Titillium Web", values: (u, v) => v.map((x) => "L" + x) },
    { stroke: AXIS, grid: { stroke: GRID }, ticks: { stroke: GRID }, font: "11px Titillium Web", size: 46, label: yl, labelSize: 14 }];
  const mk = (el, series, data, h, y = {}, yl = "") => racePlots.push(new uPlot({ width: w(), height: h, legend: { show: false },
    series: [{}, ...series], axes: ax(yl), scales: { x: { time: false }, y }, cursor: { sync: { key: "race" } } }, data, $(el)));
  const maxPos = Math.max(...rows.map((r) => r.position || 1));
  mk("r-pos", [{ stroke: css("--lap1"), width: 2, points: { show: true, size: 6 } }], [laps, rows.map((r) => r.position)], 220, { range: [0.5, maxPos + 0.5], dir: -1 }, "position");
  mk("r-gap", [{ stroke: css("--lap1"), width: 2 }, { stroke: css("--lap2"), width: 2 }],
    [laps, rows.map((r) => r.ahead?.gap_s ?? null), rows.map((r) => r.behind?.gap_s ?? null)], 220, {}, "s");
  $("r-gap").insertAdjacentHTML("beforeend", `<div class="legend" style="margin-top:6px"><span><i class="swatch" style="background:${css("--lap1")}"></i>car ahead</span><span><i class="swatch" style="background:${css("--lap2")}"></i>car behind</span></div>`);
  mk("r-laps", [{ stroke: "#a9adb6", width: 1.5, points: { show: true, size: 7, stroke: (u, si) => "#a9adb6" } }],
    [laps, rows.map((r) => r.lap_time > 0 ? r.lap_time : null)], 220, {}, "lap time s");
  // stints: one block per lap, tyre colour + letter at each stint start
  const tc = { soft: "#e10600", medium: "#f5c518", hard: "#e8e8e8", inter: "#3fb950", wet: "#2f80ed" };
  let prev = null;
  $("r-stints").innerHTML = `<div style="display:flex;gap:2px;height:26px;margin-bottom:10px">` + rows.map((r) => {
    const c = (r.tyres?.compound || "").toLowerCase(), start = c !== prev; prev = c;
    return `<div title="L${r.lap}: ${esc(c || "?")}, ${r.tyres?.age_laps ?? "?"} laps" style="flex:1;border-radius:3px;background:${tc[c] || "#2a2e37"};display:grid;place-items:center;font:800 10px var(--num);color:#0b0c0f">${start ? (TYRE[c] || "?") : ""}</div>`;
  }).join("") + "</div>";
  const wear = rows.map((r) => r.tyres?.wear_pct ? Math.max(...r.tyres.wear_pct) : null);
  const el = document.createElement("div"); $("r-wear").appendChild(el);
  racePlots.push(new uPlot({ width: w(), height: 160, legend: { show: false }, series: [{}, { stroke: css("--lap2"), width: 2 }],
    axes: ax("max wear %"), scales: { x: { time: false }, y: { range: [0, 100] } }, cursor: { sync: { key: "race" } } }, [laps, wear], el));
}

// ---------------------------------------------------------------- boot
fetch("/api/radio").then((r) => r.json()).then((rows) => rows.forEach(addRadio)).catch(() => {});
connect();
(async () => {
  let st = {}; try { st = await (await fetch("/api/state")).json(); renderState(st); } catch {}
  await loadLaps();
  const [tab, qs] = (location.hash.slice(1) || (st.offline ? "telemetry" : "live")).split("?");
  const q = new URLSearchParams(qs || "");
  showTab(TABS.includes(tab) ? tab : "telemetry");
  const ids = (q.get("laps") || "").split(",").filter(Boolean).map(Number);
  if (ids.length) { for (const id of ids) await toggleLap(id); }
  else if (LAPS.length) {
    const s = SESSIONS[0], p = s.laps.filter((l) => l.car === "Player" && l.lap_time > 0).sort((a, b) => a.lap_time - b.lap_time);
    if (p[0]) await toggleLap(p[0].id);
    if (p[1]) await toggleLap(p[1].id);
  }
})();
