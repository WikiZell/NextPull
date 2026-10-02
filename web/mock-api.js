"use strict";
// Development mock of window.pywebview.api (same shapes as NextPullApi). Loaded only with ?mock=1 or on a plain http dev server.
// Serve with:  py -3 -m http.server --directory web   then open  http://localhost:8000/?mock=1&page=jobs
(() => {
  const iso = d => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  const now = new Date();
  const day = n => { const d = new Date(now); d.setDate(d.getDate() - n); return d; };
  const state = {
    connected: true,
    settings: {notifications: true, close_to_tray: true, prevent_sleep: true, rclone_path: "", history_days: 365, log_days: 90},
    jobs: [
      {id: "a1b2c3d4", name: "Night pull", enabled: true, source: "Documents/Photos", mode: "move", dest: "D:\\Films\\Incoming", schedule: {days: [0, 1, 2, 3, 4, 5, 6], time: "02:00", stop_by: "06:00"}, catch_up: true, catch_up_hours: 12,
       speed: {limit_mbps: 8, timetable: ""}, transfers: 1, streams: 4, min_age_minutes: 5, excludes: ["*.log"], includes: [], checksum: false, dry_run: false, delete_empty_dirs: true, retries: 3},
      {id: "e5f6a7b8", name: "Weekend series", enabled: false, source: "Series/Archive", mode: "copy", dest: "E:\\Series", schedule: {days: [5, 6], time: "09:30", stop_by: ""}, catch_up: false, catch_up_hours: 12,
       speed: {limit_mbps: 0, timetable: "08:00,2M 23:00,off"}, transfers: 2, streams: 4, min_age_minutes: 5, excludes: [], includes: ["*.mkv"], checksum: true, dry_run: false, delete_empty_dirs: true, retries: 3},
    ],
    run: new URLSearchParams(location.search).has("running") ? {t0: Date.now() - 1500e3, total: 5.5e9, bytes: 0} : null,
  };
  const tree = {"": ["Documents", "Photos", "Shared", "Series"], Shared: ["DOWNLOAD", "EXTRACTED"], "Shared/EXTRACTED": ["Robocar POLI"], Series: ["Archive"], "Series/Archive": []};
  const files = {"Shared/EXTRACTED": [["La fine di Oak Street [2026.1080p].mkv", 2655484605], ["Supergirl [2026.1080p].mkv", 2890968976]]};
  const runs = [], runFiles = {};
  const names = ["Sotto Il Segno Del Pericolo.mkv", "The Odyssey (1997).mkv", "Dusting Cliff 7.mkv", "Sahara 1983.mkv", "Ski Patrol 1990.mkv"];
  for (let i = 1; i <= 24; i++) {
    const d = day(i); d.setHours(2, 0, 0, 0);
    const failed = i === 9, missed = i === 14 || i === 15, nothing = i % 4 === 0;
    const id = runs.length + 1, n = nothing || failed || missed ? 0 : 1 + (i % 3);
    const fl = Array.from({length: n}, (_, k) => ({name: names[(i + k) % names.length], size: 1.2e9 + ((i * 97 + k * 31) % 15) * 1e8, status: "ok", speed: 6e6 + ((i * 13 + k) % 8) * 1e6, duration: 300 + k * 40, error: "", deleted: 1}));
    runFiles[id] = fl;
    const bytes = fl.reduce((a, f) => a + f.size, 0), secs = fl.reduce((a, f) => a + f.duration, 0);
    const finished = new Date(d.getTime() + (secs || 25) * 1000);
    runs.push({id, job_id: "a1b2c3d4", job_name: "Night pull", trigger: "schedule", mode: "move", source: "Shared/EXTRACTED", dest: "D:\\Films\\Incoming", dry_run: 0, started: iso(d), finished: iso(finished),
      state: failed ? "failed" : missed ? "missed" : "ok", bytes, files: n, deleted: n, errors: failed ? 1 : 0, avg_speed: secs ? bytes / secs : 0, message: failed ? "Too many temporary errors (network?): connection reset" : missed ? "Missed: the app was not running at that time" : (n ? `${n} file(s) transferred` : "Nothing new to download"), log_file: ""});
  }
  runs.sort((a, b) => b.started.localeCompare(a.started));
  if (new URLSearchParams(location.search).has("running")) state.run = {t0: Date.now() - 300000, total: 5.5e9, bytes: 0};
  const ok = r => ({ok: true, ...r});
  const sim = () => {
    if (!state.run) return null;
    const r = state.run, t = (Date.now() - r.t0) / 1000;
    r.bytes = Math.min(r.total, t * 7.4e6);
    const pct = Math.floor(100 * r.bytes / r.total);
    return {run_id: 99, job_id: "a1b2c3d4", job_name: "Night pull", state: "running", mode: "move", dry_run: false, started: iso(new Date(r.t0)), bytes: r.bytes, total_bytes: r.total, speed: 7.4e6, eta: Math.max(0, (r.total - r.bytes) / 7.4e6),
      files_done: Math.floor(r.bytes / 2.7e9), transferring: [{name: "Supergirl [2026.1080p].mkv", size: 2.89e9, bytes: r.bytes % 2.89e9, percentage: pct % 100, speed: 7.4e6, eta: 120}], errors: 0, last_error: "", bwlimit: "8M"};
  };
  window.pywebview = {api: {
    bootstrap: async () => ok({app: {name: "NextPull", version: "0.1.1"}, settings: state.settings, connection: state.connected ? {connected: true, server: "https://cloud.example.org", user: "alice", display_name: "Alice Example", quota_used: 410e9, quota_total: 2e12} : {connected: false},
      rclone: {ok: true, found: true, path: "C:\\NextPull\\tools\\rclone\\rclone.exe", version: "rclone v1.68.2"}, autostart: true, watchdog: false, data_dir: "C:\\Users\\alice\\AppData\\Local\\NextPull", tray: true, windows: true}),
    status: async () => ok({running: !!state.run, current: sim(), starting: false, queue: [], connected: state.connected, rclone_found: true, last_checked: iso(now), now: iso(now),
      jobs: state.jobs.map(j => ({id: j.id, name: j.name, enabled: j.enabled, next_run: j.enabled ? iso(new Date(now.getTime() + 8 * 3600e3)) : null, last_run: {id: runs[0].id, state: runs[0].state, started: runs[0].started, finished: runs[0].finished, files: runs[0].files, bytes: runs[0].bytes, message: runs[0].message}, running: !!state.run && j.id === "a1b2c3d4"})),
      problem_count: 2, events: [{time: iso(now), level: "info", text: "Night pull: ok - 2 file(s) transferred"}, {time: iso(day(1)), level: "warn", text: "Night pull: scheduled run at 02:00 was missed"}, {time: iso(day(2)), level: "info", text: "Night pull: started (schedule)"}]}),
    connection: async () => ok({connection: {connected: state.connected, server: "https://cloud.example.org", user: "alice", display_name: "Alice Example"}}),
    connect_start: async server => { if (!server) return {ok: false, error: "Enter the Nextcloud address"}; setTimeout(() => { state.connected = true; }, 3000); return ok({login_url: "https://" + server + "/login/v2/flow/abc"}); },
    connect_status: async () => ok({state: state.connected ? "connected" : "waiting", message: "", connection: {connected: state.connected}}),
    connect_cancel: async () => ok({}), disconnect: async () => { state.connected = false; return ok({revoked: true}); },
    browse: async (path = "") => {
      const clean = String(path).replace(/^\/+|\/+$/g, "");
      const dirs = (tree[clean] || []).map(n => ({name: n, path: clean ? clean + "/" + n : n, is_dir: true, size: 4e9, modified: iso(now)}));
      const fl = (files[clean] || []).map(([n, s]) => ({name: n, path: clean + "/" + n, is_dir: false, size: s, modified: iso(day(1))}));
      return ok({path: clean, parent: clean ? clean.split("/").slice(0, -1).join("/") : null, entries: [...dirs, ...fl]});
    },
    list_jobs: async () => ok({jobs: state.jobs.map(j => ({...j, schedule_text: j.schedule.days.length === 7 ? `Every day at ${j.schedule.time}` : "Weekends at " + j.schedule.time, next_run: j.enabled ? iso(new Date(now.getTime() + 8 * 3600e3)) : null, last_run: {state: "ok", started: runs[0].started}, running: !!state.run && j.id === "a1b2c3d4"}))}),
    save_job: async job => { if (!job.name) return {ok: false, error: "Give the job a name"}; if (!job.source) return {ok: false, error: "Pick the Nextcloud folder to download"}; if (!job.dest) return {ok: false, error: "Pick the destination folder on this PC"};
      const saved = {...job, id: job.id || Math.random().toString(16).slice(2, 10)}; const i = state.jobs.findIndex(j => j.id === saved.id); if (i >= 0) state.jobs[i] = saved; else state.jobs.push(saved); return ok({job: saved}); },
    delete_job: async id => { state.jobs = state.jobs.filter(j => j.id !== id); return ok({}); },
    set_job_enabled: async (id, enabled) => { state.jobs.find(j => j.id === id).enabled = enabled; return ok({}); },
    duplicate_job: async id => { const j = state.jobs.find(x => x.id === id); state.jobs.push({...j, id: Math.random().toString(16).slice(2, 10), name: j.name + " (copy)", enabled: false}); return ok({}); },
    run_job: async () => { state.run = {t0: Date.now(), total: 5.5e9, bytes: 0}; setTimeout(() => { state.run = null; }, 60000); return ok({queued_behind: ""}); },
    cancel_run: async () => { state.run = null; return ok({}); }, set_speed: async () => ok({}),
    history: async (limit, offset, jobId) => ok({runs: runs.filter(r => !jobId || r.job_id === jobId)}),
    run_detail: async id => ok({run: runs.find(r => r.id === id), files: runFiles[id] || []}),
    run_log: async () => ok({lines: [{time: "2026-10-01 02:00:02", level: "info", msg: "Copied (new)", object: "Dusting.Cliff.7.mkv"}, {time: "2026-10-01 02:05:27", level: "info", msg: "Deleted", object: "Dusting.Cliff.7.mkv"}, {time: "2026-10-01 02:05:28", level: "error", msg: "Attempt 1/4 failed with 1 errors and: connection reset", object: ""}, {time: "2026-10-01 02:15:12", level: "notice", msg: "Skipped delete as --dry-run", object: "rclone_log.log"}]}),
    clear_history: async () => ok({}),
    diagnostics: async () => ok({runs: [{job: "Night pull", state: "failed", message: "Nextcloud is not reachable: dial tcp: lookup cloud.example.org: no such host", count: 3, last: iso(day(1)), run_id: runs[0].id, hint: "Nextcloud could not be reached. Check the internet connection and that the server is up; NextPull retries on the next schedule.", severity: "error"}, {job: "Night pull", state: "partial", message: "2 files downloaded but not deleted on Nextcloud (403)", count: 1, last: iso(day(2)), run_id: runs[0].id, hint: "Nextcloud or Windows refused the action.", severity: "warn"}],
      log: [{level: "WARNING", source: "nextpull.run", message: "run 12: Nextcloud pre-check attempt 2/3 failed: timed out", count: 4, last: "2026-10-01 02:00:31", hint: "Nextcloud could not be reached."}], unseen: 2, system: {app_version: "0.1.1", python: "3.13.1", platform: "Windows-11-10.0.26200"}, log_file: "C:\\Users\\alice\\AppData\\Local\\NextPull\\app.log"}),
    app_log: async () => ok({entries: [{time: "2026-10-01 02:00:00", level: "INFO", name: "nextpull.engine", message: "Night pull: queued (run) for slot 2026-10-01T02:00"}, {time: "2026-10-01 02:00:31", level: "WARNING", name: "nextpull.run", message: "run 12: Nextcloud pre-check attempt 2/3 failed: timed out"}, {time: "2026-10-01 02:01:10", level: "ERROR", name: "nextpull.ui", message: "TypeError: x is undefined (app.js:310)"}]}),
    log_ui_error: async () => ok({}), ack_problems: async () => ok({}),
    self_test: async () => ok({results: [{id: "rclone", title: "rclone", status: "ok", detail: "rclone v1.68.2", hint: ""}, {id: "nextcloud", title: "Nextcloud connection", status: "ok", detail: "Signed in as Alice Example (0.4s)", hint: ""}, {id: "job:a1", title: "Job 'Night pull'", status: "error", detail: "Destination drive/folder D:\Anime is not available", hint: "Edit the job and check the folders."}, {id: "autostart", title: "Start with Windows", status: "warn", detail: "Off", hint: "After a restart NextPull will not run until someone opens it. Turn it on in Settings."}]}),
    export_report: async () => ok({path: "C:\\Users\\alice\\AppData\\Local\\NextPull\\reports\\nextpull-report-20261002-120000.zip", folder: "C:\reports", size: 52000}), reveal_report: async () => ok({}),
    stats: async days => { const daily = []; let bytes = 0, files = 0;
      for (let i = days - 1; i >= 0; i--) { const d = day(i), r = runs.find(x => x.started.slice(0, 10) === iso(d).slice(0, 10)); const b = r ? r.bytes : 0, f = r ? r.files : 0; bytes += b; files += f;
        daily.push({date: iso(d).slice(0, 10), runs: r ? 1 : 0, bytes: b, files: f, failed: r && r.state === "failed" ? 1 : 0, missed: r && r.state === "missed" ? 1 : 0, speed: r && r.avg_speed ? r.avg_speed : 0}); }
      return ok({days, totals: {runs: 24, ok_runs: 17, failed_runs: 1, partial_runs: 0, missed_runs: 2, bytes, files, deleted: files, errors: 1, avg_speed: 7.6e6, success_rate: 94.4}, daily,
        top_files: Object.values(runFiles).flat().sort((a, b) => b.size - a.size).slice(0, 6).map(f => ({name: f.name, size: f.size, speed: f.speed})), by_job: [{job_name: "Night pull", runs: 22, files, bytes}]}); },
    get_settings: async () => ok({settings: state.settings}), save_settings: async s => { state.settings = {...state.settings, ...s}; return ok({settings: state.settings}); },
    check_rclone: async () => ok({found: true, path: "rclone.exe", version: "rclone v1.68.2"}), set_autostart: async () => ok({autostart: true}), set_watchdog: async () => ok({watchdog: true}),
    export_jobs: async () => ok({text: JSON.stringify({app: "NextPull", version: 1, jobs: state.jobs}, null, 2)}), import_jobs: async () => ok({imported: 1}),
    pick_folder: async () => ok({path: "D:\\Films\\Incoming"}), test_destination: async () => ok({exists: true, writable: true, free_bytes: 1.2e12}),
    open_path: async () => ok({}), open_logs: async () => ok({}), hide_window: async () => ok({}), show_window: async () => ok({}), quit_app: async () => ok({}),
  }};
})();
