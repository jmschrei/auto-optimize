"""The `auto-optimize` installer: where it installs, what it refuses, what it
finds, and that a failed install leaves the previous one in place."""

import os
import sys
import json
import shutil
import subprocess

import pytest

from auto_optimize import cli
from auto_optimize import __version__

from conftest import ROOT


def make_skill(folder, name='auto-optimize-code', version='9.9.9'):
	"""A minimal skill folder, with the caches the installer must skip."""

	os.makedirs(os.path.join(folder, 'scripts', '__pycache__'))
	os.makedirs(os.path.join(folder, '.ipynb_checkpoints'))

	with open(os.path.join(folder, 'SKILL.md'), 'w') as handle:
		handle.write('---\nname: {}\ndescription: a test skill\n---\n\n# x\n'
			.format(name))
	with open(os.path.join(folder, 'VERSION'), 'w') as handle:
		handle.write(version + '\n')
	with open(os.path.join(folder, 'scripts', 'tool.py'), 'w') as handle:
		handle.write('print(1)\n')
	with open(os.path.join(folder, 'scripts', '__pycache__', 'tool.pyc'),
			'w') as handle:
		handle.write('')

	return folder


@pytest.fixture
def home(tmp_path, monkeypatch):
	"""A private ~/.claude, plugin cache and working directory."""

	config = tmp_path / 'config'
	monkeypatch.setenv('HOME', str(tmp_path / 'home'))
	monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(config))
	monkeypatch.setenv('CLAUDE_CODE_PLUGIN_CACHE_DIR', str(tmp_path /
		'plugins'))
	project = tmp_path / 'project'
	project.mkdir()
	monkeypatch.chdir(project)

	return tmp_path


def test_bundled_skills_are_found_in_the_checkout():
	skills = cli.bundled_skills()

	assert 'auto-optimize-code' in skills
	assert os.path.samefile(skills['auto-optimize-code'], os.path.join(ROOT,
		'skills', 'auto-optimize-code'))


def test_choose_takes_the_full_name_or_the_suffix():
	available = {'auto-optimize-code': '/x'}

	assert cli.choose([], available) == ['auto-optimize-code']
	assert cli.choose(['code'], available) == ['auto-optimize-code']
	assert cli.choose(['auto-optimize-code', 'code'], available) == [
		'auto-optimize-code']

	with pytest.raises(cli.InstallError, match='no skill named'):
		cli.choose(['ml'], available)


def test_target_dir_defaults_to_the_config_dir(home):
	args = cli.parser().parse_args(['install'])
	assert cli.target_dir(args) == str(home / 'config' / 'skills')

	args = cli.parser().parse_args(['install', '--project'])
	assert cli.target_dir(args) == str(home / 'project' / '.claude' / 'skills')

	args = cli.parser().parse_args(['install', '-d', str(home / 'elsewhere')])
	assert cli.target_dir(args) == str(home / 'elsewhere')


def test_project_and_directory_are_exclusive():
	with pytest.raises(SystemExit):
		cli.parser().parse_args(['install', '--project', '-d', 'x'])


