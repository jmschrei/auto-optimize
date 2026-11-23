#!/usr/bin/env python3
"""The tables the unit-test PR's body is rendered from.

	python3 tests_pr_tables.py collection --collect-output collect.txt --root SUITE --out DIR
	python3 tests_pr_tables.py files --repo SUITE --base COMMIT --head BRANCH --out DIR
	python3 tests_pr_tables.py coverage --existing 'make test=base.json' --suite suite.json --out DIR
	python3 tests_pr_tables.py names --source pkg --tests tests --out DIR

Each subcommand reads what step 1's runs left (pytest's collection, git,
gcovr or coverage.py JSON, the package's source) and writes a tab-separated
table, with a header line, into the folder DIR, `$WORK/ship/pr-tests`.
`tests_pr_body.py DIR` renders the body from them and from one prose file.
Every format, and which section each table feeds, is in
references/pr-tests.md; `--help` has every flag.

Standard library only, Python 3.9 or later. The same inputs give the same
tables, byte for byte.
"""

import os
import re
import ast
import sys
import json
import fnmatch
import argparse
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import publish


# How a test function says it records the current output rather than a
# derived value (references/step1-unit-tests.md, *Where expected values come
# from*): "pins the current output", "pin the current values", "pins current
# behaviour". Searched in the function's source: its docstring and comments.
MARKER = r'\bpins?\b(?:\W+\w+){0,6}?\W+current\b'

# Parts per file up to this many source files, per directory above it
# (references/step1-unit-tests.md, *Report its coverage*).
PARTS_PER_FILE = 30

FOLDER = '$WORK/ship/pr-tests'


def fail(message):
	publish.fail('tests_pr_tables', message)


def note(message):
	sys.stderr.write('tests_pr_tables: {}\n'.format(message))


def clean(value):
	"""A table cell: tabs and line breaks would split the row."""

	return re.sub(r'[\t\r\n]+', ' ', str(value))


def write_tsv(directory, name, header, rows):
	"""Replace `directory/name` with the table, whole or not at all."""

	os.makedirs(directory, exist_ok=True)
	path = os.path.join(directory, name)
	publish.write(path, '\t'.join(header) + '\n' + ''.join('\t'.join(clean(
		value) for value in row) + '\n' for row in rows))

	return path


def read_tsv(path, required):
	"""Rows of a tab-separated file with a header, as dicts; refuses a
	missing column or a row of the wrong width."""

	try:
		with open(path, encoding='utf-8') as infile:
			lines = [line.rstrip('\r\n') for line in infile]
	except OSError as error:
		fail('could not read {}: {}'.format(path, error))

	lines = [line for line in lines if line.strip()
		and not line.lstrip().startswith('#')]

	if not lines:
		fail('{} is empty: it needs a header line ({})'.format(path,
			'\t'.join(required)))

	header = [field.strip() for field in lines[0].split('\t')]
	missing = [column for column in required if column not in header]

	if missing:
		fail('{} has no {} column: the header is tab-separated and holds {}'
			.format(path, ', '.join(missing), ', '.join(required)))

	rows = []

	for number, line in enumerate(lines[1:], 2):
		fields = line.split('\t')

		if len(fields) > len(header):
			fail('{} line {}: {} fields, where the header has {}'.format(path,
				number, len(fields), len(header)))

		fields += [''] * (len(header) - len(fields))
		rows.append(dict(zip(header, [field.strip() for field in fields])))

	return rows


# collection ------------------------------------------------------------------

def parse_collection(text):
	"""Node ids and the selected count from `pytest --collect-only -q`."""

	nodeids, started = [], False

	for line in text.splitlines():
		if not line.strip() or line.startswith('=') or line[0].isspace():
			if started:
				break
			continue

		if '::' in line:
			nodeids.append(line.rstrip())
			started = True
		elif started:
			break

	summary = [line for line in text.splitlines()
		if re.search(r'\btests? collected\b|\bno tests collected\b', line)]

	return nodeids, summary[-1].strip(' =') if summary else ''


