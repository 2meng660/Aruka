#!/usr/bin/env python3
"""
Render-ready Flask + SocketIO + MQTT (Single File)

Run on Render with:
  gunicorn -k eventlet -w 1 app:app --bind 0.0.0.0:$PORT --log-level info

Features:
- eventlet async (WebSocket compatible)
- one MQTT background loop (no duplicate loops)
- stable reconnect logic
- TLS with certifi (works on Render Linux)
- simple HTML UI embedded (no templates folder needed)
"""

# IMPORTANT: eventlet monkey_patch must be first
import eventlet
eventlet.monkey_patch()

import os
import ssl
import json
import time
import logging
import certifi
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt
from eventlet.semaphore import Semaphore


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
LOG = logging.getLogger("RENDER_MQTT_APP")


# =====================================================
# CONFIG (ENVIRONMENT VARIABLES)
# =====================================================
# Required (for real use):
#   MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PASS, MQTT_TOPIC
# Optional:
#   MQTT_TLS (1/0), MQTT_CLIENT_ID, SECRET_KEY, SOCKETIO_CORS
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "").strip()
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "KH/site-01/#").strip()

MQTT_TLS = os.environ.get("MQTT_TLS", "1").strip() == "1"
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "KH-01-render").strip()

SOCKETIO_CORS = os.environ.get("SOCKETIO_CORS", "*").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me").strip()

MQTT_KEEPALIVE_SEC = int(os.environ.get("MQTT_KEEPALIVE_SEC", "60"))
MQTT_RECONNECT_MIN = int(os.environ.get("MQTT_RECONNECT_MIN", "1"))
MQTT_RECONNECT_MAX = int(os.environ.get("MQTT_RECONNECT_MAX", "30"))


# =====================================================
# FLASK / SOCKETIO
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

socketio = SocketIO(
    app,
    cors_allowed_origins=SOCKETIO_CORS,
    async_mode="eventlet",
    ping_interval=25,
    ping_timeout=60
)


# =====================================================
# SHARED STATE
# =====================================================
state_lock = Semaphore(1)

LAST_MESSAGE = {
    "ok": False,
    "topic": None,
    "payload": None,
    "received_at": None,
    "note": "No data yet"
}

MQTT_STATUS = {
    "connected": False,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
    "subscribed_topic": MQTT_TOPIC
}

mqtt_client = None


def utc_iso():
    return datetime.now(timezone.utc).isoformat()


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc, properties=None):
    # rc == 0 means success
    LOG.info(f"MQTT on_connect rc={rc}")

    with state_lock:
        MQTT_STATUS["connected"] = (rc == 0)
        MQTT_STATUS["last_connect_at"] = utc_iso()
        MQTT_STATUS["last_error"] = None if rc == 0 else f"connect rc={rc}"

    if rc == 0:
        try:
            client.subscribe(MQTT_TOPIC)
            LOG.info(f"MQTT subscribed: {MQTT_TOPIC}")
        except Exception as e:
            LOG.exception(f"Subscribe failed: {e}")
            with state_lock:
                MQTT_STATUS["last_error"] = f"subscribe failed: {e}"


def on_disconnect(client, userdata, rc, properties=None):
    LOG.warning(f"MQTT on_disconnect rc={rc}")
    with state_lock:
        MQTT_STATUS["connected"] = False
        MQTT_STATUS["last_disconnect_at"] = utc_iso()


def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", errors="replace")

        # Try JSON decode; if fails, keep as string
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = raw

        payload = {
            "ok": True,
            "topic": msg.topic,
            "payload": parsed,
            "received_at": utc_iso()
        }

        with state_lock:
            LAST_MESSAGE.clear()
            LAST_MESSAGE.update(payload)

        # Push to browser via websocket
        socketio.emit("mqtt_message", payload)

    except Exception as e:
        LOG.exception(f"on_message error: {e}")
        with state_lock:
            MQTT_STATUS["last_error"] = f"on_message error: {e}"


# =====================================================
# MQTT BUILD + BACKGROUND LOOP
# =====================================================
def build_mqtt_client() -> mqtt.Client:
    c = mqtt.Client(
        client_id=MQTT_CLIENT_ID,
        protocol=mqtt.MQTTv311
    )

    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)

    if MQTT_TLS:
        # Render-friendly CA bundle (Linux)
        c.tls_set(
            ca_certs=certifi.where(),
            tls_version=ssl.PROTOCOL_TLS_CLIENT
        )
        c.tls_insecure_set(False)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message

    # Better reconnect behavior
    c.reconnect_delay_set(min_delay=MQTT_RECONNECT_MIN, max_delay=MQTT_RECONNECT_MAX)

    return c


