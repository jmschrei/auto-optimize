# auto-optimize dashboard

Operating it: `references/dashboard.md`, under the skill root two levels
above this directory.

Requirements: IPython for `show()`; plotly for the page's charts and for
`--export`; psutil optional (without it the host summary and process table
say so); playwright and jupyterlab for the tests.

## Files

| File | Owns |
|---|---|
| `scan.py` | `RunScanner`: `snapshot()` (state.json), `readmes()` (readmes/<id>.json), `hardware.resources()` (resources.json), and `classify`/`target(cmdline, pid)` for the process table. `scan.py <root>` prints the snapshot |
| `gpu_scan.py` | GPUs (three queries run side by side through one NVML session per publisher, `libnvidia-ml.so.1` by ctypes, since a new nvidia-smi process stalls cudaMalloc in every CUDA process on the host; nvidia-smi only where NVML will not load, or where `GPU_SCAN_NVML` names a file that does not; the power limit read once per session and per-process SM once a minute, since those NVML calls also hold cudaMalloc while they run; each query shared by every publisher of this user on the host through `shared_sample()` in `/dev/shm/dashboard-monitor-<uid>/`, keyed by the nvidia-smi path; an answer is dated when its call started, a publisher waits up to `SHARED_WAIT` (1.5 s) for a call under way, and a failed call keeps the last good answer), host and its name, lease holders (`GPU_LEASE_DIR`, optional), this search's processes; `autoopt_monitor.py` is in `SELF_MARKERS` |
| `monitor_writer.py` | the scan loop, the hardware thread, asset install, rounding, `writer.pid`, the writer index, the per-directory writer lock (`claim()`, in the same `/dev/shm` directory: one writer per payload directory on a host, released by the kernel at exit), and the heartbeat `writer.host` beside the payload for writers on other hosts (`yields_to()`). `monitor_writer.py <root> --out DIR [--interval 20] [--resource-interval 3]`, started by `autoopt_monitor.py` |
| `autoopt_monitor.py` | `show()`, `make_notebook()`, `--start/--status/--stop/--export`; the notebook entry point |
| `static/index.html`, `dashboard.css`, `dashboard.js` | the page, in matching `==== SECTION: <name> ====` blocks |
| `make_fixture.py`, `test_frontend.py`, `test_notebook.py` | below |
| `spacing_check.py` | geometry checks for the spacing scale |

`dashboard.js` sections: `core` through `gpu` are the generic monitor
(`plots` holds `categoryChips`); `lineage-tree` and `expandable-log` are
generic widgets; `search`, `objective`, `observers`, `validation`,
`run-table`, `log`, `tree`, `summary-autoopt`, `sidebar-autoopt` and
`polling` are this dashboard's. `CATCH_ALL` is
`MISC`. The page is an ES module and must stay one.

## The page

