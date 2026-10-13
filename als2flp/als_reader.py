"""Reader for Ableton Live Set files (.als).

An .als is a gzip-compressed XML document.  This module walks the parts of the
schema that matter for a DAW-to-DAW conversion (tempo, tracks, clips, notes,
samples, instruments, mixer, locators) and produces a :class:`Project`.

Tested against Live 12 sets; the lookups are written defensively so Live 9-11
sets degrade gracefully (missing fields fall back to defaults).
"""

from __future__ import annotations

import gzip
import os
import re
import struct
import xml.etree.ElementTree as ET
from typing import Iterable, List, Optional

from .model import (
    AudioClip,
    DrumPad,
    Instrument,
    Locator,
    MidiClip,
    Note,
    Project,
    Send,
    TimeSigChange,
    Track,
    VstInfo,
    WarpMarker,
)

# Devices Live can put in a chain that generate sound.
NATIVE_INSTRUMENTS = {
    "Operator", "UltraAnalog", "Collision", "Electric", "StringStudio",
    "InstrumentVector", "Drift", "Meld", "InstrumentImpulse", "LoungeLizard",
    "Roar",  # (not an instrument, but harmless if it ever appears first)
}
SAMPLER_DEVICES = {"OriginalSimpler", "MultiSampler"}
RACK_DEVICES = {"InstrumentGroupDevice"}
DRUM_RACK = "DrumGroupDevice"
PLUGIN_DEVICES = {"PluginDevice", "AuPluginDevice"}
M4L_INSTRUMENT = "MxDeviceInstrument"
# Devices that may precede the instrument in a MIDI track chain.
MIDI_EFFECT_PREFIXES = ("Midi", "MIDI")
MIDI_EFFECTS = {
    "MidiArpeggiator", "MidiChord", "MidiNoteLength", "MidiPitcher", "MidiRandom",
    "MidiScale", "MidiVelocity", "MidiEffectGroupDevice", "MxDeviceMidiEffect",
    "MidiNoteEcho", "MidiEnvelopeFollower", "MidiShaper", "MidiCcMacro",
    "ExpressionControl", "NoteExpressionControl", "MidiMonitor",
}

_HEX_WS = re.compile(r"\s+")


# ---------------------------------------------------------------- helpers ---

def _val(el: Optional[ET.Element], path: str, default=None):
    if el is None:
        return default
    x = el.find(path)
    if x is None:
        return default
    v = x.get("Value")
    return default if v is None else v


def _float(el, path, default=0.0) -> float:
    v = _val(el, path)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _int(el, path, default=0) -> int:
    v = _val(el, path)
    if v is None:
        return default
    try:
        return int(float(v))
    except ValueError:
        return default


def _bool(el, path, default=False) -> bool:
    v = _val(el, path)
    if v is None:
        return default
    return str(v).lower() == "true"


def _hex_bytes(el: Optional[ET.Element]) -> Optional[bytes]:
    if el is None or not el.text:
        return None
    txt = _HEX_WS.sub("", el.text)
    if not txt:
        return None
    try:
        return bytes.fromhex(txt)
    except ValueError:
        return None


def _color(el: ET.Element) -> Optional[int]:
    """Live 10+ uses <Color Value="n"/>; Live 9 used <ColorIndex Value="n"/>."""
    for tag in ("Color", "ColorIndex"):
        v = _val(el, tag)
        if v is not None:
            try:
                return int(v)
            except ValueError:
                return None
    return None


def load_xml(path: str) -> ET.Element:
    with open(path, "rb") as fh:
        raw = fh.read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return ET.fromstring(raw)


