"""International Morse code tables and helpers."""

from __future__ import annotations

from typing import List, Tuple

CODE = {
    "A": ".-", "B": "-...", "C": "-.-.", "D": "-..", "E": ".", "F": "..-.",
    "G": "--.", "H": "....", "I": "..", "J": ".---", "K": "-.-", "L": ".-..",
    "M": "--", "N": "-.", "O": "---", "P": ".--.", "Q": "--.-", "R": ".-.",
    "S": "...", "T": "-", "U": "..-", "V": "...-", "W": ".--", "X": "-..-",
    "Y": "-.--", "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--", "4": "....-",
    "5": ".....", "6": "-....", "7": "--...", "8": "---..", "9": "----.",
    "/": "-..-.", "?": "..--..", ".": ".-.-.-", ",": "--..--", "=": "-...-",
    "+": ".-.-.", "-": "-....-", "@": ".--.-.",
}

DECODE = {v: k for k, v in CODE.items()}


# Prosign patterns that no single character claims. Deliberately NOT in CODE:
# every entry there feeds chars_total, which feeds et_fraction, which gates
# channel retirement in tracker.py -- see CWDecoder._emit_char. Adding these to
# CODE would raise chars_total without raising chars_et, weakening that gate on
# exactly the noisy channels it exists to retire.
#
# BT and AR are absent because they need no entry: their patterns are identical
# to "=" and "+", which CODE already decodes and correctly counts.
PROSIGNS = {
    "...-.-": "<SK>",
    "-.--.": "<KN>",
    "-...-.-": "<BK>",
}


def decode_prosign(symbols: str) -> str:
    """Decode a prosign pattern; '' if it is not one. Transcript use only."""
    return PROSIGNS.get(symbols, "")


def decode_symbols(symbols: str) -> str:
    """Decode one character's dit/dah string; '' for unknown patterns."""
    return DECODE.get(symbols, "")


def text_to_keying(text: str) -> List[Tuple[bool, int]]:
    """Convert text to a keying sequence of (key_down, duration_units).

    Standard timing: dit=1, dah=3, intra-char gap=1, char gap=3, word gap=7.
    Used by the synthetic source and by tests.
    """
    out: List[Tuple[bool, int]] = []
    first_char = True
    for word in text.upper().split():
        if not first_char:
            out.append((False, 7))
        for ci, ch in enumerate(word):
            code = CODE.get(ch)
            if code is None:
                continue
            if ci > 0:
                out.append((False, 3))
            for si, sym in enumerate(code):
                if si > 0:
                    out.append((False, 1))
                out.append((True, 1 if sym == "." else 3))
        first_char = False
    return out
