#!/usr/bin/env python3
"""The bug report (a GitHub issue) on the original's repository, rendered
from structured inputs.

	python3 issue_body.py template DIR > DIR/prose.md
	python3 issue_body.py status DIR [--bin chromap=PATH]
	python3 issue_body.py verify DIR --bin chromap=PATH [--bug N ... | --stale] [--commit REF] [--workdir DIR]
	python3 issue_body.py verify DIR --bin chromap=PATH --manual N --reason TEXT --log FILE
	python3 issue_body.py dupes DIR (--repo OWNER/REPO | --issues FILE)
	python3 issue_body.py file DIR --repo OWNER/REPO [--post [--other-owner]]
	python3 issue_body.py link DIR FILE... [--placeholder P] [--value #N]
	python3 issue_body.py renumber DIR [--only N,N,... | --drop N,N,...] [--dry-run]
	python3 issue_body.py [render] DIR --title 'Bugs found while testing <tool> <commit>' --out DIR/body.md

DIR is `$WORK/ship/issue-bugs`. Its inputs come from step 1, which leaves one
folder per bug in `$WORK/tests/work/bugs/<short-name>/` (what the code does,
what it should do, a reproducing command, the tests removed); a writer agent
turns those into the first two files here:

	bugs.tsv    one row per bug, tab-separated, with the header
	            number slug title short category severity effect related
	prose.md    fields that each start with a line `=== <field>`: opening,
	            intro, reading, setup.context, setup, and per bug
	            <n>.what, <n>.expected, <n>.cause, <n>.context,
	            <n>.reproduction, <n>.output, <n>.varies
	verify.tsv  written by `verify`: one row per bug, the reproduction's
	            result on one binary, by checksum

`render` (the default) computes the summary table, divided by each bug's
severity, the category sections in the fixed category order, every heading,
the collapsed blocks and the fences; prose fills them. The body is the first
paragraph (the `opening` sentence, then attribution.py's fixed sentences:
many bugs are edge cases, what the severity levels mean, and that Claude
wrote the report), the severity table, which shows all three levels and says
`none` for one without a bug, the introduction, the shared setup, the bugs,
and attribution.py's closing line. The title goes to `<out stem>.title`. The
same inputs give the same body, byte for byte. A body over --limit characters
(62,000) has its longest reproductions moved to `<out stem>.comment-1.md`,
then `-2.md` and on when one comment is not enough (packed in bug order, none
split), each ending with the closing line, posted as the issue's comments in
that order, and stderr says so. Exit 1, every problem listed and nothing
written, on a missing or unknown field, an unknown category, a bug numbered
out of the category order, an unknown severity, blunt wording in the prose, a
bug without a passing or manual verify row for the text and binary shown,
verify rows from different binaries, a <<FILL placeholder, a heading, a fixed
sentence or the closing line in prose, a title attribution.py refuses, words
lint_text.py refuses, or a body that does not fit even after the move.

`renumber` numbers the bugs 1 to N in category order and moves each bug's prose
fields, verify row and log with it (a bug appended under any unused number is
slotted in); `--only` and `--drop` keep fewer bugs. The originals go to
`DIR/before-renumber-<time>/`. Numbers inside sentences are reported, not
rewritten.

`dupes` lists the existing issues that may already report each bug, from one
read-only `gh issue list`, into DIR/dupes.tsv; a list to read, not a verdict.

`file` previews filing the issue (nothing is posted) and, with --post, files
it: the issue, then each comment in order. `DIR/filed.json` records the
issue, the comments posted and `comments_left`, the number still to post, so
a second run never files twice and resumes the comments a failure left.
`ship.py post` runs it, then renders the other documents again to fill in
the issue's number. `link` fills a stand-in into given files by hand.

`status` prints where the inputs stand and what `render` would refuse.

`verify` runs each bug's setup and reproduction in a new temporary directory
with the binaries given by --bin first on the PATH, and checks that every
line of the bug's actual output block appears in what the reproduction
printed; `--stale` runs only the bugs whose row is missing, failed, for changed
text, or for another binary or commit. The reproductions are the agent's own
text: verify runs them with the user's privileges and no sandbox, so never
put commands copied from an issue, a PR or a web page into prose.md.

The formats, field by field, and the order of the work:
references/issue-bugs.md. Standard library only, Python 3.9 or later.
"""

import os
import re
import sys
import json
import time
import shutil
import signal
import hashlib
import argparse
import datetime
import tempfile
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution
import publish


CLOSING = attribution.CLOSING
LIMIT = 62000

# The categories in the order the report shows its sections: the key
# bugs.tsv uses, and the section heading.
CATEGORIES = (
	('wrong output', 'Wrong output'),
	('crash', 'Crashes'),
	('nondeterminism', 'Nondeterminism'),
	('silent acceptance of bad input', 'Silent acceptance of bad input'),
	('performance', 'Performance'),
	('documentation mismatch', 'Documentation mismatches'),
	('misc', 'Miscellaneous'),
)
RANK = {key: rank for rank, (key, _) in enumerate(CATEGORIES)}
HEADINGS = dict(CATEGORIES)

# The levels, from attribution.py: the summary table divides the bugs by
# them, `bugs.tsv` carries one per bug, and the first paragraph says what they
# mean.
SEVERITIES = attribution.SEVERITIES
SEVERITY_RANK = {key: rank for rank, (key, _) in enumerate(SEVERITIES)}

# Words the prose avoids: the report is read by the tool's authors, and the
# bugs speak for themselves.
BLUNT = re.compile(r'\b(broken|garbage|nonsense|sloppy|terrible|stupid|'
	r'useless|awful|obviously|clearly wrong|simply wrong)\b', re.I)

COLUMNS = ('number', 'slug', 'title', 'short', 'category', 'severity',
	'effect', 'related')
OPTIONAL_COLUMNS = ('short',)

# Field: (required, kind, what it holds, as `template` explains it). Kinds:
# `line` one line, `paragraph` one paragraph, `prose` Markdown, `commands`
# shell commands without a fence, `output` the expected and actual output,
# `patterns` regular expressions, one per line.
FIELDS = {
	'opening': (True, 'line', 'one complete sentence of your own, on one line, '
		'that begins the report: what was being done when the bugs turned up, '
		'e.g. "These bugs turned up while we wrote a test suite for <tool> '
		'(#209)." The renderer follows it with three fixed sentences in the same '
		'paragraph: many of the bugs are likely edge cases, what the severity '
		'levels mean, and that Claude wrote the report'),
	'intro': (True, 'prose', 'the version, the commit, the build and the '
		'platform the bugs were reproduced on; which bugs need a special build '
		'or harness; which bugs came from which part of the work'),
	'reading': (False, 'paragraph', 'optional: how to read the report, '
		'appended to the intro\'s last paragraph. Delete the field to use the '
		'default sentence'),
	'setup.context': (False, 'prose', 'optional: what the shared setup needs '
		'and makes, shown before its commands, e.g. "In an empty directory, '
		'with `tool` and `python3` on the PATH."'),
	'setup': (False, 'commands', 'optional: the shell commands every '
		'reproduction starts from, without a code fence; verify runs them '
		'before each bug'),
}
BUG_FIELDS = {
	'what': (True, 'paragraph', 'what happens, with the numbers the '
		'reproduction shows, in neutral words; it may end with a semicolon, '
		'colon or comma and run on into `expected`'),
	'expected': (True, 'paragraph', 'what should happen, and where that '
		'expectation comes from (the README, the manual page, `-h`, a '
		'specification, the code\'s own comments)'),
	'cause': (True, 'paragraph', 'the code responsible, with file:line, and '
		'a fix, as suggestions, hedged ("appears to", "we believe", "would '
		'fix it")'),
	'context': (False, 'prose', 'optional: what the reproduction needs beyond '
		'the setup, or where it runs, shown before its commands'),
	'reproduction': (True, 'commands', 'the shell commands, without a code '
		'fence'),
	'output': (True, 'output', 'a paragraph starting "Expected:", then a '
		'paragraph starting "Actual" and a fenced block of the actual output, '
		'cut to the lines that show the bug; a line `...` marks a cut. Other '
		'paragraphs and blocks may come before or after'),
	'varies': (False, 'patterns', 'optional: one regular expression per line '
		'for values that change from run to run (timings); verify masks them in '
		'the actual block and in what the reproduction printed before comparing'),
}
COMMAND_KINDS = ('commands', 'patterns')

VERIFY_COLUMNS = ('number', 'slug', 'status', 'exit', 'seconds', 'matched',
	'checksum', 'commit', 'text', 'log', 'note', 'date')

SETUP_SUMMARY = 'Setup used by the reproductions'
READING = ('Each section below says what happens, what should happen and '
	'which code we believe is responsible, and its collapsed block holds a '
	'reproduction with the expected and actual output')
READING_SETUP = ('; the reproductions run in a directory prepared by the setup '
	'block after the table, unless their section says otherwise')
MANUAL_NOTE = ('Not rerun before filing, unlike the other reproductions: {}; '
	'the output shown is from an earlier run.')
MOVED_NOTE = ('The reproduction is in {}, moved there to keep this body under '
	'GitHub\'s length limit.')
ORDINALS = ('first', 'second', 'third', 'fourth', 'fifth', 'sixth', 'seventh',
	'eighth', 'ninth', 'tenth')
COMMENT_HEAD = ('Reproductions of {}, moved here from the issue body to keep it '
	'under GitHub\'s length limit.')

FIELD = re.compile(r'^=== (\S+)[ \t]*$')
BUG_FIELD = re.compile(r'^(\d+)\.([a-z]+)$')
FENCE = re.compile(r'^\s{0,3}(```+|~~~+)(.*)$')
HEADING = re.compile(r'^\s{0,3}#{1,6}(\s|$)')
LIST_ITEM = re.compile(r'^\s*([-*+]|\d+[.)])\s')
SLUG = re.compile(r'^[a-z0-9][a-z0-9-]*$')
REFERENCE = re.compile(r'#\d+|https?://')
ELISION = ('...', '…')


