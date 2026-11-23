#!/usr/bin/env python3
"""Render the optimization PR's body from its tables and its prose.

	python3 opt_pr_body.py DIR --template > DIR/prose.md
	python3 opt_pr_body.py DIR --title 'Vectorize column scoring' --out DIR/body.md

DIR holds the tables `opt_pr_tables.py` wrote and one prose file,
`prose.md`, of `=== <field>` blocks. Every number in the body is computed from
the tables, so the same tables and prose give the same body, byte for byte;
prose writes `{{token}}` where it needs one: `{{target}}` (the branch, in
backticks), `{{commits}}`, `{{files}}`, `{{runs}}`, `{{first}}` and `{{kept}}`
(the objective's median on the target and on this branch), `{{speedup}}`. `--out` writes the body and,
beside it, the title as `<out stem>.title`. `--template` prints the prose
skeleton for the tables DIR holds.

Exits 1, writes nothing and lists every problem on stderr when the prose or
the tables are wrong. `references/pr-optimization.md` describes every field.

Standard library only.
"""

import os
import re
import sys
import json
import math
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import attribution
import publish

# What each prose field holds, as --template explains it:
# (takes an argument, text).
FIELDS = {
	'summary': (False, 'one or two sentences: what this PR changes in the '
		'code ("Replaces the per-record loop in `score()` with array '
		'operations."). The renderer follows it with the measured change'),
	'input': (False, 'the input the main measurement ran on, as a noun phrase '
		'that follows "On" ("a 10 Mbp DNA sequence with about 1% N")'),
	'row': (True, 'after the measure\'s name (`=== row seconds`): the '
		'Measure cell of its row in the Performance table, saying what is '
		'measured on which input and, when the table cannot, in which unit '
		'("Peak resident memory (MB), 10 Mbp sequence")'),
	'timed': (False, 'what one run measures: the call or command, what is '
		'warmed up or cached first, and what the time covers'),
	'machine': (False, 'the machine the numbers came from: CPU, cores, OS, '
		'Python. Used only when the tables do not record it'),
	'outliers': (False, 'each row the kept code made worse, and why. Required '
		'when a row is worse beyond the run-to-run spread'),
	'cold': (False, 'why the first call in a new process takes longer on this '
		'branch (a compile, a cache to fill). Required when it takes more '
		'than twice as long'),
	'portability': (False, 'the OS-specific calls and the constants fitted to '
		'one machine that the change adds, or that it adds none'),
	'vendored': (False, 'code copied or translated from another project: '
		'files, project, version and license; or the single word none'),
	'change': (True, 'after the entry\'s short name (`=== change Vectorize '
		'scoring`): a first line `iterations: A4, A11` naming each kept change '
		'the entry covers (never printed), then what the change does as it '
		'stands in this branch, read from the diffs; sub-bullets allowed'),
	'behaviour': (False, 'every difference a user can see, one bullet each: '
		'output, messages, threads, files written, defaults, behaviour on bad '
		'input; or one sentence saying there are none'),
	'tests': (False, 'the repository\'s test suite and how it was run, as the '
		'subject of "passed on this branch" ("The testthat suite '
		'(`devtools::test()`)"); with no unit tests, optionally why'),
	'gate': (True, 'after the check\'s name (`=== gate n_mismatch`): what '
		'the check compares, as a clause ("the number of positions where the '
		'output array differs from the one `main` returns")'),
	'compare': (False, 'the further inputs the separate output comparison '
		'ran on, as a noun phrase ("57 inputs: every documented option on '
		'three sequence files")'),
}
OPTIONAL = ('machine', 'outliers', 'cold', 'tests')
TOKEN = re.compile(r'\{\{([a-z_]+)\}\}')
LIST_ITEM = re.compile(r'^\s*([-*]|\d+\.)\s')


class Problems(list):
	def add(self, problem):
		if problem not in self:
			self.append(problem)


