"""Smooth a coverage track with a centered moving average: a correct,
deliberately slow function for a short search under a numeric tolerance.

	python3 smooth.py      # smooths a short track and prints it
"""

import numpy


def smooth(coverage, window):
	"""Return a float64 array as long as `coverage` whose entry i is the mean
	of coverage[i - window // 2 : i + window // 2 + 1], the window clipped at
	both ends of the track.

	`coverage` is a one-dimensional float64 array and `window` a positive odd
	integer.
	"""

	half = window // 2
	n = len(coverage)
	out = numpy.empty(n, dtype=numpy.float64)

	for i in range(n):
		start, end = max(0, i - half), min(n, i + half + 1)
		out[i] = coverage[start:end].mean()

	return out


if __name__ == '__main__':
	print(smooth(numpy.array([0, 0, 3, 9, 3, 0, 0], dtype=numpy.float64), 3))
