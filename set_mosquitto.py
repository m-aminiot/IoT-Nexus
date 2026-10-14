import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PORT_KEYS = ('port(websocket)', 'port_NonTls(mqtt)', 'port_Tls(mqtt)')


def validate_ports(config):
    for key in PORT_KEYS:
        if type(config.get(key)) is not int or not 1024 <= config[key] <= 65535:
            raise ValueError(f'{key} must be an integer between 1024 and 65535')
    ports = [7556, config['port_NonTls(mqtt)']]
    if config.get('active_TLS'):ESP-IDF
        ports.append(config['port_Tls(mqtt)'])
    if config.get('active_websocket'):
        ports.append(config['port(websocket)'])
    if len(ports) != len(set(ports)):
        raise ValueError('Active listeners must use different ports; 7556 is reserved for the internal listener')

def normalize_config(config):
    """Migrate older installations without changing their authentication policy."""
    config = dict(config)
    anonymous = config.pop('allow_anonymous', False)
    password = config.pop('password_file', '/opt/mqtt-gui/passwords/non-tls.passwd')
    for listener in ('non_tls', 'tls'):
        config.setdefault(f'allow_anonymous_{listener}', anonymous)
        config.setdefault(f'password_file_{listener}', password)
    return config

def write(config=None):
    if config is None:
        with (BASE_DIR / "json.json").open(encoding="utf-8") as f: config = json.load(f)
    config = normalize_config(config)
    validate_ports(config)
    # Scope credentials to each listener. Two global password_file directives
    # cause Mosquitto to exit with code 3, before opening any sockets.
    lines = ["per_listener_settings true", "log_dest stderr",
             "listener 7556 127.0.0.1", "allow_anonymous false",
             f"password_file {BASE_DIR / 'passwords' / '.mqtt_passwords'}"]
    def add_auth(listener):
        anonymous = config[f'allow_anonymous_{listener}']
        lines.append(f"allow_anonymous {'true' if anonymous else 'false'}")
        if not anonymous:
            lines.append(f"password_file {config[f'password_file_{listener}']}")
    lines.append(f"listener {config['port_NonTls(mqtt)']}")
    add_auth('non_tls')
    if config['active_TLS']:
        lines += [f"listener {config['port_Tls(mqtt)']}", f"certfile {config['certfile']}", f"keyfile {config['keyfile']}"]
        if config['need_cert(for server)']: lines.append(f"cafile {config['cafile']}")
        add_auth('tls')
    if config['active_websocket']:
        lines += [f"listener {config['port(websocket)']}", "protocol websockets"]
        add_auth('non_tls')
    (BASE_DIR / "mosquitto.conf").write_text("\n\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    write()
