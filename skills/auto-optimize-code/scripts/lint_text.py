#!/usr/bin/env python3
"""One vocabulary check for text a reader outside the search will see.

	python3 lint_text.py body FILE [--labels A0,A1,...]
	python3 lint_text.py title STRING [--labels ...]
	python3 lint_text.py commits REPO REF BASE [--labels ...]
	python3 lint_text.py diff-comments REPO REF BASE [--labels ...]

`body` checks a rendered body, `title` a title, `commits` the messages of
`BASE..REF`, `diff-comments` the comments on the lines `BASE..REF` adds. Exit
1 lists every hit with its line; exit 0 prints `no hits`.

Published text never names the search: its words (winner, winning iteration,
baseline, gate, "the search", "the base" for the original code, a round,
"stock"), the labels of its iterations (A175, when `--labels` or a
`branch.json` beside the text names them), or a local path (`~/`, `/home/`,
`/tmp/`, `$WORK`). Write `main`, or the target's own name, for the original.

Words that are also real code words pass: "binary search", "iteration over
the records" and "the base pair" are not hits. The exceptions are narrow and
fixed here:

* a search word inside an inline code span with no space in it (`--search`,
  `search.c`, `binary_search`) is a file, option or function name; a path or
  a label is a hit wherever it stands, and a fenced block is read as written;
* URLs and link targets are not read;
* a table cell shorter than 60 characters with no sentence in it is a label,
  and is read strictly: there `search`, `iteration` and `base` are hits unless
  `search` follows a qualifier such as binary or linear (`strict`), because a
  label has no sentence around the word to say which sense it has.

`autoopt.py readme` and any script that writes such text call
`problems`; nothing here rewrites text.

Standard library only, Python 3.9 or later.
"""

import os
import re
import sys
import subprocess
import argparse

QUALIFIERS = (r'binary|linear|interpolation|bisect|bisection|string|substring|'
	r'regex|regular expression|text|full-text|tree|hash|depth-first|'
	r'breadth-first|grid|beam|exhaustive|prefix|suffix|pattern|name|path|'
	r'file|index|database|lookup|keyword|fuzzy|sequence|motif|seed|k-mer')

# (rule, pattern, hint) read in prose.
WORDS = (
	('winner', r'\bwinn(?:er|ers|ing)\b', 'say what the change is, not that it '
		'won'),
	('iteration', r'\b(?:winning|kept|promoted|best) iterations?\b|'
		r'\biterations? (?:on|of) the (?:winner|path)\b', 'say "change" or '
		'"commit"'),
	('baseline', r'\bbaselines?\b', 'name the target branch (`main`)'),
	('round', r'\brounds? \d+\b|\b(?:each|every|next|last|final|previous) '
		r'rounds?\b(?! (?:to|of the (?:value|number|result)))|\brounds? of '
		r'(?:the )?(?:search|iterations)\b', 'a round is a unit of the search'),
	('gate', r'\bgat(?:e|es|ed|ing)\b', 'say what is checked'),
	('search', r'\b(?:the|this|our|that|whole|final|last) search\b|'
		r"\bsearch(?:es)?'s\b|\bsearch (?:ended|stopped|found|kept|promoted|ran"
		r'|directory|run)\b', 'the search is the tool\'s own process'),
	('base', r"\bthe base(?:'s)?\b(?! (?:branch|class|classes|image|layer|url|"
		r"address|pair|pairs|64|of|type|case|conversion|quality|qualities|call|"
		r"calls|rate|position|positions|\d))|\bbase (?:build|binary|time|"
		r'median|iteration)\b', 'name the target branch (`main`)'),
	('stock', r'\bstock\b', 'name the target branch or its release'),
	('tool', r'\bauto-optimize\b(?!/)|\bProgrammable Genomics\b', 'a '
		'published document does not name the tool or the lab'),
	('notice', r'(?i)\bgenerated (?:with|by) \[?claude\b|\bwritten (?:by|with) '
		r'(?:claude|an? (?:ai|llm|model))\b', 'a published document says '
		'nothing of how it was written'),
)
# Read in a label: a table cell with no sentence.
STRICT = (
	('search', r'\b((?:' + QUALIFIERS + r')\s+)?search(?:es)?\b', 'a label '
		'names the input or the command, not the search'),
	('iteration', r'\biterations?\b', 'a label names the input or the command'),
	('base', r'\bbase\b', 'a label names the input or the command'),
)
PATHS = (
	('local path', r'(?<![\w/.\-])(?:~/|/home/|/tmp/|/scratch/|/mnt/|/Users/|'
		r'/dev/shm/|/var/tmp/)\S*|(?<![\w/.\-])/tmp\b|\$WORK\b|'
		r'\bautooptimize/', 'a path on this machine means '
		'nothing to a reader'),
)

URL = re.compile(r'\]\([^)]*\)|https?://\S+')
SPAN = re.compile(r'`([^`]*)`')
FENCE = re.compile(r'^\s*(```|~~~)')
LABEL_CELL = 60


