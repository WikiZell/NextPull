"use strict";
// NextPull Update window. Plain ES2020. The window only draws what the updater reports; it never decides anything.
const $ = id => document.getElementById(id);
const esc = text => String(text ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const WEIGHTS = {wait: 5, backup: 5, download: 55, verify: 15, install: 10, restart: 10};
const REASSURE = {
  running: "Your settings are backed up first. If anything goes wrong, the old version comes back automatically.",
  success: "A copy of your settings from before the update is kept in the backups folder.",
  failed: "Your jobs, history and sign-ins were not lost.",
  cancelled: "Nothing was changed.",
};
const S = {sig: {}, last: null, countdown: null, countdownLeft: 0, interacted: false, notesFor: ""};
let api = null;

async function call(name, ...args) {
  try { return await api[name](...args); } catch (error) { return {ok: false, error: String(error)}; }
}

function overall(state) {
  let total = 0, done = 0;
  for (const step of state.steps) {
    const weight = WEIGHTS[step.key] || 10;
    total += weight;
    if (step.state === "done") done += weight;
    else if (step.state === "active") {
      const progress = state.progress;
      done += weight * (step.key === "download" && progress.total ? Math.min(progress.done / progress.total, 1) : 0.35);
    }
  }
  return Math.round(100 * done / total);
}

function inlineMarkdown(text) {
  return esc(text).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
}
function renderNotes(text) {
  const lines = String(text || "").split(/\r?\n/);
  let html = "", list = false;
  const close = () => { if (list) { html += "</ul>"; list = false; } };
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) { close(); continue; }
    const heading = line.match(/^#{1,6}\s+(.*)$/);
    const item = line.match(/^[-*]\s+(.*)$/);
    if (heading) { close(); html += `<h4>${inlineMarkdown(heading[1])}</h4>`; }
    else if (item) { if (!list) { html += "<ul>"; list = true; } html += `<li>${inlineMarkdown(item[1])}</li>`; }
    else { close(); html += `<p>${inlineMarkdown(line)}</p>`; }
  }
  close();
  return html || '<p class="none">No release notes were published for this version.</p>';
}

function setText(id, text) {
  const el = $(id);
  if (el.textContent !== text) el.textContent = text;
}

function render(state) {
  S.last = state;
  const shell = $("shell");
  const outcome = state.outcome || {};
  shell.dataset.phase = state.phase;
  shell.dataset.busy = String(state.phase === "running");
  shell.dataset.rolled = String(!!outcome.rolled_back);
  shell.dataset.restore = String(!outcome.restore_failed);
  const download = state.steps.find(step => step.key === "download");

  setText("v-old", state.release.current);
  setText("v-new", state.release.version);
  setText("headline", state.headline);
  setText("sub", state.sub);
  setText("reassure-text", REASSURE[state.phase] && !(state.phase === "failed" && outcome.restore_failed) ? REASSURE[state.phase] : (state.phase === "failed" ? "Open the backup folder to find your settings." : REASSURE.running));
  document.title = state.phase === "running" ? `Updating NextPull (${overall(state)}%)` : state.headline;

  const percent = state.phase === "success" ? 100 : overall(state);
  const ring = $("ring");
  ring.setAttribute("aria-valuenow", String(percent));
  $("ring-bar").style.strokeDashoffset = String(100 - percent);
  $("pct").firstChild.nodeValue = String(percent);

  const transfer = $("transfer");
  transfer.hidden = !(state.active === "download" || (download && download.state === "active"));
  if (!transfer.hidden) {
    $("dl-fill").style.width = state.progress.total ? `${Math.min(100, 100 * state.progress.done / state.progress.total).toFixed(1)}%` : "0%";
    setText("dl-text", state.progress.text || "Connecting to GitHub...");
  }

  const stepsSig = JSON.stringify(state.steps);
  if (S.sig.steps !== stepsSig) {
    S.sig.steps = stepsSig;
    $("steps").innerHTML = state.steps.map(step => `<li class="step" data-state="${esc(step.state)}"${step.state === "active" ? ' aria-current="step"' : ""}><span class="dot" aria-hidden="true"></span><span class="name">${esc(step.label)}</span><span class="detail" title="${esc(step.detail)}">${esc(step.detail)}</span></li>`).join("");
  }

  if (S.notesFor !== state.release.version) {
    S.notesFor = state.release.version;
    setText("notes-version", state.release.version);
    $("notes").innerHTML = renderNotes(state.release.notes);
  }
  const logText = state.log.join("\n");
  if (S.sig.log !== logText) {
    const box = $("log");
    const stick = box.scrollTop + box.clientHeight >= box.scrollHeight - 24;
    S.sig.log = logText;
    box.textContent = logText;
    if (stick) box.scrollTop = box.scrollHeight;
  }
  renderActions(state);
  if (state.phase === "success") startCountdown(state.closeAfter);
}

