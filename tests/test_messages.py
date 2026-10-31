"""Tests for the message-topic subscriptions and the message queue.

Run with: python3 -m unittest discover -s tests -v (no root required).
"""
import json
import queue
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import paho.mqtt.client as paho

import connector
import device_status
import main


ROOT = Path(__file__).resolve().parents[1]
BROKER_USER = "test-user"
BROKER_PASSWORD = "test-only-password"


def wait_for(predicate, timeout=15.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class PatchedTestCase(unittest.TestCase):
    """Small helper so every patched attribute is restored after each test."""

    missing = object()

    def patch_attribute(self, target, name, value=missing):
        patcher = patch.object(target, name) if value is self.missing else patch.object(target, name, value)
        patcher.start()
        self.addCleanup(patcher.stop)
        return patcher

    def temporary_directory(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name)


class TopicStoreTests(PatchedTestCase):
    """Topic bookkeeping, without any broker."""

    def setUp(self):
        self.topics_path = self.temporary_directory() / "message_topics.json"
        self.start_listener = self.patch_attribute(connector, "start_listener")
        self.patch_attribute(connector, "TOPICS_PATH", self.topics_path)

    def test_topics_are_added_removed_and_saved_for_later_runs(self):
        self.assertEqual(connector.get_topics(), [])
        connector.add_topic("home/kitchen")
        connector.add_topic("  home/garden  ")
        self.assertEqual(connector.get_topics(), ["home/kitchen", "home/garden"])
        self.assertEqual(json.loads(self.topics_path.read_text()), ["home/kitchen", "home/garden"])

        connector.remove_topic("home/kitchen")
        self.assertEqual(connector.get_topics(), ["home/garden"])
        self.assertEqual(json.loads(self.topics_path.read_text()), ["home/garden"])

    def test_adding_the_same_topic_twice_subscribes_once(self):
        connector.add_topic("home/kitchen")
        connector.add_topic("home/kitchen")
        self.assertEqual(connector.get_topics(), ["home/kitchen"])

    def test_removing_an_unknown_topic_changes_nothing(self):
        connector.add_topic("home/kitchen")
        self.assertEqual(connector.remove_topic("home/other"), ["home/kitchen"])
        self.assertEqual(connector.get_topics(), ["home/kitchen"])

    def test_nonsense_topics_are_rejected_and_nothing_is_saved(self):
        for topic in ("", "   ", "bad\ntopic", "null\x00byte", "x" * 257, None, 123, ["a"]):
            with self.subTest(topic=topic):
                with self.assertRaises(ValueError):
                    connector.add_topic(topic)
                with self.assertRaises(ValueError):
                    connector.remove_topic(topic)
        self.assertEqual(connector.get_topics(), [])
        self.assertFalse(self.topics_path.exists())

    def test_invalid_mqtt_filters_are_rejected_before_anything_is_saved(self):
        for topic in ("home/#/x", "home/#/", "#/home", "home+kitchen", "home/#x"):
            with self.subTest(topic=topic):
                with self.assertRaises(ValueError):
                    connector.add_topic(topic)
        self.assertEqual(connector.get_topics(), [])
        self.assertFalse(self.topics_path.exists())

    def test_valid_filters_are_accepted(self):
        for topic in ("home/kitchen", "home/#", "#", "home/+/temperature", "+/status"):
            with self.subTest(topic=topic):
                connector.add_topic(topic)
        self.assertEqual(connector.get_topics(), ["home/kitchen", "home/#", "#", "home/+/temperature", "+/status"])

    def test_a_hand_edited_topic_file_never_breaks_subscribing(self):
        self.topics_path.write_text(json.dumps(["gateway/ok", "home/#/x", 42]), encoding="utf-8")
        client = Mock()

        connector.on_connect(client, None, None, 0)

        self.assertEqual([call.args[0] for call in client.subscribe.call_args_list], ["isonline", "gateway/ok"])
        self.assertEqual(connector.get_topics(), ["gateway/ok"])
        connector.on_connect(client, None, None, 5)
        self.assertEqual(client.subscribe.call_count, 2)

    def test_a_hand_edited_topic_can_still_be_removed(self):
        self.topics_path.write_text(json.dumps(["gateway/ok", "home/#/x"]), encoding="utf-8")
        connector.remove_topic("home/#/x")
        self.assertEqual(json.loads(self.topics_path.read_text()), ["gateway/ok"])

    def test_topic_is_kept_when_the_broker_is_unreachable(self):
        self.start_listener.side_effect = connector.BrokerConnectionError("down")
        self.assertEqual(connector.add_topic("home/kitchen"), ["home/kitchen"])
        self.assertEqual(connector.get_topics(), ["home/kitchen"])


class TopicApiTests(PatchedTestCase):
    """The web panel endpoints, without any broker."""

    def setUp(self):
        base = self.temporary_directory()
        self.patch_attribute(connector, "TOPICS_PATH", base / "message_topics.json")
        self.patch_attribute(connector, "start_listener")
        self.client = main.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = BROKER_USER

    def test_login_is_required(self):
        anonymous = main.app.test_client()
        self.assertEqual(anonymous.get("/api/topics").status_code, 401)
        self.assertEqual(anonymous.get("/messages").status_code, 302)

    def test_topics_can_be_added_listed_and_removed(self):
        response = self.client.post("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["topics"], ["home/kitchen"])

        response = self.client.get("/api/topics")
        self.assertEqual(response.get_json()["topics"], ["home/kitchen"])
        self.assertIn("connected", response.get_json())

        response = self.client.delete("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["topics"], [])

    def test_invalid_topic_is_rejected_with_a_message(self):
        response = self.client.post("/api/topics", json={"topic": "   "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("error", response.get_json())

    def test_status_says_not_listening_when_the_broker_cannot_be_reached(self):
        reason = "Could not connect to the MQTT broker at localhost:7556 ([Errno 61] Connection refused)"
        connector.start_listener.side_effect = connector.BrokerConnectionError(reason)

        payload = self.client.get("/api/topics").get_json()

        self.assertFalse(payload["connected"])
        self.assertEqual(payload["error"], reason)

    def test_status_says_not_listening_while_the_listener_is_idle(self):
        payload = self.client.post("/api/topics", json={"topic": "home/kitchen"}).get_json()

        self.assertFalse(payload["connected"])
        self.assertEqual(payload["error"], "MQTT listener is not running")

    def test_add_and_remove_succeed_even_when_broker_is_offline(self):
        connector.start_listener.side_effect = connector.BrokerConnectionError("Broker unavailable")
        response = self.client.post("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "status": "ok", "topics": ["home/kitchen"],
            "connected": False, "error": "Broker unavailable",
        })
        self.assertEqual(connector.get_topics(), ["home/kitchen"])
        response = self.client.delete("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["topics"], [])
        self.assertEqual(connector.get_topics(), [])

    def test_unexpected_failures_still_answer_with_json_for_the_log(self):
        with patch.object(connector, "listener_status", side_effect=RuntimeError("status broke")):
            response = self.client.get("/api/topics")
        self.assertEqual(response.status_code, 500)
        self.assertIn("status broke", response.get_json()["error"])

        with patch.object(connector, "add_topic", side_effect=OSError("no space left on device")):
            response = self.client.post("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("no space left on device", response.get_json()["error"])

        with patch.object(connector, "remove_topic", side_effect=OSError("read-only file system")):
            response = self.client.delete("/api/topics", json={"topic": "home/kitchen"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("read-only file system", response.get_json()["error"])


class MessageQueueIntegrationTests(PatchedTestCase):
    """End-to-end checks against a temporary Mosquitto broker."""

    @classmethod
    def setUpClass(cls):
        if not (shutil.which("mosquitto") and shutil.which("mosquitto_passwd")):
            raise unittest.SkipTest("Mosquitto executables are needed for the integration tests")

    def setUp(self):
        self.base = self.temporary_directory()
        self.config_path = self.base / "mosquitto.conf"
        self.password_file = self.base / "devices.passwd"
        self.credentials_path = self.base / ".mqtt_credentials"
        self.port = free_port()
        self.broker = None

        subprocess.run(
            [shutil.which("mosquitto_passwd"), "-b", "-c", str(self.password_file), BROKER_USER, BROKER_PASSWORD],
            check=True, capture_output=True, text=True,
        )
        self.credentials_path.write_text(
            f"username={BROKER_USER}\npassword={BROKER_PASSWORD}\n", encoding="utf-8"
        )
        self.credentials_path.chmod(0o600)

        self.patch_attribute(connector, "CREDENTIALS_PATH", self.credentials_path)
        self.patch_attribute(connector, "TOPICS_PATH", self.base / "message_topics.json")
        self.patch_attribute(connector, "BROKER_HOST", "127.0.0.1")
        self.patch_attribute(connector, "BROKER_PORT", self.port)
        self.addCleanup(self.drain_queue)
        with connector.client_lock:
            connector.mqtt_client = None
        self.addCleanup(self.stop_listener)

        self.start_broker()
        self.addCleanup(self.stop_broker)

    def start_broker(self):
        self.config_path.write_text(
            f"listener {self.port} 127.0.0.1\nallow_anonymous false\n"
            f"password_file {self.password_file}\n",
            encoding="utf-8",
        )
        self.broker = subprocess.Popen(
            [shutil.which("mosquitto"), "-c", str(self.config_path)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        self.assertTrue(self.wait_for_port(), "Broker did not open its listener")

    def wait_for_port(self, timeout=10.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.broker.poll() is not None:
                self.fail(self.broker.communicate()[0])
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=0.2):
                    return True
            except OSError:
                time.sleep(0.05)
        return False

    def stop_broker(self):
        if self.broker is not None and self.broker.poll() is None:
            self.broker.terminate()
            try:
                self.broker.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                self.broker.kill()
                self.broker.communicate(timeout=5)
        self.broker = None

    def stop_listener(self):
        with connector.client_lock:
            client, connector.mqtt_client = connector.mqtt_client, None
        if client is not None:
            client.disconnect()
            client.loop_stop()

    def drain_queue(self):
        while True:
            try:
                connector.message_queue.get_nowait()
            except queue.Empty:
                return

    def queued(self):
        return list(connector.message_queue.queue)

    def publisher(self):
        client = paho.Client()
        client.username_pw_set(BROKER_USER, BROKER_PASSWORD)
        client.reconnect_delay_set(min_delay=1, max_delay=5)
        client.connect("127.0.0.1", self.port, keepalive=60)
        client.loop_start()
        self.addCleanup(self.close_publisher, client)
        return client

    def close_publisher(self, client):
        client.disconnect()
        client.loop_stop()

    def publish(self, client, topic, payload, qos=1):
        info = client.publish(topic, payload, qos=qos)
        info.wait_for_publish(timeout=5)

    def test_message_sent_by_a_simple_broker_user_reaches_the_queue(self):
        self.assertEqual(connector.add_topic("gateway/test"), ["gateway/test"])
        sender = self.publisher()

        self.publish(sender, "gateway/test", "hello-1")

        self.assertTrue(wait_for(lambda: len(self.queued()) == 1), self.queued())
        self.assertEqual(self.queued()[0], {"gateway/test": "hello-1"})

    def test_panel_reports_listening_only_while_the_broker_is_reachable(self):
        connector.add_topic("gateway/status")
        client = main.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True

        payload = client.get("/api/topics").get_json()
        self.assertTrue(payload["connected"], payload)
        self.assertEqual(payload["error"], "")
        self.assertEqual(payload["topics"], ["gateway/status"])

        self.stop_broker()
        self.assertTrue(
            wait_for(lambda: not connector.mqtt_client.is_connected(), timeout=10),
            "Listener did not notice the stopped broker",
        )
        payload = client.get("/api/topics").get_json()
        self.assertFalse(payload["connected"])
        self.assertIn(str(self.port), payload["error"])

    def test_wildcard_topic_receives_matching_messages(self):
        connector.add_topic("gateway/#")
        sender = self.publisher()

        self.publish(sender, "gateway/room/1", "hello-room")

        self.assertTrue(wait_for(lambda: len(self.queued()) == 1), self.queued())
        self.assertEqual(self.queued()[0], {"gateway/room/1": "hello-room"})

    def test_a_wildcard_topic_does_not_break_the_device_status_handler(self):
        self.patch_attribute(device_status, "DATABASE_PATH", self.base / "device_status.db")
        device_status.create_table()
        connector.add_topic("#")
        sender = self.publisher()

        self.publish(sender, "isonline", json.dumps({"name": "esp32-room", "status": "online"}))

        self.assertTrue(wait_for(lambda: len(self.queued()) == 1), self.queued())
        self.assertEqual(self.queued()[0]["isonline"], '{"name": "esp32-room", "status": "online"}')
        self.assertTrue(
            wait_for(lambda: device_status.get_devices().get("esp32-room", {}).get("status") == "online"),
            device_status.get_devices(),
        )

    def test_queued_messages_survive_a_broker_outage_and_subscriptions_return(self):
        connector.add_topic("gateway/keep")
        sender = self.publisher()
        self.publish(sender, "gateway/keep", "first")
        self.assertTrue(wait_for(lambda: len(self.queued()) == 1), self.queued())

        self.stop_broker()
        self.assertTrue(
            wait_for(lambda: not connector.mqtt_client.is_connected(), timeout=10),
            "Listener did not notice the stopped broker",
        )
        self.assertEqual(self.queued(), [{"gateway/keep": "first"}])

        self.start_broker()
        self.assertTrue(
            wait_for(lambda: connector.mqtt_client.is_connected(), timeout=20),
            "Listener did not reconnect to the restarted broker",
        )
        resumed = self.publisher()
        self.publish(resumed, "gateway/keep", "second")

        self.assertTrue(wait_for(lambda: len(self.queued()) == 2), self.queued())
        self.assertEqual(self.queued(), [{"gateway/keep": "first"}, {"gateway/keep": "second"}])

    def test_queue_holds_up_under_load_and_keeps_message_order(self):
        total = 1000
        connector.add_topic("gateway/load")
        sender = self.publisher()

        received = []
        failure = []

        def consume():
            deadline = time.monotonic() + 30
            while len(received) < total and time.monotonic() < deadline:
                try:
                    received.append(connector.message_queue.get(timeout=0.2))
                except queue.Empty:
                    continue
            if len(received) != total:
                failure.append(f"only {len(received)} of {total} messages were queued")

        consumer = threading.Thread(target=consume)
        consumer.start()
        for index in range(total):
            self.publish(sender, "gateway/load", f"payload-{index}")
        consumer.join(timeout=40)

        self.assertFalse(consumer.is_alive(), "Consumer thread did not finish")
        self.assertEqual(failure, [])
        self.assertEqual(
            received, [{"gateway/load": f"payload-{index}"} for index in range(total)]
        )


if __name__ == "__main__":
    unittest.main()
