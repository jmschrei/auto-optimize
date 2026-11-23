"""Step 5: `ship.py apply` (plain mode), `ship.py branch` and `autoopt.py pr`
(repository mode), with and without a unit-test suite from step 1.

GitHub is stood in for by a local bare repository (git's `insteadOf`, in a
private global config) and `gh` by a script on PATH that records its calls,
so nothing here reaches the network.

	python3 tests/code/test_ship.py
"""

import io
import os
import sys
import json
import stat
import shutil
import tempfile
import argparse
import contextlib
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(HERE)), 'skills',
	'auto-optimize-code', 'scripts')
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import autoopt
import stages
import test_runner as tr

AUTOOPT = tr.AUTOOPT
RUNNER = tr.RUNNER
SHIP = os.path.join(SCRIPTS, 'ship.py')
STAGES = os.path.join(SCRIPTS, 'stages.py')
TABLES = os.path.join(SCRIPTS, 'opt_pr_tables.py')
BODY = os.path.join(SCRIPTS, 'opt_pr_body.py')
REMOTE_URL = 'https://github.com/example/pkg.git'
ISSUE_DATA = os.path.join(HERE, 'fixtures', 'issue', 'data')

IDENTITY = '[user]\n\tname = t\n\temail = t@t\n[init]\n\tdefaultBranch = main\n'
FAKE_GH = """#!/bin/sh
if [ -n "$GH_FAIL_PR" ] && [ "$1 $2" = "pr create" ]; then
	echo "fake gh: pr create failed" >&2; exit 1
fi
echo "$@" >> "$GH_LOG"
case "$1 $2" in
	"api user") echo example ;;
	"api repos/"*) echo true ;;
	"pr create")
		while [ $# -gt 0 ]; do
			[ "$1" = "--body-file" ] && cp "$2" "$GH_BODY"
			shift
		done
		echo "https://github.com/example/pkg/pull/7" ;;
	"issue create") echo "https://github.com/example/pkg/issues/3" ;;
	"issue comment") echo "https://github.com/example/pkg/issues/3#issuecomment-1" ;;
	*) echo "fake gh: unexpected $*" >&2; exit 1 ;;
esac
"""


def run(args, expect=(0,)):
	"""`(exit code, stdout + stderr)` of a script."""

	done = subprocess.run([sys.executable] + args, capture_output=True,
		text=True)

	if done.returncode not in expect:
		raise RuntimeError('{} exited {}:\n{}{}'.format(' '.join(args),
			done.returncode, done.stdout, done.stderr))

	return done.returncode, done.stdout + done.stderr


def run_end(search):
	buffer = io.StringIO()
	real = autoopt.run_dashboard
	autoopt.run_dashboard = lambda root, *flags: (0, '')

	try:
		with contextlib.redirect_stdout(buffer), \
				contextlib.redirect_stderr(buffer):
			code = autoopt.end(argparse.Namespace(root=search, reason=None,
				id=None, background=False, reopen=False))
	finally:
		autoopt.run_dashboard = real

	if code:
		raise RuntimeError(buffer.getvalue())


def notes(search, identifier, name, subject):
	"""What a kept iteration's subagent leaves: its notes, with `## Change`
	and `## Commit message`, and its README."""

	directory = autoopt.iteration_dir(search, identifier)

	with open(os.path.join(directory, 'notes.md'), 'w') as out:
		out.write(''.join('## {}\nx\n\n'.format(section) for section in
			autoopt.NOTES_REQUIRED) + '## Change\nname: {}\nIt does this '
			'one thing.\n\n## Commit message\n{}\n\nWhat changed, and what '
			'stays the same.\n'.format(name, subject))

	with open(os.path.join(directory, 'README.md'), 'w') as out:
		out.write('# {}\n'.format(autoopt.label(search, identifier)))


def write(path, text):
	os.makedirs(os.path.dirname(path), exist_ok=True)

	with open(path, 'w') as out:
		out.write(text)


def read(path):
	with open(path) as handle:
		return handle.read()


def set_after(search, **values):
	path = os.path.join(search, 'config.json')
	config = json.load(open(path))
	config.setdefault('after', {}).update(values)
	write(path, json.dumps(config, indent='\t'))


def ship_status(work):
	return {name: state for name, state, _, _ in stages.collect(work)}['ship']


def notes_row(work, stage):
	"""The status NOTES.md shows for `stage`, as last rendered."""

	for line in read(os.path.join(work, 'NOTES.md')).splitlines():
		if line.startswith('| {} |'.format(stage)):
			return line.split('|')[2].strip()

	return None


