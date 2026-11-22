# The dashboard's tests

These test the dashboard in `skills/auto-optimize-code/scripts/dashboard/`
(its `README.md` describes the page and the payload). They need playwright
with Chromium, jupyterlab, plotly and psutil, and are not run by
`tests/code/run_tests.py`.

| File | Does |
|---|---|
| `test_frontend.py` | every panel in headless Chromium, the publisher CLI, and the checks below |
| `test_notebook.py` | the JupyterLab mount, on a private server |
| `make_fixture.py` | builds the search the tests read |
| `spacing_check.py` | geometry checks for the spacing scale |

## Running them

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
