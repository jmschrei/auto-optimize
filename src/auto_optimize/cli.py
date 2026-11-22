"""Install, list, locate and check the auto-optimize Claude Code skills.

	auto-optimize install [SKILL ...] [--project [DIR] | -d DIR] [--symlink]
	                      [-f]
	auto-optimize uninstall [SKILL ...] [--project [DIR] | -d DIR] [-f]
	auto-optimize list
	auto-optimize path [SKILL] [--bundled]
	auto-optimize doctor

A skill is named in full (auto-optimize-code) or by the part after
`auto-optimize-` (code). With no SKILL, install and uninstall act on every
skill this package ships.

Claude Code reads user skills from ~/.claude/skills/ ($CLAUDE_CONFIG_DIR/skills
when that is set) and project skills from <project>/.claude/skills/. Nothing is
installed on `pip install`; only `auto-optimize install` writes there.

Standard library only, Python 3.9 or later.
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import datetime
import subprocess

from . import __version__

PREFIX = 'auto-optimize-'
MARKER = '.install.json'
PLUGIN_ID = 'auto-optimize@auto-optimize'
PYTHON_FLOOR = (3, 9)

# Never copied: caches a test run or a notebook leaves in the source, and the
# marker, which each install writes fresh.
IGNORE = shutil.ignore_patterns('__pycache__', '*.pyc', '.pytest_cache',
	'.ipynb_checkpoints', MARKER)

# Imported by the dashboard, checked in the python3 the skill runs.
OPTIONAL = (
	('plotly', 'plotly', 'the dashboard charts and its static --export'),
	('IPython', 'ipython', 'showing the dashboard in a notebook'),
	('jupyterlab', 'jupyterlab', 'serving the dashboard notebook'),
	('psutil', 'psutil', 'the dashboard host and process panels'),
)


class InstallError(Exception):
	pass


###
# The shipped skills
###

def bundled_root():
	"""The folder of shipped skills: `skills/` inside the installed package,
	or `skills/` at the top of a source checkout."""

	here = os.path.dirname(os.path.abspath(__file__))

	for candidate in (os.path.join(here, 'skills'), os.path.normpath(
			os.path.join(here, os.pardir, os.pardir, 'skills'))):
		if os.path.isdir(candidate):
			return candidate

	raise InstallError('no skills folder in {} or in a source checkout above '
		'it; the package is incomplete'.format(here))


def bundled_skills():
	"""{name: folder} for every shipped skill, in name order."""

	root = bundled_root()

	return {name: os.path.join(root, name) for name in sorted(os.listdir(root))
		if os.path.isfile(os.path.join(root, name, 'SKILL.md'))}


def frontmatter_name(folder):
	"""The `name:` in a folder's SKILL.md frontmatter, or None."""

	try:
		with open(os.path.join(folder, 'SKILL.md'), encoding='utf-8') as handle:
			text = handle.read()
	except OSError:
		return None

	block = re.match(r'---\n(.*?)\n---\n', text, re.S)
	found = block and re.search(r'^name:\s*(\S+)\s*$', block.group(1), re.M)

	return found.group(1) if found else None


def skill_version(folder):
	"""The skill's VERSION file, or None."""

	try:
		with open(os.path.join(folder, 'VERSION')) as handle:
			return handle.read().strip() or None
	except OSError:
		return None


def choose(names, available):
	"""Full skill names for `names` (all of `available` when empty)."""

	if not names:
		return list(available)

	chosen = []

	for name in names:
		full = name if name.startswith(PREFIX) else PREFIX + name

		if full not in available:
			raise InstallError('no skill named {!r}; this package ships {}'
				.format(name, ', '.join(available) or 'none'))

		if full not in chosen:
			chosen.append(full)

	return chosen


###
# Where skills go
###

def config_dir():
	"""~/.claude, or $CLAUDE_CONFIG_DIR when set."""

	return os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(
		os.path.expanduser('~'), '.claude')


def user_skills_dir():
	return os.path.join(config_dir(), 'skills')


def target_dir(args):
	"""The skills folder an install or uninstall acts on."""

	if args.directory:
		return os.path.abspath(os.path.expanduser(args.directory))

	if args.project is not None:
		return os.path.join(os.path.abspath(os.path.expanduser(args.project)),
			'.claude', 'skills')

	return user_skills_dir()


