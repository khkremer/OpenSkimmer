# Installing OpenSkimmer on Linux

These steps were tested on Debian and Ubuntu-family systems such as Linux
Mint, with Python 3.9 or newer. OpenSkimmer is pure Python and numpy, so
nothing needs compiling, and the KiwiSDR source needs no radio drivers.

## 1. Prerequisites

```sh
sudo apt update
sudo apt install git python3-venv python3-pip
python3 --version        # needs 3.9+
```

## 2. Get the code and install

Clone to the folder where it will stay. The venv contains absolute paths
and breaks if the folder moves; the symptom is
`exec: .venv/bin/openskimmer: not found` although the file exists. If you
do move it, delete `.venv` and repeat the install in the new place.

```sh
git clone https://github.com/khkremer/OpenSkimmer.git ~/openskimmer
cd ~/openskimmer
python3 -m venv .venv
.venv/bin/pip install --upgrade pip   # older pips can't editable-install
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest          # optional: full self-test, a few seconds
```

For a machine without git access, build a ZIP on a machine that has the
checkout and copy it over:

```sh
./deploy/make-deploy-zip.sh --no-local   # -> openskimmer-YYYYMMDD.zip
unzip openskimmer-*.zip -d ~             # on the target, then the venv steps above
```

Without `--no-local`, the ZIP also includes your callsign databases and
`skimmer.local.conf`, which can hold a KiwiSDR password. Copy a ZIP like
that privately (scp or USB) and don't post it anywhere.

## 3. Configure

```sh
cp skimmer.local.conf.example skimmer.local.conf
```

Edit it to set your callsign, your receivers and the bands. The top of
`start_skimmer.sh` lists every setting. Then fetch the callsign databases
(section 8); `start_skimmer.sh` finds them on its own.

## 4. Run

```sh
./start_skimmer.sh                       # extra args go to openskimmer
```

The waterfall is at `http://<linux-host>:7380`, with one port per band
(7380, 7381 and so on). Spots for N1MM+, the RBN Aggregator or plain telnet
are on `<linux-host>:7300`.

If a firewall is running, open the ports with
`sudo ufw allow 7300/tcp 7380/tcp`.

## 5. Run as a service

For a skimmer that runs all the time, use the systemd unit in
`deploy/openskimmer.service`. Edit it first: set `User=` to your user name
(`whoami`) and point both paths at your checkout. A wrong user fails with
`217/USER` and a wrong path with `203/EXEC`. Then:

```sh
sudo cp deploy/openskimmer.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now openskimmer
journalctl -u openskimmer -f             # watch spots / logs
```

systemd restarts the service if it fails, and the KiwiSDR source retries
dead slices every 30 s by itself. If you turn on `LOG_CHANNELS` or
`LOG_CHARS`, install `deploy/openskimmer-logrotate.conf` too, because both
logs grow without limit.

## 6. Updating

```sh
cd ~/openskimmer
git pull && sudo systemctl restart openskimmer
```

`git pull` leaves your local files (`skimmer.local.conf`, the databases,
`.venv`) alone, since they aren't tracked, and the editable install picks up
the new code without reinstalling. Re-run `.venv/bin/pip install -e ".[dev]"`
only when `pyproject.toml` has changed.

## 6a. SoapySDR devices (SDRplay, RTL-SDR, Airspy) in the venv

SoapySDR's Python bindings only come as a compiled system package, and a
normal venv can't see system packages. So `--source sdrplay` fails with
"No module named 'SoapySDR'" even when `SoapySDRUtil` works in the shell.
Install the bindings, then make them visible to the venv:

```sh
sudo apt install -y python3-soapysdr        # + the device's driver module
# and the SDRplay API service for SDRplay (sdrplay.com installer)

# expose to the venv, either:
python3 -m venv --system-site-packages .venv   # recreate (then reinstall), or
# symlink into the existing venv:
SP=$(.venv/bin/python -c "import site; print(site.getsitepackages()[0])")
ln -sf /usr/lib/python3/dist-packages/SoapySDR.py "$SP"/
ln -sf /usr/lib/python3/dist-packages/_SoapySDR*.so "$SP"/

.venv/bin/python -c "import SoapySDR; print(SoapySDR.getAPIVersion())"  # verify
```

## 6b. USB SDRs held by a kernel DVB driver (Mirics clones, RTL-SDR)

If a USB dongle shows up in `SoapySDRUtil --find` but fails to open
(`usb_claim_interface error -6` or "Failed to open ... device"), the
kernel's DVB tuner driver has it. Blacklist that driver so SDR software can
claim the device.

For Mirics/MSi2500 clones (`driver=miri`):

```sh
sudo tee /etc/modprobe.d/blacklist-mirics.conf >/dev/null <<'BL'
blacklist msi2500
blacklist msi001
BL
sudo modprobe -r msi2500 msi001    # or replug the dongle / reboot
```

