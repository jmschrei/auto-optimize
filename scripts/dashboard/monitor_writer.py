"""Publish a search's state as JSON files a Jupyter server can serve.

Jupyter serves `/files/` sandboxed, so a page mounted there cannot call back
to the server. A page mounted inside a trusted notebook output is not, and
reads files through Jupyter's authenticated Contents API. This daemon is the
other half: it scans the run and writes the JSON the page reads into a
directory under the Jupyter root.

	python3 monitor_writer.py <work>/autooptimize --out ~/autoopt-monitor/<project>

Nothing here writes to the search directory and nothing here listens on a
port. `autoopt_monitor.py --start` is the usual way to start one.
"""

import io
import os
import re
import sys
import json
import math
import time
import shutil
import signal
import socket
import hashlib
import argparse
import logging
import threading
import contextlib

from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from scan import RunScanner
from gpu_scan import shared_dir


ASSETS = ('dashboard.css', 'dashboard.js', 'index.html')
PAYLOADS = ('state.json', 'resources.json')

# The modules that decide the payload's shape. A writer imported them once,
# so one started before a change keeps publishing the old shape for as long as
# it lives; the mount compares this stamp and replaces a stale writer.
SOURCES = ('scan.py', 'gpu_scan.py', 'monitor_writer.py',
	os.path.join('..', 'autoopt.py'))

logger = logging.getLogger('autoopt-monitor')

# Two hosts can mount one payload directory (a network filesystem), and neither sees the
# other's lock or pid: each would publish its own GPUs into it, and the page
# would flip between them. So a publishing writer leaves a heartbeat beside
# the payload, and a writer on another host with the weaker claim pauses.
HOST = socket.gethostname()
HEARTBEAT = 'writer.host'

# A page that wants live GPU cards touches LIVE every few seconds. While the
# file is younger than LIVE_SECONDS the hardware thread samples every
# LIVE_INTERVAL; when the page stops (a closed or hidden tab, the toggle off)
# the writer is back on its own clock within LIVE_SECONDS.
LIVE = 'live.json'
LIVE_SECONDS = 10.0
LIVE_INTERVAL = 1.0


def _stamp(names, directory):
	stamps = []

	for name in names:
		try:
			stamps.append(int(os.path.getmtime(os.path.join(directory, name))))
		except OSError:
			continue

	return str(max(stamps) if stamps else 0)


def code_version():
	"""A stamp for the scanner's code, so a stale writer can be spotted."""

	return _stamp(SOURCES, HERE)


def assets_version():
	"""A stamp for the front end, so a running writer can refresh it and an
	open page can tell it is older than what is published."""

	return _stamp(ASSETS, os.path.join(HERE, 'static'))


def _round(value, digits=5):
	"""Shrink the payload, keeping five digits that mean something.

	Five significant figures below 1, five decimals at or above it. Plain
	`round(x, 5)` sends every p-value to 0.0; sig-fig rounding above 1 would
	move a unix timestamp by hours.
	"""

	if isinstance(value, float):
		if value != value:
			return None
		if value in (float('inf'), float('-inf')) or value == 0:
			return value

		magnitude = math.floor(math.log10(abs(value)))
		places = digits if magnitude >= 0 else digits - magnitude - 1

		return round(value, min(places, 17))
	if isinstance(value, dict):
		return {key: _round(item, digits) for key, item in value.items()}
	if isinstance(value, (list, tuple)):
		return [_round(item, digits) for item in value]

	return value


def write_json(path, payload):
	"""Atomically, so the page never reads half a file. The temporary name
	is per process and thread: two writers sharing one name interleave
	into a file that is not JSON."""

	tmp = '{}.{}-{}.tmp'.format(path, os.getpid(), threading.get_ident())

	try:
		with open(tmp, 'w') as out:
			json.dump(_round(payload), out, separators=(',', ':'),
				default=lambda value: None)

		os.replace(tmp, path)
	finally:
		if os.path.exists(tmp):
			os.unlink(tmp)

	return os.path.getsize(path)


def claim(out_dir):
	"""An open file holding the lock that makes this process the only writer
	into `out_dir`, or None when another writer holds it. Held for the
	process's life and released by the kernel however it ends, so it cannot
	go stale the way `writer.pid` does. It lives on local memory, not beside
	the payload: locks on a network filesystem stall."""

	try:
		import fcntl
	except ImportError:
		return True

	path = os.path.join(shared_dir(), 'writer-{}.lock'.format(hashlib.sha1(
		os.path.abspath(out_dir).encode()).hexdigest()[:16]))
	handle = open(path, 'a')

	try:
		fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
	except BlockingIOError:
		handle.close()
		return None

	return handle


