"""Common SDR abstraction layer.

Every receiver backend implements :class:`SDRSource`. The decoding engine and
spotting layers only ever see this interface (requirement 4.4): they must never
import or call device-specific code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterator

import numpy as np


@dataclass(frozen=True)
class Capabilities:
    """Static description of what a backend can do."""

    name: str
    max_sample_rate: int          # widest IQ bandwidth per channel, in Hz
    sample_rates: tuple           # supported IQ sample rates, in Hz
    max_channels: int = 1         # simultaneous DDC/slice channels
    exclusive: bool = True        # False if access may be shared (public KiwiSDR)
    supports_ptt_sense: bool = False  # backend can report local TX (for blanking)


class SDRSource(ABC):
    """A single wideband IQ stream from one receiver.

    Lifecycle: construct -> connect() -> set_center_frequency()/set_sample_rate()
    -> iterate stream_iq() -> disconnect(). stream_iq() blocks, so backends are
    normally driven from a worker thread.
    """

    @abstractmethod
    def capabilities(self) -> Capabilities:
        ...

    @abstractmethod
    def connect(self) -> None:
        ...

    @abstractmethod
    def disconnect(self) -> None:
        ...

    @abstractmethod
    def set_center_frequency(self, hz: int) -> None:
        ...

    @abstractmethod
    def set_sample_rate(self, hz: int) -> None:
        ...

    @abstractmethod
    def stream_iq(self) -> Iterator[np.ndarray]:
        """Yield blocks of complex64 IQ samples until disconnect() or EOF.

        Block size is backend-chosen; consumers must not assume a fixed size.
        """
        ...

    @property
    def ptt_active(self) -> bool:
        """True while the local station is transmitting (self-blanking, F8).

        Backends without PTT sensing always return False.
        """
        return False

    @property
    def coverage_hz(self):
        """Frequency ranges actually covered by live hardware, as a list of
        (low, high) offsets in Hz relative to the center frequency, or None
        when the whole passband is covered.

        Tiled backends (KiwiSDR) return only the live slices, so the decoder
        won't chase upsampling images or noise in uncovered regions.
        """
        return None

    @property
    def center_frequency(self) -> int:
        return getattr(self, "_center_frequency", 0)

    @property
    def sample_rate(self) -> int:
        return getattr(self, "_sample_rate", 0)
