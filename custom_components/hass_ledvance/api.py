"""Tuya cloud API client for the Ledvance/Tuya integration."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

import requests
from Crypto.PublicKey import RSA

from .const import (
    APP_CREDENTIALS,
    REGIONS,
    TUYA_CLIENT_ID,
    TUYA_DEVICE_ID,
    TUYA_SECRET_KEY,
    TUYA_USER_AGENT,
    region_app,
)
from .exceptions import CannotConnect, InvalidAuthentication, InvalidUserSession, TooManyRequests
from .textbook_rsa import new as new_textbook_rsa

_LOGGER = logging.getLogger(__name__)

API_VERSION_FOR_ACTION: dict[str, str] = {
    "tuya.m.device.sub.list": "1.1",
    "tuya.m.location.room.list": "2.0",
}
DEFAULT_API_VERSION = "1.0"

# Only these actions have their full result logged at debug level. Other results
# (login, session refresh, device info, ...) can contain session IDs, refresh
# tokens or device local keys and must never be logged.
_LOGGABLE_RESULT_ACTIONS = frozenset({"tuya.m.device.dp.get", "tuya.m.device.dp.publish"})


@dataclass
class DeviceInfo:
    """Raw device information from the Tuya cloud API."""

    dev_id: str
    gateway_id: str
    name: str
    product_id: str
    is_online: bool
    local_key: str
    mac: str
    schema: list       # parsed schema list
    dps: dict          # current DPS state dict (str keys)
    ip: str | None = None        # local IP reported by cloud (localIp field) — usually WAN
    version: str | None = None  # Tuya protocol version (pv field, e.g. "3.3") — used by tinytuya
    fw_version: str | None = None  # firmware/base version (bv field, e.g. "40") — display only
    room_name: str = ""  # Tuya room/area name; empty if unknown


class TuyaAPI:
    """Synchronous Tuya cloud API client."""

    def __init__(
        self,
        email: str,
        password: str,
        endpoint: str,
        country_code: int,
        client_id: str = TUYA_CLIENT_ID,
        tuya_key: str = TUYA_SECRET_KEY,
        on_sid_updated: Callable[[str], None] | None = None,
        on_tokens_updated: Callable[[str, str], None] | None = None,
    ) -> None:
        self._email = email
        self._password = password
        self._endpoint = endpoint
        self._country_code = country_code
        self._client_id = client_id
        self._tuya_key = tuya_key
        self._on_sid_updated = on_sid_updated
        # Called with (sid, refresh_token) whenever either is renewed
        self._on_tokens_updated = on_tokens_updated
        self.session = requests.Session()
        self.sid: str | None = None
        self.refresh_token: str = ""

    @classmethod
    def for_region(cls, region: str, email: str, password: str, **kwargs) -> TuyaAPI:
        """Build a client for a region key from REGIONS, using its app identity."""
        region_cfg = REGIONS[region]
        client_id, tuya_key = APP_CREDENTIALS[region_app(region)]
        return cls(
            email=email,
            password=password,
            endpoint=region_cfg["endpoint"],
            country_code=region_cfg["country_code"],
            client_id=client_id,
            tuya_key=tuya_key,
            **kwargs,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _api(
        self,
        options: dict,
        post_data: dict | None = None,
        requires_sid: bool = True,
        do_not_relogin: bool = False,
    ) -> dict | list:
        headers = {"User-Agent": TUYA_USER_AGENT}
        data = (
            {"postData": json.dumps(post_data, separators=(",", ":"))}
            if post_data is not None
            else None
        )

        sanitized_options = {**options}
        if "action" in sanitized_options:
            sanitized_options["a"] = options["action"]
            del sanitized_options["action"]

        api_version = API_VERSION_FOR_ACTION.get(
            options.get("action", ""), DEFAULT_API_VERSION
        )

        params = {
            "appVersion": "1.1.6",
            "appRnVersion": "5.14",
            "channel": "oem",
            "deviceId": TUYA_DEVICE_ID,
            "platform": "Linux",
            "requestId": str(uuid.uuid4()),
            "lang": "en",
            "clientId": self._client_id,
            "osSystem": "9",
            "os": "Android",
            "timeZoneId": "America/Sao_Paulo",
            "ttid": "sdk_tuya@" + self._client_id,
            "et": "0.0.1",
            "v": api_version,
            "sdkVersion": "3.10.0",
            "time": str(int(time.time())),
            **sanitized_options,
        }

        if requires_sid:
            if self.sid is None:
                raise InvalidUserSession("Not logged in.")
            params["sid"] = self.sid

        sanitized_data = data if data is not None else {}
        params["sign"] = self._sign({**params, **sanitized_data})

        for rate_attempt in range(1, 4):
            try:
                response = self.session.post(
                    self._endpoint, params=params, data=data, headers=headers, timeout=15
                )
                action = options.get("action", "")
                body = response.json()
                _LOGGER.debug(
                    "API %s → HTTP %s success=%s errorCode=%s",
                    action, response.status_code, body.get("success"), body.get("errorCode"),
                )
                result = self._handle(body, action, api_version)
                if action in _LOGGABLE_RESULT_ACTIONS:
                    _LOGGER.debug("API %s result: %s", action, result)
                return result
            except TooManyRequests:
                if rate_attempt == 3:
                    raise
                wait = 10.0 * rate_attempt
                _LOGGER.warning(
                    "API rate limited on %s (attempt %d/3), retrying in %.0fs",
                    options.get("action"), rate_attempt, wait,
                )
                time.sleep(wait)
                # Rebuild time/requestId for the retry
                params["time"] = str(int(time.time()))
                params["requestId"] = str(uuid.uuid4())
                params["sign"] = self._sign({**params, **sanitized_data})
            except InvalidAuthentication:
                raise
            except InvalidUserSession:
                if not do_not_relogin:
                    _LOGGER.info("Session expired — re-logging in")
                    self.login()
                    return self._api(options, post_data, requires_sid, True)
                raise
            except requests.RequestException as exc:
                raise CannotConnect(str(exc)) from exc
        raise AssertionError("unreachable")

    def _sign(self, data: dict) -> str:
        KEYS_TO_SIGN = [
            "a", "v", "lat", "lon", "lang", "deviceId", "imei", "imsi",
            "appVersion", "ttid", "isH5", "h5Token", "os", "clientId",
            "postData", "time", "requestId", "n4h5", "sid", "sp", "et",
        ]
        sorted_keys = sorted(data.keys())
        str_to_sign = ""
        for key in sorted_keys:
            if key not in KEYS_TO_SIGN or key not in data or not str(data[key]):
                continue
            prefix = "||" if str_to_sign else ""
            if key == "postData":
                str_to_sign += prefix + key + "=" + self._mobile_hash(data[key])
            else:
                str_to_sign += prefix + key + "=" + data[key]
        return hmac.new(
            self._tuya_key.encode("utf-8"),
            msg=str_to_sign.encode("utf-8"),
            digestmod=hashlib.sha256,
        ).hexdigest()

    def _mobile_hash(self, data: str) -> str:
        prehash = hashlib.md5(data.encode("utf-8")).hexdigest()
        return prehash[8:16] + prehash[0:8] + prehash[24:32] + prehash[16:24]

    def _handle(self, result: dict, action: str = "", api_version: str = "") -> dict | list:
        if result.get("success"):
            return result["result"]
        error_code = result.get("errorCode", "")
        if error_code == "USER_SESSION_INVALID":
            raise InvalidUserSession
        if error_code == "USER_PASSWD_WRONG":
            _LOGGER.warning(
                "Login rejected: %s: %s (endpoint=%s client_id=%s)",
                error_code, result.get("errorMsg"), self._endpoint, self._client_id,
            )
            raise InvalidAuthentication
        if error_code == "REQUEST_TOO_FREQUENTLY_PLEASE_TRY_AGAIN_LATER":
            raise TooManyRequests(result.get("errorMsg", "Rate limited"))
        _LOGGER.error(
            "API error %s: %s (action=%s v=%s)",
            error_code, result.get("errorMsg"), action, api_version,
        )
        raise ValueError(f"API error: {error_code} — {result.get('errorMsg')}")

    def _enc_password(self, public_key: str, exponent: str, password: str) -> str:
        key = new_textbook_rsa(RSA.construct((int(public_key), int(exponent))))
        encrypted = key.encrypt(
            hashlib.md5(password.encode("utf-8")).hexdigest().encode("utf-8")
        ).hex()
        return "0" * 64 + encrypted

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def login(self, _retries: int = 3, _backoff: float = 5.0) -> None:
        """Authenticate and store the session ID.

        Tries a silent refresh-token exchange first (no push notification).
        Falls back to full email/password login only if refresh fails or is
        unavailable.
        """
        # Try silent refresh first — avoids sending a login push notification
        if self.refresh_token:
            try:
                self._refresh_session()
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Refresh token exchange failed (%s) — falling back to full login", exc)
                self.refresh_token = ""

        for attempt in range(1, _retries + 1):
            try:
                self._login_once()
                return
            except TooManyRequests:
                if attempt == _retries:
                    raise
                wait = _backoff * attempt
                _LOGGER.warning(
                    "Rate limited during login (attempt %d/%d), retrying in %.0fs",
                    attempt, _retries, wait,
                )
                time.sleep(wait)

    def _refresh_session(self) -> None:
        """Exchange the refresh token for a new SID without triggering a push notification."""
        result = self._api(
            {"action": "tuya.m.user.session.update"},
            {"refreshToken": self.refresh_token},
            requires_sid=False,
            do_not_relogin=True,
        )
        new_sid = result.get("sid") if isinstance(result, dict) else None
        if not new_sid:
            raise ValueError("No SID in refresh response")
        new_refresh = result.get("refreshToken", self.refresh_token) if isinstance(result, dict) else self.refresh_token
        self.sid = new_sid
        self.refresh_token = new_refresh
        _LOGGER.debug("Session refreshed silently")
        if self._on_tokens_updated and self.sid:
            self._on_tokens_updated(self.sid, self.refresh_token)

    def _login_once(self) -> None:
        token_info = self._api(
            {"action": "tuya.m.user.email.token.create"},
            {"countryCode": self._country_code, "email": self._email},
            requires_sid=False,
            do_not_relogin=True,
        )
        payload = {
            "countryCode": str(self._country_code),
            "email": self._email,
            "ifencrypt": 1,
            "options": '{"group": 1}',
            "passwd": self._enc_password(
                token_info["publicKey"], token_info["exponent"], self._password
            ),
            "token": token_info["token"],
        }
        login_info = self._api(
            {"action": "tuya.m.user.email.password.login"},
            payload,
            requires_sid=False,
            do_not_relogin=True,
        )
        self.sid = login_info["sid"]
        self.refresh_token = login_info.get("refreshToken", "")
        _LOGGER.debug("Logged in (endpoint=%s client_id=%s)", self._endpoint, self._client_id)
        if self._on_tokens_updated and self.sid:
            self._on_tokens_updated(self.sid, self.refresh_token)
        elif self._on_sid_updated and self.sid:
            self._on_sid_updated(self.sid)

    def groups(self) -> list[dict]:
        """Return the list of location/group dicts."""
        return self._api({"action": "tuya.m.location.list"})  # type: ignore[return-value]

    def rooms(self, group_id: str) -> dict[str, str]:
        """Return {roomId: roomName} for a location. Empty dict on failure."""
        try:
            raw = self._api({"action": "tuya.m.location.room.list", "gid": group_id})
            return {
                str(room["roomId"]): room["name"]
                for room in (raw or [])  # type: ignore[union-attr]
                if "roomId" in room and "name" in room
            }
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Could not fetch room list for group %s", group_id)
            return {}

    def devices(self, group_id: str) -> list[DeviceInfo]:
        """Return all devices in a group, with room names attached."""
        # Room list: {roomId → roomName} — best-effort, non-fatal
        room_map = self.rooms(group_id)

        raw_list = self._api(
            {"action": "tuya.m.my.group.device.list", "gid": group_id}
        )

        # Build devId → roomName from the list payload (contains roomId per device)
        dev_room: dict[str, str] = {}
        for dev in raw_list or []:  # type: ignore[union-attr]
            dev_id = dev.get("devId")
            room_id = str(dev.get("roomId", ""))
            if dev_id and room_id and room_id in room_map:
                dev_room[dev_id] = room_map[room_id]

        result = []
        for dev in raw_list or []:  # type: ignore[union-attr]
            dev_id = dev.get("devId")
            if not dev_id:
                continue
            try:
                info = self._device_info(dev_id)
                info.room_name = dev_room.get(dev_id, "")
                result.append(info)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Skipping device %s: %s", dev_id, exc)
        return result

    def _device_info(self, device_id: str) -> DeviceInfo:
        raw = self._api({"action": "tuya.m.device.get"}, {"devId": device_id})
        schema = json.loads(raw["schema"]) if isinstance(raw["schema"], str) else raw["schema"]
        dps = raw.get("dps", {})
        # Normalise DPS keys to strings
        dps = {str(k): v for k, v in dps.items()}
        return DeviceInfo(
            dev_id=raw["devId"],
            gateway_id=raw.get("gwId", raw["devId"]),
            name=raw["name"],
            product_id=raw.get("productId", ""),
            is_online=raw.get("isOnline", False),
            local_key=raw.get("localKey", ""),
            mac=raw.get("mac", ""),
            schema=schema,
            dps=dps,
            ip=raw.get("localIp") or raw.get("ip") or None,
            version=str(raw["pv"]) if raw.get("pv") else None,
            fw_version=str(raw["bv"]) if raw.get("bv") else None,
        )

    def get_dps(self, device_id: str) -> dict:
        """Return current DPS state dict (string keys)."""
        result = self._api({"action": "tuya.m.device.dp.get"}, {"devId": device_id})
        return {str(k): v for k, v in result.items()}  # type: ignore[union-attr]

    def set_dps(self, device_id: str, gateway_id: str, dps: dict) -> bool:
        """Send DPS command via cloud. Returns True on success."""
        self._api(
            {"action": "tuya.m.device.dp.publish"},
            {"devId": device_id, "gwId": gateway_id, "dps": json.dumps(dps)},
        )
        return True
