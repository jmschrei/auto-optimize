"""Drive the monitor in headless Chromium against a synthetic search.

	python3 test_frontend.py                  # build, publish, check
	python3 test_frontend.py --shot x.png     # and a full-page screenshot
	python3 test_frontend.py --headed         # watch it
	python3 test_frontend.py --scale          # 2,600 iterations: the checks that
	                                          # only fail at a real search's size

Builds the fixture by driving `autoopt.py` and `run_iteration.py`, publishes
it into a fresh temporary directory, serves that over plain HTTP so the page
runs its standalone branch, and asserts on the DOM, on computed styles and on
geometry. Any console error or page exception fails the run: a silently blank
panel is the failure these pages have. Prints `N passed, M failed` and exits
non-zero on a failure. Scratch directories come from `mktemp` and are left
for the system to clean.
"""

import os
import math
import collections
import re
import sys
import json
import time
import socket
import hashlib
import argparse
import tempfile
import threading
import subprocess
import http.server
import socketserver

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_fixture
import spacing_check
import monitor_writer
import gpu_scan
import autoopt_monitor
import scan


RESULTS = []


def check(name, condition, detail=''):
	RESULTS.append((name, bool(condition)))
	print('{}  {}{}'.format('PASS' if condition else 'FAIL', name,
		'  -- ' + str(detail) if detail and not condition else ''))


def equal(name, got, want):
	check(name, got == want, 'got {!r}, want {!r}'.format(got, want))


def make_fixture_results(root):
	sys.path.insert(0, os.path.join(os.path.dirname(HERE)))
	import autoopt
	return autoopt.all_results(root)


# The last line of iteration 3's README, beyond its excerpt.
PANEL_TAIL = 'panel-tail-7f3c'


def publish(root, out):
	"""Everything a writer would write, once, into a directory nothing has
	published before: serving an old one tests that run's assets."""

	scanner = scan.RunScanner(root)
	monitor_writer.install_assets(out)

	snapshot = scanner.snapshot()
	snapshot['assets'] = monitor_writer.assets_version()

	monitor_writer.write_json(os.path.join(out, 'state.json'), snapshot)
	os.makedirs(os.path.join(out, 'readmes'), exist_ok=True)
	for identifier, (mtime, text) in scanner.readmes().items():
		monitor_writer.write_json(os.path.join(out, 'readmes', '%d.json' % identifier),
			{'id': identifier, 'text': text})

	# Two readings, so the sparkline has a line to draw. The answer is the
	# host's shared one, which a live publisher may be refreshing past
	# SHARED_WAIT; the history records a repeated reading once, so sample
	# until it changes.
	first = scanner.hardware.resources().get('gpu_sampled_at')
	deadline = time.time() + 60

	while True:
		time.sleep(gpu_scan.SHARED_SECONDS + 0.2)
		payload = scanner.hardware.resources()
		if payload.get('gpu_sampled_at') != first or time.time() > deadline:
			break

	monitor_writer.write_json(os.path.join(out, 'resources.json'), payload)

	return json.load(open(os.path.join(out, 'state.json')))


def variant(out, name, edit):
	"""A copy of index.html with one edit, for the stale-copy checks."""

	with open(os.path.join(out, 'index.html')) as infile:
		page = infile.read()

	with open(os.path.join(out, name), 'w') as outfile:
		outfile.write(edit(page))


def serve(directory, accept_put=False):
	class Handler(http.server.SimpleHTTPRequestHandler):
		def do_PUT(self):
			# The page's live request: the body does not matter, the write does.
			if not accept_put:
				return self.send_error(501)

			body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
			name = os.path.basename(self.path.split('?')[0])
			with open(os.path.join(directory, name), 'wb') as outfile:
				outfile.write(body)
			self.send_response(204)
			self.end_headers()

		# `directory` must go through the constructor; a class attribute is
		# overwritten in __init__ and the server serves the cwd instead.
		def __init__(self, *args, **kwargs):
			super().__init__(*args, directory=directory, **kwargs)

		def end_headers(self):
			self.send_header('Cache-Control', 'no-store')
			super().end_headers()

		def log_message(self, *args):
			pass

	server = socketserver.ThreadingTCPServer(('127.0.0.1', 0), Handler)
	server.daemon_threads = True
	threading.Thread(target=server.serve_forever, daemon=True).start()

	return server, 'http://127.0.0.1:{}/'.format(server.server_address[1])


def channel_gap(a, b):
	"""Largest per-channel difference (0-255) between two colours given as
	`#rrggbb`, `rgb(r, g, b)` or the `color(srgb r g b)` of `color-mix()`."""

	def parse(text):
		if text.startswith('#'):
			return [int(text[i:i + 2], 16) for i in (1, 3, 5)]
		inner = text[text.index('(') + 1:text.index(')')]
		if inner.startswith('srgb'):
			return [round(255 * float(v)) for v in inner.split()[1:4]]
		return [int(float(v)) for v in inner.split(',')[:3]]

	return max(abs(x - y) for x, y in zip(parse(a), parse(b)))


def points_in(page, plot):
	"""Markers a Plotly chart actually painted. Counting traces passes on a
	chart that drew nothing."""

	return page.eval_on_selector_all(
		'#{} .scatterlayer .trace path.point'.format(plot), 'n => n.length')


def expected_points(snapshot, hidden=()):
	"""Markers the main chart paints: every settled iteration with an
	objective, plus one on the floor per iteration still running, plus one at
	the baseline's value per iteration set aside without a run."""

	rows = [r for r in snapshot['iterations'] if r['category'] not in hidden]
	settled = [r for r in rows if r['status'] != 'running'
		and r['value'] is not None]
	flight = [r for r in rows if r['status'] == 'running']
	closed = [r for r in snapshot.get('abandoned') or [] if r['category'] not in hidden]

	return len(settled) + (len(flight) + len(closed) if settled else 0)


def hover_point(page, plot, index=0):
	"""Move onto one marker, through Plotly's drag layer. Scrolls first:
	`bounding_box()` is viewport-relative, and a move off-screen does nothing."""

	page.eval_on_selector(plot, 'n => n.scrollIntoView({block: "center"})')
	page.wait_for_timeout(250)
	handles = page.query_selector_all(
		'{} .scatterlayer .trace path.point'.format(plot))

	if not handles:
		raise AssertionError('{} painted no points to hover'.format(plot))

	box = handles[index].bounding_box()
	page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)

	return box


def drag_panel(page, handle, target, fraction=0.25):
	"""Drag one panel onto another the way a hand does: press, ease off the
	handle, then cross in steps. `page.drag_and_drop` never raises `dragstart`
	from a small handle."""

	page.eval_on_selector(target, 'n => n.scrollIntoView({block: "center"})')
	page.wait_for_timeout(200)
	source = page.query_selector(handle).bounding_box()
	destination = page.query_selector(target).bounding_box()

	page.mouse.move(source['x'] + source['width'] / 2,
		source['y'] + source['height'] / 2)
	page.mouse.down()
	page.mouse.move(source['x'] + 30, source['y'] + 10, steps=5)
	page.mouse.move(destination['x'] + destination['width'] * fraction,
		destination['y'] + destination['height'] / 2, steps=15)
	page.mouse.move(destination['x'] + destination['width'] * (fraction - 0.05),
		destination['y'] + destination['height'] / 2, steps=5)
	page.mouse.up()


def new_page(browser, errors, height=1100):
	page = browser.new_page(viewport={'width': 1500, 'height': height})
	# A fresh browser's first render has taken 26-31 s on a busy 256-core
	# machine, with old and new dashboard.js alike; Playwright's 30 s default
	# then failed checks that had nothing to do with the page.
	page.set_default_timeout(90000)
	page.on('console', lambda m: errors.append(m.text)
		if m.type == 'error' else None)
	page.on('pageerror', lambda e: errors.append(str(e)))

	return page


###
# Publisher freshness
###

def check_reload_chain():
	"""The notebook cell reloads `autoopt_monitor` only; it must refresh what
	it imports, or a kernel left open calls into stale copies."""

	import importlib

	saved = monitor_writer.code_version
	del monitor_writer.code_version

	try:
		check('the stale module state reproduces',
			not hasattr(monitor_writer, 'code_version'))
		importlib.reload(autoopt_monitor)
		check('reloading the notebook module refreshes its imports',
			hasattr(sys.modules['monitor_writer'], 'code_version'))
		check('and the mount can reach them',
			hasattr(autoopt_monitor.monitor_writer, 'code_version'))
	finally:
		if not hasattr(sys.modules['monitor_writer'], 'code_version'):
			sys.modules['monitor_writer'].code_version = saved


def publisher(root, work):
	"""The mount's CLI and lifecycle: slugs, intervals, stop, discovery by
	process, --out, and errors as messages."""

	mount_py = os.path.join(HERE, 'autoopt_monitor.py')
	cli = lambda *args: subprocess.run([sys.executable, mount_py] + list(args),
		capture_output=True, text=True, cwd=work)
	mount = autoopt_monitor

	a, b = (os.path.join(work, p, 'project', 'autooptimize') for p in ('one', 'two'))
	check('two searches whose projects share a name get different slugs',
		mount.run_slug(a) != mount.run_slug(b) and mount.run_slug(a)
		== mount.run_slug(a + '/') and mount.run_slug(a).startswith('project-'),
		(mount.run_slug(a), mount.run_slug(b)))

	shared = tempfile.mkdtemp(prefix='clocks-', dir=work)
	try:
		first, _ = mount.start_writer(root, shared, interval=60, resource_interval=60)
		again, restarted = mount.start_writer(root, shared, interval=30,
			resource_interval=60)
		info = monitor_writer.writer_info(shared)
		check('a start on another interval restarts the writer on it',
			restarted and again != first and info and info['intervals'] == [30, 60],
			(first, again, info))

		nb = mount.make_notebook(root, path=os.path.join(work, 'clocks.ipynb'),
			out_dir=shared, interval=30.0)
		check('the notebook passes the interval to show()',
			'interval=30.0' in open(nb).read())

		stopped = mount.stop_writer(out_dir=shared)
		check('stop waits until the writer is gone', stopped == [again]
			and not mount._alive(again) and not monitor_writer.writer_info(shared),
			stopped)
	finally:
		mount.stop_writer(out_dir=shared)

	# A writer started by hand, publishing outside the base, with no index
	# entry: status() finds it by process, and --stop stops it.
	elsewhere = tempfile.mkdtemp(prefix='by-hand-', dir=work)
	hand = subprocess.Popen([sys.executable, os.path.join(HERE,
		'monitor_writer.py'), root, '--out', elsewhere, '--interval', '60',
		'--resource-interval', '60'], stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL, start_new_session=True)
	try:
		for _ in range(60):
			if monitor_writer.writer_info(elsewhere):
				break
			time.sleep(0.5)
		indexed = os.path.exists(monitor_writer.index_path(elsewhere))
		check('a writer started by hand with --out is in the writer index',
			indexed and elsewhere in monitor_writer.index_entries())
		monitor_writer.forget(elsewhere)
		listed = cli('--status', root).stdout
		check('--status still finds it by process when the index lost it',
			str(hand.pid) in listed and elsewhere in listed, listed)
		stop = cli('--stop', root)
		after = cli('--status', root).stdout
		check('--stop stops it, and --status right after no longer lists it',
			'stopped {}'.format(hand.pid) in stop.stdout
			and str(hand.pid) not in after, (stop.stdout, after))
	finally:
		if hand.poll() is None:
			hand.kill()

	# Outside the served tree needs a directory outside the home directory.
	outside = tempfile.mkdtemp(prefix='outside-root-', dir='/tmp')
	try:
		run = cli(root, '--start', '--out', outside, '--interval', '60',
			'-o', os.path.join(work, 'out.ipynb'))
		check('--out publishes where it says, and --start warns outside the '
			'served root', run.returncode == 0 and 'warning' in run.stdout
			and monitor_writer.writer_info(outside) is not None
			and repr(outside) in open(os.path.join(work, 'out.ipynb')).read()
			and 'interval=60.0' in open(os.path.join(work, 'out.ipynb')).read(),
			(run.returncode, run.stdout[-300:], run.stderr[-300:]))
	finally:
		mount.stop_writer(out_dir=outside)

	wrong = cli(os.path.join(work, 'no-such-run'))
	check('a wrong directory is one line and exit 1, not a traceback',
		wrong.returncode == 1 and wrong.stderr.startswith('error:')
		and 'Traceback' not in wrong.stderr, wrong.stderr[-300:])

	# Without -o the notebook goes beside autooptimize/, given either
	# directory.
	beside = os.path.join(os.path.dirname(root), 'dashboard.ipynb')
	made = [cli(given).stdout.strip().splitlines()[-1]
		for given in (root, os.path.dirname(root))]
	check('the default notebook is dashboard.ipynb beside autooptimize/',
		made == [beside, beside] and repr(root) in open(beside).read()
		and not os.path.exists(os.path.join(root, 'monitor.ipynb')), made)

	# stdout a pipe whose reader is already gone: the write meets it closed,
	# as `| head` leaves a long snapshot.
	reader, writer = os.pipe()
	os.close(reader)
	piped = subprocess.run([sys.executable, os.path.join(HERE, 'scan.py'), root],
		stdout=writer, stderr=subprocess.PIPE, text=True)
	os.close(writer)
	check('scan.py into a closed pipe exits quietly', piped.returncode == 0
		and 'Error' not in piped.stderr, piped.stderr[-300:])


def check_stale_writer(root, work):
	"""A publisher running older code is replaced, not reused."""

	out = os.path.join(work, 'stale-writer')

	try:
		first, started = autoopt_monitor.start_writer(root, out, interval=60)
		check('a writer starts', started and first)
		again, restarted = autoopt_monitor.start_writer(root, out, interval=60)
		check('an up-to-date writer is reused', again == first and not restarted)

		with open(os.path.join(out, 'writer.pid')) as handle:
			pid, watched = handle.readline(), handle.readline()
		with open(os.path.join(out, 'writer.pid'), 'w') as handle:
			handle.write('{}{}1\n'.format(pid, watched))

		third, replaced = autoopt_monitor.start_writer(root, out, interval=60)
		check('a writer running older code is replaced',
			replaced and third != first, (first, third))
		# start_writer returns at the first state.json; resources.json waits
		# on its own thread for a hardware sample, seconds on a busy host.
		for _ in range(60):
			if all(os.path.exists(os.path.join(out, name))
				for name in monitor_writer.PAYLOADS):
				break
			time.sleep(0.5)
		check('the writer published every payload', all(os.path.exists(
			os.path.join(out, name)) for name in monitor_writer.PAYLOADS),
			os.listdir(out))
	finally:
		autoopt_monitor.stop_writer(out_dir=out)


def check_pid_reuse(root, work):
	"""A writer.pid outlives its writer, and the kernel reuses the PID. A
	pidfile naming an unrelated process must not read as a live writer, or
	`--stop` and a handover SIGTERM that process."""

	out = tempfile.mkdtemp(prefix='reused-pid-', dir=work)
	sleeper = subprocess.Popen(['sleep', '300'])
	# Names the script on its command line, but started after the pidfile's
	# recorded start time: a reused PID that happens to look like a writer.
	lookalike = subprocess.Popen([sys.executable, '-c',
		'import time; time.sleep(300)', 'monitor_writer.py'])

	def pidfile(pid, start=None):
		with open(os.path.join(out, 'writer.pid'), 'w') as handle:
			handle.write('{}\n{}\n{}\n60.0 60.0\n{}'.format(pid, root,
				monitor_writer.code_version(), '' if start is None
				else start + '\n'))

	try:
		pidfile(sleeper.pid)
		check('a pidfile naming an unrelated process is no live writer',
			monitor_writer.writer_info(out) is None,
			monitor_writer.writer_info(out))
		autoopt_monitor.stop_writer(out_dir=out)
		time.sleep(0.3)
		check('and stopping that directory leaves the process running',
			sleeper.poll() is None)

		pidfile(sleeper.pid)
		pid, started = autoopt_monitor.start_writer(root, out, interval=60,
			resource_interval=60)
		check('a start over it leaves the process running too',
			started and sleeper.poll() is None, (pid, started, sleeper.poll()))
		autoopt_monitor.stop_writer(out_dir=out)

		pidfile(lookalike.pid, start='1')
		check('a process named like a writer, started after the recorded '
			'start time, is no live writer either',
			monitor_writer.writer_info(out) is None,
			monitor_writer.writer_info(out))
	finally:
		autoopt_monitor.stop_writer(out_dir=out)
		for process in (sleeper, lookalike):
			process.kill()


def check_slow_hardware(root, work):
	"""On a busy 8-GPU host one nvidia-smi query takes seconds. A stand-in
	that sleeps 8 s per call must not delay the first state.json, a stop or
	a handover, and the writer publishes the cadence it actually keeps."""

	bin_dir = os.path.join(work, 'slow-nvidia-smi')
	os.makedirs(bin_dir, exist_ok=True)
	with open(os.path.join(bin_dir, 'nvidia-smi'), 'w') as handle:
		handle.write('#!/bin/sh\nsleep 8\n')
	os.chmod(os.path.join(bin_dir, 'nvidia-smi'), 0o755)

	saved = os.environ['PATH']
	os.environ['PATH'] = bin_dir + os.pathsep + saved
	os.environ['GPU_SCAN_NVML'] = os.path.join(work, 'missing-libnvidia-ml.so')
	out = tempfile.mkdtemp(prefix='slow-hardware-', dir=work)

	try:
		started = time.time()
		first, _ = autoopt_monitor.start_writer(root, out, interval=2,
			resource_interval=1)
		took = time.time() - started
		# That scans do not wait on samples is the state cadence, below.
		present = [name for name in ('state.json', 'resources.json')
			if os.path.exists(os.path.join(out, name))]
		check('start returns once both payloads exist, within one sample',
			len(present) == 2 and took < 14, (present, '{:.1f} s'.format(took)))

		time.sleep(3)
		started = time.time()
		second, restarted = autoopt_monitor.start_writer(root, out, interval=3,
			resource_interval=1)
		took = time.time() - started
		check('a handover mid-sample replaces the writer within one sample',
			restarted and second != first and not autoopt_monitor._alive(first)
			and took < 14, (first, second, '{:.1f} s'.format(took)))

		# Long enough for two slow samples, so the cadence is a measured gap.
		time.sleep(18)
		resources = json.load(open(os.path.join(out, 'resources.json')))
		writer = resources.get('writer') or {}
		check('resources.json publishes the cadence the writer keeps',
			(writer.get('resource_cadence') or 0) >= 7.5
			and 3 <= (writer.get('state_cadence') or 0) < 6, writer)

		started = time.time()
		stopped = autoopt_monitor.stop_writer(out_dir=out)
		took = time.time() - started
		check('stop mid-sample returns with the writer gone',
			stopped == [second] and not autoopt_monitor._alive(second)
			and not monitor_writer.writer_info(out) and took < 3,
			(stopped, '{:.1f} s'.format(took)))
	finally:
		os.environ['PATH'] = saved
		os.environ.pop('GPU_SCAN_NVML', None)
		autoopt_monitor.stop_writer(out_dir=out)


