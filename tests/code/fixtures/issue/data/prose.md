=== opening
These bugs turned up while we wrote a test suite for chromap (#209).

=== intro
These are 8 of the 25 bugs of haowenz/chromap#210, renumbered. All were reproduced on chromap 0.3.3-r521, commit 949043c (master), built with `make` and the Makefile defaults (g++ 13.3, `-std=c++11 -Wall -O3 -fopenmp -msse4.1`) on Ubuntu 24.04, x86-64; bug 3 needs a small C++ harness, as its section says.

=== reading
Each section below says what happens, what should happen and which code we believe is responsible, and its collapsed block holds a reproduction with the expected and actual output; most reproductions run in a directory prepared by the setup block after the table.

=== setup.context
In an empty directory, with `chromap` and `python3` on the PATH. `common.py` makes the same random two-sequence reference in every script, so the random state after `from common import *` is the same each time.

=== setup
cat > common.py <<'EOF'
import random
random.seed(1)
ref = {c: "".join(random.choice("ACGT") for _ in range(n)) for c, n in (("chr1", 50000), ("chr2", 30000))}
rc = lambda s: s[::-1].translate(str.maketrans("ACGT", "TGCA"))
sub = {"A": "C", "C": "G", "G": "T", "T": "A"}
def fq(f, name, seq, qual=None):
	f.write("@%s\n%s\n+\n%s\n" % (name, seq, qual or "I" * len(seq)))
def fa(path, seqs):
	open(path, "w").write("".join(">%s\n%s\n" % c for c in seqs.items()))
EOF
python3 - <<'EOF'
from common import *
fa("ref.fa", ref)
with open("pairs_1.fq", "w") as f1, open("pairs_2.fq", "w") as f2:  # 200 exact 50 bp pairs from 250 bp fragments
	for i in range(200):
		p = random.randrange(0, 50000 - 250)
		fq(f1, "p%d" % i, ref["chr1"][p:p + 50]); fq(f2, "p%d" % i, rc(ref["chr1"][p + 200:p + 250]))
EOF
chromap -i -r ref.fa -o ref.index 2> /dev/null

=== 1.what
With single-end reads and `--chr-order`, each read is written on the reference sequence that takes its original sequence's rank, at its original coordinates, and reads that do not fit that sequence are dropped; paired-end reads with the same order file are reported correctly.

=== 1.expected
`chromap -h` describes the option as a custom chromosome order, so it should change only the order of the output.

=== 1.cause
In `Chromap::MapSingleEndReads`, the two `RerankCandidatesRid` calls (src/chromap.h:416-419) run before the candidates are taken from the cache or generated (lines 422 and 425), so the candidates keep their original ids; moving the calls after that, as `MapPairedEndReads` does (lines 1059-1072), should fix it.

=== 1.reproduction
python3 - <<'EOF'
from common import *
with open("se.fq", "w") as f:  # exact 50 bp reads: chr1 at 1000, 5000, ..., 45000; chr2 at 2000, 6000, ..., 26000
	for c, first in (("chr1", 1000), ("chr2", 2000)):
		for p in range(first, len(ref[c]) - 1000, 4000):
			fq(f, "%s_%d" % (c, p), ref[c][p:p + 50])
EOF
printf 'chr2\nchr1\n' > order.txt
chromap -x ref.index -r ref.fa -1 se.fq -o default.bed 2> /dev/null
chromap -x ref.index -r ref.fa -1 se.fq --chr-order order.txt -o chr_order.bed 2> /dev/null
echo "$(wc -l < default.bed) and $(wc -l < chr_order.bed) lines"; sed -n '1p;9p' chr_order.bed

=== 1.output
Expected: 19 lines in both files, the chr2 lines first in `chr_order.bed`.

Actual: the chr1 reads are on chr2 at their chr1 coordinates and the reverse, and the four chr1 reads beyond chr2's length are missing.

```
19 and 15 lines
chr2	1000	1050	N	60	+	1
chr1	2000	2050	N	60	+	1
```

=== 2.what
With `--bc-error-threshold 2`, two kinds of barcode at Hamming distance 2 from exactly one whitelist barcode are not corrected: an N followed later in the barcode by a substitution (the mirror case, a substitution before the N, is corrected), and two Ns when the whitelist barcode has an A under the earlier N;

=== 2.expected
the README says threshold 2 corrects barcodes up to Hamming distance 2 from a whitelist barcode.

=== 2.cause
In `Chromap::CorrectBarcodeAt` (src/chromap.cc:572), where an N is encoded as A, with one N the second changed position runs only over the bases before the N (lines 605-608 and 645-646), and with two Ns the whitelist barcode is found both as a one-change and as a two-change candidate (lines 610-651), so it looks ambiguous and its probability falls below `--bc-probability-threshold`. Letting the second position run over all positions, and with two Ns skipping one-change candidates or deduplicating candidates by corrected key, would fix both.

=== 2.reproduction
python3 - <<'EOF'
from common import *
dist = lambda a, b: sum(x != y for x, y in zip(a, b))
wl = []
while len(wl) < 12:  # 16 bp barcodes, pairwise at least 5 mismatches apart
	b = "".join(random.choice("ACGT") for _ in range(16))
	if all(dist(b, x) >= 5 for x in wl): wl.append(b)
open("wl.txt", "w").write("".join(b + "\n" for b in wl))
def changed(w, case):  # an N (quality '#') and a substitution after or before it, or two Ns over the first A of w
	b, q = list(w), ["I"] * 16
	if case == "two_n":
		first = w.index("A")
		for p in (first, random.randrange(first + 1, 16)): b[p], q[p] = "N", "#"
	else:
		n = random.randrange(4, 12)
		s = random.randrange(n + 1, 16) if case == "n_then_sub" else random.randrange(0, n)
		b[n], q[n] = "N", "#"
		b[s] = sub[b[s]]
	return "".join(b), "".join(q)
for case in ("n_then_sub", "sub_then_n", "two_n"):
	with open(case + ".fq", "w") as fr, open(case + "_bc.fq", "w") as fb:
		for k, w in enumerate(wl):  # three reads with the exact barcode, then one with the changed barcode at k * 4000 + 3000
			for j in range(4):
				p = 1000 + 4000 * k + 500 * j if j < 3 else 4000 * k + 3000
				bc, bq = (w, "I" * 16) if j < 3 else changed(w, case)
				fq(fr, "%s_%d_%d" % (case, k, j), ref["chr1"][p:p + 50]); fq(fb, "%s_%d_%d" % (case, k, j), bc, bq)
EOF
for case in n_then_sub sub_then_n two_n; do
	chromap -x ref.index -r ref.fa -1 $case.fq -b ${case}_bc.fq --barcode-whitelist wl.txt --bc-error-threshold 2 -o $case.bed 2> /dev/null
	echo "$case: $(awk '$2 % 4000 == 3000' $case.bed | wc -l) of 12 changed barcodes corrected"
done

=== 2.output
A read whose barcode is not corrected is dropped, so the lines at `start % 4000 == 3000` count the corrected barcodes.

Expected: 12 of 12 in each case.

Actual:

```
n_then_sub: 0 of 12 changed barcodes corrected
sub_then_n: 12 of 12 changed barcodes corrected
two_n: 0 of 12 changed barcodes corrected
```

=== 3.what
In `FeatureBarcodeMatrix` (src/feature_barcode_matrix.cc), in cell-by-bin mode a fragment that crosses a bin boundary on any reference sequence but the first is counted in its first bin only, and in peak mode a run of coverage that reaches the last base of a sequence is never saved as a peak.

=== 3.expected
A fragment should be counted in every bin it overlaps, and a run of coverage that reaches the end of a sequence should be saved as a peak like any other.

=== 3.cause
`GetNumOverlappedBins` (lines 236-257) compares the in-sequence end position with a bin index that already includes the bins of earlier sequences, and `CallPeaks` (lines 60-120) saves a peak only when coverage drops inside its loop; comparing with the in-sequence bin index, and saving the open run after the loop, would fix them. The peak options are commented out at 949043c (src/chromap_driver.cc:225-226), so the command line cannot reach this code; it is reported in case the feature is turned back on.

=== 3.context
In the chromap source directory after `make`, with `python3` on the PATH:

=== 3.reproduction
python3 - <<'EOF'
import random
random.seed(1)
with open("fm_ref.fa", "w") as f:
	for c, n in (("cA", 1000), ("cB", 1000), ("cC", 700)):
		f.write(">%s\n%s\n" % (c, "".join(random.choice("ACGT") for _ in range(n))))
EOF
cat > fm.cc <<'EOF'
#include <string>
#include <vector>
#include "feature_barcode_matrix.h"
#include "sequence_batch.h"
#include "utils.h"
using namespace chromap;
// fm REF PREFIX CELL_BY_BIN BIN_SIZE; fragments "rid start length" on stdin, one barcode
int main(int argc, char *argv[]) {
  SequenceBatch reference;
  reference.InitializeLoading(argv[1]);
  reference.LoadAllSequences();
  uint32_t n = reference.GetNumSequences();
  std::string bc = "AAAACCCCGGGGTTTT";
  uint64_t key = GenerateSeedFromSequence(bc.c_str(), bc.size(), 0, bc.size());
  std::vector<std::vector<PairedEndMappingWithBarcode> > mappings(n);
  uint32_t rid, start, length, id = 0;
  while (std::cin >> rid >> start >> length)
    mappings[rid].push_back(PairedEndMappingWithBarcode(id++, key, start, length, 60, 1, 1, 1, 50, 50));
  FeatureBarcodeMatrix matrix(std::string(argv[3]) == "1", std::stoi(argv[4]), 0, 3);
  matrix.OutputFeatureMatrix(n, reference, mappings, argv[2]);
  reference.FinalizeLoading();
  return 0;
}
EOF
g++ -std=c++11 -O2 -fopenmp -msse4.1 -Isrc fm.cc $(ls objs/*.o | grep -v chromap_driver) -o fm -lm -lz
echo "1 200 100" | ./fm fm_ref.fa bins 1 250 2> /dev/null; cat bins_matrix.mtx
printf '0 500 100\n0 500 100\n0 500 100\n2 600 100\n2 600 100\n2 600 100\n' | ./fm fm_ref.fa peaks 0 5000 2> /dev/null
cat peaks_peaks.bed

=== 3.output
Expected: (1) the fragment cB:200-300 overlaps cB's bins 1-250 and 251-500, features 4 and 5 (0-based) after cA's four bins, so two matrix entries; (2) coverage 3 over cA:500-600 and cC:600-700 (cC is 700 bp), so two peaks, cA 501-600 and cC 601-700.

Actual: one matrix entry (feature 4), and no peak on cC.

```
11	1	1
4	0	1
cA	501	600
```

=== 4.what
Reads in FASTA format have no quality string, and two code paths use one anyway: with `--SAM`, chromap aborts on `std::logic_error` ("basic_string: construction from null is not valid"), or, when a FASTA file follows a FASTQ file in the same `-1` list, writes each FASTA read with a FASTQ read's QUAL instead of `*`; and with `--trim-adapters`, which `--preset atac` sets, paired-end FASTA reads whose mates overlap crash chromap with SIGSEGV.

=== 4.expected
The README says chromap works with FASTA and FASTQ input without conversion.

=== 4.cause
`SequenceBatch::LoadOneSequenceAndSaveAt` (src/sequence_batch.cc:49-52, and `LoadAllSequences` at lines 98-101) moves the quality into the batch slot only when the record has one, leaving the slot's `qual` null or stale for `MappingGenerator<SAMMapping>` (src/mapping_generator.cc:56 and 106) and for `SequenceBatch::TrimSequenceAt`, which writes `qual.s[qual.l]` (src/sequence_batch.h:150; ASan: a SEGV write at address 0x28); clearing `qual` for records without one, writing `*` for an empty quality and truncating the quality only when there is one would fix all three.

=== 4.reproduction
python3 - <<'EOF'
from common import *
c1, ad = ref["chr1"], "CTGTCTCTTATACACATCT"  # Nextera adapter
with open("fa.fa", "w") as f, open("fq.fq", "w") as g:  # five FASTA reads, and five other reads as FASTQ with QUAL "5"
	for i in range(5):
		f.write(">fa%d\n%s\n" % (i, c1[1000 + 5000 * i:1050 + 5000 * i])); fq(g, "fq%d" % i, c1[3000 + 5000 * i:3050 + 5000 * i], "5" * 50)
# ten pairs as FASTA and FASTQ; five come from 40 bp fragments, so their mates overlap and read into the adapter
with open("ad_1.fa", "w") as f1, open("ad_2.fa", "w") as f2, open("ad_1.fq", "w") as q1, open("ad_2.fq", "w") as q2:
	for i in range(10):
		p, n = 1000 + 3000 * i, (250 if i < 5 else 40)
		s1, s2 = (c1[p:p + n] + ad)[:50], (rc(c1[p:p + n]) + ad)[:50]
		f1.write(">p%d\n%s\n" % (i, s1)); f2.write(">p%d\n%s\n" % (i, s2)); fq(q1, "p%d" % i, s1); fq(q2, "p%d" % i, s2)
EOF
M="-x ref.index -r ref.fa"
chromap $M -1 fa.fa --SAM -o fa.sam 2> fa.log; echo "FASTA --SAM: exit status $?"; tail -n 1 fa.log
chromap $M -1 fq.fq,fa.fa --SAM -o mixed.sam 2> /dev/null; grep -v '^@' mixed.sam | head -n 3 | cut -f 1,11 | cut -c 1-30
for o in --trim-adapters "--preset atac"; do
	for x in fa fq; do chromap $M -1 ad_1.$x -2 ad_2.$x $o -o ad.bed 2> /dev/null; echo "$x $o: exit status $?"; done
done

=== 4.output
Expected: FASTA reads written to SAM with QUAL `*`, and adapter trimming working on FASTA reads as on the same reads in FASTQ.

Actual: `fa0` and `fa1` carry the FASTQ reads' quality string.

```
FASTA --SAM: exit status 134
  what():  basic_string: construction from null is not valid
fa0	55555555555555555555555555
fq0	55555555555555555555555555
fa1	55555555555555555555555555
fa --trim-adapters: exit status 139
fq --trim-adapters: exit status 0
fa --preset atac: exit status 139
fq --preset atac: exit status 0
```

=== 5.what
Mapping the same paired-end input twice with the same command at `-t 16` does not always give the same result: the `Number of candidates` statistic changed in every one of ten runs of a 9.8 M-pair yeast ChIP-exo library, and on an ENCODE ATAC-seq library (47 M pairs) six runs gave four different BED files, which disagree on whether about 230 fragments lie at chrM:21-88 or at a copy of that sequence on chr17 (a NUMT); single-end runs did not vary.

=== 5.expected
The same command on the same input should give the same output and the same statistics in every run.

=== 5.cause
`Chromap::MapPairedEndReads` updates the minimizer-to-candidates cache from many threads in a `taskloop` (src/chromap.h:1152), and `mm_cache::Update` (src/mmcache.hpp:187-339) leaves an entry that depends on the order in which reads reach it, while `MapSingleEndReads` does the same update serially after a `taskwait` (lines 473-491); with that `taskloop` line removed, ten yeast runs all gave the `-t 1` count and four ATAC runs gave identical BED files and statistics. Updating the cache serially, or per entry in read order, and making the `head_mm` update atomic (src/mmcache.hpp:333-336) should make runs reproducible; this differs from bug 18, which needs a clang++/libomp build and never changes the output.

=== 5.context
With `chromap` and `wget` on the PATH (about 0.5 GB of downloads; each run takes about 15 s at `-t 16`). SRR11467336 is a Reb1 ChIP-exo run of *S. cerevisiae* (GSM4449823, 9,816,007 pairs).

=== 5.reproduction
wget -q https://hgdownload.soe.ucsc.edu/goldenPath/sacCer3/bigZips/sacCer3.fa.gz
wget -q https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR114/036/SRR11467336/SRR11467336_1.fastq.gz
wget -q https://ftp.sra.ebi.ac.uk/vol1/fastq/SRR114/036/SRR11467336/SRR11467336_2.fastq.gz
chromap -i -r sacCer3.fa.gz -o sacCer3.index 2> index.log
for i in $(seq 10); do
	chromap --preset chip -x sacCer3.index -r sacCer3.fa.gz -1 SRR11467336_1.fastq.gz -2 SRR11467336_2.fastq.gz \
		-t 16 -o run$i.bed 2> run$i.log
	echo "run $i: $(grep '^Number of candidates' run$i.log) BED md5 $(md5sum < run$i.bed | cut -c 1-12)"
done

=== 5.output
Expected: the same statistics in every run; both `-t 1` runs gave 25,201,655 candidates.

Actual: ten different counts between 25,201,705 and 25,202,299; the BED file is the same in every run.

```
run 1: Number of candidates: 25202281. BED md5 92d1aa7e05d8
run 2: Number of candidates: 25202151. BED md5 92d1aa7e05d8
...
run 10: Number of candidates: 25202284. BED md5 92d1aa7e05d8
```

The BED file changed on ENCODE ATAC-seq ENCSR260SWI (HCT116) replicate 1 (FASTQs ENCFF668ATD and ENCFF582TRW, 47.2 M pairs; an index of UCSC `hg38.fa.gz`; about 100 s and 22 GB per run):

```bash
for i in 1 2 3 4 5 6; do
	chromap --preset atac -x hg38.index -r hg38.fa -1 ENCFF668ATD.fastq.gz -2 ENCFF582TRW.fastq.gz -t 16 -o run$i.bed 2> run$i.log
done
md5sum run*.bed; diff run1.bed run4.bed
```

Runs 1 and 3 gave one BED file (md5 0a9fef8f73f9, 22,144,004 lines), runs 4 and 6 another (b982f60d6328, 22,144,012 lines), and runs 2 and 5 differ from these in one record each. Every differing record lies at chrM:12-88 or chr17:22,521,421-22,521,488; from `diff run1.bed run4.bed`:

```
> chr17	22521421	22521470	N	60	-	4
> chr17	22521422	22521469	N	60	-	60
...
< chrM	22	69	N	60	-	70
---
> chrM	22	69	N	60	-	3
```

=== 6.what
A gzipped FASTQ file whose compressed stream ends early is mapped as if it were complete: chromap exits 0 after the reads it could decompress, and the last read, cut inside its sequence line, is mapped as a shorter read,

=== 6.expected
although chromap stops with "Didn't reach the end of sequence file, which might be corrupted!" on a truncated plain-text record.

=== 6.cause
zlib reports a truncated stream only through `gzerror` or a `Z_BUF_ERROR` from `gzclose`, and `SequenceBatch` checks neither (src/sequence_batch.cc:17-20, 27-30 and 56-59); checking them when a file ends and exiting with the existing message would fix it, for reference FASTA files too.

=== 6.context
With `gzip` on the PATH as well:

=== 6.reproduction
python3 - <<'EOF'
from common import *
with open("reads.fq", "w") as f:
	for i in range(2000): fq(f, "r%04d" % i, ref["chr1"][100 + 20 * i:150 + 20 * i])
EOF
gzip -c reads.fq > reads.fq.gz; head -c $(( $(stat -c %s reads.fq.gz) / 2 )) reads.fq.gz > cut.fq.gz
chromap -x ref.index -r ref.fa -1 cut.fq.gz -o cut.bed 2> cut.log; echo "exit status $?"
grep '^Number of reads:' cut.log; tail -n 1 cut.bed

=== 6.output
Expected: a non-zero exit status, as `gzip -t cut.fq.gz` gives.

Actual: the last read, `r0992`, is cut after 30 of its 50 bases and mapped as a 30 bp read.

```
exit status 0
Number of reads: 993.
chr1	19940	19970	N	60	+	1
```

=== 7.what
With the default g++/libgomp build, single-end mapping at `-t 64` or more maps each batch of reads on one thread while the other threads wait; the output is correct, but in the reproduction mapping takes 1.32 s at `-t 64` against 0.30 s at `-t 63`.

=== 7.expected
Mapping at `-t 64` should be at least as fast as at `-t 63`.

=== 7.cause
`Chromap::MapSingleEndReads` creates `t * t` tasks per batch (`taskloop num_tasks(num_threads * num_threads)`, src/chromap.h:385-386), and libgomp runs a taskloop undeferred on the encountering thread once the pending tasks plus `num_tasks` exceed 64 per thread (in a probe at 8 threads, 512 tasks ran on 8 threads and 513 on one), while the paired-end path uses `grainsize(5000)` and stays below the limit. Bounding the number of tasks, for example `num_tasks(num_threads * 50)` as the commented-out line 384 mentions, mapped the same reads in 0.26 s at `-t 64` with the same output; a clang++/libomp build does not have the problem.

=== 7.context
The read file is about 80 MB and each run takes about 1.6 GB of memory. `OMP_WAIT_POLICY=passive` lets idle threads sleep, so the CPU column counts work done. The machine does not need 64 cores: our run was restricted to 32 with `taskset`.

=== 7.reproduction
python3 - <<'EOF'
from common import *
block = "".join(">r%d\n%s\n" % (i, ref["chr1"][40 * i:40 * i + 150]) for i in range(1000))
open("t64.fa", "w").write(block * 520)  # 520,000 reads of 150 bp, two batches
EOF
for t in 63 64; do
	OMP_WAIT_POLICY=passive /usr/bin/time -f "-t $t: %e s wall, CPU %P" chromap -x ref.index -r ref.fa -1 t64.fa -t $t -o t$t.bed 2> t$t.log
	tail -n 1 t$t.log; grep 'Mapped all reads' t$t.log | tail -n 1
done
cmp t63.bed t64.bed && echo "outputs identical"

=== 7.output
Expected: `-t 64` at least as fast as `-t 63`.

Actual:

```
-t 63: 0.92 s wall, CPU 399%
Mapped all reads in 0.30s.
-t 64: 2.04 s wall, CPU 100%
Mapped all reads in 1.32s.
outputs identical
```

=== 7.varies
[0-9]+\.[0-9]+ ?s\b
CPU (?:[2-9][0-9]{2}|[0-9]{4,})%

=== 8.what
chromap writes the version to stderr (`std::cerr`, src/chromap_driver.cc:239-240), so `chromap -v > version.txt` captures nothing.

=== 8.expected
chromap.1 documents `-v` as "Print version number to stdout."

=== 8.cause
Either that line should use `std::cout`, or chromap.1 should say stderr.

=== 8.reproduction
echo "stdout: [$(chromap -v 2> /dev/null)] stderr: [$(chromap -v 2>&1 > /dev/null)]"

=== 8.output
Expected: `stdout: [0.3.3-r521] stderr: []`.

Actual:

```
stdout: [] stderr: [0.3.3-r521]
```

