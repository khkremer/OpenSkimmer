"""Callsign extraction from a per-signal decoded text stream (F2).

Watches the token stream of one decoded signal for standard CW QSO/contest
grammar: "CQ <call>", "CQ TEST <call>" and "DE <call>". Every spot needs that
context plus enough clean copies of the call (see CallsignExtractor); a call
repeated with no context never spots. A CQ belongs only to the first call
after it (cq_context), so a caller in the runner's pileup is not a CQ.

Real decodes fragment and fuse words: operators stretch character spacing so
calls split ("K1N MF", "W C5WC"), and tight spacing fuses the call onto the
preceding prosign ("DEWC5WC", "CQW1AW") or the following exchange
("W1RW5NN"). Candidates are therefore built from merged token spans and
prefix/suffix-stripped forms as well as from the bare token. Repaired
candidates need the same context as plain ones and never count as an
independent copy.
"""

from __future__ import annotations

import re
from collections import OrderedDict, deque
from functools import lru_cache
from typing import Callable, List, Optional, Tuple

from openskimmer.dsp.morse import CODE

# base call: optional prefix/, 1-3 char prefix ending in digit, suffix ending
# in a letter; optional /P, /QRP, /7 etc.
_CALL_RE = re.compile(
    r"^(?:[A-Z0-9]{1,3}/)?"
    r"[A-Z0-9]{1,2}[0-9][A-Z0-9]{0,4}[A-Z]"
    r"(?:/[A-Z0-9]{1,4})?$"
)

# tokens that pattern-match the regex loosely or show up constantly in QSOs:
# grammar words, prosigns, and common abbreviations (also barred from merges)
_NOT_CALLS = frozenset({
    "CQ", "DE", "TEST", "QRZ", "TU", "UR", "RST", "AGN", "5NN", "599",
    "72", "73", "88", "DX", "WPX", "K", "E", "T",
    "BK", "KN", "AR", "SK", "ES", "PSE", "OM", "GL", "FB", "HW", "NR",
    "QTH", "QSL", "QRL", "QSB", "QRM", "RTU", "GM", "GA", "GE", "GN",
    # activation/award programs called out the same way as CQ ("CQ POTA
    # DE <call>"): POTA and SOTA are the common ones, but the "*OTA" family
    # is bigger than those two, plus WWFF (not "OTA"-shaped but the same
    # role). None of these can collide with a real call -- like every other
    # entry here, is_valid_call() rejects them on the missing digit alone --
    # so adding one is pure upside for all_boilerplate() and the
    # span-merge exclusion below, never a risk of excluding a real station.
    "POTA", "SOTA", "IOTA", "BOTA", "COTA", "WWFF",
    # the CWops weekly test, sent like TEST: "CQ CWT DL8LAS"
    "CWT",
})

# how many words before a call are searched for its CQ/DE context
CONTEXT_WORDS = 3

# how many words a call split by spurious word breaks may be merged from,
# longest first: a slow fist sends every letter as its own word
# ("DE N 3 H X Z CQ SOTA"), and merging only the last three spotted N3H at
# the H (2026-10-23: 35 of one skimmer's 72 RBN busts were such pieces)
MERGE_SPANS = (6, 5, 4, 3, 2)
# a merge cut short is vetoed by a longer one whose extra pieces are this
# short (see CallsignExtractor._cut_short): single letters, or up to two
# characters when the call itself has four or fewer
TAIL_PIECE = 1
SHORT_CALL_TAIL_PIECE = 2


def _runs_into_repeat(parts) -> bool:
    """A merge that runs on into the next copy of the call: "VE2 N C V"
    -> VE2NC + V, "M0 KTC M" -> M0KTC + M."""
    head = ""
    for k in range(len(parts) - 1):
        head += parts[k]
        tail = "".join(parts[k + 1:])
        if is_valid_call(head) and head.startswith(tail):
            return True
    return False


# a call this many marks or fewer from one the signal is established on is
# taken as a garble of it (mark_distance)
GARBLE_MARKS = 2


# activation programs, sent where a contest sends TEST: "POTA KT5E" is
# KT5E's CQ even when the CQ in front of it was lost (UberSDR reads it so)
_PROGRAMS = ("POTA", "SOTA", "WWFF")


def is_cq_word(token: str) -> bool:
    """CQ, TEST or POTA, also when the decoder ran it into the words around it.

    Missing a word gap fuses "CQ CQ DE" into "CQCQDE" and "CQ CQ POTA DE"
    into "CQCQPOTADE". POTA activators on 40 m (2026-10-15) came out that
    way while UberSDR heard the gaps on the same audio, and with no bare
    "CQ" before it a call that was copied cleanly four times (WA8VTD) never
    spotted. A token that is ALL boilerplate and contains a CQ is still a CQ.
    """
    if token in ("CQ", "TEST") or token in _PROGRAMS:
        return True
    return (("CQ" in token or any(p in token for p in _PROGRAMS))
            and all_boilerplate(token))


