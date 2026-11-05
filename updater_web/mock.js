"use strict";
// Development only: ?mock=<scenario> shows the window without Python. Scenarios: wait, force, backup, download, verify, install, restart, success, failed, rolledback, restorefailed, cancelled.
(function () {
  const scenario = new URLSearchParams(location.search).get("mock") || "download";
  const labels = {wait: "Close NextPull", backup: "Back up your settings", download: "Download the update", verify: "Check the new version", install: "Install", restart: "Start NextPull"};
  const order = ["wait", "backup", "download", "verify", "install", "restart"];
  const text = {
    wait: ["Closing NextPull", "Waiting for NextPull to finish what it is doing. A running download is stopped cleanly."],
    force: ["NextPull is taking a while to close", "You can wait a little longer, or close it now. Any running download will be picked up again at its next scheduled time."],
    backup: ["Backing up your settings", "Your jobs, history and sign-ins are copied to a safe place first."],
    download: ["Downloading the update", "From GitHub, and only from GitHub."],
    verify: ["Checking the new version", "Verifying the download, then test-starting it on a copy of your settings."],
    install: ["Installing", "Swapping the program files. This takes a moment."],
    restart: ["Starting the new version", "Making sure it runs properly. If it does not, the old version comes back."],
  };
  const details = {backup: "20261105-101500-v0.2.0.zip", download: "85.0 MB", verify: "Version 0.3.0 works with your settings"};
  const notes = "## What's new\n- **One-click updates**: NextPull checks GitHub and installs the new version for you\n- Your settings are backed up before every update and restored if anything fails\n- `Check now` in Settings\n\n## Fixes\n- Faster folder listing";
  let started = Date.now();
  let cancelled = false;

  function steps(active, errorAt, errorState = "error") {
    const index = order.indexOf(active);
    return order.map((key, i) => ({key, label: labels[key], state: errorAt === key ? errorState : i < index ? "done" : i === index ? "active" : "pending", detail: errorAt === key ? (errorState === "skipped" ? "Cancelled" : "The new version stopped right after starting (exit code 3)") : (i < index ? details[key] || "" : "")}));
  }
  function done() { return order.map(key => ({key, label: labels[key], state: "done", detail: details[key] || ""})); }
  function log(lines) { return lines.map((line, i) => `10:15:${String(10 + i).padStart(2, "0")}  ${line}`); }

  function snapshot() {
    const base = {ok: true, phase: "running", canCancel: false, needForce: false, closeAfter: 6, elapsed: 0, logPath: "C:\\Users\\you\\AppData\\Local\\NextPull\\logs\\updater.log",
                  release: {version: "0.3.0", current: "0.2.0", name: "NextPull 0.3.0", notes, size: 85e6, sizeText: "81.1 MB", page: "https://github.com/WikiZell/NextPull/releases/tag/v0.3.0"},
                  progress: {done: 0, total: 0, speed: 0, eta: 0, text: ""}, outcome: null, active: ""};
    const run = (key, extra = {}) => ({...base, steps: steps(key), active: key, headline: text[key][0], sub: text[key][1], log: log(["NextPullUpdater 1.0: 0.2.0 -> 0.3.0", `[${key}] active`]), ...extra});
    if (scenario === "download") {
      const total = 85e6, done = Math.min(total, ((Date.now() - started) / 14000) * total), speed = 7.4e6;
      const seconds = Math.max(1, Math.round((total - done) / speed));
      if (done >= total) started = Date.now();
      return run("download", {canCancel: true, progress: {done, total, speed, eta: seconds, text: `${(done / 1048576).toFixed(1)} MB of 81.1 MB  \u00b7  7.1 MB/s  \u00b7  ${seconds} s left`}});
    }
    if (scenario === "force") return run("wait", {headline: text.force[0], sub: text.force[1], needForce: true});
    if (text[scenario] && scenario !== "force") return run(scenario);
    const outcomeBase = {ok: false, version: "0.3.0", previous_version: "0.2.0", message: "", rolled_back: false, cancelled: false, restore_failed: false, backup: "C:\\Users\\you\\AppData\\Local\\NextPull\\backups\\20261105-101500-v0.2.0.zip", step: "restart"};
    if (scenario === "success") return {...base, phase: "success", steps: done(), headline: "NextPull 0.3.0 is installed", sub: "Your settings were backed up first and are unchanged. NextPull is running again.", outcome: {...outcomeBase, ok: true},
      log: log(["Settings backed up to 20261105-101500-v0.2.0.zip", "Checksum ok (9f2a41c7d0e1...)", "Swapping the program folder", "[restart] done"])};
    if (scenario === "rolledback") return {...base, phase: "failed", steps: steps("restart", "restart"), headline: "The update didn't work, so it was undone", sub: "NextPull 0.2.0 is back exactly as it was, and your settings were restored from the backup. NextPull is running again.",
      outcome: {...outcomeBase, rolled_back: true, message: "The new version stopped right after starting (exit code 3)"}, log: log(["FAILED in restart: The new version stopped right after starting (exit code 3)", "Restoring the previous version", "Restored jobs.json", "NextPull 0.2.0 started again"])};
    if (scenario === "restorefailed") return {...base, phase: "failed", steps: steps("restart", "restart"), headline: "The update failed, and the automatic repair did not finish", sub: "Your settings are safe in the backup. Open the backup folder below; the previous program files are kept next to NextPull as NextPull.previous.",
      outcome: {...outcomeBase, restore_failed: true, message: "Windows would not release the files"}, log: log(["FAILED in restart", "RESTORE FAILED: PermissionError"])};
    if (scenario === "cancelled") return {...base, phase: "cancelled", headline: "Update cancelled", sub: "Nothing was changed. NextPull 0.2.0 is running again.", steps: steps("download", "download", "skipped"), outcome: {...outcomeBase, cancelled: true, step: "download", message: "Update cancelled"}, log: log(["Cancel requested", "NextPull 0.2.0 started again"])};
    // failed before anything changed (for example the checksum did not match)
    const bad = steps("verify", "verify");
    bad[3].detail = "The downloaded file does not match its checksum";
    return {...base, phase: "failed", steps: bad, headline: "The update was not installed", sub: "Nothing was changed. NextPull 0.2.0 is running again.", outcome: {...outcomeBase, step: "verify", message: "The downloaded file does not match its checksum, so it was thrown away. Nothing was changed"},
      log: log(["FAILED in verify: The downloaded file does not match its checksum", "NextPull 0.2.0 started again"])};
  }

  window.pywebview = {api: {
    get_state: async () => snapshot(),
    cancel: async () => { cancelled = true; return {ok: true}; },
    force_close: async () => ({ok: true}),
    open_release_page: async () => ({ok: true}),
    open_backups_folder: async () => ({ok: true}),
    open_log: async () => ({ok: true}),
    finish: async () => ({ok: true}),
  }};
  window.dispatchEvent(new Event("pywebviewready"));
})();
