#!/usr/bin/env python3
"""Step 5: ship an ended search (`references/step5-ship.md`).

	python3 ship.py plan WORK                 # what this search can ship, and the question to ask
	python3 ship.py apply WORK [--to DIR] [--yes]
	python3 ship.py branch WORK [--name N] [--repo PATH] [--yes]
	python3 ship.py brief WORK                # the writer agent's prompt for the GitHub documents
	python3 ship.py status WORK               # each chosen document: inputs, body, posted
	python3 ship.py render WORK [--only bugs|tests|optimization]
	python3 ship.py rehearse WORK             # render and preview every chosen document together
	python3 ship.py post WORK all --approved --digest D [--other-owner]

The user answers one question first: how to ship. `apply` writes a plain
search's change into the original files; `branch` makes a branch in the
original repository holding one commit per kept change; the GitHub route
opens the documents the user checked (the optimization PR, the unit-test PR,
the bug report), reviewed together and posted together. Every command
previews by default and writes nothing until `--yes` (apply, branch) or
`post --approved` (GitHub).

Standard library only.
"""

import os
import sys
import json
import hashlib
import argparse
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autoopt
import publish

HERE = os.path.dirname(os.path.abspath(__file__))


def fail(message):
	publish.fail('ship', message)


def search(work_arg):
	"""`(work, root, config)` for a search directory, the one holding
	`autooptimize/` (or `autooptimize/` itself)."""

	work = os.path.abspath(os.path.expanduser(work_arg))

	if os.path.basename(work) == 'autooptimize':
		work = os.path.dirname(work)

	root = os.path.join(work, 'autooptimize')

	if not os.path.exists(os.path.join(root, 'config.json')):
		fail('{} holds no search (no autooptimize/config.json)'.format(work))

	return work, root, autoopt.load_config(root)


def after_of(config):
	return dict(autoopt.AFTER_DEFAULTS, **(config.get('after') or {}))


def show(git_dir, ref, path):
	"""The bytes of `path` at `ref`, and its git mode, or (None, None)."""

	try:
		entry = autoopt.git('-C', git_dir, 'ls-tree', ref, '--', path).split()
	except RuntimeError:
		return None, None

	if not entry:
		return None, None

	data = subprocess.run(['git', '-C', git_dir, 'cat-file', 'blob', entry[2]],
		capture_output=True, check=True).stdout

	return data, entry[0]


def read_bytes(path):
	try:
		with open(path, 'rb') as handle:
			return handle.read()
	except OSError:
		return None


###
# apply: a plain search's change, written into the original files
###

def apply_plan(work, root, config, identifier, to):
	"""What `apply` would do: `{'rows': [(path, destination, action)],
	'skipped': [paths the search wrote], 'problems': [...]}`. A path is
	applied where it came from: under `to`, else under the folder that held
	the files `init --copy` took (`config['copied']`)."""

	bare = os.path.join(root, 'repo.git')
	head = autoopt.branch_of(root, identifier)
	changed = [line.split('\t') for line in autoopt.git('-C', bare, 'diff',
		'--name-status', '--no-renames', 'autoopt/0', head).splitlines() if
		line.strip()]
	base_files = set(autoopt.git('-C', bare, 'ls-tree', '-r', '--name-only',
		'autoopt/0').splitlines())
	copied = config.get('copied') or []
	names = [entry['as'] for entry in copied]
	problems, rows, skipped, already = [], [], [], []

	def from_user(path):
		return any(path == name or path.startswith(name + '/') for name in
			names)

	if to:
		destination = lambda path: os.path.join(to, path)
	elif copied:
		parents = {os.path.dirname(entry['source']) for entry in copied}

		def destination(path):
			for entry in copied:
				if path == entry['as'] or path.startswith(entry['as'] + '/'):
					return os.path.join(os.path.dirname(entry['source']), path)

			# A file the change adds beside the copied ones goes into their
			# folder, when they all came from one.
			return os.path.join(next(iter(parents)), path) if len(parents) \
				== 1 else None
	else:
		fail('this search does not record where its files came from (it was '
			'made before `init` kept that record): pass --to DIR, the folder '
			'that holds {}'.format(', '.join(sorted(base_files)[:5])))

	for status, path in changed:
		# The baseline holds files the search wrote itself (the benchmark):
		# they are no part of the user's code.
		if path in base_files and not from_user(path) and (copied or not to):
			skipped.append(path)
			continue

		target = destination(path)

		if target is None:
			problems.append('{} is new, and the copied files came from more '
				'than one folder: pass --to DIR for it'.format(path))
			continue

		before = show(bare, 'autoopt/0', path)[0]
		after = show(bare, head, path)[0] if status != 'D' else None
		current = read_bytes(target)

		if current == after:
			already.append(target)
			continue

		if status == 'A':
			if current is not None:
				problems.append('{} exists already, and the change adds it'
					.format(target))
				continue
		elif current is None:
			problems.append('{} is missing; the search copied it from there'
				.format(target))
			continue
		elif current != before:
			problems.append('{} changed since the search copied it; the '
				'change was measured on the copy, so it is not written over '
				'edits it never saw'.format(target))
			continue

		rows.append((path, target, {'A': 'add', 'D': 'delete'}.get(status,
			'change')))

	return {'rows': rows, 'skipped': skipped, 'problems': problems,
		'head': head, 'already': already}


