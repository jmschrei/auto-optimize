#!/usr/bin/env python3
"""Run one iteration: the same idea several times, aggregated into a verdict.

	CUDA_VISIBLE_DEVICES=2 python3 run_iteration.py autooptimize/iterations/3

One *run* is one execution of the benchmark command with one seed. One
*iteration* is every run of one idea, reduced to a median objective, a
pass/fail for each gate, and a median for each observer measure. This script
turns the first into the second and writes `result.json`.

What it does, in order:

1. Commits whatever is uncommitted in the iteration's worktree, so
   `git diff autoopt/<parent>..autoopt/<id>` is exactly the idea tried.
2. Times the cold first call on an empty compiler cache, when the search
   has compiled code (`first_call`), beside the parent's recorded value.
3. Runs one run, every gate and observer on it.
4. Runs the unit tests, then the task's compare against A0's cached
   outputs, when they are configured.
5. Runs the rest of `runs_per_iteration`, one seed each, every gate and
   observer on every run; one at a time, they stop at the first failure.
6. When the tests ran only the changed modules' files (`{selected}`), runs
   the whole suite for an iteration whose median passes the keep rule.
7. Records the keep verdict against the parent and, with
   `promote: auto`, promotes on it, then runs the closed round's approved
   external validation.
8. Writes `result.json` and a report for the subagent to paste back to
   the orchestrator.

Each stage runs only when the stages before it passed. A search made with
`init --staged` screens at `runs_per_iteration` and confirms to
`confirm_runs` when the median nears the frontier.

Every run's own output stays in `runs/seed-<N>/`: its `metrics.json`, its
`log.txt`, and whatever the benchmark saved beside them.

Re-running is cheap and safe: a seed whose `metrics.json` is already on disk
is not run again unless `--rerun` is passed. An iteration interrupted halfway
picks up where it stopped.
"""

import os
import sys
import json
import time
import shutil
import queue
import argparse
import statistics
import subprocess
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autoopt
from autoopt import OPS, fail, git, fmt, cache_dir


def check_lease(config):
	"""Refuse to start without a GPU lease: `CUDA_VISIBLE_DEVICES` naming the
	GPUs this iteration owns, or `gpu_command` to take one (`autoopt.hold_lease`).

	Without it every run would see every GPU, and two iterations would land
	on the same card.
	"""

	return autoopt.hold_lease(config)


def leased_devices(config, wanted):
	"""The GPU indices to spread this iteration's runs over.

	A lease of four GPUs is `CUDA_VISIBLE_DEVICES=2,3,5,6`; each run takes
	one of them. Fewer leased than asked for is not an error -- the iteration
	simply runs that many at a time -- but it is worth saying, because the
	usual cause is asking for more than the machine had free.
	"""

	if not config.get('gpu', True):
		return [None] * max(1, wanted)

	raw = os.environ.get('CUDA_VISIBLE_DEVICES', '')
	devices = [item.strip() for item in raw.split(',') if item.strip()]

	if not devices:
		return [None]

	if wanted > len(devices):
		print('  note: {} GPU(s) requested per iteration, {} leased; running '
			'{} run(s) at a time'.format(wanted, len(devices), len(devices)))

	return devices[:max(1, wanted)]


def load_metrics(run_dir, config):
	"""Read and check one run's `metrics.json`.

	Returns `(record, error)`. A gate the benchmark did not measure is an
	error, not a pass: an unmeasured constraint is the one that silently
	stops holding.
	"""

	path = os.path.join(run_dir, 'metrics.json')

	try:
		with open(path) as infile:
			metrics = json.load(infile)
	except OSError:
		return None, 'wrote no metrics.json'
	except ValueError as error:
		return None, 'metrics.json is not valid JSON: {}'.format(error)

	if not isinstance(metrics, dict):
		return None, 'metrics.json is not an object'

	value = metrics.get('objective')

	if not isinstance(value, (int, float)) or value != value:
		return None, 'metrics.json has no numeric "objective" (got {!r})'.format(
			value)

	record = {'objective': float(value), 'gates': {}, 'observers': {}}

	for gate in config['gates']:
		found = (metrics.get('gates') or {}).get(gate['name'],
			metrics.get(gate['name']))

		if not isinstance(found, (int, float)) or found != found:
			return None, 'metrics.json has no numeric value for the gate ' \
				'"{}"'.format(gate['name'])

		record['gates'][gate['name']] = float(found)

	for observer in config['observers']:
		found = (metrics.get('observers') or {}).get(observer['name'],
			metrics.get(observer['name']))

		record['observers'][observer['name']] = (float(found)
			if isinstance(found, (int, float)) and found == found else None)

	record['note'] = metrics.get('note') or ''

	return record, None


def execute(command, iteration, seed, run_dir, timeout, device=None,
	src=None, cache='', sample=False):
	"""One run. Returns `(returncode, seconds)`; output is tee'd to log.txt.

	`device` pins the run to one leased GPU by setting `CUDA_VISIBLE_DEVICES`
	for the child alone. The value is a physical index out of the lease, so
	two runs of the same iteration never land on the same card. With
	`sample`, the most threads the run had running at once goes to
	`<run_dir>/workers.json`.
	"""

	os.makedirs(run_dir, exist_ok=True)

	# Relative, so a log or a saved output is found from the iteration
	# directory whichever machine reads it later.
	relative = os.path.relpath(run_dir, iteration)
	line = autoopt.fill(command, seed=seed, out=relative, out_abs=run_dir,
		iteration=iteration, src=src or iteration, cache=cache)

	start = time.time()

	with open(os.path.join(run_dir, 'log.txt'), 'w') as log:
		log.write('$ {}\n\n'.format(line))
		log.flush()

		environment = (dict(os.environ, CUDA_VISIBLE_DEVICES=str(device))
			if device is not None else None)

		found = {} if sample else None
		code, timed_out = autoopt.run_shell(line, iteration, log,
			environment, timeout, found)

		if sample:
			autoopt.write_json(os.path.join(run_dir, 'workers.json'), found)

		if timed_out:
			log.write('\n[killed after {}s by --timeout]\n'.format(timeout))

	return code, time.time() - start


