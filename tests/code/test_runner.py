#!/usr/bin/env python3
"""Check the decisions the runner makes, on a benchmark that runs instantly.

	python3 test_runner.py

`dashboard/test_frontend.py` covers the page and `dashboard/test_notebook.py`
the notebook mount; this covers the arithmetic underneath both — which seeds run, what counts as
passing, when the confirmation stage is earned. Every case builds a real
search in a temporary directory and drives the real scripts, because the
failures worth catching are the ones where two of them disagree.

No GPU, no network; the whole suite takes a few minutes.
"""

import os
import sys
import json
import re
import glob
import time
import shlex
import shutil
import statistics
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt

# Every search below is built without an interview; the spec check has its
# own case, which clears this.
os.environ['AUTOOPT_SKIP_SPEC_CHECK'] = '1'
# The machine these run on is shared, so the baseline's busy-machine check
# would refuse at random; quiet_check turns it back on.
os.environ['AUTOOPT_SKIP_QUIET_CHECK'] = '1'

AUTOOPT = os.path.join(SCRIPTS, 'autoopt.py')
RUNNER = os.path.join(SCRIPTS, 'run_iteration.py')


# A benchmark with a knob the tests turn: QUALITY sets the objective, GATE
# the gated value, and FAIL_ON makes one seed crash.
BENCH = '''\
import os, json, math, random, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()

QUALITY = {quality}
GATE = {gate}
FAIL_ON = {fail_on}
NOISE = {noise}
SLEEP = {sleep}

if SLEEP:
	import time
	time.sleep(SLEEP)

if FAIL_ON is not None and args.seed == FAIL_ON:
	raise RuntimeError('deliberate failure on seed {{}}'.format(args.seed))

random.seed(args.seed)
value = QUALITY + random.gauss(0, NOISE)

os.makedirs(args.out, exist_ok=True)

with open(os.path.join(args.out, 'metrics.json'), 'w') as handle:
	json.dump({{'objective': value, 'gates': {{'cost': GATE}},
		'observers': {{'size': 100}}}}, handle)
'''


class Suite:
	def __init__(self):
		self.passed, self.failed = [], []

	def check(self, name, ok, detail=''):
		if ok:
			self.passed.append(name)
			print('  ok    {}'.format(name))
		else:
			self.failed.append('{}: {}'.format(name, detail))
			print('  FAIL  {} -- {}'.format(name, detail))

	def equal(self, name, got, want):
		self.check(name, got == want, 'got {!r}, want {!r}'.format(got, want))


def prompted(out):
	"""A spawn prompt with the prompt files it has the subagent read
	(`briefs/...`) appended: what the subagent sees, for the checks on a
	brief's content."""

	for path in re.findall(r'Read `([^`]*/briefs/[^`]*)`', out):
		with open(path) as handle:
			out += '\n' + handle.read()

	return out


def call(args, expect=(0, 2)):
	out = subprocess.run([sys.executable] + args, capture_output=True,
		text=True)

	if out.returncode not in expect:
		raise RuntimeError('{}\n{}\n{}'.format(' '.join(args), out.stdout,
			out.stderr))

	# A refusal writes its reason to stderr, so a test that asserts on the
	# message needs both streams or it silently checks an empty string.
	return prompted(out.stdout + out.stderr)


def fake_end(search, winner):
	"""The marker `end` leaves, for the cases that test what after_end does
	and not `end` itself (`test_end.py` covers `end`): the winner and the
	words `status` prints."""

	with open(os.path.join(search, 'ended.json'), 'w') as out:
		json.dump({'ended_at': '2026-01-01T00:00:00', 'reason': {'criterion':
			'user', 'text': 'a test'}, 'winner': {'id': winner, 'label':
			'A{}'.format(winner)}, 'winner_changes': []}, out)


SPEED_UP = 'Speed up pkg\n\nScore with less work.\n'
LOAD_FASTER = 'Load faster\n\nRead the input once.\n'


def bench(directory, quality, gate=1.0, fail_on=None, noise=0.01, sleep=0):
	with open(os.path.join(directory, 'bench.py'), 'w') as out:
		out.write(BENCH.format(quality=quality, gate=gate, fail_on=fail_on,
			noise=noise, sleep=sleep))


def make_search(root, **kwargs):
	"""A search whose baseline scores 0.50, gated at cost <= 1.0."""

	os.makedirs(root, exist_ok=True)
	# Searches promote by hand and screen, then confirm, unless a test says
	# otherwise; auto_promotion and single_count cover init's defaults.
	kwargs.setdefault('promote', 'orchestrator')
	staged = kwargs.pop('staged', True)
	if staged:
		kwargs.setdefault('runs', 3)
		kwargs.setdefault('confirm_runs', 10)

	args = [AUTOOPT, 'init', root, '--task', 'test',
		'--command', 'python3 bench.py --seed {seed} --out {out}',
		'--objective', 'score', '--direction', 'max',
		'--gates', json.dumps([{'name': 'cost', 'op': '<=',
			'threshold': 1.0}]),
		'--observers', 'size', '--no-gpu']

	for key, value in kwargs.items():
		args += ['--' + key.replace('_', '-'), str(value)]

	if staged:
		args.append('--staged')

	call(args)

	search = os.path.join(root, 'autooptimize')
	bench(autoopt.iteration_dir(search, 0), 0.50)
	call([RUNNER, autoopt.iteration_dir(search, 0)])

	return search


def add(search, identifier, quality, gate=1.0, fail_on=None, runs=None,
	category='MISC', stage=None):
	call([AUTOOPT, 'new', search, '--id', str(identifier), '--title',
		'iteration {}'.format(identifier), '--category', category])

	directory = autoopt.iteration_dir(search, identifier)
	bench(directory, quality, gate, fail_on)

	call([RUNNER, directory] + (['--runs', str(runs)] if runs else [])
		+ (['--stage', stage] if stage else []))

	return autoopt.load_result(search, identifier)


def seeds_of(result):
	return [run['seed'] for run in result['runs']]


# Repository mode: the benchmark is a harness outside the repository that
# imports the package from whichever worktree it is pointed at.
HARNESS = """\
import os, sys, json, random, argparse

parser = argparse.ArgumentParser()
parser.add_argument('--src', required=True)
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()

# The tests rewrite pkg/core.py within the same second at the same length,
# which a cached .pyc cannot tell apart.
sys.dont_write_bytecode = True
sys.path.insert(0, args.src)
import pkg.core

# The guard the docs require: the package came from this worktree.
assert os.path.realpath(pkg.core.__file__).startswith(
	os.path.realpath(args.src)), pkg.core.__file__

random.seed(args.seed)
os.makedirs(args.out, exist_ok=True)

with open(os.path.join(args.out, 'metrics.json'), 'w') as handle:
	json.dump({'objective': pkg.core.QUALITY + random.gauss(0, 0.01),
		'gates': {'cost': 0.5}, 'observers': {'size': 100}}, handle)
"""

# The package's own test suite: it fails once QUALITY reaches 0.9, which is
# how a test can break an iteration that every run scored well.
UNIT_TEST = """\
import sys
sys.dont_write_bytecode = True
sys.path.insert(0, '.')
import pkg.core
sys.exit(0 if pkg.core.QUALITY < 0.9 else 1)
"""


PYPROJECT = """\
[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "pkg"
version = "0.1"

[tool.setuptools]
packages = ["pkg"]
"""

def git(*args):
	return subprocess.run(('git',) + args, check=True, capture_output=True,
		text=True).stdout


def set_quality(checkout, quality):
	with open(os.path.join(checkout, 'pkg', 'core.py'), 'w') as out:
		out.write('QUALITY = {}\n'.format(quality))


def make_original(directory):
	"""A small git repository standing in for the one being optimized."""

	os.makedirs(os.path.join(directory, 'pkg'))
	os.makedirs(os.path.join(directory, 'tests'))

	with open(os.path.join(directory, 'README.md'), 'w') as out:
		out.write('# pkg\n')
	with open(os.path.join(directory, 'pkg', '__init__.py'), 'w') as out:
		out.write('')
	with open(os.path.join(directory, 'tests', 'check.py'), 'w') as out:
		out.write(UNIT_TEST)

	set_quality(directory, 0.50)

	with open(os.path.join(directory, 'pyproject.toml'), 'w') as out:
		out.write(PYPROJECT)
	with open(os.path.join(directory, 'tests', 'test_core.py'), 'w') as out:
		out.write('import pkg.core\n\ndef test_quality():\n'
			'\tassert pkg.core.QUALITY < 0.9\n')

	git('-C', directory, 'init', '-q', '-b', 'main')
	git('-C', directory, 'add', '-A')
	git('-C', directory, '-c', 'user.name=t', '-c', 'user.email=t@t',
		'commit', '-q', '-m', 'initial')
	git('-C', directory, 'remote', 'add', 'origin',
		'https://github.com/example/pkg.git')


def repo_add(search, identifier, parent, quality):
	directory = call([AUTOOPT, 'new', search, '--id', str(identifier),
		'--parent', str(parent), '--title', 'quality {}'.format(quality),
		'--category', 'MISC']).strip().splitlines()[-1]
	set_quality(os.path.join(directory, 'src'), quality)
	call([RUNNER, directory])

	return autoopt.load_result(search, identifier)


def repo_mode(suite, base):
	original = os.path.join(base, 'original')
	make_original(original)
	before = git('-C', original, 'rev-parse', 'HEAD')

	harness = os.path.join(base, 'bench.py')
	with open(harness, 'w') as out:
		out.write(HARNESS)

	root = os.path.join(base, 'repo-search')
	search = os.path.join(root, 'autooptimize')

	def init(extra=()):
		return call([AUTOOPT, 'init', root, '--task', 'test', '--repo',
			original, '--copy', harness, '--command',
			'python3 {}/harness/bench.py --src {{src}} --seed {{seed}} '
			'--out {{out}}'.format(search),
			'--objective', 'score', '--direction', 'max',
			'--gates', json.dumps([{'name': 'cost', 'op': '<=',
				'threshold': 1.0}]),
			'--observers', 'size', '--no-gpu', '--runs', '3',
			'--staged', '--confirm-runs', '6', '--promote', 'orchestrator',
			'--tests', 'python3 tests/check.py'] + list(extra),
			expect=(0, 1))

	with open(os.path.join(original, 'README.md'), 'a') as out:
		out.write('uncommitted\n')

	refused = init()
	suite.check('a dirty original is refused, naming the file',
		'README.md' in refused and not os.path.exists(search), refused[-300:])

	git('-C', original, 'checkout', '--', 'README.md')
	out = init()
	config = autoopt.load_config(search)

	suite.equal('the copy records the checked-out branch',
		config['repo']['branch'], 'main')
	suite.equal('the original is untouched',
		(git('-C', original, 'rev-parse', 'HEAD'),
			git('-C', original, 'status', '--porcelain')), (before, ''))
	suite.equal('the copy has no remote pointing at the original',
		git('-C', os.path.join(search, 'repo.git'), 'remote').strip(), '')
	suite.check('the harness is outside the worktree',
		os.path.exists(os.path.join(search, 'harness', 'bench.py'))
		and not os.path.exists(os.path.join(search, 'iterations', '0',
			'src', 'bench.py')), out)

	call([RUNNER, autoopt.iteration_dir(search, 0)])
	baseline = autoopt.load_result(search, 0)
	suite.equal('the repository baseline passes, tests included',
		(baseline['status'], baseline['tests']['pass']), ('ok', True))
	with open(os.path.join(search, 'harness', 'helpers.py'), 'w') as out:
		out.write('def digest(value):\n\treturn repr(value)\n')
	call([AUTOOPT, 'brief', search, '--id', '9', '--title', 't', '--category',
		'MISC', '--idea', 'x', '--tried', 'none'])
	text = open(os.path.join(search, 'briefs', '9.md')).read()
	suite.check('a repository-mode brief lists the harness and its helpers',
		'- The harness (`{}`), off limits; a probe may import its helpers:'
		.format(os.path.join(search, 'harness')) in text and '- `helpers.py` '
		'(2 lines): digest 1' in text, text[-1500:])
	os.remove(os.path.join(search, 'briefs', '9.md'))
	os.remove(os.path.join(search, 'briefs', '9.json'))
	os.remove(os.path.join(search, 'harness', 'helpers.py'))

	with open(os.path.join(autoopt.iteration_dir(search, 0), 'README.md'),
		'w') as out:
		out.write('# Iteration 0: baseline\n')
	with open(os.path.join(autoopt.checkout_dir(search, 0), 'README.md')) \
		as infile:
		suite.equal('the iteration README does not clobber the repo README',
			infile.read(), '# pkg\n')

	first = repo_add(search, 1, 0, 0.60)
	names = git('-C', os.path.join(search, 'repo.git'), 'diff',
		'--name-only', 'autoopt/0..autoopt/1').split()
	suite.equal('the branch carries the package change and nothing else',
		names, ['pkg/core.py'])
	suite.equal('the improvement is ok', first['status'], 'ok')

	broken = repo_add(search, 2, 0, 0.95)
	suite.equal('failing unit tests fail the iteration',
		(broken['status'], broken['tests']['pass']), ('gate-failed', False))
	suite.equal('and it is not confirmed', broken['stage'], 'screen')
	suite.check('and it is off the frontier',
		autoopt.frontier(autoopt.all_results(search), 'max')[1] != 2)

	call([AUTOOPT, 'promote', search, '--id', '1'])

	# A file the plain-mode exclude list would hide is committed here.
	directory = call([AUTOOPT, 'new', search, '--id', '3', '--parent', '1',
		'--title', 'quality 0.7', '--category',
		'MISC']).strip().splitlines()[-1]
	set_quality(os.path.join(directory, 'src'), 0.70)
	with open(os.path.join(directory, 'src', 'pkg', 'README.md'), 'w') as out:
		out.write('subpackage notes\n')
	call([RUNNER, directory])
	names = git('-C', os.path.join(search, 'repo.git'), 'diff',
		'--name-only', 'autoopt/1..autoopt/3').split()
	suite.equal('a new README.md inside the package is committed',
		sorted(names), ['pkg/README.md', 'pkg/core.py'])

	out = call([AUTOOPT, 'path', search, '--json'])
	suite.equal('the path to the best runs from the baseline',
		json.loads(out)['path'], [0, 1, 3])

	table = call([AUTOOPT, 'path', search, '--markdown'])
	suite.check('the path table has one row per iteration on the path',
		all('| A{} |'.format(i) in table for i in (0, 1, 3))
		and '| A2 |' not in table and 'unit tests' in table, table)


NUMBA_HARNESS = """import os, sys, json, argparse
p = argparse.ArgumentParser()
for flag in ('--src', '--seed', '--out'):
	p.add_argument(flag)
a = p.parse_args()
sys.path.insert(0, a.src)
from pkg.driver import g
os.makedirs(a.out, exist_ok=True)
json.dump({'objective': float(g(1.0)), 'gates': {'cost': 0.0},
	'observers': {'size': 1.0}}, open(os.path.join(a.out, 'metrics.json'), 'w'))
"""


def task_checks(suite, base):
	"""The task's checks in the runner: A0's cached compare outputs, the
	stages that stop at the first failure, the confirmed iteration's
	measurements, promote, status and the brief."""

	search = make_search(os.path.join(base, 'checks'), tests='test ! -e broken.txt')
	folder = os.path.join(search, 'checks')
	os.makedirs(folder, exist_ok=True)
	cmp = os.path.join(folder, 'compare.py')
	with open(cmp, 'w') as handle:
		handle.write('import os, sys\n'
			'src, ref = sys.argv[1:3]\n'
			'def read(d, name):\n'
			'\tp = os.path.join(d, name)\n'
			'\treturn open(p).read() if os.path.exists(p) else "same"\n'
			'names = ("output.txt", "api.txt")\n'
			'if os.environ.get("AUTOOPT_WRITE_REFERENCE") == "1":\n'
			'\tfor name in names:\n'
			'\t\topen(os.path.join(ref, name), "w").write(read(src, name))\n'
			'\tprint("cached 2 inputs"); sys.exit(0)\n'
			'if not os.path.exists(os.path.join(ref, names[0])):\n'
			'\tprint("no reference"); sys.exit(2)\n'
			'differ = [n for n in names if read(src, n) != read(ref, n)]\n'
			'print("{} of 2 inputs differ".format(len(differ)) if differ '
			'else "2 inputs identical")\n'
			'sys.exit(1 if differ else 0)\n')
	with open(os.path.join(folder, 'time_one.py'), 'w') as handle:
		handle.write('import sys, os\n'
			'p = os.path.join(sys.argv[1], "speed.txt")\n'
			'text = open(p).read().strip() if os.path.exists(p) else "2.0"\n'
			'sys.exit(3) if text == "crash" else print(text)\n')
	with open(os.path.join(folder, 'first_call.py'), 'w') as handle:
		handle.write('import os, sys\n'
			'sys.exit(4) if os.path.exists(os.path.join(sys.argv[1], '
			'"nocold.txt")) else print(0.01)\n')

	def iteration(identifier, quality, output='same', api='same', speed='1.0',
			broken=False, expect=(0, 2)):
		call([AUTOOPT, 'new', search, '--id', str(identifier), '--title',
			'iteration {}'.format(identifier), '--category', 'MICRO'])
		directory = autoopt.iteration_dir(search, identifier)
		bench(directory, quality)
		for name, text in (('output.txt', output), ('api.txt', api),
				('speed.txt', speed)) + ((('broken.txt', 'x'),) if broken
					else ()):
			with open(os.path.join(directory, name), 'w') as handle:
				handle.write(text)
		out = call([RUNNER, directory], expect=expect)
		return autoopt.load_result(search, identifier), out

	# An iteration run while no check is configured has none to show.
	before, _ = iteration(1, 0.60)
	out = call([AUTOOPT, 'config', search, '--set',
		'checks.compare=python3 {} {{src}} {{reference}} {{nope}}'.format(cmp),
		'--reason', 'test'], expect=(1,))
	suite.check('a check with an unknown placeholder is refused',
		'{nope}' in out, out[-300:])
	out = call([AUTOOPT, 'config', search, '--set',
		'checks.compare=python3 {} {{src}}'.format(cmp), '--reason', 'test'],
		expect=(1,))
	suite.check('a compare without {reference} is refused',
		'must contain {reference}' in out, out[-300:])
	call([AUTOOPT, 'config', search, '--set',
		'checks.compare=python3 {} {{src}} {{reference}}'.format(cmp),
		'--set', 'checks.time=python3 {} {{src}}'.format(os.path.join(folder,
			'time_one.py')), '--set', 'checks.first_call=python3 {} {{src}}'.format(
			os.path.join(folder, 'first_call.py')), '--set', 'checks.threads=2',
		'--reason', 'test'])
	suite.equal('config --set creates the checks block',
		sorted(autoopt.load_config(search)['checks']),
		['compare', 'first_call', 'threads', 'time'])

	out = call([AUTOOPT, 'promote', search, '--id', '1'], expect=(1,))
	suite.check('promote refuses an iteration with no passing check',
		'no passing check' in out, out[-300:])
	out = call([AUTOOPT, 'check', search, '--id', '1'])
	suite.check('check --id runs it by hand', 'compare pass' in out, out)
	reference = os.path.join(search, 'reference', 'checks')
	manifest = autoopt.read_json(os.path.join(reference, 'manifest.json'))
	suite.equal('the compare outputs are cached from A0\'s commit',
		(manifest['commit'], sorted(os.listdir(os.path.join(reference,
			'outputs')))), (autoopt.load_result(search, 0)['commit'],
			['api.txt', 'output.txt']))
	call([AUTOOPT, 'promote', search, '--id', '1'])

	same, out = iteration(2, 0.61)
	suite.equal('the runner compares a clean iteration against A0\'s outputs',
		(same['status'], same['check']['compare']['verdict']), ('ok', 'pass'))
	suite.check('and its report block carries the check line',
		'check: compare pass (2 inputs identical)' in out, out[-500:])
	suite.check('the outputs are cached once, not per iteration',
		'cached A0' not in out and not [name for name in os.listdir(reference)
			if name.startswith('outputs.old')], os.listdir(reference))

	# A re-run after an edit commits the edit, never the checks' records.
	bench(autoopt.iteration_dir(search, 2), 0.62)
	call([RUNNER, autoopt.iteration_dir(search, 2), '--rerun'])
	tree = git('-C', os.path.join(search, 'repo.git'), 'ls-tree', '-r',
		'--name-only', 'autoopt/2').split()
	suite.check('a plain-mode re-run commits none of the checks\' records',
		not [name for name in tree if name.startswith('check')
			or name == 'cold.json'] and 'bench.py' in tree, tree)

	differs, out = iteration(3, 0.70, output='different')
	suite.equal('outputs that differ fail the iteration',
		(differs['status'], differs.get('failed_check')), ('gate-failed', 'compare'))
	suite.check('and stop it after its first run', differs['n_runs'] == 1
		and 'not confirmed: the compare found outputs that differ' in out,
		(differs['n_runs'], out[-600:]))

	broken, _ = iteration(4, 0.70, output='different', broken=True)
	suite.check('failing unit tests stop the iteration before the compare',
		broken['status'] == 'gate-failed'
		and 'compare' not in (broken.get('check') or {})
		and broken['n_runs'] == 1, (broken['status'], broken.get('check')))

	near, out = iteration(5, 0.60, speed='0.5')
	suite.check('a confirmed iteration inside the noise band gets no paired '
		'timing: the median decides its keep verdict', sorted(near['check'])
		== ['cold', 'compare'] and near['stage'] == 'confirmed'
		and near['keep']['verdict'] == 'fail' and 'paired' not in out,
		(near.get('check'), near.get('keep')))
	cold = near['check'].get('cold') or {}
	suite.check('the cold first call is timed before any run, beside the '
		'parent\'s recorded value', cold.get('verdict') == 'measured'
		and cold.get('parent_seconds') is not None and out.index(
			'cold first call') < out.index('seed 0:'), (cold, out[:600]))
	suite.check('both on the check line', 'check: cold first call 0.01 s '
		'(parent 0.01 s); compare pass' in out, out[-500:])

	# The one paired comparison: the winner against A0.
	fast, _ = iteration(6, 0.95, speed='0.5')
	out = call([AUTOOPT, 'final', search])
	record = autoopt.read_json(os.path.join(search, 'final.json'))
	suite.check('final pairs the winner with A0, and at the second thread '
		'count', record['winner'] == 6 and record['paired']['verdict']
		== 'B better' and record['threads']['verdict'] == 'B better'
		and 'A6 vs A0, paired: B better' in out, out[-400:])
	as_json = json.loads(call([AUTOOPT, 'final', search, '--json']))
	suite.check('final --json prints final.json and nothing else',
		as_json['winner'] == 6 and as_json['paired']['verdict'] ==
		'B better', as_json)

	call([AUTOOPT, 'new', search, '--id', '11', '--title', 'no cold',
		'--category', 'MICRO'])
	with open(os.path.join(autoopt.iteration_dir(search, 11), 'nocold.txt'),
			'w') as handle:
		handle.write('x')
	bench(autoopt.iteration_dir(search, 11), 0.70)
	call([RUNNER, autoopt.iteration_dir(search, 11)])
	nocold = autoopt.load_result(search, 11)
	suite.equal('a cold first call that crashes stops the iteration before '
		'any run', (nocold['status'], nocold.get('failed_check'),
			nocold['n_runs']), ('error', 'cold', 0))

	table = call([AUTOOPT, 'status', search])
	suite.check('status shows the check column', 'check' in next(l for l in
		table.splitlines() if l.startswith('id ')) and ' pass ' in table, table[-900:])

	# A changed check is cached again from A0 before the next compare.
	with open(cmp, 'a') as handle:
		handle.write('# changed\n')
	again, out = iteration(7, 0.61)
	manifest = autoopt.read_json(os.path.join(reference, 'manifest.json'))
	suite.check('a changed checks file caches A0\'s outputs again first',
		'cached A0\'s compare outputs' in out and manifest.get('replaced')
		and again['check']['compare']['verdict'] == 'pass'
		and [name for name in os.listdir(reference) if name.startswith(
			'outputs.old')], out[-800:])

	# A lost cache is rebuilt from A0, never from the iteration's own code.
	os.rename(os.path.join(reference, 'outputs'), os.path.join(reference,
		'outputs.lost'))
	lost, _ = iteration(8, 0.61, output='different')
	suite.equal('a lost cache is rebuilt from A0, so differing outputs still fail',
		(lost['status'], lost.get('failed_check')), ('gate-failed', 'compare'))

	silent = os.path.join(folder, 'silent.py')
	with open(silent, 'w') as handle:
		handle.write('print("2 inputs identical")\n')
	call([AUTOOPT, 'config', search, '--set',
		'checks.compare=python3 {} {{src}} {{reference}}'.format(silent),
		'--reason', 'test'])
	_, out = iteration(9, 0.61, expect=(1,))
	suite.check('a compare that caches nothing is refused before any run',
		'wrote nothing' in out and not os.path.exists(os.path.join(
			autoopt.iteration_dir(search, 9), 'runs')), out[-400:])
	call([AUTOOPT, 'config', search, '--set',
		'checks.compare=python3 {} {{src}} {{reference}}'.format(cmp),
		'--reason', 'test'])

	checked = os.path.join(autoopt.iteration_dir(search, 3), 'check')
	call([AUTOOPT, 'prune', search, '--keep-recent', '0', '--apply'])
	left = sorted(os.path.relpath(os.path.join(place, name), checked)
		for place, _, names in os.walk(checked) for name in names)
	suite.check('prune deletes the trees a check exported and keeps its logs',
		left == ['compare.log'] and os.path.exists(os.path.join(
			autoopt.iteration_dir(search, 3), 'cold.json')), left)

	brief = subprocess.run([sys.executable, AUTOOPT, 'brief', search, '--id',
		'10', '--title', 't', '--category', 'MICRO', '--idea', 'x', '--tried',
		'none'], capture_output=True, text=True)
	text = prompted(brief.stdout)
	suite.check('brief names the runner\'s checks in the prompt',
		'## Checks' in text and 'check:' in text
		and 'stopping at the first that fails' in text, text[-700:])


