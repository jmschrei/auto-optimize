"""One timed run of a probe command, shared by `paired.py` and `cold.py`.

The command prints its seconds as the last number on its last line of
stdout and exits non-zero when its output is wrong.
"""

import os
import re
import sys
import shlex
import signal
import subprocess

NUMBER = re.compile(r'[-+]?\d*\.?\d+(?:[eE][-+]?\d+)?')


def run_probe(template, directory, side, cache, extra=(), pattern=None,
		timeout=None):
	"""The seconds one run of `template` printed, or exit with why it did not.

	`{src}` is `directory` and `{side}` the side's name, both shell-quoted.
	`cache` is the compiler cache the run gets for numba, Triton and
	Inductor; `extra` is `KEY=VALUE` items for the environment. `pattern` is
	a regex whose first group is the seconds on the last line, for output
	like `t=1.2s on GPU0` where the last number is not the time. A run past
	`timeout` seconds is killed with everything it started, and exits 4.
	"""

	env = dict(os.environ, NUMBA_CACHE_DIR=cache,
		TRITON_CACHE_DIR=os.path.join(cache, 'triton'),
		TORCHINDUCTOR_CACHE_DIR=os.path.join(cache, 'inductor'))

	for item in extra:
		key, _, value = item.partition('=')
		env[key] = value

	command = template.format(src=shlex.quote(directory),
		side=shlex.quote(side))
	process = subprocess.Popen(command, shell=True, env=env, text=True,
		stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)

	try:
		out, err = process.communicate(timeout=timeout or None)
	except subprocess.TimeoutExpired:
		# The shell's children too: a hung tool outlives a killed shell.
		try:
			os.killpg(process.pid, signal.SIGKILL)
		except ProcessLookupError:
			pass

		process.communicate()
		print('side {} timed out after {:.0f} s and was killed: {}'.format(side,
			timeout, command), file=sys.stderr)
		sys.exit(4)

	if process.returncode != 0:
		sys.exit('side {} failed (exit {}): {}\n{}'.format(side,
			process.returncode, command, (err or out)[-2000:]))

	lines = [line for line in out.splitlines() if line.strip()]
	last = lines[-1] if lines else ''
	numbers = NUMBER.findall(last)

	if pattern:
		found = re.search(pattern, last)
		numbers = [found.group(1)] if found else []

	if not numbers:
		sys.exit('side {} printed no number on its last line: {!r}'.format(
			side, lines[-1] if lines else ''))

	return float(numbers[-1])
