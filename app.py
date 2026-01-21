#!/usr/bin/env python3
"""
KH-01 Industrial Control Dashboard (Single File)
Render-ready: gunicorn -k eventlet -w 1 app:app

Fixes:
- One MQTT loop (no duplicate functions)
- Stable client_id
- eventlet-safe locking
- Reliable reconnect loop (loop_start + connect/retry)
- Clear logs: connect rc + subscribe
"""

# IMPORTANT: eventlet monkey_patch must be FIRST
import eventlet
eventlet.monkey_patch()

import os
import json
import time
import ssl
import logging
import certifi
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Dict, Optional, Any

from flask import Flask, jsonify, request
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

# eventlet-safe lock
from eventlet.semaphore import Semaphore


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
LOG = logging.getLogger("KH01")


# =====================================================
# CONFIGURATION
# =====================================================
class Config:
    MQTT_BROKER = "t569f61e.ala.asia-southeast1.emqxsl.com"
    MQTT_PORT = 8883
    MQTT_USERNAME = "KH-01-device"
    MQTT_PASSWORD = "Radiation0-Disperser8-Sternum1-Trio4"

    # Subscribe wildcard (most reliable)
    MQTT_SUBSCRIBE_TOPIC = "KH/site-01/KH-01/#"

    TOPICS = {
        "temperature": [
            "KH/site-01/KH-01/temperature_probe1",
            "KH/site-01/KH-01/temperature_probe2",
            "KH/site-01/KH-01/temperature_probe3",
            "KH/site-01/KH-01/temperature_probe4",
        ],
        "vfd": "KH/site-01/KH-01/vfd_frequency",
        "power": "KH/site-01/KH-01/power_consumption",
        "status": "KH/site-01/KH-01/status",
    }

    TEMPERATURE_NAMES = {
        "p1": "Reactor Core",
        "p2": "Primary Burner",
        "p3": "Secondary Burner",
        "p4": "Output Conveyor",
    }

    VFD_NAMES = {
        "Bucket": "Bucket",
        "INLETScrew": "INLET Screw",
        "airlocker": "Air Locker",
        "exaust1": "Exhaust 1",
        "exaust2": "Exhaust 2",
        "outscrew1": "Outscrew 1",
        "outscrew2": "Outscrew 2",
        "reactor": "Reactor",
        "syngas": "Syngas",
    }


# =====================================================
# DATA MODELS
# =====================================================
@dataclass
class SensorData:
    value: Optional[float] = None
    timestamp: Optional[str] = None
    unit: str = ""
    status: str = "unknown"
    min_value: Optional[float] = None
    max_value: Optional[float] = None


@dataclass
class SystemHealth:
    overall: str = "unknown"
    message: str = "Initializing..."
    components_ok: int = 0
    components_total: int = 0
    last_check: Optional[str] = None


# =====================================================
# APP
# =====================================================
app = Flask(__name__)
socketio = SocketIO(
    app,
    async_mode="eventlet",
    cors_allowed_origins="*",
    logger=False,
    engineio_logger=False,
)


# =====================================================
# GLOBAL STATE
# =====================================================
class SystemState:
    def __init__(self):
        self.lock = Semaphore(1)

        self.mqtt_connected = False
        self.mqtt_last_connection = None
        self.websocket_clients = 0
        self.start_time = datetime.now(timezone.utc)
        self.message_count = 0

        self.temperatures = {
            "p1": SensorData(unit="°C"),
            "p2": SensorData(unit="°C"),
            "p3": SensorData(unit="°C"),
            "p4": SensorData(unit="°C"),
        }

        self.vfd_frequencies = {
            "Bucket": SensorData(unit="Hz", min_value=0, max_value=50),
            "INLETScrew": SensorData(unit="Hz", min_value=0, max_value=50),
            "airlocker": SensorData(unit="Hz", min_value=0, max_value=50),
            "exaust1": SensorData(unit="Hz", min_value=0, max_value=50),
            "exaust2": SensorData(unit="Hz", min_value=0, max_value=50),
            "outscrew1": SensorData(unit="Hz", min_value=0, max_value=50),
            "outscrew2": SensorData(unit="Hz", min_value=0, max_value=50),
            "reactor": SensorData(unit="Hz", min_value=0, max_value=50),
            "syngas": SensorData(unit="Hz", min_value=0, max_value=50),
        }

        self.power_phases = {
            "phaseA": SensorData(unit="A"),
            "phaseB": SensorData(unit="A"),
            "phaseC": SensorData(unit="A"),
        }

        self.burner_status = {
            "burner1": SensorData(unit="", min_value=0, max_value=1),
            "burner3": SensorData(unit="", min_value=0, max_value=1),
            "burner4": SensorData(unit="", min_value=0, max_value=1),
            "burner6": SensorData(unit="", min_value=0, max_value=1),
        }

        self.health = SystemHealth()
        self.last_update = None
        self.message_history = []  # last 50