def auto_promotion(suite, base):
	"""The runner's keep verdict and promotion, and two winners racing for
	the root."""

	search = make_search(os.path.join(base, 'auto'), promote='auto')
	suite.equal('init --promote auto is recorded',
		autoopt.load_config(search).get('promote'), 'auto')

	first = add(search, 1, 0.70)
	suite.check('a confirmed iteration that beats its parent by more than '
		'the band is promoted by its own runner',
		autoopt.load_state(search)['root'] == 1 and first['keep']['promoted']
		and first['keep']['verdict'] == 'pass', first.get('keep'))
	report = open(os.path.join(autoopt.iteration_dir(search, 1),
		'report.txt')).read()
	suite.check('and its report says so', 'keep: promoted to the root' in
		report, report[-400:])

	again = call([RUNNER, autoopt.iteration_dir(search, 1)])
	suite.check('re-running the root closes no second round',
		autoopt.load_state(search)['round'] == 2
		and 'already on the root' in again, (autoopt.load_state(search)[
			'round'], again[-300:]))

	directory = autoopt.iteration_dir(search, 1)
	call([AUTOOPT, 'readme', search, '--id', '1', '--template'])
	notes = os.path.join(directory, 'notes.md')
	with open(notes) as handle:
		filled = re.sub(r'<<FILL:[^>]*>>', 'Written. More.', handle.read())
	with open(notes, 'w') as handle:
		handle.write(filled)
	out = call([AUTOOPT, 'readme', search, '--id', '1'])
	path = os.path.join(search, 'rounds', 'round-1.md')
	suite.check('the closed round\'s summary is written when its last README '
		'is', 'wrote the round summary' in out and os.path.exists(path)
		and '**A1**: promoted' in open(path).read(), out[-400:])

	worse = add(search, 2, 0.40)
	suite.check('one that does not beat its parent is kept out, with why',
		worse['keep']['verdict'] == 'fail' and 'promoted' not in worse['keep']
		and autoopt.load_state(search)['root'] == 1, worse.get('keep'))
	slower = autoopt.keep_verdict({'objective': {'direction': 'min'}},
		{'status': 'ok', 'stage': 'confirmed', 'objective': {'median': 1.2},
			'noise_band': 0.05}, {'objective': {'median': 1.0}})
	suite.check('a confirmed median slower than its parent\'s is called '
		'worse, not within the band', slower[0] is False
		and 'worse than its parent' in slower[1], slower)
	near = add(search, 3, 0.705)
	suite.check('so is one within the noise band with no paired verdict',
		near['stage'] == 'confirmed' and near['keep']['verdict'] == 'fail'
		and 'noise band' in near['keep']['why'], near.get('keep'))

	# Two winners from one root, measured at the same moment: whichever
	# reaches promote's lock first is the root, and the other is refused.
	for identifier, quality in ((4, 0.80), (5, 0.85)):
		call([AUTOOPT, 'new', search, '--id', str(identifier), '--title',
			'winner {}'.format(identifier), '--category', 'MICRO'])
		bench(autoopt.iteration_dir(search, identifier), quality)
	runs = [subprocess.Popen([sys.executable, RUNNER, autoopt.iteration_dir(
		search, identifier)], stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL) for identifier in (4, 5)]
	[run.wait() for run in runs]
	pair = {identifier: autoopt.load_result(search, identifier)['keep']
		for identifier in (4, 5)}
	head = autoopt.load_state(search)['root']
	loser = 5 if head == 4 else 4
	suite.check('two winners at once: one becomes the root, the other is '
		'refused', head in (4, 5) and pair[head]['promoted']
		and pair[loser]['promoted'] is False, pair)

	out = call([AUTOOPT, 'status', search, '--brief'])
	suite.check('status prints the machine\'s load', 'machine: load' in out,
		out[:600])

	head = autoopt.load_state(search)['root']

	call([AUTOOPT, 'config', search, '--set', 'promote=orchestrator',
		'--reason', 'test'])
	held = add(search, 10, 0.99)
	suite.check('with promote=orchestrator the runner records the verdict '
		'and promotes nothing', held['keep']['verdict'] == 'pass' and
		'promoted' not in held['keep'] and autoopt.load_state(search)['root']
		== head, held.get('keep'))
	out = call([AUTOOPT, 'config', search, '--set', 'promote=sometimes',
		'--reason', 'test'], expect=(1,))
	suite.check('an unknown promote mode is refused', 'promote is one of'
		in out, out[-200:])


def bookkeeping(suite, base):
	"""The tried idea struck by readme, and a ledger correction striking
	the entry it corrects."""

	search = make_search(os.path.join(base, 'books'))
	ideas = os.path.join(search, 'ideas.md')
	with open(ideas, 'a') as handle:
		handle.write('- [MICRO] pkg/a.py:f: hoist the invariant. Why: it is '
			'recomputed. Gain: unmeasured. Exact: bit-identical. (S1)\n'
			'- [CACHING] pkg/b.py:g: cache h. Why: called twice. Gain: '
			'unmeasured. Exact: bit-identical. (S1)\n')
	lines = open(ideas).read().split('\n')
	number = next(index for index, line in enumerate(lines, 1)
		if line.startswith('- [MICRO]'))

	out = call([AUTOOPT, 'brief', search, '--id', '1', '--title', 'hoist',
		'--category', 'MICRO', '--idea-line', str(number), '--tried', 'none'])
	key = autoopt.idea_key(lines[number - 1])
	record = json.load(open(os.path.join(search, 'briefs', '1.json')))
	suite.check('brief --idea-line takes the idea from ideas.md and hands its '
		'key to new', 'hoist the invariant' in out and record['idea_key']
		== key, out[-900:])
	out = call([AUTOOPT, 'brief', search, '--id', '2', '--title', 'x',
		'--category', 'MICRO', '--idea-line', '1', '--tried', 'none'],
		expect=(1,))
	suite.check('and refuses a line that is not an untried idea',
		'not an untried idea line' in out, out[-300:])
	with open(ideas, 'r+') as handle:
		text = handle.read()
		handle.seek(0)
		handle.write(text.replace('- [MICRO]', '- a decision added above\n'
			'- [MICRO]', 1))
	out = call([AUTOOPT, 'brief', search, '--id', '2', '--title', 'cache',
		'--category', 'CACHING', '--idea-line', 'cache h', '--tried', 'none'])
	suite.check('--idea-line also takes text only one untried line holds, '
		'which lines added above do not move', 'called twice' in out,
		out[-300:])
	out = call([AUTOOPT, 'brief', search, '--id', '3', '--title', 'x',
		'--category', 'MICRO', '--idea-line', 'pkg/', '--tried', 'none'],
		expect=(1,))
	suite.check('and refuses text two lines hold', 'matches 2 untried' in out,
		out[-300:])
	os.remove(os.path.join(search, 'briefs', '2.md'))
	os.remove(os.path.join(search, 'briefs', '2.json'))
	with open(ideas, 'a') as handle:
		handle.write('- [MICRO] pkg/c.py:k: unroll the loop. Why: short. '
			'Gain: unmeasured. Exact: bit-identical. (S1)\n')
	before = autoopt.idea_counts(search)
	out = call([AUTOOPT, 'drop', search, '--idea-line', 'unroll the loop',
		'--reason', 'already in the root'], expect=(0,))
	suite.check('drop strikes a stale line with why, and it is no longer '
		'untried', '~~ dropped: already in the root' in open(ideas).read()
		and autoopt.idea_counts(search)[0] == before[0] - 1, out[-300:])
	lines = open(ideas).read().split('\n')
	number = next(index for index, line in enumerate(lines, 1)
		if line.startswith('- [MICRO]'))
	key = autoopt.idea_key(lines[number - 1])

	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'hoist',
		'--category', 'MICRO', '--idea-key', key])
	directory = autoopt.iteration_dir(search, 1)
	bench(directory, 0.40)
	call([RUNNER, directory])
	call([AUTOOPT, 'readme', search, '--id', '1', '--template'])
	notes = os.path.join(directory, 'notes.md')
	with open(notes) as handle:
		filled = re.sub(r'<<FILL:[^>]*>>', 'Written.', handle.read())
	with open(notes, 'w') as handle:
		handle.write(filled)
	out = call([AUTOOPT, 'readme', search, '--id', '1'])
	text = open(ideas).read()
	suite.check('the subagent\'s readme strikes the line it was briefed from, '
		'with its label and what became of it', '- ~~[MICRO] pkg/a.py:f: '
		'hoist the invariant.' in text and '~~ A1: kept out' in text
		and '\n- [CACHING] pkg/b.py:g' in text and 'struck in ideas.md' in out,
		text[-500:])

	knowledge = os.path.join(search, 'knowledge.md')
	with open(knowledge, 'a') as handle:
		handle.write('- [code:a] f spends 80% in the loop (A0, root A0)\n'
			'- [code:b] g is never called twice (A0, root A0)\n')
	entries = os.path.join(base, 'books-entries.txt')
	with open(entries, 'w') as handle:
		handle.write('- [code:b] g is called twice per run (A1, root A0) '
			'(corrects: g is never called twice)\n'
			'- [harness] one run calls g twice (A1, root A0)\n')
	out = call([AUTOOPT, 'ledger', search, '--by', 'A1', '--add', entries])
	text = open(knowledge).read()
	suite.check('ledger --add appends the entries and strikes the one a '
		'correction names', '- ~~[code:b] g is never called twice (A0, root '
		'A0)~~ corrected by A1' in text and text.rstrip().endswith(
			'- [harness] one run calls g twice (A1, root A0)')
		and '- [code:a] f spends' in text, text[-600:])
	with open(knowledge, 'a') as handle:
		handle.write('- [code:c] h is O(n) in the input (A0, root A0)\n')
	with open(entries, 'w') as handle:
		handle.write('- [code:c] h is O(n log n) (A2, root A0) '
			'(corrects: h is O(n) in the input)\n')
	call([AUTOOPT, 'ledger', search, '--by', 'A2', '--add', entries])
	text = open(knowledge).read()
	suite.check('a correction whose words hold parentheses still strikes the '
		'entry', '- ~~[code:c] h is O(n) in the input (A0, root A0)~~ '
		'corrected by A2' in text, text[-600:])
	before = open(knowledge).read()
	with open(entries, 'w') as handle:
		handle.write('- [code:x] something (A2, root A0) (corrects: root A0)\n')
	out = call([AUTOOPT, 'ledger', search, '--by', 'A2', '--add', entries],
		expect=(1,))
	suite.check('a correction matching several entries is refused and writes '
		'nothing', 'matches' in out and open(knowledge).read() == before,
		out[-300:])


def setup_step(suite, base):
	"""Step 2 handed to a subagent: setup opens it, a question round-trips
	through the user, nothing is briefed until it is done, and --done
	refuses until every item of step 2 is on disk."""

	root = os.path.join(base, 'setup')
	os.makedirs(root)
	call([AUTOOPT, 'init', root, '--task', 'setup test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--runs', '2', '--staged', '--confirm-runs', '2'])
	search = os.path.join(root, 'autooptimize')
	spec = os.path.join(search, 'spec.md')
	env = dict(os.environ)
	env.pop('AUTOOPT_SKIP_SPEC_CHECK', None)

	def strict(args, expect=(0,)):
		done = subprocess.run([sys.executable] + args, capture_output=True,
			text=True, env=env)
		if done.returncode not in expect:
			raise RuntimeError(done.stdout + done.stderr)
		return prompted(done.stdout + done.stderr)

	out = strict([AUTOOPT, 'setup', search], expect=(1,))
	suite.check('setup refuses before the interview is written down',
		'interview is not written down' in out, out[-300:])

	with open(spec) as handle:
		text = handle.read()
	with open(spec, 'w') as handle:
		handle.write('\n'.join(line if 'Gate proof' in line or 'Width measured'
			in line or 'TODO' not in line else line.replace('TODO',
				'answered') for line in text.split('\n')))

	out = strict([AUTOOPT, 'setup', search])
	suite.check('setup opens step 2 and prints the subagent\'s prompt',
		'You are the setup subagent' in out and 'step2-baseline.md' in out
		and '--done' in out and os.path.exists(os.path.join(search, 'briefs',
			'setup.md')) and autoopt.load_state(search)['setup']['status']
		== 'open', out[:600])
	out = strict([AUTOOPT, 'setup', search], expect=(1,))
	suite.check('a second setup is refused while one is open',
		'already with a setup subagent' in out, out[-300:])
	out = strict([AUTOOPT, 'status', search, '--brief'])
	suite.check('status says setup runs and the first iteration waits for it',
		'setup (step 2): running' in out and 'first iteration: waiting for setup '
		'(step 2) and the idea miner\'s S1' in out, out[:800])
	out = strict([AUTOOPT, 'brief', search, '--id', '1', '--title', 't',
		'--category', 'MICRO', '--idea', 'x', '--tried', 'none'], expect=(1,))
	suite.check('nothing is briefed while setup is open',
		'still with the setup subagent' in out, out[-300:])

	strict([AUTOOPT, 'setup', search, '--ask', 'Width 1 or 2? Recommended: 1'])
	out = strict([AUTOOPT, 'status', search, '--brief'])
	suite.check('a question waits in status', 'waiting for the user: Width 1 '
		'or 2?' in out, out[:800])
	out = strict([AUTOOPT, 'setup', search, '--done'], expect=(1,))
	suite.check('setup --done refuses while a question waits and names what '
		'is missing', 'a question waits' in out and 'A0 has no result.json'
		in out and 'Gate proof' in out, out[-600:])
	out = strict([AUTOOPT, 'setup', search, '--answer', 'one'])
	suite.check('--answer records the reply and prints a prompt that '
		'continues from the notes', 'They answered: "one"' in out
		and 'notes.md' in out and autoopt.load_state(search)['setup'][
			'status'] == 'open', out[:700])
	message = out.split('If it is gone')[0]
	suite.check('and, first, a short message for the subagent that asked',
		out.startswith('Send this to the setup subagent that asked '
			'(SendMessage):') and 'answered your question ("Width 1 or 2? '
		'Recommended: 1"): "one"' in message and len(message) < 600,
		message)

	strict([AUTOOPT, 'setup', search, '--ask', 'Busy machine: wait?',
		'--recommend', 'wait'])
	out = strict([AUTOOPT, 'setup', search, '--ask', 'Tolerance 1e-5 or 1e-6?'])
	setup_state = autoopt.load_state(search)['setup']
	suite.check('a second question joins the first: one stop for both, the '
		'recommendation beside its question', setup_state['question'] ==
		'Busy machine: wait? (recommended: wait)\nTolerance 1e-5 or 1e-6?'
		and '2 waiting' in out, setup_state.get('question'))
	strict([AUTOOPT, 'setup', search, '--answer', 'wait; 1e-5'])
	strict([AUTOOPT, 'config', search, '--set',
		'after.setup_questions=recommended', '--reason', 'the user is away'])
	out = strict([AUTOOPT, 'setup', search, '--ask', 'A 12% faster documented '
		'flag: use it?', '--recommend', 'no, keep the defaults'])
	setup_state = autoopt.load_state(search)['setup']
	suite.check('under after.setup_questions=recommended the recommended '
		'answer is taken and nothing waits', setup_state['status'] == 'open'
		and setup_state['answers'][-1].get('by') == 'recommended' and
		'taken without asking' in out, out)
	out = strict([AUTOOPT, 'setup', search, '--ask', 'No recommendation?'])
	suite.check('a question with no recommendation still waits', 
		autoopt.load_state(search)['setup']['status'] == 'waiting', out)
	strict([AUTOOPT, 'setup', search, '--answer', 'yes'])

	# Step 2's items, as the subagent does them.
	bench(autoopt.iteration_dir(search, 0), 0.50)
	with open(spec) as handle:
		text = handle.read().replace('TODO', 'measured')
	with open(spec, 'w') as handle:
		handle.write(text)
	strict([RUNNER, autoopt.iteration_dir(search, 0)])
	out = strict([AUTOOPT, 'setup', search, '--done'], expect=(1,))
	suite.check('and still refuses without A0\'s README and a ledger entry',
		'README.md' in out and 'knowledge.md' in out
		and 'A0 has no result' not in out, out[-500:])
	strict([AUTOOPT, 'readme', search, '--id', '0', '--template'])
	notes = os.path.join(autoopt.iteration_dir(search, 0), 'notes.md')
	with open(notes) as handle:
		filled = re.sub(r'<<FILL:[^>]*>>', 'Written.', handle.read())
	with open(notes, 'w') as handle:
		handle.write(filled)
	strict([AUTOOPT, 'readme', search, '--id', '0'])
	entries = os.path.join(base, 'setup-ledger.txt')
	with open(entries, 'w') as handle:
		handle.write('- [harness] one run scores 0.50 (A0, root A0)\n')
	strict([AUTOOPT, 'ledger', search, '--by', 'A0', '--add', entries])
	out = strict([AUTOOPT, 'setup', search, '--done'], expect=(1,))
	suite.check('an algorithm search cannot close step 2 without the task\'s '
		'checks', 'no checks.compare registered' in out, out[-400:])
	# The task's checks, as step 2 writes them, and A0's outputs cached.
	folder = os.path.join(search, 'checks')
	os.makedirs(folder, exist_ok=True)
	with open(os.path.join(folder, 'compare.py'), 'w') as handle:
		handle.write('import os, sys\n'
			'if os.environ.get("AUTOOPT_WRITE_REFERENCE") == "1":\n'
			'\topen(os.path.join(sys.argv[2], "out.txt"), "w").write("x")\n'
			'print("1 input identical")\n')
	with open(os.path.join(folder, 'time_one.py'), 'w') as handle:
		handle.write('print(1.0)\n')
	strict([AUTOOPT, 'config', search, '--set', 'checks.compare=python3 {} '
		'{{src}} {{reference}}'.format(os.path.join(folder, 'compare.py')),
		'--set', 'checks.time=python3 {} {{src}}'.format(os.path.join(folder,
			'time_one.py')), '--reason', 'test'])
	strict([AUTOOPT, 'check', search, '--reference'])
	out = strict([AUTOOPT, 'setup', search, '--done'])
	suite.check('once everything is on disk, --done closes step 2',
		'step 2 is done' in out and autoopt.load_state(search)['setup'][
			'status'] == 'done', out[-300:])
	suite.check('and lists the answers it took without asking, for the user',
		'answers taken without asking' in out and 'no, keep the defaults' in
		out, out[-500:])
	out = strict([AUTOOPT, 'brief', search, '--id', '1', '--title', 't',
		'--category', 'MICRO', '--idea', 'x', '--tried', 'none'])
	suite.check('after which briefing works', 'You are running iteration A1'
		in out, out[-300:])


