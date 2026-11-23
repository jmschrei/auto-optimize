#!/usr/bin/env python3
"""The unit-test PR's body, rendered from generated tables and one prose
file.

	python3 tests_pr_body.py DIR --template > DIR/prose.md
	python3 tests_pr_body.py DIR --title 'Add unit tests for pkg' --out DIR/body.md

DIR is the folder `tests_pr_tables.py` writes its tables into,
`$WORK/ship/pr-tests`; the prose file (`DIR/prose.md` unless `--prose` names
another) holds what no table can say, in fields that each start with a line
`=== <field>`. `--template` prints a prose skeleton with one `<<FILL: …>>` per
field the tables call for.

Every number, total, percentage and ranking in the body is computed here from
the tables; prose refers to a computed number only through a `{{token}}`. The
same tables and prose give the same body, byte for byte. The body opens with
the `opening` field followed by the sentences computed from the tables.
`--out` needs `--title`, which must
pass attribution.py's title rule and is written to `<out stem>.title`.

`#<bug issue>` is replaced by the number in `DIR/../issue-bugs/filed.json`
once the bug report is filed. When the bug report is not among the documents
chosen at step 5 (`after.ship_documents` in the search's config.json), that
issue will never exist: the prose may not hold `#<bug issue>`, and
`contradictions` names an issue number, a URL or a file in the repository
instead.

A section whose tables are absent is dropped, and stderr says which. Exit 1,
with every problem listed and nothing written, on a `<<FILL` placeholder, a
field a rendered section needs and the prose lacks, an unknown field or
token, tables that disagree with each other, or a title or body that
attribution.py or lint_text.py refuses.

The tables, the fields and the sections: references/pr-tests.md. Standard
library only, Python 3.9 or later.
"""

import os
import re
import sys
import json
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution
import publish


# Files outside the tests that a tests PR may change, to declare a test
# dependency: only the files that list dependencies, by base name.
DEPENDENCY_MANIFESTS = ('DESCRIPTION', 'requirements-dev.txt',
	'requirements-test.txt', 'test-requirements.txt', 'dev-requirements.txt')

BUG_ISSUE = attribution.PLACEHOLDERS[0]

# Field name: (takes an argument, what it holds, as --template explains it).
FIELDS = {
	'opening': (False, 'the first sentence of the body: what the suite tests, '
		'e.g. "This PR adds a pytest suite for `pkg` and its command-line '
		'programs." The renderer follows it with the number of tests and '
		'modules and the sentence that no source, build or workflow file '
		'changes'),
	'support': (False, 'optional: what the suite adds besides its test '
		'modules, ending the sentence "It holds N tests in M modules under '
		'`dir/`, with …", e.g. "shared helpers in `helpers.py` and fixtures in '
		'`conftest.py`". Delete the field if there is nothing'),
	'declares': (False, 'optional: the one change outside the test files, as '
		'the sentence "Its one change outside the tests is <this>.", for a '
		'test dependency declared in a dependency manifest (' +
		', '.join(DEPENDENCY_MANIFESTS) + '), e.g. "adding `withr`, which '
		'the test helpers use, to `Suggests` in `DESCRIPTION`". No other file '
		'outside the tests is allowed. Delete the field if there is nothing'),
	'driver': (True, 'optional: the repository\'s own test driver, named after '
		'it (`=== driver test/test.sh`), when the branch edits it so that the '
		'existing test command runs the new tests; the text is a clause after '
		'"which", e.g. "also runs each `test-<tool>-<topic>.sh` file in a '
		'tool\'s directory, so the new files run under `make test`". Allowed '
		'only when config.json\'s after.tests_driver_edit is yes. Delete the '
		'field if there is nothing'),
	'docs': (False, 'what expected values are checked against, as it reads '
		'mid-sentence: "the MANUAL", "the documentation", "the docstrings"'),
	'running.commands': (False, 'the commands that build what the suite needs '
		'and run it, one per line, without a code fence'),
	'running.notes': (False, 'bullets: how long the suite takes and on how '
		'many cores, the minimum Python and every new dependency, the '
		'environment variables the suite reads, and each reason a test skips '
		'(a missing optional dependency or tool, an operating system)'),
	'running.ci': (False, 'optional: whether the suite is added to the CI '
		'workflows, and the step that would run it. Delete the field if there '
		'is nothing to say'),
	'checked': (False, 'one paragraph, no list: the CI configurations the '
		'suite was run in before this PR (each workflow job\'s install and test '
		'commands, in each Python or compiler version), and that it passed in '
		'each, from step 1\'s record of those runs in $WORK/tests/work/. It is '
		'the last paragraph of *Running it*'),
	'derivation': (False, 'one sentence on what expected values are checked '
		'against, then bullets: how inputs are generated, which fields are '
		'computed before the program runs and from which documented rules. The '
		'renderer appends how many test functions pin the current output'),
	'module': (True, 'what this module\'s tests check, one clause of at most '
		'25 words: it is a table cell'),
	'coverage.method': (False, 'bullets: the build and its coverage flags, the '
		'compiler; the coverage tool and its version; what counts as covered; '
		'how the existing tests were run'),
	'parts.tests': (False, 'why tests per part were not counted (no per-test '
		'coverage, and no test files written per part)'),
	'contradictions': (False, 'one paragraph, no list: how many behaviours '
		'contradict the documentation and are left untested, and where they '
		'are listed: the bug report (#<bug issue>, until it is filed) when it '
		'is among the documents chosen at step 5, otherwise an issue number, a '
		'URL or a file in the repository in backticks; or the single word '
		'none'),
	'contradictions.marked': (False, 'where the suite records each case it '
		'leaves untested, as it is true of this suite: one paragraph of whole '
		'sentences, which the renderer puts after "… is left untested, so that '
		'fixing it will not break the suite." and before the paragraph naming '
		'where the cases are listed. Where every affected test is marked: '
		'"Each affected test says so in a docstring or comment." Where a '
		'document names the cases instead, name the document and count what it '
		'and the tests name. Delete the field when contradictions is none'),
	'platforms': (False, 'the platforms the suite ran on, and those CI covers '
		'that were not tried'),
}

