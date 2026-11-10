#!/usr/bin/env python3
"""Extract per-channel decoder envelopes from an IQ recording.

The decoder does not consume IQ or audio -- it consumes an envelope stream,
one (envelope, noise) pair per STFT frame. That stream is the *only* thing a
decoder test needs, and it is far smaller than the IQ it came from. Measured
on a 30 minute 192 kHz recording (2.76 GB, 908 channels tracked, 2026-09-05):
40 selected channels came to 3.15 MB gzipped, a ~880x reduction at 79 KB per
channel. That per-channel figure was 601 KB before the rounding below and the
removal of the redundant third array -- a 7.6x saving, measured, which is what
makes keeping a whole band-day of these affordable.

So this tool replays a recording through the REAL pipeline -- same
Channelizer, same SignalTracker, same detection and noise baseline -- and
captures what each channel's decoder was actually fed:

    tracker.process_frame():   (vectorized across channels; per channel:)
        env   = power[b-1:b+2].sum()
        noise = self._baseline[b-1:b+2].sum()
        ch.decoder.process(env, noise)

Nothing here reimplements that. The envelopes are captured by wrapping
decoder_cls, which Pipeline already accepts as a parameter, so a change to
the real envelope computation cannot silently diverge from what is extracted.

Output is a JSON list of cases (one per kept channel: frame envelopes, noise,
and the decoded words), so real off-air hand-sent CW can be studied beside
synthetic PARIS-timed cases. Every hard case documented in decoder.py's comments is
about timing that is NOT PARIS; SynthSource cannot produce any of them.

Usage:
    tools/extract_envelope.py --file rec.c64 --center-freq 7050000 \\
        --match CQ --out cases.json

    # keep everything, not just channels matching a marker
    tools/extract_envelope.py --file rec.c64 --center-freq 7050000 --all

Memory scales with the whole recording unless --window-seconds is given: the
per-channel frames are held until the end. Measured on a 2.6 GB / 30 min
capture, peak RSS was 3.25 GB, so a 2 hour 192 kHz capture (11 GB) would want
roughly 14 GB and will not fit on a 15 GB host beside radiod and the skimmer.

--window-seconds drains the accumulated frames periodically. The Pipeline is
NOT restarted -- channels, decoders and the noise baseline run on untouched --
so the same channels are detected either way; only long ones are split into
consecutive cases. Measured on that same recording with --window-seconds 300:

    channels seen   908 both ways (identical detection)
    peak RSS        3.25 GB -> 0.67 GB   (4.9x less)
    elapsed         8:07 -> 7:51
    cases           40 both ways, all 15 callsign tokens shared

Two memory terms exist, and bounding only one is not enough. The window bounds
the per-channel capture buffers; the ACCUMULATED OUTPUT is bounded separately by
trimming to --max-cases as cases are appended rather than only at the end. With
just the window in place three 30-minute captures still peaked at 7.6-11.1 GB,
because 1000 kept cases held 56 million frames while the capture buffers sat near
1 GB. The trim is exact, not an approximation: a case dropped at a moment when
--max-cases longer cases already exist can never re-enter the final top
--max-cases, and an A/B against the untrimmed build produced byte-identical
output at half the peak RSS.

THE TRADE-OFF: a channel whose frames straddle a drain can fall below
--min-frames in every piece and be dropped, where the whole-file run would
have kept it. Keep the window far longer than a typical transmission (300 s
is ~100x a callsign) and this only removes noise channels -- in the run above
it moved 868 short-lived channels from "unselected" to "too short" and changed
no real station. Do not set it to a few seconds.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from openskimmer.dsp.decoder import CWDecoder          # noqa: E402
from openskimmer.dsp.hmm_decoder import HMMDecoder     # noqa: E402
from openskimmer.pipeline import Pipeline              # noqa: E402

BLOCK = 1 << 16          # IQ samples per read


def sig6(x: float) -> float:
    """Round to 6 significant digits, so JSON stores 8 bytes not 18."""
    if x == 0.0 or not np.isfinite(x):
        return 0.0
    return float(f"%.6g" % x)


def make_recording_decoder(base_cls, registry):
    """base_cls, but every (envelope, noise) pair it is fed is retained.

    The tracker reassigns decoder.on_char/on_word_break AFTER construction
    (tracker.py `_create_channel`), so the decoded text is deliberately NOT
    captured here -- it is re-derived later by replaying the frames through
    a fresh decoder, which is also how the harness computes its expected
    output. That keeps a case self-consistent: its frames and its expected
    words are produced by the same code path the Swift port will run.
    """
    class RecordingDecoder(base_cls):
        def __init__(self, frame_dt, on_char, on_word_break, **kw):
            super().__init__(frame_dt, on_char, on_word_break, **kw)
            self.captured: list = []
            registry.append(self)

        def process(self, envelope: float, noise: float) -> None:
            self.captured.append((envelope, noise))
            super().process(envelope, noise)

    return RecordingDecoder


def replay(base_cls, frames, frame_dt, tail_frames=120):
    """Run captured frames through a clean decoder; return words + stats.

    Mirrors dump_cases.py's run(), including the trailing flush, so the
    expected output of an extracted case is computed exactly the way the
    harness computes it for a synthetic one.
    """
    words, chars = [], []

    def on_break():
        if chars:
            words.append("".join(chars))
            chars.clear()

    dec = base_cls(frame_dt, on_char=chars.append, on_word_break=on_break)
    for env, noise in frames:
        dec.process(float(env), float(noise))
    tail = frames[-1][1] if frames else 1.0     # flush against the noise floor
    for _ in range(tail_frames):
        dec.process(float(tail), float(tail))
    on_break()
    return words, dec


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", required=True, help="input .c64 (complex64 IQ)")
    ap.add_argument("--sample-rate", type=int, default=192000)
    ap.add_argument("--center-freq", type=int, required=True,
                    help="recording centre frequency in Hz")
    ap.add_argument("--decoder", choices=("classical", "hmm"), default="hmm",
                    help="decoder to run and to label cases with")
    ap.add_argument("--match", default="",
                    help="keep only channels whose decoded text contains "
                         "this (case-insensitive)")
    ap.add_argument("--freq", action="append", type=float, default=[],
                    metavar="KHZ",
                    help="also keep channels near this frequency (kHz) "
                         "whatever they decoded to; repeatable. Use this "
                         "for a station reported by ear")
    ap.add_argument("--freq-tol", type=float, default=500.0,
                    help="match tolerance for --freq, in Hz (default 500)")
    ap.add_argument("--all", action="store_true",
                    help="keep every channel, ignoring --match and --freq")
    ap.add_argument("--min-frames", type=int, default=400,
                    help="skip channels with fewer frames than this "
                         "(~2 s); too short to be a useful fixture")
    ap.add_argument("--max-cases", type=int, default=40)
    ap.add_argument("--out", default="cases_extracted.json")
    ap.add_argument("--window-seconds", type=float, default=0.0, metavar="S",
                    help="drain accumulated frames every S seconds of audio "
                         "instead of holding the whole recording in memory. "
                         "0 (default) keeps the old whole-file behaviour. The "
                         "Pipeline is NOT restarted, so channel detection is "
                         "unchanged; only long channels are split into "
                         "consecutive cases")
    ap.add_argument("--gzip", action="store_true", help="gzip the output")
    args = ap.parse_args(argv)

    # Resolve and prove the output path before doing any work. The replay
    # is minutes long and unrepeatable once a recording is deleted, so a
    # missing directory must not be discovered at the final write -- it cost
    # exactly that once (2026-09-05, 6.5 min of replay lost to a corpus/ dir
    # that did not exist yet).
    out = args.out + (".gz" if args.gzip and not args.out.endswith(".gz") else "")
    opener = gzip.open if out.endswith(".gz") else open
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with opener(out, "wt") as fh:
        fh.write("[]")

    base_cls = HMMDecoder if args.decoder == "hmm" else CWDecoder
    registry: list = []

    pipeline = Pipeline(
        sample_rate=args.sample_rate,
        center_frequency=args.center_freq,
        on_spot=lambda spot: None,
        decoder_cls=make_recording_decoder(base_cls, registry),
    )
    frame_dt = pipeline.channelizer.hop / args.sample_rate

    # id(decoder) -> bin position, sampled as we go: a channel that is
    # retired mid-recording vanishes from tracker.channels, but its decoder
    # (and its frames) stay in the registry, so the mapping has to be taken
    # while the channel is still alive.
    where: dict = {}

    size = os.path.getsize(args.file)
    total = size // 8

    needle = args.match.upper() if args.match else ""
    wanted = [f * 1000.0 for f in args.freq]
    cases, skipped_short, skipped_nomatch = [], 0, 0
    produced: set = set()          # decoder ids that have yielded >=1 case

    def harvest(final):
        """Turn accumulated frames into cases and DRAIN the buffers.

        Called periodically when --window-seconds is set, so peak memory is
        bounded by one window rather than the whole recording. Draining does
        not disturb the Pipeline: its channels, decoders and noise baseline
        carry on untouched, so the same channels are detected either way.
        A channel alive across a drain simply yields consecutive cases.
        """
        nonlocal skipped_short, skipped_nomatch
        for dec in registry:
            frames = dec.captured
            if len(frames) < args.min_frames:
                # Count a channel short only if it NEVER yielded anything. With
                # windowing every decoder is empty at the final drain, so the
                # naive test reported all 908 channels as "too short" in a run
                # that had just written 40 cases from them.
                if final and id(dec) not in produced:
                    skipped_short += 1
                continue
            dec.captured = []          # drain before anything can fail
            case = build_case(dec, frames)
            if case is None:
                skipped_nomatch += 1
                continue
            produced.add(id(dec))
            cases.append(case)
            # Trim as we go, not only at the end. --max-cases used to break out
            # of a single pass; with windowing there are many passes, so the
            # list grew unbounded and became the DOMINANT memory term -- the
            # three WES captures peaked at 7.6-11.1 GB even though the capture
            # buffers stayed near 1 GB. Keeping a slack factor avoids sorting
            # on every append while still bounding the list.
            if len(cases) > args.max_cases * 2:
                cases.sort(key=lambda c: -len(c["frames"]))
                del cases[args.max_cases:]

    def build_case(dec, frames):
        binpos = where.get(id(dec))
        freq = None
        if binpos is not None:
            freq = (args.center_freq
                    + pipeline.channelizer.bin_to_offset_hz(binpos))

        words, fresh = replay(base_cls, frames, frame_dt)
        text = " ".join(words)

        # Keep a channel if it SAYS the marker, or if it sits on a frequency
        # asked for by name. Text alone is not enough, and the reason is the
        # whole point of this corpus: VA3TAR (7054.99 kHz, 12 wpm, straight
        # key, 2026-09-05) is an ideal fixture -- and it never sends the marker,
        # and the decoder never recovers its callsign either, emitting only
        # "CQ CQ CS DE M M K CMA". No text filter can select the cases we
        # are worst at, because being bad at them is what removes the text.
        # Frequency is also how an operator actually reports one of these:
        # "very strong station on 7.055".
        keep = args.all or (needle and needle in text.upper())
        if not keep and wanted:
            keep = freq is not None and any(
                abs(freq - w) <= args.freq_tol for w in wanted)
        if not keep:
            return None

        return ({
            "name": f"offair_{len(cases):02d}"
                    + (f"_{freq / 1000:.1f}kHz" if freq else ""),
            "decoder": args.decoder,
            "prosign_case": False,
            "frame_dt": frame_dt,
            # The harness feeds a constant noise of 1.0, so `frames` is the
            # normalised ratio env/noise and an unmodified harness reproduces
            # `words` from it directly. `noise` preserves the live floor the
            # decoder actually saw (real QSB, unlike the harness constant);
            # the raw envelope is frames[i] * noise[i], so it is not stored.
            #
            # Values are rounded to 6 significant digits. That is ~1e-6
            # relative, orders below anything a threshold or Viterbi decision
            # turns on, and it is what makes the corpus cheap to keep: full
            # float repr costs ~18 bytes per sample and three arrays where
            # two will do, which is a 4x difference across a whole band-day.
            "frames": [sig6(float(e) / float(n)) if n else 0.0
                       for e, n in frames],
            "noise": [sig6(float(n)) for e, n in frames],
            "tail_value": float(frames[-1][1]),
            "tail_frames": 120,
            "words": words,
            "chars_total": fresh.chars_total,
            "chars_et": fresh.chars_et,
            "dit_ms": fresh.dit_ms,
            "wpm": fresh.wpm,
            "source": {
                "file": os.path.basename(args.file),
                "freq_hz": freq,
                "sample_rate": args.sample_rate,
                "off_air": True,     # real signal, provenance unknown:
                                     # a keyer and a bug look alike here
            },
        })

    # Drain on a window boundary, then once more at the end.
    window_samples = int(args.window_seconds * args.sample_rate)
    next_drain = window_samples if window_samples else 0
    done = 0
    with open(args.file, "rb") as fh:
        while True:
            raw = np.fromfile(fh, dtype=np.complex64, count=BLOCK)
            if raw.size == 0:
                break
            pipeline.process(raw)
            for ch in pipeline.tracker.channels.values():
                where[id(ch.decoder)] = ch.bin_position
            done += raw.size
            if next_drain and done >= next_drain:
                harvest(final=False)
                next_drain += window_samples
            pct = 100.0 * done / total if total else 0
            print(f"\r  replaying {pct:5.1f}%  cases={len(cases)}",
                  end="", file=sys.stderr)
    print(file=sys.stderr)
    harvest(final=True)

    if len(cases) > args.max_cases:
        cases.sort(key=lambda c: -len(c["frames"]))
        del cases[args.max_cases:]

    cases.sort(key=lambda c: -len(c["frames"]))
    # Names were assigned as cases were appended; with windowing that order is
    # interleaved across drains, so renumber once the final order is known.
    for i, c in enumerate(cases):
        c["name"] = re.sub(r"^offair_\d+", "offair_%02d" % i, c["name"])
    with opener(out, "wt") as fh:
        json.dump(cases, fh)

    print(f"channels seen      : {len(registry)}")
    print(f"  too short        : {skipped_short}")
    sel = f"'{args.match}'" if needle else "(no text match)"
    if wanted:
        sel += " or " + ", ".join(f"{w / 1000:.2f}" for w in wanted) + " kHz"
    print(f"  unselected       : {skipped_nomatch}  [wanted {sel}]")
    print(f"cases written      : {len(cases)} -> {out} "
          f"({os.path.getsize(out) / 1024:.0f} KB)")
    for c in cases[:12]:
        print("  %-26s %6d frames  %s"
              % (c["name"], len(c["frames"]), " ".join(c["words"])[:60]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