def read_json(directory, name, problems, required=True):
	path = os.path.join(directory, name)

	try:
		with open(path, encoding='utf-8') as handle:
			return json.load(handle)
	except OSError:
		if required:
			problems.add('{} is missing: run opt_pr_tables.py'.format(path))
	except ValueError as error:
		problems.add('{}: {}'.format(path, error))

	return None


def read_table(directory, name, problems):
	"""The rows of a tab-separated table with a header, as dicts."""

	path = os.path.join(directory, name)

	try:
		with open(path, encoding='utf-8') as handle:
			lines = [line.rstrip('\n') for line in handle if line.strip()]
	except OSError:
		problems.add('{} is missing: run opt_pr_tables.py'.format(path))
		return []

	header = lines[0].split('\t')
	rows = []

	for number, line in enumerate(lines[1:], 2):
		cells = line.split('\t')

		if len(cells) != len(header):
			problems.add('{}:{}: {} cells, the header has {}'.format(path,
				number, len(cells), len(header)))
			continue

		rows.append(dict(zip(header, cells)))

	return rows


def read_prose(path, problems):
	"""`{(field, argument or None): text}` in file order."""

	try:
		with open(path, encoding='utf-8') as handle:
			text = handle.read()
	except OSError:
		problems.add('{} is missing: `opt_pr_body.py DIR --template > {}`, '
			'then fill it'.format(path, path))
		return {}

	fields, current = {}, None

	for number, line in enumerate(text.splitlines(), 1):
		if line.startswith('=== '):
			name, _, argument = line[4:].strip().partition(' ')
			argument = argument.strip() or None
			current = (name, argument)

			if name not in FIELDS:
				problems.add('prose.md:{}: unknown field "{}"'.format(number,
					name))
			elif FIELDS[name][0] and not argument:
				problems.add('prose.md:{}: "=== {}" needs a name after it'
					.format(number, name))
			elif not FIELDS[name][0] and argument:
				problems.add('prose.md:{}: "=== {}" takes nothing after it'
					.format(number, name))

			if current in fields:
				problems.add('prose.md:{}: "{}" appears twice'.format(number,
					line.strip()))

			fields[current] = []
			continue

		if current is None:
			if line.strip():
				problems.add('prose.md:{}: text before the first field'.format(
					number))
			continue

		fields[current].append(line)

	prose = {}

	for key, lines in fields.items():
		value = '\n'.join(lines).strip('\n')
		where = '"=== {}"'.format(' '.join(part for part in key if part))

		if not value.strip():
			problems.add('{} is empty'.format(where))
		elif '<<FILL' in value:
			problems.add('{} still holds <<FILL'.format(where))
		elif any(line.lstrip().startswith('#') for line in lines):
			problems.add('{} holds a heading; fields hold sentences'.format(
				where))

		prose[key] = value

	return prose


def sig(value, digits=3):
	"""`value` to `digits` significant figures: commas from 1,000, no
	exponent for an ordinary number."""

	if value == 0:
		return '0'

	magnitude = int(math.floor(math.log10(abs(value))))

	if magnitude >= digits - 1:
		return '{:,.0f}'.format(round(value, digits - 1 - magnitude))

	text = '{:.{}f}'.format(value, digits - 1 - magnitude)

	return text if 'e' not in text else '{:.{}g}'.format(value, digits)


def seconds(value):
	for scale, unit in ((1.0, 's'), (1e-3, 'ms'), (1e-6, 'µs')):
		if abs(value) >= scale:
			return '{} {}'.format(sig(value / scale), unit)

	return '{} ns'.format(sig(value / 1e-9))


def is_time(name, unit):
	return unit == 's' or name == 'seconds' or name.startswith('seconds_') \
		or name.startswith('time')


def quantity(value, name, unit):
	if is_time(name, unit):
		return seconds(value)

	return sig(value) + (' ' + unit if unit else '')


