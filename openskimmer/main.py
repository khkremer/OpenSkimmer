"""OpenSkimmer CLI: headless skimmer (phase 1 -- Telnet output, no GUI)."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import threading
import time

import numpy as np

from .pipeline import Pipeline
from .sdr import create_source
from .spotting.spotter import Spot
from .spotting.telnet import TelnetSpotServer, format_spot

log = logging.getLogger("openskimmer")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openskimmer",
        description="Open source multi-channel CW skimmer",
    )
    p.add_argument("--source",
                   choices=["hl2", "kiwi", "sdrplay", "miri", "rtlsdr",
                            "airspy", "ka9q", "soapy", "synth", "file"],
                   default="hl2")
    p.add_argument("--host", help="radio host/IP (hl2: default broadcast "
                                  "discovery; kiwi: required)")
    p.add_argument("--port", type=int, default=8073,
                   help="KiwiSDR port (kiwi source)")
    # Default from the environment, NOT the command line. An argv value is
    # world-readable via `ps` to every user on the host, and this one was
    # spotted independently by two outside readers before it was fixed.
    # $KIWI_PASSWORD is readable only by the owner and root
    # (/proc/PID/environ on Linux), so start_skimmer.sh exports it instead of
    # passing it. The flag still works for ad-hoc runs and overrides the env.
    p.add_argument("--kiwi-password",
                   default=os.environ.get("KIWI_PASSWORD", ""),
                   help="KiwiSDR password (private/local units). Prefer the "
                        "KIWI_PASSWORD environment variable: an argv value is "
                        "visible in ps to every user on the host")
    p.add_argument("--sample-rate", type=int, default=192000,
                   help="IQ sample rate in Hz (hl2: 48/96/192/384 k; kiwi: "
                        "total span, rounded to 12 kHz slices, max 96 k)")
    p.add_argument("--freq", type=float, default=14_030_000,
                   help="passband center frequency in Hz")
    p.add_argument("--band", action="append", metavar="[SOURCE:]CENTER[/SPAN]",
                   help="skim a band (repeatable): center Hz, optional span "
                        "Hz and source, e.g. --band hl2:14030000/192000 "
                        "--band kiwi:7030000/36000. Bands share one spot "
                        "server; each gets a waterfall on web-port, "
                        "web-port+1, ... Defaults: --source / --sample-rate")
    p.add_argument("--file", help="IQ recording path (file source)")
    p.add_argument("--record", metavar="PATH",
                   help="write the raw IQ stream to PATH as complex64; "
                        "replay with --source file --file PATH (name it .c64)")
    p.add_argument("--start-time", metavar="UTC",
                   help="the recording's true UTC start, e.g. "
                        "2026-08-12T19:00:00Z or 20260812T1900Z. Replay then "
                        "runs on a MEDIA CLOCK -- time advancing with audio "
                        "rather than wall clock -- so respot/dedup behave as "
                        "they did on the air and spot timestamps are real UTC, "
                        "matchable against the RBN archive. Taken from the "
                        "filename automatically when it carries one "
                        "(tools/record_iq.py writes <label>_<stamp>Z.c64)")
    p.add_argument("--log-channels", metavar="PATH",
                   help="append every decoded word to PATH with UTC time, "
                        "frequency, channel id, WPM and SNR (decoder "
                        "diagnostics). On replay the time is audio time, so "
                        "the log lines up with the spots and an RBN archive")
    p.add_argument("--log-chars", metavar="PATH",
                   help="append every decoded character and word break to "
                        "PATH with the gap before it (ms), the dit (ms) and "
                        "the word threshold (dits): input for "
                        "tools/replay_chars.py. Several lines per word, so "
                        "for replays rather than live use")
    p.add_argument("--max-wpm", type=int, default=50, metavar="WPM",
                   help="never spot a signal decoded faster than this "
                        "(default 50). The decoder clamps its dit estimate at "
                        "15 ms, which IS 80 wpm, so a spot at 80 means the "
                        "estimate railed against its floor -- what a steady "
                        "carrier or noise source does to it, not a station. "
                        "0 disables the check")
    p.add_argument("--block", action="append", default=[], metavar="CALL",
                   help="never spot this callsign; repeatable, and accepts a "
                        "comma-separated list. For calls a noise source "
                        "produces repeatedly -- a wrong one is worse than a "
                        "missed one when it sends DXers chasing rare DX")
    p.add_argument("--max-channels", type=int, default=100,
                   help="most signals tracked at once. A contest hour on a "
                        "wide segment saturates the default: once full the "
                        "tracker silently refuses new signals, so they are "
                        "never decoded at all. Watch for the saturation "
                        "warning and raise this, at the cost of CPU")
    p.add_argument("--decoder", choices=["classical", "hmm"],
                   default="hmm",
                   help="per-signal decoder: fixed-lag Viterbi HMM (default, "
                        "more sensitive) or the classical threshold decoder")
    p.add_argument("--segmenter", choices=["timing", "widest"],
                   default="timing",
                   help="how words reach the callsign extractor: the "
                        "decoder's own breaks (default), or also cut at a "
                        "gap clearly wider than every other inside a word "
                        "")
    p.add_argument("--patterns", metavar="PATH",
                   help="Patt3Ch.lst callsign-pattern file (reversebeacon"
                        ".net); calls with unlisted patterns need extra "
                        "confirmation before spotting")
    p.add_argument("--scp", metavar="PATH",
                   help="Super Check Partial callsign list (MASTER.SCP from "
                        "supercheckpartial.com); known calls spot faster and "
                        "win over garbled variants of themselves")
    p.add_argument("--scp-weak", metavar="PATH",
                   help="a second, broader callsign list (e.g. combined.txt) "
                        "used only to DISCREDIT garbles, never to spot on a "
                        "single copy. A loose list vetoes more garbles but "
                        "also blesses more of them; splitting the two roles "
                        "gets both (curated list in --scp, broad one here)")
    p.add_argument("--my-call", default="SKIMMER",
                   help="callsign announced in telnet spots")
    p.add_argument("--telnet-port", type=int, default=7300)
    p.add_argument("--telnet-bind", default="0.0.0.0")
    p.add_argument("--telnet-open", action="store_true",
                   help="don't ask telnet clients for their call on connect")
    p.add_argument("--op-name", default="",
                   help="operator name announced in the telnet banner")
    p.add_argument("--grid", default="",
                   help="grid square announced in the telnet banner")
    p.add_argument("--qth", default="",
                   help="location text in the telnet banner, e.g. "
                        "'Newington, CT - USA' (for RBN Aggregator primary)")
    p.add_argument("--web-port", type=int, default=7380,
                   help="waterfall UI port (0 disables the web UI)")
    p.add_argument("--min-snr", type=int, default=3,
                   help="minimum SNR (dB) to spot")
    p.add_argument("--respot", type=float, default=600.0,
                   help="seconds before the same call/freq is spotted again "
                        "(default matches the RBN's dedup window; lower it "
                        "only for interactive/waterfall use, as the RBN will "
                        "flag the extra spots as duplicates)")
    p.add_argument("--respot-tol-hz", type=float, default=1000.0,
                   help="frequency drift tolerated between sightings of the "
                        "same call within --respot seconds before it counts "
                        "as a re-spot rather than a new one (absorbs a "
                        "channel being retired and recreated between a "
                        "station's own CQ cycles in a crowded pileup)")
    p.add_argument("--lna-gain", type=int, default=19,
                   help="HL2 LNA gain in dB (-12..48)")
    p.add_argument("--gain", type=float, default=None,
                   help="SoapySDR manual gain (disables AGC); omit for AGC")
    p.add_argument("--ppm", type=float, default=0.0,
                   help="SoapySDR frequency correction in ppm")
    p.add_argument("--band-offset", action="append", default=[],
                   metavar="CENTER_HZ=OFFSET_HZ",
                   help="shift the frequency REPORTED for one band by OFFSET_HZ "
                        "without changing the tuning, to correct a dongle whose "
                        "frequency error the driver does not correct, e.g. "
                        "--band-offset 144075000=2330")
    p.add_argument("--bias-t", action="store_true",
                   help="enable the SoapySDR device's antenna bias-T (DC "
                        "power out); off by default and forced off on connect")
    p.add_argument("--rtp-port", type=int, default=5004,
                   help="ka9q-radio RTP multicast port")
    p.add_argument("--hl2-filters", choices=["n2adr", "none"],
                   default="n2adr",
                   help="HL2 companion filter board to drive (default "
                        "n2adr; harmless if absent)")
    p.add_argument("--workers", choices=["auto", "process", "thread"],
                   default="auto",
                   help="how to run multiple bands: separate processes "
                        "(default for 2+ bands) or threads. The per-signal "
                        "decoders are pure Python, so band THREADS serialise "
                        "on the GIL and all bands together cannot exceed one "
                        "core; processes scale across cores. 'auto' uses a "
                        "thread for a single band and processes beyond that")
    p.add_argument("--quiet", action="store_true", help="don't print spots to stdout")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def parse_start_time(text: str) -> float:
    """"2026-08-12T19:00:00Z" / "20260812T1900Z" / "2026-08-12 19:00" -> epoch."""
    import datetime as _dt
    t = text.strip().rstrip("Zz")
    # The compact minute form MUST be tried before the second form: strptime
    # matches %H%M%S loosely, so "20260715T1902" would otherwise parse as
    # 19:00:02 rather than 19:02. The reverse is safe -- a 6-digit stamp
    # leaves unconverted data under %H%M and falls through.
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M",
                "%Y%m%dT%H%M", "%Y%m%dT%H%M%S"):
        try:
            return (_dt.datetime.strptime(t, fmt)
                    .replace(tzinfo=_dt.timezone.utc).timestamp())
        except ValueError:
            continue
    raise SystemExit(f"bad --start-time {text!r}: expected e.g. "
                     "2026-08-12T19:00:00Z or 20260812T1900Z")


def start_time_from_name(path: str) -> "Optional[float]":
    """Recover the UTC start from a recording's filename, if it carries one:
    recordings/cwt20m_20260812T1900Z.c64 -> epoch. tools/record_iq.py writes
    exactly this, so a capture is self-describing."""
    import os
    import re
    m = re.search(r"(\d{8}T\d{4,6})Z", os.path.basename(path or ""))
    if not m:
        return None
    try:
        return parse_start_time(m.group(1))
    except SystemExit:
        return None


def merge_ranges(ranges, tol_hz: float = 1000.0) -> list:
    """Merge (low, high) Hz ranges that touch or are within tol_hz of each
    other. Bridges the ~200 Hz inter-slice gaps in KiwiSDR coverage while
    leaving distinct bands (MHz apart) and real dead-slice gaps separate."""
    out = []
    for lo, hi in sorted(ranges):
        if out and lo <= out[-1][1] + tol_hz:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def parse_band_spec(spec: str, default_source: str,
                    default_span: int) -> "tuple[str, Optional[str], int, int]":
    """"[source[@host]:]center[/span]" -> (source, host, center_hz, span_hz).

    host defaults to None (use --host); e.g.
    "hl2@192.0.2.10:14030000/192000", "kiwi@kiwisdr.local:7030000/24000".
    """
    source, host, body = default_source, None, spec
    if ":" in spec:
        source, body = spec.split(":", 1)
        if "@" in source:
            source, host = source.split("@", 1)
    span = default_span
    if "/" in body:
        body, span_s = body.split("/", 1)
        span = int(float(span_s))
    try:
        return source, host, int(float(body)), span
    except ValueError:
        raise SystemExit(f"bad --band spec: {spec!r}")


def make_source(args, source_name: str, host, center: int, span: int):
    host = host or args.host
    if source_name == "hl2":
        return create_source("hl2", host=host, lna_gain_db=args.lna_gain,
                             filter_board=args.hl2_filters)
    if source_name == "kiwi":
        if not host:
            raise SystemExit("kiwi bands need a host (--host or source@host:)")
        return create_source("kiwi", host=host, port=args.port,
                             password=args.kiwi_password)
    if source_name == "file":
        if not args.file:
            raise SystemExit("--file is required with the file source")
        return create_source("file", path=args.file,
                             sample_rate=span, center_frequency=center)
    if source_name == "ka9q":
        if not host:
            raise SystemExit("ka9q bands need the multicast group: "
                             "ka9q@GROUP:center/span")
        return create_source("ka9q", group=host, port=args.rtp_port)
    if source_name in ("sdrplay", "soapy", "miri", "rtlsdr", "airspy"):
        # host field carries the device selector (serial or key=val args)
        return create_source(source_name, device_args=host or "",
                             gain_db=args.gain,
                             agc=args.gain is None,
                             ppm=args.ppm, bias_t=args.bias_t)
    if source_name != "synth":
        raise SystemExit(f"unknown source {source_name!r}")
    # demo scenario: a few stations calling CQ in the passband
    from .sdr.synth import SynthSignal
    return create_source("synth", sample_rate=span,
                         center_frequency=center, realtime=True,
                         signals=[
                             SynthSignal(-3200, 28, 25, "CQ TEST DE W1AW W1AW"),
                             SynthSignal(1500, 22, 15, "CQ CQ DE DL1ABC DL1ABC K",
                                         start_s=2.0),
                             SynthSignal(5800, 35, 10, "CQ DE JA3XYZ JA3XYZ",
                                         start_s=5.0),
                         ])


def shared_radio_conflict(bands) -> "Optional[str]":
    """Name a radio that two bands can only share inside one process.

    The HL2 is a single-host-session radio: several `--band hl2@host:...`
    arguments share one UDP session and one set of DDC receivers through a
    module-level registry (`hl2._radios`). That registry is per-process, so
    splitting those bands across processes would have each child open its
    own session and fight over the radio. Returns a description of the
    clash, or None if every band can stand alone.
    """
    seen = {}
    for source_name, host, _center, _span in bands:
        if source_name != "hl2":
            continue
        key = host or "*"
        seen[key] = seen.get(key, 0) + 1
        if seen[key] > 1:
            return f"{seen[key]} hl2 bands on {key}"
    return None


def worker_mode(requested: str, bands) -> str:
    """Resolve --workers to "process" or "thread".

    "auto" keeps a single band on a thread -- one band gains nothing from a
    second process, and the thread path leaves replay and --record behaving
    exactly as they always have -- and puts 2+ bands in processes, where the
    GIL would otherwise cap them all at one core.

    Bands that must share one radio session override the choice either way:
    correctness beats parallelism, and an explicit --workers process cannot
    be honoured for them.
    """
    conflict = shared_radio_conflict(bands)
    if conflict:
        if requested == "process":
            log.warning("--workers process ignored: %s must share one "
                        "process to share the radio session", conflict)
        return "thread"
    if requested != "auto":
        return requested
    return "thread" if len(bands) <= 1 else "process"


def _load_decoder_and_dbs(args):
    """Decoder class plus the validation databases.

    Shared by both worker strategies; in process mode each child loads its
    own copy rather than inheriting one, so the same code path works under
    spawn (macOS/Windows) as under fork.
    """
    decoder_cls = None
    if args.decoder == "hmm":
        from .dsp.hmm_decoder import HMMDecoder
        decoder_cls = HMMDecoder

    known_calls = None
    if args.scp:
        from .spotting.callsign import load_scp
        known_calls = load_scp(args.scp)
        log.info("Loaded %d known calls from %s", len(known_calls), args.scp)

    known_weak = None
    if getattr(args, "scp_weak", None):
        from .spotting.callsign import load_scp
        known_weak = load_scp(args.scp_weak)
        log.info("Loaded %d veto-only calls from %s",
                 len(known_weak), args.scp_weak)

    call_patterns = None
    if args.patterns:
        from .spotting.callsign import load_patterns
        call_patterns = load_patterns(args.patterns)
        log.info("Loaded %d call patterns from %s",
                 len(call_patterns), args.patterns)
    return decoder_cls, known_calls, call_patterns, known_weak


def _blocked(args) -> set:
    """--block CALL, repeatable and/or comma-separated, upper-cased."""
    out = set()
    for item in getattr(args, "block", []) or []:
        for c in item.replace(",", " ").split():
            out.add(c.strip().upper())
    return out


def _char_line(info) -> str:
    """One --log-chars line: time to the ms, kHz, channel, WPM, SNR, the
    character ("|" for a word break), then gap/dit in ms and the word
    threshold in dits ("-" where there is none)."""
    t = info["t"]
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t))
    gap = info["gap"]
    tail = ("%7.1f %6.1f %4.2f" % gap) if gap else "      -      -    -"
    return "%s.%03dZ %11.4f %5d %3d %3d  %-6s %s\n" % (
        stamp, int(t * 1000) % 1000, info["freq_hz"] / 1000.0, info["chan"],
        info["wpm"], info["snr_db"], info["char"], tail)


def _band_offset(args, center: int) -> int:
    """Reporting offset for the band tuned at `center` (see --band-offset)."""
    for item in args.band_offset:
        c, _, off = item.partition("=")
        try:
            if int(float(c)) == center:
                return int(float(off))
        except ValueError:
            raise SystemExit(f"bad --band-offset {item!r}: want CENTER_HZ=OFFSET_HZ")
    return 0


DNS_RETRY_S = 120.0


def _connect_retrying_dns(source, label, timeout_s=None) -> None:
    """connect(), retrying while the host name does not resolve yet.

    After a reboot the skimmer can start before radiod has advertised its
    mDNS stream names (seen in practice: every band failed one second
    after radiod came up). If only some bands fail, the parent keeps running
    and those bands stay dead until the next restart, so ride out the race
    here. Any other error still fails at once.
    """
    deadline = time.monotonic() + (DNS_RETRY_S if timeout_s is None
                                   else timeout_s)
    delay = 1.0
    while True:
        try:
            source.connect()
            return
        except socket.gaierror as e:
            if time.monotonic() + delay > deadline:
                raise
            log.warning("%s: %s; retrying in %.0f s", label, e, delay)
            time.sleep(delay)
            delay = min(delay * 2, 15.0)


def _band_worker(args, band, index, q) -> None:
    """Own one band end to end in a child process.

    Runs the source and the whole DSP pipeline, and ships results to the
    parent over `q` as (kind, index, payload) tuples. The parent keeps the
    single telnet server, the waterfall pages and the channel log, so the
    deployment still looks like one skimmer from outside.

    Spots and words are delivered with a blocking put -- losing one would
    lose a spot. Waterfall rows and channel snapshots are best-effort: if
    the parent falls behind, they are dropped rather than stalling the DSP
    loop, which is the whole point of moving the band out here.
    """
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    source_name, host, center, span = band
    try:
        source = make_source(args, source_name, host, center, span)
        source.set_sample_rate(span)
        source.set_center_frequency(center)
        _connect_retrying_dns(source, f"{source_name}/{center}")
    except BaseException as e:                 # SystemExit included
        q.put(("failed", index, f"{source_name}/{center}: {e}"))
        return

    rate = source.sample_rate or span
    q.put(("ready", index, {"name": source_name, "center": center,
                            "rate": rate, "coverage": source.coverage_hz}))

    def emit(kind, payload, droppable=False):
        if droppable:
            try:
                q.put_nowait((kind, index, payload))
            except Exception:                  # queue.Full -- UI data only
                pass
        else:
            q.put((kind, index, payload))

    decoder_cls, known_calls, call_patterns, known_weak = \
        _load_decoder_and_dbs(args)
    want_web = bool(args.web_port)
    if want_web:
        from .web.server import spectrum_to_row

    pipeline = Pipeline(
        sample_rate=rate,
        center_frequency=center + _band_offset(args, center),
        on_spot=lambda spot: emit("spot", spot),
        min_snr_db=args.min_snr,
        max_wpm=args.max_wpm,
        blocked_calls=_blocked(args),
        max_channels=args.max_channels,
        respot_interval_s=args.respot,
        respot_tol_hz=args.respot_tol_hz,
        coverage_fn=lambda: source.coverage_hz,
        on_spectrum=((lambda power: emit("row", spectrum_to_row(power), True))
                     if want_web else None),
        on_channels=((lambda chans: emit("chans", chans, True))
                     if want_web else None),
        on_word=((lambda info: emit("word", info))
                 if args.log_channels else None),
        on_char=((lambda info: emit("char", info))
                 if args.log_chars else None),
        decoder_cls=decoder_cls,
        segmenter=args.segmenter,
        known_calls=known_calls,
        call_patterns=call_patterns,
        known_calls_weak=known_weak,
        my_call=args.my_call,
        media_clock_epoch=getattr(args, "media_epoch", None),
    )

    rec = open(args.record, "wb") if args.record else None
    last_cov = source.coverage_hz
    try:
        for block in source.stream_iq():
            if rec is not None:
                np.asarray(block, dtype=np.complex64).tofile(rec)
            pipeline.process(block)
            cov = source.coverage_hz
            if cov != last_cov:                # SETT segments live in the parent
                last_cov = cov
                emit("cov", cov)
    except KeyboardInterrupt:
        pass
    except Exception:
        log.exception("band worker %s/%.0f died", source_name, center)
    finally:
        if rec is not None:
            rec.close()
        try:
            source.disconnect()
        except Exception:
            pass
        q.put(("done", index, None))


async def run(args) -> int:
    specs = args.band or [f"{args.source}:{int(args.freq)}/{args.sample_rate}"]
    bands = [parse_band_spec(s, args.source, args.sample_rate) for s in specs]
    if len(bands) != len({c for _, _, c, _ in bands}):
        raise SystemExit("duplicate --band center frequencies")
    if args.record and len(bands) > 1:
        raise SystemExit("--record works with a single band")

    # a media clock only makes sense for replay; a live source already
    # advances at real time
    args.media_epoch = None
    if args.source == "file" or any(b[0] == "file" for b in bands):
        if args.start_time:
            args.media_epoch = parse_start_time(args.start_time)
        else:
            args.media_epoch = start_time_from_name(args.file)
        if args.media_epoch is not None:
            log.info("replay on the media clock, anchored at %s UTC",
                     time.strftime("%Y-%m-%d %H:%M:%S",
                                   time.gmtime(args.media_epoch)))

    mode = worker_mode(args.workers, bands)
    if mode == "process":
        return await _run_processes(args, bands)
    return await _run_threads(args, bands)


async def _run_processes(args, bands) -> int:
    """One child process per band: separate interpreters, separate GILs.

    The per-signal decoders are pure Python and hold the GIL, so band
    threads serialise onto a single core no matter how many cores are
    free (measured: three bands pinned at 97% of one core with three cores
    idle and the receive queues overflowing). Processes remove that ceiling.
    """
    import multiprocessing as mp
    import queue as queue_mod

    ctx = mp.get_context()
    q = ctx.Queue(maxsize=2048)
    loop = asyncio.get_running_loop()

    state = {i: {"name": b[0], "center": b[2], "rate": b[3],
                 "coverage": None, "ready": False}
             for i, b in enumerate(bands)}

    def segments():
        """Skimmed absolute ranges across all bands (SETT), from whatever
        the children have reported so far."""
        out = []
        for st in state.values():
            if not st["ready"]:
                continue
            cov = st["coverage"]
            if cov is None:
                cov = [(-st["rate"] / 2, st["rate"] / 2)]
            out.extend((st["center"] + lo, st["center"] + hi)
                       for lo, hi in cov)
        return merge_ranges(out)

    telnet = TelnetSpotServer(args.my_call, host=args.telnet_bind,
                              port=args.telnet_port, op_name=args.op_name,
                              grid=args.grid, qth=args.qth,
                              ask_call=not args.telnet_open,
                              segments_fn=segments)
    await telnet.start()

    webs = {}
    chan_log = open(args.log_channels, "a") if args.log_channels else None
    char_log = open(args.log_chars, "a") if args.log_chars else None
    done = asyncio.Event()
    alive = [len(bands)]
    failed = []

    async def start_web(index):
        from .dsp.channelizer import Channelizer
        from .web.server import WaterfallServer
        st = state[index]
        web = WaterfallServer(center_hz=st["center"], span_hz=st["rate"],
                              fft_size=Channelizer(st["rate"]).fft_size,
                              my_call=args.my_call.upper(),
                              port=args.web_port + index)
        try:
            await web.start()
        except OSError as e:
            # the waterfall UI is optional -- a port clash must not take the
            # skimmer down (spots still flow over telnet)
            log.warning("web UI disabled on port %d: %s",
                        args.web_port + index, e)
            return
        webs[index] = web

    def one_down():
        alive[0] -= 1
        if alive[0] <= 0:
            done.set()

    def handle(msg) -> None:
        kind, index, payload = msg
        if kind == "spot":
            if not args.quiet:
                print(format_spot(payload, args.my_call.upper()), flush=True)
            telnet.broadcast(payload)
            out = {"type": "spot", "freq_hz": payload.freq_hz,
                   "call": payload.call, "snr_db": payload.snr_db,
                   "wpm": payload.wpm, "activity": payload.activity,
                   "time": payload.time_utc}
            for web in webs.values():   # each page keeps its own log
                web.broadcast_json(out)
        elif kind == "row":
            web = webs.get(index)
            if web is not None:
                web.broadcast_row(payload)
        elif kind == "chans":
            web = webs.get(index)
            if web is not None:
                web.broadcast_json({"type": "channels", "channels": payload})
        elif kind == "word":
            chan_log.write("%s %9.2f %5d %3d %3d  %s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%SZ",
                              time.gmtime(payload["t"])),
                payload["freq_hz"] / 1000.0, payload["chan"],
                payload["wpm"], payload["snr_db"], payload["word"]))
            chan_log.flush()
        elif kind == "char":
            char_log.write(_char_line(payload))
        elif kind == "cov":
            state[index]["coverage"] = payload
        elif kind == "ready":
            state[index].update(payload)
            state[index]["ready"] = True
            log.info("Skimming %s at %.1f kHz, %d kHz passband",
                     payload["name"], payload["center"] / 1000,
                     payload["rate"] // 1000)
            if args.web_port:
                asyncio.ensure_future(start_web(index))
        elif kind == "failed":
            log.error("band worker failed: %s", payload)
            failed.append(payload)
            one_down()
        elif kind == "done":
            one_down()

    procs = []
    for i, band in enumerate(bands):
        p = ctx.Process(target=_band_worker, args=(args, band, i, q),
                        name=f"skim-{band[0]}-{band[2] // 1000}", daemon=True)
        p.start()
        procs.append(p)

    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                msg = q.get(timeout=0.25)
            except queue_mod.Empty:
                continue
            except (OSError, EOFError):
                break
            loop.call_soon_threadsafe(handle, msg)

    pump = threading.Thread(target=reader, name="band-queue", daemon=True)
    pump.start()
    log.info("%d band(s) in separate processes; telnet on port %d",
             len(bands), args.telnet_port)
    try:
        await done.wait()
    finally:
        stop.set()
        for p in procs:
            if p.is_alive():
                p.terminate()
        for p in procs:
            p.join(timeout=3)
        pump.join(timeout=1)
        await telnet.stop()
        for web in webs.values():
            await web.stop()
        if chan_log is not None:
            chan_log.close()
        if char_log is not None:
            char_log.close()
    # a band that died is a failure, not a clean finish -- exit non-zero so
    # systemd's Restart=on-failure actually fires. The common case is every
    # band failing at startup because radiod is not up yet (its mDNS names
    # do not resolve); without this the unit exits 0 and stays dead.
    if failed:
        log.error("%d of %d band(s) failed; exiting non-zero",
                  len(failed), len(bands))
        return 1
    return 0


async def _run_threads(args, bands) -> int:
    loop = asyncio.get_running_loop()
    failed = []
    units = []
    for source_name, host, center, span in bands:
        source = make_source(args, source_name, host, center, span)
        try:
            source.set_sample_rate(span)
        except ValueError as e:
            raise SystemExit(str(e))
        source.set_center_frequency(center)
        _connect_retrying_dns(source, f"{source_name}/{center}")
        units.append({"name": source_name, "center": center,
                      "source": source,
                      "rate": source.sample_rate or span})

    def segments():
        """Skimmed absolute ranges across all bands (SETT); dead slices
        drop out of their band's contribution. Adjacent KiwiSDR slices are
        merged into one contiguous range (they leave a ~200 Hz gap because
        each slice's passband is slightly narrower than its 12 kHz spacing);
        genuinely separate bands and real dead-slice gaps stay split."""
        out = []
        for u in units:
            cov = u["source"].coverage_hz
            if cov is None:
                cov = [(-u["rate"] / 2, u["rate"] / 2)]
            out.extend((u["center"] + lo, u["center"] + hi) for lo, hi in cov)
        return merge_ranges(out)

    telnet = TelnetSpotServer(args.my_call, host=args.telnet_bind,
                              port=args.telnet_port, op_name=args.op_name,
                              grid=args.grid, qth=args.qth,
                              ask_call=not args.telnet_open,
                              segments_fn=segments)
    await telnet.start()

    webs = []
    if args.web_port:
        from .dsp.channelizer import Channelizer
        from .web.server import WaterfallServer
        for i, u in enumerate(units):
            u["web"] = None
            web = WaterfallServer(center_hz=u["center"], span_hz=u["rate"],
                                  fft_size=Channelizer(u["rate"]).fft_size,
                                  my_call=args.my_call.upper(),
                                  port=args.web_port + i)
            try:
                await web.start()
            except OSError as e:
                # the waterfall UI is optional -- a port clash must not take
                # the skimmer down (spots still flow over telnet)
                log.warning("web UI disabled on port %d: %s",
                            args.web_port + i, e)
                continue
            u["web"] = web
            webs.append(web)

    def on_spot(spot: Spot) -> None:
        # called on a DSP thread
        if not args.quiet:
            print(format_spot(spot, args.my_call.upper()), flush=True)
        loop.call_soon_threadsafe(telnet.broadcast, spot)
        msg = {"type": "spot", "freq_hz": spot.freq_hz, "call": spot.call,
               "snr_db": spot.snr_db, "wpm": spot.wpm,
               "activity": spot.activity, "time": spot.time_utc}
        for web in webs:  # each page keeps its own log; labels clip to span
            loop.call_soon_threadsafe(web.broadcast_json, msg)

    chan_log = open(args.log_channels, "a") if args.log_channels else None
    char_log = open(args.log_chars, "a") if args.log_chars else None

    def on_char(info) -> None:
        char_log.write(_char_line(info))

    decoder_cls, known_calls, call_patterns, known_weak = \
        _load_decoder_and_dbs(args)

    def on_word(info) -> None:
        # called on a DSP thread; plain line-buffered append
        chan_log.write("%s %9.2f %5d %3d %3d  %s\n" % (
            time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime(info["t"])),
            info["freq_hz"] / 1000.0, info["chan"], info["wpm"],
            info["snr_db"], info["word"]))
        chan_log.flush()

    from .web.server import spectrum_to_row
    done = asyncio.Event()
    alive = [0]
    rec = open(args.record, "wb") if args.record else None

    for u in units:
        web = u.get("web")

        def on_spectrum(power, web=web):
            row = spectrum_to_row(power)  # encode on the DSP thread
            loop.call_soon_threadsafe(web.broadcast_row, row)

        def on_channels(chans, web=web):
            loop.call_soon_threadsafe(web.broadcast_json,
                                      {"type": "channels", "channels": chans})

        u["pipeline"] = Pipeline(
            sample_rate=u["rate"],
            center_frequency=u["center"] + _band_offset(args, u["center"]),
            on_spot=on_spot,
            min_snr_db=args.min_snr,
        max_wpm=args.max_wpm,
        blocked_calls=_blocked(args),
        max_channels=args.max_channels,
            respot_interval_s=args.respot,
            respot_tol_hz=args.respot_tol_hz,
            coverage_fn=lambda source=u["source"]: source.coverage_hz,
            on_spectrum=on_spectrum if web is not None else None,
            on_channels=on_channels if web is not None else None,
            on_word=on_word if chan_log is not None else None,
            on_char=on_char if char_log is not None else None,
            decoder_cls=decoder_cls,
            segmenter=args.segmenter,
            known_calls=known_calls,
            call_patterns=call_patterns,
            known_calls_weak=known_weak,
            my_call=args.my_call,
            media_clock_epoch=getattr(args, "media_epoch", None),
        )

    def dsp_worker(u) -> None:
        try:
            for block in u["source"].stream_iq():
                if rec is not None:
                    np.asarray(block, dtype=np.complex64).tofile(rec)
                u["pipeline"].process(block)
        except Exception:
            log.exception("DSP worker %s/%.0f died", u["name"], u["center"])
            failed.append("%s/%.0f" % (u["name"], u["center"]))
        finally:
            def one_down():
                alive[0] -= 1
                if alive[0] <= 0:
                    done.set()
            loop.call_soon_threadsafe(one_down)

    for u in units:
        alive[0] += 1
        u["worker"] = threading.Thread(
            target=dsp_worker, args=(u,),
            name=f"dsp-{u['name']}-{u['center'] // 1000}", daemon=True)
        u["worker"].start()
        log.info("Skimming %s at %.1f kHz, %d kHz passband",
                 u["name"], u["center"] / 1000, u["rate"] // 1000)
    log.info("%d band(s); telnet on port %d", len(units), args.telnet_port)
    try:
        await done.wait()
    finally:
        for u in units:
            u["source"].disconnect()
        await telnet.stop()
        for web in webs:
            await web.stop()
        for u in units:
            u["worker"].join(timeout=3)
        if rec is not None:
            rec.close()
        if chan_log is not None:
            chan_log.close()
        if char_log is not None:
            char_log.close()
    # see _run_processes: a died worker must not look like a clean finish
    if failed:
        log.error("%d of %d band(s) failed; exiting non-zero",
                  len(failed), len(units))
        return 1
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