class Problems(list):
	def add(self, text):
		if text not in self:
			self.append(text)


# Formatting ------------------------------------------------------------------

def bugs_named(numbers):
	numbers = list(numbers)
	return '{} {}'.format('bug' if len(numbers) == 1 else 'bugs',
		publish.series(numbers))


def trim(lines):
	"""A field's text: trailing spaces and the blank lines around it removed,
	indentation kept."""

	lines = [line.rstrip() for line in lines]

	while lines and not lines[0]:
		lines.pop(0)
	while lines and not lines[-1]:
		lines.pop()

	return '\n'.join(lines)


def blocks(text):
	"""Markdown text as a list of ('para', lines) and ('fence', info, lines,
	opening line, closing line or None when unclosed), in order."""

	out, para, fence = [], [], None

	for line in text.split('\n'):
		if fence is not None:
			marker = FENCE.match(line)
			if marker and marker.group(1)[0] == fence[0][0] \
					and len(marker.group(1)) >= len(fence[0]) \
					and not marker.group(2).strip():
				out.append(('fence', fence[1], fence[2], fence[3], line))
				fence = None
			else:
				fence[2].append(line)
			continue

		marker = FENCE.match(line)
		if marker:
			if para:
				out.append(('para', para))
				para = []
			fence = (marker.group(1), marker.group(2).strip(), [], line)
			continue

		if not line.strip():
			if para:
				out.append(('para', para))
				para = []
			continue

		para.append(line)

	if fence is not None:
		out.append(('fence', fence[1], fence[2], fence[3], None))
	if para:
		out.append(('para', para))

	return out


def unwrap_lines(lines):
	"""A paragraph's lines joined: GitHub renders a line break inside an
	issue's paragraph as a break. List items and table rows keep their lines."""

	out = []

	for line in lines:
		stripped = line.strip()
		previous = out[-1].lstrip() if out else ''

		if not out or stripped.startswith('|') or LIST_ITEM.match(line) \
				or previous.startswith('|'):
			out.append(line.rstrip())
		else:
			out[-1] = out[-1].rstrip() + ' ' + stripped

	return out


def unwrap(text):
	"""Markdown with each paragraph on one line, fenced blocks kept as they
	are, one blank line between blocks."""

	parts = []

	for block in blocks(text):
		if block[0] == 'para':
			parts.append('\n'.join(unwrap_lines(block[1])))
		else:
			_, _, lines, opening, closing = block
			parts.append('\n'.join([opening] + lines + [closing]))

	return '\n\n'.join(parts)


def prose_lines(text):
	"""The lines of Markdown text outside fenced blocks."""

	return [line for block in blocks(text) if block[0] == 'para'
		for line in block[1]]


def fenced(commands, info='bash'):
	return '```{}\n{}\n```'.format(info, commands)


# Reading the inputs ----------------------------------------------------------

def read_text(path, problems, what):
	try:
		with open(path, encoding='utf-8') as handle:
			return handle.read()
	except OSError as error:
		problems.add('{}: cannot read {} ({})'.format(what, path, error.strerror))
		return None


def read_fields(path, problems):
	"""The `=== <field>` fields of a prose file, in file order."""

	text = read_text(path, problems, 'prose')
	if text is None:
		return {}

	fields, name = {}, None

	for number, line in enumerate(text.split('\n'), 1):
		match = FIELD.match(line)

		if match:
			name = match.group(1)
			if name in fields:
				problems.add('prose: "=== {}" appears twice'.format(name))
			fields[name] = []
		elif name is not None:
			fields[name].append(line)
		elif line.strip():
			problems.add('prose: line {} comes before the first "=== <field>" '
				'line'.format(number))

	return {key: trim(value) for key, value in fields.items()}


def split_effect(effect):
	"""The terms of an effect cell, split at semicolons outside parentheses,
	each without its parenthesized qualifier."""

	terms, depth, current = [], 0, ''

	for char in effect:
		depth += (char == '(') - (char == ')')
		if char == ';' and depth == 0:
			terms.append(current)
			current = ''
		else:
			current += char

	terms.append(current)
	return [re.sub(r'\s*\(.*\)\s*$', '', term).strip() for term in terms]


def check_row(bug, problems):
	where = 'bug {}'.format(bug['number'])

	if not SLUG.match(bug['slug']):
		problems.add('{}: the slug {!r} must be lower-case letters, digits and '
			'hyphens'.format(where, bug['slug']))

	for column in ('title', 'short'):
		value = bug.get(column, '-')
		if not value:
			problems.add('{}: the {} is empty{}'.format(where, column,
				' (- uses the title)' if column == 'short' else ''))
		elif value != '-' and value.endswith('.'):
			problems.add('{}: the {} must not end with a period'.format(where,
				column))
		elif re.match(r'^\d+\.\s', value):
			problems.add('{}: the {} must not start with a number; the renderer '
				'numbers the bugs'.format(where, column))

	if bug.get('severity') not in SEVERITY_RANK:
		problems.add('{}: unknown severity {!r}; the severities are {}'.format(
			where, bug.get('severity'), ', '.join(key for key, _ in
			SEVERITIES)))

	if bug['category'] not in RANK:
		problems.add('{}: unknown category {!r}; the categories are {}'.format(
			where, bug['category'], ', '.join(key for key, _ in CATEGORIES)))
	else:
		terms = split_effect(bug['effect'])
		odd = [term for term in terms if term not in RANK]
		if odd:
			problems.add('{}: the effect lists {}, which {} not a category; list '
				'categories, each with an optional qualifier in parentheses, '
				'separated by semicolons'.format(where, ', '.join(repr(term)
				for term in odd), 'is' if len(odd) == 1 else 'are'))
		elif terms[0] != bug['category']:
			problems.add('{}: the effect must start with the bug\'s own category, '
				'{!r}'.format(where, bug['category']))

	if bug['related'] != '-' and not REFERENCE.search(bug['related']):
		problems.add('{}: related must be - or name an issue (#N) or a URL; it '
			'is the text after "Related: "'.format(where))

	for column, value in bug.items():
		if '<<FILL' in str(value):
			problems.add('{}: the {} holds a <<FILL placeholder'.format(where,
				column))


def read_bugs(path, problems, strict=True):
	"""bugs.tsv's rows, checked, in number order. Not strict, the numbers may
	have gaps and be out of category order (`renumber` fixes both), but each
	is still used once."""

	text = read_text(path, problems, 'bugs.tsv')
	if text is None:
		return []

	lines = text.rstrip('\n').split('\n')
	header = [name.strip() for name in lines[0].split('\t')]
	unknown = [name for name in header if name not in COLUMNS]
	missing = [name for name in COLUMNS if name not in header
		and name not in OPTIONAL_COLUMNS]

	if unknown or missing:
		problems.add('bugs.tsv: the header must be the columns {} (short is '
			'optional); {}'.format(' '.join(COLUMNS), '; '.join(filter(None, [
			'unknown: ' + ', '.join(unknown) if unknown else '',
			'missing: ' + ', '.join(missing) if missing else '']))))
		return []

	bugs = []

	for row_number, line in enumerate(lines[1:], 2):
		cells = line.split('\t')

		if len(cells) != len(header):
			problems.add('bugs.tsv line {}: {} cells, the header has {}'.format(
				row_number, len(cells), len(header)))
			continue

		row = dict(zip(header, (value.strip() for value in cells)))

		if not row['number'].isdigit() or int(row['number']) < 1:
			problems.add('bugs.tsv line {}: the number {!r} is not a whole number '
				'from 1'.format(row_number, row['number']))
			continue

		bug = dict(row, number=int(row['number']))
		check_row(bug, problems)
		if bug.get('short', '-') == '-':
			bug['short'] = bug['title']
		bugs.append(bug)

	numbers = sorted(bug['number'] for bug in bugs)
	if not bugs:
		problems.add('bugs.tsv: no bugs')
	elif strict and numbers != list(range(1, len(bugs) + 1)):
		problems.add('bugs.tsv: the numbers must run from 1 to {} once each; they '
			'are {}'.format(len(bugs), ', '.join(str(n) for n in numbers)))
	elif not strict:
		for number in sorted(set(n for n in numbers if numbers.count(n) > 1)):
			problems.add('bugs.tsv: the number {} is used twice'.format(number))

	slugs = [bug['slug'] for bug in bugs]
	for slug in sorted(set(s for s in slugs if slugs.count(s) > 1)):
		problems.add('bugs.tsv: the slug {!r} is used twice'.format(slug))

	bugs.sort(key=lambda bug: bug['number'])
	ranked = [bug for bug in bugs if bug['category'] in RANK]
	for before, after in zip(ranked, ranked[1:] if strict else []):
		if RANK[before['category']] > RANK[after['category']]:
			problems.add('bugs.tsv: number the bugs in the category order ({}): '
				'bug {} ({}) comes before bug {} ({})'.format(', '.join(key
				for key, _ in CATEGORIES), before['number'], before['category'],
				after['number'], after['category']))
			break

	return bugs


def actual_block(output):
	"""The lines of the fenced block after the `Actual` paragraph of a bug's
	output, or None when the output does not have the required shape."""

	items = blocks(output)
	expected = next((i for i, block in enumerate(items) if block[0] == 'para'
		and block[1][0].startswith('Expected')), None)
	if expected is None:
		return None

	actual = next((i for i, block in enumerate(items) if i > expected
		and block[0] == 'para' and block[1][0].startswith('Actual')), None)
	if actual is None:
		return None

	fence = next((block for block in items[actual + 1:] if block[0] == 'fence'),
		None)
	if fence is None or fence[1] not in ('', 'text', 'console'):
		return None

	return fence[2]


def text_hash(fields, number):
	"""What verify ran and compared, as a short hash: the setup, the
	reproduction, the actual block and the masks."""

	block = actual_block(fields.get('{}.output'.format(number), '')) or []
	text = json.dumps([fields.get('setup', ''), fields.get('{}.reproduction'.format(
		number), ''), block, fields.get('{}.varies'.format(number), '')])
	return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]


