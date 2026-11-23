#!/usr/bin/env python3
"""Check the bug report's renderer, its verify step and its filing.

	python3 test_issue_body.py            # a few seconds
	python3 test_issue_body.py --update   # rewrite fixtures/issue/expected.md

The renderer is checked against eight of the 25 bugs of haowenz/chromap#210,
`fixtures/issue/data/`, whose body must equal `fixtures/issue/expected.md`
byte for byte, twice, and have #210's layout; then each refusal, the move of
the longest reproductions to comments, and `template`. `verify` is checked on
small reproductions built here (echo, a failing line, a timeout, a failing
setup, masks, a manual mark) with a shell script as the binary. `file` posts
through a fake `gh` on the PATH and `dupes` reads `--issues FILE` or that
fake, so nothing reaches the network. `--update` is for a deliberate change to
the renderer: read the diff of expected.md before keeping it.

No network; needs bash.
"""

import os
import re
import sys
import json
import time
import shutil
import argparse
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
# The skill's scripts, which these tests import and run.
SCRIPTS = os.path.normpath(os.path.join(HERE, os.pardir, os.pardir, 'skills',
	'auto-optimize-code', 'scripts'))
sys.path.insert(0, SCRIPTS)

import publish
import issue_body
import attribution
import test_runner

BODY = os.path.join(SCRIPTS, 'issue_body.py')
ATTRIBUTION_PY = os.path.join(SCRIPTS, 'attribution.py')
FIXTURE = os.path.join(HERE, 'fixtures', 'issue')
DATA = os.path.join(FIXTURE, 'data')
EXPECTED = os.path.join(FIXTURE, 'expected.md')
TITLE = 'Bugs found while testing chromap 949043c'
CLOSING = attribution.CLOSING
OPENING = 'These bugs turned up while we wrote a test suite for chromap (#209).'
# Words that would advertise the tool or its makers in a published document.
ADVERT = re.compile(r'auto-?optimi[sz]|programmable genomics|umass|'
	r'monolith|clean-room|sweep', re.I)


def run(*args, cwd=None, env=None):
	out = subprocess.run([sys.executable, BODY] + list(args), capture_output=True,
		text=True, cwd=cwd, env=env)
	return out.returncode, out.stdout, out.stderr


def check_file(title, path):
	done = subprocess.run([sys.executable, ATTRIBUTION_PY, 'check', '--kind',
		'issue', '--title', title, '--body', path], capture_output=True,
		text=True)
	return done.returncode, done.stderr


def copy_data(base, name):
	target = os.path.join(base, name)
	shutil.copytree(DATA, target)
	return target


def edit(path, old, new):
	with open(path) as handle:
		text = handle.read()

	if old not in text:
		raise RuntimeError('{!r} is not in {}'.format(old, path))

	with open(path, 'w') as handle:
		handle.write(text.replace(old, new, 1))


def read(path):
	with open(path, encoding='utf-8') as handle:
		return handle.read()


def write(path, text):
	with open(path, 'w', encoding='utf-8') as handle:
		handle.write(text)


def tsv(path):
	lines = read(path).rstrip('\n').split('\n')
	header = lines[0].split('\t')
	return {int(row['number']): row for row in (dict(zip(header, line.split(
		'\t'))) for line in lines[1:])}


# The renderer ----------------------------------------------------------------

def rendering(suite, base):
	out = os.path.join(base, 'body.md')
	code, stdout, err = run(DATA, '--title', TITLE, '--out', out)
	expected = read(EXPECTED)
	first = read(out) if os.path.exists(out) else ''
	suite.check('the fixture renders to expected.md byte for byte', code == 0
		and first == expected, err or [pair for pair in zip(first.split('\n'),
		expected.split('\n')) if pair[0] != pair[1]][:3])

	again = os.path.join(base, 'again.md')
	run('render', DATA, '--title', TITLE, '--out', again, cwd=base)
	suite.check('rendering twice, from another directory, gives the same bytes',
		os.path.exists(again) and read(again) == first)

	suite.equal('the title is stored beside the body as given, with no prefix',
		read(os.path.join(base, 'body.title')), TITLE + '\n')
	suite.check('stdout is empty, and stderr says what was written and its '
		'length', stdout == '' and 'wrote ' + out in err and 'characters'
		in err, (stdout, err))
	code, stdout, err = run(DATA, '--out', out)
	suite.check('--title may be left out when one is stored beside --out',
		code == 0 and read(out) == expected and read(os.path.join(base,
		'body.title')) == TITLE + '\n', err)
	code, _, err = run(DATA, '--out', os.path.join(base, 'untitled.md'))
	suite.check('and is refused, writing nothing, when none is', code == 1
		and '--out needs --title' in err and not os.path.exists(os.path.join(
		base, 'untitled.md')), err)

	lines = expected.split('\n')
	suite.equal('the first paragraph is the opening sentence, then the fixed '
		'sentences', lines[0], OPENING + ' ' + ' '.join(attribution.ISSUE_NOTES))
	suite.check('the first paragraph says many bugs are edge cases, and that '
		'Claude wrote the report', 'edge cases that a user may never '
		'experience' in lines[0] and 'written by Claude' in lines[0]
		and 'more direct than necessary' in lines[0] and lines[1] == '',
		lines[0][-300:])
	suite.check('nothing in the body names the tool, its makers or checks it '
		'does not have', not ADVERT.search(expected) and not ADVERT.search(
		read(os.path.join(base, 'body.title'))), ADVERT.findall(expected))
	suite.check('the severity key sits in the first paragraph, before the '
		'sentence that Claude wrote the issue, and not under the table',
		lines[0].index(attribution.SEVERITY_KEY) < lines[0].index(
		attribution.ISSUE_NOTES[2]) and lines[0].count('**high**') == 1
		and 'Severity is our estimate' not in '\n'.join(lines[1:]), lines[0][-500:])
	toy = [{'number': 1, 'short': 'a', 'category': 'crash', 'effect': 'crash',
		'severity': 'low'}]
	suite.check('the table shows every level, and says none for one without '
		'a bug', issue_body.table(toy).split('\n')[2:] == [
		'| **high** |  | none |  |  |', '| **medium** |  | none |  |  |',
		'| **low** | 1 | a | crash | crash |'], issue_body.table(toy))
	misc = [{'number': 1, 'short': 'a', 'category': 'misc', 'effect': 'misc',
		'severity': 'low'}]
	suite.check('misc is the last category, with its own section heading, and '
		'a bug in it shows in the table', issue_body.CATEGORIES[-1] ==
		('misc', 'Miscellaneous') and issue_body.RANK['misc'] == len(
		issue_body.CATEGORIES) - 1 and issue_body.table(misc).split('\n')[
		-1] == '| **low** | 1 | a | misc | misc |', issue_body.table(misc))
	suite.check('the severity table follows it, divided by severity, each '
		'level named on its first row and its bugs in number order',
		lines[2:4] == ['| Severity | # | Bug | Category | Effect |',
		'|---|---:|---|---|---|']
		and [line.split(' | ')[0:2] for line in lines[4:12]] == [
		['| **high**', '1'], ['| ', '4'], ['| **medium**', '2'], ['| ', '5'],
		['| ', '7'], ['| **low**', '3'], ['| ', '6'], ['| ', '8']]
		and lines[12] == '', lines[2:13])
	suite.check('the intro paragraph follows the table and ends with how to read '
		'the report', lines[13].startswith('These are 8 of the 25 bugs')
		and lines[13].endswith('prepared by the setup block after the table.'),
		lines[13][-200:])
	suite.check('the setup is one collapsed block after the intro',
		lines[15] == '<details><summary>Setup used by the reproductions</summary>'
		and expected.count('<summary>Setup used') == 1)

	sections = [line for line in lines if line.startswith('## ')]
	suite.equal('the categories are ## sections in the category order',
		sections, ['## Wrong output', '## Crashes', '## Nondeterminism',
		'## Silent acceptance of bad input', '## Performance',
		'## Documentation mismatches'])

	titles = re.findall(r'(?m)^### (\d+)\. ', expected)
	suite.equal('each bug is a ### section, numbered 1 to 8 in order', titles,
		[str(n) for n in range(1, 9)])

	shaped = re.findall(r'(?m)^### \d+\. [^\n]+\n\n[^\n<#][^\n]*\n\n<details>'
		r'<summary>Reproduction</summary>\n\n', expected)
	suite.check('each bug has one paragraph, then a collapsed reproduction',
		len(shaped) == 8 and expected.count('<summary>Reproduction</summary>') == 8,
		len(shaped))
	suite.check('the related issues close the paragraph', 'should fix it. '
		'Related: #172, where `--chr-order` is suggested for a lexicographic '
		'order.\n' in expected)
	suite.check('the commands are fenced as bash, the actual output as text',
		expected.count('```bash\n') == 10 and 'chr2\'s length are missing.\n\n'
		'```\n19 and 15 lines\n' in expected, expected.count('```bash\n'))

	manual = [n for n, text in re.findall(r'(?ms)^### (\d+)\. (.*?)</details>',
		expected) if 'Not rerun before filing' in text]
	suite.check('the body says which reproductions were checked by hand, and why',
		manual == ['3', '5'] and 'unlike the other reproductions: it needs a '
		'clang++' not in expected and 'unlike the other reproductions: it needs '
		'the chromap source tree' in expected, manual)
	suite.check('the body ends with the closing line',
		expected.endswith('</details>\n\n' + CLOSING + '\n'))

	code, err = check_file(TITLE, out)
	suite.check('attribution.py check passes the body', code == 0, err)

	stripped = os.path.join(base, 'no-notes.md')
	write(stripped, expected.replace(attribution.ISSUE_NOTES[1], '').replace(
		' ' + attribution.ISSUE_NOTES[0], ''))
	code, err = check_file(TITLE, stripped)
	suite.check('attribution.py check refuses a body whose first paragraph '
		'lacks the fixed sentences', code == 1 and 'fixed sentence' in err, err)

	code, _, err = run(DATA, '--title', TITLE, '--out', os.path.join(base,
		'body2.md'), '--bin', 'chromap=' + sys.executable)
	suite.check('--bin naming another binary than verify ran is refused',
		code == 1 and 'not the one --bin names' in err, err)

	code, _, err = run(DATA, '--title', TITLE, '--out', os.path.join(base,
		'body3.md'), '--freeform')
	suite.check('--freeform is gone', code == 2 and 'unrecognized arguments: '
		'--freeform' in err and not os.path.exists(os.path.join(base,
		'body3.md')), err)