def change(row, direction):
	"""`(text, worse)`: the kept code against the first measurement, and
	whether it is worse beyond the run-to-run spread (None: no direction)."""

	first, kept = row['first_median'], row['kept_median']
	time = is_time(row['measure'], row['unit'])

	if time and first > 0 and kept > 0:
		ratio = first / kept
		text = '{}x faster'.format(sig(ratio)) if ratio >= 1 else \
			'{}x slower'.format(sig(1 / ratio))
		return text, kept > first and row['kept_min'] > row['first_max']

	if first == 0:
		return 'from 0', None

	percent = (kept - first) / abs(first) * 100
	text = '{:+.{}f}%'.format(percent, 1 if abs(percent) < 10 else 0)

	if direction is None:
		return text, None

	worse = (kept > first) if direction == 'min' else (kept < first)
	beyond = (row['kept_min'] > row['first_max']) if direction == 'min' else \
		(row['kept_max'] < row['first_min'])

	return text, worse and beyond


def details(summary, lines):
	return ['<details>', '<summary>{}</summary>'.format(summary), ''] + lines + [
		'', '</details>']


def paragraph(text):
	"""A prose field joined into one paragraph, unless it is a list."""

	if any(LIST_ITEM.match(line) for line in text.split('\n')):
		return text

	return ' '.join(text.split())


class Context:
	def __init__(self, directory):
		self.directory = directory
		self.problems = Problems()
		self.used = set()
		self.numbers = set()
		problems = self.problems
		self.branch = read_json(directory, 'branch.json', problems) or {}
		self.changes = read_table(directory, 'changes.tsv', problems)
		self.files = read_table(directory, 'files.tsv', problems)
		self.performance = read_table(directory, 'performance.tsv', problems)
		self.gates = read_table(directory, 'gates.tsv', problems)
		self.checks = read_json(directory, 'checks.json', problems) or {}
		self.paired = read_json(directory, 'paired.json', problems)
		self.machine = read_json(directory, 'machine.json', problems)
		self.tests = read_json(directory, 'tests.json', problems)
		self.prose = read_prose(os.path.join(directory, 'prose.md'), problems)
		self.documents = self.branch.get('documents') or publish.chosen(
			directory)

		for row in self.performance:
			for key in ('first_median', 'first_min', 'first_max', 'kept_median',
					'kept_min', 'kept_max'):
				row[key] = float(row[key])

	def field(self, name, argument=None, required=True):
		key = (name, argument)

		if key not in self.prose:
			if required:
				self.problems.add('prose.md has no "=== {}" field{}'.format(
					' '.join(part for part in key if part), '' if FIELDS[name][
					0] else ''))
			return None

		self.used.add(key)
		return self.fill(self.prose[key], key)

	def fill(self, text, key):
		def one(match):
			name = match.group(1)

			if name not in self.tokens:
				self.problems.add('"=== {}" uses {{{{{}}}}}, which no table '
					'gives ({})'.format(' '.join(part for part in key if part),
					name, ', '.join(sorted(self.tokens))))
				return match.group(0)

			return self.tokens[name]

		return TOKEN.sub(one, text)

	def target(self):
		return '`{}`'.format(self.branch.get('target') or 'the target branch')

	def note(self, text):
		"""`text`, a number the tables give: prose may not type it."""

		self.numbers.add(text)
		return text


def objective_row(ctx):
	name = (ctx.branch.get('objective') or {}).get('name')

	return next((row for row in ctx.performance if row['role'] ==
		'objective' and row['measure'] == name), None)