def apply(args):
	work, root, config = search(args.work)
	autoopt.require_ended(root, 'apply its change')

	if config.get('repo'):
		fail('this is a repository search: `ship.py branch` makes a branch in '
			'the original, and the GitHub documents open PRs; `apply` is for a '
			'search made with `init --copy`')

	identifier = autoopt.resolve_id(root, config, args.id)
	problem = autoopt.measured_problem(root, config, identifier)

	if problem:
		fail(problem)

	to = os.path.abspath(os.path.expanduser(args.to)) if args.to else None
	plan = apply_plan(work, root, config, identifier, to)
	bare = os.path.join(root, 'repo.git')
	stat = autoopt.git('-C', bare, 'diff', '--stat', 'autoopt/0', plan['head'],
		'--', *[path for path, _, _ in plan['rows']]).rstrip() if plan[
		'rows'] else ''

	print('{} ({}): {} file(s)'.format(autoopt.label(root, identifier),
		(autoopt.load_result(root, identifier) or {}).get('title'),
		len(plan['rows'])))

	for path, target, action in plan['rows']:
		print('  {:7} {}'.format(action, target))

	for path in plan['skipped']:
		print('  not applied: {}, which the search wrote itself (it was not '
			'copied from your files)'.format(path))

	for target in plan['already']:
		print('  already holds the change: {}'.format(target))

	if stat:
		print('\n' + stat)

	if plan['problems']:
		fail('nothing was written:\n  ' + '\n  '.join(plan['problems']))

	if not plan['rows'] and plan['already']:
		print('\nthe change is applied already; nothing to write')
		return 0

	if not plan['rows']:
		fail('the change touches none of the copied files; nothing to apply')

	if not args.yes:
		print('\npreview only: nothing was written. `--yes` writes these '
			'files once the user has agreed.')
		return 0

	# Every file's new bytes first, then every write, so a failure while
	# reading leaves the originals as they were.
	writes = []

	for path, target, action in plan['rows']:
		data, mode = show(bare, plan['head'], path) if action != 'delete' \
			else (None, None)
		writes.append((target, action, data, mode))

	for target, action, data, mode in writes:
		if action == 'delete':
			os.remove(target)
			continue

		os.makedirs(os.path.dirname(target) or '.', exist_ok=True)

		with open(target + '.ship-tmp', 'wb') as out:
			out.write(data)

		if mode == '100755':
			os.chmod(target + '.ship-tmp', 0o755)
		elif os.path.exists(target):
			os.chmod(target + '.ship-tmp', os.stat(target).st_mode & 0o7777)

		os.replace(target + '.ship-tmp', target)

	patch = autoopt.git('-C', bare, 'diff', '--binary', 'autoopt/0',
		plan['head'], '--', *[path for path, _, _ in plan['rows']])
	folder = autoopt.work_path(work, 'ship')
	os.makedirs(folder, exist_ok=True)
	publish.write(os.path.join(folder, 'applied.patch'), patch)
	publish.write_json(os.path.join(folder, 'applied.json'), {'id': identifier,
		'label': autoopt.label(root, identifier), 'commit': autoopt.git('-C',
		bare, 'rev-parse', plan['head']).strip(), 'files': [{'path': path,
		'destination': target, 'action': action} for path, target, action
		in plan['rows']], 'skipped': plan['skipped'], 'patch': os.path.join(
		folder, 'applied.patch'), 'at': autoopt.now()})
	autoopt.hold_reopen(root, 'apply', 'written into the original files')
	print('\nwrote {} file(s); the patch is {}'.format(len(writes),
		os.path.join(folder, 'applied.patch')))

	return 0


