#!/usr/bin/env python3
"""
Aruka / KH-01 MQTT Dashboard (Render-ready, single file, Python 3.13 SAFE)

RENDER START COMMAND (IMPORTANT):
  gunicorn -w 1 --threads 8 --worker-class gthread --timeout 120 app:app --bind 0.0.0.0:$PORT --log-level info

RENDER ENV VARS:
  MQTT_HOST=t569f61e.ala.asia-southeast1.emqxsl.com
  MQTT_PORT=8883
  MQTT_USER=KH-01-device
  MQTT_PASS=*** (set in Render)
  MQTT_TLS=1
  MQTT_TLS_INSECURE=1   (optional; 1 like ESP32, else 0)
  MQTT_CLIENT_ID=Aruka_KH_render
  SECRET_KEY=anystring
"""

import os
import ssl
import json
import time
import logging
import threading
import certifi
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from flask import Flask, jsonify, request, render_template_string
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ARUKA_KH01")


# =====================================================
# ENV HELPERS
# =====================================================
def env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)).strip())
    except Exception:
        return default


def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_json(payload_bytes: bytes) -> Any:
    raw = payload_bytes.decode("utf-8", errors="replace")
    try:
        return json.loads(raw)
    except Exception:
        return raw


# =====================================================
# MQTT CONFIG (SUBSCRIBE ONLY YOUR 7 TOPICS)
# =====================================================
MQTT_HOST = env_str("MQTT_HOST", "t569f61e.ala.asia-southeast1.emqxsl.com")
MQTT_PORT = env_int("MQTT_PORT", 8883)
MQTT_USER = env_str("MQTT_USER", "KH-01-device")
MQTT_PASS = env_str("MQTT_PASS", "Radiation0-Disperser8-Sternum1-Trio4")
MQTT_TLS = env_bool("MQTT_TLS", "1")
MQTT_TLS_INSECURE = env_bool("MQTT_TLS_INSECURE", "1")
MQTT_CLIENT_ID = env_str("MQTT_CLIENT_ID", "Aruka_KH_render")
MQTT_KEEPALIVE_SEC = env_int("MQTT_KEEPALIVE_SEC", 60)

SECRET_KEY = env_str("SECRET_KEY", "change-me")

# Your exact topics (NO wildcard)
TOPICS: List[str] = [
    "KH/site-01/KH-01/temperature_probe1",
    "KH/site-01/KH-01/temperature_probe2",
    "KH/site-01/KH-01/temperature_probe3",
    "KH/site-01/KH-01/temperature_probe4",
    "KH/site-01/KH-01/vfd_frequency",
    "KH/site-01/KH-01/power_consumption",
    "KH/site-01/KH-01/status",
]

# Section mapping for UI layout
SECTION_BY_TOPIC = {
    "KH/site-01/KH-01/temperature_probe1": "Temperatures",
    "KH/site-01/KH-01/temperature_probe2": "Temperatures",
    "KH/site-01/KH-01/temperature_probe3": "Temperatures",
    "KH/site-01/KH-01/temperature_probe4": "Temperatures",
    "KH/site-01/KH-01/vfd_frequency": "Power / VFD",
    "KH/site-01/KH-01/power_consumption": "Power / VFD",
    "KH/site-01/KH-01/status": "Status",
}


# =====================================================
# FLASK / SOCKETIO (THREADING - PY 3.13 SAFE)
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

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

MQTT_STATUS: Dict[str, Any] = {
    "connected": False,
    "host": MQTT_HOST,
    "port": MQTT_PORT,
    "tls": MQTT_TLS,
    "tls_insecure": MQTT_TLS_INSECURE,
    "client_id": MQTT_CLIENT_ID,
    "topics": TOPICS,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
    "last_rc": None,
}

LAST_MESSAGE: Dict[str, Any] = {
    "ok": False,
    "topic": None,
    "payload": None,
    "received_at": None,
}

# store last message per topic
LAST_BY_TOPIC: Dict[str, Dict[str, Any]] = {}


