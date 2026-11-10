#!/usr/bin/env python3
"""Flag your RBN spots that no other skimmer confirms -- candidate busted calls.

The RBN daily archive contains every spot every skimmer sent, so it is a
ready-made second opinion on your own decoder: if you spotted a call and
nobody else on the network heard it anywhere near the same time, either the
call is wrong or you are hearing something they cannot.

    curl -O https://data.reversebeacon.net/rbn_history/20260804.zip
    tools/spot_confirm.py 20260804.zip --call 'N0CALL*'

The pattern is a glob, so 'N0CALL*' picks up N0CALL, N0CALL-1, N0CALL-#, ... and
-- importantly -- every spotter it matches is excluded from the confirming
set, so your own second receiver never confirms your first.

UNCONFIRMED IS NOT THE SAME AS WRONG. A station a few miles away reaches you
by ground wave with no skywave path to anyone else, and looks exactly like a
bust in this analysis. Two columns help you separate them:

  my SNR    a local ground-wave station is usually very strong; a garble of
            a distant station usually is not.
  likely    if some other skimmer was spotting a nearly identical call on
            nearly the same frequency at the same moment, yours is almost
            certainly a garble of that -- and this names it.

A future version could print the station's home grid to settle the
ground-wave question outright; that needs a call->grid source (a club log,
cty.dat plus a QSL database, ...) which the RBN archive does not carry.

Columns in the archive: callsign,de_pfx,de_cont,freq,band,dx,dx_pfx,dx_cont,
mode,db,date,speed,tx_mode   (callsign = the spotter, dx = the station spotted)
"""

import argparse
import bisect
import csv
import datetime
import fnmatch
import io
import sys
import zipfile
from collections import defaultdict


def load_all(path):
    """-> ([(ts, spotter, dx, band, freq_khz, snr_db), ...] sorted by ts, bad)"""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            handle = io.StringIO(z.read(name).decode("utf-8", "replace"))
    else:
        handle = open(path, encoding="utf-8", errors="replace")
    rows, bad = [], 0
    with handle:
        for r in csv.DictReader(handle):
            try:
                ts = datetime.datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S")
                try:
                    snr = int(r["db"])
                except (TypeError, ValueError):
                    snr = -1
                rows.append((ts, r["callsign"].upper(), r["dx"].upper(),
                             r["band"], float(r["freq"]), snr))
            except (TypeError, ValueError, KeyError):
                bad += 1
    rows.sort(key=lambda x: x[0])
    return rows, bad


