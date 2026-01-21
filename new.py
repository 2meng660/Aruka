#!/usr/bin/env python3
"""
KH-01 Industrial Control Dashboard
Render-ready (Gunicorn + eventlet) + MQTT TLS (certifi) + detailed MQTT logs
"""

# IMPORTANT: eventlet monkey_patch must be FIRST
import eventlet
eventlet.monkey_patch()

import os
import json
import time
import ssl
import uuid
import threading
import logging
import certifi
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Dict, Optional

from flask import Flask, jsonify, request
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt
# =====================================================
# CONFIGURATION
# =====================================================
class Config:
    MQTT_BROKER = "t569f61e.ala.asia-southeast1.emqxsl.com"
    MQTT_PORT = 8883
    MQTT_USERNAME = "KH-01-device"
    MQTT_PASSWORD = "Radiation0-Disperser8-Sternum1-Trio4"
    
    TOPICS = {
        "temperature": [
            "KH/site-01/KH-01/temperature_probe1",
            "KH/site-01/KH-01/temperature_probe2",
            "KH/site-01/KH-01/temperature_probe3",
            "KH/site-01/KH-01/temperature_probe4"
        ],
        "vfd": "KH/site-01/KH-01/vfd_frequency",
        "power": "KH/site-01/KH-01/power_consumption",
        "status": "KH/site-01/KH-01/status",
        "all": "KH/site-01/KH-01/#"
    }
    
    TEMPERATURE_NAMES = {
        "p1": "Reactor Core",
        "p2": "Primary Burner",
        "p3": "Secondary Burner",
        "p4": "Output Conveyor"
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
        "syngas": "Syngas"
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
    
    def to_dict(self):
        return asdict(self)

@dataclass
class SystemHealth:
    overall: str = "unknown"
    message: str = "Initializing..."
    components_ok: int = 0
    components_total: int = 0
    last_check: Optional[str] = None

# =====================================================
# APPLICATION
# =====================================================

app = Flask(__name__)

socketio = SocketIO(app,
                   async_mode="eventlet",
                   cors_allowed_origins="*",
                   logger=False,
                   engineio_logger=False)


# =====================================================
# GLOBAL STATE
# =====================================================
class SystemState:
    def __init__(self):
        self.lock = threading.Lock()
        
        # Connection state
        self.mqtt_connected = False
        self.mqtt_last_connection = None
        self.websocket_clients = 0
        self.start_time = datetime.now(timezone.utc)
        self.message_count = 0
        
        # Sensor data
        self.temperatures = {
            "p1": SensorData(unit="°C"),
            "p2": SensorData(unit="°C"),
            "p3": SensorData(unit="°C"),
            "p4": SensorData(unit="°C")
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
            "syngas": SensorData(unit="Hz", min_value=0, max_value=50)
        }
        
        self.power_phases = {
            "phaseA": SensorData(unit="A"),
            "phaseB": SensorData(unit="A"),
            "phaseC": SensorData(unit="A")
        }
        
        self.burner_status = {
            "burner1": SensorData(unit="", min_value=0, max_value=1),
            "burner3": SensorData(unit="", min_value=0, max_value=1),
            "burner4": SensorData(unit="", min_value=0, max_value=1),
            "burner6": SensorData(unit="", min_value=0, max_value=1)
        }
        
        # System health
        self.health = SystemHealth()
        
        # Statistics
        self.uptime_seconds = 0
        self.data_rate = 0
        self.last_update = None
        self.message_history = []

state = SystemState()

# =====================================================
# UTILITIES
# =====================================================
def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def format_timestamp(timestamp: str) -> str:
    """Format ISO timestamp to readable format"""
    try:
        dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
        return dt.strftime("%H:%M:%S")
    except:
        return timestamp

def calculate_system_health():
    """Calculate overall system health"""
    with state.lock:
        ok_count = 0
        total = 0
        
        # Check temperatures
        for sensor in state.temperatures.values():
            total += 1
            if sensor.value is not None:
                ok_count += 1
        
        # Check VFD frequencies
        for vfd in state.vfd_frequencies.values():
            total += 1
            if vfd.value is not None:
                ok_count += 1
        
        # Check power
        for phase in state.power_phases.values():
            total += 1
            if phase.value is not None:
                ok_count += 1
        
        # Check burners
        for burner in state.burner_status.values():
            total += 1
            if burner.value is not None:
                ok_count += 1
        
        state.health.components_ok = ok_count
        state.health.components_total = total
        state.health.last_check = now_z()
        
        # Determine status
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

def prepare_websocket_data():
    """Prepare data for WebSocket emission"""
    with state.lock:
        # Calculate uptime
        uptime = datetime.now(timezone.utc) - state.start_time
        state.uptime_seconds = int(uptime.total_seconds())
        
        return {
            "system": {
                "mqtt_connected": state.mqtt_connected,
                "uptime": state.uptime_seconds,
                "message_count": state.message_count,
                "data_rate": state.data_rate,
                "last_update": state.last_update
            },
            "health": asdict(state.health),
            "temperatures": {
                key: {
                    "value": sensor.value,
                    "unit": sensor.unit,
                    "status": sensor.status,
                    "name": Config.TEMPERATURE_NAMES.get(key, key)
                }
                for key, sensor in state.temperatures.items()
            },
            "vfd": {
                key: {
                    "value": vfd.value,
                    "unit": vfd.unit,
                    "status": vfd.status,
                    "name": Config.VFD_NAMES.get(key, key),
                    "percentage": (
                        int((vfd.value / vfd.max_value) * 100) 
                        if vfd.value is not None and vfd.max_value 
                        else None
                    )
                }
                for key, vfd in state.vfd_frequencies.items()
            },
            "power": {
                key: {
                    "value": phase.value,
                    "unit": phase.unit,
                    "status": phase.status,
                    "name": key.replace("phase", "Phase ")
                }
                for key, phase in state.power_phases.items()
            },
            "burners": {
                key: {
                    "value": burner.value,
                    "status": "on" if burner.value == 1 else "off" if burner.value == 0 else "unknown",
                    "name": key.replace("burner", "Burner ")
                }
                for key, burner in state.burner_status.items()
            }
        }

# =====================================================
# MQTT HANDLERS
# =====================================================
def on_mqtt_connect(client, userdata, flags, rc):
    """Handle MQTT connection"""
    status_messages = {
        0: "Connected successfully",
        1: "Incorrect protocol version",
        2: "Invalid client identifier",
        3: "Server unavailable",
        4: "Bad username or password",
        5: "Not authorized"
    }
    
    with state.lock:
        state.mqtt_connected = (rc == 0)
        state.mqtt_last_connection = now_z() if rc == 0 else None
    
    if rc == 0:
        logging.info(f"✅ MQTT connected to {Config.MQTT_BROKER}")
        client.subscribe(Config.TOPICS["all"])
        socketio.emit("system_status", {"mqtt_connected": True})
    else:
        logging.error(f"❌ MQTT connection failed: {status_messages.get(rc, f'Code: {rc}')}")
        socketio.emit("system_status", {"mqtt_connected": False, "error": status_messages.get(rc)})

def on_mqtt_disconnect(client, userdata, rc):
    """Handle MQTT disconnection"""
    with state.lock:
        state.mqtt_connected = False
    
    if rc == 0:
        logging.warning("⚠️  MQTT disconnected normally")
    else:
        logging.error(f"⚠️  MQTT disconnected unexpectedly (rc={rc})")
    
    socketio.emit("system_status", {"mqtt_connected": False})

def on_mqtt_message(client, userdata, msg):
    """Handle incoming MQTT messages"""
    try:
        data = json.loads(msg.payload.decode())
        timestamp = now_z()
        
        with state.lock:
            state.message_count += 1
            state.last_update = timestamp
            
            # Add to message history (keep last 50)
            state.message_history.append({
                "topic": msg.topic,
                "timestamp": timestamp,
                "data": data
            })
            if len(state.message_history) > 50:
                state.message_history.pop(0)
        
        logging.debug(f"📨 {msg.topic}: {json.dumps(data)[:100]}...")
        
        # Process based on topic
        if msg.topic in Config.TOPICS["temperature"]:
            process_temperature(msg.topic, data, timestamp)
        elif msg.topic == Config.TOPICS["vfd"]:
            process_vfd(data, timestamp)
        elif msg.topic == Config.TOPICS["power"]:
            process_power(data, timestamp)
        elif msg.topic == Config.TOPICS["status"]:
            process_status(data, timestamp)
        
        # Update system health
        calculate_system_health()
        
        # Emit update via WebSocket
        socketio.emit("data_update", prepare_websocket_data())
        
    except json.JSONDecodeError as e:
        logging.error(f"❌ JSON decode error: {e}")
    except Exception as e:
        logging.error(f"❌ Error processing message: {e}")

def process_temperature(topic: str, data: Dict, timestamp: str):
    """Process temperature data"""
    probe_num = topic[-1]  # Extract probe number
    sensor_key = f"p{probe_num}"
    
    if sensor_key in state.temperatures:
        value = data.get("value")
        state.temperatures[sensor_key].value = value
        state.temperatures[sensor_key].timestamp = timestamp
        
        # Set status based on value
        if value is not None:
            if value > 800:
                state.temperatures[sensor_key].status = "critical"
            elif value > 500:
                state.temperatures[sensor_key].status = "warning"
            else:
                state.temperatures[sensor_key].status = "normal"
        else:
            state.temperatures[sensor_key].status = "unknown"

def process_vfd(data: Dict, timestamp: str):
    """Process VFD frequency data"""
    vfd_data = data.get("value", {})
    
    for key, sensor in state.vfd_frequencies.items():
        if key in vfd_data:
            value = vfd_data[key]
            sensor.value = value
            sensor.timestamp = timestamp
            
            # Calculate percentage if max_value is set
            if sensor.max_value and value is not None:
                percentage = (value / sensor.max_value) * 100
                if percentage > 90:
                    sensor.status = "warning"
                elif percentage > 70:
                    sensor.status = "normal"
                else:
                    sensor.status = "low"
            else:
                sensor.status = "normal" if value is not None else "unknown"

def process_power(data: Dict, timestamp: str):
    """Process power consumption data"""
    power_data = data.get("value", {})
    
    for key, phase in state.power_phases.items():
        if key in power_data:
            value = power_data[key]
            phase.value = value
            phase.timestamp = timestamp
            
            # Set status (simplified)
            if value is not None:
                if value > 100:
                    phase.status = "warning"
                elif value > 0:
                    phase.status = "normal"
                else:
                    phase.status = "off"
            else:
                phase.status = "unknown"

def process_status(data: Dict, timestamp: str):
    """Process burner status data"""
    status_data = data.get("value", {})
    
    for key, burner in state.burner_status.items():
        if key in status_data:
            value = status_data[key]
            burner.value = value
            burner.timestamp = timestamp
            burner.status = "on" if value == 1 else "off" if value == 0 else "unknown"

def start_mqtt_client():
    """Start and maintain MQTT connection"""
    while True:
        try:
            client = mqtt.Client(
                client_id=f"kh01-dashboard-{int(time.time())}",
                protocol=mqtt.MQTTv311,
                clean_session=True
            )

            # show detailed mqtt/tls logs in Render
            client.enable_logger(logging.getLogger("paho"))

            client.username_pw_set(Config.MQTT_USERNAME, Config.MQTT_PASSWORD)

            # SSL/TLS configuration (VERIFY CERT)
            client.tls_set(
                ca_certs=certifi.where(),
                tls_version=ssl.PROTOCOL_TLS_CLIENT
            )
            client.tls_insecure_set(False)

            # callbacks
            client.on_connect = on_mqtt_connect
            client.on_disconnect = on_mqtt_disconnect
            client.on_message = on_mqtt_message

            # reconnect settings
            client.reconnect_delay_set(min_delay=1, max_delay=30)

            logging.info("🔌 Connecting to MQTT broker...")
            client.connect(Config.MQTT_BROKER, Config.MQTT_PORT, keepalive=60)

            client.loop_forever()

        except Exception as e:
            logging.error(f"💥 MQTT connection error: {e}")
            time.sleep(5)


# =====================================================
# WEB SOCKET HANDLERS
# =====================================================
@socketio.on('connect')
def handle_connect():
    """Handle WebSocket connection"""
    with state.lock:
        state.websocket_clients += 1
    logging.info(f"🌐 WebSocket client connected. Total: {state.websocket_clients}")
    socketio.emit("system_status", {"mqtt_connected": state.mqtt_connected})
    socketio.emit("data_update", prepare_websocket_data())

@socketio.on('disconnect')
def handle_disconnect():
    """Handle WebSocket disconnection"""
    with state.lock:
        state.websocket_clients = max(0, state.websocket_clients - 1)
    logging.info(f"🌐 WebSocket client disconnected. Total: {state.websocket_clients}")

@socketio.on('request_update')
def handle_request_update():
    """Handle update request from client"""
    socketio.emit("data_update", prepare_websocket_data())

# =====================================================
# REST API ENDPOINTS
# =====================================================
@app.route('/')
def index():
    """Serve the dashboard"""
    return DASH_HTML

@app.route('/api/status')
def api_status():
    """Get system status"""
    with state.lock:
        uptime = datetime.now(timezone.utc) - state.start_time
        return jsonify({
            "mqtt": {
                "connected": state.mqtt_connected,
                "last_connection": state.mqtt_last_connection,
                "broker": Config.MQTT_BROKER,
                "port": Config.MQTT_PORT
            },
            "system": {
                "uptime_seconds": int(uptime.total_seconds()),
                "start_time": state.start_time.isoformat(),
                "message_count": state.message_count,
                "websocket_clients": state.websocket_clients
            },
            "health": asdict(state.health),
            "last_update": state.last_update
        })

@app.route('/api/data')
def api_data():
    """Get all current data"""
    return jsonify(prepare_websocket_data())

@app.route('/api/history')
def api_history():
    """Get message history"""
    with state.lock:
        return jsonify({
            "messages": state.message_history[-20:],  # Last 20 messages
            "total_messages": state.message_count
        })

@app.route('/api/reset', methods=['POST'])
def api_reset():
    """Reset system data (for testing)"""
    with state.lock:
        # Reset sensor values but keep connection state
        for sensor in state.temperatures.values():
            sensor.value = None
            sensor.timestamp = None
            sensor.status = "unknown"
        
        for vfd in state.vfd_frequencies.values():
            vfd.value = None
            vfd.timestamp = None
            vfd.status = "unknown"
        
        for phase in state.power_phases.values():
            phase.value = None
            phase.timestamp = None
            phase.status = "unknown"
        
        for burner in state.burner_status.values():
            burner.value = None
            burner.timestamp = None
            burner.status = "unknown"
        
        calculate_system_health()
    
    socketio.emit("data_update", prepare_websocket_data())
    return jsonify({"status": "success", "message": "Data reset"})

# =====================================================
# PROFESSIONAL DASHBOARD UI
# =====================================================
DASH_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KH-01 | Industrial Control Dashboard</title>
    
    <!-- Icons -->
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    
    <!-- Fonts -->
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
    
    <!-- Socket.IO -->
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    
    <style>
        /* Design System */
        :root {
            /* Colors */
            --primary: #0066cc;
            --primary-dark: #0052a3;
            --primary-light: #4d94ff;
            --secondary: #00b3b3;
            --success: #00cc66;
            --warning: #ff9900;
            --danger: #ff3333;
            --info: #0066cc;
            
            /* Neutrals */
            --dark-1: #0a0e17;
            --dark-2: #141a2d;
            --dark-3: #1c243d;
            --dark-4: #2a3655;
            --light-1: #ffffff;
            --light-2: #e6edff;
            --light-3: #a3b4cc;
            --light-4: #7a8ba6;
            
            /* Status Colors */
            --status-healthy: var(--success);
            --status-good: #00b3b3;
            --status-warning: var(--warning);
            --status-critical: var(--danger);
            --status-unknown: var(--light-4);
            
            /* Spacing */
            --space-xs: 0.5rem;
            --space-sm: 1rem;
            --space-md: 1.5rem;
            --space-lg: 2rem;
            --space-xl: 3rem;
            
            /* Border Radius */
            --radius-sm: 0.5rem;
            --radius-md: 0.75rem;
            --radius-lg: 1rem;
            
            /* Shadows */
            --shadow-sm: 0 2px 4px rgba(0, 0, 0, 0.1);
            --shadow-md: 0 4px 8px rgba(0, 0, 0, 0.15);
            --shadow-lg: 0 8px 16px rgba(0, 0, 0, 0.2);
            --shadow-xl: 0 12px 24px rgba(0, 0, 0, 0.25);
            
            /* Transitions */
            --transition-fast: 150ms ease;
            --transition-normal: 300ms ease;
            --transition-slow: 500ms ease;
        }
        
        /* Reset & Base */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        html {
            font-size: 16px;
        }
        
        body {
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: linear-gradient(135deg, var(--dark-1) 0%, var(--dark-2) 100%);
            color: var(--light-2);
            min-height: 100vh;
            line-height: 1.6;
            overflow-x: hidden;
        }
        
        /* Typography */
        h1, h2, h3, h4, h5, h6 {
            font-weight: 600;
            line-height: 1.3;
        }
        
        .text-xs { font-size: 0.75rem; }
        .text-sm { font-size: 0.875rem; }
        .text-base { font-size: 1rem; }
        .text-lg { font-size: 1.125rem; }
        .text-xl { font-size: 1.25rem; }
        .text-2xl { font-size: 1.5rem; }
        .text-3xl { font-size: 1.875rem; }
        .text-4xl { font-size: 2.25rem; }
        
        .font-light { font-weight: 300; }
        .font-normal { font-weight: 400; }
        .font-medium { font-weight: 500; }
        .font-semibold { font-weight: 600; }
        .font-bold { font-weight: 700; }
        
        .text-primary { color: var(--primary); }
        .text-secondary { color: var(--secondary); }
        .text-success { color: var(--success); }
        .text-warning { color: var(--warning); }
        .text-danger { color: var(--danger); }
        .text-light { color: var(--light-2); }
        .text-muted { color: var(--light-4); }
        
        /* Layout */
        .container {
            width: 100%;
            max-width: 1400px;
            margin: 0 auto;
            padding: 0 var(--space-md);
        }
        
        .grid {
            display: grid;
            gap: var(--space-md);
        }
        
        .grid-cols-1 { grid-template-columns: repeat(1, 1fr); }
        .grid-cols-2 { grid-template-columns: repeat(2, 1fr); }
        .grid-cols-3 { grid-template-columns: repeat(3, 1fr); }
        .grid-cols-4 { grid-template-columns: repeat(4, 1fr); }
        .grid-cols-5 { grid-template-columns: repeat(5, 1fr); }
        .grid-cols-6 { grid-template-columns: repeat(6, 1fr); }
        
        .col-span-1 { grid-column: span 1; }
        .col-span-2 { grid-column: span 2; }
        .col-span-3 { grid-column: span 3; }
        .col-span-4 { grid-column: span 4; }
        .col-span-5 { grid-column: span 5; }
        .col-span-6 { grid-column: span 6; }
        
        .flex { display: flex; }
        .flex-col { flex-direction: column; }
        .items-center { align-items: center; }
        .items-start { align-items: flex-start; }
        .items-end { align-items: flex-end; }
        .justify-between { justify-content: space-between; }
        .justify-center { justify-content: center; }
        .justify-end { justify-content: flex-end; }
        .gap-xs { gap: var(--space-xs); }
        .gap-sm { gap: var(--space-sm); }
        .gap-md { gap: var(--space-md); }
        .gap-lg { gap: var(--space-lg); }
        
        /* Cards */
        .card {
            background: linear-gradient(135deg, var(--dark-2) 0%, var(--dark-3) 100%);
            border: 1px solid var(--dark-4);
            border-radius: var(--radius-lg);
            padding: var(--space-md);
            box-shadow: var(--shadow-md);
            transition: all var(--transition-normal);
        }
        
        .card:hover {
            transform: translateY(-2px);
            box-shadow: var(--shadow-lg);
            border-color: var(--primary);
        }
        
        .card-header {
            padding-bottom: var(--space-sm);
            margin-bottom: var(--space-sm);
            border-bottom: 1px solid var(--dark-4);
        }
        
        .card-title {
            display: flex;
            align-items: center;
            gap: var(--space-sm);
        }
        
        .card-icon {
            width: 2.5rem;
            height: 2.5rem;
            border-radius: var(--radius-sm);
            background: linear-gradient(135deg, var(--primary), var(--primary-dark));
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--light-1);
        }
        
        /* Header */
        .header {
            background: linear-gradient(135deg, var(--dark-2) 0%, var(--dark-3) 100%);
            border-bottom: 1px solid var(--dark-4);
            padding: var(--space-md) 0;
            position: sticky;
            top: 0;
            z-index: 100;
            box-shadow: var(--shadow-md);
        }
        
        .header-content {
            display: flex;
            align-items: center;
            justify-content: space-between;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: var(--space-sm);
        }
        
        .logo-icon {
            width: 3rem;
            height: 3rem;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            border-radius: var(--radius-md);
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 1.5rem;
        }
        
        .logo-text h1 {
            font-size: 1.5rem;
            background: linear-gradient(90deg, var(--light-1), var(--primary-light));
            -webkit-background-clip: text;
            background-clip: text;
            -webkit-text-fill-color: transparent;
        }
        
        .logo-text .subtitle {
            font-size: 0.875rem;
            color: var(--light-4);
        }
        
        /* Status Badge */
        .status-badge {
            display: flex;
            align-items: center;
            gap: var(--space-xs);
            padding: 0.5rem 1rem;
            background: var(--dark-3);
            border-radius: var(--radius-sm);
            border: 1px solid var(--dark-4);
        }
        
        .status-dot {
            width: 0.75rem;
            height: 0.75rem;
            border-radius: 50%;
            background: var(--danger);
            transition: all var(--transition-normal);
        }
        
        .status-dot.connected {
            background: var(--success);
            box-shadow: 0 0 10px var(--success);
            animation: pulse 2s infinite;
        }
        
        @keyframes pulse {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        
        /* Data Cards */
        .data-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: var(--space-md);
            margin: var(--space-md) 0;
        }
        
        .data-item {
            background: var(--dark-3);
            border-radius: var(--radius-md);
            padding: var(--space-sm);
            border: 1px solid var(--dark-4);
            transition: all var(--transition-fast);
        }
        
        .data-item:hover {
            background: var(--dark-4);
            border-color: var(--primary);
        }
        
        .data-label {
            font-size: 0.875rem;
            color: var(--light-4);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.25rem;
        }
        
        .data-value {
            font-size: 1.75rem;
            font-weight: 600;
            font-family: 'Courier New', monospace;
        }
        
        .data-unit {
            font-size: 0.875rem;
            color: var(--light-4);
            margin-left: 0.25rem;
        }
        
        /* Temperature Cards */
        .temperature-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: var(--space-sm);
        }
        
        .temp-card {
            background: linear-gradient(135deg, var(--dark-3) 0%, var(--dark-4) 100%);
            border-radius: var(--radius-md);
            padding: var(--space-sm);
            border-left: 4px solid var(--primary);
        }
        
        .temp-card.status-warning { border-left-color: var(--warning); }
        .temp-card.status-critical { border-left-color: var(--danger); }
        
        .temp-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: var(--space-xs);
        }
        
        .temp-name {
            font-weight: 500;
        }
        
        .temp-probe {
            font-size: 0.75rem;
            color: var(--light-4);
            background: var(--dark-2);
            padding: 0.125rem 0.5rem;
            border-radius: 1rem;
        }
        
        .temp-value {
            font-size: 2rem;
            font-weight: 700;
            font-family: 'Courier New', monospace;
        }
        
        /* VFD Cards */
        .vfd-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: var(--space-sm);
        }
        
        .vfd-card {
            background: linear-gradient(135deg, var(--dark-3) 0%, var(--dark-4) 100%);
            border-radius: var(--radius-md);
            padding: var(--space-sm);
            position: relative;
            overflow: hidden;
        }
        
        .vfd-card::before {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            height: 4px;
            background: linear-gradient(90deg, var(--primary), var(--secondary));
        }
        
        .vfd-label {
            font-size: 0.875rem;
            color: var(--light-4);
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.5rem;
        }
        
        .vfd-value {
            font-size: 1.5rem;
            font-weight: 600;
            font-family: 'Courier New', monospace;
            margin-bottom: 0.5rem;
        }
        
        .vfd-progress {
            height: 4px;
            background: var(--dark-2);
            border-radius: 2px;
            overflow: hidden;
        }
        
        .vfd-progress-bar {
            height: 100%;
            background: linear-gradient(90deg, var(--success), var(--secondary));
            border-radius: 2px;
            transition: width var(--transition-slow);
        }
        
        /* Burner Status */
        .burner-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: var(--space-sm);
        }
        
        .burner-card {
            background: var(--dark-3);
            border-radius: var(--radius-md);
            padding: var(--space-sm);
            display: flex;
            align-items: center;
            justify-content: space-between;
            border: 1px solid var(--dark-4);
        }
        
        .burner-card.status-on { border-color: var(--success); }
        .burner-card.status-off { border-color: var(--danger); }
        
        .burner-info {
            display: flex;
            align-items: center;
            gap: var(--space-sm);
        }
        
        .burner-icon {
            width: 2rem;
            height: 2rem;
            border-radius: var(--radius-sm);
            background: var(--dark-4);
            display: flex;
            align-items: center;
            justify-content: center;
            color: var(--warning);
        }
        
        .burner-status {
            padding: 0.25rem 0.75rem;
            border-radius: 1rem;
            font-size: 0.875rem;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.05em;
        }
        
        .status-on { background: rgba(0, 204, 102, 0.1); color: var(--success); }
        .status-off { background: rgba(255, 51, 51, 0.1); color: var(--danger); }
        .status-unknown { background: rgba(122, 139, 166, 0.1); color: var(--light-4); }
        
        /* System Health */
        .health-card {
            background: linear-gradient(135deg, var(--dark-2) 0%, var(--dark-3) 100%);
            border-radius: var(--radius-lg);
            padding: var(--space-md);
            border: 2px solid var(--dark-4);
        }
        
        .health-card.status-healthy { border-color: var(--success); }
        .health-card.status-good { border-color: var(--secondary); }
        .health-card.status-warning { border-color: var(--warning); }
        .health-card.status-critical { border-color: var(--danger); }
        
        .health-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: var(--space-sm);
        }
        
        .health-indicator {
            display: flex;
            align-items: center;
            gap: var(--space-xs);
        }
        
        .health-dot {
            width: 0.75rem;
            height: 0.75rem;
            border-radius: 50%;
        }
        
        .health-dot.status-healthy { background: var(--success); }
        .health-dot.status-good { background: var(--secondary); }
        .health-dot.status-warning { background: var(--warning); }
        .health-dot.status-critical { background: var(--danger); }
        
        .health-progress {
            height: 0.5rem;
            background: var(--dark-4);
            border-radius: 0.25rem;
            overflow: hidden;
            margin: var(--space-sm) 0;
        }
        
        .health-progress-bar {
            height: 100%;
            background: linear-gradient(90deg, var(--success), var(--secondary));
            border-radius: 0.25rem;
            transition: width var(--transition-slow);
        }
        
        /* Footer */
        .footer {
            margin-top: var(--space-xl);
            padding: var(--space-md) 0;
            border-top: 1px solid var(--dark-4);
            color: var(--light-4);
            font-size: 0.875rem;
        }
        
        /* Loading States */
        .loading {
            opacity: 0.7;
            position: relative;
        }
        
        .loading::after {
            content: '';
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.1), transparent);
            animation: loading 1.5s infinite;
        }
        
        @keyframes loading {
            0% { transform: translateX(-100%); }
            100% { transform: translateX(100%); }
        }
        
        /* Responsive */
        @media (max-width: 1024px) {
            .grid-cols-4 { grid-template-columns: repeat(2, 1fr); }
            .temperature-grid { grid-template-columns: 1fr; }
            .burner-grid { grid-template-columns: 1fr; }
        }
        
        @media (max-width: 768px) {
            .header-content { flex-direction: column; gap: var(--space-sm); }
            .grid-cols-2, .grid-cols-3 { grid-template-columns: 1fr; }
            .data-grid { grid-template-columns: 1fr; }
            .vfd-grid { grid-template-columns: repeat(2, 1fr); }
            .card { padding: var(--space-sm); }
        }
        
        @media (max-width: 480px) {
            .vfd-grid { grid-template-columns: 1fr; }
            .container { padding: 0 var(--space-sm); }
        }
        
        /* Utilities */
        .hidden { display: none !important; }
        .w-full { width: 100%; }
        .h-full { height: 100%; }
        .mt-xs { margin-top: var(--space-xs); }
        .mt-sm { margin-top: var(--space-sm); }
        .mt-md { margin-top: var(--space-md); }
        .mt-lg { margin-top: var(--space-lg); }
        .mb-xs { margin-bottom: var(--space-xs); }
        .mb-sm { margin-bottom: var(--space-sm); }
        .mb-md { margin-bottom: var(--space-md); }
        .mb-lg { margin-bottom: var(--space-lg); }
        .p-xs { padding: var(--space-xs); }
        .p-sm { padding: var(--space-sm); }
        .p-md { padding: var(--space-md); }
        .p-lg { padding: var(--space-lg); }
        
        /* Toast Notifications */
        .toast-container {
            position: fixed;
            bottom: var(--space-md);
            right: var(--space-md);
            z-index: 1000;
            display: flex;
            flex-direction: column;
            gap: var(--space-xs);
        }
        
        .toast {
            padding: var(--space-sm) var(--space-md);
            background: var(--dark-3);
            border: 1px solid var(--dark-4);
            border-radius: var(--radius-md);
            box-shadow: var(--shadow-lg);
            animation: slideIn 0.3s ease;
            display: flex;
            align-items: center;
            gap: var(--space-sm);
        }
        
        .toast.success { border-left: 4px solid var(--success); }
        .toast.warning { border-left: 4px solid var(--warning); }
        .toast.error { border-left: 4px solid var(--danger); }
        .toast.info { border-left: 4px solid var(--primary); }
        
        @keyframes slideIn {
            from { transform: translateX(100%); opacity: 0; }
            to { transform: translateX(0); opacity: 1; }
        }
    </style>
