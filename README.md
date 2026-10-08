# iot-nexus — Project Overview

## 1. Project purpose

MQTT GUI is designed to become a **central local control and monitoring server** for connected devices.

It can be installed on a computer that stays on at the installation site, for example:

- a small Linux computer such as a Raspberry Pi or mini PC in a home;
- an industrial PC or local gateway in a building, workshop, farm, or factory;
- a server inside a private local network.

Calling this computer an **embedded system** is not always exact. The individual sensors and controllers may be embedded devices, but this project is better described as a **central gateway/server** that manages them.

The central server runs the Mosquitto MQTT broker and this web panel. Sensors, ESP32 boards, microcontrollers, and other MQTT clients connect to it. From one web page, an authorized user can monitor devices, check their connection state, manage broker settings, and later control devices or review their data.

The long-term goal is a practical local IoT platform: devices communicate with one trusted central point instead of every device needing its own separate server setup.

## 2. What the project currently provides

The current version provides the foundation for that platform:

- A Flask-based web panel.
- User signup and login.
- Password storage with bcrypt hashing.
- Protected pages for logged-in users.
- Mosquitto broker configuration through the web panel.
- MQTT, TLS, and WebSocket configuration fields.
- A device dashboard that shows online and offline devices.
- Storage of the latest device status in SQLite.
- Storage of the time when a device becomes offline, displayed as its last online time.
- Deletion of a device and its saved status record from the web panel.
- Linux installation and removal scripts that create systemd services.

## 3. System architecture

The project has four main parts:

```text
MQTT devices and sensors
          │
          │ MQTT messages
          ▼
Mosquitto broker on the central server
          │
          │ local MQTT subscription
          ▼
Flask application and device-status listener
          │
          │ SQLite data + HTTP API
          ▼
Web panel in the user's browser
```

### MQTT devices

Devices can include ESP32 boards, sensors, relays, meters, controllers, or any MQTT-capable client. Their job is to connect to the broker and publish their state or sensor data.

### Mosquitto broker

Mosquitto is the message broker. It receives MQTT messages from devices and sends them to subscribers. In this project, it runs as the `iot-gateway-broker.service` systemd service.

### Flask web application

The Flask application runs as `iot-gateway-web.service`. It serves the web pages, manages login sessions, saves broker settings, and exposes APIs for the device dashboard.

### SQLite databases

The project currently uses local SQLite files:

| Database | Purpose |
| --- | --- |
| `login_signup.db` | Stores web-panel user accounts and bcrypt password hashes. |
| `device_status.db` | Stores device name, current status, and the last online time. |

## 4. Main project files

### Terminal control menu (Linux / systemd)

For a fresh installation, run `sudo bash install.sh` from the project directory.
The installer places the command in `/usr/local/bin`. Open it with:

```sh
iot-nexus
```

```text
1 - websocket
2 - panel management
3 - restart
4 - start
5 - stop
6 - uninstall
0 - exit
```

Start and restart manage the MQTT broker and web panel; restart also restarts
the VPN if it is already running. Stop stops all three. The WebSocket submenu
offers `1 - start`, `2 - status`, `3 - restart`, `4 - stop`,
`5 - encryption key`, and `0 - back`;
start, status, and restart ask for a port (1–65535).
Start saves `VPN_PORT` in `/etc/iot-gateway/websocket.env` and starts
`iot-gateway-websocket.service` as root through sudo/systemd. It continues
running when the menu exits. Status verifies that this service's Python process
is listening on the requested TCP port, rather than just checking for a busy port.
WebSocket starts only when selected from the menu; it is not enabled at boot.
Changing its port restarts its process and disconnects existing VPN clients.

### Encrypted layer-3 VPN packets

`vpn_crypto.py` provides `encrypt_packet(packet, cipher)` and
`decrypt_packet(frame, cipher)`, imported by `runner_vpn.py`. Outgoing packets
read from `iot0` are encrypted before WebSocket transmission. Incoming frames
are authenticated and decrypted before writing plaintext IP packets to `iot0`.
Only traffic routed through this TUN is covered; this change does not install
routes or encrypt unrelated host traffic. Outer IP/TCP/WebSocket headers remain
visible. WebSocket compression is disabled.

