#!/usr/bin/env python3
"""Check the unit-test PR's table generators and body renderer.

	python3 test_tests_pr.py            # a few seconds
	python3 test_tests_pr.py --update   # rewrite fixtures/tests_pr/expected.md

The renderer is checked against a fixed set of tables and prose,
`fixtures/tests_pr/data/`, whose body must equal `fixtures/tests_pr/expected.md`
byte for byte; then for determinism, each refusal, each section dropped, and
what the documents chosen at step 5 allow. The generators are checked on small
inputs built here: pytest's collection, gcovr and coverage.py JSON with and
without per-test data, a git repository and a Python package. `--update` is
for a deliberate change to the renderer: read the diff of expected.md before
keeping it.

No network; needs git.
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

import attribution
import test_runner

BODY = os.path.join(SCRIPTS, 'tests_pr_body.py')
TABLES = os.path.join(SCRIPTS, 'tests_pr_tables.py')
FIXTURE = os.path.join(HERE, 'fixtures', 'tests_pr')
DATA = os.path.join(FIXTURE, 'data')
EXPECTED = os.path.join(FIXTURE, 'expected.md')

# Words no published document may hold: they advertise the tool, or name
# checks this version does not run.
BANNED = ('auto-optimize', 'programmable', 'laboratory', 'umass', 'monolith',
	'stack', 'sweep', 'clean-room', 'options exercised', 'checked locally')


def run(script, *args, cwd=None):
	out = subprocess.run([sys.executable, script] + list(args),
		capture_output=True, text=True, cwd=cwd)
	return out.returncode, out.stdout, out.stderr


def copy_data(base, name):
	target = os.path.join(base, name)
	shutil.copytree(DATA, target)
	return target


def edit(path, old, new):
	with open(path) as handle:
		text = handle.read()

	if old not in text:
		raise RuntimeError('{!r} is not in {}'.format(old, path))

	with open(path, 'w') as handle:
		handle.write(text.replace(old, new, 1))


def read(path):
	with open(path) as handle:
		return handle.read()


def write_json(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)

	with open(path, 'w') as handle:
		json.dump(data, handle)


def tsv(path):
	lines = read(path).rstrip('\n').split('\n')
	header = lines[0].split('\t')
	return [dict(zip(header, line.split('\t'))) for line in lines[1:]]


def git(repo, *args):
	return subprocess.run(['git', '-C', repo, '-c', 'user.name=t', '-c',
		'user.email=t@t'] + list(args), check=True, capture_output=True,
		text=True).stdout.strip()


# The renderer ----------------------------------------------------------------

def rendering(suite, base):
	code, first, err = run(BODY, DATA)
	expected = read(EXPECTED)
	suite.check('the fixture renders to expected.md byte for byte', code == 0
		and first == expected, err or [line for line in zip(first.split('\n'),
		expected.split('\n')) if line[0] != line[1]][:3])

	_, second, _ = run(BODY, DATA)
	suite.check('rendering twice gives the same bytes', first == second)

	out = os.path.join(base, 'body.md')
	code, stdout, err = run(BODY, DATA, '--out', out, '--title',
		'Add unit tests for pkg')
	suite.check('--out writes the same body and says so on stderr', code == 0
		and read(out) == expected and stdout == '' and 'wrote' in err, err)
	suite.equal('--out writes the title beside the body as given, with no '
		'prefix', read(os.path.join(base, 'body.title')),
		'Add unit tests for pkg\n')
	suite.check('the body starts with the opening field', expected.startswith(
		'This PR adds a pytest suite for `pkg` and its command-line programs, '
		'`tool` and `tool-index`. It holds eight tests in three modules under '
		'`tests/`, with fixtures in `conftest.py`. It changes no source, build '
		'or workflow file.\n'), expected[:300])
	lowered = expected.lower()
	suite.equal('the body names neither the tool nor the lab, nor a check '
		'this version does not run', [word for word in BANNED
		if word in lowered], [])
	suite.equal('attribution.py finds no problem in the body, other than the '
		'bug report\'s stand-in number', attribution.problems('tests',
		'Add unit tests for pkg', expected.replace('#<bug issue>', '#7')), [])

	fresh = os.path.join(base, 'fresh.md')

	for args, words in ((['--out', fresh], '--out needs --title'),
			(['--out', fresh, '--title', 'add unit tests.'], 'capital letter'),
			(['--out', fresh, '--title', 'Add unit tests.'], 'period'),
			(['--out', fresh, '--title', 'Add unit tests for ' + 'x' * 60],
				'the limit is 72')):
		code, _, err = run(BODY, DATA, *args)
		suite.check('refuses {}, writing nothing'.format(words), code == 1
			and words in err and not os.path.exists(fresh), err[-300:])

	code, _, err = run(BODY, DATA, '--out', out)
	suite.check('--out with no --title re-renders with the title stored beside '
		'it', code == 0 and read(os.path.join(base, 'body.title')) ==
		'Add unit tests for pkg\n' and read(out) == expected, err)

	sections = [line for line in expected.split('\n') if line.startswith('## ')]
	suite.equal('the sections come in #535\'s order, the per-part table first, '
		'without the options and local-check sections', sections,
		['## Tests and line coverage by part', '## Running it',
		'## How expected values are derived', '## What it covers',
		'## Line coverage', '## What the suite does not cover'])
	suite.check('the body ends with the closing line', expected.rstrip(
		'\n').endswith(attribution.CLOSING))
	running = expected.split('## Running it\n', 1)[1].split('\n## ', 1)[0]
	suite.check('checked is the last paragraph of *Running it*, unwrapped',
		running.strip().split('\n\n')[-1] == 'The suite passed in each '
		'configuration the CI workflow runs: Python 3.8 and Python 3.12 on '
		'Linux, each with `pip install -e .` and `python3 -m pytest tests`, '
		'and Python 3.12 without `numpy`, where two more tests skip.', running)


# The fixture's contradictions.marked, as prose.md wraps it.
MARKED = ('`tests/KNOWN.md` names each case, and the test that would cover it '
	'says so\nin a comment.')
CHECKED = ('=== checked\nThe suite passed in each configuration the CI '
	'workflow runs: Python 3.8 and\nPython 3.12 on Linux, each with `pip '
	'install -e .` and `python3 -m pytest\ntests`, and Python 3.12 without '
	'`numpy`, where two more tests skip.\n\n')
CONTRADICTIONS = ('One case, `tool -v` printing to stdout where the '
	'documentation says stderr, is\nlisted in #<bug issue>.\n')


def marking(suite, base):
	"""contradictions.marked: where the suite records the behaviour it leaves
	untested, said in prose because it differs between suites."""

	expected = read(EXPECTED)
	suite.check('contradictions.marked is rendered between the untested-behaviour '
		'sentence and the report paragraph, unwrapped, in place of any fixed '
		'sentence', 'so that fixing it will not break the suite. {} One case'
		.format(MARKED.replace('\n', ' ')) in expected
		and 'Each affected test says so' not in expected, expected[-1500:])

	data = copy_data(base, 'marked-example')
	edit(os.path.join(data, 'prose.md'), MARKED + '\n', 'Each affected test '
		'says so in a docstring or comment\n')
	code, body, err = run(BODY, data)
	suite.check('the documented example renders as its own sentence, the '
		'closing period added', code == 0 and 'will not break the suite. Each '
		'affected test says so in a docstring or comment. One case'
		in body, err or body[-1500:])

	data = copy_data(base, 'marked-none')
	edit(os.path.join(data, 'prose.md'), CONTRADICTIONS, 'none\n')
	edit(os.path.join(data, 'prose.md'), '=== contradictions.marked\n' + MARKED
		+ '\n', '')
	code, body, err = run(BODY, data)
	suite.check('with contradictions none, contradictions.marked is not needed',
		code == 0 and '**No behaviour that contradicts the documentation was '
		'found.**' in body and 'KNOWN.md' not in body, err)

	with open(os.path.join(data, 'prose.md'), 'a') as handle:
		handle.write('\n=== contradictions.marked\n' + MARKED + '\n')
	code, body, err = run(BODY, data)
	suite.check('and when given anyway it is named as unused, not rendered',
		code == 0 and 'KNOWN.md' not in body and 'prose fields no rendered '
		'section uses: contradictions.marked' in err, err)


def refusals(suite, base):
	cases = [
		('a placeholder in the prose', 'prose.md', '=== platforms\nLinux only;',
			'=== platforms\n<<FILL: platforms>> Linux only;', 'placeholder'),
		('an unknown field', 'prose.md', '=== running.ci\n', '=== running.cli\n',
			'no field is called \'running.cli\''),
		('the local-check field this version dropped', 'prose.md',
			'=== running.ci\n', '=== checked.environment\nOn Linux:\n\n'
			'=== running.ci\n', 'no field is called \'checked.environment\''),
		('the never-passed option field this version dropped', 'prose.md',
			'=== running.ci\n', '=== never tool --color\ncrashes\n\n'
			'=== running.ci\n', 'no field is called \'never\''),
		('a module with no description', 'prose.md', '=== module '
			'tests/test_core.py\n`score` against a short reference '
			'implementation.\n', '', 'the row of tests/test_core.py'),
		('a missing checked field', 'prose.md', CHECKED, '', '"=== checked", '
			'which *Running it* needs'),
		('checked written as a list', 'prose.md', '=== checked\n',
			'=== checked\n- ', '"=== checked" must be one paragraph'),
		('checked written as two paragraphs', 'prose.md', 'without `numpy`, '
			'where two more tests skip.', 'without `numpy`.\n\nTwo more tests '
			'skip there.', '"=== checked" must be one paragraph'),
		('an opening that does not start with a capital letter', 'prose.md',
			'This PR adds', 'this PR adds', 'start it with a capital letter'),
		('a description of a module nothing collected', 'prose.md',
			'=== module tests/test_core.py', '=== module tests/test_gone.py',
			'collected no test in that module'),
		('the duration token this version dropped', 'prose.md', '{{tests}}',
			'{{seconds}}', 'no table gives {{seconds}}'),
		('a heading in the prose', 'prose.md', '=== running.ci\n',
			'=== running.ci\n## CI\n', 'holds a heading'),
		('coverage columns that disagree', 'coverage.tsv', 'pkg/io.py\tpkg/\tno'
			'\t50\t0\t45\t45', 'pkg/io.py\tpkg/\tno\t50\t0\t45\t44', 'disagree'),
		('existing coverage that is not its labels\' union', 'coverage.tsv',
			'pkg/core.py\tpkg/\tno\t100\t60\t70\t85\t55\t20',
			'pkg/core.py\tpkg/\tno\t100\t60\t70\t85\t30\t20', 'union of its labels'),
		('parts that are not coverage\'s parts', 'parts.tsv', 'pkg/cli/\tno',
			'pkg/cmd/\tno', 'name different parts'),
		('a changed file outside the test directory', 'files.tsv',
			'A\ttests/test_io.py\n', 'A\ttests/test_io.py\nM\tsetup.py\n',
			'outside `tests/`'),
		('untested behaviour without where the suite records it', 'prose.md',
			'=== contradictions.marked\n' + MARKED + '\n', '',
			'"=== contradictions.marked", which the list of untested behaviour'),
		('contradictions written as a list', 'prose.md', 'One case, `tool -v` '
			'printing', '- One case, `tool -v` printing', 'is a list'),
		('contradictions that name no bug report', 'prose.md', 'is\nlisted in '
			'#<bug issue>.', 'is\nlisted.', 'names no bug report'),
		('a module cell of more than 25 words', 'prose.md', '`score` against a '
			'short reference implementation.', ' '.join(['word'] * 26) + '.',
			'is 26 words'),
		('where the suite records untested behaviour, written as a list',
			'prose.md', '=== contradictions.marked\n', '=== contradictions.marked'
			'\n- ', 'must be one paragraph'),
		('a search word in the prose', 'prose.md', 'Linux only;', 'Linux only, '
			'as the search ran there;', 'the search'),
	]

	for name, table, old, new, words in cases:
		data = copy_data(base, 'refuse-' + str(abs(hash(name))))
		edit(os.path.join(data, table), old, new)
		out = os.path.join(data, 'body.md')
		code, stdout, err = run(BODY, data, '--out', out, '--title',
			'Add unit tests for pkg')
		suite.check('refuses {}, saying why, writing nothing'.format(name),
			code == 1 and words in err and not os.path.exists(out)
			and 'Traceback' not in err, (code, err[-600:]))


def driving(suite, base):
	"""A dependency manifest needs `declares`; the test driver needs
	`=== driver <path>` and after.tests_driver_edit yes."""

	driver = '=== driver scripts/run-tests.sh\nalso runs every module under ' \
		'`tests/`.\n\n'
	manifest = '=== declares\nadding `numpy` to `requirements-dev.txt`\n\n'
	sentence = 'It changes no source or workflow file. Its one change outside ' \
		'the tests is to `scripts/run-tests.sh`, the repository\'s test driver, ' \
		'which also runs every module under `tests/`.'
	cases = [
		('the driver, declared, with tests_driver_edit yes', ['scripts/'
			'run-tests.sh'], driver, 'yes', 0, sentence),
		('a manifest and the driver, both declared', ['scripts/run-tests.sh',
			'requirements-dev.txt'], manifest + driver, 'yes', 0, 'Its one change '
			'outside the tests is adding `numpy` to `requirements-dev.txt`. It also '
			'changes `scripts/run-tests.sh`, the repository\'s test driver'),
		('a manifest, declared, with no config', ['requirements-dev.txt'],
			manifest, None, 0, 'Its one change outside the tests is adding `numpy`'),
		('a manifest, undeclared', ['requirements-dev.txt'], '', 'yes', 1,
			'allowed only with the `declares` field'),
		('the driver with tests_driver_edit ask', ['scripts/run-tests.sh'],
			driver, 'ask', 1, "after.tests_driver_edit is 'ask'"),
		('the driver with tests_driver_edit no', ['scripts/run-tests.sh'],
			driver, 'no', 1, "after.tests_driver_edit is 'no'"),
		('the driver with no tests_driver_edit', ['scripts/run-tests.sh'],
			driver, None, 1, 'after.tests_driver_edit is absent'),
		('the driver, undeclared', ['scripts/run-tests.sh'], '', 'yes', 1,
			'outside `tests/`'),
		('a driver the branch does not change', ['scripts/run-tests.sh'],
			driver.replace('run-tests', 'other'), 'yes', 1, 'names a file '
			'files.tsv does not show changed'),
		('the driver and another file', ['scripts/run-tests.sh', 'setup.py'],
			driver, 'yes', 1, 'outside `tests/`'),
	]

	for name, changed, fields, setting, want, words in cases:
		data = copy_data(base, 'drive-' + str(abs(hash(name))))
		lines = ''.join('M\t{}\n'.format(path) for path in changed)
		edit(os.path.join(data, 'files.tsv'), 'A\ttests/test_io.py\n',
			'A\ttests/test_io.py\n' + lines)
		edit(os.path.join(data, 'prose.md'), '=== docs\n', fields + '=== docs\n')
		config = os.path.join(data, 'config.json')

		if setting is not None:
			write_json(config, {'after': {'tests_driver_edit': setting}})

		code, stdout, err = run(BODY, data, '--config', config)
		suite.check('{} {}'.format('renders' if want == 0 else 'refuses', name),
			code == want and words in (stdout if want == 0 else err)
			and 'Traceback' not in err, (code, err[-600:]))

	# The default config.json is the search's, two folders above the data.
	work = os.path.join(base, 'drive-work')
	data = os.path.join(work, 'ship', 'pr-tests')
	shutil.copytree(DATA, data)
	edit(os.path.join(data, 'files.tsv'), 'A\ttests/test_io.py\n',
		'A\ttests/test_io.py\nM\tscripts/run-tests.sh\n')
	edit(os.path.join(data, 'prose.md'), '=== docs\n', driver + '=== docs\n')
	write_json(os.path.join(work, 'autooptimize', 'config.json'), {'after': {
		'tests_driver_edit': 'yes'}})
	code, stdout, err = run(BODY, data)
	suite.check('after.tests_driver_edit is read from the search\'s '
		'config.json by default', code == 0 and sentence in stdout, err[-600:])


def allowing(suite, base):
	"""A file the user approved by name: `--allow-source PATH` plus `declares`."""

	files = ['src/seed_diffs.c', 'src/Makefile.am']
	declares = '=== declares\nfixing the seed in `src/seed_diffs.c` and adding ' \
		'the test target to `src/Makefile.am`\n\n'

	def prepare(name, prose):
		data = copy_data(base, 'allow-' + name)
		edit(os.path.join(data, 'files.tsv'), 'A\ttests/test_io.py\n',
			'A\ttests/test_io.py\n' + ''.join('M\t{}\n'.format(f) for f in files))
		edit(os.path.join(data, 'prose.md'), '=== docs\n', prose + '=== docs\n')
		return data

	flags = [flag for path in files for flag in ('--allow-source', path)]

	data = prepare('none', declares)
	code, out, err = run(BODY, data)
	suite.check('refuses source files without --allow-source, even declared',
		code == 1 and 'outside `tests/`' in err and '--allow-source' in err
		and 'Traceback' not in err, (code, err[-600:]))

	data = prepare('ok', declares)
	code, out, err = run(BODY, data, *flags)
	suite.check('renders source files named by --allow-source and `declares`',
		code == 0 and 'Traceback' not in err, (code, err[-600:]))
	opening = out.split('\n', 1)[0]
	suite.check('the opening names each file and no longer claims no source '
		'changes', all('`{}`'.format(f) in opening for f in files)
		and 'It changes no workflow file.' in opening
		and 'no source' not in opening, opening[-400:])

	data = prepare('partial', declares.replace('`src/Makefile.am`', 'the build'))
	code, out, err = run(BODY, data, *flags)
	suite.check('refuses a file named by the flag but not by `declares`',
		code == 1 and 'src/Makefile.am, and `declares` does not mention it' in err,
		(code, err[-600:]))

	data = prepare('extra', declares)
	edit(os.path.join(data, 'files.tsv'), 'M\tsrc/Makefile.am\n',
		'M\tsrc/Makefile.am\nM\tsetup.py\n')
	code, out, err = run(BODY, data, *flags)
	suite.check('still refuses a changed file the flags do not name',
		code == 1 and 'outside `tests/`' in err, (code, err[-600:]))

	data = prepare('stale', declares)
	code, out, err = run(BODY, data, *flags, '--allow-source', 'src/other.c')
	suite.check('refuses a flag naming a file the branch does not change',
		code == 1 and 'src/other.c' in err and 'does not show changed' in err,
		(code, err[-600:]))


def filling(suite, base):
	"""#<bug issue> becomes the number in the bug report's filed.json."""

	work = os.path.join(base, 'fill-work')
	data = os.path.join(work, 'pr-tests')
	shutil.copytree(DATA, data)
	code, body, err = run(BODY, data)
	suite.check('without filed.json the stand-in stays', code == 0
		and '#<bug issue>' in body, err)
	record = os.path.join(work, 'issue-bugs', 'filed.json')
	write_json(record, {'number': 41})
	code, body, err = run(BODY, data)
	suite.check('a filed.json beside the folder fills it', code == 0
		and 'listed in #41.' in body and '#<bug issue>' not in body, err)

	with open(record, 'w') as handle:
		handle.write('not json')

	code, body, err = run(BODY, data)
	suite.check('an unreadable record is refused', code == 1 and 'cannot read '
		'the number' in err, err)


