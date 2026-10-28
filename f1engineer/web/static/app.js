const $ = (id) => document.getElementById(id);
const fmt = (s) => { if (!s) return "-"; const m = Math.floor(s / 60); const r = (s - m * 60).toFixed(3).padStart(6, "0"); return m ? `${m}:${r}` : r; };
const sgn = (v, d = 3) => (v > 0 ? "+" : v < 0 ? "−" : "±") + Math.abs(v).toFixed(d);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
const COL = { A: "#00d2ff", B: "#ff8700", grid: "#262a33", axis: "#858c99", delta: "#b14cff" };
let OFFLINE = true;

// ================================================================ tabs
function showTab(t) {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
  ["review", "live", "radio"].forEach((k) => $("tab-" + k).classList.toggle("hidden", k !== t));
  if (t === "review") { loadLaps(); requestAnimationFrame(resizeCharts); }
}
document.querySelectorAll("nav button").forEach((b) => b.onclick = () => { history.replaceState(null, "", "#" + b.dataset.tab); showTab(b.dataset.tab); });

// ================================================================ live
let lastReportKey = null;
function tempColor(t) { return t < 75 ? "#2b4f7a" : t < 90 ? "#2b6f55" : t < 105 ? "#3c7a2b" : t < 115 ? "#7a6a2b" : "#7a2b2b"; }
let wasImporting = false;
function renderState(s) {
  OFFLINE = !!s.offline;
  $("importing").classList.toggle("hidden", !s.importing);
  if (s.importing) $("importing").textContent = `Importing recording ${s.importing}…`;
  if (wasImporting && !s.importing) loadLaps();
  wasImporting = !!s.importing;
  $("dot").classList.toggle("on", !!s.connected);
  $("conn").textContent = s.offline ? "review mode" : s.connected ? "live" : "waiting for game";
  $("liveoff").classList.toggle("hidden", !s.offline);
  if (s.offline) return;   // review mode: the header shows the reviewed lap instead
  $("h-game").textContent = s.game || "-";
  $("h-track").textContent = s.track || "-"; $("h-mode").textContent = s.mode || "-";
  $("h-input").textContent = s.input_device || "-";
  $("h-ref").textContent = s.reference ? `${s.reference.label} ${fmt(s.reference.time)}` : "-";
  if (s.lap) {
    const d = s.lap.delta;
    $("delta").textContent = d === undefined ? "-" : (d > 0 ? "+" : "") + d.toFixed(3);
    $("delta").className = "big " + (d === undefined ? "" : d > 0 ? "neg" : "pos");
    $("lapinfo").textContent = `Lap ${s.lap.num} · ${fmt(s.lap.time)} · last ${fmt(s.lap.last)} · best ${fmt(s.lap.best)}${s.lap.invalid ? " · INVALID" : ""} · P${s.lap.position}`;
  }
  if (s.car) {
    $("speed").textContent = s.car.speed; $("gear").textContent = s.car.gear <= 0 ? (s.car.gear < 0 ? "R" : "N") : s.car.gear;
    $("rev").style.width = Math.min(100, s.car.rpm / 125) + "%";
    $("thr").style.width = s.car.throttle * 100 + "%"; $("brk").style.width = s.car.brake * 100 + "%";
    const names = ["RL", "RR", "FL", "FR"], order = [2, 3, 0, 1];
    const wear = (s.status && s.status.tyre_wear) || [];
    $("tyres").innerHTML = order.map((i) => `<div class="tyre" style="background:${tempColor(s.car.tyre_surface[i])}">${names[i]} <b>${s.car.tyre_surface[i]}°</b><small>inner ${s.car.tyre_inner[i]}° · wear ${wear[i] ?? "-"}%</small></div>`).join("");
  }
  if (s.status) {
    $("compound").textContent = `${s.status.compound} · ${s.status.tyre_age} laps`;
    $("fuel").textContent = s.status.fuel_laps > 0 ? "+" + s.status.fuel_laps : s.status.fuel_laps;
    $("ers").textContent = s.status.ers_pct; $("tc").textContent = ["off", "medium", "full"][s.status.tc] ?? "-";
    $("abs").textContent = s.status.abs ? "on" : "off";
  }
  if (s.race && s.race.position) {
    const r = s.race;
    $("racebox").innerHTML = `P${r.position} · ahead ${r.ahead ? esc(r.ahead.name) + " " + (r.ahead.gap_s ?? "-") + "s" : "-"} · behind ${r.behind ? esc(r.behind.name) + " " + (r.behind.gap_s ?? "-") + "s" : "-"}` +
      (r.calls && r.calls.length ? `<div style="color:var(--yellow)">${r.calls.map(esc).join("<br>")}</div>` : "");
  }
  const rep = s.last_report;
  if (rep && rep.lap_num + ":" + rep.lap_time !== lastReportKey) {
    lastReportKey = rep.lap_num + ":" + rep.lap_time;
    $("report").innerHTML = reportTable(rep);
  }
  if (s.advice && s.advice.length) $("advice").innerHTML = s.advice.map((a) => `<li><b>${esc(a.change)}</b><br><span class="muted">${esc(a.why)}</span></li>`).join("");
}
function reportTable(rep) {
  const rows = rep.corners.map((c) => `<tr><td>${esc(c.corner)}</td><td class="${c.time_loss > 0.02 ? "neg" : c.time_loss < -0.02 ? "pos" : ""}">${c.time_loss > 0 ? "+" : ""}${c.time_loss.toFixed(3)}</td><td>${c.min_speed}/${c.ref_min_speed}</td><td class="muted">${esc(c.causes.slice(0, 3).join("; "))}</td></tr>`).join("");
  return `<div class="muted">Lap ${rep.lap_num} ${fmt(rep.lap_time)} vs ${esc(rep.ref_label)} ${fmt(rep.ref_time)} (${rep.delta > 0 ? "+" : ""}${rep.delta.toFixed(3)}) · ${esc(rep.input_device)}${rep.valid ? "" : " · invalid"}</div>
    <table><thead><tr><th>Corner</th><th>Δ s</th><th>Apex km/h</th><th>Why</th></tr></thead><tbody>${rows}</tbody></table>`;
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
    if (j.event && j.event.type === "lap" && !$("tab-review").classList.contains("hidden")) loadLaps();
  };
  ws.onclose = () => { $("dot").classList.remove("on"); $("conn").textContent = "reconnecting"; setTimeout(connect, 1500); };
}
$("chatform").onsubmit = async (e) => {
  e.preventDefault(); const q = $("q").value.trim(); if (!q) return; $("q").value = "";
  if (OFFLINE) addRadio({ role: "driver", text: q });
  const thinking = document.createElement("div"); thinking.className = "msg thinking"; thinking.textContent = "…thinking";
  $("radio-all").appendChild(thinking); $("radio-all").scrollTop = $("radio-all").scrollHeight;
  try {
    const r = await (await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }) })).json();
    if (OFFLINE) addRadio({ role: "engineer", text: r.answer });
  } catch (err) { addRadio({ role: "engineer", text: "Radio's down: " + err }); }
  finally { thinking.remove(); }
};

