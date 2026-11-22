#!/usr/bin/env python3
"""Find out whether the benchmark's seed controls what it should.

	python3 check_seeding.py autooptimize/iterations/0

Two things have to be true before a search means anything, and they pull in
opposite directions:

  1. **The same seed gives the same answer.** Whatever is left over when the
     seed is held fixed is noise no amount of averaging attributes to an
     idea.
  2. **A different seed gives a different answer.** If it does not, the runs
     are one number repeated, the noise band comes out at zero, and every
     keep verdict after that is made on a single sample counted
     as several.

So this runs the benchmark twice on one seed and twice more on two others,
and reports the two spreads side by side. What matters is their ratio: a
same-seed spread near zero means the seed controls everything; a same-seed
spread as large as the between-seed spread means it controls nothing that
matters and the search cannot tell an idea from a rerun.

It also reads the benchmark's source for the usual unseeded sources of
randomness, which is where the answer usually is when the numbers come out
wrong.

Runs are written under `<iteration>/runs/seedcheck-*` and do not disturb the
iteration's own results. They share one compiler cache, `{cache}`, at
`<iteration>/cache/seedcheck`, emptied before the check and removed after it
unless `--keep`.

Exit codes:
  0  OK: same seed reproduces, or its spread is small (also --scan-only)
  1  WARNING: repeating a seed moves the objective by half or more of what
     changing it does
  2  FAIL: different seeds gave identical answers
  3  refused or could not run: bad arguments, no GPU lease, a run failed
"""

import os
import re
import sys
import shutil
import argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autoopt
import run_iteration
from autoopt import fail, fmt


# Sources of randomness worth naming, and the call that seeds each. Written
# without `\b`, which does not fire next to an underscore.
SOURCES = [
	('python `random`', r'(?<![a-z0-9_])random\.(random|randint|randrange|choice|choices|shuffle|sample|uniform|gauss|normalvariate)\(',
		r'random\.seed\('),
	('numpy legacy global', r'(?<![a-z0-9_])(numpy|np)\.random\.(rand|randn|randint|random|random_sample|choice|shuffle|permutation|normal|standard_normal|uniform|binomial|poisson|exponential|beta|gamma|bytes)\(',
		r'(numpy|np)\.random\.seed\('),
	('numpy Generator', r'default_rng\(|(?<![a-z0-9_])(rng|generator|gen)\.(integers|random|normal|choice|permutation|shuffle|standard_normal|uniform)\(', r'default_rng\(\s*[^)\s]'),
	('torch CPU', r'torch\.(rand|randn|randint|randperm|normal|bernoulli|multinomial|rand_like|randn_like|randint_like)\(|(?<![a-z0-9_])(?:F|torch\.nn\.functional)\.dropout\d?d?\(|nn\.Dropout',
		r'torch\.manual_seed\('),
	('torch CUDA', r'\.(cuda|to\([\'"]?cuda)', r'torch\.cuda\.manual_seed(_all)?\('),
	('DataLoader shuffling', r'DataLoader\(', r'(worker_init_fn|generator)\s*='),
	('scikit-learn', r'(train_test_split|KFold|RandomForest|KMeans)\(',
		r'random_state\s*='),
]

# Randomness that cannot be seeded at all. Any of these in a benchmark puts a
# floor under the same-seed spread that no amount of seeding removes.
UNSEEDABLE = re.compile(r'SystemRandom\(|os\.urandom\(|uuid4\(|'
	r'seed\s*=\s*(time\.time|int\(time)')

# A seeding call handed a literal, such as `random.seed(0)` in a benchmark
# that takes `--seed`. CONSTANT_SEED does not match this call form.
SEED_CALL = re.compile(
	r'(?<![a-z0-9_])((?:random|numpy|np|torch|torch\.cuda)\.)?'
	r'(manual_seed_all|manual_seed|default_rng|seed)\(\s*(\d+)\s*\)')

# A seed parameter handed a constant, such as `random_state=0`.
CONSTANT_SEED = re.compile(
	r'(random_state|seed|random_seed)\s*=\s*(\d+)(?![\w.])')


def strip_prose(text):
	"""Blank out comments and string literals, keeping line numbers."""

	out = re.sub(r'("""|\'\'\')(?:.|\n)*?\1',
		lambda m: '\n' * m.group(0).count('\n'), text)
	out = re.sub(r'#[^\n]*', '', out)
	out = re.sub(r'"[^"\n]*"|\'[^\'\n]*\'', "''", out)

	return out