def choosing(suite, base):
	"""The documents chosen at step 5: without the bug report, #<bug issue>
	is refused and `contradictions` may name a file in the repository."""

	work = os.path.join(base, 'choose-work')
	data = os.path.join(work, 'ship', 'pr-tests')
	shutil.copytree(DATA, data)
	config = os.path.join(work, 'autooptimize', 'config.json')
	prose = os.path.join(data, 'prose.md')
	original = read(prose)

	def contradicting(text, documents, flags=()):
		write_json(config, {'after': {'ship_documents': documents}})

		with open(prose, 'w') as handle:
			handle.write(original.replace(CONTRADICTIONS, text))

		return run(BODY, data, *flags)

	code, body, err = contradicting(CONTRADICTIONS, ['bugs', 'tests'])
	suite.check('with the bug report chosen, #<bug issue> renders', code == 0
		and 'listed in #<bug issue>.' in body, err)

	code, body, err = contradicting(CONTRADICTIONS, ['tests', 'optimization'])
	suite.check('without it, #<bug issue> in the prose is refused', code == 1
		and 'holds #<bug issue>, and the bug report is not among the documents '
		'chosen' in err and 'Traceback' not in err, err[-600:])

	code, body, err = contradicting(CONTRADICTIONS.replace('#<bug issue>',
		'#12'), ['tests'])
	suite.check('without it, an issue number names the report', code == 0
		and 'listed in #12.' in body, err)

	code, body, err = contradicting('The one case is listed in\n'
		'https://example.org/known.html.\n', ['tests'])
	suite.check('without it, a URL names the report', code == 0, err)

	code, body, err = contradicting('The one case is listed in `tests/KNOWN.md`.'
		'\n', ['tests'])
	suite.check('without it, a file the tests branch does not hold is refused',
		code == 1 and 'names no report and no file in the repository' in err,
		err[-600:])

	edit(os.path.join(data, 'files.tsv'), 'A\ttests/test_io.py\n',
		'A\ttests/test_io.py\nA\ttests/KNOWN.md\n')
	code, body, err = contradicting('The one case is listed in `tests/KNOWN.md`.'
		'\n', ['tests'])
	suite.check('without it, a file files.tsv lists names where the cases are',
		code == 0 and 'listed in `tests/KNOWN.md`.' in body, err)

	code, body, err = contradicting('The one case is listed in `tests/KNOWN.md`.'
		'\n', ['bugs', 'tests'])
	suite.check('with it chosen, a file alone does not name the bug report',
		code == 1 and 'names no bug report' in err, err[-600:])

	code, body, err = contradicting('One case is left untested.\n', ['tests'])
	suite.check('without it, naming nothing is refused', code == 1
		and 'names no report and no file' in err, err[-600:])

	with open(prose, 'w') as handle:
		handle.write(original.replace(CONTRADICTIONS, 'none\n').replace(
			'=== contradictions.marked\n' + MARKED + '\n', ''))
	code, body, err = run(BODY, data)
	suite.check('without it, none is accepted', code == 0 and '**No behaviour '
		'that contradicts' in body, err)

	# A file the repository already held: found on the tests branch, from the
	# clone and branch that branch.json records.
	repo = os.path.join(base, 'choose-repo')
	os.makedirs(os.path.join(repo, 'docs'))

	with open(os.path.join(repo, 'docs', 'KNOWN_ISSUES.md'), 'w') as handle:
		handle.write('# Known issues\n')

	git(repo, 'init', '-q', '-b', 'main')
	git(repo, 'add', '-A')
	git(repo, 'commit', '-q', '-m', 'base')
	git(repo, 'checkout', '-q', '-b', 'auto-optimize/2026-11-01-unit-tests')
	write_json(os.path.join(data, 'branch.json'), {'suite': repo, 'base':
		'main', 'branch': 'auto-optimize/2026-11-01-unit-tests'})
	code, body, err = contradicting('The one case is listed in '
		'`docs/KNOWN_ISSUES.md`.\n', ['tests'])
	suite.check('without it, a file the tests branch already held names where '
		'the cases are', code == 0, err[-600:])
	code, body, err = contradicting('The one case is listed in '
		'`docs/MISSING.md`.\n', ['tests'])
	suite.check('and a path the branch does not hold is refused', code == 1
		and 'names no report and no file' in err, err[-600:])

	os.remove(config)
	code, body, err = contradicting(CONTRADICTIONS, ['tests'], ['--config',
		os.path.join(base, 'choose-other.json')])
	suite.check('with --config naming a missing file, every document counts as '
		'chosen', code == 0, err[-600:])
	write_json(os.path.join(base, 'choose-other.json'), {'after': {
		'ship_documents': ['tests']}})
	code, body, err = run(BODY, data, '--config', os.path.join(base,
		'choose-other.json'))
	suite.check('and --config names the config.json whose ship_documents '
		'decide', code == 1 and 'holds #<bug issue>' in err, err[-600:])


