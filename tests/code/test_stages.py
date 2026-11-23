#!/usr/bin/env python3
"""Check `stages.py`: each stage's status comes from the state files, the
next move follows from them, and NOTES.md is the same bytes for the same
state.

	python3 test_stages.py     # a second

A hand-built search directory, advanced one stage at a time. No network.
"""

import os
import sys
import json
import shutil
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
import ship
import stages
import test_runner as tr


def put(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)

	with open(path, 'w') as out:
		out.write(data if isinstance(data, str) else json.dumps(data))


def status(work):
	return {name: state for name, state, _, _ in stages.collect(work)}


def handoffs(suite):
	"""Each point where the next command is handed to the session."""

	work = tempfile.mkdtemp(prefix='stages-handoffs-')
	root = os.path.join(work, 'autooptimize')
	config = {'task': 'toy', 'command': 'x {seed} {out}', 'objective':
		{'name': 's', 'direction': 'min'}, 'after': {}}
	put(os.path.join(root, 'config.json'), config)
	put(os.path.join(root, 'spec.md'), '# spec\n')

	def step(state=None, after=None):
		config['after'] = after or {}
		put(os.path.join(root, 'config.json'), config)
		put(os.path.join(root, 'state.json'), state or {})
		rows = stages.collect(work)
		return stages.next_action(work, rows)

	name, kind, text = step()
	suite.check('after the interview: setup alone; the miner waits for A0\'s '
		'profile', kind == 'agent' and ' setup ' in text
		and ' mine ' not in text and 'after setup' in text, text)

	config['repo'] = 'owner/name'
	name, kind, text = step()
	suite.check('repository mode starts the same way: setup alone', kind ==
		'agent' and ' setup ' in text and text.count('.py ') == 1, text)
	del config['repo']

	name, kind, text = step({'setup': {'status': 'waiting', 'question':
		'which input?'}})
	suite.check('a setup question stops, with the answer command',
		kind == 'stop' and 'which input?' in text and '--answer' in text,
		text)

	name, kind, text = step({'setup': {'status': 'open'}})
	suite.check('an open setup waits and names --force', kind == 'agent'
		and '--force' in text, text)

	name, kind, text = step({'setup': {'status': 'done'}, 'mining': {}})
	suite.check('setup done, S1 not opened: start the miner now',
		kind == 'agent' and ' mine ' in text and 'S1' in text
		and 'wait' not in text, text)

	name, kind, text = step({'setup': {'status': 'done'}, 'mining':
		{'stage': 1, 'done': 0}})
	suite.check('S1 open: wait for the miner',
		kind == 'agent' and 'wait for the idea miner' in text
		and '--force' in text, text)

	done = {'setup': {'status': 'done', 'started': '2026-11-07T00:00:00'},
		'mining': {'done': True}}
	name, kind, text = step(done)
	suite.check('ready: start the orchestrator', kind == 'agent' and
		' loop ' in text and 'spawn the orchestrator' in text, text)

	name, kind, text = step(dict(done, loop={'open': True, 'phase': 2}))
	suite.check('an open phase waits for its report: the orchestrator closes '
		'it, and --force is for one that died', kind == 'agent' and
		'closes the phase itself' in text and '--force' in text, text)

	name, kind, text = step(done, {'budget_hours': 0.001})
	suite.check('a spent budget asks when after.stop is ask',
		kind == 'stop' and 'budget' in text, text)
	name, kind, text = step(done, {'budget_hours': 0.001, 'stop': 'auto'})
	suite.check('and ends the search when after.stop is auto',
		kind == 'run' and ' end ' in text and '--reason budget' in text
		and '--background' in text, text)

	put(os.path.join(root, 'end.log'), 'working\n')
	suite.check('an end still running is waited on, not started again',
		stages.next_action(work, stages.collect(work))[1] == 'agent'
		and ' wait ' in stages.next_action(work, stages.collect(work))[2]
		and 'end.log' in stages.next_action(work, stages.collect(work))[2], '')
	put(os.path.join(root, 'end.log'), 'working\nexit 0\n')
	shutil.rmtree(work, ignore_errors=True)


