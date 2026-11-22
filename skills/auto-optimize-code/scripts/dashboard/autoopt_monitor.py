"""Show the auto-optimize-code monitor inside a JupyterLab notebook.

	import autoopt_monitor as am
	am.show('<work>/autooptimize')

The page renders in the notebook's own origin, so it can read its data
through Jupyter's Contents API; a background writer keeps that data fresh.
Nothing is installed into Jupyter and nothing is restarted.

	python3 autoopt_monitor.py <root> --start     # write <work>/dashboard.ipynb, start the writer
	python3 autoopt_monitor.py --status [<root>]  # publishers (of one search): pid, search, out dir
	python3 autoopt_monitor.py --stop [<root>]    # stop one search's publishers, or all
	python3 autoopt_monitor.py <root> --export out.html   # one self-contained, read-only file

`<root>` is a search's `autooptimize/` directory, or the directory holding it;
`<work>` is the second, where the notebook goes.
"""

import os
import re
import sys
import glob
import warnings
import json
import time
import hashlib
import importlib
import subprocess
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))       # autoopt.py
sys.path.insert(0, HERE)

import autoopt
import gpu_scan
import scan
import monitor_writer

# A notebook cell reloads this module and nothing else, so a long-lived kernel
# would keep stale copies of the modules below. Reload them innermost first,
# so each reload binds the fresh copy of the one under it.
for _module in ('autoopt', 'gpu_scan', 'scan', 'monitor_writer'):
	try:
		globals()[_module] = importlib.reload(sys.modules[_module])
	except Exception as _error:          # a broken edit must not hide itself
		print('could not reload {}: {}'.format(_module, _error))

DEFAULT_BASE = '~/autoopt-monitor'
NOTEBOOK = 'dashboard.ipynb'
RUN_MARKER = 'config.json'


def check_run(root):
	"""Resolve a search directory, failing loudly on the usual mistake.

	Accepts the `autooptimize/` directory or the project holding it. Pointing
	at anything else otherwise renders an empty dashboard rather than an
	error.
	"""

	root = os.path.abspath(os.path.expanduser(root))

	if os.path.exists(os.path.join(root, RUN_MARKER)):
		return root

	nested = os.path.join(root, 'autooptimize')

	if os.path.exists(os.path.join(nested, RUN_MARKER)):
		return nested

	raise ValueError('{} has no {}; point this at the autooptimize/ directory '
		'of a search, or at the directory holding it.'.format(root, RUN_MARKER))


def run_slug(root):
	"""The fallback publish directory's name for a search: its project's
	name and a hash of its full resolved path.

	`/.../my-tool/autooptimize` becomes `my-tool-<8 hex>`: the
	search directory is always called `autooptimize`, so the project above it
	is what a reader recognises, and the hash keeps two projects with one
	name from sharing (and each start stealing) the directory.
	"""

	path = os.path.realpath(os.path.abspath(os.path.expanduser(root)))
	parts = [p for p in path.rstrip(os.sep).split(os.sep) if p]
	name = (parts[-2] if len(parts) > 1 and parts[-1] == 'autooptimize'
		else parts[-1] if parts else 'search')
	name = re.sub(r'[^A-Za-z0-9._-]+', '-', name).strip('-') or 'search'

	return '{}-{}'.format(name, hashlib.sha1(path.encode()).hexdigest()[:8])


def default_out_dir(root, base=DEFAULT_BASE):
	"""Where a run publishes: inside the run itself when that works.

	Keeping the payload beside the run means deleting the run deletes its
	monitor with it, rather than orphaning a directory in the home root. Two
	cases cannot do that, and fall back to `base`: a run outside the tree
	Jupyter serves, which the page could not read, and a run directory this
	user cannot write to, such as someone else's.
	"""

	root = os.path.abspath(os.path.expanduser(root))
	served, _ = jupyter_root(under=root)

	if (root == served or root.startswith(served + os.sep)) \
		and os.access(root, os.W_OK):
		return os.path.join(root, 'monitor')

	return os.path.join(os.path.abspath(os.path.expanduser(base)),
		run_slug(root))


