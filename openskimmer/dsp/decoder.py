"""Per-signal CW decoder: envelope -> keying -> Morse elements -> text.

One CWDecoder instance runs per tracked signal. It is fed one envelope
sample (summed power of the signal's bins) plus the local noise estimate per
STFT frame, and calls back with decoded characters and word breaks.

Classical adaptive approach (v1, requirement F9 keeps this swappable):
  - keying detection with SNR-based hysteresis threshold and debounce
  - dit length tracked adaptively from classified marks
  - spaces classified continuously (streaming), so characters flush on
    timeout without waiting for the next key-down
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable, Optional

from .morse import decode_prosign, decode_symbols


class CWDecoder:
    MAX_SYMBOLS = 8  # longest valid pattern is 6; beyond 8 it's garbage

    def __init__(self, frame_dt: float,
                 on_char: Callable[[str], None],
                 on_word_break: Callable[[], None],
                 on_snr_threshold: float = 4.0,   # power ratio over noise (~6 dB)
                 debounce_s: float = 0.008,
                 decode_prosigns: bool = False):
        self.frame_dt = frame_dt
        self.on_char = on_char
        self.on_word_break = on_word_break
        self.on_thresh = on_snr_threshold
        self.debounce_frames = max(1, round(debounce_s / frame_dt))
        # Off by default so the skimmer path is bit-identical to before. Beyond
        # the et_fraction gate, turning this on makes on_char emit "<KN>" style
        # tokens into the same stream spotting/callsign.py consumes -- a change
        # to callsign extraction's input that should be opted into, not
        # inherited. A Swift port of this decoder defaults it ON because its consumer is a
        # transcript pane with no spotting layer behind it.
        self.decode_prosigns = decode_prosigns
        # optional: on_gap(gap_ms, dit_ms, word_thr_dits), called just before
        # each on_char with the silence that preceded the character's first
        # mark. Report-only: nothing here reads it back, so the decoded text
        # is the same with or without it 
        self.on_gap: Optional[Callable[[float, float, float], None]] = None

        self.key_down = False
        self._flip_count = 0
        self._state_frames = 0

        self.dit_ms: Optional[float] = None
        self._recent_marks: deque = deque(maxlen=32)
        self._recent_gaps: deque = deque(maxlen=24)  # in dits, > intra-char
        self._raw_gaps: deque = deque(maxlen=16)     # in ms, all gaps
        self._word_thr_dits = 6.0
        self._symbols = ""
        self._char_marks: list = []   # mark durations of the open character
        self._char_gaps: list = []    # ... and the gaps between them
        # the silence before each mark of the open character, noise blips
        # included, for on_gap: _char_gaps holds the last fragment only
        self._mark_gaps: list = []
        self._gap_acc = 0.0
        self._char_open = False
        self._word_open = False
        self._char_flushed = False

        # signal statistics
        self._on_power = 0.0
        self._noise = 1e-30
        self.last_activity_frame = 0
        self._frame_idx = 0
        self.chars_total = 0
        self.chars_et = 0

    # -- public metrics ------------------------------------------------------

    @property
    def wpm(self) -> float:
        return 1200.0 / self.dit_ms if self.dit_ms else 0.0

    @property
    def snr_db(self) -> float:
        if self._on_power <= 0 or self._noise <= 0:
            return 0.0
        ratio = max(self._on_power / self._noise - 1.0, 0.1)
        return 10.0 * math.log10(ratio)

    @property
    def et_fraction(self) -> float:
        """Fraction of decoded characters that are E or T. Random noise blips
        decode almost exclusively to E/T, so a high fraction over many
        characters marks a channel as noise, not CW."""
        return self.chars_et / self.chars_total if self.chars_total else 0.0

    # -- per-frame input -----------------------------------------------------

    def process(self, envelope: float, noise: float) -> None:
        """Feed one STFT frame's envelope power and noise-floor power."""
        self._frame_idx += 1
        self._noise = 0.995 * self._noise + 0.005 * noise if self._noise > 1e-29 else noise

        # Threshold at the -6 dB point of the tracked carrier level: the STFT
        # window smears keying edges symmetrically about the half-amplitude
        # crossing, so this measures mark/space durations without bias. The
        # noise-relative floor handles the bootstrap before the carrier level
        # is known. Slow decay lets the threshold follow QSB downward.
        self._on_power *= 0.9995
        thr = max(self.on_thresh * self._noise, 0.25 * self._on_power)
        want_down = envelope > thr if not self.key_down else envelope > 0.7 * thr

        if want_down != self.key_down:
            self._flip_count += 1
            if self._flip_count >= self.debounce_frames:
                self._transition(want_down)
        else:
            self._flip_count = 0

        self._state_frames += 1
        if self.key_down:
            self._on_power = 0.9 * self._on_power + 0.1 * envelope if self._on_power else envelope
        else:
            self._check_space_timeouts()

    def _transition(self, down: bool) -> None:
        dur_ms = (self._state_frames - self._flip_count) * self.frame_dt * 1000.0
        prev_down = self.key_down
        self.key_down = down
        # carry over the debounced frames into the new state
        self._state_frames = self._flip_count
        self._flip_count = 0
        if prev_down:
            self._end_mark(dur_ms)
        else:
            self._end_gap(dur_ms)
            self._char_flushed = False

    # -- element classification ----------------------------------------------

    def _end_mark(self, dur_ms: float) -> None:
        if dur_ms < 8.0:  # noise blip even a 60 WPM dit is 20 ms
            self._gap_acc += dur_ms
            return
        # Activity = completed marks only, so isolated noise blips crossing
        # the threshold can't keep an otherwise-dead channel alive forever
        # (nor can a constant carrier that never keys).
        self.last_activity_frame = self._frame_idx
        self._char_marks.append(dur_ms)
        self._mark_gaps.append(self._gap_acc)
        self._gap_acc = 0.0
        if self.dit_ms is None:
            # The first mark of a transmission has no scale to be measured
            # against: seeding the estimate from it makes it a dit by
            # construction, whatever it really was.
            self.dit_ms = dur_ms
        # A mark much shorter than the current dit estimate means the
        # estimate was bootstrapped from dahs; rescale.
        if dur_ms < self.dit_ms * 0.55:
            self.dit_ms = dur_ms
        elif dur_ms < self.dit_ms * 1.9:
            self.dit_ms = 0.75 * self.dit_ms + 0.25 * dur_ms
        elif dur_ms < self.dit_ms * 4.5:  # ignore absurd marks for tracking
            self.dit_ms = 0.85 * self.dit_ms + 0.15 * (dur_ms / 3.0)
        self.dit_ms = min(max(self.dit_ms, 15.0), 400.0)  # 80 .. 3 WPM
        self._recent_marks.append(dur_ms)
        self._recluster()
        self._reclassify()
        self._char_open = True
        self._word_open = True
        if len(self._symbols) > self.MAX_SYMBOLS:
            self._char_marks.clear()  # garbage run; drop it
            self._char_gaps.clear()
            self._mark_gaps.clear()
            self._symbols = ""

    def _reclassify(self) -> None:
        """Re-derive the open character's symbols from the mark durations.

        Element classification is relative to `dit_ms`, and `dit_ms` can move
        under a character -- most sharply on the first two marks, where the
        estimate is bootstrapped from whatever arrived first. Deriving the
        symbols from the stored durations means a corrected estimate corrects
        the marks it has already seen, instead of discarding them.

        That discard was a silent bust factory. A transmission opening on a
        dah (K, N, D, M, T -- most US and German prefixes) had the dah
        classified as a dit, and then, when the following dit revealed the
        real scale, the whole character was cleared: the leading dah vanished
        and the character count was preserved, so N4TTU decoded as E4TTU,
        N3NS as E3NS, K9OM as A9OM. Those are legal callsigns, so every
        validation rule downstream passes them and they reach the RBN as
        confident spots (~5 % of our unconfirmed spots, measured against the
        2026-08-08/09 archives).

        A gap is scale-relative too, so the same stale estimate also hides
        character boundaries: a first character made only of dahs (M, T, O)
        has no dit to correct it, the 3x-too-large estimate makes the
        following character gap look intra-character, and two characters fuse
        into an undecodable run that is dropped whole ("M6T" -> "T"). So
        re-split the accumulated marks wherever a recorded gap has become a
        character gap under the corrected estimate.
        """
        while len(self._char_marks) > 1:
            for i, gap in enumerate(self._char_gaps):
                if gap > self.dit_ms * 2.0:
                    self._emit_char(self._symbols_for(self._char_marks[:i + 1]))
                    del self._char_marks[:i + 1]
                    del self._char_gaps[:i + 1]
                    del self._mark_gaps[:i + 1]
                    break
            else:
                break
        self._symbols = self._symbols_for(self._char_marks)

    def _symbols_for(self, marks) -> str:
        return "".join("." if d < self.dit_ms * 1.9 else "-" for d in marks)

    def _dit_from_gaps(self) -> Optional[float]:
        """A dit length read off the GAPS rather than the marks.

        The gap inside a character is one dit by definition, so the shortest
        gaps measure the dit directly -- without classifying a single mark.
        That breaks the bootstrap circularity: marks alone cannot say whether
        the first character is dits or dahs, but a mark sitting next to a gap
        of a third its length can only be a dah.

        Second-smallest of three rather than the outright minimum: the
        smallest gap present is intra-character (unless the sender used no
        multi-element character at all), but at 9 dB a fade inside a mark
        splits it and fakes a very short gap. Taking the minimum let one such
        split shrink the estimate and shatter subsequent dahs into dits --
        measured as CER 20.1 -> 24.2 on the classical decoder. Ignoring one
        outlier costs nothing and removes the regression entirely.
        """
        if len(self._raw_gaps) < 3:
            return None
        return sorted(self._raw_gaps)[1]

    def _recluster(self) -> None:
        """Escape a bad dit estimate (e.g. bootstrapped from a truncated
        mark): if recent marks form two duration clusters, the short cluster
        is the dit by definition -- snap to it when the tracked estimate
        disagrees. A ~3:1 spread with a correct estimate changes nothing.

        The gaps are consulted only while the marks are still ONE cluster --
        precisely the case the marks cannot resolve, because "all dits" and
        "all dahs" are the same distribution up to a scale factor. Once two
        mark clusters exist the short one is the dit outright and the gaps
        add nothing; letting them keep voting there only exposed the estimate
        to noise-induced splits inside a mark, which measured as a 65 %
        rise in insertions at 9 dB.
        """
        marks = sorted(self._recent_marks)
        lo = hi = None
        if len(marks) >= 8:
            lo = marks[int(0.15 * len(marks))]
            hi = marks[int(0.85 * len(marks))]
        if lo is None or hi < 2.2 * lo:
            # one cluster: a mark next to a gap a third its length can only
            # be a dah, so let the gaps break the tie
            gap_dit = self._dit_from_gaps()
            if (gap_dit is not None and self.dit_ms is not None
                    and self.dit_ms > gap_dit * 1.8):
                self.dit_ms = min(max(gap_dit, 15.0), 400.0)
            return
        split = (lo * hi) ** 0.5
        # dits is never empty: the `hi < 2.2 * lo` early return above means
        # hi >= 2.2*lo here, so split = sqrt(lo*hi) >= 1.48*lo > lo, and lo
        # itself always falls below it. That invariant is the ONLY thing
        # keeping the mean below safe -- if the 2.2 is ever lowered toward
        # 1.0, split approaches lo and this divides by zero on live audio.
        dits = [m for m in marks if m < split]
        est = sum(dits) / len(dits)
        if not 0.7 < (self.dit_ms or est) / est < 1.4:
            self.dit_ms = min(max(est, 15.0), 400.0)
            # the caller re-derives the open character's symbols from the
            # stored durations under the corrected estimate

    def _end_gap(self, dur_ms: float) -> None:
        """Track the durations of non-intra-character gaps so the word-break
        threshold adapts to the operator's actual spacing. Nominal timing is
        3 dits between characters and 7 between words, but real senders
        compress word gaps to ~4-5 dits (decoded text runs together) or
        stretch character gaps past 6 (calls fragment). If the recorded gaps
        form two clusters, the threshold sits between them.
        """
        self._gap_acc += dur_ms
        # remember where the marks of the open character sat, so a later
        # correction to the dit estimate can still find a boundary here
        if self._char_marks:
            self._char_gaps.append(dur_ms)
        # raw gap durations, for the scale-free dit estimate in
        # _dit_from_gaps(). Same 8 ms floor as marks: below that it is a
        # dropout inside a mark, not a gap the sender put there.
        if dur_ms >= 8.0:
            self._raw_gaps.append(dur_ms)
        # only gaps that the threshold must discriminate carry information:
        # longer ones (idle pauses, repeat gaps) break under any allowed
        # threshold, and as outliers they wreck the clustering
        if self.dit_ms is None or not 1.6 <= dur_ms / self.dit_ms <= 12.0:
            return
        self._recent_gaps.append(dur_ms / self.dit_ms)
        if len(self._recent_gaps) < 8:
            return
        g = sorted(self._recent_gaps)
        # 1-D 2-means: do the gaps split into char/word clusters?
        c1, c2 = g[0], g[-1]
        a = b = None
        for _ in range(8):
            mid = (c1 + c2) / 2.0
            a = [x for x in g if x < mid]
            b = [x for x in g if x >= mid]
            if not a or not b:
                break
            m1, m2 = sum(a) / len(a), sum(b) / len(b)
            if (m1, m2) == (c1, c2):
                break
            c1, c2 = m1, m2
        def _tight(cluster, center):
            sd = (sum((x - center) ** 2 for x in cluster) / len(cluster)) ** 0.5
            return sd < 0.3 * center

        if (a and b and len(a) >= 3 and len(b) >= 3 and c2 > 1.5 * c1
                and _tight(a, c1) and _tight(b, c2)):
            # clean fist, two distinct clusters: threshold between them
            thr = (c1 * c2) ** 0.5
        else:
            m = sum(g) / len(g)
            if _tight(g, m) and m > 3.8:
                # one tight high cluster: stretched character gaps; words
                # (beyond the 12-dit recording cap) lie above it
                thr = max(6.0, 1.6 * m)
            else:
                # smeared statistics (QSB dropouts fake gaps): don't guess,
                # nominal timing fragments least on noisy signals
                thr = 6.0
        self._word_thr_dits = min(max(thr, 3.5), 9.0)

    def _check_space_timeouts(self) -> None:
        if self.dit_ms is None:
            return
        gap_ms = self._state_frames * self.frame_dt * 1000.0
        if self._char_open and not self._char_flushed and gap_ms > self.dit_ms * 2.0:
            self._flush_char()
        if self._word_open and gap_ms > self.dit_ms * self._word_thr_dits:
            self._word_open = False
            self.on_word_break()

    def _flush_char(self) -> None:
        self._char_flushed = True
        self._char_open = False
        self._emit_char(self._symbols)
        self._symbols = ""
        self._char_marks.clear()
        self._char_gaps.clear()
        self._mark_gaps.clear()

    def _emit_char(self, symbols: str) -> None:
        ch = decode_symbols(symbols)
        if ch:
            self.chars_total += 1
            if ch in ("E", "T"):
                self.chars_et += 1
            self._report_gap()
            self.on_char(ch)
            return
        # Prosigns are transcript output only. They are emitted WITHOUT touching
        # chars_total or chars_et, so et_fraction -- and the channel-retirement
        # gate in tracker.py that reads it -- behaves exactly as if the prosign
        # had been dropped, which is what happened before this existed.
        if self.decode_prosigns:
            prosign = decode_prosign(symbols)
            if prosign:
                self._report_gap()
                self.on_char(prosign)

    def _report_gap(self) -> None:
        # called before the open character's marks are cleared, so
        # _mark_gaps[0] is the silence before this character
        if self.on_gap is not None and self._mark_gaps:
            self.on_gap(self._mark_gaps[0], self.dit_ms or 0.0,
                        self._word_thr_dits)
