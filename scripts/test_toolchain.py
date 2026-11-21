#!/usr/bin/env python3
"""Check the toolchain guard: the diff scan, the `## Toolchain` answer and the
refusal in `promote`.

	python3 test_toolchain.py     # a few seconds

A bare repository with a baseline branch and one branch per case, in a
temporary directory. The scan reads git's diff of a branch against its parent's,
so each case is a real commit. No network, no GPU.
"""

import os
import sys
import json
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import autoopt
import test_runner as tr

BASE_C = """int rank(int k) {
	return k + 1;
}

static inline void prefetch_block(const int *p, int k) {
	__builtin_prefetch(p + k);
}
"""

BASE_MAKE = "CC=gcc\nCFLAGS=-std=c99 -O3\n"

# One row per shortcut: (name, files, flagged). Each is a real commit on top
# of the baseline, scanned the way `finish` and `promote` scan it.
SHORTCUTS = (
	('Makefile -O3 -> -Ofast', {'Makefile': 'CC=gcc\nCFLAGS=-std=c99 -Ofast\n'}, True),
	('Makefile -O3 -> -O3 -march=native', {'Makefile': 'CC=gcc\nCFLAGS=-std=c99 -O3 -march=native\n'}, True),
	('-Ofast appended with nothing removed', {'Makefile': BASE_MAKE + 'CFLAGS+=-Ofast\n'}, True),
	('Makefile adds -ffast-math', {'Makefile': BASE_MAKE.replace('-O3', '-O3 -ffast-math')}, True),
	('Makefile adds -flto', {'Makefile': BASE_MAKE.replace('-O3', '-O3 -flto')}, True),
	('CC=gcc -> CC=clang', {'Makefile': 'CC=clang\nCFLAGS=-std=c99 -O3\n'}, True),
	('CC appended', {'Makefile': BASE_MAKE + 'CC=clang\n'}, True),
	('CFLAGS from $(OPT)', {'Makefile': 'CC=gcc\nCFLAGS=-std=c99 $(OPT)\n'}, True),
	('include of a new file', {'Makefile': BASE_MAKE + 'include fast.inc\n', 'fast.inc': 'CFLAGS+=-Ofast -march=native\n'}, True),
	('new flags.mk', {'flags.mk': 'CFLAGS+=-Ofast -march=native\n'}, True),
	('new Makefile.inc', {'Makefile.inc': 'CFLAGS+=-O3 -march=native\n'}, True),
	('CMakePresets.json', {'CMakePresets.json': '{"cacheVariables":{"CMAKE_C_FLAGS":"-O3 -march=native"}}\n'}, True),
	('env.sh export CFLAGS', {'env.sh': "export CFLAGS='-Ofast -march=native'\n"}, True),
	('build.sh', {'build.sh': 'gcc -Ofast -march=native -o bench bwt.c\n'}, True),
	('Makefile.am', {'Makefile.am': 'AM_CFLAGS=-Ofast -march=native\n'}, True),
	('cargo config.toml rustflags', {'config.toml': 'rustflags=["-C","target-cpu=native"]\n'}, True),
	('setup.py extra_compile_args', {'setup.py': "ext = Extension('x', extra_compile_args=['-Ofast'])\n"}, True),
	('optimize pragma in a block of added lines', {'bwt.c': '#pragma GCC optimize("O3,unroll-loops")\n#pragma GCC target("avx2")\n#include <stdint.h>\n#include <string.h>\n' + BASE_C}, True),
	('_Pragma', {'bwt.c': BASE_C.replace('int rank(int k) {', 'int rank(int k) {\n\t_Pragma("GCC optimize(\\"Ofast\\")")')}, True),
	('[[gnu::optimize]] on an existing function', {'bwt.c': BASE_C.replace('int rank', '[[gnu::optimize("O3")]] int rank')}, True),
	('optimize attribute on a new wrapper', {'bwt.c': BASE_C + '\n__attribute__((optimize("Ofast")))\nint rank2(int k) {\n\treturn rank(k);\n}\n'}, True),
	('asm in an existing function', {'bwt.c': BASE_C.replace('return k + 1;', '__asm__ volatile("" ::: "memory");\n\treturn k + 1;')}, True),
	('a .S assembly file', {'fast.S': '.text\nmov $1,%eax\nret\n'}, True),
	('a prebuilt library', {'libfast.a': '!<arch>\nbinary\n'}, True),
	('a compiled binary with no suffix', {'prog': '\x7fELF\x00\x00\x01'}, True),
	('a vendored tree', {'vendor/fast/f.c': 'int f(int k) { return k; }\n'}, True),
	('a plain new source file', {'fastlib.c': 'int f(int k) { return k; }\n'}, False),
	('a source edit with no flags', {'bwt.c': BASE_C.replace('k + 1', 'k * 2')}, False),
)


