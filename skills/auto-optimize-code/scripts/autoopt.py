#!/usr/bin/env python3
"""Manage the `autooptimize/` search tree: set it up, fork it, read it back.

	python3 autoopt.py init    <dir> --command 'python3 bench.py --seed {seed} --out {out}' ...
	python3 autoopt.py brief   <dir> --id 3 --title '...' --category CACHING --idea '...' --tried none
	python3 autoopt.py new     <dir> --new-id 3 --parent 0 --title '...' --category CACHING
	python3 autoopt.py finish  <dir> --id 3 [--ledger f] [--checker p --checker-row f]
	python3 autoopt.py status  <dir>
	python3 autoopt.py summary <dir> --round 1
	python3 autoopt.py promote <dir> --id 7
	python3 autoopt.py config  <dir> --set mode=tree --reason '...'
	python3 autoopt.py path    <dir> --id best
	python3 autoopt.py wait    <log> [--pid N]      # at most four minutes
	python3 autoopt.py bg      <dir> <log> '<command>'   # a long command, detached
	python3 autoopt.py price   <dir>                # what the base costs later

`run_iteration.py` is the other half: this script makes an iteration's
directory, that one fills it with numbers.

Every iteration is a git worktree of the bare repo at `<dir>/repo.git`, checked
out at `<dir>/iterations/<id>` on branch `autoopt/<id>`, branched from its
parent's branch. The branch graph is therefore the search tree, and
`git diff autoopt/<parent>..autoopt/<id>` is exactly the idea that iteration
tried.

With `init --repo`, the search copies an existing git repository instead: the
bare repo is a clone of it, each worktree is `<dir>/iterations/<id>/src` with
the records beside it, and the benchmark lives in `<dir>/harness/`.
"""

import os
import sys
import glob
import json
import re
import time
import fcntl
import shlex
import shutil
import signal
import argparse
import threading
import tempfile
import hashlib
import platform
import statistics
import subprocess


# What an idea changes: the idea miner's ten (references/idea-miner.md),
# and MISC, the catch-all.
CATEGORIES = ('MICRO', 'NUMBA', 'CACHING', 'CACHE_MISS', 'PRECISION',
	'PARALLEL', 'REORGANIZATION', 'IO', 'ALGORITHM', 'OPTIMIZATION', 'MISC')

# The baseline's category, and only the baseline's: no idea is filed under
# it, so it is not a choice for `new --category`.
ROOT_CATEGORY = 'ROOT'

# Short codes for the table. The dashboard colours by the same keys.
SHORT = {'MICRO': 'micro', 'NUMBA': 'numba', 'CACHING': 'cache',
	'CACHE_MISS': 'miss', 'PRECISION': 'prec', 'PARALLEL': 'par',
	'REORGANIZATION': 'reorg', 'IO': 'io', 'ALGORITHM': 'algo',
	'OPTIMIZATION': 'opt', 'MISC': 'misc',
	ROOT_CATEGORY: 'root'}


def categories_for(root):
	"""The categories a search files ideas under."""

	return CATEGORIES


def category_of(record):
	"""The category of a meta.json or result.json record. Iteration 0 is
	ROOT whatever it recorded."""

	if str(record.get('id')) == '0':
		return ROOT_CATEGORY
	return record.get('category')

# Gate comparisons. `==` is exact identity unless the gate carries a `tol`, in
# which case it is |value - threshold| <= tol -- which is how a "the outputs
# must not move" gate is spelled.
OPS = {
	'<=': lambda value, threshold, tol: value <= threshold + (tol or 0.0),
	'<': lambda value, threshold, tol: value < threshold + (tol or 0.0),
	'>=': lambda value, threshold, tol: value >= threshold - (tol or 0.0),
	'>': lambda value, threshold, tol: value > threshold - (tol or 0.0),
	'==': lambda value, threshold, tol: abs(value - threshold) <= (tol or 0.0),
}


def cache_dir(iteration, commit):
	"""`{cache}`: one compiler cache per iteration per commit.

	numba's `cache=True` checks only the decorated function's own source
	file, so a cache kept across an edit to a callee serves the old machine
	code. A commit names the code exactly (the runner commits before any
	run), so every run of one commit can share compiled kernels, Triton
	binaries and autotuning results, and a new commit starts empty.
	"""

	path = os.path.join(iteration, 'cache', (commit or 'uncommitted')[:12])
	os.makedirs(path, exist_ok=True)

	return path


# Process groups of the shell commands running now, so an interrupted
# caller can stop them: each is in a session of its own, out of reach of
# the Ctrl-C that reaches the caller.
LIVE = set()


def kill_group(process):
	"""SIGKILL a command's whole process group and reap the shell."""

	try:
		os.killpg(process.pid, signal.SIGKILL)
	except OSError:
		pass

	process.wait()


def kill_live():
	for process in list(LIVE):
		kill_group(process)


# The variables a build reads its compiler and flags from. A run never
# inherits them from a subagent's shell: the search's config.json holds the
# values A0 and every iteration build with (`build_env`,
# captured by `init`), so a speedup cannot come from an exported `CFLAGS`.
BUILD_VARS = ('CC', 'CXX', 'FC', 'CPP', 'AR', 'LD', 'CFLAGS', 'CXXFLAGS',
	'CPPFLAGS', 'FFLAGS', 'LDFLAGS', 'LDLIBS', 'RUSTFLAGS',
	'CARGO_ENCODED_RUSTFLAGS', 'CARGO_BUILD_RUSTFLAGS', 'NVCCFLAGS',
	'CUDAFLAGS', 'CMAKE_C_FLAGS', 'CMAKE_CXX_FLAGS', 'CMAKE_BUILD_TYPE',
	'CMAKE_C_COMPILER', 'CMAKE_CXX_COMPILER', 'CMAKE_TOOLCHAIN_FILE',
	'NUMBA_OPT', 'NUMBA_CPU_NAME', 'NUMBA_CPU_FEATURES')
BUILD_ENV = {}

# AUTOOPT_TMPDIR, when set, is where temporary files go.
if os.environ.get('AUTOOPT_TMPDIR'):
	try:
		os.makedirs(os.environ['AUTOOPT_TMPDIR'], exist_ok=True)
		tempfile.tempdir = os.environ['AUTOOPT_TMPDIR']
	except OSError:
		pass

# What the user decides once, at step 0, about what follows the search
# (`references/step0-interview.md`, call 5). `ask` means the process halts
# at that point and asks; `auto`, `keep` and `prune` are answers given in
# advance. These are this script's own answers. A script that imports this
# one adds the answers
# it reads with `register_after`; a key no table names is kept as it is (it
# belongs to whatever extension wrote it).
AFTER_DEFAULTS = {'stop': 'ask', 'budget_hours': None, 'budget_stages': None,
	'disk': 'ask', 'oldest_toolchain_command': None, 'tests_driver_edit': 'ask',
	'commit_trailer': None, 'pair_cap_hours': 4, 'setup_questions': 'ask',
	'build_outputs': []}
AFTER_CHOICES = {'stop': ('ask', 'auto'), 'disk': ('ask', 'keep', 'prune'),
	'tests_driver_edit': ('ask', 'yes', 'no'), 'setup_questions': ('ask',
	'recommended')}
# Keys that hold a whole number of 1 or more, and a number of 0 or more.
AFTER_WHOLE = ['budget_stages']
AFTER_NONNEG = []
# Functions `check(after)` that fail on a bad value of their own key.
AFTER_CHECKS = []
def path_list(text):
	"""`after.build_outputs` from `config --set`: a JSON list (`["a.log",
	"*.tmp"]`) or comma-separated paths; `[]`, an empty value or `null` is
	none."""

	text = text.strip()

	if text.startswith('['):
		value = json.loads(text)

		if not isinstance(value, list):
			raise ValueError(text)

		return value

	if text.lower() in ('', 'null', 'none'):
		return []

	return [part.strip() for part in text.split(',') if part.strip()]


# The keys `config --set after.X=V` changes, and how each value is read.
AFTER_KINDS = {'stop': str, 'disk': str, 'budget_hours': float,
	'budget_stages': int, 'oldest_toolchain_command': str,
	'tests_driver_edit': str,
	'commit_trailer': str, 'pair_cap_hours': float, 'setup_questions': str,
	'build_outputs': path_list}


def register_after(defaults, kinds, choices=None, whole=(), nonneg=(),
		checks=()):
	"""Add another script's `after` answers to the tables, so `config
	--set after.X` takes them and `check_after` checks them."""

	AFTER_DEFAULTS.update(defaults)
	AFTER_KINDS.update(kinds)
	AFTER_CHOICES.update(choices or {})
	AFTER_WHOLE.extend(key for key in whole if key not in AFTER_WHOLE)
	AFTER_NONNEG.extend(key for key in nonneg if key not in AFTER_NONNEG)
	AFTER_CHECKS.extend(check for check in checks if check not in
		AFTER_CHECKS)
	SETTABLE.update({'after.' + key: kind for key, kind in kinds.items()})


def after_from(text):
	"""`config.json`'s `after` block: `AFTER_DEFAULTS` over `--after` JSON."""

	try:
		given = json.loads(text) if text else {}
	except ValueError as error:
		fail('--after is not valid JSON ({})'.format(error))

	return check_after(dict(AFTER_DEFAULTS, **given))


def check_after(after):
	for key, choices in AFTER_CHOICES.items():
		if after[key] not in choices:
			fail('config.json "after.{}" is {!r}; one of {}'.format(key,
				after[key], ', '.join(str(c) for c in choices)))

	for key in AFTER_WHOLE:
		value = after[key]

		if value is not None and (isinstance(value, bool) or not isinstance(
				value, int) or value < 1):
			fail('config.json "after.{}" is {!r}; a whole number, 1 or more, '
				'or null'.format(key, value))

	for key in AFTER_NONNEG:
		value = after[key]

		if value is not None and (isinstance(value, bool) or not isinstance(
				value, (int, float)) or value < 0):
			fail('config.json "after.{}" is {!r}; a number, 0 or more'.format(
				key, value))

	value = after['build_outputs'] if after['build_outputs'] is not None else []

	if not isinstance(value, list) or not all(isinstance(one, str) and
			one.strip() and one[0] not in '/:' for one in value):
		fail('config.json "after.build_outputs" is {!r}; a list of paths or '
			'globs relative to the repository\'s root (no leading / or :)'
			.format(value))

	after['build_outputs'] = list(value)

	for check in AFTER_CHECKS:
		check(after)

	return after


def clean_env(env=None):
	"""A copy of `env` (default: this process's) without the build variables,
	with the search's `build_env` set."""

	cleaned = {key: value for key, value in (os.environ if env is None
		else env).items() if key not in BUILD_VARS}
	cleaned.update(BUILD_ENV)

	return cleaned


def busy_threads(pid):
	"""`(threads running, processes)` in the process tree under `pid` now:
	a thread counts when the kernel shows it running or runnable (`R`)."""

	busy, seen, todo = 0, set(), [pid]

	while todo:
		one = todo.pop()

		if one in seen:
			continue

		seen.add(one)

		try:
			tasks = os.listdir('/proc/{}/task'.format(one))
		except OSError:
			continue

		for task in tasks:
			base = '/proc/{}/task/{}/'.format(one, task)

			try:
				with open(base + 'stat') as handle:
					busy += handle.read().rsplit(')', 1)[1].split()[0] == 'R'

				with open(base + 'children') as handle:
					todo += [int(child) for child in handle.read().split()]
			except (OSError, IndexError, ValueError):
				pass

	return busy, len(seen)


def sample_busy(pid, stop, found, every=0.1):
	"""Until `stop` is set, keep the largest `busy_threads` of `pid`'s tree
	in `found` (`busy_peak`, `procs_peak`, `samples`)."""

	while not stop.wait(every):
		busy, procs = busy_threads(pid)
		found['busy_peak'] = max(found.get('busy_peak', 0), busy)
		found['procs_peak'] = max(found.get('procs_peak', 0), procs)
		found['samples'] = found.get('samples', 0) + 1


def run_shell(line, cwd, stdout=None, env=None, timeout=None, sample=None):
	"""Run a shell command; return `(exit code, timed out)`. With a dict as
	`sample`, the most threads its process tree had running at once is
	recorded in it (`sample_busy`).

	The command is a session leader, so a timeout kills everything it
	started. Killing only the pid would kill `sh` and leave the benchmark,
	its DataLoader workers and whatever holds the GPU running.
	"""

	process = subprocess.Popen(line, shell=True, cwd=cwd, stdout=stdout,
		stderr=subprocess.STDOUT if stdout is not None else None,
		env=clean_env(env),
		start_new_session=True)
	LIVE.add(process)
	stop = threading.Event()

	if sample is not None:
		threading.Thread(target=sample_busy, args=(process.pid, stop, sample),
			daemon=True).start()

	try:
		return process.wait(timeout=timeout or None), False
	except subprocess.TimeoutExpired:
		kill_group(process)
		return -9, True
	except BaseException:
		kill_group(process)
		raise
	finally:
		stop.set()
		LIVE.discard(process)


# The placeholders each command takes. A path is substituted shell-quoted,
# so a search under a directory with a space in its name still runs; the
# documented forms (`--out {out}`, `PYTHONPATH={src}`, `{cache}/numba`) all
# stay one word. A placeholder written inside quotes (`'{out}'`) does not.
PLACEHOLDERS = {
	'command': ('seed', 'out', 'out_abs', 'iteration', 'src', 'cache'),
	'tests': ('src', 'iteration', 'out', 'cache', 'selected'),
	'validation': ('iteration', 'src', 'out', 'out_abs', 'cache', 'round',
		'id'),
	# The task's checks (CHECKS, below): one version's tree, and for compare
	# the directory of A0's cached outputs.
	'check_compare': ('src', 'reference'),
	'check_one': ('src', 'side'),
	'check_profile': ('src', 'out'),
}

# The task's own checks, written once at step 2 into autooptimize/checks/
# and registered in config.json "checks" (references/step2-baseline.md,
# *Write the checks*). compare takes one version's tree ({src}) and the
# directory of A0's cached outputs ({reference}), and exits 0 when the
# outputs agree under the gate's rule, 1 when they differ; with
# AUTOOPT_WRITE_REFERENCE=1 it caches {src}'s outputs there instead. time
# and first_call take a tree ({src}) and print seconds last, as paired.py
# and cold.py expect.
CHECKS = {'compare': 'check_compare', 'time': 'check_one',
	'first_call': 'check_one', 'build': 'check_one',
	'profile': 'check_profile'}
REQUIRED_PLACEHOLDERS = {'check_compare': ('src', 'reference'),
	'check_one': ('src',), 'check_profile': ('src',)}

SUBAGENT_MODELS = ('opus', 'sonnet', 'haiku', 'fable')

# Who promotes in tree mode: the runner, on the default keep rule (auto), or
# the orchestrator, for a keep rule the user wrote in their own words.
PROMOTE_MODES = ('auto', 'orchestrator')
# Untried ideas below which the next idea-miner stage is called for.
MIN_UNTRIED = 2


def fill(template, **values):
	"""`template.format(**values)` with every string shell-quoted, and a
	list quoted item by item and joined with spaces (`{selected}`)."""

	quoted = {}

	for key, value in values.items():
		if isinstance(value, (list, tuple)):
			quoted[key] = ' '.join(shlex.quote(str(item)) for item in value)
		elif isinstance(value, str):
			quoted[key] = shlex.quote(value)
		else:
			quoted[key] = value

	return template.format(**quoted)


def check_placeholders(name, template, kind):
	"""Fail now on a brace the substitution cannot fill, rather than on the
	first run, after a commit."""

	try:
		fill(template, **{key: 0 if key in ('seed', 'round', 'id') else 'x'
			for key in PLACEHOLDERS[kind]})
	except KeyError as error:
		fail('{} has {{{}}}, which is not a placeholder; it takes {}. A '
			'shell variable is written $VAR, not ${{VAR}}, and a literal '
			'brace is doubled: {{{{ }}}}. Got {!r}'.format(name,
				error.args[0], ', '.join('{' + key + '}'
					for key in PLACEHOLDERS[kind]), template))
	except (IndexError, ValueError) as error:
		fail('{} has a brace that is not a placeholder ({}); a literal brace '
			'is doubled: {{{{ }}}}. Got {!r}'.format(name, error, template))


def fail(message):
	sys.stderr.write('error: {}\n'.format(message))
	sys.exit(1)


def write_json(path, data, **kwargs):
	"""Replace `path` with `data` as JSON, whole or not at all.

	Written beside it and renamed over it, so a crash or a full disk mid-
	write leaves the old file, never half of the new one. The temporary
	name carries the process and thread, since the runner's threads and
	several subagents write at once.
	"""

	kwargs.setdefault('indent', '\t')
	tmp = '{}.{}.{}.tmp'.format(path, os.getpid(), threading.get_ident())

	try:
		with open(tmp, 'w') as out:
			json.dump(data, out, **kwargs)
		os.replace(tmp, path)
	except BaseException:
		try:
			os.unlink(tmp)
		except OSError:
			pass
		raise


def read_json(path):
	"""Load a JSON file the search wrote; a corrupt one stops the command
	with its name rather than a traceback. A missing one raises OSError for
	the caller to decide about."""

	with open(path) as infile:
		try:
			return json.load(infile)
		except ValueError as error:
			fail('{} is not valid JSON ({}). It was cut off or edited by '
				'hand; repair it or move it aside, since nothing here '
				'rewrites it from scratch.'.format(path, error))


def git(*args, **kwargs):
	"""Run git, returning stdout. Raises with git's own stderr on failure."""

	out = subprocess.run(('git',) + args, capture_output=True, text=True,
		**kwargs)

	if out.returncode != 0:
		raise RuntimeError('git {} failed: {}'.format(' '.join(args),
			out.stderr.strip() or out.stdout.strip()))

	return out.stdout


###
# The search directory
###

def resolve(root):
	"""Accept either `<work>/autooptimize` or the directory above it."""

	root = os.path.abspath(os.path.expanduser(root))

	if os.path.exists(os.path.join(root, 'config.json')):
		return root

	nested = os.path.join(root, 'autooptimize')
	if os.path.exists(os.path.join(nested, 'config.json')):
		return nested

	fail('{} holds no config.json; point this at an autooptimize/ directory, '
		'or run `autoopt.py init` first'.format(root))


###
# The search directory outside `autooptimize/`: the layout is defined here and
# nowhere else (references/layout.md, *The search directory*).
###

# Folder names under the search directory (`work`, the one holding
# `autooptimize/`), by key. A path is `work_path(work, key, *more)`.
PATHS = {
	'interview': ('interview',),
	'inputs': ('inputs',),
	'tests': ('tests',),
	'tests-suite': ('tests', 'suite'),
	'setup': ('setup',),
	'proof': ('setup', 'gateproof'),
	'width': ('setup', 'width'),
	'finish': ('finish',),
	'scratch': ('scratch',),
	'dashboard': ('dashboard.ipynb',),
}


def work_path(work, key, *more):
	"""`work`'s folder or file named by `key` of PATHS, then `more`."""

	return os.path.join(work, *(PATHS[key] + more))


def tmp_dir(work):
	"""`autooptimize/tmp/`, made: where a script that needs scratch space
	beside the search puts it, and removes it when done."""

	path = os.path.join(work, 'autooptimize', 'tmp')
	os.makedirs(path, exist_ok=True)

	return path


# This skill's name, recorded in config.json: another skill that writes the
# same layout (auto-optimize-ml) has scripts of its own, and these refuse a
# search it made rather than misread it (check_config).
SKILL_NAME = 'auto-optimize-code'

# The version of the record files' layout: config.json, state.json,
# result.json and ended.json carry it as `schema_version`. It is raised when a
# field changes meaning or is removed, never for a field added. A file without
# it was written before versioning, and reads as version 1.
SCHEMA_VERSION = 1


def skill_version():
	"""The skill's release, from VERSION beside SKILL.md, or None."""

	try:
		with open(os.path.join(os.path.dirname(SCRIPTS), 'VERSION')) as handle:
			return handle.read().strip() or None
	except OSError:
		return None


def skill_commit():
	"""The last commit of the skill's own files in the checkout these
	scripts run from, or None when that is not a git checkout."""

	here = os.path.dirname(SCRIPTS)
	done = subprocess.run(['git', '-C', here, 'log', '-1', '--format=%H', '--',
		'.'], capture_output=True, text=True)

	return done.stdout.strip() or None if done.returncode == 0 else None


def skill_notice(root, mark=True):
	"""One line when the skill changed since this search began, or since the
	change last reported: nothing stops, and the user is told. `mark` records
	the change as reported. A search whose config.json records no commit
	takes the current one as its start."""

	path = os.path.join(root, 'skill-seen.json')
	current = skill_commit() if os.path.isdir(root) else None

	if current is None:
		return None

	try:
		seen = read_json(path)
	except (OSError, ValueError):
		try:
			began = (read_json(os.path.join(root, 'config.json')).get('skill')
				or {}).get('commit')
		except (OSError, ValueError):
			began = None

		seen = {'began': began or current, 'seen': began or current}

		if mark:
			write_json(path, seen)

	if seen['seen'] == current:
		return None

	count = subprocess.run(['git', '-C', os.path.dirname(SCRIPTS), 'rev-list',
		'--count', '{}..{}'.format(seen['seen'], current), '--', '.'],
		capture_output=True, text=True).stdout.strip() or '?'
	line = ('NOTICE the auto-optimize-code skill changed under this search: {} '
		'commit(s) since {} (now {}; the search began on {}). Nothing stops. '
		'Tell the user, with `git -C {} log --oneline {}..{} -- .`; later '
		'steps follow the new scripts and references'.format(count, seen[
		'seen'][:10], current[:10], seen['began'][:10], os.path.dirname(
		SCRIPTS), seen['seen'][:10], current[:10]))

	if mark:
		write_json(path, dict(seen, seen=current))

	return line


def as_repository(directory, name):
	"""Make an exported tree a git repository of one commit: build scripts,
	`pre-commit`, `setuptools-scm` and `git ls-files` steps need one."""

	for words in (['init', '-q'], ['add', '-A', '-f'], ['-c',
			'user.name=auto-optimize', '-c', 'user.email=auto-optimize@localhost',
			'commit', '-q', '--no-verify', '-m', 'the tree of ' + name]):
		subprocess.run(['git', '-C', directory] + words, check=True,
			capture_output=True)


def low_disk(work, limit):
	"""A line naming the first of WORK, the temporary directory and `/` with
	under `limit` GB free, or None."""

	for path in (work, tempfile.gettempdir(), '/'):
		try:
			free = shutil.disk_usage(path).free / 1e9
		except OSError:
			continue

		if free < limit:
			return 'low disk: {:.1f} GB free under {} (limit {} GB)'.format(
				free, path, limit)

	return None


LEASE_RETRY_SECONDS = 60
LEASE_WAIT_HOURS = 24


def hold_lease(config, argv=None):
	"""On a GPU search, make sure this process holds a GPU lease before it
	runs the benchmark. With `CUDA_VISIBLE_DEVICES` set it returns the
	devices. Without, it runs this script again under `gpu_command` (the
	words placed before a command: the machine's allocator, e.g. `gpu-alloc
	run -n 1 --`, or `env CUDA_VISIBLE_DEVICES=<index>` on a machine with
	none) and exits with that run's code; while the allocator has no GPU
	free it tries again every minute, for a day at most. It refuses when no
	`gpu_command` is recorded: unleased, a run sees every GPU and lands on
	another job's."""

	if not config.get('gpu', True):
		return None

	devices = os.environ.get('CUDA_VISIBLE_DEVICES')
	# Set by the waiting parent: touching it says the allocator started us.
	mark = os.environ.pop('AUTOOPT_LEASE_MARK', None)

	if mark:
		open(mark, 'w').close()

	if devices:
		return devices

	command = shlex.split(config.get('gpu_command') or '')

	if not command:
		fail('CUDA_VISIBLE_DEVICES is unset, so this run holds no GPU lease, '
			'and no `gpu_command` is recorded. Record the command that '
			'allocates this search\'s GPUs and runs what follows it under the '
			'lease:\n\n  python3 {script} config WORK --set '
			'gpu_command="gpu-alloc run -n {count} --" --reason "<why>"\n\n'
			'(`gpu-alloc` stands for this machine\'s allocator). On a machine '
			'with no allocator, record the GPU to use: `gpu_command` is the '
			'words placed before a command, so this works:\n\n  python3 '
			'{script} config WORK --set gpu_command="env '
			'CUDA_VISIBLE_DEVICES=<index>" --reason "<why>"\n\nor start this '
			'with CUDA_VISIBLE_DEVICES=<the indices you hold>. If the '
			'benchmark needs no GPU, set "gpu": false.'.format(
				script=os.path.join(SCRIPTS, 'autoopt.py'),
				count=config.get('gpus_per_agent', 1) or 1))

	if mark:
		fail('`gpu_command` ({}) ran this without setting '
			'CUDA_VISIBLE_DEVICES: it must set it for the command after it'
			.format(config['gpu_command']))

	argv = [sys.executable] + list(sys.argv if argv is None else argv)
	mark = os.path.join(tempfile.gettempdir(), 'lease-{}-{}'.format(
		os.getpid(), int(time.time())))
	end = time.time() + LEASE_WAIT_HOURS * 3600

	while True:
		sys.stderr.write('no GPU lease held: starting under `{}`\n'.format(
			config['gpu_command']))
		sys.stderr.flush()
		code = subprocess.run(command + argv, env=dict(os.environ,
			AUTOOPT_LEASE_MARK=mark)).returncode

		if os.path.exists(mark):
			os.remove(mark)
			sys.exit(code)

		if time.time() >= end:
			fail('`{}` started nothing for {} h (its last exit code was {}): '
				'no GPU came free'.format(config['gpu_command'],
				LEASE_WAIT_HOURS, code))

		# The allocator ended without starting this script: nothing claimed.
		sys.stderr.write('`{}` exited {} without starting this: no GPU free; '
			'again in {} s\n'.format(config['gpu_command'], code,
			LEASE_RETRY_SECONDS))
		sys.stderr.flush()
		time.sleep(LEASE_RETRY_SECONDS)


def lease_words(config):
	"""`gpu_command` as the words that go before a command that cannot take
	the lease itself (one started as a unit of its own, which inherits no
	lease), or [] when the search needs no GPU."""

	if not config.get('gpu', True):
		return []

	return shlex.split(config.get('gpu_command') or '')


def lease_flag(config):
	"""`--under` and the lease's words, for a script that starts such a
	command, or []."""

	words = lease_words(config)

	return ['--under', ' '.join(shlex.quote(word) for word in words)] if \
		words else []


def load_config(root):
	config = check_config(read_json(os.path.join(root, 'config.json')))
	BUILD_ENV.clear()
	BUILD_ENV.update(config.get('build_env') or {})

	return config


def check_config(config):
	"""Reject a config that would only fail later, inside a run.

	A missing `direction` or a gate with no threshold produces a crash eight
	hours into a search otherwise, with the runs already spent.
	"""

	maker = (config.get('skill') or {}).get('name')

	if maker not in (None, SKILL_NAME):
		fail('this search was made by the {} skill, whose scripts write the '
			'same layout with different rules; read and run it with that '
			'skill\'s scripts, not {}\'s'.format(maker, SKILL_NAME))

	for key in ('task', 'command', 'objective'):
		if key not in config:
			fail('config.json has no "{}"'.format(key))

	if '{seed}' not in config['command'] or '{out}' not in config['command']:
		fail('config.json "command" must contain both {seed} and {out}; got '
			+ repr(config['command']))

	check_placeholders('config.json "command"', config['command'], 'command')

	check_after(dict(AFTER_DEFAULTS, **config.get('after', {})))
	build_env = config.get('build_env', {})

	if not isinstance(build_env, dict) or any(key not in BUILD_VARS
			or not isinstance(value, str) for key, value in build_env.items()):
		fail('config.json "build_env" maps build variables ({}) to strings'
			.format(', '.join(BUILD_VARS[:6]) + ', ...'))

	objective = config['objective']
	if objective.get('direction') not in ('max', 'min'):
		fail('the objective needs "direction": "max" or "min"')
	if not objective.get('name'):
		fail('the objective needs a "name"')

	for gate in config.get('gates', []):
		if not gate.get('name'):
			fail('every gate needs a "name"')
		if gate.get('op') not in OPS:
			fail('gate {} needs "op" in {}'.format(gate.get('name'),
				sorted(OPS)))
		if not isinstance(gate.get('threshold'), (int, float)):
			fail('gate {} needs a numeric "threshold"'.format(gate['name']))

	for observer in config.get('observers', []):
		if not observer.get('name'):
			fail('every observer needs a "name"')

	if int(config.get('gpus_per_agent', 1) or 1) < 1:
		fail('"gpus_per_agent" must be at least 1')

	config.setdefault('gates', [])
	config.setdefault('observers', [])
	config.setdefault('runs_per_iteration', 3)
	config.setdefault('confirm_runs', 10)
	config.setdefault('staged', True)
	config.setdefault('seed_base', 0)
	config.setdefault('gpus_per_agent', 1)
	config.setdefault('timeout_seconds', 0)

	check_validation(config.get('external_validation'))

	tests = config.get('tests')

	if tests is not None:
		if not isinstance(tests, dict) or not tests.get('command'):
			fail('"tests" must be an object with a "command", or absent')
		check_placeholders('config.json "tests" command', tests['command'],
			'tests')
		tests.setdefault('timeout_seconds', 0)

	if config['staged'] and config['confirm_runs'] < config['runs_per_iteration']:
		fail('"confirm_runs" ({}) is below "runs_per_iteration" ({}); the '
			'confirmation stage has to be the deeper one'.format(
				config['confirm_runs'], config['runs_per_iteration']))

	return config


def check_validation(block):
	"""External validation is optional; a malformed one is not.

	It is deliberately *not* a gate and *not* an observer. It runs once a
	round against the root, it can be slow, and its measures may have nothing
	to do with the objective -- the point is to notice an optimization that
	has overfitted to the one input the search measures.
	"""

	if block is None:
		return

	if not isinstance(block, dict):
		fail('"external_validation" must be an object, or absent')

	command = block.get('command')

	if not command:
		fail('"external_validation" needs a "command"')

	for token in ('{iteration}', '{out}'):
		if token not in command:
			fail('"external_validation" command must contain {}; got {!r}'
				.format(token, command))

	check_placeholders('"external_validation" command', command,
		'validation')

	if not block.get('measures'):
		fail('"external_validation" needs at least one name in "measures"; '
			'they are recorded and reported, never gated on')

	block.setdefault('approved', False)
	block.setdefault('note', '')

	if not isinstance(block['approved'], bool):
		fail('"external_validation" "approved" must be true or false')


def state_path(root):
	return os.path.join(root, 'state.json')


# The letter every label carries: `A107`.
LABEL_LETTER = 'A'


def load_state(root):
	# Missing is a fresh search; corrupt is a failure. Reading a corrupt one
	# as fresh would put the search back on round 1 at the baseline, and
	# the next save would make that permanent.
	try:
		state = read_json(state_path(root))
	except FileNotFoundError:
		state = {'root': 0, 'round': 1, 'roots': [{'round': 1, 'id': 0}]}

	return state


LABEL = re.compile(r'([A-Za-z]*)(\d+)')


def label(root, identifier):
	"""`A107`: how an iteration is named everywhere a person reads it. The
	letter is constant; a bare `107` names the same iteration."""

	if identifier is None:
		return '-'

	return '{}{}'.format(LABEL_LETTER, identifier)


def parse_label(text):
	"""`107` or `A107` -> (107, 'A' or None). Raises ValueError."""

	match = LABEL.fullmatch(str(text).strip())

	if not match:
		raise ValueError(text)

	return int(match.group(2)), match.group(1) or None


def to_id(root, text, flag):
	"""An iteration named as `107` or `A107`, as an int. Any other letter is
	refused: every label carries `A`."""

	try:
		identifier, letter = parse_label(text)
	except ValueError:
		fail('{} takes an iteration such as 107 or A107; got {!r}'.format(
			flag, text))

	if letter not in (None, LABEL_LETTER):
		fail('{} names {}{}, but every label is {}{}: a letter other than '
			'{} is not a label'.format(
				flag, letter, identifier, LABEL_LETTER, identifier,
				LABEL_LETTER))

	return identifier


def save_state(root, state):
	state['schema_version'] = SCHEMA_VERSION
	write_json(state_path(root), state)
	refresh_notes(root)


class Lock:
	"""A lock around id allocation and state.json.

	Several subagents call `new` at the same moment. Without this, two of them
	read the same highest id and the second one's `git worktree add` fails on
	a branch that already exists -- or worse, succeeds against a directory the
	first one is already writing into.
	"""

	def __init__(self, root):
		self.path = os.path.join(root, '.lock')

	def __enter__(self):
		self.handle = open(self.path, 'w')
		fcntl.flock(self.handle, fcntl.LOCK_EX)
		return self

	def __exit__(self, *args):
		fcntl.flock(self.handle, fcntl.LOCK_UN)
		self.handle.close()


###
# Reading iterations back
###

def iteration_ids(root):
	directory = os.path.join(root, 'iterations')
	out = []

	for name in (os.listdir(directory) if os.path.isdir(directory) else []):
		if name.isdigit():
			out.append(int(name))

	return sorted(out)


def iteration_dir(root, identifier):
	return os.path.join(root, 'iterations', str(identifier))


def checkout_dir(root, identifier, config=None):
	"""Where an iteration's git worktree is.

	In repository mode it is `iterations/<id>/src`, so the copied repository's
	own files never share a directory with the search's records -- a
	repository has a README.md of its own. Otherwise the iteration directory
	is the worktree.
	"""

	config = config if config is not None else load_config(root)
	directory = iteration_dir(root, identifier)

	return os.path.join(directory, 'src') if config.get('repo') else directory


def load_result(root, identifier):
	"""`result.json` for one iteration, or None if it has not finished."""

	try:
		with open(os.path.join(iteration_dir(root, identifier),
			'result.json')) as infile:
			return json.load(infile)
	except (OSError, ValueError):
		return None


def load_meta(root, identifier):
	"""`meta.json` -- what the iteration is trying, written before it runs."""

	try:
		with open(os.path.join(iteration_dir(root, identifier),
			'meta.json')) as infile:
			return json.load(infile)
	except (OSError, ValueError):
		return None


def all_results(root):
	out = []

	for identifier in iteration_ids(root):
		result = load_result(root, identifier)
		if result is not None:
			# The control flag may be recorded only in meta.json.
			if 'control' not in result and (load_meta(root, identifier)
					or {}).get('control'):
				result['control'] = True
			out.append(result)

	return out


def better(a, b, direction):
	"""Is `a` a better objective value than `b`? True when `b` is None."""

	if a is None:
		return False
	if b is None:
		return True

	return a > b if direction == 'max' else a < b


def frontier(results, direction, exclude=None):
	"""The best passing iteration so far: `(value, id)`, or `(None, None)`.

	**Always the iteration's median, never a single run and never a round.**
	What the search is comparing is ideas, and an idea's value is the median
	over its seeds; one run is a sample of it, and the best run of an
	iteration is a lucky sample. Ranking on a run would make the search
	chase seeds, and `status == "partial"` exists so that an iteration
	measured by too few runs cannot enter this comparison at all.

	Only `status == "ok"` counts. An iteration that broke a gate is not on
	the frontier however good its objective, which is the entire point of a
	gate. Nor is a same-code control: its code is its parent's, so a better
	reading is the machine's drift, not an improvement.
	"""

	best, best_id = None, None

	for result in results:
		if (result.get('status') != 'ok' or result.get('control')
				or result.get('split')
				or result.get('id') == exclude):
			continue

		value = (result.get('objective') or {}).get('median')

		if better(value, best, direction):
			best, best_id = value, result.get('id')

	return best, best_id


def frontier_path(results, direction, seed=None):
	"""Every iteration that improved on everything before it, in order.

	This is what the dashboard draws solid and everything else faded.

	`seed` is a starting value to beat. Without it, the first iteration is
	always on its own path.
	"""

	out, best = [], seed

	for result in sorted(results, key=lambda r: r.get('id', 0)):
		if result.get('status') != 'ok' or result.get('control') or \
				result.get('split'):
			continue

		value = (result.get('objective') or {}).get('median')

		if better(value, best, direction):
			best = value
			out.append(result)

	return out


###
# init
###

# Everything an iteration *measures*, as opposed to everything it *changes*.
# The branch holds the code, so that `git diff autoopt/<parent>..autoopt/<id>`
# is the idea and nothing else; the numbers sit beside it on disk, where the
# dashboard reads them.
#
# These go in `repo.git/info/exclude` rather than in a committed .gitignore:
# every worktree shares the common directory, so one file covers iterations
# that do not exist yet, and it cannot itself be committed by an iteration
# that runs `git add -A`.
EXCLUDES = """\
runs/
unit-tests/
cache/
result.json
report.txt
meta.json
README.md
notes.md
probe/
check/
check.json
cold.json
*.npz
__pycache__/
"""

# Repository mode keeps the records beside the worktree rather than in it, so
# the list above would only hide real package files -- a new README.md in a
# subpackage, a test fixture saved as .npz. The repository's own .gitignore
# does the rest.
EXCLUDES_REPO = """\
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
"""


def hidden_copies(sources):
	"""The files `--copy` would put in a plain-mode baseline that EXCLUDES
	hides from git, so that they would never be committed: absent from the
	baseline's branch and from every iteration forked off it."""

	names = []

	for source in sources:
		source = os.path.abspath(os.path.expanduser(source))
		top = os.path.basename(source.rstrip(os.sep))

		if not os.path.isdir(source):
			names.append(top)
			continue

		for base, dirs, files in os.walk(source):
			dirs[:] = [d for d in dirs if d not in ('__pycache__', '.git')]
			relative = os.path.relpath(base, source)
			names += [os.path.normpath(os.path.join(top, relative, name))
				for name in files]

	if not names:
		return []

	# A scratch repository with the same exclude list answers exactly what
	# the real one will, before anything of the search exists.
	scratch = tempfile.mkdtemp(prefix='autoopt-check-ignore-')

	try:
		git('init', '-q', scratch)
		with open(os.path.join(scratch, '.git', 'info', 'exclude'), 'w') as out:
			out.write(EXCLUDES)
		out = subprocess.run(['git', '-C', scratch, 'check-ignore', '--stdin',
			'--no-index'], input='\n'.join(names) + '\n',
			capture_output=True, text=True)
	finally:
		shutil.rmtree(scratch, ignore_errors=True)

	return out.stdout.split('\n')[:-1] if out.stdout else []


SUITE_BRANCH = re.compile(r'auto-optimize/\d{4}-\d{2}-\d{2}-unit-tests')


def check_suite_squashed(top, branch):
	"""The new unit-test suite is one commit (references/step1-unit-tests.md,
	*Where it lives*, step 3): refuse a clone on the suite's branch that is
	more than one commit ahead of the branch it was cloned from, naming the
	command that squashes it. Silent when the branch is not the suite's or
	`origin/HEAD` is not there to count from."""

	if not SUITE_BRANCH.fullmatch(branch):
		return

	try:
		base = git('-C', top, 'rev-parse', '--verify', '-q',
			'refs/remotes/origin/HEAD').strip()
		ahead = int(git('-C', top, 'rev-list', '--count',
			'{}..HEAD'.format(base)).strip())
	except (RuntimeError, ValueError):
		return

	if ahead > 1:
		fail('{} is {} commits ahead of its base: the unit-test suite is one '
			'commit. Squash it, then run init again:\n\n  git -C {} reset '
			'--soft $(git -C {} merge-base HEAD refs/remotes/origin/HEAD) && '
			'git -C {} commit -m "<message>"'.format(branch, ahead, top, top,
				top))


def copy_repository(source, repo, allow_dirty):
	"""Clone the original into `repo.git`; return what the search records.

	A bare clone of a local path hardlinks the object files, so it costs
	almost no disk, and it carries the original's history -- which is what
	lets the winning branch go back upstream. The clone's `origin`
	is removed: it points at the original's directory, and a push there is
	the one way this search could modify the original.
	"""

	source = os.path.abspath(os.path.expanduser(source))

	try:
		top = git('-C', source, 'rev-parse', '--show-toplevel').strip()
	except RuntimeError:
		fail('--repo {} is not inside a git repository'.format(source))

	try:
		branch = git('-C', top, 'symbolic-ref', '--short', 'HEAD').strip()
	except RuntimeError:
		fail('{} has a detached HEAD; check out the branch the search should '
			'start from, which is also the branch its result is '
			'compared with'.format(top))

	# --no-optional-locks: a plain status refreshes and rewrites the
	# original's index, and the original is never written to.
	dirty = git('-C', top, '--no-optional-locks', 'status', '--porcelain',
		'--untracked-files=no').rstrip()

	if dirty and not allow_dirty:
		fail('{} has uncommitted changes to tracked files, and the copy is '
			'taken from the last commit, so they would not be in it:\n{}\n\n'
			'Commit them in the original, or pass --allow-dirty to start '
			'from {} without them.'.format(top, dirty, branch))

	commit = git('-C', top, 'rev-parse', 'HEAD').strip()

	check_suite_squashed(top, branch)

	# The branch's upstream names the remote the original came from;
	# without one, `origin` is the usual answer.
	remote = 'origin'

	try:
		upstream = git('-C', top, 'rev-parse', '--abbrev-ref',
			'{}@{{upstream}}'.format(branch)).strip()
		remote = upstream.split('/', 1)[0] or remote
	except RuntimeError:
		pass

	try:
		remote_url = git('-C', top, 'remote', 'get-url', remote).strip()
	except RuntimeError:
		remote_url = None

	if not os.path.isdir(repo):
		git('clone', '--bare', '-q', '--single-branch', '--branch', branch,
			top, repo)
		git('-C', repo, 'remote', 'remove', 'origin')

	return {'path': top, 'branch': branch, 'commit': commit,
		'remote': remote, 'remote_url': remote_url,
		'dirty_at_copy': bool(dirty)}


def orphan_worktree(repo, directory, branch):
	"""A worktree on a branch with no commit yet, so the baseline's first
	commit is the runner's, and `new` refuses to fork it until it has run.

	`worktree add --orphan` is git 2.42 and later. Older git gets the same
	result in two steps: a worktree detached at a throwaway empty commit,
	then `checkout --orphan` onto the branch, which leaves that commit
	unreferenced.
	"""

	try:
		git('-C', repo, 'worktree', 'add', '-q', '--orphan', '-b', branch,
			directory)
		return
	except RuntimeError as error:
		if 'orphan' not in str(error):
			raise

	empty = git('-C', repo, 'hash-object', '-t', 'tree', '-w',
		'--stdin', input='').strip()
	root = git('-C', repo, '-c', 'user.name=auto-optimize', '-c',
		'user.email=auto-optimize@localhost', 'commit-tree', empty, '-m',
		'empty').strip()
	git('-C', repo, 'worktree', 'add', '-q', '--detach', directory, root)
	git('-C', directory, 'checkout', '-q', '--orphan', branch)


HARNESS_MANIFEST = 'harness.json'


def harness_files(root):
	"""`{relative path: sha256}` of the harness (repository mode); `{}` without."""

	import hashlib

	folder = os.path.join(root, 'harness')
	found = {}

	for where, dirs, names in os.walk(folder):
		dirs[:] = sorted(d for d in dirs if d not in ('__pycache__', '.git'))

		for name in sorted(names):
			if name.endswith(('.pyc', '.log')):
				continue

			path = os.path.join(where, name)

			with open(path, 'rb') as handle:
				found[os.path.relpath(path, folder)] = hashlib.sha256(
					handle.read()).hexdigest()

	return found


def record_harness(root, reason=None):
	"""Write the harness manifest: what `setup --done` or `harness --accept`
	says the harness is. Every run checks it (`harness_problem`)."""

	files = harness_files(root)

	if not files:
		return

	write_json(os.path.join(root, HARNESS_MANIFEST), {'recorded': now(),
		'reason': reason or 'setup --done', 'files': files})


def harness_problem(root):
	"""Why no run may start: the harness differs from its manifest, or None.
	The harness is outside `repo.git`, so no diff shows an edit to it."""

	path = os.path.join(root, HARNESS_MANIFEST)

	if not os.path.exists(path):
		return None

	recorded = read_json(path)['files']
	current = harness_files(root)
	changed = sorted(name for name in set(recorded) | set(current)
		if recorded.get(name) != current.get(name))

	if not changed:
		return None

	return ('the harness changed since {} (`{}`): {}. The benchmark and its '
		'build are the same for every iteration; if the change is meant, '
		'`autoopt.py harness {} --accept --reason "..."` records it and the '
		'baseline is re-measured'.format(read_json(path)['recorded'],
		os.path.join(root, 'harness'), ', '.join(changed[:6]), root))


def harness(args):
	root = resolve(args.root)

	if args.accept:
		if not args.reason:
			fail('--accept needs --reason: the harness is fixed for the '
				'whole search')

		record_harness(root, args.reason)
		print('recorded the harness ({} files)'.format(
			len(harness_files(root))))
		return 0

	problem = harness_problem(root)
	print(problem or 'the harness matches its manifest')
	return 1 if problem else 0


def build_env_from(given):
	"""`{variable: value}` for `config.json`: `--build-env VAR=VALUE` pairs,
	else the build variables set in the environment that ran `init`."""

	if not given:
		return {key: os.environ[key] for key in BUILD_VARS
			if key in os.environ}

	pairs = {}

	for item in given:
		key, _, value = item.partition('=')

		if key not in BUILD_VARS or not _:
			fail('--build-env takes VAR=VALUE for one of: {}'.format(
				', '.join(BUILD_VARS)))

		pairs[key] = value

	return pairs


def init(args):
	root = os.path.abspath(os.path.expanduser(args.root))

	if not root.endswith('autooptimize'):
		root = os.path.join(root, 'autooptimize')

	if os.path.exists(os.path.join(root, 'config.json')) and not args.force:
		fail('{} already exists; pass --force to rewrite its config'.format(
			os.path.join(root, 'config.json')))

	if args.confirm_runs is not None and not args.staged:
		fail('--confirm-runs is the confirmation depth of a --staged search; '
			'without --staged every iteration runs --runs')

	try:
		gates = json.loads(args.gates) if args.gates else []
	except ValueError as error:
		fail('--gates is not valid JSON ({}); it takes a list such as '
			'\'[{{"name": "cost", "op": "<=", "threshold": 1.0}}]\''.format(
				error))

	config = {
		'schema_version': SCHEMA_VERSION,
		'task': args.task,
		'command': args.command,
		'objective': {'name': args.objective, 'direction': args.direction,
			'unit': args.unit or ''},
		'gates': gates,
		'observers': ([{'name': name} for name in args.observers.split(',')]
			if args.observers else []),
		'runs_per_iteration': args.runs,
		# One count of runs per iteration; a staged search (--staged) screens
		# at --runs and confirms to --confirm-runs.
		'confirm_runs': (args.confirm_runs or 10) if args.staged
			else args.runs,
		'staged': bool(args.staged),
		'seed_base': args.seed_base,
		'gpus_per_agent': args.gpus_per_agent,
		'timeout_seconds': args.timeout,
		'gpu': not args.no_gpu,
		'gpu_command': args.gpu_command,
		'floor': args.floor,
		'workers': args.workers,
		# The skill these scripts belong to and were at: another skill's
		# scripts refuse the search (check_config), and a later change is
		# reported once (`skill_notice`).
		'skill': {'name': SKILL_NAME, 'version': skill_version(),
			'commit': skill_commit()},
		# The model every iteration subagent is spawned with; the miner and
		# the orchestrator keep the session's.
		'subagent_model': args.subagent_model,
		# The runner promotes an iteration that passes the default keep rule
		# while its parent is still the root (tree mode).
		'promote': args.promote,
		# What every build in this search sees (BUILD_VARS): the variables
		# set when `init` ran, or those given with --build-env.
		'build_env': build_env_from(args.build_env),
		'after': after_from(args.after),
	}

	if args.validation_command:
		config['external_validation'] = {
			'command': args.validation_command,
			'measures': [name for name
				in (args.validation_measures or '').split(',') if name],
			'note': args.validation_note or '',
			# Sign-off is the user's, never inferred. `validate` refuses
			# until `validate --approve` records it.
			'approved': False,
		}

	if args.tests:
		config['tests'] = {'command': args.tests,
			'timeout_seconds': args.tests_timeout,
			'path': args.tests_path}

	check_config(config)

	hidden = [] if args.repo else hidden_copies(args.copy or [])

	if hidden:
		fail('{} copied file(s) match the plain-mode exclude list (records, '
			'*.npz, README.md, ...), so git would never commit '
			'them and no iteration would have them:\n  {}\n\nPut inputs '
			'the benchmark reads in {} and point the command at them there; '
			'nothing was created.'.format(len(hidden), '\n  '.join(
				hidden[:20]), os.path.join(root, 'reference') + os.sep))

	# The bare repo has no working tree of its own, so every iteration --
	# including the baseline -- is a worktree, and they are all the same shape.
	repo = os.path.join(root, 'repo.git')

	previous = (load_config(root) if os.path.exists(os.path.join(root,
		'config.json')) else {})

	if args.repo and previous.get('repo') and os.path.isdir(repo):
		# `--force` rewrites the config; the copy already made, and the
		# commit its baseline sits on, stay what they were.
		config['repo'] = previous['repo']
	elif args.repo:
		config['repo'] = copy_repository(args.repo, repo, args.allow_dirty)
	elif not os.path.isdir(repo):
		git('init', '--bare', '-q', repo)

	# Only now, so a refused --repo leaves nothing behind.
	os.makedirs(os.path.join(root, 'rounds'), exist_ok=True)
	os.makedirs(os.path.join(root, 'iterations'), exist_ok=True)
	os.makedirs(os.path.join(root, 'reference'), exist_ok=True)
	write_skeletons(root)

	write_json(os.path.join(root, 'config.json'), config)

	os.makedirs(os.path.join(repo, 'info'), exist_ok=True)

	with open(os.path.join(repo, 'info', 'exclude'), 'w') as out:
		out.write(EXCLUDES_REPO if args.repo else EXCLUDES)

	baseline = iteration_dir(root, 0)
	checkout = checkout_dir(root, 0, config)

	# In repository mode the benchmark is not part of the repository: it
	# lives in `harness/`, fixed for the whole search, and imports the
	# package from whichever worktree it is pointed at.
	harness = os.path.join(root, 'harness')
	destination = harness if args.repo else baseline

	if not os.path.exists(checkout):
		if args.repo:
			os.makedirs(baseline, exist_ok=True)
			git('-C', repo, 'worktree', 'add', '-q', '-b', 'autoopt/0',
				checkout, config['repo']['commit'])
		else:
			orphan_worktree(repo, baseline, 'autoopt/0')

		os.makedirs(destination, exist_ok=True)

		for source in args.copy or []:
			source = os.path.abspath(os.path.expanduser(source))
			target = os.path.join(destination, os.path.basename(source))

			if os.path.isdir(source):
				shutil.copytree(source, target, dirs_exist_ok=True,
					ignore=shutil.ignore_patterns('__pycache__', '.git'))
			else:
				shutil.copy2(source, target)

	# `--force` rewrites the config and nothing else: the state carries the
	# root and the round, and the baseline's meta.json the commit it ran.
	if not os.path.exists(state_path(root)):
		save_state(root, {'root': 0, 'round': 1, 'roots': [{'round': 1,
			'id': 0}]})

	wrote_spec = write_spec_template(root, config)

	# Round 0: the baseline is the thing every round is measured against,
	# not a member of the first one.
	if not os.path.exists(os.path.join(baseline, 'meta.json')):
		write_meta(root, 0, {'id': 0, 'parent': None, 'round': 0,
			'title': 'baseline', 'category': ROOT_CATEGORY,
			'branch': 'autoopt/0', 'created': now()})

	runner = os.path.join(os.path.dirname(os.path.abspath(__file__)),
		'run_iteration.py')

	# A GPU-less benchmark needs no lease, and telling it to take one anyway
	# would have it queue behind other GPU jobs for nothing.
	launch = ('python3 {} {}'.format(runner, baseline) if args.no_gpu or
		args.gpu_command else 'CUDA_VISIBLE_DEVICES=<the {} GPU(s) the '
		'allocator gave> python3 {} {}'.format(args.gpus_per_agent, runner,
		baseline))

	print(root)

	if wrote_spec:
		print('wrote {}: fill in every TODO from the interview; nothing runs '
			'until it is done'.format(spec_path(root)))

	if args.repo:
		print('copied {} at {} ({}) into {}'.format(config['repo']['path'],
			config['repo']['branch'], config['repo']['commit'][:10], repo))
		print('baseline worktree: {}'.format(checkout))
		print('harness: {}'.format(harness))
		print('fill in spec.md, then hand step 2 to the setup subagent '
			'(references/step0-interview.md): `autoopt.py setup <work>` '
			'prints its prompt. It {}runs the baseline as:'.format('' if
				args.copy else 'puts the benchmark in the harness and '))
	else:
		print('baseline worktree: {}'.format(baseline))
		print('fill in spec.md, then hand step 2 to the setup subagent '
			'(references/step0-interview.md): `autoopt.py setup <work>` '
			'prints its prompt. It {}runs the baseline as:'.format('' if
				args.copy else 'puts the code there and '))

	print('  ' + launch)

	return 0


def now():
	return time.strftime('%Y-%m-%dT%H:%M:%S')


def write_meta(root, identifier, meta):
	write_json(os.path.join(iteration_dir(root, identifier), 'meta.json'),
		meta)


###
# spec.md: step 0's record, and the gate on everything after it
###

SPEC_TEMPLATE = """# {task}

Step 0's record (references/step0-interview.md). `autoopt.py new`, `brief`
and `run_iteration.py` refuse to run while any placeholder below is left
unanswered. Mark an answer the user did not give yourself as
"(default, accepted by the user)" or "(default, not asked: <why>)".

## The interview

- Asked on: TODO (the date, and how: the AskUserQuestion calls, or "in the
  request", quoting it)
- Kind of search: TODO (algorithm implementation; repository or plain mode)
- New unit tests: TODO ("n/a: plain mode"; or no; or yes, for the optimized
  functions or the whole repository, with the clone, its branch, and the
  original's remote and base branch)

## Rules this search runs by

- Objective: {objective}, {direction}{unit}
- Gates: {gates}. Reference each compares against: TODO
- Gate proof: TODO (step 2, before the baseline: each gate's value on
  unchanged code, on a change the user accepts and on a lossy change; for a
  tolerance, both error distributions and where the threshold sits; "none"
  when there are no gates)
- Keep rule: TODO (what earns a promotion, word for word: by default a
  median beyond the noise band of the parent's, plus any tie rule the user
  adopted)
- Observers: {observers}
- Thread counts timed, tracking inputs: TODO (or "none")
- Plan: {runs} runs per iteration{confirm}; one iteration at a time on
  {devices}
- Machine and environment: TODO (whose machine this is and what else runs
  on it; the CPU cores and memory the search may use; the environment or
  command that runs the code, and whether packages may be installed into it)
- Width measured: TODO (step 2, before the baseline: for a CPU benchmark,
  the concurrency table and the width it supports; for a GPU benchmark,
  "one run per GPU")
- Benchmark: TODO (step 2, with the benchmark: what one run does, quoted in
  every brief so no subagent reads the harness for it: the entry point, the
  call into the code under test and its arguments, the inputs and their
  sizes, what is timed and what is not)
- External validation: TODO (none, or the proposed command and whether the
  user signed off)
- Off limits: TODO
- Portability: TODO (every common OS, Linux, macOS and Windows, by default;
  or this machine's OS only, named, so an optimization may rely on it)

## Goal and scope

TODO: what the user is trying to achieve, which ideas are in scope, and
which topics each subagent owns.

## Rules added during the search

Each with the date and the user's words, written before it is used.
"""


def spec_path(root):
	return os.path.join(root, 'spec.md')


# The shared files `init` starts, so every search looks the same. Each is
# written only when absent; a later `init --force` never touches them.
# Subagents append under `flock`, which is why the ledger and the two tables
# end with the part that grows.
SKELETONS = {
	'ideas.md': """# Ideas

One line per idea, in this form, so `autoopt.py status` can count what is
left:

    - [CATEGORY] where (file:function): what to change. Why: why it should help. Gain: the expected change, or the share of the call it could remove. Exact: bit-identical, within tolerance, or changes outputs. (S<stage>)

`autoopt.py finish` strikes through the line an iteration was briefed from
(`brief --idea-line`), adding its label and what became of it: "- ~~[MICRO]
… (S1)~~ A12: kept out: its median is within the noise band …". `autoopt.py
mine --done` appends each stage's idea lines at the end, under a lock.

## Decisions taken while the user was away

The question, the answer taken, why, and the date.

## Queue
""",
	'knowledge.md': """# Knowledge ledger

Read this before reading code or planning; a premise settled here is not
measured again. Append only, one to three lines per entry, with the command
below, which takes the lock (`$AO` and `$WORK` as in every brief):

    python3 $AO/autoopt.py ledger $WORK --by <your label> --add $DIR/probe/ledger.txt

Each entry: `- [tag] the fact, with its number (label, root <label>)`. Tags:
`machine`, `harness`, `code:<subsystem>`, `closed`, `bug`. A correction ends
`(corrects: <words only the old entry has>)`, and `ledger --add` strikes the
old entry through as it appends.

## Entries
""",
	os.path.join('tools', 'README.md'): """# Shared tools

Helpers later iterations reuse. None of them is the harness, and none
changes a measurement. Add a row under a lock: `flock <this file> -c 'cat
>> <this file>' < <your row>`. Checkers are listed in checkers/README.md.

| Tool | What it does | Typical use | Measured cost |
|---|---|---|---|
""",
	os.path.join('tools', 'checkers', 'README.md'): """# Checker library

One directory per checker, `<subsystem>-<label>/`, holding sources and
scripts, not build trees, and `ROW.md`, its full row: what it compares, how
it is built, the `run` line, its planted faults and inputs. `autoopt.py
finish --checker` adds the directory and the line below, with the run time
it measured. `autoopt.py checkers $WORK --id <yours> --name <checker>` runs
one on your parent and your code; `--touched`, every one guarding a file you
changed.

| Checker | Guards | Compares | Run time |
|---|---|---|---|
""",
}


def write_skeletons(root):
	for relative, text in SKELETONS.items():
		target = os.path.join(root, relative)
		if os.path.exists(target):
			continue
		os.makedirs(os.path.dirname(target), exist_ok=True)
		with open(target, 'w') as out:
			out.write(text.replace('@LEDGER@', shlex.quote(os.path.join(root,
				'knowledge.md'))).replace('@IDEAS@', shlex.quote(os.path.join(
					root, 'ideas.md'))))


# An untried idea line in ideas.md, and a tried (struck through) one.
IDEA_LINE = re.compile(r'^- \[[A-Z_]+\]')
TRIED_LINE = re.compile(r'^- ~~\[[A-Z_]+\]')


def idea_line(root, value):
	"""`(number, line)` for `--idea-line`: a 1-based line number of
	ideas.md, or text that exactly one untried line contains, which stays
	right when lines are added above it."""

	try:
		with open(os.path.join(root, 'ideas.md')) as handle:
			lines = handle.read().split('\n')
	except OSError:
		lines = []

	if str(value).isdigit():
		number = int(value)
		if 1 <= number <= len(lines) and IDEA_LINE.match(lines[number - 1]):
			return number, lines[number - 1]
		fail('line {} of ideas.md is not an untried idea line '
			'(`- [CATEGORY] …`)'.format(value))

	found = [(index + 1, line) for index, line in enumerate(lines)
		if IDEA_LINE.match(line) and value in line]
	if len(found) != 1:
		fail('--idea-line {!r} matches {} untried lines of ideas.md; give text '
			'only one contains, or its number'.format(value, len(found)))
	return found[0]


def drop(args):
	"""Strike an untried idea line whose premise is gone, with why, so
	`brief --next` never picks it."""

	root = resolve(args.root)
	number, line = idea_line(root, args.idea_line)
	path = os.path.join(root, 'ideas.md')

	with open(path, 'r+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		lines = handle.read().split('\n')
		if lines[number - 1] != line:
			fail('ideas.md changed while dropping; run it again')
		lines[number - 1] = '- ~~{}~~ dropped: {}'.format(line[2:].strip(),
			args.reason)
		handle.seek(0)
		handle.write('\n'.join(lines))
		handle.truncate()

	print(lines[number - 1])
	return 0


def idea_key(line):
	"""A short, stable name for one untried line of ideas.md."""

	return hashlib.sha1(line.strip().encode()).hexdigest()[:10]


def strike_idea(root, state, identifier):
	"""Strike the ideas.md line an iteration was briefed from (its
	`idea_key`), with its label and what became of it, under the file's lock.
	Returns the struck line, or None when it was not found."""

	meta = load_meta(root, identifier) or {}
	result = load_result(root, identifier) or {}
	key = meta.get('idea_key')
	path = os.path.join(root, 'ideas.md')

	if not key or not os.path.exists(path):
		return None

	with open(path, 'r+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		lines = handle.read().split('\n')
		for index, line in enumerate(lines):
			if IDEA_LINE.match(line) and idea_key(line) == key:
				lines[index] = '- ~~{}~~ {}: {}'.format(line[2:].strip(),
					label(root, identifier),
					keep_phrase(root, state, dict(result, id=identifier)))
				handle.seek(0)
				handle.write('\n'.join(lines))
				handle.truncate()
				return lines[index]

	return None


def ledger_plan(root, path):
	"""`(entries, struck)`: the entries of the file at `path` not already in
	knowledge.md, and the line index each correction among them strikes.
	Fails, with nothing written, on a correction whose words match no
	unstruck entry or several."""

	ledger_path = os.path.join(root, 'knowledge.md')
	entries = [line.rstrip() for line in open(os.path.expanduser(path))
		if line.strip()]

	with open(ledger_path) as handle:
		lines = handle.read().rstrip('\n').split('\n')

	# An entry already there verbatim was appended by an earlier call, so a
	# second call after a refusal elsewhere appends nothing twice.
	present = set(lines)
	entries = [entry for entry in entries if entry not in present]
	struck = []

	for entry in entries:
		# Greedy to the line's last ")": the words may hold parentheses.
		match = re.search(r'\(corrects: (.+)\)\s*$', entry)
		if not match:
			continue
		words = match.group(1).strip()
		found = [index for index, line in enumerate(lines)
			if line.startswith('- ') and not line.startswith('- ~~')
			and words in line]
		if len(found) != 1:
			fail('"(corrects: {})" matches {} entr{} of {}; give words only '
				'the old entry has. Nothing was written'.format(words,
					len(found), 'y' if len(found) == 1 else 'ies', ledger_path))
		struck.append(found[0])

	return entries, struck


def ledger_append(root, path, by):
	"""Append the file's entries to knowledge.md under its lock, striking
	each corrected entry. Returns what it did, in a line."""

	ledger_path = os.path.join(root, 'knowledge.md')

	with open(ledger_path, 'r+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		entries, struck = ledger_plan(root, path)
		lines = handle.read().rstrip('\n').split('\n')

		for index in struck:
			lines[index] = '- ~~{}~~ corrected by {}'.format(lines[index][2:],
				by)

		handle.seek(0)
		handle.write('\n'.join(lines + entries) + '\n')
		handle.truncate()

	return 'appended {} entr{} to {}{}'.format(len(entries), 'y' if
		len(entries) == 1 else 'ies', ledger_path, '; struck {} corrected'
		.format(len(struck)) if struck else '')


def ledger(args):
	"""Append a file of ledger entries to knowledge.md under its lock. An
	entry ending `(corrects: <words>)` strikes the one older entry holding
	those words; with none or several, nothing is written."""

	print(ledger_append(resolve(args.root), args.add, args.by))
	return 0


def idea_counts(root):
	"""`(untried, tried)` idea lines in ideas.md."""

	try:
		with open(os.path.join(root, 'ideas.md')) as handle:
			lines = handle.read().splitlines()
	except OSError:
		return 0, 0

	return (sum(1 for line in lines if IDEA_LINE.match(line)),
		sum(1 for line in lines if TRIED_LINE.match(line)))


def stage_budget(root, config, state=None):
	"""The search against `after.budget_stages`, the number of idea-miner
	stages the user agreed to: `budget` (None when there is none), `done`
	(the last stage closed), `spent` (that many stages have closed, so no
	further one opens) and `exhausted` (spent, no stage open, and every idea
	tried or dropped: the search stops for `budget`)."""

	state = load_state(root) if state is None else state
	budget = (config.get('after') or {}).get('budget_stages')
	mining = state.get('mining') or {}
	done = mining.get('done') or 0
	spent = bool(budget) and done >= budget
	untried, _ = idea_counts(root)

	return {'budget': budget, 'done': done, 'spent': spent,
		'exhausted': spent and (mining.get('stage') or 0) <= done
			and untried == 0}


def write_spec_template(root, config):
	path = spec_path(root)

	if os.path.exists(path):
		return False

	gates = config.get('gates') or []
	text = SPEC_TEMPLATE.format(task=config.get('task') or 'TODO: the task',
		objective=config['objective']['name'],
		direction='maximized' if config['objective']['direction'] == 'max'
			else 'minimized',
		unit=' ({})'.format(config['objective']['unit'])
			if config['objective'].get('unit') else '',
		gates='; '.join('{} {} {}{}'.format(g['name'], g['op'], g['threshold'],
			' (tol {})'.format(g['tol']) if g.get('tol') is not None else '')
			for g in gates) or 'none',
		observers=', '.join(o['name'] for o in config.get('observers') or [])
			or 'none',
		runs=config['runs_per_iteration'], confirm=', confirming to {}'.format(
			config['confirm_runs']) if config.get('staged') else '',
		# Without a GPU, gpus_per_agent is how many of an iteration's runs
		# go at once (run_iteration.leased_devices).
		devices=('{} GPU(s)' if config.get('gpu', True) else
			'no GPU, {} run(s) at a time').format(
				config.get('gpus_per_agent', 1) or 1))

	with open(path, 'w') as out:
		out.write(text)

	return True


def spec_problems(root):
	"""Why the spec is not finished: missing, or the lines still reading
	TODO. Empty when it is ready."""

	path = spec_path(root)

	if not os.path.exists(path):
		return ['{} does not exist'.format(path)]

	with open(path) as handle:
		return ['line {}: {}'.format(number, line.strip())
			for number, line in enumerate(handle, 1) if 'TODO' in line]


def require_spec(root, action):
	"""Refuse `action` until step 0 is written down. AUTOOPT_SKIP_SPEC_CHECK
	exists for the script tests and nothing else."""

	if os.environ.get('AUTOOPT_SKIP_SPEC_CHECK'):
		return

	problems = spec_problems(root)

	if problems:
		fail('refusing to {}: step 0 is not finished. Interview the user '
			'(references/step0-interview.md) and fill in spec.md; Gate proof, '
			'Width measured and Benchmark are answered at step 2, before the '
			'baseline '
			'(references/step2-baseline.md). It still has:\n  '
			'{}'.format(action, '\n  '.join(problems[:12])))


def require_interview(root, action):
	"""Like require_spec, but allows the three lines step 2 answers (Gate
	proof, Width measured, Benchmark): the idea miner starts while step 2
	runs."""

	if os.environ.get('AUTOOPT_SKIP_SPEC_CHECK'):
		return

	problems = [problem for problem in spec_problems(root)
		if not re.search(r'Gate proof|Width measured|Benchmark', problem)]

	if problems:
		fail('refusing to {}: the interview is not written down. Fill in '
			'spec.md from step 0 (references/step0-interview.md). It still '
			'has:\n  {}'.format(action, '\n  '.join(problems[:12])))


###
# new
###

def check_fork(root, config, state, parent):
	"""The checks `new` makes before forking: the parent exists and is ok.
	`brief` makes the same ones, so a brief never names a fork `new` would
	refuse."""

	existing = iteration_ids(root)

	if parent not in existing:
		fail('parent {} does not exist'.format(label(root, parent)))

	# Nothing is built on an iteration that failed a gate, crashed, or was
	# never measured: its code is not a result, and every child of it would
	# inherit whatever broke the gate. The root may still lack a
	# result.json before step 2 runs it.
	def require_ok(number, role):
		result = load_result(root, number)
		if result is None:
			if number == state['root']:
				return
			fail('{} {} has no result.json: fork only from an iteration '
				'whose status is ok (the current root is {})'.format(
					role, label(root, number), label(root, state['root'])))
		status = result.get('status')
		if status != 'ok':
			fail('{} {} is {}: an iteration that is not ok (a gate failed, a '
				'run crashed, or it is held for the user) is never forked '
				'from; fork from the current root ({}) and '
				're-implement the change there if it is still wanted '
				'(references/step3-rounds.md, *Ids, parents and briefs*)'.format(
					role, label(root, number), status,
					label(root, state['root'])))

	require_ok(parent, 'parent')


def pending_briefs(root):
	"""Ids with a brief (`briefs/<id>.md`) and no iteration yet: a subagent
	is about to run `new --id` with them."""

	folder = os.path.join(root, 'briefs')
	existing = set(iteration_ids(root))
	# An iteration moved aside (`iterations/<id>.abandoned`) keeps its brief.
	iterations = os.path.join(root, 'iterations')
	existing |= {int(name.split('.', 1)[0]) for name in (os.listdir(
		iterations) if os.path.isdir(iterations) else []) if '.' in name
		and name.split('.', 1)[0].isdigit()}

	return {int(name[:-3]) for name in (os.listdir(folder)
		if os.path.isdir(folder) else []) if name.endswith('.md')
		and name[:-3].isdigit() and int(name[:-3]) not in existing}


def taken_ids(root):
	"""`(branches, taken)`: ids with an `autoopt/<id>` branch, and every id
	that is not free. An iteration moved aside (`iterations/<id>.abandoned`)
	keeps its branch, so its id is taken even though no directory holds it,
	and a pending brief reserves its id."""

	branches = {int(name[len('autoopt/'):]) for name in git('-C',
		os.path.join(root, 'repo.git'), 'branch',
		'--format=%(refname:short)').split()
		if name.startswith('autoopt/') and name[len('autoopt/'):].isdigit()}
	aside = {int(name.split('.', 1)[0]) for name in os.listdir(
		os.path.join(root, 'iterations')) if '.' in name
		and name.split('.', 1)[0].isdigit()}

	return branches, (set(iteration_ids(root)) | branches | aside
		| pending_briefs(root))


def new(args):
	root = resolve(args.root)
	config = load_config(root)

	if args.brief is not None:
		given = [flag for flag in ('id', 'parent', 'title', 'category', 'topic',
			'predict', 'predict_basis', 'idea_key')
			if getattr(args, flag, None) is not None] + (['control']
				if args.control else [])
		if given:
			fail('--brief takes its arguments from the brief; drop {}'.format(
				', '.join('--' + flag.replace('_', '-') for flag in given)))
		number = to_id(root, args.brief, '--brief')
		try:
			record = read_json(os.path.join(root, 'briefs', '{}.json'.format(
				number)))
		except OSError:
			fail('no briefs/{}.json: `brief` writes it with the brief'.format(
				number))
		args.id = to_id(root, record['id'], '--brief')
		args.parent = to_id(root, record['parent'], '--parent')
		for key in ('title', 'category', 'topic', 'predict',
				'predict_basis', 'idea_key'):
			setattr(args, key, record.get(key))
		args.control = bool(record.get('control'))
		args.after_end = bool(record.get('after_end')) or getattr(args,
			'after_end', False)

	after_end = bool(getattr(args, 'after_end', False))
	require_open(root, 'fork an iteration', after_end)

	if not args.title or not args.category:
		fail('new needs --title and --category, or --brief')

	if args.category not in CATEGORIES:
		fail('--category must be one of {}'.format(', '.join(CATEGORIES)))

	require_spec(root, 'fork an iteration')

	with Lock(root):
		state = load_state(root)
		existing = iteration_ids(root)

		bare = os.path.join(root, 'repo.git')
		branches, taken = taken_ids(root)

		identifier = args.id if args.id is not None else (
			max(taken) + 1 if taken else 0)

		if identifier in existing:
			fail('iteration {} already exists at {}'.format(identifier,
				iteration_dir(root, identifier)))

		parent = args.parent if args.parent is not None else state['root']

		check_fork(root, config, state, parent)

		target = iteration_dir(root, identifier)
		checkout = checkout_dir(root, identifier, config)

		if subprocess.run(['git', '-C', os.path.join(root, 'repo.git'),
			'rev-parse', '--verify', '-q', 'autoopt/{}^{{commit}}'.format(parent)],
			capture_output=True).returncode != 0:
			fail('{} has no commit to fork from yet: run it first{}'.format(
				label(root, parent), ' (the baseline, step 2: '
				'references/step2-baseline.md)' if parent == 0 else ''))

		if identifier in branches:
			fail('a branch named autoopt/{} already exists in {}, left by an '
				'iteration that was moved aside; pick another --id, or omit '
				'it to take the next free one'.format(identifier, bare))

		if checkout != target:
			os.makedirs(target)

		try:
			git('-C', bare, 'worktree', 'add', '-q', '-b',
				'autoopt/{}'.format(identifier), checkout,
				'autoopt/{}'.format(parent))
		except RuntimeError as error:
			if checkout != target:
				os.rmdir(target)
			fail('could not create iteration {}: {}'.format(identifier,
				error))

		meta = {'id': identifier, 'parent': parent,
			'round': state['round'], 'title': args.title,
			'category': args.category, 'topic': args.topic or '',
			'branch': 'autoopt/{}'.format(identifier), 'created': now(),
			# The idea miner's last finished stage: the dashboard draws a
			# stage boundary where it changes.
			'mining_stage': (state.get('mining') or {}).get('done', 0)}
		if args.predict is not None:
			meta['prediction'] = {'delta': args.predict,
				'basis': args.predict_basis or ''}
		if args.control:
			meta['control'] = True
		if args.idea_key:
			meta['idea_key'] = args.idea_key
		if after_end:
			meta['after_end'] = True
		write_meta(root, identifier, meta)

	# The directory and nothing else, on either stream: callers take it with
	# `DIR=$(autoopt.py new ...)` or read the last line of the output. The
	# worktree is in the brief (`autoopt.py brief`).
	print(target)

	return 0


###
# brief: the prompt that starts a subagent
###

SPEC_ADDED = '## Rules added during the search'
SPEC_ADDED_PREAMBLE = ("Each with the date and the user's words, written "
	'before it is used.')


def spec_field(text, name):
	"""The answer on spec.md's `- <name>:` line, with its indented
	continuation lines joined; None when the line is absent."""

	lines = text.splitlines()
	prefix = '- {}:'.format(name)

	for index, line in enumerate(lines):
		if line.startswith(prefix):
			out = [line[len(prefix):].strip()]
			for follow in lines[index + 1:]:
				if follow.startswith('  ') and follow.strip():
					out.append(follow.strip())
				else:
					break
			return ' '.join(part for part in out if part)

	return None


def spec_added(text):
	"""Every line under *Rules added during the search*, verbatim."""

	if SPEC_ADDED not in text:
		return []

	body = text.split(SPEC_ADDED, 1)[1].split('\n## ', 1)[0]

	return [line.rstrip() for line in body.strip('\n').splitlines()
		if line.strip() and line.strip() != SPEC_ADDED_PREAMBLE]


def text_arg(value):
	"""A flag's text, or a file's contents when it starts with `@`."""

	value = (value or '').strip()

	if value.startswith('@'):
		try:
			with open(os.path.expanduser(value[1:])) as infile:
				return infile.read().strip()
		except OSError as error:
			fail('could not read {}: {}'.format(value[1:], error))

	return value


# `import numba`, `import numpy, numba as nb`, `from numba.core import ...`;
# not `import numbers`. POSIX classes, since git's ERE may lack \s and \b.
NUMBA_IMPORT = (r'^[[:space:]]*(import[[:space:]]+([^#]*[[:space:],])?numba'
	r'([[:space:].,]|$)|from[[:space:]]+numba([[:space:].]|$))')


def uses_numba(root, identifier):
	"""Whether any Python or Cython source on `autoopt/<identifier>` imports
	numba. Read from the branch, so a pruned checkout still answers."""

	found = subprocess.run(['git', '-C', os.path.join(root, 'repo.git'), 'grep',
		'-q', '-E', NUMBA_IMPORT, 'autoopt/{}'.format(identifier), '--', '*.py',
		'*.pyx'], capture_output=True)

	return found.returncode == 0


def stage_files(root, stage):
	"""The miner's stage file and its ledger entries' file."""

	folder = os.path.join(root, 'mining')

	return (os.path.join(folder, 'stage-{}.md'.format(stage)),
		os.path.join(folder, 'ledger-S{}.txt'.format(stage)))


def stage_append(root, stage):
	"""Append stage `stage`'s idea lines to ideas.md under its lock, and the
	entries of `mining/ledger-S<n>.txt`, when it exists, to the ledger. Both
	are checked before either is written, and a line already there is not
	appended again. Returns what it did, one line each."""

	stage_file, entries_file = stage_files(root, stage)

	try:
		with open(stage_file) as handle:
			ideas = [line.rstrip() for line in handle if IDEA_LINE.match(line)]
	except OSError:
		fail('{} does not exist: write the stage\'s idea lines there '
			'(references/idea-miner.md, *Writing them down*), then close the '
			'stage'.format(stage_file))

	if not ideas:
		fail('{} holds no idea line ("- [CATEGORY] where ..."): nothing to '
			'append'.format(stage_file))

	unknown = sorted({IDEA_LINE.match(line).group(0)[3:-1] for line in ideas}
		- set(CATEGORIES))

	if unknown:
		fail('{} files ideas under {}, which is no category ({}). Nothing was '
			'appended'.format(stage_file, ', '.join(unknown), ', '.join(
				categories_for(root))))

	has_entries = os.path.exists(entries_file)

	if has_entries:
		ledger_plan(root, entries_file)

	ideas_path = os.path.join(root, 'ideas.md')

	with open(ideas_path, 'a+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		handle.seek(0)
		text = handle.read()
		present = set(text.split('\n'))
		new = [line for line in ideas if line not in present]
		if new:
			handle.write(('' if not text or text.endswith('\n') else '\n')
				+ '\n'.join(new) + '\n')

	done = ['appended {} of the stage\'s {} idea line(s) to {}'.format(
		len(new), len(ideas), ideas_path)]

	if has_entries:
		done.append(ledger_append(root, entries_file, 'S{}'.format(stage)))

	return done


def mine(args):
	"""Open a stage of the idea miner and print its prompt, or, with
	`--done`, append the stage's ideas and ledger entries and close it. One
	stage is open at a time.

	Iterations created after a stage closes carry its number (`new` stamps
	`mining_stage`), which is where the dashboard draws a stage boundary.
	"""

	root = resolve(args.root)
	config = load_config(root)

	if not getattr(args, 'done', False):
		require_open(root, 'open an idea-miner stage')

	with Lock(root):
		state = load_state(root)
		mining = state.setdefault('mining', {'stage': 0, 'done': 0})

		if args.done:
			if mining['stage'] <= mining['done']:
				fail('no stage of the idea miner is open')
			for line in stage_append(root, mining['stage']):
				print(line)
			mining['done'] = mining['stage']
			mining['finished'] = now()
			save_state(root, state)
			untried, _ = idea_counts(root)
			print('stage S{} closed; ideas.md holds {} untried idea(s). '
				'Iterations created from now on carry S{}.'.format(
					mining['done'], untried, mining['done']))
			return 0

		require_interview(root, 'start the idea miner')

		if (not mining['stage'] and not args.without_profile
				and (config.get('checks') or {}).get('profile')
				and not os.path.exists(profile_path(root, 0))):
			fail('stage S1 prices its ideas against A0\'s profile, which '
				'does not exist yet ({}): the setup subagent writes it after '
				'the baseline. Start the miner once setup is done, or pass '
				'--without-profile to start it blind'.format(
					profile_path(root, 0)))

		if mining['stage'] > mining['done'] and not args.force:
			fail('stage S{} of the idea miner is still open (started {}): one '
				'miner runs at a time. If its subagent died, pass --force to '
				'open the next stage'.format(mining['stage'],
					mining.get('started', '?')))

		budget = (config.get('after') or {}).get('budget_stages')

		if budget and mining['done'] >= budget:
			fail('the budget is {} idea-miner stage(s) (after.budget_stages) '
				'and S{} has closed, so no further stage opens. Brief the {} '
				'untried idea(s) left; once none is left the search stops for '
				'`budget` (references/step3-rounds.md, *When to stop*). With '
				'the user\'s yes, raise it: `autoopt.py config <work> --set '
				'after.budget_stages=<more> --reason "<their words>"`'.format(
					budget, mining['done'], idea_counts(root)[0]))

		settled = sorted(r['id'] for r in all_results(root))
		seen = set(mining.get('seen') or [])
		fresh = [identifier for identifier in settled if identifier not in seen]
		mining['stage'] += 1
		mining['started'] = now()
		mining['seen'] = settled
		save_state(root, state)
		stage = mining['stage']

	with open(spec_path(root)) as infile:
		spec = infile.read()

	scripts = os.path.dirname(os.path.abspath(__file__))
	skill = os.path.dirname(scripts)
	work = os.path.dirname(root)
	reviewed = state.get('root', 0) or 0
	objective = config['objective']
	value, holder = search_frontier(root, all_results(root),
		objective['direction'])
	untried, tried = idea_counts(root)
	stage_file, entries_file = stage_files(root, stage)
	allowed = categories_for(root)

	lines = ['# The idea miner\'s prompt: stage S{}'.format(stage), '',
		'- `AO` (the scripts): `{}`'.format(scripts),
		'- `WORK` (holds `autooptimize/`): `{}`'.format(work),
		'- Code to review, read only: `{}` ({}, the root).'.format(
			checkout_dir(root, reviewed, config), label(root, reviewed)),
		'- Your stage file: `{}`. Your ledger entries, if any: `{}`. Ideas '
			'file: `{}`. Ledger: `{}`.'.format(stage_file, entries_file,
				os.path.join(root, 'ideas.md'),
				os.path.join(root, 'knowledge.md')),
		'- Kind of search: {}'.format(spec_field(spec, 'Kind of search')
			or '(not written)'),
		'- Objective: `{}`{}, {}. {}.'.format(objective['name'],
			' ({})'.format(objective['unit']) if objective.get('unit') else '',
			'maximized' if objective['direction'] == 'max' else 'minimized',
			standing(root, config, state)),
		'- Gates: {}.'.format('; '.join('`{} {} {}`{}'.format(g['name'],
			g['op'], g['threshold'], ' (tol {})'.format(g['tol'])
				if g.get('tol') is not None else '')
			for g in config['gates']) or 'none'),
		'- What one run does: {}'.format(spec_field(spec, 'Benchmark')
			if 'TODO' not in (spec_field(spec, 'Benchmark') or 'TODO')
			else 'not written yet: start from what the spec\'s task names'),
		'- Categories to file under: {}.'.format(', '.join(
			'`{}`'.format(c) for c in allowed)),
		'- ideas.md now holds {} untried and {} tried idea(s). One iteration '
			'runs at a time: write at least {} ideas, more when the code '
			'holds them.'.format(untried, tried, 2)]

	settled_lines = []
	chain = in_root(root, reviewed)

	for identifier in fresh if stage > 1 else []:
		if identifier == 0:
			continue
		meta = load_meta(root, identifier) or {}
		readme_path = os.path.join(iteration_dir(root, identifier), 'README.md')
		try:
			with open(readme_path) as handle:
				sections = markdown_sections(handle.read())
		except OSError:
			sections = {}
		settled_lines += ['', '### {}: {} [{}]'.format(label(root, identifier),
			meta.get('title'), meta.get('category')), '',
			'{}. README: `{}`.'.format(outcome(root, identifier, chain)[:1].upper()
				+ outcome(root, identifier, chain)[1:], readme_path), '',
			'What to try next: ' + (sections.get('What to try next')
				or '(no README yet)')]

	lines += ['', '## Rules from `{}`, quoted verbatim'.format(
			spec_path(root)), '',
		'- Keep rule: {}'.format(spec_field(spec, 'Keep rule')
			or '(none written)'),
		'- Off limits: {}'.format(spec_field(spec, 'Off limits')
			or '(none written)'),
		'- Thread counts timed, tracking inputs: {}'.format(spec_field(spec,
			'Thread counts timed, tracking inputs') or '(none written)'),
		'- Portability: {}'.format(spec_field(spec, 'Portability') or
			'every common OS (Linux, macOS, Windows): no line in spec.md, so '
			'the default')]

	added = spec_added(spec)
	if added:
		lines += ['- Rules added during the search:', '']
		lines += ['  ' + line for line in added]

	holder, held = nearest_profile(root, reviewed)
	lines += ['', '## The code ({}, `{}`)'.format(label(root, reviewed),
		checkout_dir(root, reviewed, config)), ''] + code_inventory(
			checkout_dir(root, reviewed, config), profile_files(held))
	if held:
		with open(held) as handle:
			shown = handle.read().strip().splitlines()
		lines += ['', '## The profile of {}{}'.format(label(root, holder),
			'' if holder == reviewed else ' (the root {} has none yet)'.format(
				label(root, reviewed))), '', 'Take *Gain* from these shares. '
			'`{}`:'.format(held), ''] + ['    ' + line for line in
				shown[2:2 + PROFILE_SHOWN]]
		if len(shown) > 2 + PROFILE_SHOWN:
			lines.append('    … {} more lines in the file'.format(
				len(shown) - 2 - PROFILE_SHOWN))

	if settled_lines:
		lines += ['', '## Settled since the last stage', '',
			'What became of each, and its README\'s *What to try next*. Read '
			'a README in full only for an idea you build on.'] + settled_lines

	lines += ['', 'When your stage file is written, close the stage with '
		'`python3 {} mine {} --done`: it appends your idea lines to ideas.md '
		'and your entries file to the ledger, each under its lock. Then report '
		'back exactly what `references/idea-miner.md`, *What to report back*, '
		'lists.'.format(os.path.join(scripts, 'autoopt.py'), work), '']

	text = '\n'.join(lines)
	folder = os.path.join(root, 'briefs')
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, 'miner-{}.md'.format(stage))

	with open(path, 'w') as out:
		out.write(text)

	print(spawn_prompt('You are the idea miner of an auto-optimize-code search, '
		'stage S{}.'.format(stage), [(path, 'your prompt'),
			(os.path.join(skill, 'references', 'idea-miner.md'), 'the protocol '
				'you follow'),
			(os.path.join(root, 'knowledge.md'), 'the ledger'),
			(os.path.join(root, 'ideas.md'), 'the ideas so far')],
		closing='Then follow your prompt and the protocol.', root=root), end='')
	sys.stdout.flush()
	sys.stderr.write(spawn_line('Idea miner: stage S{}'.format(stage), config,
		model=False))
	return 0


###
# setup: step 2, handed to a subagent
###

def setup_missing(root, config):
	"""What step 2 has not done yet, one line each: the checks `setup
	--done` makes before it closes."""

	missing = []

	if not os.environ.get('AUTOOPT_SKIP_SPEC_CHECK'):
		missing += ['spec.md: ' + problem for problem in spec_problems(root)]

	baseline = load_result(root, 0)

	if baseline is None:
		missing.append('A0 has no result.json: run the baseline')
	elif baseline.get('status') != 'ok' or baseline.get('stage') != 'confirmed':
		missing.append('A0 is {} ({}), not ok with its full count of runs'.format(
			baseline.get('status'), baseline.get('stage')))

	if not os.path.exists(os.path.join(iteration_dir(root, 0), 'README.md')):
		missing.append('A0 has no README.md: autoopt.py finish --id A0')

	try:
		with open(os.path.join(root, 'knowledge.md')) as handle:
			entries = [line for line in handle if line.startswith('- [')]
	except OSError:
		entries = []

	if not entries:
		missing.append('knowledge.md has no entry: seed it after the baseline '
			'(autoopt.py finish --id A0 --ledger setup/ledger.txt)')

	wanted = config.get('workers')
	seen = (baseline or {}).get('workers') or {}

	if wanted and baseline is not None and seen.get('busy_peak', 0) < wanted:
		missing.append('the task names {} workers and A0\'s runs never had '
			'more than {} thread(s) running at once ({} sample(s)): the worker '
			'count is not in effect (a launcher that did not start, a flag '
			'the build ignores). Fix the benchmark and run A0 again '
			'(`--rerun`), or correct `workers` with the user\'s word'.format(
			wanted, seen.get('busy_peak', 0), seen.get('samples', 0)))

	if config.get('repo') and config.get('gpu', True) and not config.get(
			'gpu_command'):
		missing.append('no gpu_command: `end` takes its GPU lease through '
			'it, and so does anything run on this search afterwards (`config --set '
			'gpu_command="<the allocator> --"`; references/step2-baseline.md, '
			'*GPU leases*)')

	if algorithm_search(root):
		checks = config.get('checks') or {}
		for key in ('compare', 'time'):
			if not checks.get(key):
				missing.append('no checks.{} registered: an algorithm search '
					'writes the task\'s checks'.format(key))
		if checks.get('compare') and not os.path.exists(os.path.join(
				reference_dir(root), 'manifest.json')):
			missing.append('A0\'s compare outputs are not cached: autoopt.py '
				'check --reference')

	return missing


LOOP_HANDBACK = 16    # settled iterations an orchestrator subagent sees


def settled_count(root):
	"""Iterations other than A0 with a README: settled and written up."""

	return sum(1 for identifier in iteration_ids(root) if identifier and
		os.path.exists(os.path.join(iteration_dir(root, identifier),
			'README.md')))


def run_spread(root, identifier):
	"""The sample standard deviation of an iteration's run values, or None
	when it has fewer than two."""

	values = [v for v in (((load_result(root, identifier) or {}).get(
		'objective') or {}).get('values') or []) if v is not None]

	return statistics.stdev(values) if len(values) > 1 else None


def band_spread(root, state):
	"""The reference spread of the noise band: the current root's, as
	measured when the phase opened (`refresh_band`); before the first phase,
	and when the root has too few runs, the baseline's."""

	record = state.get('band') or {}

	if record.get('spread') is not None:
		return record['spread']

	return run_spread(root, 0) or 0.0


def refresh_band(root, state):
	"""Re-measure the band's reference at the start of a phase, from the
	root's own runs: after a large speedup the baseline's spread is far wider
	than the noise around the root. Returns `(old, new)` spreads."""

	old = band_spread(root, state)
	head = state.get('root', 0) or 0
	new = run_spread(root, head)

	if new is None:
		new = old

	state['band'] = {'id': head, 'spread': new, 'measured': now(),
		'phase': (state.get('loop') or {}).get('phase', 0)}

	return old, new


def loop_progress(root, config):
	"""The open orchestrator-subagent phase: `{phase, started, settled,
	handback, reached, in_flight}`, or None when none is open."""

	record = load_state(root).get('loop') or {}

	if not record.get('open'):
		return None

	settled = settled_count(root) - record.get('settled_at_start', 0)
	handback = config.get('loop_handback') or LOOP_HANDBACK
	in_flight = len(working(root)) + len(pending_briefs(root))

	return {'phase': record['phase'], 'started': record.get('started'),
		'settled': settled, 'handback': handback, 'reached': settled
		>= handback, 'in_flight': in_flight}


def loop(args):
	"""Open a phase of the orchestrator subagent, which runs step 3 for the
	main session, and print its spawn prompt;
	`--force` after one died; `--done` closes the open phase."""

	root = resolve(args.root)
	config = load_config(root)

	if not args.done:
		require_open(root, 'open a phase of the orchestrator subagent')

	with Lock(root):
		state = load_state(root)
		record = state.setdefault('loop', {'phase': 0})

		if args.done:
			if not record.get('open'):
				fail('no orchestrator-subagent phase is open')
			record['open'] = False
			record['finished'] = now()
			save_state(root, state)
			print('phase {} closed; report back as your prompt says'.format(
				record['phase']))
			return 0

		if record.get('open') and not args.force:
			fail('phase {} of the orchestrator subagent is open (since {}): '
				'one runs at a time. If its subagent died, pass --force'.format(
					record['phase'], record.get('started')))

		step2 = state.get('setup') or {}
		if step2 and step2.get('status') != 'done':
			fail('step 2 is still with the setup subagent: start the '
				'orchestrator subagent once `setup --done` has closed it')
		if step2 and not (state.get('mining') or {}).get('done'):
			fail('the idea miner\'s S1 is still running: start the '
				'orchestrator subagent once it has reported')

		record.update(phase=record.get('phase', 0) + 1, open=True,
			started=now(), settled_at_start=settled_count(root))
		was, now_spread = refresh_band(root, state)
		save_state(root, state)
		phase = record['phase']

	scripts = os.path.dirname(os.path.abspath(__file__))
	skill = os.path.dirname(scripts)
	work = os.path.dirname(root)
	autoopt_py = os.path.join(scripts, 'autoopt.py')
	handback = config.get('loop_handback') or LOOP_HANDBACK
	step = ('step 3, the rounds (`references/step3-rounds.md`, *The loop*)')
	lines = ['# The orchestrator subagent\'s prompt: phase {}'.format(phase), '',
		'- `AO` (the scripts): `{}`'.format(scripts),
		'- `WORK` (holds `autooptimize/`): `{}`'.format(work),
		'- Noise band: its reference spread was re-measured when this phase '
			'opened, from {}\'s runs: {} (before: {}). It holds for the phase.'
			.format(label(root, state['band']['id']), fmt(now_spread, 4),
				fmt(was, 4)),
		'- You run {} for the main session, which talks to the user. You '
			'cannot ask the user anything.'.format(step),
		'- Your first call, `status --resume`, says where the search is: act '
			'on each unfinished iteration it lists (`references/resume.md`, '
			'*Then act on each unfinished iteration*), then go on with the '
			'loop. Iterations still in flight from an earlier phase report to '
			'the main session, not to you; `brief --next` and the watch see '
			'them settle.',
		'', '## Hand back', '',
		'Report back (below) and stop when any of these happens. Whatever the '
		'reason, first wait for every iteration in flight to report (one '
		'still in flight when you stop would wake the main session), then '
		'`python3 {} loop {} --done`:'.format(autoopt_py, work), '',
		'- `brief --next` says to hand back: this phase has seen {} iterations '
			'settle. It then briefs nothing more, so wait for the ones in '
			'flight, which report to you, before stopping; one still in flight '
			'when you stop would wake the main session.'.format(handback),
		'- the search reaches *When to stop* (`references/step3-rounds.md`)'
			+ ('; `floor` is {}: check the root\'s median against it after '
			'every promotion'.format(fmt(config['floor'])) if config.get(
			'floor') else ''),
		'- a decision the skill gives the user that its least-damage default '
			'does not cover (`references/step3-rounds.md`, *Questions while '
			'the search runs*): take the default when there is one, and hand '
			'back only for one there is not',
		'- a message from the main session that says to stop.',
		'', '## Messages from the main session', '',
		'The main session may message you (SendMessage) with a rule or a '
		'setting the user gave. It has recorded it (`config --set ... '
		'--reason`, or *Rules added during the search* in `spec.md`): apply it '
		'from your next brief on, and say so in your report.',
		'', '## What to report back', '',
		'Exactly this, in this order:', '',
		'1. Why you stopped, in one line.',
		'2. The table `status --brief` prints now.',
		'3. Each round summary table `brief --next` told you to show the user, '
			'in the order shown.',
		'4. The decisions taken while the user was away during this phase '
			'(`ideas.md`), and any question for the user, each with the '
			'options and the one you recommend.', '']

	folder = os.path.join(root, 'briefs')
	os.makedirs(folder, exist_ok=True)
	path = os.path.join(folder, 'loop-{}.md'.format(phase))
	with open(path, 'w') as out:
		out.write('\n'.join(lines))

	print(spawn_prompt('You are the orchestrator of an auto-optimize-code search, '
		'phase {}.'.format(phase), [(path, 'your prompt'),
			(os.path.join(skill, 'SKILL.md'), 'the rules that hold everywhere'),
			(os.path.join(skill, 'references', 'step3-rounds.md'),
				'the loop you run')],
		'python3 {} status {} --resume'.format(shlex.quote(autoopt_py),
			shlex.quote(work)), 'Then follow your prompt and the loop.',
			root=root),
		end='')
	sys.stdout.flush()
	sys.stderr.write(spawn_line('Orchestrator: phase {}'.format(phase), config,
		model=False))
	return 0


SOURCE_EXTENSIONS = ('.py', '.pyx', '.pxd', '.c', '.cc', '.cpp', '.cxx', '.h',
	'.hh', '.hpp', '.hxx', '.cu', '.cuh', '.rs', '.go', '.java', '.jl', '.f',
	'.f90', '.R', '.r', '.m', '.js', '.ts')
VENDORED = ('third_party/', 'vendor/', 'external/', 'extern/', 'deps/')
INVENTORY_LINES = 120
PY_DEFINITION = re.compile(r'^(\s{0,4})(?:async\s+)?(def|class)\s+(\w+)')


def code_inventory(directory, first=()):
	"""Lines listing the tracked source files under `directory` (a git
	worktree, or any directory) with their line counts, and for Python each
	top-level function, class and method with its line number. Vendored
	directories are one line each. Files named in `first` (a profile's)
	come first, then the largest; past INVENTORY_LINES the rest are
	counted."""

	listed = subprocess.run(['git', '-C', directory, 'ls-files'],
		capture_output=True, text=True)
	if listed.returncode == 0:
		names = listed.stdout.splitlines()
	else:
		names = [os.path.relpath(os.path.join(base, name), directory)
			for base, folders, files in os.walk(directory)
			for name in files if '.git' not in base.split(os.sep)]

	vendored, files = {}, []
	for name in names:
		if not name.endswith(SOURCE_EXTENSIONS):
			continue
		vendor = next((prefix for prefix in VENDORED if name.startswith(prefix)
			or '/' + prefix in name), None)
		if vendor:
			key = name[:name.index(vendor) + len(vendor)]
			vendored[key] = vendored.get(key, 0) + 1
			continue
		try:
			with open(os.path.join(directory, name), errors='replace') as handle:
				text = handle.read().splitlines()
		except OSError:
			continue
		definitions, owner = [], None
		if name.endswith(('.py', '.pyx')):
			for number, line in enumerate(text, 1):
				found = PY_DEFINITION.match(line)
				if not found:
					continue
				indent, kind, word = found.groups()
				if not indent:
					owner = word if kind == 'class' else None
					definitions.append('{} {}'.format(word, number))
				elif owner and kind == 'def':
					definitions.append('{}.{} {}'.format(owner, word, number))
		files.append((name, len(text), definitions))

	wanted = set(first)
	files.sort(key=lambda item: (item[0] not in wanted
		and not any(item[0].endswith('/' + path) for path in wanted), -item[1]))
	lines = []
	for name, count, definitions in files:
		lines.append('- `{}` ({} lines){}'.format(name, count, ': ' + ', '.join(
			definitions) if definitions else ''))
	lines += ['- `{}` ({} vendored source files, not listed)'.format(key, count)
		for key, count in sorted(vendored.items())]

	if len(lines) > INVENTORY_LINES:
		lines = lines[:INVENTORY_LINES] + ['- … {} more files: `git -C {} '
			'ls-files`'.format(len(lines) - INVENTORY_LINES, directory)]

	return lines or ['- (no source files found)']


def profile_files(path):
	"""The file paths a profile names, in its order."""

	try:
		with open(path) as handle:
			text = handle.read()
	except (OSError, TypeError):
		return []

	return list(dict.fromkeys(IDEA_FILE.findall(text)))


def setup_prompt(root, config, state, continuing=None):
	"""The setup subagent's prompt; `continuing` is a line saying why a new
	subagent picks up where the last one stopped."""

	with open(spec_path(root)) as infile:
		spec = infile.read()

	scripts = os.path.dirname(os.path.abspath(__file__))
	skill = os.path.dirname(scripts)
	work = os.path.dirname(root)
	autoopt_py = os.path.join(scripts, 'autoopt.py')
	notes = os.path.join(root, 'setup', 'notes.md')
	objective = config['objective']
	tests = (config.get('tests') or {}).get('command')

	lines = ['# The setup subagent\'s prompt: step 2', '']

	if continuing:
		lines += [continuing, 'Read your notes, `{}`, before anything else: '
			'they say what is done.'.format(notes), '']

	lines += ['- `AO` (the scripts): `{}`'.format(scripts),
		'- `WORK` (holds `autooptimize/`): `{}`'.format(work),
		'- Mode: {}'.format('repository mode: the package is `{}`, and the '
			'benchmark goes in `{}`'.format(checkout_dir(root, 0, config),
				os.path.join(root, 'harness')) if config.get('repo') else
			'plain mode: the baseline worktree `{}` holds the code and the '
			'benchmark'.format(iteration_dir(root, 0))),
		'- Kind of search: {}'.format(spec_field(spec, 'Kind of search')
			or '(not written)'),
		'- The command `init` recorded, which the benchmark answers: '
			'`{}`'.format(config['command']),
		'- Unit tests: {}'.format('`{}`'.format(tests) if tests else 'none'),
		'- Objective: `{}`{}, {}.'.format(objective['name'], ' ({})'.format(
			objective['unit']) if objective.get('unit') else '', 'maximized'
			if objective['direction'] == 'max' else 'minimized'),
		'- Observers: {}.'.format(', '.join('`{}`'.format(o['name'])
			for o in config['observers']) or 'none'),
		'- Your notes, written as you go (each item done, each proof\'s '
			'numbers): `{}`'.format(notes),
		'',
		'## Rules from `{}`, quoted verbatim'.format(spec_path(root)), '']

	for field in ('Gates', 'Keep rule', 'Plan', 'Thread counts timed, '
			'tracking inputs', 'Machine and environment',
			'External validation', 'Off limits', 'Portability'):
		lines.append('- {}: {}'.format(field, spec_field(spec, field)
			or '(none written)'))

	added = spec_added(spec)
	if added:
		lines += ['- Rules added during the search:', '']
		lines += ['  ' + line for line in added]

	lines += ['', '## The code (A0, `{}`)'.format(checkout_dir(root, 0,
		config)), ''] + code_inventory(checkout_dir(root, 0, config))
	harness = os.path.join(root, 'harness')
	if config.get('repo') and os.path.isdir(harness):
		lines += ['', '## The harness (`{}`)'.format(harness), ''] + \
			code_inventory(harness)
	lines += ['', '## The subcommands step 2 uses (`python3 {} <name> '
		'--help` for the flags in full)'.format(autoopt_py), '']
	lines += ['- `{}`'.format(line) for line in usage_lines(('prove',
		'width', 'spec', 'config', 'busy', 'check', 'price', 'profile',
		'finish', 'setup', 'wait'))]

	lines += ['', '## Decisions that are the user\'s', '',
		'A subagent cannot ask the user. Which decisions are the user\'s, and '
		'how one is left (`python3 {0} setup {1} --ask "<question and '
		'options>" --recommend "<your answer>"`; every step that does not '
		'depend on it first, then one stop for all of them): '
		'`references/step2-baseline.md`, *Decisions that are the user\'s* and '
		'*The price of the base*.'.format(autoopt_py, work), '',
		'An answer comes back as a message, or in the prompt of a subagent '
		'that continues from your notes.', '',
		'## When step 2 is done', '',
		'Close it with `python3 {} setup {} --done`; it refuses, naming what '
		'is missing. Then report back exactly the list in `references/'
		'step2-baseline.md`, *Closing step 2*, and nothing more.'.format(
		autoopt_py, work), '']

	return '\n'.join(lines)


def record_setup_answer(root, question, answer, who):
	"""The dated line of a step-2 answer in spec.md, written here so nobody
	has to: the question, the answer, and whose it is."""

	with open(spec_path(root), 'a') as out:
		out.write('- {}: setup asked "{}"; {}: "{}"\n'.format(time.strftime(
			'%Y-%m-%d'), ' / '.join(question.splitlines()), who, answer))


def setup(args):
	"""Step 2 for a subagent: open it and print the setup subagent's prompt;
	`--ask` records a question for the user, `--answer` the reply (printing
	the prompt that continues), `--force` a prompt for a subagent that
	continues after one died, `--done` closes step 2 once everything it owes
	is on disk."""

	root = resolve(args.root)
	config = load_config(root)

	with Lock(root):
		state = load_state(root)
		record = state.get('setup')

		if args.ask or args.answer or args.done or args.force:
			if not record or record.get('status') == 'done':
				fail('no setup is open: `autoopt.py setup <work>` opens it')

		if args.ask:
			after = dict(AFTER_DEFAULTS, **(config.get('after') or {}))

			# The user said at step 0 to take recommended answers: nothing
			# waits, and the answer taken is listed when step 2 closes.
			if args.recommend and after.get('setup_questions') == \
					'recommended':
				record.setdefault('answers', []).append({'question': args.ask,
					'answer': args.recommend, 'on': now(), 'by': 'recommended'})
				save_state(root, state)
				record_setup_answer(root, args.ask, args.recommend, 'the '
					'recommended answer, taken under after.setup_questions')
				print('taken without asking (after.setup_questions is '
					'recommended) and written in spec.md: "{}". Go on'.format(
					args.recommend))
				return 0

			# One stop for every question: a second one joins the first.
			asked = [record['question']] if record.get('status') == 'waiting' \
				else []
			record.update(status='waiting', question='\n'.join(asked + [
				args.ask + (' (recommended: {})'.format(args.recommend) if
				args.recommend else '')]), asked=now())
			save_state(root, state)
			print('recorded the question ({} waiting). First do every step '
				'that does not depend on an answer and `--ask` what else '
				'those reach; then stop and report all of them to the '
				'orchestrator together'.format(len(asked) + 1))
			return 0

		if args.done:
			missing = setup_missing(root, config)
			if record.get('status') == 'waiting':
				missing.insert(0, 'a question waits for the user: {}'.format(
					record.get('question')))
			if missing:
				fail('step 2 is not finished:\n  ' + '\n  '.join(missing))
			record.update(status='done', finished=now())
			save_state(root, state)
			record_harness(root)
			print('step 2 is done: the orchestrator briefs the first iteration once '
				'the idea miner\'s S1 has reported')
			taken = [one for one in record.get('answers') or [] if one.get(
				'by') == 'recommended']

			if taken:
				print('answers taken without asking, to tell the user:\n' +
					'\n'.join('  - {} -> {}'.format(one['question'], one[
					'answer']) for one in taken))

			return 0

		if args.answer:
			if record.get('status') != 'waiting':
				fail('no question is waiting for an answer')
			record.setdefault('answers', []).append({'question':
				record.pop('question'), 'answer': args.answer, 'on': now()})
			record['status'] = 'open'
			save_state(root, state)
			last = record['answers'][-1]
			record_setup_answer(root, last['question'], last['answer'],
				'the user')
			continuing = ('The setup subagent before you asked the user: "{}". '
				'They answered: "{}". It is recorded in `spec.md`; go on from '
				'there.'.format(last['question'], last['answer']))
		elif args.force:
			continuing = ('The setup subagent before you stopped without '
				'reporting; go on from where its notes end.')
		else:
			require_interview(root, 'start step 2')
			if record and record.get('status') != 'done':
				fail('step 2 is already with a setup subagent (since {}); if '
					'it died, `setup --force` prints a prompt that continues '
					'from its notes'.format(record.get('started', '?')))
			if record and record.get('status') == 'done':
				fail('step 2 is done (since {})'.format(record.get('finished')))
			state['setup'] = {'status': 'open', 'started': now()}
			save_state(root, state)
			continuing = None

	os.makedirs(os.path.join(root, 'setup'), exist_ok=True)
	text = setup_prompt(root, config, load_state(root), continuing)
	folder = os.path.join(root, 'briefs')
	os.makedirs(folder, exist_ok=True)

	with open(os.path.join(folder, 'setup.md'), 'w') as out:
		out.write(text)

	skill = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
	reads = [(os.path.join(folder, 'setup.md'), 'your prompt'),
		(os.path.join(skill, 'references', 'step2-baseline.md'), 'the '
			'protocol, which you follow in order')]
	if continuing:
		reads.append((os.path.join(root, 'setup', 'notes.md'), 'the notes of '
			'the setup subagent before you: what is done'))
	prompt = spawn_prompt('You are the setup subagent of an auto-optimize-code '
		'search: you do step 2.', reads, closing='Then follow your prompt and '
		'the protocol.', root=root)

	if args.answer:
		print('Send this to the setup subagent that asked (SendMessage):\n\n'
			'The user answered your question ("{}"): "{}". It is recorded in '
			'spec.md; go on from where you stopped.\n\nIf it is gone, spawn a '
			'new subagent with this prompt instead:\n'.format(
				last['question'], last['answer']))
	print(prompt, end='')
	sys.stdout.flush()
	sys.stderr.write(spawn_line('Setup subagent: step 2', config))
	return 0


###
# step 2's measurements, for the setup subagent: the gate proof, the width,
# and the spec.md lines they answer
###

def spec_set(args):
	"""Replace the answer on one `- <field>:` line of spec.md, and its
	indented continuation lines."""

	root = resolve(args.root)
	path = spec_path(root)
	if len(args.set) == 1:
		field, separator, value = args.set[0].partition('=')

		if not separator:
			fail('--set takes FIELD VALUE or "FIELD=VALUE"; got {!r}'.format(
				args.set[0]))
	elif len(args.set) == 2:
		field, value = args.set
	else:
		fail('--set takes FIELD VALUE or "FIELD=VALUE"')

	with open(path) as handle:
		lines = handle.read().split('\n')

	prefix = '- {}:'.format(field)
	index = next((number for number, line in enumerate(lines)
		if line.startswith(prefix)), None)

	if index is None:
		fail('spec.md has no `{}` line'.format(prefix))

	end = index + 1
	while end < len(lines) and lines[end].startswith('  ') and lines[end].strip():
		end += 1

	lines[index:end] = ['{} {}'.format(prefix, ' '.join(value.split()))]

	with open(path, 'w') as handle:
		handle.write('\n'.join(lines))

	print(lines[index])
	return 0


def baseline_tree(root, config, target):
	"""A copy of A0's code without its `.git` pointer: what the gate proof
	plants faults in."""

	source = checkout_dir(root, 0, config)
	shutil.copytree(source, target, ignore=shutil.ignore_patterns('.git',
		'__pycache__', '*.pyc'))
	return target


def tree_changes(left, right):
	"""How many files differ between two trees, `.git` and caches aside."""

	def files(top):
		out = {}
		for base, directories, names in os.walk(top):
			directories[:] = [name for name in directories
				if name not in ('.git', '__pycache__')]
			for name in names:
				if not name.endswith('.pyc'):
					path = os.path.join(base, name)
					out[os.path.relpath(path, top)] = path
		return out

	a, b = files(left), files(right)
	changed = set(a) ^ set(b)

	for name in set(a) & set(b):
		with open(a[name], 'rb') as one, open(b[name], 'rb') as two:
			if one.read() != two.read():
				changed.add(name)

	return len(changed)


# `AUTOOPT_REF=<dir>` in a command: where the benchmark keeps the gate's
# reference (references/step2-baseline.md).
REFERENCE_ASSIGNMENT = re.compile(r"""AUTOOPT_REF=('[^']*'|"[^"]*"|\S+)""")


def start_run(config, cwd, src, iteration, out, seed, reference):
	"""Start one run of the search's command as the runner makes it, in the
	background, with the gate's reference in `reference` instead of the
	search's, in the environment and in the command; returns the process."""

	os.makedirs(out, exist_ok=True)
	# The runner's cache_dir() creates {cache}; a command that writes into it
	# (a build log, a lock) fails here unless it exists too.
	os.makedirs(os.path.join(out, 'cache'), exist_ok=True)
	line = fill(config['command'], seed=seed, out=out, out_abs=out,
		iteration=iteration, src=src, cache=os.path.join(out, 'cache'))
	line = REFERENCE_ASSIGNMENT.sub(lambda match: 'AUTOOPT_REF=' + shlex.quote(
		reference), line)
	env = dict(os.environ, AUTOOPT_REF=reference)
	log = open(os.path.join(out, 'log.txt'), 'w')
	log.write('$ {}\n\n'.format(line))
	log.flush()
	process = subprocess.Popen(line, shell=True, cwd=cwd, stdout=log,
		stderr=subprocess.STDOUT, env=env, start_new_session=True)
	process.log = log
	LIVE.add(process)
	return process


def finish_run(process, config, out):
	"""Wait for a run from start_run; `(record or None, error)`."""

	try:
		code = process.wait(timeout=config.get('timeout_seconds') or None)
	except subprocess.TimeoutExpired:
		kill_group(process)
		code = -9
	finally:
		LIVE.discard(process)
		process.log.close()

	import run_iteration  # here: it imports this module

	record, error = run_iteration.load_metrics(out, config)
	if code != 0:
		return None, 'exit {} (see {})'.format(code, os.path.join(out, 'log.txt'))
	return record, error


def prove(args):
	"""`--make NAMES` copies A0's code once per name into a new scratch
	directory under the search, for faults to be planted in. Without it,
	runs the benchmark on A0 (which writes the scratch reference) and on
	every copy, and the compare on each, and tabulates what each gate and
	the compare said."""

	root = resolve(args.root)
	config = load_config(root)
	work = os.path.dirname(root)

	name = 'prove-compare' if args.compare else 'prove-gates'
	if args.background:
		if args.id is not None or args.make:
			fail('--background is for the gate and compare proofs')
		return detach(os.path.abspath(__file__), sys.argv[1:], os.path.join(
			root, 'setup', name + '.log'))
	if args.id is None and not args.make:
		hold_lease(config)

		with StepLog(root, name):
			return prove_gates(root, config, args)

	if args.id is not None:
		if not (args.checker and args.checker_row and args.faults):
			fail('prove --id proves an iteration\'s checker: it takes '
				'--checker, --checker-row and --faults')
		return prove_checker(root, config, args)
	if args.checker or args.checker_row or args.faults:
		fail('--checker, --checker-row and --faults go with --id, the '
			'iteration whose checker they prove')

	return prove_gates(root, config, args)


def prove_gates(root, config, args):
	"""`prove` without --id: make the copies (--make), or run the gates and
	the compare on A0 and each copy."""

	work = os.path.dirname(root)

	if args.checker or args.checker_row or args.faults:
		fail('--checker, --checker-row and --faults go with --id, the '
			'iteration whose checker they prove')

	with Lock(root):
		state = load_state(root)
		record = state.setdefault('setup', {})
		folder = args.dir or record.get('proof')
		if not folder or not os.path.isdir(folder):
			if not args.make:
				fail('no gate proof yet: start one with `prove --make '
					'same,reorder,lossy`')
			folder = work_path(work, 'proof')
			os.makedirs(folder, exist_ok=True)
			record['proof'] = folder
			save_state(root, state)

	if args.make:
		names = [name.strip() for name in args.make.split(',') if name.strip()]
		taken = [name for name in names if os.path.exists(os.path.join(folder,
			name))]
		if taken:
			fail('{} already in {}: give new names'.format(', '.join(taken),
				folder))
		for name in names:
			baseline_tree(root, config, os.path.join(folder, name))
			print(os.path.join(folder, name))
		# Only these are copies: the folder is also the scratch reference,
		# where the benchmark may write folders of its own.
		with Lock(root):
			state = load_state(root)
			made = state.setdefault('setup', {}).setdefault('proof_copies', [])
			made += [name for name in names if name not in made]
			save_state(root, state)
		print('plant one fault in each copy but `same`; then `autoopt.py prove '
			'$WORK` runs them')
		return 0

	made = (load_state(root).get('setup') or {}).get('proof_copies')
	names = args.copies or ([name for name in made if os.path.isdir(
		os.path.join(folder, name))] if made else sorted(name for name in
		os.listdir(folder) if os.path.isdir(os.path.join(folder, name))
		and not name.startswith(('out-', 'compare-', 'base'))))
	base_tree = os.path.join(folder, 'base')
	if not os.path.isdir(base_tree):
		baseline_tree(root, config, base_tree)
	seed = config.get('seed_base', 0)
	zero = iteration_dir(root, 0)

	reference, error = finish_run(start_run(config, zero, checkout_dir(root, 0,
		config), zero, os.path.join(folder, 'out-base'), seed, folder), config,
		os.path.join(folder, 'out-base'))
	if reference is None:
		fail('A0 itself did not run: {}'.format(error))

	compare = args.compare or (config.get('checks') or {}).get('compare')
	cached = None
	if compare:
		cached = os.path.join(folder, 'compare-ref')
		os.makedirs(cached, exist_ok=True)
		wrote = run_compare(dict(config, checks=dict(config.get('checks')
			or {}, compare=compare)), base_tree, cached, os.path.join(folder,
			'compare-base.log'), folder, write=True)
		if wrote['verdict'] != 'pass' or not os.listdir(cached):
			fail('the compare could not cache A0\'s outputs ({}): see {}'.format(
				wrote['summary'], wrote['log']))

	rows = []
	# The unchanged copy first: when it does not pass, the base differs from
	# itself, and the fault copies would each cost a run to prove nothing.
	names = sorted(names, key=lambda name: name != 'same')
	for name in ['base'] + names:
		tree = base_tree if name == 'base' else os.path.join(folder, name)
		out = os.path.join(folder, 'out-' + name)
		if name == 'base':
			metrics, error = reference, None
		else:
			metrics, error = finish_run(start_run(config, tree, tree, tree, out,
				seed, folder), config, out)
		gates = ('crashed: ' + error) if metrics is None else ', '.join(
			'{} {} {}'.format(gate['name'], fmt(metrics['gates'][gate['name']]),
				'pass' if OPS[gate['op']](metrics['gates'][gate['name']],
					gate['threshold'], gate.get('tol')) else 'FAIL')
			for gate in config['gates']) or 'no gates'
		verdict = ''
		if compare:
			done = run_compare(dict(config, checks=dict(config.get('checks')
				or {}, compare=compare)), tree, cached, os.path.join(folder,
				'compare-{}.log'.format(name)), folder)
			verdict = '{}: {}'.format(done['verdict'], done['summary'])
		changed = tree_changes(base_tree, tree) if name != 'base' else 0
		landed = '' if name in ('base', 'same') or changed else \
			'  NO FILE CHANGED: the planted fault did not land'
		timed = ''
		if compare and (config.get('checks') or {}).get('time'):
			line = fill(config['checks']['time'], src=tree)
			line = REFERENCE_ASSIGNMENT.sub(lambda match: 'AUTOOPT_REF='
				+ shlex.quote(folder), line)
			with open(os.path.join(folder, 'time-{}.log'.format(name)),
					'w') as out:
				code, _ = run_shell(line, iteration_dir(root, 0), out, dict(
					os.environ, AUTOOPT_REF=folder), config.get(
						'timeout_seconds'))
			timed = '; time exit {}'.format(code)
		rows.append('{:<10} {} file(s) changed; {}{}{}{}'.format(name, changed,
			gates, '; compare ' + verdict if verdict else '', timed, landed))

		# A gate A0 itself fails says nothing about run-to-run variation.
		if name == 'same' and 'FAIL' not in rows[0] and ('FAIL' in gates or
				'crashed' in gates or (verdict and not verdict.startswith(
				'pass'))):
			for line in rows:
				print('  ' + line)
			fail('the unchanged copy `same` does not pass against A0, so the '
				'base differs from itself run to run and the gate would refuse '
				'every iteration. The fault copies were not run. Find what '
				'varies ({}), make the gate ignore or fix it (a seed, a sort, a '
				'comparator rule), then `prove` again (references/'
				'step2-baseline.md, *Prove the gate*)'.format(os.path.join(
				folder, 'out-same')))

	print('gate proof in {} (A0 wrote the scratch reference; seed {}):'.format(
		folder, seed))
	for line in rows:
		print('  ' + line)

	return 0


def width_verdict(table, minimize):
	"""`(the slowest run made alone, the tolerance, the largest supported
	count)` from `{N: [(median, slowest, load), ...]}`. The tolerance is the
	spread (range) of the runs made alone: the slowest of several runs is
	slower than one by chance, so only a median or a slowest run past the
	slowest run alone by more than that counts. Counts are taken in order
	until one does."""

	counts = sorted(table)
	solo = [slowest for _, slowest, _ in table[counts[0]]]
	tolerance = max(solo) - min(solo)
	alone = max(solo) if minimize else min(solo)
	limit = alone + tolerance if minimize else alone - tolerance
	slower = (lambda a: a > limit) if minimize else (lambda a: a < limit)
	supported = None

	for n in counts:
		if any(slower(median) or slower(slowest)
				for median, slowest, _ in table[n]):
			break
		supported = n

	return alone, tolerance, supported


def width(args):
	"""Step 2's concurrency measurement: the search's command on A0's code,
	N runs at once for each N, --reps times; the largest N at which neither
	the median nor the slowest run is slower than the slowest run made
	alone."""

	root = resolve(args.root)

	if args.background:
		return detach(os.path.abspath(__file__), sys.argv[1:], os.path.join(
			root, 'setup', 'width.log'))

	with StepLog(root, 'width'):
		return width_measure(root, args)


def width_measure(root, args):
	config = load_config(root)

	if config.get('gpu', True):
		fail('a GPU benchmark takes one run per GPU: write "one run per GPU" on '
			'*Width measured* (`autoopt.py spec $WORK --set "Width measured" '
			'"one run per GPU"`)')

	plan = config.get('gpus_per_agent') or 1
	counts = sorted({int(n) for n in args.counts.split(',')}) if args.counts \
		else sorted({1, plan, 2 * plan} | {2 ** k for k in range(8)
			if 2 ** k <= 2 * plan})
	folder = work_path(os.path.dirname(root), 'width')
	shutil.rmtree(folder, ignore_errors=True)
	os.makedirs(folder)
	zero = iteration_dir(root, 0)
	src = checkout_dir(root, 0, config)
	seed = config.get('seed_base', 0)
	minimize = config['objective']['direction'] == 'min'
	worst = max if minimize else min
	table = {}
	busy_at_start = ([] if os.environ.get('AUTOOPT_SKIP_QUIET_CHECK')
		else busy_processes(root, seconds=1.0))

	for rep in range(args.reps):
		for n in counts:
			load = os.getloadavg()[0]
			outs = [os.path.join(folder, 'r{}-n{}-{}'.format(rep, n, i))
				for i in range(n)]
			# A0's code writes the gate's reference; each run gets its own,
			# so no run reads another's half-written one.
			for out in outs:
				os.makedirs(os.path.join(out, 'reference'))
			started = [start_run(config, zero, src, zero, out, seed,
				os.path.join(out, 'reference')) for out in outs]
			values = []
			for process, out in zip(started, outs):
				metrics, error = finish_run(process, config, out)
				if metrics is None:
					fail('a run at {} at once failed: {}'.format(n, error))
				values.append(metrics['objective'])
			table.setdefault(n, []).append((statistics.median(values),
				worst(values), load))
			print('  repetition {}, {} at once: median {}, slowest {}, load '
				'{:.1f}'.format(rep + 1, n, fmt(statistics.median(values)),
					fmt(worst(values)), load), flush=True)

	alone, tolerance, supported = width_verdict(table, minimize)

	def span(values):
		low, high = min(values), max(values)
		return fmt(low) if low == high else '{}-{}'.format(fmt(low), fmt(high))

	print('\n| concurrent runs | median | slowest run | load |')
	print('|---|---|---|---|')
	for n in counts:
		print('| {} | {} | {} | {} |'.format(n, span([row[0] for row in table[n]]),
			span([row[1] for row in table[n]]), span([round(row[2], 1)
				for row in table[n]])))

	line = ('{} concurrent run(s) at most (the plan runs {} at once): above '
		'that the median or the slowest run passes the slowest run made alone, '
		'{}, by more than the runs made alone spread, {}. {} repetitions per '
		'count, measured {}, load {}'.format(supported, plan, fmt(alone),
			fmt(tolerance), args.reps, time.strftime('%Y-%m-%d'),
			span([round(row[2], 1) for rows in table.values() for row in rows])))
	if busy_at_start:
		line += ('; measured under load from {} process(es) outside the '
			'search, so measure it again once the machine is quiet'.format(
				len(busy_at_start)))
	print('\n' + line)
	print('runs in {}'.format(folder))

	if supported is None or supported < plan:
		print("narrower than the plan: a question for the user (`setup --ask`, "
			"references/step2-baseline.md, *Decisions that are the user's*)")

	if args.write:
		spec_set(argparse.Namespace(root=args.root, set=('Width measured',
			line)))

	return 0


###
# check: the task's checks, which the runner runs on each iteration
###
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
THREAD_VARIABLES = ('OMP_NUM_THREADS', 'NUMBA_NUM_THREADS', 'MKL_NUM_THREADS',
	'OPENBLAS_NUM_THREADS')

# Set when the compare check is to cache the outputs of the tree it is given
# into {reference} instead of comparing against them. Only `run_compare` with
# `write` sets it, and only on A0's code.
REFERENCE_ENV = 'AUTOOPT_WRITE_REFERENCE'


def export_tree(root, commit, target):
	"""`commit`'s tracked files in `target`, written once and then reused:
	what {src} names for a check. A tree, not a worktree, so nothing points
	back into repo.git."""

	if os.path.isdir(target) and os.listdir(target):
		return target

	os.makedirs(target, exist_ok=True)
	archive = subprocess.run(['git', '-C', os.path.join(root, 'repo.git'),
		'archive', commit], capture_output=True)

	if archive.returncode != 0:
		raise RuntimeError('git archive {} failed: {}'.format(commit[:12],
			archive.stderr.decode(errors='replace').strip()))

	subprocess.run(['tar', '-x', '-C', target], input=archive.stdout,
		check=True)
	return target


def reference_dir(root):
	return os.path.join(root, 'reference', 'checks')


def checks_fingerprint(root, config):
	"""What the cached outputs depend on: the compare command, the gates and
	every file under autooptimize/checks/. When any of them changes, the
	outputs are cached again from A0 before the next compare."""

	digest = hashlib.sha256(json.dumps([config['checks'].get('compare'),
		config['gates']], sort_keys=True).encode())
	folder = os.path.join(root, 'checks')
	paths = []

	for base, directories, names in os.walk(folder):
		directories[:] = [name for name in directories
			if name != '__pycache__']
		paths += [os.path.join(base, name) for name in names
			if not name.endswith('.pyc')]

	for path in sorted(paths):
		digest.update(os.path.relpath(path, folder).encode() + b'\0')
		with open(path, 'rb') as handle:
			digest.update(handle.read())

	return digest.hexdigest()[:16]


def run_compare(config, src, reference, log, cwd, write=False):
	"""compare on one tree against the outputs cached in `reference`: 'pass'
	on exit 0, 'differ' on 1, 'error' on anything else or a timeout, since a
	check that crashed has measured nothing. With `write`, the script caches
	`src`'s outputs there instead."""

	command = fill(config['checks']['compare'], src=src, reference=reference)
	env = dict(os.environ)
	env.pop(REFERENCE_ENV, None)

	if write:
		env[REFERENCE_ENV] = '1'

	os.makedirs(os.path.dirname(log), exist_ok=True)

	with open(log, 'w') as out:
		code, timed_out = run_shell(command, cwd, stdout=out, env=env,
			timeout=config.get('timeout_seconds'))

	with open(log, errors='replace') as infile:
		tail = [line.strip() for line in infile if line.strip()]

	verdict = ('pass' if code == 0 and not timed_out else 'differ'
		if code == 1 else 'error')

	return {'verdict': verdict, 'exit': code, 'timed_out': timed_out,
		'summary': (tail[-1] if tail else '')[:240], 'log': log}


def ensure_reference(root, config):
	"""The outputs every compare reads, cached from A0's commit when they are
	missing or what they depend on changed (checks_fingerprint). Returns the
	manifest.

	Only A0's code is ever cached, so a lost reference is rebuilt from the
	original, never from an iteration's own output. The new outputs are
	written beside the old, checked by comparing A0 against them, then
	swapped in; the old ones are kept. A flock serializes runners that start
	together after a change.
	"""

	folder = reference_dir(root)
	os.makedirs(folder, exist_ok=True)
	manifest_path = os.path.join(folder, 'manifest.json')
	outputs = os.path.join(folder, 'outputs')
	fingerprint = checks_fingerprint(root, config)

	with open(os.path.join(folder, '.lock'), 'w') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		manifest = read_json(manifest_path) if os.path.exists(
			manifest_path) else None

		if (manifest and manifest.get('fingerprint') == fingerprint
				and os.path.isdir(outputs)):
			return manifest

		commit = ((load_meta(root, 0) or {}).get('commit')
			or (load_result(root, 0) or {}).get('commit'))

		if not commit:
			fail('A0 has no commit, so there is nothing to cache the compare '
				'from: run the baseline first')

		stamp = '{}-{}'.format(time.strftime('%Y%m%d-%H%M%S'), os.getpid())
		fresh = os.path.join(folder, 'outputs.new-' + stamp)
		os.makedirs(fresh)
		tree = export_tree(root, commit, os.path.join(folder, 'trees',
			commit[:12]))
		wrote = run_compare(config, tree, fresh, os.path.join(folder,
			'write-{}.log'.format(stamp)), folder, write=True)

		if wrote['verdict'] != 'pass':
			fail('compare could not cache A0\'s outputs with {}=1 (exit {}): '
				'{}; see {}'.format(REFERENCE_ENV, wrote['exit'],
					wrote['summary'], wrote['log']))

		if not os.listdir(fresh):
			fail('compare exited 0 with {}=1 but wrote nothing into {}: it '
				'caches the outputs of {{src}} there'.format(REFERENCE_ENV,
					fresh))

		again = run_compare(config, tree, fresh, os.path.join(folder,
			'verify-{}.log'.format(stamp)), folder)

		if again['verdict'] != 'pass':
			fail('compare does not pass A0 against its own cached outputs '
				'({}: {}): they vary between processes, or the script ignores '
				'{}; see {}'.format(again['verdict'], again['summary'],
					REFERENCE_ENV, again['log']))

		if os.path.isdir(outputs):
			os.rename(outputs, os.path.join(folder, 'outputs.old-' + stamp))

		os.rename(fresh, outputs)
		replaced = (manifest or {}).get('fingerprint')
		manifest = {'fingerprint': fingerprint, 'commit': commit,
			'created': now(), 'summary': again['summary'],
			'replaced': replaced}
		write_json(manifest_path, manifest)
		print('  check: cached A0\'s compare outputs ({}){}'.format(
			again['summary'], '; the gates or the checks changed since {}'
				.format(replaced) if replaced else ''), flush=True)
		return manifest


def check_compare(root, config, identifier, commit, fresh=False):
	"""The compare on one iteration's code against A0's cached outputs,
	written to `iterations/<id>/check.json` and reused for the same commit
	and the same cached outputs unless `fresh`."""

	manifest = ensure_reference(root, config)
	directory = iteration_dir(root, identifier)
	path = os.path.join(directory, 'check.json')
	earlier = None if fresh or not os.path.exists(path) else read_json(path)

	if (earlier and earlier.get('commit') == commit and earlier.get(
			'reference') == manifest['fingerprint'] and 'compare' in earlier):
		return earlier

	folder = os.path.join(directory, 'check')
	tree = export_tree(root, commit, os.path.join(folder, commit[:12]))
	record = {'commit': commit, 'reference': manifest['fingerprint'],
		'compare': run_compare(config, tree, os.path.join(reference_dir(root),
			'outputs'), os.path.join(folder, 'compare.log'), directory),
		'created': now()}
	write_json(path, record)
	return record


def run_paired(config, a, b, log, threads=None):
	"""paired.py on the task's timing probe; at `threads`, every common
	thread-pool variable is set to it. A comparison that `after.
	pair_cap_hours` would not hold is `over cap`, and one of whose runs was
	killed at its time limit is `timed out`: the price or the run killed is
	the summary."""

	after = dict(AFTER_DEFAULTS, **(config.get('after') or {}))
	command = [sys.executable, os.path.join(SCRIPTS, 'paired.py'), '--a', a,
		'--b', b, '--cmd', config['checks']['time'], '--json', '--log', log]

	if after.get('pair_cap_hours'):
		command += ['--cap-hours', str(after['pair_cap_hours'])]

	for name in THREAD_VARIABLES if threads else ():
		command += ['--env', '{}={}'.format(name, threads)]

	done = subprocess.run(command, capture_output=True, text=True)

	if done.returncode != 0:
		return {'verdict': {3: 'over cap', 4: 'timed out'}.get(done.returncode,
			'error'),
			'threads': threads,
			'summary': (done.stderr or done.stdout).strip()[-240:]}

	data = json.loads(done.stdout)
	return {'verdict': data['verdict'], 'median': data['median_b_minus_a'],
		'pairs': data['pairs'], 'wins': data['b_better_in'], 'p': data['p'],
		'threads': threads}


def run_cold(config, src, folder, reps=2):
	"""cold.py on the task's first-call probe: `reps` runs of one version,
	each on an empty compiler cache of its own."""

	command = [sys.executable, os.path.join(SCRIPTS, 'cold.py'), '--side',
		'iteration=' + src, '--cmd', config['checks']['first_call'],
		'--scratch', os.path.join(folder, 'cold'), '--reps', str(reps),
		'--json']
	done = subprocess.run(command, capture_output=True, text=True)

	if done.returncode != 0:
		return {'verdict': 'error',
			'summary': (done.stderr or done.stdout).strip()[-240:]}

	# cold.py prints a line per repetition before the JSON.
	lines = done.stdout.splitlines()
	start = next((index for index, line in enumerate(lines)
		if line.startswith('{')), None)

	if start is None:
		return {'verdict': 'error', 'summary': done.stdout.strip()[-240:]}

	data = json.loads('\n'.join(lines[start:]))['iteration']
	return {'verdict': 'measured', 'seconds': data['median'],
		'runs': data['runs']}


def measure_cold(root, config, identifier, commit, parent=None, fresh=False):
	"""The cold first call of one iteration's code, before its timed runs,
	beside its parent's recorded value: the parent is never re-measured.
	Kept in `iterations/<id>/cold.json` for the same commit unless
	`fresh`."""

	folder = os.path.join(iteration_dir(root, identifier), 'check')
	path = os.path.join(iteration_dir(root, identifier), 'cold.json')
	earlier = None if fresh or not os.path.exists(path) else read_json(path)

	if earlier and earlier.get('commit') == commit:
		record = earlier
	else:
		tree = export_tree(root, commit, os.path.join(folder, commit[:12]))
		record = dict(run_cold(config, tree, folder), commit=commit)
		write_json(path, record)

	recorded = (((load_result(root, parent) or {}).get('check') or {}).get(
		'cold') or {}) if parent is not None else {}
	return dict(record, parent_seconds=recorded.get('seconds'))


def apply_check(result, record, screen):
	"""Copy a check record into result.json's `check`, and judge it: a
	compare that did not pass, or a cold first call that crashed, makes the
	iteration `gate-failed` with `failed_check` naming it. A
	by-hand re-check that now passes clears a failure only a check made;
	`screen` is runs_per_iteration, below which the status is `partial`."""

	result['check'] = {key: record[key] for key in ('cold', 'compare')
		if key in record}
	failed = None

	if (record.get('cold') or {}).get('verdict') == 'error':
		failed = 'cold'
	elif (record.get('compare') or {}).get('verdict') != 'pass':
		failed = 'compare'

	if failed and result.get('status') in ('ok', 'partial'):
		# A first call that crashes is a crash, as when the runner meets it
		# before any run.
		result['status'] = 'error' if failed == 'cold' else 'gate-failed'
		result['failed_check'] = failed
	elif not failed and result.get('failed_check'):
		result.pop('failed_check')
		result['status'] = 'ok' if result.get('n_runs', 0) >= screen or \
			result.get('split') else 'partial'

	return failed


def check_line(record):
	"""One line for the report block, the README and the orchestrator."""

	words = []
	cold = record.get('cold')

	if cold:
		words.append('cold first call {}'.format('{} s{}'.format(
			fmt(cold['seconds']), ' (parent {} s)'.format(fmt(cold[
				'parent_seconds'])) if cold.get('parent_seconds') is not None
			else '') if cold.get('seconds') is not None else cold['verdict']
			+ (' ({})'.format(cold['summary']) if cold.get('summary')
				else '')))

	if 'compare' in record:
		compare = record['compare']
		words.append('compare {}{}'.format(compare.get('verdict', '-'),
			' ({})'.format(compare['summary']) if compare.get('summary')
			else ''))

	return 'check: ' + '; '.join(words)


def check(args):
	"""The task's checks on one iteration by hand (`--id`), as the runner
	runs them, recorded in its result.json; or (`--reference`) cache A0's
	compare outputs now."""

	root = resolve(args.root)
	config = load_config(root)

	if not (config.get('checks') or {}).get('compare'):
		fail('this search has no checks: write them at step 2 and register '
			'them with `autoopt.py config --set checks.compare=…` '
			'(references/step2-baseline.md, *Write the checks*)')

	if args.reference:
		manifest = ensure_reference(root, config)
		print('A0\'s compare outputs (commit {}) are cached in {}: {}'.format(
			manifest['commit'][:10], os.path.join(reference_dir(root),
				'outputs'), manifest.get('summary')))
		return 0

	if args.id is None:
		fail('--id names the iteration to check (or pass --reference)')

	result = load_result(root, args.id)

	if not result or not result.get('commit'):
		fail('{} needs a result.json with a commit: run it with '
			'run_iteration.py first'.format(label(root, args.id)))

	record = check_compare(root, config, args.id, result['commit'],
		fresh=True)
	meta = load_meta(root, args.id) or {}

	if (config.get('checks') or {}).get('first_call') and not meta.get(
			'control'):
		record['cold'] = measure_cold(root, config, args.id, result['commit'],
			meta.get('parent'), fresh=True)

	# Recorded where the runner records it, since promote and status read
	# result.json.
	with Lock(root):
		result = load_result(root, args.id)
		failed = apply_check(result, record, config['runs_per_iteration'])
		write_json(os.path.join(iteration_dir(root, args.id), 'result.json'),
			result)

	print(check_line(record))
	return 1 if failed else 0


def final(args):
	"""Step 4's end: the winner against the original, A0, by paired timing
	(paired.py on the task's `time` probe), and again at `checks.threads`
	when it is set. The only paired timing a search runs. Written to
	`autooptimize/final.json` and printed."""

	root = resolve(args.root)

	if args.background:
		return detach(os.path.abspath(__file__), sys.argv[1:], os.path.join(
			root, 'final.log'), root)

	config = load_config(root)
	hold_lease(config)

	if args.json:
		import contextlib
		import io

		with contextlib.redirect_stdout(io.StringIO()):
			code = run_final(root, config, resolve_id(root, config, args.id))

		print(json.dumps(read_json(os.path.join(root, 'final.json')) if
			os.path.exists(os.path.join(root, 'final.json')) else None,
			indent='\t'))
		return code

	return run_final(root, config, resolve_id(root, config, args.id))


def run_final(root, config, winner):
	"""The paired timing of `winner` against A0, written to `final.json`:
	`final` and `end` both call it."""

	checks = config.get('checks') or {}

	if not checks.get('time'):
		fail('this search has no timing probe (config "checks.time"), so '
			'there is nothing to pair: quote the runner\'s medians')

	if winner == 0:
		fail('the root is still A0: nothing was kept, so there is nothing to '
			'pair against it')

	commits = {identifier: (load_result(root, identifier) or {}).get('commit')
		for identifier in (0, winner)}

	if None in commits.values():
		fail('A0 and {} both need a result.json with a commit'.format(
			label(root, winner)))

	folder = os.path.join(root, 'final')
	a = export_tree(root, commits[0], os.path.join(folder, commits[0][:12]))
	b = export_tree(root, commits[winner], os.path.join(folder,
		commits[winner][:12]))
	record = {'winner': winner, 'original': 0, 'created': now(),
		'paired': run_paired(config, a, b, os.path.join(folder, 'paired.log'))}

	# Not after a main comparison that was not made: it would cost as much.
	if checks.get('threads') and record['paired']['verdict'] not in ('error',
			'over cap', 'timed out'):
		record['threads'] = run_paired(config, a, b, os.path.join(folder,
			'threads.log'), threads=checks['threads'])

	write_json(os.path.join(root, 'final.json'), record)

	for key, name in (('paired', 'paired'), ('threads', 'at {} threads, '
			'paired'.format(checks.get('threads')))):
		entry = record.get(key)
		if entry:
			print('{} vs A0, {}: {}{}'.format(label(root, winner), name,
				entry['verdict'], ', {:+.3g}, {}/{}, p = {:.2g}'.format(
					entry['median'], entry['wins'], entry['pairs'], entry['p'])
				if 'median' in entry else ' ({})'.format(entry.get('summary'))))

	if record['paired']['verdict'] == 'over cap':
		print('the paired timing was not made: raise the cap (`config --set '
			'after.pair_cap_hours=H`), or point checks.time at a smaller input '
			'the user agrees to, then run this again')
	elif record['paired']['verdict'] == 'timed out':
		print('the paired timing stopped: one run hung and was killed (see '
			'the summary and {}); find why before running this again'.format(
			os.path.join(folder, 'paired.log')))

	return 0 if record['paired']['verdict'] not in ('error', 'over cap',
		'timed out') else 1


###
# end: the search ends on the record (references/step4-final.md)
###

ENDED = 'ended.json'

# The When-to-stop criteria (references/step3-rounds.md), by the word
# `end --reason` takes for each. Any other text is the user's own words.
REASONS = {
	'stalled': 'the root has not moved through a phase\'s worth of settled '
		'iterations (`loop_handback`)',
	'variations': 'the remaining ideas are variations of ones that failed',
	'budget': 'the compute spent has passed what the user agreed to',
	'noise': 'the frontier\'s improvements are smaller than the noise band',
	'floor': 'the root reached the smallest value the user called worth '
		'measuring (`floor`): this benchmark no longer resolves a gain',
}


def floor_reached(root, config, state):
	"""True when a minimized objective's root is at or under `floor`, the
	smallest value the user called worth measuring."""

	floor = config.get('floor')

	if not floor or config['objective'].get('direction') != 'min':
		return False

	median = ((load_result(root, state.get('root', 0)) or {}).get('objective')
		or {}).get('median')

	return median is not None and median <= floor


def ended_path(root):
	return os.path.join(root, ENDED)


def load_ended(root):
	"""`ended.json`, or None while the search has not ended."""

	path = ended_path(root)

	return read_json(path) if os.path.exists(path) else None


def require_ended(root, what):
	"""Refuse `what` until the search has ended: after_end reads ended.json."""

	if load_ended(root) is None:
		fail('the search has not ended, so it cannot {}: run `python3 {} end '
			'{}` first (references/step4-final.md)'.format(what,
			os.path.join(SCRIPTS, 'autoopt.py'), os.path.dirname(root)))


def require_open(root, what, after_end=False):
	"""Refuse `what` once the search has ended. An iteration made after
	the end (a notice, a fix, a clean-up) says so with `--after-end`."""

	ended = load_ended(root)

	if ended is not None and not after_end:
		fail('the search ended on {}, so it cannot {}: `end --reopen` '
			'reopens it (refused once the winner is published), and an iteration '
			'made after the end takes --after-end (references/step4-final.md)'
			.format(ended.get('ended_at'), what))


def settled_since_promotion(root, state):
	"""Settled iterations of the round still open, which never passed the
	keep rule."""

	number = state.get('round', 1)

	return [i for i in iteration_ids(root) if i and (load_meta(root, i)
		or {}).get('round') == number and load_result(root, i) is not None]


def end_reason(root, config, state, text):
	"""`{criterion, text}`: the When-to-stop criterion `--reason` names, the
	user's words, or, with none given, `stalled` when the open round holds a
	phase's worth of settled iterations."""

	if text and text.strip() in REASONS:
		return {'criterion': text.strip(), 'text': REASONS[text.strip()]}

	if text and text.strip():
		return {'criterion': 'user', 'text': text.strip()}

	if floor_reached(root, config, state):
		return {'criterion': 'floor', 'text': '{} (derived: the root is at {} '
			'and the floor is {})'.format(REASONS['floor'], fmt(load_result(
			root, state.get('root', 0))['objective']['median']), fmt(config[
			'floor']))}

	stages = stage_budget(root, config, state)

	if stages['exhausted']:
		return {'criterion': 'budget', 'text': '{} (derived: S{} closed '
			'against a budget of {} idea-miner stage(s), and every idea is '
			'tried or dropped)'.format(REASONS['budget'], stages['done'],
			stages['budget'])}

	handback = config.get('loop_handback') or LOOP_HANDBACK
	count = len(settled_since_promotion(root, state))

	if count >= handback:
		return {'criterion': 'stalled', 'text': '{} (derived: {} settled '
			'since the last promotion)'.format(REASONS['stalled'], count)}

	return {'criterion': None, 'text': 'none stated'}


def winning_iterations(root, config, winner):
	"""The winner's members that change the code, A0 excluded."""

	bare = os.path.join(root, 'repo.git')
	tree_of = lambda i: git('-C', bare, 'rev-parse',
		'autoopt/{}^{{tree}}'.format(i)).strip()
	repo = config.get('repo') or {}
	base = git('-C', bare, 'rev-parse', '{}^{{tree}}'.format(repo['commit'])
		).strip() if repo.get('commit') else tree_of(0)

	return [i for i, _ in winning_steps(root, lineage(root, winner),
		tree_of, base) if i != 0]


def winner_record(root, config, winner):
	"""What ended.json says of the winner, read from result.json."""

	result = load_result(root, winner) or {}
	meta = load_meta(root, winner) or {}

	return {'id': winner, 'label': label(root, winner),
		'title': result.get('title') or meta.get('title'),
		'category': category_of(dict(meta, id=winner)),
		'path': [label(root, i) for i in lineage(root, winner)],
		'median': (result.get('objective') or {}).get('median'),
		'gates': {name: {'op': gate.get('op'), 'threshold': gate.get('threshold'),
			'worst': gate.get('worst'), 'pass': gate.get('pass')}
			for name, gate in (result.get('gates') or {}).items()}}


def end_unsettled(root, state):
	"""Everything still in flight, as lines: a phase open, iterations
	running or finishing, briefs not yet started."""

	lines = []
	record = state.get('loop') or {}

	if record.get('open'):
		lines.append('phase {} of the orchestrator subagent is open: close it '
			'with `loop --done`'.format(record.get('phase')))

	for identifier, kind, idle in working(root):
		lines.append('{} is {} (last write {})'.format(label(root, identifier),
			kind, human_seconds(idle)))

	for identifier in sorted(pending_briefs(root)):
		lines.append('{} has a brief and no iteration yet: spawn it and let '
			'it settle, or withdraw it by deleting {}'.format(label(root,
			identifier), os.path.join(root, 'briefs', '{}.md'.format(
			identifier))))

	return lines


def run_dashboard(root, *flags):
	"""`(exit code, output)` of the dashboard's own CLI on this search,
	always naming it: a bare `--stop` would stop every session's
	publishers."""

	done = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'dashboard',
		'autoopt_monitor.py'), root] + list(flags), capture_output=True,
		text=True)

	return done.returncode, (done.stdout + done.stderr).strip()


def end_cleanup(root, config, winner):
	"""Delete the regenerable files, as `prune --worktrees` does, with the
	winner's path, every root and A0 protected.
	Records, `repo.git`, the harness, checks, reference and tools are never
	in the plan."""

	state = load_state(root)
	keep = set(known_lineage(root, winner)) | {0} | {entry['id'] for entry in
		state.get('roots', [])}
	running = {i for i in iteration_ids(root) if load_result(root, i) is None}
	plan = prune_plan(root, config, keep | running, running,
		keep_outputs=False, extensions=OUTPUT_EXTENSIONS, worktrees=True,
		extra=False)
	removed, freed, messages = prune_apply(root, plan)

	return {'files_removed': removed, 'bytes_freed': freed,
		'iterations_pruned': len(plan['targets']),
		'output_arrays_removed': len(plan['outputs']),
		'worktrees_removed': len(plan['worktrees']),
		'protected': sorted(keep), 'problems': messages}


def gain_line(config, first, last):
	"""`first -> last (+d, +p%)` in the objective's units."""

	unit = config['objective'].get('unit')
	text = '{} -> {}{}'.format(fmt(first), fmt(last), ' ' + unit if unit
		else '')

	if first is not None and last is not None:
		text += ' ({:+.4g}'.format(last - first)

		if first:
			text += ', {:+.1f}%'.format(100.0 * (last - first) / abs(first))

		text += ')'

	return text


def ended_markdown(root, config, ended):
	"""`ended.md`: the summary of what the search produced, every number
	read from result.json and final.json."""

	objective = config['objective']
	winner = ended['winner']
	zero = (load_result(root, 0) or {}).get('objective', {}).get('median')
	lines = ['# {}: the search ended'.format(config.get('task') or 'search'),
		'', 'Ended {}: {}.'.format(ended['ended_at'], ended['reason']['text']),
		'', '## Result', '',
		'- Objective `{}` ({}): the median goes from {} in A0 to {} in {}: '
		'{}.'.format(objective['name'], objective['direction'], fmt(zero),
		fmt(winner['median']), winner['label'], gain_line(config, zero,
		winner['median']))]

	gates = winner.get('gates') or {}

	if gates:
		lines.append('- Gates, worst run of {}: {}.'.format(winner['label'],
			'; '.join('`{} {} {}`: {} ({})'.format(name, g['op'], g['threshold'],
			'pass' if g['pass'] else 'FAIL', fmt(g['worst'])) for name, g in
			sorted(gates.items()))))

	paired = ended.get('paired')

	if paired and paired.get('paired'):
		entry = paired['paired']
		line = '- Paired, on the search\'s own input: {} vs A0, {}{}'.format(
			label(root, paired['winner']), entry['verdict'],
			', {:+.3g}, {}/{}, p = {:.2g}'.format(entry['median'],
			entry['wins'], entry['pairs'], entry['p']) if 'median' in entry
			else '')

		if paired.get('threads'):
			t = paired['threads']
			line += '; at {} threads, {}{}'.format(config['checks'].get(
				'threads'), t['verdict'], ', {:+.3g}, {}/{}, p = {:.2g}'
				.format(t['median'], t['wins'], t['pairs'], t['p'])
				if 'median' in t else '')

		lines.append(line + '.')
	else:
		lines.append('- No paired timing: the search has no timing probe '
			'(`checks.time`), so the medians are the result.')

	lines += ['- Every number here was measured inside the search, on its own '
		'input and machine.', '', '## Winning iterations, by category', '']
	by_category = {}

	for identifier in ended['winning_iterations']:
		result = load_result(root, identifier) or {}
		meta = load_meta(root, identifier) or {}
		parent = meta.get('parent')
		own = (result.get('objective') or {}).get('median')
		before = ((load_result(root, parent) or {}).get('objective')
			or {}).get('median') if parent is not None else None
		change = '{:+.4g}'.format(own - before) if own is not None and \
			before is not None else '?'
		by_category.setdefault(category_of(dict(meta, id=identifier)) or '?',
			[]).append('- {}: {} (median {} -> {}, {} from {})'.format(
			label(root, identifier), meta.get('title') or result.get('title')
			or '?', fmt(before), fmt(own), change, label(root, parent)
			if parent is not None else '?'))

	for name in sorted(by_category):
		lines += ['### {} ({})'.format(name, len(by_category[name]))] \
			+ by_category[name] + ['']

	counts = ended['counts']
	lines += ['{} rounds, {} iterations, {} winning iterations.'.format(
		counts['rounds'], counts['iterations'], counts['winning_iterations']),
		'']

	for change in ended.get('winner_changes') or []:
		lines.append('- Winner changed after the end: {} ({}).'.format(
			change['label'], change.get('title')))

	return '\n'.join(lines) + '\n'


def refresh_notes(root):
	"""Rewrite `$WORK/NOTES.md` from the state files (`stages.py`). Never
	fails the command that called it."""

	try:
		import stages
		stages.render(os.path.dirname(os.path.abspath(root)))
	except Exception:
		pass


def write_ended(root, config, ended):
	write_json(ended_path(root), ended)
	refresh_notes(root)

	with open(os.path.join(root, 'ended.md'), 'w') as out:
		out.write(ended_markdown(root, config, ended))


def end(args):
	"""Step 4: end the search on the record (`references/step4-final.md`).
	Refuses while anything is in flight, runs `final` and the last round's
	summary, writes `ended.json` and `ended.md`, deletes the regenerable
	files, snapshots this search's dashboard, and names the next
	step. `--reopen` undoes the ending, never the clean-up."""

	root = resolve(args.root)
	config = load_config(root)

	if args.reopen:
		return end_reopen(root, config)

	if args.background:
		return detach(os.path.abspath(__file__), sys.argv[1:], os.path.join(
			root, 'end.log'), root)

	if load_ended(root) is not None:
		fail('the search already ended on {}: `end --reopen` to reopen it'
			.format(load_ended(root).get('ended_at')))

	state = load_state(root)
	flying = end_unsettled(root, state)

	if flying:
		fail('the search cannot end while:\n  - {}'.format('\n  - '.join(
			flying)))

	winner = resolve_id(root, config, args.id if args.id is not None
		else 'best')

	if winner == 0:
		fail('the root is still A0: nothing was kept, so there is no winner '
			'to end on')

	reason = end_reason(root, config, state, args.reason)
	paired = None

	if (config.get('checks') or {}).get('time'):
		# After every refusal above, so an ending that will not happen does
		# not queue for a GPU first.
		hold_lease(config)
		code = run_final(root, config, winner)

		if code != 0:
			fail('final did not finish ({}): fix it and run `end` again; '
				'nothing was written or deleted'.format(code))

		paired = read_json(os.path.join(root, 'final.json'))

	round_number = state.get('round', 1)
	summary_path = None
	members = [i for i in iteration_ids(root) if i and (load_meta(root, i)
		or {}).get('round') == round_number]

	if members and all(os.path.exists(os.path.join(iteration_dir(root, i),
			'README.md')) for i in members):
		summary_path, lines = write_round_summary(root, config, round_number)

		if summary_path:
			print('wrote the round summary {}'.format(summary_path))
	elif members:
		print('round {} has iterations with no README ({}): its summary is '
			'not written'.format(round_number, ', '.join(label(root, i) for i
			in members if not os.path.exists(os.path.join(iteration_dir(root,
			i), 'README.md')))))

	results = all_results(root)
	winning = winning_iterations(root, config, winner)
	zero = (load_result(root, 0) or {}).get('objective', {}).get('median')
	record = winner_record(root, config, winner)
	ended = {'schema_version': SCHEMA_VERSION, 'ended_at': now(),
		'reason': reason, 'winner': record,
		'paired': paired, 'medians': {'A0': zero, 'winner': record['median'],
		'objective': config['objective']['name'], 'direction':
		config['objective']['direction'], 'unit': config['objective'].get(
		'unit')}, 'counts': {'rounds': round_number, 'iterations': len([
		r for r in results if r['id'] != 0]), 'winning_iterations':
		len(winning)}, 'winning_iterations': winning,
		'round_summary': summary_path, 'cleanup': None, 'dashboard': None,
		'winner_changes': []}

	# The record first: a failure below leaves a search that has ended.
	write_ended(root, config, ended)
	ended['cleanup'] = end_cleanup(root, config, winner)
	folder = os.path.join(root, 'final')
	os.makedirs(folder, exist_ok=True)
	snapshot = os.path.join(folder, 'dashboard.html')
	code, output = run_dashboard(root, '--export', snapshot)
	ended['dashboard'] = {'snapshot': snapshot if code == 0 else None,
		'snapshot_error': None if code == 0 else output[-300:],
		'publisher_stopped': False}
	write_ended(root, config, ended)

	with open(os.path.join(root, 'ended.md')) as handle:
		print(handle.read())

	cleanup = ended['cleanup']
	print('Cleaned up: {} file(s) and {} worktree(s) removed, {} freed (record '
		'in ended.json).'.format(cleanup['files_removed'],
		cleanup['worktrees_removed'], human(cleanup['bytes_freed'])))
	print('Dashboard: {}; the publisher keeps running until the finish '
		'step stops it.'.format('snapshot ' + snapshot
		if code == 0 else 'no snapshot ({})'.format(output[-120:])))

	print('\nNext: step 5, finishing (`references/step5-finish.md`).')

	return 0


def reopen_lock_path(root):
	return os.path.join(root, 'reopen-lock.json')


def reopen_holds(root):
	"""Why the ended search may no longer be reopened: the `holds` of
	`reopen-lock.json`, each `{"by", "reason"}`. Whatever published the
	winner writes it (`hold_reopen`); [] with no file."""

	try:
		with open(reopen_lock_path(root)) as handle:
			return list(json.load(handle).get('holds') or [])
	except (OSError, ValueError):
		return []


def hold_reopen(root, by, reason):
	"""Record that `by` published the winner, so `end --reopen` refuses;
	a `reason` of None withdraws `by`'s hold."""

	holds = [hold for hold in reopen_holds(root) if hold.get('by') != by]

	if reason:
		holds.append({'by': by, 'reason': reason, 'at': now()})

	if holds:
		write_json(reopen_lock_path(root), {'holds': holds})
	elif os.path.exists(reopen_lock_path(root)):
		os.remove(reopen_lock_path(root))


def end_reopen(root, config):
	"""Undo the ending: archive ended.json as `ended-<n>.json`, restart the
	publisher, and let `loop`, `brief` and `new` work again. Refused once a
	winner is published (`reopen-lock.json`). The clean-up is not undone
	(`restore --id`)."""

	ended = load_ended(root)

	if ended is None:
		fail('the search has not ended: there is nothing to reopen')

	held = reopen_holds(root)

	if held:
		fail('the search cannot be reopened: {}. Change the winner with a '
			'new iteration made with --after-end instead'.format('; '.join(
			'{} ({})'.format(hold.get('reason') or 'held', hold.get('by')
			or '?') for hold in held)))

	number = 1

	while os.path.exists(os.path.join(root, 'ended-{}.json'.format(number))):
		number += 1

	os.replace(ended_path(root), os.path.join(root, 'ended-{}.json'.format(
		number)))
	summary = os.path.join(root, 'ended.md')

	if os.path.exists(summary):
		os.replace(summary, os.path.join(root, 'ended-{}.md'.format(number)))

	code, output = run_dashboard(root, '--start')
	print('reopened: ended.json is now ended-{}.json; the publisher {}. The '
		'clean-up is not undone: `restore --id` brings a checkout back.'.format(
		number, 'is running' if code == 0 else 'did not start ({})'.format(
		output[-120:])))

	return 0


def record_winner_change(root, config, identifier):
	"""A promoted iteration made after the end becomes the winner:
	appended to `winner_changes`, and ended.json's winner and medians are read
	again."""

	ended = load_ended(root)
	meta = load_meta(root, identifier) or {}
	ended.setdefault('winner_changes', []).append({'id': identifier,
		'label': label(root, identifier), 'title': meta.get('title'),
		'previous': ended['winner']['id'], 'promoted_at': now()})
	ended['winner'] = winner_record(root, config, identifier)
	ended['medians']['winner'] = ended['winner']['median']
	ended['winning_iterations'] = winning_iterations(root, config, identifier)
	ended['counts']['winning_iterations'] = len(ended['winning_iterations'])
	write_ended(root, config, ended)


NUMBER_WORDS = ('no', 'one', 'two', 'three', 'four', 'five', 'six')


TRAILER_SENTENCE = ('End every commit message with the trailer line your '
	'spawner gave you, verbatim; if none was given, copy the last line of the '
	'parent commit\'s message; never write your own model name.')


def commit_trailer(root):
	"""`after.commit_trailer`, the line every commit message ends with, or
	None."""

	try:
		config = read_json(os.path.join(root, 'config.json'))
	except (OSError, ValueError):
		return None

	return ((config.get('after') or {}).get('commit_trailer') or '').strip() \
		or None


def with_trailer(text, trailer):
	"""`text` ending with the line `trailer` (after a blank line), unless it
	already does."""

	if not trailer:
		return text

	lines = text.rstrip('\n').split('\n')

	if lines[-1].strip() == trailer:
		return text.rstrip('\n') + '\n'

	return text.rstrip('\n') + '\n\n' + trailer + '\n'


def trailer_problems(text, trailer):
	"""What is wrong with a message's trailer under `after.commit_trailer`."""

	if not trailer:
		return []

	lines = [line.strip() for line in text.rstrip('\n').split('\n')]
	other = [line for line in lines if line.lower().startswith(
		'co-authored-by:') and line != trailer]

	return (['its last line is not after.commit_trailer `{}`'.format(trailer)]
		if lines[-1] != trailer else []) + ['it holds another trailer: '
		'`{}`'.format(line) for line in other]


def spawn_prompt(opening, reads, command=None, closing=None, root=None):
	"""The short prompt an Agent call carries: who the subagent is, and the
	files to read, with at most one command, all in its first turn. The long
	prompt is one of the files, so it never passes through the
	orchestrator's context. Every one ends with the trailer sentence, and the
	search's recorded trailer when `root` has one."""

	count = len(reads) + (1 if command else 0)
	lines = ['{} In your first turn, make these {} calls together:'.format(
		opening, NUMBER_WORDS[count]), '']

	for number, (path, what) in enumerate(reads, 1):
		lines.append('{}. Read `{}`: {}.'.format(number, path, what))

	if command:
		lines.append('{}. Bash: `{}`'.format(count, command))

	if closing:
		lines += ['', closing]

	trailer = commit_trailer(root) if root else None
	lines += ['', TRAILER_SENTENCE + (' This search records it: `{}`.'.format(
		trailer) if trailer else '')]

	return '\n'.join(lines) + '\n'


# What a subagent taking over an iteration is told, by where the last one
# stopped (references/step3-rounds.md, *A subagent that goes quiet*).
TAKEOVER_UNMEASURED = ('Finish the edit in `SRC` if it is incomplete, then go '
	'on from step 3 of *The sequence*.')
TAKEOVER_MEASURED = ('This iteration is measured: go to step 5 of *The '
	'sequence* and write `notes.md` from its files.')


def takeover_line(root, identifier):
	"""The line for a subagent taking over an existing iteration: measured
	(a result with its full count) or not."""

	result = load_result(root, identifier)

	if result is not None and result.get('status') != 'partial':
		return TAKEOVER_MEASURED

	return TAKEOVER_UNMEASURED


def iteration_prompt(root, identifier, exists=False):
	"""The spawn prompt for the subagent that runs one iteration from its
	brief, `briefs/<id>.md`; with `exists`, for one that takes over an
	iteration already created, with the line for where the last one
	stopped."""

	scripts = os.path.dirname(os.path.abspath(__file__))
	skill = os.path.dirname(scripts)
	work = os.path.dirname(root)
	tools = ' '.join(shlex.quote(os.path.join(root, *parts)) for parts in (
		('tools', 'README.md'), ('tools', 'checkers', 'README.md')))
	command = 'cat {}{}'.format(tools, '' if exists else '; python3 {} new {} '
		'--brief {}'.format(shlex.quote(os.path.join(scripts, 'autoopt.py')),
			shlex.quote(work), identifier))

	return spawn_prompt('You are running iteration {} of an auto-optimize-code '
		'search.{}'.format(label(root, identifier), ' It exists already: do '
			'not run `new`. {}'.format(takeover_line(root, identifier))
			if exists else ''), [
		(os.path.join(root, 'briefs', '{}.md'.format(identifier)), 'your brief'),
		(os.path.join(skill, 'references', 'subagent.md'), 'the protocol you '
			'follow'),
		(os.path.join(root, 'knowledge.md'), 'the ledger; a premise it settled '
			'is not measured again')], command,
		'Then follow the brief and the protocol, and report back exactly what '
		'the protocol\'s *What to report back* lists.', root=root)


def spawn_line(description, config, model=True):
	"""The stderr line beside a spawn prompt: how to make the Agent call."""

	return 'spawn with the Agent tool: description "{}"{}\n'.format(
		description, ', model {}'.format(config.get('subagent_model') or 'opus')
		if model else ', the session\'s model')


# A runner call whose parent took longer than this goes in the background:
# the child may be slower, and the compare and the whole suite come on top.
RUNNER_FOREGROUND = 120


def runner_seconds(result):
	"""How long an iteration's runner call took, runs and unit tests
	together, from its result.json; None without one."""

	if not result or result.get('seconds') is None:
		return None

	return result['seconds'] + ((result.get('tests') or {}).get(
		'seconds_total') or 0) + (result.get('profile_seconds') or 0)


# `- [CATEGORY] where (file:function): what to change. Why: … (S<n>)`
IDEA_PARTS = re.compile(r'^- \[(?P<category>[A-Z_]+)\] (?P<where>.*?): '
	r'(?P<what>.*?)(?:\. Why:|$)')
IDEA_STAGE = re.compile(r'\(S(\d+)\)\s*$')
IDEA_FILE = re.compile(r'[\w./-]+\.[A-Za-z]\w*')
TITLE_LENGTH = 70


def idea_fields(line):
	"""`{category, where, what, file, files, function, stage}` from an
	untried ideas.md line; None for a line that is not one. `file` and
	`function` are the first that `where` names, `files` every file it
	names."""

	match = IDEA_PARTS.match(line.strip())
	if not match or not IDEA_LINE.match(line):
		return None

	where, what = match.group('where'), match.group('what').strip()
	found = IDEA_FILE.search(where)
	path = found.group(0) if found else None
	after = where[found.end():] if found else ''
	function = re.match(r':(\w+)', after)
	stage = IDEA_STAGE.search(line)

	return {'category': match.group('category'), 'where': where,
		'what': what, 'file': path,
		'files': list(dict.fromkeys(IDEA_FILE.findall(where))),
		'function': function.group(1) if function else None,
		'stage': int(stage.group(1)) if stage else 0}


def idea_title(fields):
	"""A title from an idea line: its function, then what it changes, cut at
	a word to fit the tables."""

	head = '{}: '.format(fields['function']) if fields['function'] else ''
	what = fields['what']
	cuts = [what.find(mark) for mark in (': ', '; ', ', ', ' (')]
	cuts = [cut for cut in cuts if cut >= 12]
	text = head + (what[:min(cuts)] if cuts else what)

	if len(text) <= TITLE_LENGTH:
		return text

	return text[:TITLE_LENGTH].rsplit(' ', 1)[0].rstrip(',;:') + '…'


def touched_files(root, config, identifier):
	"""The files an iteration changed against its parent: its branch's diff,
	or for one still in flight, its worktree's."""

	meta = load_meta(root, identifier) or {}
	parent = meta.get('parent')

	if parent is None:
		return set()

	bare = os.path.join(root, 'repo.git')

	if load_result(root, identifier) is None:
		checkout = checkout_dir(root, identifier, config)
		listed = subprocess.run(['git', '-C', checkout, 'diff', '--name-only',
			'autoopt/{}'.format(parent)], capture_output=True, text=True)
	else:
		listed = subprocess.run(['git', '-C', bare, 'diff', '--name-only',
			'autoopt/{}'.format(parent), 'autoopt/{}'.format(identifier)],
			capture_output=True, text=True)

	return set(listed.stdout.split()) if listed.returncode == 0 else set()


def outcome(root, identifier, chain):
	"""What became of an iteration, in words, from its records: its median
	against its parent's, then in the root (`chain`, from `in_root`), passed
	but not in the root, not kept and why, failed, or in flight."""

	result = load_result(root, identifier)

	if result is None:
		return 'in flight'

	meta = load_meta(root, identifier) or {}
	parent = load_result(root, meta.get('parent')) or {}
	median = (result.get('objective') or {}).get('median')
	against = (parent.get('objective') or {}).get('median')
	measured = '{} against {}\'s {}'.format(fmt(median), label(root,
		meta.get('parent')), fmt(against)) if median is not None else \
		'no median'

	if result.get('status') != 'ok':
		fate = '{}{}'.format(result.get('status'), ' ({})'.format(
			result['failed_check']) if result.get('failed_check') else '')
	elif identifier in chain:
		fate = 'in the root'
	elif (result.get('keep') or {}).get('verdict') == 'pass':
		fate = 'passed the keep rule but is not in the root'
	else:
		fate = 'not kept: {}'.format((result.get('keep') or {}).get('why')
			or 'inside the noise band')

	return '{}; {}'.format(measured, fate)


def names_file(touched, path):
	"""Whether `touched`, a path relative to the repository's top, is the
	file an idea line names: idea lines often give a bare filename or the
	tail of the path."""

	path = path[2:] if path.startswith('./') else path
	return touched == path or touched.endswith('/' + path)


def tried_on(root, config, state, paths):
	"""What `brief --next` writes under *Already tried on this code*: every
	iteration that changed one of `paths`, the files an idea line names, once
	each, with what became of it."""

	if not paths:
		return 'none recorded: the idea line names no file'

	chain = in_root(root, state['root'])
	lines = []

	for identifier in iteration_ids(root):
		meta = load_meta(root, identifier) or {}
		if identifier == 0 or meta.get('control'):
			continue
		if not any(names_file(touched, path) for touched in
				touched_files(root, config, identifier) for path in paths):
			continue

		lines.append('{} ({}): {}.'.format(label(root, identifier),
			meta.get('title'), outcome(root, identifier, chain)))

	if not lines:
		return 'none: no iteration has changed {}.'.format(' or '.join(
			'`{}`'.format(path) for path in paths))

	return ' '.join(lines)


def claimed_keys(root):
	"""The ideas.md lines (by key) already briefed and not yet settled."""

	keys = set()
	folder = os.path.join(root, 'briefs')

	for identifier in pending_briefs(root):
		try:
			keys.add(read_json(os.path.join(folder, '{}.json'.format(
				identifier))).get('idea_key'))
		except OSError:
			pass

	for identifier, _, _ in working(root):
		keys.add((load_meta(root, identifier) or {}).get('idea_key'))

	return keys - {None}


def next_ideas(root, count):
	"""Up to `count` untried, unclaimed ideas.md lines, `(number, line,
	fields)`: the latest miner stage first, top to bottom, and no two on the
	same file and function while another can be had."""

	try:
		with open(os.path.join(root, 'ideas.md')) as handle:
			lines = handle.read().split('\n')
	except OSError:
		return []

	claimed = claimed_keys(root)
	candidates, places = [], set()

	for number, line in enumerate(lines, 1):
		fields = idea_fields(line)
		if not fields:
			continue
		if idea_key(line) in claimed:
			# In flight: the next ones go elsewhere while they can.
			places.add((fields['file'], fields['function']))
		else:
			candidates.append((number, line, fields))

	candidates.sort(key=lambda item: (-item[2]['stage'], item[0]))
	chosen = []

	for spread in (True, False):
		for item in candidates:
			if len(chosen) == count:
				return chosen
			place = (item[2]['file'], item[2]['function'])
			if item in chosen or (spread and place in places):
				continue
			chosen.append(item)
			places.add(place)

	return chosen


def known_lineage(root, identifier):
	"""`lineage`, or the part of it that can be read: an ancestor moved
	aside (`iterations/<id>.abandoned`) ends the walk instead of failing."""

	chain, seen, current = [], set(), identifier

	while current is not None and current not in seen:
		seen.add(current)
		meta = load_meta(root, current)
		if meta is None:
			break
		chain.append(current)
		current = meta.get('parent')

	return list(reversed(chain))


def standing(root, config, state, results=None):
	"""Where the search stands, in a line: the root and its
	median, which the keep rule judges a child against, and, when an
	iteration outside the root's path holds a better median, that one as
	"best median, not in the root" with why."""

	results = all_results(root) if results is None else results
	value, holder = search_frontier(root, results,
		config['objective']['direction'])

	head = state.get('root', 0) or 0
	median = ((load_result(root, head) or {}).get('objective') or {}).get(
		'median')
	text = 'Root: {}, median {}, the keep rule\'s reference'.format(
		label(root, head), fmt(median))

	if holder is not None and holder not in known_lineage(root, head):
		keep = (load_result(root, holder) or {}).get('keep') or {}
		text += '; best median, not in the root: {}, {} ({})'.format(
			label(root, holder), fmt(value), 'kept out by the noise band'
			if keep.get('verdict') == 'fail' else 'passed the keep rule, '
			'not promoted' if keep.get('verdict') == 'pass' else 'not kept')

	return text


def free_slots(root, config):
	"""One slot minus the iterations still being worked on and the
	briefs not yet started. A STALLED one keeps its slot: it goes to a new
	subagent (`brief --prompt`), not a new idea."""

	return max(0, 1 - len(working(root)) - len(pending_briefs(root)))


def show_new_summaries(root):
	"""Print the table of each round summary written since the last
	`brief --next`, once."""

	folder = os.path.join(root, 'rounds')
	names = sorted(name for name in (os.listdir(folder) if os.path.isdir(
		folder) else []) if name.startswith('round-') and name.endswith('.md'))

	with Lock(root):
		state = load_state(root)
		# With no record yet, every summary on disk counts as shown.
		first = 'summaries_shown' not in state
		shown = set(state.get('summaries_shown') or [])
		fresh = [name for name in names if name not in shown]
		if fresh or first:
			state['summaries_shown'] = sorted(shown | set(fresh))
			save_state(root, state)
		if first:
			fresh = []

	for name in fresh:
		with open(os.path.join(folder, name)) as handle:
			text = handle.read()
		block = re.search(r'```\n(.*?)\n```', text, re.S)
		print('\n{}, written since the last brief: show the user this '
			'table\n{}'.format(os.path.join(folder, name), block.group(1)
				if block else text.strip()))


def brief_next(args):
	"""`brief --next`: status, any round summary written since the last
	call, then a brief and a spawn prompt for each free slot, from the top
	untried ideas, every field the record can give filled in."""

	root = resolve(args.root)
	config = load_config(root)
	require_open(root, 'brief the next iterations')

	status(argparse.Namespace(root=args.root, round=None, json=False,
		brief=True, resume=False, last=6))
	show_new_summaries(root)

	count = args.count if args.count is not None else free_slots(root, config)
	phase = loop_progress(root, config)
	# --replace rewrites briefs not yet spawned, oldest first, in place of
	# briefing new ids: they hold the slots.
	replacing = sorted(pending_briefs(root)) if args.replace else []
	if args.replace:
		if not replacing:
			fail('--replace: no brief is waiting to be spawned')
		count = min(args.count or 1, len(replacing))

	if phase and phase['reached']:
		print('\nbrief --next: this orchestrator subagent\'s phase {} has seen '
			'{} iterations settle, its {}: nothing more is briefed in it. {}'
			.format(phase['phase'], phase['settled'], phase['handback'],
				'Nothing is in flight: hand back now (your prompt, *Hand back*)'
				if not phase['in_flight'] else 'Hand back once the {} in flight '
				'have reported (your prompt, *Hand back*)'.format(
					phase['in_flight'])))
		return 0

	if args.count is not None and args.count <= 0:
		print('\nbrief --next: --count {}; nothing briefed'.format(args.count))
		return 0

	if count <= 0:
		print('\nbrief --next: no free slot (one iteration at a time); '
			'nothing briefed')
		return 0

	if count > 1 and args.gpus and re.match(r'^[\d,\s]+$', args.gpus):
		fail('--gpus {} names indices, which two iterations must not share: '
			'brief one slot at a time with its own, or give the command that '
			'allocates them'.format(args.gpus))

	if args.idea_line is not None:
		number, line = idea_line(root, args.idea_line)
		fields = idea_fields(line)
		if not fields:
			fail('line {} of ideas.md is not an untried idea line '
				'(`- [CATEGORY] where: what. …`)'.format(number))
		picks = [(number, line, fields)]
	else:
		picks = next_ideas(root, count)

	if not picks:
		stages = stage_budget(root, config)
		print('\nbrief --next: no untried idea left that is not already '
			'briefed; {}'.format('the budget of {} idea-miner stage(s) is '
			'spent, so the search stops for `budget` once the iterations in '
			'flight settle: hand back'.format(stages['budget'])
			if stages['spent'] else 'run `autoopt.py mine` if no miner runs'))
		return 0

	state = load_state(root)
	ledger_text = ''
	try:
		with open(os.path.join(root, 'knowledge.md')) as handle:
			ledger_text = handle.read()
	except OSError:
		pass

	for number, line, fields in picks:
		_, taken = taken_ids(root)
		one = argparse.Namespace(**vars(args))
		one.next = False
		one.id = replacing.pop(0) if replacing else (max(taken) + 1
			if taken else 1)
		one.idea_line = number
		one.title = args.title or idea_title(fields)
		one.category = args.category or fields['category']
		one.tried = args.tried or tried_on(root, config, state,
			fields['files'])
		stem = os.path.splitext(os.path.basename(fields['file']))[0] \
			if fields['file'] else None
		tags = (['code:' + stem] if stem and '[code:{}]'.format(stem)
			in ledger_text else []) + [tag for tag in ('harness', 'machine',
				'closed') if '[{}]'.format(tag) in ledger_text]
		one.sections = args.sections or ','.join(tags) or None
		print()
		code = brief(one)
		if code:
			return code

	# The status above was printed before these briefs existed, so it may not
	# have asked for a watch.
	if live_watch(root) is None:
		print('\nwatch: none running: start `autoopt.py watch <work>` in the '
			'background before ending the turn')

	return 0


def brief(args):
	"""Write `briefs/<id>.md`, everything the subagent that runs one
	iteration knows, and `briefs/<id>.json`, what `new --brief` creates it
	from; print the short prompt that has the subagent read the brief.
	Everything the search already records -- paths, the parent, the
	frontier, the gates, the keep rule and the other rules quoted from
	spec.md -- is filled in here, so no two briefs state one rule in
	different words. The orchestrator supplies only the idea, what was
	already tried, and the GPUs, and `--next` takes those from the records
	too."""

	if getattr(args, 'next', False):
		return brief_next(args)

	root = resolve(args.root)
	config = load_config(root)

	if not args.prompt:
		require_open(root, 'brief an iteration', bool(getattr(args, 'after_end',
			False)))

	if args.prompt:
		identifier = to_id(root, args.prompt, '--prompt')
		if not os.path.exists(os.path.join(root, 'briefs', '{}.md'.format(
				identifier))):
			fail('iteration {} has no brief'.format(label(root, identifier)))
		print(iteration_prompt(root, identifier, exists=identifier
			in iteration_ids(root)), end='')
		sys.stdout.flush()
		sys.stderr.write(spawn_line('{}: {}'.format(label(root, identifier),
			(load_meta(root, identifier) or {}).get('title') or 'take over'),
			config))
		return 0

	missing = [flag for flag, value in (('--id', args.id), ('--title',
		args.title), ('--category', args.category), ('--tried', args.tried))
		if value is None]
	if missing:
		fail('brief needs {}'.format(', '.join(missing)))

	stage = (load_state(root).get('setup') or {}).get('status')
	if stage not in (None, 'done'):
		fail('step 2 is still with the setup subagent ({}): nothing is briefed '
			'until `setup --done`'.format('waiting for the user' if stage
				== 'waiting' else 'running'))
	require_spec(root, 'brief an iteration')

	state = load_state(root)
	identifier = args.id

	branches, taken = taken_ids(root)
	pending = pending_briefs(root)

	if identifier in taken and not (args.replace and identifier in pending):
		fail('id {} is taken ({}); the next free id is {}{}'.format(
			identifier, 'a brief not yet started; pass --replace to rewrite it'
				if identifier in pending else 'an iteration, a branch, or a '
				'directory set aside', LABEL_LETTER, max(taken) + 1))

	parent = args.parent if args.parent is not None else state['root']
	check_fork(root, config, state, parent)

	idea, tried = text_arg(args.idea), text_arg(args.tried)
	key = None

	if args.idea_line is not None:
		_, line = idea_line(root, args.idea_line)
		key = idea_key(line)
		idea = idea or line[2:].strip()

	if not idea:
		fail('--idea is empty and no --idea-line gives it: what to change, '
			'where (file and function), why it might help, and each premise '
			'to confirm')

	if not tried:
		fail('--tried is empty: the iterations that already touched this '
			'code and what each measured, or "none"')

	if config.get('gpu', True) and not (args.gpus or '').strip():
		args.gpus = config.get('gpu_command') or ''

	if config.get('gpu', True) and not args.gpus.strip():
		fail('this search uses GPUs and records no `gpu_command`: pass --gpus '
			'with the {} index(es) this iteration holds (e.g. --gpus 2) or '
			'the command that allocates them on this machine'.format(
			config.get('gpus_per_agent', 1)))

	with open(spec_path(root)) as infile:
		spec = infile.read()

	keep = spec_field(spec, 'Keep rule')
	off = spec_field(spec, 'Off limits')
	bench_line = spec_field(spec, 'Benchmark')
	threads = spec_field(spec, 'Thread counts timed, tracking inputs')
	portability = spec_field(spec, 'Portability')
	added = spec_added(spec)

	scripts = os.path.dirname(os.path.abspath(__file__))
	skill = os.path.dirname(scripts)
	work = os.path.dirname(root)
	directory = iteration_dir(root, identifier)
	checkout = checkout_dir(root, identifier, config)
	me = '{}{}'.format(LABEL_LETTER, identifier)
	parent_label = label(root, parent)
	objective = config['objective']
	direction = 'maximized' if objective['direction'] == 'max' else 'minimized'
	unit = ' ({})'.format(objective['unit']) if objective.get('unit') else ''
	value, holder = search_frontier(root, all_results(root),
		objective['direction'])

	# What `new --brief <id>` creates the iteration with, so no command line
	# is copied by hand.
	creation = {'id': me, 'parent': parent_label, 'title': args.title,
		'category': args.category, 'topic': args.topic or None,
		'predict': args.predict, 'predict_basis': args.predict_basis
		if args.predict is not None else None, 'control': bool(args.control),
		'idea_key': key, 'after_end': bool(getattr(args, 'after_end', False))}

	lines = ['# The brief for iteration {}'.format(me), '',
		'- `AO` (the scripts): `{}`'.format(scripts),
		'- `WORK` (holds `autooptimize/`): `{}`'.format(work),
		'- `DIR` (your records): `{}`'.format(directory),
		'- `SRC` (your worktree, the only place you edit code): `{}`'.format(
			checkout),
		'- Iteration **{}**, parent **{}**, category **{}**{}.'.format(me,
			parent_label, args.category, ', topic {}'.format(args.topic)
				if args.topic else ''),
		'- Title: "{}"'.format(args.title)]

	if args.predict is not None:
		lines.append('- Prediction: {:+g} against the parent{}.'.format(
			args.predict, ', from "{}"'.format(args.predict_basis)
				if args.predict_basis else ''))

	note = profile_note(root, parent, idea)
	lines += ['', '## The idea', '', idea] + (['', note] if note else []) + ['',
		'## Already tried on this code', '', tried, '',
		'## Established facts', '',
		'The ledger, `{}`, and the tools and checkers beside it{}. Append what '
			'you establish (`references/subagent.md`, *Before you stop*).'
			.format(os.path.join(root, 'knowledge.md'),
				'; above all its entries tagged {}'.format(args.sections)
					if args.sections else ''), '',
		'## The measurement', '',
		'- Objective: `{}`{}, {}. {}.'.format(objective['name'], unit,
			direction, standing(root, config, state)),
		'- Gates, checked on every run; one violating run fails the '
			'iteration: {}.'.format('; '.join('`{} {} {}`{}'.format(g['name'],
				g['op'], g['threshold'], ' (tol {})'.format(g['tol'])
					if g.get('tol') is not None else '')
				for g in config['gates']) or 'none'),
		'- Observers to emit: {}.'.format(', '.join('`{}`'.format(o['name'])
			for o in config['observers']) or 'none'),
		'- Runs: {}{}; the runner makes them.'.format(
			config['runs_per_iteration'], ', confirming to {} if the screening '
				'median earns it'.format(config['confirm_runs'])
				if config.get('staged', True) else ' per iteration')]

	if bench_line and 'TODO' not in bench_line:
		lines.append('- What one run does (`spec.md`, *Benchmark*): {}'.format(
			bench_line))

	harness = os.path.join(root, 'harness')
	if config.get('repo') and os.path.isdir(harness):
		lines += ['- The harness (`{}`), off limits; a probe may import its '
			'helpers:'.format(harness)] + ['  ' + item for item in
				code_inventory(harness)[:30]]

	took = runner_seconds(load_result(root, parent))
	if took is not None:
		lines.append('- On {} the runner took {}: {}.'.format(parent_label,
			human_seconds(took), 'run it in the foreground' if took
			<= RUNNER_FOREGROUND else 'start it with `--background` and watch '
			'`probe/runner.log` with `autoopt.py wait` (`references/'
			'subagent.md`, *The sequence*, step 3)'))

	if config.get('tests'):
		lines.append('- Unit tests, run once after the first run passes its gates: '
			'`{}`. Fix the '
			'code, never the test.'.format(config['tests']['command']))

	if config.get('gpu', True) and re.match(r'^[\d,\s]+$', args.gpus):
		lines.append('- GPUs: {}. Prefix every command that runs the code '
			'(`run_iteration.py`, `paired.py`, `cold.py`, your probes) with '
			'`CUDA_VISIBLE_DEVICES=<those indices>`; never pick a device '
			'yourself.'.format(args.gpus.strip()))
	elif config.get('gpu', True):
		lines.append('- GPUs: allocated by `{0}`. Run `run_iteration.py` '
			'bare: it takes its lease through that command. Put the command '
			'before anything else that runs the code (`{0} python3 paired.py '
			'...`, `cold.py`, your probes); never pick a device yourself.'
			.format(args.gpus.strip()))
	else:
		lines.append('- This benchmark needs no GPU: run everything without '
			'`CUDA_VISIBLE_DEVICES`.')

	lines += ['', '## Rules from `{}`, quoted verbatim'.format(spec_path(root)),
		'',
		'- Keep rule: {}'.format(keep or '(none written)'),
		'- Off limits: {}'.format(off or '(none written)'),
		'- Thread counts timed, tracking inputs: {}'.format(threads
			or '(none written)'),
		'- Portability: {}'.format(portability or 'every common OS (Linux, '
			'macOS, Windows): no line in spec.md, so the default'),
		'- Toolchain: no change may rely on a compiler\'s decisions; answer '
			'in notes.md\'s `## Toolchain` (`references/subagent.md`, '
			'*Portability*), whatever the scope above says']

	if added:
		lines += ['- Rules added during the search:', '']
		lines += ['  ' + line for line in added]

	if (config.get('checks') or {}).get('compare'):
		lines += ['', '## Checks', '',
			'The runner runs the task\'s checks and reports them on its '
			'`check:` line, stopping at the first that fails: before the '
			'runs, the cold first call where the search has compiled code; '
			'after the first run, the unit tests and the compare against '
			'A0\'s cached outputs; after the other runs, when the tests ran '
			'only your changed modules\' files and your median passes the '
			'keep rule, the whole test suite. Do not time '
			'your version against {} yourself. You owe only the checks in '
			'`references/subagent.md`, *Checks beyond the runner*, table '
			'*Your own checks*.{}'.format(parent_label,
				' Also: ' + text_arg(args.checks) if args.checks else '')]
	else:
		lines += ['', '## Checks you owe beyond the runner', '',
			'The checks in `references/subagent.md`, *Checks beyond the '
			'runner*, table *Your own checks*. Do not time your version '
			'against your parent, {}, yourself.{}'.format(parent_label,
				' Also: ' + text_arg(args.checks) if args.checks else '')]

	if config.get('promote') == 'auto':
		lines += ['', '## Promotion', '',
			'When your iteration passes the keep rule and {} is still the '
			'root, the runner promotes it and prints a `keep:` line saying '
			'so; you promote nothing.'.format(parent_label)]

	validation = config.get('external_validation') or {}

	if validation.get('approved') and args.validate:
		lines += ['', 'Run the external validation on your iteration, '
			'alone and untimed: `python3 {} validate {} --id {} --out {}`.'.format(
				os.path.join(scripts, 'autoopt.py'), work, me,
				os.path.join(directory, 'validation'))]

	notes = [text_arg(note) for note in args.note or []]

	if config.get('repo'):
		notes.append('Repository mode: edit code only in `SRC`. The harness '
			'(`{}`) and the unit tests are off limits.'.format(
				os.path.join(root, 'harness')))

	if uses_numba(root, parent):
		notes.append('The code uses numba: before editing, read `{}`, '
			'*Numba*.'.format(os.path.join(skill, 'references',
				'algorithm.md')))

	if notes:
		lines += ['', '## Notes', ''] + ['- ' + note for note in notes]

	lines += ['', 'A well-measured "this is closed" is an acceptable result; '
		'bring the number that closes it.', '']

	text = '\n'.join(lines)
	folder = os.path.join(root, 'briefs')
	os.makedirs(folder, exist_ok=True)

	with open(os.path.join(folder, '{}.md'.format(identifier)), 'w') as out:
		out.write(text)

	write_json(os.path.join(folder, '{}.json'.format(identifier)), creation)

	print(iteration_prompt(root, identifier), end='')
	# Not part of the prompt: the orchestrator's instruction for the Agent
	# call that carries it.
	sys.stdout.flush()
	sys.stderr.write(spawn_line('{}: {}'.format(me, args.title), config))

	return 0


###
# promote, for tree mode
###

PROFILE_SHOWN = 60     # lines of a profile a prompt quotes


def profile_path(root, identifier):
	return os.path.join(iteration_dir(root, identifier), 'profile.txt')


def profile_root(root, config, identifier):
	"""Run `checks.profile` on an iteration's measured commit, exported (and
	built with `checks.build`) under `iterations/<id>/profile/`, and write
	what it prints to `iterations/<id>/profile.txt`. Returns `(path,
	seconds)`, or `(None, what went wrong)`."""

	command = (config.get('checks') or {}).get('profile')
	commit = ((load_result(root, identifier) or {}).get('commit')
		or (load_meta(root, identifier) or {}).get('commit'))

	if not command or not commit:
		return None, 'no checks.profile registered' if not command \
			else 'no measured commit'

	folder = os.path.join(iteration_dir(root, identifier), 'profile')
	tree = export_tree(root, commit, os.path.join(folder, commit[:12]))
	problem = build_code(config, tree)

	if problem:
		return None, problem

	line = fill(command, src=tree, out=folder)
	start = time.time()
	process = subprocess.Popen(line, shell=True, cwd=iteration_dir(root,
		identifier), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
		text=True, start_new_session=True)
	LIVE.add(process)

	try:
		out, err = process.communicate(timeout=config.get('timeout_seconds')
			or None)
	except subprocess.TimeoutExpired:
		kill_group(process)
		return None, 'checks.profile timed out'
	finally:
		LIVE.discard(process)

	seconds = time.time() - start

	with open(os.path.join(folder, 'profile.log'), 'w') as log:
		log.write('$ {}\n\n{}{}'.format(line, out, err))

	if process.returncode != 0 or not out.strip():
		return None, 'checks.profile exits {}{} (see {})'.format(
			process.returncode, ', printing nothing' if not out.strip()
			else '', os.path.join(folder, 'profile.log'))

	path = profile_path(root, identifier)
	with open(path, 'w') as handle:
		handle.write('# Profile of {} (commit {}), {}, by checks.profile\n\n'
			'{}\n'.format(label(root, identifier), commit[:12], now(),
				out.strip()))

	result = load_result(root, identifier)
	if result is not None:
		result['profile_seconds'] = round(seconds, 1)
		write_json(os.path.join(iteration_dir(root, identifier),
			'result.json'), result)

	return path, seconds


def nearest_profile(root, identifier):
	"""`(id, path)` of the profile of `identifier` or of its nearest
	ancestor that has one; `(None, None)` when none does."""

	for ancestor in [identifier] + [i for i in reversed(lineage(root,
			identifier)) if i != identifier]:
		if os.path.exists(profile_path(root, ancestor)):
			return ancestor, profile_path(root, ancestor)

	return None, None


def profile_note(root, identifier, idea):
	"""A brief's line on the idea's function in its parent's profile: the
	profile lines naming it, or that the profile does not name it. None
	when there is no profile or the idea names no function."""

	fields = idea_fields('- ' + idea if idea.startswith('[')
		else '- [MISC] ' + idea)
	function = (fields or {}).get('function')
	holder, path = nearest_profile(root, identifier)

	if not function or path is None:
		return None

	with open(path) as handle:
		lines = [line.strip() for line in handle.read().splitlines()[2:]
			if line.strip()]

	found = [line for line in lines if re.search(r'(?<![\w]){}(?![\w])'
		.format(re.escape(function)), line)]
	where = 'the profile of {}{} (`{}`)'.format(label(root, holder),
		'' if holder == identifier else ', the nearest ancestor of {} with '
		'one'.format(label(root, identifier)), path)

	if found:
		return '{} names `{}`: {}'.format(where[0].upper() + where[1:],
			function, '; '.join('`{}`'.format(line) for line in found[:3]))

	return ('{} does not name `{}`: confirm that the timed call reaches it '
		'before building anything').format(where[0].upper() + where[1:],
			function)


def profile(args):
	"""Run `checks.profile` on an iteration (default: the current root)
	and write its `profile.txt`."""

	root = resolve(args.root)
	config = load_config(root)
	state = load_state(root)
	identifier = args.id if args.id is not None else state['root']
	path, detail = profile_root(root, config, identifier)

	if path is None:
		fail('no profile of {}: {}'.format(label(root, identifier), detail))

	with open(path) as handle:
		lines = handle.read().splitlines()

	print('\n'.join(lines[:PROFILE_SHOWN]))
	print('\nprofile: {} ({})'.format(path, human_seconds(detail)))
	return 0


# A change must not depend on a compiler's decisions (references/idea-miner.md,
# *What makes an idea usable*; references/subagent.md, *Portability*). The
# scan flags the changed lines that hold a construct which steers a compiler,
# or a build flag, so the subagent has to answer for them in notes.md.
TOOLCHAIN_SOURCES = ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.cu',
	'.cuh', '.rs', '.f', '.f90', '.s', '.asm')
TOOLCHAIN_BUILD_FILES = ('makefile', 'gnumakefile', 'cmakelists.txt',
	'meson.build', 'meson_options.txt', 'cargo.toml', 'build.rs',
	'configure.ac', 'configure', 'setup.py', 'setup.cfg', 'pyproject.toml',
	'cmakepresets.json', 'cmakeuserpresets.json', 'build.sh', 'env.sh',
	'justfile', 'build.ninja', 'build.zig', 'config.toml', 'toolchain.cmake',
	'makefile.inc', 'makefile.am', 'makefile.in')
TOOLCHAIN_BUILD_SUFFIXES = ('.mk', '.cmake', '.inc', '.am', '.ninja')
TOOLCHAIN_CONSTRUCT = re.compile(
	r'__attribute__|__declspec|always_inline|noinline|#\s*pragma|_Pragma'
	r'|#\[(?:inline|target_feature|cold|hot)|__builtin_(?:expect|unreachable'
	r'|assume_aligned)|\[\[(?:un)?likely\]\]|\[\[gnu::|__restrict'
	r'|\*\s*restrict\b|\b(?:__asm__|asm)\b')
# These steer the compiler wherever they are, so a block of added lines does
# not excuse them the way it excuses an inline hint in a function added by the diff.
TOOLCHAIN_ALWAYS = re.compile(
	r'optimize\s*\(|\btarget\s*\(|#\s*pragma\s+(?:GCC|clang)\b|_Pragma'
	r'|gnu::(?:optimize|target)|target_feature')
TOOLCHAIN_FLAG = re.compile(
	r'(?<![\w-])-(?:O(?:[0-3sgz]|fast)?|f[a-z][a-z0-9-]*'
	r'|m[a-z][a-z0-9=.-]*)(?![\w-])')
# A compiler or flag variable set in a build file, or a build file pulled in
# from another file (the flags may live there).
TOOLCHAIN_ASSIGN = re.compile(
	r'^\s*(?:export\s+)?(?:CC|CXX|FC|CPP|AR|LD|CFLAGS|CXXFLAGS|CPPFLAGS'
	r'|FFLAGS|LDFLAGS|LDLIBS|RUSTFLAGS|NVCCFLAGS|OPT|OPTFLAGS|OPTIMIZE'
	r'|CMAKE_[A-Z_]*(?:COMPILER|FLAGS|BUILD_TYPE))\b\s*[:+?]?='
	r'|^\s*-?include\s+\S|^\s*(?:set|option)\s*\(\s*CMAKE_[A-Z_]*(?:COMPILER'
	r'|FLAGS|BUILD_TYPE)|\brustflags\b|\bopt-level\b|\blto\s*=')
# A binary or a vendored tree added to the repository brings a faster
# implementation that no diff of ours optimizes.
TOOLCHAIN_BINARY_SUFFIXES = ('.a', '.so', '.o', '.obj', '.lib', '.dll',
	'.dylib', '.whl', '.rlib')
TOOLCHAIN_VENDOR_DIRS = ('vendor', 'third_party', 'thirdparty', 'third-party',
	'external', 'extern', 'deps', 'contrib')
# Added lines past this many, with nothing removed, are a new function: a
# pragma or attribute there is allowed. A lone added line is an edit.
TOOLCHAIN_NEW_CODE_LINES = 3
TOOLCHAIN_ANSWER = re.compile(r'relies on a compiler decision:\s*(yes|no)\b',
	re.IGNORECASE)


def toolchain_flags(root, identifier):
	"""`[(file, line)]`: the changed lines of an iteration, against its
	parent, that hold a toolchain construct in an existing function (a hunk
	that removes lines, or adds at most `TOOLCHAIN_NEW_CODE_LINES`) or a build
	flag in a build file. Empty for the baseline."""

	parent = (load_meta(root, identifier) or {}).get('parent')

	if parent is None:
		return []

	try:
		diff = git('-C', os.path.join(root, 'repo.git'), 'diff', '--unified=0',
			'--no-renames', 'autoopt/{}'.format(parent),
			'autoopt/{}'.format(identifier))
	except RuntimeError as error:
		fail('cannot read the diff of {} against {}: {}'.format(label(root,
			identifier), label(root, parent), error))

	found, name, hunk = [], None, None

	def close():
		if not name or not hunk or not (hunk['added'] or hunk['removed']):
			return

		base = os.path.basename(name).lower()
		build = base in TOOLCHAIN_BUILD_FILES or base.endswith(
			TOOLCHAIN_BUILD_SUFFIXES)

		if not build and not name.lower().endswith(TOOLCHAIN_SOURCES):
			return

		lines = hunk['added'] + hunk['removed']

		if build:
			hits = [l for l in lines if TOOLCHAIN_FLAG.search(l)
				or TOOLCHAIN_CONSTRUCT.search(l) or TOOLCHAIN_ASSIGN.search(l)]
		elif hunk['removed'] or len(hunk['added']) <= TOOLCHAIN_NEW_CODE_LINES:
			hits = [l for l in lines if TOOLCHAIN_CONSTRUCT.search(l)]
		else:
			hits = [l for l in lines if TOOLCHAIN_ALWAYS.search(l)]

		found.extend((name, ' '.join(l.split())[:100]) for l in hits[:2])

	for line in diff.splitlines():
		if line.startswith('diff --git '):
			close()
			name, hunk = line.split(' b/', 1)[-1], None
		elif line.startswith('@@'):
			close()
			hunk = {'added': [], 'removed': []}
		elif hunk is not None and line.startswith('+'):
			hunk['added'].append(line[1:])
		elif hunk is not None and line.startswith('-'):
			hunk['removed'].append(line[1:])

	close()

	try:
		added = git('-C', os.path.join(root, 'repo.git'), 'diff',
			'--name-only', '--diff-filter=A', '--no-renames',
			'autoopt/{}'.format(parent), 'autoopt/{}'.format(identifier))
	except RuntimeError:
		added = ''

	try:
		binary = {line.split('\t', 2)[2] for line in git('-C', os.path.join(
			root, 'repo.git'), 'diff', '--numstat', '--diff-filter=A',
			'--no-renames', 'autoopt/{}'.format(parent), 'autoopt/{}'.format(
			identifier)).splitlines() if line.startswith('-\t-\t')}
	except RuntimeError:
		binary = set()

	for name in added.splitlines():
		parts = name.lower().split('/')

		if name.lower().endswith(TOOLCHAIN_BINARY_SUFFIXES) or name in binary:
			found.append((name, 'a new binary or library file'))
		elif name.lower().endswith(('.s', '.asm')):
			found.append((name, 'a new assembly file'))
		elif any(part in TOOLCHAIN_VENDOR_DIRS for part in parts[:-1]):
			found.append((name, 'a new file in a vendored directory'))

	return found


def toolchain_build_changes(root, identifier):
	"""The flagged changes no answer excuses: a changed build file, a binary
	or library, a vendored tree, an assembly file. Whatever the notes say,
	the gain then comes from the build or from code nobody here wrote."""

	return [(name, text) for name, text in toolchain_flags(root, identifier)
		if text.startswith(('a new binary', 'a new file in a vendored',
		'a new assembly')) or os.path.basename(name).lower() in
		TOOLCHAIN_BUILD_FILES or name.lower().endswith(
		TOOLCHAIN_BUILD_SUFFIXES)]


def toolchain_problem(root, identifier, sections):
	"""`(answer, problem)`: `answer` is `'yes'` or `'no'` from the first line
	of notes.md's `## Toolchain` section, `None` without one; `problem` says
	what to fix. A flagged diff needs the section."""

	body = dict(sections).get('Toolchain')

	if body is None:
		flagged = toolchain_flags(root, identifier)

		if flagged:
			return None, ('the diff holds {} (e.g. {}: `{}`): notes.md needs '
				'a `## Toolchain` section, references/subagent.md, '
				'*Portability*'.format('a toolchain construct or build flag'
				if len(flagged) == 1 else '{} toolchain constructs or build '
				'flags'.format(len(flagged)), flagged[0][0], flagged[0][1]))

		return None, None

	first, _, rest = body.partition('\n')
	match = TOOLCHAIN_ANSWER.match(first.strip())

	if not match or not rest.strip():
		return None, ('notes.md `## Toolchain` starts with `Relies on a '
			'compiler decision: yes` or `no`, then the mechanism that makes '
			'the change faster')

	return match.group(1).lower(), None


def artifacts_path(root):
	"""`autooptimize/test-artifacts.txt`: every file a unit-test run created
	in a worktree, one path (relative to the worktree) per line."""

	return os.path.join(root, 'test-artifacts.txt')


# Files the runner writes into a worktree for its own bookkeeping: never part
# of an iteration, never a reason to call a worktree unmeasured.
RUNNER_FILES = ('.autoopt-build.lock', '.autoopt-build.log')


def unmeasured(checkout, known=(), untracked='normal', paths=(), exclude=()):
	"""`git status --porcelain` of `checkout` (limited to `paths`), less the
	runner's own files (RUNNER_FILES), the untracked files in `known` and the
	paths `exclude` matches (`after.build_outputs`)."""

	skip = set(RUNNER_FILES) | set(known)
	specs = (list(paths) or ['.']) + glob_specs(exclude, 'exclude,glob') if \
		exclude else list(paths)
	out = git('-C', checkout, 'status', '--porcelain', '--untracked-files='
		+ untracked, *(['--'] + specs if specs else [])).rstrip()

	return '\n'.join(line for line in out.split('\n') if line and not (
		line.startswith('?? ') and line[3:] in skip))


def test_artifacts(root):
	try:
		with open(artifacts_path(root)) as handle:
			return {line.rstrip('\n') for line in handle if line.strip()}
	except OSError:
		return set()


def record_artifacts(root, paths):
	"""Add `paths` to the record of test-created files, under its lock."""

	if not paths:
		return

	with open(artifacts_path(root), 'a+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		handle.seek(0)
		known = {line.rstrip('\n') for line in handle}
		new = [path for path in paths if path not in known]

		if new:
			handle.write(''.join(path + '\n' for path in new))


def artifact_problem(root, identifier):
	"""Why `promote` refuses an iteration whose commit adds a file a unit-test
	run creates (`test-artifacts.txt`), or None."""

	known = test_artifacts(root)
	result = load_result(root, identifier) or {}
	meta = load_meta(root, identifier) or {}
	parent = (load_result(root, meta.get('parent')) or {}).get('commit') if \
		meta.get('parent') is not None else None

	if not known or not result.get('commit') or not parent:
		return None

	config = read_json(os.path.join(root, 'config.json'))
	done = subprocess.run(['git', '-C', checkout_dir(root, identifier, config),
		'diff', '--name-only', '--no-renames', '--diff-filter=A', '-z', parent,
		result['commit']], capture_output=True, text=True)
	added = sorted(set(name for name in done.stdout.split('\0') if name) &
		known)

	if not added:
		return None

	return ('iteration {} commits {}, which the unit tests create ({}): a '
		'test artifact, not a change. Remove it from the commit (git rm '
		'--cached, then measure again) before promoting'.format(label(root,
		identifier), ', '.join(added[:5]), os.path.basename(artifacts_path(
		root))))


def toolchain_chain(root, identifiers):
	"""`(problems, attested)` for the iterations that make up a winner.
	`problems` are the refusals `promote` would give today (the scan is wider
	than when they were promoted); `attested` are `(label, file, line)` of
	flagged changes the subagent answered `no` for, which the user sees."""

	problems, attested = [], []

	for identifier in identifiers:
		problem = toolchain_gate(root, identifier)

		if problem:
			problems.append(problem)
			continue

		attested.extend((label(root, identifier), name, text) for name, text
			in toolchain_flags(root, identifier))

	return problems, attested


def toolchain_gate(root, identifier):
	"""Why `promote` must refuse an iteration on toolchain grounds, or None."""

	meta = load_meta(root, identifier) or {}
	name = label(root, identifier)

	raw = os.path.join(root, 'config.json')

	if not (os.path.exists(raw) and read_json(raw).get('allow_build_changes')):
		build = toolchain_build_changes(root, identifier)

		if build:
			return ('iteration {} changes the build or adds a binary, vendored '
				'or assembly file ({}: `{}`): no `## Toolchain` answer excuses '
				'it. Remove the change, or set allow_build_changes with '
				'`autoopt.py config --set allow_build_changes=yes --reason '
				'"..."` when tuning the build is the task'.format(name,
				build[0][0], build[0][1]))

	if meta.get('toolchain') == 'specific':
		return ('iteration {} relies on a compiler decision (its `## '
			'Toolchain` says yes): a toolchain-specific change is never '
			'promoted'.format(name))

	flagged = toolchain_flags(root, identifier)

	if flagged and meta.get('toolchain') != 'no':
		return ('iteration {} changes {} ({}: `{}`): write `## Toolchain` in '
			'its notes.md and run `finish`; promotion waits for an answer '
			'of no'.format(name, 'a toolchain construct or build flag'
			if len(flagged) == 1 else '{} toolchain constructs or build '
			'flags'.format(len(flagged)), flagged[0][0], flagged[0][1]))

	return None


def preflight_problem(root, identifier):
	"""Why `promote` refuses a repository search's iteration on
	`preflight.py`'s checks of its own change, or None."""

	config = read_json(os.path.join(root, 'config.json'))

	if not config.get('repo'):
		return None

	import preflight
	found = preflight.problems(root, load_config(root), identifier)

	if not found:
		return None

	return ('iteration {} fails the preflight (scripts/preflight.py): fix each '
		'in a new commit of its branch, then run_iteration.py again\n  {}'
		.format(label(root, identifier), '\n  '.join('{}: {}'.format(*item)
		for item in found)))


def promote(args):
	root = resolve(args.root)
	state = load_state(root)
	ended = load_ended(root)

	if ended is not None and not (load_meta(root, args.id) or {}).get(
			'after_end'):
		fail('the search ended on {}: only an iteration made after the end '
			'(`new --after-end`) is promoted now, and it becomes the winner'
			.format(ended.get('ended_at')))

	result = load_result(root, args.id)

	if result is None:
		fail('iteration {} has no result.json'.format(args.id))

	if result.get('status') != 'ok':
		fail('iteration {} is {}; only a passing iteration can become the '
			'root'.format(label(root, args.id), result.get('status')))

	if result.get('control') or (load_meta(root, args.id) or {}).get(
			'control'):
		fail('iteration {} is a same-code control: its code is its parent\'s, '
			'so it measures the machine and is never promoted'.format(
				label(root, args.id)))

	problem = toolchain_gate(root, args.id) or artifact_problem(root,
		args.id) or preflight_problem(root, args.id)

	if problem:
		fail(problem)

	# A subset of the suite passed; the root has to have passed all of it.
	if (result.get('tests') or {}).get('scope') == 'subset':
		fail('iteration {} passed only the unit tests for the modules it '
			'changed. Run the whole suite first:\n\n  python3 {} {} '
			'--full-tests\n'.format(label(root, args.id), os.path.join(os.path.dirname(
				os.path.abspath(__file__)), 'run_iteration.py'),
				iteration_dir(root, args.id)))

	# The runner runs the task's compare on every iteration whose runs pass
	# their gates; one with no passing compare on record has not shown its
	# outputs agree on the inputs the benchmark never times.
	if ((load_config(root).get('checks') or {}).get('compare')
			and ((result.get('check') or {}).get('compare') or {}).get(
				'verdict') != 'pass'):
		fail('iteration {} has no passing check: run `python3 {} check {} '
			'--id {}` first'.format(label(root, args.id), os.path.abspath(
				__file__), root, label(root, args.id)))

	with Lock(root):
		state = load_state(root)

		if state['root'] == args.id:
			print('iteration {} is already the root; nothing changed'.format(
				label(root, args.id)))
			return 0

		# Everything forked since the last promotion carries the root's
		# change; an iteration that does not descend from the root would
		# drop it from the new root without a word.
		if state['root'] not in lineage(root, args.id):
			fail('iteration {} does not descend from the root {}: promoting '
				'it would drop {}\'s change. Fork from the current root and '
				're-implement the change there if it is still wanted'.format(
					label(root, args.id), label(root, state['root']),
					label(root, state['root'])))

		state['root'] = args.id
		state['round'] = state.get('round', 1) + 1
		state.setdefault('roots', []).append({'round': state['round'],
			'id': args.id})
		save_state(root, state)

	print('the root is now iteration {} (round {})'.format(
		label(root, args.id), state['round']))

	config = load_config(root)

	if ended is not None:
		record_winner_change(root, config, args.id)
		print('the winner is now {} (ended.json, winner_changes); everything '
			'checked on or built from the old winner is stale'.format(label(root, args.id)))
	if (config.get('checks') or {}).get('profile'):
		path, detail = profile_root(root, config, args.id)
		print('profile: {}'.format('{} ({})'.format(path, human_seconds(detail))
			if path else 'not written: {}'.format(detail)))

	for path in summarize_closed_rounds(root, load_config(root)):
		print('wrote the round summary {}'.format(path))

	remind_summaries(root, load_state(root))

	return 0


###
# config: change a setting mid-search, and record why
###

# What `config --set` may change, and how each value is read. The objective,
# the command and the seeds are absent on purpose: changing any of them makes
# every earlier number incommensurable, which is a new search
# (references/changes.md).
SETTABLE = {
	'task': str, 'gpus_per_agent': int, 'runs_per_iteration': int, 'confirm_runs': int,
	'staged': bool, 'timeout_seconds': float, 'gpu': bool, 'gpu_command': str,
	'floor': float, 'workers': int,
	'tests.command': str, 'tests.timeout_seconds': float, 'tests.path': str,
	'external_validation.note': str, 'subagent_model': str, 'promote': str,
	'loop_handback': int, 'allow_build_changes': bool,
	'checks.compare': str, 'checks.time': str, 'checks.first_call': str,
	'checks.build': str, 'checks.profile': str, 'checks.threads': int,
}
SETTABLE.update({'after.' + key: kind for key, kind in AFTER_KINDS.items()})
GATE_FIELDS = {'threshold': float, 'op': str, 'tol': float}


def parse_value(kind, text):
	if kind is bool:
		if text.lower() in ('true', 'yes', '1'):
			return True
		if text.lower() in ('false', 'no', '0'):
			return False
		raise ValueError(text)
	if kind in (float, str) and text.lower() == 'null':
		return None
	if kind is float and text.lower() == 'none':
		return None
	if kind is float:
		value = float(text)
		return int(value) if value.is_integer() and 'e' not in text.lower() \
			and '.' not in text else value
	return kind(text)


def config_change(args):
	root = resolve(args.root)
	path = os.path.join(root, 'config.json')

	if not args.set and not args.add_observer:
		print(json.dumps(load_config(root), indent='\t'))
		return 0

	if not (args.reason or '').strip():
		fail('--reason is required: the user\'s words, or why the default '
			'was taken while they were away. It goes into spec.md, *Rules '
			'added during the search*')

	with Lock(root):
		raw = read_json(path)
		changes = []

		for item in args.set or []:
			key, _, text = item.partition('=')
			key = key.strip()

			if not _:
				fail('--set takes KEY=VALUE; got {!r}'.format(item))

			if key.startswith('gates.'):
				parts = key.split('.')
				if len(parts) != 3 or parts[2] not in GATE_FIELDS:
					fail('a gate is set as gates.<name>.<{}>; got {!r}'.format(
						'|'.join(GATE_FIELDS), key))
				gate = next((g for g in raw.get('gates', [])
					if g.get('name') == parts[1]), None)
				if gate is None:
					fail('there is no gate named {!r}; the gates are {}'.format(
						parts[1], ', '.join(g['name'] for g in raw.get('gates',
							[])) or 'none'))
				holder, field, kind = gate, parts[2], GATE_FIELDS[parts[2]]
			elif key.startswith('build_env.'):
				field = key.split('.', 1)[1]

				if field not in BUILD_VARS:
					fail('{} is not a build variable; they are {}'.format(
						field, ', '.join(BUILD_VARS)))

				holder, kind = raw.setdefault('build_env', {}), str
			elif key == 'command':
				# Until the baseline has run, nothing was measured under the
				# command, so fixing it is not a new search.
				if load_result(root, 0) is not None or glob.glob(os.path.join(
						iteration_dir(root, 0), 'runs', 'seed-*')):
					fail('the command is set before the baseline runs; A0 has '
						'run under it, so a change to it is a new search '
						'(references/changes.md)')
				holder, field, kind = raw, key, str
			elif key in SETTABLE:
				holder, field, kind = raw, key, SETTABLE[key]
				if '.' in key:
					block, field = key.split('.', 1)
					# `after` is absent from a search begun before the block
					# existed; check_config fills the defaults.
					if block in ('checks', 'after'):
						raw.setdefault(block, {})
					if not isinstance(raw.get(block), dict):
						fail('{} is not configured, so {} cannot be set; '
							'it is added with `init --force`'.format(block, key))
					holder = raw[block]
			else:
				fail('{!r} cannot be set here. Settable: {}, gates.<name>.<{}>, '
					'and the command until the baseline runs. The objective, '
					'seed_base and, after the baseline, the command are not: '
					'a change to any of them is a new search '
					'(references/changes.md)'.format(key,
						', '.join(sorted(SETTABLE)), '|'.join(GATE_FIELDS)))

			try:
				# `null` unsets an `after` answer of any kind.
				value = None if key.startswith('after.') and text.strip() \
					.lower() in ('null', 'none') else parse_value(kind,
						text.strip())
			except ValueError:
				fail('{} takes a {}; got {!r}'.format(key, kind.__name__, text))

			if kind is int and value is not None and value < 1:
				fail('{} must be at least 1; got {}'.format(key, value))

			if key == 'promote' and value not in PROMOTE_MODES:
				fail('promote is one of {}; got {!r}'.format(
					', '.join(PROMOTE_MODES), value))

			if key == 'subagent_model' and value not in SUBAGENT_MODELS:
				fail('subagent_model is one of {}; got {!r}'.format(
					', '.join(SUBAGENT_MODELS), value))

			if key.startswith('checks.') and field in CHECKS:
				check_placeholders(key, value, CHECKS[field])
				missing = [name for name in REQUIRED_PLACEHOLDERS[
					CHECKS[field]] if '{' + name + '}' not in value]
				if missing:
					fail('{} must contain {}; got {!r}'.format(key, ' and '
						.join('{' + name + '}' for name in missing), value))

			if field in ('timeout_seconds',) and value is not None and value < 0:
				fail('{} must be 0 (off) or more; got {}'.format(key, value))

			old = holder.get(field)
			holder[field] = value
			changes.append((key, old, value))

			# An unstaged search runs one count: the depth follows it.
			if key == 'runs_per_iteration' and not raw.get('staged', True):
				raw['confirm_runs'] = value

		for name in args.add_observer or []:
			if any(o.get('name') == name for o in raw.get('observers', [])):
				fail('observer {!r} is already recorded'.format(name))
			raw.setdefault('observers', []).append({'name': name})
			changes.append(('observers', None, '+' + name))

		check_config(json.loads(json.dumps(raw)))
		write_json(path, raw)

		lines = ['- {}: config {} by the orchestrator. Reason: "{}"'.format(
			time.strftime('%Y-%m-%d'), '; '.join('{} {} -> {}'.format(key,
				json.dumps(old), json.dumps(new)) if not str(new).startswith('+')
				else 'observer {} added'.format(str(new)[1:])
				for key, old, new in changes), args.reason.strip())]

		with open(spec_path(root), 'a') as out:
			out.write('\n'.join(lines) + '\n')

	for key, old, new in changes:
		print('{}: {} -> {}'.format(key, json.dumps(old), json.dumps(new)))

	print('recorded in {}, *Rules added during the search*'.format(
		spec_path(root)))

	if any(key.startswith('gates.') for key, _, _ in changes):
		print('a gate changed: every earlier pass and FAIL was judged under '
			'the old one. Re-judge an iteration from its stored runs, without '
			'--rerun, starting with the baseline: python3 {} {}. Iterations '
			'in flight read config.json when they started and keep the old '
			'gate; re-judge them once they settle.'.format(
				os.path.join(os.path.dirname(os.path.abspath(__file__)),
					'run_iteration.py'), iteration_dir(root, 0)))

	if any(key in ('runs_per_iteration', 'confirm_runs', 'staged')
		or key.startswith('gates.') for key, _, _ in changes):
		print('tell the user with the next round summary: it changes what '
			'the later numbers mean')

	return 0


def search_frontier(root, results, direction):
	"""The best iteration, seeded by the baseline: a first iteration that
	does not beat it is not an improvement."""

	best, best_id = frontier(results, direction)

	if 0 not in {r.get('id') for r in results}:
		seed = load_result(root, 0)

		if seed is not None and seed.get('status') == 'ok':
			value = (seed.get('objective') or {}).get('median')

			if better(value, best, direction):
				best, best_id = value, 0

	return best, best_id


###
# external validation
###

STALLED_AFTER = 900      # seconds of silence before a working subagent is flagged
FINISHING_WINDOW = 3600  # a README-less iteration silent this long is not "working"
QUIET_SKIP = ('cache', 'src', 'v', '.numba-cache', '__pycache__', '.git')


def last_activity(directory):
	"""Seconds since anything a subagent writes in an iteration changed,
	skipping caches and code copies, which say nothing about progress."""

	newest = 0.0

	for base, dirs, names in os.walk(directory):
		dirs[:] = [d for d in dirs if d not in QUIET_SKIP]
		for name in names:
			try:
				newest = max(newest, os.path.getmtime(os.path.join(base, name)))
			except OSError:
				continue

	return time.time() - newest if newest else None


def live_runners(root):
	"""`{id: [pid, ...]}`: the run_iteration.py processes running an
	iteration of this search, from /proc where it exists and `ps` elsewhere.
	A process of another user is skipped."""

	folder = os.path.realpath(os.path.join(root, 'iterations'))
	found = {}

	def note(pid, argv, cwd):
		if not any(os.path.basename(arg) == 'run_iteration.py' for arg in argv):
			return
		for arg in argv:
			path = os.path.realpath(os.path.join(cwd or os.sep,
				os.path.expanduser(arg)))
			if os.path.dirname(path) == folder and os.path.basename(
					path).isdigit():
				found.setdefault(int(os.path.basename(path)), []).append(pid)
				return

	if os.path.isdir('/proc/self'):
		for name in os.listdir('/proc'):
			if not name.isdigit():
				continue
			try:
				with open('/proc/{}/cmdline'.format(name), 'rb') as handle:
					argv = [part.decode(errors='replace') for part in
						handle.read().split(b'\0') if part]
				cwd = os.readlink('/proc/{}/cwd'.format(name))
			except OSError:
				continue
			note(int(name), argv, cwd)
		return found

	try:
		listing = subprocess.run(['ps', '-eo', 'pid=,args='],
			capture_output=True, text=True).stdout
	except OSError:
		return found

	for line in listing.splitlines():
		parts = line.split()
		if len(parts) > 1 and parts[0].isdigit():
			note(int(parts[0]), parts[1:], None)

	return found


def worktree_changed(root, config, identifier, meta):
	"""Whether an unmeasured iteration's worktree holds any edit: an
	uncommitted or new file, or a tracked change against its parent's
	branch. The runner commits only when it starts, so the branches alone
	say nothing."""

	checkout = checkout_dir(root, identifier, config)

	if not os.path.isdir(checkout):
		return False

	try:
		dirty = unmeasured(checkout)
	except RuntimeError:
		dirty = ''

	parent = meta.get('parent')
	diff = subprocess.run(['git', '-C', checkout, 'diff', '--stat',
		'autoopt/{}'.format(parent)], capture_output=True,
		text=True).stdout.strip() if parent is not None else ''

	return bool(dirty or diff)


def resume_lines(root, config, state):
	"""What a session picking up the search reads (references/resume.md),
	with a verdict for each unfinished iteration."""

	lines = []
	runners = live_runners(root)
	busy = {identifier: idle for identifier, _, idle in working(root)}
	verdicts = []

	for identifier in iteration_ids(root):
		if identifier == 0:
			continue
		directory = iteration_dir(root, identifier)
		result = load_result(root, identifier)
		partial = result is not None and result.get('status') == 'partial'
		if result is not None and not partial and os.path.exists(
				os.path.join(directory, 'README.md')):
			continue
		meta = load_meta(root, identifier) or {}
		name = label(root, identifier)
		idle = busy.get(identifier, last_activity(directory))
		quiet = idle is None or idle > (STALLED_AFTER if result is None
			else FINISHING_WINDOW)
		prompt = '`autoopt.py brief <work> --prompt {}`'.format(identifier)

		if runners.get(identifier):
			verdict = ('its runner is alive (pid {}): leave it; resuming it '
				'runs the same seeds twice'.format(', '.join(str(pid)
					for pid in runners[identifier])))
		elif not quiet:
			verdict = ('its subagent wrote {} ago and may still be working: '
				'look again before handing it on'.format(human_seconds(idle)))
		elif result is None and not worktree_changed(root, config,
				identifier, meta):
			verdict = ('an empty fork, never edited: set it aside (`mv '
				'iterations/{0} iterations/{0}.abandoned`, then `git -C '
				'repo.git worktree prune`)'.format(identifier))
		elif partial:
			verdict = ('partial ({} run(s)): hand it to a new subagent with the '
				'prompt {} prints, or set it aside'.format(result.get('n_runs'),
					prompt))
		else:
			verdict = ('{}: hand it to a new subagent with the prompt {} '
				'prints'.format('measured, no README' if result is not None
					else 'edited, not measured', prompt))

		verdicts.append('  {} ({}): {}'.format(name, meta.get('title') or '',
			verdict))

	lines += ['', '## Unfinished iterations', '']
	lines += verdicts or ['  none']

	plan = ['{} run(s) per iteration'.format(config['runs_per_iteration']),
		'promote {}'.format(config.get('promote', 'orchestrator'))]
	plan.append('GPU: {}'.format('{} per iteration'.format(config.get(
		'gpus_per_agent', 1)) if config.get('gpu', True) else 'none'))
	plan.append('subagent model {}'.format(config.get('subagent_model')
		or 'opus'))
	checks = sorted(key for key, value in (config.get('checks') or {}).items()
		if value)
	if checks:
		plan.append('checks: ' + ', '.join(checks))
	validation = config.get('external_validation') or {}
	if validation.get('command'):
		plan.append('external validation {}'.format('approved'
			if validation.get('approved') else 'not approved'))
	lines += ['', '## The plan (config.json)', '', '  ' + '; '.join(plan)]

	try:
		with open(spec_path(root)) as handle:
			spec = handle.read()
	except OSError:
		spec = ''
	lines += ['', '## Rules (spec.md)', '']
	for field in ('Keep rule', 'Off limits', 'Portability'):
		lines.append('  {}: {}'.format(field, spec_field(spec, field)
			or '(none written)'))
	added = spec_added(spec)
	if added:
		lines.append('  Rules added during the search:')
		lines += ['    ' + line.strip() for line in added]

	folder = os.path.join(root, 'rounds')
	stems = [round_stem(number) for number in range(
		state.get('round', 1), 0, -1)]
	latest = next((os.path.join(folder, stem + '.md') for stem in stems
		if os.path.exists(os.path.join(folder, stem + '.md'))), None)
	lines += ['', '## The last round summary', '']
	if latest:
		with open(latest) as handle:
			text = handle.read()
		block = re.search(r'```\n(.*?)\n```', text, re.S)
		landed = markdown_sections(text).get('What landed and why')
		lines += ['  ' + latest, ''] + (block.group(1).splitlines() if block
			else text.strip().splitlines()[:40])
		if landed:
			lines += ['', 'What landed and why:'] + landed.splitlines()
	else:
		lines.append('  none yet')

	upcoming = next_ideas(root, 5)
	lines += ['', '## The next untried ideas, as brief --next takes them', '']
	lines += ['  ' + line[:200] for _, line, _ in upcoming] or (['  none: the '
		'budget of {} idea-miner stage(s) is spent, so the search stops for '
		'`budget`'.format(stage_budget(root, config, state)['budget'])]
		if stage_budget(root, config, state)['spent'] else ['  none: run '
		'`autoopt.py mine <work>`'])
	lines += ['', 'Act on each verdict above, then go on with the loop in '
		'references/step3-rounds.md (`brief --next`).']

	return lines


def working(root):
	"""Iterations a subagent is still on: running (no result.json yet), or
	finishing (measured, README not written, active within the hour). Each
	as (id, 'running' | 'finishing', seconds since its last write)."""

	out = []

	for identifier in iteration_ids(root):
		if identifier == 0:
			continue

		directory = iteration_dir(root, identifier)

		# last_activity walks the whole iteration; a settled one with a
		# README never needs it, and a long search holds millions of files.
		if load_result(root, identifier) is None:
			out.append((identifier, 'running', last_activity(directory)))
		elif not os.path.exists(os.path.join(directory, 'README.md')) and \
				not load_meta(root, identifier).get('split'):
			# An iteration built from others' code (`meta.split`) is measured
			# and never written up.
			idle = last_activity(directory)
			if idle is not None and idle < FINISHING_WINDOW:
				out.append((identifier, 'finishing', idle))

	return out


def overdue_summaries(root, state):
	"""Closed rounds whose iterations have all settled and that have no
	summary yet."""

	rounds = {}

	for identifier in iteration_ids(root):
		if identifier == 0:
			continue
		record = load_result(root, identifier) or load_meta(root, identifier) or {}
		settled = load_result(root, identifier) is not None
		number = record.get('round')
		if number is None:
			continue
		rounds.setdefault(number, []).append(settled)

	return [number for number, flags in sorted(rounds.items())
		if number < state.get('round', 1) and all(flags)
		and not os.path.exists(os.path.join(root, 'rounds',
			round_stem(number) + '.md'))]


def remind_summaries(root, state):
	overdue = overdue_summaries(root, state)

	if overdue:
		print('summary overdue for round(s) {}: every iteration '
			'has settled; it is written once each has a README, or now with '
			'autoopt.py summary <work> --round {}'.format(
				', '.join(str(n) for n in overdue), overdue[0]),
			file=sys.stderr)


def round_stem(round_number):
	"""The file stem for one round's summary and validation."""

	return 'round-{}'.format(round_number)


def validation_path(root, round_number):
	return os.path.join(root, 'validations',
		round_stem(round_number) + '.json')


def load_validation(root, round_number):
	path = validation_path(root, round_number)

	if not os.path.exists(path):
		return None

	return read_json(path)


def validate(args):
	"""Run the external validation once, against one iteration.

	It never touches `result.json`, the frontier or a status. A
	failing external validation is a finding for the user, not a verdict on
	an iteration -- which is the whole reason it is not a gate.
	"""

	root = resolve(args.root)

	if getattr(args, 'background', False):
		return detach(os.path.abspath(__file__), sys.argv[1:], os.path.join(
			root, 'validations', 'validate.log'))

	config = load_config(root)
	block = config.get('external_validation')

	if block is None:
		fail('this search has no "external_validation" in config.json. It is '
			'optional; add one with `init --validation-command ...`, or ask '
			'the user whether they want one.')

	if args.approve:
		# Sign-off and a run are two decisions; a flag for the run would
		# otherwise be accepted and silently ignored.
		if (args.round is not None or args.id is not None or args.dry_run
			or args.out):
			fail('--approve only records the sign-off and runs nothing; run '
				'`validate` again without it to validate')

		block['approved'] = True
		block['approved_on'] = now()

		write_json(os.path.join(root, 'config.json'), config)

		print('external validation approved on ' + block['approved_on'])
		return

	if not block.get('approved'):
		fail("the external validation has not been approved. It can be slow "
			"and it is the user's compute: show them the command and what it "
			"measures, and run `validate --approve` only once they have said "
			"yes.")

	state = load_state(root)
	round_number = args.round if args.round is not None else state['round']
	identifier = args.id if args.id is not None else state['root']
	directory = iteration_dir(root, identifier)

	if not os.path.isdir(directory):
		fail('iteration {} has no checkout to validate'.format(
			label(root, identifier)))

	# --out is a subagent checking its own iteration: the files go where it
	# says, and no round record is written, since the round's validation is
	# the run against the root.
	if args.out:
		if args.id is None:
			fail('--out validates one iteration: give it --id')
		out = os.path.abspath(args.out)
	else:
		out = os.path.join(root, 'validations',
			round_stem(round_number))
	os.makedirs(out, exist_ok=True)

	commit = (load_result(root, identifier) or {}).get('commit')
	command = fill(block['command'], iteration=directory,
		src=checkout_dir(root, identifier, config), out=out,
		cache=cache_dir(directory, commit),
		out_abs=os.path.abspath(out), round=round_number, id=identifier)

	if args.dry_run:
		print('would run, from {}:\n  {}'.format(directory, command))
		return 0

	print('external validation, {}, iteration {}'.format(
		'into {}'.format(out) if args.out else 'round {}'.format(
			round_number), label(root, identifier)))
	print('  ' + command)

	started = time.time()
	code, _ = run_shell(command, directory)
	seconds = time.time() - started

	payload, emitted = {}, os.path.join(out, 'validation.json')

	if os.path.exists(emitted):
		payload = read_json(emitted)

	record = {
		'round': round_number,
		'iteration': identifier,
		'command': command,
		'seconds': seconds,
		'exit_code': code,
		'measures': payload.get('measures', {}),
		'ok': payload.get('ok'),
		'note': payload.get('note', ''),
		'created': now(),
	}

	if record['ok'] is None:
		record['ok'] = code == 0

	write_json(os.path.join(out, 'record.json') if args.out
		else validation_path(root, round_number), record)

	print()
	print(validation_block(config, record))

	# Exit 0 whatever it found. This is not a gate, and a non-zero exit would
	# make it look like one to whatever called it.
	return 0


def validation_block(config, record):
	"""The lines that go on the terminal and into the round summary."""

	if record is None:
		return ''

	block = config.get('external_validation') or {}

	lines = ['external validation  round {}, iteration {}, {:.1f} min'.format(
		record['round'], record['iteration'], record['seconds'] / 60.0)]

	if block.get('note'):
		lines.append('  ' + block['note'])

	for name in block.get('measures') or sorted(record.get('measures') or {}):
		value = (record.get('measures') or {}).get(name)
		lines.append('  {:<34} {}'.format(name,
			'not reported' if value is None else fmt(value)))

	lines.append('  {:<34} {}'.format('verdict',
		'ok' if record.get('ok') else 'NEEDS ATTENTION'))

	if record.get('note'):
		lines.append('  ' + record['note'])

	lines.append('  not a gate: nothing here chose or rejected an iteration')

	return '\n'.join(lines)


###
# status and summary
###

def fmt(value, digits=4):
	if value is None:
		return '-'
	if isinstance(value, float):
		if value != value:
			return 'nan'
		return '{:.{}g}'.format(value, digits)

	return str(value)


def tests_cell(result):
	"""`pass`, `FAIL`, or `-` when the suite never ran (a run crashed or a
	gate broke first)."""

	tests = result.get('tests')

	if not tests:
		return '-'

	return ('pass' if tests.get('pass') else 'FAIL') + (' sub'
		if tests.get('scope') == 'subset' else '')


def check_cell(result):
	"""`pass`, `differ` or `error` for the compare."""

	record = result.get('check') or {}
	return (record.get('compare') or {}).get('verdict') or '-'


def table(root, config, results, on_frontier=None, compact=False):
	"""The round table: one row per iteration, one column per gate/observer;
	`compact` folds the gates into one verdict and drops the observers.

	`on_frontier` is the set of ids that improved on everything before them,
	computed over the *whole* search. Computing it from `results` instead
	would star the best row of any subset being printed, which is how a
	round-filtered table ends up claiming a frontier that is not one.
	"""

	direction = config['objective']['direction']

	if on_frontier is None:
		on_frontier = {r['id'] for r in frontier_path(results, direction)}

	header = ['id']
	header += ['cat', 'title', 'n', config['objective']['name'], 'Δ']
	if compact:
		header += ['gates'] if config['gates'] else []
	else:
		header += [gate['name'] for gate in config['gates']]
		header += [observer['name'] for observer in config['observers']]
	header += ['tests'] if config.get('tests') else []
	checked = bool((config.get('checks') or {}).get('compare'))
	header += ['check'] if checked else []
	header += ['status']

	baseline = load_result(root, 0)
	base_value = ((baseline or {}).get('objective') or {}).get('median')

	rows = []
	for result in sorted(results, key=lambda r: r.get('id', 0)):
		objective = result.get('objective') or {}
		value = objective.get('median')

		delta = (None if value is None or base_value is None
			else value - base_value)

		row = [
			'{}{}'.format(label(root, result['id']),
				'*' if result['id'] in on_frontier else ''),
		]

		row += [
			SHORT.get(category_of(result), '?'),
			(result.get('title') or '')[:38],
			result.get('n_runs'),
			fmt(value),
			fmt(delta, 3),
		]

		if compact and config['gates']:
			broken = [gate['name'] for gate in config['gates'] if not ((
				result.get('gates') or {}).get(gate['name']) or {}).get('pass')]
			row.append('FAIL ' + ','.join(broken) if broken else 'pass')

		for gate in [] if compact else config['gates']:
			record = (result.get('gates') or {}).get(gate['name']) or {}
			row.append('{} {}'.format('pass' if record.get('pass') else 'FAIL',
				fmt(record.get('worst'), 3))
				if record else '-')

		for observer in [] if compact else config['observers']:
			record = (result.get('observers') or {}).get(observer['name']) or {}
			row.append(fmt(record.get('median'), 3))

		if config.get('tests'):
			row.append(tests_cell(result))

		if checked:
			row.append(check_cell(result))

		row.append(result.get('status', '?'))
		rows.append([str(cell) for cell in row])

	widths = [max(len(str(header[i])), *(len(row[i]) for row in rows))
		if rows else len(str(header[i])) for i in range(len(header))]

	lines = ['  '.join(str(header[i]).ljust(widths[i])
		for i in range(len(header))).rstrip()]
	lines.append('  '.join('-' * widths[i] for i in range(len(header))))

	for row in rows:
		lines.append('  '.join(row[i].ljust(widths[i])
			for i in range(len(header))).rstrip())

	return '\n'.join(lines)


def status(args):
	root = resolve(args.root)
	config = load_config(root)
	state = load_state(root)
	results = all_results(root)

	direction = config['objective']['direction']

	best, best_id = frontier(results, direction)
	on_frontier = {r['id'] for r in frontier_path(results, direction, None)}

	chosen = (results if args.round is None
		else [r for r in results if r.get('round') == args.round])

	if args.json:
		print(json.dumps({'state': state, 'frontier': best,
			'frontier_id': best_id, 'on_frontier': sorted(on_frontier),
			'results': chosen}, indent='\t'))
		return 0

	running = [i for i in iteration_ids(root) if load_result(root, i) is None]

	print('{}  round={}  root={}'.format(root, state.get('round'),
		label(root, state.get('root'))))
	ended = load_ended(root)

	if ended is not None:
		print('ENDED {} ({}): winner {}; see ended.md'.format(
			ended['ended_at'], ended['reason']['text'], ended['winner']['label']))
	print('{} iterations settled, {} in flight, {} runs spent'.format(
		len(results), len(running),
		sum(r.get('n_runs') or 0 for r in results)))

	_, taken = taken_ids(root)
	print('next free id: {}{}'.format(LABEL_LETTER,
		max(taken) + 1 if taken else 0))

	untried, tried = idea_counts(root)
	mining = state.get('mining') or {}
	miner_open = mining.get('stage', 0) > mining.get('done', 0)
	stages = stage_budget(root, config, state)
	print('ideas: {} untried, {} tried; idea miner: {}; the next stage is '
		'called for below {} untried{}'.format(untried, tried,
		'stage S{} running since {}'.format(mining['stage'], mining.get(
			'started', '?')) if miner_open else 'stage S{} done'.format(
				mining['done']) if mining.get('done') else 'not run yet',
		MIN_UNTRIED, '; budget: {} idea-miner stage(s)'.format(
			stages['budget']) if stages['budget'] else ''))

	print('subagent model: {}'.format(config.get('subagent_model') or 'opus'))

	step2 = state.get('setup') or {}
	if step2.get('status') == 'waiting':
		print('setup (step 2): waiting for the user: {}. Ask them in one '
			'call, then `autoopt.py setup <work> --answer "<their words>"` and '
			'send the message it prints to the setup subagent (SendMessage); '
			'if it is gone, spawn the prompt printed after it'.format(
			step2.get('question')))
	elif step2.get('status') == 'open':
		print('setup (step 2): running since {}; nothing is briefed until it '
			'reports'.format(step2.get('started')))
	if step2 and (step2.get('status') != 'done' or not (state.get('mining')
			or {}).get('done')) and not [r for r in results if r.get('id')]:
		print('first iteration: waiting for {}'.format(' and '.join(name for name,
			ready in (('setup (step 2)', step2.get('status') == 'done'),
				('the idea miner\'s S1', bool((state.get('mining') or {}).get(
					'done')))) if not ready)))

	if hasattr(os, 'getloadavg'):
		load, cores = os.getloadavg()[0], os.cpu_count() or 1
		print('machine: load {:.1f} on {} cores, {:.0f}% busy, this search\'s '
			'runs included'.format(load, cores, 100 * load / cores))

	if stages['exhausted']:
		print('the budget of {} idea-miner stage(s) is spent and every idea '
			'is tried or dropped: the search stops for `budget` '
			'(references/step3-rounds.md, *When to stop*)'.format(
				stages['budget']))
	elif untried < MIN_UNTRIED and not miner_open and stages['spent']:
		print('the budget of {} idea-miner stage(s) is spent, so no new '
			'stage opens: brief the {} idea(s) left; once none is left the '
			'search stops for `budget`'.format(stages['budget'], untried))
	elif untried < MIN_UNTRIED and not miner_open:
		print('fewer than {} untried ideas: run `autoopt.py '
			'mine <work>` and spawn the idea miner with what it prints '
			'(references/step3-rounds.md, *Keeping the ideas*)'.format(
				MIN_UNTRIED))

	busy = working(root)

	if busy:
		print('subagents still working: ' + '; '.join('{} {} (last write '
			'{} ago{})'.format(label(root, i), kind, human_seconds(idle),
				', STALLED' if idle is not None and idle > STALLED_AFTER
				else '') for i, kind, idle in busy))
		if any(idle is not None and idle > STALLED_AFTER for _, _, idle in busy):
			print('  a STALLED subagent has written nothing for over {} min: '
				'hand its iteration to a new subagent rather than waiting '
				'(references/step3-rounds.md, *A subagent that goes quiet*)'
				.format(STALLED_AFTER // 60))

	remind_summaries(root, state)

	phase = loop_progress(root, config)
	if phase:
		print('orchestrator subagent: phase {}, since {}; {} of {} iterations '
			'settled in it{}'.format(phase['phase'], phase['started'],
				phase['settled'], phase['handback'], '; it hands back once the '
				'{} in flight have reported'.format(phase['in_flight'])
				if phase['reached'] else ''))

	if busy or pending_briefs(root):
		watching = live_watch(root)
		print('watch: {}'.format('running (pid {}, since {})'.format(
			watching['pid'], watching.get('started', '?')) if watching
			else 'none running: start `autoopt.py watch <work>` in the '
				'background before ending the turn'))

	unwritten = [i for i in iteration_ids(root) if load_result(root, i)
		is not None and not os.path.exists(os.path.join(iteration_dir(root,
			i), 'README.md')) and i not in {b[0] for b in busy}]

	if unwritten:
		print('no README: {}. Measured, and no subagent has written for over an '
			'hour: spawn a new subagent with the prompt `brief --prompt <id>` '
			'prints, to write its notes.md (references/step3-rounds.md, *A '
			'subagent that goes quiet*)'.format(', '.join(label(root, i)
				for i in unwritten)))

	if running and not live_publishers(root):
		print('dashboard: no publisher is running for this search. Start it: '
			'python3 {} {} --start'.format(os.path.join(os.path.dirname(
				os.path.abspath(__file__)), 'dashboard', 'autoopt_monitor.py'),
				root))

	print(standing(root, config, state, results))

	if args.round is not None:
		round_best, round_best_id = frontier(chosen, direction)
		print('best in round {}: {} (iteration {})'.format(args.round,
			fmt(round_best), label(root, round_best_id)))

	if args.brief or args.resume:
		last = sorted(chosen, key=lambda r: r['id'])[-args.last:]
		print()
		print(table(root, config, last, on_frontier, compact=True))
		print('(the last {} settled; the full table without --brief)'.format(
			len(last)))
		if args.resume:
			print('\n'.join(resume_lines(root, config, state)))
		return 0

	print()
	print(table(root, config, chosen, on_frontier))
	print()
	print('* improved on everything before it. Δ is against the baseline '
		'(iteration A0).')

	return 0


def prediction_note(root, result):
	"""'; predicted -0.030, measured -0.025 against the parent', or ''."""

	prediction = result.get('prediction')

	if not prediction:
		return ''

	value = (result.get('objective') or {}).get('median')
	parent = load_result(root, result.get('parent'))
	base = ((parent or {}).get('objective') or {}).get('median')
	measured = (fmt(value - base, 3) if value is not None
		and base is not None else '-')

	return '; predicted {}, measured {} against the parent{}'.format(
		fmt(prediction.get('delta'), 3), measured,
		' ({})'.format(prediction['basis']) if prediction.get('basis')
			else '')


###
# readme: an iteration's README.md, numbers from result.json
###

# The marker every generated skeleton leaves where a person or an agent has
# to write; `readme` refuses a file that still has one.
FILL = '<<FILL'

NOTES_REQUIRED = ('The idea', 'What happened', 'Notes and failures',
	'What to try next')

NOTES_TEMPLATE = """## The idea

<<FILL: what was changed, where (file and function), and why it might help>>

## What happened

<<FILL: the result in words>>

## Notes and failures

<<FILL: anything surprising: a seed that diverged, a dependency pinned, a
premise of the brief that was false, the share of the tolerance this spent>>

## What to try next

<<FILL: the follow-ups this result suggests, or "closed" and the number
that closes it>>
"""

def note_sections(text):
	"""`[(heading, body)]` for each `## ` section of notes.md, or a problem
	string. A `# ` title line before the first section is ignored."""

	sections, current, stray = [], None, []

	for line in text.splitlines():
		if line.startswith('## '):
			current = [line[3:].strip(), []]
			sections.append(current)
		elif current is not None:
			current[1].append(line)
		elif line.strip() and not line.startswith('# '):
			stray.append(line.strip())

	if stray:
		return 'notes.md has text before its first `## ` heading: {!r}; put it '\
			'under one'.format(stray[0][:60])

	names = [name for name, _ in sections]
	twice = sorted({name for name in names if names.count(name) > 1})

	if twice:
		return 'notes.md has more than one `## {}` section'.format(twice[0])

	return [(name, '\n'.join(body).strip()) for name, body in sections]


def change_fragment(root, identifier):
	"""What an iteration's subagent wrote about its change in notes.md, or
	None for each part it did not write: `{name, change, message}` from
	`## Change` (first line `name: <short name>`, then what the change does)
	and `## Commit message` (a subject, a blank line, a body)."""

	found = {'name': None, 'change': None, 'message': None}

	try:
		with open(os.path.join(iteration_dir(root, identifier), 'notes.md'),
				encoding='utf-8') as handle:
			sections = note_sections(handle.read())
	except OSError:
		return found

	if isinstance(sections, str):
		return found

	for heading, body in sections:
		if heading == 'Change' and body:
			first, _, rest = body.partition('\n')
			match = re.match(r'name:\s*(\S.*)$', first.strip())

			if match and rest.strip():
				found['name'], found['change'] = match.group(1).strip(), \
					rest.strip()
		elif heading == 'Commit message' and body:
			found['message'] = body.strip() + '\n'

	return found


def status_words(config, result):
	"""'ok at 5 runs' and the like."""

	runs, status = result.get('n_runs') or 0, result.get('status')

	if status == 'partial':
		return 'partial: {} run(s), fewer than the {} an iteration runs, so ' \
			'never compared to anything'.format(runs,
				config['runs_per_iteration'])

	return '{} at {} run{}{}'.format(status, runs, '' if runs == 1 else 's',
		'' if result.get('stage') == 'confirmed' else ', short of the full '
			'count')


def readme_text(root, config, identifier, sections):
	result = load_result(root, identifier)
	meta = load_meta(root, identifier) or {}
	objective = config['objective']
	unit = ' ({})'.format(objective['unit']) if objective.get('unit') else ''
	parent = meta.get('parent')

	fields = [('Category', category_of(dict(meta, id=identifier)) or '-'),
		('Parent', label(root, parent) if parent is not None
			else 'none: this is the baseline')]

	fields.append(('Round', str(result.get('round', meta.get('round')))))

	if result.get('prediction'):
		fields.append(('Prediction', prediction_note(root, result)
			.lstrip('; ')))

	fields.append(('Status', status_words(config, result)))

	if config.get('tests'):
		tests = result.get('tests')
		fields.append(('Unit tests', 'not run: a run crashed or a gate broke '
			'first' if not tests else '{}, {} suite'.format('pass'
				if tests.get('pass') else 'FAIL', 'the whole' if
				tests.get('scope') != 'subset' else 'a subset of the')))

	if result.get('check'):
		fields.append(('Check', check_line(result['check'])[len('check: '):]))

	header = ['seed', objective['name'] + unit]
	header += [gate['name'] for gate in config['gates']]
	header += [observer['name'] for observer in config['observers']]
	table_lines = ['| ' + ' | '.join(header) + ' |',
		'|' + '---|' * len(header)]

	for run in result.get('runs') or []:
		row = [str(run.get('seed')), fmt(run.get('objective'), 6)]
		row += [fmt((run.get('gates') or {}).get(g['name']), 6)
			for g in config['gates']]
		row += [fmt((run.get('observers') or {}).get(o['name']), 6)
			for o in config['observers']]
		table_lines.append('| ' + ' | '.join(row) + ' |')

	values = result.get('objective') or {}
	numbers = ['## Numbers', ''] + table_lines + ['']

	if result.get('failures'):
		numbers += ['Failed runs: ' + '; '.join('seed {}: {}'.format(
			f.get('seed'), f.get('error')) for f in result['failures']) + '.',
			'']

	sentence = ['Median {} **{}** over {} run(s), range {} to {}.'.format(
		objective['name'], fmt(values.get('median'), 6),
		result.get('n_runs'), fmt(values.get('min'), 6),
		fmt(values.get('max'), 6))]

	if result.get('frontier_at_start') is not None:
		sentence.append('The frontier was {} when it started; noise band '
			'{}.'.format(fmt(result['frontier_at_start'], 6),
				fmt(result.get('noise_band'), 4)))

	for gate in config['gates']:
		record = (result.get('gates') or {}).get(gate['name']) or {}
		if record:
			sentence.append('`{} {} {}`: worst {}, {}.'.format(gate['name'],
				gate['op'], gate['threshold'], fmt(record.get('worst'), 6),
				'pass' if record.get('pass') else 'FAIL on {} run(s)'.format(
					record.get('n_violations'))))

	numbers += [' '.join(sentence), '']

	lines = ['# {}: {}'.format(label(root, identifier), meta.get('title')
		or result.get('title') or ''), '']
	lines += ['- **{}:** {}'.format(name, value) for name, value in fields]
	lines.append('')

	body = dict(sections)

	for name in ('The idea', 'What happened'):
		lines += ['## ' + name, '', body[name], '']

	lines += numbers

	for name, text in sections:
		if name not in ('The idea', 'What happened'):
			lines += ['## ' + name, '', text, '']

	return '\n'.join(lines).rstrip('\n') + '\n'


def notes_problem(root, identifier):
	"""`(sections, None)` from an iteration's notes.md, ready for its
	README, or `(None, what is wrong)`."""

	directory = iteration_dir(root, identifier)
	result = load_result(root, identifier)

	if result is None:
		return None, 'iteration {} has no result.json yet: run it first'.format(
			label(root, identifier))

	notes = os.path.join(directory, 'notes.md')

	if not os.path.exists(notes):
		return None, 'no {}: write it with the sections in ' \
			'references/subagent.md, *The README*'.format(notes)

	with open(notes) as infile:
		text = infile.read()

	if FILL in text:
		return None, '{} still has {} placeholder(s); replace each'.format(
			notes, text.count(FILL))

	sections = note_sections(text)

	if isinstance(sections, str):
		return None, sections

	names = [name for name, _ in sections]
	needed = list(NOTES_REQUIRED)
	missing = [name for name in needed if name not in names]

	if missing:
		return None, '{} lacks {}'.format(notes, ', '.join('`## {}`'.format(
			name) for name in missing))

	empty = [name for name, body in sections if name in needed and not body]

	if empty:
		return None, '{} has an empty `## {}`'.format(notes, empty[0])

	lacking = fragment_problem(root, identifier, result)

	if lacking:
		return None, '{} {}'.format(notes, lacking)

	return sections, None


def fragment_problem(root, identifier, result=None):
	"""What a kept iteration of a repository search (or one made after the
	end) has not written about its change in notes.md, or None: `## Change`
	and `## Commit message` describe it for whoever takes the winner
	upstream, so nobody has to read the diff again."""

	result = result or load_result(root, identifier) or {}
	meta = load_meta(root, identifier) or {}

	if not read_json(os.path.join(root, 'config.json')).get('repo') or \
			result.get('control') or meta.get('control') or not identifier:
		return None

	if (result.get('keep') or {}).get('verdict') != 'pass' and not meta.get(
			'after_end'):
		return None

	fragment = change_fragment(root, identifier)
	lacking = [heading for heading, key in (('## Change` (first line `name: '
		'<short name>`, then what the change does)', 'change'), ('## Commit '
		'message`', 'message')) if not fragment[key]]

	if not lacking:
		# Linted here, where its author can still reword them: `promote` ran
		# before an ordinary iteration wrote them.
		import lint_text
		labels = [label(root, i) for i in iteration_ids(root)]
		found = lint_text.problems('{}\n{}'.format(fragment['name'],
			fragment['change']), '## Change', labels) + lint_text.problems(
			fragment['message'], '## Commit message', labels) + [
			'## Commit message: {}'.format(item) for item in message_problems(
			root, with_trailer(fragment['message'], commit_trailer(root)))]

		return 'breaks the rules of text read outside the search (references/subagent.md, ' \
			'*The README*): {}'.format('; '.join(found)) if found else None

	return ('lacks `{}: a kept iteration writes both: they describe the '
		'change upstream (references/subagent.md, *The README*)'.format(
		' and `'.join(lacking)))


def write_readme(root, config, identifier):
	"""Assemble `README.md` from notes.md and result.json. Returns None, or
	what is wrong."""

	sections, problem = notes_problem(root, identifier)

	if problem:
		return problem

	with open(os.path.join(iteration_dir(root, identifier), 'README.md'),
			'w') as out:
		out.write(readme_text(root, config, identifier, sections))

	return None


PRICE_FLAG_HOURS = 1.0
# A search's gains have reached 10x to 70x: an A0 under this many floors is
# likely to end there.
FLOOR_TIMES = 20


def price_lines(root, config):
	"""What each later step that runs the base costs, from A0's own runs:
	a slow base is paid for again at `end` and by anything that measures
	the winner afterwards, and the time to say so is setup. [] before A0 has a run."""

	result = load_result(root, 0) or {}
	runs = result.get('n_runs') or 0

	if not runs or not result.get('seconds'):
		return []

	after = dict(AFTER_DEFAULTS, **(config.get('after') or {}))
	one = result['seconds'] / runs
	tests = (result.get('tests') or {}).get('seconds') or 0.0
	depth = config.get('confirm_runs') or config.get('runs_per_iteration') or 1
	rows = [('one run of the benchmark on the base', one),
		('an iteration at the base\'s speed ({} run(s))'.format(depth),
			depth * one)]

	if (config.get('checks') or {}).get('time'):
		rows.append(('the paired timing at `end`, the base\'s side alone (2 '
			'unrecorded + 8 to 24 pairs)', 9 * one))

		if config['checks'].get('threads'):
			rows.append(('the same at checks.threads, when one thread is no '
				'slower', 9 * one))

	if config.get('repo'):
		rows.append(('a re-measurement of the base outside the search ({} '
			'run(s) and the tests)'.format(depth), depth * one + tests))

	text = lambda seconds: '{:.1f} h'.format(seconds / 3600) if seconds >= \
		3600 else '{:.1f} min'.format(seconds / 60) if seconds >= 60 else \
		'{:.1f} s'.format(seconds)
	slow = [name for name, seconds in rows if seconds >= PRICE_FLAG_HOURS
		* 3600]
	lines = ['price of the base (A0: {} run(s), {} a run):'.format(runs,
		text(one))] + ['  {} {:>9}  {}'.format('!' if seconds >=
		PRICE_FLAG_HOURS * 3600 else ' ', text(seconds), name) for name,
		seconds in rows]
	floor = config.get('floor')
	median = (result.get('objective') or {}).get('median')

	if floor and median and config['objective'].get('direction') == 'min':
		times = median / floor
		lines.append('  {} A0 is {:.3g} times the floor ({} against {})'.format(
			'!' if times < FLOOR_TIMES else ' ', times, fmt(median), fmt(floor)))

		if times < FLOOR_TIMES:
			lines.append('  ! a gain of {:.3g}x ends the search at the floor: '
				'size the input so A0 is {} to 50 times it'.format(times,
				FLOOR_TIMES))
			slow.append('the floor')

	cap = after.get('pair_cap_hours')

	if cap and (config.get('checks') or {}).get('time') and 8 * one > cap * \
			3600:
		lines.append('  ! the paired timing passes after.pair_cap_hours ({:g} '
			'h): `end` will refuse it'.format(cap))
		slow.append('the cap')

	if slow:
		lines.append('{} marked line(s). Ask the user now (`setup --ask`): for '
			'an hour or more, a smaller input for the timing probe '
			'(checks.time), or these prices as '
			'they are; when `end` will refuse, that smaller input or a higher '
			'after.pair_cap_hours, never "as they are"; for the floor, a '
			'larger input (references/'
			'step2-baseline.md, *The price of the base*)'.format(len(slow)))

	return lines


def price(args):
	root = resolve(args.root)
	lines = price_lines(root, load_config(root))

	if not lines:
		fail('A0 has no finished run yet: run the baseline first')

	print('\n'.join(lines))
	return 0


def after_readme(root, config, identifier):
	"""What a new README sets off: the struck `ideas.md` line and any closed
	round's summary. Returns the lines to print."""

	out = []
	struck = strike_idea(root, load_state(root), identifier)
	if struck:
		out.append('struck in ideas.md: ' + struck)

	for path in summarize_closed_rounds(root, config):
		out.append('wrote the round summary {}'.format(path))

	return out


def readme(args):
	root = resolve(args.root)
	config = load_config(root)
	identifier = args.id
	directory = iteration_dir(root, identifier)

	if not os.path.isdir(directory):
		fail('iteration {} does not exist'.format(identifier))

	if args.template:
		notes = os.path.join(directory, 'notes.md')
		if os.path.exists(notes):
			fail('{} already exists; edit it, then run `autoopt.py readme` '
				'without --template'.format(notes))
		result = load_result(root, identifier) or {}
		with open(notes, 'w') as out:
			out.write(NOTES_TEMPLATE)
		print(notes)
		return 0

	problem = write_readme(root, config, identifier)

	if problem:
		fail(problem)

	print(os.path.join(directory, 'README.md'))

	for line in after_readme(root, config, identifier):
		print(line)

	return 0


# The prompt cache lives five minutes. An agent's turn after a longer gap
# writes its whole context to the cache again, at 25 times the price of
# reading it, so no wait may outlast this. The default stays inside the Bash
# tool's 240000 ms timeout the protocol gives a foreground call.
WAIT_LIMIT = 240
WAIT_DEFAULT = 200
WAIT_SHOWN = 60
WAIT_EXIT = re.compile(r'^exit (-?\d+)$')


###
# finish: an iteration's last step in one call
###

# The keys of `finish --checker-row`, in the checkers README's column order;
# `time` replaces running `run` for a checker that cannot run in four
# minutes.
CHECKER_KEYS = ('name', 'files', 'compares', 'built', 'run', 'faults',
	'inputs')
CHECKER_LIMIT = WAIT_LIMIT


def checker_fields(path, need=CHECKER_KEYS):
	"""`{key: value}` from a `key: value` file, one key a line, with every
	key in `need` (`run` may be `time`)."""

	fields = {}

	with open(os.path.expanduser(path)) as infile:
		for line in infile:
			if not line.strip():
				continue
			key, colon, value = line.partition(':')
			key = key.strip().lower()
			if not colon or key not in CHECKER_KEYS + ('time',):
				fail('{}: "{}" is not a `key: value` line with a key from {}'
					.format(path, line.rstrip(), ', '.join(CHECKER_KEYS
						+ ('time',))))
			fields[key] = value.strip()

	missing = [key for key in need if not fields.get(key)
		and not (key == 'run' and fields.get('time'))]

	if missing:
		fail('{} lacks {}'.format(path, ', '.join(missing)))

	fields['name'] = re.sub(r'^code:', '', fields['name'])

	if not re.match(r'^[A-Za-z0-9_.-]+$', fields['name']):
		fail('{}: name is the subsystem, one word such as `distance`; got '
			'{!r}'.format(path, fields['name']))

	return fields


def run_checker(root, config, identifier, source, fields):
	"""Run the checker from where it was written, the parent as {parent}
	and this iteration as {child}; returns its run time in seconds. A
	checker that fails on the iteration that wrote it is not registered."""

	meta = load_meta(root, identifier) or {}
	parent = meta.get('parent')
	folder = source if os.path.isdir(source) else os.path.dirname(source)
	parent_tree, child_tree = checker_trees(root, config, identifier,
		measured=True)
	line = fill(fields['run'], checker=folder, parent=parent_tree,
		child=child_tree)
	log = os.path.join(iteration_dir(root, identifier), 'probe',
		'finish-checker.log')
	os.makedirs(os.path.dirname(log), exist_ok=True)
	start = time.time()

	with open(log, 'w') as out:
		out.write('$ {}\n\n'.format(line))
		out.flush()
		code, timed_out = run_shell(line, iteration_dir(root, identifier),
			out, timeout=CHECKER_LIMIT)

	seconds = time.time() - start

	if timed_out:
		fail('the checker ran past {} s ({}); time it yourself and give '
			'`time:` in place of `run:`. Nothing was written'.format(
				CHECKER_LIMIT, log))

	if code != 0:
		fail('the checker exits {} on {} against its parent {} ({}): a '
			'checker must pass on the iteration that wrote it. Nothing was '
			'written'.format(code, label(root, identifier), label(root, parent),
				log))

	return seconds


def register_checker(root, identifier, source, fields, seconds):
	"""Copy the checker into `tools/checkers/<name>-<label>/` and put its
	row in the checker library's README, under its lock, replacing an
	earlier row of the same name."""

	me = label(root, identifier)
	name = '{}-{}'.format(fields['name'], me)
	target = os.path.join(root, 'tools', 'checkers', name)
	ignore = shutil.ignore_patterns('__pycache__', '*.pyc', '*.log', '*.seen')

	if os.path.isdir(source):
		shutil.copytree(source, target, ignore=ignore, dirs_exist_ok=True)
	else:
		os.makedirs(target, exist_ok=True)
		shutil.copy2(source, target)

	folder = 'tools/checkers/{}'.format(name)
	run = fields.get('run') or ''
	run = run.replace('{checker}', folder).replace('{parent}',
		'<parent src>').replace('{child}', '<child src>')
	took = human_seconds(seconds) if seconds is not None else fields['time']

	# The full row, with the run line as the checkers command fills it.
	with open(os.path.join(target, 'ROW.md'), 'w') as out:
		out.write('# Checker {}\n\n'.format(name))
		for key in CHECKER_KEYS + ('time',):
			value = fields.get(key) if key != 'time' else took
			if value:
				out.write('{}: {}\n'.format(key, value))

	readme_path = os.path.join(root, 'tools', 'checkers', 'README.md')

	with open(readme_path, 'r+') as handle:
		fcntl.flock(handle, fcntl.LOCK_EX)
		lines = handle.read().rstrip('\n').split('\n')
		if any('Planted-fault controls' in line for line in lines):
			# A library begun before ROW.md: its rows keep every column.
			cells = [name, fields['compares'], fields['built'], '`{}`'.format(
				run) if run else '(not run here)', fields['faults'],
				fields['inputs'], took]
		else:
			cells = [name, fields['files'], first_sentence(fields['compares'],
				160), took]
		row = '| ' + ' | '.join(cell.replace('|', '\\|') for cell in cells) \
			+ ' |'
		lines = [line for line in lines if not line.startswith('| {} |'.format(
			name))] + [row]
		handle.seek(0)
		handle.write('\n'.join(lines) + '\n')
		handle.truncate()

	return 'registered the checker {}/ ({})'.format(folder, took)


def export_code(checkout, treeish, target):
	"""Write the files of `treeish` (a commit, a branch or a tree) in the
	repository of `checkout` into `target`, which must not exist: tracked
	files only, no `.git` pointer."""

	import tarfile

	os.makedirs(target)
	archive = subprocess.Popen(['git', '-C', checkout, 'archive', treeish],
		stdout=subprocess.PIPE)
	with tarfile.open(fileobj=archive.stdout, mode='r|') as tar:
		tar.extractall(target, filter='data') if hasattr(tarfile,
			'data_filter') else tar.extractall(target)
	# tarfile stops at the end-of-archive marker, and git may still have a
	# whole 10240-byte record of padding to write: drain it, or git blocks on
	# a full pipe (8 KiB once the user passes fs.pipe-user-pages-soft) and the
	# wait below never returns.
	while archive.stdout.read(65536):
		pass
	archive.stdout.close()
	if archive.wait() != 0:
		fail('git archive {} failed in {}'.format(treeish, checkout))


def current_tree(checkout):
	"""The tree of a worktree's current edit: everything `git add -A`
	stages, which the runner would commit. Staged in a private copy of the
	index, so two calls at once (a `prove` beside a `checkers`) never meet on
	the worktree's `index.lock`, and the worktree's own index is untouched."""

	index = git('-C', checkout, 'rev-parse', '--git-path', 'index').strip()
	index = os.path.join(checkout, index) if not os.path.isabs(index) else index
	handle, private = tempfile.mkstemp(prefix='autoopt-index.')
	os.close(handle)

	try:
		if os.path.exists(index):
			# copy2 keeps the index's mtime: git re-reads a file changed in the
			# second the index was written only while the index is no newer.
			shutil.copy2(index, private)
		else:
			os.remove(private)
		env = dict(os.environ, GIT_INDEX_FILE=private)
		for command in (['add', '-A'], ['write-tree']):
			done = subprocess.run(['git', '-C', checkout] + command, env=env,
				capture_output=True, text=True)
			if done.returncode != 0:
				fail('git {} failed in {}: {}'.format(' '.join(command), checkout,
					done.stderr.strip()))
		return done.stdout.strip()
	finally:
		if os.path.exists(private):
			os.remove(private)


def build_code(config, target):
	"""Build an exported tree in place with `checks.build`, once: its log is
	`<target>.build.log`, ending `exit <code>`. Returns None when it built
	(or the search has no build), or what went wrong."""

	command = (config.get('checks') or {}).get('build')

	if not command:
		return None

	log_path = target.rstrip(os.sep) + '.build.log'

	try:
		with open(log_path) as handle:
			done = handle.read().rstrip().rsplit('\n', 1)[-1]
		if done == 'exit 0':
			return None
	except OSError:
		pass

	line = fill(command, src=target)

	with open(log_path, 'w') as out:
		out.write('$ {}\n\n'.format(line))
		out.flush()
		code, timed_out = run_shell(line, target, out,
			timeout=config.get('timeout_seconds'))
		out.write('\nexit {}\n'.format(code))

	if code == 0:
		return None
	return '{} (see {})'.format('the build timed out' if timed_out
		else 'the build exits {}'.format(code), log_path)


def checker_trees(root, config, identifier, measured=False):
	"""`(parent, child)`: the two trees a checker of iteration `identifier`
	runs on. In a search with `checks.build`, exported copies of the
	parent's commit and this iteration's current edit (with `measured`, the
	commit the runner measured), built with it, under `probe/trees/<tree>/`
	(once per tree); otherwise the two worktrees."""

	meta = load_meta(root, identifier) or {}
	parent = meta.get('parent')
	checkout = checkout_dir(root, identifier, config)

	if not (config.get('checks') or {}).get('build'):
		return checkout_dir(root, parent, config), checkout

	folder = os.path.join(iteration_dir(root, identifier), 'probe', 'trees')
	trees = []

	child = (git('-C', checkout, 'rev-parse', '{}^{{tree}}'.format(
		meta['commit'])).strip() if measured and meta.get('commit')
		else current_tree(checkout))

	for treeish in (git('-C', checkout, 'rev-parse', 'autoopt/{}^{{tree}}'
			.format(parent)).strip(), child):
		target = os.path.join(folder, treeish[:12])
		if not os.path.isdir(target):
			export_code(checkout, treeish, target)
		problem = build_code(config, target)
		if problem:
			fail('{}: {}'.format(target, problem))
		trees.append(target)

	return tuple(trees)


def variant(args):
	"""Export an iteration's current edit (or, with --parent, its parent's
	code) into `probe/<name>/`, and build it there with `checks.build` when
	the search registers one."""

	root = resolve(args.root)
	config = load_config(root)
	identifier = args.id
	directory = iteration_dir(root, identifier)

	if not os.path.isdir(directory):
		fail('iteration {} does not exist'.format(identifier))

	if not re.match(r'^[A-Za-z0-9_.-]+$', args.name):
		fail('the name is one word, such as v2 or no_simd; got {!r}'.format(
			args.name))

	target = os.path.join(directory, 'probe', args.name)

	if os.path.exists(target):
		fail('{} exists: give a new name (nothing is deleted)'.format(target))

	checkout = checkout_dir(root, identifier, config)
	parent = (load_meta(root, identifier) or {}).get('parent')
	treeish = ('autoopt/{}'.format(parent) if args.from_parent
		else current_tree(checkout))
	export_code(checkout, treeish, target)
	problem = None if args.no_build else build_code(config, target)
	built = (config.get('checks') or {}).get('build') and not args.no_build

	print(target)
	if problem:
		print('variant: the copy is made, but {}'.format(problem))
		return 1
	print('variant: {}{}'.format('the parent\'s code ({})'.format(label(root,
		parent)) if args.from_parent else 'your edit as it stands', ', built ({})'
		.format(target.rstrip(os.sep) + '.build.log') if built else ''))
	return 0


# A planted fault in a `prove --faults` file:
#     ## <name>
#     file: <path in the tree>
#     <<<
#     <the exact text to replace>
#     ===
#     <its replacement>
#     >>>
FAULT_OPEN, FAULT_SPLIT, FAULT_CLOSE = '<<<', '===', '>>>'


def read_faults(path):
	"""`[(name, file, old, new)]` from a faults file."""

	faults, current, block, part = [], None, None, None

	for raw in open(os.path.expanduser(path)).read().split('\n'):
		line = raw.rstrip('\r')
		if block is not None:
			if line == FAULT_SPLIT and part == 'old':
				part = 'new'
				continue
			if line == FAULT_CLOSE and part == 'new':
				current['old'], current['new'] = ('\n'.join(block['old']),
					'\n'.join(block['new']))
				block = part = None
				continue
			block[part].append(line)
			continue
		if line.startswith('## '):
			current = {'name': line[3:].split(':')[0].strip()}
			faults.append(current)
		elif line.startswith('file:') and current is not None:
			current['file'] = line[5:].strip()
		elif line == FAULT_OPEN and current is not None:
			block, part = {'old': [], 'new': []}, 'old'

	if block is not None:
		fail('{}: a fault\'s <<< block is not closed with >>>'.format(path))

	problems = [fault.get('name', '?') for fault in faults
		if not all(fault.get(key) is not None for key in ('file', 'old', 'new'))
		or not re.match(r'^[A-Za-z0-9_.-]+$', fault.get('name', ''))]

	if not faults or problems:
		fail('{}: {}; each fault is `## <name>` (one word), `file: <path>`, then '
			'<<< the exact old text === the new text >>> on lines of their own'
			.format(path, 'no fault found' if not faults else 'incomplete: '
				+ ', '.join(problems)))

	return [(fault['name'], fault['file'], fault['old'], fault['new'])
		for fault in faults]


def proof_path(root, identifier):
	return os.path.join(iteration_dir(root, identifier), 'probe',
		'checker-proof.json')


def prove_checker(root, config, args):
	"""`prove --id`: run an iteration's checker on its parent and its own
	code, which must pass, then on one copy of its code per planted fault,
	each of which it should catch. Writes the table to
	`probe/checker-proof.json`, which `finish` takes the row's `faults`
	from."""

	identifier = args.id
	source = os.path.abspath(os.path.expanduser(args.checker))

	if not os.path.exists(source):
		fail('no checker at {}'.format(source))

	fields = checker_fields(args.checker_row, need=('name', 'run'))
	faults = read_faults(args.faults)
	parent_tree, child_tree = checker_trees(root, config, identifier)
	checkout = checkout_dir(root, identifier, config)
	folder = tempfile.mkdtemp(prefix='prove-{}.'.format(fields['name']),
		dir=os.path.join(iteration_dir(root, identifier), 'probe'))
	edit = current_tree(checkout)
	checker_dir = source if os.path.isdir(source) else os.path.dirname(source)

	def run(child, name):
		line = fill(fields['run'], checker=checker_dir, parent=parent_tree,
			child=child)
		log = os.path.join(folder, name + '.log')
		with open(log, 'w') as out:
			out.write('$ {}\n\n'.format(line))
			out.flush()
			code, timed_out = run_shell(line, iteration_dir(root, identifier),
				out, timeout=args.timeout)
		return code, timed_out, log

	code, timed_out, log = run(child_tree, 'clean')
	if code != 0:
		fail('the checker {} on your own code against the parent ({}): it '
			'must pass there before a fault means anything'.format(
				'timed out' if timed_out else 'exits {}'.format(code), log))

	rows = []
	for name, path, old, new in faults:
		target = os.path.join(folder, name)
		export_code(checkout, edit, target)
		file_path = os.path.join(target, path)
		try:
			text = open(file_path).read()
		except OSError:
			fail('fault {}: {} is not in the tree'.format(name, path))
		if text.count(old) != 1:
			fail('fault {}: its old text occurs {} times in {}, not once: the '
				'fault would not land'.format(name, text.count(old), path))
		with open(file_path, 'w') as out:
			out.write(text.replace(old, new))
		problem = build_code(config, target)
		if problem:
			rows.append({'fault': name, 'verdict': 'not built', 'detail':
				problem})
			continue
		code, timed_out, log = run(target, name)
		rows.append({'fault': name, 'verdict': 'timed out' if timed_out
			else 'caught' if code != 0 else 'MISSED', 'exit': code, 'log': log})

	summary = '; '.join('{}: {}'.format(row['fault'], row['verdict'])
		for row in rows)
	write_json(proof_path(root, identifier), {'name': fields['name'],
		'checker': source, 'faults': rows, 'summary': summary,
		'written': now()})

	print('checker {} on {} (parent {}), copies under {}:'.format(
		fields['name'], label(root, identifier), label(root, (load_meta(root,
			identifier) or {}).get('parent')), folder))
	print('  clean      passes')
	for row in rows:
		print('  {:<10} {}{}'.format(row['fault'], row['verdict'],
			'  ({})'.format(row.get('detail') or row.get('log'))))
	print('finish takes the row\'s faults from {}'.format(proof_path(root,
		identifier)))

	return 0


def registered_checkers(root):
	"""`[(name, fields or None)]` for every directory under tools/checkers/,
	its fields read from ROW.md; None for one registered before ROW.md."""

	folder = os.path.join(root, 'tools', 'checkers')
	out = []

	for name in sorted(os.listdir(folder) if os.path.isdir(folder) else []):
		if not os.path.isdir(os.path.join(folder, name)):
			continue
		fields = None
		try:
			with open(os.path.join(folder, name, 'ROW.md')) as handle:
				fields = {}
				for line in handle:
					key, colon, value = line.partition(':')
					if colon and key.strip() in CHECKER_KEYS + ('time',):
						fields[key.strip()] = value.strip()
		except OSError:
			pass
		out.append((name, fields))

	return out


def changed_files(root, config, identifier):
	"""The files an iteration's current edit changes against its parent."""

	checkout = checkout_dir(root, identifier, config)
	parent = (load_meta(root, identifier) or {}).get('parent')

	return [line for line in git('-C', checkout, 'diff', '--name-only',
		'autoopt/{}'.format(parent), current_tree(checkout)).splitlines()
		if line.strip()]


def guards(fields, changed):
	"""Whether a checker's `files` (paths, directories or globs, comma- or
	space-separated) cover any changed file."""

	import fnmatch

	patterns = [item.strip().strip('`') for item in re.split(r'[,\s]+',
		(fields or {}).get('files') or '') if item.strip()]

	return any(fnmatch.fnmatch(path, pattern) or path.startswith(
		pattern.rstrip('/') + '/') for path in changed for pattern in patterns)


def checkers(args):
	"""List the registered checkers, marking those that guard a file the
	iteration changed; with --name or --touched, run them on its parent and
	its current edit, one log each, and say which passed."""

	root = resolve(args.root)
	config = load_config(root)
	identifier = args.id
	known = registered_checkers(root)
	changed = changed_files(root, config, identifier)

	if args.name:
		names = {name for name, _ in known}
		unknown = [name for name in args.name if name not in names]
		if unknown:
			fail('no checker {} under tools/checkers/; there are: {}'.format(
				', '.join(unknown), ', '.join(sorted(names)) or 'none'))
		chosen = [(name, fields) for name, fields in known
			if name in args.name]
	elif args.touched:
		chosen = [(name, fields) for name, fields in known
			if guards(fields, changed)]
	else:
		print('{} changes: {}'.format(label(root, identifier), ', '.join(
			changed) or 'nothing yet'))
		for name, fields in known:
			print('  {:<24} {}{}'.format(name, '(registered before ROW.md: '
				'run it by hand from its README row)' if fields is None
				else 'guards {}; {}'.format(fields.get('files') or '?',
					fields.get('time') or '?'), '  TOUCHED' if guards(fields,
						changed) else ''))
		if not known:
			print('  no checker is registered yet')
		return 0

	if not chosen:
		print('checkers: none guards a file {} changes ({})'.format(
			label(root, identifier), ', '.join(changed) or 'nothing yet'))
		return 0

	parent_tree, child_tree = checker_trees(root, config, identifier)
	logs = os.path.join(iteration_dir(root, identifier), 'probe', 'checkers')
	os.makedirs(logs, exist_ok=True)
	failed = 0

	for name, fields in chosen:
		if fields is None or not fields.get('run'):
			print('  {:<24} not run: {}'.format(name, 'registered before '
				'ROW.md' if fields is None else 'it has no run line (time: '
				'{})'.format(fields.get('time'))))
			continue
		line = fill(fields['run'], checker=os.path.join(root, 'tools',
			'checkers', name), parent=parent_tree, child=child_tree)
		log = os.path.join(logs, name + '.log')
		start = time.time()
		with open(log, 'w') as out:
			out.write('$ {}\n\n'.format(line))
			out.flush()
			code, timed_out = run_shell(line, iteration_dir(root, identifier),
				out, timeout=args.timeout)
		failed += code != 0
		print('  {:<24} {} in {} ({})'.format(name, 'timed out' if timed_out
			else 'pass' if code == 0 else 'FAIL (exit {})'.format(code),
			human_seconds(time.time() - start), log), flush=True)

	return 1 if failed else 0


def finish(args):
	"""Close an iteration: README.md from notes.md, a checker registered in
	the library, ledger entries appended, then the block to report. Every
	part is checked before anything is written, so a refusal leaves the
	search as it was and the same call can be made again."""

	root = resolve(args.root)
	config = load_config(root)
	identifier = args.id
	directory = iteration_dir(root, identifier)

	if not os.path.isdir(directory):
		fail('iteration {} does not exist'.format(identifier))

	if bool(args.checker) != bool(args.checker_row):
		fail('--checker and --checker-row go together: the checker\'s file or '
			'directory, and its `key: value` row')

	sections, problem = notes_problem(root, identifier)

	if problem:
		fail(problem + '. Nothing was written')

	answer, problem = toolchain_problem(root, identifier, sections)

	if problem:
		fail(problem + '. Nothing was written')

	if args.ledger:
		ledger_plan(root, args.ledger)

	fields = seconds = None

	if args.checker:
		source = os.path.abspath(os.path.expanduser(args.checker))
		if not os.path.exists(source):
			fail('no checker at {}'.format(source))
		if (load_meta(root, identifier) or {}).get('parent') is None:
			fail('{} has no parent to point a checker at'.format(label(root,
				identifier)))
		fields = checker_fields(args.checker_row, need=tuple(key
			for key in CHECKER_KEYS if key != 'faults'))
		if not fields.get('faults'):
			try:
				with open(proof_path(root, identifier)) as handle:
					proof = json.load(handle)
			except (OSError, ValueError):
				proof = {}
			if proof.get('name') != fields['name']:
				fail('{} has no faults line, and no `prove --id` table for '
					'the checker {} is in {}: prove it on planted faults or '
					'write the line. Nothing was written'.format(
						args.checker_row, fields['name'], proof_path(root,
							identifier)))
			fields['faults'] = proof['summary'] + ' (prove --id)'
		if fields.get('run'):
			seconds = run_checker(root, config, identifier, source, fields)

	with open(os.path.join(directory, 'README.md'), 'w') as out:
		out.write(readme_text(root, config, identifier, sections))

	done = ['wrote {}'.format(os.path.join(directory, 'README.md'))]

	if answer:
		meta = load_meta(root, identifier) or {}
		meta['toolchain'] = 'specific' if answer == 'yes' else 'no'
		write_meta(root, identifier, meta)

		if answer == 'yes':
			done.append('toolchain-specific: promote will refuse it; close '
				'the idea in the ledger with the compiler named')

	done += after_readme(root, config, identifier)

	if fields:
		done.append(register_checker(root, identifier, source, fields,
			seconds))

	if args.ledger:
		done.append(ledger_append(root, args.ledger, label(root, identifier)))

	for line in done:
		print('finish: ' + line)

	try:
		with open(os.path.join(directory, 'report.txt')) as infile:
			block = infile.read().strip('\n')
	except OSError:
		block = ''

	print('\nReport this block back, then at most two sentences '
		'(references/subagent.md, *What to report back*):\n')
	print(block)

	return 0


# A round summary's three sections, assembled by `summary` from the records:
# no agent writes them, and no number in them is typed by hand.
SUMMARY_PROSE = ('What this round tested', 'What landed and why', 'Next round')


def first_sentence(text, limit=300):
	"""The first sentence of a README section, on one line."""

	text = ' '.join((text or '').split())
	match = re.search(r'(.+?[.!?])(\s|$)', text)
	sentence = match.group(1) if match else text
	return sentence if len(sentence) <= limit else sentence[:limit - 1] + '…'


def keep_phrase(root, state, result):
	"""What became of one iteration, from its records."""

	keep = result.get('keep') or {}
	roots = {entry['id'] for entry in state.get('roots', [])}

	if result.get('control'):
		return 'a same-code control, never on the frontier'
	if result.get('status') != 'ok':
		return '{}{}'.format(result.get('status'), ' (failed check: {})'
			.format(result['failed_check']) if result.get('failed_check')
			else '')
	if keep.get('promoted') or result['id'] in roots:
		return 'promoted' + (': ' + keep['why'] if keep.get('why') else '')
	if keep.get('verdict') == 'pass':
		return 'passed the keep rule ({}), not promoted'.format(keep['why'])
	if keep.get('why'):
		return 'kept out: ' + keep['why']
	return 'no keep verdict recorded'


def assemble_prose(root, config, state, chosen):
	"""`{heading: text}` for the three sections, from each iteration's
	records and README and from ideas.md."""

	tested, landed = [], []

	for result in sorted(chosen, key=lambda r: r.get('id', 0)):
		name = label(root, result['id'])
		path = os.path.join(iteration_dir(root, result['id']), 'README.md')
		sections = {}
		if os.path.exists(path):
			with open(path) as infile:
				sections = markdown_sections(infile.read())
		tested.append('- **{}** {}: {}'.format(name, result.get('title') or '',
			first_sentence(sections.get('The idea')) or '(no README)'))
		landed.append('- **{}**: {}. {}'.format(name, keep_phrase(root, state,
			result), first_sentence(sections.get('What happened'))).rstrip())

	try:
		with open(os.path.join(root, 'ideas.md')) as handle:
			untried = [line[2:].strip() for line in handle.read().splitlines()
				if IDEA_LINE.match(line)]
	except OSError:
		untried = []

	upcoming = ('The next untried ideas in `ideas.md`:\n\n' + '\n'.join(
		'- ' + line for line in untried[:3]) if untried else
		'`ideas.md` holds no untried idea: the idea miner opens the next stage.')

	return {'What this round tested': '\n'.join(tested),
		'What landed and why': '\n'.join(landed), 'Next round': upcoming}


def markdown_sections(text):
	"""`{heading: body}` for each `## ` section of a markdown file."""

	out, current = {}, None

	for line in text.splitlines():
		if line.startswith('## '):
			current = line[3:].strip()
			out[current] = []
		elif current is not None:
			out[current].append(line)

	return {name: '\n'.join(body).strip() for name, body in out.items()}


def summary(args):
	"""Write `rounds/round-<N>.md` and print it."""

	root = resolve(args.root)
	config = load_config(root)
	path, lines = write_round_summary(root, config, args.round)

	if path is None:
		fail('no settled iteration carries round {}'.format(args.round))

	print(path)
	print()
	print('\n'.join(lines))

	return 0


def summarize_closed_rounds(root, config):
	"""Write the summary of every closed round whose iterations have all
	settled and written their README, and that has none yet: called by
	`readme` and `promote`, so no one writes it by hand.
	Returns the paths written."""

	state = load_state(root)
	written = []

	for number in overdue_summaries(root, state):
		members = [identifier for identifier in iteration_ids(root)
			if identifier
			and (load_meta(root, identifier) or {}).get('round') == number]
		if all(os.path.exists(os.path.join(iteration_dir(root, identifier),
				'README.md')) for identifier in members):
			path, _ = write_round_summary(root, config, number)
			if path:
				written.append(path)

	return written


def write_round_summary(root, config, round_number):
	"""`rounds/round-<N>.md`: the table, a line per idea, the round's
	validation and the three assembled sections. `(None, None)` when no
	settled iteration carries the round."""

	state = load_state(root)
	results = all_results(root)

	chosen = [r for r in results if r.get('round') == round_number]

	if not chosen:
		return None, None

	direction = config['objective']['direction']
	best, best_id = frontier(results, direction)
	round_best, round_best_id = frontier(chosen, direction)

	on_frontier = {r['id'] for r in frontier_path(results, direction)}

	lines = ['# Round {}'.format(round_number), '',
		'{} iterations, {} runs. Objective: {} ({}).'.format(len(chosen),
			sum(r.get('n_runs') or 0 for r in chosen),
			config['objective']['name'], direction), '',
		'Best this round: {} (iteration {}). Best overall: {} '
		'(iteration {}).'.format(fmt(round_best), label(root, round_best_id),
			fmt(best), label(root, best_id)), '', '```', table(root, config, chosen, on_frontier),
		'```', '',
		'## Ideas', '']

	for result in sorted(chosen, key=lambda r: r.get('id', 0)):
		lines.append('- **{}** ({}, iteration {}, parent {}): {}{}'.format(
			result.get('title') or '?', category_of(result),
			label(root, result['id']),
			label(root, result.get('parent')), result.get('status'),
			prediction_note(root, result)))

	lines.append('')

	if os.path.isdir(os.path.join(root, 'repo.git')):
		attested = []

		for result in sorted(chosen, key=lambda r: r.get('id', 0)):
			if (load_meta(root, result['id']) or {}).get('toolchain') == 'no':
				flagged = toolchain_flags(root, result['id'])

				if flagged:
					attested.append('- {}: answered `no` for {} (`{}`)'.format(
						label(root, result['id']), flagged[0][0],
						flagged[0][1]))

		if attested:
			lines += ['## Toolchain answers to check', '', 'These changes '
				'touch a compiler hint; the subagent\'s own answer is `no`, '
				'and nothing else checked it.', ''] + attested + ['']

	record = load_validation(root, round_number)

	if record is not None:
		lines += ['## External validation', '',
			'Recorded, never gated on: it chose nothing and rejected '
			'nothing.', '', '```', validation_block(config, record), '```',
			'']

	path = os.path.join(root, 'rounds', round_stem(round_number) + '.md')
	prose = assemble_prose(root, config, state, chosen)

	for heading in SUMMARY_PROSE:
		lines += ['## ' + heading, '', prose[heading], '']

	os.makedirs(os.path.dirname(path), exist_ok=True)
	with open(path, 'w') as out:
		out.write('\n'.join(lines))

	return path, lines


###
# prune
###

# What a pruned iteration keeps. `metrics.json` is the evidence the numbers
# were ever real and is tiny; `log.txt` is the only record of why a run
# failed. Everything else in a run directory is the artifact -- output
# files, saved arrays -- and that is what the disk went to.
KEEP_IN_RUN = ('metrics.json', 'log.txt', 'commit')

# Saved output arrays: results, alignments, comparison copies. Each can be
# regenerated exactly from what the search keeps (the iteration's code on its
# branch, the harness, the inputs), so they go even from protected
# iterations.
OUTPUT_EXTENSIONS = ('.npy', '.npz')


def regenerable_outputs(root, config, identifier, extensions):
	"""Output arrays in one iteration that can be rebuilt from its code."""

	# The baseline holds the inputs every iteration inherits, and a file
	# placed there by hand is untracked whatever it is.
	if identifier == 0:
		return []

	directory = iteration_dir(root, identifier)
	checkout = checkout_dir(root, identifier, config)
	tracked = set()

	# Plain mode: the iteration directory is the code checkout, and a tracked
	# .npy is part of the code (test data, a lookup table), never an output.
	if checkout == directory and os.path.exists(os.path.join(checkout, '.git')):
		try:
			tracked = set(git('-C', checkout, 'ls-files').split('\n'))
		except RuntimeError:
			return []

	found = []

	for base, dirs, names in os.walk(directory):
		relative_base = os.path.relpath(base, directory)

		# Repository mode keeps the code in src/; nothing in it is an output.
		if checkout != directory and (relative_base == 'src'
			or relative_base.startswith('src' + os.sep)):
			dirs[:] = []
			continue

		dirs[:] = [d for d in dirs if d != '.git']

		for name in names:
			if not name.lower().endswith(extensions):
				continue
			relative = os.path.normpath(os.path.join(relative_base, name))
			if relative in tracked:
				continue
			found.append(os.path.join(base, name))

	return found


# Functions `find(root, extensions) -> [path]` of a script that imports
# this one and keeps regenerable output arrays outside `iterations/`:
# `prune` removes those too.
OUTPUT_FINDERS = []


def protected(root, config, results, keep_recent):
	"""Ids whose artifacts are never pruned.

	The frontier path, every root, anything still running, and the most
	recent few. Those are the iterations somebody will want to inspect or
	rerun; everything else is a measurement that has already been taken.
	"""

	direction = config['objective']['direction']
	state = load_state(root)

	safe = {row['id'] for row in frontier_path(results, direction)}
	safe |= {entry['id'] for entry in state.get('roots', [])}
	safe |= {identifier for identifier in iteration_ids(root)
		if load_result(root, identifier) is None}
	safe |= set(sorted(iteration_ids(root))[-keep_recent:]) if keep_recent \
		else set()
	safe.add(0)

	return safe


def directory_size(path):
	total = 0

	for base, _, names in os.walk(path):
		for name in names:
			try:
				total += os.path.getsize(os.path.join(base, name))
			except OSError:
				continue

	return total


def human_seconds(seconds):
	if seconds is None:
		return '?'
	if seconds < 90:
		return '{:.0f} s'.format(seconds)
	if seconds < 5400:
		return '{:.0f} min'.format(seconds / 60)
	return '{:.1f} h'.format(seconds / 3600)


def human(n):
	for unit in ('B', 'kB', 'MB', 'GB', 'TB'):
		if n < 1024 or unit == 'TB':
			return '{:.1f} {}'.format(n, unit)
		n /= 1024.0


def drop_worktree(root, directory):
	"""Remove an iteration's code checkout, keeping its record.

	`git worktree remove` deletes the whole directory, and the record lives
	inside it -- `result.json`, `README.md`, every run's `metrics.json`. So
	the record is carried out, the checkout is removed, and the record is put
	back into a plain directory. The branch still holds the code, and
	`git -C repo.git worktree add iterations/<id> autoopt/<id>` brings the
	checkout back whenever it is wanted.
	"""

	# Repository mode: the records are beside the checkout, not in it.
	if load_config(root).get('repo'):
		git('-C', os.path.join(root, 'repo.git'), 'worktree', 'remove',
			'--force', os.path.join(directory, 'src'))
		return len(os.listdir(directory))

	saved = tempfile.mkdtemp(prefix='autoopt-keep-')
	kept = []

	for base, _, names in os.walk(directory):
		if '.git' in base.split(os.sep):
			continue

		for name in names:
			source = os.path.join(base, name)
			relative = os.path.relpath(source, directory)

			# Exactly the ignored files: the record, never the code.
			if relative == '.git' or not is_record(relative):
				continue

			target = os.path.join(saved, relative)
			os.makedirs(os.path.dirname(target), exist_ok=True)
			shutil.copy2(source, target)
			kept.append(relative)

	git('-C', os.path.join(root, 'repo.git'), 'worktree', 'remove',
		'--force', directory)

	os.makedirs(directory, exist_ok=True)

	for relative in kept:
		target = os.path.join(directory, relative)
		os.makedirs(os.path.dirname(target), exist_ok=True)
		shutil.move(os.path.join(saved, relative), target)

	shutil.rmtree(saved, ignore_errors=True)

	return len(kept)


def restore(args):
	"""Put a pruned iteration's code checkout back.

	`git worktree add` refuses a directory that already exists, and a pruned
	iteration's directory still holds its record -- so the record moves
	aside, the checkout lands, and the record moves back. The exact inverse
	of what `prune --worktrees` did.
	"""

	root = resolve(args.root)
	config = load_config(root)
	directory = iteration_dir(root, args.id)
	checkout = checkout_dir(root, args.id, config)

	if os.path.exists(os.path.join(checkout, '.git')):
		print('{} already has a checkout'.format(directory))
		return 0

	meta = load_meta(root, args.id)

	if meta is None and not args.branch:
		fail('iteration {} has no meta.json; pass --branch'.format(args.id))

	branch = args.branch or (meta or {}).get('branch') \
		or 'autoopt/{}'.format(args.id)

	# Before anything on disk moves: a branch that does not resolve would
	# otherwise fail the `worktree add` after the directory was emptied.
	if subprocess.run(['git', '-C', os.path.join(root, 'repo.git'),
		'rev-parse', '--verify', '-q', branch + '^{commit}'],
		capture_output=True).returncode != 0:
		fail('branch {} does not exist in {}; nothing was changed. Name the '
			'iteration\'s branch with --branch'.format(branch,
				os.path.join(root, 'repo.git')))

	if checkout != directory:
		git('-C', os.path.join(root, 'repo.git'), 'worktree', 'add', '-q',
			checkout, branch)
		print('{} restored from {}'.format(checkout, branch))
		return 0

	saved = tempfile.mkdtemp(prefix='autoopt-restore-')
	kept, others = [], []

	for base, _, names in os.walk(directory):
		for name in names:
			source = os.path.join(base, name)
			relative = os.path.relpath(source, directory)

			if not is_record(relative):
				others.append(relative)
				continue

			target = os.path.join(saved, relative)
			os.makedirs(os.path.dirname(target), exist_ok=True)
			shutil.move(source, target)
			kept.append(relative)

	# Anything else was put there by hand after the prune. It is not
	# deleted: the whole remainder moves beside the iteration, under a name
	# `iteration_ids` does not read as an iteration.
	aside = None

	if others:
		aside = '{}.restore-aside-{}'.format(directory,
			time.strftime('%Y%m%d-%H%M%S'))
		shutil.move(directory, aside)
	else:
		shutil.rmtree(directory)

	def put_back():
		for relative in kept:
			target = os.path.join(directory, relative)
			os.makedirs(os.path.dirname(target), exist_ok=True)
			shutil.move(os.path.join(saved, relative), target)

		shutil.rmtree(saved, ignore_errors=True)

	try:
		git('-C', os.path.join(root, 'repo.git'), 'worktree', 'add',
			directory, branch)
	except RuntimeError:
		# Never leave the record in a temp directory.
		os.makedirs(directory, exist_ok=True)
		put_back()
		if aside:
			print('files that are not records are in {}'.format(aside))
		raise

	put_back()

	print('{} restored from {}, {} record file(s) kept'.format(directory,
		branch, len(kept)))

	if aside:
		print('{} file(s) that are not records were moved aside, not '
			'deleted: {}'.format(len(others), aside))

	return 0


def is_record(relative):
	"""Is this one of the files that outlives the checkout?"""

	name = os.path.basename(relative)

	return (name in ('meta.json', 'result.json', 'report.txt', 'README.md',
		'notes.md', 'check.json', 'cold.json')
		or relative.startswith('runs' + os.sep)
		or relative.startswith('unit-tests' + os.sep)
		or relative.startswith('probe' + os.sep))


def prune_plan(root, config, skip, running, keep_outputs=False,
		extensions=OUTPUT_EXTENSIONS, worktrees=False, extra=True):
	"""What `prune` would delete: `{freed, targets, outputs, output_bytes,
	worktrees}`. `skip` are the iterations whose run artifacts, caches and
	checkouts stay; `running` are untouched entirely. Output arrays go from
	every settled iteration, and from wherever `OUTPUT_FINDERS` name unless
	`extra` is off."""

	freed, targets = 0, []

	for identifier in iteration_ids(root):
		if identifier in skip:
			continue

		directory = iteration_dir(root, identifier)
		runs = os.path.join(directory, 'runs')
		victims = []

		# `{cache}` holds only what a compiler can rebuild, and `check/` the
		# exported trees `git archive` rebuilds; only the checks' logs stay.
		walks = [(base, names, True) for base, _, names in os.walk(runs)] \
			+ [(base, names, False) for base, _, names in
				os.walk(os.path.join(directory, 'cache'))] \
			+ [(base, [n for n in names if not n.endswith('.log')], False)
				for base, _, names in os.walk(os.path.join(directory, 'check'))]

		for base, names, in_runs in walks:
			for name in names:
				if in_runs and name in KEEP_IN_RUN:
					continue

				path = os.path.join(base, name)

				try:
					freed += os.path.getsize(path)
				except OSError:
					continue

				victims.append(path)

		if victims:
			targets.append((identifier, victims))

	# Output arrays go from every settled iteration, protected or not: the
	# code that wrote them is kept on its branch.
	outputs, output_bytes = [], 0

	if not keep_outputs:
		already = {path for _, victims in targets for path in victims}

		for identifier in iteration_ids(root):
			if identifier in running:
				continue
			for path in regenerable_outputs(root, config, identifier, extensions):
				if path not in already:
					outputs.append(path)

		if extra:
			for find in OUTPUT_FINDERS:
				outputs += find(root, extensions)

		for path in outputs:
			try:
				output_bytes += os.path.getsize(path)
			except OSError:
				continue

	checkouts = []

	if worktrees:
		for identifier in iteration_ids(root):
			if identifier in skip:
				continue

			directory = iteration_dir(root, identifier)

			if os.path.exists(os.path.join(checkout_dir(root, identifier,
				config), '.git')):
				checkouts.append((identifier, directory))

	return {'freed': freed, 'targets': targets, 'outputs': outputs,
		'output_bytes': output_bytes, 'worktrees': checkouts}


def prune_apply(root, plan):
	"""Delete what `prune_plan` listed. Returns `(files removed, bytes freed,
	messages)`; a file or worktree that could not go is a message."""

	removed, messages = 0, []

	for identifier, victims in plan['targets']:
		for path in victims:
			try:
				os.unlink(path)
				removed += 1
			except OSError as error:
				messages.append('could not remove {}: {}'.format(path, error))

	for path in plan['outputs']:
		try:
			os.unlink(path)
			removed += 1
		except OSError as error:
			messages.append('could not remove {}: {}'.format(path, error))

	for identifier, directory in plan['worktrees']:
		try:
			drop_worktree(root, directory)
		except RuntimeError as error:
			messages.append('could not remove worktree {}: {}'.format(
				identifier, error))

	return removed, plan['freed'] + plan['output_bytes'], messages


def prune(args):
	"""Delete the artifacts of iterations nobody will rerun, and say how much.

	Reports by default and deletes only with `--apply`: a search directory
	is the record of days of compute and there is no undo.
	"""

	root = resolve(args.root)
	config = load_config(root)
	results = all_results(root)
	safe = protected(root, config, results, args.keep_recent)

	# `--all` lifts every protection but this one: an iteration with no
	# result.json is being written by a subagent right now.
	running = {identifier for identifier in iteration_ids(root)
		if load_result(root, identifier) is None}
	skip = running if args.all else safe | running
	extensions = tuple(e if e.startswith('.') else '.' + e for e in
		args.outputs_ext.replace(' ', '').split(',') if e)
	plan = prune_plan(root, config, skip, running, args.keep_outputs,
		extensions, args.worktrees)
	freed, targets = plan['freed'], plan['targets']
	outputs, output_bytes = plan['outputs'], plan['output_bytes']
	worktrees = plan['worktrees']

	if args.all:
		print('--all: only the {} running iteration(s) are protected.'.format(
			len(running)))
	else:
		print('{} iteration(s) protected: the frontier path, every root, '
			'anything running{}.'.format(len(safe),
				', and the {} most recent'.format(args.keep_recent)
				if args.keep_recent else ''))
	print('{} iteration(s) hold {} of prunable artifacts.'.format(
		len(targets), human(freed)))

	if outputs:
		print('{} saved output array(s) ({}) can be regenerated from the kept '
			'code and inputs.'.format(len(outputs), human(output_bytes)))

	if worktrees:
		print('{} worktree checkout(s) could also be removed; the branch, '
			'and so the code, stays either way.'.format(len(worktrees)))

	if not args.apply:
		print('\nNothing deleted. Re-run with --apply to delete it.')
		return 0

	removed, freed, messages = prune_apply(root, plan)

	for message in messages:
		print(message)

	print('\nDeleted {} file(s), {} freed.'.format(removed, human(freed)))

	if worktrees:
		print('Removed {} worktree(s). Restore one with: autoopt.py restore '
			'<work> --id <id>'.format(len(worktrees)))

	return 0


def live_publishers(root):
	"""Dashboard writers currently publishing this search.

	Read from the pidfiles under `~/autoopt-monitor/`, the same place
	`autoopt_monitor.py --status` reads. Imported lazily and defensively:
	the lifecycle commands must keep working on a machine where the
	dashboard has never been used.
	"""

	base = os.path.expanduser('~/autoopt-monitor')
	out = []

	# Three places a payload directory can be: beside the search (the
	# default), under the fallback base, or anywhere the writer index names.
	candidates = [os.path.join(root, 'monitor')]
	if os.path.isdir(base):
		candidates += [os.path.join(base, name) for name in sorted(os.listdir(base))]
	index = os.path.expanduser('~/.cache/autoopt-monitor')
	for name in (sorted(os.listdir(index)) if os.path.isdir(index) else []):
		try:
			with open(os.path.join(index, name)) as handle:
				candidates.append(handle.readline().strip())
		except OSError:
			continue

	seen = set()
	for out_dir in candidates:
		out_dir = os.path.abspath(out_dir)
		if out_dir in seen:
			continue
		seen.add(out_dir)
		path = os.path.join(out_dir, 'writer.pid')

		# Three plain lines -- pid, the search it watches, a code stamp --
		# the same shape `monitor_writer.writer_info` reads. Parsed here
		# rather than imported, so the lifecycle commands never depend on
		# the dashboard being importable.
		try:
			with open(path) as handle:
				pid = int(handle.readline().strip())
				watching = handle.readline().strip()
		except (OSError, ValueError):
			continue

		if os.path.abspath(watching) != os.path.abspath(root):
			continue

		try:
			os.kill(pid, 0)
		except OSError:
			continue

		# A pid outlives its process and gets reused, so a stale pidfile
		# can name anything. Where /proc exists, the process has to be a
		# writer; without it, the pid check above is all there is.
		try:
			with open('/proc/{}/cmdline'.format(pid), 'rb') as handle:
				if b'monitor_writer.py' not in handle.read():
					continue
		except FileNotFoundError:
			if os.path.isdir('/proc/self'):
				continue
		except OSError:
			pass

		out.append({'pid': pid, 'out_dir': out_dir})

	# A writer whose pidfile is gone keeps publishing: an older writer's exit
	# removed it, or a later writer overwrote it. Found by process too, as the
	# dashboard's own `publishers()` does, or status reports none and the
	# next phase starts a second one.
	known = {entry['pid'] for entry in out}
	for entry in writer_processes():
		if entry['pid'] not in known and search_dir(entry['run']) == search_dir(
				root):
			out.append({'pid': entry['pid'], 'out_dir': entry['out_dir']})

	return out


def search_dir(path):
	"""`path` as the `autooptimize/` directory: a writer may have been
	started on it or on the project directory holding it."""

	path = os.path.abspath(os.path.expanduser(path))
	nested = os.path.join(path, 'autooptimize')

	return nested if os.path.isfile(os.path.join(nested, 'config.json')) else path


def writer_processes():
	"""This user's running `monitor_writer.py` processes, from any copy of
	the script: `[{pid, run, out_dir}]`. From /proc only, so nothing is
	found where it does not exist; the pidfiles still are."""

	found = []

	try:
		pids = [p for p in os.listdir('/proc') if p.isdigit()]
	except OSError:
		return found

	for pid in pids:
		try:
			if os.stat('/proc/' + pid).st_uid != os.getuid():
				continue
			with open('/proc/{}/cmdline'.format(pid), 'rb') as handle:
				argv = handle.read().decode('utf-8', 'replace').split('\0')
			cwd = os.readlink('/proc/{}/cwd'.format(pid))
		except OSError:
			continue

		at = [i for i, a in enumerate(argv) if a.endswith('monitor_writer.py')]
		if not at:
			continue

		rest = [a for a in argv[at[0] + 1:] if a]
		out_dir = rest[rest.index('--out') + 1] if '--out' in rest[:-1] else None
		positional = [a for i, a in enumerate(rest) if not a.startswith('-')
			and (i == 0 or not rest[i - 1].startswith('--'))]

		if out_dir and positional:
			found.append({'pid': int(pid),
				'run': os.path.join(cwd, positional[0]),
				'out_dir': os.path.abspath(os.path.join(cwd,
					os.path.expanduser(out_dir)))})

	return found


def stale_idea_refs(root, keep, name='ideas.md'):
	"""Iteration ids a notes file (`ideas.md`, `knowledge.md`) mentions that
	a reset would remove: labels such as `A12` (the letter is checked so that
	`L1`, a cache level, is not read as one), and the older "iteration 12"
	spelling."""

	path = os.path.join(root, name)

	try:
		with open(path) as handle:
			text = handle.read()
	except OSError:
		return []

	found = set()

	for match in re.finditer(r'iterations?\s+(\d+(?:\s*(?:,|and)\s*\d+)*)',
		text):
		for number in re.findall(r'\d+', match.group(1)):
			found.add(int(number))

	for match in re.finditer(r'(?<![A-Za-z0-9])[{}](\d+)(?![0-9])'.format(
		re.escape(LABEL_LETTER)), text):
		found.add(int(match.group(1)))

	return sorted(found - set(keep))


def reset(args):
	"""Clear the search back to its baseline, keeping the baseline measured.

	For starting the ideas over -- a topic split that turned out wrong, a
	round of iterations built on a mistaken premise -- without paying for
	the baseline again. The kept iterations, their branches, their
	`result.json` and their runs survive; everything else goes, and the
	round counter and root return to where `init` left them.

	`config.json`, `spec.md`, `ideas.md` and `reference/` are never touched:
	the objective, the gates and the fixed reference the gates compare
	against have to outlive a reset, or the numbers before and after it are
	from different experiments.
	"""

	root = resolve(args.root)
	config = load_config(root)
	state = load_state(root)

	keep = sorted({to_id(root, item, '--keep')
		for item in args.keep.split(',') if item.strip() != ''})

	if not keep:
		fail('--keep must name at least one iteration; a reset that keeps '
			'nothing is a new search, so run init instead')

	present = iteration_ids(root)

	for identifier in keep:
		if identifier not in present:
			fail('iteration {} does not exist, so it cannot be kept'.format(
				identifier))

		if load_result(root, identifier) is None:
			fail('iteration {} has no result.json, so keeping it would '
				'leave the search with no measured baseline. Run it first, '
				'or keep a different one.'.format(identifier))

	new_root = keep[0] if args.new_root is None else args.new_root

	if new_root not in keep:
		fail('--new-root {} is not in --keep {}; the root has to be an '
			'iteration that survives the reset'.format(new_root, args.keep))

	doomed = [i for i in present if i not in keep]
	in_flight = [i for i in doomed if load_result(root, i) is None
		and load_meta(root, i) is not None]

	rounds = os.path.join(root, 'rounds')
	summaries = sorted(os.listdir(rounds)) if os.path.isdir(rounds) else []

	# A brief belongs to its iteration, and one left behind would reserve its
	# id (pending_briefs) after the reset frees it.
	folder = os.path.join(root, 'briefs')
	stale_briefs = sorted(name for name in (os.listdir(folder)
		if os.path.isdir(folder) else []) if name.endswith('.md')
		and name[:-3].isdigit() and int(name[:-3]) not in keep)

	# Anything still publishing this search will serve the deleted
	# iterations until it next scans. The message names it; stopping it
	# is the user's call because it may be somebody else's window.
	publishers = live_publishers(root)

	# References in ideas.md to iterations that will not exist. The file is
	# deliberately kept -- it is the idea list, not a result -- but a strike
	# reading "(iteration 12: -0.04)" against a missing 12 is a trap.
	stale_notes = stale_idea_refs(root, keep)
	stale_ledger = stale_idea_refs(root, keep, 'knowledge.md')

	# Validation records are filed by round, and every round restarts at 1.
	validations = os.path.join(root, 'validations')
	old_validations = sorted(os.listdir(validations)) if os.path.isdir(
		validations) else []

	print('keeping iteration(s) {}, with their branches, results and '
		'runs.'.format(', '.join(str(i) for i in keep)))
	print('removing {} iteration(s): {}'.format(len(doomed),
		', '.join(str(i) for i in doomed) if doomed else 'none'))
	print('removing {} round summary/summaries.'.format(len(summaries)))
	if stale_briefs:
		print('removing {} brief(s) of iterations that will not exist.'.format(
			len(stale_briefs)))
	print('the root becomes iteration {}, and the search returns to round '
		'1.'.format(new_root))
	print('git objects left unreachable will be pruned.')
	print('untouched: config.json, spec.md, ideas.md, knowledge.md, tools/, '
		'reference/.')

	if old_validations:
		print('removing {} external validation record(s); rounds restart at '
			'1.'.format(len(old_validations)))

	for name, stale in (('ideas.md', stale_notes), ('knowledge.md',
		stale_ledger)):
		if stale:
			print('\n{} mentions {} iteration(s) that will not exist: {}. The '
				'file is kept; those references will dangle, and once ids are '
				'reused they will name new, unrelated iterations.'.format(name,
					len(stale), ', '.join(str(i) for i in stale)))

	if publishers:
		print('\n{} dashboard publisher(s) are watching this search and will '
			'serve the removed iterations until they rescan:'.format(
				len(publishers)))

		for entry in publishers:
			print('  pid {}  ->  {}'.format(entry['pid'], entry['out_dir']))

		print('  Stop each with: python3 <skill>/scripts/dashboard/'
			'autoopt_monitor.py {} --stop'.format(args.root))

	if in_flight:
		print('\n{} of them {} no result.json, so {} either running now or '
			'died mid-iteration: {}'.format(len(in_flight),
				'has' if len(in_flight) == 1 else 'have',
				'it is' if len(in_flight) == 1 else 'they are',
				', '.join(str(i) for i in in_flight)))

		if not args.force:
			print('Removing a directory a subagent is writing into breaks '
				'that subagent. Wait for them, or re-run with --force.')
			return 1

	if not args.apply:
		print('\nNothing removed. Re-run with --apply to do it.')
		return 0

	bare = os.path.join(root, 'repo.git')

	for identifier in doomed:
		directory = iteration_dir(root, identifier)
		checkout = checkout_dir(root, identifier, config)

		# Read before the directory that holds it goes.
		meta = load_meta(root, identifier) or {}
		branch = meta.get('branch', 'autoopt/{}'.format(identifier))

		if os.path.exists(os.path.join(checkout, '.git')):
			try:
				git('-C', bare, 'worktree', 'remove', '--force', checkout)
			except RuntimeError as error:
				print('could not remove worktree {}: {}'.format(identifier,
					error))

		shutil.rmtree(directory, ignore_errors=True)

		try:
			git('-C', bare, 'branch', '-D', branch)
		except RuntimeError:
			# Already gone, or never created because the fork failed.
			pass

	# `worktree remove` leaves the registration behind when the directory was
	# deleted by hand, and the next `new` then
	# refuses the id it wants to reuse.
	try:
		git('-C', bare, 'worktree', 'prune')
	except RuntimeError as error:
		print('could not prune stale worktree registrations: {}'.format(
			error))

	for name in summaries:
		try:
			os.unlink(os.path.join(rounds, name))
		except OSError as error:
			print('could not remove {}: {}'.format(name, error))

	for name in stale_briefs:
		for path in (name, name[:-3] + '.json'):
			if not os.path.exists(os.path.join(folder, path)):
				continue
			try:
				os.unlink(os.path.join(folder, path))
			except OSError as error:
				print('could not remove brief {}: {}'.format(path, error))

	for name in old_validations:
		target = os.path.join(validations, name)
		try:
			if os.path.isdir(target):
				shutil.rmtree(target)
			else:
				os.unlink(target)
		except OSError as error:
			print('could not remove {}: {}'.format(target, error))

	# Every kept iteration goes back to round 1, so its own record agrees
	# with the state that now describes it. Left alone, an iteration
	# promoted in round 17 would still claim round 17 in a search whose
	# the search is on round 1, and the round tables would disagree with
	# the summaries that are about to be written.
	for identifier in keep:
		for loader, filename in ((load_meta, 'meta.json'),
			(load_result, 'result.json')):
			record = loader(root, identifier)

			if record is None:
				continue

			# A0 carries round 0: it is what every round is measured
			# against, not a member of the first one.
			record['round'] = 0 if identifier == 0 else 1
			path = os.path.join(iteration_dir(root, identifier), filename)

			try:
				write_json(path, record)
			except OSError as error:
				print('could not rewrite {} for iteration {}: {}'.format(
					filename, identifier, error))

	with Lock(root):
		state = load_state(root)
		state.update(root=new_root, round=1, roots=[{'round': 1,
			'id': new_root}])
		state.pop('band', None)
		save_state(root, state)

	# The removed branches' commits are unreachable but still on disk,
	# and a long search leaves a lot of them. Without this the directory
	# does not actually shrink, which is half the reason resets get run.
	before = directory_size(bare)

	try:
		git('-C', bare, 'reflog', 'expire', '--expire=now', '--all')
		git('-C', bare, 'gc', '--prune=now', '--quiet')
	except RuntimeError as error:
		print('could not prune unreachable git objects: {}'.format(error))

	freed = max(0, before - directory_size(bare))

	_, taken = taken_ids(root)
	print('\nRemoved {} iteration(s). The root is {}, the search is on round '
		'1, and the next `new` takes id {}.'.format(len(doomed),
			new_root, max(taken) + 1))

	if freed:
		print('Reclaimed {} of unreachable git objects.'.format(human(freed)))

	print('The objective, the gates and {} are unchanged, so iterations '
		'from before and after this reset are still comparable.'.format(
			config['objective']['name']))

	if publishers:
		print('\nThe dashboard publisher(s) listed above are still running '
			'and still hold the removed iterations. Stop them, or the next '
			'person to open the page reads a search that no longer exists.')

	return 0


###
# path: the winning iteration's lineage
###

def best_id(root, config):
	"""`best`, the winner: the best median among the root and the iterations
	descending from it that passed the keep rule (one not yet promoted).
	Their code holds every change kept, while a better median outside the
	root's line was kept out. Once the search has ended, the winner
	`ended.json` records."""

	ended = load_ended(root)

	if ended is not None:
		return ended['winner']['id']

	direction = config['objective']['direction']

	state = load_state(root)
	heads = {state.get('root', 0) or 0}
	chosen = []
	for result in all_results(root):
		if result.get('status') != 'ok' or result.get('control') or \
				result.get('split'):
			continue
		line = set(known_lineage(root, result['id']))
		if result['id'] in heads or (heads & line and (result.get('keep')
				or {}).get('verdict') == 'pass'):
			chosen.append(result)
	if chosen:
		return frontier(chosen, direction)[1]

	value, found = frontier(all_results(root), direction)

	if found is None:
		fail('no iteration has status "ok" yet, so there is no best')

	return found


def resolve_id(root, config, identifier):
	"""An iteration id, or `best` (`best_id`)."""

	if isinstance(identifier, int):
		return identifier

	if identifier is not None and str(identifier) != 'best':
		return to_id(root, identifier, '--id')

	return best_id(root, config)


def lineage(root, identifier):
	"""The path to an iteration: its ancestors from the baseline, then it."""

	chain, seen, current = [], set(), identifier

	while current is not None:
		if current in seen:
			fail('iteration {} is its own ancestor; meta.json is '
				'corrupt'.format(current))

		meta = load_meta(root, current)

		if meta is None:
			fail('iteration {} on the path to {} has no meta.json'.format(
				current, identifier))

		seen.add(current)
		chain.append(current)
		current = meta.get('parent')

	return chain[::-1]


def path_rows(root, config, chain):
	"""Header and rows for the path table, every number from result.json."""

	objective = config['objective']
	unit = ' ({})'.format(objective['unit']) if objective.get('unit') else ''

	header = ['iteration', 'category', 'change', 'status', 'runs',
		objective['name'] + unit, 'Δ vs parent']
	header += [gate['name'] for gate in config['gates']]
	header += [observer['name'] for observer in config['observers']]
	header += ['unit tests'] if config.get('tests') else []

	rows, previous = [], None

	for identifier in chain:
		result = load_result(root, identifier) or {}
		meta = load_meta(root, identifier) or {}
		value = (result.get('objective') or {}).get('median')

		delta = (None if value is None or previous is None
			else value - previous)

		row = [label(root, identifier), category_of(dict(meta, id=identifier))
			or '-',
			(meta.get('title') or '').replace('|', '/'),
			result.get('status', 'running'),
			'{} ({})'.format(result.get('n_runs', 0), result.get('stage', '-')),
			fmt(value), '' if identifier == chain[0] else fmt(delta, 3)]

		for gate in config['gates']:
			record = (result.get('gates') or {}).get(gate['name']) or {}
			row.append(fmt(record.get('median'), 4))

		for observer in config['observers']:
			record = (result.get('observers') or {}).get(observer['name']) or {}
			row.append(fmt(record.get('median'), 4))

		if config.get('tests'):
			row.append(tests_cell(result))

		rows.append(row)
		previous = value

	return header, rows


def path_markdown(root, config, chain):
	header, rows = path_rows(root, config, chain)

	lines = ['| ' + ' | '.join(header) + ' |',
		'|' + '---|' * len(header)]
	lines += ['| ' + ' | '.join(row) + ' |' for row in rows]

	names = ', '.join('{} {} {}{}'.format(gate['name'], gate['op'],
		fmt(gate['threshold']), ' ± {}'.format(fmt(gate['tol']))
			if gate.get('tol') else '') for gate in config['gates'])
	lines += ['', 'Every value is the median over that iteration\'s runs '
		'(one seed per run); Δ is against the row above.' + (' Gates: {}.'
			.format(names) if names else '')]

	return '\n'.join(lines)


def in_root(root, head):
	"""Every iteration whose change is in `head`: its path from the
	baseline."""

	return set(lineage(root, head))


def keep_verdict(config, result, parent_result):
	"""The default keep rule on a settled iteration against its parent, as
	`(passes, why)`: a median, over the full count of runs, that beats the
	parent's by more than the noise band. Paired timing runs once, at the
	end (`final`)."""

	if result.get('status') != 'ok':
		return False, 'it is {}'.format(result.get('status'))
	if result.get('control'):
		return False, 'a same-code control is never kept'
	if result.get('stage') != 'confirmed':
		return False, 'not confirmed: the screening median did not earn it'

	direction = config['objective']['direction']
	median = (result.get('objective') or {}).get('median')
	base = ((parent_result or {}).get('objective') or {}).get('median')
	band = result.get('noise_band')

	if median is None or base is None:
		return False, 'no median for it or its parent'

	gain = base - median if direction == 'min' else median - base

	if band is not None and gain > band:
		return True, 'its median beats its parent\'s by {} > the noise band {}'.format(
			fmt(gain), fmt(band))

	if gain < 0:
		return False, 'its median is worse than its parent\'s by {}'.format(
			fmt(-gain))

	return False, 'its median is within the noise band {} of its parent\'s ({:+.3g})'.format(
		fmt(band), gain)


def algorithm_search(root):
	"""Whether spec.md records an algorithm search."""

	try:
		with open(spec_path(root)) as infile:
			kind = spec_field(infile.read(), 'Kind of search') or ''
	except OSError:
		return False

	return 'algorithm' in kind.lower() and 'TODO' not in kind


LABEL_WORD = re.compile(r'(?<![A-Za-z0-9_])([A-Z]+\d+)(?![A-Za-z0-9_])')


def labels_named(root, text):
	"""The iteration labels (`A4`, `B22`) that `text` writes, in order. A
	label hyphenated onto a word that is not one is part of a name (cell
	`AZ-A2`, sample `HG002-A7`), not a label; `A12-A16` and `A4-based` are
	labels."""

	known = {label(root, identifier) for identifier in iteration_ids(root)}
	found = []

	for match in LABEL_WORD.finditer(text):
		word = match.group(1)
		joined = re.search(r'([A-Za-z0-9_]+)-$', text[:match.start()])
		if joined and joined.group(1) not in known:
			continue
		if word in known and word not in found:
			found.append(word)

	return found


MESSAGE_SUBJECT_LIMIT = 72


def message_problems(root, text):
	"""What is wrong with a commit message, as a list."""

	lines = text.rstrip('\n').split('\n')
	subject = lines[0].strip()
	problems = []

	if not subject:
		return ['the first line, the subject, is empty']

	if len(subject) > MESSAGE_SUBJECT_LIMIT:
		problems.append('the subject is {} characters; the limit is {}'.format(
			len(subject), MESSAGE_SUBJECT_LIMIT))

	if subject.endswith('.'):
		problems.append('the subject must not end with a period')

	if len(lines) > 1 and lines[1].strip():
		problems.append('the second line must be blank, between the subject '
			'and the body')

	if FILL in text:
		problems.append('{} placeholder(s) left'.format(text.count(FILL)))

	named = labels_named(root, text)
	if named:
		problems.append('it names iteration label(s) {}'.format(
			', '.join(named)))

	return problems + trailer_problems(text, commit_trailer(root))


def winning_steps(root, chain, tree_of, base_tree):
	"""The winning iterations of a path that change the repository, in order,
	as `(id, tree)`: each iteration after the baseline whose tree differs
	from the one before it (`base_tree` before the first). `tree_of(id)` is
	an iteration's tree."""

	steps, previous = [], base_tree

	for identifier in chain[1:]:
		tree = tree_of(identifier)

		if tree != previous:
			steps.append((identifier, tree))
			previous = tree

	return steps


def build_outputs(config):
	"""`after.build_outputs`: paths the build writes into the repository,
	which are no part of any change."""

	return list((config.get('after') or {}).get('build_outputs') or [])


def glob_specs(patterns, magic='glob'):
	"""`patterns` as git pathspecs with `magic` (`glob`, `exclude,glob`)."""

	return [':({}){}'.format(magic, pattern) for pattern in patterns]


STRIPPED = {}


def strip_tree(repository, tree, patterns, base_tree):
	"""`tree` with every tracked path `patterns` match as `base_tree` has it:
	removed, or `base_tree`'s entry where it has one, so no diff between two
	stripped trees touches such a path. A pattern is a path relative to the
	repository's root or a glob, matched by git as a `:(glob)` pathspec (`*`
	stays inside one directory, `**/` spans directories, a directory matches
	everything under it); one that matches nothing is ignored. Made in a
	temporary index; no worktree is read or written."""

	if not patterns:
		return tree

	key = (os.path.realpath(repository), tree, tuple(patterns), base_tree)

	if key not in STRIPPED:
		specs = ['--'] + glob_specs(patterns)

		with tempfile.TemporaryDirectory() as scratch:
			env = dict(os.environ, GIT_INDEX_FILE=os.path.join(scratch, 'index'))
			git('-C', repository, 'read-tree', base_tree, env=env)
			kept = git('-C', repository, 'ls-files', '-s', '-z', *specs, env=env)
			git('-C', repository, 'read-tree', tree, env=env)
			git('-C', repository, 'rm', '-r', '-q', '-f', '--cached',
				'--ignore-unmatch', *specs, env=env)

			if kept:
				git('-C', repository, 'update-index', '-z', '--index-info',
					input=kept, env=env)

			STRIPPED[key] = git('-C', repository, 'write-tree', env=env).strip()

	return STRIPPED[key]


def outputs_stripper(root, config, repository=None):
	"""`strip_tree` with `after.build_outputs`, against the tree of the commit
	the search copied, in `repository` (default the search's `repo.git`):
	a function of a tree id. The identity when the list is empty."""

	patterns = build_outputs(config)

	if not patterns:
		return lambda tree: tree

	repository = repository or os.path.join(root, 'repo.git')
	base = git('-C', repository, 'rev-parse', '{}^{{tree}}'.format((config.get(
		'repo') or {}).get('commit') or 'autoopt/0')).strip()

	return lambda tree: strip_tree(repository, tree, patterns, base)


def tests_branch(args):
	"""Step 1's git sequence: check the original, clone it into
	`<work>/tests/suite` with the original's remote, and branch; or, with
	`--squash`, collapse the branch above the original's commit into one."""

	work = os.path.abspath(os.path.expanduser(args.work))
	suite = work_path(work, 'tests-suite')

	if args.squash:
		if not (args.message or '').strip():
			fail('--squash needs --message: the suite\'s commit message')

		branch = git('-C', suite, 'rev-parse', '--abbrev-ref', 'HEAD').strip()

		if not re.match(r'auto-optimize/\d{4}-\d\d-\d\d-unit-tests$', branch):
			fail('{} is on {}, not auto-optimize/<YYYY-MM-DD>-unit-tests'
				.format(suite, branch))

		base = git('-C', suite, 'merge-base', 'HEAD', 'origin/HEAD').strip()
		git('-C', suite, 'reset', '--soft', base)
		git('-C', suite, 'commit', '-q', '-m', with_trailer(args.message,
			commit_trailer(os.path.join(work, 'autooptimize'))))
		count = git('-C', suite, 'rev-list', '--count',
			'origin/HEAD..HEAD').strip()

		if count != '1':
			fail('the branch is {} commits above origin/HEAD, not 1'.format(
				count))

		print('squashed {} into one commit'.format(suite))
		return 0

	if not args.original:
		fail('give the original repository, or --squash')

	original = os.path.abspath(os.path.expanduser(args.original))

	if git('-C', original, 'status', '--porcelain').strip():
		fail('{} has uncommitted changes: the clone takes only committed '
			'work; settle them with the user first'.format(original))

	branch = git('-C', original, 'rev-parse', '--abbrev-ref', 'HEAD').strip()

	if branch == 'HEAD':
		fail('{} is on a detached HEAD: settle which branch with the user'
			.format(original))

	remote = git('-C', original, 'remote', 'get-url', 'origin').strip()
	pushed = git('-C', original, 'ls-remote', 'origin', 'refs/heads/' +
		branch).split()[:1]
	local = git('-C', original, 'rev-parse', branch).strip()

	if pushed != [local]:
		fail('{} is not pushed: origin has {}, the branch is at {}; the '
			'user pushes (or pulls) first'.format(branch, pushed[0][:10] if
			pushed else 'nothing', local[:10]))

	if os.path.exists(suite):
		fail('{} exists'.format(suite))

	name = 'auto-optimize/{}-unit-tests'.format(args.date or time.strftime(
		'%Y-%m-%d'))
	git('clone', '-q', original, suite)
	git('-C', suite, 'remote', 'set-url', 'origin', remote)
	git('-C', suite, 'checkout', '-q', '-b', name)
	print('cloned {} to {} on {}; origin is {}'.format(original, suite, name,
		remote))
	return 0


def archive(args):
	"""Step 6, section 1: the winner's patch, code, README and result in one
	folder, and the final report's skeleton with the sections the files
	settle already written."""

	import contextlib
	import io

	root = resolve(args.root)
	config = load_config(root)
	identifier = resolve_id(root, config, args.id)
	work = os.path.dirname(root)
	dest = os.path.abspath(os.path.expanduser(args.dest or work_path(work,
		'finish', 'archive')))
	folder = iteration_dir(root, identifier)
	repo = os.path.join(root, 'repo.git')

	if os.path.exists(os.path.join(dest, 'idea.patch')) and not args.force:
		fail('{} already holds an archive; --force replaces it'.format(dest))

	os.makedirs(os.path.join(dest, 'code'), exist_ok=True)

	with open(os.path.join(dest, 'idea.patch'), 'w') as out:
		out.write(git('-C', repo, 'diff', 'autoopt/0..autoopt/{}'.format(
			identifier)))

	tarred = subprocess.run('git -C {} archive autoopt/{} | tar -x -C {}'
		.format(shlex.quote(repo), identifier, shlex.quote(os.path.join(dest,
		'code'))), shell=True, capture_output=True, text=True)

	if tarred.returncode:
		fail('could not archive the code: {}'.format(tarred.stderr.strip()))

	for name in ('README.md', 'result.json'):
		if os.path.exists(os.path.join(folder, name)):
			shutil.copy(os.path.join(folder, name), dest)

	objective = config['objective']
	gates = ['{} {} {}'.format(g['name'], g['op'], g.get('threshold'))
		for g in config.get('gates', [])] or ['none']
	zero = (load_result(root, 0) or {}).get('objective', {}).get('median')
	best = (load_result(root, identifier) or {}).get('objective', {}).get(
		'median')
	report = os.path.join(work, 'finish', 'final-report.md')

	if os.path.exists(report) and not args.force:
		print('kept {} (it exists)'.format(report))
	else:
		os.makedirs(os.path.dirname(report), exist_ok=True)
		buffer = io.StringIO()

		with contextlib.redirect_stdout(buffer):
			path(argparse.Namespace(root=root, id=str(identifier), json=False,
				markdown=True, messages_template=None))

		text = '\n'.join([
			'# {} \u2014 final report'.format(config['task']), '',
			'## What was optimized', '',
			'Objective: {} ({}, {}). Gates: {}. Search: `{}`.'.format(
				objective['name'], 'lower is better' if objective['direction']
				== 'min' else 'higher is better', objective.get('unit') or
				'no unit', '; '.join(gates), work), '',
			'## Result', '',
			'{} median of the baseline {} and of {} {}: see the path below.'
			.format(objective['name'], zero, label(root, identifier), best),
			'', '## The path', '', buffer.getvalue().rstrip(), '',
			'## What produced the gains', '', '<<FILL: the gain by category>>',
			'', '## What was tried and failed', '', '<<FILL>>', '',
			'## Compute', '', '<<FILL>>', '', '## Where things are', '',
			'Code and patch: `{}`.'.format(dest), ''])
		with open(report, 'w') as out:
			out.write(text)

		print('wrote {}'.format(report))

	print('archived {} to {}'.format(label(root, identifier), dest))
	return 0


def path(args):
	root = resolve(args.root)
	config = load_config(root)
	identifier = resolve_id(root, config, args.id)
	chain = lineage(root, identifier)

	if args.json:
		print(json.dumps({'id': identifier, 'path': chain,
			'results': [load_result(root, i) for i in chain]}, indent='\t'))
		return 0

	if args.markdown:
		print(path_markdown(root, config, chain))
		return 0

	header, rows = path_rows(root, config, chain)
	widths = [max(len(header[i]), *(len(row[i]) for row in rows))
		for i in range(len(header))]

	print('path to iteration {}: {}'.format(label(root, identifier),
		' -> '.join(label(root, i) for i in chain)))
	print()
	print('  '.join(header[i].ljust(widths[i])
		for i in range(len(header))).rstrip())
	print('  '.join('-' * width for width in widths))

	for row in rows:
		print('  '.join(row[i].ljust(widths[i])
			for i in range(len(header))).rstrip())

	return 0


###
# table and show: read a search without changing it
###

# The columns `table` can print, in the order `--fields all` gives them. Each
# gate and observer of the search is a column too, under its own name, or as
# gate.<name> / observer.<name> where that name is one of these.
TABLE_FIELDS = ('id', 'parent', 'round', 'category', 'title', 'status',
	'stage', 'n', 'median', 'spread', 'vs_parent', 'vs_base', 'vs_base_pct',
	'keep', 'promoted', 'frontier', 'gates', 'tests', 'check', 'commit')
TABLE_DEFAULT = ('id', 'parent', 'category', 'title', 'status', 'n', 'median',
	'vs_parent', 'vs_base', 'keep', 'frontier')


def metric_columns(config):
	"""[(column, 'gate' or 'observer', name)] for the search's gates and
	observers."""

	out = []

	for kind, entries in (('gate', config['gates']), ('observer',
			config['observers'])):
		for entry in entries:
			name = entry['name']
			out.append((name if name not in TABLE_FIELDS else '{}.{}'.format(
				kind, name), kind, name))

	return out


def iteration_rows(root, config):
	"""Every iteration as a dict of raw values (numbers stay numbers, a
	missing value is None), settled ones from result.json and in-flight ones
	from meta.json. Reads files only."""

	direction = config['objective']['direction']
	results = all_results(root)
	by_id = {result['id']: result for result in results}
	on_frontier = {r['id'] for r in frontier_path(results, direction)}
	roots = {entry.get('id') for entry in load_state(root).get('roots') or []}
	base = ((by_id.get(0) or {}).get('objective') or {}).get('median')
	checked = bool((config.get('checks') or {}).get('compare'))
	rows = []

	for identifier in iteration_ids(root):
		result = by_id.get(identifier)
		meta = load_meta(root, identifier) or {}
		record = result or {}
		parent = record.get('parent', meta.get('parent'))
		median = (record.get('objective') or {}).get('median')
		above = ((by_id.get(parent) or {}).get('objective') or {}).get(
			'median')
		gates = record.get('gates') or {}

		if result is not None:
			status = record.get('status')
		elif os.path.exists(os.path.join(iteration_dir(root, identifier),
				'result.json')):
			status = 'unreadable'
		else:
			status = 'running'

		broken = [gate['name'] for gate in config['gates'] if not (gates.get(
			gate['name']) or {}).get('pass')]

		row = {
			'id': label(root, identifier),
			'parent': label(root, parent) if parent is not None else None,
			'round': record.get('round', meta.get('round')),
			'category': category_of({'id': identifier, 'category': record.get(
				'category') or meta.get('category')}),
			'title': record.get('title') or meta.get('title'),
			'status': status,
			'stage': record.get('stage'),
			'n': record.get('n_runs'),
			'median': median,
			'spread': (record.get('objective') or {}).get('std'),
			'vs_parent': median - above if None not in (median, above)
				else None,
			'vs_base': median - base if None not in (median, base) else None,
			'vs_base_pct': 100.0 * (median - base) / abs(base) if None not in (
				median, base) and base != 0 else None,
			'keep': (record.get('keep') or {}).get('verdict'),
			'promoted': identifier in roots,
			'frontier': identifier in on_frontier,
			'gates': None if result is None or not config['gates'] else
				'FAIL ' + ','.join(broken) if broken else 'pass',
			'tests': tests_cell(record) if result and config.get('tests')
				else None,
			'check': check_cell(record) if result and checked else None,
			'commit': (record.get('commit') or meta.get('commit') or '')[:12]
				or None,
		}

		for column, kind, name in metric_columns(config):
			found = (record.get(kind + 's') or {}).get(name) or {}
			row[column] = found.get('worst' if kind == 'gate' else 'median')

		row['_id'] = identifier
		rows.append(row)

	return rows


def table_cell(value):
	"""A raw value as text for the aligned table."""

	if value is True:
		return 'yes'
	if value is False:
		return ''

	return fmt(value)


def table_command(args):
	"""`table`: one row per iteration, filtered, sorted and cut, as text,
	TSV or JSON."""

	root = resolve(args.root)
	config = load_config(root)
	rows = iteration_rows(root, config)
	every = list(TABLE_FIELDS) + [column for column, _, _ in metric_columns(
		config)]

	fields = every if args.fields == 'all' else [field.strip() for field in (
		args.fields or ','.join(TABLE_DEFAULT)).split(',') if field.strip()]
	unknown = [field for field in fields if field not in every]

	if unknown:
		fail('--fields: no column {}; the columns are {}'.format(', '.join(
			unknown), ', '.join(every)))

	if args.status:
		wanted = {word.strip() for word in args.status.split(',')}
		rows = [row for row in rows if row['status'] in wanted]

	if args.category:
		wanted = {word.strip().upper() for word in args.category.split(',')}
		rows = [row for row in rows if (row['category'] or '').upper()
			in wanted or SHORT.get(row['category'], '').upper() in wanted]

	if args.round is not None:
		rows = [row for row in rows if row['round'] == args.round]

	if args.parent is not None:
		parent = label(root, to_id(root, args.parent, '--parent'))
		rows = [row for row in rows if row['parent'] == parent]

	if args.frontier:
		rows = [row for row in rows if row['frontier']]

	if args.promoted:
		rows = [row for row in rows if row['promoted']]

	if args.sort:
		key = args.sort.lstrip('-')
		descending = args.sort.startswith('-')

		if key == 'best':
			key = 'median'
			descending = config['objective']['direction'] == 'max'
		elif key not in every:
			fail('--sort: no column {}; the columns are best, {}'.format(key,
				', '.join(every)))

		present = [row for row in rows if row.get(key) is not None]
		present.sort(key=lambda row: (row[key], row['_id']),
			reverse=descending)
		rows = present + [row for row in rows if row.get(key) is None]

	if args.limit is not None:
		rows = rows[:args.limit]

	if args.json:
		print(json.dumps([{field: row.get(field) for field in fields}
			for row in rows], indent='\t'))
		return 0

	if args.tsv:
		print('\t'.join(fields))
		for row in rows:
			print('\t'.join('' if row.get(field) is None else str(row[field])
				for field in fields))
		return 0

	cells = [[table_cell(row.get(field)) for field in fields] for row in rows]
	cells = [[cell[:40] if field == 'title' else cell for field, cell in zip(
		fields, line)] for line in cells]
	widths = [max([len(field)] + [len(line[i]) for line in cells])
		for i, field in enumerate(fields)]

	print('  '.join(field.ljust(widths[i]) for i, field in enumerate(
		fields)).rstrip())
	print('  '.join('-' * width for width in widths))

	for line in cells:
		print('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(
			line)).rstrip())

	print('({} of {} iterations; median: {}, {}imized, over each iteration\'s '
		'runs; vs_parent and vs_base: its median minus the parent\'s and A0\'s)'
		.format(len(rows), len(iteration_ids(root)), config['objective'][
		'name'], config['objective']['direction']))

	return 0


def notes_excerpts(root, identifier, chars):
	"""{section: text} for The idea and What happened, from notes.md, else
	README.md, each cut to `chars` (0: whole). A file with neither section
	gives its first `## ` section other than Numbers."""

	directory = iteration_dir(root, identifier)

	for name in ('notes.md', 'README.md'):
		try:
			with open(os.path.join(directory, name), encoding='utf-8') as handle:
				text = handle.read()
		except OSError:
			continue

		# Lenient: a README opens with a title and a metadata block.
		sections, current = [], None
		for line in text.splitlines():
			if line.startswith('## '):
				current = [line[3:].strip(), []]
				sections.append(current)
			elif current is not None:
				current[1].append(line)

		sections = [(heading, '\n'.join(body).strip()) for heading, body
			in sections if '\n'.join(body).strip()]
		chosen = [(heading, body) for heading, body in sections if heading in (
			'The idea', 'What happened')] or [(heading, body) for heading, body
			in sections if heading != 'Numbers'][:1]

		if chosen:
			return {heading: body if not chars or len(body) <= chars else
				body[:chars].rstrip() + ' …' for heading, body in chosen}

	return {}


def show(args):
	"""`show`: one iteration on one screen, with the paths to read further.
	Reads files and runs `git diff --shortstat` only."""

	root = resolve(args.root)
	config = load_config(root)
	identifier = resolve_id(root, config, args.id)

	if identifier is None or not os.path.isdir(iteration_dir(root,
			identifier)):
		fail('no iteration {} in {}'.format(label(root, identifier), root))

	row = next(row for row in iteration_rows(root, config) if row['_id']
		== identifier)
	result = load_result(root, identifier) or {}
	meta = load_meta(root, identifier) or {}
	objective = config['objective']
	parent = result.get('parent', meta.get('parent'))
	directory = iteration_dir(root, identifier)
	bare = os.path.join(root, 'repo.git')
	diff = None

	if parent is not None:
		span = 'autoopt/{}...autoopt/{}'.format(parent, identifier)
		try:
			diff = {'command': 'git -C {} diff {}'.format(shlex.quote(bare),
				span), 'shortstat': git('-C', bare, 'diff', '--shortstat',
				span).strip() or 'no change'}
		except RuntimeError:
			diff = None

	paths = {name: os.path.join(directory, name) for name in ('README.md',
		'notes.md', 'result.json', 'meta.json', 'runs')
		if os.path.exists(os.path.join(directory, name))}
	brief = os.path.join(root, 'briefs', '{}.md'.format(identifier))
	if os.path.exists(brief):
		paths['brief'] = brief

	notes = notes_excerpts(root, identifier, args.chars)

	if args.json:
		print(json.dumps({'row': {key: value for key, value in row.items()
			if not key.startswith('_')}, 'objective': result.get('objective'),
			'gates': result.get('gates'), 'observers': result.get('observers'),
			'keep': result.get('keep'), 'failures': result.get('failures'),
			'tests': result.get('tests'), 'check': result.get('check'),
			'prediction': result.get('prediction'), 'diff': diff,
			'notes': notes, 'paths': paths}, indent='\t'))
		return 0

	lines = ['{}  {}'.format(row['id'], row['title'] or '(no title)')]
	where = ['round {}'.format(row['round']), 'parent {}'.format(row['parent']
		or 'none: the baseline')]
	if result.get('branch') or meta.get('branch'):
		where.append('branch ' + (result.get('branch') or meta.get('branch')))
	if row['commit']:
		where.append('commit ' + row['commit'])
	lines.append('{:<11}{}, {}'.format('category', row['category'] or '-',
		', '.join(where)))

	if not result:
		lines.append('{:<11}{}'.format('status', 'running: no result.json yet'
			if row['status'] == 'running' else 'unreadable: result.json is not '
			'valid JSON'))
	else:
		values = result.get('objective') or {}
		unit = ' ' + objective['unit'] if objective.get('unit') else ''
		lines.append('{:<11}{}'.format('status', status_words(config, result)))
		lines.append('{:<11}{}{} (std {}, range {} to {}, {} runs)'.format(
			'median', fmt(values.get('median'), 6), unit, fmt(values.get(
			'std'), 3), fmt(values.get('min'), 6), fmt(values.get('max'), 6),
			result.get('n_runs')))
		lines.append('{:<11}{}, {}imized'.format('objective',
			objective['name'], objective['direction']))

		for name, key, other in (('vs parent', 'vs_parent', parent),
				('vs A0', 'vs_base', 0)):
			if row[key] is not None and identifier != other:
				above = ((load_result(root, other) or {}).get('objective')
					or {}).get('median')
				lines.append('{:<11}{:+.4g}{} against {}\'s {}'.format(name,
					row[key], ' ({:+.1f}%)'.format(100.0 * row[key] / abs(above))
					if above else '', label(root, other), fmt(above, 6)))

		keep = result.get('keep') or {}
		if keep:
			lines.append('{:<11}{}: {}'.format('keep', keep.get('verdict'),
				keep.get('why') or ''))
		lines.append('{:<11}{}; on the frontier path: {}'.format('promoted',
			'yes' if row['promoted'] else 'no', 'yes' if row['frontier']
			else 'no'))

		for gate in config['gates']:
			record = (result.get('gates') or {}).get(gate['name']) or {}
			lines.append('{:<11}{} {} {}: {} (worst {})'.format('gate',
				gate['name'], gate['op'], fmt(gate['threshold']), 'pass'
				if record.get('pass') else 'FAIL', fmt(record.get('worst'), 6)))

		observed = ['{} {}'.format(entry['name'], fmt(((result.get(
			'observers') or {}).get(entry['name']) or {}).get('median'), 4))
			for entry in config['observers']]
		if observed:
			lines.append('{:<11}{}'.format('observers', ', '.join(observed)))

		if row['tests']:
			lines.append('{:<11}{}'.format('tests', row['tests']))
		if result.get('check'):
			lines.append('{:<11}{}'.format('check', check_line(result['check'])[
				len('check: '):]))
		if result.get('prediction'):
			lines.append('{:<11}{}'.format('prediction', prediction_note(root,
				result).lstrip('; ')))
		for failure in result.get('failures') or []:
			lines.append('{:<11}seed {}: {}'.format('failed', failure.get(
				'seed'), failure.get('error')))

	if diff:
		lines.append('{:<11}{}; {}'.format('change', diff['shortstat'],
			diff['command']))

	for heading, body in notes.items():
		lines += ['', '## ' + heading, body]

	lines += ['', 'files']
	lines += ['  {:<12}{}'.format(name, path) for name, path in paths.items()]
	print('\n'.join(lines))

	return 0


###
# tokens: what a search's agents spent, from Claude Code's transcripts
###

TOKEN_WEIGHTS = {'input_tokens': 1.0, 'cache_creation_input_tokens': 1.25,
	'cache_read_input_tokens': 0.05, 'output_tokens': 5.0}


def names_path(path, needle):
	"""Whether the file at `path` contains `needle` (bytes), read in
	chunks."""

	tail = b''
	try:
		with open(path, 'rb') as handle:
			while True:
				chunk = handle.read(1 << 22)
				if not chunk:
					return False
				if needle in tail + chunk:
					return True
				tail = chunk[-len(needle):]
	except OSError:
		return False


def own_mention(path, work, spawns=False):
	"""When a transcript first names `work` in its own words: its prompt, a
	user message, or one of its tool calls; never in a tool's output, which
	can name any search a `ps` listing shows. With `spawns`, only an Agent
	call does. None when it never does."""

	try:
		handle = open(path)
	except OSError:
		return None

	with handle:
		for line in handle:
			if work not in line:
				continue
			try:
				record = json.loads(line)
			except ValueError:
				continue
			message = record.get('message') or {}
			content = message.get('content') if isinstance(message, dict) \
				else None
			if record.get('type') == 'user' and not spawns:
				texts = [content] if isinstance(content, str) else [
					block.get('text') or '' for block in content or []
					if isinstance(block, dict) and block.get('type') == 'text']
				if any(work in text for text in texts):
					return record.get('timestamp') or ''
			elif record.get('type') == 'assistant':
				for block in content or []:
					if isinstance(block, dict) and block.get('type') == \
							'tool_use' and (not spawns or block.get('name') in (
								'Agent', 'Task')) and work in json.dumps(
								block.get('input')):
						return record.get('timestamp') or ''

	return None


def agent_role(description):
	"""An agent's role in a search, from its Agent-call description."""

	text = (description or '').strip()
	if text.startswith('Orchestrator: phase'):
		return 'phases'
	if text.lower().startswith('setup subagent'):
		return 'setup'
	if 'miner' in text.lower():
		return 'miners'
	if re.match(r'^[A-Z]\d+[:\s]', text) or text.lower().startswith(
			'iteration'):
		return 'iterations'
	if 'main session' in text.lower():
		return 'main session'
	return 'other'


def transcript_cost(path, since=None):
	"""`{turns, weighted, rebuilds, rebuild_cost, peak, first}` of one
	transcript: the last usage record of each request, from `since` (an ISO
	time) on. A rebuild is a turn after the first whose cache write is over
	half its context."""

	usage, order, first = {}, [], None
	with open(path) as handle:
		for line in handle:
			try:
				record = json.loads(line)
			except ValueError:
				continue
			message = record.get('message')
			if record.get('type') != 'assistant' or not isinstance(message,
					dict) or 'usage' not in message:
				continue
			if since and (record.get('timestamp') or '') < since:
				continue
			first = first or record.get('timestamp')
			request = record.get('requestId') or record.get('uuid')
			if request not in usage:
				order.append(request)
			usage[request] = message['usage']

	out = {'turns': len(order), 'weighted': 0.0, 'rebuilds': 0,
		'rebuild_cost': 0.0, 'peak': 0, 'first': first}
	for index, request in enumerate(order):
		item = usage[request]
		cost = sum((item.get(key) or 0) * weight
			for key, weight in TOKEN_WEIGHTS.items())
		context = sum(item.get(key) or 0 for key in ('input_tokens',
			'cache_read_input_tokens', 'cache_creation_input_tokens'))
		out['weighted'] += cost
		out['peak'] = max(out['peak'], context)
		if index and (item.get('cache_creation_input_tokens') or 0) > context / 2:
			out['rebuilds'] += 1
			out['rebuild_cost'] += cost

	return out


def tokens(args):
	"""What a search's agents spent, from the transcripts that name its
	directory in their own words (a prompt, a message, a tool call; not a
	tool's output): each session that spawned an agent for it, the main
	session, counted from its first mention, and each such subagent, by
	role."""

	root = resolve(args.root)
	work = os.path.dirname(root)
	needle = work.encode()
	projects = os.path.expanduser(args.projects)
	rows = []

	for session in sorted(glob.glob(os.path.join(projects, '*', '*.jsonl'))):
		folder = os.path.join(session[:-len('.jsonl')], 'subagents')
		ident = os.path.basename(session)[:-len('.jsonl')]
		agents = [path for path in sorted(glob.glob(os.path.join(folder,
			'*.jsonl'))) if names_path(path, needle)
			and own_mention(path, work) is not None]
		main = (not args.subagents_only and ident not in args.exclude_session
			and (ident in args.session or (not args.session and names_path(
				session, needle) and own_mention(session, work, spawns=True)
				is not None)))
		# A session starts on other work: count it from its own first mention.
		start = own_mention(session, work) if main else None
		if start is not None:
			rows.append(('main session', 'session ' + ident[:8],
				transcript_cost(session, max(start, args.since or ''))))
		for path in agents:
			try:
				with open(path[:-len('.jsonl')] + '.meta.json') as handle:
					description = json.load(handle).get('description') or ''
			except (OSError, ValueError):
				description = os.path.basename(path)
			cost = transcript_cost(path, args.since)
			if cost['turns']:
				rows.append((agent_role(description), description, cost))

	if not rows:
		fail('no transcript under {} names {}'.format(projects, work))

	rows.sort(key=lambda row: row[2]['first'] or '')
	roles = ('main session', 'phases', 'setup', 'miners', 'iterations', 'other')

	if args.json:
		print(json.dumps([{'role': role, 'agent': name, **cost}
			for role, name, cost in rows], indent='\t'))
		return 0

	print('tokens for {}, weighted: input 1, cache write 1.25, cache read '
		'0.05, output 5 (the transcripts under-record output)'.format(work))
	print()
	print('{:<46} {:<12} {:>5} {:>9} {:>5} {:>9} {:>7}'.format('agent', 'role',
		'turns', 'k wtd', 'rbld', 'rbld k', 'peak k'))
	for role, name, cost in rows:
		print('{:<46} {:<12} {:>5} {:>9.0f} {:>5} {:>9.0f} {:>7.0f}'.format(
			name[:46], role, cost['turns'], cost['weighted'] / 1e3,
			cost['rebuilds'], cost['rebuild_cost'] / 1e3, cost['peak'] / 1e3))
	print()
	print('{:<14} {:>6} {:>6} {:>9} {:>9}'.format('role', 'agents', 'turns',
		'k wtd', 'rbld k'))
	total = 0.0
	for role in roles:
		chosen = [cost for kind, _, cost in rows if kind == role]
		if not chosen:
			continue
		spent = sum(cost['weighted'] for cost in chosen)
		total += spent
		print('{:<14} {:>6} {:>6} {:>9.0f} {:>9.0f}'.format(role, len(chosen),
			sum(cost['turns'] for cost in chosen), spent / 1e3,
			sum(cost['rebuild_cost'] for cost in chosen) / 1e3))
	print('{:<14} {:>6} {:>6} {:>9.0f}'.format('all', len(rows), sum(
		cost['turns'] for _, _, cost in rows), total / 1e3))
	iterations = [cost['weighted'] for kind, _, cost in rows
		if kind == 'iterations']
	if iterations:
		print('\nper iteration: {:.0f}k weighted, mean of {}'.format(
			statistics.fmean(iterations) / 1e3, len(iterations)))
	return 0


###
# background: a long command detached, with its own log; step 2's logs
###

BACKGROUND = 'background.jsonl'


def start_detached(line, log, root=None):
	"""Run the shell line `line` detached in a session of its own: its
	output, then an `exit <code>` line, go to `log`. With `root`, the job is
	listed in `<root>/background.jsonl`, where a watcher finds it. Returns
	the pid."""

	os.makedirs(os.path.dirname(log), exist_ok=True)

	# A wait on an earlier log of the same name starts from the top again.
	with open(log + '.seen', 'w') as out:
		out.write('0\n')

	if os.path.exists(log + '.reported'):
		os.remove(log + '.reported')

	process = subprocess.Popen('({}) > {} 2>&1; echo "exit $?" >> {}'.format(
		line, shlex.quote(log), shlex.quote(log)), shell=True, cwd=os.getcwd(),
		stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL, env=dict(os.environ, AUTOOPT_DETACHED='1'),
		start_new_session=True)

	if root:
		with open(os.path.join(root, BACKGROUND), 'a') as out:
			out.write(json.dumps({'log': log, 'pid': process.pid, 'started':
				now()}) + '\n')

	return process.pid


def background_jobs(root):
	"""`[(log, state, code)]` of the newest listed job of each log that no
	`wait` or `watch` has reported yet: `running`, `finished` with its exit
	code, or `died` (its process is gone and the log has no exit line)."""

	jobs = {}

	try:
		with open(os.path.join(root, BACKGROUND)) as handle:
			for line in handle:
				if line.strip():
					entry = json.loads(line)
					jobs[entry['log']] = entry
	except (OSError, ValueError):
		return []

	found = []

	for log, entry in sorted(jobs.items()):
		if os.path.exists(log + '.reported'):
			continue

		try:
			with open(log, 'rb') as handle:
				lines = handle.read().decode('utf-8', 'replace').split('\n')
		except OSError:
			lines = []

		lines = [line for line in lines if line.strip()]
		ended = WAIT_EXIT.match(lines[-1].strip()) if lines else None

		if ended:
			found.append((log, 'finished', ended.group(1)))
			continue

		# Gone, or exited and not reaped by the process that started it.
		try:
			with open('/proc/{}/stat'.format(entry['pid'])) as handle:
				alive = handle.read().rsplit(')', 1)[1].split()[0] != 'Z'
		except (OSError, IndexError):
			alive = False

		found.append((log, 'running' if alive else 'died', None))

	return found


def mark_reported(log):
	with open(log + '.reported', 'w') as out:
		out.write(now() + '\n')


def detach(script, argv, log, root=None):
	"""Start `script argv` again without --background, detached
	(`start_detached`). Prints how to watch it with `wait`."""

	# argparse accepts `--back` for `--background`; the child must not see it.
	argv = [arg for arg in argv if not (len(arg) > 2 and '--background'
		.startswith(arg))]
	pid = start_detached(' '.join(shlex.quote(part) for part in [
		sys.executable, script] + argv), log, root)
	print('started in the background (pid {}), writing {}. Watch it, again '
		'until it prints `wait: finished`:\n\n  python3 {} wait {} --pid {}'
		.format(pid, log, os.path.join(SCRIPTS, 'autoopt.py'), log, pid))
	return 0


def bg(args):
	"""`bg WORK LOG COMMAND`: the main session's long command that has no
	`--background` of its own, started detached and listed for the watch."""

	root = resolve(args.root)
	log = os.path.abspath(os.path.expanduser(args.log))
	pid = start_detached(args.command, log, root)
	print('started in the background (pid {}), writing {}. Your next call, '
		'again until it prints `wait: finished`:\n\n  python3 {} wait {} '
		'--pid {}\n\nDo not end the turn on it with no wait running ({}).'.format(pid, log,
		os.path.join(SCRIPTS, 'autoopt.py'), log, pid, os.path.dirname(root)))
	return 0


class StepLog:
	"""Copy what a step-2 command prints, its errors included, into
	`setup/<name>.log`, ending `exit <code>`: the file the setup notes cite
	and a successor reads. Detached, the shell already writes that log."""

	def __init__(self, root, name):
		self.path = os.path.join(root, 'setup', name + '.log')
		self.handle = None

	def __enter__(self):
		if os.environ.get('AUTOOPT_DETACHED'):
			return self
		os.makedirs(os.path.dirname(self.path), exist_ok=True)
		self.handle = open(self.path, 'w')
		self.streams = sys.stdout, sys.stderr
		sys.stdout, sys.stderr = (self.Copy(stream, self.handle)
			for stream in self.streams)
		return self

	class Copy:
		def __init__(self, stream, handle):
			self.stream, self.handle = stream, handle

		def write(self, text):
			self.stream.write(text)
			self.handle.write(text)

		def flush(self):
			self.stream.flush()
			self.handle.flush()

	def __exit__(self, kind, error, trace):
		if self.handle is None:
			return False
		sys.stdout, sys.stderr = self.streams
		code = 0 if kind is None else (error.code or 0) if isinstance(error,
			SystemExit) else 1
		self.handle.write('exit {}\n'.format(code))
		self.handle.close()
		return False


def busy_processes(root, seconds=2.0, threshold=None):
	"""`[(pid, user, percent of one core, command)]`, busiest first: the
	processes outside this search above `threshold` percent over `seconds`
	(default 110, step 2's "above about 100% CPU"), from /proc. Empty where
	/proc is missing."""

	if not os.path.isdir('/proc/self'):
		return []

	import pwd

	threshold = threshold or float(os.environ.get('AUTOOPT_BUSY_PERCENT', 110))
	tick = os.sysconf('SC_CLK_TCK')
	work = os.path.realpath(os.path.dirname(root))
	mine = {os.getpid(), os.getppid()}

	def sample():
		out = {}
		for name in os.listdir('/proc'):
			if not name.isdigit():
				continue
			try:
				with open('/proc/{}/stat'.format(name)) as handle:
					fields = handle.read().rsplit(')', 1)[1].split()
				out[int(name)] = int(fields[11]) + int(fields[12])
			except (OSError, IndexError, ValueError):
				continue
		return out

	before = sample()
	time.sleep(seconds)
	after = sample()
	busy = []

	for pid, ticks in after.items():
		percent = 100.0 * (ticks - before.get(pid, ticks)) / tick / seconds
		if percent <= threshold or pid in mine:
			continue
		try:
			cwd = os.path.realpath(os.readlink('/proc/{}/cwd'.format(pid)))
		except OSError:
			cwd = ''
		try:
			with open('/proc/{}/cmdline'.format(pid), 'rb') as handle:
				command = handle.read().replace(b'\0', b' ').decode(
					errors='replace').strip()
			user = pwd.getpwuid(os.stat('/proc/{}'.format(pid)).st_uid).pw_name
		except (OSError, KeyError):
			continue
		if cwd.startswith(work) or work in command:
			continue
		busy.append((pid, user, percent, command[:80]))

	return sorted(busy, key=lambda item: -item[2])


def busy_words(busy):
	"""`pid 12 ana at 300% CPU: modisco …; …` for busy_processes' records
	(tuples, or the dicts result.json keeps)."""

	rows = [(item['pid'], item['user'], item['percent'], item['command'])
		if isinstance(item, dict) else item for item in busy]
	return '; '.join('pid {} {} at {:.0f}% CPU: {}'.format(*row)
		for row in rows)


def busy(args):
	"""Step 2's quiet check: the processes outside the search above about
	100% CPU over two seconds. Exit 1 when there is one."""

	root = resolve(args.root)
	found = busy_processes(root, seconds=args.seconds)

	if not found:
		print('busy: no process outside this search is above about 100% CPU: '
			'the machine is quiet')
		return 0

	print('busy: {} process(es) outside this search above about 100% CPU '
		'over {:g} s:'.format(len(found), args.seconds))
	for pid, user, percent, command in found:
		print('  pid {:<8} {:<10} {:>5.0f}%  {}'.format(pid, user, percent,
			command))
	print('A busy machine before the baseline is the user\'s decision '
		'(references/step2-baseline.md, *Check the machine is quiet*)')
	return 1


###
# watch: the orchestrator's fallback wake-up, on events, not a timer
###

WATCH_CAP = 3 * 3600    # seconds a watch runs at most
WATCH_EVERY = 60        # seconds between its looks at the records
WATCH_IDLE = 1800       # it ends once nothing has been in flight this long


def watch_path(root):
	return os.path.join(root, 'watch.json')


def live_watch(root):
	"""The running watch's record (`pid`, `started`), or None."""

	try:
		with open(watch_path(root)) as handle:
			record = json.load(handle)
		os.kill(int(record['pid']), 0)
	except (OSError, ValueError, KeyError, TypeError):
		return None

	if os.path.isdir('/proc/self'):
		try:
			with open('/proc/{}/cmdline'.format(record['pid']), 'rb') as handle:
				argv = handle.read().split(b'\0')
		except OSError:
			return None
		if b'watch' not in argv:
			return None

	return record


def watch_events(root, state):
	"""`(events, in_flight)`: the `(kind, id)` pairs the orchestrator acts
	on, by status's own rules (a running iteration silent past
	STALLED_AFTER; a measured one with no README silent past
	FINISHING_WINDOW), and whether anything is in flight: an iteration being
	worked on, a pending brief, an open miner stage or step 2."""

	events, busy = set(), False

	for identifier in iteration_ids(root):
		if identifier == 0:
			continue
		directory = iteration_dir(root, identifier)
		if os.path.exists(os.path.join(directory, 'README.md')):
			continue
		idle = last_activity(directory)
		if load_result(root, identifier) is None:
			busy = True
			if idle is not None and idle > STALLED_AFTER:
				events.add(('is STALLED', identifier))
		elif idle is not None and idle > FINISHING_WINDOW:
			events.add(('has no README after an hour', identifier))
		else:
			busy = True

	mining = state.get('mining') or {}
	busy = (busy or bool(pending_briefs(root))
		or mining.get('stage', 0) > mining.get('done', 0)
		or (state.get('setup') or {}).get('status') in ('open', 'waiting'))

	return events, busy


def watch(args):
	"""Run in the background in place of a timer: return only when the
	orchestrator has something to act on that `status` would flag (a
	subagent gone STALLED, a measured iteration whose README nobody has
	written for an hour), once nothing has been in flight for WATCH_IDLE, or
	after --cap. An event already present when it starts does not end it.
	A subagent's report wakes the session without it."""

	root = resolve(args.root)
	running = live_watch(root)

	if running:
		print('watch: one is already running (pid {}, since {}); this one '
			'stops'.format(running['pid'], running.get('started', '?')))
		return 0

	known, busy = watch_events(root, load_state(root))

	if not busy:
		print('watch: nothing is in flight (no subagent working, no pending '
			'brief, no open miner stage or step 2), so nothing to wait for')
		return 0

	write_json(watch_path(root), {'pid': os.getpid(), 'started': now()})
	start = last_busy = time.monotonic()
	advice = 'run `autoopt.py brief <work> --next`'

	try:
		while True:
			time.sleep(args.every)
			events, busy = watch_events(root, load_state(root))
			fresh = sorted(events - known, key=lambda item: item[1])
			clock = time.monotonic()
			if busy:
				last_busy = clock
			if fresh:
				print('watch: {}: {} (references/step3-rounds.md, *A subagent '
					'that goes quiet*)'.format('; '.join('{} {}'.format(
						label(root, identifier), kind) for kind, identifier
						in fresh), advice))
				return 0
			# One that cleared and comes back is new again.
			known &= events
			if clock - last_busy >= WATCH_IDLE:
				print('watch: nothing has been in flight for {}; it '
					'stops'.format(human_seconds(WATCH_IDLE)))
				return 0
			if clock - start >= args.cap:
				print('watch: {} with nothing new to act on{}; {}, then start '
					'the watch again'.format(human_seconds(args.cap),
						' (still flagged from before it started: {})'.format(
							', '.join('{} {}'.format(label(root, identifier),
								kind) for kind, identifier in sorted(known,
								key=lambda item: item[1]))) if known else '',
						advice))
				return 0
	finally:
		try:
			if (live_watch(root) or {}).get('pid') == os.getpid():
				os.remove(watch_path(root))
		except OSError:
			pass


###
# wait: a background command, watched in calls of at most four minutes
###

def wait(args):
	"""Return within --max seconds with the lines `log` gained since the
	previous `wait` on it, and whether the command has finished: its last
	line is `exit <code>` (written by `(<command>; echo "exit $?")`), or
	--pid is no longer running."""

	if not 1 <= args.max <= WAIT_LIMIT:
		fail('--max is between 1 and {} seconds: a turn after more than five '
			'idle minutes re-writes the whole context'.format(WAIT_LIMIT))

	log = os.path.abspath(args.log)
	seen = log + '.seen'
	deadline = time.monotonic() + args.max

	def tail():
		try:
			with open(log, 'rb') as infile:
				lines = infile.read().decode('utf-8', 'replace').splitlines()
		except OSError:
			return None
		lines = [line for line in lines if line.strip()]
		return WAIT_EXIT.match(lines[-1].strip()) if lines else None

	def gone():
		if args.pid is None:
			return False
		try:
			os.kill(args.pid, 0)
		except ProcessLookupError:
			return True
		except PermissionError:
			return False
		# A child of this process that has exited but not been reaped.
		try:
			with open('/proc/{}/stat'.format(args.pid)) as infile:
				return infile.read().rsplit(')', 1)[1].split()[0] == 'Z'
		except (OSError, IndexError):
			return False

	while True:
		ended, dead = tail(), gone()
		if ended or dead or time.monotonic() >= deadline:
			break
		time.sleep(min(5.0, max(0.1, deadline - time.monotonic())))

	try:
		with open(seen) as infile:
			offset = int(infile.read().strip() or 0)
	except (OSError, ValueError):
		offset = 0

	try:
		with open(log, 'rb') as infile:
			infile.seek(0, os.SEEK_END)
			if offset > infile.tell():
				offset = 0
			infile.seek(offset)
			fresh = infile.read()
	except OSError:
		fresh = b''

	# Only whole lines: a line still being written is shown next time.
	if fresh and not fresh.endswith(b'\n') and not (ended or dead):
		fresh = fresh[:fresh.rfind(b'\n') + 1]

	if os.path.exists(log):
		with open(seen, 'w') as out:
			out.write('{}\n'.format(offset + len(fresh)))

	lines = fresh.decode('utf-8', 'replace').splitlines()
	if len(lines) > WAIT_SHOWN:
		print('… {} earlier new lines in {}'.format(len(lines) - WAIT_SHOWN,
			log))
		lines = lines[-WAIT_SHOWN:]
	for line in lines:
		print(line)

	if ended:
		mark_reported(log)
		print('wait: finished, exit {}'.format(ended.group(1)))
	elif dead:
		print('wait: finished: pid {} is no longer running'.format(args.pid))
	elif not os.path.exists(log) and args.pid is None:
		print('wait: {} does not exist yet, and no --pid was given; after {} s '
			'run this command again'.format(log, args.max))
	else:
		print('wait: still running after {} s; run this command again'.format(
			args.max))

	return 0


# Functions `add(subparsers)` of a script that imports this one and adds
# its own subcommands before calling `main`.
EXTRA_PARSERS = []


def build_parser():
	"""Every subcommand's parser: what main parses, and where a prompt
	takes a subcommand's usage line from."""

	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	subparsers = parser.add_subparsers(dest='subcommand', required=True)

	one = subparsers.add_parser('init', help='create autooptimize/ and the '
		'baseline worktree')
	one.add_argument('root', help='the working directory, or its '
		'autooptimize/ subdirectory')
	one.add_argument('--task', required=True, help='one line on what is '
		'being optimized')
	one.add_argument('--command', required=True, help='how one run is '
		'launched; must contain {seed} and {out}')
	one.add_argument('--objective', required=True, help='the key the '
		'benchmark emits as "objective"')
	one.add_argument('--direction', required=True, choices=('max', 'min'))
	one.add_argument('--unit', default='')
	one.add_argument('--gates', help='JSON list of '
		'{"name","op","threshold"[,"tol"]}')
	one.add_argument('--observers', help='comma-separated names, recorded '
		'but never optimized or gated')
	one.add_argument('--runs', type=int, default=5,
		help='runs per iteration, each one seed (default: %(default)s)')
	one.add_argument('--staged', action='store_true', help='screen at --runs, '
		'then confirm to --confirm-runs when the median nears the frontier')
	one.add_argument('--confirm-runs', type=int, help='with --staged, the '
		'confirmation depth (default 10)')
	one.add_argument('--no-staged', action='store_true',
		help=argparse.SUPPRESS)
	one.add_argument('--seed-base', type=int, default=0)
	one.add_argument('--subagent-model', choices=SUBAGENT_MODELS,
		default='opus', help='the model iteration subagents are spawned '
		'with (default: %(default)s)')
	one.add_argument('--promote', choices=PROMOTE_MODES, default='auto',
		help='tree mode: auto, the runner promotes an iteration that passes '
		'the default keep rule while its parent is still the root; '
		'orchestrator, for a keep rule the user wrote (default: %(default)s)')
	one.add_argument('--after', metavar='JSON', help='the step 0 answers '
		'about everything after the search (references/step0-interview.md, '
		'call 5): any of {}; the rest default to asking'.format(
		', '.join(AFTER_DEFAULTS)))
	one.add_argument('--build-env', action='append', metavar='VAR=VALUE',
		help='a compiler or flag variable every build sees (CC, CFLAGS, ...); '
		'repeatable. Without it, the build variables set in this shell are '
		'recorded. No run inherits them from anywhere else')
	one.add_argument('--gpus-per-agent', type=int, default=1,
		help='GPUs each iteration leases, and so how many of its runs go at '
			'once (default: %(default)s). Raise it when the search is narrow '
			'-- one or two ideas at a time -- and you would rather spend the '
			'machine on finishing each iteration sooner')
	one.add_argument('--timeout', type=float, default=0,
		help='seconds before one run is killed; 0 disables')
	one.add_argument('--no-gpu', action='store_true',
		help='the benchmark does not need a GPU, so runs need no lease')
	one.add_argument('--floor', type=float, help='a minimized objective: '
		'the smallest value still worth measuring, in its unit (the user\'s '
		'answer). The search hands back when the root reaches it, and the '
		'price table says how many times A0 is above it')
	one.add_argument('--workers', type=int, help='the worker, rank or thread '
		'count the task names (`-p 4`, 8 threads): A0\'s runs must show that '
		'many threads running at once, or setup does not close')
	one.add_argument('--gpu-command', help='the machine\'s GPU allocator, as '
		'the words that go before a command and run it under a lease with '
		'CUDA_VISIBLE_DEVICES set (e.g. "gpu-alloc run -n 1 --", where '
		'gpu-alloc stands for the allocator; on a machine with none, "env '
		'CUDA_VISIBLE_DEVICES=<index>"): `end`, `final` and anything run on '
		'this search afterwards start under it when they hold no lease')
	one.add_argument('--validation-command', help='optional external '
		'validation, run once a round against the root; must contain '
		'{iteration} and {out}, and {src} is the iteration\'s code checkout. '
		'Recorded and reported, never gated on')
	one.add_argument('--validation-measures', help='comma-separated names '
		'the validation emits under "measures"')
	one.add_argument('--validation-note', default='', help='one line on what '
		'the validation covers, shown wherever its numbers appear')
	one.add_argument('--copy', action='append', help='a file or directory to '
		'copy into the baseline (into harness/ with --repo); repeatable')
	one.add_argument('--repo', help='repository mode: clone this git '
		'repository into repo.git, at the branch it has checked out, and put '
		'every iteration\'s worktree in iterations/<id>/src. The original is '
		'never written to')
	one.add_argument('--allow-dirty', action='store_true', help='with --repo, '
		'start from the last commit even though the original has uncommitted '
		'changes to tracked files; they are not copied')
	one.add_argument('--tests', help='a command run once per iteration in its '
		'worktree, after the first run passes its gates. A non-zero exit '
		'fails the iteration (gate-failed). {src}, {iteration}, {out} (the '
		'unit-tests/ directory), {cache} and {selected} (see --tests-path) '
		'are substituted, shell-quoted')
	one.add_argument('--tests-path', default='tests', help='the test '
		'directory, relative to the worktree. With {selected} in --tests, '
		'an iteration runs only the test_<module>.py files for the modules '
		'it changed, and the whole of this directory once its median passes '
		'the keep rule')
	one.add_argument('--tests-timeout', type=float, default=0,
		help='seconds before the test command is killed and counted as a '
			'failure; 0 disables')
	one.add_argument('--force', action='store_true', help='rewrite the '
		'config.json of an existing search; state.json, spec.md and the '
		'baseline\'s meta.json are kept')
	one.set_defaults(func=init)

	two = subparsers.add_parser('new', help='fork an iteration off its parent')
	two.add_argument('root')
	two.add_argument('--new-id', '--id', dest='id', help='the id this '
		'iteration takes (--id is the same flag). Every other command\'s --id '
		'names an existing iteration. Default: one past the highest id an '
		'iteration, an autoopt/<id> branch, an iterations/<id>.* directory '
		'or a pending brief holds')
	two.add_argument('--parent', help='default: the current root')
	two.add_argument('--brief', metavar='ID', help='create the iteration a '
		'brief describes, with the arguments `brief` recorded for it in '
		'briefs/<id>.json; no other flag is given with it')
	two.add_argument('--title', help='required without --brief')
	two.add_argument('--category', choices=CATEGORIES, help='required '
		'without --brief')
	two.add_argument('--topic', help='the subagent topic this came from')
	two.add_argument('--predict', type=float, help='the predicted change in '
		'the objective\'s median against the parent\'s, in its units '
		'(-0.03 for 0.03 s faster); recorded, compared in the round summary')
	two.add_argument('--predict-basis', help='what the prediction was built '
		'from ("iteration 88 measured -0.026 on 86")')
	two.add_argument('--control', action='store_true', help='a same-code '
		'control: the parent re-measured unchanged')
	two.add_argument('--idea-key', help='set by brief --idea-line: the '
		'ideas.md line finish strikes when the iteration is written up')
	two.add_argument('--after-end', action='store_true', help='an iteration '
		'made after `end` (a notice, a fix, a clean-up): the only kind `new` makes once the search has ended; '
		'promoted, it becomes the winner')
	two.set_defaults(func=new)

	ledger_parser = subparsers.add_parser('ledger', help='append entries to '
		'knowledge.md under its lock; one ending (corrects: <words>) strikes '
		'the old entry')
	ledger_parser.add_argument('root')
	ledger_parser.add_argument('--add', required=True, help='a file of '
		'entries, one per line')
	ledger_parser.add_argument('--by', required=True, help='the label to '
		'credit a correction to (A7, or S2 for the idea miner)')
	ledger_parser.set_defaults(func=ledger)

	watcher = subparsers.add_parser('watch', help='the orchestrator\'s '
		'fallback wake-up, run in the background: returns when a subagent '
		'goes STALLED or leaves a measured iteration without a README, '
		'not on a timer')
	watcher.add_argument('root')
	watcher.add_argument('--cap', type=float, default=WATCH_CAP,
		help='seconds to run at most (default %(default)s)')
	watcher.add_argument('--every', type=float, default=WATCH_EVERY,
		help='seconds between looks at the records (default %(default)s)')
	watcher.set_defaults(func=watch)

	starter = subparsers.add_parser('bg', help='start a long command in the '
		'background with its log, listed in background.jsonl; for a command with no --background of its own')
	starter.add_argument('root')
	starter.add_argument('log', help='the file its output goes to, ending '
		'with `exit <code>`')
	starter.add_argument('command', help='the command, one shell line in '
		'quotes')
	starter.set_defaults(func=bg)

	waiter = subparsers.add_parser('wait', help='watch a background '
		'command for at most four minutes, printing the lines its log gained')
	waiter.add_argument('log', help='the file the command writes, ending '
		'with `exit <code>`: `(<command>; echo "exit $?") > <log> 2>&1 &`')
	waiter.add_argument('--pid', type=int, help='also finished when this '
		'process is no longer running')
	waiter.add_argument('--max', type=int, default=WAIT_DEFAULT,
		help='seconds to wait at most (default %(default)s), up to {}'.format(
			WAIT_LIMIT))
	waiter.set_defaults(func=wait)

	brief_parser = subparsers.add_parser('brief', help='write and print the '
		'prompt for the subagent that runs one iteration')
	brief_parser.add_argument('root')
	brief_parser.add_argument('--new-id', '--id', dest='id', help='the id '
		'the iteration will take (--id is the same flag); '
		'not yet used. Required unless --prompt is given')
	brief_parser.add_argument('--prompt', metavar='ID', help='print the spawn '
		'prompt of an existing brief again, for a subagent that takes it over, '
		'and write nothing')
	brief_parser.add_argument('--next', action='store_true', help='print '
		'status --brief and any round summary written since the last call, '
		'then brief each free slot from the top untried ideas, filling the '
		'id, title, category, tried and sections from the records; a flag '
		'given beside it overrides that field')
	brief_parser.add_argument('--count', type=int, help='with --next: how '
		'many to brief (default: the free slots)')
	brief_parser.add_argument('--parent', help='default: the current root')
	brief_parser.add_argument('--title')
	brief_parser.add_argument('--category', choices=CATEGORIES)
	brief_parser.add_argument('--topic')
	brief_parser.add_argument('--predict', type=float, help='the predicted '
		'change in the objective\'s median against the parent\'s, in its units')
	brief_parser.add_argument('--predict-basis')
	brief_parser.add_argument('--idea', help='what to change, where, why, and '
		'each premise to confirm; text, or @file. Required unless --idea-line '
		'gives it')
	brief_parser.add_argument('--idea-line', help='the ideas.md line the '
		'idea comes from, by number (1-based) or by text only that untried '
		'line contains: its text is the idea when --idea is not given, and '
		'the subagent\'s finish strikes the line')
	brief_parser.add_argument('--tried', help='the iterations '
		'that already touched this code and what each measured, or "none"; '
		'text, or @file')
	brief_parser.add_argument('--sections', help='the knowledge.md tags that '
		'matter for this idea')
	brief_parser.add_argument('--checks', help='checks owed beyond the '
		'column of the checks table; text, or @file')
	brief_parser.add_argument('--gpus', help='the GPU indices this iteration '
		'holds, or the command that allocates them; required unless "gpu" is '
		'false')
	brief_parser.add_argument('--validate', action='store_true',
		help='ask the subagent to run the approved external validation on '
			'its own iteration')
	brief_parser.add_argument('--note', action='append', help='one more line '
		'for the brief\'s Notes; repeatable; text, or @file')
	brief_parser.add_argument('--replace', action='store_true', help='rewrite '
		'a brief whose subagent has not yet run `new`')
	brief_parser.add_argument('--control', action='store_true', help='brief a '
		'same-code control, the parent re-measured unchanged')
	brief_parser.add_argument('--after-end', action='store_true', help='brief '
		'an iteration made after the end, after `end` (`new --after-end`)')
	brief_parser.set_defaults(func=brief)

	three = subparsers.add_parser('status', help='the table of every '
		'settled iteration')
	three.add_argument('root')
	three.add_argument('--round', type=int)
	three.add_argument('--json', action='store_true')
	three.add_argument('--brief', '--header', dest='brief',
		action='store_true', help='the header and '
		'its notices, and only the last --last settled iterations with the '
		'gates folded into one verdict: what the orchestrator reads after '
		'each report')
	three.add_argument('--resume', action='store_true', help='--brief, then '
		'what a session picking up the search reads: each unfinished '
		'iteration with what to do about it (a live runner, an empty fork, a '
		'subagent to hand it to), the plan, the rules, the last round summary '
		'and the next ideas')
	three.add_argument('--last', type=int, default=6)
	three.set_defaults(func=status)

	checker = subparsers.add_parser('check', help='the task\'s checks on one '
		'iteration by hand, as the runner runs them; or (--reference) cache '
		'A0\'s compare outputs now')
	checker.add_argument('root')
	checker.add_argument('--id', help='the iteration')
	checker.add_argument('--reference', action='store_true', help='cache '
		'A0\'s compare outputs, when they are missing or the gates or the '
		'checks changed since; the runner does this itself before a compare')
	checker.set_defaults(func=check)

	setter = subparsers.add_parser('setup', help='step 2 for a subagent: open '
		'it and print the setup subagent\'s prompt; --ask, --answer, --force, '
		'--done')
	setter.add_argument('root')
	group = setter.add_mutually_exclusive_group()
	group.add_argument('--ask', help='the setup subagent: record a question '
		'for the user; it joins the one stop of step 2')
	setter.add_argument('--recommend', help='with --ask: the answer you '
		'recommend. Under after.setup_questions=recommended it is taken and '
		'nothing waits')
	group.add_argument('--answer', help='the orchestrator: record the user\'s '
		'answer and print the prompt that continues step 2')
	group.add_argument('--force', action='store_true', help='print a prompt '
		'for a new subagent that continues after one died')
	group.add_argument('--done', action='store_true', help='the setup '
		'subagent: close step 2; refuses, naming what is missing')
	setter.set_defaults(func=setup)

	dropper = subparsers.add_parser('drop', help='strike an untried '
		'ideas.md line whose premise is gone, so brief --next skips it')
	dropper.add_argument('root')
	dropper.add_argument('--idea-line', required=True, help='its number, or '
		'text only that untried line contains')
	dropper.add_argument('--reason', required=True, help='why, e.g. '
		'"already in the root since B13"')
	dropper.set_defaults(func=drop)

	miner = subparsers.add_parser('mine', help='open a stage of the idea '
		'miner and print its prompt; --done appends its ideas and closes it')
	miner.add_argument('root')
	miner.add_argument('--done', action='store_true', help='append the open '
		'stage\'s idea lines (mining/stage-<n>.md) to ideas.md and its '
		'entries (mining/ledger-S<n>.txt, if any) to the ledger, then close '
		'it')
	miner.add_argument('--force', action='store_true', help='open the next '
		'stage although one is open (its miner died)')
	miner.add_argument('--without-profile', action='store_true', help='open '
		'stage S1 although A0 has no profile yet (the ideas are then priced '
		'blind)')
	miner.set_defaults(func=mine)

	prover = subparsers.add_parser('prove', help='step 2: copies of A0 for '
		'planted faults, then each one\'s gates and compare against A0')
	prover.add_argument('root')
	prover.add_argument('--make', metavar='NAMES', help='comma-separated: one '
		'copy of A0\'s code each, added to the scratch directory under the '
		'search (made by the first --make)')
	prover.add_argument('copies', nargs='*', help='the copies to run '
		'(default: every one in the scratch directory)')
	prover.add_argument('--dir', help='the scratch directory (default: the '
		'one --make made last)')
	prover.add_argument('--compare', help='a compare command with {src} and '
		'{reference} to prove before it is registered (default: '
		'checks.compare)')
	prover.add_argument('--id', help='an iteration: prove its checker '
		'(--checker, --checker-row, --faults) instead of step 2\'s gates')
	prover.add_argument('--checker', help='with --id: the checker\'s file or '
		'directory')
	prover.add_argument('--checker-row', help='with --id: its `key: value` '
		'row, as finish takes it; name and run are what prove reads')
	prover.add_argument('--faults', help='with --id: the planted faults, each '
		'`## <name>`, `file: <path>`, then <<< old === new >>> lines')
	prover.add_argument('--background', action='store_true', help='the gate '
		'or compare proof detached, writing setup/prove-gates.log or '
		'setup/prove-compare.log, and return')
	prover.add_argument('--timeout', type=float, default=CHECKER_LIMIT,
		help='with --id: seconds each checker run may take (default '
		'%(default)s)')
	prover.set_defaults(func=prove)

	lister = subparsers.add_parser('checkers', help='the registered '
		'checkers, marking those that guard a file the iteration changed; '
		'--name or --touched runs them on its parent and its edit')
	lister.add_argument('root')
	lister.add_argument('--id', required=True)
	lister.add_argument('--name', action='append', help='a checker\'s '
		'directory under tools/checkers/ (repeatable)')
	lister.add_argument('--touched', action='store_true', help='every checker '
		'whose files the edit changes')
	lister.add_argument('--timeout', type=float, default=1800,
		help='seconds each checker may take (default %(default)s)')
	lister.set_defaults(func=checkers)

	fixed = subparsers.add_parser('harness', help='check the harness '
		'against the manifest `setup --done` recorded, or --accept a change')
	fixed.add_argument('root')
	fixed.add_argument('--accept', action='store_true')
	fixed.add_argument('--reason')
	fixed.set_defaults(func=harness)

	tokener = subparsers.add_parser('tokens', help='what the search\'s agents '
		'spent, by agent and role, from the Claude Code transcripts that name '
		'it')
	tokener.add_argument('root')
	tokener.add_argument('--since', help='an ISO time (UTC): count only '
		'records from then on')
	tokener.add_argument('--subagents-only', action='store_true',
		help='leave out the sessions themselves, as when a test ran the main '
			'session as a subagent')
	tokener.add_argument('--session', action='append', default=[],
		help='a main session\'s id (repeatable), counted in place of the '
			'sessions that spawned agents for the search')
	tokener.add_argument('--exclude-session', action='append', default=[],
		help='a session id to leave out, such as one that only looked at the '
			'search')
	tokener.add_argument('--projects', default='~/.claude/projects',
		help='where Claude Code keeps transcripts (default %(default)s)')
	tokener.add_argument('--json', action='store_true')
	tokener.set_defaults(func=tokens)

	pricer = subparsers.add_parser('price', help='step 2: what the later '
		'steps that run the base cost, from A0\'s runs')
	pricer.add_argument('root')
	pricer.set_defaults(func=price)

	busier = subparsers.add_parser('busy', help='step 2: the processes '
		'outside the search above about 100%% CPU; exit 1 when there is one')
	busier.add_argument('root')
	busier.add_argument('--seconds', type=float, default=2.0,
		help='how long to sample (default %(default)s)')
	busier.set_defaults(func=busy)

	looper = subparsers.add_parser('loop', help='open a phase of the '
		'orchestrator subagent, which runs the rounds for the main session, '
		'and print its prompt; --done closes it')
	looper.add_argument('root')
	looper.add_argument('--force', action='store_true', help='open the next '
		'phase although one is open (its subagent died)')
	looper.add_argument('--done', action='store_true', help='close the open '
		'phase, before its subagent reports')
	looper.set_defaults(func=loop)

	profiler = subparsers.add_parser('profile', help='run checks.profile on '
		'an iteration (default: the current root) and write its profile.txt')
	profiler.add_argument('root')
	profiler.add_argument('--id', help='default: the current root')
	profiler.set_defaults(func=profile)

	varier = subparsers.add_parser('variant', help='export an iteration\'s '
		'current edit (or its parent\'s code) into probe/<name>/, built '
		'with checks.build when the search has one')
	varier.add_argument('root')
	varier.add_argument('--id', required=True)
	varier.add_argument('name', help='one word: the copy is probe/<name>/')
	varier.add_argument('--parent', dest='from_parent', action='store_true',
		help='export the '
		'parent\'s code, not your edit')
	varier.add_argument('--no-build', action='store_true', help='export '
		'only')
	varier.set_defaults(func=variant)

	widener = subparsers.add_parser('width', help='step 2: N runs at once on '
		'A0, and the largest N that does not slow the objective')
	widener.add_argument('root')
	widener.add_argument('--counts', help='comma-separated N (default: 1, '
		'powers of two, the plan and twice the plan)')
	widener.add_argument('--reps', type=int, default=2)
	widener.add_argument('--write', action='store_true', help='write the '
		'result on spec.md\'s Width measured line')
	widener.add_argument('--background', action='store_true', help='start '
		'it detached, writing setup/width.log, and return')
	widener.set_defaults(func=width)

	specer = subparsers.add_parser('spec', help='replace one answer in '
		'spec.md')
	specer.add_argument('root')
	specer.add_argument('--set', nargs='+', metavar='FIELD VALUE',
		required=True, help='the `- FIELD:` line and its new answer: FIELD '
		'VALUE, or FIELD=VALUE as `config --set` takes it')
	specer.set_defaults(func=spec_set)

	finisher_parser = subparsers.add_parser('finish', help='an iteration\'s '
		'last step: README.md from notes.md, a checker registered, ledger '
		'entries appended, and the block to report')
	finisher_parser.add_argument('root')
	finisher_parser.add_argument('--id', required=True)
	finisher_parser.add_argument('--ledger', help='a file of ledger entries, '
		'one per line, as `ledger --add` takes')
	finisher_parser.add_argument('--checker', help='the checker\'s file or '
		'directory, under $DIR/probe/')
	finisher_parser.add_argument('--checker-row', help='its row: `key: value` '
		'lines for name, files, compares, built, run, faults and inputs; '
		'faults may be left to the table `prove --id` wrote')
	finisher_parser.set_defaults(func=finish)

	readme_parser = subparsers.add_parser('readme', help='assemble an '
		'iteration\'s README.md from its notes.md and result.json')
	readme_parser.add_argument('root')
	readme_parser.add_argument('--id', required=True)
	readme_parser.add_argument('--template', action='store_true',
		help='write the notes.md skeleton instead, when there is none')
	readme_parser.set_defaults(func=readme)

	four = subparsers.add_parser('summary', help='write rounds/round-N.md')
	four.add_argument('root')
	four.add_argument('--round', type=int, required=True)
	four.set_defaults(func=summary)

	five = subparsers.add_parser('promote', help='make an iteration the root '
		'(tree mode)')
	five.add_argument('root')
	five.add_argument('--id', required=True)
	five.set_defaults(func=promote)

	val = subparsers.add_parser('validate', help='run the external '
		'validation once against the root; recorded, never gated on')
	val.add_argument('root')
	val.add_argument('--round', type=int, help='default: the current round')
	val.add_argument('--id', help='which iteration to validate '
		'(default: the current root)')
	val.add_argument('--approve', action='store_true', help='record the '
		"user's sign-off in config.json, and run nothing. Only after they "
		'have said yes')
	val.add_argument('--dry-run', action='store_true',
		help='print the command that would run, and stop')
	val.add_argument('--out', help='with --id: write the output and '
		'record.json into this directory and record no round; how a '
		'subagent validates its own iteration')
	val.add_argument('--background', action='store_true', help='start it '
		'detached, writing autooptimize/validations/validate.log, and return')
	val.set_defaults(func=validate)

	seven = subparsers.add_parser('prune', help='delete the artifacts of '
		'iterations nobody will rerun')
	seven.add_argument('root')
	seven.add_argument('--keep-recent', type=int, default=20,
		help='also protect the N most recent iterations '
			'(default: %(default)s)')
	seven.add_argument('--worktrees', action='store_true',
		help='also remove the pruned iterations\' worktree checkouts; their '
			'branches, and so their code, stay')
	seven.add_argument('--all', action='store_true',
		help='prune the protected iterations too, keeping only metrics.json '
			'and log.txt anywhere -- for a search that is finished and whose '
			'winner has been taken upstream')
	seven.add_argument('--outputs-ext', default=','.join(OUTPUT_EXTENSIONS),
		help='extensions of saved output arrays that the kept code '
			'regenerates exactly (default: %(default)s)')
	seven.add_argument('--keep-outputs', action='store_true',
		help='do not delete saved output arrays')
	seven.add_argument('--apply', action='store_true',
		help='actually delete; without it this only reports')
	seven.set_defaults(func=prune)

	eight = subparsers.add_parser('restore', help='put a pruned iteration\'s '
		'code checkout back')
	eight.add_argument('root')
	eight.add_argument('--id', required=True)
	eight.add_argument('--branch', help='default: the branch in meta.json')
	eight.set_defaults(func=restore)

	nine = subparsers.add_parser('reset', help='clear the search back to its '
		'baseline, keeping the baseline measured')
	nine.add_argument('root')
	nine.add_argument('--keep', default='0',
		help='iteration ids to keep, comma separated (default: %(default)s, '
			'the baseline)')
	nine.add_argument('--new-root',
		help='which kept iteration becomes the root (default: the lowest '
			'id in --keep). Point it at a promoted best to start the ideas '
			'over from there rather than from the baseline')
	nine.add_argument('--force', action='store_true',
		help='remove iterations that have no result.json, which are either '
			'running now or died mid-iteration')
	nine.add_argument('--apply', action='store_true',
		help='actually remove; without it this only reports')
	nine.set_defaults(func=reset)

	finisher = subparsers.add_parser('final', help='the winner against A0 '
		'by paired timing, and at the second thread count: the one paired '
		'comparison a search runs')
	finisher.add_argument('root')
	finisher.add_argument('--json', action='store_true', help='print final.json')
	finisher.add_argument('--id', default='best', help='the winner (default: '
		'best)')
	finisher.add_argument('--background', action='store_true', help='start '
		'it detached, writing autooptimize/final.log, and return')
	finisher.set_defaults(func=final)

	ender = subparsers.add_parser('end', help='step 4: end the search on '
		'the record: the paired timing, the last summary, ended.json and '
		'ended.md, the clean-up, the dashboard')
	ender.add_argument('root')
	ender.add_argument('--reason', help='which When-to-stop criterion ({}), '
		'or the user\'s words'.format(', '.join(REASONS)))
	ender.add_argument('--id', default=None, help='the winner (default: '
		'best)')
	ender.add_argument('--background', action='store_true', help='start it '
		'detached, writing autooptimize/end.log, and return (the paired '
		'timing can take minutes)')
	ender.add_argument('--reopen', action='store_true', help='undo the '
		'ending: archive ended.json as ended-<n>.json, restart the '
		'publisher; refused once the winner is published '
		'(reopen-lock.json). The clean-up stays')
	ender.set_defaults(func=end)

	path_parser = subparsers.add_parser('path', help='the path to an '
		'iteration: every ancestor from the baseline, with its numbers')
	path_parser.add_argument('root')
	path_parser.add_argument('--id', default='best', help='an iteration id, '
		'or best (default): the root, or an iteration descending '
		'from one that passed the keep rule')
	path_parser.add_argument('--markdown', action='store_true')
	path_parser.add_argument('--json', action='store_true')
	path_parser.set_defaults(func=path)

	tabler = subparsers.add_parser('table', help='read-only: one row per '
		'iteration, in flight included, filtered, sorted and cut; text, TSV '
		'or JSON')
	tabler.add_argument('root')
	tabler.add_argument('--fields', help='comma-separated columns, or all '
		'(default: {}). Also: {}, and each gate and observer by name'.format(
		','.join(TABLE_DEFAULT), ', '.join(field for field in TABLE_FIELDS
		if field not in TABLE_DEFAULT)))
	tabler.add_argument('--status', help='keep these statuses: ok, '
		'gate-failed, error, partial, running, unreadable (comma-separated)')
	tabler.add_argument('--category', help='keep these categories, by name '
		'or short name (CACHING or cache; comma-separated)')
	tabler.add_argument('--round', type=int, help='keep this round')
	tabler.add_argument('--parent', help='keep the children of this '
		'iteration (A5)')
	tabler.add_argument('--frontier', action='store_true', help='keep the '
		'iterations that improved on everything before them')
	tabler.add_argument('--promoted', action='store_true', help='keep the '
		'iterations promoted to root')
	tabler.add_argument('--sort', help='a column, descending with a leading '
		'-; best sorts by median, best first. Missing values go last')
	tabler.add_argument('--limit', type=int, help='the first N rows, after '
		'sorting')
	shape = tabler.add_mutually_exclusive_group()
	shape.add_argument('--tsv', action='store_true', help='tab-separated, '
		'raw values, a header line')
	shape.add_argument('--json', action='store_true', help='a list of '
		'objects, raw values')
	tabler.set_defaults(func=table_command)

	shower = subparsers.add_parser('show', help='read-only: one iteration on '
		'one screen: its numbers, keep verdict, gates, code change, notes and '
		'the paths to read further')
	shower.add_argument('root')
	shower.add_argument('id', nargs='?', default='best', help='an iteration '
		'(A7 or 7), or best (default)')
	shower.add_argument('--chars', type=int, default=600, help='cut each '
		'notes section at this many characters; 0 prints it whole')
	shower.add_argument('--json', action='store_true')
	shower.set_defaults(func=show)

	branching = subparsers.add_parser('tests-branch', help='step 1: check '
		'the original, clone it into <work>/tests/suite and branch; --squash '
		'collapses the branch to one commit')
	branching.add_argument('original', nargs='?', help='the original '
		'repository')
	branching.add_argument('--work', required=True, help='the search\'s '
		'directory')
	branching.add_argument('--date', help='YYYY-MM-DD in the branch name '
		'(default: today)')
	branching.add_argument('--squash', action='store_true')
	branching.add_argument('--message', '-m', help='the squashed commit\'s '
		'message')
	branching.set_defaults(func=tests_branch)

	archiver = subparsers.add_parser('archive', help='step 5: the winner\'s '
		'patch, code, README and result copied out, and the final report\'s '
		'skeleton')
	archiver.add_argument('root')
	archiver.add_argument('--id', default='best')
	archiver.add_argument('--dest', help='default: <work>/finish/archive')
	archiver.add_argument('--force', action='store_true')
	archiver.set_defaults(func=archive)

	config_parser = subparsers.add_parser('config', help='print config.json, '
		'or change a setting mid-search and record why in spec.md')
	config_parser.add_argument('root')
	config_parser.add_argument('--set', action='append', metavar='KEY=VALUE',
		help='a setting such as mode=tree or gates.<name>.threshold=1e-4; '
			'repeatable')
	config_parser.add_argument('--add-observer', action='append',
		metavar='NAME', help='record one more observer from now on; '
			'repeatable')
	config_parser.add_argument('--reason', help='the user\'s words, or why a '
		'default was taken while they were away; required with a change')
	config_parser.set_defaults(func=config_change)

	for add in EXTRA_PARSERS:
		add(subparsers)

	return parser


def usage_lines(names):
	"""The usage line of each named subcommand, as `--help` prints it."""

	choices = next(action.choices for action in build_parser()._actions
		if isinstance(action, argparse._SubParsersAction))

	# `usage: <prog> <name> …`, with the program as this file is named.
	return ['autoopt.py ' + ' '.join(choices[name].format_usage().split()[2:])
		for name in names]


def main(argv=None):
	parser = build_parser()
	args = parser.parse_args(argv)

	# Every id flag takes `107` or `A107`. `new --id` names an iteration that
	# does not exist yet, so it is parsed without a lookup.
	for flag in ('id', 'parent', 'new_root'):
		value = getattr(args, flag, None)

		if value is None or value == 'best':
			continue

		if flag == 'id' and args.subcommand in ('new', 'brief'):
			try:
				args.id, letter = parse_label(value)
			except ValueError:
				fail('--id takes an iteration such as 107 or A107; got '
					'{!r}'.format(value))
			if letter not in (None, LABEL_LETTER):
				fail('--id {} names a letter other than {}: labels are {}{}'
					.format(value, LABEL_LETTER, LABEL_LETTER, args.id))
			continue

		setattr(args, flag, to_id(resolve(args.root), value,
			'--' + flag.replace('_', '-')))

	return args.func(args)


if __name__ == '__main__':
	sys.exit(main())
