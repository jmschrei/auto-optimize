# The dashboard: watching a search

A live dashboard over an `autooptimize/` directory, mounted in a JupyterLab
notebook output. It needs nothing instrumented and works against a finished
search or one this session did not start. It writes only its notebook
(`$WORK/dashboard.ipynb`, beside `autooptimize/`), its payload directory
(`autooptimize/monitor/` by default, else
`~/autoopt-monitor/<project>-<8 hex of the search's path>/`), an index entry
under `~/.cache/autoopt-monitor/`, and the GPU sample its publishers share and
the writer lock in `/dev/shm/dashboard-monitor-<uid>/` (the temporary
directory where `/dev/shm` is not writable); it never touches iterations,
records or code.

```bash
DASH=$AO/dashboard
```

## The publisher starts with the first iteration, always

```bash
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --start
```

`--start` writes `$WORK/dashboard.ipynb` *and* starts the publisher
behind it, so the page has data before anyone opens it. Run it as soon as
round 1's first iteration is spawned, on the machine running the search. It returns
after the first scan and one hardware sample, so a slow `nvidia-smi` delays it
by that call. Report the pid it prints and how to see the page:

| The user has | Tell them |
|---|---|
| JupyterLab serving the search's directory | open the notebook path it printed and run its one cell; right-click the output → **Create New View for Output** gives it a panel of its own |
| JupyterLab, but the search is outside the directory it serves | re-run with `-o` under the served root (below); the publisher is not disturbed |
| no JupyterLab | `autoopt.py status $WORK` in a terminal, and `--export` for a static HTML file of the page (*Managing publishers*) |

Re-running `--start` is safe, so re-run it whenever unsure: on the same
interval it prints `already running` and the existing pid, in whatever
directory the search's publisher writes; on another, it restarts the
publisher on that one.

```bash
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --start \
	-o <a directory Jupyter serves>/dashboard-<search name>.ipynb
```

## Opening it by hand

The cell `--start` writes is this, for a search a previous session started or
a finished one:

```python
import sys, importlib
sys.path.insert(0, '<absolute path of the skill>/scripts/dashboard')
import autoopt_monitor as am
am = importlib.reload(am)

am.show('<work>/autooptimize')
```

`show()` copies the front end and plotly into `autooptimize/monitor/`
(falling back to `~/autoopt-monitor/<project>-<8 hex of the search's path>/`
when the search is outside the tree Jupyter serves, or not writable), starts or
reuses the publisher, and renders the page. Paths with spaces, `#` or `?`
work.

| Argument (CLI flag) | Default | |
|---|---|---|
| `out_dir` (`--out`) | `autooptimize/monitor` | must be under the directory Jupyter serves; `show()` raises otherwise, `--start` warns |
| `base` (none) | `~/autoopt-monitor` | where the fallback out directory goes |
| `interval` (`--interval`) | 20 s | full scan. Raise it for a search whose runs take an hour, or past a few hundred iterations, when the payload is megabytes on every poll over a remote Jupyter connection. A publisher on another interval is restarted on this one |
| `resource_interval` (none) | 3 s | GPU and host sample; the GPUs card's Live toggle makes it 1 s for as long as the tab asks |
| `writer` (none) | `True` | `False` renders against a publisher someone else runs |
| `path` (`-o`), `make_notebook()` only | `$WORK/dashboard.ipynb` | |
| `overwrite` (`--no-overwrite`), `make_notebook()` only | `True` | keep a notebook the user edited |

The rest are `show()`'s. `make_notebook()` writes a cell that calls `show()`
with its other keyword arguments; the CLI passes `--out` and `--interval`.

GPU cards name each device's lease holder when `GPU_LEASE_DIR` names a lease
tool's directory of `gpu<index>.json` files, each a JSON object with any of
`gpu`, `user`, `note` (or `cmd`), `pid`, `acquired_at`, `expires_at` (epoch
seconds). A lease counts while its pid lives and it has not expired; a
malformed file is skipped. Unset, the cards show no holder.

## What to look at

Panels, marks and interactions are specified in `scripts/dashboard/README.md`,
*The page*.