state = SystemState()


def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calculate_system_health():
    with state.lock:
        ok_count = 0
        total = 0

        for s in state.temperatures.values():
            total += 1
            if s.value is not None:
                ok_count += 1

        for s in state.vfd_frequencies.values():
            total += 1
            if s.value is not None:
                ok_count += 1

        for s in state.power_phases.values():
            total += 1
            if s.value is not None:
                ok_count += 1

        for s in state.burner_status.values():
            total += 1
            if s.value is not None:
                ok_count += 1

        state.health.components_ok = ok_count
        state.health.components_total = total
        state.health.last_check = now_z()

        if total == 0:
            state.health.overall = "unknown"
            state.health.message = "No data available"
        elif ok_count == total:
            state.health.overall = "healthy"
            state.health.message = "All systems operational"
        elif ok_count >= total * 0.8:
            state.health.overall = "good"
            state.health.message = f"{ok_count}/{total} components active"
        elif ok_count >= total * 0.5:
            state.health.overall = "warning"
            state.health.message = f"Limited data ({ok_count}/{total})"
        else:
            state.health.overall = "critical"
            state.health.message = f"Critical: Only {ok_count}/{total} active"


def prepare_websocket_data() -> Dict[str, Any]:
    with state.lock:
        uptime = datetime.now(timezone.utc) - state.start_time
        uptime_seconds = int(uptime.total_seconds())

        return {
            "system": {
                "mqtt_connected": state.mqtt_connected,
                "uptime": uptime_seconds,
                "message_count": state.message_count,
                "data_rate": 0,
                "last_update": state.last_update,
                "websocket_clients": state.websocket_clients,
            },
            "health": asdict(state.health),
            "temperatures": {
                key: {
                    "value": s.value,
                    "unit": s.unit,
                    "status": s.status,
                    "name": Config.TEMPERATURE_NAMES.get(key, key),
                }
                for key, s in state.temperatures.items()
            },
            "vfd": {
                key: {
                    "value": s.value,
                    "unit": s.unit,
                    "status": s.status,
                    "name": Config.VFD_NAMES.get(key, key),
                    "percentage": (
                        int((s.value / s.max_value) * 100)
                        if s.value is not None and s.max_value
                        else None
                    ),
                }
                for key, s in state.vfd_frequencies.items()
            },
            "power": {
                key: {
                    "value": s.value,
                    "unit": s.unit,
                    "status": s.status,
                    "name": key.replace("phase", "Phase "),
                }
                for key, s in state.power_phases.items()
            },
            "burners": {
                key: {
                    "value": s.value,
                    "status": "on" if s.value == 1 else "off" if s.value == 0 else "unknown",
                    "name": key.replace("burner", "Burner "),
                }
                for key, s in state.burner_status.items()
            },
        }


# =====================================================
# MQTT PROCESSORS
# =====================================================
def process_temperature(topic: str, data: Dict, timestamp: str):
    probe_num = topic[-1]
    sensor_key = f"p{probe_num}"
    if sensor_key not in state.temperatures:
        return

    value = data.get("value")
    s = state.temperatures[sensor_key]
    s.value = value
    s.timestamp = timestamp

    if value is None:
        s.status = "unknown"
    elif value > 800:
        s.status = "critical"
    elif value > 500:
        s.status = "warning"
    else:
        s.status = "normal"


