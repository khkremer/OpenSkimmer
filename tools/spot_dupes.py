#!/usr/bin/env python3
"""Analyse a spot log for RBN-style duplicates and explain where they come from.

The RBN counts a spot as a duplicate when the same skimmer re-spots the same
callsign on the same band inside its dedup window. A high dupe percentage
means the skimmer is talking more than the RBN wants to listen -- it does not
mean the decodes are wrong. This tool measures the rate from your own output
and, more usefully, splits the duplicates by CAUSE:

  * "re-spot"     the same call at (essentially) the same frequency, spotted
                  again after --respot elapsed. Cure: raise --respot.
  * "freq split"  the same call spotted at a *different* frequency inside the
                  window, so the de-duplicator's frequency bucket never
                  matched. Usually one signal being tracked by two decoders,
                  or a drifting/chirpy signal. Cure: widen the bucket.

Feed it the skimmer's stdout (spots are printed there), e.g.

  tools/spot_dupes.py skimmer.log
  journalctl -u openskimmer --since today | tools/spot_dupes.py
  tools/spot_dupes.py skimmer.log --window 600 --sweep

Spot lines carry only HH:MM, so timing resolution is one minute; that is
plenty for windows measured in minutes. --sweep tries a range of windows,
which lets you match the dupe percentage the RBN reports for your station and
infer the window it is actually using.
"""

import argparse
import csv
import datetime
import io
import re
import sys
import zipfile
from collections import Counter, defaultdict

SPOT_RE = re.compile(
    r"DX de \s*(?P<de>\S+?)-#:\s+(?P<khz>\d+\.\d+)\s+(?P<call>\S+)\s+"
    r"(?P<snr>-?\d+) dB\s+(?P<wpm>\d+) WPM\s+(?P<act>\S+)\s+"
    r"(?P<hh>\d{2})(?P<mm>\d{2})Z"
)

BANDS = [(1800, 2000, "160m"), (3500, 4000, "80m"), (5250, 5450, "60m"),
         (7000, 7300, "40m"), (10100, 10150, "30m"), (14000, 14350, "20m"),
         (18068, 18168, "17m"), (21000, 21450, "15m"), (24890, 24990, "12m"),
         (28000, 29700, "10m"), (50000, 54000, "6m")]


def band_of(khz):
    for lo, hi, name in BANDS:
        if lo <= khz <= hi:
            return name
    return f"{int(khz // 1000)}MHz"


def parse(lines):
    """-> list of (minutes_since_start, call, khz, band, wpm, snr)."""
    out = []
    prev_min = None
    day = 0
    for line in lines:
        m = SPOT_RE.search(line)
        if not m:
            continue
        mins = int(m["hh"]) * 60 + int(m["mm"])
        if prev_min is not None and mins < prev_min - 60:
            day += 1                      # UTC midnight rollover
        prev_min = mins
        khz = float(m["khz"])
        out.append((day * 1440 + mins, m["call"], khz, band_of(khz),
                    int(m["wpm"]), int(m["snr"])))
    return out


def analyse(spots, window_min, freq_tol_khz):
    """Classify every spot as new / re-spot / freq-split within the window."""
    last = defaultdict(list)               # (call, band) -> [(min, khz)]
    counts = Counter()
    by_call = Counter()
    gaps = []
    for t, call, khz, band, _wpm, _snr in spots:
        key = (call, band)
        recent = [(pt, pk) for pt, pk in last[key] if t - pt < window_min]
        last[key] = recent + [(t, khz)]
        if not recent:
            counts["new"] += 1
            continue
        near = [pt for pt, pk in recent if abs(pk - khz) <= freq_tol_khz]
        if near:
            counts["re-spot"] += 1
            gaps.append(t - max(near))
        else:
            counts["freq split"] += 1
        by_call[f"{call} {band}"] += 1
    return counts, by_call, gaps


# -- RBN archive mode ------------------------------------------------------
# The RBN publishes every spot it receives -- nothing is dropped as a dupe --
# so your own duplicates can be counted straight out of the daily archive, and
# you can compare yourself against every other skimmer on the same day.
#   curl -O https://data.reversebeacon.net/rbn_history/20260731.zip
#   tools/spot_dupes.py --rbn 20260731.zip --call YOURCALL
# Columns: callsign,de_pfx,de_cont,freq,band,dx,dx_pfx,dx_cont,mode,db,date,
#          speed,tx_mode   (callsign = the spotter, dx = the station spotted)

RBN_WINDOWS = (60, 120, 300, 600, 1800)


def load_rbn(path):
    """-> {spotter: [(datetime, dx, band, freq_khz), ...]}, malformed_count"""
    if path.lower().endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = next(n for n in z.namelist() if n.lower().endswith(".csv"))
            raw = z.read(name).decode("utf-8", "replace")
        handle = io.StringIO(raw)
    else:
        handle = open(path, encoding="utf-8", errors="replace")
    data = defaultdict(list)
    bad = 0
    with handle:
        for r in csv.DictReader(handle):
            try:
                ts = datetime.datetime.strptime(r["date"], "%Y-%m-%d %H:%M:%S")
                data[r["callsign"]].append(
                    (ts, r["dx"], r["band"], float(r["freq"])))
            except (TypeError, ValueError, KeyError):
                bad += 1
    for v in data.values():
        v.sort(key=lambda x: x[0])
    return data, bad