def jupyter_servers():
	"""Every Jupyter server this user is running, newest first."""

	# jupyter_core knows the platform's runtime directory (macOS keeps it
	# under ~/Library/Jupyter); the Linux default stands in without it.
	# Its first import warns that it is moving to platformdirs. The warning
	# is ignored rather than JUPYTER_PLATFORM_DIRS set: that would move the
	# directory away from the one the running servers write to.
	try:
		with warnings.catch_warnings():
			warnings.simplefilter('ignore', DeprecationWarning)
			from jupyter_core.paths import jupyter_runtime_dir
		runtimes = [jupyter_runtime_dir()]
	except Exception:
		runtimes = []

	runtimes += [os.path.expanduser('~/.local/share/jupyter/runtime'),
		os.environ.get('JUPYTER_RUNTIME_DIR')]
	paths = [path for runtime in runtimes if runtime
		for path in glob.glob(os.path.join(runtime, 'jpserver-*.json'))]

	servers = []

	for path in sorted(set(paths), key=os.path.getmtime, reverse=True):
		try:
			with open(path) as infile:
				info = json.load(infile)
		except (OSError, ValueError):
			continue

		servers.append((os.path.abspath(os.path.expanduser(
			info.get('root_dir') or '~')), info.get('base_url') or '/'))

	return servers


def jupyter_root(under=None):
	"""The directory the running Jupyter serves, and its base URL.

	With several servers running, prefer the one that serves `under`: the
	newest is not necessarily the one this notebook is open in.
	"""

	servers = jupyter_servers()

	if under:
		under = os.path.abspath(os.path.expanduser(under))

		for root, base in servers:
			if under == root or under.startswith(root + os.sep):
				return root, base

	return servers[0] if servers else (os.path.expanduser('~'), '/')


def writer_processes(any_copy=False):
	"""This user's running `monitor_writer.py` processes from this
	dashboard, found by process rather than by directory: a writer started by
	hand with `--out` anywhere. `[{pid, run, out_dir}]`, from /proc.
	`any_copy` takes a writer from any copy of the script, such as another
	checkout of the skill."""

	script = os.path.realpath(os.path.join(HERE, 'monitor_writer.py'))
	found = []

	for pid, argv, cwd in _own_processes():

		at = [i for i, a in enumerate(argv) if a.endswith('monitor_writer.py')
			and (any_copy or os.path.realpath(os.path.join(cwd, a)) == script)]
		if not at:
			continue

		rest = argv[at[0] + 1:]
		out = rest[rest.index('--out') + 1] if '--out' in rest[:-1] else None
		positional = [a for i, a in enumerate(rest) if not a.startswith('-')
			and (i == 0 or not rest[i - 1].startswith('--'))]

		if out and positional:
			found.append({'pid': int(pid),
				'run': os.path.abspath(os.path.join(cwd, positional[0])),
				'out_dir': os.path.abspath(os.path.join(cwd,
					os.path.expanduser(out)))})

	return found


def _own_processes():
	"""`(pid, argv, cwd)` for this user's processes: from /proc, else from
	psutil, else none, since without either nothing can be found."""

	try:
		pids = [p for p in os.listdir('/proc') if p.isdigit()]
	except OSError:
		pids = None

	if pids is None:
		try:
			import psutil
		except ImportError:
			return

		for proc in psutil.process_iter(['pid', 'uids', 'cmdline', 'cwd']):
			info = proc.info
			if info.get('uids') is not None and info['uids'].real == os.getuid() \
				and info.get('cmdline') and info.get('cwd'):
				yield str(info['pid']), info['cmdline'], info['cwd']

		return

	for pid in pids:
		try:
			if os.stat('/proc/' + pid).st_uid != os.getuid():
				continue
			with open('/proc/{}/cmdline'.format(pid), 'rb') as infile:
				argv = [a.decode(errors='replace') for a in
					infile.read().split(b'\0') if a]
			cwd = os.readlink('/proc/{}/cwd'.format(pid))
		except OSError:
			continue

		yield pid, argv, cwd


