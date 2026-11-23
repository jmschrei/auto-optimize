"""Per-record statistics of a FASTA file: a correct, deliberately slow
command-line tool for a short search.

	python3 fasta_stats.py example.fa > stats.tsv

Each output line is tab-separated and holds one record's name (the header's
first word), its length, its GC fraction over the A, C, G and T bases of
either case (six decimals; 0.000000 when it has none), its number of N bases
of either case, and its number of soft-masked (lowercase) characters.
"""

import sys


def read_fasta(path):
	"""Return the records of a FASTA file as (name, sequence) pairs."""

	records = []
	name, sequence = None, ''

	with open(path) as handle:
		for line in handle:
			line = line.rstrip('\n')

			if line.startswith('>'):
				if name is not None:
					records.append((name, sequence))

				name, sequence = line[1:].split()[0], ''
			else:
				sequence += line

	if name is not None:
		records.append((name, sequence))

	return records


def record_stats(sequence):
	"""Return (length, GC fraction, N count, soft-masked count)."""

	gc, at, n, masked = 0, 0, 0, 0

	for character in sequence:
		if character.islower():
			masked += 1

		base = character.upper()

		if base == 'G' or base == 'C':
			gc += 1
		elif base == 'A' or base == 'T':
			at += 1
		elif base == 'N':
			n += 1

	fraction = gc / (gc + at) if gc + at else 0.0
	return len(sequence), fraction, n, masked


def main():
	for name, sequence in read_fasta(sys.argv[1]):
		length, fraction, n, masked = record_stats(sequence)
		print('{}\t{}\t{:.6f}\t{}\t{}'.format(name, length, fraction, n,
			masked))


if __name__ == '__main__':
	main()
