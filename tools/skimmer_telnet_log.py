#!/usr/bin/env python3
"""Log spots from another skimmer's telnet server, for side-by-side grading.

CW Skimmer serves spots on a telnet port (default 7300) in DX-cluster
format -- that is how the RBN Aggregator harvests them. Pointing this at it
captures the same stream to a file, so the same recording can be graded
through both skimmers against one RBN archive.

    tools/skimmer_telnet_log.py --host 127.0.0.1 --port 7300 \\
        --out cwskimmer_1855Z.log --media-epoch 2026-08-12T18:55

THE TIMESTAMP TRAP: replaying a recording makes a skimmer stamp its spots
with the clock of the machine replaying it, not the time the audio was
recorded, so the spots appear to be from today and match nothing in an
archive. OpenSkimmer solves this internally with a media clock;
CW Skimmer has no such notion. So pass --media-epoch (the recording's true
UTC start) and start this logger at the same moment you start playback: each
line is then written with a corrected UTC time as well as the raw one.

If playback is not real-time, pass --rate (e.g. 2.0 for double speed) so the
correction scales with it. CW Skimmer plays I/Q files at 1:1, so the default
of 1.0 is normally right.

Output: one line per spot,

    2026-08-12 18:57:03Z | <the skimmer's own line as received>
"""

import argparse
import datetime
import socket
import sys
import time


def stamp_now(epoch, t0, rate):
    """UTC string for right now, mapped back into recording time."""
    if epoch is None:
        return datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    t = epoch + (time.time() - t0) * rate
    return datetime.datetime.fromtimestamp(
        t, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def parse_epoch(text):
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y%m%dT%H%M%S", "%Y%m%dT%H%M"):
        try:
            return datetime.datetime.strptime(text, fmt).replace(
                tzinfo=datetime.timezone.utc).timestamp()
        except ValueError:
            continue
    raise SystemExit(f"cannot parse --media-epoch {text!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=7300)
    ap.add_argument("--out", required=True)
    ap.add_argument("--media-epoch",
                    help="true UTC start of the recording being replayed; "
                         "start this logger when playback starts")
    ap.add_argument("--rate", type=float, default=1.0,
                    help="playback speed multiplier (1.0 = real time)")
    ap.add_argument("--login", default="",
                    help="YOUR CALLSIGN. Both CW Skimmer and Skimmer Server "
                         "prompt for one on the cluster port and send NOTHING "
                         "until they get it -- omitting this looks exactly "
                         "like a skimmer that is decoding nothing")
    ap.add_argument("--quiet-warn", type=float, default=90.0,
                    help="warn if no spot line has arrived in this many "
                         "seconds (0 disables)")
    args = ap.parse_args()

    epoch = parse_epoch(args.media_epoch) if args.media_epoch else None
    t0 = time.time()

    s = socket.create_connection((args.host, args.port), timeout=30)
    print(f"connected to {args.host}:{args.port}; logging to {args.out}",
          file=sys.stderr)
    if not args.login:
        print("WARNING: no --login. These servers prompt for a callsign and "
              "stay silent until answered; you will capture the banner and "
              "nothing else.", file=sys.stderr)
    else:
        s.sendall((args.login + "\r\n").encode())

    n, spots, last_spot = 0, 0, time.time()
    buf = b""
    logged_in = bool(args.login)
    with open(args.out, "a", encoding="utf-8") as out:
        while True:
            try:
                s.settimeout(5.0)
                try:
                    chunk = s.recv(4096)
                except socket.timeout:
                    # a prompt often arrives with NO trailing newline, so it
                    # never reaches the line loop below; answer it from the
                    # partial buffer instead of waiting forever
                    low = buf.lower()
                    if not logged_in and args.login and (
                            b"callsign" in low or b"login" in low
                            or low.rstrip().endswith(b":")):
                        s.sendall((args.login + "\r\n").encode())
                        logged_in = True
                    if (args.quiet_warn and not spots
                            and time.time() - last_spot > args.quiet_warn):
                        print("WARNING: connected but no spot lines yet -- is "
                              "playback running, and did the login succeed?",
                              file=sys.stderr)
                        last_spot = time.time()
                    continue
            except (OSError, KeyboardInterrupt):
                break
            if not chunk:
                break
            buf += chunk
            if not logged_in and args.login and (
                    b"callsign" in buf.lower() or b"login" in buf.lower()):
                s.sendall((args.login + "\r\n").encode())
                logged_in = True
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").rstrip("\r")
                if not line.strip():
                    continue
                # The login prompt arrives with no trailing newline, so the
                # first spot is appended straight onto it ("...callsign: DX
                # de ..."). Split it off, or that spot is lost and the line
                # will not parse.
                cut = line.find("DX de")
                if cut > 0:
                    head, line = line[:cut].rstrip(), line[cut:]
                    if head:
                        out.write(f"{stamp_now(epoch, t0, args.rate)} "
                                  f"| {head}\n")
                        n += 1
                stamp = stamp_now(epoch, t0, args.rate)
                out.write(f"{stamp} | {line}\n")
                out.flush()
                n += 1
                if line.startswith("DX de"):
                    spots += 1
                    last_spot = time.time()
                if n % 25 == 0:
                    print(f"  {n} lines, {spots} spots", file=sys.stderr)
    print(f"done, {n} lines ({spots} spots) -> {args.out}", file=sys.stderr)
    if not spots:
        print("NO SPOTS CAPTURED. Check: the login was accepted, playback is "
              "actually running, and this is the port the skimmer doing the "
              "playback serves on (you appear to run both CW Skimmer and "
              "Skimmer Server -- they cannot share a port).", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