def scan(directory):
	"""Read the benchmark's source for randomness it may not be seeding."""

	findings, constants, unseedable = [], [], []

	for base, _, names in os.walk(directory):
		if '.git' in base.split(os.sep) or 'runs' in base.split(os.sep):
			continue

		for name in sorted(names):
			if not name.endswith('.py'):
				continue

			path = os.path.join(base, name)

			try:
				with open(path) as infile:
					text = infile.read()
			except OSError:
				continue

			# A comment or docstring that mentions torch.cuda is not a use
			# of it, so prose is blanked before the scan.
			text = strip_prose(text)
			relative = os.path.relpath(path, directory)

			for label, uses, seeds in SOURCES:
				if re.search(uses, text) and not re.search(seeds, text):
					findings.append((relative, label))

			for pattern in (CONSTANT_SEED, SEED_CALL):
				for match in pattern.finditer(text):
					line = text[:match.start()].count('\n') + 1
					constants.append((relative, line, match.group(0)))

			for match in UNSEEDABLE.finditer(text):
				line = text[:match.start()].count('\n') + 1
				unseedable.append((relative, line,
					match.group(0).rstrip('(')))

	return findings, constants, unseedable


def one_run(config, iteration, seed, label, cache):
	"""Run the benchmark once into its own directory, and read the result."""

	run_dir = os.path.join(iteration, 'runs', label)
	shutil.rmtree(run_dir, ignore_errors=True)

	code, seconds = run_iteration.execute(config['command'], iteration, seed,
		run_dir, config.get('timeout_seconds'),
		src=autoopt.checkout_dir(autoopt.resolve(os.path.dirname(
			os.path.dirname(iteration))), os.path.basename(iteration), config),
		cache=cache)

	record, error = run_iteration.load_metrics(run_dir, config)

	if code != 0 or record is None:
		fail('the run with seed {} failed ({}); see {}/log.txt'.format(seed,
			error or 'exit code {}'.format(code), run_dir))

	return record['objective'], seconds


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)

	parser.add_argument('iteration', help='autooptimize/iterations/<id>, '
		'usually the baseline')
	parser.add_argument('--seed', type=int, help='the seed to repeat '
		'(default: the search\'s first seed)')
	parser.add_argument('--repeats', type=int, default=2,
		help='runs on the repeated seed (default: %(default)s)')
	parser.add_argument('--others', type=int, default=2,
		help='runs on further seeds (default: %(default)s)')
	parser.add_argument('--scan-only', action='store_true',
		help='read the source and stop, running nothing')
	parser.add_argument('--keep', action='store_true',
		help='keep the runs/seedcheck-* directories')

	args = parser.parse_args(argv)

	iteration = os.path.abspath(os.path.expanduser(args.iteration))

	if not os.path.isdir(iteration):
		fail('not a directory: {}'.format(iteration))

	root = autoopt.resolve(os.path.dirname(os.path.dirname(iteration)))
	config = autoopt.load_config(root)
	seed = args.seed if args.seed is not None else config['seed_base']

	# In repository mode the benchmark is the harness; the package in src/
	# takes its seed from the harness, and scanning its tests would cry wolf.
	source = os.path.join(root, 'harness') if config.get('repo') else iteration

	print('reading {} for unseeded randomness'.format(source))
	findings, constants, unseedable = scan(source)

	for relative, label in findings:
		print('  ? {}: uses {} and never seeds it'.format(relative, label))

	for relative, line, name in unseedable:
		print('  ? {}:{}: `{}` cannot be seeded at all, so it puts a floor '
			'under the same-seed spread below.'.format(relative, line, name))

	if not (findings or unseedable):
		print('  every source it recognizes is seeded')

	for relative, line, text in constants:
		print('  ? {}:{}: `{}` is a literal. If that is the run seed it has '
			'to come from --seed, or every run is the same run.'.format(
				relative, line, text))

	if args.scan_only:
		return 0

	run_iteration.check_lease(config)

	print('\nrunning seed {} {} times, then {} further seed(s)'.format(seed,
		args.repeats, args.others))

	# One cache for every run of the check: they all run the same code, and
	# a fresh one keeps a cache from before an edit out of the comparison.
	cache = os.path.join(iteration, 'cache', 'seedcheck')
	shutil.rmtree(cache, ignore_errors=True)
	os.makedirs(cache)

	same = []
	for index in range(args.repeats):
		value, seconds = one_run(config, iteration, seed,
			'seedcheck-same-{}'.format(index), cache)
		same.append(value)
		print('  seed {} (run {}): {} in {:.1f} min'.format(seed, index + 1,
			fmt(value), seconds / 60.0))

	others = []
	for index in range(args.others):
		other = seed + 1 + index
		value, seconds = one_run(config, iteration, other,
			'seedcheck-other-{}'.format(index), cache)
		others.append(value)
		print('  seed {}: {} in {:.1f} min'.format(other, fmt(value),
			seconds / 60.0))

	same_spread = (max(same) - min(same)) if len(same) > 1 else 0.0
	across = same[:1] + others
	across_spread = (max(across) - min(across)) if len(across) > 1 else 0.0

	print('\nsame-seed spread     {}'.format(fmt(same_spread)))
	print('between-seed spread  {}'.format(fmt(across_spread)))

	verdict = 0
	runs = 'from {} runs on one seed and {} distinct seeds'.format(
		args.repeats, args.others + 1)

	if across_spread == 0:
		print('\nFAIL: different seeds gave identical answers. The seed is '
			'not reaching the randomness, so the runs are one number '
			'repeated and the noise band will be zero. Find what is still '
			'fixed -- a hard-coded random_state is the usual cause -- before '
			'running the baseline.')
		verdict = 2
	elif same_spread == 0:
		print('\nOK: the same seed reproduces exactly, and different seeds '
			'move the objective. Differences between iterations are '
			'attributable to the idea.')
	else:
		ratio = same_spread / across_spread

		print('\nRepeating a seed moves the objective by {:.0%} of what '
			'changing it does ({}).'.format(ratio, runs))

		if ratio >= 0.5:
			print('\nWARNING: that is most of the variation, so the seed '
				'controls less than half of what moves the objective. A '
				'median over seeds is then averaging noise the search cannot '
				'attribute to any idea. If the seed is meant to vary the '
				'result, look for the unseeded source; either way, expect to '
				'need more runs per iteration.')
			verdict = 1
		elif ratio > 0.2:
			print('\nWorth chasing before the search starts: it widens the '
				'noise band, and the noise band is the keep rule\'s margin.')
		else:
			print('\nOK: most of what varies is the seed, so differences '
				'between iterations are attributable to the idea.')

		# references/step2-baseline.md: on a fixed input the ratio is chance.
		print('\nIf the benchmark runs a fixed input, so the seed changes '
			'nothing (a timing harness), both spreads are machine noise and '
			'this verdict is about noise, not the seed. The noise band is '
			'still right; say so in the README. Letting the seed choose the '
			'input or its order makes the runs independent repeats.')

		print('\nBoth spreads are estimated from a handful of runs and are '
			'themselves noisy. Near the line, re-run with --repeats 5 '
			'--others 5 before believing either.')

	if not args.keep:
		for name in os.listdir(os.path.join(iteration, 'runs')):
			if name.startswith('seedcheck-'):
				shutil.rmtree(os.path.join(iteration, 'runs', name),
					ignore_errors=True)
		shutil.rmtree(cache, ignore_errors=True)

	return verdict


def logged():
	"""main(), its output copied into setup/seeding.log when it checks the
	baseline, as step 2 does."""

	target = next((arg for arg in sys.argv[1:] if not arg.startswith('-')), '')
	target = os.path.abspath(os.path.expanduser(target))
	root = os.path.dirname(os.path.dirname(target))

	# Every exit that is not a verdict -- argparse's 2, fail()'s 1 -- becomes
	# 3, so 1 and 2 only ever mean WARNING and FAIL.
	def verdict():
		try:
			return main()
		except SystemExit as error:
			return 3 if error.code not in (0, None) else 0

	if os.path.basename(target) != '0' or not os.path.exists(os.path.join(
			root, 'config.json')):
		return verdict()

	with autoopt.StepLog(root, 'seeding') as log:
		code = verdict()
		if code:
			raise SystemExit(code)
	return code


if __name__ == '__main__':
	sys.exit(logged())
