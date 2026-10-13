"""End-to-end tests: build a synthetic Live Set, convert it, read the FLP back."""

from __future__ import annotations

import gzip
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from als2flp.als_reader import AlsReader, decode_time_signature  # noqa: E402
from als2flp.cli import main as cli_main  # noqa: E402
from als2flp.convert import Options, convert_project, unroll  # noqa: E402
from als2flp.flp_inspect import insert_volume, summarize  # noqa: E402


def _note(time, dur, vel=100):
    return f'<MidiNoteEvent Time="{time}" Duration="{dur}" Velocity="{vel}" OffVelocity="64" IsEnabled="true" NoteId="1"/>'


def _midi_clip(start, end, loop_start, loop_end, start_rel, loop_on, keytracks, name="Clip"):
    kt = ""
    for key, notes in keytracks.items():
        kt += f'<KeyTrack Id="{key}"><Notes>{"".join(notes)}</Notes><MidiKey Value="{key}"/></KeyTrack>'
    return f"""
<MidiClip Id="1">
  <CurrentStart Value="{start}"/><CurrentEnd Value="{end}"/>
  <Loop><LoopStart Value="{loop_start}"/><LoopEnd Value="{loop_end}"/><StartRelative Value="{start_rel}"/>
        <LoopOn Value="{'true' if loop_on else 'false'}"/><OutMarker Value="{loop_end}"/></Loop>
  <Name Value="{name}"/><Color Value="14"/><Disabled Value="false"/>
  <Notes><KeyTracks>{kt}</KeyTracks></Notes>
</MidiClip>"""


def _audio_clip(start, end, path, loop_start=0.0, loop_end=8.0, warped=True, warp_mode=4):
    return f"""
<AudioClip Id="2">
  <CurrentStart Value="{start}"/><CurrentEnd Value="{end}"/>
  <Loop><LoopStart Value="{loop_start}"/><LoopEnd Value="{loop_end}"/><StartRelative Value="0"/>
        <LoopOn Value="false"/><OutMarker Value="{loop_end}"/></Loop>
  <Name Value="Loop"/><Color Value="3"/><Disabled Value="false"/>
  <SampleRef>
    <FileRef><RelativePathType Value="1"/><RelativePath Value="x.wav"/><Path Value="{path}"/><Type Value="2"/></FileRef>
    <DefaultDuration Value="176400"/><DefaultSampleRate Value="44100"/>
  </SampleRef>
  <IsWarped Value="{'true' if warped else 'false'}"/><WarpMode Value="{warp_mode}"/>
  <SampleVolume Value="0.5"/><PitchCoarse Value="2"/><PitchFine Value="50"/>
  <WarpMarkers><WarpMarker Id="0" SecTime="0" BeatTime="0"/><WarpMarker Id="1" SecTime="2" BeatTime="4"/></WarpMarkers>
</AudioClip>"""


def _mixer(vol=1.0, pan=0.0, on=True, sends=(0.0, 0.0)):
    holders = "".join(
        f'<TrackSendHolder Id="{i}"><Send><Manual Value="{v}"/></Send><EnabledByUser Value="true"/></TrackSendHolder>'
        for i, v in enumerate(sends))
    return f"""
<Mixer>
  <Sends>{holders}</Sends>
  <Speaker><Manual Value="{'true' if on else 'false'}"/></Speaker>
  <Pan><Manual Value="{pan}"/></Pan>
  <Volume><Manual Value="{vol}"/></Volume>
  <Tempo><Manual Value="128"/><AutomationTarget Id="8"/></Tempo>
  <TimeSignature><Manual Value="201"/><AutomationTarget Id="10"/></TimeSignature>
</Mixer>"""


def _simpler(path, root=60):
    return f"""
<OriginalSimpler Id="0"><UserName Value="Simp"/>
  <Player><MultiSampleMap><SampleParts>
    <MultiSamplePart Id="0"><RootKey Value="{root}"/>
      <SampleRef><FileRef><Path Value="{path}"/></FileRef></SampleRef>
    </MultiSamplePart>
  </SampleParts></MultiSampleMap></Player>
</OriginalSimpler>"""


def _drum_rack(pads):
    branches = ""
    for key, name, path in pads:
        branches += f"""
<DrumBranch Id="{key}">
  <Name><EffectiveName Value="{name}"/></Name>
  <DeviceChain><MidiToAudioDeviceChain><Devices>{_simpler(path)}</Devices></MidiToAudioDeviceChain></DeviceChain>
  <MixerDevice><Volume><Manual Value="1"/></Volume><Pan><Manual Value="0"/></Pan><Speaker><Manual Value="true"/></Speaker></MixerDevice>
  <BranchInfo><ReceivingNote Value="{128 - key}"/><SendingNote Value="60"/></BranchInfo>
</DrumBranch>"""
    return f'<DrumGroupDevice Id="5"><UserName Value="Kit"/><Branches>{branches}</Branches></DrumGroupDevice>'


