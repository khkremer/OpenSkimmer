#!/usr/bin/env python3
"""Sniff a ka9q-radio (radiod) RTP multicast stream and dump its structure.

Point it at radiod's data multicast group to see the RTP headers and the
IQ payload layout, so the OpenSkimmer ka9q backend can be built against the
real wire format instead of guessing. Read-only: it only joins and listens.

Find radiod's multicast group any of these ways:
  avahi-browse -atr | grep -i rtp        # ka9q announces its streams via mDNS
  grep -i 'data\\|address' /etc/radiod@*.conf
  sudo tcpdump -n -i <iface> udp and multicast   # watch for the group:port

Usage:
  tools/rtp_tap.py --group 239.1.2.3:5004
  tools/rtp_tap.py --group 239.1.2.3:5004 --ssrc 14030000 --bytes 32
  tools/rtp_tap.py --group hf-iq.local:5004      # a name is resolved

Stdlib only, runs with plain python3 on the box that can see the multicast.
"""

import argparse
import socket
import struct
import sys
import time


def parse_rtp(pkt: bytes):
    """RFC 3550 RTP header + payload; returns a dict or None."""
    if len(pkt) < 12:
        return None
    b0, b1 = pkt[0], pkt[1]
    cc = b0 & 0x0F
    seq, ts, ssrc = struct.unpack_from("!HII", pkt, 2)
    off = 12 + cc * 4
    if b0 & 0x10 and len(pkt) >= off + 4:          # extension header
        ext_words = struct.unpack_from("!H", pkt, off + 2)[0]
        off += 4 + ext_words * 4
    return {"ver": b0 >> 6, "pt": b1 & 0x7F, "marker": b1 >> 7,
            "seq": seq, "ts": ts, "ssrc": ssrc, "payload": pkt[off:]}


def _samples_preview(payload: bytes, n: int = 6) -> str:
    """Show the first few int16 pairs both endian ways -- helps spot IQ."""
    m = min(n * 4, len(payload) - len(payload) % 4)
    be = struct.unpack_from(f">{m // 2}h", payload, 0) if m else ()
    le = struct.unpack_from(f"<{m // 2}h", payload, 0) if m else ()
    return f"int16 BE {list(be)}  LE {list(le)}"


def main() -> int:
    ap = argparse.ArgumentParser(description="ka9q-radio RTP multicast sniffer")
    ap.add_argument("--group", required=True, help="MCAST_HOST:PORT")
    ap.add_argument("--iface", default="0.0.0.0",
                    help="local interface IP to join on (default any)")
    ap.add_argument("--ssrc", type=int, help="only dump this SSRC")
    ap.add_argument("--bytes", type=int, default=24,
                    help="payload bytes to hex-dump per packet")
    ap.add_argument("--count", type=int, default=0,
                    help="stop after N dumped packets (0 = forever)")
    args = ap.parse_args()

    host, port = args.group.rsplit(":", 1)
    port = int(port)
    grp = socket.gethostbyname(host)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):          # share the port with other subscribers
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(grp),
                       socket.inet_aton(args.iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(5.0)
    print(f"joined {grp}:{port} on {args.iface}; listening...", flush=True)

    seen = {}          # ssrc -> [count, pt, set(payload lengths)]
    dumped = 0
    last_summary = time.time()
    while True:
        try:
            pkt, _ = sock.recvfrom(65536)
        except socket.timeout:
            print("(no packets in 5 s -- wrong group/port/interface?)",
                  file=sys.stderr, flush=True)
            continue
        r = parse_rtp(pkt)
        if r is None:
            continue
        s = seen.setdefault(r["ssrc"], [0, r["pt"], set()])
        s[0] += 1
        s[2].add(len(r["payload"]))

        if not args.ssrc or r["ssrc"] == args.ssrc:
            pl = r["payload"][:args.bytes]
            print(f"ssrc={r['ssrc']} (0x{r['ssrc']:08x}) pt={r['pt']} "
                  f"seq={r['seq']} ts={r['ts']} plen={len(r['payload'])}\n"
                  f"    hex {pl.hex(' ', 2)}\n"
                  f"    {_samples_preview(r['payload'])}", flush=True)
            dumped += 1
            if args.count and dumped >= args.count:
                break

        if time.time() - last_summary > 3.0:
            print("--- streams: " + "; ".join(
                f"ssrc {k} (0x{k:08x}) pt={v[1]} x{v[0]} plen={sorted(v[2])}"
                for k, v in sorted(seen.items())), flush=True)
            last_summary = time.time()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        pass
