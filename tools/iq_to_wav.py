#!/usr/bin/env python3
"""Convert a .c64 IQ recording to an I/Q WAV that CW Skimmer can open.

`record_iq.py` writes raw interleaved complex64, which nothing else reads.
CW Skimmer takes a stereo WAV carrying I in the left channel and Q in the
right, so this is what lets the same audio be graded through both skimmers
against one RBN archive -- a far cleaner comparison than running them on
different days.

    tools/iq_to_wav.py recordings/cwt20m_20260812T1855Z.c64
    tools/iq_to_wav.py IN.c64 --decimate 2 --freq 14050000 --out cwt_96k.wav

Give --freq: without the QRG tag CW Skimmer spots offsets from the centre,
and without QTR (taken from the file name) its clock starts at 0000Z.

Sizing matters here: 70 minutes at 192 kHz is 3.2 GB as 16-bit stereo, and
WAV cannot exceed 4 GB. --decimate 2 halves it to 96 kHz / 1.6 GB, which
still spans +/-45 kHz after the anti-alias filter -- the whole CW segment
either side of a 14050 centre. CW Skimmer's own recorder never writes more
than 2 GB, so stay under that.

The name is preserved (`<label>_<YYYYMMDDTHHMMZ>.wav`) so the start time
stays recoverable, as it is for the .c64.
"""

import argparse
import os
import re
import struct
import sys
from datetime import datetime, timedelta

import numpy as np

BLOCK = 1 << 20          # complex samples per chunk


