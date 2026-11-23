#!/usr/bin/env python3
"""Check `autoopt.py end`: the step that ends a search on the record.

	python3 test_end.py     # about a minute

Real searches in a temporary directory, driven with the real scripts. The
dashboard's own commands are stubbed except where a case names them: `end`
must stop this search's publisher and no other, and a test must not start a
real one. No GPU, no network.
"""

import io
import os
import sys
import json
import time
import shutil
import tempfile
import argparse
import contextlib
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
import test_runner as tr

AUTOOPT = tr.AUTOOPT
RUNNER = tr.RUNNER
MONITOR = os.path.join(SCRIPTS, 'dashboard', 'autoopt_monitor.py')


def read(path):
	with open(path) as handle:
		return handle.read()


def build(root):
	"""A search with a winner A2 (A1 then A2 promoted), A3 in the round still
	open, files for the clean-up to find, and a README on A3."""

	search = tr.make_search(root, staged=False, runs=3)
	tr.add(search, 1, 0.60)
	tr.call([AUTOOPT, 'promote', search, '--id', '1'])
	tr.add(search, 2, 0.70)
	tr.call([AUTOOPT, 'promote', search, '--id', '2'])
	tr.add(search, 3, 0.65)

	for identifier in (2, 3):
		directory = autoopt.iteration_dir(search, identifier)
		os.makedirs(os.path.join(directory, 'runs', 'seed-9'), exist_ok=True)
		with open(os.path.join(directory, 'runs', 'seed-9', 'artifact.bin'),
				'w') as out:
			out.write('x' * 1000)
		with open(os.path.join(directory, 'saved.npy'), 'w') as out:
			out.write('array')
		os.makedirs(os.path.join(directory, 'probe'), exist_ok=True)

		with open(os.path.join(directory, 'probe', 'notes.txt'), 'w') as out:
			out.write('kept')

	for identifier in (1, 2, 3):
		write_readme(search, identifier)

	for name in ('harness', 'checks', 'reference', 'tools', 'elsewhere',
			'final'):
		os.makedirs(os.path.join(search, name), exist_ok=True)
		with open(os.path.join(search, name, 'sentinel.npy'), 'w') as out:
			out.write(name)

	return search


def write_readme(search, identifier):
	"""What `finish` leaves: an iteration with a README is settled for `end`,
	one without it is still `finishing`."""

	with open(os.path.join(autoopt.iteration_dir(search, identifier),
			'README.md'), 'w') as out:
		out.write('# A{}\n'.format(identifier))

	# What a kept iteration writes about its change; `promote` asks one made while
	# after_end for it (`autoopt.fragment_problem`).
	notes = os.path.join(autoopt.iteration_dir(search, identifier), 'notes.md')

	if not os.path.exists(notes):
		with open(notes, 'w') as out:
			out.write(''.join('## {}\nx\n\n'.format(name) for name in
				autoopt.NOTES_REQUIRED) + '## Change\nname: Add a notice\nThe '
				'file now carries its notice.\n\n## Commit message\nAdd a '
				'notice\n\nThe file carries the notice of its source.\n')


def end_args(search, **kwargs):
	return argparse.Namespace(root=search, reason=kwargs.get('reason'),
		id=kwargs.get('id'), background=False, reopen=kwargs.get('reopen',
		False))


def run_end(search, **kwargs):
	"""`autoopt.end` in this process, its output and exit code returned."""

	buffer = io.StringIO()
	code = 0

	with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
		try:
			code = autoopt.end(end_args(search, **kwargs))
		except SystemExit as exit_:
			code = exit_.code

	return code, buffer.getvalue()