def ownership(dest, name):
	"""What is at `dest`: ('missing'|'copy'|'link'|'stale'|'foreign', why).

	`copy` carries this installer's marker; `link` is a symlink to a folder
	whose SKILL.md is named `name`; `stale` is a symlink to nothing. Anything
	else is `foreign`, and replacing it needs --force.
	"""

	if not os.path.lexists(dest):
		return 'missing', None

	if os.path.islink(dest):
		if not os.path.exists(dest):
			return 'stale', None
		if frontmatter_name(os.path.realpath(dest)) == name:
			return 'link', None
		return 'foreign', 'is a symlink to {}, which holds no {} skill'.format(
			os.readlink(dest), name)

	if os.path.isdir(dest) and os.path.isfile(os.path.join(dest, MARKER)):
		return 'copy', None

	return 'foreign', 'was not installed by auto-optimize (it has no ' \
		'{})'.format(MARKER)


def remove(path):
	if os.path.islink(path) or os.path.isfile(path):
		os.unlink(path)
	else:
		shutil.rmtree(path)


###
# install, uninstall
###

def install_one(name, source, skills_dir, symlink=False, force=False):
	"""Put the skill `name` from `source` at `skills_dir/name`, replacing an
	earlier install. Returns the destination.

	The new copy is built beside the destination and renamed into place, so a
	failure part-way leaves the previous install as it was.
	"""

	dest = os.path.join(skills_dir, name)
	kind, why = ownership(dest, name)

	if kind == 'foreign' and not force:
		raise InstallError('{} {}; re-run with --force to replace it'.format(
			dest, why))

	os.makedirs(skills_dir, exist_ok=True)
	staged = os.path.join(skills_dir, '.{}.new-{}'.format(name, os.getpid()))
	old = os.path.join(skills_dir, '.{}.old-{}'.format(name, os.getpid()))

	try:
		if symlink:
			os.symlink(os.path.realpath(source), staged)
		else:
			shutil.copytree(source, staged, ignore=IGNORE)
			with open(os.path.join(staged, MARKER), 'w') as handle:
				json.dump({'name': name, 'version': skill_version(source),
					'installer': 'auto-optimize {}'.format(__version__),
					'source': os.path.realpath(source), 'mode': 'copy',
					'installed': datetime.datetime.now().isoformat(
						timespec='seconds')}, handle, indent='\t')
				handle.write('\n')
	except BaseException:
		if os.path.lexists(staged):
			remove(staged)
		raise

	moved = False

	try:
		if os.path.lexists(dest):
			os.rename(dest, old)
			moved = True
		os.rename(staged, dest)
	except BaseException:
		if moved and not os.path.lexists(dest):
			os.rename(old, dest)
		if os.path.lexists(staged):
			remove(staged)
		raise

	if moved:
		remove(old)

	return dest


def uninstall_one(name, skills_dir, force=False):
	"""Remove `skills_dir/name` if this installer put it there. Returns the
	removed path, or None when nothing was there."""

	dest = os.path.join(skills_dir, name)
	kind, why = ownership(dest, name)

	if kind == 'missing':
		return None

	if kind == 'foreign' and not force:
		raise InstallError('{} {}; re-run with --force to remove it'.format(
			dest, why))

	remove(dest)

	return dest


def install(args):
	skills = bundled_skills()
	skills_dir = target_dir(args)

	for name in choose(args.skills, skills):
		dest = install_one(name, skills[name], skills_dir, args.symlink,
			args.force)
		print('installed {} {} at {}{}'.format(name, skill_version(
			skills[name]) or '', dest, ' -> ' + os.path.realpath(dest)
			if args.symlink else ''))

	if duplicates(installations()):
		print('note: a skill is installed more than once; `auto-optimize list` '
			'shows where', file=sys.stderr)

	return 0


def uninstall(args):
	skills_dir = target_dir(args)

	for name in choose(args.skills, bundled_skills()):
		removed = uninstall_one(name, skills_dir, args.force)
		print('removed {}'.format(removed) if removed else
			'{} is not installed in {}'.format(name, skills_dir))

	return 0


###
# Finding every copy Claude Code would load
###

def project_dirs(start=None):
	"""`.claude/skills` in `start` and each parent up to the git repository's
	top, the folders Claude Code reads project skills from."""

	start = os.path.abspath(start or os.getcwd())
	top = subprocess.run(['git', '-C', start, 'rev-parse', '--show-toplevel'],
		capture_output=True, text=True) if shutil.which('git') else None
	top = os.path.abspath(top.stdout.strip()) if top and top.returncode == 0 \
		else start

	out, here = [], start

	while True:
		out.append(os.path.join(here, '.claude', 'skills'))
		parent = os.path.dirname(here)
		if here == top or parent == here or not here.startswith(top):
			break
		here = parent

	return out


