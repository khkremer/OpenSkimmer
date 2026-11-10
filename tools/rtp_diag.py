#!/usr/bin/env python3
"""One command that answers 'are the packets actually being sent?'

Written after a day of diagnosing RTP packet loss from subscriber symptoms
alone, in which seven plausible mechanisms were each proposed and then
refuted by measurement: untuned socket buffers, a missing IGMP querier, IGMP
query timing, scheduled jobs, ethernet flow control, USB device contention,
and SO_REUSEPORT. What was never measured is the one fact that halves the
search space:

    ARE THE RTP SEQUENCE NUMBERS ON THE WIRE CONTIGUOUS?

Contiguous means nothing is being lost and the LOSS ACCOUNTING is wrong.
Gapped means radiod is not producing them, and no receiver-side change can
help. Everything else is downstream of that answer.

    tools/rtp_diag.py                 # find groups from the running radiod
    tools/rtp_diag.py --group 239.87.17.228 --seconds 20
    tools/rtp_diag.py --out /tmp/rtp_diag.txt

The tap deliberately does NOT set SO_REUSEPORT. On Linux that option makes
the kernel load-balance a multicast group across the sockets sharing its
port, so a tap that sets it would join the skimmer's group and perturb the
very thing being measured (tools/rtp_tap.py has this problem).

Also collects what radiod itself says, which subscriber-side debugging never
looks at: "Temporary send failure" means its send buffer overflowed, and a
variable "RX888 measured sample rate error" means dropped USB samples --
never a clock error, so never fix that with --ppm.
"""

import argparse
import collections
import os
import re
import socket
import struct
import subprocess
import sys
import time