def next_move(work):
	return stages.next_action(work, stages.collect(work))[2]


def settle(search):
	"""Steps 0 and 2 on the record, as a real search has them, so the stages
	before step 5 read complete."""

	state = autoopt.load_state(search)
	state['setup'] = {'status': 'done'}
	autoopt.save_state(search, state)
	write(os.path.join(search, 'spec.md'), '# spec\n')


def plain(base, suite):
	"""A search made with `init --copy` of the user's `score.py`: A1 changes
	it and the benchmark the search wrote; `apply` writes only the first."""

	user = os.path.join(base, 'user')
	write(os.path.join(user, 'score.py'), 'QUALITY = 0.50\n')
	work = os.path.join(base, 'plain')
	search = os.path.join(work, 'autooptimize')
	tr.call([AUTOOPT, 'init', work, '--task', 'test', '--copy', os.path.join(
		user, 'score.py'), '--command', 'python3 bench.py --seed {seed} --out '
		'{out}', '--objective', 'score', '--direction', 'max', '--gates',
		json.dumps([{'name': 'cost', 'op': '<=', 'threshold': 1.0}]),
		'--observers', 'size', '--no-gpu', '--runs', '3', '--promote',
		'orchestrator'])
	config = autoopt.load_config(search)
	suite.equal('init records where the copied file came from',
		config.get('copied'), [{'source': os.path.join(user, 'score.py'), 'as':
		'score.py'}])

	tr.bench(autoopt.iteration_dir(search, 0), 0.50)
	tr.call([RUNNER, autoopt.iteration_dir(search, 0)])
	tr.call([AUTOOPT, 'new', search, '--id', '1', '--title', 'faster score',
		'--category', 'MISC'])
	one = autoopt.iteration_dir(search, 1)
	write(os.path.join(one, 'score.py'), 'QUALITY = 0.70\n')
	write(os.path.join(one, 'helper.py'), 'HELP = 1\n')
	tr.bench(one, 0.70)
	tr.call([RUNNER, one])
	tr.call([AUTOOPT, 'promote', search, '--id', '1'])
	notes(search, 1, 'Faster score', 'Score faster')

	code, out = run([SHIP, 'apply', work], expect=(1,))
	suite.check('apply refuses before the search has ended', 'has not ended'
		in out, out)
	run_end(search)
	code, out = run([SHIP, 'plan', work])
	suite.check('plan offers a plain search apply or not now, and asks nothing '
		'about branch names', 'Apply the change to the original file(s)' in
		out and 'Not now' in out and 'Start the branch names' not in out, out)

	code, out = run([SHIP, 'apply', work])
	suite.check('the preview names the copied file and the file the change '
		'adds beside it, and leaves out the benchmark the search wrote',
		os.path.join(user, 'score.py') in out and os.path.join(user,
		'helper.py') in out and 'not applied: bench.py' in out and
		'preview only' in out, out)
	suite.equal('the preview writes nothing', read(os.path.join(user,
		'score.py')), 'QUALITY = 0.50\n')

	code, out = run([SHIP, 'branch', work], expect=(1,))
	suite.check('branch refuses a plain search, naming apply', 'ship.py '
		'apply' in out, out)

	code, out = run([SHIP, 'apply', work, '--yes'], expect=(1,))
	suite.check('apply --yes refuses until the user chose to apply',
		'after.ship is ask' in out and read(os.path.join(user, 'score.py'))
		== 'QUALITY = 0.50\n', out)
	set_after(search, ship='apply')
	suite.equal('the ship stage runs once the user chose to apply',
		ship_status(work), 'running')
	run([SHIP, 'apply', work, '--yes'])
	suite.equal('--yes writes the measured file', read(os.path.join(user,
		'score.py')), 'QUALITY = 0.70\n')
	suite.equal('and adds the new one', read(os.path.join(user, 'helper.py')),
		'HELP = 1\n')
	suite.check('the benchmark is not copied to the user\'s folder',
		not os.path.exists(os.path.join(user, 'bench.py')), os.listdir(user))
	record = json.load(open(os.path.join(work, 'ship', 'applied.json')))
	suite.equal('applied.json lists what was written', sorted((row['path'],
		row['action']) for row in record['files']), [('helper.py', 'add'),
		('score.py', 'change')])
	suite.equal('and completes the ship stage', ship_status(work), 'complete')
	suite.equal('NOTES.md says so at once', notes_row(work, 'ship'),
		'complete')
	suite.check('end --reopen is held once the change is written',
		os.path.exists(os.path.join(search, 'reopen-lock.json')))

	code, out = run([SHIP, 'apply', work])
	suite.check('a second apply finds it applied already', 'applied already'
		in out, out)

	applied = os.path.join(work, 'ship', 'applied.json')
	first = read(applied)
	code, out = run([SHIP, 'apply', work, '--yes'])
	suite.check('a second apply --yes of the same winner keeps the first '
		'record, which says what was written', code == 0 and read(applied) ==
		first and 'applied already' in out, out)

	# The user applied it by hand: --yes records that, and the stage is done.
	os.remove(applied)
	code, out = run([SHIP, 'apply', work, '--yes'])
	record = json.load(open(applied))
	suite.check('--yes on files that hold the change already records them as '
		'applied, writing nothing', record['files'] == [] and sorted(row[
		'destination'] for row in record['already']) == [os.path.join(user,
		'helper.py'), os.path.join(user, 'score.py')] and ship_status(work) ==
		'complete', (out, record))
	suite.check('and its patch is the change those files hold', record['patch']
		and 'QUALITY = 0.70' in read(record['patch']) and 'helper.py' in read(
		record['patch']), record)

	import ship
	digest_before = ship.inputs_digest(work, [], {})

	# A winner promoted after the end replaces the one applied.
	tr.call([AUTOOPT, 'new', search, '--id', '2', '--parent', '1', '--title',
		'faster again', '--category', 'MISC', '--after-end'])
	two = autoopt.iteration_dir(search, 2)
	write(os.path.join(two, 'score.py'), 'QUALITY = 0.80\n')
	tr.bench(two, 0.80)
	tr.call([RUNNER, two])
	notes(search, 2, 'Faster again', 'Score faster again')
	tr.call([AUTOOPT, 'promote', search, '--id', '2'])
	suite.equal('a winner promoted after the apply runs the ship stage again',
		ship_status(work), 'running')
	suite.check('a winner promoted after a rehearsal changes the digest, so '
		'post refuses the stale batch', ship.inputs_digest(work, [], {}) !=
		digest_before)
	code, out = run([SHIP, 'apply', work, '--yes'], expect=(0, 1))
	suite.check('and apply writes it over what the first apply wrote',
		code == 0 and read(os.path.join(user, 'score.py')) == 'QUALITY = '
		'0.80\n' and json.load(open(applied))['id'] == 2 and ship_status(work)
		== 'complete', out)
	record = json.load(open(applied))
	suite.check('the record and patch cover every file the original now holds '
		'the change in: helper.py, which A1 added and A2 kept, as well as the '
		'score.py A2 wrote', [row['path'] for row in record['already']] == [
		'helper.py'] and 'helper.py' in read(record['patch']) and 'QUALITY = '
		'0.80' in read(record['patch']), record)

	write(os.path.join(user, 'score.py'), 'QUALITY = 0.40\n')
	os.remove(os.path.join(user, 'helper.py'))
	code, out = run([SHIP, 'apply', work], expect=(1,))
	suite.check('a file edited since the copy is refused, and nothing is '
		'written', 'changed since the search copied it' in out and read(
		os.path.join(user, 'score.py')) == 'QUALITY = 0.40\n', out)

	# The user merges the change by hand: --by-hand records it as applied.
	write(os.path.join(user, 'helper.py'), tr.git('-C', os.path.join(search,
		'repo.git'), 'show', 'autoopt/2:helper.py'))
	write(os.path.join(user, 'score.py'), 'QUALITY = 0.80  # by hand\n')
	code, out = run([SHIP, 'apply', work], expect=(1,))
	suite.check('a file merged by hand is still refused without --by-hand',
		'changed since the search copied it' in out, out)
	code, out = run([SHIP, 'apply', work, '--by-hand'])
	suite.check('--by-hand previews it as applied by hand, writing nothing',
		'applied by hand, on the user\'s word' in out and read(os.path.join(
		user, 'score.py')) == 'QUALITY = 0.80  # by hand\n', out)
	run([SHIP, 'apply', work, '--by-hand', '--yes'])
	record = json.load(open(applied))
	suite.check('--by-hand --yes records it and leaves the file as it is',
		[row['path'] for row in record['by_hand']] == ['score.py'] and read(
		os.path.join(user, 'score.py')) == 'QUALITY = 0.80  # by hand\n',
		record)
	write(os.path.join(user, 'score.py'), 'QUALITY = 0.40\n')

	config = json.load(open(os.path.join(search, 'config.json')))
	del config['copied']
	write(os.path.join(search, 'config.json'), json.dumps(config,
		indent='\t'))
	code, out = run([SHIP, 'apply', work], expect=(1,))
	suite.check('a search with no record of its copies asks for --to',
		'--to DIR' in out, out)
	other = os.path.join(base, 'other')
	write(os.path.join(other, 'score.py'), 'QUALITY = 0.50\n')
	write(os.path.join(other, 'bench.py'), read(os.path.join(
		autoopt.iteration_dir(search, 0), 'bench.py')))
	run([SHIP, 'apply', work, '--to', other, '--yes'])
	suite.equal('with --to it applies there, the benchmark included',
		(read(os.path.join(other, 'score.py')), os.path.exists(os.path.join(
		other, 'helper.py'))), ('QUALITY = 0.80\n', True))

	# Step 6: `stages.py next --run` starts the archive itself.
	settle(search)
	suite.check('with step 5 done, the next move is the archive, to run',
		stages.next_action(work, stages.collect(work))[:2] == ('finish',
		'run'), next_move(work))
	code, out = run([STAGES, 'next', work, '--run'], expect=(0, 1))
	report = os.path.join(work, 'finish', 'final-report.md')
	where = (read(report) if os.path.exists(report) else '').split(
		'## Where things are')[-1]
	suite.check('next --run runs it: the archive and the report\'s skeleton, '
		'with what apply wrote', code == 0 and os.path.exists(os.path.join(work,
		'finish', 'archive', 'idea.patch')) and 'A2 applied to the original '
		'files' in where and '`{}`'.format(os.path.join(other, 'score.py')) in
		where, (out, where))
	suite.equal('and the finish runs until the report\'s <<FILL marks are '
		'gone', notes_row(work, 'finish'), 'running')


