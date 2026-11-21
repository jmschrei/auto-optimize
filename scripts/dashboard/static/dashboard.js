'use strict';

/* dashboard.js -- the auto-optimize monitor: a monitor template with the lineage-tree and expandable-log widgets
   pasted in.

   One entry point, `window.startAutoopt(root, transport, options)`. The mount
   supplies the transport: the notebook passes one that reads published JSON
   through Jupyter's Contents API, the standalone page (tests only) one that
   fetches the same files over HTTP, and an export one that reads an embedded
   snapshot. Nothing below knows which.

   Loaded as an ES module, and it must stay one. As a classic script a second
   copy in one page throws `Identifier ... has already been declared`, the new
   code never runs, and the old copy keeps serving a stale page silently.

   Sections are delimited by `==== SECTION: <name> ====` and match the ones in
   dashboard.css and index.html. core through gpu are the template's;
   lineage-tree and expandable-log are the gallery's widgets; search
   through sidebar-autoopt are this dashboard's own. */

/* ==== SECTION: core ==== */

let STATE_INTERVAL = 20000;
let RESOURCE_INTERVAL = 3000;

const state = {
	snapshot: null,
	resources: null,
	lastGpus: null,       // {gpus, sampledAt}: the last sample that had GPUs
	selection: null,      // Set of iteration ids, or null for "everything"
	hidden: new Set(),    // categories hidden by a chip's dot
	emphasised: new Set(),  // categories emphasised by clicking their chip
	assetsStale: false,
	pinned: null,         // iteration clicked: its runs are outlined in Every run
	pinnedSeed: null,     // the run clicked, when it was a run's own dot
	hideFailed: false,
	showStair: true,
	showStages: true,     // a dashed line wherever the tree's generation changes
	showCI: false,        // dotted whiskers: each median's bootstrap interval
	showRuns: false,      // every seed's own score, not just the median
	treeNode: null,       // iteration whose panel is open under the tree
	readmes: {},          // id -> whole README text (null: could not be read)
	readmeFor: {},        // id -> the payload excerpt the text was fetched against
	logFull: new Set(),   // log entries showing their whole README (SHOW MORE)
};

/* The version this script was fetched with. The mount bakes a cache-busting
   query into the URL, so if the published front end has moved on, this copy
   is the old one and only re-running the cell will replace it. */
const LOADED_ASSETS = (() => {
	try {
		return new URL(import.meta.url).searchParams.get('v');
	} catch (error) {
		return null;
	}
})();

let ROOT = document.body;
let TRANSPORT = null;
const timers = [];

const $ = (id) => ROOT.querySelector('#' + id);
const css = (name) => getComputedStyle(ROOT).getPropertyValue(name).trim();

/* `$(id).replaceChildren()` on a missing node throws inside the poll's try,
   which silently stops every later panel. This keeps the rest alive. */
function fill(id, ...children) {
	const node = $(id);
	if (node) node.replaceChildren(...children.filter((c) =>
		c !== null && c !== undefined && c !== false));
	return node;
}

/* `el('div', {class, text, style, dataset, onclick}, [children])`. Null
   children are dropped: `replaceChildren(null)` inserts the text "null". */
function el(tag, attrs, children) {
	const node = document.createElement(tag);

	for (const [key, value] of Object.entries(attrs || {})) {
		if (value === null || value === undefined || value === false) continue;
		if (key === 'text') node.textContent = value;
		else if (key === 'class') node.className = value;
		else if (key === 'dataset') Object.assign(node.dataset, value);
		else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
		else node.setAttribute(key, value === true ? '' : value);
	}

	for (const child of [].concat(children || []))
		if (child !== null && child !== undefined && child !== false)
			node.append(child.nodeType ? child : document.createTextNode(child));

	return node;
}

const missing = (x) => x === null || x === undefined || Number.isNaN(x);

// `A107`: every iteration is shown with the label's letter, as the scripts
// print it.
const tag = (id) => missing(id) ? '–' : 'A' + id;

const fmt = {
	int: (x) => missing(x) ? '–' : Math.round(x).toLocaleString(),
	num: (x, digits = 3) => missing(x) ? '–' : Number(x).toFixed(digits),
	/* Four significant digits, scientific only outside [1e-4, 1e6). */
	sig: (x, digits = 4) => {
		if (missing(x)) return '–';
		const size = Math.abs(x);
		if (size !== 0 && (size < 1e-4 || size >= 1e6))
			return Number(x).toExponential(2);
		return Number(Number(x).toPrecision(digits)).toString();
	},
	signed: (x, digits = 2) => missing(x) ? '–'
		: (x >= 0 ? '+' : '') + Number(x).toFixed(digits),
	pct: (x) => missing(x) ? '–' : Math.round(x) + '%',
	signedPct: (x) => missing(x) ? '–' : (x >= 0 ? '+' : '') + Math.round(x) + '%',
	bytes: (x) => {
		if (missing(x)) return '–';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let i = 0;
		while (x >= 1024 && i < units.length - 1) { x /= 1024; i += 1; }
		return x.toFixed(x >= 100 || i === 0 ? 0 : 1) + ' ' + units[i];
	},
	duration: (seconds) => {
		if (missing(seconds)) return '–';
		seconds = Math.max(0, Math.round(seconds));
		const d = Math.floor(seconds / 86400);
		const h = Math.floor((seconds % 86400) / 3600);
		const m = Math.floor((seconds % 3600) / 60);
		if (d) return `${d}d ${h}h`;
		if (h) return `${h}h ${m}m`;
		if (m) return `${m}m ${seconds % 60}s`;
		return `${seconds}s`;
	},
};

const escape = (text) => String(missing(text) ? '' : text)
	.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

/* ==== SECTION: colour ==== */

/* A token as a plain colour Plotly and canvas can parse. `css()` returns the
   token's text, and `--running` is a `color-mix()` that Plotly rejects and a
   canvas reads back as `color(srgb ...)`; painting one pixel and reading it
   back gives `#rrggbb` (or `rgba()` when translucent). Takes a token name or
   a colour. Use it for every colour that reaches Plotly or a canvas. */
const resolvedCache = new Map();
let resolveCtx = null;