def decode_time_signature(value: int):
    """Live packs the time signature into one int: numerator-1 + 99*log2(denominator)."""
    numerator = (value % 99) + 1
    denominator = 2 ** (value // 99)
    return numerator, denominator


# ----------------------------------------------------------------- reader ---

class AlsReader:
    def __init__(self, path: str):
        self.path = os.path.abspath(path)
        self.base_dir = os.path.dirname(self.path)
        self.root = load_xml(self.path)
        self.liveset = self.root.find("LiveSet")
        if self.liveset is None:
            raise ValueError("Not an Ableton Live Set: no <LiveSet> element")
        self.warnings: List[str] = []

    # -- public ---------------------------------------------------------------

    def read(self) -> Project:
        main = self._main_track()
        tempo, tempo_automated = self._tempo(main)
        numerator, denominator, sig_changes = self._time_signature(main)

        tracks: List[Track] = []
        tracks_el = self.liveset.find("Tracks")
        if tracks_el is not None:
            for t_el in tracks_el:
                tr = self._track(t_el)
                if tr is not None:
                    tracks.append(tr)

        transport = self.liveset.find("Transport")
        project = Project(
            title=os.path.splitext(os.path.basename(self.path))[0],
            source_path=self.path,
            creator=self.root.get("Creator", "Ableton Live"),
            tempo=tempo,
            numerator=numerator,
            denominator=denominator,
            tracks=tracks,
            locators=self._locators(),
            time_sig_changes=sig_changes,
            tempo_automated=tempo_automated,
            loop_start=_float(transport, "LoopStart", 0.0),
            loop_length=_float(transport, "LoopLength", 0.0),
            master_volume=_float(main, "DeviceChain/Mixer/Volume/Manual", 1.0) if main is not None else 1.0,
        )
        return project

    # -- global ---------------------------------------------------------------

    def _main_track(self) -> Optional[ET.Element]:
        for tag in ("MainTrack", "MasterTrack"):
            el = self.liveset.find(tag)
            if el is not None:
                return el
        return None

    def _automation_events(self, main: ET.Element, target_id: Optional[str]) -> List[ET.Element]:
        if main is None or target_id is None:
            return []
        for env in main.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope"):
            if _val(env, "EnvelopeTarget/PointeeId") == target_id:
                ev = env.find("Automation/Events")
                return list(ev) if ev is not None else []
        return []

    def _tempo(self, main: Optional[ET.Element]):
        tempo = _float(main, "DeviceChain/Mixer/Tempo/Manual", 120.0) if main is not None else 120.0
        automated = False
        if main is not None:
            target = main.find("DeviceChain/Mixer/Tempo/AutomationTarget")
            events = self._automation_events(main, target.get("Id") if target is not None else None)
            real_events = [e for e in events if float(e.get("Time", "0")) >= 0]
            if real_events:
                automated = True
                # Use the value in effect at the very start of the arrangement.
                first = sorted(events, key=lambda e: float(e.get("Time", "0")))[0]
                try:
                    tempo = float(first.get("Value", tempo))
                except ValueError:
                    pass
                self.warnings.append(
                    "Tempo automation found; FL project uses the initial tempo only "
                    f"({tempo:g} BPM). Tempo changes are not converted."
                )
        if tempo <= 0:
            tempo = 120.0
        return tempo, automated

    def _time_signature(self, main: Optional[ET.Element]):
        numerator, denominator = 4, 4
        changes: List[TimeSigChange] = []
        if main is None:
            return numerator, denominator, changes
        mixer = main.find("DeviceChain/Mixer")
        manual = _val(mixer, "TimeSignature/Manual")
        if manual is not None:
            numerator, denominator = decode_time_signature(int(float(manual)))
        else:  # Live <= 10 layout
            rts = mixer.find("TimeSignature/TimeSignatures/RemoteableTimeSignature") if mixer is not None else None
            if rts is not None:
                numerator = _int(rts, "Numerator", 4)
                denominator = _int(rts, "Denominator", 4)
        target = mixer.find("TimeSignature/AutomationTarget") if mixer is not None else None
        for e in self._automation_events(main, target.get("Id") if target is not None else None):
            t = float(e.get("Time", "0"))
            if t < 0:
                continue
            n, d = decode_time_signature(int(float(e.get("Value", "201"))))
            changes.append(TimeSigChange(t, n, d))
        changes.sort(key=lambda c: c.time)
        return numerator, denominator, changes

    def _locators(self) -> List[Locator]:
        out = []
        for loc in self.liveset.findall("Locators/Locators/Locator"):
            out.append(Locator(_float(loc, "Time", 0.0), _val(loc, "Name", "") or ""))
        out.sort(key=lambda l: l.time)
        return out

    # -- tracks ---------------------------------------------------------------

    def _track(self, el: ET.Element) -> Optional[Track]:
        kind = {
            "MidiTrack": "midi",
            "AudioTrack": "audio",
            "ReturnTrack": "return",
            "GroupTrack": "group",
        }.get(el.tag)
        if kind is None:
            return None
        name = _val(el, "Name/EffectiveName") or _val(el, "Name/UserName") or el.tag
        mixer = el.find("DeviceChain/Mixer")
        track = Track(
            kind=kind,
            id=int(el.get("Id", "0")),
            name=name,
            color=_color(el),
            volume=_float(mixer, "Volume/Manual", 1.0),
            pan=_float(mixer, "Pan/Manual", 0.0),
            muted=not _bool(mixer, "Speaker/Manual", True),
            group_id=_int(el, "TrackGroupId", -1),
        )
        # Sends
        for i, holder in enumerate(mixer.findall("Sends/TrackSendHolder") if mixer is not None else []):
            idx = int(holder.get("Id", str(i)))
            track.sends.append(Send(
                return_index=idx,
                amount=_float(holder, "Send/Manual", 0.0),
                enabled=_bool(holder, "EnabledByUser", True),
            ))
        devices = el.find("DeviceChain/DeviceChain/Devices")
        if devices is not None:
            track.devices = [d.tag for d in devices]

        seq = el.find("DeviceChain/MainSequencer")
        if kind == "midi":
            track.instrument = self._instrument(devices, name)
            if seq is not None:
                for c in seq.findall("ClipTimeable/ArrangerAutomation/Events/MidiClip"):
                    track.midi_clips.append(self._midi_clip(c))
                for c in seq.findall("ClipSlotList/ClipSlot/ClipSlot/Value/MidiClip"):
                    track.session_midi_clips.append(self._midi_clip(c))
        elif kind == "audio":
            if seq is not None:
                for c in seq.findall("Sample/ArrangerAutomation/Events/AudioClip"):
                    ac = self._audio_clip(c)
                    if ac is not None:
                        track.audio_clips.append(ac)
                for c in seq.findall("ClipSlotList/ClipSlot/ClipSlot/Value/AudioClip"):
                    ac = self._audio_clip(c)
                    if ac is not None:
                        track.session_audio_clips.append(ac)
        track.midi_clips.sort(key=lambda c: c.start)
        track.audio_clips.sort(key=lambda c: c.start)
        return track

    # -- clips ----------------------------------------------------------------

    def _clip_common(self, c: ET.Element):
        return dict(
            name=_val(c, "Name", "") or "",
            start=_float(c, "CurrentStart", 0.0),
            end=_float(c, "CurrentEnd", 0.0),
            loop_start=_float(c, "Loop/LoopStart", 0.0),
            loop_end=_float(c, "Loop/LoopEnd", 0.0),
            start_relative=_float(c, "Loop/StartRelative", 0.0),
            loop_on=_bool(c, "Loop/LoopOn", False),
            color=_color(c),
            disabled=_bool(c, "Disabled", False),
        )

    def _midi_clip(self, c: ET.Element) -> MidiClip:
        notes: List[Note] = []
        for kt in c.findall("Notes/KeyTracks/KeyTrack"):
            key = _int(kt, "MidiKey", 60)
            for ev in kt.findall("Notes/MidiNoteEvent"):
                enabled = str(ev.get("IsEnabled", "true")).lower() != "false"
                notes.append(Note(
                    time=float(ev.get("Time", "0")),
                    duration=float(ev.get("Duration", "0")),
                    key=key,
                    velocity=int(round(float(ev.get("Velocity", "100")))),
                    off_velocity=int(round(float(ev.get("OffVelocity", "64")))),
                    enabled=enabled,
                ))
        notes.sort(key=lambda n: (n.time, n.key))
        common = self._clip_common(c)
        return MidiClip(notes=notes, **common)

    def _audio_clip(self, c: ET.Element) -> Optional[AudioClip]:
        sref = c.find("SampleRef")
        path = self._file_ref_path(sref.find("FileRef") if sref is not None else None)
        if path is None:
            self.warnings.append(f"Audio clip '{_val(c, 'Name', '')}' has no resolvable sample path; skipped.")
            return None
        markers = [
            WarpMarker(float(m.get("SecTime", "0")), float(m.get("BeatTime", "0")))
            for m in c.findall("WarpMarkers/WarpMarker")
        ]
        markers.sort(key=lambda m: m.beat)
        dur_samples = _float(sref, "DefaultDuration", 0.0)
        rate = _float(sref, "DefaultSampleRate", 44100.0) or 44100.0
        common = self._clip_common(c)
        return AudioClip(
            path=path,
            warped=_bool(c, "IsWarped", False),
            warp_mode=_int(c, "WarpMode", 0),
            warp_markers=markers,
            sample_duration_sec=dur_samples / rate if dur_samples else 0.0,
            gain=_float(c, "SampleVolume", 1.0),
            pitch_semitones=_float(c, "PitchCoarse", 0.0) + _float(c, "PitchFine", 0.0) / 100.0,
            **common,
        )

    def _file_ref_path(self, fr: Optional[ET.Element]) -> Optional[str]:
        if fr is None:
            return None
        p = _val(fr, "Path")
        if p:
            return p
        rel = fr.find("RelativePath")
        if rel is not None and rel.get("Value"):
            return os.path.normpath(os.path.join(self.base_dir, rel.get("Value")))
        # Live <= 10: <RelativePath><RelativePathElement Dir="..."/>...</RelativePath><Name Value="x.wav"/>
        if rel is not None:
            dirs = [e.get("Dir", "") for e in rel.findall("RelativePathElement")]
            name = _val(fr, "Name")
            if name:
                parts = [self.base_dir] + [d if d else ".." for d in dirs] + [name]
                return os.path.normpath(os.path.join(*parts))
        # Last resort: absolute path elements (Live <= 10)
        sp = fr.find("SearchHint/PathHint")
        name = _val(fr, "Name")
        if sp is not None and name:
            dirs = [e.get("Dir", "") for e in sp.findall("RelativePathElement")]
            if dirs:
                return "/" + "/".join(dirs + [name])
        return None

    # -- instruments ----------------------------------------------------------

    def _instrument(self, devices: Optional[ET.Element], track_name: str) -> Instrument:
        if devices is None:
            return Instrument(kind="none")
        for dev in devices:
            inst = self._instrument_from_device(dev, track_name)
            if inst is not None:
                return inst
        return Instrument(kind="none")

    def _instrument_from_device(self, dev: ET.Element, track_name: str) -> Optional[Instrument]:
        tag = dev.tag
        if tag in MIDI_EFFECTS or (tag.startswith(MIDI_EFFECT_PREFIXES) and tag not in NATIVE_INSTRUMENTS):
            return None
        if tag == DRUM_RACK:
            return self._drum_rack(dev)
        if tag in RACK_DEVICES:
            for branch in dev.findall("Branches/InstrumentBranch"):
                chain = branch.find("DeviceChain/MidiToAudioDeviceChain/Devices")
                if chain is None:
                    chain = branch.find("DeviceChain/DeviceChain/Devices")
                if chain is None:
                    continue
                for sub in chain:
                    inst = self._instrument_from_device(sub, track_name)
                    if inst is not None:
                        if not inst.display_name:
                            inst.display_name = _val(dev, "UserName", "") or tag
                        return inst
            return Instrument(kind="native", native_device="InstrumentGroupDevice",
                              display_name=_val(dev, "UserName", "") or "Instrument Rack")
        if tag in SAMPLER_DEVICES:
            path, root = self._sampler_sample(dev)
            return Instrument(kind="sampler", display_name=_val(dev, "UserName", "") or tag,
                              sample_path=path, root_key=root)
        if tag in PLUGIN_DEVICES:
            vst = self._plugin_info(dev)
            if vst is None:
                return Instrument(kind="native", native_device=tag, display_name="Unknown plugin")
            if not vst.is_instrument:
                return None  # an effect plugin in the chain before/after the instrument
            return Instrument(kind="vst", display_name=vst.name, vst=vst)
        if tag == M4L_INSTRUMENT or tag in NATIVE_INSTRUMENTS:
            return Instrument(kind="native", native_device=tag,
                              display_name=_val(dev, "UserName", "") or tag)
        # Unknown device: audio effects live after the instrument, so keep scanning.
        return None

    def _sampler_sample(self, dev: ET.Element):
        part = dev.find("Player/MultiSampleMap/SampleParts/MultiSamplePart")
        if part is None:
            return None, 60
        path = self._file_ref_path(part.find("SampleRef/FileRef"))
        root = _int(part, "RootKey", 60)
        return path, root

    def _drum_rack(self, dev: ET.Element) -> Instrument:
        inst = Instrument(kind="drumrack", display_name=_val(dev, "UserName", "") or "Drum Rack")
        for br in dev.findall("Branches/DrumBranch"):
            recv_raw = _int(br, "BranchInfo/ReceivingNote", -1)
            send = _int(br, "BranchInfo/SendingNote", 60)
            if recv_raw < 0:
                continue
            # Live stores the pad's input note inverted: 128 - MIDI key (92 -> C1 = 36).
            recv = 128 - recv_raw
            if not 0 <= recv <= 127:
                continue
            name = _val(br, "Name/EffectiveName") or _val(br, "Name/UserName") or f"Pad {recv}"
            chain = br.find("DeviceChain/MidiToAudioDeviceChain/Devices")
            if chain is None:
                chain = br.find("DeviceChain/DeviceChain/Devices")
            sample_path, root = None, 60
            if chain is not None:
                sample_path, root = self._find_sample_in_chain(chain)
            mix = br.find("MixerDevice")
            inst.pads.append(DrumPad(
                name=name,
                receiving_note=recv,
                sending_note=send,
                sample_path=sample_path,
                root_key=root,
                volume=_float(mix, "Volume/Manual", 1.0),
                pan=_float(mix, "Pan/Manual", 0.0),
                muted=not _bool(mix, "Speaker/Manual", True),
            ))
        inst.pads.sort(key=lambda p: p.receiving_note)
        return inst

    def _find_sample_in_chain(self, chain: ET.Element):
        for dev in chain:
            if dev.tag in SAMPLER_DEVICES:
                return self._sampler_sample(dev)
            if dev.tag in RACK_DEVICES or dev.tag == DRUM_RACK:
                for br in dev.findall("Branches/*"):
                    sub = br.find("DeviceChain/MidiToAudioDeviceChain/Devices")
                    if sub is None:
                        sub = br.find("DeviceChain/DeviceChain/Devices")
                    if sub is not None:
                        p, r = self._find_sample_in_chain(sub)
                        if p:
                            return p, r
        return None, 60

    def _plugin_info(self, dev: ET.Element) -> Optional[VstInfo]:
        desc = dev.find("PluginDesc")
        if desc is None:
            return None
        v2 = desc.find("VstPluginInfo")
        v3 = desc.find("Vst3PluginInfo")
        au = desc.find("AuPluginInfo")
        if v2 is not None:
            name = _val(v2, "PlugName") or _val(v2, "Name") or "VST plugin"
            flags = _int(v2, "Flags", 0)
            category = _int(v2, "Category", 0)
            num_params = _int(v2, "NumberOfParameters", 0)
            info = VstInfo(
                kind="vst2", name=name, vendor=_val(v2, "Manufacturer", "") or "",
                fourcc=_int(v2, "UniqueId", 0) or None, path=_val(v2, "Path", "") or "",
                is_instrument=(category == 2) or _int(v2, "NumAudioInputs", 0) == 0,
            )
            preset = v2.find("Preset/VstPreset")
            if preset is not None:
                info.program = _int(preset, "ProgramNumber", 0)
                buf = _hex_bytes(preset.find("Buffer"))
                if buf:
                    if flags & (1 << 10):      # kEffFlagsProgramChunks
                        info.state_chunk = buf
                    elif num_params > 0:
                        info.state_params = _decode_vst2_programs(buf, num_params)
            return info
        if v3 is not None:
            name = _val(v3, "Name") or "VST3 plugin"
            fields = []
            for i in range(4):
                f = _val(v3, f"Uid/Fields.{i}")
                if f is None:
                    break
                fields.append(int(f) & 0xFFFFFFFF)
            guid = struct.pack(">4I", *fields) if len(fields) == 4 else None
            dtype = _int(v3, "DeviceType", 1)
            return VstInfo(
                kind="vst3", name=name, vendor=_val(v3, "Manufacturer", "") or "",
                guid=guid, is_instrument=(dtype == 1) or _int(v3, "NumAudioInputs", 0) == 0,
            )
        if au is not None:
            name = _val(au, "Name") or "AU plugin"
            ctype = _int(au, "ComponentType", 0)
            # 'aumu' = 0x61756D75 music device (instrument)
            return VstInfo(
                kind="au", name=name, vendor=_val(au, "Manufacturer", "") or "",
                is_instrument=(ctype == 0x61756D75) or _int(au, "NumAudioInputs", 0) == 0,
            )
        return None


def _decode_vst2_programs(buf: bytes, num_params: int):
    """Live stores non-chunk VST2 state as an array of (name[28], float[num_params])."""
    rec = 28 + 4 * num_params
    programs = []
    for off in range(0, len(buf) - rec + 1, rec):
        name = buf[off:off + 28].split(b"\x00", 1)[0].decode("latin-1", "replace")
        vals = list(struct.unpack_from(f"<{num_params}f", buf, off + 28))
        programs.append((name, vals))
    return programs or None


def read_als(path: str) -> Project:
    """Convenience wrapper: parse a Live Set into a Project (warnings attached)."""
    r = AlsReader(path)
    p = r.read()
    p.warnings = list(r.warnings)  # type: ignore[attr-defined]
    return p


def iter_sample_paths(project: Project) -> Iterable[str]:
    for t in project.tracks:
        for c in t.audio_clips + t.session_audio_clips:
            yield c.path
        if t.instrument is not None:
            if t.instrument.sample_path:
                yield t.instrument.sample_path
            for pad in t.instrument.pads:
                if pad.sample_path:
                    yield pad.sample_path
