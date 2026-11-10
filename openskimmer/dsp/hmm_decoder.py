"""Bayesian mark/space detection: fixed-lag Viterbi over a 2-state HMM.

Drop-in alternative to CWDecoder's keying detector (requirement F9 keeps the
decoder swappable); the element/timing/text layers are inherited unchanged.

The classical decoder thresholds each frame independently (with hysteresis
and a debounce). This decoder instead scores both key states per frame with
an explicit likelihood model and tracks the most probable state *sequence*,
committing to each frame's state only after `lag` frames of look-ahead:

  - a state flip must buy back SWITCH_COST in accumulated log-likelihood,
    so isolated noise flares can't key the channel;
  - a QSB sag in the middle of a dah is bridged when staying "down" is
    cheaper than switching out and back in;
  - evidence integrates across frames (matched-filter-like), which helps
    exactly where a hard threshold is noisiest.

Emission model: the per-frame envelope is the summed power of 3 FFT bins of
complex Gaussian noise (+ carrier), i.e. ~Gamma(k=3) with mean `noise` when
the key is up and `signal+noise` when down. Only the log-likelihood ratio
matters, and for Gamma(k) it is k times the exponential-distribution LLR.
"""

from __future__ import annotations

import math
from collections import deque
from typing import Callable

from .decoder import CWDecoder


class HMMDecoder(CWDecoder):
    LAG_S = 0.17        # look-ahead as a DURATION, not a frame count.
                        # Was `LAG = 32` with a comment reading it as
                        # "~170 ms at 187 fps" -- true for every rate this
                        # runs at (Channelizer scales fft_size with the
                        # sample rate, so frame_dt is 5.33 ms from 24 kHz to
                        # 3.072 MHz), but nothing enforced it. Off that grid
                        # the look-ahead silently changed: 8 kHz audio gives
                        # frame_dt 8 ms, so 32 frames = 256 ms, 50 % longer
                        # than tuned, and the debounce drops 2 frames -> 1.
                        # Found porting this decoder to Swift for
                        # an app that feeds it 8 kHz audio.
                        # Deriving from frame_dt is a no-op at every rate
                        # OpenSkimmer currently uses.
    GAMMA_K = 3.0       # envelope = power summed over 3 bins
    LLR_CLAMP = 18.0    # cap per-frame evidence (impulse robustness)
    SWITCH_COST = 5.0   # -log odds of a keying transition per frame
    EDGE_LEAK = 1 / 16  # residual carrier power in the key-up hypothesis:
                        # STFT edge smear leaks carrier into "up" frames, so
                        # pure-noise up-modeling puts the decision boundary
                        # ~22 dB down a strong carrier's smeared edges and
                        # measures marks wide (dit inflated ~25%, word gaps
                        # fused: "TUK1GU"). With up-mean = n + s/16 the LLR
                        # zero lands near -6 dB of the carrier -- the same
                        # unbiased edge point the classical threshold uses --
                        # while weak signals stay noise-limited.

    def __init__(self, frame_dt: float,
                 on_char: Callable[[str], None],
                 on_word_break: Callable[[], None], **kw):
        super().__init__(frame_dt, on_char, on_word_break, **kw)
        self.lag = max(1, round(self.LAG_S / frame_dt))
        self._metric_up = 0.0
        self._metric_dn = -self.SWITCH_COST      # keying starts up
        # Survivor paths by register exchange: bit k of each is the key
        # state k frames ago on the best path ending in that state, so bit
        # `lag` of the winner's path is the fixed-lag decision -- exactly
        # what walking `lag` back-pointers computes, ties included, without
        # the 32-step walk every frame. Bit 0 seeds the state at "frame 0".
        self._path_up = 0
        self._path_dn = 1
        self._path_mask = (1 << (self.lag + 1)) - 1
        self._steps = 0                          # frames seen, for warm-up
        self._lag_env = deque(maxlen=self.lag)   # envelopes awaiting verdict
        self._lag_down = False                   # decided (lagged) key state

    def process(self, envelope: float, noise: float) -> None:
        self._frame_idx += 1
        self._noise = (0.995 * self._noise + 0.005 * noise
                       if self._noise > 1e-29 else noise)
        n = max(self._noise, 1e-30)
        self._on_power *= 0.9995                 # follow QSB downward
        s = max(self._on_power - n, 3.0 * n)     # tracked carrier power

        mu_up = n + self.EDGE_LEAK * s
        mu_dn = s + n
        llr = self.GAMMA_K * (math.log(mu_up / mu_dn)
                              + envelope * (1.0 / mu_up - 1.0 / mu_dn))
        llr = max(-self.LLR_CLAMP, min(self.LLR_CLAMP, llr))

        # Viterbi step (log domain; up emits 0, down emits llr by symmetry)
        up_from_dn = self._metric_dn - self.SWITCH_COST
        dn_from_up = self._metric_up - self.SWITCH_COST
        if self._metric_up >= up_from_dn:
            new_up, prev_up = self._metric_up, False
        else:
            new_up, prev_up = up_from_dn, True
        if self._metric_dn >= dn_from_up:
            new_dn, prev_dn = self._metric_dn, True
        else:
            new_dn, prev_dn = dn_from_up, False
        new_dn += llr
        norm = max(new_up, new_dn)
        self._metric_up = new_up - norm
        self._metric_dn = new_dn - norm

        mask = self._path_mask
        p_up = ((self._path_dn if prev_up else self._path_up) << 1) & mask
        p_dn = (((self._path_dn if prev_dn else self._path_up) << 1) | 1) & mask
        self._path_up, self._path_dn = p_up, p_dn
        self._lag_env.append(envelope)
        self._steps += 1
        if self._steps < self.lag:
            return                               # still warming up
        best = p_dn if self._metric_dn > self._metric_up else p_up
        self._emit(bool(best >> self.lag), self._lag_env[0])

    # -- the decided (lagged) stream feeds the inherited timing layer -------

    def _emit(self, down: bool, envelope: float) -> None:
        if down != self._lag_down:
            dur_ms = self._state_frames * self.frame_dt * 1000.0
            was_down = self._lag_down
            self._lag_down = down
            self.key_down = down
            self._state_frames = 0
            if was_down:
                self._end_mark(dur_ms)
            else:
                self._end_gap(dur_ms)
                self._char_flushed = False
        self._state_frames += 1
        if down:
            self._on_power = (0.9 * self._on_power + 0.1 * envelope
                              if self._on_power else envelope)
        else:
            self._check_space_timeouts()