mqtt_client: Optional[mqtt.Client] = None
_worker_started = False
_worker_lock = threading.Lock()


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc, properties=None):
    LOG.info(f"MQTT on_connect rc={rc}")
    with state_lock:
        MQTT_STATUS["connected"] = (rc == 0)
        MQTT_STATUS["last_connect_at"] = utc_iso()
        MQTT_STATUS["last_rc"] = rc
        MQTT_STATUS["last_error"] = None if rc == 0 else f"connect rc={rc}"

    if rc == 0:
        ok = 0
        for t in TOPICS:
            try:
                client.subscribe(t, qos=0)
                ok += 1
            except Exception as e:
                LOG.exception("Subscribe failed")
                with state_lock:
                    MQTT_STATUS["last_error"] = f"subscribe failed: {e}"
        LOG.info(f"Subscribed to {ok}/{len(TOPICS)} topics")


def on_disconnect(client, userdata, rc, properties=None):
    # rc=0 is clean, rc>0 means unexpected disconnect
    LOG.warning(f"MQTT on_disconnect rc={rc}")
    with state_lock:
        MQTT_STATUS["connected"] = False
        MQTT_STATUS["last_disconnect_at"] = utc_iso()
        MQTT_STATUS["last_rc"] = rc
        if rc != 0:
            MQTT_STATUS["last_error"] = f"disconnect rc={rc}"


def on_message(client, userdata, msg):
    try:
        parsed = safe_json(msg.payload)
        payload = {
            "ok": True,
            "topic": msg.topic,
            "payload": parsed,
            "received_at": utc_iso(),
            "qos": int(getattr(msg, "qos", 0)),
        }

        with state_lock:
            LAST_MESSAGE.clear()
            LAST_MESSAGE.update(payload)
            LAST_BY_TOPIC[msg.topic] = payload

        # push realtime to UI
        socketio.emit("mqtt_message", payload)

    except Exception as e:
        LOG.exception("on_message error")
        with state_lock:
            MQTT_STATUS["last_error"] = f"on_message error: {e}"


# =====================================================
# MQTT WORKER (BACKGROUND THREAD)
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

    # Let paho handle reconnect timing
    c.reconnect_delay_set(min_delay=1, max_delay=30)
    return c


def mqtt_worker():
    global mqtt_client

    if not MQTT_HOST:
        LOG.error("MQTT_HOST is empty. Set MQTT_HOST in Render Environment Variables.")
        with state_lock:
            MQTT_STATUS["last_error"] = "MQTT_HOST not set"
        return

    if not MQTT_PASS:
        LOG.warning("MQTT_PASS is empty. If broker requires auth, set MQTT_PASS in Render.")
        # do not return; some brokers can allow no pass.

    while True:
        try:
            mqtt_client = build_mqtt_client()

            LOG.info(f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} TLS={MQTT_TLS} insecure={MQTT_TLS_INSECURE}")
            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE_SEC)

            # network loop runs in its own thread
            mqtt_client.loop_start()

            # keep this worker alive; reconnect is handled by paho
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


@app.before_request
def start_worker_once():
    global _worker_started
    if _worker_started:
        return
    with _worker_lock:
        if _worker_started:
            return
        _worker_started = True
        socketio.start_background_task(mqtt_worker)
        LOG.info("Started MQTT background worker")