def singular(suite, base):
	"""One line is "1 line", not "1 lines"."""

	data = copy_data(base, 'singular')
	path = os.path.join(data, 'coverage.tsv')

	for old, new in (('pkg/core.py\tpkg/\tno\t100\t60\t70\t85',
			'pkg/core.py\tpkg/\tno\t100\t60\t70\t100'),
			('pkg/io.py\tpkg/\tno\t50\t0\t45\t45',
			'pkg/io.py\tpkg/\tno\t46\t0\t45\t45'),
			('pkg/cli/main.py\tpkg/cli/\tno\t40\t10\t35\t36',
			'pkg/cli/main.py\tpkg/cli/\tno\t40\t10\t35\t40')):
		edit(path, old, new)

	code, body, err = run(BODY, data)
	suite.check('a gap of one line reads "1 line not run"', code == 0
		and '(1 line not run,' in body and '1 lines' not in body, err
		or [l for l in body.split('\n') if 'largest gaps' in l])


def capping(suite, base):
	"""The public names are cut to ten, the rest counted."""

	data = copy_data(base, 'capping')

	with open(os.path.join(data, 'names.tsv'), 'a') as handle:
		for i in range(14):
			handle.write('extra_{:02d}\tpkg/io.py\t0\n'.format(i))

	code, body, err = run(BODY, data)
	line = [l for l in body.split('\n') if l.startswith('**Public names')]
	suite.check('public names no test names are cut to ten and the rest counted',
		code == 0 and line and '(16 of 18)' in line[0] and line[0].count('`')
		== 20 and ', and 6 more.' in line[0], err or line)