</head>
<body>
    <!-- Header -->
    <header class="header">
        <div class="container">
            <div class="header-content">
                <div class="logo">
                    <div class="logo-icon">
                        <i class="fas fa-industry"></i>
                    </div>
                    <div class="logo-text">
                        <h1>KH-01 INDUSTRIAL CONTROL</h1>
                        <div class="subtitle">REAL-TIME PROCESS MONITORING DASHBOARD</div>
                    </div>
                </div>
                <div class="flex items-center gap-md">
                    <div class="status-badge">
                        <div class="status-dot" id="mqttStatusDot"></div>
                        <span id="mqttStatusText">Connecting...</span>
                    </div>
                    <div class="text-sm text-muted" id="lastUpdate">
                        Last update: --
                    </div>
                </div>
            </div>
        </div>
    </header>

    <!-- Main Content -->
    <main class="container">
        <!-- System Health -->
        <section class="grid grid-cols-1 lg:grid-cols-4 gap-md mt-md">
            <div class="health-card col-span-1 lg:col-span-2" id="systemHealthCard">
                <div class="health-header">
                    <div class="health-indicator">
                        <div class="health-dot" id="healthDot"></div>
                        <h2 class="text-xl font-semibold">System Health</h2>
                    </div>
                    <div class="text-lg font-semibold" id="healthPercentage">0%</div>
                </div>
                <p class="text-muted mb-sm" id="healthMessage">Initializing...</p>
                <div class="health-progress">
                    <div class="health-progress-bar" id="healthProgressBar" style="width: 0%"></div>
                </div>
                <div class="flex justify-between mt-sm text-sm">
                    <span>Components Active</span>
                    <span id="componentsActive">0/0</span>
                </div>
            </div>
            
            <div class="card">
                <div class="card-title">
                    <div class="card-icon">
                        <i class="fas fa-clock"></i>
                    </div>
                    <h3 class="text-lg font-semibold">Uptime</h3>
                </div>
                <div class="data-value text-3xl" id="uptimeDisplay">00:00:00</div>
                <p class="text-muted text-sm mt-xs">System running time</p>
            </div>
            
            <div class="card">
                <div class="card-title">
                    <div class="card-icon">
                        <i class="fas fa-database"></i>
                    </div>
                    <h3 class="text-lg font-semibold">Data Rate</h3>
                </div>
                <div class="data-value text-3xl" id="dataRateDisplay">0/s</div>
                <p class="text-muted text-sm mt-xs">Messages per second</p>
            </div>
        </section>

        <!-- Temperature Monitoring -->
        <section class="mt-lg">
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <div class="card-icon">
                            <i class="fas fa-thermometer-half"></i>
                        </div>
                        <h2 class="text-xl font-semibold">Temperature Monitoring</h2>
                    </div>
                </div>
                <div class="temperature-grid mt-md">
                    <!-- Temperature cards will be populated by JavaScript -->
                    <div class="temp-card" id="tempP1">
                        <div class="temp-header">
                            <div class="temp-name">Reactor Core</div>
                            <div class="temp-probe">Probe #1</div>
                        </div>
                        <div class="temp-value">-- °C</div>
                    </div>
                    <div class="temp-card" id="tempP2">
                        <div class="temp-header">
                            <div class="temp-name">Primary Burner</div>
                            <div class="temp-probe">Probe #2</div>
                        </div>
                        <div class="temp-value">-- °C</div>
                    </div>
                    <div class="temp-card" id="tempP3">
                        <div class="temp-header">
                            <div class="temp-name">Secondary Burner</div>
                            <div class="temp-probe">Probe #3</div>
                        </div>
                        <div class="temp-value">-- °C</div>
                    </div>
                    <div class="temp-card" id="tempP4">
                        <div class="temp-header">
                            <div class="temp-name">Output Conveyor</div>
                            <div class="temp-probe">Probe #4</div>
                        </div>
                        <div class="temp-value">-- °C</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- Power & Burners -->
        <section class="grid grid-cols-1 lg:grid-cols-2 gap-md mt-md">
            <!-- Power Consumption -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <div class="card-icon">
                            <i class="fas fa-bolt"></i>
                        </div>
                        <h2 class="text-xl font-semibold">Power Consumption</h2>
                    </div>
                </div>
                <div class="data-grid mt-md">
                    <div class="data-item">
                        <div class="data-label">Phase A</div>
                        <div class="data-value" id="powerPhaseA">-- <span class="data-unit">A</span></div>
                    </div>
                    <div class="data-item">
                        <div class="data-label">Phase B</div>
                        <div class="data-value" id="powerPhaseB">-- <span class="data-unit">A</span></div>
                    </div>
                    <div class="data-item">
                        <div class="data-label">Phase C</div>
                        <div class="data-value" id="powerPhaseC">-- <span class="data-unit">A</span></div>
                    </div>
                </div>
            </div>

            <!-- Burner Status -->
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <div class="card-icon">
                            <i class="fas fa-fire"></i>
                        </div>
                        <h2 class="text-xl font-semibold">Burner Status</h2>
                    </div>
                </div>
                <div class="burner-grid mt-md">
                    <div class="burner-card" id="burner1">
                        <div class="burner-info">
                            <div class="burner-icon">
                                <i class="fas fa-fire"></i>
                            </div>
                            <div>
                                <div class="font-medium">Burner #1</div>
                                <div class="text-xs text-muted">Primary</div>
                            </div>
                        </div>
                        <div class="burner-status status-unknown">--</div>
                    </div>
                    <div class="burner-card" id="burner3">
                        <div class="burner-info">
                            <div class="burner-icon">
                                <i class="fas fa-fire"></i>
                            </div>
                            <div>
                                <div class="font-medium">Burner #3</div>
                                <div class="text-xs text-muted">Secondary</div>
                            </div>
                        </div>
                        <div class="burner-status status-unknown">--</div>
                    </div>
                    <div class="burner-card" id="burner4">
                        <div class="burner-info">
                            <div class="burner-icon">
                                <i class="fas fa-fire"></i>
                            </div>
                            <div>
                                <div class="font-medium">Burner #4</div>
                                <div class="text-xs text-muted">Tertiary</div>
                            </div>
                        </div>
                        <div class="burner-status status-unknown">--</div>
                    </div>
                    <div class="burner-card" id="burner6">
                        <div class="burner-info">
                            <div class="burner-icon">
                                <i class="fas fa-fire"></i>
                            </div>
                            <div>
                                <div class="font-medium">Burner #6</div>
                                <div class="text-xs text-muted">Quaternary</div>
                            </div>
                        </div>
                        <div class="burner-status status-unknown">--</div>
                    </div>
                </div>
            </div>
        </section>

        <!-- VFD Frequencies -->
        <section class="mt-md">
            <div class="card">
                <div class="card-header">
                    <div class="card-title">
                        <div class="card-icon">
                            <i class="fas fa-tachometer-alt"></i>
                        </div>
                        <h2 class="text-xl font-semibold">VFD Frequency Control</h2>
                    </div>
                </div>
                <div class="vfd-grid mt-md">
                    <!-- VFD cards will be populated by JavaScript -->
                    <div class="vfd-card" id="vfdBucket">
                        <div class="vfd-label">Bucket</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdInletScrew">
                        <div class="vfd-label">INLET Screw</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdAirLocker">
                        <div class="vfd-label">Air Locker</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdExhaust1">
                        <div class="vfd-label">Exhaust 1</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdExhaust2">
                        <div class="vfd-label">Exhaust 2</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdOutscrew1">
                        <div class="vfd-label">Outscrew 1</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdOutscrew2">
                        <div class="vfd-label">Outscrew 2</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdReactor">
                        <div class="vfd-label">Reactor</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                    <div class="vfd-card" id="vfdSyngas">
                        <div class="vfd-label">Syngas</div>
                        <div class="vfd-value">-- Hz</div>
                        <div class="vfd-progress">
                            <div class="vfd-progress-bar" style="width: 0%"></div>
                        </div>
                    </div>
                </div>
            </div>
        </section>
    </main>

    <!-- Footer -->
    <footer class="footer">
        <div class="container">
            <div class="flex flex-col md:flex-row justify-between items-center gap-sm">
                <div class="text-center md:text-left">
                    <div class="font-medium">KH-01 Industrial Control System</div>
                    <div class="text-xs text-muted">Version 3.0.0 | Real-time Monitoring Dashboard</div>
                </div>
                <div class="text-center md:text-right">
                    <div class="text-sm">MQTT Broker: t569f61e.ala.asia-southeast1.emqxsl.com:8883</div>
                    <div class="text-xs text-muted">WebSocket Clients: <span id="wsClients">0</span></div>
                </div>
            </div>
        </div>
    </footer>

    <!-- Toast Container -->
    <div class="toast-container" id="toastContainer"></div>

    <script>
        // Socket.IO connection
        const socket = io({
            reconnection: true,
            reconnectionAttempts: Infinity,
            reconnectionDelay: 1000
        });

        // Application state
        let appState = {
            lastUpdate: null,
            dataPoints: [],
            toastQueue: []
        };

        // DOM Elements
        const elements = {
            // Status
            mqttStatusDot: document.getElementById('mqttStatusDot'),
            mqttStatusText: document.getElementById('mqttStatusText'),
            lastUpdate: document.getElementById('lastUpdate'),
            
            // System Health
            systemHealthCard: document.getElementById('systemHealthCard'),
            healthDot: document.getElementById('healthDot'),
            healthMessage: document.getElementById('healthMessage'),
            healthPercentage: document.getElementById('healthPercentage'),
            healthProgressBar: document.getElementById('healthProgressBar'),
            componentsActive: document.getElementById('componentsActive'),
            
            // System Info
            uptimeDisplay: document.getElementById('uptimeDisplay'),
            dataRateDisplay: document.getElementById('dataRateDisplay'),
            wsClients: document.getElementById('wsClients'),
            
            // Temperatures
            tempP1: document.getElementById('tempP1'),
            tempP2: document.getElementById('tempP2'),
            tempP3: document.getElementById('tempP3'),
            tempP4: document.getElementById('tempP4'),
            
            // Power
            powerPhaseA: document.getElementById('powerPhaseA'),
            powerPhaseB: document.getElementById('powerPhaseB'),
            powerPhaseC: document.getElementById('powerPhaseC'),
            
            // Burners
            burner1: document.getElementById('burner1'),
            burner3: document.getElementById('burner3'),
            burner4: document.getElementById('burner4'),
            burner6: document.getElementById('burner6'),
            
            // VFD
            vfdBucket: document.getElementById('vfdBucket'),
            vfdInletScrew: document.getElementById('vfdInletScrew'),
            vfdAirLocker: document.getElementById('vfdAirLocker'),
            vfdExhaust1: document.getElementById('vfdExhaust1'),
            vfdExhaust2: document.getElementById('vfdExhaust2'),
            vfdOutscrew1: document.getElementById('vfdOutscrew1'),
            vfdOutscrew2: document.getElementById('vfdOutscrew2'),
            vfdReactor: document.getElementById('vfdReactor'),
            vfdSyngas: document.getElementById('vfdSyngas')
        };

        // Toast system
        function showToast(message, type = 'info', duration = 5000) {
            const toastContainer = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            
            const icons = {
                success: 'fas fa-check-circle',
                warning: 'fas fa-exclamation-triangle',
                error: 'fas fa-times-circle',
                info: 'fas fa-info-circle'
            };
            
            toast.innerHTML = `
                <i class="${icons[type] || icons.info}"></i>
                <span>${message}</span>
            `;
            
            toastContainer.appendChild(toast);
            
            setTimeout(() => {
                toast.style.animation = 'slideIn 0.3s ease reverse';
                setTimeout(() => toast.remove(), 300);
            }, duration);
            
            return toast;
        }

        // Formatting functions
        function formatTime(seconds) {
            const hours = Math.floor(seconds / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            const secs = seconds % 60;
            return `${hours.toString().padStart(2, '0')}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
        }

        function formatTimestamp(timestamp) {
            if (!timestamp) return '--';
            const date = new Date(timestamp);
            return date.toLocaleTimeString('en-US', {
                hour12: false,
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            });
        }

        // Update data rate
        function updateDataRate() {
            const now = Date.now();
            const windowStart = now - 10000; // 10 second window
            
            appState.dataPoints = appState.dataPoints.filter(t => t > windowStart);
            const rate = appState.dataPoints.length / 10;
            elements.dataRateDisplay.textContent = `${rate.toFixed(1)}/s`;
        }

        // Update UI functions
        function updateTemperature(id, data) {
            const element = elements[`tempP${id}`];
            if (!element || !data) return;
            
            const value = data.value;
            const unit = data.unit || '°C';
            const status = data.status || 'normal';
            const name = data.name || `Probe ${id}`;
            
            element.querySelector('.temp-value').textContent = value !== null ? `${value} ${unit}` : `-- ${unit}`;
            element.className = `temp-card status-${status}`;
            
            // Update status indicator
            element.style.borderLeftColor = {
                normal: '#0066cc',
                warning: '#ff9900',
                critical: '#ff3333'
            }[status] || '#0066cc';
        }

        function updatePower(phase, data) {
            const element = elements[`powerPhase${phase}`];
            if (!element || !data) return;
            
            const value = data.value;
            const unit = data.unit || 'A';
            
            element.innerHTML = value !== null ? 
                `<span class="data-value">${value} <span class="data-unit">${unit}</span></span>` :
                `<span class="data-value">-- <span class="data-unit">${unit}</span></span>`;
        }

        function updateBurner(id, data) {
            const element = elements[`burner${id}`];
            if (!element || !data) return;
            
            const status = data.status || 'unknown';
            const statusText = status === 'on' ? 'ON' : status === 'off' ? 'OFF' : '--';
            
            element.className = `burner-card status-${status}`;
            const statusElement = element.querySelector('.burner-status');
            statusElement.className = `burner-status status-${status}`;
            statusElement.textContent = statusText;
        }

        function updateVFD(id, data) {
            const element = elements[`vfd${id}`];
            if (!element || !data) return;
            
            const value = data.value;
            const unit = data.unit || 'Hz';
            const percentage = data.percentage || 0;
            
            element.querySelector('.vfd-value').textContent = value !== null ? `${value} ${unit}` : `-- ${unit}`;
            element.querySelector('.vfd-progress-bar').style.width = `${percentage}%`;
        }

        // Socket.IO event handlers
        socket.on('connect', () => {
            console.log('Connected to WebSocket server');
            showToast('Connected to server', 'success');
        });

        socket.on('disconnect', () => {
            console.log('Disconnected from WebSocket server');
            showToast('Disconnected from server', 'warning');
        });

        socket.on('system_status', (data) => {
            console.log('System status:', data);
            
            if (data.mqtt_connected) {
                elements.mqttStatusDot.classList.add('connected');
                elements.mqttStatusText.textContent = 'Connected';
                elements.mqttStatusText.style.color = '#00cc66';
            } else {
                elements.mqttStatusDot.classList.remove('connected');
                elements.mqttStatusText.textContent = 'Disconnected';
                elements.mqttStatusText.style.color = '#ff3333';
                
                if (data.error) {
                    showToast(`MQTT Error: ${data.error}`, 'error');
                }
            }
        });

        socket.on('data_update', (data) => {
            console.log('Data update received:', data);
            
            // Track data point for rate calculation
            appState.dataPoints.push(Date.now());
            updateDataRate();
            
            // Update system information
            if (data.system) {
                elements.uptimeDisplay.textContent = formatTime(data.system.uptime);
                elements.wsClients.textContent = data.system.websocket_clients || 0;
                elements.lastUpdate.textContent = `Last update: ${formatTimestamp(data.system.last_update)}`;
            }
            
            // Update system health
            if (data.health) {
                const health = data.health;
                const percentage = Math.round((health.components_ok / health.components_total) * 100);
                
                elements.healthDot.className = `health-dot status-${health.overall}`;
                elements.healthMessage.textContent = health.message;
                elements.healthPercentage.textContent = `${percentage}%`;
                elements.healthProgressBar.style.width = `${percentage}%`;
                elements.componentsActive.textContent = `${health.components_ok}/${health.components_total}`;
                
                // Update health card style
                elements.systemHealthCard.className = `health-card col-span-1 lg:col-span-2 status-${health.overall}`;
            }
            
            // Update temperatures
            if (data.temperatures) {
                Object.entries(data.temperatures).forEach(([key, tempData]) => {
                    const id = key.replace('p', '');
                    updateTemperature(id, tempData);
                });
            }
            
            // Update power
            if (data.power) {
                Object.entries(data.power).forEach(([key, powerData]) => {
                    const phase = key.replace('phase', '');
                    updatePower(phase, powerData);
                });
            }
            
            // Update burners
            if (data.burners) {
                Object.entries(data.burners).forEach(([key, burnerData]) => {
                    const id = key.replace('burner', '');
                    updateBurner(id, burnerData);
                });
            }
            
            // Update VFD frequencies
            if (data.vfd) {
                const vfdMapping = {
                    'Bucket': 'Bucket',
                    'INLETScrew': 'InletScrew',
                    'airlocker': 'AirLocker',
                    'exaust1': 'Exhaust1',
                    'exaust2': 'Exhaust2',
                    'outscrew1': 'Outscrew1',
                    'outscrew2': 'Outscrew2',
                    'reactor': 'Reactor',
                    'syngas': 'Syngas'
                };
                
                Object.entries(data.vfd).forEach(([key, vfdData]) => {
                    const elementId = vfdMapping[key];
                    if (elementId) {
                        updateVFD(elementId, vfdData);
                    }
                });
            }
        });

        // Initialize application
        function init() {
            console.log('Dashboard initialized');
            
            // Request initial data
            socket.emit('request_update');
            
            // Set up periodic tasks
            setInterval(() => {
                updateDataRate();
            }, 5000);
            
            // Initial toast
            setTimeout(() => {
                showToast('Dashboard loaded successfully', 'success');
            }, 1000);
            
            // Add keyboard shortcuts
            document.addEventListener('keydown', (e) => {
                if (e.key === 'r' || e.key === 'R') {
                    socket.emit('request_update');
                    showToast('Data refresh requested', 'info');
                }
                if (e.key === 'd' || e.key === 'D') {
                    showToast('Debug mode activated', 'info');
                    console.log('Current state:', appState);
                }
            });
        }

        // Start application
        document.addEventListener('DOMContentLoaded', init);
    </script>
</body>
</html>
"""

# =====================================================
# MAIN EXECUTION
# =====================================================
# =====================================================
# START MQTT (RUNS ON RENDER/GUNICORN IMPORT)
# =====================================================
# IMPORTANT: This must be AFTER start_mqtt_client() is defined
mqtt_thread = threading.Thread(target=start_mqtt_client, daemon=True)
mqtt_thread.start()


# =====================================================
# LOCAL RUN ONLY (NOT USED ON RENDER)
# =====================================================
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )

    print("=" * 80)
    print("KH-01 INDUSTRIAL CONTROL DASHBOARD")
    print("=" * 80)
    print("Starting Web Server (LOCAL TEST)...")
    print("=" * 80)

    port = int(os.environ.get("PORT", "5000"))
    print(f"Starting Web Server on http://127.0.0.1:{port}")

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=True,
        use_reloader=False
    )

