"""Count the words in a text file and print them, most frequent first.

	python3 wordcount.py input.txt > counts.tsv

A word is a run of letters, lowercased. Each output line is `word<TAB>count`,
ordered by count (highest first), then by word. This implementation is
correct and deliberately slow.
"""

import sys


def words_of(text):
	out, current = [], ''

	for character in text:
		if character.isalpha():
			current = current + character.lower()
		elif current:
			out.append(current)
			current = ''

	if current:
		out.append(current)

	return out


def count(words):
	unique = []

	for word in words:
		if word not in unique:
			unique.append(word)

	return [(word, words.count(word)) for word in unique]


def main(path):
	with open(path) as handle:
		counts = count(words_of(handle.read()))

	counts.sort(key=lambda pair: (-pair[1], pair[0]))

	for word, n in counts:
		sys.stdout.write('{}\t{}\n'.format(word, n))


if __name__ == '__main__':
	main(sys.argv[1])
