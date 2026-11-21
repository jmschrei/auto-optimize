"""Geometry checks for the spacing rules, for a Playwright page.

	from spacing_check import spacing_violations, rhythm_violations, \
		axis_title_collisions
	bad = spacing_violations(page, '.dash-root')     # [] when the page is clean

The rules are the ones listed below. Every function returns a list of
human-readable violations, empty when the page follows the rules.

What is measured, and what is exempt:

- `spacing_violations`: with the page scrolled to the top, every visible
  text run inside the root (clipped by its scrolling and overflow-hidden
  ancestors, so elided text counts as drawn), against
  every visible border (hairline, rule, a log entry's left rule) of any
  element in the root. Horizontally a text run keeps `h` px (12) from a
  vertical line whose height it overlaps; vertically `v` px (8) from a
  horizontal line whose width it overlaps, or `v_list` px (4) inside a list
  row (table cells, log entries, stat rows, model rows, bar rows, hierarchy
  rows), where the rule separates rows of values. Exempt: text inside
  controls (buttons, chips, inputs, selects, badges, grips, a checkbox's
  box), legend keys (`.swatch`, `.mark-key`), anything inside a closed
  `<details>` other than its summary, inside SVG, canvas and Plotly plots and their modebar (their own
  check below), the tooltip, the dashed empty-state box and anything
  `[hidden]`; borders of those elements do not count either. Transparent
  borders do not count.
- `divider_violations`: an element matching a key of `DIVIDED` (add a
  dashboard's own hairline-divided instruments to it; nothing else is
  checked) with a visible left border
  pads the same on both sides of it: its content box starts `DIVIDED[sel]`
  px after the line and the previous sibling's content box ends as many px
  before it, unless that sibling is on another row (wrapped tiles) (the
  previous sibling of its parent when it is the first child, as for the
  first summary tile after the hero).
- `rhythm_violations`: section head -> next block is `head_gap` (12);
  consecutive sections in `main` are `section_gap` (24) apart; a figure
  caption -> the next block (a headline or the plot) and a headline -> the
  plot are `title_gap` (8).
- `axis_title_collisions`: in every Plotly plot, no axis title's box meets a
  tick label's box.
- `log_axis_violations`: every Plotly log axis has at most one gridline per
  decade (three when it spans two decades or less), draws no value outside
  its range (pass logTicks every value any trace draws on the axis), and no
  axis has overlapping tick labels.
"""

# Hairline-divided instruments and the padding each keeps on both sides of
# its divider: --space-3, and --space-4 for the sidebar's gutter.
DIVIDED = {'.gpu-card': 12, '.tile': 12, '.group-col': 12, 'aside': 16,
	'.browser-models': 12, '.lineage-map': 12}

# Legend keys (.swatch, .mark-key) are marks, not dividers, even when drawn
# with a border (a hollow circle, a dashed tally key).
_EXEMPT = ('button, .chip, input, select, .badge, .grip, svg, canvas, .tip, '
	'.empty, .js-plotly-plot, .modebar, .check .box, .swatch, .mark-key, '
	'[hidden]')
_LIST = 'td, th, .log-entry, .log-header, .stat-row, .model-row, .bar-row, .hier-row'

