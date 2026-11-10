#!/usr/bin/env python3
"""Collect everything needed to diagnose OpenSkimmer/radiod packet loss.

ONE self-contained file, stdlib only. Copy it to any host in the chain --
sender or receiver -- run it, and send the file it writes. Nothing to paste,
no repo checkout needed, no arguments required.

    scp tools/site_diag.py radiohost:
    ssh radiohost 'python3 site_diag.py'      # prints the path it wrote

Written after a day in which seven mechanisms for the same packet loss were
each proposed from indirect evidence and each refuted: untuned socket
buffers, a missing IGMP querier, IGMP query timing, scheduled jobs, ethernet
flow control, USB device contention, and SO_REUSEPORT. The cost was not the
wrong guesses, it was gathering one number at a time over hours. This takes
every measurement at once, from the same moment, so the picture is coherent.

The measurement that matters most, and which took longest to get to:

    ARE THE RTP SEQUENCE NUMBERS ON THE WIRE CONTIGUOUS?

Contiguous means nothing is lost and the loss ACCOUNTING is wrong. Gapped
means radiod is not producing them and no receiver-side change can help.
Run it while the loss is happening, or the answer is about a healthy system.
"""

import argparse
import collections
import datetime
import os
import re
import socket
import struct
import subprocess
import sys
import time

OUT = []


def w(s=""):
    OUT.append(s)


def sh(label, cmd, timeout=25):
    w(f"\n--- {label} ---")
    w(f"$ {cmd}")
    try:
        p = subprocess.run(cmd, shell=True, capture_output=True,
                           text=True, timeout=timeout)
        t = ((p.stdout or "") + (p.stderr or "")).rstrip()
    except Exception as e:                        # noqa: BLE001
        t = f"[failed: {e}]"
    w(t if t else "(no output)")
    return t


# ---------------------------------------------------------------- RTP tap