class Hit(object):
	def __init__(self, line, rule, word, hint, text):
		self.line, self.rule, self.word, self.hint, self.text = (line, rule,
			word, hint, text)

	def __str__(self):
		return '{}: {}: "{}" ({}) in: {}'.format(self.line, self.rule, self.word,
			self.hint, self.text.strip()[:110])


def blank(match):
	return ' ' * len(match.group(0))


def code_masks(text, inside):
	"""`(text with every identifier-like code span blanked, whether the line
	holds a span)`: a span of one word is a name."""

	def one(match):
		return blank(match) if not re.search(r'\s', match.group(1)) else \
			match.group(0)

	return SPAN.sub(one, text)


def label_regex(labels):
	labels = sorted({label for label in labels or () if label}, key=len,
		reverse=True)

	if not labels:
		return None

	return re.compile(r'(?<![A-Za-z0-9_.\-])(?:' + '|'.join(re.escape(label)
		for label in labels) + r')(?![A-Za-z0-9_])')


def is_label_cell(cell):
	cell = cell.strip()
	return bool(cell) and len(cell) < LABEL_CELL and not re.search(r'[.!?]\s|'
		r'[.!?]$', cell) and not re.fullmatch(r'[-: ]+', cell)


def scan(line, number, words, labels, hits, fenced=False):
	"""Add the hits of one line to `hits`: `words` are read in what is left
	after the names in code spans and the URLs are blanked."""

	plain = URL.sub(blank, line)
	named = plain if fenced else code_masks(plain, False)

	for rule, pattern, hint in words:
		for match in re.finditer(pattern, named, re.I):
			if match.re.groups and match.group(1):
				continue

			hits.append(Hit(number, rule, match.group(0), hint, line))

	for rule, pattern, hint in PATHS:
		for match in re.finditer(pattern, plain):
			hits.append(Hit(number, rule, match.group(0), hint, line))

	if labels:
		for match in labels.finditer(plain):
			hits.append(Hit(number, 'label', match.group(0), 'describe the '
				'change by what it does', line))


def check(text, labels=None, strict=False):
	"""The hits of `text`, a list of `Hit`, in line order. `labels` are the
	iteration labels (a list). `strict` reads every line as a label."""

	found = label_regex(labels)
	hits, fenced = [], False

	for number, line in enumerate(text.splitlines(), 1):
		if FENCE.match(line):
			fenced = not fenced
			continue

		if line.lstrip().startswith('|') and not fenced:
			cells = line.strip().strip('|').split('|')

			for cell in cells:
				scan(cell, number, WORDS + (STRICT if is_label_cell(cell) else
					()), found, hits)

			continue

		scan(line, number, WORDS + (STRICT if strict else ()), found, hits,
			fenced)

	return hits


def problems(text, where, labels=None, strict=False):
	"""`check` as strings for a refusal list: `where:LINE: rule: ...`."""

	return ['{}:{}'.format(where, hit) for hit in check(text, labels, strict)]


def cell_problems(value, where, advice, labels=None):
	"""A label in a table (`value`, one cell), read strictly. `where` says
	which row, `advice` which field of the data to change."""

	return ['{}: {} ({})'.format(where, str(hit).split(': ', 1)[1], advice)
		for hit in check(value, labels, strict=True)]


# Comments of a diff ----------------------------------------------------------

C_LIKE = ('.c', '.h', '.cc', '.cpp', '.cxx', '.hpp', '.hh', '.cu', '.cuh', '.java',
	'.js', '.ts', '.go', '.rs', '.cs', '.swift', '.kt', '.scala', '.cl', '.metal')
HASH_LIKE = ('.py', '.pyx', '.pxd', '.sh', '.bash', '.rb', '.pl', '.pm', '.mk',
	'.yml', '.yaml', '.toml', '.cfg', '.conf', '.cmake', '.r', '.jl', '.nim')
HASH_NAMES = ('Makefile', 'makefile', 'GNUmakefile', 'CMakeLists.txt',
	'Dockerfile')


def after_code(line, marker, shell):
	"""The text after the first `marker` that is outside a string, or None."""

	quote, index = None, 0

	while index < len(line):
		char = line[index]

		if quote:
			if char == '\\':
				index += 1
			elif char == quote:
				quote = None
		elif char in '"\'':
			quote = char
		elif line.startswith(marker, index):
			before = line[index - 1] if index else ' '

			if marker == '#' and before not in ' \t' and index:
				pass
			elif marker == '//' and before == ':':
				pass
			else:
				return line[index + len(marker):]

		index += 1

	return None