def install_assets(out_dir):
	"""Copy the front end next to the JSON so `/files/` can serve it.

	Stylesheet and script requests made by the notebook page are same-origin,
	so the sandbox on `/files/` does not apply to them. plotly.min.js comes
	from the installed Python package, so the page needs no CDN.
	"""

	os.makedirs(out_dir, exist_ok=True)

	for name in ASSETS:
		shutil.copy2(os.path.join(HERE, 'static', name),
			os.path.join(out_dir, name))

	target = os.path.join(out_dir, 'plotly.min.js')

	if not os.path.exists(target):
		try:
			import plotly
			source = os.path.join(os.path.dirname(plotly.__file__),
				'package_data', 'plotly.min.js')

			if os.path.exists(source):
				shutil.copy2(source, target)
		except ImportError:
			logger.warning('plotly is not installed; the charts will not '
				'render and the page will say so')


# Payloads now live beside the run they describe, so there is no single
# directory to list to find them. One small file per live writer, here, keeps
# discovery a directory listing. It is read by Python only and never served to
# the browser, so a hidden path is safe for it.
INDEX = '~/.cache/autoopt-monitor'


def index_path(out_dir, legacy=False):
	"""The index entry naming `out_dir`, one file per payload directory.

	The readable slug alone collides (`/a/b-c` and `/a-b/c`), so a hash of
	the whole path follows it. `legacy` is the name before the hash, which
	`forget` also removes."""

	path = os.path.abspath(os.path.expanduser(out_dir))
	slug = re.sub(r'[^A-Za-z0-9._-]+', '-', path.strip(os.sep)).strip('-')

	if not legacy:
		slug += '-' + hashlib.sha1(path.encode()).hexdigest()[:8]

	return os.path.join(os.path.expanduser(INDEX), slug + '.writer')


def index_entries():
	"""Every payload directory an index entry points at, live or not."""

	base = os.path.expanduser(INDEX)
	out = []

	for name in (sorted(os.listdir(base)) if os.path.isdir(base) else []):
		if not name.endswith('.writer'):
			continue

		try:
			with open(os.path.join(base, name)) as infile:
				out.append(infile.readline().strip())
		except OSError:
			continue

	return out


def forget(out_dir):
	"""Drop the index entry for `out_dir`, if any."""

	for legacy in (False, True):
		try:
			os.unlink(index_path(out_dir, legacy))
		except OSError:
			pass


def read_heartbeat(out_dir):
	try:
		with open(os.path.join(out_dir, HEARTBEAT)) as infile:
			beat = json.load(infile)
	except (OSError, ValueError):
		return None

	return beat if isinstance(beat, dict) else None


def live_requested(out_dir, now=None, window=LIVE_SECONDS):
	"""Whether a page asked for live samples within `window` seconds. The
	file's own mtime says when, so no clock but the file system's is used."""

	try:
		age = (now or time.time()) - os.path.getmtime(os.path.join(out_dir, LIVE))
	except OSError:
		return False

	return -window < age < window


def yields_to(mine, other, now, stale):
	"""Whether the writer whose heartbeat would be `mine` leaves the
	directory to the one that wrote `other`, the writer publishing there:
	one on another host, heard from within `stale` seconds. It keeps the
	directory unless this writer sees the search's processes and it does
	not, so the strip ends on the GPUs the search runs on and stays there
	between the search's jobs. The heartbeat file holds one writer, so two
	hosts never both pause."""

	if (not other or other.get('host') in (None, mine['host'])
			or now - (other.get('at') or 0) > stale):
		return False

	return not (mine.get('search') and not other.get('search'))


class _Stop(BaseException):
	"""Raised by the SIGTERM handler. Not an Exception, so the per-tick
	handlers that keep the loop alive do not swallow a stop."""


class Busy(RuntimeError):
	"""Another writer holds `out_dir`'s lock."""