def select_tests(config, checkout, parent_commit, commit):
	"""The test files for what this iteration changed, or None for all.

	`pkg/foo.py` maps to every `test_foo.py` under the tests path, and a
	changed test file selects itself. Anything that maps to nothing -- a
	`conftest.py`, an `__init__.py`, a file that is not Python, a module
	with no test file of its own -- runs the whole suite, since a subset
	that misses the code it should cover is a pass nobody earned.
	"""

	tests = config['tests']
	path = tests.get('path') or 'tests'

	if '{selected}' not in tests['command']:
		return None, 'the test command has no {selected}'

	if not parent_commit:
		return None, 'the parent has no measured commit'

	try:
		changed = git('-C', checkout, 'diff', '--name-only', parent_commit,
			commit).split()
	except RuntimeError as error:
		return None, 'git diff against the parent failed: {}'.format(error)

	if not changed:
		return None, 'nothing changed against the parent'

	root = os.path.join(checkout, path)
	available = {}

	for directory, _, names in os.walk(root):
		for name in names:
			if name.startswith('test_') and name.endswith('.py'):
				available.setdefault(name, []).append(os.path.relpath(
					os.path.join(directory, name), checkout))

	selected = set()
	prefix = path.rstrip('/') + '/'

	for name in changed:
		base = os.path.basename(name)

		if name.startswith(prefix) and base.startswith('test_') \
			and base.endswith('.py'):
			selected.add(name)
		elif (not name.endswith('.py') or base == '__init__.py'
			or name.startswith(prefix)):
			return None, '{} maps to no single test file'.format(name)
		elif 'test_' + base in available:
			selected.update(available['test_' + base])
		else:
			return None, 'no test_{} under {}'.format(base, path)

	return sorted(selected), 'the tests for {}'.format(', '.join(changed))


def untracked(checkout):
	"""The worktree's untracked files git does not ignore, or None when it is
	not a git worktree."""

	done = subprocess.run(['git', '-C', checkout, 'ls-files', '--others',
		'--exclude-standard', '-z'], capture_output=True, text=True)

	if done.returncode != 0:
		return None

	return set(name for name in done.stdout.split('\0') if name)


def remove_created(checkout, before):
	"""Remove the untracked files that appeared since `before` (a snapshot
	from `untracked`), and the directories that leaves empty. Returns their
	paths, relative to `checkout`."""

	after = untracked(checkout)

	if before is None or after is None:
		return []

	created = sorted(after - before)

	for name in created:
		path = os.path.join(checkout, name)

		try:
			os.remove(path)
		except OSError:
			continue

		folder = os.path.dirname(path)

		while os.path.abspath(folder) != os.path.abspath(checkout):
			try:
				os.rmdir(folder)
			except OSError:
				break
			folder = os.path.dirname(folder)

	return created


def run_tests(config, iteration, checkout, commit, rerun, parent_commit=None,
	full=False):
	"""The package's own test suite, once per iteration, as a gate.

	Once and not per run: the suite checks the code, which is the same for
	every seed. It runs in the worktree, so it tests this iteration's code.
	The verdict is kept in `unit-tests/result.json` beside the commit it
	tested, and a resumed iteration whose commit has not moved reuses it.

	With `{selected}` in the command, only the test files for the changed
	modules run (`select_tests`); `full=True` runs the whole suite, which an
	iteration whose median passes the keep rule gets before it can be
	promoted.
	"""

	tests = config['tests']
	directory = os.path.join(iteration, 'unit-tests')
	record_path = os.path.join(directory, 'result.json')

	earlier = 0.0

	record = None

	if os.path.exists(record_path):
		try:
			with open(record_path) as infile:
				record = json.load(infile)
		except ValueError as error:
			# Only a cache of the verdict: running the tests again rebuilds
			# it, which is better than refusing the iteration.
			print('  unit tests: {} is not valid JSON ({}); running them '
				'again'.format(record_path, error))

	if record is not None:
		# A full pass after a subset pass on the same code: both are time the
		# iteration spent on tests, and the dashboard's breakdown counts both.
		if record.get('commit') == commit:
			earlier = record.get('seconds_total', record.get('seconds') or 0.0)

		if not rerun and record.get('commit') == commit and (not full
			or record.get('scope', 'full') == 'full'):
			print('  unit tests: {} (already run on {}, {})'.format(
				'pass' if record['pass'] else 'FAIL', commit[:10],
				record.get('scope', 'full')))
			return record

	selected, reason = ((None, 'the full suite was asked for') if full
		else select_tests(config, checkout, parent_commit, commit))
	scope = 'full' if selected is None else 'subset'
	arguments = [tests.get('path') or 'tests'] if selected is None \
		else selected

	os.makedirs(directory, exist_ok=True)
	line = autoopt.fill(tests['command'], src=checkout, iteration=iteration,
		out=directory, cache=cache_dir(iteration, commit), selected=arguments)

	print('  unit tests: running {} ({})…'.format(
		'the full suite' if scope == 'full' else ' '.join(arguments), reason),
		flush=True)
	start = time.time()

	before = untracked(checkout)

	with open(os.path.join(directory, 'log.txt'), 'w') as log:
		log.write('$ {}\n\n'.format(line))
		log.flush()

		code, timed_out = autoopt.run_shell(line, checkout, log,
			timeout=tests.get('timeout_seconds'))

		if timed_out:
			log.write('\n[killed after {}s]\n'.format(
				tests['timeout_seconds']))

		# Files the test run wrote into the worktree are not the
		# iteration's change: left there, the next commit takes them in.
		created = remove_created(checkout, before)

		if created:
			log.write('\n[removed {} file(s) the tests created in the '
				'worktree: {}]\n'.format(len(created), ', '.join(created)))

	record = {'command': line, 'exit_code': code, 'pass': code == 0,
		'scope': scope, 'selected': selected, 'reason': reason,
		'seconds': time.time() - start, 'commit': commit,
		'log': os.path.relpath(os.path.join(directory, 'log.txt'), iteration),
		'removed': created}

	record['seconds_total'] = record['seconds'] + earlier
	autoopt.record_artifacts(os.path.dirname(os.path.dirname(os.path.abspath(
		iteration))), created)

	autoopt.write_json(record_path, record)

	print('  unit tests: {} {} (exit {}, {:.0f} s; {})'.format(
		'pass' if record['pass'] else 'FAIL', scope, code, record['seconds'],
		record['log']), flush=True)

	return record


