#!/usr/bin/env python3
"""Check that the runner's own files in a worktree (`autoopt.RUNNER_FILES`)
never make it unmeasured, and that any other untracked file still does.

	python3 test_runner_files.py     # a second

No network.
"""

import os
import sys
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
from test_runner import Suite

CONFIG = {'repo': {'path': 'original'}}


def git(*args):
	subprocess.run(['git'] + list(args), check=True, capture_output=True)


def main():
	suite = Suite()
	base = tempfile.mkdtemp()

	try:
		checkout = autoopt.checkout_dir(base, 0, CONFIG)
		os.makedirs(checkout)
		git('-C', checkout, 'init', '-q')

		with open(os.path.join(checkout, 'core.py'), 'w') as out:
			out.write('x = 1\n')

		git('-C', checkout, 'add', '-A')
		git('-C', checkout, '-c', 'user.name=t', '-c', 'user.email=t@t',
			'commit', '-qm', 'base')

		for name in autoopt.RUNNER_FILES:
			with open(os.path.join(checkout, name), 'w') as out:
				out.write('bookkeeping\n')

		for mode in ('normal', 'all'):
			suite.check('the runner\'s own files are not unmeasured ({})'
				.format(mode), autoopt.unmeasured(checkout, untracked=mode) ==
				'', autoopt.unmeasured(checkout, untracked=mode))

		with open(os.path.join(checkout, 'stray.txt'), 'w') as out:
			out.write('stray\n')

		for mode in ('normal', 'all'):
			found = autoopt.unmeasured(checkout, untracked=mode)
			suite.check('a stray file still is ({})'.format(mode), found ==
				'?? stray.txt', found)

		found = autoopt.unmeasured(checkout, known={'stray.txt'},
			untracked='all')
		suite.check('a recorded test artifact is not', found == '', found)
		suite.check('worktree_changed ignores the runner\'s files only',
			autoopt.worktree_changed(base, CONFIG, 0, {}) is True)
		os.remove(os.path.join(checkout, 'stray.txt'))
		suite.check('worktree_changed: only the runner\'s files is unchanged',
			autoopt.worktree_changed(base, CONFIG, 0, {}) is False)
		import run_iteration
		with open(os.path.join(checkout, 'core.py'), 'w') as out:
			out.write('x = 2\n')

		run_iteration.commit_worktree(checkout, {'id': 1, 'title': 'edit'})
		listed = subprocess.run(['git', '-C', checkout, 'show', '--name-only',
			'--format=', 'HEAD'], capture_output=True, text=True).stdout.split()
		suite.check('the runner commits the change and not its own files',
			listed == ['core.py'] and all(os.path.exists(os.path.join(checkout,
			name)) for name in autoopt.RUNNER_FILES), str(listed))
	finally:
		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for line in suite.failed:
		print('  ' + line)

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
