"""Light platform for Ledvance/Tuya integration."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_EFFECT,
    ATTR_HS_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util.color import (
    color_hs_to_RGB,
    color_RGB_to_hs,
)

from .const import (
    DOMAIN,
    DPS_BRIGHT_VALUE,
    DPS_BRIGHT_VALUE_V2,
    DPS_COLOUR_DATA,
    DPS_COLOUR_DATA_V2,
    DPS_SCENE_DATA,
    DPS_SCENE_DATA_V2,
    DPS_SWITCH_LED,
    DPS_TEMP_VALUE,
    DPS_WORK_MODE,
    HA_COLOUR_TEMP_MAX_KELVIN,
    HA_COLOUR_TEMP_MIN_KELVIN,
    TUYA_BRIGHTNESS_MAX,
    TUYA_BRIGHTNESS_MIN,
    TUYA_COLOUR_TEMP_MAX,
    TUYA_COLOUR_TEMP_MIN,
)
from .coordinator import CoordinatorDeviceData, LedvanceTuyaCoordinator, build_device_info
from .local_control import async_send_command
from .schema_parser import get_enum_range, get_integer_range

# work_mode values that map to HA colour modes rather than effects
_COLOUR_WORK_MODES = {"white", "colour"}

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ledvance/Tuya light entities."""
    coordinator: LedvanceTuyaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities = [
        LedvanceTuyaLight(coordinator, entry, dev_id)
        for dev_id, dev in coordinator.data.items()
        if dev.device_type == "light"
    ]
    _LOGGER.debug("Setting up %d light entities", len(entities))
    async_add_entities(entities)


