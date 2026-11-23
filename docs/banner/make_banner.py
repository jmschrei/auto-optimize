#!/usr/bin/env python3
"""Draw the README's banner: the dashboard's pink-to-cyan wordmark beside a
stopwatch that has just run down the frontier staircase.

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
WIDTH, HEIGHT = 1280, 360

# The dashboard's palette (scripts/dashboard/static/dashboard.css).
PINK, CYAN, PURPLE = '#ff4fa3', '#00e5ff', '#a06bff'
PINK_MUTED, CYAN_MUTED, PURPLE_MUTED = '#c77a9f', '#5fb8c4', '#9585c4'
INK, DIM, LINE = '#1c1830', '#8e86ad', '#2a2440'


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
	"""The plot behind the stopwatch: faint grid, iterations that were not
	kept above the frontier, and the dotted frontier staircase down to it."""

	grid = ''.join('<line x1="48" x2="540" y1="{0}" y2="{0}"/>'.format(y)
		for y in (92, 150, 208, 266, 324))
	others = [(104, 70, CYAN_MUTED), (136, 56, PURPLE_MUTED), (204, 116,
		PINK_MUTED), (232, 94, CYAN_MUTED), (252, 128, PURPLE_MUTED), (312, 174,
		PURPLE_MUTED), (340, 146, PINK_MUTED), (364, 186, CYAN_MUTED)]
	kept = [(170, 150, CYAN), (280, 208, PURPLE), (390, 266, PINK)]

	return ''.join([
		'<g stroke="#ffffff" stroke-opacity="0.05" stroke-width="1">', grid,
		'</g>',
		''.join('<circle cx="{}" cy="{}" r="6" fill="{}" fill-opacity="0.8"/>'
			.format(*point) for point in others),
		'<path d="M60 92H170V150H280V208H390V266H560" fill="none" stroke="{}" '
		'stroke-width="3" stroke-dasharray="0.1 8" stroke-linecap="round"/>'
		.format(PINK),
		'<circle cx="60" cy="92" r="7.5" fill="#ece8f6"/>',
		''.join('<circle cx="{}" cy="{}" r="7.5" fill="{}"/>'.format(*point)
			for point in kept)])


def sparkle(x, y, r, color):
	"""A four-pointed star centered at (x, y)."""

	t = r * 0.28

	return ('<path d="M{x} {a}Q{x2} {y2} {b} {y}Q{x2} {y3} {x} {c}Q{x1} {y3} '
		'{d} {y}Q{x1} {y2} {x} {a}Z" fill="{color}"/>').format(x=x, y=y,
		a=y - r, b=x + r, c=y + r, d=x - r, x1=x - t, x2=x + t, y2=y - t,
		y3=y + t, color=color)


def stopwatch(cx, cy):
	"""The mascot: a stopwatch with a face, arms up, on the lowest step."""

	r = 54
	ticks = ''.join(
		'<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}"/>'.format(
			cx + dx * 39, cy + dy * 39, cx + dx * 44, cy + dy * 44)
		for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)))

	return ''.join([
		# Legs and feet, behind the body.
		'<g stroke="{}" stroke-width="7" stroke-linecap="round">'.format(DIM),
		'<line x1="{}" y1="{}" x2="{}" y2="{}"/>'.format(cx - 16, cy + 46,
			cx - 19, cy + 60),
		'<line x1="{}" y1="{}" x2="{}" y2="{}"/>'.format(cx + 16, cy + 46,
			cx + 19, cy + 60),
		'</g>',
		'<ellipse cx="{}" cy="{}" rx="12" ry="6" fill="{}"/>'.format(cx - 22,
			cy + 63, DIM),
		'<ellipse cx="{}" cy="{}" rx="12" ry="6" fill="{}"/>'.format(cx + 22,
			cy + 63, DIM),
		# Arms up.
		'<g stroke="url(#ring)" stroke-width="8" stroke-linecap="round" '
		'fill="none">',
		'<path d="M{} {}Q{} {} {} {}"/>'.format(cx - 50, cy + 4, cx - 72,
			cy - 6, cx - 76, cy - 34),
		'<path d="M{} {}Q{} {} {} {}"/>'.format(cx + 50, cy + 4, cx + 72,
			cy - 6, cx + 76, cy - 34),
		'</g>',
		'<circle cx="{}" cy="{}" r="7" fill="{}"/>'.format(cx - 76, cy - 38,
			PINK),
		'<circle cx="{}" cy="{}" r="7" fill="{}"/>'.format(cx + 76, cy - 38,
			CYAN),
		# The crown and the side button.
		'<rect x="{}" y="{}" width="16" height="16" rx="3" fill="url(#ring)"/>'
		.format(cx - 8, cy - r - 13),
		'<rect x="{}" y="{}" width="32" height="10" rx="5" fill="url(#ring)"/>'
		.format(cx - 16, cy - r - 21),
		'<rect x="{}" y="{}" width="14" height="10" rx="3" fill="url(#ring)" '
		'transform="rotate(45 {} {})"/>'.format(cx + 33, cy - 48, cx + 40,
			cy - 43),
		# Body: the gradient ring and a light face.
		'<circle cx="{}" cy="{}" r="{}" fill="url(#ring)"/>'.format(cx, cy, r),
		'<circle cx="{}" cy="{}" r="45" fill="#fbf8ff"/>'.format(cx, cy),
		'<g stroke="#d9d2ee" stroke-width="3" stroke-linecap="round">', ticks,
		'</g>',
		# Face.
		'<ellipse cx="{}" cy="{}" rx="6.5" ry="8.5" fill="{}"/>'.format(cx - 16,
			cy - 6, INK),
		'<ellipse cx="{}" cy="{}" rx="6.5" ry="8.5" fill="{}"/>'.format(cx + 16,
			cy - 6, INK),
		'<circle cx="{}" cy="{}" r="2.6" fill="#ffffff"/>'.format(cx - 14,
			cy - 9),
		'<circle cx="{}" cy="{}" r="2.6" fill="#ffffff"/>'.format(cx + 18,
			cy - 9),
		'<ellipse cx="{}" cy="{}" rx="7.5" ry="4.5" fill="{}" '
		'fill-opacity="0.35"/>'.format(cx - 28, cy + 10, PINK),
		'<ellipse cx="{}" cy="{}" rx="7.5" ry="4.5" fill="{}" '
		'fill-opacity="0.35"/>'.format(cx + 28, cy + 10, PINK),
		'<path d="M{} {}Q{} {} {} {}" fill="none" stroke="{}" '
		'stroke-width="3.5" stroke-linecap="round"/>'.format(cx - 10, cy + 10,
			cx, cy + 21, cx + 10, cy + 10, INK),
		sparkle(cx - 92, cy - 70, 11, CYAN),
		sparkle(cx + 96, cy - 74, 8, PINK),
		sparkle(cx + 104, cy - 40, 5, PURPLE)])


def banner():
	title, title_width = text_path('auto-optimize', font_file(True), 96, 610,
		196, fit=620)
	tagline, _ = text_path('many small experiments, keep what survives',
		font_file(False), 27, 612, 252, fit=title_width)

	return '\n'.join([
		'<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" '
		'viewBox="0 0 {0} {1}" role="img" aria-label="auto-optimize">'.format(
			WIDTH, HEIGHT),
		'<title>auto-optimize</title>',
		'<defs>',
		'<linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
		'<stop offset="0" stop-color="#1c1830"/>'
		'<stop offset="1" stop-color="#110e1a"/></linearGradient>',
		'<linearGradient id="ring" x1="0" y1="0" x2="1" y2="1">'
		'<stop offset="0" stop-color="{}"/><stop offset="1" stop-color="{}"/>'
		'</linearGradient>'.format(PINK, CYAN),
		'<linearGradient id="word" gradientUnits="userSpaceOnUse" x1="610" '
		'y1="120" x2="{:.0f}" y2="150"><stop offset="0" stop-color="{}"/>'
		'<stop offset="1" stop-color="{}"/></linearGradient>'.format(
			610 + title_width, PINK, CYAN),
		'</defs>',
		'<rect x="0.75" y="0.75" width="{}" height="{}" rx="28" fill="url(#bg)" '
		'stroke="{}" stroke-width="1.5"/>'.format(WIDTH - 1.5, HEIGHT - 1.5,
			LINE),
		'<g transform="translate(0 16)">', staircase(), stopwatch(470, 200),
		'</g>',
		'<path d="{}" fill="url(#word)"/>'.format(title),
		'<path d="{}" fill="{}"/>'.format(tagline, DIM),
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
