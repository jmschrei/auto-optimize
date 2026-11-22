#!/usr/bin/env python3
"""Check the version fields of the record files and the skill's name in
config.json: `init`, the runner, `save_state` and `end` write
`schema_version`, `init` records which skill made the search, and every
script refuses a search another skill made.

	python3 test_records.py     # about half a minute

Real searches in a temporary directory, driven with the real scripts. No
GPU, no network.
"""

import os
import sys
import json
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt
import test_end
import test_runner

AUTOOPT = test_runner.AUTOOPT
RUNNER = test_runner.RUNNER


def read_json(path):
	with open(path) as handle:
		return json.load(handle)


def run(*args):
	return subprocess.run([sys.executable] + list(args), capture_output=True,
		text=True)


def main():
	suite = test_runner.Suite()
	base = tempfile.mkdtemp(prefix='records-')

	with open(os.path.join(os.path.dirname(SCRIPTS), 'VERSION')) as handle:
		version = handle.read().strip()

	search = test_runner.make_search(os.path.join(base, 'a'), staged=False,
		runs=3)
	config = read_json(os.path.join(search, 'config.json'))

	suite.equal('init records the skill\'s name and its VERSION',
		{key: config['skill'].get(key) for key in ('name', 'version')},
		{'name': 'auto-optimize-code', 'version': version})
	suite.check('and the commit key, null or a hash', 'commit' in
		config['skill'], config['skill'])
	suite.equal('config.json carries schema_version', config.get(
		'schema_version'), autoopt.SCHEMA_VERSION)
	suite.equal('the baseline\'s result.json carries schema_version',
		autoopt.load_result(search, 0).get('schema_version'),
		autoopt.SCHEMA_VERSION)

	state = autoopt.load_state(search)
	autoopt.save_state(search, state)
	suite.equal('save_state writes schema_version into state.json',
		read_json(os.path.join(search, 'state.json')).get('schema_version'),
		autoopt.SCHEMA_VERSION)

	# A search another skill made: refused by name, with the skill to use.
	theirs = os.path.join(base, 'theirs')
	shutil.copytree(os.path.join(base, 'a'), theirs, symlinks=True)
	path = os.path.join(theirs, 'autooptimize', 'config.json')
	other = read_json(path)
	other['skill']['name'] = 'auto-optimize-ml'
	with open(path, 'w') as handle:
		json.dump(other, handle)

	done = run(AUTOOPT, 'status', theirs)
	suite.check('status refuses a search the auto-optimize-ml skill made, '
		'naming it', done.returncode == 1 and 'made by the auto-optimize-ml '
		'skill' in done.stderr, done.stderr[-300:])
	done = run(RUNNER, '--dry-run', autoopt.iteration_dir(os.path.join(
		theirs, 'autooptimize'), 0))
	suite.check('and so does the runner', done.returncode == 1
		and 'auto-optimize-ml' in done.stderr, done.stderr[-300:])

	# A search made before the field existed has no name, and is read.
	del other['skill']['name']
	with open(path, 'w') as handle:
		json.dump(other, handle)
	done = run(AUTOOPT, 'status', theirs, '--brief')
	suite.check('a search with no skill name is accepted', done.returncode
		== 0, done.stderr[-300:])

	# end writes ended.json with the field.
	real = autoopt.run_dashboard
	autoopt.run_dashboard = lambda root, *flags: (0, '')
	try:
		ended = test_end.build(os.path.join(base, 'ended'))
		code, out = test_end.run_end(ended)
	finally:
		autoopt.run_dashboard = real
	suite.check('end succeeds on a finished search', code in (0, None),
		out[-400:])
	suite.equal('ended.json carries schema_version', read_json(os.path.join(
		ended, 'ended.json')).get('schema_version'), autoopt.SCHEMA_VERSION)

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
