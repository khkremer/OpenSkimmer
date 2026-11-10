# How OpenSkimmer works, in plain language

This is a tour of what OpenSkimmer does between radio waves coming in and a
callsign appearing on the screen. It needs no math. If you know Morse code
and roughly what a radio does, you'll be able to follow it.

## The short version

OpenSkimmer listens to a wide slice of a ham band, follows every Morse signal
in that slice at once, decodes each one into letters, works out which letters
form a real callsign, and announces that it heard this station on this
frequency just now. It does that continuously, for as many signals as the band
holds.

Picture a room of Morse operators, each assigned to one signal on the band,
all calling out the callsigns they hear. OpenSkimmer is one program doing the
work of that room.

## The problem it solves

A CW operator tunes to one signal at a time and copies it by ear. If fifty
stations are calling CQ across the band, you hear one of them. A skimmer
copies all of them at once and posts what it hears to the Reverse Beacon
Network (RBN), a worldwide record of who is audible where. That's how you can
look up your own call and see within minutes which parts of the world are
hearing you.

Getting there takes nine steps.

## Step 1: get the raw radio data

OpenSkimmer doesn't connect to an antenna. It talks to a software-defined
radio (SDR): a Hermes-Lite 2, a KiwiSDR, an SDRplay, an RX-888 and so on. The
SDR delivers a fast stream of numbers describing the radio waves in a chunk
of spectrum, typically 192 kHz wide (say 14.000 to 14.192 MHz on 20 meters).

These numbers are called I/Q samples. You can think of them as a very fast,
very detailed recording of everything happening on that stretch of band:
192,000 snapshots per second. Each type of radio has its own backend that
converts the radio's format into this same stream, so everything after this
step works the same whichever radio you use.

## Step 2: turn the band into a spectrogram

The first real processing step turns the incoming stream into a picture, the
same scrolling waterfall you've seen on any SDR.

The tool for this is the Fourier transform (FFT). It takes a short slice of
the recording and works out how much energy there is at each frequency at
that moment. Repeat that many times a second and you get a running chart:
frequency across, time down, and brightness for signal strength.

OpenSkimmer sets this up so that each frequency lane is about 47 Hz wide,
narrow enough that two CW signals close together land in different lanes. It
takes a new snapshot about 187 times a second, fast enough to see single dits
and dahs switch on and off even above 40 words per minute.

After this step the band is a grid of brightness values. Each frequency lane
has its own stream of loud, loud, quiet, loud as the keying switches the
carrier on and off. Everything after this works from that grid.

## Step 3: find the signals in the noise

Most of the band is noise, with signals standing up here and there. The
software has to decide which lanes hold a station and which hold only static.

The trick is estimating the noise floor. For each stretch of about 3 kHz,
OpenSkimmer takes the median strength across all the lanes in it. Real
signals occupy only a minority of any small stretch, so the median is the
background noise, and signals are what stands out above it.

The median follows the natural shape of the passband (some parts are always a
little louder), and the CW signals can't drag it up because they are the
minority. Anything clearly above this floor, roughly 6 dB or about four times
the noise power, becomes a candidate signal at its own frequency.

Every quarter second the software scans again. It picks up stations that have
started calling and retires ones that have gone quiet. It also measures each
signal's frequency to better than one lane's width, so the reported frequency
is accurate.

## Step 4: one decoder per signal

This is where the room of operators becomes real. For every signal it finds,
OpenSkimmer starts a separate decoder that watches only that frequency. A busy
contest weekend can have a hundred of them running at once, all fed from the
same spectrogram. They don't interfere with each other; each one watches its
own lane light up and go dark.

## Step 5: turn brightness into dots and dashes

Each decoder now has to read its lane's blinking as Morse. That is harder
than it sounds, for three reasons.

The first is deciding when the key is down. The decoder compares the signal's
brightness with the local noise estimate and decides on or off. It uses
hysteresis (a change of state needs more evidence than staying put) and a
short debounce delay, so a moment of noise or fading doesn't become a dit.
Your ear does the same when it ignores a crackle of static in the middle of a
letter.

The second is speed. Morse has no fixed speed: a dit at 15 WPM lasts nearly
three times as long as one at 40 WPM. The decoder learns the speed as it
listens. It watches the lengths of the on periods, sees them fall into two
groups (dits short, dahs long), and keeps updating its estimate of a dit for
this particular signal. When the operator speeds up or slows down, the
estimate follows.

The third is gaps. In Morse the silences carry meaning too: a short gap
separates the elements within a letter, a medium one separates letters, and a
long one separates words. The decoder measures the silences against the same
learned dit length to know when a letter has ended and a new word begun.

What comes out of this step, per signal, is dots and dashes grouped into
letters, such as `-.-.` `--.-`.

## Step 6: dots and dashes into text

This step is a table lookup: `.-` is A, `-...` is B, and so on. Each decoder
now produces text like `CQ CQ DE W1AW W1AW K`.

## Step 7: find the callsign in the text

The decoded text is a stream of words, and the software has to work out which
word is a callsign. That turns out to be the hard part.

It starts with the grammar of a CW contact:

- `CQ <call>`: someone calling CQ. `TEST <call>` in a contest and
  `POTA <call>` from a park activator mean the same.
- `DE <call>`: "this is", followed by a callsign.

A callsign with neither in front of it is never spotted, however often it
repeats. Noise can produce the same call-shaped string twice, but it rarely
puts a CQ in front of it.

The CQ also has to belong to the right station. In a busy contest the station
calling CQ and the stations answering it are often on exactly the same
frequency, so one decoder hears all of them in a single stream:
`CQ CWT N4BP N1EN BOB 2463`. N4BP called CQ and N1EN is answering, so
OpenSkimmer gives the CQ only to the first call after it. A call with another
call between it and the CQ is a caller and isn't reported as calling CQ.

