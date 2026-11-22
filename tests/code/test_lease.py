#!/usr/bin/env python3
"""Check the GPU lease a benchmark-running script takes for itself
(`autoopt.hold_lease`, `gpu_command`), the `--under` words a stage that runs
as its own unit is started with, and that setup does not close on a GPU
repository search with no allocator.

	python3 test_lease.py     # a few seconds

No GPU is used: the allocator is a script that sets CUDA_VISIBLE_DEVICES.
"""

import os
import sys
import json
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import autoopt

CHILD = '''import sys, json
sys.path.insert(0, {here!r})
import autoopt
autoopt.LEASE_RETRY_SECONDS = 0.2
print('devices', autoopt.hold_lease(json.loads(sys.argv[1])))
'''


class Suite:
	def __init__(self):
		self.passed, self.failed = [], []

	def check(self, name, ok, detail=''):
		if ok:
			self.passed.append(name)
			print('  ok    {}'.format(name))
		else:
			self.failed.append('{}: {}'.format(name, detail))
			print('  FAIL  {} -- {}'.format(name, detail))


def main():
	suite = Suite()

	with tempfile.TemporaryDirectory() as base:
		child = os.path.join(base, 'child.py')
		allocator = os.path.join(base, 'alloc')
		idle = os.path.join(base, 'idle')

		with open(child, 'w') as out:
			out.write(CHILD.format(here=SCRIPTS))

		busy = os.path.join(base, 'busy')

		# `busy` claims nothing on its first call, as an allocator with no GPU
		# free does, and leases on its second.
		for path, line in ((allocator, 'CUDA_VISIBLE_DEVICES=7 exec "$@"'), (
				idle, 'exec "$@"'), (busy, 'if [ -e {0} ]; then '
				'CUDA_VISIBLE_DEVICES=5 exec "$@"; fi; touch {0}; echo none '
				'free >&2; exit 1'.format(os.path.join(base, 'asked')))):
			with open(path, 'w') as out:
				out.write('#!/bin/bash\n[ "$1" = -- ] && shift\n' + line + '\n')

			os.chmod(path, 0o755)

		def held(config, devices=None):
			env = dict((key, value) for key, value in os.environ.items() if key
				not in ('CUDA_VISIBLE_DEVICES', 'AUTOOPT_LEASE_MARK'))

			if devices:
				env['CUDA_VISIBLE_DEVICES'] = devices

			done = subprocess.run([sys.executable, child, json.dumps(config)],
				capture_output=True, text=True, env=env)

			return done.returncode, done.stdout + done.stderr

		code, out = held({'gpu': False})
		suite.check('a search with no GPU needs no lease', code == 0 and
			'devices None' in out, out)
		code, out = held({}, '3')
		suite.check('a lease already held is kept', code == 0 and 'devices 3'
			in out, out)
		code, out = held({})
		suite.check('a GPU search with no lease and no gpu_command is refused, '
			'naming the setting', code != 0 and 'gpu_command' in out and
			'devices' not in out.replace('CUDA_VISIBLE_DEVICES', ''), out)
		code, out = held({'gpu_command': allocator + ' --'})
		suite.check('with gpu_command the script starts again under the '
			'allocator and holds what it set', code == 0 and 'devices 7' in
			out, out)
		code, out = held({'gpu_command': idle + ' --'})
		suite.check('an allocator that does not set CUDA_VISIBLE_DEVICES is '
			'refused, not run in a loop', code != 0 and 'must set it' in out,
			out)

		code, out = held({'gpu_command': busy + ' --'})
		suite.check('an allocator with no GPU free is tried again until it '
			'leases, and the script then runs once', code == 0 and
			out.count('devices 5') == 1 and 'no GPU free; again' in out, out)

		kept = os.environ.pop('CUDA_VISIBLE_DEVICES', None)
		os.environ['CUDA_VISIBLE_DEVICES'] = '3'
		suite.check('a command started as its own unit gets the flag whatever '
			'lease this process holds', autoopt.lease_flag({
			'gpu_command': 'a --'}) == ['--under', 'a --'])
		del os.environ['CUDA_VISIBLE_DEVICES']

		try:
			suite.check('the flag quotes the allocator as one word',
				autoopt.lease_flag({'gpu_command': 'gpu-alloc run -n 1 --'}) ==
				['--under', 'gpu-alloc run -n 1 --'] and autoopt.lease_flag({
				'gpu': False, 'gpu_command': 'x'}) == [] and
				autoopt.lease_flag({}) == [])
			work = os.path.join(base, 'search')
			os.makedirs(os.path.join(work, 'autooptimize'))
			with open(os.path.join(work, 'autooptimize', 'config.json'), 'w') \
					as out:
				json.dump({'repo': 'x', 'task': 't', 'command': 'c {iteration} '
					'{seed} {out}', 'objective': {'name': 't', 'direction':
					'min'}}, out)

			with open(os.path.join(work, 'autooptimize', 'ended.json'), 'w') \
					as out:
				json.dump({'winner': {'id': 1}}, out)

			os.environ['AUTOOPT_SKIP_SPEC_CHECK'] = '1'
			root = os.path.join(work, 'autooptimize')
			owed = lambda: [line for line in autoopt.setup_missing(root,
				autoopt.load_config(root)) if 'gpu_command' in line]
			suite.check('setup does not close on a GPU repository search with '
				'no gpu_command', len(owed()) == 1, owed())
		finally:
			if kept is not None:
				os.environ['CUDA_VISIBLE_DEVICES'] = kept

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for line in suite.failed:
		print('  - ' + line)

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
