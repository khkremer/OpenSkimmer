#!/usr/bin/env python3
"""Demodulate a slice of a ka9q RTP *IQ* channel to audio (for WSJT-X etc.).

Instead of asking radiod for a separate demodulated audio channel, this
takes the wideband IQ stream we already have and demodulates any frequency
inside it on the receiving end: mix the wanted frequency toward baseband,
low-pass, downsample to an audio rate, and play it. Point the output at a
virtual cable (BlackHole / PulseAudio null sink) and set WSJT-X's input to
it. Any frequency in the IQ passband can be tuned without touching radiod.

The demod takes the real part after mixing the dial to DC, so it is really
double-sideband: a signal the same distance *below* the dial folds onto the
same audio frequency as one above it. That is fine for FT8/FT4 (the whole
sub-band sits above the dial) but a strong carrier just below the dial would
alias in. Ask if you need true single-sideband (phasing/Weaver) here.

Needs 'sounddevice':  pip install sounddevice

Usage:
  tools/rtp_iq_audio.py --group skimmer-iq.local:5004 \
      --center 28050000 --tune 28074000            # USB, e.g. FT8
  tools/rtp_iq_audio.py --group 239.1.2.3:5004 --center 28050000 \
      --tune 28030000 --mode cw --device BlackHole
"""

import argparse
import collections
import socket
import struct
import sys
import threading
import time

import numpy as np


def parse_rtp_iq(pkt, want_ssrc):
    if len(pkt) < 12:
        return None
    b0 = pkt[0]
    cc = b0 & 0x0F
    seq, ts, ssrc = struct.unpack_from("!HII", pkt, 2)
    if want_ssrc is not None and ssrc != want_ssrc:
        return None
    off = 12 + cc * 4
    if b0 & 0x10 and len(pkt) >= off + 4:
        off += 4 + struct.unpack_from("!H", pkt, off + 2)[0] * 4
    n = (len(pkt) - off) // 4
    if n <= 0:
        return None
    s = np.frombuffer(pkt, dtype=">i2", count=2 * n, offset=off).astype(np.float32)
    return seq, ts, ssrc, ((s[0::2] + 1j * s[1::2]) / 32768.0).astype(np.complex64)


def _lowpass(fs, cutoff, numtaps=161):
    m = np.arange(numtaps) - (numtaps - 1) / 2
    h = 2 * cutoff / fs * np.sinc(2 * cutoff / fs * m)
    h *= np.hamming(numtaps)
    return (h / h.sum()).astype(np.complex64)


class Demod:
    """Streaming SSB/CW demodulator: complex IQ block in, real audio out."""

    def __init__(self, fs, offset_hz, audio_rate, mode="usb"):
        self.fs = fs
        self.decim = max(1, round(fs / audio_rate))
        self.audio_rate = fs / self.decim
        bfo, bw = {"usb": (0.0, 3000.0), "lsb": (0.0, 3000.0),
                   "cw": (600.0, 400.0)}.get(mode, (0.0, 3000.0))
        self.eff_offset = offset_hz - bfo      # bring signal to +bfo (or DC)
        cutoff = min(bw, 0.45 * self.audio_rate)
        self.taps = _lowpass(fs, cutoff)
        self.state = np.zeros(len(self.taps) - 1, dtype=np.complex64)
        self.dec_rem = np.zeros(0, dtype=np.complex64)
        self.n = 0

    def process(self, iq: np.ndarray) -> np.ndarray:
        idx = np.arange(self.n, self.n + len(iq))
        self.n += len(iq)
        y = iq * np.exp(-2j * np.pi * self.eff_offset * idx / self.fs)
        z = np.concatenate([self.state, y.astype(np.complex64)])
        filt = np.convolve(z, self.taps, mode="valid")
        self.state = z[len(z) - (len(self.taps) - 1):]
        buf = np.concatenate([self.dec_rem, filt])
        n_out = len(buf) // self.decim
        out = buf[:n_out * self.decim].reshape(-1, self.decim)[:, 0]
        self.dec_rem = buf[n_out * self.decim:]
        return np.real(out).astype(np.float32)      # real part -> DSB (see docstring)


def join_group(group, port, iface):
    grp = socket.gethostbyname(group)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):          # share the port with other subscribers
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 21)
    sock.bind(("", port))
    _membership(sock, grp, iface, socket.IP_ADD_MEMBERSHIP)
    sock.settimeout(1.0)
    return sock, grp


def _membership(sock, grp, iface, op):
    mreq = struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, op, mreq)


def rejoin(sock, grp, iface):
    """Leave and re-join the group. Multicast delivery can lapse silently --
    an IGMP-snooping switch stops forwarding if the membership times out and
    no querier refreshes it -- and re-joining re-arms it. This is what
    restarting the tool does by hand; doing it here recovers automatically."""
    try:
        _membership(sock, grp, iface, socket.IP_DROP_MEMBERSHIP)
    except OSError:
        pass
    try:
        _membership(sock, grp, iface, socket.IP_ADD_MEMBERSHIP)
    except OSError as e:
        print(f"  rejoin failed: {e}", flush=True)


def measure_iq_rate(sock, ssrc, seconds=1.5):
    first_ts = last_ts = first_t = None
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            pkt, _ = sock.recvfrom(65536)
        except socket.timeout:
            if first_ts is not None:
                break                       # had packets, just a lull
            return None
        r = parse_rtp_iq(pkt, ssrc)
        if r is None:
            continue
        _seq, ts, _ssrc, _iq = r
        if first_ts is None:
            first_ts, first_t = ts, time.time()
        last_ts = ts
    if first_ts is None or last_ts == first_ts:
        return None
    span = (last_ts - first_ts) & 0xFFFFFFFF
    return span / (time.time() - first_t)


