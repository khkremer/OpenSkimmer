#!/usr/bin/env python3
"""Record raw IQ to a .c64 file -- corpus capture, no DSP.

`openskimmer --record` also writes IQ, but it runs the whole pipeline to do
it, which costs a core. This does nothing but read the source and write the
bytes, so it can run alongside the live skimmer on the same host: a
ka9q-radio channel is multicast, and the backend sets SO_REUSEPORT, so any
number of subscribers coexist.

    tools/record_iq.py --band ka9q@skim-20m.local:14050000/192000 \
        --duration 4200 --out-dir recordings --label cwt20m

Writes recordings/<label>_<YYYYMMDDTHHMMZ>.c64 -- interleaved complex64, the
format `--source file` replays bit-identically.

Size is 8 bytes per sample: 192 kHz is 1.54 MB/s, 5.5 GB/hour. The tool
refuses to start unless the estimated size fits in the free space with a
margin, because filling the disk on the radiod host would take the skimmer
down with it.

Pair a recording with that day's RBN archive
(https://data.reversebeacon.net/rbn_history/YYYYMMDD.zip) and ~150 other
skimmers become consensus ground truth for whatever the recording contains.
"""

from __future__ import annotations

import argparse
import datetime
import os
import shutil
import signal
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openskimmer.main import make_source, parse_band_spec   # noqa: E402

BYTES_PER_SAMPLE = 8            # complex64
FREE_MARGIN = 1.15              # keep 15% headroom over the estimate


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--band", required=True,
                    metavar="[SOURCE[@HOST]:]CENTER[/SPAN]",
                    help="same syntax as openskimmer --band, e.g. "
                         "ka9q@skim-20m.local:14050000/192000")
    ap.add_argument("--duration", type=float, default=3600,
                    help="seconds to record (default 3600)")
    ap.add_argument("--out-dir", default="recordings")
    ap.add_argument("--label", default="iq",
                    help="filename prefix, e.g. cwt20m")
    ap.add_argument("--rtp-port", type=int, default=5004)
    ap.add_argument("--source", default="ka9q")
    ap.add_argument("--sample-rate", type=int, default=192000)
    # accepted so make_source() can be reused unchanged
    for opt, default in (("--host", None), ("--file", None)):
        ap.add_argument(opt, default=default)
    for opt in ("--gain", "--ppm"):
        ap.add_argument(opt, type=float, default=None if opt == "--gain" else 0.0)
    ap.add_argument("--port", type=int, default=8073)
    ap.add_argument("--kiwi-password", default="")
    ap.add_argument("--lna-gain", type=int, default=19)
    ap.add_argument("--hl2-filters", default="n2adr")
    ap.add_argument("--bias-t", action="store_true")
    args = ap.parse_args()

    source_name, host, center, span = parse_band_spec(
        args.band, args.source, args.sample_rate)

    est = args.duration * span * BYTES_PER_SAMPLE
    os.makedirs(args.out_dir, exist_ok=True)
    free = shutil.disk_usage(args.out_dir).free
    if free < est * FREE_MARGIN:
        print(f"refusing to start: need ~{est / 1e9:.1f} GB "
              f"(+15% margin), only {free / 1e9:.1f} GB free in "
              f"{args.out_dir}", file=sys.stderr)
        return 1

    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%MZ")
    path = os.path.join(args.out_dir, f"{args.label}_{stamp}.c64")

    src = make_source(args, source_name, host, center, span)
    src.set_sample_rate(span)
    src.set_center_frequency(center)
    src.connect()
    rate = src.sample_rate or span

    stop = {"now": False}
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.__setitem__("now", True))

    print(f"recording {source_name} {center / 1000:.1f} kHz, {rate} Sa/s "
          f"-> {path} (~{est / 1e9:.1f} GB, {args.duration:.0f} s)",
          flush=True)
    t0 = time.monotonic()
    n = 0
    try:
        with open(path, "wb") as f:
            for block in src.stream_iq():
                f.write(np.asarray(block, dtype=np.complex64).tobytes())
                n += len(block)
                if stop["now"] or time.monotonic() - t0 >= args.duration:
                    break
    finally:
        try:
            src.disconnect()
        except Exception:
            pass
    secs = n / float(rate or 1)
    print(f"wrote {n} samples ({secs:.1f} s of audio, "
          f"{os.path.getsize(path) / 1e9:.2f} GB) to {path}", flush=True)
    # a short file means the stream stalled -- worth a non-zero exit for cron
    return 0 if secs >= 0.9 * args.duration else 2


if __name__ == "__main__":
    sys.exit(main())
