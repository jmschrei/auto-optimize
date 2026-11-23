#!/usr/bin/env python3
"""Check a budget in idea-miner stages (`after.budget_stages`) and the
command set before the baseline runs: the setting is validated, `mine`
refuses a stage past the budget, `status`, `brief --next` and `stages.py`
say when it is spent, the search stops for `budget` once every idea is
tried, and `config --set command` works until A0 has run.

	python3 test_budget.py     # about twenty seconds

Real searches in a temporary directory, driven with the real scripts. No
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
import stages
import test_runner

AUTOOPT = test_runner.AUTOOPT
RUNNER = test_runner.RUNNER
IDEA = '- [MICRO] onehot.py:f: one idea. Why: a reason. Gain: some. Exact: ' \
	'bit-identical. (S{})\n'


def run(*args):
	return subprocess.run([sys.executable] + list(args), capture_output=True,
		text=True)


def refused(after):
	"""The message check_after fails with, or None when it accepts."""

	try:
		autoopt.check_after(dict(autoopt.AFTER_DEFAULTS, **after))
	except SystemExit:
		return 'refused'

	return None


def set_mining(search, stage, done):
	state = autoopt.load_state(search)
	state['mining'] = dict(state.get('mining') or {}, stage=stage, done=done)
	autoopt.save_state(search, state)


def write_ideas(search, untried, tried=0):
	with open(os.path.join(search, 'ideas.md'), 'w') as out:
		out.write('# Ideas\n\n## Queue\n')
		out.write(''.join('- ~~' + IDEA[2:].rstrip('\n').format(1) + '~~ A1: '
			'kept out\n' for _ in range(tried)))
		out.write(''.join(IDEA.format(1) for _ in range(untried)))


def main():
	suite = test_runner.Suite()
	base = tempfile.mkdtemp(prefix='budget-')

	suite.check('after.budget_stages takes a whole number of 1 or more',
		refused({'budget_stages': 3}) is None and refused(
		{'budget_stages': None}) is None)
	suite.check('and refuses 0, a fraction and true', all(refused(
		{'budget_stages': value}) for value in (0, 1.5, True)))

	search = test_runner.make_search(os.path.join(base, 'a'), staged=False,
		runs=3)
	work = os.path.dirname(search)

	done = run(AUTOOPT, 'config', work, '--set', 'after.budget_stages=2',
		'--reason', 'the user: two stages')
	suite.check('config --set after.budget_stages=2', done.returncode == 0
		and autoopt.load_config(search)['after']['budget_stages'] == 2,
		done.stderr[-300:])
	done = run(AUTOOPT, 'config', work, '--set', 'after.budget_stages=0',
		'--reason', 'x')
	suite.check('and refuses 0', done.returncode == 1, done.stderr[-300:])

	# One stage closed of two: the next opens as before.
	set_mining(search, 1, 1)
	write_ideas(search, untried=0, tried=2)
	stats = autoopt.stage_budget(search, autoopt.load_config(search))
	suite.equal('one of two stages closed is not spent', (stats['spent'],
		stats['exhausted']), (False, False))
	out = run(AUTOOPT, 'status', work, '--brief').stdout
	suite.check('status names the budget and calls for the miner',
		'budget: 2 idea-miner stage(s)' in out and 'run `autoopt.py mine'
		in out, out[-600:])

	# Both closed, one idea left: no stage opens, the idea is briefed first.
	set_mining(search, 2, 2)
	write_ideas(search, untried=1, tried=2)
	done = run(AUTOOPT, 'mine', work)
	suite.check('mine refuses a third stage, naming the budget and how to '
		'raise it', done.returncode == 1 and 'budget is 2 idea-miner stage(s)'
		in done.stderr and 'after.budget_stages=<more>' in done.stderr,
		done.stderr[-400:])
	out = run(AUTOOPT, 'status', work, '--brief').stdout
	suite.check('status says the budget is spent and to brief the idea left',
		'is spent, so no new stage opens: brief the 1 idea(s) left' in out
		and 'run `autoopt.py mine' not in out, out[-600:])
	config = autoopt.load_config(search)
	state = autoopt.load_state(search)
	suite.equal('not a reason to stop while an idea is left',
		autoopt.end_reason(search, config, state, None)['criterion'], None)

	# Every idea tried: the search stops for budget.
	write_ideas(search, untried=0, tried=3)
	reason = autoopt.end_reason(search, config, state, None)
	suite.check('every idea tried: end_reason derives budget', reason[
		'criterion'] == 'budget' and 'budget of 2 idea-miner stage(s)'
		in reason['text'], reason)
	suite.equal('stages.py stops for budget', stages.stop_word(search, config,
		state, stages.after_of(work)), 'budget')
	out = run(AUTOOPT, 'status', work, '--brief').stdout
	suite.check('status says the search stops for budget', 'every idea is '
		'tried or dropped: the search stops for `budget`' in out, out[-600:])
	state['setup'] = {'status': 'done'}
	autoopt.save_state(search, state)
	done = run(AUTOOPT, 'brief', work, '--next')
	suite.check('brief --next hands back for budget with nothing left',
		'stops for `budget` once the iterations in flight settle' in
		done.stdout, (done.stdout + done.stderr)[-600:])

	# A stage still open does not count as spent ideas.
	set_mining(search, 3, 2)
	suite.equal('a stage open keeps the search going', autoopt.stage_budget(
		search, autoopt.load_config(search))['exhausted'], False)

	# No budget: nothing changes.
	done = run(AUTOOPT, 'config', work, '--set', 'after.budget_stages=null',
		'--reason', 'the user: no limit')
	set_mining(search, 2, 2)
	suite.check('null unsets it, and mine opens a stage again',
		done.returncode == 0 and autoopt.load_config(search)['after'][
		'budget_stages'] is None and run(AUTOOPT, 'mine', work).returncode
		== 0, done.stderr[-300:])

	# A budget in iterations: after.budget_iterations.
	suite.check('after.budget_iterations takes a whole number of 1 or more',
		refused({'budget_iterations': 5}) is None and all(refused(
		{'budget_iterations': value}) for value in (0, 2.5, True)))
	counted = test_runner.make_search(os.path.join(base, 'c'), staged=False,
		runs=3)
	counted_work = os.path.dirname(counted)
	done = run(AUTOOPT, 'config', counted_work, '--set',
		'after.budget_iterations=2', '--reason', 'the user: two iterations')
	suite.check('config --set after.budget_iterations=2', done.returncode
		== 0 and autoopt.load_config(counted)['after']['budget_iterations']
		== 2, done.stderr[-300:])
	state = autoopt.load_state(counted)
	state['setup'] = {'status': 'done'}
	state['mining'] = {'stage': 1, 'done': 1}
	autoopt.save_state(counted, state)
	write_ideas(counted, untried=4)
	test_runner.add(counted, 1, 0.40)
	config = autoopt.load_config(counted)
	stats = autoopt.iteration_budget(counted, config)
	suite.equal('one of two iterations used is not spent', (stats['used'],
		stats['spent'], stats['exhausted']), (1, False, False))
	out = run(AUTOOPT, 'status', counted_work, '--brief').stdout
	suite.check('status names the iteration budget and what is used',
		'budget: 2 iteration(s), 1 used' in out, out[-600:])
	done = run(AUTOOPT, 'brief', counted_work, '--next', '--count', '3')
	suite.check('brief --next briefs no more than the budget leaves',
		done.returncode == 0 and len(autoopt.pending_briefs(counted)) == 1,
		(done.stdout + done.stderr)[-600:])
	shutil.rmtree(os.path.join(counted, 'briefs'))
	test_runner.add(counted, 2, 0.45)
	test_runner.call([AUTOOPT, 'new', counted, '--id', '3', '--title',
		'control', '--category', 'MISC', '--control'])
	test_runner.bench(autoopt.iteration_dir(counted, 3), 0.50)
	test_runner.call([RUNNER, autoopt.iteration_dir(counted, 3)])
	stats = autoopt.iteration_budget(counted, config)
	suite.equal('two used, the control not counted: spent and exhausted',
		(stats['used'], stats['spent'], stats['exhausted']), (2, True, True))
	reason = autoopt.end_reason(counted, config, autoopt.load_state(counted),
		None)
	suite.check('end_reason derives budget from the iterations',
		reason['criterion'] == 'budget' and 'budget of 2' in reason['text'],
		reason)
	done = run(AUTOOPT, 'brief', counted_work, '--next')
	suite.check('brief --next briefs nothing and says to hand back',
		'budget of 2 iteration(s) (after.budget_iterations) is used' in
		done.stdout and not autoopt.pending_briefs(counted),
		(done.stdout + done.stderr)[-600:])
	done = run(AUTOOPT, 'brief', counted_work, '--id', '4', '--title', 'x',
		'--category', 'MICRO', '--tried', 'none', '--idea', 'one more')
	suite.check('brief --id refuses past the budget, naming how to raise it',
		done.returncode == 1 and 'after.budget_iterations=<more>' in
		done.stderr, done.stderr[-400:])
	done = run(AUTOOPT, 'brief', counted_work, '--id', '4', '--control',
		'--title', 'control', '--category', 'MISC', '--tried', 'none',
		'--idea', 'Change nothing.')
	suite.check('a same-code control is still briefed', done.returncode == 0,
		done.stderr[-400:])
	done = run(AUTOOPT, 'mine', counted_work)
	suite.check('mine refuses once the iterations are used', done.returncode
		== 1 and 'after.budget_iterations' in done.stderr, done.stderr[-400:])
	out = run(AUTOOPT, 'status', counted_work, '--brief').stdout
	suite.check('status says the search stops for budget', 'budget of 2 '
		'iteration(s) is used and every one has settled' in out, out[-600:])

	# A budget in hours: brief --next briefs nothing once it is spent.
	done = run(AUTOOPT, 'config', counted_work, '--set',
		'after.budget_iterations=null', '--reason', 'x')
	done = run(AUTOOPT, 'config', counted_work, '--set',
		'after.budget_hours=0.5', '--reason', 'the user: half an hour')
	state = autoopt.load_state(counted)
	state['setup'] = {'status': 'done', 'started': '2020-01-01T00:00:00'}
	autoopt.save_state(counted, state)
	shutil.rmtree(os.path.join(counted, 'briefs'), ignore_errors=True)
	done = run(AUTOOPT, 'brief', counted_work, '--next')
	suite.check('brief --next briefs nothing once the hours are spent',
		'budget of 0.5 hour(s) (after.budget_hours) is spent' in done.stdout
		and not autoopt.pending_briefs(counted),
		(done.stdout + done.stderr)[-600:])

	# `none` is a choice of after.ship, and unsets any other after key.
	done = run(AUTOOPT, 'config', counted_work, '--set', 'after.ship=none',
		'--reason', 'the user: not now')
	suite.check('config --set after.ship=none records the choice none',
		done.returncode == 0 and autoopt.load_config(counted)['after'][
		'ship'] == 'none', done.stderr[-300:])
	done = run(AUTOOPT, 'config', counted_work, '--set',
		'after.budget_hours=none', '--reason', 'the user: no limit')
	suite.check('and none unsets a key without that choice', done.returncode
		== 0 and autoopt.load_config(counted)['after']['budget_hours'] is
		None, done.stderr[-300:])

	# The command: settable until the baseline runs.
	fresh = os.path.join(base, 'b')
	test_runner.call([AUTOOPT, 'init', fresh, '--task', 'test', '--command',
		'python3 bench.py --seed {seed} --out {out}', '--objective', 'score',
		'--direction', 'max', '--gates', json.dumps([{'name': 'cost', 'op':
		'<=', 'threshold': 1.0}]), '--observers', 'size', '--no-gpu'])
	root = os.path.join(fresh, 'autooptimize')
	command = ('python3 bench.py --seed {seed} --out {out} '
		'--iteration {iteration}')
	done = run(AUTOOPT, 'config', fresh, '--set', 'command=' + command,
		'--reason', 'step 2: the benchmark needs its iteration')
	suite.check('before the baseline, config --set command works',
		done.returncode == 0 and autoopt.load_config(root)['command'] ==
		command, done.stderr[-300:])
	done = run(AUTOOPT, 'config', fresh, '--set', 'command=python3 bench.py',
		'--reason', 'x')
	suite.check('and still checks the placeholders', done.returncode == 1
		and '{seed}' in done.stderr, done.stderr[-300:])
	done = run(AUTOOPT, 'config', work, '--set', 'command=' + command,
		'--reason', 'x')
	suite.check('after the baseline ran, it is a new search', done.returncode
		== 1 and 'A0 has run under it' in done.stderr, done.stderr[-300:])

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