def dropping(suite, base):
	data = copy_data(base, 'drop-coverage')
	os.remove(os.path.join(data, 'coverage.tsv'))
	os.remove(os.path.join(data, 'parts.tsv'))
	code, body, err = run(BODY, data)
	suite.check('without coverage.tsv and parts.tsv, both coverage sections '
		'and the unreached lines are dropped, and stderr says so', code == 0
		and '## Line coverage' not in body and '## Tests and line coverage'
		not in body and 'Lines no test reaches' not in body and 'dropped *Line '
		'coverage*: no coverage.tsv and parts.tsv' in err, err)
	suite.check('and the prose field only they used is named as unused',
		'coverage.method' in err, err)

	data = copy_data(base, 'drop-parts')
	os.remove(os.path.join(data, 'parts.tsv'))
	code, _, err = run(BODY, data)
	suite.check('coverage.tsv without parts.tsv is refused', code == 1
		and 'parts.tsv is missing' in err, err)

	data = copy_data(base, 'drop-names-files')
	os.remove(os.path.join(data, 'names.tsv'))
	os.remove(os.path.join(data, 'files.tsv'))
	code, body, err = run(BODY, data)
	suite.check('without names.tsv the public names line is dropped, and without '
		'files.tsv stderr says the opening was not checked', code == 0
		and 'Public names' not in body and 'was not checked' in err, err)

	data = copy_data(base, 'drop-collection')
	os.remove(os.path.join(data, 'collection.tsv'))
	code, _, err = run(BODY, data)
	suite.check('without collection.tsv nothing renders', code == 1
		and 'has no collection.tsv' in err, err)