def check_one_writer(root, work):
	"""One writer per payload directory. A second one, started beside a
	writer whose pidfile was lost, published alongside it for days: twelve
	searches once had 39, racing each other over every payload and keeping
	forty nvidia-smi calls in flight."""

	out = tempfile.mkdtemp(prefix='one-writer-', dir=work)
	script = os.path.join(HERE, 'monitor_writer.py')
	into = lambda: sorted(autoopt_monitor.writers_into(out))

	try:
		first, _ = autoopt_monitor.start_writer(root, out, interval=60,
			resource_interval=60)
		os.unlink(os.path.join(out, 'writer.pid'))
		# One that does not refuse runs until the timeout kills it.
		try:
			second = subprocess.run([sys.executable, script, root, '--out', out,
				'--interval', '60', '--resource-interval', '60'],
				capture_output=True, text=True, timeout=30)
			refused = second.returncode != 0 and 'already publishing' in second.stderr
		except subprocess.TimeoutExpired:
			refused = False
		check('a second writer into a directory refuses, pidfile or not',
			refused and into() == [first], into())

		third, started = autoopt_monitor.start_writer(root, out, interval=60,
			resource_interval=60)
		check('a start stops a writer its pidfile lost, then starts one',
			started and not autoopt_monitor._alive(first) and into() == [third],
			(first, third, into()))
	finally:
		autoopt_monitor.stop_writer(out_dir=out)

	fresh = tempfile.mkdtemp(prefix='racing-starts-', dir=work)
	results = []

	def start():
		try:
			results.append(autoopt_monitor.start_writer(root, fresh, interval=60,
				resource_interval=60)[0])
		except Exception as error:
			results.append(repr(error))

	try:
		threads = [threading.Thread(target=start) for _ in range(2)]
		for thread in threads:
			thread.start()
		for thread in threads:
			thread.join()
		check('two starts at once leave one writer, and both return it',
			len(set(results)) == 1 and autoopt_monitor.writers_into(fresh)
			== [results[0]], (results, autoopt_monitor.writers_into(fresh)))
	finally:
		autoopt_monitor.stop_writer(out_dir=fresh)

	# Two writers into one file: each write must land whole.
	path = os.path.join(work, 'racing-payload.json')
	payload = {'rows': list(range(20000))}
	bad, going = [], {'on': True}

	def write():
		while going['on']:
			try:
				monitor_writer.write_json(path, payload)
			except OSError as error:
				bad.append(repr(error))

	monitor_writer.write_json(path, payload)
	threads = [threading.Thread(target=write) for _ in range(2)]
	for thread in threads:
		thread.start()
	for _ in range(300):
		try:
			with open(path) as infile:
				json.load(infile)
		except ValueError as error:
			bad.append(repr(error))
	going['on'] = False
	for thread in threads:
		thread.join()
	check('two writers into one payload never leave it half-written',
		not bad, bad[:3])

	# A writer that is stopped late must leave its successor's pidfile.
	handover = tempfile.mkdtemp(prefix='late-stop-', dir=work)
	try:
		old, _ = autoopt_monitor.start_writer(root, handover, interval=60,
			resource_interval=60)
		pidfile = os.path.join(handover, 'writer.pid')
		lines = open(pidfile).read().splitlines()
		with open(pidfile, 'w') as handle:
			handle.write('\n'.join([str(os.getpid())] + lines[1:]) + '\n')
		os.kill(old, 15)
		for _ in range(40):
			if not autoopt_monitor._alive(old):
				break
			time.sleep(0.25)
		check('a writer stopping leaves a pidfile that names another process',
			not autoopt_monitor._alive(old) and os.path.exists(pidfile)
			and open(pidfile).readline().strip() == str(os.getpid()))
	finally:
		autoopt_monitor.stop_writer(out_dir=handover)


def check_shared_sample(work):
	"""Every publisher on the host asks nvidia-smi the same questions. Six
	publishers polling together make one call per query per freshness window,
	and a call that fails keeps the last good GPU rows instead of none."""

	bin_dir = os.path.join(work, 'counting-nvidia-smi')
	log = os.path.join(work, 'nvidia-smi-calls.log')
	os.makedirs(bin_dir, exist_ok=True)
	with open(os.path.join(bin_dir, 'nvidia-smi'), 'w') as handle:
		handle.write('#!/bin/sh\necho "$*" >> {}\nsleep 1\n'
			'[ -e {}.fail ] && exit 1\n'
			'case "$*" in\n'
			'  *query-gpu*) echo "0, GPU-a, Stub, 37, 10, 1000, 40960, 50, 100, 400";;\n'
			'esac\n'.format(log, log))
	os.chmod(os.path.join(bin_dir, 'nvidia-smi'), 0o755)

	probe = ('import sys, json; sys.path.insert(0, {!r}); import gpu_scan; '
		'h = gpu_scan.Hardware({!r}); r = [h.resources() for _ in range(3)]; '
		'print(json.dumps([[len(x["gpus"]), x.get("gpu_sampled_at")] for x in r]))'
		.format(HERE, work))
	# A library that will not load puts the scanner on nvidia-smi: the stub.
	env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ['PATH'],
		GPU_SCAN_NVML=os.path.join(work, 'missing-libnvidia-ml.so'))

	try:
		procs = [subprocess.Popen([sys.executable, '-c', probe], env=env,
			stdout=subprocess.PIPE, text=True) for _ in range(6)]
		outputs = [json.loads(p.communicate(timeout=120)[0]) for p in procs]
		calls = open(log).read().splitlines()
		check('six publishers share nvidia-smi: one call per query, not six',
			len(calls) <= 6 and all(n == 1 for out in outputs for n, _ in out),
			(len(calls), outputs))

		open(log + '.fail', 'w').close()
		time.sleep(gpu_scan.SHARED_SECONDS + 0.5)
		failing = json.loads(subprocess.run([sys.executable, '-c', probe],
			env=env, capture_output=True, text=True, timeout=120).stdout)
		check('a failing nvidia-smi keeps the last GPU rows, marked by age',
			all(n == 1 for n, _ in failing)
			and failing[-1][1] == max(at for out in outputs for _, at in out),
			(failing, outputs))

		# A new nvidia-smi process stalls cudaMalloc in every CUDA process on
		# the host, so where NVML loads a publisher must not start one.
		if gpu_scan.nvml() is None:
			print('  (NVML does not load here: the NVML check is skipped)')
		else:
			os.unlink(log + '.fail')
			open(log, 'w').close()
			time.sleep(gpu_scan.SHARED_SECONDS + 0.5)
			loaded = dict(env)
			del loaded['GPU_SCAN_NVML']
			read = json.loads(subprocess.run([sys.executable, '-c', probe],
				env=loaded, capture_output=True, text=True, timeout=120).stdout)
			check('with NVML loaded a publisher reads every GPU and calls no '
				'nvidia-smi', not open(log).read() and all(n == len(
				gpu_scan.nvml().gpus()) for n, _ in read), (read, open(log).read()))
	finally:
		key = hashlib.sha1(os.path.join(bin_dir, 'nvidia-smi').encode()
			).hexdigest()[:8]
		for name in os.listdir(gpu_scan.shared_dir()):
			if key in name:
				os.unlink(os.path.join(gpu_scan.shared_dir(), name))

	# The answer's date is when its call started: dated when it finished, a
	# call over 0.5 s left it fresh at the next 3 s tick, so it moved every
	# 6 s. A publisher that finds another's call under way waits for it.
	keys = ['test-dating-{}'.format(os.getpid()), 'test-waiting-{}'.format(
		os.getpid())]

	def slow(value, seconds=1.0):
		def compute():
			time.sleep(seconds)
			return value
		return compute

	try:
		began = time.time()
		_, at = gpu_scan.shared_sample(keys[0], 2.5, slow('a'))
		check('a shared answer is dated when its call started',
			at - began < 0.2, round(at - began, 2))

		gpu_scan.shared_sample(keys[1], 0.5, lambda: 'old')
		time.sleep(0.6)
		landed = {}
		caller = threading.Thread(target=lambda: landed.update(
			first=gpu_scan.shared_sample(keys[1], 0.5, slow('new', 1.0))))
		caller.start()
		time.sleep(0.2)
		waited = gpu_scan.shared_sample(keys[1], 0.5, lambda: 'third call')
		caller.join()
		check('a publisher waits for the call under way and takes its answer, '
			'even one older than the window', waited == landed.get('first')
			and waited[0] == 'new', (waited, landed))
	finally:
		for name in os.listdir(gpu_scan.shared_dir()):
			if any(key in name for key in keys):
				os.unlink(os.path.join(gpu_scan.shared_dir(), name))


def check_live_samples(root, work):
	"""A page can ask its publisher for live GPU cards. NVML answers in about
	a tenth of a millisecond, so a live sample skips the shared cache there;
	where only nvidia-smi exists the cache stays, because each call costs
	every CUDA job on the host. A request lapses by itself."""

	out = tempfile.mkdtemp(prefix='live-', dir=work)
	live = os.path.join(out, monitor_writer.LIVE)
	check('no live file is no request', not monitor_writer.live_requested(out))
	open(live, 'w').close()
	check('a fresh live file is a request', monitor_writer.live_requested(out))
	os.utime(live, (time.time() - 30, time.time() - 30))
	check('a live file past its window is not',
		not monitor_writer.live_requested(out))
	os.unlink(live)

	bin_dir = os.path.join(work, 'live-nvidia-smi')
	log = os.path.join(work, 'live-nvidia-smi.log')
	os.makedirs(bin_dir, exist_ok=True)
	with open(os.path.join(bin_dir, 'nvidia-smi'), 'w') as handle:
		handle.write('#!/bin/sh\necho "$*" >> {}\n'
			'case "$*" in\n'
			'  *query-gpu*) echo "0, GPU-a, Stub, 37, 10, 1000, 40960, 50, 100, 400";;\n'
			'esac\n'.format(log))
	os.chmod(os.path.join(bin_dir, 'nvidia-smi'), 0o755)

	probe = ('import sys, json; sys.path.insert(0, {!r}); import gpu_scan; '
		'h = gpu_scan.Hardware({!r}); h.live = {}; '
		'r = [h.resources() for _ in range(3)]; '
		'print(json.dumps([x["gpu_sampled_at"] for x in r]))'.format(HERE, work, '{}'))
	env = dict(os.environ, PATH=bin_dir + os.pathsep + os.environ['PATH'])

	def sampled(live, nvml):
		environ = dict(env)
		if not nvml:
			environ['GPU_SCAN_NVML'] = os.path.join(work, 'missing-libnvidia-ml.so')
		else:
			environ.pop('GPU_SCAN_NVML', None)
		open(log, 'w').close()
		time.sleep(gpu_scan.SHARED_SECONDS + 0.5)
		result = subprocess.run([sys.executable, '-c', probe.format(live)],
			env=environ, capture_output=True, text=True, timeout=120)
		return json.loads(result.stdout), open(log).read().splitlines()

	times, calls = sampled(True, nvml=False)
	check('live without NVML still shares one nvidia-smi answer',
		len(set(times)) == 1 and len(calls) <= 3, (times, len(calls)))

	if gpu_scan.nvml() is None:
		print('  (NVML does not load here: the live NVML checks are skipped)')
	else:
		times, calls = sampled(False, nvml=True)
		check('without a request three polls share one sample',
			len(set(times)) == 1, times)
		times, calls = sampled(True, nvml=True)
		check('with one every poll is a new NVML sample, and no nvidia-smi',
			len(set(times)) == 3 and not calls, (times, calls))

	out = tempfile.mkdtemp(prefix='live-writer-', dir=work)
	path = os.path.join(out, 'resources.json')

	def changes(seconds):
		seen, last = [], None
		end = time.time() + seconds

		while time.time() < end:
			try:
				payload = json.load(open(path))
			except (OSError, ValueError):
				payload = {}
			if payload.get('timestamp') != last:
				last = payload.get('timestamp')
				seen.append(payload)
			time.sleep(0.1)

		return seen

	def touch():
		with open(os.path.join(out, monitor_writer.LIVE), 'w') as handle:
			handle.write('{}')

	try:
		autoopt_monitor.start_writer(root, out, interval=60, resource_interval=3)
		slow = changes(7)
		check('with no request the writer keeps its own clock',
			len(slow) <= 4 and not slow[-1]['writer']['live'], len(slow))

		stop = threading.Event()
		beat = threading.Thread(target=lambda: [touch() or stop.wait(2)
			for _ in iter(stop.is_set, True)], daemon=True)
		beat.start()
		time.sleep(2)
		before = len(slow[-1]['history'])
		fast = changes(6)
		stop.set()
		beat.join()
		check('with a request it samples about every second',
			len(fast) >= 5 and fast[-1]['writer']['live'], len(fast))
		grown = len(fast[-1]['history']) - before
		check('but the sparkline history keeps the slow clock',
			0 <= grown <= 3, grown)

		lapsed = changes(live_window() + 7)
		check('and a request that stops lapses back to the slow clock',
			not lapsed[-1]['writer']['live'] and len(changes(7)) <= 4,
			lapsed[-1]['writer'])
	finally:
		autoopt_monitor.stop_writer(out_dir=out)


def live_window():
	return monitor_writer.LIVE_SECONDS


def check_live_button(root, work, args=None):
	"""The GPU strip's Live toggle: off by default, asks the publisher by
	writing live.json while on, polls at its own clock, and says so when the
	write fails instead of looking live."""

	from playwright.sync_api import sync_playwright

	out = os.path.join(work, 'live-button')
	publish(root, out)
	variant(out, 'slow.html', lambda page: page.replace('data-standalone',
		'data-standalone data-resource-interval="30000"'))
	server = serve(out, accept_put=True)
	refusing = serve(out)
	errors = []

	print('\nthe live toggle')

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch(headless=not (args and args.headed))
			page = new_page(browser, errors)
			polls = []
			page.on('request', lambda r: polls.append(time.time())
				if 'resources.json' in r.url else None)
			page.goto(server[1] + 'slow.html')
			page.wait_for_selector('.gpu-card')
			button = page.locator('#toggle-live')
			check('the toggle is shown and off by default',
				button.is_visible() and button.get_attribute('aria-pressed')
				== 'false' and not os.path.exists(os.path.join(out, 'live.json')))

			page.wait_for_timeout(2500)
			quiet = len(polls)
			button.click()
			page.wait_for_timeout(5500)
			check('on, the label says it is waiting for the publisher (none here)',
				button.inner_text() == 'Live · waiting', button.inner_text())
			check('on, the page writes live.json and polls about every second',
				os.path.exists(os.path.join(out, 'live.json'))
				and len(polls) - quiet >= 4, (quiet, len(polls)))

			stamp = os.path.getmtime(os.path.join(out, 'live.json'))
			page.wait_for_timeout(3500)
			check('it keeps renewing the request',
				os.path.getmtime(os.path.join(out, 'live.json')) > stamp)

			button.click()
			page.wait_for_timeout(500)
			stamp = os.path.getmtime(os.path.join(out, 'live.json'))
			seen = len(polls)
			page.wait_for_timeout(4500)
			check('off, it stops renewing and stops polling fast',
				button.get_attribute('aria-pressed') == 'false'
				and os.path.getmtime(os.path.join(out, 'live.json')) == stamp
				and len(polls) - seen <= 1, (len(polls) - seen,))

			page.goto(refusing[1] + 'slow.html')
			page.wait_for_selector('.gpu-card')
			page.locator('#toggle-live').click()
			page.wait_for_timeout(1500)
			check('a refused write turns the toggle back off and says why',
				page.locator('#toggle-live').get_attribute('aria-pressed') == 'false'
				and 'unavailable' in (page.locator('#toggle-live')
				.get_attribute('title') or '')
				and page.locator('#toggle-live').inner_text() == 'Live unavailable')
			browser.close()
	finally:
		server[0].shutdown()
		refusing[0].shutdown()

	# The refusing server's 501 is the browser logging the failure the page
	# is meant to handle.
	errors = [e for e in errors if '501' not in e]
	check('no console errors from the live toggle', not errors, errors[:3])


def check_other_host(root, work):
	"""Two hosts mounting one payload directory each ran a writer, and the
	page flipped every write between their GPUs: the lock and the pid checks
	are per host. A writer pauses while another host with the better claim
	publishes, and takes over once that host goes quiet."""

	now = time.time()
	mine = {'host': 'b', 'since': now - 100, 'search': False}

	def beat(**fields):
		return dict({'host': 'a', 'since': now - 50, 'search': False,
			'at': now - 1}, **fields)

	for other, sees, want, why in (
			(None, False, False, 'with no other writer'),
			(beat(host='b'), False, False, 'beside a heartbeat from its own host'),
			(beat(search=True, at=now - 30), False, False,
				'beside a heartbeat gone stale'),
			(beat(), False, True, 'beside another host publishing'),
			(beat(search=True), False, True,
				'beside another host that sees the search'),
			(beat(search=True), True, True,
				'beside another host that sees it too'),
			(beat(), True, False, 'when it sees the search and the other host '
				'does not')):
		check('a writer {} {}'.format('pauses' if want else 'publishes', why),
			monitor_writer.yields_to(dict(mine, search=sees), other, now, 10.0)
				== want, (other, sees))

	out = tempfile.mkdtemp(prefix='other-host-', dir=work)
	heartbeat = os.path.join(out, monitor_writer.HEARTBEAT)
	resources = os.path.join(out, 'resources.json')
	beating = {'on': True}

	def other_host():
		while beating['on']:
			monitor_writer.write_json(heartbeat, {'host': 'elsewhere',
				'pid': 1, 'since': 0, 'search': True, 'at': time.time()})
			time.sleep(0.5)

	thread = threading.Thread(target=other_host, daemon=True)
	thread.start()
	log = open(os.path.join(out, 'writer.log'), 'w')
	writer = subprocess.Popen([sys.executable, os.path.join(HERE,
		'monitor_writer.py'), root, '--out', out, '--interval', '1',
		'--resource-interval', '1'], stdout=log, stderr=log,
		start_new_session=True)

	try:
		time.sleep(5)
		check('a writer pauses while another host that sees the search '
			'publishes: no resources.json from it, and its log says so',
			not os.path.exists(resources) and 'paused: elsewhere'
			in open(os.path.join(out, 'writer.log')).read(),
			open(os.path.join(out, 'writer.log')).read()[-400:])

		beating['on'] = False
		thread.join()
		deadline = time.time() + 30
		while time.time() < deadline and not os.path.exists(resources):
			time.sleep(0.5)
		published = json.load(open(resources)) if os.path.exists(resources) else {}
		check('and takes over once that host goes quiet, naming its own host',
			published.get('hostname') == socket.gethostname()
			and monitor_writer.read_heartbeat(out).get('host')
				== monitor_writer.HOST, (published.get('hostname'),
					monitor_writer.read_heartbeat(out)))
	finally:
		beating['on'] = False
		writer.terminate()
		writer.wait(timeout=30)