def sh(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        return ((p.stdout or "") + (p.stderr or "")).strip()
    except Exception as e:                       # noqa: BLE001
        return f"[{cmd}: {e}]"


def running_config():
    """The .conf of the radiod instance systemd currently has running."""
    units = sh("systemctl list-units 'radiod@*' --all --no-legend "
               "--plain 2>/dev/null")
    for line in units.splitlines():
        unit = line.split()[0] if line.split() else ""
        m = re.match(r"radiod@(.+?)\.service$", unit)
        if not m:
            continue
        name = m.group(1)
        if name.endswith(".conf"):               # the doubled-suffix trap
            print(f"WARNING: unit radiod@{name} has .conf in the INSTANCE "
                  f"name; it can never load a config. Check its restart "
                  f"counter.", file=sys.stderr)
            continue
        for path in (f"/etc/radio/radiod@{name}.conf", f"/etc/radio/{name}.conf"):
            if os.path.exists(path):
                return path, unit
    return None, None


def groups_from(path):
    """-> [(section, group_name, freq_hz)] from a radiod config."""
    out, sec, data, freq = [], None, None, None
    with open(path, errors="replace") as f:
        for line in f:
            line = line.split("#")[0].strip()
            if line.startswith("["):
                if sec and data:
                    out.append((sec, data, freq))
                sec, data, freq = line.strip("[]"), None, None
            elif "=" in line:
                k, v = (x.strip() for x in line.split("=", 1))
                if k == "data":
                    data = v
                elif k == "freq":
                    try:
                        freq = int(float(v))
                    except ValueError:
                        pass
    if sec and data:
        out.append((sec, data, freq))
    return out


def tap(group, port, seconds, iface="0.0.0.0"):
    """Join read-only and measure per-SSRC rate and sequence gaps."""
    try:
        addr = socket.gethostbyname(group)
    except OSError as e:
        return f"  cannot resolve {group}: {e}", {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # NO SO_REUSEPORT -- see the module docstring.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
    # Bind the GROUP address rather than INADDR_ANY: a skimmer already holds
    # 0.0.0.0:port, and since this tap deliberately omits SO_REUSEPORT an
    # INADDR_ANY bind collides with it. Binding the group also means only
    # that group's traffic reaches this socket.
    note = ""
    for bind_addr in (addr, ""):
        try:
            s.bind((bind_addr, port))
            break
        except OSError as e:
            last = e
    else:
        s.close()
        return f"  cannot bind {port}: {last}", {}
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(addr),
                                 socket.inet_aton(iface)))
    except OSError as e:
        s.close()
        return f"  cannot join {addr}: {e}", {}
    s.settimeout(1.0)
    seen, gaps, last = collections.Counter(), collections.Counter(), {}
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            pkt, _ = s.recvfrom(65536)
        except socket.timeout:
            continue
        if len(pkt) < 12:
            continue
        seq, _ts, ssrc = struct.unpack_from("!HII", pkt, 2)
        seen[ssrc] += 1
        if ssrc in last:
            d = (seq - last[ssrc] - 1) & 0xFFFF
            if 0 < d < 1000:
                gaps[ssrc] += d
        last[ssrc] = seq
    s.close()
    el = max(time.time() - t0, 0.001)
    if not seen:
        return (f"  NOTHING RECEIVED in {el:.0f}s -- wrong group, or radiod "
                f"is not sending"), {}
    lines = []
    for ssrc, n in seen.most_common():
        g = gaps[ssrc]
        pct = 100.0 * g / max(1, n + g)
        lines.append(f"  SSRC {ssrc:>9} = {ssrc / 1000.0:9.3f} MHz  "
                     f"{n:6} pkts {n / el:7.1f}/s  gaps {g:6}  "
                     f"{pct:5.1f}% MISSING ON THE WIRE")
    return "\n".join(lines), {k: (seen[k], gaps[k]) for k in seen}


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--group", action="append",
                    help="repeatable; default: read the running radiod config")
    ap.add_argument("--port", type=int, default=5004)
    ap.add_argument("--seconds", type=float, default=15.0)
    ap.add_argument("--iface", default="0.0.0.0")
    ap.add_argument("--out", help="also write everything to this file")
    args = ap.parse_args()

    buf = []

    def emit(s=""):
        print(s, flush=True)
        buf.append(s)

    emit(f"=== rtp_diag {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} "
         f"on {socket.gethostname()} ===")

    groups = [(g, g, None) for g in (args.group or [])]
    if not groups:
        cfg, unit = running_config()
        if cfg:
            emit(f"\nrunning radiod: {unit}  config {cfg}")
            groups = groups_from(cfg)
        else:
            emit("\nno running radiod config found; pass --group")

    emit(f"\n--- sequence on the wire ({args.seconds:.0f}s per group) ---")
    verdicts = []
    for sec, grp, freq in groups:
        emit(f"\n[{sec}] {grp}" + (f"  freq {freq}" if freq else ""))
        text, per = tap(grp, args.port, args.seconds, args.iface)
        emit(text)
        for n, g in per.values():
            verdicts.append(100.0 * g / max(1, n + g))

    emit("\n--- what radiod says (never Started/Git/Copyright) ---")
    emit(sh("journalctl -u 'radiod@*' --since '20 min ago' --no-pager 2>/dev/null "
            "| grep -viE 'started|git |copyright|scheduled restart' | tail -25")
         or "  (nothing)")

    emit("\n--- sockets and counters ---")
    emit(sh(f"ss -uanm 2>/dev/null | grep -A1 ':{args.port}' | head -30"))
    emit(sh("nstat -az 2>/dev/null | grep -E "
            "'Udp(SndbufErrors|RcvbufErrors|InErrors)'"))
    emit(sh("cat /proc/loadavg"))
    emit(sh("top -b -n1 -w 200 2>/dev/null | head -12"))

    if verdicts:
        worst = max(verdicts)
        emit("\n=== VERDICT ===")
        if worst < 1.0:
            emit(f"  Sequence is CONTIGUOUS on the wire (worst {worst:.1f}% "
                 f"missing).\n  radiod is sending everything. If a subscriber "
                 f"reports loss, the packets\n  are arriving and being lost "
                 f"or MISCOUNTED locally -- look at socket\n  delivery and at "
                 f"the loss accounting, not at the network.")
        else:
            emit(f"  Sequence is GAPPED on the wire ({worst:.1f}% missing).\n"
                 f"  The packets are not being produced or not reaching this "
                 f"host. No\n  receiver-side change can help; look at radiod "
                 f"and the path.")
    if args.out:
        with open(args.out, "w") as f:
            f.write("\n".join(buf) + "\n")
        print(f"\nwritten to {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