# Each refusal: (name, file, old, new, words in stderr).
REFUSALS = [
	('a missing field', 'prose.md', '=== 1.cause\n', '=== 1.context\n',
		'"=== 1.cause" is missing'),
	('an unknown field', 'prose.md', '=== opening\n', '=== remark\nHello.\n\n'
		'=== opening\n', 'no field is called \'remark\''),
	('the old two-field format\'s body field', 'prose.md', '=== opening\n',
		'=== body\n## The parser\n\n=== opening\n', 'no field is called '
		'\'body\''),
	('a field for a bug bugs.tsv does not have', 'prose.md', '=== opening\n',
		'=== 9.what\nIt fails.\n\n=== opening\n', 'is for bug 9'),
	('an unknown category', 'bugs.tsv', '\tcrash\thigh\tcrash; wrong output\t',
		'\tcrashes\thigh\tcrash; wrong output\t', 'unknown category \'crashes\''),
	('an unknown severity', 'bugs.tsv', '\twrong output\thigh\twrong output\t#172',
		'\twrong output\turgent\twrong output\t#172',
		'unknown severity \'urgent\''),
	('blunt wording in the prose', 'prose.md', '=== 1.expected\n',
		'=== 1.expected\nThis is obviously broken. ', 'blunt wording'),
	('a search word in the prose', 'prose.md', '=== intro\n', '=== intro\n'
		'Each bug was timed against the baseline. ', 'baseline'),
	('a local path in the prose', 'prose.md', '=== intro\n', '=== intro\n'
		'The logs are in /tmp/verify. ', 'local path'),
	('bugs numbered out of the category order', 'bugs.tsv',
		'\twrong output\thigh\twrong output\t#172', '\tcrash\thigh\tcrash\t#172',
		'number the bugs in the category order'),
	('an effect that does not start with the category', 'bugs.tsv',
		'performance (output correct)', 'wrong output; performance',
		'must start with the bug\'s own category'),
	('an effect that is not a list of categories', 'bugs.tsv',
		'performance (output correct)', 'performance; slow', '\'slow\', which is '
		'not a category'),
	('related text that names no issue', 'bugs.tsv', '\t#172, where',
		'\tsee the forum, where', 'related must be - or name an issue'),
	('a number used twice', 'bugs.tsv', '2\tbc-error-threshold-2-n',
		'1\tbc-error-threshold-2-n', 'from 1 to 8 once each'),
	('a bug without a verify row', 'verify.tsv', '\n2\tbc-error', '\n#2\tbc-error',
		'bug 2: no verify row'),
	('a bug whose verify failed', 'verify.tsv', '1\tsingle-end-chr-order\tpass',
		'1\tsingle-end-chr-order\tfail', 'bug 1: verify gave fail'),
	('a reproduction changed since verify ran', 'prose.md',
		'printf \'chr2\\nchr1\\n\' > order.txt', 'printf \'chr2\\nchr1\\n\' '
		'>order.txt', 'bug 1: the setup, the reproduction'),
	('an actual output changed since verify ran', 'prose.md', '19 and 15 lines',
		'19 and 14 lines', 'bug 1: the setup, the reproduction'),
	('verify rows from different binaries', 'verify.tsv', '2\tbc-error-threshold'
		'-2-n\tpass\t0\t1.17\t3/3\ta', '2\tbc-error-threshold-2-n\tpass\t0\t1.17'
		'\t3/3\tb', 'come from different binaries'),
	('a manual mark without a reason', 'verify.tsv', '\tit needs 0.5 GB of '
		'downloads and ten runs at `-t 16`, and the counts it shows differ from '
		'run to run\t', '\t-\t', 'bug 5: marked manual without a reason'),
	('an intro that does not name the commit verify ran', 'prose.md',
		'commit 949043c (master)', 'the master branch', 'does not name commit '
		'949043c'),
	('a placeholder', 'prose.md', '=== 1.expected\n', '=== 1.expected\n'
		'<<FILL: the source>> ', 'placeholder'),
	('a top-level heading in prose', 'prose.md', '=== 3.context\n',
		'=== 3.context\n# Bugs\n\n', 'holds a heading (\'# Bugs\')'),
	('a section heading in an output', 'prose.md', 'Expected: 19 lines',
		'## Output\n\nExpected: 19 lines', 'holds a heading (\'## Output\')'),
	('the closing line in prose', 'prose.md', '=== 8.reproduction\n',
		'=== 8.reproduction\n' + CLOSING + '\n', 'holds the closing line'),
	('a fixed sentence in prose', 'prose.md', '=== intro\n', '=== intro\n'
		+ attribution.ISSUE_NOTES[2] + '\n\n', 'holds the fixed sentence'),
	('a code fence in the commands', 'prose.md', '=== 1.reproduction\n',
		'=== 1.reproduction\n```bash\n', 'holds a code fence'),
	('an output without its actual block', 'prose.md', 'Actual: the chr1 reads',
		'Seen: the chr1 reads', 'needs a paragraph starting "Expected"'),
	('a what of two paragraphs', 'prose.md', '=== 1.what\n', '=== 1.what\n'
		'First.\n\n', 'must be one paragraph'),
	('an expected without a closing period', 'prose.md', 'change only the order of '
		'the output.', 'change only the order of the output', '"=== 1.expected" '
		'must end with a period'),
	('an opening on two lines', 'prose.md', '=== opening\nThese bugs turned up',
		'=== opening\nThese bugs\nturned up', 'must be one line'),
	('an opening that starts in lower case', 'prose.md', '=== opening\nThese '
		'bugs', '=== opening\nthese bugs', 'starts with a capital letter'),
	('an opening that is not a whole sentence', 'prose.md', 'chromap (#209).\n',
		'chromap (#209)\n', 'complete sentence that ends with a period'),
]


