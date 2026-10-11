# HASS Ledvance

[![HACS Validation](https://github.com/buggedcom/hass-ledvance/actions/workflows/hacs.yaml/badge.svg)](https://github.com/buggedcom/hass-ledvance/actions/workflows/hacs.yaml)
[![Hassfest](https://github.com/buggedcom/hass-ledvance/actions/workflows/hassfest.yaml/badge.svg)](https://github.com/buggedcom/hass-ledvance/actions/workflows/hassfest.yaml)
[![Tests](https://github.com/buggedcom/hass-ledvance/actions/workflows/tests.yaml/badge.svg)](https://github.com/buggedcom/hass-ledvance/actions/workflows/tests.yaml)

A Home Assistant custom integration for **Ledvance** and **Sylvania** smart devices. It talks directly to the Ledvance mobile app backend — no Tuya developer account, no cloud subscription, no YAML configuration required.

Commands are sent over your local network first and fall back to the cloud only when a device is unreachable on LAN. The result is fast, reliable control that keeps working even when the Tuya cloud has an outage, as long as your devices are reachable on the network.

---

## ⚠️ Important: Ledvance / Sylvania devices only

This integration authenticates using the **Ledvance or Sylvania mobile app** credentials. It will **not** work with:

- Devices added to the **Tuya Smart** or **Smart Life** apps
- Devices added to a third-party Tuya OEM app
- Generic Tuya devices that have never been paired with the Ledvance or Sylvania platform

If your device is controlled through any app other than the official **Ledvance** or **Sylvania** app, use [Local Tuya](#comparison-with-other-integrations) or the [official Tuya integration](https://www.home-assistant.io/integrations/tuya/) instead.

---

## Features

- **Zero-configuration local control** — discovers device IPs automatically via LAN scan; no manual IP entry or local key hunting required
- **LAN-first command path** — commands reach devices in milliseconds over the local network; cloud is used only as a fallback
- **Automatic cloud fallback** — if a device is not reachable on LAN the command is transparently sent via the Tuya cloud API
- **Full light support** — on/off, brightness, colour temperature, RGB colour, and work modes (white / colour / scene effects)
- **Multi-gang socket strips** — each outlet is individually controllable, with a synthesised "All Outlets" master switch for strips that lack a hardware master DPS
- **Power monitoring** — current (A), voltage (V), active power (W), and cumulative energy (kWh) sensors for supported smart plugs and strips
- **Fault detection** — overvoltage, overcurrent, overtemperature, and leakage current alarms exposed as binary sensors
- **Fast online/offline detection** — online status is derived from the LAN poll result rather than waiting for the Tuya cloud heartbeat timeout, giving near-instant reactivity when a device goes offline
- **Automatic area assignment** — fuzzy-matches Tuya room names against your existing HA areas on setup and on every update (handles typos and partial matches)
- **Countdown timers** — per-outlet timers exposed as `number` entities (seconds)
- **Token caching** — credentials are validated once; session tokens are cached so HA restarts don't trigger a fresh login or push notification

---

## Supported Devices

Any device paired with the Ledvance or Sylvania app that uses the Tuya protocol underneath should work. Confirmed working categories:

| Category | Examples |
|---|---|
| Bulbs | SMART+ WiFi Classic A60, SMART+ WiFi Filament, SMART+ WiFi Candle |
| Ceiling & panel lights | SMART+ WiFi Ceiling Round, SMART+ WiFi Panel |
| Spots & downlights | SMART+ WiFi Spot GU10, SMART+ WiFi PAR16 |
| LED strips | SMART+ WiFi Indoor Flex RGBW, SMART+ WiFi Flex |
| Smart plugs | SMART+ WiFi Plug (EU, UK, US variants) |
| Extension sockets / strips | SMART+ WiFi Extension Socket |

### Device classification

The integration automatically classifies each device based on its Tuya DPS schema:

| Detected type | Entity platforms created |
|---|---|
| **light** | `light` + diagnostic sensors |
| **switch** | `switch` + diagnostic sensors |
| **socket_strip** | One `switch` per outlet + power `sensor` + fault `binary_sensor` + countdown `number` + diagnostic sensors |
| **unknown** | Diagnostic sensors only — the device is visible but not controllable |

---

## Comparison with other integrations

If you're deciding which integration to use, here is an honest comparison.

### vs. Local Tuya ([rospogrigio/localtuya](https://github.com/rospogrigio/localtuya))

| | HASS Ledvance | Local Tuya |
|---|---|---|
| **Works with** | Ledvance / Sylvania app accounts only | Any Tuya-based device from any app |
| **Setup complexity** | Email + password — done | Requires manual local key extraction, device IP, and DPS mapping per device |
| **Local key management** | Automatic — fetched from the cloud API on every reload | Manual — must be re-extracted after every factory reset or firmware update |
| **Schema / DPS mapping** | Automatic — fetched from the cloud schema | Manual — you define which DPS does what |
| **New device discovery** | Automatic on next poll | Manual entry for each device |
| **LAN control** | Yes — auto-discovered IPs, cloud fallback | Yes — manual IPs, no cloud fallback |
| **Cloud dependency** | Minimal — only for initial auth and when LAN fails | None at runtime |
| **Tuya developer account** | Not required | Not required |
| **Lights** | Full (brightness, colour temp, RGB, scenes) | Full — but you map each DPS manually |
| **Power monitoring** | Automatic | Manual DPS mapping |
| **Supported HA versions** | 2024.11+ | Broad support |
| **Works after cloud outage** | Yes, if devices are on LAN | Yes |
| **Works without internet** | Partial — initial setup requires cloud auth | Yes, once local keys are extracted |

**Summary:** Local Tuya gives you control over any Tuya device regardless of which app it was paired with, but every device requires significant manual configuration. HASS Ledvance is plug-and-play for Ledvance/Sylvania users but is strictly limited to that ecosystem.

---

### vs. Official Tuya integration ([home-assistant.io/integrations/tuya](https://www.home-assistant.io/integrations/tuya/))

| | HASS Ledvance | Official Tuya |
|---|---|---|
| **Works with** | Ledvance / Sylvania accounts | Any Tuya account via Tuya IoT Platform |
| **Developer account** | Not required | Required — Tuya IoT Platform account with linked app |
| **Setup complexity** | Email + password | Tuya IoT Platform project setup, client ID, secret, linking apps |
| **Local control** | Yes — automatic LAN discovery | Yes — via local control add-on (requires extra setup) |
| **Cloud polling** | Every 30 s with LAN poll | Push-based via Tuya cloud MQTT |
| **Real-time push** | No | Yes |
| **State latency** | Up to 30 s (cloud) or near-instant (LAN) | Near-instant (push) |
| **Cost** | Free | Tuya IoT Platform has a monthly API call quota (free tier limits may apply) |
| **Supported devices** | Ledvance / Sylvania only | All Tuya-ecosystem devices |

**Summary:** The official integration is more powerful (real-time push, broader device support) but requires a Tuya IoT Platform developer account and additional setup. HASS Ledvance requires nothing beyond your existing app credentials.

---

### vs. Smart Life / Tuya Smart app integrations

These integrations (e.g. [tuya-smart-life](https://github.com/tuya/tuya-smart-life)) are cloud-only with no local control. HASS Ledvance will generally be more responsive because commands are sent over LAN rather than round-tripping through the cloud.

---

## Installation via HACS

1. Open **HACS** in Home Assistant
2. Go to **Integrations**
3. Click the **⋮** menu → **Custom repositories**
4. Add `https://github.com/buggedcom/hass-ledvance` with category **Integration**
5. Search for **HASS Ledvance** and install
6. Restart Home Assistant
7. Go to **Settings → Devices & Services → Add Integration** and search for **Ledvance / Tuya**

---

## Manual Installation

1. Download or clone this repository
2. Copy the `custom_components/hass_ledvance/` directory into your HA `config/custom_components/` directory
3. Restart Home Assistant
4. Add the integration via **Settings → Devices & Services → Add Integration**

---

## Configuration

The integration is configured entirely through the UI — no YAML required.

| Field | Description |
|---|---|
| **Email** | The email address you use to log in to the Ledvance or Sylvania app |
| **Password** | Your app password |
| **Region** | The region your account is registered in — `EU`, `US`, `China`, or `India`. **Sylvania app users:** choose `US (Sylvania app)` |

> **Sylvania users:** Sylvania app accounts are separate from Ledvance app accounts, even though both apps run on the same cloud. If you set up your devices in the **Sylvania** app, pick the **`US (Sylvania app)`** region. Choosing plain `US` sends your login through the Ledvance app identity, and the cloud rejects it as "Invalid email or password" even when your credentials are correct.

Session tokens are cached in the config entry after the first successful login, so restarting HA does not trigger a new login request or push notification to your phone.

---

## How it works

### Device discovery

On first load and every 5 minutes thereafter, the integration performs a tinytuya LAN broadcast scan to discover which devices are present on the local network and what their current IPs are. This keeps the IP cache fresh automatically — no static IPs or DHCP reservations required.

### State polling

Every **30 seconds** the integration fetches device state. For devices found on LAN, state is read directly via the Tuya local protocol (encrypted TCP). For devices not on LAN, state falls back to the Tuya cloud API.

A device that responds to a LAN poll is immediately considered **online**, regardless of what the Tuya cloud reports. This gives significantly faster online/offline detection than relying on the cloud's heartbeat timeout (which can take 1–2 minutes).

### Command sending

When you toggle a switch or change a light setting in HA:

1. The command is sent directly to the device's LAN IP using the Tuya local protocol — typically responds in under 100 ms
2. If the local command fails (device unreachable or not yet discovered on LAN), the command is transparently retried via the Tuya cloud API
3. State is optimistically updated in the UI immediately so there is no visible flicker while waiting for the next poll

---

## Entities

### Lights (`light`)

| Capability | Notes |
|---|---|
| On / Off | Always |
| Brightness | When `bright_value` or `bright_value_v2` DPS present |
| Colour temperature | When `temp_value` DPS present — range mapped to 2000 K–6536 K |
| RGB colour | When `colour_data` or `colour_data_v2` DPS present |
| Work mode | White / colour / scene — exposed via the HA light colour mode system |

### Switches (`switch`)

Single-outlet smart plugs expose one switch entity with device class `outlet`.

### Socket strips (`switch`)

Each outlet on a multi-gang strip is exposed as a separate switch. If the device does not have a hardware master switch DPS, a synthesised **All Outlets** switch is created that controls all outlets simultaneously (on = any outlet on; off = all outlets off).

### Power monitoring (`sensor`)

Available on smart plugs and socket strips that include power monitoring DPS:

| Sensor | Unit | Device class |
|---|---|---|
| Current | A | `current` |
| Voltage | V | `voltage` |
| Power | W | `power` |
| Energy | kWh | `energy` |

### Fault alarms (`binary_sensor`)

| Sensor | Trigger |
|---|---|
| Overvoltage | Fault DPS bit 0x01 set |
| Overcurrent | Fault DPS bit 0x02 set |
| Overtemperature | Fault DPS bit 0x04 set |
| Leakage current | Fault DPS bit 0x08 set |

### Countdown timers (`number`)

Per-outlet countdown timers (in seconds) where supported by the device.

### Diagnostic sensors (`sensor`)

| Sensor | Always shown | Description |
|---|---|---|
| LAN IP Address | ✓ | IP discovered via LAN scan — used for local control |
| Protocol Version | ✓ | Tuya protocol version in use (3.1 / 3.3 / 3.4) |
| Cloud IP Address | — | IP reported by Tuya cloud (usually your WAN IP); disabled by default |
| Local Key | — | Device encryption key; disabled by default |
| Device ID | — | Tuya device ID; disabled by default |

---

## Troubleshooting

### "Invalid email or password" with credentials that work in the app

If your devices are set up in the **Sylvania** app, make sure you chose the `US (Sylvania app)` region — see [Configuration](#configuration). The reason the cloud gave for a rejected login is logged as a warning (credentials and tokens are never logged).

### Devices appear offline but are working in the Ledvance app

The LAN scan may not have found the device yet. Check that:
- Your HA instance and the devices are on the same network subnet
- No firewall rules are blocking UDP broadcast or TCP port 6668 between HA and your devices
- The **LAN IP Address** diagnostic sensor shows a valid IP — if it shows *Unknown*, the device has not been found on LAN and all control is going via the cloud

### Commands are slow (1–3 seconds)

If the **LAN IP Address** sensor shows *Unknown*, local control is not working and every command is falling back to the cloud API. Check the subnet/firewall points above. You can also check HA logs at debug level (`logger: custom_components.hass_ledvance: debug`) to see whether commands are going local or cloud.

### Device shows as `unknown` type

The integration could not determine what kind of device it is from the Tuya schema. Open an issue and include your device's product ID (visible in the HA device page under **Device ID**) and the debug log output — this allows the product ID table to be extended.

### Local key stopped working after a firmware update or factory reset

Reload the integration (**Settings → Devices & Services → Ledvance / Tuya → ⋮ → Reload**). Local keys are fetched fresh from the cloud on every reload.

### Area assignment isn't working

The integration uses fuzzy matching (exact → substring → difflib similarity ≥ 0.5) to match Tuya room names to HA areas. If a device is not being assigned to the correct area:
- Check that an HA area with a similar name to the Tuya room exists
- The match runs on every coordinator update, so creating the area and waiting up to 30 seconds should be sufficient
- Tuya room name is not exposed as an entity, but the assignment logic is logged at debug level

---

## Development

```bash
# Clone the repo
git clone https://github.com/buggedcom/hass-ledvance.git
cd hass-ledvance

# Install test dependencies (includes runtime deps)
pip install -r requirements_test.txt

# Run the full test suite
pytest tests/ -v

# Run with coverage report
pytest tests/ --cov=custom_components/hass_ledvance --cov-report=term-missing
```

Tests use `pytest-homeassistant-custom-component` and do not require a running HA instance or any real Ledvance devices — all API calls and hardware interactions are mocked.

---

## Contributing

Pull requests are welcome. Please:

- Run the test suite before submitting (`pytest tests/`)
- Add or update tests for any new logic
- Keep new device type mappings in `const.py` (`KNOWN_PRODUCT_TYPES`) with a note of the product name
- For new DPS codes or device capabilities, open an issue first to discuss the approach

---

## License

MIT — see [LICENSE](LICENSE) for details.
