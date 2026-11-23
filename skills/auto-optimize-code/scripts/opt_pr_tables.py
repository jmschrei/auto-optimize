#!/usr/bin/env python3
"""Write the optimization PR's tables from an ended search.

	python3 opt_pr_tables.py WORK [--id best] [--out DIR]

DIR defaults to `WORK/ship/pr-optimization`. Everything comes from what the
search recorded; nothing is measured again:

	branch.json       the branch the PR targets, the commit it builds on, the
	                  kept changes in order, the documents chosen at step 5
	changes.tsv       one row per kept change: id, title, files, +, -
	files.tsv         one row per file the PR changes: path, +, -
	performance.tsv   the objective and each observer: medians and ranges of
	                  the first measurement and of the kept code, and runs
	gates.tsv         each output check: name, op, threshold, worst, runs
	checks.json       the compare check's summary and the first-call times
	paired.json       the paired timing at the end, or null
	machine.json      the machine the search ran on, or null
	fragments.json    each kept change's own name and description
	                  (`## Change` in its notes.md), for the prose template
	tests.json        the unit-test record of the kept code and its counts,
	                  read from its log (`--tests-format` for another runner)
	messages/<id>.txt the commit message of each kept change, from its
	                  `## Commit message` with `after.commit_trailer`; an
	                  existing file is kept, a missing message is a skeleton

`opt_pr_body.py DIR --template` then writes the prose skeleton. Standard
library only.
"""

import os
import re
import sys
import json
import argparse
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autoopt
import publish

KEYS = ('passed', 'skipped', 'failed', 'errors')
# Test-runner summaries, tried in order; the first that matches a line of
# the log gives the counts. Each regex has one group, an integer, and the
# integers of every matching line are added.
LOG_FORMATS = (
	('pytest', {'passed': r'\b(\d+) passed\b', 'failed': r'\b(\d+) failed\b',
		'skipped': r'\b(\d+) skipped\b', 'errors': r'\b(\d+) errors?\b'},
		r'^=+ .*\b(passed|failed|errors?)\b.* in [0-9.]+s'),
	('testthat', {'passed': r'\bPASS (\d+)\b', 'failed': r'\bFAIL (\d+)\b',
		'skipped': r'\bSKIP (\d+)\b'}, r'\[ FAIL \d+ \|'),
	('shell', {'passed': r'(?i)\b(\d+) (?:tests? )?passed\b',
		'failed': r'(?i)\b(\d+) (?:failures?|failed)\b',
		'skipped': r'(?i)\b(\d+) skipped\b', 'errors': r'(?i)\b(\d+) errors?\b'},
		r'(?i)\b\d+ (?:tests? )?passed\b'),
)
SKELETON = ('<<FILL: an imperative subject of at most 72 characters, no '
	'closing period>>\n\n<<FILL: what this change does and what stays the '
	'same, wrapped at 72 columns>>\n')


def given_format(items):
	"""`--tests-format KEY=REGEX` items as one LOG_FORMATS entry, KEY one of
	KEYS or `total` (passed is then total less failed, errors and skipped);
	each REGEX has one group, an integer. `fail`s on a malformed item."""

	patterns = {}

	for item in items:
		key, equals, pattern = item.partition('=')

		if not equals or key not in KEYS + ('total',):
			autoopt.fail('--tests-format takes KEY=REGEX, KEY one of {} or '
				'total; got {!r}'.format(', '.join(KEYS), item))

		try:
			if re.compile(pattern).groups != 1:
				raise re.error('it needs exactly one group, the count')
		except re.error as error:
			autoopt.fail('--tests-format {}: {}'.format(key, error))

		patterns[key] = pattern

	return ('given', patterns, '|'.join('(?:{})'.format(pattern) for pattern
		in patterns.values()))


def log_counts(text, formats=LOG_FORMATS):
	"""`(format, {passed, skipped, failed, errors})` from a test log's
	summary lines, or `(None, None)` when no known summary is in it."""

	for name, patterns, marker in formats:
		marked = [line for line in text.splitlines() if re.search(marker,
			line)]

		if not marked:
			continue

		# A summary printed several times (a runner that reports as it goes)
		# counts once: the last line is the final one.
		lines = marked[-1:] if name in ('pytest', 'testthat') else marked
		totals = {key: 0 for key in KEYS + ('total',)}

		for line in lines:
			for key, pattern in patterns.items():
				for found in re.finditer(pattern, line):
					totals[key] += int(found.group(1))

		if 'total' in patterns and 'passed' not in patterns:
			totals['passed'] = totals['total'] - totals['failed'] - \
				totals['errors'] - totals['skipped']

		del totals['total']

		return name, totals

	return None, None


