# Changelog

## 0.1.0 (2026-11-10)

First public release.

- Receivers: Hermes-Lite 2, KiwiSDR, SoapySDR (SDRplay, RTL-SDR, Airspy,
  Mirics), ka9q-radio `radiod`, IQ files, synthetic signals.
- HMM (fixed-lag Viterbi) decoder by default; classical threshold decoder
  as an alternative.
- Callsign extraction with CQ/DE grammar, clean-copy counts, and the
  MASTER.SCP and RBN pattern databases.
- CW Skimmer-compatible Telnet spot server, verified with the RBN
  Aggregator.
- Multi-band operation with one process per band behind one Telnet port.
- Waterfall web UI.
- RBN-archive spot-quality and replay tools.