TOKEN = re.compile(r'\{\{\s*([A-Za-z_]+)\s*\}\}')
FIELD = re.compile(r'^=== (\S+)(?:[ \t]+(.*?))?[ \t]*$')
LIST_ITEM = re.compile(r'^\s*([-*+]|\d+[.)])\s')
WORDS = ('no', 'one', 'two', 'three', 'four', 'five', 'six', 'seven', 'eight',
	'nine')
# How many rows the rankings show.
GAINS_SHOWN = 5
UNREACHED_SHOWN = 3
GAPS_SHOWN = 6
NAMES_SHOWN = 10
# The longest a module's table cell may be.
MODULE_WORDS = 25
# Where `contradictions` says its cases are listed: the bug report, or another
# issue or page; without the bug report, a file in the repository also does.
REPORT_LINK = re.compile(r'#<bug issue>|#\d+|https?://')
OTHER_LINK = re.compile(r'#\d+|https?://')
CODE_SPAN = re.compile(r'`([^`\s]+)`')


# Formatting ------------------------------------------------------------------

def count(n):
	"""Words below ten, digits with thousands separators from ten."""

	return WORDS[n] if 0 <= n < len(WORDS) else '{:,}'.format(n)


def plural(n, word, many=None):
	return '{} {}'.format(count(n), word if n == 1 else (many or word + 's'))


def lines_of(n):
	return '{} {}'.format(digits(n), 'line' if n == 1 else 'lines')


def digits(n):
	return '{:,}'.format(n)


def percent(part, whole):
	return '{:.1f}%'.format(100.0 * part / whole) if whole else '–'


def capital(text):
	return text[:1].upper() + text[1:]


def code(text):
	return text if '`' in text else '`{}`'.format(text)


def pathlike(text):
	"""A part name shown in code type: a path, not a name the map gave."""

	return code(text) if re.match(r'^[\w./+-]+$', text) and ('/' in text
		or '.' in text) else text


def unwrap(text):
	"""Each paragraph and list item on one line: GitHub renders a line break
	inside a PR's paragraph as a break. Code fences and table rows
	are kept as they are."""

	out, fenced = [], False

	for line in text.split('\n'):
		stripped = line.strip()

		if stripped.startswith('```'):
			fenced = not fenced
			out.append(line.rstrip())
			continue

		if fenced:
			out.append(line.rstrip())
			continue

		if not stripped:
			if out and out[-1] != '':
				out.append('')
			continue

		previous = out[-1].lstrip() if out else ''

		if not out or not previous or stripped.startswith('|') \
				or LIST_ITEM.match(line) or previous.startswith(('|', '```')):
			out.append(line.rstrip())
		else:
			out[-1] = out[-1].rstrip() + ' ' + stripped

	return '\n'.join(out).strip('\n')


def append_bullet(text, bullet):
	"""`bullet` as the last item of the list `text` ends with, or as a list
	of its own after it."""

	last = text.rstrip('\n').split('\n')[-1] if text.strip() else ''
	return text.rstrip('\n') + ('\n' if LIST_ITEM.match(last) else '\n\n') \
		+ bullet


def one_paragraph(text):
	"""True when `text`, unwrapped, is one paragraph and not a list."""

	text = text.strip()
	return '\n' not in text and not LIST_ITEM.match(text)


# Reading ---------------------------------------------------------------------

class Problems(list):
	def add(self, message):
		if message not in self:
			self.append(message)


def read_table(data, name, columns, problems):
	"""Rows of DATA/name as dicts (with `_line`), None when the file is
	absent."""

	path = os.path.join(data, name)

	if not os.path.exists(path):
		return None

	try:
		with open(path, encoding='utf-8') as infile:
			lines = [line.rstrip('\r\n') for line in infile]
	except OSError as error:
		problems.add('could not read {}: {}'.format(path, error))
		return []

	numbered = [(number, line) for number, line in enumerate(lines, 1)
		if line.strip()]

	if not numbered:
		problems.add('{} is empty: it needs its header line'.format(name))
		return []

	header = numbered[0][1].split('\t')
	missing = [column for column in columns if column not in header]

	if missing:
		problems.add('{} has no {} column (its header: {})'.format(name,
			', '.join(missing), ', '.join(header)))
		return []

	rows = []

	for number, line in numbered[1:]:
		fields = line.split('\t')

		if len(fields) != len(header):
			problems.add('{} line {}: {} fields where the header has {}'.format(
				name, number, len(fields), len(header)))
			continue

		row = dict(zip(header, fields))
		row['_line'] = number
		rows.append(row)

	return rows


def whole(row, key, name, problems, empty=False):
	"""A cell as a non-negative int; None for an empty cell where allowed."""

	value = row.get(key, '').strip()

	if value == '' and empty:
		return None

	if not re.match(r'^\d+$', value):
		problems.add('{} line {}: {} is {!r}, not a whole number'.format(name,
			row['_line'], key, value))
		return 0

	return int(value)


