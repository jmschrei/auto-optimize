#!/usr/bin/env python3
"""Check the read-only commands `references/reading-a-search.md` routes to:
`table` filters, sorts and prints every iteration, `show` puts one on one
screen, and neither they nor `status --brief`, `stages.py status` and
`path` change a file in the search.

	python3 test_reading.py     # about twenty seconds

A real search in a temporary directory, driven with the real scripts. No
GPU, no network.
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

STAGES = os.path.join(SCRIPTS, 'stages.py')


def run(*args):
	return subprocess.run([sys.executable] + list(args), capture_output=True,
		text=True)


def snapshot(folder):
	"""Every path under `folder` with its size and modification time."""

	out = {}

	for directory, names, files in os.walk(folder):
		for name in names + files:
			path = os.path.join(directory, name)
			info = os.lstat(path)
			out[os.path.relpath(path, folder)] = (info.st_size,
				info.st_mtime_ns)

	return out


def build(root):
	"""A0 at 0.50; A1 better and promoted; A2 worse; A3 breaks the gate;
	A4 forked and not yet run."""

	search = tr.make_search(root, staged=False, runs=3)
	tr.add(search, 1, 0.60, category='CACHING')
	tr.call([tr.AUTOOPT, 'promote', search, '--id', '1'])
	tr.add(search, 2, 0.55, category='MICRO')
	tr.add(search, 3, 0.70, gate=2.0, category='MICRO')
	tr.call([tr.AUTOOPT, 'new', search, '--id', '4', '--title', 'in flight',
		'--category', 'IO'])

	with open(os.path.join(autoopt.iteration_dir(search, 1), 'notes.md'),
			'w') as handle:
		handle.write('## The idea\n\nCache the score.\n\n## What happened\n\n'
			+ 'It helped. ' * 200 + '\n')

	return search


def main():
	suite = tr.Suite()
	base = tempfile.mkdtemp(prefix='reading-')
	work = os.path.join(base, 'w')
	search = build(work)
	before = snapshot(work)

	done = run(tr.AUTOOPT, 'table', work)
	suite.check('table lists every iteration, the one in flight as running',
		done.returncode == 0 and all('A{} '.format(i) in done.stdout
		for i in range(5)) and 'running' in done.stdout, done.stdout
		+ done.stderr)

	rows = json.loads(run(tr.AUTOOPT, 'table', work, '--json', '--fields',
		'all').stdout)
	by_id = {row['id']: row for row in rows}
	suite.equal('the statuses, from result.json and meta.json', [row['status']
		for row in rows], ['ok', 'ok', 'ok', 'gate-failed', 'running'])
	suite.check('vs_parent is against the parent, vs_base against A0',
		abs(by_id['A2']['vs_parent'] - (by_id['A2']['median']
		- by_id['A1']['median'])) < 1e-12 and abs(by_id['A2']['vs_base']
		- (by_id['A2']['median'] - by_id['A0']['median'])) < 1e-12, by_id['A2'])
	suite.equal('promoted marks A0 and A1', [row['id'] for row in rows
		if row['promoted']], ['A0', 'A1'])
	suite.check('a gate is a column under its name, and an observer too',
		by_id['A3']['cost'] == 2.0 and by_id['A0']['size'] == 100.0
		and by_id['A3']['gates'] == 'FAIL cost', by_id['A3'])

	rows = json.loads(run(tr.AUTOOPT, 'table', work, '--json', '--status',
		'ok', '--sort', 'best', '--limit', '1', '--fields', 'id,median').stdout)
	suite.equal('--status ok --sort best --limit 1 is the best passing '
		'iteration, a gate-failed one excluded', [row['id'] for row in rows],
		['A1'])
	suite.equal('--fields sets the keys', sorted(rows[0]), ['id', 'median'])

	out = run(tr.AUTOOPT, 'table', work, '--tsv', '--category', 'micro',
		'--fields', 'id,category').stdout.splitlines()
	suite.equal('--tsv, and --category takes the short name', out,
		['id\tcategory', 'A2\tMICRO', 'A3\tMICRO'])

	out = run(tr.AUTOOPT, 'table', work, '--tsv', '--parent', 'A1',
		'--fields', 'id').stdout.split()
	suite.equal('--parent keeps the children', out, ['id', 'A2', 'A3', 'A4'])

	out = run(tr.AUTOOPT, 'table', work, '--tsv', '--frontier', '--fields',
		'id').stdout.split()
	suite.equal('--frontier keeps the improvements', out, ['id', 'A0', 'A1'])

	done = run(tr.AUTOOPT, 'table', work, '--fields', 'id,nope')
	suite.check('an unknown column is refused, listing the columns',
		done.returncode == 1 and 'no column nope' in done.stderr
		and 'vs_base_pct' in done.stderr, done.stderr)

	done = run(tr.AUTOOPT, 'show', work, 'A2')
	out = done.stdout
	suite.check('show gives the status, the median, both differences and the '
		'keep verdict', done.returncode == 0 and 'status     ok at 3 runs' in out
		and 'vs parent' in out and 'against A1' in out and 'vs A0' in out
		and 'keep       fail' in out, out + done.stderr)
	suite.check('and the code change, with the command for the whole diff',
		'change     ' in out and 'diff autoopt/1...autoopt/2' in out, out)
	suite.check('and the paths to read further', 'result.json' in out
		and os.path.join(search, 'iterations', '2') in out, out)

	out = run(tr.AUTOOPT, 'show', work, 'A1', '--chars', '40').stdout
	suite.check('the notes sections, cut at --chars', '## The idea' in out
		and 'Cache the score.' in out and '## What happened' in out
		and out.count('It helped.') < 10 and ' …' in out, out)

	out = run(tr.AUTOOPT, 'show', work).stdout
	suite.check('with no id, the winner', out.startswith('A1 '), out[:80])

	out = run(tr.AUTOOPT, 'show', work, '4').stdout
	suite.check('an iteration in flight says so', 'running: no result.json yet'
		in out, out)

	record = json.loads(run(tr.AUTOOPT, 'show', work, 'A3', '--json').stdout)
	suite.check('show --json carries the row, the gates and the paths',
		record['row']['status'] == 'gate-failed' and not record['gates']['cost'][
		'pass'] and 'result.json' in record['paths'], sorted(record))

	done = run(tr.AUTOOPT, 'show', work, 'A9')
	suite.check('an id with no iteration is refused', done.returncode == 1
		and 'no iteration A9' in done.stderr, done.stderr)

	for words in (['status', work, '--brief'], ['path', work, '--markdown'],
			['path', work, '--json']):
		done = run(tr.AUTOOPT, *words)
		suite.check('{} runs'.format(' '.join(words[:1] + words[2:])),
			done.returncode == 0, done.stderr[-300:])

	done = run(STAGES, 'status', work)
	suite.check('stages.py status runs', done.returncode == 0, done.stderr)

	after = snapshot(work)
	changed = sorted(path for path in set(before) | set(after)
		if before.get(path) != after.get(path))
	suite.equal('none of them changed a file in the search', changed, [])

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