def find_dupes(rows, window, by_freq=True, freq_tol=0.5):
    """Every spot that repeats an earlier one inside `window` seconds.

    -> [(prev_ts, ts, dx, band, freq_khz, gap_s), ...] in time order.
    dupe_pct() counts exactly these, so a listing always accounts for the
    percentage instead of approximating it by some slightly different rule
    (frequencies are bucketed, not compared pairwise: two spots either side
    of a bucket edge are not paired even if they are within freq_tol).
    """
    q = 1.0 / (2 * freq_tol) if freq_tol else 0
    last = {}
    out = []
    for ts, dx, band, fq in rows:
        key = (dx, band, round(fq * q) if by_freq else 0)
        prev = last.get(key)
        if prev is not None:
            gap = (ts - prev).total_seconds()
            if gap < window:
                out.append((prev, ts, dx, band, fq, gap))
        last[key] = ts
    return out


def dupe_pct(rows, window, by_freq=True, freq_tol=0.5):
    """Percentage of spots that repeat an earlier one inside `window` seconds."""
    if not rows:
        return 0.0
    return 100.0 * len(find_dupes(rows, window, by_freq, freq_tol)) / len(rows)


def apply_respot(rows, respot, freq_tol=0.5):
    """What the log would look like with a different --respot setting."""
    q = 1.0 / (2 * freq_tol) if freq_tol else 0
    last, out = {}, []
    for row in rows:
        ts, dx, band, fq = row
        key = (dx, band, round(fq * q))
        if key not in last or (ts - last[key]).total_seconds() >= respot:
            out.append(row)
            last[key] = ts
    return out


def print_dupe_list(rows, window, freq_tol):
    """The individual repeats behind the percentage, with their gaps.

    The gap is the diagnosis: one just under your --respot is the interval
    releasing on schedule (and only counts as a dupe because the analysis
    window is no shorter than the respot), while a much shorter one is a
    real repeat -- usually dedup state lost to a restart, or a second
    skimmer feeding the same call from another host."""
    dupes = find_dupes(rows, window, freq_tol=freq_tol)
    print(f"\nduplicate spots within {window:.0f}s "
          f"(same call+band+frequency): {len(dupes)}")
    if not dupes:
        return
    print(f"  {'when (UTC)':<21}{'station':<11}{'band':<6}{'kHz':>9}{'gap':>8}")
    for _prev, ts, dx, band, fq, gap in dupes:
        print(f"  {ts.strftime('%Y-%m-%d %H:%M:%S'):<21}{dx:<11}{band:<6}"
              f"{fq:>9.1f}{gap:>7.0f}s")


