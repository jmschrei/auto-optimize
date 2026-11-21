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
sys.path.insert(0, HERE)

import autoopt
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
		'paired': {}})
	rows = {name: (state, text) for name, state, text, _
		in stages.collect(work)}
	suite.equal('ended.json completes the search and the final',
		(rows['search'][0], rows['final'][0]), ('complete', 'complete'))
	suite.check('the final row reads the medians', 'A0 2.5 -> 1' in
		rows['final'][1], rows['final'])

	suite.equal('five stages, and no others', [name for name, _,
		_, _ in stages.collect(work)], ['interview', 'setup', 'search', 'final',
		'finish'])
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('after the end the next move is the finish, as a plain '
		'search\'s archive', (name, kind) == ('finish', 'run') and
		' archive ' in text, text)
	config = json.load(open(os.path.join(root, 'config.json')))
	config['repo'] = {'path': '/x', 'commit': 'abc'}
	put(os.path.join(root, 'config.json'), config)
	name, kind, text = stages.next_action(work, stages.collect(work))
	suite.check('a repository search finishes the same way, by the finish '
		'step\'s reference', (name, kind) == ('finish', 'agent') and
		'step5-finish.md' in text, text)

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
	suite.check('NOTES.md lists the five stages', all('| {} |'.format(name)
		in first for name, _ in stages.STAGES), first[-300:])

	config['after'] = {'theirs': 'yes', 'another': 3}
	cleaned = autoopt.check_after(dict(autoopt.AFTER_DEFAULTS,
		**config['after']))
	suite.check('check_after keeps a key that is not its own as it is',
		cleaned.get('theirs') == 'yes' and cleaned.get('another') == 3,
		cleaned)

	shutil.rmtree(work, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