def main():
	suite = tr.Suite()
	base = tempfile.mkdtemp()
	calls = []

	def stub(root, *flags):
		calls.append((root, flags))
		return 0, ''

	real = autoopt.run_dashboard
	autoopt.run_dashboard = stub

	try:
		root = os.path.join(base, 'a')
		search = build(root)

		# Preconditions.
		tr.call([AUTOOPT, 'new', search, '--id', '5', '--title', 'in flight',
			'--category', 'MISC', '--parent', '2'])
		code, out = run_end(search)
		suite.check('an iteration in flight refuses, listing it', code == 1
			and 'A5 is running' in out and not os.path.exists(os.path.join(
			search, 'ended.json')), out)

		other = build(os.path.join(base, 'split-built'))
		tr.add(other, 6, 0.50)
		suite.check('a measured iteration with no README is still finishing',
			(6, 'finishing') in [w[:2] for w in autoopt.working(other)])
		meta = autoopt.load_meta(other, 6)
		meta['split'] = {'step': 1}
		autoopt.write_meta(other, 6, meta)
		suite.check('one built from others\' code (meta.split) never is: it has no README to '
			'wait for, so end is not held back after a reopen', 6 not in [w[0]
			for w in autoopt.working(other)])

		directory = autoopt.iteration_dir(search, 5)
		tr.bench(directory, 0.55)
		tr.call([RUNNER, directory])
		write_readme(search, 5)
		state = autoopt.load_state(search)
		state['loop'] = {'phase': 1, 'open': True, 'started': 'then'}
		autoopt.save_state(search, state)
		code, out = run_end(search)
		suite.check('an open phase refuses, naming `loop --done`', code == 1
			and 'loop --done' in out, out)
		state['loop']['open'] = False
		autoopt.save_state(search, state)

		code, out = run_end(search, id='0')
		suite.check('the baseline is no winner', code == 1 and 'A0' in out
			and not os.path.exists(os.path.join(search, 'ended.json')), out)

		# The ending.
		code, out = run_end(search, reason='noise')
		ended = autoopt.read_json(os.path.join(search, 'ended.json'))
		results = {i: autoopt.load_result(search, i) for i in (0, 1, 2, 3)}
		suite.check('end succeeds and prints the summary', code == 0 and
			'the search ended' in out and 'ended.md' not in out, out[-600:])
		suite.equal('ended.json names the winner, from the records',
			(ended['winner']['id'], ended['winner']['label'],
			ended['winner']['path']), (2, 'A2', ['A0', 'A1', 'A2']))
		suite.equal('the medians are read from result.json, never typed',
			(ended['medians']['A0'], ended['medians']['winner']),
			(results[0]['objective']['median'], results[2]['objective'][
			'median']))
		suite.equal('the reason is the criterion named', ended['reason'][
			'criterion'], 'noise')
		suite.equal('the counts', (ended['counts']['rounds'],
			ended['counts']['iterations'], ended['counts'][
			'winning_iterations'], ended['winning_iterations']),
			(3, 4, 2, [1, 2]))
		suite.check('no timing probe: no paired result, and the summary says '
			'so', ended['paired'] is None and 'No paired timing' in read(
			os.path.join(search, 'ended.md')))
		suite.equal('winner_changes starts empty', ended['winner_changes'], [])
		suite.check('the last round\'s summary was written', ended[
			'round_summary'] and os.path.exists(ended['round_summary']),
			ended['round_summary'])

		summary = read(os.path.join(search, 'ended.md'))
		suite.check('ended.md grades the gain, the gates and the categories',
			'from {} in A0 to {} in A2'.format(autoopt.fmt(results[0][
			'objective']['median']), autoopt.fmt(results[2]['objective'][
			'median'])) in summary and '`cost <= 1.0`: pass' in summary and
			'### MISC (2)' in summary and 'A1:' in summary and 'A2:' in
			summary and 'from A1' in summary, summary)
		suite.check('and says where the numbers were measured', 'measured '
			'inside the search' in summary)
		suite.check('ended.md is printed whole', summary.strip() in out)

		# The clean-up: what goes, what stays.
		a2 = autoopt.iteration_dir(search, 2)
		a3 = autoopt.iteration_dir(search, 3)
		suite.check('a non-winning iteration loses its run artifacts and its '
			'checkout, and keeps its record, its probe notes and its branch',
			not os.path.exists(os.path.join(a3, 'runs', 'seed-9',
			'artifact.bin')) and not os.path.exists(os.path.join(a3, '.git'))
			and os.path.exists(os.path.join(a3, 'result.json')) and
			os.path.exists(os.path.join(a3, 'README.md')) and os.path.exists(
			os.path.join(a3, 'probe', 'notes.txt')) and subprocess.run(['git',
			'-C', os.path.join(search, 'repo.git'), 'rev-parse', '--verify',
			'-q', 'autoopt/3'], capture_output=True).returncode == 0,
			os.listdir(a3))
		suite.check('the winner\'s path keeps its checkouts and run files',
			all(os.path.exists(os.path.join(autoopt.iteration_dir(search, i),
			'.git')) for i in (0, 1, 2)) and os.path.exists(os.path.join(a2,
			'runs', 'seed-9', 'artifact.bin')))
		suite.check('saved output arrays go from every iteration, as prune '
			'does', not os.path.exists(os.path.join(a2, 'saved.npy')) and not
			os.path.exists(os.path.join(a3, 'saved.npy')))

		for name in ('harness', 'checks', 'reference', 'tools', 'elsewhere',
				'final'):
			suite.check('{}/ is untouched'.format(name), os.path.exists(
				os.path.join(search, name, 'sentinel.npy')))

		for name in ('config.json', 'spec.md', 'state.json', 'ideas.md',
				'knowledge.md', 'ended.json', 'ended.md'):
			suite.check('{} is kept'.format(name), os.path.exists(
				os.path.join(search, name)))

		ended = autoopt.read_json(os.path.join(search, 'ended.json'))
		cleanup = ended['cleanup']
		suite.check('and recorded in ended.json', cleanup['bytes_freed'] >=
			1000 and cleanup['files_removed'] >= 2 and cleanup[
			'worktrees_removed'] == 2 and cleanup['protected'][:3] == [0, 1,
			2], cleanup)
		suite.check('the dashboard was snapshotted and this search\'s '
			'publisher left running for the later stages', calls == [(search,
			('--export', os.path.join(search, 'final', 'dashboard.html')))]
			and not ended['dashboard']['publisher_stopped'], calls)
		suite.check('a plain-mode search is sent to step 5, shipping, with '
			'no word of after_end', 'Next: step 5, shipping' in out and
			'after_end' not in out.split('Next: step 5')[1], out[-300:])
		suite.check('ended.json records the machine the search ran on',
			set(ended.get('machine') or {}) >= {'os', 'arch', 'cpu', 'cores',
			'python'} and ended['machine']['cores'] == os.cpu_count(),
			ended.get('machine'))

		code, out = run_end(search)
		suite.check('ending twice refuses, naming --reopen', code == 1 and
			'--reopen' in out, out)

		# After the ending: best is the recorded winner, the gates close.
		tr.call([AUTOOPT, 'brief', search, '--id', '9', '--title', 't',
			'--category', 'MISC', '--tried', 'none', '--idea', 'x', '--gpus',
			'0'], expect=(1,))
		refused = tr.call([AUTOOPT, 'new', search, '--id', '9', '--title',
			't', '--category', 'MISC'], expect=(1,))
		suite.check('new needs --after-end once the search has ended',
			'--after-end' in refused and 'end --reopen' in refused, refused)
		refused = tr.call([AUTOOPT, 'loop', search], expect=(1,))
		suite.check('loop refuses', 'ended' in refused, refused)
		suite.equal('--id best is the winner ended.json names', autoopt.
			resolve_id(search, autoopt.load_config(search), 'best'), 2)

		# An iteration made after the end becomes the winner.
		tr.call([AUTOOPT, 'new', search, '--id', '9', '--parent', '2',
			'--title', 'notice', '--category', 'MISC', '--after-end'])
		meta = autoopt.load_meta(search, 9)
		suite.check('--after-end stamps the iteration', meta.get('after_end')
			is True, meta)
		directory = autoopt.iteration_dir(search, 9)
		tr.bench(directory, 0.70)
		tr.call([RUNNER, directory])
		write_readme(search, 9)
		refused = tr.call([AUTOOPT, 'promote', search, '--id', '3'],
			expect=(1,))
		suite.check('an iteration not made after the end is not promoted',
			'ended' in refused, refused)
		out = tr.call([AUTOOPT, 'promote', search, '--id', '9'])
		ended = autoopt.read_json(os.path.join(search, 'ended.json'))
		suite.equal('the promoted after_end iteration is appended to '
			'winner_changes and is the winner', ([c['label'] for c in ended[
			'winner_changes']], ended['winner']['id'], autoopt.resolve_id(
			search, autoopt.load_config(search), 'best')), (['A9'], 9, 9))
		suite.check('ended.md follows the winner', 'Winner changed after '
			'the end: A9' in read(os.path.join(search, 'ended.md')))

		# Reopen.
		del calls[:]
		code, out = run_end(search, reopen=True)
		suite.check('reopen archives ended.json and restarts the publisher',
			code == 0 and not os.path.exists(os.path.join(search,
			'ended.json')) and os.path.exists(os.path.join(search,
			'ended-1.json')) and calls == [(search, ('--start',))], (out,
			calls))
		tr.call([AUTOOPT, 'new', search, '--id', '10', '--title', 'again',
			'--category', 'MISC'])
		suite.check('after a reopen, new works without --after-end',
			autoopt.load_meta(search, 10) is not None)

		# A reopen is refused while a hold is recorded.
		code, out = run_end(search, reason='variations')
		suite.check('an iteration in flight refuses the second ending too',
			code == 1 and 'A10 is running' in out, out)
		directory = autoopt.iteration_dir(search, 10)
		tr.bench(directory, 0.5)
		tr.call([RUNNER, directory])
		write_readme(search, 10)
		code, out = run_end(search, reason='variations')
		suite.check('a second ending writes a new ended.json', code == 0 and
			autoopt.load_ended(search)['reason']['criterion'] == 'variations',
			out[-300:])
		autoopt.hold_reopen(search, 'a publisher', 'it is public')
		code, out = run_end(search, reopen=True)
		suite.check('reopen is refused while a hold is recorded, naming who '
			'holds it and why', code == 1 and 'it is public' in out and
			'a publisher' in out and os.path.exists(os.path.join(search,
			'ended.json')), out)
		autoopt.hold_reopen(search, 'a publisher', None)
		suite.check('a withdrawn hold removes the lock file', not
			os.path.exists(autoopt.reopen_lock_path(search)) and
			autoopt.reopen_holds(search) == [])

		# Reasons: the user's words, and none stated.
		other = build(os.path.join(base, 'b'))
		code, out = run_end(other, reason='the budget ran out on Friday')
		record = autoopt.load_ended(other)
		suite.equal('any other text is the user\'s words', (record['reason'][
			'criterion'], record['reason']['text']), ('user',
			'the budget ran out on Friday'))
		third = build(os.path.join(base, 'c'))
		code, out = run_end(third)
		suite.equal('no reason given, and the phase\'s worth not reached: '
			'none stated', autoopt.load_ended(third)['reason']['criterion'],
			None)

		# Repository mode: the winning iterations are the ones that change the
		# code.
		original = os.path.join(base, 'original')
		tr.make_original(original)
		harness = os.path.join(base, 'bench.py')

		with open(harness, 'w') as out:
			out.write(tr.HARNESS)

		repo_root = os.path.join(base, 'repo')
		repo_search = os.path.join(repo_root, 'autooptimize')
		tr.call([AUTOOPT, 'init', repo_root, '--task', 'test', '--repo',
			original, '--copy', harness, '--command', 'python3 {}/harness/'
			'bench.py --src {{src}} --seed {{seed}} --out {{out}}'.format(
			repo_search), '--objective', 'score', '--direction', 'max',
			'--gates', json.dumps([{'name': 'cost', 'op': '<=', 'threshold':
			1.0}]), '--observers', 'size', '--no-gpu', '--runs', '3',
			'--staged', '--confirm-runs', '3', '--promote', 'orchestrator'])
		tr.call([RUNNER, autoopt.iteration_dir(repo_search, 0)])
		tr.repo_add(repo_search, 1, 0, 0.60)
		tr.repo_add(repo_search, 2, 1, 0.70)

		for identifier in (1, 2):
			write_readme(repo_search, identifier)

		code, out = run_end(repo_search)
		record = autoopt.load_ended(repo_search)
		suite.equal('repository mode: the winner and its winning iterations',
			(code, record['winner']['id'], record['winning_iterations']),
			(0, 2, [1, 2]))
		suite.check('and the next step is shipping, as in plain mode',
			out.rstrip().endswith('The search is over. Next: step 5, shipping '
			'(`references/step5-ship.md`): ask the user how to ship.'),
			out[-400:])
	finally:
		autoopt.run_dashboard = real

	# The real dashboard commands: only this search's publisher stops.
	mine = os.path.join(base, 'mine', 'autooptimize')
	theirs = os.path.join(base, 'theirs', 'autooptimize')

	for folder in (mine, theirs):
		os.makedirs(folder)

		with open(os.path.join(folder, 'config.json'), 'w') as out:
			json.dump({}, out)

	writer = os.path.join(SCRIPTS, 'dashboard', 'monitor_writer.py')
	fakes = {}

	for name, folder in (('mine', mine), ('theirs', theirs)):
		fakes[name] = subprocess.Popen([sys.executable, '-c',
			'import time; time.sleep(120)', writer, folder, '--out',
			os.path.join(os.path.dirname(folder), 'monitor')],
			stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

	time.sleep(0.5)
	done = subprocess.run([sys.executable, MONITOR, mine, '--stop'],
		capture_output=True, text=True)
	time.sleep(0.5)
	suite.check('the real --stop on this search ends its publisher and not '
		'another search\'s', fakes['mine'].poll() is not None and fakes[
		'theirs'].poll() is None, (done.stdout, done.stderr))

	for process in fakes.values():
		process.kill()
		process.wait()

	# `archive` copies the winner out and writes the report's skeleton.
	mine = build(os.path.join(base, 'archive-built'))
	work = os.path.dirname(mine)
	done = subprocess.run([sys.executable, AUTOOPT, 'archive', mine],
		capture_output=True, text=True)
	folder = os.path.join(work, 'finish', 'archive')
	suite.check('archive writes the patch, the code, the README and the '
		'result of the winner', done.returncode == 0 and all(os.path.exists(
		os.path.join(folder, name)) for name in ('idea.patch', 'code',
		'README.md', 'result.json')), (done.stdout, done.stderr))
	report = os.path.join(work, 'finish', 'final-report.md')
	text = read(report) if os.path.exists(report) else ''
	suite.check('the final report has its headings in order and the path',
		[h for h in ('## What was optimized', '## Result', '## The path',
		'## What produced the gains', '## Where things are') if h in text]
		== ['## What was optimized', '## Result', '## The path',
		'## What produced the gains', '## Where things are']
		and '<<FILL' in text, text[:300])
	again = subprocess.run([sys.executable, AUTOOPT, 'archive', mine],
		capture_output=True, text=True)
	suite.check('archive refuses to replace an archive without --force',
		again.returncode != 0 and 'already holds' in again.stderr,
		again.stderr)

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
