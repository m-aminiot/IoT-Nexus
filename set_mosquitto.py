import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def write(config=None):
    if config is None:
        with (BASE_DIR / "json.json").open(encoding="utf-8") as f: config = json.load(f)
    lines = ["listener 7556 127.0.0.1", "allow_anonymous false", f"password_file {BASE_DIR / '.mqtt_passwords'}"]
    if not config['allow_anonymous']: lines.append(f"password_file {config['password_file']}")
    lines.append(f"listener {config['port_NonTls(mqtt)']}")
    if config['active_TLS']:
        lines += [f"listener {config['port_Tls(mqtt)']}", f"certfile {config['certfile']}", f"keyfile {config['keyfile']}"]
        if config['need_cert(for server)']: lines.append(f"cafile {config['cafile']}")
    if config['active_websocket']: lines += [f"listener {config['port(websocket)']}", "protocol websockets"]
    (BASE_DIR / "mosquitto.conf").write_text("\n\n".join(lines) + "\n", encoding="utf-8")
