"use strict";
// NextPull UI: plain ES2020, no modules, no bundler. window.pywebview.api methods return {ok, error, ...}.
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const ic = name => `<svg class="i" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const TRIGGER = {schedule: "Scheduled", manual: "Run now", "catch-up": "Catch-up", quick: "Manual download"};
function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.dataset.theme = theme; else delete root.dataset.theme;
}
const S = {page: "dashboard", board: null, status: null, jobs: [], settings: {}, boot: {}, filesPath: "", filesConn: "", conns: [], statsDays: 30, historyJob: "", sig: {}, nowKey: "", login: null};
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
function fmtAgo(iso) {
  if (!iso) return "";
  const diff = (new Date() - new Date(iso)) / 1000;
  if (isNaN(diff)) return "";
  if (diff < 90) return "just now";
  if (diff < 3600) return `${Math.round(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.round(diff / 3600)} h ago`;
  return fmtTime(iso);
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

async function loadConns() {
  const r = await api.list_connections();
  S.conns = (r && r.connections) || [];
  return S.conns;
}
const connLabel = id => ((S.conns.find(c => c.id === id) || {}).label) || (id === "nextcloud" ? "Nextcloud" : "(missing connection)");

// ------------------------------------------------------------------------------------------------- plumbing
function toast(text, kind = "", action = null) {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  const span = document.createElement("span");
  span.textContent = text;
  el.appendChild(span);
  if (action) {
    const button = document.createElement("button");
    button.textContent = action.label;
    button.addEventListener("click", () => { el.remove(); if (action.go) show(action.go); });
    el.appendChild(button);
  }
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 7000 : action ? 7000 : 3500);
}
async function call(name, ...args) {
  let result;
  try { result = await api[name](...args); } catch (error) { result = {ok: false, error: String(error)}; reportUiError(`${name}: ${error}`, "bridge", 0); }
  if (result && result.ok === false && result.error && !result.quiet) toast(result.error, "error");
  return result || {ok: false};
}
function modal(html, wide = false, layer = 30, extra = "") {
  const overlay = document.createElement("div");
  overlay.className = "overlay";
  overlay.style.zIndex = layer;
  overlay.innerHTML = `<div class="modal ${wide ? "wide" : ""} ${extra}" role="dialog" aria-modal="true">${html}</div>`;
  const heading = overlay.querySelector("h2");
  if (heading) { heading.id = `dlg-${Math.random().toString(36).slice(2, 8)}`; overlay.firstChild.setAttribute("aria-labelledby", heading.id); }
  overlay.addEventListener("mousedown", event => { if (event.target === overlay) overlay.remove(); });
  overlay.querySelectorAll("[data-close]").forEach(button => button.addEventListener("click", () => overlay.remove()));
  $("modals").appendChild(overlay);
  return overlay;
}
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  const open = $("modals").lastElementChild;
  if (open) { open.querySelector("[data-close]") ? open.querySelector("[data-close]").click() : open.remove(); event.preventDefault(); }
  else if (S.page === "files" && F.sel.size) { F.sel.clear(); renderFiles(); }
});
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
  document.querySelectorAll(".nav").forEach(el => { const on = el.dataset.page === page; el.classList.toggle("active", on); if (on) el.setAttribute("aria-current", "page"); else el.removeAttribute("aria-current"); });
  document.getElementById("main").scrollTo(0, 0);
  ({dashboard: loadDashboard, jobs: loadJobs, files: loadFiles, history: loadHistory, stats: loadStats, diagnostics: loadDiagnostics, settings: loadSettings})[page]();
}
document.addEventListener("click", async event => {
  const upd = event.target.closest("[data-update]");
  if (upd) {
    event.preventDefault();
    if (upd.dataset.update === "page") call("open_release_page");
    if (upd.dataset.update === "install") openInstallDialog();
    if (upd.dataset.update === "backups") call("open_backups_folder");
    if (upd.dataset.update === "dismiss") { await call("dismiss_update_result"); S.sig.notice = null; poll(); }
    if (upd.dataset.update === "skip" && S.status && S.status.update) { await call("skip_update", S.status.update.version); S.sig.notice = null; poll(); }
    return;
  }
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
  $("foot-paused").hidden = !status.paused;
  $("nav-update").hidden = !status.update;
  if (was && was.running && !status.running && S.page === "dashboard") loadDashboard();
  else if (S.page === "dashboard") updateDashboard();
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
  const [jobs, board] = await Promise.all([api.list_jobs(), api.dashboard()]);
  S.jobs = jobs.jobs || [];
  S.board = board && board.ok ? board : {totals: {}, daily: [], recent: [], disks: [], today: {bytes: 0, files: 0}, week: {bytes: 0, files: 0}};
  S.sig = {};
  S.nowKey = "";
  renderBoard();
  updateDashboard();
  loadHealth(false);
}
function diskTile(disks) {
  if (!disks.length) return `<div class="tile"><span>Free space</span><b>-</b><small>No destination folder yet</small></div>`;
  const low = [...disks].sort((a, b) => a.free_bytes - b.free_bytes)[0];
  const usedPct = low.total_bytes ? Math.round(100 * (low.total_bytes - low.free_bytes) / low.total_bytes) : 0;
  const level = low.free_bytes < 5 * 2 ** 30 || usedPct > 95 ? "bad" : low.free_bytes < 20 * 2 ** 30 || usedPct > 88 ? "warn" : "good";
  return `<div class="tile"><span>Free space on ${esc(low.drive.replace(/[\\/]+$/, ""))}</span><b>${fmtBytes(low.free_bytes)}</b>${low.total_bytes ? `<div class="bar thin ${level}"><i style="width:${usedPct}%"></i></div>` : ""}<small>${level === "bad" ? "Almost full: downloads may fail" : level === "warn" ? "Getting full" : `${usedPct}% used`}</small></div>`;
}
function renderBoard() {
  const b = S.board, t = b.totals || {};
  $("dash-tiles").innerHTML = [
    `<div class="tile"><span>Today</span><b>${fmtBytes(b.today.bytes)}</b><small>${b.today.files} file${b.today.files === 1 ? "" : "s"} &middot; ${fmtBytes(b.week.bytes)} this week</small></div>`,
    `<div class="tile"><span>Last 30 days</span><b>${fmtBytes(t.bytes)}</b><small>${t.files ?? 0} files${t.avg_speed ? ` &middot; ${fmtSpeed(t.avg_speed)} on average` : ""}</small></div>`,
    `<div class="tile"><span>Reliability</span><b>${t.success_rate == null ? "-" : `${t.success_rate}%`}</b><small>${t.failed_runs ?? 0} failed &middot; ${t.partial_runs ?? 0} partial &middot; ${t.missed_runs ?? 0} missed</small></div>`,
    diskTile(b.disks || []),
  ].join("");
  const daily = b.daily || [], max = Math.max(1, ...daily.map(d => d.bytes));
  $("dash-chart-note").textContent = `${fmtBytes(daily.reduce((n, d) => n + d.bytes, 0))} in total`;
  $("dash-chart").innerHTML = daily.length ? `<div class="spark" role="img" aria-label="Downloaded per day over the last 14 days">${daily.map(d => `<div class="col" title="${esc(d.date)}: ${fmtBytes(d.bytes)}, ${d.files} file(s)${d.failed ? ", a run failed" : ""}"><i class="${d.bytes ? (d.failed ? "bad" : "") : "zero"}" style="height:${d.bytes ? Math.max(4, 100 * d.bytes / max) : 3}%"></i></div>`).join("")}</div>
    <div class="spark-days">${daily.map((d, i) => `<span>${i % 2 === 0 || daily.length < 8 ? new Date(d.date + "T12:00").toLocaleDateString([], {weekday: "narrow"}) : ""}</span>`).join("")}</div>` : '<div class="empty">No data yet.</div>';
  const rec = b.recent || [];
  $("dash-recent").innerHTML = rec.length ? rec.map(f => `<div class="rec"><span class="ic ${f.mode === "move" ? "move" : ""}">${ic(f.mode === "move" ? "move" : "file")}</span><div class="who"><b title="${esc(f.name)}">${esc(f.name.split("/").pop())}</b><span>${f.trigger === "quick" ? "Manual download" : esc(f.job_name)} &middot; ${fmtAgo(f.finished)}${f.mode === "move" ? " &middot; moved" : ""}</span></div><span class="sz">${fmtBytes(f.size)}</span><button class="icon-btn" data-open="${esc(f.dest)}" title="Open the folder" aria-label="Open the folder of ${esc(f.name)}">${ic("folder")}</button></div>`).join("")
    : `<div class="empty">${ic("inbox")}Nothing downloaded yet.</div>`;
}
async function loadHealth(force) {
  const box = $("dash-servers");
  if (!S.status || !S.status.connected) { box.innerHTML = `<div class="empty">${ic("server")}No server connected yet.<div style="margin-top:8px"><button class="btn small" data-goto="settings">Connect a server</button></div></div>`; return; }
  if (force) box.innerHTML = '<div class="empty">Checking...</div>';
  const r = await api.server_health(!!force);
  if (S.page !== "dashboard" || !r || !r.ok) return;
  box.innerHTML = r.servers.map(s => `<div class="sv"><span class="dot ${s.ok ? "on" : "off"}"></span><div class="grow"><b>${esc(s.label)}</b><span>${s.ok ? `Answers in ${s.ms < 1000 ? `${s.ms} ms` : `${(s.ms / 1000).toFixed(1)} s`}` : esc(s.error || "Not reachable")}</span></div><span class="badge ${s.ok ? "ok" : "error"}">${s.ok ? "Online" : "Problem"}</span></div>`).join("") || '<div class="empty">No server connected yet.</div>';
}
function welcomeHtml(status) {
  const have = status.connected;
  return `<div class="welcome"><div><h2>${have ? "You are connected" : "Welcome to NextPull"}</h2><p class="muted" style="margin-top:4px">${have ? "Now choose what to download." : "NextPull downloads files from your server to this PC, on a schedule or whenever you want one."}</p></div>
    <ol><li class="${have ? "muted" : ""}"><b>Connect your server</b> (Nextcloud, or a NextPush / WebDAV server) in Settings.${have ? " Done." : ""}</li><li><b>Get a file now</b> from the Files page, or <b>create a job</b> that downloads a folder every night.</li></ol>
    <div class="row">${have ? `<button class="btn big" data-goto="files">${ic("folder")}Open Files</button><button class="btn ghost big" data-goto="jobs">Create a job</button>` : `<button class="btn big" data-goto="settings">${ic("server")}Connect a server</button>`}</div></div>`;
}
function updateDashboard() {
  const status = S.status;
  if (!status) return;
  const notices = [];
  if (status.connected && !status.rclone_found) notices.push(`<div class="notice bad">${ic("alert")}<div>rclone was not found. Put <b>rclone.exe</b> in the <b>tools\\rclone</b> folder next to NextPull, or set its path in Settings.</div></div>`);
  if (status.update) notices.push(`<div class="notice info">${ic("arrow-up")}<div>NextPull <b>${esc(status.update.version)}</b> is available (you have ${esc((S.boot.app || {}).version)}). ${status.update.can_install ? '<a href="#" data-update="install"><b>Install now</b></a> &middot; ' : ""}<a href="#" data-update="page">What is new</a> &middot; <a href="#" data-update="skip">Skip this version</a></div></div>`);
  const result = status.update_result;
  if (result) notices.push(updateResultHtml(result));
  if (status.problem_count) notices.push(`<div class="notice">${ic("alert")}<div>${status.problem_count} recent run(s) had problems. <a href="#" data-goto="diagnostics">See what went wrong</a>.</div></div>`);
  const noticeHtml = notices.join("");
  if (S.sig.notice !== noticeHtml) { S.sig.notice = noticeHtml; $("dash-notice").innerHTML = noticeHtml; }
  updateNow(status);
  const jobsHtml = status.jobs.length ? status.jobs.map(job => `<div class="mini"><div class="grow"><b>${esc(job.name)} ${job.enabled ? "" : '<span class="badge">Paused</span>'}</b><span>${job.enabled && job.next_run ? `Next ${fmtTime(job.next_run)} (${fmtIn(job.next_run)})` : "Not scheduled"}${job.last_run ? ` &middot; last: ${esc(STATE_LABEL[job.last_run.state] || job.last_run.state)}, ${job.last_run.files} file(s)` : " &middot; never run"}</span></div>
    <button class="btn ghost small" data-run="${esc(job.id)}" ${job.running ? "disabled" : ""}>Run now</button></div>`).join("") : `<div class="empty">${ic("jobs")}No jobs yet.<div style="margin-top:8px"><button class="btn small" data-goto="jobs">Create a job</button></div></div>`;
  if (S.sig.jobs !== jobsHtml) { S.sig.jobs = jobsHtml; $("dash-jobs").innerHTML = jobsHtml; }
  const eventsHtml = status.events.length ? `<table><tbody>${status.events.slice(0, 12).map(e => `<tr><td class="muted" style="width:130px">${fmtTime(e.time)}</td><td>${e.level === "warn" || e.level === "error" ? `<span class="badge warn">!</span> ` : ""}${esc(e.text)}</td></tr>`).join("")}</tbody></table>` : '<div class="empty">Nothing yet.</div>';
  if (S.sig.events !== eventsHtml) { S.sig.events = eventsHtml; $("dash-events").innerHTML = eventsHtml; }
}
function updateNow(status) {
  const run = status.current;
  const queued = status.queue || [];
  const key = !status.connected && !S.jobs.length ? "welcome" : `${run ? `run:${run.run_id}` : status.starting ? "starting" : "idle"}|${status.paused ? "p" : ""}|${queued.map(q => q.job_name).join(",")}|${S.jobs.length}`;
  if (key !== S.nowKey) {   // rebuild the skeleton only when the situation changes, so typing in the speed box is never interrupted
    S.nowKey = key;
    const waiting = queued.length ? `<div class="hint">${queued.length} waiting: ${esc(queued.slice(0, 3).map(q => q.job_name).join(", "))}${queued.length > 3 ? "..." : ""}</div>` : "";
    if (key === "welcome") { $("dash-now").innerHTML = welcomeHtml(status); }
    else if (!run) {
      const upcoming = status.jobs.filter(job => job.enabled && job.next_run).sort((a, b) => a.next_run.localeCompare(b.next_run))[0];
      const title = status.starting ? "Starting..." : status.paused ? "Scheduled downloads are paused" : "All quiet";
      const sub = status.paused ? "Nothing runs on its own until you resume. Getting a file now and Run now still work."
        : upcoming ? `Next: <b>${esc(upcoming.name)}</b> ${fmtTime(upcoming.next_run)} (${fmtIn(upcoming.next_run)})` : status.jobs.length ? "No scheduled runs." : "Get a file now, or create a job to download on a schedule.";
      $("dash-now").innerHTML = `<div class="headline"><div class="who"><span class="orb ${status.paused ? "pause" : ""}">${ic(status.paused ? "pause" : "check")}</span><div><h2>${title}</h2><div class="sub">${sub}</div></div></div>
        <div class="actions">${status.connected ? `<button class="btn big" data-goto="files">${ic("download")}Get a file now</button>` : ""}${status.jobs.length || status.paused ? `<button class="btn ghost big" id="hero-pause">${ic(status.paused ? "play" : "pause")}${status.paused ? "Resume schedules" : "Pause schedules"}</button>` : ""}</div></div>${waiting}`;
      const pause = $("hero-pause");
      if (pause) pause.addEventListener("click", async () => { const r = await call("set_paused", !status.paused); if (r.ok) { toast(r.paused ? "Scheduled downloads paused" : "Scheduled downloads resumed", "ok"); await poll(); S.nowKey = ""; updateNow(S.status); } });
    } else {
      $("dash-now").innerHTML = `<div class="headline"><div class="who"><span class="orb live">${ic("download")}</span><div><h2 id="now-title"></h2><div class="sub" id="now-sub"></div></div></div>
        <div class="row"><label class="inline" style="width:190px">Limit MB/s<input id="now-speed" type="number" min="0" step="0.5" placeholder="0 = none"></label><button class="btn ghost small" id="now-apply">Apply</button><button class="btn danger small" id="now-cancel">Cancel</button></div></div>
        <div><div class="bar"><i id="now-bar"></i></div><div class="row between" style="margin-top:7px"><span id="now-stats" class="muted"></span><span id="now-eta" class="muted"></span></div></div><div class="files-now" id="now-files"></div>${waiting}`;
      $("now-apply").addEventListener("click", async () => { const r = await call("set_speed", Number($("now-speed").value || 0)); if (r.ok) toast("Speed limit changed for this run", "ok"); });
      $("now-cancel").addEventListener("click", async () => { if (await ask("Cancel the running download?", "Cancel run", true)) call("cancel_run"); });
    }
  }
  if (!run) return;
  const pct = run.total_bytes ? Math.min(100, 100 * run.bytes / run.total_bytes) : 0;
  $("now-title").innerHTML = `${esc(run.job_name)} ${badge(run.state)} ${run.dry_run ? '<span class="badge">Dry run</span>' : ""}`;
  $("now-sub").textContent = `${run.mode === "move" ? "Moving" : "Downloading"} - started ${fmtTime(run.started)} - limit ${run.bwlimit}`;
  $("now-bar").style.width = `${pct}%`;
  $("now-stats").textContent = `${fmtBytes(run.bytes)} of ${fmtBytes(run.total_bytes)} - ${fmtSpeed(run.speed)} - ${run.files_done} file(s) done${run.errors ? ` - ${run.errors} error(s)` : ""}`;
  $("now-eta").textContent = run.eta != null ? `${fmtDur(run.eta)} left` : "";
  $("now-files").innerHTML = run.transferring.map(item => `<div><div class="row between"><span class="mono">${esc(item.name)}</span><span class="muted">${item.percentage}% - ${fmtSpeed(item.speed)} - ${fmtBytes(item.size)}</span></div><div class="bar thin"><i style="width:${item.percentage}%"></i></div></div>`).join("");
}
document.addEventListener("click", async event => {
  const run = event.target.closest("[data-run]");
  if (run) {
    const result = await call("run_job", run.dataset.run);
    if (result.ok) toast(result.queued_behind ? `Queued behind ${result.queued_behind}` : "Started", "ok");
    poll();
  }
  const open = event.target.closest("[data-open]");
  if (open) call("open_path", open.dataset.open);
});
$("dash-health-refresh").addEventListener("click", () => loadHealth(true));

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
  return `<div class="card job ${job.enabled ? "" : "off"}" data-job="${esc(job.id)}">
    <div class="top"><label class="switch" title="Enable or pause"><input type="checkbox" data-toggle="${esc(job.id)}" ${job.enabled ? "checked" : ""}><i></i></label>
      <h2>${esc(job.name)}</h2><span class="badge ${job.mode === "move" ? "warn" : ""}">${job.mode === "move" ? `Move (deletes on ${job.connection && job.connection !== "nextcloud" ? "the server" : "Nextcloud"} after download)` : "Copy"}</span>${job.dry_run ? '<span class="badge">Dry run</span>' : ""}${job.running ? badge("running") : ""}</div>
    <div class="path">${esc(job.connection_label || "Nextcloud")}: /${esc(job.source)} &rarr; ${esc(job.dest)}</div>
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

function blankJob(source = "", connection = "") {
  return {id: "", name: "", enabled: true, connection: connection || (S.conns[0] && S.conns[0].id) || "nextcloud", source, mode: "copy", dest: "", schedule: {days: [0, 1, 2, 3, 4, 5, 6], time: "02:00", stop_by: ""}, catch_up: true, catch_up_hours: 12,
          speed: {limit_mbps: 0, timetable: ""}, transfers: 1, streams: 4, min_age_minutes: 5, excludes: [], includes: [], checksum: false, dry_run: false, delete_empty_dirs: true, retries: 3};
}
async function openEditor(job, presetSource = "", presetConn = "") {
  await loadConns();
  const isNew = !job;
  job = job ? structuredClone(job) : blankJob(presetSource, presetConn);
  job.connection = job.connection || "nextcloud";
  const connOptions = S.conns.some(c => c.id === job.connection) ? S.conns : [...S.conns, {id: job.connection, label: connLabel(job.connection)}];
  const connSelect = connOptions.length > 1 || job.connection !== "nextcloud" ? `<label>Connect to<select id="je-conn">${connOptions.map(c => `<option value="${esc(c.id)}" ${c.id === job.connection ? "selected" : ""}>${esc(c.label)}</option>`).join("")}</select></label>` : "";
  const currentConn = () => ($("je-conn") ? $("je-conn").value : job.connection);
  const overlay = modal(`<div class="modal-head"><h2>${isNew ? "New job" : "Edit job"}</h2><button class="close" data-close>&times;</button></div>
  <div class="form">
    <label>Name<input id="je-name" value="${esc(job.name)}" placeholder="Night pull" maxlength="60"></label>
    ${connSelect}
    <label>Folder to download<div class="row" style="flex-wrap:nowrap"><input id="je-source" value="${esc(job.source)}" placeholder="e.g. Documents/Photos"><button class="btn ghost" id="je-browse" type="button">Browse...</button></div></label>
    <label>Destination folder on this PC<div class="row" style="flex-wrap:nowrap"><input id="je-dest" value="${esc(job.dest)}" placeholder="D:\\Downloads\\NextPull"><button class="btn ghost" id="je-pick" type="button">Choose...</button></div><small id="je-destinfo"></small></label>
    <div><h3>What to do with the files</h3><div class="seg" id="je-mode"><button type="button" data-mode="copy" class="${job.mode === "copy" ? "on" : ""}">Copy (keep at the source)</button><button type="button" data-mode="move" class="${job.mode === "move" ? "on" : ""}">Move (delete on Nextcloud after a verified download)</button></div></div>
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
      <label class="check"><input id="je-emptydirs" type="checkbox" ${job.delete_empty_dirs ? "checked" : ""}><span>Remove empty folders at the source after a move<small>Not used with SFTPGo connections (that login may not remove folders).</small></span></label>
      <label class="check"><input id="je-dry" type="checkbox" ${job.dry_run ? "checked" : ""}><span>Always dry run<small>Lists what would happen, transfers nothing.</small></span></label></div></details>
    <div class="row"><button class="btn" id="je-save">Save job</button><button class="btn ghost" data-close>Cancel</button></div>
  </div>`, true);
  let mode = job.mode;
  overlay.querySelectorAll("[data-mode]").forEach(b => b.addEventListener("click", () => { mode = b.dataset.mode; overlay.querySelectorAll("[data-mode]").forEach(x => x.classList.toggle("on", x === b)); }));
  overlay.querySelectorAll("[data-day]").forEach(b => b.addEventListener("click", () => b.classList.toggle("on")));
  overlay.querySelector("#je-browse").addEventListener("click", () => pickRemote($("je-source").value, path => { $("je-source").value = path; if (!$("je-name").value) $("je-name").value = path.split("/").pop() || connLabel(currentConn()); }, currentConn()));
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
    const payload = {id: job.id, name: $("je-name").value, enabled: job.enabled, connection: currentConn(), source: $("je-source").value, mode, dest: $("je-dest").value,
      schedule: {days: [...overlay.querySelectorAll("[data-day].on")].map(b => Number(b.dataset.day)), time: $("je-time").value || "02:00", stop_by: $("je-stop").value || ""},
      catch_up: $("je-catchup").checked, catch_up_hours: Number($("je-catchhours").value || 12), speed: {limit_mbps: Number($("je-speed").value || 0), timetable: $("je-timetable").value},
      transfers: Number($("je-transfers").value || 1), streams: Number($("je-streams").value || 4), min_age_minutes: Number($("je-minage").value || 0), retries: Number($("je-retries").value || 0),
      excludes: lines("je-excludes"), includes: lines("je-includes"), checksum: $("je-checksum").checked, delete_empty_dirs: $("je-emptydirs").checked, dry_run: $("je-dry").checked};
    if (mode === "move" && !payload.dry_run && !await ask("Move deletes the files at the source after they are downloaded and verified. Use this job in Move mode?", "Yes, use Move")) return;
    const r = await call("save_job", payload);
    if (r.ok) { overlay.remove(); toast("Job saved", "ok"); if (S.page === "jobs") loadJobs(); else if (S.page === "dashboard") loadDashboard(); }
  });
}