_JS = r'''
(args) => {
	const root = document.querySelector(args.root);
	if (!root) return ['no element matches ' + args.root];
	const exempt = args.exempt, list = args.list, out = [];
	// A closed <details> keeps boxes for its contents; only its summary shows.
	const folded = (e) => { const d = e.closest('details');
		return Boolean(d && !d.open && !e.closest('summary')); };
	const visible = (e) => { const r = e.getBoundingClientRect();
		return r.width > 0 && r.height > 0 && !folded(e)
			&& getComputedStyle(e).visibility !== 'hidden'; };
	const clear = (c) => /rgba\([^)]*,\s*0\)$/.test(c) || c === 'transparent';
	const name = (e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '')
		+ (e.className && typeof e.className === 'string'
			? '.' + e.className.trim().split(/\s+/).join('.') : '');

	// Measured at the top of the page: a sticky element scrolled over
	// another is not a layout. Rects are clipped by every scrolling or
	// overflow-hidden ancestor, so elided text and rows scrolled out of a
	// viewport are measured as the reader sees them.
	window.scrollTo(0, 0);
	const clip = (e, r) => {
		let x0 = r.left, x1 = r.right, y0 = r.top, y1 = r.bottom;
		for (let a = e.parentElement; a && a !== root.parentElement; a = a.parentElement) {
			const cs = getComputedStyle(a);
			if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
			const b = a.getBoundingClientRect();
			if (cs.overflowX !== 'visible') { x0 = Math.max(x0, b.left); x1 = Math.min(x1, b.right); }
			if (cs.overflowY !== 'visible') { y0 = Math.max(y0, b.top); y1 = Math.min(y1, b.bottom); }
		}
		return x1 - x0 > 0.5 && y1 - y0 > 0.5
			? { left: x0, right: x1, top: y0, bottom: y1 } : null;
	};

	const lines = [];
	for (const e of root.querySelectorAll('*')) {
		if (e.closest(exempt) || !visible(e)) continue;
		const cs = getComputedStyle(e), raw = e.getBoundingClientRect();
		const r = clip(e, raw);
		if (!r) continue;
		// A line sits where the element's own box puts it; the clip only
		// shortens it, or drops it when it lies outside the visible part.
		const add = (v, at, w) => {
			if (v ? at < r.left - 0.5 || at > r.right + 0.5
				: at < r.top - 0.5 || at > r.bottom + 0.5) return;
			lines.push(v ? { v, at, w, a: r.top, b: r.bottom, e }
				: { v, at, w, a: r.left, b: r.right, e });
		};
		for (const side of ['Top', 'Right', 'Bottom', 'Left']) {
			const w = parseFloat(cs['border' + side + 'Width']);
			if (!(w > 0) || cs['border' + side + 'Style'] === 'none'
				|| clear(cs['border' + side + 'Color'])) continue;
			if (side === 'Left') add(true, raw.left, w);
			if (side === 'Right') add(true, raw.right - w, w);
			if (side === 'Top') add(false, raw.top, w);
			if (side === 'Bottom') add(false, raw.bottom - w, w);
		}
	}

	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	for (let node = walker.nextNode(); node; node = walker.nextNode()) {
		if (!node.textContent.trim()) continue;
		const parent = node.parentElement;
		if (!parent || parent.closest(exempt) || !visible(parent)) continue;
		const range = document.createRange();
		range.selectNodeContents(node);
		const t = clip(node, range.getBoundingClientRect());
		if (!t) continue;
		const inList = Boolean(parent.closest(list));

		for (const l of lines) {
			let d;
			if (l.v) {
				if (Math.min(t.bottom, l.b) - Math.max(t.top, l.a) <= 1) continue;
				d = t.left >= l.at + l.w ? t.left - (l.at + l.w)
					: t.right <= l.at ? l.at - t.right : -1;
				if (d < args.h - 0.5) out.push(`${JSON.stringify(node.textContent.trim().slice(0, 30))} `
					+ `in ${name(parent)} is ${d.toFixed(1)}px from the vertical line of ${name(l.e)}`);
			} else {
				if (Math.min(t.right, l.b) - Math.max(t.left, l.a) <= 1) continue;
				d = t.top >= l.at + l.w ? t.top - (l.at + l.w)
					: t.bottom <= l.at ? l.at - t.bottom : -1;
				const min = inList ? args.v_list : args.v;
				if (d < min - 0.5) out.push(`${JSON.stringify(node.textContent.trim().slice(0, 30))} `
					+ `in ${name(parent)} is ${d.toFixed(1)}px from the horizontal line of ${name(l.e)}`);
			}
		}
	}
	return out;
}
'''

_DIVIDERS_JS = r'''
(args) => {
	const root = document.querySelector(args.root), out = [];
	const box = (e) => { const r = e.getBoundingClientRect(), s = getComputedStyle(e);
		return { left: r.left + parseFloat(s.borderLeftWidth) + parseFloat(s.paddingLeft),
			right: r.right - parseFloat(s.borderRightWidth) - parseFloat(s.paddingRight),
			line: r.left, w: parseFloat(s.borderLeftWidth), style: s.borderLeftStyle,
			visible: r.width > 0 && r.height > 0 }; };
	for (const [sel, h] of Object.entries(args.divided)) for (const e of root.querySelectorAll(sel)) {
		const me = box(e);
		if (!me.visible || !(me.w > 0) || me.style === 'none') continue;
		const prev = e.previousElementSibling
			|| (e.parentElement && e.parentElement.previousElementSibling);
		const after = me.left - (me.line + me.w);
		const label = e.className || e.tagName;
		if (Math.abs(after - h) > 1)
			out.push(`${label}: ${after.toFixed(1)}px from its divider to its content, want ${h}`);
		if (prev) {
			const p = box(prev);
			// A previous sibling on another row (wrapped tiles) is not across
			// this divider.
			const top = (x) => Math.round(x.getBoundingClientRect().top);
			if (!p.visible || top(prev) !== top(e)) continue;
			const before = me.line - p.right;
			if (Math.abs(before - h) > 1)
				out.push(`${label}: ${before.toFixed(1)}px from ${prev.className || prev.tagName}'s `
					+ `content to the divider, want ${h}`);
		}
	}
	return out;
}
'''

