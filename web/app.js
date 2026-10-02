"use strict";
// NextPull UI: plain ES2020, no modules, no bundler. window.pywebview.api methods return {ok, error, ...}.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const S = {page: "dashboard", status: null, jobs: [], settings: {}, boot: {}, filesPath: "", statsDays: 30, historyJob: "", sig: {}, nowKey: "", login: null};
let api = null;

// ---------------------------------------------------------------------------------------------- formatting
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${Math.round(n)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return `${n >= 100 ? n.toFixed(0) : n.toFixed(1)} ${units[i]}`;
}
const fmtSpeed = n => `${fmtBytes(n)}/s`;
function fmtDur(sec) {
  sec = Math.max(0, Math.round(Number(sec) || 0));
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ${sec % 60}s`;
  return `${Math.floor(sec / 3600)}h ${Math.floor(sec % 3600 / 60)}m`;
}
function fmtTime(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  return isNaN(d) ? esc(iso) : d.toLocaleString([], {month: "short", day: "numeric", hour: "2-digit", minute: "2-digit"});
}
function fmtIn(iso) {
  if (!iso) return "";
  const diff = (new Date(iso) - new Date()) / 1000;
  if (isNaN(diff)) return "";
  if (diff < 90) return "in a moment";
  if (diff < 5400) return `in ${Math.round(diff / 60)} min`;
  if (diff < 172800) return `in ${Math.round(diff / 3600)} h`;
  return `in ${Math.round(diff / 86400)} days`;
}
const STATE_LABEL = {ok: "OK", partial: "Needs attention", failed: "Failed", cancelled: "Cancelled", stopped: "Stopped", missed: "Missed", skipped: "Skipped", interrupted: "Interrupted", running: "Running", starting: "Starting", stopping: "Stopping",
  error: "Failed", dry: "Would transfer", present: "Already had"};
const badge = state => `<span class="badge ${esc(state)}">${esc(STATE_LABEL[state] || state)}</span>`;
function duration(run) {
  if (!run.started || !run.finished) return "";
  return fmtDur((new Date(run.finished) - new Date(run.started)) / 1000);
}

// ------------------------------------------------------------------------------------------------- plumbing
function toast(text, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = text;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : 3500);
}
async function call(name, ...args) {
  let result;
  try { result = await api[name](...args); } catch (error) { result = {ok: false, error: String(error)}; reportUiError(`${name}: ${error}`, "bridge", 0); }
  if (result && result.ok === false && result.error && !result.quiet) toast(result.error, "error");
  return result || {ok: false};
}
function modal(html, wide = false, layer = 30) {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.style.zIndex = layer;
  overlay.innerHTML = `<div class="modal ${wide ? "wide" : ""}">${html}</div>`;
  overlay.addEventListener("mousedown", event => { if (event.target === overlay) overlay.remove(); });
  overlay.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => overlay.remove()));
  $("modals").appendChild(overlay);
  return overlay;
}
function ask(message, okLabel = "Yes", danger = false) {
  return new Promise(resolve => {
    const overlay = modal(`<h2>${esc(message)}</h2><div class="row"><button class="btn ${danger ? "danger" : ""}" id="ask-ok">${esc(okLabel)}</button><button class="btn ghost" data-close>Cancel</button></div>`, false, 50);
    overlay.querySelector("#ask-ok").addEventListener("click", () => { overlay.remove(); resolve(true); });
    overlay.addEventListener("click", event => { if (event.target.closest("[data-close]")) resolve(false); });
  });
}

// ------------------------------------------------------------------------------------------------ navigation
function show(page) {
  S.page = page;
  document.querySelectorAll(".page").forEach(el => el.hidden = el.id !== `page-${page}`);
  document.querySelectorAll(".nav").forEach(el => el.classList.toggle("active", el.dataset.page === page));
  ({dashboard: loadDashboard, jobs: loadJobs, files: loadFiles, history: loadHistory, stats: loadStats, diagnostics: loadDiagnostics, settings: loadSettings})[page]();
}
document.addEventListener("click", event => {
  const nav = event.target.closest("[data-page]");
  if (nav) show(nav.dataset.page);
  const go = event.target.closest("[data-goto]");
  if (go) show(go.dataset.goto);
});

// ---------------------------------------------------------------------------------------------- status poll
async function poll() {
  const status = await api.status();
  if (!status || status.ok === false) return;
  const was = S.status;
  S.status = status;
  $("foot-conn").innerHTML = `<span class="dot ${status.connected ? "on" : "off"}"></span>${status.connected ? "Connected" : "Not connected"}`;
  $("foot-rclone").innerHTML = `<span class="dot ${status.rclone_found ? "on" : "off"}"></span>${status.rclone_found ? "rclone ready" : "rclone missing"}`;
  const badgeEl = $("nav-problems");
  badgeEl.hidden = !status.problem_count;
  badgeEl.textContent = status.problem_count > 9 ? "9+" : status.problem_count;
  if (S.page === "dashboard") updateDashboard();
  if (S.login && S.login.state === "waiting") pollLogin();
  if (was && was.running !== status.running && S.page === "jobs") loadJobs();
}
async function pollLogin() {
  const result = await api.connect_status();
  if (!result || result.state === S.login.state) return;
  S.login = result;
  if (result.state === "connected") { toast("Connected to Nextcloud", "ok"); S.boot.connection = result.connection; }
  if (S.page === "settings") loadSettings();
}

// -------------------------------------------------------------------------------------------------- dashboard
async function loadDashboard() {
  const [jobs, stats] = await Promise.all([api.list_jobs(), api.stats(30)]);
  S.jobs = jobs.jobs || [];
  const totals = (stats && stats.totals) || {};
  $("dash-tiles").innerHTML = [
    ["Downloaded (30 days)", fmtBytes(totals.bytes)], ["Files", totals.files ?? 0],
    ["Success rate", totals.success_rate == null ? "-" : `${totals.success_rate}%`], ["Average speed", totals.avg_speed ? fmtSpeed(totals.avg_speed) : "-"],
  ].map(([label, value]) => `<div class="tile"><span>${label}</span><b>${value}</b></div>`).join("");
  S.nowKey = "";
  updateDashboard();
}
function updateDashboard() {
  const status = S.status;
  if (!status) return;
  const notices = [];
  if (!status.connected) notices.push(`<div class="notice bad">Not connected to Nextcloud. <a href="#" data-goto="settings">Open Settings to connect</a>.</div>`);
  if (!status.rclone_found) notices.push(`<div class="notice bad">rclone was not found. Put <b>rclone.exe</b> in the <b>tools\\rclone</b> folder next to NextPull, or set its path in Settings.</div>`);
  if (status.problem_count) notices.push(`<div class="notice">${status.problem_count} recent run(s) had problems. <a href="#" data-goto="diagnostics">See what went wrong</a>.</div>`);
  if (!S.jobs.length) notices.push(`<div class="notice info">No jobs yet. <a href="#" data-goto="jobs">Create your first job</a>.</div>`);
  const noticeHtml = notices.join("");
  if (S.sig.notice !== noticeHtml) { S.sig.notice = noticeHtml; $("dash-notice").innerHTML = noticeHtml; }
  updateNow(status);
  const jobsHtml = status.jobs.length ? `<table><thead><tr><th>Job</th><th>Next run</th><th>Last run</th><th></th></tr></thead><tbody>${status.jobs.map(job => `
    <tr><td><b>${esc(job.name)}</b> ${job.enabled ? "" : '<span class="badge">Paused</span>'}</td>
    <td>${job.next_run ? `${fmtTime(job.next_run)} <span class="muted">${fmtIn(job.next_run)}</span>` : "-"}</td>
    <td>${job.last_run ? `${badge(job.last_run.state)} <span class="muted">${fmtTime(job.last_run.started)} - ${job.last_run.files} file(s), ${fmtBytes(job.last_run.bytes)}</span>` : '<span class="muted">never</span>'}</td>
    <td class="num"><button class="btn ghost small" data-run="${esc(job.id)}" ${job.running ? "disabled" : ""}>Run now</button></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No jobs.</div>';
  if (S.sig.jobs !== jobsHtml) { S.sig.jobs = jobsHtml; $("dash-jobs").innerHTML = jobsHtml; }
  const eventsHtml = status.events.length ? `<table><tbody>${status.events.slice(0, 12).map(e => `<tr><td class="muted" style="width:120px">${fmtTime(e.time)}</td><td>${e.level === "warn" || e.level === "error" ? `<span class="badge warn">!</span> ` : ""}${esc(e.text)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">Nothing yet.</div>';
  if (S.sig.events !== eventsHtml) { S.sig.events = eventsHtml; $("dash-events").innerHTML = eventsHtml; }
}
function updateNow(status) {
  const run = status.current;
  const key = run ? `run:${run.run_id}` : (status.starting ? "starting" : "idle");
  if (key !== S.nowKey) {   // rebuild the skeleton only when the situation changes, so typing in the speed box is never interrupted
    S.nowKey = key;
    if (!run) {
      const upcoming = status.jobs.filter(job => job.next_run).sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
      $("dash-now").innerHTML = `<div class="row between"><div><h2>${status.starting ? "Starting..." : "Idle"}</h2><p class="muted">${upcoming ? `Next run: <b>${esc(upcoming.name)}</b> ${fmtTime(upcoming.next_run)} (${fmtIn(upcoming.next_run)})` : "No scheduled runs."}</p></div></div>`;
    } else {
      $("dash-now").innerHTML = `<div class="row between"><div><h2 id="now-title"></h2><p class="muted" id="now-sub"></p></div>
        <div class="row"><label style="width:150px">Speed limit (MB/s)<input id="now-speed" type="number" min="0" step="0.5" placeholder="0 = unlimited"></label><button class="btn ghost small" id="now-apply">Apply</button><button class="btn danger small" id="now-cancel">Cancel run</button></div></div>
        <div class="bar" style="margin:14px 0 8px"><i id="now-bar"></i></div><div class="row between"><span id="now-stats" class="muted"></span><span id="now-eta" class="muted"></span></div><div id="now-files" style="margin-top:12px"></div>`;
      $("now-apply").addEventListener("click", async () => { const r = await call("set_speed", Number($("now-speed").value || 0)); if (r.ok) toast("Speed limit changed for this run", "ok"); });
      $("now-cancel").addEventListener("click", async () => { if (await ask("Cancel the running download?", "Cancel run", true)) call("cancel_run"); });
    }
  }
  if (!run) return;
  const pct = run.total_bytes ? Math.min(100, 100 * run.bytes / run.total_bytes) : 0;
  $("now-title").innerHTML = `${esc(run.job_name)} ${badge(run.state)} ${run.dry_run ? '<span class="badge">Dry run</span>' : ""}`;
  $("now-sub").textContent = `${run.mode === "move" ? "Move" : "Copy"} - started ${fmtTime(run.started)} - limit ${run.bwlimit}`;
  $("now-bar").style.width = `${pct}%`;
  $("now-stats").textContent = `${fmtBytes(run.bytes)} of ${fmtBytes(run.total_bytes)} - ${fmtSpeed(run.speed)} - ${run.files_done} file(s) done${run.errors ? ` - ${run.errors} error(s)` : ""}`;
  $("now-eta").textContent = run.eta != null ? `${fmtDur(run.eta)} left` : "";
  $("now-files").innerHTML = run.transferring.map(item => `<div style="margin:6px 0"><div class="row between"><span class="mono">${esc(item.name)}</span><span class="muted">${item.percentage}% - ${fmtSpeed(item.speed)} - ${fmtBytes(item.size)}</span></div><div class="bar thin"><i style="width:${item.percentage}%"></i></div></div>`).join("");
}
document.addEventListener("click", async event => {
  const run = event.target.closest("[data-run]");
  if (!run) return;
  const result = await call("run_job", run.dataset.run);
  if (result.ok) toast(result.queued_behind ? `Queued behind ${result.queued_behind}` : "Started", "ok");
  poll();
});

// ------------------------------------------------------------------------------------------------------ jobs
async function loadJobs() {
  const result = await api.list_jobs();
  S.jobs = result.jobs || [];
  $("jobs-list").innerHTML = S.jobs.length ? S.jobs.map(jobCard).join("") : '<div class="card empty">No jobs yet. Click <b>+ New job</b>.</div>';
}
function speedText(job) {
  if (job.speed.timetable) return `timetable ${esc(job.speed.timetable)}`;
  return job.speed.limit_mbps > 0 ? `${job.speed.limit_mbps} MB/s` : "no limit";
}
function jobCard(job) {
  return `<div class="card job" data-job="${esc(job.id)}">
    <div class="top"><label class="switch" title="Enable or pause"><input type="checkbox" data-toggle="${esc(job.id)}" ${job.enabled ? "checked" : ""}><i></i></label>
      <h2>${esc(job.name)}</h2><span class="badge ${job.mode === "move" ? "warn" : ""}">${job.mode === "move" ? "Move (deletes on Nextcloud after download)" : "Copy"}</span>${job.dry_run ? '<span class="badge">Dry run</span>' : ""}${job.running ? badge("running") : ""}</div>
    <div class="path">Nextcloud: /${esc(job.source)} &rarr; ${esc(job.dest)}</div>
    <div class="kv"><span>${esc(job.schedule_text)}</span><span>Speed: ${speedText(job)}</span><span>${job.transfers} file(s) at once, ${job.streams} stream(s)</span><span>${job.next_run && job.enabled ? `Next: ${fmtTime(job.next_run)} (${fmtIn(job.next_run)})` : "Not scheduled"}</span>
      <span>${job.last_run ? `Last: ${esc(STATE_LABEL[job.last_run.state] || job.last_run.state)} ${fmtTime(job.last_run.started)}` : "Never run"}</span></div>
    <div class="row"><button class="btn small" data-run="${esc(job.id)}" ${job.running ? "disabled" : ""}>Run now</button><button class="btn ghost small" data-dry="${esc(job.id)}">Dry run</button>
      <button class="btn ghost small" data-edit="${esc(job.id)}">Edit</button><button class="btn ghost small" data-dup="${esc(job.id)}">Duplicate</button><button class="btn ghost small" data-del="${esc(job.id)}">Delete</button></div></div>`;
}
document.addEventListener("click", async event => {
  const t = event.target;
  const edit = t.closest("[data-edit]"), dup = t.closest("[data-dup]"), del = t.closest("[data-del]"), dry = t.closest("[data-dry]");
  if (edit) openEditor(S.jobs.find(job => job.id === edit.dataset.edit));
  if (dup) { const r = await call("duplicate_job", dup.dataset.dup); if (r.ok) { toast("Duplicated (paused)", "ok"); loadJobs(); } }
  if (dry) { const r = await call("run_job", dry.dataset.dry, true); if (r.ok) toast("Dry run started: nothing will be transferred", "ok"); poll(); }
  if (del && await ask("Delete this job? Its history is kept.", "Delete", true)) { await call("delete_job", del.dataset.del); loadJobs(); }
});
document.addEventListener("change", async event => {
  const toggle = event.target.closest("[data-toggle]");
  if (toggle) { await call("set_job_enabled", toggle.dataset.toggle, toggle.checked); loadJobs(); }
});
$("job-new").addEventListener("click", () => openEditor(null));

function blankJob(source = "") {
  return {id: "", name: "", enabled: true, source, mode: "copy", dest: "", schedule: {days: [0, 1, 2, 3, 4, 5, 6], time: "02:00", stop_by: ""}, catch_up: true, catch_up_hours: 12,
          speed: {limit_mbps: 0, timetable: ""}, transfers: 1, streams: 4, min_age_minutes: 5, excludes: [], includes: [], checksum: false, dry_run: false, delete_empty_dirs: true, retries: 3};
}
function openEditor(job, presetSource = "") {
  const isNew = !job;
  job = job ? structuredClone(job) : blankJob(presetSource);
  const overlay = modal(`<div class="modal-head"><h2>${isNew ? "New job" : "Edit job"}</h2><button class="close" data-close>&times;</button></div>
  <div class="form">
    <label>Name<input id="je-name" value="${esc(job.name)}" placeholder="Night pull" maxlength="60"></label>
    <label>Nextcloud folder to download<div class="row" style="flex-wrap:nowrap"><input id="je-source" value="${esc(job.source)}" placeholder="e.g. Documents/Photos"><button class="btn ghost" id="je-browse" type="button">Browse Nextcloud...</button></div></label>
    <label>Destination folder on this PC<div class="row" style="flex-wrap:nowrap"><input id="je-dest" value="${esc(job.dest)}" placeholder="D:\\Downloads\\Nextcloud"><button class="btn ghost" id="je-pick" type="button">Choose...</button></div><small id="je-destinfo"></small></label>
    <div><h3>What to do with the files</h3><div class="seg" id="je-mode"><button type="button" data-mode="copy" class="${job.mode === "copy" ? "on" : ""}">Copy (keep on Nextcloud)</button><button type="button" data-mode="move" class="${job.mode === "move" ? "on" : ""}">Move (delete on Nextcloud after a verified download)</button></div></div>
    <div><h3>When</h3><div class="chips" id="je-days">${DAYS.map((d, i) => `<button type="button" class="chip ${job.schedule.days.includes(i) ? "on" : ""}" data-day="${i}">${d}</button>`).join("")}</div></div>
    <div class="cols"><label>Start time<input id="je-time" type="time" value="${esc(job.schedule.time)}"></label>
      <label>Stop by <small>(optional, ends the run)</small><input id="je-stop" type="time" value="${esc(job.schedule.stop_by)}"></label>
      <label>Speed limit (MB/s) <small>0 = unlimited</small><input id="je-speed" type="number" min="0" step="0.5" value="${job.speed.limit_mbps}"></label></div>
    <label class="check"><input id="je-catchup" type="checkbox" ${job.catch_up ? "checked" : ""}><span>Catch up if the PC was off or asleep at start time<small>Runs late, up to <input id="je-catchhours" type="number" min="0.5" max="72" step="0.5" value="${job.catch_up_hours}" style="width:70px;height:26px;display:inline-block"> hours after the scheduled time. Otherwise the run is recorded as missed.</small></span></label>
    <details><summary><b>Advanced</b></summary><div class="form" style="margin-top:12px">
      <div class="cols"><label>Files at once<input id="je-transfers" type="number" min="1" max="8" value="${job.transfers}"></label><label>Streams per file<input id="je-streams" type="number" min="1" max="16" value="${job.streams}"></label>
        <label>Skip files newer than (minutes)<input id="je-minage" type="number" min="0" max="1440" value="${job.min_age_minutes}"></label><label>Retries<input id="je-retries" type="number" min="0" max="10" value="${job.retries}"></label></div>
      <div class="cols"><label>Exclude patterns <small>one per line, e.g. *.log. An exclude always wins over an include.</small><textarea id="je-excludes">${esc(job.excludes.join("\n"))}</textarea></label><label>Only include <small>one per line, e.g. *.mkv. Everything else is skipped.</small><textarea id="je-includes">${esc(job.includes.join("\n"))}</textarea></label></div>
      <label>Speed timetable <small>replaces the limit above, e.g. 08:00,2M 23:00,off (checked when you save)</small><input id="je-timetable" value="${esc(job.speed.timetable)}"></label>
      <label class="check"><input id="je-checksum" type="checkbox" ${job.checksum ? "checked" : ""}><span>Compare checksums<small>Slower; use if the server provides them.</small></span></label>
      <label class="check"><input id="je-emptydirs" type="checkbox" ${job.delete_empty_dirs ? "checked" : ""}><span>Remove empty folders on Nextcloud after a move</span></label>
      <label class="check"><input id="je-dry" type="checkbox" ${job.dry_run ? "checked" : ""}><span>Always dry run<small>Lists what would happen, transfers nothing.</small></span></label></div></details>
    <div class="row"><button class="btn" id="je-save">Save job</button><button class="btn ghost" data-close>Cancel</button></div>
  </div>`, true);
  let mode = job.mode;
  overlay.querySelectorAll("[data-mode]").forEach(b => b.addEventListener("click", () => { mode = b.dataset.mode; overlay.querySelectorAll("[data-mode]").forEach(x => x.classList.toggle("on", x === b)); }));
  overlay.querySelectorAll("[data-day]").forEach(b => b.addEventListener("click", () => b.classList.toggle("on")));
  overlay.querySelector("#je-browse").addEventListener("click", () => pickRemote($("je-source").value, path => { $("je-source").value = path; if (!$("je-name").value) $("je-name").value = path.split("/").pop() || "Nextcloud"; }));
  overlay.querySelector("#je-pick").addEventListener("click", async () => { const r = await call("pick_folder", $("je-dest").value); if (r.ok && r.path) { $("je-dest").value = r.path; checkDest(); } });
  const checkDest = async () => {
    const value = $("je-dest").value.trim();
    if (!value) { $("je-destinfo").textContent = ""; return; }
    const r = await api.test_destination(value);
    $("je-destinfo").textContent = r.ok ? `${r.exists ? "Folder exists" : "Folder will be created"}${r.writable ? "" : " - NOT writable"}${r.free_bytes != null ? ` - ${fmtBytes(r.free_bytes)} free` : ""}` : (r.error || "");
  };
  overlay.querySelector("#je-dest").addEventListener("change", checkDest);
  checkDest();
  overlay.querySelector("#je-save").addEventListener("click", async () => {
    const lines = id => $(id).value.split("\n").map(x => x.trim()).filter(Boolean);
    const payload = {id: job.id, name: $("je-name").value, enabled: job.enabled, source: $("je-source").value, mode, dest: $("je-dest").value,
      schedule: {days: [...overlay.querySelectorAll("[data-day].on")].map(b => Number(b.dataset.day)), time: $("je-time").value || "02:00", stop_by: $("je-stop").value || ""},
      catch_up: $("je-catchup").checked, catch_up_hours: Number($("je-catchhours").value || 12), speed: {limit_mbps: Number($("je-speed").value || 0), timetable: $("je-timetable").value},
      transfers: Number($("je-transfers").value || 1), streams: Number($("je-streams").value || 4), min_age_minutes: Number($("je-minage").value || 0), retries: Number($("je-retries").value || 0),
      excludes: lines("je-excludes"), includes: lines("je-includes"), checksum: $("je-checksum").checked, delete_empty_dirs: $("je-emptydirs").checked, dry_run: $("je-dry").checked};
    if (mode === "move" && !payload.dry_run && !await ask("Move deletes the files on Nextcloud after they are downloaded and verified. Use this job in Move mode?", "Yes, use Move")) return;
    const r = await call("save_job", payload);
    if (r.ok) { overlay.remove(); toast("Job saved", "ok"); if (S.page === "jobs") loadJobs(); else if (S.page === "dashboard") loadDashboard(); }
  });
}

// ------------------------------------------------------------------------------ Nextcloud folder picker
function pickRemote(startPath, onPick) {
  const overlay = modal(`<div class="modal-head"><h2>Choose a Nextcloud folder</h2><button class="close" data-close>&times;</button></div><div class="crumbs" id="pk-crumbs"></div><div class="list" id="pk-list"></div>
    <div class="row between"><span class="muted">Selected: <b id="pk-sel">/</b></span><div class="row"><button class="btn" id="pk-ok">Use this folder</button><button class="btn ghost" data-close>Cancel</button></div></div>`, false, 40);
  let current = String(startPath || "").split("/").slice(0, -0).join("/");
  const load = async path => {
    const r = await call("browse", path);
    if (!r.ok) return;
    current = r.path;
    $("pk-sel").textContent = "/" + current;
    $("pk-crumbs").innerHTML = crumbsHtml(current);
    $("pk-list").innerHTML = r.entries.filter(e => e.is_dir).map(e => `<div class="item dir" data-path="${esc(e.path)}">&#128193; <b>${esc(e.name)}</b><span class="size">${fmtBytes(e.size)}</span></div>`).join("") || '<div class="empty">No sub-folders here.</div>';
  };
  overlay.addEventListener("click", event => { const d = event.target.closest("[data-path]"); if (d) load(d.dataset.path); const c = event.target.closest("[data-crumb]"); if (c) load(c.dataset.crumb); });
  overlay.querySelector("#pk-ok").addEventListener("click", () => { overlay.remove(); onPick(current); });
  load(current);
}
function crumbsHtml(path) {
  const parts = path ? path.split("/") : [];
  let acc = "";
  return `<button data-crumb="">Files</button>` + parts.map(part => { acc += (acc ? "/" : "") + part; return `/<button data-crumb="${esc(acc)}">${esc(part)}</button>`; }).join("");
}

// ----------------------------------------------------------------------------------------------- files page
async function loadFiles(path = S.filesPath) {
  const r = await api.browse(path);
  if (!r.ok) { $("files-list").innerHTML = `<div class="empty">${esc(r.error || "Could not load")}</div>`; return; }
  S.filesPath = r.path;
  $("files-crumbs").innerHTML = crumbsHtml(r.path);
  $("files-list").innerHTML = r.entries.map(e => `<div class="item ${e.is_dir ? "dir" : ""}" ${e.is_dir ? `data-fpath="${esc(e.path)}"` : ""}>${e.is_dir ? "&#128193;" : "&#128196;"} <span>${esc(e.name)}</span><span class="size">${fmtBytes(e.size)}${e.modified ? ` - ${fmtTime(e.modified)}` : ""}</span></div>`).join("") || '<div class="empty">Empty folder.</div>';
}
$("page-files").addEventListener("click", event => {
  const d = event.target.closest("[data-fpath]"); if (d) loadFiles(d.dataset.fpath);
  const c = event.target.closest("[data-crumb]"); if (c) loadFiles(c.dataset.crumb);
});
$("files-make-job").addEventListener("click", () => openEditor(null, S.filesPath));

// -------------------------------------------------------------------------------------------------- history
async function loadHistory() {
  const jobs = await api.list_jobs();
  S.jobs = jobs.jobs || [];
  $("history-job").innerHTML = `<option value="">All jobs</option>` + S.jobs.map(j => `<option value="${esc(j.id)}" ${j.id === S.historyJob ? "selected" : ""}>${esc(j.name)}</option>`).join("");
  const r = await api.history(150, 0, S.historyJob);
  const runs = r.runs || [];
  $("history-table").innerHTML = runs.length ? `<table><thead><tr><th>Started</th><th>Job</th><th>Result</th><th class="num">Files</th><th class="num">Size</th><th class="num">Avg speed</th><th class="num">Time</th><th>How</th></tr></thead><tbody>${runs.map(run => `
    <tr class="click" data-runid="${run.id}"><td>${fmtTime(run.started)}</td><td>${esc(run.job_name)} ${run.dry_run ? '<span class="badge">dry</span>' : ""}</td><td>${badge(run.state)}</td>
    <td class="num">${run.files}</td><td class="num">${fmtBytes(run.bytes)}</td><td class="num">${run.avg_speed ? fmtSpeed(run.avg_speed) : "-"}</td><td class="num">${duration(run)}</td><td class="muted">${esc(run.trigger)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No runs yet.</div>';
}
$("history-job").addEventListener("change", event => { S.historyJob = event.target.value; loadHistory(); });
$("history-table").addEventListener("click", event => { const row = event.target.closest("[data-runid]"); if (row) openRun(Number(row.dataset.runid)); });
async function openRun(id) {
  const [detail, log] = await Promise.all([api.run_detail(id), api.run_log(id, 600)]);
  if (!detail.ok) return;
  const run = detail.run;
  const overlay = modal(`<div class="modal-head"><h2>${esc(run.job_name)} ${badge(run.state)}</h2><button class="close" data-close>&times;</button></div>
    <div class="kv"><span>${fmtTime(run.started)} &rarr; ${fmtTime(run.finished)} (${duration(run) || "-"})</span><span>${esc(run.trigger)}</span><span>${run.mode}</span><span>${run.files} file(s), ${fmtBytes(run.bytes)}</span>${run.deleted ? `<span>${run.deleted} deleted on Nextcloud</span>` : ""}${run.errors ? `<span>${run.errors} error(s)</span>` : ""}</div>
    <div class="path">/${esc(run.source)} &rarr; ${esc(run.dest)}</div>${run.message ? `<div class="notice ${["failed", "interrupted"].includes(run.state) ? "bad" : "info"}">${esc(run.message)}</div>` : ""}
    <h3>Files</h3><div class="list">${detail.files.length ? `<table><thead><tr><th>Name</th><th class="num">Size</th><th>Result</th><th class="num">Speed</th></tr></thead><tbody>${detail.files.map(f => `<tr><td class="mono">${esc(f.name)}${f.error ? `<div class="hint">${esc(f.error)}</div>` : ""}</td><td class="num">${fmtBytes(f.size)}</td><td>${badge(f.status)}${f.deleted ? ' <span class="badge">deleted on Nextcloud</span>' : ""}${f.status === "ok" && f.error ? ' <span class="badge warn">not deleted on Nextcloud</span>' : ""}</td><td class="num">${f.speed ? fmtSpeed(f.speed) : "-"}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No files in this run.</div>'}</div>
    <div class="row between"><h3>Log</h3><div class="row"><select id="lg-level" style="width:140px;height:30px"><option value="">All messages</option><option value="warn">Warnings and errors</option><option value="error">Errors only</option></select></div></div>
    <div class="log" id="lg-body"></div><div class="row"><button class="btn ghost" id="lg-open" ${run.state === "running" ? "disabled" : ""}>Open destination folder</button><button class="btn ghost" data-close>Close</button></div>`, true);
  const lines = log.lines || [];
  const paint = () => {
    const level = overlay.querySelector("#lg-level").value;
    const pick = line => !level || (level === "error" ? ["error", "critical"].includes(line.level) : ["error", "critical", "warning", "notice"].includes(line.level));
    overlay.querySelector("#lg-body").innerHTML = lines.filter(pick).map(line => `<div class="l-${esc(line.level)}">${esc(line.time)} ${esc(line.level.toUpperCase())} ${esc(line.msg)}${line.object ? ` - ${esc(line.object)}` : ""}</div>`).join("") || '<span class="muted">No log lines.</span>';
  };
  overlay.querySelector("#lg-level").addEventListener("change", paint);
  overlay.querySelector("#lg-open").addEventListener("click", () => call("open_path", run.dest));
  paint();
}

// ------------------------------------------------------------------------------------------------ statistics
function barChart(data, key, fmt) {
  const W = 700, H = 190, L = 52, R = 8, T = 10, B = 24, max = Math.max(1, ...data.map(d => d[key]));
  const bw = (W - L - R) / data.length;
  const bars = data.map((d, i) => `<rect x="${L + i * bw + 1}" y="${T + (H - T - B) * (1 - d[key] / max)}" width="${Math.max(1, bw - 2)}" height="${(H - T - B) * d[key] / max}" rx="2" fill="${d.failed ? "#e0605a" : "#3265ce"}"><title>${d.date}: ${fmt(d[key])}${d.failed ? " (failed run)" : ""}</title></rect>`).join("");
  const grid = [0, .5, 1].map(f => `<line x1="${L}" x2="${W - R}" y1="${T + (H - T - B) * (1 - f)}" y2="${T + (H - T - B) * (1 - f)}" stroke="#e6ebf4"/><text x="${L - 6}" y="${T + (H - T - B) * (1 - f) + 3}" text-anchor="end">${fmt(max * f)}</text>`).join("");
  const labels = [0, Math.floor(data.length / 2), data.length - 1].map(i => `<text x="${L + i * bw + bw / 2}" y="${H - 6}" text-anchor="middle">${data[i].date.slice(5)}</text>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${grid}${bars}${labels}</svg>`;
}
function lineChart(data, key, fmt) {
  const W = 700, H = 170, L = 52, R = 8, T = 10, B = 24, max = Math.max(1, ...data.map(d => d[key])), bw = (W - L - R) / Math.max(1, data.length - 1);
  const pts = data.map((d, i) => [L + i * bw, T + (H - T - B) * (1 - d[key] / max), d]);
  const path = pts.filter(p => p[2][key] > 0).map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(" ");
  const grid = [0, .5, 1].map(f => `<line x1="${L}" x2="${W - R}" y1="${T + (H - T - B) * (1 - f)}" y2="${T + (H - T - B) * (1 - f)}" stroke="#e6ebf4"/><text x="${L - 6}" y="${T + (H - T - B) * (1 - f) + 3}" text-anchor="end">${fmt(max * f)}</text>`).join("");
  const dots = pts.filter(p => p[2][key] > 0).map(p => `<circle cx="${p[0]}" cy="${p[1]}" r="3" fill="#20775b"><title>${p[2].date}: ${fmt(p[2][key])}</title></circle>`).join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}">${grid}<path d="${path}" fill="none" stroke="#20775b" stroke-width="2"/>${dots}</svg>`;
}
async function loadStats() {
  const r = await api.stats(S.statsDays);
  if (!r.ok) return;
  const t = r.totals;
  const tiles = [["Downloaded", fmtBytes(t.bytes)], ["Files", t.files], ["Runs", `${t.ok_runs} ok - ${t.failed_runs} failed - ${t.partial_runs} partial`], ["Missed", t.missed_runs],
                 ["Success rate", t.success_rate == null ? "-" : `${t.success_rate}%`], ["Average speed", t.avg_speed ? fmtSpeed(t.avg_speed) : "-"], ["Deleted on Nextcloud", t.deleted], ["File errors", t.errors]];
  const heat = r.daily.map(d => `<i class="${d.failed ? "failed" : d.runs - d.missed > 0 ? "ok" : d.missed ? "missed" : "none"}" title="${d.date}: ${d.runs} run(s), ${fmtBytes(d.bytes)}${d.failed ? ", failed" : ""}${d.missed ? ", missed" : ""}"></i>`).join("");
  $("stats-body").innerHTML = `<div class="grid cols-4">${tiles.map(([l, v]) => `<div class="tile"><span>${l}</span><b style="font-size:18px">${v}</b></div>`).join("")}</div>
    <div class="card"><h2>Downloaded per day</h2>${barChart(r.daily, "bytes", fmtBytes)}</div>
    <div class="card"><h2>Average speed per day</h2>${lineChart(r.daily, "speed", fmtSpeed)}</div>
    <div class="card"><div class="row between"><h2>Run calendar</h2><div class="legend"><span><i style="background:#4fb68e"></i>ran</span><span><i style="background:#f0b45a"></i>missed</span><span><i style="background:#e0605a"></i>failed</span><span><i style="background:#e6ebf4"></i>nothing</span></div></div><div class="heat" style="margin-top:10px">${heat}</div></div>
    <div class="grid cols-2"><div class="card"><h2>Largest files</h2>${r.top_files.length ? `<table><tbody>${r.top_files.map(f => `<tr><td class="mono">${esc(f.name)}</td><td class="num">${fmtBytes(f.size)}</td><td class="num muted">${f.speed ? fmtSpeed(f.speed) : ""}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No files yet.</div>'}</div>
    <div class="card"><h2>By job</h2>${r.by_job.length ? `<table><thead><tr><th>Job</th><th class="num">Runs</th><th class="num">Files</th><th class="num">Size</th></tr></thead><tbody>${r.by_job.map(j => `<tr><td>${esc(j.job_name)}</td><td class="num">${j.runs}</td><td class="num">${j.files}</td><td class="num">${fmtBytes(j.bytes)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">No runs yet.</div>'}</div></div>`;
}
$("stats-range").addEventListener("click", event => {
  const b = event.target.closest("[data-days]"); if (!b) return;
  S.statsDays = Number(b.dataset.days);
  $("stats-range").querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
  loadStats();
});

// ------------------------------------------------------------------------------------------------- settings
async function loadSettings() {
  const [boot, s] = await Promise.all([api.bootstrap(), api.get_settings()]);
  S.boot = boot; S.settings = s.settings || {};
  const c = boot.connection || {}, st = S.settings;
  const login = S.login || {state: "idle"};
  const connection = c.connected ? `<div class="stack"><div><b>${esc(c.display_name || c.user)}</b> <span class="muted">(${esc(c.user)}) on ${esc(c.server)}</span></div>
      ${c.quota_total ? `<div class="bar thin" style="max-width:360px"><i style="width:${Math.min(100, 100 * (c.quota_used || 0) / c.quota_total)}%"></i></div><div class="hint">${fmtBytes(c.quota_used)} of ${fmtBytes(c.quota_total)} used on Nextcloud</div>` : ""}
      <div class="row"><button class="btn ghost" id="st-check">Check connection</button><button class="btn danger" id="st-disconnect">Disconnect</button></div><label class="check"><input type="checkbox" id="st-revoke" checked><span>Also remove NextPull's access on the Nextcloud server</span></label></div>`
    : login.state === "waiting" ? `<div class="stack"><div class="notice info">Waiting for you to approve the sign-in in your browser...</div><div class="hint">If the page did not open: <span class="mono">${esc(login.login_url || "")}</span></div><div><button class="btn ghost" id="st-cancel-login">Cancel</button></div></div>`
    : `<div class="stack">${login.state === "error" || login.state === "cancelled" ? `<div class="notice bad">${esc(login.message)}</div>` : ""}<label>Nextcloud address<input id="st-server" placeholder="cloud.example.com"></label><div><button class="btn" id="st-connect">Connect with your browser</button></div><div class="hint">You sign in on your Nextcloud page. NextPull only receives an app password (it never sees your real password) and stores it encrypted for this Windows user.</div></div>`;
  const toggle = (id, label, hint, on) => `<label class="check"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span>${label}<small>${hint}</small></span></label>`;
  $("settings-body").innerHTML = `<div class="card stack"><h2>Nextcloud connection</h2>${connection}</div>
    <div class="card stack"><h2>Behaviour</h2>
      ${toggle("st-tray", "Keep running in the tray when the window is closed", "Downloads continue in the background. Use Quit from the tray menu to stop NextPull.", st.close_to_tray)}
      ${toggle("st-notify", "Windows notifications", "When a run transfers files or fails.", st.notifications)}
      ${toggle("st-sleep", "Keep the PC awake during a download", "Prevents sleep only while a run is active.", st.prevent_sleep)}
      ${toggle("st-autostart", "Start with Windows", "Starts hidden in the tray when you sign in.", boot.autostart)}
      ${toggle("st-watchdog", "Restart automatically if it stops", "Adds a Windows scheduled task that starts NextPull every 5 minutes if it is not running.", boot.watchdog)}</div>
    <div class="card stack"><h2>rclone</h2><div id="st-rclone" class="${boot.rclone && boot.rclone.found ? "" : "notice bad"}">${boot.rclone && boot.rclone.found ? `<span class="badge ok">Found</span> ${esc(boot.rclone.version)} <span class="muted mono">${esc(boot.rclone.path)}</span>` : esc((boot.rclone && boot.rclone.message) || "rclone not found")}</div>
      <label>Custom rclone.exe path <small>leave empty to use tools\\rclone\\rclone.exe next to NextPull, or PATH</small><div class="row" style="flex-wrap:nowrap"><input id="st-rpath" value="${esc(st.rclone_path)}"><button class="btn ghost" id="st-rcheck">Check</button></div></label></div>
    <div class="card stack"><h2>Data</h2><div class="cols form"><div class="form" style="grid-template-columns:1fr 1fr;display:grid"><label>Keep history (days)<input id="st-hdays" type="number" min="7" value="${st.history_days}"></label><label>Keep log files (days)<input id="st-ldays" type="number" min="1" value="${st.log_days}"></label></div></div>
      <div class="hint mono">${esc(boot.data_dir)}</div>
      <div class="row"><button class="btn ghost" id="st-logs">Open logs folder</button><button class="btn ghost" id="st-export">Export jobs</button><button class="btn ghost" id="st-import">Import jobs</button><button class="btn danger" id="st-clear">Clear history</button></div></div>
    <div class="card"><h2>About</h2><p class="muted">NextPull <b>version ${esc((boot.app || {}).version)}</b> - scheduled Nextcloud downloads with rclone.</p><p class="muted">Made by René Girardi. Downloads are done by <a href="https://rclone.org" target="_blank" rel="noopener">rclone</a> (MIT license, © Nick Craig-Wood and contributors). NextPull is an independent project, not affiliated with rclone or Nextcloud.</p></div>`;
  wireSettings();
}
function wireSettings() {
  const on = (id, fn, evt = "click") => { const el = $(id); if (el) el.addEventListener(evt, fn); };
  const save = async () => {
    const r = await call("save_settings", {close_to_tray: $("st-tray").checked, notifications: $("st-notify").checked, prevent_sleep: $("st-sleep").checked, rclone_path: $("st-rpath").value, history_days: Number($("st-hdays").value), log_days: Number($("st-ldays").value)});
    if (r.ok) S.settings = r.settings;
  };
  ["st-tray", "st-notify", "st-sleep", "st-hdays", "st-ldays"].forEach(id => on(id, save, "change"));
  on("st-rpath", async () => { await save(); loadSettings(); }, "change");
  on("st-rcheck", async () => { await save(); loadSettings(); });
  on("st-autostart", async () => { const r = await call("set_autostart", $("st-autostart").checked); if (!r.ok) $("st-autostart").checked = !$("st-autostart").checked; }, "change");
  on("st-watchdog", async () => { const r = await call("set_watchdog", $("st-watchdog").checked); if (!r.ok) $("st-watchdog").checked = !$("st-watchdog").checked; }, "change");
  on("st-connect", async () => { const r = await call("connect_start", $("st-server").value); if (r.ok) { S.login = {state: "waiting", login_url: r.login_url, message: ""}; loadSettings(); } });
  on("st-cancel-login", async () => { await call("connect_cancel"); S.login = {state: "cancelled", message: "Sign-in cancelled"}; loadSettings(); });
  on("st-check", async () => { const r = await call("connection", true); if (r.ok) { toast("Connection is working", "ok"); loadSettings(); } });
  on("st-disconnect", async () => { if (!await ask("Disconnect from Nextcloud? Scheduled jobs will fail until you connect again.", "Disconnect", true)) return; const r = await call("disconnect", $("st-revoke").checked); if (r.ok) { S.login = null; toast(r.revoked ? "Disconnected and access removed on the server" : "Disconnected", "ok"); loadSettings(); } });
  on("st-logs", () => call("open_logs"));
  on("st-clear", async () => { if (await ask("Delete the whole run history?", "Delete", true)) { await call("clear_history"); toast("History cleared", "ok"); } });
  on("st-export", async () => { const r = await call("export_jobs"); if (r.ok) modal(`<div class="modal-head"><h2>Export jobs</h2><button class="close" data-close>&times;</button></div><p class="muted">Copy this text. It contains no passwords.</p><textarea style="min-height:260px" class="mono" readonly>${esc(r.text)}</textarea>`); });
  on("st-import", () => {
    const o = modal(`<div class="modal-head"><h2>Import jobs</h2><button class="close" data-close>&times;</button></div><p class="muted">Paste exported jobs. They are added as new jobs; nothing is overwritten.</p><textarea id="im-text" style="min-height:260px" class="mono"></textarea><div class="row"><button class="btn" id="im-go">Import</button><button class="btn ghost" data-close>Cancel</button></div>`);
    o.querySelector("#im-go").addEventListener("click", async () => { const r = await call("import_jobs", o.querySelector("#im-text").value); if (r.ok) { o.remove(); toast(`Imported ${r.imported} job(s)`, "ok"); } });
  });
}

// ---------------------------------------------------------------------------------------------- diagnostics
const CHECK_LABEL = {ok: "OK", warn: "Check", error: "Problem", skip: "Off"};
let logFilter = {level: "", query: ""};
async function loadDiagnostics() {
  const [info, log] = await Promise.all([api.diagnostics(7), api.app_log(250, logFilter.level, logFilter.query)]);
  if (!info.ok) return;
  const runs = info.runs.length ? info.runs.map(p => `<div class="problem"><div class="row between"><div><b>${esc(p.job)}</b> ${badge(p.state)} ${p.count > 1 ? `<span class="badge">${p.count} times</span>` : ""}</div><span class="muted">${fmtTime(p.last)}</span></div>
      <div>${esc(p.message || "No details were recorded")}</div><div class="fix">What to do: ${esc(p.hint)}</div>${p.run_id ? `<div><button class="btn ghost small" data-openrun="${p.run_id}">Open this run</button></div>` : ""}</div>`).join("")
    : '<div class="empty">No failed or partial runs in the last 7 days.</div>';
  const warnings = info.log.length ? `<table><tbody>${info.log.map(w => `<tr><td class="muted" style="width:130px">${esc(w.last)}</td><td><span class="badge ${w.level === "WARNING" ? "warn" : "error"}">${esc(w.level)}</span> ${w.count > 1 ? `<span class="badge">${w.count}x</span>` : ""} ${esc(w.message.split("\n")[0])}<div class="hint">${esc(w.hint)}</div></td></tr>`).join("")}</tbody></table>` : '<div class="empty">No warnings in the application log.</div>';
  const entries = (log.entries || []).map(e => `<div class="l-${esc(e.level.toLowerCase())}">${esc(e.time)} ${esc(e.level)} ${esc(e.name.replace("nextpull.", ""))}: ${esc(e.message)}</div>`).join("") || '<span class="muted">No log lines.</span>';
  $("dg-body").innerHTML = `<div class="card" id="dg-checkup" hidden></div>
    <div class="card"><div class="row between"><h2>Recent problems</h2>${info.unseen ? '<button class="btn ghost small" id="dg-ack">Mark as seen</button>' : ""}</div>${runs}</div>
    <div class="card"><h2>Warnings from the application log</h2>${warnings}</div>
    <div class="card stack"><h2>Send a report</h2>
      <p class="muted">If something keeps failing, create a report and send the file to whoever helps you. It holds the version, settings, recent runs and logs. <b>Passwords are never included.</b></p>
      <label class="check"><input type="checkbox" id="dg-names"><span>Include file, folder and account names<small>Leave off to replace them with codes (recommended). Turn on only if the helper needs the real names.</small></span></label>
      <label>What happened? <small>optional, a sentence helps</small><textarea id="dg-note" placeholder="For example: the night download stopped at 03:10 and nothing arrived."></textarea></label>
      <div class="row"><button class="btn" id="dg-report">Create report</button><span id="dg-report-out"></span></div></div>
    <div class="card stack"><div class="row between"><h2>Application log</h2><div class="row"><select id="dg-level" style="width:150px;height:30px"><option value="">Everything</option><option value="WARNING">Warnings and errors</option><option value="ERROR">Errors only</option></select>
      <input id="dg-query" placeholder="Search" style="width:160px;height:30px" value="${esc(logFilter.query)}"><button class="btn ghost small" id="dg-refresh">Refresh</button><button class="btn ghost small" id="dg-folder">Open logs folder</button></div></div>
      <div class="log" id="dg-log">${entries}</div><div class="hint mono">${esc(info.log_file)}</div></div>
    <div class="hint">NextPull ${esc(info.system.app_version)} - Python ${esc(info.system.python)} - ${esc(info.system.platform)}</div>`;
  $("dg-level").value = logFilter.level;
  const logEl = $("dg-log"); logEl.scrollTop = logEl.scrollHeight;
}
function renderCheckup(results) {
  const box = $("dg-checkup");
  const bad = results.filter(r => r.status === "error").length, warn = results.filter(r => r.status === "warn").length;
  box.hidden = false;
  box.innerHTML = `<div class="row between"><h2>Checkup</h2><span class="badge ${bad ? "error" : warn ? "warn" : "ok"}">${bad ? `${bad} problem(s)` : warn ? `${warn} to check` : "All good"}</span></div>${results.map(r => `<div class="check-row"><span><span class="badge ${r.status === "ok" ? "ok" : r.status === "error" ? "error" : r.status === "warn" ? "warn" : "skip"}">${CHECK_LABEL[r.status]}</span></span><div><b>${esc(r.title)}</b> <span class="muted">${esc(r.detail)}</span></div>${r.hint ? `<div class="fix">${esc(r.hint)}</div>` : ""}</div>`).join("")}`;
}
$("dg-test").addEventListener("click", async () => {
  const button = $("dg-test");
  button.disabled = true; button.textContent = "Checking...";
  const r = await call("self_test");
  button.disabled = false; button.textContent = "Run checkup";
  if (r.ok) { if (!$("dg-checkup")) await loadDiagnostics(); renderCheckup(r.results); }
});
$("dg-body").addEventListener("click", async event => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.dataset.openrun) openRun(Number(target.dataset.openrun));
  if (target.id === "dg-ack") { await call("ack_problems"); loadDiagnostics(); poll(); }
  if (target.id === "dg-refresh") { logFilter = {level: $("dg-level").value, query: $("dg-query").value}; loadDiagnostics(); }
  if (target.id === "dg-folder") call("open_logs");
  if (target.id === "dg-report") {
    target.disabled = true;
    const r = await call("export_report", $("dg-names").checked, $("dg-note").value);
    target.disabled = false;
    if (!r.ok) return;
    $("dg-report-out").innerHTML = `<span class="badge ok">Created</span> <span class="mono">${esc(r.path)}</span> <button class="btn ghost small" id="dg-reveal">Show in folder</button>`;
    $("dg-reveal").addEventListener("click", () => call("reveal_report", r.path));
  }
});
$("dg-body").addEventListener("change", event => { if (event.target.id === "dg-level") { logFilter = {level: event.target.value, query: $("dg-query").value}; loadDiagnostics(); } });

// Errors in this page itself go to the application log, so a broken screen shows up in the report.
function reportUiError(message, source, line) {
  try { if (api && api.log_ui_error) api.log_ui_error(String(message), String(source || ""), Number(line) || 0); } catch (e) { /* nothing more to do */ }
}
window.addEventListener("error", event => reportUiError(event.message, event.filename, event.lineno));
window.addEventListener("unhandledrejection", event => reportUiError(`Unhandled promise: ${(event.reason && event.reason.stack) || event.reason}`, "promise", 0));

// ------------------------------------------------------------------------------------------------ startup
async function start() {
  const params = new URLSearchParams(location.search);
  if (!window.pywebview || !window.pywebview.api || params.has("mock")) {
    await new Promise(resolve => { const s = document.createElement("script"); s.src = "mock-api.js"; s.onload = resolve; document.head.appendChild(s); });
  }
  api = window.pywebview.api;
  const boot = await api.bootstrap();
  S.boot = boot;
  $("foot-version").textContent = `Version ${(boot.app || {}).version || ""}`;
  $("brand-version").textContent = `v${(boot.app || {}).version || ""}`;
  show(params.get("page") || "dashboard");
  await poll();
  setInterval(poll, 1000);
}
if (window.pywebview && window.pywebview.api) start();
else {
  let started = false;
  const go = () => { if (!started) { started = true; start(); } };
  window.addEventListener("pywebviewready", go);
  // Development only: a plain browser (http dev server) or ?mock=1 uses the mock API. The real app is file:// and waits for pywebviewready.
  if (new URLSearchParams(location.search).has("mock")) go();
  else if (location.protocol.startsWith("http")) setTimeout(go, 1500);
}
