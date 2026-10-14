import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import serialization
import mosquitto_assets as assets


class AssetTests(unittest.TestCase):
    def internal_config(self):
        (self.base / 'mosquitto.conf').write_text(
            "listener 7556 127.0.0.1\nallow_anonymous false\n"
            "password_file /opt/mqtt-gui/.mqtt_passwords'\n"
            "listener 1883\npassword_file /external.passwd\n")

    def test_internal_password_migrates_hashes_and_repairs_only_internal_path(self):
        self.internal_config()
        legacy = self.base / '.mqtt_passwords'
        legacy.write_bytes(b'internal:existing-hash\n')
        with assets.locked():
            assets.repair_internal_password()
        destination = self.base / 'passwords' / '.mqtt_passwords'
        self.assertEqual(destination.read_bytes(), legacy.read_bytes())
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        assets.os.chown.assert_any_call(destination, 123, os.getgid())
        config = (self.base / 'mosquitto.conf').read_text()
        self.assertIn(f'password_file {destination}\n', config)
        self.assertIn('password_file /external.passwd\n', config)
        with assets.locked(), patch.object(assets, 'run') as run:
            assets.repair_internal_password()
        run.assert_not_called()

    @unittest.skipUnless(shutil.which('mosquitto_passwd'), 'mosquitto_passwd required')
    def test_missing_internal_password_rebuilt_from_same_connector_credentials(self):
        self.internal_config()
        credentials = self.base / '.mqtt_credentials'
        content = 'username=svc_internal\npassword=existing-secret\n'
        credentials.write_text(content)
        with assets.locked():
            assets.repair_internal_password()
        destination = self.base / 'passwords' / '.mqtt_passwords'
        self.assertTrue(destination.read_text().startswith('svc_internal:'))
        self.assertNotIn('existing-secret', destination.read_text())
        self.assertEqual(credentials.read_text(), content)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        assets.os.chown.assert_any_call(destination, 123, os.getgid())

    def test_internal_repair_reports_missing_credentials(self):
        self.internal_config()
        with assets.locked(), self.assertRaisesRegex(ValueError, 'credentials are missing'):
            assets.repair_internal_password()
        self.assertFalse((self.base / 'passwords' / '.mqtt_passwords').exists())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        for patcher in (patch.object(assets, 'APP_DIR', self.base),
                        patch.object(assets.os, 'chown'),
                        patch.object(assets.pwd, 'getpwnam'),
                        patch.object(assets.grp, 'getgrnam')):
            mock = patcher.start()
            if patcher.attribute == 'getgrnam':
                mock.return_value.gr_gid = os.getgid()
            if patcher.attribute == 'getpwnam':
                mock.return_value.pw_uid = 123
            self.addCleanup(patcher.stop)
        (self.base / 'json.json').write_text('{}')
        (self.base / 'mosquitto.conf').write_text('')

    @unittest.skipUnless(shutil.which('mosquitto_passwd'), 'mosquitto_passwd required')
    def test_real_password_hashing_preserves_existing_users(self):
        with assets.locked(), patch('getpass.getpass', side_effect=['secret1', 'secret1']):
            assets.password_action('create', 'devices', 'alice')
        path = assets.password_path('devices')
        original = path.read_bytes()
        self.assertNotIn(b'secret1', original)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertTrue(any(call.args[1:] == (123, os.getgid()) for call in assets.os.chown.call_args_list))
        with assets.locked(), patch('getpass.getpass', side_effect=['secret2', 'secret2']):
            assets.password_action('add-user', 'devices', 'bob')
        self.assertTrue(path.read_bytes().startswith(original))
        with assets.locked(), patch('getpass.getpass', side_effect=['new-secret', 'new-secret']):
            assets.password_action('add-user', 'devices', 'alice')
        self.assertEqual(path.read_bytes().count(b'alice:'), 1)
        self.assertNotIn(original, path.read_bytes())
        with assets.locked():
            assets.password_action('remove-user', 'devices', 'bob')
        self.assertNotIn(b'bob:', path.read_bytes())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            assets.password_action('create', 'devices', 'new')

    def test_paths_and_symlinks_are_rejected(self):
        for value in ('../secret', '/etc/shadow', '', '-invalid', 'a\nb'):
            with self.assertRaises(ValueError):
                assets.password_path(value)
        (self.base / 'passwords').symlink_to(self.base)
        with self.assertRaises(ValueError):
            assets.password_path('test')

    def test_deletion_checks_json_and_generated_configuration(self):
        folder = assets.folder(self.base / 'passwords')
        path = folder / 'devices.passwd'
        path.write_text('alice:hash\n')
        (self.base / 'json.json').write_text(json.dumps({'password_file_tls': str(path)}))
        with self.assertRaises(ValueError):
            assets.delete_asset(path)
        (self.base / 'json.json').write_text('{}')
        (self.base / 'mosquitto.conf').write_text(f'password_file {path}\n')
        with self.assertRaises(ValueError):
            assets.delete_asset(path)
        (self.base / 'mosquitto.conf').write_text('')
        with patch('builtins.input', return_value='no'):
            assets.delete_asset(path)
        self.assertTrue(path.exists())
        with patch('builtins.input', return_value='DELETE'):
            assets.delete_asset(path)
        self.assertFalse(path.exists())

    @unittest.skipUnless(shutil.which('openssl'), 'OpenSSL required')
    def test_real_self_signed_certificate_has_san_and_matching_key(self):
        # Silence OpenSSL's key-generation progress in test output.
        def quiet_run(args):
            subprocess.run(args, check=True, capture_output=True, timeout=30)
        with patch.object(assets, 'run', side_effect=quiet_run):
            assets.certificate_action('self-signed', 'local', '192.0.2.10')
        path = assets.certificate_path('local')
        cert = x509.load_pem_x509_certificate((path / 'server.crt').read_bytes())
        key = serialization.load_pem_private_key((path / 'server.key').read_bytes(), None)
        self.assertEqual(cert.public_key().public_numbers(), key.public_key().public_numbers())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertEqual(str(san.get_values_for_type(x509.IPAddress)[0]), '192.0.2.10')
        self.assertEqual((path / 'server.key').stat().st_mode & 0o777, 0o640)
        self.assertEqual(path.stat().st_mode & 0o777, 0o750)
        with self.assertRaises(ValueError):
            assets.certificate_action('self-signed', 'local', 'localhost')

    def test_failed_certificate_creation_cleans_up(self):
        with patch.object(assets, 'run', side_effect=subprocess.CalledProcessError(1, 'openssl')):
            with self.assertRaises(subprocess.CalledProcessError):
                assets.certificate_action('self-signed', 'failed', 'localhost')
        self.assertFalse(assets.certificate_path('failed').exists())
        self.assertEqual(list((self.base / 'certificates').iterdir()), [])

    def test_certbot_rejects_ip_and_bad_domains(self):
        for hostname in ('192.0.2.1', 'localhost', '-example.org', 'a/b', 'example.org\n'):
            with self.assertRaises(ValueError):
                assets.certificate_action('certbot', 'public', hostname)

    def test_certbot_requests_domain_and_copies_protected_files(self):
        def copy_certificate(source, target):
            Path(target).write_bytes(b'key' if Path(source).name == 'privkey.pem' else b'certificate')
        with patch.object(assets, 'run') as run, patch.object(assets.shutil, 'copyfile', side_effect=copy_certificate):
            assets.certificate_action('certbot', 'public', 'mqtt.example.org')
        run.assert_called_once_with(['certbot', 'certonly', '--standalone', '--cert-name',
                                     'mqtt.example.org', '-d', 'mqtt.example.org'])
        path = assets.certificate_path('public')
        self.assertEqual((path / 'server.crt').read_bytes(), b'certificate')
        self.assertEqual((path / 'server.key').read_bytes(), b'key')
        self.assertEqual((path / 'certbot-domain').read_text(), 'mqtt.example.org')
        self.assertEqual((path / 'server.key').stat().st_mode & 0o777, 0o640)

    def test_listing_prints_paths_without_private_contents(self):
        directory = assets.folder(self.base / 'passwords')
        (directory / 'devices.passwd').write_text('alice:private-hash')
        with patch('sys.stdout', new_callable=io.StringIO) as output:
            assets.list_assets('password')
        self.assertIn(str(directory / 'devices.passwd'), output.getvalue())
        self.assertNotIn('private-hash', output.getvalue())


if __name__ == '__main__':
    unittest.main()
