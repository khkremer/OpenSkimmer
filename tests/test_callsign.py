from openskimmer.spotting.callsign import CallsignExtractor, is_cq_word, is_valid_call, mark_distance


def test_valid_calls():
    for call in ["W1AW", "DL1ABC", "JA3XYZ", "KH6ABC", "9A1A", "F/W1AW",
                 "W1AW/P", "VP2E", "N0AX", "G4ABC/QRP"]:
        assert is_valid_call(call), call


def test_invalid_calls():
    for token in ["CQ", "TEST", "DE", "599", "5NN", "TU", "ABC", "111",
                  "E", "73", "QRZ"]:
        assert not is_valid_call(token), token


def _feed(extractor, text):
    for word in text.split():
        for ch in word:
            extractor.add_char(ch)
        extractor.add_word_break()


def test_cq_pattern():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ CQ DE W1AW W1AW K")
    assert ("W1AW", "CQ") in calls


def test_contest_cq():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "TEST DL1ABC DL1ABC")
    assert ("DL1ABC", "CQ") in calls


def test_repeated_call_without_context_does_not_spot():
    # repetition alone is noise's favourite trick: no CQ/DE, no spot
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "JA3XYZ 5NN 14 JA3XYZ")
    assert calls == []


def test_repeated_call_with_cq_context_spots():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ JA3XYZ JA3XYZ")
    assert ("JA3XYZ", "CQ") in calls


def test_no_false_positive_on_exchange():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "TU 5NN 599 73 GL")
    assert calls == []


# -- fragment/fusion recovery (tuned against live 20m captures) -------------

def test_split_call_merged_with_de_context():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    # observed live: K1NMF split after K; two copies confirm the call
    _feed(ex, "DE K 1NMF EN DE K1N MF")
    assert ("K1NMF", "DE") in calls


def test_split_call_merged_on_repeat():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "W C5WC BK W C5WC")       # observed live: WC5WC split after W
    # a repaired (merged) call needs its own repeats AND a CQ/DE context
    assert calls == []
    # and the fragment must not also be spotted as its own station
    assert not any(c == "C5WC" for c, _ in calls)


def test_slow_fist_call_merged_whole_not_cut_short():
    # observed live 2026-10-23 (SOTA, 7055 kHz): every letter its own word.
    # Merging only the last three words spotted N3H at the H, on every copy
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ SOTA DE N 3 H X Z CQ SOTA DE N 3 H X Z CQ SOTA")
    assert ("N3HXZ", "CQ") in calls
    assert not any(c == "N3H" for c, _ in calls)


def test_merge_does_not_run_into_the_next_copy():
    # observed live 2026-10-23 (SKCC, 10120 kHz): VE2NC + the V of its repeat
    # is call-shaped too, and must neither spot nor veto VE2NC
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ CQ SKCC DE V E2 N C V E2 N C K CQ CQ SKCC DE V E2 N C V E2 N C K")
    assert ("VE2NC", "CQ") in calls
    assert not any(c in ("VE2N", "VE2NCV") for c, _ in calls)


def test_fused_de_prefix():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "DEWC5WC W C5WC K")       # observed live: DE fused onto call
    # one fused copy is a repair: no spot until the call repeats cleanly
    assert calls == []
    _feed(ex, "DE WC5WC WC5WC")
    assert ("WC5WC", "DE") in calls


def test_fused_exchange_suffix():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    # observed live: 5NN fused onto call; repeated copy confirms it
    _feed(ex, "CQ W1RW5NN CQ W1RW5NN")
    assert ("W1RW", "CQ") in calls
    assert not any(c == "W1RW5NN" for c, _ in calls)


def test_exchange_token_not_a_call():
    # "55N 55N NJ NJ BK" (garbled 5NN exchange) produced a false spot live
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "55N 55N NJ NJ BK")
    assert calls == []


def test_fused_boilerplate_not_a_call():
    # observed live: "TU 5NN" fused into "TU5NN", which is call-shaped (TU is
    # even a real prefix) and repeats every QSO -- must never spot
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ TU5NN CQ TU5NN")
    assert not any(c == "TU5NN" for c, _ in calls)


def test_fused_boilerplate_not_spotted_via_repetition():
    # the real failure: no CQ/DE anywhere -- "TU5NN" repeats at every QSO end
    # and the repetition alone supplied a "DUPE" activity. Must still not spot.
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "W1AW 5NN 14 TU5NN GL TU5NN E E")
    assert calls == []


