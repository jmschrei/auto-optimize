"""Count the k-mers of a DNA sequence: a correct function that holds far more
memory than it needs, for a short search on peak memory.

	python3 kmers.py      # counts the 3-mers of a short sequence
"""

from collections import Counter


def count_kmers(sequence, k):
	"""Return a dict mapping each k-mer of `sequence` to its number of
	occurrences.

	Every window of length k is counted, overlapping ones included. A window
	holding any character other than A, C, G or T (an N, a lowercase base) is
	skipped. The dict's order carries no meaning.
	"""

	kmers = [sequence[i:i + k] for i in range(len(sequence) - k + 1)]
	kmers = [kmer for kmer in kmers if set(kmer) <= set('ACGT')]
	return dict(Counter(kmers))


if __name__ == '__main__':
	print(count_kmers('ACGTACGTNACGa', 3))
