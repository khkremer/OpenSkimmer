# CW Skimmer and OpenSkimmer on the same IQ recording

This is the side-by-side test from section 8 of the requirements document.

The recording is `cwt_20m_20260715T1902Z.c64`: 10 minutes of the CWT
mini-contest, 48 kHz around 14038 kHz, recorded on 2026-07-15 at 1902Z from a
calibrated KiwiSDR. (The recording isn't distributed with OpenSkimmer.) CW
Skimmer on Windows decoded it from a stereo I/Q WAV in SoftRock mode, and
its Telnet spots were captured to a file. Its WAV playback ignores the
configured LO, so its frequencies are offsets from 14038.0 kHz.

OpenSkimmer replayed the same file twice, with `--decoder classical` and with
`--decoder hmm`, using the databases it runs with live
(`--patterns patt3ch.lst --scp combined.txt`) and default settings otherwise.
The numbers are from 2026-08-04.

| system | spots | unique calls | calls in neither database |
|---|---|---|---|
| CW Skimmer | 209 | 108 | 6 |
| OpenSkimmer classical | 152 | 108 | 6 |
| OpenSkimmer HMM | 157 | 111 | 3 |
| CW Skimmer or HMM | | 146 | |
| any of the three | | 159 | |

CW Skimmer and the HMM share 73 calls, CW Skimmer and the classical decoder
67, the two OpenSkimmer decoders 95, and all three 67.

Compare unique calls, not spots. When this was measured, OpenSkimmer's spot
de-duplication ran on the wall clock during a file replay. The replay ran
about 40 times faster than real time, so the 10 minutes took about 15 s, and
the 600 s re-spot window suppressed nearly every repeat. CW Skimmer's 209
spots came from real-time playback, where repeats accumulate normally. (A
replay now runs on the recording's own clock when it knows the start time,
from `--start-time` or a timestamp in the file name.)

## Findings

On identical IQ, the HMM decoder found the most calls: 111, against 108 for
CW Skimmer and 108 for the classical decoder.

Each side's exclusive calls are systematic. The HMM's 38 exclusives are strong
signals (median 24 dB): pileup callers on run frequencies, where CW Skimmer
locks onto the runner. CW Skimmer's 35 exclusives are weak (median 8 dB) and
sit at the noise-floor edge of its Bayesian decoder. The HMM has closed much
of that weak-signal gap but not all of it.

Those strong HMM exclusives need a caveat. They are mostly search-and-pounce
stations answering someone else's CQ, and CW Skimmer leaves them out on
purpose, because a skimmer is meant to report who is calling CQ. A full day of
OpenSkimmer's spots from that period, checked against the RBN archive with
`tools/spot_confirm.py`, showed the cost. 44.8 % of its spots were confirmed by
no other skimmer, against a median of 1.6 % across 175 skimmers, and 97.5 % of
those unconfirmed spots were within 0.5 kHz of a different station that other
skimmers had spotted running there. So part of the HMM's margin here comes
from counting stations the RBN doesn't want. OpenSkimmer has since stopped
reporting callers as CQ, and a re-run under the current rules would measure
the decoder more fairly.

Calls in neither MASTER.SCP nor combined.txt give a rough count of busts: 3
for the HMM (`KC3MX`, `KU9S`, `WB2O`), 6 for the classical decoder and 6 for
CW Skimmer. All three systems spotted `KU9S`, so it is more likely a real call
missing from the databases than a bust, which leaves the HMM at about 2.

The HMM doesn't find everything the classical decoder finds: it gains 16
calls and loses 13. Several of the losses are the classical decoder's own
busts (`E4KYN`, `N5M`, `TI5IN`), which is why the bust count drops while the
unique-call count rises.

On calls both found, the frequencies agree to a median of 100 Hz, which is the
two programs' reporting resolution.

## Limits of this test

It is one 10-minute contest recording on one band, with CW Skimmer at its
default validation settings, and contest conditions favor validation from a
callsign database. A recording of a quiet band with weak signals would be the
harder test, because that is where CW Skimmer's remaining exclusives are.

An earlier version of the HMM decoder did much worse on this recording (79
unique calls). It measured marks about 25 % too wide on strong carriers and
ran words together (`TUK1GU`). Modelling the carrier that leaks into key-up
frames fixed that, and the next run found 113. Later changes to callsign
extraction, which reject fused boilerplate such as `TU5NN` and handle
portable calls, trade a few marginal calls for fewer busts and account for
the drop from 113 to 111.

## Reproducing it

With your own recording:

```
openskimmer --source file --file recordings/cwt_20m_20260715T1902Z.c64 \
    --sample-rate 48000 --freq 14038000 --decoder hmm \
    --patterns patt3ch.lst --scp combined.txt \
    --my-call N0CALL --web-port 0 --telnet-port 0
```