def opening(ctx):
	"""The summary, the measured change on the main input, and where the
	output checks are."""

	objective = ctx.branch.get('objective') or {}
	row = objective_row(ctx)
	sentences = [paragraph(ctx.field('summary') or '')]
	main_input = ctx.field('input')

	if row is None:
		ctx.problems.add('performance.tsv has no row for the objective {}'
			.format(objective.get('name')))
		return ' '.join(sentences)

	text, _ = change(row, objective.get('direction'))
	runs = int(row['kept_runs'])
	time = is_time(row['measure'], row['unit'])
	what = 'time' if time else '`{}`'.format(row['measure'])
	measured = 'On {}, the median {} of {} runs goes from {} on {} to {} on ' \
		'this branch{}'.format(main_input or '', what, ctx.note(str(runs)),
		ctx.note(quantity(row['first_median'], row['measure'], row['unit'])),
		ctx.target(), ctx.note(quantity(row['kept_median'], row['measure'],
		row['unit'])), ', ' + ctx.note(text) if time else ' ({})'.format(
		ctx.note(text)))
	paired = (ctx.paired or {}).get('paired') or {}

	if paired.get('pairs'):
		measured += '; in {} alternating runs of the two, this branch was ' \
			'faster in {} (sign test, p = {})'.format(ctx.note(str(paired[
			'pairs'])), ctx.note(str(paired['wins'])), ctx.note(sig(paired[
			'p'], 2)))

	sentences.append(measured + '.')

	if ctx.gates:
		checked_text = 'Its output was checked against the output of {} in ' \
			'every run'.format(ctx.target())
	elif ctx.checks.get('compare'):
		checked_text = 'Its output was compared with the output of {} on ' \
			'further inputs'.format(ctx.target())
	else:
		checked_text = 'No check compared its output with the output of {}' \
			.format(ctx.target())

	sentences.append('{}; see [How the output was checked]{}.'.format(
		checked_text, attribution.CHECKED_LINK))

	return ' '.join(sentences)


def scope(ctx):
	"""What the PR targets and holds, and how it stands to the unit tests."""

	added = sum(int(row['added']) for row in ctx.files)
	deleted = sum(int(row['deleted']) for row in ctx.files)
	commits = len(ctx.changes)
	lines = ['It targets {}. It is {} commit{} changing {} file{}, +{:,} / '
		'-{:,}.'.format(ctx.target(), ctx.note(str(commits)), attribution.plural(
		commits), ctx.note(str(len(ctx.files))), attribution.plural(len(
		ctx.files)), added, deleted)]

	if ctx.branch.get('suite'):
		if 'tests' in ctx.documents:
			lines.append('It is independent of the unit tests in {}: it '
				'changes none of their files, and it was measured with them in '
				'place.'.format(attribution.PLACEHOLDERS[1]))
		else:
			lines.append('It was measured with a new unit-test suite in place, '
				'which is not part of this PR; it changes none of that suite\'s '
				'files.')

	vendored = ctx.field('vendored')

	if vendored and vendored.strip().lower().rstrip('.') == 'none':
		lines.append('It adds no code from another project.')
	elif vendored:
		lines.append(paragraph(vendored))

	return ' '.join(lines)


