# A first search: a slow word counter

`wordcount.py` counts the words in a text file and prints them, most frequent
first. It is correct and slow: it builds each word one character at a time
and counts each distinct word with a separate pass over the whole list. A run
on the default input takes about 6 seconds on the machine this was written on.

Make the input and the reference output:

```bash
python3 make_input.py                       # input.txt: 400,000 words, the same every time
python3 wordcount.py input.txt > counts.tsv
```

Then, in Claude Code, from this folder:

> Use auto-optimize-code to make `wordcount.py` faster on `input.txt`. Its
> output must stay byte-identical to `counts.tsv`.

The skill's interview asks the rest: where the search lives (the default is
`~/auto-optimize/`, outside this repository), how many runs each iteration
gets, which model runs the iteration subagents, and when to stop. Every
iteration is a subagent session, so set a budget you are comfortable with.
