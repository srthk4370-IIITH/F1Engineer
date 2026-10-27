const $ = (id) => document.getElementById(id);
const fmt = (s) => { if (!s) return "-"; const m = Math.floor(s / 60); const r = (s - m * 60).toFixed(3).padStart(6, "0"); return m ? `${m}:${r}` : r; };
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

// ---- tabs ----
document.querySelectorAll("nav button").forEach((b) => b.onclick = () => {
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x === b));
  ["live", "laps", "chat"].forEach((t) => $("tab-" + t).classList.toggle("hidden", t !== b.dataset.tab));
  if (b.dataset.tab === "laps") loadLaps();
});

// ---- live ----
let lastReportKey = null;
function tempColor(t) { return t < 75 ? "#2b4f7a" : t < 90 ? "#2b6f55" : t < 105 ? "#3c7a2b" : t < 115 ? "#7a6a2b" : "#7a2b2b"; }
function renderState(s) {
  $("dot").classList.toggle("on", !!s.connected);
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
  ws.onmessage = (m) => { const j = JSON.parse(m.data); if (j.state) renderState(j.state); if (j.event && j.event.type === "radio") addRadio(j.event); if (j.event && j.event.type === "lap") loadLapsIfVisible(); };
  ws.onclose = () => { $("dot").classList.remove("on"); setTimeout(connect, 1500); };
}
fetch("/api/radio").then((r) => r.json()).then((rows) => rows.forEach(addRadio));
connect();

// ---- chat ----
$("chatform").onsubmit = async (e) => {
  e.preventDefault(); const q = $("q").value.trim(); if (!q) return; $("q").value = "";
  try { await fetch("/api/ask", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ q }) }); }
  catch (err) { addRadio({ role: "engineer", text: "Radio's down: " + err }); }
};