def performance(ctx):
	direction = (ctx.branch.get('objective') or {}).get('direction')
	rows, worse = [], []

	for row in ctx.performance:
		label = ctx.field('row', row['measure'])
		text, bad = change(row, direction if row['role'] == 'objective' else
			('min' if is_time(row['measure'], row['unit']) else None))

		if bad:
			worse.append(label or row['measure'])

		rows.append((paragraph(label or row['measure']), ctx.note(quantity(
			row['first_median'], row['measure'], row['unit'])), ctx.note(
			quantity(row['kept_median'], row['measure'], row['unit'])),
			ctx.note(text)))

	lines = ['## Performance', ''] + publish.table(('Measure', ctx.target(),
		'This branch', 'Change'), rows, ['---', '---:', '---:', '---:'])
	counts = {(int(row['first_runs']), int(row['kept_runs'])) for row in
		ctx.performance}

	if len(counts) == 1:
		first, kept = counts.pop()
		lines += ['', 'Each value is the median of {} run{}{}.'.format(
			ctx.note(str(kept)), attribution.plural(kept), '' if first == kept
			else ' of this branch and {} of {}'.format(ctx.note(str(first)),
			ctx.target()))]

	outliers = ctx.field('outliers', required=bool(worse))

	if worse and outliers:
		lines += ['', paragraph(outliers)]
	elif outliers:
		lines += ['', paragraph(outliers)]

	cold = ctx.checks.get('cold') or {}

	if cold.get('kept') is not None:
		sentence = 'The first call in a new process took {} on this branch'\
			.format(ctx.note(seconds(cold['kept'])))

		if cold.get('first') is not None:
			sentence += ' and {} on {}'.format(ctx.note(seconds(cold['first'])),
				ctx.target())

		slower = cold.get('first') is not None and cold['kept'] > 2 * cold[
			'first']
		why = ctx.field('cold', required=slower)
		lines += ['', sentence + '.' + (' ' + paragraph(why) if why else '')]

	lines += [''] + details('How it was measured', measured(ctx))

	return lines


def measured(ctx):
	lines = []
	machine = ctx.machine

	if machine:
		parts = [machine.get('distribution') or ' '.join(part for part in (
			machine.get('os'), machine.get('release')) if part)]
		parts += [machine.get('arch'), machine.get('cpu')]
		cores = machine.get('cores')
		text = ', '.join(part for part in parts if part)

		if cores:
			text += ', {} cores'.format(cores)

		if machine.get('python'):
			text += '; Python {}'.format(machine['python'])

		lines.append('- **Machine:** {}.'.format(text))
	else:
		lines.append('- **Machine:** {}'.format(paragraph(ctx.field('machine')
			or '')))

	lines.append('- **What a run measures:** {}'.format(paragraph(ctx.field(
		'timed') or '')))
	lines.append('- **Runs:** each build ran with a different seed in each '
		'run, in its own process; the table above gives the medians, and this '
		'one the range of each:')
	lines.append('')
	lines += ['  ' + line for line in publish.table(('Measure',
		'{}, min to max'.format(ctx.target()), 'This branch, min to max'), [(
		paragraph(ctx.prose.get(('row', row['measure']), row['measure'])),
		'{} to {}'.format(quantity(row['first_min'], row['measure'],
		row['unit']), quantity(row['first_max'], row['measure'], row['unit'])),
		'{} to {}'.format(quantity(row['kept_min'], row['measure'], row[
		'unit']), quantity(row['kept_max'], row['measure'], row['unit']))) for
		row in ctx.performance], ['---', '---:', '---:'])]
	paired = ctx.paired or {}

	for key, where in (('paired', ''), ('threads', ' at {} threads')):
		entry = paired.get(key) or {}

		if not entry.get('pairs'):
			continue

		lines.append('')
		lines.append('- **Paired runs{}:** the two builds ran one after the '
			'other in {} pairs of separate processes, on the same input; this '
			'branch was faster in {} of them (sign test, p = {}).'.format(
			where.format(entry.get('threads')), entry['pairs'], entry['wins'],
			sig(entry['p'], 2)))

	return lines


