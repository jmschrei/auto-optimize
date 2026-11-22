#!/usr/bin/env python3
"""The checks of a change that need no measurement, in one script.

	python3 preflight.py WORK --id A12            # the iteration's own change, from its parent
	python3 preflight.py WORK --id best --path    # the whole path, from the commit the search copied

Four checks of the lines the change adds, each named in the output:

	text       comments that name the search (`lint_text.py`: winner, baseline,
	           a label such as A12, a path on this machine); the same in the
	           iteration's notes.md `## Change`, and the commit message rules
	           in its `## Commit message`, when it wrote them
	indent     an added line indented with tabs in a file indented with spaces,
	           or the reverse; a new file is compared with the repository's
	           files of the same extension
	toolchain  `after.oldest_toolchain_command`, run on an export of the tree
	           (`{src}`), exits non-zero: the oldest compiler or library the
	           repository supports does not build or pass. Skipped when the
	           key is unset
	license    an added line carries a license marker (an SPDX identifier, a
	           license name) of a family that cannot be added to a permissive
	           repository: GPL, LGPL, AGPL, MPL, a non-commercial or
	           share-alike terms. Code copied without any marker is not found

`autoopt.py promote` runs the first form in repository mode and refuses an
iteration with a problem, so it is fixed while its subagent is alive; the
second checks a whole winner at once.
`--path` writes `WORK/autooptimize/preflight.json` (`{winner, problems}`).

Exit codes: 0 no problem, 1 a problem (each on one line, `check: text`),
2 refused (not a repository search, no such iteration).

Standard library only, Python 3.8 or later; needs git.
"""

import os
import re
import sys
import json
import shutil
import argparse
import tempfile
import subprocess
import collections

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import autoopt
import lint_text

# License families a permissive repository cannot take code from
# (references/promotion.md, *Code from another project*).
FORBIDDEN = (('GPL', re.compile(r'(?<![A-Za-z0-9])(A|L)?GPL(-| )?(v)?\d|GNU '
	r'(Lesser |Affero |Library )?General Public', re.I)),
	('MPL', re.compile(r'(?<![A-Za-z0-9])MPL-\d|Mozilla Public License', re.I)),
	('non-commercial', re.compile(r'non-?commercial|academic use only|'
	r'research use only|CC-BY-NC', re.I)),
	('share-alike', re.compile(r'CC-BY-SA|ShareAlike', re.I)))
SAMPLE = 200


def fail(message):
	sys.stderr.write('preflight: {}\n'.format(message))
	sys.exit(2)


def leading(line):
	"""`tab`, `space` or None for a line's indentation."""

	if line.startswith('\t'):
		return 'tab'

	return 'space' if line.startswith('  ') else None


def style_of(lines):
	"""The indentation most lines use, `tab` or `space`, or None when fewer
	than five lines are indented or neither holds four fifths of them."""

	count = collections.Counter(kind for kind in map(leading, lines) if kind)
	total = sum(count.values())

	if total < 5:
		return None

	kind, most = count.most_common(1)[0]

	return kind if most >= 0.8 * total else None


def blob_lines(bare, tree, path):
	done = subprocess.run(['git', '-C', bare, 'show', '{}:{}'.format(tree,
		path)], capture_output=True)

	return done.stdout.decode('utf-8', 'replace').split('\n') if \
		done.returncode == 0 else None


def extension_style(bare, tree, path, cache):
	"""The style of the repository's files with `path`'s extension."""

	extension = os.path.splitext(path)[1]

	if extension not in cache:
		names = [name for name in subprocess.run(['git', '-C', bare, 'ls-tree',
			'-r', '--name-only', tree], capture_output=True, text=True).stdout
			.split('\n') if extension and name.endswith(extension)]
		lines = []

		for name in names[:20]:
			lines += (blob_lines(bare, tree, name) or [])[:SAMPLE]

		cache[extension] = style_of(lines)

	return cache[extension]


def indent_problems(bare, base, added):
	"""Added lines whose indentation is not the file's."""

	found, cache = [], {}

	for path, lines in sorted(added.items()):
		before = blob_lines(bare, base, path)
		style = style_of(before) if before is not None else extension_style(
			bare, base, path, cache)

		if not style:
			continue

		wrong = [number for number, text in lines if leading(text) not in (
			None, style)]

		if wrong:
			found.append('{}:{}: {} added line(s) are indented with {}s; the '
				'{} is indented with {}s'.format(path, wrong[0], len(wrong),
				'tab' if style == 'space' else 'space', 'file' if before is not
				None else 'repository\'s {} files'.format(os.path.splitext(
				path)[1]), style))

	return found