def check_field(name, kind, text, problems):
	where = 'prose: "=== {}"'.format(name)

	if '<<FILL' in text:
		problems.add('{} still holds a <<FILL placeholder'.format(where))
	if CLOSING in text:
		problems.add('{} holds the closing line, which the renderer '
			'adds'.format(where))

	joined = ' '.join(text.split())

	for note in attribution.ISSUE_NOTES:
		if note in joined:
			problems.add('{} holds the fixed sentence "{}", which the renderer '
				'writes after the opening'.format(where, note[:60] + '...'))

	if kind in COMMAND_KINDS:
		if any(FENCE.match(line) for line in text.split('\n')):
			problems.add('{} holds a code fence; it is commands only, and the '
				'renderer adds the fence'.format(where))
		if kind == 'patterns':
			for line in text.split('\n'):
				try:
					re.compile(line)
				except re.error as error:
					problems.add('{}: {!r} is not a regular expression ({})'.format(
						where, line, error))
		return

	for line in prose_lines(text):
		if kind in ('line', 'prose', 'paragraph') and BLUNT.search(line):
			problems.add('{} uses blunt wording ({!r}); describe what happens '
				'and what was expected, and hedge the cause ("appears to", '
				'"we believe", "would")'.format(where,
				BLUNT.search(line).group(0)))
		if HEADING.match(line):
			problems.add('{} holds a heading ({!r}); the renderer writes every '
				'heading'.format(where, line.strip()))
		if re.search(r'\b(TODO|TBD|FIXME)\b', line):
			problems.add('{} holds a placeholder word ({!r})'.format(where,
				line.strip()))

	if any(block[0] == 'fence' and block[4] is None for block in blocks(text)):
		problems.add('{} has a code fence that is never closed'.format(where))

	if kind == 'line' and '\n' in text:
		problems.add('{} must be one line'.format(where))
	elif name == 'opening':
		if not re.match(r'^[`"*_(]*[A-Z0-9]', text):
			problems.add('{} must be a complete sentence that starts with a '
				'capital letter ("These bugs turned up while ...")'.format(where))
		if not re.search(r'[.!?][)"\'`]*$', text):
			problems.add('{} must be a complete sentence that ends with a '
				'period'.format(where))

	if kind == 'paragraph':
		items = blocks(text)
		if len(items) != 1 or items[0][0] != 'para' \
				or any(LIST_ITEM.match(line) for line in items[0][1]):
			problems.add('{} must be one paragraph, without lists or code '
				'blocks: it is joined with the bug\'s other sentences'.format(where))
		elif name.endswith('.what') and not re.search(r'[.;:,!?][)"\'`]*$', text):
			problems.add('{} must end with a period, or with a semicolon, '
				'colon or comma that runs on into `expected`'.format(where))
		elif name.endswith(('.expected', '.cause')) \
				and not re.search(r'[.!?][)"\'`]*$', text):
			problems.add('{} must end with a period'.format(where))

	if kind == 'output' and actual_block(text) is None:
		problems.add('{} needs a paragraph starting "Expected", then a paragraph '
			'starting "Actual" followed by a fenced block of the actual '
			'output'.format(where))


def check_prose(bugs, fields, problems):
	numbers = {bug['number'] for bug in bugs}

	for name, text in fields.items():
		match = BUG_FIELD.match(name)

		if name in FIELDS:
			kind = FIELDS[name][1]
		elif match and match.group(2) in BUG_FIELDS:
			kind = BUG_FIELDS[match.group(2)][1]
			if int(match.group(1)) not in numbers:
				problems.add('prose: "=== {}" is for bug {}, which bugs.tsv does '
					'not have'.format(name, match.group(1)))
		else:
			problems.add('prose: no field is called {!r}; the fields are {}, and '
				'per bug <n>.{}'.format(name, ', '.join(FIELDS),
				', <n>.'.join(BUG_FIELDS)))
			continue

		check_field(name, kind, text, problems)

	for name, (required, _, _) in FIELDS.items():
		if required and not fields.get(name):
			problems.add('prose: "=== {}" is missing or empty'.format(name))
		elif name in fields and not fields[name]:
			problems.add('prose: "=== {}" is empty; fill it or delete it'.format(
				name))

	for bug in bugs:
		for key, (required, _, _) in BUG_FIELDS.items():
			name = '{}.{}'.format(bug['number'], key)
			if required and not fields.get(name):
				problems.add('prose: "=== {}" is missing or empty'.format(name))
			elif name in fields and not fields[name]:
				problems.add('prose: "=== {}" is empty; fill it or delete '
					'it'.format(name))

	if fields.get('setup.context') and not fields.get('setup'):
		problems.add('prose: "=== setup.context" without "=== setup"')


# verify.tsv ------------------------------------------------------------------

def read_verify(path, problems):
	"""verify.tsv's rows by bug number; {} when there is no file."""

	if not os.path.exists(path):
		return {}

	text = read_text(path, problems, 'verify.tsv') or ''
	lines = text.rstrip('\n').split('\n')

	if tuple(lines[0].split('\t')) != VERIFY_COLUMNS:
		problems.add('verify.tsv: the header is not {}; it is written by '
			'`issue_body.py verify`'.format(' '.join(VERIFY_COLUMNS)))
		return {}

	rows = {}
	for line in lines[1:]:
		row = dict(zip(VERIFY_COLUMNS, line.split('\t')))
		if row.get('number', '').isdigit() and len(row) == len(VERIFY_COLUMNS):
			rows[int(row['number'])] = row
		else:
			problems.add('verify.tsv: a malformed row ({!r})'.format(line[:60]))

	return rows


def write_verify(path, rows):
	temporary = path + '.tmp'

	with open(temporary, 'w', encoding='utf-8', newline='\n') as out:
		out.write('\t'.join(VERIFY_COLUMNS) + '\n')
		for number in sorted(rows):
			out.write('\t'.join(rows[number].get(column, '-')
				for column in VERIFY_COLUMNS) + '\n')

	os.replace(temporary, path)


def tsv_cell(text):
	return re.sub(r'[\t\r\n]+', ' ', text).strip() or '-'


def stale_reason(row, fields, number, checksum, commit):
	"""Why a bug's verify row no longer counts, or None: there is no row, it
	did not pass (a manual row counts), the text it ran has changed, or it ran
	another binary or commit than the ones now given."""

	if row is None:
		return 'never verified'
	if row['text'] != text_hash(fields, number):
		return 'the setup, reproduction, output or masks changed'
	if row['status'] not in ('pass', 'manual'):
		return 'it did not pass ({})'.format(row['status'])
	if checksum and row['checksum'] != checksum:
		return 'it ran another binary'
	if commit and row['commit'] not in ('-', commit):
		return 'it ran commit {}'.format(row['commit'])

	return None


def check_verify(directory, bugs, fields, rows, binary, problems):
	"""Every bug needs a pass or a justified manual row, for the text shown
	now, and every row the same binary."""

	if not rows:
		problems.add('verify.tsv: missing or empty; run `issue_body.py verify '
			'DIR --bin NAME=PATH` on the base first')
		return

	checksums, commits = {}, set()

	for bug in bugs:
		number = bug['number']
		row = rows.get(number)
		where = 'bug {}'.format(number)

		if row is None:
			problems.add('{}: no verify row; run `issue_body.py verify DIR --bin '
				'NAME=PATH --bug {}`'.format(where, number))
			continue

		checksums.setdefault(row['checksum'], []).append(number)
		commits.add(row['commit'])

		if row['status'] == 'manual':
			log = row['log'] if os.path.isabs(row['log']) else os.path.join(
				directory, row['log'])
			if row['note'] in ('', '-'):
				problems.add('{}: marked manual without a reason'.format(where))
			if not os.path.exists(log):
				problems.add('{}: marked manual, but its log {} does not '
					'exist'.format(where, row['log']))
		elif row['status'] != 'pass':
			problems.add('{}: verify gave {} ({}); fix the reproduction or the '
				'output shown, then run verify again'.format(where, row['status'],
				row['note']))

		if row['text'] != text_hash(fields, number):
			problems.add('{}: the setup, the reproduction, the actual output or '
				'the masks changed since verify ran; run it again{}'.format(where,
				' (or mark it manual again)' if row['status'] == 'manual' else ''))

	if len(checksums) > 1:
		problems.add('verify.tsv: the rows come from different binaries ({}); '
			'verify every bug on the one the report names'.format('; '.join(
			'{} for {}'.format(checksum[:12], bugs_named(numbers))
			for checksum, numbers in sorted(checksums.items()))))
	elif binary and checksums and list(checksums)[0] != binary:
		problems.add('verify.tsv: the rows are for binary {}, not the one --bin '
			'names ({})'.format(list(checksums)[0][:12], binary[:12]))

	commits.discard('-')
	if len(commits) > 1:
		problems.add('verify.tsv: the rows name different commits ({})'.format(
			', '.join(sorted(commits))))
	elif commits and list(commits)[0] not in fields.get('intro', ''):
		problems.add('prose: "=== intro" does not name commit {}, which verify '
			'ran'.format(list(commits)[0]))


# Rendering -------------------------------------------------------------------

def bug_paragraph(bug, fields):
	parts = [' '.join(unwrap_lines(fields['{}.{}'.format(bug['number'],
		key)].split('\n'))) for key in ('what', 'expected', 'cause')]
	related = bug['related']

	if related != '-':
		parts.append('Related: {}{}'.format(related, '' if related.endswith('.')
			else '.'))

	return ' '.join(parts)


def reproduction(bug, fields, rows):
	number = bug['number']
	parts = []
	row = rows.get(number, {})

	if row.get('status') == 'manual':
		parts.append(MANUAL_NOTE.format(row['note'].rstrip('.')))

	if fields.get('{}.context'.format(number)):
		parts.append(unwrap(fields['{}.context'.format(number)]))

	parts.append(fenced(fields['{}.reproduction'.format(number)]))
	parts.append(unwrap(fields['{}.output'.format(number)]))

	return '<details><summary>Reproduction</summary>\n\n{}\n\n</details>'.format(
		'\n\n'.join(parts))