def test_qrp_signoff_fuses_into_a_call_shaped_token():
    # observed live: "ES 72 EE" (ES = "and", 72 = the QRP sign-off, EE = the
    # dit-dit farewell) fused into "ES72EE" -- call-shaped, and ES is a real
    # Estonian prefix. Only 72 was missing from the sign-off family: the
    # identical "ES73EE" was already rejected.
    from openskimmer.spotting.callsign import all_boilerplate
    assert all_boilerplate("ES72EE")
    assert all_boilerplate("ES73EE")
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "W1AW 5NN 14 GL ES72EE ES72EE")
    assert not any(c == "ES72EE" for c, _ in calls)


def test_letter_fused_onto_a_portable_district_suffix():
    # observed live: W1AW/5W, W1AW/5Q, W1AW/5O -- the next word's first letter
    # fused onto W1AW/5. Dangerous because base_call() normalises them to
    # W1AW, so the database blesses them as known and they spot on ONE copy.
    # A digit-initial portable suffix is a district change: exactly one digit.
    from openskimmer.spotting.callsign import is_valid_call
    for bust in ("W1AW/5W", "W1AW/5Q", "W1AW/5O"):
        assert not is_valid_call(bust)
    for real in ("W1AW/5", "W1AW/P", "W1AW/QRP", "W1AW/VE3", "W1AW/KH6",
                 "F/W1AW", "9A/W1AW", "W4MQC/1"):
        assert is_valid_call(real)
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W1AW"})
    _feed(ex, "CQ DE W1AW/5W W1AW/5W")
    assert not any(c.startswith("W1AW/5W") for c, _ in calls)


def test_garbled_affix_does_not_inherit_the_database_entry():
    # observed live: "W1AW/CO" while W1AW/0 was being spotted on the same
    # frequency. base_call() normalises it to W1AW, so before this the bogus
    # suffix was blessed as "known" and spotted on a SINGLE copy -- the same
    # trap as W1AW/5W, but letter-initial so the shape check cannot reject it
    # (CO is Cuba's prefix, so a suffix form is unconventional, not absurd).
    from openskimmer.spotting.callsign import conventional_affix
    for ok in ("W1AW", "W1AW/0", "W1AW/5", "W1AW/P", "W1AW/MM", "W1AW/QRP",
               "F/W1AW", "CO/W1AW", "9A/W1AW"):
        assert conventional_affix(ok), ok
    for odd in ("W1AW/CO", "W1AW/VE3", "W1AW/KH6"):
        assert not conventional_affix(odd), odd

    # the real call on the same signal wins outright
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W1AW"})
    _feed(ex, "CQ DE W1AW/0 W1AW/0 CQ DE W1AW/CO W1AW/CO")
    assert not any(c == "W1AW/CO" for c, _ in calls)
    assert any(c == "W1AW/0" for c, _ in calls)

    # and on its own it no longer spots on one copy
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W1AW"})
    _feed(ex, "CQ DE W1AW/CO")
    assert calls == []


def test_implausible_prefix_does_not_bless_the_call():
    # observed live: "KWC/KW7Q" alongside the real KW7Q on the same frequency
    # (75 RBN skimmers had KW7Q that day; KWC is in no database). Any prefix
    # form used to count as conventional, so base_call() blessed it via KW7Q.
    from openskimmer.spotting.callsign import conventional_affix, base_call
    assert not conventional_affix("KWC/KW7Q")
    for ok in ("F/W1AW", "DL/W1AW", "VE3/W1AW", "9A/W1AW", "3B8/W1AW",
               "KP4/W1AW", "VP2E/W1AW"):
        assert conventional_affix(ok), ok
    # prefix/homecall on a length tie: the home call is the base
    assert base_call("VP2E/W1AW") == "W1AW"

    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"KW7Q"})
    _feed(ex, "CQ DE KW7Q KW7Q CQ DE KWC/KW7Q KWC/KW7Q")
    assert sorted({c for c, _ in calls}) == ["KW7Q"]

    # the front-garble repair must not simply shave the bogus prefix down to
    # another plausible one ("WC/KW7Q"), but must still fix a garbled base
    # (the repair applies once the clean call is established on the signal)
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W4MQC"})
    _feed(ex, "CQ DE W4MQC/1 W4MQC/1 CQ DE CW4MQC/1 CW4MQC/1")
    assert {c for c, _ in calls} == {"W4MQC/1"}, calls


def test_truncation_loses_to_a_longer_call_already_spotted():
    # observed live: E77D at the same time and frequency as E77DX. BOTH are
    # listed in MASTER.SCP so the database cannot arbitrate -- the RBN had 135
    # skimmers on E77DX that day and none on E77D. This is only caught when
    # the full call was spotted FIRST; see the note in _report() for why
    # "longer known call wins" cannot be applied in general (it suppresses
    # K0BX in favour of the doubling garble K0BXB).
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known={"E77DX", "E77D"})
    _feed(ex, "CQ DE E77DX E77DX CQ DE E77D E77D")
    assert sorted({c for c, _ in calls}) == ["E77DX"]


