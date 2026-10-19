"""DataUpdateCoordinator for Ledvance/Tuya devices."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import DeviceInfo as TuyaDeviceInfo
from .api import TuyaAPI
from .const import DEFAULT_SCAN_INTERVAL, DOMAIN, LAN_SCAN_INTERVAL
from .device_io import device_lock
from .exceptions import CannotConnect, InvalidAuthentication, TooManyRequests
from .schema_parser import detect_device_type, get_dps_map

_LOGGER = logging.getLogger(__name__)


@dataclass
class CoordinatorDeviceData:
    """All data needed to manage a single device."""

    device_id: str
    gateway_id: str
    name: str
    product_id: str
    device_type: str  # 'light' | 'switch' | 'unknown'
    schema: list
    dps_map: dict[str, str]  # code → dps_number_string
    dps: dict[str, object]  # dps_number_string → value
    local_key: str
    mac: str
    is_online: bool
    ip: str | None = None  # cloud-reported IP (typically WAN) — diagnostic only
    lan_ip: str | None = None  # tinytuya LAN-discovered IP — used for local control
    version: str | None = None  # Tuya protocol version (e.g. "3.3") — used by tinytuya
    fw_version: str | None = None  # firmware/base version (e.g. "40") — shown in device panel
    room_name: str = ""  # Tuya room name; empty if unassigned


# Type alias for the coordinator data dict
DevicesData = dict[str, CoordinatorDeviceData]  # keyed by device_id


def build_device_info(dev: CoordinatorDeviceData) -> DeviceInfo:
    """Build a HA DeviceInfo for a Ledvance/Tuya device.

    Populates the device panel with manufacturer, model, firmware version,
    serial number, and MAC address so they appear like first-party integrations.
    """
    connections: set[tuple[str, str]] = set()
    if dev.mac:
        connections.add((CONNECTION_NETWORK_MAC, dev.mac.lower()))
    return DeviceInfo(
        identifiers={(DOMAIN, dev.device_id)},
        name=dev.name,
        manufacturer="Ledvance / Tuya",
        model=dev.product_id or None,
        serial_number=dev.device_id,
        sw_version=dev.fw_version,
        connections=connections,
    )


class LedvanceTuyaCoordinator(DataUpdateCoordinator[DevicesData]):
    """Polls device states — LAN-first with cloud fallback — and scans for local IPs."""

    def __init__(self, hass: HomeAssistant, api: TuyaAPI) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="Ledvance/Tuya",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.api = api
        self._lan_cache: dict[str, dict] = {}  # device_id → {ip, version}
        self._last_lan_scan: float = 0.0
        self._lan_monitor_task: asyncio.Task | None = None
        self._command_locks: dict[str, asyncio.Lock] = {}

    def command_lock(self, device_id: str) -> asyncio.Lock:
        """Serialize commands, including mixed-channel read/modify/write operations."""
        return self._command_locks.setdefault(device_id, asyncio.Lock())

    # ------------------------------------------------------------------

    async def _async_update_data(self) -> DevicesData:
        """Fetch device states — LAN-first with cloud fallback — and keep LAN cache fresh."""
        # Refresh LAN scan when due (always on first call when cache is empty,
        # then every LAN_SCAN_INTERVAL seconds).  Running it BEFORE the device
        # fetch means local DPS polling is available from the very first update.
        now = time.monotonic()
        if not self._lan_cache or now - self._last_lan_scan >= LAN_SCAN_INTERVAL:
            try:
                scan_result = await self.hass.async_add_executor_job(self._scan_lan)
                self._lan_cache = scan_result
                self._last_lan_scan = now
                _LOGGER.debug("LAN scan found %d devices", len(scan_result))
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("LAN scan failed (non-fatal): %s", exc)

        try:
            devices_data = await self.hass.async_add_executor_job(self._fetch_all_devices)
        except InvalidAuthentication as exc:
            raise UpdateFailed(f"Authentication failed: {exc}") from exc
        except TooManyRequests as exc:
            raise UpdateFailed(f"Tuya API rate limited — will retry next poll: {exc}") from exc
        except CannotConnect as exc:
            raise UpdateFailed(f"Cannot connect to Tuya cloud: {exc}") from exc

        # Start the per-device LAN monitor after the first successful fetch.
        if self._lan_monitor_task is None or self._lan_monitor_task.done():
            self._lan_monitor_task = self.hass.async_create_background_task(
                self._lan_monitor_loop(),
                "ledvance_lan_monitor",
            )

        return devices_data

    def async_optimistic_update(self, device_id: str, dps_patch: dict) -> None:
        """Immediately apply a DPS patch to the cached data without a network round-trip.

        Call this right after sending a command so the UI reflects the intended
        state instantly, avoiding the flicker that would otherwise occur while
        waiting for the next coordinator poll to return the new device state.
        The next real poll will confirm (or correct) the value.
        """
        if self.data is None or device_id not in self.data:
            return
        dev = self.data[device_id]
        patched = replace(dev, dps={**dev.dps, **dps_patch})
        self.async_set_updated_data({**self.data, device_id: patched})

    def _fetch_all_devices(self) -> DevicesData:
        """Synchronous: fetch all devices from all groups."""
        # Re-login if needed (API auto-re-logins on session expiry,
        # but explicit login on first run or after auth error)
        if self.api.sid is None:
            self.api.login()

        result: DevicesData = {}
        try:
            groups = self.api.groups()
        except Exception as exc:
            raise CannotConnect(f"Failed to fetch groups: {exc}") from exc

        for group in groups:
            group_id = str(group.get("groupId") or group.get("locationId") or "")
            if not group_id:
                continue
            try:
                devices: list[TuyaDeviceInfo] = self.api.devices(group_id)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Failed to fetch devices for group %s: %s", group_id, exc)
                continue

            for dev_info in devices:
                # Get fresh DPS — try LAN first, fall back to cloud
                lan = self._lan_cache.get(dev_info.dev_id, {})
                dps = self._poll_dps_local(
                    dev_info.dev_id,
                    lan.get("ip"),
                    dev_info.local_key,
                    lan.get("version"),
                )
                lan_reachable = dps is not None
                if lan_reachable:
                    _LOGGER.debug("DPS for '%s' fetched via LAN", dev_info.name)
                else:
                    try:
                        dps = self.api.get_dps(dev_info.dev_id)
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.warning("Failed to fetch DPS for %s: %s", dev_info.dev_id, exc)
                        dps = dev_info.dps  # last resort: keep previous values

                device_type = detect_device_type(dev_info.schema, dev_info.product_id)
                dps_map = get_dps_map(dev_info.schema)

                # If the device responded on LAN it's definitely online;
                # fall back to cloud's is_online only when LAN isn't available.
                lan_ip = lan.get("ip")
                is_online = lan_reachable or dev_info.is_online

                result[dev_info.dev_id] = CoordinatorDeviceData(
                    device_id=dev_info.dev_id,
                    gateway_id=dev_info.gateway_id,
                    name=dev_info.name,
                    product_id=dev_info.product_id,
                    device_type=device_type,
                    schema=dev_info.schema,
                    dps_map=dps_map,
                    dps=dps,
                    local_key=dev_info.local_key,
                    mac=dev_info.mac,
                    is_online=is_online,
                    ip=dev_info.ip,
                    lan_ip=lan_ip,
                    version=lan.get("version"),  # LAN scan only — cloud pv is unrelated
                    fw_version=dev_info.fw_version,
                    room_name=dev_info.room_name,
                )

        _LOGGER.debug("Fetched %d devices from cloud", len(result))
        return result

    @staticmethod
    def _poll_dps_local(
        device_id: str,
        ip: str | None,
        local_key: str,
        version: str | None,
    ) -> dict | None:
        """Try to read DPS state from a device over LAN via tinytuya.

        Returns a normalised {dps_number_str: value} dict on success,
        or None if the device cannot be reached or tinytuya is unavailable.
        """
        if not (ip and local_key and version):
            return None
        try:
            import tinytuya  # noqa: PLC0415
        except ImportError:
            return None
        with device_lock(device_id):
            d = None
            try:
                d = tinytuya.OutletDevice(
                    dev_id=device_id,
                    address=ip,
                    local_key=local_key,
                    version=float(version),
                )
                d.set_socketTimeout(3)
                status = d.status()
                if isinstance(status, dict) and "Error" not in status:
                    raw = status.get("dps")
                    if isinstance(raw, dict) and raw:
                        return {str(k): v for k, v in raw.items()}
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug(
                    "Local DPS poll failed for %s (%s): %s", device_id, ip, type(exc).__name__
                )
            finally:
                if d is not None:
                    with suppress(Exception):
                        d.close()
        return None

    async def _lan_monitor_loop(self) -> None:
        """Background task: LAN-poll devices one at a time, staggered across a 5-second window.

        Each device is polled roughly every 5 seconds. With N devices the gap
        between polls is 5/N seconds, so only one device is being queried at
        any given moment and the network is never flooded.
        """
        try:
            while True:
                if not self.data:
                    await asyncio.sleep(5)
                    continue

                device_ids = list(self.data.keys())
                n = len(device_ids)
                gap = 5.0 / n if n > 0 else 5.0

                for dev_id in device_ids:
                    if self.data is None:
                        break
                    dev = self.data.get(dev_id)
                    if dev is None:
                        await asyncio.sleep(gap)
                        continue

                    ip = dev.lan_ip
                    version = dev.version

                    if ip and dev.local_key and version:
                        dps = await self.hass.async_add_executor_job(
                            self._poll_dps_local, dev_id, ip, dev.local_key, version
                        )
                        # Only push an update when something actually changed.
                        current = self.data.get(dev_id) if self.data else None
                        # One missed local poll must not mark a cloud-reachable
                        # device offline. The regular refresh handles fallback.
                        if (
                            current is not None
                            and dps is not None
                            and (not current.is_online or dps != current.dps)
                        ):
                            updated = replace(
                                current,
                                is_online=True,
                                dps=dps,
                            )
                            self.async_set_updated_data({**self.data, dev_id: updated})

                    await asyncio.sleep(gap)
        except asyncio.CancelledError:
            pass

    async def async_shutdown(self) -> None:
        """Cancel the LAN monitor background task."""
        if self._lan_monitor_task and not self._lan_monitor_task.done():
            self._lan_monitor_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._lan_monitor_task

    def _scan_lan(self) -> dict[str, dict]:
        """Synchronous: scan LAN for Tuya devices. Returns {device_id: {ip, version}}."""
        try:
            import tinytuya  # noqa: PLC0415
        except ImportError:
            _LOGGER.debug("tinytuya not available — skipping LAN scan")
            return {}

        found: dict[str, dict] = {}
        try:
            # poll=True (default) actively sends a discovery broadcast so devices
            # respond — required for v3.x firmware.  poll=False only listens for
            # spontaneous device advertisements, which most devices don't send.
            # Matches exactly what the _dev/print-local-keys.py script does.
            scan_result = tinytuya.deviceScan(maxretry=5, verbose=False)
            # tinytuya's outer keys are arbitrary (often IPs); the device ID is
            # the gwId field inside each entry — same as what the cloud returns.
            for dev in scan_result.values():
                gw_id = dev.get("gwId")
                ip = dev.get("ip")
                if gw_id and ip:
                    found[gw_id] = {
                        "ip": ip,
                        "version": dev.get("ver") or dev.get("version"),
                    }
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("tinytuya scan error: %s", exc)
        _LOGGER.debug("LAN scan result: %d device(s) found: %s", len(found), list(found.keys()))
        return found
