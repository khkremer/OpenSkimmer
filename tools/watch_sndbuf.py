#!/usr/bin/env python3
"""Capture the state of the machine WHILE multicast packets are being dropped.

UdpSndbufErrors is cumulative since boot, so a big number proves only that
something went wrong at some point -- it cannot tell you what, or when. The
drops here come in bounded episodes (one ran 26 minutes and accounted for
roughly 5 M errors), and by the time anyone looks, the machine is idle again
and the evidence is gone. Every diagnosis so far has been a guess
reconstructed after the fact, and two of those guesses were wrong.

So: poll the counter cheaply, and the moment it starts climbing, dump what
the box is doing into a file. One file per episode, with a snapshot at the
start, periodic snapshots while it lasts, and totals at the end.

ALSO polls --iface's qdisc drop count (`tc -s qdisc show`) as an
independent trigger, not only a snapshot field. Found 2026-09-03: an
fq_codel qdisc dropped 38 % of everything radiod sent on the radiod host's NIC
over 27 hours -- 132.8 M packets -- while UdpSndbufErrors stayed at 0 for
all but the last two minutes of that stretch. Qdisc drops are a THIRD loss
point, one layer above both the send- and receive-side socket counters,
and this tool used to be blind to them unless a socket-counter episode
happened to be open at the same moment to capture one in its snapshot.

    tools/watch_sndbuf.py --iface enp3s0f0
    tools/watch_sndbuf.py --counter RcvbufErrors --iface eth0   # on a receiver

Leave it running under systemd, /etc/systemd/system/watch-sndbuf.service:

    [Unit]
    Description=Capture host state during multicast drop episodes
    [Service]
    ExecStart=/usr/bin/python3 /opt/openskimmer/tools/watch_sndbuf.py \\
        --iface enp3s0f0 --out-dir /var/log/sndbuf-episodes
    Restart=always
    Nice=10
    [Install]
    WantedBy=multi-user.target

Nice=10 on purpose: this must never compete with radiod for the CPU it is
trying to observe.
"""

import argparse
import datetime
import os
import re
import subprocess
import sys
import time


def read_counter(path, proto, name):
    """One counter out of /proc/net/snmp, e.g. ('Udp', 'SndbufErrors')."""
    hdr = None
    try:
        with open(path) as f:
            for line in f:
                if not line.startswith(proto + ":"):
                    continue
                fields = line.split()[1:]
                if hdr is None:
                    hdr = fields
                    continue
                if name not in hdr:
                    return None
                return int(fields[hdr.index(name)])
    except (OSError, ValueError, IndexError):
        return None
    return None


def read_qdisc_drops(iface):
    """Cumulative packets dropped by iface's ROOT qdisc.

    There is no /proc shortcut for this -- qdisc stats are netlink-only --
    so shell out to `tc`, same as the rest of this file's iproute2 calls.
    Only the root qdisc's own line matters: with a plain qdisc (pfifo,
    fq_codel with no children) it is the only "dropped" figure there is,
    and a child qdisc under it would double count the same packets.
    Returns None on any failure -- a missing `tc` or a down interface must
    not stop the watcher, only skip this trigger.
    """
    if not iface:
        return None
    out = run(f"tc -s qdisc show dev {iface}", timeout=5)
    m = re.search(r"^qdisc \S+ .*?\broot\b.*\n.*?dropped (\d+)",
                  out, re.MULTILINE)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def run(cmd, timeout=10):
    """Best effort: a missing tool must not kill the watcher."""
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return (p.stdout or "") + (p.stderr or "")
    except Exception as e:                     # noqa: BLE001 - diagnostics
        return f"[{cmd}: {e}]\n"


def snapshot(iface):
    """What the box is doing, cheapest first."""
    parts = [
        ("loadavg", run("cat /proc/loadavg")),
        ("top", run("top -b -n1 -w 200 | head -22")),
        # The SENDER's socket is the one that matters, and it uses an
        # EPHEMERAL source port -- grepping ':500x' catches only the
        # subscribers' receive sockets and misses it entirely. That flaw made
        # the first captured episode unable to answer the question the tool
        # exists for. Capture radiod's own sockets, plus every UDP socket
        # with anything queued to send.
        ("radiod sockets", run("ss -uanpm 2>/dev/null | grep -A1 radiod "
                               "| head -30")),
        ("udp sockets with a send backlog",
         run("ss -uanm 2>/dev/null | awk '/^UNCONN|^ESTAB/{h=$0} "
             "/skmem/{if ($0 ~ /,t[1-9]/) print h ORS $0}' | head -30")),
        ("rtp sockets", run("ss -uanm 2>/dev/null | grep -A1 ':500[0-9]' "
                            "| head -30")),
        ("net dev", run("cat /proc/net/dev")),
        # `ip -s link` does NOT include qdisc drops -- they live one layer up,
        # and reading TX "drop 0" as "the transmit path is clean" cost a full
        # day of misdiagnosis. Capture both.
        ("qdisc", run("for i in $(ls /sys/class/net | grep -v lo); do "
                      "tc -s qdisc show dev $i 2>/dev/null | head -3; done")),
        # If a bridge carries the multicast, its snooping database decides
        # whether frames reach the physical port at all. With
        # multicast_snooping=1 and multicast_querier=0 these entries age out
        # (membership interval 260 s) and forwarding silently stops.
        ("bridge mdb (IPv4 groups)",
         run("bridge mdb show 2>/dev/null | grep -E 'grp 2(2[4-9]|3[0-9])\\.' "
             "| head -20")),
        ("bridge multicast config",
         run("for b in $(ls /sys/class/net/*/bridge -d 2>/dev/null); do "
             "echo \"${b%/bridge}:\"; for f in multicast_snooping "
             "multicast_querier multicast_membership_interval; do "
             "printf '  %s=%s\\n' $f $(cat $b/$f 2>/dev/null); done; done")),
    ]
    if iface:
        parts.append((f"link {iface}", run(f"ip -s link show {iface}")))
        parts.append((f"pause {iface}",
                      run(f"ethtool -S {iface} 2>/dev/null | "
                          f"grep -iE 'pause|drop|err|miss|fifo' | "
                          f"grep -v ': 0$'")))
    return parts