def repository(base, suite, with_suite=False):
	"""A repository search ended on A2 (A1 then A2, each changing
	pkg/core.py), optionally made on a step-1 suite. Returns (work, search,
	original)."""

	name = 'suite' if with_suite else 'repo'
	original = os.path.join(base, name + '-original')
	tr.make_original(original)
	remote = os.path.join(base, name + '-remote.git')
	tr.git('clone', '-q', '--bare', original, remote)
	# This repository's stand-in for GitHub replaces the last one's.
	write(os.environ['GIT_CONFIG_GLOBAL'], IDENTITY + '[url "{}"]\n'
		'\tinsteadOf = {}\n'.format(remote, REMOTE_URL))
	tr.git('-C', original, 'fetch', '-q', 'origin')
	tr.git('-C', original, 'branch', '-q', '--set-upstream-to=origin/main',
		'main')
	work = os.path.join(base, name)
	search = os.path.join(work, 'autooptimize')
	source = original

	if with_suite:
		tr.call([AUTOOPT, 'tests-branch', original, '--work', work, '--date',
			'2026-01-02'])
		clone = autoopt.work_path(work, 'tests-suite')
		write(os.path.join(clone, 'tests', 'test_new.py'), 'def test_new():\n'
			'\tassert True\n')
		tr.git('-C', clone, 'add', '-A')
		tr.git('-C', clone, 'commit', '-q', '-m', 'wip')
		tr.call([AUTOOPT, 'tests-branch', '--work', work, '--squash', '-m',
			'Add unit tests for pkg'])
		source = clone

	harness = os.path.join(base, name + '-bench.py')
	write(harness, tr.HARNESS)
	tr.call([AUTOOPT, 'init', work, '--task', 'test', '--repo', source,
		'--copy', harness, '--command', 'python3 {}/harness/{} --src {{src}} '
		'--seed {{seed}} --out {{out}}'.format(search, os.path.basename(
		harness)), '--objective', 'score', '--direction', 'max', '--gates',
		json.dumps([{'name': 'cost', 'op': '<=', 'threshold': 1.0}]),
		'--observers', 'size', '--no-gpu', '--runs', '3', '--promote',
		'orchestrator'])
	tr.call([RUNNER, autoopt.iteration_dir(search, 0)])
	tr.repo_add(search, 1, 0, 0.60)
	tr.call([AUTOOPT, 'promote', search, '--id', '1'])
	tr.repo_add(search, 2, 1, 0.70)
	tr.call([AUTOOPT, 'promote', search, '--id', '2'])
	notes(search, 1, 'Raise the quality', 'Raise the quality')
	notes(search, 2, 'Raise it again', 'Raise the quality again')
	run_end(search)
	settle(search)

	return work, search, original


