"""GPUs, host load, GPU lease holders and the processes that belong to a run.

The hardware half of a monitor's scanner, with no web dependencies. A
dashboard's `RunScanner` owns one `Hardware` and calls `resources()` on the
publisher's fast clock:

	self.hardware = Hardware(root, classify=my_classify, target=my_target)
	payload = self.hardware.resources()        # -> resources.json

`classify(cmdline)` names what a process is doing ('training', 'design',
'scheduler', or 'other') and `target(cmdline, pid)` names the unit of work it
is on (a model, a design, a task id) or returns None; `pid` lets it read what
the command line does not show, such as a shell-redirected stdout
(`stdout_of(pid)`). Both are the only dashboard-specific parts.
"""

import os
import json
import time
import ctypes
import shutil
import socket
import hashlib
import tempfile
import threading
import subprocess

from collections import defaultdict, deque

try:
	import fcntl
except ImportError:
	fcntl = None

try:
	import psutil
except ImportError:
	psutil = None


SHELLS = ('bash', 'sh', 'zsh', 'dash', 'fish', 'ksh')

# The publisher's own processes mention the run directory on their command
# line, so they would otherwise count as part of the run: the writer, the
# scanner, and the mount script (`autoopt_monitor.py` here) while `--start`
# waits for the writer. Keep each entry equal to its script's file name.
SELF_MARKERS = ('monitor_writer.py', 'scan.py', 'autoopt_monitor.py')


def _float(x):
	try:
		x = float(x)
	except (TypeError, ValueError):
		return None

	return None if x != x else x


def _int(x):
	x = _float(x)
	return None if x is None else int(x)


# Seconds one nvidia-smi call may take. Many calls at once on one host take
# over ten seconds each; a call cut short publishes no reading at all.
SMI_TIMEOUT = 30


def nvidia_smi(query, mode='gpu'):
	"""One nvidia-smi CSV query as a list of field lists: [] where there is
	no nvidia-smi, None when the call failed or timed out."""

	if shutil.which('nvidia-smi') is None:
		return []

	flag = '--query-gpu' if mode == 'gpu' else '--query-compute-apps'

	try:
		out = subprocess.run(['nvidia-smi', '{}={}'.format(flag, query),
			'--format=csv,noheader,nounits'], capture_output=True, text=True,
			timeout=SMI_TIMEOUT)
	except (subprocess.SubprocessError, OSError):
		return None

	if out.returncode != 0:
		return None

	return [[field.strip() for field in line.split(',')]
		for line in out.stdout.strip().splitlines() if line.strip()]


def concurrently(*calls):
	"""Each zero-argument call on its own thread; their results, in order,
	None for one that raised. Daemon threads, not an executor: interpreter
	exit joins an executor's workers, so a stopping writer would wait out an
	nvidia-smi call it no longer needs."""

	results = [None] * len(calls)

	def work(index, call):
		try:
			results[index] = call()
		except Exception:
			pass

	threads = [threading.Thread(target=work, args=(index, call), daemon=True)
		for index, call in enumerate(calls)]

	for thread in threads:
		thread.start()
	for thread in threads:
		thread.join()

	return results


def pmon():
	"""Per-process SM and memory utilisation, `{pid: {'sm': %, 'mem': %}}`,
	with the pid as a string so the result survives the shared cache's JSON;
	{} where there is no nvidia-smi, None when the call failed.

	`nvidia-smi pmon` samples for a full second, so callers hold the result.
	"""

	if shutil.which('nvidia-smi') is None:
		return {}

	try:
		out = subprocess.run(['nvidia-smi', 'pmon', '-c', '1'],
			capture_output=True, text=True, timeout=SMI_TIMEOUT)
	except (subprocess.SubprocessError, OSError):
		return None

	if out.returncode != 0:
		return None

	usage = {}

	for line in out.stdout.splitlines():
		if line.startswith('#') or not line.strip():
			continue

		fields = line.split()
		if len(fields) < 5 or not fields[1].isdigit():
			continue

		usage[fields[1]] = {'sm': _float(fields[3]), 'mem': _float(fields[4])}

	return usage


