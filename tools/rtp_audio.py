#!/usr/bin/env python3
"""Play a ka9q-radio RTP audio channel to a sound device (for WSJT-X etc.).

radiod emits a demodulated channel (USB/FT8 mode) as an RTP multicast
stream of 16-bit big-endian PCM. This joins the group, selects the channel
by SSRC (= centre frequency in kHz), and plays it to an output device --
point that at a virtual cable (BlackHole on macOS, a PulseAudio null sink
on Linux) and set WSJT-X's audio input to the same device.

The stream's sample rate is measured from the RTP timestamps, so you don't
have to know radiod's channel samprate in advance.

Needs the 'sounddevice' module:  pip install sounddevice
(its wheel bundles PortAudio on macOS/Windows; on Linux: apt install
libportaudio2).

Usage:
  tools/rtp_audio.py --list-devices
  tools/rtp_audio.py --group ft8-audio.local:5004 --freq 28074000
  tools/rtp_audio.py --group 239.1.2.3:5004 --ssrc 28074 --device BlackHole
"""

import argparse
import collections
import socket
import struct
import sys
import threading
import time

import numpy as np

STD_RATES = [8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000]


def parse_rtp(pkt: bytes):
    if len(pkt) < 12:
        return None
    b0 = pkt[0]
    cc = b0 & 0x0F
    seq, ts, ssrc = struct.unpack_from("!HII", pkt, 2)
    off = 12 + cc * 4
    if b0 & 0x10 and len(pkt) >= off + 4:
        off += 4 + struct.unpack_from("!H", pkt, off + 2)[0] * 4
    return seq, ts, ssrc, pkt[off:]


def join_group(group: str, port: int, iface: str) -> socket.socket:
    grp = socket.gethostbyname(group)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):          # share the port with other subscribers
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    sock.bind(("", port))
    mreq = struct.pack("4s4s", socket.inet_aton(grp), socket.inet_aton(iface))
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    sock.settimeout(5.0)
    return sock


def measure_rate(sock, ssrc, channels, seconds=1.5):
    """Estimate the audio sample rate from RTP timestamps over a short window."""
    first_ts = last_ts = None
    first_t = None
    frames = 0
    t0 = time.time()
    while time.time() - t0 < seconds:
        try:
            pkt, _ = sock.recvfrom(65536)
        except socket.timeout:
            return None
        r = parse_rtp(pkt)
        if r is None or (ssrc is not None and r[2] != ssrc):
            continue
        _seq, ts, got_ssrc, payload = r
        if ssrc is None:
            ssrc = got_ssrc                       # lock onto the first channel
        n = len(payload) // (2 * channels)
        if first_ts is None:
            first_ts, first_t = ts, time.time()
        last_ts = ts
        frames += n
    if first_ts is None or last_ts == first_ts:
        return ssrc, None
    span = (last_ts - first_ts) & 0xFFFFFFFF
    dt = time.time() - first_t
    return ssrc, span / dt if dt > 0 else None


def main() -> int:
    ap = argparse.ArgumentParser(description="play a ka9q RTP audio channel")
    ap.add_argument("--group", help="MCAST_HOST:PORT of the audio channel")
    ap.add_argument("--iface", default="0.0.0.0")
    ap.add_argument("--ssrc", type=int, help="channel SSRC (freq in kHz)")
    ap.add_argument("--freq", type=float,
                    help="channel centre Hz (SSRC derived as freq/1000)")
    ap.add_argument("--channels", type=int, default=1, choices=[1, 2])
    ap.add_argument("--rate", type=int, help="override the measured sample rate")
    ap.add_argument("--device", help="output device name or index "
                                     "(e.g. BlackHole)")
    ap.add_argument("--gain", type=float, default=1.0)
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    try:
        import sounddevice as sd
    except ImportError:
        print("needs sounddevice: pip install sounddevice", file=sys.stderr)
        return 1

    if args.list_devices:
        print(sd.query_devices())
        return 0
    if not args.group:
        print("--group is required (or use --list-devices)", file=sys.stderr)
        return 1

    ssrc = args.ssrc
    if ssrc is None and args.freq:
        ssrc = round(args.freq / 1000.0)

    host, port = args.group.rsplit(":", 1)
    sock = join_group(host, int(port), args.iface)

    rate = args.rate
    if rate is None:
        print("measuring sample rate from RTP timestamps...", flush=True)
        res = measure_rate(sock, ssrc, args.channels)
        if res is None:
            print("no packets -- wrong group/port/SSRC?", file=sys.stderr)
            return 1
        ssrc, measured = res
        if measured:
            rate = min(STD_RATES, key=lambda r: abs(r - measured))
            print(f"measured ~{measured:.0f} Hz -> using {rate} Hz "
                  f"(ssrc {ssrc})", flush=True)
        else:
            rate = 48000
            print(f"could not measure; assuming {rate} Hz", flush=True)

    buf = collections.deque()
    lock = threading.Lock()

    def callback(outdata, frames, time_info, status):
        out = np.zeros((frames, args.channels), dtype="float32")
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
        outdata[:] = out

    stream = sd.OutputStream(samplerate=rate, channels=args.channels,
                             dtype="float32", device=args.device,
                             callback=callback, blocksize=0)
    stream.start()
    print(f"playing ssrc {ssrc} at {rate} Hz, {args.channels} ch"
          f"{' to ' + str(args.device) if args.device else ''}"
          " -- Ctrl-C to stop", flush=True)
    try:
        while True:
            try:
                pkt, _ = sock.recvfrom(65536)
            except socket.timeout:
                continue
            r = parse_rtp(pkt)
            if r is None or (ssrc is not None and r[2] != ssrc):
                continue
            payload = r[3]
            s = np.frombuffer(payload, dtype=">i2").astype("float32")
            s *= args.gain / 32768.0
            frames = s.reshape(-1, args.channels)
            with lock:
                buf.append(frames)
                if len(buf) > 200:               # ~seconds of backlog: drop
                    buf.popleft()
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop()
        stream.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