def holds_other_call(token: str, cand: str) -> bool:
    """Does this word carry a call other than cand, even fused into it?

    Besides a plain call: the runner's call sent twice with the gap lost
    ("CQ CWT DD2MLDD2ML N2EY") or run into its own CQ words
    ("F6IRACWTTEST K4YYL"). Either still means the next call is answering.
    A "?" (a word the decoder could not read) is taken as one too: in
    "CQ D ? N3KI TU MIKE" it stands where the runner's call was.
    """
    if token == cand:
        return False
    if token == "?" or is_valid_call(token):
        return True
    half = len(token) // 2
    if (len(token) % 2 == 0 and token[:half] == token[half:]
            and token[:half] != cand and is_valid_call(token[:half])):
        return True
    for i in range(3, len(token)):
        head, tail = token[:i], token[i:].replace("CWT", "")
        if (head != cand and is_valid_call(head)
                and (not tail or all_boilerplate(tail))):
            return True
    # ... or with its CQ words run onto its front: in "CQ CWTDL8LAS K0AF" the
    # CQ is DL8LAS's and K0AF is calling it (CWT 2026-10-21)
    for i in range(2, len(token) - 2):
        head, tail = token[:i], token[i:]
        if tail != cand and all_boilerplate(head) and is_valid_call(tail):
            return True
    return False


def rejoin_boilerplate(words: List[str]) -> List[str]:
    """Join the words before a call that only together read as boilerplate.

    Word gaps that fall in the wrong place split the context, not just fuse
    it: on 2026-09-16 KS4CR's "CQ POTA DE KS4CR" decoded as "CQP OTADE
    KS4CR" and "CQP O TADE KS4CR", where no single word is a CQ or a DE, and
    UberSDR, splitting words by their text, read it right on the same audio.
    The longest run of two or more words at the end that joins into pure
    boilerplate ("CQPOTADE" = CQ POTA DE) replaces those words, so the fused
    forms handled by is_cq_word and the DE check apply to it. Only context
    is rebuilt this way; a call is never made out of the joined text.
    """
    for k in range(len(words), 1, -1):
        run = words[-k:]
        if any(not w or w == "?" or is_valid_call(w) for w in run):
            continue
        # words that already read on their own ("TEST E") stay as they are:
        # joining them would only hide the TEST in "TESTE"
        if all(all_boilerplate(w) for w in run):
            continue
        joined = "".join(run)
        if all_boilerplate(joined) and ("CQ" in joined or "TEST" in joined
                                        or joined.endswith("DE")):
            return list(words[:-k]) + [joined]
    return list(words)


def cq_context(pre: List[str], cand: str, lead: str = "",
               owns_test: Optional[Callable[[str], bool]] = None,
               adjacent: bool = False) -> bool:
    """Is cand the station calling the CQ in `pre` (the words before it)?

    A CQ word counts only when no OTHER call sits between it and cand.
    In a zero-beat pileup the runner and its callers share one channel, so
    "CQ CWT K1GU W8FN" reaches one extractor: K1GU called CQ, W8FN is
    answering it. Taking any CQ in the last few words labelled W8FN a CQ
    too, which put this skimmer at 34-41 % caller-as-runner spots on K1MK's
    measure (Makrothen and CQ WW 2026, field median 4 %).

    A TEST straight after another station's call is that station's:
    runners that end their CQ with it ("CWT NA2U NA2U TEST N5ER", CWT
    2026-09-16) are answered right after it. `owns_test(word)` says whether
    the word before a TEST is such a station; the caller makes it strict,
    because the runner's own call garbled into a call-shaped scrap ("IA3C
    TEST 9 A3YT", "E2U TEST CWT NA2U") sits there just as often. `lead` is
    the word before `pre`, so a TEST at the start of the window can be
    checked too.
    """
    words = [lead] + list(pre)
    for i in range(len(words) - 1, 0, -1):
        t = words[i]
        if is_cq_word(t):
            return not ("TEST" in t and owns_test is not None
                        and owns_test(words[i - 1]))
        # the CQ with this very call fused onto its end: "CQCQPOTADENZ1J"
        # before a clean "NZ1J". These POTA fists send word gaps of 3-4.5
        # dits, overlapping their own character gaps (2.4-4.0), so no word
        # threshold can split them (tools/gap_trace.py, 40 m 2026-10-15).
        # The call itself still comes only from the clean copy; the fused
        # token only lends it the CQ.
        if (t != cand and t.endswith(cand) and len(t) > len(cand)
                and is_cq_word(t[:-len(cand)])):
            return True
        if holds_other_call(t, cand):
            return False
        # with `adjacent`, only CQ words may stand between the CQ and cand
        if adjacent and not (t in ("DE", "CWT") or (all_boilerplate(t) and (
                "CQ" in t or "TEST" in t or t.endswith("DE")))):
            return False
    return False

# report fragments that fuse onto the end of a call ("W1RW5NN")
_EXCHANGE_TAILS = ("5NN", "599")

