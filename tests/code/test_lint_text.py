#!/usr/bin/env python3
"""Check lint_text.py on the phrasings it must flag and on the real code words
that must pass.

	python3 test_lint_text.py     # a second
"""

import os
import sys
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import lint_text


def words(text, **kw):
	return {hit.rule for hit in lint_text.check(text, **kw)}


def git(repo, *args):
	subprocess.run(['git', '-C', repo] + list(args), check=True,
		capture_output=True)


def main():
	passed, failed = 0, []

	def check(name, ok, detail=''):
		nonlocal passed
		print('  {}  {}'.format('ok  ' if ok else 'FAIL', name))

		if ok:
			passed += 1
		else:
			failed.append('{}: {}'.format(name, detail))

	check('a table label with "search" is caught', 'search' in words(
		'search larger.bed (bridge)', strict=True))
	check('the same label inside a table row of a body is caught', 'search' in
		words('| Category | Input |\n|---|---|\n| intersect | search larger.bed '
		'(bridge) |\n'))
	check('"stock grep" is caught', 'stock' in words('stock grep '
		'printed it as a C string'))
	check('"winning iteration" is caught', {'winner', 'iteration'} <= words(
		'Each winning iteration is one commit.'))
	check('baseline, gate and "the search" are caught', {'baseline', 'gate',
		'search'} <= words('The baseline passed the gate of the search; the '
		'search ended.'))
	check('"the base build" is caught', 'base' in words('timed on the base '
		'build'))
	check('a local path and ~/ are caught', {'local path'} == words(
		'see /tmp/x and ~/work/y'))
	check('a label is caught only when named', 'label' in words('A175 won',
		labels=['A175']) and 'label' not in words('A100 GPUs', labels=['A175']))
	check('binary search passes', words('uses a binary search over the '
		'offsets') == set())
	check('iteration over records passes', words('one iteration over the '
		'records, then the base pair is read; round to the nearest') == set())
	check('a name in a code span passes', words('the `--search` option and '
		'`binary_search`') == set())
	check('a path in a code span is still a hit', 'local path' in words(
		'`~/x/y`'))
	check('a URL is not read', words('see https://example.com/tmp/winner') ==
		set())
	check('a fenced block holds names but not paths', words('```\nfor iteration '
		'in x:\n```') == set() and 'local path' in words('```\ncd /tmp/x\n```'))
	check('a long cell is prose, not a label', words('| a | The search over '
		'the table uses a binary search. |') >= {'search'} and words('| a | '
		'Reads each record once and compares it with the database before the '
		'last block. A search in it. |') == set())

	with tempfile.TemporaryDirectory() as scratch:
		repo = os.path.join(scratch, 'r')
		os.makedirs(repo)
		git(repo, 'init', '-q', '-b', 'main')
		git(repo, 'config', 'user.email', 'a@b')
		git(repo, 'config', 'user.name', 'a')
		with open(os.path.join(repo, 'a.c'), 'w') as out:
			out.write('int x;\n')
		git(repo, 'add', '.')
		git(repo, 'commit', '-qm', 'Start')
		git(repo, 'checkout', '-qb', 'work')
		with open(os.path.join(repo, 'a.c'), 'w') as out:
			out.write('int x;\n// stock grep printed it as a C string\n'
				'int y; // binary search over y\n/* the winner\n * keeps it */\n'
				'#include <x.h>\nchar *s = "stock // x";\n')
		with open(os.path.join(repo, 'b.py'), 'w') as out:
			out.write('x = 1  # kept: A175\n"""Doc.\n\nWorks in /tmp/z.\n"""\n'
				'y = "# stock"\n')
		git(repo, 'add', '.')
		git(repo, 'commit', '-qm', 'Use the winning iteration\n\nbody')
		found = lint_text.diff_problems(repo, 'work', 'main', ['A175'])
		text = '\n'.join(found)
		check('added comments in C: line, block and trailing', 'a.c:2:' in text
			and 'a.c:4:' in text and 'a.c:3:' not in text, text)
		check('a preprocessor line and a string are not comments', 'a.c:6' not
			in text and 'a.c:7' not in text, text)
		check('Python trailing comments and docstrings are read', 'b.py:1:' in
			text and 'b.py:4:' in text and 'b.py:6' not in text, text)
		check('commit messages are read', any('winner' in p for p in
			lint_text.commit_problems(repo, 'work', 'main')))
		done = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'lint_text.py'),
			'diff-comments', repo, 'work', 'main'], capture_output=True, text=True)
		check('the CLI exits 1 listing every hit with its line', done.returncode
			== 1 and 'a.c:2:' in done.stderr and 'Traceback' not in done.stderr,
			done.stderr)
		done = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'lint_text.py'),
			'commits', repo, 'work', 'main'], capture_output=True, text=True)
		check('the CLI commits subcommand lists the message hits', done.returncode
			== 1 and 'winner' in done.stderr, done.stderr)
		page = os.path.join(scratch, 'body.md')
		with open(page, 'w') as out:
			out.write('The winner is A175.\n')
		done = subprocess.run([sys.executable, os.path.join(SCRIPTS, 'lint_text.py'),
			'body', page, '--labels', 'A175'], capture_output=True, text=True)
		check('the CLI body subcommand lists the hits', done.returncode == 1 and
			'label' in done.stderr and 'winner' in done.stderr, done.stderr)
		check('the CLI title passes a clean title', subprocess.run([
			sys.executable, os.path.join(SCRIPTS, 'lint_text.py'), 'title',
			'Vectorize scoring']).returncode == 0)

	print('\n{} passed, {} failed'.format(passed, len(failed)))

	for item in failed:
		print('  ' + item)

	return 1 if failed else 0


if __name__ == '__main__':
	sys.exit(main())
