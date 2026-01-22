#!/usr/bin/env python3
"""
Aruka / KH-01 MQTT Dashboard (Render-ready, single file, Python 3.13)

Render Start Command:
  gunicorn -w 1 app:app --bind 0.0.0.0:$PORT --log-level info

Render Environment Variables (IMPORTANT):
  MQTT_HOST=t569f61e.ala.asia-southeast1.emqxsl.com
  MQTT_PORT=8883
  MQTT_USER=KH-01-device
  MQTT_PASS=Radiation0-Disperser8-Sternum1-Trio4
  MQTT_TLS=1
  MQTT_TLS_INSECURE=1   (optional; set 1 if you want insecure like ESP32)
  MQTT_TOPIC=KH/site-01/KH-01/#
  MQTT_CLIENT_ID=Aruka_KH_render (optional)
  SECRET_KEY=anystring (optional)
"""

import os
import ssl
import json
import time
import logging
import threading
import certifi
from datetime import datetime, timezone

from flask import Flask, jsonify, request, render_template_string
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ARUKA_RENDER")


# =====================================================
# ENV CONFIG
# =====================================================
def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "").strip()
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "KH/site-01/KH-01/#").strip()

MQTT_TLS = env_bool("MQTT_TLS", "1")
MQTT_TLS_INSECURE = env_bool("MQTT_TLS_INSECURE", "0")  # set 1 to behave like ESP32 insecure
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "Aruka_KH_render").strip()

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
    cors_allowed_origins="*",
    async_mode="threading",     # Python 3.13 safe
    path="socket.io",           # must match client
    ping_interval=25,
    ping_timeout=60,
)


# =====================================================
# SHARED STATE
# =====================================================
state_lock = threading.Lock()

MQTT_STATUS = {
    "connected": False,
    "subscribed_topic": MQTT_TOPIC,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
    "host": MQTT_HOST,
    "port": MQTT_PORT,
    "tls": MQTT_TLS,
    "tls_insecure": MQTT_TLS_INSECURE,
}

LAST_BY_TOPIC = {}  # {topic: {"topic":..., "payload":..., "received_at":...}}
LAST_MESSAGE = {
    "ok": False,
    "topic": None,
    "payload": None,
    "received_at": None,
    "note": "No data yet",
}

mqtt_client = None


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return s


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc, properties=None):
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
            LOG.exception("Subscribe failed")
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
        parsed = safe_json_loads(raw)

        payload = {
            "ok": True,
            "topic": msg.topic,
            "payload": parsed,
            "received_at": utc_iso(),
        }

        with state_lock:
            LAST_MESSAGE.clear()
            LAST_MESSAGE.update(payload)
            LAST_BY_TOPIC[msg.topic] = payload
            # keep memory small
            if len(LAST_BY_TOPIC) > 200:
                # remove oldest by received_at (simple)
                oldest = sorted(LAST_BY_TOPIC.items(), key=lambda kv: kv[1].get("received_at", ""))[:50]
                for k, _ in oldest:
                    LAST_BY_TOPIC.pop(k, None)

        socketio.emit("mqtt_message", payload)

    except Exception as e:
        LOG.exception("on_message error")
        with state_lock:
            MQTT_STATUS["last_error"] = f"on_message error: {e}"