| Panel | Reads | Behavior to keep |
|---|---|---|
| Summary band | `result.json`, `resources.json` | settled, frontier, gain, rounds, frontier steps, runs per iteration, GPUs in use, subagents working |
| Category chips | `categories` | `ROOT`, then the categories `autoopt.py` allows (`autoopt.categories_for`), empty ones included, and any other an iteration used. Eight hues (`--cat-0` to `--cat-7`), since shape carries status here, so each of the idea miner's eight categories has its own; `MISC` is the grey catch-all. A click emphasizes (several may be on; a second click clears); the `.eye` dot hides the category from charts and log and dims it in the tree |
| GPUs | `gpu_scan` | one card per device: utilization sparkline, lease holder, this search's processes. A sample with no GPUs after one with GPUs keeps the last cards; a reading older than 30 s (or three hardware cadences) dims them and the host summary gives its age (`gpu_sampled_at`). The strip's height only grows while mounted, and its drag bar is left alone while the strip is not laid out. Its Live toggle (off on every load) writes `live.json` beside the payload every 3 s through the Contents API while the tab is showing and polls every second; while that file is under 10 s old the publisher samples every second, straight from NVML without the shared cache (nvidia-smi keeps it), and reports `writer.live`. Per-process SM stays on its 60 s clock, and the sparkline history on the slow one |
| Objective by iteration | `result.json`, `meta.json` | medians, the frontier staircase, in-flight iterations on the floor. *Stage boundaries* (on): a purple dashed line between neighbors whose idea-miner stage (`mining_stage`; the baseline is 0) differs, so a bump after a fresh batch of ideas shows; in a search with no stages, whose tree generation differs, and the legend says which. *Confidence intervals* (off): a capped dotted whisker from `scan.median_ci` (95% percentile bootstrap of the median, 2,000 resamples, seed 0), none under two runs. *All runs*: every seed, size 4, opacity 0.3, muted hue. The hover adds the code change: `lines` `+`/`−` and each nonzero file count, in the diff colors, and the miner stage |
| Abandoned iterations | `iterations/<id>.abandoned/meta.json`, `README.md` | an empty fork set aside (`references/resume.md`): a thin ✕ (`x-thin-open`) in its category's hue at its own x and the baseline's value, keyed in the legend; follows the chips and *hide failed*; in no count, tree, table or log (scan.py `abandoned`) |
| Subagent working | `result.json`, `README.md`, mtimes | measured, no `README.md`, a file written within the hour: larger point, thick ring in its category's color, counted in the summary band |
| Every run | `result.json` | one row per seed, failed ones included; a broken gate is red; `note` cut at 240 characters; sortable, searchable, status and type filters, 40 rows a page, newest first |
| Observers | `result.json` | one chart each, reordered by its grip |
| External validation | `config.json`, `validations/round-<N>.json` | one small multiple per numeric measure over rounds; string measures only in the sidebar card. "Not a gate" above the panels, in the card and in every hover. Follows neither selection nor chips |
| Search tree | `meta.json` parents, `readmes/<id>.json` | one lineage, a generation strip; height follows the drag handle (double-click: 430 px); a node click opens its README and numbers, a second closes it |
| Iteration log | `meta.json`, `result.json`, `README.md`, `unit-tests/result.json`, `validation/record.json`, `repo.git` | newest first. An open entry leads with its code change against its parent (`diffstat`: lines `+`/`−`, files modified, added, deleted, in `--diff-add`, `--diff-del`, `--diff-mod`, GitHub's dark-theme colors), then the README excerpt; a cut excerpt (`notes_cut`) ends in SHOW MORE, which fetches `readmes/<id>.json` and shows it without the title, metadata line and run table, until SHOW LESS. The detail's GATE, OBS and SEED labels take `--tag-gate`, `--tag-obs`, `--tag-seed`. Time split: total (`created` to the last of those files and `report.txt`), runs (summed, divided by GPUs shared), unit tests, validation, and subagent work as the remainder |
| Processes | psutil | pid, kind, iteration, CPU, RSS, elapsed |
| Ended line and stage strip | `after` in the snapshot (`RunScanner.after`): `ended.json`, `final.json`, `stages.collect` | the topbar's ended line (hidden while the search runs) and the five-stage strip under the chips; plain text. The `toolchain` row of an open log entry comes from `meta.json` (`references/dashboard.md`) |

| Mark | Means |
|---|---|
| hue | category |
| full-strength hue | frontier (2 px larger on a chart), root (tree), selected or emphasized; `MISC` strong is `--cat-other-strong` |
| circle / ✕ / hollow circle / hollow square | passed / gate failed / a run errored / fewer runs than `runs_per_iteration` |
| hollow diamond on the floor, dashed ring in the tree | running |
| dotted pink staircase | the frontier |
| `+N` dashed tally | condensed off-frontier iterations; hover gives passed count and best |
| muted green / red validation point | that round's script reported `ok` true / false |

Lasso or box-select links every chart and dims the tree; **Clear selection**
or a double-click drops it. A click on a point or tree node, or an opened
log entry, pins that iteration: its rows in *Every run* are outlined and
scrolled to, and a second click drops the pin; a click on one run's dot
outlines that run. `x log` / `y log`
override the automatic log axis and count the points it hides; the
objective chart's `x log` is manual only (`(1 hidden)`: the baseline at x = 0).

Scale (`LINEAGE_MANY`, `markStyle`, `DENSITY_LIMIT`): past 120 iterations
the lineage draws roots, the frontier path, anything running and the 12
newest, the rest one `+N` per parent and generation, nodes at least 34 px
apart. Past 500 points markers are size 5 at opacity 0.3; past 5,000 medians
become a density and *All runs* is not drawn. Hover README excerpts shrink
from 600 to 160 characters. `test_frontend.py --scale` covers this.

## The payload

| File | Written | Page fetches |
|---|---|---|
| `state.json` | every full scan (`interval`, 20 s) | every state poll |
| `resources.json` | every hardware sample (`resource_interval`, 3 s, or as long as the GPU query takes), on its own thread | every hardware poll |
| `readmes/<id>.json` | when that iteration's `README.md` mtime moves; the whole file, where `state.json` carries an excerpt | when its tree node's panel opens, and again when the excerpt changes |

`state.json` carries every iteration (its `objective.ci` is `median_ci`'s
bootstrap interval, cached per set of run values), the root, round and roots, the frontier and its steps, and `categories`, the
fixed list from `autoopt.py`, which fixes each category's color: sorted,
`MISC` the gray catch-all, `ROOT` (iteration 0, whatever its `meta.json`
says) a fixed light neutral outside the slots. The lineage is built and condensed in the page
from `iterations`; the payload carries no tree. `validation` is None for a
search without an external validation, else its configured `measures`,
`units` (config.json's, then each round's `validation.json`), `note`,
`approved` and one record per `validations/round-<N>.json`, oldest
first.

An iteration whose files raise in the scanner (a malformed `result.json`)
becomes a row with `status` `unreadable` and its `error`, counted as
settled and never on the frontier; `state.json`'s `unreadable` lists them
as `{id, error}`.

`resources.json` carries `psutil` (False when it is not installed),
`gpu_sampled_at` (when the last GPU query that answered started;
older than `timestamp` while it is slow or failing, and a history sample
repeats no reading), `hostname` (the host the GPUs are on, which the host
summary leads with), and
`writer`, the writer's report on itself: `interval`,
`resource_interval`, `state_cadence` and `resource_cadence` (the longest of
the last five measured gaps, in seconds, None until two have happened),
and `scan_error` with `scan_error_at`, the last failed scan's reason
(including the message `autoopt.fail()` prints before its SystemExit),
cleared by the next good scan. The page's pulse ages `state.json` alone
against `max(interval, state_cadence)`: live under 2.5 of those, stale
under 10, down beyond. A `scan_error` turns it stale and is shown beside
it with the writer log; unreadable iterations are named there too.

`writer.pid` holds five lines: PID, search, code stamp, the two intervals,
and the process start time (`/proc/<pid>/stat` field 22, else psutil's
`create_time`). `writer_info` accepts it only while that PID's command line
names `monitor_writer.py` and its start time matches, so a PID the kernel
reused is never signaled. It is written whole (a temporary file, then a
rename). SIGTERM raises out of a scan or a sleep; in `finally` the pidfile
and index entry are removed only while the pidfile still names this
process. The index entry is
`~/.cache/autoopt-monitor/<out_dir slug>-<8 hex of its SHA-1>.writer`.
`autoopt.py status` (`live_publishers`) reads the pidfiles and, where /proc
exists, also finds writers of the search by process, so a writer whose
pidfile is gone still counts as running.

`start_writer` first stops every writer into the directory but the
pidfile's (`writers_into`: by process, from any copy of the script) and
every writer of the search into another directory (`publishers`, the same
way), and returns once the new writer's `state.json` and `resources.json` both
exist, so it blocks for one hardware sample. When its writer loses the
lock to a start racing it, it returns the winner's pid. `--start`
without `--out` takes the directory a live writer of the search publishes
into, so a second `--start` reports that writer. A
handover to another search deletes the three payloads and renames
`readmes/` to `readmes.<YYYYmmdd-HHMMSS>-<pid>`; nothing deletes those.
The page fetches `readmes/<id>.json` only for a row with `has_readme`.

A settled row with a parent carries `diffstat`, `[lines added, lines
deleted, files added, files deleted, files modified]`, from `git diff --raw
--numstat -M <parent commit>...<commit>` in `repo.git`: counted from the
merge base; a rename or mode change is a modification,
a binary file adds no lines. The baseline, in-flight iterations and a pair git
cannot read have none. Each pair is cached for the writer's life; a scan
spends at most `DIFF_BUDGET` (2 s) on new pairs, newest first, so a long
search fills in over a few polls. `notes_cut` is present, and true, only
when the excerpt was cut.
`show()` URL-encodes each segment of the payload directory.

Without `/proc` (macOS) writer discovery uses psutil, and finds nothing
without it. Jupyter servers are read from
`jupyter_core.paths.jupyter_runtime_dir()`, `~/.local/share/jupyter/runtime`
and `$JUPYTER_RUNTIME_DIR`.

`SOURCES` in `monitor_writer.py` includes `../autoopt.py`, so a change to the
runner's scoring replaces a running writer too.

## Testing

```bash
python3 test_frontend.py                  # a 13-iteration search, every panel
python3 test_frontend.py --scale [N]      # N iterations (default 2,600); condensing, payload size
python3 test_frontend.py --shot x.png     # any mode, plus a screenshot
python3 test_frontend.py --headed         # any mode, with the browser visible
python3 test_notebook.py                  # the JupyterLab mount, on a private server
python3 test_notebook.py --run <dir>      # the mount against a real search
python3 test_notebook.py --theme 'JupyterLab Dark'   # under a Lab theme
python3 test_notebook.py --keep           # leave the server up to look at
```

Each prints `N passed, M failed` and exits non-zero on a failure. Scratch
goes in fresh `mktemp` directories that are not deleted.

`make_fixture.py --out <dir>` (required) builds its search by driving
`autoopt.py` and `run_iteration.py` as a subagent would, including an
approved external validation run in both rounds; `--scale N` clones settled
iterations up to N. `test_frontend.py` launches three processes detached,
since the scanner skips its own children: the fixture's
`iterations/11/worker.py`, so the process table has a row; a copy in
iteration 12 with a relative command line and stdout in
`runs/seed-9/log.txt`, which only `target`'s stdout lookup attributes; and a
stand-in `autoopt_monitor.py --start`, which `SELF_MARKERS` must exclude. It
writes a fake lease on GPU 3 into a private `GPU_LEASE_DIR`, and copies the
fixture twice to check a search without a validation and one configured but
never run.
`test_notebook.py` starts its own JupyterLab with its own token and root and
touches `static/dashboard.js` to check the re-run notice.

The default run also drives the publisher CLI (`publisher()`): slugs with a
path hash, a restart on another interval, `--stop` waiting, discovery by
process, `--out`, a wrong directory as one `error:` line, and `scan.py`
into a closed pipe. Before it: `check_dead_code` (unused JS names and CSS
classes), `check_pid_reuse` (a pidfile naming a `sleep` must leave it
alive), `check_slow_hardware` (with NVML kept from loading, a stand-in
`nvidia-smi` that sleeps 8 s must not delay the first state.json, a
handover or a stop),
`check_one_writer` (a second writer refuses, a start stops a writer its
pidfile lost, two racing starts leave one, payloads written whole by two
writers, a late stop leaves a successor's pidfile), `check_shared_sample`
(on the nvidia-smi fallback, six publishers, one call per query; a
failing call keeps the last rows; with NVML loaded, every GPU and no
nvidia-smi call; an answer dated at its call's start; a waiter takes the
answer under way), `check_other_host` (`yields_to()`'s cases; a writer pauses
under another host's fresh heartbeat and takes over when it goes quiet),
`check_bad_config` (a failing config.json and a corrupt state.json),
`check_unreadable`, `check_portability` (no /proc, psutil or plotly,
another runtime directory, malformed leases) and `check_paths`. After the
browser runs: `check_readme_handover`, `check_export_markup` (a README
holding `<!--` then `<script`), `check_pulse` and `check_gpu_strip` (payloads
alternating GPUs with processes, none, and without: card count and the next
section's top; a hidden strip's drag bar; a stale reading). Every browser run sits in a `try` whose failure records
`the browser checks ran to the end`.

`test_frontend.py` runs `spacing_check`'s five checks, with a
log entry open: text against hairlines, dividers (`DIVIDED` adds nothing of
this dashboard's own), rhythm, axis titles and log axes.
The default run repeats them on a forced log y axis with an out-of-range
seed added, and checks the wrapped tiles at 560 px. Legend keys carry
`.mark-key` or `.swatch`, which the checker exempts.

The harness helpers `hover_point` and `drag_panel` exist because
`page.hover` cannot reach a plotly point under its drag layer, and
`page.drag_and_drop` does not start an HTML5 drag from a small handle.
