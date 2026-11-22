#!/usr/bin/env python3
"""Check `preflight.py`: its four checks on hand-made lines and on a small
repository search, and that `promote` refuses an iteration with a problem.

	python3 test_preflight.py     # about half a minute

No network, no gh.
"""

import os
import sys
import json
import shlex
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
import preflight
import test_runner as tr

PREFLIGHT = os.path.join(SCRIPTS, 'preflight.py')
SPACES = ''.join('def f{}():\n    x = {}\n    return x\n\n'.format(i, i) for i
	in range(4))


def units(suite):
	suite.equal('four fifths of the indented lines decide the style',
		(preflight.style_of(['\ta'] * 9 + ['  b']), preflight.style_of(['  a']
		* 9 + ['\tb']), preflight.style_of(['\ta'] * 5 + ['  b'] * 5)),
		('tab', 'space', None))
	suite.equal('fewer than five indented lines decide nothing',
		preflight.style_of(['\ta'] * 4 + ['b'] * 20), None)
	suite.equal('one leading space is alignment, not indentation',
		preflight.leading(' * a comment line'), None)
	hits = preflight.license_problems({'a.c': [(3, '// SPDX-License-Identifier: '
		'GPL-2.0-or-later'), (4, '// SPDX-License-Identifier: MIT'), (5,
		'/* GNU Lesser General Public License */'), (6, 'int gplot = 3;'), (7,
		'// for non-commercial use only'), (8, '// licensed CC-BY-SA 4.0'),
		(9, '// Mozilla Public License 2.0'), (10, '// BSD-3-Clause')]})
	suite.check('GPL, LGPL, MPL, non-commercial and share-alike markers are '
		'found; MIT, BSD and a word holding gpl are not', [h.split(':')[1] for
		h in hits] == ['3', '5', '7', '8', '9'], hits)