def refusals(suite, base):
	for number, (name, table, old, new, words) in enumerate(REFUSALS):
		data = copy_data(base, 'refuse-{}'.format(number))
		edit(os.path.join(data, table), old, new)
		out = os.path.join(data, 'body.md')
		code, stdout, err = run(data, '--title', TITLE, '--out', out)
		suite.check('refuses {}, saying why, writing nothing'.format(name),
			code == 1 and words in err and not os.path.exists(out)
			and not os.path.exists(os.path.join(data, 'body.title'))
			and stdout == '' and 'Traceback' not in err, (code, err[-700:]))

	data = copy_data(base, 'refuse-log')
	os.remove(os.path.join(data, 'verify-logs', '05-paired-end-nondeterminism.log'))
	code, _, err = run(data, '--title', TITLE, '--out', os.path.join(data, 'b.md'))
	suite.check('refuses a manual mark whose log is gone', code == 1
		and 'bug 5: marked manual, but its log' in err, err)

	data = copy_data(base, 'refuse-verify')
	os.remove(os.path.join(data, 'verify.tsv'))
	code, _, err = run(data, '--title', TITLE, '--out', os.path.join(data, 'b.md'))
	suite.check('refuses inputs never verified', code == 1
		and 'verify.tsv: missing' in err, err)

	for title, words in (('bugs found while testing', 'capital letter'),
			('Bugs found while testing chromap.', 'period'),
			('Bugs found while testing ' + 'x' * 60, 'the limit is 72'),
			('Bugs found in the baseline', 'baseline')):
		out = os.path.join(base, 'title.md')
		code, _, err = run(DATA, '--title', title, '--out', out)
		suite.check('refuses the title {!r}'.format(title[:40]), code == 1
			and 'title' in err and words in err and not os.path.exists(out), err)

	code, _, err = run(DATA, '--title', TITLE, '--out', os.path.join(base,
		'no-such-dir', 'b.md'))
	suite.check('refuses an --out in a directory that does not exist',
		code == 1 and 'does not exist' in err and 'Traceback' not in err, err)


def splitting(suite, base):
	data = copy_data(base, 'split')
	out = os.path.join(data, 'issue.md')
	comment = os.path.join(data, 'issue.comment-1.md')
	full = len(read(EXPECTED))
	limit = full - 3000

	code, _, err = run(data, '--title', TITLE, '--out', out, '--limit', str(limit))
	body = read(out) if os.path.exists(out) else ''
	extra = read(comment) if os.path.exists(comment) else ''
	moved = re.findall(r'(?m)^### (\d+)\. ', extra)
	sizes = {n: len(block) for n, block in re.findall(r'(?ms)^### (\d+)\. [^\n]*'
		r'\n\n[^\n]*\n\n(<details><summary>Reproduction.*?</details>)', read(
		EXPECTED))}
	longest = sorted(sizes, key=lambda n: (-sizes[n], int(n)))
	note = issue_body.MOVED_NOTE.format('the first comment')
	suite.check('a body over the limit moves the longest reproductions to '
		'<out stem>.comment-1.md', code == 0 and 0 < len(body) <= limit
		and moved and sorted(moved, key=int) == sorted(longest[:len(moved)],
		key=int), (code, err, moved, longest[:3]))
	suite.check('and says so on stderr', 'comment-1.md' in err and 'comment 1'
		in err and 'to 1 comment,' in err, err)
	suite.check('each moved bug keeps its paragraph and points to the comment',
		body.count(note) == len(moved) and all(re.search(
		r'(?m)^### {}\. [^\n]+\n\n[^\n]+\n\n{}'.format(n, re.escape(note)), body)
		for n in moved), moved)
	suite.check('the comment holds the moved sections whole, then the closing '
		'line', extra.startswith('Reproductions of bug') and extra.count(
		'<summary>Reproduction</summary>') == len(moved) and extra.endswith(
		'</details>\n\n' + CLOSING + '\n') and len(extra) <= limit, extra[:200])
	suite.check('one comment is enough here, so there is no second',
		not os.path.exists(os.path.join(data, 'issue.comment-2.md')))

	code, _, err = run(data, '--title', TITLE, '--out', out)
	suite.check('a later render that fits removes the stale comment', code == 0
		and not os.path.exists(comment) and 'removed' in err and read(out)
		== read(EXPECTED), err)

	# Three long contexts make the reproductions together longer than the
	# body with every one of them moved.
	for number in (1, 4, 8):
		edit(os.path.join(data, 'prose.md'), '=== {}.reproduction\n'.format(number),
			'=== {}.context\n'.format(number) + 'It runs on a large input, as the '
			'next paragraph explains. ' * 100 + '\n\n=== {}.reproduction\n'.format(
			number))

	bugs = issue_body.read_bugs(os.path.join(data, 'bugs.tsv'), [])
	fields = issue_body.read_fields(os.path.join(data, 'prose.md'), [])
	rows = issue_body.read_verify(os.path.join(data, 'verify.tsv'), [])
	everything, alone = issue_body.render(bugs, fields, rows, 0)

	# A limit just above the body with every reproduction moved and the
	# largest single comment: the comments cannot share one, so there are
	# several, each under the limit.
	limit = max([len(everything)] + [len(text) for _, text in alone])
	code, _, err = run(data, '--title', TITLE, '--out', out, '--limit', str(limit))
	files = sorted(name for name in os.listdir(data)
		if re.match(r'^issue\.comment-\d+\.md$', name))
	texts = [read(os.path.join(data, name)) for name in files]
	groups = [[int(n) for n in re.findall(r'(?m)^### (\d+)\. ', text)]
		for text in texts]
	flat = [n for group in groups for n in group]
	body = read(out)
	suite.check('reproductions that do not fit one comment go on to the next, '
		'in order', code == 0 and len(texts) >= 2 and flat == sorted(flat)
		and sorted(flat) == list(range(1, len(bugs) + 1)) and files == [
		'issue.comment-{}.md'.format(i) for i in range(1, len(texts) + 1)]
		and all(len(text) <= limit for text in texts) and len(body) <= limit,
		(code, err, files, groups))
	suite.check('every comment is a whole set of sections with its own head '
		'and the closing line', all(text.startswith('Reproductions of ' + (
		issue_body.bugs_named(group))) and text.endswith('</details>\n\n'
		+ CLOSING + '\n') for text, group in zip(texts, groups)), groups)
	suite.check('each bug in the body points to the comment that holds it',
		all(re.search(r'(?m)^### {}\. [^\n]+\n\n[^\n]+\n\n{}$'.format(n,
		re.escape(issue_body.MOVED_NOTE.format(issue_body.comment_name(
		index)))), body) for index, group in enumerate(groups, 1) for n in group),
		groups)
	suite.check('stderr lists each comment file', all('comment-{}.md'.format(i)
		in err for i in range(1, len(texts) + 1)) and '{} comments'.format(
		len(texts)) in err, err)
	suite.check('no comment names the tool or its makers', not any(
		ADVERT.search(text) for text in texts))

	code, _, err = run(data, '--title', TITLE, '--out', out, '--limit', str(
		full - 3000))
	suite.check('a later render with fewer comments removes the extra files',
		code == 0 and len(texts) == 3 and os.path.exists(os.path.join(data,
		'issue.comment-2.md')) and not os.path.exists(os.path.join(data,
		'issue.comment-3.md')) and 'issue.comment-3.md, left' in err,
		(len(texts), err))

	# A longer context makes bug 2's reproduction larger than the body with
	# every reproduction moved: that comment cannot fit, however it is split.
	edit(os.path.join(data, 'prose.md'), '=== 2.reproduction\n', '=== 2.context\n'
		+ 'It runs on a large input, as the next paragraph explains. ' * 1000
		+ '\n\n=== 2.reproduction\n')
	fields = issue_body.read_fields(os.path.join(data, 'prose.md'), [])
	everything, alone = issue_body.render(bugs, fields, rows, 0)
	biggest = max(len(text) for _, text in alone)

	for name, limit, words in (('the body', 1000, 'with every reproduction '
			'moved'), ('one reproduction', len(everything) + 1,
			'the comment holding bug 2 would be')):
		target = os.path.join(base, 'split-{}.md'.format(limit))
		code, _, err = run(data, '--title', TITLE, '--out', target, '--limit',
			str(limit))
		suite.check('refuses when {} cannot fit, writing nothing'.format(name),
			code == 1 and words in err and biggest > len(everything) + 1
			and not os.path.exists(target)
			and not os.path.exists(target[:-3] + '.comment-1.md'), err)