def check_bad_config(root, work):
	"""A config.json that fails validation raises SystemExit inside the scan
	(autoopt.fail). The writer must log it, publish it, keep running and
	recover when the file is fixed."""

	import shutil

	copy = os.path.join(work, 'bad-config', 'autooptimize')
	shutil.copytree(root, copy, symlinks=True)
	path = os.path.join(copy, 'config.json')
	config = json.load(open(path))
	direction = config['objective']['direction']
	config['objective']['direction'] = 'sideways'
	json.dump(config, open(path, 'w'), indent='\t')

	out = tempfile.mkdtemp(prefix='bad-config-', dir=work)
	log = open(os.path.join(out, 'writer.log'), 'w')
	writer = subprocess.Popen([sys.executable, os.path.join(HERE,
		'monitor_writer.py'), copy, '--out', out, '--interval', '1',
		'--resource-interval', '1'], stdout=log, stderr=log,
		start_new_session=True)

	def resources():
		try:
			return json.load(open(os.path.join(out, 'resources.json')))
		except (OSError, ValueError):
			return {}

	# resources.json carries the writer's status, one hardware sample late:
	# seconds on a busy host, so each check waits for its condition.
	def until(condition, seconds=30):
		deadline = time.time() + seconds
		while time.time() < deadline and not condition():
			time.sleep(0.5)

	scan_error = lambda: (resources().get('writer') or {}).get('scan_error') \
		or ''

	try:
		until(lambda: scan_error())
		text = open(os.path.join(out, 'writer.log')).read()
		check('a config failing validation does not kill the writer',
			writer.poll() is None and monitor_writer.writer_info(out),
			text[-400:])
		check('the reason is in its log', 'direction' in text, text[-400:])
		error = scan_error()
		check('and published for the page', 'direction' in error, error)

		config['objective']['direction'] = direction
		json.dump(config, open(path, 'w'), indent='\t')
		until(lambda: not scan_error())
		check('fixing the file recovers the scan', os.path.exists(
			os.path.join(out, 'state.json')) and not (resources().get('writer')
			or {}).get('scan_error'), resources().get('writer'))

		# autoopt.load_state exits on a corrupt state.json, naming the file.
		state_path = os.path.join(copy, 'state.json')
		good = open(state_path).read()
		with open(state_path, 'w') as handle:
			handle.write(good[:len(good) // 2])
		until(lambda: 'state.json' in scan_error())
		error = scan_error()
		check('a corrupt state.json is a published error, not a dead writer',
			writer.poll() is None and 'state.json' in error, error)
		with open(state_path, 'w') as handle:
			handle.write(good)
		until(lambda: not scan_error())
		check('and restoring it recovers the scan', not (resources().get(
			'writer') or {}).get('scan_error'), resources().get('writer'))
	finally:
		writer.terminate()
		try:
			writer.wait(10)
		except subprocess.TimeoutExpired:
			writer.kill()
		log.close()

	check('a stopped writer removes its pidfile',
		not os.path.exists(os.path.join(out, 'writer.pid')))


def unreadable_copy(root, work):
	"""A copy of the search with one malformed result.json: `runs` a string."""

	import shutil

	copy = os.path.join(work, 'unreadable', 'autooptimize')
	if not os.path.exists(copy):
		shutil.copytree(root, copy, symlinks=True)
		path = os.path.join(copy, 'iterations', '5', 'result.json')
		result = json.load(open(path))
		result['runs'] = 'x'
		json.dump(result, open(path, 'w'))

	return copy


def check_unreadable(root, work):
	"""One malformed result.json must cost one row, not the whole scan."""

	copy = unreadable_copy(root, work)
	try:
		snapshot = scan.RunScanner(copy).snapshot()
	except Exception as error:
		check('a malformed result.json does not fail the scan', False,
			repr(error)[:300])
		return

	whole = scan.RunScanner(root).snapshot()
	rows = {row['id']: row for row in snapshot['iterations']}
	check('a malformed result.json does not fail the scan',
		len(rows) == len(whole['iterations']))
	check('its iteration is an unreadable row naming the error',
		rows[5]['status'] == 'unreadable' and rows[5].get('error'), rows[5])
	equal('and the snapshot lists it', [entry['id'] for entry in
		snapshot.get('unreadable') or []], [5])


def check_portability(root, work):
	"""What a machine without /proc, without psutil or plotly, with another
	Jupyter runtime directory or a malformed lease file must survive."""

	import gpu_scan

	real_listdir = os.listdir

	def no_proc(path='.'):
		if str(path).rstrip('/') == '/proc':
			raise FileNotFoundError(path)
		return real_listdir(path)

	os.listdir = no_proc
	try:
		found = autoopt_monitor.writer_processes()
		check('writer discovery without /proc returns, not raises',
			isinstance(found, list))
	except Exception as error:
		check('writer discovery without /proc returns, not raises', False,
			repr(error))
	finally:
		os.listdir = real_listdir

	# jupyter_core warns about platformdirs when its paths module is first
	# imported. A fresh process, as each notebook kernel is, must not show
	# it, and must look in the runtime directory the servers themselves use.
	probe = ('import os, sys, json, warnings; warnings.simplefilter("always"); '
		'sys.path.insert(0, {!r}); import autoopt_monitor; '
		'autoopt_monitor.jupyter_servers(); '
		'import jupyter_core.paths as p; '
		'print(json.dumps([p.jupyter_runtime_dir(), '
		'os.environ.get("JUPYTER_PLATFORM_DIRS")]))').format(HERE)
	env = {key: value for key, value in os.environ.items()
		if key not in ('JUPYTER_PLATFORM_DIRS', 'JUPYTER_RUNTIME_DIR')}
	done = subprocess.run([sys.executable, '-c', probe], capture_output=True,
		text=True, env=env)
	plain = subprocess.run([sys.executable, '-W', 'ignore', '-c',
		'import jupyter_core.paths as p; print(p.jupyter_runtime_dir())'],
		capture_output=True, text=True, env=env)
	check('finding Jupyter servers prints no jupyter_core platformdirs '
		'warning', done.returncode == 0 and 'platformdirs' not in done.stderr,
		done.stderr[-400:])
	check('and leaves the runtime directory as jupyter_core finds it unaided',
		done.returncode == 0 and json.loads(done.stdout.splitlines()[-1])
		== [plain.stdout.strip(), None], done.stdout[-300:])

	# A runtime directory only jupyter_core knows about, as on macOS.
	runtime = tempfile.mkdtemp(prefix='runtime-', dir=work)
	served = tempfile.mkdtemp(prefix='served-', dir=work)
	with open(os.path.join(runtime, 'jpserver-1.json'), 'w') as out:
		json.dump({'root_dir': served, 'base_url': '/lab-base/'}, out)
	saved_env = os.environ.pop('JUPYTER_RUNTIME_DIR', None)
	try:
		import jupyter_core.paths
		saved = jupyter_core.paths.jupyter_runtime_dir
		jupyter_core.paths.jupyter_runtime_dir = lambda: runtime
		try:
			servers = autoopt_monitor.jupyter_servers()
		finally:
			jupyter_core.paths.jupyter_runtime_dir = saved
		check('Jupyter servers are found in jupyter_core\'s runtime directory',
			(served, '/lab-base/') in servers, servers)
	except ImportError:
		check('jupyter_core is installed for this check', False)
	finally:
		if saved_env is not None:
			os.environ['JUPYTER_RUNTIME_DIR'] = saved_env

	leases = tempfile.mkdtemp(prefix='bad-leases-', dir=work)
	for name, payload in (('gpu0.json', [1, 2]), ('gpu1.json', {'pid': 'x'}),
		('gpu2.json', {'expires_at': 'soon'}), ('gpu4.json', {'gpu': 'four'}),
		('gpu5.json', {'user': 'someone', 'pid': os.getpid()})):
		with open(os.path.join(leases, name), 'w') as out:
			json.dump(payload, out)
	try:
		held = gpu_scan.leases(leases)
		equal('malformed lease files are skipped, the good one kept',
			sorted(held), [5])
	except Exception as error:
		check('malformed lease files are skipped, the good one kept', False,
			repr(error))

	saved_plotly = sys.modules.get('plotly')
	sys.modules['plotly'] = None
	try:
		autoopt_monitor.export(root, os.path.join(work, 'no-plotly.html'))
		check('export without plotly says what to install', False,
			'no error raised')
	except ImportError as error:
		check('export without plotly says what to install',
			'pip install plotly' in str(error), str(error))
	finally:
		if saved_plotly is None:
			del sys.modules['plotly']
		else:
			sys.modules['plotly'] = saved_plotly
	cli = subprocess.run([sys.executable, '-c', 'import sys, runpy; '
		'sys.modules["plotly"] = None; sys.argv = ["autoopt_monitor.py", '
		'{!r}, "--export", {!r}]; runpy.run_path({!r}, run_name="__main__")'
		.format(root, os.path.join(work, 'no-plotly.html'), os.path.join(HERE,
		'autoopt_monitor.py'))], capture_output=True, text=True)
	check('and --export says it in one line, exit 1', cli.returncode == 1
		and cli.stderr.startswith('error:') and 'Traceback' not in cli.stderr,
		cli.stderr[-300:])

	saved_psutil = gpu_scan.psutil
	gpu_scan.psutil = None
	try:
		resources = gpu_scan.Hardware(root).resources()
	finally:
		gpu_scan.psutil = saved_psutil
	check('resources.json says when psutil is missing',
		resources.get('psutil') is False, resources.get('psutil'))


def readmeless_copy(root, work):
	"""A copy of the search whose iteration 3 has no README.md (moved aside)."""

	import shutil

	copy = os.path.join(work, 'readmeless', 'autooptimize')
	if not os.path.exists(copy):
		shutil.copytree(root, copy, symlinks=True)
		readme = os.path.join(copy, 'iterations', '3', 'README.md')
		os.replace(readme, readme + '.aside')

	return copy


def check_readme_handover(root, work, args=None):
	"""A handover to another search must not leave the old search's whole
	READMEs behind, and the page must not fetch one for an iteration whose
	row says it has none."""

	from playwright.sync_api import sync_playwright

	copy = readmeless_copy(root, work)
	out = tempfile.mkdtemp(prefix='readme-handover-', dir=work)
	stale = os.path.join(out, 'readmes', '3.json')

	try:
		autoopt_monitor.start_writer(root, out, interval=60,
			resource_interval=60)
		for _ in range(60):
			if os.path.exists(stale):
				break
			time.sleep(0.5)
		check('the first search publishes iteration 3\'s README',
			os.path.exists(stale))
		autoopt_monitor.start_writer(copy, out, interval=60,
			resource_interval=60)
		time.sleep(1)
		check('a handover leaves no README of the old search',
			not os.path.exists(stale), os.listdir(os.path.join(out, 'readmes')))
		check('it moves them aside rather than deleting them', any(
			name.startswith('readmes.') and os.path.exists(os.path.join(out,
			name, '3.json')) for name in os.listdir(out)), os.listdir(out))
	finally:
		autoopt_monitor.stop_writer(out_dir=out)

	# The page, over a payload holding a stale readmes/3.json anyway.
	published = os.path.join(work, 'readmeless-published')
	publish(copy, published)
	monitor_writer.write_json(os.path.join(published, 'readmes', '3.json'),
		{'id': 3, 'text': 'stale-readme-9d1e'})
	server, base = serve(published)
	errors, fetched = [], []

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch(headless=not (args and args.headed))
			page = new_page(browser, errors)
			page.on('request', lambda request: fetched.append(request.url))
			page.goto(base + 'index.html')
			page.wait_for_selector('#tree-holder .lineage-node[data-node-id="3"]')
			page.click('#tree-holder .lineage-node[data-node-id="3"]')
			page.wait_for_timeout(1000)
			check('a node whose row has no README fetches none',
				not any('readmes/3.json' in url for url in fetched)
				and 'stale-readme-9d1e' not in page.inner_text('#tree-node'),
				[url for url in fetched if 'readmes' in url])
			check('no console errors on the README-less node', not errors,
				errors[:3])
			browser.close()
	finally:
		server.shutdown()


def ended_copy(root, work):
	"""A copy of the search as it stands after the ending: ended.json and the
	paired timing. Iteration 3 answered `specific` to the toolchain
	question, iteration 2 `no`."""

	import shutil

	copy = os.path.join(work, 'ended', 'autooptimize')

	if os.path.exists(copy):
		return copy

	shutil.copytree(root, copy, symlinks=True)

	def put(path, content):
		path = os.path.join(copy, path)
		with open(path, 'w') as out:
			json.dump(content, out)

	put('ended.json', {'ended_at': '2026-11-05T10:00:00',
		'reason': {'criterion': 'stalled', 'text': 'stalled-reason-3a'},
		'winner': {'id': 3, 'label': 'A3'}, 'medians': {'A0': 12.5,
		'winner': 4.25, 'objective': 'throughput', 'unit': 's'},
		'counts': {'rounds': 2, 'iterations': 11, 'winning_iterations': 3}})
	put('final.json', {'summary': 'paired-9of9'})

	for number, answer in ((3, 'specific'), (2, 'no')):
		path = os.path.join(copy, 'iterations', str(number), 'meta.json')
		meta = json.load(open(path))
		meta['toolchain'] = answer
		json.dump(meta, open(path, 'w'))

	return copy


def check_after_payload(root, work):
	"""The scan reads what the search wrote about its ending."""

	copy = ended_copy(root, work)
	snapshot = scan.RunScanner(copy).snapshot()
	after = snapshot['after']

	check('the scan reads the ending and the stages', not after.get('errors'),
		after.get('errors'))
	equal('the snapshot key holds the ending, the stages and nothing else',
		sorted(after), ['ended', 'stages'])
	equal('the ending', (after['ended']['winner'], after['ended']['zero_median'],
		after['ended']['reason']), ('A3', 12.5, 'stalled-reason-3a'))
	equal('the five stages', [name for name, _, _ in after['stages']],
		['interview', 'setup', 'search', 'final', 'finish'])
	by_id = {row['id']: row for row in snapshot['iterations']}
	equal('the toolchain answers', (by_id[3]['toolchain'], by_id[2]['toolchain']),
		('specific', 'no'))
	plain = scan.RunScanner(root).snapshot()['after']
	check('a search that has not ended has no ending', plain['ended'] is None
		and len(plain['stages']) == 5, plain)

	# An ending the scan cannot read blanks the parts that read it, names
	# them, and the snapshot is still built.
	broken = os.path.join(work, 'ended-broken', 'autooptimize')

	if not os.path.exists(broken):
		import shutil
		shutil.copytree(copy, broken, symlinks=True)
		with open(os.path.join(broken, 'ended.json'), 'w') as out:
			json.dump({'ended_at': '2026-11-05', 'medians': [1]}, out)

	snapshot = scan.RunScanner(broken).snapshot()
	after = snapshot['after']
	check('an ending with no medians record blanks the ending and names it',
		after['ended'] is None and 'AttributeError' in (after.get('errors')
		or {}).get('ended', ''), after)
	check('and the iterations are still scanned', len(snapshot['iterations'])
		== len(by_id), len(snapshot['iterations']))


def check_after_page(root, work, args=None):
	"""What the page draws from it: the topbar line, the stage strip and the
	toolchain row of an open log entry."""

	from playwright.sync_api import sync_playwright

	copy = ended_copy(root, work)
	published = os.path.join(work, 'ended-published')
	publish(copy, published)
	server, base = serve(published)
	errors = []

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch(headless=not (args and args.headed))
			page = new_page(browser, errors)
			page.goto(base + 'index.html')
			page.wait_for_selector('#ended-line:not([hidden])')
			page.wait_for_timeout(700)

			line = page.inner_text('#ended-line')
			check('the topbar says when it ended, the winner against A0 and why',
				line.startswith('ended 2026-11-05 · A3 4.25 s vs A0 12.5 s · ')
				and 'stalled-reason-3a' in line, line)
			check('its tooltip carries the paired timing',
				'paired-9of9' in (page.get_attribute('#ended-line', 'title') or ''))
			strip = page.inner_text('#stage-strip')
			words = [part.rsplit(' ', 1) for part in strip.split(' · ')]
			equal('five stages, in order', [name for name, _ in words],
				['interview', 'setup', 'search', 'final', 'finish'])
			check('each with one status word', all(word in ('done', 'running',
				'pending') for _, word in words) and 'finish pending' in strip,
				strip)
			boxes = page.evaluate("""() => ['ended-line', 'stage-strip'].map((id) => {
				const node = document.getElementById(id);
				const box = node.getBoundingClientRect();
				return {id, height: box.height, over: node.scrollWidth
					- node.clientWidth, right: box.right, width: innerWidth};
			})""")
			check('both lines are drawn inside the page', all(box['height'] > 0
				and box['over'] <= 0 and box['right'] <= box['width']
				for box in boxes), boxes)

			for number, word, tip in ((3, 'specific', 'not promotable'),
					(2, 'no', None)):
				page.locator('#log .log-entry[data-id="{}"]'.format(number)).click()
				value = page.locator('#log .log-entry[data-id="{}"] .pair'.format(
					number)).filter(has_text='toolchain').locator('.v')
				equal('iteration {} shows its toolchain answer'.format(number),
					value.inner_text(), word)
				got = value.get_attribute('title')
				check('and the tooltip {}'.format('says not promotable' if tip
					else 'is empty'), (tip in got) if tip else not got, got)

			check('no console errors on a finished search', not errors, errors[:3])
			browser.close()
	finally:
		server.shutdown()


def check_paths(root, work):
	"""Payload paths that a URL or a slug would otherwise mangle."""

	check('index entries for /a/b-c and /a-b/c differ',
		monitor_writer.index_path('/a/b-c') != monitor_writer.index_path('/a-b/c'),
		monitor_writer.index_path('/a/b-c'))

	import IPython.display

	out = os.path.join(work, 'my run #1?', 'monitor')
	shown = []
	saved = (IPython.display.display, autoopt_monitor.jupyter_root)
	IPython.display.display = lambda *items: shown.extend(items)
	autoopt_monitor.jupyter_root = lambda under=None: (work, '/')
	try:
		autoopt_monitor.show(root, out_dir=out, writer=False)
	finally:
		IPython.display.display, autoopt_monitor.jupyter_root = saved

	script = next((item.data for item in shown
		if type(item).__name__ == 'Javascript'), '')
	directory = re.findall(r'const dir = (.*);', script)
	equal('the mount URL-encodes each segment of the payload directory',
		directory, ['"my%20run%20%231%3F/monitor"'])


def check_dead_code(root=None, work=None):
	"""Every top-level function and constant in dashboard.js is used, and
	every class the stylesheet names appears in the script, the markup or
	the Python that builds the page. A class built by string concatenation
	would read as unused here; spell it out whole in the script instead."""

	static = os.path.join(HERE, 'static')
	read = lambda name: open(os.path.join(static, name)).read()
	js, html, css = read('dashboard.js'), read('index.html'), read('dashboard.css')
	python = ''.join(open(os.path.join(HERE, name)).read()
		for name in ('autoopt_monitor.py', 'spacing_check.py'))

	names = set(re.findall(r'^(?:async )?function (\w+)\(', js, re.M)) \
		| set(re.findall(r'^(?:const|let) (\w+) = ', js, re.M))
	unused = sorted(name for name in names if len(re.findall(
		r'(?<![\w$])' + name + r'(?![\w$])', js + html)) < 2)
	equal('dashboard.js defines nothing it never uses', unused, [])

	selectors = re.sub(r'\{[^}]*\}', '{}', re.sub(r'/\*.*?\*/', '', css,
		flags=re.S))
	classes = set(re.findall(r'\.([A-Za-z][\w-]*)', selectors))
	unused = sorted(name for name in classes if not re.search(
		r'(?<![\w-])' + re.escape(name) + r'(?![\w-])', js + html + python))
	equal('dashboard.css styles no class nothing uses', unused, [])


def check_export_markup(root, work, args=None):
	"""A README holding `<!--` then `<script` puts the HTML parser in its
	double-escaped state, where `</script>` no longer closes the embedded
	JSON: the export must still open."""

	import shutil
	from playwright.sync_api import sync_playwright

	copy = os.path.join(work, 'export-markup', 'autooptimize')
	shutil.copytree(root, copy, symlinks=True)
	with open(os.path.join(copy, 'iterations', '3', 'README.md'), 'a') as out:
		out.write('\nAn HTML note: <!-- a comment --> and <script>x</script>, '
			'then <!-- <script> unclosed.\n')

	path, _ = autoopt_monitor.export(copy, os.path.join(work,
		'export-markup.html'))
	errors = []

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=not (args and args.headed))
		page = new_page(browser, errors)
		page.goto('file://' + path)
		try:
			page.wait_for_selector('#main-chart .main-svg', timeout=15000)
		except Exception:
			pass
		check('an export whose README holds <!-- and <script still opens',
			'snapshot of' in page.inner_text('#updated') and not errors,
			(page.inner_text('#updated'), errors[:3]))
		browser.close()


def check_pulse(root, work, args=None):
	"""The pulse against what the writer publishes about itself, each case
	in its own payload directory under 200 ms page clocks."""

	from playwright.sync_api import sync_playwright

	headed = bool(args and args.headed)
	cases = {}

	def case(name, edit=None):
		out = os.path.join(work, 'pulse-' + name)
		publish(root, out)
		path = os.path.join(out, 'resources.json')
		resources = json.load(open(path))
		if edit:
			edit(resources)
		monitor_writer.write_json(path, resources)
		variant(out, 'fast.html', lambda page: page.replace('data-standalone',
			'data-standalone data-state-interval="200" '
			'data-resource-interval="200"'))
		cases[name] = serve(out)

	# A writer whose scans take a minute: 200 ms clocks would call it down.
	case('slow', lambda r: r.update(writer={'state_cadence': 60.0,
		'resource_cadence': 60.0}))
	# Scans failing: state.json is frozen while resources.json keeps ticking.
	case('failing', lambda r: r.update(writer={'scan_error': 'error: the '
		'objective needs "direction": "max" or "min"',
		'scan_error_at': time.time()}))
	case('unreadable')
	case('no-psutil', lambda r: r.update(psutil=False, processes=[],
		host={}))
	snapshot = scan.RunScanner(unreadable_copy(root, work)).snapshot()
	monitor_writer.write_json(os.path.join(work, 'pulse-unreadable',
		'state.json'), dict(snapshot, assets=monitor_writer.assets_version()))

	ticking = {'on': True}

	def tick():
		path = os.path.join(work, 'pulse-failing', 'resources.json')
		while ticking['on']:
			resources = json.load(open(path))
			resources['timestamp'] = time.time()
			monitor_writer.write_json(path, resources)
			time.sleep(0.1)

	threading.Thread(target=tick, daemon=True).start()

	print('\nthe pulse')
	errors = []

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch(headless=not headed)

			page = new_page(browser, errors)
			page.goto(cases['slow'][1] + 'fast.html')
			page.wait_for_timeout(3500)
			check('a writer slower than the page clocks, by its published '
				'cadence, still reads as live', page.get_attribute('#pulse',
				'data-state') == 'live', page.inner_text('#updated'))

			page = new_page(browser, errors)
			page.goto(cases['failing'][1] + 'fast.html')
			page.wait_for_timeout(3500)
			check('a frozen state.json is not live while resources.json ticks',
				page.get_attribute('#pulse', 'data-state') != 'live',
				page.get_attribute('#pulse', 'data-state'))
			check('and the page shows the scan error', 'direction'
				in page.inner_text('#updated'), page.inner_text('#updated'))

			page = new_page(browser, errors)
			page.goto(cases['unreadable'][1] + 'index.html')
			page.wait_for_selector('#main-chart .main-svg')
			page.wait_for_timeout(1000)
			check('an unreadable iteration is named on the page',
				'unreadable' in page.inner_text('#updated')
				and 'A5' in page.inner_text('#updated'),
				page.inner_text('#updated'))

			page = new_page(browser, errors)
			page.goto(cases['no-psutil'][1] + 'index.html')
			page.wait_for_selector('#main-chart .main-svg')
			page.wait_for_timeout(1000)
			check('without psutil the process table says why it is empty',
				page.is_visible('#process-card') and 'psutil'
				in page.inner_text('#process-card'),
				page.inner_text('#process-card') if page.query_selector(
				'#process-card') else None)

			check('no console errors on the pulse pages', not errors, errors[:5])
			browser.close()
	finally:
		ticking['on'] = False
		for server, _ in cases.values():
			server.shutdown()


def check_gpu_strip(root, work, args=None):
	"""The GPU strip holds its height. Writers before the shared sample
	published an empty GPU list whenever nvidia-smi timed out, so the cards
	vanished and came back every few seconds and the notebook jumped with
	them; process blocks coming and going did the same at a smaller scale."""

	from playwright.sync_api import sync_playwright

	def gpu(index, processes):
		return {'index': index, 'uuid': 'GPU-{}'.format(index), 'name': 'Stub',
			'utilization': 40.0, 'memory_utilization': 10.0, 'memory_used': 1000.0,
			'memory_total': 40960.0, 'temperature': 50.0, 'power': 100.0,
			'power_limit': 400.0, 'processes': processes, 'mine': bool(processes),
			'lease': None, 'state': 'ours' if processes else 'free'}

	ours = [{'pid': 4242 + i, 'mine': True, 'gpu_memory': 900.0, 'sm': 55.0,
		'kind': 'benchmark', 'target': 'A3', 'cpu_percent': 99.0, 'rss': 1e9,
		'workers': 1, 'elapsed': 30.0} for i in range(2)]
	cycle = [[gpu(0, ours), gpu(1, [])], [], [gpu(0, []), gpu(1, [])]]

	out = os.path.join(work, 'gpu-strip')
	publish(root, out)
	path = os.path.join(out, 'resources.json')
	base = json.load(open(path))
	variant(out, 'fast.html', lambda page: page.replace('data-standalone',
		'data-standalone data-resource-interval="200"'))
	server = serve(out)
	ticking = {'on': True, 'mode': 'cycle'}

	def tick():
		step = 0
		while ticking['on']:
			now = time.time()
			if ticking['mode'] == 'cycle':
				gpus, sampled = cycle[step % len(cycle)], now
			else:
				gpus, sampled = cycle[2], now - 120
			monitor_writer.write_json(path, dict(base, gpus=gpus, timestamp=now,
				gpu_sampled_at=sampled if gpus else None))
			step += 1
			time.sleep(0.3)

	threading.Thread(target=tick, daemon=True).start()

	print('\nthe GPU strip')
	errors = []

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch(headless=not (args and args.headed))
			page = new_page(browser, errors)
			page.goto(server[1] + 'fast.html')
			page.wait_for_selector('.gpu-card')
			samples = page.evaluate('''async () => {
				const out = [];
				for (let i = 0; i < 40; i++) {
					await new Promise((r) => setTimeout(r, 100));
					const below = document.querySelector('#section-frontier');
					out.push([document.querySelectorAll('.gpu-card').length,
						Math.round(below.getBoundingClientRect().top + scrollY)]);
				}
				return out;
			}''')
			counts = sorted({n for n, _ in samples})
			tops = [top for _, top in samples]
			check('the cards stay through a sample with no GPUs', counts == [2],
				counts)
			check('and nothing below the strip moves up as process blocks go',
				all(b >= a for a, b in zip(tops, tops[1:])), sorted(set(tops)))

			page.evaluate('''() => { document.querySelector('#gpu-cards')
				.style.display = 'none'; }''')
			page.wait_for_timeout(800)
			check('a strip that is not laid out does not show its drag bar',
				page.eval_on_selector('#gpu-scroll', 'n => n.hidden'))
			page.evaluate('''() => { document.querySelector('#gpu-cards')
				.style.display = ''; }''')

			ticking['mode'] = 'stale'
			page.wait_for_timeout(1000)
			check('a reading two minutes old is dimmed and says its age',
				page.get_attribute('#gpu-cards', 'data-stale') == 'true'
				and 'GPU reading 2' in page.inner_text('#host-summary'),
				page.inner_text('#host-summary'))
			check('the host summary starts with the host the GPUs are on',
				page.inner_text('#host-summary').startswith(
					socket.gethostname() + ' · '), page.inner_text('#host-summary'))

			check('no console errors on the GPU strip page', not errors, errors[:5])
			browser.close()
	finally:
		ticking['on'] = False
		server[0].shutdown()


###
# The default run
###

def run(base, snapshot, resources, exported, args):
	from playwright.sync_api import sync_playwright

	errors = []
	iterations = snapshot['iterations']
	by_id = {r['id']: r for r in iterations}

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=not args.headed)
		page = new_page(browser, errors)
		page.goto(base + 'index.html')
		page.wait_for_selector('#main-chart .main-svg')
		page.wait_for_timeout(900)

		print('\nsummary band')
		check('no stale-markup banner', not page.query_selector('#stale-markup'))
		equal('hero shows settled iterations', page.inner_text('#hero-value'),
			str(snapshot['counts']['iterations']))
		check('the sub-line counts the runs', str(snapshot['counts']['runs'])
			in page.inner_text('#hero-sub'), page.inner_text('#hero-sub'))
		check('the sub-line counts a subagent still working after its runs',
			'1 subagent working' in page.inner_text('#hero-sub'),
			page.inner_text('#hero-sub'))
		working = page.evaluate('''() => {
			const gd = document.getElementById('main-chart');
			const out = [];
			for (const t of gd.data) {
				if (!t.marker || !Array.isArray(t.marker.line.width)) continue;
				t.marker.line.width.forEach((w, i) => { if (w === 3)
					out.push({id: t.customdata[i], text: t.text[i],
						ring: t.marker.line.color[i]}); });
			}
			return out; }''')
		check('the chart rings the working iteration and says so on hover',
			len(working) == 1 and 'subagent' in working[0]['text']
			and 'README pending' in working[0]['text'], working)
		expected = page.evaluate('''(id) => {
			const d = autooptDashboard, snap = d.state.snapshot;
			const row = snap.iterations.find((r) => r.id === id);
			return d.colour.emphasis(d.colour.groupSlot(row.category)); }''',
			working[0]['id']) if working else None
		check('the ring is in the colour of its category', bool(working) and bool(expected)
			and channel_gap(working[0]['ring'], expected) <= 2,
			(working[0]['ring'] if working else None, expected))
		equal('six tiles', len(page.query_selector_all('#tiles .tile')), 6)
		check('the frontier tile is the pink headline', page.get_attribute(
			'#tiles .tile:first-child', 'data-tone') == 'pink')
		check('tiles name the objective and its units', snapshot['objective']
			['name'] in page.inner_text('#tiles') and 'seeds' in page.inner_text(
			'#tiles'), page.inner_text('#tiles'))
		equal('one chip per category, empty ones included',
			len(page.query_selector_all('#catbar .chip')),
			len(snapshot['categories']))
		check('the title is gradient text', page.get_attribute('.topbar h1',
			'class') == 'title-gradient')

		print('\nink')
		# Every block under a section head is a filled panel, its text inset
		# --space-3 from each edge. A panel holding a scroller (the GPU row,
		# the log, a wide table) is checked on its leading edges only: its
		# content runs past the rest.
		insets = page.eval_on_selector_all('.card:not([hidden]), .summary, '
			'.panel, #sidebar .stat-card', '''n => n.filter((c) =>
				c.offsetParent !== null).map((c) => {
			const box = c.getBoundingClientRect(), s = getComputedStyle(c);
			const scroller = !!c.querySelector('.gpu-cards, .log, .table-scroll');
			const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
			const rects = [];
			for (let t = walker.nextNode(); t; t = walker.nextNode()) {
				if (!t.textContent.trim()) continue;
				const e = t.parentElement;
				if (e.closest('svg, .js-plotly-plot, [hidden], button, select, input'))
					continue;
				const range = document.createRange(); range.selectNodeContents(t);
				for (const r of range.getClientRects())
					if (r.width && r.height) rects.push(r);
			}
			if (!rects.length) return null;
			return { name: (c.id || c.className).slice(0, 30), bg: s.backgroundColor,
				radius: s.borderTopLeftRadius,
				left: Math.min(...rects.map((r) => r.left)) - box.left,
				top: Math.min(...rects.map((r) => r.top)) - box.top,
				right: scroller ? 99 : box.right - Math.max(...rects.map((r) => r.right)) }; })
			.filter(Boolean)''')
		check('every content block is a filled, rounded panel', len(insets) >= 8
			and all(i['bg'] not in ('rgba(0, 0, 0, 0)', 'transparent')
				and i['radius'] != '0px' for i in insets), insets[:2])
		tight = [(i['name'], round(i['left'], 1), round(i['right'], 1),
			round(i['top'], 1)) for i in insets
			if min(i['left'], i['right'], i['top']) < 12]
		check('and no text comes within 12 px of its panel\'s edge', not tight, tight)
		for plot in ('main-chart', 'observer-allocs'):
			check('{}: the plot area is its panel, not a box within it'.format(plot),
				page.eval_on_selector('#' + plot, 'n => n.layout.plot_bgcolor === '
				'n.layout.paper_bgcolor && n.layout.paper_bgcolor === '
				'getComputedStyle(n.closest(".card")).getPropertyValue("--panel").trim()'))
		check('the y axis names the objective and its reduction',
			'median per iteration' in page.eval_on_selector('#main-chart',
			'n => n.layout.yaxis.title.text'))

		print('\nmain chart')
		plotted = points_in(page, 'main-chart')
		equal('one point per iteration, in-flight ones included', plotted,
			expected_points(snapshot))

		# An iteration set aside without a run (13.abandoned in the fixture):
		# a thin x at its own x and the baseline's value, keyed in the legend,
		# and in no count, tree or log.
		closed = snapshot.get('abandoned') or []
		equal('the scanner reports the set-aside iteration', [r['id'] for r in closed], [13])
		marks = page.evaluate('''() => document.querySelector('#main-chart').data
			.filter((t) => t.name.startsWith('abandoned '))
			.map((t) => [t.x, t.y, t.marker.symbol])''')
		equal('it is drawn at its own x and the baseline\'s value, as a thin x',
			marks, [[[13], [snapshot['frontier']['baseline']], 'x-thin-open']])
		check('the legend keys the abandoned mark', page.query_selector(
			'#main-legend .legend-item[data-key="abandoned"]') is not None)
		hover = page.evaluate('''() => document.querySelector('#main-chart').data
			.find((t) => t.name.startsWith('abandoned ')).text[0]''')
		check('its hover says what happened', 'abandoned: closed without a '
			'measured run' in hover and 'baseline' in hover, hover)
		check('it is not an iteration row, so no count or table sees it',
			13 not in {r['id'] for r in snapshot['iterations']})
		check('nor drawn in the lineage tree', page.query_selector(
			'#tree-holder .lineage-node[data-node-id="13"]') is None)
		value = str(round(snapshot['frontier']['value'], 4))[:5]
		headline = page.evaluate('''() => {
			const h = document.querySelector('#main-headline');
			return [h.textContent, getComputedStyle(h).fontFamily,
				(document.querySelector('#main-chart').layout.annotations
					|| []).length]; }''')
		check("the chart's headline carries the frontier value, as HTML in the "
			'font, not a Plotly annotation', value in headline[0]
			and 'monospace' not in headline[1] and headline[2] == 0, headline)
		head = page.evaluate('''() => {
			const t = document.querySelector('#main-chart').data
				.filter((t) => t.line && t.line.dash === 'dot')[0];
			return t ? {x: t.x[0], y: t.y[0], colour: t.line.color} : null; }''')
		check('the frontier staircase is drawn', head is not None)
		if head:
			equal('it starts at the baseline value', round(head['y'], 6),
				round(by_id[0]['value'], 6))
			check('in pink, the number the search exists to move',
				head['colour'] == '#ff4fa3', head['colour'])

		colours = page.evaluate('''() => {
			const out = {on: new Set(), off: new Set()};
			for (const t of document.querySelector('#main-chart').data) {
				if (!Array.isArray(t.customdata) || !t.marker || !Array.isArray(t.marker.color))
					continue;
				t.customdata.forEach((id, i) => {
					const row = autooptDashboard.state.snapshot.iterations
						.find((r) => r.id === id);
					(row.on_frontier ? out.on : out.off).add(t.marker.color[i]); });
			}
			return {on: [...out.on], off: [...out.off]}; }''')
		# Categorical slots: cyan, purple, orange, green, then the catch-all
		# grey. Pink is never one; it is the frontier line and the headline.
		# MISC's full strength is the strong catch-all, not its muted grey.
		# ROOT, the baseline alone, is a light neutral and white at full.
		full = {'#00e5ff', '#a06bff', '#ff9f43', '#57d9a3', '#b5aed0', '#ffffff'}
		muted = {'#5fb8c4', '#9585c4', '#c9a075', '#7fb39a', '#7b7399', '#d9d4ea'}
		check('frontier points are their hue at full strength',
			colours['on'] and set(colours['on']) <= full, colours)
		check('everything else is muted', colours['off']
			and set(colours['off']) <= muted, colours)
		style = page.eval_on_selector('#main-chart', '''n => {
			const t = n.data.find((t) => t.marker && Array.isArray(t.marker.size));
			const off = t.customdata.map((id, i) => i).filter((i) =>
				!autooptDashboard.state.snapshot.iterations.find((r) =>
					r.id === t.customdata[i]).on_frontier)[0];
			return [t.marker.size[off], t.marker.opacity[off]]; }''')
		equal('a small scatter is opaque size 7', style, [7, 1])
		emphasised = page.eval_on_selector('#main-chart', '''n => {
			const t = n.data.find((t) => t.marker && Array.isArray(t.marker.size)
				&& t.customdata.some((id) => autooptDashboard.state.snapshot
					.iterations.find((r) => r.id === id).on_frontier));
			const on = t.customdata.findIndex((id) => autooptDashboard.state
				.snapshot.iterations.find((r) => r.id === id).on_frontier);
			return [t.marker.size[on], t.marker.opacity[on],
				t.selected.marker.size]; }''')
		equal('a frontier point is emphasis: opaque and 2px larger', emphasised[:2],
			[9, 1])
		equal('a selected point is 2px larger too', emphasised[2], 9)
		chip_colours = page.eval_on_selector_all('#catbar .chip', 'n => n.map(c => '
			'[c.dataset.category, getComputedStyle(c).color])')
		check('no category is pink', chip_colours and not any(c[1] in (
			'rgb(255, 79, 163)', 'rgb(199, 122, 159)') for c in chip_colours),
			chip_colours)
		check('CACHING takes the purple slot', ['CACHING', 'rgb(149, 133, 196)']
			in chip_colours, chip_colours)

		partial = [r for r in iterations if r['status'] == 'partial']
		check('the fixture has an under-depth iteration', partial)
		if partial:
			check('an under-depth iteration cannot hold the frontier',
				snapshot['frontier']['id'] != partial[0]['id'])
			check('and is counted in the sidebar', 'under depth'
				in page.inner_text('#sidebar'))

		legend = len(page.query_selector_all('#main-legend .legend-item'))
		# Eight marks, plus the thin x of an abandoned iteration when there is one.
		equal('the legend names every mark drawn', legend,
			8 + bool(snapshot.get('abandoned')))
		keys = page.evaluate('''() => { const k = (name) => { const e = document
			.querySelector(`#main-legend [data-key="${name}"] .mark-key span`);
			const s = getComputedStyle(e); return [s.borderTopStyle, s.borderTopColor,
				e.closest('.legend-item').textContent]; };
			return [k('frontier-line'), k('frontier-point')]; }''')
		check('the frontier line is keyed as a pink dotted line', keys[0][:2]
			== ['dotted', 'rgb(255, 79, 163)'] and 'running best' in keys[0][2],
			keys[0])
		check('and frontier points in words, not a pink circle', 'full strength'
			in keys[1][2] and '2px larger' in keys[1][2] and not page.evaluate(
			'''() => [...document.querySelectorAll('#main-legend .mark-key span')]
				.some((s) => getComputedStyle(s).backgroundColor
					=== 'rgb(255, 79, 163)')'''), keys[1])

		walls = page.evaluate('''() => { const heads = [...document.querySelectorAll(
			'#run-table thead th')].map((h) => h.textContent.toLowerCase());
			const i = heads.findIndex((h) => h.startsWith('wall clock'));
			return [heads[i], [...document.querySelectorAll('#run-table tbody tr')]
				.map((r) => r.children[i].textContent)]; }''')
		check('the run table\'s wall clock is in seconds to two decimals',
			walls[0] == 'wall clock (s)' and walls[1] and all(
				v == '–' or (v.count('.') == 1 and len(v.split('.')[1]) == 2)
				for v in walls[1]), walls[:1] + [walls[1][:4]])

		print('\nlineage has no merge edges')
		equal('the tree draws parent edges only', page.eval_on_selector_all(
			'#tree-holder .lineage-merge', 'n => n.length'), 0)
		check('and the tree card has no merge toggle or legend entry',
			page.query_selector('#toggle-merges') is None
			and page.query_selector('#tree-legend [data-key="merge"]') is None)

		print('\ntree panel: scrollbars and resizing')
		page.eval_on_selector('#tree-resize', 'n => n.scrollIntoView({block: "center"})')
		page.mouse.move(2, 2)
		page.wait_for_timeout(250)
		colours = lambda: page.evaluate('''() => {
			const h = document.querySelector('#tree-holder .lineage-holder');
			const s = getComputedStyle(h);
			return [s.scrollbarWidth, s.scrollbarColor]; }''')
		idle = colours()
		parts = re.findall(r'rgba\([^)]*\)|transparent|#[0-9a-f]+|rgb\([^)]*\)', idle[1])
		check('tree scrollbars are thin and invisible until hovered',
			idle[0] == 'thin' and parts and all(p in ('transparent', 'rgba(0, 0, 0, 0)')
				for p in parts), idle)
		box = page.query_selector('#tree-holder .lineage-holder').bounding_box()
		page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
		page.wait_for_timeout(250)
		hover = colours()
		check('and show a faint thumb while the pointer is over them',
			hover[1] != idle[1] and hover[1].split(' ')[0] not in
				('transparent', 'rgba(0,'), hover)
		max_h = lambda: page.evaluate('''() => parseFloat(getComputedStyle(
			document.querySelector('#tree-holder .lineage-holder')).maxHeight)''')
		start = max_h()
		handle = page.query_selector('#tree-resize').bounding_box()
		hx, hy = handle['x'] + handle['width'] / 2, handle['y'] + handle['height'] / 2
		under = page.evaluate('([x, y]) => { const e = document.elementFromPoint(x, y);'
			' return e ? (e.id || e.className) : null; }', [hx, hy])
		check('the handle is what the pointer hits at its centre', under == 'tree-resize', under)
		page.mouse.move(hx, hy)
		page.mouse.down()
		page.mouse.move(hx, hy + 150, steps=10)
		page.mouse.up()
		page.wait_for_timeout(200)
		dragged = max_h()
		check('dragging the handle down 150 px gives the tree more height',
			abs(dragged - (max(start, box['height']) + 150)) <= 3, (start, dragged))
		check('the map beside the tree follows it', abs(page.evaluate('''() =>
			parseFloat(getComputedStyle(document.querySelector(
				'#tree-holder .lineage-map')).maxHeight)''') - dragged) <= 1)
		page.evaluate('() => autooptDashboard.renderTree()')
		page.wait_for_timeout(200)
		equal('the height survives a re-render', max_h(), dragged)
		page.dblclick('#tree-resize')
		page.wait_for_timeout(200)
		equal('double-clicking the handle restores the default', max_h(), 430.0)

		print('\nROOT category')
		equal('the baseline is ROOT', [r['category'] for r in iterations
			if r['id'] == 0], ['ROOT'])
		equal('and nothing else is', [r['id'] for r in iterations
			if r['category'] == 'ROOT'], [0])
		chips = page.eval_on_selector_all('#catbar .chip', 'n => n.map((c) => '
			'c.dataset.category)')
		check('its chip comes first and MISC stays last', chips[:1] == ['ROOT']
			and chips[-1:] == ['MISC'], chips)
		check('ROOT moved no other category off its slot', ['CACHING',
			'rgb(149, 133, 196)'] in chip_colours, chip_colours)

		print('\nstage boundaries')
		if iterations:
			stage = {r['id']: r.get('mining_stage') or 0 for r in iterations}
			ids = sorted(stage)
			want = [(a + b) / 2 for a, b in zip(ids, ids[1:]) if stage[a] != stage[b]]
			parent = {r['id']: r['parent'] for r in iterations}

			def depth_of(i):
				return 0 if parent.get(i) is None else depth_of(parent[i]) + 1

			generations = [(a + b) / 2 for a, b in zip(ids, ids[1:])
				if depth_of(a) != depth_of(b)]
			check('the fixture has two miner stages, apart from its generations',
				len(want) == 2 and want != generations, [want, generations])
			shapes = page.evaluate('''() => (document.querySelector('#main-chart')
				.layout.shapes || []).map((s) => [s.x0, s.x1, s.yref, s.line.dash,
					s.line.color])''')
			equal('one line wherever the idea miner\'s stage changes, halfway between',
				[s[0] for s in shapes], want)
			check('each is a vertical purple dash spanning the plot', shapes and all(
				s[0] == s[1] and s[2] == 'paper' and s[3] == 'dash'
				and s[4] == '#a06bff' for s in shapes), shapes)
			equal('and Plotly drew every one', page.eval_on_selector_all(
				'#main-chart .shapelayer path', 'n => n.length'), len(want))
			stage_key = page.eval_on_selector('#main-legend [data-key="stage-boundary"] '
				'.mark-key span', 'e => [getComputedStyle(e).borderTopStyle, '
				'getComputedStyle(e).borderTopColor]')
			equal('the legend keys it as a purple dashed line', stage_key,
				['dashed', 'rgb(160, 107, 255)'])
			check('and names the idea miner', 'idea miner' in page.inner_text(
				'#main-legend [data-key="stage-boundary"]'))
			check('the toggle sits beside "Frontier line", pressed', page.evaluate(
				'''() => { const b = document.querySelector('#toggle-stages');
				return b.previousElementSibling.id === 'toggle-stair'
					&& b.textContent === 'Stage boundaries'
					&& b.getAttribute('aria-pressed') === 'true'; }'''))
			page.click('#toggle-stages')
			page.wait_for_timeout(500)
			equal('turning it off removes every line', page.eval_on_selector_all(
				'#main-chart .shapelayer path', 'n => n.length'), 0)
			equal('and its legend row', len(page.query_selector_all(
				'#main-legend [data-key="stage-boundary"]')), 0)
			page.click('#toggle-stages')
			page.wait_for_timeout(500)
			equal('turning it back on restores them', page.eval_on_selector_all(
				'#main-chart .shapelayer path', 'n => n.length'), len(want))

			# Move the newest iteration into a third stage: only stages may
			# move the lines, not the tree or the rounds.
			last = ids[-1]
			moved = want + [(ids[-2] + last) / 2]
			got = page.evaluate('''(last) => {
				const rows = autooptDashboard.state.snapshot.iterations;
				const saved = rows.map((r) => r.mining_stage);
				for (const r of rows) if (r.id === last) r.mining_stage = 3;
				autooptDashboard.renderMain();
				const xs = document.querySelector('#main-chart').layout.shapes
					.map((s) => s.x0);
				rows.forEach((r, i) => { r.mining_stage = saved[i]; });
				autooptDashboard.renderMain();
				return xs; }''', last)
			equal('the lines follow the miner\'s stages', sorted(got), sorted(moved))

			fallback = page.evaluate('''() => {
				const rows = autooptDashboard.state.snapshot.iterations;
				const saved = rows.map((r) => r.mining_stage);
				for (const r of rows) delete r.mining_stage;
				autooptDashboard.renderMain();
				const xs = document.querySelector('#main-chart').layout.shapes
					.map((s) => s.x0);
				const legend = document.querySelector(
					'#main-legend [data-key="stage-boundary"]').textContent;
				rows.forEach((r, i) => { r.mining_stage = saved[i]; });
				autooptDashboard.renderMain();
				return [xs, legend]; }''')
			equal('a search begun before the miner falls back to tree generations',
				fallback[0], generations)
			check('and says so in the legend', 'generation' in fallback[1], fallback[1])

		hues = page.evaluate('''() => {
			const s = autooptDashboard.state.snapshot, c = autooptDashboard.colour;
			const saved = s.categories;
			s.categories = ['ROOT', 'MICRO', 'NUMBA', 'CACHING', 'CACHE_MISS',
				'PRECISION', 'PARALLEL', 'REORGANIZATION', 'IO', 'ALGORITHM',
				'OPTIMIZATION', 'MISC'];
			const out = s.categories.filter((g) => g !== 'ROOT' && g !== 'MISC')
				.map((g) => c.resolved(c.groupSlot(g)));
			s.categories = saved;
			return [out, ['--pink', '--pink-muted', '--bad', '--bad-muted',
				'--cat-other'].map((t) => c.resolved(t))]; }''')
		check('the idea miner\'s ten categories take ten distinct hues',
			len(set(hues[0])) == 10, hues[0])
		check('none of them pink, a verdict red or the catch-all grey',
			not set(hues[0]) & set(hues[1]), hues)

		print('\nconfidence intervals')
		with_ci = [r for r in iterations if r['status'] != 'running'
			and r['value'] is not None and len((r['objective'] or {}).get('values')
				or []) >= 2]
		check('the fixture has iterations with two or more runs', with_ci)
		# The payload's values are rounded to five places, so recomputing from
		# them can differ from the published interval in the last place.
		ci_gap = [max(abs(a - b) for a, b in zip(r['objective']['ci'],
			scan.median_ci(r['objective']['values']))) for r in with_ci]
		check('every such iteration carries the bootstrap interval of scan.median_ci',
			with_ci and max(ci_gap) < 1e-4, max(ci_gap or [None]))
		check('each interval lies within its runs\' min and max and brackets '
			'nothing outside them', with_ci and all(r['objective']['min']
			<= r['objective']['ci'][0] <= r['objective']['ci'][1]
			<= r['objective']['max'] for r in with_ci))
		equal('fewer than two runs has no interval', scan.median_ci([1.0]), None)
		check('it defaults to off, with no whiskers drawn', page.get_attribute(
			'#toggle-ci', 'aria-pressed') == 'false' and not page.evaluate(
			'''() => document.querySelector('#main-chart').data.some(
				(t) => (t.name || '').startsWith('ci '))'''))
		page.click('#toggle-ci')
		page.wait_for_timeout(500)
		whiskers = page.evaluate('''() => document.querySelector('#main-chart').data
			.filter((t) => (t.name || '').startsWith('ci ')).map((t) => ({
				name: t.name, dash: t.line.dash, x: t.x, y: t.y,
				symbol: t.marker.symbol }))''')
		drawn = {}
		for w in whiskers:
			for i in range(0, len(w['x']), 3):
				drawn[w['x'][i]] = [w['y'][i], w['y'][i + 1]]
		shown = with_ci
		check('one whisker per iteration with an interval, from its low to its high',
			shown and all(drawn.get(r['id']) == r['objective']['ci'] for r in shown),
			[(r['id'], drawn.get(r['id']), r['objective']['ci']) for r in shown][:4])
		check('the whiskers are dotted and capped', whiskers and all(
			w['dash'] == 'dot' and w['symbol'] == 'line-ew-open' for w in whiskers),
			whiskers[:1])
		ci_key = page.eval_on_selector('#main-legend [data-key="ci"] .mark-key span',
			'e => getComputedStyle(e).borderTopStyle')
		equal('the legend keys them as a dotted line', ci_key, 'dotted')
		page.click('#toggle-ci')
		page.wait_for_timeout(500)
		check('turning it off removes the whiskers and the legend row', not page.evaluate(
			'''() => document.querySelector('#main-chart').data.some(
				(t) => (t.name || '').startsWith('ci '))''') and not
			page.query_selector('#main-legend [data-key="ci"]'))

		print('\nall-runs toggle')
		medians = points_in(page, 'main-chart')
		runs = sum(len(r.get('runs') or []) for r in iterations
			if r['status'] != 'running' and r['value'] is not None)
		check('it defaults to medians only', page.get_attribute('#toggle-runs',
			'aria-pressed') == 'false')
		page.click('#toggle-runs')
		page.wait_for_timeout(600)
		equal('turning it on adds every run', points_in(page, 'main-chart'),
			medians + runs)
		sizes = page.evaluate('''() => document.querySelector('#main-chart').data
			.filter((t) => t.mode === 'markers').map((t) => [
				Array.isArray(t.marker.size) ? Math.min(...t.marker.size)
					: t.marker.size, Array.isArray(t.marker.opacity)
					? Math.max(...t.marker.opacity) : t.marker.opacity,
				(t.name || '').startsWith('runs')])''')
		small = [s for s in sizes if s[2]]
		big = [s for s in sizes if not s[2]]
		check('run dots are smaller and fainter than the medians', small and
			all(s[0] < min(b[0] for b in big) and s[1] < 1 for s in small), sizes)
		equal('the legend gains a row for them', len(page.query_selector_all(
			'#main-legend .legend-item')), 9 + bool(snapshot.get('abandoned')))
		page.click('#catbar .chip[data-category="CACHING"] .chip-main')
		page.wait_for_timeout(500)
		faded_runs = page.evaluate('''() => {
			const out = {on: new Set(), off: new Set()};
			for (const t of document.querySelector('#main-chart').data) {
				if (!(t.name || '').startsWith('runs ')) continue;
				const on = new Set(t.selectedpoints === undefined ? t.x.map((_, i) => i)
					: t.selectedpoints);
				t.x.forEach((_, i) => (on.has(i) ? out.on : out.off).add(t.name)); }
			return {on: [...out.on], off: [...out.off]}; }''')
		check('an emphasised chip keeps its runs and fades the others',
			faded_runs['on'] == ['runs CACHING'] and 'runs CACHING' not in faded_runs['off']
			and len(faded_runs['off']) >= 3, faded_runs)
		shown_runs = page.evaluate('''() => {
			const n = document.querySelector('#main-chart');
			const groups = [...n.querySelectorAll('.scatterlayer .trace')];
			const out = {};
			n._fullData.forEach((t, i) => { if ((t.name || '').startsWith('runs '))
				out[t.name] = [...new Set([...groups[i].querySelectorAll('path.point')]
					.map((p) => Number(getComputedStyle(p).opacity)))]; });
			return out; }''')
		context = page.evaluate('''() => document.querySelector('#main-chart').data
			.filter((t) => (t.name || '').startsWith('runs ')).map((t) => [t.name,
				t.marker.color, t.marker.size, t.marker.opacity])''')
		muted_of = {'runs CACHE_MISS': '#5fb8c4', 'runs CACHING': '#9585c4',
			'runs MICRO': '#c9a075', 'runs NUMBA': '#7fb39a',
			'runs MISC': '#7b7399', 'runs ROOT': '#d9d4ea'}
		check("each category's runs are its own muted hue, size 4 at 0.3",
			context and all(c[1:] == [muted_of[c[0]], 4, 0.3] for c in context),
			context)
		key = page.eval_on_selector('#main-legend [data-key="runs"] .mark-key',
			'n => getComputedStyle(n).opacity')
		equal('the runs key is drawn at the layer\'s opacity', key, '0.3')
		check('the faded runs are drawn fainter than the context layer; '
			'CACHING\'s keep it', shown_runs.get('runs CACHING') == [0.3] and all(
			max(v) < 0.1 for k, v in shown_runs.items() if k != 'runs CACHING'),
			shown_runs)
		page.click('#catbar .chip[data-category="CACHING"] .chip-main')
		page.wait_for_timeout(400)
		page.click('#toggle-runs')
		page.wait_for_timeout(600)
		equal('turning it off restores medians only',
			points_in(page, 'main-chart'), medians)

		failed = [r for r in iterations if r['status'] not in ('ok', 'running')
			and r['value'] is not None]
		page.click('#toggle-failed')
		page.wait_for_timeout(500)
		equal('hiding gate failures drops every non-passing iteration',
			points_in(page, 'main-chart'), expected_points(snapshot) - len(failed)
			- len(snapshot.get('abandoned') or []))
		page.click('#toggle-failed')
		page.click('#toggle-stair')
		page.wait_for_timeout(500)
		check('the frontier line toggles off', not page.evaluate('''() =>
			document.querySelector('#main-chart').data.some((t) => t.line
				&& t.line.dash === 'dot')'''))
		page.click('#toggle-stair')
		page.wait_for_timeout(300)

		kind = page.evaluate('''() => {
			const s = autooptDashboard.state.snapshot;
			const keep = s.iterations;
			const done = keep.filter((r) => r.status === 'ok');
			s.iterations = Array.from({length: 6000}, (_, i) => Object.assign(
				{}, done[i % done.length], {id: 1000 + i, on_frontier: false}));
			autooptDashboard.renderMain();
			const type = document.getElementById('main-chart').data
				.find((t) => t.type !== 'scatter' || t.mode === 'markers').type;
			const mid = Array.from({length: 1200}, (_, i) => Object.assign(
				{}, done[i % done.length], {id: 1000 + i, on_frontier: false}));
			s.iterations = mid;
			autooptDashboard.renderMain();
			const m = document.getElementById('main-chart').data
				.find((t) => Array.isArray(t.marker && t.marker.size)).marker;
			autooptDashboard.state.showRuns = true;
			autooptDashboard.renderMain();
			const runs = document.getElementById('main-chart').data
				.filter((t) => (t.name || '').startsWith('runs'));
			const context = [runs.reduce((a, t) => a + t.x.length, 0),
				[...new Set(runs.map((t) => t.mode + ' ' + t.marker.size + ' '
					+ t.marker.opacity))]];
			autooptDashboard.state.showRuns = false;
			s.iterations = keep;
			autooptDashboard.renderMain();
			return [type, m.size[0], m.opacity[0], context]; }''')
		equal('past 5,000 iterations the chart is a density', kind[0],
			'histogram2d')
		equal('500 to 5,000 are size 5 at opacity 0.3', kind[1:3], [5, 0.3])
		check('the All runs layer is past 5,000 dots here', kind[3][0] > 5000,
			kind[3][0])
		equal('and, being context, keeps size 4 at opacity 0.3 markers',
			kind[3][1], ['markers 4 0.3'])

		print('\nobservers')
		check('the observer section is shown',
			not page.is_hidden('#section-observers'))
		equal('one chart per observer', len(page.query_selector_all(
			'#observer-charts .reorder-card')), len(snapshot['observers']))
		for observer in snapshot['observers']:
			check('{} chart has points'.format(observer['name']),
				points_in(page, 'observer-' + observer['name']) > 0)
		check('an observer axis names its measure', 'allocs' in
			page.eval_on_selector('#observer-allocs',
			'n => n.layout.yaxis.title.text'))

		print('\nexternal validation')
		block = snapshot['validation']
		check('the fixture has a validated search', block and len(
			block['records']) == 2, block)
		numeric = [name for name in block['measures'] if any(isinstance(
			r['measures'].get(name), (int, float)) for r in block['records'])]
		check('the validation section is shown',
			not page.is_hidden('#section-validation'))
		equal('one small multiple per numeric measure', page.eval_on_selector_all(
			'#validation-charts figure', 'n => n.map(f => f.dataset.measure)'),
			numeric)
		for name in numeric:
			equal('{}: a point per validated round'.format(name), points_in(page,
				'validation-' + name), len(block['records']))
		fills = page.eval_on_selector_all('#validation-max_abs_error .scatterlayer '
			'path.point', 'n => n.map(p => getComputedStyle(p).fill)')
		equal("each round's point takes its verdict's tone (ok, then not)", fills,
			['rgb(127, 179, 154)', 'rgb(255, 92, 122)'])
		equal('a panel title carries its unit, from config.json or the '
			"script's validation.json, and none when neither names one",
			page.eval_on_selector_all('#validation-charts figcaption',
			'n => n.map(f => f.textContent)'), ['n_inputs',
			'max_abs_error (abs. difference)', 'heldout_throughput (records/s)'])

		# A new round rebuilds the cards; if the page is hidden then (a cell
		# scrolled out of view), Plotly draws at its default 700 px. Each plot
		# must come back to its card's width once the page is shown again.
		page.evaluate('''() => { document.body.style.display = 'none';
			document.getElementById('validation-charts').dataset.keys = 'stale';
			autooptDashboard.renderAll(); }''')
		page.wait_for_timeout(300)
		page.evaluate("() => { document.body.style.display = ''; }")
		page.wait_for_timeout(800)
		widths = page.eval_on_selector_all('#validation-charts .plot', '''n => n.map((p) =>
			[Math.round(p.getBoundingClientRect().width), Math.round(p._fullLayout.width)])''')
		check('a validation plot drawn while hidden returns to its card\'s width',
			widths and all(abs(box - plot) <= 1 for box, plot in widths), widths)
		equal('the scanner merges both sources', block.get('units'),
			{'heldout_throughput': 'records/s', 'max_abs_error': 'abs. difference'})
		equal('the x axis is the round', page.eval_on_selector(
			'#validation-max_abs_error', 'n => n.layout.xaxis.title.text'), 'round')
		note = page.query_selector('#validation-note .not-a-gate')
		check('the section says it is not a gate', note and 'Not a gate' in
			note.inner_text())
		geometry = page.evaluate('''() => {
			const note = document.querySelector('#validation-note .not-a-gate');
			const panel = document.querySelector('#validation-charts figure');
			const s = getComputedStyle(note);
			return [note.getBoundingClientRect().bottom,
				panel.getBoundingClientRect().top, s.color,
				getComputedStyle(document.body).getPropertyValue('--text').trim()];
		}''')
		check('above every panel', geometry[0] <= geometry[1], geometry[:2])
		check('in full-strength text, not the faint note colour',
			channel_gap(geometry[2], geometry[3]) < 3, geometry[2:])
		card = page.query_selector('#sidebar .stat-card:has(h3:text-is('
			'"External validation"))')
		check('the sidebar has the latest validation', card is not None)
		if card:
			text = card.inner_text()
			check('naming every measure, the string one included', all(name in text
				for name in block['measures']), text)
			check('with its verdict and that it gated nothing', 'needs attention'
				in text and 'Not a gate' in text, text)
			tone = card.evaluate('''c => [...c.querySelectorAll('.stat-row')]
				.filter((r) => r.textContent.includes('needs attention'))
				.map((r) => getComputedStyle(r.querySelector('.val')).color)''')
			equal('needs attention is in the bad tone', tone, ['rgb(255, 92, 122)'])
			clipped = card.eval_on_selector_all('.val', 'n => n.filter(x => '
				'x.scrollWidth > x.clientWidth + 1).map(x => x.textContent)')
			check('no validation value is clipped', not clipped, clipped)
		hover_point(page, '#validation-max_abs_error', 1)
		page.wait_for_timeout(400)
		label = page.text_content('#validation-max_abs_error .hoverlayer') or ''
		check('hovering a round names its iteration and says not a gate',
			'A{}'.format(block['records'][1]['iteration']) in label
			and 'not a gate' in label, label)
		hover_tone = page.evaluate('''() => [...document.querySelectorAll(
			'#validation-max_abs_error .hoverlayer tspan')].filter((t) =>
			t.textContent.includes('needs attention')
			&& !t.querySelector('tspan')).map((t) =>
			getComputedStyle(t).fill)''')
		check('and its verdict line is in the bad tone', hover_tone
			and set(hover_tone) == {'rgb(255, 92, 122)'}, hover_tone)
		page.mouse.move(5, 5)

		print('\ngpus and processes')
		cards = page.query_selector_all('.gpu-card')
		equal('one card per GPU', len(cards), len(resources['gpus']))
		if resources['gpus']:
			tops = page.eval_on_selector_all('.gpu-card',
				'n => [...new Set(n.map(c => Math.round(c.getBoundingClientRect().top)))]')
			check('GPU cards sit in one row', len(tops) == 1, tops)
			if [g for g in resources['gpus'] if g['index'] == 3 and not g['mine']]:
				card = page.inner_text('.gpu-card:nth-child(4)')
				check('a leased GPU names its holder and note',
					'someone — search iteration' in card and 'leased' in card, card)
			check('a sparkline has a line', page.eval_on_selector(
				'.gpu-spark polyline', 'n => n.getAttribute("points")'
				'.split(" ").length') >= 2)
			marks = page.eval_on_selector('.gpu-card .spark',
				'n => [...n.querySelectorAll(".mark")].map(m => m.dataset.mark)')
			equal('the sparkline marks its min and max', sorted(marks),
				['max', 'min'])

			# Lab's own rule, which the standalone page never loads:
			# `height: auto` scales the 100x26 viewBox with the card's
			# width, and a 0% line falls into the "idle" row beneath.
			overflow = page.evaluate('''() => {
				const style = document.createElement('style');
				style.textContent = '.jp-RenderedHTMLCommon svg '
					+ '{ max-width: 100%; height: auto; }';
				document.head.append(style);
				document.documentElement.classList.add('jp-RenderedHTMLCommon');
				const out = [...document.querySelectorAll('.gpu-card .spark')]
					.map(s => s.querySelector('svg').getBoundingClientRect()
						.height - s.getBoundingClientRect().height)
					.filter(d => Math.abs(d) > 0.5);
				style.remove();
				document.documentElement.classList.remove('jp-RenderedHTMLCommon');
				return out; }''')
			check('sparkline keeps its height under Lab CSS', not overflow,
				overflow)
			badges = page.eval_on_selector_all('.gpu-card .badge', 'n => [...new '
				'Set(n.map(b => b.getBoundingClientRect().height))]')
			check('no GPU badge wraps', len(badges) == 1 and badges[0] < 20, badges)
		body = page.inner_text('#process-body')
		check('the process table names the worker\'s iteration', '#11' in body,
			body)
		check('a run named only by its stdout is attributed to its iteration',
			'#12' in body, body)

		print('\nlineage tree')
		settled_nodes = len(iterations)
		equal('one node per iteration below the condensing limit',
			len(page.query_selector_all('#tree-holder .lineage-node')),
			settled_nodes)
		check('edges stop short of the discs', page.eval_on_selector_all(
			'#tree-holder .lineage-edge', 'n => n.length') >= settled_nodes - 1)
		roots = [entry['id'] for entry in snapshot['state']['roots']]
		depth = page.evaluate('''() => {
			const ys = [...document.querySelectorAll('#tree-holder circle.dot')]
				.map((c) => c.getAttribute('cy'));
			return new Set(ys).size; }''')
		equal('one generation label per row', len(page.query_selector_all(
			'#tree-holder .lineage-rowlabel')), depth)
		equal('roots are drawn larger', page.eval_on_selector_all(
			'#tree-holder circle.dot[r="9"]', 'n => n.length'), len(roots))
		fills = page.evaluate('''() => {
			const out = {on: [], off: []};
			for (const g of document.querySelectorAll('#tree-holder '
				+ '.lineage-node:not(.lineage-tally)')) {
				const dot = g.querySelector('circle.dot');
				if (dot.getAttribute('fill') === 'none') continue;
				(g.dataset.frontier === 'true' ? out.on : out.off).push(
					dot.getAttribute('fill').toLowerCase());
			}
			return out; }''')
		check('frontier nodes are full strength', fills['on']
			and set(fills['on']) <= full, fills['on'])
		check('the other nodes are muted (roots excepted)', fills['off'] and
			len(set(fills['off']) - muted) <= len(roots), set(fills['off']))
		crossed = page.evaluate('''() => {
			const out = [];
			for (const svg of document.querySelectorAll('#tree-holder svg')) {
				const labels = [...svg.querySelectorAll('.lineage-node text')]
					.map((t) => [t.textContent, t.getBBox()]);
				for (const edge of svg.querySelectorAll('.lineage-edge')) {
					const [x, y] = edge.getAttribute('d').split(' ').slice(1, 3)
						.map(Number);
					for (const [text, b] of labels)
						if (x >= b.x - 1 && x <= b.x + b.width + 1
							&& y < b.y + b.height && y > b.y - 16) out.push(text);
				}
			}
			return out; }''')
		check('no edge starts through its parent\'s id label', not crossed,
			crossed)
		rings = page.eval_on_selector_all('#tree-holder circle.dot', 'n => n'
			'.filter(c => c.getAttribute("fill") !== "none" && c.getAttribute('
			'"stroke") && c.getAttribute("stroke") !== "none").length')
		equal('no filled node carries a ring', rings, 0)

		deep = max((r for r in iterations if r['status'] == 'ok'),
			key=lambda r: (len(lineage(by_id, r['id'])), r['id']))
		path = lineage(by_id, deep['id'])
		target = page.query_selector('#tree-holder .lineage-node[data-node-id='
			'"{}"]'.format(deep['id']))
		target.scroll_into_view_if_needed()
		box = target.bounding_box()
		page.mouse.move(box['x'] + box['width'] / 2, box['y'] + box['height'] / 2)
		page.wait_for_timeout(450)
		check('hovering marks the diagram', page.get_attribute(
			'#tree-holder svg', 'data-hovering') == 'true')
		equal('exactly the ancestry is lit', sorted(page.eval_on_selector_all(
			'#tree-holder .lineage-node[data-on-path="true"]',
			'n => n.map(g => Number(g.dataset.nodeId))')), sorted(path))
		faded = page.eval_on_selector_all('#tree-holder .lineage-node:not('
			'[data-on-path="true"])', 'n => [...new Set(n.map(g => '
			'getComputedStyle(g).opacity))]')
		check('everything else is pushed back', faded == ['0.1'], faded)
		widths = page.eval_on_selector_all('#tree-holder .lineage-edge'
			'[data-on-path="true"]', 'n => [...new Set(n.map(e => '
			'getComputedStyle(e).strokeWidth))]')
		check('the hovered path is drawn thick', widths == ['2.4px'], widths)
		tip = page.inner_text('#tip')
		for step in path[:-1]:
			title = (by_id[step]['title'] or '')[:18]
			if title:
				check('#{} is named in the path tooltip'.format(step), title in tip,
					tip[:200])
		page.mouse.move(5, 5)
		page.wait_for_timeout(300)
		check('leaving clears it', page.get_attribute('#tree-holder svg',
			'data-hovering') is None)
		check('one generation marker per row', len(page.query_selector_all(
			'#tree-holder .lineage-level')) == depth)


		print('\nsidebar')
		titles = page.eval_on_selector_all('#sidebar .stat-card h3',
			'n => n.map(x => x.textContent)')
		for wanted in ('Search', 'Frontier', 'Gain per step', 'Throughput',
			'Outcomes', 'By category', 'Summaries'):
			check('{} card'.format(wanted), wanted in titles, titles)
		clipped = page.eval_on_selector_all('#sidebar .bar-row .n', 'n => n.filter'
			'(x => x.scrollWidth > x.clientWidth + 1).map(x => x.textContent)')
		check('no sidebar value is clipped', not clipped, clipped)
		bars = page.eval_on_selector_all('.gain-list .bar', 'n => n.map(b => '
			'b.getBoundingClientRect().width)')
		gains = [abs(s['gain']) for s in snapshot['frontier']['steps'] if s['gain']]
		ratio = [b / bars[0] for b in bars] if bars and bars[0] else []
		check('gain bars are at true proportion', ratio and all(abs(r - g / gains[0])
			< 0.03 for r, g in zip(ratio, gains)), (ratio, gains))

		page.click('#tree-holder .lineage-node[data-node-id="3"]')
		page.wait_for_timeout(400)
		check('clicking a node outlines its runs in Every run',
			page.eval_on_selector_all('#run-table tbody tr[data-pinned="true"]',
			'n => n.length') > 0)
		panel = page.query_selector('#tree-node')
		check('and opens its panel under the tree', panel.is_visible())
		page.wait_for_function('() => document.getElementById("tree-node")'
			+ '.textContent.includes("{}")'.format(PANEL_TAIL), timeout=5000)
		excerpt = page.evaluate('''() => autooptDashboard.state.snapshot.iterations
			.find((r) => r.id === 3).notes''')
		check('the panel holds the whole README, past the excerpt',
			PANEL_TAIL not in excerpt and 'Appendix' in panel.text_content())
		keys = page.eval_on_selector_all('#tree-node .pair .k', 'n => n.map((k) => k.textContent)')
		check('and every detail of the iteration', all(k in keys for k in
			('status', 'parent', 'median', 'branch')), keys)
		page.click('#tree-holder .lineage-node[data-node-id="3"]')
		page.wait_for_timeout(200)
		check('a second click on the node closes the panel',
			not page.query_selector('#tree-node').is_visible())

		print('\nrun table')
		pinned_rows = lambda: page.eval_on_selector_all(
			'#run-table tbody tr[data-pinned="true"]', 'n => n.length')
		shown = [r for r in iterations if r['status'] != 'running']
		want = sum(len(r.get('runs') or []) + len(r.get('failures') or [])
			for r in shown)
		check('the table counts every run of every settled iteration',
			page.inner_text('#run-table .table-count').endswith(
				'of {} shown'.format(want)),
			(page.inner_text('#run-table .table-count'), want))
		heads = page.eval_on_selector_all('#run-table thead th',
			'n => n.map(h => h.textContent)')
		for name in [g['name'] for g in snapshot['gates']] \
				+ [o['name'] for o in snapshot['observers']]:
			check('a column for ' + name, any(h.startswith(name) for h in heads),
				heads)
		cells = page.eval_on_selector_all('#run-table tbody tr:first-child td',
			'n => n.map(c => c.textContent)')
		check('a row carries numbers, not dashes', sum(c not in ('', '–')
			for c in cells) >= 8, cells)

		page.select_option('#run-table select[data-key="status"]', 'gate-failed')
		page.wait_for_timeout(150)
		# Only the gate columns: the status columns are red on these rows too.
		red = page.eval_on_selector('#run-table', '''(h, names) => {
			const heads = [...h.querySelectorAll('thead th')].map(t => t.textContent);
			const cols = heads.map((t, i) => names.some(n => t.startsWith(n)) ? i : -1)
				.filter(i => i >= 0);
			return [...h.querySelectorAll('tbody tr')].flatMap(r => cols.map(i =>
				r.cells[i])).filter(c => c.dataset.tone === 'bad')
				.map(c => getComputedStyle(c).color); }''',
			[g['name'] for g in snapshot['gates']])
		check('a run that broke a gate is drawn in the bad tone',
			'rgb(255, 92, 122)' in red, red)
		page.select_option('#run-table select[data-key="status"]', '')
		page.wait_for_timeout(150)

		def point_of(want_key):
			# The DOM paints traces and points in data order, so the index of
			# a customdata entry names the path to move onto.
			return page.evaluate('''([key]) => {
				const n = document.querySelector('#main-chart');
				const groups = n.querySelectorAll('.scatterlayer .trace');
				for (let t = 0; t < n.data.length; t++) {
					const i = (n.data[t].customdata || []).findIndex(
						(c) => String(c) === key);
					if (i < 0) continue;
					const r = groups[t].querySelectorAll('path.point')[i]
						.getBoundingClientRect();
					return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
				}
				return null; }''', [str(want_key)])

		def click_point(key):
			page.eval_on_selector('#main-chart',
				'n => n.scrollIntoView({block: "center"})')
			page.wait_for_timeout(250)
			at = point_of(key)
			if at is None:
				raise AssertionError('no point for {}'.format(key))
			page.mouse.click(at['x'], at['y'])
			page.wait_for_timeout(400)

		# The oldest iteration sorts last, so pinning it has to change page
		# and scroll the box; nothing is outlined before the click.
		oldest = min(r['id'] for r in shown if r.get('runs'))
		equal('nothing is outlined before a click', pinned_rows(), 0)
		click_point(oldest)
		n_oldest = len(by_id[oldest]['runs']) + len(by_id[oldest].get('failures')
			or [])
		equal('clicking a point outlines each of its runs', pinned_rows(),
			n_oldest)
		look = page.eval_on_selector('#run-table', '''h => {
			const row = h.querySelector('tbody tr[data-pinned="true"]');
			const box = h.querySelector('.table-scroll').getBoundingClientRect();
			const head = h.querySelector('thead').getBoundingClientRect();
			const r = row.getBoundingClientRect();
			return { shadow: getComputedStyle(row.cells[1]).boxShadow,
				id: row.cells[0].textContent,
				inside: r.top >= head.bottom - 1 && r.bottom <= box.bottom + 1 }; }''')
		check('in the pink outline', 'rgb(255, 79, 163)' in look['shadow'], look)
		equal('the outlined rows are that iteration', look['id'], 'A' + str(oldest))
		check('and the table scrolled to them, below its header', look['inside'],
			look)
		click_point(oldest)
		equal('clicking it again clears the outline', pinned_rows(), 0)

		page.click('#toggle-runs')
		page.wait_for_timeout(400)
		run = by_id[oldest]['runs'][-1]
		click_point('{}:{}'.format(oldest, run['seed']))
		one = page.eval_on_selector_all('#run-table tbody tr[data-pinned="true"]',
			'n => n.map(r => r.cells[0].textContent + " seed " + '
			'[...r.cells].map(c => c.textContent)['
			'[...r.closest("table").querySelectorAll("th")].findIndex('
			'h => h.textContent.startsWith("seed"))])')
		equal('clicking a run dot outlines that one run', one,
			['A{} seed {}'.format(oldest, run['seed'])])
		click_point('{}:{}'.format(oldest, run['seed']))
		page.click('#toggle-runs')
		page.wait_for_timeout(300)
		equal('and a second click clears it', pinned_rows(), 0)

		print('\nobserver reordering')
		order = lambda: page.eval_on_selector_all('#observer-charts .reorder-card',
			'n => n.map(c => c.dataset.panel)')
		before = order()
		drag_panel(page, '#observer-charts .reorder-card:last-child .grip',
			'#observer-charts .reorder-card:first-child')
		page.wait_for_timeout(700)
		after = order()
		equal('dragging a grip to the front reorders the panels', after,
			[before[-1]] + before[:-1])
		check('the moved panel still has its chart',
			points_in(page, 'observer-' + after[0]) > 0)
		stored = page.evaluate('() => Object.keys(localStorage)'
			'.filter(k => k.startsWith("autoopt:order:observers"))')
		check('the order is persisted per search', stored, stored)
		page.reload()
		page.wait_for_selector('#observer-charts .reorder-card .main-svg')
		page.wait_for_timeout(900)
		equal('and survives a reload', order(), after)

		print('\niteration log')
		equal('one entry per iteration', len(page.query_selector_all(
			'#log .log-entry')), len(iterations))
		ids = page.eval_on_selector_all('#log .log-entry',
			'n => n.map(e => Number(e.dataset.id))')
		check('newest first', ids == sorted(ids, reverse=True))
		equal('frontier entries are marked', len(page.query_selector_all(
			'#log .log-entry[data-frontier="true"]')),
			sum(1 for r in iterations if r['on_frontier']))
		equal('the header names the columns once', page.eval_on_selector_all(
			'#log .log-header span', 'n => n.map(s => s.textContent)'),
			['#', 'type', snapshot['objective']['name'], 'runs', 'time', 'status',
				'idea'])
		check('the type renders in capitals', page.inner_text(
			'#log .log-entry .m:nth-child(2)').isupper())
		columns = page.evaluate('''() => [...document.querySelectorAll(
			'#log .log-entry')].slice(0, 6).map((e) => [...e.querySelectorAll(
			'.log-row > *')].map((c) => Math.round(c.getBoundingClientRect().left)))''')
		check('every entry puts its columns at the same x', len(columns) > 1
			and all(c == columns[0] for c in columns), columns[:2])
		heads = page.evaluate('''() => [...document.querySelectorAll(
			'#log .log-header > *')].map((c) => Math.round(
			c.getBoundingClientRect().left))''')
		check('the header sits over its values', len(heads) == len(columns[0])
			and all(abs(a - b) <= 2 for a, b in zip(heads, columns[0])),
			(heads, columns[0]))
		decimals = page.eval_on_selector_all('#log .log-entry .m:nth-child(3)',
			'n => [...new Set(n.map(c => c.textContent).filter(t => '
			't.includes(".")).map(t => t.split(".")[1].length))]')
		check('the objective column shares one decimal count', len(decimals) == 1,
			decimals)
		check('the wall-clock unit is not uppercased', not any(ch.isupper() for ch
			in page.inner_text('#log .log-entry .m:nth-child(5)')))
		check('entries are one line until opened',
			not page.query_selector('#log .log-desc'))

		page.click('#log .log-entry[data-id="1"]')
		page.wait_for_timeout(400)
		entry = '#log .log-entry[data-id="1"]'
		desc = page.query_selector(entry + ' .log-desc')
		check('opening shows the README', desc and len(desc.inner_text()) > 20)
		check('and its detail', len(page.query_selector_all(entry
			+ ' .log-detail .pair')) >= 12)
		seeds = len(by_id[1].get('runs') or [])
		equal('the detail lists every seed', page.eval_on_selector_all(entry
			+ ' .log-detail .k', 'n => n.filter(k => k.textContent.startsWith('
			'"SEED ")).length'), seeds)
		keys = page.eval_on_selector_all(entry + ' .log-detail .k',
			'n => n.map(k => k.textContent)')
		check('the detail breaks the iteration\'s time down, the rest as '
			'subagent work', all(k in keys for k in ('time total', 'time: runs',
				'time: subagent work')), keys)
		timing = by_id[1].get('time') or {}
		parts = (timing.get('runs') or 0) + (timing.get('tests') or 0) \
			+ (timing.get('validation') or 0) + (timing.get('subagent') or 0)
		check('and the parts add up to the total', timing.get('total') is not None
			and (abs(parts - timing['total']) < 1e-4 or timing['subagent'] == 0),
			timing)
		check('opening outlines its runs in Every run', page.eval_on_selector_all(
			'#run-table tbody tr[data-pinned="true"]', 'n => n.length') > 0)

		print('\ncode changes, labels and SHOW MORE')
		stat = by_id[1].get('diffstat')
		check('the scanner gives a settled iteration its diffstat: lines '
			'added and deleted, files added, deleted and modified',
			isinstance(stat, list) and len(stat) == 5
			and all(isinstance(v, int) for v in stat) and stat[4] >= 1
			and stat[0] + stat[1] > 0, stat)
		check('the baseline, with no parent, has none', 'diffstat' not in by_id[0])
		check('nor an iteration in flight', all('diffstat' not in r
			for r in iterations if r['status'] == 'running'))
		shown = page.evaluate('''(e) => {
			const line = document.querySelector(e + ' .log-desc .log-diffstat');
			if (!line) return null;
			const first = line.parentNode.firstElementChild === line;
			return {first, parts: [...line.children].map((s) => [s.className,
				s.textContent, getComputedStyle(s).color])}; }''', entry)
		check('the open entry leads its README with the code change', shown
			and shown['first'], shown)
		if shown and stat:
			equal('lines added and deleted, as GitHub writes them',
				[p[1] for p in shown['parts'][:2]],
				['+{:,}'.format(stat[0]), '−{:,}'.format(stat[1])])
			equal('in GitHub\'s green and red', [p[2] for p in shown['parts'][:2]],
				['rgb(63, 185, 80)', 'rgb(248, 81, 73)'])
			check('and the files modified in its amber', any(p[0] == 'diff-mod'
				and p[2] == 'rgb(210, 153, 34)' and p[1].startswith(str(stat[4]))
				for p in shown['parts']), shown['parts'])
		tags = page.eval_on_selector_all(entry + ' .log-detail .k-tag',
			'n => n.map((t) => [t.textContent, getComputedStyle(t).color])')
		kinds = {text: colour for text, colour in tags}
		equal('the detail labels its gates, observers and seeds in capitals',
			sorted(kinds), ['GATE', 'OBS', 'SEED'])
		equal('each kind in its own muted hue', [kinds.get(k) for k in
			('GATE', 'OBS', 'SEED')], ['rgb(201, 178, 125)', 'rgb(95, 184, 196)',
				'rgb(149, 133, 196)'])
		check('none at full strength, and none the plain key colour', kinds and all(
			max(int(c) for c in re.findall(r'\d+', colour)) <= 210
			and colour != page.eval_on_selector(entry + ' .log-detail .k',
				'n => getComputedStyle(n).color') for colour in kinds.values()),
			kinds)

		more = page.query_selector(entry + ' .log-more')
		equal('a README the payload did not cut has no SHOW MORE',
			more is not None, bool(by_id[1].get('notes_cut')))
		check('iteration 3\'s README was cut', by_id[3].get('notes_cut') is True)
		long = '#log .log-entry[data-id="3"]'
		page.click(long)
		page.wait_for_timeout(300)
		button = page.query_selector(long + ' .log-desc .log-more')
		check('a cut README ends in SHOW MORE', button is not None
			and button.inner_text() == 'SHOW MORE'
			and PANEL_TAIL not in page.inner_text(long + ' .log-desc'))
		if button:
			button.click()
			page.wait_for_function('(e) => document.querySelector(e + " .log-desc")'
				+ '.textContent.includes("{}")'.format(PANEL_TAIL), arg=long,
				timeout=5000)
			equal('clicking it shows the whole README and keeps the entry open',
				page.get_attribute(long, 'data-open'), 'true')
			equal('the button then reads SHOW LESS', page.inner_text(long
				+ ' .log-more'), 'SHOW LESS')
			check('the whole README leaves out the title and the run table',
				page.eval_on_selector(long + ' .log-desc', 'n => !n.querySelector('
				'"table, .md-h1")'))
			page.evaluate('() => autooptDashboard.renderAll()')
			page.wait_for_timeout(1200)
			check('it survives the log\'s re-render', PANEL_TAIL in page.inner_text(
				long + ' .log-desc'))
			page.click(long + ' .log-more')
			page.wait_for_timeout(300)
			check('SHOW LESS returns to the excerpt, the entry still open',
				PANEL_TAIL not in page.inner_text(long + ' .log-desc')
				and page.get_attribute(long, 'data-open') == 'true')
		page.click(long + ' .log-row')
		page.wait_for_timeout(300)

		labels, whole = [], ''
		for index in range(6):
			hover_point(page, '#main-chart', index)
			page.wait_for_timeout(350)
			labels = page.evaluate('''() => [...document.querySelectorAll(
				'#main-chart .hoverlayer tspan')].filter((t) => !t.querySelector(
				'tspan')).map((t) => [t.textContent, getComputedStyle(t).fill])''')
			whole = page.text_content('#main-chart .hoverlayer') or ''
			if any(text.startswith('+') for text, _ in labels):
				break
		page.mouse.move(5, 5)
		added = [fill for text, fill in labels if re.fullmatch(r'\+[\d,]+', text)]
		deleted = [fill for text, fill in labels if re.fullmatch('−' + r'[\d,]+', text)]
		check('the scatter tooltip carries the lines added and deleted, in '
			'GitHub\'s green and red', added == ['rgb(63, 185, 80)']
			and deleted == ['rgb(248, 81, 73)'], labels)
		check('and the files modified', 'files modified' in whole, whole[-300:])
		check('and the idea miner\'s stage', 'miner stage' in whole, whole[:300])

		print('\nselection')
		page.eval_on_selector('#main-chart', 'n => n.scrollIntoView({block: '
			'"center"})')
		page.wait_for_timeout(300)
		box = page.query_selector('#main-chart .nsewdrag').bounding_box()
		left, top = box['x'] + 5, box['y'] + 5
		bottom, middle = box['y'] + box['height'] - 5, box['x'] + box['width'] * 0.45
		page.mouse.move(left, top)
		page.mouse.down()
		for x, y in ((left, bottom), (middle, bottom), (middle, top), (left, top)):
			page.mouse.move(x, y, steps=10)
		page.mouse.up()
		page.wait_for_timeout(700)
		check('a lasso selects iterations', page.evaluate('() => '
			'autooptDashboard.state.selection && '
			'autooptDashboard.state.selection.size > 0'))
		check('and reveals Clear selection', page.is_visible('#clear-selection'))
		check('the selection dims tree nodes', page.eval_on_selector_all(
			'#tree-holder .lineage-node[data-dim="true"]', 'n => n.length') > 0)
		check('the selection reaches the observer charts', page.eval_on_selector(
			'#observer-allocs', 'n => n.data.some(t => Array.isArray('
			't.selectedpoints))'))
		page.click('#clear-selection')
		page.wait_for_timeout(400)
		equal('Clear selection restores every point', points_in(page, 'main-chart'),
			expected_points(snapshot))

		print('\nchip emphasis')
		caching_ids = {r['id'] for r in iterations if r['category'] == 'CACHING'}
		opacities = lambda: page.eval_on_selector_all('#main-chart .scatterlayer '
			'.trace path.point', 'n => n.map(p => Number(getComputedStyle(p)'
			'.opacity))')
		check('no point is faded before a chip is clicked',
			min(opacities()) > 0.5, sorted(set(opacities())))
		page.click('#catbar .chip[data-category="CACHING"] .chip-main')
		page.wait_for_timeout(500)
		check('clicking a chip marks it emphasised', page.get_attribute(
			'#catbar .chip[data-category="CACHING"]', 'data-emphasised') == 'true')
		equal('emphasis hides nothing', points_in(page, 'main-chart'),
			expected_points(snapshot))
		shown = opacities()
		settled_caching = sum(1 for r in iterations if r['category'] == 'CACHING'
			and r['status'] != 'running' and r['value'] is not None)
		equal('only its points stay at full opacity in the chart',
			sum(1 for o in shown if o > 0.5), settled_caching + sum(1 for r
			in iterations if r['status'] == 'running'))
		check('the rest fade', sum(1 for o in shown if o < 0.2) >= len(shown)
			- settled_caching - 1, sorted(set(shown)))
		check('it reaches the observer charts', page.eval_on_selector(
			'#observer-allocs', 'n => n.data.some(t => Array.isArray('
			't.selectedpoints))'))
		faded = page.eval_on_selector_all('#log .log-entry[data-faded="true"]',
			'n => n.map(e => Number(e.dataset.id))')
		equal('the log fades every other category', sorted(faded),
			sorted(r['id'] for r in iterations if r['id'] not in caching_ids))
		log_opacity = page.eval_on_selector('#log .log-entry[data-faded="true"]',
			'n => getComputedStyle(n).opacity')
		equal('a faded entry is drawn at 0.35', log_opacity, '0.35')
		dim = page.eval_on_selector_all('#tree-holder .lineage-node[data-dim='
			'"true"]', 'n => n.map(g => Number(g.dataset.nodeId))')
		equal('the tree dims every other category', sorted(dim),
			sorted(r['id'] for r in iterations if r['id'] not in caching_ids))
		page.click('#catbar .chip[data-category="CACHING"] .chip-main')
		page.wait_for_timeout(500)
		check('clicking again clears it', min(opacities()) > 0.5 and not
			page.query_selector('#log .log-entry[data-faded="true"]'))

		page.click('#catbar .chip[data-category="MISC"] .chip-main')
		page.wait_for_timeout(500)
		misc = page.evaluate('''() => { const n = document.querySelector('#main-chart');
			const groups = [...n.querySelectorAll('.scatterlayer .trace')];
			const i = n._fullData.findIndex((t) => t.name === 'MISC');
			return [...new Set([...groups[i].querySelectorAll('path.point')]
				.map((p) => getComputedStyle(p).fill))].filter((f) => f !== 'none'); }''')
		equal('an emphasised MISC point is the strong catch-all, not the muted grey',
			misc, ['rgb(181, 174, 208)'])
		page.click('#catbar .chip[data-category="MISC"] .chip-main')
		page.wait_for_timeout(400)

		page.click('#catbar .chip[data-category="CACHE_MISS"] .eye')
		page.wait_for_timeout(400)
		equal("a category chip's dot hides its points in the chart",
			points_in(page, 'main-chart'), expected_points(snapshot,
				hidden=('CACHE_MISS',)))
		check('and in the log', len(page.query_selector_all('#log .log-entry'))
			== sum(1 for r in iterations if r['category'] != 'CACHE_MISS'))
		check('and dims it in the tree', page.eval_on_selector_all(
			'#tree-holder .lineage-node[data-dim="true"]', 'n => n.length')
			== sum(1 for r in iterations if r['category'] == 'CACHE_MISS'))
		page.click('#catbar .chip[data-category="CACHE_MISS"] .eye')
		page.wait_for_timeout(300)

		print('\nspacing, with a log entry open')
		page.evaluate('() => window.scrollTo(0, 0)')
		if not page.query_selector('#log .log-entry[data-open="true"]'):
			page.click('#log .log-entry[data-id="1"]')
		page.wait_for_timeout(400)
		check_spacing(page)

		print('\nx log')
		labels = page.eval_on_selector_all('#scales-main button', 'n => n.map((b) => '
			'[b.textContent, b.getAttribute("aria-pressed")])')
		check('the objective chart has x log and y log toggles, x off by default',
			[l[0].split(' (')[0] for l in labels] == ['x log', 'y log']
			and labels[0][1] == 'false', labels)
		check('x log says the baseline at x = 0 cannot be drawn',
			'(1 hidden)' in labels[0][0], labels)
		page.click('#scales-main button:has-text("x log")')
		page.wait_for_timeout(500)
		xa = page.evaluate('''() => { const f = document.getElementById('main-chart')
			._fullLayout.xaxis; return [f.type, f.autorange, f.range]; }''')
		check('turning it on draws a log x axis with its own range', xa[0] == 'log'
			and xa[1] is False, xa)
		check_spacing(page)
		page.click('#scales-main button:has-text("x log")')
		page.wait_for_timeout(500)
		equal('and turning it off restores linear', page.evaluate('''() =>
			document.getElementById('main-chart')._fullLayout.xaxis.type'''), 'linear')

		print('\nlog axes and exemptions')
		page.click('#scales-main button:has-text("y log")')
		page.wait_for_timeout(500)
		# One seed far above every median, so a range taken from the medians
		# alone would cut it off.
		page.evaluate('''() => { const r = autooptDashboard.state.snapshot
			.iterations.find((r) => r.status === 'ok' && (r.runs || []).length);
			r.runs.push({seed: 99, objective: 3 * r.value, seconds: 1}); }''')
		page.click('#toggle-runs')
		page.wait_for_timeout(500)
		axis = page.evaluate('''() => { const n = document.getElementById('main-chart');
			const full = n._fullLayout.yaxis;
			const ys = n.data.filter((t) => t.mode === 'markers').flatMap((t) => t.y)
				.filter((v) => v > 0);
			return [full.type, Math.abs(full.range[1] - full.range[0]), full.tickmode,
				full.autorange, full.range, Math.log10(Math.min(...ys)),
				Math.log10(Math.max(...ys)), full.tickvals, full.ticktext]; }''')
		check('a log axis sets its own range and places its own ticks',
			axis[0] == 'log' and axis[3] is False and axis[2] == 'array', axis)
		vals, text = axis[7], axis[8]
		decades = [round(math.log10(v), 6) for v in vals]
		# 1-2-5 (at most three ticks a decade) within two decades, else decades;
		# a narrow axis may fall back to every 1-9 step.
		per_decade = max(collections.Counter(math.floor(d) for d in decades).values())
		check('it picks 1-2-5 or decades from its range', len(vals) >= 2 and (
			per_decade <= (3 if axis[1] <= 2 else 1) or per_decade <= 9), (vals, axis[1]))
		check('every tick label is the full number, not a bare leading digit',
			[float(t.replace(',', '')) for t in text] == [float(v) for v in vals]
			and all(axis[4][0] <= d <= axis[4][1] for d in decades), (vals, text))
		check('and that range holds every point drawn, the runs included',
			axis[4][0] < axis[5] and axis[6] < axis[4][1], axis)
		check_spacing(page)
		page.evaluate('''() => autooptDashboard.state.snapshot.iterations
			.forEach((r) => { r.runs = (r.runs || []).filter((x) => x.seed !== 99); })''')
		page.click('#toggle-runs')
		page.click('#scales-main button:has-text("y log")')
		page.wait_for_timeout(500)
		page.evaluate('''() => {
			const probe = document.createElement('div');
			probe.id = 'spacing-probe';
			probe.innerHTML = '<div class="legend"><span class="legend-item">'
				+ '<span class="swatch hollow" style="border:1px solid #fff"></span>'
				+ 'hollow key</span></div><details><summary>Folded</summary><div '
				+ 'style="border-left:1px solid #fff;padding-left:2px">folded text'
				+ '</div></details>';
			document.querySelector('main').append(probe); }''')
		exempt = spacing_check.spacing_violations(page, '.autoopt-root')
		page.evaluate('() => document.getElementById("spacing-probe").remove()')
		check('legend keys and a closed <details> are not measured',
			not exempt, exempt[:4])

		print('\nwrapped tiles')
		tiles = wrapped_tiles(browser, base, args.shot and args.shot.replace(
			'.png', '-narrow.png'))
		check('wrapped tiles: a row start drops its divider, keeps the column, '
			'rows 12px apart', tiles['rows'] > 1 and tiles['starts']
			== tiles['rows'] - 1 and tiles['clear'] and tiles['columns']
			and set(tiles['gaps']) == {12} and not tiles['dividers']
			and not tiles['spacing'], tiles)

		if args.shot:
			page.screenshot(path=args.shot, full_page=True)
			print('screenshot: ' + args.shot)

		check('no console errors', not errors, errors[:5])

		print('\nstale copies, the pulse and the export')
		stale = browser.new_page()
		stale.goto(base + 'stale.html')
		stale.wait_for_timeout(1500)
		check('an old script asks for the cell to be re-run', 're-run'
			in stale.inner_text('#updated'), stale.inner_text('#updated'))

		missing = browser.new_page()
		missing.goto(base + 'missing.html')
		missing.wait_for_timeout(900)
		check('restored markup missing an id shows the banner', 'tiles' in
			(missing.text_content('#stale-markup') or ''))

		dead = browser.new_page()
		dead.goto(base + 'dead.html')
		dead.wait_for_timeout(3500)
		check('a stopped publisher reads as down', dead.get_attribute('#pulse',
			'data-state') == 'down' and 'publisher stopped' in dead.inner_text(
			'#updated'), dead.inner_text('#updated'))

		snap_errors = []
		snap = new_page(browser, snap_errors)
		snap.goto('file://' + exported)
		snap.wait_for_selector('#main-chart .main-svg')
		snap.wait_for_timeout(600)
		check('the export opens from a file, read-only', 'snapshot of'
			in snap.inner_text('#updated') and snap.inner_text('#hero-value')
			== str(snapshot['counts']['iterations']), snap.inner_text('#updated'))
		check('the export runs without errors', not snap_errors, snap_errors[:3])

		browser.close()