def license_problems(added):
	found = []

	for path, lines in sorted(added.items()):
		for number, text in lines:
			for family, pattern in FORBIDDEN:
				if pattern.search(text):
					found.append('{}:{}: the added line names a {} license, '
						'which cannot be added to a permissive repository: '
						'`{}`'.format(path, number, family, text.strip()[:120]))
					break

	return found


def toolchain_problems(root, config, tree):
	after = dict(autoopt.AFTER_DEFAULTS, **(config.get('after') or {}))
	command = after.get('oldest_toolchain_command')

	if not command:
		return []

	work = os.path.dirname(root)
	scratch = autoopt.tmp_dir(work)
	directory = tempfile.mkdtemp(prefix='preflight-', dir=scratch)

	try:
		archive = subprocess.run(['git', '-C', os.path.join(root, 'repo.git'),
			'archive', tree], capture_output=True, check=True)
		subprocess.run(['tar', '-x', '-C', directory], input=archive.stdout,
			check=True)
		done = subprocess.run(autoopt.fill(command, src=directory), shell=True,
			cwd=directory, capture_output=True, text=True, errors='replace',
			env=autoopt.clean_env(), stdin=subprocess.DEVNULL)
	finally:
		shutil.rmtree(directory, ignore_errors=True)

	if done.returncode == 0:
		return []

	lines = [line for line in (done.stdout + done.stderr).splitlines() if
		line.strip()]

	return ['after.oldest_toolchain_command exited {}: {}'.format(
		done.returncode, lines[-1][:300] if lines else 'no output')]


def problems(root, config, identifier, whole=False):
	"""`[(check, text)]` for iteration `identifier`: its own change, or with
	`whole` everything from the commit the search copied."""

	bare = os.path.join(root, 'repo.git')
	meta = autoopt.load_meta(root, identifier)

	if meta is None:
		fail('no iteration {}'.format(identifier))

	strip = autoopt.outputs_stripper(root, config)
	tree_of = lambda ref: strip(autoopt.git('-C', bare, 'rev-parse', ref +
		'^{tree}').strip())
	parent = meta.get('parent')
	base = tree_of(config['repo']['commit'] if whole or parent in (None, '',
		'0', 0) else 'autoopt/{}'.format(parent))
	tree = tree_of('autoopt/{}'.format(identifier))
	added = lint_text.added_lines(bare, tree, base)
	labels = [autoopt.label(root, i) for i in autoopt.iteration_ids(root)]
	found = [('text', item) for item in lint_text.diff_problems(bare, tree,
		base, labels)]
	steps = autoopt.lineage(root, identifier)[1:] if whole else [identifier]

	for step in steps:
		fragment = autoopt.change_fragment(root, step)
		name = autoopt.label(root, step)

		# An iteration made after the end is promoted by hand, after its
		# notes: what it lacks would be an agent's work at the review.
		if not whole and (autoopt.load_meta(root, step) or {}).get('after_end'):
			lacking = autoopt.fragment_problem(root, step)

			if lacking:
				found.append(('text', '{} notes.md {}'.format(name, lacking)))

		if fragment['change']:
			found += [('text', item) for item in lint_text.problems('{}\n{}'
				.format(fragment['name'], fragment['change']), '{} notes.md '
				'## Change'.format(name), labels)]

		if fragment['message']:
			found += [('text', '{} notes.md ## Commit message: {}'.format(name,
				item)) for item in autoopt.message_problems(root,
				autoopt.with_trailer(fragment['message'], autoopt.commit_trailer(
				root)))]

	found += [('indent', item) for item in indent_problems(bare, base, added)]
	found += [('license', item) for item in license_problems(added)]
	found += [('toolchain', item) for item in toolchain_problems(root, config,
		tree)]

	return found


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('work', help='the search\'s directory')
	parser.add_argument('--id', default='best', help='the iteration (default '
		'best)')
	parser.add_argument('--path', action='store_true', help='check the whole '
		'path from the commit the search copied, and write autooptimize/preflight.json')
	args = parser.parse_args(argv)
	root = autoopt.resolve(args.work)
	config = autoopt.load_config(root)

	if not config.get('repo'):
		fail('this search was not made with `init --repo`')

	identifier = autoopt.resolve_id(root, config, args.id)
	found = problems(root, config, identifier, args.path)

	if args.path:
		path = os.path.join(root, 'preflight.json')
		os.makedirs(os.path.dirname(path), exist_ok=True)
		autoopt.write_json(path, {'winner': identifier, 'problems': [list(item)
			for item in found]})

	for check, text in found:
		print('{}: {}'.format(check, text))

	if not found:
		print('preflight: no problem in {}'.format(autoopt.label(root,
			identifier)))

	return 1 if found else 0


if __name__ == '__main__':
	sys.exit(main())