# =====================================================
# PROFESSIONAL UI (LIKE YOUR SCREENSHOT)
# =====================================================
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Aruka KH-01 MQTT Dashboard</title>
  <style>
    :root{
      --bg:#0b0f16;
      --panel:#101826;
      --panel2:#0f1724;
      --stroke:rgba(255,255,255,.08);
      --text:#e7eefc;
      --muted:rgba(231,238,252,.65);
      --accent:#b8ff2c;
      --good:#32d296;
      --bad:#ff4d4f;
      --shadow:0 10px 30px rgba(0,0,0,.45);
      --radius:18px;
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      --sans: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Helvetica Neue", sans-serif;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      background: radial-gradient(1200px 600px at 20% -10%, rgba(184,255,44,.12), transparent 55%),
                  radial-gradient(900px 700px at 95% 0%, rgba(56,189,248,.10), transparent 55%),
                  var(--bg);
      color:var(--text);
      font-family:var(--sans);
    }
    header{
      padding:22px 20px 8px;
      max-width:1100px;
      margin:0 auto;
    }
    h1{
      margin:0;
      font-size:26px;
      letter-spacing:.2px;
    }
    .sub{
      margin-top:6px;
      color:var(--muted);
      font-size:13px;
    }
    .wrap{
      max-width:1100px;
      margin:0 auto;
      padding:12px 20px 34px;
    }
    .grid2{
      display:grid;
      grid-template-columns: 1.1fr .9fr;
      gap:14px;
    }
    .grid2, .grid4, .grid1{
      margin-top:14px;
    }
    .grid4{
      display:grid;
      grid-template-columns: repeat(2, 1fr);
      gap:14px;
    }
    @media (max-width: 900px){
      .grid2{grid-template-columns:1fr}
      .grid4{grid-template-columns:1fr}
    }
    .card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.02));
      border:1px solid var(--stroke);
      border-radius:var(--radius);
      padding:14px 14px 12px;
      box-shadow: var(--shadow);
      position:relative;
      overflow:hidden;
    }
    .card:before{
      content:"";
      position:absolute;
      inset:-2px;
      background: radial-gradient(500px 150px at 15% 0%, rgba(184,255,44,.10), transparent 60%);
      pointer-events:none;
    }
    .card > *{ position:relative; }
    .title{
      font-weight:700;
      font-size:14px;
      margin:0 0 10px;
      letter-spacing:.2px;
    }
    .kv{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      color:var(--muted);
      font-size:12px;
      line-height:1.4;
    }
    .statusRow{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
      margin-bottom:8px;
    }
    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding:7px 10px;
      border-radius:999px;
      border:1px solid var(--stroke);
      background: rgba(0,0,0,.25);
      font-size:12px;
      color:var(--muted);
    }
    .dot{
      width:10px; height:10px; border-radius:50%;
      background: var(--bad);
      box-shadow: 0 0 18px rgba(255,77,79,.40);
    }
    .dot.ok{
      background: var(--good);
      box-shadow: 0 0 18px rgba(50,210,150,.35);
    }
    button{
      border:1px solid var(--stroke);
      background: rgba(255,255,255,.04);
      color:var(--text);
      border-radius:12px;
      padding:9px 12px;
      cursor:pointer;
      font-weight:600;
      font-size:12px;
    }
    button:hover{ background: rgba(255,255,255,.07); }
    pre{
      margin:0;
      padding:12px;
      border-radius:16px;
      border:1px solid var(--stroke);
      background: rgba(0,0,0,.25);
      max-height:220px;
      overflow:auto;
      font-family:var(--mono);
      font-size:12px;
      line-height:1.35;
    }
    .topicRow{
      display:flex;
      gap:10px;
      align-items:center;
      margin-top:10px;
      justify-content:space-between;
      flex-wrap:wrap;
    }
    .topicPill{
      background: rgba(184,255,44,.85);
      color:#102013;
      font-weight:800;
      padding:8px 12px;
      border-radius:999px;
      font-size:12px;
      letter-spacing:.2px;
      max-width: 100%;
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
    }
    .qosPill{
      background: rgba(184,255,44,.85);
      color:#102013;
      font-weight:800;
      padding:8px 12px;
      border-radius:999px;
      font-size:12px;
    }
    .time{
      margin-top:8px;
      color:var(--muted);
      font-size:11px;
    }
    .section{
      margin-top:18px;
      color:rgba(231,238,252,.85);
      font-weight:800;
      font-size:13px;
      letter-spacing:.3px;
    }
    .hint{
      color:var(--muted);
      font-size:12px;
      margin-top:6px;
    }
  </style>
</head>
<body>
<header>
  <h1>Aruka KH-01 MQTT Dashboard</h1>
  <div class="sub">Realtime monitoring via EMQX Cloud MQTT + Socket.IO (Render)</div>
</header>

<div class="wrap">

  <div class="grid2">
    <div class="card">
      <div class="title">MQTT Connection</div>
      <div class="statusRow">
        <div class="pill">
          <span id="dot" class="dot"></span>
          <span>Status:</span>
          <b id="connText">CONNECTING</b>
        </div>
        <button onclick="refreshStatus()">Refresh</button>
      </div>
      <div class="kv" id="connDetails"></div>
      <div class="hint" id="subHint"></div>
    </div>

    <div class="card">
      <div class="title">Last MQTT Message</div>
      <div class="hint" id="lastMeta">No data</div>
      <pre id="lastPayload">{}</pre>
    </div>
  </div>

  <div class="section">Temperatures</div>
  <div class="grid4" id="tempGrid"></div>

  <div class="section">Power / VFD</div>
  <div class="grid4" id="powerGrid"></div>

  <div class="section">Status</div>
  <div class="grid4" id="statusGrid"></div>

</div>