def write(fh, title, parts):
    fh.write(f"\n{'=' * 70}\n{title}\n{'=' * 70}\n")
    for name, text in parts:
        fh.write(f"\n--- {name} ---\n{text.rstrip()}\n")
    fh.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--counter", default="SndbufErrors",
                    help="SndbufErrors (sender) or RcvbufErrors (receiver)")
    ap.add_argument("--proto", default="Udp")
    ap.add_argument("--snmp", default="/proc/net/snmp",
                    help="override for testing")
    ap.add_argument("--iface",
                    help="NIC to sample link/pause counters on, AND to "
                         "watch for qdisc drops as a second trigger")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--threshold", type=int, default=1,
                    help="drops in one interval, on EITHER watched "
                         "source, that start an episode")
    ap.add_argument("--quiet-intervals", type=int, default=6,
                    help="consecutive intervals with BOTH sources quiet "
                         "that end an episode")
    ap.add_argument("--snapshot-every", type=float, default=60.0,
                    help="seconds between snapshots during an episode")
    ap.add_argument("--out-dir", default="sndbuf-episodes")
    args = ap.parse_args()

    # Two independent triggers, either one can open or extend an episode:
    # the socket-level counter this tool was built around, and (if --iface
    # is given) that interface's qdisc drop count -- a loss point neither
    # UdpSndbufErrors nor UdpRcvbufErrors can see (module docstring has the
    # 2026-09-03 incident that motivated adding it: 38 % of everything
    # radiod sent was dropped by fq_codel while the socket counter it had
    # been watching stayed at 0 for all but the last two minutes).
    sources = [{"label": f"{args.proto}{args.counter}",
               "read": lambda: read_counter(args.snmp, args.proto,
                                             args.counter)}]
    if args.iface:
        sources.append({"label": f"qdisc({args.iface})",
                        "read": lambda: read_qdisc_drops(args.iface)})

    for s in sources:
        s["prev"] = s["read"]()
        if s["prev"] is None:
            print(f"cannot read {s['label']}", file=sys.stderr)
            return 1

    os.makedirs(args.out_dir, exist_ok=True)
    print("watching " + ", ".join(s["label"] for s in sources)
          + f" every {args.interval:g}s; episodes -> {args.out_dir}/",
          flush=True)

    fh = None
    quiet = 0
    started = None
    last_snap = 0.0

    while True:
        time.sleep(args.interval)
        # a source that fails this poll (e.g. tc briefly unavailable) just
        # sits out the interval rather than blocking the other source
        deltas = {}
        for s in sources:
            cur = s["read"]()
            if cur is None:
                continue
            deltas[s["label"]] = cur - s["prev"]
            s["prev"] = cur
        moved = {k: v for k, v in deltas.items() if v >= args.threshold}

        if moved:
            quiet = 0
            if fh is None:                     # episode begins
                started = datetime.datetime.now()
                path = os.path.join(
                    args.out_dir,
                    started.strftime("%Y%m%dT%H%M%S") + ".txt")
                fh = open(path, "w")
                for s in sources:
                    s["at_start"] = s["prev"] - deltas.get(s["label"], 0)
                fh.write(
                    f"episode started {started.isoformat()}\n"
                    + "".join(
                        f"{s['label']} at {s['at_start']}, "
                        f"+{deltas.get(s['label'], 0)} in the first "
                        f"{args.interval:g}s\n" for s in sources))
                print(f"EPISODE started {started.isoformat()} "
                      f"({', '.join(f'{k} +{v}' for k, v in moved.items())})"
                      f" -> {path}", flush=True)
                write(fh, "AT ONSET", snapshot(args.iface))
                last_snap = time.time()
            else:
                fh.write(f"{datetime.datetime.now().isoformat()}  "
                         + ", ".join(f"{k} +{v}" for k, v in deltas.items()
                                     if v) + "\n")
                if time.time() - last_snap >= args.snapshot_every:
                    write(fh, f"DURING ({datetime.datetime.now():%H:%M:%S})",
                          snapshot(args.iface))
                    last_snap = time.time()
        elif fh is not None:
            quiet += 1
            if quiet >= args.quiet_intervals:  # episode ends
                ended = datetime.datetime.now()
                dur = (ended - started).total_seconds()
                write(fh, "AFTER", snapshot(args.iface))
                totals = {s["label"]: s["prev"] - s["at_start"]
                          for s in sources}
                fh.write(
                    f"\nepisode ended {ended.isoformat()}\n"
                    f"duration {dur:.0f}s\n"
                    + "".join(
                        f"{label}: {total:,} drops, "
                        f"{total / max(dur, 1):.0f}/s\n"
                        for label, total in totals.items()))
                fh.close()
                fh = None
                print(f"EPISODE ended, {dur:.0f}s, "
                      + ", ".join(f"{label} {total:,}"
                                  for label, total in totals.items()),
                      flush=True)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
