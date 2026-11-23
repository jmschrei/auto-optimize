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
import re
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
import test_runner

AUTOOPT = test_runner.AUTOOPT
RUNNER = test_runner.RUNNER
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

	search = test_runner.make_search(root, staged=False, runs=3)
	test_runner.add(search, 1, 0.60, category='CACHING')
	test_runner.call([AUTOOPT, 'promote', search, '--id', '1'])
	test_runner.add(search, 2, 0.55, category='MICRO')
	test_runner.add(search, 3, 0.70, gate=2.0, category='MICRO')
	test_runner.call([AUTOOPT, 'new', search, '--id', '4', '--title',
		'in flight', '--category', 'IO'])

	with open(os.path.join(autoopt.iteration_dir(search, 1), 'notes.md'),
			'w') as handle:
		handle.write('## The idea\n\nCache the score.\n\n## What happened\n\n'
			+ 'It helped. ' * 200 + '\n')

	return search


def records(suite, search):
	"""`load_result` and `load_meta` parse a file once per process and hand
	out copies, and a write is read back at once."""

	path = os.path.join(autoopt.iteration_dir(search, 1), 'result.json')
	first = autoopt.load_result(search, 1)
	first['status'] = 'edited by the caller'
	second = autoopt.load_result(search, 1)
	suite.check('a loaded record is parsed once and handed out as a copy, so '
		'an edit to it reaches no later caller', second['status'] == 'ok'
		and path in autoopt._RECORDS and second is not autoopt._RECORDS[
			path][1], second['status'])

	autoopt.write_json(path, dict(second, note='written'))
	suite.equal('write_json is read back at once', autoopt.load_result(
		search, 1).get('note'), 'written')

	with open(path, 'w') as handle:
		json.dump(dict(second, note='rewritten in place, longer'), handle)
	suite.equal('and so is a file rewritten in place by anything else',
		autoopt.load_result(search, 1).get('note'),
		'rewritten in place, longer')

	autoopt.write_json(path, second)
	meta = autoopt.load_meta(search, 2)
	suite.check('meta.json the same way, and a missing record is None',
		meta['id'] == 2 and autoopt.load_meta(search, 99) is None
		and autoopt.load_result(search, 4) is None, meta)


def touched(suite, search):
	"""What each iteration changed is asked of git once per pair of
	commits, in one call, and kept in touched.json; the one in flight is
	diffed each time."""

	config = autoopt.load_config(search)
	ids = [1, 2, 3, 4]
	want = {identifier: autoopt.touched_files(search, config, identifier)
		for identifier in ids}
	calls = {'pairs': [], 'diffed': []}
	diff_names, touched_files = autoopt.diff_names, autoopt.touched_files

	def counting_names(bare, pairs):
		calls['pairs'].append(len(pairs))
		return diff_names(bare, pairs)

	def counting_files(root, config, identifier):
		calls['diffed'].append(identifier)
		return touched_files(root, config, identifier)

	autoopt.diff_names, autoopt.touched_files = counting_names, counting_files
	try:
		first = autoopt.touched_many(search, config, ids)
		second = autoopt.touched_many(search, config, ids)
	finally:
		autoopt.diff_names, autoopt.touched_files = diff_names, touched_files

	suite.check('touched_many names the files git diff names, for settled '
		'iterations and the one in flight', first == want and second == want
		and want[1] == {'bench.py'}, (first, want))
	suite.equal('settled pairs go to git in one call, once; only the '
		'iteration in flight is diffed on its own, each time',
		calls, {'pairs': [3, 0], 'diffed': [4, 4]})
	record = json.load(open(os.path.join(search, autoopt.TOUCHED)))
	suite.check('touched.json keys each list by <parent commit>..<commit>',
		len(record) == 3 and all(re.fullmatch(r'[0-9a-f]{40}\.\.[0-9a-f]{40}',
			key) for key in record), record)
	suite.equal('a pair git cannot read gives nothing, so the caller diffs '
		'each iteration on its own', autoopt.diff_names(os.path.join(search,
			'repo.git'), [('0' * 40, '1' * 40)]), {})


def renames(suite, base):
	"""diff_names names a renamed file by its new path, as `git diff
	--name-only` does: two commits on one `diff-tree --stdin` line read as a
	commit and its parent, which diffs them the wrong way round."""

	repo = os.path.join(base, 'renames')
	os.makedirs(repo)
	git = lambda *args: subprocess.run(['git', '-C', repo, '-c',
		'user.name=t', '-c', 'user.email=t@example.org'] + list(args),
		capture_output=True, text=True, check=True).stdout.strip()
	git('init', '-q')

	with open(os.path.join(repo, 'old.py'), 'w') as handle:
		handle.write(''.join('line {}\n'.format(i) for i in range(50)))
	with open(os.path.join(repo, 'kept.py'), 'w') as handle:
		handle.write('x = 1\n')

	git('add', '.')
	git('commit', '-qm', 'first')
	git('mv', 'old.py', 'new.py')

	with open(os.path.join(repo, 'kept.py'), 'a') as handle:
		handle.write('y = 2\n')

	git('commit', '-qam', 'second')
	first, second = git('rev-parse', 'HEAD~1'), git('rev-parse', 'HEAD')
	want = sorted(git('diff', '--name-only', first, second).split())

	suite.equal('a rename is named by its new path, as git diff names it',
		{key: sorted(value) for key, value in autoopt.diff_names(repo,
			[(first, second)]).items()}, {(first, second): want})
	suite.check('and git diff itself names new.py, not old.py',
		'new.py' in want and 'old.py' not in want, want)