def read_prose(path, problems, fields=None, plain=('running.commands',)):
	"""{(field, argument): text}, in file order. `fields` (default FIELDS)
	names the fields the file may hold; `plain` names those that may hold
	lines starting with `#`."""

	known, kept = fields or FIELDS, {}

	try:
		with open(path, encoding='utf-8') as infile:
			lines = infile.read().split('\n')
	except OSError as error:
		problems.add('could not read the prose file {}: {}. `--template` prints '
			'a skeleton'.format(path, error))
		return {}

	fields, current, name = kept, None, os.path.basename(path)

	for number, line in enumerate(lines, 1):
		found = FIELD.match(line)

		if found:
			field, argument = found.group(1), (found.group(2) or '').strip()
			current = None

			if field not in known:
				problems.add('{} line {}: no field is called {!r} (the fields: {})'
					.format(name, number, field, ', '.join(sorted(known))))
				continue

			if known[field][0] and not argument:
				problems.add('{} line {}: "=== {}" needs what it describes after '
					'it'.format(name, number, field))
				continue

			if not known[field][0] and argument:
				problems.add('{} line {}: "=== {}" takes nothing after it'.format(
					name, number, field))
				continue

			key = (field, ' '.join(argument.split()))

			if key in fields:
				problems.add('{} line {}: "=== {}" appears twice'.format(name,
					number, ' '.join(key).strip()))
				continue

			fields[key] = []
			current = key
			continue

		if current is None:
			if line.strip():
				problems.add('{} line {}: text before the first "=== <field>" '
					'line'.format(name, number))
			continue

		fields[current].append(line.rstrip())

	texts = {}

	for key, body in fields.items():
		text = '\n'.join(body).strip('\n')
		label = ' '.join(key).strip()

		if not text.strip():
			problems.add('{}: "=== {}" is empty: fill it, or delete an optional '
				'field'.format(name, label))
			continue

		if '<<FILL' in text:
			problems.add('{}: "=== {}" still holds a <<FILL placeholder'.format(
				name, label))

		fenced = False

		for line in text.split('\n'):
			if line.strip().startswith('```'):
				fenced = not fenced
			elif not fenced and key[0] not in plain \
					and re.match(r'^#{1,6}\s', line):
				problems.add('{}: "=== {}" holds a heading ({!r}); the renderer '
					'writes every heading'.format(name, label, line.strip()))

		texts[key] = text

	return texts


# The tables ------------------------------------------------------------------

def load(data, problems):
	"""Every table in DATA, checked against itself and the others."""

	tables = {}
	collection = read_table(data, 'collection.tsv', ['nodeid', 'pinned'],
		problems)

	if collection is None:
		problems.add('{} has no collection.tsv: `tests_pr_tables.py collection` '
			'writes it, and every body needs it'.format(data))
		collection = []

	nodeids = [row['nodeid'] for row in collection]

	if len(set(nodeids)) != len(nodeids):
		problems.add('collection.tsv lists a node id twice')

	modules = {}

	for nodeid in nodeids:
		module = publish.split_nodeid(nodeid)[0]
		modules[module] = modules.get(module, 0) + 1

	functions = sorted({publish.split_nodeid(n) for n in nodeids})
	marks = {row['pinned'].strip() for row in collection}
	pinned = None

	if collection and marks <= {'yes', 'no'}:
		pinned = len({publish.split_nodeid(row['nodeid']) for row in collection
			if row['pinned'].strip() == 'yes'})
	elif marks - {''}:
		problems.add('collection.tsv: pinned is yes, no, or empty in every row')

	tables['tests'] = len(nodeids)
	tables['modules'] = modules
	tables['functions'] = len(functions)
	tables['pinned'] = pinned

	files = read_table(data, 'files.tsv', ['status', 'path'], problems)
	tables['files'] = None if files is None else [row['path'] for row in files]
	tables['branch'] = None
	branch = os.path.join(data, 'branch.json')

	if os.path.exists(branch):
		try:
			with open(branch, encoding='utf-8') as handle:
				tables['branch'] = json.load(handle)
		except (OSError, ValueError) as error:
			problems.add('could not read {}: {}'.format(branch, error))

	coverage = read_table(data, 'coverage.tsv', ['file', 'part', 'excluded',
		'lines', 'existing', 'suite', 'both'], problems)
	parts = read_table(data, 'parts.tsv', ['part', 'excluded', 'tests_before',
		'tests_after', 'counted'], problems)

	if (coverage is None) != (parts is None):
		problems.add('coverage.tsv and parts.tsv come together from '
			'`tests_pr_tables.py coverage`; {} is missing'.format(
			'parts.tsv' if parts is None else 'coverage.tsv'))
		coverage = parts = None

	tables['coverage'] = coverage and load_coverage(coverage, parts, problems)

	names = read_table(data, 'names.tsv', ['name', 'defined_in', 'tests'],
		problems)

	if names is not None:
		for row in names:
			row['tests'] = whole(row, 'tests', 'names.tsv', problems)

	tables['names'] = names

	return tables


