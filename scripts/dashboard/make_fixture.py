"""Build a synthetic `autooptimize/` tree covering every state the page draws.

	python3 make_fixture.py --out <new dir>              # about a dozen iterations
	python3 make_fixture.py --out <new dir> --scale 2600 # a real search's size

Prints the path to the search directory. `--out` is required and must not
hold an `autooptimize/` yet. The tree is built by driving
`autoopt.py` and `run_iteration.py` exactly as a subagent would, rather than
by writing `result.json` files directly: a fixture that fabricates the payload
stops catching the bugs where the runner and the page disagree about it.

It covers a passing baseline, iterations in all five categories, a gate
failure, a run that crashes, an iteration still in flight, two rounds with a
promoted root, observer measures, an approved external validation run
once a round (clean in round 1, needing attention in round 2, with one
measure a string), and a `worker.py` inside the in-flight iteration that a test launches so the GPU monitor's
process table has a row.
"""

import os
import sys
import json
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)


BENCH = '''\
"""A stand-in benchmark: fast, noisy, and gated, like the real thing."""

import os
import json
import random
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()

THROUGHPUT = {throughput}
LATENCY = {latency}
ALLOCS = {allocs}
CRASH_ON = {crash_on}

if CRASH_ON is not None and args.seed == CRASH_ON:
	raise RuntimeError('CUDA out of memory')

random.seed(args.seed * 7919 + ALLOCS)

value = THROUGHPUT + random.gauss(0, 0.011)
os.makedirs(args.out, exist_ok=True)

with open(os.path.join(args.out, 'metrics.json'), 'w') as handle:
	json.dump({{
		'objective': value,
		'gates': {{'latency_ms': LATENCY + random.gauss(0, 0.22)}},
		'observers': {{'allocs': ALLOCS, 'compile_seconds': 6.0 + ALLOCS / 5e5}},
	}}, handle)

print('seed {{}} -> {{:.4f}}'.format(args.seed, value))
'''


VALIDATE = '''\
"""A stand-in external validation: the root checked on inputs the search
never measures. Clean in round 1; round 2's root is off on one input."""

import os
import json
import argparse

parser = argparse.ArgumentParser()
parser.add_argument('--iteration', required=True)
parser.add_argument('--round', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()

with open(os.path.join(args.iteration, 'result.json')) as handle:
	value = json.load(handle)['objective']['median']

bad = args.round >= 2
os.makedirs(args.out, exist_ok=True)

with open(os.path.join(args.out, 'validation.json'), 'w') as handle:
	json.dump({'measures': {
			'n_inputs': 12,
			'max_abs_error': 0.0031 if bad else 0.0,
			'heldout_throughput': round(value - 0.041, 4),
			'worst_input': 'input 07, the widest record' if bad else 'none',
		}, 'units': {'max_abs_error': 'abs. difference'}, 'ok': not bad,
		'note': 'input 07 differs past 1e-3' if bad else ''}, handle)
'''


# (title, category, throughput, latency, allocs, crash_on, readme)
IDEAS = [
	('struct-of-arrays record layout', 'CACHE_MISS', 0.601, 8.2, 2.1e6, None,
		'One array per field instead of one object per record. Clearly '
		'faster and still inside the latency gate, but the margin is thin -- '
		'anything else that adds a pass will break it.'),
	('prefetch the next block', 'CACHE_MISS', 0.624, 11.4, 3.4e6,
		None, 'Faster per record, slower per call: the prefetch thread starts '
		'cold, so every one of the ten runs broke the latency gate.'),
	('njit the inner loop', 'NUMBA', 0.607, 7.9, 1.0e6, None,
		'The record loop under @njit with cache=True. Stable across every '
		'seed; the variance across seeds halved, which is worth as much as '
		'the median.'),
	('prange over records', 'NUMBA', 0.588, 7.8, 1.0e6, None,
		'Raced on two seeds until the per-thread buffers were split. '
		'Reported numbers are with the split buffers.'),
	('memoize the per-key lookup', 'CACHING', 0.616, 7.9, 1.0e6, None,
		'A dictionary in front of the per-key lookup. The gain is real but '
		'arrives only once the cache is warm, so a shorter input would miss it.'),
	('cache the parsed header', 'CACHING', 0.572, 7.8, 1.0e6, None,
		'Slower. Reparsing the header was apparently cheaper than the lookup.'),
	('hoist the bounds check', 'MICRO', 0.598, 7.6, 1.0e6, None,
		'Within noise of the baseline, at a quarter of the branches, so this '
		'is a latency win rather than a throughput one.'),
	('branchless compare', 'MICRO', 0.565, 7.8, 1.0e6, None,
		'Slower. Two seeds hit the slow path on every record.'),
	('reorder the imports', 'MISC', 0.594, 7.8, 1.0e6, None, ''),
	('memory-mapped input', 'MISC', 0.599, 7.7, 1.0e6, 1,
		'Crashed on seed 1 with an OOM while mapping the input. Not '
		'retried -- the failure looks deterministic in the map size.'),
]