###
# branch: a repository search's change, as a branch in the original
###

def local_messages(root, steps):
	"""Each step's commit message: its `## Commit message`, else its title,
	with `after.commit_trailer` added."""

	trailer = autoopt.commit_trailer(root)
	messages = {}

	for step, _ in steps:
		message = autoopt.change_fragment(root, step).get('message')

		if not message:
			message = (autoopt.load_result(root, step) or {}).get('title') or \
				'Change {}'.format(autoopt.label(root, step))

		messages[step] = autoopt.with_trailer(message, trailer)

	return messages


def path_steps(root, config, identifier):
	bare = os.path.join(root, 'repo.git')
	strip = autoopt.outputs_stripper(root, config)
	start = config['repo']['commit']

	return autoopt.winning_steps(root, autoopt.lineage(root, identifier),
		lambda i: strip(autoopt.git('-C', bare, 'rev-parse', autoopt.branch_of(
		root, i) + '^{tree}').strip()), strip(autoopt.git('-C', bare,
		'rev-parse', start + '^{tree}').strip()))


def prefix_problem(config):
	"""Refuse a branch named before the user said whether branch names start
	with `auto-optimize/`, the third question of `ship.py plan`."""

	if after_of(config).get('branch_prefix') not in ('yes', 'no'):
		fail('the user has not said whether the branch names start with {} '
			'(after.branch_prefix): ask it with the other questions of `ship.py '
			'plan`'.format(autoopt.BRANCH_PREFIX))


def branch(args):
	work, root, config = search(args.work)
	autoopt.require_ended(root, 'make a branch from it')

	if not config.get('repo'):
		fail('this search was made with `init --copy`; `ship.py apply` writes '
			'its change into the original files')

	identifier = autoopt.resolve_id(root, config, args.id)
	problem = autoopt.measured_problem(root, config, identifier)

	if problem:
		fail(problem)

	original = autoopt.original_of(work, config)
	path = os.path.abspath(os.path.expanduser(args.repo)) if args.repo else \
		original.get('path')

	if not path:
		fail('the original repository\'s folder is not recorded: pass --repo '
			'PATH')

	prefix_problem(config)
	messages = local_messages(root, path_steps(root, config, identifier))
	made = autoopt.pr_commits(root, config, identifier, messages)
	bare = os.path.join(root, 'repo.git')
	name = args.name or autoopt.ship_branch(config, 'optimized-{}'.format(
		autoopt.now()[:10]))
	listing = autoopt.git('-C', bare, 'log', '--reverse', '--format=%h %s',
		'{}..{}'.format(made['base'], made['tip'])).rstrip()
	stat = autoopt.git('-C', bare, 'diff', '--stat', made['base'],
		made['tip']).rstrip()

	print('{} commit(s) on {} ({}) -> branch {} in {}'.format(len(
		made['series']), original.get('branch') or '?', made['base'][:10], name,
		path))

	if made['suite']:
		print('without the unit-test suite: each change applied to the '
			'original\'s commit')

	print('\nthe commits, oldest first:\n' + listing + '\n\n' + stat)

	exists = subprocess.run(['git', '-C', path, 'rev-parse', '--verify', '-q',
		'refs/heads/' + name], capture_output=True).returncode == 0

	if exists:
		fail('{} already has a branch {}: pass --name'.format(path, name))

	if not args.yes:
		print('\npreview only: nothing was written. `--yes` makes the branch '
			'once the user has agreed; it does not check it out.')
		return 0

	autoopt.git('-C', path, 'fetch', '-q', bare, 'refs/pr/{}:refs/heads/{}'
		.format(identifier, name))
	publish.write_json(autoopt.work_path(work, 'ship', 'branch.json'), {
		'id': identifier, 'label': autoopt.label(root, identifier), 'repo': path,
		'branch': name, 'base': made['base'], 'tip': made['tip'],
		'commits': [sha for _, sha in made['series']], 'at': autoopt.now()})
	autoopt.hold_reopen(root, 'branch', 'branch {} in {}'.format(name, path))
	print('\nmade branch {} in {}: `git -C {} switch {}` checks it out'.format(
		name, path, path, name))

	return 0