def run_without_validation(work, root, args):
	"""A search with no external validation shows no trace of one: not an
	empty panel, not a sidebar card. One configured but not run yet says
	so in words and draws no chart."""

	import shutil
	from playwright.sync_api import sync_playwright

	pages = {}

	for name, keep_config in (('unrun', True), ('plain', False)):
		copy = os.path.join(work, name, 'autooptimize')
		shutil.copytree(root, copy, symlinks=True)
		# Moved aside, not deleted: the copy is scratch either way.
		os.rename(os.path.join(copy, 'validations'),
			os.path.join(copy, 'validations.aside'))

		if not keep_config:
			path = os.path.join(copy, 'config.json')
			config = json.load(open(path))
			del config['external_validation']
			json.dump(config, open(path, 'w'), indent='\t')

		snapshot = publish(copy, os.path.join(work, name + '-published'))
		pages[name] = (snapshot, serve(os.path.join(work, name + '-published')))

	print('\nno external validation')
	check('the scanner emits none for a search without one',
		pages['plain'][0]['validation'] is None, pages['plain'][0]['validation'])
	unrun = pages['unrun'][0]['validation']
	check('and a configured one with no rounds yet', unrun and unrun['configured']
		and unrun['records'] == [], unrun)

	errors = []

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=not args.headed)

		page = new_page(browser, errors)
		page.goto(pages['plain'][1][1] + 'index.html')
		page.wait_for_selector('#main-chart .main-svg')
		page.wait_for_timeout(700)
		check('the section is hidden', page.is_hidden('#section-validation'))
		equal('and draws no panel', len(page.query_selector_all(
			'#validation-charts > *')), 0)
		check('the sidebar has no validation card', 'External validation'
			not in page.inner_text('#sidebar'))

		page = new_page(browser, errors)
		page.goto(pages['unrun'][1][1] + 'index.html')
		page.wait_for_selector('#main-chart .main-svg')
		page.wait_for_timeout(700)
		check('configured but not run: the section says so',
			not page.is_hidden('#section-validation') and 'no round has been '
			'validated' in page.inner_text('#validation-charts'),
			page.inner_text('#validation-charts'))
		equal('and draws no chart', len(page.query_selector_all(
			'#validation-charts .plot')), 0)

		check('no console errors without a validation', not errors, errors[:5])
		browser.close()

	for _, (server, _) in pages.values():
		server.shutdown()


