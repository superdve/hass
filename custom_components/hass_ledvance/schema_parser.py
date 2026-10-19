"""Parse Tuya device schema to detect entity type and DPS number mappings."""

from __future__ import annotations

import json

from .const import KNOWN_PRODUCT_TYPES

# DPS codes that indicate a light device
_LIGHT_CODES = frozenset(
    {
        "bright_value",
        "bright_value_v2",
        "colour_data",
        "colour_data_v2",
        "temp_value",
        "work_mode",
        "mix_rgbcw",
    }
)


def detect_device_type(schema: list[dict], product_id: str) -> str:
    """Return 'light', 'socket_strip', 'switch', or 'unknown' for a device.

    Detection order:
    1. Schema-based: presence of known light/socket-strip/switch DPS codes
    2. Product ID table fallback
    """
    codes = {item.get("code", "") for item in schema}

    # Light detection: any light-specific DPS present
    if codes & _LIGHT_CODES:
        return "light"

    # Socket strip: requires multiple numbered outlets (switch_1, switch_2, …)
    # A single smart plug with power monitoring DPS is still just a "switch".
    numbered_switches = [c for c in codes if c.startswith("switch_") and c[7:].isdigit()]
    if len(numbered_switches) > 1:
        return "socket_strip"

    # Single switch device
    if any(c.startswith("switch") for c in codes):
        return "switch"

    # Product ID fallback
    return KNOWN_PRODUCT_TYPES.get(product_id, "unknown")


def get_dps_map(schema: list[dict]) -> dict[str, str]:
    """Return a mapping of DPS code name → DPS number string.

    E.g. {"switch_led": "1", "bright_value": "3", "temp_value": "4"}

    The schema item format from the Tuya API is:
      {"id": 1, "code": "switch_led", "type": "Boolean", "mode": "rw", ...}
    """
    result: dict[str, str] = {}
    for item in schema:
        code = item.get("code")
        dps_id = item.get("id")
        if code and dps_id is not None:
            result[code] = str(dps_id)
    return result


def get_schema_property(schema: list[dict], code: str) -> dict | None:
    """Return the schema entry for a specific DPS code, or None."""
    for item in schema:
        if item.get("code") == code:
            return item
    return None


def get_integer_range(schema: list[dict], code: str) -> tuple[int, int]:
    """Return (min, max) for an Integer-type DPS, or (0, 1000) as default."""
    item = get_schema_property(schema, code)
    if item is None:
        return (0, 1000)
    try:
        prop = item.get("property", {})
        if isinstance(prop, str):
            prop = json.loads(prop)
        return (int(prop.get("min", 0)), int(prop.get("max", 1000)))
    except (ValueError, TypeError, KeyError):
        return (0, 1000)


def get_numeric_scale(schema: list[dict], code: str) -> float:
    """Return the divisor to convert raw Tuya integer to real-world value.

    Tuya encodes sensor values as integers where the 'scale' property gives
    the number of decimal places (i.e. raw / 10**scale = real value).
    E.g. scale=1 → divide by 10 (0.1 V steps), scale=3 → divide by 1000 (mA → A).
    """
    item = get_schema_property(schema, code)
    if item is None:
        return 1.0
    try:
        prop = item.get("property", {})
        if isinstance(prop, str):
            prop = json.loads(prop)
        return 10.0 ** int(prop.get("scale", 0))
    except (ValueError, TypeError):
        return 1.0


def get_enum_range(schema: list[dict], code: str) -> list[str]:
    """Return list of valid enum values for an Enum-type DPS, or []."""
    item = get_schema_property(schema, code)
    if item is None:
        return []
    try:
        prop = item.get("property", {})
        if isinstance(prop, str):
            prop = json.loads(prop)
        return list(prop.get("range", []))
    except (ValueError, TypeError):
        return []


def get_socket_outlet_dps(schema: list[dict]) -> list[tuple[str, str]]:
    """Return [(dps_code, label), ...] for every controllable outlet on a socket strip.

    Order: numbered outlets (switch_1, switch_2, …), then USB ports,
    then an "all outlets" master if present.
    """
    codes = {item.get("code", "") for item in schema}
    result: list[tuple[str, str]] = []

    # Numbered outlets: switch_1, switch_2, …
    numbered = sorted(
        (c for c in codes if c.startswith("switch_") and c[7:].isdigit()),
        key=lambda c: int(c[7:]),
    )
    for code in numbered:
        result.append((code, f"Outlet {code[7:]}"))

    # USB ports
    if "usb_switch" in codes:
        result.append(("usb_switch", "USB"))
    for i in range(1, 5):
        usb_code = f"switch_usb{i}"
        if usb_code in codes:
            result.append((usb_code, f"USB {i}"))

    # Master / all-outlets switch
    for master_code in ("master_switch", "switch_all"):
        if master_code in codes:
            result.append((master_code, "All Outlets"))
            break

    return result


def has_hardware_master(schema: list[dict]) -> bool:
    """Return True if the schema contains a hardware master-switch DPS code."""
    codes = {item.get("code") for item in schema}
    return bool(codes & {"master_switch", "switch_all"})
