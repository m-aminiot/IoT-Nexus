"""Run with: python3 -m unittest discover -s tests -v (no root required)."""
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from cryptography.exceptions import InvalidTag
import vpn_crypto as crypto
import set_mosquitto
import json


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("runner_vpn", ROOT / "runner_vpn.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


class BrokerConfigTests(unittest.TestCase):
    def test_all_ports_reject_privileged_and_invalid_values_without_overwriting_config(self):
        config = json.loads((ROOT / 'json.json').read_text())
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            destination = base / 'mosquitto.conf'
            destination.write_text('existing configuration\n')
            with patch.object(set_mosquitto, 'BASE_DIR', base):
                for key in set_mosquitto.PORT_KEYS:
                    for port in (0, 90, 1023, 65536, True, '1883', 1883.5):
                        with self.subTest(key=key, port=port):
                            with self.assertRaisesRegex(ValueError, '1024 and 65535'):
                                set_mosquitto.write({**config, key: port})
                            self.assertEqual(destination.read_text(), 'existing configuration\n')

    def test_port_boundaries_and_reserved_internal_port(self):
        config = json.loads((ROOT / 'json.json').read_text())
        for key in set_mosquitto.PORT_KEYS:
            for port in (1024, 65535):
                set_mosquitto.validate_ports({**config, key: port})
        with self.assertRaisesRegex(ValueError, '7556 is reserved'):
            set_mosquitto.validate_ports({**config, 'port_NonTls(mqtt)': 7556})

    def test_tls_and_non_tls_have_independent_authentication(self):
        text = self.render(active_TLS=True, active_websocket=True,
                           allow_anonymous_non_tls=True, allow_anonymous_tls=False,
                           password_file_non_tls='/non-tls.passwd', password_file_tls='/tls.passwd')
        listeners = text.split('\nlistener ')[1:]
        self.assertIn('allow_anonymous true', listeners[1])
        self.assertNotIn('password_file ', listeners[1])
        self.assertIn('allow_anonymous false', listeners[2])
        self.assertIn('password_file /tls.passwd', listeners[2])
        self.assertIn('allow_anonymous true', listeners[3])
        text = self.render(active_TLS=True, allow_anonymous_non_tls=False,
                           allow_anonymous_tls=True, password_file_non_tls='/non-tls.passwd')
        listeners = text.split('\nlistener ')[1:]
        self.assertIn('password_file /non-tls.passwd', listeners[1])
        self.assertNotIn('password_file ', listeners[2])

    def test_legacy_configuration_preserves_policy(self):
        config = set_mosquitto.normalize_config({'allow_anonymous': False, 'password_file': '/old.passwd'})
        self.assertNotIn('allow_anonymous', config)
        self.assertNotIn('password_file', config)
        for listener in ('non_tls', 'tls'):
            self.assertFalse(config[f'allow_anonymous_{listener}'])
            self.assertEqual(config[f'password_file_{listener}'], '/old.passwd')

    def render(self, **changes):
        config = json.loads((ROOT / "json.json").read_text())
        config.update(changes)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(set_mosquitto, "BASE_DIR", Path(directory)):
                set_mosquitto.write(config)
            return (Path(directory) / "mosquitto.conf").read_text()

    def test_password_files_are_scoped_to_each_listener(self):
        text = self.render(allow_anonymous_non_tls=False, allow_anonymous_tls=False, active_TLS=True, active_websocket=True,
                           password_file_non_tls="/etc/mosquitto/devices.passwd", password_file_tls="/etc/mosquitto/devices.passwd")
        self.assertTrue(text.startswith("per_listener_settings true\n"))
        listeners = text.split("\nlistener ")[1:]
        self.assertEqual(len(listeners), 4)
        for listener in listeners:
            self.assertEqual(listener.count("password_file "), 1)
            self.assertIn("allow_anonymous false", listener)
        self.assertIn("/passwords/.mqtt_passwords", listeners[0])
        for listener in listeners[1:]:
            self.assertIn("/etc/mosquitto/devices.passwd", listener)

    def test_anonymous_public_listener_keeps_internal_listener_private(self):
        text = self.render(allow_anonymous_non_tls=True, active_TLS=False, active_websocket=False)
        listeners = text.split("\nlistener ")[1:]
        self.assertEqual(len(listeners), 2)
        self.assertIn("allow_anonymous false", listeners[0])
        self.assertIn("password_file ", listeners[0])
        self.assertIn("allow_anonymous true", listeners[1])
        self.assertNotIn("password_file ", listeners[1])

    @unittest.skipUnless(shutil.which("mosquitto") and shutil.which("mosquitto_passwd"),
                         "Mosquitto executables are needed for the integration test")
    def test_generated_config_starts_real_broker_with_two_password_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / 'passwords').mkdir()
            for name in ("passwords/.mqtt_passwords", "devices.passwd"):
                subprocess.run([shutil.which("mosquitto_passwd"), "-b", "-c",
                                str(base / name), "test-user", "test-only-password"],
                               check=True, capture_output=True)
            with socket.socket() as first, socket.socket() as second:
                first.bind(("127.0.0.1", 0))
                second.bind(("127.0.0.1", 0))
                internal, external = first.getsockname()[1], second.getsockname()[1]
            config = json.loads((ROOT / "json.json").read_text())
            config.update({"allow_anonymous_non_tls": False, "active_TLS": False,
                           "active_websocket": False, "port_NonTls(mqtt)": external,
                           "password_file_non_tls": str(base / "devices.passwd")})
            with patch.object(set_mosquitto, "BASE_DIR", base):
                set_mosquitto.write(config)
            path = base / "mosquitto.conf"
            path.write_text(path.read_text().replace("listener 7556 127.0.0.1",
                            f"listener {internal} 127.0.0.1").replace(
                            f"listener {external}\n", f"listener {external} 127.0.0.1\n"))
            process = subprocess.Popen([shutil.which("mosquitto"), "-c", str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            try:
                for _ in range(50):
                    if process.poll() is not None:
                        self.fail(process.communicate()[0])
                    try:
                        with socket.create_connection(("127.0.0.1", internal), timeout=0.1), socket.create_connection(("127.0.0.1", external), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.05)
                else:
                    self.fail("Broker did not open both configured listeners")
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


class MenuTests(unittest.TestCase):
    def test_broker_state_is_shown_below_main_menu(self):
        result, _, _ = self.run_shell('main_menu')
        self.assertIn('Broker: RUN', result.stdout)
        self.assertGreater(result.stdout.index('Broker: RUN'), result.stdout.index('7 - mosquitto'))
        result, _, _ = self.run_shell('broker_summary', active=False)
        self.assertIn('Broker: STOPPED', result.stdout)
        result, _, _ = self.run_shell('systemctl() { [[ "$1" == is-failed ]]; }; broker_summary')
        self.assertIn('Broker: ERROR', result.stdout)

    def test_direct_broker_restart_checks_readiness_and_keeps_other_services_running(self):
        result, _, log = self.run_shell('broker_ready() { return 0; }; main', '8\n0\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('restarted and listening on port 7556', result.stdout)
        self.assertIn('restart iot-gateway-broker.service', log)
        self.assertIn('repair internal credentials', log)
        self.assertNotIn('restart iot-gateway-web.service', log)
        self.assertNotIn('restart iot-gateway-websocket.service', log)

    def test_failed_broker_restart_reports_error_and_logs(self):
        result, _, _ = self.run_shell('broker_ready() { return 1; }; restart_broker')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('Broker: ERROR', result.stdout)
        self.assertIn('service failed to bind', result.stdout)

    def test_missing_internal_credentials_prevent_broker_restart(self):
        result, _, log = self.run_shell('prepare_broker_credentials() { return 1; }; restart_broker')
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn('restart iot-gateway-broker.service', log)

    def test_mosquitto_submenus_dispatch_and_return(self):
        result, _, _ = self.run_shell('mosquitto_asset_action() { echo "asset $*"; }; main',
                                     '7\n1\n1\n2\n3\n4\n5\n6\n0\n2\n1\n2\n3\n4\n5\n0\n0\n0\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('7 - mosquitto', result.stdout)
        for action in ('create', 'add-user', 'remove-user', 'list', 'users', 'delete'):
            self.assertIn(f'asset password {action}', result.stdout)
        for action in ('self-signed', 'certbot', 'list', 'delete', 'renew'):
            self.assertIn(f'asset certificate {action}', result.stdout)

    def run_shell(self, commands, inputs="", active=True, socket_pid=321):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "websocket.env"
            log = Path(directory) / "actions"
            uninstall_script = Path(directory) / "uninstall.sh"
            uninstall_script.write_text('echo "uninstall executed"\n')
            script = r'''
source "$MENU"
VPN_CONFIG="$TEST_CONFIG"
PANEL_CONFIG="$TEST_PANEL_CONFIG"
UNINSTALL_SCRIPT="$TEST_UNINSTALL"
eval "$(declare -f repair_panel_permissions | sed '1s/repair_panel_permissions/real_repair_panel_permissions/')"
eval "$(declare -f configure_panel_port | sed '1s/configure_panel_port/real_configure_panel_port/')"
as_root() { "$@"; }
systemctl() {
    printf '%s\n' "$*" >> "$TEST_LOG"
    case "$1" in
        is-active)
            if [[ "$*" == *iot-gateway-web.service* ]]; then
                [[ "$TEST_WEB_ACTIVE" == 1 ]]
            else
                [[ "$TEST_ACTIVE" == 1 ]]
            fi ;;
        is-failed) return 1 ;;
        show) echo 321 ;;
        start|restart)
            if [[ "$*" == *iot-gateway-web.service* ]]; then
                TEST_WEB_ACTIVE=1
                TEST_WEB_PORT=$(sed -n 's/^PORT=//p' "$PANEL_CONFIG")
            elif [[ "$*" == *iot-gateway-websocket.service* ]]; then
                TEST_ACTIVE=1; TEST_PID=321
                TEST_VPN_PORT=$(sed -n 's/^VPN_PORT=//p' "$VPN_CONFIG")
                TEST_VPN_PORT=${TEST_VPN_PORT:-8765}
            fi ;;
        stop)
            if [[ "$*" == *iot-gateway-web.service* ]]; then TEST_WEB_ACTIVE=0; fi
            if [[ "$*" == *iot-gateway-websocket.service* ]]; then TEST_ACTIVE=0; fi ;;
        *) return 0 ;;
    esac
}
ss() {
    local requested_port='' argument
    for argument in "$@"; do
        if [[ "$argument" == 'sport = :'* ]]; then requested_port=${argument##*:}; fi
    done
    if [[ -z "$requested_port" || "$requested_port" == "$TEST_WEB_PORT" ]]; then
        if [[ "$TEST_WEB_ACTIVE" == 1 ]]; then
            echo "LISTEN 0 128 0.0.0.0:$TEST_WEB_PORT 0.0.0.0:* users:((python,pid=321,fd=8))"
        fi
    fi
    if [[ -n "$requested_port" && "$requested_port" == "$TEST_VPN_PORT" && "$TEST_ACTIVE" == 1 ]]; then
        echo "LISTEN 0 128 0.0.0.0:$TEST_VPN_PORT 0.0.0.0:* users:((python,pid=$TEST_PID,fd=8))"
    fi
}
install() { printf 'install %s\n' "$*" >> "$TEST_LOG"; }
sleep() { :; }
journalctl() { echo 'service failed to bind'; }
ip() { echo '2: eth0 inet 192.0.2.10/24 scope global eth0'; }
panel_http_ready() { [[ "$TEST_WEB_ACTIVE" == 1 ]]; }
repair_panel_permissions() { echo 'repair permissions' >> "$TEST_LOG"; }
prepare_broker_credentials() { echo 'repair internal credentials' >> "$TEST_LOG"; }
configure_panel_port() { printf 'PORT=%s\n' "$panel_port" > "$PANEL_CONFIG"; }
''' + commands
            result = subprocess.run(
                ["bash", "-c", script], input=inputs, text=True,
                capture_output=True, timeout=10,
                env={**os.environ, "MENU": str(ROOT / "iot-gateway.sh"),
                     "TEST_CONFIG": str(env_file), "TEST_LOG": str(log),
                     "TEST_PANEL_CONFIG": str(Path(directory) / "panel.env"),
                     "TEST_DIRECTORY": directory,
                     "TEST_UNINSTALL": str(uninstall_script),
                     "TEST_ACTIVE": "1" if active else "0",
                     "TEST_WEB_ACTIVE": "1" if active else "0",
                     "TEST_WEB_PORT": "9000", "TEST_VPN_PORT": "8765",
                     "TEST_PID": str(socket_pid)},
            )
            return result, env_file.read_text() if env_file.exists() else "", log.read_text() if log.exists() else ""

    def test_main_menu_actions_and_exit(self):
        result, _, log = self.run_shell("main", "4\n9000\n3\n9000\n5\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("1 - websocket", result.stdout)
        self.assertIn("2 - panel management", result.stdout)
        self.assertIn("start iot-gateway-broker.service\n", log)
        self.assertIn("start iot-gateway-web.service\n", log)
        self.assertIn("restart iot-gateway-broker.service\n", log)
        self.assertIn("restart iot-gateway-web.service\n", log)
        self.assertIn("stop iot-gateway-web.service iot-gateway-broker.service iot-gateway-websocket.service", log)

    def test_port_validation_and_leading_zeros(self):
        result, _, _ = self.run_shell('read_port && echo "selected=$port"', "abc\n0\n65536\n0080\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("Enter a port"), 3)
        self.assertIn("selected=80", result.stdout)

    def test_status_requires_matching_process(self):
        result, _, _ = self.run_shell("port=8765; vpn_status", socket_pid=999)
        self.assertIn("NOT LISTENING", result.stdout)

    def test_status_reports_our_listener(self):
        result, _, _ = self.run_shell("port=8765; vpn_status")
        self.assertIn("RUNNING: runner_vpn.py", result.stdout)

    def test_inactive_service_is_not_listening(self):
        result, _, _ = self.run_shell("port=8765; vpn_status", active=False)
        self.assertIn("NOT LISTENING", result.stdout)

    def test_start_saves_environment_and_starts_service(self):
        result, config, log = self.run_shell("port=9010; vpn_start", active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config, "VPN_PORT=9010\n")
        self.assertIn("restart iot-gateway-websocket.service", log)
        self.assertIn("Started WebSocket VPN on port 9010", result.stdout)

    def test_busy_port_does_not_restart_service(self):
        result, config, log = self.run_shell("port=8765; vpn_start", socket_pid=999)
        self.assertEqual(result.returncode, 1)
        self.assertIn("already in use", result.stdout)
        self.assertNotIn("restart", log)
        self.assertEqual(config, "")

    def test_already_running_does_not_restart(self):
        result, _, log = self.run_shell("port=8765; vpn_start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Already running", result.stdout)
        self.assertNotIn("restart", log)

    def test_vpn_restart_restarts_existing_listener(self):
        result, config, log = self.run_shell("port=8765; vpn_start 1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(config, "VPN_PORT=8765\n")
        self.assertIn("restart iot-gateway-websocket.service", log)

    def test_gateway_restart_includes_active_vpn(self):
        result, _, log = self.run_shell("manage_gateway restart", "9000\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart iot-gateway-websocket.service\n", log)

    def test_gateway_restart_leaves_stopped_vpn_stopped(self):
        result, _, log = self.run_shell("manage_gateway restart", "9000\n", active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart iot-gateway-broker.service\n", log)
        self.assertNotIn("restart iot-gateway-websocket.service\n", log)

    def test_vpn_stop_from_submenu(self):
        result, _, log = self.run_shell("websocket_menu", "4\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stop iot-gateway-websocket.service", log)
        self.assertIn("WebSocket VPN stopped", result.stdout)

    def test_key_menu_dispatch_and_active_rotation(self):
        result, _, log = self.run_shell('''vpn_key_action() { echo "key action=$1"; }
websocket_menu''', "5\n1\n2\n3\n4\n0\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for action in ("status", "show", "set", "generate"):
            self.assertIn("key action=" + action, result.stdout)
        result, _, log = self.run_shell('VPN_PYTHON=true; vpn_key_action generate')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart iot-gateway-websocket.service", log)

    def test_key_failure_does_not_restart_and_stopped_vpn_stays_stopped(self):
        result, _, log = self.run_shell('VPN_PYTHON=false; vpn_key_action set')
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("restart", log)
        self.assertNotIn("Key saved", result.stdout)
        result, _, log = self.run_shell('VPN_PYTHON=true; vpn_key_action generate', active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("restart", log)

    def test_start_failure_shows_logs(self):
        result, _, _ = self.run_shell('ss() { :; }; port=8765; vpn_start', active=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("service failed to bind", result.stdout)

    def test_submenu_back_and_eof(self):
        result, _, _ = self.run_shell("main", "1\n0\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("2 - status", result.stdout)
        result, _, _ = self.run_shell("main")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_panel_actions_manage_only_web_service(self):
        result, _, log = self.run_shell("main", "2\n1\n9000\n2\n9000\n3\n0\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for action in ("start", "restart", "stop"):
            self.assertIn(f"{action} iot-gateway-web.service\n", log)
        for action in ('start', 'restart', 'stop'):
            self.assertNotIn(f'{action} iot-gateway-broker.service', log)
        self.assertNotIn("iot-gateway-websocket.service", log)

    def test_panel_url_uses_actual_port_and_matches_process(self):
        result, _, _ = self.run_shell('''ss() {
echo 'LISTEN 0 128 0.0.0.0:8088 0.0.0.0:* users:((python,pid=321,fd=8))'
echo 'LISTEN 0 128 0.0.0.0:9999 0.0.0.0:* users:((python,pid=999,fd=8))'
}; panel_status''')
        self.assertIn("http://127.0.0.1:8088", result.stdout)
        self.assertIn("http://192.0.2.10:8088", result.stdout)
        self.assertNotIn("9999", result.stdout)

    def test_panel_stopped_does_not_report_url(self):
        result, _, _ = self.run_shell("panel_status", active=False)
        self.assertIn("Panel is not running", result.stdout)
        self.assertNotIn("http://", result.stdout)

    def test_active_panel_without_socket_does_not_guess_url(self):
        result, _, _ = self.run_shell('ss() { :; }; panel_status')
        self.assertIn("not ready", result.stdout)
        self.assertNotIn("http://", result.stdout)

    def test_panel_ipv6_url(self):
        result, _, _ = self.run_shell('''ss() {
echo 'LISTEN 0 128 [2001:db8::1]:9000 [::]:* users:((python,pid=321,fd=8))'
}; panel_status''')
        self.assertIn("http://[2001:db8::1]:9000", result.stdout)

    def test_uninstall_requires_confirmation(self):
        result, _, _ = self.run_shell("main", "6\nno\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Uninstall cancelled", result.stdout)
        self.assertNotIn("uninstall executed", result.stdout)

    def test_uninstall_runs_script_and_exits_menu(self):
        result, _, _ = self.run_shell("main", "6\nUNINSTALL\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("uninstall executed", result.stdout)
        self.assertIn("has been uninstalled", result.stdout)
        self.assertEqual(result.stdout.count("6 - uninstall"), 1)

    def test_uninstall_missing_or_failed_is_not_reported_as_success(self):
        result, _, _ = self.run_shell('UNINSTALL_SCRIPT=/missing/uninstall.sh; uninstall_gateway')
        self.assertEqual(result.returncode, 1)
        self.assertIn("not found", result.stdout)
        result, _, _ = self.run_shell('as_root() { return 1; }; uninstall_gateway', "UNINSTALL\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Uninstall failed", result.stdout)
        self.assertNotIn("has been uninstalled", result.stdout)

    def test_panel_manual_port_reaches_saved_config_and_url(self):
        result, _, log = self.run_shell('manage_panel start; cat "$PANEL_CONFIG"', "8088\n", active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORT=8088", result.stdout)
        self.assertIn("http://192.0.2.10:8088", result.stdout)
        self.assertIn("Panel is ready", result.stdout)
        self.assertIn("repair permissions", log)

    def test_panel_random_port_is_saved(self):
        result, _, _ = self.run_shell('random_panel_port() { echo 54321; }; manage_panel start; cat "$PANEL_CONFIG"', "r\n", active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORT=54321", result.stdout)
        self.assertIn("http://192.0.2.10:54321", result.stdout)

    def test_panel_rejects_invalid_and_busy_port(self):
        result, _, _ = self.run_shell('''ss() {
    if [[ "$*" == *'sport = :8000'* ]]; then echo 'LISTEN 0 128 0.0.0.0:8000'; fi
}; read_panel_port; echo "selected=$panel_port"''', "invalid\n65536\n8000\n8088\n", active=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("already in use", result.stdout)
        self.assertIn("selected=8088", result.stdout)

    def test_start_changes_port_of_running_panel(self):
        result, _, log = self.run_shell('manage_panel start', "8088\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("restart iot-gateway-web.service", log)
        self.assertIn("http://192.0.2.10:8088", result.stdout)

    def test_panel_crash_is_not_reported_as_success(self):
        result, _, _ = self.run_shell('''panel_http_ready() { return 1; }
systemctl() {
    case "$1" in
        is-active|is-failed) return 1 ;;
        show) echo 321 ;;
        *) return 0 ;;
    esac
}
manage_panel start''', "8088\n", active=False)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("Panel startup failed", result.stdout)
        self.assertIn("service failed to bind", result.stdout)
        self.assertNotIn("Panel is ready", result.stdout)
        self.assertNotIn("http://", result.stdout)

    def test_panel_checks_http_in_addition_to_listening_socket(self):
        result, _, _ = self.run_shell('panel_http_ready() { return 1; }; manage_panel start', "9000\n")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("startup failed", result.stdout)
        self.assertNotIn("Panel is ready", result.stdout)

    def test_menus_are_not_appended_for_every_selection(self):
        result, _, _ = self.run_shell("main", "2\n4\n4\n0\n0\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("4 - status / URL"), 1)
        self.assertIn("iot-nexus", result.stdout)

    def test_readable_modules_and_private_databases_after_permission_repair(self):
        result, _, _ = self.run_shell('''APP_DIR="$TEST_DIRECTORY/app"
mkdir -p "$APP_DIR/templates"
printf '# test module\n' > "$APP_DIR/login_signup.py"
printf '# test entry\n' > "$APP_DIR/main.py"
printf 'template\n' > "$APP_DIR/templates/login.html"
printf 'data\n' > "$APP_DIR/device_status.db"
chmod 600 "$APP_DIR/login_signup.py" "$APP_DIR/main.py" "$APP_DIR/templates/login.html"
chmod 700 "$APP_DIR/templates"
as_root() { if [[ "$1" == chown ]]; then return 0; fi; "$@"; }
real_repair_panel_permissions || exit 1
python3 - "$APP_DIR" <<'PY'
import stat, sys
from pathlib import Path
base = Path(sys.argv[1])
for name, mode in [('login_signup.py', 0o644), ('main.py', 0o644),
                   ('templates', 0o755), ('templates/login.html', 0o644),
                   ('device_status.db', 0o600)]:
    assert stat.S_IMODE((base / name).stat().st_mode) == mode, name
assert (base / 'device_status.db').read_text() == 'data\\n'
print('permissions repaired; data preserved')
PY''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("data preserved", result.stdout)

    def test_existing_service_gets_environment_dropin(self):
        result, _, log = self.run_shell('''PANEL_DROPIN="$TEST_DIRECTORY/10-panel.conf"
panel_port=8088
real_configure_panel_port || exit 1
cat "$PANEL_CONFIG" "$PANEL_DROPIN"''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PORT=8088", result.stdout)
        self.assertIn("[Service]", result.stdout)
        self.assertIn("EnvironmentFile=", result.stdout)
        self.assertIn("daemon-reload", log)


class PanelIntegrationTests(unittest.TestCase):
    def test_config_api_rejects_low_ports_before_saving_or_restarting(self):
        with tempfile.TemporaryDirectory() as directory:
            copied_app = Path(directory)
            for name in ('main.py', 'login_signup.py', 'connector.py', 'device_status.py', 'set_mosquitto.py', 'json.json'):
                shutil.copy2(ROOT / name, copied_app / name)
            result = subprocess.run([sys.executable, '-c', '''
import json
from unittest.mock import patch
import main
original = main.CONFIG_PATH.read_bytes()
config = main.read_config()
client = main.app.test_client()
with client.session_transaction() as session:
    session['logged_in'] = True
with patch.object(main.set_mosquitto, 'write') as write, patch.object(main.subprocess, 'run') as restart:
    for key in main.set_mosquitto.PORT_KEYS:
        for port in (90, 1023, 65536, True, '1883'):
            response = client.post('/api/config', json={**config, key: port})
            assert response.status_code == 400, response.get_json()
            assert '1024 and 65535' in response.get_json()['message']
            assert main.CONFIG_PATH.read_bytes() == original
        for port in (1024, 65535):
            assert main.validate_config({**config, key: port}) is None
    write.assert_not_called()
    restart.assert_not_called()
'''], cwd=copied_app, capture_output=True, text=True, timeout=10,
                env={**os.environ, 'SECRET_KEY': 'test-config', 'PYTHONDONTWRITEBYTECODE': '1'})
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_actual_flask_process_uses_selected_port_and_http_probe(self):
        """Use real application modules and HTTP, without touching project data."""
        with tempfile.TemporaryDirectory() as directory:
            copied_app = Path(directory)
            for name in ('main.py', 'login_signup.py', 'connector.py', 'device_status.py', 'set_mosquitto.py', 'json.json'):
                shutil.copy2(ROOT / name, copied_app / name)
            shutil.copytree(ROOT / 'templates', copied_app / 'templates')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1', 0))
                selected_port = listener.getsockname()[1]
            process = subprocess.Popen(
                [sys.executable, str(copied_app / 'main.py')],
                cwd=copied_app, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, env={**os.environ, 'PORT': str(selected_port),
                               'SECRET_KEY': 'integration-test',
                               'PYTHONDONTWRITEBYTECODE': '1'},
            )
            try:
                for _ in range(40):
                    result = subprocess.run(
                        ['bash', '-c', 'source "$MENU"; PANEL_BOUND_ADDRESS=127.0.0.1; PANEL_BOUND_PORT="$PORT"; panel_http_ready'],
                        env={**os.environ, 'MENU': str(ROOT / 'iot-gateway.sh'), 'PORT': str(selected_port)},
                        capture_output=True, text=True, timeout=3,
                    )
                    if result.returncode == 0:
                        break
                    if process.poll() is not None:
                        self.fail(process.communicate(timeout=1)[0])
                    time.sleep(0.1)
                else:
                    self.fail('Real Flask HTTP readiness check timed out')
                self.assertIsNone(process.poll())
                self.assertTrue((copied_app / 'login_signup.db').exists())
                self.assertTrue((copied_app / 'device_status.db').exists())
            finally:
                if process.poll() is None:
                    process.terminate()
                process.communicate(timeout=5)


class RunnerTests(unittest.IsolatedAsyncioTestCase):
    def test_port_range_and_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(runner.get_port(), 8765)
        for value in ("1", "65535", "9010"):
            with patch.dict(os.environ, {"VPN_PORT": value}):
                self.assertEqual(runner.get_port(), int(value))
        for value in ("", "0", "65536", "abc", "-1", "1.5", "۸۰"):
            with patch.dict(os.environ, {"VPN_PORT": value}):
                with self.assertRaises(ValueError):
                    runner.get_port()

    async def test_invalid_port_does_not_create_tun(self):
        with patch.dict(os.environ, {"VPN_PORT": "0"}), patch.object(runner, "TUNInterface") as tun:
            with self.assertRaises(ValueError):
                await runner.main()
            tun.assert_not_called()

    async def test_selected_port_reaches_websocket_and_tun_closes(self):
        entered = asyncio.Event()
        class Server:
            async def __aenter__(self):
                entered.set()
                return self
            async def __aexit__(self, *args):
                pass
        with patch.dict(os.environ, {"VPN_PORT": "9010"}), patch.object(runner, "load_key", return_value=b"k" * 32), patch.object(runner, "TUNInterface") as tun, patch.object(runner.websockets, "serve", return_value=Server()) as serve:
            task = asyncio.create_task(runner.main())
            await asyncio.wait_for(entered.wait(), timeout=1)
            self.assertEqual(serve.call_args.args[1:], ("0.0.0.0", 9010))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            tun.return_value.close.assert_called_once()


class CryptoTests(unittest.TestCase):
    def pair(self, key=b"k" * 32, client_random=b"c" * 32):
        return (crypto.PacketCipher(key, b"s" * 32, client_random, server=True),
                crypto.PacketCipher(key, b"s" * 32, client_random, server=False))

    def test_roundtrip_both_directions_and_max_size(self):
        server, client = self.pair()
        for data in (b"", b"\x45\x00IP packet", os.urandom(65535)):
            frame = crypto.encrypt_packet(data, server)
            if data:
                self.assertNotEqual(frame[12:-16], data)
            self.assertEqual(crypto.decrypt_packet(frame, client), data)
            self.assertEqual(crypto.decrypt_packet(crypto.encrypt_packet(data, client), server), data)

    def test_tampering_wrong_key_and_reflection_rejected(self):
        server, client = self.pair()
        frame = crypto.encrypt_packet(b"secret", server)
        changed = frame[:-1] + bytes([frame[-1] ^ 1])
        for context, data in ((client, changed), (server, frame),
                              (self.pair(key=b"x" * 32)[1], frame)):
            with self.assertRaises(InvalidTag):
                crypto.decrypt_packet(data, context)
            self.assertEqual(context.rx_sequence, 0)

    def test_replay_reordering_and_previous_session_rejected(self):
        server, client = self.pair()
        first = crypto.encrypt_packet(b"one", server)
        second = crypto.encrypt_packet(b"two", server)
        with self.assertRaises(ValueError):
            crypto.decrypt_packet(second, client)
        crypto.decrypt_packet(first, client)
        with self.assertRaises(ValueError):
            crypto.decrypt_packet(first, client)
        self.assertEqual(crypto.decrypt_packet(second, client), b"two")
        with self.assertRaises(InvalidTag):
            crypto.decrypt_packet(first, self.pair(client_random=b"n" * 32)[1])

    def test_malformed_and_sequence_exhaustion(self):
        server, client = self.pair()
        for frame in ("plaintext", b"", b"x" * 27, b"x" * 65564):
            with self.assertRaises(ValueError):
                crypto.decrypt_packet(frame, client)
        server.tx_sequence = crypto.MAX_SEQUENCE
        with self.assertRaises(ValueError):
            crypto.encrypt_packet(b"packet", server)

    def test_private_key_storage_rotation_and_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vpn.key"
            crypto.save_key(b"a" * 32, path)
            self.assertEqual(crypto.load_key(path), b"a" * 32)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            crypto.save_key(b"b" * 32, path)
            self.assertEqual(crypto.load_key(path), b"b" * 32)
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                crypto.load_key(path)
            link = Path(directory) / "link"
            link.symlink_to(path)
            with self.assertRaises(OSError):
                crypto.load_key(link)
        for key in ("a" * 8, "a" * 63, "z" * 64, "aa " * 32):
            with self.assertRaises(ValueError):
                crypto.parse_key(key)

    def test_install_initialization_preserves_key(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vpn.key"
            # Bind default arguments explicitly for this isolated CLI test.
            with patch.object(crypto, "KEY_FILE", path), patch.object(crypto, "load_key", side_effect=lambda: original_load(path)), patch.object(crypto, "save_key", side_effect=lambda key: original_save(key, path)), patch.object(sys, "argv", ["vpn_crypto.py", "init"]):
                crypto.main()
                before = path.read_bytes()
                crypto.main()
                self.assertEqual(before, path.read_bytes())


original_load = crypto.load_key
original_save = crypto.save_key


class EncryptedRunnerTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_key_fails_before_tun_creation(self):
        with patch.object(runner, "load_key", side_effect=FileNotFoundError), patch.object(runner, "TUNInterface") as tun:
            with self.assertRaises(FileNotFoundError):
                await runner.main()
            tun.assert_not_called()

    async def test_real_websocket_encrypted_tun_roundtrip_and_replay(self):
        read_fd, write_fd = os.pipe()
        tun = Mock(fd=read_fd)
        tun.read.side_effect = lambda size: os.read(read_fd, size)
        lock = asyncio.Lock()
        key = os.urandom(32)
        try:
            async with runner.websockets.serve(lambda ws: runner.handler(ws, tun, lock, key), "127.0.0.1", 0, compression=None) as server:
                port = server.sockets[0].getsockname()[1]
                async with runner.websockets.connect(f"ws://127.0.0.1:{port}", compression=None) as ws:
                    hello = await ws.recv()
                    client_random = os.urandom(32)
                    cipher = crypto.PacketCipher(key, hello[4:], client_random, server=False)
                    await ws.send(crypto.MAGIC + client_random + crypto.encrypt_packet(b"", cipher))
                    self.assertEqual(crypto.decrypt_packet(await ws.recv(), cipher), b"")
                    outbound = b"\x45\x00outgoing IP packet"
                    os.write(write_fd, outbound)
                    frame = await asyncio.wait_for(ws.recv(), 2)
                    self.assertNotIn(outbound, frame)
                    self.assertEqual(crypto.decrypt_packet(frame, cipher), outbound)
                    incoming = b"\x60\x00incoming IP packet"
                    frame = crypto.encrypt_packet(incoming, cipher)
                    await ws.send(frame)
                    for _ in range(100):
                        if tun.write.called:
                            break
                        await asyncio.sleep(0.01)
                    tun.write.assert_called_once_with(incoming)
                    await ws.send(frame)
                    await asyncio.wait_for(ws.wait_closed(), 2)
                    self.assertEqual(ws.close_code, 1008)
                    tun.write.assert_called_once()
            self.assertFalse(lock.locked())
        finally:
            os.close(read_fd)
            os.close(write_fd)

    async def test_bad_authentication_cannot_write_tun(self):
        tun = Mock()
        lock = asyncio.Lock()
        async with runner.websockets.serve(lambda ws: runner.handler(ws, tun, lock, b"k" * 32), "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            for response in (b"plaintext IP", "text", crypto.MAGIC + b"x" * 60):
                async with runner.websockets.connect(f"ws://127.0.0.1:{port}") as ws:
                    await ws.recv()
                    await ws.send(response)
                    await asyncio.wait_for(ws.wait_closed(), 2)
                    self.assertEqual(ws.close_code, 1008)
            tun.write.assert_not_called()
            self.assertFalse(lock.locked())


if __name__ == "__main__":
    unittest.main()