function button(id, label, cls = "") {
  return `<button class="btn ${cls}" id="${id}" type="button">${esc(label)}</button>`;
}
function renderActions(state) {
  const outcome = state.outcome || {};
  let html = "";
  if (state.phase === "running") {
    if (state.needForce) html += button("a-force", "Close NextPull now", "warn");
    if (state.canCancel || state.needForce) html += button("a-cancel", "Cancel");
  } else if (state.phase === "success") {
    html += `<span class="closing" id="closing"></span>` + button("a-done", "Done", "primary");
  } else {
    if (outcome.backup || outcome.restore_failed) html += button("a-backups", "Open backup folder");
    if (state.release.page) html += button("a-page", "Download manually");
    html += button("a-done", "Close", "primary");
  }
  if (S.sig.actions !== html) {
    S.sig.actions = html;
    $("actions").innerHTML = html;
    const primary = $("a-done");
    if (primary) primary.focus({preventScroll: true});
  }
}

function startCountdown(seconds) {
  if (S.countdown || S.interacted) return;
  S.countdownLeft = seconds;
  const paint = () => { const el = $("closing"); if (el) el.textContent = `Closing in ${S.countdownLeft} s`; };
  paint();
  S.countdown = setInterval(() => {
    S.countdownLeft -= 1;
    if (S.countdownLeft <= 0) { clearInterval(S.countdown); call("finish"); return; }
    paint();
  }, 1000);
}
function stopCountdown() {
  S.interacted = true;
  if (S.countdown) { clearInterval(S.countdown); S.countdown = null; const el = $("closing"); if (el) el.textContent = ""; }
}

function detailsText() {
  const state = S.last;
  if (!state) return "";
  const outcome = state.outcome || {};
  return [`NextPull update ${state.release.current} -> ${state.release.version}`, `Result: ${state.phase}${outcome.message ? " - " + outcome.message : ""}`, ...state.steps.map(step => `  [${step.state}] ${step.label}${step.detail ? ": " + step.detail : ""}`), "", ...state.log].join("\n");
}
async function copyDetails() {
  const text = detailsText();
  let done = false;
  try { await navigator.clipboard.writeText(text); done = true; } catch (error) { /* fall back below */ }
  if (!done) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.cssText = "position:fixed;opacity:0";
    document.body.appendChild(area);
    area.select();
    try { done = document.execCommand("copy"); } catch (error) { done = false; }
    area.remove();
  }
  const note = $("copied");
  note.textContent = done ? "Copied" : "Could not copy - select the text and press Ctrl+C";
  note.hidden = false;
  setTimeout(() => { note.hidden = true; }, 2500);
}

document.addEventListener("click", async event => {
  const target = event.target.closest("button");
  if (!target) return;
  if (target.id !== "a-done") stopCountdown();
  if (target.id === "a-cancel") { target.disabled = true; const r = await call("cancel"); if (!r || r.ok === false) target.disabled = false; }
  else if (target.id === "a-force") { target.disabled = true; target.textContent = "Closing..."; const r = await call("force_close"); if (!r || r.ok === false) { target.disabled = false; target.textContent = "Close NextPull now"; } }
  else if (target.id === "a-done") await call("finish");
  else if (target.id === "a-backups") await call("open_backups_folder");
  else if (target.id === "a-page") await call("open_release_page");
  else if (target.id === "copy-log") await copyDetails();
});
document.addEventListener("keydown", event => { if (event.key !== "Tab") stopCountdown(); });
function togglePanel(which) {
  const panels = {notes: [$("chip-notes"), $("panel-notes")], log: [$("chip-log"), $("panel-log")]};
  for (const [name, [chip, panel]] of Object.entries(panels)) {
    const open = name === which && panel.hidden;
    panel.hidden = !open;
    chip.setAttribute("aria-expanded", String(open));
    if (open && name === "log") { const box = $("log"); box.scrollTop = box.scrollHeight; }
  }
  stopCountdown();
}
$("chip-notes").addEventListener("click", () => togglePanel("notes"));
$("chip-log").addEventListener("click", () => togglePanel("log"));

async function tick() {
  const state = await call("get_state");
  if (state && state.ok !== false && state.steps) render(state);
}
async function start() {
  if (S.started) return;
  S.started = true;
  const params = new URLSearchParams(location.search);
  if (!window.pywebview || !window.pywebview.api || params.has("mock")) {
    await new Promise(resolve => { const s = document.createElement("script"); s.src = "mock.js"; s.onload = resolve; document.head.appendChild(s); });
  }
  api = window.pywebview.api;
  await tick();
  setInterval(tick, 350);
}
if (window.pywebview && window.pywebview.api) start();
else {
  window.addEventListener("pywebviewready", start, {once: true});
  if (new URLSearchParams(location.search).has("mock")) start();
}
