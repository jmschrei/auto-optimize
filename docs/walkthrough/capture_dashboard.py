"""Screenshot a search's dashboard, opened in a private JupyterLab.

	python3 capture_dashboard.py WORK OUTDIR --prefix NAME [--diff 3] [--node 3] [--log 3]

WORK is the directory holding autooptimize/ and the dashboard.ipynb that
`autoopt_monitor.py --start` wrote, with its publisher running. A JupyterLab
serving the home directory (as the user's own does, so the notebook's payload
paths resolve the same way) starts with a private token, an empty workspace,
no news prompt and no autosave; the notebook's one cell is run and these are
written:

	<prefix>-lab.png       the browser window: Lab and the top of the page
	<prefix>-frontier.png  the objective chart with its summary band
	<prefix>-diff.png      the code change opened by a click on --diff's point
	<prefix>-tree.png      the search tree with --node's README open
	<prefix>-log.png       --log's entry in the iteration log, opened

Before each picture the home directory is written as ~ and the hostname as
`host`, the left sidebar is closed, and the host and GPU strip is hidden: it
shows this machine's other jobs.
"""

import os
import sys
import json
import time
import socket
import argparse
import subprocess
import urllib.parse
import urllib.request

TOKEN = 'walkthrough-capture'

REDACT = '''([home, host]) => {
	const walk = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
	for (let n = walk.nextNode(); n; n = walk.nextNode()) {
		let t = n.nodeValue;
		if (t.includes(home)) t = t.split(home).join('~');
		if (host && t.includes(host)) t = t.split(host).join('host');
		if (t !== n.nodeValue) n.nodeValue = t;
	}
	const hw = document.getElementById('section-hardware');
	if (hw) hw.style.display = 'none';
}'''

POINT = '''([key, plot]) => {
	const n = document.getElementById(plot);
	const groups = n.querySelectorAll('.scatterlayer .trace');
	for (let t = 0; t < n.data.length; t++) {
		const i = (n.data[t].customdata || []).findIndex((c) => String(c) === key);
		if (i < 0) continue;
		const r = groups[t].querySelectorAll('path.point')[i].getBoundingClientRect();
		return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
	}
	return null; }'''

SETTINGS = (
	('notebook-extension', 'tracker', {'windowingMode': 'none'}),
	('apputils-extension', 'themes', {'theme': 'JupyterLab Dark'}),
	('apputils-extension', 'notification', {'fetchNews': 'false',
		'checkForUpdates': False}),
	('docmanager-extension', 'plugin', {'autosave': False}),
)


def free_port():
	with socket.socket() as sock:
		sock.bind(('127.0.0.1', 0))
		return sock.getsockname()[1]


def start_lab(root, port, scratch):
	settings = os.path.join(scratch, 'lab-settings')
	for package, name, body in SETTINGS:
		folder = os.path.join(settings, '@jupyterlab', package)
		os.makedirs(folder, exist_ok=True)
		with open(os.path.join(folder, name + '.jupyterlab-settings'), 'w') as out:
			json.dump(body, out)

	log = open(os.path.join(scratch, 'lab.log'), 'w')
	process = subprocess.Popen(['jupyter', 'lab', '--no-browser',
		'--ServerApp.ip=127.0.0.1', '--ServerApp.port={}'.format(port),
		'--ServerApp.root_dir={}'.format(root),
		'--IdentityProvider.token={}'.format(TOKEN)], stdout=log, stderr=log,
		stdin=subprocess.DEVNULL, start_new_session=True,
		env=dict(os.environ, JUPYTERLAB_SETTINGS_DIR=settings,
			JUPYTERLAB_WORKSPACES_DIR=os.path.join(scratch, 'workspaces')))

	for _ in range(120):
		try:
			urllib.request.urlopen('http://127.0.0.1:{}/api/status?token={}'.format(
				port, TOKEN), timeout=2)
			return process
		except Exception:
			if process.poll() is not None:
				raise RuntimeError('jupyter lab exited; see lab.log')
			time.sleep(1)

	raise RuntimeError('jupyter lab did not come up')


