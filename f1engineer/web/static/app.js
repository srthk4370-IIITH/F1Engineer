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
function renderState(s) {
  OFFLINE = !!s.offline;
  $("dot").classList.toggle("on", !!s.connected);
  $("conn").textContent = s.offline ? "review mode" : s.connected ? "live" : "waiting for game";
  $("liveoff").classList.toggle("hidden", !s.offline);
  if (s.offline) return;   // review mode: the header shows the reviewed lap instead
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
  try {
    const r = await (await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }) })).json();
    if (OFFLINE) { addRadio({ role: "driver", text: q }); addRadio({ role: "engineer", text: r.answer }); }
  } catch (err) { addRadio({ role: "engineer", text: "Radio's down: " + err }); }
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
function renderLapList() {
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
          <span class="meta">${l.valid ? "" : "invalid · "}${inp}</span>
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
  drawStats(); drawMinis(); drawCharts(); drawCornerTable(); drawNotes(); drawMap(); cursorAt(null);
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
  if (A.trace.slip_f) { [se, da] = pair("slip_f"); add("slip", se, da, 100, { yl: "Front slip" }); }
}
function resizeCharts() { const w = Math.max(300, $("charts").clientWidth - 16); plots.forEach((p) => p.setSize({ width: w, height: p.height })); if (A) drawMap(); }
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
let mapBase = null, mapGeo = null;
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
  if (B) {
    const dl = X.map((d, i) => tAt(B, d) - t.t[i]), w = 12;
    col = (i) => { const v = (dl[Math.min(dl.length - 1, i + w)] - dl[Math.max(0, i - w)]); const k = Math.min(1, Math.abs(v) / 0.03);
      return Math.abs(v) < 0.002 ? "#9aa0aa" : v > 0 ? `rgba(0,210,255,${0.35 + 0.65 * k})` : `rgba(255,135,0,${0.35 + 0.65 * k})`; };
    $("maplegend").innerHTML = '<span class="cA">A faster</span> · <span class="cB">B faster</span>';
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