def plugin_installs():
	"""[(scope, install path)] for the auto-optimize plugin, from Claude
	Code's record of installed plugins."""

	root = os.environ.get('CLAUDE_CODE_PLUGIN_CACHE_DIR') or os.path.join(
		config_dir(), 'plugins')

	try:
		with open(os.path.join(root, 'installed_plugins.json')) as handle:
			record = json.load(handle)
	except (OSError, ValueError):
		return []

	entries = (record.get('plugins') or {}).get(PLUGIN_ID) if isinstance(
		record, dict) else None

	if isinstance(entries, dict):
		entries = [entries]

	return [(entry.get('scope') or 'plugin', entry['installPath'])
		for entry in entries or [] if isinstance(entry, dict)
		and entry.get('installPath')]


def installations(start=None):
	"""Every auto-optimize-* skill Claude Code would load from here: dicts
	of name, version, scope, mode and path."""

	found, seen = [], set()
	user = user_skills_dir()
	places = [('user', user)] + [('project', d) for d in project_dirs(start)
		if os.path.abspath(d) != os.path.abspath(user)]

	for scope, folder in places:
		if not os.path.isdir(folder):
			continue

		for name in sorted(os.listdir(folder)):
			path = os.path.join(folder, name)

			if not name.startswith(PREFIX) or path in seen:
				continue

			kind, _ = ownership(path, frontmatter_name(path) or name)

			if kind == 'stale' or not os.path.isfile(os.path.join(path,
					'SKILL.md')):
				continue

			seen.add(path)
			found.append({'name': frontmatter_name(path) or name,
				'version': skill_version(path), 'scope': scope,
				'mode': {'copy': 'copy', 'link': 'symlink'}.get(kind,
					'not installed by auto-optimize'), 'path': path})

	for scope, root in plugin_installs():
		folder = os.path.join(root, 'skills')

		for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
			path = os.path.join(folder, name)

			if os.path.isfile(os.path.join(path, 'SKILL.md')):
				found.append({'name': frontmatter_name(path) or name,
					'version': skill_version(path), 'scope': scope,
					'mode': 'plugin ' + PLUGIN_ID, 'path': path})

	return found


def duplicates(found):
	"""Names installed more than once."""

	names = [item['name'] for item in found]

	return sorted({name for name in names if names.count(name) > 1})


def list_command(args):
	found = installations()

	if not found:
		print('no auto-optimize skill is installed for {}'.format(os.getcwd()))
		return 0

	rows = [(item['name'], item['version'] or '-', item['scope'],
		item['mode'], item['path']) for item in found]
	header = ('skill', 'version', 'scope', 'how', 'path')
	widths = [max(len(row[i]) for row in rows + [header]) for i in range(4)]

	for row in [header] + rows:
		print('  '.join(row[i].ljust(widths[i]) for i in range(4)) + '  '
			+ row[4])

	for name in duplicates(found):
		print('\nnote: {} is installed more than once. Claude Code loads every '
			'copy and their descriptions compete for the same requests; keep '
			'one (`auto-optimize uninstall`, or `claude plugin uninstall {}`)'
			.format(name, PLUGIN_ID))

	return 0


def path_command(args):
	skills = bundled_skills()

	if args.skill:
		name = choose([args.skill], skills)[0]
	elif len(skills) == 1:
		name = next(iter(skills))
	else:
		raise InstallError('this package ships {}; name one'.format(
			', '.join(skills)))

	if args.bundled:
		print(skills[name])
		return 0

	for item in installations():
		if item['name'] == name:
			print(item['path'])
			return 0

	print('{} is not installed; `auto-optimize install {}`'.format(name,
		name[len(PREFIX):]), file=sys.stderr)

	return 1


###
# doctor
###

def run(words):
	"""(exit code, stdout) of a command, or (None, '') when it cannot start."""

	try:
		done = subprocess.run(words, capture_output=True, text=True,
			timeout=60)
	except (OSError, subprocess.TimeoutExpired):
		return None, ''

	return done.returncode, done.stdout.strip()