# prefixes whose Patt3Ch shape IS listed (real historical activity exists
# somewhere under it), which is normally enough to spot on one confirming
# copy -- but that have been confirmed, specifically, to produce false spots
# in practice, so pattern-tier trust does more harm than good for them.
#
# T5 (Somalia): T5TE, T5AT, T5IT all shape-match the listed "T5@@" pattern
# and all spotted live (2026-09-09..14, 80/40/17m) on the strength of that
# match alone -- none is in MASTER.SCP or combined.txt, and none corresponds
# to any real QSO partner. The RBN daily archive shows other independent
# skimmers spotting the same shapes (WT8P/VE6WZ "T5TE", NG7M/W6YX "ET5TE" on
# 14086 kHz) at the same times on multiple days, so this is not one channel's
# decode error -- whatever is on those frequencies produces this shape
# reliably enough to clear the normal repetition bar (see
# CallsignExtractor.__doc__) day after day. A database entry always wins
# regardless of this list (see the `known` check below), so a real T5
# station remains spottable once one is confirmed and added.
_UNTRUSTED_PREFIXES = ("T5",)


def load_scp(path: str) -> frozenset:
    """Load a Super Check Partial callsign list (MASTER.SCP: one call per
    line, '#' comments) as used by contest loggers; supercheckpartial.com."""
    calls = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().upper()
            if line and not line.startswith("#"):
                calls.add(line)
    return frozenset(calls)


# suffixes a station actually signs with, beyond a single district digit
_PORTABLE_QUALIFIERS = frozenset({
    "P", "M", "MM", "AM", "A", "R", "B", "J", "QRP", "LH", "BCN",
})


def _base_index(parts) -> int:
    """Which "/"-separated part is the operator's own call: the longest, and
    on a tie the LAST -- "VP2E/W1AW" is prefix/homecall, the ITU convention
    for signing abroad, so W1AW is the base even though both parts are four
    characters."""
    best = max(len(p) for p in parts)
    return max(i for i, p in enumerate(parts) if len(p) == best)


def _plausible_prefix(part: str) -> bool:
    """Could this be a DXCC prefix, as in "F/W1AW" or "VE3/W1AW"?

    Real prefixes are one or two characters (F, DL, CO, 9A), or longer only
    with a digit in them (VE3, KH6, KP4, VP2E, 3B8). Three letters and no
    digit -- "KWC" in the observed "KWC/KW7Q" -- is not a prefix any country
    issues, and in practice is decoded rubbish stuck to the front of a real
    call.
    """
    if not part or not part.isalnum():
        return False
    return len(part) <= 2 or any(c.isdigit() for c in part)


def conventional_affix(call: str) -> bool:
    """True if the "/" affix is one stations really sign with.

    A SUFFIX is a district digit ("W1AW/5") or a standard qualifier ("/P",
    "/M", "/MM", "/QRP"). A PREFIX is the conventional way to sign abroad
    ("F/W1AW", "CO/W1AW") and is always allowed.

    Anything else -- "W1AW/CO", "W1AW/VE3" -- is far more often the next
    word's first letters fused onto the call than a real signing, and must
    not inherit the base call's database entry: base_call() normalises
    W1AW/CO to W1AW, so without this an arbitrary garbled suffix is blessed
    as "known" and spots on a single copy. Such a call can still spot, it
    just has to earn it through the normal confirmation rules.
    """
    parts = call.split("/")
    if len(parts) == 1:
        return True
    base_i = _base_index(parts)
    for i, part in enumerate(parts):
        if i == base_i:            # the base call itself
            continue
        if i < base_i:             # a prefix -- but it must look like one
            if not _plausible_prefix(part):
                return False
            continue
        if part in _PORTABLE_QUALIFIERS:
            continue
        if len(part) == 1 and part.isdigit():
            continue
        return False
    return True


def base_call(call: str) -> str:
    """The identifying part of a call, without portable prefixes/suffixes:
    W4MQC/1 -> W4MQC, F/W1AW -> W1AW. Callsign databases list base calls, so
    lookups must normalise or every portable operator misses the database."""
    parts = call.split("/")
    return parts[_base_index(parts)]


def _morse_symbols(call: str) -> str:
    """Concatenated dit/dah symbols for a call, with NO element, character
    or word gaps -- just the raw marks in sending order."""
    return "".join(CODE.get(c, "") for c in call)


def is_morse_dropout(short: str, long: str) -> bool:
    """True if `short` is exactly what a decoder produces when it starts
    listening partway through `long`'s transmission: it loses whatever
    marks came before it tuned in, and re-segments the marks that survive
    into different, still valid-looking characters. This is a common onset
    failure (see decoder.py's "leading dah"/bootstrap comments) and it is
    invisible to a text-level substring check, because the dropped marks
    are not a whole number of characters.

    Observed live 2026-09-16: RW6A spotted, then T6A on the same frequency
    seconds later. R=.-. W=.-- 6=-.... A=.- ; drop R entirely and W's first
    two marks, and the one mark left of W (a dah) plus 6 and A re-segment
    exactly as T(-) 6(-....) A(.-) = "T6A" -- a perfect suffix match on the
    raw symbol stream, not a coincidence of similar-looking text.
    """
    if len(short) >= len(long):
        return False
    a, b = _morse_symbols(short), _morse_symbols(long)
    return bool(a) and b.endswith(a)