# =====================================================
# MQTT CLIENT + WORKER
# =====================================================
def build_mqtt_client() -> mqtt.Client:
    c = mqtt.Client(client_id=MQTT_CLIENT_ID, protocol=mqtt.MQTTv311)

    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)

    if MQTT_TLS:
        c.tls_set(
            ca_certs=certifi.where(),
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        c.tls_insecure_set(MQTT_TLS_INSECURE)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message

    c.reconnect_delay_set(min_delay=MQTT_RECONNECT_MIN, max_delay=MQTT_RECONNECT_MAX)
    return c


def mqtt_worker():
    global mqtt_client

    if not MQTT_HOST:
        LOG.error("MQTT_HOST is empty. Set MQTT_HOST in Render Environment Variables.")
        with state_lock:
            MQTT_STATUS["last_error"] = "MQTT_HOST not set"
        return

    while True:
        try:
            with state_lock:
                MQTT_STATUS["last_error"] = None

            mqtt_client = build_mqtt_client()

            LOG.info(f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} TLS={MQTT_TLS} insecure={MQTT_TLS_INSECURE}")
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE_SEC)
            mqtt_client.loop_start()

            while True:
                time.sleep(5)

        except Exception as e:
            LOG.error(f"MQTT worker error: {e}")
            with state_lock:
                MQTT_STATUS["connected"] = False
                MQTT_STATUS["last_error"] = str(e)

            try:
                if mqtt_client is not None:
                    mqtt_client.loop_stop()
                    mqtt_client.disconnect()
            except Exception:
                pass

            time.sleep(5)


# start worker only once
_started = False
_started_lock = threading.Lock()


@app.before_request
def start_worker_once():
    global _started
    if _started:
        return
    with _started_lock:
        if _started:
            return
        _started = True
        socketio.start_background_task(mqtt_worker)
        LOG.info("Started MQTT background worker")


# =====================================================
# UI
# =====================================================
INDEX_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Aruka KH-01 Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 18px; }
    .row { display:flex; gap:12px; flex-wrap:wrap; }
    .card { border:1px solid #ddd; border-radius:10px; padding:12px; min-width:320px; }
    .title { font-weight:700; margin-bottom:8px; }
    pre { background:#f7f7f7; padding:10px; border-radius:8px; overflow:auto; max-height:360px; }
    .ok { color: #0a7; font-weight:700; }
    .bad { color: #c22; font-weight:700; }
    .small { color:#666; font-size: 13px; }
    button { padding:8px 12px; cursor:pointer; }
  </style>
</head>
<body>
  <h2>Aruka KH-01 MQTT Dashboard</h2>

  <div class="row">
    <div class="card">
      <div class="title">MQTT Connection</div>
      <div>Status: <span id="conn" class="bad">Unknown</span></div>
      <div class="small" id="connDetails"></div>
      <div style="margin-top:10px;">
        <button onclick="refreshStatus()">Refresh</button>
      </div>
    </div>

    <div class="card">
      <div class="title">Last MQTT Message</div>
      <div class="small" id="lastMeta">No data</div>
      <pre id="lastPayload">{}</pre>
    </div>
  </div>

  <div class="card" style="margin-top:12px;">
    <div class="title">Live Stream</div>
    <div class="small">Socket.IO event: <b>mqtt_message</b></div>
    <pre id="stream"></pre>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script>
    const streamEl = document.getElementById("stream");
    const lastPayloadEl = document.getElementById("lastPayload");
    const lastMetaEl = document.getElementById("lastMeta");
    const connEl = document.getElementById("conn");
    const connDetailsEl = document.getElementById("connDetails");

    // IMPORTANT: match server path
    const socket = io({ path: "/socket.io" });

    socket.on("connect", () => addLine("socket connected: " + socket.id));
    socket.on("disconnect", () => addLine("socket disconnected"));

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
        `host=${data.mqtt.host || "?"}:${data.mqtt.port} | tls=${data.mqtt.tls} insecure=${data.mqtt.tls_insecure} | topic=${data.mqtt.subscribed_topic} | last_error=${data.mqtt.last_error || "none"}`;
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
            "last_message": LAST_MESSAGE,
            "topics_count": len(LAST_BY_TOPIC),
        })


@app.get("/topics")
def topics():
    """Return last value for each topic (useful for debugging)."""
    with state_lock:
        # return a copy (avoid race)
        data = dict(LAST_BY_TOPIC)
    return jsonify({"ok": True, "topics": data})


@app.post("/publish")
def publish():
    """
    Publish from HTTP for testing.
    JSON: {"topic":"KH/site-01/KH-01/status", "payload": {... or "text"}}
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
# LOCAL RUN (Render uses gunicorn)
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    socketio.run(app, host="0.0.0.0", port=port)
MQTT_PASS