def renumbering(suite, base):
	data = copy_data(base, 'renumber')
	before = {name: read(os.path.join(data, name)) for name in ('bugs.tsv',
		'prose.md', 'verify.tsv')}

	code, out, err = run('renumber', data)
	suite.check('renumber leaves a report that is numbered in category order '
		'alone', code == 0 and 'already' in out and not [name for name in
		os.listdir(data) if name.startswith('before-renumber')]
		and all(read(os.path.join(data, name)) == text
		for name, text in before.items()), (code, out, err))

	# A ninth bug, appended under an unused number, is a wrong output and
	# belongs after bug 3.
	with open(os.path.join(data, 'bugs.tsv'), 'a') as handle:
		handle.write('99\tnew-wrong-output\tA new wrong output\t-\twrong output\t'
			'low\twrong output\t-\n')
	with open(os.path.join(data, 'prose.md'), 'a') as handle:
		handle.write('\n=== 99.what\nIt writes the wrong value.\n\n=== 99.expected\n'
			'It should write the right one.\n\n=== 99.cause\nThe code appears to '
			'do it.\n\n=== 99.reproduction\necho new\n\n=== 99.output\n'
			'Expected: nothing.\n\nActual:\n\n```text\nnew\n```\n')

	edit(os.path.join(data, 'prose.md'), '=== intro\n', '=== intro\nbug 5 needs '
		'more input than the rest. ')
	code, out, err = run('renumber', data, '--dry-run')
	suite.check('--dry-run prints the new numbers and writes nothing',
		code == 0 and '99 -> 4  new-wrong-output' in out and '4 -> 5  sam-fasta'
		in out and 'dry run' in out and '=== 99.what' in read(os.path.join(data,
		'prose.md')) and not [name for name in os.listdir(data)
		if name.startswith('before-renumber')], (out, err))
	suite.check('and names the prose lines that call a bug by a number that '
		'changes', 'bug 5 as bug 6' in err and 'bug 18' not in err, err)

	code, out, err = run('renumber', data)
	bugs = issue_body.read_bugs(os.path.join(data, 'bugs.tsv'), [])
	fields = issue_body.read_fields(os.path.join(data, 'prose.md'), [])
	rows = issue_body.read_verify(os.path.join(data, 'verify.tsv'), [])
	slugs = [bug['slug'] for bug in bugs]
	backups = [name for name in os.listdir(data) if name.startswith(
		'before-renumber')]
	suite.check('the new bug takes its place in category order and the later '
		'ones move down', code == 0 and [bug['number'] for bug in bugs]
		== list(range(1, 10)) and slugs[3] == 'new-wrong-output'
		and slugs[4] == 'sam-fasta-reads' and slugs[8] == 'version-stdout', (
		code, err, slugs))
	suite.check('each bug\'s prose fields move with it', 'what' in ''.join(
		name for name in fields if name.startswith('4.')) and fields['4.what']
		== 'It writes the wrong value.' and 'FASTA' in fields['5.what']
		and '99.what' not in fields, list(fields)[:12])
	suite.check('the prose keeps the bugs\' fields in number order',
		[int(name.split('.')[0]) for name in fields if name[0].isdigit()]
		== sorted(int(name.split('.')[0]) for name in fields if name[0].isdigit()),
		list(fields))
	suite.check('the verify rows and their log files move with the bugs, and '
		'the rows stay valid', sorted(rows) == [1, 2, 3, 5, 6, 7, 8, 9]
		and rows[5]['slug'] == 'sam-fasta-reads' and rows[5]['log']
		== 'verify-logs/05-sam-fasta-reads.log' and os.path.exists(os.path.join(
		data, 'verify-logs', '05-sam-fasta-reads.log')) and not os.path.exists(
		os.path.join(data, 'verify-logs', '04-sam-fasta-reads.log'))
		and all(row['text'] == issue_body.text_hash(fields, number)
		for number, row in rows.items()), (sorted(rows), os.listdir(os.path.join(
		data, 'verify-logs'))))
	suite.check('the originals are kept in a before-renumber folder',
		len(backups) == 1 and read(os.path.join(data, backups[0], 'bugs.tsv'))
		.count('99\t') == 1 and os.path.exists(os.path.join(data, backups[0],
		'verify-logs', '04-sam-fasta-reads.log')), backups)

	target = os.path.join(base, 'renumber.md')
	code, _, err = run(data, '--title', TITLE, '--out', target)
	suite.check('render then refuses only the bug that was never verified',
		code == 1 and 'bug 4: no verify row' in err and err.count('verify row')
		== 1, err)

	# Dropping and keeping.
	data = copy_data(base, 'renumber-drop')
	code, out, err = run('renumber', data, '--drop', '2,6')
	bugs = issue_body.read_bugs(os.path.join(data, 'bugs.tsv'), [])
	rows = issue_body.read_verify(os.path.join(data, 'verify.tsv'), [])
	fields = issue_body.read_fields(os.path.join(data, 'prose.md'), [])
	suite.check('--drop removes bugs, with their prose, rows and logs, and '
		'renumbers the rest', code == 0 and [bug['slug'] for bug in bugs]
		== ['single-end-chr-order', 'feature-matrix', 'sam-fasta-reads',
		'paired-end-nondeterminism', 'single-end-64-threads', 'version-stdout']
		and sorted(rows) == [1, 2, 3, 4, 5, 6] and not any(name.startswith(
		('7.', '8.')) for name in fields) and sorted(os.listdir(os.path.join(
		data, 'verify-logs'))) == ['01-single-end-chr-order.log',
		'02-feature-matrix.log', '03-sam-fasta-reads.log',
		'04-paired-end-nondeterminism.log', '05-single-end-64-threads.log',
		'06-version-stdout.log'], (out, err, sorted(rows)))
	target = os.path.join(base, 'renumber-drop.md')
	code, _, err = run(data, '--title', TITLE, '--out', target)
	suite.check('the six bugs left render without a new verify run',
		code == 0 and len(re.findall(r'(?m)^### \d+\. ', read(target))) == 6, err)

	data = copy_data(base, 'renumber-only')
	code, out, err = run('renumber', data, '--only', '1,3,5')
	bugs = issue_body.read_bugs(os.path.join(data, 'bugs.tsv'), [])
	suite.check('--only keeps just those bugs, numbered 1 to 3', code == 0
		and [bug['slug'] for bug in bugs] == ['single-end-chr-order',
		'feature-matrix', 'paired-end-nondeterminism'], (out, err))
	suite.check('the intro\'s "bug 3" is reported, not rewritten', 'bug 3 as bug 2'
		in err and 'bug 3 needs' in read(os.path.join(data, 'prose.md')), err)

	for words, args in (('not in bugs.tsv', ['--drop', '40']),
			('alternatives', ['--only', '1', '--drop', '2']),
			('no bug would be left', ['--drop', '1,2,3,4,5,6,7,8'])):
		data = copy_data(base, 'renumber-bad')
		code, _, err = run('renumber', data, *args)
		suite.check('refuses {}, changing nothing'.format(' '.join(args)),
			code == 1 and words in err and read(os.path.join(data, 'bugs.tsv'))
			== before['bugs.tsv'], err)
		shutil.rmtree(data)


def templating(suite, base):
	code, stdout, err = run('template', DATA)
	suite.check('template prints every field for every bug, each a placeholder',
		code == 0 and all('=== {}\n<<FILL'.format(name) in stdout for name in
		('opening', 'intro', 'reading', 'setup', '1.what', '8.output', '8.varies')),
		err or stdout[:300])
	opening = stdout.split('=== opening\n', 1)[-1].split('\n', 1)[0]
	suite.check('the opening asks for one complete sentence of the writer\'s '
		'own, and names neither the tool nor its makers', 'one complete '
		'sentence of your own' in opening and 'These bugs turned up while' in
		opening and not ADVERT.search(stdout), opening)

	data = copy_data(base, 'templated')
	write(os.path.join(data, 'prose.md'), stdout)
	code, _, err = run(data, '--title', TITLE, '--out', os.path.join(data, 'b.md'))
	suite.check('a prose file straight from the template is refused',
		code == 1 and 'placeholder' in err, err[-300:])


# verify ----------------------------------------------------------------------