def measured_commit(run_dir):
	"""The commit a stored run measured, or None when the run directory has
	no `commit` file."""

	try:
		with open(os.path.join(run_dir, 'commit')) as infile:
			return infile.read().strip() or None
	except OSError:
		return None


def start_load(run_dir):
	"""The one-minute load average when a stored run started, or None."""

	try:
		with open(os.path.join(run_dir, 'load')) as infile:
			return float(infile.read().strip())
	except (OSError, ValueError):
		return None


def reusable(run_dir, config, commit):
	"""A stored run's record, if it measured this commit; otherwise None.

	Without the commit check, code committed after an iteration was measured
	would inherit the old code's numbers on the next plain re-run, and be
	recorded as measured.
	"""

	record = load_metrics(run_dir, config)[0]

	if record is None:
		return None

	stored = measured_commit(run_dir)

	if stored not in (None, commit):
		return None

	record['load'] = start_load(run_dir)

	return record


def spread(values):
	"""Sample standard deviation, or 0.0 when there are too few values."""

	values = [v for v in values if v is not None]

	return statistics.stdev(values) if len(values) > 1 else 0.0


def worst_of(values, op, threshold):
	"""The value that comes closest to breaking the gate, or breaks it most."""

	if not values:
		return None
	if op == '<=':
		return max(values)
	if op == '>=':
		return min(values)

	return max(values, key=lambda value: abs(value - threshold))


def aggregate(root, config, meta, records, failures, stage, frontier_value,
	band, tests=None):
	"""Everything the orchestrator and the dashboard read: `result.json`."""

	objective = config['objective']
	values = [record['objective'] for record in records]

	gates = {}
	for gate in config['gates']:
		gate_values = [record['gates'][gate['name']] for record in records]
		test = OPS[gate['op']]
		passes = [test(value, gate['threshold'], gate.get('tol'))
			for value in gate_values]

		gates[gate['name']] = {
			'op': gate['op'],
			'threshold': gate['threshold'],
			'tol': gate.get('tol'),
			'values': gate_values,
			'median': statistics.median(gate_values) if gate_values else None,
			'worst': worst_of(gate_values, gate['op'], gate['threshold']),
			'pass': bool(gate_values) and all(passes),
			'n_violations': sum(1 for ok in passes if not ok),
		}

	observers = {}
	for observer in config['observers']:
		seen = [record['observers'].get(observer['name'])
			for record in records]
		seen = [value for value in seen if value is not None]

		observers[observer['name']] = {
			'values': seen,
			'median': statistics.median(seen) if seen else None,
			'std': spread(seen),
		}

	if failures or not records:
		state = 'error'
	elif not all(gate['pass'] for gate in gates.values()):
		state = 'gate-failed'
	elif config.get('tests') and not (tests or {}).get('pass'):
		# A suite that failed, or never ran, is not a pass.
		state = 'gate-failed'
	elif len(records) < config['runs_per_iteration'] and not meta.get('split'):
		# Fewer runs than an iteration's count, so this is a smoke test or an
		# interrupted iteration, not a measurement. Keeping it out of `ok`
		# keeps it off the frontier, where one lucky seed would be what every
		# later iteration is compared with.
		state = 'partial'
	else:
		state = 'ok'

	best = (max(records, key=lambda r: r['objective'])
		if objective['direction'] == 'max' and records
		else min(records, key=lambda r: r['objective']) if records else None)

	return {
		'id': meta['id'],
		'parent': meta.get('parent'),
		'prediction': meta.get('prediction'),
		'round': meta.get('round'),
		'branch': meta.get('branch'),
		'title': meta.get('title'),
		'category': meta.get('category'),
		'topic': meta.get('topic', ''),
		'commit': meta.get('commit'),
		# A same-code control is never on the frontier (autoopt.frontier).
		'control': bool(meta.get('control')),
		# A checked-only iteration (`meta.split`), one run: never on the frontier either.
		'split': bool(meta.get('split')),
		'status': state,
		'stage': stage,
		'n_runs': len(records),
		'n_failed': len(failures),
		'objective': {
			'name': objective['name'],
			'direction': objective['direction'],
			'unit': objective.get('unit', ''),
			'median': statistics.median(values) if values else None,
			'mean': statistics.fmean(values) if values else None,
			'std': spread(values),
			'min': min(values) if values else None,
			'max': max(values) if values else None,
			'values': values,
		},
		'gates': gates,
		'observers': observers,
		'frontier_at_start': frontier_value,
		'noise_band': band,
		'best_run': best['dir'] if best else None,
		'runs': [{key: record.get(key) for key in ('seed', 'dir', 'objective',
			'seconds', 'gates', 'observers', 'note', 'load')}
			for record in records],
		'failures': failures,
		'tests': tests,
		'seconds': sum(record['seconds'] for record in records),
		'gpu': os.environ.get('CUDA_VISIBLE_DEVICES'),
		'finished': autoopt.now(),
	}


def workers_seen(iteration, records, expected):
	"""`{expected, busy_peak, procs_peak, samples}` over the runs'
	`workers.json`: the most threads any run had running at once."""

	seen = {'expected': expected, 'busy_peak': 0, 'procs_peak': 0, 'samples': 0}

	for record in records:
		path = os.path.join(iteration, record['dir'], 'workers.json')

		try:
			one = autoopt.read_json(path)
		except (OSError, ValueError):
			continue

		for key in ('busy_peak', 'procs_peak'):
			seen[key] = max(seen[key], one.get(key, 0))

		seen['samples'] += one.get('samples', 0)

	return seen


