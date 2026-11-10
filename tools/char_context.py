#!/usr/bin/env python3
"""Show what the decoder sent around a call, with the gap before each
character, from a --log-chars log.

    char_context.py chars.log CALL [KHZ] [--n 6] [--before 30]

For each copy of CALL (as the decoder broke it, a whole word) on channels
within 0.3 kHz of KHZ (any frequency if left out), prints the characters
before and after it on that channel. Each character carries the gap before
it in dits; a decoder word break shows as " | ". Gaps under 2 dits (inside
a run of characters the decoder kept together) print without a number, so
what stands out is where the sender's long gaps really were:

    19:24:03 ch1585 14047.50  C Q·4.1 W·3.0 4 C·3.2 M G | 7.4 C·3.1 W T

reads "CQ", a 4.1-dit gap, "W4CMG" with one 3.2-dit gap inside, a break,
then "CWT" after 7.4 dits.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from replay_chars import parse  # noqa: E402


def render(items):
    out = []
    for ch, g in items:
        if ch == "|":
            out.append("|")
        elif g is None or g < 2.0:
            out.append(ch)
        else:
            out.append("%.1f·%s" % (g, ch))
    return " ".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log")
    ap.add_argument("call")
    ap.add_argument("khz", nargs="?", type=float)
    ap.add_argument("--n", type=int, default=6, help="copies to show")
    ap.add_argument("--before", type=int, default=30, help="characters before the call")
    ap.add_argument("--after", type=int, default=12, help="characters after the call")
    args = ap.parse_args()

    chans = {}
    for line in open(args.log, errors="replace"):
        r = parse(line)
        if r is None:
            continue
        t, khz, chan, wpm, snr, ch, gap, dit, thr = r
        if args.khz is not None and abs(khz - args.khz) > 0.3:
            continue
        g = gap / dit if gap is not None and dit else None
        chans.setdefault(chan, []).append((t, khz, ch, g))

    hits = []
    for chan, items in chans.items():
        word, start = "", 0
        for i, (t, khz, ch, g) in enumerate(items):
            if ch == "|":
                if word == args.call:
                    hits.append((items[start][0], chan, khz, start, i))
                word = ""
            else:
                if not word:
                    start = i
                word += ch
    import time
    for t, chan, khz, a, b in sorted(hits)[:args.n]:
        items = chans[chan]
        lo, hi = max(0, a - args.before), min(len(items), b + args.after)
        text = render([(c, g) for _, _, c, g in items[lo:a]]) + "  [" + \
            render([(c, g) for _, _, c, g in items[a:b]]) + "]  " + \
            render([(c, g) for _, _, c, g in items[b:hi]])
        print(time.strftime("%H:%M:%S", time.gmtime(t)), "ch%d" % chan, "%.2f" % khz, " ", text)
    print("  (%d copies)" % len(hits))


if __name__ == "__main__":
    main()
