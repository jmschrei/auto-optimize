#!/usr/bin/env python3
"""Draw the README's banner: the dashboard's pink-to-cyan wordmark beside the
frontier staircase of a search.

	python3 docs/banner/make_banner.py            # banner.svg and banner.png here
	python3 docs/banner/make_banner.py --no-png   # the SVG alone

The text is converted to outlines (fontTools, DejaVu Sans, the font the
dashboard's title shows on Linux), so the SVG looks the same on every
machine. The PNG is the SVG rendered by headless Chromium (playwright) at
twice its size; GitHub shows the PNG.
"""

import os
import argparse
import subprocess

from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

HERE = os.path.dirname(os.path.abspath(__file__))
WIDTH, HEIGHT = 1280, 220
# The wordmark's baseline: its letters, with half the weight of the p's
# descender, centered on the height.
BASELINE = 136

# The dashboard's palette (scripts/dashboard/static/dashboard.css).
PINK, CYAN, PURPLE = '#ff4fa3', '#00e5ff', '#a06bff'
PINK_MUTED, CYAN_MUTED, PURPLE_MUTED = '#c77a9f', '#5fb8c4', '#9585c4'
LINE = '#2a2440'


def font_file(bold):
	"""The DejaVu Sans file fontconfig finds."""

	name = 'DejaVu Sans:bold' if bold else 'DejaVu Sans'
	out = subprocess.run(['fc-match', name, 'file'], capture_output=True,
		text=True, check=True).stdout.strip()

	return out.split('=', 1)[1]


def text_path(text, path, size, x, y, fit=None):
	"""SVG path data for `text` set in the font at `path`, its baseline at
	(x, y), and its width; scaled down to `fit` pixels wide when given."""

	font = TTFont(path)
	glyphs = font.getGlyphSet()
	cmap = font.getBestCmap()
	scale = size / font['head'].unitsPerEm
	names = [cmap[ord(character)] for character in text]
	advance = sum(glyphs[name].width for name in names)

	if fit and advance * scale > fit:
		scale = fit / advance

	pen = SVGPathPen(glyphs)
	cursor = 0

	for name in names:
		moved = TransformPen(pen, (scale, 0, 0, -scale, x + cursor * scale, y))
		glyphs[name].draw(moved)
		cursor += glyphs[name].width

	return pen.getCommands(), advance * scale


def staircase():
	"""A faint grid, iterations that were not kept above the frontier, and
	the dotted frontier staircase down through the kept ones."""

	grid = ''.join('<line x1="48" x2="540" y1="{0}" y2="{0}"/>'.format(y)
		for y in (68, 102, 136, 170))
	others = [(104, 55, CYAN_MUTED), (136, 47, PURPLE_MUTED), (204, 82,
		PINK_MUTED), (232, 69, CYAN_MUTED), (252, 89, PURPLE_MUTED), (312, 116,
		PURPLE_MUTED), (340, 100, PINK_MUTED), (364, 123, CYAN_MUTED)]
	kept = [(170, 102, CYAN), (280, 136, PURPLE), (390, 170, PINK)]

	return ''.join([
		'<g stroke="#ffffff" stroke-opacity="0.05" stroke-width="1">', grid,
		'</g>',
		''.join('<circle cx="{}" cy="{}" r="6" fill="{}" fill-opacity="0.8"/>'
			.format(*point) for point in others),
		'<path d="M60 68H170V102H280V136H390V170H540" fill="none" stroke="{}" '
		'stroke-width="3" stroke-dasharray="0.1 8" stroke-linecap="round"/>'
		.format(PINK),
		'<circle cx="60" cy="68" r="7.5" fill="#ece8f6"/>',
		''.join('<circle cx="{}" cy="{}" r="7.5" fill="{}"/>'.format(*point)
			for point in kept)])


def banner():
	title, title_width = text_path('auto-optimize', font_file(True), 96, 610,
		BASELINE, fit=620)

	return '\n'.join([
		'<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" '
		'viewBox="0 0 {0} {1}" role="img" aria-label="auto-optimize">'.format(
			WIDTH, HEIGHT),
		'<title>auto-optimize</title>',
		'<defs>',
		'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
		'<stop offset="0" stop-color="#1c1830"/>'
		'<stop offset="1" stop-color="#110e1a"/></linearGradient>',
		'<linearGradient id="word" gradientUnits="userSpaceOnUse" x1="610" '
		'y1="120" x2="{:.0f}" y2="150"><stop offset="0" stop-color="{}"/>'
		'<stop offset="1" stop-color="{}"/></linearGradient>'.format(
			610 + title_width, PINK, CYAN),
		'</defs>',
		'<rect x="0.75" y="0.75" width="{}" height="{}" rx="28" fill="url(#bg)" '
		'stroke="{}" stroke-width="1.5"/>'.format(WIDTH - 1.5, HEIGHT - 1.5,
			LINE),
		staircase(),
		'<path d="{}" fill="url(#word)"/>'.format(title),
		'</svg>', ''])


def render_png(svg_path, png_path):
	from playwright.sync_api import sync_playwright

	with sync_playwright() as driver:
		browser = driver.chromium.launch()
		page = browser.new_page(viewport={'width': WIDTH, 'height': HEIGHT},
			device_scale_factor=2)
		page.goto('file://' + svg_path)
		page.screenshot(path=png_path, omit_background=True)
		browser.close()


def main():
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--out', default=HERE, help='the folder for '
		'banner.svg and banner.png (default: this one)')
	parser.add_argument('--no-png', action='store_true', help='write the SVG '
		'only')
	args = parser.parse_args()

	svg_path = os.path.abspath(os.path.join(args.out, 'banner.svg'))

	with open(svg_path, 'w', encoding='utf-8') as handle:
		handle.write(banner())

	print('wrote', svg_path)

	if not args.no_png:
		png_path = os.path.join(os.path.dirname(svg_path), 'banner.png')
		render_png(svg_path, png_path)
		print('wrote', png_path)


if __name__ == '__main__':
	main()