def call(args, **kwargs):
	out = subprocess.run([sys.executable] + args, capture_output=True,
		text=True, **kwargs)

	if out.returncode not in (0, 2):
		raise RuntimeError('{} failed:\n{}\n{}'.format(' '.join(args),
			out.stdout, out.stderr))

	return out.stdout


def bench(directory, throughput, latency, allocs, crash_on):
	with open(os.path.join(directory, 'bench.py'), 'w') as out:
		out.write(BENCH.format(throughput=throughput, latency=latency,
			allocs=int(allocs), crash_on=crash_on))


def build(out_dir, quiet=True):
	root = os.path.join(out_dir, 'autooptimize')

	if os.path.exists(root):
		raise RuntimeError('{} exists; build the fixture into a new '
			'directory'.format(root))

	os.makedirs(out_dir, exist_ok=True)

	autoopt = os.path.join(SCRIPTS, 'autoopt.py')
	validator = os.path.join(out_dir, 'validate.py')

	with open(validator, 'w') as out:
		out.write(VALIDATE)
	runner = os.path.join(SCRIPTS, 'run_iteration.py')

	call([autoopt, 'init', out_dir,
		'--task', 'Maximise throughput without raising latency.',
		'--command', 'python3 bench.py --seed {seed} --out {out}',
		'--objective', 'throughput', '--direction', 'max', '--unit', '',
		'--gates', json.dumps([{'name': 'latency_ms', 'op': '<=',
			'threshold': 10.0}]),
		'--observers', 'allocs,compile_seconds',
		'--runs', '3', '--staged', '--confirm-runs', '10',
		'--promote', 'orchestrator', '--no-gpu',
		'--validation-command', 'python3 ' + validator
			+ ' --iteration {iteration} --round {round} --out {out}',
		'--validation-measures', 'n_inputs,max_abs_error,heldout_throughput,worst_input',
		'--validation-note', '12 held-out inputs the search never measures'])

	# Step 0 answered, as a real search would have it before anything runs.
	spec = os.path.join(root, 'spec.md')
	with open(spec) as infile:
		text = infile.read()
	with open(spec, 'w') as out:
		out.write(text.replace('TODO', 'answered'))

	# A unit for one measure in config.json, the way a user adds one; the
	# validation script names another, and n_inputs has none.
	path = os.path.join(root, 'config.json')
	with open(path) as handle:
		config = json.load(handle)
	config['external_validation']['units'] = {'heldout_throughput': 'records/s'}
	with open(path, 'w') as handle:
		json.dump(config, handle, indent='\t')

	bench(os.path.join(root, 'iterations', '0'), 0.594, 7.8, 1000000, None)
	call([runner, os.path.join(root, 'iterations', '0')])

	write_readme(root, 0, 'baseline', 'MISC',
		'The starting point: a pure-Python loop over the records.')

	# Round 1: the first five ideas, all forked from the baseline. The idea
	# miner's first stage lands before them and a second one before the
	# fourth, so the page's stage boundaries (one per miner stage) fall where
	# the tree's generations do not.
	for index, idea in enumerate(IDEAS[:5], start=1):
		if index in (1, 4):
			call([autoopt, 'mine', root])
			stage = 1 if index == 1 else 2
			os.makedirs(os.path.join(root, 'mining'), exist_ok=True)
			with open(os.path.join(root, 'mining', 'stage-{}.md'.format(
					stage)), 'w') as handle:
				handle.write('- [CACHE_MISS] bench.py:scan: an idea of stage S{0}. '
					'Why: the fixture. Gain: unmeasured. Exact: changes outputs. '
					'(S{0})\n'.format(stage))
			call([autoopt, 'mine', root, '--done'])
		run_idea(root, autoopt, runner, index, idea, parent=0)

	# The best of round 1 is validated, then becomes the root, and round 2
	# forks from it. The sign-off is recorded first, as a separate call.
	promoted = best(root)
	call([autoopt, 'validate', root, '--approve'])
	call([autoopt, 'validate', root, '--round', '1', '--id', str(promoted)])
	call([autoopt, 'summary', root, '--round', '1'])
	call([autoopt, 'promote', root, '--id', str(promoted)])

	for index, idea in enumerate(IDEAS[5:], start=6):
		run_idea(root, autoopt, runner, index, idea, parent=promoted)

	call([autoopt, 'validate', root, '--round', '2', '--id', str(best(root))])
	call([autoopt, 'summary', root, '--round', '2'])

	# One iteration forked but not yet run, so the page has something in
	# flight to draw.
	call([autoopt, 'new', root, '--id', '11', '--title',
		'tiled traversal of the record blocks', '--category',
		'CACHE_MISS', '--topic', 'cache_miss'])
	bench(os.path.join(root, 'iterations', '11'), 0.63, 9.1, 3400000, None)

	# A stand-in for its benchmark run, which a test launches so the process
	# table has a row naming iteration 11.
	with open(os.path.join(root, 'iterations', '11', 'worker.py'), 'w') as out:
		out.write('import time\nwhile True:\n\ttime.sleep(5)\n')

	# And one left at the smoke-test depth, which is what an interrupted
	# subagent leaves behind: a result that must not reach the frontier.
	call([autoopt, 'new', root, '--id', '12', '--title',
		'one-seed smoke test, never finished', '--category', 'MISC',
		'--topic', 'misc'])
	directory = os.path.join(root, 'iterations', '12')
	bench(directory, 0.70, 8.0, 1000000, None)
	call([runner, directory, '--runs', '1', '--stage', 'screen'])

	write_readme(root, 12, 'one-seed smoke test, never finished', 'MISC',
		'Interrupted after the smoke run. One seed only, so it is not a '
		'measurement and cannot reach the frontier however good it looks.')

	# And one closed without a run and set aside, as the handoff procedure
	# does with an empty fork (references/resume.md): its directory is
	# renamed to 13.abandoned and its worktree pruned. The page marks it on the
	# objective chart at the baseline's value.
	call([autoopt, 'new', root, '--id', '13', '--title',
		'one pass instead of two, closed without a run', '--category', 'MISC',
		'--topic', 'misc'])
	write_readme(root, 13, 'one pass instead of two, closed without a run', 'MISC',
		'Closed after hand measurements showed the merged form changes results. '
		'No code was changed, so nothing was run.')
	os.rename(os.path.join(root, 'iterations', '13'),
		os.path.join(root, 'iterations', '13.abandoned'))
	subprocess.run(['git', '-C', os.path.join(root, 'repo.git'), 'worktree', 'prune'],
		check=True)

	return root


