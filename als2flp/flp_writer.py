"""Native FL Studio project (.flp) writer.

The FLP format is a tagged event stream:

* ``FLhd`` header chunk: format (0 = project), channel count, PPQ.
* ``FLdt`` data chunk: a sequence of events.  The event id decides the payload
  size: 0-63 one byte, 64-127 a 16-bit word, 128-191 a 32-bit dword,
  192-255 a varint length followed by raw bytes (text is UTF-16LE, NUL-terminated).

The structures below are written in the order FL Studio itself (and other
third-party writers) emit them.  Everything is plain ``struct`` packing, no
dependencies, so it runs identically on Windows, macOS and Linux.
"""

from __future__ import annotations

import datetime as _dt
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

FL_VERSION = "20.7.2.1852"
FL_BUILD = 1852

PATTERN_BASE = 20480          # playlist item_index >= PATTERN_BASE means "pattern"
MAX_TRACKS = 500
NUM_INSERTS = 127             # master + 125 inserts + current
MAX_USER_INSERT = 125

# Channel types
CH_SAMPLER = 0
CH_PLUGIN = 2
CH_LAYER = 3
CH_AUDIOCLIP = 4
CH_AUTOMATION = 5

# Stretch modes (channel sampler settings)
STRETCH_STRETCH = -1
STRETCH_RESAMPLE = 0
STRETCH_E3_GENERIC = 1
STRETCH_E3_MONO = 2
STRETCH_SLICE = 3
STRETCH_SLICE_MAP = 4
STRETCH_AUTO = 5

STRETCH_UNITS_PER_BEAT = 192  # "stretch time" is stored as 1/192 of a quarter note

# Time marker types
MARKER_PLAIN = 0
MARKER_TIMESIG = 8

# Channel rack display (filter) groups, in the order FL Studio creates them.
DISPLAY_GROUPS = ["Unsorted", "Audio clips", "Automation"]
GROUP_UNSORTED = 0
GROUP_AUDIO = 1
GROUP_AUTOMATION = 2


# ------------------------------------------------------------ event stream --

def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _text(s: str) -> bytes:
    return s.encode("utf-16-le") + b"\x00\x00"


class EventStream:
    def __init__(self) -> None:
        self.buf = bytearray()

    def byte(self, eid: int, v: int) -> None:
        assert 0 <= eid <= 63
        self.buf += struct.pack("<BB", eid, int(v) & 0xFF)

    def word(self, eid: int, v: int) -> None:
        assert 64 <= eid <= 127
        self.buf += struct.pack("<BH", eid, int(v) & 0xFFFF)

    def dword(self, eid: int, v: int) -> None:
        assert 128 <= eid <= 191
        self.buf += struct.pack("<BI", eid, int(v) & 0xFFFFFFFF)

    def data(self, eid: int, payload: bytes) -> None:
        assert 192 <= eid <= 255
        self.buf += bytes([eid]) + _varint(len(payload)) + payload

    def text(self, eid: int, s: str) -> None:
        self.data(eid, _text(s))

    def ascii(self, eid: int, s: str) -> None:
        self.data(eid, s.encode("ascii", "replace") + b"\x00")


# ---------------------------------------------------------------- objects ---

@dataclass
class FLNote:
    pos: int                 # ticks from pattern start
    length: int              # ticks
    key: int                 # 0..131
    rack: int                # channel index
    velocity: int = 100      # 0..128
    release: int = 64        # 0..128
    pan: int = 64            # 0..128
    fine_pitch: int = 120    # 0..240, 120 = centre
    flags: int = 16384
    mod_x: int = 128
    mod_y: int = 128

    def pack(self) -> bytes:
        return struct.pack(
            "<IHHIHHBBBBBBBB",
            self.pos, self.flags, self.rack, self.length, self.key, 0,
            self.fine_pitch, 0, self.release, 0, self.pan, self.velocity,
            self.mod_x, self.mod_y,
        )


@dataclass
class FLPattern:
    name: str
    color: Optional[int] = None
    notes: List[FLNote] = field(default_factory=list)