def tap(addr, port, seconds, iface="0.0.0.0"):
    """Join read-only; return per-SSRC (packets, sequence gaps)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Deliberately NO SO_REUSEPORT: on Linux it makes the kernel load-balance
    # a multicast group across the sockets sharing the port, so a tap setting
    # it would join the skimmer's group and steal what it means to observe.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
    err = None
    # Bind the GROUP, not INADDR_ANY: a running skimmer already holds
    # 0.0.0.0:port and without SO_REUSEPORT that bind collides.
    for b in (addr, ""):
        try:
            s.bind((b, port))
            err = None
            break
        except OSError as e:
            err = e
    if err:
        s.close()
        return None, f"cannot bind {port}: {err}"
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                     struct.pack("4s4s", socket.inet_aton(addr),
                                 socket.inet_aton(iface)))
    except OSError as e:
        s.close()
        return None, f"cannot join {addr}: {e}"
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
    return {k: (seen[k], gaps[k], el) for k in seen}, None


def radiod_units():
    out = subprocess.run("systemctl list-units 'radiod@*' --all --no-legend "
                         "--plain 2>/dev/null", shell=True,
                         capture_output=True, text=True).stdout
    units = []
    for line in out.splitlines():
        f = line.split()
        if f and f[0].startswith("radiod@"):
            units.append(f[0])
    return units


def config_for(unit):
    m = re.match(r"radiod@(.+?)\.service$", unit)
    if not m:
        return None
    name = m.group(1)
    for p in (f"/etc/radio/radiod@{name}.conf", f"/etc/radio/{name}.conf"):
        if os.path.exists(p):
            return p
    return None


def groups_from(path):
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


# ------------------------------------------------------- loss episodes

TS = re.compile(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})[,.](\d{1,6})")
LOST = re.compile(r"(\d+)\s+RTP packet\(s\) lost")


def episodes(since="2 days ago", gap=60.0):
    p = subprocess.run(f"journalctl -u openskimmer --since '{since}' "
                       f"--no-pager 2>/dev/null", shell=True,
                       capture_output=True, text=True, timeout=300)
    ev = []
    for line in p.stdout.splitlines():
        m = LOST.search(line)
        t = TS.search(line)
        if not (m and t):
            continue
        ts = datetime.datetime.strptime(t.group(1).replace("T", " "),
                                        "%Y-%m-%d %H:%M:%S")
        ev.append((ts, int(m.group(1))))
    if not ev:
        return "no 'RTP packet(s) lost' lines in the window"
    ev.sort()
    eps = []
    for ts, n in ev:
        if eps and (ts - eps[-1][-1][0]).total_seconds() <= gap:
            eps[-1].append((ts, n))
        else:
            eps.append([(ts, n)])
    lines = [f"{len(ev)} reports, {sum(n for _, n in ev):,} packets, "
             f"{len(eps)} episodes (gap {gap:.0f}s)",
             f"{'start':<21}{'end':<21}{'dur':>10}{'packets':>12}"]
    for e in eps[-15:]:
        d = (e[-1][0] - e[0][0]).total_seconds()
        lines.append(f"{e[0][0]:%m-%d %H:%M:%S}    {e[-1][0]:%m-%d %H:%M:%S}"
                     f"{d / 3600:>9.2f}h{sum(n for _, n in e):>12,}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=12.0,
                    help="tap duration per multicast group")
    ap.add_argument("--max-groups", type=int, default=4)
    ap.add_argument("--port", type=int, default=5004)
    ap.add_argument("--group", action="append",
                    help="tap these instead of reading the radiod config")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    host = socket.gethostname()
    out = args.out or f"/tmp/site_diag_{host}_{time.strftime('%Y%m%dT%H%M%S')}.txt"

    w(f"=== site_diag on {host} at "
      f"{time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())} ===")
    sh("uptime / load", "uptime; cat /proc/loadavg")

    # --- radiod units, and the doubled-.conf trap -----------------------
    units = radiod_units()
    w("\n--- radiod units ---")
    for u in units:
        name = re.sub(r"^radiod@|\.service$", "", u)
        rc = subprocess.run(f"systemctl show -p NRestarts --value {u} "
                            f"2>/dev/null", shell=True, capture_output=True,
                            text=True).stdout.strip()
        flag = ""
        if name.endswith(".conf"):
            flag = ("   <<< BROKEN: '.conf' in the INSTANCE name means radiod "
                    "looks for NAME.conf.conf and can never start")
        w(f"  {u}   restarts={rc or '?'}{flag}")
    if not units:
        w("  (none -- this host is a receiver only)")

    # --- what to tap ----------------------------------------------------
    groups = [(g, g, None) for g in (args.group or [])]
    cfg = None
    if not groups:
        for u in units:
            if re.sub(r"^radiod@|\.service$", "", u).endswith(".conf"):
                continue
            cfg = config_for(u)
            if cfg:
                w(f"\nreading groups from {cfg}")
                groups = groups_from(cfg)
                break
    if not groups:
        t = sh("multicast groups joined", "ip maddr show 2>/dev/null "
               "| awk '/inet /{print $2}' | grep -E '^23[2-9]\\.'")
        groups = [(f"joined-{i}", g, None)
                  for i, g in enumerate(t.split()) if g]

    w(f"\n--- RTP SEQUENCE ON THE WIRE ({args.seconds:.0f}s per group, "
      f"tap has NO SO_REUSEPORT) ---")
    worst = None
    for sec, grp, freq in groups[:args.max_groups]:
        try:
            addr = socket.gethostbyname(grp)
        except OSError as e:
            w(f"\n[{sec}] {grp}: cannot resolve ({e})")
            continue
        w(f"\n[{sec}] {grp} -> {addr}" + (f"  freq={freq}" if freq else ""))
        per, err = tap(addr, args.port, args.seconds)
        if err:
            w(f"  {err}")
            continue
        if not per:
            w(f"  NOTHING RECEIVED in {args.seconds:.0f}s -- radiod not "
              f"sending to this group, or wrong interface")
            continue
        for ssrc, (n, g, el) in sorted(per.items(), key=lambda kv: -kv[1][0]):
            pct = 100.0 * g / max(1, n + g)
            worst = pct if worst is None else max(worst, pct)
            w(f"  SSRC {ssrc:>9} = {ssrc / 1000.0:9.3f} MHz  {n:6} pkts "
              f"{n / el:7.1f}/s  gaps {g:6}   {pct:5.1f}% MISSING ON WIRE")

    sh("radiod's own log (banners filtered)",
       "journalctl -u 'radiod@*' --since '30 min ago' --no-pager 2>/dev/null "
       "| grep -viE 'started|git |copyright|scheduled restart' | tail -30")

    w("\n--- openskimmer loss episodes (last 2 days) ---")
    w(episodes())

    sh("sockets", f"ss -uanm 2>/dev/null | grep -A1 ':{args.port}' | head -40")
    sh("udp counters", "nstat -az 2>/dev/null | grep -E "
                       "'Udp(SndbufErrors|RcvbufErrors|InErrors|NoPorts)'")
    sh("socket sysctls", "sysctl net.core.rmem_max net.core.wmem_max "
                         "net.core.rmem_default net.core.wmem_default "
                         "2>/dev/null")
    sh("interfaces", "ip -br addr")
    sh("link stats", "for i in $(ls /sys/class/net | grep -v lo); do "
                     "echo \"== $i\"; ip -s link show $i | tail -4; done")
    sh("pause frames / nic errors",
       "for i in $(ls /sys/class/net | grep -v lo); do echo \"== $i\"; "
       "ethtool -S $i 2>/dev/null | grep -iE 'pause|drop|err|miss|fifo' "
       "| grep -v ': 0$'; done")
    sh("multicast groups", "ip maddr show 2>/dev/null | grep -E 'inet |^[0-9]'")
    sh("top", "top -b -n1 -w 200 2>/dev/null | head -18")
    sh("openskimmer command line",
       "ps -o command= -p $(systemctl show -p MainPID --value openskimmer "
       "2>/dev/null) 2>/dev/null")

    if worst is not None:
        w("\n=== VERDICT ===")
        if worst < 1.0:
            w(f"  CONTIGUOUS on the wire (worst {worst:.1f}% missing).")
            w("  radiod is sending everything. Any subscriber reporting loss")
            w("  is losing or MISCOUNTING it locally -- look at socket")
            w("  delivery and at the loss accounting, not at the network.")
        else:
            w(f"  GAPPED on the wire ({worst:.1f}% missing).")
            w("  The packets are not produced, or not reaching this host.")
            w("  No receiver-side change can help; look at radiod and the path.")
    else:
        w("\n=== VERDICT ===\n  No RTP seen -- could not measure. Check the "
          "group/interface, and run this WHILE loss is happening.")

    text = "\n".join(OUT) + "\n"
    with open(out, "w") as f:
        f.write(text)
    # Only the verdict goes to the terminal -- the point of this tool is that
    # the operator sends ONE file instead of copying command output around.
    for line in OUT:
        if line.startswith("=== VERDICT") or line.startswith("  "):
            pass
    tail = text.split("=== VERDICT ===")
    print("=== VERDICT ===" + tail[-1] if len(tail) > 1 else "(no verdict)")
    print(f">>> full report written to {out}")
    print(f">>> send that file; it is {len(text.splitlines())} lines, "
          f"{len(text)} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