TOY_BUGS = ('number\tslug\ttitle\tshort\tcategory\tseverity\teffect\trelated\n'
	'1\techo-tool\tThe tool echoes\t-\twrong output\thigh\twrong output\t-\n'
	'2\ttimed\tThe tool prints a time\t-\tnondeterminism\tmedium\tnondeterminism\t#4\n'
	'3\tby-hand\tThe tool needs a GPU\t-\tperformance\tlow\tperformance\t-\n')

TOY_PROSE = '''=== opening
These bugs turned up while testing the toy at commit abc123.

=== intro
The toy at commit abc123.

=== setup.context
In an empty directory.

=== setup
echo setup-ran > setup.txt

=== 1.what
The tool echoes.

=== 1.expected
It should not.

=== 1.cause
The code in toy.c.

=== 1.reproduction
tool hello
cat setup.txt
case "$PWD" in "$TOY_WORKDIR"/*) echo cwd-inside;; esac
case "$HOME" in "$TOY_WORKDIR"/*) echo home-inside;; esac
case "$TMPDIR" in "$TOY_WORKDIR"/*) echo tmp-inside;; esac
echo made > made.txt

=== 1.output
Expected: nothing.

Actual:

```
tool says hello
setup-ran
...
cwd-inside
home-inside
tmp-inside
```

=== 2.what
The tool prints a time.

=== 2.expected
It should not.

=== 2.cause
The clock in toy.c.

=== 2.reproduction
echo "took $((RANDOM + 1000)) ms"

=== 2.output
Expected: no time.

Actual:

```
took 12 ms
```

=== 2.varies
took [0-9]+ ms

=== 3.what
The tool needs a GPU.

=== 3.expected
It should not.

=== 3.cause
The kernel in toy.cu.

=== 3.reproduction
tool --gpu

=== 3.output
Expected: a CPU path.

Actual:

```
tool says --gpu
```
'''


def toy(base, name):
	data = os.path.join(base, name)
	os.makedirs(data)
	write(os.path.join(data, 'bugs.tsv'), TOY_BUGS)
	write(os.path.join(data, 'prose.md'), TOY_PROSE)
	return data


def verifying(suite, base):
	tool = os.path.join(base, 'toolbin')
	write(tool, '#!/bin/sh\necho "tool says $*"\n')
	os.chmod(tool, 0o755)
	work = os.path.join(base, 'verify-work')
	os.makedirs(work)
	env = dict(os.environ, TOY_WORKDIR=work)
	data = toy(base, 'toy')
	verify = os.path.join(data, 'verify.tsv')
	log = os.path.join(base, 'gpu.log')
	write(log, 'tool says --gpu\n')
	common = ['--bin', 'tool=' + tool, '--workdir', work, '--commit', 'abc123']

	code, stdout, err = run('verify', data, '--manual', '3', '--bin', 'tool='
		+ tool, env=env)
	suite.check('--manual without --reason and --log is refused, nothing run',
		code == 1 and '--manual needs --reason' in err and '--manual needs --log'
		in err and not os.path.exists(verify), err)

	code, stdout, err = run('verify', data, '--manual', '3', '--reason', 'it '
		'needs a GPU', '--log', log, *common, env=env)
	rows = tsv(verify) if os.path.exists(verify) else {}
	suite.check('--manual records a manual row with the reason and the log',
		code == 0 and rows.get(3, {}).get('status') == 'manual' and rows[3]['note']
		== 'it needs a GPU' and rows[3]['log'] == log, (err, rows))

	code, stdout, err = run('verify', data, *common, env=env)
	rows = tsv(verify)
	suite.check('verify runs every bug not marked manual and passes them',
		code == 0 and rows[1]['status'] == 'pass' and rows[2]['status'] == 'pass'
		and rows[3]['status'] == 'manual', (stdout, err))
	suite.equal('the binary is first on the PATH under its name, and the setup '
		'runs first', rows[1]['matched'], '5/5')
	suite.check('the reproduction runs in a temporary directory, with HOME and '
		'TMPDIR inside it', 'cwd-inside' in read(os.path.join(data, rows[1]['log']))
		and not os.path.exists(os.path.join(data, 'made.txt'))
		and not os.path.exists(os.path.join(base, 'made.txt')))
	suite.equal('the masks hide values that change from run to run, and ... '
		'lines are skipped', rows[2]['matched'], '1/1')
	suite.check('each row carries the exit code, the seconds, the binary\'s '
		'sha256 and the commit', rows[1]['exit'] == '0' and float(rows[1][
		'seconds']) >= 0 and rows[1]['checksum'] == issue_body.sha256(tool)
		and rows[1]['commit'] == 'abc123', rows[1])
	suite.equal('the temporary directories of passing bugs are removed',
		os.listdir(work), [])
	suite.equal('each bug\'s output is kept in DIR/verify-logs',
		sorted(os.listdir(os.path.join(data, 'verify-logs'))), ['01-echo-tool.log',
		'02-timed.log'])

	out = os.path.join(data, 'issue.md')
	code, _, err = run(data, '--title', 'Bugs in the toy', '--out', out, '--bin',
		'tool=' + tool)
	text = read(out) if os.path.exists(out) else ''
	suite.check('the verified toy renders, and the manual bug says so', code == 0
		and 'Not rerun before filing, unlike the other reproductions: it needs a '
		'GPU; the output shown is from an earlier run.' in text and text.count(
		'Not rerun before filing') == 1, err)

	edit(os.path.join(data, 'prose.md'), 'tool hello', 'tool hi')
	code, _, err = run(data, '--title', 'Bugs in the toy', '--out', out + '2')
	suite.check('a reproduction edited after verify is refused', code == 1
		and 'bug 1: the setup, the reproduction' in err, err)

	code, stdout, err = run('verify', data, '--bug', '1', *common, env=env)
	rows = tsv(verify)
	kept = [name for name in os.listdir(work) if name.startswith('verify-01-')]
	suite.check('a reproduction whose output lacks an actual line fails, naming '
		'it, and keeps its directory', code == 1 and rows[1]['status'] == 'fail'
		and rows[1]['note'] == 'missing: tool says hello' and rows[1]['matched']
		== '4/5' and len(kept) == 1 and 'kept' in stdout, (stdout, rows[1]))
	suite.check('verify of one bug leaves the other rows alone',
		rows[2]['status'] == 'pass' and rows[3]['status'] == 'manual', rows)

	code, _, err = run(data, '--title', 'Bugs in the toy', '--out', out + '3')
	suite.check('render refuses a failed bug', code == 1
		and 'bug 1: verify gave fail (missing: tool says hello)' in err, err)

	edit(os.path.join(data, 'prose.md'), 'echo "took', 'sleep 20; echo "took')
	start = time.monotonic()
	code, stdout, err = run('verify', data, '--bug', '2', '--timeout', '1',
		*common, env=env)
	rows = tsv(verify)
	suite.check('a reproduction past --timeout is killed and recorded as a '
		'timeout', code == 1 and rows[2]['status'] == 'timeout'
		and rows[2]['exit'] == '-' and time.monotonic() - start < 15, (stdout,
		rows[2]))

	edit(os.path.join(data, 'prose.md'), 'echo setup-ran > setup.txt', 'exit 3')
	code, stdout, err = run('verify', data, '--bug', '2', *common, env=env)
	rows = tsv(verify)
	suite.check('a failing setup is recorded as setup-failed', code == 1
		and rows[2]['status'] == 'setup-failed' and rows[2]['exit'] == '3',
		rows[2])

	other = os.path.join(base, 'othertool')
	write(other, '#!/bin/sh\necho "tool says $*"\n# another build\n')
	os.chmod(other, 0o755)
	data2 = toy(base, 'toy-mixed')
	run('verify', data2, '--bug', '1', *common, env=env)
	run('verify', data2, '--bug', '2', '--bin', 'tool=' + other, '--workdir',
		work, '--commit', 'abc123', env=env)
	run('verify', data2, '--manual', '3', '--reason', 'it needs a GPU', '--log',
		log, *common, env=env)
	code, _, err = run(data2, '--title', 'Bugs in the toy', '--out', os.path.join(
		data2, 'issue.md'))
	suite.check('render refuses rows verified on different binaries', code == 1
		and 'come from different binaries' in err, err)

	for args, words in ((['--bug', '7'], 'bug 7: not in bugs.tsv'),
			(['--bin', 'tool=' + os.path.join(base, 'nothing')], 'is not an '
			'executable file'), ([], 'verify needs --bin')):
		before = read(verify)
		extra = [] if '--bin' in args else (['--bin', 'tool=' + tool] if args
			else [])
		code, stdout, err = run('verify', data, *(args + extra), '--workdir', work,
			env=env)
		suite.check('verify refuses {}, running nothing'.format(words), code == 1
			and words in err and read(verify) == before and stdout == '', err)

	data3 = toy(base, 'toy-fence')
	edit(os.path.join(data3, 'prose.md'), '=== 1.reproduction\n',
		'=== 1.reproduction\n```bash\n')
	code, _, err = run('verify', data3, '--bug', '1', *common, env=env)
	suite.check('verify refuses commands that are not commands only', code == 1
		and 'holds a code fence' in err and not os.path.exists(os.path.join(data3,
		'verify.tsv')), err)