def status(base=DEFAULT_BASE):
	"""Every run currently being published, wherever its payload lives.

	Payloads sit beside their runs, so there is no one directory to list. The
	writer index names them. `base` is also scanned, including the base
	itself, for a writer started by hand with `--out` pointing there.
	"""

	base = os.path.abspath(os.path.expanduser(base))

	candidates = list(monitor_writer.index_entries())
	candidates += [base] + [os.path.join(base, name) for name in
		(sorted(os.listdir(base)) if os.path.isdir(base) else [])]

	out, seen = [], set()

	for out_dir in candidates:
		out_dir = os.path.abspath(os.path.expanduser(out_dir))

		if out_dir in seen:
			continue

		seen.add(out_dir)
		info = monitor_writer.writer_info(out_dir)

		if info:
			out.append({'run': info['root'], 'out_dir': out_dir,
				'pid': info['pid']})
		else:
			# The entry outlived its writer: a kill -9, or a reboot.
			monitor_writer.forget(out_dir)

	# And by process: a writer that never reached the index (another copy of
	# the script, an unwritable ~/.cache) is still this user's monitor_writer.py.
	pids = {entry['pid'] for entry in out}
	out += [entry for entry in writer_processes() if entry['pid'] not in pids]

	return out


def _alive(pid):
	"""True while `pid` runs; a zombie (exited, not yet reaped by the
	process that started it) counts as gone."""

	try:
		os.kill(pid, 0)
	except ProcessLookupError:
		return False
	except OSError:
		pass

	try:
		with open('/proc/{}/stat'.format(pid)) as infile:
			return infile.read().rsplit(')', 1)[1].split()[0] != 'Z'
	except FileNotFoundError:
		if os.path.isdir('/proc/self'):
			return False
	except (OSError, IndexError):
		pass

	try:
		import psutil
		return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
	except ImportError:
		return True
	except Exception:
		return False


def _terminate(out_dir, pid):
	"""SIGTERM, then wait up to 10 s for the process to go and its pidfile
	with it."""

	try:
		os.kill(pid, 15)
	except OSError:
		return

	for _ in range(40):
		if not _alive(pid) and (monitor_writer.writer_info(out_dir)
			or {}).get('pid') != pid:
			return

		time.sleep(0.25)


def writers_into(out_dir):
	"""The pid of every writer publishing into `out_dir`: the one its
	pidfile names first, then any other found by process, from any copy of
	the script. A writer whose pidfile a later one overwrote keeps
	publishing, invisible to the pidfile, and racing it over every payload."""

	named = monitor_writer.alive(out_dir)
	others = [entry['pid'] for entry in writer_processes(any_copy=True)
		if entry['out_dir'] == out_dir and entry['pid'] != named]

	return ([named] if named else []) + others


def publishers(root):
	"""Every writer publishing the search `root`, wherever it writes:
	`[{pid, run, out_dir}]`, by process, from any copy of the script."""

	root = os.path.abspath(os.path.expanduser(root))
	found = []

	for entry in writer_processes(any_copy=True):
		try:
			if check_run(entry['run']) == root:
				found.append(entry)
		except ValueError:
			continue

	return found


