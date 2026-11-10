#!/usr/bin/env python3
"""Show what the decoder actually heard around each unconfirmed spot.

`spot_confirm.py` says WHICH spots no other skimmer corroborates. It cannot
say WHY, because a spot is only a call, a frequency and a time. This pairs
each unconfirmed spot with the raw words from the CHANNEL that emitted it
(`--log-channels` output), so the failure can be classified by MECHANISM
rather than by the shape of the resulting string.

Grouping by channel id is the whole point. Channels a few hundred Hz apart
are routinely different stations, so selecting words by frequency window
merges them and invents "drift" that never happened -- an earlier version of
this tool did exactly that and called 8 of 12 cases DRIFT wrongly. The id
comes from the tracker, so following it is exact.

Mechanisms, once one channel can be followed:

  INTERLEAVE  the channel carries another corroborated call, INTERMIXED in
              time with ours -- two stations sharing FFT bins, their tokens
              combing together. A caller zero-beating a runner.
  SPLICE      the channel carries another corroborated call but strictly
              BEFORE or AFTER ours -- the tracker reassigned the channel
              mid-transmission, so one channel is two stations end to end.
  DRIFT       this one channel's own frequency moved materially while it
              was decoding.
  ISOLATED    the channel carried no other corroborated call. Not
              necessarily a bust: a local station reaching you by ground
              wave looks exactly like this.

Usage:
    tools/bust_context.py --spots SPOTS.log --channels CHANNELS.log \\
        --archive 20260812.csv [--band 20m] [--top 25] [--secs 30]

SPOTS.log and CHANNELS.log must come from ONE replay, and CHANNELS.log must
have the channel-id column (added 2026-08-12). All three sources share a
clock: replay stamps audio time, so use a recording whose name carries its
start (record_iq.py does) or pass --start-time.
"""

import argparse
import collections
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))

from spot_confirm import load_all, edit_distance   # noqa: E402


def load_spots(path, day):
    """-> [(ts, freq_khz, call, snr, wpm, activity)] from a telnet-format log."""
    out = []
    for line in open(path, encoding="utf-8", errors="replace"):
        f = line.split()
        if len(f) < 9 or f[0] != "DX":
            continue
        hhmm = f[-1].rstrip("Z")
        try:
            ts = datetime.datetime.combine(
                day, datetime.time(int(hhmm[:2]), int(hhmm[2:4])))
            out.append((ts, float(f[3]), f[4], int(f[5]), int(f[7]), f[-2]))
        except (ValueError, IndexError):
            continue
    return out


def load_channels(path):
    """-> {chan_id: [(ts, freq_khz, snr, wpm, word), ...]} sorted by time."""
    by_chan = collections.defaultdict(list)
    bad = 0
    for line in open(path, encoding="utf-8", errors="replace"):
        f = line.split()
        if len(f) < 7:
            bad += 1
            continue
        try:
            ts = datetime.datetime.strptime(f[0] + " " + f[1].rstrip("Z"),
                                            "%Y-%m-%d %H:%M:%S")
            by_chan[int(f[3])].append(
                (ts, float(f[2]), int(f[4]), int(f[5]), f[6]))
        except ValueError:
            bad += 1
    for k in by_chan:
        by_chan[k].sort(key=lambda x: x[0])
    return by_chan, bad