def mqtt_worker():
    """
    Runs forever in a background green thread.
    Ensures:
    - connect
    - loop_start
    - reconnect on any exception
    """
    global mqtt_client

    if not MQTT_HOST:
        LOG.error("MQTT_HOST is empty. Set MQTT_HOST in Render environment variables.")
        with state_lock:
            MQTT_STATUS["last_error"] = "MQTT_HOST not set"
        return

    while True:
        try:
            with state_lock:
                MQTT_STATUS["last_error"] = None

            mqtt_client = build_mqtt_client()

            LOG.info(f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} TLS={MQTT_TLS}")
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE_SEC)

            mqtt_client.loop_start()

            # Keep worker alive; loop_start runs network loop in background thread
            # We still watch state periodically.
            while True:
                eventlet.sleep(5)

        except Exception as e:
            LOG.error(f"MQTT worker error: {e}")
            with state_lock:
                MQTT_STATUS["connected"] = False
                MQTT_STATUS["last_error"] = str(e)

            # Clean shutdown if possible
            try:
                if mqtt_client is not None:
                    mqtt_client.loop_stop()
                    mqtt_client.disconnect()
            except Exception:
                pass

            eventlet.sleep(5)


_started = False


@app.before_request
def _start_once():
    """
    Start background task once.
    Using before_request instead of before_first_request (Flask 3 changes).
    """
    global _started
    if not _started:
        _started = True
        socketio.start_background_task(mqtt_worker)
        LOG.info("Started MQTT background worker")


# =====================================================
# SIMPLE UI (embedded)
# =====================================================
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>KH-01 MQTT Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 18px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    .card { border:1px solid #ddd; border-radius:10px; padding:12px; min-width:320px; }
    .title { font-weight:700; margin-bottom:8px; }
    pre { background:#f7f7f7; padding:10px; border-radius:8px; overflow:auto; max-height:360px; }
    .ok { color: #0a7; font-weight:700; }
    .bad { color: #c22; font-weight:700; }
    .small { color:#666; font-size: 13px; }
    input { padding:8px; width: 320px; }
    button { padding:8px 12px; cursor:pointer; }
  </style>
</head>
<body>
  <h2>KH-01 MQTT Dashboard (Render)</h2>

  <div class="row">
    <div class="card">
      <div class="title">Connection</div>
      <div>Status: <span id="conn" class="bad">Unknown</span></div>
      <div class="small" id="connDetails"></div>
      <div style="margin-top:10px;">
        <button onclick="refreshStatus()">Refresh status</button>
      </div>
    </div>

    <div class="card">
      <div class="title">Last MQTT Message</div>
      <div class="small" id="lastMeta">No data</div>
      <pre id="lastPayload">{}</pre>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="title">Live stream</div>
    <div class="small">Listens on Socket.IO event: <b>mqtt_message</b></div>
    <pre id="stream"></pre>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script>
    const streamEl = document.getElementById("stream");
    const lastPayloadEl = document.getElementById("lastPayload");
    const lastMetaEl = document.getElementById("lastMeta");
    const connEl = document.getElementById("conn");
    const connDetailsEl = document.getElementById("connDetails");

    const socket = io();

    socket.on("connect", () => {
      addLine("socket connected: " + socket.id);
    });

    socket.on("disconnect", () => {
      addLine("socket disconnected");
    });

    socket.on("mqtt_message", (msg) => {
      lastMetaEl.textContent = `${msg.received_at} | ${msg.topic}`;
      lastPayloadEl.textContent = JSON.stringify(msg.payload, null, 2);
      addLine(JSON.stringify(msg, null, 2));
    });

    function addLine(s) {
      streamEl.textContent = (s + "\\n\\n" + streamEl.textContent).slice(0, 12000);
    }

    async function refreshStatus() {
      const r = await fetch("/status");
      const data = await r.json();
      if (data.mqtt.connected) {
        connEl.textContent = "CONNECTED";
        connEl.className = "ok";
      } else {
        connEl.textContent = "DISCONNECTED";
        connEl.className = "bad";
      }
      connDetailsEl.textContent =
        `topic=${data.mqtt.subscribed_topic} | last_error=${data.mqtt.last_error || "none"}`;
    }

    refreshStatus();
    setInterval(refreshStatus, 5000);
  </script>
</body>
</html>
"""


# =====================================================
# ROUTES
# =====================================================
@app.get("/")
def index():
    return render_template_string(INDEX_HTML)


@app.get("/health")
def health():
    return jsonify({"ok": True, "time_utc": utc_iso()})


@app.get("/status")
def status():
    with state_lock:
        return jsonify({
            "mqtt": MQTT_STATUS,
            "last_message": LAST_MESSAGE
        })


@app.post("/publish")
def publish():
    """
    Optional: publish from HTTP for testing.
    Body JSON: {"topic":"...", "payload": {... or "text"}}
    """
    global mqtt_client
    data = request.get_json(silent=True) or {}
    topic = (data.get("topic") or "").strip()
    payload = data.get("payload")

    if not topic:
        return jsonify({"ok": False, "error": "topic is required"}), 400

    if mqtt_client is None:
        return jsonify({"ok": False, "error": "mqtt client not ready"}), 503

    try:
        if isinstance(payload, (dict, list)):
            payload_out = json.dumps(payload, ensure_ascii=False)
        else:
            payload_out = "" if payload is None else str(payload)

        mqtt_client.publish(topic, payload_out)
        return jsonify({"ok": True, "topic": topic})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =====================================================
# MAIN
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    socketio.run(app, host="0.0.0.0", port=port)