@dataclass
class FLVstWrapper:
    """Fruity Wrapper state for an external plugin."""
    name: str
    vendor: str = ""
    is_generator: bool = True
    fourcc: Optional[int] = None
    guid: Optional[bytes] = None
    path: str = ""
    state: Optional[bytes] = None   # already in wrapper-state format (see helpers below)

    @staticmethod
    def _chunk(cid: int, payload: bytes) -> bytes:
        return struct.pack("<IQ", cid, len(payload)) + payload

    def to_bytes(self) -> bytes:
        out = bytearray(b"\x0a\x00\x00\x00")
        plug_type = 4 if self.is_generator else 0
        out += self._chunk(50, struct.pack("<I", plug_type) + b"\x00" * 12)
        if self.fourcc is not None:
            out += self._chunk(51, struct.pack("<I", self.fourcc & 0xFFFFFFFF))
        if self.guid is not None and len(self.guid) == 16:
            out += self._chunk(52, self.guid)
        out += self._chunk(57, b"\x60\x09\x00\x00")
        if self.name:
            out += self._chunk(54, self.name.encode("utf-8"))
        if self.path:
            out += self._chunk(55, self.path.encode("utf-8"))
        if self.vendor:
            out += self._chunk(56, self.vendor.encode("utf-8"))
        if self.state:
            out += self._chunk(53, self.state)
        return bytes(out)


def vst2_state_from_chunk(chunk: bytes, program: int = 0, use_program: bool = False) -> bytes:
    """Wrap a raw VST2 'chunk' (as returned by effGetChunk) for Fruity Wrapper."""
    if use_program:
        return (b"\xf7\xff\xff\xff\x0d\xfe\xff\xff\xff" + struct.pack("<I", len(chunk))
                + b"\x00\x00\x00\x00" + struct.pack("<I", program) + chunk)
    return (b"\xf7\xff\xff\xff\x0c\xfe\xff\xff\xff" + struct.pack("<I", len(chunk))
            + b"\x00" * 8 + chunk)


def vst2_state_from_params(programs, current: int = 0) -> bytes:
    """Wrap VST2 parameter-based presets ([(name, [floats]), ...]) for Fruity Wrapper."""
    out = bytearray(b"\xf7\xff\xff\xff\x05\xfe\xff\xff\xff")
    out += b"\x00" * 8
    out += struct.pack("<I", current)
    total = sum(len(p[1]) for p in programs)
    out += struct.pack("<I", total)
    for _, vals in programs:
        out += struct.pack(f"<{len(vals)}f", *vals)
    out += struct.pack("<I", len(programs))
    for name, _ in programs:
        out += name.encode("latin-1", "replace")[:24].ljust(25, b"\x00")
    return bytes(out)