def table(bugs):
	"""One table, divided by severity: every level in order, high first, with
	its bugs in number order and its name on its first row, or the word
	`none` when it has no bug."""

	lines = ['| Severity | # | Bug | Category | Effect |',
		'|---|---:|---|---|---|']

	for level, _ in SEVERITIES:
		mine = sorted((bug for bug in bugs if bug['severity'] == level),
			key=lambda bug: bug['number'])

		if not mine:
			lines.append('| **{}** |  | none |  |  |'.format(level))

		for index, bug in enumerate(mine):
			lines.append('| {} | {} | {} | {} | {} |'.format(
				'**{}**'.format(level) if index == 0 else '', bug['number'],
				publish.cell(bug['short']), publish.cell(bug['category']),
				publish.cell(bug['effect'])))

	return '\n'.join(lines)


def introduction(fields):
	reading = fields.get('reading') or READING + (READING_SETUP
		if fields.get('setup') else '') + '.'
	reading = ' '.join(unwrap_lines(reading.split('\n')))
	intro = unwrap(fields['intro'])
	items = blocks(intro)

	if items and items[-1][0] == 'para' and not any(LIST_ITEM.match(line)
			or line.lstrip().startswith('|') for line in items[-1][1]):
		return intro + ' ' + reading

	return intro + '\n\n' + reading


def opening_paragraph(fields):
	"""The first paragraph: the writer's `opening` sentence, then the fixed
	sentences of attribution.ISSUE_NOTES."""

	return ' '.join([fields['opening'].strip()] + list(attribution.ISSUE_NOTES))


def comment_name(index):
	"""How the body names comment number `index` (from 1)."""

	return 'the {} comment'.format(ORDINALS[index - 1]) if index <= len(
		ORDINALS) else 'comment {}'.format(index)


def pack(numbers, sections, titles, limit):
	"""The bugs `numbers` split into comments, in number order: each comment
	takes bugs until the next would pass `limit`. A bug whose section alone is
	over the limit gets a comment of its own."""

	groups = []

	for number in sorted(numbers):
		if groups and len(comment_text(groups[-1] + [number], sections,
				titles)) <= limit:
			groups[-1].append(number)
		else:
			groups.append([number])

	return groups


def comment_text(numbers, sections, titles):
	return '\n\n'.join([COMMENT_HEAD.format(bugs_named(numbers))]
		+ ['### {}. {}\n\n{}'.format(number, titles[number], sections[number])
		for number in numbers] + [CLOSING]) + '\n'


def render(bugs, fields, rows, limit):
	"""The body, and the comments the moved reproductions fill, as a list of
	(the numbers of the bugs in it, its text)."""

	head = [opening_paragraph(fields), table(bugs), introduction(fields)]

	if fields.get('setup'):
		setup = [unwrap(fields['setup.context'])] if fields.get('setup.context') \
			else []
		setup.append(fenced(fields['setup']))
		head.append('<details><summary>{}</summary>\n\n{}\n\n</details>'.format(
			SETUP_SUMMARY, '\n\n'.join(setup)))

	sections = {bug['number']: reproduction(bug, fields, rows) for bug in bugs}
	titles = {bug['number']: bug['title'] for bug in bugs}

	def assemble(groups):
		where = {number: index for index, group in enumerate(groups, 1)
			for number in group}
		parts = list(head)
		category = None

		for bug in bugs:
			if bug['category'] != category:
				category = bug['category']
				parts.append('## ' + HEADINGS[category])

			parts.append('### {}. {}'.format(bug['number'], bug['title']))
			parts.append(bug_paragraph(bug, fields))
			parts.append(MOVED_NOTE.format(comment_name(where[bug['number']]))
				if bug['number'] in where else sections[bug['number']])

		parts.append(CLOSING)
		return '\n\n'.join(parts) + '\n'

	moved, groups = [], []
	body = assemble(groups)
	longest = sorted(sections, key=lambda number: (-len(sections[number]), number))

	while len(body) > limit and len(moved) < len(longest):
		moved.append(longest[len(moved)])
		groups = pack(moved, sections, titles, limit)
		body = assemble(groups)

	return body, [(group, comment_text(group, sections, titles))
		for group in groups]


write = publish.write


def refuse(problems, left='nothing written'):
	return publish.refuse('issue_body', problems, left)


NO_TITLE = ('--out needs --title: the body is not written without the title '
	'it will be opened with, and none is stored beside it')


def render_command(args):
	if not args.dir:
		return refuse(['name the input directory: issue_body.py render DIR '
			'--title TITLE --out DIR/body.md'])

	problems = Problems()
	fields = read_fields(args.prose or os.path.join(args.dir, 'prose.md'),
		problems)
	bugs = read_bugs(os.path.join(args.dir, 'bugs.tsv'), problems)
	check_prose(bugs, fields, problems)
	rows = read_verify(os.path.join(args.dir, 'verify.tsv'), problems)
	bins = parse_bins(args.bin, problems)
	check_verify(args.dir, bugs, fields, rows, checksum_of(bins) if bins
		else None, problems)

	title = args.title or publish.stored_title(args.out)

	if not title:
		problems.add(NO_TITLE)

	for problem in attribution.title_problems(title) if title else ():
		problems.add('title: ' + problem)

	if not os.path.isdir(os.path.dirname(os.path.abspath(args.out))):
		problems.add('--out: the directory of {} does not exist'.format(args.out))

	if problems:
		return refuse(problems)

	body, comments = render(bugs, fields, rows, args.limit)

	import lint_text

	for index, (_, text) in enumerate(comments, 1):
		for problem in lint_text.problems(text, 'comment {}'.format(index)):
			problems.add(problem)

	if len(body) > args.limit:
		problems.add('the body is {:,} characters with every reproduction moved '
			'to comments, over the limit of {:,}; shorten the prose'.format(
			len(body), args.limit))

	for numbers, text in comments:
		if len(text) > args.limit:
			problems.add('the comment holding {} would be {:,} characters, over '
				'the limit of {:,}; shorten its reproduction'.format(bugs_named(
				numbers), len(text), args.limit))

	# The body and its title, after the title and vocabulary checks every
	# published document gets; a problem from either writes nothing.
	if publish.emit('issue_body', body, problems, title, args.out):
		return 1

	stem = os.path.splitext(args.out)[0]

	for index, (numbers, text) in enumerate(comments, 1):
		path = '{}.comment-{}.md'.format(stem, index)
		write(path, text)
		sys.stderr.write('issue_body: {} holds the reproductions of {} ({:,} '
			'characters): it is posted as comment {} right after the issue\n'
			.format(path, bugs_named(numbers), len(text), index))

	if comments:
		sys.stderr.write('issue_body: the body was over {:,} characters, so the '
			'reproductions moved to {} comment{}, posted in order\n'.format(
			args.limit, len(comments), attribution.plural(len(comments))))

	for stale in comment_files(stem, len(comments)):
		os.remove(stale)
		sys.stderr.write('issue_body: removed {}, left from an earlier render\n'
			.format(stale))

	sys.stderr.write('issue_body: the body is {:,} characters\n'.format(len(
		body)))
	return 0


def comment_paths(stem):
	"""The `<stem>.comment-N.md` files that exist, as (N, path) in order."""

	folder = os.path.dirname(os.path.abspath(stem))
	found = []

	if not os.path.isdir(folder):
		return found

	for name in os.listdir(folder):
		match = re.match(r'^{}\.comment-(\d+)\.md$'.format(re.escape(
			os.path.basename(stem))), name)

		if match:
			found.append((int(match.group(1)), os.path.join(folder, name)))

	return sorted(found)


def comment_files(stem, keep):
	"""The `<stem>.comment-N.md` files that exist for N above `keep`."""

	return [path for number, path in comment_paths(stem) if number > keep]


# verify ----------------------------------------------------------------------

def parse_bins(values, problems):
	"""`--bin NAME=PATH` (or a bare PATH, named for its file) as (name, path)."""

	bins = []

	for value in values or []:
		name, separator, path = value.partition('=')
		if not separator or '/' in name:
			name, path = os.path.basename(value), value

		path = os.path.abspath(os.path.expanduser(path))
		if not re.match(r'^[\w.+-]+$', name):
			problems.add('--bin {}: {!r} is not a program name'.format(value, name))
		elif not (os.path.isfile(path) and os.access(path, os.X_OK)):
			problems.add('--bin {}: {} is not an executable file'.format(value, path))
		else:
			bins.append((name, path))

	return bins


def sha256(path):
	digest = hashlib.sha256()

	with open(path, 'rb') as handle:
		for chunk in iter(lambda: handle.read(1 << 20), b''):
			digest.update(chunk)

	return digest.hexdigest()


def checksum_of(bins):
	"""The binary's sha256; for several, the sha256 of their names and sums."""

	if len(bins) == 1:
		return sha256(bins[0][1])

	return hashlib.sha256('\n'.join('{} {}'.format(name, sha256(path))
		for name, path in sorted(bins)).encode('utf-8')).hexdigest()


def run_script(path, cwd, env, timeout, prefix, log):
	"""Run a script with bash in its own process group, its output to `log`,
	and kill the group when it ends. Returns the exit code (None on a
	timeout) and the seconds."""

	start = time.monotonic()

	with open(log, 'wb') as out:
		process = subprocess.Popen(prefix + ['bash', path], cwd=cwd, env=env,
			stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
			start_new_session=True)
		try:
			code = process.wait(timeout=timeout)
		except subprocess.TimeoutExpired:
			code = None

		try:
			os.killpg(process.pid, signal.SIGKILL)
		except OSError:
			pass
		process.wait()

	return code, time.monotonic() - start


