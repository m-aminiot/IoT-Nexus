# iot-nexus

[راهنمای فارسی — Persian README](README.FA.md)

![IoT Nexus terminal control center](picture/Screenshot%201405-08-07%20at%2001.13.40.png)

*Terminal menu screenshot; available options can differ from the current version.*

`iot-nexus` is a local IoT gateway built around Mosquitto and Flask. MQTT
devices publish their connection state to the broker; the web panel stores and
displays the latest state of each device.

## Current features

- Flask web panel with signup, login, logout, and protected pages.
- bcrypt password hashing in SQLite (`login_signup.db`).
- Mosquitto configuration for MQTT, optional TLS, and optional WebSocket listeners.
- Device status storage in SQLite (`device_status.db`).
- Device dashboard with online/offline counts, refresh, and device deletion.
- Message topics page: subscribe the gateway to topics and queue incoming messages.
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
- Debian/Ubuntu-style Linux with `apt` and systemd for the full installer
- Mosquitto for a running broker

Python dependencies are listed in [`requirements.txt`](requirements.txt).

## Run locally

Create a virtual environment, install the dependencies, and start the Flask
application:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
python main.py
```

Open `http://127.0.0.1:9000`, create an account, and log in. The local app
expects `.mqtt_credentials` and a broker listening on `127.0.0.1:7556`; those
files are normally created by `install.sh`.

The panel can start without a broker, but device monitoring and subscriptions
need the internal listener. Generating its configuration alone does not create
credentials, password files, or certificates. Use the Linux installer for the
complete setup. For development, generate a broker configuration with:

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

The VPN server supports multiple authenticated WebSocket clients at the same
time. Packets read from the shared `iot0` TUN interface are broadcast to all
connected clients, while packets received from any client are written back to
that interface.

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
offline payload. A retained message can restore the latest publication after reconnecting,
but the shared `isonline` topic retains only one message, not one per device.
Per-device retained topics would require a corresponding listener change. See
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

## Message topics and the message queue

The **Messages** page (`/messages`) lists subscription topics that you add or
remove at any time. They are saved in `message_topics.json` and survive a
restart. The gateway's own MQTT user (the one in `.mqtt_credentials`) subscribes
to them, so no extra broker account is needed; wildcards such as `home/#` work.

Every incoming message is queued in order as `{topic: message}` on the
`connector.message_queue` queue (`queue.Queue` from the standard library), for
example `{"home/kitchen": "21.5"}`. Payloads that are not valid UTF-8 are kept
with replacement characters rather than dropped. The queue lives in memory, so
messages already collected stay in the queue while the broker is down; storing
them is left to the consumer.

The queue has no size limit, is not persisted, and is lost when the Python
process exits. No HTTP endpoint or browser message viewer drains it; a consumer
must run in the same process. Multiple web workers would have separate queues.

Subscriptions are registered in `on_connect`, so the gateway re-subscribes by
itself after a broker restart or a network drop.

The page reports the real listener state from `/api/topics` (`connected` and
`error`), so it only shows **Listening** while the broker is actually
reachable; connection errors are written into a small log under the topic
list. Topics can still be saved or removed while the broker is offline;
HTTP 200 can include `connected: false` and a connection `error`. That is a
connection status, not a failed topic write. The page refreshes every five seconds.

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
| `GET` | `/mainpage` | Dashboard |
| `GET` | `/devices` | Device-status page |
| `GET` | `/mosquitto/panel` | Broker settings page |
| `GET` | `/messages` | Message-topic page |
| `GET` | `/api/topics` | List the subscription topics |
| `POST` | `/api/topics` | Add one subscription topic |
| `DELETE` | `/api/topics` | Remove one subscription topic |

## Main files

| File | Purpose |
| --- | --- |
| `main.py` | Flask routes, sessions, configuration and device APIs |
| `connector.py` | MQTT connection, status-message parsing, topic subscriptions, message queue |
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