@dataclass
class FLChannel:
    name: str
    ctype: int = CH_SAMPLER
    color: int = 0x5A5A5A
    icon: int = 0
    enabled: bool = True
    insert: int = 0                 # mixer insert (0 = master)
    volume: int = 10000             # 0..12800 (10000 = FL default, 12800 = 100 %)
    pan: int = 6400                 # 0..12800, 6400 = centre
    pitch_cents: int = 0
    sample_path: Optional[str] = None
    root_note: int = 60
    stretch_mode: int = STRETCH_RESAMPLE
    stretch_time: int = 0           # in 1/192 beats; 0 = no stretch
    stretch_pitch_cents: int = 0
    reverse: bool = False
    plugin: Optional[FLVstWrapper] = None
    sampler_flags: int = 10

    # -- sub-structures ----------------------------------------------------
    def _levels(self) -> bytes:
        return struct.pack("<IIiIII", max(0, min(12800, self.pan)), max(0, min(12800, self.volume)),
                           self.pitch_cents, 256, 0, 0)

    def _params(self) -> bytes:
        b = bytearray()
        b += struct.pack("<ii", -1, 0)
        b += struct.pack("<BBBB", 0, 0, 0, 1)           # unk, remove_dc, delay flags, main pitch
        b += struct.pack("<ii", -1, 60)
        b += struct.pack("<fffff", 1.0, 1.0, 1.0, 1.0, 1.0)   # DS params
        b += struct.pack("<IIIII", 0, 1, 0xFFFFFFFF, 1024, 48)  # arp dir, range, chord, time, gate
        b += struct.pack("<BBBB", 0, 0, 1, 0)           # arp slide, pad, full porta, add to key
        b += struct.pack("<HH", 1447, 0)                # time gate, pad
        b += struct.pack("<III", 0, 256, 0)             # key range min/max, pad
        b += struct.pack("<BBBB", 0, 0, 0, 0)           # normalize, reverse polarity, pad, declick
        b += struct.pack("<III", 0, 0, 1)               # crossfade, trim, arp repeat
        b += struct.pack("<Iiii", self.stretch_time, self.stretch_pitch_cents, 0, self.stretch_mode)
        b += struct.pack("<I", 0)                       # midi chan thru
        b += struct.pack("<iii", -2, -1, 0)
        b += struct.pack("<ddd", 0.0, 1.0, 0.0)         # start, length, start offset
        b += b"\xff\xff\xff\xff\x00\x01\x00\x00"
        b += struct.pack("<d", 0.5)                     # formant
        return bytes(b)

    @staticmethod
    def _env_lfo(flags: int = 0) -> bytes:
        return struct.pack("<IIIIIIIIiIIIIIiii", flags, 0, 100, 20000, 20000, 30000, 50, 20000, 0,
                           100, 20000, 0, 32950, 0, 0, 0, 0)

    def write(self, ev: EventStream, index: int) -> None:
        ev.word(64, index)
        ev.byte(21, self.ctype)
        if self.ctype == CH_PLUGIN and self.plugin is not None:
            ev.text(201, "Fruity Wrapper")
            flags = (1 << 4) | (1 << 6)   # generator, threaded processing
            ev.data(212, struct.pack("<IIIII", 0, 0, 2, 0, flags) + struct.pack("<IIII", 0, 0, 0, 0)
                    + struct.pack("<IIII", 0, 0, 0, 0))
        ev.text(203, self.name)
        ev.dword(155, self.icon)
        ev.dword(128, self.color)
        if self.ctype == CH_PLUGIN and self.plugin is not None:
            ev.data(213, self.plugin.to_bytes())
        ev.byte(0, 1 if self.enabled else 0)
        ev.data(209, struct.pack("<IIIII", 0, 0, 0, 4, 144))      # delay
        ev.dword(138, 8388736)                                    # delay mod x/y
        ev.dword(139, 65536)                                      # reverb
        ev.word(89, 0)                                            # time shift
        ev.word(97, 128)                                          # swing
        ev.word(69, 128)                                          # freq tilt
        ev.word(86, 256)                                          # pogo
        ev.word(71, 1024)                                         # cutoff
        ev.word(83, 0)                                            # resonance
        ev.word(74, 0)                                            # preamp
        ev.word(75, 0)                                            # fade out
        ev.word(76, 0)                                            # fade in
        ev.word(85, 2048)                                         # stereo delay
        ev.dword(131, 8388608)                                    # ring mod
        fxflags = (1 << 1) if self.reverse else 0
        ev.word(70, fxflags)                                      # fx flags
        ev.byte(22, self.insert)                                  # mixer insert
        ev.data(219, self._levels())
        ev.data(229, b"\x00\x00\x00\x00\x00\x32\x00\x00" + b"\x00" * 12)   # level adjusts
        ev.data(221, struct.pack("<IIB", 0, 500, 0))              # polyphony
        ev.data(215, self._params())
        ev.dword(132, 0)                                          # cut / cut by
        ev.dword(144, 0)                                          # layer flags
        ev.dword(145, GROUP_AUDIO if self.ctype == CH_AUDIOCLIP else GROUP_UNSORTED)  # display group
        ev.byte(32, 0)
        for _ in range(2):
            ev.data(228, struct.pack("<iiii", 0, 0, 0, 0))        # tracking
        for i in range(5):
            ev.data(218, self._env_lfo(4 if i == 1 else 0))       # envelopes / lfo
        ev.dword(143, self.sampler_flags)
        ev.byte(20, 0)                                            # loop type
        if self.sample_path:
            ev.text(196, self.sample_path)
        if self.root_note != 60:
            ev.dword(135, self.root_note)


@dataclass
class FLPlaylistItem:
    position: int                   # ticks
    length: int                     # ticks
    track: int                      # 1-based playlist track
    pattern: Optional[int] = None   # 1-based pattern number, or
    channel: Optional[int] = None   # channel index for audio clips
    start_offset: Optional[float] = None   # pattern: ticks (int), audio: ms (float)
    end_offset: Optional[float] = None
    muted: bool = False

    def pack(self) -> bytes:
        if self.pattern is not None:
            item_index = PATTERN_BASE + self.pattern
        else:
            item_index = int(self.channel or 0)
        flags = 12352 if self.muted else 64
        head = struct.pack("<IHHIIHHI", self.position, PATTERN_BASE, item_index, self.length,
                           MAX_TRACKS - self.track, 120, flags, 2155897920)
        if self.pattern is not None:
            so = 0xFFFFFFFF if self.start_offset is None else int(self.start_offset)
            eo = 0xFFFFFFFF if self.end_offset is None else int(self.end_offset)
            return head + struct.pack("<II", so, eo)
        so = 0.0 if self.start_offset is None else float(self.start_offset)
        eo = 0.0 if self.end_offset is None else float(self.end_offset)
        return head + struct.pack("<ff", so, eo)