def classify(words, call, real_calls):
    """(verdict, detail) for one channel's words around one spot.

    `words` are this channel's only; `real_calls` is the set of calls other
    skimmers corroborated on this band, used to tell a genuine second station
    from decode debris.
    """
    # A call is "present" if it is the token OR is fused inside one: real
    # decodes run prosigns onto calls ("TUW5TM", "TESTW5TM"), and treating
    # those as absent makes two stations look sequential when they are
    # actually alternating -- it turned N4DPM/W5TM into a false SPLICE.
    def mentions(word, who):
        return word == who or (len(who) >= 4 and who in word)

    ours = [t for t, _, _, _, w in words if mentions(w, call)]
    other = [(t, c) for t, _, _, _, w in words for c in real_calls
             if c != call and len(c) >= 4 and mentions(w, c)]
    freqs = [f for _, f, _, _, _ in words]
    span = (max(freqs) - min(freqs)) if freqs else 0.0

    if other:
        names = collections.Counter(w for _, w in other)
        who = names.most_common(1)[0][0]
        ot = [t for t, w in other if w == who]
        if ours and ot and (max(ours) < min(ot) or max(ot) < min(ours)):
            # sequential by exact times -- but only call it a splice if the
            # two runs do not touch; adjacent runs are usually one pileup
            gap = (min(ot) - max(ours) if max(ours) < min(ot)
                   else min(ours) - max(ot)).total_seconds()
            if gap < 5.0:
                return "INTERLEAVE", f"with {who} ({names[who]}x, {gap:.0f}s apart)"
        if ours and ot:
            # strictly separated in time -> the channel changed hands
            if max(ours) < min(ot) or max(ot) < min(ours):
                return "SPLICE", f"{who} then {call}" if max(ot) < min(ours) \
                    else f"{call} then {who}"
            return "INTERLEAVE", f"with {who} ({names[who]}x)"
        return "INTERLEAVE", f"with {who} ({names[who]}x)"
    if span >= 0.15:
        return "DRIFT", f"channel moved {span * 1000:.0f} Hz"
    return "ISOLATED", "no other corroborated call in this channel"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spots", required=True)
    ap.add_argument("--channels", required=True)
    ap.add_argument("--archive", required=True)
    ap.add_argument("--call", required=True)
    ap.add_argument("--band", default="20m")
    ap.add_argument("--secs", type=float, default=30.0,
                    help="context window either side of the spot")
    ap.add_argument("--window", type=float, default=600.0,
                    help="seconds within which a peer spot confirms")
    ap.add_argument("--top", type=int, default=25,
                    help="how many to print in full (all are tallied)")
    args = ap.parse_args()

    rows, _ = load_all(args.archive)
    if not rows:
        print("empty archive", file=sys.stderr)
        return 1
    day = rows[0][0].date()

    peers = collections.defaultdict(list)
    real_calls = set()
    pat = args.call.rstrip("*")
    for ts, sp, dx, band, f, snr in rows:
        if band != args.band:
            continue
        if sp.upper().startswith(pat):
            continue
        peers[dx].append(ts)
        real_calls.add(dx)

    spots = load_spots(args.spots, day)
    chans, bad = load_channels(args.channels)
    if not chans:
        print(f"no channel records parsed from {args.channels} "
              f"({bad} unparsed lines) -- does it have the channel-id "
              f"column?", file=sys.stderr)
        return 1

    # word -> [(ts, chan_id)] so a spot can be traced to the channel that said it
    said = collections.defaultdict(list)
    for cid, ws in chans.items():
        for t, _, _, _, w in ws:
            said[w].append((t, cid))

    unconfirmed = [s for s in spots
                   if not any(abs((t - s[0]).total_seconds()) <= args.window
                              for t in peers.get(s[2], ()))]
    print(f"{len(spots)} spots, {len(unconfirmed)} unconfirmed "
          f"({100.0 * len(unconfirmed) / max(1, len(spots)):.1f}%) "
          f"on {args.band}; {len(chans)} channels in the log\n")

    tally = collections.Counter()
    shown = 0
    for ts, khz, call, snr, wpm, act in unconfirmed:
        cands = [(abs((t - ts).total_seconds()), cid)
                 for t, cid in said.get(call, ())
                 if abs((t - ts).total_seconds()) <= 120]
        if not cands:
            tally["NOT IN LOG"] += 1
            continue
        cid = min(cands)[1]
        lo, hi = (ts - datetime.timedelta(seconds=args.secs),
                  ts + datetime.timedelta(seconds=args.secs))
        words = [w for w in chans[cid] if lo <= w[0] <= hi]
        verdict, detail = classify(words, call, real_calls)
        tally[verdict] += 1
        if shown < args.top:
            shown += 1
            print(f"== {call:<10} {khz:9.2f} kHz  chan {cid:<5} {snr:>2} dB "
                  f"{wpm:>2} wpm  {act:<4} {ts.strftime('%H:%M:%S')}Z")
            print(f"   -> {verdict}: {detail}")
            print("   channel: "
                  + " ".join(w[4] for w in words)[:280])
            print()

    total = sum(tally.values())
    print("mechanism tally over ALL unconfirmed spots:")
    for k, n in tally.most_common():
        print(f"  {n:5} ({100.0 * n / max(1, total):5.1f}%)  {k}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