def read_capped(path, limit=64 << 20):
	with open(path, 'rb') as handle:
		return handle.read(limit).decode('utf-8', 'replace')


def compare(shown, printed, masks):
	"""How many of the actual block's lines appear in the printed output, of
	how many, and the missing ones. Blank lines and `...` cuts are skipped."""

	def masked(line):
		for pattern in masks:
			line = pattern.sub('…', line)
		return line.rstrip()

	seen = {masked(line) for line in printed.split('\n')}
	wanted = [line for line in shown if line.strip() and line.strip()
		not in ELISION]
	missing = [line for line in wanted if masked(line) not in seen]

	return len(wanted) - len(missing), len(wanted), missing


def verify_one(args, bug, fields, bins, logs):
	"""Run one bug in a new temporary directory: bin/ holds the binaries,
	work/ is the current directory, home/ and tmp/ are HOME and TMPDIR."""

	number, slug = bug['number'], bug['slug']
	base = tempfile.mkdtemp(prefix='verify-{:02d}-{}-'.format(number, slug),
		dir=args.workdir)
	folders = {name: os.path.join(base, name) for name in ('bin', 'work', 'home',
		'tmp')}
	for folder in folders.values():
		os.makedirs(folder)
	for name, path in bins:
		os.symlink(path, os.path.join(folders['bin'], name))

	env = dict(os.environ, PATH=folders['bin'] + os.pathsep + (args.path
		or os.environ.get('PATH', '')), HOME=folders['home'],
		TMPDIR=folders['tmp'])
	env.pop('BASH_ENV', None)
	env.pop('ENV', None)
	prefix = ['taskset', '-c', args.cpus] if args.cpus else []

	report, code, seconds, status, printed = [], None, 0.0, 'pass', ''

	for name, commands in (('setup', fields.get('setup', '')), ('reproduction',
			fields['{}.reproduction'.format(number)])):
		if not commands:
			continue

		script = os.path.join(base, name + '.sh')
		with open(script, 'w') as out:
			out.write(commands + '\n')

		output = os.path.join(base, name + '.out')
		code, seconds = run_script(script, folders['work'], env, args.timeout,
			prefix, output)
		printed = read_capped(output)
		report.append('== {}: {}, {:.2f} s\n{}'.format(name, 'timed out after '
			'{:g} s'.format(args.timeout) if code is None else 'exit {}'.format(
			code), seconds, printed))

		if code is None or (name == 'setup' and code != 0):
			status = 'timeout' if name == 'reproduction' else 'setup-failed'
			break

	note, matched = '-', '-'
	if status == 'pass':
		masks = [re.compile(line) for line in fields.get('{}.varies'.format(
			number), '').split('\n') if line]
		found, total, missing = compare(actual_block(fields['{}.output'.format(
			number)]), printed, masks)
		matched = '{}/{}'.format(found, total)
		report.append('== matched {} of {} lines of the actual output{}'.format(
			found, total, ''.join('\nmissing: ' + line for line in missing)))
		if missing:
			status, note = 'fail', 'missing: ' + missing[0]
	else:
		note = report[-1].split('\n', 1)[0].lstrip('= ')

	log = os.path.join(logs, '{:02d}-{}.log'.format(number, slug))
	with open(log, 'w', encoding='utf-8') as out:
		out.write('# bug {} ({}), {}, in {}\n'.format(number, slug, ', '.join(
			'{}={}'.format(name, path) for name, path in bins), base))
		out.write('\n'.join(report) + '\n')

	kept = status != 'pass' or args.keep
	if not kept:
		shutil.rmtree(base, ignore_errors=True)

	return {
		'number': str(number), 'slug': slug, 'status': status,
		'exit': '-' if code is None else str(code),
		'seconds': '{:.2f}'.format(seconds), 'matched': matched,
		'log': os.path.relpath(log, args.dir), 'note': tsv_cell(note),
	}, base if kept else None


def verify_command(args):
	problems = Problems()
	bugs = read_bugs(os.path.join(args.dir, 'bugs.tsv'), problems)
	fields = read_fields(args.prose or os.path.join(args.dir, 'prose.md'), problems)
	bins = parse_bins(args.bin, problems)
	path = os.path.join(args.dir, 'verify.tsv')
	rows = read_verify(path, problems)
	by_number = {bug['number']: bug for bug in bugs}

	if not args.bin:
		problems.add('verify needs --bin NAME=PATH: the binary the report is '
			'about, put first on the PATH as NAME')

	checksum = checksum_of(bins) if bins else None

	if args.stale and (args.bug or args.manual is not None):
		problems.add('--stale runs the bugs that need it; it does not go with '
			'--bug or --manual')

	if args.manual is not None:
		selected = [args.manual]
		if not (args.reason or '').strip():
			problems.add('--manual needs --reason: why the reproduction cannot run '
				'here, as a clause the body shows')
		if not args.log or not os.path.isfile(args.log):
			problems.add('--manual needs --log: an existing file with the output '
				'of the run that shows the bug')
	elif args.stale:
		why = {bug['number']: stale_reason(rows.get(bug['number']), fields,
			bug['number'], checksum, args.commit) for bug in bugs}
		selected = [number for number, reason in why.items() if reason and
			rows.get(number, {}).get('status') != 'manual']
		for number, reason in why.items():
			if reason and number not in selected:
				problems.add('bug {} is marked manual and {}; mark it manual again '
					'(--manual {} --reason … --log …) or run it'.format(number,
					reason, number))
	else:
		selected = args.bug or [bug['number'] for bug in bugs if rows.get(
			bug['number'], {}).get('status') != 'manual']
		if args.reason or args.log:
			problems.add('--reason and --log go with --manual')

	for number in selected:
		if number not in by_number:
			problems.add('bug {}: not in bugs.tsv'.format(number))
			continue
		for key in ('reproduction', 'output', 'varies'):
			name = '{}.{}'.format(number, key)
			if fields.get(name):
				check_field(name, BUG_FIELDS[key][1], fields[name], problems)
			elif BUG_FIELDS[key][0]:
				problems.add('prose: "=== {}" is missing or empty'.format(name))

	if fields.get('setup'):
		check_field('setup', 'commands', fields['setup'], problems)
	if args.workdir and not os.path.isdir(args.workdir):
		problems.add('--workdir {} is not a directory'.format(args.workdir))
	if args.cpus and not shutil.which('taskset'):
		problems.add('--cpus needs taskset on the PATH')

	if problems:
		sys.stderr.write('issue_body: verify refused, nothing run:\n'
			+ ''.join('  ' + problem + '\n' for problem in problems))
		return 1

	if args.stale:
		print('{} of {} bugs need a run{}'.format(len(selected), len(bugs),
			''.join('\n  bug {} {}: {}'.format(number, by_number[number]['slug'],
			why[number]) for number in selected)))
		if not selected:
			return 0

	common = {'checksum': checksum, 'commit': tsv_cell(args.commit
		or '-'), 'date': datetime.datetime.now(datetime.timezone.utc).strftime(
		'%Y-%m-%dT%H:%MZ')}

	if args.manual is not None:
		number, log = args.manual, os.path.abspath(args.log)
		inside = os.path.commonpath([log, os.path.abspath(args.dir)]) \
			== os.path.abspath(args.dir)
		rows[number] = dict(common, number=str(number),
			slug=by_number[number]['slug'], status='manual', exit='-', seconds='-',
			matched='-', text=text_hash(fields, number), log=os.path.relpath(log,
			args.dir) if inside else log, note=tsv_cell(args.reason.rstrip('.')))
		write_verify(path, rows)
		print('bug {} {}: marked manual ({})'.format(number,
			by_number[number]['slug'], rows[number]['note']))
		return 0

	logs = os.path.join(args.dir, 'verify-logs')
	os.makedirs(logs, exist_ok=True)
	failed = 0

	for number in selected:
		row, kept = verify_one(args, by_number[number], fields, bins, logs)
		row.update(common, text=text_hash(fields, number))
		rows[number] = row
		write_verify(path, rows)
		failed += row['status'] != 'pass'
		print('bug {} {}: {}, exit {}, {} s, {} lines matched{}{}'.format(number,
			row['slug'], row['status'], row['exit'], row['seconds'], row['matched'],
			'' if row['note'] == '-' else '; ' + row['note'],
			'; kept ' + kept if kept else ''))
		sys.stdout.flush()

	return 1 if failed else 0


# template --------------------------------------------------------------------

def template_command(args):
	problems = Problems()
	path = os.path.join(args.dir, 'bugs.tsv')
	bugs = read_bugs(path, problems) if os.path.exists(path) else []

	if problems:
		sys.stderr.write('issue_body: bugs.tsv has problems render will refuse:\n'
			+ ''.join('  ' + problem + '\n' for problem in problems))
	if not bugs:
		sys.stderr.write('issue_body: no bugs.tsv rows, so the skeleton has one '
			'bug, 1\n')
		bugs = [{'number': 1, 'title': 'the first bug'}]

	lines = []
	for name, (_, _, what) in FIELDS.items():
		lines += ['=== ' + name, '<<FILL: {}>>'.format(what), '']

	for bug in bugs:
		for key, (_, _, what) in BUG_FIELDS.items():
			lines += ['=== {}.{}'.format(bug['number'], key), '<<FILL: bug {} ({}): '
				'{}>>'.format(bug['number'], bug['title'], what), '']

	sys.stdout.write('\n'.join(lines))
	return 0


# file and link ---------------------------------------------------------------

FILED = publish.RECORDS['bugs']
GITHUB_LIMIT = 65536
ISSUE_URL = re.compile(r'https://\S+/issues/(\d+)')


