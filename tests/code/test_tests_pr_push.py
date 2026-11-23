#!/usr/bin/env python3
"""Check tests_pr_push.py against local bare repositories and a stand-in `gh`.

	python3 test_tests_pr_push.py     # a few seconds

Step 1's branch is made the way step 1 makes it, with `autoopt.py
tests-branch`, from a repository whose origin is a GitHub URL. A git config
of its own (`GIT_CONFIG_GLOBAL`) sends that URL, and a fork's, to bare
repositories in a temporary directory, so every push stays on this machine.
The stand-in `gh` logs each call, answers `api user` and prints a PR URL for
`pr create`. No network; needs git.
"""

import os
import sys
import json
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import test_runner

AUTOOPT = os.path.join(SCRIPTS, 'autoopt.py')
PUSH = os.path.join(SCRIPTS, 'tests_pr_push.py')
BODY = os.path.join(SCRIPTS, 'tests_pr_body.py')
TABLES = os.path.join(SCRIPTS, 'tests_pr_tables.py')
DATA = os.path.join(HERE, 'fixtures', 'tests_pr', 'data')
BRANCH = 'auto-optimize/2026-11-01-unit-tests'
# The name it is pushed under when the user did not choose the auto-optimize/
# prefix at step 5 (`after.branch_prefix`).
PLAIN = 'unit-tests-2026-11-01'
TITLE = 'Add unit tests for pkg'

GH = '''#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$1 $2" in
	"api user") echo "${GH_LOGIN:-example}" ;;
	"pr create") echo "https://github.com/example/pkg/pull/${GH_NUMBER:-12}" ;;
esac
'''

GITCONFIG = '''[user]
	name = t
	email = t@t
[init]
	defaultBranch = main
[url "{upstream}"]
	insteadOf = https://github.com/example/pkg.git
[url "{fork}"]
	insteadOf = https://github.com/me/pkg.git
'''


def sh(*command, cwd=None):
	return subprocess.run(command, capture_output=True, text=True, cwd=cwd)


def git(repo, *args):
	done = sh('git', '-C', repo, *args)
	assert done.returncode == 0, done.stderr
	return done.stdout.strip()


def read(path):
	with open(path) as handle:
		return handle.read()


def write(path, text):
	os.makedirs(os.path.dirname(path), exist_ok=True)

	with open(path, 'w') as handle:
		handle.write(text)


def write_json(path, data):
	write(path, json.dumps(data))


def setup(root):
	"""A search directory whose step 1 made the tests branch, the ship
	folder's tables and body rendered: (work, suite clone, ship folder, the
	original's commit)."""

	upstream = os.path.join(root, 'upstream.git')
	fork = os.path.join(root, 'fork.git')
	sh('git', 'init', '-q', '--bare', upstream)
	sh('git', 'init', '-q', '--bare', fork)
	write(os.path.join(root, 'gitconfig'), GITCONFIG.format(upstream=upstream,
		fork=fork))
	bin_dir = os.path.join(root, 'bin')
	write(os.path.join(bin_dir, 'gh'), GH)
	os.chmod(os.path.join(bin_dir, 'gh'), 0o755)
	# This file runs as its own process, so its environment is the tests':
	# every git call below and in the scripts reads the config above, and
	# every gh call is the stand-in.
	os.environ.update(GIT_CONFIG_GLOBAL=os.path.join(root, 'gitconfig'),
		GIT_CONFIG_NOSYSTEM='1', GH_LOG=os.path.join(root, 'gh.log'),
		PATH=bin_dir + os.pathsep + os.environ['PATH'])

	original = os.path.join(root, 'original')
	test_runner.make_original(original)
	git(original, 'push', '-q', 'origin', 'main')

	work = os.path.join(root, 'work')
	done = sh(sys.executable, AUTOOPT, 'tests-branch', original, '--work', work,
		'--date', '2026-11-01')
	assert done.returncode == 0, done.stderr
	suite_dir = os.path.join(work, 'tests', 'suite')
	# `tests-branch` copies the original's URL as `git remote get-url` prints
	# it, after the rewrite above; without one, that is the GitHub URL.
	git(suite_dir, 'remote', 'set-url', 'origin',
		'https://github.com/example/pkg.git')
	write(os.path.join(suite_dir, 'tests', 'test_new.py'), 'def test_new():\n'
		'\tpass\n')
	git(suite_dir, 'add', '-A')
	git(suite_dir, 'commit', '-q', '-m', 'wip')
	done = sh(sys.executable, AUTOOPT, 'tests-branch', '--work', work,
		'--squash', '-m', 'Add unit tests for pkg')
	assert done.returncode == 0, done.stderr
	base = json.loads(read(os.path.join(work, 'tests', 'branch.json')))['commit']

	ship = os.path.join(work, 'ship', 'pr-tests')
	shutil.copytree(DATA, ship)
	write_json(os.path.join(work, 'ship', 'issue-bugs', 'filed.json'),
		{'number': 7})
	done = sh(sys.executable, TABLES, 'files', '--repo', suite_dir, '--base',
		base, '--head', BRANCH, '--out', ship)
	assert done.returncode == 0, done.stderr
	render(ship, TITLE)

	return work, suite_dir, ship, base


