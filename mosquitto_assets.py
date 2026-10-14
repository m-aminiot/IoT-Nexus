"""Root-only terminal management of MQTT credentials and TLS assets."""
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import grp
import ipaddress
import json
import os
import pwd
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

APP_DIR = Path('/opt/mqtt-gui')


def name(value):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', value):
        raise ValueError('Use a name of 1–64 letters, numbers, dots, underscores or hyphens.')
    return value


def checked(path):
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError(f'Symbolic links are not allowed: {component}')
    return path


def permissions(path, directory=False):
    checked(path)
    os.chown(path, 0, grp.getgrnam('mosquitto').gr_gid)
    path.chmod(0o750 if directory else 0o640)


def password_permissions(path):
    checked(path)
    os.chown(path, pwd.getpwnam('mosquitto').pw_uid, grp.getgrnam('mosquitto').gr_gid)
    path.chmod(0o600)


def folder(path):
    checked(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o750)
    permissions(path, directory=True)
    return path


@contextmanager
def locked():
    # Serialize menu sessions, including read/modify/write of password files.
    directory = folder(APP_DIR / 'passwords')
    lock = checked(directory / '.lock')
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def run(args):
    subprocess.run(args, check=True, timeout=300)


def password_path(value):
    return checked(APP_DIR / 'passwords' / (name(value) + '.passwd'))


def certificate_path(value):
    return checked(APP_DIR / 'certificates' / name(value))


def atomic_password(path, content, *, mosquitto_owned=True):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(content)
    try:
        if mosquitto_owned:
            password_permissions(temporary)
        else:
            permissions(temporary)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def repair_internal_password():
    """Migrate the internal listener's hashes or rebuild from its existing login."""
    path = checked(APP_DIR / 'passwords' / '.mqtt_passwords')
    if path.exists():
        if not path.is_file() or not path.stat().st_size:
            raise ValueError('Internal password file is empty or invalid.')
    else:
        legacy = checked(APP_DIR / '.mqtt_passwords')
        if legacy.is_file() and legacy.stat().st_size:
            atomic_password(path, legacy.read_bytes())
        else:
            credentials = checked(APP_DIR / '.mqtt_credentials')
            if not credentials.is_file():
                raise ValueError('Internal MQTT credentials are missing. Restore .mqtt_credentials from the installation backup.')
            values = dict(line.partition('=')[::2] for line in credentials.read_text().splitlines() if '=' in line)
            username, password = values.get('username', ''), values.get('password', '')
            if not username or not password or any(c in username for c in ':\r\n\x00') or any(c in password for c in '\r\n\x00'):
                raise ValueError('Invalid internal MQTT credentials.')
            with tempfile.TemporaryDirectory(dir=path.parent) as directory:
                entry = Path(directory) / 'entry'
                entry.write_text(f'{username}:{password}\n')
                entry.chmod(0o600)
                run(['mosquitto_passwd', '-U', str(entry)])
                atomic_password(path, entry.read_bytes())
    password_permissions(path)
    # Repair only the internal listener; preserve manually edited public listeners.
    config = checked(APP_DIR / 'mosquitto.conf')
    lines = config.read_text().splitlines(keepends=True)
    internal = False
    replacements = 0
    for index, line in enumerate(lines):
        parts = line.strip().split()
        if parts and parts[0] == 'listener':
            if len(parts) < 2 or not parts[1].isdigit() or not 1024 <= int(parts[1]) <= 65535:
                raise ValueError('Broker listener ports must be between 1024 and 65535. Choose a valid port in the web panel and apply.')
            internal = len(parts) >= 2 and parts[1] == '7556'
        elif internal and parts and parts[0] == 'password_file':
            lines[index] = f'password_file {path}\n'
            replacements += 1
    if replacements != 1:
        raise ValueError('Expected one password_file for listener 7556. Apply the web configuration to regenerate mosquitto.conf.')
    updated = ''.join(lines)
    if updated != config.read_text():
        config.write_text(updated)
    print(f'Internal listener 7556 password file: {path}')


