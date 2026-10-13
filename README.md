# als2flp — Ableton Live Set → FL Studio project converter

`als2flp` converts an Ableton Live Set (`.als`) into a **native FL Studio
project (`.flp`)**. It reads Live's gzipped XML directly and writes FL Studio's
binary event format itself: no DAW, no MIDI export, no intermediate format.

It is pure Python with **zero dependencies** and runs the same on
**Windows, macOS and Linux** (Python 3.8+).

## What gets converted

| Ableton                                   | FL Studio                                                                 |
|-------------------------------------------|---------------------------------------------------------------------------|
| Tempo, time signature                     | Project tempo / time signature (time-signature changes → markers)         |
| Arrangement MIDI clips (incl. loops)      | Patterns (loops unrolled, start offsets honoured) placed in the Playlist  |
| Session View MIDI clips                   | Extra patterns named `[Session] Track - Clip` (not placed; `--no-session` to skip) |
| Notes: pitch, length, velocity, release   | Pattern notes (release velocity → FL release)                             |
| Drum Rack                                 | One Sampler channel per pad, with the pad's sample, level, pan            |
| Simpler / Sampler                         | Sampler channel with the sample and root key                              |
| VST2 plugin instruments                   | Fruity Wrapper channel, plugin id + name; preset state transferred (chunk or parameter presets) |
| VST3 / AU plugin instruments              | Fruity Wrapper channel identified by name (and VST3 class id)             |
| Native Live instruments (Operator, Wavetable, …), Max for Live | Empty Sampler channel keeping the notes (there is no FL equivalent) |
| Audio tracks / arrangement audio clips    | Audio Clip channels in the Playlist with clip start/end, gain, transpose; warped clips get FL time-stretch to the same length |
| Tracks                                    | One Playlist track and one Mixer insert per track, with name and colour   |
| Track volume, pan, mute                   | Mixer insert level, pan, mute                                             |
| Return tracks + sends                     | Inserts with send routing and levels                                      |
| Group tracks                              | Group insert; member tracks route into it                                 |
| Locators                                  | Playlist time markers                                                     |

Not converted (yet): automation envelopes, audio/MIDI effect devices,
clip envelopes, tempo changes (the initial tempo is used and a warning is printed),
Impulse / multisample zones beyond the first sample, crossfades.

## Install

```bash
pip install git+https://github.com/grioghar/ableton-to-flstudio-save-convert
```

or just clone the repo and run it in place (no dependencies needed):

```bash
python -m als2flp "My Song Project/My Song.als"
```

## Usage

```text
als2flp INPUT.als [INPUT2.als ...] [-o OUTPUT.flp | -o DIR] [options]

  --ppq N               FL Studio timebase (default 96, FL's own default)
  --no-session          ignore Session View clips
  --path-map OLD=NEW    rewrite sample paths that start with OLD (repeatable)
  --windows-paths       write sample paths with backslashes
  --copy-samples [DIR]  copy every referenced sample into DIR
                        (default <output>_Samples) and point the project at the copies
  --title / --author    project metadata
  -v                    show per-track notes and the list of missing sample files
```

Examples:

```bash
# Convert next to the original
als2flp "Miami Project/Miami.als"

# Convert for a Windows machine: collect samples and rewrite the paths
als2flp Miami.als -o out/Miami.flp --copy-samples \
    --path-map "/Users/me/out=D:\\Projects\\Miami" --windows-paths

# Inspect what ended up in an .flp (no FL Studio needed)
python -m als2flp.flp_inspect out/Miami.flp
```

The converter prints a short report: tempo, counts of tracks/channels/patterns,
and warnings for anything it could not map (unsupported instruments, samples
that are not on disk, tempo automation, …).

## How it works

* `als2flp/als_reader.py` – parses the Live Set XML into a small DAW-neutral model
  (tracks, clips, notes, samples, instruments, mixer, locators).
* `als2flp/convert.py` – maps that model onto FL Studio concepts: channels,
  patterns, playlist items, mixer inserts, routing. Clip loops are unrolled so
  the Playlist matches the Arrangement bar for bar.
* `als2flp/flp_writer.py` – serialises the FL Studio event stream
  (`FLhd`/`FLdt` chunks, byte/word/dword/varint events, the channel, pattern,
  playlist, track and mixer structures).
* `als2flp/flp_inspect.py` – a tiny `.flp` reader used by the tests and handy for
  checking output.

Volume faders use FL's non-linear curves (mixer: 12800 = 0 dB, 16000 = +5.6 dB;
channel: 10000 = −5.2 dB), so levels land close to Ableton's, not exactly on them.

## Verifying

```bash
python -m unittest discover -s tests -v
```

The suite builds a synthetic Live Set exercising loops, start offsets, Drum Racks,
VST2/VST3 instruments, warped audio, sends, groups, mutes and locators, converts
it, and reads the `.flp` back. If [PyFLP](https://github.com/demberto/PyFLP) is
installed (it only works on Python ≤ 3.10), an extra test round-trips the file through it as well.

## Notes and caveats

* Sample paths are written as stored by Live (absolute). Use `--copy-samples`
  and/or `--path-map` when the project will be opened on another computer.
* FL Studio locates plugins by name/identifier; the plugin must be installed
  there. Live's **AU** plugins need the VST version in FL Studio.
* VST3 and AU preset state cannot be transferred into Fruity Wrapper's format;
  the plugin loads with its default preset. VST2 state is transferred.
* Warped audio uses FL's time-stretch set to the clip's warped length
  (mode chosen from Live's warp mode). Un-warped clips play at their original speed.
* Files are written as FL Studio 20.7 projects and open in FL Studio 20.7 and later.

## License

MIT
