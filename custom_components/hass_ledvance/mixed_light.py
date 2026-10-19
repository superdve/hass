"""Independent white/RGB entities for validated mix_rgbcw devices."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DPS_SWITCH_LED, HA_COLOUR_TEMP_MAX_KELVIN, HA_COLOUR_TEMP_MIN_KELVIN
from .coordinator import CoordinatorDeviceData, LedvanceTuyaCoordinator, build_device_info
from .local_control import async_send_and_update
from .mix_rgbcw import MixedLightState, decode_mixed_light
from .schema_parser import get_schema_property


def create_mixed_lights(
    coordinator: LedvanceTuyaCoordinator,
    entry: ConfigEntry,
    device_id: str,
) -> list[LightEntity]:
    """Use the RAW protocol only when schema and live payload both match."""
    dev = coordinator.data[device_id]
    dp = dev.dps_map.get("mix_rgbcw")
    item = get_schema_property(dev.schema, "mix_rgbcw")
    if not dp or not item or str(item.get("type", "")).lower() != "raw":
        return []
    if "w" not in str(item.get("mode", "rw")).lower():
        return []
    if decode_mixed_light(dev.dps.get(dp)) is None:
        return []
    return [
        LedvanceMixedLight(coordinator, entry, device_id, dp, channel)
        for channel in ("combined", "white", "colour")
    ]


class LedvanceMixedLight(CoordinatorEntity[LedvanceTuyaCoordinator], LightEntity):
    """Combined power or one independent channel of a mixed light."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, device_id: str, mixed_dp: str, channel: str) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._mixed_dp = mixed_dp
        self._channel = channel
        dev = coordinator.data[device_id]
        base_id = f"{entry.entry_id}_{device_id}"
        self._attr_unique_id = base_id if channel == "combined" else f"{base_id}_{channel}"
        self._attr_name = {"combined": None, "white": "Main light", "colour": "Secondary light"}[
            channel
        ]
        self._attr_device_info = build_device_info(dev)
        self._switch_dp = dev.dps_map.get(DPS_SWITCH_LED)
        mode = {"combined": ColorMode.ONOFF, "white": ColorMode.COLOR_TEMP, "colour": ColorMode.HS}[
            channel
        ]
        self._attr_supported_color_modes = {mode}
        self._attr_color_mode = mode
        self._attr_min_color_temp_kelvin = HA_COLOUR_TEMP_MIN_KELVIN
        self._attr_max_color_temp_kelvin = HA_COLOUR_TEMP_MAX_KELVIN

    @property
    def _device_data(self) -> CoordinatorDeviceData:
        return self.coordinator.data[self._device_id]

    @property
    def _state(self) -> MixedLightState | None:
        return decode_mixed_light(self._device_data.dps.get(self._mixed_dp))

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._device_id in self.coordinator.data
            and self._device_data.is_online
            and self._state is not None
        )

    @property
    def is_on(self) -> bool | None:
        if (state := self._state) is None:
            return None
        if self._switch_dp and not self._device_data.dps.get(self._switch_dp):
            return False
        if self._channel == "white":
            return state.white_on
        if self._channel == "colour":
            return state.colour_on
        return state.white_on or state.colour_on

    @property
    def brightness(self) -> int | None:
        if self._channel == "combined" or (state := self._state) is None:
            return None
        value = state.white_brightness if self._channel == "white" else state.colour_brightness
        return round(max(0, value - 10) / 990 * 254 + 1)

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if self._channel != "colour" or (state := self._state) is None:
            return None
        return float(state.hue), state.saturation / 10

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._channel != "white" or (state := self._state) is None:
            return None
        return round(
            HA_COLOUR_TEMP_MIN_KELVIN
            + state.temperature / 1000 * (HA_COLOUR_TEMP_MAX_KELVIN - HA_COLOUR_TEMP_MIN_KELVIN)
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._async_set(True, kwargs)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._async_set(False, {})

    async def _async_set(self, on: bool, options: dict) -> None:
        def build(dev: CoordinatorDeviceData) -> dict:
            state = decode_mixed_light(dev.dps.get(self._mixed_dp))
            if state is None:
                raise HomeAssistantError("The device returned an unsupported mix_rgbcw payload")
            # Run inside the command lock, so simultaneous channel updates do
            # not overwrite one another with a stale copy of the other channel.
            mask = {"combined": 3, "white": 1, "colour": 2}[self._channel]
            flags = ((state.flags | mask) if on else (state.flags & ~mask)) | 4
            patch: dict[str, int] = {"flags": flags}
            if on:
                if self._channel in ("combined", "white") and not state.white_brightness:
                    patch["white_brightness"] = 1000
                if self._channel in ("combined", "colour") and not state.colour_brightness:
                    patch["colour_brightness"] = 1000
            if ATTR_BRIGHTNESS in options and self._channel != "combined":
                key = "white_brightness" if self._channel == "white" else "colour_brightness"
                patch[key] = round(
                    (max(1, min(255, options[ATTR_BRIGHTNESS])) - 1) / 254 * 990 + 10
                )
            if ATTR_HS_COLOR in options and self._channel == "colour":
                h, s = options[ATTR_HS_COLOR]
                patch.update(
                    hue=round(max(0, min(360, h))), saturation=round(max(0, min(100, s)) * 10)
                )
            if ATTR_COLOR_TEMP_KELVIN in options and self._channel == "white":
                ratio = (options[ATTR_COLOR_TEMP_KELVIN] - HA_COLOUR_TEMP_MIN_KELVIN) / (
                    HA_COLOUR_TEMP_MAX_KELVIN - HA_COLOUR_TEMP_MIN_KELVIN
                )
                patch["temperature"] = round(max(0, min(1, ratio)) * 1000)
            updated = replace(state, **patch)
            dps = {self._mixed_dp: updated.encode()}
            if self._switch_dp:
                dps[self._switch_dp] = bool(flags & 3)
            return dps

        await async_send_and_update(self.hass, self.coordinator, self._device_id, build)