def main():
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('work')
	parser.add_argument('out')
	parser.add_argument('--prefix', required=True)
	parser.add_argument('--diff', type=int)
	parser.add_argument('--node', type=int)
	parser.add_argument('--log', type=int)
	parser.add_argument('--scale', type=float, default=2)
	args = parser.parse_args()

	work, out = os.path.abspath(args.work), os.path.abspath(args.out)
	home, host = os.path.expanduser('~'), socket.getfqdn()
	notebook = os.path.join(work, 'dashboard.ipynb')
	if not os.path.isfile(notebook) or not work.startswith(home + os.sep):
		raise SystemExit('need {} under {}'.format(notebook, home))

	scratch = os.path.join(out, '.lab-' + args.prefix)
	os.makedirs(scratch, exist_ok=True)
	port = free_port()
	lab = start_lab(home, port, scratch)
	path = lambda name: os.path.join(out, '{}-{}.png'.format(args.prefix, name))
	relative = urllib.parse.quote(os.path.relpath(notebook, home))

	from playwright.sync_api import sync_playwright

	try:
		with sync_playwright() as p:
			browser = p.chromium.launch()
			page = browser.new_page(viewport={'width': 1500, 'height': 980},
				device_scale_factor=args.scale)
			page.goto('http://127.0.0.1:{}/lab/workspaces/capture/tree/{}?reset&token={}'
				.format(port, relative, TOKEN))
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
			page.wait_for_selector('.autoopt-root #main-chart .main-svg',
				timeout=120000)
			page.wait_for_timeout(6000)
			page.keyboard.press('Escape')
			if page.is_visible('#jp-left-stack'):
				page.keyboard.press('Control+b')
			page.wait_for_timeout(800)

			def redact():
				page.evaluate(REDACT, [home, host])
				page.wait_for_timeout(300)

			page.evaluate('''() => { for (const n of document.querySelectorAll(
				'.jp-WindowedPanel-outer, .jp-Notebook')) n.scrollTop = 0; }''')
			page.wait_for_timeout(500)
			redact()
			page.screenshot(path=path('lab'))

			# Element pictures in a window large enough to hold each element.
			page.set_viewport_size({'width': 1900, 'height': 2600})
			page.wait_for_timeout(1500)
			page.locator('#section-frontier').scroll_into_view_if_needed()
			redact()
			page.locator('#section-frontier').screenshot(path=path('frontier'))

			if args.diff is not None:
				page.eval_on_selector('#main-chart',
					'n => n.scrollIntoView({block: "center"})')
				page.wait_for_timeout(400)
				at = page.evaluate(POINT, [str(args.diff), 'main-chart'])
				if at is None:
					raise RuntimeError('no point for iteration {}'.format(args.diff))
				page.mouse.click(at['x'], at['y'])
				page.wait_for_selector('#diff-card:not([hidden]) .diff-table',
					timeout=10000)
				page.mouse.move(5, 5)
				page.wait_for_timeout(600)
				redact()
				page.locator('#diff-card').screenshot(path=path('diff'))
				page.click('#diff-close')

			if args.node is not None:
				node = '#tree-holder .lineage-node[data-node-id="{}"]'.format(
					args.node)
				page.locator(node).scroll_into_view_if_needed()
				page.click(node)
				page.wait_for_timeout(800)
				page.mouse.move(5, 5)
				page.locator('#section-tree').scroll_into_view_if_needed()
				redact()
				page.locator('#section-tree').screenshot(path=path('tree'))

			if args.log is not None:
				entry = '#log .log-entry[data-id="{}"]'.format(args.log)
				page.locator(entry).scroll_into_view_if_needed()
				page.locator(entry).click()
				page.wait_for_timeout(800)
				page.mouse.move(5, 5)
				page.locator(entry).scroll_into_view_if_needed()
				redact()
				page.locator(entry).screenshot(path=path('log'))

			browser.close()
	finally:
		os.killpg(lab.pid, 15)

	for name in ('lab', 'frontier', 'diff', 'tree', 'log'):
		if os.path.exists(path(name)):
			print(path(name))


if __name__ == '__main__':
	sys.exit(main())
