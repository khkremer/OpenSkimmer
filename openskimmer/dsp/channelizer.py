"""STFT channelizer: wideband IQ -> stream of power-spectrum frames.

FFT size is chosen per sample rate so bins stay ~47 Hz wide and the frame
rate stays ~187 fps (hop = fft/4), which resolves keying up to ~45 WPM while
keeping per-bin noise bandwidth narrow. Frame index * hop / fs is the
pipeline's timebase, so file playback decodes identically to live streams.
"""

from __future__ import annotations

from typing import Iterator

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view


class Channelizer:
    def __init__(self, sample_rate: int, target_bin_hz: float = 46.875):
        self.sample_rate = sample_rate
        self.fft_size = 1 << max(8, round(np.log2(sample_rate / target_bin_hz)))
        self.hop = self.fft_size // 4
        self.bin_hz = sample_rate / self.fft_size
        self.frame_dt = self.hop / sample_rate
        self._window = np.hanning(self.fft_size).astype(np.float32)
        self._buf = np.zeros(0, dtype=np.complex64)

    def bin_to_offset_hz(self, bin_index: float) -> float:
        """Bin index (0 = lowest, fftshifted) to Hz offset from center."""
        return (bin_index - self.fft_size / 2) * self.bin_hz

    def process(self, samples: np.ndarray) -> Iterator[np.ndarray]:
        """Consume IQ samples, yield fftshifted power spectra (float32).

        All frames in the buffer are transformed in a single batched FFT
        (one pocketfft call over the frame axis) rather than one call per
        hop, which removes the Python-loop overhead and is markedly faster
        on many-frame blocks. Numerically identical to the per-frame form,
        so file replay stays bit-for-bit reproducible.
        """
        self._buf = np.concatenate([self._buf, samples.astype(np.complex64)])
        n, hop = self.fft_size, self.hop
        if len(self._buf) < n:
            return
        # (n_frames, n) hop-stepped view -> windowed copy -> batched FFT
        frames = sliding_window_view(self._buf, n)[::hop]
        n_frames = frames.shape[0]
        spec = np.fft.fftshift(np.fft.fft(frames * self._window, axis=1),
                               axes=1)
        power = (spec.real ** 2 + spec.imag ** 2).astype(np.float32)
        self._buf = self._buf[n_frames * hop:].copy()
        yield from power