def peak_of(path, sample_bytes=8, probes=24):
    """Scan the file at intervals for a peak, without reading it all."""
    n = os.path.getsize(path) // sample_bytes
    peak = 0.0
    with open(path, "rb") as f:
        for k in range(probes):
            f.seek((n // probes) * k * sample_bytes)
            x = np.fromfile(f, dtype=np.complex64, count=1 << 16)
            if x.size:
                peak = max(peak, float(np.abs(x).max()))
    return peak


def lowpass_taps(factor, taps=255, edge=0.47):
    """Kaiser-windowed sinc low-pass for decimation by `factor`.

    The 6 dB point sits at `edge` of the output rate (0.47 x 96 kHz = 45 kHz
    each side) and the stopband (~80 dB) starts about 4 kHz further out, so
    what folds back lands within the outer 3 kHz of the band. The boxcar this
    replaced was only 3 dB down at the output's edge: at 192 -> 96 kHz,
    14100-14146 kHz (digital modes, beacons) folded onto 14004-14050 at about
    -6 to -9 dB, right on top of the CW segment.
    """
    n = np.arange(taps) - (taps - 1) / 2.0
    fc = edge / factor              # cycles per input sample
    h = 2 * fc * np.sinc(2 * fc * n) * np.kaiser(taps, 8.0)
    return (h / h.sum()).astype(np.complex64)


class Decimator:
    """Overlap-save FFT filter, then every `factor`-th sample, across chunks."""

    def __init__(self, factor):
        self.factor = factor
        self.h = lowpass_taps(factor) if factor > 1 else None
        self.tail = np.zeros(0 if factor == 1 else self.h.size - 1, dtype=np.complex64)
        self.phase = 0              # input index (mod factor) of the next output

    def __call__(self, x):
        if self.factor == 1:
            return x
        x = np.concatenate([self.tail, x])
        m = self.h.size
        if x.size < m:
            self.tail = x
            return np.empty(0, dtype=np.complex64)
        nfft = 1 << int(np.ceil(np.log2(x.size + m - 1)))
        y = np.fft.ifft(np.fft.fft(x, nfft) * np.fft.fft(self.h, nfft))[m - 1:x.size]
        self.tail = x[x.size - (m - 1):]
        out = y[self.phase::self.factor].astype(np.complex64)
        self.phase = (self.phase - y.size) % self.factor
        return out


def info_chunk(tags):
    """A RIFF LIST/INFO chunk; CW Skimmer reads QRG, QTR and CHAN from it.

    Chunk IDs are four bytes, so the three-letter tags are space-padded.
    """
    body = b"INFO"
    for key, val in tags.items():
        v = val.encode("ascii") + b"\0"
        if len(v) % 2:
            v += b"\0"
        body += key.ljust(4).encode("ascii") + struct.pack("<I", len(v)) + v
    return b"LIST" + struct.pack("<I", len(body)) + body


def start_from_name(path):
    """The recording's UTC start from a <label>_<YYYYMMDDTHHMMZ> name, or None."""
    m = re.search(r"(\d{8}T\d{4})Z", os.path.basename(path))
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M") if m else None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("infile")
    ap.add_argument("--out", help="default: same name with .wav")
    ap.add_argument("--sample-rate", type=int, default=192000)
    ap.add_argument("--decimate", type=int, default=1,
                    help="integer factor; 2 turns 192 kHz into 96 kHz")
    ap.add_argument("--headroom-db", type=float, default=6.0,
                    help="peak backoff from full scale")
    ap.add_argument("--freq", type=int,
                    help="centre frequency, Hz, for the QRG tag (CW Skimmer then "
                         "shows real frequencies instead of offsets)")
    ap.add_argument("--start", type=float, default=0.0,
                    help="seconds into the recording to start at")
    ap.add_argument("--duration", type=float,
                    help="seconds to convert (default: to the end)")
    args = ap.parse_args()

    out = args.out or os.path.splitext(args.infile)[0] + ".wav"
    rate = args.sample_rate // args.decimate
    skip = int(args.start * args.sample_rate)
    total = os.path.getsize(args.infile) // 8 - skip
    if args.duration:
        total = min(total, int(args.duration * args.sample_rate))
    est = total // args.decimate * 4
    print(f"{args.infile} -> {out}")
    print(f"  {total} samples, {args.sample_rate} Hz -> {rate} Hz, "
          f"~{est / 1e9:.2f} GB")
    if est > 2 << 30:
        print("  WARNING: over 2 GB, which CW Skimmer's recorder never "
              "writes; use --decimate or --duration", file=sys.stderr)

    peak = peak_of(args.infile)
    if peak <= 0:
        print("  file appears to be silence", file=sys.stderr)
        return 2
    scale = (10 ** (-args.headroom_db / 20.0)) * 32767.0 / peak
    print(f"  peak {peak:.4f} -> scale {scale:.1f}")

    tags = {"CHAN": "I/Q", "ISFT": "OpenSkimmer iq_to_wav.py"}
    if args.freq:
        tags["QRG"] = str(args.freq)
    t0 = start_from_name(args.infile)
    if t0:
        t0 += timedelta(seconds=args.start)
        tags["QTR"] = t0.strftime("%Y-%m-%d %H:%M:%S")
    info = info_chunk(tags)

    written, clipped = 0, 0
    dec = Decimator(args.decimate)
    with open(args.infile, "rb") as f, open(out, "wb") as w:
        # RIFF, fmt, LIST/INFO, data; the two sizes are patched at the end
        w.write(b"RIFF\0\0\0\0WAVE")
        w.write(b"fmt " + struct.pack("<IHHIIHH", 16, 1, 2, rate, rate * 4, 4, 16))
        w.write(info)
        w.write(b"data\0\0\0\0")
        f.seek(skip * 8)
        left = total
        while left > 0:
            x = np.fromfile(f, dtype=np.complex64, count=min(BLOCK, left))
            if x.size == 0:
                break
            left -= x.size
            y = dec(x)
            if y.size == 0:
                continue
            iq = np.empty(y.size * 2, dtype=np.float32)
            iq[0::2] = y.real * scale
            iq[1::2] = y.imag * scale
            clipped += int(np.count_nonzero(np.abs(iq) > 32767))
            w.write(np.clip(iq, -32768, 32767).astype("<i2").tobytes())
            written += y.size
        data_bytes = written * 4
        w.seek(4)
        w.write(struct.pack("<I", 4 + 24 + len(info) + 8 + data_bytes))
        w.seek(12 + 24 + len(info) + 4)
        w.write(struct.pack("<I", data_bytes))
    print(f"  wrote {written} frames ({written / rate:.1f} s), "
          f"{clipped} clipped values, tags {tags}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