def templating(suite, base):
	code, text, err = run(BODY, DATA, '--template')
	fields = [line for line in text.split('\n') if line.startswith('=== ')]
	suite.check('--template lists a field per module, checked and '
		'contradictions.marked, each with a placeholder', code == 0
		and '=== module tests/test_cli.py' in fields and '=== checked' in fields
		and '=== contradictions.marked' in fields
		and text.count('<<FILL') == len(fields), fields)
	suite.equal('and none of the dropped fields', [field for field in fields
		if field.split()[1] in ('existing', 'options.existing', 'never.intro',
		'never', 'checked.environment', 'checked.notes')], [])
	suite.check('and leaves out parts.tests when tests per part were counted',
		'=== parts.tests' not in fields, fields)
	lowered = text.lower()
	suite.equal('the template names neither the tool nor the lab', [word
		for word in BANNED if word in lowered], [])

	data = copy_data(base, 'template')
	edit(os.path.join(data, 'parts.tsv'), 'part\texcluded\ttests_before\t'
		'tests_after\tcounted\npkg/\tno\t3\t9\tper-test\npkg/cli/\tno\t2\t6\t'
		'per-test\nvendor/\tyes\t1\t2\tper-test\n(total)\tno\t3\t10\tper-test\n',
		'part\texcluded\ttests_before\ttests_after\tcounted\npkg/\tno\t\t\t\n'
		'pkg/cli/\tno\t\t\t\nvendor/\tyes\t\t\t\n(total)\tno\t\t\t\n')
	code, text, _ = run(BODY, data, '--template')
	suite.check('--template asks for parts.tests when tests per part were not '
		'counted', '=== parts.tests' in text, text[-800:])
	code, _, err = run(BODY, data)
	suite.check('and the body refuses without it', code == 1 and '"=== '
		'parts.tests"' in err, err)

	with open(os.path.join(data, 'prose.md'), 'a') as handle:
		handle.write('\n=== parts.tests\nno per-test coverage was recorded\n')
	code, body, err = run(BODY, data)
	suite.check('with it, the test columns show a dash and the reason follows '
		'the table', code == 0 and '| `pkg/` | – | – | 37.0% | 80.2% |' in body
		and 'Tests per part were not counted: no per-test coverage was '
		'recorded.' in body, err or body)