def run(root, out_dir, interval, resource_interval):
	os.makedirs(out_dir, exist_ok=True)

	# Taken before anything is written, so two starts racing past the
	# pidfile check cannot both publish.
	lock = claim(out_dir)
	if lock is None:
		raise Busy('a writer is already publishing into {}'.format(out_dir))

	scanner = RunScanner(root)
	install_assets(out_dir)

	pidfile = os.path.join(out_dir, 'writer.pid')

	# The start time pins the PID to this process: a pidfile outlives a
	# kill -9, and the kernel hands its PID to something else.
	tmp = '{}.{}.tmp'.format(pidfile, os.getpid())
	with open(tmp, 'w') as out:
		out.write('{}\n{}\n{}\n{} {}\n{}\n'.format(os.getpid(), scanner.root,
			code_version(), float(interval), float(resource_interval),
			(process_identity(os.getpid()) or (None, None))[1] or ''))
	os.replace(tmp, pidfile)

	entry = index_path(out_dir)
	os.makedirs(os.path.dirname(entry), exist_ok=True)

	with open(entry, 'w') as out:
		out.write('{}\n'.format(os.path.abspath(out_dir)))

	stopping = threading.Event()
	paused = threading.Event()
	since = time.time()
	stale = max(10.0, 3 * float(resource_interval))

	try:
		# Only the main thread may install a handler; run in a thread (a test
		# server), the daemon thread dies with the process instead. The handler
		# raises, so a stop cuts a long scan or a sleep short rather than waiting
		# for it.
		if threading.current_thread() is threading.main_thread():
			def stop(*args):
				if not stopping.is_set():
					stopping.set()
					raise _Stop()

			for sig in (signal.SIGTERM, signal.SIGINT):
				signal.signal(sig, stop)

		# What the writer reports about itself, in resources.json: the gaps it
		# actually keeps between scans and between hardware samples, which the
		# page's freshness check uses in place of the configured intervals.
		# Each cadence is the longest of the last few gaps: a sample served
		# from nvidia-smi's short cache follows a slow one at once, and the
		# last gap alone would swing between the two.
		status = {'interval': float(interval),
			'resource_interval': float(resource_interval),
			'state_cadence': None, 'resource_cadence': None,
			'scan_error': None, 'scan_error_at': None, 'live': False}
		gaps = {'state': deque(maxlen=5), 'resource': deque(maxlen=5)}

		def lap(kind, gap):
			gaps[kind].append(gap)
			status[kind + '_cadence'] = max(gaps[kind])

		# Hardware sampling runs on its own thread: nvidia-smi on a busy host
		# takes seconds per query, and the scan and a stop must not wait on it.
		def hardware():
			last = None
			scanner.hardware.history_gap = 0.9 * resource_interval

			while not stopping.is_set():
				start = time.time()
				live = live_requested(out_dir, start)
				scanner.hardware.live = live
				status['live'] = live

				try:
					mine = {'host': HOST, 'pid': os.getpid(), 'since': since,
						'search': any(p.get('is_root') for p in
							scanner.hardware.processes())}
					other = read_heartbeat(out_dir)

					if yields_to(mine, other, start, stale):
						if not paused.is_set():
							logger.info('paused: %s publishes this directory',
								other['host'])
						paused.set()
						last = None
					else:
						if paused.is_set():
							logger.info('resumed: no other host publishes '
								'this directory')
						paused.clear()
						write_json(os.path.join(out_dir, HEARTBEAT),
							dict(mine, at=start))
						payload = scanner.hardware.resources()
						if last is not None:
							lap('resource', start - last)
						last = start
						payload['writer'] = dict(status)
						write_json(os.path.join(out_dir, 'resources.json'),
							payload)
				except Exception as error:
					logger.warning('resources: %s', error)

				gap = min(resource_interval, LIVE_INTERVAL) if live else resource_interval
				stopping.wait(max(0.05, start + gap - time.time()))

		threading.Thread(target=hardware, name='hardware', daemon=True).start()

		installed = assets_version()
		last_state = None
		readme_seen = {}
		os.makedirs(os.path.join(out_dir, 'readmes'), exist_ok=True)
		logger.info('publishing %s to %s', scanner.root, out_dir)

		while not stopping.is_set():
			start = time.time()

			# The front end is copied in at start-up; a writer left running
			# across a change to it would otherwise serve the old page
			# indefinitely.
			if assets_version() != installed:
				install_assets(out_dir)
				installed = assets_version()
				logger.info('front end changed; reinstalled it')

			# Another host publishes this directory (the hardware thread
			# decides): scan and write nothing until it stops.
			if paused.is_set():
				last_state = None
				time.sleep(0.5)
				continue

			# autoopt.fail() prints its reason to stderr and raises SystemExit
			# (a config.json that fails validation): caught here, so one bad
			# edit to the search is a published error, not a dead writer.
			said = io.StringIO()

			try:
				with contextlib.redirect_stderr(said):
					snapshot = scanner.snapshot()

				# An open page compares this with the ?v= it was loaded with
				# and asks for the cell to be re-run when they differ.
				snapshot['assets'] = installed
				size = write_json(os.path.join(out_dir, 'state.json'), snapshot)

				# Whole READMEs for the tree's node panel, one small file per
				# iteration, rewritten only when its README.md changes.
				for identifier, (mtime, text) in scanner.readmes(readme_seen).items():
					write_json(os.path.join(out_dir, 'readmes', '%d.json' % identifier),
						{'id': identifier, 'text': text})
					readme_seen[identifier] = mtime

				if last_state is not None:
					lap('state', start - last_state)
				last_state = start
				status['scan_error'] = status['scan_error_at'] = None
				logger.info('state %.0f kB, %d iterations, %d running, '
					'frontier %s', size / 1024, snapshot['counts']['iterations'],
					snapshot['counts']['running'], snapshot['frontier']['value'])
			except (Exception, SystemExit) as error:
				reason = said.getvalue().strip() or '{}: {}'.format(
					type(error).__name__, error)
				status['scan_error'], status['scan_error_at'] = reason, time.time()
				logger.warning('state: %s', reason)
			else:
				if said.getvalue():
					sys.stderr.write(said.getvalue())

			# Slices, not one long sleep: the handler raises out of it, but
			# a stop set from elsewhere is only seen between slices.
			deadline = start + interval

			while not stopping.is_set() and time.time() < deadline:
				time.sleep(min(0.5, max(0.05, deadline - time.time())))
	except _Stop:
		pass
	finally:
		stopping.set()

		# Only our own: removing a successor's pidfile hides a live writer
		# from every later start, which then starts another beside it.
		try:
			with open(pidfile) as infile:
				ours = infile.readline().strip() == str(os.getpid())
			if ours:
				os.unlink(pidfile)
				forget(out_dir)
		except OSError:
			pass

		logger.info('stopped')

		if lock is not True:
			lock.close()