// ---- laps ----
let A = null, B = null, plots = [];
function loadLapsIfVisible() { if (!$("tab-laps").classList.contains("hidden")) loadLaps(); }
async function loadLaps() {
  const laps = await (await fetch("/api/laps")).json();
  $("laps").innerHTML = laps.map((l) => `<tr data-id="${l.id}"><td>${l.id === A?.id ? '<span class="tagA">A</span>' : l.id === B?.id ? '<span class="tagB">B</span>' : ""}</td><td>${esc(l.car)}</td><td>${l.lap}</td><td>${fmt(l.lap_time)}${l.valid ? "" : " ✗"}</td><td class="muted">${esc(l.input || "")}</td><td class="muted">${esc(l.track || "")} ${esc(l.type || "")}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No laps stored yet.</td></tr>';
  document.querySelectorAll("#laps tr[data-id]").forEach((tr) => tr.onclick = async (e) => {
    const lap = await (await fetch("/api/lap/" + tr.dataset.id)).json(); lap.id = +tr.dataset.id;
    if (e.shiftKey) B = lap; else A = lap;
    loadLaps(); drawCompare();
  });
}
function interp(x, xs, ys) { // linear interpolation on sorted xs
  let lo = 0, hi = xs.length - 1; if (x <= xs[0]) return ys[0]; if (x >= xs[hi]) return ys[hi];
  while (hi - lo > 1) { const m = (lo + hi) >> 1; if (xs[m] <= x) lo = m; else hi = m; }
  const f = (x - xs[lo]) / (xs[hi] - xs[lo]); return ys[lo] + f * (ys[hi] - ys[lo]);
}
function drawCompare() {
  plots.forEach((p) => p.destroy()); plots = []; $("charts").innerHTML = "";
  if (!A) return;
  $("labA").textContent = `A: ${A.car} L${A.lap} ${fmt(A.lap_time)}`; $("labB").textContent = B ? `B: ${B.car} L${B.lap} ${fmt(B.lap_time)}` : "B: -";
  const x = A.trace.dist, w = $("charts").clientWidth || 600;
  const bAt = (k) => B ? x.map((d) => interp(d, B.trace.dist, B.trace[k])) : null;
  const mk = (title, keys, h) => {
    const data = [x], series = [{}];
    keys.forEach(([k, label, col, src]) => { data.push(src === "B" ? bAt(k) : A.trace[k]); series.push({ label, stroke: col, width: 1.5 }); });
    const el = document.createElement("div"); $("charts").appendChild(el);
    plots.push(new uPlot({ title, width: w, height: h, series, axes: [{ stroke: "#8b93a1", grid: { stroke: "#2a2f37" } }, { stroke: "#8b93a1", grid: { stroke: "#2a2f37" } }],
      cursor: { sync: { key: "lap" } }, legend: { show: true } }, data.filter((d) => d), el));
  };
  const two = (k, la) => B ? [[k, "A " + la, "#4da3ff"], [k, "B " + la, "#ffc53d", "B"]] : [[k, la, "#4da3ff"]];
  if (B) {
    const delta = x.map((d, i) => interp(d, B.trace.dist, B.trace.t) - A.trace.t[i]);
    const el = document.createElement("div"); $("charts").appendChild(el);
    plots.push(new uPlot({ title: "Delta B − A (s, + = B slower)", width: w, height: 140, series: [{}, { label: "delta", stroke: "#b37bff", width: 2 }],
      axes: [{ stroke: "#8b93a1", grid: { stroke: "#2a2f37" } }, { stroke: "#8b93a1", grid: { stroke: "#2a2f37" } }], cursor: { sync: { key: "lap" } } }, [x, delta], el));
  }
  mk("Speed (km/h)", two("speed", "speed"), 200);
  mk("Throttle / brake", B ? [["throttle", "A thr", "#2fd27a"], ["brake", "A brk", "#ff4d4d"], ["throttle", "B thr", "#9be8bd", "B"], ["brake", "B brk", "#ffa3a3", "B"]] : [["throttle", "thr", "#2fd27a"], ["brake", "brk", "#ff4d4d"]], 150);
  mk("Gear", two("gear", "gear"), 110);
  mk("Steering", two("steer", "steer"), 110);
  drawMap();
  const rep = A.report; $("lapreport").innerHTML = rep ? reportTable(rep) : '<div class="muted">No corner report for this lap (reference laps and ghosts have none).</div>';
}
function drawMap() {
  const c = $("map"), g = c.getContext("2d"); g.clearRect(0, 0, c.width, c.height);
  const t = A.trace; if (!t.x) return;
  const xs = t.x, zs = t.z, minx = Math.min(...xs), maxx = Math.max(...xs), minz = Math.min(...zs), maxz = Math.max(...zs);
  const s = 380 / Math.max(maxx - minx, maxz - minz), px = (i) => 20 + (xs[i] - minx) * s, pz = (i) => 20 + (zs[i] - minz) * s;
  let loss = null;
  if (B) { const d = t.dist.map((dd, i) => interp(dd, B.trace.dist, B.trace.t) - t.t[i]); loss = d.map((v, i) => i ? v - d[i - 1] : 0); }
  for (let i = 1; i < xs.length; i++) {
    let col = "#4da3ff";
    if (loss) { const v = Math.max(-0.01, Math.min(0.01, loss[i])) / 0.01; col = v > 0 ? `rgba(255,77,77,${0.3 + 0.7 * v})` : `rgba(47,210,122,${0.3 - 0.7 * v})`; }
    g.strokeStyle = col; g.lineWidth = 4; g.beginPath(); g.moveTo(px(i - 1), pz(i - 1)); g.lineTo(px(i), pz(i)); g.stroke();
  }
  g.fillStyle = "#8b93a1"; g.font = "12px sans-serif"; g.fillText(B ? "red = B losing to A, green = B gaining" : "lap A line", 20, 410);
}
window.addEventListener("resize", () => { if (A) drawCompare(); });
