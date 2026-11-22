"""Read an `autooptimize/` search tree into the payloads the page draws.

	from scan import RunScanner
	snapshot = RunScanner('<work>/autooptimize').snapshot()

Nothing here writes to the search directory and nothing here talks to a
browser: `monitor_writer.py` publishes these payloads as JSON, but the reading
is all in this file so it can be called from a notebook cell on its own.

	snapshot()            -> state.json, every scan
	hardware.resources()  -> resources.json, every hardware tick

The external validation, when there is one, comes from `config.json` and
`validations/round-<N>.json` into the snapshot's `validation`.

The source of truth is `iterations/<id>/result.json`, written by
`run_iteration.py`. An iteration with a `meta.json` and no `result.json` is
still running, and is reported as such rather than skipped -- a search where
half the round has not landed yet is the normal state of things.
"""

import os
import re
import sys
import json
import time
import random
import statistics
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import autoopt
import stages
from gpu_scan import Hardware, stdout_of


# How much of an iteration's README rides along in the payload, for the
# tooltip. The whole file can be pages long; the top of it is the idea.
#
# A long search pays this per iteration on every poll: 2,600 iterations at
# 600 characters is 1.5 MB of README in a payload that crosses an ssh tunnel
# every few seconds, and it is the single largest block. Past `MANY` the
# budget drops to NOTES_CHARS_LARGE, the first line or two, which is all a
# tooltip shows anyway.
NOTES_CHARS = 600
NOTES_CHARS_LARGE = 160
# Seconds of silence after which a README-less iteration is not 'working'.
WORKING_IDLE = 3600

# Above this many iterations the README excerpt shrinks to NOTES_CHARS_LARGE.
# The lineage diagram condenses on its own past the same count, in the page
# (`LINEAGE_MANY` in dashboard.js).
MANY = 120

# An iteration set aside without a measurement (references/resume.md).
ABANDONED = re.compile(r'^(\d+)\.abandoned$')

# The objective's confidence interval, drawn as whiskers by the page's
# "Confidence intervals" toggle: a percentile bootstrap of the median over an
# iteration's runs, with a fixed seed so the whiskers do not move between
# polls. A settled iteration's runs never change, so each set of values is
# resampled once per writer.
CI_LEVEL = 0.95
CI_RESAMPLES = 2000
_CI_CACHE = {}

# An iteration's code change against its parent, from their merge base:
# lines added and deleted, and files added, deleted and modified (a rename or
# a mode change is a modification). Commits never change, so each pair is
# asked of git once per writer. A first scan of a long search would otherwise
# spend tens of seconds in git, so each scan spends at most DIFF_BUDGET
# seconds on pairs it has not seen, newest iteration first, and the rest fill
# in over the next polls.
DIFF_BUDGET = 2.0
_DIFF_CACHE = {}


def median_ci(values, level=CI_LEVEL, resamples=CI_RESAMPLES, seed=0):
	"""[low, high] of the bootstrap distribution of the median of `values`,
	or None for fewer than two finite values. The percentiles interpolate
	linearly between order statistics, as numpy's default does. With few runs
	the resampled medians can take only a few values: at three runs the
	interval is at most min to max."""

	values = tuple(v for v in values if isinstance(v, (int, float))
		and v == v and abs(v) != float('inf'))
	if len(values) < 2:
		return None

	key = (values, level, resamples, seed)
	if key not in _CI_CACHE:
		rng = random.Random(seed)
		medians = sorted(statistics.median(rng.choices(values, k=len(values)))
			for _ in range(resamples))

		def percentile(q):
			position = q * (resamples - 1)
			low = int(position)
			high = min(low + 1, resamples - 1)
			return medians[low] + (medians[high] - medians[low]) * (position - low)

		tail = (1 - level) / 2
		_CI_CACHE[key] = [percentile(tail), percentile(1 - tail)]

	return list(_CI_CACHE[key])


def classify(cmdline):
	"""What a process of this search is doing, for the GPU monitor.

	`run_iteration.py` owns the benchmark runs it launches, so they fold into
	its row; a run started by hand shows as a `run` of its own.
	"""

	if 'run_iteration.py' in cmdline:
		return 'iteration'
	if ('dashboard.ipynb' in cmdline or 'monitor.ipynb' in cmdline
			or 'autoopt_monitor' in cmdline):
		return 'other'
	if re.search(r'iterations/\d+', cmdline):
		return 'run'

	return 'other'


