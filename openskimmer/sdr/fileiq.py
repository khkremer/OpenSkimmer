"""IQ file playback source (recordings for offline decoding / A-B tests).

Supported formats:
  - .wav  : stereo PCM16, left = I, right = Q
  - .c64  : raw interleaved complex64
  - .s16  : raw interleaved int16 I/Q
"""

from __future__ import annotations

import time
import wave
from pathlib import Path
from typing import Iterator

import numpy as np

from .base import Capabilities, SDRSource


class FileIQSource(SDRSource):
    def __init__(self, path: str, sample_rate: int = 48000,
                 center_frequency: int = 14_030_000, realtime: bool = False,
                 block_size: int = 8192):
        self._path = Path(path)
        self._sample_rate = sample_rate
        self._center_frequency = center_frequency
        self._realtime = realtime
        self._block = block_size
        self._running = False

    def capabilities(self) -> Capabilities:
        return Capabilities(name="file", max_sample_rate=10_000_000,
                            sample_rates=())

    def connect(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(self._path)

    def disconnect(self) -> None:
        self._running = False

    def set_center_frequency(self, hz: int) -> None:
        self._center_frequency = int(hz)

    def set_sample_rate(self, hz: int) -> None:
        self._sample_rate = int(hz)

    def _chunks(self, n: int) -> Iterator[np.ndarray]:
        """The file as complex64 pieces of n samples (the last may be short).

        Read piece by piece rather than whole: a 70-minute 192 kHz recording
        is 6.5 GB, more than a small host's memory.
        """
        suffix = self._path.suffix.lower()
        if suffix == ".wav":
            with wave.open(str(self._path), "rb") as w:
                if w.getnchannels() != 2 or w.getsampwidth() != 2:
                    raise ValueError("IQ wav must be stereo PCM16")
                self._sample_rate = w.getframerate()
                while True:
                    raw = np.frombuffer(w.readframes(n), dtype=np.int16)
                    if not len(raw):
                        return
                    iq = raw.reshape(-1, 2).astype(np.float32) / 32768.0
                    yield (iq[:, 0] + 1j * iq[:, 1]).astype(np.complex64)
        elif suffix in (".c64", ".s16"):
            with open(self._path, "rb") as f:
                while True:
                    if suffix == ".c64":
                        data = np.fromfile(f, dtype=np.complex64, count=n)
                    else:
                        raw = np.fromfile(f, dtype=np.int16, count=2 * n)
                        raw = raw.astype(np.float32) / 32768.0
                        data = (raw[0::2] + 1j * raw[1::2]).astype(np.complex64)
                    if not len(data):
                        return
                    yield data
        else:
            raise ValueError(f"Unknown IQ file format: {suffix}")

    def stream_iq(self) -> Iterator[np.ndarray]:
        self._running = True
        t0 = time.monotonic()
        sent = 0
        # whole blocks per piece, so the blocks are the same as slicing the
        # whole file
        for data in self._chunks(self._block * 128):
            for start in range(0, len(data), self._block):
                if not self._running:
                    return
                block = data[start:start + self._block]
                sent += len(block)
                if self._realtime:
                    target = t0 + sent / self._sample_rate
                    delay = target - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                yield block