def test_portable_call_matches_database_on_base_call():
    # databases list base calls; a /1 portable must still count as known and
    # so spot on a single copy rather than needing two
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W4MQC"})
    _feed(ex, "CQ W4MQC/1 W4MQC/1")
    assert ("W4MQC/1", "CQ") in calls


def test_leading_garble_letter_stripped_against_database():
    # observed live: CW4MQC/1 spotted alongside the real W4MQC/1 -- a CQ whose
    # Q was lost fuses onto the call and is itself call-shaped
    for text in ("CQ CW4MQC/1 CQ CW4MQC/1",     # only the garble is ever heard
                 "CQ CW4MQC/1 CQ W4MQC/1",
                 "CQ W4MQC/1 CQ CW4MQC/1"):
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                               known={"W4MQC"})
        _feed(ex, text)
        assert not any(c == "CW4MQC/1" for c, _ in calls), text
    # the clean form spots once it is heard twice in CQ context
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"W4MQC"})
    _feed(ex, "CQ W4MQC/1 W4MQC/1")
    assert ("W4MQC/1", "CQ") in calls


def test_boilerplate_shaped_known_call_still_spots():
    # a genuinely-listed call of a boilerplate shape is the one exception
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known={"K5NN"})
    _feed(ex, "CQ K5NN CQ K5NN")
    assert ("K5NN", "CQ") in calls


def test_trailing_k_is_not_merged():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ CQ DE W1AW K W1AW K")
    assert ("W1AW", "CQ") in calls
    assert not any("W1AWK" == c for c, _ in calls)


def test_three_char_call_needs_context_not_just_repeat():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "K1N BK K1N BK")          # garbled fragment repeating
    assert calls == []
    _feed(ex, "CQ DE K1N")              # but real context still spots it
    assert ("K1N", "CQ") in calls


def test_stacked_fused_prosigns():
    # observed live: "BK DE WB7BNE" fused into one token
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "UP T0FT BKDEWB7BNE HW BKDEWB7BNE")
    assert ("WB7BNE", "DE") in calls


def test_fully_fused_cq_call():
    # a tight fist can fuse the whole CQ into one token
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQCQDEK4RLC CQCQDEK4RLC")
    assert ("K4RLC", "CQ") in calls


def test_three_cqs_fused_onto_the_call():
    # observed 2026-10-25: "CQCQCQDEK4BSK" (four prosigns) came out as no
    # candidate at all when the strip stopped after three
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQCQCQDEK4BSK CQCQCQDEK4BSK")
    assert ("K4BSK", "CQ") in calls


def test_real_calls_with_prosign_prefixes_not_mangled():
    # TU5AX (Ivory Coast) and KN4ABC are real call shapes; stripping their
    # leading letters leaves junk, so the full token must win
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ DE TU5AX TU5AX")
    assert ("TU5AX", "CQ") in calls
    calls.clear()
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ DE KN4ABC KN4ABC")
    assert ("KN4ABC", "CQ") in calls


def test_doubled_call_token_is_self_confirming():
    # observed live: "W7BEM W7BEM" with the word gap missed becomes one
    # token; the doubled form is itself the two-copy confirmation
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "W7BEMW7BEM")
    # a repaired doubled token is not an independent copy: no context, no spot
    assert calls == []
    _feed(ex, "CQ W7BEM W7BEM")
    assert ("W7BEM", "CQ") in calls


def test_known_call_needs_two_copies():
    # a database listing no longer spots on one copy: a runner's keying
    # garbles are often listed calls themselves (K4TZ for TK4TZ)
    known = frozenset({"K3CBK"})
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ CQ DE K3CBK")            # one clean copy in CQ context
    assert calls == []
    _feed(ex, "K3CBK")                     # the second copy spots it
    assert calls == [("K3CBK", "CQ")]


def test_unknown_variant_loses_to_known_call():
    # decode truncations of a known call (K3CBK -> K3CB, K3C) are shape-
    # valid; once the known call has been seen, its variants must not spot
    known = frozenset({"K3CBK"})
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ DE K3CBK K3CBK BK CQ DE K3CB BK CQ DE K3CB")
    assert ("K3CBK", "CQ") in calls
    assert not any(c == "K3CB" for c, _ in calls)


def test_unknown_calls_keep_two_copy_rule():
    known = frozenset({"K3CBK"})
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ DE W7BEM")               # unknown, single copy: no spot
    assert calls == []
    _feed(ex, "CQ DE W7BEM")               # second copy confirms
    assert ("W7BEM", "CQ") in calls


# -- Patt3Ch pattern layer ---------------------------------------------------