@dataclass
class FLTrack:
    name: str = ""
    color: int = 0x565148
    height: float = 1.0
    enabled: bool = True

    def pack(self, index: int) -> bytes:
        return struct.pack("<IIIBfBIIIIIIBB", index, self.color, 0, 1 if self.enabled else 0,
                           self.height, 255, 16777215, 0, 0, 5, 0, 1, 0, 0) \
            + b"\x00" * 9 + b"\xff" * 8 + b"\x01\x00\x00\x00\x00"


@dataclass
class FLTimeMarker:
    position: int
    name: str = ""
    mtype: int = MARKER_PLAIN
    numerator: int = 4
    denominator: int = 4


@dataclass
class FLInsert:
    name: str = ""
    color: Optional[int] = None
    icon: Optional[int] = None
    enabled: bool = True
    volume: int = 12800             # 0..16000, 12800 = 0 dB
    pan: int = 0                    # -6400..6400
    routes: Dict[int, int] = field(default_factory=dict)   # target insert -> level (12800 = 100 %)
    input: int = -1
    output: int = -1

    def flags_bytes(self) -> bytes:
        flags = 4 | 64                 # fx enabled, docked centre
        if self.enabled:
            flags |= 8
        return struct.pack("<fI", 0.0, flags) + b"\x00\x00\x00\x00"


# -------------------------------------------------------------- mixer params --

