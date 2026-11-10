# Contributing to OpenSkimmer

Bug reports, measurements and pull requests are welcome.

## Reporting a problem

Please include:

- the command line or `skimmer.local.conf` (remove any password),
- the receiver and band,
- the log output around the problem,
- for a wrong or missing spot: the time (UTC), frequency and call, and if
  you have it, the `--log-channels` or `--log-chars` lines for that signal.
  With the char log, `tools/replay_chars.py` reproduces the spot exactly,
  which usually makes a busted call easy to explain.

## Development setup

```sh
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests need no hardware and run in a few seconds.

## Changing the spotting rules

Changes to callsign extraction (`openskimmer/spotting/callsign.py`) are
easy to make and hard to judge, because a rule that removes busts usually
also removes real calls. Before proposing one:

1. Record a decode with `--log-chars` (or use an existing IQ recording with
   `--source file --start-time ...`).
2. Replay it before and after the change with `tools/replay_chars.py`.
3. Score both against that day's RBN archive with `tools/spot_confirm.py`
   and `tools/rbn_consensus.py replay`, and report both numbers: busts
   removed and real calls lost.
4. Check the rule on a different day or band than the one it was tuned
   on. Many rules that look good on one recording don't hold up on the
   next.

A rule must never invent a call: every spot must come from text the
decoder actually produced.

## Adding a receiver

Implement `SDRSource` (`openskimmer/sdr/base.py`) in a new module and
register it in `openskimmer/sdr/__init__.py`. Nothing in `dsp/` or
`spotting/` may import from a backend.

## Code style

Plain Python 3.9+, numpy for the vector work, no other heavy dependencies
in the core. Comments should explain *why* a value or rule is what it is,
ideally with the measurement that set it.