A few more cases follow the same idea. Some runners end their CQ with TEST
(`CWT NA2U NA2U TEST`) and the caller answers right after it, so a TEST
following the runner's own call belongs to the runner. The same goes for a
runner's call with its CQ words run onto it (`CQ CWTDL8LAS K0AF`: K0AF is
calling DL8LAS). And once a signal has been spotted as one station, a new
call on it needs its CQ right in front of it. A new call there is usually a
caller who came right after a runner's call that the decoder garbled
(`TEST WA3 TT KB9S`, where `WA3 TT` was WA3AAN), and only rarely a new station
taking over the frequency. Skimmers that get this wrong report a station as
running on a frequency it was only calling on, and that's a well-known
complaint about them.

Real copy off the air is messy in other ways too. Operators stretch their
spacing, so a call splits into pieces (`K1N MF` for `K1NMF`). Or they run
words together, so the call fuses onto its neighbor: `DEWC5WC` is `DE` and
`WC5WC` jammed together, and in `W1RW5NN` a `5NN` report stuck to the end.
OpenSkimmer tries the bare word, tries gluing split pieces back together, and
tries peeling off prefixes and suffixes that look like grammar words or report
fragments.

Grammar words run together are still recognized: `CQCQDE` is "CQ CQ DE" with
the gaps lost. So are grammar words split in the wrong places. `CQP OTADE
KS4CR` is "CQ POTA DE KS4CR" with the gaps after the P and the A instead of
after the Q and the A, and joining the pieces shows it. Text rebuilt this way
only ever supplies the CQ; the callsign itself still has to be copied
cleanly.

OpenSkimmer also keeps a list of words that look like calls but never are
(`CQ`, `TEST`, `599`, `73` and so on), so they aren't mistaken for stations.

## Step 8: make sure the callsign is real

A decode error can easily produce a call-shaped string that no station owns.
OpenSkimmer checks every candidate at three levels, the same layered scheme
CW Skimmer uses:

1. Does it have a legal callsign shape? Real calls follow rules (a prefix
   ending in a digit, then a suffix), and a pattern check throws out anything
   that couldn't be a callsign at all.

2. Is its pattern one that real calls use? The RBN publishes a database of
   callsign shapes that exist in the world. A candidate whose shape is on
   that list has to be copied cleanly twice. One with an implausible shape,
   a pattern no real prefix produces, needs a third copy.

3. Is it a known call? An optional list of real, active callsigns (from
   contest logs, POTA activators, FCC data and so on) helps here. A listed
   candidate wins over garbled near-misses of itself, and a four-character
   call needs one copy fewer if it's listed.

A clean copy means the call came out as a word of its own, exactly as
decoded. A callsign the software rebuilt from pieces can be spotted, but it
never counts as a second, independent copy.

One more check catches the decoder's most common mistake on a strong runner.
After hearing EA5M twenty times, a single fade can turn the last dah of the M
into a T, and out comes `EA5T`, which is also a real callsign. So OpenSkimmer
compares calls as dits and dahs, not letters. A new call within two dits or
dahs of the station this signal has been sending is treated as a garble of
it, unless it has been copied just as often.

So every spot needs a CQ or DE in front of it and at least two clean copies
of the call. That holds even for calls in the known-calls list, because a
garbled copy of one station is often another real station's call. A call the
software is unsure of needs a third copy: one with an implausible shape, and
a short one (three characters, or four and not in the known-calls list). A
call that lost its first or last letter looks just like a short call
(`N8A` for `N8AI`).

## Step 9: announce the spot, once

A callsign that passes all this becomes a spot: "I heard W1AW on 14031.0 kHz,
signal 23 dB, sending 25 WPM, calling CQ, at 1516 UTC."

To avoid flooding the network, the software remembers what it has reported.
The same station within about 1 kHz of the same frequency isn't spotted again
for 10 minutes, which matches the RBN's own duplicate window. If it moves
further or comes back later, that's new and gets reported. OpenSkimmer also
never spots your own callsign, so your transmissions don't fill the network
with spots of yourself.

Finished spots go three places. They're printed to the screen. They're served
on a Telnet port that DX-cluster programs and the RBN Aggregator connect to,
formatted exactly like CW Skimmer's output so existing tools accept them
unchanged. And they appear on a live web waterfall, where you can watch the
spectrum, the spot labels on their frequencies, and each decoder's running
text.

From there the RBN Aggregator forwards the spots to the Reverse Beacon
Network, and a few minutes later someone on the other side of the world can
look up their call and see that your station heard them.

## The default decoder

Steps 5 and 6 describe the classical decoder: thresholds, adaptive timing,
and a lookup table. The default decoder is newer and uses a hidden Markov
model. Rather than deciding key up or key down at each moment and living
with its mistakes, it looks at the recent history of a signal and works out
which sequence of key-down and key-up periods most probably produced what it
saw. It holds each decision back for a fraction of a second, so later
evidence can still change it.

In practice it copies signals about 3 dB deeper into the noise than the
classical decoder, roughly the difference between a signal you can barely
hear and one you can copy comfortably. On a recording of a CWT contest it
found more unique callsigns than either the classical decoder or CW Skimmer
working on the same recording. The two decoders are interchangeable; nothing
else in the pipeline changes.

## The whole path

Radio waves become a wide digital recording of the band, then a scrolling
picture of frequency against time. OpenSkimmer finds the frequencies that
hold a signal and gives each one its own decoder. Each decoder reads its
lane's brightness as dits and dahs at that station's own speed and turns them
into letters. The letters are searched for a callsign, the callsign is
checked, and a de-duplicated spot goes out to the world, for every signal on
the band, for as long as the program runs.