def comments_of(path, lines):
	"""`[(number, comment text)]` for the added `lines` (`[(number, text)]`,
	each hunk's run of lines in order) of the file `path`."""

	name = os.path.basename(path)
	ext = os.path.splitext(name)[1].lower()
	found = []

	if ext in C_LIKE:
		block = False

		for number, line in lines:
			text = None
			stripped = line.strip()

			if block:
				text = line
				block = '*/' not in line
			elif re.match(r'^\*(?:\s|/|$)', stripped):
				text = line
			elif '/*' in line:
				text = line[line.index('/*'):]
				block = '*/' not in text
			else:
				text = after_code(line, '//', False)

			if text:
				found.append((number, text))
	elif ext in HASH_LIKE or name in HASH_NAMES:
		python = ext in ('.py', '.pyx', '.pxd')
		opened = None

		for number, line in lines:
			text = None

			if python:
				for quote in ('"""', "'''"):
					count = line.count(quote)

					if opened == quote:
						text = line
						opened = None if count % 2 else opened
					elif opened is None and count:
						text = line[line.index(quote):]
						opened = quote if count % 2 else None

					if text is not None:
						break

			if text is None and opened is None:
				text = after_code(line, '#', not python)

			if text and not text.startswith('!'):
				found.append((number, text))

	return found


def added_lines(repo, ref, base):
	"""`{path: [(number, text)]}` of the lines `BASE..REF` adds."""

	done = subprocess.run(['git', '-C', repo, 'diff', '-U0', '--no-color',
		'--no-renames', base, ref], capture_output=True, text=True,
		errors='replace')

	if done.returncode != 0:
		raise SystemExit('lint_text: git diff {} {} failed: {}'.format(base,
			ref, done.stderr.strip()))

	found, path, number = {}, None, 0

	for line in done.stdout.splitlines():
		if line.startswith('+++ '):
			path = line[6:] if line.startswith('+++ b/') else None
		elif line.startswith('@@'):
			match = re.search(r'\+(\d+)', line)
			number = int(match.group(1)) if match else 0
		elif line.startswith('+') and path:
			found.setdefault(path, []).append((number, line[1:]))
			number += 1

	return found


def diff_problems(repo, ref, base, labels=None):
	"""The hits in the comments the branch adds, as `path:line: ...`."""

	out = []

	for path, lines in sorted(added_lines(repo, ref, base).items()):
		for number, text in comments_of(path, lines):
			for hit in check(text, labels):
				hit.line = number
				out.append('{}:{}'.format(path, hit))

	return out


def commit_problems(repo, ref, base, labels=None):
	"""The hits in the messages of `BASE..REF`, as `<short sha>:line: ...`."""

	done = subprocess.run(['git', '-C', repo, 'log', '--format=%h%x00%B%x01',
		'{}..{}'.format(base, ref)], capture_output=True, text=True,
		errors='replace')

	if done.returncode != 0:
		raise SystemExit('lint_text: git log {}..{} failed: {}'.format(base,
			ref, done.stderr.strip()))

	out = []

	for entry in done.stdout.split('\x01'):
		if '\x00' not in entry:
			continue

		sha, message = entry.strip('\n').split('\x00', 1)
		out += problems(message, 'commit ' + sha.strip(), labels)

	return out


def message_problems(directory, labels=None):
	"""The hits in every `<id>.txt` commit message file of `directory`."""

	out = []

	if os.path.isdir(directory):
		for name in sorted(os.listdir(directory)):
			if name.endswith('.txt'):
				with open(os.path.join(directory, name), encoding='utf-8') as handle:
					out += problems(handle.read(), os.path.join(os.path.basename(
						directory), name), labels)

	return out


def branch_problems(bare, ref, base, messages_dir, labels=None):
	"""The hits in the commit messages written for a branch and in the comments
	its branch adds to `base`: what is refused before anything is pushed or
	built."""

	return message_problems(messages_dir, labels) + diff_problems(bare, ref,
		base, labels)


def branch_labels(directory):
	"""The labels in `directory/branch.json`, or []."""

	import json

	try:
		with open(os.path.join(directory, 'branch.json'), encoding='utf-8') as handle:
			return list(json.load(handle).get('labels') or [])
	except (OSError, ValueError):
		return []


def report(found, what):
	if found:
		sys.stderr.write('lint_text: {} hit(s) in {}:\n'.format(len(found), what)
			+ ''.join('  {}\n'.format(item) for item in found))
		return 1

	print('lint_text: no hits in {}'.format(what))
	return 0


def main(argv=None):
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	commands = parser.add_subparsers(dest='command', required=True)

	for name, arguments in (('body', ['file']), ('title', ['string']),
			('commits', ['repo', 'ref', 'base']), ('diff-comments', ['repo',
			'ref', 'base'])):
		sub = commands.add_parser(name)

		for argument in arguments:
			sub.add_argument(argument)

		sub.add_argument('--labels', default='', help='comma-separated '
			'iteration labels (body: default the labels of branch.json beside '
			'the file)')

	args = parser.parse_args(argv)
	labels = [one for one in args.labels.split(',') if one]

	if args.command == 'body':
		if not labels:
			labels = branch_labels(os.path.dirname(os.path.abspath(args.file)))

		with open(args.file, encoding='utf-8') as handle:
			return report(problems(handle.read(), args.file, labels), args.file)

	if args.command == 'title':
		return report(problems(args.string, 'title', labels), 'the title')

	if args.command == 'commits':
		return report(commit_problems(args.repo, args.ref, args.base, labels),
			'the commit messages')

	return report(diff_problems(args.repo, args.ref, args.base, labels),
		'the added comments')


if __name__ == '__main__':
	sys.exit(main())