###
# plan: what this search can ship, and the question to ask
###

def bug_folders(work):
	folder = os.path.join(autoopt.work_path(work, 'tests'), 'work', 'bugs')

	try:
		return sorted(name for name in os.listdir(folder) if os.path.isdir(
			os.path.join(folder, name)))
	except OSError:
		return []


def offer(work, root, config):
	"""`{'mode', 'routes': [(choice, label, description)], 'documents':
	[(document, label, description)], 'facts': [lines]}`."""

	facts, documents = [], []
	ended = autoopt.load_ended(root) or {}
	winner = ended.get('winner') or {}
	medians = ended.get('medians') or {}
	facts.append('the search ended on {}: {} {} {} -> {}'.format((ended.get(
		'ended_at') or '?')[:10], winner.get('label', '?'), medians.get(
		'objective', ''), medians.get('A0'), medians.get('winner')))

	if not config.get('repo'):
		copied = config.get('copied') or []
		facts.append('plain mode; copied from: {}'.format(', '.join(entry[
			'source'] for entry in copied) if copied else 'not recorded (apply '
			'needs --to DIR)'))
		return {'mode': 'plain', 'facts': facts, 'documents': [], 'branches':
			[], 'routes': [
			('apply', 'Apply the change to the original file(s)', 'writes the '
			'measured files over the originals after a preview; refuses a file '
			'that changed since the search copied it'),
			('none', 'Not now', 'nothing is written; the change stays in the '
			'search, and step 6 copies it out')]}

	original = autoopt.original_of(work, config)
	slug = autoopt.github_slug(original.get('remote_url'))
	suite = original.get('suite')
	bugs = bug_folders(work)
	facts.append('repository mode: {} on {} at {}'.format(original.get('path')
		or '(original folder not recorded)', original.get('branch'),
		(original.get('commit') or '?')[:10]))
	facts.append('remote: {}{}'.format(original.get('remote_url') or 'none',
		' (GitHub {})'.format(slug) if slug else ' (not GitHub)'))

	if slug:
		push = publish.can_push(slug)
		facts.append('gh: {}; push to {}: {}'.format(publish.login() or
			'not logged in or not installed', slug, {True: 'yes', False: 'no, '
			'so the PRs come from a fork (after.pr_remote, after.pr_head_owner)',
			None: 'unknown'}[push]))

	facts.append('unit-test suite from step 1: {}'.format('yes, branch {}'
		.format(suite.get('tests_branch')) if suite else 'no'))
	facts.append('bugs step 1 recorded: {}'.format(', '.join(bugs) if bugs else
		'none'))
	routes = []

	if slug:
		routes.append(('github', 'Open GitHub documents', 'you pick which: '
			'the optimization PR{}{}; all are shown to you together before '
			'anything is posted'.format(', the unit-test PR' if suite else '',
			', the bug report' if bugs else '')))
		documents.append(('optimization', 'The optimization PR', 'one commit '
			'per kept change, on {}{}'.format(original.get('branch'),
			', separate from the unit tests' if suite else '')))

		if suite:
			documents.append(('tests', 'The unit-test PR', 'the new suite '
				'alone, as step 1 squashed it'))

		if bugs:
			documents.append(('bugs', 'The bug report', 'one GitHub issue for '
				'the {} bug(s) step 1 found, each reproduction re-run on {} '
				'first'.format(len(bugs), original.get('branch'))))

	routes.append(('branch', 'A branch in your local repository', 'one commit '
		'per kept change, as a new branch in {}; nothing is pushed'.format(
		original.get('path') or 'the original')))
	routes.append(('none', 'Not now', 'nothing is published or written; the '
		'change stays in the search'))

	# The names step 5 would give, both ways: the optimization PR's comes
	# from its title, written later, so it is shown by its form.
	date = autoopt.now()[:10]
	tests_branch = (suite or {}).get('tests_branch') or \
		'{}{}-unit-tests'.format(autoopt.BRANCH_PREFIX, date)
	branches = []

	for answer, label in (('no', 'No'), ('yes', 'Yes')):
		named = dict(config, after=dict(after_of(config), branch_prefix=answer))
		names = ['`{}`'.format(autoopt.ship_branch(named, '<from-the-pr-title>'))]

		if suite:
			names.append('`{}`'.format(autoopt.tests_branch_remote(named,
				tests_branch)))

		names.append('`{}`'.format(autoopt.ship_branch(named,
			'optimized-{}'.format(date))))
		branches.append((label, ', '.join(names)))

	return {'mode': 'repository', 'facts': facts, 'routes': routes,
		'documents': documents, 'branches': branches}


