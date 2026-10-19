"""Binary sensor platform for Ledvance/Tuya integration.

Two categories:
  - Connectivity  (all devices)        — cloud-online status
  - Alarm / fault (socket_strip only)  — overvoltage, overcurrent, overtemperature,
                                         leakage-current fault flag
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    DPS_FAULT,
    DPS_OVERCURRENT_ALARM,
    DPS_OVERTEMP_ALARM,
    DPS_OVERVOLTAGE_ALARM,
    FAULT_LEAKAGE_BIT,
    FAULT_OVERCURRENT_BIT,
    FAULT_OVERTEMP_BIT,
    FAULT_OVERVOLTAGE_BIT,
)
from .coordinator import CoordinatorDeviceData, LedvanceTuyaCoordinator, build_device_info

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Alarm sensor descriptions (socket_strip)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LedvanceAlarmDescription(BinarySensorEntityDescription):
    """Alarm binary sensor with an accessor callback."""

    # Returns True when the alarm condition is active
    is_on_fn: Any  # Callable[[CoordinatorDeviceData], bool | None]


def _dps_flag(dps_code: str):
    """Return a lambda that reads a plain boolean DPS."""

    def _fn(dev: CoordinatorDeviceData) -> bool | None:
        dps_num = dev.dps_map.get(dps_code)
        if dps_num is None:
            return None
        return bool(dev.dps.get(dps_num))

    return _fn


def _fault_bit(bit_mask: int):
    """Return a lambda that tests a specific bit in the 'fault' integer DPS."""

    def _fn(dev: CoordinatorDeviceData) -> bool | None:
        dps_num = dev.dps_map.get(DPS_FAULT)
        if dps_num is None:
            return None
        raw = dev.dps.get(dps_num)
        if raw is None:
            return None
        return bool(int(raw) & bit_mask)

    return _fn


ALARM_DESCRIPTIONS: tuple[LedvanceAlarmDescription, ...] = (
    # Dedicated boolean alarm DPS (present on most devices)
    LedvanceAlarmDescription(
        key="overvoltage_alarm",
        name="Overvoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_dps_flag(DPS_OVERVOLTAGE_ALARM),
    ),
    LedvanceAlarmDescription(
        key="overcurrent_alarm",
        name="Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_dps_flag(DPS_OVERCURRENT_ALARM),
    ),
    LedvanceAlarmDescription(
        key="overtemperature_alarm",
        name="Overtemperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        is_on_fn=_dps_flag(DPS_OVERTEMP_ALARM),
    ),
    # Fault bitmap fallbacks (used when device has a combined 'fault' DPS)
    LedvanceAlarmDescription(
        key="fault_overvoltage",
        name="Fault: Overvoltage",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_fault_bit(FAULT_OVERVOLTAGE_BIT),
    ),
    LedvanceAlarmDescription(
        key="fault_overcurrent",
        name="Fault: Overcurrent",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_fault_bit(FAULT_OVERCURRENT_BIT),
    ),
    LedvanceAlarmDescription(
        key="fault_overtemperature",
        name="Fault: Overtemperature",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_fault_bit(FAULT_OVERTEMP_BIT),
    ),
    LedvanceAlarmDescription(
        key="fault_leakage",
        name="Fault: Leakage Current",
        device_class=BinarySensorDeviceClass.PROBLEM,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        is_on_fn=_fault_bit(FAULT_LEAKAGE_BIT),
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
    """Set up Ledvance/Tuya binary sensor entities."""
    coordinator: LedvanceTuyaCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[BinarySensorEntity] = []

    for dev_id, dev in coordinator.data.items():
        # Cloud connectivity — every device
        entities.append(LedvanceTuyaOnlineSensor(coordinator, entry, dev_id))

        # Alarm / fault sensors — socket strip devices only
        if dev.device_type == "socket_strip":
            schema_codes = {item.get("code") for item in dev.schema}
            for desc in ALARM_DESCRIPTIONS:
                # Add dedicated-DPS alarms only if that DPS is in the schema;
                # add fault-bitmap alarms only if the 'fault' DPS is present.
                is_fault_bit = desc.key.startswith("fault_")
                if is_fault_bit:
                    if DPS_FAULT in schema_codes:
                        entities.append(LedvanceAlarmSensor(coordinator, entry, dev_id, desc))
                else:
                    dps_code_map = {
                        "overvoltage_alarm": DPS_OVERVOLTAGE_ALARM,
                        "overcurrent_alarm": DPS_OVERCURRENT_ALARM,
                        "overtemperature_alarm": DPS_OVERTEMP_ALARM,
                    }
                    if dps_code_map.get(desc.key, desc.key) in schema_codes:
                        entities.append(LedvanceAlarmSensor(coordinator, entry, dev_id, desc))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Entity classes
# ---------------------------------------------------------------------------


class LedvanceTuyaOnlineSensor(CoordinatorEntity[LedvanceTuyaCoordinator], BinarySensorEntity):
    """Reports whether a Ledvance/Tuya device is reachable in the cloud."""

    _attr_has_entity_name = True
    _attr_name = "Online"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: LedvanceTuyaCoordinator,
        entry: ConfigEntry,
        device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = f"{entry.entry_id}_{device_id}_online"
        self._attr_device_info = build_device_info(coordinator.data[device_id])

    @property
    def _device_data(self) -> CoordinatorDeviceData:
        return self.coordinator.data[self._device_id]

    @property
    def is_on(self) -> bool:
        return self._device_data.is_online


class LedvanceAlarmSensor(CoordinatorEntity[LedvanceTuyaCoordinator], BinarySensorEntity):
    """An alarm / fault binary sensor for a Ledvance/Tuya socket strip."""

    entity_description: LedvanceAlarmDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LedvanceTuyaCoordinator,
        entry: ConfigEntry,
        device_id: str,
        description: LedvanceAlarmDescription,
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
    def is_on(self) -> bool | None:
        return self.entity_description.is_on_fn(self._device_data)