def target(cmdline, pid=None):
	"""The iteration a process is working on: `iterations/<id>` in its
	command line, else in the path its stdout goes to. `run_iteration.py`
	starts each benchmark run with a relative command line from inside the
	iteration and its stdout in `runs/seed-<n>/log.txt`, so only the second
	names the iteration."""

	match = re.search(r'iterations/(\d+)', cmdline) \
		or re.search(r'iterations/(\d+)/', stdout_of(pid) or '')

	return '#' + match.group(1) if match else None


def time_breakdown(directory, meta, result, gpus=1):
	"""Where an iteration's wall clock went: the steps the scripts time, and
	the rest, which is the subagent's own work (reading, editing, probes,
	its own checks). Only the fixed steps are measured; everything else is
	inferred as the remainder, since each subagent structures its work
	differently.

	The end is the last of the files a finished iteration writes, README.md
	last by protocol, so a subagent that kept measuring after the runner
	finished is counted to when it stopped.
	"""

	def stamp(text):
		try:
			return time.mktime(time.strptime(text, '%Y-%m-%dT%H:%M:%S'))
		except (TypeError, ValueError):
			return None

	start = stamp(meta.get('created'))

	if start is None or not result:
		return None

	ends = [stamp(result.get('finished'))]

	for name in ('README.md', 'result.json', 'report.txt',
		os.path.join('unit-tests', 'result.json'),
		os.path.join('validation', 'record.json')):
		try:
			ends.append(os.path.getmtime(os.path.join(directory, name)))
		except OSError:
			pass

	ends = [end for end in ends if end is not None]
	total = max(ends) - start if ends else None

	if total is None or total < 0:
		return None

	# With several GPUs an iteration's seeds run side by side, so their sum
	# overstates the wall clock they took; divide by the cards they shared.
	runs = sum(run.get('seconds') or 0.0 for run in result.get('runs') or [])
	runs_wall = runs / max(1, min(gpus, len(result.get('runs') or []) or 1))

	tests = result.get('tests') if isinstance(result.get('tests'), dict) \
		else None
	test_seconds = (tests.get('seconds_total', tests.get('seconds')) or 0.0) \
		if tests else 0.0

	validation = 0.0
	record = os.path.join(directory, 'validation', 'record.json')

	if os.path.exists(record):
		try:
			with open(record) as infile:
				validation = float(json.load(infile).get('seconds') or 0.0)
		except (OSError, ValueError, TypeError):
			validation = 0.0

	fixed = runs_wall + test_seconds + validation

	return {'total': total, 'runs': runs_wall,
		'n_runs': len(result.get('runs') or []),
		'tests': test_seconds if tests else None,
		'tests_scope': (tests or {}).get('scope', 'full') if tests else None,
		'validation': validation if os.path.exists(record) else None,
		'subagent': max(0.0, total - fixed)}


def read_json(path):
	"""A JSON file, or None when it is missing or malformed."""

	try:
		with open(path) as handle:
			return json.load(handle)
	except (OSError, ValueError):
		return None


def read_text(path, limit=None):
	return read_excerpt(path, limit)[0]


def read_excerpt(path, limit=None):
	"""`(text, cut)`: the file's first `limit` characters, and whether it
	held more, in which case the text ends with an ellipsis."""

	try:
		with open(path) as infile:
			text = infile.read(limit + 1 if limit else None)
	except OSError:
		return '', False

	if limit and len(text) > limit:
		return text[:limit].rstrip() + '…', True

	return text, False


def diffstat(repo, base, commit):
	"""`[lines added, lines deleted, files added, files deleted, files
	modified]` from the merge base of `base` and `commit` to `commit`, or None
	when git cannot say. A binary file counts as a file and adds no lines."""

	try:
		done = subprocess.run(['git', '-C', repo, 'diff', '--raw', '--numstat',
			'-M', '--no-color', '{}...{}'.format(base, commit)],
			capture_output=True, text=True, timeout=30)
	except (OSError, subprocess.SubprocessError):
		return None

	if done.returncode != 0:
		return None

	added = deleted = 0
	files = {'A': 0, 'D': 0, 'M': 0}

	for line in done.stdout.splitlines():
		if line.startswith(':'):
			status = line.split('\t', 1)[0].split()[-1][:1]
			files[status if status in files else 'M'] += 1
			continue
		parts = line.split('\t')
		if len(parts) >= 3 and parts[0].isdigit() and parts[1].isdigit():
			added += int(parts[0])
			deleted += int(parts[1])

	return [added, deleted, files['A'], files['D'], files['M']]