// ================================================================ review: helpers
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
const onA = (lap, k, x) => x.map((d) => interp(d, lap.trace.dist, lap.trace[k]));            // lap channel on A's grid
const onAstep = (lap, k, x) => x.map((d) => lap.trace[k][nearestIdx(d, lap.trace.dist)]);   // for gear (no blending)
const tAt = (lap, d) => interp(d, lap.trace.dist, lap.trace.t);
const lapName = (l) => `${l.car === "Player" ? "Lap " + l.lap : esc(l.car)}`;

// ================================================================ review: lap list
let LAPS = [], A = null, B = null, cache = new Map();
async function loadLaps() {
  try { LAPS = await (await fetch("/api/laps")).json(); } catch { return; }
  renderLapList();
}
function bestOf(sess) { const v = LAPS.filter((l) => l.session === sess && l.lap_time > 0); return v.reduce((b, l) => (!b || l.lap_time < b.lap_time ? l : b), null); }
function sectorColours() {
  const out = new Map(), bySess = new Map();
  for (const l of LAPS) if (l.sectors && l.car === "Player") { if (!bySess.has(l.session)) bySess.set(l.session, []); bySess.get(l.session).push(l); }
  for (const laps of bySess.values()) {
    laps.sort((a, b) => a.lap - b.lap || a.id - b.id);
    const best = [0, 1, 2].map((k) => Math.min(...laps.map((l) => l.sectors[k] || 1e9)));
    const run = [1e9, 1e9, 1e9];
    for (const l of laps) out.set(l.id, l.sectors.map((v, k) => { const c = v === best[k] ? "p" : v <= run[k] ? "g" : "y"; run[k] = Math.min(run[k], v); return c; }));
  }
  return out;
}
function renderLapList() {
  const SC = sectorColours();
  const groups = new Map();
  for (const l of LAPS) { if (!groups.has(l.session)) groups.set(l.session, []); groups.get(l.session).push(l); }
  if (!LAPS.length) { $("lapgroups").innerHTML = '<div class="muted">No laps stored yet.</div>'; return; }
  $("lapgroups").innerHTML = [...groups.entries()].map(([sid, laps]) => {
    const f = laps[0], best = bestOf(sid), when = f.created ? new Date(f.created * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
    laps = [...laps].sort((a, b) => (a.car === "Player") - (b.car === "Player") || a.lap - b.lap).sort((a, b) => (b.car === "Player") - (a.car === "Player"));
    return `<div class="sess"><div class="sess-title"><b>${esc(f.track || "?")}</b> · ${esc(f.type || "")} <span style="float:right">${when}</span></div>` +
      laps.map((l) => {
        const isGhost = l.car !== "Player";
        const cls = ["laprow", A && A.id === l.id ? "selA" : "", B && B.id === l.id ? "selB" : ""].join(" ");
        const inp = l.input === "keyboard" ? "⌨ kb" : l.input && l.input.startsWith("controller") ? "🎮 pad" : "";
        return `<div class="${cls}" data-id="${l.id}" title="${isGhost ? esc(l.car) + " ghost (rebuilt from position data)" : ""}">
          <span class="${A && A.id === l.id ? "cA" : "cB"}">${A && A.id === l.id ? "A" : B && B.id === l.id ? "B" : ""}</span>
          <span class="ln">${isGhost ? '<span class="ghost">PB</span>' : "L" + l.lap}</span>
          <span class="lt ${best && best.id === l.id ? "best" : ""} ${l.valid ? "" : "inv"}">${fmt(l.lap_time)}</span>
          <span class="meta">${SC.has(l.id) ? `<span class="secs" title="S1 ${(l.sectors[0] / 1000).toFixed(3)} · S2 ${(l.sectors[1] / 1000).toFixed(3)} · S3 ${(l.sectors[2] / 1000).toFixed(3)}">${SC.get(l.id).map((c) => `<span class="${c}"></span>`).join("")}</span>` : ""}${l.valid ? "" : "invalid · "}${inp}</span>
          <button class="bbtn" data-b="${l.id}" title="Compare as lap B">B</button></div>`;
      }).join("") + "</div>";
  }).join("");
  document.querySelectorAll(".laprow").forEach((row) => row.onclick = (e) => {
    const id = +row.dataset.id;
    if (e.target.dataset.b || e.shiftKey) setLaps(A ? A.id : id, A ? id : null); else setLaps(id, B && B.id !== id ? B.id : null);
  });
}
async function getLap(id) {
  if (!cache.has(id)) { const l = await (await fetch("/api/lap/" + id)).json(); l.id = id; l.corners = l.corners || []; cache.set(id, l); }
  return cache.get(id);
}
async function setLaps(a, b) {
  if (b != null && (!B || B.id !== b) && MAPMODE === "speed") document.querySelector('#mapmode button[data-m="delta"]').click();
  A = a != null ? await getLap(a) : null; B = b != null && b !== a ? await getLap(b) : null;
  history.replaceState(null, "", `#review?a=${A ? A.id : ""}${B ? "&b=" + B.id : ""}`);
  renderLapList(); drawAll();
}
$("btnSwap").onclick = () => B && setLaps(B.id, A.id);
$("btnClearB").onclick = () => A && setLaps(A.id, null);
$("btnBest").onclick = () => {
  if (!A) return; const best = bestOf(A.session || LAPS.find((l) => l.id === A.id)?.session);
  if (best && best.id !== A.id) setLaps(A.id, best.id);
  else { const other = LAPS.filter((l) => l.id !== A.id && l.lap_time > 0).sort((x, y) => x.lap_time - y.lap_time)[0]; if (other) setLaps(A.id, other.id); }
};
$("btnReset").onclick = () => zoom(null);

// ================================================================ review: analysis
function segments(lap) {   // mini-sectors: split the lap halfway between consecutive apexes
  const c = lap.corners, L = lap.track_length || lap.trace.dist[lap.trace.dist.length - 1];
  if (!c.length) return [];
  const b = [0, ...c.slice(1).map((x, i) => (c[i].apex + x.apex) / 2), L];
  const seen = {};
  return c.map((x, i) => { seen[x.name] = (seen[x.name] || 0) + 1; return { name: x.name + (seen[x.name] > 1 ? " " + seen[x.name] : ""), apex: x.apex, s: b[i], e: b[i + 1] }; });
}
function cornerStats(lap, seg) {
  const t = lap.trace, d = t.dist;
  let i0 = nearestIdx(Math.max(seg.s, seg.apex - 120), d), i1 = nearestIdx(Math.min(seg.e, seg.apex + 120), d), mi = i0;
  for (let i = i0; i <= i1; i++) if (t.speed[i] < t.speed[mi]) mi = i;
  const apexD = d[mi];
  let brake = null, kind = "";
  for (let i = nearestIdx(seg.s, d); i <= mi; i++) { if (t.brake[i] > 0.1) { brake = apexD - d[i]; kind = "brake"; break; } }
  if (brake === null) for (let i = nearestIdx(seg.s, d); i <= mi; i++) { if (t.throttle[i] < 0.5) { brake = apexD - d[i]; kind = "lift"; break; } }
  let thr = null;
  for (let i = mi, end = nearestIdx(seg.e, d); i <= end; i++) { if (t.throttle[i] > 0.95) { thr = d[i] - apexD; break; } }
  return { vmin: t.speed[mi], gear: t.gear[mi], brake, kind, thr, time: tAt(lap, seg.e) - tAt(lap, seg.s) };
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
  const L = t.dist[n - 1];
  return { time: lap.lap_time, top, low, avg: L / lap.lap_time * 3.6, ft: ft / n * 100, br: br / n * 100, coast, shifts, glat };
}

// ================================================================ review: drawing
let plots = [], syncing = false, X = null, SEG = [];
function zoom(range) {
  syncing = true;
  plots.forEach((p) => p.setScale("x", range ? { min: range[0], max: range[1] } : { min: X[0], max: X[X.length - 1] }));
  syncing = false;
}
function drawAll() {
  $("empty").classList.toggle("hidden", !!A); $("content").classList.toggle("hidden", !A);
  chipFill(); plots.forEach((p) => p.destroy()); plots = []; $("charts").innerHTML = "";
  if (!A) return;
  X = A.trace.dist; SEG = segments(A);
  if (OFFLINE) { $("h-track").textContent = A.track || "-"; $("h-input").textContent = A.input || "ghost"; $("h-mode").textContent = LAPS.find((l) => l.id === A.id)?.type || "-"; }
  drawStats(); drawMinis(); drawCharts(); drawCornerTable(); drawNotes(); drawMap(); cursorAt(null); drawAnalysis();
}
function chipFill() {
  const chip = (l) => l ? `<b>${fmt(l.lap_time)}</b> &nbsp;${lapName(l)} · ${esc(l.input || "ghost")}${l.valid === false ? " · invalid" : ""}` : null;
  $("chipA").querySelector(".nm").innerHTML = chip(A) || "pick a lap";
  $("chipB").querySelector(".nm").innerHTML = chip(B) || "compare with...";
  if (A && B) { const g = B.lap_time - A.lap_time; $("gap").innerHTML = `<span class="${g > 0 ? "cA" : "cB"}">${sgn(g)}</span>`; $("gap").title = g > 0 ? "A is faster" : "B is faster"; }
  else $("gap").textContent = "";
}
function drawStats() {
  const a = lapStats(A), b = B ? lapStats(B) : null;
  const row = (k, f, better) => {
    const va = f(a), vb = b ? f(b) : null;
    return `<div class="stat"><div class="k">${k}</div><div class="v"><span class="${b ? "a" : ""}">${va}</span>${b ? `<span class="b">${vb}</span>` : ""}</div></div>`;
  };
  $("stats").innerHTML = [
    row("Lap time", (s) => fmt(s.time)), row("Top speed", (s) => s.top.toFixed(0) + " km/h"), row("Slowest corner", (s) => s.low.toFixed(0) + " km/h"),
    row("Avg speed", (s) => s.avg.toFixed(1)), row("Full throttle", (s) => s.ft.toFixed(0) + "%"), row("On the brakes", (s) => s.br.toFixed(0) + "%"),
    row("Coasting", (s) => s.coast.toFixed(0) + " m"), row("Gear shifts", (s) => s.shifts), ...(A.trace.glat ? [row("Max lateral G", (s) => s.glat.toFixed(2))] : []),
  ].join("");
}
function drawMinis() {
  const L = X[X.length - 1];
  $("minis").innerHTML = SEG.map((s, i) => {
    const ta = tAt(A, s.e) - tAt(A, s.s);
    let bg = "#3a3f4a", label = `${esc(s.name)} ${ta.toFixed(2)}`, title = `${s.name}: A ${ta.toFixed(3)}s`;
    if (B) {
      const tb = tAt(B, s.e) - tAt(B, s.s), d = tb - ta;
      bg = Math.abs(d) < 0.01 ? "#c9ccd1" : d > 0 ? COL.A : COL.B;
      label = Math.abs(d) < 0.01 ? `${esc(s.name)} =` : `${esc(s.name)} ${d > 0 ? "A" : "B"} −${Math.abs(d).toFixed(2)}`; title += ` · B ${tb.toFixed(3)}s · ${d > 0 ? "A" : "B"} faster by ${Math.abs(d).toFixed(3)}`;
    }
    return `<div data-i="${i}" style="width:${(s.e - s.s) / L * 100}%;background:${bg}" title="${esc(title)}">${label}</div>`;
  }).join("");
  document.querySelectorAll("#minis div").forEach((el) => el.onclick = () => { const s = SEG[+el.dataset.i]; zoom([s.s, s.e]); });
}
function bandsHook(u) {   // shade alternating corner segments + dashed apex lines
  const { ctx, bbox } = u; ctx.save();
  SEG.forEach((s, i) => {
    const x0 = u.valToPos(s.s, "x", true), x1 = u.valToPos(s.e, "x", true);
    if (i % 2) { ctx.fillStyle = "rgba(255,255,255,0.025)"; ctx.fillRect(x0, bbox.top, x1 - x0, bbox.height); }
    const xa = u.valToPos(s.apex, "x", true);
    ctx.strokeStyle = "rgba(225,6,0,0.35)"; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(xa, bbox.top); ctx.lineTo(xa, bbox.top + bbox.height); ctx.stroke();
  });
  ctx.restore();
}
function labelsHook(u) {
  const { ctx, bbox } = u; ctx.save(); ctx.fillStyle = "#c9ccd1"; ctx.font = `600 ${11 * devicePixelRatio}px Titillium Web, sans-serif`; ctx.textAlign = "center";
  SEG.forEach((s, i) => {
    const xa = u.valToPos(s.apex, "x", true);
    if (xa >= bbox.left && xa <= bbox.left + bbox.width) ctx.fillText(s.name, xa, bbox.top + (i % 2 ? 27 : 13) * devicePixelRatio);
  });
  ctx.restore();
}
function drawCharts() {
  const w = () => Math.max(300, $("charts").clientWidth - 16);
  const axes = (yl, splits) => [{ stroke: COL.axis, grid: { stroke: COL.grid, width: 1 }, ticks: { stroke: COL.grid }, values: (u, v) => v.map((m) => m + " m") },
    { stroke: COL.axis, grid: { stroke: COL.grid, width: 1 }, ticks: { stroke: COL.grid }, size: 50, label: yl, labelSize: 14, labelFont: "11px Titillium Web", ...(splits ? { splits: () => splits } : {}) }];
  const add = (title, series, data, h, extra = {}) => {
    const el = document.createElement("div"); el.className = "chart"; $("charts").appendChild(el);
    const p = new uPlot({
      width: w(), height: h, series: [{ label: "Dist", value: (u, v) => v == null ? "-" : v.toFixed(0) + " m" }, ...series], axes: axes(extra.yl, extra.splits),
      scales: { x: { time: false }, y: extra.y || {} }, legend: { show: true, live: true },
      cursor: { sync: { key: "rv", setSeries: false }, drag: { x: true, y: false }, points: { size: 6 } },
      hooks: { drawAxes: [bandsHook], ...(extra.labels ? { draw: [labelsHook] } : {}),
        setScale: [(u, k) => { if (k !== "x" || syncing) return; syncing = true; plots.forEach((o) => o !== u && o.setScale("x", { min: u.scales.x.min, max: u.scales.x.max })); syncing = false; }],
        setCursor: [(u) => cursorAt(u.cursor.idx)] },
    }, data, el);
    plots.push(p);
  };
  const s = (label, color, k = {}) => ({ label, stroke: color, width: 1.6 / 1, points: { show: false }, ...k });
  const pair = (k, f = (v) => v, step = false) => {
    const a = A.trace[k].map(f), series = [s("A", COL.A, step ? { paths: uPlot.paths.stepped({ align: 1 }) } : {})], data = [X, a];
    if (B && B.trace[k]) { data.push((step ? onAstep(B, k, X) : onA(B, k, X)).map(f)); series.push(s("B", COL.B, step ? { paths: uPlot.paths.stepped({ align: 1 }) } : {})); }
    return [series, data];
  };
  if (B) {
    const delta = X.map((d, i) => tAt(B, d) - A.trace.t[i]);
    add("delta", [s("B − A (s)", COL.delta, { width: 2, fill: "rgba(177,76,255,0.12)", value: (u, v) => v == null ? "-" : sgn(v) })], [X, delta], 130, { yl: "Δ s (+ = B slower)" });
  }
  let [se, da] = pair("speed"); add("speed", se, da, 230, { yl: "Speed km/h", labels: true });
  [se, da] = pair("throttle", (v) => v * 100); add("throttle", se, da, 110, { yl: "Throttle %", y: { range: [0, 105] }, splits: [0, 50, 100] });
  [se, da] = pair("brake", (v) => v * 100); add("brake", se, da, 100, { yl: "Brake %", y: { range: [0, 105] }, splits: [0, 50, 100] });
  [se, da] = pair("gear", (v) => v, true); add("gear", se, da, 120, { yl: "Gear", y: { range: [0, 8.5] }, splits: [2, 4, 6, 8] });
  if (A.trace.rpm) { [se, da] = pair("rpm"); add("rpm", se, da, 120, { yl: "RPM" }); }
  [se, da] = pair("steer"); add("steer", se, da, 100, { yl: "Steering", y: { range: [-1.05, 1.05] }, splits: [-1, 0, 1] });
  if (A.trace.glat) { [se, da] = pair("glat"); add("glat", se, da, 100, { yl: "Lateral G" }); }
  if (A.trace.glon) { [se, da] = pair("glon"); add("glon", se, da, 100, { yl: "Long. G" }); }
  if (A.trace.slip_f) { [se, da] = pair("slip_f"); add("slip", se, da, 100, { yl: "Front slip" }); }
  if (A.trace.drs && A.trace.drs.some((v) => v > 0)) { [se, da] = pair("drs", (v) => v, true); add("drs", se, da, 70, { yl: "DRS", y: { range: [0, 1.1] }, splits: [0, 1] }); }
}
function resizeCharts() { const w = Math.max(300, $("charts").clientWidth - 16); plots.forEach((p) => p.setSize({ width: w, height: p.height })); if (A) { drawMap(); drawAnalysis(); } }
window.addEventListener("resize", resizeCharts);

function drawCornerTable() {
  if (!SEG.length) { $("corners").innerHTML = '<tr><td class="muted">Corner detection needs a full lap.</td></tr>'; return; }
  const two = (a, b, f) => b == null ? f(a) : `<span class="cA">${f(a)}</span> / <span class="cB">${f(b)}</span>`;
  const bp = (c) => c.brake == null ? "flat" : `${c.brake.toFixed(0)}${c.kind === "lift" ? " lift" : ""}`;
  const rows = SEG.map((sg, i) => {
    const a = cornerStats(A, sg), b = B ? cornerStats(B, sg) : null, d = b ? b.time - a.time : null;
    return `<tr data-i="${i}"><td class="nm">${esc(sg.name)}</td>
      <td class="${d == null ? "" : d > 0.01 ? "cA" : d < -0.01 ? "cB" : "muted"}">${d == null ? a.time.toFixed(2) : sgn(d)}</td>
      <td>${two(a.vmin, b && b.vmin, (v) => v.toFixed(0))}</td><td>${two(a, b, bp)}</td>
      <td>${two(a.gear, b && b.gear, (v) => Math.round(v))}</td><td>${two(a.thr, b && b.thr, (v) => v == null ? "-" : v.toFixed(0))}</td></tr>`;
  }).join("");
  $("corners").innerHTML = `<thead><tr><th>Corner</th><th>${B ? "Δ B−A" : "Time"}</th><th>Apex km/h</th><th title="metres before the apex where braking (or a lift) began">Brake m</th><th>Gear</th><th title="metres after the apex to full throttle">Thr m</th></tr></thead><tbody>${rows}</tbody>`;
  document.querySelectorAll("#corners tbody tr").forEach((tr) => tr.onclick = () => { const s = SEG[+tr.dataset.i]; zoom([s.s, s.e]); });
}
function drawNotes() {
  const r = A.report;
  if (!r) { $("notes").innerHTML = '<span class="muted">No engineer analysis for this lap (reference/ghost laps are the yardstick, not analysed).</span>'; return; }
  const li = (arr) => arr.map((x) => `<li>${x}</li>`).join("");
  const losses = (r.losses || []).map((c) => `<b>${esc(c.corner)}</b> <span class="neg">+${c.time_loss.toFixed(3)}</span> ${esc((c.causes || []).join(", "))}`);
  const gains = (r.gains || []).map((c) => `<b>${esc(c.corner)}</b> <span class="pos">${c.time_loss.toFixed(3)}</span> ${esc((c.causes || []).join(", "))}`);
  $("notes").innerHTML = `<div class="muted">vs ${esc(r.ref_label)} ${fmt(r.ref_time)} (${sgn(r.delta)}) · ${esc(r.input_device)}${r.valid ? "" : " · invalid lap"}</div>` +
    (losses.length ? `<div style="margin-top:6px">Time lost</div><ul>${li(losses)}</ul>` : "") +
    (gains.length ? `<div style="margin-top:6px">Better than reference</div><ul>${li(gains)}</ul>` : "") +
    ((r.mistakes || []).length ? `<div style="margin-top:6px">Mistakes</div><ul>${li(r.mistakes.map(esc))}</ul>` : "");
}

// ---- track map: colour by speed (single lap) or by who is faster (comparison); synced cursor
let mapBase = null, mapGeo = null, MAPMODE = "speed";
document.querySelectorAll("#mapmode button").forEach((b) => b.onclick = () => {
  MAPMODE = b.dataset.m; document.querySelectorAll("#mapmode button").forEach((x) => x.classList.toggle("on", x === b));
  if (A) { drawMap(); cursorAt(null); }
});
function drawMap() {
  const c = $("map"), dpr = devicePixelRatio || 1, size = c.clientWidth || 360;
  c.width = size * dpr; c.height = size * dpr;
  const g = c.getContext("2d"), t = A.trace;
  if (!t.x) { g.clearRect(0, 0, c.width, c.height); mapBase = null; return; }
  const pad = 22 * dpr, xs = t.x, zs = t.z;
  const minx = Math.min(...xs), maxx = Math.max(...xs), minz = Math.min(...zs), maxz = Math.max(...zs);
  const sc = (c.width - 2 * pad) / Math.max(maxx - minx, maxz - minz), ox = (c.width - (maxx - minx) * sc) / 2, oz = (c.height - (maxz - minz) * sc) / 2;
  const P = (x, z) => [ox + (x - minx) * sc, oz + (maxz - z) * sc];
  mapGeo = { P };
  g.fillStyle = "#14161b"; g.fillRect(0, 0, c.width, c.height);
  g.lineCap = "round"; g.lineJoin = "round";
  g.strokeStyle = "#2c313b"; g.lineWidth = 11 * dpr; g.beginPath(); xs.forEach((x, i) => { const [px, pz] = P(x, zs[i]); i ? g.lineTo(px, pz) : g.moveTo(px, pz); }); g.stroke();
  let col;
  if (MAPMODE === "gear") {
    const gc = ["#555", "#3b4cc0", "#5977e3", "#7b9ff9", "#9ebeff", "#f7b89c", "#f08a6c", "#d65244", "#b40426"];
    col = (i) => gc[Math.max(0, Math.min(8, Math.round(t.gear[i])))];
    $("maplegend").innerHTML = "gear: " + [1, 2, 3, 4, 5, 6, 7, 8].map((g) => `<span style="color:${gc[g]}">■${g}</span>`).join(" ");
  } else if (MAPMODE === "pedals") {
    col = (i) => t.brake[i] > 0.05 ? `rgba(225,6,0,${0.45 + 0.55 * t.brake[i]})` : t.throttle[i] > 0.98 ? "#20d47a" : t.throttle[i] > 0.05 ? "#ffd23d" : "#9aa0aa";
    $("maplegend").innerHTML = '<span style="color:#20d47a">■ full throttle</span> <span style="color:#ffd23d">■ partial</span> <span style="color:#e10600">■ braking</span> <span style="color:#9aa0aa">■ coasting</span>';
  } else if (B && MAPMODE === "delta") {
    const dl = X.map((d, i) => tAt(B, d) - t.t[i]), w = 12;
    col = (i) => { const v = (dl[Math.min(dl.length - 1, i + w)] - dl[Math.max(0, i - w)]); const k = Math.min(1, Math.abs(v) / 0.03);
      return Math.abs(v) < 0.002 ? "#9aa0aa" : v > 0 ? `rgba(0,210,255,${0.35 + 0.65 * k})` : `rgba(255,135,0,${0.35 + 0.65 * k})`; };
    $("maplegend").innerHTML = '<span class="cA">A faster</span> · <span class="cB">B faster</span>';
  } else if (MAPMODE === "delta") {
    col = () => "#9aa0aa";
    $("maplegend").textContent = "pick a lap B to see who is faster where";
  } else {
    const lo = Math.min(...t.speed), hi = Math.max(...t.speed);
    col = (i) => `hsl(${(1 - (t.speed[i] - lo) / (hi - lo)) * 220},90%,55%)`;   // red = fast, blue = slow
    $("maplegend").textContent = "colour = speed (blue slow → red fast)";
  }
  g.lineWidth = 5 * dpr;
  for (let i = 1; i < xs.length; i++) { g.strokeStyle = col(i); g.beginPath(); g.moveTo(...P(xs[i - 1], zs[i - 1])); g.lineTo(...P(xs[i], zs[i])); g.stroke(); }
  const [sx, sz] = P(xs[0], zs[0]); g.fillStyle = "#fff"; g.fillRect(sx - 6 * dpr, sz - 2 * dpr, 12 * dpr, 4 * dpr);   // start/finish
  g.font = `600 ${11 * dpr}px Titillium Web, sans-serif`; g.fillStyle = "#c9ccd1"; g.textAlign = "center";
  SEG.forEach((s) => { const i = nearestIdx(s.apex, X), [px, pz] = P(xs[i], zs[i]); g.fillText(s.name, px, pz - 9 * dpr); });
  mapBase = g.getImageData(0, 0, c.width, c.height);
}
function cursorAt(idx) {
  const c = $("map"), g = c.getContext("2d");
  if (mapBase) g.putImageData(mapBase, 0, 0);
  if (idx == null || !A || !mapGeo) { $("readout").innerHTML = '<div class="muted" style="grid-column:1/-1">Hover the charts to scrub the lap. Drag to zoom, double-click to reset.</div>'; return; }
  const dpr = devicePixelRatio || 1, d = X[idx], t = A.trace;
  const dotAt = (x, z, color) => { const [px, pz] = mapGeo.P(x, z); g.fillStyle = color; g.strokeStyle = "#000"; g.lineWidth = 2 * dpr; g.beginPath(); g.arc(px, pz, 6 * dpr, 0, 7); g.fill(); g.stroke(); };
  if (B && B.trace.x) dotAt(interp(d, B.trace.dist, B.trace.x), interp(d, B.trace.dist, B.trace.z), COL.B);
  if (t.x) dotAt(t.x[idx], t.z[idx], COL.A);
  const bi = B ? nearestIdx(d, B.trace.dist) : null, two = (fa, fb) => B ? `<span class="cA">${fa}</span> <span class="cB">${fb}</span>` : fa;
  const corner = SEG.find((s) => d >= s.s && d < s.e);
  $("readout").innerHTML = `<div><span class="muted">Distance</span><b>${d.toFixed(0)} m</b></div>
    <div><span class="muted">Speed</span><b>${two(t.speed[idx].toFixed(0), B && B.trace.speed[bi].toFixed(0))}</b></div>
    <div><span class="muted">Gear</span><b>${two(Math.round(t.gear[idx]), B && Math.round(B.trace.gear[bi]))}</b></div>
    <div><span class="muted">Corner</span><b>${esc(corner ? corner.name : "-")}</b></div>
    <div><span class="muted">Thr / Brk</span><b>${two(`${(t.throttle[idx] * 100).toFixed(0)}/${(t.brake[idx] * 100).toFixed(0)}`, B && `${(B.trace.throttle[bi] * 100).toFixed(0)}/${(B.trace.brake[bi] * 100).toFixed(0)}`)}</b></div>
    <div><span class="muted">${B ? "Δ B−A" : "Lap time"}</span><b>${B ? sgn(tAt(B, d) - t.t[idx]) : fmt(t.t[idx])}</b></div>`;
}

// ---- analysis panels (plain canvas charts)
function fitCanvas(c, hCss) {
  const dpr = devicePixelRatio || 1, w = c.clientWidth || 360, h = hCss || +c.dataset.h || w;
  c.width = w * dpr; c.height = h * dpr; c.style.height = h + "px";
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0); g.clearRect(0, 0, w, h);
  g.font = "600 11px Titillium Web, sans-serif"; g.textBaseline = "middle";
  return { g, w, h };
}
function hbars(id, labels, series, opt = {}) {   // horizontal grouped bars, one row per label
  const c = $(id); const rows = labels.length; if (!rows) { fitCanvas(c, 40).g.fillText("no data", 8, 20); return; }
  const rowH = series.length > 1 ? 22 : 18, { g, w } = fitCanvas(c, rows * rowH + 24), left = 118, right = opt.signed ? 56 : 48;
  const vals = series.flatMap((s) => s.values.filter((v) => v != null));
  const lo = opt.signed ? Math.min(0, ...vals) : 0, hi = Math.max(opt.signed ? 0 : 1e-9, ...vals);
  const X = (v) => left + (v - lo) / (hi - lo || 1) * (w - left - right);
  g.strokeStyle = "#262a33"; g.beginPath(); g.moveTo(X(0), 4); g.lineTo(X(0), rows * rowH + 8); g.stroke();
  labels.forEach((lab, r) => {
    const y0 = 8 + r * rowH;
    g.fillStyle = "#c9ccd1"; g.textAlign = "right"; g.fillText(lab, left - 8, y0 + rowH / 2);
    series.forEach((s, k) => {
      const v = s.values[r]; if (v == null) return;
      const bh = (rowH - 6) / series.length, y = y0 + 2 + k * bh;
      g.fillStyle = typeof s.color === "function" ? s.color(v) : s.color;
      const x0 = X(Math.min(0, v)), x1 = X(Math.max(0, v)); g.fillRect(x0, y, Math.max(1, x1 - x0), bh - 1);
      if (series.length === 1 || k === series.length - 1 || opt.labelAll) {
        g.fillStyle = "#858c99";
        if (opt.signed) { g.textAlign = "right"; g.fillText(opt.fmt ? opt.fmt(v) : v.toFixed(0), w - 2, y + bh / 2); }   // value column on the right
        else { g.textAlign = "left"; g.fillText(opt.fmt ? opt.fmt(v) : v.toFixed(0), x1 + 4, y + bh / 2); }
      }
    });
  });
}
function vbars(id, labels, series, opt = {}) {   // vertical grouped bars (distributions)
  const { g, w, h } = fitCanvas($(id)), pad = 26, top = 10;
  const hi = Math.max(1e-9, ...series.flatMap((s) => s.values));
  const gw = (w - pad - 6) / labels.length, bw = (gw - 6) / series.length;
  g.strokeStyle = "#262a33"; for (let k = 0; k <= 4; k++) { const y = top + (h - pad - top) * k / 4; g.beginPath(); g.moveTo(pad, y); g.lineTo(w, y); g.stroke(); }
  labels.forEach((lab, i) => {
    series.forEach((s, k) => {
      const v = s.values[i], bh = (h - pad - top) * v / hi, x = pad + i * gw + 3 + k * bw;
      g.fillStyle = s.color; g.fillRect(x, h - pad - bh, bw - 1, bh);
    });
    g.fillStyle = "#858c99"; g.textAlign = "center"; g.fillText(lab, pad + i * gw + gw / 2, h - pad / 2);
  });
  g.textAlign = "left"; g.fillStyle = "#858c99"; g.fillText(hi.toFixed(0) + (opt.unit || ""), 2, top);
}
function drawGG() {
  const c = $("gg"); const { g, w, h } = fitCanvas(c, c.clientWidth || 360);
  const R = Math.min(w, h) / 2 - 14, cx = w / 2, cy = h / 2;
  const laps = [A, B].filter((l) => l && l.trace.glat && l.trace.glon);
  const all = laps.flatMap((l) => [...l.trace.glat.map(Math.abs), ...l.trace.glon.map(Math.abs)]).sort((a, b) => a - b);
  const m = Math.max(2, all.length ? all[Math.floor(all.length * 0.995)] : 3);   // ignore kerb/crash spikes
  const S = R / Math.ceil(m);
  g.strokeStyle = "#262a33"; g.fillStyle = "#858c99"; g.textAlign = "left";
  for (let k = 1; k <= Math.ceil(m); k++) { g.beginPath(); g.arc(cx, cy, k * S, 0, 7); g.stroke(); g.fillText(k + "g", cx + k * S * 0.71 + 2, cy - k * S * 0.71); }
  g.beginPath(); g.moveTo(cx - R, cy); g.lineTo(cx + R, cy); g.moveTo(cx, cy - R); g.lineTo(cx, cy + R); g.stroke();
  g.textAlign = "center"; g.fillText("braking", cx, cy + R + 8); g.fillText("acceleration", cx, cy - R - 6);
  laps.forEach((l, k) => {
    g.fillStyle = k ? "rgba(255,135,0,0.35)" : "rgba(0,210,255,0.35)";
    for (let i = 0; i < l.trace.glat.length; i++) if (Math.abs(l.trace.glat[i]) <= Math.ceil(m) && Math.abs(l.trace.glon[i]) <= Math.ceil(m)) g.fillRect(cx + l.trace.glat[i] * S - 1, cy - l.trace.glon[i] * S - 1, 2.2, 2.2);
  });
  if (!laps.length) { g.fillStyle = "#858c99"; g.fillText("no G-force data for this lap (ghost)", cx, cy); }
}
function drawAnalysis() {
  if (!A) return;
  drawGG();
  // lap time progression for A's session
  const sess = LAPS.find((l) => l.id === A.id)?.session, laps = LAPS.filter((l) => l.session === sess && l.car === "Player" && l.lap_time > 0).sort((a, b) => a.lap - b.lap || a.id - b.id);
  const ghost = LAPS.filter((l) => l.session === sess && l.car !== "Player" && l.lap_time > 0).reduce((b, l) => !b || l.lap_time < b.lap_time ? l : b, null);
  { const { g, w, h } = fitCanvas($("c-laps")), pad = 46;
    if (laps.length) {
      const ts = laps.map((l) => l.lap_time).concat(ghost ? [ghost.lap_time] : []), lo = Math.min(...ts) - 0.5, hi = Math.max(...ts) + 0.5;
      const X = (i) => pad + (laps.length === 1 ? (w - pad) / 2 : i * (w - pad - 20) / (laps.length - 1)), Y = (v) => 12 + (v - lo) / (hi - lo) * (h - 40);
      g.strokeStyle = "#262a33"; g.fillStyle = "#858c99"; g.textAlign = "right";
      for (let k = 0; k <= 3; k++) { const v = lo + (hi - lo) * k / 3; g.beginPath(); g.moveTo(pad, Y(v)); g.lineTo(w, Y(v)); g.stroke(); g.fillText(fmt(v), pad - 4, Y(v)); }
      if (ghost) { g.strokeStyle = "#b14cff"; g.setLineDash([5, 4]); g.beginPath(); g.moveTo(pad, Y(ghost.lap_time)); g.lineTo(w, Y(ghost.lap_time)); g.stroke(); g.setLineDash([]); g.fillStyle = "#b14cff"; g.textAlign = "left"; g.fillText("PB ghost " + fmt(ghost.lap_time), pad + 4, Y(ghost.lap_time) - 8); }
      g.strokeStyle = "#c9ccd1"; g.beginPath(); laps.forEach((l, i) => i ? g.lineTo(X(i), Y(l.lap_time)) : g.moveTo(X(i), Y(l.lap_time))); g.stroke();
      const best = Math.min(...laps.map((l) => l.lap_time));
      laps.forEach((l, i) => {
        g.fillStyle = l.lap_time === best ? "#b14cff" : l.valid ? "#20d47a" : "#ff5a4f";
        g.beginPath(); g.arc(X(i), Y(l.lap_time), l.id === A.id || (B && l.id === B.id) ? 6 : 4, 0, 7); g.fill();
        g.fillStyle = "#858c99"; g.textAlign = "center"; g.fillText("L" + l.lap + (l.valid ? "" : "✗"), X(i), h - 10);
      });
    } else g.fillText("no laps", 10, 20);
  }
  // per-corner comparisons
  const names = SEG.map((s) => s.name), sa = SEG.map((s) => cornerStats(A, s)), sb = B ? SEG.map((s) => cornerStats(B, s)) : null;
  if (B) {
    $("cdl-sub").innerHTML = '<span class="cA">◀ A faster</span> · <span class="cB">B faster ▶</span>';
    hbars("c-cdelta", names, [{ values: sa.map((a, i) => sb[i].time - a.time), color: (v) => v > 0 ? COL.A : COL.B }], { signed: true, fmt: (v) => sgn(v) });
  } else if (A.report) {
    $("cdl-sub").textContent = `vs ${A.report.ref_label} · + = time lost`;
    const rc = new Map(A.report.corners.map((c) => [c.corner, c.time_loss]));
    const seen = {}, lossFor = (s) => { const base = s.name.replace(/ \d+$/, ""); seen[base] = (seen[base] || 0) + 1; return A.report.corners.filter((c) => c.corner === base)[seen[base] - 1]?.time_loss ?? rc.get(base) ?? null; };
    hbars("c-cdelta", names, [{ values: SEG.map(lossFor), color: (v) => v > 0 ? "#ff5a4f" : "#20d47a" }], { signed: true, fmt: (v) => sgn(v) });
  } else { $("cdl-sub").textContent = "segment times"; hbars("c-cdelta", names, [{ values: sa.map((a) => a.time), color: "#5a6170" }], { fmt: (v) => v.toFixed(2) + "s" }); }
  const two = (f) => [{ values: sa.map(f), color: COL.A }, ...(B ? [{ values: sb.map(f), color: COL.B }] : [])];
  hbars("c-apex", names, two((s) => s.vmin), { labelAll: true });
  hbars("c-brake", names, two((s) => s.brake), { labelAll: true });
  // distributions
  const dist = (lap, fn, bins) => { const n = new Array(bins).fill(0), t = lap.trace; for (let i = 1; i < t.dist.length; i++) { const b = fn(t, i); if (b >= 0 && b < bins) n[b] += t.t[i] - t.t[i - 1]; } const tot = n.reduce((a, b) => a + b, 0) || 1; return n.map((v) => v / tot * 100); };
  const distD = (lap, fn, bins) => { const n = new Array(bins).fill(0), t = lap.trace; for (let i = 0; i < t.dist.length; i++) { const b = fn(t, i); if (b >= 0 && b < bins) n[b]++; } return n.map((v) => v / t.dist.length * 100); };
  const gearF = (t, i) => Math.round(t.gear[i]) - 1;
  vbars("c-gears", ["1", "2", "3", "4", "5", "6", "7", "8"], [{ values: distD(A, gearF, 8), color: COL.A }, ...(B ? [{ values: distD(B, gearF, 8), color: COL.B }] : [])], { unit: "%" });
  const spF = (t, i) => Math.floor(t.speed[i] / 20), spL = Array.from({ length: 18 }, (_, k) => (k % 3 ? "" : k * 20));
  vbars("c-speedhist", spL, [{ values: dist(A, spF, 18), color: COL.A }, ...(B ? [{ values: dist(B, spF, 18), color: COL.B }] : [])], { unit: "%" });
  const phF = (t, i) => t.brake[i] > 0.05 ? 0 : t.throttle[i] > 0.98 ? 3 : t.throttle[i] > 0.05 ? 2 : 1;
  vbars("c-phases", ["braking", "coasting", "partial", "full throttle"], [{ values: dist(A, phF, 4), color: COL.A }, ...(B ? [{ values: dist(B, phF, 4), color: COL.B }] : [])], { unit: "%" });
  drawSectors(laps);
}
function drawSectors(laps) {
  const withS = laps.filter((l) => l.sectors);
  if (!withS.length) { $("sectors").innerHTML = '<span class="muted">No sector times for this session (recorded before sector logging, or ghost laps).</span>'; return; }
  const best = [0, 1, 2].map((k) => Math.min(...withS.map((l) => l.sectors[k]))), SC = sectorColours();
  const ideal = best.reduce((a, b) => a + b, 0) / 1000;
  $("sectors").innerHTML = `<table class="sectab"><thead><tr><th>Lap</th><th>S1</th><th>S2</th><th>S3</th><th>Time</th></tr></thead><tbody>` +
    withS.map((l) => `<tr${l.id === A.id ? ' style="background:rgba(0,210,255,.08)"' : ""}><td>L${l.lap}${l.valid ? "" : " ✗"}</td>${l.sectors.map((v, k) => `<td class="${SC.get(l.id)?.[k] || ""}">${(v / 1000).toFixed(3)}</td>`).join("")}<td>${fmt(l.lap_time)}</td></tr>`).join("") +
    `<tr><td class="muted">Ideal</td>${best.map((v) => `<td class="p">${(v / 1000).toFixed(3)}</td>`).join("")}<td class="p">${fmt(ideal)}</td></tr></tbody></table>` +
    `<div class="muted" style="margin-top:6px">Ideal lap = your best three sectors combined: ${fmt(ideal)} (${sgn(ideal - Math.min(...withS.map((l) => l.lap_time)))} vs your best lap).</div>`;
}

// ================================================================ boot
fetch("/api/radio").then((r) => r.json()).then((rows) => rows.forEach(addRadio)).catch(() => {});
connect();
(async () => {
  let st = {}; try { st = await (await fetch("/api/state")).json(); renderState(st); } catch {}
  const [tab, qs] = (location.hash.slice(1) || (st.offline ? "review" : "live")).split("?");
  showTab(["review", "live", "radio"].includes(tab) ? tab : "review");
  if (tab === "review" || tab === "laps") {
    await loadLaps();
    const q = new URLSearchParams(qs || "");
    if (q.get("a")) setLaps(+q.get("a"), q.get("b") ? +q.get("b") : null);
    else if (LAPS.length) { const p = LAPS.filter((l) => l.car === "Player"); const best = p.length ? p.reduce((b, l) => l.lap_time < b.lap_time ? l : b) : LAPS[0]; setLaps(best.id, null); }
  }
})();
