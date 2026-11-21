#!/usr/bin/env python3
"""Paired separate-process timing of two versions, with a stopping rule.

	python3 paired.py --a DIR_A --b DIR_B --cmd 'python3 probe.py --src {src}'

Each pair runs the command once for each side, in a fresh process, alternating
which side goes first (the side run first in a pair was 0.01-0.04 s slower in
one search, so a fixed order hands the second a fake win). Each side keeps its
own compiler cache, `<DIR>/.paired-cache`, warmed by one unrecorded run before
the first pair, so no pair times a compile.

The command prints the seconds it measured as the last number on its last
line of stdout, and exits non-zero if its output is wrong; any failure stops
the comparison. `{src}` is the side's directory and `{side}` its name, `a` or
`b`.

After `--min-pairs` pairs, and after each pair from then on, an exact
two-sided sign test on the paired differences is checked; the comparison
stops as soon as p < `--alpha`, and otherwise runs to `--max-pairs`. With the
defaults a unanimous result stops at 9 pairs and a close call runs all 24.

The two unrecorded runs also price the comparison: `priced` in the log gives
a pair's seconds and the hours `--min-pairs` and `--max-pairs` take. With
`--cap-hours`, a comparison whose `--min-pairs` would pass the cap exits 3
before the first pair. Every later run is killed past `--run-timeout` seconds
(default: ten times that side's unrecorded run, plus 60), and that exits 4.

Looking after every pair raises the chance of a false difference above the
per-look alpha. With alpha 0.005 and looks from pair 8 to 24 it is 1.2%
under no real difference (simulated, 200,000 trials); at alpha 0.01 it would
be 2.2%. Deciding by eye on 10 wins of 12 pairs has a 3.9% false-positive
rate.
"""

import os
import sys
import json
import math
import time
import argparse
import statistics

from probe_run import run_probe


def sign_test(wins, losses):
	"""Exact two-sided sign test; ties are dropped before calling."""

	n = wins + losses

	if n == 0:
		return 1.0

	k = min(wins, losses)
	tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n

	return min(1.0, 2 * tail)


def run_once(args, side, directory, timeout=None):
	return run_probe(args.cmd, directory, side, os.path.join(directory,
		'.paired-cache'), args.env, args.regex, timeout)


def price(warm, min_pairs, max_pairs):
	"""`(seconds a pair, hours at min_pairs, hours at max_pairs)` from the
	wall seconds of each side's unrecorded run."""

	pair = warm['a'] + warm['b']

	return pair, min_pairs * pair / 3600, max_pairs * pair / 3600


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--a', required=True, help='directory of version A')
	parser.add_argument('--b', required=True, help='directory of version B')
	parser.add_argument('--cmd', required=True, help='the timing command; '
		'{src} and {side} are substituted')
	parser.add_argument('--env', action='append', default=[],
		help='KEY=VALUE for every run, e.g. NUMBA_NUM_THREADS=1; repeatable')
	parser.add_argument('--min-pairs', type=int, default=8)
	parser.add_argument('--max-pairs', type=int, default=24)
	parser.add_argument('--alpha', type=float, default=0.005)
	parser.add_argument('--direction', choices=('min', 'max'), default='min',
		help='min: smaller is better (seconds); max: larger is better')
	parser.add_argument('--log', help='append one line per run to this file')
	parser.add_argument('--regex', help='a pattern whose first group is the '
		'seconds on the command\'s last line (default: its last number)')
	parser.add_argument('--cap-hours', type=float, help='exit 3 before the '
		'first pair when --min-pairs would take longer than this, and kill '
		'an unrecorded run that passes it')
	parser.add_argument('--run-timeout', type=float, help='seconds after '
		'which a recorded run is killed (default: ten times that side\'s '
		'unrecorded run, plus 60)')
	parser.add_argument('--json', action='store_true')
	args = parser.parse_args(argv)

	if args.max_pairs < 1 or args.max_pairs < args.min_pairs:
		parser.error('--max-pairs ({}) must be at least 1 and at least '
			'--min-pairs ({})'.format(args.max_pairs, args.min_pairs))

	sides = {'a': os.path.abspath(args.a), 'b': os.path.abspath(args.b)}

	for side, directory in sides.items():
		if not os.path.isdir(directory):
			sys.exit('--{} {} is not a directory'.format(side, directory))

	log = open(args.log, 'a') if args.log else None

	warm = {}

	for side in ('a', 'b'):                       # fill both caches, not counted
		start = time.time()
		run_once(args, side, sides[side], args.cap_hours * 3600 if
			args.cap_hours else None)
		warm[side] = time.time() - start

	pair, least, most = price(warm, args.min_pairs, args.max_pairs)
	line = ('priced: a pair is about {:.0f} s (a {:.0f} s, b {:.0f} s); {} to '
		'{} pairs take {:.2f} to {:.2f} h'.format(pair, warm['a'], warm['b'],
		args.min_pairs, args.max_pairs, least, most))

	if log:
		log.write(line + '\n')
		log.flush()

	if args.cap_hours and least > args.cap_hours:
		print('over the cap of {:g} h: {}'.format(args.cap_hours, line),
			file=sys.stderr)
		return 3

	limit = {side: args.run_timeout or 10 * warm[side] + 60 for side in warm}

	diffs, p, stopped = [], 1.0, 'max-pairs'

	for index in range(args.max_pairs):
		order = ('a', 'b') if index % 2 == 0 else ('b', 'a')
		seconds = {}

		for side in order:
			seconds[side] = run_once(args, side, sides[side], limit[side])

			if log:
				log.write('pair {} {} {:.9g}\n'.format(index + 1, side,
					seconds[side]))
				log.flush()

		diffs.append(seconds['b'] - seconds['a'])
		better = [d < 0 if args.direction == 'min' else d > 0 for d in diffs]
		wins = sum(better)
		ties = sum(d == 0 for d in diffs)
		p = sign_test(wins, len(diffs) - wins - ties)

		if len(diffs) >= args.min_pairs and p < args.alpha:
			stopped = 'sign test'
			break

	if log:
		log.close()

	wins = sum(d < 0 if args.direction == 'min' else d > 0 for d in diffs)
	median = statistics.median(diffs)

	if p < args.alpha:
		verdict = 'B better' if (median < 0) == (args.direction == 'min') \
			else 'B worse'
	else:
		verdict = 'no difference resolved'

	result = {'pairs': len(diffs), 'median_b_minus_a': median,
		'mean_b_minus_a': statistics.mean(diffs), 'b_better_in': wins,
		'p': p, 'alpha': args.alpha, 'stopped_by': stopped,
		'verdict': verdict, 'diffs': diffs}

	if args.json:
		print(json.dumps(result, indent='\t'))
	else:
		print('paired, B - A: median {:+.3g}, B better in {}/{}, sign test '
			'p = {:.3g} -> {} (stopped by {})'.format(median, wins,
				len(diffs), p, verdict, stopped))

	return 0


if __name__ == '__main__':
	sys.exit(main())