@lru_cache(maxsize=4096)
def mark_distance(a: str, b: str, limit: int = 99) -> int:
    """Edit distance between two calls' raw marks, character gaps ignored.

    The decoder's commonest runner garbles are the same keying with one
    mark lost or the letter breaks moved: K0SN -> K0STE (N -. = T E) and
    TA0SN (K -.- = T A) are 0 marks apart, EA5M -> EA5T (M -- , T -) and
    WU6P -> WU6W (P .--. , W .--) are 1. As text they differ by up to two
    characters, while two different stations rarely come this close.

    Anything over `limit` comes back as limit + 1, computed only along the
    diagonal band it can be reached in: this runs for every candidate on
    every signal.
    """
    x, y = _morse_symbols(a), _morse_symbols(b)
    if abs(len(x) - len(y)) > limit:
        return limit + 1
    big = limit + 1
    prev = [j if j <= limit else big for j in range(len(y) + 1)]
    for i, p in enumerate(x, 1):
        cur = [i if i <= limit else big] + [big] * len(y)
        for j in range(max(1, i - limit), min(len(y), i + limit) + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (p != y[j - 1]), big)
        if min(cur) > limit:
            return big
        prev = cur
    return min(prev[-1], big)


def call_pattern(call: str) -> str:
    """CW Skimmer's Patt3Ch pattern of a call: the identifying prefix stays
    literal, the rest becomes # (any digit) / @ (any letter). Prefix length
    is 3, except 2 for letter-digit-letter calls (N4ZR -> N4@@) and 4 for
    digit-letter-letter calls (3DA0RU -> 3DA0@@)."""
    base = max(call.split("/"), key=len)
    n = 3
    if len(base) >= 3:
        if base[0].isalpha() and base[1].isdigit() and base[2].isalpha():
            n = 2
        elif base[0].isdigit() and base[1].isalpha() and base[2].isalpha():
            n = 4
    return base[:n] + "".join("#" if c.isdigit() else "@" for c in base[n:])


def load_patterns(path: str) -> frozenset:
    """Load a Patt3Ch.lst callsign-pattern file (from reversebeacon.net).
    Lines carry a leading confidence flag ('+' or blank), which is ignored:
    membership at all means the pattern occurs in real callsigns."""
    pats = set()
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            tok = line.strip().upper().lstrip("+").strip()
            if tok:
                pats.add(tok)
    return frozenset(pats)


@lru_cache(maxsize=8192)
def all_boilerplate(token: str) -> bool:
    """True if the token is entirely concatenated CW boilerplate -- prosigns,
    abbreviations, and signal reports (e.g. "TU5NN" = TU + 5NN, "DEK" = DE + K)
    -- so its call-shaped appearance is a word-fusion artifact, not a real
    station. A word-break over the _NOT_CALLS vocabulary: reachable[i] means the
    first i characters split cleanly into boilerplate pieces."""
    n = len(token)
    reachable = [True] + [False] * n
    for i in range(n):
        if not reachable[i]:
            continue
        for w in _NOT_CALLS:
            j = i + len(w)
            if j <= n and token[i:j] == w:
                reachable[j] = True
    return reachable[n]


def is_valid_call(token: str) -> bool:
    if len(token) < 3 or token in _NOT_CALLS:
        return False
    # A portable suffix beginning with a digit is a district change and is
    # exactly one digit ("W1AW/5"). "W1AW/5W", "W1AW/5Q", "W1AW/5O" are that
    # call with the following word's first letter fused onto it -- and they
    # are dangerous because base_call() normalises them to W1AW, so the
    # database blesses them as "known" and they spot on a single copy.
    # Letter-initial suffixes stay valid (/P, /M, /QRP, /VE3, /KH6), as do
    # prefix forms (F/W1AW, 9A/W1AW), where the longest part is the base.
    # Cost: the non-standard suffix form of a digit-initial prefix
    # ("W1AW/9A" for Croatia, conventionally sent 9A/W1AW) is rejected.
    parts = token.split("/")
    if len(parts) > 1:
        suffix = parts[-1]
        if len(suffix) > 1 and suffix[0].isdigit():
            return False
    base = max(parts, key=len)
    if not any(c.isdigit() for c in base) or not any(c.isalpha() for c in base):
        return False
    if _CALL_RE.match(token) is None:
        return False
    # every real prefix has a letter before its separating digit; tokens like
    # "55N"/"57N" are garbled contest exchanges, not calls
    last_digit = max(i for i, c in enumerate(base) if c.isdigit())
    return any(c.isalpha() for c in base[:last_digit])