def test_call_pattern_rules():
    from openskimmer.spotting.callsign import call_pattern
    assert call_pattern("VE3NEA") == "VE3@@@"      # default: 3 literal
    assert call_pattern("N4ZR") == "N4@@"          # letter-digit-letter: 2
    assert call_pattern("3DA0RU") == "3DA0@@"      # digit-letter-letter: 4
    assert call_pattern("HA2021ITU") == "HA2###@@@"
    assert call_pattern("W1AW/P") == "W1@@"        # base call only


def test_load_patterns(tmp_path):
    from openskimmer.spotting.callsign import load_patterns
    f = tmp_path / "patt3ch.lst"
    f.write_bytes(b"+ K3@@@\r\n  N4@@\r\n+ VE3@@@\r\n")
    pats = load_patterns(str(f))
    assert pats == frozenset({"K3@@@", "N4@@", "VE3@@@"})


PATS = frozenset({"K3@@@", "W1@@", "N4@@", "KO4@@@"})


def test_unlisted_pattern_blocks_repetition_spot():
    # "T0FT" (no real prefix produces T0##) repeating must not spot ...
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), patterns=PATS)
    _feed(ex, "T0FT 5NN T0FT 5NN T0FT")
    assert calls == []
    # ... and so does a listed call, once its repeat has CQ context
    _feed(ex, "CQ K3CBK K3CBK")
    assert ("K3CBK", "CQ") in calls


def test_unlisted_pattern_needs_extra_context_copy():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), patterns=PATS)
    _feed(ex, "CQ DE T0AA CQ DE T0AA")          # two copies: still blocked
    assert calls == []
    _feed(ex, "CQ DE T0AA")                     # third copy earns the spot
    assert ("T0AA", "CQ") in calls


def test_listed_pattern_keeps_two_copy_rule():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), patterns=PATS)
    _feed(ex, "CQ DE KO4AYC CQ DE KO4AYC")
    assert ("KO4AYC", "CQ") in calls


def test_morse_dropout_detection():
    from openskimmer.spotting.callsign import is_morse_dropout
    # R=.-. W=.-- 6=-.... A=.- ; drop R and W's first two marks, and the one
    # mark left of W (a dah) plus 6 and A re-segment exactly as T6A
    assert is_morse_dropout("T6A", "RW6A")
    assert not is_morse_dropout("RW6A", "T6A")   # wrong direction
    assert not is_morse_dropout("W1AW", "K5NN")  # unrelated calls
    assert not is_morse_dropout("W1AW", "W1AW")  # identical, not shorter


def test_morse_dropout_not_spotted_after_the_real_call():
    # observed live 2026-09-16, 18080.0 kHz: RW6A spotted (9 dB, 24 wpm),
    # T6A spotted 16 s later (7 dB, 25 wpm) on the same frequency. Neither
    # call is in MASTER.SCP or combined.txt (RW6A isn't listed anywhere;
    # T6A is, but only in combined.txt, which blesses nothing), and T6A is
    # not a text substring of RW6A, so neither the database nor the
    # existing substring check caught it -- only the mark-level check does.
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ CQ DE RW6A RW6A K")
    assert ("RW6A", "CQ") in calls
    _feed(ex, "CQ DE T6A CQ DE T6A")   # would otherwise clear the 1-extra-copy bar
    assert ("T6A", "CQ") not in calls
    # a database entry still overrides the veto
    calls2 = []
    ex2 = CallsignExtractor(lambda c, a: calls2.append((c, a)), known={"T6A"})
    _feed(ex2, "CQ CQ DE RW6A RW6A K")
    # (three copies: a 3-character call needs one more than a longer one)
    _feed(ex2, "CQ DE T6A CQ DE T6A CQ DE T6A")
    assert ("T6A", "CQ") in calls2


def test_untrusted_prefix_never_spots_on_pattern_trust_alone():
    # T5TE/T5AT/T5IT: observed live 2026-09-09..14 on 80/40/17m, all
    # shape-matching the real listed "T5@@" pattern (some genuine T5 call of
    # that shape exists in RBN's history) but none of them a real call --
    # not in MASTER.SCP or combined.txt, and none ever confirmed by another
    # skimmer as a real QSO partner. Unlike an ordinary unlisted pattern
    # (test_unlisted_pattern_needs_extra_context_copy), a LISTED pattern
    # normally only needs one confirming copy, which this signal cleared
    # every day for six days straight -- so the fix has to hold even with
    # arbitrarily many repeats, not just raise the copy count.
    pats = PATS | {"T5@@"}
    for bogus in ("T5TE", "T5AT", "T5IT"):
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)), patterns=pats)
        _feed(ex, f"CQ DE {bogus} CQ DE {bogus} CQ DE {bogus} CQ DE {bogus}")
        assert calls == [], bogus
    # a database entry still overrides the veto, so a genuine T5 station
    # (once confirmed and added to the curated list) remains spottable
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known={"T5AT"}, patterns=pats)
    _feed(ex, "CQ DE T5AT CQ DE T5AT")
    assert ("T5AT", "CQ") in calls