def load_coverage(rows, parts, problems):
	labels = [column[len('existing:'):] for column in rows[0] if column.startswith(
		'existing:')] if rows else []
	files = []

	for row in rows:
		record = {'file': row['file'], 'part': row['part'],
			'excluded': row['excluded'].strip() == 'yes'}

		for key in ('lines', 'existing', 'suite', 'both'):
			record[key] = whole(row, key, 'coverage.tsv', problems)

		record['labels'] = [whole(row, 'existing:' + label, 'coverage.tsv',
			problems) for label in labels]
		where = 'coverage.tsv line {} ({})'.format(row['_line'], row['file'])

		if not (max(record['existing'], record['suite']) <= record['both']
				<= min(record['lines'], record['existing'] + record['suite'])):
			problems.add('{}: lines {}, existing {}, suite {} and both {} '
				'disagree: both is at least each of existing and suite, at most '
				'their sum and at most lines'.format(where, record['lines'],
				record['existing'], record['suite'], record['both']))

		if record['labels'] and not (max(record['labels']) <= record['existing']
				<= sum(record['labels'])):
			problems.add('{}: existing ({}) is not the union of its labels ({})'
				.format(where, record['existing'], ', '.join(str(n)
				for n in record['labels'])))

		files.append(record)

	if len({record['file'] for record in files}) != len(files):
		problems.add('coverage.tsv lists a file twice')

	counted_parts = {}

	for record in files:
		counted_parts.setdefault(record['part'], set()).add(record['excluded'])

	for part, marks in sorted(counted_parts.items()):
		if len(marks) > 1:
			problems.add('coverage.tsv: part {} holds both excluded and counted '
				'files'.format(part))

	table, total = {}, None

	for row in parts:
		record = {'part': row['part'], 'excluded': row['excluded'].strip() == 'yes',
			'before': whole(row, 'tests_before', 'parts.tsv', problems, True),
			'after': whole(row, 'tests_after', 'parts.tsv', problems, True),
			'counted': row['counted'].strip()}

		if record['counted'] not in ('per-test', 'by-file', ''):
			problems.add('parts.tsv line {}: counted is per-test, by-file or '
				'empty, not {!r}'.format(row['_line'], record['counted']))

		if (record['before'] is None) != (record['after'] is None) or \
				(record['before'] is not None and record['before'] > record['after']):
			problems.add('parts.tsv line {} ({}): tests_before must be at most '
				'tests_after, and both given or both empty'.format(row['_line'],
				row['part']))

		if row['part'] == '(total)':
			total = record
		elif row['part'] in table:
			problems.add('parts.tsv lists part {} twice'.format(row['part']))
		else:
			table[row['part']] = record

	if set(table) != set(counted_parts):
		problems.add('parts.tsv and coverage.tsv name different parts: only in '
			'parts.tsv {}; only in coverage.tsv {}'.format(sorted(set(table)
			- set(counted_parts)) or 'none', sorted(set(counted_parts)
			- set(table)) or 'none'))

	if total is None:
		problems.add('parts.tsv has no (total) row')
		total = {'before': None, 'after': None, 'counted': ''}

	for part, record in table.items():
		if part in counted_parts and record['excluded'] != (True in
				counted_parts[part]):
			problems.add('parts.tsv and coverage.tsv disagree on whether part {} '
				'is excluded'.format(part))

	counted = {record['counted'] for record in list(table.values()) + [total]
		if not record.get('excluded')}

	if len(counted) > 1:
		problems.add('parts.tsv mixes ways of counting tests per part: {}'.format(
			', '.join(sorted(c or 'not counted' for c in counted))))

	kept = [record for record in table.values() if not record['excluded']]

	if total['before'] is not None and kept and all(record['before'] is not None
			for record in kept):
		if total['after'] < max(record['after'] for record in kept) or \
				total['after'] > sum(record['after'] for record in kept):
			problems.add('parts.tsv: the (total) row\'s tests_after ({}) is below '
				'a part\'s or above their sum'.format(total['after']))

	return {'labels': labels, 'files': files, 'parts': table, 'total': total,
		'counted': counted.pop() if len(counted) == 1 else ''}


# Rendering -------------------------------------------------------------------

class Context:
	def __init__(self, tables, prose, problems):
		self.tables = tables
		self.prose = prose
		self.problems = problems
		self.used = set()
		self.allow_source = tables.get('allow_source') or []
		self.bugs = tables.get('bugs', True)
		self.tokens = self.make_tokens()

	def make_tokens(self):
		tables = self.tables
		tokens = {'tests': digits(tables['tests']),
			'modules': count(len(tables['modules'])),
			'functions': digits(tables['functions'])}

		if tables['pinned'] is not None:
			tokens['pinned'] = digits(tables['pinned'])

		return tokens

	def substitute(self, key, text):
		def replace(found):
			name = found.group(1)

			if name not in self.tokens:
				self.problems.add('prose "=== {}": no table gives {{{{{}}}}} (the '
					'tokens here: {})'.format(' '.join(key).strip(), name,
					', '.join(sorted(self.tokens))))
				return found.group(0)

			return self.tokens[name]

		return TOKEN.sub(replace, text)

	def get(self, field, argument='', why=None, keep=False):
		"""The field's text, unwrapped, with its tokens substituted; None when
		absent, and then a problem when `why` (the section that needs it) is
		given."""

		key = (field, argument)
		self.used.add(key)
		text = self.prose.get(key)

		if text is None:
			if why:
				self.problems.add('the prose has no "=== {}", which {} needs'.format(
					' '.join(key).strip(), why))
			return None

		text = self.substitute(key, text)
		return text if keep else unwrap(text)


def common_directory(modules):
	directories = [os.path.dirname(module) for module in modules]

	if not directories:
		return ''

	common = os.path.commonpath(directories) if all(directories) else ''
	return common.replace('\\', '/')


