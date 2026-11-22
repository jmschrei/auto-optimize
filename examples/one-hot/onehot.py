"""One-hot encode a DNA sequence: the correct, deliberately slow function the
README's walkthrough optimizes.

	python3 onehot.py      # encodes a short sequence and prints the array
"""

import numpy


def one_hot_encode(sequence, alphabet='ACGT', dtype=numpy.int8):
	"""Return the one-hot encoding of `sequence`, shaped
	(len(alphabet), len(sequence)).

	Entry [j, i] is 1 when sequence[i] == alphabet[j] and 0 otherwise, so a
	character outside the alphabet, such as N or a lowercase base, gives a
	column of zeros. Matching is case-sensitive.
	"""

	ohe = numpy.zeros((len(alphabet), len(sequence)), dtype=dtype)

	for i, character in enumerate(sequence):
		for j, letter in enumerate(alphabet):
			if character == letter:
				ohe[j, i] = 1

	return ohe


if __name__ == '__main__':
	print(one_hot_encode('ACGTNacgt'))