def test_broad_list_vetoes_but_does_not_bless():
    # observed live: N3ADP alongside the real N3AD on one frequency. N3AD is
    # in MASTER.SCP and had 96-129 RBN spotters; N3ADP is in combined.txt
    # only and has never been spotted by anyone. Same shape as K0BXB vs K0BX.
    #
    # A callsign list does two opposite jobs: it BLESSES (a listed call spots
    # on one copy) and it VETOES (a listed call discredits an overlapping
    # garble). A broad list should only do the second.
    strong, weak = {"N3AD", "K0BX"}, {"N3ADP", "K0BXB"}
    for real, garble in (("N3AD", "N3ADP"), ("K0BX", "K0BXB")):
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                               known=strong, known_weak=weak)
        _feed(ex, f"CQ DE {real} {real} CQ DE {garble} {garble}")
        assert sorted({c for c, _ in calls}) == [real]

    # one list for both jobs is the bug: the garble spots too
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known=strong | weak)
    _feed(ex, "CQ DE N3AD N3AD CQ DE N3ADP N3ADP")
    assert "N3ADP" in {c for c, _ in calls}

    # and with no weak list at all nothing changes for existing setups:
    # a curated call spots on two clean copies in CQ context
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=strong)
    _feed(ex, "CQ DE N3AD N3AD")
    assert any(c == "N3AD" for c, _ in calls)


def test_doubled_call_with_a_truncated_second_copy():
    # observed live: "AA4JV4JV" for AA4JV -- the call sent twice with the word
    # gap missed AND the second copy's head lost, so the tail repeats the END
    # of the head rather than the whole of it (the existing exact-double
    # repair, W7BEMW7BEM, cannot see it). AA4JV is in NEITHER database yet 17
    # RBN skimmers spotted it that day, so only the shape can arbitrate.
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ AA4JV4JV")
    # a repaired token is not an independent copy: one sighting is not enough
    assert calls == []
    _feed(ex, "CQ AA4JV AA4JV")
    assert any(c == "AA4JV" for c, _ in calls)
    assert not any(c == "AA4JV4JV" for c, _ in calls)

    # the exact-double repair still works
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ W7BEMW7BEM")
    _feed(ex, "CQ W7BEM W7BEM")
    assert any(c == "W7BEM" for c, _ in calls)

    # exactly one listed call in 72871 has this shape; being listed protects
    # it from being shortened to YO0HO
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"YO0HOHO"})
    _feed(ex, "CQ DE YO0HOHO YO0HOHO")
    assert set(c for c, _ in calls) == {"YO0HOHO"}


def test_answering_station_is_de_not_cq():
    """"<call> DE <call>" is an S&P caller answering someone -- it must stay
    DE so the Aggregator holds it, while "CQ ... DE <call>" is the sender
    calling CQ and must go out as CQ. The whole suite used the CQ form as
    scaffolding and asserted DE, so this direction was never covered."""
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "N8JMS DE WA3TVH WA3TVH")
    assert ("WA3TVH", "DE") in calls
    assert not any(a == "CQ" for _, a in calls)


def test_cq_de_call_is_a_cq():
    """The commonest way to call CQ in CW. Labelling it DE held the spot at
    the Aggregator, so these stations never reached the RBN: observed live
    on 2026-09-02 with a POTA activator we never once spotted while
    spotting four of its hunters."""
    for text, call in [("CQ POTA DE N8JMS N8JMS K", "N8JMS"),
                       ("CQ TEST DE K4RUM K4RUM", "K4RUM"),
                       ("CQ CQ CQ DE W1AW W1AW K", "W1AW"),
                       ("CQCQDEK4RLC CQCQDEK4RLC", "K4RLC")]:
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
        _feed(ex, text)
        assert (call, "CQ") in calls, (text, calls)


def test_fused_report_with_uncallshaped_head_is_rejected():
    """"UN5NN" is a signal report fused onto junk. The stripper that turns
    "W1RW5NN" into W1RW only fires when the head is itself a valid call, so
    a head like "UN" left the FUSED token to be accepted whole -- and UN5 is
    a real Kazakh prefix, so the regex, all_boilerplate and the patt3ch
    pattern all pass. Spotted live on 2026-09-02 on a frequency where a POTA
    activator was running."""
    known = frozenset({"DJ5NN"})
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ DE UN5NN UN5NN")
    assert calls == []

    # the same shape when it IS a listed call must still spot
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ DE DJ5NN DJ5NN")
    assert ("DJ5NN", "CQ") in calls

    # and the head-is-a-call case keeps being stripped, not rejected
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, "CQ DE W1RW5NN W1RW5NN")
    assert ("W1RW", "CQ") in calls
    assert not any(c == "W1RW5NN" for c, _ in calls)