def rbn_report(data, call, ref_window, min_spots, top,
               list_dupes=False, window=600, freq_tol=0.5):
    mine = data.get(call)
    if not mine:
        near = [c for c in data if c.startswith(call.split("-")[0])]
        print(f"no spots for {call!r} in this archive"
              + (f" -- did you mean {', '.join(sorted(near)[:5])}?" if near else ""),
              file=sys.stderr)
        return 1
    uniq = len({(dx, band) for _t, dx, band, _f in mine})
    day = mine[0][0].date()
    print(f"{call} on {day}: {len(mine)} spots, {uniq} unique call+band\n")

    print("your dupe % by window (same call+band+frequency):")
    print("  " + "".join(f"{w:>8}s" for w in RBN_WINDOWS))
    print("  " + "".join(f"{dupe_pct(mine, w):8.1f} " for w in RBN_WINDOWS))
    print("\n  A cliff between two windows is the fingerprint of a --respot")
    print("  set to that interval: repeats hide below it and appear above it.")

    # peer comparison at one window
    peers = [(dupe_pct(v, ref_window), c, len(v))
             for c, v in data.items() if len(v) >= min_spots]
    peers.sort()
    mine_row = next((p for p in peers if p[1] == call), None)
    print(f"\nagainst {len(peers)} skimmers with >={min_spots} spots "
          f"(at a {ref_window:.0f}s window):")
    print(f"  {'spotter':<12}{'spots':>7}{'dupe%':>8}")
    for d, c, n in peers[:top]:
        print(f"  {c:<12}{n:>7}{d:>7.1f}%   cleanest")
    if mine_row:
        d, c, n = mine_row
        rank = peers.index(mine_row) + 1
        med = peers[len(peers) // 2][0]
        print(f"  {'...':<12}")
        print(f"  {c:<12}{n:>7}{d:>7.1f}%   <<< YOU, rank {rank}/{len(peers)}"
              f" (1 = cleanest)")
        print(f"  median skimmer: {med:.1f}%")

    print("\nwhat other --respot settings would have produced:")
    print(f"  {'--respot':<12}{'spots':>7}" + "".join(f"{w:>8}s" for w in RBN_WINDOWS))
    for respot in (60, 300, 600, 900, 1800):
        f = apply_respot(mine, respot)
        print(f"  {respot:<12}{len(f):>7}"
              + "".join(f"{dupe_pct(f, w):8.1f} " for w in RBN_WINDOWS))
    print("\n  (a simulation: it can only drop spots you actually sent, so it"
          "\n   understates a longer interval's unique-station count slightly)")

    worst = Counter()
    for ts, dx, band, _f in mine:
        worst[f"{dx} {band}"] += 1
    print(f"\nmost-repeated stations:")
    for name, n in worst.most_common(top):
        print(f"  {n:4d}  {name}")

    if list_dupes:
        print_dupe_list(mine, window, freq_tol)
    return 0


def main():
    ap = argparse.ArgumentParser(description="RBN duplicate analysis of a spot log")
    ap.add_argument("logfile", nargs="?", help="spot log (default: stdin)")
    ap.add_argument("--window", type=float, default=600,
                    help="RBN dedup window in SECONDS (default 600 = 10 min)")
    ap.add_argument("--freq-tol", type=float, default=0.5,
                    help="kHz within which two spots are the 'same' frequency")
    ap.add_argument("--sweep", action="store_true",
                    help="report the dupe rate across a range of windows")
    ap.add_argument("--top", type=int, default=12,
                    help="how many worst-offender calls to list")
    ap.add_argument("--rbn", metavar="ZIP_OR_CSV",
                    help="analyse an RBN daily archive instead of a local log "
                         "(data.reversebeacon.net/rbn_history/YYYYMMDD.zip)")
    ap.add_argument("--call", help="your spotter callsign, for --rbn")
    ap.add_argument("--ref-window", type=float, default=300,
                    help="window (s) used for the peer comparison in --rbn mode")
    ap.add_argument("--min-spots", type=int, default=300,
                    help="ignore skimmers with fewer spots in the comparison")
    ap.add_argument("--list-dupes", action="store_true",
                    help="--rbn mode: list the individual duplicate spots "
                         "behind the percentage, with the gap to the spot "
                         "each repeats (uses --window, default 600 s)")
    args = ap.parse_args()

    if args.rbn:
        if not args.call:
            print("--rbn needs --call YOURCALL", file=sys.stderr)
            return 1
        data, bad = load_rbn(args.rbn)
        if bad:
            print(f"(skipped {bad} malformed rows)\n")
        return rbn_report(data, args.call.upper(), args.ref_window,
                          args.min_spots, args.top,
                          list_dupes=args.list_dupes, window=args.window,
                          freq_tol=args.freq_tol)

    src = open(args.logfile, errors="replace") if args.logfile else sys.stdin
    spots = parse(src)
    if not spots:
        print("no spot lines found -- expected 'DX de CALL-#:  14031.0  W1AW"
              "  23 dB  25 WPM  CQ  1516Z'", file=sys.stderr)
        return 1

    span = (spots[-1][0] - spots[0][0]) or 1
    print(f"{len(spots)} spots over {span/60:.1f} h "
          f"({len(spots)/(span/60):.0f}/h)\n")

    if args.sweep:
        print("dupe rate vs assumed RBN window:")
        print(f"  {'window':>8}  {'dupes':>6}  {'of total':>9}")
        for w in (60, 120, 300, 600, 900, 1800, 3600):
            c, _, _ = analyse(spots, w / 60.0, args.freq_tol)
            dup = c["re-spot"] + c["freq split"]
            print(f"  {w//60:5d} min  {dup:6d}  {100*dup/len(spots):8.1f}%")
        print("\nMatch the percentage the RBN reports for your call to infer"
              "\nthe window it actually uses.\n")

    counts, by_call, gaps = analyse(spots, args.window / 60.0, args.freq_tol)
    dup = counts["re-spot"] + counts["freq split"]
    print(f"at a {args.window/60:.0f}-minute window:")
    print(f"  new (counted)      {counts['new']:6d}  "
          f"{100*counts['new']/len(spots):5.1f}%")
    print(f"  DUPES              {dup:6d}  {100*dup/len(spots):5.1f}%")
    print(f"    re-spot          {counts['re-spot']:6d}  "
          f"(same freq -- raise --respot)")
    print(f"    freq split       {counts['freq split']:6d}  "
          f"(different freq -- widen the dedup bucket)")

    if gaps:
        gaps.sort()
        med = gaps[len(gaps) // 2]
        print(f"\n  median gap between re-spots: {med:.0f} min "
              f"(min {gaps[0]:.0f}, max {gaps[-1]:.0f})")
        print(f"  -> a --respot of about {args.window:.0f}s would collapse these")

    if by_call:
        print(f"\nworst repeat offenders (dupes per call/band):")
        for name, n in by_call.most_common(args.top):
            print(f"  {n:4d}  {name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
