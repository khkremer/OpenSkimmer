"""Spot records and de-duplication (F4)."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple


@dataclass
class Spot:
    freq_hz: float
    call: str
    snr_db: int
    wpm: int
    activity: str = "CQ"          # "CQ", "DE", "DUPE"
    mode: str = "CW"
    time_utc: float = field(default_factory=time.time)


class Spotter:
    """Collects candidate calls and emits de-duplicated spots.

    A (call, ~100 Hz frequency bucket) pair is re-spotted at most once per
    ``respot_interval_s``, tolerating up to ``respot_tol_hz`` of drift
    between sightings (see ``_respot_span`` below for why that needs to be
    wider than one bucket). The interval default matches the RBN's own dedup
    window: re-spotting faster than that does not add information, it just
    gets the extra spots flagged as duplicates (tools/spot_dupes.py measures
    the rate).
    """

    def __init__(self, on_spot: Callable[[Spot], None],
                 respot_interval_s: float = 600.0,
                 respot_tol_hz: float = 1000.0,
                 min_snr_db: int = 3,
                 clock: Optional[Callable[[], float]] = None,
                 my_call: Optional[str] = None,
                 runner_memory_s: float = 900.0,
                 runner_tol_hz: float = 500.0,
                 max_wpm: int = 50,
                 blocked: Optional[set] = None,
                 cq_runner_guard: bool = False):
        self.on_spot = on_spot
        # EXPERIMENT: a CQ within runner_tol_hz of a DIFFERENT station we
        # spotted calling CQ within runner_memory_s is reported as DE
        self.cq_runner_guard = cq_runner_guard
        self.respot_interval_s = respot_interval_s
        self.min_snr_db = min_snr_db
        self.max_wpm = max_wpm
        # calls never to spot, whatever the evidence -- see submit()
        self.blocked = {c.upper() for c in (blocked or ())}
        self._clock = clock or time.time
        # (call, 100 Hz bucket) -> (when, whether it went out as CQ)
        self._last: Dict[Tuple[str, int], Tuple[float, bool]] = {}
        # the operator's own call: never spot it (self-spots from local TX
        # or its images would pollute RBN)
        self.my_call = my_call.upper() if my_call else None
        # frequencies where we have heard someone CALL CQ, and when
        self._runners: Dict[int, Tuple[str, float]] = {}
        self.runner_memory_s = runner_memory_s
        self._runner_span = max(1, int(round(runner_tol_hz / 100.0)))
        # A tracked signal's channel is retired after 20 s of no keying
        # (SignalTracker.channel_timeout_s) and recreated fresh the next
        # time it keys up -- ordinary between a real station's own CQ
        # cycles, especially in a crowded pileup where it pauses longer to
        # listen. The new channel is a new CallsignExtractor with no memory
        # of the old spot, and a standard "CQ CQ DE <call> <call> K" already
        # repeats the call twice in one transmission, so it re-spots
        # immediately. In a crowded segment the new channel's peak bin can
        # land several hundred Hz from the old one (measured live 2026-09-18,
        # IZ2EWR: 18086.35/18086.53/18086.70/18086.05 kHz across four
        # channels in two minutes -- up to 650 Hz apart), well past the one
        # spot line's ~100-200 Hz tolerance. Widening it costs nothing: two
        # DIFFERENT real stations never share a callsign, so a wider match on
        # the SAME call can only suppress a genuine re-spot of one station,
        # never conflate two.
        self._respot_span = max(1, int(round(respot_tol_hz / 100.0)))

    def _runner_near(self, bucket: int, call: str, now: float) -> bool:
        """Is a DIFFERENT station known to be running on this frequency?"""
        for b in range(bucket - self._runner_span, bucket + self._runner_span + 1):
            r = self._runners.get(b)
            if r is not None and r[0] != call and now - r[1] < self.runner_memory_s:
                return True
        return False

    def submit(self, spot: Spot) -> bool:
        if self.my_call and spot.call.upper() == self.my_call:
            return False
        if spot.snr_db < self.min_snr_db or spot.wpm <= 0:
            return False
        # An implausible speed means the decoder's timing estimate has railed,
        # not that someone is sending that fast. `dit_ms` is clamped to
        # [15, 400] ms, and 15 ms IS 80 wpm -- so a "80 wpm" spot is the
        # estimate pinned against its floor, which is what a steady carrier or
        # a noise source does to it. Measured over 17925 live spots: 97.2 %
        # were at or below 40 wpm and only 0.57 % above 50, while 29 of our 37
        # bogus E4* spots were above it. A real 50+ wpm operator is rare; a
        # railed estimate is not.
        if self.max_wpm and spot.wpm > self.max_wpm:
            return False
        # Calls known to be decoder artifacts rather than stations. E4IK was
        # spotted 27 times over 8 days, always on 10100.8 kHz and almost always
        # at the 80 wpm rail, and E4 is Palestine -- so every one of those sent
        # DXers chasing an all-time-new-one after a noise source. Blocking is a
        # blunt instrument and costs a real station if one ever holds the call,
        # which is why it is an explicit operator choice, not a built-in list.
        if spot.call.upper() in self.blocked:
            return False
        now = self._clock()
        bucket = int(round(spot.freq_hz / 100.0))
        # A DE spot does not hold back a CQ of the same call: the Aggregator
        # holds DE, so a runner first copied as "DE W4CMG" and then as
        # "CQ W4CMG CWT" would otherwise never reach the RBN (CWT 2026-10-21)
        cq = spot.activity == "CQ"
        for b in range(bucket - self._respot_span, bucket + self._respot_span + 1):
            last = self._last.get((spot.call, b))
            if (last is not None and now - last[0] < self.respot_interval_s
                    and (last[1] or not cq)):
                return False

        # Is a repetition-only sighting a station CALLING or ANSWERING?
        #
        # "DUPE" means the call repeated with no CQ/DE grammar around it. That
        # is both a CQ caller whose "CQ" the decoder missed AND an S&P station
        # sending its call twice while answering someone -- and the RBN only
        # wants the first. Suppressing all of them (2026-08-06 13:10Z) cost
        # more than it saved off-contest: over that day's two halves the
        # confirmed-spot rate fell 53% while the unconfirmed rate fell only
        # 30%, i.e. proportionally more good spots went than bad ones.
        #
        # The distinction is not repetition, it is whether someone else is
        # already running on that frequency. If we have heard a DIFFERENT
        # station call CQ within `runner_tol_hz` recently, a repeat here is
        # almost certainly one of its callers; otherwise it is most likely a
        # CQ we failed to copy. Leaving it as DUPE makes format_spot report
        # DE, which the RBN Aggregator holds.
        if spot.activity == "DUPE":
            if not self._runner_near(bucket, spot.call, now):
                spot.activity = "CQ"
        elif spot.activity == "CQ":
            if self.cq_runner_guard and self._runner_near(bucket, spot.call, now):
                spot.activity = "DE"
                self._sent(spot, bucket, now)
                return True
            self._runners[bucket] = (spot.call, now)
            if len(self._runners) > 20000:
                cutoff = now - self.runner_memory_s
                self._runners = {k: v for k, v in self._runners.items()
                                 if v[1] >= cutoff}
        self._sent(spot, bucket, now)
        return True

    def _sent(self, spot: Spot, bucket: int, now: float) -> None:
        # recorded with the label it LEFT with: a DUPE that went out as CQ
        # holds back later CQs like any other CQ
        self._last[(spot.call, bucket)] = (now, spot.activity == "CQ")
        if len(self._last) > 20000:  # bound memory over multi-day runs
            cutoff = now - self.respot_interval_s
            self._last = {k: v for k, v in self._last.items() if v[0] >= cutoff}
        self.on_spot(spot)
