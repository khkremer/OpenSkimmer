#!/usr/bin/env python3
"""Re-run callsign extraction and spotting on a --log-channels word log.

Decoding a 70-minute 192 kHz recording takes many minutes and gigabytes of
memory, but the callsign rules see only the decoded words. A word log from
one decode (main.py --log-channels, on a --start-time replay so the times
are real UTC) replays through CallsignExtractor and Spotter in seconds, so
rule changes can be compared on identical decoder output. Each channel id
gets its own extractor, as in the live pipeline. Spots print as the telnet
"DX de" lines, stamped with the word's log time, ready for
tools/rbn_consensus.py replay.

    replay_words.py words.log --scp MASTER.SCP --scp-weak combined.txt \
        --patterns patt3ch.lst > spots.txt

The log keeps whole seconds and only words under 24 characters, so this
is close to the live run but not identical; compare variants with each
other, not with a live decode's spot count.
"""
import argparse
import calendar
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from openskimmer.spotting.callsign import CallsignExtractor, load_patterns, load_scp  # noqa: E402
from openskimmer.spotting.spotter import Spot, Spotter  # noqa: E402
from openskimmer.spotting.telnet import format_spot  # noqa: E402


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("--scp")
    ap.add_argument("--scp-weak")
    ap.add_argument("--patterns")
    ap.add_argument("--my-call", default="N0CALL")
    ap.add_argument("--min-snr", type=int, default=3)
    ap.add_argument("--runner-guard", action="store_true",
                    help="experimental Spotter rule: CQ near a different recent runner -> DE")
    ap.add_argument("--runner-tol", type=float, default=500.0, help="Hz")
    ap.add_argument("--runner-mem", type=float, default=900.0, help="s")
    args = ap.parse_args()

    known = load_scp(args.scp) if args.scp else None
    weak = load_scp(args.scp_weak) if args.scp_weak else None
    patterns = load_patterns(args.patterns) if args.patterns else None

    now = [0.0]
    out = sys.stdout

    def on_spot(spot):
        out.write(format_spot(spot, args.my_call) + "\n")

    kw = {"cq_runner_guard": True} if args.runner_guard else {}
    spotter = Spotter(on_spot, my_call=args.my_call, clock=lambda: now[0], min_snr_db=args.min_snr,
                      runner_tol_hz=args.runner_tol, runner_memory_s=args.runner_mem, **kw)
    chans = {}

    for line in open(args.log, errors="replace"):
        p = line.split()
        # "YYYY-MM-DD HH:MM:SSZ  kHz  chan  wpm  snr  WORD"
        if len(p) < 7:
            continue
        try:
            t = calendar.timegm(time.strptime(p[0] + " " + p[1], "%Y-%m-%d %H:%M:%SZ"))
            khz, chan, wpm, snr = float(p[2]), int(p[3]), int(p[4]), int(p[5])
        except ValueError:
            continue
        word = p[6]
        now[0] = t
        st = chans.get(chan)
        if st is None:
            st = chans[chan] = {"f": khz, "wpm": wpm, "snr": snr}

            def on_call(call, activity, st=st):
                s = Spot(freq_hz=st["f"] * 1000.0, call=call, snr_db=st["snr"], wpm=st["wpm"],
                         activity=activity, time_utc=now[0])
                spotter.submit(s)

            st["x"] = CallsignExtractor(on_call, known=known, patterns=patterns, known_weak=weak)
        st["f"], st["wpm"], st["snr"] = khz, wpm, snr
        x = st["x"]
        for ch in word:
            x.add_char(ch)
        x.add_word_break()


if __name__ == "__main__":
    main()
