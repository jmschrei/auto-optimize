"""What the scripts that write and publish a document share: `issue_body.py`,
`tests_pr_body.py`, `tests_pr_push.py`, `opt_pr_body.py`, `ship.py` and
`autoopt.py pr`. Not run on its own.

Writing a file whole or not at all, a body with its title beside it, the
title a body was last rendered with, the bug report's and the unit-test PR's
numbers filled into a rendered body, a refusal that lists every problem, a
`gh` call returning (exit code, stdout, stderr), filling a stand-in such as
`#<tests PR>` into documents, and the check that a repository belongs to the
account `gh` is logged in as. Standard library only.
"""

import os
import re
import sys
import json
import shutil
import hashlib
import subprocess

import attribution

# The folder of each document under `<work>/ship/`, and the record that holds
# its number once posted. The same table as `autoopt.SHIP_FOLDERS`, kept
# here so a renderer runs without importing the search's scripts.
FOLDERS = {'bugs': 'issue-bugs', 'tests': 'pr-tests',
	'optimization': 'pr-optimization'}
RECORDS = {'bugs': 'filed.json', 'tests': 'pr.json', 'optimization': 'pr.json'}

# The stand-in each posted document fills into the others.
STAND_INS = {'bugs': attribution.PLACEHOLDERS[0],
	'tests': attribution.PLACEHOLDERS[1]}


def series(items):
	"""`a`, `a and b`, `a, b and c`."""

	items = [str(item) for item in items]

	if len(items) < 2:
		return ''.join(items)

	return ', '.join(items[:-1]) + ' and ' + items[-1]


def cell(text):
	"""A Markdown table cell: a pipe would end it."""

	return text.replace('|', '\\|')


def table(header, rows, align=None):
	"""Markdown table lines. `align` is one delimiter cell per column
	(`---`, `---:`), `---` by default."""

	align = align or ['---'] * len(header)

	return ['| ' + ' | '.join(header) + ' |', '|' + '|'.join(align) + '|'] + [
		'| ' + ' | '.join(cell(str(value)) for value in row) + ' |'
		for row in rows]


def digest(title, body):
	"""What a document's title and body were when they were posted."""

	return hashlib.sha256((title + '\n' + body).encode('utf-8')).hexdigest()


def stored_title(out):
	"""The title `out`'s body was last rendered with (`<out stem>.title`), or
	None."""

	try:
		with open(os.path.splitext(out)[0] + '.title', encoding='utf-8') as handle:
			return handle.read().strip() or None
	except OSError:
		return None


def write_titled(out, title, text):
	"""The body at `out` and its title beside it, each whole or not at all."""

	write(out, text)
	write(os.path.splitext(out)[0] + '.title', title.strip() + '\n')


def sibling(directory, document):
	"""The record of `document` (`bugs`, `tests`) in the ship folder beside
	`directory`, the folder of another document."""

	return os.path.join(os.path.dirname(os.path.abspath(directory)),
		FOLDERS[document], RECORDS[document])


def chosen(directory):
	"""The documents chosen at step 5 (`after.ship_documents`), read from the
	search's `config.json` two folders above `directory`
	(`<work>/ship/<document>/`); every document when there is none, as for a
	document rendered on its own."""

	config = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(
		directory))), 'autooptimize', 'config.json')

	try:
		with open(config, encoding='utf-8') as handle:
			documents = (json.load(handle).get('after') or {}).get(
				'ship_documents')
	except (OSError, ValueError):
		documents = None

	return list(documents) if documents else list(FOLDERS)


def fill_numbers(text, directory, problems):
	"""`text` with `#<bug issue>` replaced by the number in the bug report's
	`filed.json` and `#<tests PR>` by the one in the unit-test PR's
	`pr.json`, in the folders beside `directory`, where those files exist. An
	unreadable record is added to `problems`."""

	for document, placeholder in STAND_INS.items():
		path = sibling(directory, document)

		if not os.path.exists(path):
			continue

		try:
			with open(path, encoding='utf-8') as handle:
				number = json.load(handle)['number']

			text = text.replace(placeholder, '#{}'.format(number))
		except (OSError, ValueError, KeyError, TypeError) as error:
			problems.add('{}: cannot read the number ({})'.format(path, error))

	return text


def write(path, text):
	"""Write `path` whole or not at all."""

	temporary = path + '.tmp'

	with open(temporary, 'w', encoding='utf-8', newline='\n') as out:
		out.write(text)

	os.replace(temporary, path)


def write_json(path, data):
	"""`data` as sorted, indented JSON with a final newline, whole or not at
	all; a missing folder is made."""

	os.makedirs(os.path.dirname(path), exist_ok=True)
	write(path, json.dumps(data, indent=1, sort_keys=True) + '\n')


def read(path):
	with open(path, encoding='utf-8') as handle:
		return handle.read()


def refuse(tool, problems, left='nothing done'):
	"""Print every problem on stderr; the exit code to return."""

	sys.stderr.write('{}: refused, {}:\n'.format(tool, left)
		+ ''.join('  ' + problem + '\n' for problem in problems))

	return 1


def split_nodeid(nodeid):
	"""(module, qualified function name) of a pytest node id, the
	parametrization dropped: `a/test_b.py::TestC::test_d[x-1]` gives
	(`a/test_b.py`, `TestC.test_d`)."""

	base = nodeid.split('[', 1)[0]
	parts = base.split('::')

	return parts[0], '.'.join(parts[1:])


def fail(prefix, message):
	"""Print `prefix: message` on stderr and exit 1."""

	sys.stderr.write('{}: {}\n'.format(prefix, message))
	raise SystemExit(1)