def fill_prose(directory):
	"""prose.md from the template, every <<FILL answered with a sentence."""

	_, template = run([BODY, directory, '--template'])
	lines = []

	for line in template.splitlines():
		if line.startswith('<<FILL'):
			line = 'none' if lines and lines[-1] == '=== vendored' else \
				'One plain sentence about it.'
		lines.append(line)

	write(os.path.join(directory, 'prose.md'), '\n'.join(lines) + '\n')


def branches(suite, base):
	work, search, original = repository(base, suite)
	code, out = run([SHIP, 'apply', work], expect=(1,))
	suite.check('apply refuses a repository search, naming branch',
		'ship.py branch' in out, out)

	code, out = run([SHIP, 'plan', work])
	suite.check('plan asks a repository search whether the branch names start '
		'with auto-optimize/, showing both forms', 'Start the branch names '
		'with auto-optimize/?' in out and '- No: `<from-the-pr-title>`' in out
		and '- Yes: `auto-optimize/<from-the-pr-title>`' in out and
		'after.branch_prefix=<no|yes>' in out, out)
	suite.check('on GitHub it recommends the documents, and offers the '
		'optimization PR alone with no suite and no bugs', '1. Open GitHub '
		'documents (Recommended)' in out and '- The optimization PR' in out and
		'The unit-test PR' not in out and 'The bug report' not in out, out)
	done = subprocess.run([sys.executable, SHIP, 'plan', work, '--json'],
		capture_output=True, text=True)
	offered = json.loads(done.stdout)
	suite.equal('plan --json gives the routes and the documents',
		([route[0] for route in offered['routes']], [document[0] for document
		in offered['documents']]), (['github', 'branch', 'none'],
		['optimization']))
	code, out = run([SHIP, 'branch', work, '--name', 'faster', '--yes'],
		expect=(1,))
	suite.check('branch --yes refuses until the user chose the branch route',
		'after.ship is ask' in out and 'after.ship=branch' in out, out)
	set_after(search, ship='branch')
	code, out = run([SHIP, 'branch', work], expect=(1,))
	suite.check('branch refuses to name a branch until the user has answered '
		'the prefix question', 'after.branch_prefix' in out, out)
	code, out = run([SHIP, 'branch', work, '--name', 'faster'])
	suite.check('a name the user gave needs no prefix answer', 'preview only'
		in out, out)
	set_after(search, branch_prefix='no')
	code, out = run([SHIP, 'branch', work, '--name', 'faster'])
	suite.check('the branch preview lists one commit per kept change, by '
		'their commit messages', 'Raise the quality\n' in out and
		'Raise the quality again' in out and 'preview only' in out, out)
	suite.check('and makes nothing in the original', subprocess.run(['git',
		'-C', original, 'rev-parse', '--verify', '-q', 'refs/heads/faster'],
		capture_output=True).returncode != 0)

	run([SHIP, 'branch', work, '--name', 'faster', '--yes'])
	record = json.load(open(os.path.join(work, 'ship', 'branch.json')))
	suite.check('--yes records the branch, holds end --reopen and completes '
		'the stage, in NOTES.md too', (record['id'], record['branch'],
		record['repo']) == (2, 'faster', original) and 'branch' in [hold['by']
		for hold in autoopt.reopen_holds(search)] and ship_status(work) ==
		'complete' and notes_row(work, 'ship') == 'complete', record)
	subjects = tr.git('-C', original, 'log', '--format=%s',
		'main..faster').split('\n')[:-1]
	suite.equal('--yes makes the branch, oldest change first',
		list(reversed(subjects)), ['Raise the quality', 'Raise the quality '
		'again'])
	bare = os.path.join(search, 'repo.git')
	suite.equal('and its last tree is the measured code', tr.git('-C',
		original, 'rev-parse', 'faster^{tree}'), tr.git('-C', bare,
		'rev-parse', 'autoopt/2^{tree}'))
	suite.equal('the original stays on its branch, unchanged',
		tr.git('-C', original, 'rev-parse', '--abbrev-ref', 'HEAD').strip(),
		'main')
	code, out = run([SHIP, 'branch', work, '--name', 'faster', '--yes'],
		expect=(1,))
	suite.check('a branch that exists is refused', 'already has a branch' in
		out, out)
	set_after(search, branch_prefix='yes')
	run([SHIP, 'branch', work, '--yes'])
	named = 'auto-optimize/optimized-{}'.format(autoopt.now()[:10])
	suite.check('with the prefix chosen, the default branch starts with it',
		subprocess.run(['git', '-C', original, 'rev-parse', '--verify', '-q',
		'refs/heads/' + named], capture_output=True).returncode == 0, tr.git(
		'-C', original, 'branch', '--list'))
	set_after(search, branch_prefix='no')

	config = {'after': {'branch_prefix': 'yes'}}
	plain_config = {'after': {'branch_prefix': 'no'}}
	suite.equal('the PR and local branch names, with and without the prefix',
		(autoopt.ship_branch(config, 'faster'), autoopt.ship_branch(
		plain_config, 'faster'), autoopt.ship_branch({}, 'faster')),
		('auto-optimize/faster', 'faster', 'faster'))
	suite.equal('step 1\'s branch is pushed under its own name with the prefix, '
		'and as unit-tests-<date> without it', (autoopt.tests_branch_remote(
		config, 'auto-optimize/2026-01-02-unit-tests'),
		autoopt.tests_branch_remote(plain_config,
		'auto-optimize/2026-01-02-unit-tests')), (
		'auto-optimize/2026-01-02-unit-tests', 'unit-tests-2026-01-02'))

	return work, search