def run(*args, cwd):
	subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def build(base):
	"""`(root, commit)`: root is `<base>/autooptimize` with `repo.git`, whose
	branch autoopt/0 holds bwt.c and Makefile; `commit(id, files)` makes the
	branch autoopt/<id> from autoopt/0 with those files rewritten and writes
	the iteration's meta.json (parent 0)."""

	root = os.path.join(base, 'autooptimize')
	bare = os.path.join(root, 'repo.git')
	work = os.path.join(base, 'work')
	os.makedirs(root)
	run('git', 'init', '--bare', '-q', bare, cwd=base)
	run('git', 'clone', '-q', bare, work, cwd=base)
	run('git', 'config', 'user.email', 't@example.org', cwd=work)
	run('git', 'config', 'user.name', 't', cwd=work)
	run('git', 'checkout', '-q', '-b', 'autoopt/0', cwd=work)

	for name, text in (('bwt.c', BASE_C), ('Makefile', BASE_MAKE)):
		with open(os.path.join(work, name), 'w') as out:
			out.write(text)

	run('git', 'add', '-A', cwd=work)
	run('git', 'commit', '-q', '-m', 'base', cwd=work)
	run('git', 'push', '-q', 'origin', 'autoopt/0', cwd=work)

	def commit(identifier, files):
		run('git', 'checkout', '-q', '-B', 'autoopt/{}'.format(identifier),
			'autoopt/0', cwd=work)

		for name, text in files.items():
			os.makedirs(os.path.dirname(os.path.join(work, name)),
				exist_ok=True)

			with open(os.path.join(work, name), 'w') as out:
				out.write(text)

		run('git', 'add', '-A', cwd=work)
		run('git', 'commit', '-q', '-m', 'it {}'.format(identifier), cwd=work)
		run('git', 'push', '-q', 'origin', 'autoopt/{}'.format(identifier),
			cwd=work)
		folder = os.path.join(root, 'iterations', str(identifier))
		os.makedirs(folder, exist_ok=True)

		with open(os.path.join(folder, 'meta.json'), 'w') as out:
			json.dump({'id': identifier, 'parent': 0}, out)

	folder = os.path.join(root, 'iterations', '0')
	os.makedirs(folder)

	with open(os.path.join(folder, 'meta.json'), 'w') as out:
		json.dump({'id': 0, 'parent': None}, out)

	return root, commit


