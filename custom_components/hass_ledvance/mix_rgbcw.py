"""Tuya version-zero mixed-light RAW packet codec.

DP51 layout: version (byte), channel flags (byte), then five big-endian
uint16 values: hue, saturation, RGB value, white brightness, temperature.
See Tuya's ceiling-light function definition, DP51 (mix_rgbcw).
"""

from __future__ import annotations

import base64
import binascii
import struct
from dataclasses import dataclass


@dataclass(frozen=True)
class MixedLightState:
    """Validated state of independent white and colour light channels."""

    flags: int
    hue: int
    saturation: int
    colour_brightness: int
    white_brightness: int
    temperature: int

    @property
    def white_on(self) -> bool:
        return bool(self.flags & 1)

    @property
    def colour_on(self) -> bool:
        return bool(self.flags & 2)

    def encode(self) -> str:
        return base64.b64encode(
            struct.pack(
                ">BBHHHHH",
                0,
                self.flags,
                self.hue,
                self.saturation,
                self.colour_brightness,
                self.white_brightness,
                self.temperature,
            )
        ).decode("ascii")


def decode_mixed_light(raw: object) -> MixedLightState | None:
    """Accept only the documented version-zero, 12-byte mixed-light packet."""
    if not isinstance(raw, str):
        return None
    try:
        data = base64.b64decode(raw, validate=True)
        if len(data) != 12:
            return None
        version, flags, h, s, v, b, t = struct.unpack(">BBHHHHH", data)
        if version != 0 or flags & ~7 or h > 360 or any(x > 1000 for x in (s, v, b, t)):
            return None
        return MixedLightState(flags, h, s, v, b, t)
    except (ValueError, binascii.Error, struct.error):
        return None