def test_install_copies_skips_caches_and_writes_the_marker(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	dest = cli.install_one('auto-optimize-code', source, str(tmp_path / 's'))

	assert os.path.isfile(os.path.join(dest, 'SKILL.md'))
	assert os.path.isfile(os.path.join(dest, 'scripts', 'tool.py'))
	assert not os.path.exists(os.path.join(dest, 'scripts', '__pycache__'))
	assert not os.path.exists(os.path.join(dest, '.ipynb_checkpoints'))

	with open(os.path.join(dest, cli.MARKER)) as handle:
		marker = json.load(handle)

	assert marker['name'] == 'auto-optimize-code'
	assert marker['version'] == '9.9.9'
	assert marker['mode'] == 'copy'
	assert marker['installer'] == 'auto-optimize ' + __version__


def test_reinstall_replaces_its_own_copy_without_force(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = str(tmp_path / 's')
	dest = cli.install_one('auto-optimize-code', source, skills)

	with open(os.path.join(dest, 'stale.md'), 'w') as handle:
		handle.write('from an earlier version\n')

	cli.install_one('auto-optimize-code', source, skills)

	assert not os.path.exists(os.path.join(dest, 'stale.md'))
	assert sorted(os.listdir(skills)) == ['auto-optimize-code']


def test_a_folder_it_did_not_create_needs_force(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = tmp_path / 's'
	theirs = skills / 'auto-optimize-code'
	theirs.mkdir(parents=True)
	(theirs / 'mine.md').write_text('hand edited\n')

	with pytest.raises(cli.InstallError, match='--force'):
		cli.install_one('auto-optimize-code', source, str(skills))

	assert (theirs / 'mine.md').exists()

	cli.install_one('auto-optimize-code', source, str(skills), force=True)

	assert not (theirs / 'mine.md').exists()
	assert (theirs / 'SKILL.md').exists()


def test_symlink_points_at_the_source_and_is_replaced_in_place(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = str(tmp_path / 's')
	dest = cli.install_one('auto-optimize-code', source, skills, symlink=True)

	assert os.path.islink(dest)
	assert os.path.realpath(dest) == os.path.realpath(source)
	assert not os.path.exists(os.path.join(source, cli.MARKER))
	assert cli.ownership(dest, 'auto-optimize-code')[0] == 'link'

	cli.install_one('auto-optimize-code', source, skills)

	assert not os.path.islink(dest)
	assert os.path.isfile(os.path.join(dest, cli.MARKER))


def test_a_symlink_to_another_skill_is_foreign(tmp_path):
	other = make_skill(str(tmp_path / 'other'), name='something-else')
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = tmp_path / 's'
	skills.mkdir()
	os.symlink(other, str(skills / 'auto-optimize-code'))

	with pytest.raises(cli.InstallError, match='holds no auto-optimize-code'):
		cli.install_one('auto-optimize-code', source, str(skills))


def test_a_dangling_symlink_is_replaced_without_force(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = tmp_path / 's'
	skills.mkdir()
	os.symlink(str(tmp_path / 'gone'), str(skills / 'auto-optimize-code'))

	assert cli.ownership(str(skills / 'auto-optimize-code'),
		'auto-optimize-code')[0] == 'stale'

	dest = cli.install_one('auto-optimize-code', source, str(skills))

	assert os.path.isfile(os.path.join(dest, 'SKILL.md'))


def test_a_failed_copy_leaves_the_previous_install(tmp_path, monkeypatch):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = str(tmp_path / 's')
	dest = cli.install_one('auto-optimize-code', source, skills)

	def broken(*args, **kwargs):
		os.makedirs(args[1])
		raise OSError('disk full')

	monkeypatch.setattr(cli.shutil, 'copytree', broken)

	with pytest.raises(OSError, match='disk full'):
		cli.install_one('auto-optimize-code', source, skills)

	assert os.path.isfile(os.path.join(dest, cli.MARKER))
	assert sorted(os.listdir(skills)) == ['auto-optimize-code']


def test_uninstall_removes_only_what_it_installed(tmp_path):
	source = make_skill(str(tmp_path / 'src' / 'auto-optimize-code'))
	skills = tmp_path / 's'

	assert cli.uninstall_one('auto-optimize-code', str(skills)) is None

	cli.install_one('auto-optimize-code', source, str(skills))
	assert cli.uninstall_one('auto-optimize-code', str(skills)) == str(
		skills / 'auto-optimize-code')
	assert not (skills / 'auto-optimize-code').exists()

	(skills / 'auto-optimize-code').mkdir()

	with pytest.raises(cli.InstallError, match='--force'):
		cli.uninstall_one('auto-optimize-code', str(skills))

	cli.uninstall_one('auto-optimize-code', str(skills), force=True)
	assert not (skills / 'auto-optimize-code').exists()


def test_installations_finds_user_project_and_plugin_copies(home):
	source = make_skill(str(home / 'src' / 'auto-optimize-code'))
	cli.install_one('auto-optimize-code', source, str(home / 'config' /
		'skills'))
	cli.install_one('auto-optimize-code', source, str(home / 'project' /
		'.claude' / 'skills'), symlink=True)

	plugin = home / 'plugins' / 'cache' / 'auto-optimize' / 'auto-optimize' / \
		'1.0.0'
	make_skill(str(plugin / 'skills' / 'auto-optimize-code'))
	(home / 'plugins' / 'installed_plugins.json').write_text(json.dumps(
		{'version': 2, 'plugins': {cli.PLUGIN_ID: [{'scope': 'user',
			'installPath': str(plugin), 'version': '1.0.0'}]}}))

	found = cli.installations()
	modes = sorted((item['scope'], item['mode']) for item in found)

	assert modes == [('project', 'symlink'), ('user', 'copy'),
		('user', 'plugin ' + cli.PLUGIN_ID)]
	assert cli.duplicates(found) == ['auto-optimize-code']


def test_installations_ignores_other_skills_and_stale_links(home):
	skills = home / 'config' / 'skills'
	make_skill(str(skills / 'figure-aesthetics'), name='figure-aesthetics')
	os.symlink(str(home / 'gone'), str(skills / 'auto-optimize-code'))

	assert cli.installations() == []


def test_installations_skips_another_authors_skill_with_the_prefix(home):
	skills = home / 'config' / 'skills'
	make_skill(str(skills / 'auto-optimize-other'), name='auto-optimize-other')

	assert cli.installations() == []


def test_an_earlier_copy_named_auto_optimize_is_flagged(home, capsys):
	skills = home / 'config' / 'skills'
	make_skill(str(skills / 'auto-optimize'), name='auto-optimize')

	found = cli.installations()
	assert [(item['name'], item.get('legacy')) for item in found] == [
		('auto-optimize', True)]

	assert cli.main(['list']) == 0
	assert 'before 1.0.0' in capsys.readouterr().out

	cli.main(['doctor'])
	out = capsys.readouterr().out
	assert 'installed skills: none' in out and 'before 1.0.0' in out


def test_doctor_passes_over_the_installers_own_environment(tmp_path,
		monkeypatch):
	tool = tmp_path / 'tool'
	user = tmp_path / 'user'
	for folder in (tool / 'bin', user):
		folder.mkdir(parents=True)
	for folder in (tool / 'bin', user):
		script = folder / 'python3'
		script.write_text('#!/bin/sh\n')
		script.chmod(0o755)

	monkeypatch.setattr(sys, 'prefix', str(tool))
	monkeypatch.setattr(sys, 'base_prefix', str(tmp_path / 'base'))
	monkeypatch.delenv('VIRTUAL_ENV', raising=False)
	monkeypatch.setenv('PATH', os.pathsep.join([str(tool / 'bin'),
		str(user)]))
	assert cli.skill_python3() == str(user / 'python3')

	monkeypatch.setenv('VIRTUAL_ENV', str(tool))
	assert cli.skill_python3() == str(tool / 'bin' / 'python3')


def test_main_install_list_path_uninstall(home, capsys):
	assert cli.main(['install']) == 0
	installed = home / 'config' / 'skills' / 'auto-optimize-code'
	assert (installed / 'SKILL.md').is_file()
	assert not list(installed.rglob('test_*.py'))

	assert cli.main(['list']) == 0
	assert str(installed) in capsys.readouterr().out

	assert cli.main(['path']) == 0
	assert capsys.readouterr().out.strip() == str(installed)

	assert cli.main(['path', '--bundled']) == 0
	assert os.path.samefile(capsys.readouterr().out.strip(), os.path.join(
		ROOT, 'skills', 'auto-optimize-code'))

	assert cli.main(['uninstall', 'code']) == 0
	assert not installed.exists()
	assert cli.main(['path']) == 1


def test_main_reports_a_refusal_as_one_line(home, capsys):
	(home / 'config' / 'skills' / 'auto-optimize-code').mkdir(parents=True)

	assert cli.main(['install']) == 1
	error = capsys.readouterr().err

	assert error.startswith('error: ') and '--force' in error
	assert error.count('\n') == 1


def test_doctor_checks_the_python3_on_path(home, capsys):
	code = cli.main(['doctor'])
	out = capsys.readouterr().out

	assert 'python3 3.9 or later on PATH' in out
	assert 'installed skills: none' in out
	assert code == 1

	cli.main(['install'])
	capsys.readouterr()
	cli.main(['doctor'])
	assert 'installed skills: auto-optimize-code (user, copy)' in \
		capsys.readouterr().out


def test_the_module_runs_as_a_command():
	done = subprocess.run([sys.executable, '-m', 'auto_optimize', '--version'],
		capture_output=True, text=True, cwd=ROOT, env=dict(os.environ,
			PYTHONPATH=os.path.join(ROOT, 'src')))

	assert done.returncode == 0, done.stderr
	assert done.stdout.strip() == 'auto-optimize ' + __version__