def start_writer(root, out_dir, interval=20.0, resource_interval=3.0):
	"""Start the publisher unless a current one already serves this run.

	A writer on a different run is replaced, since it would show the wrong
	numbers under the right name. A writer on this run that started before the
	current scanner code is replaced too, since it keeps publishing the
	payload shape it started with. A writer of this run into another
	directory is stopped: a search has one publisher.
	"""

	root = os.path.abspath(os.path.expanduser(root))
	out_dir = os.path.abspath(os.path.expanduser(out_dir))
	os.makedirs(out_dir, exist_ok=True)

	info = monitor_writer.writer_info(out_dir)
	clocks = [float(interval), float(resource_interval)]

	# Only the pidfile's writer may stay; any other was orphaned by a lost
	# pidfile and would publish beside it for as long as it lives.
	extra = [pid for pid in writers_into(out_dir)
		if not info or pid != info['pid']]

	if extra:
		print('Stopping {} other writer{} publishing into {} (pid {}).'.format(
			len(extra), '' if len(extra) == 1 else 's', out_dir,
			', '.join(map(str, extra))))
		for pid in extra:
			_terminate(out_dir, pid)

	for entry in publishers(root):
		if entry['out_dir'] != out_dir:
			print('Stopping the writer publishing {} into {} (pid {}); {} will '
				'publish it.'.format(root, entry['out_dir'], entry['pid'],
					out_dir))
			_terminate(entry['out_dir'], entry['pid'])

	if info and info['root'] == root \
		and info['code'] == monitor_writer.code_version() \
		and info['intervals'] == clocks:
		return info['pid'], False

	if info and info['root'] == root:
		print('Restarting the writer (pid {}): {}.'.format(info['pid'],
			'it predates the current scanner' if info['code']
			!= monitor_writer.code_version() else 'its intervals were {}, not {}'
			.format(info['intervals'], clocks)))
		_terminate(out_dir, info['pid'])

	elif info:
		print('Stopping the writer on {} (pid {}); {} will publish '
			'{}.'.format(info['root'], info['pid'], out_dir, root))
		_terminate(out_dir, info['pid'])

		# Drop the previous search's files, or the wait below would accept
		# them as the new writer's output. Its whole READMEs are moved aside, not
		# deleted: the new writer writes only its own, so any left would
		# show under the new search's ids.
		for name in monitor_writer.PAYLOADS:
			try:
				os.unlink(os.path.join(out_dir, name))
			except OSError:
				pass

		for name in ('readmes', 'diffs'):
			stale = os.path.join(out_dir, name)
			if os.path.isdir(stale):
				os.rename(stale, '{}.{}-{}'.format(stale,
					time.strftime('%Y%m%d-%H%M%S'), os.getpid()))

	log = open(os.path.join(out_dir, 'writer.log'), 'a')
	process = subprocess.Popen([sys.executable,
		os.path.join(HERE, 'monitor_writer.py'), root, '--out', out_dir,
		'--interval', str(interval), '--resource-interval',
		str(resource_interval)], cwd=HERE, stdout=log, stderr=log,
		stdin=subprocess.DEVNULL, start_new_session=True)

	# Wait for the pidfile to name this run, not merely for a state.json to
	# exist: after a handover the previous search's may still be on disk. And for
	# resources.json, which its own thread writes one hardware sample after
	# start (seconds on a busy host): a page mounted before it polls a 404.
	exited = None

	for _ in range(120):
		current = monitor_writer.writer_info(out_dir)

		if current and current['root'] == root and all(os.path.exists(
			os.path.join(out_dir, name)) for name in ('state.json',
			'resources.json')):
			break

		# Ours exiting is a failure, unless a start running at the same
		# moment took the directory's lock first: then its writer is the
		# one to wait for, and it gets 10 s to write its pidfile.
		if process.poll() is not None:
			exited = exited or time.time()
			if time.time() - exited > 10:
				raise RuntimeError('the writer exited; see '
					+ os.path.join(out_dir, 'writer.log'))

		time.sleep(0.5)

	if current and current['pid'] != process.pid:
		return current['pid'], False

	return process.pid, True


def stop_writer(root=None, out_dir=None, base=DEFAULT_BASE):
	"""Stop every publisher watching `root`, the one in `out_dir`, or all.

	Every publisher of the run, not only the default directory's: one
	started by hand elsewhere would keep the page updating from a writer the
	user thinks they stopped.
	"""

	if out_dir is not None:
		targets = [os.path.abspath(os.path.expanduser(out_dir))]
	elif root is not None:
		root = os.path.abspath(os.path.expanduser(root))
		targets = [e['out_dir'] for e in status(base) if e['run'] == root]
	else:
		targets = [e['out_dir'] for e in status(base)]

	stopped = []

	for target in dict.fromkeys(targets):
		for pid in writers_into(target):
			# Wait, as a handover does: `--status` right after must not
			# still list it.
			_terminate(target, pid)
			stopped.append(pid)

	return stopped