# The generators --------------------------------------------------------------

COLLECT = '''tests/test_a.py::test_one
tests/test_a.py::test_two[x]
tests/test_a.py::test_two[y]
tests/test_b.py::TestThing::test_three

=============================== warnings summary ===============================
tests/test_a.py::test_one
  something deprecated

4 tests collected in 0.01s
'''

TEST_A = '''
def test_one():
	"""Checks a derived value."""


def test_two(value):
	"""The label pins the current output; the docs say nothing."""
'''

TEST_B = '''
class TestThing:
	def test_three(self):
		# order pins current
		# behaviour
		pass
'''


def subcommands(suite):
	code, out, err = run(TABLES, '--help')
	listed = out.split('{', 1)[1].split('}', 1)[0].split(',') if '{' in out \
		else []
	suite.equal('the generators are collection, files, coverage and names',
		listed, ['collection', 'files', 'coverage', 'names'])


def collecting(suite, base):
	root = os.path.join(base, 'collect')
	os.makedirs(os.path.join(root, 'tests'))

	for name, text in (('test_a.py', TEST_A), ('test_b.py', TEST_B)):
		with open(os.path.join(root, 'tests', name), 'w') as handle:
			handle.write(text)

	output = os.path.join(root, 'collect.txt')

	with open(output, 'w') as handle:
		handle.write(COLLECT)

	data = os.path.join(root, 'data')
	code, out, err = run(TABLES, 'collection', '--collect-output', output,
		'--root', root, '--out', data)
	rows = tsv(os.path.join(data, 'collection.tsv'))
	suite.check('collection reads node ids up to the first blank line, not the '
		'warnings summary', code == 0 and [r['nodeid'] for r in rows] ==
		['tests/test_a.py::test_one', 'tests/test_a.py::test_two[x]',
		'tests/test_a.py::test_two[y]', 'tests/test_b.py::TestThing::test_three'],
		(err, rows))
	suite.equal('and marks the functions whose docstring or comments pin the '
		'current output', [r['pinned'] for r in rows], ['no', 'yes', 'yes',
		'yes'])

	with open(output, 'w') as handle:
		handle.write(COLLECT.replace('4 tests collected', '5 tests collected'))
	code, _, err = run(TABLES, 'collection', '--collect-output', output,
		'--out', data)
	suite.check('collection refuses a count that is not pytest\'s', code == 1
		and 'lists 4 node ids' in err, err)