def function_texts(path):
	"""{qualified name: source text} of every function in a test module."""

	try:
		with open(path, encoding='utf-8') as infile:
			source = infile.read()
	except OSError as error:
		fail('could not read {}: {}'.format(path, error))

	try:
		tree = ast.parse(source)
	except SyntaxError as error:
		fail('could not parse {}: {}'.format(path, error))

	lines = source.splitlines()
	found = {}

	def visit(body, prefix):
		for node in body:
			if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
				found[prefix + node.name] = '\n'.join(
					lines[node.lineno - 1:node.end_lineno])
			elif isinstance(node, ast.ClassDef):
				visit(node.body, prefix + node.name + '.')

	visit(tree.body, '')
	return found


def collection(args):
	try:
		with open(args.collect_output, encoding='utf-8') as infile:
			text = infile.read()
	except OSError as error:
		fail('could not read {}: {}'.format(args.collect_output, error))

	nodeids, summary = parse_collection(text)

	if not nodeids:
		fail('{} lists no test: it must be the output of `pytest '
			'--collect-only -q`, one node id per line'.format(args.collect_output))

	if not summary:
		fail('{} has no "N tests collected" line: save the whole output of '
			'`pytest --collect-only -q`'.format(args.collect_output))

	if re.search(r'\berrors?\b', summary):
		fail('the collection had errors ({}): fix them before counting'.format(
			summary))

	counted = re.search(r'(\d+)(?:/\d+)? tests? collected', summary)

	if not counted or int(counted.group(1)) != len(nodeids):
		fail('{} lists {} node ids, but pytest says "{}"'.format(
			args.collect_output, len(nodeids), summary))

	if len(set(nodeids)) != len(nodeids):
		twice = sorted({n for n in nodeids if nodeids.count(n) > 1})
		fail('node ids collected twice: {}'.format(', '.join(twice[:5])))

	pinned = {}

	if args.root:
		marker = re.compile(args.marker, re.IGNORECASE | re.DOTALL)
		unknown = []

		for module in sorted({publish.split_nodeid(n)[0] for n in nodeids}):
			texts = function_texts(os.path.join(args.root, module))

			for nodeid in nodeids:
				if publish.split_nodeid(nodeid)[0] != module:
					continue

				name = publish.split_nodeid(nodeid)[1]
				text = texts.get(name)

				if text is None:
					# A test inherited from a class elsewhere: its name alone.
					short = name.rsplit('.', 1)[-1]
					text = next((t for q, t in sorted(texts.items())
						if q.rsplit('.', 1)[-1] == short), None)

				if text is None:
					unknown.append(nodeid)

				pinned[nodeid] = 'yes' if text and marker.search(text) else 'no'

		if unknown:
			note('{} test(s) have no function in their module\'s source, so they '
				'count as not pinned: {}'.format(len(unknown), ', '.join(
					unknown[:5])))

	path = write_tsv(args.out, 'collection.tsv', ['nodeid', 'pinned'],
		[[n, pinned.get(n, '')] for n in nodeids])

	modules = {}

	for nodeid in nodeids:
		module = publish.split_nodeid(nodeid)[0]
		modules[module] = modules.get(module, 0) + 1

	functions = {publish.split_nodeid(n) for n in nodeids}
	print('collection: {} tests in {} modules, {} test functions{}; wrote {}'
		.format(len(nodeids), len(modules), len(functions), ', {} pinning the '
		'current output'.format(len({publish.split_nodeid(n) for n in nodeids
			if pinned.get(n) == 'yes'})) if args.root else '', path))

	for module, count in sorted(modules.items()):
		print('  {:6d}  {}'.format(count, module))

	return 0


# files -----------------------------------------------------------------------

def files(args):
	out = subprocess.run(['git', '-C', args.repo, 'diff', '--name-status',
		'--no-renames', '{}...{}'.format(args.base, args.head)],
		capture_output=True, text=True)

	if out.returncode != 0:
		fail('git diff {}...{} failed: {}'.format(args.base, args.head,
			out.stderr.strip()))

	rows = []

	for line in out.stdout.splitlines():
		status, _, path = line.partition('\t')

		if path:
			rows.append([status.strip()[:1], path])

	if not rows:
		fail('{}...{} changes no file'.format(args.base, args.head))

	rows.sort(key=lambda row: row[1])
	path = write_tsv(args.out, 'files.tsv', ['status', 'path'], rows)
	publish.write_json(os.path.join(args.out, 'branch.json'), {
		'suite': os.path.abspath(args.repo), 'base': args.base,
		'branch': args.head})

	print('files: {} changed between {} and {}; wrote {} and branch.json'
		.format(len(rows), args.base, args.head, path))

	return 0


