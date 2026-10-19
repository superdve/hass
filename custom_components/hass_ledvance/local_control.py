"""Local device control via tinytuya with cloud fallback."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .api import TuyaAPI
from .coordinator import CoordinatorDeviceData, LedvanceTuyaCoordinator
from .device_io import device_lock

_LOGGER = logging.getLogger(__name__)

_LOCAL_TIMEOUT = 3  # seconds to wait for local response


async def async_send_command(
    hass: HomeAssistant,
    api: TuyaAPI,
    device: CoordinatorDeviceData,
    dps_dict: dict,
) -> bool:
    """Send a DPS command, trying local first and falling back to cloud.

    dps_dict uses DPS number strings as keys, e.g. {"1": True, "3": 500}.
    Returns True if the command was accepted.
    """
    if not dps_dict:
        return False
    if device.lan_ip and device.local_key and device.version:
        try:
            success = await hass.async_add_executor_job(
                _send_local,
                device.device_id,
                device.lan_ip,
                device.local_key,
                device.version,
                dps_dict,
            )
            if success:
                _LOGGER.debug(
                    "Local command sent to %s (%s): %s",
                    device.name,
                    device.lan_ip,
                    dps_dict,
                )
                return True
            _LOGGER.debug("Local command failed for %s — falling back to cloud", device.name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Local control error for %s (%s): %s — falling back to cloud",
                device.name,
                device.lan_ip,
                exc,
            )

    # Cloud fallback
    try:
        result = await hass.async_add_executor_job(
            api.set_dps,
            device.device_id,
            device.gateway_id,
            dps_dict,
        )
        if result is False:
            return False
        _LOGGER.debug("Cloud command sent to %s: %s", device.name, dps_dict)
        return True
    except Exception as exc:  # noqa: BLE001
        _LOGGER.error("Cloud command failed for %s: %s", device.name, exc)
        return False


async def async_send_and_update(
    hass: HomeAssistant,
    coordinator: LedvanceTuyaCoordinator,
    device_id: str,
    dps: dict | Callable[[CoordinatorDeviceData], dict],
) -> None:
    """Update HA only after an accepted command; serialize concurrent writes."""
    async with coordinator.command_lock(device_id):
        device = coordinator.data[device_id]
        patch = dps(device) if callable(dps) else dps
        if not await async_send_command(hass, coordinator.api, device, patch):
            raise HomeAssistantError(f"{device.name} did not accept the command over LAN or cloud")
        coordinator.async_optimistic_update(device_id, patch)
        await coordinator.async_request_refresh()


def _send_local(
    device_id: str,
    ip: str,
    local_key: str,
    version: str,
    dps_dict: dict,
) -> bool:
    """Synchronous: send command to device over LAN via tinytuya."""
    try:
        import tinytuya  # noqa: PLC0415
    except ImportError:
        return False

    with device_lock(device_id):
        device = None
        try:
            device = tinytuya.OutletDevice(
                dev_id=device_id,
                address=ip,
                local_key=local_key,
                version=float(version),
            )
            device.set_socketTimeout(_LOCAL_TIMEOUT)
            result = device.set_multiple_values(dps_dict, nowait=False)
            # A successful ACK may be None or an empty dict.
            if isinstance(result, dict) and "Error" in result:
                _LOGGER.debug("tinytuya local error: %s", result["Error"])
                return False
            return result is not False
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("tinytuya exception: %s", type(exc).__name__)
            return False
        finally:
            if device is not None:
                with suppress(Exception):
                    device.close()
