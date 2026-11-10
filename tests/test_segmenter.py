"""Word segmenters between the decoder and the extractor."""

from openskimmer.spotting.callsign import CallsignExtractor
from openskimmer.spotting.segmenter import TimingSegmenter, WidestGapSegmenter


class Words:
    """Collects what a segmenter passes on: (word, cut)."""

    def __init__(self):
        self.words, self._w = [], ""

    def add_char(self, ch):
        self._w += ch

    def add_word_break(self, cut=False):
        self.words.append((self._w, cut))
        self._w = ""


def feed(seg, word, gaps, dit_ms=50.0):
    """One decoder word: gaps[i] is the gap before character i, in dits."""
    for ch, g in zip(word, gaps):
        seg.add_gap(g * dit_ms, dit_ms, 6.0)
        seg.add_char(ch)
    seg.add_word_break()


def test_timing_passes_the_decoders_words_through():
    out = Words()
    feed(TimingSegmenter(out), "CQPOTADE", [7, 3, 4, 3, 3, 3, 4, 3])
    assert out.words == [("CQPOTADE", False)]


def test_widest_gap_cuts_where_the_sender_left_a_word_gap():
    # NN1RR sends word gaps of 4.5 dits against character gaps of ~3:
    # the decoder kept "NN1RRTEST" whole
    out = Words()
    feed(WidestGapSegmenter(out), "NN1RRTEST", [8, 3.0, 2.9, 3.1, 3.0, 4.5, 3.0, 3.1, 2.9])
    assert out.words == [("NN1RR", True), ("TEST", True)]


def test_no_cut_when_no_gap_stands_out():
    # KB1WCK's CQ POTA gap (2.4 dits) is no wider than its letters' gaps
    out = Words()
    feed(WidestGapSegmenter(out), "CQPOTADE", [7, 2.8, 2.4, 3.3, 3.2, 2.7, 2.8, 2.8])
    assert out.words == [("CQPOTADE", False)]
    # nor at a gap that is widest but still a character gap
    out = Words()
    feed(WidestGapSegmenter(out), "K1ABCDE", [7, 2.0, 2.0, 3.1, 2.0, 2.0, 2.0])
    assert out.words == [("K1ABCDE", False)]


def test_a_cut_piece_is_not_a_copy():
    # KA9ZAP with the gap before its AP the widest (POTA 2026-10-15): the
    # cut leaves KA9Z, which must not spot on its own
    calls = []
    x = CallsignExtractor(lambda c, a: calls.append((c, a)))
    seg = WidestGapSegmenter(x)
    for _ in range(3):
        feed(seg, "CQ", [8, 3])
        feed(seg, "KA9ZAP", [7, 3, 3, 3, 4.2, 3])
    assert ("KA9Z", "CQ") not in calls


def test_a_cut_piece_confirms_a_call_copied_cleanly_before():
    calls = []
    x = CallsignExtractor(lambda c, a: calls.append((c, a)))
    seg = WidestGapSegmenter(x)
    feed(seg, "KB1WCK", [9, 3, 3, 3, 3, 3])              # a clean copy
    # "CQ POTA DE" run onto the call, the gap before the call the widest
    feed(seg, "CQPOTADEKB1WCK", [8, 3, 4, 3, 3, 3, 3, 3, 5.5, 3, 3, 3, 3, 3])
    assert ("KB1WCK", "CQ") in calls
