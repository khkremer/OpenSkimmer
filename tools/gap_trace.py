#!/usr/bin/env python3
"""Show every gap the decoder measured, and where it broke words.

Feeds extract_envelope.py cases through the real decoder (frames as the
normalised envelope against a noise of 1.0, exactly as the harness does)
and prints the decoded text with each inter-character gap written in dits
between the characters. A "/" after a gap means the decoder broke the word
there; "thr" is the word-break threshold in force at that moment.

    gap_trace.py cases.json [--match NZ1J] [--decoder hmm|classical]

Nominal spacing is 3 dits between characters and 7 between words, and the
default threshold is 6.0, so a sender whose word gaps run 4-5 dits comes out
as one long word unless the threshold adapts (CWDecoder._end_gap).
"""
import argparse
import gzip
import json
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openskimmer.dsp.decoder import CWDecoder  # noqa: E402
from openskimmer.dsp.hmm_decoder import HMMDecoder  # noqa: E402


def trace(base_cls, frames, frame_dt, word_thr=None):
    out = []          # pieces of the annotated text
    gaps = []         # (gap_dits, broke_word, thr)
    state = {"broke": False}

    class Tracing(base_cls):
        def _end_gap(self, dur_ms):
            if self.dit_ms and dur_ms / self.dit_ms >= 1.6:
                g = dur_ms / self.dit_ms
                gaps.append((g, state["broke"], self._word_thr_dits))
                out.append(f" {g:.1f}{'/' if state['broke'] else ''} ")
            state["broke"] = False
            super()._end_gap(dur_ms)
            if word_thr is not None:
                self._word_thr_dits = word_thr

    def on_char(c):
        out.append(c)

    def on_break():
        state["broke"] = True

    dec = Tracing(frame_dt, on_char=on_char, on_word_break=on_break)
    if word_thr is not None:
        dec._word_thr_dits = word_thr
    for v in frames:
        dec.process(float(v), 1.0)
    for _ in range(120):
        dec.process(1.0, 1.0)
    return "".join(out), gaps, dec


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases")
    ap.add_argument("--match", default="")
    ap.add_argument("--decoder", choices=("hmm", "classical"), default="hmm")
    ap.add_argument("--hist", action="store_true", help="histogram of gaps (dits) per case")
    args = ap.parse_args()
    opener = gzip.open if args.cases.endswith(".gz") else open
    cases = json.load(opener(args.cases, "rt"))
    base_cls = HMMDecoder if args.decoder == "hmm" else CWDecoder
    for c in cases:
        if args.match and args.match not in " ".join(c.get("words", [])):
            continue
        text, gaps, dec = trace(base_cls, c["frames"], c["frame_dt"])
        print(f"\n=== {c['name']}  {len(c['frames']) * c['frame_dt']:.0f} s  "
              f"dit {dec.dit_ms or 0:.0f} ms  final thr {dec._word_thr_dits:.1f}")
        print("    words:", " ".join(c.get("words", []))[:200])
        print("    trace:", text[:1200])
        if args.hist and gaps:
            h = Counter(min(int(g * 2) / 2, 12) for g, _, _ in gaps)
            print("    gaps :", "  ".join(f"{k:g}:{h[k]}" for k in sorted(h)))


if __name__ == "__main__":
    main()