def opening(ctx):
	tables = ctx.tables
	modules = tables['modules']
	common = common_directory(modules)
	first = (ctx.get('opening', why='the opening paragraph') or '').strip()
	support = ctx.get('support')
	declares = ctx.get('declares')
	closing = 'It changes no source, build or workflow file.'

	if first and not (first[0].isupper() or first[0] == '`'):
		ctx.problems.add('prose "=== opening" is the first sentence of the body: '
			'start it with a capital letter ("This PR adds a pytest suite for '
			'`pkg`.")')

	if len(modules) == 1:
		where = 'one module, `{}`'.format(next(iter(modules)))
	else:
		where = '{} modules{}'.format(count(len(modules)), ' under `{}/`'.format(
			common) if common else '')

	holds = 'It holds {} in {}{}.'.format(plural(tables['tests'], 'test'), where,
		', with ' + support.strip().rstrip('.') if support else '')

	drivers = sorted(argument for field, argument in ctx.prose
		if field == 'driver')
	driver = drivers[0] if drivers else None
	ctx.used.update(('driver', path) for path in drivers)

	if len(drivers) > 1:
		ctx.problems.add('prose: `driver` names {}; a tests PR may edit one test '
			'driver'.format(', '.join(drivers)))

	if tables['files'] is not None:
		if common:
			outside = [path for path in tables['files']
				if not path.startswith(common + '/')]

			manifests = [path for path in outside
				if os.path.basename(path) in DEPENDENCY_MANIFESTS]
			driven = driver if driver in outside else None
			others = [path for path in outside
				if path not in manifests and path != driven]
			edit = tables.get('driver_edit')

			if driver and not driven:
				ctx.problems.add('prose: `driver {}` names a file files.tsv does not '
					'show changed outside `{}/`: fix the path or delete the field'
					.format(driver, common))

			if driven and edit not in ('yes', True):
				ctx.problems.add('files.tsv: the branch changes the test driver `{}`, '
					'and config.json\'s after.tests_driver_edit is {}: it is allowed '
					'only once the user says yes, recorded with `autoopt.py config '
					'--set after.tests_driver_edit=yes --reason \'<their words>\'`'
					.format(driven, 'absent' if edit is None else repr(edit)))

			allowed = [path for path in others if path in ctx.allow_source]
			others = [path for path in others if path not in allowed]
			stale = [path for path in ctx.allow_source if path not in outside]
			undeclared = [path for path in allowed
				if path not in (declares or '')]

			if stale:
				ctx.problems.add('--allow-source names {}, which files.tsv does not '
					'show changed outside `{}/`: fix the path or drop the flag'
					.format(', '.join(stale), common))

			if undeclared:
				ctx.problems.add('prose: --allow-source names {}, and `declares` does '
					'not mention {}: `declares` must name every file the user approved, '
					'in backticks'.format(', '.join(undeclared), 'it' if len(undeclared)
					== 1 else 'them'))

			if others or (manifests and not declares) or (allowed and not declares):
				ctx.problems.add('files.tsv: the branch changes {} outside `{}/`, '
					'so the opening cannot say it changes no source, build or '
					'workflow file: {}{}'.format(plural(len(outside), 'file'), common,
					', '.join(outside[:10]),
					'; a dependency manifest ({}) is allowed only with the '
					'`declares` field, the repository\'s test driver only with '
					'`=== driver <path>` and after.tests_driver_edit yes, and any other '
					'file only when the user approved it by name: `--allow-source PATH` '
					'plus `declares` naming it'.format(', '.join(DEPENDENCY_MANIFESTS))))
			elif manifests or driven or allowed:
				named = ['`{}`'.format(path) for path in manifests + allowed]

				if allowed:
					closing = 'It changes no workflow file.'
				else:
					closing = 'It changes no source or workflow file.'

				if allowed:
					closing += ' It changes {} outside the tests: {}.'.format(
						publish.series(named), declares.strip().rstrip('.'))
				elif manifests:
					closing += ' Its one change outside the tests is {}.'.format(
						declares.strip().rstrip('.'))

				if driven:
					closing += ' {} `{}`, the repository\'s test driver, which {}.'\
						.format('It also changes' if manifests or allowed else 'Its one '
						'change outside the tests is to', driven, ctx.get('driver', driven)
						.strip().rstrip('.'))

			if declares and not manifests and not others and not allowed:
				ctx.problems.add('prose: `declares` names a change outside the '
					'tests, and files.tsv shows no dependency manifest: delete the '
					'field')
		else:
			sys.stderr.write('tests_pr_body: the test modules share no directory, '
				'so files.tsv was not checked against the opening\'s claim\n')
	else:
		sys.stderr.write('tests_pr_body: no files.tsv, so the opening\'s claim '
			'that no source, build or workflow file changes was not checked\n')

	return [' '.join(part for part in (first, holds, closing) if part)]


def module_name(module, common):
	return module[len(common) + 1:] if common and module.startswith(common + '/') \
		else module


def running(ctx):
	commands = ctx.get('running.commands', why='*Running it*', keep=True) or ''
	commands = '\n'.join(line for line in commands.strip('\n').split('\n')
		if not line.strip().startswith('```'))
	notes = ctx.get('running.notes', why='*Running it*') or ''
	lines = ['## Running it', '', '```', commands, '```', '', notes]
	ci = ctx.get('running.ci')

	if ci:
		lines += ['', ci]

	# Where the suite was run before the PR, from step 1's record: always the
	# section's last paragraph.
	checked = (ctx.get('checked', why='*Running it*') or '').strip()

	if checked and not one_paragraph(checked):
		ctx.problems.add('prose "=== checked" must be one paragraph, not a list '
			'or several paragraphs: the CI configurations the suite was run in '
			'and that it passed in each')

	return lines + ['', checked]


def derivation(ctx):
	text = ctx.get('derivation', why='*How expected values are derived*') or ''
	tables = ctx.tables
	docs = ctx.get('docs') or 'the documentation'

	if tables['pinned'] is None:
		sys.stderr.write('tests_pr_body: collection.tsv has no pinned column '
			'(collection without --root), so *How expected values are derived* '
			'does not count the test functions that pin the current output\n')
	elif tables['pinned']:
		text = append_bullet(text, '- **Where {} is silent:** {} of the {} test '
			'functions or methods pin the current output instead, and each says '
			'so in its documentation or a comment.'.format(docs,
			digits(tables['pinned']), digits(tables['functions'])))
	else:
		text = append_bullet(text, '- **Pinned output:** none of the {} test '
			'functions or methods pins the current output.'.format(digits(
			tables['functions'])))

	return ['## How expected values are derived', '', text]