def _vst3():
    return """
<PluginDevice Id="7"><PluginDesc><Vst3PluginInfo Id="0">
  <NumAudioInputs Value="0"/><Name Value="Serum 2"/><DeviceType Value="1"/>
  <Uid><Fields.0 Value="1"/><Fields.1 Value="2"/><Fields.2 Value="3"/><Fields.3 Value="4"/></Uid>
</Vst3PluginInfo></PluginDesc></PluginDevice>"""


def _vst2_chunk():
    return """
<PluginDevice Id="8"><PluginDesc><VstPluginInfo Id="0">
  <PlugName Value="Sylenth1"/><UniqueId Value="1400201281"/><NumberOfParameters Value="3"/>
  <Category Value="2"/><Flags Value="1024"/>
  <Preset><VstPreset Id="0"><ProgramNumber Value="0"/><Buffer>DEADBEEF</Buffer></VstPreset></Preset>
</VstPluginInfo></PluginDesc></PluginDevice>"""


def _track(tag, tid, name, devices="", arr_midi="", session_midi="", arr_audio="", color=5, group=-1, **mix):
    seq = ""
    if tag == "MidiTrack":
        seq = f"""<MainSequencer>
          <ClipSlotList><ClipSlot Id="0"><ClipSlot><Value>{session_midi}</Value></ClipSlot></ClipSlot></ClipSlotList>
          <ClipTimeable><ArrangerAutomation><Events>{arr_midi}</Events></ArrangerAutomation></ClipTimeable>
        </MainSequencer>"""
    elif tag == "AudioTrack":
        seq = f"""<MainSequencer>
          <ClipSlotList/>
          <Sample><ArrangerAutomation><Events>{arr_audio}</Events></ArrangerAutomation></Sample>
        </MainSequencer>"""
    return f"""
<{tag} Id="{tid}">
  <Name><EffectiveName Value="{name}"/><UserName Value=""/></Name>
  <Color Value="{color}"/><TrackGroupId Value="{group}"/>
  <DeviceChain>{_mixer(**mix)}{seq}<DeviceChain><Devices>{devices}</Devices></DeviceChain></DeviceChain>
</{tag}>"""


def build_als(tmpdir, sample_path):
    # Clip 1: 2-beat loop repeated over 8 beats, notes on C3 (48) and the kick pad (36).
    clip1 = _midi_clip(0, 8, 0, 2, 0, True, {36: [_note(0, 0.5), _note(1, 0.5)]}, name="Kick loop")
    # Clip 2: unlooped, starts 1 beat into the clip content, one long note cut at clip end.
    clip2 = _midi_clip(16, 20, 0, 8, 1, False, {38: [_note(0, 1), _note(2, 10)]}, name="Snare bit")
    session = _midi_clip(0, 4, 0, 4, 0, True, {36: [_note(0, 1)]}, name="Session only")
    tracks = (
        _track("MidiTrack", 10, "Drums", devices=_drum_rack([(36, "Kick", sample_path), (38, "Snare", sample_path)]),
               arr_midi=clip1 + clip2, session_midi=session, vol=0.5, pan=-0.25, sends=(0.3, 0.0))
        + _track("MidiTrack", 11, "Lead", devices=_vst3(), arr_midi=_midi_clip(0, 4, 0, 4, 0, True, {60: [_note(0, 4, 127)]}, name="Lead"), on=False)
        + _track("MidiTrack", 12, "Bass", devices=_vst2_chunk(), arr_midi=_midi_clip(4, 8, 0, 4, 0, True, {40: [_note(0, 1)]}, name="Bass"), group=20)
        + _track("MidiTrack", 13, "Simp", devices=_simpler(sample_path, 62), arr_midi="")
        + _track("AudioTrack", 14, "Audio", arr_audio=_audio_clip(8, 16, sample_path))
        + _track("GroupTrack", 20, "Group")
        + _track("ReturnTrack", 2, "A-Reverb")
    )
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Ableton MajorVersion="5" MinorVersion="12.0_12300" Creator="Ableton Live 12.3.6">
<LiveSet>
  <Tracks>{tracks}</Tracks>
  <MainTrack>
    <Name><EffectiveName Value="Main"/></Name>
    <AutomationEnvelopes><Envelopes>
      <AutomationEnvelope Id="0"><EnvelopeTarget><PointeeId Value="10"/></EnvelopeTarget>
        <Automation><Events><EnumEvent Id="0" Time="-63072000" Value="201"/><EnumEvent Id="1" Time="16" Value="200"/></Events></Automation>
      </AutomationEnvelope>
    </Envelopes></AutomationEnvelopes>
    <DeviceChain>{_mixer(vol=0.8)}</DeviceChain>
  </MainTrack>
  <Transport><LoopStart Value="0"/><LoopLength Value="16"/></Transport>
  <Locators><Locators>
    <Locator Id="0"><Time Value="8"/><Name Value="Drop"/></Locator>
    <Locator Id="1"><Time Value="0"/><Name Value="Intro"/></Locator>
  </Locators></Locators>