def password_action(action, value, username=None):
    path = password_path(value)
    if action == 'create' and path.exists():
        raise ValueError('Password file already exists; use add/update user.')
    if action != 'create' and not path.is_file():
        raise ValueError('Password file not found.')
    if action == 'users':
        print(f'Password file: {path}')
        for line in path.read_text().splitlines():
            print(line.split(':', 1)[0])
        return
    if action == 'delete':
        delete_asset(path)
        return
    if not isinstance(username, str) or not username or username.startswith('#') or any(c in username for c in ':\r\n\x00'):
        raise ValueError('Invalid MQTT username.')
    old = path.read_bytes().splitlines(keepends=True) if path.exists() else []
    prefix = (username + ':').encode()
    found = any(line.startswith(prefix) for line in old)
    if action == 'remove-user' and not found:
        raise ValueError('User not found.')
    lines = [line for line in old if not line.startswith(prefix)]
    if action in ('create', 'add-user'):
        password = getpass.getpass('Password: ')
        if not password or any(c in password for c in '\r\n\x00'):
            raise ValueError('Password must be nonempty and contain no line breaks.')
        if getpass.getpass('Confirm password: ') != password:
            raise ValueError('Passwords do not match.')
        # Hash only a fresh entry. -U on an existing hashed file corrupts hashes.
        # The secret is never passed in command-line arguments.
        with tempfile.TemporaryDirectory(dir=path.parent) as directory:
            entry = Path(directory) / 'entry'
            entry.write_text(f'{username}:{password}\n')
            entry.chmod(0o600)
            run(['mosquitto_passwd', '-U', str(entry)])
            lines.append(entry.read_bytes())
    atomic_password(path, b''.join(line.rstrip(b'\r\n') + b'\n' for line in lines))
    print(f'Password file: {path}')
    print('Paste this path into password_file_non_tls or password_file_tls in the web panel.')
    print('Apply the web configuration or restart the broker to load user changes.')


def in_use(path):
    """Fail closed when configuration cannot be checked before deletion."""
    config = json.loads(checked(APP_DIR / 'json.json').read_text())
    targets = {path.resolve()} if path.is_file() else {p.resolve() for p in path.rglob('*') if p.is_file()}
    for key, value in config.items():
        if (key.startswith('password_file') or key in ('certfile', 'keyfile', 'cafile')) and isinstance(value, str):
            if Path(value).resolve() in targets:
                return True
    for line in checked(APP_DIR / 'mosquitto.conf').read_text().splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and parts[0] in ('password_file', 'certfile', 'keyfile', 'cafile'):
            if Path(parts[1]).resolve() in targets:
                return True
    return False


def delete_asset(path):
    if in_use(path):
        raise ValueError('This asset is referenced by the configuration. Change its path in the web panel and apply first.')
    if input(f'Type DELETE to permanently remove {path}: ') != 'DELETE':
        print('Deletion cancelled.')
        return
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    print('Deleted.')


def identity(value, allow_ip=True):
    if allow_ip:
        try:
            return 'IP:' + str(ipaddress.ip_address(value))
        except ValueError:
            pass
    if len(value) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label) for label in value.split('.')):
        raise ValueError('Enter a valid hostname or IP address.')
    if not allow_ip and ('.' not in value or value.replace('.', '').isdigit()):
        raise ValueError('Certbot requires a public DNS domain.')
    return 'DNS:' + value


