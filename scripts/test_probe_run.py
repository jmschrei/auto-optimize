#!/usr/bin/env python3
"""Check the small shared helpers: the probe runner of `paired.py` and
`cold.py`, and the `--background` re-detach filter.

	python3 test_probe_run.py     # a few seconds
"""

import os
import sys
import json
import time
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autoopt
import probe_run
import test_runner as tr


def main():
	suite = tr.Suite()
	base = tempfile.mkdtemp(prefix='probe-')
	cache = os.path.join(base, 'cache')

	def seconds(command, **kwargs):
		return probe_run.run_probe(command, base, 'a', cache, **kwargs)

	suite.equal('the last number of the last line', seconds(
		"echo noise; echo 'took 1.25'"), 1.25)
	suite.equal('a pattern picks the seconds when the last number is not them',
		seconds("echo 't=1.5s on GPU0'", pattern=r't=([0-9.]+)s'), 1.5)
	suite.equal('without the pattern that line reads 0',
		seconds("echo 't=1.5s on GPU0'"), 0.0)
	suite.equal('the side and the directory are shell-quoted words',
		seconds("test {side} = a && test -d {src} && echo 2"), 2.0)
	suite.equal('every compiler cache is under the one directory given',
		seconds("test \"$NUMBA_CACHE_DIR\" = {c} && test \"$TRITON_CACHE_DIR\" "
		"= {c}/triton && test \"$TORCHINDUCTOR_CACHE_DIR\" = {c}/inductor "
		"&& echo 3".replace('{c}', cache)), 3.0)
	suite.equal('an extra KEY=VALUE reaches the command', seconds(
		'echo $PROBE_X', extra=['PROBE_X=4']), 4.0)

	for command, text in (('exit 3', 'failed'), ('echo none', 'no number'),
			('echo "t=x"', 'no number')):
		done = subprocess.run([sys.executable, '-c', 'import sys; sys.path.'
			'insert(0, {!r}); import probe_run; probe_run.run_probe({!r}, '
			'{!r}, "a", {!r}, pattern={!r})'.format(HERE, command, base,
			cache, 't=([0-9.]+)s' if 't=' in command else None)],
			capture_output=True, text=True)
		suite.check('{!r} exits with {!r}'.format(command, text),
			done.returncode != 0 and text in done.stderr, done.stderr)

	for side in ('a', 'b'):
		os.makedirs(os.path.join(base, side), exist_ok=True)

	done = subprocess.run([sys.executable, os.path.join(HERE, 'paired.py'),
		'--a', os.path.join(base, 'a'), '--b', os.path.join(base, 'b'),
		'--cmd', "echo 't=1.5s on GPU0'", '--regex', 't=([0-9.]+)s',
		'--min-pairs', '2', '--max-pairs', '3', '--json',
		'--log', os.path.join(base, 'log.txt')], capture_output=True,
		text=True)
	suite.check('paired.py takes --regex and writes its log', done.returncode
		== 0 and json.loads(done.stdout)['pairs'] == 3 and os.path.getsize(
		os.path.join(base, 'log.txt')) > 0, done.stderr)

	paired = lambda command, *flags: subprocess.run([sys.executable,
		os.path.join(HERE, 'paired.py'), '--a', os.path.join(base, 'a'), '--b',
		os.path.join(base, 'b'), '--cmd', command, '--min-pairs', '2',
		'--max-pairs', '3', '--json', '--log', os.path.join(base, 'cap.txt')]
		+ list(flags), capture_output=True, text=True)
	done = paired('sleep 0.4; echo 1', '--cap-hours', '0.0003')

	with open(os.path.join(base, 'cap.txt')) as handle:
		logged = handle.read()

	suite.check('a comparison whose least pairs pass --cap-hours exits 3 '
		'before the first pair, with its price', done.returncode == 3 and
		'over the cap' in done.stderr and 'priced: a pair is about' in logged
		and 'pair 1' not in logged, done.stderr + logged)
	done = paired('sleep 0.1; echo 1', '--cap-hours', '1')
	suite.check('under the cap it runs', done.returncode == 0 and json.loads(
		done.stdout)['pairs'] == 3, done.stderr)
	for side in ('a', 'b'):
		os.makedirs(os.path.join(base, side), exist_ok=True)

	slow_pair = autoopt.run_paired({'checks': {'time': 'sleep 0.4; echo 1'},
		'after': {'pair_cap_hours': 0.0003}}, os.path.join(base, 'a'),
		os.path.join(base, 'b'), os.path.join(base, 'final-paired.log'))
	suite.check('`end`\'s paired timing past after.pair_cap_hours is `over '
		'cap`, with its price as the summary', slow_pair['verdict'] == 'over '
		'cap' and 'priced' in slow_pair['summary'], slow_pair)
	marker = os.path.join(base, 'ran-once')
	start = time.time()
	done = paired('if [ -e {0}.{{side}} ]; then sleep 30; fi; touch '
		'{0}.{{side}}; echo 1'.format(marker), '--run-timeout', '1')
	suite.check('a recorded run that hangs is killed at --run-timeout and '
		'exits 4', done.returncode == 4 and 'timed out' in done.stderr and
		time.time() - start < 15, '{} {}'.format(done.returncode, done.stderr))

	priced = os.path.join(base, 'priced')
	os.makedirs(os.path.join(priced, 'iterations', '0'))
	slow = {'checks': {'time': 'x', 'threads': 1}, 'repo': 'x', 'confirm_runs':
		5, 'runs_per_iteration': 5}
	suite.equal('no price before A0 has a run', autoopt.price_lines(priced,
		slow), [])

	with open(os.path.join(priced, 'iterations', '0', 'result.json'), 'w') \
			as out:
		json.dump({'n_runs': 5, 'seconds': 5 * 2400.0, 'tests': {'seconds':
			600.0}}, out)

	lines = autoopt.price_lines(priced, slow)
	suite.check('a 40-minute base: the paired timing is priced at 6.0 h on '
		'the base\'s side, past the 4 h cap, and the user is to be asked',
		any('6.0 h  the paired timing' in line and line.lstrip().startswith(
		'!') for line in lines) and any('`end` will refuse' in line for line
		in lines) and 'setup --ask' in lines[-1] and any('3.5 h  a re-measurement '
		'of the base' in line for line in lines), lines)

	with open(os.path.join(priced, 'iterations', '0', 'result.json'), 'w') \
			as out:
		json.dump({'n_runs': 5, 'seconds': 10.0}, out)

	lines = autoopt.price_lines(priced, slow)
	suite.check('a 2-second base is priced with no flag and no question',
		not any('!' in line for line in lines) and 'setup --ask' not in
		lines[-1] and '2.0 s' in lines[0], lines)

	found = {}
	autoopt.run_shell("for i in 1 2 3; do python3 -c 'import time\nt = "
		"time.time()\nwhile time.time() - t < 1: pass' & done; wait", base,
		sample=found)
	suite.check('a run of three busy processes shows three threads running '
		'at once', found.get('busy_peak', 0) >= 3 and found['samples'] > 2,
		found)
	found = {}
	autoopt.run_shell('sleep 0.5', base, sample=found)
	suite.check('and a run that only sleeps shows at most the shell starting',
		found.get('busy_peak', 0) <= 1, found)
	import run_iteration

	os.makedirs(os.path.join(priced, 'iterations', '0', 'runs', 'seed-0'))

	with open(os.path.join(priced, 'iterations', '0', 'runs', 'seed-0',
			'workers.json'), 'w') as out:
		json.dump({'busy_peak': 1, 'procs_peak': 2, 'samples': 9}, out)

	seen = run_iteration.workers_seen(os.path.join(priced, 'iterations', '0'),
		[{'dir': 'runs/seed-0'}, {'dir': 'runs/seed-1'}], 4)
	suite.equal('the runs\' worker samples are gathered into the result', seen,
		{'expected': 4, 'busy_peak': 1, 'procs_peak': 2, 'samples': 9})
	os.environ['AUTOOPT_SKIP_SPEC_CHECK'] = os.environ['AUTOOPT_SKIP_SMOKE'] \
		= '1'
	four = {'workers': 4, 'gpu': False, 'objective': {'direction': 'min'}}

	with open(os.path.join(priced, 'iterations', '0', 'result.json'), 'w') \
			as out:
		json.dump({'status': 'ok', 'stage': 'confirmed', 'workers': seen,
			'objective': {'median': 0.5}}, out)

	owed = [line for line in autoopt.setup_missing(priced, four) if
		'workers' in line]
	suite.check('setup does not close when the task names 4 workers and A0 '
		'ran one', len(owed) == 1 and 'never had more than 1 thread' in
		owed[0], owed)

	with open(os.path.join(priced, 'iterations', '0', 'result.json'), 'w') \
			as out:
		json.dump({'status': 'ok', 'stage': 'confirmed', 'workers': dict(seen,
			busy_peak=4), 'objective': {'median': 0.5}, 'n_runs': 5, 'seconds':
			2.5}, out)

	suite.equal('and closes on that count when A0 ran four', [line for line in
		autoopt.setup_missing(priced, four) if 'workers' in line], [])
	low = dict(four, floor=0.1, confirm_runs=5)
	lines = autoopt.price_lines(priced, low)
	suite.check('an A0 five times the floor is marked: a 5x gain ends the '
		'search there', any('A0 is 5 times the floor' in line and '!' in line
		for line in lines) and any('size the input' in line for line in
		lines) and 'setup --ask' in lines[-1], lines)
	suite.check('an A0 fifty times the floor is not', not any('!' in line
		for line in autoopt.price_lines(priced, dict(low, floor=0.01))),
		autoopt.price_lines(priced, dict(low, floor=0.01)))
	suite.equal('the root at the floor is reached; above it, or with no '
		'floor, or maximized, it is not', (autoopt.floor_reached(priced, dict(
		low, floor=0.5), {'root': 0}), autoopt.floor_reached(priced, low,
		{'root': 0}), autoopt.floor_reached(priced, four, {'root': 0}),
		autoopt.floor_reached(priced, dict(low, floor=9, objective={
		'direction': 'max'}), {'root': 0})), (True, False, False, False))

	suite.equal('and it is the reason the search stops on', autoopt.end_reason(
		priced, dict(low, floor=0.5), {'root': 0}, None)['criterion'], 'floor')

	began = os.path.join(base, 'began')
	os.makedirs(began)

	with open(os.path.join(began, 'config.json'), 'w') as out:
		json.dump({'skill': {'commit': 'a' * 40}}, out)

	real = autoopt.skill_commit

	try:
		autoopt.skill_commit = lambda: 'a' * 40
		suite.equal('a search on the skill commit it began on gets no notice',
			autoopt.skill_notice(began), None)
		autoopt.skill_commit = lambda: 'b' * 40
		peek = autoopt.skill_notice(began, mark=False)
		line = autoopt.skill_notice(began)
		suite.check('a changed skill is one NOTICE line naming both commits, '
			'and it says nothing stops', (line or '').startswith('NOTICE') and
			'a' * 10 in line and 'b' * 10 in line and 'Nothing stops' in line
			and peek == line, line)
		suite.equal('reported once', autoopt.skill_notice(began), None)
		autoopt.skill_commit = lambda: 'c' * 40
		line = autoopt.skill_notice(began)
		suite.check('a later change is reported against the one last seen',
			'since ' + 'b' * 10 in (line or '') and 'began on ' + 'a' * 10 in
			line, line)
		old = os.path.join(base, 'older')
		os.makedirs(old)
		suite.equal('a search with no recorded commit takes the current one',
			(autoopt.skill_notice(old), autoopt.read_json(os.path.join(old,
			'skill-seen.json'))['began']), (None, 'c' * 40))
		autoopt.skill_commit = lambda: None
		suite.equal('outside a git checkout there is no notice',
			autoopt.skill_notice(began), None)
	finally:
		autoopt.skill_commit = real

	suite.check('the skill\'s own commit is read from its checkout', real()
		is None or len(real()) == 40, real())

	# `--back` is an argparse abbreviation of --background: the detached
	# child must not see it, or it detaches again.
	script = os.path.join(base, 'argv.py')

	with open(script, 'w') as out:
		out.write('import sys, json\nprint(json.dumps(sys.argv[1:]))\n')

	log = os.path.join(base, 'argv.log')
	autoopt.detach(script, ['final', '/x', '--back', '--background', '--bac',
		'--b', '--keep'], log)

	for _ in range(50):
		if os.path.exists(log) and 'exit' in open(log).read():
			break

		time.sleep(0.1)

	line = open(log).read().splitlines()[0]
	suite.equal('the child sees neither --background nor an abbreviation of it',
		json.loads(line), ['final', '/x', '--keep'])

	# `spec --set` takes FIELD VALUE and FIELD=VALUE.
	search = tr.make_search(os.path.join(base, 'spec-search'), staged=False)
	environment = dict(os.environ, AUTOOPT_SKIP_SPEC_CHECK='1')

	for words, want in ((['Gate proof', 'one 1'], 'one 1'),
			(['Gate proof=two = 2'], 'two = 2')):
		done = subprocess.run([sys.executable, tr.AUTOOPT, 'spec', search,
			'--set'] + words, capture_output=True, text=True, env=environment)
		text = open(os.path.join(search, 'spec.md')).read()
		suite.check('spec --set {!r}'.format(words), done.returncode == 0 and
			'- Gate proof: {}'.format(want) in text, done.stderr)

	# `tests-branch`: the checks, the clone, the branch and the squash.
	origin = os.path.join(base, 'origin.git')
	original = os.path.join(base, 'original')
	work = os.path.join(base, 'tb-work')
	tr.git('init', '-q', '--bare', origin)
	tr.git('init', '-q', '-b', 'main', original)
	tr.git('-C', original, 'remote', 'add', 'origin', origin)

	with open(os.path.join(original, 'a.py'), 'w') as out:
		out.write('x = 1\n')

	tr.git('-C', original, 'add', '-A')
	tr.git('-C', original, '-c', 'user.email=t@e', '-c', 'user.name=t',
		'commit', '-q', '-m', 'base')

	def branch(*words):
		return subprocess.run([sys.executable, tr.AUTOOPT, 'tests-branch',
			*words], capture_output=True, text=True)

	done = branch(original, '--work', work)
	suite.check('an unpushed branch is refused', done.returncode != 0 and
		'not pushed' in done.stderr, done.stderr)
	tr.git('-C', original, 'push', '-q', 'origin', 'main')
	tr.git('-C', original, 'remote', 'set-head', 'origin', 'main')

	with open(os.path.join(original, 'a.py'), 'a') as out:
		out.write('y = 2\n')

	done = branch(original, '--work', work)
	suite.check('uncommitted changes are refused', done.returncode != 0 and
		'uncommitted' in done.stderr, done.stderr)
	tr.git('-C', original, 'checkout', '-q', 'a.py')
	done = branch(original, '--work', work, '--date', '2026-11-07')
	suite_dir = os.path.join(work, 'tests', 'suite')
	suite.check('a clean pushed original is cloned and branched, with its '
		'remote', done.returncode == 0 and tr.git('-C', suite_dir, 'remote',
		'get-url', 'origin').strip() == origin and tr.git('-C', suite_dir,
		'rev-parse', '--abbrev-ref', 'HEAD').strip() == 'auto-optimize/'
		'2026-11-07-unit-tests', done.stderr)

	for number in (1, 2):
		with open(os.path.join(suite_dir, 't{}.py'.format(number)), 'w') as out:
			out.write('pass\n')

		tr.git('-C', suite_dir, 'add', '-A')
		tr.git('-C', suite_dir, '-c', 'user.email=t@e', '-c', 'user.name=t',
			'commit', '-q', '-m', 'wip {}'.format(number))

	tr.git('-C', suite_dir, 'config', 'user.email', 't@e')
	tr.git('-C', suite_dir, 'config', 'user.name', 't')
	done = branch('--work', work, '--squash')
	suite.check('--squash needs a message', done.returncode != 0 and
		'--message' in done.stderr, done.stderr)
	done = branch('--work', work, '--squash', '-m', 'ADD tests')
	suite.check('--squash leaves one commit above origin/HEAD',
		done.returncode == 0 and tr.git('-C', suite_dir, 'rev-list',
		'--count', 'origin/HEAD..HEAD').strip() == '1', done.stderr)

	search = tr.make_search(os.path.join(base, 'template-search'), staged=False)

	# A search from before the scrub has no build_env: status says so, and
	# `config --set build_env.CC=` is the way to give it one.
	config_path = os.path.join(search, 'config.json')
	data = json.load(open(config_path))
	data.pop('build_env', None)
	json.dump(data, open(config_path, 'w'))
	status = subprocess.run([sys.executable, tr.AUTOOPT, 'status', search],
		capture_output=True, text=True).stdout
	suite.check('status warns when config.json has no build_env',
		'build_env: config.json has none' in status, status[-300:])
	done = subprocess.run([sys.executable, tr.AUTOOPT, 'config', search,
		'--set', 'build_env.CC=gcc', '--reason', 'test'], capture_output=True,
		text=True)
	status = subprocess.run([sys.executable, tr.AUTOOPT, 'status', search],
		capture_output=True, text=True).stdout
	suite.check('config --set build_env.CC= records it and the warning goes',
		done.returncode == 0 and json.load(open(config_path))['build_env']
		== {'CC': 'gcc'} and 'build_env: config.json' not in status,
		done.stderr + status[-200:])
	done = subprocess.run([sys.executable, tr.AUTOOPT, 'config', search,
		'--set', 'build_env.NOPE=1', '--reason', 'test'], capture_output=True,
		text=True)
	suite.check('and a name that is not a build variable is refused',
		done.returncode != 0 and 'not a build variable' in done.stderr,
		done.stderr)

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