| Question | Where |
|---|---|
| Is the publisher alive? | the topbar pulse, below |
| Best so far, and by how much | the summary band and the sidebar's Frontier card |
| Which ideas worked | the objective chart: the dotted staircase is the frontier; ✕ failed a gate and a hollow square has fewer runs than `runs_per_iteration`, and neither is ever on it |
| Is it noise? | *Confidence intervals* and *All runs* on the objective chart |
| What is running now | hollow diamonds on the chart's floor; ringed points are measured iterations whose subagent is still writing its README |
| Why an iteration scored what it did | click its tree node (the whole README) or log entry (the README, whole after SHOW MORE): every number, where the time went |
| How much code an iteration changed | hover its point or open its log entry: lines added and deleted, files modified, added and deleted, against its parent |
| What an iteration changed, line by line | click its point on the objective chart: its diff against its parent opens under the chart, old file left and new right; a second click or *close* shuts it |
| Whether a fresh batch of ideas helped | the objective chart's stage boundaries: one dashed line where the idea miner's stage changes |
| A gate or observer moving | *Every run* (a broken gate is red) and the observer charts |
| External validation | its own section (`references/external-validation.md`) |
| GPUs idle or contended | the GPU cards and the process table |
| Whether it ended, and how | the topbar's `ended <date> · A<id> <median> vs A0 <median> · <reason>` line; its tooltip has the paired timing from `final.json` |
| Which stages are done | the strip under the category chips: `interview complete · setup complete · … · ship not started · finish not started`, in `stages.py`'s own words (`complete`, `running`, `not started`) |
| An iteration's toolchain answer | `toolchain` in its open log entry (`no` or `specific`); `specific` carries the tooltip *not promotable* |

The ended line and the stage strip read `ended.json`, `final.json` and
`stages.py` through `scan.py`'s `after()`. The line is hidden until the search
ends, and a part that fails to read is blank and named in
`snapshot['after']['errors']`; the rest of the page still draws.

The pulse ages `state.json` against the larger of the scan interval and the
writer's measured scan cadence (the footer prints both): green under 2.5 of
those, orange under 10, and red beyond, with **publisher stopped · see
<writer.log>**. A failing scan turns it orange at once. What each message
beside it means: *When it looks wrong*, below.

## Managing publishers

```bash
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --start --interval 120
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --start --out <dir under the served root>
python3 $DASH/autoopt_monitor.py --status [$WORK/autooptimize]   # pid, search, out dir; tab-separated
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --stop       # every publisher of this search
python3 $DASH/autoopt_monitor.py $WORK/autooptimize --export out.html
```

A publisher outlives the notebook kernel. `autoopt.py end` writes
`final/dashboard.html` with `--export` (`references/step4-final.md`) and
leaves the publisher running, so the ended line and the stage strip stay
current; `end --reopen` finds it still running. Stop it with `--stop` once the
search is finished (step 6), or by hand for a search that is over some other
way.
The export is meant to be passed around, so it leaves out the hostname, the
GPUs' serial numbers and who holds a GPU lease, and writes the home directory
as `~`. The live page shows all of them.
One writer publishes into a directory, and a search has one writer: a second
writer into a directory refuses, and `--start` or `show()` stops any writer
into it that the pidfile does not name and any writer of the search into
another directory, so `--out` moves the publisher. Those checks see one host.
Writers on two hosts that share the directory (a notebook on one machine,
the search on another) coordinate through the heartbeat `writer.host`: the
writer publishing keeps the directory unless the other sees the search's
processes and it does not. The other pauses, writing nothing, until that
heartbeat is 10 s old.
`--status` without a search lists other sessions' publishers too. Stop only
your own, always naming the search: a bare `--stop` stops every session's
publishers, so never run it. `--stop` returns once the process has exited, so
`--start` can follow at once. A wrong directory prints one `error:` line and
exits 1.

`--export` scans once and writes one self-contained, read-only HTML file that
opens from disk anywhere and reads `snapshot of <time>` where the pulse was.
Use it to share a finished search, or as the page for a user without
JupyterLab.

