"""Intermediate project model shared by the Ableton reader and the FLP writer.

Everything time-related is stored in *beats* (quarter notes) as floats, exactly
like Ableton does internally.  The FLP writer converts to ticks at write time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Note:
    time: float          # beats, relative to clip start
    duration: float      # beats
    key: int             # MIDI key 0..127
    velocity: int        # 1..127
    off_velocity: int = 64
    enabled: bool = True


@dataclass
class MidiClip:
    name: str
    start: float                 # arrangement position in beats
    end: float                   # arrangement end in beats
    loop_start: float
    loop_end: float
    start_relative: float
    loop_on: bool
    notes: List[Note]
    color: Optional[int] = None  # Ableton colour index
    disabled: bool = False


@dataclass
class WarpMarker:
    sec: float
    beat: float


@dataclass
class AudioClip:
    name: str
    start: float
    end: float
    loop_start: float
    loop_end: float
    start_relative: float
    loop_on: bool
    path: str
    warped: bool
    warp_mode: int
    warp_markers: List[WarpMarker]
    sample_duration_sec: float
    gain: float = 1.0            # linear
    pitch_semitones: float = 0.0
    color: Optional[int] = None
    disabled: bool = False
    reversed: bool = False


@dataclass
class VstInfo:
    kind: str                     # 'vst2' | 'vst3' | 'au'
    name: str
    vendor: str = ""
    fourcc: Optional[int] = None  # VST2 unique id
    guid: Optional[bytes] = None  # VST3 class id (16 bytes)
    path: str = ""
    is_instrument: bool = True
    state_chunk: Optional[bytes] = None   # VST2 raw chunk (chunk mode)
    state_params: Optional[List[Tuple[str, List[float]]]] = None  # VST2 param mode programs
    program: int = 0


@dataclass
class DrumPad:
    name: str
    receiving_note: int
    sending_note: int
    sample_path: Optional[str]
    root_key: int = 60
    volume: float = 1.0
    pan: float = 0.0
    muted: bool = False


@dataclass
class Instrument:
    """Describes what generates sound on a MIDI track."""
    kind: str                        # 'sampler' | 'vst' | 'drumrack' | 'native' | 'none'
    display_name: str = ""
    sample_path: Optional[str] = None
    root_key: int = 60
    vst: Optional[VstInfo] = None
    pads: List[DrumPad] = field(default_factory=list)
    native_device: str = ""


@dataclass
class Send:
    return_index: int
    amount: float                # linear 0..1
    enabled: bool = True


@dataclass
class Track:
    kind: str                    # 'midi' | 'audio' | 'return' | 'group'
    id: int
    name: str
    color: Optional[int]
    volume: float                # linear gain
    pan: float                   # -1..1
    muted: bool
    group_id: int                # Ableton TrackGroupId or -1
    sends: List[Send] = field(default_factory=list)
    midi_clips: List[MidiClip] = field(default_factory=list)
    session_midi_clips: List[MidiClip] = field(default_factory=list)
    audio_clips: List[AudioClip] = field(default_factory=list)
    session_audio_clips: List[AudioClip] = field(default_factory=list)
    instrument: Optional[Instrument] = None
    devices: List[str] = field(default_factory=list)   # device chain tag names (for the report)


@dataclass
class Locator:
    time: float
    name: str


@dataclass
class TimeSigChange:
    time: float
    numerator: int
    denominator: int


@dataclass
class Project:
    title: str
    source_path: str
    creator: str
    tempo: float
    numerator: int
    denominator: int
    tracks: List[Track]
    locators: List[Locator] = field(default_factory=list)
    time_sig_changes: List[TimeSigChange] = field(default_factory=list)
    tempo_automated: bool = False
    loop_start: float = 0.0
    loop_length: float = 0.0
    master_volume: float = 1.0