# coverage --------------------------------------------------------------------

def normalize(path, strip):
	path = path.replace('\\', '/')

	for prefix in strip:
		prefix = prefix.replace('\\', '/')

		if prefix and path.startswith(prefix):
			path = path[len(prefix):].lstrip('/')
			break

	return os.path.normpath(path).replace('\\', '/') if path else path


def test_of(context):
	"""A coverage.py context as a test id: `--cov-context=test` appends
	`|run`, `|setup` or `|teardown`."""

	return re.sub(r'\|(run|setup|teardown)$', '', context)


def load_one(path, strip):
	"""{file: [executable set, covered set]} and {test: set of files run},
	the second None when the file holds no per-test contexts."""

	try:
		with open(path, encoding='utf-8') as infile:
			data = json.load(infile)
	except (OSError, ValueError) as error:
		fail('could not read {} as JSON: {}'.format(path, error))

	lines, touched = {}, None

	if isinstance(data, dict) and isinstance(data.get('files'), list):
		# gcovr --json: one entry per source file, a line once per function
		# instance that holds it.
		for entry in data['files']:
			name = normalize(entry.get('file', ''), strip)
			executable, covered = lines.setdefault(name, [set(), set()])

			for line in entry.get('lines', []):
				if line.get('gcovr/noncode') or line.get('gcovr/excluded'):
					continue

				executable.add(line['line_number'])

				if line.get('count', 0) > 0:
					covered.add(line['line_number'])

	elif isinstance(data, dict) and isinstance(data.get('files'), dict) \
			and 'meta' in data:
		# coverage json: executed and missing lines, and with
		# --show-contexts the contexts that ran each line.
		contexts = {}

		for name, entry in data['files'].items():
			name = normalize(name, strip)
			executable, covered = lines.setdefault(name, [set(), set()])
			executable.update(entry.get('executed_lines', []))
			executable.update(entry.get('missing_lines', []))
			covered.update(entry.get('executed_lines', []))

			for names in (entry.get('contexts') or {}).values():
				for context in names:
					if context:
						contexts.setdefault(test_of(context), set()).add(name)

		if contexts:
			touched = contexts
	else:
		fail('{} is neither gcovr JSON (`gcovr --json`) nor coverage.py JSON '
			'(`coverage json`)'.format(path))

	return lines, touched


def load_coverage(path, strip):
	"""A file, or a directory holding one JSON file per test (the test being
	its name without `.json`): lines as `load_one`, and the tests run."""

	if not os.path.isdir(path):
		return load_one(path, strip)

	names = sorted(name for name in os.listdir(path) if name.endswith('.json'))

	if not names:
		fail('{} holds no .json file: a per-test directory holds one coverage '
			'file per test'.format(path))

	lines, touched = {}, {}

	for name in names:
		one, _ = load_one(os.path.join(path, name), strip)
		test = name[:-len('.json')]
		touched[test] = set()

		for source, (executable, covered) in one.items():
			total = lines.setdefault(source, [set(), set()])
			total[0].update(executable)
			total[1].update(covered)

			if covered:
				touched[test].add(source)

	return lines, touched


def assign_parts(names, excluded, mapping, mode):
	if mapping is not None:
		parts = {}

		for name in names:
			part = next((row['part'] for row in mapping
				if fnmatch.fnmatch(name, row['pattern'])), None)

			if part is None:
				fail('{} matches no pattern of the part map: add one (a last '
					'line `*\tother` catches the rest)'.format(name))

			parts[name] = part

		return parts

	if mode == 'auto':
		counted = [name for name in names if name not in excluded]
		mode = 'file' if len(counted) <= PARTS_PER_FILE else 'dir'

	if mode == 'file':
		return {name: name for name in names}

	return {name: (os.path.dirname(name) + '/' if os.path.dirname(name)
		else '(top level)') for name in names}