def tried(suite, search):
	"""*Already tried*: every iteration on the idea's files while they are
	few; past TRIED_RECENT, those on the root's path, the latest on the
	idea's function and the latest overall, and the rest counted."""

	config = autoopt.load_config(search)
	state = autoopt.load_state(search)
	whole = autoopt.tried_on(search, config, state, ['bench.py'])
	suite.check('a few iterations are all listed, in id order, as before',
		re.match(r'A1 \(iteration 1\): .*\. A2 \(iteration 2\): .*\. '
			r'A3 \(iteration 3\): ', whole) and 'more on this code' not in whole,
		whole)

	saved = autoopt.TRIED_RECENT
	autoopt.TRIED_RECENT = 1
	try:
		capped = autoopt.tried_on(search, config, state, ['bench.py'])
		named = autoopt.tried_on(search, config, state, ['bench.py'], '2')
	finally:
		autoopt.TRIED_RECENT = saved

	suite.check('past the cap: the root\'s path and the latest, the rest '
		'counted by what became of them', capped.startswith('A1 (iteration 1)')
		and 'A3 (iteration 3)' in capped and 'A2 (' not in capped
		and capped.endswith('And 1 more on this code, not listed: 1 not kept '
			'(`autoopt.py show <work> <label>` gives one).'), capped)
	suite.check('and an iteration whose title names the idea\'s function is '
		'listed too', 'A2 (iteration 2)' in named and 'more on this code'
		not in named, named)


def main():
	suite = test_runner.Suite()
	base = tempfile.mkdtemp(prefix='reading-')
	work = os.path.join(base, 'w')
	search = build(work)
	before = snapshot(work)

	done = run(AUTOOPT, 'table', work)
	suite.check('table lists every iteration, the one in flight as running',
		done.returncode == 0 and all('A{} '.format(i) in done.stdout
		for i in range(5)) and 'running' in done.stdout, done.stdout
		+ done.stderr)

	rows = json.loads(run(AUTOOPT, 'table', work, '--json', '--fields',
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

	rows = json.loads(run(AUTOOPT, 'table', work, '--json', '--status',
		'ok', '--sort', 'best', '--limit', '1', '--fields', 'id,median').stdout)
	suite.equal('--status ok --sort best --limit 1 is the best passing '
		'iteration, a gate-failed one excluded', [row['id'] for row in rows],
		['A1'])
	suite.equal('--fields sets the keys', sorted(rows[0]), ['id', 'median'])

	out = run(AUTOOPT, 'table', work, '--tsv', '--category', 'micro',
		'--fields', 'id,category').stdout.splitlines()
	suite.equal('--tsv, and --category takes the short name', out,
		['id\tcategory', 'A2\tMICRO', 'A3\tMICRO'])

	out = run(AUTOOPT, 'table', work, '--tsv', '--parent', 'A1',
		'--fields', 'id').stdout.split()
	suite.equal('--parent keeps the children', out, ['id', 'A2', 'A3', 'A4'])

	out = run(AUTOOPT, 'table', work, '--tsv', '--frontier', '--fields',
		'id').stdout.split()
	suite.equal('--frontier keeps the improvements', out, ['id', 'A0', 'A1'])

	done = run(AUTOOPT, 'table', work, '--fields', 'id,nope')
	suite.check('an unknown column is refused, listing the columns',
		done.returncode == 1 and 'no column nope' in done.stderr
		and 'vs_base_pct' in done.stderr, done.stderr)

	done = run(AUTOOPT, 'show', work, 'A2')
	out = done.stdout
	suite.check('show gives the status, the median, both differences and the '
		'keep verdict', done.returncode == 0
		and 'status     ok at 3 runs' in out and 'vs parent' in out
		and 'against A1' in out and 'vs A0' in out
		and 'keep       fail' in out, out + done.stderr)
	suite.check('and the code change, with the command for the whole diff',
		'change     ' in out and 'diff autoopt/1...autoopt/2' in out, out)
	suite.check('and the paths to read further', 'result.json' in out
		and os.path.join(search, 'iterations', '2') in out, out)

	out = run(AUTOOPT, 'show', work, 'A1', '--chars', '40').stdout
	suite.check('the notes sections, cut at --chars', '## The idea' in out
		and 'Cache the score.' in out and '## What happened' in out
		and out.count('It helped.') < 10 and ' …' in out, out)

	out = run(AUTOOPT, 'show', work).stdout
	suite.check('with no id, the winner', out.startswith('A1 '), out[:80])

	out = run(AUTOOPT, 'show', work, '4').stdout
	suite.check('an iteration in flight says so', 'running: no result.json yet'
		in out, out)

	record = json.loads(run(AUTOOPT, 'show', work, 'A3', '--json').stdout)
	suite.check('show --json carries the row, the gates and the paths',
		record['row']['status'] == 'gate-failed'
		and not record['gates']['cost']['pass']
		and 'result.json' in record['paths'], sorted(record))

	done = run(AUTOOPT, 'show', work, 'A9')
	suite.check('an id with no iteration is refused', done.returncode == 1
		and 'no iteration A9' in done.stderr, done.stderr)

	for words in (['status', work, '--brief'], ['path', work, '--markdown'],
			['path', work, '--json']):
		done = run(AUTOOPT, *words)
		suite.check('{} runs'.format(' '.join(words[:1] + words[2:])),
			done.returncode == 0, done.stderr[-300:])

	done = run(STAGES, 'status', work)
	suite.check('stages.py status runs', done.returncode == 0, done.stderr)

	after = snapshot(work)
	changed = sorted(path for path in set(before) | set(after)
		if before.get(path) != after.get(path))
	suite.equal('none of them changed a file in the search', changed, [])

	records(suite, search)
	touched(suite, search)
	renames(suite, base)
	tried(suite, search)

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