class LedvanceTuyaLight(CoordinatorEntity[LedvanceTuyaCoordinator], LightEntity):
    """Representation of a Ledvance/Tuya light."""

    _attr_has_entity_name = True
    _attr_name = None  # uses device name

    def __init__(
        self,
        coordinator: LedvanceTuyaCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{device_id}"

        dev = self._device_data
        self._attr_device_info = build_device_info(dev)

        # Determine supported colour modes from schema
        codes = {item.get("code") for item in dev.schema}
        self._switch_dps = dev.dps_map.get(DPS_SWITCH_LED) or next(
            (v for k, v in dev.dps_map.items() if k.startswith("switch")), None
        )
        self._bright_dps = dev.dps_map.get(DPS_BRIGHT_VALUE) or dev.dps_map.get(DPS_BRIGHT_VALUE_V2)
        self._temp_dps = dev.dps_map.get(DPS_TEMP_VALUE)
        self._colour_dps = dev.dps_map.get(DPS_COLOUR_DATA) or dev.dps_map.get(DPS_COLOUR_DATA_V2)
        self._mode_dps = dev.dps_map.get(DPS_WORK_MODE)

        # Determine brightness range from schema
        bright_code = DPS_BRIGHT_VALUE if DPS_BRIGHT_VALUE in codes else DPS_BRIGHT_VALUE_V2
        self._bright_min, self._bright_max = get_integer_range(dev.schema, bright_code)
        self._temp_min, self._temp_max = get_integer_range(dev.schema, DPS_TEMP_VALUE)

        # Build supported colour modes set
        supported: set[ColorMode] = set()
        if self._colour_dps:
            supported.add(ColorMode.HS)
        if self._temp_dps:
            supported.add(ColorMode.COLOR_TEMP)
        if self._bright_dps and not supported:
            supported.add(ColorMode.BRIGHTNESS)
        if not supported:
            supported.add(ColorMode.ONOFF)

        self._attr_supported_color_modes = supported
        # Default colour mode
        if ColorMode.HS in supported:
            self._attr_color_mode = ColorMode.HS
        elif ColorMode.COLOR_TEMP in supported:
            self._attr_color_mode = ColorMode.COLOR_TEMP
        elif ColorMode.BRIGHTNESS in supported:
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_color_mode = ColorMode.ONOFF

        self._attr_min_color_temp_kelvin = HA_COLOUR_TEMP_MIN_KELVIN
        self._attr_max_color_temp_kelvin = HA_COLOUR_TEMP_MAX_KELVIN

        # Scene / effect support
        self._scene_dps = dev.dps_map.get(DPS_SCENE_DATA) or dev.dps_map.get(DPS_SCENE_DATA_V2)
        work_mode_values = get_enum_range(dev.schema, DPS_WORK_MODE)
        effect_modes = [m for m in work_mode_values if m not in _COLOUR_WORK_MODES]
        if effect_modes:
            self._effect_modes: list[str] = effect_modes
            self._attr_effect_list = effect_modes
            self._attr_supported_features = LightEntityFeature.EFFECT
        else:
            self._effect_modes = []

    # ------------------------------------------------------------------

    @property
    def _device_data(self) -> CoordinatorDeviceData:
        return self.coordinator.data[self._device_id]

    @property
    def available(self) -> bool:
        return (
            super().available
            and self._device_id in self.coordinator.data
            and self._device_data.is_online
        )

    @property
    def is_on(self) -> bool | None:
        if self._switch_dps is None:
            return None
        return bool(self._device_data.dps.get(self._switch_dps))

    @property
    def brightness(self) -> int | None:
        if self._bright_dps is None:
            return None
        raw = self._device_data.dps.get(self._bright_dps)
        if raw is None:
            return None
        return self._tuya_to_ha_brightness(int(raw))

    @property
    def color_temp_kelvin(self) -> int | None:
        if self._temp_dps is None:
            return None
        raw = self._device_data.dps.get(self._temp_dps)
        if raw is None:
            return None
        return self._tuya_to_ha_color_temp(int(raw))

    @property
    def hs_color(self) -> tuple[float, float] | None:
        if self._colour_dps is None:
            return None
        raw = self._device_data.dps.get(self._colour_dps)
        if not raw:
            return None
        return self._parse_colour_data(str(raw))

    @property
    def effect(self) -> str | None:
        """Return the active effect (scene/music mode), or None."""
        if not self._effect_modes or self._mode_dps is None:
            return None
        mode = self._device_data.dps.get(self._mode_dps)
        return mode if mode in self._effect_modes else None

    @property
    def color_mode(self) -> ColorMode:
        """Return current colour mode based on work_mode DPS."""
        if self._mode_dps is None:
            return self._attr_color_mode  # type: ignore[return-value]
        mode = self._device_data.dps.get(self._mode_dps)
        if mode == "colour" and ColorMode.HS in self._attr_supported_color_modes:
            return ColorMode.HS
        if mode == "white":
            if ColorMode.COLOR_TEMP in self._attr_supported_color_modes:
                return ColorMode.COLOR_TEMP
            if ColorMode.BRIGHTNESS in self._attr_supported_color_modes:
                return ColorMode.BRIGHTNESS
        return self._attr_color_mode  # type: ignore[return-value]

    # ------------------------------------------------------------------

    async def async_turn_on(self, **kwargs: Any) -> None:
        dps_dict: dict[str, Any] = {}

        if self._switch_dps:
            dps_dict[self._switch_dps] = True

        if ATTR_BRIGHTNESS in kwargs and self._bright_dps:
            dps_dict[self._bright_dps] = self._ha_to_tuya_brightness(kwargs[ATTR_BRIGHTNESS])
            if self._mode_dps:
                dps_dict[self._mode_dps] = "white"

        if ATTR_COLOR_TEMP_KELVIN in kwargs and self._temp_dps:
            dps_dict[self._temp_dps] = self._ha_to_tuya_color_temp(kwargs[ATTR_COLOR_TEMP_KELVIN])
            if self._mode_dps:
                dps_dict[self._mode_dps] = "white"

        if ATTR_HS_COLOR in kwargs and self._colour_dps:
            dps_dict[self._colour_dps] = self._encode_colour_data(kwargs[ATTR_HS_COLOR])
            if self._mode_dps:
                dps_dict[self._mode_dps] = "colour"

        if ATTR_EFFECT in kwargs and self._mode_dps:
            effect = kwargs[ATTR_EFFECT]
            if effect in self._effect_modes:
                dps_dict[self._mode_dps] = effect

        await async_send_command(
            self.hass, self.coordinator.api, self._device_data, dps_dict
        )
        self.coordinator.async_optimistic_update(self._device_id, dps_dict)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs: Any) -> None:
        if self._switch_dps is None:
            return
        dps_dict = {self._switch_dps: False}
        await async_send_command(
            self.hass,
            self.coordinator.api,
            self._device_data,
            dps_dict,
        )
        self.coordinator.async_optimistic_update(self._device_id, dps_dict)
        await self.coordinator.async_request_refresh()

    # ------------------------------------------------------------------
    # Value conversion helpers
    # ------------------------------------------------------------------

    def _tuya_to_ha_brightness(self, value: int) -> int:
        """Map Tuya brightness (e.g. 10-1000) to HA brightness (1-255)."""
        lo, hi = self._bright_min, self._bright_max
        clamped = max(lo, min(hi, value))
        return round((clamped - lo) / (hi - lo) * 254 + 1)

    def _ha_to_tuya_brightness(self, value: int) -> int:
        """Map HA brightness (1-255) to Tuya brightness range."""
        lo, hi = self._bright_min, self._bright_max
        return round((value - 1) / 254 * (hi - lo) + lo)

    def _tuya_to_ha_color_temp(self, value: int) -> int:
        """Map Tuya color temp (0-1000) to HA kelvin (2000-6536).

        Tuya 0 = warmest (low K), Tuya max = coldest (high K).
        """
        lo, hi = self._temp_min, self._temp_max
        ratio = (value - lo) / max(hi - lo, 1)
        return round(
            HA_COLOUR_TEMP_MIN_KELVIN
            + ratio * (HA_COLOUR_TEMP_MAX_KELVIN - HA_COLOUR_TEMP_MIN_KELVIN)
        )

    def _ha_to_tuya_color_temp(self, kelvin: int) -> int:
        """Map HA kelvin to Tuya color temp range."""
        lo, hi = self._temp_min, self._temp_max
        span = max(HA_COLOUR_TEMP_MAX_KELVIN - HA_COLOUR_TEMP_MIN_KELVIN, 1)
        ratio = (kelvin - HA_COLOUR_TEMP_MIN_KELVIN) / span
        ratio = max(0.0, min(1.0, ratio))
        return round(lo + ratio * (hi - lo))

    @staticmethod
    def _parse_colour_data(raw: str) -> tuple[float, float] | None:
        """Parse Tuya colour_data string (HHHHSSSSSSSS or RRGGBB) to HS tuple."""
        raw = raw.strip()
        try:
            if len(raw) == 12:
                # HHHHSSSSSSSS: H in 0-360, S and V in 0-1000
                h = int(raw[0:4], 16)
                s = int(raw[4:8], 16)
                # v = int(raw[8:12], 16)  # not needed for hs_color
                return (float(h), float(s) / 10.0)  # HA saturation 0-100
            if len(raw) == 6:
                # RRGGBB
                r = int(raw[0:2], 16)
                g = int(raw[2:4], 16)
                b = int(raw[4:6], 16)
                return color_RGB_to_hs(r, g, b)
        except (ValueError, IndexError):
            pass
        return None

    @staticmethod
    def _encode_colour_data(hs: tuple[float, float]) -> str:
        """Encode HA HS (hue 0-360, saturation 0-100) to Tuya HHHHSSSSSSSS."""
        h = int(hs[0])
        s = int(hs[1] * 10)  # 0-1000
        v = 1000              # full brightness in colour mode; dim via bright_value
        return f"{h:04X}{s:04X}{v:04X}"