function resolved(token) {
	const value = token.startsWith('--') ? css(token) : token;
	if (/^#[0-9a-f]{6}$/i.test(value) || /^rgba?\(/i.test(value)) return value;
	if (resolvedCache.has(value)) return resolvedCache.get(value);

	resolveCtx = resolveCtx || document.createElement('canvas')
		.getContext('2d', { willReadFrequently: true });
	resolveCtx.clearRect(0, 0, 1, 1);
	resolveCtx.fillStyle = value;
	resolveCtx.fillRect(0, 0, 1, 1);

	const [r, g, b, a] = resolveCtx.getImageData(0, 0, 1, 1).data;
	const hex = '#' + [r, g, b].map((c) => c.toString(16).padStart(2, '0'))
		.join('');
	const out = a === 255 ? hex : `rgba(${r},${g},${b},${(a / 255).toFixed(3)})`;

	resolvedCache.set(value, out);
	return out;
}

/* Four muted hues, then the shape carries the group as a second channel:
   group i takes hue i % 4 and symbol floor(i / 4). Pink is not a slot: it
   is the frontier line and the headline. A catch-all group (any name in
   CATCH_ALL) is always grey circles. Indices come from every group in the
   payload, never the visible subset, so a group keeps its colour and shape
   when a filter changes. */
const CAT_SLOTS = ['--cat-0', '--cat-1', '--cat-2', '--cat-3', '--cat-4',
	'--cat-5', '--cat-6', '--cat-7', '--cat-8', '--cat-9'];
const LINE_SLOTS = CAT_SLOTS.slice(0, 8).concat(['--line-4', '--line-5']);

/* A grouping whose values are verdicts (pass / marginal / fail) takes the
   state tones, not the categorical slots: muted for the bulk, full strength
   for emphasis. An unrecognised value is the catch-all grey. */
const VERDICT_TONES = { pass: 'good', ok: 'good', good: 'good',
	marginal: 'warn', warn: 'warn', fail: 'bad', bad: 'bad' };

const verdictSlot = (value) => {
	const tone = VERDICT_TONES[String(value).toLowerCase()];
	return tone ? `--${tone}-muted` : '--cat-other';
};

/* The full-strength hue behind each muted slot, for emphasis: a frontier
   point, a selected point, an emphasised category. */
const FULL = { '--cat-0': '--cyan', '--cat-1': '--purple', '--cat-2': '--orange',
	'--cat-3': '--green', '--cat-4': '--blue', '--cat-5': '--olive',
	'--cat-6': '--lime', '--cat-7': '--orchid', '--cat-8': '--clay',
	'--cat-9': '--steel', '--line-4': '--yellow', '--line-5': '--cat-other-strong',
	'--cat-other': '--cat-other-strong', '--cat-root': '--cat-root-strong',
	'--good-muted': '--good', '--warn-muted': '--warn', '--bad-muted': '--bad' };
const emphasis = (slot) => resolved(FULL[slot] || slot);

const verdictColour = (value, emphasised) => emphasised
	? emphasis(verdictSlot(value)) : resolved(verdictSlot(value));

/* autoopt.py's catch-all category, and the baseline's own. ROOT holds only
   iteration 0 and takes a fixed neutral outside the slots, so adding it
   moved no other category's colour. */
const CATCH_ALL = new Set(['MISC']);
const ROOT_CATEGORY = 'ROOT';

/* Categories are the fixed list autoopt.py allows, so a category keeps its
   colour across searches and whatever the chips hide. */
const CATEGORY_ORDER = ['CACHE_MISS', 'CACHING', 'MICRO', 'NUMBA', 'OPTIMIZATION',
	'PARALLEL', 'PRECISION', 'REORGANIZATION', 'IO', 'ALGORITHM'];

function groupIndex(group) {
	const fixed = CATEGORY_ORDER.indexOf(group);
	if (fixed >= 0) return fixed;
	const extra = [...((state.snapshot || {}).categories || [])]
		.filter((g) => !CATCH_ALL.has(g) && g !== ROOT_CATEGORY
			&& !CATEGORY_ORDER.includes(g)).sort();
	const at = extra.indexOf(group);
	return at < 0 ? -1 : CATEGORY_ORDER.length + at;
}

function groupSlot(group) {
	if (group === ROOT_CATEGORY) return '--cat-root';
	const i = groupIndex(group);
	return i < 0 ? '--cat-other' : CAT_SLOTS[i % CAT_SLOTS.length];
}

/* Display order: ROOT first, the catch-all last, the rest by name. */
const categoryOrder = (a, b) => (b === ROOT_CATEGORY) - (a === ROOT_CATEGORY)
	|| CATCH_ALL.has(a) - CATCH_ALL.has(b) || a.localeCompare(b);

/* The legend mirrors the mark: same hue, same shape. symbol: a Plotly
   marker symbol, 'x', or 'line', 'dotted', 'dashed' for a line trace;
   opacity for a key of a faint layer. */
function markerKey(colour, symbol, opacity) {
	// A line or dotted-line key for a trace drawn as a line (a frontier, a
	// reference): the key draws the mark it names.
	const lines = { line: `height:2px;width:12px;background:${colour}`,
		dotted: `height:0;width:12px;border-top:2px dotted ${colour}`,
		dashed: `height:0;width:12px;border-top:2px dashed ${colour}` };

	const shapes = {
		circle: 'border-radius:50%',
		square: '',
		diamond: 'transform:rotate(45deg) scale(.8)',
		'triangle-up': 'clip-path:polygon(50% 0,100% 100%,0 100%)',
		cross: 'clip-path:polygon(38% 0,62% 0,62% 38%,100% 38%,100% 62%,'
			+ '62% 62%,62% 100%,38% 100%,38% 62%,0 62%,0 38%,38% 38%)',
		// The baseline's mark: a cross rotated 45 degrees.
		x: 'transform:rotate(45deg);clip-path:polygon(38% 0,62% 0,62% 38%,'
			+ '100% 38%,100% 62%,62% 62%,62% 100%,38% 100%,38% 62%,0 62%,'
			+ '0 38%,38% 38%)',
		// Plotly's x-thin-open: an abandoned iteration.
		'x-thin': 'transform:rotate(45deg);clip-path:polygon(44% 0,56% 0,56% 44%,'
			+ '100% 44%,100% 56%,56% 56%,56% 100%,44% 100%,44% 56%,0 56%,'
			+ '0 44%,44% 44%)',
	};

	// .mark-key: a legend key is a mark, never a divider (spacing_check).
	return el('span', { class: 'mark-key', style: 'display:inline-flex;width:12px;height:12px;'
		+ 'align-items:center;justify-content:center'
		+ (opacity !== undefined ? `;opacity:${opacity}` : '') }, [
		el('span', { style: lines[symbol] || `width:9px;height:9px;background:${colour};`
			+ (shapes[symbol] || '') })]);
}

/* ==== SECTION: summary ==== */

function tile(label, value, delta, tone) {
	return el('div', { class: 'tile', 'data-tone': tone || null }, [
		el('div', { class: 'label', text: label }),
		el('div', { class: 'value', text: value }),
		el('div', { class: 'delta', text: delta || ' ' }),
	]);
}

/* ==== SECTION: sidebar ==== */

/* rows: [key, value, tone?]; a null row is skipped. */
function statCard(title, rows, note) {
	return el('div', { class: 'stat-card' }, [
		el('h3', { text: title }),
		...rows.filter(Boolean).map(([key, value, tone]) =>
			el('div', { class: 'stat-row' }, [
				el('span', { class: 'key', text: key, title: key }),
				el('span', { class: 'val' + (tone ? ' ' + tone : ''),
					text: value })])),
		note ? el('div', { class: 'stat-note', text: note }) : null,
	]);
}

/* entries: [key, n]; colour(key) gives each bar its fill. */
function barCard(title, entries, colour, note) {
	const max = Math.max(1, ...entries.map(([, n]) => n));

	return el('div', { class: 'stat-card' }, [
		el('h3', { text: title }),
		el('div', { class: 'bar-list' }, entries.map(([key, n]) =>
			el('div', { class: 'bar-row' }, [
				el('span', { class: 'key', text: key, title: key }),
				// True proportion: a floor would draw small counts larger
				// than they are. One pixel only so a non-zero count shows.
				el('span', { class: 'bar', style: `width:${100 * n / max}%;`
					+ `${n > 0 ? 'min-width:1px;' : ''}background:${colour(key)}` }),
				el('span', { class: 'n', text: fmt.int(n) })]))),
		note ? el('div', { class: 'stat-note', text: note }) : null,
	]);
}

/* ==== SECTION: plots ==== */

const plotted = new Set();
const plotly = () => window.Plotly || null;

/* The axis title keeps a standoff from the tick labels, and automargin
   makes room for both, so the two never meet. A caller's `title` is merged
   into the default rather than replacing it. With `automargin: false` (a
   fixed margin), leave the y title out and name the quantity in the panel
   title instead. */
function axis(overrides) {
	overrides = overrides || {};
	const title = Object.assign({ font: { color: resolved('--text-faint'),
		size: 11 }, standoff: 8 }, overrides.title || {});

	return Object.assign({
		gridcolor: resolved('--line-soft'),
		zeroline: false,
		linecolor: resolved('--line'),
		tickcolor: resolved('--line'),
		tickfont: { color: resolved('--text-faint'), size: 10 },
		automargin: true,
	}, overrides, { title });
}

/* The ticks of a log axis: gridlines and labels at decades only (1, 10,
   100), or at 1-2-5 when the positive values span two decades or less; the
   2-9 between are short unlabelled minor ticks with no gridline. Every label
   is the tick's full value (20, 50, 200): Plotly's own 1-2-5 mode ('D2')
   prints the 2s and 5s as bare leading digits. Spread into axis() whenever
   scaleOf() says 'log'; a linear axis gets {}. */
function logTicks(values, type) {
	if (type !== 'log') return {};

	// The range is set here rather than left to Plotly's autorange, so the
	// rule decides from the axis as drawn: the data's decades plus 5% of
	// them (at least 0.05) either side. Two decades or less of drawn axis
	// is 1-2-5; more is decades. spacing_check reads the same range.
	const positive = values.filter((v) => v > 0);
	if (!positive.length) return {};
	const lo = Math.log10(Math.min(...positive));
	const hi = Math.log10(Math.max(...positive));
	const pad = Math.max(0.05, 0.05 * (hi - lo));
	const range = [lo - pad, hi + pad];

	let tickvals = logTickValues(range, range[1] - range[0] <= 2 ? [1, 2, 5] : [1]);
	// A narrow axis (forced to log by its button) may hold fewer than two of
	// those; then every 1-9 step is labelled.
	if (tickvals.length < 2) tickvals = logTickValues(range, [1, 2, 3, 4, 5, 6, 7, 8, 9]);

	return { range, autorange: false, tickmode: 'array', tickvals,
		ticktext: tickvals.map((value) => value.toLocaleString('en-US',
			{ maximumSignificantDigits: 1 })),
		minor: { ticks: 'outside', ticklen: 3, tickcolor: resolved('--line'),
			showgrid: false, dtick: 'D1' } };
}

/* The values step x 10^k (for each step given) that fall inside a log
   axis's range, which is in log10 units. */
function logTickValues(range, steps) {
	const values = [];
	for (let k = Math.floor(range[0]) - 1; k <= Math.ceil(range[1]); k++)
		for (const step of steps) {
			const value = Number((step * 10 ** k).toPrecision(1));
			const at = Math.log10(value);
			if (at >= range[0] && at <= range[1]) values.push(value);
		}
	return values;
}

/* Flat: paper and plot area are the page colour, so the data sits on the
   page with no box. Faint grid on both axes. The hover label is monospace,
   which the aligned tooltip block needs. */
function plotLayout(overrides) {
	return Object.assign({
		// Every plot sits in a panel.
		paper_bgcolor: resolved('--panel'),
		plot_bgcolor: resolved('--panel'),
		font: { family: 'system-ui, -apple-system, sans-serif', size: 11,
			color: resolved('--text-dim') },
		margin: { l: 54, r: 14, t: 10, b: 40 },
		hovermode: 'closest',
		hoverlabel: { bgcolor: resolved('--tip-bg'), bordercolor: resolved('--line'),
			font: { color: resolved('--text'), size: 11,
				family: 'ui-monospace, Menlo, monospace' }, align: 'left' },
		showlegend: false,
		dragmode: 'lasso',
		xaxis: axis(),
		yaxis: axis(),
	}, overrides || {});
}

/* Charts that start a selection keep a trimmed modebar, so the reader can
   swap between lasso, box select and zoom. */
const SELECT_CONFIG = {
	responsive: true,
	displaylogo: false,
	modeBarButtonsToRemove: ['toImage', 'sendDataToCloud', 'toggleSpikelines',
		'hoverClosestCartesian', 'hoverCompareCartesian', 'autoScale2d'],
};
const PLAIN_CONFIG = { displayModeBar: false, responsive: true };

/* options: {ids, selectable, onHover, onClick, config}. `ids[i]` is the row
   behind input point i, so a selection event can be turned back into ids
   whatever the trace layout. onClick(id) and onHover(id) receive the point's
   customdata; onHover(null) when the pointer leaves the points. Each handler
   is wired once per node. */
function draw(id, traces, layout, options) {
	const P = plotly();
	const node = $(id);
	if (!node) return null;

	if (!P) {
		node.replaceChildren(el('div', { class: 'empty', text: 'Plotly failed '
			+ 'to load, so the charts are unavailable. The sidebar still '
			+ 'carries the numbers.' }));
		return null;
	}

	options = options || {};
	plotted.add(id);
	if (options.ids) node._ids = options.ids;

	// A node whose plot was replaced (an empty state filled in, then data
	// again) still carries Plotly's layout, and react() would skip the
	// redraw. Purge it, and its handlers with it.
	if (node.layout && !node.querySelector('.main-svg')) {
		P.purge(node);
		node._wired = node._clickWired = node._hoverWired = false;
	}

	P.react(node, traces, layout, options.config || PLAIN_CONFIG);
	if (sizeWatch) sizeWatch.observe(node);

	if (options.selectable && !node._wired) {
		node._wired = true;
		node.on('plotly_selected', (event) => onBrush(node, event));
		node.on('plotly_deselect', () => setSelection(null));
		node.on('plotly_doubleclick', () => setSelection(null));
	}

	node._onClick = options.onClick;
	node._onHover = options.onHover;

	if (options.onHover && !node._hoverWired) {
		node._hoverWired = true;
		node.on('plotly_hover', (event) => {
			const point = event.points && event.points[0];
			if (point && point.customdata !== undefined && node._onHover)
				node._onHover(point.customdata);
		});
		node.on('plotly_unhover', () => { if (node._onHover) node._onHover(null); });
	}

	if (options.onClick && !node._clickWired) {
		node._clickWired = true;
		node.on('plotly_click', (event) => {
			const point = event.points && event.points[0];
			if (point && point.customdata !== undefined && node._onClick)
				node._onClick(point.customdata);
		});
	}

	return node;
}

let rendering = false;

/* Re-rendering the other charts wipes this one's lasso, and Plotly reports
   that as a fresh selection of nothing. Ignore our own redraws and every
   empty selection; clearing is explicit (deselect, double-click, the Clear
   button). */
function onBrush(node, event) {
	if (rendering || !event || !event.points || !event.points.length) return;

	const ids = new Set();

	for (const point of event.points) {
		if (point.pointNumbers && node._ids) {
			for (const index of point.pointNumbers)
				if (node._ids[index] !== undefined) ids.add(node._ids[index]);
		} else if (point.customdata !== undefined) {
			ids.add(point.customdata);
		}
	}

	setSelection(ids.size ? ids : null);
}

/* Everything that follows a selection registers here: charts, tables, the
   tree. `setSelection` re-renders each, under the redraw guard. */
const selectionListeners = [];

function setSelection(ids) {
	state.selection = ids;
	$('clear-selection').hidden = !ids;

	rendering = true;
	try {
		for (const render of selectionListeners) render();
	} finally {
		setTimeout(() => { rendering = false; }, 250);
	}
}

/* A row is emphasised when it is in the selection (or there is none) and its
   group is emphasised by a chip (or none is). `groupOf` reads the row's
   group; rows without one follow the selection only. */
function isEmphasised(row, groupOf) {
	const group = (groupOf || ((r) => r.group))(row);
	return (!state.selection || state.selection.has(row.id))
		&& (!state.emphasised.size || group === undefined
			|| state.emphasised.has(group));
}

/* Indices of the emphasised `rows`, or undefined when nothing is selected
   or emphasised, which Plotly reads as "everything selected". */
function selectedIndices(rows, groupOf) {
	if (!state.selection && !state.emphasised.size) return undefined;

	const out = [];
	rows.forEach((row, index) => {
		if (isEmphasised(row, groupOf)) out.push(index);
	});

	return out;
}

/* One chip per category with its count. Clicking a chip emphasises its
   category in every linked chart (the rest fade, as in a selection); several
   may be emphasised, and clicking again clears. The dot at the chip's end
   hides the category instead: gone from every chart and the log, dimmed in
   a tree. `hidden` and `emphasised` are Sets the caller owns and every
   renderer reads; `onChange` re-renders all of them, this included. */
function categoryChips(holderId, counts, colourOf, hidden, emphasised,
	onChange) {
	fill(holderId, ...Object.entries(counts).map(([category, n]) => {
		const shown = !hidden.has(category);
		const on = emphasised.has(category);

		return el('span', { class: 'chip', style: `color:${colourOf(category)}`,
			dataset: { category, emphasised: String(on), hidden: String(!shown) } }, [
			el('button', { class: 'chip-main', type: 'button',
				'aria-pressed': String(on), title: 'Emphasise in every chart',
				onclick: () => {
					if (on) emphasised.delete(category);
					else { emphasised.add(category); hidden.delete(category); }
					onChange();
				} }, [
				el('span', { class: 'dot', style: `background:${colourOf(category)}` }),
				el('b', { text: fmt.int(n) }), category]),
			el('button', { class: 'eye', type: 'button',
				'aria-pressed': String(shown), text: shown ? '●' : '○',
				title: shown ? 'Hide everywhere' : 'Show again',
				onclick: () => {
					if (shown) { hidden.add(category); emphasised.delete(category); }
					else hidden.delete(category);
					onChange();
				} }),
		]);
	}));
}

/* Log axes: automatic, with a per-plot override. An axis flagged log-able
   goes log when its positive values span at least a decade; the buttons
   under the plot override that, and say how many points a log axis hides,
   because Plotly omits non-positive values on one silently. */
const SCALES = {};

function autoLog(values) {
	const positive = values.filter((v) => v > 0);
	return positive.length > 1
		&& Math.log10(Math.max(...positive) / Math.min(...positive)) >= 1;
}

function scaleOf(plot, which, values, logable) {
	const chosen = (SCALES[plot] || {})[which];
	if (chosen) return chosen;
	return logable && autoLog(values) ? 'log' : 'linear';
}

/* `rerender` redraws whatever reads SCALES after a click. */
function renderScales(plot, axes, rerender) {
	fill('scales-' + plot, ...axes.map(({ which, values, logable }) => {
		const log = scaleOf(plot, which, values, logable) === 'log';
		const dropped = values.filter((v) => !missing(v) && v <= 0).length;
		const auto = !(SCALES[plot] || {})[which];

		return el('button', { type: 'button', 'aria-pressed': String(log),
			text: which + ' log' + (dropped ? ` (${dropped} hidden)` : ''),
			title: (auto ? 'Automatic. ' : '') + (dropped ? `${dropped} `
				+ 'point(s) are <= 0 and a log axis omits them.'
				: `Switch the ${which} axis between linear and log.`),
			onclick: () => {
				SCALES[plot] = SCALES[plot] || {};
				SCALES[plot][which] = log ? 'linear' : 'log';
				rerender();
			} });
	}));
}

/* Hover text as an aligned label/value block. Labels pad to a fixed width
   with   -- Plotly renders each line as an SVG tspan, where runs of
   ordinary spaces collapse -- and numbers right-align in their field so a
   minus sign does not shift the digits. Blank lines separate groups. */
const TIP_LABEL = 13;
const TIP_NUMBER = 9;

function tipRow(label, value) {
	if (value === null || value === undefined || value === '') return null;

	const text = String(value);
	return escape(label).padEnd(TIP_LABEL, ' ')
		+ escape(text).padStart(TIP_NUMBER, ' ');
}

/* A tipRow whose value is coloured in parts: [[text, token or null]]. The
   padding counts the plain text, so the parts align like any value. */
function tipRowParts(label, parts) {
	const text = parts.map(([part]) => part).join('');
	return escape(label).padEnd(TIP_LABEL, ' ')
		+ ' '.repeat(Math.max(0, TIP_NUMBER - text.length))
		+ parts.map(([part, token]) => token ? `<span style="color:${resolved(token)}">`
			+ `${escape(part)}</span>` : escape(part)).join('');
}

/* An iteration's code change against its parent, from their merge
   base, in GitHub's colours: lines added and deleted, and the files added,
   modified and deleted. The baseline and anything in flight have none. */
const DIFF_FILES = [['modified', 4, '--diff-mod', 'diff-mod'],
	['added', 2, '--diff-add', 'diff-add'], ['deleted', 3, '--diff-del', 'diff-del']];

function diffTipRows(row) {
	const stat = row.diffstat;
	if (!stat) return [];
	if (!stat.some(Boolean)) return [tipRow('code', 'unchanged')];
	return [tipRowParts('lines', [['+' + fmt.int(stat[0]), '--diff-add'],
			[' ', null], ['−' + fmt.int(stat[1]), '--diff-del']]),
		...DIFF_FILES.filter(([, index]) => stat[index]).map(([word, index, token]) =>
			tipRowParts('files ' + word, [[fmt.int(stat[index]), token]]))];
}

function diffLine(row) {
	const stat = row.diffstat;
	if (!stat) return null;
	if (!stat.some(Boolean)) return el('div', { class: 'log-diffstat',
		text: 'no change to the code' });
	const files = DIFF_FILES.filter(([, index]) => stat[index]).map(([word, index, , cls]) =>
		el('span', { class: cls },
			`${fmt.int(stat[index])} ${stat[index] === 1 ? 'file' : 'files'} ${word}`));
	return el('div', { class: 'log-diffstat' }, [
		el('span', { class: 'diff-add', text: '+' + fmt.int(stat[0]) }),
		el('span', { class: 'diff-del', text: '−' + fmt.int(stat[1]) }),
		...files]);
}

/* A hover label taller than its plot does not render at all -- Plotly draws
   it inside the plot's SVG -- so the block is cut to what the plot can hold.
   Empirical: 15 px per line plus 30 px, which gives 18 lines at 300 px. */
function tipBlock(groups, plotId) {
	const node = plotId ? $(plotId) : null;
	const budget = Math.max(8, Math.floor((((node && node.clientHeight) || 300)
		- 30) / 15));
	const lines = [];

	for (const group of groups) {
		const rows = group.filter((row) => row !== null && row !== undefined);
		if (!rows.length) continue;
		if (lines.length) lines.push('');
		lines.push(...rows);
	}

	return lines.slice(0, budget).join('<br>');
}

const HOVER = '%{text}<extra></extra>';

/* The sequential heatmap scale as a Plotly colorscale. */
function sequentialScale() {
	return [[0, resolved('--seq-0')], [0.35, resolved('--seq-1')], [0.7, resolved('--seq-2')],
		[1, resolved('--seq-3')]];
}

/* Plotly sizes a plot from its container when it draws it. A plot drawn
   while the output is hidden (the cell scrolled out of view or collapsed when
   a new round rebuilds the validation cards) takes Plotly's default 700 px
   and keeps it, and no window resize comes to fix it. So every plotted node
   is watched, and resized whenever its own width stops matching the plot's. */
const sizeWatch = typeof ResizeObserver === 'undefined' ? null
	: new ResizeObserver((entries) => {
		const P = plotly();
		if (!P) return;
		for (const entry of entries) {
			const node = entry.target;
			const width = Math.round(entry.contentRect.width);
			if (width > 0 && node._fullLayout && node.isConnected
				&& Math.abs(node._fullLayout.width - width) > 1) P.Plots.resize(node);
		}
	});

/* A chart in a fresh output box renders one pixel wide until the cell
   settles, so every plot is resized on window resize and once after mount. */
function resizePlots() {
	const P = plotly();
	if (!P) return;

	for (const id of plotted) {
		const node = $(id);
		if (node) P.Plots.resize(node);
	}
}

/* ==== SECTION: reorder ==== */

/* Drag to reorder, from a grip handle only -- Plotly owns the pointer over a
   plot, where a drag is a lasso -- with the order stored per run in
   localStorage so a poll or a reopened notebook does not undo it.

   `order(key, names)` returns the saved order reconciled with `names`: kept
   entries first, new ones appended, vanished ones dropped. `wire(card, key,
   name, names, rerender, axis)` makes `card` a drop target whose `.grip`
   starts the drag; axis 'x' for a row of panels, 'y' for a list. */
const reorder = {
	dragging: null,

	storageKey(key) {
		return 'autoopt:order:' + key + ':' + ((state.snapshot || {}).root || '');
	},

	order(key, names) {
		let saved = null;

		try {
			saved = JSON.parse(localStorage.getItem(reorder.storageKey(key)));
		} catch (error) { /* storage unavailable: per-session only */ }

		if (!Array.isArray(saved)) return names.slice();

		const kept = saved.filter((name) => names.includes(name));
		return kept.concat(names.filter((name) => !kept.includes(name)));
	},

	save(key, names) {
		try {
			localStorage.setItem(reorder.storageKey(key), JSON.stringify(names));
		} catch (error) { /* per-session only */ }
	},

	wire(card, key, name, names, rerender, axisName) {
		const grip = card.querySelector('.grip');
		const horizontal = axisName !== 'y';

		grip.setAttribute('draggable', 'true');

		grip.addEventListener('dragstart', (event) => {
			reorder.dragging = { key, name };
			card.dataset.dragging = 'true';
			event.dataTransfer.effectAllowed = 'move';
			event.dataTransfer.setData('text/plain', name);

			// Drag the panel, not the handle.
			if (event.dataTransfer.setDragImage)
				event.dataTransfer.setDragImage(card, 30, 14);
		});

		grip.addEventListener('dragend', () => {
			reorder.dragging = null;
			delete card.dataset.dragging;
			for (const other of ROOT.querySelectorAll('[data-drop]'))
				delete other.dataset.drop;
		});

		card.addEventListener('dragover', (event) => {
			const drag = reorder.dragging;
			if (!drag || drag.key !== key || drag.name === name) return;

			event.preventDefault();
			event.dataTransfer.dropEffect = 'move';

			const box = card.getBoundingClientRect();
			const after = horizontal
				? event.clientX > box.left + box.width / 2
				: event.clientY > box.top + box.height / 2;
			card.dataset.drop = after ? 'after' : 'before';
		});

		card.addEventListener('dragleave', () => { delete card.dataset.drop; });

		card.addEventListener('drop', (event) => {
			event.preventDefault();

			const drag = reorder.dragging;
			const after = card.dataset.drop === 'after';
			delete card.dataset.drop;

			if (!drag || drag.key !== key || drag.name === name) return;

			const next = reorder.order(key, names)
				.filter((entry) => entry !== drag.name);
			const at = next.indexOf(name);
			next.splice(at < 0 ? next.length : at + (after ? 1 : 0), 0,
				drag.name);

			reorder.save(key, next);
			rerender();
		});
	},
};

/* ==== SECTION: density ==== */

const DENSITY_LIMIT = 5000;

/* Marks by n: opaque size 7 up to 500 points (on the dark page
   translucent marks vanish at small n), size 5 at opacity
   0.3 up to DENSITY_LIMIT, a density beyond. */
const markStyle = (n) => n <= 500 ? { size: 7, opacity: 1 }
	: { size: 5, opacity: 0.3 };

/* A layer that is only faint context behind the data ("All runs" behind
   per-iteration medians) keeps these at any n: the density rule is for the
   data. */
const CONTEXT_MARKS = { size: 4, opacity: 0.3 };

/* A context layer draws in its data's own muted hue, faint, never grey, so
   it cannot read as the catch-all group; its legend key is that hue at the
   same opacity. */
const contextMarker = (slot) => Object.assign({ color: resolved(slot) },
	CONTEXT_MARKS);

/* Emphasis is colour plus size: an emphasised point (selected, or in an
   emphasised category) is its hue at full strength, opaque, and 2px larger
   than the trace's density-rule size; the rest fade to 0.08. */
const emphasisStyle = (slot, marks) => ({
	selected: { marker: { opacity: 1, color: emphasis(slot),
		size: marks.size + 2 } },
	unselected: { marker: { opacity: 0.08 } },
});

/* Past DENSITY_LIMIT points: counts per bin on the sequential scale,
   binned in the axis's own space so a log axis gets log-width bins. The
   caption says it is a density and gives n. */
function densityTrace(xs, ys, xType, yType) {
	const keep = xs.map((x, i) => [x, ys[i]]).filter(([x, y]) => !missing(x)
		&& !missing(y) && (xType !== 'log' || x > 0) && (yType !== 'log' || y > 0));

	return {
		type: 'histogram2d',
		x: keep.map(([x]) => xType === 'log' ? Math.log10(x) : x),
		y: keep.map(([, y]) => yType === 'log' ? Math.log10(y) : y),
		colorscale: sequentialScale(), nbinsx: 60, nbinsy: 60,
		showscale: false, hovertemplate: 'n = %{z}<extra></extra>',
	};
}

/* ==== SECTION: tooltip ==== */

function showTip(event, children) {
	const tip = $('tip');
	if (!tip) return;

	tip.replaceChildren(...children);
	tip.classList.add('show');

	const bounds = ROOT.getBoundingClientRect();
	const rect = tip.getBoundingClientRect();
	let left = event.clientX - bounds.left + 14;
	if (left + rect.width > bounds.width) left = event.clientX - bounds.left
		- rect.width - 14;

	// Flip above the pointer rather than running off the bottom.
	const below = event.clientY - bounds.top + 14;
	const above = event.clientY - bounds.top - rect.height - 10;

	tip.style.left = Math.max(4, left) + 'px';
	tip.style.top = (below + rect.height > bounds.height && above > 0
		? above : below) + 'px';
}

function hideTip() {
	const tip = $('tip');
	if (tip) tip.classList.remove('show');
}

/* ==== SECTION: gpu ==== */

/* Utilisation over the samples the publisher kept, as a Tufte sparkline:
   the line, a dot at its minimum and maximum, and the last value at the
   right end. The card's numbers are a snapshot; this says whether the GPU
   has been busy or just was at the poll. The line stretches to the width;
   the dots are HTML so they stay round. */
function sparkline(values, width) {
	const height = 26;
	const ns = 'http://www.w3.org/2000/svg';
	const svg = document.createElementNS(ns, 'svg');
	const holder = el('div', { class: 'spark', role: 'img',
		'aria-label': 'Recent utilisation' }, [svg]);

	svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
	svg.setAttribute('preserveAspectRatio', 'none');
	svg.setAttribute('width', '100%');
	svg.setAttribute('height', String(height));

	const last = el('span', { class: 'spark-last',
		text: values.length ? fmt.pct(values[values.length - 1]) : '' });

	if (values.length < 2) return [holder, last];

	const step = width / (values.length - 1);
	const yOf = (value) => height - ((value || 0) / 100) * height;
	const line = document.createElementNS(ns, 'polyline');

	line.setAttribute('points', values.map((value, i) =>
		`${(i * step).toFixed(1)},${yOf(value).toFixed(1)}`).join(' '));
	line.setAttribute('fill', 'none');
	line.setAttribute('stroke', resolved('--cyan-muted'));
	line.setAttribute('stroke-width', '1.5');
	line.setAttribute('stroke-linejoin', 'round');
	line.setAttribute('vector-effect', 'non-scaling-stroke');
	svg.append(line);

	const low = values.indexOf(Math.min(...values));
	const high = values.indexOf(Math.max(...values));

	for (const [index, kind] of [[low, 'min'], [high, 'max']])
		holder.append(el('span', { class: 'mark', 'data-mark': kind,
			title: `${kind} ${fmt.pct(values[index])}`,
			style: `left:${100 * index / (values.length - 1)}%;`
				+ `top:${100 * yOf(values[index]) / height}%` }));

	return [holder, last];
}

/* One of this run's processes on a GPU: what it is working on first. */
function gpuProcess(proc) {
	return el('div', { class: 'gpu-proc' }, [
		el('div', { class: 'row' }, [
			el('code', { text: proc.target || proc.kind || '(unidentified)' }),
			el('span', { class: 'muted', text: `pid ${proc.pid}` })]),
		el('div', { class: 'row muted' }, [
			el('span', { text: `SM ${fmt.pct(proc.sm)} · `
				+ `${fmt.int(proc.gpu_memory)} MiB` }),
			el('span', { text: `CPU ${fmt.pct(proc.cpu_percent)} · `
				+ fmt.bytes(proc.rss) })]),
		el('div', { class: 'row muted' }, [
			el('span', { text: proc.kind || 'process' }),
			el('span', { text: fmt.duration(proc.elapsed) })]),
	]);
}

/* A visible drag bar for a horizontally scrolling strip, because the native
   scrollbar is an overlay with nothing to grab. Hidden when the strip fits;
   generic over any (strip, bar, thumb). */
function wireDragBar(stripId, barId, thumbId) {
	const strip = $(stripId);
	const bar = $(barId);
	const thumb = $(thumbId);
	if (!strip || !bar || !thumb) return;

	const sync = () => {
		// Not laid out: the cell is scrolled out of a windowed notebook or
		// collapsed. A zero width there would show the bar.
		if (!strip.clientWidth) return;

		const ratio = strip.clientWidth / (strip.scrollWidth || 1);
		bar.hidden = ratio >= 0.999;
		if (bar.hidden) return;

		const width = Math.max(24, bar.clientWidth * ratio);
		const scrollable = strip.scrollWidth - strip.clientWidth;

		thumb.style.width = width + 'px';
		thumb.style.left = (scrollable > 0 ? strip.scrollLeft / scrollable
			* (bar.clientWidth - width) : 0) + 'px';
	};

	if (!bar._wired) {
		bar._wired = true;

		const to = (clientX) => {
			const rect = bar.getBoundingClientRect();
			const width = thumb.getBoundingClientRect().width;
			const fraction = Math.max(0, Math.min(1, (clientX - rect.left
				- width / 2) / Math.max(1, rect.width - width)));
			strip.scrollLeft = fraction * (strip.scrollWidth - strip.clientWidth);
		};

		bar.addEventListener('mousedown', (event) => {
			bar.dataset.dragging = 'true';
			to(event.clientX);
			event.preventDefault();
		});
		window.addEventListener('mousemove', (event) => {
			if (bar.dataset.dragging === 'true') to(event.clientX);
		});
		window.addEventListener('mouseup', () => { bar.dataset.dragging = 'false'; });
		strip.addEventListener('scroll', sync);
		window.addEventListener('resize', sync);
		if (typeof ResizeObserver !== 'undefined') new ResizeObserver(sync).observe(strip);
	}

	sync();
}

/* The GPU strip's height only grows while the page is mounted. A card
   gains or loses a process block as jobs come and go, and every change in
   the strip's height moves everything below it; when the strip is above
   the viewport, that is the whole notebook jumping. */
function holdHeight(id) {
	const node = $(id);
	if (!node || !node.offsetHeight) return;

	const tallest = Math.max(node.offsetHeight, parseFloat(node.style.minHeight) || 0);
	node.style.minHeight = tallest + 'px';
}

const BADGE = { ours: 'this run', leased: 'leased', busy: 'other',
	free: 'free' };

function gpuCard(gpu, history) {
	const processes = gpu.processes || [];
	const mine = processes.filter((p) => p.mine);
	const others = processes.length - mine.length;
	const series = history.map((point) => (point.gpu || {})[gpu.index])
		.filter((value) => !missing(value));

	// Whose lease it is, when a lease tool holds it and none of ours is on it.
	const lease = gpu.lease && !gpu.mine ? el('div', { class: 'gpu-proc muted',
		title: gpu.lease.note || '' }, [el('div', { class: 'row' }, [
		el('span', { text: `${gpu.lease.user || '?'} — `
			+ `${gpu.lease.note || 'no note'}` })])]) : null;

	const idle = processes.length || lease ? null : el('div', {
		class: 'gpu-proc muted', text: 'idle' });

	return el('div', { class: 'gpu-card', 'data-state': gpu.state }, [
		el('div', { class: 'gpu-head' }, [
			el('span', { class: 'gpu-title', text: `GPU ${gpu.index}` }),
			el('span', { class: 'badge', 'data-state': gpu.state,
				text: BADGE[gpu.state] || gpu.state })]),
		el('div', { class: 'gpu-util', text: fmt.pct(gpu.utilization) }),
		// One span per stat: `gap` does not apply to bare text nodes.
		el('div', { class: 'gpu-stats' }, [
			`${fmt.int(gpu.memory_used)} / ${fmt.int(gpu.memory_total)} MiB`,
			missing(gpu.temperature) ? null : `${fmt.int(gpu.temperature)} °C`,
			missing(gpu.power) ? null
				: `${fmt.int(gpu.power)} / ${fmt.int(gpu.power_limit)} W`,
			gpu.name,
		].filter(Boolean).map((text) => el('span', { text, title: text }))),
		el('div', { class: 'gpu-spark' }, sparkline(series, 100)),
		...mine.map(gpuProcess),
		others ? el('div', { class: 'gpu-proc muted', text: `${others} `
			+ `process${others > 1 ? 'es' : ''} from another job` }) : null,
		lease,
		idle,
	]);
}

/* A process that only mentions the run and does nothing is noise. */
const interesting = (proc) => proc.kind !== 'other' || proc.target
	|| ((proc.tree || {}).cpu_percent || 0) > 1
	|| ((proc.tree || {}).rss || 0) > 2e8;

/* How long a GPU reading may lag the poll before the cards say so: a few
   hardware samples, since nvidia-smi on a busy host takes seconds. */
const GPU_STALE_SECONDS = 30;

function renderResources(resources) {
	const host = resources.host || {};

	// A sample with no GPUs after ones with GPUs is nvidia-smi failing or
	// timing out (writers before the shared sample published that as an
	// empty list), not the GPUs leaving: keep the last cards, and say how
	// old they are, rather than drop the strip and jump the notebook.
	let gpus = resources.gpus || [];
	let sampledAt = resources.gpu_sampled_at;

	if (gpus.length) {
		state.lastGpus = { gpus, sampledAt: sampledAt || resources.timestamp };
	} else if (state.lastGpus) {
		({ gpus, sampledAt } = state.lastGpus);
	}

	const age = missing(sampledAt) || missing(resources.timestamp) ? 0
		: resources.timestamp - sampledAt;
	const stale = gpus.length && age > Math.max(GPU_STALE_SECONDS,
		3 * cadence('resource') / 1000);

	// The host first: two machines can publish one search, and the strip
	// shows the GPUs of whichever one is publishing.
	$('host-summary').textContent = (resources.hostname
		? `${resources.hostname} · ` : '') + (host.cpu_count
		? `${fmt.pct(host.cpu_percent)} of ${host.cpu_count} cores · load `
			+ `${(host.load || []).map((x) => x.toFixed(1)).join(' ')} · `
			+ `${fmt.bytes(host.memory_used)} of ${fmt.bytes(host.memory_total)} RAM`
		: 'psutil is not installed, so the host summary is unavailable.')
		+ (stale ? ` · GPU reading ${fmt.duration(age)} old: nvidia-smi is `
			+ 'slow or failing' : '');

	const cards = fill('gpu-cards', ...gpus.map((gpu) =>
		gpuCard(gpu, resources.history || [])));
	if (cards) cards.dataset.stale = stale ? 'true' : 'false';
	wireDragBar('gpu-cards', 'gpu-scroll', 'gpu-scroll-thumb');
	holdHeight('gpu-cards');

	const all = resources.processes || [];
	const shown = all.filter(interesting);
	const hidden = all.length - shown.length;

	// Without psutil the table is empty because nothing can be listed, not
	// because nothing runs: say so.
	const blind = resources.psutil === false;
	$('process-card').hidden = !shown.length && !blind;
	$('process-note').textContent = blind
		? 'psutil is not installed, so this search\'s processes cannot be listed'
		: hidden ? `${hidden} idle process${hidden > 1 ? 'es' : ''} hidden` : '';

	fill('process-body', ...shown.map((proc) => el('tr', {}, [
		el('td', { class: 'num', text: String(proc.pid) }),
		el('td', { text: proc.kind }),
		el('td', {}, [el('code', { text: proc.target || '–' })]),
		el('td', { class: 'num', text: fmt.pct((proc.tree || {}).cpu_percent) }),
		el('td', { class: 'num', text: fmt.bytes((proc.tree || {}).rss) }),
		el('td', { class: 'num', text: fmt.duration(proc.elapsed) })])));
}

/* ==== SECTION: lineage-tree ==== */

/* A search lineage as an SVG tree, root at the top, one row per generation.

   nodes: [{id, parent, status, category, value, title, on_frontier, is_root}]
   status is ok | gate-failed | running; depth is computed from `parent`.

   Past LINEAGE_MANY nodes the tree is condensed: roots, the frontier path,
   anything running and the newest LINEAGE_RECENT are drawn, and the rest
   stand in as one tally per (parent, depth). 2,600 iterations forked from
   one parent otherwise draw one 119,000 px row. Nodes keep a legible
   spacing (LINEAGE_MIN_GAP) and the panel scrolls rather than squeezing.

   Ink: nodes are plain discs in their category's muted hue; the frontier is
   the same hue at full strength, not a ring. Hovering a node lights its
   ancestry and pushes everything else back. */
const LINEAGE_MANY = 120;
const LINEAGE_RECENT = 12;
const LINEAGE_GAP = 46;
const LINEAGE_MIN_GAP = 34;
const LINEAGE_ROW = 72;
const LINEAGE_PAD = 26;
const LINEAGE_GUTTER = 40;
const LINEAGE_R = 7;
const LINEAGE_LABEL = 20;   // the id label's baseline, below the node's centre

function lineageDepths(nodes) {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const depth = new Map();

	const walk = (id, seen) => {
		if (depth.has(id)) return depth.get(id);
		const node = byId.get(id);
		const parent = node ? node.parent : null;
		const d = (node === undefined || parent === null || parent === undefined
			|| parent === id || seen.has(parent) || !byId.has(parent))
			? 0 : walk(parent, new Set(seen).add(id)) + 1;
		depth.set(id, d);
		return d;
	};

	for (const node of nodes) node.depth = walk(node.id, new Set());
	return nodes;
}

function condenseLineage(nodes) {
	if (nodes.length <= LINEAGE_MANY) return { nodes, collapsed: 0 };

	const newest = new Set(nodes.map((n) => n.id).sort((a, b) => a - b)
		.slice(-LINEAGE_RECENT));
	const keep = [];
	const dropped = new Map();

	for (const node of nodes) {
		if (node.is_root || node.on_frontier || node.status === 'running'
			|| newest.has(node.id)) {
			keep.push(node);
		} else {
			const key = node.parent + '|' + node.depth;
			if (!dropped.has(key)) dropped.set(key, []);
			dropped.get(key).push(node);
		}
	}

	let collapsed = 0;
	for (const group of dropped.values()) {
		collapsed += group.length;
		const values = group.map((n) => n.value).filter((v) => !missing(v));
		keep.push({ id: null, parent: group[0].parent, depth: group[0].depth,
			status: 'collapsed', n: group.length,
			n_ok: group.filter((n) => n.status === 'ok').length,
			best: values.length ? Math.max(...values) : null });
	}

	return { nodes: keep, collapsed };
}

/* The chain from the root down to `id`, root first. */
function lineagePath(nodes, id) {
	const byId = new Map(nodes.map((n) => [n.id, n]));
	const path = [];

	for (let at = id; at !== null && at !== undefined && byId.has(at)
		&& path.length < 200; at = byId.get(at).parent)
		path.unshift(byId.get(at));

	return path;
}

function renderLineage(holderId, mapId, allNodes, options) {
	const holder = $(holderId);
	if (!holder) return;

	options = options || {};
	const colourSlot = options.slotOf || (() => '--cat-other');
	const hidden = options.hidden || new Set();
	const emphasised = options.emphasised || new Set();
	const full = lineageDepths(allNodes.map((n) => Object.assign({}, n)));
	const { nodes, collapsed } = condenseLineage(full);

	if (!nodes.length) {
		holder.replaceChildren(el('div', { class: 'empty', text: 'No iterations yet.' }));
		return { collapsed: 0 };
	}

	const depth = Math.max(...nodes.map((n) => n.depth));
	const rows = [];
	for (let level = 0; level <= depth; level++)
		rows.push(nodes.filter((n) => n.depth === level)
			.sort((a, b) => (a.id === null) - (b.id === null) || a.id - b.id));

	const widest = Math.max(...rows.map((r) => r.length), 1);
	const available = Math.max((holder.clientWidth || 900) - 2 * LINEAGE_PAD
		- LINEAGE_GUTTER, 320);
	const gap = widest > 1 ? Math.max(LINEAGE_MIN_GAP,
		Math.min(LINEAGE_GAP, available / (widest - 1))) : LINEAGE_GAP;
	const keyOf = (n) => n.id === null ? `c${n.depth}-${n.parent}` : `i${n.id}`;
	const labelled = gap >= 26;

	let width = Math.max(LINEAGE_GUTTER + 2 * LINEAGE_PAD + (widest - 1) * gap, 520);
	const height = 2 * LINEAGE_PAD + (depth + 1) * LINEAGE_ROW;
	const place = {};

	rows.forEach((row, level) => {
		const left = LINEAGE_GUTTER + (width - LINEAGE_GUTTER) / 2
			- (row.length - 1) * gap / 2;
		row.forEach((n, i) => {
			place[keyOf(n)] = { x: left + i * gap, y: LINEAGE_PAD + level * LINEAGE_ROW };
		});
	});

	// Centre each parent over its children, deepest row first; otherwise a
	// lone root sits mid-canvas, off the edge of the panel.
	for (let level = rows.length - 1; level > 0; level--) {
		const children = {};
		for (const n of rows[level]) {
			const at = place[keyOf(n)];
			if (n.parent !== null && n.parent !== undefined && at)
				(children[n.parent] = children[n.parent] || []).push(at.x);
		}
		for (const n of rows[level - 1]) {
			const xs = children[n.id];
			if (xs && xs.length) place[keyOf(n)].x = xs.reduce((a, b) => a + b) / xs.length;
		}
	}

	// Then push apart anything centring dropped onto a neighbour.
	for (const row of rows) {
		const ordered = row.map((n) => place[keyOf(n)]).sort((a, b) => a.x - b.x);
		for (let i = 1; i < ordered.length; i++)
			ordered[i].x = Math.max(ordered[i].x, ordered[i - 1].x + gap);
		if (ordered.length) width = Math.max(width, ordered[ordered.length - 1].x + LINEAGE_PAD);
	}

	const NS = 'http://www.w3.org/2000/svg';
	const make = (tag, attrs) => {
		const node = document.createElementNS(NS, tag);
		for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
		return node;
	};

	const svg = make('svg', { width: Math.ceil(width), height,
		viewBox: `0 0 ${Math.ceil(width)} ${height}`, class: 'lineage' });

	const colourOf = (n, strong) => n.status === 'running'
		? resolved('--running')
		: strong ? emphasis(colourSlot(n.category)) : resolved(colourSlot(n.category));

	// Edges first, stopping short of each disc so a fork does not trace a
	// ring around its parent.
	for (const n of nodes) {
		const child = place[keyOf(n)];
		const parent = n.parent === null || n.parent === undefined ? null
			: place[`i${n.parent}`];
		if (!child || !parent) continue;

		// Below the parent's id label when labels are drawn (its baseline is
		// 20px under the centre), else just off the disc: an edge from the
		// disc runs through the label.
		const from = parent.y + (labelled ? LINEAGE_LABEL + 4 : LINEAGE_R + 3);
		const to = child.y - LINEAGE_R - 3;
		const mid = (from + to) / 2;

		svg.append(make('path', { class: 'lineage-edge',
			d: `M ${parent.x} ${from} C ${parent.x} ${mid}, ${child.x} ${mid}, ${child.x} ${to}`,
			stroke: n.status === 'collapsed' ? resolved('--line') : colourOf(n, n.on_frontier),
			'stroke-opacity': n.on_frontier ? 1 : 0.45,
			'data-node-id': n.id === null ? '' : String(n.id),
			'data-dim': String(hidden.has(n.category)
				|| (emphasised.size > 0 && !emphasised.has(n.category))) }));
	}

	for (const n of nodes) {
		const at = place[keyOf(n)];

		if (n.status === 'collapsed') {
			const tally = make('g', { class: 'lineage-node lineage-tally' });
			tally.append(make('rect', { x: at.x - 18, y: at.y - 9, width: 36,
				// A transparent fill, not 'none': an unfilled rect takes no
				// pointer events inside it, so its tooltip never showed.
				height: 18, rx: 6, fill: 'transparent', stroke: resolved('--line'),
				'stroke-dasharray': '3 2' }));
			const count = make('text', { x: at.x, y: at.y + 4 });
			count.textContent = '+' + fmt.int(n.n);
			tally.append(count);
			tally.addEventListener('mouseenter', (event) => showTip(event, [
				el('div', { class: 'head', text: `${fmt.int(n.n)} iterations` }),
				el('div', { text: `forked from #${n.parent}, ${fmt.int(n.n_ok)} ok` }),
				el('div', { text: tipRow('best value', fmt.sig(n.best)) }),
				el('div', { text: 'none on the frontier' })]));
			tally.addEventListener('mouseleave', hideTip);
			svg.append(tally);
			continue;
		}

		// Hidden or outside the emphasis: dimmed, never removed, which would
		// cut the lineage through it.
		const dim = (state.selection && !state.selection.has(n.id))
			|| hidden.has(n.category)
			|| (emphasised.size > 0 && !emphasised.has(n.category));
		const group = make('g', { class: 'lineage-node',
			'data-node-id': String(n.id), 'data-dim': String(Boolean(dim)),
			'data-frontier': String(Boolean(n.on_frontier)) });

		// A larger invisible target, so the pointer need not hit a 7px dot.
		group.append(make('circle', { cx: at.x, cy: at.y, r: 16, fill: 'transparent' }));

		const running = n.status === 'running';
		const radius = n.is_root ? LINEAGE_R + 2 : LINEAGE_R;

		group.append(make('circle', { class: 'dot', cx: at.x, cy: at.y,
			r: radius,
			fill: running ? 'none' : colourOf(n, n.on_frontier || n.is_root),
			'fill-opacity': n.status === 'ok' || running ? 1 : 0.4,
			stroke: running ? colourOf(n) : 'none', 'stroke-dasharray': '3 2' }));

		if (n.status === 'gate-failed')
			group.append(make('path', { d: `M ${at.x - 4} ${at.y - 4} L ${at.x + 4} `
				+ `${at.y + 4} M ${at.x + 4} ${at.y - 4} L ${at.x - 4} ${at.y + 4}`,
				stroke: resolved('--bad'), 'stroke-width': 1.6 }));

		if (labelled) {
			const label = make('text', { x: at.x, y: at.y + LINEAGE_LABEL });
			label.textContent = tag(n.id);
			group.append(label);
		}

		group.addEventListener('mouseenter', (event) => {
			const ids = new Set(lineagePath(full, n.id).map((p) => p.id));
			svg.dataset.hovering = 'true';
			for (const item of svg.querySelectorAll('[data-node-id]'))
				item.dataset.onPath = String(ids.has(Number(item.dataset.nodeId)));

			const path = lineagePath(full, n.id);
			showTip(event, [
				el('div', { class: 'head', text: `${tag(n.id)} ${n.title || ''}` }),
				...path.slice(0, -1).map((p) => el('div', { class: 'dim',
					text: `  ${tag(p.id)} ${p.title || ''}` })),
				el('div', { text: tipRow('value', fmt.sig(n.value)) }),
				el('div', { text: `${n.category} · ${n.status}`
					+ (n.on_frontier ? ' · frontier' : '') })]);
			if (options.onHover) options.onHover(n.id);
		});
		group.addEventListener('mouseleave', () => {
			delete svg.dataset.hovering;
			hideTip();
			if (options.onHover) options.onHover(null);
		});
		group.addEventListener('click', () => options.onPin && options.onPin(n.id));

		svg.append(group);
	}

	rows.forEach((row, level) => {
		const text = make('text', { x: 4, y: LINEAGE_PAD + level * LINEAGE_ROW + 4,
			class: 'lineage-rowlabel' });
		text.textContent = level === 0 ? 'root' : 'gen ' + level;
		svg.append(text);
	});

	holder.replaceChildren(svg);

	// One dot per generation with its size; clicking scrolls the panel to
	// that row, centred on its best node.
	if (mapId && $(mapId)) fill(mapId, ...rows.map((row, level) => {
		const best = row.filter((n) => n.id !== null && !missing(n.value))
			.sort((a, b) => b.value - a.value)[0];
		return el('button', { class: 'lineage-level', type: 'button',
			title: `${level === 0 ? 'root' : 'generation ' + level}: `
				+ `${row.length} node(s)`,
			onclick: () => {
				const at = best ? place[keyOf(best)] : place[keyOf(row[0])];
				holder.scrollTo({ left: at.x - holder.clientWidth / 2,
					top: at.y - holder.clientHeight / 2, behavior: 'smooth' });
			} }, [
			el('span', { class: 'dot', style: `background:${best
				? colourOf(best) : resolved('--line')}` }),
			el('span', { class: 'n', text: fmt.int(row.reduce((a, n) =>
				a + (n.n || 1), 0)) }),
			el('span', { class: 'lbl', text: level === 0 ? 'root' : 'gen ' + level })]);
	}));

	return { collapsed, width: Math.ceil(width) };
}

/* ==== SECTION: expandable-log ==== */

/* Newest-first entries on one line each, under a header row sharing the
   same fixed-width grid (an `auto` column would resolve separately in each
   row and the header would drift off the values). Each entry has a left
   rule in its category's colour; a frontier entry shows its id at full
   strength. Clicking expands the detail and calls `onPin(id)`.

   columns: [{key, label, width, format}] -- widths in px, the last column
   (the title) takes the rest. detail(row) returns [[key, value]] pairs, or a
   DOM Node to insert as it is (a README, a small plot); the Node is kept
   while the entry stays open, so the log's own re-render (another entry
   clicked, a chip toggled) re-inserts it rather than calling detail again.
   Rows need a numeric `id`: entries sort newest first by `b.id - a.id`, so
   string ids give an unordered log with no error. onPin(id, opened) fires
   on every click, with whether the entry is now open. `options.hidden`
   removes a category's entries; `options.emphasised`, when non-empty, fades
   every entry outside it. */
const logOpen = {};
const logDetails = {};

function logDetailBlock(details, row, detail) {
	if (details.has(row.id)) return details.get(row.id);

	// Pairs are rebuilt on every render, so live values stay current; a
	// Node is built once and kept while the entry is open.
	const made = detail(row);
	if (made && made.nodeType) {
		const node = el('div', { class: 'log-detail' }, [made]);
		details.set(row.id, node);
		return node;
	}

	// A key may be a Node (a label with its own colour) or plain text.
	return el('div', { class: 'log-detail pairs' }, made.map(([key, value, title]) =>
		el('div', { class: 'pair' }, [
			el('span', { class: 'k' }, [key]),
			el('span', { class: 'v', text: value, title })])));
}

function renderLog(holderId, rows, columns, options) {
	const holder = $(holderId);
	if (!holder) return;

	options = options || {};
	const open = logOpen[holderId] = logOpen[holderId] || new Set();
	const details = logDetails[holderId] = logDetails[holderId] || new Map();
	const hidden = options.hidden || new Set();
	const emphasised = options.emphasised || new Set();
	const visibleRows = rows.filter((row) => !hidden.has(row.category))
		.sort((a, b) => b.id - a.id);

	holder.style.setProperty('--log-cols', columns.map((c) => c.width
		? c.width + 'px' : 'minmax(0, 1fr)').join(' '));

	if (!visibleRows.length) {
		holder.replaceChildren(el('div', { class: 'empty', text: 'No entries yet.' }));
		return;
	}

	const cell = (column, row) => {
		const value = row[column.key];
		return el('span', { class: 'm' + (column.num ? ' num' : ''),
			style: column.colour ? `color:${column.colour(value, row)}` : null,
			text: column.format ? column.format(value, row)
				: missing(value) ? '–' : String(value) });
	};
	// The row's rule colour: its category's.
	const slotOfRow = (row) => (options.slotOf || (() => '--cat-other'))(
		row.category);

	holder.replaceChildren(
		el('div', { class: 'log-header' }, columns.map((c) =>
			el('span', { class: c.num ? 'num' : null, text: c.label }))),
		...visibleRows.map((row) => {
			const colour = resolved(slotOfRow(row));
			const isOpen = open.has(row.id);
			const faded = emphasised.size > 0 && !emphasised.has(row.category);

			return el('div', { class: 'log-entry', dataset: { id: String(row.id),
				open: String(isOpen), frontier: String(Boolean(row.on_frontier)),
				faded: String(faded) },
				style: `border-left-color:${colour}`,
				onclick: (event) => {
					// A click inside an inserted detail Node (a link, a
					// selection of text) is not a click on the entry.
					if (event.target.closest('.log-detail')) return;
					if (open.has(row.id)) { open.delete(row.id); details.delete(row.id); }
					else open.add(row.id);
					if (options.onPin) options.onPin(row.id, open.has(row.id));
					renderLog(holderId, rows, columns, options);
				} }, [
				el('div', { class: 'log-row' }, columns.map((c) => cell(c, row))),
				isOpen && options.detail ? logDetailBlock(details, row, options.detail) : null,
			]);
		}));

	// The frontier entry's id at full strength: the one the reader looks for.
	for (const entry of holder.querySelectorAll('.log-entry[data-frontier="true"]')) {
		const row = visibleRows.find((r) => String(r.id) === entry.dataset.id);
		entry.querySelector('.m').style.color = emphasis(slotOfRow(row));
	}
}

/* ==== SECTION: search ==== */

/* The rows every chart reads. Everything is drawn from
   `state.snapshot.iterations`, one row per iteration, so a lasso in any chart
   turns into a set of iteration ids that every other panel can follow. */

const SHORT = { MICRO: 'micro', NUMBA: 'numba', CACHING: 'cache',
	CACHE_MISS: 'miss', PRECISION: 'prec', PARALLEL: 'par', REORGANIZATION: 'reorg',
	IO: 'io', ALGORITHM: 'algo', OPTIMIZATION: 'opt', MISC: 'misc', ROOT: 'root' };

/* Marker shape carries the iteration's status; colour carries its category. */
const STATUS_SYMBOL = { ok: 'circle', 'gate-failed': 'x', error: 'circle-open',
	partial: 'square-open' };

const catSlot = (category) => groupSlot(category);

function settled() {
	return state.snapshot ? state.snapshot.iterations
		.filter((row) => row.status !== 'running') : [];
}

/* In-flight iterations, honouring the chips: a chip that hid an iteration's
   points but left its in-flight marker would make the filter look broken. */
function running() {
	return state.snapshot ? state.snapshot.iterations.filter((row) =>
		row.status === 'running' && !state.hidden.has(row.category)) : [];
}

/* Settled iterations the chips and "Gate failures" have not hidden. A hidden row is gone from every chart at once. */
function visible() {
	return settled().filter((row) => !state.hidden.has(row.category)
		&& !(state.hideFailed && row.status !== 'ok'));
}

const plottable = () => visible().filter((row) => !missing(row.value));

function byId(identifier) {
	return (state.snapshot ? state.snapshot.iterations : [])
		.find((row) => row.id === identifier) || null;
}

/* Where an iteration sits on the x axis: its id. */
const xOf = (row) => row.id;

/* The frontier staircase: the running best, one step per improvement. */
const frontierSteps = () => state.snapshot.frontier.steps || [];

/* Break `text` into at most `limit` lines of about `width` characters,
   ending with an ellipsis when there was more. */
function wrapText(text, width, limit) {
	const words = text.split(' ').filter(Boolean);
	const lines = [];
	let current = '';

	for (const word of words) {
		if ((current + ' ' + word).trim().length > width) {
			lines.push(current.trim());
			current = word;
			if (lines.length === limit) break;
		} else {
			current = (current + ' ' + word).trim();
		}
	}

	if (lines.length < limit && current) lines.push(current.trim());
	if (lines.length && words.join(' ').length > lines.join(' ').length)
		lines[lines.length - 1] += '…';

	return lines;
}

/* The aligned hover block for one iteration: what it is, what it scored,
   what it was held to, what else was recorded, where it sits, and the top
   of its README last, so the README is what a short plot cuts. */
function hoverText(row, plotId) {
	const snapshot = state.snapshot;
	const stats = row.objective || {};
	const baseline = snapshot.frontier.baseline;
	const title = (row.title || '(untitled)');

	const notes = row.notes ? wrapText(row.notes.replace(/^#+ .*$/m, '')
		.replace(/[*`|#]/g, '').replace(/\s+/g, ' ').trim(), 44, 2)
		.map((line) => '<i>' + escape(line) + '</i>')
		: ['<i>no README.md written</i>'];

	return tipBlock([
		[`<b>${tag(row.id)} ${escape(title.length > 44 ? title.slice(0, 43) + '…'
			: title)}</b>`,
			tipRow('category', SHORT[row.category] || row.category),
			minedStages() && !missing(row.mining_stage)
				? tipRow('miner stage', 'S' + row.mining_stage) : null,
			tipRow('status', row.status + (row.stage ? ' · ' + row.stage : '')),
			row.working ? tipRow('subagent', 'working: checks and README pending, last '
				+ 'activity ' + (missing(row.idle) ? '–' : fmt.duration(row.idle) + ' ago'))
				: null,
			row.on_frontier ? tipRow('frontier', 'yes') : null],
		[tipRow(snapshot.objective.name, fmt.sig(row.value)),
			tipRow('sd', fmt.sig(stats.std)),
			tipRow('min', fmt.sig(stats.min)), tipRow('max', fmt.sig(stats.max)),
			stats.ci ? tipRow('95% CI of median', `${fmt.sig(stats.ci[0])} – `
				+ `${fmt.sig(stats.ci[1])}`) : null,
			tipRow('runs', row.n_runs),
			missing(baseline) || missing(row.value) ? null
				: tipRow('vs baseline', fmt.signed(row.value - baseline, 4)),
			row.n_failed ? tipRow('failed runs', row.n_failed) : null],
		Object.entries(row.gates || {}).map(([name, gate]) =>
			tipRow(`${name} ${gate.op} ${fmt.sig(gate.threshold)}`,
				`${gate.pass ? 'pass' : 'FAIL'} ${fmt.sig(gate.worst)}`)),
		Object.entries(row.observers || {}).map(([name, observer]) =>
			tipRow(name, fmt.sig(observer.median))),
		diffTipRows(row),
		[tipRow('parent', tag(row.parent)),
			tipRow('round', row.round), tipRow('wall clock', fmt.duration(row.seconds)),
			row.topic ? tipRow('topic', row.topic) : null,
			row.best_run ? tipRow('best run', row.best_run) : null],
		notes,
	], plotId);
}

/* ==== SECTION: objective ==== */

selectionListeners.push(() => renderMain());

/* Grouped by category. The frontier is its hue at full strength; everything
   else is muted. */
function medianTraces(rows, yOf, plotId) {
	const groups = [...new Set(rows.map((row) => row.category))].sort().map(
		(category) => ({ key: category, slot: catSlot(category),
			rows: rows.filter((row) => row.category === category) }));

	const marks = markStyle(rows.length);

	return groups.filter((g) => g.rows.length).map((g) => ({
		type: 'scatter', mode: 'markers', name: g.key,
		x: g.rows.map(xOf), y: g.rows.map(yOf),
		text: g.rows.map((row) => hoverText(row, plotId)),
		customdata: g.rows.map((row) => row.id),
		hovertemplate: HOVER,
		selectedpoints: selectedIndices(g.rows, (row) => row.category),
		...emphasisStyle(g.slot, marks),
		// The frontier is emphasis too: full strength, opaque, 2px larger.
		marker: {
			color: g.rows.map((row) => row.on_frontier ? emphasis(g.slot)
				: resolved(g.slot)),
			// A subagent still working on a measured iteration (checks and
			// README pending): larger, opaque, and a thick ring in its category's colour,
			// so a search whose runs finished does not look idle.
			size: g.rows.map((row) => row.working ? marks.size + 6
				: row.on_frontier ? marks.size + 2 : marks.size),
			opacity: g.rows.map((row) => row.on_frontier || row.working ? 1
				: marks.opacity),
			symbol: g.rows.map((row) => STATUS_SYMBOL[row.status] || 'circle'),
			line: { width: g.rows.map((row) => row.working ? 3 : 1.2),
				color: g.rows.map((row) => row.working
					? emphasis(g.slot)
					: row.on_frontier ? emphasis(g.slot) : resolved(g.slot)) },
		},
	}));
}

/* Iterations set aside without a measured run (`iterations/<id>.abandoned`,
   scan.py's `abandoned`), honouring the chips and the hide-failed toggle like
   every other point. */
function abandonedRows() {
	return ((state.snapshot && state.snapshot.abandoned) || []).filter((row) =>
		!state.hidden.has(row.category) && !state.hideFailed);
}

/* An abandoned iteration has no objective, so it is drawn at the baseline's
   value: a thin ✕ in its category's hue at its own x, one trace per category so
   chip emphasis colours it like the medians. Shape is the status here as
   everywhere on this chart; the thin ✕ is not the gate-failed ✕. */
function abandonedTraces(rows, y, total) {
	const marks = markStyle(total);
	// Grouped as the medians are, by category, so each trace is one colour.
	const groups = [...new Set(rows.map((row) => row.category))].sort().map(
		(category) => ({ key: category, slot: catSlot(category),
			rows: rows.filter((row) => row.category === category) }));

	return groups.filter((g) => g.rows.length).map((g) => ({
		type: 'scatter', mode: 'markers', name: 'abandoned ' + g.key,
		x: g.rows.map(xOf), y: g.rows.map(() => y),
		text: g.rows.map(abandonedHover),
		// No customdata: there is no measured iteration behind the mark, so
		// hovering or clicking it draws and pins nothing.
		hovertemplate: HOVER, showlegend: false,
		selectedpoints: selectedIndices(g.rows, (row) => row.category),
		...emphasisStyle(g.slot, marks),
		marker: { symbol: 'x-thin-open', size: marks.size + 4, color: resolved(g.slot),
			opacity: marks.opacity, line: { width: 1.8, color: resolved(g.slot) } },
	}));
}

function abandonedHover(row) {
	const title = row.title || '(untitled)';
	const notes = row.notes ? wrapText(row.notes.replace(/^#+ .*$/m, '')
		.replace(/[*`|#]/g, '').replace(/\s+/g, ' ').trim(), 44, 3)
		.map((line) => '<i>' + escape(line) + '</i>')
		: ['<i>no README.md written</i>'];

	return tipBlock([
		[`<b>${tag(row.id)} ${escape(title.length > 44 ? title.slice(0, 43) + '…'
			: title)}</b>`,
			tipRow('category', SHORT[row.category] || row.category),
			tipRow('status', 'abandoned: closed without a measured run'),
			tipRow('drawn at', 'the baseline\'s value')],
		[tipRow('parent', tag(row.parent)), tipRow('round', row.round),
			row.topic ? tipRow('topic', row.topic) : null],
		notes,
	], 'main-chart');
}

/* Each median's confidence interval (scan.py's bootstrap of the median over
   its runs) as a dotted vertical whisker with a cap at each end, one trace per
   category in its muted hue, under the medians. An iteration with fewer than
   two runs has no interval and draws none. */
function ciTraces(rows) {
	return [...new Set(rows.map((row) => row.category))].sort().map((category) => {
		const x = [], y = [];

		for (const row of rows.filter((r) => r.category === category)) {
			const ci = (row.objective || {}).ci;
			if (!ci || missing(ci[0]) || missing(ci[1])) continue;
			x.push(xOf(row), xOf(row), null);
			y.push(ci[0], ci[1], null);
		}

		return { type: 'scatter', mode: 'lines+markers', name: 'ci ' + category,
			x, y, hoverinfo: 'skip', showlegend: false, connectgaps: false,
			line: { color: resolved(catSlot(category)), width: 1.2, dash: 'dot' },
			marker: { symbol: 'line-ew-open', size: 7,
				line: { width: 1.2, color: resolved(catSlot(category)) } } };
	}).filter((trace) => trace.x.length);
}

/* Every run's own score, one small faint dot per seed stacked at its
   iteration's x, under the medians: the spread the median hides. A
   background context layer, so CONTEXT_MARKS at any n, never the density. */
function runTraces(rows) {
	return [...new Set(rows.map((row) => row.category))].sort().map((category) => {
		const x = [], y = [], text = [], owners = [], keys = [];

		for (const row of rows.filter((r) => r.category === category))
			for (const run of row.runs || []) {
				if (missing(run.objective)) continue;
				owners.push(row);
				keys.push(`${row.id}:${run.seed}`);
				x.push(xOf(row));
				y.push(run.objective);
				text.push(tipBlock([[`<b>#${row.id}</b>`, tipRow('seed', run.seed),
					tipRow(state.snapshot.objective.name, fmt.sig(run.objective)),
					tipRow('wall clock', fmt.duration(run.seconds))]], 'main-chart'));
			}

		// Follows chip emphasis and the selection like the medians, but stays
		// context: an emphasised run keeps CONTEXT_MARKS, the rest nearly vanish.
		return { type: 'scatter', mode: 'markers', name: 'runs ' + category, x, y,
			text, customdata: keys, hovertemplate: HOVER, showlegend: false,
			selectedpoints: selectedIndices(owners, (row) => row.category),
			selected: { marker: { opacity: CONTEXT_MARKS.opacity } },
			unselected: { marker: { opacity: 0.04 } },
			marker: contextMarker(catSlot(category)) };
	}).filter((trace) => trace.x.length);
}

/* Tree depth by id: the baseline is 0, each iteration one below its parent.
   The same generations the search tree draws its rows by. */
function treeDepths() {
	const rows = state.snapshot ? state.snapshot.iterations : [];
	const parent = new Map(rows.map((row) => [row.id, row.parent]));
	const depth = new Map();

	const walk = (id, seen) => {
		if (depth.has(id)) return depth.get(id);
		const up = parent.get(id);
		// A missing or cyclic parent roots its own tree rather than looping.
		const d = up === null || up === undefined || !parent.has(up) || seen.has(up)
			? 0 : walk(up, seen.add(id)) + 1;
		depth.set(id, d);
		return d;
	};

	for (const row of rows) walk(row.id, new Set());
	return depth;
}

/* Whether the search records idea-miner stages. A search begun before the
   miner has none, and its boundaries fall back to tree generations. */
const minedStages = () => ((state.snapshot || {}).iterations || []).some(
	(row) => !missing(row.mining_stage) && row.mining_stage > 0);

/* Where the objective chart draws a stage boundary: halfway between two
   neighbouring iterations (in x order, running ones
   included) whose idea-miner stage differs, so the chart shows whether a
   fresh batch of ideas moved the objective. The baseline counts as stage 0.
   Without stages, whose tree generations differ. Structural, so the chips
   and "Gate failures" do not move it. */
function stageBoundaries() {
	const mined = minedStages();
	const depth = mined ? new Map(((state.snapshot || {}).iterations || []).map(
		(row) => [row.id, row.mining_stage || 0])) : treeDepths();
	const out = [];

	const rows = (state.snapshot ? state.snapshot.iterations : []).slice()
		.sort((a, b) => xOf(a) - xOf(b));

	for (let i = 1; i < rows.length; i++)
		if (depth.get(rows[i].id) !== depth.get(rows[i - 1].id))
			out.push({ x: (xOf(rows[i - 1]) + xOf(rows[i])) / 2,
				generation: depth.get(rows[i].id) });

	return out;
}

function renderMain() {
	const snapshot = state.snapshot;
	if (!snapshot) return;

	const rows = plottable();
	const ys = rows.map((row) => row.value);
	const yType = scaleOf('main', 'y', ys, true);
	const dense = rows.length > DENSITY_LIMIT;
	const xType = dense ? 'linear' : scaleOf('main', 'x', rows.map(xOf), false);

	const traces = dense
		? [densityTrace(rows.map(xOf), ys, 'linear', yType)]
		: (state.showRuns ? runTraces(rows) : []).concat(
			state.showCI ? ciTraces(rows) : [],
			medianTraces(rows, (row) => row.value, 'main-chart'));

	// A density is binned in log10 already, so its y values are log10 and so
	// is every line drawn over it.
	const yv = (value) => dense && yType === 'log' ? Math.log10(value) : value;

	// The staircase is the running best. It steps only where an iteration
	// improved, so the flat stretches are the search finding nothing, drawn
	// to scale.
	if (state.showStair) {
		const steps = frontierSteps();

		if (steps.length && !(steps.length < 2 && !rows.length)) {
			const xs = steps.map((step) => byId(step.id) ? xOf(byId(step.id)) : step.id);
			// Carried to the last iteration, so a search that stopped
			// improving reads as flat rather than finished.
			const end = rows.length ? Math.max(...rows.map(xOf)) : xs[xs.length - 1];

			traces.unshift({
				type: 'scatter', mode: 'lines', name: 'frontier',
				x: xs.concat([Math.max(end, xs[xs.length - 1])]),
				y: steps.map((s) => yv(s.value)).concat([yv(steps[steps.length - 1].value)]),
				line: { color: resolved('--pink'), width: 1.6, shape: 'hv',
					dash: 'dot' },
				hoverinfo: 'skip', showlegend: false,
			});
		}
	}

	// Still running: no objective yet, so drawn on the floor of the chart.
	const inFlight = running();

	if (inFlight.length && rows.length) {
		const floor = Math.min(...ys);

		traces.push({
			type: 'scatter', mode: 'markers', name: 'running',
			x: inFlight.map(xOf), y: inFlight.map(() => yv(floor)),
			text: inFlight.map((row) => tipBlock([[`<b>${tag(row.id)} `
				+ `${escape(row.title || '')}</b>`,
				tipRow('running for', fmt.duration(row.elapsed))]], 'main-chart')),
			customdata: inFlight.map((row) => row.id),
			hovertemplate: HOVER, showlegend: false,
			marker: { color: resolved('--running'), size: 9,
				symbol: 'diamond-open',
				line: { width: 1.5, color: resolved('--running') } },
		});
	}

	// Closed without a measured run: a thin ✕ at the baseline's value.
	const closed = abandonedRows();
	const baselineValue = snapshot.frontier.baseline;

	if (!dense && closed.length && rows.length && !missing(baselineValue))
		traces.push(...abandonedTraces(closed, yv(baselineValue), rows.length));

	// The headline, first in reading order: the number the search exists to
	// move, as HTML under the title rather than a Plotly annotation.
	const sep = () => el('span', { class: 'sep', text: '•' });
	const headline = (value, id, percent, against) => missing(value)
		? ['no frontier yet'] : [
			`${snapshot.objective.name} `, el('b', { text: fmt.sig(value) }),
			sep(), 'iteration ', el('b', { text: tag(id) }),
			...(missing(percent) ? [] : [sep(),
				el('b', { text: fmt.signedPct(percent) }), ` over ${against}`])];

	const frontier = scopedFrontier();
	fill('main-headline', ...headline(frontier.value, frontier.id,
		frontier.total_percent, 'baseline'));

	const yLabel = snapshot.objective.name + (snapshot.objective.unit
		? ` (${snapshot.objective.unit})` : '') + ', median per iteration';

	draw('main-chart', traces, plotLayout({
		// x log is manual only (never automatic): the baseline sits at x = 0,
		// which a log axis cannot draw, and the scale button says so.
		xaxis: axis({ type: xType, ...logTicks(traces.flatMap((t) =>
				t.type === 'scatter' ? t.x : []).filter((v) => !missing(v)), xType),
			title: { text: 'iteration',
			font: { size: 11, color: resolved('--text-faint') } } }),
		yaxis: axis({ type: dense ? 'linear' : yType,
			// logTicks fixes the range, so it sees every y any trace draws:
			// runs, the staircases, the in-flight markers on the floor.
			...logTicks(traces.flatMap((t) => t.type === 'scatter' ? t.y : [])
				.filter((v) => !missing(v)), dense ? 'linear' : yType), title: { text:
			(dense && yType === 'log' ? 'log10 ' : '') + yLabel,
			font: { size: 11, color: resolved('--text-faint') } } }),
		margin: { l: 62, r: 16, t: 10, b: 40 },
		// Shapes, not traces: a boundary spans the plot whatever the y range,
		// and cannot be hovered, lassoed or pinned.
		shapes: state.showStages && !dense ? stageBoundaries().map((b) => ({
			type: 'line', xref: 'x', yref: 'paper', x0: b.x, x1: b.x, y0: 0, y1: 1,
			layer: 'below', line: { color: resolved('--purple'), width: 1.2,
				dash: 'dash' } })) : [],
	}), { selectable: true, ids: rows.map((row) => row.id), config: SELECT_CONFIG,
		onClick: pinPoint });

	renderScales('main', dense ? [{ which: 'y', values: ys, logable: true }]
		: [{ which: 'x', values: rows.map(xOf), logable: false },
			{ which: 'y', values: ys, logable: true }], renderMain);
	renderMainLegend(dense, rows.length);
}

/* Clicking a point outlines its runs in Every run. A median's customdata is
   its iteration id; a run dot's is "id:seed". */
function pinPoint(key) {
	// A mark with no iteration behind it (an abandoned one) pins nothing.
	if (key === null || key === undefined) return;
	const [id, seed] = String(key).split(':');
	setPinned(Number(id), seed === undefined ? null : Number(seed));
}

/* A marker key for the status shapes, drawn the way Plotly draws them. */
function statusKey(shape, colour) {
	const hollow = `background:none;border:1.5px solid ${colour};`;
	const styles = {
		circle: `background:${colour};border-radius:50%`,
		hollow: hollow + 'border-radius:50%',
		square: hollow,
		diamond: hollow + 'transform:rotate(45deg) scale(.8)',
		small: `background:${colour};border-radius:50%;width:5px;height:5px;opacity:.6`,
	};

	if (shape === 'x') return markerKey(colour, 'x');

	return el('span', { class: 'mark-key', style: 'display:inline-flex;width:12px;height:12px;'
		+ 'align-items:center;justify-content:center' }, [
		el('span', { style: 'width:9px;height:9px;box-sizing:border-box;'
			+ styles[shape] })]);
}

function renderMainLegend(dense, n) {
	const dim = css('--text-dim');

	if (dense) {
		fill('main-legend', el('span', { class: 'legend-item', text: `density of `
			+ `${fmt.int(n)} iterations, darker to cyan = more iterations per bin` }));
		return;
	}

	fill('main-legend',
		// A context layer's key: its data's muted hue at the layer's opacity.
		state.showRuns ? el('span', { class: 'legend-item', dataset: { key: 'runs' } },
			[markerKey(resolved(CAT_SLOTS[0]), 'circle', CONTEXT_MARKS.opacity),
				'one run (seed), faint, in its category\'s hue']) : null,
		// The staircase is pink; a frontier point is its own hue, at full
		// strength and larger.
		el('span', { class: 'legend-item', dataset: { key: 'frontier-line' } }, [
			markerKey(resolved('--pink'), 'dotted'), 'frontier (running best)']),
		state.showCI ? el('span', { class: 'legend-item', dataset: { key: 'ci' } },
			[markerKey(css('--text-dim'), 'dotted'), '95% confidence interval of the '
				+ 'median: a bootstrap over the iteration\'s runs (at 3 runs, at most '
				+ 'min to max)']) : null,
		state.showStages ? el('span', { class: 'legend-item',
			dataset: { key: 'stage-boundary' } }, [markerKey(resolved('--purple'), 'dashed'),
			minedStages() ? 'stage boundary: the idea miner\'s next batch of ideas'
				: 'stage boundary: the next generation of the search tree']) : null,
		el('span', { class: 'legend-item', dataset: { key: 'frontier-point' } }, [
			el('span', { class: 'mark-key', style: 'display:inline-flex;width:12px;'
				+ 'height:12px;align-items:center;justify-content:center' }, [
				el('span', { style: `width:11px;height:11px;border-radius:50%;`
					+ `background:${css('--text')}` })]),
			'on the frontier: its hue at full strength, 2px larger']),
		el('span', { class: 'legend-item' }, [statusKey('circle', dim), 'passed']),
		// ✕ is gate failed here, an accepted exception to x-for-baseline.
		el('span', { class: 'legend-item' }, [statusKey('x', dim),
			'✕ gate failed']),
		el('span', { class: 'legend-item' }, [statusKey('hollow', dim), 'run errored']),
		el('span', { class: 'legend-item' }, [statusKey('square', dim),
			'under depth (too few runs)']),
		abandonedRows().length ? el('span', { class: 'legend-item',
			dataset: { key: 'abandoned' } }, [markerKey(dim, 'x-thin'),
			'thin ✕ abandoned: closed without a measured run, drawn at the '
				+ 'baseline\'s value']) : null,
		el('span', { class: 'legend-item' }, [statusKey('diamond',
			resolved('--running')), 'still running, on the floor']));
}

/* ==== SECTION: observers ==== */

selectionListeners.push(() => renderObservers());

const observerId = (name) => 'observer-' + String(name).replace(/[^A-Za-z0-9_-]/g, '_');

function observerCard(name) {
	const id = observerId(name);

	return el('figure', { class: 'card reorder-card', dataset: { panel: name } }, [
		el('figcaption', {}, [
			el('span', { class: 'grip', title: 'Drag to reorder', text: '⠿' }),
			el('span', { text: name })]),
		el('div', { class: 'plot', id }),
		el('div', { class: 'plot-scales', id: 'scales-' + id }),
	]);
}

function renderObservers() {
	const snapshot = state.snapshot;
	const holder = $('observer-charts');
	if (!snapshot || !holder) return;

	const configured = (snapshot.observers || []).map((o) => o.name);
	$('section-observers').hidden = !configured.length;
	if (!configured.length) return;

	const names = reorder.order('observers', configured);

	// Built once per arrangement and only react()ed into afterwards.
	if (holder.dataset.keys !== names.join(',')) {
		holder.dataset.keys = names.join(',');
		for (const id of [...plotted]) if (id.startsWith('observer-')) plotted.delete(id);
		holder.replaceChildren(...names.map((name) => {
			const card = observerCard(name);
			reorder.wire(card, 'observers', name, names, renderObservers, 'x');
			return card;
		}));
	}

	for (const name of names) {
		const id = observerId(name);
		const unit = ((snapshot.observers || []).find((o) => o.name === name) || {}).unit;
		const rows = visible().filter((row) => (row.observers || {})[name]
			&& !missing(row.observers[name].median));
		const ys = rows.map((row) => row.observers[name].median);
		const yType = scaleOf(id, 'y', ys, true);

		draw(id, medianTraces(rows, (row) => row.observers[name].median, id),
			plotLayout({
				margin: { l: 58, r: 12, t: 8, b: 34 },
				xaxis: axis({ title: { text: 'iteration', font: { size: 10, color: resolved('--text-faint') } } }),
				yaxis: axis({ type: yType, ...logTicks(ys, yType), title: { text: name + (unit ? ` (${unit})`
					: '') + ', median', font: { size: 10, color: resolved('--text-faint') } } }),
			}), { selectable: true, ids: rows.map((row) => row.id),
			config: SELECT_CONFIG, onClick: (pid) => setPinned(Number(pid)) });

		renderScales(id, [{ which: 'y', values: ys, logable: true }], renderObservers);
	}
}

/* ==== SECTION: validation ==== */

/* The external validation: one small multiple per numeric measure over
   rounds, each round's value marked in its verdict's tone. Run against that
   round's root on inputs the search never measures; it is not a gate and
   chose nothing, which the section says before any number. A search without
   one hides the section. Not linked to the selection: a point is a round,
   not an iteration. */
const NOT_A_GATE = 'Not a gate: it chose nothing and rejected nothing, and '
	+ 'its numbers need not be comparable to the objective.';

const validationId = (name) => 'validation-'
	+ String(name).replace(/[^A-Za-z0-9_-]/g, '_');
const verdictWord = (record) => record.ok ? 'ok' : 'needs attention';

/* The verdict line of a hover; "needs attention" in the bad tone, as on the
   panel's point and in the sidebar. */
const verdictTip = (record) => record.ok ? tipRow('verdict', verdictWord(record))
	: `<span style="color:${resolved('--bad')}">`
		+ tipRow('verdict', verdictWord(record)) + '</span>';
const isNumber = (value) => typeof value === 'number' && Number.isFinite(value);

/* A measure's name with its unit, when the validation names one. */
const withUnit = (name) => {
	const unit = (((state.snapshot || {}).validation || {}).units || {})[name];
	return unit ? `${name} (${unit})` : name;
};

function renderValidation() {
	const snapshot = state.snapshot;
	const holder = $('validation-charts');
	if (!snapshot || !holder) return;

	const block = snapshot.validation;
	$('section-validation').hidden = !block;
	if (!block) return;

	const records = block.records || [];
	fill('validation-note', el('span', { class: 'not-a-gate', text: NOT_A_GATE }),
		' Run once a round against that round\'s root, on inputs the search '
		+ 'does not otherwise touch' + (block.note ? `: ${block.note}.` : '.'));

	const numeric = block.measures.filter((name) =>
		records.some((record) => isNumber(record.measures[name])));
	const key = numeric.join(',') + '|' + records.length + '|'
		+ JSON.stringify(block.units || {});

	// Built once per set of measures and only react()ed into afterwards.
	if (holder.dataset.keys !== key) {
		holder.dataset.keys = key;
		for (const id of [...plotted]) if (id.startsWith('validation-')) plotted.delete(id);

		holder.replaceChildren(...(numeric.length ? numeric.map((name) =>
			el('figure', { class: 'card', dataset: { measure: name } }, [
				el('figcaption', {}, [el('span', { text: withUnit(name) })]),
				el('div', { class: 'plot short', id: validationId(name) })]))
			: [el('div', { class: 'empty', text: records.length
				? 'No measure reported a number; the sidebar lists what each did.'
				: 'Configured' + (block.approved ? ' and approved' : ', not yet '
					+ 'approved') + '; no round has been validated yet.' })]));
	}

	for (const name of numeric) {
		const traces = [(() => {
		const rows = records.filter((record) => isNumber(record.measures[name]))
			.sort((a, b) => a.round - b.round);

		return {
			type: 'scatter', mode: 'lines+markers', name,
			x: rows.map((record) => record.round),
			y: rows.map((record) => record.measures[name]),
			line: { color: resolved('--line'), width: 1.2 },
			// The script's own verdict: ok in the muted good tone, needs
			// attention in the bad tone at full strength, the one to find.
			marker: { size: 8, color: rows.map((record) => record.ok
				? verdictColour('ok') : verdictColour('bad', true)) },
			// Eight lines fit a short plot; "not a gate" rides in the head so
			// the cut never takes it.
			text: rows.map((record) => tipBlock([
				[`<b>${escape(withUnit(name))}</b> · not a gate`,
					tipRow('value', fmt.sig(record.measures[name]))],
				[tipRow('round', record.round),
					tipRow('iteration', tag(record.iteration)),
					verdictTip(record)],
				record.note ? [escape(record.note)] : [],
			], validationId(name))),
			hovertemplate: HOVER,
		};
		})()].filter((trace) => trace.x.length);

		draw(validationId(name), traces, plotLayout({
			margin: { l: 54, r: 12, t: 8, b: 34 },
			dragmode: false,
			xaxis: axis({ dtick: 1, title: { text: 'round',
				font: { size: 10, color: resolved('--text-faint') } } }),
			// The panel title names the measure; a rotated y title in this
			// fixed margin met the tick labels.
			yaxis: axis({ automargin: false }),
		}), { config: PLAIN_CONFIG });
	}
}

/* The latest round's values, for the sidebar. */
function validationCard() {
	const block = (state.snapshot || {}).validation;
	if (!block) return null;

	const records = block.records || [];
	const latest = records[records.length - 1];

	if (!latest)
		return statCard('External validation', [
			['approved', block.approved ? 'yes' : 'not yet'],
			['rounds validated', '0']], NOT_A_GATE);

	const value = (x) => missing(x) ? 'not reported'
		: isNumber(x) ? fmt.sig(x) : String(x);

	return statCard('External validation', [
		['round', `${latest.round} · #${latest.iteration}`],
		...block.measures.map((name) => [withUnit(name), value(latest.measures[name])]),
		['verdict', verdictWord(latest), latest.ok ? 'good' : 'bad'],
	], (latest.note ? latest.note + '. ' : '') + NOT_A_GATE);
}

/* ==== SECTION: run-table ==== */

/* The data-table widget, plus two things it
   lacks: an initial sort, and a pinned set of rows that is outlined and
   scrolled to. `options.pinned(row)` names them; `reveal` moves to their
   page and scrolls the box, and is set only when the pin changed, so a
   poll never yanks the table away from where someone scrolled it. */
const TABLE_PAGE = 40;
const tables = {};

function renderDataTable(holderId, rows, columns, options, reveal) {
	const holder = $(holderId);
	if (!holder) return;

	options = options || {};
	const view = tables[holderId] = tables[holderId] || {
		sort: options.sort || columns[0].key, dir: options.dir || 1, page: 0,
		query: '', filters: {}, rows, columns, options };
	Object.assign(view, { rows, columns, options });

	if (!holder.dataset.built) {
		holder.dataset.built = 'true';
		const search = el('input', { type: 'search', class: 'table-search',
			placeholder: 'Search…', 'aria-label': 'Search the table' });
		search.addEventListener('input', () => {
			view.query = search.value.trim().toLowerCase();
			view.page = 0;
			drawTable(holderId);
		});

		const selects = (options.filters || []).map((filter) => {
			const select = el('select', { 'aria-label': filter.label,
				dataset: { key: filter.key } });
			select.addEventListener('change', () => {
				view.filters[filter.key] = select.value;
				view.page = 0;
				drawTable(holderId);
			});
			return select;
		});

		holder.replaceChildren(
			el('div', { class: 'table-controls' }, [search, ...selects,
				el('span', { class: 'table-count muted' })]),
			el('div', { class: 'table-scroll' }, [el('table', {}, [
				el('thead', {}, [el('tr')]), el('tbody')])]),
			el('div', { class: 'table-foot' }, [
				el('button', { class: 'ghost small', type: 'button', text: 'Previous',
					onclick: () => { view.page = Math.max(0, view.page - 1); drawTable(holderId); } }),
				el('span', { class: 'table-page muted' }),
				el('button', { class: 'ghost small', type: 'button', text: 'Next',
					onclick: () => { view.page += 1; drawTable(holderId); } })]));
	}

	for (const select of holder.querySelectorAll('.table-controls select')) {
		const key = select.dataset.key;
		const label = (options.filters.find((f) => f.key === key) || {}).label;
		const values = [...new Set(rows.map((r) => r[key]))]
			.filter((v) => !missing(v)).sort();
		const current = view.filters[key] || '';
		select.replaceChildren(el('option', { value: '', text: `All ${label}` }),
			...values.map((v) => el('option', { value: String(v), text: String(v) })));
		select.value = values.map(String).includes(current) ? current : '';
	}

	drawTable(holderId, reveal);
}

function drawTable(holderId, reveal) {
	const holder = $(holderId);
	const view = tables[holderId];
	const { columns, sort, dir } = view;
	const pinned = view.options.pinned || (() => false);

	holder.querySelector('thead tr').replaceChildren(...columns.map((c) =>
		el('th', { class: c.num ? 'num' : null, scope: 'col',
			title: c.title || 'Sort', onclick: () => {
				if (view.sort === c.key) view.dir *= -1;
				else { view.sort = c.key; view.dir = 1; }
				drawTable(holderId);
			} }, [c.label, el('span', { class: 'arrow', text: c.key === sort
				? (dir > 0 ? ' ▲' : ' ▼') : '' })])));

	const matches = view.rows.filter((row) => {
		for (const [key, value] of Object.entries(view.filters))
			if (value && String(row[key]) !== value) return false;
		return !view.query || columns.some((c) => !missing(row[c.key])
			&& String(row[c.key]).toLowerCase().includes(view.query));
	}).sort((a, b) => {
		const x = a[sort], y = b[sort];
		if (x === y) return (a._order || 0) - (b._order || 0);
		if (missing(x)) return 1;
		if (missing(y)) return -1;
		return (typeof x === 'number' ? x - y
			: String(x).localeCompare(String(y))) * dir;
	});

	const pages = Math.max(1, Math.ceil(matches.length / TABLE_PAGE));
	if (reveal) {
		const first = matches.findIndex(pinned);
		if (first >= 0) view.page = Math.floor(first / TABLE_PAGE);
	}
	view.page = Math.min(view.page, pages - 1);
	const page = matches.slice(view.page * TABLE_PAGE, (view.page + 1) * TABLE_PAGE);

	holder.querySelector('.table-count').textContent =
		`${fmt.int(matches.length)} of ${fmt.int(view.rows.length)} shown`;
	holder.querySelector('.table-page').textContent =
		`page ${view.page + 1} of ${pages}`;

	holder.querySelector('tbody').replaceChildren(...page.map((row) =>
		el('tr', { dataset: { pinned: String(Boolean(pinned(row))),
			selected: String(Boolean(view.options.selected
				? view.options.selected(row)
				: state.selection && state.selection.has(row.id))) } },
			columns.map((c) => {
				const value = row[c.key];
				const text = c.format ? c.format(value, row) : missing(value) ? '–'
					: c.num ? Number(value).toFixed(c.digits ?? 0) : String(value);
				return el('td', { class: [c.num ? 'num' : null, c.cls || null]
					.filter(Boolean).join(' ') || null, text,
					style: c.colour ? `color:${c.colour(value, row)}` : null,
					title: c.cls === 'note' ? text : null,
					dataset: c.tone && c.tone(value, row) ? { tone: c.tone(value, row) }
						: {} });
			}))));

	// Scroll the box, not the page: the chart that was clicked stays put.
	const target = reveal && holder.querySelector('tbody tr[data-pinned="true"]');
	if (target) {
		const box = holder.querySelector('.table-scroll');
		const head = holder.querySelector('thead').getBoundingClientRect().height;
		const offset = target.getBoundingClientRect().top
			- box.getBoundingClientRect().top;
		// Flush with the sticky header's bottom edge: any less and a sliver
		// of the row above shows beneath it.
		if (offset < head || offset > box.clientHeight - target.offsetHeight)
			box.scrollTop += offset - head;
	}
}

/* Same test the runner applies (autoopt.OPS), so a red cell is a run that
   broke the gate. */
function gatePasses(value, gate) {
	const tol = gate.tol || 0;
	switch (gate.op) {
	case '<=': return value <= gate.threshold + tol;
	case '<': return value < gate.threshold + tol;
	case '>=': return value >= gate.threshold - tol;
	case '>': return value > gate.threshold - tol;
	case '==': return Math.abs(value - gate.threshold) <= tol;
	default: return true;
	}
}

/* One row per run: every settled, visible iteration's seeds, and its failed
   seeds with their error in the note column. */
function runRows() {
	const rows = [];
	let order = 0;

	for (const row of visible().slice().sort((a, b) => b.id - a.id)) {
		const common = { id: row.id, iteration: row.id, category: row.category,
			title: row.title || '', status: row.status, parent: row.parent,
			round: row.round,
			commit: (row.commit || '').slice(0, 8),
			tests: row.tests === true ? 'pass' : row.tests === false ? 'FAIL' : '' };

		for (const run of row.runs || []) {
			const out = { ...common, _order: order++, seed: run.seed,
				run_status: 'ok', objective: run.objective,
				wall: run.seconds, note: run.note || '' };
			for (const [name, value] of Object.entries(run.gates || {}))
				out['g:' + name] = value;
			for (const [name, value] of Object.entries(run.observers || {}))
				out['o:' + name] = value;
			rows.push(out);
		}

		for (const failure of row.failures || [])
			rows.push({ ...common, _order: order++, seed: failure.seed,
				run_status: 'error', objective: null, wall: failure.seconds,
				note: String(failure.error || '') });
	}

	return rows;
}

function runColumns(rows) {
	const snapshot = state.snapshot;
	const unit = (u) => u ? ` (${u})` : '';
	const numeric = (key) => ({ digits: columnDigits(rows.map((r) => r[key])) });

	return [
		{ key: 'iteration', label: '#', format: tag },
		{ key: 'category', label: 'type', format: (v) => (SHORT[v] || v).toUpperCase() },
		{ key: 'seed', label: 'seed', num: true },
		{ key: 'objective', label: snapshot.objective.name + unit(snapshot.objective.unit),
			num: true, ...numeric('objective'),
			format: (v) => missing(v) ? '–'
				: v.toFixed(columnDigits(rows.map((r) => r.objective))) },
		...(snapshot.gates || []).map((gate) => ({ key: 'g:' + gate.name,
			label: gate.name, num: true, title: `gate: ${gate.op} ${gate.threshold}`
				+ (gate.tol ? ` ± ${gate.tol}` : ''),
			format: (v) => missing(v) ? '–' : fmt.sig(v),
			tone: (v) => !missing(v) && !gatePasses(v, gate) ? 'bad' : null })),
		...(snapshot.observers || []).map((observer) => ({ key: 'o:' + observer.name,
			label: observer.name + unit(observer.unit), num: true,
			format: (v) => missing(v) ? '–' : fmt.sig(v) })),
		// Seconds to two places, so runs a fraction of a second apart differ.
		{ key: 'wall', label: 'wall clock (s)', num: true,
			format: (v) => missing(v) ? '–' : Number(v).toFixed(2) },
		{ key: 'run_status', label: 'run', tone: (v) => v === 'ok' ? null : 'bad' },
		{ key: 'status', label: 'iteration',
			tone: (v) => v === 'ok' ? null : 'bad' },
		{ key: 'tests', label: 'tests', tone: (v) => v === 'FAIL' ? 'bad' : null },
		{ key: 'parent', label: 'parent', format: tag },
		{ key: 'round', label: 'round', num: true },
		{ key: 'commit', label: 'commit', cls: 'mono' },
		{ key: 'title', label: 'idea' },
		{ key: 'note', label: 'note', cls: 'note' },
	].filter(Boolean);
}

function renderRunTable(reveal) {
	if (!state.snapshot) return;
	const rows = runRows();

	fill('run-note', `${fmt.int(rows.length)} runs. Click a point in the chart `
		+ 'to outline its runs here; a run dot (All runs) outlines that one run. '
		+ 'A red value broke its gate.');

	renderDataTable('run-table', rows, runColumns(rows), {
		sort: 'iteration', dir: -1,
		filters: [{ key: 'status', label: 'statuses' },
			{ key: 'category', label: 'types' }],
		pinned: (row) => state.pinned !== null && row.iteration === state.pinned
			&& (state.pinnedSeed === null || row.seed === state.pinnedSeed),
	}, reveal);
}

selectionListeners.push(() => renderRunTable(false));

/* A second click on the same point or node drops the pin. */
function setPinned(identifier, seed) {
	seed = seed === undefined ? null : seed;
	const same = state.pinned === identifier && state.pinnedSeed === seed;
	state.pinned = same ? null : identifier;
	state.pinnedSeed = same ? null : seed;
	renderRunTable(true);
}

/* ==== SECTION: log ==== */

/* One fixed decimal count for a column of values, so the digits align: four
   significant figures of the largest. */
function columnDigits(values) {
	const top = Math.max(0, ...values.filter((v) => !missing(v)).map(Math.abs));
	return top ? Math.max(0, Math.min(6, 3 - Math.floor(Math.log10(top)))) : 3;
}

function logColumns() {
	const snapshot = state.snapshot;
	const digits = columnDigits(snapshot.iterations.map((row) => row.value));

	return [
		{ key: 'id', label: '#', width: 44, format: tag },
		{ key: 'category', label: 'type', width: 50,
			format: (v) => (SHORT[v] || v).toUpperCase() },
		{ key: 'value', label: snapshot.objective.name, width: 78, num: true,
			format: (v, row) => row.status === 'running' ? 'running' : fmt.num(v, digits) },
		{ key: 'n_runs', label: 'runs', width: 34, num: true,
			format: (v) => v ? fmt.int(v) : '' },
		{ key: 'seconds', label: 'time', width: 54, num: true,
			format: (v) => v ? fmt.duration(v) : '' },
		{ key: 'status', label: 'status', width: 82,
			format: (v) => v === 'ok' ? '' : v },
		{ key: 'title', label: 'idea', format: (v) => v || '(untitled)' },
	].filter(Boolean);
}

/* Where an iteration's time went. Only the steps the scripts time are
   measured; the remainder is the subagent's own work, inferred, since every
   subagent structures its reading, editing and checking differently. */
function timeRows(time) {
	if (!time || missing(time.total)) return [];
	const share = (s) => time.total > 0 ? ` · ${Math.round(100 * s / time.total)}%` : '';
	const line = (s, note) => `${fmt.duration(s)}${share(s)}${note ? ' · ' + note : ''}`;
	return [
		['time total', fmt.duration(time.total)],
		['time: runs', line(time.runs, `${time.n_runs} run${time.n_runs === 1 ? '' : 's'}`)],
		...(missing(time.tests) ? [] : [['time: unit tests',
			line(time.tests, time.tests_scope === 'subset' ? 'subset' : 'full suite')]]),
		...(missing(time.validation) ? [] : [['time: validation', line(time.validation)]]),
		['time: subagent work', line(time.subagent, 'the rest, inferred')],
	];
}

function iterationDetail(row) {
	const snapshot = state.snapshot;
	const objective = row.objective || {};
	const baseline = snapshot.frontier.baseline;

	return [
		['status', row.status + (row.stage ? ' · ' + row.stage : '')],
		['category', row.category],
		...(minedStages() && !missing(row.mining_stage)
			? [['miner stage', 'S' + row.mining_stage]] : []),
		['parent', tag(row.parent)],
		['round', String(row.round)],
		['runs', `${row.n_runs}` + (row.n_failed ? ` (${row.n_failed} failed)` : '')],
		['median', fmt.sig(row.value)],
		['mean ± sd', `${fmt.sig(objective.mean)} ± ${fmt.sig(objective.std)}`],
		['min … max', `${fmt.sig(objective.min)} … ${fmt.sig(objective.max)}`],
		['vs baseline', missing(baseline) || missing(row.value) ? '–'
			: fmt.signed(row.value - baseline, 4)],
		['frontier at start', fmt.sig(row.frontier_at_start)],
		['noise band', fmt.sig(row.noise_band)],
		['wall clock', fmt.duration(row.seconds)],
		...timeRows(row.time),
		['branch', row.branch || '–'],
		...(row.toolchain ? [['toolchain', row.toolchain, row.toolchain === 'specific'
			? 'not promotable: the change depends on a compiler or toolchain choice'
			: null]] : []),
		['commit', (row.commit || '–').slice(0, 10)],
		['best run', row.best_run || '–'],
		...Object.entries(row.gates || {}).map(([name, gate]) => [kindKey('gate', name),
			`${gate.pass ? 'pass' : 'FAIL'} ${gate.op} ${fmt.sig(gate.threshold)}, `
				+ `worst ${fmt.sig(gate.worst)}`]),
		...Object.entries(row.observers || {}).map(([name, observer]) =>
			[kindKey('obs', name), fmt.sig(observer.median)]),
		...(row.runs || []).map((run) => [kindKey('seed', run.seed),
			`${fmt.sig(run.objective)} · ${fmt.duration(run.seconds)}`]),
		...(row.failures || []).map((failure) => [kindKey('seed',
			`${failure.seed} failed`), String(failure.error)]),
	];
}

/* A detail key naming a gate, an observer or a seed: the kind in capitals
   in its own muted hue (`--tag-gate`, `--tag-obs`, `--tag-seed`), so the
   three groups of rows can be told apart at a glance. */
const KIND_CLASS = { gate: 'k-tag k-gate', obs: 'k-tag k-obs', seed: 'k-tag k-seed' };

function kindKey(kind, name) {
	return el('span', {}, [el('span', { class: KIND_CLASS[kind],
		text: kind.toUpperCase() }), ' ' + name]);
}

/* The README as it should read on screen: the markdown these files use,
   turned into elements. The title and the metadata line are dropped, since
   both are already in the entry. With `options.more` (the log), the code
   change leads, and an excerpt the payload cut ends in SHOW MORE, which
   swaps in the whole README fetched from readmes/<id>.json; the choice
   survives the log's re-render on every poll. */
function describe(row, options) {
	const more = Boolean(options && options.more);
	const lead = more ? diffLine(row) : null;
	if (!row.notes) return el('div', { class: 'log-desc' },
		[lead, el('p', { class: 'log-p', text: 'No README written.' })]);

	if (more && row.notes_cut && state.logFull.has(row.id)) {
		loadReadme(row.id);
		const text = state.readmes[row.id];
		if (typeof text === 'string')
			return el('div', { class: 'log-desc' }, [lead,
				markdown(readmeBody(text)), moreButton(row, true)]);
		return el('div', { class: 'log-desc' }, [lead, excerptBlocks(row),
			el('p', { class: 'log-p muted', text: text === null ? 'The whole README '
				+ 'could not be read.' : 'Loading the whole README…' }),
			moreButton(row, true)]);
	}

	return el('div', { class: 'log-desc' }, [lead, excerptBlocks(row),
		more && row.notes_cut ? moreButton(row, false) : null]);
}

/* The README without what the entry already shows: the title, the
   metadata line and the per-run table. */
function readmeBody(text) {
	return text.split('\n').filter((line) => !/^#\s/.test(line.trim())
		&& !/^\*\*Category:/.test(line.trim()) && !/^\s*\|/.test(line)).join('\n');
}

/* SHOW MORE at the cut, SHOW LESS once open. A click here is not a click on
   the entry, which would close it. */
function moreButton(row, open) {
	return el('button', { class: 'ghost small log-more', type: 'button',
		'aria-expanded': String(open), text: open ? 'SHOW LESS' : 'SHOW MORE',
		onclick: (event) => {
			event.stopPropagation();
			if (open) state.logFull.delete(row.id);
			else state.logFull.add(row.id);
			renderIterationLog();
		} });
}

/* The payload's excerpt as paragraphs and headings. */
function excerptBlocks(row) {
	const blocks = [];
	let paragraph = [];
	const flush = () => {
		if (paragraph.length) {
			blocks.push(el('p', { class: 'log-p',
				text: paragraph.join(' ').replace(/[*`]/g, '') }));
			paragraph = [];
		}
	};

	for (const raw of row.notes.split('\n')) {
		const line = raw.trim();
		if (!line) { flush(); continue; }
		if (/^#\s/.test(line) || /^\*\*Category:/.test(line) || /^\|/.test(line))
			continue;
		if (/^#{2,}\s/.test(line)) {
			flush();
			blocks.push(el('div', { class: 'log-h', text: line.replace(/^#+\s*/, '') }));
			continue;
		}
		paragraph.push(line);
	}
	flush();

	// The excerpt is cut at a character budget; a trailing heading with
	// nothing under it, or a few words of a sentence, is noise.
	while (blocks.length) {
		const last = blocks[blocks.length - 1];
		if (!(last.className === 'log-h' || (last.textContent.length < 40
			&& last.textContent.endsWith('…')))) break;
		blocks.pop();
	}

	return el('div', {}, blocks.length ? blocks
		: [el('p', { class: 'log-p', text: row.notes })]);
}

/* The widget draws the one-line entries and the key/value detail; the
   README goes in above the detail of every open entry. */
function decorateLog() {
	for (const entry of ROOT.querySelectorAll('#log .log-entry[data-open="true"]')) {
		const row = byId(Number(entry.dataset.id));
		const detail = entry.querySelector('.log-detail');
		if (row && detail && !entry.querySelector('.log-desc'))
			entry.insertBefore(describe(row, { more: true }), detail);
	}
}

function renderIterationLog() {
	const snapshot = state.snapshot;
	if (!snapshot) return;

	renderLog('log', snapshot.iterations, logColumns(), { slotOf: catSlot,
		hidden: state.hidden, emphasised: state.emphasised, detail: iterationDetail,
		// Opening an entry outlines its runs in Every run. The widget
		// re-renders the log after this returns.
		onPin: (id, opened) => {
			if (opened) { state.pinned = null; setPinned(id); }
			queueMicrotask(decorateLog);
		} });
	decorateLog();
}

/* ==== SECTION: tree ==== */

selectionListeners.push(() => renderTree());

/* The lineage of the whole search. */
function renderTree() {
	const snapshot = state.snapshot;
	const holder = $('tree-holder');
	if (!snapshot || !holder) return;

	// The column is built once; the widget needs its holder in the page to
	// measure it.
	if (!holder.firstChild)
		holder.replaceChildren(el('div', { class: 'tree-col' }, [
			el('div', { class: 'lineage-wrap' }, [
				el('div', { class: 'lineage-holder', id: 'lineage-0' }),
				el('div', { class: 'lineage-map', id: 'lineage-map-0' })]),
		]));

	const roots = new Set(((snapshot.state || {}).roots || []).map((r) => r.id));
	const nodes = snapshot.iterations.map((row) => ({ id: row.id,
		parent: row.parent,
		status: row.status, category: row.category, value: row.value,
		title: row.title, round: row.round, on_frontier: row.on_frontier,
		is_root: roots.has(row.id) }));

	const drawn = renderLineage('lineage-0', 'lineage-map-0', nodes, {
		slotOf: catSlot, hidden: state.hidden, emphasised: state.emphasised,
		onPin: (id) => { setPinned(id); toggleTreeNode(id); } });

	renderTreeNote((drawn && drawn.collapsed) || 0);
	renderTreeNode();
}

/* ==== the node panel: a clicked tree node's whole README and every number ==== */

/* A click opens the node's panel under the tree; clicking the same node, or
   'close', shuts it. */
function toggleTreeNode(id) {
	state.treeNode = state.treeNode === id ? null : id;
	renderTreeNode();
}

/* The payload carries only an excerpt of each README; the whole file is
   fetched when its panel opens, and again when the excerpt changes (a
   subagent still writing it). */
async function loadReadme(id) {
	const row = byId(id);
	const excerpt = row ? row.notes || '' : '';
	// No README in the row means none to fetch; a readmes/<id>.json on disk
	// then belongs to another search a handover published.
	if (!row || !row.has_readme) return;
	if (!TRANSPORT || !TRANSPORT.readme || state.readmeFor[id] === excerpt) return;
	state.readmeFor[id] = excerpt;
	try {
		const payload = await TRANSPORT.readme(id);
		state.readmes[id] = payload && typeof payload.text === 'string' ? payload.text : null;
	} catch (error) {
		state.readmes[id] = null;
	}
	if (state.treeNode === id) renderTreeNode();
	if (state.logFull.has(id)) renderIterationLog();
}

function renderTreeNode() {
	const holder = $('tree-node');
	if (!holder) return;
	const row = state.treeNode === null ? null : byId(state.treeNode);
	if (!row) {
		holder.hidden = true;
		holder.replaceChildren();
		return;
	}

	loadReadme(row.id);
	const text = state.readmes[row.id];
	const readme = text ? markdown(text)
		: el('div', {}, [
			el('p', { class: 'muted', text: text === null ? 'The whole README could not '
				+ 'be read; this is the excerpt the payload carries.'
				: (row.notes ? 'Loading the whole README…' : '') }),
			describe(row)]);

	const pairs = iterationDetail(row);

	holder.hidden = false;
	holder.replaceChildren(
		el('div', { class: 'tree-node-head' }, [
			el('span', { class: 'tree-node-id', text: tag(row.id) }),
			el('span', { class: 'tree-node-title', text: row.title || '' }),
			el('button', { class: 'ghost small', type: 'button', text: 'close',
				onclick: () => { state.treeNode = null; renderTreeNode(); } })]),
		el('div', { class: 'tree-node-body' }, [
			el('div', { class: 'tree-node-readme' }, [readme]),
			el('div', { class: 'log-detail pairs tree-node-detail' }, pairs.map(([key, value]) =>
				el('div', { class: 'pair' }, [
					el('span', { class: 'k' }, [key]),
					el('span', { class: 'v', text: value })])))]));
}

/* The whole README, rendered: headings, paragraphs, lists, tables and code
   blocks, with `code` and **bold** spans. Everything from the file goes in
   as text, never as markup. */
function markdown(text) {
	const LIST = /^\s*([-*]|\d+\.)\s+/;
	const OTHER = /^(#{1,6}\s|```|\s*\|)/;
	const inline = (line) => {
		const parts = [];
		const span = /(`[^`]+`|\*\*[^*]+\*\*)/g;
		let last = 0;
		let match;
		while ((match = span.exec(line))) {
			if (match.index > last) parts.push(line.slice(last, match.index));
			const token = match[0];
			parts.push(token[0] === '`' ? el('code', { text: token.slice(1, -1) })
				: el('strong', { text: token.slice(2, -2) }));
			last = match.index + token.length;
		}
		if (last < line.length) parts.push(line.slice(last));
		return parts;
	};

	const lines = text.split('\n');
	const blocks = [];
	let i = 0;
	while (i < lines.length) {
		const line = lines[i];
		if (/^```/.test(line.trim())) {
			const body = [];
			for (i++; i < lines.length && !/^```/.test(lines[i].trim()); i++) body.push(lines[i]);
			i++;
			blocks.push(el('pre', { class: 'md-pre', text: body.join('\n') }));
			continue;
		}
		const heading = /^(#{1,6})\s+(.*)$/.exec(line);
		if (heading) {
			blocks.push(el('div', { class: heading[1].length === 1 ? 'md-h1' : 'md-h' },
				inline(heading[2])));
			i++;
			continue;
		}
		if (/^\s*\|/.test(line)) {
			const rows = [];
			for (; i < lines.length && /^\s*\|/.test(lines[i]); i++)
				if (!/^\s*\|[\s:|-]+\|?\s*$/.test(lines[i])) rows.push(lines[i]);
			const cells = (row) => row.trim().replace(/^\|/, '').replace(/\|$/, '')
				.split('|').map((cell) => cell.trim());
			blocks.push(el('table', { class: 'md-table' }, rows.map((row, k) =>
				el('tr', {}, cells(row).map((cell) => el(k === 0 ? 'th' : 'td', {},
					inline(cell)))))));
			continue;
		}
		if (LIST.test(line)) {
			const items = [];
			for (; i < lines.length; i++) {
				if (LIST.test(lines[i])) items.push({ nested: lines[i].search(/\S/) >= 2,
					text: lines[i].replace(LIST, '') });
				else if (/^\s{2,}\S/.test(lines[i])) items[items.length - 1].text += ' '
					+ lines[i].trim();
				else break;
			}
			blocks.push(el('ul', { class: 'md-ul' }, items.map((item) => el('li',
				{ class: item.nested ? 'md-li-nested' : null }, inline(item.text)))));
			continue;
		}
		if (!line.trim()) { i++; continue; }
		const paragraph = [];
		for (; i < lines.length && lines[i].trim() && !OTHER.test(lines[i])
			&& !LIST.test(lines[i]); i++) paragraph.push(lines[i].trim());
		blocks.push(el('p', { class: 'md-p' }, inline(paragraph.join(' '))));
	}
	return el('div', { class: 'md' }, blocks);
}

function renderTreeNote(collapsed) {
	const snapshot = state.snapshot;
	const roots = ((snapshot.state || {}).roots || []).length;

	$('tree-note').textContent = (collapsed ? `Condensed: ${fmt.int(collapsed)} `
		+ 'iterations off the frontier are tallied rather than drawn. Roots, the '
		+ 'frontier path, anything running and the 12 most recent are drawn '
		+ 'individually. ' : '')
		+ 'Each promotion makes an iteration the root, and the next generation '
			+ `forks from it. ${roots} root(s).`;

	fill('tree-legend',
		...[...(state.snapshot.categories || [])].sort(categoryOrder)
			.map((category) => el('span', { class: 'legend-item' }, [
				el('span', { class: 'swatch round', style: `background:`
					+ `${css(catSlot(category))}` }), SHORT[category] || category])),
		el('span', { class: 'legend-item' }, [markerKey(css('--text'), 'circle'),
			'root or frontier: its hue at full strength']),
		el('span', { class: 'legend-item' }, [statusKey('hollow', resolved('--running')),
			'running']),
		el('span', { class: 'legend-item' }, [markerKey(css('--bad'), 'x'),
			'gate failed']),
		el('span', { class: 'legend-item' }, [el('span', { class: 'tally-key mark-key',
			text: '+N' }), 'N iterations tallied, hover for detail']));
}

/* The frontier the tiles and the sidebar describe. */
function scopedFrontier() {
	const snapshot = state.snapshot;
	const frontier = snapshot.frontier;

	return { value: frontier.value, id: frontier.id,
		baseline: frontier.baseline, total_gain: frontier.total_gain,
		total_percent: frontier.total_percent,
		steps: snapshot.stats.n_frontier };
}

/* The ending in the topbar and the five stages as text, both from the
   search's own state files. */
const STAGE_WORD = { complete: 'done', running: 'running', 'not started': 'pending' };

function renderAfterLines(after) {
	const ended = after.ended;
	const line = $('ended-line');
	line.hidden = !ended;
	if (ended) {
		const unit = ended.unit ? ` ${ended.unit}` : '';
		line.textContent = `ended ${String(ended.at || '').slice(0, 10)} · `
			+ `${ended.winner || '–'} ${fmt.sig(ended.winner_median)}${unit} vs A0 `
			+ `${fmt.sig(ended.zero_median)}${unit} · ${ended.reason || 'no reason stated'}`;
		line.title = ended.paired ? `paired timing: ${ended.paired}` : '';
	}

	const strip = $('stage-strip');
	strip.hidden = !(after.stages || []).length;
	strip.textContent = (after.stages || []).map(([name, status]) =>
		`${name} ${STAGE_WORD[status] || status}`).join(' · ');
}

function renderSummary() {
	const snapshot = state.snapshot;
	if (!snapshot) return;

	const counts = snapshot.counts;
	const plan = snapshot.plan;
	const frontier = scopedFrontier();
	const unit = snapshot.objective.unit ? ` ${snapshot.objective.unit}` : '';
	const gpus = (state.lastGpus || {}).gpus || [];
	const improved = !missing(frontier.total_gain) && frontier.total_gain !== 0
		&& (frontier.total_gain > 0) === (snapshot.objective.direction === 'max');

	$('run-root').textContent = snapshot.root;
	renderAfterLines(snapshot.after || {});
	$('hero-value').textContent = fmt.int(counts.iterations);
	$('hero-sub').textContent = `${fmt.int(counts.runs)} runs`
		+ (counts.running ? ` · ${fmt.int(counts.running)} in flight` : '')
		+ (counts.working ? ` · ${fmt.int(counts.working)} subagent${counts.working === 1 ? '' : 's'} working` : '');

	fill('tiles',
		tile('Frontier',
			fmt.sig(frontier.value), `${snapshot.objective.name}${unit}`
				+ (missing(frontier.id) ? '' : ` · iteration ${tag(frontier.id)}`),
			'pink'),
		tile('Gain over baseline',
			missing(frontier.total_gain) ? '–' : fmt.signed(frontier.total_gain, 4),
			`${snapshot.objective.name}${unit} · ${fmt.signedPct(frontier.total_percent)}`,
			improved ? 'good' : null),
		tile('Rounds', fmt.int(counts.rounds), `summarized · on round ${counts.round}`),
		tile('Frontier steps', fmt.int(frontier.steps),
			`iterations · ${fmt.int(snapshot.stats.n_gate_failed)} gate-failed`),
		tile('Runs per iteration', plan.staged
			? `${plan.runs_per_iteration}→${plan.confirm_runs}`
			: String(plan.runs_per_iteration), plan.staged
				? 'seeds · staged: screen, then confirm' : 'seeds · fixed'),
		tile('GPUs in use', `${gpus.filter((g) => g.mine).length} / ${gpus.length}`,
			'this search / on the machine'));

	$('objective-note').textContent = `${snapshot.objective.name}, `
		+ `${snapshot.objective.direction === 'max' ? 'higher' : 'lower'} is better. `
		+ (snapshot.task || '') + ' Lasso or box-select in any chart to select the '
		+ 'same iterations everywhere; double-click or Clear selection to drop it.';

	renderCategoryBar();
	markRowStarts('tiles');
}

/* Tiles may wrap. A tile that opens a row (it sits on a different row from
   the tile or hero before it) is marked `data-row-start`, and the CSS drops
   its divider but keeps its indent, so its text stays in the column of the
   tile above. Re-run on every render and on resize. */
function markRowStarts(holderId) {
	const holder = $(holderId);
	if (!holder) return;

	let prev = holder.previousElementSibling;
	for (const item of holder.children) {
		const top = Math.round(item.getBoundingClientRect().top);
		const start = !prev || Math.round(prev.getBoundingClientRect().top) !== top;
		if (start) item.dataset.rowStart = 'true';
		else delete item.dataset.rowStart;
		prev = item;
	}
}

/* Every category autoopt.py allows, empty ones included, the catch-all last. */
function renderCategoryBar() {
	const snapshot = state.snapshot;
	const counts = snapshot.counts.by_category || {};
	const ordered = {};

	for (const category of [...(snapshot.categories || Object.keys(counts))].sort(
		categoryOrder))
		ordered[category] = counts[category] || 0;

	// Clicking a chip emphasises its category in every chart, the log and
	// the tree; its dot hides the category.
	categoryChips('catbar', ordered, (c) => css(catSlot(c)), state.hidden,
		state.emphasised, () => {
			renderCategoryBar();
			renderCharts();
			renderTree();
		});
}

/* ==== SECTION: sidebar-autoopt ==== */

/* What each step of the frontier bought: a bar apiece at true proportion,
   coloured by the kind of idea that made it. */
function gainCard(steps) {
	const biggest = Math.max(...steps.map((s) => Math.abs(s.gain)), 1e-12);
	const digits = columnDigits(steps.map((s) => s.gain));

	return el('div', { class: 'stat-card' }, [
		el('h3', { text: 'Gain per step' }),
		el('div', { class: 'bar-list gain-list' }, steps.map((step) =>
			el('div', { class: 'bar-row' }, [
				el('span', { class: 'key', text: tag(step.id), title: step.title || '' }),
				el('span', { class: 'bar', style: `width:${100 * Math.abs(step.gain)
					/ biggest}%;min-width:1px;background:${css(catSlot(step.category))}` }),
				el('span', { class: 'n', text: fmt.signed(step.gain, digits) })]))),
		el('div', { class: 'stat-note', text: 'Each bar is the change in '
			+ `${state.snapshot.objective.name} over the step before it, coloured by `
			+ 'the kind of idea that made it.' }),
	]);
}

function renderSidebar() {
	const snapshot = state.snapshot;
	if (!snapshot) return;

	const stats = snapshot.stats;
	const counts = snapshot.counts;
	const frontier = scopedFrontier();
	const hours = (x) => missing(x) ? '–' : fmt.sig(x) + ' h';
	const steps = (snapshot.frontier.steps || []).filter((step) => step.gain);
	const perCategory = Object.entries(stats.per_category || {})
		.sort((a, b) => b[1].n - a[1].n);
	const onFrontier = perCategory.filter(([, e]) => e.frontier)
		.map(([c, e]) => `${SHORT[c] || c} ${e.frontier}`).join(', ');

	fill('sidebar',
		statCard('Search', [
			['root', tag(snapshot.state.root)],
			['round', String(counts.round ?? '–')],
			['iterations', fmt.int(counts.iterations)],
			['in flight', fmt.int(counts.running), counts.running ? 'warn' : null],
			['subagents working', fmt.int(counts.working || 0), counts.working ? 'warn' : null],
			['runs', fmt.int(counts.runs)],
		]),
		statCard('Frontier', [
			[snapshot.objective.name, fmt.sig(frontier.value), 'pink'],
			['baseline', fmt.sig(frontier.baseline)],
			['total gain', fmt.signed(frontier.total_gain, 4)],
			['relative', fmt.signedPct(frontier.total_percent)],
			['steps', fmt.int(frontier.steps)],
		], missing(frontier.id) ? null : `Held by iteration ${tag(frontier.id)}.`),
		steps.length ? gainCard(steps) : null,
		statCard('Throughput', [
			['iterations/hour', fmt.sig(stats.iterations_per_hour)],
			['runs/hour', fmt.sig(stats.runs_per_hour)],
			['seconds/run', fmt.sig(stats.seconds_per_run)],
			['median iteration', missing(stats.median_iteration_minutes) ? '–'
				: fmt.sig(stats.median_iteration_minutes) + ' min'],
			['compute', hours(stats.compute_hours)],
			['wall clock', hours(stats.wall_hours)],
			stats.runs_saved == null ? null
				: ['runs saved', fmt.int(stats.runs_saved), 'good'],
		], stats.runs_saved == null ? null : 'Runs saved is what the staged '
			+ 'evaluation did not spend: iterations that stopped at the '
			+ 'screening depth.'),
		statCard('Outcomes', [
			['passing', fmt.int(stats.n_ok), 'good'],
			['gate-failed', fmt.int(stats.n_gate_failed), stats.n_gate_failed ? 'bad' : null],
			['errored', fmt.int(stats.n_error), stats.n_error ? 'warn' : null],
			// Only a row when it happened: an under-depth iteration is a smoke
			// test or an interruption, not an outcome of the search.
			stats.n_partial ? ['under depth', fmt.int(stats.n_partial), 'warn'] : null,
			['pass rate', missing(stats.gate_pass_rate) ? '–'
				: fmt.pct(100 * stats.gate_pass_rate)],
		], 'Pass rate is passing over every settled iteration except those '
			+ 'under depth.'),
		validationCard(),
		perCategory.length ? barCard('By category', perCategory.map(([c, e]) =>
			[c, e.n]), (c) => css(catSlot(c)), 'Settled iterations per category.'
			+ (onFrontier ? ` On the frontier: ${onFrontier}.` : '')) : null,
		(stats.rounds || []).length ? statCard('Summaries', stats.rounds.map((entry) =>
			['round ' + entry.round, entry.path.split('/').pop()])) : null);
}

function renderCharts() {
	renderMain();
	renderObservers();
	renderValidation();
	renderRunTable(false);
	renderIterationLog();
}

function renderAll() {
	renderSummary();
	renderCharts();
	renderTree();
	renderSidebar();
}
/* ==== SECTION: polling ==== */

let lastUpdate = 0;        // browser time state.json last changed
let lastSeen = {};         // the stamps it changed from
let WRITER_LOG = null;
let SNAPSHOT = null;       // set for a static export: no polling, no pulse

/* Freshness, not fetch success: a dead writer leaves its files readable, so
   the page must notice that they stopped changing. */
function noteChange(kind, stamp) {
	if (stamp !== undefined && stamp !== lastSeen[kind]) {
		lastSeen[kind] = stamp;
		lastUpdate = Date.now();
	}
}

async function pollState() {
	try {
		const snapshot = await TRANSPORT.state();
		state.snapshot = snapshot;
		noteChange('state', snapshot.generated);

		state.assetsStale = Boolean(LOADED_ASSETS && snapshot.assets
			&& String(snapshot.assets) !== String(LOADED_ASSETS));

		renderAll();

		$('footer').textContent = `Scanned ${snapshot.root} · monitor up `
			+ `${fmt.duration(snapshot.uptime)} · full scan every `
			+ `${fmt.duration(cadence('state') / 1000)}, hardware every `
			+ `${fmt.duration(cadence('resource') / 1000)}.`;
	} catch (error) {
		console.warn('state poll failed', error);
	}

	updatePulse();
}

/* The Live toggle. While it is on, and the tab is showing, the page touches
   the publisher's live file every LIVE_BEAT and polls every LIVE_INTERVAL;
   the publisher samples every second for as long as the file is fresh and
   returns to its own clock when it stops. Nothing is sampled for a hidden
   or closed tab. */
const LIVE_INTERVAL = 1000;
const LIVE_BEAT = 3000;
let liveTimers = [];
let polling = false;

function setLive(on) {
	state.live = on;
	liveTimers.forEach((timer) => {
		clearInterval(timer);
		const at = timers.indexOf(timer);
		if (at >= 0) timers.splice(at, 1);
	});
	liveTimers = [];

	if (on) {
		const beat = () => {
			if (document.hidden) return;
			TRANSPORT.live().catch((error) => {
				console.warn('live request failed', error);
				state.liveError = String(error.message || error);
				$('toggle-live').setAttribute('aria-pressed', 'false');
				setLive(false);
			});
		};

		state.liveError = null;
		beat();
		liveTimers = [setInterval(beat, LIVE_BEAT),
			setInterval(() => { if (!document.hidden) pollResources(); },
				LIVE_INTERVAL)];
		timers.push(...liveTimers);
	}

	updateLiveButton();
}

/* Says what the publisher did with the request: its resources.json carries
   `writer.live`. A publisher that never confirms is an old one. */
function updateLiveButton() {
	const button = $('toggle-live');
	if (!button) return;

	button.hidden = !(TRANSPORT && TRANSPORT.live && !SNAPSHOT);
	const confirmed = (((state.resources || {}).writer) || {}).live === true;

	// The label says what the publisher is doing: with idle GPUs the cards
	// look the same at one second as at three, so a toggle that only
	// changed colour looked as if it did nothing.
	button.textContent = state.liveError ? 'Live unavailable'
		: !state.live ? 'Live'
		: confirmed ? 'Live · 1 s' : 'Live · waiting';
	button.title = state.liveError
		? `Live is unavailable: ${state.liveError}`
		: !state.live ? 'Sample the GPUs every second while this tab is open'
		: confirmed ? 'The publisher is sampling every second'
		: 'Asked the publisher for live samples; waiting for it to answer';
}

async function pollResources() {
	if (polling) return;
	polling = true;

	try {
		state.resources = await TRANSPORT.resources();
		renderResources(state.resources);
		updateLiveButton();
		if (state.snapshot) renderSummary();
	} catch (error) {
		console.warn('resource poll failed', error);
	}

	polling = false;
	updatePulse();
}

/* The gap the writer actually keeps between scans ('state') or hardware
   samples ('resource'), in ms, from what it publishes about itself in
   resources.json; never shorter than the configured clock. A scan or an
   nvidia-smi call slower than its interval is a slow publisher, not a
   stopped one. */
function cadence(kind) {
	const writer = (state.resources || {}).writer || {};
	const measured = 1000 * (Number(writer[kind + '_cadence']) || 0);
	return Math.max(kind === 'state' ? STATE_INTERVAL : RESOURCE_INTERVAL,
		measured);
}

/* Freshness is state.json's age against the writer's scan cadence: live
   while it changed within 2.5 scans, stale up to 10, down beyond, and down
   says the publisher stopped and where its log is. resources.json is left
   out because it comes from its own thread and keeps ticking while every
   scan fails. A failing scan, which the writer reports in resources.json,
   is named, as is any iteration the scan could not read. A page older
   than the published front end says so instead, because no amount of
   polling replaces a script the browser already has. */
function updatePulse() {
	const pulse = $('pulse');
	const updated = $('updated');

	if (SNAPSHOT) {
		pulse.hidden = true;
		updated.textContent = 'snapshot of ' + new Date(SNAPSHOT * 1000)
			.toLocaleString();
		return;
	}

	if (state.assetsStale) {
		pulse.dataset.state = 'stale';
		updated.replaceChildren(el('b', { text: 'the dashboard has been '
			+ 'updated — re-run the cell' }));
		return;
	}

	if (!lastUpdate) {
		updated.textContent = 'connecting…';
		return;
	}

	const every = cadence('state');
	const age = Date.now() - lastUpdate;
	const failing = ((state.resources || {}).writer || {}).scan_error;
	const unreadable = (state.snapshot || {}).unreadable || [];
	const log = WRITER_LOG ? ` · see ${WRITER_LOG}` : '';

	pulse.dataset.state = age < 2.5 * every && !failing ? 'live'
		: age < 10 * every ? 'stale' : 'down';

	if (failing)
		updated.replaceChildren(el('b', { text: 'scan failing' }),
			` · ${failing} · last good scan ${fmt.duration(age / 1000)} ago${log}`);
	else if (pulse.dataset.state === 'down')
		updated.replaceChildren(el('b', { text: 'publisher stopped' }), log);
	else
		updated.textContent = `updated ${fmt.duration(age / 1000)} ago`;

	if (unreadable.length) {
		updated.append(` · ${unreadable.length} unreadable: `
			+ unreadable.slice(0, 5).map((entry) => tag(entry.id)).join(', ')
			+ (unreadable.length > 5 ? ' …' : ''));
		updated.title = unreadable.map((entry) => `${tag(entry.id)}: `
			+ entry.error).join('\n');
	} else {
		updated.removeAttribute('title');
	}
}

/* Drag the handle under the search tree to give it more height; double-click
   for the default. The height lives in --tree-height on the section, which
   the re-rendered tree reads, so it survives every refresh. */
const TREE_MIN = 160, TREE_DEFAULT = 430;

function treeResize() {
	const handle = $('tree-resize');
	const section = $('section-tree');
	const current = () => {
		const holder = ROOT.querySelector('#tree-holder .lineage-holder');
		return holder ? holder.getBoundingClientRect().height : TREE_DEFAULT;
	};

	handle.addEventListener('pointerdown', (event) => {
		event.preventDefault();
		handle.setPointerCapture(event.pointerId);
		handle.dataset.dragging = 'true';
		const startY = event.clientY;
		const startHeight = Math.max(current(),
			parseFloat(section.style.getPropertyValue('--tree-height')) || TREE_DEFAULT);
		const move = (e) => {
			const height = Math.max(TREE_MIN, Math.round(startHeight + e.clientY - startY));
			section.style.setProperty('--tree-height', height + 'px');
		};
		const up = () => {
			delete handle.dataset.dragging;
			handle.removeEventListener('pointermove', move);
			handle.removeEventListener('pointerup', up);
			handle.removeEventListener('pointercancel', up);
		};
		handle.addEventListener('pointermove', move);
		handle.addEventListener('pointerup', up);
		handle.addEventListener('pointercancel', up);
	});

	handle.addEventListener('dblclick', () => section.style.removeProperty('--tree-height'));
}

/* Every id the render functions need. A notebook saved with its output and
   reopened without re-running restores yesterday's markup under today's
   script; this names what is missing instead of rendering half a page. */
const REQUIRED_IDS = ['pulse', 'updated', 'run-root', 'hero-value', 'hero-sub',
	'tiles', 'sidebar', 'footer', 'clear-selection', 'tip',
	'catbar', 'objective-note', 'toggle-runs', 'toggle-failed', 'toggle-stair',
	'toggle-stages', 'toggle-ci',
	'main-headline', 'main-chart', 'scales-main', 'main-legend',
	'section-observers', 'observer-charts', 'section-validation',
	'validation-note', 'validation-charts', 'ended-line',
	'stage-strip', 'tree-holder',
	'tree-note', 'tree-legend', 'tree-resize', 'log', 'run-table', 'run-note',
	'host-summary', 'toggle-live', 'gpu-cards', 'gpu-scroll', 'process-card', 'process-body',
	'process-note'];

function reportStaleMarkup(missingIds) {
	const banner = el('div', { class: 'empty', id: 'stale-markup',
		style: 'border-color:var(--bad);color:var(--text);margin:var(--space-3)' }, [
		el('strong', { text: 'This page is older than the dashboard code it '
			+ 'just loaded.' }),
		el('div', { style: 'margin-top:var(--space-2)', text: `Missing: `
			+ `${missingIds.join(', ')}. This is saved notebook output being `
			+ 'restored instead of rebuilt; re-run the cell.' })]);

	ROOT.prepend(banner);
	console.error('stale markup; missing: ' + missingIds.join(', '));
}

/* A ghost toggle: flips aria-pressed and reports the new value. */
function toggle(id, onChange) {
	const node = $(id);

	node.addEventListener('click', () => {
		const on = node.getAttribute('aria-pressed') !== 'true';
		node.setAttribute('aria-pressed', String(on));
		onChange(on);
	});
}

function init() {
	const absent = REQUIRED_IDS.filter((id) => !$(id));

	// Rendering into half the markup would draw half a page under a banner
	// nobody reads; the banner alone says what to do.
	if (absent.length) {
		reportStaleMarkup(absent);
		return;
	}

	$('clear-selection').addEventListener('click', () => setSelection(null));
	toggle('toggle-live', setLive);
	setLive(false);
	toggle('toggle-runs', (on) => { state.showRuns = on; renderMain(); });
	toggle('toggle-failed', (on) => { state.hideFailed = !on; renderCharts(); });
	toggle('toggle-stair', (on) => { state.showStair = on; renderMain(); });
	toggle('toggle-stages', (on) => { state.showStages = on; renderMain(); });
	toggle('toggle-ci', (on) => { state.showCI = on; renderMain(); });
	treeResize();

	window.addEventListener('resize', () => { resizePlots(); markRowStarts('tiles'); });
	setTimeout(resizePlots, 350);

	pollState();
	pollResources();

	if (SNAPSHOT) {
		updatePulse();
		return;
	}

	// Two clocks, skipped while the tab is hidden.
	timers.push(setInterval(() => { if (!document.hidden) pollState(); },
		STATE_INTERVAL));
	timers.push(setInterval(() => { if (!document.hidden) pollResources(); },
		RESOURCE_INTERVAL));
	timers.push(setInterval(updatePulse, 2000));
}

/* Mount into `root`, reading through `transport`. A newer copy is a separate
   module instance with its own timers, so the previous one's are stopped
   first; without this a re-run cell leaves a second copy polling. */
window.startAutoopt = function (root, transport, options) {
	options = options || {};

	if (window.__autooptStop) window.__autooptStop();
	window.__autooptStop = () => { while (timers.length) clearInterval(timers.pop()); };

	ROOT = root;
	TRANSPORT = transport;
	plotted.clear();

	if (options.stateInterval) STATE_INTERVAL = options.stateInterval;
	if (options.resourceInterval) RESOURCE_INTERVAL = options.resourceInterval;
	WRITER_LOG = options.writerLog || null;
	SNAPSHOT = options.snapshot || null;
	lastUpdate = 0;
	lastSeen = {};

	// The quickest way to tell a rendering problem from a data problem.
	window.autooptDashboard = { state, setSelection, renderAll, renderMain,
		renderTree, reorder, xOf, draw, markRowStarts, colour: { resolved, emphasis,
		verdictColour, groupSlot, contextMarker, markerKey, CATCH_ALL, CAT_SLOTS,
		LINE_SLOTS } };

	init();
};

/* A static export (`autoopt_monitor.py --export`) carries every payload in
   one JSON script element and needs no server: read-only, no polling. */
const embedded = document.getElementById('autoopt-snapshot');

if (embedded) {
	const data = JSON.parse(embedded.textContent);
	const read = async (name) => {
		if (!(name in data.files)) throw new Error(name + ': not in this export');
		return data.files[name];
	};

	window.startAutoopt(document.body, {
		readme: (id) => read('readmes/' + id + '.json'),
		state: () => read('state.json'),
		resources: () => read('resources.json'),
	}, { snapshot: data.generated });

/* Standalone (tests): the directory serving this file serves the JSON. Module
   scripts are deferred and `document.currentScript` is null inside them, so
   the marker lives on <body>. `data-state-interval` and
   `data-resource-interval` on <body> shorten the clocks for a test. */
} else if (document.body && document.body.dataset.standalone !== undefined) {
	const json = async (name) => {
		const response = await fetch(name + '?_=' + Date.now());
		if (!response.ok) throw new Error(name + ': ' + response.status);
		return response.json();
	};

	const intervals = document.body.dataset;
	const start = () => window.startAutoopt(document.body, {
		state: () => json('state.json'),
		readme: (id) => json('readmes/' + id + '.json'),
		resources: () => json('resources.json'),
		live: async () => {
			const response = await fetch('live.json', { method: 'PUT',
				body: '{}' });
			if (!response.ok) throw new Error('live.json: ' + response.status);
		},
	}, { stateInterval: Number(intervals.stateInterval) || 0,
		resourceInterval: Number(intervals.resourceInterval) || 0 });

	if (document.readyState === 'loading')
		document.addEventListener('DOMContentLoaded', start);
	else
		start();
}
