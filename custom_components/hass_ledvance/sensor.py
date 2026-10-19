"""Sensor platform for Ledvance/Tuya integration.

Two categories:
  - Diagnostic sensors (all devices)     — device ID, IP, MAC, local key, firmware version
  - Power monitoring sensors (socket_strip) — current, voltage, power, energy
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DPS_CUR_CURRENT,
    DPS_CUR_POWER,
    DPS_CUR_VOLTAGE,
    DPS_TOTAL_ENERGY,
)
from .coordinator import CoordinatorDeviceData, LedvanceTuyaCoordinator, build_device_info
from .schema_parser import get_numeric_scale

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Diagnostic sensors (text, all device types)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LedvanceDiagSensorDescription(SensorEntityDescription):
    """Diagnostic sensor with a data-accessor callback."""

    value_fn: Callable[[CoordinatorDeviceData], str | None]


DIAG_SENSOR_DESCRIPTIONS: tuple[LedvanceDiagSensorDescription, ...] = (
    # Device ID, MAC and firmware version are now surfaced in the HA device info
    # panel (serial number, connections, sw_version) — no need to duplicate here.
    LedvanceDiagSensorDescription(
        key="lan_ip_address",
        name="LAN IP Address",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.lan_ip,
    ),
    LedvanceDiagSensorDescription(
        key="cloud_ip_address",
        name="Cloud IP Address",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.ip,
    ),
    LedvanceDiagSensorDescription(
        key="local_key",
        name="Local Key",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda d: d.local_key or None,
    ),
    LedvanceDiagSensorDescription(
        key="protocol_version",
        name="Protocol Version",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d.version,
    ),
)


# ---------------------------------------------------------------------------
# Power monitoring sensors (socket_strip devices only)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LedvancePowerSensorDescription(SensorEntityDescription):
    """Power-monitoring sensor: DPS code + optional default scale divisor."""

    dps_code: str
    default_scale: float = 1.0  # fallback if schema has no 'scale' property


POWER_SENSOR_DESCRIPTIONS: tuple[LedvancePowerSensorDescription, ...] = (
    LedvancePowerSensorDescription(
        key="current",
        name="Current",
        dps_code=DPS_CUR_CURRENT,
        default_scale=1000.0,  # Tuya typically reports mA
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        suggested_display_precision=3,
    ),
    LedvancePowerSensorDescription(
        key="voltage",
        name="Voltage",
        dps_code=DPS_CUR_VOLTAGE,
        default_scale=10.0,  # Tuya typically reports 0.1 V steps
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        suggested_display_precision=1,
    ),
    LedvancePowerSensorDescription(
        key="power",
        name="Power",
        dps_code=DPS_CUR_POWER,
        default_scale=10.0,  # Tuya typically reports 0.1 W steps
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=1,
    ),
    LedvancePowerSensorDescription(
        key="energy",
        name="Energy",
        dps_code=DPS_TOTAL_ENERGY,
        default_scale=1000.0,  # Tuya typically reports Wh; HA wants kWh
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        suggested_display_precision=3,
    ),
)


# ---------------------------------------------------------------------------
# Setup
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Ledvance/Tuya sensor entities."""
    coordinator: LedvanceTuyaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    for dev_id, dev in coordinator.data.items():
        # Diagnostic sensors for every device
        for desc in DIAG_SENSOR_DESCRIPTIONS:
            entities.append(LedvanceDiagSensor(coordinator, entry, dev_id, desc))

        # Power monitoring sensors for socket strips and smart plugs with power DPS
        if dev.device_type in ("socket_strip", "switch"):
            dps_codes_in_schema = {item.get("code") for item in dev.schema}
            for desc in POWER_SENSOR_DESCRIPTIONS:
                if desc.dps_code in dps_codes_in_schema:
                    entities.append(LedvancePowerSensor(coordinator, entry, dev_id, desc))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------


class LedvanceDiagSensor(CoordinatorEntity[LedvanceTuyaCoordinator], SensorEntity):
    """A diagnostic (text) sensor for any Ledvance/Tuya device."""

    entity_description: LedvanceDiagSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LedvanceTuyaCoordinator,
        entry: ConfigEntry,
        device_id: str,
        description: LedvanceDiagSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"
        self._attr_device_info = build_device_info(coordinator.data[device_id])

    @property
    def _device_data(self) -> CoordinatorDeviceData:
        return self.coordinator.data[self._device_id]

    @property
    def native_value(self) -> str | None:
        return self.entity_description.value_fn(self._device_data)


class LedvancePowerSensor(CoordinatorEntity[LedvanceTuyaCoordinator], SensorEntity):
    """A power-monitoring sensor on a Ledvance/Tuya socket strip."""

    entity_description: LedvancePowerSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LedvanceTuyaCoordinator,
        entry: ConfigEntry,
        device_id: str,
        description: LedvancePowerSensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_{description.key}"

        dev = coordinator.data[device_id]
        self._attr_device_info = build_device_info(dev)
        self._dps: str | None = dev.dps_map.get(description.dps_code)

        # Prefer the scale from the device schema; fall back to the description default
        schema_scale = get_numeric_scale(dev.schema, description.dps_code)
        self._scale = schema_scale if schema_scale != 1.0 else description.default_scale

    @property
    def _device_data(self) -> CoordinatorDeviceData:
        return self.coordinator.data[self._device_id]

    @property
    def native_value(self) -> float | None:
        if self._dps is None:
            return None
        raw = self._device_data.dps.get(self._dps)
        if raw is None:
            return None
        try:
            return round(float(raw) / self._scale, 6)
        except (TypeError, ValueError):
            return None