class CallsignExtractor:
    """One instance per tracked signal; fed chars/word-breaks by its decoder."""

    def __init__(self, on_call: Callable[[str, str], None],
                 window: int = 12, known=None, patterns=None,
                 known_weak=None):
        """on_call(callsign, activity) with activity in {"CQ", "DE"}.

        A call spots only after a CQ or DE in the last few words, and only
        once it has been copied cleanly as a standalone word at least once
        before (see _report), listed in a database or not. Repetition alone
        never spots.

        known: optional container of known-good callsigns (e.g. load_scp()).
        An unknown variant loses to an overlapping known call ("K3CB" garble vs the
        real K3CBK) -- decode truncations/fusions of a call are shape-valid,
        so only a database can arbitrate.

        patterns: optional container of Patt3Ch callsign patterns
        (load_patterns()). A call whose pattern is listed gets the normal
        rules; an unlisted pattern (T0FT, LK0RVV -- shapes real prefixes
        don't produce) needs an extra confirming copy and cannot spot on
        repetition alone.

        known_weak: a second, broader list that may VETO but never BLESS.
        A callsign database does two opposite jobs here: it lets a listed
        call spot on a single copy (blessing), and it lets a call seen on
        this signal veto an overlapping garble. A bigger, looser list vetoes
        more garbles but also blesses more of them -- measured on
        combined.txt (MASTER.SCP plus 22856 entries): calls listed only
        there were ~2x over-represented among spots no other RBN skimmer
        confirmed, and half as likely to appear in a day's real traffic.
        Yet dropping it outright cost the vetoes and let "KC4YRVEE" through.
        So: pass the curated list as `known` and the broad one here, and
        each job gets the list it should have. N3ADP and K0BXB (broad-list
        only) then lose their single-copy pass and are vetoed by N3AD and
        K0BX, which are curated.
        """
        self.on_call = on_call
        self._known = known
        self._known_weak = known_weak
        self._patterns = patterns
        self._word = ""
        self._tokens: deque = deque(maxlen=window)
        self._recent_calls: deque = deque(maxlen=8)  # candidates seen lately
        self._merged: deque = deque(maxlen=8)        # parts of recent merges
        # the subset of those that were a whole word exactly as decoded, not a
        # repair/merge/strip of one: only these count as independent copies
        self._clean: deque = deque(maxlen=8)
        self._token_now = ""
        self._prosign_strip: Optional[str] = None
        self._spotted: deque = deque(maxlen=8)       # calls actually reported
        # clean copies per call over the signal's recent life: longer than
        # the 8-word _clean window, so a runner heard 20 times still
        # outweighs the one garbled copy that follows (see _report)
        self._copies: "OrderedDict[str, int]" = OrderedDict()
        # activity this signal's grammar assigned to a call when it was
        # first seen with real context, so the confirming copy inherits it
        self._activity: "OrderedDict[str, str]" = OrderedDict()

    def _is_known(self, call: str) -> bool:
        """Database membership, matching portable calls on their base form
        (W4MQC/1 counts as known when the database lists W4MQC)."""
        if self._known is None:
            return False
        return self._in(call, self._known)

    @staticmethod
    def _in(call: str, listing) -> bool:
        if listing is None:
            return False
        if call in listing:
            return True
        # only a conventional affix inherits the base call's entry: otherwise
        # "W1AW/CO" (a garbled "W1AW/0") is blessed as known via W1AW
        return conventional_affix(call) and base_call(call) in listing

    def _is_listed(self, call: str) -> bool:
        """In either list. Used where a call only needs to be plausible
        enough to veto a garble, not trusted enough to spot on one copy."""
        return (self._in(call, self._known)
                or self._in(call, self._known_weak))

    def add_char(self, ch: str) -> None:
        if len(self._word) < 16:
            self._word += ch

    def add_word_break(self, cut: bool = False) -> None:
        """End the word. cut: a segmenter made this boundary inside a word
        the decoder kept whole, so the word is evidence but not a copy."""
        token, self._word = self._word, ""
        if not token:
            return
        self._tokens.append(token)
        self._evaluate(token, cut)

    # -- candidate generation --------------------------------------------

    def _candidates(self, token: str) -> List[Tuple[str, int, Optional[str]]]:
        """(call, span_tokens, forced_activity), strongest evidence first."""
        out: List[Tuple[str, int, Optional[str]]] = []
        # prosigns fused onto the call, possibly stacked: "DEWC5WC",
        # "BKDEWB7BNE" (BK + DE + call), "CQCQDEK4RLC" (fully fused text)
        rest, act = token, None
        # "CQCQCQDE" is four
        for _ in range(8):
            for pref in ("BK", "TU", "QRZ", "CQ", "DE", "KN"):
                if rest.startswith(pref) and len(rest) - len(pref) >= 3:
                    if pref == "DE":
                        # "CQCQDEK4RLC" is a CQ: a DE after a CQ still
                        # introduces the CALLER, it does not demote the CQ
                        act = "CQ" if act == "CQ" else "DE"
                    elif pref in ("CQ", "QRZ"):
                        act = "CQ"
                    rest = rest[len(pref):]
                    break
            else:
                break
        if rest != token and is_valid_call(rest):
            out.append((rest, 1, act))
            self._prosign_strip = rest
        # a call sent twice with the word gap missed ("W7BEMW7BEM"): the
        # doubled token is itself the two-copy confirmation
        half = len(token) // 2
        if (len(token) >= 8 and len(token) % 2 == 0
                and token[:half] == token[half:]
                and is_valid_call(token[:half])):
            out.append((token[:half], 1, None))
        # ... and the same with the second copy's head lost as well as the
        # gap ("AA4JV4JV" = AA4JV + 4JV), so the tail repeats the END of the
        # head rather than the whole of it. Observed live; AA4JV is in
        # neither database yet 17 RBN skimmers spotted it that day, so no
        # database can arbitrate here -- only the shape.
        #
        # Requiring a tail of 2+ characters makes this strikingly specific:
        # across all 72871 listed calls exactly one has the shape (YO0HOHO),
        # and the "not already listed" gate protects it.
        elif not self._is_listed(token):
            for cut in range(len(token) - 2, 2, -1):
                head, tail = token[:cut], token[cut:]
                if head.endswith(tail) and is_valid_call(head):
                    out.append((head, 1, None))
                    break
        # spurious word breaks split a call across tokens; longest merge first
        toks = list(self._tokens)
        for span in MERGE_SPANS:
            if len(toks) < span:
                continue
            parts = toks[-span:]
            # grammar words never belong inside a call. Exception: a leading
            # "K" is usually the first letter of a split call ("K 1NMF"),
            # while a trailing "K" is the over prosign ("W1AW K").
            if any(p in _NOT_CALLS for p in parts[1:]):
                continue
            if parts[0] in _NOT_CALLS and parts[0] != "K":
                continue
            # two complete calls side by side ("9A1A 9A1A" repeats, or two
            # different stations) are not fragments of a longer call
            if sum(1 for p in parts if is_valid_call(p)) >= 2:
                continue
            cand = "".join(parts)
            if is_valid_call(cand) and not _runs_into_repeat(parts):
                out.append((cand, span, None))
        # exchange fused onto the call: "W1RW5NN" -> W1RW. Preferred over the
        # bare token, which often passes the call regex itself.
        stripped = None
        ends_in_report = token.endswith(_EXCHANGE_TAILS)
        for tail in _EXCHANGE_TAILS:
            if token.endswith(tail) and is_valid_call(token[:-len(tail)]):
                stripped = token[:-len(tail)]
                out.append((stripped, 1, None))
                break
        # one garbled letter fused onto the front of a known call: a CQ whose
        # Q was lost leaves "CW4MQC/1" for W4MQC/1, which is itself call-shaped
        # with a plausible prefix, so only the database can arbitrate. Gated on
        # a database hit, and it replaces the bare token rather than adding to
        # it, so the garble cannot spot if the stripped call lacks evidence.
        # Only when the garble is on the front of the BASE call: in
        # "KWC/KW7Q" the junk is the prefix, and stripping one character off
        # it just yields another plausible-looking prefix ("WC/KW7Q") that
        # the database would bless all over again.
        #
        # Only when the shorter call is already established on this signal
        # (spotted, or copied cleanly twice). Ungated, it rewrote every real
        # call missing from the database whose tail IS listed: NN1RR (not in
        # MASTER.SCP) came out as N1RR on every copy and spotted five times
        # in one CWT hour, TK4TZ as K4TZ (2026-09-16). Gated, the bench lost
        # nothing: removing the repair outright gave identical output.
        if (stripped is None and len(token) >= 4 and token[0].isalpha()
                and not self._is_known(token) and self._is_known(token[1:])
                and token.split("/")[0] == base_call(token)
                and (token[1:] in self._spotted
                     or self._copies.get(token[1:], 0) >= 2)):
            stripped = token[1:]
            out.append((stripped, 1, None))
        if stripped is None and is_valid_call(token):
            # A token ending in a signal report whose head is NOT call-shaped
            # ("UN5NN" -> "UN") is report-fusion the stripper above could not
            # undo. Without this the guard that exists to catch fusion passes
            # its worst case: the head fails is_valid_call, the loop does
            # nothing, and the FUSED token is accepted whole -- which is how
            # UN5NN spotted on 2026-09-02 (UN5 is a real Kazakh prefix, so
            # the regex, all_boilerplate and the patt3ch pattern all pass).
            # Across MASTER.SCP + combined.txt exactly six real calls have
            # this shape (DJ5NN DM5NN KE5NN NA5NN NR5NN W5NN) and none ends
            # in "599", so requiring a listing costs no real call. The BROAD
            # list is right here: this is not a single-copy pass, only
            # permission to compete under the normal confirmation rules.
            if not ends_in_report or self._is_listed(token):
                out.append((token, 1, None))
        return out

    def _evaluate(self, token: str, cut: bool = False) -> None:
        self._token_now = token
        # one word is one sighting: two repair branches yielding the same
        # string from it must not count as two copies of that call
        tried = set()
        self._prosign_strip = None
        for cand, span, forced in self._candidates(token):
            if cand in tried:
                continue
            tried.add(cand)
            # the call with only prosigns run onto its front ("CQCQDEN8TGQ",
            # "DEWC5WC") is a copy too: nothing of the call itself was
            # rebuilt, unlike a merge of fragments ("K 1 A" -> K1A)
            # a word a segmenter cut out of a longer one is judged like a
            # clean copy but never counts as one: it can only spot a call
            # already copied cleanly here (KA9Z cut from KA9ZAP must not)
            whole = not cut and (cand == token or cand == self._prosign_strip)
            if whole:
                self._copies[cand] = self._copies.pop(cand, 0) + 1
                if len(self._copies) > 32:
                    self._copies.popitem(last=False)
            if span > 1:
                self._merged.append(tuple(list(self._tokens)[-span:]))
            spotted = self._report(cand, span, forced)
            if whole:
                self._clean.append(cand)
            if spotted:
                return
            self._recent_calls.append(cand)

    def _cut_short(self, cand: str) -> bool:
        """cand is the front of a longer merge seen on this signal, and the
        rest of that merge is single letters ("N 3 H" of "N 3 H X Z"). Not
        any longer merge: "K 1NMF EN" makes K1NMFEN out of K1NMF and the
        next word, so the tail pieces must look like letters of a call."""
        lim = SHORT_CALL_TAIL_PIECE if len(cand) <= 4 else TAIL_PIECE
        for parts in self._merged:
            head = ""
            for k, p in enumerate(parts):
                head += p
                if len(head) >= len(cand):
                    break
            if head != cand or k == len(parts) - 1:
                continue
            if all(len(p) <= lim for p in parts[k + 1:]):
                return True
        return False

    def _report(self, cand: str, span: int, forced: Optional[str]) -> bool:
        # a fragment of a call already spotted on this signal ("C5WC" after
        # "WC5WC") is a re-split of the same call, not a new station
        if any(cand != s and cand in s for s in self._spotted):
            return False
        # the stations this signal has really been sending: spotted, or
        # copied cleanly twice
        est = set(self._spotted) | {c for c, k in self._copies.items() if k >= 2}
        est.discard(cand)
        # a keying garble of one of them (EA5M copied 20 times, then "TEST
        # CWT EA5T"). A database listing doesn't excuse it: EA5T and K0PK
        # are real calls, which is why the listed single-copy pass let them
        # out. A second real station this close in marks on the same signal
        # would have to out-copy the first.
        n = self._copies.get(cand, 0)
        if any(n < self._copies.get(s, 0) and mark_distance(cand, s, GARBLE_MARKS) <= GARBLE_MARKS
               for s in est):
            return False
        known = self._is_known(cand)
        # the same thing at the MARK level rather than the text level (see
        # is_morse_dropout): a decoder that starts listening partway through
        # a call already spotted on this signal produces a shorter, DIFFERENT
        # -looking, still valid-shaped call -- text substring can't catch
        # that, because the dropped marks are not a whole number of
        # characters. Checked against _spotted (already vetted, however that
        # happened) rather than gated on a database listing, because the
        # source call need not be database-listed itself to be real (RW6A,
        # the observed case, is in neither MASTER.SCP nor combined.txt).
        if not known and any(is_morse_dropout(cand, s) for s in self._spotted):
            return False
        # a token made entirely of fused boilerplate ("TU5NN" = TU + 5NN) can
        # pass the call regex but is never a real station; a genuinely-listed
        # call of that shape (in the known database) is the only exception
        if not known and all_boilerplate(cand):
            return False
        # confirmed-bogus prefix (see _UNTRUSTED_PREFIXES): the shape pattern
        # is real, but this specific candidate never is, so it loses the
        # pattern-tier trust a database entry would still grant
        if not known and base_call(cand).startswith(_UNTRUSTED_PREFIXES):
            return False
        # NOTE: a truncation onto another REAL call ("E77D" decoded from
        # "E77DX"; both listed, so the database cannot arbitrate) is only
        # caught when the longer call was spotted FIRST, by the check above.
        # Rejecting the shorter whenever a longer known call has merely been
        # SEEN was tried and reverted: "longer wins" is wrong when the longer
        # one is itself a doubling garble, and it cost K0BX -- listed in both
        # MASTER.SCP and combined.txt -- to K0BXB, which is in neither the
        # curated list nor the RBN. Deciding this needs evidence the decoder
        # does not have; tools/spot_confirm.py catches it against the archive.
        # a merge cut short: N3H merged at the H of "N 3 H X Z", repeated on
        # every copy. Only for a merge (a clean word "KC2SI" before "Z" lost
        # real calls on the bench) and only unlisted
        if span > 1 and not known and self._cut_short(cand):
            return False
        if (self._known is not None or self._known_weak is not None) and not known:
            # decode variants of a call (truncated K3CB, doubled KK3CBK) are
            # shape-valid; an unknown candidate that overlaps a listed call
            # seen on this signal is a garble of it, not a new station.
            # Vetoing uses the BROAD listing: being plausible enough to
            # discredit a garble is a lower bar than being trusted enough to
            # spot on a single copy.
            for v in set(self._recent_calls) | set(self._spotted):
                if v == cand or not self._is_listed(v):
                    continue
                if cand in v or v in cand:
                    return False
                # same station, garbled affix: we saw W1AW/0 on this signal
                # and now "W1AW/CO". Substring overlap misses it because the
                # affixes differ, but a conventional affix beats an
                # unconventional one for the same base call.
                if (base_call(v) == base_call(cand)
                        and conventional_affix(v)
                        and not conventional_affix(cand)):
                    return False
        activity = forced
        if activity is None:
            before = list(self._tokens)[:-span]
            # rejoin first, so a context split over four words still fits.
            # Only for a call decoded as one clean word: a slow fist whose
            # every letter is its own word ("C Q DE K 1 A Y Z", 11 wpm) also
            # rejoins "C Q", and the merged fragment K1A would get its CQ
            words = before[-CONTEXT_WORDS - 1:]
            if cand == self._token_now:
                words = rejoin_boilerplate(words)
            pre = words[-CONTEXT_WORDS:]
            lead = words[-CONTEXT_WORDS - 1] if len(words) > CONTEXT_WORDS else ""

            # only a station this signal is established on owns a TEST after it
            def owns_test(t):
                return any(c in t for c in est)
            # a signal that has already been spotted as another station is a
            # runner's frequency: a new call there is far more often one of
            # its callers, after a runner's call the decoder garbled ("TEST
            # WA3 TT KB9S" for WA3AAN), than a new runner
            adjacent = any(s != cand for s in self._spotted)
            # "DE <call>" means the sender is identifying itself; whether
            # that is a CQ or an answer depends on what preceded the DE.
            # "CQ TEST DE K4RUM" is K4RUM calling CQ; "N8JMS DE WA3TVH" is
            # WA3TVH answering. Testing "DE" first labelled every
            # "CQ ... DE <call>" -- the commonest way to call CQ in CW -- as
            # DE, which the Aggregator holds, so those stations never
            # reached the RBN at all.
            last = pre[-1] if pre else ""
            if last == "DE" or (last.endswith("DE") and all_boilerplate(last)):
                # "CQCQDE <call>": the CQ is fused into the DE token itself
                activity = ("CQ" if is_cq_word(last) or cq_context(pre[:-1], cand, lead, owns_test, adjacent)
                            else "DE")
            # a CQ in view now beats what this call was given before: one
            # stray "NA2U DE E NT2U NA2U" must not make a runner's CQs DE
            # for as long as the call stays in memory (CWT 2026-08-12)
            elif "DE" not in pre and cq_context(pre, cand, lead, owns_test, adjacent):
                activity = "CQ"
            # the copy that SPOTS is the confirming one, by which time the
            # opening CQ has scrolled out of a 3-token window. Reuse what
            # this call was given when its grammar was still visible --
            # deliberately per-call, so a bystander in a zero-beat pileup
            # does not inherit the runner's CQ (tried and reverted 08-10).
            elif cand in self._activity:
                activity = self._activity[cand]
            elif "DE" in pre:
                activity = "DE"
            if activity in ("CQ", "DE"):
                self._activity[cand] = activity
                if len(self._activity) > 8:
                    self._activity.popitem(last=False)
        if activity is None:
            return False
        # Copy requirement, as CW Skimmer does it: a CQ/DE context alone is
        # not enough, because noise produces CQ-looking tokens too. The call
        # must also have been copied cleanly as a standalone word before --
        # a repair or a substring of a garbled word does not count.
        pat_ok = (known or self._patterns is None
                  or call_pattern(cand) in self._patterns)
        spotted_before = 1 if cand in self._spotted else 0
        if cand == self._token_now:
            # an independent copy: the word itself, sent as decoded. Every
            # call needs one clean copy before this one, database-listed or
            # not, and an implausible pattern one more still. A listed call
            # used to spot on this copy alone, which let a runner's keying
            # garbles out whenever the garble was itself a listed call
            # (K4TZ for TK4TZ, OZ1AA for OZ1AAR). UberSDR, on the same audio,
            # wants two copies of a known call; asking the same halved our
            # off-consensus rate on three CWT hours (10.6 -> 5.3, 14.6 ->
            # 9.0, 16.2 -> 11.8 %) and lost 2-5 single-spot calls a session.
            seen = list(self._clean).count(cand) + spotted_before
            needed = 1 if pat_ok else 2
            # a call that is a listed call with one letter in front ("CW4MQC/1"
            # for W4MQC/1, a CQ whose Q was lost) needs a copy more: the
            # front-garble repair in _candidates now only applies once the
            # listed call is established, so before that the garble competes
            # as itself. A real unlisted call of this shape (NN1RR over N1RR)
            # is sent over and over and still spots.
            if (not known and len(cand) >= 4 and cand[0].isalpha()
                    and self._is_known(cand[1:])
                    and cand.split("/")[0] == base_call(cand)):
                needed += 1
            # short calls are what a call with its head or tail lost looks
            # like: N8A for N8AI, N6D for ON6DC, TA3B for AA3B. Across four
            # CWT hours half the 3-character CQ spots and 5 of 11 unlisted
            # 4-character ones were busts, against 3 % of listed 4-character
            # ones (2026-10-21)
            if self._known is not None and (
                    len(cand) <= 3 or (len(cand) == 4 and not known)):
                needed += 1
        else:
            # a repair (merge, strip, doubled head): it may confirm only its
            # own repeats, and only if it is already listed or pattern-listed
            if not (known or pat_ok):
                return False
            seen = list(self._recent_calls).count(cand) + spotted_before
            needed = 1
        if seen < needed:
            return False
        self._spotted.append(cand)
        self._recent_calls.append(cand)
        self.on_call(cand, activity)
        return True
