#!/usr/bin/env python3
"""Push step 1's tests branch and open the unit-test PR.

	python3 tests_pr_push.py DIR --repo OWNER/REPO          # preview: pushes and opens nothing
	python3 tests_pr_push.py DIR --repo OWNER/REPO --post   # push the branch, open the PR
	python3 tests_pr_push.py DIR --repo OWNER/REPO --edit   # replace the open PR's title and body

DIR is `$WORK/ship/pr-tests`, holding `body.md` and `body.title`
(`tests_pr_body.py --out`), and `files.tsv` and `branch.json`
(`tests_pr_tables.py files`). The branch's commits are pushed as they are,
from the clone step 1 wrote them in; nothing here commits. Each default below
gives way to its flag:

- the clone, the branch and the commit it was made from (`--suite`,
	`--branch`, `--base`): `DIR/branch.json`;
- the branch the PR targets (`--target-branch`): `branch` in
	`$WORK/tests/branch.json`, which `autoopt.py tests-branch` wrote, else the
	original's branch as the search's config.json records it;
- the remote pushed to (`--remote`): `after.pr_remote` of the search, else
	origin; a remote name the clone does not know is read from the original
	repository (`$WORK/tests/branch.json`'s `path`), else taken as a URL;
- the name the branch gets there (`--remote-branch`): its own,
	`auto-optimize/<date>-unit-tests`, when the user chose that prefix at step 5
	(`after.branch_prefix` yes), else `unit-tests-<date>`;
- the PR's head (`--head OWNER:BRANCH`): `after.pr_head_owner:BRANCH` when the
	remote came from `after.pr_remote`, else that name.

The preview prints the repository, the target branch, the head, the title,
the commit's files and what `--post` needs; its one `gh` call reads the
account `gh` is logged in as. It refuses, listing every problem, when
attribution.py rejects the title or the body (a `#<bug issue>` still in it,
say); the body is not what the tables and prose render to; files.tsv is not
the branch's files; the clone has uncommitted changes; a commit lacks
after.commit_trailer; or `DIR/pr.json` names another repository.

`--post` pushes the branch, runs `gh pr create` and records the PR in
`DIR/pr.json`; once that record exists, `--post` is refused. `--edit`
replaces the open PR's title and body (`gh pr edit`) and pushes nothing. When
OWNER is not the account `gh` is logged in as, both also need
`--other-owner`, given once the user has confirmed the repository.
`--allow-source PATH` is the renderer's flag of that name, passed on: give the
push the flags body.md was rendered with.

Standard library only, Python 3.9 or later; needs git, and gh for `--post`
and `--edit`.
"""

import os
import re
import sys
import json
import shutil
import argparse
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution
import autoopt
import publish

RECORD = 'pr.json'
HERE = os.path.dirname(os.path.abspath(__file__))

run = publish.run


def refuse(problems, left='nothing done'):
	return publish.refuse('tests_pr_push', problems, left)


def rendered(directory, title, allow_source=()):
	"""The body the tables and prose render to, or None and the reasons they
	do not."""

	with tempfile.TemporaryDirectory() as scratch:
		out = os.path.join(scratch, 'body.md')
		code, _, err = run([sys.executable, os.path.join(HERE,
			'tests_pr_body.py'), directory, '--title', title, '--out', out]
			+ [flag for path in allow_source for flag in ('--allow-source', path)])

		if code != 0:
			return None, err

		return publish.read(out), ''


def search_config(work):
	"""The search's config.json, or {} when `work` holds no search."""

	path = os.path.join(work, 'autooptimize', 'config.json')

	return autoopt.read_json(path) if os.path.exists(path) else {}


def target_branch(work, config):
	"""(the original's branch, where it was read), or (None, None)."""

	record = autoopt.work_path(work, 'tests-branch')

	if os.path.exists(record):
		branch = autoopt.read_json(record).get('branch')

		if branch:
			return branch, record

	if config:
		# A search made before `tests-branch` wrote its record.
		try:
			branch = autoopt.original_of(work, config).get('branch')
		except (RuntimeError, OSError, KeyError):
			branch = None

		if branch:
			return branch, 'the search\'s config.json'

	return None, None


