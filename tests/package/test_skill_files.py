"""What ships: each skill's frontmatter, that every reference file is routed
to and every path the text names exists, no test code inside a skill, and
one version number everywhere."""

import os
import re
import json
import glob

import pytest

from auto_optimize import __version__

from conftest import ROOT

SKILLS = sorted(glob.glob(os.path.join(ROOT, 'skills', '*', 'SKILL.md')))
FOLDERS = [os.path.dirname(path) for path in SKILLS]
IDS = [os.path.basename(folder) for folder in FOLDERS]


def read(path):
	with open(path, encoding='utf-8') as handle:
		return handle.read()


def frontmatter(folder):
	text = read(os.path.join(folder, 'SKILL.md'))
	block = re.match(r'---\n(.*?)\n---\n', text, re.S)
	assert block, 'SKILL.md must open with a --- frontmatter block'

	fields, key = {}, None

	for line in block.group(1).splitlines():
		match = re.match(r'^(\w[\w-]*):\s*(.*)$', line)
		if match:
			key = match.group(1)
			fields[key] = '' if match.group(2) in ('>-', '>', '|') \
				else match.group(2)
		elif key:
			fields[key] = (fields[key] + ' ' + line.strip()).strip()

	return fields


def skill_texts(folder):
	"""Every Markdown file of a skill, as {relative path: text}."""

	out = {}

	for path in glob.glob(os.path.join(folder, '**', '*.md'), recursive=True):
		out[os.path.relpath(path, folder)] = read(path)

	return out


def test_at_least_one_skill_ships():
	assert 'auto-optimize-code' in IDS


@pytest.mark.parametrize('folder', FOLDERS, ids=IDS)
def test_frontmatter_names_the_folder_and_describes_it(folder):
	fields = frontmatter(folder)

	assert fields.get('name') == os.path.basename(folder)
	assert fields.get('description')
	# Claude Code truncates a longer description in its skill listing, and
	# shows < and > as entities.
	assert len(fields['description']) <= 1536
	assert '<' not in fields['description'] and '>' not in fields['description']


@pytest.mark.parametrize('folder', FOLDERS, ids=IDS)
def test_every_reference_file_is_routed_from_skill_md(folder):
	routed = read(os.path.join(folder, 'SKILL.md'))
	files = sorted(os.path.basename(path) for path in glob.glob(os.path.join(
		folder, 'references', '*.md')))

	assert files
	assert [name for name in files if '`references/{}`'.format(name)
		not in routed] == []


@pytest.mark.parametrize('folder', FOLDERS, ids=IDS)
def test_every_named_reference_and_script_exists(folder):
	missing = []

	for name, text in skill_texts(folder).items():
		for path in re.findall(r'`(references/[\w.-]+\.md)', text):
			if not os.path.isfile(os.path.join(folder, path)):
				missing.append('{}: {}'.format(name, path))

		for path in re.findall(r'\$AO/([\w/.-]+\.py)', text):
			if not os.path.isfile(os.path.join(folder, 'scripts', path)):
				missing.append('{}: $AO/{}'.format(name, path))

		for path in re.findall(r'`(scripts/[\w/.-]+\.(?:py|md|js|css|html))`',
				text):
			if not os.path.isfile(os.path.join(folder, path)):
				missing.append('{}: {}'.format(name, path))

	assert missing == []


@pytest.mark.parametrize('folder', FOLDERS, ids=IDS)
def test_no_test_code_ships_inside_a_skill(folder):
	shipped = [os.path.relpath(path, folder) for path in glob.glob(os.path.join(
		folder, '**', '*.py'), recursive=True)]

	assert [path for path in shipped if os.path.basename(path).startswith(
		'test_') or os.path.basename(path) in ('make_fixture.py',
		'spacing_check.py', 'run_tests.py')] == []


@pytest.mark.parametrize('folder', FOLDERS, ids=IDS)
def test_each_skill_carries_the_license_and_the_version(folder):
	assert read(os.path.join(folder, 'LICENSE')) == read(os.path.join(ROOT,
		'LICENSE'))
	assert read(os.path.join(folder, 'VERSION')).strip() == __version__


def test_the_plugin_manifests_agree_with_the_package():
	plugin = json.loads(read(os.path.join(ROOT, '.claude-plugin',
		'plugin.json')))
	market = json.loads(read(os.path.join(ROOT, '.claude-plugin',
		'marketplace.json')))
	entry, = market['plugins']

	assert plugin['name'] == entry['name'] == market['name'] == 'auto-optimize'
	assert plugin['version'] == entry['version'] == __version__
	assert entry['source'] == './'


def test_the_changelog_has_a_section_for_this_version():
	text = read(os.path.join(ROOT, 'CHANGELOG.md'))

	assert re.search(r'^## \[{}\]'.format(re.escape(__version__)), text, re.M) \
		or re.search(r'^## \[Unreleased\]', text, re.M)
