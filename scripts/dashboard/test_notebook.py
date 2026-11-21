"""Run the monitor in a real JupyterLab and check what rendered.

	python3 test_notebook.py            # fixture, private server, all checks
	python3 test_notebook.py --keep     # leave the server up to look at

`test_frontend.py` covers the page over plain HTTP. This covers the mount
people use: the bootstrap injected into a notebook output, assets from
`/files/`, the payload through the authenticated Contents API, sticky chrome
inside Lab's own scroller, and the notice when the script goes stale.

	python3 test_notebook.py --run <dir> # against a real search, read-only

Starts its own JupyterLab on a free port with its own token and root, and
never touches a server you are already running. Needs playwright with
Chromium, and `jupyter lab`. Scratch directories come from `mktemp` and are
left for the system to clean.
"""

import os
import sys
import json
import time
import socket
import argparse
import tempfile
import subprocess
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import make_fixture
import autoopt_monitor

TOKEN = 'autoopttest'
RESULTS = []


def check(name, condition, detail=''):
	RESULTS.append((name, bool(condition)))
	print('{}  {}{}'.format('PASS' if condition else 'FAIL', name,
		'  -- ' + str(detail) if detail and not condition else ''))


def free_port():
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return sock.getsockname()[1]


def lab_settings(root, theme=None):
	"""A private settings directory with notebook windowing off. Lab's
	default windowing unmounts cells scrolled out of view, which blanks a
	dashboard's output mid-test."""

	path = os.path.join(root, 'lab-settings')
	tracker = os.path.join(path, '@jupyterlab', 'notebook-extension')
	os.makedirs(tracker, exist_ok=True)

	with open(os.path.join(tracker, 'tracker.jupyterlab-settings'), 'w') as out:
		json.dump({'windowingMode': 'none'}, out)

	if theme:
		themes = os.path.join(path, '@jupyterlab', 'apputils-extension')
		os.makedirs(themes, exist_ok=True)
		with open(os.path.join(themes, 'themes.jupyterlab-settings'), 'w') as out:
			json.dump({'theme': theme}, out)

	return path


def start_server(root, port, theme=None):
	log = open(os.path.join(root, 'server.log'), 'w')
	env = dict(os.environ, JUPYTERLAB_SETTINGS_DIR=lab_settings(root, theme))
	process = subprocess.Popen(['jupyter', 'lab', '--no-browser',
		'--ServerApp.ip=127.0.0.1', '--ServerApp.port={}'.format(port),
		'--ServerApp.root_dir={}'.format(root),
		'--IdentityProvider.token={}'.format(TOKEN)], stdout=log, stderr=log,
		stdin=subprocess.DEVNULL, start_new_session=True, env=env)

	for _ in range(120):
		try:
			urllib.request.urlopen('http://127.0.0.1:{}/api/status?token={}'
				.format(port, TOKEN), timeout=2)
			return process
		except Exception:
			if process.poll() is not None:
				raise RuntimeError('jupyter lab exited; see server.log')
			time.sleep(1)

	raise RuntimeError('jupyter lab did not come up')


