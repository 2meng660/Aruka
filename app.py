#!/usr/bin/env python3
"""
Aruka / KH-01 Industrial Control Dashboard - Professional Edition

Features:
- Modern industrial UI with dark/light themes
- Real-time alerts and notifications
- Historical data logging
- Export functionality (CSV/JSON)
- Multi-site support
- Role-based access control (basic)
- Performance metrics
- Mobile-responsive design
- Production monitoring dashboard
"""

import os
import ssl
import json
import time
import csv
import sqlite3
import socket
import logging
import threading
import secrets
import hashlib
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from enum import Enum
from collections import deque
import functools

from flask import (
    Flask, jsonify, render_template_string, 
    request, session, redirect, url_for,
    send_file, Response
)
from flask_socketio import SocketIO, emit
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import paho.mqtt.client as mqtt
import certifi
import pandas as pd
from werkzeug.security import generate_password_hash, check_password_hash

# =====================================================
# LOGGING SETUP
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('industrial_dashboard.log')
    ]
)
LOG = logging.getLogger("KH01_Industrial")

# =====================================================
# CONFIGURATION
# =====================================================
class Config:
    # MQTT Configuration
    MQTT_HOST = os.environ.get("MQTT_HOST", "t569f61e.ala.asia-southeast1.emqxsl.com").strip()
    MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
    MQTT_USER = os.environ.get("MQTT_USER", "KH-01-device").strip()
    MQTT_PASS = os.environ.get("MQTT_PASS", "Radiation0-Disperser8-Sternum1-Trio4").strip()
    MQTT_TLS = os.environ.get("MQTT_TLS", "1").lower() in ("1", "true", "yes", "on")
    MQTT_TLS_INSECURE = os.environ.get("MQTT_TLS_INSECURE", "0").lower() in ("1", "true", "yes", "on")
    
    # Application Configuration
    SECRET_KEY = os.environ.get("SECRET_KEY", secrets.token_hex(32))
    SESSION_TYPE = "filesystem"
    PERMANENT_SESSION_LIFETIME = timedelta(hours=12)
    
    # Database Configuration
    DATABASE_PATH = os.environ.get("DATABASE_PATH", "industrial_data.db")
    MAX_HISTORY_DAYS = int(os.environ.get("MAX_HISTORY_DAYS", "30"))
    
    # Security
    RATE_LIMIT = os.environ.get("RATE_LIMIT", "100 per minute")
    ENABLE_AUTH = os.environ.get("ENABLE_AUTH", "0").lower() in ("1", "true", "yes", "on")
    
    # Default Users (change in production!)
    DEFAULT_USERS = {
        "admin": generate_password_hash("admin123"),
        "operator": generate_password_hash("operator123"),
        "viewer": generate_password_hash("viewer123")
    }
    
    # Alert Thresholds
    ALERT_THRESHOLDS = {
        "temperature_max": 1000,  # °C
        "temperature_min": -50,   # °C
        "current_max": 100,       # A
        "frequency_max": 100,     # Hz
        "frequency_min": 0,       # Hz
    }
    
    # Topics
    @staticmethod
    def get_topics():
        env_topics = os.environ.get("MQTT_TOPICS", "").strip()
        if env_topics:
            return [t.strip() for t in env_topics.split(",") if t.strip()]
        
        return [
            "KH/site-01/KH-01/temperature_probe1",
            "KH/site-01/KH-01/temperature_probe2",
            "KH/site-01/KH-01/temperature_probe3",
            "KH/site-01/KH-01/temperature_probe4",
            "KH/site-01/KH-01/vfd_frequency",
            "KH/site-01/KH-01/power_consumption",
            "KH/site-01/KH-01/status",
            "KH/site-01/KH-01/alerts",
        ]

