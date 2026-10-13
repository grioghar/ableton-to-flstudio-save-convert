"""Ableton Live colour palette (Live 10/11/12, 70 colours) -> FL Studio colour ints.

FL Studio stores colours as a little-endian 32-bit int: R | G << 8 | B << 16.
"""

from __future__ import annotations

from typing import Optional

LIVE_PALETTE = [
    # row 1
    "FF94A6", "FFA529", "CC9927", "F7F47C", "BFFB00", "1AFF2F", "25FFA8", "5CFFE8",
    "8BC5FF", "5480E4", "92A7FF", "D86CE4", "E553A0", "FFFFFF",
    # row 2
    "FF3636", "F66C03", "99724B", "FFF034", "87FF67", "3DC300", "00BFAF", "19E9FF",
    "10A4EE", "007DC0", "886CE4", "B677C6", "FF39D4", "D0D0D0",
    # row 3
    "E2675A", "FFA374", "D3AD71", "EDFFAE", "D2E498", "BAD074", "9BC48D", "D4FDE1",
    "CDF1F8", "B9C1E3", "CDBBE4", "AE98E5", "E5DCE1", "A9A9A9",
    # row 4
    "C6928B", "B78256", "99836A", "BFBA69", "A6BE00", "7DB04D", "88C2BA", "9BB3C4",
    "85A5C2", "8393CC", "A595B5", "BF9FBE", "BC7196", "7B7B7B",
    # row 5
    "AF3333", "A95131", "724F41", "DBC300", "85961F", "539F31", "0A9C8E", "236384",
    "1A2F96", "2F52A2", "624BAD", "A34BAD", "CC2E6E", "3C3C3C",
]

FL_DEFAULT_CHANNEL_COLOR = 0x5A5A5A  # grey-ish, R=G=B=0x5A
FL_DEFAULT_TRACK_COLOR = 0x565148    # FL's default playlist track colour (R=0x48,G=0x51,B=0x56)


def rgb_to_fl(r: int, g: int, b: int) -> int:
    return (r & 0xFF) | ((g & 0xFF) << 8) | ((b & 0xFF) << 16)


def hex_to_fl(hexstr: str) -> int:
    hexstr = hexstr.lstrip("#")
    r, g, b = int(hexstr[0:2], 16), int(hexstr[2:4], 16), int(hexstr[4:6], 16)
    return rgb_to_fl(r, g, b)


def live_color_to_fl(index: Optional[int], default: int = FL_DEFAULT_CHANNEL_COLOR) -> int:
    if index is None:
        return default
    try:
        idx = int(index)
    except (TypeError, ValueError):
        return default
    if 0 <= idx < len(LIVE_PALETTE):
        return hex_to_fl(LIVE_PALETTE[idx])
    return default
