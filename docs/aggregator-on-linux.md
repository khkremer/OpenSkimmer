# Running the RBN Aggregator on Linux

The Reverse Beacon Network's Aggregator takes spots from one or more
skimmers and uploads them to the RBN. It is a Windows program with no
open-source replacement, so running it usually means keeping a Windows
machine around just for that.

It also runs on Linux under Wine. These steps start from a fresh Linux Mint
install and were tested on Mint 22.3 (Ubuntu 24.04 "noble" base) with
Aggregator 6.7, feeding three OpenSkimmer instances to the RBN. The install
takes about 400 MB, mostly 32-bit Wine libraries.

## Why Wine and not Mono

Aggregator is a 32-bit .NET 4.0 assembly using WinForms (no WPF), so running
it directly on Mono looks plausible. It doesn't work:

    $ strings Aggregator.exe | grep PrivateProfile
    WritePrivateProfileString
    GetPrivateProfileString

It stores all of its configuration through the Win32 INI functions in
`kernel32`. Those are Windows functions, not .NET, and Mono doesn't implement
them. Under plain Mono the program starts, draws its window, and then fails
with "Error Saving Aggregator.ini" without touching the file system, because
the P/Invoke fails before any I/O happens. It can't save its settings, so it
can't be configured.

Wine implements those functions, so under Wine the INI file is written
normally. You don't need the real .NET Framework either. Wine ships
`wine-mono`, which runs the managed code while the P/Invokes go to Wine's own
`kernel32`. That skips `winetricks dotnet48`, the step that makes most
Wine/.NET guides painful.

## 1. Install Wine

```bash
sudo dpkg --add-architecture i386        # usually already enabled on Mint
sudo apt update
sudo apt install --install-recommends wine wine32:i386 winetricks
wine --version                            # 9.0 or newer is fine
```

The distro package is sufficient. No WineHQ repository needed.

## 2. Create a 32-bit prefix

Aggregator.exe is PE32, so use a win32 prefix, which avoids WoW64
complications.

```bash
export WINEPREFIX=$HOME/.wine-agg WINEARCH=win32
```

Put `wine-mono` in Wine's cache before the first `wineboot`. Otherwise Wine
shows a download prompt, which is awkward over SSH, and skips the install
silently if there is no display:

```bash
mkdir -p ~/.cache/wine
wget -O ~/.cache/wine/wine-mono-8.1.0-x86.msi \
  https://dl.winehq.org/wine/wine-mono/8.1.0/wine-mono-8.1.0-x86.msi
wineboot -u
ls "$WINEPREFIX/drive_c/windows/mono"     # must show mono-2.0
```

Match the wine-mono version to your Wine: 8.1.0 pairs with Wine 9.0. If
`mono-2.0` is missing, nothing else here will work.

## 3. Install Aggregator

There is no installer. It's a single executable that has to live in a
writable directory.

```bash
mkdir -p ~/aggregator && cd ~/aggregator
wget -O Aggregator.exe \
  "https://cms.reversebeacon.net/sites/cms.reversebeacon.net/files/2025/02/21/Aggregator%20v6.7.exe"
```

Check <https://www.reversebeacon.net/pages/Aggregator+34> for the current
version.

## 4. Supply Patt3Ch.lst