# Each new nvidia-smi process stalls cudaMalloc in every CUDA process on the
# host while it starts, so the GPUs are read through one NVML session held for
# the publisher's life. nvidia-smi is the fallback where the library will not
# load; GPU_SCAN_NVML names another library file (a test's).
NVML_LIBRARY = 'libnvidia-ml.so.1'

# pmon's window: a process's utilisation is its latest sample in the last
# second.
PMON_WINDOW = 1.0

_OK, _NOT_FOUND, _TOO_SMALL = 0, 6, 7
_NOT_AVAILABLE = 2 ** 64 - 1


def _mib(size):
	"""Bytes as MiB rounded up, as nvidia-smi rounds them."""

	return -(-size // 2 ** 20)


class _Utilization(ctypes.Structure):
	_fields_ = [('gpu', ctypes.c_uint), ('memory', ctypes.c_uint)]


class _Memory(ctypes.Structure):
	# nvmlMemory_v2_t: `used` leaves out what the driver reserves, as
	# nvidia-smi's memory.used does.
	_fields_ = [('version', ctypes.c_uint), ('total', ctypes.c_ulonglong),
		('reserved', ctypes.c_ulonglong), ('free', ctypes.c_ulonglong),
		('used', ctypes.c_ulonglong)]


class _Process(ctypes.Structure):
	_fields_ = [('pid', ctypes.c_uint), ('used', ctypes.c_ulonglong),
		('gpu_instance', ctypes.c_uint), ('compute_instance', ctypes.c_uint)]


class _Sample(ctypes.Structure):
	_fields_ = [('pid', ctypes.c_uint), ('time', ctypes.c_ulonglong),
		('sm', ctypes.c_uint), ('memory', ctypes.c_uint),
		('encoder', ctypes.c_uint), ('decoder', ctypes.c_uint)]


class NVML:
	"""The scanner's three GPU queries, answered by one NVML session.

	Each returns what its nvidia-smi counterpart returns: rows, [] where
	there are no GPUs, None when the library did not answer. A field a GPU
	does not report is None, where nvidia-smi prints [N/A].

	Reading the power limit holds every other process's cudaMalloc while it
	runs, so it is read once per session.
	"""

	SYMBOLS = ('nvmlInit_v2', 'nvmlShutdown', 'nvmlDeviceGetCount_v2',
		'nvmlDeviceGetHandleByIndex_v2', 'nvmlDeviceGetUUID',
		'nvmlDeviceGetName', 'nvmlDeviceGetUtilizationRates',
		'nvmlDeviceGetMemoryInfo_v2', 'nvmlDeviceGetTemperature',
		'nvmlDeviceGetPowerUsage', 'nvmlDeviceGetPowerManagementLimit',
		'nvmlDeviceGetComputeRunningProcesses_v3',
		'nvmlDeviceGetProcessUtilization')

	def __init__(self, path):
		self.lib = ctypes.CDLL(path)
		for name in self.SYMBOLS:
			getattr(self.lib, name)

		self.lock = threading.Lock()
		self.ready = False
		self.limits = {}

	def _devices(self):
		"""`[(index, handle, uuid)]`, leaving out a GPU that does not answer;
		None when NVML does not."""

		lib = self.lib

		# Opened again at the next sample after a failure: a session from
		# before a driver reload would otherwise fail for the writer's life.
		with self.lock:
			if not self.ready:
				self.ready = lib.nvmlInit_v2() == _OK
				self.limits = {}
		if not self.ready:
			return None

		count = ctypes.c_uint()
		if lib.nvmlDeviceGetCount_v2(ctypes.byref(count)) != _OK:
			with self.lock:
				if self.ready:
					lib.nvmlShutdown()
					self.ready = False
			return None

		devices = []

		for index in range(count.value):
			handle, uuid = ctypes.c_void_p(), ctypes.create_string_buffer(96)

			if lib.nvmlDeviceGetHandleByIndex_v2(index, ctypes.byref(handle)) \
				== _OK and lib.nvmlDeviceGetUUID(handle, uuid, 96) == _OK:
				devices.append((index, handle, uuid.value.decode()))

		return devices

	def gpus(self):
		"""`--query-gpu` of index, uuid, name, utilization.gpu,
		utilization.memory, memory.used, memory.total (MiB), temperature.gpu,
		power.draw and power.limit (W)."""

		devices = self._devices()
		if devices is None:
			return None

		lib, rows = self.lib, []

		def read(call, *args):
			return call(*args) == _OK

		for index, handle, uuid in devices:
			name = ctypes.create_string_buffer(96)
			use = _Utilization()
			memory = _Memory(version=ctypes.sizeof(_Memory) | 2 << 24)
			temperature, power, limit = (ctypes.c_uint() for _ in range(3))

			if uuid not in self.limits:
				self.limits[uuid] = round(limit.value / 1000, 2) if read(
					lib.nvmlDeviceGetPowerManagementLimit, handle,
					ctypes.byref(limit)) else None

			has_use = read(lib.nvmlDeviceGetUtilizationRates, handle,
				ctypes.byref(use))
			has_memory = read(lib.nvmlDeviceGetMemoryInfo_v2, handle,
				ctypes.byref(memory))

			rows.append([index, uuid,
				name.value.decode() if read(lib.nvmlDeviceGetName, handle, name,
					96) else None,
				use.gpu if has_use else None,
				use.memory if has_use else None,
				_mib(memory.used) if has_memory else None,
				_mib(memory.total) if has_memory else None,
				temperature.value if read(lib.nvmlDeviceGetTemperature, handle, 0,
					ctypes.byref(temperature)) else None,
				round(power.value / 1000, 2) if read(lib.nvmlDeviceGetPowerUsage,
					handle, ctypes.byref(power)) else None,
				self.limits[uuid]])

		return rows

	def apps(self):
		"""`--query-compute-apps` of gpu_uuid, pid and used_memory (MiB)."""

		devices = self._devices()
		if devices is None:
			return None

		rows = []

		for index, handle, uuid in devices:
			size = 64

			while True:
				count, found = ctypes.c_uint(size), (_Process * size)()
				code = self.lib.nvmlDeviceGetComputeRunningProcesses_v3(handle,
					ctypes.byref(count), found)

				if code != _TOO_SMALL or size >= 4096:
					break
				size *= 4

			if code != _OK:
				return None

			rows.extend([uuid, process.pid, None if process.used == _NOT_AVAILABLE
				else _mib(process.used)] for process in found[:count.value])

		return rows

	def usage(self):
		"""`pmon`'s per-process SM and memory utilisation, `{pid: {'sm': %,
		'mem': %}}`, from each process's latest sample in the last
		PMON_WINDOW seconds."""

		devices = self._devices()
		if devices is None:
			return None

		since = ctypes.c_ulonglong(int((time.time() - PMON_WINDOW) * 1e6))
		latest = {}

		for index, handle, uuid in devices:
			count = ctypes.c_uint(0)
			code = self.lib.nvmlDeviceGetProcessUtilization(handle, None,
				ctypes.byref(count), since)

			# NOT_FOUND: no sample in the window. Anything else: not supported.
			if code not in (_OK, _TOO_SMALL) or count.value == 0:
				continue

			count = ctypes.c_uint(count.value + 16)
			samples = (_Sample * count.value)()
			if self.lib.nvmlDeviceGetProcessUtilization(handle, samples,
				ctypes.byref(count), since) != _OK:
				continue

			for sample in samples[:count.value]:
				key = str(sample.pid)
				if key not in latest or sample.time > latest[key][0]:
					latest[key] = (sample.time, {'sm': float(sample.sm),
						'mem': float(sample.memory)})

		return {pid: value for pid, (_, value) in latest.items()}


_nvml, _nvml_lock = None, threading.Lock()


def nvml():
	"""This process's NVML session, None where the library will not load."""

	global _nvml

	with _nvml_lock:
		if _nvml is None:
			try:
				_nvml = NVML(os.environ.get('GPU_SCAN_NVML') or NVML_LIBRARY)
			except (OSError, AttributeError):
				_nvml = False

	return _nvml or None


GPU_QUERY = ('index,uuid,name,utilization.gpu,utilization.memory,memory.used,'
	'memory.total,temperature.gpu,power.draw,power.limit')


def gpu_rows():
	"""`--query-gpu` of GPU_QUERY's fields, one list per GPU."""

	session = nvml()
	return session.gpus() if session else nvidia_smi(GPU_QUERY)


def app_rows():
	"""`--query-compute-apps` of gpu_uuid, pid and used_memory."""

	session = nvml()
	return session.apps() if session else nvidia_smi(
		'gpu_uuid,pid,used_memory', mode='apps')


# Reading per-process utilisation holds every other process's cudaMalloc for
# as long as it runs, a few milliseconds per GPU, so all publishers together
# read it once a minute and show the last answer between.
USAGE_SECONDS = 60


def process_usage():
	"""`pmon`'s per-process utilisation, `{pid: {'sm': %, 'mem': %}}`."""

	session = nvml()
	return session.usage() if session else pmon()


# How old a shared GPU answer may be before a publisher asks again; under the
# 3 s hardware clock, so each poll sees a new sample. An answer is dated when
# its call started, so a slow call does not look fresh at the next tick.
SHARED_SECONDS = 2.5
# How long a publisher waits for another's call before showing the last
# answer: long enough for a normal call, short of a stalled one.
SHARED_WAIT = 1.5


def shared_dir():
	"""This user's directory for what every publisher on the host shares:
	/dev/shm where there is one, since locks on a network filesystem stall."""

	base = '/dev/shm' if os.access('/dev/shm', os.W_OK) else tempfile.gettempdir()
	path = os.path.join(base, 'dashboard-monitor-{}'.format(os.getuid()))
	os.makedirs(path, mode=0o700, exist_ok=True)

	return path


def _read_sample(path):
	try:
		with open(path) as infile:
			sample = json.load(infile)
	except (OSError, ValueError):
		return None

	return sample if isinstance(sample, dict) and 'at' in sample else None


def shared_sample(key, seconds, compute):
	"""`(value, sampled_at)` of `compute()`, run at most once per `seconds` by
	all of this user's publishers on the host together, or `(None, None)`
	before any call has succeeded.

	The GPU queries answer for the whole host, so every dashboard asks the same
	question. Asked separately, a dozen publishers keep forty calls in
	flight and each takes longer than the poll. One publisher refreshes the
	answer under a lock while the rest read the last one. A failed call
	(None) leaves the last good answer in place, so a slow query makes
	the reading older, never absent.
	"""

	if fcntl is None:
		value = compute()
		return (value, time.time()) if value is not None else (None, None)

	# Keyed by the executable too: a test's stub on PATH must not hand its
	# made-up GPUs to the real publishers, nor read theirs.
	directory = shared_dir()
	key += '-' + hashlib.sha1(str(shutil.which('nvidia-smi')).encode()).hexdigest()[:8]
	path = os.path.join(directory, key + '.json')
	sample = _read_sample(path)

	if sample is not None and time.time() - sample['at'] < seconds:
		return sample['value'], sample['at']

	with open(os.path.join(directory, key + '.lock'), 'a') as lock:
		# Someone else is asking: their answer if it lands within
		# SHARED_WAIT, the last one otherwise. With no answer yet there is
		# nothing to show, so wait for it.
		taken = _take(lock, SHARED_WAIT if sample else None)
		latest = _read_sample(path) or sample

		# An answer that landed while this one waited is used whatever its
		# age: a call slower than `seconds` would otherwise be repeated by
		# every publisher that waited on it.
		if not taken or (latest is not None and (time.time() - latest['at']
				< seconds or sample is None or latest['at'] != sample['at'])):
			return latest['value'], latest['at']

		sample = latest

		at = time.time()
		value = compute()

		if value is None:
			return (sample['value'], sample['at']) if sample else (None, None)

		tmp = '{}.{}-{}.tmp'.format(path, os.getpid(), threading.get_ident())
		with open(tmp, 'w') as out:
			json.dump({'at': at, 'value': value}, out)
		os.replace(tmp, path)

		return value, at


def _take(lock, timeout):
	"""Lock `lock`, waiting at most `timeout` seconds (None: as long as it
	takes). Whether it was taken."""

	if timeout is None:
		fcntl.flock(lock, fcntl.LOCK_EX)
		return True

	deadline = time.time() + timeout

	while True:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
			return True
		except BlockingIOError:
			if time.time() >= deadline:
				return False
			time.sleep(0.05)


def leases(directory=None):
	"""Live GPU lease holders, `{index: {user, note, pid, acquired_at}}`.

	Optional: read only when `GPU_LEASE_DIR` (or `directory`) names a
	directory written by a lease tool, one `gpu<index>.json` per held GPU
	with any of `gpu`, `user`, `note` (or `cmd`), `pid`, `acquired_at` and
	`expires_at` (epoch seconds). A lease counts while its pid is alive and
	its expiry has not passed. The files are read, never reaped. No
	directory means no information, not an error.
	"""

	directory = directory or os.environ.get('GPU_LEASE_DIR')
	if not directory:
		return {}
	directory = os.path.expanduser(directory)
	out = {}

	try:
		names = os.listdir(directory)
	except OSError:
		return out

	for name in names:
		if not (name.startswith('gpu') and name.endswith('.json')):
			continue

		try:
			with open(os.path.join(directory, name)) as infile:
				lease = json.load(infile)
		except (OSError, ValueError):
			continue

		# A lease tool's file, not ours: anything of the wrong shape is
		# skipped rather than allowed to stop the hardware sample.
		number = lambda x: isinstance(x, (int, float)) \
			and not isinstance(x, bool)

		if not isinstance(lease, dict):
			continue

		pid, expires = lease.get('pid'), lease.get('expires_at')
		index = lease.get('gpu', _int(name[3:-5]))

		if not (pid is None or (isinstance(pid, int) and pid > 0)) \
			or not (expires is None or number(expires)) \
			or not (isinstance(index, int) and not isinstance(index, bool)):
			continue

		if expires is not None and time.time() > expires:
			continue

		if pid is not None:
			try:
				os.kill(pid, 0)
			except ProcessLookupError:
				continue
			except OSError:
				pass

		out[index] = {'user': lease.get('user'),
			'note': lease.get('note') or lease.get('cmd'),
			'pid': pid, 'acquired_at': lease.get('acquired_at')}

	return out


def stdout_of(pid):
	"""Where a process's stdout goes (`/proc/<pid>/fd/1`), or None. A job
	started as `python train.py > logs/m3.log` names its unit of work only
	there."""

	if pid is None:
		return None

	try:
		return os.readlink('/proc/{}/fd/1'.format(pid))
	except OSError:
		return None


def is_job(argv):
	"""True for a command doing work, not an interactive shell.

	Matching on the working directory alone sweeps up any shell someone has
	cd'd into the run; a shell only counts when it is running a script.
	"""

	if not argv:
		return False

	if os.path.basename(argv[0]) not in SHELLS:
		return True

	return len(argv) > 1 and not argv[1].startswith('-')


class Hardware:
	"""GPU, host and process state for one run directory.

	Parameters
	----------
	root: str
		The run directory. A process belongs to the run when its command line
		names it or its working directory is inside it.

	classify: callable
		`classify(cmdline) -> str`, what a process is doing. 'scheduler' is
		special: workers are never folded into a scheduler.

	target: callable
		`target(cmdline, pid) -> str or None`, the unit of work a process
		is on.

	history: int
		Samples kept for the sparklines.
	"""

	def __init__(self, root, classify=None, target=None, history=180):
		self.root = os.path.abspath(os.path.expanduser(root))
		self.classify = classify or (lambda cmdline: 'other')
		self.target = target or (lambda cmdline, pid: None)
		self.history = deque(maxlen=history)

		self.process_cache = {}
		self.process_ttl = 2.0
		self._processes = (0.0, [])
		self._ttl = {}
		self.gpu_sampled_at = None
		self._history_at = None
		self._history_last = 0.0

		# Set by the writer while a page asks for live cards. NVML answers in
		# about a tenth of a millisecond, so with it a live sample skips the
		# shared cache; without it the shared nvidia-smi cache stays, since
		# every call there costs the host hundreds of milliseconds. Per-process
		# utilisation stays on its own 60 s clock either way.
		self.live = False

		# The least seconds between history points; the writer sets it just
		# under its own clock, so live samples move the cards and not the
		# sparkline's span.
		self.history_gap = 0.0

	def _hold(self, key, seconds, compute):
		"""A value recomputed at most every `seconds`, so several callers in one
		poll do not repeat an expensive query."""

		when, value = self._ttl.get(key, (0.0, None))

		if time.time() - when < seconds:
			return value

		value = compute()
		self._ttl[key] = (time.time(), value)

		return value

	def processes(self):
		"""Every process whose command line or cwd names the run.

		Workers are folded into the job that owns them, so the table has one
		row per job rather than one per dataloader: each record carries `tree`
		(summed CPU, RSS and worker count) and `is_root`.
		"""

		if psutil is None:
			return []

		when, cached = self._processes

		if time.time() - when < self.process_ttl:
			return cached

		fields = ['pid', 'ppid', 'name', 'cmdline', 'create_time',
			'memory_info', 'cwd', 'num_threads', 'status']
		found, seen, mine = {}, set(), {os.getpid()}

		for proc in psutil.process_iter(fields):
			try:
				info = proc.info
				argv = info.get('cmdline') or []
				cmdline = ' '.join(argv)
				cwd = info.get('cwd') or ''
			except (psutil.NoSuchProcess, psutil.AccessDenied):
				continue

			if not cmdline:
				continue

			pid = info['pid']

			if pid in mine or info.get('ppid') in mine:
				mine.add(pid)
				continue

			if any(marker in cmdline for marker in SELF_MARKERS):
				continue

			if not is_job(argv):
				continue

			if not (self.root in cmdline or cwd == self.root
				or cwd.startswith(self.root + os.sep)):
				continue

			seen.add(pid)

			# cpu_percent is measured since the previous call on the same
			# Process object, so the object is kept between scans.
			handle = self.process_cache.get(pid)
			if handle is None:
				handle = self.process_cache[pid] = proc

			try:
				cpu = handle.cpu_percent()
			except (psutil.NoSuchProcess, psutil.AccessDenied):
				cpu = None

			memory = info.get('memory_info')

			found[pid] = {
				'pid': pid, 'ppid': info.get('ppid'),
				'name': info.get('name'), 'cmdline': cmdline,
				'threads': info.get('num_threads'),
				'cpu_percent': cpu, 'rss': getattr(memory, 'rss', None),
				'elapsed': time.time() - (info.get('create_time')
					or time.time()),
				'kind': self.classify(cmdline),
				'target': self.target(cmdline, pid),
			}

		for pid in [p for p in self.process_cache if p not in seen]:
			del self.process_cache[pid]

		# Roll workers up into the process that owns the same job. Grouping on
		# the job, not the whole process tree, keeps a scheduler from
		# swallowing every job it launched.
		for record in found.values():
			root, ppid = record['pid'], record['ppid']

			while ppid in found and found[ppid]['kind'] != 'scheduler':
				parent = found[ppid]

				if record['target'] is not None \
					and parent['target'] not in (None, record['target']):
					break

				root, ppid = ppid, parent['ppid']

			record['root_pid'] = root

		totals = defaultdict(lambda: {'cpu_percent': 0.0, 'rss': 0,
			'workers': 0})
		kinds, targets = {}, {}

		for record in found.values():
			total = totals[record['root_pid']]
			total['cpu_percent'] += record['cpu_percent'] or 0.0
			total['rss'] += record['rss'] or 0
			total['workers'] += 1

			# A wrapper shell says nothing about what it wraps; the work
			# underneath names the group.
			if record['kind'] != 'other':
				kinds.setdefault(record['root_pid'], record['kind'])
			if record['target'] is not None:
				targets.setdefault(record['root_pid'], record['target'])

		for record in found.values():
			root = record['root_pid']
			record['tree'] = dict(totals[root])
			record['is_root'] = root == record['pid']

			if record['is_root']:
				record['kind'] = kinds.get(root, record['kind'])
				record['target'] = record['target'] or targets.get(root)

		records = sorted(found.values(), key=lambda r: r['pid'])
		self._processes = (time.time(), records)

		return records

	def gpus(self):
		"""Per-GPU state with this run's processes and any lease attached.

		`state` is 'ours' when a process of this run is on it, 'leased' when a
		live lease holds it and none of ours is, 'busy' when another job
		is on it, and 'free' otherwise.
		"""

		# Side by side: through nvidia-smi, on a busy 8-GPU host each query
		# takes seconds, and `pmon` samples for one more.
		if self.live and nvml():
			at = time.time()
			rows, apps, self.gpu_sampled_at = gpu_rows(), app_rows(), at
			usage = shared_sample('pmon', USAGE_SECONDS, process_usage)[0]
		else:
			(rows, self.gpu_sampled_at), (apps, _), (usage, _) = (
				result or (None, None) for result in concurrently(
				lambda: shared_sample('smi', SHARED_SECONDS, gpu_rows),
				lambda: shared_sample('apps', SHARED_SECONDS, app_rows),
				lambda: shared_sample('pmon', USAGE_SECONDS, process_usage)))
		rows, apps, usage = rows or [], apps or [], usage or {}

		gpus, by_uuid = [], {}

		for row in rows:
			if len(row) < 10:
				continue

			gpu = {
				'index': _int(row[0]), 'uuid': row[1], 'name': row[2],
				'utilization': _float(row[3]),
				'memory_utilization': _float(row[4]),
				'memory_used': _float(row[5]), 'memory_total': _float(row[6]),
				'temperature': _float(row[7]), 'power': _float(row[8]),
				'power_limit': _float(row[9]), 'processes': [],
			}

			gpus.append(gpu)
			by_uuid[row[1]] = gpu

		ours = {record['pid']: record for record in self.processes()}
		held = self._hold('leases', 2.0, leases)

		for row in apps:
			if len(row) < 3 or row[0] not in by_uuid:
				continue

			pid = _int(row[1])
			record = ours.get(pid)

			by_uuid[row[0]]['processes'].append({
				'pid': pid, 'mine': record is not None,
				'gpu_memory': _float(row[2]),
				'sm': (usage.get(str(pid)) or {}).get('sm'),
				'kind': record['kind'] if record else None,
				'target': record['target'] if record else None,
				'cpu_percent': record['tree']['cpu_percent'] if record else None,
				'rss': record['tree']['rss'] if record else None,
				'workers': record['tree']['workers'] if record else None,
				'elapsed': record['elapsed'] if record else None,
			})

		for gpu in gpus:
			gpu['mine'] = any(p['mine'] for p in gpu['processes'])
			gpu['lease'] = held.get(gpu['index'])
			gpu['state'] = ('ours' if gpu['mine']
				else 'leased' if gpu['lease']
				else 'busy' if gpu['processes'] else 'free')

		return gpus

	def host(self):
		if psutil is None:
			return {}

		memory = psutil.virtual_memory()

		return {'cpu_percent': psutil.cpu_percent(),
			'cpu_count': psutil.cpu_count(), 'load': list(os.getloadavg()),
			'memory_used': memory.used, 'memory_total': memory.total,
			'memory_percent': memory.percent}

	def resources(self, sample=True):
		"""The `resources.json` payload, appending one history sample.

		`history` is per-sample records rather than one series per GPU, so a
		memory or host-CPU sparkline can be added without a payload change.
		"""

		# `psutil` False says why the host summary and the process table
		# are empty, rather than leaving an idle-looking search.
		# `gpu_sampled_at` is when the last GPU query that answered
		# started, which is older than `timestamp` while it is slow or failing.
		state = {'timestamp': time.time(), 'gpus': self.gpus(),
			'gpu_sampled_at': self.gpu_sampled_at,
			'hostname': socket.gethostname(),
			'host': self.host(), 'psutil': psutil is not None,
			'processes': [p for p in self.processes() if p['is_root']]}

		if sample:
			# A reading already in the history is not repeated, or a stalled
			# query would draw a flat line that looks like a steady GPU.
			now = state['timestamp']
			fresh = state['gpus'] if self.gpu_sampled_at != self._history_at else []
			self._history_at = self.gpu_sampled_at

			if now - self._history_last >= self.history_gap:
				self._history_last = now
				self.history.append({
					'timestamp': now,
					'gpu': {g['index']: g['utilization'] for g in fresh},
					'gpu_memory': {g['index']: g['memory_used'] for g in fresh},
					'cpu': state['host'].get('cpu_percent'),
					'memory': state['host'].get('memory_percent'),
				})

		state['history'] = list(self.history)

		return state
