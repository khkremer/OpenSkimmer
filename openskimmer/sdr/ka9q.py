"""ka9q-radio backend: subscribe to a radiod RTP multicast IQ channel.

radiod (ka9q-radio) does the wideband front-end DSP and emits each channel
as an RTP multicast stream. This backend joins the channel's data multicast
group, selects our channel by its RTP SSRC, parses the IQ payload, and
yields complex64 blocks -- no tiling, no DSP. The front end is whatever
radiod drives (RTL-SDR for the proof of concept, RX-888 later); this code
never sees the hardware, only the network stream.

Wire format (verified against a live radiod):
  - standard 12-byte RTP header; payload is interleaved I/Q samples, no
    status header: 16-bit big-endian by default (4 bytes = one complex
    sample), or 32-bit float when the channel has `encoding = f32` in
    radiod's config (8 bytes = one complex sample, little-endian as radiod
    sends it from an x86 or ARM host). --rtp-encoding must match: read with
    the wrong one, the stream decodes as noise and nothing else says so.
  - the RTP SSRC is the channel's centre frequency in kHz (a 28.050 MHz
    channel has SSRC 28050), so we derive it from the centre frequency.
  - the channel's output sample rate is set in radiod; --sample-rate here
    must match it (there is no host-side resampling).
"""

from __future__ import annotations

import logging
import socket
import struct
from typing import Iterator, Optional

import numpy as np

from .base import Capabilities, SDRSource

log = logging.getLogger(__name__)

RTP_PORT = 5004
ENCODINGS = ("s16be", "f32le")
# Receive buffer to ask for. The kernel silently caps it at net.core.rmem_max,
# so asking big costs nothing on an untuned host and lets a raised limit
# (deploy/tune-net-buffers.sh sets 32 MB) take effect. A fixed 2 MB request
# held the socket at 2 MB whatever the limit: nine 192 kHz float32 channels
# arrive at about 112 Mbit/s, and every band's socket receives all of them.
RCVBUF = 32 << 20


def parse_rtp_iq(pkt: bytes, want_ssrc: int, encoding: str = "s16be"):
    """Parse an RTP/IQ packet for one channel.

    Returns (seq, complex64 samples scaled to +/-1.0), or None if the packet
    is malformed, empty, or belongs to a different SSRC. encoding: "s16be"
    (radiod's default) or "f32le" (radiod `encoding = f32`).
    """
    if len(pkt) < 12:
        return None
    b0 = pkt[0]
    cc = b0 & 0x0F
    seq, _ts, ssrc = struct.unpack_from("!HII", pkt, 2)
    if ssrc != want_ssrc:
        return None
    off = 12 + cc * 4
    if b0 & 0x10 and len(pkt) >= off + 4:          # RTP extension header
        off += 4 + struct.unpack_from("!H", pkt, off + 2)[0] * 4
    if encoding == "f32le":
        n = (len(pkt) - off) // 8                   # 8 bytes per complex sample
        if n <= 0:
            return None
        s = np.frombuffer(pkt, dtype="<f4", count=2 * n, offset=off)
        return seq, (s[0::2] + 1j * s[1::2]).astype(np.complex64)
    n = (len(pkt) - off) // 4                       # 4 bytes per complex sample
    if n <= 0:
        return None
    s = np.frombuffer(pkt, dtype=">i2", count=2 * n, offset=off).astype(np.float32)
    s /= 32768.0
    return seq, (s[0::2] + 1j * s[1::2]).astype(np.complex64)


class Ka9qSource(SDRSource):
    def __init__(self, group: str, port: int = RTP_PORT,
                 ssrc: Optional[int] = None, iface: str = "0.0.0.0",
                 encoding: str = "s16be", rcvbuf: int = RCVBUF):
        if not group:
            raise ValueError("ka9q backend needs a multicast group")
        if encoding not in ENCODINGS:
            raise ValueError(f"ka9q encoding must be one of {ENCODINGS}, got {encoding!r}")
        self._encoding = encoding
        self._rcvbuf = int(rcvbuf)
        self._group = group
        self._port = port
        self._iface = iface
        self._ssrc = ssrc                 # None -> derive from centre freq
        self._center_frequency = 28_050_000
        self._sample_rate = 192000
        self._sock: Optional[socket.socket] = None
        self._running = False

    def capabilities(self) -> Capabilities:
        return Capabilities(
            name="ka9q",
            max_sample_rate=self._sample_rate,
            sample_rates=(self._sample_rate,),
            max_channels=1,
            exclusive=False,             # multicast: many subscribers are fine
        )

    def set_center_frequency(self, hz: int) -> None:
        self._center_frequency = int(hz)
        if self._ssrc is None:
            self._ssrc = round(hz / 1000.0)   # ka9q SSRC = frequency in kHz

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate = int(hz)

    def connect(self) -> None:
        grp = socket.gethostbyname(self._group)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM,
                             socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):      # coexist with taps/other subscribers
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, self._rcvbuf)
        got = sock.getsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF) // 2   # Linux reports double
        if got < min(self._rcvbuf, 8 << 20):
            log.warning("ka9q: receive buffer %d kB (net.core.rmem_max caps it); packets "
                        "may be lost on busy bands: see deploy/tune-net-buffers.sh", got // 1024)
        sock.bind(("", self._port))
        mreq = struct.pack("4s4s", socket.inet_aton(grp),
                           socket.inet_aton(self._iface))
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        sock.settimeout(2.0)
        self._sock = sock
        if self._ssrc is None:
            self._ssrc = round(self._center_frequency / 1000.0)
        log.info("ka9q: joined %s:%d on %s, selecting channel SSRC %d (%.3f MHz), %s",
                 grp, self._port, self._iface, self._ssrc, self._center_frequency / 1e6,
                 self._encoding)

    def disconnect(self) -> None:
        self._running = False
        if self._sock is not None:
            self._sock.close()
            self._sock = None

    @property
    def sample_rate(self) -> int:
        return self._sample_rate

    def stream_iq(self) -> Iterator[np.ndarray]:
        if self._sock is None:
            raise RuntimeError("connect() first")
        self._running = True
        expected: Optional[int] = None
        got_any = False
        try:
            while self._running:
                try:
                    pkt, _ = self._sock.recvfrom(65536)
                except socket.timeout:
                    if not got_any:
                        log.warning("ka9q: no packets for our SSRC yet "
                                    "(check group/port and the channel freq)")
                    continue
                except OSError:
                    break
                r = parse_rtp_iq(pkt, self._ssrc, self._encoding)
                if r is None:
                    continue
                seq, iq = r
                got_any = True
                if expected is not None and seq != expected:
                    lost = (seq - expected) & 0xFFFF
                    if 0 < lost < 1000:
                        log.warning("ka9q: %d RTP packet(s) lost", lost)
                expected = (seq + 1) & 0xFFFF
                yield iq
        finally:
            self._running = False
