"""Write input.txt: deterministic text with a skewed word distribution.

	python3 make_input.py [--words 400000] [--vocabulary 3000] [--out input.txt]
"""

import random
import argparse


def main():
	parser = argparse.ArgumentParser(description=__doc__,
		formatter_class=argparse.RawDescriptionHelpFormatter)
	parser.add_argument('--words', type=int, default=400000)
	parser.add_argument('--vocabulary', type=int, default=3000)
	parser.add_argument('--out', default='input.txt')
	args = parser.parse_args()

	rng = random.Random(0)
	letters = 'abcdefghijklmnopqrstuvwxyz'
	vocabulary = sorted({''.join(rng.choice(letters) for _ in range(
		rng.randint(2, 9))) for _ in range(args.vocabulary)})
	weights = [1.0 / (rank + 1) for rank in range(len(vocabulary))]
	words = rng.choices(vocabulary, weights=weights, k=args.words)

	with open(args.out, 'w') as handle:
		for start in range(0, len(words), 12):
			line = words[start:start + 12]
			# Mixed case and punctuation, which the counter has to strip.
			line[0] = line[0].capitalize()
			handle.write(' '.join(line) + rng.choice('.,;!?') + '\n')


if __name__ == '__main__':
	main()