After installing the Python dependencies, run the standard-library test suite:

```bash
python -m unittest discover -s tests -v
```

The gateway tests include a real Mosquitto integration check. When both
`mosquitto` and `mosquitto_passwd` are installed, it creates temporary internal
and public password files, generates a temporary configuration, starts a
broker on temporary localhost ports, and verifies that both listeners open:

```bash
python -m unittest discover -s tests -p test_gateway.py -k test_generated_config_starts_real_broker_with_two_password_files -v
```

The test uses only temporary files and ports, so it does not modify the
installed broker configuration. If Mosquitto is unavailable, the integration
test is skipped; the remaining gateway and configuration tests still run.

`tests/test_messages.py` covers the topic store and the panel API, plus
broker-backed checks that a message published by a normal broker user reaches
the queue, that queued messages survive a broker outage, that the listener
re-subscribes after the broker returns, and that the queue keeps 1000 messages
in order while a consumer reads it. The broker-backed tests are skipped when
Mosquitto is missing and only use a temporary broker on a random localhost
port.

Compile-check the Python sources with:

```bash
python -m compileall -q .
```

## Ports and settings

| Setting | Default / behavior |
| --- | --- |
| Web panel `PORT` | `9000`; configurable in the terminal menu |
| Internal MQTT | `127.0.0.1:7556`; reserved, authenticated listener |
| Public MQTT | `1883`; checked-in configuration allows anonymous access |
| MQTT TLS | `8883`; disabled by default |
| MQTT over WebSocket | `9001`; disabled by default |
| Encrypted VPN `VPN_PORT` | `8765`; independent of MQTT WebSocket |
| Flask `SECRET_KEY` | Set explicitly for local runs; installer generates one |
| `COOKIE_SECURE` | `0` by default; set to `1` for HTTPS deployments |

Replace the example certificate and password paths in `json.json` before
activating listeners that need them. The terminal menu's **mosquitto** submenu
manages password files, self-signed/Certbot certificates, and broker restarts.
The **panel management** submenu controls the panel port and shows its URL.
MQTT accounts and web-panel accounts are separate.

## VPN behavior

The optional VPN uses Linux `iot0` (`10.0.0.1/24`) and AES-256-GCM encrypted
packets with a shared key. `vpn_crypto.py` derives separate session keys for
both directions; sequence checks reject repeated or out-of-order frames.
The shared-key protocol does not provide forward secrecy. Routing must be
configured separately: starting the service does not route all host traffic
through it. TUN output is broadcast to authenticated clients, so this is not
per-client traffic isolation. Manage the key through **websocket → encryption
key**; changing it restarts an active VPN and clients need the new key.

## Troubleshooting message topics

1. Open the browser Network tab and inspect `POST /api/topics`, including its
   HTTP status and JSON response. HTTP 400 indicates invalid input; HTTP 500
   needs the returned error and server log. HTTP 401 requires logging in again.
2. Use filters such as `home/temperature`, `home/+`, or `home/#`. `+` must occupy
   an entire level; `#` must occupy the final level. Empty filters are rejected.
3. If saving fails, check that the web-service user can write
   `/opt/mqtt-gui/message_topics.json` and its directory. A file created by root
   can remain unwritable even if the directory has the correct owner.
4. For connection errors, inspect the broker and web logs:

   ```bash
   sudo journalctl -u iot-gateway-web.service -n 50 --no-pager
   sudo journalctl -u iot-gateway-broker.service -n 50 --no-pager
   ```

   Check the internal listener and the service user's `.mqtt_credentials` file
   (owner must match the process user; no group/other permissions). Do not post
   passwords or private keys in bug reports.
5. If browser messages differ from this source, verify the installed copy in
   `/opt/mqtt-gui`, restart the web service after updating it, and reload the page.
   Reinstalling is not a data-preserving update.

## License

[MIT](LICENSE).