def gcovr(files):
	"""gcovr JSON: {file: {line: count}}."""

	return {'gcovr/format_version': '0.14', 'files': [{'file': name,
		'lines': [{'line_number': n, 'count': c} for n, c in sorted(
		lines.items())], 'functions': []} for name, lines in sorted(
		files.items())]}


def coveragepy(files, contexts=None):
	"""coverage.py JSON: {file: (executed, missing)}, and per line its
	contexts."""

	out = {'meta': {'version': '7.6.1', 'format': 3, 'show_contexts':
		contexts is not None}, 'files': {}}

	for name, (executed, missing) in sorted(files.items()):
		entry = {'executed_lines': executed, 'missing_lines': missing}

		if contexts is not None:
			entry['contexts'] = {str(n): names for n, names in contexts.get(name,
				{}).items()}

		out['files'][name] = entry

	return out


def covering(suite, base):
	root = os.path.join(base, 'coverage')
	os.makedirs(root)
	existing = os.path.join(root, 'existing.json')
	suite_json = os.path.join(root, 'suite.json')
	write_json(existing, gcovr({'/build/src/a.cpp': {1: 1, 2: 0, 3: 0},
		'/build/src/b.h': {1: 0, 2: 0}, '/build/third/z.c': {1: 1, 2: 0}}))
	write_json(suite_json, gcovr({'/build/src/a.cpp': {1: 0, 2: 1, 3: 0, 4: 0},
		'/build/src/b.h': {1: 1, 2: 0}, '/build/tests/t.cpp': {1: 1},
		'/build/third/z.c': {1: 0, 2: 1}}))
	data = os.path.join(root, 'data')
	code, out, err = run(TABLES, 'coverage', '--existing', 'make test=' + existing,
		'--suite', suite_json, '--strip', '/build/', '--drop', 'tests/*',
		'--exclude', 'third/*', '--out', data)
	rows = {r['file']: r for r in tsv(os.path.join(data, 'coverage.tsv'))}
	suite.check('coverage unions executable lines across inputs and counts '
		'covered lines per role', code == 0 and rows.get('src/a.cpp') == {
		'file': 'src/a.cpp', 'part': 'src/a.cpp', 'excluded': 'no', 'lines': '4',
		'existing': '1', 'suite': '1', 'both': '2', 'existing:make test': '1'},
		(err, rows))
	suite.check('--drop removes files and --exclude marks them', 'tests/t.cpp'
		not in rows and rows.get('third/z.c', {}).get('excluded') == 'yes', rows)
	parts = tsv(os.path.join(data, 'parts.tsv'))
	suite.check('gcovr files without per-test data leave tests per part empty',
		all(r['tests_before'] == '' and r['counted'] == '' for r in parts)
		and parts[-1]['part'] == '(total)', parts)

	# coverage.py with per-test contexts, existing and suite.
	write_json(existing, coveragepy({'pkg/a.py': ([1], [2, 3]),
		'pkg/sub/b.py': ([], [1, 2])}, {'pkg/a.py': {1: ['old.py::test_x|run']}}))
	write_json(suite_json, coveragepy({'pkg/a.py': ([1, 2], [3]),
		'pkg/sub/b.py': ([1], [2])}, {'pkg/a.py': {1: ['', 'new.py::test_p|run'],
		2: ['new.py::test_p|setup', 'new.py::test_q|run']}, 'pkg/sub/b.py': {
		1: ['new.py::test_q|run']}}))
	code, out, err = run(TABLES, 'coverage', '--existing', 'old=' + existing,
		'--suite', suite_json, '--parts', 'dir', '--out', data)
	parts = {r['part']: r for r in tsv(os.path.join(data, 'parts.tsv'))}
	suite.check('coverage.py contexts count tests per part, a test once however '
		'many lines it runs, |run and |setup being one test', code == 0
		and (parts['pkg/']['tests_before'], parts['pkg/']['tests_after'],
		parts['pkg/sub/']['tests_before'], parts['pkg/sub/']['tests_after'],
		parts['(total)']['tests_after'], parts['pkg/']['counted']) ==
		('1', '3', '0', '1', '3', 'per-test'), (err, parts))

	# A suite run that also ran the existing test: counted once, with a warning.
	write_json(suite_json, coveragepy({'pkg/a.py': ([1, 2], [3]),
		'pkg/sub/b.py': ([1], [2])}, {'pkg/a.py': {1: ['old.py::test_x|run',
		'new.py::test_p|run'], 2: ['new.py::test_q|run']}}))
	code, out, err = run(TABLES, 'coverage', '--existing', 'old=' + existing,
		'--suite', suite_json, '--parts', 'dir', '--out', data)
	parts = {r['part']: r for r in tsv(os.path.join(data, 'parts.tsv'))}
	suite.check('a test in both runs is counted once and the overlap is warned '
		'about', code == 0 and parts['pkg/']['tests_before'] == '1'
		and parts['pkg/']['tests_after'] == '3'
		and '1 test(s) appear in both' in err, (err, parts))

	# A per-test directory for the suite, and no existing tests.
	per_test = os.path.join(root, 'per-test')
	os.makedirs(per_test)
	write_json(os.path.join(per_test, 'test_one.json'), gcovr({'a.cpp': {1: 1,
		2: 0}, 'b.cpp': {1: 0}}))
	write_json(os.path.join(per_test, 'test_two.json'), gcovr({'a.cpp': {1: 0,
		2: 1}, 'b.cpp': {1: 1}}))
	code, out, err = run(TABLES, 'coverage', '--suite', per_test, '--out', data)
	rows = {r['file']: r for r in tsv(os.path.join(data, 'coverage.tsv'))}
	parts = {r['part']: r for r in tsv(os.path.join(data, 'parts.tsv'))}
	suite.check('a directory of one file per test unions them, with no existing '
		'tests counted as 0', code == 0 and rows['a.cpp']['suite'] == '2'
		and rows['a.cpp']['existing'] == '0' and parts['a.cpp']['tests_after']
		== '2' and parts['b.cpp']['tests_after'] == '1'
		and parts['(total)']['tests_after'] == '2', (err, rows, parts))

	part_map = os.path.join(root, 'map.tsv')

	with open(part_map, 'w') as handle:
		handle.write('pattern\tpart\na.*\tFirst\n')
	code, _, err = run(TABLES, 'coverage', '--suite', per_test, '--part-map',
		part_map, '--out', data)
	suite.check('a part map that leaves a file without a part is refused',
		code == 1 and 'b.cpp matches no pattern' in err, err)