def pull_request(suite, base, work, search):
	directory = autoopt.work_path(work, 'ship', 'pr-optimization')
	code, out = run([SHIP, 'brief', work], expect=(1,))
	suite.check('brief refuses until the user chose GitHub documents',
		'after.ship is \'branch\'' in out, out)
	# Step 1 recorded a bug, so the bug report is offered.
	os.makedirs(os.path.join(work, 'tests', 'work', 'bugs', 'wrong-score'))
	set_after(search, ship='github', ship_documents=['tests', 'optimization'])
	code, out = run([SHIP, 'render', work], expect=(1,))
	suite.check('a document this search does not offer is refused, and the '
		'stage asks again', 'the unit-test PR, which this search does not '
		'offer' in out and ship_status(work) == 'not started', out)
	set_after(search, ship='github', ship_documents=['bugs', 'optimization'])
	code, out = run([SHIP, 'brief', work])
	suite.check('the brief is the document writer\'s, and keeps what an '
		'earlier one wrote', 'You are the document writer' in out and
		'never regenerated over' in out and 'only when prose.md does not '
		'exist yet' in out and 'The bug report' in out, out)
	bugs = autoopt.work_path(work, 'ship', 'issue-bugs')
	shutil.copytree(ISSUE_DATA, bugs)
	run([os.path.join(SCRIPTS, 'issue_body.py'), bugs, '--title', 'Bugs '
		'found while testing chromap 949043c', '--out', os.path.join(bugs,
		'body.md')])
	suite.equal('with GitHub chosen and nothing posted, the ship stage runs',
		ship_status(work), 'running')
	code, out = run([SHIP, 'status', work])
	suite.check('status shows each document where it stands',
		'the bug report       rendered: "Bugs found while testing chromap '
		'949043c"' in out and 'the optimization PR  not started' in out and
		'rehearsal: none yet' in out, out)
	suite.check('and next spawns the document writer', ' brief ' in
		next_move(work), next_move(work))
	code, out = run([TABLES, work])
	suite.check('the tables write one message per kept change from its '
		'notes', read(os.path.join(directory, 'messages', '1.txt')).startswith(
		'Raise the quality\n\n'), out)
	fill_prose(directory)
	code, out = run([BODY, directory, '--title', 'Raise the quality of pkg',
		'--out', os.path.join(directory, 'body.md')])
	body = read(os.path.join(directory, 'body.md'))
	suite.check('the body renders with no word of the tool, the lab or the '
		'search', all(word not in body.lower() for word in ('auto-optimize',
		'programmable genomics', 'baseline', 'winner', 'iteration')), body)
	suite.check('the chosen bug report is named by its stand-in until it is '
		'filed', '#<bug issue>' in body and '#<tests PR>' not in body, body)
	suite.check('and says nothing about how it was written', 'Claude' not in
		body and 'Generated with' not in body, body[-200:])
	prose = os.path.join(directory, 'prose.md')
	kept = read(prose)
	write(prose, kept.replace('=== behaviour\n', '=== behaviour\nGenerated with '
		'[Claude Code](https://claude.com/claude-code). ', 1))
	code, out = run([BODY, directory, '--out', os.path.join(directory,
		'body.md')], expect=(1,))
	suite.check('a notice of how it was written, put in the prose, is refused',
		'notice' in out and read(os.path.join(directory, 'body.md')) == body,
		out)
	write(prose, kept)

	code, out = run([AUTOOPT, 'pr', work])
	suite.check('the PR preview pushes nothing and names the push target',
		'preview only' in out and 'example/pkg' in out and
		'raise-the-quality-of-pkg' in out, out)
	remote = os.path.join(base, 'repo-remote.git')
	suite.check('the remote has no new branch after a preview', 'raise' not in
		tr.git('-C', remote, 'branch', '--list'))

	code, out = run([SHIP, 'render', work, '--only', 'bugs'])
	suite.check('render --only renders that document alone', 'the bug report: '
		'rendered' in out and 'optimization' not in out, out)
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		'0' * 16], expect=(1,))
	suite.check('post refuses before a rehearsal', 'no rehearsal' in out, out)
	code, out = run([SHIP, 'post', work, 'all', '--digest', '0' * 16],
		expect=(1,))
	suite.check('and without --approved', 'posting needs --approved' in out,
		out)

	title = os.path.join(directory, 'body.title')
	os.rename(title, title + '.aside')
	code, out = run([SHIP, 'rehearse', work], expect=(1,))
	digest = json.load(open(os.path.join(work, 'ship', 'rehearsal.json')))[
		'digest']
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest], expect=(1,))
	suite.check('a rehearsal with problems posts nothing', 'had problems' in
		out, out)
	os.rename(title + '.aside', title)

	code, out = run([SHIP, 'rehearse', work, '--branch',
		'optimization=another-name'])
	digest = json.load(open(os.path.join(work, 'ship', 'rehearsal.json')))[
		'digest']
	suite.check('rehearse --branch previews the PR on that branch, and names '
		'the flag in the post command', 'another-name' in read(os.path.join(
		work, 'ship', 'rehearsal', 'optimization.txt')) and '--branch '
		'optimization=another-name' in out, out)
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest], expect=(1,))
	suite.check('post refuses other --branch flags than the rehearsal\'s',
		'is not what the rehearsal the user reviewed pushed' in out, out)

	code, out = run([SHIP, 'rehearse', work])
	digest = json.load(open(os.path.join(work, 'ship', 'rehearsal.json')))[
		'digest']
	set_after(search, branch_prefix='yes')
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest], expect=(1,))
	suite.check('a branch-name answer changed after the review is refused like '
		'an input', 'inputs changed since the rehearsal' in out, out)
	set_after(search, branch_prefix='no')
	code, out = run([SHIP, 'rehearse', work])
	digest = json.load(open(os.path.join(work, 'ship', 'rehearsal.json')))[
		'digest']
	suite.check('the rehearsal previews both documents and gives the digest '
		'to post with', '"Bugs found while testing chromap 949043c"' in out
		and '"Raise the quality of pkg"' in out and digest in out, out)
	suite.equal('and posts nothing', read(os.environ['GH_LOG']).count(
		'create'), 0)
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		'f' * 16], expect=(1,))
	suite.check('post refuses a digest the user did not review', 'is not the '
		'last rehearsal' in out, out)
	prose = os.path.join(directory, 'prose.md')
	write(prose, read(prose) + '\n')
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest], expect=(1,))
	suite.check('and inputs changed after the review', 'changed since the '
		'rehearsal' in out, out)
	write(prose, read(prose).rstrip('\n') + '\n')
	code, out = run([SHIP, 'rehearse', work])
	digest = json.load(open(os.path.join(work, 'ship', 'rehearsal.json')))[
		'digest']
	suite.check('a rehearsal that holds: next is the user\'s review, then the '
		'post', 'show the user' in next_move(work) and '--digest ' + digest in
		next_move(work), next_move(work))

	# gh fails to open the PR after the issue is filed and the branch pushed.
	os.environ['GH_FAIL_PR'] = '1'
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest, '--other-owner'], expect=(1,))
	del os.environ['GH_FAIL_PR']
	suite.check('a post that stops part way leaves the stage running, and '
		'next says to run the same post again', 'was not posted' in out and
		ship_status(work) == 'running' and 'the same command again' in
		next_move(work) and notes_row(work, 'ship') == 'running',
		(out, next_move(work)))
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest, '--other-owner'], expect=(0, 1))
	suite.check('and the same command posts the rest, opening the PR on the '
		'branch already pushed', code == 0 and 'the bug report: posted already'
		in out, out)
	calls = read(os.environ['GH_LOG'])
	suite.check('post files the bug report before it opens the PR', 0 <=
		calls.find('issue create') < calls.find('pr create'), calls)
	posted = read(os.path.join(directory, 'body.md'))
	suite.check('and the PR names the issue by its number', '#3' in posted and
		'#<bug issue>' not in posted, posted[-400:])
	record = json.load(open(os.path.join(directory, 'pr.json')))
	suite.equal('--post pushes the branch and records the PR', (record[
		'number'], record['branch']), (7, 'raise-the-quality-of-pkg'))
	suite.equal('the pushed branch is the commits made locally',
		tr.git('-C', remote, 'rev-parse', 'raise-the-quality-of-pkg').strip(),
		record['commits'][-1])
	calls = read(os.environ['GH_LOG'])
	suite.check('gh opened the PR on the original with the rendered title',
		'pr create --repo example/pkg --base main --head '
		'raise-the-quality-of-pkg --title Raise the quality of pkg' in calls,
		calls)
	suite.check('and the posted body has the commits\' hashes linked',
		'https://github.com/example/pkg/commit/' + record['commits'][0] in
		read(os.environ['GH_BODY']), read(os.environ['GH_BODY'])[-300:])
	suite.equal('every chosen document posted completes the ship stage',
		ship_status(work), 'complete')
	suite.check('holds end --reopen, and NOTES.md says so', 'step 5' in [hold[
		'by'] for hold in autoopt.reopen_holds(search)] and notes_row(work,
		'ship') == 'complete', autoopt.reopen_holds(search))
	code, out = run([SHIP, 'post', work, 'all', '--approved', '--digest',
		digest])
	suite.check('posting again posts nothing new', 'posted already' in out and
		read(os.environ['GH_LOG']).count('create') == 2, out)
	code, out = run([AUTOOPT, 'pr', work, '--post'])
	suite.check('a second post opens nothing', 'already opened' in out and
		read(os.environ['GH_LOG']).count('pr create') == 1, out)