def process_vfd(data: Dict, timestamp: str):
    vfd_data = data.get("value", {})
    for key, s in state.vfd_frequencies.items():
        if key not in vfd_data:
            continue
        value = vfd_data.get(key)
        s.value = value
        s.timestamp = timestamp

        if value is None:
            s.status = "unknown"
            continue

        if s.max_value:
            pct = (value / s.max_value) * 100
            if pct > 90:
                s.status = "warning"
            elif pct > 70:
                s.status = "normal"
            else:
                s.status = "low"
        else:
            s.status = "normal"


def process_power(data: Dict, timestamp: str):
    power_data = data.get("value", {})
    for key, s in state.power_phases.items():
        if key not in power_data:
            continue
        value = power_data.get(key)
        s.value = value
        s.timestamp = timestamp

        if value is None:
            s.status = "unknown"
        elif value > 100:
            s.status = "warning"
        elif value > 0:
            s.status = "normal"
        else:
            s.status = "off"


def process_status(data: Dict, timestamp: str):
    status_data = data.get("value", {})
    for key, s in state.burner_status.items():
        if key not in status_data:
            continue
        value = status_data.get(key)
        s.value = value
        s.timestamp = timestamp
        s.status = "on" if value == 1 else "off" if value == 0 else "unknown"


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_mqtt_connect(client, userdata, flags, rc):
    status_messages = {
        0: "Connected successfully",
        1: "Incorrect protocol version",
        2: "Invalid client identifier",
        3: "Server unavailable",
        4: "Bad username or password",
        5: "Not authorized",
    }

    with state.lock:
        state.mqtt_connected = (rc == 0)
        state.mqtt_last_connection = now_z() if rc == 0 else None

    if rc == 0:
        LOG.info("✅ MQTT CONNECTED (rc=0). Subscribing to: %s", Config.MQTT_SUBSCRIBE_TOPIC)
        result, mid = client.subscribe(Config.MQTT_SUBSCRIBE_TOPIC, qos=0)
        LOG.info("📌 SUBSCRIBE result=%s mid=%s", result, mid)
        socketio.emit("system_status", {"mqtt_connected": True})
    else:
        LOG.error("❌ MQTT CONNECT FAILED rc=%s (%s)", rc, status_messages.get(rc, "Unknown"))
        socketio.emit("system_status", {"mqtt_connected": False, "error": status_messages.get(rc)})


def on_mqtt_disconnect(client, userdata, rc):
    with state.lock:
        state.mqtt_connected = False
    if rc == 0:
        LOG.warning("⚠️ MQTT disconnected normally")
    else:
        LOG.error("⚠️ MQTT disconnected unexpectedly (rc=%s)", rc)
    socketio.emit("system_status", {"mqtt_connected": False})


def on_mqtt_message(client, userdata, msg):
    try:
        payload = msg.payload.decode(errors="replace")
        data = json.loads(payload)
        timestamp = now_z()

        with state.lock:
            state.message_count += 1
            state.last_update = timestamp
            state.message_history.append({"topic": msg.topic, "timestamp": timestamp, "data": data})
            if len(state.message_history) > 50:
                state.message_history.pop(0)

        # ROUTING
        if msg.topic in Config.TOPICS["temperature"]:
            process_temperature(msg.topic, data, timestamp)
        elif msg.topic == Config.TOPICS["vfd"]:
            process_vfd(data, timestamp)
        elif msg.topic == Config.TOPICS["power"]:
            process_power(data, timestamp)
        elif msg.topic == Config.TOPICS["status"]:
            process_status(data, timestamp)

        calculate_system_health()
        socketio.emit("data_update", prepare_websocket_data())

    except Exception as e:
        LOG.exception("❌ Error processing MQTT message: %s", e)


