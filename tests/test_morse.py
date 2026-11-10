from openskimmer.dsp.morse import CODE, decode_symbols, text_to_keying


def test_roundtrip_alphabet():
    for ch, code in CODE.items():
        assert decode_symbols(code) == ch


def test_unknown_pattern():
    assert decode_symbols("......--") == ""


def test_keying_timing():
    # "EE" = dit, char-gap(3), dit
    assert text_to_keying("EE") == [(True, 1), (False, 3), (True, 1)]
    # "E E" = dit, word-gap(7), dit
    assert text_to_keying("E E") == [(True, 1), (False, 7), (True, 1)]
    # "A" = dit, gap, dah
    assert text_to_keying("A") == [(True, 1), (False, 1), (True, 3)]
