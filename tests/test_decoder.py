"""CWDecoder timing tests: adaptive word-gap classification.

Drives the decoder directly with frame-level keying built from custom
element timing, so operator habits (compressed word gaps, stretched
character gaps) can be simulated exactly.
"""

from openskimmer.dsp.decoder import CWDecoder
from openskimmer.dsp.morse import CODE

FRAME_DT = 0.005
UNIT_FRAMES = 10  # 50 ms dit ~ 24 WPM


def keying(text, char_gap=3, word_gap=7):
    """(key_down, units) sequence with configurable gap timing."""
    out = []
    first = True
    for word in text.upper().split():
        if not first:
            out.append((False, word_gap))
        for ci, ch in enumerate(word):
            if ci > 0:
                out.append((False, char_gap))
            for si, sym in enumerate(CODE[ch]):
                if si > 0:
                    out.append((False, 1))
                out.append((True, 1 if sym == "." else 3))
        first = False
    return out


def decode(units):
    words = []
    chars = []

    def on_char(c):
        chars.append(c)

    def on_break():
        if chars:
            words.append("".join(chars))
            chars.clear()

    dec = CWDecoder(FRAME_DT, on_char=on_char, on_word_break=on_break)
    for down, n in units:
        for _ in range(n * UNIT_FRAMES):
            dec.process(100.0 if down else 0.01, 1.0)
    for _ in range(12 * UNIT_FRAMES):  # flush the tail
        dec.process(0.01, 1.0)
    on_break()
    return words


def test_standard_spacing():
    words = decode(keying("CQ CQ DE K4RLC K4RLC K"))
    # (the very first character may garble while the dit estimate bootstraps)
    assert words[-5:] == ["CQ", "DE", "K4RLC", "K4RLC", "K"]


def test_compressed_word_gaps_split_after_adaptation():
    # ~5-dit word gaps fuse everything under the fixed 6-dit rule
    # ("CQCQDEK4RLCK4RLCK" seen live); the gap clusters (3 vs 5) must be
    # learned so later words separate.
    text = "CQ CQ DE K4RLC K4RLC K " * 3
    words = decode(keying(text.strip(), char_gap=3, word_gap=5))
    assert "K4RLC" in words
    # the tail end must be fully separated
    assert words[-4:] == ["DE", "K4RLC", "K4RLC", "K"] or \
           words[-3:] == ["K4RLC", "K4RLC", "K"]


def test_stretched_char_gaps_do_not_fragment():
    # 7-dit character gaps fragment every character under the fixed 6-dit
    # rule ("K 1 N MF" seen live); with 16-dit word gaps the clusters are
    # separable and later words must come out whole.
    text = "CQ CQ DE K1NMF K1NMF K " * 3
    words = decode(keying(text.strip(), char_gap=7, word_gap=16))
    assert "K1NMF" in words


def test_leading_dah_survives_the_dit_bootstrap():
    """A transmission opening on a dah must not lose it.

    The first mark has no scale to be measured against, so the dit estimate
    is seeded from it and it classifies as a dit by construction. The
    following dit then reveals the real scale. Re-deriving the character
    from the stored durations recovers the dah; discarding them (the old
    behaviour) deleted it silently and left a legal callsign one character
    shorter at the front: N4TTU -> E4TTU, N3NS -> E3NS, K9OM -> A9OM.
    """
    for call in ("N4TTU", "N3NS", "K9OM", "DL5YM", "KE4TWI", "M6T", "G3P"):
        assert decode(keying(call)) == [call], call


def test_dah_only_first_character_does_not_fuse():
    """A first character with no dit in it must still end where it ends.

    The gap thresholds are scale-relative too, so the same 3x-too-large
    bootstrap estimate hid the character boundary after M/T/O and fused two
    characters into an undecodable run that was dropped whole ("M6T" -> "T",
    "OS1A" -> "1A"). The correction has to reach back over marks already
    accumulated AND re-split them at the recorded gaps.
    """
    for call in ("M6T", "T9AB", "OS1A", "OK1RI", "OM2VL", "TM4M"):
        assert decode(keying(call)) == [call], call