def covers(ctx):
	modules = ctx.tables['modules']
	common = common_directory(modules)
	lines = ['## What it covers', '', '| Module | Tests | What it checks |',
		'|---|---:|---|']

	for module, n in sorted(modules.items(), key=lambda item: (-item[1],
			item[0])):
		text = ctx.get('module', module, why='the row of {} in *What it '
			'covers*'.format(module))

		if text and '\n' in text:
			ctx.problems.add('prose "=== module {}" is more than one paragraph: '
				'it is a table cell'.format(module))
		elif text and len(text.split()) > MODULE_WORDS:
			ctx.problems.add('prose "=== module {}" is {} words: a table cell '
				'holds one clause of at most {}'.format(module, len(text.split()),
				MODULE_WORDS))

		lines.append('| `{}` | {} | {} |'.format(module_name(module, common),
			digits(n), publish.cell((text or '').replace('\n', ' '))))

	for key in ctx.prose:
		if key[0] == 'module' and key[1] not in modules:
			ctx.used.add(key)
			ctx.problems.add('prose "=== module {}": collection.tsv collected no '
				'test in that module (the modules: {})'.format(key[1], ', '.join(
				sorted(modules))))

	return lines


def parts_section(ctx):
	data = ctx.tables['coverage']
	files = data['files']
	counted = [f for f in files if not f['excluded']]
	excluded = [f for f in files if f['excluded']]
	total = sum(f['lines'] for f in counted)

	# One row per part, the excluded ones after the rest, then the total.
	by_part = {}

	for record in files:
		by_part.setdefault(record['part'], []).append(record)

	def tests(n):
		return '–' if n is None else digits(n)

	lines = ['## Tests and line coverage by part', '', '| Part | Tests before '
		'| Tests after | Line coverage before | Line coverage after |',
		'|---|---:|---:|---:|---:|']
	unknown = {'before': None, 'after': None, 'excluded': False}
	order = sorted(by_part, key=lambda part: (data['parts'].get(part,
		unknown)['excluded'], re.sub(r'[`*_]', '', part).lower(), part))

	for part in order:
		members = by_part[part]
		record = data['parts'].get(part, unknown)
		lines.append('| {}{} | {} | {} | {} | {} |'.format(publish.cell(
			pathlike(part)), ' (excluded)' if record['excluded'] else '',
			tests(record['before']), tests(record['after']), percent(sum(
			f['existing'] for f in members), sum(f['lines'] for f in members)),
			percent(sum(f['both'] for f in members), sum(f['lines']
			for f in members))))

	lines.append('| **Total**{} | {} | {} | {} | {} |'.format(
		' (excluded parts left out)' if excluded else '',
		tests(data['total']['before']), tests(data['total']['after']),
		percent(sum(f['existing'] for f in counted), total),
		percent(sum(f['both'] for f in counted), total)))
	how = '"Before" is the existing tests, and "after" adds this suite.'

	if data['counted'] == 'per-test':
		how += ' Tests per part count the tests that ran at least one line of ' \
			'the part, from per-test coverage; a test counts once per part, so ' \
			'the rows do not sum to the total.'
	elif data['counted'] == 'by-file':
		how += ' Tests per part count the tests in the files written for that ' \
			'part, as no per-test coverage was recorded.'
	else:
		reason = ctx.get('parts.tests', why='*Line coverage*, whose parts.tsv '
			'has no tests per part,')
		how += ' Tests per part were not counted: {}'.format(
			(reason or '').strip().rstrip('.') + '.')

	lines += ['', how]

	return lines


def coverage_section(ctx):
	data = ctx.tables['coverage']
	files = data['files']
	counted = [f for f in files if not f['excluded']]
	excluded = [f for f in files if f['excluded']]
	total = sum(f['lines'] for f in counted)

	def share(n, bold=False):
		text = '{} ({})'.format(digits(n), percent(n, total))
		return '**{}**'.format(text) if bold else text

	method = ctx.get('coverage.method', why='*Line coverage*') or ''
	tally = '- **Lines counted:** each source line counts once: {} executable ' \
		'lines in {}.'.format(digits(total), plural(len(counted), 'file'))

	if excluded:
		tally = tally[:-1] + '; {} of vendored or generated code ({}) {} ' \
			'counted apart and left out of every total.'.format(plural(len(
			excluded), 'file'), lines_of(sum(f['lines'] for f in excluded)),
			'is' if len(excluded) == 1 else 'are')

	lines = ['## Line coverage', '', 'How it was measured:', '',
		append_bullet(method, tally), '', '| Tests | Lines covered |', '|---|---:|']
	labels = data['labels']

	if len(labels) > 1:
		for i, label in enumerate(labels):
			lines.append('| {} | {} |'.format(publish.cell(label), share(sum(
				f['labels'][i] for f in counted))))

	lines.append('| **{}** | {} |'.format('Existing tests together'
		if len(labels) > 1 else 'Existing tests', share(sum(f['existing']
		for f in counted), True)))
	lines.append('| This suite alone | {} |'.format(share(sum(f['suite']
		for f in counted))))
	lines.append('| **Existing tests and this suite** | {} |'.format(share(sum(
		f['both'] for f in counted), True)))

	# The files that gain the most, when parts hold several files each.
	parts_counted = {f['part'] for f in counted}

	if len(counted) > len(parts_counted):
		gains = sorted((f for f in counted if f['both'] > f['existing']),
			key=lambda f: (-(f['both'] - f['existing']), f['file']))[:GAINS_SHOWN]

		if gains:
			lines += ['', 'The files that gain the most:', '',
				'| File | Lines | Existing tests | With this suite |',
				'|---|---:|---:|---:|']

			for i, f in enumerate(gains):
				lines.append('| `{}` | {} | {} | {} (+{}{}) |'.format(f['file'],
					digits(f['lines']), percent(f['existing'], f['lines']),
					percent(f['both'], f['lines']), digits(f['both']
					- f['existing']), ' ' + ('line' if f['both'] - f['existing']
					== 1 else 'lines') if i == 0 else ''))

	return lines