def plan(args):
	work, root, config = search(args.work)
	autoopt.require_ended(root, 'ship')
	offered = offer(work, root, config)
	after = after_of(config)

	if args.json:
		print(json.dumps(offered, indent=1))
		return 0

	print('\n'.join(offered['facts']))
	print('\nAsk the user (AskUserQuestion), "How do you want to ship?":')

	for number, (choice, text, description) in enumerate(offered['routes'],
			1):
		print('  {}. {}{}: {}'.format(number, text, ' (Recommended)' if
			number == 1 and choice != 'none' else '', description))

	if offered['documents']:
		print('\nOn "Open GitHub documents", a multiSelect checklist, "Which '
			'documents?":')

		for document, text, description in offered['documents']:
			print('  - {}: {}'.format(text, description))

	if offered['branches']:
		print('\nFor a branch or a PR, "Start the branch names with {}?":'
			.format(autoopt.BRANCH_PREFIX))

		for choice, names in offered['branches']:
			print('  - {}: {}'.format(choice, names))

	print('\nRecord the answers in the user\'s words:\n  python3 {0} config '
		'{1} --set after.ship=<{2}> --reason "<their words>"'.format(
		os.path.join(HERE, 'autoopt.py'), work, '|'.join(choice for choice, _,
		_ in offered['routes'])))

	if offered['documents']:
		print('  python3 {} config {} --set after.ship_documents={} --reason '
			'"<their words>"'.format(os.path.join(HERE, 'autoopt.py'), work,
			','.join(document for document, _, _ in offered['documents'])))

	if offered['branches']:
		print('  python3 {} config {} --set after.branch_prefix=<no|yes> '
			'--reason "<their words>"'.format(os.path.join(HERE, 'autoopt.py'),
			work))

	if after.get('ship') not in (None, 'ask'):
		print('\nalready answered: after.ship = {}{}'.format(after['ship'],
			'; documents: {}'.format(', '.join(after['ship_documents'])) if
			after.get('ship_documents') else ''))

	return 0


###
# The GitHub documents: brief, status, render, rehearse, post
###

ORDER = autoopt.SHIP_DOCUMENTS
NAMES = {'bugs': 'the bug report', 'tests': 'the unit-test PR',
	'optimization': 'the optimization PR'}
REFERENCES = {'bugs': 'references/issue-bugs.md', 'tests':
	'references/pr-tests.md', 'optimization': 'references/pr-optimization.md'}
# What a document's folder holds that is rendered or posted from its inputs,
# not an input: left out of the digest the review approves.
OUTPUTS = {'body.md', 'pr.json', 'filed.json', 'dupes.tsv'}
OUTPUT_DIRS = {'verify-logs', 'work'}


def folder(work, document):
	return autoopt.work_path(work, 'ship', autoopt.SHIP_FOLDERS[document])


def github(work, config):
	"""`(after, documents, slug)` of a search whose user chose GitHub
	documents; `fail`s otherwise."""

	after = after_of(config)

	if after.get('ship') != 'github':
		fail('after.ship is {!r}: the GitHub documents are for a user who chose '
			'them (`ship.py plan` gives the question)'.format(after.get('ship')))

	documents = [one for one in ORDER if one in (after.get('ship_documents')
		or [])]

	if not documents:
		fail('after.ship_documents names no document: record the user\'s '
			'checklist (`ship.py plan`)')

	slug = autoopt.github_slug(autoopt.original_of(work, config).get(
		'remote_url'))

	if not slug:
		fail('the original\'s remote is not on GitHub')

	return after, documents, slug


def allow_source(directory):
	"""The `--allow-source` flags the unit-test PR is rendered and pushed
	with: one path per line of `allow-source.txt`, when the writer kept one."""

	try:
		with open(os.path.join(directory, 'allow-source.txt'),
				encoding='utf-8') as handle:
			paths = [line.strip() for line in handle if line.strip()]
	except OSError:
		return []

	return [flag for path in paths for flag in ('--allow-source', path)]