def search_checks(suite, base):
	original = os.path.join(base, 'original')
	tr.make_original(original)

	with open(os.path.join(original, 'pkg', 'spaced.py'), 'w') as out:
		out.write(SPACES)

	tr.git('-C', original, 'add', '-A')
	tr.git('-C', original, '-c', 'user.name=t', '-c', 'user.email=t@t',
		'commit', '-q', '-m', 'a file indented with spaces')
	harness = os.path.join(base, 'bench.py')

	with open(harness, 'w') as out:
		out.write(tr.HARNESS)

	work = os.path.join(base, 'search')
	search = os.path.join(work, 'autooptimize')
	tr.call([tr.AUTOOPT, 'init', work, '--task', 'test', '--repo', original,
		'--copy', harness, '--command', 'python3 {}/harness/bench.py --src '
		'{{src}} --seed {{seed}} --out {{out}}'.format(shlex.quote(search)),
		'--objective', 'score', '--direction', 'max', '--gates', json.dumps([
		{'name': 'cost', 'op': '<=', 'threshold': 1.0}]), '--observers',
		'size', '--no-gpu', '--runs', '3', '--staged', '--confirm-runs', '3',
		'--promote', 'orchestrator'])
	tr.call([tr.RUNNER, autoopt.iteration_dir(search, 0)])

	def add(identifier, parent, quality, files):
		directory = tr.call([tr.AUTOOPT, 'new', search, '--id', str(identifier),
			'--parent', str(parent), '--title', 'quality {}'.format(quality),
			'--category', 'MISC']).strip().splitlines()[-1]
		tr.set_quality(os.path.join(directory, 'src'), quality)

		for path, text in files.items():
			with open(os.path.join(directory, 'src', path), 'w') as out:
				out.write(text)

		tr.call([tr.RUNNER, directory])

	def check(*flags):
		done = subprocess.run([sys.executable, PREFLIGHT, work] + list(flags),
			capture_output=True, text=True)
		return done.returncode, done.stdout + done.stderr

	add(1, 0, 0.60, {})
	code, out = check('--id', '1')
	suite.check('a clean change has no problem and exits 0', code == 0 and
		'no problem in A1' in out, out)
	out = tr.call([tr.AUTOOPT, 'promote', search, '--id', '1'])
	suite.check('and is promoted', autoopt.load_state(search)['root'] == 1,
		out)
	add(2, 1, 0.70, {'pkg/spaced.py': SPACES + 'def g():\n\treturn 1\n',
		'pkg/vendored.py': '# SPDX-License-Identifier: GPL-3.0-only\nX = 1\n',
		'pkg/note.py': '# faster than the baseline A1\nY = 2\n'})
	code, out = check('--id', '2')
	suite.check('tabs in a file indented with spaces, a GPL marker and a '
		'comment naming the search are each named with file and line, exit 1',
		code == 1 and 'indent: pkg/spaced.py:18: 1 added line(s) are indented '
		'with tabs' in out and 'license: pkg/vendored.py:1:' in out and
		'text: pkg/note.py:1' in out, out)
	done = subprocess.run([sys.executable, tr.AUTOOPT, 'promote', search,
		'--id', '2'], capture_output=True, text=True)
	suite.check('promote refuses it, naming the script and every problem',
		done.returncode != 0 and 'fails the preflight' in done.stderr and
		'indent: ' in done.stderr and 'license: ' in done.stderr and
		autoopt.load_state(search)['root'] == 1, done.stderr)
	add(3, 1, 0.72, {'pkg/spaced.py': SPACES + 'def g():\n    return 1\n'})
	code, out = check('--id', '3')
	suite.check('the same change indented as the file is clean', code == 0,
		out)
	code, out = check('--id', '2', '--path')
	record = json.load(open(os.path.join(search, 'preflight.json')))
	suite.check('--path checks from the copied commit and writes '
		'autooptimize/preflight.json', code == 1 and record['winner'] == 2 and {item[0]
		for item in record['problems']} == {'indent', 'license', 'text'},
		record)
	config = json.load(open(os.path.join(search, 'config.json')))
	config['after']['oldest_toolchain_command'] = ('test -f {src}/pkg/core.py '
		'&& echo "old compiler: syntax error" >&2 && exit 3')
	json.dump(config, open(os.path.join(search, 'config.json'), 'w'))
	code, out = check('--id', '3')
	suite.check('after.oldest_toolchain_command runs on an export of the tree '
		'and its failure is a problem with the last output line', code == 1
		and 'toolchain: after.oldest_toolchain_command exited 3: old compiler: '
		'syntax error' in out, out)
	suite.check('the export is removed from autooptimize/tmp', os.listdir(
		os.path.join(search, 'tmp')) == [])
	config['after']['oldest_toolchain_command'] = 'test -f {src}/pkg/core.py'
	json.dump(config, open(os.path.join(search, 'config.json'), 'w'))
	suite.equal('a command that passes is no problem', check('--id', '3')[0],
		0)

	# What a kept iteration's subagent writes about its change, in its notes.md.
	notes = os.path.join(autoopt.iteration_dir(search, 3), 'notes.md')
	body = ('## The idea\nx\n\n## What happened\ny\n\n## Change\nname: {}\n'
		'Returns one from g.\n\n## Commit message\n{}\n')

	with open(notes, 'w') as out:
		out.write(body.format('beat the baseline A1', 'Add g.\n\nIt returns '
			'one'))

	code, out = check('--id', '3')
	suite.check('a ## Change that names the search and a ## Commit message '
		'whose subject ends with a period are text problems', code == 1 and
		'A3 notes.md ## Change' in out and 'A3 notes.md ## Commit message: the '
		'subject must not end with a period' in out, out)

	with open(notes, 'w') as out:
		out.write(body.format('Add g', 'Add g\n\nIt returns one.'))

	suite.equal('written by the rules they pass', check('--id', '3')[0], 0)

	# A kept iteration must write both sections before `readme` (finish).
	def edit(identifier, name, change):
		path = os.path.join(autoopt.iteration_dir(search, identifier), name)
		record = json.load(open(path))
		change(record)
		json.dump(record, open(path, 'w'))

	plain = os.path.join(autoopt.iteration_dir(search, 1), 'notes.md')

	with open(plain, 'w') as out:
		out.write(''.join('## {}\nx\n\n'.format(name) for name in
			autoopt.NOTES_REQUIRED))

	kept = json.load(open(os.path.join(autoopt.iteration_dir(search, 1),
		'result.json'))).get('keep')
	edit(1, 'result.json', lambda r: r.pop('keep', None))
	suite.equal('an iteration that was not kept owes nothing',
		autoopt.fragment_problem(search, 1), None)
	edit(1, 'result.json', lambda r: r.update(keep={'verdict': 'pass', 'why':
		'x'}))
	edit(3, 'result.json', lambda r: r.update(keep={'verdict': 'pass', 'why':
		'x'}))
	lacking = autoopt.fragment_problem(search, 1)
	suite.check('a kept one that wrote neither section is told both',
		lacking and '## Change' in lacking and '## Commit message' in lacking,
		lacking)
	suite.equal('one that wrote both is not', autoopt.fragment_problem(search,
		3), None)
	refused = subprocess.run([sys.executable, tr.AUTOOPT, 'readme', search,
		'--id', '1'], capture_output=True, text=True)
	suite.check('and `readme` refuses its README until it does',
		refused.returncode != 0 and '## Change' in refused.stderr,
		refused.stderr)
	base_notes = ''.join('## {}\nx\n\n'.format(name) for name in
		autoopt.NOTES_REQUIRED)

	with open(plain, 'w') as out:
		out.write(base_notes + '## Change\nname: Beat the baseline A3\nReturns '
			'two.\n\n## Commit message\nAdd h.\n\nIt returns two.\n')

	refused = subprocess.run([sys.executable, tr.AUTOOPT, 'readme', search,
		'--id', '1'], capture_output=True, text=True)
	suite.check('sections that name the search or break the message rules '
		'are refused when they are written, not later',
		refused.returncode != 0 and 'breaks the rules of text read outside' in
		refused.stderr and '## Change' in refused.stderr and 'must not end '
		'with a period' in refused.stderr, refused.stderr)

	with open(plain, 'w') as out:
		out.write(base_notes + '## Change\nname: Add h\nReturns two.\n\n## '
			'Commit message\nAdd h\n\nIt returns two.\n')

	suite.equal('and writes it once both are there', subprocess.run([
		sys.executable, tr.AUTOOPT, 'readme', search, '--id', '1'],
		capture_output=True).returncode, 0)

	with open(plain, 'w') as out:
		out.write(''.join('## {}\nx\n\n'.format(name) for name in
			autoopt.NOTES_REQUIRED))

	edit(1, 'result.json', lambda r: r.pop('keep'))
	suite.equal('preflight does not ask an ordinary iteration for them',
		check('--id', '1')[0], 0)
	edit(1, 'meta.json', lambda m: m.update(after_end=True))
	code, out = check('--id', '1')
	suite.check('but an iteration made after the end is not promoted without '
		'them', code == 1 and 'A1 notes.md lacks' in out, out)
	edit(1, 'meta.json', lambda m: m.pop('after_end'))
	edit(1, 'result.json', lambda r: r.update(keep=kept) if kept else None)
	os.remove(plain)
	config['after']['commit_trailer'] = 'Co-Authored-By: X <x@y>'
	json.dump(config, open(os.path.join(search, 'config.json'), 'w'))
	suite.equal('the recorded trailer is added later, so a message without '
		'it still passes', check('--id', '3')[0], 0)
	config['after']['commit_trailer'] = None
	json.dump(config, open(os.path.join(search, 'config.json'), 'w'))
	suite.equal('change_fragment reads the three parts', autoopt.change_fragment(
		search, 3), {'name': 'Add g', 'change': 'Returns one from g.',
		'message': 'Add g\n\nIt returns one.\n'})
	suite.equal('and gives None for an iteration that wrote none',
		autoopt.change_fragment(search, 1), {'name': None, 'change': None,
		'message': None})

	return work, search, config


def main():
	suite = tr.Suite()
	units(suite)
	base = tempfile.mkdtemp(prefix='preflight-')

	try:
		search_checks(suite, base)
	finally:
		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for line in suite.failed:
		print('  - ' + line[:900])

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