def certificate_action(action, value, hostname=None):
    path = certificate_path(value)
    if action == 'delete':
        if not path.is_dir():
            raise ValueError('Certificate not found.')
        delete_asset(path)
        return
    san = identity(hostname, allow_ip=action == 'self-signed')
    if path.exists():
        raise ValueError('Certificate name already exists; choose another name.')
    parent = folder(APP_DIR / 'certificates')
    with tempfile.TemporaryDirectory(prefix='.creating-', dir=parent) as directory:
        stage = Path(directory)
        cert, key = stage / 'server.crt', stage / 'server.key'
        if action == 'self-signed':
            run(['openssl', 'req', '-x509', '-newkey', 'rsa:3072', '-sha256', '-nodes',
                 '-days', '365', '-subj', f'/CN={hostname}', '-addext', f'subjectAltName={san}',
                 '-addext', 'extendedKeyUsage=serverAuth', '-keyout', str(key), '-out', str(cert)])
        else:
            print('The domain must point to this server; inbound TCP port 80 must be available.')
            # Certbot prompts for email and explicit acceptance of CA terms itself.
            run(['certbot', 'certonly', '--standalone', '--cert-name', hostname, '-d', hostname])
            live = Path('/etc/letsencrypt/live') / hostname
            shutil.copyfile(live / 'fullchain.pem', cert)
            shutil.copyfile(live / 'privkey.pem', key)
            (stage / 'certbot-domain').write_text(hostname)
        for item in stage.iterdir():
            permissions(item)
        permissions(stage, directory=True)
        stage.rename(path)
    show_certificate(path)


def show_certificate(path):
    print(f'certfile: {path / "server.crt"}')
    print(f'keyfile: {path / "server.key"}')
    print('Files: root:mosquitto 0640; directory: root:mosquitto 0750.')
    if (path / 'certbot-domain').exists():
        print('Use renew in this menu to renew and refresh the project copies.')


def renew_certificate(value):
    path = certificate_path(value)
    domain = checked(path / 'certbot-domain').read_text().strip()
    identity(domain, allow_ip=False)
    run(['certbot', 'renew', '--cert-name', domain])
    live = Path('/etc/letsencrypt/live') / domain
    # Read both renewed files before modifying the existing working pair.
    renewed = {'server.crt': (live / 'fullchain.pem').read_bytes(),
               'server.key': (live / 'privkey.pem').read_bytes()}
    previous = {target: checked(path / target).read_bytes() for target in renewed}
    try:
        for target, content in renewed.items():
            atomic_password(path / target, content, mosquitto_owned=False)
    except OSError:
        for target, content in previous.items():
            atomic_password(path / target, content, mosquitto_owned=False)
        raise
    show_certificate(path)
    print('Apply the web configuration or restart the broker to load the renewed certificate.')


def list_assets(kind):
    directory = checked(APP_DIR / ('passwords' if kind == 'password' else 'certificates'))
    print(f'Managed directory: {directory}')
    if not directory.exists():
        print('No assets yet.')
        return
    for path in sorted(directory.iterdir()):
        if path.name.startswith('.'):
            continue
        checked(path)
        if kind == 'password' and path.suffix == '.passwd':
            print(f'{path.stem}: {path}')
        elif kind == 'certificate' and path.is_dir():
            print(f'Certificate: {path.name}')
            show_certificate(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=['password', 'certificate', 'internal'])
    parser.add_argument('action')
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('Run through iot-nexus with sudo/root.')
    try:
        with locked():
            if args.kind == 'internal':
                if args.action != 'repair':
                    raise ValueError('Invalid internal credential action.')
                repair_internal_password()
                return
            if args.action == 'list':
                list_assets(args.kind)
                return
            value = name(input('File/set name (without extension): ').strip())
            if args.kind == 'password':
                if args.action not in ('create', 'add-user', 'remove-user', 'users', 'delete'):
                    raise ValueError('Invalid password action.')
                username = input('MQTT username: ').strip() if args.action in ('create', 'add-user', 'remove-user') else None
                password_action(args.action, value, username)
            elif args.action in ('self-signed', 'certbot'):
                certificate_action(args.action, value, input('Server hostname or IP: ').strip())
            elif args.action == 'delete':
                certificate_action(args.action, value)
            elif args.action == 'renew':
                renew_certificate(value)
            else:
                raise ValueError('Invalid certificate action.')
    except (OSError, ValueError, KeyError, subprocess.SubprocessError, EOFError) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