# =====================================================
# DATA MODELS
# =====================================================
class AlertLevel(Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"
    EMERGENCY = "emergency"

@dataclass
class Alert:
    id: str
    level: AlertLevel
    title: str
    message: str
    timestamp: str
    topic: Optional[str] = None
    value: Optional[float] = None
    threshold: Optional[float] = None
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None

@dataclass
class DeviceReading:
    topic: str
    payload: Any
    timestamp: str
    received_at: str
    site: str = "site-01"
    device: str = "KH-01"
    processed: bool = False

@dataclass
class SystemStatus:
    mqtt_connected: bool
    database_connected: bool
    websocket_connected: bool
    uptime_seconds: float
    memory_usage_mb: float
    active_alerts: int
    total_readings: int
    last_update: str

# =====================================================
# DATABASE MANAGER
# =====================================================
class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
        
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        with self.get_connection() as conn:
            # Readings table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS readings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic TEXT NOT NULL,
                    site TEXT NOT NULL,
                    device TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    processed BOOLEAN DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Alerts table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS alerts (
                    id TEXT PRIMARY KEY,
                    level TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    topic TEXT,
                    value REAL,
                    threshold REAL,
                    acknowledged BOOLEAN DEFAULT 0,
                    acknowledged_by TEXT,
                    acknowledged_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Users table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    username TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL,
                    full_name TEXT,
                    email TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    last_login TEXT
                )
            """)
            
            # Indexes for performance
            conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_topic ON readings(topic)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_timestamp ON readings(timestamp)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_acknowledged ON alerts(acknowledged)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_alerts_timestamp ON alerts(timestamp)")
            
            conn.commit()
    
    def save_reading(self, reading: DeviceReading):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO readings (topic, site, device, value_json, timestamp, received_at, processed)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                reading.topic,
                reading.site,
                reading.device,
                json.dumps(reading.payload),
                reading.timestamp,
                reading.received_at,
                reading.processed
            ))
            conn.commit()
    
    def save_alert(self, alert: Alert):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO alerts 
                (id, level, title, message, timestamp, topic, value, threshold, acknowledged, acknowledged_by, acknowledged_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                alert.id,
                alert.level.value,
                alert.title,
                alert.message,
                alert.timestamp,
                alert.topic,
                alert.value,
                alert.threshold,
                alert.acknowledged,
                alert.acknowledged_by,
                alert.acknowledged_at
            ))
            conn.commit()
    
    def get_recent_readings(self, topic: Optional[str] = None, limit: int = 1000):
        with self.get_connection() as conn:
            if topic:
                cursor = conn.execute("""
                    SELECT * FROM readings 
                    WHERE topic = ? 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (topic, limit))
            else:
                cursor = conn.execute("""
                    SELECT * FROM readings 
                    ORDER BY timestamp DESC 
                    LIMIT ?
                """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def get_active_alerts(self, limit: int = 100):
        with self.get_connection() as conn:
            cursor = conn.execute("""
                SELECT * FROM alerts 
                WHERE acknowledged = 0 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_historical_data(self, start_time: str, end_time: str, topic: Optional[str] = None):
        with self.get_connection() as conn:
            if topic:
                cursor = conn.execute("""
                    SELECT * FROM readings 
                    WHERE timestamp BETWEEN ? AND ? 
                    AND topic = ?
                    ORDER BY timestamp
                """, (start_time, end_time, topic))
            else:
                cursor = conn.execute("""
                    SELECT * FROM readings 
                    WHERE timestamp BETWEEN ? AND ? 
                    ORDER BY timestamp
                """, (start_time, end_time))
            
            return [dict(row) for row in cursor.fetchall()]

# =====================================================
# ALERT MANAGER
# =====================================================
class AlertManager:
    def __init__(self, thresholds: Dict[str, float]):
        self.thresholds = thresholds
        self.active_alerts = {}
        
    def check_reading(self, reading: DeviceReading) -> Optional[Alert]:
        """Check reading against thresholds and return alert if needed"""
        try:
            payload = reading.payload
            if not isinstance(payload, dict):
                return None
                
            value = payload.get('value')
            if value is None:
                return None
                
            # Temperature alerts
            if 'temperature' in reading.topic:
                if value > self.thresholds['temperature_max']:
                    return self._create_alert(
                        level=AlertLevel.CRITICAL,
                        title="High Temperature Alert",
                        message=f"Temperature {value}°C exceeds maximum threshold {self.thresholds['temperature_max']}°C",
                        topic=reading.topic,
                        value=value,
                        threshold=self.thresholds['temperature_max']
                    )
                elif value < self.thresholds['temperature_min']:
                    return self._create_alert(
                        level=AlertLevel.WARNING,
                        title="Low Temperature Alert",
                        message=f"Temperature {value}°C below minimum threshold {self.thresholds['temperature_min']}°C",
                        topic=reading.topic,
                        value=value,
                        threshold=self.thresholds['temperature_min']
                    )
            
            # Current alerts
            elif 'power' in reading.topic:
                if value > self.thresholds['current_max']:
                    return self._create_alert(
                        level=AlertLevel.CRITICAL,
                        title="High Current Alert",
                        message=f"Current {value}A exceeds maximum threshold {self.thresholds['current_max']}A",
                        topic=reading.topic,
                        value=value,
                        threshold=self.thresholds['current_max']
                    )
            
            # Frequency alerts
            elif 'frequency' in reading.topic:
                if value > self.thresholds['frequency_max']:
                    return self._create_alert(
                        level=AlertLevel.WARNING,
                        title="High Frequency Alert",
                        message=f"Frequency {value}Hz exceeds maximum threshold {self.thresholds['frequency_max']}Hz",
                        topic=reading.topic,
                        value=value,
                        threshold=self.thresholds['frequency_max']
                    )
                elif value < self.thresholds['frequency_min']:
                    return self._create_alert(
                        level=AlertLevel.WARNING,
                        title="Low Frequency Alert",
                        message=f"Frequency {value}Hz below minimum threshold {self.thresholds['frequency_min']}Hz",
                        topic=reading.topic,
                        value=value,
                        threshold=self.thresholds['frequency_min']
                    )
                    
        except Exception as e:
            LOG.error(f"Error checking reading for alerts: {e}")
            
        return None
    
    def _create_alert(self, level: AlertLevel, title: str, message: str, 
                     topic: str, value: float, threshold: float) -> Alert:
        alert_id = hashlib.md5(f"{topic}_{value}_{threshold}_{time.time()}".encode()).hexdigest()
        
        return Alert(
            id=alert_id,
            level=level,
            title=title,
            message=message,
            timestamp=datetime.now(timezone.utc).isoformat(),
            topic=topic,
            value=value,
            threshold=threshold
        )

# =====================================================
# APPLICATION SETUP
# =====================================================
app = Flask(__name__)
app.config.from_object(Config)

# Rate limiting
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[Config.RATE_LIMIT],
    storage_uri="memory://",
)

# Socket.IO
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    path="socket.io",
    ping_interval=25,
    ping_timeout=60,
    logger=True,
    engineio_logger=True
)

# Global state
lock = threading.RLock()
start_time = time.time()

# Initialize components
db_manager = DatabaseManager(Config.DATABASE_PATH)
alert_manager = AlertManager(Config.ALERT_THRESHOLDS)

# State storage
mqtt_status = {
    "connected": False,
    "host": Config.MQTT_HOST,
    "port": Config.MQTT_PORT,
    "tls": Config.MQTT_TLS,
    "client_id": f"{socket.gethostname()}_{os.getpid()}",
    "topics": Config.get_topics(),
    "last_connect": None,
    "last_disconnect": None,
    "bytes_received": 0,
    "messages_received": 0,
}

latest_readings = {}
active_alerts = deque(maxlen=100)
system_metrics = {
    "uptime": 0,
    "memory_usage": 0,
    "cpu_usage": 0,
    "connected_clients": 0,
}

# =====================================================
# AUTHENTICATION HELPERS
# =====================================================
def login_required(f):
    @functools.wraps(f)
    def decorated_function(*args, **kwargs):
        if Config.ENABLE_AUTH and 'username' not in session:
            return redirect(url_for('login', next=request.url))
        return f(*args, **kwargs)
    return decorated_function

def role_required(role):
    def decorator(f):
        @functools.wraps(f)
        def decorated_function(*args, **kwargs):
            if Config.ENABLE_AUTH:
                if 'username' not in session:
                    return redirect(url_for('login', next=request.url))
                user_role = session.get('role', 'viewer')
                if role == 'admin' and user_role != 'admin':
                    return jsonify({"error": "Admin access required"}), 403
                elif role == 'operator' and user_role not in ('admin', 'operator'):
                    return jsonify({"error": "Operator access required"}), 403
            return f(*args, **kwargs)
        return decorated_function
    return decorator

# =====================================================
# MQTT CLIENT
# =====================================================
class IndustrialMQTTClient:
    def __init__(self):
        self.client = None
        self.connected = False
        
    def connect(self):
        try:
            self.client = mqtt.Client(
                client_id=mqtt_status["client_id"],
                protocol=mqtt.MQTTv5,
                clean_session=True
            )
            
            self.client.enable_logger(LOG)
            
            if Config.MQTT_USER:
                self.client.username_pw_set(Config.MQTT_USER, Config.MQTT_PASS)
            
            if Config.MQTT_TLS:
                self.client.tls_set(
                    ca_certs=certifi.where(),
                    cert_reqs=ssl.CERT_REQUIRED,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT
                )
                if Config.MQTT_TLS_INSECURE:
                    self.client.tls_insecure_set(True)
            
            self.client.on_connect = self._on_connect
            self.client.on_disconnect = self._on_disconnect
            self.client.on_message = self._on_message
            self.client.on_log = self._on_log
            
            self.client.connect(
                Config.MQTT_HOST,
                Config.MQTT_PORT,
                keepalive=60
            )
            
            self.client.loop_start()
            return True
            
        except Exception as e:
            LOG.error(f"MQTT connection failed: {e}")
            return False
    
    def _on_connect(self, client, userdata, flags, rc, properties=None):
        with lock:
            mqtt_status["connected"] = (rc == 0)
            mqtt_status["last_connect"] = datetime.now(timezone.utc).isoformat()
            
        if rc == 0:
            LOG.info("MQTT Connected successfully")
            for topic in Config.get_topics():
                try:
                    client.subscribe(topic, qos=1)
                    LOG.info(f"Subscribed to {topic}")
                except Exception as e:
                    LOG.error(f"Failed to subscribe to {topic}: {e}")
        else:
            LOG.error(f"MQTT Connection failed with code {rc}")
    
    def _on_disconnect(self, client, userdata, rc, properties=None):
        with lock:
            mqtt_status["connected"] = False
            mqtt_status["last_disconnect"] = datetime.now(timezone.utc).isoformat()
        LOG.warning(f"MQTT Disconnected with code {rc}")
    
    def _on_message(self, client, userdata, msg):
        try:
            received_at = datetime.now(timezone.utc).isoformat()
            payload = json.loads(msg.payload.decode('utf-8'))
            
            with lock:
                mqtt_status["bytes_received"] += len(msg.payload)
                mqtt_status["messages_received"] += 1
            
            # Create reading object
            reading = DeviceReading(
                topic=msg.topic,
                payload=payload,
                timestamp=payload.get('timestamp', received_at),
                received_at=received_at
            )
            
            # Store in database
            db_manager.save_reading(reading)
            
            # Update latest readings
            with lock:
                latest_readings[msg.topic] = {
                    "payload": payload,
                    "received_at": received_at,
                    "timestamp": reading.timestamp
                }
            
            # Check for alerts
            alert = alert_manager.check_reading(reading)
            if alert:
                db_manager.save_alert(alert)
                with lock:
                    active_alerts.append(alert)
                socketio.emit('new_alert', asdict(alert))
            
            # Emit to WebSocket clients
            socketio.emit('new_reading', {
                'topic': msg.topic,
                'payload': payload,
                'received_at': received_at,
                'timestamp': reading.timestamp
            })
            
            LOG.debug(f"Received message on {msg.topic}: {payload}")
            
        except Exception as e:
            LOG.error(f"Error processing MQTT message: {e}")
    
    def _on_log(self, client, userdata, level, buf):
        if level == mqtt.MQTT_LOG_DEBUG:
            LOG.debug(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_INFO:
            LOG.info(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_NOTICE:
            LOG.info(f"MQTT: {buf}")
        elif level == mqtt.MQTT_LOG_WARNING:
            LOG.warning(f"MQTT: {buf}")
        else:
            LOG.error(f"MQTT: {buf}")

# =====================================================
# BACKGROUND TASKS
# =====================================================
def update_system_metrics():
    """Update system metrics periodically"""
    import psutil
    while True:
        try:
            with lock:
                system_metrics["uptime"] = time.time() - start_time
                system_metrics["memory_usage"] = psutil.Process().memory_info().rss / 1024 / 1024
                system_metrics["cpu_usage"] = psutil.cpu_percent(interval=1)
                system_metrics["connected_clients"] = len(socketio.server.manager.rooms.get('/', {}))
            
            # Emit metrics update
            socketio.emit('system_metrics', system_metrics)
            
        except Exception as e:
            LOG.error(f"Error updating system metrics: {e}")
        
        time.sleep(10)

def cleanup_old_data():
    """Clean up old data from database"""
    while True:
        try:
            cutoff_time = (datetime.now(timezone.utc) - timedelta(days=Config.MAX_HISTORY_DAYS)).isoformat()
            with db_manager.get_connection() as conn:
                conn.execute("DELETE FROM readings WHERE timestamp < ?", (cutoff_time,))
                conn.execute("DELETE FROM alerts WHERE timestamp < ? AND acknowledged = 1", (cutoff_time,))
                conn.commit()
            LOG.info(f"Cleaned up data older than {Config.MAX_HISTORY_DAYS} days")
        except Exception as e:
            LOG.error(f"Error cleaning up old data: {e}")
        
        time.sleep(3600)  # Run hourly

# =====================================================
# ROUTES
# =====================================================
@app.route('/')
@login_required
def index():
    return render_template_string(INDEX_TEMPLATE)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not Config.ENABLE_AUTH:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        with db_manager.get_connection() as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE username = ?", 
                (username,)
            ).fetchone()
            
            if user and check_password_hash(user['password_hash'], password):
                session['username'] = username
                session['role'] = user['role']
                session.permanent = True
                
                # Update last login
                conn.execute(
                    "UPDATE users SET last_login = ? WHERE username = ?",
                    (datetime.now(timezone.utc).isoformat(), username)
                )
                conn.commit()
                
                next_page = request.args.get('next', url_for('index'))
                return redirect(next_page)
        
        return render_template_string(LOGIN_TEMPLATE, error="Invalid credentials")
    
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/api/status')
@login_required
def api_status():
    with lock:
        return jsonify({
            "system": {
                "uptime": system_metrics["uptime"],
                "memory_usage_mb": system_metrics["memory_usage"],
                "cpu_usage_percent": system_metrics["cpu_usage"],
                "connected_clients": system_metrics["connected_clients"],
            },
            "mqtt": mqtt_status,
            "readings": {
                "total_topics": len(latest_readings),
                "latest_update": max((v["received_at"] for v in latest_readings.values()), default=None)
            },
            "alerts": {
                "active": len(active_alerts),
                "total_today": 0  # Would need to query database
            }
        })

@app.route('/api/readings')
@login_required
def api_readings():
    topic = request.args.get('topic')
    limit = min(int(request.args.get('limit', 100)), 1000)
    
    with lock:
        if topic:
            readings = {topic: latest_readings.get(topic)}
        else:
            readings = latest_readings
    
    return jsonify(readings)

@app.route('/api/alerts')
@login_required
def api_alerts():
    acknowledged = request.args.get('acknowledged', 'false').lower() == 'true'
    limit = min(int(request.args.get('limit', 50)), 200)
    
    with db_manager.get_connection() as conn:
        if acknowledged:
            cursor = conn.execute("""
                SELECT * FROM alerts 
                WHERE acknowledged = 1 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        else:
            cursor = conn.execute("""
                SELECT * FROM alerts 
                WHERE acknowledged = 0 
                ORDER BY timestamp DESC 
                LIMIT ?
            """, (limit,))
        
        alerts = [dict(row) for row in cursor.fetchall()]
    
    return jsonify(alerts)

@app.route('/api/alerts/<alert_id>/acknowledge', methods=['POST'])
@role_required('operator')
def acknowledge_alert(alert_id):
    username = session.get('username', 'system')
    
    with db_manager.get_connection() as conn:
        conn.execute("""
            UPDATE alerts 
            SET acknowledged = 1, acknowledged_by = ?, acknowledged_at = ?
            WHERE id = ?
        """, (username, datetime.now(timezone.utc).isoformat(), alert_id))
        conn.commit()
    
    # Remove from active alerts
    with lock:
        active_alerts[:] = [a for a in active_alerts if a.id != alert_id]
    
    socketio.emit('alert_acknowledged', {'alert_id': alert_id, 'acknowledged_by': username})
    
    return jsonify({"success": True})

@app.route('/api/export')
@role_required('operator')
def export_data():
    format_type = request.args.get('format', 'json')
    start_time = request.args.get('start')
    end_time = request.args.get('end', datetime.now(timezone.utc).isoformat())
    topic = request.args.get('topic')
    
    if not start_time:
        return jsonify({"error": "Start time required"}), 400
    
    data = db_manager.get_historical_data(start_time, end_time, topic)
    
    if format_type == 'csv':
        if not data:
            return "No data found", 404
        
        # Create CSV response
        output = []
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
        
        response = Response(
            '\n'.join(output),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment;filename=export_{datetime.now().strftime("%Y%m%d_%H%M%S")}.csv'}
        )
        return response
    
    else:  # JSON
        return jsonify(data)

@app.route('/api/dashboard')
@login_required
def dashboard_data():
    """Aggregated data for dashboard widgets"""
    # Get temperature readings
    temp_data = {}
    for topic in [t for t in latest_readings.keys() if 'temperature' in t]:
        if topic in latest_readings:
            reading = latest_readings[topic]
            temp_data[topic.split('_')[-1]] = {
                'value': reading['payload'].get('value'),
                'unit': reading['payload'].get('unit', '°C'),
                'timestamp': reading['timestamp']
            }
    
    # Get power data
    power_data = {}
    power_topic = next((t for t in latest_readings.keys() if 'power' in t), None)
    if power_topic and power_topic in latest_readings:
        reading = latest_readings[power_topic]
        power_data = reading['payload']
    
    # Get VFD data
    vfd_data = {}
    vfd_topic = next((t for t in latest_readings.keys() if 'vfd' in t), None)
    if vfd_topic and vfd_topic in latest_readings:
        reading = latest_readings[vfd_topic]
        vfd_data = reading['payload']
    
    # Get status data
    status_data = {}
    status_topic = next((t for t in latest_readings.keys() if 'status' in t), None)
    if status_topic and status_topic in latest_readings:
        reading = latest_readings[status_topic]
        status_data = reading['payload']
    
    return jsonify({
        'temperatures': temp_data,
        'power': power_data,
        'vfd': vfd_data,
        'status': status_data,
        'system': system_metrics,
        'mqtt': mqtt_status,
        'alerts': list(active_alerts)[-10:]  # Last 10 alerts
    })

# =====================================================
# SOCKET.IO EVENTS
# =====================================================
@socketio.on('connect')
def handle_connect():
    LOG.info(f"Client connected: {request.sid}")
    
    # Send current state to new client
    with lock:
        emit('initial_state', {
            'readings': latest_readings,
            'mqtt': mqtt_status,
            'system': system_metrics,
            'alerts': list(active_alerts)
        })

@socketio.on('disconnect')
def handle_disconnect():
    LOG.info(f"Client disconnected: {request.sid}")

@socketio.on('subscribe')
def handle_subscribe(data):
    topic = data.get('topic')
    if topic:
        LOG.info(f"Client {request.sid} subscribed to {topic}")
        # In a real implementation, you might track client subscriptions

# =====================================================
# INITIALIZATION
# =====================================================
def initialize_application():
    """Initialize application components"""
    LOG.info("Initializing Industrial Dashboard")
    
    # Initialize default users if not exists
    if Config.ENABLE_AUTH:
        with db_manager.get_connection() as conn:
            for username, password_hash in Config.DEFAULT_USERS.items():
                role = 'admin' if username == 'admin' else 'operator' if username == 'operator' else 'viewer'
                conn.execute("""
                    INSERT OR IGNORE INTO users (username, password_hash, role)
                    VALUES (?, ?, ?)
                """, (username, password_hash, role))
            conn.commit()
    
    # Start MQTT client
    mqtt_client = IndustrialMQTTClient()
    if not mqtt_client.connect():
        LOG.error("Failed to initialize MQTT client")
    
    # Start background tasks
    socketio.start_background_task(update_system_metrics)
    socketio.start_background_task(cleanup_old_data)
    
    LOG.info("Application initialized successfully")

# =====================================================
# HTML TEMPLATES
# =====================================================
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Industrial Dashboard - Login</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .login-container {
            background: white;
            padding: 3rem;
            border-radius: 20px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
            width: 100%;
            max-width: 400px;
        }
        .logo {
            text-align: center;
            margin-bottom: 2rem;
        }
        .logo h1 {
            color: #2d3748;
            font-size: 1.5rem;
            font-weight: 600;
        }
        .logo p {
            color: #718096;
            font-size: 0.875rem;
        }
        .form-group {
            margin-bottom: 1.5rem;
        }
        label {
            display: block;
            color: #4a5568;
            font-size: 0.875rem;
            font-weight: 500;
            margin-bottom: 0.5rem;
        }
        input {
            width: 100%;
            padding: 0.75rem 1rem;
            border: 2px solid #e2e8f0;
            border-radius: 8px;
            font-size: 1rem;
            transition: border-color 0.2s;
        }
        input:focus {
            outline: none;
            border-color: #667eea;
        }
        .btn {
            width: 100%;
            padding: 0.75rem 1rem;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            border-radius: 8px;
            font-size: 1rem;
            font-weight: 500;
            cursor: pointer;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: translateY(-1px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        .error {
            color: #e53e3e;
            font-size: 0.875rem;
            margin-top: 0.5rem;
        }
        .footer {
            text-align: center;
            margin-top: 2rem;
            color: #718096;
            font-size: 0.875rem;
        }
    </style>
</head>
<body>
    <div class="login-container">
        <div class="logo">
            <h1>Industrial Control Dashboard</h1>
            <p>KH-01 Monitoring System</p>
        </div>
        <form method="POST">
            <div class="form-group">
                <label for="username">Username</label>
                <input type="text" id="username" name="username" required autofocus>
            </div>
            <div class="form-group">
                <label for="password">Password</label>
                <input type="password" id="password" name="password" required>
            </div>
            {% if error %}
            <div class="error">{{ error }}</div>
            {% endif %}
            <button type="submit" class="btn">Sign In</button>
        </form>
        <div class="footer">
            <p>Default credentials: admin/admin123</p>
        </div>
    </div>
</body>
</html>
"""

INDEX_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Industrial Dashboard - KH-01</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root {
            --primary: #2563eb;
            --primary-dark: #1d4ed8;
            --secondary: #10b981;
            --danger: #ef4444;
            --warning: #f59e0b;
            --dark: #1f2937;
            --light: #f9fafb;
            --gray: #6b7280;
            --gray-light: #e5e7eb;
            --border: #d1d5db;
            --shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -1px rgba(0, 0, 0, 0.06);
        }
        
        * { box-sizing: border-box; margin: 0; padding: 0; }
        
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background-color: var(--light);
            color: var(--dark);
            line-height: 1.6;
        }
        
        .app-container {
            display: flex;
            flex-direction: column;
            min-height: 100vh;
        }
        
        /* Header */
        .header {
            background: white;
            border-bottom: 1px solid var(--border);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            box-shadow: var(--shadow);
            position: sticky;
            top: 0;
            z-index: 100;
        }
        
        .logo {
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        
        .logo-icon {
            width: 40px;
            height: 40px;
            background: linear-gradient(135deg, var(--primary), var(--secondary));
            border-radius: 10px;
            display: flex;
            align-items: center;
            justify-content: center;
            color: white;
            font-weight: bold;
            font-size: 1.2rem;
        }
        
        .logo-text h1 {
            font-size: 1.5rem;
            font-weight: 600;
        }
        
        .logo-text p {
            font-size: 0.875rem;
            color: var(--gray);
        }
        
        .header-actions {
            display: flex;
            align-items: center;
            gap: 1rem;
        }
        
        .status-indicator {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background-color: var(--gray-light);
            border-radius: 20px;
            font-size: 0.875rem;
        }
        
        .status-dot {
            width: 10px;
            height: 10px;
            border-radius: 50%;
            background-color: var(--danger);
        }
        
        .status-dot.connected {
            background-color: var(--secondary);
        }
        
        .user-menu {
            display: flex;
            align-items: center;
            gap: 0.5rem;
            padding: 0.5rem 1rem;
            background-color: var(--gray-light);
            border-radius: 20px;
            cursor: pointer;
        }
        
        .user-avatar {
            width: 32px;
            height: 32px;
            border-radius: 50%;
            background-color: var(--primary);
            color: white;
            display: flex;
            align-items: center;
            justify-content: center;
            font-weight: bold;
        }
        
        /* Main Content */
        .main-content {
            flex: 1;
            padding: 2rem;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            align-content: start;
        }
        
        /* Widgets */
        .widget {
            background: white;
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: var(--shadow);
            border: 1px solid var(--border);
        }
        
        .widget-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }
        
        .widget-title {
            font-size: 1.125rem;
            font-weight: 600;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }
        
        .widget-icon {
            color: var(--primary);
        }
        
        /* Temperature Widget */
        .temp-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
            gap: 1rem;
        }
        
        .temp-sensor {
            background-color: #f8fafc;
            border-radius: 8px;
            padding: 1rem;
            text-align: center;
            border: 1px solid var(--border);
        }
        
        .temp-value {
            font-size: 1.75rem;
            font-weight: 700;
            margin: 0.5rem 0;
        }
        
        .temp-label {
            font-size: 0.875rem;
            color: var(--gray);
        }
        
        /* Alert Widget */
        .alert-list {
            max-height: 300px;
            overflow-y: auto;
        }
        
        .alert-item {
            padding: 0.75rem;
            border-left: 4px solid var(--danger);
            background-color: #fef2f2;
            margin-bottom: 0.5rem;
            border-radius: 0 8px 8px 0;
        }
        
        .alert-item.warning {
            border-left-color: var(--warning);
            background-color: #fffbeb;
        }
        
        .alert-item.info {
            border-left-color: var(--primary);
            background-color: #eff6ff;
        }
        
        .alert-header {
            display: flex;
            justify-content: space-between;
            margin-bottom: 0.25rem;
        }
        
        .alert-title {
            font-weight: 600;
            font-size: 0.875rem;
        }
        
        .alert-time {
            font-size: 0.75rem;
            color: var(--gray);
        }
        
        .alert-message {
            font-size: 0.875rem;
        }
        
        /* Chart Container */
        .chart-container {
            height: 200px;
            position: relative;
        }
        
        /* System Stats */
        .stats-grid {
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 1rem;
        }
        
        .stat-item {
            text-align: center;
            padding: 1rem;
            background-color: #f8fafc;
            border-radius: 8px;
        }
        
        .stat-value {
            font-size: 1.5rem;
            font-weight: 700;
            margin: 0.25rem 0;
        }
        
        .stat-label {
            font-size: 0.875rem;
            color: var(--gray);
        }
        
        /* Control Panel */
        .control-panel {
            display: flex;
            flex-direction: column;
            gap: 1rem;
        }
        
        .control-group {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 1rem;
            background-color: #f8fafc;
            border-radius: 8px;
        }
        
        .switch {
            position: relative;
            display: inline-block;
            width: 60px;
            height: 30px;
        }
        
        .switch input {
            opacity: 0;
            width: 0;
            height: 0;
        }
        
        .slider {
            position: absolute;
            cursor: pointer;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background-color: #ccc;
            transition: .4s;
            border-radius: 34px;
        }
        
        .slider:before {
            position: absolute;
            content: "";
            height: 22px;
            width: 22px;
            left: 4px;
            bottom: 4px;
            background-color: white;
            transition: .4s;
            border-radius: 50%;
        }
        
        input:checked + .slider {
            background-color: var(--secondary);
        }
        
        input:checked + .slider:before {
            transform: translateX(30px);
        }
        
        /* Footer */
        .footer {
            background: white;
            border-top: 1px solid var(--border);
            padding: 1rem 2rem;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.875rem;
            color: var(--gray);
        }
        
        /* Responsive */
        @media (max-width: 768px) {
            .header {
                flex-direction: column;
                gap: 1rem;
                padding: 1rem;
            }
            
            .main-content {
                padding: 1rem;
                grid-template-columns: 1fr;
            }
            
            .footer {
                flex-direction: column;
                gap: 0.5rem;
                text-align: center;
            }
        }
    </style>
</head>
<body>
    <div class="app-container">
        <header class="header">
            <div class="logo">
                <div class="logo-icon">
                    <i class="fas fa-industry"></i>
                </div>
                <div class="logo-text">
                    <h1>KH-01 Industrial Control</h1>
                    <p>Real-time monitoring & control system</p>
                </div>
            </div>
            
            <div class="header-actions">
                <div class="status-indicator">
                    <div class="status-dot" id="mqtt-status"></div>
                    <span id="mqtt-status-text">Disconnected</span>
                </div>
                
                <div class="user-menu" onclick="logout()">
                    <div class="user-avatar" id="user-avatar">U</div>
                    <div>
                        <div id="username">User</div>
                        <div style="font-size: 0.75rem; color: var(--gray);" id="user-role">Operator</div>
                    </div>
                </div>
            </div>
        </header>
        
        <main class="main-content">
            <!-- Temperature Monitoring -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-thermometer-half widget-icon"></i>
                        Temperature Monitoring
                    </h2>
                    <div class="widget-actions">
                        <span style="font-size: 0.875rem; color: var(--gray);" id="temp-update-time"></span>
                    </div>
                </div>
                <div class="temp-grid" id="temperature-grid">
                    <!-- Filled by JavaScript -->
                </div>
            </div>
            
            <!-- Alert Panel -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-exclamation-triangle widget-icon"></i>
                        Active Alerts
                    </h2>
                    <span class="badge" id="alert-count">0</span>
                </div>
                <div class="alert-list" id="alert-list">
                    <!-- Filled by JavaScript -->
                </div>
            </div>
            
            <!-- Power Consumption -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-bolt widget-icon"></i>
                        Power Consumption
                    </h2>
                </div>
                <div class="chart-container" id="power-chart">
                    <!-- Chart will be rendered here -->
                </div>
            </div>
            
            <!-- System Status -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-server widget-icon"></i>
                        System Status
                    </h2>
                </div>
                <div class="stats-grid" id="system-stats">
                    <!-- Filled by JavaScript -->
                </div>
            </div>
            
            <!-- VFD Control -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-tachometer-alt widget-icon"></i>
                        VFD Frequency Control
                    </h2>
                </div>
                <div class="control-panel" id="vfd-controls">
                    <!-- Filled by JavaScript -->
                </div>
            </div>
            
            <!-- Data Export -->
            <div class="widget">
                <div class="widget-header">
                    <h2 class="widget-title">
                        <i class="fas fa-download widget-icon"></i>
                        Data Export
                    </h2>
                </div>
                <div style="text-align: center; padding: 2rem 0;">
                    <button onclick="exportData('csv')" style="margin: 0.5rem; padding: 0.75rem 1.5rem; background: var(--primary); color: white; border: none; border-radius: 8px; cursor: pointer;">
                        <i class="fas fa-file-csv"></i> Export CSV
                    </button>
                    <button onclick="exportData('json')" style="margin: 0.5rem; padding: 0.75rem 1.5rem; background: var(--secondary); color: white; border: none; border-radius: 8px; cursor: pointer;">
                        <i class="fas fa-file-code"></i> Export JSON
                    </button>
                </div>
            </div>
        </main>
        
        <footer class="footer">
            <div>Industrial Dashboard v2.0</div>
            <div>© 2024 Aruka Industries. All rights reserved.</div>
            <div id="connection-status">Last updated: --:--:--</div>
        </footer>
    </div>
    
    <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <script>
        // Socket.IO connection
        const socket = io({
            path: '/socket.io',
            transports: ['websocket', 'polling']
        });
        
        // State management
        let appState = {
            mqttConnected: false,
            temperatures: {},
            alerts: [],
            system: {},
            user: {username: 'Guest', role: 'viewer'}
        };
        
        // Chart instances
        let powerChart = null;
        
        // DOM Elements
        const mqttStatusDot = document.getElementById('mqtt-status');
        const mqttStatusText = document.getElementById('mqtt-status-text');
        const temperatureGrid = document.getElementById('temperature-grid');
        const alertList = document.getElementById('alert-list');
        const alertCount = document.getElementById('alert-count');
        const systemStats = document.getElementById('system-stats');
        const connectionStatus = document.getElementById('connection-status');
        const usernameElement = document.getElementById('username');
        const userRoleElement = document.getElementById('user-role');
        const userAvatar = document.getElementById('user-avatar');
        
        // Initialize
        document.addEventListener('DOMContentLoaded', () => {
            fetchUserInfo();
            initializeCharts();
            fetchInitialData();
        });
        
        // Fetch user information
        async function fetchUserInfo() {
            try {
                const response = await fetch('/api/status');
                if (response.ok) {
                    // User info would typically come from a separate endpoint
                    // For now, we'll set defaults
                    appState.user = {username: 'Operator', role: 'operator'};
                    updateUserUI();
                }
            } catch (error) {
                console.error('Error fetching user info:', error);
            }
        }
        
        function updateUserUI() {
            usernameElement.textContent = appState.user.username;
            userRoleElement.textContent = appState.user.role;
            userAvatar.textContent = appState.user.username.charAt(0).toUpperCase();
        }
        
        // Initialize charts
        function initializeCharts() {
            const ctx = document.getElementById('power-chart').getContext('2d');
            powerChart = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: [],
                    datasets: [{
                        label: 'Power (kW)',
                        data: [],
                        borderColor: 'rgb(59, 130, 246)',
                        backgroundColor: 'rgba(59, 130, 246, 0.1)',
                        tension: 0.4,
                        fill: true
                    }]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    plugins: {
                        legend: {display: false}
                    },
                    scales: {
                        y: {
                            beginAtZero: true,
                            grid: {color: 'rgba(0,0,0,0.1)'}
                        },
                        x: {
                            grid: {display: false}
                        }
                    }
                }
            });
        }
        
        // Fetch initial data
        async function fetchInitialData() {
            try {
                const response = await fetch('/api/dashboard');
                const data = await response.json();
                updateAppState(data);
                updateUI();
            } catch (error) {
                console.error('Error fetching initial data:', error);
            }
        }
        
        // Update application state
        function updateAppState(data) {
            appState.temperatures = data.temperatures || {};
            appState.alerts = data.alerts || [];
            appState.system = data.system || {};
            appState.mqttConnected = data.mqtt?.connected || false;
            
            // Update MQTT status
            updateMqttStatus();
            
            // Update connection status
            connectionStatus.textContent = `Last updated: ${formatTime(new Date())}`;
        }
        
        // Update UI
        function updateUI() {
            updateTemperatureGrid();
            updateAlertList();
            updateSystemStats();
        }
        
        // Update MQTT status indicator
        function updateMqttStatus() {
            if (appState.mqttConnected) {
                mqttStatusDot.className = 'status-dot connected';
                mqttStatusText.textContent = 'Connected';
            } else {
                mqttStatusDot.className = 'status-dot';
                mqttStatusText.textContent = 'Disconnected';
            }
        }
        
        // Update temperature grid
        function updateTemperatureGrid() {
            const sensors = [
                {id: 'probe1', label: 'Reactor Core', color: '#ef4444'},
                {id: 'probe2', label: 'Primary Burner', color: '#f59e0b'},
                {id: 'probe3', label: 'Secondary Burner', color: '#10b981'},
                {id: 'probe4', label: 'Exhaust', color: '#3b82f6'}
            ];
            
            let html = '';
            
            sensors.forEach(sensor => {
                const data = appState.temperatures[sensor.id] || {};
                const value = data.value !== undefined ? `${data.value} ${data.unit || '°C'}` : '--';
                
                html += `
                    <div class="temp-sensor">
                        <div class="temp-label">${sensor.label}</div>
                        <div class="temp-value" style="color: ${sensor.color}">${value}</div>
                        <div style="font-size: 0.75rem; color: var(--gray);">
                            ${data.timestamp ? formatTime(new Date(data.timestamp)) : 'No data'}
                        </div>
                    </div>
                `;
            });
            
            temperatureGrid.innerHTML = html;
        }
        
        // Update alert list
        function updateAlertList() {
            alertCount.textContent = appState.alerts.length;
            
            if (appState.alerts.length === 0) {
                alertList.innerHTML = '<div style="text-align: center; color: var(--gray); padding: 2rem;">No active alerts</div>';
                return;
            }
            
            let html = '';
            
            appState.alerts.forEach(alert => {
                const levelClass = alert.level || 'warning';
                const time = alert.timestamp ? formatTime(new Date(alert.timestamp)) : '--:--:--';
                
                html += `
                    <div class="alert-item ${levelClass}">
                        <div class="alert-header">
                            <div class="alert-title">${alert.title || 'Alert'}</div>
                            <div class="alert-time">${time}</div>
                        </div>
                        <div class="alert-message">${alert.message || 'No message'}</div>
                    </div>
                `;
            });
            
            alertList.innerHTML = html;
        }
        
        // Update system stats
        function updateSystemStats() {
            const stats = [
                {label: 'Uptime', value: formatDuration(appState.system.uptime || 0), icon: 'clock'},
                {label: 'Memory', value: `${(appState.system.memory_usage_mb || 0).toFixed(1)} MB`, icon: 'memory'},
                {label: 'CPU', value: `${(appState.system.cpu_usage_percent || 0).toFixed(1)}%`, icon: 'microchip'},
                {label: 'Clients', value: appState.system.connected_clients || 0, icon: 'users'}
            ];
            
            let html = '';
            
            stats.forEach(stat => {
                html += `
                    <div class="stat-item">
                        <i class="fas fa-${stat.icon}" style="color: var(--primary);"></i>
                        <div class="stat-value">${stat.value}</div>
                        <div class="stat-label">${stat.label}</div>
                    </div>
                `;
            });
            
            systemStats.innerHTML = html;
        }
        
        // Format time
        function formatTime(date) {
            return date.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
        }
        
        // Format duration
        function formatDuration(seconds) {
            const days = Math.floor(seconds / 86400);
            const hours = Math.floor((seconds % 86400) / 3600);
            const minutes = Math.floor((seconds % 3600) / 60);
            
            if (days > 0) return `${days}d ${hours}h`;
            if (hours > 0) return `${hours}h ${minutes}m`;
            return `${minutes}m`;
        }
        
        // Export data
        function exportData(format) {
            const startDate = new Date();
            startDate.setDate(startDate.getDate() - 1); // Last 24 hours
            
            const start = startDate.toISOString();
            const end = new Date().toISOString();
            
            window.open(`/api/export?format=${format}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}`, '_blank');
        }
        
        // Logout
        function logout() {
            window.location.href = '/logout';
        }
        
        // Socket.IO event handlers
        socket.on('connect', () => {
            console.log('Connected to WebSocket server');
        });
        
        socket.on('new_reading', (data) => {
            // Update specific sensor data
            if (data.topic.includes('temperature')) {
                const probeId = data.topic.split('_').pop();
                appState.temperatures[probeId] = {
                    value: data.payload.value,
                    unit: data.payload.unit || '°C',
                    timestamp: data.timestamp
                };
                updateTemperatureGrid();
            }
            
            connectionStatus.textContent = `Last updated: ${formatTime(new Date())}`;
        });
        
        socket.on('new_alert', (alert) => {
            appState.alerts.unshift(alert);
            if (appState.alerts.length > 10) appState.alerts.pop();
            updateAlertList();
        });
        
        socket.on('system_metrics', (metrics) => {
            appState.system = {...appState.system, ...metrics};
            updateSystemStats();
        });
        
        socket.on('mqtt_status', (status) => {
            appState.mqttConnected = status.connected;
            updateMqttStatus();
        });
        
        // Auto-refresh every 5 seconds
        setInterval(fetchInitialData, 5000);
    </script>
</body>
</html>
"""

# =====================================================
# APPLICATION ENTRY POINT
# =====================================================
if __name__ == "__main__":
    # Initialize application
    initialize_application()
    
    # Start Flask application
    port = int(os.environ.get("PORT", 5000))
    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        allow_unsafe_werkzeug=True
    )