The installer creates a random **32-byte / 256-bit** pre-shared key (not a
32-bit key) in `/etc/iot-gateway/vpn.key`, encoded as 64 hex characters.
The file is root-owned, mode `600`, outside the web application's directory.
Reinstallation preserves this key. Missing, invalid, or publicly readable keys
prevent VPN startup; there is no plaintext fallback. Uninstall removes the key.

In the terminal, select `iot-nexus → 1 websocket → 5 encryption key`:

```text
1 - key status           # algorithm and fingerprint, no secret
2 - show key             # explicitly display for provisioning the peer
3 - set key              # hidden input: exactly 64 hex characters
4 - generate random key  # new random 32-byte key
0 - back
```

Key operations use sudo/root. The Flask web user receives no new sudo rights.
Changing the key restarts an active VPN, disconnecting its clients; a stopped
VPN stays stopped. Update the peer's key before reconnecting. If the menu reports
a restart failure, resolve the service error before using the VPN.

**Wire protocol v1 (both endpoints must implement this):**

1. Server sends a binary `b"INX1" + server_random`, with 32 random bytes.
2. Client generates its own fresh 32 random bytes for every connection. Both
   derive 64 bytes using HKDF-SHA256 with the shared key as input,
   `server_random + client_random` as salt, and `b"iot-nexus/v1/packet-keys"`
   as info. First 32 bytes are the server-to-client AES key; the remaining 32
   are the client-to-server AES key.
3. Client sends `b"INX1" + client_random + encrypt_packet(b"", client_cipher)`.
   Server verifies this proof within 10 seconds, then returns its own encrypted
   empty acknowledgment. Neither acknowledgment is written to the TUN.
4. Each subsequent binary message contains a 12-byte big-endian sequence number
   used as the nonce, followed by ciphertext and a 16-byte GCM tag. AAD is
   `b"INX1"`. Each direction starts at sequence 0 for its acknowledgment and 1
   for its first IP packet. Maximum plaintext size is 65535 bytes. Reconnect
   before sequence `2**32`. Replayed, out-of-order, invalid-tag and plaintext
   messages are rejected; accepted IP packets are written only after validation.

Each connection has its own `PacketCipher`; never reuse a context or handshake
randomness across connections. This pre-shared-key protocol does not provide
forward secrecy. A WebSocket HTTP 101 response alone no longer proves successful
VPN authentication. Old clients that send raw IP packets must be updated.

Minimal peer handshake using the same module (inside your async client):

```python
import secrets
from vpn_crypto import MAGIC, PacketCipher, encrypt_packet, decrypt_packet

# key: 32 secret bytes provisioned securely, not a password or a hex string.
hello = await ws.recv()
if not isinstance(hello, bytes) or len(hello) != 36 or hello[:4] != MAGIC:
    raise ValueError("Invalid server hello")
client_random = secrets.token_bytes(32)
cipher = PacketCipher(key, hello[4:], client_random, server=False)
await ws.send(MAGIC + client_random + encrypt_packet(b"", cipher))
if decrypt_packet(await ws.recv(), cipher) != b"":
    raise ValueError("Invalid server proof")
# Send: await ws.send(encrypt_packet(ip_packet, cipher))
# Receive: tun.write(decrypt_packet(await ws.recv(), cipher))
```

**Upgrade an existing installation:** the full installer replaces
`/opt/mqtt-gui`; use this targeted update from the updated source directory to
preserve panel data. Stop the VPN first; existing raw-packet clients need the
protocol update above before reconnecting.

```sh
sudo systemctl stop iot-gateway-websocket.service
sudo install -d -o root -g root -m 755 /etc/iot-gateway /usr/local/lib/iot-gateway
sudo install -o root -g root -m 644 runner_vpn.py vpn_crypto.py Tun.py /usr/local/lib/iot-gateway/
sudo /usr/local/lib/iot-gateway/venv/bin/pip install 'websockets>=14.0,<16.0' 'cryptography>=46.0.3,<47.0'
sudo /usr/local/lib/iot-gateway/venv/bin/python /usr/local/lib/iot-gateway/vpn_crypto.py init
sudo install -o root -g root -m 755 iot-gateway.sh /usr/local/bin/iot-gateway
sudo install -o root -g root -m 755 iot-gateway.sh /usr/local/bin/iot-nexus
iot-nexus
```