def test_wider_ota_family_recognized_as_boilerplate():
    """POTA and SOTA are the common ones, but the *OTA family is bigger
    (IOTA, BOTA, COTA) plus WWFF -- add them alongside POTA/SOTA in
    _NOT_CALLS for all_boilerplate()/the span-merge exclusion. None can
    collide with a real call: is_valid_call rejects all of them outright
    on the missing digit, the same as every other _NOT_CALLS entry.

    The CQ/DE grammar fix itself needs no such list: pre[-1]=="DE" with a
    CQ/TEST anywhere before it resolves correctly whatever word sits
    between them, recognized or not -- see test_cq_de_call_is_a_cq's
    sibling case with a nonsense word for the same point.
    """
    for word in ["IOTA", "BOTA", "COTA", "WWFF"]:
        assert not is_valid_call(word), word
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
        _feed(ex, f"CQ {word} DE K1ABC K1ABC")
        assert ("K1ABC", "CQ") in calls, word


def test_cq_de_grammar_is_content_agnostic_between_cq_and_de():
    """A completely unrecognized word between CQ and DE must not matter --
    the precedence fix only checks position, never identity, of what sits
    between them. This is what makes it correct for every current and
    future activation program without a lookup table."""
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ FROBNICATE DE K4TEST K4TEST")
    assert ("K4TEST", "CQ") in calls


def test_noise_garbles_containing_a_call_never_spot():
    # observed live 2026-10-06, 2 m: I4ITI decoded from noise inside longer
    # garbled words, repeating across channels. No clean copy, no spot.
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
    _feed(ex, "CQ TII4ITI E TII4I CQ I4ITI-S=EE ATINVI4ITII4I CQ")
    _feed(ex, "CQ 5VIAEITIAEITIAEITI-I4ITI CQ 4ITII4ITI")
    assert calls == []



def test_runners_reply_to_a_caller_is_not_the_callers_cq():
    """Decoded on 20 m in the CWT, 2026-09-16 13:00Z, one channel:
    N4BP calls CQ, then answers N1EN by sending N1EN's call and the
    exchange back. N1EN's call therefore follows "CQ CWT N4BP" within
    three words. Taking any CQ in that window labelled N1EN a CQ, which put
    this skimmer at 34-41 % caller-as-runner spots on K1MK's measure (Makrothen
    and CQ WW 2026, field median 4 %)."""
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known={"N4BP", "N1EN", "KW1X"})
    _feed(ex, "CQ CWT N4BP N1EN BOB 2463 TU N4BP KW1X BOB 2463 TU")
    assert ("N4BP", "CQ") in calls
    assert not any(c in ("N1EN", "KW1X") and a == "CQ" for c, a in calls), calls


def test_cq_word_still_reaches_past_non_call_words():
    # only another CALL between them breaks the link; "CWT", "POTA" and
    # "DE" between the CQ and the call do not
    for text, call in [("CQ CWT K1GU K1GU", "K1GU"),
                       ("CQ POTA DE N8JMS N8JMS", "N8JMS"),
                       ("CQ DX DE JA3XYZ JA3XYZ", "JA3XYZ")]:
        calls = []
        ex = CallsignExtractor(lambda c, a: calls.append((c, a)))
        _feed(ex, text)
        assert (call, "CQ") in calls, (text, calls)


def test_answering_station_after_a_cq_is_de_not_cq():
    # "CQ TEST K4RUM DE W8FN": W8FN identifies while answering K4RUM
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)),
                           known={"K4RUM", "W8FN"})
    _feed(ex, "CQ TEST K4RUM K4RUM DE W8FN W8FN")
    assert ("K4RUM", "CQ") in calls
    assert not any(c == "W8FN" and a == "CQ" for c, a in calls), calls


def test_fused_cq_de_token_is_cq_context():
    """Decoded on 40 m, 2026-10-15 12:15Z: every CQ before WA8VTD came out
    with its word gaps lost, so there was no bare "CQ" and four clean copies
    of the call never spotted. UberSDR heard "CQ CQ CQ DE WA8VTD" in the
    same audio."""
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=set())
    _feed(ex, "CQCQCQDE WA8VTD CQCQDE WA8VTD")
    assert ("WA8VTD", "CQ") in calls, calls


def test_fused_cq_pota_token_is_cq_context():
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"NZ1J"})
    _feed(ex, "QRL? CQCQPOTADE NZ1J NZ1J K")
    assert ("NZ1J", "CQ") in calls, calls