</LiveSet>
</Ableton>"""
    path = os.path.join(tmpdir, "Test Set.als")
    with gzip.open(path, "wb") as fh:
        fh.write(xml.encode("utf-8"))
    return path


class UnrollTests(unittest.TestCase):
    def test_loop_unrolls_to_clip_length(self):
        wins = list(unroll(0, 8, 0, 2, 0, True))
        self.assertEqual(len(wins), 4)
        self.assertEqual(wins[0], (0.0, 0.0, 2.0))
        self.assertEqual(wins[3], (6.0, 0.0, 2.0))

    def test_start_relative_partial_first_window(self):
        wins = list(unroll(0, 5, 0, 2, 0.5, True))
        self.assertEqual(wins[0], (0.0, 0.5, 2.0))
        self.assertAlmostEqual(sum(w[2] - w[1] for w in wins), 5.0)

    def test_unlooped(self):
        self.assertEqual(list(unroll(16, 20, 0, 8, 1, False)), [(0.0, 1.0, 5.0)])

    def test_time_signature_decode(self):
        self.assertEqual(decode_time_signature(201), (4, 4))
        self.assertEqual(decode_time_signature(200), (3, 4))
        self.assertEqual(decode_time_signature(99 * 3 + 6), (7, 8))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sample = os.path.join(self.tmp.name, "kick.wav")
        with open(self.sample, "wb") as fh:
            fh.write(b"RIFF\x00\x00\x00\x00WAVE")
        self.als = build_als(self.tmp.name, self.sample)

    def tearDown(self):
        self.tmp.cleanup()

    def _convert(self, **opts):
        reader = AlsReader(self.als)
        project = reader.read()
        project.warnings = reader.warnings
        fl, report = convert_project(project, Options(**opts))
        data = fl.to_bytes()
        return project, fl, report, summarize(data)

    def test_reader(self):
        project = AlsReader(self.als).read()
        self.assertEqual(project.tempo, 128.0)
        self.assertEqual((project.numerator, project.denominator), (4, 4))
        self.assertEqual([t.name for t in project.tracks], ["Drums", "Lead", "Bass", "Simp", "Audio", "Group", "A-Reverb"])
        drums = project.tracks[0]
        self.assertEqual(drums.instrument.kind, "drumrack")
        self.assertEqual([p.receiving_note for p in drums.instrument.pads], [36, 38])
        self.assertEqual(len(drums.midi_clips), 2)
        self.assertEqual(len(drums.session_midi_clips), 1)
        self.assertEqual(project.tracks[1].instrument.vst.kind, "vst3")
        self.assertEqual(len(project.tracks[1].instrument.vst.guid), 16)
        self.assertEqual(project.tracks[2].instrument.vst.state_chunk, b"\xde\xad\xbe\xef")
        self.assertEqual(project.tracks[3].instrument.root_key, 62)
        self.assertEqual(project.tracks[4].audio_clips[0].pitch_semitones, 2.5)
        self.assertEqual([l.name for l in project.locators], ["Intro", "Drop"])
        self.assertEqual(len(project.time_sig_changes), 1)

    def test_flp_structure(self):
        project, fl, report, s = self._convert()
        self.assertEqual(s.header["ppq"], 96)
        self.assertEqual(s.header["channels"], len(s.channels))
        self.assertEqual(s.tempo, 128.0)
        self.assertEqual(s.title, "Test Set")
        names = [c["name"] for c in s.channels]
        self.assertEqual(names[:2], ["Kick", "Snare"])
        self.assertEqual(s.channels[0]["sample"], self.sample)
        self.assertEqual(s.channels[0]["insert"], 1)
        self.assertEqual(s.channels[2]["type"], 2)          # VST3 -> plugin channel
        self.assertEqual(s.channels[2]["plugin"], "Fruity Wrapper")
        self.assertEqual(s.channels[3]["type"], 2)          # VST2 -> plugin channel
        self.assertEqual(s.channels[4]["name"], "Simp")
        audio = [c for c in s.channels if c["type"] == 4]
        self.assertEqual(len(audio), 1)
        self.assertEqual(audio[0]["sample"], self.sample)
        # 2 s of audio spans 4 beats -> sample of 4 s is 8 beats -> 8 * 192
        self.assertEqual(audio[0]["stretch_time"], 8 * 192)
        self.assertEqual(audio[0]["stretch_mode"], 1)
        # Playlist tracks: Drums, Lead, Bass, Simp, Audio
        self.assertEqual([s.tracks[i] for i in sorted(s.tracks)], ["Drums", "Lead", "Bass", "Simp", "Audio"])
        self.assertEqual([m["name"] for m in s.markers], ["Intro", "Drop", "3/4"])
        self.assertEqual(s.markers[2]["type"], 8)
        self.assertEqual(s.markers[1]["pos"], 8 * 96)

    def test_patterns_and_items(self):
        project, fl, report, s = self._convert()
        by_name = {p["name"]: p for p in s.patterns}
        kick = by_name["Kick loop"]
        # 2 notes per 2-beat loop, 4 repetitions
        self.assertEqual(len(kick["notes"]), 8)
        self.assertEqual(sorted(n["pos"] for n in kick["notes"]), [0, 96, 192, 288, 384, 480, 576, 672])
        self.assertTrue(all(n["rack"] == 0 and n["key"] == 60 for n in kick["notes"]))
        snare = by_name["Snare bit"]
        # starts 1 beat in: note at 0 skipped, note at 2 -> pos 1 beat, cut to 3 beats (clip is 4 beats long)
        self.assertEqual([(n["pos"], n["len"]) for n in snare["notes"]], [(96, 288)])
        self.assertTrue(all(n["rack"] == 1 for n in snare["notes"]))
        self.assertIn("[Session] Drums - Session only", by_name)
        lead = by_name["Lead"]
        self.assertEqual(lead["notes"][0]["vel"], 128)
        self.assertEqual(lead["notes"][0]["key"], 60)
        pat_items = [it for it in s.items if "pattern" in it]
        self.assertEqual(len(pat_items), 4)
        kick_item = [it for it in pat_items if it["pattern"] == kick["index"]][0]
        self.assertEqual((kick_item["pos"], kick_item["len"], kick_item["track"]), (0, 8 * 96, 1))
        audio_items = [it for it in s.items if "channel" in it]
        self.assertEqual(len(audio_items), 1)
        self.assertEqual((audio_items[0]["pos"], audio_items[0]["len"], audio_items[0]["track"]), (8 * 96, 8 * 96, 5))
        self.assertAlmostEqual(audio_items[0]["start_ms"], 0.0)
        self.assertAlmostEqual(audio_items[0]["end_ms"], 4000.0, places=1)

    def test_mixer(self):
        project, fl, report, s = self._convert()
        names = {i["name"]: i for i in s.inserts}
        self.assertEqual(s.inserts[0]["name"], "Master")
        self.assertEqual(names["Drums"]["routes"], [0, 7])      # master + send to A-Reverb (insert 7)
        self.assertEqual(names["Bass"]["routes"], [6])          # grouped -> Group insert
        self.assertEqual(names["Group"]["routes"], [0])
        self.assertFalse(names["Lead"]["enabled"])              # muted track
        self.assertEqual(insert_volume(s, 0), 12800)
        self.assertEqual(insert_volume(s, 1), int(round(12800 * 0.5 ** (1 / 2.89))))
        self.assertEqual(insert_volume(s, 7), 12800)

    def test_path_map_and_no_session(self):
        project, fl, report, s = self._convert(path_map=[(self.tmp.name, "D:\\Samples")], include_session_clips=False)
        self.assertTrue(s.channels[0]["sample"].startswith("D:\\Samples"))
        self.assertFalse(any(p["name"].startswith("[Session]") for p in s.patterns))

    def test_cli_and_copy_samples(self):
        out = os.path.join(self.tmp.name, "out", "Test Set.flp")
        rc = cli_main([self.als, "-o", out, "--copy-samples"])
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(out))
        copied = os.path.join(self.tmp.name, "out", "Test Set_Samples", "kick.wav")
        self.assertTrue(os.path.isfile(copied))
        with open(out, "rb") as fh:
            s = summarize(fh.read())
        self.assertEqual(s.channels[0]["sample"], os.path.abspath(copied))

    def test_pyflp_roundtrip_if_available(self):
        try:
            import pyflp  # noqa: F401
        except Exception:
            self.skipTest("pyflp not installed")
        import warnings
        warnings.simplefilter("ignore")
        out = os.path.join(self.tmp.name, "t.flp")
        cli_main([self.als, "-o", out])
        p = pyflp.parse(out)
        self.assertEqual(p.tempo, 128.0)
        chans = list(p.channels)
        self.assertEqual(chans[0].name, "Kick")
        pats = list(p.patterns)
        self.assertTrue(any(pat.name == "Kick loop" for pat in pats))


if __name__ == "__main__":
    unittest.main()
