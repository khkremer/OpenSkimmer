# OpenSkimmer

OpenSkimmer is an open-source CW (Morse) skimmer for Linux and macOS. It
decodes every CW signal in a wideband IQ passband at once, picks out the
callsigns, checks them, and serves spots on a DX-cluster Telnet port. It does
the job CW Skimmer and SkimSrv do on Windows, and its Telnet output uses CW
Skimmer's format, so N1MM+, cluster clients and the Reverse Beacon Network
(RBN) Aggregator accept it unchanged.

It works with a Hermes-Lite 2, a KiwiSDR, any SoapySDR device (SDRplay,
RTL-SDR, Airspy, Mirics clones) and ka9q-radio's `radiod`, which is how it
uses an RX-888. It can also decode IQ recordings and generate synthetic
signals for testing. Each signal gets its own decoder; the default is a
hidden Markov model (fixed-lag Viterbi) that decides when the key is down,
and a classical threshold decoder is the alternative. A call is spotted
only when it follows a CQ or DE and has been copied cleanly enough times,
with the Super Check Partial and RBN callsign-pattern databases deciding how
many copies are enough. One process can skim several bands from different
receivers behind a single Telnet port, with a web waterfall for each band.

For a non-technical tour of how it works, see
[docs/how-openskimmer-works.md](docs/how-openskimmer-works.md). The
requirements the project started from are in
[cw_skimmer_requirements.md](cw_skimmer_requirements.md).

## Status

OpenSkimmer runs around the clock and feeds the RBN on several bands. 0.1.0
is the first public release.

| Piece | State |
|---|---|
| Hermes-Lite 2 backend (HPSDR protocol 1, discovery, PTT blanking, N2ADR filter control) | tested on hardware (gateware v73), including blanking during on-air transmissions. With the filter board in use (the default), the stock `--lna-gain 19` is usually already limited by external noise; raise it only if the ADC overload LEDs stay dark |
| KiwiSDR backend (WebSocket IQ slices tiled into one passband) | tested against a real KiwiSDR |
| SoapySDR backend (SDRplay, Mirics clones, RTL-SDR, Airspy) | tested on an RSP1 (Mirics clone), with frequencies checked against the RBN |
| ka9q-radio backend (RTP multicast IQ from `radiod`) | running live with RTL-SDR and RX-888 mk2 front ends |
| Synthetic and IQ-file backends | for testing and offline decoding |
| HMM decoder | the default, about 3 dB more sensitive than the classical decoder. On a 10-minute CWT recording it found 111 unique calls, where the classical decoder and CW Skimmer each found 108 on the same IQ |
| Classical decoder (`--decoder classical`) | adaptive threshold; the alternative |
| Callsign extraction | CQ/DE grammar, clean-copy count, callsign databases |
| Telnet spot server (CW Skimmer format: banner, `sh/dx`, `SETT`) | tested with the RBN Aggregator |
| RBN upload | live, through the standard RBN Aggregator (see [docs/aggregator-on-linux.md](docs/aggregator-on-linux.md)) |
| Waterfall web UI | done |

## Install

You need Python 3.9 or newer. The core depends only on numpy, aiohttp and
websocket-client:

```sh
git clone https://github.com/khkremer/OpenSkimmer.git ~/openskimmer
cd ~/openskimmer
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest            # optional self-test, a few seconds
```

SoapySDR devices also need the system SoapySDR Python bindings and a driver
module, and ka9q-radio needs a running `radiod`.
[INSTALL-linux.md](INSTALL-linux.md) covers those, running as a systemd
service, and USB dongles that the kernel claims first. On macOS,
[deploy/org.openskimmer.skimmer.plist](deploy/org.openskimmer.skimmer.plist)
runs it under launchd.

### Callsign databases (recommended)

These files aren't included. Put them in the project directory and
`start_skimmer.sh` uses them automatically:

```sh
curl -sLo MASTER.SCP  https://www.supercheckpartial.com/MASTER.SCP
curl -sLo patt3ch.lst https://data.reversebeacon.net/downloads/patt3ch/patt3ch.lst
```

OpenSkimmer runs without them but needs more copies of a call before it
spots it. "How a call gets spotted" below has the details.

## Quick start

To try it without a radio, use synthetic signals:

```sh
.venv/bin/openskimmer --source synth --sample-rate 48000 --my-call YOURCALL
```

The waterfall is at http://localhost:7380, and `telnet localhost 7300` shows
the spots.

For a real station, copy the example configuration and edit it:

```sh
cp skimmer.local.conf.example skimmer.local.conf
$EDITOR skimmer.local.conf      # your call, receiver(s), bands
./start_skimmer.sh
```

`start_skimmer.sh` sources `skimmer.local.conf` as a shell fragment. The
file is gitignored because it can hold a KiwiSDR password. All the settings
are listed at the top of [start_skimmer.sh](start_skimmer.sh). Extra
arguments to `start_skimmer.sh` are passed to `openskimmer`, and
`openskimmer --help` lists them all.

## Running against each receiver

A Hermes-Lite 2 is found by broadcast, or you can give `--host`:

```sh
openskimmer --source hl2 --freq 14030000 --sample-rate 192000 --my-call YOURCALL
```

A KiwiSDR can be local or remote. OpenSkimmer opens one WebSocket connection
per 12 kHz slice, and `--sample-rate` is the total span, rounded to whole
slices up to 96 kHz (8 slices). On a shared or public Kiwi with fewer free
channels, it keeps running on whatever slices it gets:

```sh
openskimmer --source kiwi --host kiwisdr.local --freq 14030000 \
    --sample-rate 36000 --my-call YOURCALL
```

A Kiwi's `chan_no_pwd` setting limits connections without a password, often
to 2. Pass `--kiwi-password` to use more of its channels.

Any SoapySDR device works too: SDRplay RSP, Mirics clone, RTL-SDR and so on.
The `SoapySDR` Python module and the device's driver module must be
installed, and SDRplay devices also need the proprietary `sdrplay_api`
service running. `--gain` sets a manual gain and turns off AGC; leave it out
for AGC. `--ppm` corrects the tuner's clock error, which you can measure with
`tools/measure_ppm.py` against a WWV carrier; the TCXO in a cheap RSP or
Mirics clone can be tens of ppm off. The antenna bias-T (DC power on the
antenna input of the RSP1A, RSP2, RSPdx and RSPduo, but not the RSP1) is
switched off on connect, and `--bias-t` turns it on. To pick a specific
device, use `source@serial` or `source@key=val,...`:

```sh
openskimmer --source sdrplay --freq 14030000 --sample-rate 192000 \
    --my-call YOURCALL
# multiple devices, one per band (genuine units have unique serials):
openskimmer --band sdrplay@1234ABCD:14030000/192000 \
    --band sdrplay@5678EF01:7030000/192000 --my-call YOURCALL
# RTL-SDR (note the sample rate -- see caveats below):
openskimmer --source rtlsdr --freq 14030000 --sample-rate 250000 \
    --my-call YOURCALL
```

An RTL-SDR goes through the same backend, with two quirks. Its sample rate
must be between about 250 kHz and 3.2 MHz; the driver clamps 192 kHz, so
pass something like `--sample-rate 250000`. And a plain R820T2 dongle only
tunes down to about 24 MHz, so HF needs an RTL-SDR Blog V3 or V4, or an
upconverter. Its 8-bit ADC has much less dynamic range than an RSP or HL2,
which makes it fine as a spare or for an extra band but weak on a crowded
band with strong signals. On Linux, blacklist the kernel DVB driver
(`dvb_usb_rtl28xxu`) so SoapySDR can claim the dongle; INSTALL-linux.md
explains how.

With ka9q-radio, `radiod` does the wideband DSP for an RX-888 or any other
front end it supports and multicasts each channel as RTP IQ. OpenSkimmer
joins the multicast group and picks the channel by its SSRC, which `radiod`
sets to the channel centre in kHz, so OpenSkimmer derives it from `--freq`.
Point `--band ka9q@GROUP:center/span` at the channel's data multicast group,
either an mDNS name like `skimmer-iq.local` or a `239.x.x.x` address. The
span must match the channel's sample rate in `radiod`. There is an example
`radiod` config in `docs/radiod@skimmer-rtlsdr.conf`, and
`tools/rtp_tap.py` shows what is in a stream:

```sh
openskimmer --band ka9q@skimmer-iq.local:28050000/192000 --my-call YOURCALL
```

Synthetic signals need no hardware:

```sh
openskimmer --source synth --sample-rate 48000 --my-call YOURCALL
```

An IQ recording can be a stereo I/Q `.wav`, raw complex64 `.c64`, or `.s16`:

```sh
openskimmer --source file --file band.wav --freq 14030000
```

To skim several bands at once, repeat `--band
[source[@host]:]center[/span]`. The bands can use different receivers. They
share one spot server and one SETT segment list, and each band gets its own
waterfall on `--web-port`, `--web-port`+1 and so on:

```sh
openskimmer --band hl2@192.0.2.10:14030000/192000 \
    --band kiwi@kiwisdr.local:7030000/24000 --kiwi-password ...
```

Each band after the first runs in its own process. The per-signal decoders
are pure Python, so bands running as threads would take turns on the GIL and
all of them together would get one CPU core however many the machine has.
As processes, the bands can use separate cores, and the skimmer still has one
Telnet port, one Aggregator connection and one channel log. `--workers
thread` runs everything in one process, and `--workers process` starts a
child process even for a single band. The default, `auto`, uses a thread
when there's only one band, so replays and `--record` run in one process.

Several `hl2` bands on the same radio share one session, using the HL2's
multiple receivers (up to 4, and all of them must use the same span):

```sh
openskimmer --band hl2@192.0.2.10:14030000/192000 \
    --band hl2@192.0.2.10:7030000/192000
```

The N2ADR filters are chosen from the set of receivers: the low-pass filter
for the highest band, and the broadcast-reject high-pass filter unless 160 m
is in use.

Any live run can save its raw IQ with `--record band.c64` for later replay.
A replay decodes exactly as the live run did, so recordings work as test
material for tuning the decoder.

## How a call gets spotted

A call is spotted only after a CQ or DE, and only after enough clean copies.
Repeating a call without a CQ or DE never gets it spotted.

### Context

A `CQ` or `TEST` (or `DE`) has to be among the last few words before the
call. A CQ counts only for the first call after it. When another call comes
between them, the second call is a caller answering the runner. In a
zero-beat pileup the runner and its callers share one decoder, so in
"CQ CWT N4BP N1EN BOB 2463" N4BP gets the CQ and N1EN gets nothing.

A TEST straight after the runner's own call ends its CQ
("CWT NA2U NA2U TEST N5ER"), so the call after it is a caller too. The same
applies after a runner's call with its CQ words run onto the front of it
("CQ CWTDL8LAS K0AF"). On a signal already spotted as another station, a
new call needs its CQ right in front of it, with only CQ, CWT, TEST or DE
words in between. A new call there is far more often a caller following a
runner's call the decoder garbled ("TEST WA3 TT KB9S" for WA3AAN) than a new
runner.

`DE <call>` after a CQ counts as a CQ. After another call
("N8JMS DE WA3TVH") it's an answer and is reported as DE, which the RBN
Aggregator holds back. A call is spotted again near the same frequency at
most once every 10 minutes, but a DE spot never holds back a later CQ spot
for the same call ("QRL? DE W4CMG", then "CQ W4CMG CWT").

`POTA`, `SOTA` and `WWFF` count like `CQ`, so "POTA KT5E" is a CQ even when
the CQ before it was lost. Words the decoder ran together still count:
`CQCQDE` and `CQCQPOTADE` are CQ context, and so is `CQCQPOTADENZ1J` for the
clean `NZ1J` that follows it. So are words split in the wrong places:
"CQP OTADE KS4CR" is joined back into `CQPOTADE` (CQ POTA DE), but only when
the call itself was decoded as one clean word. Rejoined text only provides
context; no call is ever built from it.

### Copies

A copy is the call as a word of its own, exactly as decoded. A candidate
that was repaired, merged or stripped never counts as an independent copy.
The exception is a call with only prosigns run onto its front
(`CQCQDEN8TGQ`, `DEWC5WC`), because nothing of the call itself was rebuilt.

Every call needs at least two copies, whether it's in a database or not.
UberSDR's skimmer asks for the same on the same audio. A runner's garbled
keying often produces real, listed calls (`K4TZ` for `TK4TZ`). A
three-character call, or a four-character one that MASTER.SCP doesn't list,
needs a third copy. That is what a call looks like after losing its first or
last letter (`N8A` for `N8AI`), and half of those spots were busts.

### Garbles of the runner

Once a signal is established on a call (spotted, or copied cleanly twice),
a new call within two dits or dahs of it is taken as a garble unless it has
as many copies. Examples are `EA5T` after `EA5M`, and `K0STE`, which has the
same marks as `K0SN` split into different letters. This applies even to
calls in MASTER.SCP.

### Callsign databases

How many copies a call needs depends on the callsign databases, a scheme
taken from CW Skimmer:

1. Structure: the token must have a legal callsign shape.
2. `--patterns patt3ch.lst`, the RBN's database of callsign patterns
   (data.reversebeacon.net/downloads/patt3ch/). A call whose pattern (such
   as `K3@@@`) is listed spots on its second clean copy. A call with an
   unlisted pattern, a shape no real prefix produces (`T0FT`, `LK0RVV`),
   needs a third.
3. `--scp MASTER.SCP`, the Super Check Partial list of known calls
   (supercheckpartial.com). A listed call also spots on its second clean
   copy and wins over garbled variants of itself. An unlisted call that is a
   listed one with a letter in front (`NN1RR` next to `N1RR`) needs a third
   copy, because a CQ fused onto a call looks the same (`CW4MQC/1`). Any file
   with one call per line works, so you can use a fuller list, for example
   one built from FCC data.

`--scp-weak combined.txt` adds a second, broader list. It can only veto a
garble that overlaps a listed call seen on the same signal; it never vouches
for a call. A loose list does the first job well and the second badly:
calls listed only in combined.txt turned up about twice as often among spots
that no other skimmer confirmed.

`start_skimmer.sh` uses these files automatically when they are in the
project directory: MASTER.SCP as `--scp` with `combined.txt` (built below) as
`--scp-weak`, or `combined.txt` as `--scp` if there's no MASTER.SCP.

To cover more than contesters, merge in other call lists, such as the POTA
activator roster. The pota.app activators page pages through the list in the
browser, but the API behind it returns all of it (about 29,000 calls) in one
request:

```sh
curl https://api.pota.app/activator/all -o activators.json
python3 -c "import json; print('\n'.join(sorted(a['activeCallsign'] \
    for a in json.load(open('activators.json')))))" > pota_activators.txt
# tr strips MASTER.SCP's CRLF endings so sort -u can deduplicate properly
cat MASTER.SCP pota_activators.txt | tr -d '\r' | sort -u > combined.txt
./start_skimmer.sh --scp combined.txt    # overrides the auto MASTER.SCP
```

The POTA data has the occasional malformed entry, such as "05W7RTA" for
W7RTA. These do no harm, because the database is only consulted for
candidates that already have a valid callsign shape. The JSON also has
statistics for each activator (activations, parks, QSOs by mode) if you
want more than the call list.

## Logs, waterfall and Telnet

`--log-channels words.log` appends every decoded word as
`UTC time, freq kHz, WPM, SNR, word`. The web UI only shows the last few
words per signal; this log keeps everything each decoder copied:

```
2026-07-14 15:32:10Z  14044.58  24  12  BKDEWB7BNE
```

While OpenSkimmer runs, the waterfall is at http://localhost:7380
(`--web-port`, 0 turns it off). It shows a scrolling spectrum with spot
labels at their frequencies, a spot log, and a live table of every active
decoder with its frequency, WPM, SNR and recent text. That table is the best
way to see how the decoder is doing on real signals.

Spots are printed to stdout and served on Telnet port 7300
(`--telnet-port`):

```
DX de YOURCALL-#:  14031.0  W1AW           23 dB  25 WPM  CQ            1516Z
```

Point N1MM+ or any cluster client at `host:7300`. The connect banner shows
the operator's call, name and grid (`--op-name`, `--grid`). Clients are asked
for their call when they connect, unless `--telnet-open` is given. Commands
are answered even at the login prompt, which the RBN Aggregator needs: as a
primary skimmer it wants a SETT response before it forwards anything.
`sh/dx [n] [mode]` replays recent spots (for example `sh/dx 25 cw`), `SETT`
reports the skimmed segments (the slices currently covered) the way CW
Skimmer does, and `bye` disconnects.

## Checking spot quality

The RBN publishes every spot from every skimmer as a daily archive
(`https://data.reversebeacon.net/rbn_history/YYYYMMDD.zip`), and these tools
check against it:

- `tools/spot_confirm.py DAY.zip --call 'YOURCALL*'` lists your spots that
  no other skimmer confirmed. Those are the candidates for busted calls.
- `tools/rbn_consensus.py field DAY.zip --call YOURCALL` gives every
  skimmer's rate of reporting callers as runners, using K1MK's off-consensus
  measure (cells of 0.1 kHz by 5 minutes, decided by at least 4 skimmers).
  It reports the median across all skimmers and your rank, by band and by
  hour.
- `tools/rbn_consensus.py replay DAY.zip spots.txt ...` scores the `DX de`
  lines from a replay the same way, against the other skimmers' spots only.

