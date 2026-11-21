#!/usr/bin/env python3
"""Run every test file in this directory and print one line for each.

	python3 run_tests.py                  # all of test_*.py, in parallel
	python3 run_tests.py test_runner      # only these (names, without .py)
	python3 run_tests.py --jobs 1

Each file runs as its own process with its own output; a file that exits
non-zero, or whose last "N passed, M failed" line is missing or counts a
failure, is reported with the tail of its output. Exit 0 only when every file
passed. The dashboard's browser tests (`dashboard/`, which need Playwright)
are not included.

Standard library only, Python 3.8 or later.
"""

import os
import re
import sys
import time
import argparse
import subprocess
import concurrent.futures

HERE = os.path.dirname(os.path.abspath(__file__))
RESULT = re.compile(r'(\d+) passed, (\d+) failed')
PYTEST = {}


def pytest_counts(text):
	"""(passed, failed) from pytest's last line, `2 failed, 53 passed in 5 s`."""

	line = [l for l in text.splitlines() if re.search(r'\d+ (passed|failed|'
		r'error)', l)]
	line = line[-1] if line else ''
	count = lambda word: int((re.search(r'(\d+) ' + word, line) or [0, 0])[1])

	return count('passed'), count('failed') + count('error')


def run_one(name):
	"""(name, passed, failed, seconds, ok, tail of the output)."""

	start = time.time()

	if name in PYTEST:
		done = subprocess.run([sys.executable, '-m', 'pytest', '-q',
			PYTEST[name]], capture_output=True, text=True, cwd=os.path.dirname(
			HERE), stdin=subprocess.DEVNULL)
		passed, failed = pytest_counts(done.stdout)
		found = [(passed, failed)] if passed or failed else []
	else:
		done = subprocess.run([sys.executable, os.path.join(HERE, name +
			'.py')], capture_output=True, text=True, cwd=HERE,
			stdin=subprocess.DEVNULL)
		found = RESULT.findall(done.stdout)
		passed, failed = (int(found[-1][0]), int(found[-1][1])) if found \
			else (0, 0)

	ok = done.returncode == 0 and bool(found) and failed == 0
	tail = '\n'.join((done.stdout + done.stderr).rstrip().split('\n')[-12:])

	return name, passed, failed, time.time() - start, ok, tail


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('names', nargs='*', help='test files to run, by name '
		'without .py (default: every test_*.py here)')
	parser.add_argument('--jobs', type=int, default=4, help='files at once '
		'(default 4)')
	args = parser.parse_args(argv)

	every = sorted([name[:-3] for name in os.listdir(HERE)
		if re.match(r'^test_\w+\.py$', name)] + list(PYTEST))
	unknown = [name for name in args.names if name not in every]

	if unknown:
		sys.stderr.write('run_tests: no such test file: {} (have: {})\n'.format(
			', '.join(unknown), ', '.join(every)))
		return 1

	names = args.names or every
	failures = 0

	with concurrent.futures.ThreadPoolExecutor(max(1, args.jobs)) as pool:
		for name, passed, failed, seconds, ok, tail in pool.map(run_one, names):
			print('{:<6}{:<24}{:>5} passed, {} failed ({:.0f} s)'.format(
				'ok' if ok else 'FAIL', name, passed, failed, seconds))

			if not ok:
				failures += 1
				print('\n'.join('      ' + line for line in tail.split('\n')))

	print('\n{} of {} files passed'.format(len(names) - failures, len(names)))

	return 1 if failures else 0


if __name__ == '__main__':
	sys.exit(main())
