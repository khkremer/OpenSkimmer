#!/usr/bin/env python3
"""Measure a SoapySDR device's frequency error against a known carrier.

Tune to a frequency standard (WWV 10 or 15 MHz, CHU 14.670 MHz) and this
prints where its carrier actually lands -- the offset in Hz and the implied
ppm -- so you can set --ppm on the skimmer. A single ham CW spot can't give
this: it mixes real clock error with decoder bin scatter; a steady standard
carrier isolates the clock error.

The reading is the MEDIAN over several ~1 s windows, so an occasional
window contaminated by WWV modulation (tones/voice) or co-channel WWVH
can't throw the number; the spread across windows is printed too. Let the
device warm up ~10 min first -- a cheap TCXO drifts while heating.

Usage:
    tools/measure_ppm.py [ref_hz] [driver] [device_args]
    tools/measure_ppm.py                 # WWV 15 MHz, driver=sdrplay
    tools/measure_ppm.py 10000000        # WWV 10 MHz (often cleaner)

Run it in the venv that can import SoapySDR, on the host with the device.
"""

import sys

import numpy as np

REF_HZ = int(float(sys.argv[1])) if len(sys.argv) > 1 else 15_000_000
DRIVER = sys.argv[2] if len(sys.argv) > 2 else "sdrplay"
DEV_ARGS = sys.argv[3] if len(sys.argv) > 3 else ""
RATE = 192000
WINDOW_S = 1.0          # per-measurement integration
WINDOWS = 8            # median over this many windows
SEARCH_HZ = 3000        # look for the carrier within +/- this of center


def carrier_offset_hz(x: np.ndarray) -> float:
    """Offset of the strongest carrier near center, sub-bin refined."""
    spec = np.abs(np.fft.fftshift(np.fft.fft(x * np.hanning(len(x)))))
    freqs = np.fft.fftshift(np.fft.fftfreq(len(x), 1.0 / RATE))
    band = np.abs(freqs) < SEARCH_HZ
    k = int(np.argmax(np.where(band, spec, 0.0)))
    if 0 < k < len(spec) - 1:
        a, b, c = (np.log(spec[k - 1] + 1e-12), np.log(spec[k] + 1e-12),
                   np.log(spec[k + 1] + 1e-12))
        denom = a - 2 * b + c
        frac = 0.5 * (a - c) / denom if denom != 0 else 0.0
    else:
        frac = 0.0
    return float(freqs[k] + frac * (RATE / len(x)))


def main() -> int:
    try:
        import SoapySDR
        from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX
    except ImportError:
        print("SoapySDR Python module not importable (see INSTALL-linux.md "
              "section 6a)", file=sys.stderr)
        return 1

    args = dict(driver=DRIVER)
    for part in DEV_ARGS.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            args[k.strip()] = v.strip()
        elif part.strip():
            args["serial"] = part.strip()

    dev = SoapySDR.Device(args)
    dev.setSampleRate(SOAPY_SDR_RX, 0, float(RATE))
    if dev.hasGainMode(SOAPY_SDR_RX, 0):
        dev.setGainMode(SOAPY_SDR_RX, 0, True)
    dev.setFrequency(SOAPY_SDR_RX, 0, float(REF_HZ))

    st = dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
    dev.activateStream(st)
    need = int(RATE * WINDOW_S) * WINDOWS
    iq = np.empty(need, dtype=np.complex64)
    got = 0
    while got < need:
        tmp = np.empty(1 << 16, dtype=np.complex64)
        sr = dev.readStream(st, [tmp], len(tmp), timeoutUs=1_000_000)
        if sr.ret > 0:
            n = min(sr.ret, need - got)
            iq[got:got + n] = tmp[:n]
            got += n
        elif sr.ret < 0 and sr.ret != -1:   # not a timeout
            print(f"readStream error {sr.ret}", file=sys.stderr)
            break
    dev.deactivateStream(st)
    dev.closeStream(st)
    # release once; neutralize the binding's finalizer so its __del__ does
    # not unmake again and print an "unknown device" traceback at exit
    try:
        SoapySDR.Device.unmake(dev)
    except Exception:
        pass
    try:
        dev.close = lambda: None
    except Exception:
        pass

    win = int(RATE * WINDOW_S)
    n_win = got // win
    if n_win < 3:
        print("too few samples (no signal / stream error)", file=sys.stderr)
        return 1
    offsets = [carrier_offset_hz(iq[i * win:(i + 1) * win])
               for i in range(n_win)]
    offsets.sort()
    offset = float(np.median(offsets))
    ppm = offset / REF_HZ * 1e6
    spread = offsets[-1] - offsets[0]

    print(f"reference   : {REF_HZ/1e6:.6f} MHz  (driver={DRIVER})")
    print(f"windows      : {n_win} x {WINDOW_S:.0f}s  "
          f"(spread {spread:.0f} Hz -> {spread/REF_HZ*1e6:.2f} ppm)")
    print(f"carrier at  : {offset:+.1f} Hz from center (median)")
    print(f"clock error : {ppm:+.2f} ppm")
    print()
    print(f"try:  --ppm {-ppm:.2f}")
    print("(re-run after applying it: the offset should drop toward 0; if it")
    print(" doubles instead, use the opposite sign. A large spread means a")
    print(" noisy reference -- try 10 MHz WWV, or let the device warm up.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