_RHYTHM_JS = r'''
(args) => {
	const root = document.querySelector(args.root), out = [];
	const shown = (e) => e && e.getBoundingClientRect().height > 0
		&& getComputedStyle(e).display !== 'none';
	const next = (e) => { let n = e.nextElementSibling;
		while (n && !shown(n)) n = n.nextElementSibling; return n; };
	const gap = (a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().bottom;
	const want = (what, a, b, g) => {
		if (!b) return;
		const d = gap(a, b);
		if (Math.abs(d - g) > 1) out.push(`${what}: ${d.toFixed(1)}px, want ${g}`
			+ ` (${a.className || a.tagName} -> ${b.className || b.tagName || b.id})`);
	};
	for (const head of root.querySelectorAll('.section-head'))
		if (shown(head)) want('section head -> content', head, next(head), args.head_gap);
	for (const main of root.querySelectorAll('main')) {
		const sections = [...main.children].filter((s) => s.tagName === 'SECTION' && shown(s));
		for (let i = 1; i < sections.length; i++)
			want('between sections', sections[i - 1], sections[i], args.section_gap);
	}
	for (const cap of root.querySelectorAll('figcaption'))
		if (shown(cap) && !cap.closest('.hero')) want('title -> next', cap, next(cap), args.title_gap);
	for (const head of root.querySelectorAll('.panel-headline'))
		if (shown(head)) want('headline -> plot', head, next(head), args.title_gap);
	return out;
}
'''

_PLOTLY_JS = r'''
(root) => {
	const out = [];
	const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5
		&& a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
	for (const plot of document.querySelectorAll(root + ' .js-plotly-plot')) {
		if (!plot.getBoundingClientRect().height) continue;
		const titles = [...plot.querySelectorAll('[class$="title"] text')]
			.filter((t) => /^g-[xy]\d*title$/.test(t.parentElement.getAttribute('class') || '')
				|| /^[xy]\d*title$/.test(t.getAttribute('class') || ''));
		const ticks = [...plot.querySelectorAll('.xtick text, .ytick text, [class*="tick"] > text')];
		for (const title of titles) {
			const a = title.getBoundingClientRect();
			if (!a.width) continue;
			for (const tick of ticks) {
				const b = tick.getBoundingClientRect();
				if (b.width && hit(a, b)) {
					out.push(`${plot.id || 'plot'}: axis title "${title.textContent}" meets tick "${tick.textContent}"`);
					break;
				}
			}
		}
	}
	return out;
}
'''


def spacing_violations(page, root, h=12, v=8, v_list=4):
	return page.evaluate(_JS, {'root': root, 'h': h, 'v': v, 'v_list': v_list,
		'exempt': _EXEMPT, 'list': _LIST})


def divider_violations(page, root, divided=DIVIDED):
	return page.evaluate(_DIVIDERS_JS, {'root': root, 'divided': dict(divided)})


def rhythm_violations(page, root, head_gap=12, section_gap=24, title_gap=8):
	return page.evaluate(_RHYTHM_JS, {'root': root, 'head_gap': head_gap,
		'section_gap': section_gap, 'title_gap': title_gap})


def axis_title_collisions(page, root):
	return page.evaluate(_PLOTLY_JS, root)


_LOG_JS = r'''
(root) => {
	const out = [];
	const hit = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5
		&& a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
	for (const plot of document.querySelectorAll(root + ' .js-plotly-plot')) {
		const full = plot._fullLayout;
		if (!full || !plot.getBoundingClientRect().height) continue;
		for (const key of Object.keys(full).filter((k) => /^[xy]axis\d*$/.test(k))) {
			const ax = full[key];
			const id = ax._id;
			const labels = [...plot.querySelectorAll(`g.${id}tick > text`)]
				.map((t) => [t.textContent, t.getBoundingClientRect()])
				.filter(([, r]) => r.width > 0);
			for (let i = 0; i < labels.length; i++)
				for (let j = i + 1; j < labels.length; j++)
					if (hit(labels[i][1], labels[j][1]))
						out.push(`${plot.id} ${id}: tick labels "${labels[i][0]}" and "${labels[j][0]}" overlap`);
			if (ax.type !== 'log') continue;
			// The drawn range, in decades: the same quantity logTicks()
			// decides from, since it sets that range itself.
			const span = Math.abs(ax.range[1] - ax.range[0]);
			const grid = plot.querySelectorAll(`.gridlayer path.${id}grid`).length;
			const allowed = span <= 2 ? 3 * Math.ceil(span) + 1 : Math.ceil(span) + 1;
			if (grid > allowed)
				out.push(`${plot.id} ${id}: ${grid} gridlines over ${span.toFixed(2)} decades, `
					+ `at most ${allowed} (decades, or 1-2-5 within two)`);

			// Clipping: every positive value a trace draws on this axis lies
			// inside its range. A range computed from fewer values than are
			// drawn (a context layer, a reference point left out) cuts them.
			const [lo, hi] = [Math.min(...ax.range), Math.max(...ax.range)];
			const letter = id[0], suffix = id.slice(1);
			let clipped = 0;
			for (const t of plot.data || []) {
				if ((t[letter + 'axis'] || letter) !== letter + suffix) continue;
				for (const v of t[letter] || [])
					if (v > 0 && (Math.log10(v) < lo - 1e-9 || Math.log10(v) > hi + 1e-9))
						clipped += 1;
			}
			if (clipped)
				out.push(`${plot.id} ${id}: ${clipped} drawn value(s) outside the axis range`);
		}
	}
	return out;
}
'''


def log_axis_violations(page, root):
	"""Every Plotly log axis draws at most one gridline per decade (three,
	at 1-2-5, when it spans two decades or less), and no axis has two tick
	labels that overlap."""

	return page.evaluate(_LOG_JS, root)
