"""Word segmentation between the decoder and the callsign extractor.

The decoder breaks words on one threshold per channel (CWDecoder._end_gap),
and when a sender's word gaps overlap their character gaps no threshold is
right: "NN1RR NN1RR TEST" comes out as one word. A word the decoder made can
still hold the sender's real boundaries, visible as gaps clearly wider than
the rest inside it.

Segmenters sit where the extractor would, taking add_char / add_gap /
add_word_break, and pass words on to the extractor. "timing" passes the
decoder's words through unchanged, today's behaviour.
"""

from __future__ import annotations

from typing import List, Optional, Tuple


class TimingSegmenter:
    """The decoder's words, unchanged."""

    def __init__(self, inner):
        self.inner = inner

    def add_gap(self, gap_ms: float, dit_ms: float, word_thr: float) -> None:
        pass

    def add_char(self, ch: str) -> None:
        self.inner.add_char(ch)

    def add_word_break(self) -> None:
        self.inner.add_word_break()


class WidestGapSegmenter:
    """Cut a decoder word at a gap clearly wider than every other inside it.

    Inside one decoder word every gap fell under the word threshold, but a
    sender who compresses word gaps still leaves them wider than their own
    character gaps (measured 2026-10-22 on four CWT hours: about 10 missed
    calls an hour sat inside longer words with both edges wider than any
    gap within them). A word is cut at its widest gap when that gap is at
    least `min_dits` and `ratio` times every other gap in the word, and the
    pieces are cut again the same way. The word is complete when the
    decoder breaks it, so this adds no delay.
    """

    def __init__(self, inner, ratio: float = 1.15, min_dits: float = 3.3,
                 min_len: int = 5, copy_ratio: float = float("inf")):
        self.inner = inner
        self.ratio = ratio
        # cuts this much wider than every other gap make pieces that count
        # as copies; below it a piece is evidence only
        self.copy_ratio = copy_ratio
        self.min_dits = min_dits
        self.min_len = int(min_len)
        self._word: List[Tuple[str, float]] = []   # (char, gap before, dits)
        self._gap: Optional[float] = None

    def add_gap(self, gap_ms: float, dit_ms: float, word_thr: float) -> None:
        self._gap = gap_ms / dit_ms if dit_ms else None

    def add_char(self, ch: str) -> None:
        self._word.append((ch, self._gap if self._gap is not None else 0.0))
        self._gap = None

    def add_word_break(self) -> None:
        self._weakest = float("inf")
        pieces = self._split(self._word)
        cut = len(pieces) > 1 and self._weakest < self.copy_ratio
        for piece in pieces:
            for ch, _ in piece:
                self.inner.add_char(ch)
            self.inner.add_word_break(cut=cut)
        self._word = []

    def _split(self, word):
        if len(word) < self.min_len:
            return [word] if word else []
        # gaps inside the word: before characters 1..n-1
        inner = [g for _, g in word[1:]]
        i = max(range(len(inner)), key=inner.__getitem__)
        widest = inner[i]
        rest = inner[:i] + inner[i + 1:]
        if widest < self.min_dits or (rest and widest < self.ratio * max(rest)):
            return [word]
        self._weakest = min(self._weakest,
                            widest / max(rest) if rest and max(rest) > 0 else 9.9)
        cut = i + 1
        return self._split(word[:cut]) + self._split(word[cut:])


SEGMENTERS = {"timing": TimingSegmenter, "widest": WidestGapSegmenter}