def coverage(args):
	strip = args.strip or []
	labels, existing = [], {}

	for item in args.existing or []:
		label, sep, path = item.partition('=')

		if not sep or not label.strip() or not path:
			fail('--existing takes LABEL=PATH; got {!r}'.format(item))

		label = label.strip()

		if label in labels:
			fail('--existing names {!r} twice'.format(label))

		labels.append(label)
		existing[label] = load_coverage(path, strip)

	suite = [load_coverage(path, strip) for path in args.suite]

	def keep(name):
		return name and not any(fnmatch.fnmatch(name, glob)
			for glob in args.drop or [])

	names = sorted({name for lines, _ in list(existing.values()) + suite
		for name in lines if keep(name)})

	if not names:
		fail('no source file is left in the coverage data after --drop')

	excluded = {name for name in names if any(fnmatch.fnmatch(name, glob)
		for glob in args.exclude or [])}
	mapping = read_tsv(args.part_map, ['pattern', 'part']) \
		if args.part_map else None
	parts = assign_parts(names, excluded, mapping, args.parts)

	for part in sorted(set(parts.values())):
		members = [name for name in names if parts[name] == part]
		marked = {name in excluded for name in members}

		if len(marked) > 1:
			fail('part {} mixes excluded files ({}) with counted ones ({}): give '
				'the excluded files a part of their own'.format(part,
				', '.join(sorted(set(members) & excluded)[:3]), ', '.join(sorted(
				set(members) - excluded)[:3])))

	def union(sources, name, which):
		out = set()

		for lines, _ in sources:
			out |= lines.get(name, [set(), set()])[which]

		return out

	everything = list(existing.values()) + suite
	rows = []

	for name in names:
		executable = union(everything, name, 0)
		by_label = [union([existing[label]], name, 1) for label in labels]
		before = set().union(*by_label) if by_label else set()
		after = union(suite, name, 1)
		rows.append([name, parts[name], 'yes' if name in excluded else 'no',
			len(executable), len(before), len(after), len(before | after)]
			+ [len(covered) for covered in by_label])

	path = write_tsv(args.out, 'coverage.tsv', ['file', 'part', 'excluded',
		'lines', 'existing', 'suite', 'both'] + ['existing:' + label
		for label in labels], rows)

	# Tests per part: from per-test data when every input has it.
	def tests_by_role(sources):
		if any(touched is None for _, touched in sources):
			return None

		merged = {}

		for _, touched in sources:
			for test, run in touched.items():
				merged.setdefault(test, set()).update(run)

		return merged

	before_tests = tests_by_role(list(existing.values()))
	after_tests = tests_by_role(suite)
	part_names = sorted(set(parts.values()))
	counted_parts = [part for part in part_names
		if not any(parts[n] == part and n in excluded for n in names)]
	table = []

	if before_tests is not None and after_tests is not None:
		def count(tests, members):
			return len([test for test, run in tests.items() if run & members])

		shared = set(before_tests) & set(after_tests)

		if shared:
			note('{} test(s) appear in both --existing and --suite and are '
				'counted once. --suite should be the run of the new tests only: '
				'otherwise its coverage row includes the existing tests'.format(
				len(shared)))

		combined = {}

		for tests in (before_tests, after_tests):
			for test, run in tests.items():
				combined.setdefault(test, set()).update(run)

		for part in part_names + ['(total)']:
			members = {name for name in names if (parts[name] == part
				if part != '(total)' else parts[name] in counted_parts)}
			table.append([part, count(before_tests, members),
				count(combined, members), 'per-test'])

		how = 'from per-test coverage ({} existing tests, {} of this suite)'.format(
			len(before_tests), len(after_tests))
	elif args.part_tests:
		given = {row['part']: row for row in read_tsv(args.part_tests, ['part',
			'tests_before', 'tests_after'])}
		missing = [part for part in counted_parts + ['(total)']
			if part not in given]
		unknown = sorted(set(given) - set(part_names) - {'(total)'})

		if missing or unknown:
			fail('{}: {}{}'.format(args.part_tests, 'no row for {}'.format(
				', '.join(missing)) if missing else '', '{}rows for parts the '
				'coverage has not: {}'.format('; ' if missing else '', ', '.join(
				unknown)) if unknown else ''))

		for part in part_names + ['(total)']:
			row = given.get(part)

			if row is None:
				table.append([part, '', '', ''])
				continue

			try:
				before, after = int(row['tests_before']), int(row['tests_after'])
			except ValueError:
				fail('{}: the tests of {} are not whole numbers'.format(
					args.part_tests, part))

			table.append([part, before, after, 'by-file'])

		how = 'from {} (tests in the files written for each part)'.format(
			args.part_tests)
	else:
		table = [[part, '', '', ''] for part in part_names + ['(total)']]
		how = 'not counted: no per-test coverage (coverage.py contexts, or a ' \
			'directory of one file per test) for every input, and no --part-tests'

	for row in table:
		row.insert(1, 'yes' if row[0] not in counted_parts
			and row[0] != '(total)' else 'no')

	parts_path = write_tsv(args.out, 'parts.tsv', ['part', 'excluded',
		'tests_before', 'tests_after', 'counted'], table)

	total = sum(row[3] for row in rows if row[2] == 'no')

	def share(column):
		hit = sum(row[column] for row in rows if row[2] == 'no')
		return '{:,} ({:.1f}%)'.format(hit, 100.0 * hit / total) if total else '0'

	print('coverage: {} files, {:,} executable lines outside excluded code; '
		'existing {}, this suite {}, both {}'.format(len(rows), total,
		share(4), share(5), share(6)))
	print('  parts: {} ({} excluded); tests per part {}'.format(len(part_names),
		len(part_names) - len(counted_parts), how))
	print('  wrote {} and {}'.format(path, parts_path))

	return 0