def commit_worktree(iteration, meta):
	"""Commit the idea, so the branch carries the change and nothing else.

	A subagent that forgot to commit would otherwise leave the diff to be
	reconstructed from a directory that the next reader has no reason to
	trust.
	"""

	skip = []
	root = os.path.dirname(os.path.dirname(os.path.abspath(os.path.dirname(
		iteration) if os.path.basename(iteration) == 'src' else iteration)))
	known = autoopt.test_artifacts(root) if os.path.isdir(os.path.join(root,
		'iterations')) else set()
	left = sorted(name for name in untracked(iteration) or () if name in
		known)

	if left:
		print('  commit: leaving out {} file(s) the unit tests create: {}'
			.format(len(left), ', '.join(left[:5])))
		skip += [':(exclude){}'.format(name) for name in left]

	skip += [':(exclude){}'.format(name) for name in autoopt.RUNNER_FILES if
		os.path.exists(os.path.join(iteration, name)) and subprocess.run(['git',
		'-C', iteration, 'check-ignore', '-q', name], capture_output=True)
		.returncode != 0]
	spec = ['--', '.'] + skip

	dirty = git('-C', iteration, 'status', '--porcelain', *spec).strip()

	if not dirty:
		return git('-C', iteration, 'rev-parse', 'HEAD').strip()

	git('-C', iteration, 'add', '-A', *spec)
	name = '{}{}'.format(autoopt.LABEL_LETTER, meta['id'])

	# A machine with no git identity would refuse the commit, and with it
	# the whole iteration. The one a re-measurement outside the search uses fills only what is
	# missing, so a configured identity is never overridden.
	identity = []

	for key, value in (('user.name', 'auto-optimize'),
		('user.email', 'auto-optimize@localhost')):
		if not subprocess.run(['git', '-C', iteration, 'config', key],
			capture_output=True, text=True).stdout.strip():
			identity += ['-c', '{}={}'.format(key, value)]

	git('-C', iteration, *identity, 'commit', '-q', '-m', '{}: {}'.format(
		name, meta.get('title') or 'iteration {}'.format(name)))

	return git('-C', iteration, 'rev-parse', 'HEAD').strip()


def decide(config, median, frontier_value, band):
	"""A staged search: is this iteration close enough to the frontier to
	confirm?

	Close enough means "within one seed-noise band", not "better": a real
	improvement read through three seeds lands below the frontier often
	enough that a strict test throws most of them away. The band is the wider
	of the reference spread (the root's, as measured when the phase opened)
	and this iteration's.
	"""

	if frontier_value is None or median is None:
		return True, 'no frontier yet'

	if config['objective']['direction'] == 'max':
		promote = median >= frontier_value - band
	else:
		promote = median <= frontier_value + band

	return promote, '{} vs frontier {} ± band {}'.format(fmt(median),
		fmt(frontier_value), fmt(band))


def run_rows(result, config, parent, parent_label):
	"""One line per run: its objective and each observer, the parent's run
	of the same seed in brackets, any gate the run broke, and the load when it
	started (this search's own runs included). What a subagent otherwise reads
	out of every `metrics.json`."""

	theirs = {run.get('seed'): run for run in (parent or {}).get('runs') or []}
	rows = []

	for run in result.get('runs') or []:
		other = theirs.get(run['seed']) or {}
		parts = ['{}{}'.format(fmt(run['objective']), ' ({} {})'.format(
			parent_label, fmt(other['objective']))
			if other.get('objective') is not None else '')]

		for name, value in (run.get('observers') or {}).items():
			before = (other.get('observers') or {}).get(name)
			parts.append('{} {}{}'.format(name, fmt(value), ' ({})'.format(
				fmt(before)) if before is not None else ''))

		for gate in config['gates']:
			value = (run.get('gates') or {}).get(gate['name'])
			if value is not None and not OPS[gate['op']](value,
					gate['threshold'], gate.get('tol')):
				parts.append('{} {} BROKEN'.format(gate['name'], fmt(value)))

		if run.get('load') is not None:
			parts.append('load {:.1f}'.format(run['load']))

		rows.append((run, '  '.join(parts)))

	return rows


def report(result, config, reason, parent=None, parent_label=None):
	"""The block the subagent pastes back to the orchestrator. `parent` is
	the parent's result: the keep rule's reference, beside which the median
	and each observer are given."""

	objective = result['objective']
	before = parent if (parent or {}).get('objective') else None

	# One label column, wide enough for the longest gate or observer name:
	# the orchestrator reads these side by side across iterations.
	labels = ([objective['name'], 'status', 'frontier', 'best so far',
		'parent', 'wall clock',
		'unit tests']
		+ ['gate ' + name for name in result['gates']]
		+ ['obs ' + name for name in result['observers']]
		+ ['run {}'.format(run['seed']) for run in result.get('runs') or []])
	width = max(len(label) for label in labels) + 2

	def row(label, text):
		return '{}{}'.format(label.ljust(width), text)

	lines = ['', 'iteration {}{}  {}  [{}]'.format(
		autoopt.LABEL_LETTER, result['id'],
		result.get('title'), result.get('category')),
		row('status', '{} ({}{} run{})'.format(result['status'],
			result['stage'] + ', ' if config.get('staged') else '',
			result['n_runs'],
			'' if result['n_runs'] == 1 else 's')),
		row(objective['name'], '{} median, {} ± {} (min {}, max {})'.format(
			fmt(objective['median']), fmt(objective['mean']),
			fmt(objective['std'], 3), fmt(objective['min']),
			fmt(objective['max']))),
		row('best so far', '{} at start{}; {}'.format(
			fmt(result['frontier_at_start']), ', held by {}, not in the '
			'root'.format(result['frontier_holder'])
			if result.get('frontier_holder') is not None and not result.get(
				'frontier_on_path', True) else '', reason))]

	if before is not None:
		lines.append(row('parent', '{} {} median, what the keep rule judges '
			'against'.format(parent_label, fmt(before['objective']['median']))))

	for name, gate in result['gates'].items():
		lines.append(row('gate ' + name, '{} {} {}  worst {}{}'.format(
			'PASS' if gate['pass'] else 'FAIL', gate['op'],
			fmt(gate['threshold']), fmt(gate['worst']),
			'' if gate['pass'] else '  ({} run(s) broke it)'.format(
				gate['n_violations']))))

	for name, observer in result['observers'].items():
		theirs = ((before or {}).get('observers') or {}).get(name) or {}
		lines.append(row('obs ' + name, '{} median{}'.format(
			fmt(observer['median']), ' ({} {})'.format(parent_label,
				fmt(theirs['median'])) if theirs.get('median') is not None
				else '')))

	lines += [row('run {}'.format(run['seed']), text)
		for run, text in run_rows(result, config, before, parent_label)]

	if result.get('tests'):
		tests = result['tests']
		lines.append(row('unit tests', '{} {} (exit {}; {})'.format(
			'PASS' if tests['pass'] else 'FAIL', tests.get('scope', 'full'),
			tests['exit_code'], tests['log'])))

	if result['failures']:
		lines.append(row('failures', '; '.join(
			'seed {}: {}'.format(failure['seed'], failure['error'])
			for failure in result['failures'])))

	lines.append(row('wall clock', '{:.1f} min over {} run(s)'.format(
		result['seconds'] / 60.0, result['n_runs'])))

	return '\n'.join(lines)


