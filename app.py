#!/usr/bin/env python3
"""
Aruka / KH-01 MQTT Dashboard (Render-ready, single file, Python 3.13)

RENDER START COMMAND:
  gunicorn -w 1 app:app --bind 0.0.0.0:$PORT --log-level info

RENDER ENVIRONMENT VARIABLES (set these in Render Dashboard -> Environment):
  MQTT_HOST=t569f61e.ala.asia-southeast1.emqxsl.com
  MQTT_PORT=8883
  MQTT_USER=KH-01-device
  MQTT_PASS=Radiation0-Disperser8-Sternum1-Trio4
  MQTT_TLS=1
  MQTT_TLS_INSECURE=1      # optional (1 = insecure like ESP32), else 0
  MQTT_TOPIC=KH/site-01/KH-01/#

OPTIONAL:
  MQTT_CLIENT_ID=Aruka_KH_render
  SECRET_KEY=anystring
  UI_TITLE=Aruka KH-01 MQTT Dashboard
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
# HELPERS
# =====================================================
def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return s


def topic_tail(topic: str) -> str:
    # For nicer UI label: show last part
    if not topic:
        return ""
    return topic.split("/")[-1]


# =====================================================
# ENV CONFIG
# =====================================================
MQTT_HOST = os.environ.get("MQTT_HOST", "t569f61e.ala.asia-southeast1.emqxsl.com").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "KH-01-device").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "Radiation0-Disperser8-Sternum1-Trio4").strip()
MQTT_TOPIC = os.environ.get("MQTT_TOPIC", "KH/site-01/KH-01/#").strip()

MQTT_TLS = env_bool("MQTT_TLS", "1")
MQTT_TLS_INSECURE = env_bool("MQTT_TLS_INSECURE", "0")  # 1 behaves like ESP32 insecure
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "Aruka_KH_render").strip()

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me").strip()
UI_TITLE = os.environ.get("UI_TITLE", "Aruka KH-01 MQTT Dashboard").strip()

MQTT_KEEPALIVE_SEC = int(os.environ.get("MQTT_KEEPALIVE_SEC", "60"))
MQTT_RECONNECT_MIN = int(os.environ.get("MQTT_RECONNECT_MIN", "1"))
MQTT_RECONNECT_MAX = int(os.environ.get("MQTT_RECONNECT_MAX", "30"))


# =====================================================
# FLASK / SOCKETIO
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

# Python 3.13 safe. This will use polling (not websocket) under gunicorn sync workers.
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
    path="socket.io",
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

# Last message overall
LAST_MESSAGE = {
    "ok": False,
    "topic": None,
    "payload": None,
    "received_at": None,
    "note": "No data yet",
}

# Last message per topic
LAST_BY_TOPIC = {}  # {topic: payload_dict}

mqtt_client = None


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
            client.subscribe(MQTT_TOPIC, qos=0)
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
            "qos": int(getattr(msg, "qos", 0)),
            "retain": bool(getattr(msg, "retain", False)),
            "payload": parsed,
            "received_at": utc_iso(),
        }

        with state_lock:
            LAST_MESSAGE.clear()
            LAST_MESSAGE.update(payload)
            LAST_BY_TOPIC[msg.topic] = payload

            # keep memory bounded
            if len(LAST_BY_TOPIC) > 500:
                # remove ~100 oldest by received_at
                oldest = sorted(
                    LAST_BY_TOPIC.items(),
                    key=lambda kv: kv[1].get("received_at", "")
                )[:100]
                for k, _ in oldest:
                    LAST_BY_TOPIC.pop(k, None)

        # push to UI
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

            LOG.info(
                f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} "
                f"TLS={MQTT_TLS} insecure={MQTT_TLS_INSECURE} topic={MQTT_TOPIC}"
            )
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
  <title>{{title}}</title>
  <style>
    :root{
      --bg:#0b0f17;
      --panel:#111827;
      --panel2:#0f172a;
      --text:#e5e7eb;
      --muted:#9ca3af;
      --border:#1f2937;
      --accent:#a3e635; /* green like your screenshot */
      --bad:#ef4444;
      --ok:#22c55e;
      --shadow: 0 12px 40px rgba(0,0,0,.35);
      --radius: 18px;
    }
    *{ box-sizing:border-box; }
    body{
      margin:0;
      background: radial-gradient(1200px 700px at 10% 10%, rgba(163,230,53,.10), transparent 50%),
                  radial-gradient(900px 600px at 90% 30%, rgba(59,130,246,.10), transparent 45%),
                  var(--bg);
      color:var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
    }
    .wrap{ max-width:1200px; margin:28px auto; padding:0 16px; }
    h1{ margin:0 0 14px 0; font-size:28px; letter-spacing:.2px; }
    .toprow{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:16px;
      margin-bottom:16px;
    }
    @media (max-width: 900px){
      .toprow{ grid-template-columns:1fr; }
    }
    .card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      border:1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding:16px;
    }
    .title{ font-weight:800; margin-bottom:10px; font-size:16px; }
    .row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:10px 14px;
      border-radius: 999px;
      background: rgba(163,230,53,.12);
      color: var(--accent);
      font-weight:700;
      border:1px solid rgba(163,230,53,.25);
    }
    .pill.gray{
      background: rgba(148,163,184,.10);
      border:1px solid rgba(148,163,184,.20);
      color: #cbd5e1;
      font-weight:600;
    }
    .status{
      font-weight:900;
      letter-spacing:.3px;
    }
    .status.ok{ color: var(--ok); }
    .status.bad{ color: var(--bad); }
    .muted{ color: var(--muted); font-size:13px; line-height:1.4; }
    button{
      background: rgba(255,255,255,.06);
      border:1px solid var(--border);
      color: var(--text);
      padding:10px 14px;
      border-radius: 12px;
      cursor:pointer;
      font-weight:700;
    }
    button:hover{ background: rgba(255,255,255,.10); }
    .grid{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:16px;
      margin-top:16px;
    }
    @media (max-width: 900px){
      .grid{ grid-template-columns:1fr; }
    }
    .topic-card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      border:1px solid var(--border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      padding:18px;
      position:relative;
      overflow:hidden;
      min-height: 140px;
    }
    .topic-card:before{
      content:"";
      position:absolute;
      inset:-80px;
      background: radial-gradient(260px 180px at 20% 30%, rgba(163,230,53,.10), transparent 70%),
                  radial-gradient(260px 180px at 80% 40%, rgba(59,130,246,.10), transparent 70%);
      pointer-events:none;
    }
    .topic-card > *{ position:relative; }
    pre{
      margin:0;
      padding:14px;
      border-radius: 16px;
      background: rgba(0,0,0,.22);
      border:1px solid rgba(255,255,255,.06);
      color: #e5e7eb;
      overflow:auto;
      max-height: 180px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .cardhead{
      display:flex;
      justify-content:space-between;
      align-items:center;
      gap:12px;
      margin-top:14px;
      flex-wrap:wrap;
    }
    .topicname{ font-weight:900; font-size:18px; }
    .badge{
      display:inline-flex;
      align-items:center;
      justify-content:center;
      padding:10px 16px;
      border-radius:999px;
      background: rgba(163,230,53,.90);
      color:#0b0f17;
      font-weight:900;
      letter-spacing:.2px;
      white-space:nowrap;
    }
    .badge.qos{
      background: rgba(163,230,53,.90);
      min-width: 82px;
    }
    .toolbar{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      align-items:center;
      margin-top:10px;
    }
    .input{
      flex:1;
      min-width: 240px;
      background: rgba(255,255,255,.06);
      border:1px solid var(--border);
      border-radius: 12px;
      padding:10px 12px;
      color: var(--text);
      outline:none;
    }
    .input::placeholder{ color: rgba(229,231,235,.55); }
    .split{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      flex-wrap:wrap;
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{{title}}</h1>

    <div class="toprow">
      <div class="card">
        <div class="title">MQTT Connection</div>
        <div class="split">
          <div class="row">
            <div>Status: <span id="conn" class="status bad">DISCONNECTED</span></div>
          </div>
          <button onclick="refreshAll()">Refresh</button>
        </div>
        <div class="muted" id="connDetails" style="margin-top:10px;"></div>

        <div class="toolbar">
          <input id="filter" class="input" placeholder="Filter topics (example: temperature, status, vfd)..." oninput="renderCards()"/>
          <button onclick="toggleSort()">Sort: <span id="sortMode">Latest</span></button>
        </div>
      </div>

      <div class="card">
        <div class="title">Last MQTT Message</div>
        <div class="muted" id="lastMeta">No data</div>
        <pre id="lastPayload">{}</pre>
      </div>
    </div>

    <div class="card">
      <div class="title">Topics (Last value per topic)</div>
      <div class="muted" id="topicsInfo">Loading...</div>
      <div class="grid" id="cards"></div>
    </div>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script>
    const connEl = document.getElementById("conn");
    const connDetailsEl = document.getElementById("connDetails");
    const lastMetaEl = document.getElementById("lastMeta");
    const lastPayloadEl = document.getElementById("lastPayload");
    const cardsEl = document.getElementById("cards");
    const topicsInfoEl = document.getElementById("topicsInfo");
    const filterEl = document.getElementById("filter");
    const sortModeEl = document.getElementById("sortMode");

    let topicsMap = {};     // topic -> payload object
    let sortMode = "latest"; // "latest" or "name"

    // IMPORTANT: Force polling to avoid websocket session errors on threading mode
    const socket = io({
      path: "/socket.io",
      transports: ["polling"]
    });

    socket.on("connect", () => {
      // ok
    });

    socket.on("mqtt_message", (msg) => {
      // Update last overall
      lastMetaEl.textContent = `${msg.received_at} | ${msg.topic}`;
      lastPayloadEl.textContent = formatPayload(msg.payload);

      // Update the per-topic map
      topicsMap[msg.topic] = msg;
      renderCards();
    });

    function formatPayload(p) {
      try {
        if (typeof p === "string") return p;
        return JSON.stringify(p, null, 2);
      } catch(e) {
        return String(p);
      }
    }

    async function refreshStatus() {
      const r = await fetch("/status");
      const data = await r.json();

      if (data.mqtt.connected) {
        connEl.textContent = "CONNECTED";
        connEl.className = "status ok";
      } else {
        connEl.textContent = "DISCONNECTED";
        connEl.className = "status bad";
      }

      connDetailsEl.textContent =
        `host=${data.mqtt.host || "?"}:${data.mqtt.port} | tls=${data.mqtt.tls} insecure=${data.mqtt.tls_insecure} | topic=${data.mqtt.subscribed_topic} | last_error=${data.mqtt.last_error || "none"}`;
    }

    async function refreshTopics() {
      const r = await fetch("/topics");
      const data = await r.json();
      if (data.ok && data.topics) {
        topicsMap = data.topics;
        renderCards();
      }
    }

    async function refreshAll() {
      await refreshStatus();
      await refreshTopics();
    }

    function toggleSort(){
      sortMode = (sortMode === "latest") ? "name" : "latest";
      sortModeEl.textContent = (sortMode === "latest") ? "Latest" : "Name";
      renderCards();
    }

    function renderCards() {
      const filter = (filterEl.value || "").toLowerCase().trim();

      // topicsMap may be {topic: msg} OR {topic: payload} depending on endpoint
      let items = Object.entries(topicsMap).map(([topic, obj]) => {
        // if endpoint returns msg object already
        const msg = obj && obj.topic ? obj : { topic, payload: obj.payload ?? obj, received_at: obj.received_at ?? null, qos: obj.qos ?? 0 };
        return msg;
      });

      if (filter) {
        items = items.filter(m =>
          (m.topic || "").toLowerCase().includes(filter) ||
          JSON.stringify(m.payload || "").toLowerCase().includes(filter)
        );
      }

      if (sortMode === "name") {
        items.sort((a,b) => (a.topic||"").localeCompare(b.topic||""));
      } else {
        // latest first
        items.sort((a,b) => (b.received_at||"").localeCompare(a.received_at||""));
      }

      topicsInfoEl.textContent = `Showing ${items.length} topic(s).`;

      cardsEl.innerHTML = items.map(m => {
        const payloadText = formatPayload(m.payload);
        const qos = (m.qos === undefined || m.qos === null) ? 0 : m.qos;

        return `
          <div class="topic-card">
            <pre>${escapeHtml(payloadText)}</pre>
            <div class="cardhead">
              <div class="badge">${escapeHtml(m.topic || "")}</div>
              <div class="badge qos">QoS ${qos}</div>
            </div>
            <div class="muted" style="margin-top:10px;">
              ${escapeHtml(m.received_at || "")}
            </div>
          </div>
        `;
      }).join("");
    }

    function escapeHtml(s){
      return String(s)
        .replaceAll("&","&amp;")
        .replaceAll("<","&lt;")
        .replaceAll(">","&gt;")
        .replaceAll('"',"&quot;")
        .replaceAll("'","&#039;");
    }

    // Initial load + refresh loops
    refreshAll();
    setInterval(refreshStatus, 5000);
    setInterval(refreshTopics, 6000);
  </script>
</body>
</html>
"""


# =====================================================
# ROUTES
# =====================================================
@app.get("/")
def index():
    return render_template_string(INDEX_HTML, title=UI_TITLE)


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
    """Return last value for each topic."""
    with state_lock:
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

        mqtt_client.publish(topic, payload_out, qos=0, retain=False)
        return jsonify({"ok": True, "topic": topic})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# =====================================================
# LOCAL RUN (Render uses gunicorn)
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "10000"))
    socketio.run(app, host="0.0.0.0", port=port)