def render_command(work, document):
	directory = folder(work, document)
	out = os.path.join(directory, 'body.md')

	if document == 'bugs':
		return [os.path.join(HERE, 'issue_body.py'), directory, '--out', out]

	if document == 'tests':
		return [os.path.join(HERE, 'tests_pr_body.py'), directory, '--out', out,
			'--config', os.path.join(work, 'autooptimize', 'config.json')] + \
			allow_source(directory)

	return [os.path.join(HERE, 'opt_pr_body.py'), directory, '--out', out]


def post_command(work, document, slug, post, other_owner):
	directory = folder(work, document)
	flags = (['--post'] if post else []) + (['--other-owner'] if other_owner
		else [])

	if document == 'bugs':
		return [os.path.join(HERE, 'issue_body.py'), 'file', directory,
			'--repo', slug] + flags

	if document == 'tests':
		return [os.path.join(HERE, 'tests_pr_push.py'), directory, '--repo',
			slug] + allow_source(directory) + flags

	return [os.path.join(HERE, 'autoopt.py'), 'pr', work] + flags


def call(command):
	"""`(exit code, stdout + stderr)` of a script of this skill."""

	done = subprocess.run([sys.executable] + command, capture_output=True,
		text=True, stdin=subprocess.DEVNULL)

	return done.returncode, (done.stdout + done.stderr).rstrip()


def render_document(work, document):
	directory = folder(work, document)

	if not os.path.exists(os.path.join(directory, 'body.title')):
		return 1, ('{} has no body.title: the writer renders it once with '
			'--title ({})'.format(directory, REFERENCES[document]))

	return call(render_command(work, document))


def inputs_digest(work, documents):
	"""A digest of everything the chosen documents are rendered from: every
	file in their folders except what is rendered or posted from them."""

	hasher = hashlib.sha256()

	for document in documents:
		base = folder(work, document)

		for top, dirs, files in os.walk(base):
			dirs[:] = sorted(name for name in dirs if name not in OUTPUT_DIRS
				and not name.startswith('before-renumber-'))

			for name in sorted(files):
				if name in OUTPUTS or name.endswith('.tmp') or name.startswith(
						'body.comment-'):
					continue

				path = os.path.join(top, name)
				hasher.update(os.path.relpath(path, autoopt.work_path(work,
					'ship')).encode() + b'\0')

				with open(path, 'rb') as handle:
					hasher.update(handle.read())

				hasher.update(b'\0')

	return hasher.hexdigest()[:16]


def rehearsal_path(work):
	return autoopt.work_path(work, 'ship', 'rehearsal.json')


def brief(args):
	work, root, config = search(args.work)
	autoopt.require_ended(root, 'ship')
	after, documents, slug = github(work, config)
	original = autoopt.original_of(work, config)
	lines = ['You write the GitHub documents for step 5 of the ended search in '
		'{}: {}. Write their inputs and render them; post nothing, which the '
		'user approves after one review of all of them.'.format(work,
		', '.join(NAMES[one] for one in documents)), '',
		'AO={}'.format(HERE), 'WORK={}'.format(work), '',
		'Read {}/../references/documents.md first, then each document\'s '
		'reference before its work. They open on {} (branch {}).'.format(HERE,
		slug, original.get('branch'))]

	if 'bugs' in documents:
		lines += ['', '**The bug report** ({}): turn each folder of {} into a '
			'row of bugs.tsv and its prose fields in {}; check `issue_body.py '
			'dupes`; run `issue_body.py verify` on the original\'s build until '
			'every bug passes or is marked manual; render it with a title.'
			.format(REFERENCES['bugs'], os.path.join(autoopt.work_path(work,
			'tests'), 'work', 'bugs'), folder(work, 'bugs'))]

	if 'tests' in documents:
		lines += ['', '**The unit-test PR** ({}): make its tables in {} from '
			'the suite in {} and step 1\'s coverage files in {}, write the '
			'prose, and render it with a title.'.format(REFERENCES['tests'],
			folder(work, 'tests'), autoopt.work_path(work, 'tests-suite'),
			os.path.join(autoopt.work_path(work, 'tests'), 'work'))]

	if 'optimization' in documents:
		lines += ['', '**The optimization PR** ({}): `python3 $AO/opt_pr_tables.py '
			'$WORK`, then `opt_pr_body.py {} --template` into prose.md, fill it '
			'from the kept changes\' diffs and READMEs, write each '
			'messages/<id>.txt the tables left as a skeleton, and render it '
			'with a title.'.format(REFERENCES['optimization'], folder(work,
			'optimization'))]

	lines += ['', 'Rules: every number comes from a table, so prose writes a '
		'token where it needs one; no document names the tool, the lab, or the '
		'search\'s own words (winner, baseline, iteration labels, local '
		'paths), or says how it was written (no "Generated with" line); the '
		'diffs decide what a change does. Then run `python3 '
		'$AO/ship.py rehearse $WORK` and fix every problem it lists until it '
		'exits 0. Report each title, the rehearsal\'s digest, and anything you '
		'could not settle.']

	print('\n'.join(lines))
	return 0


