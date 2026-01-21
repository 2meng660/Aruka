#!/usr/bin/env python3
"""
KH-01 Industrial Control Dashboard (Complete Version)
Ready for Render: gunicorn -k eventlet -w 1 app:app
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
        self.last_message_time = None
        self.data_rate = 0

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

        # Calculate data rate (messages per minute)
        if state.last_message_time and state.message_count > 0:
            time_diff = (datetime.now(timezone.utc) - state.start_time).total_seconds()
            if time_diff > 0:
                state.data_rate = int(state.message_count / (time_diff / 60))
            else:
                state.data_rate = 0

        return {
            "system": {
                "mqtt_connected": state.mqtt_connected,
                "uptime": uptime_seconds,
                "message_count": state.message_count,
                "data_rate": state.data_rate,
                "last_update": state.last_update,
                "websocket_clients": state.websocket_clients,
                "broker": Config.MQTT_BROKER,
            },
            "health": asdict(state.health),
            "temperatures": {
                key: {
                    "value": s.value,
                    "unit": s.unit,
                    "status": s.status,
                    "name": Config.TEMPERATURE_NAMES.get(key, key),
                    "timestamp": s.timestamp,
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
                        if s.value is not None and s.max_value and s.max_value > 0
                        else None
                    ),
                    "timestamp": s.timestamp,
                }
                for key, s in state.vfd_frequencies.items()
            },
            "power": {
                key: {
                    "value": s.value,
                    "unit": s.unit,
                    "status": s.status,
                    "name": key.replace("phase", "Phase "),
                    "timestamp": s.timestamp,
                }
                for key, s in state.power_phases.items()
            },
            "burners": {
                key: {
                    "value": s.value,
                    "status": "on" if s.value == 1 else "off" if s.value == 0 else "unknown",
                    "name": key.replace("burner", "Burner "),
                    "timestamp": s.timestamp,
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
    if isinstance(value, dict):
        value = value.get("value", value.get("temperature"))
    
    s = state.temperatures[sensor_key]
    s.value = float(value) if value is not None else None
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
    if not isinstance(vfd_data, dict):
        return
        
    for key, s in state.vfd_frequencies.items():
        if key not in vfd_data:
            continue
        value = vfd_data.get(key)
        s.value = float(value) if value is not None else None
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
    if not isinstance(power_data, dict):
        return
        
    for key, s in state.power_phases.items():
        if key not in power_data:
            continue
        value = power_data.get(key)
        s.value = float(value) if value is not None else None
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
    if not isinstance(status_data, dict):
        return
        
    for key, s in state.burner_status.items():
        if key not in status_data:
            continue
        value = status_data.get(key)
        s.value = int(value) if value is not None else None
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
        result, mid = client.subscribe(Config.MQTT_SUBSCRIBE_TOPIC, qos=1)
        LOG.info("📌 SUBSCRIBE result=%s mid=%s", result, mid)
        socketio.emit("system_status", {"mqtt_connected": True, "message": "Connected to MQTT broker"})
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
    socketio.emit("system_status", {"mqtt_connected": False, "message": "Disconnected from MQTT"})


def on_mqtt_message(client, userdata, msg):
    try:
        with state.lock:
            state.last_message_time = datetime.now(timezone.utc)
        
        payload = msg.payload.decode(errors="replace")
        data = json.loads(payload)
        timestamp = now_z()

        with state.lock:
            state.message_count += 1
            state.last_update = timestamp
            state.message_history.append({"topic": msg.topic, "timestamp": timestamp, "data": data})
            if len(state.message_history) > 50:
                state.message_history.pop(0)

        LOG.debug("📥 Received MQTT: %s", msg.topic)
        
        # ROUTING
        if msg.topic in Config.TOPICS["temperature"]:
            process_temperature(msg.topic, data, timestamp)
        elif msg.topic == Config.TOPICS["vfd"]:
            process_vfd(data, timestamp)
        elif msg.topic == Config.TOPICS["power"]:
            process_power(data, timestamp)
        elif msg.topic == Config.TOPICS["status"]:
            process_status(data, timestamp)
        else:
            LOG.debug("ℹ️ Received unknown topic: %s", msg.topic)

        calculate_system_health()
        socketio.emit("data_update", prepare_websocket_data())

    except json.JSONDecodeError as e:
        LOG.error("❌ Invalid JSON received on topic %s: %s", msg.topic, e)
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
    client_id = f"KH01_DASHBOARD_{int(time.time())}"
    LOG.info("🚀 MQTT worker starting with client_id=%s", client_id)

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
            last_activity = time.time()
            while True:
                eventlet.sleep(5)
                # Check if we're still receiving messages
                with state.lock:
                    if state.last_message_time:
                        time_since_last = (datetime.now(timezone.utc) - state.last_message_time).total_seconds()
                        if time_since_last > 300:  # 5 minutes no data
                            LOG.warning("⚠️ No MQTT messages for %.0f seconds", time_since_last)
                
                # Reconnect if loop seems dead
                if time.time() - last_activity > 600:  # 10 minutes
                    LOG.warning("⚠️ MQTT loop inactive, forcing reconnect")
                    break
                    
        except KeyboardInterrupt:
            LOG.info("Shutdown requested")
            break
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
                "topic": Config.MQTT_SUBSCRIBE_TOPIC,
            },
            "system": {
                "uptime_seconds": int(uptime.total_seconds()),
                "start_time": state.start_time.isoformat(),
                "message_count": state.message_count,
                "data_rate": state.data_rate,
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
# COMPLETE DASHBOARD HTML
# =====================================================
DASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KH-01 Industrial Control Dashboard</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <style>
        :root {
            --primary: #1a237e;
            --primary-light: #534bae;
            --secondary: #00bcd4;
            --success: #4caf50;
            --warning: #ff9800;
            --danger: #f44336;
            --info: #2196f3;
            --dark: #121212;
            --light: #f5f5f5;
            --gray: #757575;
            --card-bg: #ffffff;
            --body-bg: #f0f2f5;
            --border: #e0e0e0;
        }
        
        body.dark-mode {
            --dark: #f5f5f5;
            --light: #121212;
            --card-bg: #1e1e1e;
            --body-bg: #121212;
            --border: #333;
        }
        
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background-color: var(--body-bg);
            color: var(--dark);
            line-height: 1.6;
            transition: all 0.3s ease;
        }
        
        .container {
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }
        
        /* HEADER */
        header {
            background: linear-gradient(135deg, var(--primary) 0%, var(--primary-light) 100%);
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
        }
        
        .header-left h1 {
            font-size: 1.8rem;
            margin-bottom: 5px;
        }
        
        .header-left .subtitle {
            font-size: 1rem;
            opacity: 0.9;
        }
        
        .header-right {
            display: flex;
            gap: 10px;
            align-items: center;
        }
        
        /* STATUS BADGES */
        .status-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .status-connected {
            background-color: var(--success);
            color: white;
        }
        
        .status-disconnected {
            background-color: var(--danger);
            color: white;
        }
        
        /* CARDS */
        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        
        .card {
            background: var(--card-bg);
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            border: 1px solid var(--border);
            transition: transform 0.3s ease, box-shadow 0.3s ease;
        }
        
        .card:hover {
            transform: translateY(-2px);
            box-shadow: 0 4px 8px rgba(0,0,0,0.15);
        }
        
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid var(--border);
        }
        
        .card-title {
            font-size: 1.2rem;
            font-weight: 600;
            color: var(--primary);
        }
        
        /* HEALTH INDICATOR */
        .health-indicator {
            display: flex;
            align-items: center;
            gap: 10px;
            padding: 10px;
            border-radius: 8px;
            background: var(--light);
        }
        
        .health-dot {
            width: 12px;
            height: 12px;
            border-radius: 50%;
        }
        
        .health-healthy { background-color: var(--success); }
        .health-good { background-color: var(--info); }
        .health-warning { background-color: var(--warning); }
        .health-critical { background-color: var(--danger); }
        .health-unknown { background-color: var(--gray); }
        
        /* GAUGES AND METERS */
        .gauge {
            height: 20px;
            background: var(--light);
            border-radius: 10px;
            overflow: hidden;
            position: relative;
        }
        
        .gauge-fill {
            height: 100%;
            border-radius: 10px;
            transition: width 0.5s ease;
        }
        
        .gauge-normal { background: var(--success); }
        .gauge-warning { background: var(--warning); }
        .gauge-critical { background: var(--danger); }
        .gauge-off { background: var(--gray); }
        
        /* SENSOR DISPLAY */
        .sensor-grid {
            display: grid;
            gap: 15px;
        }
        
        .sensor-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 10px;
            background: var(--light);
            border-radius: 8px;
        }
        
        .sensor-info {
            flex: 1;
        }
        
        .sensor-name {
            font-weight: 600;
            margin-bottom: 5px;
        }
        
        .sensor-value {
            font-size: 1.4rem;
            font-weight: 700;
        }
        
        .sensor-unit {
            color: var(--gray);
            font-size: 0.9rem;
        }
        
        .sensor-status {
            padding: 4px 12px;
            border-radius: 15px;
            font-size: 0.8rem;
            font-weight: 600;
        }
        
        .status-normal { background-color: #e8f5e9; color: var(--success); }
        .status-warning { background-color: #fff3e0; color: var(--warning); }
        .status-critical { background-color: #ffebee; color: var(--danger); }
        .status-unknown { background-color: #f5f5f5; color: var(--gray); }
        
        /* SYSTEM INFO */
        .system-info {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
        }
        
        .info-item {
            display: flex;
            flex-direction: column;
            gap: 5px;
        }
        
        .info-label {
            font-size: 0.9rem;
            color: var(--gray);
        }
        
        .info-value {
            font-size: 1.1rem;
            font-weight: 600;
        }
        
        /* BUTTONS */
        .btn {
            padding: 10px 20px;
            border: none;
            border-radius: 5px;
            font-weight: 600;
            cursor: pointer;
            transition: all 0.3s ease;
            display: inline-flex;
            align-items: center;
            gap: 8px;
        }
        
        .btn-primary {
            background-color: var(--primary);
            color: white;
        }
        
        .btn-primary:hover {
            background-color: var(--primary-light);
        }
        
        .btn-secondary {
            background-color: var(--secondary);
            color: white;
        }
        
        .btn-secondary:hover {
            opacity: 0.9;
        }
        
        /* BURNER STATUS */
        .burner-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 10px;
        }
        
        .burner-item {
            text-align: center;
            padding: 15px;
            border-radius: 8px;
            background: var(--light);
        }
        
        .burner-on { background-color: #e8f5e9; border: 2px solid var(--success); }
        .burner-off { background-color: #f5f5f5; border: 2px solid var(--gray); }
        
        .burner-icon {
            font-size: 2rem;
            margin-bottom: 10px;
        }
        
        /* FOOTER */
        footer {
            text-align: center;
            padding: 20px;
            color: var(--gray);
            font-size: 0.9rem;
            margin-top: 20px;
            border-top: 1px solid var(--border);
        }
        
        /* RESPONSIVE */
        @media (max-width: 768px) {
            .container {
                padding: 10px;
            }
            
            header {
                flex-direction: column;
                text-align: center;
                gap: 15px;
            }
            
            .dashboard-grid {
                grid-template-columns: 1fr;
            }
        }
        
        /* ANIMATIONS */
        @keyframes pulse {
            0% { opacity: 1; }
            50% { opacity: 0.5; }
            100% { opacity: 1; }
        }
        
        .pulse {
            animation: pulse 2s infinite;
        }
        
        /* LOADING */
        .loading {
            text-align: center;
            padding: 40px;
            color: var(--gray);
        }
        
        .spinner {
            border: 3px solid var(--light);
            border-top: 3px solid var(--primary);
            border-radius: 50%;
            width: 40px;
            height: 40px;
            animation: spin 1s linear infinite;
            margin: 0 auto 20px;
        }
        
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div class="header-left">
                <h1><i class="fas fa-industry"></i> KH-01 Industrial Control Dashboard</h1>
                <div class="subtitle">Real-time Monitoring System</div>
            </div>
            <div class="header-right">
                <div id="mqttStatus" class="status-badge status-disconnected">
                    <i class="fas fa-plug"></i> MQTT: Disconnected
                </div>
                <button id="themeToggle" class="btn btn-secondary">
                    <i class="fas fa-moon"></i> Dark Mode
                </button>
                <button id="refreshBtn" class="btn btn-primary">
                    <i class="fas fa-sync-alt"></i> Refresh
                </button>
            </div>
        </header>
        
        <!-- SYSTEM HEALTH CARD -->
        <div class="card">
            <div class="card-header">
                <h2 class="card-title"><i class="fas fa-heartbeat"></i> System Health</h2>
                <div id="healthIndicator" class="health-indicator">
                    <div class="health-dot health-unknown"></div>
                    <span id="healthStatus">Initializing...</span>
                </div>
            </div>
            <div class="system-info">
                <div class="info-item">
                    <span class="info-label">Overall Status</span>
                    <span id="overallStatus" class="info-value">Unknown</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Components</span>
                    <span id="componentsStatus" class="info-value">0/0</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Last Check</span>
                    <span id="lastCheck" class="info-value">--:--:--</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Health Message</span>
                    <span id="healthMessage" class="info-value">Waiting for data...</span>
                </div>
            </div>
        </div>
        
        <!-- MAIN DASHBOARD GRID -->
        <div class="dashboard-grid">
            <!-- TEMPERATURES -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title"><i class="fas fa-thermometer-half"></i> Temperatures</h2>
                </div>
                <div class="sensor-grid" id="temperatureGrid">
                    <!-- Filled by JavaScript -->
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading temperature data...
                    </div>
                </div>
            </div>
            
            <!-- VFD FREQUENCIES -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title"><i class="fas fa-tachometer-alt"></i> VFD Frequencies</h2>
                </div>
                <div class="sensor-grid" id="vfdGrid">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading VFD data...
                    </div>
                </div>
            </div>
        </div>
        
        <!-- SECOND ROW -->
        <div class="dashboard-grid">
            <!-- POWER CONSUMPTION -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title"><i class="fas fa-bolt"></i> Power Consumption</h2>
                </div>
                <div class="sensor-grid" id="powerGrid">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading power data...
                    </div>
                </div>
            </div>
            
            <!-- BURNER STATUS -->
            <div class="card">
                <div class="card-header">
                    <h2 class="card-title"><i class="fas fa-fire"></i> Burner Status</h2>
                </div>
                <div class="burner-grid" id="burnerGrid">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading burner status...
                    </div>
                </div>
            </div>
        </div>
        
        <!-- SYSTEM INFO CARD -->
        <div class="card">
            <div class="card-header">
                <h2 class="card-title"><i class="fas fa-info-circle"></i> System Information</h2>
            </div>
            <div class="system-info">
                <div class="info-item">
                    <span class="info-label">MQTT Broker</span>
                    <span id="mqttBroker" class="info-value">Connecting...</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Uptime</span>
                    <span id="systemUptime" class="info-value">00:00:00</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Messages</span>
                    <span id="messageCount" class="info-value">0</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Data Rate</span>
                    <span id="dataRate" class="info-value">0/min</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Last Update</span>
                    <span id="lastUpdate" class="info-value">Never</span>
                </div>
                <div class="info-item">
                    <span class="info-label">WebSocket Clients</span>
                    <span id="wsClients" class="info-value">0</span>
                </div>
            </div>
        </div>
        
        <footer>
            <p>KH-01 Industrial Control Dashboard v1.0 | Real-time Monitoring System</p>
            <p>Data updates automatically via MQTT WebSocket</p>
        </footer>
    </div>

    <script>
        // Socket.IO connection
        const socket = io();
        
        // State management
        let dashboardData = null;
        let lastUpdateTime = null;
        
        // DOM Elements
        const elements = {
            mqttStatus: document.getElementById('mqttStatus'),
            healthIndicator: document.getElementById('healthIndicator'),
            healthStatus: document.getElementById('healthStatus'),
            overallStatus: document.getElementById('overallStatus'),
            componentsStatus: document.getElementById('componentsStatus'),
            lastCheck: document.getElementById('lastCheck'),
            healthMessage: document.getElementById('healthMessage'),
            temperatureGrid: document.getElementById('temperatureGrid'),
            vfdGrid: document.getElementById('vfdGrid'),
            powerGrid: document.getElementById('powerGrid'),
            burnerGrid: document.getElementById('burnerGrid'),
            mqttBroker: document.getElementById('mqttBroker'),
            systemUptime: document.getElementById('systemUptime'),
            messageCount: document.getElementById('messageCount'),
            dataRate: document.getElementById('dataRate'),
            lastUpdate: document.getElementById('lastUpdate'),
            wsClients: document.getElementById('wsClients'),
            themeToggle: document.getElementById('themeToggle'),
            refreshBtn: document.getElementById('refreshBtn')
        };
        
        // Theme toggle
        elements.themeToggle.addEventListener('click', () => {
            document.body.classList.toggle('dark-mode');
            const icon = elements.themeToggle.querySelector('i');
            if (document.body.classList.contains('dark-mode')) {
                icon.className = 'fas fa-sun';
                elements.themeToggle.innerHTML = '<i class="fas fa-sun"></i> Light Mode';
            } else {
                icon.className = 'fas fa-moon';
                elements.themeToggle.innerHTML = '<i class="fas fa-moon"></i> Dark Mode';
            }
        });
        
        // Refresh button
        elements.refreshBtn.addEventListener('click', () => {
            socket.emit('request_update');
            elements.refreshBtn.querySelector('i').className = 'fas fa-sync-alt fa-spin';
            setTimeout(() => {
                elements.refreshBtn.querySelector('i').className = 'fas fa-sync-alt';
            }, 1000);
        });
        
        // Socket.IO event handlers
        socket.on('connect', () => {
            console.log('WebSocket connected');
        });
        
        socket.on('system_status', (data) => {
            updateMqttStatus(data.mqtt_connected);
        });
        
        socket.on('data_update', (data) => {
            dashboardData = data;
            lastUpdateTime = new Date();
            updateDashboard(data);
        });
        
        socket.on('disconnect', () => {
            console.log('WebSocket disconnected');
            updateMqttStatus(false);
        });
        
        // Update MQTT status
        function updateMqttStatus(connected) {
            if (connected) {
                elements.mqttStatus.className = 'status-badge status-connected';
                elements.mqttStatus.innerHTML = '<i class="fas fa-plug"></i> MQTT: Connected';
            } else {
                elements.mqttStatus.className = 'status-badge status-disconnected';
                elements.mqttStatus.innerHTML = '<i class="fas fa-plug"></i> MQTT: Disconnected';
            }
        }
        
        // Format time
        function formatTime(seconds) {
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const secs = seconds % 60;
            return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        
        // Format date
        function formatDate(dateString) {
            if (!dateString) return 'Never';
            const date = new Date(dateString);
            return date.toLocaleTimeString();
        }
        
        // Update health indicator
        function updateHealthIndicator(health) {
            const healthDot = elements.healthIndicator.querySelector('.health-dot');
            healthDot.className = 'health-dot';
            
            switch(health.overall) {
                case 'healthy':
                    healthDot.classList.add('health-healthy');
                    elements.healthIndicator.style.background = '#e8f5e9';
                    break;
                case 'good':
                    healthDot.classList.add('health-good');
                    elements.healthIndicator.style.background = '#e3f2fd';
                    break;
                case 'warning':
                    healthDot.classList.add('health-warning');
                    elements.healthIndicator.style.background = '#fff3e0';
                    break;
                case 'critical':
                    healthDot.classList.add('health-critical');
                    elements.healthIndicator.style.background = '#ffebee';
                    break;
                default:
                    healthDot.classList.add('health-unknown');
                    elements.healthIndicator.style.background = '#f5f5f5';
            }
            
            elements.healthStatus.textContent = health.overall.toUpperCase();
            elements.overallStatus.textContent = health.overall.toUpperCase();
            elements.componentsStatus.textContent = `${health.components_ok}/${health.components_total}`;
            elements.lastCheck.textContent = formatDate(health.last_check);
            elements.healthMessage.textContent = health.message;
        }
        
        // Update temperatures
        function updateTemperatures(temps) {
            let html = '';
            for (const [key, sensor] of Object.entries(temps)) {
                const value = sensor.value !== null ? sensor.value.toFixed(1) : '--';
                const statusClass = `status-${sensor.status}`;
                
                html += `
                    <div class="sensor-item">
                        <div class="sensor-info">
                            <div class="sensor-name">${sensor.name}</div>
                            <div class="sensor-value">${value}<span class="sensor-unit">${sensor.unit}</span></div>
                        </div>
                        <div class="sensor-status ${statusClass}">${sensor.status}</div>
                    </div>
                `;
            }
            elements.temperatureGrid.innerHTML = html;
        }
        
        // Update VFD frequencies
        function updateVFD(vfds) {
            let html = '';
            for (const [key, vfd] of Object.entries(vfds)) {
                const value = vfd.value !== null ? vfd.value.toFixed(1) : '--';
                const percentage = vfd.percentage !== null ? vfd.percentage : 0;
                const gaugeClass = `gauge-${vfd.status}`;
                
                html += `
                    <div class="sensor-item">
                        <div class="sensor-info">
                            <div class="sensor-name">${vfd.name}</div>
                            <div class="sensor-value">${value}<span class="sensor-unit">${vfd.unit}</span></div>
                            <div class="gauge">
                                <div class="gauge-fill ${gaugeClass}" style="width: ${percentage}%"></div>
                            </div>
                        </div>
                        <div class="sensor-status status-${vfd.status}">${percentage}%</div>
                    </div>
                `;
            }
            elements.vfdGrid.innerHTML = html;
        }
        
        // Update power
        function updatePower(power) {
            let html = '';
            for (const [key, phase] of Object.entries(power)) {
                const value = phase.value !== null ? phase.value.toFixed(1) : '--';
                const statusClass = `status-${phase.status}`;
                
                html += `
                    <div class="sensor-item">
                        <div class="sensor-info">
                            <div class="sensor-name">${phase.name}</div>
                            <div class="sensor-value">${value}<span class="sensor-unit">${phase.unit}</span></div>
                        </div>
                        <div class="sensor-status ${statusClass}">${phase.status}</div>
                    </div>
                `;
            }
            elements.powerGrid.innerHTML = html;
        }
        
        // Update burners
        function updateBurners(burners) {
            let html = '';
            for (const [key, burner] of Object.entries(burners)) {
                const isOn = burner.status === 'on';
                const burnerClass = isOn ? 'burner-on' : 'burner-off';
                const icon = isOn ? 'fas fa-fire' : 'fas fa-fire-extinguisher';
                const text = isOn ? 'ON' : 'OFF';
                
                html += `
                    <div class="burner-item ${burnerClass}">
                        <div class="burner-icon">
                            <i class="${icon}"></i>
                        </div>
                        <div class="sensor-name">${burner.name}</div>
                        <div class="sensor-status status-${burner.status}">${text}</div>
                    </div>
                `;
            }
            elements.burnerGrid.innerHTML = html;
        }
        
        // Update system info
        function updateSystemInfo(system) {
            elements.mqttBroker.textContent = system.broker || 'Unknown';
            elements.systemUptime.textContent = formatTime(system.uptime);
            elements.messageCount.textContent = system.message_count;
            elements.dataRate.textContent = `${system.data_rate}/min`;
            elements.lastUpdate.textContent = formatDate(system.last_update);
            elements.wsClients.textContent = system.websocket_clients;
        }
        
        // Main update function
        function updateDashboard(data) {
            updateHealthIndicator(data.health);
            updateTemperatures(data.temperatures);
            updateVFD(data.vfd);
            updatePower(data.power);
            updateBurners(data.burners);
            updateSystemInfo(data.system);
        }
        
        // Request initial data
        socket.emit('request_update');
        
        // Auto-refresh uptime every second
        setInterval(() => {
            if (dashboardData) {
                dashboardData.system.uptime++;
                elements.systemUptime.textContent = formatTime(dashboardData.system.uptime);
            }
        }, 1000);
        
        // Show connection status
        console.log('Dashboard initialized. Waiting for data...');
    </script>
</body>
</html>"""


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
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)