def edit_distance(a, b, cap=2):
    """Levenshtein distance, giving up once it exceeds `cap` (returns cap+1)."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def near_miss(spot, by_band, window, freq_tol, cap):
    """The call other skimmers were spotting next to `spot` on nearly the
    same frequency -- i.e. what yours is probably a garble of.

    -> (call, distance, n_spotters) or None. Prefers the smallest edit
    distance, then the most independent skimmers behind it."""
    _ts, _sp, dx, band, fq, _snr = spot
    entries = by_band.get(band)
    if not entries:
        return None
    times = [e[0] for e in entries]
    lo = bisect.bisect_left(times, _ts - datetime.timedelta(seconds=window))
    hi = bisect.bisect_right(times, _ts + datetime.timedelta(seconds=window))
    cands = defaultdict(set)
    for ts, spotter, other, _b, ofq, _s in entries[lo:hi]:
        if other == dx or abs(ofq - fq) > freq_tol:
            continue
        cands[other].add(spotter)
    best = None
    for other, spotters in cands.items():
        d = edit_distance(dx, other, cap)
        if d > cap:
            continue
        cand = (d, -len(spotters), other, len(spotters))
        if best is None or cand < best:
            best = cand
    if best is None:
        return None
    return best[2], best[0], best[3]


def family(call):
    """N0CALL-1 and N0CALL are one station and must not confirm each other."""
    return call.split("-")[0]


def parse_clock(text, day):
    """"13:10" or "13:10:00" (UTC) -> a datetime on `day`."""
    if text is None:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            t = datetime.datetime.strptime(text, fmt).time()
        except ValueError:
            continue
        return datetime.datetime.combine(day, t)
    raise SystemExit(f"bad time {text!r} -- expected HH:MM or HH:MM:SS (UTC)")


def in_window(ts, t_from, t_until):
    return not ((t_from and ts < t_from) or (t_until and ts >= t_until))


def unconfirmed_rate_all(rows, window, any_band, min_confirmers,
                         t_from=None, t_until=None):
    """Every skimmer's unconfirmed rate, in one pass over the archive.

    Needed for context: an unconfirmed percentage means nothing on its own,
    because it depends far more on the day's activity and on how many
    skimmers share your propagation than on how good your decoder is. What
    matters is where you sit against everyone else on the SAME day.

    t_from/t_until restrict which spots are SCORED, for everyone alike, so a
    part-day comparison stays fair. Confirmation is always sought across the
    whole archive -- a spot at 13:09 is still confirmed by someone else's at
    13:12, which is what makes a cut at a deployment time meaningful.

    -> {spotter: (total, unconfirmed)}
    """
    groups = defaultdict(list)
    for r in rows:
        groups[(r[2], None if any_band else r[3])].append(r)
    win = datetime.timedelta(seconds=window)
    tally = defaultdict(lambda: [0, 0])
    for spots in groups.values():
        spots.sort(key=lambda x: x[0])
        times = [s[0] for s in spots]
        for i, s in enumerate(spots):
            if not in_window(s[0], t_from, t_until):
                continue          # not scored, but still available to confirm
            lo = bisect.bisect_left(times, s[0] - win)
            hi = bisect.bisect_right(times, s[0] + win)
            mine = family(s[1])
            seen = {family(o[1]) for o in spots[lo:hi]
                    if family(o[1]) != mine}
            t = tally[s[1]]
            t[0] += 1
            if len(seen) < min_confirmers:
                t[1] += 1
    return {k: tuple(v) for k, v in tally.items()}


def main():
    ap = argparse.ArgumentParser(
        description="flag RBN spots of yours that no other skimmer confirms")
    ap.add_argument("archive", help="RBN daily archive (.zip or .csv)")
    ap.add_argument("--call", required=True, metavar="PATTERN",
                    help="your spotter call, glob-matched: 'N0CALL*' covers "
                         "N0CALL and N0CALL-1. Every match is excluded from "
                         "the confirming set")
    ap.add_argument("--window", type=float, default=600,
                    help="seconds either side in which another skimmer's "
                         "spot counts as confirmation (default 600)")
    ap.add_argument("--any-band", action="store_true",
                    help="confirm on the callsign alone, ignoring band "
                         "(default: the confirming spot must be same-band)")
    ap.add_argument("--min-confirmers", type=int, default=1,
                    help="how many other skimmers must have heard it "
                         "(default 1)")
    ap.add_argument("--near-freq", type=float, default=2.0,
                    help="kHz around your spot to search for the call you "
                         "probably garbled (default 2.0)")
    ap.add_argument("--near-dist", type=int, default=2,
                    help="maximum edit distance for that suggestion "
                         "(default 2)")
    ap.add_argument("--min-snr", type=int, default=None,
                    help="only report unconfirmed spots below this SNR -- "
                         "strong ones are more likely a real local station")
    ap.add_argument("--top", type=int, default=40,
                    help="how many unconfirmed calls to list")
    ap.add_argument("--from", dest="t_from", metavar="HH:MM",
                    help="only score spots at or after this UTC time; "
                         "confirmation still uses the whole day, so a cut at "
                         "a deployment time gives a clean before/after inside "
                         "one archive")
    ap.add_argument("--until", dest="t_until", metavar="HH:MM",
                    help="only score spots before this UTC time")
    ap.add_argument("--baseline", action="store_true",
                    help="also show every busy skimmer's unconfirmed rate on "
                         "the same day -- the only way to know whether yours "
                         "is high (it depends mostly on the day's activity)")
    ap.add_argument("--min-spots", type=int, default=300,
                    help="ignore skimmers with fewer spots in --baseline")
    args = ap.parse_args()

    rows, bad = load_all(args.archive)
    if bad:
        print(f"(skipped {bad} malformed rows)")
    if not rows:
        print("no spots parsed", file=sys.stderr)
        return 1

    pat = args.call.upper()
    is_mine = lambda c: fnmatch.fnmatch(c, pat)

    mine = [r for r in rows if is_mine(r[1])]
    if not mine:
        near = sorted({c for _t, c, *_ in rows
                       if c.startswith(pat.rstrip("*")[:3])})[:6]
        print(f"no spots matched {args.call!r}"
              + (f" -- did you mean {', '.join(near)}?" if near else ""),
              file=sys.stderr)
        return 1

    my_calls = sorted({r[1] for r in mine})
    day = mine[0][0].date()
    t_from = parse_clock(args.t_from, day)
    t_until = parse_clock(args.t_until, day)
    if t_from or t_until:
        mine = [r for r in mine if in_window(r[0], t_from, t_until)]
        if not mine:
            print("no spots of yours inside that time window", file=sys.stderr)
            return 1

    # everyone else's spots, indexed for the two lookups we need
    others_by_dx = defaultdict(list)
    others_by_band = defaultdict(list)
    for r in rows:
        if is_mine(r[1]):
            continue
        others_by_dx[r[2]].append(r)
        others_by_band[r[3]].append(r)
    for v in others_by_dx.values():
        v.sort(key=lambda x: x[0])
    for v in others_by_band.values():
        v.sort(key=lambda x: x[0])

    win = datetime.timedelta(seconds=args.window)
    unconfirmed = []
    n_conf = 0
    for spot in mine:
        ts, _sp, dx, band, _fq, _snr = spot
        seen = {o[1] for o in others_by_dx.get(dx, [])
                if abs(o[0] - ts) <= win and (args.any_band or o[3] == band)}
        if len(seen) >= args.min_confirmers:
            n_conf += 1
        else:
            unconfirmed.append(spot)

    scope = "any band" if args.any_band else "same band"
    span = ""
    if t_from or t_until:
        span = (f"  [scoring {args.t_from or '00:00'}-"
                f"{args.t_until or '24:00'} UTC only; confirmation still "
                f"uses the whole day]")
    print(f"\n{args.call} on {day}: {len(mine)} spots of "
          f"{len({r[2] for r in mine})} unique calls{span}")
    print(f"  spotter calls matched: {', '.join(my_calls)}")
    print(f"\nconfirmed by >={args.min_confirmers} other skimmer(s) within "
          f"{args.window:.0f}s ({scope}): "
          f"{n_conf} ({100.0 * n_conf / len(mine):.1f}%)")
    print(f"UNCONFIRMED: {len(unconfirmed)} spots "
          f"({100.0 * len(unconfirmed) / len(mine):.1f}%), "
          f"{len({s[2] for s in unconfirmed})} calls")

    if args.baseline:
        rates = unconfirmed_rate_all(rows, args.window, args.any_band,
                                     args.min_confirmers, t_from, t_until)
        peers = sorted(((100.0 * u / t, c, t) for c, (t, u) in rates.items()
                        if t >= args.min_spots))
        if peers:
            print(f"\nunconfirmed rate across {len(peers)} skimmers with "
                  f">={args.min_spots} spots today:")
            print(f"  {'spotter':<12}{'spots':>7}{'unconf':>9}")
            for pct, c, t in peers[:8]:
                print(f"  {c:<12}{t:>7}{pct:>8.1f}%   lowest")
            med = peers[len(peers) // 2][0]
            print(f"  {'...':<12}")
            print(f"  {'median':<12}{'':>7}{med:>8.1f}%")
            ranked_me = [(p, c, t) for p, c, t in peers if is_mine(c)]
            for row in ranked_me:
                pct, c, t = row
                print(f"  {c:<12}{t:>7}{pct:>8.1f}%   <<< YOU, rank "
                      f"{peers.index(row) + 1}/{len(peers)} (1 = best)")
            if not ranked_me:
                # below --min-spots: still show it, flagged, rather than
                # leaving the reader without their own number
                for c in my_calls:
                    if c in rates:
                        t, u = rates[c]
                        print(f"  {c:<12}{t:>7}{100.0 * u / t:>8.1f}%   "
                              f"<<< YOU (below the {args.min_spots}-spot "
                              f"threshold, so unranked)")
        print("\n  A high rate here is mostly about the day and your"
              "\n  propagation overlap with other skimmers, not your decoder."
              "\n  Compare yourself against the median, and watch the trend"
              "\n  across days rather than any single number.")

    if args.min_snr is not None:
        before = len(unconfirmed)
        unconfirmed = [s for s in unconfirmed if s[5] < args.min_snr]
        print(f"  (showing only SNR < {args.min_snr} dB: "
              f"{len(unconfirmed)} of {before} -- strong unconfirmed spots "
              f"are more often real local stations than busts)")

    # group by call so a station spotted repeatedly reads as one row
    by_call = defaultdict(list)
    for s in unconfirmed:
        by_call[s[2]].append(s)

    # "elsewhere" is the sharpest discriminator in the whole report: a call
    # NO other skimmer reported at ANY time today is very likely not a real
    # callsign at all, while one others did report is a real station whose
    # spot simply went unconfirmed in your window (a brief CQ, a local
    # ground-wave path, a band nobody else was watching).
    ranked = sorted(by_call.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    unknown = [(c, s) for c, s in ranked if not others_by_dx.get(c)]
    elsewhere = [(c, s) for c, s in ranked if others_by_dx.get(c)]

    def show(title, items, note):
        if not items:
            return
        print(f"\n{title}")
        print(f"  {'call':<12}{'n':>3}  {'bands':<12}{'kHz':>9}{'SNR':>6}"
              f"  {'elsewhere':>9}  {'first':<9}likely a garble of")
        for call, spots in items[:args.top]:
            bands = ",".join(sorted({s[3] for s in spots}))
            others = len({o[1] for o in others_by_dx.get(call, [])})
            nm = near_miss(spots[0], others_by_band, args.window,
                           args.near_freq, args.near_dist)
            hint = ""
            if nm:
                other, dist, n = nm
                hint = f"{other} (dist {dist}, {n} skimmer{'s' if n > 1 else ''})"
            print(f"  {call:<12}{len(spots):>3}  {bands:<12}"
                  f"{spots[0][4]:>9.1f}{max(s[5] for s in spots):>5} dB"
                  f"  {others:>9}  {spots[0][0].strftime('%H:%M:%S'):<9}{hint}")
        if len(items) > args.top:
            print(f"  ... and {len(items) - args.top} more "
                  f"(raise --top to see them)")
        print(f"  {note}")

    show(f"A. call spotted by NOBODY else today ({len(unknown)} calls) "
         f"-- prime bust candidates:", unknown,
         "these callsigns appear nowhere else in the archive")
    show(f"B. call IS spotted by others today, just not near your spot "
         f"({len(elsewhere)} calls):", elsewhere,
         "real stations; 'elsewhere' counts the other skimmers that heard "
         "them at some point")

    named = sum(1 for c, s in unknown
                if near_miss(s[0], others_by_band, args.window,
                             args.near_freq, args.near_dist))
    print(f"\n{named} of the {len(unknown)} group-A calls sit within "
          f"{args.near_dist} character(s) of a call other skimmers were"
          f"\nspotting on the same frequency at the same moment -- those are "
          f"very likely busts,\nand the suggestion names what you probably "
          f"should have decoded.")
    print("\nThe rest of group A are unexplained: a station only you heard "
          "(check for a high\nSNR -- a few miles away by ground wave reads "
          "very strong), or a garble of\nsomething nobody else spotted "
          "either.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
