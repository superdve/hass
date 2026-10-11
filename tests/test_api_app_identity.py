"""Tests for app identity selection (Ledvance vs Sylvania) and log sanitization."""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from custom_components.hass_ledvance.api import TuyaAPI
from custom_components.hass_ledvance.const import (
    APP_LEDVANCE,
    APP_SYLVANIA,
    REGIONS,
    SYLVANIA_CLIENT_ID,
    SYLVANIA_SECRET_KEY,
    TUYA_CLIENT_ID,
    TUYA_SECRET_KEY,
    region_app,
)
from custom_components.hass_ledvance.exceptions import InvalidAuthentication

SECRET_SID = "sid-should-never-be-logged"
SECRET_REFRESH = "refresh-should-never-be-logged"
SECRET_LOCAL_KEY = "localkey-should-never-be-logged"


def _response(body: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = body
    return resp


class TestRegionAppSelection:
    @pytest.mark.parametrize("region", ["EU", "US", "China", "India"])
    def test_existing_regions_keep_ledvance_identity(self, region):
        assert region_app(region) == APP_LEDVANCE
        api = TuyaAPI.for_region(region, "user@example.com", "pw")
        assert api._client_id == TUYA_CLIENT_ID
        assert api._tuya_key == TUYA_SECRET_KEY
        assert api._endpoint == REGIONS[region]["endpoint"]
        assert api._country_code == REGIONS[region]["country_code"]

    def test_sylvania_region_uses_sylvania_identity_on_us_endpoint(self):
        region = "US (Sylvania app)"
        assert region_app(region) == APP_SYLVANIA
        api = TuyaAPI.for_region(region, "user@example.com", "pw")
        assert api._client_id == SYLVANIA_CLIENT_ID
        assert api._tuya_key == SYLVANIA_SECRET_KEY
        assert api._endpoint == REGIONS["US"]["endpoint"]
        assert api._country_code == 1

    def test_existing_region_keys_unchanged(self):
        # Region keys are persisted in config entries/unique IDs; renaming breaks users.
        assert {"EU", "US", "China", "India"} <= set(REGIONS)

    def test_signing_uses_selected_app_secret(self):
        ledvance = TuyaAPI.for_region("US", "u", "p")
        sylvania = TuyaAPI.for_region("US (Sylvania app)", "u", "p")
        data = {"a": "tuya.m.user.email.token.create", "time": "1", "requestId": "r"}
        assert ledvance._sign(data) != sylvania._sign(data)

    def test_kwargs_are_forwarded(self):
        cb = MagicMock()
        api = TuyaAPI.for_region("US (Sylvania app)", "u", "p", on_tokens_updated=cb)
        assert api._on_tokens_updated is cb


class TestLogSanitization:
    def test_login_does_not_log_session_tokens(self, caplog):
        api = TuyaAPI.for_region("US (Sylvania app)", "user@example.com", "hunter2")
        token_resp = _response(
            {"success": True, "result": {"publicKey": "65537", "exponent": "3", "token": "tok"}}
        )
        login_resp = _response(
            {"success": True, "result": {"sid": SECRET_SID, "refreshToken": SECRET_REFRESH}}
        )
        api.session.post = MagicMock(side_effect=[token_resp, login_resp])
        api._enc_password = MagicMock(return_value="enc")

        with caplog.at_level(logging.DEBUG, logger="custom_components.hass_ledvance"):
            api.login()

        assert api.sid == SECRET_SID
        for secret in (SECRET_SID, SECRET_REFRESH, "hunter2", "user@example.com"):
            assert secret not in caplog.text

    def test_device_info_result_is_not_logged(self, caplog):
        api = TuyaAPI.for_region("US", "u", "p")
        api.sid = "x"
        api.session.post = MagicMock(
            return_value=_response(
                {
                    "success": True,
                    "result": {
                        "devId": "dev1",
                        "name": "Bulb",
                        "localKey": SECRET_LOCAL_KEY,
                        "schema": "[]",
                    },
                }
            )
        )
        with caplog.at_level(logging.DEBUG, logger="custom_components.hass_ledvance"):
            info = api._device_info("dev1")

        assert info.local_key == SECRET_LOCAL_KEY
        assert SECRET_LOCAL_KEY not in caplog.text
        assert "HTTP 200" in caplog.text

    def test_dps_result_is_still_logged(self, caplog):
        api = TuyaAPI.for_region("US", "u", "p")
        api.sid = "x"
        api.session.post = MagicMock(return_value=_response({"success": True, "result": {"1": True}}))
        with caplog.at_level(logging.DEBUG, logger="custom_components.hass_ledvance"):
            api.get_dps("dev1")
        assert "tuya.m.device.dp.get result" in caplog.text

    def test_rejected_login_logs_sanitized_reason(self, caplog):
        api = TuyaAPI.for_region("US", "user@example.com", "hunter2")
        api.session.post = MagicMock(
            return_value=_response(
                {"success": False, "errorCode": "USER_PASSWD_WRONG", "errorMsg": "Incorrect password"}
            )
        )
        with caplog.at_level(logging.DEBUG, logger="custom_components.hass_ledvance"), pytest.raises(
            InvalidAuthentication
        ):
            api.login()

        assert "USER_PASSWD_WRONG" in caplog.text
        assert TUYA_CLIENT_ID in caplog.text
        assert "hunter2" not in caplog.text
        assert "user@example.com" not in caplog.text