def entries(ctx):
	"""`[(first position, name, iterations, files, text)]` from the `change`
	fields, checked against the kept changes: each in one entry, every file
	the PR changes named by some entry."""

	steps = [str(step) for step in ctx.branch.get('steps') or []]
	labels = dict(zip(ctx.branch.get('step_labels') or [], steps))
	order = {step: at for at, step in enumerate(steps)}
	own = {row['id']: [path for path in row['files'].split(',') if path] for
		row in ctx.changes}
	final = [row['path'] for row in ctx.files]
	made, covered = [], {}

	for key in [key for key in ctx.prose if key[0] == 'change']:
		ctx.used.add(key)
		name = key[1]
		head, _, rest = ctx.prose[key].partition('\n')
		match = re.match(r'^iterations:\s*(.+)$', head.strip())
		where = '"=== change {}"'.format(name)

		if '<' in name:
			ctx.problems.add('{} still holds a placeholder name'.format(where))

		if not match or not rest.strip():
			ctx.problems.add('{} needs a first line `iterations: A4, A11` and '
				'then what the change does'.format(where))
			continue

		named = [part.strip() for part in re.split(r'[,\s]+', match.group(1))
			if part.strip()]
		unknown = [one for one in named if one not in labels]

		if unknown:
			ctx.problems.add('{} names {}, which is not a kept change ({})'
				.format(where, ', '.join(unknown), ', '.join(labels)))

		for one in named:
			if one in covered:
				ctx.problems.add('{} covers {} again; {} has it already'
					.format(where, one, covered[one]))

			covered[one] = where

		ids = [labels[one] for one in named if one in labels]
		files = [path for path in final if any(path in own.get(i, []) for i in
			ids)]

		if ids and not files:
			ctx.problems.add('{} covers no file the PR changes: a change later '
				'undone belongs in the entry that undid it'.format(where))

		if ids:
			made.append((min(order[i] for i in ids), name, sorted(order[i] + 1
				for i in ids), files, rest.strip('\n')))

	missing = [one for one in labels if one not in covered]

	if missing:
		ctx.problems.add('no "=== change" field covers {}; every kept change is '
			'in one entry'.format(', '.join(missing)))

	unnamed = [path for path in final if not any(path in entry[3] for entry in
		made)]

	if unnamed and made:
		ctx.problems.add('no change entry names {}: say which entry changed '
			'it'.format(', '.join(unnamed)))

	return sorted(made)


def changes(ctx):
	lines = ['## Changes', '', 'In the order they were made, each with its '
		'commits. Open a change for its files and what it does.', '']

	for number, (_, name, commits, files, text) in enumerate(entries(ctx), 1):
		first, _, rest = text.partition('\n')
		where = ' (commit{} {})'.format('' if len(commits) == 1 else 's',
			', '.join('{{{{commit:{}}}}}'.format(at) for at in commits))
		body = ['Files: {}.'.format(', '.join('`{}`'.format(path) for path in
			files)), '', ctx.fill(first.strip(), ('change', name))]

		for line in rest.split('\n'):
			if line.strip():
				body.append(ctx.fill(line.strip() if LIST_ITEM.match(line) and
					not line.startswith(' ') else line.rstrip(), ('change',
					name)))

		lines.append('{}. **{}**{}'.format(number, name, where))
		lines.append('')
		lines += ['   ' + line if line else '' for line in details('Details',
			body)]
		lines.append('')

	return lines[:-1]


def behaviour(ctx):
	return ['## Behaviour changes', '', ctx.field('behaviour') or '']


def tests(ctx):
	lines = ['## Tests', '']
	record = ctx.tests

	if not record:
		why = ctx.field('tests', required=False)
		return lines + ['No unit tests ran on this branch.' + (' ' + paragraph(
			why) if why else '')]

	if not record.get('pass'):
		ctx.problems.add('tests.json: the unit tests did not pass on the kept '
			'code')

	counts = record.get('counts')
	sentence = '{} passed on this branch'.format(paragraph(ctx.field('tests')
		or '').rstrip('.'))

	if counts:
		failed = counts['failed'] + counts.get('errors', 0)
		sentence += ': {} test{} passed, {} {} skipped and {} failed'.format(
			ctx.note('{:,}'.format(counts['passed'])), attribution.plural(
			counts['passed']), ctx.note('{:,}'.format(counts['skipped'])),
			'was' if counts['skipped'] == 1 else 'were', '{:,}'.format(failed))

	sentence += '.'

	if ctx.branch.get('suite'):
		sentence += ' The run included the new unit tests{}.'.format(' of ' +
			attribution.PLACEHOLDERS[1] if 'tests' in ctx.documents else
			', which are not part of this PR')

	return lines + [sentence]