def staleness(suite, base):
	tool = os.path.join(base, 'stale-tool')
	write(tool, '#!/bin/sh\necho "tool says $*"\n')
	os.chmod(tool, 0o755)
	other = os.path.join(base, 'stale-other')
	write(other, '#!/bin/sh\necho "tool says $*"\n# another build\n')
	os.chmod(other, 0o755)
	work = os.path.join(base, 'stale-work')
	os.makedirs(work)
	env = dict(os.environ, TOY_WORKDIR=work)
	data = toy(base, 'toy-stale')
	verify = os.path.join(data, 'verify.tsv')
	common = ['--bin', 'tool=' + tool, '--workdir', work, '--commit', 'abc123']

	code, out, err = run('verify', data, '--stale', *common, env=env)
	suite.check('--stale on inputs never verified runs every bug', code == 0
		and '3 of 3 bugs need a run' in out and 'never verified' in out
		and sorted(tsv(verify)) == [1, 2, 3], (out, err))

	first = read(verify)
	code, out, err = run('verify', data, '--stale', *common, env=env)
	suite.check('--stale again runs nothing and leaves verify.tsv alone',
		code == 0 and '0 of 3 bugs need a run' in out and read(verify) == first,
		(out, err))

	edit(os.path.join(data, 'prose.md'), 'The tool prints a time.', 'The tool '
		'prints a time, as the section says.')
	code, out, err = run('verify', data, '--stale', *common, env=env)
	suite.check('a prose edit outside the commands and the output needs no '
		'run', code == 0 and '0 of 3' in out and read(verify) == first, out)

	edit(os.path.join(data, 'prose.md'), 'echo made > made.txt', 'echo made > '
		'made.txt\ntrue')
	before = tsv(verify)
	code, out, err = run('verify', data, '--stale', *common, env=env)
	after = tsv(verify)
	suite.check('an edited reproduction is the only bug rerun, and the others '
		'keep their rows', code == 0 and '1 of 3 bugs need a run' in out
		and 'bug 1 echo-tool: the setup, reproduction' in out
		and after[2] == before[2] and after[3] == before[3]
		and after[1]['text'] != before[1]['text'], (out, err))

	code, out, err = run('verify', data, '--stale', '--bin', 'tool=' + tool,
		'--workdir', work, '--commit', 'def456', env=env)
	suite.check('another --commit makes every row stale', code == 0
		and '3 of 3 bugs need a run' in out and 'it ran commit abc123' in out,
		out)

	code, out, err = run('verify', data, '--stale', '--bin', 'tool=' + other,
		'--workdir', work, '--commit', 'def456', env=env)
	suite.check('another binary makes every row stale', code == 0
		and '3 of 3 bugs need a run' in out and 'it ran another binary' in out,
		out)

	log = os.path.join(base, 'stale-gpu.log')
	write(log, 'tool says --gpu\n')
	run('verify', data, '--manual', '3', '--reason', 'it needs a GPU', '--log',
		log, '--bin', 'tool=' + other, '--workdir', work, '--commit', 'def456',
		env=env)
	before = read(verify)
	code, out, err = run('verify', data, '--stale', '--bin', 'tool=' + other,
		'--workdir', work, '--commit', 'def456', env=env)
	suite.check('a manual row that is still current is not rerun', code == 0
		and '0 of 3' in out and read(verify) == before, (out, err))

	edit(os.path.join(data, 'prose.md'), 'tool --gpu', 'tool --gpu --again')
	code, out, err = run('verify', data, '--stale', '--bin', 'tool=' + other,
		'--workdir', work, '--commit', 'def456', env=env)
	suite.check('a manual row whose text changed is reported, not run, and the '
		'other bugs are not run either', code == 1 and 'bug 3 is marked manual '
		'and the setup, reproduction' in err and out == '' and read(verify)
		== before, (out, err))

	for args in (['--bug', '1'], ['--manual', '1', '--reason', 'x', '--log', log]):
		code, out, err = run('verify', data, '--stale', *(common + args), env=env)
		suite.check('--stale does not go with {}'.format(args[0]), code == 1
			and 'does not go with' in err, err)


def reporting(suite, base):
	code, out, err = run('status', DATA)
	suite.check('status lists a line per bug, the counts and the size, and '
		'says ready', code == 0 and len(re.findall(r'(?m)^\d+  ', out)) == 8
		and 'severity: 2 high, 3 medium, 3 low' in out and 'body: {:,} of '
		'62,000 characters; no comments'.format(len(read(EXPECTED))) in out
		and 'ready to render' in out and re.search(r'(?m)^3  low +wrong output '
		r'+feature-matrix +ok +manual$', out), (code, out, err))

	data = copy_data(base, 'status')
	edit(os.path.join(data, 'prose.md'), '=== 4.cause\n', '=== 4.cause\n<<FILL: x>>'
		'\n\n=== 4.zzz\n')
	edit(os.path.join(data, 'prose.md'), 'printf \'chr2\\nchr1\\n\' > order.txt',
		'printf \'chr2\\nchr1\\n\' > order.txt; true')
	code, out, err = run('status', data)
	suite.check('status names a bug with a placeholder or a stale row, lists '
		'every problem render would refuse, and exits 1', code == 1 and re.search(
		r'(?m)^4 .* fill cause +pass$', out) and re.search(r'(?m)^1 .*'
		r' ok +the setup, reproduction', out) and 'render would refuse:' in out
		and 'still holds a <<FILL placeholder' in out and 'bug 1: the setup' in out
		and 'ready to render' not in out, (code, out))

	data = copy_data(base, 'status-filed')
	write(os.path.join(data, 'filed.json'), json.dumps({'repo': 'o/r',
		'url': 'https://github.com/o/r/issues/7', 'number': 7, 'comments': [
		'https://github.com/o/r/issues/7#issuecomment-1'], 'comments_left': 2}))
	code, out, err = run('status', data)
	suite.check('status says how many of the issue\'s comments are posted',
		code == 0 and 'filed: https://github.com/o/r/issues/7 (1 of 3 comments '
		'posted)' in out, out)

	toy_dir = toy(base, 'status-toy')
	code, out, err = run('status', toy_dir)
	suite.check('status of inputs never verified says so for every bug',
		code == 1 and out.count('never verified') >= 3 and 'verify.tsv: missing'
		in out, out)

	code, out, err = run('status', os.path.join(base, 'no-such-dir'))
	suite.check('status of a missing directory exits 1 with the reason',
		code == 1 and 'cannot read' in out, (out, err))


# file and link ---------------------------------------------------------------

# A stand-in for gh: it logs each call, answers `api user` with the login
# `o`, files issue 7, and fails `issue create` or the Nth `issue comment` on
# request.
FAKE_GH = '''#!/bin/sh
echo "$@" >> "$GH_LOG"
case "$1 $2" in
"api user") echo o ;;
"issue create")
	if [ -n "$GH_FAIL_CREATE" ]; then echo "HTTP 502" >&2; exit 1; fi
	echo "https://github.com/o/r/issues/7" ;;
"issue comment")
	n=$(grep -c "issue comment" "$GH_LOG")
	if [ "$GH_FAIL_COMMENT" = "$n" ]; then echo boom >&2; exit 1; fi
	echo "https://github.com/o/r/issues/7#issuecomment-$n" ;;
esac
'''