# names -----------------------------------------------------------------------

def python_files(paths):
	out = []

	for path in paths:
		if os.path.isfile(path):
			out.append(path)
			continue

		for directory, subdirectories, names in os.walk(path):
			subdirectories[:] = sorted(d for d in subdirectories
				if not d.startswith('.') and d != '__pycache__')
			out += [os.path.join(directory, name) for name in sorted(names)
				if name.endswith('.py')]

	return sorted(set(os.path.normpath(path) for path in out))


def public_names(path):
	with open(path, encoding='utf-8') as infile:
		tree = ast.parse(infile.read())

	exported = None

	for node in tree.body:
		if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name)
				and target.id == '__all__' for target in node.targets) \
				and isinstance(node.value, (ast.List, ast.Tuple)):
			exported = {element.value for element in node.value.elts
				if isinstance(element, ast.Constant)
				and isinstance(element.value, str)}

	names = []

	for node in tree.body:
		if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
				ast.ClassDef)) or node.name.startswith('_'):
			continue

		if exported is not None and node.name not in exported:
			continue

		names.append((node.name, node.name))

		if isinstance(node, ast.ClassDef):
			for sub in node.body:
				if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) \
						and not sub.name.startswith('_'):
					names.append((node.name + '.' + sub.name, sub.name))

	return names


def identifiers(path):
	with open(path, encoding='utf-8') as infile:
		tree = ast.parse(infile.read())

	found = set()

	for node in ast.walk(tree):
		if isinstance(node, ast.Name):
			found.add(node.id)
		elif isinstance(node, ast.Attribute):
			found.add(node.attr)
		elif isinstance(node, ast.alias):
			found.add(node.name.rsplit('.', 1)[-1])

	return found


def names(args):
	tests = python_files(args.tests)
	test_set = set(tests)
	sources = [path for path in python_files(args.source) if path not in test_set
		and not os.path.basename(path).startswith(('test_', 'conftest'))
		and not any(fnmatch.fnmatch(path, glob) for glob in args.exclude or [])
		and not (os.path.basename(path).startswith('_')
			and os.path.basename(path) != '__init__.py')]

	if not sources:
		fail('no Python source file under {}'.format(', '.join(args.source)))

	if not tests:
		fail('no Python test file under {}'.format(', '.join(args.tests)))

	named = [identifiers(path) for path in tests]
	rows = []

	for path in sources:
		for display, identifier in public_names(path):
			rows.append([display, path, sum(1 for found in named
				if identifier in found)])

	rows.sort(key=lambda row: (row[1], row[0]))
	out = write_tsv(args.out, 'names.tsv', ['name', 'defined_in', 'tests'], rows)
	unnamed = [row for row in rows if not row[2]]
	print('names: {} public names in {} files; {} named by no test file; wrote {}'
		.format(len(rows), len(sources), len(unnamed), out))

	return 0


