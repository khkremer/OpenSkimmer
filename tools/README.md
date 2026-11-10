# Tools

Stand-alone scripts that support OpenSkimmer. Each script's docstring and
`--help` explain its use in detail. Run them with the project venv, for
example `.venv/bin/python tools/spot_confirm.py --help`.

## Spot quality (against the RBN daily archive)

The RBN publishes every spot from every skimmer each day at
`https://data.reversebeacon.net/rbn_history/YYYYMMDD.zip`.

| Script | What it does |
|---|---|
| `spot_confirm.py` | Lists your spots that no other skimmer confirmed: the candidates for busted calls. |
| `rbn_consensus.py` | Caller-as-runner rate (K1MK's off-consensus measure) for every skimmer, your rank, and scoring of a replay. |
| `bust_context.py` | Shows what the decoder heard around each unconfirmed spot, from a word log. |
| `spot_dupes.py` | Explains RBN-style duplicate spots in a spot log. |
| `skimmer_telnet_log.py` | Logs another skimmer's Telnet spots (e.g. CW Skimmer) for side-by-side grading. |

## Replay and diagnosis

| Script | What it does |
|---|---|
| `replay_words.py` | Re-runs callsign extraction and spotting on a `--log-channels` word log, in seconds. |
| `replay_chars.py` | The same from a `--log-chars` character log; reproduces the live spots exactly. |
| `char_context.py` | Prints the characters and gaps around each copy of a call in a character log. |
| `extract_envelope.py` | Extracts per-channel decoder envelopes from an IQ recording into JSON cases. |
| `gap_trace.py` | Shows every gap the decoder measured in those cases, and where it broke words. |

## Recording and receivers

| Script | What it does |
|---|---|
| `record_iq.py` | Records raw IQ to a `.c64` file without running the DSP. |
| `iq_to_wav.py` | Converts a `.c64` recording to an I/Q WAV that CW Skimmer can open. |
| `measure_ppm.py` | Measures a SoapySDR device's frequency error against a known carrier (e.g. WWV). |

## ka9q-radio / multicast

| Script | What it does |
|---|---|
| `rtp_tap.py` | Sniffs a `radiod` RTP multicast stream and dumps its structure. |
| `rtp_audio.py` | Plays a `radiod` RTP audio channel to a sound device (e.g. for WSJT-X). |
| `rtp_iq_audio.py` | Demodulates a slice of an RTP IQ channel to audio. |
| `rtp_diag.py` | Answers "are the packets actually being sent?" |
| `site_diag.py` | Collects everything needed to diagnose packet loss into one file. |
| `watch_sndbuf.py` | Captures host state while multicast packets are being dropped. |
| `loss_episodes.py` | Summarises "RTP packet(s) lost" log warnings into timed episodes. |
| `tcp_tap.py` | Logging TCP proxy, e.g. between the RBN Aggregator and a skimmer. |
