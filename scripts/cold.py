#!/usr/bin/env python3
"""Cold first-call time of one or more versions, each run on an empty cache.

	python3 cold.py --side b120=DIR_A --side b126=DIR_B \\
		--cmd 'python3 first_call.py --src {src}' --scratch $DIR/cold --reps 2

Every run gets a new, empty compiler cache created under `--scratch` (never
`/tmp`, which another process may clean mid-run) and passed as
`NUMBA_CACHE_DIR`, `TRITON_CACHE_DIR` and `TORCHINDUCTOR_CACHE_DIR`. Nothing
is deleted: the caches stay in `--scratch`. The runner's, under
`iterations/<id>/check/cold/`, are reclaimed by `autoopt.py prune`.
The sides alternate order each repetition, so a slow moment does not land on
one side.

The command prints the first call's seconds as the last number on its last
line of stdout. A single cold run varied by 3-5 s in one search: take two or
more repetitions.
"""

import os
import sys
import json
import argparse
import tempfile
import statistics

from probe_run import run_probe


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--side', action='append', required=True,
		help='NAME=DIRECTORY; repeat for each version')
	parser.add_argument('--cmd', required=True, help='{src} and {side} are '
		'substituted')
	parser.add_argument('--scratch', required=True, help='a directory inside '
		'the iteration directory for the empty caches')
	parser.add_argument('--reps', type=int, default=2)
	parser.add_argument('--env', action='append', default=[],
		help='KEY=VALUE for every run; repeatable')
	parser.add_argument('--regex', help='a pattern whose first group is the '
		'seconds on the command\'s last line (default: its last number)')
	parser.add_argument('--json', action='store_true')
	args = parser.parse_args(argv)

	sides = []

	for item in args.side:
		name, _, directory = item.partition('=')

		if not name or not os.path.isdir(directory):
			sys.exit('--side takes NAME=DIRECTORY; got {!r}'.format(item))

		sides.append((name, os.path.abspath(directory)))

	os.makedirs(args.scratch, exist_ok=True)
	times = {name: [] for name, _ in sides}

	for rep in range(args.reps):
		order = sides if rep % 2 == 0 else sides[::-1]

		for name, directory in order:
			cache = tempfile.mkdtemp(prefix='cold-{}-'.format(name),
				dir=os.path.abspath(args.scratch))

			if os.listdir(cache):
				sys.exit('{} is not empty'.format(cache))

			times[name].append(run_probe(args.cmd, directory, name, cache,
				args.env, args.regex))
			print('rep {} {:>12}  {:.4g} s'.format(rep + 1, name,
				times[name][-1]), flush=True)

	result = {name: {'runs': runs, 'median': statistics.median(runs)}
		for name, runs in times.items()}

	if args.json:
		print(json.dumps(result, indent='\t'))
	else:
		for name, record in result.items():
			print('{:>12}  median {:.4g} s  runs {}'.format(name,
				record['median'], ', '.join('{:.4g}'.format(t)
					for t in record['runs'])))

	return 0


if __name__ == '__main__':
	sys.exit(main())
