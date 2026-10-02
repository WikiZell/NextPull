# Scheduler

Code: `scheduler.py` (pure functions) and `Engine.tick` in `engine.py`.

## Model

A job has `schedule = {"days": [0..6], "time": "HH:MM", "stop_by": "HH:MM" | ""}` (0 = Monday). Times are the PC's **local** time,
as naive datetimes. A **slot** is one nominal start: a chosen day at the chosen time.

## The tick

Every `tick_seconds` (default 10) the engine:

1. reads `last_checked` (persisted in `state.json`; set to *now* on the very first start),
2. for each enabled job asks `due_slots(schedule, last_checked, now)`: the slots in `(last_checked, now]`,
3. **older slots** (all but the newest) are recorded as `missed`, so the history shows every hole,
4. the **newest slot** is classified (`classify_slot`):

| Delay (now - slot) | Condition | Result |
| --- | --- | --- |
| at most 2 ticks (20 s) | always | `run` (trigger `schedule`) |
| later | job has *catch-up* on, delay is within `catch_up_hours`, and the stop-by window has not closed | `catch-up` (trigger `catch-up`) |
| later | otherwise | `missed` (recorded, not executed) |

5. a run that cannot be queued because the same job is running or waiting is recorded as `skipped`,
6. `last_checked = now`.

If the clock went **backwards** (manual change, DST fall-back), `last_checked` is clamped to *now*: the future is never replayed.

## Stop-by

`stop_by` ends a *scheduled* run: `window_end(schedule, slot)` is the deadline, passed to the run, which then asks rclone to quit
(`core/quit`) and ends with state `stopped`. A `stop_by` at or before the start time means the **next day** (`22:00` -> `06:00`).
A manual *Run now* ignores `stop_by`.

## Examples

* Job daily 02:00, PC on 24/7: the 02:00:00 slot is seen at 02:00:00-02:00:10 and runs.
* PC asleep 00:00-05:00, catch-up on (12 h): at 05:00 the 02:00 slot is 3 h late, so it runs as `catch-up`.
* App closed Mon-Wed, started Thursday 02:00:05: Mon/Tue/Wed slots are `missed`, Thursday runs.
* Same, but the PC wakes at 15:00 with catch-up limit 12 h: the 02:00 slot is 13 h late, so it is `missed`.
* Job with `stop_by 06:00`, catch-up wakes the PC at 07:00: the window is closed, so the slot is `missed`.

## Limits and notes

* **DST**: slots are plain local wall-clock times. On the spring-forward day a 02:30 slot does not exist (the run happens at the next
  tick after it is seen, or is caught up); on the fall-back day a slot may be seen twice if `last_checked` lands exactly in the repeated
  hour, which the same-job queue rule prevents from running twice.
* **One run at a time** (see [ARCHITECTURE.md](ARCHITECTURE.md)): a second due job waits in the queue.
* The scheduler only *starts* things; it never kills a running manual run.
