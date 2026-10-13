"""Tiny dependency-free .flp reader used for verification and debugging.

It understands the event framing and the handful of structures als2flp
writes, which is enough to check a converted project without FL Studio.

    python -m als2flp.flp_inspect song.flp
"""

from __future__ import annotations

import struct
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

PATTERN_BASE = 20480


def _read_varint(buf: bytes, pos: int) -> Tuple[int, int]:
    n, shift = 0, 0
    while True:
        b = buf[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7


def read_events(data: bytes):
    """Yield (event_id, value) for every event in an .flp file's bytes."""
    if data[:4] != b"FLhd":
        raise ValueError("not an FLP file (missing FLhd)")
    hdr_size = struct.unpack_from("<I", data, 4)[0]
    fmt, nchan, ppq = struct.unpack_from("<HHH", data, 8)
    pos = 8 + hdr_size
    if data[pos:pos + 4] != b"FLdt":
        raise ValueError("missing FLdt chunk")
    size = struct.unpack_from("<I", data, pos + 4)[0]
    pos += 8
    end = pos + size
    if end != len(data):
        raise ValueError(f"FLdt size mismatch: {end} != {len(data)}")
    header = {"format": fmt, "channels": nchan, "ppq": ppq}
    events = []
    while pos < end:
        eid = data[pos]
        pos += 1
        if eid < 64:
            val = data[pos]
            pos += 1
        elif eid < 128:
            val = struct.unpack_from("<H", data, pos)[0]
            pos += 2
        elif eid < 192:
            val = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        else:
            ln, pos = _read_varint(data, pos)
            val = data[pos:pos + ln]
            pos += ln

        events.append((eid, val))
    return header, events


def _utf16(b: bytes) -> str:
    return b.decode("utf-16-le", "replace").rstrip("\x00")


@dataclass
class Summary:
    header: Dict[str, int]
    version: str = ""
    tempo: float = 0.0
    numerator: int = 4
    denominator: int = 4
    title: str = ""
    channels: List[Dict] = field(default_factory=list)
    patterns: List[Dict] = field(default_factory=list)
    items: List[Dict] = field(default_factory=list)
    tracks: Dict[int, str] = field(default_factory=dict)
    inserts: List[Dict] = field(default_factory=list)
    markers: List[Dict] = field(default_factory=list)
    mixer_params: List[Tuple[int, int, int]] = field(default_factory=list)


def summarize(data: bytes) -> Summary:
    header, events = read_events(data)
    s = Summary(header=header)
    cur_chan = cur_pat = None
    in_mixer = False
    for eid, val in events:
        if eid == 199:
            s.version = val.split(b"\x00")[0].decode("ascii", "replace")
        elif eid == 156:
            s.tempo = val / 1000.0
        elif eid == 17:
            s.numerator = val
        elif eid == 18:
            s.denominator = val
        elif eid == 194:
            s.title = _utf16(val)
        elif eid == 65:
            cur_pat = {"index": val, "name": "", "notes": []}
            s.patterns.append(cur_pat)
        elif eid == 193 and cur_pat is not None:
            cur_pat["name"] = _utf16(val)
        elif eid == 224 and cur_pat is not None:
            for off in range(0, len(val) - 23, 24):
                p, fl, rack, ln, key, grp, finep, _u, rel, mch, pan, vel, mx, my = struct.unpack_from("<IHHIHHBBBBBBBB", val, off)
                cur_pat["notes"].append({"pos": p, "len": ln, "key": key, "rack": rack, "vel": vel, "rel": rel})
        elif eid == 64:
            cur_chan = {"index": val, "name": "", "type": None, "insert": 0, "sample": None,
                        "plugin": None, "volume": None, "pan": None, "stretch_time": None, "stretch_mode": None}
            s.channels.append(cur_chan)
            in_mixer = False
        elif eid == 236:
            in_mixer = True
            s.inserts.append({"index": len(s.inserts), "name": "", "routes": [], "enabled": bool(struct.unpack_from("<I", val, 4)[0] & 8)})
        elif in_mixer:
            ins = s.inserts[-1]
            if eid == 204:
                ins["name"] = _utf16(val)
            elif eid == 235:
                ins["routes"] = [i for i, b in enumerate(val) if b]
        elif cur_chan is not None:
            if eid == 21:
                cur_chan["type"] = val
            elif eid == 203:
                cur_chan["name"] = _utf16(val)
            elif eid == 22:
                cur_chan["insert"] = val
            elif eid == 196:
                cur_chan["sample"] = _utf16(val)
            elif eid == 201:
                cur_chan["plugin"] = _utf16(val)
            elif eid == 219:
                pan, vol = struct.unpack_from("<II", val, 0)
                cur_chan["volume"], cur_chan["pan"] = vol, pan
            elif eid == 215 and len(val) >= 112:
                st, sp, sm, smode = struct.unpack_from("<Iiii", val, 96)
                cur_chan["stretch_time"], cur_chan["stretch_mode"] = st, smode
        if eid == 233:
            for off in range(0, len(val) - 31, 32):
                p, base, idx, ln, trk, _u1, flags, _u2 = struct.unpack_from("<IHHIIHHI", val, off)
                item = {"pos": p, "len": ln, "track": 500 - trk, "muted": flags != 64}
                if idx >= PATTERN_BASE:
                    item["pattern"] = idx - PATTERN_BASE
                else:
                    item["channel"] = idx
                    item["start_ms"], item["end_ms"] = struct.unpack_from("<ff", val, off + 24)
                s.items.append(item)
        elif eid == 238:
            cur_track = struct.unpack_from("<I", val, 0)[0]
        elif eid == 239:
            s.tracks[cur_track] = _utf16(val)
        elif eid == 148:
            s.markers.append({"pos": val & 0xFFFFFF, "type": val >> 24, "name": ""})
        elif eid == 205 and s.markers:
            s.markers[-1]["name"] = _utf16(val)
        elif eid == 225:
            for off in range(0, len(val) - 11, 12):
                ctrl, grp, v = struct.unpack_from("<HHi", val, off + 4)
                s.mixer_params.append((ctrl, grp, v))
    return s


def insert_volume(s: Summary, insert: int):
    grp = (2 << 12) + (insert << 6)
    ctrl = (31 << 8) + (12 << 4)
    for c, g, v in s.mixer_params:
        if c == ctrl and g == grp:
            return v
    return None


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m als2flp.flp_inspect FILE.flp [...]")
        return 2
    for path in argv:
        with open(path, "rb") as fh:
            s = summarize(fh.read())
        print(f"{path}: FL {s.version}, {s.tempo:g} BPM, {s.numerator}/{s.denominator}, ppq {s.header['ppq']}, title {s.title!r}")
        print(f"  {len(s.channels)} channels, {len(s.patterns)} patterns, {len(s.items)} playlist items, "
              f"{len(s.tracks)} named tracks, {sum(1 for i in s.inserts if i['name'])} named inserts, {len(s.markers)} markers")
        for ch in s.channels:
            kind = {0: "Sampler", 2: "Plugin", 4: "AudioClip", 5: "Automation"}.get(ch["type"], ch["type"])
            extra = ch["sample"] or ch["plugin"] or ""
            print(f"  ch {ch['index']:3d} {kind:9s} insert {ch['insert']:3d} vol {ch['volume']} {ch['name']!r} {extra}")
        for p in s.patterns:
            print(f"  pattern {p['index']:3d} {p['name']!r}: {len(p['notes'])} notes")
        for it in s.items[:50]:
            tgt = f"pattern {it['pattern']}" if "pattern" in it else f"channel {it['channel']} ({it['start_ms']:.0f}..{it['end_ms']:.0f} ms)"
            print(f"  item track {it['track']:3d} @ {it['pos']:7d} len {it['len']:6d} -> {tgt}{' [muted]' if it['muted'] else ''}")
        if len(s.items) > 50:
            print(f"  ... {len(s.items) - 50} more items")
        for ins in s.inserts:
            if ins["name"]:
                print(f"  insert {ins['index']:3d} {ins['name']!r} vol {insert_volume(s, ins['index'])} -> {ins['routes']}{'' if ins['enabled'] else ' [muted]'}")
        for m in s.markers:
            print(f"  marker @ {m['pos']} type {m['type']} {m['name']!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