def status(args):
	work, root, config = search(args.work)
	after = after_of(config)
	print('after.ship: {}'.format(after.get('ship')))

	if after.get('ship') != 'github':
		return 0

	documents = [one for one in ORDER if one in (after.get('ship_documents')
		or [])]
	record = None

	try:
		record = autoopt.read_json(rehearsal_path(work))
	except OSError:
		pass

	for document in documents:
		directory = folder(work, document)
		posted = autoopt.ship_posted(work, document)
		title = publish.stored_title(os.path.join(directory, 'body.md'))

		if posted:
			state = 'posted: {}'.format(posted['url'])
		elif not os.path.isdir(directory):
			state = 'not started'
		elif not title:
			state = 'inputs only; not rendered with a title yet'
		else:
			state = 'rendered: "{}"'.format(title)

		print('{:20} {}'.format(NAMES[document], state))

	if record:
		current = inputs_digest(work, documents)
		print('rehearsal: digest {}{}{}'.format(record['digest'], '' if
			record.get('ok') else ', with problems', '' if current == record[
			'digest'] else '; the inputs changed since (rehearse again)'))
	else:
		print('rehearsal: none yet (`ship.py rehearse`)')

	return 0


def render(args):
	work, root, config = search(args.work)
	after, documents, slug = github(work, config)

	for document in documents:
		if args.only and document != args.only:
			continue

		code, out = render_document(work, document)
		print('{}: {}'.format(NAMES[document], 'rendered' if code == 0 else
			'refused'))

		if code:
			print(out)
			return 1

	return 0


def rehearse(args):
	"""Render every chosen document, run each one's preview (nothing is
	posted), and record a digest of their inputs for `post`."""

	work, root, config = search(args.work)
	autoopt.require_ended(root, 'ship')
	after, documents, slug = github(work, config)
	prefix_problem(config)
	scratch = autoopt.work_path(work, 'ship', 'rehearsal')
	os.makedirs(scratch, exist_ok=True)
	problems, lines = [], []

	for document in documents:
		directory = folder(work, document)
		code, out = render_document(work, document)

		if code == 0:
			code, out = call(post_command(work, document, slug, False, False))

		publish.write(os.path.join(scratch, '{}.txt'.format(document)), out +
			'\n')
		title = publish.stored_title(os.path.join(directory, 'body.md'))
		lines.append('{}: {}'.format(NAMES[document], '"{}"'.format(title) if
			title else 'no title'))
		lines.append('  body    {}'.format(os.path.join(directory, 'body.md')))
		lines.append('  preview {}'.format(os.path.join(scratch,
			'{}.txt'.format(document))))

		if code:
			problems.append('{}: {}'.format(NAMES[document], out.splitlines()[-1]
				if out else 'exit {}'.format(code)))
			lines.append('  REFUSED; the preview file has every problem')
		elif autoopt.ship_posted(work, document):
			lines.append('  posted already')

	digest = inputs_digest(work, documents)
	publish.write_json(rehearsal_path(work), {'digest': digest, 'documents':
		documents, 'ok': not problems, 'problems': problems, 'at':
		autoopt.now()})
	print('\n'.join(lines))

	if problems:
		print('\nrefused, nothing can be posted until these are fixed:\n  ' +
			'\n  '.join(problems))
		return 1

	print('\nShow the user every document above together. Once they ask for '
		'posting, `python3 {} post {} all --approved --digest {}` posts them '
		'in this order, filling each number into the next.'.format(
		os.path.join(HERE, 'ship.py'), work, digest))
	return 0