def single_count(suite, base):
	"""init's default: one count of runs per iteration, five, with no
	screening stage; the whole test suite only for a keep candidate."""

	staging = os.path.join(base, 'count-staging')
	os.makedirs(os.path.join(staging, 'pkg'))
	os.makedirs(os.path.join(staging, 'tests'))
	for name in ('pkg/core.py', 'tests/test_core.py', 'tests/test_other.py',
			'tests/test_bench.py'):
		with open(os.path.join(staging, name), 'w') as handle:
			handle.write('# {}\n'.format(name))
	bench(staging, 0.5)
	root = os.path.join(base, 'count')
	os.makedirs(root)
	call([AUTOOPT, 'init', root, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--promote',
		'orchestrator', '--copy', os.path.join(staging, 'pkg'), '--copy',
		os.path.join(staging, 'tests'), '--copy', os.path.join(staging,
			'bench.py'), '--tests', 'echo {selected}', '--tests-path', 'tests'])
	search = os.path.join(root, 'autooptimize')
	config = autoopt.load_config(search)
	suite.equal('init defaults to five runs per iteration, unstaged',
		(config['runs_per_iteration'], config['confirm_runs'],
			config['staged']), (5, 5, False))
	spec = open(os.path.join(search, 'spec.md')).read()
	suite.check('the spec\'s plan names one count', '- Plan: 5 runs per '
		'iteration; ' in spec and 'confirming' not in spec,
		[line for line in spec.splitlines() if 'Plan' in line])

	call([RUNNER, autoopt.iteration_dir(search, 0)])
	worse = add(search, 1, 0.30)
	better = None
	call([AUTOOPT, 'new', search, '--id', '2', '--title', 'better',
		'--category', 'MICRO'])
	directory = autoopt.iteration_dir(search, 2)
	bench(directory, 0.80)
	with open(os.path.join(directory, 'pkg', 'core.py'), 'a') as handle:
		handle.write('# changed\n')
	call([RUNNER, directory])
	better = autoopt.load_result(search, 2)
	suite.check('every iteration runs the count, a worse one too, with no '
		'early stop', autoopt.load_result(search, 0)['n_runs'] == 5
		and worse['n_runs'] == 5 and better['n_runs'] == 5
		and worse['keep']['verdict'] == 'fail', (worse['n_runs'],
			better['n_runs'], worse.get('keep')))
	suite.check('a keep candidate runs the whole suite after its subset',
		better['keep']['verdict'] == 'pass' and better['tests']['scope']
		== 'full' and better['tests']['seconds_total']
		>= better['tests']['seconds'], better.get('tests'))

	call([AUTOOPT, 'new', search, '--id', '3', '--title', 'tied',
		'--category', 'MICRO'])
	directory = autoopt.iteration_dir(search, 3)
	bench(directory, 0.505)
	with open(os.path.join(directory, 'pkg', 'core.py'), 'a') as handle:
		handle.write('# changed\n')
	call([RUNNER, directory])
	tied = autoopt.load_result(search, 3)
	suite.check('one that fails the keep rule keeps its subset pass',
		tied['keep']['verdict'] == 'fail' and tied['tests']['scope']
		== 'subset' and 'tests/test_core.py' in tied['tests']['selected']
		and 'tests/test_other.py' not in tied['tests']['selected'],
		(tied.get('keep'), tied.get('tests')))
	brief = call([AUTOOPT, 'brief', search, '--id', '4', '--title', 't',
		'--category', 'MICRO', '--idea', 'x', '--tried', 'none'])
	suite.check('the brief names the one count', '- Runs: 5 per iteration'
		in brief and 'confirming' not in brief, brief[-1500:])
	report = open(os.path.join(autoopt.iteration_dir(search, 2),
		'report.txt')).read()
	suite.check('the report\'s status row gives the count, no stage',
		'ok (5 runs)' in report, report[:600])
	out = call([RUNNER, autoopt.iteration_dir(search, 2), '--confirm-runs',
		'8'], expect=(1,))
	suite.check('the runner refuses a confirmation depth on an unstaged '
		'search', 'for a staged search' in out, out[-300:])
	call([AUTOOPT, 'config', search, '--set', 'runs_per_iteration=3',
		'--reason', 'test'])
	config = autoopt.load_config(search)
	suite.equal('changing the count moves the depth with it',
		(config['runs_per_iteration'], config['confirm_runs']), (3, 3))
	other = os.path.join(base, 'count-refused')
	os.makedirs(other)
	out = call([AUTOOPT, 'init', other, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--confirm-runs', '8'], expect=(1,))
	suite.check('init refuses --confirm-runs without --staged',
		'without --staged' in out, out[-300:])


def idea_miner(suite, base):
	"""The idea miner's stages, the count of ideas left, and status --brief."""

	blind = make_search(os.path.join(base, 'miner-profile'))
	call([AUTOOPT, 'config', blind, '--set', 'checks.profile=true {src}',
		'--reason', 'test: a registered profile check'])
	out = call([AUTOOPT, 'mine', blind], expect=(1,))
	suite.check('S1 is refused until A0 has a profile, naming the file and '
		'--without-profile', 'profile' in out and 'does not exist yet' in out
		and '--without-profile' in out
		and autoopt.load_state(blind).get('mining', {}).get('stage', 0) == 0,
		out)
	out = call([AUTOOPT, 'mine', blind, '--without-profile'])
	suite.check('--without-profile starts it blind', 'stage S1' in out, out)
	profiled = make_search(os.path.join(base, 'miner-profiled'))
	call([AUTOOPT, 'config', profiled, '--set', 'checks.profile=true {src}',
		'--reason', 'test: a registered profile check'])
	with open(autoopt.profile_path(profiled, 0), 'w') as handle:
		handle.write('50%  a.py:f\n')
	out = call([AUTOOPT, 'mine', profiled])
	suite.check('S1 starts once A0\'s profile exists', 'stage S1' in out, out)

	search = make_search(os.path.join(base, 'miner'))
	done = subprocess.run([sys.executable, AUTOOPT, 'mine', search],
		capture_output=True, text=True)
	out = prompted(done.stdout + done.stderr)
	suite.check('mine prints the miner\'s prompt for stage S1', 'idea miner' in out
		and 'stage S1' in out and 'idea-miner.md' in out, out[:400])
	suite.check('as a short prompt that has the miner read the prompt file, '
		'the protocol, the ledger and the ideas in its first turn',
		len(done.stdout) < 1500 and all(name in done.stdout for name in (
			'briefs/miner-1.md', 'references/idea-miner.md', 'knowledge.md',
			'ideas.md')) and 'keeps the session' not in done.stdout
		and 'the session\'s model' in done.stderr, done.stdout + done.stderr)
	suite.check('and keeps it as briefs/miner-1.md, which reserves no id',
		os.path.exists(os.path.join(search, 'briefs', 'miner-1.md'))
		and not autoopt.pending_briefs(search))
	mining = autoopt.load_state(search)['mining']
	suite.equal('state.json records the open stage', (mining['stage'],
		mining['done']), (1, 0))
	out = call([AUTOOPT, 'mine', search], expect=(1,))
	suite.check('a second miner is refused while S1 is open', 'still open' in out,
		out)

	add(search, 1, 0.55)
	suite.equal('an iteration made while S1 runs carries stage 0',
		autoopt.load_meta(search, 1).get('mining_stage'), 0)
	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- ~~[PARALLEL] b.py:g: threads. (S1)~~ A1: no gain\n')
	out = call([AUTOOPT, 'mine', search, '--done'], expect=(1,))
	suite.check('--done without the stage file is refused, and S1 stays open',
		'stage-1.md does not exist' in out and autoopt.load_state(search)[
			'mining']['done'] == 0, out)
	idea = ('- [MICRO] a.py:f: sort once. Why: x. Gain: y. Exact: '
		'bit-identical. (S1)')
	mining_dir = os.path.join(search, 'mining')
	os.makedirs(mining_dir, exist_ok=True)
	with open(os.path.join(mining_dir, 'stage-1.md'), 'w') as handle:
		handle.write(idea + '\n- [WARP] c.py:h: a category nobody uses. (S1)'
			'\n\n## Notes\n\nlong notes stay here\n')
	before = open(os.path.join(search, 'ideas.md')).read()
	out = call([AUTOOPT, 'mine', search, '--done'], expect=(1,))
	suite.check('a category the search does not use is refused, with nothing '
		'appended', 'WARP' in out and open(os.path.join(search, 'ideas.md'))
		.read() == before, out)
	with open(os.path.join(mining_dir, 'stage-1.md'), 'w') as handle:
		handle.write(idea + '\n\n## Notes\n\nlong notes stay here\n')
	with open(os.path.join(mining_dir, 'ledger-S1.txt'), 'w') as handle:
		handle.write('- [code:core] f sorts twice per call (S1, root A0)\n')
	out = call([AUTOOPT, 'mine', search, '--done'])
	ideas_text = open(os.path.join(search, 'ideas.md')).read()
	suite.check('--done appends the stage\'s idea lines, not its notes, and '
		'its ledger entries', ideas_text.count(idea) == 1
		and 'long notes' not in ideas_text and '- [code:core] f sorts twice '
		'per call (S1, root A0)' in open(os.path.join(search,
			'knowledge.md')).read(), out)
	suite.check('--done closes S1 and counts the untried ideas', 'S1 closed' in out
		and '1 untried' in out, out)
	add(search, 2, 0.45, category='NUMBA')
	meta = autoopt.load_meta(search, 2)
	suite.equal('an iteration made after S1 closes carries it, under a miner '
		'category', (meta.get('mining_stage'), meta.get('category')), (1, 'NUMBA'))
	out = call([AUTOOPT, 'mine', search, '--done'], expect=(1,))
	suite.check('--done with no stage open is refused', 'no stage' in out, out)

	out = call([AUTOOPT, 'status', search])
	suite.check('status counts the ideas and names the miner\'s stage',
		'ideas: 1 untried, 1 tried; idea miner: stage S1 done; the next stage '
		'is called for below 2 untried' in out, out[:600])
	brief = call([AUTOOPT, 'status', search, '--brief'])
	header = lambda text: next(l for l in text.splitlines() if l.startswith('id '))
	suite.check('status --brief folds the gates into one column and drops the '
		'observers', 'gates' in header(brief) and 'cost' not in header(brief)
		and 'size' not in header(brief) and 'cost' in header(out), header(brief))
	suite.check('and lists only the last settled iterations', '(the last 3 '
		'settled' in brief and len(brief) < len(out), brief[-300:])

	with open(os.path.join(autoopt.iteration_dir(search, 1), 'notes.md'),
			'w') as handle:
		handle.write(NOTES.replace('Closed.', 'Try the cache next.'))
	call([AUTOOPT, 'finish', search, '--id', '1'], expect=(0,))
	out = call([AUTOOPT, 'mine', search])
	suite.check('a later stage lists what settled since the last one, with '
		'what became of each', 'stage S2' in out and '## Settled since the '
		'last stage' in out and re.search(r'### A1: .*\n\n[\d.]+ against '
		r'A0\'s [\d.]+; ', out) and '### A2: ' in out, out[-1500:])
	suite.check('and the README\'s What to try next, so the miner reads no '
		'README', 'What to try next: Try the cache next.' in out
		and 'What to try next: (no README yet)' in out, out[-1500:])
	with open(os.path.join(mining_dir, 'stage-2.md'), 'w') as handle:
		handle.write(idea + '\n- [CACHING] a.py:f: keep the sort. Why: x. '
			'Gain: y. Exact: bit-identical. (S2)\n')
	out = call([AUTOOPT, 'mine', search, '--done'])
	suite.check('a line already in ideas.md is not appended twice',
		'appended 1 of the stage\'s 2' in out and open(os.path.join(search,
			'ideas.md')).read().count(idea) == 1, out)

	env = dict(os.environ)
	env.pop('AUTOOPT_SKIP_SPEC_CHECK', None)
	refused = subprocess.run([sys.executable, AUTOOPT, 'mine', search],
		capture_output=True, text=True, env=env)
	suite.check('mine refuses before the interview is written down',
		refused.returncode == 1 and 'interview is not written down'
		in refused.stderr, refused.stderr[-300:])
	spec = os.path.join(search, 'spec.md')
	lines = []
	for line in open(spec).read().splitlines():
		if 'Kind of search' in line:
			line = '- Kind of search: algorithm implementation'
		elif 'TODO' in line and not re.search('Gate proof|Width measured|'
				'Benchmark', line):
			line = line.replace('TODO', 'written')
		lines.append(line)
	open(spec, 'w').write('\n'.join(lines) + '\n')
	allowed = subprocess.run([sys.executable, AUTOOPT, 'mine', search],
		capture_output=True, text=True, env=env)
	suite.check('and starts while only step 2\'s three lines are open',
		allowed.returncode == 0 and 'stage S3' in allowed.stdout,
		allowed.stderr[-300:])
	text = prompted(allowed.stdout)
	suite.check('offering an algorithm search the miner\'s ten categories',
		'`NUMBA`' in text and '`CACHE_MISS`' in text and '`IO`' in text
		and '`ALGORITHM`' in text and '`ARCHITECTURE`' not in text, text[:900])
	prompt = open(os.path.join(search, 'briefs', 'miner-3.md')).read()
	suite.check('and saying the Benchmark line is not written yet',
		'- What one run does: not written yet' in prompt, prompt[:1200])


def test_subset(suite, base):
	"""Per-iteration tests run the files for the changed modules; a
	confirmed iteration then runs the whole suite, and promote refuses one
	that has only passed a subset."""

	original = os.path.join(base, 'subset-original')
	make_original(original)
	with open(os.path.join(original, 'pkg', 'other.py'), 'w') as out:
		out.write('X = 1\n')
	with open(os.path.join(original, 'tests', 'test_other.py'), 'w') as out:
		out.write('')
	git('-C', original, 'add', '-A')
	git('-C', original, '-c', 'user.name=t', '-c', 'user.email=t@t',
		'commit', '-q', '-m', 'other')

	harness = os.path.join(base, 'subset-bench.py')
	with open(harness, 'w') as out:
		out.write(HARNESS)

	root = os.path.join(base, 'subset-search')
	search = os.path.join(root, 'autooptimize')

	# The command logs what it was asked to run, then runs the real check.
	call([AUTOOPT, 'init', root, '--task', 'test', '--repo', original,
		'--copy', harness, '--command',
		'python3 {}/harness/subset-bench.py --src {{src}} --seed {{seed}} '
		'--out {{out}}'.format(search),
		'--objective', 'score', '--direction', 'max',
		'--gates', json.dumps([{'name': 'cost', 'op': '<=',
			'threshold': 1.0}]),
		'--observers', 'size', '--no-gpu', '--runs', '3',
		'--staged', '--confirm-runs', '6', '--promote', 'orchestrator',
		'--tests', 'echo {selected} >> {out}/calls.txt && '
			'python3 tests/check.py'])

	def calls(identifier):
		with open(os.path.join(autoopt.iteration_dir(search, identifier),
			'unit-tests', 'calls.txt')) as infile:
			return [line.strip() for line in infile]

	call([RUNNER, autoopt.iteration_dir(search, 0)])
	suite.equal('the baseline, with no parent, runs the full suite',
		calls(0), ['tests'])

	worse = repo_add(search, 1, 0, 0.30)
	suite.equal('a change to pkg/core.py runs tests/test_core.py only',
		calls(1), ['tests/test_core.py'])
	suite.equal('and records its scope', worse['tests']['scope'], 'subset')
	suite.check('the table marks a subset pass',
		'pass sub' in call([AUTOOPT, 'status', search]), '')

	better = repo_add(search, 2, 0, 0.60)
	suite.equal('a confirmed iteration then runs the full suite',
		calls(2), ['tests/test_core.py', 'tests'])
	suite.equal('and its record is the full run',
		(better['stage'], better['tests']['scope']), ('confirmed', 'full'))
	suite.check('and its test time counts the subset pass and the full one',
		better['tests']['seconds_total'] > better['tests']['seconds'],
		better['tests'])

	out = call([AUTOOPT, 'promote', search, '--id', '1'], expect=(1,))
	suite.check('promote refuses a subset-only pass, naming --full-tests',
		'--full-tests' in out, out)
	call([RUNNER, autoopt.iteration_dir(search, 1), '--full-tests'])
	suite.equal('--full-tests runs the whole suite on a finished iteration',
		calls(1), ['tests/test_core.py', 'tests'])
	out = call([AUTOOPT, 'promote', search, '--id', '1'], expect=(0,))
	suite.check('after which promote accepts it', 'root is now' in out, out)

	# A validation script in harness/ finds the code through {src}: in
	# repository mode the iteration directory holds only records.
	check = os.path.join(search, 'harness', 'validate_src.py')
	with open(check, 'w') as handle:
		handle.write(
			'import json, os, sys\n'
			'src, out = sys.argv[1], sys.argv[2]\n'
			'os.makedirs(out, exist_ok=True)\n'
			"json.dump({'measures': {'has_pkg': int(os.path.isdir(\n"
			"	os.path.join(src, 'pkg')))}, 'note': src},\n"
			"	open(os.path.join(out, 'validation.json'), 'w'))\n")
	config_path = os.path.join(search, 'config.json')
	config = json.load(open(config_path))
	config['external_validation'] = {'command': 'python3 {} {{src}} {{out}} '
		'# {{iteration}}'.format(check), 'measures': ['has_pkg'],
		'note': '', 'approved': True}
	json.dump(config, open(config_path, 'w'), indent=1)
	out = call([AUTOOPT, 'validate', search, '--id', '1', '--round', '9'])
	record = autoopt.load_validation(search, 9)
	suite.check('a validation command gets {src}, the repository-mode '
		'checkout', record is not None and record['measures'].get(
			'has_pkg') == 1 and record['note'] == autoopt.checkout_dir(
			search, 1, autoopt.load_config(search)), out)

	directory = call([AUTOOPT, 'new', search, '--id', '3', '--parent', '1',
		'--title', 'new module', '--category', 'MISC']).strip().splitlines()[-1]
	with open(os.path.join(directory, 'src', 'pkg', 'fresh.py'), 'w') as out:
		out.write('Y = 2\n')
	call([RUNNER, directory])
	suite.equal('a module with no test file runs the full suite',
		calls(3), ['tests'])

	directory = call([AUTOOPT, 'new', search, '--id', '4', '--parent', '1',
		'--title', 'two modules', '--category', 'MISC']).strip().splitlines()[-1]
	with open(os.path.join(directory, 'src', 'pkg', 'other.py'), 'w') as out:
		out.write('X = 3\n')
	set_quality(os.path.join(directory, 'src'), 0.20)
	call([RUNNER, directory])
	suite.equal('two changed modules run both their test files',
		calls(4), ['tests/test_core.py tests/test_other.py'])


PROBE = """\
import os, sys
src, kind = sys.argv[1], sys.argv[2]
cache = os.environ['NUMBA_CACHE_DIR']
if not cache.startswith(src):
	sys.exit('cache {} belongs to another side'.format(cache))
counter = os.path.join(src, 'count')
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, 'w').write(str(n + 1))
if kind == 'fail' and n > 2:
	sys.exit(3)
if kind == 'fast':
	print('seconds', 0.90)
elif kind == 'alternate':
	print('seconds', 1.0 + (0.01 if n % 2 else -0.01))
else:
	print('seconds', 1.00)
"""

COLD_PROBE = """\
import os, sys
cache = os.environ['NUMBA_CACHE_DIR']
print('empty' if not os.listdir(cache) else 'NOT EMPTY', cache, file=open(sys.argv[2], 'a'))
open(os.path.join(cache, 'index.nbi'), 'w').write('x')
print('first call', 20.0 if sys.argv[1].endswith('old') else 15.0)
"""


def measuring_scripts(suite, base):
	root = os.path.join(base, 'measure')
	sides = {name: os.path.join(root, name) for name in ('a', 'b', 'c', 'd',
		'e', 'f', 'old', 'new')}
	for directory in sides.values():
		os.makedirs(directory, exist_ok=True)
	probe = os.path.join(root, 'probe.py')
	with open(probe, 'w') as out:
		out.write(PROBE)
	paired = os.path.join(SCRIPTS, 'paired.py')

	def run(a, b, kind_b, kind_a='same', expect=(0,)):
		return call([paired, '--a', sides[a], '--b', sides[b], '--json',
			'--log', os.path.join(root, 'log-{}{}.txt'.format(a, b)), '--cmd',
			'python3 {} {{src}} "$([ {{side}} = a ] && echo {} || echo {})"'.format(
				probe, kind_a, kind_b)], expect=expect)

	clear = json.loads(run('a', 'b', 'fast'))
	suite.equal('a unanimous difference stops at 9 pairs, as B better',
		(clear['pairs'], clear['verdict'], clear['stopped_by']),
		(9, 'B better', 'sign test'))
	lines = [line.split() for line in open(os.path.join(root, 'log-ab.txt'))
		if line.startswith('pair ')]
	firsts = [row[2] for row in lines[0::2]]
	suite.equal('the side run first alternates, pair by pair',
		firsts[:4], ['a', 'b', 'a', 'b'])
	tie = json.loads(run('c', 'd', 'alternate'))
	suite.equal('a split result runs to 24 pairs and resolves nothing',
		(tie['pairs'], tie['verdict']), (24, 'no difference resolved'))
	out = run('e', 'f', 'fail', expect=(1,))
	suite.check('a failing run stops the comparison', 'failed' in out, out)

	cold_probe = os.path.join(root, 'cold_probe.py')
	with open(cold_probe, 'w') as out:
		out.write(COLD_PROBE)
	record = os.path.join(root, 'cold-record.txt')
	scratch = os.path.join(root, 'cold-scratch')
	out = call([os.path.join(SCRIPTS, 'cold.py'), '--side', 'old=' + sides['old'],
		'--side', 'new=' + sides['new'], '--scratch', scratch, '--reps', '2',
		'--json', '--cmd', 'python3 {} {{src}} {}'.format(cold_probe, record)])
	got = json.loads(out[out.index('{'):])
	rows = [line.split() for line in open(record)]
	suite.check('every cold run starts on an empty cache of its own, under '
		'--scratch', len(rows) == 4 and all(r[0] == 'empty' for r in rows)
		and len({r[1] for r in rows}) == 4
		and all(r[1].startswith(scratch) for r in rows), rows)
	order = [line.split()[2] for line in out.splitlines() if line.startswith('rep')]
	suite.equal('the sides alternate order across repetitions', order,
		['old', 'new', 'new', 'old'])
	suite.equal('and each side reports its median',
		(got['old']['median'], got['new']['median']), (20.0, 15.0))

	# Times and differences print to significant figures, so a real
	# difference of a few microseconds is not shown as zero.
	root = os.path.join(base, 'small')
	sides = {name: os.path.join(root, name) for name in 'ab'}
	for directory in sides.values():
		os.makedirs(directory)
	probe = os.path.join(root, 'probe.py')
	with open(probe, 'w') as out:
		out.write('import sys\nprint("seconds", 0.000412 if '
			'sys.argv[1].endswith("a") else 0.000409)\n')
	cmd = 'python3 {} {{src}}'.format(probe)
	out = call([os.path.join(SCRIPTS, 'paired.py'), '--a', sides['a'], '--b',
		sides['b'], '--cmd', cmd], expect=(0,))
	median = re.search(r'median (\S+),', out)
	suite.check('paired.py prints a 3 us difference as nonzero',
		median is not None and float(median.group(1)) != 0
		and abs(float(median.group(1)) + 3e-6) < 1e-7, out[-300:])
	out = call([os.path.join(SCRIPTS, 'cold.py'), '--side', 'a=' + sides['a'],
		'--side', 'b=' + sides['b'], '--reps', '1', '--scratch',
		os.path.join(root, 'scratch'), '--cmd', cmd], expect=(0,))
	medians = re.findall(r'median (\S+) s', out)
	suite.check('cold.py prints sub-millisecond times as nonzero',
		sorted(float(m) for m in medians) == [0.000409, 0.000412], out[-300:])

	for flags in (('--min-pairs', '8', '--max-pairs', '4'),
		('--max-pairs', '0')):
		done = subprocess.run([sys.executable, paired, '--a', base, '--b',
			base, '--cmd', 'false'] + list(flags), capture_output=True,
			text=True)
		suite.check('paired.py refuses {}'.format(' '.join(flags)),
			done.returncode != 0 and '--max-pairs' in done.stderr
			and 'failed (exit' not in done.stderr, done.stderr[-300:])


def numba_cache(suite, base):
	"""{cache} against the footgun it exists for: numba's cache=True checks
	only the decorated function's own file, so an edit to a callee in
	another file is served stale from a cache kept across the edit."""

	try:
		import numba  # noqa: F401
	except ImportError:
		print('  skip  numba is not installed; {cache} is untested here')
		return

	original = os.path.join(base, 'numba-original')
	os.makedirs(os.path.join(original, 'pkg'))
	callee = lambda d, k: open(os.path.join(d, 'pkg', 'callee.py'), 'w').write(
		'from numba import njit\n\n@njit(cache=True)\ndef f(x):\n'
		'\treturn x + {}\n'.format(k))
	open(os.path.join(original, 'pkg', '__init__.py'), 'w').write('')
	open(os.path.join(original, 'pkg', 'driver.py'), 'w').write(
		'from numba import njit\nfrom pkg.callee import f\n\n'
		'@njit(cache=True)\ndef g(x):\n\treturn f(x) * 10\n')
	callee(original, 1)
	git('-C', original, 'init', '-q', '-b', 'main')
	git('-C', original, 'add', '-A')
	git('-C', original, '-c', 'user.name=t', '-c', 'user.email=t@t',
		'commit', '-q', '-m', 'initial')

	harness = os.path.join(base, 'numba_bench.py')
	open(harness, 'w').write(NUMBA_HARNESS)

	root = os.path.join(base, 'numba-search')
	search = os.path.join(root, 'autooptimize')
	env = 'PYTHONDONTWRITEBYTECODE=1 NUMBA_CACHE_DIR={cache}/numba '
	call([AUTOOPT, 'init', root, '--task', 't', '--repo', original,
		'--copy', harness, '--command', env + 'python3 {}/harness/numba_bench.py '
		'--src {{src}} --seed {{seed}} --out {{out}}'.format(search),
		'--objective', 'score', '--direction', 'max', '--gates',
		json.dumps([{'name': 'cost', 'op': '<=', 'threshold': 1.0}]),
		'--observers', 'size', '--no-gpu', '--runs', '2', '--staged', '--confirm-runs',
		'2', '--promote', 'orchestrator'])
	call([RUNNER, autoopt.iteration_dir(search, 0)])
	suite.equal('{cache}: the baseline measures its own code',
		autoopt.load_result(search, 0)['objective']['median'], 20.0)

	directory = call([AUTOOPT, 'new', search, '--id', '1', '--parent', '0',
		'--title', 'callee +2', '--category', 'MISC']).strip().splitlines()[-1]
	src = os.path.join(directory, 'src')
	callee(src, 2)
	call([RUNNER, directory, '--runs', '1', '--stage', 'screen'])
	first = os.listdir(os.path.join(directory, 'cache'))

	# The case the cache must survive: a smoke run, then an edit to the
	# callee alone, then the full run.
	callee(src, 3)
	call([RUNNER, directory, '--rerun'])
	result = autoopt.load_result(search, 1)
	suite.equal('an edit to a callee after a smoke run is measured, not '
		'served from the cache', result['objective']['values'], [40.0, 40.0])
	dirs = sorted(os.listdir(os.path.join(directory, 'cache')))
	suite.check('each commit gets its own cache directory',
		len(first) == 1 and len(dirs) == 2 and first[0] in dirs
		and result['commit'][:12] in dirs, (first, dirs))
	suite.check('and the runs of one commit share it',
		any(name.endswith('.nbi') for _, _, names in os.walk(os.path.join(
			directory, 'cache', result['commit'][:12])) for name in names), dirs)
	suite.check('the cache is not committed',
		'cache' not in git('-C', os.path.join(search, 'repo.git'), 'ls-tree',
			'-r', '--name-only', 'autoopt/1'), '')

	# The control: one cache kept across the same edit serves the old code.
	# If this ever reads the new value, numba changed and the test above no
	# longer proves anything.
	shared = os.path.join(base, 'numba-shared-cache')
	run = lambda: subprocess.run([sys.executable, '-c',
		'import sys; sys.path.insert(0, {!r}); from pkg.driver import g; '
		'print(g(1.0))'.format(src)], capture_output=True, text=True,
		env=dict(os.environ, NUMBA_CACHE_DIR=shared,
			PYTHONDONTWRITEBYTECODE='1')).stdout.strip()
	callee(src, 4)
	fresh = run()
	callee(src, 5)
	stale = run()
	suite.equal('control: a cache shared across a callee edit is stale',
		(fresh, stale), ('50.0', '50.0'))


def spec_gate(suite, base):
	"""Step 0 has to be written down before anything forks or runs."""

	root = os.path.join(base, 'spec-gate')
	os.makedirs(root)
	bench(root, 0.5)
	env = dict(os.environ)
	env.pop('AUTOOPT_SKIP_SPEC_CHECK', None)

	def run(args):
		out = subprocess.run([sys.executable] + args, capture_output=True,
			text=True, env=env)
		return out.returncode, out.stdout + out.stderr

	code, out = run([AUTOOPT, 'init', root, '--task', 'spec gate',
		'--command', 'python3 bench.py --seed {seed} --out {out}',
		'--objective', 'score', '--direction', 'max', '--no-gpu',
		'--runs', '1', '--staged', '--confirm-runs', '1',
		'--copy', os.path.join(root, 'bench.py')])
	search = os.path.join(root, 'autooptimize')
	spec = os.path.join(search, 'spec.md')
	suite.check('init writes a spec.md skeleton and says so',
		code == 0 and os.path.exists(spec) and 'spec.md' in out, out[-400:])
	text = open(spec).read()
	suite.check('the skeleton carries the config and TODO lines',
		'Objective: score, maximized' in text and 'Keep rule: TODO' in text
		and 'Asked on: TODO' in text, text[:600])
	config = autoopt.load_config(search)
	suite.check('a --no-gpu skeleton states the runs at a time config.json '
		'sets, not a GPU count', 'no GPU, {} run(s) at a time'.format(
			config['gpus_per_agent']) in text and 'GPU(s)' not in text,
		[line for line in text.splitlines() if 'subagent' in line])
	flagged = [line for line in text.splitlines() if 'TODO' in line]
	suite.check('only answer placeholders read TODO, never the preamble',
		flagged and all(line.lstrip('- ').split(':')[0] in ('Asked on',
			'Kind of search', 'New unit tests', 'Gates', 'Gate proof',
			'Keep rule', 'Thread counts timed, tracking inputs',
			'Machine and environment', 'Width measured', 'Benchmark',
			'External validation', 'Off limits',
			'Portability', 'TODO')
			for line in flagged), flagged)

	code, out = run([RUNNER, autoopt.iteration_dir(search, 0)])
	suite.check('the baseline refuses to run while spec.md has TODO lines',
		code != 0 and 'step 0 is not finished' in out and 'Keep rule' in out
		and not os.path.exists(os.path.join(autoopt.iteration_dir(search, 0),
			'result.json')), out[-400:])

	code, out = run([AUTOOPT, 'new', search, '--title', 'x',
		'--category', 'MISC'])
	suite.check('new refuses too', code != 0
		and 'step 0 is not finished' in out, out[-400:])

	with open(spec, 'w') as handle:
		handle.write(text.replace('TODO', 'answered'))

	code, out = run([AUTOOPT, 'new', search, '--title', 'early',
		'--category', 'MISC'])
	suite.check('new before the baseline has run is refused plainly',
		code != 0 and 'no commit to fork from' in out
		and 'Traceback' not in out, out[-300:])

	code, out = run([RUNNER, autoopt.iteration_dir(search, 0)])
	suite.check('once every TODO is answered, the baseline runs', code == 0
		and autoopt.load_result(search, 0) is not None, out[-400:])

	code, out = run([AUTOOPT, 'new', search, '--title', 'x',
		'--category', 'MISC'])
	code, out = run([AUTOOPT, 'status', search])
	suite.check('status names a missing dashboard while work is in flight',
		'dashboard: no publisher is running' in out, out[:600])

	os.remove(spec)
	code, out = run([AUTOOPT, 'new', search, '--title', 'y',
		'--category', 'MISC'])
	suite.check('a missing spec.md is refused as well', code != 0
		and 'does not exist' in out, out[-300:])


def restore_guard(suite, base):
	"""restore checks the branch before it touches the directory, and moves a
	file that is not a record aside rather than deleting it."""

	search = make_search(os.path.join(base, 'restore'))
	add(search, 1, 0.40)
	call([AUTOOPT, 'prune', search, '--keep-recent', '0', '--worktrees',
		'--apply'])
	directory = autoopt.iteration_dir(search, 1)
	notes = os.path.join(directory, 'notes.txt')
	with open(notes, 'w') as out:
		out.write('hand-written\n')

	out = call([AUTOOPT, 'restore', search, '--id', '1', '--branch',
		'autoopt/no-such'], expect=(1,))
	suite.check('restore refuses a branch that does not exist, plainly',
		'autoopt/no-such' in out and 'Traceback' not in out, out[-300:])
	suite.check('and leaves the directory exactly as it was',
		os.path.exists(notes) and os.path.exists(os.path.join(directory,
			'result.json')), os.listdir(directory))

	out = call([AUTOOPT, 'restore', search, '--id', '1'])
	aside = [name for name in os.listdir(os.path.dirname(directory))
		if name.startswith('1.restore-aside-')]
	suite.check('restore moves a file that is not a record aside, and says '
		'where', len(aside) == 1 and os.path.exists(os.path.join(
			os.path.dirname(directory), aside[0], 'notes.txt'))
		and aside[0] in out, (aside, out[-300:]))
	suite.check('and restores the checkout with the record in place',
		os.path.exists(os.path.join(directory, '.git'))
		and os.path.exists(os.path.join(directory, 'bench.py'))
		and autoopt.load_result(search, 1) is not None, os.listdir(directory))
	suite.equal('and the aside directory is not an iteration',
		autoopt.iteration_ids(search), [0, 1])


def init_force(suite, base):
	"""init --force rewrites the config and nothing the search has since
	written: the root, the round, the baseline's measured commit, spec.md
	and ideas.md."""

	root = os.path.join(base, 'force')
	search = make_search(root)
	add(search, 1, 0.60)
	call([AUTOOPT, 'promote', search, '--id', '1'])
	commit = autoopt.load_meta(search, 0).get('commit')
	spec = os.path.join(search, 'spec.md')
	with open(spec) as infile:
		written = infile.read().replace('TODO', 'answered')
	with open(spec, 'w') as out:
		out.write(written)
	with open(os.path.join(search, 'ideas.md'), 'a') as out:
		out.write('- an edited line\n')

	call([AUTOOPT, 'init', root, '--task', 'test', '--force', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--promote', 'orchestrator'])
	state = autoopt.load_state(search)
	suite.equal('init --force keeps the root and the round',
		(state['root'], state['round']), (1, 2))
	suite.equal('and the baseline meta.json the runner wrote',
		autoopt.load_meta(search, 0).get('commit'), commit)
	suite.check('and a written spec.md', open(spec).read() == written,
		open(spec).read()[:300])
	suite.check('and an edited ideas.md',
		'an edited line' in open(os.path.join(search, 'ideas.md')).read())


def no_identity(suite, base):
	"""A machine with no git identity: the runner's commit still lands."""

	home = os.path.join(base, 'no-identity-home')
	os.makedirs(home)
	saved = dict(os.environ)
	# useConfigOnly stops git guessing an identity from the hostname, which
	# is what a machine with an unresolvable domain does anyway.
	os.environ.update(HOME=home, XDG_CONFIG_HOME=home, GIT_CONFIG_NOSYSTEM='1',
		GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='user.useConfigOnly',
		GIT_CONFIG_VALUE_0='true')
	for key in ('GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_EMAIL', 'EMAIL',
		'GIT_AUTHOR_NAME', 'GIT_COMMITTER_NAME'):
		os.environ.pop(key, None)

	try:
		search = make_search(os.path.join(base, 'no-identity'))
		result = autoopt.load_result(search, 0)
		suite.check('with no git identity the baseline is still committed '
			'and measured', result is not None and result['status'] == 'ok'
			and result.get('commit'), result and result.get('status'))
	except RuntimeError as error:
		suite.check('with no git identity the baseline is still committed '
			'and measured', False, str(error)[-300:])
	finally:
		os.environ.clear()
		os.environ.update(saved)


# A git without `worktree add --orphan` (git 2.42 and later has it);
# `checkout --orphan` still works.
OLD_GIT = """#!/bin/sh
case " $* " in
	*" worktree "*" --orphan "*)
		echo "error: unknown option \\`orphan'" >&2
		exit 129;;
esac
exec {git} "$@"
"""


def old_git(suite, base):
	"""Plain-mode init on a git without `worktree add --orphan`."""

	bin_dir = os.path.join(base, 'old-git-bin')
	os.makedirs(bin_dir)
	wrapper = os.path.join(bin_dir, 'git')
	with open(wrapper, 'w') as out:
		out.write(OLD_GIT.format(git=shutil.which('git')))
	os.chmod(wrapper, 0o755)
	saved = os.environ['PATH']
	os.environ['PATH'] = bin_dir + os.pathsep + saved

	try:
		root = os.path.join(base, 'old-git')
		os.makedirs(root)
		out = call([AUTOOPT, 'init', root, '--task', 'test', '--command',
			'python3 bench.py --seed {seed} --out {out}', '--objective',
			'score', '--direction', 'max', '--no-gpu'], expect=(0, 1))
		search = os.path.join(root, 'autooptimize')
		made = os.path.isdir(autoopt.iteration_dir(search, 0)) \
			and 'Traceback' not in out
		suite.check('plain-mode init works without worktree add --orphan',
			made, out[-300:])
		if not made:
			return

		out = call([AUTOOPT, 'new', search, '--title', 'x', '--category',
			'MISC'], expect=(1,))
		suite.check('and the baseline still has no commit until it runs',
			'no commit to fork from' in out, out[-300:])

		bench(autoopt.iteration_dir(search, 0), 0.50)
		call([RUNNER, autoopt.iteration_dir(search, 0)])
		first = add(search, 1, 0.60)
		bare = os.path.join(search, 'repo.git')
		suite.equal('the baseline branch is one root commit holding the code',
			(git('-C', bare, 'rev-list', '--count', 'autoopt/0').strip(),
				git('-C', bare, 'ls-tree', '--name-only',
					'autoopt/0').split()), ('1', ['bench.py']))
		suite.equal('and an iteration forks from it', first['status'], 'ok')
	finally:
		os.environ['PATH'] = saved


def plain_excludes(suite, base):
	"""Plain mode's exclude list hides records and saved arrays; a copied input
	it would hide is refused at init, prune never takes the baseline's
	files, and neither a subagent's probe/ nor cache/ is committed."""

	staging = os.path.join(base, 'excl-staging')
	os.makedirs(os.path.join(staging, 'pkg'))
	with open(os.path.join(staging, 'pkg', 'inputs.npz'), 'w') as out:
		out.write('data')
	bench(staging, 0.5)

	root = os.path.join(base, 'excl-refused')
	os.makedirs(root)
	out = call([AUTOOPT, 'init', root, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--copy', os.path.join(staging,
			'pkg'), '--copy', os.path.join(staging, 'bench.py')],
		expect=(0, 1))
	suite.check('init refuses a copied file the exclude list would hide',
		'pkg/inputs.npz' in out and 'reference/' in out
		and not os.path.exists(os.path.join(root, 'autooptimize',
			'config.json')), out[-400:])

	search = make_search(os.path.join(base, 'excl'))
	held = os.path.join(autoopt.iteration_dir(search, 0), 'inputs.npz')
	with open(held, 'w') as out:
		out.write('placed by hand')
	call([AUTOOPT, 'prune', search, '--apply'])
	suite.check('prune never deletes a file from the baseline',
		os.path.exists(held), '')

	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'probe',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	os.makedirs(os.path.join(directory, 'probe'))
	with open(os.path.join(directory, 'probe', 'time_one.py'), 'w') as out:
		out.write('print(1)\n')
	bench(directory, 0.40)
	call([RUNNER, directory])
	tree = git('-C', os.path.join(search, 'repo.git'), 'ls-tree', '-r',
		'--name-only', 'autoopt/1').split()
	suite.check('a subagent\'s probe/ is not committed',
		'bench.py' in tree and not any(name.startswith('probe/')
			for name in tree), tree)
	before = autoopt.load_result(search, 1)['commit']
	bench(directory, 0.45)
	call([RUNNER, directory, '--rerun'])
	suite.check('an edit after a run, with cache/ and probe/ on disk, is '
		'committed', autoopt.load_result(search, 1)['commit'] != before, '')
	call([AUTOOPT, 'prune', search, '--keep-recent', '0', '--worktrees',
		'--apply'])
	suite.check('and it survives prune --worktrees with the record',
		os.path.exists(os.path.join(directory, 'probe', 'time_one.py'))
		and not os.path.exists(os.path.join(directory, '.git')),
		os.listdir(directory))

	plain = make_search(os.path.join(base, 'plain-cache'))
	exclude = os.path.join(plain, 'repo.git', 'info', 'exclude')
	suite.check('a new plain-mode search excludes cache/',
		'cache/' in open(exclude).read().split(), '')


DEVICE_BENCH = """\
import os, sys, json, time, argparse
p = argparse.ArgumentParser()
p.add_argument('--seed', type=int)
p.add_argument('--out')
a = p.parse_args()
start = time.time()
time.sleep(1.5 if a.seed == 0 else 0.2)
os.makedirs(a.out, exist_ok=True)
json.dump({'device': os.environ.get('CUDA_VISIBLE_DEVICES'), 'start': start,
	'end': time.time()}, open(os.path.join(a.out, 'device.json'), 'w'))
json.dump({'objective': 0.5 + a.seed / 100}, open(os.path.join(a.out,
	'metrics.json'), 'w'))
"""


def device_queue(suite, base):
	"""Each leased GPU runs one seed at a time, whichever finishes first."""

	root = os.path.join(base, 'devices')
	os.makedirs(root)
	call([AUTOOPT, 'init', root, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--runs', '4', '--staged', '--confirm-runs', '4',
		'--gpus-per-agent', '2'])
	search = os.path.join(root, 'autooptimize')
	directory = autoopt.iteration_dir(search, 0)
	with open(os.path.join(directory, 'bench.py'), 'w') as out:
		out.write(DEVICE_BENCH)

	# Indices no machine has: the benchmark only reads the variable.
	subprocess.run([sys.executable, RUNNER, directory], check=True,
		capture_output=True, env=dict(os.environ, CUDA_VISIBLE_DEVICES='71,93'))
	spans = [json.load(open(os.path.join(directory, 'runs', 'seed-{}'.format(
		seed), 'device.json'))) for seed in range(4)]
	overlap = [(a['device'], i, j) for i, a in enumerate(spans)
		for j, b in enumerate(spans) if i < j and a['device'] == b['device']
		and a['start'] < b['end'] and b['start'] < a['end']]
	suite.equal('no two runs share a leased GPU at once', overlap, [])
	suite.equal('and every run had one of the leased GPUs',
		sorted({span['device'] for span in spans}), ['71', '93'])


# A run that starts a child of its own and then hangs: what a benchmark with
# worker processes looks like to a timeout.
HANG_BENCH = """\
import os, sys, time, subprocess, argparse
p = argparse.ArgumentParser()
p.add_argument('--seed', type=int)
p.add_argument('--out')
a = p.parse_args()
child = subprocess.Popen(['sleep', '120'])
with open({pids!r}, 'a') as out:
	out.write('{{}} {{}}\\n'.format(os.getpid(), child.pid))
time.sleep(120)
"""


def alive(pid):
	"""Running, as opposed to gone or a zombie waiting to be reaped."""

	try:
		with open('/proc/{}/stat'.format(pid)) as handle:
			return handle.read().split(')')[-1].split()[0] != 'Z'
	except OSError:
		return False


def survivors(pids, wait=5.0):
	"""The pids in `pids` (a file of 'pid child' lines) still running after
	up to `wait` seconds."""

	deadline = time.time() + wait
	numbers = [int(n) for line in open(pids) for n in line.split()]

	while time.time() < deadline:
		left = [pid for pid in numbers if alive(pid)]
		if not left:
			return []
		time.sleep(0.1)

	for pid in left:
		try:
			os.kill(pid, 9)
		except OSError:
			pass

	return left


def process_groups(suite, base):
	"""A timeout, or Ctrl-C, kills everything a run started, not only the
	shell that launched it."""

	pids = os.path.join(base, 'hang-pids.txt')
	search = make_search(os.path.join(base, 'hang'), runs=1, confirm_runs=1,
		timeout=1)
	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'hangs',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	with open(os.path.join(directory, 'bench.py'), 'w') as out:
		out.write(HANG_BENCH.format(pids=pids))
	started = time.time()
	call([RUNNER, directory])
	suite.check('a run past --timeout is stopped on time',
		time.time() - started < 30, time.time() - started)
	suite.equal('and nothing it started is left running', survivors(pids), [])

	# Ctrl-C reaches the runner's process group, and the runs are in groups
	# of their own, so the runner has to pass it on.
	open(pids, 'w').close()
	search = make_search(os.path.join(base, 'interrupt'), runs=2,
		confirm_runs=2, gpus_per_agent=2)
	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'hangs',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	with open(os.path.join(directory, 'bench.py'), 'w') as out:
		out.write(HANG_BENCH.format(pids=pids))
	runner = subprocess.Popen([sys.executable, RUNNER, directory],
		stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
		start_new_session=True)
	deadline = time.time() + 20
	while time.time() < deadline and len(open(pids).readlines()) < 2:
		time.sleep(0.1)
	os.killpg(runner.pid, 2)
	try:
		runner.wait(timeout=20)
	except subprocess.TimeoutExpired:
		runner.kill()
	suite.equal('Ctrl-C stops every run and what it started',
		survivors(pids), [])


def spaces(suite, base):
	"""A search under a path with a space: every substituted path reaches
	the command as one word, and a placeholder that is not one fails at
	init."""

	base = os.path.join(base, 'with space')
	search = make_search(os.path.join(base, 'search'), runs=1,
		confirm_runs=1, command='python3 bench.py --seed {seed} --out {out} '
		'&& test -d {out_abs} && test -d {iteration} && test -d {cache}',
		tests='test -d {src} && test -d {cache} && test -d {out} '
			'&& test -d {iteration}',
		validation_command='test -d {out} && test -d {iteration}',
		validation_measures='x')
	result = autoopt.load_result(search, 0)
	suite.equal('runs and tests take a path with a space as one word',
		(result['status'], (result['tests'] or {}).get('pass')),
		('ok', True))

	call([AUTOOPT, 'validate', search, '--approve'])
	call([AUTOOPT, 'validate', search])
	suite.equal('and so does the external validation',
		autoopt.load_validation(search, 1)['exit_code'], 0)

	sides = {name: os.path.join(base, 'side ' + name) for name in 'ab'}
	for directory in sides.values():
		os.makedirs(directory)
	probe = os.path.join(base, 'probe.py')
	with open(probe, 'w') as out:
		out.write('import os, sys\nassert len(sys.argv) == 2 and '
			'os.path.isdir(sys.argv[1]), sys.argv\nprint("seconds", 1.0)\n')
	out = call([os.path.join(SCRIPTS, 'paired.py'), '--a', sides['a'], '--b',
		sides['b'], '--min-pairs', '1', '--max-pairs', '1', '--json',
		'--cmd', 'python3 "{}" {{src}}'.format(probe)], expect=(0, 1))
	suite.check('paired.py passes {src} as one word', '"pairs": 1' in out,
		out[-300:])
	out = call([os.path.join(SCRIPTS, 'cold.py'), '--side', 'a=' + sides['a'],
		'--reps', '1', '--scratch', os.path.join(base, 'cold scratch'),
		'--cmd', 'python3 "{}" {{src}}'.format(probe)], expect=(0, 1))
	suite.check('cold.py passes {src} as one word', 'median' in out,
		out[-300:])

	root = os.path.join(base, 'literal')
	os.makedirs(root)
	out = call([AUTOOPT, 'init', root, '--task', 'test', '--command',
		'NUMBA_CACHE_DIR=${HOME}/x python3 bench.py --seed {seed} --out '
		'{out}', '--objective', 'score', '--direction', 'max', '--no-gpu'],
		expect=(0, 1))
	suite.check('init refuses a ${VAR} the substitution would choke on',
		'${HOME}' in out and 'Traceback' not in out and not os.path.exists(
			os.path.join(root, 'autooptimize', 'config.json')), out[-300:])


def json_files(suite, base):
	"""Records are replaced whole or not at all, and one that is corrupt
	stops the command that reads it, naming the file."""

	search = make_search(os.path.join(base, 'json'))
	meta = os.path.join(autoopt.iteration_dir(search, 0), 'meta.json')
	before = open(meta).read()
	try:
		autoopt.write_meta(search, 0, {'id': 0, 'broken': object()})
	except TypeError:
		pass
	suite.equal('a write that fails halfway leaves the old record whole',
		open(meta).read(), before)
	suite.equal('and no temporary file behind',
		[name for name in os.listdir(os.path.dirname(meta))
			if name.endswith('.tmp')], [])

	state = autoopt.state_path(search)
	saved = open(state).read()
	with open(state, 'w') as out:
		out.write(saved[:len(saved) // 2])
	out = call([AUTOOPT, 'status', search], expect=(0, 1, 2))
	suite.check('a corrupt state.json is refused, naming it, not reset',
		'state.json' in out and 'Traceback' not in out
		and open(state).read() == saved[:len(saved) // 2], out[-300:])
	with open(state, 'w') as out:
		out.write(saved)

	add(search, 1, 0.60)
	os.makedirs(os.path.join(search, 'validations'))
	with open(autoopt.validation_path(search, 1), 'w') as out:
		out.write('{"round": 1,')
	out = call([AUTOOPT, 'summary', search, '--round', '1'],
		expect=(0, 1, 2))
	suite.check('a corrupt validation record is named, not a traceback',
		'round-1.json' in out and 'Traceback' not in out, out[-300:])
	os.remove(autoopt.validation_path(search, 1))

	out = call([AUTOOPT, 'init', os.path.join(base, 'json-gates'), '--task',
		't', '--command', 'python3 b.py --seed {seed} --out {out}',
		'--objective', 's', '--direction', 'max', '--gates', '[{"name": '],
		expect=(0, 1, 2))
	suite.check('init names a --gates that is not JSON',
		'--gates' in out and 'Traceback' not in out, out[-300:])

	tested = make_search(os.path.join(base, 'json-tests'), runs=1,
		confirm_runs=1, tests='true')
	cached = os.path.join(autoopt.iteration_dir(tested, 0), 'unit-tests',
		'result.json')
	with open(cached, 'w') as out:
		out.write('{"pass": tr')
	out = call([RUNNER, autoopt.iteration_dir(tested, 0)], expect=(0, 1, 2))
	suite.check('a corrupt unit-test record is named and the tests re-run',
		'not valid JSON' in out and autoopt.load_result(tested, 0)['tests'][
			'pass'] is True, out[-300:])


FIXED_INPUT_BENCH = """\
import os, json, argparse
parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()
counter = {counter!r}
n = int(open(counter).read()) if os.path.exists(counter) else 0
open(counter, 'w').write(str(n + 1))
os.makedirs(args.out, exist_ok=True)
json.dump({{'objective': 1.0 + 0.001 * n, 'gates': {{'cost': 1.0}},
	'observers': {{'size': 100}}}}, open(os.path.join(args.out,
		'metrics.json'), 'w'))
"""


def small_fixes(suite, base):
	"""One check each for the smaller interface promises."""

	seeding = os.path.join(SCRIPTS, 'check_seeding.py')

	# check_seeding.py hands its runs a real {cache}, and its exit codes
	# keep a refusal apart from a verdict.
	search = make_search(os.path.join(base, 'seedcheck'), runs=1,
		confirm_runs=1, command='python3 bench.py --seed {seed} --out {out} '
		'&& test -d {cache}')
	done = subprocess.run([sys.executable, seeding,
		autoopt.iteration_dir(search, 0)], capture_output=True, text=True)
	suite.equal('check_seeding.py gives its runs a {cache} directory',
		done.returncode, 0)
	done = subprocess.run([sys.executable, seeding, os.path.join(base,
		'no-such-dir')], capture_output=True, text=True)
	suite.equal('and a refusal exits 3, apart from WARNING (1) and FAIL (2)',
		done.returncode, 3)
	# A fixed-input benchmark: the seed changes nothing, and the value
	# drifts by run like timing noise. Its verdict is about machine noise,
	# and the output has to say so rather than send anyone after a source.
	search = make_search(os.path.join(base, 'seedcheck-fixed'), runs=1,
		confirm_runs=1)
	fixed = autoopt.iteration_dir(search, 0)
	with open(os.path.join(fixed, 'bench.py'), 'w') as out:
		out.write(FIXED_INPUT_BENCH.format(counter=os.path.join(fixed,
			'calls')))
	for flags, code in ((['--repeats', '3', '--others', '1'], 1),
		(['--repeats', '2', '--others', '5'], 0)):
		done = subprocess.run([sys.executable, seeding, fixed] + flags,
			capture_output=True, text=True)
		suite.check('check_seeding.py exit {} carries the fixed-input caveat '
			'and sends no one after an unseeded source unconditionally'.format(
				code), done.returncode == code and 'fixed input' in done.stdout
			and 'Find the unseeded source' not in done.stdout,
			done.stdout[-700:] + done.stderr[-300:])

	done = subprocess.run([sys.executable, seeding, '--help'],
		capture_output=True, text=True)
	suite.check('its --help lists the exit codes',
		all(word in done.stdout for word in ('WARNING', 'FAIL', '3')),
		done.stdout[-400:])

	# reset deletes the branch its meta.json names, so it reads it first.
	search = make_search(os.path.join(base, 'reset-branch'))
	add(search, 1, 0.40)
	bare = os.path.join(search, 'repo.git')
	git('-C', bare, 'branch', 'autoopt/custom', 'autoopt/1')
	meta = autoopt.load_meta(search, 1)
	meta['branch'] = 'autoopt/custom'
	autoopt.write_meta(search, 1, meta)
	call([AUTOOPT, 'reset', search, '--keep', '0', '--apply'])
	suite.check('reset deletes the branch meta.json names',
		'autoopt/custom' not in git('-C', bare, 'branch'), git('-C', bare,
			'branch'))

	# A process that is not a dashboard writer, with a pidfile naming it.
	monitor = os.path.join(search, 'monitor')
	os.makedirs(monitor, exist_ok=True)
	impostor = subprocess.Popen(['sleep', '30'])
	try:
		with open(os.path.join(monitor, 'writer.pid'), 'w') as out:
			out.write('{}\n{}\n'.format(impostor.pid, search))
		suite.equal('a pidfile whose pid is not monitor_writer.py is no '
			'publisher', autoopt.live_publishers(search), [])
	finally:
		impostor.kill()
		impostor.wait()

	# The original's index is not rewritten by the copy's status check.
	original = os.path.join(base, 'locks-original')
	make_original(original)
	future = time.time() + 100
	os.utime(os.path.join(original, 'README.md'), (future, future))
	index = os.path.join(original, '.git', 'index')
	before = (os.stat(index).st_mtime_ns, os.stat(index).st_ino)
	harness = os.path.join(base, 'locks-bench.py')
	with open(harness, 'w') as out:
		out.write(HARNESS)
	call([AUTOOPT, 'init', os.path.join(base, 'locks-search'), '--task', 't',
		'--repo', original, '--copy', harness, '--command',
		'python3 x.py --src {src} --seed {seed} --out {out}', '--objective',
		's', '--direction', 'max', '--no-gpu'])
	suite.equal('init --repo leaves the original\'s index untouched',
		(os.stat(index).st_mtime_ns, os.stat(index).st_ino), before)

	done = subprocess.run([sys.executable, os.path.join(HERE,
		'test_runner.py'), '--help'], capture_output=True, text=True,
		timeout=30)
	suite.check('test_runner.py --help prints usage and runs nothing',
		done.returncode == 0 and 'usage' in done.stdout
		and 'passed' not in done.stdout, done.stdout[-300:])

	text = subprocess.run([sys.executable, AUTOOPT, 'init', '--help'],
		capture_output=True, text=True).stdout
	start = text.index('\n  --tests TESTS')
	tests_help = text[start:text.index('--tests-path', start)]
	suite.check('init --tests help names every placeholder',
		all(name in tests_help for name in ('{src}', '{iteration}', '{out}',
			'{cache}', '{selected}')), tests_help)


def unlisted_publisher(suite, base):
	"""A writer whose pidfile is gone still publishes: status must see it,
	or it reports none running and the next phase starts a second one."""

	search = make_search(os.path.join(base, 'unlisted-publisher'))
	other = make_search(os.path.join(base, 'unlisted-publisher-other'))

	# Stand-ins named monitor_writer.py, launched the way start_writer
	# launches the real one, with no writer.pid anywhere.
	fake = os.path.join(base, 'unlisted-writer')
	os.makedirs(fake, exist_ok=True)
	script = os.path.join(fake, 'monitor_writer.py')
	with open(script, 'w') as out:
		out.write('import time\ntime.sleep(60)\n')

	ours = subprocess.Popen([sys.executable, script, search, '--out',
		os.path.join(base, 'unlisted-out')])
	theirs = subprocess.Popen([sys.executable, script, other, '--out',
		os.path.join(base, 'unlisted-out-other')])
	relative = subprocess.Popen([sys.executable, 'monitor_writer.py',
		os.path.relpath(os.path.dirname(search), fake), '--out', 'out'],
		cwd=fake)

	try:
		time.sleep(0.5)
		found = sorted(entry['pid'] for entry in autoopt.live_publishers(search))
		suite.equal('a writer without a pidfile is found by process, by '
			'absolute and relative paths, and only for its own search', found,
			sorted([ours.pid, relative.pid]))
	finally:
		for process in (ours, theirs, relative):
			process.kill()
			process.wait()


def one_publisher(suite, base):
	"""A second `autoopt_monitor.py --start` for a search leaves one
	publisher, wherever the first one writes."""

	monitor = os.path.join(SCRIPTS, 'dashboard', 'autoopt_monitor.py')
	search = make_search(os.path.join(base, 'one-publisher'))
	home = os.path.join(base, 'one-publisher-home')
	first, other = (os.path.join(base, name) for name in ('publish-a',
		'publish-b'))
	# The default directory and the writer index follow the home directory
	# and the Jupyter servers; a private pair keeps both under base.
	env = dict(os.environ, HOME=home, JUPYTER_RUNTIME_DIR=os.path.join(home,
		'runtime'))
	os.makedirs(env['JUPYTER_RUNTIME_DIR'])

	def monitor_cli(*args):
		done = subprocess.run([sys.executable, monitor, search] + list(args),
			capture_output=True, text=True, env=env, timeout=180)
		return done.stdout + done.stderr

	def listed():
		return sorted(line.split('\t')[2] for line in subprocess.run(
			[sys.executable, monitor, '--status', search], capture_output=True,
			text=True, env=env, timeout=60).stdout.splitlines() if '\t' in line)

	try:
		monitor_cli('--start', '--out', first, '--interval', '60')
		out = monitor_cli('--start', '--interval', '60')
		suite.check('a second --start reports the search\'s publisher already '
			'running and starts none beside it', 'already running' in out
			and listed() == [first], (out[-400:], listed()))
		out = monitor_cli('--start', '--out', other, '--interval', '60')
		suite.check('--start --out another directory moves the publisher '
			'there', listed() == [other], (out[-400:], listed()))
	finally:
		monitor_cli('--stop')


def abandoned_ids(suite, base):
	"""An iteration moved aside to `<id>.abandoned` keeps its branch, so the
	next `new` must not hand out its id again."""

	search = make_search(os.path.join(base, 'abandon'))
	call([AUTOOPT, 'new', search, '--title', 'empty fork', '--category',
		'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	os.rename(directory, directory + '.abandoned')
	git('-C', os.path.join(search, 'repo.git'), 'worktree', 'prune')

	os.makedirs(os.path.join(search, 'briefs'), exist_ok=True)
	open(os.path.join(search, 'briefs', '1.md'), 'w').close()
	suite.check('the brief of an iteration moved aside is not pending',
		1 not in autoopt.pending_briefs(search), autoopt.pending_briefs(search))

	out = call([AUTOOPT, 'new', search, '--title', 'next', '--category',
		'MISC'], expect=(0, 1))
	suite.check('new after an abandoned fork takes a fresh id',
		out.strip().splitlines()[-1] == autoopt.iteration_dir(search, 2)
		and 'Traceback' not in out, out[-300:])

	out = call([AUTOOPT, 'new', search, '--id', '1', '--title', 'reuse',
		'--category', 'MISC'], expect=(0, 1))
	suite.check('and an explicit --id whose branch exists fails plainly',
		'autoopt/1' in out and 'Traceback' not in out
		and not os.path.exists(directory), out[-300:])


def off_root_promote(suite, base):
	"""`promote` takes only an iteration that descends from the root."""

	search = make_search(os.path.join(base, 'off-root'))

	add(search, 1, 0.60)
	call([AUTOOPT, 'promote', search, '--id', '1'])
	call([AUTOOPT, 'new', search, '--id', '2', '--parent', '0', '--title',
		'side', '--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 2), 0.70)
	call([RUNNER, autoopt.iteration_dir(search, 2)])
	out = call([AUTOOPT, 'promote', search, '--id', '2'], expect=(1,))
	suite.check('promote refuses an iteration that does not descend from the '
		'root and says to fork from the current root',
		'Fork from the current root' in out, out[-300:])


def suite_squashed(suite, base):
	"""`init --repo` on the unit-test suite's branch wants it as one commit."""

	original = os.path.join(base, 'squash-original')
	make_original(original)
	clone = os.path.join(base, 'squash-clone')
	git('clone', '-q', original, clone)
	git('-C', clone, 'checkout', '-q', '-b', 'auto-optimize/2026-10-30-unit-tests')
	identity = ('-c', 'user.name=t', '-c', 'user.email=t@t')

	for number in (1, 2):
		with open(os.path.join(clone, 'tests', 'test_extra{}.py'.format(
				number)), 'w') as out:
			out.write('def test_{}():\n\tassert True\n'.format(number))
		git('-C', clone, 'add', '-A')
		git('-C', clone, *identity, 'commit', '-q', '-m', 'tests {}'.format(
			number))

	root = os.path.join(base, 'squash-search')
	args = [AUTOOPT, 'init', root, '--task', 'test', '--repo', clone,
		'--command', 'python3 bench.py --seed {seed} --out {out}',
		'--objective', 'score', '--direction', 'max', '--no-gpu',
		'--promote', 'orchestrator']
	out = call(args, expect=(1,))
	suite.check('two commits on the suite\'s branch are refused, with the '
		'squash command', '2 commits ahead of its base' in out
		and 'reset --soft' in out and not os.path.exists(root), out[-400:])

	git('-C', clone, 'reset', '--soft', 'refs/remotes/origin/HEAD')
	git('-C', clone, *identity, 'commit', '-q', '-m', 'Add unit tests')
	autoopt.check_suite_squashed(clone, 'auto-optimize/2026-10-30-unit-tests')
	suite.check('one commit passes', True)

	git('-C', clone, 'checkout', '-q', '-b', 'feature')
	with open(os.path.join(clone, 'extra.txt'), 'w') as out:
		out.write('x\n')
	git('-C', clone, 'add', '-A')
	git('-C', clone, *identity, 'commit', '-q', '-m', 'another')
	autoopt.check_suite_squashed(clone, 'feature')
	suite.check('a branch that is not the suite\'s is not counted', True)


def leftover_controls(suite, base):
	"""A same-code control passes, and changes nothing: it never holds the
	frontier and is never promoted."""

	search = make_search(os.path.join(base, 'controls'))
	add(search, 1, 0.60)
	call([AUTOOPT, 'promote', search, '--id', '1'])
	call([AUTOOPT, 'new', search, '--id', '2', '--parent', '1', '--title',
		'control', '--category', 'MISC'])
	call([RUNNER, autoopt.iteration_dir(search, 2)])
	add(search, 3, 0.65)

	# A control that reads better than everything, as a quieter machine
	# makes unchanged code do: it is still not the frontier.
	call([AUTOOPT, 'new', search, '--id', '4', '--title', 'control: root',
		'--category', 'MISC', '--control'])
	bench(autoopt.iteration_dir(search, 4), 0.90)
	out = call([RUNNER, autoopt.iteration_dir(search, 4)])
	state = autoopt.load_state(search)
	value, holder = autoopt.search_frontier(search, autoopt.all_results(search),
		'max')
	suite.check('a control that reads best never holds the frontier',
		holder == 3 and 'NEW FRONTIER' not in out and 'a control never holds '
		'the frontier' in out, (holder, out[-400:]))
	out = call([AUTOOPT, 'promote', search, '--id', '4'], expect=(1,))
	suite.check('and promote refuses it', 'same-code control' in out,
		out[-300:])
	with open(os.path.join(autoopt.iteration_dir(search, 4), 'result.json')) as infile:
		old = json.load(infile)
	old.pop('control')
	autoopt.write_json(os.path.join(autoopt.iteration_dir(search, 4),
		'result.json'), old)
	suite.equal('a control whose result.json has no control flag is '
		'kept off the frontier too', autoopt.search_frontier(search, autoopt.all_results(
			search), 'max')[1], 3)


SPEC_FOR_BRIEF = """# brief test

## The interview

- Asked on: 2026-10-11, in the request
- Kind of search: algorithm implementation; plain mode
- New unit tests: no

## Rules this search runs by

- Objective: score, maximized
- Gates: cost <= 1.0. Reference each compares against: none
- Gate proof: cost reads 0.5 unchanged and 2.0 on a lossy change
- Keep rule: a margin of 0.01 over the frontier,
  or a paired "B better"
- Off limits: no new dependencies
- Portability: this machine's OS only (Linux); /proc may be read
- Width measured: four runs at once stay inside the band

## Goal and scope

Make score larger.

## Rules added during the search

Each with the date and the user's words, written before it is used.

- 2026-10-11: compile time breaks a runtime tie. The user: "yes, that counts"
"""


def reporting(suite, base):
	"""The files the search writes for people and agents to read: the
	skeletons init starts, briefs, READMEs, summaries, and the
	config changes recorded in spec.md."""

	# init's skeletons.
	search = make_search(os.path.join(base, 'reporting'), runs=2,
		confirm_runs=3)
	suite.check('init creates reference/, ideas.md, knowledge.md and the '
		'tools READMEs', os.path.isdir(os.path.join(search, 'reference'))
		and all(os.path.exists(os.path.join(search, name)) for name in
			('ideas.md', 'knowledge.md', os.path.join('tools', 'README.md'),
				os.path.join('tools', 'checkers', 'README.md'))))
	ledger = open(os.path.join(search, 'knowledge.md')).read()
	suite.check('the ledger header gives the command that appends under its '
		'lock', 'autoopt.py ledger $WORK --by' in ledger, ledger[:500])

	with open(os.path.join(search, 'spec.md'), 'w') as out:
		out.write(SPEC_FOR_BRIEF)

	add(search, 1, 0.55)
	add(search, 2, 0.45)
	out = call([AUTOOPT, 'status', search])
	suite.check('status names the next free id', 'next free id: A3' in out,
		out[:500])
	brief = [AUTOOPT, 'brief', search, '--title', 'third idea', '--category',
		'OPTIMIZATION', '--idea', 'Try a third thing in bench.py.', '--tried',
		'none']
	call([AUTOOPT, 'new', search, '--id', '3', '--title', 'control: root',
		'--category', 'MISC', '--control'])

	# The brief: everything the search records, filled in.
	out = call(brief + ['--id', '3'], expect=(1,))
	suite.check('brief refuses a taken id', 'is taken' in out, out[-200:])
	done = subprocess.run([sys.executable] + brief + ['--id', 'A4',
		'--predict', '0.02', '--predict-basis', 'A1 measured +0.05'],
		capture_output=True, text=True)
	written = open(os.path.join(search, 'briefs', '4.md')).read()
	suite.check('brief writes the brief to briefs/<id>.md and prints only the '
		'short prompt that has the subagent read it', done.returncode == 0
		and '`{}`: your brief'.format(os.path.join(search, 'briefs', '4.md'))
		in done.stdout and 'references/subagent.md' in done.stdout
		and 'knowledge.md' in done.stdout and '--brief 4`' in done.stdout
		and '## The idea' not in done.stdout and '## The idea' in written
		and len(done.stdout) < 2000, done.stdout)
	suite.check('with the Agent call\'s description and model on stderr',
		done.stderr == 'spawn with the Agent tool: description "A4: third '
		'idea", model opus\n', done.stderr)
	suite.check('the brief names no glossary to read and no new command',
		'glossary' not in written and 'autoopt.py new' not in written,
		written[:600])
	again = call(brief + ['--id', '4'], expect=(1,))
	suite.check('a pending brief reserves its id', 'not yet started' in again
		and 'next free id is A5' in again, again[-300:])
	suite.check('and status counts it', 'next free id: A5' in call([AUTOOPT,
		'status', search]))
	out = call(brief + ['--id', 'A4', '--replace', '--predict', '0.02',
		'--predict-basis', 'A1 measured +0.05'], expect=(0,))
	suite.check('the keep rule and the added rules are quoted from spec.md',
		'Keep rule: a margin of 0.01 over the frontier, or a paired "B '
		'better"' in out and '2026-10-11: compile time breaks a runtime tie. '
		'The user: "yes, that counts"' in out and 'Off limits: no new '
		'dependencies' in out and 'Portability: this machine\'s OS only '
		'(Linux); /proc may be read' in out, out)
	suite.check('the brief quotes the thread counts and names spec.md',
		'Thread counts timed, tracking inputs:' in out
		and os.path.join(search, 'spec.md') in out, out)
	suite.check('and no numba note for code that does not import numba',
		'The code uses numba' not in out, out)
	suite.check('the brief carries DIR, SRC, the frontier and the gates',
		'`{}`'.format(autoopt.iteration_dir(search, 4)) in out
		and 'Root: A0' in out and '`cost <= 1.0`' in out
		and 'needs no GPU' in out, out)
	refused = call([AUTOOPT, 'new', search, '--brief', '4', '--title', 'x'],
		expect=(1,))
	suite.check('new --brief takes no other flag', 'drop --title' in refused,
		refused[-300:])
	made = subprocess.run([sys.executable, AUTOOPT, 'new', search, '--brief',
		'4'], capture_output=True, text=True)
	suite.check('new --brief creates exactly the iteration the brief describes',
		made.returncode == 0 and made.stdout.strip()
		== autoopt.iteration_dir(search, 4)
		and (autoopt.load_meta(search, 4).get('prediction') or {}).get(
			'delta') == 0.02 and autoopt.load_meta(search, 4)['title']
		== 'third idea',
		made.stdout + made.stderr)
	again = call([AUTOOPT, 'brief', search, '--prompt', 'A4'], expect=(0,))
	suite.check('brief --prompt of a created iteration says not to run new',
		'It exists already: do not run `new`' in again and '--brief 4`'
		not in again.split('\n#')[0], again[:900])
	bench(autoopt.iteration_dir(search, 4), 0.20)
	call([RUNNER, autoopt.iteration_dir(search, 4)])
	gpu_root = os.path.join(base, 'reporting-gpu')
	os.makedirs(gpu_root)
	call([AUTOOPT, 'init', gpu_root, '--task', 't', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max'])
	out = call([AUTOOPT, 'brief', gpu_root, '--id', '1', '--title', 'x',
		'--category', 'MISC', '--idea', 'x', '--tried', 'none'], expect=(1,))
	suite.check('a GPU search\'s brief needs --gpus', '--gpus' in out,
		out[-200:])

	# README.md from notes.md and result.json.
	directory = autoopt.iteration_dir(search, 1)
	out = call([AUTOOPT, 'readme', search, '--id', '1'], expect=(1,))
	suite.check('readme without notes.md points at the sections to write',
		'references/subagent.md, *The README*' in out, out[-200:])
	call([AUTOOPT, 'readme', search, '--id', '1', '--template'])
	out = call([AUTOOPT, 'readme', search, '--id', '1'], expect=(1,))
	suite.check('readme refuses notes that still hold a placeholder',
		'placeholder' in out, out[-200:])
	notes = os.path.join(directory, 'notes.md')
	filled = re.sub(r'<<FILL:[^>]*>>', 'written', open(notes).read())
	with open(notes, 'w') as out:
		out.write(filled.replace('## What to try next', '## Next'))
	out = call([AUTOOPT, 'readme', search, '--id', '1'], expect=(1,))
	suite.check('and notes missing a required section',
		'`## What to try next`' in out, out[-200:])
	with open(notes, 'w') as out:
		out.write(filled + '\n## Paired timing\n\npaired: B better, 9/9\n')
	call([AUTOOPT, 'readme', search, '--id', '1'])
	text = open(os.path.join(directory, 'README.md')).read()
	result = autoopt.load_result(search, 1)
	rows = [line for line in text.splitlines() if re.match(r'\| \d+ \|', line)]
	suite.check('the README numbers come from result.json, one row per run',
		text.startswith('# A1: iteration 1\n') and len(rows)
		== result['n_runs'] and '**{}**'.format(autoopt.fmt(
			result['objective']['median'], 6)) in text
		and '- **Status:** ok' in text, text[:900])
	suite.check('the notes sections follow the numbers, extras kept',
		text.index('## What happened') < text.index('## Numbers')
		< text.index('## Notes and failures') < text.index('## Paired timing'),
		text)
	out = call([AUTOOPT, 'status', search])
	suite.check('a README written by readme ends the finishing state',
		'A1 finishing' not in out, out[:600])
	bench(directory, 0.80)
	call([RUNNER, directory, '--rerun'])
	text = open(os.path.join(directory, 'README.md')).read()
	suite.check('a re-run re-assembles the README with the new numbers',
		'**{}**'.format(autoopt.fmt(autoopt.load_result(search, 1)
			['objective']['median'], 6)) in text, text[:600])
	suite.equal('notes.md is never committed in plain mode',
		git('-C', directory, 'ls-files', 'notes.md', 'README.md').strip(), '')

	# How *Changes* may name a file, and what counts as a label.
	suite.equal('labels are matched as whole words, and only the search\'s own',
		autoopt.labels_named(search, 'A1, A2; A549 cells, BA1, xA1, A1x, '
			'0xA1, A1'), ['A1', 'A2'])
	suite.equal('a label hyphenated onto another word is part of a name, '
		'unless that word is a label too',
		autoopt.labels_named(search, 'cells AZ-A1 and HG002-A2'), [])
	suite.equal('a range of labels and a label leading a compound are labels',
		autoopt.labels_named(search, 'A1-A2 and the A2-based change'),
		['A1', 'A2'])

	for text, why in (('Speed up scoring\n\nScore with less work.\n', None),
			('x' * 73 + '\n', 'characters'), ('Speed up scoring.\n',
				'period'), ('Speed up scoring\nno blank line\n', 'blank'),
			('Speed up scoring\n\nA1 made it faster.\n', 'label(s) A1'),
			('\n\nbody\n', 'empty')):
		problems = autoopt.message_problems(search, text)
		suite.check('a commit message {}'.format('passes' if why is None
			else 'is refused for: ' + why), problems == [] if why is None
			else any(why in p for p in problems), problems)
	# A config change, recorded where every brief reads the rules.
	out = call([AUTOOPT, 'config', search, '--set', 'loop_handback=7'],
		expect=(1,))
	suite.check('config refuses a change without --reason', '--reason' in out,
		out[-200:])
	out = call([AUTOOPT, 'config', search, '--set', 'command=x', '--reason',
		'r'], expect=(1,))
	suite.check('and the command, which would be a new search',
		'new search' in out, out[-300:])
	out = call([AUTOOPT, 'config', search, '--set', 'gpus_per_agent=0',
		'--reason', 'r'], expect=(1,))
	suite.check('a value check_config rejects leaves config.json alone',
		autoopt.load_config(search)['gpus_per_agent'] == 1, out[-200:])
	out = call([AUTOOPT, 'config', search, '--set', 'gpus_per_agent=2',
		'--set', 'gates.cost.threshold=1.5', '--add-observer', 'rss',
		'--reason', 'the user asked for two GPUs'])
	config = autoopt.load_config(search)
	spec = open(os.path.join(search, 'spec.md')).read()
	suite.check('config writes the change and appends a dated rule to spec.md',
		config['gpus_per_agent'] == 2 and config['gates'][0]['threshold'] == 1.5
		and config['observers'][-1]['name'] == 'rss'
		and spec.rstrip().splitlines()[-1].startswith('- {}: config '
			'gpus_per_agent'.format(time.strftime('%Y-%m-%d')))
		and 'the user asked for two GPUs' in spec, spec[-400:])
	suite.check('a gate change says to re-run the baseline',
		'--rerun' in out, out)
	out = call([AUTOOPT, 'config', search, '--set', 'subagent_model=gpt',
		'--reason', 'test'], expect=(1,))
	suite.check('an unknown model is refused', 'subagent_model is one of' in out,
		out[-200:])
	call([AUTOOPT, 'config', search, '--set', 'subagent_model=sonnet',
		'--reason', 'test'])
	suite.check('status shows the subagent model', 'subagent model: sonnet' in
		call([AUTOOPT, 'status', search, '--brief']))

	# The round summary's three sections, assembled from the records.
	out = call([AUTOOPT, 'summary', search, '--round', '1'])
	sections = autoopt.markdown_sections(out)
	suite.check('summary assembles the three sections from the records, with '
		'no placeholder', all(heading in sections for heading in
			autoopt.SUMMARY_PROSE) and '<<FILL' not in out
		and '**A1**' in sections['What landed and why']
		and ('untried ideas' in sections['Next round']
			or 'idea miner' in sections['Next round']), out[-900:])

	# Measured, no README, and quiet for over an hour.
	stale = autoopt.iteration_dir(search, 2)
	old = time.time() - 7200
	for base_dir, _, names in os.walk(stale):
		for name in names:
			os.utime(os.path.join(base_dir, name), (old, old))
	out = call([AUTOOPT, 'status', search])
	suite.check('status names a measured iteration whose README was never '
		'written', any(line.startswith('no README:') and 'A2' in line.split(
			'.')[0] for line in out.splitlines()), out[:800])

	call(brief + ['--id', '9'])
	with open(os.path.join(search, 'knowledge.md'), 'a') as out:
		out.write('- [closed] the generator form loses at n=1 (A4, root A0).\n')
	os.makedirs(os.path.join(search, 'validations'), exist_ok=True)
	with open(os.path.join(search, 'validations', 'round-1.json'), 'w') as out:
		out.write('{}')
	os.makedirs(os.path.join(search, 'iterations', '7.abandoned'))
	report = call([AUTOOPT, 'reset', search, '--keep', '0,1', '--force'])
	suite.check('reset lists the ledger\'s references to removed iterations',
		'knowledge.md mentions' in report and '4' in report.split(
			'knowledge.md mentions')[1].splitlines()[0], report)
	out = call([AUTOOPT, 'reset', search, '--keep', '0,1', '--force', '--apply'])
	suite.check('reset removes the validation records',
		not os.listdir(os.path.join(search, 'validations')))
	suite.check('and names a next id past a directory set aside',
		'next `new` takes id 8' in out, out[-300:])
	suite.check('reset removes the briefs of the iterations it removes, so '
		'their ids are free again', sorted(os.listdir(os.path.join(search,
			'briefs'))) == [] and 'next free id: A8' in call([AUTOOPT,
				'status', search]), os.listdir(os.path.join(search, 'briefs')))

	call([AUTOOPT, 'prune', search, '--all', '--worktrees', '--apply'])
	suite.check('prune --worktrees keeps notes.md with the README',
		not os.path.exists(os.path.join(directory, '.git'))
		and os.path.exists(os.path.join(directory, 'notes.md'))
		and os.path.exists(os.path.join(directory, 'README.md')))


def tree_rules(suite, base):
	"""promote keeps the root's change; brief reads numba from the branch."""

	search = make_search(os.path.join(base, 'tree-rules'), runs=2, confirm_runs=3)
	add(search, 1, 0.70)
	add(search, 2, 0.65)                      # a sibling of 1, forked from A0
	call([AUTOOPT, 'promote', search, '--id', '1'])
	out = call([AUTOOPT, 'promote', search, '--id', '2'], expect=(1,))
	suite.check('promote refuses an iteration that does not descend from '
		'the root', 'does not descend from the root A1' in out
		and autoopt.load_state(search)['root'] == 1, out[-300:])

	directory = autoopt.iteration_dir(search, 1)
	with open(os.path.join(directory, 'helper.py'), 'w') as out:
		out.write('import os\nimport numpy, numba as nb\n')
	git('-C', directory, 'add', 'helper.py')
	git('-C', directory, '-c', 'user.name=t', '-c', 'user.email=t@t', 'commit',
		'-qm', 'helper')
	out = call([AUTOOPT, 'brief', search, '--id', '3', '--title', 'x',
		'--category', 'MISC', '--idea', 'x', '--tried', 'none'])
	suite.check('brief adds the numba note when the parent\'s branch imports '
		'numba', 'The code uses numba' in out, out[-600:])
	suite.check('and the note names the section to read', 'algorithm.md' in
		out and '*Numba*' in out, out[-600:])


def phase_band(suite, base):
	"""The band's reference spread is the root's, re-measured when a phase
	opens; before that it is the baseline's."""

	search = make_search(os.path.join(base, 'phase-band'), runs=3,
		confirm_runs=4)
	wide = autoopt.load_result(search, 0)['noise_band']

	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'quiet and better',
		'--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 1), 0.70, noise=0.0001)
	call([RUNNER, autoopt.iteration_dir(search, 1), '--stage', 'confirm'])
	inherited = autoopt.load_result(search, 1)['noise_band']
	call([AUTOOPT, 'promote', search, '--id', '1'])

	out = call([AUTOOPT, 'loop', search])
	state = autoopt.load_state(search)
	suite.check('opening a phase re-measures the band from the root',
		state['band']['id'] == 1 and state['band']['spread'] < wide / 10
		and 'Noise band: its reference spread was re-measured' in out,
		(state.get('band'), wide))
	call([AUTOOPT, 'new', search, '--id', '2', '--title', 'next',
		'--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 2), 0.71, noise=0.0001)
	call([RUNNER, autoopt.iteration_dir(search, 2)])
	band = autoopt.load_result(search, 2)['noise_band']

	suite.check('before the first phase the band carries the baseline\'s '
		'spread', inherited >= wide * 0.99, (inherited, wide))
	suite.check('in a phase it judges against the root\'s spread',
		band < wide / 10, (band, wide))


NOTES = """## The idea

Made it better.

## What happened

It helped.

## Notes and failures

None.

## What to try next

Closed.
"""

# A checker as a subagent writes one: two trees, exit 1 on a difference.
CHECKER = """import sys
a, b = (open(path + '/bench.py').read() for path in sys.argv[1:3])
sys.exit(0 if ('QUALITY' in a and 'QUALITY' in b) else 1)
"""


def finishing(suite, base):
	"""`finish`: the README, a checker registered and ledger entries in one
	call, everything checked before anything is written."""

	search = make_search(os.path.join(base, 'finishing'))
	ledger_path = os.path.join(search, 'knowledge.md')
	with open(ledger_path, 'a') as handle:
		handle.write('- [code:core] f is called twice per run (A0, root A0)\n')
	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'one',
		'--category', 'MICRO'])
	directory = autoopt.iteration_dir(search, 1)
	bench(directory, 0.6)
	call([RUNNER, directory])
	probe = os.path.join(directory, 'probe')
	os.makedirs(os.path.join(probe, 'twin'))
	with open(os.path.join(probe, 'twin', 'twin.py'), 'w') as handle:
		handle.write(CHECKER)
	row = os.path.join(probe, 'checker.txt')
	with open(row, 'w') as handle:
		handle.write('name: code:core\nfiles: bench.py\ncompares: bench.py of '
			'two trees | by text\nbuilt: plain Python\nrun: python3 '
			'{checker}/twin.py {parent} {child}\nfaults: none planted\ninputs: '
			'in the script\n')
	entries = os.path.join(probe, 'ledger.txt')
	with open(entries, 'w') as handle:
		handle.write('- [code:core] f is called once per run (A1, root A0) '
			'(corrects: called twice per run)\n- [harness] a new fact (A1, '
			'root A0)\n')
	arguments = [AUTOOPT, 'finish', search, '--id', '1', '--ledger', entries,
		'--checker', os.path.join(probe, 'twin'), '--checker-row', row]
	before = open(ledger_path).read()

	out = call(arguments, expect=(1,))
	suite.check('finish refuses without notes.md, and writes nothing',
		'no {}'.format(os.path.join(directory, 'notes.md')) in out
		and 'Nothing was written' in out and open(ledger_path).read() == before
		and not os.path.exists(os.path.join(directory, 'README.md')), out[-400:])

	with open(os.path.join(directory, 'notes.md'), 'w') as handle:
		handle.write(NOTES)
	with open(os.path.join(probe, 'twin', 'twin.py'), 'w') as handle:
		handle.write(CHECKER.replace("'QUALITY' in b", "'NOWHERE' in b"))
	out = call(arguments, expect=(1,))
	suite.check('a checker that fails on its own iteration is refused, and '
		'nothing is written', 'a checker must pass on the iteration that '
		'wrote it' in out and open(ledger_path).read() == before
		and not os.path.exists(os.path.join(directory, 'README.md'))
		and not os.path.exists(os.path.join(search, 'tools', 'checkers',
			'core-A1')), out[-400:])

	with open(os.path.join(probe, 'twin', 'twin.py'), 'w') as handle:
		handle.write(CHECKER)
	out = call(arguments, expect=(0,))
	text = open(ledger_path).read()
	library = open(os.path.join(search, 'tools', 'checkers',
		'README.md')).read()
	report = open(os.path.join(directory, 'report.txt')).read().strip('\n')
	suite.check('one call writes the README, the ledger and the checker',
		os.path.exists(os.path.join(directory, 'README.md'))
		and '- [harness] a new fact' in text
		and '- ~~[code:core] f is called twice per run (A0, root A0)~~ '
		'corrected by A1' in text and os.path.isfile(os.path.join(search,
			'tools', 'checkers', 'core-A1', 'twin.py')), out[-800:])
	suite.check('the checker\'s line is the index, with its run time measured',
		re.search(r'\n\| core-A1 \| bench\.py \| bench\.py of two trees \\\| by '
			r'text \| \d+ s \|\n', library), library[-500:])
	row_md = open(os.path.join(search, 'tools', 'checkers', 'core-A1',
		'ROW.md')).read()
	suite.check('and its full row, with the run line to fill, is ROW.md',
		'run: python3 {checker}/twin.py {parent} {child}\n' in row_md
		and 'faults: none planted\n' in row_md and re.search(r'\ntime: \d+ s\n',
			row_md), row_md)
	suite.check('and it prints the block to report', out.rstrip('\n')
		.endswith(report) and 'Report this block back' in out, out[-600:])

	call(arguments, expect=(0,))
	suite.check('a second call appends no entry twice and keeps one row',
		open(ledger_path).read() == text and open(os.path.join(search,
			'tools', 'checkers', 'README.md')).read().count('| core-A1 |') == 1)

	with open(entries, 'w') as handle:
		handle.write('- [harness] another (A1, root A0) (corrects: no such '
			'words)\n')
	written = os.stat(os.path.join(directory, 'README.md')).st_mtime_ns
	time.sleep(0.05)
	out = call([AUTOOPT, 'finish', search, '--id', '1', '--ledger', entries],
		expect=(1,))
	suite.check('a correction that matches nothing is refused before the '
		'README is touched', 'matches 0 entries' in out
		and open(ledger_path).read() == text and os.stat(os.path.join(
			directory, 'README.md')).st_mtime_ns == written, out[-300:])


# A benchmark whose gate compares against the baseline's output: the run of
# an --iteration named 0 writes the reference under $AUTOOPT_REF, every other
# run compares with it (references/step2-baseline.md).
PROOF_BENCH = """import os, sys, json, argparse
parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int)
parser.add_argument('--out')
parser.add_argument('--iteration')
args = parser.parse_args()
sys.path.insert(0, args.iteration)
from calc import result
value = result()
reference = os.path.join(os.environ['AUTOOPT_REF'], 'seed-{}.json'.format(
	args.seed))
if os.path.basename(os.path.normpath(args.iteration)) == '0':
	json.dump(value, open(reference, 'w'))
same = 1.0 if json.load(open(reference)) == value else 0.0
os.makedirs(args.out, exist_ok=True)
json.dump({'objective': 1.0, 'gates': {'identical': same}},
	open(os.path.join(args.out, 'metrics.json'), 'w'))
"""

PROOF_COMPARE = """import os, sys, json
src, reference = sys.argv[1:3]
sys.path.insert(0, src)
from calc import result
path = os.path.join(reference, 'result.json')
if os.environ.get('AUTOOPT_WRITE_REFERENCE'):
	json.dump(result(), open(path, 'w'))
	print('1 input written')
	sys.exit(0)
same = json.load(open(path)) == result()
print('1 input identical' if same else '1 of 1 inputs differ')
sys.exit(0 if same else 1)
"""


def setup_tools(suite, base):
	"""Step 2's commands: `prove` (copies of A0 with planted faults, each
	gate and the compare on each), `width` and `spec --set`."""

	staging = os.path.join(base, 'tools-staging')
	os.makedirs(staging)
	for name, text in (('bench.py', PROOF_BENCH), ('calc.py',
			'def result():\n\treturn [1, 2, 3]\n'), ('compare.py', PROOF_COMPARE)):
		with open(os.path.join(staging, name), 'w') as handle:
			handle.write(text)
	root = os.path.join(base, 'tools')
	os.makedirs(root)
	real = os.path.join(root, 'autooptimize', 'reference')
	call([AUTOOPT, 'init', root, '--task', 'test', '--command', 'AUTOOPT_REF={} '
		'python3 bench.py --seed {{seed}} --out {{out}} --iteration '
		'{{iteration}}'.format(real),
		'--objective', 'seconds', '--direction', 'min', '--no-gpu', '--gates',
		json.dumps([{'name': 'identical', 'op': '==', 'threshold': 1}]),
		'--copy', os.path.join(staging, 'bench.py'),
		'--copy', os.path.join(staging, 'calc.py')])
	search = os.path.join(root, 'autooptimize')

	out = call([AUTOOPT, 'prove', search, '--make', 'same,reorder,lossy'],
		expect=(0,))
	folder = autoopt.load_state(search)['setup']['proof']
	suite.equal('the copies live in the fixed folder setup/gateproof, not a '
		'random name', folder, os.path.join(root, 'setup', 'gateproof'))
	suite.check('prove --make copies A0 once per name under the search',
		folder.startswith(root) and all(os.path.isfile(os.path.join(folder,
			name, 'calc.py')) and not os.path.exists(os.path.join(folder, name,
			'.git')) for name in ('same', 'reorder', 'lossy')), out)
	with open(os.path.join(folder, 'lossy', 'calc.py'), 'w') as handle:
		handle.write('def result():\n\treturn [1, 2, 4]\n')
	# A folder the benchmark writes in the scratch reference, not a copy.
	os.makedirs(os.path.join(folder, 'gates'))
	with open(os.path.join(staging, 'time_one.py'), 'w') as handle:
		handle.write('import sys, time\nsys.path.insert(0, sys.argv[1])\n'
			'import calc\nstart = time.time()\nvalue = calc.result()\n'
			'if value != [1, 2, 3]:\n\tsys.exit(1)\n'
			'print(time.time() - start)\n')
	call([AUTOOPT, 'config', search, '--set', 'checks.time=python3 {} '
		'{{src}}'.format(os.path.join(staging, 'time_one.py')), '--reason',
		'test'])

	out = call([AUTOOPT, 'prove', search, '--compare', 'python3 {} {{src}} '
		'{{reference}}'.format(os.path.join(staging, 'compare.py'))],
		expect=(0,))
	rows = {line.split()[0]: line for line in out.splitlines()
		if line.startswith('  ')}
	suite.check('prove runs A0 and each copy: unchanged code passes',
		'identical 1 pass' in rows.get('base', '') and 'identical 1 pass'
		in rows.get('same', '') and 'compare pass: 1 input identical'
		in rows.get('same', ''), out)
	suite.check('the lossy copy fails the gate and the compare',
		'1 file(s) changed' in rows.get('lossy', '') and 'identical 0 FAIL'
		in rows.get('lossy', '') and 'compare differ' in rows.get('lossy', ''),
		out)
	suite.check('and a copy whose planted fault did not land is named',
		'NO FILE CHANGED' in rows.get('reorder', '') and 'NO FILE CHANGED'
		not in rows.get('same', ''), out)
	suite.check('only the copies --make made are run, not a folder the '
		'benchmark wrote', 'gates' not in rows, out)
	suite.check('and the time check runs on each: exit 0 unchanged, '
		'non-zero on a wrong output', 'time exit 0' in rows.get('same', '')
		and 'time exit 1' in rows.get('lossy', ''), out)
	log = os.path.join(search, 'setup', 'prove-compare.log')
	log = open(log).read() if os.path.exists(log) else ''
	suite.check('the proof writes its own log in setup/, ending with its exit',
		log.rstrip().endswith('exit 0') and rows.get('lossy', '').strip()
		in log, log[-300:])

	unchanged = open(os.path.join(folder, 'same', 'calc.py')).read()

	with open(os.path.join(folder, 'same', 'calc.py'), 'w') as handle:
		handle.write('def result():\n\treturn [1, 2, 5]\n')

	# A rewrite of the same size within the second would keep a stale .pyc.
	shutil.rmtree(os.path.join(folder, 'same', '__pycache__'),
		ignore_errors=True)

	out = call([AUTOOPT, 'prove', search, '--compare', 'python3 {} {{src}} '
		'{{reference}}'.format(os.path.join(staging, 'compare.py'))],
		expect=(1,))
	suite.check('an unchanged copy that differs from A0 stops the proof before '
		'any fault copy runs: the base is not the same run to run', '`same` '
		'does not pass against A0' in out and 'same ' in out and 'lossy'
		not in out.split('error:')[0], out)

	with open(os.path.join(folder, 'same', 'calc.py'), 'w') as handle:
		handle.write(unchanged)

	out = call([AUTOOPT, 'width', search, '--counts', '1,2', '--reps', '1',
		'--write'], expect=(0,))
	spec = open(os.path.join(search, 'spec.md')).read()
	suite.check('width runs N at once and tables the median and the slowest',
		'| 1 | 1 | 1 |' in out and '| 2 | 1 | 1 |' in out, out[-800:])
	suite.check('width runs in the fixed folder setup/width, made fresh each '
		'time', os.path.isdir(os.path.join(root, 'setup', 'width')) and not [
		name for name in os.listdir(root) if name.startswith('width.')],
		os.listdir(root))
	suite.check('and --write puts its verdict on Width measured',
		'- Width measured: 2 concurrent run(s) at most (the plan runs 1 at '
		'once)' in spec, [line for line in spec.splitlines() if 'Width' in line])
	out = call([AUTOOPT, 'width', search, '--counts', '1', '--reps', '1',
		'--background'], expect=(0,))
	log = os.path.join(search, 'setup', 'width.log')
	waited = ''
	for _ in range(30):
		waited += call([AUTOOPT, 'wait', log, '--max', '5'])
		if 'wait: finished' in waited:
			break
	suite.check('width --background returns at once and writes setup/width.log '
		'once, ending with its exit', 'started in the background' in out
		and 'wait: finished, exit 0' in waited and open(log).read().count(
			'| 1 | 1 | 1 |') == 1, out + waited[-600:])

	table = {1: [(0.92, 0.926, 0), (0.917, 0.92, 0)], 4: [(0.90, 0.918, 0)],
		8: [(0.899, 0.935, 0)], 12: [(0.90, 1.01, 0)]}
	alone, tolerance, supported = autoopt.width_verdict(table, True)
	suite.check('the width is the last count whose median and slowest run '
		'stay within the slowest run alone plus the runs made alone spread '
		'(the step 2 table)', (alone, round(tolerance, 6), supported)
		== (0.926, 0.006, 4), (alone, tolerance, supported))
	suite.equal('and the other way round for an objective maximized',
		autoopt.width_verdict({1: [(10, 9.5, 0)], 2: [(10, 9.6, 0)],
			4: [(9.4, 9.0, 0)]}, False), (9.5, 0, 2))
	# A table where a 0.4% slowdown of the slowest run is noise.
	measured = {1: [(v, v, 41) for v in (0.1234, 0.1236, 0.1238, 0.124,
		0.1241)], 2: [(0.1229, 0.1231, 41), (0.1243, 0.1246, 41)],
		4: [(0.123, 0.1234, 41), (0.124, 0.1244, 41)]}
	suite.equal('a slowest run inside the alone runs\' spread does not narrow '
		'the width', autoopt.width_verdict(measured, True)[2], 4)
	measured[4].append((0.1239, 0.1260, 41))
	suite.equal('and one past it does', autoopt.width_verdict(measured,
		True)[2], 2)

	suite.check('prove and width leave the search\'s own reference alone, '
		'though the command names it', not [name for name in os.listdir(real)
			if name.startswith('seed-')] if os.path.isdir(real) else True,
		os.listdir(real) if os.path.isdir(real) else None)

	out = call([AUTOOPT, 'spec', search, '--set', 'Gate proof', 'same 1, '
		'reorder 1, lossy 0'], expect=(0,))
	spec = open(os.path.join(search, 'spec.md')).read()
	suite.check('spec --set replaces one answer and its continuation lines',
		'- Gate proof: same 1, reorder 1, lossy 0\n' in spec and spec.count(
			'- Gate proof:') == 1, [line for line in spec.splitlines()
			if 'Gate proof' in line])
	out = call([AUTOOPT, 'spec', search, '--set', 'No such line', 'x'],
		expect=(1,))
	suite.check('and refuses a line spec.md does not have',
		'has no `- No such line:` line' in out, out[-200:])


def next_briefs(suite, base):
	"""`brief --next`: status, new round summaries once, then a brief per
	free slot from the top untried ideas, every field filled from the
	records."""

	search = make_search(os.path.join(base, 'next-briefs'), staged=False)
	add(search, 1, 0.60, category='MICRO')
	with open(os.path.join(autoopt.iteration_dir(search, 1), 'notes.md'),
			'w') as handle:
		handle.write(NOTES)
	call([AUTOOPT, 'finish', search, '--id', '1'], expect=(0,))
	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- [MICRO] bench.py:score: raise the quality in place. '
			'Why: higher. Gain: unmeasured. Exact: changes outputs. (S1)\n'
			'- [CACHING] bench.py:score: cache the random draws, which repeat. '
			'Why: twice. Gain: unmeasured. Exact: bit-identical. (S1)\n'
			'- [PRECISION] other.py:f: lower the precision, keeping the shape. '
			'Why: faster. Gain: unmeasured. Exact: within tolerance. (S2)\n')
	call([AUTOOPT, 'spec', search, '--set', 'Benchmark', 'bench.py draws '
		'one seeded score and writes it'])
	out = call([AUTOOPT, 'brief', search, '--next', '--count', '0'],
		expect=(0,))
	suite.check('--count 0 prints the status and briefs nothing, saying so',
		'--count 0; nothing briefed' in out and not os.path.exists(
			os.path.join(search, 'briefs', '2.json')), out[-300:])
	call([AUTOOPT, 'summary', search, '--round', '1'])

	out = call([AUTOOPT, 'brief', search, '--next'], expect=(0,))
	suite.check('brief --next prints the status first', out.startswith(
		search) and 'next free id: A2' in out, out[:400])
	suite.check('and a round summary written since, once, for the user',
		'round-1.md, written since the last brief: show the user this table'
		in out, out[:1500])
	first = json.load(open(os.path.join(search, 'briefs', '2.json')))
	printed = out.split('\n# The brief for iteration')[0]
	suite.check('and ends by asking for a watch when none runs',
		printed.rfind('watch: none running: start `autoopt.py watch <work>` '
			'in the background before ending the turn') > printed.rfind(
				'In your first turn'), printed[-400:])
	suite.check('it briefs the one free slot, the latest stage first',
		first['category'] == 'PRECISION' and first['title']
		== 'f: lower the precision' and out.count('In your first turn') == 1
		and not os.path.exists(os.path.join(search, 'briefs', '3.json')),
		first)
	text = open(os.path.join(search, 'briefs', '2.md')).read()
	suite.check('and the spec\'s Benchmark line, so no subagent reads the '
		'harness for it', '- What one run does (`spec.md`, *Benchmark*): '
		'bench.py draws one seeded score and writes it\n' in text, text[-1500:])
	suite.check('and "none" for a file nothing changed', 'none: no '
		'iteration has changed `other.py`' in text, text[-2500:])

	out = call([AUTOOPT, 'brief', search, '--next'], expect=(0,))
	suite.check('with the slot briefed, a second call briefs nothing and '
		'shows no summary twice', 'no free slot' in out and 'round-1.md'
		not in out, out[-400:])
	call([AUTOOPT, 'brief', search, '--next', '--count', '1'], expect=(0,))
	second = json.load(open(os.path.join(search, 'briefs', '3.json')))
	suite.check('--count briefs past the slot, the next idea',
		second['category'] == 'MICRO' and second['title']
		== 'score: raise the quality in place', second)
	text = open(os.path.join(search, 'briefs', '3.md')).read()
	suite.check('with what already changed the idea\'s file under Already '
		'tried', re.search(r'A1 \(iteration 1\): [\d.]+ against A0\'s [\d.]+; '
			r'passed the keep rule but is not in the root\.', text) is not None, text[-2500:])
	out = call([AUTOOPT, 'brief', search, '--next', '--count', '1'],
		expect=(0,))
	third = json.load(open(os.path.join(search, 'briefs', '4.json')))
	suite.check('and again, skipping the briefed lines',
		third['category'] == 'CACHING', third)
	out = call([AUTOOPT, 'brief', search, '--next', '--count', '1'],
		expect=(0,))
	suite.check('and says so when no unbriefed idea is left',
		'no untried idea left' in out, out[-300:])
	out = call([AUTOOPT, 'brief', search, '--next', '--count', '2', '--gpus',
		'1,2'], expect=(1,))
	suite.check('GPU indices refuse two briefs at once', 'must not share'
		in out, out[-300:])

	before_ids = sorted(autoopt.pending_briefs(search))
	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- [MICRO] fresh.py:g: a newer idea. Why: x. Gain: '
			'unmeasured. Exact: bit-identical. (S2)\n')
	out = call([AUTOOPT, 'brief', search, '--next', '--replace', '--count',
		'1'], expect=(0,))
	rewritten = open(os.path.join(search, 'briefs', '{}.md'.format(
		before_ids[0]))).read()
	suite.check('--next --replace rewrites the oldest unspawned brief, taking '
		'no new id', sorted(autoopt.pending_briefs(search)) == before_ids
		and 'fresh.py:g: a newer idea' in rewritten, (before_ids,
			sorted(autoopt.pending_briefs(search)), out[-400:]))

	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- [MICRO] other.py:f, bench.py:score and ./bench.py:draw: '
			'share one draw. Why: x. Gain: unmeasured. Exact: bit-identical. '
			'(S1)\n')
	call([AUTOOPT, 'brief', search, '--next', '--count', '1', '--idea-line',
		'share one draw'], expect=(0,))
	text = open(os.path.join(search, 'briefs', '{}.md'.format(max(
		autoopt.pending_briefs(search))))).read()
	tried = text.split('## Already tried on this code\n\n')[-1].split('\n')[0]
	suite.check('Already tried covers every file the idea line names, each '
		'iteration once', tried.count('A1 (') == 1 and 'none' not in tried,
		tried)

	older = make_search(os.path.join(base, 'next-briefs-older'), staged=False)
	add(older, 1, 0.60)
	call([AUTOOPT, 'summary', older, '--round', '1'])
	out = call([AUTOOPT, 'brief', older, '--next', '--count', '0'],
		expect=(0,))
	suite.check('on the first call of a search that already has summaries, none is '
		'shown, and all are marked shown', 'written since the last brief'
		not in out and autoopt.load_state(older).get('summaries_shown')
		== ['round-1.md'], out[-600:])


def one_run_first(suite, base):
	"""The runner's own smoke run: one run, then the unit tests, then the
	other runs; one at a time, the runs stop at the first failure."""

	root = os.path.join(base, 'one-run-first')
	os.makedirs(root)
	call([AUTOOPT, 'init', root, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--no-gpu', '--promote', 'orchestrator',
		'--gates', json.dumps([{'name': 'cost', 'op': '<=', 'threshold': 1.0}]),
		'--observers', 'size', '--tests', 'test ! -e BROKEN'])
	search = os.path.join(root, 'autooptimize')
	bench(autoopt.iteration_dir(search, 0), 0.5)
	call([RUNNER, autoopt.iteration_dir(search, 0)])

	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'breaks a test',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	bench(directory, 0.6)
	open(os.path.join(directory, 'BROKEN'), 'w').close()
	out = call([RUNNER, directory])
	broken = autoopt.load_result(search, 1)
	suite.check('a failing unit test stops the iteration after one run',
		broken['status'] == 'gate-failed' and broken['n_runs'] == 1
		and seeds_of(broken) == [0] and not broken['tests']['pass'], (
			broken['status'], broken['n_runs'], broken.get('tests')))
	suite.check('and the tests ran after that run, before any other',
		out.index('seed 0:') < out.index('unit tests: running')
		and 'seed 1:' not in out, out[-800:])

	os.remove(os.path.join(directory, 'BROKEN'))
	out = call([RUNNER, directory])
	fixed = autoopt.load_result(search, 1)
	suite.check('the fix re-runs on its new commit to the full count',
		fixed['status'] == 'ok' and seeds_of(fixed) == [0, 1, 2, 3, 4]
		and fixed['tests']['pass'], (fixed['status'], seeds_of(fixed)))
	suite.check('with the tests once, between run one and the rest',
		out.index('seed 0:') < out.index('unit tests:') < out.index('seed 1:'),
		out[-800:])
	report = open(os.path.join(directory, 'report.txt')).read()
	parent = autoopt.load_result(search, 0)
	suite.check('the report gives the parent\'s median as the keep rule\'s '
		'reference', re.search(r'\nparent +A0 {} median, what the keep rule '
		r'judges against\n'.format(re.escape(autoopt.fmt(parent['objective'][
			'median']))), report), report)
	suite.check('and each observer beside the parent\'s',
		re.search(r'\nobs size +100 median \(A0 100\)\n', report), report)
	rows = re.findall(r'(?m)^run (\d) +(\S+) \(A0 (\S+)\)  size 100 \(100\)  '
		r'load \d+\.\d$', report)
	suite.check('and one row per run, beside the parent\'s run of the same '
		'seed, with the load at its start',
		[int(seed) for seed, _, _ in rows] == [0, 1, 2, 3, 4]
		and all(mine == autoopt.fmt(run['objective']) and theirs
			== autoopt.fmt(other['objective']) for (_, mine, theirs), run, other
			in zip(rows, fixed['runs'], parent['runs'])), report)

	call([AUTOOPT, 'new', search, '--id', '2', '--title', 'crashes on seed 2',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 2)
	bench(directory, 0.6, fail_on=2)
	out = call([RUNNER, directory])
	crashed = autoopt.load_result(search, 2)
	suite.check('the runs stop at the first that fails',
		crashed['status'] == 'error' and seeds_of(crashed) == [0, 1]
		and [f['seed'] for f in crashed['failures']] == [2]
		and 'seed 3:' not in out and 'stopping: seed 2 failed' in out,
		(crashed['status'], seeds_of(crashed), out[-600:]))

	call([AUTOOPT, 'new', search, '--id', '3', '--title', 'breaks the gate',
		'--category', 'MISC'])
	directory = autoopt.iteration_dir(search, 3)
	bench(directory, 0.9, gate=2.0)
	out = call([RUNNER, directory])
	gated = autoopt.load_result(search, 3)
	suite.check('a broken gate on the first run stops before the tests',
		gated['status'] == 'gate-failed' and gated['n_runs'] == 1
		and 'unit tests: running' not in out, (gated['status'],
			gated['n_runs'], out[-600:]))
	suite.check('and its run\'s row names the gate it broke',
		re.search(r'\nrun 0 +\S+ \(A0 \S+\)  size 100 \(100\)  cost 2 BROKEN  '
		r'load', open(os.path.join(directory, 'report.txt')).read()),
		open(os.path.join(directory, 'report.txt')).read())


def age(directory, seconds):
	"""Set every file under `directory` back `seconds`, as if nothing had
	written there since."""

	then = time.time() - seconds
	for folder, _, names in os.walk(directory):
		for name in names:
			os.utime(os.path.join(folder, name), (then, then))


def resuming(suite, base):
	"""`status --resume`: a verdict for each unfinished iteration, and
	`brief --prompt` with the line for where the last subagent stopped."""

	search = make_search(os.path.join(base, 'resume'), staged=False, runs=2)

	def brief_file(identifier):
		os.makedirs(os.path.join(search, 'briefs'), exist_ok=True)
		with open(os.path.join(search, 'briefs', '{}.md'.format(identifier)),
				'w') as handle:
			handle.write('# The brief for iteration A{}\n'.format(identifier))

	for identifier in (1, 2, 3, 4, 5):
		call([AUTOOPT, 'new', search, '--id', str(identifier), '--title',
			'idea {}'.format(identifier), '--category', 'MISC'])
		brief_file(identifier)

	# A1 never edited; A2 edited, never run; A3 measured, no README; A4 a
	# live runner; A5 written to just now.
	bench(autoopt.iteration_dir(search, 2), 0.7)
	bench(autoopt.iteration_dir(search, 3), 0.6)
	call([RUNNER, autoopt.iteration_dir(search, 3)])
	for identifier in (1, 2, 3, 4):
		age(autoopt.iteration_dir(search, identifier), 7200)
	fake = os.path.join(base, 'resume-fake')
	os.makedirs(fake, exist_ok=True)
	with open(os.path.join(fake, 'run_iteration.py'), 'w') as handle:
		handle.write('import time\ntime.sleep(60)\n')
	live = subprocess.Popen([sys.executable, os.path.join(fake,
		'run_iteration.py'), autoopt.iteration_dir(search, 4)])

	try:
		out = call([AUTOOPT, 'status', search, '--resume'])
	finally:
		live.kill()
		live.wait()

	def verdict(name):
		found = re.search(r'\n  {} \([^)]*\): (.*)'.format(name), out)
		return found.group(1) if found else ''

	suite.check('status --resume prints --brief, then each unfinished '
		'iteration', out.index('(the last') < out.index('## Unfinished '
			'iterations'), out[:600])
	suite.check('an iteration never edited is an empty fork to set aside',
		verdict('A1').startswith('an empty fork') and 'iterations/1.abandoned'
		in verdict('A1'), verdict('A1'))
	suite.check('an edited one goes to a new subagent with brief --prompt',
		verdict('A2').startswith('edited, not measured') and '--prompt 2'
		in verdict('A2'), verdict('A2'))
	suite.check('a measured one without a README too',
		verdict('A3').startswith('measured, no README') and '--prompt 3'
		in verdict('A3'), verdict('A3'))
	suite.check('a live runner is left alone, by its pid',
		verdict('A4').startswith('its runner is alive (pid {})'.format(
			live.pid)), verdict('A4'))
	suite.check('and one written to just now may still be working',
		verdict('A5').startswith('its subagent wrote'), verdict('A5'))
	suite.check('then the plan, the rules, the last round and the next ideas',
		all(heading in out for heading in ('## The plan (config.json)',
			'## Rules (spec.md)', '## The last round summary',
			'## The next untried ideas')) and '2 run(s) per iteration; '
		'promote orchestrator' in out, out[-1500:])

	measured = call([AUTOOPT, 'brief', search, '--prompt', '3'])
	unmeasured = call([AUTOOPT, 'brief', search, '--prompt', '2'])
	suite.check('brief --prompt tells the next subagent where the last '
		'stopped', autoopt.TAKEOVER_MEASURED in measured
		and autoopt.TAKEOVER_UNMEASURED in unmeasured
		and autoopt.TAKEOVER_MEASURED not in unmeasured, measured + unmeasured)


def watching(suite, base):
	"""`watch`: the orchestrator's fallback wake-up returns on a new event,
	never on one already flagged when it started, and at its cap."""

	search = make_search(os.path.join(base, 'watch'), staged=False, runs=2)
	out = call([AUTOOPT, 'watch', search, '--every', '0.2'])
	suite.check('with nothing in flight, watch returns at once',
		'nothing is in flight' in out, out)

	for identifier in (1, 2):
		call([AUTOOPT, 'new', search, '--id', str(identifier), '--title',
			'idea {}'.format(identifier), '--category', 'MISC'])
	age(autoopt.iteration_dir(search, 1), 7200)

	def start(*extra):
		return subprocess.Popen([sys.executable, AUTOOPT, 'watch', search,
			'--every', '0.2'] + list(extra), stdout=subprocess.PIPE,
			stderr=subprocess.STDOUT, text=True)

	first = start('--cap', '60')
	time.sleep(1.5)
	running = first.poll() is None
	listed = call([AUTOOPT, 'status', search, '--brief'])
	second = call([AUTOOPT, 'watch', search])
	suite.check('A1, stalled before it started, does not end it', running,
		first.stdout.read() if not running else '')
	suite.check('status says a watch is running', re.search(
		r'\nwatch: running \(pid {},'.format(first.pid), listed), listed)
	suite.check('and a second watch stops at once', 'already running (pid '
		'{}'.format(first.pid) in second, second)

	age(autoopt.iteration_dir(search, 2), 7200)
	try:
		said, _ = first.communicate(timeout=20)
	except subprocess.TimeoutExpired:
		first.kill()
		said = first.communicate()[0]
	suite.check('A2 going silent ends it, naming only A2',
		'A2 is STALLED' in said and 'A1' not in said, said)
	suite.check('and its record is removed',
		not os.path.exists(os.path.join(search, 'watch.json')))
	listed = call([AUTOOPT, 'status', search, '--brief'])
	suite.check('status then asks for one', 'watch: none running: start '
		'`autoopt.py watch <work>`' in listed, listed)

	capped = start('--cap', '1')
	said, _ = capped.communicate(timeout=20)
	suite.check('at its cap it returns, naming what was flagged before',
		'with nothing new to act on (still flagged from before it started: '
		'A1 is STALLED, A2 is STALLED)' in said, said)

	call([AUTOOPT, 'new', search, '--id', '3', '--title', 'idea 3',
		'--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 3), 0.6)
	call([RUNNER, autoopt.iteration_dir(search, 3)])
	age(autoopt.iteration_dir(search, 3), 3000)
	unwritten = start('--cap', '60')
	time.sleep(1.0)
	age(autoopt.iteration_dir(search, 3), 7200)
	try:
		said, _ = unwritten.communicate(timeout=20)
	except subprocess.TimeoutExpired:
		unwritten.kill()
		said = unwritten.communicate()[0]
	suite.check('a measured iteration left without a README for an hour '
		'ends it', 'A3 has no README after an hour' in said, said)


# A checker that reads two trees: exit 1 when the child's bench.py was
# broken, and, with --built, when either tree was not built.
TREE_CHECKER = """import os, sys
parent, child = sys.argv[1:3]
text = open(os.path.join(child, 'bench.py')).read()
bad = 'BROKEN' in text or 'random.seed' not in text
if '--built' in sys.argv:
	bad = bad or not all(os.path.exists(os.path.join(tree, 'BUILT'))
		for tree in (parent, child))
sys.exit(1 if bad else 0)
"""

FAULTS = """## broken: the seed is dropped
file: bench.py
<<<
random.seed(args.seed)
===
pass  # BROKEN
>>>

## comment: a change it cannot see
file: bench.py
<<<
os.makedirs(args.out, exist_ok=True)
===
os.makedirs(args.out, exist_ok=True)  # harmless
>>>
"""


def variants_and_proofs(suite, base):
	"""`variant` exports an edit or the parent's code, built with
	checks.build; `prove --id` runs an iteration's checker on planted faults,
	and `finish` takes the row's faults from its table."""

	search = make_search(os.path.join(base, 'variants'), staged=False, runs=2)
	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'one',
		'--category', 'MICRO'])
	directory = autoopt.iteration_dir(search, 1)
	bench(directory, 0.6)

	out = call([AUTOOPT, 'variant', search, '--id', 'A1', 'v1'])
	mine = os.path.join(directory, 'probe', 'v1')
	suite.check('variant exports the edit as it stands, before any commit',
		'QUALITY = 0.6' in open(os.path.join(mine, 'bench.py')).read()
		and not os.path.exists(os.path.join(mine, '.git'))
		and out.startswith(mine), out)
	staged = git('-C', directory, 'diff', '--cached', '--name-only')
	suite.check('the export leaves the worktree\'s own index as it was',
		staged.strip() == '', staged)
	racers = [subprocess.Popen([sys.executable, AUTOOPT, 'variant', search,
		'--id', '1', 'race{}'.format(n)], stdout=subprocess.PIPE,
		stderr=subprocess.STDOUT, text=True) for n in range(4)]
	said = [racer.communicate()[0] for racer in racers]
	suite.check('four exports at once never meet on the index lock',
		all(racer.returncode == 0 for racer in racers), said)
	call([AUTOOPT, 'variant', search, '--id', '1', 'p', '--parent'])
	suite.check('and --parent the parent\'s code', 'QUALITY = 0.5' in open(
		os.path.join(directory, 'probe', 'p', 'bench.py')).read())
	out = call([AUTOOPT, 'variant', search, '--id', '1', 'v1'], expect=(1,))
	suite.check('an existing name is refused, nothing deleted',
		'exists: give a new name' in out, out)

	call([AUTOOPT, 'config', search, '--set', 'checks.build=touch {src}/BUILT',
		'--reason', 'test'])
	out = call([AUTOOPT, 'variant', search, '--id', '1', 'v2'])
	suite.check('with checks.build, the copy is built in place, its log '
		'beside it', os.path.exists(os.path.join(directory, 'probe', 'v2',
			'BUILT')) and open(os.path.join(directory, 'probe',
			'v2.build.log')).read().rstrip().endswith('exit 0'), out)

	probe = os.path.join(directory, 'probe')
	os.makedirs(os.path.join(probe, 'guard'))
	with open(os.path.join(probe, 'guard', 'check.py'), 'w') as handle:
		handle.write(TREE_CHECKER)
	row = os.path.join(probe, 'checker.txt')
	with open(row, 'w') as handle:
		handle.write('name: core\nfiles: bench.py\ncompares: bench.py of two '
			'trees\nbuilt: touch BUILT\nrun: python3 {checker}/check.py {parent} '
			'{child} --built\ninputs: none\n')
	faults = os.path.join(probe, 'faults.txt')
	with open(faults, 'w') as handle:
		handle.write(FAULTS)
	arguments = [AUTOOPT, 'prove', search, '--id', '1', '--checker',
		os.path.join(probe, 'guard'), '--checker-row', row, '--faults', faults]
	out = call(arguments)
	suite.check('prove --id: the checker passes on the clean edit, built, '
		'and each fault is caught or missed', re.search(r'\n  clean +passes\n'
		r'  broken +caught .*\n  comment +MISSED ', out), out)
	proof = json.load(open(os.path.join(probe, 'checker-proof.json')))
	suite.check('the table is kept for finish', proof['summary']
		== 'broken: caught; comment: MISSED', proof)

	with open(faults, 'w') as handle:
		handle.write(FAULTS.replace('random.seed(args.seed)', 'nowhere()'))
	out = call(arguments, expect=(1,))
	suite.check('a fault whose old text is not in the file is refused',
		'occurs 0 times' in out, out[-300:])
	with open(faults, 'w') as handle:
		handle.write(FAULTS)

	with open(os.path.join(probe, 'guard', 'check.py'), 'w') as handle:
		handle.write('import sys\nsys.exit(1)\n')
	out = call(arguments, expect=(1,))
	suite.check('a checker that fails on the clean edit is refused',
		'must pass there before a fault means anything' in out, out[-300:])
	with open(os.path.join(probe, 'guard', 'check.py'), 'w') as handle:
		handle.write(TREE_CHECKER)

	call([RUNNER, directory])
	with open(os.path.join(directory, 'notes.md'), 'w') as handle:
		handle.write(NOTES)
	out = call([AUTOOPT, 'finish', search, '--id', '1', '--checker',
		os.path.join(probe, 'guard'), '--checker-row', row], expect=(0,))
	library = open(os.path.join(search, 'tools', 'checkers',
		'README.md')).read()
	suite.check('finish takes the faults from the proof when the row has '
		'none, and runs the checker on built trees', 'broken: caught; '
		'comment: MISSED (prove --id)' in open(os.path.join(search, 'tools',
			'checkers', 'core-A1', 'ROW.md')).read() and os.path.isdir(
			os.path.join(probe, 'trees')), out[-600:] + library[-600:])

	call([AUTOOPT, 'new', search, '--id', '2', '--parent', '1', '--title',
		'two', '--category', 'MICRO'])
	second = autoopt.iteration_dir(search, 2)
	bench(second, 0.7)
	out = call([AUTOOPT, 'checkers', search, '--id', '2'])
	suite.check('checkers lists the library, marking one that guards a file '
		'the edit changes', re.search(r'core-A1 +guards bench\.py; .*TOUCHED',
			out) and 'A2 changes: bench.py' in out, out)
	out = call([AUTOOPT, 'checkers', search, '--id', '2', '--touched'],
		expect=(0,))
	suite.check('--touched runs it on the parent and the edit, built',
		re.search(r'core-A1 +pass in ', out), out)
	with open(os.path.join(second, 'bench.py')) as handle:
		text = handle.read()
	with open(os.path.join(second, 'bench.py'), 'w') as handle:
		handle.write(text.replace('random.seed(args.seed)', 'pass  # BROKEN'))
	out = call([AUTOOPT, 'checkers', search, '--id', '2', '--name',
		'core-A1'], expect=(1,))
	suite.check('and says FAIL, exiting 1, when the edit breaks what it '
		'guards', re.search(r'core-A1 +FAIL \(exit 1\)', out), out)
	out = call([AUTOOPT, 'checkers', search, '--id', '2', '--name', 'nope'],
		expect=(1,))
	suite.check('an unknown checker is refused, naming the library',
		'no checker nope' in out and 'core-A1' in out, out)
	with open(os.path.join(second, 'bench.py'), 'w') as handle:
		handle.write(text)
	call([AUTOOPT, 'new', search, '--id', '3', '--parent', '1', '--title',
		'three', '--category', 'MICRO'])
	with open(os.path.join(autoopt.iteration_dir(search, 3), 'other.py'),
			'w') as handle:
		handle.write('x = 1\n')
	out = call([AUTOOPT, 'checkers', search, '--id', '3', '--touched'],
		expect=(0,))
	suite.check('--touched runs nothing when no checker guards a changed file',
		'none guards a file A3 changes (other.py)' in out, out)

	old_library = os.path.join(search, 'tools', 'checkers', 'README.md')
	with open(old_library, 'w') as handle:
		handle.write('# Checker library\n\n| Checker | Compares | How it is '
			'built | Pointing it at a new parent and child | Planted-fault '
			'controls | Inputs | Run time |\n|---|---|---|---|---|---|---|\n')
	call([AUTOOPT, 'finish', search, '--id', '1', '--checker',
		os.path.join(probe, 'guard'), '--checker-row', row], expect=(0,))
	suite.check('a library whose header has seven columns keeps them',
		re.search(r'\n\| core-A1 \| bench\.py of two trees \| touch BUILT \| '
			r'`python3 tools/checkers/core-A1/check\.py', open(old_library)
			.read()), open(old_library).read())


# A profile as setup writes one: the shares of the timed call, largest
# first, read from the tree it is given.
PROFILER = """import os, sys
text = open(os.path.join(sys.argv[1], 'bench.py')).read()
if 'FAIL_PROFILE' in os.environ:
	sys.exit(1)
print('60.0%  bench.py:score')
print('40.0%  bench.py:draw ({})'.format(text.split('QUALITY = ')[1].split()[0]))
"""


def profiling(suite, base):
	"""`checks.profile`: written for A0 by `profile`, for each new root by
	`promote`; quoted in the miner's prompt and, for the idea's function, in
	each brief."""

	search = make_search(os.path.join(base, 'profiling'), promote='auto', staged=False, runs=2)
	script = os.path.join(base, 'profiling', 'profile.py')
	with open(script, 'w') as handle:
		handle.write(PROFILER)
	call([AUTOOPT, 'config', search, '--set', 'checks.profile=python3 {} '
		'{{src}} {{out}}'.format(script), '--reason', 'test'])

	out = call([AUTOOPT, 'profile', search, '--id', '0'])
	text = open(os.path.join(autoopt.iteration_dir(search, 0),
		'profile.txt')).read()
	suite.check('profile writes A0\'s profile.txt from its committed tree',
		text.startswith('# Profile of A0 (commit ') and '40.0%  bench.py:draw '
		'(0.5)' in text and 'profile: ' in out, text + out)

	promoted = add(search, 1, 0.7)
	report = open(os.path.join(autoopt.iteration_dir(search, 1),
		'report.txt')).read()
	path = os.path.join(autoopt.iteration_dir(search, 1), 'profile.txt')
	text = open(path).read() if os.path.exists(path) else ''
	suite.check('an auto-promoted root is profiled by promote, and its report '
		'says so', promoted['keep'].get('promoted') and '(0.7)' in text
		and re.search(r'\nprofile: \S+iterations/1/profile\.txt \(', report)
		and autoopt.load_result(search, 1).get('profile_seconds') is not None,
		report[-500:])

	call([AUTOOPT, 'mine', search])
	prompt = open(os.path.join(search, 'briefs', 'miner-1.md')).read()
	suite.check('the miner\'s prompt carries the root\'s profile',
		'## The profile of A1' in prompt and '    60.0%  bench.py:score'
		in prompt, prompt[-800:])

	def brief_text(identifier, idea):
		call([AUTOOPT, 'brief', search, '--id', str(identifier), '--title',
			't', '--category', 'MICRO', '--idea', idea, '--tried', 'none'])
		return open(os.path.join(search, 'briefs', '{}.md'.format(
			identifier))).read()

	named = brief_text(2, '[MICRO] bench.py:score: draw once. Why: twice.')
	missing = brief_text(3, 'bench.py:absent: remove it. Why: dead.')
	suite.check('a brief quotes the profile lines that name the idea\'s '
		'function', 'The profile of A1 (`' in named and 'names `score`: '
		'`60.0%  bench.py:score`' in named, named[:1500])
	suite.check('and says when the profile does not name it', 'does not name '
		'`absent`: confirm that the timed call reaches it' in missing,
		missing[:1500])

	os.environ['FAIL_PROFILE'] = '1'
	try:
		third = add(search, 4, 0.9)
	finally:
		del os.environ['FAIL_PROFILE']
	report = open(os.path.join(autoopt.iteration_dir(search, 4),
		'report.txt')).read()
	suite.check('a profile that fails does not stop the promotion, and says '
		'why', third['keep'].get('promoted') and 'profile: not written: '
		'checks.profile exits 1' in report, report[-500:])

	call([AUTOOPT, 'new', search, '--id', '5', '--title', 'a hair better',
		'--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 5), 0.9015)
	out = call([RUNNER, autoopt.iteration_dir(search, 5)])
	suite.check('a best median kept out by the noise band is not announced '
		'as a new frontier', 'best median so far' in out and 'not kept' in out and 'NEW FRONTIER' not in out
		and autoopt.load_result(search, 5)['keep']['verdict'] == 'fail',
		out[-500:])


def inventories(suite, base):
	"""The code inventory in the setup and miner prompts: files with line
	counts, Python definitions with line numbers, the profile's files first,
	vendored code folded, a cap on its length."""

	folder = os.path.join(base, 'inventory')
	os.makedirs(os.path.join(folder, 'pkg'))
	os.makedirs(os.path.join(folder, 'third_party', 'lib'))
	with open(os.path.join(folder, 'pkg', 'big.py'), 'w') as handle:
		handle.write('x = 1\n' * 100)
	with open(os.path.join(folder, 'pkg', 'small.py'), 'w') as handle:
		handle.write('def f():\n\treturn 1\n\nclass K:\n    def g(self):\n'
			'        pass\n')
	for name in ('a.c', 'b.c'):
		with open(os.path.join(folder, 'third_party', 'lib', name), 'w') as handle:
			handle.write('int x;\n')
	lines = autoopt.code_inventory(folder, first=('pkg/small.py',))
	suite.check('the inventory lists the profile\'s files first, with '
		'definitions and line counts', lines[0] == '- `pkg/small.py` (6 '
		'lines): f 1, K 4, K.g 5' and lines[1] == '- `pkg/big.py` (100 lines)',
		lines)
	suite.check('and folds vendored code into one line',
		'- `third_party/` (2 vendored source files, not listed)' in lines, lines)
	for number in range(130):
		with open(os.path.join(folder, 'pkg', 'm{}.py'.format(number)),
				'w') as handle:
			handle.write('y = 2\n')
	lines = autoopt.code_inventory(folder)
	suite.check('past its cap it counts the rest', len(lines)
		== autoopt.INVENTORY_LINES + 1 and lines[-1].startswith('- … 13 more '
			'files'), lines[-3:])

	search = make_search(os.path.join(base, 'inventory-search'), staged=False,
		runs=2)
	with open(os.path.join(autoopt.iteration_dir(search, 0), 'helper.py'),
			'w') as handle:
		handle.write('def helper():\n\treturn 2\n')
	git('-C', autoopt.iteration_dir(search, 0), 'add', 'helper.py')
	state = autoopt.load_state(search)
	state.pop('setup', None)
	autoopt.save_state(search, state)
	call([AUTOOPT, 'setup', search])
	prompt = open(os.path.join(search, 'briefs', 'setup.md')).read()
	suite.check('the setup prompt carries A0\'s code and the usage of the '
		'subcommands step 2 uses', '## The code (A0, `' in prompt
		and '- `helper.py` (2 lines): helper 1' in prompt
		and '- `autoopt.py prove [-h]' in prompt and '- `autoopt.py width '
		in prompt, prompt[-2500:])
	call([AUTOOPT, 'mine', search])
	prompt = open(os.path.join(search, 'briefs', 'miner-1.md')).read()
	suite.check('and the miner\'s prompt the reviewed root\'s code',
		'## The code (A0, `' in prompt and '- `helper.py` (2 lines): '
		'helper 1' in prompt, prompt[-1500:])


def quiet_check(suite, base):
	"""`busy` names the processes outside the search above about 100% CPU;
	the baseline's runner records them and never refuses; the runner's
	--background detaches it."""

	search = make_search(os.path.join(base, 'quiet'), staged=False, runs=2)
	burner = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
		cwd=base)
	inside = subprocess.Popen([sys.executable, '-c', 'while True: pass'],
		cwd=search)
	env = dict(os.environ, AUTOOPT_BUSY_PERCENT='50')
	env.pop('AUTOOPT_SKIP_QUIET_CHECK')
	try:
		listed = [pid for pid, _, _, _ in autoopt.busy_processes(search,
			threshold=50)]
		checked = subprocess.run([sys.executable, AUTOOPT, 'busy', search],
			capture_output=True, text=True, env=env)
		ran = subprocess.run([sys.executable, RUNNER,
			autoopt.iteration_dir(search, 0), '--rerun'], capture_output=True,
			text=True, env=env)
	finally:
		for process in (burner, inside):
			process.kill()
			process.wait()
	suite.check('a process outside the search is busy, one inside it is not',
		burner.pid in listed and inside.pid not in listed, listed[:10])
	suite.check('busy names it and exits 1', checked.returncode == 1
		and 'pid {} '.format(burner.pid) in checked.stdout, checked.stdout[-400:])
	recorded = autoopt.load_result(search, 0).get('busy_at_start') or []
	report = open(os.path.join(autoopt.iteration_dir(search, 0),
		'report.txt')).read()
	suite.check('the baseline runs on a busy machine, naming it in its report '
		'and result.json', ran.returncode == 0 and burner.pid in [item['pid']
			for item in recorded] and 'busy at start: ' in report
		and 'measured under this load' in ran.stdout, ran.stdout[-500:])

	call([AUTOOPT, 'new', search, '--id', '1', '--title', 'one', '--category',
		'MISC'])
	directory = autoopt.iteration_dir(search, 1)
	bench(directory, 0.6)
	out = call([RUNNER, directory, '--background'])
	log = os.path.join(directory, 'probe', 'runner.log')
	waited = ''
	for _ in range(30):
		waited += call([AUTOOPT, 'wait', log, '--max', '5'])
		if 'wait: finished' in waited:
			break
	suite.check('the runner\'s --background returns at once and writes '
		'probe/runner.log, ending with its exit', 'started in the background'
		in out and 'wait: finished, exit 0' in waited and autoopt.load_result(
			search, 1) is not None, out + waited[-400:])
	out = call([AUTOOPT, 'final', search, '--background'])
	log = os.path.join(search, 'final.log')
	waited = ''
	for _ in range(30):
		waited += call([AUTOOPT, 'wait', log, '--max', '5'])
		if 'wait: finished' in waited:
			break
	suite.check('final --background detaches into final.log, ending with its '
		'exit', 'started in the background' in out and 'wait: finished, exit '
		'1' in waited and 'no timing probe' in open(log).read(), waited[-400:])
	out = call([os.path.join(SCRIPTS, 'check_seeding.py'),
		autoopt.iteration_dir(search, 0)], expect=(0, 1, 2))
	log = os.path.join(search, 'setup', 'seeding.log')
	suite.check('check_seeding on the baseline writes setup/seeding.log',
		os.path.exists(log) and re.search(r'exit \d\n$', open(log).read()),
		out[-300:])


def orchestrator_phases(suite, base):
	"""`loop`: the orchestrator subagent's phases, the prompt that runs
	them, and the hand-back `brief --next` calls for."""

	search = make_search(os.path.join(base, 'phases'), staged=False, runs=2)
	state = autoopt.load_state(search)
	state['setup'] = {'status': 'open'}
	autoopt.save_state(search, state)
	out = call([AUTOOPT, 'loop', search], expect=(1,))
	suite.check('loop refuses while step 2 is open', 'step 2 is still with '
		'the setup subagent' in out, out)
	state = autoopt.load_state(search)
	state['setup'] = {'status': 'done'}
	state['mining'] = {'stage': 1, 'done': 1}
	autoopt.save_state(search, state)
	call([AUTOOPT, 'config', search, '--set', 'loop_handback=2', '--reason',
		'test'])

	done = subprocess.run([sys.executable, AUTOOPT, 'loop', search],
		capture_output=True, text=True)
	prompt = open(os.path.join(search, 'briefs', 'loop-1.md')).read()
	suite.check('loop opens phase 1 and prints a short prompt: its file, '
		'SKILL.md and the loop, with status --resume', done.returncode == 0
		and all(name in done.stdout for name in ('briefs/loop-1.md',
			'SKILL.md', 'references/step3-rounds.md', 'status'))
		and '--resume' in done.stdout and 'Orchestrator: phase 1' in done.stderr
		and 'the session\'s model' in done.stderr, done.stdout + done.stderr)
	suite.check('its prompt says when to hand back and what to report',
		'this phase has seen 2 iterations settle' in prompt
		and '## What to report back' in prompt and 'loop {} --done'.format(
			os.path.dirname(search)) in prompt, prompt)
	out = call([AUTOOPT, 'loop', search], expect=(1,))
	suite.check('a second phase is refused while one is open', 'phase 1 of the '
		'orchestrator subagent is open' in out, out)
	call([AUTOOPT, 'loop', search, '--force'])
	out = call([AUTOOPT, 'status', search, '--brief'])
	suite.check('--force opens the next, and status shows its progress',
		'orchestrator subagent: phase 2, since ' in out and '0 of 2 '
		'iterations settled in it' in out, out[:900])

	for identifier in (1, 2):
		add(search, identifier, 0.5 + identifier / 10)
		with open(os.path.join(autoopt.iteration_dir(search, identifier),
				'notes.md'), 'w') as handle:
			handle.write(NOTES)
		call([AUTOOPT, 'finish', search, '--id', str(identifier)], expect=(0,))
	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- [MICRO] bench.py:score: x. Why: y. Gain: unmeasured. '
			'Exact: bit-identical. (S1)\n')
	call([AUTOOPT, 'new', search, '--id', '3', '--title', 'in flight',
		'--category', 'MISC'])
	out = call([AUTOOPT, 'brief', search, '--next'], expect=(0,))
	suite.check('at its count, brief --next briefs nothing more and waits for '
		'what is in flight', 'nothing more is briefed in it' in out
		and 'Hand back once the 1 in flight have reported' in out
		and not os.path.exists(os.path.join(search, 'briefs', '4.md')), out[-600:])
	bench(autoopt.iteration_dir(search, 3), 0.6)
	call([RUNNER, autoopt.iteration_dir(search, 3)])
	with open(os.path.join(autoopt.iteration_dir(search, 3), 'notes.md'),
			'w') as handle:
		handle.write(NOTES)
	call([AUTOOPT, 'finish', search, '--id', '3'], expect=(0,))
	out = call([AUTOOPT, 'brief', search, '--next'], expect=(0,))
	suite.check('and with nothing in flight, says to hand back now',
		'Nothing is in flight: hand back now' in out, out[-400:])
	call([AUTOOPT, 'loop', search, '--done'])
	out = call([AUTOOPT, 'brief', search, '--next'], expect=(0,))
	suite.check('once the phase is closed, brief --next briefs again',
		os.path.exists(os.path.join(search, 'briefs', '4.md'))
		and 'orchestrator subagent: phase' not in out, out[-600:])


def tree_reference(suite, base):
	"""In tree mode, briefs, status and reports name the root as the keep
	rule's reference, and a better median outside the root's path as best,
	not in the root."""

	search = make_search(os.path.join(base, 'tree-reference'), promote='auto', staged=False, runs=2)
	add(search, 1, 0.9)
	call([AUTOOPT, 'new', search, '--id', '2', '--parent', '1', '--title',
		'a hair better', '--category', 'MISC'])
	bench(autoopt.iteration_dir(search, 2), 0.9015)
	call([RUNNER, autoopt.iteration_dir(search, 2)])
	root = autoopt.load_result(search, 1)['objective']['median']
	best = autoopt.load_result(search, 2)['objective']['median']
	line = ('Root: A1, median {}, the keep rule\'s reference; best '
		'median, not in the root: A2, {} (kept out by the noise band)'.format(
			autoopt.fmt(root), autoopt.fmt(best)))
	out = call([AUTOOPT, 'status', search, '--brief'])
	suite.check('status names the root as the reference, and the better '
		'median kept out', line in out, out[:1200])
	call([AUTOOPT, 'brief', search, '--id', '3', '--title', 't', '--category',
		'MISC', '--idea', 'x', '--tried', 'none'])
	text = open(os.path.join(search, 'briefs', '3.md')).read()
	suite.check('and so does a brief', line in text, text[:1500])
	call([AUTOOPT, 'new', search, '--brief', '3'])
	bench(autoopt.iteration_dir(search, 3), 0.9)
	call([RUNNER, autoopt.iteration_dir(search, 3)])
	report = open(os.path.join(autoopt.iteration_dir(search, 3),
		'report.txt')).read()
	suite.check('and the report\'s best-so-far row, beside the parent row',
		re.search(r'\nbest so far +{} at start, held by A2, not in the root; '
			r'noise band'.format(re.escape(autoopt.fmt(best))), report)
		and re.search(r'\nparent +A1 ', report), report)

	config = autoopt.load_config(search)
	suite.equal('best, what final and path take, is the root in tree '
		'mode, not the better median kept out', autoopt.best_id(search, config),
		autoopt.load_state(search)['root'])
	head = autoopt.load_state(search)['root']
	with open(os.path.join(search, 'ideas.md'), 'a') as handle:
		handle.write('- [MICRO] bench.py:score: draw once. Why: twice. Gain: '
			'unmeasured. Exact: bit-identical. (S1)\n')
	out = call([AUTOOPT, 'brief', search, '--next', '--count', '1'])
	ident = max(autoopt.pending_briefs(search))
	text = open(os.path.join(search, 'briefs', '{}.md'.format(ident))).read()
	suite.check('the spawn prompt names the iteration with its label',
		'You are running iteration A{} of'.format(ident) in out, out[-900:])
	suite.check('and Already tried lists the root\'s changes to the '
		'file', re.search(r'## Already tried on this code\n\nA1 ', text)
		and 'none: no iteration' not in text, text[:2500])
	suite.equal('Already tried matches an idea line\'s bare filename or path '
		'tail against the repository paths an iteration changed',
		[autoopt.names_file('src/main/java/p/MarkDuplicates.java',
			'MarkDuplicates.java'),
		autoopt.names_file('src/main/java/p/MarkDuplicates.java',
			'p/MarkDuplicates.java'),
		autoopt.names_file('src/main/java/p/SimpleMarkDuplicates.java',
			'MarkDuplicates.java'),
		autoopt.names_file('bench.py', './bench.py')],
		[True, True, False, True])


def every_help(suite, base):
	"""Every subcommand's --help formats: a bare % in a help string breaks
	it only when someone asks."""

	parser = autoopt.build_parser()
	choices = next(action.choices for action in parser._actions
		if isinstance(action, autoopt.argparse._SubParsersAction))
	broken = []
	for name, sub in choices.items():
		try:
			sub.format_help()
		except (ValueError, TypeError) as error:
			broken.append('{}: {}'.format(name, error))
	try:
		parser.format_help()
	except (ValueError, TypeError) as error:
		broken.append('autoopt.py: {}'.format(error))
	suite.check('autoopt.py --help and every subcommand\'s --help format',
		not broken, broken)


def token_audit(suite, base):
	"""`tokens`: what a search's agents spent, from transcripts that name it
	in their own words, by role."""

	search = make_search(os.path.join(base, 'tokens'), staged=False, runs=2)
	work = os.path.dirname(search)
	projects = os.path.join(base, 'tokens-projects')
	folder = os.path.join(projects, 'proj')
	agents = os.path.join(folder, 'main1', 'subagents')
	os.makedirs(agents)

	def turn(time, request, usage, calls=()):
		content = [{'type': 'tool_use', 'name': name, 'input': data}
			for name, data in calls]
		return json.dumps({'type': 'assistant', 'timestamp': time,
			'requestId': request, 'message': {'content': content,
				'usage': usage}})

	def user(time, text):
		return json.dumps({'type': 'user', 'timestamp': time,
			'message': {'content': text}})

	reads = {'cache_read_input_tokens': 1000}
	write = {'cache_creation_input_tokens': 4000, 'cache_read_input_tokens': 100}
	with open(os.path.join(folder, 'main1.jsonl'), 'w') as out:
		out.write('\n'.join([
			user('2026-01-01T00:00', 'something else'),
			turn('2026-01-01T00:01', 'r0', {'input_tokens': 9999}),
			turn('2026-01-01T01:00', 'r1', reads, [('Agent', {'prompt':
				'read {}/autooptimize/briefs/setup.md'.format(work)})]),
			turn('2026-01-01T02:00', 'r2', write)]) + '\n')
	with open(os.path.join(folder, 'observer.jsonl'), 'w') as out:
		out.write(turn('2026-01-01T03:00', 'o1', reads, [('Bash', {'command':
			'autoopt.py status {}'.format(work)})]) + '\n')
	for name, description, lines in (
			('a1', 'Setup subagent: step 2', [user('2026-01-01T01:01',
				'You are the setup subagent: {}'.format(work)),
				turn('2026-01-01T01:02', 's1', write),
				turn('2026-01-01T01:03', 's2', reads)]),
			('a2', 'A1: one idea', [user('2026-01-01T01:05', 'go'),
				turn('2026-01-01T01:06', 'i1', reads, [('Bash', {'command':
					'cat {}/autooptimize/knowledge.md'.format(work)})])]),
			('a3', 'Another search', [json.dumps({'type': 'user',
				'timestamp': '2026-01-01T01:07', 'message': {'content': [{
					'type': 'tool_result', 'content': 'ps: ' + work}]}}),
				turn('2026-01-01T01:08', 'x1', {'input_tokens': 5000})])):
		with open(os.path.join(agents, 'agent-{}.jsonl'.format(name)),
				'w') as out:
			out.write('\n'.join(lines) + '\n')
		with open(os.path.join(agents, 'agent-{}.meta.json'.format(name)),
				'w') as out:
			json.dump({'description': description}, out)

	out = call([AUTOOPT, 'tokens', search, '--projects', projects, '--json'])
	rows = {row['agent']: row for row in json.loads(out)}
	suite.check('tokens counts the session that spawned agents for the search, '
		'from its first mention, and the agents that name it themselves',
		sorted(rows) == ['A1: one idea', 'Setup subagent: step 2',
			'session main1'] and rows['session main1']['turns'] == 2
		and abs(rows['session main1']['weighted'] - (50 + 5005)) < 1e-6
		and rows['session main1']['rebuilds'] == 1, rows)
	suite.check('by role, never counting a search named only in a tool\'s '
		'output or a session that only looked', rows['Setup subagent: step '
		'2']['role'] == 'setup' and rows['A1: one idea']['role']
		== 'iterations' and 'Another search' not in rows
		and 'session observer' not in rows, rows)
	out = call([AUTOOPT, 'tokens', search, '--projects', projects, '--json',
		'--session', 'observer'])
	suite.check('--session names the main session instead',
		sorted(row['agent'] for row in json.loads(out)) == ['A1: one idea',
			'Setup subagent: step 2', 'session observer'], out)
	out = call([AUTOOPT, 'tokens', search, '--projects', projects])
	suite.check('and the table gives each role and the mean per iteration',
		re.search(r'\nsetup +1 +2 +', out) and 'per iteration: 0k weighted, '
		'mean of 1' in out, out)


def waiting(suite, base):
	"""`wait`: a background command watched in calls of at most four
	minutes, each printing only the lines that are new."""

	folder = os.path.join(base, 'waiting')
	os.makedirs(folder)
	log = os.path.join(folder, 'x.log')
	script = ('for i in 1 2 3; do echo "line $i"; sleep 1; done; '
		'echo "exit 3"')
	job = subprocess.Popen(['sh', '-c', '({}) > {} 2>&1'.format(script,
		shlex.quote(log))])

	started = time.monotonic()
	first = call([AUTOOPT, 'wait', log, '--max', '1'], expect=(0,))
	suite.check('wait returns at --max while the command runs',
		time.monotonic() - started < 10 and 'still running after 1 s' in first,
		first)
	second = call([AUTOOPT, 'wait', log, '--max', '30'], expect=(0,))
	suite.check('and at the command\'s `exit <code>` line, saying so',
		'wait: finished, exit 3' in second, second)
	shown = first + second
	suite.check('every line is shown exactly once across the calls',
		all(shown.count('line {}\n'.format(i)) == 1 for i in (1, 2, 3)),
		shown)
	third = call([AUTOOPT, 'wait', log, '--max', '30'], expect=(0,))
	suite.check('a call after it finished prints nothing new',
		third.strip() == 'wait: finished, exit 3', third)
	job.wait()

	sleeper = subprocess.Popen(['sleep', '30'])
	quiet = os.path.join(folder, 'quiet.log')
	out = call([AUTOOPT, 'wait', quiet, '--pid', str(sleeper.pid), '--max',
		'1'], expect=(0,))
	suite.check('a live --pid keeps it waiting', 'still running' in out, out)
	sleeper.kill()
	sleeper.wait()
	out = call([AUTOOPT, 'wait', quiet, '--pid', str(sleeper.pid), '--max',
		'30'], expect=(0,))
	suite.check('a --pid that is gone finishes it, with no exit line',
		'pid {} is no longer running'.format(sleeper.pid) in out, out)

	out = call([AUTOOPT, 'wait', log, '--max', '241'], expect=(1,))
	suite.check('--max above four minutes is refused',
		'between 1 and 240 seconds' in out, out)
	out = call([AUTOOPT, 'wait', '--help'], expect=(0,))
	suite.check('and the default fits inside a 240000 ms Bash timeout',
		'default 200' in out, out[-400:])

	noisy = os.path.join(folder, 'noisy.log')
	with open(noisy, 'w') as handle:
		handle.write(''.join('row {}\n'.format(i) for i in range(500))
			+ 'exit 0\n')
	out = call([AUTOOPT, 'wait', noisy, '--max', '5'], expect=(0,))
	suite.check('a burst of output is cut to its last lines',
		'… 441 earlier new lines in' in out and 'row 499' in out
		and 'row 400\n' not in out, out[:300])

	search = make_search(os.path.join(base, 'waiting-brief'), staged=False)
	out = call([AUTOOPT, 'brief', search, '--id', '1', '--title', 'quick',
		'--category', 'MISC', '--idea', 'x', '--tried', 'none'], expect=(0,))
	suite.check('a brief says how long the parent\'s runner took and to run '
		'a quick one in the foreground', re.search(r'On A0 the runner took '
		r'\d+ s: run it in the foreground', out), out[-1500:])
	path = os.path.join(autoopt.iteration_dir(search, 0), 'result.json')
	with open(path) as handle:
		record = json.load(handle)
	record['seconds'] = 600.0
	with open(path, 'w') as handle:
		json.dump(record, handle)
	out = call([AUTOOPT, 'brief', search, '--id', '1', '--replace', '--title',
		'slow', '--category', 'MISC', '--idea', 'x', '--tried', 'none'],
		expect=(0,))
	suite.check('and a slow one in the background, watched with wait',
		'On A0 the runner took 10 min: start it with `--background` and '
		'watch `probe/runner.log` with `autoopt.py wait`' in out, out[-1500:])


def racy_index(suite, base):
	"""current_tree sees a same-size edit made in the second the index was
	written, which git trusts the index about once its copy is newer."""

	checkout = os.path.join(base, 'racy')
	os.makedirs(checkout)
	git('init', '-q', checkout)
	path = os.path.join(checkout, 'f.txt')
	for attempt in range(10):
		# The add and the edit inside one second, the copy in a later one.
		while time.time() % 1 > 0.3:
			time.sleep(0.05)
		second = int(time.time())
		with open(path, 'w') as out:
			out.write('aaaa\n')
		git('-C', checkout, 'add', 'f.txt')
		with open(path, 'w') as out:
			out.write('bbbb\n')
		if int(time.time()) == second:
			break
	while int(time.time()) == second:
		time.sleep(0.05)
	got = autoopt.current_tree(checkout)
	blob = git('-C', checkout, 'hash-object', 'f.txt').strip()
	listed = git('-C', checkout, 'ls-tree', got).split()
	suite.check('current_tree stages a same-size edit made in the second '
		'the index was written', blob in listed, (blob, listed))


def main(argv=None):
	import argparse

	# No options: the parser is there so --help prints this instead of
	# running the suite.
	argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter).parse_args(argv)

	suite = Suite()
	base = tempfile.mkdtemp(prefix='autoopt-test-')

	try:
		print('\nseeds')
		search = make_search(os.path.join(base, 'seeds'), runs=3,
			confirm_runs=6)
		baseline = autoopt.load_result(search, 0)

		# An improvement large enough to earn the confirmation stage, and a
		# regression far enough below it not to.
		better = add(search, 1, 0.60)
		worse = add(search, 2, 0.30)

		suite.equal('the baseline runs the confirmation depth',
			seeds_of(baseline), [0, 1, 2, 3, 4, 5])
		suite.equal('a screened iteration runs the first seeds',
			seeds_of(worse), [0, 1, 2])
		suite.equal('a confirmed iteration extends the same seeds',
			seeds_of(better), [0, 1, 2, 3, 4, 5])
		suite.check('every iteration shares the seeds it ran',
			set(seeds_of(worse)) <= set(seeds_of(better))
			and set(seeds_of(better)) == set(seeds_of(baseline)),
			'{} vs {} vs {}'.format(seeds_of(baseline), seeds_of(better),
				seeds_of(worse)))

		# Same seed, same benchmark, same number: the property the whole
		# comparison rests on.
		repeat = add(search, 3, 0.50)
		suite.check('the same seed gives the same value in another iteration',
			[round(v, 12) for v in repeat['objective']['values'][:3]]
			== [round(v, 12) for v in baseline['objective']['values'][:3]],
			'{} vs {}'.format(repeat['objective']['values'][:3],
				baseline['objective']['values'][:3]))

		print('\nstaged evaluation')
		suite.equal('an improvement is confirmed', better['stage'],
			'confirmed')
		suite.equal('a regression stops at the screening depth',
			worse['stage'], 'screen')
		suite.check('the objective is the median, not the best',
			abs(better['objective']['median']
				- statistics.median(better['objective']['values'])) < 1e-12
			and better['objective']['median']
				< max(better['objective']['values']))

		print('\ngates and status')
		broken = add(search, 4, 0.90, gate=2.0)
		suite.equal('a broken gate fails the iteration however good',
			broken['status'], 'gate-failed')
		suite.equal('and it is not confirmed', broken['stage'], 'screen')
		suite.check('a gate-failed iteration is off the frontier',
			autoopt.frontier(autoopt.all_results(search), 'max')[1]
			!= broken['id'])

		crashed = add(search, 5, 0.70, fail_on=1)
		suite.equal('a crashed run makes the iteration an error',
			crashed['status'], 'error')
		suite.equal('and the seed is named', [f['seed']
			for f in crashed['failures']], [1])

		partial = add(search, 6, 0.99, runs=1, stage='screen')
		suite.equal('an under-depth iteration is partial',
			partial['status'], 'partial')
		suite.check('and cannot hold the frontier despite the best score',
			autoopt.frontier(autoopt.all_results(search), 'max')[1] != 6)

		print('\nresuming')
		directory = autoopt.iteration_dir(search, 1)
		out = call([RUNNER, directory])
		suite.check('finished seeds are not run again',
			out.count('already done') == 6, out.count('already done'))
		suite.equal('and a re-run does not demote a confirmed iteration',
			autoopt.load_result(search, 1)['n_runs'], 6)
		suite.check('a re-run of the iteration holding the frontier does not '
			'announce a new one', 'NEW FRONTIER' not in out
			and 'still holds the frontier' in out, out[-300:])
		directory = autoopt.iteration_dir(search, 7)
		call([AUTOOPT, 'new', search, '--id', '7', '--title', 'iteration 7',
			'--category', 'MISC'])
		bench(directory, 0.80)
		out = call([RUNNER, directory])
		suite.check('a genuinely better iteration announces the new frontier '
			'against the one it beat', 'NEW FRONTIER: 0.8' in out
			and 'beats 0.6' in out, out[-300:])
		out = call([RUNNER, directory, '--full-tests'])
		suite.check('and its re-run does not announce it again',
			'NEW FRONTIER' not in out, out[-300:])

		print('\nparallel runs')
		# Six runs that sleep a second each. Serially that is >= 6s; four at
		# a time it should be about half that, and the record must come back
		# in seed order however the pool finished.
		import time as clock
		wide = os.path.join(base, 'wide')
		os.makedirs(wide, exist_ok=True)
		call([AUTOOPT, 'init', wide, '--task', 'test',
			'--command', 'python3 bench.py --seed {seed} --out {out}',
			'--objective', 'score', '--direction', 'max', '--no-gpu',
			'--runs', '6', '--staged', '--confirm-runs', '6', '--gpus-per-agent', '4'])

		search_wide = os.path.join(wide, 'autooptimize')
		bench(autoopt.iteration_dir(search_wide, 0), 0.50, sleep=1)

		started = clock.time()
		call([RUNNER, autoopt.iteration_dir(search_wide, 0)])
		elapsed = clock.time() - started

		result = autoopt.load_result(search_wide, 0)
		suite.equal('every seed ran', result['n_runs'], 6)
		suite.equal('the record is in seed order',
			[run['seed'] for run in result['runs']], [0, 1, 2, 3, 4, 5])
		suite.check('four at a time beat six serially',
			elapsed < 5.0, '{:.1f}s for 6 one-second runs'.format(elapsed))
		suite.check('the median is unaffected by completion order',
			abs(result['objective']['median']
				- statistics.median(result['objective']['values'])) < 1e-12)

		print('\nminimising')
		low = make_search(os.path.join(base, 'min'), runs=3, confirm_runs=6,
			direction='min')
		call([AUTOOPT, 'new', low, '--id', '1', '--title', 'lower',
			'--category', 'MISC'])
		bench(autoopt.iteration_dir(low, 1), 0.20)
		call([RUNNER, autoopt.iteration_dir(low, 1)])
		suite.equal('lower wins when direction is min',
			autoopt.frontier(autoopt.all_results(low), 'min')[1], 1)

		print('\nlabels')
		multi = make_search(os.path.join(base, 'multi'))
		add(multi, 1, 0.60)
		add(multi, 2, 0.70)
		call([AUTOOPT, 'promote', multi, '--id', '2'])
		add(multi, 3, 0.65)
		add(multi, 4, 0.66)
		results = autoopt.all_results(multi)
		suite.equal('the frontier is seeded by the baseline',
			autoopt.search_frontier(multi, results, 'max')[1], 2)

		named = make_search(os.path.join(base, 'labels'))
		add(named, 1, 0.60)
		add(named, 2, 0.70)
		call([AUTOOPT, 'promote', named, '--id', 'A2'])
		add(named, 3, 0.75)
		out = call([AUTOOPT, 'status', named])
		suite.check('the status table names an iteration with its label',
			any(line.startswith('A3') for line in out.splitlines()), out)
		suite.check('and the root line too', 'root=A2' in out,
			out.splitlines()[0])
		out = call([AUTOOPT, 'promote', named, '--id', 'A3'], expect=(0,))
		suite.check('promote takes A3', 'iteration A3' in out, out)
		out = call([AUTOOPT, 'promote', named, '--id', 'B3'], expect=(1,))
		suite.check('and refuses B3: every label carries A',
			'every label is A3' in out, out)
		out = call([AUTOOPT, 'new', named, '--id', 'B9', '--title', 'x',
			'--category', 'MISC'], expect=(1,))
		suite.check('new --id B9 is refused', 'labels are A9' in out
			and not os.path.exists(autoopt.iteration_dir(named, 9)), out)
		out = call([AUTOOPT, 'new', named, '--id', 'A9', '--parent', 'A3',
			'--title', 'x', '--category', 'MISC'])
		suite.equal('new takes labels for --id and --parent',
			autoopt.load_meta(named, 9)['parent'], 3)
		out = call([AUTOOPT, 'path', named, '--id', 'A3'])
		suite.check('path prints labels', 'A0 -> A2 -> A3' in out,
			out.splitlines()[0])

		print('\nprune: regenerable outputs')
		pr_search = make_search(os.path.join(base, 'prune-outputs'))
		call([AUTOOPT, 'new', pr_search, '--id', '1', '--title', 'frontier',
			'--category', 'MISC'])
		kept_dir = autoopt.iteration_dir(pr_search, 1)            # protected: the frontier
		with open(os.path.join(kept_dir, 'data.npy'), 'w') as out:
			out.write('tracked')                                   # committed by the runner: code
		bench(kept_dir, 0.60)
		call([RUNNER, kept_dir])
		def touch(*parts):
			path = os.path.join(*parts)
			os.makedirs(os.path.dirname(path), exist_ok=True)
			with open(path, 'w') as out:
				out.write('x' * 1000)
			return path
		probe_out = touch(kept_dir, 'probe', 'variant_out.npz')
		prediction = touch(kept_dir, 'runs', 'seed-0', 'predictions.npy')
		other = touch(pr_search, 'elsewhere', 'r1', 'reference', 'seed-0.npz')
		call([AUTOOPT, 'new', pr_search, '--id', '5', '--title', 'running',
			'--category', 'MISC'])
		running_out = touch(autoopt.iteration_dir(pr_search, 5), 'probe', 'out.npz')
		report = call([AUTOOPT, 'prune', pr_search])
		suite.check('prune reports the regenerable outputs without deleting',
			'saved output array' in report and os.path.exists(probe_out), report)
		call([AUTOOPT, 'prune', pr_search, '--apply'])
		suite.check('it deletes outputs from a protected iteration',
			not os.path.exists(probe_out) and not os.path.exists(prediction), '')
		suite.check('and keeps tracked code, a folder that is not an '
			'iteration\'s and a running iteration', os.path.exists(os.path.join(
			kept_dir, 'data.npy')) and os.path.exists(other) and
			os.path.exists(running_out), '')
		probe_out = touch(kept_dir, 'probe', 'variant_out.npz')
		autoopt.OUTPUT_FINDERS.append(lambda root, extensions: [other])
		plan = autoopt.prune_plan(pr_search, autoopt.load_config(pr_search),
			set(), set())
		autoopt.OUTPUT_FINDERS.pop()
		suite.check('a registered finder adds its outputs to the plan',
			other in plan['outputs'] and probe_out in plan['outputs'], plan[
			'outputs'])
		keep = touch(kept_dir, 'probe', 'again.npz')
		call([AUTOOPT, 'prune', pr_search, '--apply', '--keep-outputs'])
		suite.check('--keep-outputs leaves them', os.path.exists(keep), '')

		print('\nworking subagents and overdue summaries')
		ws = make_search(os.path.join(base, 'working'))
		add(ws, 1, 0.60)
		call([AUTOOPT, 'promote', ws, '--id', '1'])        # round 1 closes, all settled
		out = call([AUTOOPT, 'status', ws])
		suite.check('status reminds when a closed, settled round has no summary',
			'summary overdue for round(s) 1' in out, out)
		call([AUTOOPT, 'summary', ws, '--round', '1'])
		out = call([AUTOOPT, 'status', ws])
		suite.check('and stops once the summary exists', 'summary overdue' not in out, out)
		add(ws, 2, 0.40)                     # measured; no README yet
		call([AUTOOPT, 'new', ws, '--id', '3', '--title', 'in flight',
			'--category', 'MISC'])                       # running: no result yet
		out = call([AUTOOPT, 'status', ws])
		suite.check('status lists subagents still working, running and finishing',
			'A3 running' in out and 'A2 finishing' in out, out)
		old = time.time() - 3000
		for d in (autoopt.iteration_dir(ws, 3),):
			for base_, _, names in os.walk(d):
				for name in names:
					os.utime(os.path.join(base_, name), (old, old))
		out = call([AUTOOPT, 'status', ws])
		suite.check('and flags one silent for over 15 minutes as STALLED',
			'A3 running' in out and ', STALLED)' in out, out)

		print('\nreset')
		# A round summary, and an iteration whose round is not 1,
		# so the reset has something of every kind to clean.
		call([AUTOOPT, 'summary', multi, '--round', '1'])
		before = autoopt.load_result(multi, 2)['objective']['median']

		out = call([AUTOOPT, 'reset', multi, '--keep', '0,3',
			'--new-root', '3'])
		suite.check('reset reports without --apply',
			'Nothing removed' in out, out)
		suite.equal('and removes nothing',
			len(autoopt.iteration_ids(multi)), 5)

		call([AUTOOPT, 'reset', multi, '--keep', '0,3', '--new-root', '3',
			'--apply'])

		suite.equal('reset keeps exactly what was asked for',
			autoopt.iteration_ids(multi), [0, 3])
		suite.equal('the kept measurement survives',
			autoopt.load_result(multi, 0)['objective']['median'] is not None,
			True)
		suite.equal('every kept iteration returns to round 1, and A0 keeps '
			'round 0 in result.json and meta.json', [autoopt.load_result(multi,
				i)['round'] for i in (0, 3)] + [autoopt.load_meta(multi, 0)[
				'round']], [0, 1, 0])

		state = autoopt.load_state(multi)
		suite.equal('the state is rebuilt on the new root, on round 1',
			(state['root'], state['round']), (3, 1))

		branches = autoopt.git('-C', os.path.join(multi, 'repo.git'),
			'branch', '--format=%(refname:short)').split()
		suite.equal('only the kept branches remain', sorted(branches),
			['autoopt/0', 'autoopt/3'])

		loose = autoopt.git('-C', os.path.join(multi, 'repo.git'),
			'count-objects', '-v')
		suite.check('unreachable objects are pruned',
			'count: 0' in loose, loose.strip())

		suite.equal('the round summaries are gone',
			os.listdir(os.path.join(multi, 'rounds')), [])

		# The point of all of it: the search still runs.
		add(multi, 4, 0.55)
		results = autoopt.all_results(multi)
		state = autoopt.load_state(multi)
		suite.equal('the search still runs after a reset',
			autoopt.load_result(multi, 4)['status'], 'ok')
		suite.equal('and is still judged against the kept frontier',
			autoopt.search_frontier(multi, results, 'max')[1], 3)

		out = call([AUTOOPT, 'reset', multi, '--keep', '99'], expect=(1,))
		suite.check('reset refuses an id that does not exist',
			'does not exist' in out, out)

		# --- external validation ------------------------------------
		# Optional, run once a round against the root, recorded and never
		# gated on. The property that matters most is the sign-off: it can
		# be slow and it is the user's compute.
		print('\nexternal validation')

		check = os.path.join(base, 'validation.py')

		with open(check, 'w') as handle:
			handle.write(
				'import json, os, sys, argparse\n'
				'p = argparse.ArgumentParser()\n'
				"p.add_argument('--out', required=True)\n"
				"p.add_argument('--iteration', default='')\n"
				"p.add_argument('--verdict', default='good')\n"
				'a = p.parse_args()\n'
				'os.makedirs(a.out, exist_ok=True)\n'
				"json.dump({'measures': {'n_datasets': 12,\n"
				"	'n_mismatches_all': 0 if a.verdict == 'good' else 7},\n"
				"	'ok': a.verdict == 'good', 'note': 'synthetic'},\n"
				"	open(os.path.join(a.out, 'validation.json'), 'w'))\n")

		# init refuses a command that cannot be substituted.
		bad = os.path.join(base, 'badval')
		os.makedirs(bad, exist_ok=True)
		out = call([AUTOOPT, 'init', bad, '--task', 't',
			'--command', 'python3 bench.py --seed {seed} --out {out}',
			'--objective', 'score', '--direction', 'max', '--no-gpu',
			'--validation-command', 'python3 validation.py',
			'--validation-measures', 'n_datasets'], expect=(1,))
		suite.check('init refuses a validation command without {iteration}',
			'{iteration}' in out, out)

		nomeasure = os.path.join(base, 'nomeasure')
		os.makedirs(nomeasure, exist_ok=True)
		out = call([AUTOOPT, 'init', nomeasure, '--task', 't',
			'--command', 'python3 bench.py --seed {seed} --out {out}',
			'--objective', 'score', '--direction', 'max', '--no-gpu',
			'--validation-command',
			'python3 v.py --iteration {iteration} --out {out}'], expect=(1,))
		suite.check('and one that measures nothing',
			'measures' in out, out)

		ext = make_search(os.path.join(base, 'ext'),
			validation_command='python3 {} --out {{out}} --iteration '
				'{{iteration}}'.format(check),
			validation_measures='n_datasets,n_mismatches_all',
			validation_note='12 synthetic databases')

		config = autoopt.load_config(ext)
		suite.check('init records the validation unapproved',
			config['external_validation']['approved'] is False,
			json.dumps(config['external_validation']))

		out = call([AUTOOPT, 'validate', ext], expect=(1,))
		suite.check('validate refuses without sign-off',
			'not been approved' in out, out)
		suite.check('and says whose decision it is',
			"user's compute" in out, out)
		suite.check('and nothing was written',
			not os.path.exists(os.path.join(ext, 'validations')),
			str(os.listdir(ext)))

		out = call([AUTOOPT, 'validate', ext, '--approve'])
		suite.check('--approve records the sign-off',
			autoopt.load_config(ext)['external_validation']['approved'],
			out)
		suite.check('and runs nothing',
			not os.path.exists(os.path.join(ext, 'validations')), out)

		out = call([AUTOOPT, 'validate', ext, '--approve', '--round', '1'],
			expect=(1,))
		suite.check('--approve with a flag for the run refuses',
			'runs nothing' in out, out)

		add(ext, 1, 0.60)
		call([AUTOOPT, 'promote', ext, '--id', '1'])

		out = call([AUTOOPT, 'validate', ext, '--round', '1', '--id', '1'])
		record = autoopt.load_validation(ext, 1)

		suite.check('validate writes the round record', record is not None,
			out)
		suite.equal('against the iteration it was given',
			record['iteration'], 1)
		suite.equal('and carries the measures the script emitted',
			record['measures']['n_datasets'], 12)
		suite.check('the block says it is not a gate',
			'not a gate' in out, out)

		before = autoopt.load_result(ext, 1)

		# A failing validation is a finding, not a verdict.
		call([AUTOOPT, 'validate', ext, '--round', '2', '--id', '1'])
		worse = os.path.join(ext, 'validations', 'round-2')
		os.makedirs(worse, exist_ok=True)
		subprocess.run([sys.executable, check, '--out', worse,
			'--verdict', 'bad'], check=True)
		call([AUTOOPT, 'validate', ext, '--round', '2', '--id', '1'])

		after = autoopt.load_result(ext, 1)
		suite.equal('a failing validation leaves the status alone',
			after['status'], before['status'])
		suite.equal('and the objective alone',
			after['objective']['median'], before['objective']['median'])

		best, best_id = autoopt.frontier(autoopt.all_results(ext), 'max')
		suite.equal('and the frontier alone', best_id, 1)

		out = call([AUTOOPT, 'summary', ext, '--round', '1'])
		suite.check('the round summary carries the validation',
			'External validation' in out, out[:400])
		suite.check('and says it gated nothing',
			'never gated on' in out, out[:400])

		# A subagent validating its own iteration by number: files where it
		# says, and no round record to confuse with the round's own.
		add(ext, 2, 0.55)
		mine = os.path.join(autoopt.iteration_dir(ext, 2), 'validation')
		rounds_before = sorted(os.listdir(os.path.join(ext, 'validations')))
		out = call([AUTOOPT, 'validate', ext, '--id', '2', '--out', mine])
		suite.check('validate --id --out writes into the given directory',
			os.path.exists(os.path.join(mine, 'record.json'))
			and os.path.exists(os.path.join(mine, 'validation.json')),
			out)
		suite.equal('against the iteration named by number',
			json.load(open(os.path.join(mine, 'record.json')))['iteration'], 2)
		suite.equal('and records no round',
			sorted(os.listdir(os.path.join(ext, 'validations'))),
			rounds_before)
		out = call([AUTOOPT, 'validate', ext, '--out', mine], expect=(1,))
		suite.check('--out without --id refuses', '--id' in out, out)

		print('\npredictions')
		pred = make_search(os.path.join(base, 'predict'))
		call([AUTOOPT, 'new', pred, '--id', '1', '--title', 'guess',
			'--category', 'MISC', '--predict', '0.05', '--predict-basis',
			'a sibling measured +0.05'])
		directory = autoopt.iteration_dir(pred, 1)
		bench(directory, 0.60)
		call([RUNNER, directory])
		got = autoopt.load_result(pred, 1)
		suite.equal('the prediction reaches result.json', got.get('prediction'),
			{'delta': 0.05, 'basis': 'a sibling measured +0.05'})
		plain = add(pred, 2, 0.40)
		suite.equal('an iteration made without --predict records none',
			plain.get('prediction'), None)
		out = call([AUTOOPT, 'summary', pred, '--round', '1'])
		line = [l for l in out.splitlines() if 'iteration A1,' in l]
		suite.check('the summary sets the prediction against the measurement',
			bool(line) and 'predicted 0.05, measured 0.1' in line[0]
			and 'a sibling measured +0.05' in line[0], line)
		other = [l for l in out.splitlines() if 'iteration A2,' in l]
		suite.check('and says nothing for an iteration without one',
			bool(other) and 'predicted' not in other[0], other)

		print('\nrepository mode')
		repo_mode(suite, base)

		print('\nunit-test subsets')
		test_subset(suite, base)

		print('\nidea miner')
		idea_miner(suite, base)

		print('\ntask checks')
		task_checks(suite, base)

		print('\nautomatic promotion')
		auto_promotion(suite, base)

		print('\nbookkeeping: struck ideas and ledger corrections')
		bookkeeping(suite, base)

		print('\nsetup: step 2 for a subagent')
		setup_step(suite, base)

		print('\nsingle count of runs')
		single_count(suite, base)

		print('\nmeasuring scripts')
		measuring_scripts(suite, base)

		print('\nshared compiler cache')
		numba_cache(suite, base)


		print('\nstep 0 gate')
		spec_gate(suite, base)

		print('\nrestore')
		restore_guard(suite, base)

		print('\ninit --force')
		init_force(suite, base)

		print('\nno git identity')
		no_identity(suite, base)

		print('\ngit without worktree add --orphan')
		old_git(suite, base)

		print('\nplain-mode excludes')
		plain_excludes(suite, base)

		print('\nGPU assignment')
		device_queue(suite, base)

		print('\ntimeouts and interrupts')
		process_groups(suite, base)

		print('\npaths with a space')
		spaces(suite, base)

		print('\nJSON records')
		json_files(suite, base)

		print('\nsmall interface checks')
		small_fixes(suite, base)
		one_publisher(suite, base)
		unlisted_publisher(suite, base)

		print('\nabandoned iterations')
		abandoned_ids(suite, base)

		print('\ncontrols')
		leftover_controls(suite, base)

		print('\npromoting off the root')
		off_root_promote(suite, base)

		print('\nthe unit-test suite is one commit')
		suite_squashed(suite, base)

		print('\nbriefs, READMEs, bodies, summaries, config')
		reporting(suite, base)

		print('\ntree rules')
		tree_rules(suite, base)

		print('\nphase noise band')
		phase_band(suite, base)

		print('\nwait')
		waiting(suite, base)

		print('\none run first')
		one_run_first(suite, base)
		resuming(suite, base)
		watching(suite, base)
		variants_and_proofs(suite, base)
		profiling(suite, base)
		inventories(suite, base)
		quiet_check(suite, base)
		orchestrator_phases(suite, base)
		tree_reference(suite, base)
		token_audit(suite, base)
		every_help(suite, base)
		racy_index(suite, base)

		print('\nfinish')
		finishing(suite, base)

		print('\nbrief --next')
		next_briefs(suite, base)

		print('\nstep 2 tools')
		setup_tools(suite, base)

	finally:
		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed),
		len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