def checked(ctx):
	lines = [attribution.CHECKED_HEADING, '']
	items = []
	runs = {int(row['runs']) for row in ctx.gates if row['runs'].isdigit()}

	for row in ctx.gates:
		what = ctx.field('gate', row['name'])
		condition = '`{} {} {}`'.format(row['name'], row['op'],
			row['threshold'])
		worst = row['worst']

		try:
			worst = '{:.4g}'.format(float(worst))
		except ValueError:
			pass

		items.append('- {}, {}: it held in every run; the worst value was {}.'
			.format(condition, (what or '').rstrip('.'), worst))

	compare = ctx.checks.get('compare') or {}

	if compare:
		if compare.get('verdict') != 'pass':
			ctx.problems.add('checks.json: the output comparison of the kept '
				'code did not pass ({})'.format(compare.get('verdict')))

		inputs = ctx.field('compare')
		items.append('- A separate comparison of the two builds, on {}, '
			'reported "{}".'.format((inputs or '').rstrip('.'), compare.get(
			'summary') or 'pass'))

	if ctx.tests and ctx.tests.get('pass'):
		items.append('- The unit tests, under [Tests](#tests).')

	if ctx.gates:
		lines.append('Each of this branch\'s {} run{} compared its output with '
			'the output of {}:'.format(ctx.note(str(min(runs))) if runs else
			'measured', attribution.plural(min(runs)) if runs else 's',
			ctx.target()))
	elif items:
		lines.append('The output was compared with the output of {}:'.format(
			ctx.target()))
	else:
		lines.append('No check compared the output with the output of {}.'
			.format(ctx.target()))

	if items:
		lines += [''] + items

	if 'bugs' in ctx.documents:
		lines += ['', 'Bugs found in {} along the way are reported in {}.'
			.format(ctx.target(), attribution.PLACEHOLDERS[0])]

	return lines


def typed_numbers(ctx):
	"""A number of three or more digits the tables give, typed in prose."""

	numbers = {number for number in ctx.numbers if sum(c.isdigit() for c in
		number) >= 3}

	for key, text in ctx.prose.items():
		if key[0] in ('change',):
			text = text.partition('\n')[2]

		for number in sorted(numbers):
			core = re.sub(r'[^0-9.,]', '', number).strip('.,')

			if core and re.search(r'(?<![0-9.,]){}(?![0-9])'.format(re.escape(
					core)), text):
				ctx.problems.add('"=== {}" types {}, which the tables give: '
					'write the token instead, or leave it to the table'.format(
					' '.join(part for part in key if part), core))


def render(ctx):
	ctx.tokens = {'target': ctx.target(), 'commits': str(len(ctx.changes)),
		'files': str(len(ctx.files))}
	row = objective_row(ctx)

	if row is not None:
		ctx.tokens.update(first=quantity(row['first_median'], row['measure'],
			row['unit']), kept=quantity(row['kept_median'], row['measure'],
			row['unit']), speedup=change(row, (ctx.branch.get('objective') or
			{}).get('direction'))[0], runs=str(int(row['kept_runs'])))
	parts = [opening(ctx), paragraph(ctx.field('portability') or ''),
		scope(ctx), '\n'.join(performance(ctx)), '\n'.join(changes(ctx)),
		'\n'.join(behaviour(ctx)), '\n'.join(tests(ctx)), '\n'.join(checked(
		ctx))]
	typed_numbers(ctx)
	text = '\n\n'.join(part for part in parts if part) + '\n'
	problems = Problems()
	text = publish.fill_numbers(text, ctx.directory, problems)

	for problem in problems:
		ctx.problems.add(problem)

	for problem in attribution.opening_problems('optimization', text):
		ctx.problems.add('body: ' + problem)

	if 'tests' not in ctx.documents and attribution.PLACEHOLDERS[1] in text:
		ctx.problems.add('the body names the unit-test PR, which was not chosen')

	if 'bugs' not in ctx.documents and attribution.PLACEHOLDERS[0] in text:
		ctx.problems.add('the body names the bug report, which was not chosen')

	return text