For RTL-SDR dongles (`driver=rtlsdr`, which needs `soapysdr-module-rtlsdr`):

```sh
sudo apt install -y soapysdr-module-rtlsdr rtl-sdr
echo 'blacklist dvb_usb_rtl28xxu' | sudo tee /etc/modprobe.d/blacklist-rtl.conf
sudo modprobe -r dvb_usb_rtl28xxu  # or replug the dongle / reboot
```

An RTL-SDR has some limits for skimming. Its sample rate must be between
about 250 kHz and 3.2 MHz, so use `--sample-rate 250000` (the driver clamps
192 kHz). A plain R820T2 dongle only tunes down to about 24 MHz, so HF needs
an RTL-SDR Blog V3 or V4, or an upconverter. Its 8-bit ADC has limited
dynamic range, which is marginal on a crowded band. Its clock is loose:
measure it with `tools/measure_ppm.py 10000000 rtlsdr` and pass the result
with `--ppm`.

The official `sdrplay_api` service (`driver=sdrplay`) doesn't have this
problem. Only the open-source `miri` driver competes with the kernel DVB
modules.

If the `miri` driver then crashes when it opens or starts streaming, with a
NaN tuner setup (`res: -nanmhz`, `wtf is if2?`, `set_reg52 err`) or a heap
abort (`malloc(): invalid size`) at any sample rate, the system's
`libmirisdr` is too broken for the device. We saw this with
libmirisdr-0.0.4. Rather than fight it, install the SDRplay API service
(sdrplay.com) and `soapysdr-module-sdrplay3`, keep the blacklist above, and
use `--source sdrplay`. The same clone then shows up as an RSP1 and works,
with native support for low sample rates and proper AGC.

If apt has no `soapysdr-module-sdrplay3` (only the older `-sdrplay` module,
which doesn't work with version 3 of the API, or nothing at all), build it
from source against the installed API:

```sh
sudo apt install -y git cmake build-essential libsoapysdr-dev
git clone https://github.com/pothosware/SoapySDRPlay3.git
cd SoapySDRPlay3 && mkdir build && cd build
cmake ..            # must report that it found the SDRplay API
make && sudo make install && sudo ldconfig
SoapySDRUtil --find # driver=sdrplay should now appear
```

The SDRplay installer doesn't always create a systemd unit for the API
service. If `systemctl status sdrplay` shows nothing, run
`/opt/sdrplay_api/sdrplay_apiService` directly or write a unit for it.
SoapySDRPlay3 only needs the service to be running, however it was started.

## 7. ka9q-radio (RX-888 and other wideband front ends)

`radiod` from [ka9q-radio](https://github.com/ka9q/ka9q-radio) does the
wideband DSP and multicasts each channel as RTP IQ, and OpenSkimmer joins
the multicast groups (`--band ka9q@GROUP:center/span`). There are example
`radiod` configurations in `docs/`: `radiod@skimmer-rtlsdr.conf` for an
RTL-SDR, and `radiod@skimmer-rx888.conf` and
`radiod@skimmer-rx888-warc96.conf` for an RX-888 mk2. The comments in each
file explain the channel layout and the traps. One trap to know up front:
the systemd unit name must not include `.conf`. Use
`radiod@skimmer-rx888`, not `radiod@skimmer-rx888.conf`.

Two options must match how `radiod` is set up. A channel with
`encoding = f32` needs `--rtp-encoding f32le` (the default reads radiod's
16-bit format; read with the wrong one, the IQ is noise and nothing is
spotted). With `radiod` on the same host and `ttl = 0`, which keeps its
multicast off the LAN, it sends only on the loopback interface, so join
there with `--rtp-iface 127.0.0.1`.

Multicast IQ at these rates often loses packets with default Linux network
settings. If the skimmer logs `RTP packet(s) lost`, these can help:

- `deploy/tune-net-buffers.sh` raises socket buffer limits. It never lowers
  a value that is already higher.
- `deploy/tune-net-qdisc.sh` replaces a queueing discipline that drops
  packets, and turns off Ethernet flow control on the sending host.
- `tools/rtp_diag.py`, `tools/site_diag.py`, `tools/watch_sndbuf.py` and
  `tools/loss_episodes.py` help find where packets are being lost.

Read each script's header before running it, because they change system
settings.

## 8. Callsign databases

Fetch these once, and again every so often to pick up new calls:

```sh
cd ~/openskimmer
curl -sLo MASTER.SCP  https://www.supercheckpartial.com/MASTER.SCP
curl -sLo patt3ch.lst https://data.reversebeacon.net/downloads/patt3ch/patt3ch.lst
curl -s https://api.pota.app/activator/all -o activators.json
python3 -c "import json; print('\n'.join(sorted(a['activeCallsign'] \
    for a in json.load(open('activators.json')))))" > pota_activators.txt
cat MASTER.SCP pota_activators.txt | tr -d '\r' | sort -u > combined.txt
sudo systemctl restart openskimmer
```
