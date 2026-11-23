"""The fixed parts of the documents step 5 publishes: the title rule, the
stand-in numbers, and the bug report's fixed sentences. Nothing is added
that names the tool or says how a document was written.

The optimization PR, the unit-test PR and the bug report are rendered by
`opt_pr_body.py`, `tests_pr_body.py` and `issue_body.py`, and opened by
`autoopt.py pr`, `tests_pr_push.py` and `issue_body.py file`. Each takes these
parts from here, so the wording is the same every time and a document that
breaks a rule is refused before it leaves the machine.

	python3 attribution.py check --kind optimization|tests|issue --title T --body FILE
"""

TITLE_LIMIT = 72

KINDS = ('optimization', 'tests', 'issue')

# How likely a user is to meet a bug and what it costs them, most severe
# first. The bug report's table divides the bugs by it, and its first
# paragraph says what the levels mean.
SEVERITIES = (
	('high', 'likely to be met in ordinary use, with wrong output or a crash'),
	('medium', 'needs unusual input, options or an environment, or costs time '
		'rather than correctness'),
	('low', 'an edge case, malformed input, a rarely used option, or a '
		'documentation mismatch'),
)
SEVERITY_KEY = ('Severity is our estimate of how likely a user is to meet the '
	'bug and what it costs them: {}.'.format('; '.join('**{}**, {}'.format(key,
	meaning) for key, meaning in SEVERITIES)))

# Fixed sentences that end a bug report's first paragraph: many of the bugs
# are edge cases, and what the severity levels mean.
ISSUE_NOTES = (
	'Many of the bugs below are likely edge cases that a user may never '
	'experience in practice, and the table after this paragraph divides them '
	'by severity.',
	SEVERITY_KEY)

# Stand-ins a draft carries for a number that does not exist yet: the bug
# report's and the unit-test PR's. `ship.py post` fills each once that
# document is posted; a document that still holds one is not ready to open.
PLACEHOLDERS = ('#<bug issue>', '#<tests PR>')

# Where an optimization PR's first paragraph points, and the section it
# points to.
CHECKED_HEADING = '## How the output was checked'
CHECKED_LINK = '(#how-the-output-was-checked)'


def plural(count):
	return '' if count == 1 else 's'


def placeholder_problems(text):
	"""One problem for each stand-in `text` still holds."""

	return ['it still holds {}, the number of a document not posted yet; '
		'`ship.py post` fills it'.format(held) for held in PLACEHOLDERS
		if held in text]


def title_problems(text):
	"""What is wrong with a title, as a list: it starts with a capital letter,
	does not end with a period, and fits in TITLE_LIMIT characters."""

	text = text.strip()
	problems = []

	if not text:
		problems.append('it is empty')
	elif not text[0].isupper():
		problems.append('it must start with a capital letter ("Vectorize '
			'column scoring", "Bugs in the clustering script")')

	if text.endswith('.'):
		problems.append('it must not end with a period')

	if len(text) > TITLE_LIMIT:
		problems.append('it is {} characters; the limit is {}'.format(len(text),
			TITLE_LIMIT))

	return problems


def first_paragraph(text):
	"""The first paragraph of `text`, its lines joined by spaces."""

	return ' '.join(text.strip().split('\n\n')[0].split())


def opening_problems(kind, text):
	"""What is wrong with how a `kind` document begins, as a list: a bug
	report's first paragraph ends with ISSUE_NOTES, and an optimization PR's
	links its *How the output was checked* section, which exists."""

	problems = []
	paragraph = first_paragraph(text)

	if not paragraph:
		return ['the body is empty']

	if kind == 'issue':
		missing = [note for note in ISSUE_NOTES if note not in paragraph]

		if missing:
			problems.append('the first paragraph must hold the fixed '
				'sentence(s): "{}"'.format('" "'.join(missing)))

	if kind == 'optimization':
		if CHECKED_LINK not in paragraph:
			problems.append('the first paragraph must link {}'.format(
				CHECKED_LINK.strip('()')))

		if CHECKED_HEADING not in text.splitlines():
			problems.append('the body has no "{}" section'.format(
				CHECKED_HEADING))

	return problems


def problems(kind, title, text):
	"""Every problem with a `kind` document's title and body."""

	return (['title: ' + p for p in title_problems(title)] + ['body: ' + p
		for p in opening_problems(kind, text) + placeholder_problems(text)])


def main(argv=None):
	import argparse
	import sys

	parser = argparse.ArgumentParser(description='Check a document step 5 '
		'publishes: exits 1 and lists every problem with its title or body.')
	sub = parser.add_subparsers(dest='command', required=True)
	check = sub.add_parser('check', help='check a title and a body file')
	check.add_argument('--kind', required=True, choices=KINDS)
	check.add_argument('--title', required=True)
	check.add_argument('--body', required=True, help='the body file')
	args = parser.parse_args(argv)

	with open(args.body, encoding='utf-8') as infile:
		text = infile.read()

	found = problems(args.kind, args.title, text)

	if found:
		sys.stderr.write('refused:\n' + ''.join('  ' + p + '\n' for p in found))
		return 1

	print('ok: {}'.format(args.title.strip()))
	return 0


if __name__ == '__main__':
	raise SystemExit(main())
