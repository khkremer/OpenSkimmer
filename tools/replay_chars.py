#!/usr/bin/env python3
"""Re-run callsign extraction and spotting on a --log-chars character log.

The character log (main.py --log-chars, on a --start-time replay) holds every
decoded character with the gap before it, and every word break the decoder
made. Unlike a --log-channels word log it is complete -- no 24-character
word cap, times to the millisecond -- so this replay reproduces the live
run's spots exactly, and it is what a word segmenter that re-decides the
breaks from the gaps is measured on.

    replay_chars.py chars.log --scp MASTER.SCP --scp-weak combined.txt \
        --patterns patt3ch.lst > spots.txt

Spots print as the telnet "DX de" lines, stamped with the log time.
"""
import argparse
import calendar
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from openskimmer.spotting.callsign import CallsignExtractor, load_patterns, load_scp  # noqa: E402
from openskimmer.spotting.segmenter import SEGMENTERS  # noqa: E402
from openskimmer.spotting.spotter import Spot, Spotter  # noqa: E402
from openskimmer.spotting.telnet import format_spot  # noqa: E402


def parse(line):
    """(t, khz, chan, wpm, snr, char, gap_ms, dit_ms, thr) or None.
    gap_ms, dit_ms and thr are None on a word break."""
    p = line.split()
    # "YYYY-MM-DD HH:MM:SS.mmmZ  kHz  chan  wpm  snr  CHAR  gap  dit  thr"
    if len(p) != 10:
        return None
    try:
        stamp, ms = (p[0] + " " + p[1]).rstrip("Z").split(".")
        t = calendar.timegm(time.strptime(stamp, "%Y-%m-%d %H:%M:%S")) + int(ms) / 1000.0
        khz, chan, wpm, snr = float(p[2]), int(p[3]), int(p[4]), int(p[5])
        gap = (None, None, None) if p[6] == "|" else (float(p[7]), float(p[8]), float(p[9]))
    except ValueError:
        return None
    return (t, khz, chan, wpm, snr, p[6]) + gap


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--scp")
    ap.add_argument("--scp-weak")
    ap.add_argument("--patterns")
    ap.add_argument("--my-call", default="N0CALL")
    ap.add_argument("--min-snr", type=int, default=3)
    ap.add_argument("--segmenter", choices=sorted(SEGMENTERS), default="timing",
                    help="how words are cut before the extractor (default: the decoder's own breaks)")
    ap.add_argument("--seg-opt", action="append", default=[], metavar="NAME=VALUE",
                    help="segmenter parameter, e.g. ratio=1.2 (repeatable)")
    args = ap.parse_args()
    seg_kw = {k: float(v) for k, v in (o.split("=", 1) for o in args.seg_opt)}

    known = load_scp(args.scp) if args.scp else None
    weak = load_scp(args.scp_weak) if args.scp_weak else None
    patterns = load_patterns(args.patterns) if args.patterns else None

    now = [0.0]
    out = sys.stdout

    def on_spot(spot):
        out.write(format_spot(spot, args.my_call) + "\n")

    spotter = Spotter(on_spot, my_call=args.my_call, clock=lambda: now[0], min_snr_db=args.min_snr)
    chans = {}

    for line in open(args.log, errors="replace"):
        r = parse(line)
        if r is None:
            continue
        t, khz, chan, wpm, snr, ch = r[:6]
        now[0] = t
        st = chans.get(chan)
        if st is None:
            st = chans[chan] = {}

            def on_call(call, activity, st=st):
                s = Spot(freq_hz=st["f"] * 1000.0, call=call, snr_db=st["snr"], wpm=st["wpm"],
                         activity=activity, time_utc=now[0])
                spotter.submit(s)

            st["x"] = SEGMENTERS[args.segmenter](
                CallsignExtractor(on_call, known=known, patterns=patterns, known_weak=weak), **seg_kw)
        st["f"], st["wpm"], st["snr"] = khz, wpm, snr
        x = st["x"]
        if ch == "|":
            x.add_word_break()
        else:
            x.add_gap(*r[6:9])
            x.add_char(ch)


if __name__ == "__main__":
    main()