def taken_ids(directory):
	"""Every id an iteration directory holds, set-aside ones (`13.abandoned`)
	included, so a new id never collides with one."""

	return [int(name.split('.')[0]) for name in os.listdir(directory)
		if name.split('.')[0].isdigit()]


def run_idea(root, autoopt, runner, index, idea, parent):
	title, category, throughput, latency, allocs, crash_on, readme = idea

	call([autoopt, 'new', root, '--id', str(index), '--parent', str(parent),
		'--title', title, '--category', category,
		'--topic', category.lower()])

	directory = os.path.join(root, 'iterations', str(index))
	bench(directory, throughput, latency, allocs, crash_on)
	call([runner, directory])

	write_readme(root, index, title, category, readme)


def best(root):
	sys.path.insert(0, SCRIPTS)
	import autoopt as lifecycle

	value, identifier = lifecycle.frontier(lifecycle.all_results(root), 'max')

	return identifier if identifier is not None else 0


def write_readme(root, identifier, title, category, notes):
	directory = os.path.join(root, 'iterations', str(identifier))
	path = os.path.join(directory, 'README.md')

	try:
		with open(os.path.join(directory, 'result.json')) as infile:
			result = json.load(infile)
	except OSError:
		result = {}

	objective = result.get('objective') or {}
	lines = ['# Iteration {}: {}'.format(identifier, title), '',
		'**Category:** {}'.format(category), '',
		'## What was tried', '', notes or 'No notes were recorded.', '',
		'## Numbers', '',
		'- throughput: {} median over {} runs'.format(
			objective.get('median'), result.get('n_runs')),
		'- status: {}'.format(result.get('status')), '']

	with open(path, 'w') as out:
		out.write('\n'.join(lines))