def test_fused_token_with_other_text_is_not_a_cq():
    # "CQCN" is not all boilerplate: no CQ context from it
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"WA8VTD"})
    _feed(ex, "CQCN E WA8VTD")
    assert calls == []


def test_fused_cq_with_the_call_lends_cq_to_the_clean_copy():
    """40 m POTA, 2026-10-15 11:58Z: NZ1J's word gaps (4.0-4.5 dits) overlap
    its character gaps, so "CQ CQ POTA DE NZ1J" decodes as one token. The
    clean copies that follow get the CQ; the fused token never spots."""
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"NZ1J"})
    _feed(ex, "KRL? CQCQPOTADENZ1J NZ1J NZ1J K")
    assert calls == [("NZ1J", "CQ")], calls


def test_fused_cq_lends_nothing_to_a_different_call():
    # the fused token's call must be the SAME as the clean copy
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known={"NZ1J", "W8FN"})
    _feed(ex, "CQCQPOTADENZ1J W8FN W8FN")
    assert not any(c == "W8FN" and a == "CQ" for c, a in calls), calls


def test_fused_cq_call_alone_never_creates_a_call():
    # an unlisted remainder with no clean copy of its own does not spot
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=set())
    _feed(ex, "CQCQPOTADEAE0YMK E E")
    assert calls == [], calls


def test_mark_distance_sees_moved_letter_breaks_and_lost_marks():
    assert mark_distance("K0SN", "K0STE") == 0   # N -. = T E
    assert mark_distance("K0SN", "TA0SN") == 0   # K -.- = T A
    assert mark_distance("EA5M", "EA5T") == 1    # M -- , T -
    assert mark_distance("WU6P", "WU6W") == 1    # P .--. , W .--
    assert mark_distance("EA5M", "K1ABC") > 2


def _calls(text, known):
    calls = []
    ex = CallsignExtractor(lambda c, a: calls.append((c, a)), known=known)
    _feed(ex, text)
    return calls


def test_keying_garble_of_a_spotted_runner_does_not_spot():
    # CWT 2026-09-16: EA5M copied over and over, then one "EA5T". Both are
    # real, listed calls, so only the marks show the second is a garble.
    calls = _calls("TEST CWT EA5M TEST CWT EA5M TEST CWT EA5M TEST CWT EA5T",
                   known={"EA5M", "EA5T"})
    assert ("EA5T", "CQ") not in calls
    assert ("EA5M", "CQ") in calls


def test_regrouped_marks_of_a_spotted_runner_do_not_spot():
    calls = _calls("CQ TEST K0SN CQ TEST K0SN CQ TEST K0SN CQ TEST K0STE CQ TEST TA0SN",
                   known={"K0SN", "K0STE", "TA0SN"})
    assert [c for c, _ in calls] == ["K0SN", "K0SN"], calls


def test_a_different_station_after_the_runner_still_spots():
    # far apart in marks: a new runner on the frequency is not a garble
    calls = _calls("CQ TEST EA5M CQ TEST EA5M CQ TEST K1ABC CQ TEST K1ABC",
                   known={"EA5M", "K1ABC"})
    assert ("K1ABC", "CQ") in calls


def test_test_after_the_runners_call_belongs_to_the_runner():
    # "CWT NA2U NA2U TEST" ends NA2U's CQ; N5ER right after it is a caller
    calls = _calls("CWT NA2U NA2U TEST N5ER FRED 2455",
                   known={"NA2U", "N5ER"})
    assert ("N5ER", "CQ") not in calls, calls


def test_garbled_scrap_before_test_does_not_take_the_runners_cq():
    # "E2U" is the runner's call garbled, copied once: it owns no TEST
    calls = _calls("E2U TEST CWT NA2U NA2U", known={"NA2U"})
    assert calls == [("NA2U", "CQ")], calls


def test_runner_call_fused_into_its_cq_still_separates_the_caller():
    calls = _calls("CQ F6IRA CWT TEST CG F6IRACWTTEST K4YYL GILF",
                   known={"F6IRA", "K4YYL"})
    assert ("K4YYL", "CQ") not in calls, calls
    calls = _calls("CQ CWT DD2ML CQ CWT DD2MLDD2ML N2EY ULI",
                   known={"DD2ML", "N2EY"})
    assert ("N2EY", "CQ") not in calls, calls


def test_unreadable_word_after_a_cq_is_where_the_runners_call_was():
    calls = _calls("CQ D ? N3KI TU MIKE", known={"N3KI"})
    assert ("N3KI", "CQ") not in calls, calls


def test_a_cq_in_view_beats_an_earlier_de_label():
    # CWT 2026-08-12: one "NA2U DE E NT2U NA2U" made all of NA2U's later
    # CQs DE for as long as the call stayed in the activity memory
    calls = _calls("NA2U DE E NT2U NA2U TU CQ CWT NA2U", known={"NA2U", "NT2U"})
    assert calls[-1] == ("NA2U", "CQ"), calls