# -----------------------------------------------------------------------------

def build_parser():
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	commands = parser.add_subparsers(dest='command', required=True)
	out_help = 'the folder of tables, {}'.format(FOLDER)

	sub = commands.add_parser('collection', help='collection.tsv: every test '
		'pytest collects, and whether its function pins the current output')
	sub.add_argument('--collect-output', required=True, help='the saved output '
		'of `pytest --collect-only -q` over the suite')
	sub.add_argument('--root', help='the directory node ids are relative to '
		'(pytest\'s rootdir, `$WORK/tests/suite`); without it the pinned '
		'column is left empty')
	sub.add_argument('--marker', default=MARKER, help='the regular expression, '
		'matched without regard to case in each test function\'s source, that '
		'marks it as pinning the current output (default: "pin" or "pins" '
		'followed within seven words by "current")')
	sub.add_argument('--out', required=True, help=out_help)
	sub.set_defaults(handler=collection)

	sub = commands.add_parser('files', help='files.tsv: the files the tests '
		'branch changes, from git; and branch.json, the clone, branch and base '
		'commit the push reads')
	sub.add_argument('--repo', required=True, help='the clone holding the '
		'tests branch, `$WORK/tests/suite`')
	sub.add_argument('--base', required=True, help='the commit the tests '
		'branch was made from (`commit` in `$WORK/tests/branch.json`)')
	sub.add_argument('--head', required=True, help='the tests branch, '
		'auto-optimize/<YYYY-MM-DD>-unit-tests')
	sub.add_argument('--out', required=True, help=out_help)
	sub.set_defaults(handler=files)

	sub = commands.add_parser('coverage', help='coverage.tsv and parts.tsv: '
		'lines covered per file by the existing tests, this suite and both, and '
		'tests per part')
	sub.add_argument('--existing', action='append', metavar='LABEL=PATH',
		help='coverage of the repository\'s own tests at the base commit, one '
		'per suite or CI step, LABEL as the body names it; repeatable; none '
		'when the repository has no tests')
	sub.add_argument('--suite', action='append', required=True, metavar='PATH',
		help='coverage of the new suite; repeatable (several builds)')
	sub.add_argument('--strip', action='append', metavar='PREFIX',
		help='a prefix removed from file paths, so every input names a file '
		'the same way; repeatable')
	sub.add_argument('--drop', action='append', metavar='GLOB', help='files '
		'left out of every table (the tests themselves); repeatable')
	sub.add_argument('--exclude', action='append', metavar='GLOB',
		help='vendored or generated files: kept as parts of their own, marked '
		'excluded, never in a total; repeatable')
	sub.add_argument('--part-map', metavar='FILE', help='TSV with header '
		'`pattern`, `part`: the first glob that matches a file names its part')
	sub.add_argument('--parts', choices=('auto', 'file', 'dir'), default='auto',
		help='without --part-map: a part per file, per directory, or (auto) per '
		'file up to {} counted files and per directory above'.format(
			PARTS_PER_FILE))
	sub.add_argument('--part-tests', metavar='FILE', help='TSV with header '
		'`part`, `tests_before`, `tests_after` and a `(total)` row: tests per part '
		'counted from the test files written for each part, used when the '
		'inputs have no per-test data')
	sub.add_argument('--out', required=True, help=out_help)
	sub.set_defaults(handler=coverage)

	sub = commands.add_parser('names', help='names.tsv: public Python names '
		'and how many test files name them')
	sub.add_argument('--source', action='append', required=True, metavar='PATH',
		help='a package directory or file; repeatable')
	sub.add_argument('--tests', action='append', required=True, metavar='PATH',
		help='a test directory or file; repeatable')
	sub.add_argument('--exclude', action='append', metavar='GLOB',
		help='source files left out (vendored, generated); repeatable')
	sub.add_argument('--out', required=True, help=out_help)
	sub.set_defaults(handler=names)

	return parser


def main(argv=None):
	args = build_parser().parse_args(argv)
	return args.handler(args)


if __name__ == '__main__':
	sys.exit(main())