def separate(suite, base):
	work, search, original = repository(base, suite, with_suite=True)
	config = autoopt.load_config(search)
	found = autoopt.original_of(work, config)
	suite.equal('step 1 records the original\'s branch and commit',
		(found['branch'], found['commit']), ('main', tr.git('-C', original,
		'rev-parse', 'HEAD').strip()))

	messages = {1: 'Raise the quality\n', 2: 'Raise it again\n'}
	made = autoopt.pr_commits(search, config, 2, messages)
	bare = os.path.join(search, 'repo.git')
	files = tr.git('-C', bare, 'diff', '--name-only', made['base'],
		made['tip']).split()
	suite.equal('with a suite, the commits sit on the original\'s commit and '
		'carry none of the suite\'s files', (made['base'], files),
		(found['commit'], ['pkg/core.py']))
	suite.equal('the suite\'s files are the ones it added', made['owned'],
		['tests/test_new.py'])

	# A change to a file the suite adds cannot go into a PR of its own.
	directory = tr.call([AUTOOPT, 'new', search, '--id', '3', '--parent', '2',
		'--title', 'touch the tests', '--category', 'MISC', '--after-end']
		).strip().splitlines()[-1]
	write(os.path.join(directory, 'src', 'tests', 'test_new.py'),
		'def test_new():\n\tassert 1\n')
	tr.call([RUNNER, directory])

	try:
		with contextlib.redirect_stderr(io.StringIO()) as err:
			autoopt.pr_commits(search, config, 3, {1: messages[1], 2:
				messages[2], 3: 'Touch\n'})
		refused = ''
	except SystemExit:
		refused = err.getvalue()

	suite.check('a kept change to a suite file is refused', 'which the '
		'unit-test suite adds' in refused, refused)