def tree(bare, ref):
	return autoopt.git('-C', bare, 'rev-parse', ref + '^{tree}').strip()


def numstat(bare, before, after, exclude=()):
	"""`[(path, added, deleted)]` from `before` to `after` (commits or
	trees), leaving out the paths in `exclude`; binary files count 0."""

	specs = ['--', '.'] + [':(exclude){}'.format(name) for name in exclude]
	rows = []

	for line in autoopt.git('-C', bare, 'diff', '--numstat', '--no-renames',
			before, after, *specs).splitlines():
		added, deleted, path = line.split('\t', 2)
		rows.append((path, int(added) if added.isdigit() else 0,
			int(deleted) if deleted.isdigit() else 0))

	return rows


def spread(values):
	values = [value for value in values or [] if isinstance(value, (int,
		float))]

	if not values:
		return None, None, None, 0

	return statistics.median(values), min(values), max(values), len(values)


def write_tsv(path, header, rows):
	publish.write(path, '\n'.join(['\t'.join(header)] + ['\t'.join(str(value)
		for value in row) for row in rows]) + '\n')


def number(value):
	return '' if value is None else repr(float(value))


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('work')
	parser.add_argument('--id', default='best')
	parser.add_argument('--out', help='default WORK/ship/pr-optimization')
	parser.add_argument('--tests-format', action='append', default=[],
		metavar='KEY=REGEX', help='the unit-test log\'s summary, for a runner '
		'whose summary is not pytest\'s, testthat\'s or "N passed": KEY one '
		'of passed, failed, skipped, errors or total, REGEX with one group, '
		'the count; repeat for each key. Without it, or when nothing matches, '
		'the PR gives no counts')
	args = parser.parse_args(argv)
	formats = (given_format(args.tests_format),) if args.tests_format else \
		LOG_FORMATS

	work = os.path.abspath(os.path.expanduser(args.work))

	if os.path.basename(work) == 'autooptimize':
		work = os.path.dirname(work)

	root = os.path.join(work, 'autooptimize')
	config = autoopt.load_config(root)

	if not config.get('repo'):
		autoopt.fail('this search was made with `init --copy`: a plain search '
			'ships with `ship.py apply`, not a PR')

	ended = autoopt.load_ended(root)

	if ended is None:
		autoopt.fail('the search has not ended: `autoopt.py end` first')

	identifier = autoopt.resolve_id(root, config, args.id)
	problem = autoopt.measured_problem(root, config, identifier)

	if problem:
		autoopt.fail(problem)

	out = os.path.abspath(os.path.expanduser(args.out or autoopt.work_path(work,
		'ship', autoopt.SHIP_FOLDERS['optimization'])))
	os.makedirs(os.path.join(out, 'messages'), exist_ok=True)

	bare = os.path.join(root, 'repo.git')
	original = autoopt.original_of(work, config)
	start = config['repo']['commit']
	strip = autoopt.outputs_stripper(root, config)
	head = autoopt.branch_of(root, identifier)
	chain = autoopt.lineage(root, identifier)
	tree_of = lambda i: strip(tree(bare, autoopt.branch_of(root, i)))
	steps = autoopt.winning_steps(root, chain, tree_of, strip(tree(bare,
		start)))

	if not steps:
		autoopt.fail('{} changes nothing in the repository'.format(
			autoopt.label(root, identifier)))

	suite = original.get('suite')
	owned = [name for name in autoopt.git('-C', bare, 'diff', '-z',
		'--name-only', original['commit'], start).split('\0') if name] if \
		suite else []

	# Each kept change: the files it changed against the one before it.
	rows, before = [], strip(tree(bare, start))

	for step, step_tree in steps:
		changed = numstat(bare, before, step_tree, owned)
		rows.append((step, ((autoopt.load_result(root, step) or {}).get('title')
			or '').replace('\t', ' '), ','.join(path for path, _, _ in changed),
			sum(a for _, a, _ in changed), sum(d for _, _, d in changed)))
		before = step_tree

	write_tsv(os.path.join(out, 'changes.tsv'), ('id', 'title', 'files',
		'added', 'deleted'), rows)
	files = numstat(bare, strip(tree(bare, start)), tree_of(identifier), owned)
	write_tsv(os.path.join(out, 'files.tsv'), ('path', 'added', 'deleted'),
		files)

	# The objective and every observer, first measurement against the kept
	# code: each run's value, so the table can say how many and how far apart.
	zero = autoopt.load_result(root, 0) or {}
	kept = autoopt.load_result(root, identifier) or {}
	objective = config['objective']
	performance = []

	def measure(name, role, unit, first, last):
		a = spread(first)
		b = spread(last)

		if a[0] is None or b[0] is None:
			return

		performance.append((name, role, unit, number(a[0]), number(a[1]),
			number(a[2]), a[3], number(b[0]), number(b[1]), number(b[2]), b[3]))

	measure(objective['name'], 'objective', objective.get('unit') or '',
		(zero.get('objective') or {}).get('values'), (kept.get('objective') or
		{}).get('values'))

	for observer in config.get('observers') or []:
		name = observer['name']
		measure(name, 'observer', '', ((zero.get('observers') or {}).get(name)
			or {}).get('values'), ((kept.get('observers') or {}).get(name) or
			{}).get('values'))

	write_tsv(os.path.join(out, 'performance.tsv'), ('measure', 'role', 'unit',
		'first_median', 'first_min', 'first_max', 'first_runs', 'kept_median',
		'kept_min', 'kept_max', 'kept_runs'), performance)

	gates = []

	for gate in config.get('gates') or []:
		record = (kept.get('gates') or {}).get(gate['name']) or {}
		values = [value for value in record.get('values') or [] if value is not
			None]
		gates.append((gate['name'], gate['op'], gate.get('threshold'),
			record.get('worst'), len(values)))

	write_tsv(os.path.join(out, 'gates.tsv'), ('name', 'op', 'threshold',
		'worst', 'runs'), gates)

	check = kept.get('check') or {}
	compare = check.get('compare') or {}
	cold_kept = (check.get('cold') or {}).get('seconds')
	cold_first = ((zero.get('check') or {}).get('cold') or {}).get('seconds')
	publish.write_json(os.path.join(out, 'checks.json'), {
		'compare': {'verdict': compare.get('verdict'), 'summary':
		compare.get('summary')} if compare else None,
		'cold': {'first': cold_first, 'kept': cold_kept} if cold_kept is not
		None else None})

	final = ended.get('paired') if isinstance(ended.get('paired'), dict) else \
		None
	paired = None

	if final and final.get('winner') == identifier:
		paired = {key: final.get(key) for key in ('paired', 'threads')}

	publish.write_json(os.path.join(out, 'paired.json'), paired)
	publish.write_json(os.path.join(out, 'machine.json'), ended.get('machine'))

	# The unit tests that ran on the kept code, and their counts from the log.
	record = kept.get('tests')
	tests = None

	if record:
		log = os.path.join(autoopt.iteration_dir(root, identifier),
			record.get('log') or os.path.join('unit-tests', 'log.txt'))

		try:
			with open(log, encoding='utf-8', errors='replace') as handle:
				form, counts = log_counts(handle.read(), formats)
		except OSError:
			form, counts = None, None

		tests = {'pass': bool(record.get('pass')), 'scope': record.get('scope'),
			'format': form, 'counts': counts}

	publish.write_json(os.path.join(out, 'tests.json'), tests)

	# A message the writer has edited is kept; one the iteration wrote fills
	# a missing file; otherwise a skeleton to fill.
	trailer = autoopt.commit_trailer(root)
	skeletons = []

	for step, _ in steps:
		path = os.path.join(out, 'messages', '{}.txt'.format(step))

		if os.path.exists(path):
			continue

		message = autoopt.change_fragment(root, step).get('message')

		if not message:
			skeletons.append(autoopt.label(root, step))

		publish.write(path, autoopt.with_trailer(message or SKELETON, trailer))

	# What each kept change's subagent wrote about it, for the prose template.
	publish.write_json(os.path.join(out, 'fragments.json'), {str(step): {
		key: autoopt.change_fragment(root, step).get(key) for key in ('name',
		'change')} for step, _ in steps})
	publish.write_json(os.path.join(out, 'branch.json'), {
		'id': identifier, 'label': autoopt.label(root, identifier),
		'target': original.get('branch'), 'base': original['commit'] if suite
		else start, 'remote_url': original.get('remote_url'),
		'suite': bool(suite), 'suite_files': len(owned),
		'steps': [step for step, _ in steps],
		'step_labels': [autoopt.label(root, step) for step, _ in steps],
		'labels': [autoopt.label(root, i) for i in autoopt.iteration_ids(root)],
		'objective': {'name': objective['name'], 'unit': objective.get('unit')
		or '', 'direction': objective['direction']},
		'documents': list((dict(autoopt.AFTER_DEFAULTS, **(config.get('after')
		or {}))).get('ship_documents') or [])})

	print('wrote the tables in {}: {} kept change(s), {} file(s), {} row(s) of '
		'performance, {} output check(s)'.format(out, len(steps), len(files),
		len(performance), len(gates)))

	if skeletons:
		print('commit messages to write (skeletons in messages/): {}'.format(
			', '.join(skeletons)))

	return 0


if __name__ == '__main__':
	sys.exit(main())