def make_notebook(root, path=None, out_dir=None, overwrite=True, **kwargs):
	"""Write a one-cell notebook that renders the monitor for `root`.

	The default is `dashboard.ipynb` in the search's directory, beside
	`autooptimize/`, so the notebook sits with the run it watches. Pass
	`overwrite=False` to keep one the user has edited.
	"""

	root = check_run(root)
	path = os.path.abspath(os.path.expanduser(path
		or os.path.join(os.path.dirname(root), NOTEBOOK)))

	if os.path.exists(path) and not overwrite:
		return path

	# Outside the served tree the file is fine but can never be opened in
	# Lab, which looks like the monitor not working.
	served_root, _ = jupyter_root(under=path)

	if not path.startswith(served_root + os.sep):
		print('warning: {} is outside the directory Jupyter serves ({}), so '
			'it will not appear in the file browser.'.format(path, served_root))

	if out_dir is not None:
		kwargs = dict(kwargs, out_dir=out_dir)

	extra = ''.join(',\n\t{}={!r}'.format(k, v) for k, v in kwargs.items())

	# Reload on every run: the kernel outlives the cell.
	code = ('import sys, importlib\n'
		'sys.path.insert(0, {!r})\n'
		'import autoopt_monitor as am\n'
		'am = importlib.reload(am)\n'
		'\n'
		'am.show({!r}{})\n'.format(HERE, root, extra))

	notebook = {
		# `id` is required from nbformat 4.5; without it every read warns.
		'cells': [{'cell_type': 'code', 'id': 'monitor',
			'execution_count': None, 'metadata': {}, 'outputs': [],
			'source': code.splitlines(keepends=True)}],
		'metadata': {'kernelspec': {'display_name': 'Python 3',
			'language': 'python', 'name': 'python3'},
			'language_info': {'name': 'python'}},
		'nbformat': 4, 'nbformat_minor': 5,
	}

	with open(path, 'w') as out:
		json.dump(notebook, out, indent=1)

	return path


def _body_markup():
	"""The markup, taken from the standalone page so the two never drift."""

	with open(os.path.join(HERE, 'static', 'index.html')) as infile:
		page = infile.read()

	body = re.search(r'<body[^>]*>(.*)</body>', page, re.S).group(1)

	return re.sub(r'<script\b.*?</script>', '', body, flags=re.S).strip()


def show(root, out_dir=None, interval=20.0, resource_interval=3.0,
	writer=True, base=DEFAULT_BASE):
	"""Render the monitor for `root` in this notebook.

	Parameters
	----------
	root: str
		The `autooptimize/` directory to watch, or the directory holding it.

	out_dir: str, optional
		Where the JSON is published. Defaults to `<root>/monitor` when that
		is inside the directory Jupyter serves and writable, else
		`<base>/<project>-<hash>`. Must be inside the directory Jupyter
		serves, because the page reads it through the Contents API.

	interval, resource_interval: float, optional
		Seconds between full scans, and between hardware samples.

	writer: bool, optional
		Start the publisher if it is not running. False renders against data
		someone else is publishing.
	"""

	from IPython.display import display, HTML, Javascript

	root = check_run(root)
	out_dir = os.path.abspath(os.path.expanduser(out_dir
		or default_out_dir(root, base)))

	served_root, base_url = jupyter_root(under=out_dir)

	if not out_dir.startswith(served_root + os.sep):
		raise ValueError('{} is outside the directory Jupyter serves ({}); '
			'the page could not read it. Pass out_dir= somewhere under '
			'{}.'.format(out_dir, served_root, served_root))

	# Install the current front end now: a long-lived writer may be mid-sleep.
	monitor_writer.install_assets(out_dir)

	if writer:
		start_writer(root, out_dir, interval, resource_interval)

	# Encoded per segment: a space, '#' or '?' in a directory name would
	# otherwise end the path inside the /files/ and Contents API URLs.
	relative = '/'.join(urllib.parse.quote(part, safe='') for part in
		os.path.relpath(out_dir, served_root).split(os.sep))
	mount = 'autoopt-{}'.format(int(time.time() * 1000))

	display(HTML('<div id="{}" class="autoopt-root"></div>'.format(mount)))
	display(Javascript(_BOOTSTRAP % {
		'mount': json.dumps(mount),
		'base': json.dumps(base_url if base_url.endswith('/')
			else base_url + '/'),
		'dir': json.dumps(relative),
		'body': json.dumps(_body_markup()),
		'version': json.dumps(monitor_writer.assets_version()),
		'state_interval': int(interval * 1000),
		'resource_interval': int(resource_interval * 1000),
		'writer_log': json.dumps(os.path.join(out_dir, 'writer.log')),
	}))