def test_scale_comes_from_the_gaps_when_no_dit_is_sent():
    """Calls with no dit at all still decode, using the inter-element gaps.

    "MM0T" is -- -- ----- -, so the MARKS carry no scale reference: they are
    the same distribution as a string of Es up to a scale factor. The gaps
    settle it, because the gap inside a character is one dit by definition,
    so a mark three times its neighbouring gap can only be a dah.

    This is not just the pathological case. Without it the decoder missed 103
    calls in MASTER.SCP whose first two characters contain no dit -- the
    whole UK M0/MM0 population, plus OM0, OK0, TM0 and friends -- because the
    estimate stayed 3x too large across both characters.
    """
    for call in ("MM0T", "O0O", "M0GLV", "MM0KKC", "OM0M", "M0TDW", "TM0HQ"):
        assert decode(keying(call)) == [call], call


def keying_with_prosign(text, pattern, char_gap=3, word_gap=7):
    """`text`, then a word gap, then one raw dit/dah `pattern`.

    keying() can only emit patterns CODE claims, and the whole point of a
    prosign is that CODE claims none -- so the pattern is appended by hand.
    Real text goes first so the dit estimate is bootstrapped before the
    prosign arrives, exactly as it would be on the air.
    """
    out = keying(text, char_gap, word_gap)
    out.append((False, word_gap))
    for si, sym in enumerate(pattern):
        if si > 0:
            out.append((False, 1))
        out.append((True, 1 if sym == "." else 3))
    return out


def decode_full(units, **kw):
    """decode(), but returns (words, decoder) so statistics can be inspected."""
    words, chars = [], []

    def on_break():
        if chars:
            words.append("".join(chars))
            chars.clear()

    dec = CWDecoder(FRAME_DT, on_char=chars.append, on_word_break=on_break, **kw)
    for down, n in units:
        for _ in range(n * UNIT_FRAMES):
            dec.process(100.0 if down else 0.01, 1.0)
    for _ in range(12 * UNIT_FRAMES):
        dec.process(0.01, 1.0)
    on_break()
    return words, dec


def test_prosigns_do_not_enter_the_character_statistics():
    """A prosign must reach the transcript without moving et_fraction.

    chars_total feeds et_fraction, which gates channel retirement in
    tracker.py, so a decoded prosign must count for nothing there -- exactly
    as the dropped prosign it replaces counted for nothing. This is the whole
    reason prosigns live outside CODE; see morse.PROSIGNS.
    """
    for pattern, token in (("-.--.", "<KN>"),
                           ("...-.-", "<SK>"),
                           ("-...-.-", "<BK>")):
        units = keying_with_prosign("W1AW", pattern)
        off_words, off = decode_full(units, decode_prosigns=False)
        on_words, on = decode_full(units, decode_prosigns=True)

        assert off.chars_total == on.chars_total, pattern
        assert off.chars_et == on.chars_et, pattern
        assert token in " ".join(on_words), (pattern, on_words)
        assert token not in " ".join(off_words), (pattern, off_words)


def test_prosigns_default_off_keeps_the_skimmer_path_unchanged():
    """The flag must default off: on_char feeds spotting/callsign.py."""
    units = keying_with_prosign("W1AW", "-.--.")
    default_words, default_dec = decode_full(units)
    off_words, off_dec = decode_full(units, decode_prosigns=False)
    assert default_words == off_words
    assert default_dec.chars_total == off_dec.chars_total


def test_bt_and_ar_stay_ordinary_characters():
    """BT and AR share patterns with '=' and '+', which CODE already counts.

    They must NOT be diverted into the prosign path, or two characters that
    legitimately feed chars_total would stop doing so.
    """
    from openskimmer.dsp.morse import PROSIGNS, CODE
    assert CODE["="] not in PROSIGNS
    assert CODE["+"] not in PROSIGNS
    words, dec = decode_full(keying("K1ABC = K"), decode_prosigns=True)
    assert "=" in " ".join(words)
    assert dec.chars_total > 0
