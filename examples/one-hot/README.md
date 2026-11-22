# A first search: a slow one-hot encoder

`onehot.py` holds `one_hot_encode(sequence, alphabet='ACGT', dtype=numpy.int8)`,
which returns a DNA sequence's one-hot encoding as a `(4, len(sequence))`
array. A character outside the alphabet, such as `N` or a lowercase base,
gives a column of zeros. It is correct and slow: two nested Python `for`
loops test each position against each letter. It needs numpy.

```bash
python3 onehot.py      # prints the encoding of ACGTNacgt
```

The README's walkthrough runs a search on this file. To run one yourself, open
Claude Code in this folder and ask:

> Use auto-optimize-code to make one_hot_encode in onehot.py faster on a 10
> Mbp DNA sequence. Its output must stay bitwise identical.

The skill's interview asks the rest: where the search lives (outside this
repository), how many runs each iteration gets, whether numba may be added,
which model runs the iteration subagents, and the budget. Every iteration is
a subagent session, so pick a budget you are comfortable with.