def listed_file(ctx, text):
	"""Whether `text` names, in backticks, a file the tests branch holds: True,
	False, or None when there is nothing to check it against (no files.tsv
	and no branch.json)."""

	paths = [span[2:] if span.startswith('./') else span
		for span in CODE_SPAN.findall(text)]
	paths = [path for path in paths if '/' in path
		or re.search(r'\.[A-Za-z]\w*$', path)]

	if not paths:
		return False

	files = ctx.tables['files']

	if files and any(path in files for path in paths):
		return True

	branch = ctx.tables.get('branch') or {}

	if branch.get('suite') and branch.get('branch'):
		return any(publish.run(['git', '-C', branch['suite'], 'cat-file', '-e',
			'{}:{}'.format(branch['branch'], path)])[0] == 0 for path in paths)

	return None if files is None else False


def listing_problem(ctx, text):
	"""Why `contradictions` does not say where its cases are listed, or
	None."""

	if ctx.bugs:
		if REPORT_LINK.search(text):
			return None

		return ('prose "=== contradictions" names no bug report: write '
			'#<bug issue>, an issue number or a URL')

	if BUG_ISSUE in text or OTHER_LINK.search(text):
		# A #<bug issue> here is refused with every other one.
		return None

	found = listed_file(ctx, text)

	if found is None:
		sys.stderr.write('tests_pr_body: no files.tsv and no branch.json, so the '
			'file `contradictions` names was not checked against the tests '
			'branch\n')
		return None

	if found:
		return None

	return ('prose "=== contradictions" names no report and no file in the '
		'repository: the bug report is not among the documents chosen at step '
		'5, so write an issue number, a URL or the path of a file the tests '
		'branch holds, in backticks (files.tsv, or the branch in branch.json)')


def gaps(ctx):
	"""*What the suite does not cover*."""

	lines = ['## What the suite does not cover', '']
	docs = ctx.get('docs') or 'the documentation'
	contradictions = ctx.get('contradictions', why='*What the suite does not '
		'cover*') or ''

	if contradictions.strip().lower().rstrip('.') == 'none':
		lines.append('**No behaviour that contradicts {} was found.**'.format(docs))
	else:
		# Where the suite records each case differs between suites, so the
		# prose says it: a fixed sentence would be false for some of them.
		marked = (ctx.get('contradictions.marked', why='the list of untested '
			'behaviour in *What the suite does not cover*') or '').strip()

		if marked and not one_paragraph(marked):
			ctx.problems.add('prose "=== contradictions.marked" must be one '
				'paragraph of sentences, not a list: it sits inside the sentence '
				'before the list of cases')

		if '\n' in contradictions.strip() and LIST_ITEM.search(contradictions) \
				or LIST_ITEM.match(contradictions.strip()):
			ctx.problems.add('prose "=== contradictions" is a list: the cases '
				'belong in the bug report or the file that lists them, so write '
				'one paragraph that counts them and names it')
		else:
			problem = listing_problem(ctx, contradictions)

			if problem:
				ctx.problems.add(problem)

		lines.append('**Behaviour that contradicts {} is left untested**, so that '
			'fixing it will not break the suite. {}{}'.format(docs, marked.rstrip(
			'.') + '. ' if marked else '', ' '.join(contradictions.split())))

	names = ctx.tables['names']

	if names is not None:
		unnamed = [row for row in names if not row['tests']]
		lines.append('')

		if unnamed:
			shown = [code(row['name']) for row in unnamed[:NAMES_SHOWN]]
			more = len(unnamed) - NAMES_SHOWN
			lines.append('**Public names no test file names ({} of {}):** {}.'
				.format(digits(len(unnamed)), digits(len(names)), ', '.join(shown)
				+ ', and {} more'.format(digits(more)) if more > 0
				else publish.series(shown)))
		else:
			lines.append('**Every public name is named by at least one test '
				'file.**')

	coverage = ctx.tables['coverage']

	if coverage:
		counted = [f for f in coverage['files'] if not f['excluded']]
		total = sum(f['lines'] for f in counted)
		unreached = sorted((f for f in counted if f['both'] == 0 and f['lines']),
			key=lambda f: (-f['lines'], f['file']))
		reached = [f for f in counted if f['both']]
		lines += ['', '**Lines no test reaches:**', '']

		if unreached:
			lost = sum(f['lines'] for f in unreached)
			without = percent(sum(f['both'] for f in counted), total - lost)
			largest = ['`{}` ({}{})'.format(f['file'], digits(f['lines']), ' ' + (
				'line' if f['lines'] == 1 else 'lines') if i == 0 else '') for i, f in
				enumerate(unreached[:UNREACHED_SHOWN])]

			if len(unreached) == 1:
				lines.append('- **One file, {}, runs in no test at all.** Without '
					'it, coverage is {}.'.format(largest[0], without))
			elif len(unreached) <= UNREACHED_SHOWN:
				lines.append('- **{}, {}, run in no test at all:** {}. Without '
					'them, coverage is {}.'.format(capital(plural(len(unreached),
					'file')), lines_of(lost), publish.series(largest), without))
			else:
				lines.append('- **{}, {}, run in no test at all.** The largest '
					'are {}. Without them, coverage is {}.'.format(capital(plural(
					len(unreached), 'file')), lines_of(lost), publish.series(
					largest), without))
		else:
			lines.append('- **Every file runs in at least one test.**')

		widest = sorted((f for f in reached if f['lines'] > f['both']),
			key=lambda f: (-(f['lines'] - f['both']), f['file']))[:GAPS_SHOWN]

		if widest:
			lines.append('- **The largest gaps in files that are reached** are in '
				'{}.'.format(publish.series('`{}` ({}{}, {} covered)'.format(
				f['file'], digits(f['lines'] - f['both']), ' {} not run'.format(
				'line' if f['lines'] - f['both'] == 1 else 'lines') if i == 0
				else '', percent(f['both'], f['lines']))
				for i, f in enumerate(widest))))

	platforms = ctx.get('platforms', why='*What the suite does not cover*') or ''
	lines += ['', '**Platforms:** ' + platforms.strip()]

	return lines