PATCH_LINE_CHARS = 1000
PATCH_FILE_ROWS = 2000
PATCH_BYTES = 400000
_HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@ ?(.*)$')


def parse_patch(text):
	"""The files of a `git diff` as dicts for the page's diff panel.

	Each file has `path`, `old` (the path before a rename, else None),
	`status` (A, D, M or R), `binary`, `additions`, `deletions`, `truncated`
	and `hunks`: `{old, new, head, rows}` where `old` and `new` are the first
	line numbers and a row is `[tag, text]`, tag one of ' ', '+', '-'. A line
	is cut at PATCH_LINE_CHARS. A file past PATCH_FILE_ROWS rows, or after
	the files before it have used PATCH_BYTES, keeps its counts and loses its
	hunks (`truncated`)."""

	files, current, hunk = [], None, None
	spent = 0

	for line in text.split('\n'):
		if line.startswith('diff --git '):
			head = line[len('diff --git '):]
			half = head.find(' b/')
			current = {'path': head[half + 3:] if half >= 0 else head,
				'old': None, 'status': 'M', 'binary': False, 'additions': 0,
				'deletions': 0, 'truncated': False, 'hunks': [], 'rows': 0,
				'bytes': 0}
			files.append(current)
			hunk = None
		elif current is None:
			continue
		elif hunk is None and line.startswith('new file mode'):
			current['status'] = 'A'
		elif hunk is None and line.startswith('deleted file mode'):
			current['status'] = 'D'
		elif hunk is None and line.startswith('rename from '):
			current['old'], current['status'] = line[len('rename from '):], 'R'
		elif hunk is None and line.startswith('rename to '):
			current['path'] = line[len('rename to '):]
		elif hunk is None and line.startswith('Binary files '):
			current['binary'] = True
		elif hunk is None and line.startswith('+++ b/'):
			current['path'] = line[len('+++ b/'):]
		elif hunk is None and line.startswith('--- a/') and current['status'] == 'D':
			current['path'] = line[len('--- a/'):]
		else:
			match = _HUNK.match(line)
			if match:
				hunk = {'old': int(match.group(1)), 'new': int(match.group(2)),
					'head': match.group(3), 'rows': []}
				current['hunks'].append(hunk)
			elif hunk is not None and line[:1] in (' ', '+', '-'):
				if line[0] == '+':
					current['additions'] += 1
				elif line[0] == '-':
					current['deletions'] += 1
				body = line[1:]
				if len(body) > PATCH_LINE_CHARS:
					body = body[:PATCH_LINE_CHARS] + '…'
				hunk['rows'].append([line[0], body])
				current['rows'] += 1
				current['bytes'] += len(body) + 8

	for item in files:
		spent += item['bytes']
		if item['rows'] > PATCH_FILE_ROWS or spent > PATCH_BYTES:
			item['hunks'], item['truncated'] = [], True
		del item['rows'], item['bytes']

	return files


def patch(repo, base, commit):
	"""`parse_patch` of the diff from the merge base of `base` and `commit` to
	`commit` (the same range as `diffstat`), or None when git cannot say."""

	try:
		done = subprocess.run(['git', '-C', repo, '-c', 'core.quotepath=off',
			'diff', '-M', '--no-color', '--no-ext-diff', '-U3',
			'--src-prefix=a/', '--dst-prefix=b/',
			'{}...{}'.format(base, commit)], capture_output=True,
			timeout=30)
	except (OSError, subprocess.SubprocessError):
		return None

	if done.returncode != 0:
		return None

	return parse_patch(done.stdout.decode('utf-8', 'replace'))