# =====================================================
# MQTT WORKER (RECONNECT FOREVER)
# =====================================================
def mqtt_worker():
    """
    Keep one MQTT client running forever.
    Uses loop_start() for reliability under eventlet.
    """
    client_id = os.environ.get("MQTT_CLIENT_ID", "KH01_RENDER_DASHBOARD")  # stable
    LOG.info("MQTT worker starting with client_id=%s", client_id)

    while True:
        client = None
        try:
            client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311, clean_session=True)
            client.enable_logger(logging.getLogger("paho"))

            client.username_pw_set(Config.MQTT_USERNAME, Config.MQTT_PASSWORD)

            client.tls_set(
                ca_certs=certifi.where(),
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
            client.tls_insecure_set(False)

            client.on_connect = on_mqtt_connect
            client.on_disconnect = on_mqtt_disconnect
            client.on_message = on_mqtt_message

            client.reconnect_delay_set(min_delay=1, max_delay=30)

            LOG.info("🔌 Connecting to MQTT broker %s:%s ...", Config.MQTT_BROKER, Config.MQTT_PORT)
            client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, keepalive=60)

            client.loop_start()

            # Keep alive, detect dead loop
            while True:
                eventlet.sleep(5)

        except Exception as e:
            LOG.error("💥 MQTT worker error: %s", e)
            eventlet.sleep(5)

        finally:
            try:
                if client is not None:
                    client.loop_stop()
                    client.disconnect()
            except Exception:
                pass


# =====================================================
# WEBSOCKET EVENTS
# =====================================================
@socketio.on("connect")
def ws_connect():
    with state.lock:
        state.websocket_clients += 1
    LOG.info("🌐 WebSocket client connected. Total=%s", state.websocket_clients)
    socketio.emit("system_status", {"mqtt_connected": state.mqtt_connected})
    socketio.emit("data_update", prepare_websocket_data())


@socketio.on("disconnect")
def ws_disconnect():
    with state.lock:
        state.websocket_clients = max(0, state.websocket_clients - 1)
    LOG.info("🌐 WebSocket client disconnected. Total=%s", state.websocket_clients)


@socketio.on("request_update")
def ws_request_update():
    socketio.emit("data_update", prepare_websocket_data())


# =====================================================
# REST API
# =====================================================
@app.route("/api/status")
def api_status():
    with state.lock:
        uptime = datetime.now(timezone.utc) - state.start_time
        return jsonify({
            "mqtt": {
                "connected": state.mqtt_connected,
                "last_connection": state.mqtt_last_connection,
                "broker": Config.MQTT_BROKER,
                "port": Config.MQTT_PORT,
            },
            "system": {
                "uptime_seconds": int(uptime.total_seconds()),
                "start_time": state.start_time.isoformat(),
                "message_count": state.message_count,
                "websocket_clients": state.websocket_clients,
            },
            "health": asdict(state.health),
            "last_update": state.last_update,
        })


@app.route("/api/data")
def api_data():
    return jsonify(prepare_websocket_data())


@app.route("/api/history")
def api_history():
    with state.lock:
        return jsonify({
            "messages": state.message_history[-20:],
            "total_messages": state.message_count,
        })


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with state.lock:
        for s in state.temperatures.values():
            s.value = None
            s.timestamp = None
            s.status = "unknown"
        for s in state.vfd_frequencies.values():
            s.value = None
            s.timestamp = None
            s.status = "unknown"
        for s in state.power_phases.values():
            s.value = None
            s.timestamp = None
            s.status = "unknown"
        for s in state.burner_status.values():
            s.value = None
            s.timestamp = None
            s.status = "unknown"

    calculate_system_health()
    socketio.emit("data_update", prepare_websocket_data())
    return jsonify({"status": "success", "message": "Data reset"})


# =====================================================
# DASHBOARD HTML (keep yours)
# =====================================================
DASH_HTML = """<html><body><h3>Dashboard HTML is very long. Keep your existing DASH_HTML here unchanged.</h3></body></html>"""

@app.route("/")
def index():
    return DASH_HTML


# =====================================================
# START BACKGROUND SERVICES ON IMPORT
# =====================================================
_mqtt_started = False

def start_background_services():
    global _mqtt_started
    if _mqtt_started:
        return
    _mqtt_started = True
    socketio.start_background_task(mqtt_worker)
    LOG.info("✅ MQTT background task started (eventlet)")


start_background_services()


# =====================================================
# LOCAL RUN
# =====================================================
if __name__ == "__main__":
    start_background_services()
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, debug=True, use_reloader=False)