`show()` and `--start` replace a publisher started before the current
`scan.py`, `gpu_scan.py`, `monitor_writer.py` or `autoopt.py`, and a running
writer re-copies the front end when it changes, so an edit reaches the page on
the next poll plus a re-run of the cell.

## When it looks wrong

| Symptom | Cause |
|---|---|
| `RuntimeError: the writer exited; see <out>/writer.log` | the writer died before its first `state.json`, usually an unwritable out directory. Read the end of `writer.log` |
| **scan failing · <reason> · last good scan N ago · see <writer.log>** | the scanner raises on this search (a bad `config.json`, a corrupt `state.json`); the writer keeps running and recovers on the next good scan. Fix the file it names; `python3 $DASH/scan.py $WORK/autooptimize` reproduces it |
| `N unreadable: …` beside the pulse | those iterations' records raise in the scanner (a malformed `result.json`) and their rows have status `unreadable`; the rest of the page is current |
| **the dashboard has been updated — re-run the cell** | the open page runs an older script than the one published; re-running the cell is the only fix |
| stuck on `Loading the dashboard…` | `dashboard.js` loaded but did not reach `startAutoopt`; the browser console names the error |
| `could not load …/dashboard.css` (or `.js`) | the out directory is outside the root of the server the notebook runs on; `--status` shows where it publishes |
| `connecting…` never clears | no `state.json` was read: the writer is not running or has not written yet. `--status`, then `writer.log` |
| `could not reload <module>: …` above the output | the reload chain hit an error in that module, and the old copy stays loaded. Fix it and re-run the cell |
| `TypeError: show() got an unexpected keyword argument` | the cell passes an argument `show()` does not take; `--start` rewrites the cell |
| `has no config.json` | pointed at something other than a search's `autooptimize/` or the directory holding it |
| Empty, but the directory is right | no iteration has a `result.json` yet; in-flight ones are the hollow diamonds on the floor |
| Numbers from another search | a publisher of another search held that output directory. `show()` replaces it and says so; `--status` shows which search each watches |
| Charts replaced by "Plotly failed to load" | plotly is not installed where the publisher runs: `pip install plotly` |
| `error: --export inlines plotly.min.js …` | the same, for `--export` |
| Host summary or process table says psutil is not installed | `pip install psutil`. Without `/proc` (macOS), psutil is also the only way `--status` finds publishers |
| GPU cards dimmed, **GPU reading N old: nvidia-smi is slow or failing** | the GPU query (NVML, or nvidia-smi where NVML will not load) has not answered for that long; the cards are its last answer. `time nvidia-smi` on the host. Publishers share one query, so many searches do not slow it |
| A timed GPU call is sometimes 0.2-2 s slower, the delay in its first allocations after `torch.cuda.empty_cache()` | each new nvidia-smi process stalls cudaMalloc in every CUDA process on the host while it starts. The publishers read through NVML and share one sample, so they start none; look for anything else on the host that runs nvidia-smi on a timer or on every command |
| GPU cards vanish and return, the notebook jumps, `--status` lists several pids for one search | several publishers write one search's payload. Re-run the cell or `--start`: it stops the extra writers |
| GPU cards flip between two sets of readings, the host summary's host name changes | publishers on two hosts write one payload directory on a shared filesystem. Re-run the cell, and `--start` on the other host: one of the two pauses |
| Red pulse, **publisher stopped** | the writer died; read its `writer.log`, then `--start` |
| A banner naming missing ids | a saved notebook restored old markup under a newer script; re-run the cell |
| `warning: <notebook> is outside the directory Jupyter serves` | the notebook cannot be opened from Lab; re-run with `-o` under the served root. The publisher is fine |
| `ValueError: <dir> is outside the directory Jupyter serves` | the output directory must be under the Jupyter root; pass `out_dir=` there |
| External validation section with no chart | configured but no round validated yet (`validate` refuses until `--approve`), or no measure reported a number; the sidebar card says which |

## Without a browser

`autoopt.py table` prints a row per iteration, as the charts plot them, and
`scan.py` prints the whole payload:

```bash
python3 $AO/autoopt.py table $WORK
python3 $DASH/scan.py $WORK/autooptimize | head -40
```

Answering a question about a search without a dashboard:
`references/reading-a-search.md`.