def gitting(suite, base):
	repo = os.path.join(base, 'repo')
	os.makedirs(os.path.join(repo, 'tests'))
	git(repo, 'init', '-q', '-b', 'main')

	with open(os.path.join(repo, 'setup.py'), 'w') as handle:
		handle.write('# setup\n')

	git(repo, 'add', '-A')
	git(repo, 'commit', '-q', '-m', 'base')
	git(repo, 'checkout', '-q', '-b', 'tests')

	for name in ('tests/test_x.py', 'tests/conftest.py'):
		with open(os.path.join(repo, name), 'w') as handle:
			handle.write('# test\n')

	git(repo, 'add', '-A')
	git(repo, 'commit', '-q', '-m', 'tests')
	data = os.path.join(base, 'files-data')
	code, out, err = run(TABLES, 'files', '--repo', repo, '--base', 'main',
		'--head', 'tests', '--out', data)
	rows = [(r['status'], r['path']) for r in tsv(os.path.join(data,
		'files.tsv'))]
	suite.equal('files records the clone, base and branch for the push',
		json.loads(read(os.path.join(data, 'branch.json'))), {'suite':
		os.path.abspath(repo), 'base': 'main', 'branch': 'tests'})
	suite.check('files lists what the branch changes against its base, sorted',
		code == 0 and rows == [('A', 'tests/conftest.py'), ('A',
		'tests/test_x.py')], (err, rows))


def naming(suite, base):
	root = os.path.join(base, 'names')
	os.makedirs(os.path.join(root, 'pkg'))
	os.makedirs(os.path.join(root, 'tests'))
	files = {
		'pkg/__init__.py': 'from .core import score\n',
		'pkg/core.py': 'def score():\n\tpass\n\ndef _private():\n\tpass\n\n'
			'class Reader:\n\tdef read(self):\n\t\tpass\n\tdef close(self):\n'
			'\t\tpass\n',
		'pkg/_hidden.py': 'def secret():\n\tpass\n',
		'pkg/limited.py': '__all__ = ["shown"]\n\ndef shown():\n\tpass\n\n'
			'def unlisted():\n\tpass\n',
		'tests/test_core.py': 'from pkg import score\n\ndef test_score():\n'
			'\tscore()\n\tReader().read()\n',
	}

	for name, text in files.items():
		with open(os.path.join(root, name), 'w') as handle:
			handle.write(text)

	data = os.path.join(root, 'data')
	code, out, err = run(TABLES, 'names', '--source', 'pkg', '--tests', 'tests',
		'--out', data, cwd=root)
	rows = [(r['name'], r['defined_in'], r['tests'])
		for r in tsv(os.path.join(data, 'names.tsv'))]
	suite.check('names lists public functions, classes and methods (honouring '
		'__all__, skipping private names and modules) with the test files that '
		'name them', code == 0 and rows == [('Reader', 'pkg/core.py', '1'),
		('Reader.close', 'pkg/core.py', '0'), ('Reader.read', 'pkg/core.py', '1'),
		('score', 'pkg/core.py', '1'), ('shown', 'pkg/limited.py', '0')],
		(err, rows))


def main(argv=None):
	import argparse

	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--update', action='store_true', help='rewrite '
		'expected.md from the fixture, then run the checks')
	args = parser.parse_args(argv)

	if args.update:
		code, body, err = run(BODY, DATA)

		if code != 0:
			sys.exit('the fixture does not render:\n' + err)

		with open(EXPECTED, 'w', encoding='utf-8', newline='\n') as handle:
			handle.write(body)

		print('rewrote {}'.format(EXPECTED))

	suite = test_runner.Suite()
	base = tempfile.mkdtemp(prefix='tests-pr-test-')

	try:
		print('\nthe renderer')
		rendering(suite, base)
		marking(suite, base)
		refusals(suite, base)
		driving(suite, base)
		allowing(suite, base)
		filling(suite, base)
		choosing(suite, base)
		singular(suite, base)
		capping(suite, base)
		dropping(suite, base)
		templating(suite, base)

		print('\nthe generators')
		subcommands(suite)
		collecting(suite, base)
		covering(suite, base)
		gitting(suite, base)
		naming(suite, base)
	finally:
		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
