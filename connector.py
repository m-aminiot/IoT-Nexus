import os
import stat
import json
import queue
import threading
import time
from pathlib import Path
import paho.mqtt.client as paho
import device_status

BASE_DIR = Path(__file__).resolve().parent
CREDENTIALS_PATH = BASE_DIR / ".mqtt_credentials"
TOPICS_PATH = BASE_DIR / "message_topics.json"

BROKER_HOST = "localhost"
BROKER_PORT = 7556

client_lock = threading.Lock()
topics_lock = threading.Lock()
mqtt_client = None
message_queue = queue.Queue()


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
    for topic in get_topics():
        client.message_callback_add(topic, on_topic_message)
    return client


def on_connect(client, userdata, flags, reason_code, properties=None):
    """Subscribe again after the first connection or a later reconnect."""
    if reason_code == 0:
        client.subscribe("isonline", qos=1)
        for topic in get_topics():
            client.subscribe(topic, qos=1)


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


def on_topic_message(client, userdata, msg):
    """Save one subscribed message in the queue as {topic: message}."""
    message_queue.put({msg.topic: msg.payload.decode("utf-8", errors="replace")})
    if msg.topic == "isonline":
        # A wildcard subscription such as "#" also matches the status topic.
        on_message(client, userdata, msg)


def start_listener():
    """Start exactly one MQTT listener for the Flask process."""
    global mqtt_client
    with client_lock:
        if mqtt_client is not None:
            return
        try:
            mqtt_client = create_client()
            mqtt_client.connect(host=BROKER_HOST, port=BROKER_PORT, keepalive=60)
            mqtt_client.loop_start()
        except Exception as exc:
            mqtt_client = None
            reason = f"Could not connect to the MQTT broker at {BROKER_HOST}:{BROKER_PORT} ({exc})"
            raise BrokerConnectionError(reason) from exc


def listener_status():
    """Return (connected, error) so the panel shows the real listener state."""
    try:
        start_listener()
    except BrokerConnectionError as exc:
        return False, str(exc)
    with client_lock:
        client = mqtt_client
    if client is None:
        return False, "MQTT listener is not running"
    for _ in range(10):
        # The connect handshake needs a moment after the listener starts.
        if client.is_connected():
            return True, ""
        time.sleep(0.05)
    return False, f"Lost the connection to the broker at {BROKER_HOST}:{BROKER_PORT}"


def get_devices():
    """Return the saved status of every MQTT client."""
    start_listener()
    return device_status.get_devices()


def delete_device(name):
    return device_status.delete_device(name)


def _clean_topic(topic):
    if not isinstance(topic, str):
        raise ValueError("Topic is required")
    topic = topic.strip()
    if not topic or len(topic) > 256 or any(character in topic for character in "\x00\r\n"):
        raise ValueError("Invalid topic")
    return topic


def validate_topic(topic):
    """Reject anything MQTT would not accept as a subscription filter."""
    topic = _clean_topic(topic)
    levels = topic.split("/")
    for index, level in enumerate(levels):
        if "#" in level and (level != "#" or index != len(levels) - 1):
            raise ValueError("Invalid topic: '#' must be the last level and stand alone")
        if "+" in level and level != "+":
            raise ValueError("Invalid topic: '+' must stand alone in its level")
    return topic


def _is_valid_filter(topic):
    try:
        validate_topic(topic)
        return True
    except ValueError:
        return False


def _read_saved_topics():
    """Every string in the topic file, valid filter or not."""
    try:
        saved = json.loads(TOPICS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [topic for topic in saved if isinstance(topic, str)] if isinstance(saved, list) else []


def _read_topics():
    """Saved topics that MQTT can actually subscribe to."""
    # A hand-edited or saved-invalid entry must never break the listener.
    return [topic for topic in _read_saved_topics() if _is_valid_filter(topic)]


def _write_topics(topics):
    TOPICS_PATH.write_text(json.dumps(topics, indent=2) + "\n", encoding="utf-8")


def get_topics():
    """Return the saved subscription topics."""
    with topics_lock:
        return _read_topics()


def add_topic(topic):
    """Save one topic and subscribe to it on the running listener."""
    topic = validate_topic(topic)
    with topics_lock:
        topics = _read_topics()
        if topic not in topics:
            topics.append(topic)
            _write_topics(topics)
    try:
        start_listener()
    except BrokerConnectionError:
        # The topic is saved; on_connect subscribes once the broker is reachable.
        return topics
    with client_lock:
        if mqtt_client is None:
            return topics
        mqtt_client.message_callback_add(topic, on_topic_message)
        mqtt_client.subscribe(topic, qos=1)
    return topics


def remove_topic(topic):
    """Stop listening to one topic and forget it."""
    topic = _clean_topic(topic)
    with topics_lock:
        saved = _read_saved_topics()
        if topic not in saved:
            return _read_topics()
        saved.remove(topic)
        _write_topics([entry for entry in saved if _is_valid_filter(entry)])
    with client_lock:
        if mqtt_client is not None:
            mqtt_client.unsubscribe(topic)
            mqtt_client.message_callback_remove(topic)
    return _read_topics()