def run(command, cwd=None):
	"""(exit code, stdout, stderr) of a command, stdout and stderr stripped."""

	done = subprocess.run(command, capture_output=True, text=True, cwd=cwd,
		stdin=subprocess.DEVNULL)

	return done.returncode, done.stdout.strip(), done.stderr.strip()


def run_gh(arguments):
	return run(['gh'] + arguments)


def link_files(paths, placeholder, value):
	"""Replace `placeholder` with `value` in each file; the count in each."""

	counts = {}

	for path in paths:
		with open(path, encoding='utf-8') as handle:
			text = handle.read()

		counts[path] = text.count(placeholder)

		if counts[path]:
			write(path, text.replace(placeholder, value))

	return counts


def account_problem(repo, other_owner):
	"""Why `repo` (OWNER/REPO) may not be published to yet, or None: `gh` must
	answer, and an owner other than the logged-in account needs `other_owner`,
	set once the user has confirmed it."""

	if not shutil.which('gh'):
		return 'gh is not installed'

	code, login_name, err = run_gh(['api', 'user', '-q', '.login'])

	if code != 0:
		return 'gh api user failed: {}'.format(err)

	if repo.split('/')[0] != login_name and not other_owner:
		return ('{} is not {}\'s own account ({}); show the user, then pass '
			'--other-owner'.format(repo.split('/')[0], login_name, repo))

	return None


def login():
	"""The account `gh` is logged in as, or None (gh missing or not logged
	in). A read-only call."""

	if not shutil.which('gh'):
		return None

	code, out, _ = run_gh(['api', 'user', '-q', '.login'])

	return out if code == 0 and out else None


def can_push(repo):
	"""True, False, or None when unknown: whether the logged-in account may
	push to `repo` (OWNER/REPO), from GitHub's own answer. Read-only."""

	if not shutil.which('gh'):
		return None

	code, out, _ = run_gh(['api', 'repos/{}'.format(repo), '-q',
		'.permissions.push'])

	if code != 0 or out not in ('true', 'false'):
		return None

	return out == 'true'


def post_needs(owner_repo, push, fills=()):
	"""The lines a preview prints under `post needs:`, so the user sees what
	the real post will ask for: `--other-owner` when OWNER of `owner_repo`
	(OWNER/REPO, the repository the push lands on) is not the logged-in
	account, the push target (`push`, one line), and what it will fill (a
	list of `(placeholder, path, count)`)."""

	who = login()
	owner = owner_repo.split('/')[0]
	lines = ['post needs:']

	if who is None:
		lines.append('  --other-owner: not checked (gh is missing or not '
			'logged in); {} must be your own account or the post needs it'
			.format(owner))
	elif owner != who:
		lines.append('  --other-owner: yes, {} is not {}\'s account; confirm '
			'the repository with the user first'.format(owner, who))
	else:
		lines.append('  --other-owner: no, {} is your account'.format(owner))

	lines.append('  push: ' + push)

	for placeholder, path, count in fills:
		lines.append('  fills {} in {}: {} place{}'.format(placeholder, path,
			count, '' if count == 1 else 's'))

	if not fills:
		lines.append('  fills: no stand-in number in another document')

	return lines


def emit(tool, text, problems, title, out, dropped=(), unused=(), labels=None):
	"""The end of a body renderer: with `out`, the body and its title beside
	it (`title`, else the one stored there), otherwise the body on stdout.
	`problems` (with `.add`) are checked together with the title and, by
	`lint_text.py`, the vocabulary of the body and title; any problem
	writes nothing and returns 1. `dropped` and `unused` are noted on stderr."""

	title = title or (stored_title(out) if out else None)

	if out and not title:
		problems.add('--out needs --title: the body is not written without the '
			'title it will be opened with, and none is stored beside it')

	if title:
		for problem in attribution.title_problems(title):
			problems.add('title: ' + problem)

	import lint_text

	for problem in lint_text.problems(text, 'body', labels):
		problems.add(problem)

	if title:
		for problem in lint_text.problems(title, 'title', labels):
			problems.add(problem)

	if problems:
		return refuse(tool, problems, 'nothing written')

	for reason in dropped:
		sys.stderr.write('{}: dropped {}\n'.format(tool, reason))

	if unused:
		sys.stderr.write('{}: prose fields no rendered section uses: {}\n'.format(
			tool, ', '.join(unused)))

	if out:
		write_titled(out, title, text)

		sys.stderr.write('{}: wrote {}\n'.format(tool, out))
	else:
		sys.stdout.write(text)

	return 0


PULL_URL = re.compile(r'https://\S+/(?:pull|issues)/(\d+)')


def new_record(repo, url, branch, title, body, **more):
	"""The record of a document just posted: its repository, URL, number,
	branch, title, a digest of title and body, the time, and `more`."""

	import datetime

	found = PULL_URL.search(url)
	record = {'repo': repo, 'url': url, 'number': int(found.group(1)) if found
		else None, 'branch': branch, 'title': title, 'digest': digest(title,
		body), 'date': datetime.datetime.now(datetime.timezone.utc).strftime(
		'%Y-%m-%dT%H:%MZ')}
	record.update(more)
	return record


def write_record(path, record):
	write(path, json.dumps(record, indent=1) + '\n')


def edit_pull(path, record, title, body, body_path, other_owner):
	"""Replace the title and body of the PR `record` (the `pr.json`
	at `path`) describes, and record the new digest. Returns what went wrong,
	or None."""

	problem = account_problem(record['repo'], other_owner)

	if problem:
		return problem

	code, out, err = run_gh(['pr', 'edit', str(record['number']), '--repo',
		record['repo'], '--title', title, '--body-file', body_path])

	if code != 0:
		return 'gh pr edit failed ({}): {}{}'.format(code, out, err)

	record.update(title=title, digest=digest(title, body))
	write_record(path, record)
	return None