_BOOTSTRAP = r"""
(async () => {
	const mount = document.getElementById(%(mount)s);
	if (!mount) return;

	const base = %(base)s;
	const dir = %(dir)s;
	const files = base + 'files/' + dir + '/';
	const bust = '?v=' + %(version)s;

	const once = (tag, attrs) => new Promise((resolve, reject) => {
		const key = attrs.href || attrs.src;
		if (document.querySelector(`${tag}[data-autoopt="${key}"]`))
			return resolve();

		const node = document.createElement(tag);
		Object.assign(node, attrs);
		node.dataset.autoopt = key;
		node.onload = resolve;
		node.onerror = () => reject(new Error('could not load ' + key));
		document.head.append(node);
	});

	const contents = async (name) => {
		const url = base + 'api/contents/' + dir + '/' + name
			+ '?content=1&type=file&format=text&_=' + Date.now();
		const response = await fetch(url, {credentials: 'same-origin'});
		if (!response.ok) throw new Error(name + ': ' + response.status);
		return JSON.parse((await response.json()).content);
	};

	// A write the publisher reads only for its age: Jupyter's contents API
	// takes the PUT with the XSRF token it set as a cookie.
	const live = async () => {
		const cookie = document.cookie.split('; ').find((c) => c.startsWith('_xsrf='));
		const response = await fetch(base + 'api/contents/' + dir + '/live.json', {
			method: 'PUT', credentials: 'same-origin',
			headers: {'Content-Type': 'application/json',
				'X-XSRFToken': cookie ? decodeURI(cookie.slice(6)) : ''},
			body: JSON.stringify({type: 'file', format: 'text', content: '{}'})});
		if (!response.ok) throw new Error('live.json: ' + response.status);
	};

	mount.textContent = 'Loading the dashboard…';

	try {
		await once('link', {rel: 'stylesheet', href: files + 'dashboard.css' + bust});
		await once('script', {src: files + 'plotly.min.js'}).catch(() => {});
		// A module, so re-running the cell after an update does not collide
		// with the copy the page already loaded.
		await once('script', {src: files + 'dashboard.js' + bust, type: 'module'});
	} catch (error) {
		mount.textContent = String(error);
		return;
	}

	mount.innerHTML = %(body)s;

	window.startAutoopt(mount, {
		state: () => contents('state.json'),
		resources: () => contents('resources.json'),
		readme: (id) => contents('readmes/' + id + '.json'),
		diff: (id) => contents('diffs/' + id + '.json'),
		live,
	}, {stateInterval: %(state_interval)d,
		resourceInterval: %(resource_interval)d, writerLog: %(writer_log)s});
})();
"""


def export(root, path):
	"""Write one self-contained, read-only HTML file of the search as it is.

	Inlines the stylesheet, plotly, the script and every payload the page
	reads, so the file opens anywhere with no Jupyter and no publisher. The
	page shows "snapshot of <time>" in place of the pulse. Returns the path
	and the size in bytes.
	"""

	root = check_run(root)
	scanner = scan.RunScanner(root)

	snapshot = scanner.snapshot()
	files = {'state.json': snapshot,
		'resources.json': scanner.hardware.resources()}
	for identifier, (mtime, text) in scanner.readmes().items():
		files['readmes/%d.json' % identifier] = {'id': identifier, 'text': text}
	for identifier, (payload, pair) in scanner.diffs(snapshot['iterations'],
			budget=None).items():
		files['diffs/%d.json' % identifier] = payload

	def read(name):
		with open(os.path.join(HERE, 'static', name)) as infile:
			return infile.read()

	# An inline `</script` ends the element early, wherever it appears. In
	# the code it is escaped; the data escapes every `<`, since `<!--` then
	# `<script` in a README would keep `</script>` from closing it at all.
	inline = lambda text: re.sub(r'</(script)', r'<\\/\1', text,
		flags=re.I)

	try:
		import plotly
	except ImportError:
		raise ImportError('--export inlines plotly.min.js from the plotly '
			'package, which is not installed here: pip install plotly') from None

	with open(os.path.join(os.path.dirname(plotly.__file__), 'package_data',
		'plotly.min.js')) as infile:
		plotly_js = infile.read()

	data = json.dumps({'generated': time.time(),
		'files': monitor_writer._round(files)}, separators=(',', ':'),
		default=lambda value: None).replace('<', '\\u003c')

	page = read('index.html').replace(' data-standalone', '')
	page = page.replace('<link rel="stylesheet" href="dashboard.css">',
		'<style>\n' + read('dashboard.css') + '\n</style>')
	page = page.replace('<script src="plotly.min.js"></script>',
		'<script>' + inline(plotly_js) + '</script>')
	page = page.replace('<script type="module" src="dashboard.js"></script>',
		'<script id="autoopt-snapshot" type="application/json">' + data
		+ '</script>\n<script type="module">\n' + inline(read('dashboard.js'))
		+ '\n</script>')

	path = os.path.abspath(os.path.expanduser(path))
	with open(path, 'w') as out:
		out.write(page)

	return path, os.path.getsize(path)


