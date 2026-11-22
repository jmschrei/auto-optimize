"""Render a tmux capture (capture-pane -e) to a PNG through headless Chromium.

	python3 render_ansi.py pane.ansi out.png [--stop-after-rules 2]

The walkthrough's prompt picture came from Claude Code started in a detached
tmux pane, the prompt typed and never submitted, all in one shell:

	cd examples/one-hot
	tmux new-session -d -s shot -x 100 -y 30 -c "$PWD" claude
	# wait for the input box, then:
	tmux send-keys -t shot -l 'Use auto-optimize-code to make ...'
	tmux capture-pane -t shot -p -e > pane.ansi
	tmux kill-session -t shot

Keeps the lines up to and including the Nth horizontal rule, folds runs of
blank lines into one, and draws them on a dark terminal background.
"""

import re
import sys
import html
import argparse

SGR = re.compile(r'\x1b\[([0-9;]*)m')
BASIC = ['#1e1e2e', '#f38ba8', '#a6e3a1', '#f9e2af', '#89b4fa', '#f5c2e7',
	'#94e2d5', '#cdd6f4']


def xterm(n):
	if n < 8:
		return BASIC[n]
	if n < 16:
		return BASIC[n - 8]
	if n < 232:
		n -= 16
		steps = [0, 95, 135, 175, 215, 255]
		return '#{:02x}{:02x}{:02x}'.format(steps[n // 36], steps[n // 6 % 6],
			steps[n % 6])
	level = 8 + 10 * (n - 232)
	return '#{0:02x}{0:02x}{0:02x}'.format(level)


def to_html(line):
	out, style, pos = [], {}, 0

	def flush(text):
		if not text:
			return
		css = ';'.join('{}:{}'.format(k, v) for k, v in style.items())
		out.append('<span style="{}">{}</span>'.format(css, html.escape(text))
			if css else html.escape(text))

	for match in SGR.finditer(line):
		flush(line[pos:match.start()])
		pos = match.end()
		codes = [int(c) if c else 0 for c in match.group(1).split(';')]
		i = 0
		while i < len(codes):
			c = codes[i]
			if c == 0:
				style = {}
			elif c == 1:
				style['font-weight'] = '700'
			elif c == 2:
				style['opacity'] = '0.7'
			elif 30 <= c <= 37:
				style['color'] = BASIC[c - 30]
			elif c == 39:
				style.pop('color', None)
			elif 40 <= c <= 47:
				style['background'] = BASIC[c - 40]
			elif c == 49:
				style.pop('background', None)
			elif c in (38, 48) and i + 2 < len(codes) and codes[i + 1] == 5:
				style['color' if c == 38 else 'background'] = xterm(codes[i + 2])
				i += 2
			elif c in (38, 48) and i + 4 < len(codes) and codes[i + 1] == 2:
				style['color' if c == 38 else 'background'] = '#{:02x}{:02x}{:02x}'.format(
					*codes[i + 2:i + 5])
				i += 4
			i += 1

	flush(line[pos:])
	return ''.join(out)


def main():
	parser = argparse.ArgumentParser()
	parser.add_argument('pane')
	parser.add_argument('out')
	parser.add_argument('--stop-after-rules', type=int, default=2)
	args = parser.parse_args()

	lines, rules, blank = [], 0, False
	for line in open(args.pane, encoding='utf-8').read().splitlines():
		plain = SGR.sub('', line)
		if not plain.strip():
			if not blank and lines:
				lines.append('')
			blank = True
			continue
		blank = False
		lines.append(line.rstrip())
		if '─' * 20 in plain:
			rules += 1
			if rules == args.stop_after_rules:
				break

	body = '\n'.join(to_html(line) for line in lines)
	page = ('<!doctype html><meta charset="utf-8"><style>'
		'body{margin:0;background:#11111b}'
		'pre{margin:0;display:inline-block;padding:18px 22px;background:#1e1e2e;'
		'color:#cdd6f4;font:15px/1.35 "DejaVu Sans Mono",monospace;'
		'border-radius:8px}</style><pre id="t">' + body + '</pre>')

	from playwright.sync_api import sync_playwright
	with sync_playwright() as p:
		browser = p.chromium.launch()
		tab = browser.new_page(device_scale_factor=2, viewport={'width': 1100,
			'height': 400})
		tab.set_content(page)
		tab.locator('#t').screenshot(path=args.out)
		browser.close()


if __name__ == '__main__':
	sys.exit(main())
