"""Project model (from Ableton) -> FLProject (FL Studio)."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .colors import FL_DEFAULT_CHANNEL_COLOR, FL_DEFAULT_TRACK_COLOR, live_color_to_fl
from .flp_writer import (
    CH_AUDIOCLIP,
    CH_PLUGIN,
    CH_SAMPLER,
    MARKER_PLAIN,
    MARKER_TIMESIG,
    MAX_TRACKS,
    MAX_USER_INSERT,
    STRETCH_E3_GENERIC,
    STRETCH_RESAMPLE,
    STRETCH_SLICE,
    STRETCH_UNITS_PER_BEAT,
    FLChannel,
    FLInsert,
    FLNote,
    FLPattern,
    FLPlaylistItem,
    FLProject,
    FLTimeMarker,
    FLTrack,
    FLVstWrapper,
    vst2_state_from_chunk,
    vst2_state_from_params,
)
from .model import AudioClip, Instrument, MidiClip, Note, Project, Track

# FL Studio fader curves (empirical): mixer insert 12800 = 0 dB, 16000 = +5.6 dB;
# channel volume 10000 (78 %) = -5.2 dB, 12800 = 0 dB.
MIXER_CURVE = 2.89
CHANNEL_CURVE = 2.41

# Ableton WarpMode -> FL stretch mode
WARP_MODE_MAP = {
    0: STRETCH_SLICE,        # Beats
    1: STRETCH_E3_GENERIC,   # Tones
    2: STRETCH_E3_GENERIC,   # Texture
    3: STRETCH_RESAMPLE,     # Re-Pitch
    4: STRETCH_E3_GENERIC,   # Complex
    5: STRETCH_E3_GENERIC,   # Complex Pro
    6: STRETCH_E3_GENERIC,   # REX
}


@dataclass
class Options:
    ppq: int = 96
    include_session_clips: bool = True
    path_map: List[Tuple[str, str]] = field(default_factory=list)
    path_transform: Optional[Callable[[str], str]] = None
    author: str = ""


@dataclass
class Report:
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    channels: int = 0
    patterns: int = 0
    playlist_items: int = 0
    inserts: int = 0
    tracks: int = 0
    dropped_notes: int = 0
    missing_samples: List[str] = field(default_factory=list)

    def warn(self, msg: str) -> None:
        if msg not in self.warnings:
            self.warnings.append(msg)


# --------------------------------------------------------------- helpers ---

def gain_to_mixer(gain: float) -> int:
    if gain <= 0:
        return 0
    return int(round(12800 * gain ** (1.0 / MIXER_CURVE)))


def gain_to_channel(gain: float, base: int = 10000) -> int:
    if gain <= 0:
        return 0
    return int(round(base * gain ** (1.0 / CHANNEL_CURVE)))


def pan_to_mixer(pan: float) -> int:
    return int(round(max(-1.0, min(1.0, pan)) * 6400))


def pan_to_channel(pan: float) -> int:
    return int(round((max(-1.0, min(1.0, pan)) + 1.0) * 6400))


def unroll(start: float, end: float, loop_start: float, loop_end: float,
           start_relative: float, loop_on: bool):
    """Yield (arrangement_offset, internal_from, internal_to) windows for a clip.

    ``arrangement_offset`` is relative to the clip start.  Each window maps the
    clip-internal range [internal_from, internal_to) onto the arrangement.
    """
    length = end - start
    if length <= 0:
        return
    play_from = loop_start + start_relative
    if not loop_on or loop_end - loop_start <= 1e-9:
        yield 0.0, play_from, play_from + length
        return
    loop_len = loop_end - loop_start
    offset = 0.0
    internal = play_from
    remaining = length
    guard = 0
    while remaining > 1e-9 and guard < 100000:
        guard += 1
        seg = min(loop_end - internal, remaining)
        if seg <= 1e-9:
            internal = loop_start
            continue
        yield offset, internal, internal + seg
        offset += seg
        remaining -= seg
        internal = loop_start


def unroll_notes(clip: MidiClip, notes: List[Note]) -> List[Tuple[float, float, Note]]:
    """Return (time_in_clip, duration, note) for every note occurrence in the clip window."""
    out = []
    for offset, ifrom, ito in unroll(clip.start, clip.end, clip.loop_start, clip.loop_end,
                                     clip.start_relative, clip.loop_on):
        for n in notes:
            if not n.enabled:
                continue
            if n.time < ifrom - 1e-9 or n.time >= ito - 1e-9:
                continue
            t = offset + (n.time - ifrom)
            dur = min(n.duration, ito - n.time)
            if dur <= 0:
                continue
            out.append((t, dur, n))
    return out


class WarpMap:
    """Piecewise-linear beat <-> second mapping from Ableton warp markers."""

    def __init__(self, clip: AudioClip, tempo: float):
        self.tempo = tempo
        pts = [(m.beat, m.sec) for m in clip.warp_markers]
        pts = sorted(set(pts))
        if not clip.warped or len(pts) < 2:
            spb = 60.0 / tempo
            self.pts = [(0.0, 0.0), (1.0, spb)]
            self.uniform = True
        else:
            self.pts = pts
            self.uniform = False

    def beat_to_sec(self, b: float) -> float:
        pts = self.pts
        if b <= pts[0][0]:
            (b0, s0), (b1, s1) = pts[0], pts[1]
        elif b >= pts[-1][0]:
            (b0, s0), (b1, s1) = pts[-2], pts[-1]
        else:
            for i in range(len(pts) - 1):
                if pts[i][0] <= b <= pts[i + 1][0]:
                    (b0, s0), (b1, s1) = pts[i], pts[i + 1]
                    break
        if b1 == b0:
            return s0
        return s0 + (b - b0) * (s1 - s0) / (b1 - b0)

    def sec_to_beat(self, s: float) -> float:
        pts = self.pts
        if s <= pts[0][1]:
            (b0, s0), (b1, s1) = pts[0], pts[1]
        elif s >= pts[-1][1]:
            (b0, s0), (b1, s1) = pts[-2], pts[-1]
        else:
            for i in range(len(pts) - 1):
                if pts[i][1] <= s <= pts[i + 1][1]:
                    (b0, s0), (b1, s1) = pts[i], pts[i + 1]
                    break
        if s1 == s0:
            return b0
        return b0 + (s - s0) * (b1 - b0) / (s1 - s0)


# ------------------------------------------------------------- converter ---

class Converter:
    def __init__(self, project: Project, options: Optional[Options] = None):
        self.p = project
        self.o = options or Options()
        self.fl = FLProject(ppq=self.o.ppq)
        self.report = Report()
        self._pattern_cache: Dict[tuple, int] = {}
        self._audio_channel_cache: Dict[tuple, int] = {}
        self._insert_of_track: Dict[int, int] = {}
        self._playlist_track_of: Dict[int, int] = {}

    # -- paths ----------------------------------------------------------------
    def _path(self, path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        if not os.path.exists(path):
            if path not in self.report.missing_samples:
                self.report.missing_samples.append(path)
        out = path
        for old, new in self.o.path_map:
            if out.startswith(old):
                out = new + out[len(old):]
                break
        if self.o.path_transform:
            out = self.o.path_transform(out)
        return out

    # -- main -----------------------------------------------------------------
    def run(self) -> FLProject:
        p, fl = self.p, self.fl
        fl.title = p.title
        fl.author = self.o.author
        fl.tempo = p.tempo
        fl.numerator = p.numerator
        fl.denominator = p.denominator
        fl.comment = f"Converted from {os.path.basename(p.source_path)} ({p.creator}) by als2flp."
        fl.main_volume = max(0, min(16000, gain_to_mixer(p.master_volume)))
        for w in getattr(p, "warnings", []):
            self.report.warn(w)

        self._build_mixer()
        self._build_playlist_tracks()
        for t in p.tracks:
            if t.kind == "midi":
                self._convert_midi_track(t)
            elif t.kind == "audio":
                self._convert_audio_track(t)
        self._build_markers()

        r = self.report
        r.channels = len(fl.channels)
        r.patterns = len(fl.patterns)
        r.playlist_items = len(fl.items)
        r.inserts = len(self._insert_of_track)
        r.tracks = len(self._playlist_track_of)
        if not fl.channels:
            # FL needs at least one channel to open a project comfortably.
            fl.channels.append(FLChannel(name="Empty", ctype=CH_SAMPLER))
        return fl

    # -- mixer ----------------------------------------------------------------
    def _build_mixer(self) -> None:
        p, fl = self.p, self.fl
        fl.inserts[0].name = "Master"
        next_insert = 1
        order = [t for t in p.tracks if t.kind in ("midi", "audio", "group")] + \
                [t for t in p.tracks if t.kind == "return"]
        for t in order:
            if next_insert > MAX_USER_INSERT:
                self.report.warn(
                    f"More than {MAX_USER_INSERT} mixer tracks; '{t.name}' routed to Master instead.")
                self._insert_of_track[t.id] = 0
                continue
            idx = next_insert
            next_insert += 1
            self._insert_of_track[t.id] = idx
            ins = fl.inserts[idx]
            ins.name = t.name[:64]
            ins.color = live_color_to_fl(t.color, FL_DEFAULT_CHANNEL_COLOR)
            ins.volume = max(0, min(16000, gain_to_mixer(t.volume)))
            ins.pan = pan_to_mixer(t.pan)
            ins.enabled = not t.muted
        returns = [t for t in p.tracks if t.kind == "return"]
        for t in order:
            idx = self._insert_of_track[t.id]
            if idx == 0:
                continue
            ins = fl.inserts[idx]
            # Output: group track insert if grouped, else master.
            target = 0
            if t.group_id != -1 and t.group_id in self._insert_of_track:
                target = self._insert_of_track[t.group_id]
            ins.routes[target] = 12800
            # Sends to return tracks.
            for s in t.sends:
                if not s.enabled or s.amount <= 0.001 or s.return_index >= len(returns):
                    continue
                r_idx = self._insert_of_track.get(returns[s.return_index].id, 0)
                if r_idx and r_idx != idx:
                    ins.routes[r_idx] = max(0, min(12800, gain_to_mixer(s.amount)))

    # -- playlist tracks ------------------------------------------------------
    def _build_playlist_tracks(self) -> None:
        n = 1
        for t in self.p.tracks:
            if t.kind not in ("midi", "audio"):
                continue
            if n > MAX_TRACKS:
                self.report.warn(f"More than {MAX_TRACKS} playlist tracks; '{t.name}' dropped.")
                continue
            self._playlist_track_of[t.id] = n
            self.fl.tracks[n] = FLTrack(name=t.name, color=live_color_to_fl(t.color, FL_DEFAULT_TRACK_COLOR))
            n += 1

    # -- MIDI tracks ----------------------------------------------------------
    def _add_channel(self, ch: FLChannel) -> int:
        self.fl.channels.append(ch)
        return len(self.fl.channels) - 1

    def _channels_for_instrument(self, t: Track) -> Tuple[Dict[int, int], Optional[int], Dict[int, int]]:
        """Create FL channels for a MIDI track.

        Returns (key_to_channel for drum racks, default channel, key_to_fl_key).
        """
        inst = t.instrument or Instrument(kind="none")
        insert = self._insert_of_track.get(t.id, 0)
        color = live_color_to_fl(t.color)
        key_map: Dict[int, int] = {}
        key_remap: Dict[int, int] = {}

        if inst.kind == "drumrack" and inst.pads:
            for pad in inst.pads:
                ch = FLChannel(
                    name=f"{pad.name}"[:64], ctype=CH_SAMPLER, color=color, insert=insert,
                    sample_path=self._path(pad.sample_path), root_note=60,
                    volume=max(0, min(12800, gain_to_channel(pad.volume))),
                    pan=pan_to_channel(pad.pan), enabled=not pad.muted,
                )
                idx = self._add_channel(ch)
                key_map[pad.receiving_note] = idx
                # Pads play their sample at the root key; move the note there.
                key_remap[pad.receiving_note] = 60 + (pad.sending_note - pad.root_key)
            self.report.notes.append(f"Drum Rack '{inst.display_name}' on '{t.name}': {len(inst.pads)} pads -> sampler channels.")
            return key_map, None, key_remap

        if inst.kind == "sampler":
            ch = FLChannel(name=t.name[:64], ctype=CH_SAMPLER, color=color, insert=insert,
                           sample_path=self._path(inst.sample_path), root_note=inst.root_key)
            return key_map, self._add_channel(ch), key_remap

        if inst.kind == "vst" and inst.vst is not None:
            v = inst.vst
            state = None
            if v.kind == "vst2":
                if v.state_chunk:
                    state = vst2_state_from_chunk(v.state_chunk, v.program, use_program=False)
                elif v.state_params:
                    state = vst2_state_from_params(v.state_params, v.program)
            wrapper = FLVstWrapper(name=v.name, vendor=v.vendor, is_generator=True,
                                   fourcc=v.fourcc if v.kind == "vst2" else None,
                                   guid=v.guid if v.kind == "vst3" else None,
                                   path="", state=state)
            ch = FLChannel(name=f"{v.name}"[:64], ctype=CH_PLUGIN, color=color, insert=insert, plugin=wrapper)
            if v.kind == "vst3":
                self.report.notes.append(f"'{t.name}': VST3 '{v.name}' placed as Fruity Wrapper (plugin identified by name/ID; preset state not transferable).")
            elif v.kind == "au":
                self.report.notes.append(f"'{t.name}': Audio Unit '{v.name}' placed as Fruity Wrapper by name (FL Studio needs the VST version installed; AU state is not transferable).")
            elif state is None:
                self.report.notes.append(f"'{t.name}': VST2 '{v.name}' placed without preset state.")
            else:
                self.report.notes.append(f"'{t.name}': VST2 '{v.name}' placed with preset state.")
            return key_map, self._add_channel(ch), key_remap

        # native Ableton instrument / Max for Live / nothing: placeholder sampler
        label = inst.display_name or inst.native_device or "no instrument"
        ch = FLChannel(name=f"{t.name}"[:64], ctype=CH_SAMPLER, color=color, insert=insert)
        if inst.kind == "native":
            self.report.warn(f"'{t.name}': Ableton device '{label}' has no FL Studio equivalent; notes kept on an empty Sampler channel.")
        return key_map, self._add_channel(ch), key_remap

    def _pattern_for_clip(self, clip: MidiClip, t: Track, key_map: Dict[int, int],
                          default_ch: Optional[int], key_remap: Dict[int, int],
                          name: str) -> Optional[int]:
        ppq = self.o.ppq
        fl_notes: List[FLNote] = []
        for time, dur, n in unroll_notes(clip, clip.notes):
            if n.key in key_map:
                rack = key_map[n.key]
                key = key_remap.get(n.key, 60)
            elif default_ch is not None:
                rack = default_ch
                key = n.key
            else:
                self.report.dropped_notes += 1
                continue
            pos = int(round(time * ppq))
            length = max(1, int(round(dur * ppq)))
            fl_notes.append(FLNote(
                pos=pos, length=length, key=max(0, min(131, key)), rack=rack,
                velocity=max(0, min(128, int(round(n.velocity * 128 / 127)))),
                release=max(0, min(128, int(round(n.off_velocity * 128 / 127)))),
            ))
        if not fl_notes:
            return None
        sig = tuple(sorted((x.pos, x.length, x.key, x.rack, x.velocity, x.release) for x in fl_notes))
        cached = self._pattern_cache.get(sig)
        if cached is not None:
            return cached
        pat = FLPattern(name=name[:64], color=live_color_to_fl(clip.color, None) if clip.color is not None else None,
                        notes=fl_notes)
        self.fl.patterns.append(pat)
        num = len(self.fl.patterns)
        self._pattern_cache[sig] = num
        return num

    def _convert_midi_track(self, t: Track) -> None:
        key_map, default_ch, key_remap = self._channels_for_instrument(t)
        pl_track = self._playlist_track_of.get(t.id)
        ppq = self.o.ppq
        for i, clip in enumerate(t.midi_clips, start=1):
            if clip.end <= clip.start:
                continue
            name = clip.name or f"{t.name} {i}"
            num = self._pattern_for_clip(clip, t, key_map, default_ch, key_remap, name)
            if num is None or pl_track is None:
                continue
            self.fl.items.append(FLPlaylistItem(
                position=int(round(clip.start * ppq)),
                length=max(1, int(round((clip.end - clip.start) * ppq))),
                track=pl_track, pattern=num,
                start_offset=None, end_offset=None, muted=clip.disabled,
            ))
        if self.o.include_session_clips:
            for i, clip in enumerate(t.session_midi_clips, start=1):
                name = f"[Session] {t.name} - {clip.name or i}"
                self._pattern_for_clip(clip, t, key_map, default_ch, key_remap, name)

    # -- audio tracks ---------------------------------------------------------
    def _audio_channel(self, clip: AudioClip, t: Track, warp: WarpMap) -> int:
        insert = self._insert_of_track.get(t.id, 0)
        stretch_mode = STRETCH_RESAMPLE
        stretch_time = 0
        if clip.warped and not warp.uniform and clip.sample_duration_sec > 0:
            total_beats = warp.sec_to_beat(clip.sample_duration_sec) - warp.sec_to_beat(0.0)
            if total_beats > 0:
                stretch_time = int(round(total_beats * STRETCH_UNITS_PER_BEAT))
                stretch_mode = WARP_MODE_MAP.get(clip.warp_mode, STRETCH_E3_GENERIC)
        key = (clip.path, stretch_mode, stretch_time, round(clip.gain, 4), round(clip.pitch_semitones, 3), insert)
        cached = self._audio_channel_cache.get(key)
        if cached is not None:
            return cached
        name = clip.name or os.path.splitext(os.path.basename(clip.path))[0]
        ch = FLChannel(
            name=name[:64], ctype=CH_AUDIOCLIP, color=live_color_to_fl(clip.color if clip.color is not None else t.color),
            insert=insert, sample_path=self._path(clip.path),
            volume=max(0, min(12800, gain_to_channel(clip.gain))),
            stretch_mode=stretch_mode, stretch_time=stretch_time,
            stretch_pitch_cents=int(round(clip.pitch_semitones * 100)),
        )
        idx = self._add_channel(ch)
        self._audio_channel_cache[key] = idx
        return idx

    def _convert_audio_track(self, t: Track) -> None:
        pl_track = self._playlist_track_of.get(t.id)
        ppq = self.o.ppq
        for clip in t.audio_clips:
            if clip.end <= clip.start or pl_track is None:
                continue
            warp = WarpMap(clip, self.p.tempo)
            ch = self._audio_channel(clip, t, warp)
            for offset, ifrom, ito in unroll(clip.start, clip.end, clip.loop_start, clip.loop_end,
                                             clip.start_relative, clip.loop_on):
                self.fl.items.append(FLPlaylistItem(
                    position=int(round((clip.start + offset) * ppq)),
                    length=max(1, int(round((ito - ifrom) * ppq))),
                    track=pl_track, channel=ch,
                    start_offset=max(0.0, warp.beat_to_sec(ifrom) * 1000.0),
                    end_offset=max(0.0, warp.beat_to_sec(ito) * 1000.0),
                    muted=clip.disabled,
                ))
        if self.o.include_session_clips:
            for clip in t.session_audio_clips:
                self._audio_channel(clip, t, WarpMap(clip, self.p.tempo))

    # -- markers --------------------------------------------------------------
    def _build_markers(self) -> None:
        ppq = self.o.ppq
        for loc in self.p.locators:
            self.fl.markers.append(FLTimeMarker(position=int(round(loc.time * ppq)), name=loc.name or "Marker",
                                                mtype=MARKER_PLAIN, numerator=self.p.numerator,
                                                denominator=self.p.denominator))
        for ts in self.p.time_sig_changes:
            self.fl.markers.append(FLTimeMarker(position=int(round(ts.time * ppq)), name=f"{ts.numerator}/{ts.denominator}",
                                                mtype=MARKER_TIMESIG, numerator=ts.numerator, denominator=ts.denominator))


def convert_project(project: Project, options: Optional[Options] = None) -> Tuple[FLProject, Report]:
    c = Converter(project, options)
    fl = c.run()
    return fl, c.report