class RunScanner:
	def __init__(self, root):
		self.root = autoopt.resolve(root)
		self.started_at = time.time()
		self.hardware = Hardware(self.root, classify=classify, target=target)

	###
	# The payloads
	###

	def snapshot(self):
		config = autoopt.load_config(self.root)
		state = autoopt.load_state(self.root)

		iterations = self.iterations(config)
		settled = [row for row in iterations if row['status'] != 'running']

		direction = config['objective']['direction']

		# On-frontier is decided over the settled passing iterations, seeded
		# by nothing: the baseline is one of them.
		path = autoopt.frontier_path([row for row in settled
			if row['status'] == 'ok'], direction)
		on_frontier = {row['id'] for row in path}

		for row in iterations:
			row['on_frontier'] = row['id'] in on_frontier

		return {
			'root': self.root,
			'generated': time.time(),
			'uptime': time.time() - self.started_at,
			# The search kind's categories, plus any an iteration used: a
			# chip and a colour for each, empty ones included.
			'categories': [autoopt.ROOT_CATEGORY] + list(autoopt.categories_for(
				self.root)) + sorted({row['category'] for row in iterations}
				- set(autoopt.categories_for(self.root))
				- {autoopt.ROOT_CATEGORY}),
			'mining': state.get('mining') or {},
			'task': config.get('task', ''),
			'objective': config['objective'],
			'gates': config['gates'],
			'observers': config['observers'],
			'plan': {'runs_per_iteration': config['runs_per_iteration'],
				'confirm_runs': config['confirm_runs'],
				'staged': config['staged']},
			'state': state,
			'iterations': iterations,
			'counts': self.counts(iterations, state),
			'frontier': self.frontier_block(path, settled, config),
			'stats': self.stats(iterations, settled, config, path),
			'validation': self.validation(config),
			'unreadable': [{'id': row['id'], 'error': row['error']}
				for row in iterations if row['status'] == 'unreadable'],
			'abandoned': self.abandoned(),
			'after': self.after(),
		}

	###
	# What the search wrote about its own ending
	###

	def after(self):
		"""The search's own account of where it stands: `ended` (ended.json
		and final.json; None while the search runs) and the five `stages`.
		A part that raises is None and named in `errors`."""

		work = os.path.dirname(self.root)
		out = {}

		for key, read in (('ended', lambda: self.ended()),
				('stages', lambda: self.stages(work))):
			try:
				out[key] = read()
			except Exception as error:
				out[key] = None
				out.setdefault('errors', {})[key] = '{}: {}'.format(
					type(error).__name__, error)[:200]

		return out

	def ended(self):
		ended = autoopt.load_ended(self.root)

		if not ended:
			return None

		medians = ended.get('medians') or {}
		winner = ended.get('winner') or {}
		paired = read_json(os.path.join(self.root, 'final.json')) or {}

		return {'at': ended.get('ended_at'), 'winner': winner.get('label'),
			'winner_median': medians.get('winner'), 'zero_median': medians.get(
			'A0'), 'unit': medians.get('unit'), 'reason': (ended.get('reason')
			or {}).get('text'), 'paired': paired.get('summary') if isinstance(
			paired, dict) else None}

	def stages(self, work):
		"""`[[stage, status, detail]]` for the search's five stages."""

		return [[name, status, detail] for name, status, detail, _ in
			stages.collect(work)]

	def validation(self, config):
		"""The external validation: its configuration and every round's
		record from `validations/round-<N>.json`, oldest round first. None
		when the search has neither, so the page draws nothing.

		Reported, never gated on: nothing here feeds `on_frontier` or a
		status.
		"""

		block = config.get('external_validation')
		directory = os.path.join(self.root, 'validations')
		records = []

		for name in (os.listdir(directory) if os.path.isdir(directory) else []):
			match = re.fullmatch(r'round-(\d+)\.json', name)

			if not match:
				continue

			number = match.group(1)
			stem = name[:-len('.json')]

			try:
				with open(os.path.join(directory, name)) as infile:
					record = json.load(infile)
			except (OSError, ValueError):
				continue

			# The script's own validation.json may name units; the record
			# autoopt.py writes does not carry them.
			try:
				with open(os.path.join(directory, stem,
					'validation.json')) as infile:
					emitted = json.load(infile)
			except (OSError, ValueError):
				emitted = {}

			records.append({
				'units': emitted.get('units') if isinstance(emitted, dict) else None,
				'round': record.get('round', int(number)),
				'iteration': record.get('iteration'),
				'measures': record.get('measures') or {},
				'ok': record.get('ok'),
				'note': record.get('note') or '',
				'seconds': record.get('seconds'),
				'exit_code': record.get('exit_code'),
				'created': record.get('created'),
			})

		if block is None and not records:
			return None

		block = block or {}
		# Oldest first, so the sidebar's last record is the latest run.
		records.sort(key=lambda record: (record['created'] or 0,
			record['round'] or 0))

		# The configured measures in their order, then any a record carries
		# that the configuration does not name.
		measures = list(block.get('measures') or [])
		for record in records:
			measures += [name for name in sorted(record['measures'])
				if name not in measures]

		# Units: config.json's `units`, then any a round's validation.json
		# names, the newest round last. Strings only; anything else is dropped.
		units = {}
		for source in [block.get('units')] + [r.pop('units') for r in records]:
			if isinstance(source, dict):
				units.update({name: unit for name, unit in source.items()
					if isinstance(unit, str) and unit})

		return {
			'configured': bool(block),
			'measures': measures,
			'units': units,
			'note': block.get('note') or '',
			'approved': bool(block.get('approved')),
			'approved_on': block.get('approved_on'),
			'records': records,
		}

	###
	# One iteration
	###

	def iterations(self, config):
		out = []
		ids = autoopt.iteration_ids(self.root)
		budget = NOTES_CHARS if len(ids) <= MANY else NOTES_CHARS_LARGE

		for identifier in ids:
			# One malformed result.json costs its own row, not the scan: a
			# failed snapshot freezes state.json for every iteration.
			try:
				row = self.iteration(identifier, config, budget)
			except Exception as error:
				row = self.unreadable(identifier, error)

			if row is not None:
				out.append(row)

		self.diffstats(out)
		return out

	def diffstats(self, rows):
		"""Give every settled iteration with a parent its `diffstat`."""

		repo = os.path.join(self.root, 'repo.git')
		commits = {row['id']: row.get('commit') for row in rows}
		spent = 0.0

		for row in sorted(rows, key=lambda row: -row['id']):
			base, head = commits.get(row.get('parent')), row.get('commit')
			if row['status'] in ('running', 'unreadable') or not base or not head:
				continue
			key = (repo, base, head)
			if key not in _DIFF_CACHE:
				if spent >= DIFF_BUDGET:
					continue
				started = time.time()
				_DIFF_CACHE[key] = diffstat(repo, base, head)
				spent += time.time() - started
			if _DIFF_CACHE[key] is not None:
				row['diffstat'] = _DIFF_CACHE[key]

	def abandoned(self, notes_chars=NOTES_CHARS):
		"""Iterations set aside without a measurement: `iterations/<id>.abandoned`,
		an empty fork moved out of the way (references/resume.md). They
		are kept out of `iterations`, so no count, frontier, tree or table sees
		them; the page marks each on the objective chart at the baseline's value,
		so a reader can see that the idea was tried and closed."""

		directory = os.path.join(self.root, 'iterations')
		out = []

		for name in (os.listdir(directory) if os.path.isdir(directory) else []):
			match = ABANDONED.match(name)
			if not match:
				continue

			path = os.path.join(directory, name)
			try:
				with open(os.path.join(path, 'meta.json')) as handle:
					meta = json.load(handle)
			except (OSError, ValueError):
				meta = {}
			meta = meta if isinstance(meta, dict) else {}
			identifier = int(match.group(1))
			notes = read_text(os.path.join(path, 'README.md'), notes_chars)

			out.append({'id': identifier,
				'parent': meta.get('parent'),
				'round': meta.get('round'),
				'title': meta.get('title') or '',
				'category': autoopt.category_of(dict(meta, id=identifier)) or 'MISC',
				'topic': meta.get('topic') or '',
				'created': meta.get('created'),
				'notes': notes,
				'has_readme': bool(notes),
				'status': 'abandoned'})

		return sorted(out, key=lambda row: row['id'])

	def unreadable(self, identifier, error):
		"""The row for an iteration whose files could not be read: status
		'unreadable', counted as settled, never on the frontier, with the
		error in `error`."""

		meta = autoopt.load_meta(self.root, identifier)
		meta = meta if isinstance(meta, dict) else {}
		parent = meta.get('parent')

		return {'id': identifier,
			'parent': parent if isinstance(parent, int) else None,
			'round': None, 'title': '',
			'category': 'MISC', 'topic': '', 'branch': None, 'commit': None,
			'created': None, 'notes': '', 'has_readme': False, 'status': 'unreadable', 'stage': None,
			'n_runs': 0, 'value': None, 'objective': None, 'gates': {},
			'observers': {}, 'seconds': None,
			'error': '{}: {}'.format(type(error).__name__, error)[:300]}

	def readmes(self, seen=None):
		"""The whole README.md of every iteration whose file changed since `seen`.

		Returns {id: (mtime, text)}. The payload carries only an excerpt
		(NOTES_CHARS), so the page fetches `readmes/<id>.json` when a tree node
		is opened; `seen` ({id: mtime}) lets the writer rewrite only the files
		that moved.
		"""

		seen = seen or {}
		changed = {}
		for identifier in autoopt.iteration_ids(self.root):
			path = os.path.join(autoopt.iteration_dir(self.root, identifier), 'README.md')
			try:
				mtime = os.path.getmtime(path)
			except OSError:
				continue
			if seen.get(identifier) != mtime:
				changed[identifier] = (mtime, read_text(path))
		return changed

	def diffs(self, rows, seen=None, budget=DIFF_BUDGET):
		"""The code change of every settled iteration with a parent, against
		that parent, for the diff panel under the objective chart.

		`rows` are the snapshot's iterations. Returns {id: (payload, pair)} for
		the iterations whose (parent commit, commit) pair is not already in `seen` ({id: (base, head)}); the writer records what it
		wrote there. A pair is asked of git once, newest iteration first, and
		each call spends at most `budget` seconds (None: no limit, for the
		export) on pairs it has not seen; the rest fill in on later calls.
		The payload is `{id, parent, files}`, `files` from `parse_patch`."""

		seen = seen if seen is not None else {}
		repo = os.path.join(self.root, 'repo.git')
		commits = {row['id']: row.get('commit') for row in rows}
		rows = [row for row in rows
			if row['status'] not in ('running', 'unreadable')]
		out, spent = {}, 0.0

		for row in sorted(rows, key=lambda row: -row['id']):
			base, head = commits.get(row.get('parent')), row.get('commit')
			if not base or not head or seen.get(row['id']) == (base, head):
				continue
			if budget is not None and spent >= budget:
				break
			started = time.time()
			files = patch(repo, base, head)
			spent += time.time() - started
			if files is not None:
				out[row['id']] = ({'id': row['id'], 'parent': row['parent'],
					'files': files}, (base, head))

		return out

	def iteration(self, identifier, config, notes_chars=NOTES_CHARS):
		directory = autoopt.iteration_dir(self.root, identifier)
		meta = autoopt.load_meta(self.root, identifier) or {}
		result = autoopt.load_result(self.root, identifier)

		if not meta and not result:
			return None

		notes, cut = read_excerpt(os.path.join(directory, 'README.md'),
			notes_chars)

		row = {
			'id': identifier,
			'parent': (result or meta).get('parent'),
			'round': (result or meta).get('round'),
			'title': (result or meta).get('title') or '',
			'category': autoopt.category_of(dict(result or meta,
				id=identifier)) or 'MISC',
			'topic': (result or meta).get('topic') or '',
			'branch': (result or meta).get('branch'),
			'commit': (result or meta).get('commit'),
			'created': meta.get('created'),
			# The idea miner's last finished stage when it was created (0 before
			# any has finished).
			'mining_stage': meta.get('mining_stage'),
			# `no` or `specific`, the answer to the brief's toolchain question.
			'toolchain': meta.get('toolchain'),
			'notes': notes,
			'has_readme': bool(notes),
		}

		# A same-code control is never on the frontier (autoopt.frontier_path).
		if meta.get('control'):
			row['control'] = True

		# Only when true: the page's SHOW MORE fetches readmes/<id>.json.
		if cut:
			row['notes_cut'] = True

		if result is None:
			# Started but not settled. `elapsed` is what the summary band
			# counts as "in flight" and what the tree draws hollow.
			row.update({'status': 'running', 'stage': None, 'n_runs': 0,
				'value': None, 'objective': None, 'gates': {},
				'observers': {}, 'seconds': None,
				'elapsed': self.elapsed(directory, meta)})

			return row

		objective = result.get('objective') or {}

		row.update({
			'status': result.get('status', '?'),
			'stage': result.get('stage'),
			'n_runs': result.get('n_runs') or 0,
			'n_failed': result.get('n_failed') or 0,
			'value': objective.get('median'),
			'objective': dict({key: objective.get(key) for key in
				('median', 'mean', 'std', 'min', 'max', 'values')},
				ci=median_ci(objective.get('values') or [])),
			'gates': {name: {key: gate.get(key) for key in
					('op', 'threshold', 'worst', 'median', 'pass',
						'n_violations')}
				for name, gate in (result.get('gates') or {}).items()},
			'observers': {name: {'median': observer.get('median'),
					'std': observer.get('std')}
				for name, observer in (result.get('observers') or {}).items()},
			'seconds': result.get('seconds'),
			# Every number a run recorded, for the per-run table. The note is
			# the benchmark's own free text, clipped so one verbose run cannot
			# bloat every snapshot.
			'runs': [{'seed': run.get('seed'),
					'objective': run.get('objective'),
					'seconds': run.get('seconds'),
					'dir': run.get('dir'),
					'gates': run.get('gates') or {},
					'observers': run.get('observers') or {},
					'note': str(run.get('note') or '')[:240]}
				for run in (result.get('runs') or [])],
			'tests': (None if not isinstance(result.get('tests'), dict)
				else bool(result['tests'].get('pass'))),
			'finished': result.get('finished'),
			'best_run': result.get('best_run'),
			'failures': result.get('failures') or [],
			'frontier_at_start': result.get('frontier_at_start'),
			'noise_band': result.get('noise_band'),
			'time': time_breakdown(directory, meta, result,
				config.get('gpus_per_agent', 1) or 1),
		})

		# The runner finishes long before its subagent does: the subagent's
		# own checks and validation come after result.json, and the README
		# is written last. Until it exists the iteration is still being
		# worked on, and `idle` says how long since anything in it changed.
		# A subagent that died leaves no README either, so a silent hour ends
		# the marker; the baseline never has one.
		if not notes and identifier != 0:
			idle = self.idle(directory)
			if idle is not None and idle < WORKING_IDLE:
				row['working'] = True
				row['idle'] = idle

		return row

	def elapsed(self, directory, meta):
		"""Seconds since a running iteration last wrote anything.

		Taken from the newest file under `runs/`, not from `created`: an
		iteration forked an hour ago and started ten minutes ago has been
		running for ten minutes.
		"""

		newest = 0.0
		runs = os.path.join(directory, 'runs')

		for base, _, names in os.walk(runs):
			for name in names:
				try:
					newest = max(newest,
						os.path.getmtime(os.path.join(base, name)))
				except OSError:
					continue

		if not newest:
			try:
				newest = os.path.getmtime(directory)
			except OSError:
				return None

		return time.time() - newest

	def idle(self, directory):
		"""Seconds since anything a subagent writes in an iteration changed:
		probes, logs, validation, the report. Compiler caches and exported
		source trees are skipped; they are large and not a sign of progress."""

		newest, skip = 0.0, {'cache', '.numba-cache', '__pycache__', 'src', 'v'}

		for base, dirs, names in os.walk(directory):
			dirs[:] = [d for d in dirs if d not in skip]
			for name in names:
				try:
					newest = max(newest,
						os.path.getmtime(os.path.join(base, name)))
				except OSError:
					continue

		return time.time() - newest if newest else None

	###
	# Aggregates
	###

	def counts(self, iterations, state):
		settled = [row for row in iterations if row['status'] != 'running']

		by_category, by_status = {}, {}

		for row in settled:
			by_category[row['category']] = by_category.get(row['category'],
				0) + 1
			by_status[row['status']] = by_status.get(row['status'], 0) + 1

		return {
			'iterations': len(settled),
			'running': len(iterations) - len(settled),
			'working': sum(1 for row in settled if row.get('working')),
			'runs': sum(row['n_runs'] for row in settled),
			'rounds': len(self.rounds()),
			'round': state.get('round'),
			'by_category': by_category,
			'by_status': by_status,
		}

	def rounds(self):
		"""The rounds that have been summarized, newest last."""

		directory = os.path.join(self.root, 'rounds')
		out = []

		for name in sorted(os.listdir(directory)
			if os.path.isdir(directory) else []):

			if name.startswith('round-') and name.endswith('.md'):
				number = name[len('round-'):-len('.md')]
				out.append({'round': int(number) if number.isdigit() else 0,
					'path': os.path.join(directory, name)})

		return sorted(out, key=lambda entry: entry['round'])

	def frontier_block(self, path, settled, config):
		"""The frontier, and what each step of it bought.

		`gain` is against the step before it, so the steps sum to `total`;
		the baseline's own gain is zero by construction.
		"""

		steps, previous = [], None

		for row in path:
			gain = None if previous is None else row['value'] - previous
			relative = (None if previous in (None, 0)
				else gain / abs(previous) * 100.0)

			steps.append({'id': row['id'], 'value': row['value'],
				'title': row['title'], 'category': row['category'],
				'round': row['round'], 'gain': gain, 'percent': relative})

			previous = row['value']

		baseline = next((row for row in settled if row['id'] == 0), None)
		base_value = baseline['value'] if baseline else None
		best = steps[-1] if steps else None

		return {
			'value': best['value'] if best else None,
			'id': best['id'] if best else None,
			'steps': steps,
			'baseline': base_value,
			'total_gain': (None if best is None or base_value is None
				else best['value'] - base_value),
			'total_percent': (None if best is None or not base_value
				else (best['value'] - base_value) / abs(base_value) * 100.0),
		}

	def stats(self, iterations, settled, config, path):
		"""The right-hand column: what the search has cost and bought."""

		ok = [row for row in settled if row['status'] == 'ok']
		gate_failed = [row for row in settled
			if row['status'] == 'gate-failed']
		errored = [row for row in settled if row['status'] == 'error']
		partial = [row for row in settled if row['status'] == 'partial']

		seconds = [row['seconds'] for row in settled if row['seconds']]
		total_seconds = sum(seconds)
		runs = sum(row['n_runs'] for row in settled)

		# Wall clock of the search, from the first iteration's start to now.
		created = [row['created'] for row in iterations if row.get('created')]
		started = min(created) if created else None
		wall = None

		if started:
			try:
				wall = time.time() - time.mktime(time.strptime(started,
					'%Y-%m-%dT%H:%M:%S'))
			except ValueError:
				wall = None

		# What the staged evaluation did not spend: every iteration that
		# stopped at the screening depth skipped the rest of the confirmation
		# runs.
		saved = 0
		if config['staged']:
			extra = config['confirm_runs'] - config['runs_per_iteration']
			saved = extra * sum(1 for row in settled
				if row.get('stage') == 'screen')

		per_category = {}
		for row in settled:
			entry = per_category.setdefault(row['category'],
				{'n': 0, 'ok': 0, 'best': None, 'frontier': 0})
			entry['n'] += 1
			entry['ok'] += row['status'] == 'ok'
			entry['frontier'] += bool(row.get('on_frontier'))

			if row['status'] == 'ok' and autoopt.better(row['value'],
				entry['best'], config['objective']['direction']):
				entry['best'] = row['value']

		return {
			'n_ok': len(ok),
			'n_gate_failed': len(gate_failed),
			'n_error': len(errored),
			'n_partial': len(partial),
			'n_frontier': len(path),
			'runs': runs,
			'runs_saved': saved if config['staged'] else None,
			'compute_hours': total_seconds / 3600.0,
			'wall_hours': (wall / 3600.0) if wall else None,
			'iterations_per_hour': (len(settled) / (wall / 3600.0)
				if wall and wall > 60 else None),
			'runs_per_hour': (runs / (wall / 3600.0)
				if wall and wall > 60 else None),
			'seconds_per_run': (total_seconds / runs) if runs else None,
			'median_iteration_minutes': (statistics.median(seconds) / 60.0
				if seconds else None),
			'gate_pass_rate': (len(ok) / (len(settled) - len(partial))
				if len(settled) > len(partial) else None),
			'per_category': per_category,
			'rounds': self.rounds(),
		}


def main(argv=None):
	"""Print a search's snapshot as JSON: the quickest way to tell a scanner
	problem from a page problem.

		python3 scan.py <root> | head
	"""

	import argparse

	parser = argparse.ArgumentParser(description=main.__doc__)
	parser.add_argument('root', help='the autooptimize/ directory')
	args = parser.parse_args(argv)

	scanner = RunScanner(args.root)
	payload = scanner.snapshot()
	# Flushed here, inside the handler below: a flush left to interpreter
	# exit reports a closed pipe as "Exception ignored" and cannot be caught.
	print(json.dumps(payload, indent='\t', default=lambda value: None),
		flush=True)

	return 0


if __name__ == '__main__':
	try:
		sys.exit(main())
	except BrokenPipeError:
		# `| head` closed the pipe: stop quietly, as a filter should, and keep
		# Python from reporting the flush at exit.
		os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
		sys.exit(0)
	except ValueError as error:
		print('error: {}'.format(error), file=sys.stderr)
		sys.exit(1)