# This dashboard has no hairline-divided columns of its own.
DIVIDED = dict(spacing_check.DIVIDED)


def wrapped_tiles(browser, base, shot=None):
	"""At a narrow width the tiles wrap: a tile opening a row drops its
	divider and keeps its indent, rows are --space-3 apart, and the dividers
	that remain pad evenly."""

	page = browser.new_page(viewport={'width': 560, 'height': 900})
	page.goto(base + 'index.html')
	page.wait_for_selector('#main-chart .main-svg')
	page.wait_for_timeout(300)
	out = page.evaluate('''() => {
		const tiles = [...document.querySelectorAll('#tiles .tile')];
		const tops = [...new Set(tiles.map(t => Math.round(t.getBoundingClientRect().top)))];
		const starts = tiles.filter(t => t.dataset.rowStart === 'true');
		const content = (t) => { const r = t.getBoundingClientRect(), s = getComputedStyle(t);
			return Math.round(r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft)); };
		const clear = (t) => /rgba\\([^)]*,\\s*0\\)$/.test(getComputedStyle(t).borderLeftColor);
		const rows = tops.map(top => tiles.filter(t => Math.round(t.getBoundingClientRect().top) === top));
		const gaps = rows.slice(1).map((row, i) => Math.round(row[0].getBoundingClientRect().top
			- Math.max(...rows[i].map(t => t.getBoundingClientRect().bottom))));
		return { rows: tops.length, starts: starts.length,
			clear: starts.every(clear), columns: rows.slice(1).every(r => content(r[0])
				=== content(rows[0][0])), gaps }; }''')
	out['dividers'] = spacing_check.divider_violations(page, '.autoopt-root',
		DIVIDED)
	out['spacing'] = spacing_check.spacing_violations(page, '.autoopt-root')
	if shot:
		page.screenshot(path=shot, full_page=True)
	page.close()

	return out


