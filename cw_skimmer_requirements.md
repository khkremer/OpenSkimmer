# Requirements: an open-source multi-channel CW skimmer

Karl Heinz Kremer, K5KHK, July 13, 2026

> The project started from this document. It's kept for reference, lightly
> edited for publication. Everything in it has been built except the ML
> decoder (phase 5); section 11 says how things turned out. Two receiver
> paths were added later: SoapySDR, which also covers RTL-SDR and Airspy,
> and ka9q-radio `radiod`, which is how the RX-888 is used.

## 1. Purpose

Define requirements for an open source, multi-channel CW (Morse code) decoding and
spotting tool that matches the core functionality of Alex Shovkoplyas's (VE3NEA)
CW Skimmer / SkimSrv, while natively supporting a heterogeneous set of common SDR receivers:
Hermes-Lite 2, KiwiSDR, and SDRplay (RSP series).

Beating CW Skimmer's decoding accuracy on day one is not required. The system should
be open and independent of any one radio, so the decoding engine can be improved
separately from the SDR interface layer and new receivers can be added without a
rewrite.

## 2. Background: the reference system

CW Skimmer (Windows, closed source) sets the functional bar:

| Capability | CW Skimmer behavior |
|---|---|
| Decoding | Simultaneous multi-signal Bayesian CW decoder across the full receiver passband (up to roughly 192 kHz depending on SDR) |
| Display | Waterfall with per-signal call sign overlay |
| Output | Telnet server (DX-cluster-like protocol), RBN upload format |
| Hardware | Works with a fixed list of supported SDRs via vendor-specific plugin DLLs (Hermes/HPSDR, SDR-IQ, Perseus, RSP via SDRplay's own DLL, etc.) |
| Companion | SkimSrv (headless/server variant used for RBN nodes) |

Known open source efforts that partially address this space (fldigi's CW decoder,
ggmorse, AG1LE's deep-learning decoders, DeepCW) are either single-channel, not
hardware-agnostic, or not production-hardened. None combines multi-channel decoding
with a pluggable multi-SDR front end, and this project sets out to do that.

## 3. Scope

### In scope
- Wideband IQ capture from any of the three target SDRs
- Simultaneous decoding of multiple CW signals within the captured passband
- Call sign extraction and spot generation (time, frequency, call, SNR, WPM)
- Local waterfall/spectrum display with spot overlay
- Telnet server output compatible with standard DX-cluster clients (N1MM, etc.)
- Optional RBN-compatible upload
- Pluggable SDR back end (clean abstraction layer)
- Pluggable decoding engine (classic DSP first; ML-based decoder as a later swap-in)
- Linux and macOS (Apple Silicon) as the two required platforms

### Out of scope
- iOS. It isn't a target at any phase: a skimmer runs unattended and feeds a waterfall and a Telnet port, which doesn't suit iOS, and there's no plan to port it there
- RTTY/PSK/other digital mode decoding (CW Skimmer supports some of this; treat as a stretch goal, not v1)
- Full RBN node operational hardening (rate limiting, abuse handling, etc.)
- Mobile/embedded (Pi Zero, ESP32) deployment; may be a future target once the core is stable

### Nice to have
- Windows support. CW Skimmer already covers Windows, so there's no pressure to support it here. If the code turns out portable enough to run there too, good.

## 4. Hardware interface requirements

Each SDR has materially different capabilities and constraints, so this is broken out
per device rather than treated as one generic "SDR input."

### 4.1 Hermes-Lite 2 (HL2)
- Protocol: open HPSDR/Metis-style UDP protocol over Ethernet; fully documented, no vendor SDK dependency
- Bandwidth: up to roughly 384 kHz per ADC; multiple simultaneous "receivers" (DDC channels) supported by firmware
- Requirement: implement a native HPSDR/Metis client (discovery, start/stop stream, IQ sample parsing) rather than depending on existing Windows-only DLLs (e.g. HermesIntf)
- Priority: highest; most open protocol, least risk, best fit for a from-scratch client
- Note: PTT-blanking behavior (don't decode own TX) should be replicated as in existing HermesIntf implementations

### 4.2 KiwiSDR
- Protocol: WebSocket-based audio/IQ streaming (documented, community-maintained client libraries exist, e.g. kiwiclient in Python)
- Bandwidth constraint: each client connection is limited to a narrow slice (typically 20 kHz or less of IQ per channel in standard firmware), and the number of simultaneous connections to a given Kiwi is capped (often 4 to 8 depending on config) and shared with other users if it's not a private/local unit
- Requirement: support connecting multiple simultaneous narrow-slice channels and stitching/tiling them into a wider virtual passband for the decoder, since a single KiwiSDR connection won't offer CW-Skimmer-style full-band coverage
- Requirement: gracefully degrade if fewer channels are available (e.g., shared/public Kiwi); should not assume exclusive access
- Priority: medium; useful for remote/community receiver access, but the multi-connection tiling adds real complexity, so treat as second target after HL2

### 4.3 SDRplay (RSP1A / RSPdx / etc.)
- Protocol: proprietary SDRplay API (closed-source shared library, but with a stable, documented C API and permissively-licensed wrapper bindings in several languages)
- Bandwidth: up to 10 MHz IQ (model-dependent), single local USB device; well suited to genuine full-band, single-hardware-instance skimming
- Requirement: link against the official SDRplay API library (note: introduces a non-fully-open dependency; document this clearly in the architecture as the one "impure" component)
- Priority: high for standalone/full-band local operation, since it's the only device of the three offering wide native IQ bandwidth without protocol workarounds

### 4.4 Common abstraction layer
- Define a single internal interface (e.g. SDRSource) with:
  - connect(), disconnect()
  - set_center_frequency(hz), set_sample_rate(hz)
  - stream_iq() returning an iterator of complex sample blocks
  - capabilities(), reporting max bandwidth, channel count, whether shared/exclusive access
- Each device (HL2, KiwiSDR, SDRplay) implements this interface as a separate backend module
- Decoding engine must never call device-specific code directly

## 5. Functional requirements: decoding and spotting

| ID | Requirement |
|---|---|
| F1 | Decode multiple simultaneous CW signals within the captured passband (target: comparable density to CW Skimmer, e.g. dozens of channels across 48 to 192 kHz) |
| F2 | Extract and validate call signs from decoded text using standard CW contest/QSO grammar (CQ, DE, callsign patterns, etc.) |
| F3 | Estimate per-signal SNR, frequency, and WPM |
| F4 | Generate a spot record per detected/validated transmission: timestamp, frequency, call sign, SNR, WPM, mode |
| F5 | Provide a waterfall display with spot labels overlaid at the correct frequency, similar to CW Skimmer's UI |
| F6 | Expose spots via a Telnet server using DX-cluster-compatible syntax, so existing logging software (N1MM+, etc.) can connect as if to a normal cluster |
| F7 | Optional: format and upload spots to RBN using their existing submission protocol |
| F8 | Support a self-blanking mode; suppress decoding during local TX (PTT) to avoid self-spotting, matching HermesIntf's existing behavior for HL2 |
| F9 | Decoding engine should be swappable: v1 can use a classical DSP/pattern-matching approach; architecture must allow later substitution of an ML-based decoder (e.g. adapting concepts from AG1LE's deepmorse-decoder or DeepCW) without touching the SDR or spotting layers |

## 6. Non-functional requirements

- Platform: native Linux and macOS (Apple Silicon) support are required; avoid Windows-only dependencies (ruling out reuse of existing HermesIntf.dll, SkimSrv plugins, etc.). Windows support is optional and not a design goal; if the chosen stack happens to run there too, that's a bonus. iOS is explicitly not a target platform.
- Performance: real-time decoding of the full captured passband with latency low enough for practical spotting (target: comparable to CW Skimmer's near-instant call recognition, sub-2-second spot latency)
- Resource usage: should run on a modest always-on Linux server without requiring dedicated GPU hardware for the v1 classical decoder; ML decoder variants may have higher requirements and should be clearly documented
- Extensibility: new SDR backends should be addable by implementing the SDRSource interface only, with no changes to decoder or UI code
- Reliability: long unattended runs. Like CW Skimmer feeding the RBN, this is a tool people leave running around the clock
- Licensing: fully open source core (permissive license, e.g. MIT/BSD/Apache-2.0); SDRplay backend must isolate the closed proprietary API dependency so the rest of the system remains cleanly open

## 7. Proposed architecture

Three SDR backends (HL2 over Metis/UDP, KiwiSDR over WebSocket, SDRplay over its
native API) each implement the common SDRSource interface. Their IQ output feeds a
shared buffering/channelization stage, which hands per-channel signal data to the
pluggable CW decoding engine (classical DSP for v1, ML-based later). Decoded,
validated call signs become spot records, which drive both the waterfall UI and the
Telnet/RBN output layer. The main architectural constraint is that the decoder and spotting logic never
depend on which backend supplied the samples.

## 8. Success criteria: comparison with CW Skimmer

To evaluate whether the tool is a credible alternative, measure against CW Skimmer on:

1. Call sign decode accuracy on identical recorded IQ samples (side-by-side test, same passband, same time window)
2. Number of simultaneous signals decoded correctly in a busy contest-band recording
3. Spot latency from transmission to spot output
4. False positive / false call sign rate
5. Uptime / stability over a multi-day unattended run

For v1 it's enough to be usable, for example within a defined accuracy tolerance of CW
Skimmer on moderate-density bands, while being fully open and running natively on all
three target SDRs.

## 9. Open questions

- KiwiSDR channel-tiling: is exclusive/local access to a KiwiSDR assumed, or should the design assume shared/public access with unpredictable channel availability?
- Should the v1 decoding engine be a fresh implementation, or should it wrap/adapt an existing open source decoder (ggmorse, AG1LE's work) to save development time?
- Is a GUI waterfall required for v1, or is a headless "SkimSrv-equivalent" (Telnet output only) sufficient for the first working version, with UI added later?
- Target language/stack: which implementation language best balances DSP performance with portability needs? Python plus numpy/scipy is fastest to prototype the DSP decoder; a compiled language may be needed later for the multi-channel real-time path.

## 10. Suggested phases

| Phase | Deliverable |
|---|---|
| 1 | HL2 backend plus single/multi-channel classical CW decoder, headless Telnet spot output only |
| 2 | Add waterfall UI |
| 3 | Add SDRplay backend (full-band local capture) |
| 4 | Add KiwiSDR backend with channel tiling |
| 5 | Evaluate ML-based decoder as a drop-in replacement for the DSP decoder |
| 6 | Optional: RBN upload integration, contest-grade hardening |

## 11. Where things ended up

- Phases 1-4 and 6 are done. Spots reach the RBN through the standard
  RBN Aggregator, which connects to the Telnet server (F7).
- SDRplay devices are reached through SoapySDR (`soapysdr-module-sdrplay3`), not
  by linking the SDRplay API directly. The proprietary piece stays outside
  the project.
- The KiwiSDR backend assumes shared access: slices that can't be had are retried,
  and the skimmer runs on whatever it gets.
- The decoder is a new implementation in Python and numpy. The default is
  a Bayesian HMM mark/space detector (fixed-lag Viterbi), which met the CW
  Skimmer comparison in section 8 on a contest recording (see
  docs/cwskimmer-comparison-20260715.md). The classical decoder remains as
  the alternative.
- Phase 5, the ML decoder, was evaluated and dropped. On live signals,
  most decoding errors arose from word segmentation and callsign
  extraction, not from mark/space detection, so a better envelope
  classifier would not have fixed them.