def main() -> int:
    ap = argparse.ArgumentParser(description="demodulate a ka9q IQ slice to audio")
    ap.add_argument("--group", help="MCAST_HOST:PORT of the IQ channel")
    ap.add_argument("--iface", default="0.0.0.0")
    ap.add_argument("--center", type=float, help="IQ channel centre Hz "
                                                 "(SSRC = center/1000)")
    ap.add_argument("--tune", type=float, help="frequency to demodulate, Hz")
    ap.add_argument("--mode", default="usb", choices=["usb", "lsb", "cw"])
    ap.add_argument("--audio-rate", type=int, default=12000)
    ap.add_argument("--rate", type=int, help="override the measured IQ rate")
    ap.add_argument("--device", help="output device (e.g. BlackHole)")
    ap.add_argument("--gain", type=float, default=4.0)
    ap.add_argument("--list-devices", action="store_true")
    ap.add_argument("--stall-timeout", type=float, default=5.0,
                    help="seconds without packets before warning + re-joining")
    ap.add_argument("--status-interval", type=float, default=60.0,
                    help="seconds between status lines (0 disables)")
    args = ap.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print("needs sounddevice: pip install sounddevice", file=sys.stderr)
        return 1
    if args.list_devices:
        print(sd.query_devices())
        return 0
    if not (args.group and args.center and args.tune):
        print("need --group, --center and --tune", file=sys.stderr)
        return 1

    ssrc = round(args.center / 1000.0)
    host, port = args.group.rsplit(":", 1)
    sock, grp = join_group(host, int(port), args.iface)

    fs = args.rate
    if fs is None:
        print("measuring IQ sample rate...", flush=True)
        fs = measure_iq_rate(sock, ssrc)
        if not fs:
            print("no IQ packets for that SSRC -- check --group/--center",
                  file=sys.stderr)
            return 1
        fs = int(round(fs / 1000.0) * 1000)      # snap to nearest kHz
        print(f"IQ rate ~{fs} Hz", flush=True)

    offset = args.tune - args.center
    if abs(offset) > fs / 2:
        print(f"--tune is {offset/1000:.0f} kHz off centre, outside the "
              f"+/-{fs/2000:.0f} kHz passband", file=sys.stderr)
        return 1
    dem = Demod(fs, offset, args.audio_rate, args.mode)

    buf = collections.deque()
    lock = threading.Lock()
    stats = {"pkts": 0, "lost": 0, "dropped": 0, "starved": 0, "cb": 0}

    def callback(outdata, frames, time_info, status):
        stats["cb"] += 1
        if status:                            # under/overflow reported by PortAudio
            stats["starved"] += 1
        out = np.zeros(frames, dtype="float32")
        filled = 0
        with lock:
            while filled < frames and buf:
                chunk = buf[0]
                take = min(frames - filled, len(chunk))
                out[filled:filled + take] = chunk[:take]
                if take == len(chunk):
                    buf.popleft()
                else:
                    buf[0] = chunk[take:]
                filled += take
        if filled < frames:
            stats["starved"] += 1             # nothing to play: audio goes silent
        outdata[:, 0] = out

    stream = sd.OutputStream(samplerate=int(dem.audio_rate), channels=1,
                             dtype="float32", device=args.device,
                             callback=callback)
    stream.start()
    print(f"demod {args.mode.upper()} {args.tune/1e6:.4f} MHz "
          f"({offset/1000:+.1f} kHz) -> {dem.audio_rate:.0f} Hz audio"
          f"{' to ' + str(args.device) if args.device else ''}"
          " -- Ctrl-C to stop", flush=True)
    last_pkt = time.time()
    last_report = last_pkt
    last_seq = None
    stalled = False
    try:
        while True:
            now = time.time()
            try:
                pkt, _ = sock.recvfrom(65536)
            except socket.timeout:
                pkt = None
            if pkt is not None:
                r = parse_rtp_iq(pkt, ssrc)
                if r is not None:
                    seq, _ts, _ssrc, iq = r
                    if last_seq is not None:
                        gap = (seq - last_seq - 1) & 0xFFFF
                        if 0 < gap < 1000:      # ignore restarts/reorder storms
                            stats["lost"] += gap
                    last_seq = seq
                    stats["pkts"] += 1
                    last_pkt = now
                    if stalled:
                        print(f"[{time.strftime('%H:%M:%S')}] stream resumed",
                              flush=True)
                        stalled = False
                    audio = dem.process(iq) * args.gain
                    with lock:
                        buf.append(audio)
                        if len(buf) > 400:      # cap backlog: keep audio live
                            buf.popleft()
                            stats["dropped"] += 1

            # watchdog: no packets is a NETWORK/sender problem; packets but a
            # starving callback is a LOCAL audio problem. Say which, then try
            # re-joining the group, which fixes a lapsed IGMP membership.
            if now - last_pkt > args.stall_timeout:
                if not stalled:
                    print(f"[{time.strftime('%H:%M:%S')}] NO PACKETS for "
                          f"{now - last_pkt:.0f}s -- sender stopped or multicast "
                          f"delivery lapsed; re-joining {args.group}", flush=True)
                    stalled = True
                rejoin(sock, grp, args.iface)
                last_pkt = now - args.stall_timeout + 5.0   # retry every 5 s

            if args.status_interval and now - last_report >= args.status_interval:
                with lock:
                    depth = sum(len(c) for c in buf)
                print(f"[{time.strftime('%H:%M:%S')}] "
                      f"{stats['pkts']} pkts, {stats['lost']} lost, "
                      f"buffer {depth/dem.audio_rate:.2f}s, "
                      f"{stats['dropped']} dropped, {stats['starved']} starved",
                      flush=True)
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