def main():
	suite = tr.Suite()
	base = tempfile.mkdtemp(prefix='toolchain-')
	root, commit = build(base)

	commit(1, {'bwt.c': BASE_C.replace('static inline void prefetch_block',
		'/* always_inline: gcc -O3 deletes the call */\n'
		'static inline __attribute__((always_inline)) void prefetch_block')})
	flagged = autoopt.toolchain_flags(root, 1)
	suite.check('an attribute added to an existing function is flagged',
		bool(flagged) and {name for name, _ in flagged} == {'bwt.c'}
		and 'always_inline' in flagged[0][1], flagged)

	commit(2, {'bwt.c': BASE_C + "\nstatic int fresh(int *a, int n) {\n"
		"#pragma omp simd\n\tfor (int i = 0; i < n; ++i) a[i] += 1;\n"
		"\treturn n;\n}\n"})
	suite.equal('a pragma inside a new function is not flagged',
		autoopt.toolchain_flags(root, 2), [])

	commit(3, {'bwt.c': BASE_C.replace('return k + 1;',
		'return k + 1;\n#pragma GCC unroll 4')})
	suite.check('a lone pragma added inside an existing function is flagged',
		bool(autoopt.toolchain_flags(root, 3)),
		autoopt.toolchain_flags(root, 3))

	commit(4, {'Makefile': BASE_MAKE.replace('-O3', '-O2 -flto')})
	flagged = autoopt.toolchain_flags(root, 4)
	suite.check('a changed optimization level or flag in the Makefile is '
		'flagged', bool(flagged) and {name for name, _ in flagged}
		== {'Makefile'} and '-O2' in flagged[0][1], flagged)

	commit(5, {'bwt.c': BASE_C.replace('k + 1', 'k + 2')})
	suite.equal('an ordinary edit is not flagged',
		autoopt.toolchain_flags(root, 5), [])

	commit(6, {'bwt.c': BASE_C.replace('k + 1', 'k + 2'),
		'README.txt': 'use -O2 and always_inline\n'})
	suite.equal('a flag named in a text file is not flagged',
		autoopt.toolchain_flags(root, 6), [])

	suite.equal('the baseline is never flagged',
		autoopt.toolchain_flags(root, 0), [])

	sections = [('The idea', 'x'), ('What happened', 'y')]
	answer, problem = autoopt.toolchain_problem(root, 1, sections)
	suite.check('a flagged diff with no `## Toolchain` section is refused',
		answer is None and problem and '## Toolchain' in problem, problem)

	answer, problem = autoopt.toolchain_problem(root, 5, sections)
	suite.check('an unflagged diff needs no section', (answer, problem) ==
		(None, None), (answer, problem))

	for text, want in (
		('Relies on a compiler decision: no\nThe change removes a copy.', 'no'),
		('relies on a compiler decision: YES\ngcc 13 deleted the calls.',
			'yes')):
		answer, problem = autoopt.toolchain_problem(root, 1,
			sections + [('Toolchain', text)])
		suite.equal('the answer {!r} is read'.format(text.split('\n')[0]),
			(answer, problem), (want, None))

	for text in ('Relies on a compiler decision: no',
		'Probably fine\nit is faster', ''):
		answer, problem = autoopt.toolchain_problem(root, 1,
			sections + [('Toolchain', text)])
		suite.check('a section without the answer line and a mechanism is '
			'refused: {!r}'.format(text[:30]), answer is None and problem,
			(answer, problem))

	suite.check('promote waits for an answer on a flagged iteration',
		'waits for an answer' in (autoopt.toolchain_gate(root, 1) or ''),
		autoopt.toolchain_gate(root, 1))

	for identifier, value, want in ((1, 'no', None),
		(1, 'specific', 'never promoted'), (5, None, None)):
		meta = autoopt.load_meta(root, identifier)

		if value:
			meta['toolchain'] = value
			autoopt.write_meta(root, identifier, meta)

		gate = autoopt.toolchain_gate(root, identifier)
		suite.check('the gate for A{} with toolchain {!r}'.format(identifier,
			value), (gate is None) if want is None else (want in (gate or '')),
			gate)

	for number, (name, files, want) in enumerate(SHORTCUTS, 100):
		commit(number, files)
		flagged = autoopt.toolchain_flags(root, number)
		suite.check('{}: {}'.format(name, 'flagged' if want
			else 'not flagged'), bool(flagged) == want, flagged)

	# Build environment: an exported CFLAGS never reaches a run.
	os.environ['CFLAGS'] = '-Ofast -march=native'
	os.environ['AUTOOPT_PROBE'] = 'kept'
	autoopt.BUILD_ENV.clear()
	autoopt.BUILD_ENV.update({'CC': 'gcc'})
	seen = os.path.join(base, 'env.txt')

	with open(seen, 'w') as out:
		autoopt.run_shell('echo "[$CFLAGS][$CC][$AUTOOPT_PROBE]"', base, out)

	with open(seen) as handle:
		suite.equal('a run sees the config\'s build variables, not the '
			'shell\'s, and every other variable', handle.read().strip(),
			'[][gcc][kept]')

	suite.equal('init records the build variables of its shell',
		autoopt.build_env_from(None)['CFLAGS'], '-Ofast -march=native')
	suite.equal('--build-env overrides the shell',
		autoopt.build_env_from(['CC=clang']), {'CC': 'clang'})
	del os.environ['CFLAGS'], os.environ['AUTOOPT_PROBE']
	autoopt.BUILD_ENV.clear()

	# Harness manifest: an edit to the harness stops the next run.
	folder = os.path.join(root, 'harness')
	os.makedirs(folder)

	with open(os.path.join(folder, 'build.sh'), 'w') as out:
		out.write('gcc -O3 bwt.c\n')

	autoopt.record_harness(root)
	suite.equal('an untouched harness passes', autoopt.harness_problem(root),
		None)

	with open(os.path.join(folder, 'build.sh'), 'w') as out:
		out.write('gcc -Ofast -march=native bwt.c\n')

	suite.check('an edited harness is refused, naming the file',
		'build.sh' in (autoopt.harness_problem(root) or ''),
		autoopt.harness_problem(root))

	# The gate applies to a whole winner too.
	problems, attested = autoopt.toolchain_chain(root, [5])
	suite.equal('an unflagged winner has no problem and nothing attested',
		(problems, attested), ([], []))
	problems, attested = autoopt.toolchain_chain(root, [100])
	suite.check('a flagged step with no answer is a problem',
		len(problems) == 1, problems)
	suite.check('and a build change is refused even when answered no',
		'no `## Toolchain` answer excuses it' in autoopt.toolchain_gate(root,
		100), autoopt.toolchain_gate(root, 100))
	meta = autoopt.load_meta(root, 100)
	meta['toolchain'] = 'no'
	autoopt.write_meta(root, 100, meta)
	problems, _ = autoopt.toolchain_chain(root, [100])
	suite.check('the answer no does not clear it', len(problems) == 1,
		problems)
	autoopt.write_json(os.path.join(root, 'config.json'),
		{'allow_build_changes': True})
	problems, attested = autoopt.toolchain_chain(root, [100])
	suite.check('allow_build_changes lets a flagged step through as attested',
		problems == [] and len(attested) >= 1, (problems, attested))

	shutil.rmtree(base, ignore_errors=True)
	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
