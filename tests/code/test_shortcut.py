#!/usr/bin/env python3
"""The compiler shortcut, end to end: a search on a small C program where the
obvious win is a build flag, driven with the real scripts.

	python3 test_shortcut.py     # about a minute; needs gcc and make

Each case is an iteration that takes a shortcut. The runner measures it (a
build-flag change really is several times faster), and the guards must keep
it from becoming the root: `promote` refuses the changed build and a vendored
library, a changed harness stops the run, and an exported `CFLAGS` never
reaches the build. An honest source edit is promoted. Skipped without gcc.
"""

import os
import sys
import json
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
import test_runner as tr

AUTOOPT, RUNNER = tr.AUTOOPT, tr.RUNNER

HOT_C = r"""#include <stdio.h>
#include <stdlib.h>

#define N 150000000u

static unsigned f(unsigned i, unsigned s) {
	return (i * s + 7u) % 1000003u;
}

int main(int argc, char **argv) {
	unsigned s = (unsigned)atoi(argv[1]);
	unsigned long long total = 0;

	for (unsigned i = 0; i < N; i++) {
		unsigned x = f(i, s);
		total += (unsigned long long)x * x;
	}

	printf("%llu\n", total);
	return 0;
}
"""

MAKEFILE = ("CC ?= gcc\nCFLAGS = -O1\n\nprog: hot.c\n"
	"\t$(CC) $(CFLAGS) -o prog hot.c\n\nclean:\n\trm -f prog\n")

HARNESS = r'''import os, sys, json, argparse, subprocess, time

parser = argparse.ArgumentParser()
parser.add_argument('--src', required=True)
parser.add_argument('--seed', type=int, required=True)
parser.add_argument('--out', required=True)
args = parser.parse_args()
os.makedirs(args.out, exist_ok=True)

subprocess.run(['make', '-s', 'clean', 'prog'], cwd=args.src, check=True)
start = time.perf_counter()
done = subprocess.run(['./prog', str(args.seed)], cwd=args.src, check=True,
	capture_output=True, text=True)
seconds = time.perf_counter() - start

# The reference output for a seed: the baseline source built at -O0, once.
here = os.path.dirname(os.path.abspath(__file__))
reference = os.path.join(os.path.dirname(here), 'reference', 'ref-%d.txt' % args.seed)

if not os.path.exists(reference):
	os.makedirs(os.path.dirname(reference), exist_ok=True)
	binary = os.path.join(args.out, 'ref')
	subprocess.run(['gcc', '-O0', '-o', binary, os.path.join(args.src, 'hot.c')], check=True)
	text = subprocess.run([binary, str(args.seed)], capture_output=True, text=True, check=True).stdout
	open(reference, 'w').write(text)

with open(os.path.join(args.out, 'metrics.json'), 'w') as handle:
	json.dump({'objective': seconds, 'gates': {'n_mismatch': 0 if done.stdout == open(reference).read() else 1}, 'observers': {}}, handle)
'''

HONEST = """\
	unsigned v = 7u;

	for (unsigned i = 0; i < N; i++) {
		unsigned x = v % 1000003u;
		total += (unsigned long long)x * x;
		v += s;
	}

"""


def run(*args, env=None):
	return subprocess.run(args, capture_output=True, text=True, env=env)