SECTIONS = (
	('*Tests and line coverage by part*', 'coverage', parts_section),
	('*Running it*', None, running),
	('*How expected values are derived*', None, derivation),
	('*What it covers*', None, covers),
	('*Line coverage*', 'coverage', coverage_section),
	('*What the suite does not cover*', None, gaps),
)

SOURCES = {'coverage': 'coverage.tsv and parts.tsv'}


def render(tables, prose, problems):
	ctx = Context(tables, prose, problems)
	blocks = [opening(ctx)]
	dropped = []

	for title, needs, build in SECTIONS:
		if needs and not tables[needs]:
			dropped.append('{}: no {}'.format(title, SOURCES[needs]))
			continue

		blocks.append(build(ctx))

	if not ctx.bugs:
		for key, value in prose.items():
			if BUG_ISSUE in value:
				problems.add('prose "=== {}" holds {}, and the bug report is not '
					'among the documents chosen at step 5 (after.ship_documents), so '
					'that issue will not exist: name an issue number, a URL or a file '
					'in the repository instead'.format(' '.join(key).strip(),
					BUG_ISSUE))

	unused = [' '.join(key).strip() for key in prose if key not in ctx.used]
	text = '\n\n'.join('\n'.join(block) for block in blocks)
	text = re.sub(r'\n{3,}', '\n\n', text).strip('\n') + '\n'

	return text, dropped, unused


# The template ----------------------------------------------------------------

def template(tables):
	"""A prose file with one <<FILL: …>> per field these tables call for."""

	fields = [('opening', ''), ('support', ''), ('docs', ''),
		('running.commands', ''), ('running.notes', ''), ('running.ci', ''),
		('checked', ''), ('derivation', '')]
	fields += [('module', module) for module in sorted(tables['modules'])]

	if any(os.path.basename(path) in DEPENDENCY_MANIFESTS
			for path in tables['files'] or []):
		fields.append(('declares', ''))

	if tables['coverage']:
		fields.append(('coverage.method', ''))

		if not tables['coverage']['counted']:
			fields.append(('parts.tests', ''))

	fields += [('contradictions', ''), ('contradictions.marked', ''),
		('platforms', '')]
	out = []

	for field, argument in fields:
		out += ['=== {}{}'.format(field, ' ' + argument if argument else ''),
			'<<FILL: {}>>'.format(FIELDS[field][1]), '']

	return '\n'.join(out)


def read_config(path, problems):
	"""The search's config.json as a dict; {} when it does not exist."""

	try:
		with open(path, encoding='utf-8') as infile:
			config = json.load(infile)
	except FileNotFoundError:
		return {}
	except (OSError, ValueError) as error:
		problems.add('could not read {}: {}'.format(path, error))
		return {}

	return config if isinstance(config, dict) else {}


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('data', help='the folder of tables and prose, '
		'$WORK/ship/pr-tests')
	parser.add_argument('--prose', help='the prose file (default: DIR/prose.md)')
	parser.add_argument('--out', help='write the body here (default: stdout), '
		'and the title beside it as <out stem>.title; needs --title, unless a '
		'title is stored there')
	parser.add_argument('--title', help='the PR\'s title, such as "Add unit '
		'tests for pkg": a capital letter, no closing period, at most {} '
		'characters (default: the one stored beside --out)'.format(
		attribution.TITLE_LIMIT))
	parser.add_argument('--config', help='the search\'s config.json, whose '
		'after.tests_driver_edit allows a `driver` edit and whose '
		'after.ship_documents says whether the bug report is posted (default: '
		'DIR/../../autooptimize/config.json, when it exists)')
	parser.add_argument('--allow-source', action='append', default=[],
		metavar='PATH', help='a changed file outside the tests, other than a '
		'manifest or the test driver, that the user approved by name (repeatable). '
		'`declares` must also name it; the opening then lists it instead of '
		'claiming no source changes. Record their words in spec.md first')
	parser.add_argument('--template', action='store_true', help='print a prose '
		'skeleton for these tables instead of a body')
	args = parser.parse_args(argv)

	problems = Problems()
	tables = load(args.data, problems)
	tables['allow_source'] = args.allow_source
	config = read_config(args.config or os.path.join(args.data, '..', '..',
		'autooptimize', 'config.json'), problems)
	after = config.get('after') or {}
	tables['driver_edit'] = after.get('tests_driver_edit')

	if args.config:
		documents = list(after.get('ship_documents') or publish.FOLDERS)
	else:
		documents = publish.chosen(args.data)

	tables['bugs'] = 'bugs' in documents

	if args.template:
		if problems:
			sys.stderr.write('tests_pr_body: the tables have problems the body '
				'will refuse:\n  {}\n'.format('\n  '.join(problems)))

		sys.stdout.write(template(tables))
		return 0

	prose = read_prose(args.prose or os.path.join(args.data, 'prose.md'),
		problems)
	text, dropped, unused = render(tables, prose, problems)
	text = publish.fill_numbers(text, args.data, problems)

	return publish.emit('tests_pr_body', text, problems, args.title, args.out,
		dropped, unused)


if __name__ == '__main__':
	sys.exit(main())