def inflate(root, n, seed=0):
	"""Clone the fixture's settled iterations up to `n` of them.

	For testing how the page behaves at the scale a real search reaches --
	thousands of iterations, which is where the lineage diagram and the
	payload size stop being free. The clones are real runner output with
	perturbed numbers rather than invented rows, so the format cannot drift
	from what `run_iteration.py` writes; no benchmark is re-run, because this
	tests rendering, not measurement.

	Returns the number of iterations added.
	"""

	import random

	source = os.path.join(root, 'iterations')
	templates = []

	for identifier in sorted(int(name) for name in os.listdir(source)
		if name.isdigit()):

		meta = os.path.join(source, str(identifier), 'meta.json')
		result = os.path.join(source, str(identifier), 'result.json')

		if os.path.exists(result):
			readme = os.path.join(source, str(identifier), 'README.md')
			templates.append((json.load(open(meta)), json.load(open(result)),
				open(readme).read() if os.path.exists(readme) else None))

	if not templates:
		raise RuntimeError('nothing to clone; build the fixture first')

	rng = random.Random(seed)
	start = max(taken_ids(source)) + 1
	best = max(t[1]['objective']['median'] for t in templates)
	added = 0

	for identifier in range(start, n):
		meta, result, readme = [json.loads(json.dumps(part))
			for part in rng.choice(templates)]

		value = best + rng.gauss(0, 0.02)

		# A rare genuine improvement, so the frontier path grows with the
		# search the way a real one does.
		if rng.random() < 0.004:
			best = value = best + abs(rng.gauss(0, 0.004))

		meta.update(id=identifier, parent=0, round=2 + identifier // 20,
			branch='autoopt/{}'.format(identifier))
		result.update(id=identifier, parent=0, round=meta['round'],
			branch=meta['branch'], title=meta['title'], status='ok')
		result['objective']['median'] = value
		result['objective']['values'] = [value + rng.gauss(0, 0.01)
			for _ in range(3)]

		directory = os.path.join(source, str(identifier))
		os.makedirs(directory, exist_ok=True)

		with open(os.path.join(directory, 'meta.json'), 'w') as out:
			json.dump(meta, out)
		with open(os.path.join(directory, 'result.json'), 'w') as out:
			json.dump(result, out)
		# A settled iteration writes its README last; without one the
		# scanner reads the clone as a subagent still at work.
		with open(os.path.join(directory, 'README.md'), 'w') as out:
			out.write(readme or '# {}\n\nA clone for scale.\n'.format(
				meta['title']))

		added += 1

	return added


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)

	parser.add_argument('--out', required=True, help='a directory with no '
		'autooptimize/ in it yet')
	parser.add_argument('--scale', type=int, help='clone the result up to '
		'this many iterations, for testing the page at the scale a real '
		'search reaches')

	args = parser.parse_args(argv)

	root = build(os.path.abspath(os.path.expanduser(args.out)))

	if args.scale:
		print('cloned {} extra iterations'.format(inflate(root, args.scale)))

	print(root)

	return 0


if __name__ == '__main__':
	sys.exit(main())