def filing(suite, base):
	stub = os.path.join(base, 'ghbin')
	os.makedirs(stub)
	write(os.path.join(stub, 'gh'), FAKE_GH)
	os.chmod(os.path.join(stub, 'gh'), 0o755)
	gh_log = os.path.join(base, 'gh.log')

	def setup(name):
		"""A search directory whose ship/issue-bugs/ is rendered to several
		comments, with the other documents' bodies beside it holding the bug
		report's stand-in."""

		work = os.path.join(base, name)
		data = copy_data(os.path.join(work, 'ship'), 'issue-bugs')
		for number in (1, 4, 8):
			edit(os.path.join(data, 'prose.md'), '=== {}.reproduction\n'.format(
				number), '=== {}.context\n'.format(number) + 'It runs on a '
				'large input. ' * 150 + '\n\n=== {}.reproduction\n'.format(number))
		bugs = issue_body.read_bugs(os.path.join(data, 'bugs.tsv'), [])
		fields = issue_body.read_fields(os.path.join(data, 'prose.md'), [])
		rows = issue_body.read_verify(os.path.join(data, 'verify.tsv'), [])
		everything, alone = issue_body.render(bugs, fields, rows, 0)
		limit = max([len(everything)] + [len(text) for _, text in alone])
		code, _, err = run(data, '--title', TITLE, '--out', os.path.join(data,
			'body.md'), '--limit', str(limit))
		assert code == 0, err
		for folder in ('pr-tests', 'pr-optimization'):
			os.makedirs(os.path.join(work, 'ship', folder))
			write(os.path.join(work, 'ship', folder, 'body.md'), 'Fixes '
				'#<bug issue>.\n')
		return work, data, limit

	def gh_calls():
		# The read of the logged-in account is not a call that files or
		# changes anything.
		return [line for line in (read(gh_log).splitlines() if os.path.exists(
			gh_log) else []) if line != 'api user -q .login']

	def others(work):
		return [read(os.path.join(work, 'ship', folder, 'body.md')) for folder
			in ('pr-tests', 'pr-optimization')]

	env = dict(os.environ, PATH=stub + os.pathsep + os.environ['PATH'],
		GH_LOG=gh_log)
	work, data, limit = setup('file-a')
	files = sorted(name for name in os.listdir(data) if re.match(
		r'^body\.comment-\d+\.md$', name))
	args = ['file', data, '--repo', 'o/r', '--limit', str(limit)]
	record_path = os.path.join(data, 'filed.json')

	code, out, err = run(*args, env=env)
	suite.check('file without --post only previews: the repository, the '
		'title, each comment and what the post needs', code == 0
		and 'repository: o/r' in out and 'title:      ' + TITLE + '\n' in out
		and 'comment 1:' in out and 'comment 2:' in out and len(files) >= 2
		and 'post needs:' in out and '--other-owner: no' in out
		and 'preview only: nothing was posted' in out, (out, err))
	suite.check('and posts nothing, records nothing, and changes no document',
		gh_calls() == [] and not os.path.exists(record_path)
		and others(work) == ['Fixes #<bug issue>.\n'] * 2, gh_calls())

	edited = os.path.join(work, 'edited')
	shutil.copytree(data, edited)
	with open(os.path.join(edited, 'body.md'), 'a') as handle:
		handle.write('An extra line.\n')
	code, out, err = run('file', edited, '--repo', 'o/r', '--limit', str(limit),
		'--post', env=env)
	suite.check('a body that is not what the inputs render to is refused, and '
		'--post files nothing', code == 1 and 'not what the inputs render to'
		in err and gh_calls() == [], err)

	code, out, err = run('file', data, '--repo', 'not a repo', '--limit', str(
		limit), env=env)
	suite.check('--repo must be OWNER/REPO', code == 1 and '--repo must be' in err,
		err)

	code, out, err = run(*args, '--post', env=env)
	calls = gh_calls()
	filed = json.loads(read(record_path)) if os.path.exists(record_path) else {}
	suite.check('--post creates the issue, then posts each comment in order',
		code == 0 and calls[0].startswith('issue create --repo o/r --title '
		+ TITLE + ' --body-file') and calls[0].endswith('body.md')
		and [call.split()[:4] for call in calls[1:]] == [['issue', 'comment', '7',
		'--repo']] * len(files) and [call.split()[-1] for call in calls[1:]]
		== [os.path.join(data, name) for name in files], (out, err, calls))
	suite.check('filed.json records the issue, its digest and each comment, '
		'and no comment left', filed.get('number') == 7 and filed['url']
		== 'https://github.com/o/r/issues/7' and filed['repo'] == 'o/r'
		and filed['title'] == TITLE and filed['digest'] == publish.digest(TITLE,
		read(os.path.join(data, 'body.md'))) and filed['date']
		and len(filed['comments']) == len(files) and filed['comments_left'] == 0
		and filed['comments'][0].endswith('#issuecomment-1'), filed)
	suite.check('no other document is changed: ship.py fills the number in '
		'when it renders them again', others(work) == ['Fixes '
		'#<bug issue>.\n'] * 2 and 'fill' not in out, out)

	code, out, err = run(*args, '--post', env=env)
	suite.check('filing again says it is filed and calls nothing', code == 0
		and 'already filed: https://github.com/o/r/issues/7' in out
		and len(gh_calls()) == len(calls), (out, err))
	code, out, err = run('file', data, '--repo', 'x/y', '--limit', str(limit),
		env=env)
	suite.check('filed.json for another repository refuses', code == 1
		and 'not x/y' in err, err)

	target = os.path.join(work, 'ship', 'pr-tests', 'body.md')
	code, out, err = run('link', data, target, env=env)
	suite.check('link fills the number filed.json records into a file given '
		'by hand', code == 0 and read(target) == 'Fixes #7.\n' and '1 place '
		'filled with #7' in out, (out, err))

	# A comment that fails, then the rerun.
	os.remove(gh_log)
	work, data, limit = setup('file-b')
	record_path = os.path.join(data, 'filed.json')
	failing = dict(env, GH_FAIL_COMMENT='2')
	args = ['file', data, '--repo', 'o/r', '--limit', str(limit), '--post']
	code, out, err = run(*args, env=failing)
	filed = json.loads(read(record_path)) if os.path.exists(record_path) else {}
	suite.check('a comment that fails stops the run and records the issue, the '
		'comment posted, and the comments left', code == 1 and 'comment 2' in err
		and len(filed.get('comments', [])) == 1 and filed.get('comments_left')
		== len(files) - 1 and 'running this again posts the rest' in err,
		(out, err, filed))

	code, out, err = run('status', data)
	suite.check('status shows the comments still to post', 'filed: '
		'https://github.com/o/r/issues/7 (1 of {} comments posted)'.format(len(
		files)) in out, out)

	before = len(gh_calls())
	code, out, err = run(*args, env=env)
	calls = gh_calls()[before:]
	filed = json.loads(read(record_path))
	suite.check('running it again posts only the rest, never a second issue',
		code == 0 and not any(call.startswith('issue create') for call in calls)
		and len(calls) == len(files) - 1 and calls[0].endswith('comment-2.md')
		and len(filed['comments']) == len(files) and filed['comments_left'] == 0,
		(out, err, calls, filed))

	# A rerun after the body changed would post comments that do not match the
	# issue.
	os.remove(gh_log)
	work, data, limit = setup('file-c')
	record_path = os.path.join(data, 'filed.json')
	run('file', data, '--repo', 'o/r', '--limit', str(limit), '--post',
		env=failing)
	partial = read(record_path) if os.path.exists(record_path) else ''
	run(data, '--title', TITLE + ' again', '--out', os.path.join(data, 'body.md'),
		'--limit', str(limit))
	before = len(gh_calls())
	code, out, err = run('file', data, '--repo', 'o/r', '--limit', str(limit),
		'--post', env=env)
	suite.check('a rerun refuses when the title or body differs from the one '
		'the issue was created with', partial and json.loads(partial)[
		'comments_left'] > 0 and code == 1 and 'was created with another title '
		'or body' in err and len(gh_calls()) == before and read(record_path)
		== partial, (err, partial[:200]))

	# Another owner, a failed create and an unreadable record.
	work, data, limit = setup('file-d')
	record_path = os.path.join(data, 'filed.json')
	before = len(gh_calls())
	code, out, err = run('file', data, '--repo', 'someone/r', '--limit', str(
		limit), env=env)
	suite.check('the preview says when the post needs --other-owner', code == 0
		and '--other-owner: yes' in out, out)
	code, out, err = run('file', data, '--repo', 'someone/r', '--limit', str(
		limit), '--post', env=env)
	suite.check('--post on another owner\'s repository refuses without '
		'--other-owner', code == 1 and '--other-owner' in err
		and len(gh_calls()) == before and not os.path.exists(record_path), err)

	code, out, err = run('file', data, '--repo', 'o/r', '--limit', str(limit),
		'--post', env=dict(env, GH_FAIL_CREATE='1'))
	suite.check('a failed issue create records nothing', code == 1
		and 'gh issue create failed' in err and not os.path.exists(record_path),
		err)

	write(record_path, 'not json')
	before = len(gh_calls())
	code, out, err = run('file', data, '--repo', 'o/r', '--limit', str(limit),
		'--post', env=env)
	suite.check('an unreadable filed.json refuses, so no second issue is '
		'filed', code == 1 and 'cannot read it' in err and len(gh_calls())
		== before, err)
	os.remove(record_path)

	code, out, err = run('file', data, '--repo', 'someone/r', '--limit', str(
		limit), '--post', '--other-owner', env=env)
	filed = json.loads(read(record_path)) if os.path.exists(record_path) else {}
	suite.check('and files with --other-owner', code == 0
		and filed.get('repo') == 'someone/r' and filed.get('comments_left') == 0,
		(out, err))

	# link, by hand.
	target = os.path.join(base, 'link-target.md')
	write(target, 'Builds on #<tests PR> and #<bug issue>.\n')
	data = copy_data(base, 'link-data')
	code, out, err = run('link', data, target, env=env)
	suite.check('link needs filed.json or --value', code == 1
		and 'give --value' in err, err)
	code, out, err = run('link', data, target, '--placeholder', '#<tests PR>',
		'--value', '#12', env=env)
	suite.check('link --placeholder --value fills the tests PR\'s number',
		code == 0 and read(target) == 'Builds on #12 and #<bug issue>.\n'
		and '1 place filled with #12' in out, (out, err))
	code, out, err = run('link', data, target, '--value', 'seven', env=env)
	suite.check('link refuses a value that is not #N or a URL', code == 1
		and '--value must be' in err, err)

	holding = os.path.join(base, 'holding.md')
	write(holding, read(EXPECTED) + '\nSee #<bug issue>.\n')
	code, err = check_file(TITLE, holding)
	suite.check('attribution.py check refuses a document that still holds a '
		'stand-in', code == 1 and 'still holds #<bug issue>' in err, err)