def git_problems(args, work):
	"""(problems, the branch's changed files as `git diff --name-status`
	prints them, the number of commits over the base)."""

	problems = []
	span = '{}...{}'.format(args.base, args.branch)
	code, count, err = run(['git', '-C', args.suite, 'rev-list', '--count',
		'{}..{}'.format(args.base, args.branch)])

	if code != 0:
		return ['git: {}..{}: {}'.format(args.base, args.branch, err)], '', None

	code, out, err = run(['git', '-C', args.suite, 'status', '--porcelain',
		'--untracked-files=no'])

	if out:
		problems.append('{} has uncommitted changes:\n    {}'.format(args.suite,
			out.replace('\n', '\n    ')))

	trailer = autoopt.commit_trailer(os.path.join(work, 'autooptimize'))

	if trailer:
		code, out, err = run(['git', '-C', args.suite, 'log', '--format=%H%n%B'
			'%x00', '{}..{}'.format(args.base, args.branch)])

		for entry in out.split('\0'):
			head, _, message = entry.strip().partition('\n')

			for problem in autoopt.trailer_problems(message, trailer) if head \
					else []:
				problems.append('commit {}: {}'.format(head[:10], problem))

	code, changed, err = run(['git', '-C', args.suite, 'diff', '--name-status',
		'--no-renames', span])

	if code != 0:
		problems.append('git diff {}: {}'.format(span, err))

	return problems, changed, count


def listed_files(directory):
	"""files.tsv as sorted (status, path) pairs, or None when it is absent."""

	path = os.path.join(directory, 'files.tsv')

	if not os.path.exists(path):
		return None

	rows = [line.split('\t') for line in publish.read(path).splitlines()[1:]
		if line.strip()]

	return sorted((row[0][:1], row[1]) for row in rows if len(row) > 1)


def read_record(directory):
	path = os.path.join(directory, RECORD)

	if not os.path.exists(path):
		return None

	return json.loads(publish.read(path))