Aggregator reads the RBN callsign-pattern file from CW Skimmer's data folder
(Afreet is CW Skimmer's vendor). Windows paths map predictably into the
prefix:

| Windows | Linux |
|---|---|
| `C:\` | `~/.wine-agg/drive_c/` |
| `C:\users\<you>\AppData\Roaming\` | `~/.wine-agg/drive_c/users/<you>/AppData/Roaming/` |

`AppData\Roaming` is a real directory inside the prefix. Only Desktop,
Documents and similar folders are symlinked out to the XDG folders.

```bash
D=~/.wine-agg/drive_c/users/$USER/AppData/Roaming/Afreet/Reference
mkdir -p "$D"
cp ~/openskimmer/patt3ch.lst "$D/Patt3Ch.lst"     # note the capitalisation
```

Use the exact Windows capitalisation. Linux is case-sensitive underneath,
and Wine's case-insensitive matching isn't worth relying on. This is the same
file OpenSkimmer takes with `--patterns`.

`winepath -u 'C:\some\path'` translates any Windows path for you.

## 5. Launcher script

Aggregator saves its window geometry to `[Window]` in `Aggregator.ini`, but
under Wine it doesn't reliably apply it. It has started at 152x1 pixels, a
sliver of title bar with nothing to click. Setting the size from outside
after the window appears works, and the program leaves it alone.

Save as `~/aggregator/start-aggregator.sh` and `chmod +x` it:

```bash
#!/bin/bash
set -u
export WINEPREFIX="$HOME/.wine-agg" WINEARCH=win32
APPDIR="$HOME/aggregator"; INI="$APPDIR/Aggregator.ini"

ini_val() { awk -v k="$1" -F= '/^\[Window\]/{f=1;next} /^\[/{f=0} f && $1==k {print $2}' "$INI" 2>/dev/null | tr -d '\r'; }
W=${AGG_W:-$(ini_val Width)};  H=${AGG_H:-$(ini_val Height)}
[[ "${W:-0}" =~ ^[0-9]+$ ]] && (( W >= 600 )) || W=1100
[[ "${H:-0}" =~ ^[0-9]+$ ]] && (( H >= 400 )) || H=750
X=${AGG_X:-120}; Y=${AGG_Y:-90}

# pgrep -x matches the process NAME. pgrep -f would match this script too.
if pgrep -x Aggregator.exe >/dev/null; then
    command -v wmctrl >/dev/null && wmctrl -a "Aggregator"
    exit 0
fi

cd "$APPDIR" || exit 1
setsid nohup wine Aggregator.exe >"$APPDIR/wine.log" 2>&1 </dev/null &

for _ in $(seq 1 40); do
    sleep 1
    id=$(wmctrl -l 2>/dev/null | awk '/Aggregator/{print $1; exit}')
    [ -n "$id" ] || continue
    sleep 1
    wmctrl -i -r "$id" -e "0,$X,$Y,$W,$H" 2>/dev/null
    exit 0
done
```

Needs `wmctrl` (`sudo apt install wmctrl`). Override the size per run with
`AGG_W=1400 AGG_H=900 ./start-aggregator.sh`.

## 6. Desktop icon and menu entry

Extract the real icon from the executable. `wrestool` alone produces a
malformed `.ico` here (it writes the group directory's declared length rather
than the actual bitmap), so trim it to the size the DIB header implies:

```bash
sudo apt install icoutils wmctrl
cd /tmp && wrestool -x -t 14 ~/aggregator/Aggregator.exe -o /tmp/
python3 - <<'EOF'
import struct, glob
src = sorted(glob.glob('/tmp/*.ico'))[0]
d = open(src, 'rb').read()
w,h,cc,_r,pl,bc,sz,off = struct.unpack_from('<BBBBHHII', d, 6)
blob = d[off:off+sz]
hsz,bw,bh,planes,bits = struct.unpack_from('<IiiHH', blob, 0)
W,H = bw, bh//2
row=((W*bits+31)//32)*4; mrow=((W+31)//32)*4
ncol=(1<<bits) if bits<=8 else 0
blob = blob[:hsz+ncol*4+row*H+mrow*H]
out = bytearray(struct.pack('<HHH',0,1,1))
out += struct.pack('<BBBBHHII', W%256, H%256, ncol%256, 0, planes, bits, len(blob), 22)
out += blob
open('/tmp/rbn-aggregator.ico','wb').write(out)
print('wrote /tmp/rbn-aggregator.ico', len(out), 'bytes')
EOF
mkdir -p ~/.local/share/icons && cp /tmp/rbn-aggregator.ico ~/.local/share/icons/
```

Then the launcher itself:

```bash
cat > ~/Desktop/rbn-aggregator.desktop <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=RBN Aggregator
Comment=Aggregator 6.7 running under Wine
Exec=$HOME/aggregator/start-aggregator.sh
Path=$HOME/aggregator
Icon=$HOME/.local/share/icons/rbn-aggregator.ico
Terminal=false
StartupNotify=true
Categories=Network;HamRadio;
EOF
chmod 755 ~/Desktop/rbn-aggregator.desktop
cp ~/Desktop/rbn-aggregator.desktop ~/.local/share/applications/
gio set ~/Desktop/rbn-aggregator.desktop metadata::trusted true
```

Don't skip `gio set ... trusted`: Cinnamon and GNOME refuse to run desktop
launchers that aren't marked trusted.

## 7. Start at login

```bash
mkdir -p ~/.config/autostart
cp ~/Desktop/rbn-aggregator.desktop ~/.config/autostart/
printf 'X-GNOME-Autostart-enabled=true\nX-GNOME-Autostart-Delay=20\n' \
  >> ~/.config/autostart/rbn-aggregator.desktop
chmod 644 ~/.config/autostart/rbn-aggregator.desktop
```

The 20-second delay lets the desktop session and the network settle before
Aggregator reaches for the skimmers.

Aggregator is a GUI program, so it needs a logged-in desktop session. On a
headless machine you'd need `Xvfb` plus `x11vnc`.

## 8. Configure it

The rest is done in the program's own window:

- Aggregator Information: your callsign and grid. Watch the capitalisation;
  the RBN validates the spotter callsign.
- Connections - Skimmer: the primary skimmer's IP and port (OpenSkimmer's
  Telnet port, 7300 by default).
- Secondary Skimmers: one entry per additional skimmer, each with its own
  IP, port and Auto Start.

The settings are saved in `~/aggregator/Aggregator.ini`, an ordinary text
file. Aggregator rewrites it on exit, so only edit it while the program is
closed.

## 9. Verifying it works

Don't look for a standing connection to the RBN. Aggregator uploads spots in
short HTTP POSTs to `www.reversebeacon.net/fb0.php`; it doesn't keep a Telnet
session to `telnet.reversebeacon.net:7000`, which is where users receive
spots. Each POST lasts milliseconds, so `ss` will almost never catch one, and
not seeing one proves nothing.

A working Aggregator has exactly one TCP connection per skimmer:

```bash
sudo ss -tnp | grep -i aggregator
#   192.0.2.10:xxxxx   192.0.2.20:7300   first skimmer host
#   192.0.2.10:xxxxx   192.0.2.30:7300   second skimmer host
#   192.0.2.10:xxxxx   192.0.2.10:7300   a skimmer on this machine
```

The real test is spots with your callsign as the spotter and current
timestamps showing up at <https://www.reversebeacon.net/>.

## Troubleshooting

"Error Saving Aggregator.ini" means it's running under plain Mono rather than
Wine. That can't be fixed; see "Why Wine and not Mono" above.

If the window is a sliver of title bar, start it with the launcher script
from section 5.

If `wineboot` hangs or Mono never installs, the MSI wasn't in
`~/.cache/wine/`, or its version doesn't match your Wine.

If it complains about a missing file in `AppData\Roaming\Afreet\...`, put
the file there with the exact Windows capitalisation. It may also want
`MASTER.DTA` next to `Patt3Ch.lst` if you run CW Skimmer as well.

In scripts, use `pgrep -x` and `pkill -x`, which match the process name.
`pgrep -f` matches the whole command line, including the command doing the
matching, so it reports your own script as a running instance, and
`pkill -f` can kill the shell it runs in.