def read_filed(directory, problems):
	"""What `file --post` recorded in DIR/filed.json (publish.new_record, with
	`comments`, the URL of each comment posted so far, and `comments_left`,
	the number still to post), or None before the issue exists. A record that
	cannot be read is added to `problems`: it stands for an issue already
	filed, so nothing may file another until it is fixed."""

	path = os.path.join(directory, FILED)

	if not os.path.exists(path):
		return None

	try:
		with open(path, encoding='utf-8') as handle:
			record = json.load(handle)

		if not isinstance(record, dict) or not record.get('url') \
				or not isinstance(record.get('number'), int):
			raise ValueError('it names no issue URL and number')
	except (OSError, ValueError) as error:
		problems.add('{}: cannot read it ({}); it records an issue already '
			'filed, so fix it or remove it by hand first'.format(path, error))
		return None

	record.setdefault('comments', [])
	return record


def filing_problems(args, body_path, title, body, comments):
	"""Everything that should stop the issue being filed: the files are
	missing or not what the inputs render to, the first paragraph, the
	closing line or the title is wrong, a stand-in is left, or the text is
	over GitHub's limit."""

	problems = Problems()

	for problem in attribution.problems('issue', title, body):
		problems.add(problem)

	for number, text in comments:
		for problem in (attribution.closing_problems(text)
				+ attribution.placeholder_problems(text)):
			problems.add('comment {}: {}'.format(number, problem))

	for text, name in [(body, 'the body')] + [(text, 'comment {}'.format(number))
			for number, text in comments]:
		if len(text) > GITHUB_LIMIT:
			problems.add('{} is {:,} characters, over GitHub\'s {:,}'.format(name,
				len(text), GITHUB_LIMIT))

	numbers = [number for number, _ in comments]

	if numbers != list(range(1, len(numbers) + 1)):
		problems.add('the comment files are numbered {}; they must run 1 to '
			'N'.format(', '.join(map(str, numbers))))

	issues = Problems()
	bugs = read_bugs(os.path.join(args.dir, 'bugs.tsv'), issues)
	fields = read_fields(args.prose or os.path.join(args.dir, 'prose.md'), issues)
	check_prose(bugs, fields, issues)
	rows = read_verify(os.path.join(args.dir, 'verify.tsv'), issues)
	check_verify(args.dir, bugs, fields, rows, None, issues)

	for problem in issues:
		problems.add('inputs: ' + problem)

	if not issues:
		again, extra = render(bugs, fields, rows, args.limit)

		if again != body or [text for _, text in extra] != [text for _, text
				in comments]:
			problems.add('{} and its comments are not what the inputs render to '
				'now (an input changed since the last render, or another --limit '
				'was used); run render again'.format(body_path))

	return problems


run_gh = publish.run_gh


def file_command(args):
	"""Show what would be filed; with --post, file it: create the issue, then
	post each comment in order, recording each step in filed.json. Never
	posts without --post, never files twice, and a rerun posts only the
	comments a failure left."""

	body_path = args.body or os.path.join(args.dir, 'body.md')
	stem = os.path.splitext(body_path)[0]
	record_path = os.path.join(args.dir, FILED)
	problems = Problems()
	body = read_text(body_path, problems, 'body') or ''
	title = (read_text(stem + '.title', problems, 'title') or '').strip()
	comments = [(number, read_text(path, problems, 'comment') or '')
		for number, path in comment_paths(stem)]

	if not problems:
		problems = filing_problems(args, body_path, title, body, comments)

	filed = read_filed(args.dir, problems)
	posted = len(filed['comments']) if filed else 0
	done = bool(filed) and posted >= len(comments) and not filed.get(
		'comments_left')

	if filed and filed.get('repo') != args.repo:
		problems.add('{} says this was filed on {}, not {}'.format(FILED,
			filed.get('repo'), args.repo))

	if filed and not done and filed.get('digest') and filed['digest'] \
			!= publish.digest(title, body):
		problems.add('{} was created with another title or body than {} holds '
			'now, so the comments left would not match it; render the inputs it '
			'was created from again, or post the rest by hand'.format(
			filed['url'], body_path))

	if not re.match(r'^[\w.-]+/[\w.-]+$', args.repo):
		problems.add('--repo must be OWNER/REPO')

	if args.post and not shutil.which('gh'):
		problems.add('--post needs gh on the PATH')

	if problems:
		return refuse(problems, 'nothing posted')

	if done:
		print('already filed: {}\nnothing to post'.format(filed['url']))
		return 0

	print('repository: {}'.format(args.repo))
	print('title:      {}'.format(title))
	print('body:       {} ({:,} characters)'.format(body_path, len(body)))

	if filed:
		print('already created: {}; {} of {} comments posted'.format(filed['url'],
			posted, len(comments)))

	for number, text in comments:
		bugs_in = re.findall(r'(?m)^### (\d+)\. ', text)
		print('comment {}:  {}.comment-{}.md, bug{} {} ({:,} characters){}'.format(
			number, stem, number, attribution.plural(len(bugs_in)), ', '.join(
			bugs_in), len(text), ' (posted)' if number <= posted else ''))

	if not args.post:
		left = len(comments) - posted
		print('\n'.join(publish.post_needs(args.repo, 'nothing is pushed; this '
			+ ('posts the {} comment{} left on {}'.format(left, attribution.plural(
			left), filed['url']) if filed else 'files an issue on {} and posts {} '
			'comment{}'.format(args.repo, left, attribution.plural(left))))))
		print('\npreview only: nothing was posted. Run again with --post, after '
			'the repository, title and comments have been approved')
		return 0

	problem = publish.account_problem(args.repo, args.other_owner)

	if problem:
		return refuse([problem], 'nothing posted')

	if not filed:
		code, out, err = run_gh(['issue', 'create', '--repo', args.repo, '--title',
			title, '--body-file', body_path])
		match = ISSUE_URL.search(out)

		if code != 0 or not match:
			sys.stderr.write('issue_body: gh issue create failed ({}); nothing '
				'recorded{}:\n{}\n{}\n'.format(code, '' if code else ', but the '
				'issue may exist: look at {}\'s issues before running this again'
				.format(args.repo), out, err))
			return 1

		filed = publish.new_record(args.repo, match.group(0), None, title, body,
			comments=[], comments_left=len(comments))
		publish.write_record(record_path, filed)
		print('created {}'.format(filed['url']))

	for number, _ in comments[len(filed['comments']):]:
		path = '{}.comment-{}.md'.format(stem, number)
		code, out, err = run_gh(['issue', 'comment', str(filed['number']), '--repo',
			args.repo, '--body-file', path])

		if code != 0:
			sys.stderr.write('issue_body: gh issue comment failed on comment {} '
				'({}); {} is recorded in {} with {} comment{} left, so running '
				'this again posts the rest:\n{}\n{}\n'.format(number, code,
				filed['url'], FILED, len(comments) - len(filed['comments']),
				attribution.plural(len(comments) - len(filed['comments'])), out,
				err))
			return 1

		filed['comments'].append(out.splitlines()[-1] if out else '-')
		filed['comments_left'] = len(comments) - len(filed['comments'])
		publish.write_record(record_path, filed)
		print('posted comment {}: {}'.format(number, filed['comments'][-1]))

	if filed.get('comments_left', 0) != 0:
		filed['comments_left'] = 0
		publish.write_record(record_path, filed)

	print('filed {} with {} comment{}'.format(filed['url'], len(comments),
		attribution.plural(len(comments))))
	return 0


def link_command(args):
	"""Fill a stand-in into the given files: by default the bug report's
	number, from filed.json."""

	problems = Problems()
	filed = read_filed(args.dir, problems)
	placeholder = args.placeholder or attribution.PLACEHOLDERS[0]

	if placeholder not in attribution.PLACEHOLDERS:
		problems.add('--placeholder is one of {}'.format(', '.join(
			attribution.PLACEHOLDERS)))

	if args.value:
		value = args.value
	elif filed and placeholder == attribution.PLACEHOLDERS[0]:
		value = '#{}'.format(filed['number'])
	else:
		value = None
		problems.add('give --value: {} is the bug report\'s number only when '
			'{} says it was filed'.format(placeholder, FILED))

	if value and not re.match(r'^#\d+$|^https://\S+$', value):
		problems.add('--value must be #N or a URL')

	for path in args.files:
		if not os.path.exists(path):
			problems.add('{}: no such file'.format(path))

	if problems:
		return refuse(problems)

	for path, count in publish.link_files(args.files, placeholder, value).items():
		print('{}: {} place{} filled with {}'.format(path, count,
			attribution.plural(count), value))

	return 0


# dupes -----------------------------------------------------------------------

DUPES_COLUMNS = ('bug', 'slug', 'issue', 'state', 'score', 'title', 'url',
	'matched')
STOPWORDS = frozenset('''about after also because before being between both can
could does each else every file files from have into just more most much must
never only other output outputs over same should some such than that their
then there these they this those through tool under until used uses using
want what when where which while with without would write writes written
wrong value values input inputs option options default reads read the and for
not are but you your was were will has had its one two any all'''.split())


def distinctive(bug, fields):
	"""The words a bug is told apart by: its backticked spans (options, names
	of functions), and its other words of four letters or more, from the
	title and the first paragraph."""

	number = bug['number']
	text = ' '.join([bug['title']] + [fields.get('{}.{}'.format(number, key), '')
		for key in ('what', 'cause')])
	spans = {span.strip().lower() for span in re.findall(r'`([^`]+)`', text)
		if len(span.strip()) >= 3}
	plain = re.sub(r'`[^`]*`', ' ', text).lower()
	words = {word for word in re.findall(r'[a-z][a-z0-9_.-]{3,}', plain)
		if word not in STOPWORDS}
	title_words = {word for word in re.findall(r'[a-z][a-z0-9_.-]{3,}', re.sub(
		r'`[^`]*`', ' ', bug['title']).lower()) if word not in STOPWORDS}

	return spans, words, title_words


