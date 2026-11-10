"""KiwiSDR wire-protocol helpers (pure functions, unit-testable).

The KiwiSDR streams over WebSocket. Server frames start with a 3-byte tag:
  b"MSG" : space-separated, URL-encoded key=value pairs (config/status)
  b"SND" : audio -- [flags u8][seq u32 LE][smeter u16 BE][payload]
           in IQ mode the payload starts with a 10-byte GPS timestamp
           header (<BBII), followed by interleaved big-endian s16 I/Q pairs

Protocol behavior mirrors the community kiwiclient implementation.
"""

from __future__ import annotations

import struct
from typing import Dict, Optional, Tuple
from urllib.parse import unquote

import numpy as np

SND_HEADER = struct.Struct("<BI")     # flags, sequence
IQ_GPS_HEADER_LEN = 10                # <BBII gps timestamp block

# SND flags byte (values from the reference kiwiclient implementation)
SND_FLAG_ADC_OVFL = 0x02
SND_FLAG_STEREO = 0x08      # set in IQ mode (interleaved I/Q pairs)
SND_FLAG_COMPRESSED = 0x10  # ADPCM -- we always request compression off


def parse_msg(body: bytes) -> Dict[str, str]:
    """Parse a MSG frame body (after the tag) into a key -> value dict."""
    out: Dict[str, str] = {}
    for part in body.decode("utf-8", "replace").split():
        if "=" in part:
            k, v = part.split("=", 1)
            out[k] = unquote(v)
        else:
            out[part] = ""
    return out


def parse_snd_iq(body: bytes) -> Tuple[int, Optional[np.ndarray]]:
    """Parse an IQ-mode SND frame body (after the tag).

    Returns (sequence, complex64 samples scaled to +/-1.0), or (seq, None)
    for a malformed/empty frame -- or for a frame that is not raw stereo IQ.
    A full Kiwi may "camp" an over-capacity connection onto another user's
    channel and stream their (mono, ADPCM-compressed) audio; parsed as raw
    s16 pairs that becomes full-scale noise, so such frames must be dropped.
    """
    if len(body) < 7 + IQ_GPS_HEADER_LEN:
        return -1, None
    flags, seq = SND_HEADER.unpack_from(body, 0)
    if flags & SND_FLAG_COMPRESSED or not flags & SND_FLAG_STEREO:
        return seq, None
    data = body[7 + IQ_GPS_HEADER_LEN:]
    n = len(data) // 4
    if n == 0:
        return seq, None
    pairs = np.frombuffer(data, dtype=">i2", count=2 * n).astype(np.float32)
    pairs /= 32768.0
    return seq, (pairs[0::2] + 1j * pairs[1::2]).astype(np.complex64)


def client_setup_commands(password: str, ident: str) -> list:
    """Commands sent right after the WebSocket opens."""
    return [f"SET auth t=kiwi p={password}", f"SET ident_user={ident}"]


def client_stream_commands(audio_rate: int, freq_hz: float,
                           passband_hz: float) -> list:
    """Commands that configure the IQ stream once audio_rate is known.

    AGC stays ON (slow decay, hang): with fixed manual gain a strong
    in-slice carrier clips the s16 stream and smears full-scale noise across
    the whole slice (seen live as a solid-white waterfall band). AGC scales
    signal and noise floor together, so the per-bin SNR the skimmer measures
    is unaffected. Compression must be off: the decoder wants raw s16
    samples, not ADPCM.
    """
    cut = passband_hz / 2
    return [
        f"SET AR OK in={audio_rate} out=44100",
        "SET squelch=0 max=0",
        "SET lms_autonotch=0",
        "SET genattn=0",
        "SET gen=0 mix=-1",
        "SET compression=0",
        "SET agc=1 hang=1 thresh=-100 slope=6 decay=2000 manGain=50",
        f"SET mod=iq low_cut={-cut:.0f} high_cut={cut:.0f} freq={freq_hz / 1000.0:.3f}",
    ]
