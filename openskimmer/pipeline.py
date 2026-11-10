"""Decoding pipeline: IQ blocks in, Spot records out.

Hardware-agnostic by construction: it only sees numpy blocks and the sample
rate / center frequency, never a backend object (requirement 4.4 / 7).
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Callable, Optional

import numpy as np

from .dsp.channelizer import Channelizer
from .dsp.tracker import Channel, SignalTracker
from .spotting.callsign import CallsignExtractor
from .spotting.segmenter import SEGMENTERS
from .spotting.spotter import Spot, Spotter

log = logging.getLogger(__name__)


class TextTap:
    """Channel sink wrapper: forwards to the extractor, keeps a rolling
    window of decoded text for UIs/diagnostics, and optionally reports each
    completed word (for --log-channels) and each character with the gap
    before it (for --log-chars)."""

    def __init__(self, inner: CallsignExtractor, maxlen: int = 96,
                 on_word: Optional[Callable[[str], None]] = None,
                 on_char: Optional[Callable[[str, Optional[tuple]], None]] = None):
        self.inner = inner
        self.on_word = on_word
        self.on_char = on_char
        self._chars: deque = deque(maxlen=maxlen)
        self._word = ""
        self._gap: Optional[tuple] = None

    def add_gap(self, gap_ms: float, dit_ms: float, word_thr: float) -> None:
        self._gap = (gap_ms, dit_ms, word_thr)
        add_gap = getattr(self.inner, "add_gap", None)   # a segmenter
        if add_gap is not None:
            add_gap(gap_ms, dit_ms, word_thr)

    def add_char(self, ch: str) -> None:
        if self.on_char is not None:
            self.on_char(ch, self._gap)
            self._gap = None
        self._chars.append(ch)
        if self.on_word is not None and len(self._word) < 24:
            self._word += ch
        self.inner.add_char(ch)

    def add_word_break(self) -> None:
        if self._chars and self._chars[-1] != " ":
            self._chars.append(" ")
        if self.on_word is not None and self._word:
            self.on_word(self._word)
        if self.on_char is not None:
            self.on_char("|", None)
        self._word = ""
        self.inner.add_word_break()

    @property
    def text(self) -> str:
        return "".join(self._chars)


class Pipeline:
    def __init__(self, sample_rate: int, center_frequency: int,
                 on_spot: Callable[[Spot], None],
                 min_snr_db: int = 3,
                 max_wpm: int = 50,
                 blocked_calls=None,
                 respot_interval_s: float = 600.0,
                 respot_tol_hz: float = 1000.0,
                 max_channels: int = 100,
                 coverage_fn: Optional[Callable[[], Optional[list]]] = None,
                 on_spectrum: Optional[Callable[[np.ndarray], None]] = None,
                 on_channels: Optional[Callable[[list], None]] = None,
                 on_word: Optional[Callable[[dict], None]] = None,
                 on_char: Optional[Callable[[dict], None]] = None,
                 decoder_cls: Optional[type] = None,
                 segmenter: str = "timing",
                 known_calls=None, call_patterns=None,
                 known_calls_weak=None,
                 media_clock_epoch: Optional[float] = None,
                 my_call: Optional[str] = None):
        """coverage_fn, when given, returns the source's live (low, high) Hz
        offset ranges (SDRSource.coverage_hz); regions outside them are
        excluded from signal detection.

        on_spectrum receives a time-averaged power spectrum (~15/s) and
        on_channels a snapshot of active decoders (~1/s); both are called on
        the DSP thread and exist for UI taps -- the pipeline itself has no
        UI knowledge. on_word receives every completed decoded word as
        {freq_hz, wpm, snr_db, word} (diagnostics / --log-channels), and
        on_char every character and word break ("|") with the gap before
        the character: gap_ms, dit_ms, word_thr in dits (--log-chars).

        respot_tol_hz: frequency drift tolerated between sightings of the
        SAME call before respot_interval_s lets it spot again -- see
        Spotter._respot_span for why a tracked channel's own churn needs
        more than the ~100 Hz a single spot bucket gives it.
        """
        self.center_frequency = center_frequency
        self.coverage_fn = coverage_fn
        self.on_spectrum = on_spectrum
        self.on_channels = on_channels
        self.on_word = on_word
        self.on_char = on_char
        self.segmenter_cls = SEGMENTERS[segmenter]
        self.known_calls = known_calls
        self.known_calls_weak = known_calls_weak
        self.call_patterns = call_patterns
        self._last_coverage: Optional[tuple] = ()
        self.channelizer = Channelizer(sample_rate)
        # Media clock: time advancing with AUDIO processed, not wall clock.
        # File replay runs ~40x faster than real time, so on the wall clock a
        # 10-minute recording finishes in 15 s and --respot suppresses every
        # repeat while the runner memory never expires -- replay simply does
        # not reproduce live spotting. Anchored at the recording's true UTC
        # start, spot timestamps also become real times, so a replay can be
        # matched against the RBN archive to the second instead of over a
        # whole window.
        self._epoch = media_clock_epoch
        self._frame_count = 0
        clock = None if media_clock_epoch is None else self.media_time
        self.spotter = Spotter(on_spot, my_call=my_call,
                               respot_interval_s=respot_interval_s,
                               respot_tol_hz=respot_tol_hz,
                               min_snr_db=min_snr_db, clock=clock,
                               max_wpm=max_wpm, blocked=blocked_calls)
        tracker_kw = {"decoder_cls": decoder_cls} if decoder_cls else {}
        self.tracker = SignalTracker(
            n_bins=self.channelizer.fft_size,
            frame_dt=self.channelizer.frame_dt,
            sink_factory=self._make_sink,
            max_channels=max_channels,
            **tracker_kw,
        )
        # UI tap cadence, in STFT frames
        self._row_interval = max(1, round(0.064 / self.channelizer.frame_dt))
        self._chan_interval = max(1, round(1.0 / self.channelizer.frame_dt))
        self._row_accum = np.zeros(self.channelizer.fft_size, dtype=np.float64)
        self._row_count = 0
        log.info("Pipeline: fs=%d Hz, FFT=%d (%.1f Hz bins), frame=%.1f ms",
                 sample_rate, self.channelizer.fft_size,
                 self.channelizer.bin_hz, self.channelizer.frame_dt * 1000)

    def media_time(self) -> float:
        """Seconds since the epoch, counted in audio rather than wall time."""
        return self._epoch + self._frame_count * self.channelizer.frame_dt

    def _make_sink(self, channel: Channel) -> TextTap:
        def on_call(call: str, activity: str) -> None:
            freq = (self.center_frequency
                    + self.channelizer.bin_to_offset_hz(channel.bin_position))
            spot = Spot(
                freq_hz=freq,
                call=call,
                snr_db=round(channel.decoder.snr_db),
                wpm=round(channel.decoder.wpm),
                activity=activity,
            )
            if self._epoch is not None:      # stamp with audio time, not now
                spot.time_utc = self.media_time()
            self.spotter.submit(spot)

        word_cb = None
        if self.on_word is not None:
            def word_cb(word: str) -> None:
                self.on_word({
                    "freq_hz": self.center_frequency
                    + self.channelizer.bin_to_offset_hz(channel.bin_position),
                    "wpm": round(channel.decoder.wpm),
                    "snr_db": round(channel.decoder.snr_db),
                    "word": word,
                    # which tracked channel produced this. Without it, words
                    # from two channels a few hundred Hz apart are
                    # indistinguishable in the log, and the two failure
                    # mechanisms cannot be told apart: a channel REASSIGNED
                    # mid-transmission (one channel, two stations in
                    # sequence) looks the same as two stations INTERLEAVING
                    # inside one window.
                    "chan": channel.id,
                    # stamped here, on the same clock as the spots: on replay
                    # that is audio time, so the channel log lines up with the
                    # spot log and with an RBN archive. Stamping it where the
                    # line is written instead gave replay wall-clock, which
                    # made the diagnostics uncorrelatable with everything else.
                    "t": (self.media_time() if self._epoch is not None
                          else time.time()),
                })
        char_cb = None
        if self.on_char is not None:
            def char_cb(ch: str, gap: Optional[tuple]) -> None:
                self.on_char({
                    "freq_hz": self.center_frequency
                    + self.channelizer.bin_to_offset_hz(channel.bin_position),
                    "wpm": round(channel.decoder.wpm),
                    "snr_db": round(channel.decoder.snr_db),
                    "char": ch, "chan": channel.id, "gap": gap,
                    "t": (self.media_time() if self._epoch is not None
                          else time.time()),
                })
        extractor = CallsignExtractor(on_call, known=self.known_calls,
                                      patterns=self.call_patterns,
                                      known_weak=self.known_calls_weak)
        return TextTap(self.segmenter_cls(extractor),
                       on_word=word_cb, on_char=char_cb)

    def channel_snapshot(self) -> list:
        """Active decoder states (freq, wpm, snr, rolling decoded text)."""
        out = []
        for ch in self.tracker.channels.values():
            out.append({
                "freq_hz": self.center_frequency
                + self.channelizer.bin_to_offset_hz(ch.bin_position),
                "wpm": round(ch.decoder.wpm),
                "snr_db": round(ch.decoder.snr_db),
                "text": ch.sink.text if isinstance(ch.sink, TextTap) else "",
            })
        out.sort(key=lambda c: c["freq_hz"])
        return out

    def _update_coverage(self) -> None:
        ranges = self.coverage_fn()
        key = tuple(ranges) if ranges is not None else None
        if key == self._last_coverage:
            return
        self._last_coverage = key
        if ranges is None:
            self.tracker.coverage_mask = None
            return
        n = self.channelizer.fft_size
        offsets = self.channelizer.bin_to_offset_hz(np.arange(n))
        mask = np.zeros(n, dtype=bool)
        for lo, hi in ranges:
            mask |= (offsets >= lo) & (offsets <= hi)
        self.tracker.coverage_mask = mask
        log.info("Pipeline: coverage now %.0f%% of passband",
                 100.0 * mask.mean())

    def process(self, samples: np.ndarray) -> None:
        if self.coverage_fn is not None:
            self._update_coverage()
        for frame in self.channelizer.process(samples):
            self.tracker.process_frame(frame)
            self._frame_count += 1
            if self.on_spectrum is not None:
                self._row_accum += frame
                self._row_count += 1
                if self._row_count >= self._row_interval:
                    row = self._row_accum / self._row_count
                    mask = self.tracker.coverage_mask
                    if mask is not None:
                        # uncovered regions carry only filter-image leakage
                        # of live slices; blank them so the waterfall shows
                        # an honest gap instead of ghost signals
                        row = np.where(mask, row, 0.0)
                    self.on_spectrum(row)
                    self._row_accum = np.zeros_like(self._row_accum)
                    self._row_count = 0
            if (self.on_channels is not None
                    and self._frame_count % self._chan_interval == 0):
                self.on_channels(self.channel_snapshot())

    @property
    def active_channels(self) -> int:
        return len(self.tracker.channels)