Select WebSocket → encryption key to provision your peer, then start the VPN.
Local tests cover real WebSocket exchange with a simulated TUN, tampering,
wrong keys, replay, key storage, and menu rotation behavior. Linux TUN/routing
and systemd operation must additionally be verified on the deployment server.

Panel management offers `1 - start`, `2 - restart`, `3 - stop`,
`4 - status / URL`, and `0 - back`. Its actions affect only the web panel.
Start and restart ask for a port: enter a number, `r` for an automatically
selected free port, or Enter to keep the current/default port. The choice is
saved as `PORT` in `/etc/iot-gateway/panel.env`. The menu creates a systemd
environment drop-in for existing installations, so no reinstall is needed.
It repairs Python-module/template read permissions and database ownership
before starting. This fixes failures such as `Permission denied: login_signup.py`
when copied source files retain mode 600. Success requires two HTTP readiness
checks with the same process; a failed startup displays its journal errors.
Status reads the panel process's listening sockets and shows local and server
interface URLs with the actual port. A stopped panel has no active URL.
The listed interface addresses do not discover a public NAT address or reverse
proxy domain, and remote access still depends on network/firewall configuration.
In an interactive terminal, the banner and menus occupy an alternate screen;
after reading an action's result, press Enter to return to the same screen.
`iot-gateway` remains an alias command for compatibility; existing systemd unit
names and installation directories remain as listed below.

Uninstall runs the root-owned copy of `uninstall.sh` at
`/usr/local/lib/iot-gateway/uninstall.sh` through sudo. It asks you to type
`UNINSTALL`, removes the application and data as well as Mosquitto packages,
and exits the menu after successful removal.

To update just the menu on an existing installation, run these from the updated
project directory on your Linux server (without reinstalling or replacing data):

```sh
sudo install -o root -g root -m 755 iot-gateway.sh /usr/local/bin/iot-gateway
sudo install -o root -g root -m 755 iot-gateway.sh /usr/local/bin/iot-nexus
sudo install -o root -g root -m 644 uninstall.sh /usr/local/lib/iot-gateway/uninstall.sh
iot-nexus
```

The service names are `iot-gateway-broker.service`, `iot-gateway-web.service`,
and `iot-gateway-websocket.service`. VPN code and its Python environment are
installed under `/usr/local/lib/iot-gateway`, owned by root.
For WebSocket startup errors, use `sudo journalctl -u iot-gateway-websocket -n 50`.

The installer replaces `/opt/mqtt-gui` on each run, including any databases in
that directory. Back up existing data before reinstalling. Uninstall with
`sudo bash uninstall.sh`; this also removes the menu and WebSocket service.

| File or folder | Responsibility |
| --- | --- |
| `main.py` | Main Flask application, routes, authentication checks, configuration API, device API, and device deletion API. |
| `connector.py` | Creates one MQTT listener for the web application and receives device-status messages. |
| `device_status.py` | Contains the device database functions: save a device, read all devices, and delete a device. |
| `login_signup.py` | Creates and validates web-panel accounts. |
| `set_mosquitto.py` | Generates `mosquitto.conf` from the saved JSON configuration. |
| `json.json` | Stores broker settings selected from the panel. |
| `templates/` | Contains the HTML pages for login, signup, dashboard, broker settings, and devices. |
| `install.sh` | Installs dependencies, creates users and services, and starts the project on Linux. |
| `uninstall.sh` | Removes the installed services and project files. |
| `DEVICE_STATUS_GUIDE.md` | Short device-side guide for online/offline MQTT messages. |

## 5. Device online and offline status

The current application listens for device status messages on this MQTT topic:

```text
isonline
```

Each device sends JSON with a unique name and status.

