#!/usr/bin/env python3
"""Summarise 'RTP packet(s) lost' warnings into episodes, and time them.

A busy log has thousands of these lines and collapsing them per second still
fills a screen. What actually diagnoses the fault is the shape: how long each
outage lasted, how much of the expected traffic vanished inside it, and how
far apart the outages are.

    journalctl -u openskimmer --since '3 days ago' --no-pager | tools/loss_episodes.py
    tools/loss_episodes.py /var/log/skimmer.log --gap 10

Why the spacing matters. A brief TOTAL outage on every band at once is not a
buffer overflow -- an overflowing socket sheds packets progressively, while a
link that stops delivers nothing and then resumes. If those outages recur on
a regular cadence, the usual cause is the switch's IGMP snooping state
lapsing (membership timeout, or a querier re-election), which stops
forwarding the groups to this port until the next report. Random spacing
points instead at the link, the driver, or the sender.

Pair it with `nstat -az | grep UdpRcvbufErrors`: RTP sequence gaps with a
ZERO kernel drop counter mean the packets never reached this host at all.
"""

import argparse
import collections
import datetime
import re
import statistics
import sys

# the app's own stamp carries milliseconds and a year; the journal prefix does not
TS = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[,.](\d{1,6})")
LOST = re.compile(r"(\d+)\s+RTP packet\(s\) lost")
PID = re.compile(r"\[(\d+)\]")


def parse(stream):
    """-> [(datetime, packets_lost, worker_pid_or_None)] in file order."""
    out, unparsed = [], 0
    for line in stream:
        m = LOST.search(line)
        if not m:
            continue
        t = TS.search(line)
        if not t:
            unparsed += 1
            continue
        frac = t.group(2).ljust(6, "0")[:6]
        ts = datetime.datetime.strptime(t.group(1).replace("T", " "),
                                        "%Y-%m-%d %H:%M:%S")
        ts = ts.replace(microsecond=int(frac))
        p = PID.search(line)
        out.append((ts, int(m.group(1)), p.group(1) if p else None))
    out.sort(key=lambda x: x[0])
    return out, unparsed


def episodes(events, gap):
    """Group events separated by less than `gap` seconds."""
    eps = []
    for ts, n, pid in events:
        if eps and (ts - eps[-1][-1][0]).total_seconds() <= gap:
            eps[-1].append((ts, n, pid))
        else:
            eps.append([(ts, n, pid)])
    return eps


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("logfile", nargs="?", help="default: stdin")
    ap.add_argument("--gap", type=float, default=5.0,
                    help="seconds of quiet that separate two episodes")
    ap.add_argument("--pps", type=float, default=533.0,
                    help="RTP packets/s PER WORKER, for the share-lost "
                         "estimate. 533 = 192 kHz / 360-sample packets; "
                         "halve it for a 96 kHz band")
    ap.add_argument("--top", type=int, default=20,
                    help="episodes to list (longest first)")
    args = ap.parse_args()

    stream = open(args.logfile, errors="replace") if args.logfile else sys.stdin
    events, unparsed = parse(stream)
    if not events:
        print("no 'RTP packet(s) lost' lines found", file=sys.stderr)
        return 1
    if unparsed:
        print(f"note: {unparsed} loss lines had no parsable timestamp",
              file=sys.stderr)

    eps = episodes(events, args.gap)
    total = sum(n for _, n, _ in events)
    span = (events[-1][0] - events[0][0]).total_seconds()
    workers = collections.Counter(p for _, _, p in events if p)

    print(f"\n{len(events)} loss reports, {total:,} packets, in "
          f"{len(eps)} episodes")
    print(f"log spans {events[0][0]} .. {events[-1][0]}"
          f"  ({span / 3600:.1f} h)")
    if workers:
        print("per worker: " + ", ".join(f"pid {k}: {v:,} reports"
                                         for k, v in workers.most_common()))

    rows = []
    for e in eps:
        dur = (e[-1][0] - e[0][0]).total_seconds()
        lost = sum(n for _, n, _ in e)
        nw = len({p for _, _, p in e if p}) or 1
        # what SHOULD have arrived in that window, across the workers seen
        expect = max(dur, 1.0 / args.pps) * args.pps * nw
        rows.append((e[0][0], dur, lost, len(e), nw,
                     100.0 * lost / expect if expect else 0.0))

    print(f"\n{'start':<24}{'dur s':>8}{'packets':>9}{'bursts':>8}"
          f"{'wrkrs':>7}{'~% of expected':>16}")
    for r in sorted(rows, key=lambda x: -x[2])[:args.top]:
        print(f"{str(r[0]):<24}{r[1]:>8.2f}{r[2]:>9,}{r[3]:>8}{r[4]:>7}"
              f"{min(r[5], 999):>15.0f}%")
    if len(rows) > args.top:
        print(f"... and {len(rows) - args.top} more episodes")

    # the periodicity test
    if len(eps) >= 3:
        gaps = [(b[0][0] - a[-1][0]).total_seconds()
                for a, b in zip(eps, eps[1:])]
        med = statistics.median(gaps)
        spread = statistics.pstdev(gaps) / med if med else 999
        print(f"\ngaps between episodes: median {med:,.0f} s "
              f"(min {min(gaps):,.0f}, max {max(gaps):,.0f}), "
              f"relative spread {spread:.2f}")
        if spread < 0.25 and med < 600:
            print("  -> REGULAR AND SHORT. A repeating brief outage on every "
                  "band is the\n     signature of IGMP snooping state lapsing "
                  "on the switch\n     (membership timeout / querier "
                  "re-election). Check the switch,\n     and whether a "
                  "querier exists on this VLAN.")
            if 15 < med < 300:
                print(f"     {med:.0f} s is in the usual query-interval / "
                      "membership-timeout range.")
        elif spread < 0.25:
            # Regular is not the same as IGMP. Timers that long are jobs.
            print(f"  -> REGULAR BUT SLOW ({med / 3600:.1f} h between "
                  "episodes). FAR too long for any IGMP\n     timer, which "
                  "runs 20-125 s -- do not read this as a switch problem.\n"
                  "     A period of hours means something SCHEDULED. Compare "
                  "the episode\n     start AND end times against crontab -l, "
                  "sudo crontab -l,\n     systemctl list-timers --all, and "
                  "any backup window; then read\n     the journal either side "
                  "of a boundary to name the event.")
        else:
            print("  -> IRREGULAR. Not a timer. Look at the link and driver "
                  "(ip -s link),\n     at the sender, or at whatever else runs "
                  "on this host at those times.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
