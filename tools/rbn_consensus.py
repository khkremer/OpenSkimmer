#!/usr/bin/env python3
"""Caller-as-runner rate: K1MK's "off-consensus" measure, from the RBN archive.

K1MK's method (k1mk-7388.github.io/ContestPages/RBNSkimmerAnomaly2026):
CW spots only, the NCDXF beacon frequencies left out. Spots are grouped
into cells of one 0.1 kHz frequency x one 5-minute slot. In each cell, the
call the most distinct skimmers report holds the frequency. When at least
MIN_BACKERS skimmers back it (and no other call ties it), the cell is
"decided". A skimmer's spots in decided cells are "judged". A judged spot
naming a different call is "off-consensus": nearly always a caller reported
as though it were the runner.

  rbn_consensus.py field ARCHIVE.zip [...]
      every skimmer's rate (judged >= 100), the field median, and the
      --call skimmer's rank, by band and by hour
  rbn_consensus.py replay ARCHIVE.zip SPOTS [SPOTS ...]
      score a replay's "DX de" lines (times must be real UTC, i.e. a
      --start-time replay) against the cells built from OTHER skimmers'
      spots that day, so the archive's own spots for --call don't vote
"""
import argparse
import csv
import io
import re
import statistics
import sys
import zipfile
from collections import Counter, defaultdict

MIN_BACKERS = 4
MIN_JUDGED = 100
BEACON_KHZ = {14100.0, 18110.0, 21150.0, 24930.0, 28200.0}
DX = re.compile(r"DX de \S+:\s+([\d.]+)\s+(\S+)\s+-?\d+ dB\s+\d+ WPM\s+(\S*)\s*(\d{2})(\d{2})Z")


def cell_of(khz, hh, mm):
    return (round(khz, 1), hh * 12 + mm // 5)


def read_archive(path):
    """Yield (spotter, khz, dx, hh, mm) for CW, non-beacon spots."""
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            with z.open(name) as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8", errors="replace")):
                    if r.get("tx_mode") != "CW" or r.get("mode") in ("BEACON", "NCDXF B"):
                        continue
                    try:
                        khz = float(r["freq"])
                        hh, mm = int(r["date"][11:13]), int(r["date"][14:16])
                    except (ValueError, KeyError, TypeError):
                        continue
                    if round(khz, 1) in BEACON_KHZ:
                        continue
                    yield r["callsign"].upper(), khz, r["dx"].upper(), hh, mm


def decide(cells):
    """cells: {cell: {dx: set(spotters)}} -> {cell: holder} for decided cells."""
    out = {}
    for c, calls in cells.items():
        ranked = sorted(((len(s), dx) for dx, s in calls.items()), reverse=True)
        if ranked[0][0] >= MIN_BACKERS and (len(ranked) == 1 or ranked[1][0] < ranked[0][0]):
            out[c] = ranked[0][1]
    return out


def band_of(khz):
    for lo, hi, b in ((1800, 2000, "160"), (3500, 4000, "80"), (5330, 5410, "60"), (7000, 7300, "40"),
                      (10100, 10150, "30"), (14000, 14350, "20"), (18068, 18168, "17"),
                      (21000, 21450, "15"), (24890, 24990, "12"), (28000, 29700, "10")):
        if lo <= khz <= hi:
            return b
    return "?"


def field(args):
    spots = []
    cells = defaultdict(lambda: defaultdict(set))
    for path in args.archive:
        for sp, khz, dx, hh, mm in read_archive(path):
            c = cell_of(khz, hh, mm) + (path,)
            cells[c][dx].add(sp)
            spots.append((sp, c, dx, khz, hh))
    held = decide(cells)
    judged, off = Counter(), Counter()
    me_band, me_hour = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    me = args.call.upper()
    for sp, c, dx, khz, hh in spots:
        h = held.get(c)
        if h is None:
            continue
        judged[sp] += 1
        bad = dx != h
        off[sp] += bad
        if sp == me:
            me_band[band_of(khz)][0] += 1
            me_band[band_of(khz)][1] += bad
            me_hour[hh][0] += 1
            me_hour[hh][1] += bad
    rates = {s: 100.0 * off[s] / judged[s] for s in judged if judged[s] >= MIN_JUDGED}
    ranked = sorted(rates, key=rates.get, reverse=True)
    print(f"{len(held)} decided cells; {len(rates)} skimmers with >= {MIN_JUDGED} judged spots; "
          f"field median {statistics.median(rates.values()):.1f}%")
    for i, s in enumerate(ranked[:args.top], 1):
        print(f"  {i:3} {s:12} {rates[s]:5.1f}%  ({off[s]}/{judged[s]})")
    if me in rates:
        print(f"\n{me}: {rates[me]:.1f}% ({off[me]}/{judged[me]}), rank {ranked.index(me) + 1} of {len(ranked)}")
        print("  by band:", "  ".join(f"{b}m {100*o/j:.0f}% ({o}/{j})" for b, (j, o) in sorted(me_band.items(), key=lambda kv: -kv[1][0])))
        print("  by hour:", " ".join(f"{h:02d}:{100*o/j:.0f}%" for h, (j, o) in sorted(me_hour.items()) if j >= 20))


def replay(args):
    cells = defaultdict(lambda: defaultdict(set))
    me = args.call.upper()
    for sp, khz, dx, hh, mm in read_archive(args.archive[0]):
        if not sp.startswith(me):
            cells[cell_of(khz, hh, mm)][dx].add(sp)
    held = decide(cells)
    for path in args.spots:
        judged = off = total = 0
        by_type = Counter()
        examples = []
        for line in open(path, errors="replace"):
            m = DX.search(line)
            if not m:
                continue
            total += 1
            khz, dx, typ = float(m.group(1)), m.group(2).upper(), m.group(3) or "-"
            h = held.get(cell_of(khz, int(m.group(4)), int(m.group(5))))
            if h is None:
                continue
            judged += 1
            if dx != h:
                off += 1
                by_type[typ] += 1
                if len(examples) < args.examples:
                    examples.append(f"{m.group(4)}{m.group(5)}Z {khz:.1f} {dx} ({typ}) where {h} ran")
        rate = 100.0 * off / judged if judged else 0.0
        print(f"{path}: {total} spots, {judged} judged, {off} off-consensus = {rate:.1f}%  {dict(by_type)}")
        for e in examples:
            print("    " + e)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("what", choices=("field", "replay"))
    ap.add_argument("archive", nargs="+", help="RBN daily archive zip(s); for replay, one zip then spot files")
    ap.add_argument("--call", required=True)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--examples", type=int, default=0)
    args = ap.parse_args()
    if args.what == "replay":
        args.spots = args.archive[1:]
        args.archive = args.archive[:1]
        replay(args)
    else:
        field(args)


if __name__ == "__main__":
    sys.exit(main())