def _mixer_param(control: int, group: int, value: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack("<HHi", control, group, value)


def _insert_group(insert: int, slot: int = 0) -> int:
    return (2 << 12) + (insert << 6) + slot


def insert_param_records(insert: int, ins: FLInsert) -> bytes:
    base = (31 << 8) + (12 << 4)
    g = _insert_group(insert)
    out = bytearray()
    out += _mixer_param(base + 0, g, max(0, min(16000, ins.volume)))
    out += _mixer_param(base + 1, g, max(-6400, min(6400, ins.pan)))
    out += _mixer_param(base + 2, g, 0)
    for p in (16, 17, 18):
        out += _mixer_param(base + p, g, 0)
    for p, v in ((24, 5777), (25, 33145), (26, 55825)):
        out += _mixer_param(base + p, g, v)
    for p in (32, 33, 34):
        out += _mixer_param(base + p, g, 17500)
    for target, level in sorted(ins.routes.items()):
        if target < 64:
            ctrl = (31 << 8) + (4 << 4) + target
        else:
            ctrl = (31 << 8) + (8 << 4) + (target - 64)
        out += _mixer_param(ctrl, g, max(0, min(12800, level)))
    return bytes(out)


# ---------------------------------------------------------------- project ---

@dataclass
class FLProject:
    title: str = ""
    author: str = ""
    genre: str = ""
    comment: str = ""
    url: str = ""
    tempo: float = 120.0
    numerator: int = 4
    denominator: int = 4
    ppq: int = 96
    main_volume: int = 12800
    channels: List[FLChannel] = field(default_factory=list)
    patterns: List[FLPattern] = field(default_factory=list)
    items: List[FLPlaylistItem] = field(default_factory=list)
    tracks: Dict[int, FLTrack] = field(default_factory=dict)
    markers: List[FLTimeMarker] = field(default_factory=list)
    inserts: List[FLInsert] = field(default_factory=lambda: [FLInsert() for _ in range(NUM_INSERTS)])
    arrangement_name: str = "Arrangement"

    # -- helpers ---------------------------------------------------------------
    def beats_to_ticks(self, beats: float) -> int:
        return int(round(beats * self.ppq))

    @staticmethod
    def _delphi_now() -> float:
        delta = _dt.datetime.now() - _dt.datetime(1899, 12, 30)
        return delta.days + delta.seconds / 86400.0

    # -- serialisation -------------------------------------------------------------
    def to_bytes(self) -> bytes:
        ev = EventStream()
        ev.ascii(199, FL_VERSION)
        ev.dword(159, FL_BUILD)
        ev.byte(37, 1)
        ev.data(200, b"\x00\x00")                        # licensee (none)
        ev.dword(156, int(round(self.tempo * 1000)))
        ev.word(67, 0)                                   # current pattern
        ev.byte(9, 1)                                    # loop active
        ev.byte(11, 0)                                   # shuffle
        ev.word(80, 0)                                   # main pitch
        ev.byte(17, self.numerator)
        ev.byte(18, self.denominator)
        ev.byte(35, 1)
        ev.byte(23, 0)                                   # pan law
        ev.byte(30, 0)                                   # play truncated notes
        ev.byte(10, 0)                                   # show info
        ev.text(194, self.title)
        ev.text(206, self.genre)
        ev.text(207, self.author)
        ev.text(202, "")                                 # project data path
        ev.text(195, self.comment)
        if self.url:
            ev.text(197, self.url)
        ev.data(237, struct.pack("<dd", self._delphi_now(), 0.0))
        ev.dword(146, 0xFFFFFFFF)                        # current display group (all)
        for g in DISPLAY_GROUPS:
            ev.text(231, g)
        ev.data(216, b"")                                # channel init values

        for i, pat in enumerate(self.patterns, start=1):
            ev.word(65, i)
            if pat.notes:
                ev.data(224, b"".join(n.pack() for n in sorted(pat.notes, key=lambda n: (n.pos, n.rack, n.key))))
            if pat.color is not None:
                ev.dword(150, pat.color)
            ev.text(193, pat.name)

        # Default MIDI remote-control links present in every FL project.
        ev.data(226, b"\x01\x00\x00\x00\x00\x00\x00\x00\x01\x90\xff\x0f\x04\x00\x00\x00\xd5\x01\x00\x00")
        ev.data(226, b"\xfd\x00\x00\x00\x00\x00\x00\x00\x80\x90\xff\x0f\x04\x00\x00\x00\xd5\x01\x00\x00")
        ev.data(226, b"\xff\x00\x00\x00\xff\x00\x00\x00\x04\x00\xff\x0f\x04\x00\x00\x00\x00\xfe\xff\xff")

        for idx, ch in enumerate(self.channels):
            ch.write(ev, idx)

        # Arrangement
        ev.word(99, 0)
        ev.text(241, self.arrangement_name)
        ev.byte(36, 0)
        items = sorted(self.items, key=lambda it: (it.position, it.track))
        ev.data(233, b"".join(it.pack() for it in items))
        for t in range(1, MAX_TRACKS + 1):
            trk = self.tracks.get(t, FLTrack())
            ev.data(238, trk.pack(t))
            if trk.name:
                ev.text(239, trk.name)
        for m in sorted(self.markers, key=lambda m: m.position):
            ev.dword(148, (m.mtype << 24) | (m.position & 0x00FFFFFF))
            ev.byte(33, m.numerator)
            ev.byte(34, m.denominator)
            ev.text(205, m.name or " ")
        ev.word(100, 0)                                  # current arrangement
        ev.byte(29, 1)
        ev.byte(39, 1)
        ev.byte(31, 0)
        ev.byte(38, 1)

        # Mixer
        for i, ins in enumerate(self.inserts):
            ev.data(236, ins.flags_bytes())
            if ins.color is not None:
                ev.dword(149, ins.color)
            if ins.name:
                ev.text(204, ins.name)
            if ins.icon is not None:
                ev.word(95, ins.icon)
            for slot in range(10):
                ev.word(98, slot)
            routing = bytearray(NUM_INSERTS)
            for target in ins.routes:
                if 0 <= target < NUM_INSERTS:
                    routing[target] = 1
            ev.data(235, bytes(routing))
            ev.dword(154, ins.input & 0xFFFFFFFF)
            ev.dword(147, ins.output & 0xFFFFFFFF)

        params = bytearray()
        params += _mixer_param(0, 4 << 12, max(0, min(16000, self.main_volume)))   # main volume
        for i, ins in enumerate(self.inserts):
            params += insert_param_records(i, ins)
        ev.data(225, bytes(params))
        ev.dword(133, 216)                               # window height

        header = b"FLhd" + struct.pack("<IHHH", 6, 0, len(self.channels), self.ppq)
        data = b"FLdt" + struct.pack("<I", len(ev.buf)) + bytes(ev.buf)
        return header + data

    def save(self, path: str) -> None:
        with open(path, "wb") as fh:
            fh.write(self.to_bytes())
