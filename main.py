import json
import os
import subprocess
from pathlib import Path
from functools import wraps
import connector

from flask import Flask, jsonify, request, render_template, session, url_for, redirect

import login_signup
import set_mosquitto

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "json.json"

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
secret_key = os.getenv("SECRET_KEY","dsadqwd")
if not secret_key:
    raise RuntimeError("SECRET_KEY environment variable is required")
app.secret_key = secret_key
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax", SESSION_COOKIE_SECURE=os.getenv("COOKIE_SECURE", "0") == "1")


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("logged_in"):
            if request.path.startswith("/api/") or request.path.startswith("/info_"):
                return jsonify({"error": "Please login first"}), 401
            return redirect(url_for("get_login_page"))
        return view(*args, **kwargs)
    return wrapped


def read_config():
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def validate_config(data):
    if not isinstance(data, dict):
        return "JSON object required"
    defaults = read_config()
    allowed = set(defaults)
    if set(data) != allowed:
        return "Invalid configuration fields"
    for key in ("port(websocket)", "port_NonTls(mqtt)", "port_Tls(mqtt)"):
        if type(data[key]) is not int or not 1 <= data[key] <= 65535:
            return f"{key} must be an integer between 1 and 65535"
    ports = [data["port_NonTls(mqtt)"]]
    if data["active_TLS"]: ports.append(data["port_Tls(mqtt)"])
    if data["active_websocket"]: ports.append(data["port(websocket)"])
    if len(ports) != len(set(ports)):
        return "Active listeners must use different ports"
    for key in ("allow_anonymous", "active_TLS", "active_websocket", "need_cert(for server)"):
        if type(data[key]) is not bool:
            return f"{key} must be boolean"
    for key in ("password_file", "certfile", "keyfile", "cafile", "listen_ip(wireguard)"):
        if not isinstance(data[key], str) or not data[key].strip() or "\x00" in data[key]:
            return f"Invalid {key}"
    if data["active_TLS"]:
        for key in ("certfile", "keyfile"):
            if not Path(data[key]).is_absolute(): return f"{key} must be an absolute path"
        if data["need_cert(for server)"] and not Path(data["cafile"]).is_absolute(): return "cafile must be an absolute path"
    if not data["allow_anonymous"] and not Path(data["password_file"]).is_absolute():
        return "password_file must be an absolute path"
    return None


@app.get("/")
def redirecter():
    return redirect(url_for("get_mainpage" if session.get("logged_in") else "get_login_page"))


@app.get("/api/config")
@login_required
def get_config():
    try: return jsonify(read_config())
    except (OSError, json.JSONDecodeError): return jsonify({"error": "Configuration is unavailable"}), 500


@app.post("/api/config")
@login_required
def save_config():
    data = request.get_json(silent=True)
    error = validate_config(data)
    if error: return jsonify({"status": "error", "message": error}), 400
    backup = CONFIG_PATH.with_suffix(".json.bak")
    try:
        if CONFIG_PATH.exists(): backup.write_bytes(CONFIG_PATH.read_bytes())
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=4, sort_keys=True), encoding="utf-8")
        tmp.replace(CONFIG_PATH)
        set_mosquitto.write(data)
        subprocess.run(["sudo", "systemctl", "restart", "mosquitto-project.service"], check=True, capture_output=True, text=True, timeout=30)
        return jsonify({"status": "ok"})
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        if backup.exists(): backup.replace(CONFIG_PATH)
        return jsonify({"status": "error", "message": "Failed to apply configuration"}), 500


@app.get("/mainpage")
@login_required
def get_mainpage():
    return render_template("mainpage.html", username=session.get("username", "User"))


@app.get("/mosquitto/panel")
@login_required
def get_mosquitto_panel(): return render_template("mosquitto_conf.html")


@app.get("/devices")
@login_required
def get_devices_page(): return render_template("devices.html")


@app.get("/login")
def get_login_page(): return render_template("login.html")


@app.get("/signup")
def get_signup_page(): return render_template("signup.html")


@app.post("/info_signup")
def signup():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        return jsonify({"message": "Invalid input"}), 400
    try: status = login_signup.signup(data["username"], data["password"])
    except ValueError as exc: return jsonify({"message": str(exc)}), 400
    if status == "Username already exists": return jsonify({"message": status}), 409
    return jsonify({"message": "Signup successful"}), 201


@app.post("/info_login")
def login():
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("username"), str) or not isinstance(data.get("password"), str):
        return jsonify({"message": "Invalid credentials"}), 400
    status = login_signup.signin(data["username"], data["password"])
    if status == "login successful":
        session.clear(); session.update(logged_in=True, username=data["username"].strip())
        return jsonify({"message": "Login successful"})
    return jsonify({"message": "Invalid username or password"}), 401


@app.post("/logout")
def logout():
    session.clear(); return jsonify({"message": "Logged out"})


@app.get("/api/devices")
@login_required
def api_devices():
    try:
        return jsonify(connector.get_devices())
    except connector.BrokerConnectionError:
        return jsonify({"error": "MQTT broker is unavailable"}), 503


@app.delete("/api/devices")
@login_required
def remove_device():
    data = request.get_json(silent=True) or {}
    try:
        removed = connector.delete_device(data.get("name"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if not removed:
        return jsonify({"error": "Device not found"}), 404
    return jsonify({"status": "ok"})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "9000")))