def promote_on_keep(root, config, meta, result, no_promote=False):
	"""With `promote: auto`, promote an iteration that passes
	the keep rule while its parent is still the root, then run the approved
	external validation of the round that closed. Returns the report's
	lines."""

	keep = result.get('keep')

	if not keep:
		return []
	if keep['verdict'] != 'pass':
		return ['keep: no, ' + keep['why']]
	if no_promote:
		return ['keep: passes ({}); not promoted (--no-promote)'.format(
			keep['why'])]
	if config.get('promote', 'orchestrator') != 'auto':
		return ['keep: passes ({}); promotion is the orchestrator\'s '
			'(config "promote")'.format(keep['why'])]

	head = autoopt.load_state(root)['root']

	if meta['id'] in autoopt.lineage(root, head):
		return ['keep: passes; already on the root\'s path']

	done = subprocess.run([sys.executable, os.path.join(HERE, 'autoopt.py'),
		'promote', root, '--id', str(meta['id'])], capture_output=True,
		text=True)

	if done.returncode != 0:
		message = (done.stderr or done.stdout).strip().splitlines()[-1:]
		message = message[0] if message else 'exit {}'.format(done.returncode)
		keep['promoted'] = False
		autoopt.write_json(os.path.join(autoopt.iteration_dir(root,
			meta['id']), 'result.json'), result)
		return ['keep: passes ({}); not promoted: {}'.format(keep['why'],
			message.replace('error: ', ''))]

	keep['promoted'] = True
	keep['round_closed'] = autoopt.load_state(root)['round'] - 1
	lines = ['keep: promoted to the root ({}); round {} '
		'closed'.format(keep['why'], keep['round_closed'])]
	# promote profiled the new root (checks.profile) and recorded its time.
	lines += [line for line in done.stdout.splitlines()
		if line.startswith('profile: ')]
	seconds = (autoopt.load_result(root, meta['id']) or {}).get(
		'profile_seconds')
	if seconds is not None:
		result['profile_seconds'] = seconds
	validation = config.get('external_validation') or {}

	if validation.get('command') and validation.get('approved'):
		ran = subprocess.run([sys.executable, os.path.join(HERE, 'autoopt.py'),
			'validate', root, '--round', str(keep['round_closed'])],
			capture_output=True, text=True)
		record = autoopt.load_validation(root, keep['round_closed']) or {}
		keep['validation'] = {'round': keep['round_closed'],
			'ok': record.get('ok'), 'exit': ran.returncode}
		lines.append('validation, round {}: {}{}'.format(keep['round_closed'],
			'ok' if record.get('ok') else 'NEEDS ATTENTION',
			' ({})'.format(', '.join('{} {}'.format(key, fmt(value))
				for key, value in (record.get('measures') or {}).items()))
			if record.get('measures') else ''))
	elif validation.get('command'):
		lines.append('validation: not run, the user has not approved it')

	autoopt.write_json(os.path.join(autoopt.iteration_dir(root, meta['id']),
		'result.json'), result)
	return lines


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)

	parser.add_argument('iteration', help='autooptimize/iterations/<id>')
	parser.add_argument('--runs', type=int, help='override '
		'runs_per_iteration from config.json')
	parser.add_argument('--confirm-runs', type=int, help='a search made with '
		'init --staged: override confirm_runs from config.json')
	parser.add_argument('--stage', choices=('screen', 'confirm', 'all'),
		default='all', help='a search made with init --staged: screen stops '
			'after the screening runs; confirm goes straight to the full '
			'count; all screens, then confirms if the median earns it '
			'(default)')
	parser.add_argument('--rerun', action='store_true',
		help='re-run seeds that already have a metrics.json')
	parser.add_argument('--no-promote', action='store_true',
		help='never promote, whatever config "promote" says, and run no '
			'external validation')
	parser.add_argument('--full-tests', action='store_true',
		help='run the whole unit-test suite rather than the files for the '
			'changed modules; what promote needs, and what the runner gives '
			'an iteration whose median passes the keep rule itself')
	parser.add_argument('--dry-run', action='store_true',
		help='print the runs that would happen, and stop')
	parser.add_argument('--background', action='store_true',
		help='start this call detached, writing <iteration>/probe/runner.log '
			'(ending `exit <code>`), and return; watch it with autoopt.py wait')

	args = parser.parse_args(argv)

	iteration = os.path.abspath(os.path.expanduser(args.iteration))

	if not os.path.isdir(iteration):
		fail('not a directory: {}'.format(iteration))

	if args.background:
		return autoopt.detach(os.path.abspath(__file__), sys.argv[1:],
			os.path.join(iteration, 'probe', 'runner.log'))

	root = os.path.dirname(os.path.dirname(iteration))
	root = autoopt.resolve(root)
	config = autoopt.load_config(root)

	problem = autoopt.harness_problem(root)

	if problem:
		fail(problem)

	meta = autoopt.load_meta(root, os.path.basename(iteration))

	if meta is None:
		fail('{} has no meta.json; make the iteration with `autoopt.py new` '
			'rather than by hand'.format(iteration))

	screen = args.runs or config['runs_per_iteration']
	confirm = args.confirm_runs or config['confirm_runs']
	staged = config['staged'] and args.stage != 'screen'

	# One count of runs: every iteration runs it, and it is the depth.
	if not config['staged']:
		if args.confirm_runs or args.stage == 'confirm':
			fail('--confirm-runs and --stage confirm are for a staged search; '
				'this one runs runs_per_iteration ({}) on every iteration'
				.format(config['runs_per_iteration']))
		confirm = config['runs_per_iteration']

	# A staged search measures the baseline, which sets the frontier and the
	# noise band, at the full count from the start, unless the
	# caller asked for a count: a smoke run on the baseline stays one run.
	if (meta['id'] == 0 and config['staged'] and args.runs is None
		and args.stage == 'all'):
		screen = confirm

	if args.stage == 'confirm':
		screen = confirm

	# A checked-only iteration (`meta.split`) is not measured: one run for
	# the gates and the tests, since five would cost a slow base's time five
	# times for each of them.
	earlier = autoopt.load_result(root, meta['id']) or {}

	if meta.get('split') and args.runs is None and earlier.get('n_runs', 0) \
			<= 1:
		screen = confirm = 1
		staged = False

	if args.dry_run:
		print('would run seeds {} in {}'.format(
			list(range(config['seed_base'], config['seed_base'] + screen)),
			iteration))
		return 0

	autoopt.require_spec(root, 'run {}'.format(autoopt.label(root, meta['id'])))

	# A pruned iteration keeps its record but not its checkout, so the code
	# this would run is in the branch and not on disk.
	checkout = autoopt.checkout_dir(root, meta['id'], config)

	if not os.path.exists(os.path.join(checkout, '.git')):
		fail('{} has no git checkout: it was pruned, or made by hand. '
			'Restore it with:\n\n  python3 {} restore {} --id {}\n'.format(
				checkout, os.path.join(HERE, 'autoopt.py'), root,
				meta['id']))

	meta['commit'] = commit_worktree(checkout, meta)
	autoopt.write_meta(root, meta['id'], meta)

	# A compare reads A0's cached outputs: cache them before any run is
	# spent, when they are missing or the gates or the checks changed.
	if (config.get('checks') or {}).get('compare'):
		autoopt.ensure_reference(root, config)

	results = [r for r in autoopt.all_results(root) if r.get('id') != meta['id']]
	direction = config['objective']['direction']

	state = autoopt.load_state(root)
	frontier_value, frontier_id = autoopt.search_frontier(
		root, results, direction)

	# Whether this iteration's earlier result already held the frontier. A
	# re-run of it (to add --full-tests, say) is compared above against the
	# runner-up, and must not announce that as a new frontier.
	held = autoopt.search_frontier(root, autoopt.all_results(root),
		direction)[1] == meta['id']

	# The band's reference spread is the root's, re-measured when each phase
	# opens (`autoopt.py loop`): after a tenfold speedup the baseline's
	# spread is far wider than the noise at the new scale.
	baseline_spread = autoopt.band_spread(root, state)

	# Runs already on disk from an earlier attempt, counted so a re-run
	# reports every run it has. A staged search would otherwise re-take its
	# decision against a frontier that has since moved, stop at the
	# screening count, and rewrite result.json with fewer runs than were
	# measured.
	already = 0

	if not args.rerun:
		for index in range(confirm):
			run_dir = os.path.join(iteration, 'runs',
				'seed-{}'.format(config['seed_base'] + index))

			if reusable(run_dir, config, meta['commit']) is None:
				break

			already = index + 1

	# Only when a run could actually happen. Re-aggregating an iteration
	# whose seeds are all on disk needs no GPU, and demanding a lease for it
	# means queueing behind other GPU jobs to re-read a directory.
	if already < confirm:
		check_lease(config)

	# The baseline sets the frontier and the noise band: measured beside
	# another job, it makes every later iteration look like a win once that
	# job ends. On a CPU benchmark that is the user's call.
	# Never a refusal: whether to measure under load is the user's call,
	# asked before this runs (`autoopt.py busy`); what was busy is recorded.
	busy_at_start = []
	if (meta['id'] == 0 and already < confirm and not config.get('gpu', True)
			and not os.environ.get('AUTOOPT_SKIP_QUIET_CHECK')):
		busy_at_start = [{'pid': pid, 'user': user, 'percent': round(percent),
			'command': command} for pid, user, percent, command in
			autoopt.busy_processes(root)]
		if busy_at_start:
			print('  the machine is busy: {}. The baseline is measured under '
				'this load: say so in A0\'s README and the ledger '
				'(references/step2-baseline.md, *Check the machine is quiet*)'
				.format(autoopt.busy_words(busy_at_start[:8])), flush=True)

	records, failures = [], []
	stage = 'screen'
	promoted = False
	reason = ('the baseline: the frontier and the noise band start here'
		if meta['id'] == 0 else 'not evaluated')

	if already > screen:
		stage = 'confirmed' if already >= confirm else 'screen'
		reason = 're-run of {} run(s) already on disk'.format(already)

	print('iteration {} ({}), frontier {} = {} from iteration {}'.format(
		autoopt.label(root, meta['id']), meta.get('title'),
		config['objective']['name'], fmt(frontier_value),
		autoopt.label(root, frontier_id)))

	devices = leased_devices(config, config.get('gpus_per_agent', 1) or 1)

	# A run takes a device off the queue and gives it back when it ends, so
	# each GPU holds one run at a time. Assigning by seed instead put the
	# next seed on a busy card whenever a run finished out of turn.
	free = queue.Queue()
	for device in devices:
		free.put(device)

	def one(seed):
		"""Run or recover a single seed. Returns a record, or None."""

		run_dir = os.path.join(iteration, 'runs', 'seed-{}'.format(seed))
		record = reusable(run_dir, config, meta['commit'])

		if record is not None and not args.rerun:
			record.update(seed=seed, dir=os.path.relpath(run_dir, iteration),
				seconds=record.get('seconds', 0.0))
			return record, None, '  seed {}: already done, {} = {}'.format(
				seed, config['objective']['name'], fmt(record['objective']))

		if os.path.isdir(run_dir):
			# --rerun, or a run of an older commit: its numbers are not this
			# code's.
			shutil.rmtree(run_dir)

		device = free.get()
		load = os.getloadavg()[0] if hasattr(os, 'getloadavg') else None

		try:
			code, seconds = execute(config['command'], iteration, seed,
				run_dir, config.get('timeout_seconds'), device, checkout,
				cache_dir(iteration, meta['commit']), bool(config.get('workers')))
		finally:
			free.put(device)

		record, error = load_metrics(run_dir, config)

		if code != 0 or record is None:
			message = (error or 'exit code {}'.format(code)) if record is None \
				else 'exit code {}'.format(code)

			return None, {'seed': seed, 'error': message,
				'dir': os.path.relpath(run_dir, iteration),
				'seconds': seconds}, \
				'  seed {}: FAILED -- {} (see {}/log.txt)'.format(seed,
					message, os.path.relpath(run_dir, iteration))

		with open(os.path.join(run_dir, 'commit'), 'w') as out:
			out.write(meta['commit'] + '\n')

		if load is not None:
			with open(os.path.join(run_dir, 'load'), 'w') as out:
				out.write('{:.2f}\n'.format(load))

		record.update(seed=seed, dir=os.path.relpath(run_dir, iteration),
			seconds=seconds, load=load)

		return record, None, '  seed {}: {} = {} in {:.1f} min'.format(seed,
			config['objective']['name'], fmt(record['objective']),
			seconds / 60.0)

	def passes(record):
		return all(OPS[gate['op']](record['gates'][gate['name']],
			gate['threshold'], gate.get('tol')) for gate in config['gates'])

	def batch(first_index, last_index):
		"""Run a contiguous block of seeds, len(devices) at a time. One at a
		time, the block stops at the first run that fails or breaks a gate:
		the iteration's verdict is settled, and the rest would measure
		nothing."""

		seeds = [config['seed_base'] + index
			for index in range(first_index, last_index)]

		if not seeds:
			return

		if len(devices) == 1:
			# Serial, and printed as each finishes, which is what a single
			# GPU search has always looked like.
			for seed in seeds:
				print('  seed {}: running…'.format(seed), flush=True)
				record, failure, line = one(seed)
				print(line, flush=True)

				if record is not None:
					records.append(record)
				elif failure is not None:
					failures.append(failure)

				if failure is not None or (record is not None
						and not passes(record)):
					if seed != seeds[-1]:
						print('  stopping: seed {} {}'.format(seed, 'failed'
							if failure is not None else 'broke a gate'),
							flush=True)
					return

			return

		print('  seeds {} on {} GPU(s): {}'.format(
			', '.join(str(seed) for seed in seeds), len(devices),
			', '.join(str(device) for device in devices)), flush=True)

		with concurrent.futures.ThreadPoolExecutor(
			max_workers=len(devices)) as pool:

			futures = {pool.submit(one, seed): seed for seed in seeds}

			try:
				for future in concurrent.futures.as_completed(futures):
					record, failure, line = future.result()
					print(line, flush=True)

					if record is not None:
						records.append(record)
					elif failure is not None:
						failures.append(failure)
			except BaseException:
				# Ctrl-C lands here, in the main thread. The runs are in
				# sessions of their own and never saw it, and the pool's
				# exit waits for them: stop them first.
				for future in futures:
					future.cancel()
				autoopt.kill_live()
				raise

		# Completion order is arbitrary when runs are parallel; the record
		# is kept in seed order so the median and the per-seed table do not
		# depend on which GPU finished first.
		records.sort(key=lambda record: record['seed'])
		failures.sort(key=lambda failure: failure['seed'])

	# The cold first call, before any timed run: this iteration's code on
	# empty compiler caches, beside the parent's recorded value, which is
	# never re-measured. A crash stops the iteration here.
	cold = None

	if (config.get('checks') or {}).get('first_call') and not meta.get(
			'control'):
		cold = autoopt.measure_cold(root, config, meta['id'], meta['commit'],
			meta.get('parent'), fresh=args.rerun)
		print('  ' + autoopt.check_line({'cold': cold}), flush=True)

	cold_ok = cold is None or cold['verdict'] != 'error'

	# One run, then the unit tests and the compare, then the other runs: a
	# broken edit costs one run, not the whole count.
	first = max(screen, already)
	if cold_ok:
		batch(0, 1)

	def gates_hold():
		return all(passes(record) for record in records)

	# Each stage runs only when the ones before it passed: a verdict of
	# `error` or `gate-failed` is settled by the first that fails, and the
	# rest would spend minutes measuring nothing.
	def screened():
		return bool(records) and not failures and gates_hold()

	tests = None
	checked = None

	parent_commit = (autoopt.load_result(root, meta.get('parent')) or {}).get(
		'commit') if meta.get('parent') is not None else None

	# The unit tests and the compare are gates on the code, so each runs
	# once, after the first run, and the compare only once the tests pass.
	if config.get('tests') and screened():
		tests = run_tests(config, iteration, checkout, meta['commit'],
			args.rerun, parent_commit, full=args.full_tests)

	if ((config.get('checks') or {}).get('compare') and screened()
		and (tests is None or tests['pass'])):
		checked = autoopt.check_compare(root, config, meta['id'],
			meta['commit'], fresh=args.rerun)
		print('  ' + autoopt.check_line(checked), flush=True)

	compared = checked is None or checked['compare']['verdict'] == 'pass'

	if cold_ok and screened() and (tests is None or tests['pass']) and compared:
		batch(1, first)

	# A staged search: decide whether confirming is worth its runs.
	if not cold_ok:
		reason = 'not run: the cold first call crashed'
	elif staged and confirm > screen and first == screen:
		if tests is not None and not tests['pass']:
			reason = 'not confirmed: the unit tests failed'
		elif not compared:
			reason = 'not confirmed: the compare {}'.format(
				'found outputs that differ' if checked['compare']['verdict']
				== 'differ' else 'crashed')
		elif failures:
			reason = 'not confirmed: {} run(s) failed'.format(len(failures))
		elif not records:
			reason = 'not confirmed: no run produced a number'
		else:
			median = statistics.median([r['objective'] for r in records])
			band = max(baseline_spread, spread(
				[r['objective'] for r in records]))
			if not gates_hold():
				reason = 'not confirmed: a gate is broken'
			else:
				promoted, why = decide(config, median, frontier_value, band)
				reason = ('confirming: ' if promoted else
					'not confirmed: ') + why

				if promoted:
					stage = 'confirmed'

		print('  {}'.format(reason))

		if promoted:
			batch(screen, confirm)

	band = max(baseline_spread, spread([r['objective'] for r in records]))

	if not staged or confirm <= screen:
		stage = 'confirmed' if len(records) >= confirm else 'screen'
		reason = 'noise band {}'.format(fmt(band)) if not config['staged'] \
			else reason

	# An iteration whose median passes the keep rule is a candidate for the
	# root, and a subset of the suite is not enough to promote on: run the
	# rest now, while the lease and the checkout are both here.
	def keep_candidate():
		"""Whether the median passes the keep rule against the parent: only
		such an iteration can be promoted, so only it pays for the
		whole test suite."""
		if meta.get('control') or meta.get('parent') is None or not records:
			return False
		probe = {'status': 'ok', 'stage': stage, 'noise_band': band,
			'objective': {'median': statistics.median(
				[r['objective'] for r in records])}}
		return autoopt.keep_verdict(config, probe, autoopt.load_result(root,
			meta['parent']))[0]

	if (tests is not None and tests['pass'] and tests.get('scope') == 'subset'
		and stage == 'confirmed' and compared and screened()
		and (args.full_tests or keep_candidate())):
		tests = run_tests(config, iteration, checkout, meta['commit'],
			args.rerun, parent_commit, full=True)

	result = aggregate(root, config, meta, records, failures, stage,
		frontier_value, band, tests)
	if config.get('workers'):
		result['workers'] = workers_seen(iteration, records, config['workers'])

	result['frontier_holder'] = (autoopt.label(root, frontier_id)
		if frontier_id is not None else None)
	result['frontier_on_path'] = frontier_id is None or frontier_id in \
		autoopt.known_lineage(root, meta['id'])
	if busy_at_start:
		result['busy_at_start'] = busy_at_start

	# The cold first call joins the compare's record. Without a compare (none
	# configured, or the runs failed first) it is recorded alone, and a
	# crash, which ran nothing, names it the failed check.
	if checked is not None and cold is not None:
		checked = dict(checked, cold=cold)
	elif cold is not None:
		result['check'] = {'cold': cold}
		if not cold_ok:
			result['failed_check'] = 'cold'

	if checked is not None:
		autoopt.apply_check(result, checked, config['runs_per_iteration'])

	# The keep rule against the parent, recorded on every settled iteration
	# so promotion and the status tables read it.
	if (meta.get('parent') is not None and result['status'] == 'ok'
		and not meta.get('control') and not meta.get('split')):
		passes, why = autoopt.keep_verdict(config, result,
			autoopt.load_result(root, meta['parent']))
		result['keep'] = {'verdict': 'pass' if passes else 'fail', 'why': why}

	autoopt.write_json(os.path.join(iteration, 'result.json'), result)
	keep_lines = promote_on_keep(root, config, meta, result,
		args.no_promote)

	text = report(result, config, reason, autoopt.load_result(root,
		meta['parent']) if meta.get('parent') is not None else None,
		autoopt.label(root, meta['parent'])
		if meta.get('parent') is not None else None)
	if result.get('busy_at_start'):
		text += '\nbusy at start: ' + autoopt.busy_words(
			result['busy_at_start'][:8])
	if result.get('check'):
		text += '\n' + autoopt.check_line(result['check'])
	if keep_lines:
		text += '\n' + '\n'.join(keep_lines)
	print(text)

	with open(os.path.join(iteration, 'report.txt'), 'w') as out:
		out.write(text + '\n')

	# A README assembled from the subagent's notes.md quotes this result's
	# numbers; re-assemble it so a re-run never leaves it quoting old ones.
	if os.path.exists(os.path.join(iteration, 'notes.md')):
		problem = autoopt.write_readme(root, config, meta['id'])
		if problem:
			print('note: README.md was not re-assembled: ' + problem)

	leads = result['status'] == 'ok' and autoopt.better(
		result['objective']['median'], frontier_value, direction)

	if meta.get('control'):
		# Unchanged code: the difference is the machine's, never a frontier.
		median = result['objective']['median']
		if median is not None and frontier_value:
			print('\ncontrol: unchanged code reads {} against the frontier\'s '
				'{} ({:+.1%}), noise band {}; a control never holds the '
				'frontier'.format(fmt(median), fmt(frontier_value),
					median / frontier_value - 1, fmt(band)))
	elif leads and held:
		print('\nfrontier unchanged: {} still holds the frontier at {}'.format(
			autoopt.label(root, meta['id']),
			fmt(result['objective']['median'])))
	elif leads and (result.get('keep') or {}).get('verdict') == 'fail':
		print('\nbest median so far ({} against {}), but within the noise '
			'band of its parent: not kept'.format(
				fmt(result['objective']['median']), fmt(frontier_value)))
	elif leads:
		print('\nNEW FRONTIER: {} beats {}'.format(
			fmt(result['objective']['median']), fmt(frontier_value)))
	elif held:
		print('\nfrontier moved: {} no longer holds it; {} = {} from '
			'iteration {}'.format(autoopt.label(root, meta['id']),
				config['objective']['name'], fmt(frontier_value),
				autoopt.label(root, frontier_id)))

	if meta['id'] == 0 and result['status'] == 'ok':
		print('\n' + '\n'.join(autoopt.price_lines(root, config)))

	return 0 if result['status'] != 'error' else 2


if __name__ == '__main__':
	sys.exit(main())