def candidates(bug, fields, issues, skip, top, floor):
	"""The `top` open or closed issues most like a bug, best first, as
	(score, issue, the words shared): 3 for each backticked span the issue
	holds, 2 for each word of the bug's title in the issue's title, 1 for each
	other word in its text up to five."""

	spans, words, title_words = distinctive(bug, fields)
	found = []

	for issue in issues:
		if issue['number'] in skip:
			continue

		title = issue['title'].lower()
		text = title + ' ' + (issue.get('body') or '').lower()
		held = sorted(span for span in spans if span in text)
		in_title = sorted(word for word in title_words if word in title)
		in_text = sorted(word for word in words - set(in_title) if word in text)
		score = 3 * len(held) + 2 * len(in_title) + min(len(in_text), 5)

		if score >= floor and (held or len(in_title) >= 2):
			found.append((score, issue, held + in_title + in_text[:5 - len(
				in_title)]))

	found.sort(key=lambda item: (-item[0], item[1]['number']))
	return found[:top]


def dupes_command(args):
	"""Which existing issues might already report each bug: one `gh issue list`
	(or --issues FILE, its JSON), matched here by shared words. A list to read,
	not a verdict; `related` in bugs.tsv is filled by hand from it."""

	problems = Problems()
	bugs = read_bugs(os.path.join(args.dir, 'bugs.tsv'), problems)
	fields = read_fields(args.prose or os.path.join(args.dir, 'prose.md'), problems)

	if args.issues:
		text = read_text(args.issues, problems, '--issues')
	elif not shutil.which('gh'):
		text = None
		problems.add('dupes needs gh on the PATH, or --issues FILE (the JSON of '
			'`gh issue list --state all --json number,title,state,url,body`)')
	else:
		text = None

	if not args.issues and not args.repo:
		problems.add('give --repo OWNER/REPO, or --issues FILE')

	filed = read_filed(args.dir, problems)

	if problems:
		return refuse(problems)

	if text is None:
		code, text, err = run_gh(['issue', 'list', '--repo', args.repo, '--state',
			'all', '--limit', str(args.max_issues), '--json',
			'number,title,state,url,body'])

		if code != 0:
			sys.stderr.write('issue_body: gh issue list failed ({}):\n{}\n'.format(
				code, err))
			return 1

	try:
		issues = json.loads(text)
		issues = [dict(issue, number=int(issue['number']), title=issue['title'],
			state=str(issue.get('state', '')).lower(), url=issue.get('url', '-'))
			for issue in issues]
	except (ValueError, KeyError, TypeError) as error:
		return refuse(['the issue list is not the JSON `gh issue list --json '
			'number,title,state,url,body` prints ({!r})'.format(error)])

	skip = {filed['number']} if filed else set()
	lines = ['\t'.join(DUPES_COLUMNS)]

	print('{} issues read'.format(len(issues)))
	for bug in bugs:
		found = candidates(bug, fields, issues, skip, args.top, args.min_score)
		print('bug {} {}:{}'.format(bug['number'], bug['slug'], '' if found
			else ' none'))

		for score, issue, shared in found:
			print('  #{} ({}, score {}) {}'.format(issue['number'], issue['state'],
				score, issue['title']))
			lines.append('\t'.join([str(bug['number']), bug['slug'], str(issue[
				'number']), issue['state'], str(score), tsv_cell(issue['title']),
				issue['url'], tsv_cell(', '.join(shared))]))

	write(os.path.join(args.dir, 'dupes.tsv'), '\n'.join(lines) + '\n')
	print('wrote {}; read each candidate, then fill `related` in bugs.tsv for '
		'the ones that are the same bug or close to it'.format(os.path.join(
		args.dir, 'dupes.tsv')))
	return 0


# status ----------------------------------------------------------------------

def status_command(args):
	"""Where the inputs stand: one line per bug, the counts, the size of the
	body and its comments, and every problem `render` would refuse for."""

	problems = Problems()
	bugs = read_bugs(os.path.join(args.dir, 'bugs.tsv'), problems)
	fields = read_fields(args.prose or os.path.join(args.dir, 'prose.md'), problems)
	check_prose(bugs, fields, problems)
	rows = read_verify(os.path.join(args.dir, 'verify.tsv'), problems)
	bins = parse_bins(args.bin, problems)
	checksum = checksum_of(bins) if bins else None
	check_verify(args.dir, bugs, fields, rows, checksum, problems)

	lines = [('#', 'severity', 'category', 'slug', 'prose', 'verify')]

	for bug in bugs:
		number = bug['number']
		names = {key: '{}.{}'.format(number, key) for key in BUG_FIELDS}
		missing = [key for key, (required, _, _) in BUG_FIELDS.items()
			if required and not fields.get(names[key])]
		filling = [key for key in BUG_FIELDS if '<<FILL' in fields.get(names[
			key], '')]
		row = rows.get(number)
		reason = stale_reason(row, fields, number, checksum, None)

		lines.append((str(number), bug['severity'], bug['category'], bug['slug'],
			'missing ' + ','.join(missing) if missing else 'fill ' + ','.join(
			filling) if filling else 'ok', reason or row['status']))

	widths = [max(len(line[i]) for line in lines) for i in range(6)]
	for line in lines:
		print('  '.join(cell.ljust(widths[i]) for i, cell in enumerate(line)).rstrip())

	print('')
	for title, key, order in (('severity', 'severity', [k for k, _ in SEVERITIES]),
			('category', 'category', [k for k, _ in CATEGORIES])):
		print('{}: {}'.format(title, ', '.join('{} {}'.format(sum(
			bug[key] == value for bug in bugs), value) for value in order)))

	sound = not any(problem.startswith(('bugs.tsv', 'prose')) for problem in
		problems)

	if sound and bugs:
		body, comments = render(bugs, fields, rows, args.limit)
		print('body: {:,} of {:,} characters; {}'.format(len(body), args.limit,
			'no comments' if not comments else '{} comment{}: {}'.format(len(
			comments), '' if len(comments) == 1 else 's', '; '.join(
			'{} ({:,} characters)'.format(bugs_named(numbers), len(text))
			for numbers, text in comments))))

	filed = read_filed(args.dir, problems)

	if filed:
		posted = len(filed['comments'])
		print('filed: {} ({} of {} comment{} posted)'.format(filed['url'],
			posted, posted + int(filed.get('comments_left') or 0),
			attribution.plural(posted + int(filed.get('comments_left') or 0))))

	if problems:
		print('\nrender would refuse:')
		for problem in problems:
			print('  ' + problem)
		return 1

	print('\nready to render')
	return 0


# renumber --------------------------------------------------------------------

MENTION = re.compile(r'\bbugs?\s+(\d+(?:(?:,\s*|\s+and\s+|\s*-\s*)\d+)*)')


def prose_blocks(text):
	"""A prose file as the lines before its first field and a list of (field
	name, its lines from the `=== name` line on)."""

	before, fields = [], []

	for line in text.split('\n'):
		match = FIELD.match(line)

		if match:
			fields.append((match.group(1), [line]))
		elif fields:
			fields[-1][1].append(line)
		else:
			before.append(line)

	return before, fields


def renumbered_prose(text, mapping):
	"""The prose with each bug's fields under its new number, general fields
	first and bug fields in the new number order, the fields of a bug not in
	`mapping` dropped."""

	before, fields = prose_blocks(text)
	general, per_bug = [], []

	for name, lines in fields:
		match = BUG_FIELD.match(name)

		if not match or match.group(2) not in BUG_FIELDS:
			general.append(lines)
		elif int(match.group(1)) in mapping:
			new = mapping[int(match.group(1))]
			per_bug.append((new, '=== {}.{}'.format(new, match.group(2)), lines))

	per_bug.sort(key=lambda item: item[0])
	blocks_out = [[line for line in before]] if any(line.strip()
		for line in before) else []
	blocks_out += general + [[head] + lines[1:] for _, head, lines in per_bug]

	return '\n\n'.join('\n'.join(lines).strip('\n') for lines in blocks_out) + '\n'


def mentions(text, mapping, known):
	"""The lines of the prose that name one of the bugs `known` by number, with
	the numbers that change or go away: (line number, line, [(old, new or
	None)]). A number that is no bug of this report, like "bug 18" of another
	one, is left alone."""

	found = []

	for number, line in enumerate(text.split('\n'), 1):
		for match in MENTION.finditer(line):
			named = [int(n) for n in re.findall(r'\d+', match.group(1))]
			moves = [(old, mapping.get(old)) for old in named
				if old in known and mapping.get(old) != old]
			if moves:
				found.append((number, line.strip(), moves))
				break

	return found


