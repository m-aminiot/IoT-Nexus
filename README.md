# iot-nexus

`iot-nexus` is a local IoT gateway built around Mosquitto and Flask. MQTT
devices publish their connection state to the broker; the web panel stores and
displays the latest state of each device.

## Current features

- Flask web panel with signup, login, logout, and protected pages.
- bcrypt password hashing in SQLite (`login_signup.db`).
- Mosquitto configuration for MQTT, optional TLS, and optional WebSocket listeners.
- Device status storage in SQLite (`device_status.db`).
- Device dashboard with online/offline counts, refresh, and device deletion.
- Linux installer/uninstaller and systemd services.
- Optional encrypted layer-3 VPN over WebSocket (`runner_vpn.py`).

The project is an early local gateway. Device control, sensor history, roles,
alerts, and blockchain integration are not implemented.

## Architecture

```text
MQTT devices -> Mosquitto -> connector.py -> device_status.db
                                      \
                                       Flask web panel -> browser
```

The Flask process connects to Mosquitto's internal listener on `127.0.0.1:7556`.
The public MQTT listener defaults to `1883`. TLS and MQTT-over-WebSocket
listeners are disabled until enabled in the panel.

## Requirements

- Python 3.10 or newer
- Linux with systemd for the full installer
- Mosquitto for a running broker

Python dependencies are listed in [`requirements.txt`](requirements.txt).

## Run locally

Create a virtual environment, install the dependencies, and start the Flask
application:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python main.py
```

Open `http://127.0.0.1:9000`, create an account, and log in. The local app
expects `.mqtt_credentials` and a broker listening on `127.0.0.1:7556`; those
files are normally created by `install.sh`.

For development, generate a broker configuration with:

```bash
python set_mosquitto.py
```

Do not commit real credentials, private keys, certificates, or SQLite database
files.

## Linux installation

Run the installer from the project directory:

```bash
sudo bash install.sh
```

The installer installs dependencies, copies the project to `/opt/mqtt-gui`,
creates a Python environment, creates the broker and web services, and installs
the `iot-nexus` terminal menu. The installer replaces `/opt/mqtt-gui`, including
databases, so back up existing data before reinstalling.

Useful commands after installation:

```bash
iot-nexus
systemctl status iot-gateway-broker.service
systemctl status iot-gateway-web.service
journalctl -u iot-gateway-web.service -n 50
```

The menu controls the broker, web panel, and optional WebSocket VPN. The VPN is
started only when selected from the menu. Its key is stored in
`/etc/iot-gateway/vpn.key`; it is a 32-byte key encoded as 64 hexadecimal
characters.

To remove an installation, use the menu or run:

```bash
sudo bash uninstall.sh
```

## Device status messages

Subscribe/publish on the exact topic `isonline`. Each payload must be JSON:

```json
{"name":"esp32-room2","status":"online"}
```

Allowed statuses are `online` and `offline`. A device should publish `online`
after connecting and configure an MQTT Last Will containing the matching
offline payload. Retained messages are recommended so the latest state is
available after the panel reconnects. See
[`DEVICE_STATUS_GUIDE.md`](DEVICE_STATUS_GUIDE.md) for the device-side
sequence.

The listener also accepts a JSON map such as:

```json
{"esp32-room2":"online","sensor-garden":"offline"}
```

The database keeps one row per device. `last_online_at` records the timestamp
when an offline transition is received; it is not a complete event history.
Deleting a row only removes the saved record. A later status message recreates
it.

## Web endpoints

All configuration and device endpoints require an authenticated session.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Redirect to login or dashboard |
| `GET` | `/login` | Login page |
| `GET` | `/signup` | Signup page |
| `POST` | `/info_login` | Authenticate a user |
| `POST` | `/info_signup` | Create a user |
| `POST` | `/logout` | End the session |
| `GET` | `/api/config` | Read Mosquitto settings |
| `POST` | `/api/config` | Validate, save, and apply settings |
| `GET` | `/api/devices` | Read saved device states |
| `DELETE` | `/api/devices` | Delete one saved device by name |

## Main files

| File | Purpose |
| --- | --- |
| `main.py` | Flask routes, sessions, configuration and device APIs |
| `connector.py` | MQTT connection and status-message parsing |
| `device_status.py` | SQLite device-status operations |
| `login_signup.py` | SQLite accounts and bcrypt verification |
| `set_mosquitto.py` | Mosquitto configuration generation and port validation |
| `mosquitto_assets.py` | Password-file and certificate management used by the menu |
| `iot-gateway.sh` | Interactive service-management menu |
| `runner_vpn.py` / `vpn_crypto.py` / `Tun.py` | Optional encrypted WebSocket VPN |
| `templates/` | Web pages |
| `tests/` | Unit and integration tests |

## Known limitations

- The current status topic is shared by all devices; per-device topics may be
  preferable for a larger deployment.
- The Flask development server is not a production WSGI deployment.
- Public deployments need HTTPS, firewall rules, and a reverse proxy.
- Browser-changing requests do not yet include CSRF protection.
- Linux TUN, routing, systemd, and the full VPN deployment require a Linux
  machine; they are not fully testable on macOS.

## Testing

Run the test suite when its Python test runner is installed:

```bash
python -m unittest discover -s tests -v
```

Compile-check the Python sources with:

```bash
python -m compileall -q .
```