def doctor(args):
	"""Check what the skill needs, in the python3 the skill runs (the one
	on PATH, not this installer's), and every installed copy."""

	problems, lines = 0, []

	def report(ok, text, required=True):
		nonlocal problems
		problems += 0 if ok or not required else 1
		lines.append('{:<9}{}'.format('ok' if ok else 'MISSING' if required
			else 'optional', text))

	report(sys.platform.startswith('linux'), 'Linux (this is {}); the '
		'scripts read /proc and take file locks'.format(sys.platform))

	python3 = shutil.which('python3')
	code, out = run([python3, '-c', 'import sys; print("%d.%d.%d" % '
		'sys.version_info[:3])']) if python3 else (None, '')
	version = tuple(int(part) for part in out.split('.')) if code == 0 else ()
	report(version >= PYTHON_FLOOR, 'python3 {} or later on PATH: {}'.format(
		'.'.join(map(str, PYTHON_FLOOR)), '{} at {}'.format(out, python3)
		if version else 'not found'))

	for tool, why in (('git', 'every iteration is a git worktree'),
			('tar', 'the checks export an iteration\'s tree')):
		where = shutil.which(tool)
		report(bool(where), '{} ({}): {}'.format(tool, why, where or
			'not found'))

	missing = []

	if version:
		for module, package, why in OPTIONAL:
			code, _ = run([python3, '-c', 'import importlib.util, sys; '
				'sys.exit(importlib.util.find_spec({!r}) is None)'.format(
				module)])
			report(code == 0, '{} ({}){}'.format(module, why, '' if code == 0
				else ': not installed'), required=False)
			if code != 0:
				missing.append(package)

	claude = shutil.which('claude')
	report(bool(claude), 'Claude Code (`claude`): {}'.format(claude or
		'not on PATH'), required=False)

	found = installations()
	report(bool(found), 'installed skills: {}'.format(', '.join(
		'{} ({}, {})'.format(item['name'], item['scope'], item['mode'])
		for item in found) or 'none; `auto-optimize install`'))

	print('\n'.join(lines))

	if missing:
		print('\nfor the dashboard: {} -m pip install {}'.format(python3,
			' '.join(missing)))

	for name in duplicates(found):
		print('\nnote: {} is installed more than once; `auto-optimize list` '
			'shows where'.format(name))

	return 1 if problems else 0


###
# The command line
###

def parser():
	top = argparse.ArgumentParser(prog='auto-optimize', description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	top.add_argument('--version', action='version',
		version='auto-optimize ' + __version__)
	commands = top.add_subparsers(dest='command', metavar='COMMAND')
	commands.required = True

	def where(sub, verb):
		group = sub.add_mutually_exclusive_group()
		group.add_argument('--project', nargs='?', const='.', metavar='DIR',
			help='{} the project skills folder, DIR/.claude/skills (DIR '
			'defaults to the current directory)'.format(verb))
		group.add_argument('-d', '--directory', metavar='DIR',
			help='{} DIR/<skill> instead of ~/.claude/skills/<skill>'.format(
				verb))
		sub.add_argument('-f', '--force', action='store_true',
			help='replace or remove a folder this installer did not create')

	one = commands.add_parser('install', help='copy the skills into a '
		'skills folder (the user folder by default)')
	one.add_argument('skills', nargs='*', metavar='SKILL')
	where(one, 'install into')
	one.add_argument('--symlink', action='store_true', help='link to this '
		'package\'s copy instead of copying it; edits there show up at once, '
		'and the link breaks if the package moves')
	one.set_defaults(func=install)

	two = commands.add_parser('uninstall', help='remove skills this '
		'installer put in a skills folder')
	two.add_argument('skills', nargs='*', metavar='SKILL')
	where(two, 'remove from')
	two.set_defaults(func=uninstall)

	three = commands.add_parser('list', help='every installed copy Claude '
		'Code would load from here: user, project and plugin')
	three.set_defaults(func=list_command)

	four = commands.add_parser('path', help='the folder of an installed '
		'skill, e.g. AO="$(auto-optimize path)/scripts"')
	four.add_argument('skill', nargs='?', metavar='SKILL')
	four.add_argument('--bundled', action='store_true', help='the copy '
		'inside this package instead')
	four.set_defaults(func=path_command)

	five = commands.add_parser('doctor', help='check python3, git, tar, the '
		'dashboard\'s optional modules and the installed copies')
	five.set_defaults(func=doctor)

	return top


def main(argv=None):
	args = parser().parse_args(argv)

	try:
		return args.func(args)
	except InstallError as error:
		print('error: {}'.format(error), file=sys.stderr)
		return 1


if __name__ == '__main__':
	sys.exit(main())