def drive(url, payload_dir, args_run=None, payload_path=None):
	from playwright.sync_api import sync_playwright

	problems, failed = [], []

	with sync_playwright() as p:
		browser = p.chromium.launch()
		page = browser.new_page(viewport={'width': 1600, 'height': 1100})
		page.on('pageerror', lambda e: problems.append(str(e)))
		page.on('console', lambda m: problems.append(m.text)
			if m.type == 'error' else None)
		page.on('response', lambda r: failed.append((r.status, r.url))
			if r.status >= 400 else None)

		page.goto(url)
		page.wait_for_selector('.jp-Notebook', timeout=90000)
		page.wait_for_timeout(5000)

		for label in ('button:has-text("Select Kernel")',
			'button:has-text("Select")'):
			if page.is_visible(label):
				page.click(label)
				page.wait_for_timeout(2000)
				break

		page.click('.jp-Notebook .jp-CodeCell .jp-InputArea-editor')
		page.keyboard.press('Shift+Enter')

		try:
			page.wait_for_selector('.autoopt-root #main-chart .main-svg',
				timeout=120000)
		except Exception:
			print('output area:', (page.text_content('.jp-OutputArea')
				or '')[:800])
			raise

		page.wait_for_timeout(4000)

		hero = (page.text_content('.autoopt-root #hero-value') or '').strip()
		check('mounted and read state.json through the Contents API',
			hero not in ('', '–'), hero)
		check('the stylesheet loaded from /files/', page.eval_on_selector(
			'.autoopt-root', 'n => getComputedStyle(n).backgroundColor')
			== 'rgb(15, 13, 23)')
		check('resources.json read: GPU cards or none on this machine',
			page.eval_on_selector_all('.autoopt-root .gpu-card', 'n => n.length')
			>= 0 and 'cores' in (page.text_content('#host-summary') or ''))
		check('the pulse is live', page.get_attribute('.autoopt-root #pulse',
			'data-state') == 'live')

		heading = page.eval_on_selector('.autoopt-root h1', 'n => [getComputedStyle(n)'
			'.lineHeight, getComputedStyle(n).fontSize]')
		check("Lab's heading line-height is overridden to 1.3",
			abs(float(heading[0][:-2]) - 1.3 * float(heading[1][:-2])) < 0.5, heading)

		windowing = page.evaluate('''async () => {
			const r = await fetch('/lab/api/settings/@jupyterlab/'
				+ 'notebook-extension:tracker?token=' + %r);
			return (await r.json()).settings.windowingMode; }''' % TOKEN)
		check('the test server runs with notebook windowing off',
			windowing == 'none', windowing)
		check('plotly drew the objective chart', page.eval_on_selector_all(
			'.autoopt-root #main-chart .scatterlayer .trace path.point',
			'n => n.length') >= 1)
		check('the sidebar rendered', page.eval_on_selector_all(
			'.autoopt-root #sidebar .stat-card', 'n => n.length') >= 4)
		check('the lineage tree rendered', page.eval_on_selector_all(
			'.autoopt-root #tree-holder .lineage-node', 'n => n.length') >= 1)
		if not args_run:
			check('the external validation drew its panels', page.eval_on_selector_all(
				'.autoopt-root #validation-charts .scatterlayer path.point',
				'n => n.length') >= 2)

		# Grows with its content: no inner scroller, so the output is as tall
		# as the page inside it.
		heights = page.eval_on_selector('.autoopt-root',
			'n => [n.scrollHeight, n.clientHeight]')
		check('the output grows with its content (no inner scroll)',
			heights[0] <= heights[1] + 1, heights)

		# No rule reaches outside the root class. Lab's output wrappers are
		# overflow auto/hidden, which traps `position: sticky`, so in the
		# notebook the topbar scrolls away with the page by design.
		overflow = page.eval_on_selector('.autoopt-root', '''n => {
			const out = [];
			for (let e = n.parentElement; e && !e.classList.contains('jp-Cell');
				e = e.parentElement) out.push(getComputedStyle(e).overflowY);
			return out; }''')
		check("Lab's output wrappers keep their own overflow",
			'auto' in overflow, overflow)

		# Live: the page's PUT through Jupyter's contents API, with the XSRF
		# token, reaches the publisher, which says it is sampling live.
		page.click('.autoopt-root #toggle-live')
		live = os.path.join(payload_path, 'live.json')
		confirmed = False

		for _ in range(15):
			page.wait_for_timeout(1000)
			try:
				confirmed = json.load(open(os.path.join(payload_path,
					'resources.json'))).get('writer', {}).get('live') is True
			except (OSError, ValueError):
				pass
			if confirmed:
				break

		check('the Live toggle writes live.json through Jupyter and the '
			'publisher confirms', os.path.exists(live) and confirmed, confirmed)
		page.wait_for_timeout(2500)
		check('the button then reads Live · 1 s', page.inner_text(
			'.autoopt-root #toggle-live') == 'Live · 1 s', page.inner_text(
			'.autoopt-root #toggle-live'))
		page.click('.autoopt-root #toggle-live')

		# The script URL's ?v= is fixed when the cell ran, so the page must
		# notice a newer published front end and say so.
		before = page.text_content('.autoopt-root #updated') or ''
		os.utime(os.path.join(HERE, 'static', 'dashboard.js'))

		for _ in range(45):
			page.wait_for_timeout(1000)
			if 're-run' in (page.text_content('.autoopt-root #updated') or ''):
				break

		check('it notices its own script is out of date', 're-run' in
			(page.text_content('.autoopt-root #updated') or ''))
		check('and was not already saying so', 're-run' not in before, before)

		shot = os.path.join(tempfile.gettempdir(), 'autoopt-notebook.png')
		page.screenshot(path=shot)
		print('screenshot: ' + shot)
		browser.close()

	# Lab's own extensions log errors and 404s; only ours count.
	ours = [f for f in failed if payload_dir in f[1]]
	check('no failed request belongs to the dashboard', not ours, ours[:3])
	check('no page error belongs to the dashboard', not [m for m in problems
		if 'dashboard.js' in m or 'startAutoopt' in m or payload_dir in m],
		problems[:3])


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--keep', action='store_true')
	parser.add_argument('--run', help='an autooptimize/ directory to watch '
		'(default: build a fixture)')
	parser.add_argument('--theme', help='a JupyterLab theme by name, such as '
		'"JupyterLab Dark" (default: Lab\'s light theme)')
	args = parser.parse_args(argv)

	# A fresh mkdtemp, left for the system to clean.
	root = tempfile.mkdtemp(prefix='autoopt-jupyter-')
	payload = os.path.join(root, 'monitor-payload')
	search = (autoopt_monitor.check_run(args.run) if args.run
		else make_fixture.build(os.path.join(root, 'fixture')))

	port = free_port()
	process = start_server(root, port, args.theme)

	try:
		# The payload must live under the server's root: the page reads it
		# through that server's Contents API.
		autoopt_monitor.make_notebook(search, path=os.path.join(root,
			'dashboard.ipynb'), out_dir=payload, interval=5.0)
		drive('http://127.0.0.1:{}/lab/tree/dashboard.ipynb?token={}'.format(
			port, TOKEN), 'monitor-payload', args.run, payload)
	finally:
		autoopt_monitor.stop_writer(out_dir=payload)

		if args.keep:
			print('server left on port {} (pid {})'.format(port, process.pid))
		else:
			process.terminate()
			try:
				process.wait(timeout=20)
			except subprocess.TimeoutExpired:
				process.kill()

	passed = sum(1 for _, ok in RESULTS if ok)
	print('{} passed, {} failed'.format(passed, len(RESULTS) - passed))

	return 0 if passed == len(RESULTS) else 1


if __name__ == '__main__':
	sys.exit(main())