# dupes -----------------------------------------------------------------------

ISSUES = json.dumps([
	{'number': 172, 'title': 'Sort output by chromosome name with --chr-order',
		'state': 'OPEN', 'url': 'https://github.com/o/r/issues/172',
		'body': 'Is `--chr-order` the right way to get a lexicographic order?'},
	{'number': 300, 'title': 'chromap -v writes to stderr', 'state': 'CLOSED',
		'url': 'https://github.com/o/r/issues/300', 'body': 'The version goes '
		'to stderr but the man page says stdout.'},
	{'number': 400, 'title': 'Add support for CRAM', 'state': 'OPEN',
		'url': 'https://github.com/o/r/issues/400', 'body': 'Please add CRAM '
		'output.'}])


def duplicates(suite, base):
	stub = os.path.join(base, 'ghlist')
	os.makedirs(stub)
	write(os.path.join(stub, 'gh'), '''#!/bin/sh
echo "$@" >> "$GH_LOG"
if [ -n "$GH_FAIL_LIST" ]; then echo "HTTP 401" >&2; exit 1; fi
cat "$GH_ISSUES"
''')
	os.chmod(os.path.join(stub, 'gh'), 0o755)
	listing = os.path.join(base, 'issues.json')
	write(listing, ISSUES)
	log = os.path.join(base, 'list.log')
	env = dict(os.environ, PATH=stub + os.pathsep + os.environ['PATH'],
		GH_LOG=log, GH_ISSUES=listing)
	data = copy_data(base, 'dupes')
	before = read(os.path.join(data, 'bugs.tsv'))

	code, out, err = run('dupes', data, '--issues', listing, env=env)
	rows = [line.split('\t') for line in read(os.path.join(data,
		'dupes.tsv')).rstrip('\n').split('\n')] if code == 0 else [[]]
	suite.check('dupes lists the issues that share a bug\'s options and words, '
		'and none for the others', code == 0 and '3 issues read' in out
		and re.search(r'(?m)^bug 1 single-end-chr-order:\n  #172 \(open, score',
		out) and re.search(r'(?m)^bug 8 version-stdout:\n  #300 \(closed, score',
		out) and out.count(': none') == 6 and '#400' not in out, (out, err))
	suite.check('dupes.tsv has a header and a row per candidate, and bugs.tsv '
		'is left alone', rows[0] == list(issue_body.DUPES_COLUMNS)
		and [row[:3] for row in rows[1:]] == [['1', 'single-end-chr-order',
		'172'], ['8', 'version-stdout', '300']] and rows[1][3] == 'open'
		and '--chr-order' in rows[1][7] and read(os.path.join(data,
		'bugs.tsv')) == before, rows)
	suite.check('--issues FILE asks gh nothing', not os.path.exists(log))

	first = read(os.path.join(data, 'dupes.tsv')) if code == 0 else ''
	code, out2, err = run('dupes', data, '--repo', 'o/r', env=env)
	calls = read(log).splitlines() if os.path.exists(log) else []
	suite.check('without --issues it asks gh once, for every state, with the '
		'bodies, and finds the same candidates', code == 0 and len(calls) == 1
		and calls[0].startswith('issue list --repo o/r --state all --limit 2000 '
		'--json number,title,state,url,body') and read(os.path.join(data,
		'dupes.tsv')) == first, (calls, out2, err))

	write(os.path.join(data, 'filed.json'), json.dumps({'repo': 'o/r',
		'url': 'https://github.com/o/r/issues/172', 'number': 172, 'title': 't',
		'comments': [], 'comments_left': 0}))
	code, out, err = run('dupes', data, '--issues', listing, env=env)
	suite.check('the report\'s own issue is never a candidate', code == 0
		and '#172' not in out, out)
	os.remove(os.path.join(data, 'filed.json'))

	code, out, err = run('dupes', data, '--issues', listing, '--min-score', '40',
		env=env)
	suite.check('--min-score raises the bar', code == 0 and out.count(
		': none') == 8, out)

	for words, args, extra in (('give --repo', ['dupes', data], env),
			('not the JSON', ['dupes', data, '--issues', os.path.join(base,
			'bugs-like.tsv')], env), ('gh issue list failed', ['dupes', data,
			'--repo', 'o/r'], dict(env, GH_FAIL_LIST='1'))):
		write(os.path.join(base, 'bugs-like.tsv'), 'not json')
		code, out, err = run(*args, env=extra)
		suite.check('dupes refuses: {}'.format(words), code == 1 and words in err,
			err)


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--update', action='store_true', help='rewrite '
		'expected.md from the fixture, then run the checks')
	args = parser.parse_args(argv)

	if args.update:
		target = tempfile.mkdtemp(prefix='issue-body-update-')
		out = os.path.join(target, 'issue.md')
		code, _, err = run(DATA, '--title', TITLE, '--out', out)

		if code != 0:
			sys.exit('the fixture does not render:\n' + err)

		shutil.copy(out, EXPECTED)
		shutil.rmtree(target)
		print('rewrote {}'.format(EXPECTED))

	suite = test_runner.Suite()
	base = tempfile.mkdtemp(prefix='issue-body-test-')

	try:
		print('\nthe renderer')
		rendering(suite, base)
		refusals(suite, base)
		splitting(suite, base)
		templating(suite, base)
		renumbering(suite, base)

		print('\nverify')
		verifying(suite, base)
		staleness(suite, base)
		reporting(suite, base)

		print('\nfile, link and dupes')
		filing(suite, base)
		duplicates(suite, base)
	finally:
		shutil.rmtree(base, ignore_errors=True)

	print('\n{} passed, {} failed'.format(len(suite.passed), len(suite.failed)))

	for failure in suite.failed:
		print('  - {}'.format(failure))

	return 1 if suite.failed else 0


if __name__ == '__main__':
	sys.exit(main())