def main():
	suite = tr.Suite()

	if not shutil.which('gcc') or not shutil.which('make'):
		print('skipped: gcc and make are needed')
		return 0

	base = tempfile.mkdtemp(prefix='shortcut-')
	environment = dict(os.environ, AUTOOPT_SKIP_SPEC_CHECK='1')
	environment.pop('CC', None)
	environment.pop('CFLAGS', None)
	project = os.path.join(base, 'project')
	os.makedirs(project)

	for name, text in (('hot.c', HOT_C), ('Makefile', MAKEFILE)):
		with open(os.path.join(project, name), 'w') as out:
			out.write(text)

	tr.git('init', '-q', '-b', 'main', project)
	tr.git('-C', project, 'add', '-A')
	tr.git('-C', project, '-c', 'user.email=t@e', '-c', 'user.name=t',
		'commit', '-q', '-m', 'base')
	harness = os.path.join(base, 'bench.py')

	with open(harness, 'w') as out:
		out.write(HARNESS)

	work = os.path.join(base, 'search')
	search = os.path.join(work, 'autooptimize')
	done = run(sys.executable, AUTOOPT, 'init', work, '--task', 'checksum',
		'--repo', project, '--copy', harness, '--command',
		'python3 {}/harness/bench.py --src {{src}} --seed {{seed}} --out '
		'{{out}}'.format(search), '--objective', 'seconds', '--direction',
		'min', '--gates', json.dumps([{'name': 'n_mismatch', 'op': '==',
		'threshold': 0}]), '--no-gpu', '--runs', '3', '--promote',
		'orchestrator', '--build-env', 'CC=gcc', env=environment)
	suite.equal('init', done.returncode, 0)
	done = run(sys.executable, RUNNER, os.path.join(search, 'iterations',
		'0'), env=environment)
	suite.equal('the baseline runs', done.returncode, 0)
	autoopt.record_harness(search)
	zero = autoopt.load_result(search, 0)['objective']['median']

	def new(identifier):
		done = run(sys.executable, AUTOOPT, 'new', search, '--id',
			str(identifier), '--parent', '0', '--title', 'case', '--category',
			'MISC', env=environment)
		return done.stdout.strip().splitlines()[-1]

	def edit(path, old, new_text):
		with open(path) as handle:
			text = handle.read()

		assert old in text, (path, old)

		with open(path, 'w') as out:
			out.write(text.replace(old, new_text, 1))

	def measure(identifier, extra=None):
		return run(sys.executable, RUNNER, os.path.join(search, 'iterations',
			str(identifier)), env=dict(environment, **(extra or {})))

	def promote(identifier):
		return run(sys.executable, AUTOOPT, 'promote', search, '--id',
			str(identifier), env=environment)

	# 1. The shortcut: build flags in the Makefile.
	directory = new(1)
	edit(os.path.join(directory, 'src', 'Makefile'), '-O1',
		'-O3 -march=native -ffast-math')
	measure(1)
	median = autoopt.load_result(search, 1)['objective']['median']
	suite.check('the flags really are faster (the shortcut is real)',
		median < zero * 0.7, (zero, median))
	done = promote(1)
	suite.check('promote refuses the changed build', done.returncode != 0
		and 'no `## Toolchain` answer excuses it' in done.stderr,
		done.stderr)
	meta = autoopt.load_meta(search, 1)
	meta['toolchain'] = 'no'
	autoopt.write_meta(search, 1, meta)
	suite.check('and answering no does not change that',
		promote(1).returncode != 0, '')
	_, lines = autoopt.write_round_summary(search, autoopt.load_config(search),
		1)
	suite.check('the round summary lists the answered-no build change',
		'## Toolchain answers to check' in lines and any('A1' in line
		and 'Makefile' in line for line in lines), lines[-12:])

	# 2. The same flags exported in the shell: scrubbed.
	new(2)
	measure(2, {'CFLAGS': '-O3 -march=native -ffast-math'})
	median = autoopt.load_result(search, 2)['objective']['median']
	suite.check('an exported CFLAGS never reaches the build',
		median > zero * 0.8, (zero, median))

	# 3. A vendored prebuilt library.
	directory = new(3)
	os.makedirs(os.path.join(directory, 'src', 'vendor'))

	with open(os.path.join(directory, 'src', 'vendor', 'libfast.a'),
			'w') as out:
		out.write('!<arch>\n')

	measure(3)
	done = promote(3)
	suite.check('promote refuses a vendored library', done.returncode != 0
		and 'vendored' in done.stderr, done.stderr)

	# 4. An edit to the harness's build step stops the next run.
	edit(os.path.join(search, 'harness', 'bench.py'), "'prog']",
		"'prog', 'CFLAGS=-Ofast']")
	new(4)
	done = measure(4)
	suite.check('a changed harness refuses to run', done.returncode != 0
		and 'harness changed' in done.stderr, done.stderr)
	edit(os.path.join(search, 'harness', 'bench.py'),
		"'prog', 'CFLAGS=-Ofast']", "'prog']")

	# 5. An honest edit of the source is promoted.
	directory = new(5)
	path = os.path.join(directory, 'src', 'hot.c')

	with open(path) as handle:
		text = handle.read()

	start = text.index('\tfor (unsigned i = 0;')
	end = text.index('\tprintf')

	with open(path, 'w') as out:
		out.write(text[:start] + HONEST + text[end:])

	measure(5)
	suite.equal('an honest source edit is measured',
		autoopt.load_result(search, 5)['status'], 'ok')
	done = promote(5)
	suite.check('and promoted', done.returncode == 0 and 'root is now'
		in done.stdout, (done.stdout, done.stderr))

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