When the device has connected successfully, it must immediately publish:

```json
{"name": "esp32-room2", "status": "online"}
```

When the device becomes unavailable, the broker must publish:

```json
{"name": "esp32-room2", "status": "offline"}
```

The application stores the current status. When it receives `offline`, it also saves the exact time of that event. The Devices page displays that saved time as the device's last online time.

## 6. MQTT Last Will and Testament

Every device should configure an MQTT **Last Will and Testament (LWT)** before connecting.

The Last Will must use the same device name and send an `offline` JSON message to `isonline`. If the device loses power, Wi-Fi, or its MQTT connection without sending a normal disconnect message, Mosquitto automatically publishes the Last Will. This lets the panel mark the device as offline.

Recommended connection sequence:

1. Configure the Last Will with the `offline` message.
2. Connect to the MQTT broker.
3. Immediately publish the `online` message after a successful connection.

## 7. Current limits

This is an early version. The following limits are known:

- The device database stores the latest state, not a complete history of all state changes.
- Deleting a device removes its saved local record. If that device later publishes a new status message, it will appear in the panel again.
- The current shared status topic is simple for development. For a larger system, each device should use its own topic such as `isonline/esp32-room2`, while the server subscribes to `isonline/#`.
- A shared retained MQTT topic can only keep one retained message at a time. Per-device topics are needed if retained state must be reliable for every device.
- The current Flask service is a simple deployment. A public production deployment needs HTTPS, firewall rules, a reverse proxy, and a production WSGI server.
- Browser requests that change configuration or delete a device should later receive CSRF protection.

## 8. Security notes

- Web-panel passwords are stored as bcrypt hashes rather than plain text.
- The installer generates private MQTT credentials for the local service.
- MQTT credential files are hidden local files and are excluded from Git.
- The installer generates a random Flask secret key for the installed service.
- The web panel should not be exposed directly to the public Internet without HTTPS and network protection.
- TLS should be enabled when MQTT devices communicate across an untrusted network or the Internet.

## 9. Planned future development

The following items are plans for future versions. They are **not implemented in the current project**.

### C++ libraries for microcontrollers

A small C++ library or module is planned for microcontrollers and embedded devices. Its purpose is to make device integration easier:

- Connect to Wi-Fi and MQTT with less repeated code.
- Configure the MQTT Last Will automatically.
- Publish the online message after connecting.
- Handle reconnects and publish the offline state correctly through the Last Will.
- Provide simple helper functions for publishing sensor data and receiving commands.

This should reduce setup mistakes and make it easier to add new ESP32 or other microcontroller-based devices to the platform.

### Device control and sensor data

Future modules can add commands for relays, lights, motors, alarms, and other actuators. They can also store sensor readings such as temperature, humidity, energy use, water level, or machine state.

### Encrypted blockchain data records

A future design may record selected device events or data hashes in a blockchain-based system. The intended purpose is **tamper-evidence and auditability**: once a record is committed, changing it later should be detectable.

Before any blockchain integration, the design must clearly define:

- which data is valuable enough to record;
- whether raw data stays in a normal database and only its cryptographic hash is recorded;
- how encryption keys are created, protected, and recovered;
- who is allowed to read or write records;
- cost, speed, privacy, and legal requirements.

For most sensor systems, storing every raw measurement directly on a blockchain is usually unnecessary. A more practical approach is to keep encrypted operational data in the project database or another secure storage system, then write a hash or selected audit record to the blockchain when data integrity needs independent proof.

### Other possible modules

- Device groups, rooms, and locations.
- Charts and sensor-data history.
- Alerts for offline devices or unsafe sensor values.
- Roles such as administrator, operator, and viewer.
- Backup and restore tools.
- Remote access through a secure VPN.
- Audit logs for device commands and configuration changes.

## 10. Summary

MQTT GUI is the starting point for a local, central IoT gateway. It brings devices, sensors, the MQTT broker, and the user dashboard together in one place. The current version focuses on secure access, broker setup, and basic device-status monitoring. Future versions can grow it into a more complete monitoring, automation, data-history, and integrity-verification platform.