def test_unlisted_call_is_not_shaved_to_a_listed_one():
    # CWT 2026-09-16: NN1RR is not in MASTER.SCP but N1RR is. The front-garble
    # repair turned every copy of NN1RR into N1RR, which spotted five times
    calls = _calls("TEST CWT NN1RR NN1RR TEST CWT NN1RR NN1RR TEST",
                   known={"N1RR"})
    assert {c for c, _ in calls} == {"NN1RR"}, calls


def test_context_split_in_the_wrong_places_is_rejoined():
    # CWT 2026-09-16: "CQ POTA DE KS4CR" decoded as "CQP OTADE KS4CR"; no
    # single word is a CQ or a DE, but together they are
    calls = _calls("CQP OTADE KS4CR CQP O TADE KS4CR", known=None)
    assert calls == [("KS4CR", "CQ")], calls


def test_rejoining_leaves_words_that_already_read():
    # "TEST E" must not become "TESTE" and lose its TEST
    calls = _calls("TEST E EA4M TEST E EA4M", known={"EA4M"})
    assert calls == [("EA4M", "CQ")], calls


def test_rejoined_context_is_not_lent_to_a_merged_fragment():
    # an 11 wpm fist with every letter its own word: "C Q" rejoins, but the
    # three-word merge K1A is only a fragment of the call
    calls = _calls("C Q DE K 1 A Y Z C Q DE K 1 A Y Z", known=None)
    assert ("K1A", "CQ") not in calls, calls


def test_pota_is_cq_context():
    # "POTA KT5E" is KT5E's CQ even when the CQ in front of it was lost
    calls = _calls("POTA KT5E KT5E", known=None)
    assert calls == [("KT5E", "CQ")], calls


def test_prosigns_run_onto_the_call_still_make_a_copy():
    # 40 m POTA 2026-10-15: "CQCQDEN8TGQ N8TGQ" is two copies of N8TGQ
    calls = _calls("CQCQDEN8TGQ N8TGQ K", known={"N8TGQ"})
    assert calls == [("N8TGQ", "CQ")], calls


def test_short_calls_need_another_copy():
    # CWT 2026-10-21: "CQ CQ CQ C DE N8A N8A N8AI" -- N8AI with its I lost
    # twice spotted as N8A. A 3-character call, or an unlisted 4-character
    # one, needs a third copy once a database is loaded
    calls = _calls("CQ CQ DE N8A N8A N8AI", known={"W1AW"})
    assert calls == [], calls
    calls = _calls("CQ CQ DE N8A N8A N8A", known={"W1AW"})
    assert calls == [("N8A", "CQ")], calls
    calls = _calls("CQ CWT KG5E KG5E", known={"W1AW"})
    assert calls == [], calls
    # a listed 4-character call still spots on two
    calls = _calls("CQ CWT KG5E KG5E", known={"KG5E"})
    assert calls == [("KG5E", "CQ")], calls


def test_cq_words_run_onto_the_runners_call_belong_to_it():
    # CWT 2026-10-21, 14028.7: "CQ CWTDL8LAS K0AF" is K0AF calling DL8LAS
    calls = _calls("CQ CWT DL8LAS DL8LAS CQ CWTDL8LAS K0AF K0AF",
                   known={"DL8LAS", "K0AF"})
    assert ("K0AF", "CQ") not in calls, calls
    # CWT is boilerplate, so "CQCWT" is a CQ like "CQ CWT"
    assert is_cq_word("CQCWT")
    calls = _calls("CQCWT K1TR CQCWT K1TR", known={"K1TR"})
    assert calls == [("K1TR", "CQ")], calls


def test_a_new_call_on_a_spotted_runners_signal_needs_its_cq_next_to_it():
    # CWT 2026-10-21: WA3AAN's own call garbled to "WA3 TT", and the caller
    # KB9S after it took WA3AAN's TEST
    calls = _calls("TEST WA3AAN TEST WA3AAN TU MARK 28 TEST WA3 TT KB9S KB9S",
                   known={"WA3AAN", "KB9S"})
    assert calls == [("WA3AAN", "CQ")], calls
    # a new runner who sends CQ right before the call still spots
    calls = _calls("TEST WA3AAN TEST WA3AAN CQ CWT KB9S CQ CWT KB9S",
                   known={"WA3AAN", "KB9S"})
    assert calls == [("WA3AAN", "CQ"), ("KB9S", "CQ")], calls
    # and on a signal nothing has been spotted on, junk between is tolerated
    calls = _calls("TEST WA3 TT KB9S KB9S", known={"KB9S"})
    assert calls == [("KB9S", "CQ")], calls