def renumber_command(args):
	"""Put bugs.tsv in category order, numbered 1 to N, and move everything
	that carries a number with it; with --only or --drop, keep fewer bugs."""

	problems = Problems()
	path = os.path.join(args.dir, 'bugs.tsv')
	bugs = read_bugs(path, problems, strict=False)
	prose_path = args.prose or os.path.join(args.dir, 'prose.md')
	prose = read_text(prose_path, problems, 'prose') or ''
	verify_path = os.path.join(args.dir, 'verify.tsv')
	rows = read_verify(verify_path, problems)
	numbers = {bug['number'] for bug in bugs}

	only = {int(n) for n in args.only.split(',')} if args.only else None
	drop = {int(n) for n in args.drop.split(',')} if args.drop else set()
	if args.only and args.drop:
		problems.add('--only and --drop are alternatives')
	for number in sorted((only or set()) | drop):
		if number not in numbers:
			problems.add('bug {}: not in bugs.tsv'.format(number))

	if problems:
		return refuse(problems)

	kept = [bug for bug in bugs if (only is None or bug['number'] in only)
		and bug['number'] not in drop]
	if not kept:
		return refuse(['no bug would be left'])

	kept.sort(key=lambda bug: (RANK[bug['category']], bug['number']))
	mapping = {bug['number']: new for new, bug in enumerate(kept, 1)}
	dropped = sorted(numbers - set(mapping))
	moved = [bug for bug in kept if mapping[bug['number']] != bug['number']]

	for bug in moved:
		print('{} -> {}  {}'.format(bug['number'], mapping[bug['number']],
			bug['slug']))
	for number in dropped:
		print('{} dropped  {}'.format(number, next(bug['slug'] for bug in bugs
			if bug['number'] == number)))

	if not moved and not dropped:
		print('the {} bugs are numbered 1 to {} in category order already'.format(
			len(bugs), len(bugs)))
		return 0

	new_prose = renumbered_prose(prose, mapping)
	for number, line, moves in mentions(new_prose, mapping, numbers):
		sys.stderr.write('issue_body: line {} of the renumbered prose names {}: '
			'{!r}; edit it by hand\n'.format(number, '; '.join('bug {} as {}'
			.format(old, 'bug {}'.format(new) if new else 'a dropped bug')
			for old, new in moves), line))

	if args.dry_run:
		print('dry run: nothing written')
		return 0

	stamp = datetime.datetime.now().strftime('%Y%m%dT%H%M%S')
	backup = os.path.join(args.dir, 'before-renumber-' + stamp)
	os.makedirs(backup)
	for name in ('bugs.tsv', 'prose.md', 'verify.tsv', 'verify-logs'):
		source = os.path.join(args.dir, name) if name != 'prose.md' else prose_path
		if os.path.isdir(source):
			shutil.copytree(source, os.path.join(backup, name))
		elif os.path.exists(source):
			shutil.copy(source, os.path.join(backup, name))

	with open(path, encoding='utf-8') as handle:
		lines = handle.read().rstrip('\n').split('\n')
	header = [name.strip() for name in lines[0].split('\t')]
	raw = {}
	for line in lines[1:]:
		cells = dict(zip(header, (value.strip() for value in line.split('\t'))))
		raw[int(cells['number'])] = cells
	out = ['\t'.join(header)]

	for bug in kept:
		cells = dict(raw[bug['number']], number=str(mapping[bug['number']]))
		out.append('\t'.join(cells[name] for name in header))

	write(path, '\n'.join(out) + '\n')
	write(prose_path, new_prose)

	logs = os.path.join(args.dir, 'verify-logs')
	if rows:
		fresh, renames = {}, []

		for old, row in rows.items():
			if old not in mapping:
				continue

			new = mapping[old]
			was = 'verify-logs/{:02d}-{}.log'.format(old, row['slug'])
			now = 'verify-logs/{:02d}-{}.log'.format(new, row['slug'])
			if row['log'] == was and old != new:
				renames.append((was, now))
				row = dict(row, log=now)
			fresh[new] = dict(row, number=str(new))

		for old in dropped:
			if old in rows:
				name = os.path.basename(rows[old]['log'])
				if rows[old]['log'] == 'verify-logs/' + name and os.path.exists(
						os.path.join(logs, name)):
					os.remove(os.path.join(logs, name))

		for was, now in renames:
			if os.path.exists(os.path.join(args.dir, was)):
				os.replace(os.path.join(args.dir, was), os.path.join(args.dir,
					now + '.moving'))
		for _, now in renames:
			if os.path.exists(os.path.join(args.dir, now + '.moving')):
				os.replace(os.path.join(args.dir, now + '.moving'),
					os.path.join(args.dir, now))

		write_verify(verify_path, fresh)

	print('renumbered {} bugs ({} moved, {} dropped); the originals are in '
		'{}'.format(len(kept), len(moved), len(dropped), backup))
	return 0


# The command line ------------------------------------------------------------

DIR_HELP = 'the input directory, normally $WORK/ship/issue-bugs'


def main(argv=None):
	argv = list(sys.argv[1:] if argv is None else argv)
	if not argv or argv[0] not in ('render', 'verify', 'template', 'renumber',
			'status', 'file', 'link', 'dupes', '-h', '--help'):
		argv.insert(0, 'render')

	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	sub = parser.add_subparsers(dest='command', required=True)

	render_parser = sub.add_parser('render', help='write the body (the default)')
	render_parser.add_argument('dir', nargs='?', help=DIR_HELP)
	render_parser.add_argument('--title', help='the issue\'s title, checked by '
		'attribution.py: a capital letter first, no closing period, at most '
		'{} characters (default: the one stored beside --out)'.format(
		attribution.TITLE_LIMIT))
	render_parser.add_argument('--out', required=True, help='the body, '
		'normally DIR/body.md; the title goes to <out stem>.title, comments to '
		'<out stem>.comment-N.md')
	render_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')
	render_parser.add_argument('--bin', action='append', metavar='NAME=PATH',
		help='refuse unless verify.tsv is for this binary')
	render_parser.add_argument('--limit', type=int, default=LIMIT, help='the '
		'most characters the body and each comment may have (default '
		'%(default)s)')

	verify_parser = sub.add_parser('verify', help='run the reproductions and '
		'write verify.tsv')
	verify_parser.add_argument('dir', help=DIR_HELP)
	verify_parser.add_argument('--bin', action='append', metavar='NAME=PATH',
		help='a binary the reproductions call as NAME, first on the PATH; '
		'repeatable')
	verify_parser.add_argument('--bug', type=int, action='append', help='only '
		'this bug; repeatable (default: every bug not marked manual)')
	verify_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')
	verify_parser.add_argument('--commit', help='the commit the binary was '
		'built from, recorded in each row; render requires the intro to name it')
	verify_parser.add_argument('--timeout', type=float, default=600, help='seconds '
		'for the setup and for the reproduction, each (default %(default)s)')
	verify_parser.add_argument('--workdir', help='where the temporary '
		'directories go (default: the system\'s)')
	verify_parser.add_argument('--path', help='the PATH after the binaries\' '
		'directory (default: this process\'s)')
	verify_parser.add_argument('--cpus', help='run under taskset -c CPUS')
	verify_parser.add_argument('--stale', action='store_true', help='run only '
		'the bugs with no row, a failed row, a row for text that has changed, or '
		'a row for another binary or --commit; a stale manual row is reported')
	verify_parser.add_argument('--keep', action='store_true', help='keep the '
		'temporary directory of a bug that passed')
	verify_parser.add_argument('--manual', type=int, metavar='N', help='mark bug '
		'N manual instead of running it: it needs a build or harness verify '
		'cannot provide')
	verify_parser.add_argument('--reason', help='with --manual: why, as a clause '
		'the body shows ("it needs a clang++/libomp build")')
	verify_parser.add_argument('--log', help='with --manual: the output of the '
		'run that shows the bug')

	template_parser = sub.add_parser('template', help='print a prose skeleton '
		'for DIR/bugs.tsv')
	template_parser.add_argument('dir', help=DIR_HELP)

	renumber_parser = sub.add_parser('renumber', help='number the bugs 1 to N in '
		'category order and move their prose, verify rows and logs with them')
	renumber_parser.add_argument('dir', help=DIR_HELP)
	renumber_parser.add_argument('--only', metavar='N,N,…', help='keep only '
		'these bugs')
	renumber_parser.add_argument('--drop', metavar='N,N,…', help='remove these '
		'bugs')
	renumber_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')
	renumber_parser.add_argument('--dry-run', action='store_true', help='print '
		'the new numbers and the prose lines to edit; write nothing')

	status_parser = sub.add_parser('status', help='where the inputs stand, and '
		'what render would refuse')
	status_parser.add_argument('dir', help=DIR_HELP)
	status_parser.add_argument('--bin', action='append', metavar='NAME=PATH',
		help='also report rows for another binary than this one as stale')
	status_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')
	status_parser.add_argument('--limit', type=int, default=LIMIT, help='the '
		'size limit to plan the comments for (default %(default)s)')

	file_parser = sub.add_parser('file', help='preview filing the issue; with '
		'--post, file it and its comments')
	file_parser.add_argument('dir', help=DIR_HELP)
	file_parser.add_argument('--repo', required=True, metavar='OWNER/REPO')
	file_parser.add_argument('--post', action='store_true', help='create the '
		'issue and post the comments; without it nothing is filed')
	file_parser.add_argument('--body', help='the rendered body (default '
		'DIR/body.md; its title and comments sit beside it)')
	file_parser.add_argument('--other-owner', action='store_true', help='with '
		'--post: the user has confirmed filing on a repository the account gh is '
		'logged in as does not own')
	file_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')
	file_parser.add_argument('--limit', type=int, default=LIMIT, help='the limit '
		'the body was rendered with (default %(default)s)')

	link_parser = sub.add_parser('link', help='fill a stand-in number in '
		'documents')
	link_parser.add_argument('dir', help=DIR_HELP + ', which holds filed.json')
	link_parser.add_argument('files', nargs='+', metavar='FILE')
	link_parser.add_argument('--placeholder', help='one of {} (default the '
		'first)'.format(', '.join(attribution.PLACEHOLDERS)))
	link_parser.add_argument('--value', help='#N or a URL (default the issue '
		'number in filed.json)')

	dupes_parser = sub.add_parser('dupes', help='list existing issues that may '
		'already report each bug')
	dupes_parser.add_argument('dir', help=DIR_HELP)
	dupes_parser.add_argument('--repo', metavar='OWNER/REPO')
	dupes_parser.add_argument('--issues', metavar='FILE', help='the JSON of `gh '
		'issue list --state all --json number,title,state,url,body`, instead of '
		'asking gh')
	dupes_parser.add_argument('--top', type=int, default=3, help='candidates per '
		'bug (default %(default)s)')
	dupes_parser.add_argument('--min-score', type=int, default=4, help='the '
		'lowest score listed (default %(default)s)')
	dupes_parser.add_argument('--max-issues', type=int, default=2000, help='how '
		'many issues to read (default %(default)s)')
	dupes_parser.add_argument('--prose', help='the prose file (default '
		'DIR/prose.md)')

	args = parser.parse_args(argv)

	if args.command == 'dupes':
		return dupes_command(args)
	if args.command == 'file':
		return file_command(args)
	if args.command == 'link':
		return link_command(args)
	if args.command == 'status':
		return status_command(args)
	if args.command == 'renumber':
		return renumber_command(args)
	if args.command == 'verify':
		return verify_command(args)
	if args.command == 'template':
		return template_command(args)
	return render_command(args)


if __name__ == '__main__':
	raise SystemExit(main())
