import os
import stat
import json
import threading
from pathlib import Path
import paho.mqtt.client as paho
import device_status

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / ".mqtt_credentials"

client_lock = threading.Lock()
mqtt_client = None


class BrokerConnectionError(RuntimeError):
    pass

def read_credentials():
    values = {}
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(CREDENTIALS_PATH, flags)
    file_stat = os.fstat(fd)
    if not stat.S_ISREG(file_stat.st_mode):
        os.close(fd)
        raise RuntimeError("MQTT credentials must be a regular file")
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        os.close(fd)
        raise PermissionError("MQTT credentials must not be accessible by group or others")
    if file_stat.st_uid != os.geteuid():
        os.close(fd)
        raise PermissionError("MQTT credentials must be owned by the service user")
    with os.fdopen(fd, encoding="utf-8") as f:
        for line in f:
            key, sep, value = line.strip().partition("=")
            if sep and key in {"username", "password"}:
                values[key] = value
    if set(values) != {"username", "password"}:
        raise RuntimeError("Invalid MQTT credentials file")
    return values["username"], values["password"]

def create_client():
    username, password = read_credentials()
    client = paho.Client()
    client.username_pw_set(username, password)
    client.on_message = on_message
    client.on_connect = on_connect
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    return client


def on_connect(client, userdata, flags, reason_code, properties=None):
    """Subscribe again after the first connection or a later reconnect."""
    if reason_code == 0:
        client.subscribe("isonline", qos=1)


def update_devices(payload):
    """Accept one status or a map of client names to statuses.

    Valid MQTT payload examples:
      {"name": "esp32-room", "status": "online"}
      {"esp32-room": "online", "sensor-garden": "offline"}
    """
    if not isinstance(payload, dict):
        return

    if set(("name", "status")).issubset(payload):
        updates = {payload["name"]: payload["status"]}
    else:
        updates = payload

    for name, status in updates.items():
        if not isinstance(name, str) or not isinstance(status, str):
            continue
        name = name.strip()
        status = status.strip().lower()
        if name and len(name) <= 128 and status in {"online", "offline"}:
            device_status.save_device(name, status)


def on_message(client, userdata, msg):
    try:
        update_devices(json.loads(msg.payload.decode("utf-8")))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Ignore invalid messages instead of stopping the MQTT listener.
        return


def start_listener():
    """Start exactly one MQTT listener for the Flask process."""
    global mqtt_client
    with client_lock:
        if mqtt_client is not None:
            return
        try:
            mqtt_client = create_client()
            mqtt_client.connect(host="localhost", port=7556, keepalive=60)
            mqtt_client.loop_start()
        except Exception as exc:
            mqtt_client = None
            raise BrokerConnectionError("Could not connect to the MQTT broker") from exc


def get_devices():
    """Return the saved status of every MQTT client."""
    start_listener()
    return device_status.get_devices()


def delete_device(name):
    return device_status.delete_device(name)