def main():
	suite = tr.Suite()
	handoffs(suite)
	work = tempfile.mkdtemp(prefix='stages-')
	root = os.path.join(work, 'autooptimize')

	suite.equal('nothing is started in an empty directory',
		set(status(work).values()), {'not started'})

	put(os.path.join(root, 'config.json'), {'task': 'toy', 'command':
		'x {seed} {out}', 'objective': {'name': 's', 'direction': 'min'}})
	put(os.path.join(root, 'spec.md'), '- Off limits: TODO (say)\n')
	suite.equal('a step 0 TODO keeps the interview running',
		status(work)['interview'], 'running')
	put(os.path.join(root, 'spec.md'), '- Gate proof: TODO (step 2)\n'
		'- Width measured: TODO (step 2)\n- Benchmark: TODO (step 2)\n')
	suite.equal('the three lines step 2 answers do not',
		status(work)['interview'], 'complete')
	put(os.path.join(root, 'spec.md'), '# spec\n')
	suite.equal('a config and a spec complete the interview',
		status(work)['interview'], 'complete')

	put(os.path.join(root, 'state.json'), {'setup': {'status': 'waiting',
		'question': 'which input?'}})
	suite.equal('a setup question leaves setup running',
		status(work)['setup'], 'running')

	put(os.path.join(root, 'state.json'), {'setup': {'status': 'done',
		'finished': '2026-11-07T10:00:00'}})
	put(os.path.join(root, 'iterations', '0', 'result.json'),
		{'objective': {'median': 2.5}})
	suite.equal('a closed setup is complete', status(work)['setup'], 'complete')

	put(os.path.join(root, 'iterations', '1', 'result.json'),
		{'objective': {'median': 2.0}})
	suite.equal('a settled iteration starts the search',
		status(work)['search'], 'running')

	put(os.path.join(root, 'ended.json'), {'ended_at': '2026-11-08T09:00:00',
		'counts': {'rounds': 2, 'iterations': 7, 'winning_iterations': 3},
		'medians': {'A0': 2.5, 'winner': 1.0, 'objective': 's', 'unit': 's'},
		'paired': {}, 'winner': {'id': 3, 'label': 'A3'}})
	rows = {name: (state, text) for name, state, text, _
		in stages.collect(work)}
	suite.equal('ended.json completes the search and the final',
		(rows['search'][0], rows['final'][0]), ('complete', 'complete'))
	suite.check('the final row reads the medians', 'A0 2.5 -> 1' in
		rows['final'][1], rows['final'])

	suite.equal('six stages, and no others', [name for name, _,
		_, _ in stages.collect(work)], ['interview', 'setup', 'search', 'final',
		'ship', 'finish'])
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('after the end the next move is to ask how to ship',
		(name, kind) == ('ship', 'agent') and 'step5-ship.md' in text and
		'ship.py plan' in text, text)
	suite.equal('the ship stage waits for the answer', status(work)['ship'],
		'not started')

	config = json.load(open(os.path.join(root, 'config.json')))
	config.setdefault('after', {})['ship'] = 'apply'
	put(os.path.join(root, 'config.json'), config)
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('once the user chose to apply, the next move is the apply '
		'preview', status(work)['ship'] == 'running' and (name, kind) ==
		('ship', 'agent') and 'ship.py apply' in text, (status(work), text))
	put(os.path.join(work, 'ship', 'applied.json'), {'files': ['x.py']})
	suite.equal('ship/applied.json completes it', status(work)['ship'],
		'complete')
	put(os.path.join(work, 'ship', 'applied.json'), {'id': 2, 'label': 'A2',
		'files': ['x.py']})
	rows = {name: (state, text) for name, state, text, _
		in stages.collect(work)}
	suite.check('a winner promoted after the apply runs the stage again, '
		'naming both', rows['ship'][0] == 'running' and 'A2' in rows['ship'][1]
		and 'A3' in rows['ship'][1], rows['ship'])
	put(os.path.join(work, 'ship', 'applied.json'), {'id': 3, 'files': []})
	suite.equal('the winner\'s own record completes it', status(work)['ship'],
		'complete')

	config['after'] = {'ship': 'branch', 'branch_prefix': 'no'}
	put(os.path.join(root, 'config.json'), config)
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('a branch on a plain search is not offered: the question '
		'again, not a command that refuses', status(work)['ship'] ==
		'not started' and 'ship.py plan' in text, (status(work), text))

	# A repository on GitHub with step 1's bugs, and no suite.
	config['repo'] = {'path': '/x', 'commit': 'abc', 'remote_url':
		'https://github.com/o/r.git'}
	os.makedirs(os.path.join(work, 'tests', 'work', 'bugs', 'wrong-sum'))
	put(os.path.join(root, 'config.json'), config)
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('the branch chosen on a repository search: its preview next',
		status(work)['ship'] == 'running' and (name, kind) == ('ship',
		'agent') and 'ship.py branch' in text, (status(work), text))
	put(os.path.join(work, 'ship', 'branch.json'), {'id': 3, 'branch':
		'optimized'})
	suite.equal('ship/branch.json of the winner completes it',
		status(work)['ship'], 'complete')
	put(os.path.join(work, 'ship', 'branch.json'), {'id': 2, 'branch':
		'optimized'})
	suite.equal('and a branch of an earlier winner does not',
		status(work)['ship'], 'running')

	for after, why in (({'ship': 'apply'}, 'apply on a repository search'),
			({'ship': 'github', 'branch_prefix': 'no', 'ship_documents': []},
			'GitHub with no document checked'), ({'ship': 'github',
			'branch_prefix': 'no', 'ship_documents': ['tests', 'optimization']},
			'the unit-test PR without a suite from step 1')):
		config['after'] = after
		put(os.path.join(root, 'config.json'), config)
		name, kind, text = stages.next_action(work, stages.collect(work))
		suite.check('{} is no answer: the question again'.format(why),
			status(work)['ship'] == 'not started' and 'ship.py plan' in text,
			(status(work), text))

	config['after'] = {'ship': 'github', 'ship_documents': ['bugs',
		'optimization']}
	put(os.path.join(root, 'config.json'), config)
	suite.equal('GitHub chosen, and the branch names not asked yet, is still '
		'the question', status(work)['ship'], 'not started')
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('so the next move is still to ask', 'ship.py plan' in text,
		text)
	config['after']['branch_prefix'] = 'no'
	put(os.path.join(root, 'config.json'), config)

	def move():
		return stages.next_action(work, stages.collect(work))[2]

	text = move()
	suite.check('nothing written yet: the brief, and the document writer it '
		'spawns', ' brief ' in text and 'document writer' in text and
		'*The document writer*' in text, text)
	for folder in ('issue-bugs', 'pr-optimization'):
		put(os.path.join(work, 'ship', folder, 'body.title'), 'A title\n')
	text = move()
	suite.check('every document rendered and no rehearsal: rehearse',
		' rehearse ' in text and 'no rehearsal yet' in text, text)
	after = stages.after_of(work)
	digest = ship.inputs_digest(work, ['bugs', 'optimization'], after)
	rehearsal = os.path.join(work, 'ship', 'rehearsal.json')
	put(rehearsal, {'digest': digest, 'documents': ['bugs', 'optimization'],
		'ok': True, 'branches': {}})
	text = move()
	suite.check('a rehearsal that holds: show the user, then post with its '
		'digest', 'show the user' in text and 'post {} all --approved '
		'--digest {}'.format(work, digest) in text and
		'*Review, then post all*' in text, text)
	put(os.path.join(work, 'ship', 'pr-optimization', 'body.title'), 'B\n')
	text = move()
	suite.check('inputs changed since: rehearse again', ' rehearse ' in text
		and 'inputs changed' in text, text)
	put(os.path.join(work, 'ship', 'pr-optimization', 'body.title'),
		'A title\n')
	put(rehearsal, {'digest': digest, 'documents': ['bugs', 'optimization'],
		'ok': False, 'branches': {}})
	text = move()
	suite.check('a rehearsal with problems: rehearse again', ' rehearse ' in
		text and 'had problems' in text, text)
	put(rehearsal, {'digest': digest, 'documents': ['bugs', 'optimization'],
		'ok': True, 'branches': {}})
	put(os.path.join(work, 'ship', 'issue-bugs', 'filed.json'), {'url':
		'https://github.com/o/r/issues/4', 'comments_left': 1})
	suite.equal('an issue with a comment still to post is not posted',
		status(work)['ship'], 'running')
	put(os.path.join(work, 'ship', 'issue-bugs', 'filed.json'), {'url':
		'https://github.com/o/r/issues/4', 'comments_left': 0})
	suite.equal('one of two chosen documents posted is still running',
		status(work)['ship'], 'running')
	text = move()
	suite.check('and the move is the same post again, which skips it',
		'1 of 2' in text and 'the same command again' in text and
		'--digest {}'.format(digest) in text, text)
	put(os.path.join(work, 'ship', 'pr-optimization', 'pr.json'), {'url':
		'https://github.com/o/r/pull/5'})
	suite.equal('every chosen document posted completes it',
		status(work)['ship'], 'complete')

	del config['repo']
	config['after'] = {'ship': 'none'}
	put(os.path.join(root, 'config.json'), config)
	suite.equal('"not now" completes it', status(work)['ship'], 'complete')
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('then the finish, as a plain search\'s archive, a command '
		'that runs', (name, kind) == ('finish', 'run') and ' archive ' in text
		and text.startswith('python3 '), text)
	config['repo'] = {'path': '/x', 'commit': 'abc'}
	put(os.path.join(root, 'config.json'), config)
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('a repository search finishes the same way, by the finish '
		'step\'s reference, the archive named', (name, kind) == ('finish',
		'agent') and 'step6-finish.md' in text and ' archive ' in text, text)

	put(os.path.join(work, 'finish', 'final-report.md'), '# report\n\n'
		'<<FILL: the gain by category>>\n')
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('the skeleton archive writes leaves the finish running',
		status(work)['finish'] == 'running' and name == 'finish',
		(status(work), text))
	put(os.path.join(work, 'finish', 'final-report.md'), '# report\n')
	suite.equal('a final report completes the finish', status(work)['finish'],
		'complete')
	suite.equal('and nothing is next', stages.next_action(work,
		stages.collect(work))[:2], (None, 'done'))

	stages.render(work)
	first = open(os.path.join(work, 'NOTES.md')).read()
	stages.render(work)
	suite.equal('the same state renders the same bytes', first,
		open(os.path.join(work, 'NOTES.md')).read())
	suite.check('NOTES.md lists the six stages', all('| {} |'.format(name)
		in first for name, _ in stages.STAGES), first[-300:])

	config['after'] = {'theirs': 'yes', 'another': 3}
	cleaned = autoopt.check_after(dict(autoopt.AFTER_DEFAULTS,
		**config['after']))
	suite.check('check_after keeps a key that is not its own as it is',
		cleaned.get('theirs') == 'yes' and cleaned.get('another') == 3,
		cleaned)

	# A failed `end` is shown to the main session, not started again.
	log = os.path.join(work, 'end.log')
	with open(log, 'w') as out:
		out.write('running final\nerror: final did not finish (1)\nexit 1\n')
	suite.check('end_failure reads a failed end.log', 'exit 1' in (
		stages.end_failure(log) or '') and 'final did not finish' in
		stages.end_failure(log))
	with open(log, 'w') as out:
		out.write('done\nexit 0\n')
	suite.equal('and passes over one that succeeded', stages.end_failure(log),
		None)

	shutil.rmtree(work, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