def main(argv=None):
	parser = argparse.ArgumentParser(description='Preview, then push step 1\'s '
		'tests branch and open the unit-test PR. Pushes and opens nothing '
		'without --post or --edit.')
	parser.add_argument('dir', help='$WORK/ship/pr-tests: body.md, body.title, '
		'files.tsv and branch.json')
	parser.add_argument('--repo', required=True, help='OWNER/REPO the PR '
		'opens on')
	parser.add_argument('--suite', help='the clone holding the tests branch '
		'(default: DIR/branch.json, which `tests_pr_tables.py files` wrote)')
	parser.add_argument('--branch', help='the tests branch (default: '
		'DIR/branch.json)')
	parser.add_argument('--base', help='the commit the tests branch was made '
		'from (default: DIR/branch.json)')
	parser.add_argument('--remote-branch', help='the name the branch gets '
		'on the remote (default: its own, auto-optimize/<date>-unit-tests, '
		'when the search\'s after.branch_prefix is yes, else '
		'unit-tests-<date>)')
	parser.add_argument('--target-branch', help='the branch of the original '
		'the PR targets (default: `branch` in $WORK/tests/branch.json, else the '
		'original\'s branch in the search\'s config.json)')
	parser.add_argument('--remote', help='the remote to push to (default: '
		'after.pr_remote of the search, else origin)')
	parser.add_argument('--head', help='OWNER:BRANCH for a fork (default: '
		'after.pr_head_owner:BRANCH when the remote is after.pr_remote, else the '
		'branch)')
	parser.add_argument('--allow-source', action='append', default=[],
		metavar='PATH', help='a changed file outside the tests that the user '
		'approved by name (repeatable); the same flag tests_pr_body.py took when '
		'it rendered body.md, which is rendered again with it here')
	parser.add_argument('--post', action='store_true', help='push the branch '
		'and open the PR')
	parser.add_argument('--edit', action='store_true', help='replace the open '
		'PR\'s title and body; pushes nothing')
	parser.add_argument('--other-owner', action='store_true', help='confirm a '
		'repository owned by another account')
	args = parser.parse_args(argv)

	problems = []
	args.dir = os.path.abspath(args.dir)
	work = os.path.dirname(os.path.dirname(args.dir))
	config = search_config(work)
	after = config.get('after') or {}
	remote_from = '--remote'

	if not args.remote and after.get('pr_remote'):
		args.remote, remote_from = after['pr_remote'], 'after.pr_remote'
	elif not args.remote:
		args.remote, remote_from = 'origin', 'the default'
	recorded = os.path.join(args.dir, 'branch.json')

	if os.path.exists(recorded):
		known = json.loads(publish.read(recorded))
		args.suite = args.suite or known.get('suite')
		args.base = args.base or known.get('base')
		args.branch = args.branch or known.get('branch')

	for flag in ('suite', 'branch', 'base'):
		if not getattr(args, flag):
			problems.append('no --{}, and {} does not hold one (`tests_pr_tables.py '
				'files` writes it)'.format(flag, recorded))

	target_from = 'the flags'

	if not args.target_branch:
		args.target_branch, target_from = target_branch(work, config)

		if not args.target_branch:
			problems.append('no --target-branch, and neither {} (`autoopt.py '
				'tests-branch` writes it) nor the search\'s config.json names the '
				'original\'s branch'.format(autoopt.work_path(work, 'tests-branch')))

	if args.branch and not args.remote_branch:
		args.remote_branch = autoopt.tests_branch_remote(config, args.branch)

	if not args.head and remote_from == 'after.pr_remote' \
			and after.get('pr_head_owner') and args.branch:
		args.head = '{}:{}'.format(after['pr_head_owner'], args.remote_branch)

	head = args.head or args.remote_branch
	body_path = os.path.join(args.dir, 'body.md')
	title_path = os.path.join(args.dir, 'body.title')

	if not re.match(r'^[\w.-]+/[\w.-]+$', args.repo):
		problems.append('--repo must be OWNER/REPO')

	for path in (body_path, title_path):
		if not os.path.exists(path):
			problems.append('no {}: `tests_pr_body.py {} --title TITLE --out {}` '
				'writes it'.format(path, args.dir, body_path))

	if problems:
		return refuse(problems)

	body, title = publish.read(body_path), publish.read(title_path).strip()

	# A stand-in for the bug report's number is expected in a preview made
	# before the report is filed; posting or editing needs it filled.
	stand_ins = ['body: ' + problem for problem in
		attribution.placeholder_problems(body)]
	problems += [problem for problem in attribution.problems('tests', title,
		body) if args.post or args.edit or problem not in stand_ins]
	fresh, why = rendered(args.dir, title, args.allow_source)

	if fresh is None:
		problems.append('the tables and prose do not render:\n    '
			+ why.replace('\n', '\n    '))
	elif fresh != body:
		problems.append('body.md is not what the tables and prose render to; '
			'render it again with tests_pr_body.py (it also fills the bug '
			'report\'s number, once filed)')

	extra, changed, commits = git_problems(args, work)
	problems += extra
	listed = listed_files(args.dir)
	actual = sorted((line.split('\t')[0][:1], line.split('\t')[-1])
		for line in changed.splitlines() if '\t' in line)

	if listed is None:
		problems.append('no files.tsv in {}: `tests_pr_tables.py files` writes '
			'it, and the opening\'s claim about changed files is checked '
			'against it'.format(args.dir))
	elif commits is not None and listed != actual:
		problems.append('files.tsv is not the files {} changes over {}: run '
			'`tests_pr_tables.py files` again, then render the body again'.format(
			args.branch, args.base))

	record = read_record(args.dir)

	if record and record.get('repo') != args.repo:
		problems.append('{} says this was opened on {}, not {}'.format(RECORD,
			record.get('repo'), args.repo))

	if (args.post or args.edit) and not shutil.which('gh'):
		problems.append('--post and --edit need gh on the PATH')

	if args.edit and not record:
		problems.append('--edit: no {} in {}, so no PR is open to edit; --post '
			'opens it'.format(RECORD, args.dir))

	if args.post and not args.edit and record:
		problems.append('already opened: {}. A PR is opened once; --edit '
			'replaces its title and body'.format(record.get('url')))

	if problems:
		return refuse(problems)

	changed_body = record and record.get('digest') != publish.digest(title, body)
	# The URL as the clone names it, before any `insteadOf` rewrite; a remote
	# the clone lacks is read from the original's clone, or taken as a URL,
	# as `autoopt.py pr` reads after.pr_remote.
	url = run(['git', '-C', args.suite, 'config', '--get', 'remote.{}.url'
		.format(args.remote)])[1]
	target = args.remote

	if not url:
		try:
			original = autoopt.original_of(work, config).get('path') if config \
				else None
		except (RuntimeError, OSError, KeyError):
			original = None

		url = autoopt.fork_url(None, original, args.remote)
		target = url or args.remote

	print('repository: {} (target branch {}, from {})'.format(args.repo,
		args.target_branch, target_from))
	print('branch:     {} -> {} on remote {} ({}, from {}), head {}'.format(
		args.branch, args.remote_branch, args.remote, url or 'no such remote',
		remote_from, head))
	print('title:      {}'.format(title))
	print('body:       {} ({:,} characters)'.format(body_path, len(body)))
	print('commits:    {} over {}, changing:\n    {}'.format(commits, args.base,
		changed.replace('\n', '\n    ')))

	for line in re.findall(r'(?m)^## .*$', body):
		print('section:    {}'.format(line[3:]))

	if record:
		print('already opened: {}{}'.format(record['url'], '; the title or body '
			'has changed since' if changed_body else ''))

	print('\n'.join(publish.post_needs(args.repo, 'remote {} ({}) -> branch {}; '
		'the PR opens on {} with head {}'.format(args.remote, url or
		'no such remote', args.remote_branch, args.repo, head))))

	if not args.post and not args.edit:
		print('\npreview only: nothing was pushed or opened. Run again with '
			'--post after the repository, branch, title and sections have '
			'been approved')
		return 0

	if args.edit:
		if not changed_body:
			print('nothing to edit: {} already has this title and body'.format(
				record['url']))
			return 0

		problem = publish.edit_pull(os.path.join(args.dir, RECORD), record,
			title, body, body_path, args.other_owner)

		if problem:
			return refuse([problem], 'nothing changed')

		print('edited {}'.format(record['url']))
		return 0

	problem = publish.account_problem(args.repo, args.other_owner)

	if problem:
		return refuse([problem])

	code, out, err = run(['git', '-C', args.suite, 'push', target,
		'{}:refs/heads/{}'.format(args.branch, args.remote_branch)])

	if code != 0:
		sys.stderr.write('tests_pr_push: git push failed ({}); no PR '
			'opened:\n{}\n{}\n'.format(code, out, err))
		return 1

	print('pushed {} to {} as {}'.format(args.branch, args.remote,
		args.remote_branch))
	code, out, err = publish.run_gh(['pr', 'create', '--repo', args.repo,
		'--base', args.target_branch, '--head', head, '--title', title,
		'--body-file', body_path])
	found = publish.PULL_URL.search(out)

	if code != 0 or not found:
		sys.stderr.write('tests_pr_push: gh pr create failed ({}); the branch is '
			'pushed, so running this again retries it:\n{}\n{}\n'.format(code,
			out, err))
		return 1

	record = publish.new_record(args.repo, found.group(0), args.remote_branch,
		title, body)
	publish.write_record(os.path.join(args.dir, RECORD), record)
	print('opened {}'.format(record['url']))

	return 0


if __name__ == '__main__':
	raise SystemExit(main())