def main():
	suite = tr.Suite()
	base = tempfile.mkdtemp()
	saved = {key: os.environ.get(key) for key in ('GIT_CONFIG_GLOBAL', 'PATH',
		'GH_LOG', 'GH_BODY')}

	# A private global git config (an identity, and the stand-in GitHub) and
	# a fake gh first on PATH.
	config = os.path.join(base, 'gitconfig')
	write(config, IDENTITY)
	os.environ['GIT_CONFIG_GLOBAL'] = config
	tools = os.path.join(base, 'bin')
	write(os.path.join(tools, 'gh'), FAKE_GH)
	os.chmod(os.path.join(tools, 'gh'), stat.S_IRWXU)
	os.environ['PATH'] = tools + os.pathsep + os.environ.get('PATH', '')
	os.environ['GH_LOG'] = os.path.join(base, 'gh.log')
	os.environ['GH_BODY'] = os.path.join(base, 'gh-body.md')
	write(os.environ['GH_LOG'], '')

	try:
		plain(base, suite)
		work, search = branches(suite, base)
		pull_request(suite, base, work, search)
		separate(suite, base)
	finally:
		for key, value in saved.items():
			if value is None:
				os.environ.pop(key, None)
			else:
				os.environ[key] = value

		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(
		suite.failed)))
	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