def main(argv=None):
	import argparse

	parser = argparse.ArgumentParser(description='Write the one-cell monitor '
		'notebook for a search, or manage the publishers behind them.')
	parser.add_argument('root', nargs='?', help='the autooptimize/ directory, '
		'or the directory holding it')
	parser.add_argument('-o', '--path', help='where to write the notebook '
		'(default: {} beside autooptimize/)'.format(NOTEBOOK))
	parser.add_argument('--out', help='where to publish the JSON (default: '
		'where this search\'s publisher already writes, else <root>/monitor, '
		'else {}/<project>-<hash>); must be under the directory Jupyter '
		'serves. --start stops a publisher of this search writing elsewhere'
		.format(DEFAULT_BASE))
	parser.add_argument('--no-overwrite', action='store_true',
		help='keep an existing notebook rather than replacing it')
	parser.add_argument('--start', action='store_true', help='also start the '
		'publisher, so the page has data before anyone opens the notebook')
	parser.add_argument('--interval', type=float, default=20.0,
		help='seconds between scans; written into the notebook, and a running '
		'publisher on another interval is restarted (default: 20)')
	parser.add_argument('--status', action='store_true',
		help='list the publishers, only those watching root if given, and exit; '
			'this lists other sessions\' searches too')
	parser.add_argument('--stop', action='store_true',
		help='stop the publishers for root, or every one if root is omitted; '
			'returns once they have exited')
	parser.add_argument('--export', metavar='HTML', help='write one '
		'self-contained, read-only HTML file of root as it is, and exit')
	args = parser.parse_args(argv)

	if args.status:
		wanted = check_run(args.root) if args.root else None

		for entry in status():
			if wanted and entry['run'] != wanted:
				continue
			print('{}\t{}\t{}'.format(entry['pid'], entry['run'],
				entry['out_dir']))

	elif args.stop:
		for pid in stop_writer(check_run(args.root) if args.root else None):
			print('stopped {}'.format(pid))

	elif args.root is None:
		parser.error('a search directory is required')

	elif args.export:
		path, size = export(args.root, args.export)
		print('{}\t{:.1f} MB'.format(path, size / 1024 ** 2))

	else:
		root = check_run(args.root)
		# Without --out, where this search's publisher already writes, so a
		# second --start reports it instead of starting one beside it.
		live = [entry['out_dir'] for entry in publishers(root)]
		default = default_out_dir(root)
		out_dir = os.path.abspath(os.path.expanduser(args.out or (default
			if default in live or not live else live[0])))

		print(make_notebook(root, path=args.path, out_dir=out_dir,
			overwrite=not args.no_overwrite, interval=args.interval))

		if args.start:
			served_root, _ = jupyter_root(under=out_dir)
			if not out_dir.startswith(served_root + os.sep):
				print('warning: {} is outside the directory Jupyter serves '
					'({}); the page will not be able to read it, and show() '
					'will refuse it.'.format(out_dir, served_root))

			pid, started = start_writer(root, out_dir, interval=args.interval)
			print('{} publisher pid {} -> {}'.format('started' if started
				else 'already running', pid, out_dir))

	return 0


if __name__ == '__main__':
	# A wrong directory is a message, not a traceback.
	try:
		sys.exit(main())
	except (ValueError, ImportError) as error:
		print('error: {}'.format(error), file=sys.stderr)
		sys.exit(1)
