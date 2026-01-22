#!/usr/bin/env python3
"""
KH-01 Industrial Control Dashboard - TEST VERSION
Simplified UI for testing
Ready for Render: python app.py
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


# =====================================================
# DATA MODELS
# =====================================================
@dataclass
class SensorData:
    value: Optional[float] = None
    timestamp: Optional[str] = None
    unit: str = ""
    status: str = "unknown"


@dataclass
class SystemHealth:
    overall: str = "unknown"
    message: str = "Initializing..."
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
        self.start_time = datetime.now(timezone.utc)
        self.message_count = 0
        self.last_update = None
        
        # Simplified data storage
        self.temperatures = {}
        self.vfd_data = {}
        self.power_data = {}
        self.status_data = {}
        
        self.health = SystemHealth()


state = SystemState()

def now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def calculate_system_health():
    with state.lock:
        # Simple health check based on data availability
        total_data = len(state.temperatures) + len(state.vfd_data) + len(state.power_data) + len(state.status_data)
        
        if total_data == 0:
            state.health.overall = "unknown"
            state.health.message = "Waiting for data..."
        elif state.mqtt_connected:
            state.health.overall = "healthy"
            state.health.message = f"Receiving data ({state.message_count} messages)"
        else:
            state.health.overall = "disconnected"
            state.health.message = "MQTT disconnected"
        
        state.health.last_check = now_z()


def prepare_websocket_data() -> Dict[str, Any]:
    with state.lock:
        uptime = datetime.now(timezone.utc) - state.start_time
        uptime_seconds = int(uptime.total_seconds())
        
        # Calculate data rate
        data_rate = 0
        if uptime_seconds > 0:
            data_rate = int(state.message_count / (uptime_seconds / 60))

        return {
            "system": {
                "mqtt_connected": state.mqtt_connected,
                "uptime": uptime_seconds,
                "message_count": state.message_count,
                "data_rate": data_rate,
                "last_update": state.last_update,
                "broker": Config.MQTT_BROKER,
            },
            "health": asdict(state.health),
            "temperatures": state.temperatures,
            "vfd": state.vfd_data,
            "power": state.power_data,
            "status": state.status_data,
        }


# =====================================================
# MQTT PROCESSORS (Simplified)
# =====================================================
def process_temperature(topic: str, data: Dict, timestamp: str):
    probe_num = topic[-1]
    sensor_key = f"probe{probe_num}"
    
    value = data.get("value")
    if isinstance(value, dict):
        value = value.get("value", value.get("temperature"))
    
    state.temperatures[sensor_key] = {
        "value": float(value) if value is not None else None,
        "unit": "°C",
        "name": Config.TEMPERATURE_NAMES.get(f"p{probe_num}", f"Probe {probe_num}"),
        "timestamp": timestamp,
        "status": "normal" if value and value < 500 else "warning" if value and value < 800 else "critical"
    }


def process_vfd(data: Dict, timestamp: str):
    vfd_data = data.get("value", {})
    if isinstance(vfd_data, dict):
        for key, value in vfd_data.items():
            state.vfd_data[key] = {
                "value": float(value) if value is not None else None,
                "unit": "Hz",
                "name": key,
                "timestamp": timestamp
            }


def process_power(data: Dict, timestamp: str):
    power_data = data.get("value", {})
    if isinstance(power_data, dict):
        for key, value in power_data.items():
            state.power_data[key] = {
                "value": float(value) if value is not None else None,
                "unit": "A",
                "name": f"Phase {key[-1]}" if key.startswith("phase") else key,
                "timestamp": timestamp
            }


def process_status(data: Dict, timestamp: str):
    status_data = data.get("value", {})
    if isinstance(status_data, dict):
        for key, value in status_data.items():
            state.status_data[key] = {
                "value": int(value) if value is not None else None,
                "status": "on" if value == 1 else "off",
                "name": key,
                "timestamp": timestamp
            }


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_mqtt_connect(client, userdata, flags, rc):
    with state.lock:
        state.mqtt_connected = (rc == 0)
    
    if rc == 0:
        LOG.info("✅ MQTT CONNECTED")
        client.subscribe(Config.MQTT_SUBSCRIBE_TOPIC, qos=1)
        socketio.emit("system_status", {"mqtt_connected": True})
    else:
        LOG.error("❌ MQTT CONNECT FAILED rc=%s", rc)
        socketio.emit("system_status", {"mqtt_connected": False})


def on_mqtt_disconnect(client, userdata, rc):
    with state.lock:
        state.mqtt_connected = False
    socketio.emit("system_status", {"mqtt_connected": False})


def on_mqtt_message(client, userdata, msg):
    try:
        payload = msg.payload.decode(errors="replace")
        data = json.loads(payload)
        timestamp = now_z()

        with state.lock:
            state.message_count += 1
            state.last_update = timestamp

        LOG.info("📥 MQTT: %s", msg.topic)
        
        # Simple routing
        if "temperature" in msg.topic:
            process_temperature(msg.topic, data, timestamp)
        elif "vfd" in msg.topic:
            process_vfd(data, timestamp)
        elif "power" in msg.topic:
            process_power(data, timestamp)
        elif "status" in msg.topic:
            process_status(data, timestamp)

        calculate_system_health()
        socketio.emit("data_update", prepare_websocket_data())

    except Exception as e:
        LOG.error("Error processing MQTT: %s", e)


# =====================================================
# MQTT WORKER
# =====================================================
def mqtt_worker():
    """
    Simple MQTT worker
    """
    LOG.info("🚀 Starting MQTT worker")
    
    while True:
        try:
            client = mqtt.Client(protocol=mqtt.MQTTv311, clean_session=True)
            client.username_pw_set(Config.MQTT_USERNAME, Config.MQTT_PASSWORD)
            
            client.tls_set(ca_certs=certifi.where())
            client.tls_insecure_set(False)
            
            client.on_connect = on_mqtt_connect
            client.on_disconnect = on_mqtt_disconnect
            client.on_message = on_mqtt_message
            
            client.reconnect_delay_set(min_delay=1, max_delay=30)
            
            LOG.info("🔌 Connecting to MQTT broker...")
            client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, keepalive=60)
            client.loop_forever()
            
        except Exception as e:
            LOG.error("💥 MQTT error: %s", e)
            eventlet.sleep(5)


# =====================================================
# WEBSOCKET EVENTS
# =====================================================
@socketio.on("connect")
def ws_connect():
    socketio.emit("system_status", {"mqtt_connected": state.mqtt_connected})
    socketio.emit("data_update", prepare_websocket_data())


@socketio.on("request_update")
def ws_request_update():
    socketio.emit("data_update", prepare_websocket_data())


# =====================================================
# REST API
# =====================================================
@app.route("/api/status")
def api_status():
    return jsonify(prepare_websocket_data())


@app.route("/health")
def health():
    return jsonify({"status": "ok", "mqtt": state.mqtt_connected})


# =====================================================
# SIMPLE TEST UI
# =====================================================
DASH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KH-01 Test Dashboard</title>
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: Arial, sans-serif;
            background: #f0f2f5;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
        }
        header {
            background: linear-gradient(135deg, #1a237e 0%, #534bae 100%);
            color: white;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
            text-align: center;
        }
        h1 {
            margin-bottom: 10px;
            font-size: 24px;
        }
        .status-bar {
            display: flex;
            gap: 10px;
            justify-content: center;
            margin-top: 10px;
        }
        .status {
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: bold;
            background: #4caf50;
            color: white;
        }
        .status.disconnected {
            background: #f44336;
        }
        .dashboard {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: white;
            border-radius: 10px;
            padding: 20px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }
        .card h2 {
            color: #1a237e;
            margin-bottom: 15px;
            padding-bottom: 10px;
            border-bottom: 2px solid #e0e0e0;
        }
        .data-item {
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #eee;
        }
        .data-item:last-child {
            border-bottom: none;
        }
        .data-name {
            font-weight: 500;
        }
        .data-value {
            font-weight: bold;
            color: #1a237e;
        }
        .data-unit {
            color: #666;
            margin-left: 5px;
        }
        .system-info {
            background: white;
            border-radius: 10px;
            padding: 20px;
            margin-top: 20px;
        }
        .info-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-top: 15px;
        }
        .info-item {
            display: flex;
            flex-direction: column;
        }
        .info-label {
            font-size: 14px;
            color: #666;
        }
        .info-value {
            font-size: 18px;
            font-weight: bold;
            color: #1a237e;
        }
        .loading {
            text-align: center;
            padding: 40px;
            color: #666;
        }
        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #1a237e;
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
            <h1>🔧 KH-01 Test Dashboard</h1>
            <p>Real-time Monitoring System - Test Version</p>
            <div class="status-bar">
                <div id="mqttStatus" class="status disconnected">MQTT: Disconnected</div>
                <div id="healthStatus" class="status">Health: Unknown</div>
            </div>
        </header>
        
        <div class="dashboard">
            <!-- Temperatures Card -->
            <div class="card">
                <h2>🌡️ Temperatures</h2>
                <div id="temperatureList">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading temperatures...
                    </div>
                </div>
            </div>
            
            <!-- VFD Card -->
            <div class="card">
                <h2>⚡ VFD Frequencies</h2>
                <div id="vfdList">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading VFD data...
                    </div>
                </div>
            </div>
        </div>
        
        <div class="dashboard">
            <!-- Power Card -->
            <div class="card">
                <h2>🔌 Power Consumption</h2>
                <div id="powerList">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading power data...
                    </div>
                </div>
            </div>
            
            <!-- Status Card -->
            <div class="card">
                <h2>🔥 Burner Status</h2>
                <div id="statusList">
                    <div class="loading">
                        <div class="spinner"></div>
                        Loading status...
                    </div>
                </div>
            </div>
        </div>
        
        <div class="system-info">
            <h2>📊 System Information</h2>
            <div class="info-grid">
                <div class="info-item">
                    <span class="info-label">Broker</span>
                    <span id="brokerInfo" class="info-value">Connecting...</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Uptime</span>
                    <span id="uptimeInfo" class="info-value">00:00:00</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Messages</span>
                    <span id="messageInfo" class="info-value">0</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Data Rate</span>
                    <span id="rateInfo" class="info-value">0/min</span>
                </div>
                <div class="info-item">
                    <span class="info-label">Last Update</span>
                    <span id="updateInfo" class="info-value">Never</span>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let lastData = null;
        
        socket.on('connect', () => {
            console.log('Connected to server');
            updateStatus('connected', 'WebSocket Connected');
        });
        
        socket.on('system_status', (data) => {
            updateMqttStatus(data.mqtt_connected);
        });
        
        socket.on('data_update', (data) => {
            lastData = data;
            updateDashboard(data);
        });
        
        socket.on('disconnect', () => {
            updateStatus('disconnected', 'Disconnected');
        });
        
        function updateMqttStatus(connected) {
            const el = document.getElementById('mqttStatus');
            el.textContent = `MQTT: ${connected ? 'Connected' : 'Disconnected'}`;
            el.className = `status ${connected ? '' : 'disconnected'}`;
        }
        
        function updateStatus(status, message) {
            const el = document.getElementById('healthStatus');
            el.textContent = `Health: ${message}`;
        }
        
        function formatValue(value, unit) {
            if (value === null || value === undefined) return '--';
            return `${parseFloat(value).toFixed(1)}${unit ? ' ' + unit : ''}`;
        }
        
        function formatTime(seconds) {
            const hrs = Math.floor(seconds / 3600);
            const mins = Math.floor((seconds % 3600) / 60);
            const secs = seconds % 60;
            return `${hrs.toString().padStart(2, '0')}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }
        
        function updateDashboard(data) {
            // Update health
            document.getElementById('healthStatus').textContent = `Health: ${data.health.overall}`;
            
            // Update temperatures
            updateList('temperatureList', data.temperatures, 'value', '°C');
            
            // Update VFD
            updateList('vfdList', data.vfd, 'value', 'Hz');
            
            // Update power
            updateList('powerList', data.power, 'value', 'A');
            
            // Update status
            updateStatusList('statusList', data.status);
            
            // Update system info
            document.getElementById('brokerInfo').textContent = data.system.broker;
            document.getElementById('uptimeInfo').textContent = formatTime(data.system.uptime);
            document.getElementById('messageInfo').textContent = data.system.message_count;
            document.getElementById('rateInfo').textContent = `${data.system.data_rate}/min`;
            document.getElementById('updateInfo').textContent = data.system.last_update ? 
                new Date(data.system.last_update).toLocaleTimeString() : 'Never';
        }
        
        function updateList(elementId, data, valueKey, unit) {
            const element = document.getElementById(elementId);
            if (!data || Object.keys(data).length === 0) {
                element.innerHTML = '<div class="loading">No data available</div>';
                return;
            }
            
            let html = '';
            for (const [key, item] of Object.entries(data)) {
                html += `
                    <div class="data-item">
                        <span class="data-name">${item.name || key}</span>
                        <span class="data-value">${formatValue(item[valueKey], unit)}</span>
                    </div>
                `;
            }
            element.innerHTML = html;
        }
        
        function updateStatusList(elementId, data) {
            const element = document.getElementById(elementId);
            if (!data || Object.keys(data).length === 0) {
                element.innerHTML = '<div class="loading">No data available</div>';
                return;
            }
            
            let html = '';
            for (const [key, item] of Object.entries(data)) {
                const status = item.status === 'on' ? '🟢 ON' : '🔴 OFF';
                html += `
                    <div class="data-item">
                        <span class="data-name">${item.name || key}</span>
                        <span class="data-value">${status}</span>
                    </div>
                `;
            }
            element.innerHTML = html;
        }
        
        // Request initial data
        socket.emit('request_update');
        
        // Auto-update uptime
        setInterval(() => {
            if (lastData) {
                lastData.system.uptime++;
                document.getElementById('uptimeInfo').textContent = formatTime(lastData.system.uptime);
            }
        }, 1000);
        
        console.log('Dashboard loaded. Waiting for data...');
    </script>
</body>
</html>"""


@app.route("/")
def index():
    return DASH_HTML


# =====================================================
# START SERVICES
# =====================================================
_mqtt_started = False

def start_background_services():
    global _mqtt_started
    if _mqtt_started:
        return
    _mqtt_started = True
    socketio.start_background_task(mqtt_worker)
    LOG.info("✅ MQTT background task started")


start_background_services()


# =====================================================
# RENDER DEPLOYMENT
# =====================================================
if __name__ == "__main__":
    start_background_services()
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port, debug=False, use_reloader=False)