<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<script>
  const TOPICS = [
    "KH/site-01/KH-01/temperature_probe1",
    "KH/site-01/KH-01/temperature_probe2",
    "KH/site-01/KH-01/temperature_probe3",
    "KH/site-01/KH-01/temperature_probe4",
    "KH/site-01/KH-01/vfd_frequency",
    "KH/site-01/KH-01/power_consumption",
    "KH/site-01/KH-01/status",
  ];

  const SECTION_BY_TOPIC = {
    "KH/site-01/KH-01/temperature_probe1": "temp",
    "KH/site-01/KH-01/temperature_probe2": "temp",
    "KH/site-01/KH-01/temperature_probe3": "temp",
    "KH/site-01/KH-01/temperature_probe4": "temp",
    "KH/site-01/KH-01/vfd_frequency": "power",
    "KH/site-01/KH-01/power_consumption": "power",
    "KH/site-01/KH-01/status": "status",
  };

  const dot = document.getElementById("dot");
  const connText = document.getElementById("connText");
  const connDetails = document.getElementById("connDetails");
  const subHint = document.getElementById("subHint");
  const lastMeta = document.getElementById("lastMeta");
  const lastPayload = document.getElementById("lastPayload");

  const tempGrid = document.getElementById("tempGrid");
  const powerGrid = document.getElementById("powerGrid");
  const statusGrid = document.getElementById("statusGrid");

  // Build cards for each topic
  const cardByTopic = {};
  function makeTopicCard(topic){
    const card = document.createElement("div");
    card.className = "card";

    const title = document.createElement("div");
    title.className = "title";
    title.textContent = "Live Topic";

    const pre = document.createElement("pre");
    pre.textContent = "{\n  \"waiting\": true\n}";

    const topicRow = document.createElement("div");
    topicRow.className = "topicRow";

    const topicPill = document.createElement("div");
    topicPill.className = "topicPill";
    topicPill.title = topic;
    topicPill.textContent = topic;

    const qosPill = document.createElement("div");
    qosPill.className = "qosPill";
    qosPill.textContent = "QoS 0";

    topicRow.appendChild(topicPill);
    topicRow.appendChild(qosPill);

    const t = document.createElement("div");
    t.className = "time";
    t.textContent = "—";

    card.appendChild(title);
    card.appendChild(pre);
    card.appendChild(topicRow);
    card.appendChild(t);

    cardByTopic[topic] = { card, pre, t, qosPill };

    return card;
  }

  // place cards by section
  TOPICS.forEach(t=>{
    const section = SECTION_BY_TOPIC[t] || "status";
    const c = makeTopicCard(t);
    if(section === "temp") tempGrid.appendChild(c);
    else if(section === "power") powerGrid.appendChild(c);
    else statusGrid.appendChild(c);
  });

  // Socket.IO (match server path)
  const socket = io({ path: "/socket.io", transports: ["polling", "websocket"] });

  socket.on("connect", ()=>{ /* ok */ });
  socket.on("disconnect", ()=>{ /* ok */ });

  socket.on("mqtt_message", (msg)=>{
    // update last message
    lastMeta.textContent = `${msg.received_at} | ${msg.topic}`;
    lastPayload.textContent = JSON.stringify(msg.payload, null, 2);

    // update per-topic card
    const ref = cardByTopic[msg.topic];
    if(ref){
      ref.pre.textContent = JSON.stringify(msg.payload, null, 2);
      ref.t.textContent = msg.received_at;
      ref.qosPill.textContent = "QoS " + (msg.qos ?? 0);
    }
  });

  async function refreshStatus(){
    const r = await fetch("/status");
    const data = await r.json();

    if (data.mqtt.connected) {
      dot.className = "dot ok";
      connText.textContent = "CONNECTED";
    } else {
      dot.className = "dot";
      connText.textContent = "DISCONNECTED";
    }

    const m = data.mqtt;
    connDetails.textContent =
      `host=${m.host}:${m.port} | tls=${m.tls} insecure=${m.tls_insecure} | client_id=${m.client_id} | last_rc=${m.last_rc} | last_error=${m.last_error || "none"}`;

    subHint.textContent = `Subscribing to ${m.topics.length} topics (exact list, no wildcard).`;
  }

  refreshStatus();
  setInterval(refreshStatus, 8000);
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
    with state_lock:
        data = dict(LAST_BY_TOPIC)
    return jsonify({"ok": True, "topics": data})


@app.post("/publish")
def publish():
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