def check_spacing(page):
	"""The spacing rules, measured on the rendered page."""

	for label, found in (
		('text keeps 12px from vertical and 8px from horizontal lines',
			spacing_check.spacing_violations(page, '.autoopt-root')),
		('hairline-divided columns pad 12px on both sides (16 for the sidebar)',
			spacing_check.divider_violations(page, '.autoopt-root', DIVIDED)),
		('section, head and title gaps are the scale values',
			spacing_check.rhythm_violations(page, '.autoopt-root')),
		('no axis title meets a tick label',
			spacing_check.axis_title_collisions(page, '.autoopt-root')),
		('log axes grid decades (or 1-2-5) only; no tick labels overlap',
			spacing_check.log_axis_violations(page, '.autoopt-root'))):
		check(label, not found, '{} found: {}'.format(len(found), found[:6]))


def lineage(by_id, identifier):
	path, at = [], identifier
	while at is not None and at in by_id and len(path) < 200:
		path.insert(0, at)
		at = by_id[at]['parent']
	return path


###
# --scale
###

def run_scale(base, snapshot, out, args):
	"""What breaks only at a real search's size: a row of 2,600
	nodes forked from one parent was 119,000 px wide with the root off the edge, and no error."""

	from playwright.sync_api import sync_playwright

	errors = []
	settled = [r for r in snapshot['iterations'] if r['status'] != 'running']
	print('\n{} iterations, {} runs'.format(len(settled),
		snapshot['counts']['runs']))

	size = os.path.getsize(os.path.join(out, 'state.json')) / 1e6
	check('the payload stays under 6 MB', size < 6.0, '{:.1f} MB'.format(size))

	with sync_playwright() as p:
		browser = p.chromium.launch(headless=not args.headed)
		page = new_page(browser, errors, height=1200)

		# A new Chromium process stalls on its first 2D-canvas paint (about
		# 20 s on a GPU host; a blank page shows it too). The page's first
		# resolved() pays it, so it is paid here, outside the clock, which is
		# for the page's own work.
		warm = time.time()
		page.goto('about:blank')
		page.evaluate('''() => { const c = document.createElement('canvas')
			.getContext('2d', { willReadFrequently: true });
			c.fillRect(0, 0, 1, 1); return c.getImageData(0, 0, 1, 1).data[3]; }''')
		print('browser canvas warm-up {:.1f} s, not counted'.format(
			time.time() - warm))

		started = time.time()
		page.goto(base + 'index.html')
		page.wait_for_selector('#main-chart .main-svg', timeout=120000)
		page.wait_for_selector('#tree-holder svg', timeout=120000)
		elapsed = time.time() - started
		print('first render {:.1f} s'.format(elapsed))
		page.wait_for_timeout(1500)

		check('first render under 20 s', elapsed < 20.0, '{:.1f} s'.format(elapsed))
		equal('every settled iteration is plotted', points_in(page, 'main-chart'),
			expected_points(snapshot))
		style = page.eval_on_selector('#main-chart', '''n => {
			const t = n.data.find((t) => t.marker && Array.isArray(t.marker.size));
			const i = t.customdata.findIndex((id) => !autooptDashboard.state
				.snapshot.iterations.find((r) => r.id === id).on_frontier);
			return [t.marker.size[i], t.marker.opacity[i]]; }''')
		equal('past 500 points the medians are size 5 at opacity 0.3', style,
			[5, 0.3])

		drawn = len(page.query_selector_all('#tree-holder .lineage-node:not('
			'.lineage-tally)'))
		check('the tree is condensed, not drawn in full', drawn < 120,
			'{} nodes drawn'.format(drawn))
		check('tallies stand in for the rest', len(page.query_selector_all(
			'#tree-holder .lineage-tally')) >= 1)
		check('and the panel says so', 'Condensed' in page.inner_text('#tree-note'))
		tally = page.query_selector('#tree-holder .lineage-tally rect')
		tally.scroll_into_view_if_needed()
		box = tally.bounding_box()
		# The centre of the rect, clear of its "+N" text: an unfilled rect
		# takes no pointer events inside it and its tooltip never shows.
		page.mouse.move(box['x'] + 4, box['y'] + box['height'] / 2)
		page.wait_for_timeout(300)
		check('a tally\'s tooltip gives the best value among its iterations',
			'best value' in page.inner_text('#tip'), page.inner_text('#tip'))
		page.mouse.move(5, 5)

		svg = float(page.get_attribute('#tree-holder svg', 'width'))
		pane = page.eval_on_selector('.lineage-holder', 'n => n.clientWidth')
		check('the tree stays within a few screens', svg <= pane * 3,
			'{:.0f} px of tree in a {} px panel'.format(svg, pane))
		viewport = page.eval_on_selector('.lineage-holder', 'n => [n.clientHeight, '
			'getComputedStyle(n).maxHeight, getComputedStyle(n).overflowX]')
		check('the tree scrolls in a 430 px viewport', viewport[0] <= 430
			and viewport[1] == '430px' and viewport[2] == 'auto', viewport)

		root = page.query_selector('#tree-holder circle.dot[r="9"]')
		holder = page.query_selector('.lineage-holder')
		holder.scroll_into_view_if_needed()
		box, frame = root.bounding_box(), holder.bounding_box()
		check('the root is inside the visible panel', box and frame['x']
			<= box['x'] <= frame['x'] + frame['width'], (box, frame))

		spacing = page.evaluate('''() => {
			const rows = {};
			for (const g of document.querySelectorAll('#tree-holder .lineage-node')) {
				const shape = g.querySelector('circle.dot, rect');
				const x = shape.tagName === 'rect' ? Number(shape.getAttribute('x'))
					+ 18 : Number(shape.getAttribute('cx'));
				const y = shape.tagName === 'rect' ? Number(shape.getAttribute('y'))
					+ 9 : Number(shape.getAttribute('cy'));
				(rows[y] = rows[y] || []).push(x);
			}
			let gap = Infinity;
			for (const xs of Object.values(rows)) {
				xs.sort((a, b) => a - b);
				for (let i = 1; i < xs.length; i++) gap = Math.min(gap, xs[i] - xs[i - 1]);
			}
			return isFinite(gap) ? gap : null; }''')
		check('nodes keep at least 34 px between them', spacing is None
			or spacing >= 33.5, spacing)

		levels = page.query_selector_all('#tree-holder .lineage-level')
		counts = [int(l.query_selector('.n').inner_text().replace(',', ''))
			for l in levels]
		widest = counts.index(max(counts))
		levels[widest].click()
		page.wait_for_timeout(1200)
		check('jumping to a generation puts its nodes in view', page.evaluate('''() => {
			const holder = document.querySelector('.lineage-holder');
			const rect = holder.getBoundingClientRect();
			return [...holder.querySelectorAll('circle.dot')].some((c) => {
				const b = c.getBoundingClientRect();
				return b.left >= rect.left && b.right <= rect.right
					&& b.top >= rect.top && b.bottom <= rect.bottom; }); }'''))

		scroll = page.eval_on_selector('#log', 'n => [n.clientHeight, '
			'n.scrollHeight, getComputedStyle(n.querySelector(".log-header"))'
			'.position]')
		check('the log scrolls in a 480 px viewport under a sticky header',
			scroll[0] <= 480 and scroll[1] > scroll[0] and scroll[2] == 'sticky',
			scroll)

		if args.shot:
			page.screenshot(path=args.shot, full_page=True)

		check('no console errors', not errors, errors[:5])
		browser.close()


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--shot', help='write a full-page screenshot here')
	parser.add_argument('--headed', action='store_true')
	parser.add_argument('--scale', type=int, nargs='?', const=2600,
		help='clone the fixture up to N iterations (default 2600) and run the '
			'checks that only fail at that size')
	args = parser.parse_args(argv)

	work = tempfile.mkdtemp(prefix='autoopt-test-')
	root = make_fixture.build(os.path.join(work, 'fixture'))
	out = os.path.join(work, 'published')

	if args.scale:
		print('cloning up to {} iterations…'.format(args.scale))
		make_fixture.inflate(root, args.scale)

	# One measured iteration whose subagent is still working: its README is
	# not written yet (moved aside, so its files are fresh).
	working_id = max(r['id'] for r in make_fixture_results(root)
		if r.get('status') == 'ok' and r['id'] > 1)
	os.replace(os.path.join(root, 'iterations', str(working_id), 'README.md'),
		os.path.join(root, 'iterations', str(working_id), 'README.pending'))

	# Iteration 3's README runs past the excerpt the payload carries, so the
	# tree node panel can show it fetched the whole file.
	assert working_id != 3
	with open(os.path.join(root, 'iterations', '3', 'README.md'), 'a') as outfile:
		outfile.write('\n## Appendix\n\n' + 'Filler past the excerpt. ' * 40
			+ '\n\n- the last line, `' + PANEL_TAIL + '`\n')

	print('fixture: ' + root)

	worker, server, stdout_run, mount = None, None, None, None

	try:
		if not args.scale:
			print('\npublisher freshness')
			check_reload_chain()
			check_dead_code()
			check_stale_writer(root, work)
			check_pid_reuse(root, work)
			check_slow_hardware(root, work)
			check_one_writer(root, work)
			check_shared_sample(work)
			check_live_samples(root, work)
			check_other_host(root, work)
			check_bad_config(root, work)
			check_unreadable(root, work)
			check_portability(root, work)
			check_paths(root, work)
			check_after_payload(root, work)
			publisher(root, work)

			# A benchmark run of iteration 11, launched detached through a
			# shell that exits at once: the scanner skips its own children, and
			# this test is the process doing the scanning.
			launcher = subprocess.run(['sh', '-c', '{} {} --seed 0 >/dev/null '
				'2>&1 & echo $!'.format(sys.executable, os.path.join(root,
				'iterations', '11', 'worker.py'))], cwd=root, capture_output=True,
				text=True)
			worker = int(launcher.stdout.strip())

			# A benchmark run as run_iteration.py starts one: a relative
			# command line, from inside the iteration, stdout into
			# runs/seed-<n>/log.txt. Only the stdout path names iteration 12.
			twelve = os.path.join(root, 'iterations', '12')
			os.makedirs(os.path.join(twelve, 'runs', 'seed-9'), exist_ok=True)
			with open(os.path.join(root, 'iterations', '11', 'worker.py')) as \
				source, open(os.path.join(twelve, 'worker.py'), 'w') as copy:
				copy.write(source.read())
			launcher = subprocess.run(['sh', '-c', '{} worker.py > {} 2>&1 & '
				'echo $!'.format(sys.executable, os.path.join('runs', 'seed-9',
				'log.txt'))], cwd=twelve, capture_output=True, text=True)
			stdout_run = int(launcher.stdout.strip())

			# The mount script names the search on its command line while
			# `--start` waits for the writer; SELF_MARKERS must keep it out.
			launcher = subprocess.run(['sh', '-c', '{} -c "import time; '
				'time.sleep(600)" autoopt_monitor.py --start {} >/dev/null 2>&1 & '
				'echo $!'.format(sys.executable, root)], capture_output=True,
				text=True)
			mount = int(launcher.stdout.strip())

			# A live lease on GPU 3, read from a private GPU_LEASE_DIR; the
			# real lease directory is untouched.
			leases = os.path.join(work, 'leases')
			os.makedirs(leases)
			with open(os.path.join(leases, 'gpu3.json'), 'w') as outfile:
				json.dump({'user': 'someone', 'note': 'search iteration', 'gpu': 3,
					'pid': os.getpid(), 'expires_at': None,
					'acquired_at': time.time()}, outfile)
			os.environ['GPU_LEASE_DIR'] = leases
			time.sleep(0.5)

		snapshot = publish(root, out)
		resources = json.load(open(os.path.join(out, 'resources.json')))

		if mount:
			pids = {proc['pid'] for proc in resources.get('processes') or []}
			check('the scanner sees the detached worker', worker in pids, pids)
			check('and leaves out the mount script, which names the search',
				mount not in pids, pids)
		server, base = serve(out)

		if args.scale:
			try:
				run_scale(base, snapshot, out, args)
			except Exception as error:
				# A timeout or a missing element is a failed check, not a lost
				# summary: record it and let the run print its count.
				check('the browser checks ran to the end', False, repr(error)[:300])
		else:
			variant(out, 'stale.html', lambda page: page.replace(
				'src="dashboard.js"', 'src="dashboard.js?v=0"'))
			variant(out, 'missing.html', lambda page: page.replace(
				'id="tiles"', 'id="tiles-renamed"'))
			# Clocks of 200 ms over files nothing rewrites: the publisher
			# has stopped, and the page must say so rather than stay green.
			variant(out, 'dead.html', lambda page: page.replace(
				'data-standalone', 'data-standalone data-state-interval="200" '
				'data-resource-interval="200"'))
			exported, _ = autoopt_monitor.export(root, os.path.join(work,
				'export.html'))
			try:
				run(base, snapshot, resources, exported, args)
			except Exception as error:
				# A timeout or a missing element is a failed check, not a lost
				# summary: record it and let the run print its count.
				check('the browser checks ran to the end', False, repr(error)[:300])
			try:
				run_without_validation(work, root, args)
			except Exception as error:
				# A timeout or a missing element is a failed check, not a lost
				# summary: record it and let the run print its count.
				check('the browser checks ran to the end', False, repr(error)[:300])
			try:
				check_readme_handover(root, work, args)
				check_export_markup(root, work, args)
				check_pulse(root, work, args)
				check_gpu_strip(root, work, args)
				check_live_button(root, work, args)
				check_after_page(root, work, args)
			except Exception as error:
				check('the browser checks ran to the end', False, repr(error)[:300])
	finally:
		if server:
			server.shutdown()
		for pid in (worker, stdout_run, mount):
			if pid:
				os.kill(pid, 15)

	passed = sum(1 for _, ok in RESULTS if ok)
	print('{} passed, {} failed'.format(passed, len(RESULTS) - passed))

	return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
	sys.exit(main())
