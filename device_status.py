"""Small database helper for the MQTT device-status panel."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path


DATABASE_PATH = Path(__file__).resolve().parent / "device_status.db"


def _connect():
    return sqlite3.connect(DATABASE_PATH)


def create_table():
    with _connect() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                name TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (status IN ('online', 'offline')),
                last_online_at TEXT
            )
            """
        )


def save_device(name, status):
    """Save one device status.

    `name` is the device name and `status` must be "online" or "offline".
    When a device becomes offline, this saves that exact moment as its last
    online time. Receiving online does not change the saved time.
    """
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Device name is required")

    if not isinstance(status, str) or status.strip().lower() not in {"online", "offline"}:
        raise ValueError("Status must be online or offline")

    name = name.strip()
    status = status.strip().lower()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO devices (name, status, last_online_at)
            VALUES (?, ?, CASE WHEN ? = 'offline' THEN ? ELSE NULL END)
            ON CONFLICT(name) DO UPDATE SET
                status = excluded.status,
                last_online_at = CASE
                    WHEN excluded.status = 'offline' AND devices.status = 'online'
                        THEN excluded.last_online_at
                    ELSE devices.last_online_at
                END
            """,
            (name, status, status, now),
        )


def get_devices():
    """Return all saved devices for the API and the web page."""
    with _connect() as connection:
        rows = connection.execute(
            "SELECT name, status, last_online_at FROM devices ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return {
        name: {"status": status, "last_online_at": last_online_at}
        for name, status, last_online_at in rows
    }


def delete_device(name):
    """Remove one device and its saved history. Returns True when removed."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Device name is required")

    with _connect() as connection:
        cursor = connection.execute("DELETE FROM devices WHERE name = ?", (name.strip(),))
    return cursor.rowcount > 0


create_table()
