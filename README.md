# MQTT GUI — Project Overview

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

Mosquitto is the message broker. It receives MQTT messages from devices and sends them to subscribers. In this project, it runs as the `mosquitto-project.service` systemd service.

### Flask web application

The Flask application runs as `mqtt-gui.service`. It serves the web pages, manages login sessions, saves broker settings, and exposes APIs for the device dashboard.

### SQLite databases

The project currently uses local SQLite files:

| Database | Purpose |
| --- | --- |
| `login_signup.db` | Stores web-panel user accounts and bcrypt password hashes. |
| `device_status.db` | Stores device name, current status, and the last online time. |

## 4. Main project files

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