def template(ctx):
	"""The prose skeleton: every field the tables call for, each change
	entry started from what the kept change's own notes say."""

	fragments = read_json(ctx.directory, 'fragments.json', Problems(),
		required=False) or {}
	steps = ctx.branch.get('steps') or []
	labels = ctx.branch.get('step_labels') or []
	out = []

	def put(key, text):
		out.append('=== ' + ' '.join(part for part in key if part))
		out.append(text)
		out.append('')

	def fill(name):
		return '<<FILL: {}>>'.format(FIELDS[name][1])

	for name in ('summary', 'input'):
		put((name,), fill(name))

	for row in ctx.performance:
		put(('row', row['measure']), fill('row'))

	put(('timed',), fill('timed'))

	if not ctx.machine:
		put(('machine',), fill('machine'))

	direction = (ctx.branch.get('objective') or {}).get('direction')

	if any(change(row, direction if row['role'] == 'objective' else ('min' if
			is_time(row['measure'], row['unit']) else None))[1] for row in
			ctx.performance):
		put(('outliers',), fill('outliers'))

	cold = ctx.checks.get('cold') or {}

	if cold.get('kept') is not None and cold.get('first') is not None and \
			cold['kept'] > 2 * cold['first']:
		put(('cold',), fill('cold'))

	for name in ('portability', 'vendored'):
		put((name,), fill(name))

	for step, label in zip(steps, labels):
		fragment = fragments.get(str(step)) or {}
		name = fragment.get('name') or '<short name for {}>'.format(label)
		put(('change', name), 'iterations: {}\n{}'.format(label, fragment.get(
			'change') or fill('change')))

	put(('behaviour',), fill('behaviour'))

	if ctx.tests:
		put(('tests',), fill('tests'))

	for row in ctx.gates:
		put(('gate', row['name']), fill('gate'))

	if ctx.checks.get('compare'):
		put(('compare',), fill('compare'))

	return '\n'.join(out)


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('directory')
	parser.add_argument('--title', help='the PR\'s title: an imperative, a '
		'capital letter, no closing period, at most 72 characters')
	parser.add_argument('--out', help='write the body here and the title '
		'beside it; without it the body goes to stdout')
	parser.add_argument('--template', action='store_true', help='print the '
		'prose skeleton')
	args = parser.parse_args(argv)
	directory = os.path.abspath(os.path.expanduser(args.directory))

	if args.template:
		ctx = Context.__new__(Context)
		ctx.directory = directory
		problems = Problems()
		ctx.branch = read_json(directory, 'branch.json', problems) or {}
		ctx.performance = read_table(directory, 'performance.tsv', problems)
		ctx.gates = read_table(directory, 'gates.tsv', problems)
		ctx.checks = read_json(directory, 'checks.json', problems) or {}
		ctx.machine = read_json(directory, 'machine.json', problems)
		ctx.tests = read_json(directory, 'tests.json', problems)

		for row in ctx.performance:
			for key in ('first_median', 'first_min', 'first_max', 'kept_median',
					'kept_min', 'kept_max'):
				row[key] = float(row[key])

		if problems:
			return publish.refuse('opt_pr_body', problems)

		sys.stdout.write(template(ctx))
		return 0

	ctx = Context(directory)
	text = render(ctx) if not ctx.problems or ctx.branch else ''
	unused = [' '.join(part for part in key if part) for key in ctx.prose if
		key not in ctx.used]

	return publish.emit('opt_pr_body', text, ctx.problems, args.title,
		args.out, unused=unused, labels=ctx.branch.get('labels'))


if __name__ == '__main__':
	sys.exit(main())