// ------------------------------------------------------------------------------ Nextcloud folder picker
function pickRemote(startPath, onPick, conn = "nextcloud") {
  const overlay = modal(`<div class="modal-head"><h2>Choose a folder</h2><button class="close" data-close>&times;</button></div><div class="crumbs" id="pk-crumbs"></div><div class="list" id="pk-list"></div>
    <div class="row between"><span class="muted">Selected: <b id="pk-sel">/</b></span><div class="row"><button class="btn" id="pk-ok">Use this folder</button><button class="btn ghost" data-close>Cancel</button></div></div>`, false, 40);
  let current = String(startPath || "").split("/").slice(0, -0).join("/");
  const load = async path => {
    const r = await call("browse", path, conn);
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
  return `<button data-crumb="" class="${parts.length ? "" : "here"}">Server</button>` + parts.map((part, i) => { acc += (acc ? "/" : "") + part; return `<span class="sep">${ic("chevron")}</span><button data-crumb="${esc(acc)}" class="${i === parts.length - 1 ? "here" : ""}">${esc(part)}</button>`; }).join("");
}

// ----------------------------------------------------------------------------------------------- files page
const F = {entries: [], sel: new Map(), filter: "", sort: "name"};
const entryItem = e => ({path: e.path, is_dir: !!e.is_dir, size: e.size || 0});
async function loadFiles(path = S.filesPath) {
  await loadConns();
  if (!S.conns.length) { $("files-conn-wrap").hidden = true; $("files-list").innerHTML = `<div class="empty">${ic("server")}No server is connected yet.<div style="margin-top:8px"><button class="btn small" data-goto="settings">Connect a server</button></div></div>`; $("files-crumbs").innerHTML = ""; F.sel.clear(); updateSelbar(); return; }
  if (!S.conns.some(c => c.id === S.filesConn)) { S.filesConn = S.conns[0].id; path = ""; }
  $("files-conn-wrap").hidden = S.conns.length < 2;
  $("files-conn").innerHTML = S.conns.map(c => `<option value="${esc(c.id)}" ${c.id === S.filesConn ? "selected" : ""}>${esc(c.label)}</option>`).join("");
  $("files-list").innerHTML = '<div class="empty">Loading...</div>';
  const r = await api.browse(path, S.filesConn);
  if (!r.ok) { $("files-list").innerHTML = `<div class="empty">${ic("alert")}${esc(r.error || "Could not load")}</div>`; return; }
  if (r.path !== S.filesPath) { F.sel.clear(); F.filter = ""; $("files-filter").value = ""; }
  S.filesPath = r.path;
  F.entries = r.entries || [];
  $("files-crumbs").innerHTML = crumbsHtml(r.path);
  renderFiles();
}
function visibleFiles() {
  const q = F.filter.trim().toLowerCase();
  const list = F.entries.filter(e => !q || e.name.toLowerCase().includes(q));
  const by = {name: (a, b) => a.name.localeCompare(b.name, undefined, {numeric: true, sensitivity: "base"}), new: (a, b) => String(b.modified || "").localeCompare(String(a.modified || "")) || by_name(a, b), size: (a, b) => (b.size || 0) - (a.size || 0) || by_name(a, b)};
  const by_name = by.name;
  return list.sort((a, b) => (b.is_dir - a.is_dir) || by[F.sort](a, b));
}
function renderFiles() {
  const list = visibleFiles();
  const all = list.length > 0 && list.every(e => F.sel.has(e.path));
  const head = `<div class="fi head"><span><input type="checkbox" id="files-all" aria-label="Select everything shown" ${all ? "checked" : ""} ${list.length ? "" : "disabled"}></span><span></span><span>Name</span><span class="sz">Size</span><span class="tm">Modified</span><span></span></div>`;
  $("files-list").innerHTML = list.length ? head + list.map(e => `<div class="fi ${e.is_dir ? "dir" : ""} ${F.sel.has(e.path) ? "sel" : ""}" role="listitem">
      <span><input type="checkbox" data-pick="${esc(e.path)}" aria-label="Select ${esc(e.name)}" ${F.sel.has(e.path) ? "checked" : ""}></span><span class="ic">${ic(e.is_dir ? "folder" : "file")}</span>
      <span class="nm" title="${esc(e.name)}">${e.is_dir ? `<button data-fpath="${esc(e.path)}">${esc(e.name)}</button>` : esc(e.name)}</span>
      <span class="sz">${e.is_dir && !e.size ? "" : fmtBytes(e.size)}</span><span class="tm">${e.modified ? fmtTime(e.modified) : ""}</span>
      <button class="btn ghost small go" data-get="${esc(e.path)}" aria-label="Get ${esc(e.name)} now">${ic("download")}Get</button></div>`).join("")
    : `<div class="empty">${ic("inbox")}${F.entries.length ? "Nothing matches your filter." : "This folder is empty."}</div>`;
  updateSelbar();
}
function updateSelbar() {
  const picked = [...F.sel.values()];
  $("files-selbar").hidden = !picked.length;
  if (!picked.length) return;
  const files = picked.filter(e => !e.is_dir), folders = picked.length - files.length;
  $("sel-count").textContent = `${picked.length} selected`;
  $("sel-size").textContent = `${files.length ? fmtBytes(files.reduce((n, e) => n + (e.size || 0), 0)) : ""}${folders ? `${files.length ? " + " : ""}${folders} folder${folders === 1 ? "" : "s"}` : ""}`;
}
$("page-files").addEventListener("click", event => {
  const d = event.target.closest("[data-fpath]"); if (d) loadFiles(d.dataset.fpath);
  const c = event.target.closest("[data-crumb]"); if (c) loadFiles(c.dataset.crumb);
  const g = event.target.closest("[data-get]"); if (g) { const entry = F.entries.find(e => e.path === g.dataset.get); if (entry) openGetDialog([entry]); }
});
$("page-files").addEventListener("change", event => {
  const t = event.target;
  if (t.id === "files-all") { for (const e of visibleFiles()) t.checked ? F.sel.set(e.path, e) : F.sel.delete(e.path); renderFiles(); }
  else if (t.dataset.pick) { const entry = F.entries.find(e => e.path === t.dataset.pick); if (entry) t.checked ? F.sel.set(entry.path, entry) : F.sel.delete(entry.path); renderFiles(); }
});
$("files-filter").addEventListener("input", event => { F.filter = event.target.value; renderFiles(); });
$("files-sort").addEventListener("change", event => { F.sort = event.target.value; renderFiles(); });
$("files-refresh").addEventListener("click", () => loadFiles(S.filesPath));
$("sel-clear").addEventListener("click", () => { F.sel.clear(); renderFiles(); });
$("sel-get").addEventListener("click", () => openGetDialog([...F.sel.values()]));
$("files-make-job").addEventListener("click", () => openEditor(null, S.filesPath, S.filesConn));
$("files-conn").addEventListener("change", event => { S.filesConn = event.target.value; S.filesPath = ""; F.sel.clear(); loadFiles(""); });
document.addEventListener("keydown", event => {
  if (event.key === "/" && S.page === "files" && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !$("modals").children.length) { event.preventDefault(); $("files-filter").focus(); }
});

// "Get now": one or several files or folders, right away, outside any schedule. The user chooses Download (keep on the server) or Move.
async function openGetDialog(entries) {
  if (!entries.length) return;
  const conn = S.filesConn, items = entries.map(entryItem);
  const info = await call("quick_info", conn, items, "");
  if (!info.ok) return;
  const folders = entries.filter(e => e.is_dir).length, files = entries.length - folders;
  const total = entries.filter(e => !e.is_dir).reduce((n, e) => n + (e.size || 0), 0);
  const what = entries.length === 1 ? esc(entries[0].name) : `${entries.length} items`;
  const preview = entries.slice(0, 6).map(e => `<div>${ic(e.is_dir ? "folder" : "file")}<span title="${esc(e.name)}">${esc(e.name)}</span><span class="muted">${e.is_dir ? "folder" : fmtBytes(e.size)}</span></div>`).join("") + (entries.length > 6 ? `<div class="muted">and ${entries.length - 6} more</div>` : "");
  const overlay = modal(`<div class="modal-head"><h2>Get ${what} now</h2><button class="close" data-close aria-label="Close">${ic("x")}</button></div>
    ${entries.length > 1 || entries[0].is_dir ? `<div class="pick">${preview}</div>` : `<div class="hint">${fmtBytes(total)} &middot; from ${esc(connLabel(conn))}</div>`}
    <div class="choice" role="radiogroup" aria-label="What should happen to the files on the server?">
      <label class="opt"><input type="radio" name="gm-mode" value="copy" checked>${ic("download")}<div><b>Download</b><span>Keep the original on the server. Nothing is deleted there.</span></div></label>
      <label class="opt moveopt"><input type="radio" name="gm-mode" value="move">${ic("move")}<div><b>Move</b><span>Download, check that it arrived complete, then remove it from the server.</span></div></label>
    </div>
    <label>Save to<div class="row nowrap"><input id="gm-dest" value="${esc(info.dest)}" spellcheck="false"><button class="btn ghost" id="gm-pick" type="button">Choose...</button></div><small id="gm-info"></small></label>
    <div id="gm-extra" class="stack tight"></div>
    <label class="check" id="gm-def-wrap" hidden><input type="checkbox" id="gm-default"><span>Use this folder from now on for quick downloads</span></label>
    <div class="row"><button class="btn" id="gm-go">Download</button><button class="btn ghost" data-close>Cancel</button></div>`, false, 30, "narrow");
  const mode = () => overlay.querySelector('input[name="gm-mode"]:checked').value;
  const go = overlay.querySelector("#gm-go");
  let state = info, busy = false;
  const label = () => {
    const nouns = entries.length === 1 ? (entries[0].is_dir ? "folder" : "file") : files && !folders ? `${files} files` : `${entries.length} items`;
    go.textContent = mode() === "move" ? `Move ${nouns}` : `Download ${entries.length === 1 ? "" : nouns}`.trim();
    go.classList.toggle("danger", mode() === "move");
  };
  const refresh = async () => {
    const dest = $("gm-dest").value.trim();
    if (!dest) { $("gm-info").textContent = ""; return; }
    const r = await api.quick_info(conn, items, dest);
    if (!r || !r.ok) { $("gm-info").textContent = (r && r.error) || ""; return; }
    state = r;
    const moveBlocked = !!(r.move && !r.move.allowed), moveRadio = overlay.querySelector('input[value="move"]');
    moveRadio.disabled = moveBlocked;
    overlay.querySelector(".moveopt span").textContent = moveBlocked ? `Not available: your account can only download from ${r.move.blocked.join(", ")}.` : "Download, check that it arrived complete, then remove it from the server.";
    if (moveBlocked && moveRadio.checked) { overlay.querySelector('input[value="copy"]').checked = true; label(); }
    const need = total > 0 && r.free_bytes != null && total > r.free_bytes;
    $("gm-info").textContent = `${r.exists ? "Folder exists" : "Folder will be created"}${r.writable ? "" : " - NOT writable"}${r.free_bytes != null ? ` - ${fmtBytes(r.free_bytes)} free` : ""}`;
    $("gm-def-wrap").hidden = dest === r.default_dest;
    const conflicts = r.conflicts || [];
    $("gm-extra").innerHTML = `${need ? `<div class="notice bad">${ic("alert")}<div>Not enough free space: this needs ${fmtBytes(total)} and the disk has ${fmtBytes(r.free_bytes)}.</div></div>` : ""}${!r.writable && r.exists ? `<div class="notice bad">${ic("alert")}<div>NextPull cannot write to this folder. Choose another one.</div></div>` : ""}
      ${conflicts.length ? `<div class="notice">${ic("alert")}<div><b>${conflicts.length === 1 ? esc(conflicts[0].name) + " already exists" : `${conflicts.length} files already exist`} in this folder</b> with a different size.
        <div class="choice" style="margin-top:8px"><label class="check"><input type="radio" name="gm-conf" value="keep" checked><span>Keep mine<small>The existing file stays. The one on the server is not taken${mode() === "move" ? " and not removed" : ""}.</small></span></label>
        <label class="check"><input type="radio" name="gm-conf" value="replace"><span>Replace it<small>The old file is saved first in NextPull's "replaced" folder, so nothing is lost.</small></span></label></div></div></div>` : ""}`;
    go.disabled = busy || need || (!r.writable && r.exists);
  };
  overlay.addEventListener("change", event => { if (event.target.name === "gm-mode") { label(); refresh(); } });
  overlay.querySelector("#gm-dest").addEventListener("change", refresh);
  overlay.querySelector("#gm-pick").addEventListener("click", async () => { const r = await call("pick_folder", $("gm-dest").value); if (r.ok && r.path) { $("gm-dest").value = r.path; refresh(); } });
  go.addEventListener("click", async () => {
    const dest = $("gm-dest").value.trim();
    const conflict = (overlay.querySelector('input[name="gm-conf"]:checked') || {}).value || "replace";
    busy = true; go.disabled = true;
    const r = await call("fetch_now", conn, items, mode(), dest, conflict);
    busy = false;
    if (!r.ok) { go.disabled = false; return; }
    if (!$("gm-def-wrap").hidden && $("gm-default").checked) { const s = await call("save_settings", {quick_dest: dest}); if (s.ok) S.settings = s.settings; }
    overlay.remove();
    F.sel.clear(); if (S.page === "files") renderFiles();
    toast(r.queued_behind ? `Queued behind ${r.queued_behind}` : mode() === "move" ? "Moving now" : "Download started", "ok", {label: "Show progress", go: "dashboard"});
    poll();
  });
  label();
  refresh();
  go.focus();
}

// -------------------------------------------------------------------------------------------------- history
async function loadHistory() {
  const jobs = await api.list_jobs();
  S.jobs = jobs.jobs || [];
  $("history-job").innerHTML = `<option value="">Everything</option><option value="quick" ${S.historyJob === "quick" ? "selected" : ""}>Manual downloads</option>` + S.jobs.map(j => `<option value="${esc(j.id)}" ${j.id === S.historyJob ? "selected" : ""}>${esc(j.name)}</option>`).join("");
  const r = await api.history(150, 0, S.historyJob);
  const runs = r.runs || [];
  $("history-table").innerHTML = runs.length ? `<table><thead><tr><th>Started</th><th>Job</th><th>Result</th><th class="num">Files</th><th class="num">Size</th><th class="num">Avg speed</th><th class="num">Time</th><th>How</th></tr></thead><tbody>${runs.map(run => `
    <tr class="click" data-runid="${run.id}"><td>${fmtTime(run.started)}</td><td>${esc(run.job_name)} ${run.dry_run ? '<span class="badge">dry</span>' : ""}</td><td>${badge(run.state)}</td>
    <td class="num">${run.files}</td><td class="num">${fmtBytes(run.bytes)}</td><td class="num">${run.avg_speed ? fmtSpeed(run.avg_speed) : "-"}</td><td class="num">${duration(run)}</td><td class="muted">${esc(TRIGGER[run.trigger] || run.trigger)}</td></tr>`).join("")}</tbody></table>` : `<div class="empty">${ic("inbox")}No runs yet.</div>`;
}
$("history-job").addEventListener("change", event => { S.historyJob = event.target.value; loadHistory(); });
$("history-table").addEventListener("click", event => { const row = event.target.closest("[data-runid]"); if (row) openRun(Number(row.dataset.runid)); });
async function openRun(id) {
  const [detail, log] = await Promise.all([api.run_detail(id), api.run_log(id, 600)]);
  if (!detail.ok) return;
  const run = detail.run;
  const overlay = modal(`<div class="modal-head"><h2>${esc(run.job_name)} ${badge(run.state)}</h2><button class="close" data-close aria-label="Close">${ic("x")}</button></div>
    <div class="kv"><span>${fmtTime(run.started)} &rarr; ${fmtTime(run.finished)} (${duration(run) || "-"})</span><span>${esc(TRIGGER[run.trigger] || run.trigger)}</span><span>${run.mode === "move" ? "Move" : "Download"}</span><span>${run.files} file(s), ${fmtBytes(run.bytes)}</span>${run.deleted ? `<span>${run.deleted} deleted on Nextcloud</span>` : ""}${run.errors ? `<span>${run.errors} error(s)</span>` : ""}</div>
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
  const [boot, s, upd] = await Promise.all([api.bootstrap(), api.get_settings(), api.update_info(), loadConns()]);
  S.boot = boot; S.settings = s.settings || {}; S.upd = upd;
  const c = boot.connection || {}, st = S.settings;
  const login = S.login || {state: "idle"};
  const connection = c.connected ? `<div class="stack"><div><b>${esc(c.display_name || c.user)}</b> <span class="muted">(${esc(c.user)}) on ${esc(c.server)}</span></div>
      ${c.quota_total ? `<div class="bar thin" style="max-width:360px"><i style="width:${Math.min(100, 100 * (c.quota_used || 0) / c.quota_total)}%"></i></div><div class="hint">${fmtBytes(c.quota_used)} of ${fmtBytes(c.quota_total)} used on Nextcloud</div>` : ""}
      <div class="row"><button class="btn ghost" id="st-check">Check connection</button><button class="btn danger" id="st-disconnect">Disconnect</button></div><label class="check"><input type="checkbox" id="st-revoke" checked><span>Also remove NextPull's access on the Nextcloud server</span></label></div>`
    : login.state === "waiting" ? `<div class="stack"><div class="notice info">Waiting for you to approve the sign-in in your browser...</div><div class="hint">If the page did not open: <span class="mono">${esc(login.login_url || "")}</span></div><div><button class="btn ghost" id="st-cancel-login">Cancel</button></div></div>`
    : `<div class="stack">${login.state === "error" || login.state === "cancelled" ? `<div class="notice bad">${esc(login.message)}</div>` : ""}<label>Nextcloud address<input id="st-server" placeholder="cloud.example.com"></label><div><button class="btn" id="st-connect">Connect with your browser</button></div><div class="hint">You sign in on your Nextcloud page. NextPull only receives an app password (it never sees your real password) and stores it encrypted for this Windows user.</div></div>`;
  const toggle = (id, label, hint, on) => `<label class="check"><input type="checkbox" id="${id}" ${on ? "checked" : ""}><span>${label}<small>${hint}</small></span></label>`;
  $("settings-body").innerHTML = `<div class="card stack"><h2>Nextcloud connection</h2>${connection}</div>
    <div class="card stack"><div class="row between"><h2>Other servers (NextPush, SFTPGo, WebDAV)</h2><button class="btn ghost small" id="st-addserver">Add a server</button></div>
      ${serversHtml(S.conns)}
      <div class="hint">For a server you were given access to with a user name and password. Jobs can pull from it just like from Nextcloud. NextPull only reads and deletes (after a verified copy); it never uploads.</div></div>
    <div class="card stack"><h2>Downloads</h2>
      <label>Folder for quick downloads <small>Where "Get now" on the Files page puts files, unless you pick another folder in the dialog. Empty = the folder of your first job, else Downloads\\NextPull.</small><div class="row nowrap"><input id="st-qdest" value="${esc(st.quick_dest)}" placeholder="D:\\Downloads\\NextPull"><button class="btn ghost" id="st-qpick" type="button">Choose...</button></div></label>
      ${toggle("st-paused", "Pause all scheduled downloads", "Nothing runs on its own while this is on (holiday, maintenance). Get now and Run now still work.", st.paused)}</div>
    <div class="card stack"><h2>Appearance</h2><div class="seg" id="st-theme" role="group" aria-label="Colour theme">${[["system", "Same as Windows"], ["light", "Light"], ["dark", "Dark"]].map(([v, l]) => `<button type="button" data-theme="${v}" class="${(st.theme || "system") === v ? "on" : ""}">${l}</button>`).join("")}</div></div>
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
    <div class="card stack"><h2>Updates</h2><div id="st-upd">${updateHtml(S.upd)}</div>
      ${toggle("st-updates", "Check for updates automatically", "Asks GitHub for the latest NextPull release about every 6 hours. Nothing else is sent. NextPull never installs anything without you clicking.", st.check_updates)}
      <div class="row"><button class="btn ghost" id="st-upcheck">Check now</button></div></div>
    <div class="card"><h2>About</h2><p class="muted">NextPull <b>version ${esc((boot.app || {}).version)}</b> - scheduled Nextcloud downloads with rclone.</p><p class="muted">Made by René Girardi. Downloads are done by <a href="https://rclone.org" target="_blank" rel="noopener">rclone</a> (MIT license, © Nick Craig-Wood and contributors). NextPull is an independent project, not affiliated with rclone or Nextcloud.</p></div>`;
  wireSettings();
}
function updateResultHtml(result) {
  const links = ' <a href="#" data-update="dismiss">Dismiss</a>';
  if (result.ok) return `<div class="notice ok">NextPull was updated from ${esc(result.previous_version)} to <b>${esc(result.version)}</b>. Your settings from before the update are kept in the backups folder. <a href="#" data-update="backups">Open backups</a> -${links}</div>`;
  if (result.cancelled) return `<div class="notice">The update to ${esc(result.version)} was cancelled. Nothing was changed.${links}</div>`;
  if (result.restore_failed) return `<div class="notice bad"><b>The update to ${esc(result.version)} failed and the automatic restore did not finish.</b> ${esc(result.message)} Your settings are in the backups folder: <a href="#" data-update="backups">Open backups</a> -${links}</div>`;
  if (result.rolled_back) return `<div class="notice bad">The update to <b>${esc(result.version)}</b> did not work, so it was undone: NextPull ${esc(result.previous_version)} and your settings are back as they were. ${esc(result.message)}${links}</div>`;
  return `<div class="notice bad">The update to <b>${esc(result.version)}</b> was not installed and nothing was changed. ${esc(result.message)}${links}</div>`;
}
function installNotes(text) {
  const lines = String(text || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean).slice(0, 14);
  return lines.length ? `<div class="notes">${lines.map(line => /^[-*]\s+/.test(line) ? `<div class="li">${esc(line.replace(/^[-*]\s+/, ""))}</div>` : `<div class="hd">${esc(line.replace(/^#+\s*/, ""))}</div>`).join("")}</div>` : "";
}
async function openInstallDialog() {
  const info = await call("update_info");
  if (!info.ok || !info.available || !info.latest) return;
  const latest = info.latest;
  const overlay = modal(`<div class="modal-head"><h2>Install NextPull ${esc(latest.version)}?</h2><button class="btn ghost small" data-close>Close</button></div>
    <div class="muted">You have ${esc(info.current)}${latest.size ? ` - download about ${esc(fmtBytes(latest.size))}` : ""}.</div>
    ${installNotes(latest.notes)}
    <ul class="plain">
      <li><b>NextPull closes</b> and a small updater window takes over. A running download is stopped cleanly and starts again at its next scheduled time.</li>
      <li><b>Your settings are backed up first</b> (jobs, history, saved sign-ins), and checked.</li>
      <li>The new version is tested before it replaces the old one. <b>If anything goes wrong, the old version and your settings come back automatically.</b></li>
    </ul>
    <div class="row"><button class="btn" id="inst-go">Install and restart</button><button class="btn ghost" data-close>Not now</button></div>`, false, 40);
  overlay.querySelector("#inst-go").addEventListener("click", async () => {
    const button = overlay.querySelector("#inst-go");
    button.disabled = true; button.textContent = "Starting the updater...";
    let r = await call("install_update", false);
    if (r && r.busy) {
      overlay.remove();
      if (!await ask("A download is running right now. Update anyway? It is stopped cleanly and starts again at its next scheduled time.", "Stop it and update")) return;
      r = await call("install_update", true);
    }
    if (r && r.ok) { const body = overlay.querySelector(".modal"); if (body) body.innerHTML = '<h2>Handing over to the updater...</h2><div class="muted">NextPull is closing. The update window opens now, and NextPull reopens by itself when it is done.</div>'; }
    else if (overlay.isConnected) { button.disabled = false; button.textContent = "Install and restart"; }
  });
}
function updateHtml(u) {
  if (!u) return "";
  const when = u.checked ? `Last checked ${fmtTime(u.checked)}.` : "Not checked yet.";
  const state = u.error ? `<div class="notice">${esc(u.error)}</div>`
    : u.available ? `<div class="notice info">Version <b>${esc(u.latest.version)}</b> is available. <a href="#" data-update="page">What is new</a>${u.latest.can_install ? ' - <a href="#" data-update="install"><b>Install now</b></a>' : ""}</div>`
    : u.latest ? '<span class="badge ok">Up to date</span>' : "";
  return `<div>You have version <b>${esc(u.current)}</b>. ${when}</div>${state}`;
}
function serversHtml(conns) {
  const others = (conns || []).filter(c => c.type !== "nextcloud");
  if (!others.length) return '<div class="empty">No other server yet.</div>';
  return others.map(c => `<div class="srv"><div class="grow"><b>${esc(c.label)}</b> <span class="badge">${esc(c.type_label)}</span> <span class="badge ${c.secure ? "ok" : "warn"}">${c.secure ? "HTTPS" : "not encrypted (local network)"}</span>${c.has_access_key ? ' <span class="badge ok">access key</span>' : ""}<div class="hint mono">${esc(c.server)} - ${esc(c.user)}</div></div>
    <button class="btn ghost small" data-srv-test="${esc(c.id)}">Test</button><button class="btn ghost small" data-srv-del="${esc(c.id)}">Remove</button></div>`).join("");
}
function openAddServer() {
  const overlay = modal(`<div class="modal-head"><h2>Add a server</h2><button class="close" data-close>&times;</button></div>
  <div class="form">
    <label>Invite code <small>optional: paste it to fill in the address and access key</small><div class="row" style="flex-wrap:nowrap"><input id="as-invite" placeholder="nextpull-invite:..."><button class="btn ghost" id="as-useinvite" type="button">Use code</button></div></label>
    <label>Server address<div class="row" style="flex-wrap:nowrap"><input id="as-server" placeholder="pull.example.com"><button class="btn ghost" id="as-detect" type="button">Check</button></div><small id="as-found"></small></label>
    <div class="cols"><label>User name<input id="as-user" autocomplete="off"></label><label>Password<input id="as-pass" type="password" autocomplete="new-password"></label></div>
    <details><summary><b>Access key</b> <span class="muted">(only if the administrator gave you one)</span></summary><div class="cols" style="margin-top:10px"><label>Header name<input id="as-hname" placeholder="X-Pull-Key"></label><label>Key<input id="as-hvalue" type="password" autocomplete="off"></label></div></details>
    <label>Name for this connection <small>optional</small><input id="as-label" maxlength="60"></label>
    <div class="hint">NextPull tests the login before saving. The password and key are stored encrypted for this Windows user and never shown again.</div>
    <div class="row"><button class="btn" id="as-save">Test and save</button><button class="btn ghost" data-close>Cancel</button></div></div>`);
  const found = overlay.querySelector("#as-found");
  overlay.querySelector("#as-useinvite").addEventListener("click", async () => {
    const r = await call("read_invite", $("as-invite").value);
    if (!r.ok) return;
    $("as-server").value = r.server; if (r.user) $("as-user").value = r.user; if (r.label) $("as-label").value = r.label;
    $("as-hname").value = r.header_name || ""; $("as-hvalue").value = r.header_value || "";
    if (r.header_name) overlay.querySelector("details").open = true;
    found.textContent = "Invite code applied. Enter your password.";
  });
  overlay.querySelector("#as-detect").addEventListener("click", async () => {
    found.textContent = "Checking...";
    const r = await call("discover_server", $("as-server").value);
    found.textContent = !r.ok ? `${r.error || "No answer"}${r.hint ? " " + r.hint : ""}`
      : r.type === "nextcloud" ? "This is a Nextcloud server: connect to it with the Nextcloud card instead (browser sign-in)."
      : `${r.product || "WebDAV server"}${r.name ? ` "${r.name}"` : ""}${r.version ? " " + r.version : ""} found. Enter your user name and password.`;
    if (r.ok && r.type !== "nextcloud" && r.server) $("as-server").value = r.server;
  });
  overlay.querySelector("#as-save").addEventListener("click", async () => {
    const b = overlay.querySelector("#as-save"); b.disabled = true; b.textContent = "Testing...";
    const r = await call("add_connection", {server: $("as-server").value, user: $("as-user").value, password: $("as-pass").value, header_name: $("as-hname").value, header_value: $("as-hvalue").value, label: $("as-label").value});
    b.disabled = false; b.textContent = "Test and save";
    if (r.ok) { overlay.remove(); toast("Server added", "ok"); loadSettings(); }
  });
}
function wireSettings() {
  const addServer = $("st-addserver"); if (addServer) addServer.addEventListener("click", openAddServer);
  document.querySelectorAll("[data-srv-test]").forEach(b => b.addEventListener("click", async () => { b.disabled = true; const r = await call("test_connection", b.dataset.srvTest); b.disabled = false; if (r.ok) toast(`Connection works (${r.seconds}s). ${r.tls ? r.tls.summary : ""}`, r.tls && r.tls.encrypted === false && r.tls.scope === "internet" ? "error" : "ok"); }));
  document.querySelectorAll("[data-srv-del]").forEach(b => b.addEventListener("click", async () => { if (!await ask("Remove this server? Its saved password is deleted.", "Remove", true)) return; const r = await call("remove_connection", b.dataset.srvDel); if (r.ok) { toast("Removed", "ok"); loadSettings(); } }));
  const on = (id, fn, evt = "click") => { const el = $(id); if (el) el.addEventListener(evt, fn); };
  const save = async () => {
    const r = await call("save_settings", {close_to_tray: $("st-tray").checked, notifications: $("st-notify").checked, prevent_sleep: $("st-sleep").checked, rclone_path: $("st-rpath").value, quick_dest: $("st-qdest").value.trim(), history_days: Number($("st-hdays").value), log_days: Number($("st-ldays").value), check_updates: $("st-updates").checked});
    if (r.ok) S.settings = r.settings;
  };
  ["st-tray", "st-notify", "st-sleep", "st-hdays", "st-ldays", "st-updates", "st-qdest"].forEach(id => on(id, save, "change"));
  on("st-qpick", async () => { const r = await call("pick_folder", $("st-qdest").value); if (r.ok && r.path) { $("st-qdest").value = r.path; save(); } });
  on("st-paused", async () => { const r = await call("set_paused", $("st-paused").checked); if (!r.ok) $("st-paused").checked = !$("st-paused").checked; else { S.settings.paused = r.paused; poll(); } }, "change");
  document.querySelectorAll("#st-theme [data-theme]").forEach(b => b.addEventListener("click", async () => { const r = await call("save_settings", {theme: b.dataset.theme}); if (r.ok) { S.settings = r.settings; applyTheme(r.settings.theme); document.querySelectorAll("#st-theme button").forEach(x => x.classList.toggle("on", x === b)); } }));
  on("st-rpath", async () => { await save(); loadSettings(); }, "change");
  on("st-rcheck", async () => { await save(); loadSettings(); });
  on("st-autostart", async () => { const r = await call("set_autostart", $("st-autostart").checked); if (!r.ok) $("st-autostart").checked = !$("st-autostart").checked; }, "change");
  on("st-watchdog", async () => { const r = await call("set_watchdog", $("st-watchdog").checked); if (!r.ok) $("st-watchdog").checked = !$("st-watchdog").checked; }, "change");
  on("st-connect", async () => { const r = await call("connect_start", $("st-server").value); if (r.ok) { S.login = {state: "waiting", login_url: r.login_url, message: ""}; loadSettings(); } });
  on("st-cancel-login", async () => { await call("connect_cancel"); S.login = {state: "cancelled", message: "Sign-in cancelled"}; loadSettings(); });
  on("st-check", async () => { const r = await call("connection", true); if (r.ok) { toast(`Connection is working. ${r.tls ? r.tls.summary : ""}`, r.tls && r.tls.encrypted === false && r.tls.scope === "internet" ? "error" : "ok"); loadSettings(); } });
  on("st-disconnect", async () => { if (!await ask("Disconnect from Nextcloud? Scheduled jobs will fail until you connect again.", "Disconnect", true)) return; const r = await call("disconnect", $("st-revoke").checked); if (r.ok) { S.login = null; toast(r.revoked ? "Disconnected and access removed on the server" : "Disconnected", "ok"); loadSettings(); } });
  on("st-upcheck", async () => { const b = $("st-upcheck"); b.disabled = true; b.textContent = "Checking..."; const r = await call("check_update"); b.disabled = false; b.textContent = "Check now"; if (r.ok) { S.upd = r; $("st-upd").innerHTML = updateHtml(r); if (r.available) toast(`Version ${r.latest.version} is available`, "ok"); else if (!r.error) toast("You have the latest version", "ok"); } });
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
  applyTheme((boot.settings || {}).theme);
  await loadConns();
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