To compare changes to the spotting rules on identical decoder output, decode
a recording once with its real start time and a word log, then replay the
words. The replay takes seconds instead of minutes:

```sh
openskimmer --source file --file cwt.c64 --freq 14030000 \
    --start-time 2026-10-15T18:55:00Z --log-channels words.log --my-call YOURCALL
tools/replay_words.py words.log --scp MASTER.SCP --scp-weak combined.txt \
    --patterns patt3ch.lst > spots.txt
tools/rbn_consensus.py replay 20261015.zip spots.txt
```

`--log-chars chars.log` logs every character instead, with the gap before
it, the current dit length and word threshold, and every word break. Its
replay tool, `tools/replay_chars.py`, reproduces the decode's spots exactly,
which the word log can't (it keeps whole seconds and drops words longer than
24 characters). It also takes `--segmenter` to try a different way of
cutting words on the same characters. `tools/char_context.py chars.log CALL
[KHZ]` prints the characters around each copy of a call with their gaps, so
you can see where the sender's word gaps really were.

`--segmenter widest` (the default is `timing`, the decoder's own breaks)
also cuts a word the decoder kept whole at a gap clearly wider than every
other gap in it, such as "NN1RRTEST" with a 4.5-dit gap among 3-dit ones. A
word made by such a cut never counts as a copy of a call. It can provide
context, and it can confirm a call already copied cleanly on that signal.

`tools/gap_trace.py cases.json --match CALL` shows how the decoder broke
words in a case written by `extract_envelope.py`. It prints every gap
between characters in dits and marks each place it ended a word, which shows
why a call came out fused to the words around it.

## Architecture

```
sdr/  (one module per receiver; only these know about hardware)
  base.py           SDRSource interface + Capabilities
  hl2.py            Hermes-Lite 2: HPSDR/Metis UDP client
  kiwi.py           KiwiSDR: one WebSocket IQ connection per 12 kHz slice
  kiwi_protocol.py  KiwiSDR wire-format helpers
  stitcher.py       tiles slices into one wide virtual passband
  soapy.py          SoapySDR devices (SDRplay, RTL-SDR, Airspy, Mirics)
  ka9q.py           ka9q-radio radiod RTP multicast IQ
  synth.py          synthetic multi-signal CW generator
  fileiq.py         IQ recording playback
        │  complex64 IQ blocks
        ▼
pipeline.py (hardware-agnostic; sees only samples + sample rate)
  dsp/channelizer.py  STFT: ~47 Hz bins, ~5 ms frames
  dsp/tracker.py      median-baseline noise floor, peak detection,
                      one Channel (+ decoder) per CW signal
  dsp/hmm_decoder.py  fixed-lag Viterbi mark/space detector (default)
  dsp/decoder.py      classical adaptive threshold; dit clustering and
                      Morse element → text, shared by both decoders
  dsp/morse.py        Morse table
        │  chars / word breaks per signal
        ▼
spotting/
  segmenter.py  word cutting before extraction (--segmenter)
  callsign.py   CQ/DE grammar, copy count, call validation
  spotter.py    Spot records, de-duplication
  telnet.py     CW Skimmer-compatible Telnet server
web/
  server.py    waterfall UI: aiohttp + WebSocket (consumes pipeline taps)
  static/      self-contained single-page frontend
main.py        command line, multi-band workers, logs
```

The decoder never calls device code. A new receiver implements `SDRSource`
and is registered in `openskimmer/sdr/__init__.py`, and nothing else has to
change.

## Tests

```sh
.venv/bin/pytest
```

The tests include an end-to-end run that synthesizes a passband with
several signals and checks that the right calls are spotted at the right
frequencies. A harder 192 kHz scenario with 13 signals, down to 6 dB SNR and
up to 34 WPM, decodes all 13 calls with no false ones, at about 44 times real
time on an M-series Mac.

## Feeding the Reverse Beacon Network

The RBN takes spots through its Aggregator program, which connects to a
skimmer's Telnet port like any cluster client. Point the Aggregator at
`host:7300` as its primary skimmer. The Aggregator is a Windows program;
[docs/aggregator-on-linux.md](docs/aggregator-on-linux.md) shows how to run
it under Wine on the same Linux machine.

Check a skimmer before you let it feed the RBN: run `tools/spot_confirm.py`
on a day of your spots against the RBN archive first (see "Checking spot
quality").

## Contributing

Bug reports and pull requests are welcome; see
[CONTRIBUTING.md](CONTRIBUTING.md).

## License

MIT, see [LICENSE](LICENSE).