def post(args):
	work, root, config = search(args.work)
	autoopt.require_ended(root, 'ship')
	after, documents, slug = github(work, config)

	if args.which != 'all':
		fail('`post all` posts every chosen document, after one review of all '
			'of them')

	if not args.approved or not args.digest:
		fail('posting needs --approved and the --digest of the rehearsal the '
			'user reviewed (`ship.py rehearse`)')

	try:
		record = autoopt.read_json(rehearsal_path(work))
	except OSError:
		fail('no rehearsal: run `ship.py rehearse` and show the user every '
			'document first')

	if record.get('digest') != args.digest:
		fail('--digest {} is not the last rehearsal\'s ({}): show the user the '
			'documents of the last one'.format(args.digest, record.get(
			'digest')))

	if not record.get('ok'):
		fail('the last rehearsal had problems; fix them and rehearse again')

	if inputs_digest(work, documents) != args.digest:
		fail('the documents\' inputs changed since the rehearsal the user '
			'reviewed: rehearse again and show them the new batch')

	if record.get('documents') != documents:
		fail('the chosen documents changed since the rehearsal: rehearse again')

	for document in documents:
		if autoopt.ship_posted(work, document):
			print('{}: posted already, {}'.format(NAMES[document],
				autoopt.ship_posted(work, document)['url']))
			continue

		# Rendered again first, so the numbers of the documents posted
		# before it are filled in.
		code, out = render_document(work, document)

		if code == 0:
			code, out = call(post_command(work, document, slug, True,
				args.other_owner))

		print('{}:\n{}'.format(NAMES[document], out))

		if code:
			fail('{} was not posted; fix the cause and run the same command '
				'again: what was posted is skipped'.format(NAMES[document]))

		autoopt.hold_reopen(root, 'step 5', 'GitHub documents posted')

	return 0


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	commands = parser.add_subparsers(dest='command', required=True)

	for name, function, text in (('brief', brief, 'the writer agent\'s prompt '
			'for the chosen GitHub documents'), ('status', status, 'where each '
			'chosen document stands'), ('rehearse', rehearse, 'render every '
			'chosen document and preview it; posts nothing')):
		sub = commands.add_parser(name, help=text)
		sub.add_argument('work')
		sub.set_defaults(func=function)

	sub = commands.add_parser('render', help='render every chosen document '
		'again, in posting order')
	sub.add_argument('work')
	sub.add_argument('--only', choices=ORDER)
	sub.set_defaults(func=render)

	sub = commands.add_parser('post', help='post every chosen document, once '
		'the user approved the rehearsal')
	sub.add_argument('work')
	sub.add_argument('which', choices=('all',))
	sub.add_argument('--approved', action='store_true')
	sub.add_argument('--digest')
	sub.add_argument('--other-owner', action='store_true', help='confirm a '
		'repository another account owns, once the user has seen it')
	sub.set_defaults(func=post)

	sub = commands.add_parser('plan', help='what this search can ship, and '
		'the question to ask the user')
	sub.add_argument('work')
	sub.add_argument('--json', action='store_true')
	sub.set_defaults(func=plan)

	sub = commands.add_parser('apply', help='a plain search: write the '
		'change into the original files (preview without --yes)')
	sub.add_argument('work')
	sub.add_argument('--id', default='best')
	sub.add_argument('--to', help='the folder the change applies under, for a '
		'search that does not record where its files came from')
	sub.add_argument('--yes', action='store_true', help='write the files')
	sub.set_defaults(func=apply)

	sub = commands.add_parser('branch', help='a repository search: a branch '
		'in the original with one commit per kept change (preview without '
		'--yes)')
	sub.add_argument('work')
	sub.add_argument('--id', default='best')
	sub.add_argument('--name', help='the branch (default optimized-<date>)')
	sub.add_argument('--repo', help='the original repository, when the '
		'search does not record it')
	sub.add_argument('--yes', action='store_true', help='make the branch')
	sub.set_defaults(func=branch)

	args = parser.parse_args(argv)

	return args.func(args)


if __name__ == '__main__':
	sys.exit(main())