def render(ship, title):
	done = sh(sys.executable, BODY, ship, '--title', title, '--out',
		os.path.join(ship, 'body.md'))
	assert done.returncode == 0, done.stderr


def main():
	suite = test_runner.Suite()
	root = tempfile.mkdtemp(prefix='tests-pr-push-')

	try:
		checks(suite, root)
	finally:
		shutil.rmtree(root, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for line in suite.failed:
		print('  - ' + line)

	return 1 if suite.failed else 0


def checks(suite, root):
	work, suite_dir, ship, base = setup(root)
	log = os.environ['GH_LOG']
	upstream = os.path.join(root, 'upstream.git')
	fork = os.path.join(root, 'fork.git')
	config = os.path.join(work, 'autooptimize', 'config.json')
	body = os.path.join(ship, 'body.md')

	def push(*extra, **env):
		done = subprocess.run([sys.executable, PUSH, ship, '--repo',
			'example/pkg'] + list(extra), capture_output=True, text=True,
			env=dict(os.environ, **env))
		return done.returncode, done.stdout, done.stderr

	def branches(remote):
		return sh('git', '-C', remote, 'branch', '--list').stdout.split()

	def calls():
		return read(log) if os.path.exists(log) else ''

	def reads_only():
		# A preview may read who is logged in (post needs), nothing else.
		return all(line == 'api user -q .login' for line in
			calls().splitlines())

	print('\nthe preview')
	listed = sorted(os.listdir(ship))
	code, out, err = push()
	suite.check('the preview needs only --repo: the clone, branch and base '
		'come from branch.json and the target branch from step 1\'s record',
		code == 0 and 'preview only' in out and 'target branch main, from {}'
		.format(os.path.join(work, 'tests', 'branch.json')) in out, err + out)
	suite.check('it pushes to origin, the branch named without the prefix '
		'the user did not choose, as the head', 'branch:     {} -> {} on '
		'remote origin (https://github.com/example/pkg.git, from the default), '
		'head {}'.format(BRANCH, PLAIN, PLAIN) in out, out)
	suite.check('and names the title, the sections and the commit\'s files',
		'title:      ' + TITLE in out and 'section:    Running it' in out
		and 'commits:    1 over {}'.format(base) in out
		and 'A\ttests/test_new.py' in out, out)
	suite.check('and says what the post needs', 'post needs:' in out
		and '--other-owner: no, example is your account' in out
		and 'push: remote origin' in out, out)
	suite.check('the preview calls no gh but `api user`, pushes nothing and '
		'writes nothing', reads_only() and branches(upstream) == ['main']
		and sorted(os.listdir(ship)) == listed, (calls(), os.listdir(ship)))
	suite.equal('the body holds the bug report\'s number from filed.json',
		('#7' in read(body), '#<bug issue>' in read(body)), (True, False))

	recorded = os.path.join(ship, 'branch.json')
	os.rename(recorded, recorded + '.aside')
	code, out, err = push()
	suite.check('without branch.json and the flags it refuses, naming them',
		code == 1 and 'no --suite' in err and 'no --base' in err
		and 'no --branch' in err, err)
	code, out, err = push('--suite', suite_dir, '--branch', BRANCH, '--base',
		base)
	suite.check('and the flags stand in for it', code == 0, err)
	os.rename(recorded + '.aside', recorded)

	step1 = os.path.join(work, 'tests', 'branch.json')
	os.rename(step1, step1 + '.aside')
	code, out, err = push()
	suite.check('without step 1\'s record or a config.json, no target branch '
		'is refused', code == 1 and 'no --target-branch' in err, err)
	write_json(config, {'repo': {'branch': 'develop'}})
	code, out, err = push()
	suite.check('a search made before that record reads the original\'s branch '
		'from config.json', code == 0 and 'target branch develop, from the '
		'search\'s config.json' in out, err + out)
	code, out, err = push('--target-branch', 'release')
	suite.check('and --target-branch overrides both', code == 0
		and 'target branch release, from the flags' in out, err + out)
	os.remove(config)
	os.rename(step1 + '.aside', step1)

	print('\nrefusals')
	original = read(body)
	write(body, original + 'by hand\n')
	code, out, err = push('--post')
	suite.check('a body that is not what the tables render to is refused',
		code == 1 and 'not what the tables and prose render to' in err
		and reads_only() and branches(upstream) == ['main'], err)
	write(body, original.replace('#7', '#<bug issue>', 1))
	code, out, err = push('--post')
	suite.check('a body holding #<bug issue> is refused', code == 1
		and '#<bug issue>' in err and reads_only(), err)
	write(body, original)

	title = os.path.join(ship, 'body.title')
	write(title, 'add unit tests\n')
	code, out, err = push()
	suite.check('a title attribution.py refuses is refused', code == 1
		and 'title: it must start with a capital letter' in err, err)
	write(title, TITLE + '\n')

	write(os.path.join(suite_dir, 'tests', 'test_new.py'), 'changed\n')
	code, out, err = push('--post')
	suite.check('an uncommitted change in the clone is refused', code == 1
		and 'uncommitted' in err and reads_only(), err)
	git(suite_dir, 'checkout', '--', 'tests/test_new.py')

	files = os.path.join(ship, 'files.tsv')
	listed_files = read(files)
	write(files, listed_files + 'A\ttests/test_gone.py\n')
	code, out, err = push()
	suite.check('a files.tsv that is not the branch\'s files is refused',
		code == 1 and 'files.tsv is not the files' in err, err)
	write(files, listed_files)

	write_json(config, {'after': {'commit_trailer': 'Co-Authored-By: Someone '
		'<s@example.org>'}})
	code, out, err = push()
	suite.check('a commit without after.commit_trailer is refused', code == 1
		and 'its last line is not after.commit_trailer' in err, err)
	os.remove(config)

	code, out, err = push('--edit')
	suite.check('--edit with no PR open is refused', code == 1
		and 'no PR is open to edit' in err, err)

	code, out, err = push('--post', GH_LOGIN='someone')
	suite.check('another owner needs --other-owner', code == 1
		and '--other-owner' in err and branches(upstream) == ['main']
		and 'pr create' not in calls(), err)

	print('\nposting through a fork')
	git(suite_dir, 'remote', 'add', 'fork', 'https://github.com/me/pkg.git')
	write_json(config, {'after': {'pr_remote': 'fork', 'pr_head_owner': 'me',
		'branch_prefix': 'yes'}})
	code, out, err = push()
	suite.check('with after.pr_remote and after.pr_head_owner the preview uses '
		'the fork with no flags', code == 0 and '-> {} on remote fork '
		'(https://github.com/me/pkg.git, from after.pr_remote), head me:{}'
		.format(BRANCH, BRANCH) in out, err + out)
	code, out, err = push('--head', 'other:' + BRANCH, '--remote', 'origin')
	suite.check('and --remote and --head override them', code == 0
		and '-> {} on remote origin (https://github.com/example/pkg.git, from '
		'--remote), head other:{}'.format(BRANCH, BRANCH) in out, err + out)
	code, out, err = push('--post')
	suite.check('--post pushes the branch to the fork and opens the PR with '
		'the fork\'s head', code == 0 and 'opened https://github.com/example/'
		'pkg/pull/12' in out and BRANCH in branches(fork)
		and branches(upstream) == ['main'] and 'pr create --repo example/pkg '
		'--base main --head me:{} --title {} --body-file {}'.format(BRANCH,
		TITLE, body) in calls(), err + out + calls())

	print('\nposting to origin')
	os.remove(config)
	os.remove(os.path.join(ship, 'pr.json'))
	code, out, err = push('--post', GH_NUMBER='13')
	suite.check('--post pushes the branch\'s commit under the name without '
		'the prefix, and opens the PR', code == 0 and 'opened https://github.com/'
		'example/pkg/pull/13' in out and git(upstream, 'rev-parse', PLAIN) ==
		git(suite_dir, 'rev-parse', BRANCH) and BRANCH not in branches(
		upstream), err + out)
	record = json.loads(read(os.path.join(ship, 'pr.json')))
	suite.equal('pr.json records the repository, URL, number, branch, title, '
		'digest and date', sorted(record), ['branch', 'date', 'digest', 'number',
		'repo', 'title', 'url'])
	suite.equal('with the values of this PR', (record['repo'], record['number'],
		record['branch'], record['title']), ('example/pkg', 13, PLAIN, TITLE))
	suite.check('gh pr create got the repository, target branch, head, title '
		'and body file', 'pr create --repo example/pkg --base main --head {} '
		'--title {} --body-file {}'.format(PLAIN, TITLE, body)
		in calls().splitlines(), calls())

	before = calls()
	code, out, err = push('--post')
	suite.check('a second --post is refused and opens nothing', code == 1
		and 'already opened: https://github.com/example/pkg/pull/13' in err
		and calls() == before, err + out)

	code, out, err = push('--edit')
	suite.check('--edit with an unchanged title and body edits nothing',
		code == 0 and 'nothing to edit' in out and 'pr edit' not in calls(),
		err + out)

	render(ship, TITLE + ' and tool')
	code, out, err = push()
	suite.check('a changed title is reported by the preview', code == 0
		and 'the title or body has changed since' in out, out + err)
	pushed = git(upstream, 'rev-parse', PLAIN)
	code, out, err = push('--edit')
	record = json.loads(read(os.path.join(ship, 'pr.json')))
	suite.check('--edit replaces the open PR\'s title and body, and records '
		'them', code == 0 and 'edited https://github.com/example/pkg/pull/13'
		in out and 'pr edit 13 --repo example/pkg --title {} and tool '
		'--body-file {}'.format(TITLE, body) in calls() and record['title'] ==
		TITLE + ' and tool', err + out + calls())
	suite.equal('and pushes nothing', git(upstream, 'rev-parse', PLAIN),
		pushed)

	record['repo'] = 'other/pkg'
	write_json(os.path.join(ship, 'pr.json'), record)
	code, out, err = push('--post')
	suite.check('a pr.json for another repository is refused', code == 1
		and 'not example/pkg' in err, err)


if __name__ == '__main__':
	raise SystemExit(main())