def process_identity(pid):
	"""`(argv, start)` for a running `pid`, or None when there is no such
	process. `start` is an opaque token, equal across calls for one process
	and different for a later one given the same PID. Either is None where
	this platform cannot tell: /proc first, then psutil, then only whether
	the PID exists."""

	try:
		with open('/proc/{}/cmdline'.format(pid), 'rb') as infile:
			argv = [a.decode(errors='replace') for a in
				infile.read().split(b'\0') if a]
		with open('/proc/{}/stat'.format(pid)) as infile:
			start = infile.read().rsplit(')', 1)[1].split()[19]

		return argv, start
	except FileNotFoundError:
		if os.path.isdir('/proc/self'):
			return None
	except (OSError, IndexError):
		pass

	try:
		import psutil
	except ImportError:
		psutil = None

	if psutil is not None:
		try:
			process = psutil.Process(pid)
			return process.cmdline(), repr(process.create_time())
		except psutil.NoSuchProcess:        # a zombie is one too
			return None
		except psutil.Error:
			return None, None

	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return None
	except OSError:
		pass

	return None, None


def writer_info(out_dir):
	"""`{pid, root, code, intervals}` for a live writer publishing into
	`out_dir`, else None; `intervals` is `[interval, resource_interval]`.

	The run is part of the answer: a live writer watching a different run is
	the one case that silently shows the wrong numbers under the right name.
	A writer that predates the code stamp reports none, which reads as stale.

	The PID must still be a writer: its command line names
	`monitor_writer.py` and its start time matches the one recorded, where
	the platform can tell. Otherwise a stale pidfile would have `--stop` and
	a handover SIGTERM whatever process now holds the PID.
	"""

	try:
		with open(os.path.join(out_dir, 'writer.pid')) as infile:
			pid = int(infile.readline().strip())
			root = infile.readline().strip()
			code = infile.readline().strip()
			clocks = [float(x) for x in infile.readline().split()] or None
			started = infile.readline().strip() or None
	except (OSError, ValueError):
		return None

	identity = process_identity(pid)

	if identity is None:
		return None

	argv, start = identity

	if argv is not None and not any(os.path.basename(a) == 'monitor_writer.py'
		for a in argv):
		return None

	if started is not None and start is not None and started != start:
		return None

	return {'pid': pid, 'root': root, 'code': code or None, 'intervals': clocks}


def alive(out_dir):
	info = writer_info(out_dir)

	return info['pid'] if info else None


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('root', help='the autooptimize/ directory to watch')
	parser.add_argument('--out', required=True,
		help='where to publish; must be under the Jupyter root')
	parser.add_argument('--interval', type=float, default=20.0,
		help='seconds between full scans (default: %(default)s)')
	parser.add_argument('--resource-interval', type=float, default=3.0,
		help='seconds between hardware samples (default: %(default)s)')
	args = parser.parse_args(argv)

	logging.basicConfig(level=logging.INFO,
		format='%(asctime)s %(levelname)s %(message)s')

	out_dir = os.path.abspath(os.path.expanduser(args.out))
	running = alive(out_dir)

	if running:
		parser.error('a writer is already publishing there (pid {})'.format(
			running))

	try:
		run(args.root, out_dir, args.interval, args.resource_interval)
	except Busy as error:
		parser.error(str(error))


if __name__ == '__main__':
	main()
