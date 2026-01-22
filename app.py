#!/usr/bin/env python3
"""
Aruka / KH-01 MQTT Dashboard (Render, stable Socket.IO with eventlet)

RENDER START COMMAND:
  gunicorn -k eventlet -w 1 app:app --bind 0.0.0.0:$PORT --log-level info --timeout 120

RENDER ENV VARS:
  MQTT_HOST=t569f61e.ala.asia-southeast1.emqxsl.com
  MQTT_PORT=8883
  MQTT_USER=KH-01-device
  MQTT_PASS=*** (set in Render, do NOT commit)
  MQTT_TLS=1
  MQTT_TLS_INSECURE=1   # optional (1 like ESP32), else 0
  MQTT_CLIENT_ID=Aruka_KH_render
  SECRET_KEY=anystring
"""

# IMPORTANT: must be first
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


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("ARUKA_RENDER")


def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_json_loads(s: str):
    try:
        return json.loads(s)
    except Exception:
        return s


# =====================================================
# MQTT CONFIG (ONLY YOUR 7 TOPICS)
# =====================================================
MQTT_HOST = os.environ.get("MQTT_HOST", "").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "").strip()

MQTT_TLS = env_bool("MQTT_TLS", "1")
MQTT_TLS_INSECURE = env_bool("MQTT_TLS_INSECURE", "0")
MQTT_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "Aruka_KH_render").strip()

MQTT_KEEPALIVE_SEC = int(os.environ.get("MQTT_KEEPALIVE_SEC", "60"))
MQTT_RECONNECT_MIN = int(os.environ.get("MQTT_RECONNECT_MIN", "1"))
MQTT_RECONNECT_MAX = int(os.environ.get("MQTT_RECONNECT_MAX", "30"))

SECRET_KEY = os.environ.get("SECRET_KEY", "change-me").strip()

TOPICS_TEMP = [
    "KH/site-01/KH-01/temperature_probe1",
    "KH/site-01/KH-01/temperature_probe2",
    "KH/site-01/KH-01/temperature_probe3",
    "KH/site-01/KH-01/temperature_probe4",
]
TOPICS_POWER = [
    "KH/site-01/KH-01/power_consumption",
    "KH/site-01/KH-01/vfd_frequency",
]
TOPICS_STATUS = [
    "KH/site-01/KH-01/status",
]
SUB_TOPICS = TOPICS_TEMP + TOPICS_POWER + TOPICS_STATUS


# =====================================================
# FLASK / SOCKETIO (EVENTLET)
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="eventlet",
    ping_interval=25,
    ping_timeout=60,
)


# =====================================================
# SHARED STATE
# =====================================================
state_lock = eventlet.semaphore.Semaphore(1)

MQTT_STATUS = {
    "connected": False,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
    "host": MQTT_HOST,
    "port": MQTT_PORT,
    "tls": MQTT_TLS,
    "tls_insecure": MQTT_TLS_INSECURE,
    "topics": SUB_TOPICS,
}

LAST_BY_TOPIC = {}  # topic -> last message object
LAST_MESSAGE = {
    "ok": False,
    "topic": None,
    "payload": None,
    "received_at": None,
    "note": "No data yet",
}

mqtt_client = None


# =====================================================
# VALUE FORMATTER (for professional cards)
# =====================================================
def summarize(topic: str, payload):
    """
    Convert your JSON payload into a clean card:
    - temperature: show big value + unit + name
    - power: show 3-phase (or keys)
    - vfd: show key summary
    - status: show burners states
    """
    # payload might be dict or string
    if not isinstance(payload, dict):
        return {"headline": "Message", "value": str(payload), "unit": "", "meta": ""}

    # temperature format expected:
    # {"value":504,"name":"Temperature Sensor 1(reactor)","unit":"C","timestamp":"..."}
    if "temperature_probe" in topic:
        v = payload.get("value", "")
        unit = payload.get("unit", "")
        name = payload.get("name", "")
        ts = payload.get("timestamp", "")
        return {"headline": name or "Temperature", "value": v, "unit": unit, "meta": ts}

    if topic.endswith("/power_consumption"):
        unit = payload.get("unit", "")
        v = payload.get("value", payload)
        ts = payload.get("timestamp", "")
        if isinstance(v, dict):
            # show A/B/C if exists
            a = v.get("phaseA")
            b = v.get("phaseB")
            c = v.get("phaseC")
            if a is not None and b is not None and c is not None:
                return {"headline": "Power Consumption", "value": f"A:{a}  B:{b}  C:{c}", "unit": unit, "meta": ts}
            # else fallback
            return {"headline": "Power Consumption", "value": ", ".join([f"{k}:{v[k]}" for k in list(v)[:6]]), "unit": unit, "meta": ts}
        return {"headline": "Power Consumption", "value": str(v), "unit": unit, "meta": ts}

    if topic.endswith("/vfd_frequency"):
        unit = payload.get("unit", "")
        v = payload.get("value", payload)
        ts = payload.get("timestamp", "")
        if isinstance(v, dict):
            # show first few keys
            parts = [f"{k}:{v[k]}" for k in list(v)[:6]]
            return {"headline": "VFD Frequency", "value": " | ".join(parts), "unit": unit, "meta": ts}
        return {"headline": "VFD Frequency", "value": str(v), "unit": unit, "meta": ts}

    if topic.endswith("/status"):
        v = payload.get("value", payload)
        ts = payload.get("timestamp", "")
        if isinstance(v, dict):
            on = [k for k, val in v.items() if str(val) == "1" or val is True]
            off = [k for k, val in v.items() if str(val) == "0" or val is False]
            return {"headline": "Status", "value": f"ON: {', '.join(on) if on else '-'}", "unit": "", "meta": f"OFF: {', '.join(off) if off else '-'} | {ts}"}
        return {"headline": "Status", "value": str(v), "unit": "", "meta": ts}

    # default
    return {"headline": "Message", "value": str(payload), "unit": "", "meta": ""}


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc, properties=None):
    LOG.info(f"MQTT on_connect rc={rc}")

    with state_lock:
        MQTT_STATUS["connected"] = (rc == 0)
        MQTT_STATUS["last_connect_at"] = utc_iso()
        MQTT_STATUS["last_error"] = None if rc == 0 else f"connect rc={rc}"

    if rc != 0:
        return

    ok_count = 0
    for t in SUB_TOPICS:
        try:
            client.subscribe(t, qos=0)
            ok_count += 1
        except Exception as e:
            LOG.exception(f"Subscribe failed: {t}")
            with state_lock:
                MQTT_STATUS["last_error"] = f"subscribe failed: {t} => {e}"

    LOG.info(f"Subscribed to {ok_count}/{len(SUB_TOPICS)} topics")


def on_disconnect(client, userdata, rc, properties=None):
    # rc=7 usually "connection lost"
    LOG.warning(f"MQTT on_disconnect rc={rc}")
    with state_lock:
        MQTT_STATUS["connected"] = False
        MQTT_STATUS["last_disconnect_at"] = utc_iso()
        MQTT_STATUS["last_error"] = f"disconnect rc={rc}" if rc else MQTT_STATUS["last_error"]


def on_message(client, userdata, msg):
    try:
        raw = msg.payload.decode("utf-8", errors="replace")
        parsed = safe_json_loads(raw)

        s = summarize(msg.topic, parsed)

        payload = {
            "ok": True,
            "topic": msg.topic,
            "qos": int(getattr(msg, "qos", 0)),
            "retain": bool(getattr(msg, "retain", False)),
            "payload": parsed,
            "summary": s,
            "received_at": utc_iso(),
        }

        with state_lock:
            LAST_MESSAGE.clear()
            LAST_MESSAGE.update(payload)
            LAST_BY_TOPIC[msg.topic] = payload

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
        c.tls_set(ca_certs=certifi.where(), tls_version=ssl.PROTOCOL_TLS_CLIENT)
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
                socketio.sleep(5)

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

            socketio.sleep(3)


_started = False
_started_lock = eventlet.semaphore.Semaphore(1)


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
# UI (now shows BIG values + still supports JSON)
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
      --bg:#0b0f17; --panel:#111827; --text:#e5e7eb; --muted:#9ca3af;
      --border:#1f2937; --accent:#a3e635; --shadow: 0 12px 40px rgba(0,0,0,.35);
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
    .toprow{ display:grid; grid-template-columns: 1fr 1fr; gap:16px; margin-bottom:16px; }
    @media (max-width: 900px){ .toprow{ grid-template-columns:1fr; } }
    .card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      border:1px solid var(--border);
      border-radius: var(--radius);
      box-shadow: var(--shadow);
      padding:16px;
    }
    .title{ font-weight:800; margin-bottom:10px; font-size:16px; }
    .muted{ color: var(--muted); font-size:13px; line-height:1.4; }
    .status{ font-weight:900; letter-spacing:.3px; }
    .ok{ color:#22c55e; }
    .bad{ color:#ef4444; }
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

    .section{ margin-top:16px; }
    .section h2{ margin:18px 0 10px; font-size:16px; color:#cbd5e1; letter-spacing:.2px; }
    .grid{ display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
    @media (max-width: 900px){ .grid{ grid-template-columns:1fr; } }

    .topic-card{
      background: linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.015));
      border:1px solid var(--border);
      border-radius: 26px;
      box-shadow: var(--shadow);
      padding:18px;
      position:relative;
      overflow:hidden;
      min-height: 170px;
    }
    .topic-card:before{
      content:"";
      position:absolute; inset:-80px;
      background: radial-gradient(260px 180px at 20% 30%, rgba(163,230,53,.10), transparent 70%),
                  radial-gradient(260px 180px at 80% 40%, rgba(59,130,246,.10), transparent 70%);
      pointer-events:none;
    }
    .topic-card > *{ position:relative; }

    .bigrow{ display:flex; align-items:baseline; gap:10px; margin-top:8px; flex-wrap:wrap; }
    .bigvalue{ font-size:40px; font-weight:1000; letter-spacing:.3px; }
    .unit{ font-size:16px; font-weight:900; color:#d1d5db; }
    .headline{ font-size:14px; font-weight:900; color:#cbd5e1; }

    details{ margin-top:12px; }
    pre{
      margin:0;
      padding:14px;
      border-radius: 16px;
      background: rgba(0,0,0,.22);
      border:1px solid rgba(255,255,255,.06);
      color: #e5e7eb;
      overflow:auto;
      max-height: 200px;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .cardhead{
      display:flex; justify-content:space-between; align-items:center;
      gap:12px; margin-top:14px; flex-wrap:wrap;
    }
    .badge{
      display:inline-flex; align-items:center; justify-content:center;
      padding:10px 16px; border-radius:999px;
      background: rgba(163,230,53,.90);
      color:#0b0f17; font-weight:900; letter-spacing:.2px; white-space:nowrap;
    }
    .badge.qos{ min-width: 82px; }
    .split{ display:flex; justify-content:space-between; align-items:center; gap:10px; flex-wrap:wrap; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>Aruka KH-01 MQTT Dashboard</h1>

    <div class="toprow">
      <div class="card">
        <div class="title">MQTT Connection</div>
        <div class="split">
          <div>Status: <span id="conn" class="status bad">DISCONNECTED</span></div>
          <button onclick="refreshAll()">Refresh</button>
        </div>
        <div class="muted" id="connDetails" style="margin-top:10px;"></div>
        <div class="muted" id="topicsInfo" style="margin-top:8px;"></div>
      </div>

      <div class="card">
        <div class="title">Last MQTT Message</div>
        <div class="muted" id="lastMeta">No data</div>
        <pre id="lastPayload">{}</pre>
      </div>
    </div>

    <div class="card section">
      <h2>Temperatures</h2>
      <div class="grid" id="gridTemp"></div>

      <h2>Power / VFD</h2>
      <div class="grid" id="gridPower"></div>

      <h2>Status</h2>
      <div class="grid" id="gridStatus"></div>
    </div>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script>
    const connEl = document.getElementById("conn");
    const connDetailsEl = document.getElementById("connDetails");
    const topicsInfoEl = document.getElementById("topicsInfo");
    const lastMetaEl = document.getElementById("lastMeta");
    const lastPayloadEl = document.getElementById("lastPayload");

    const gridTemp = document.getElementById("gridTemp");
    const gridPower = document.getElementById("gridPower");
    const gridStatus = document.getElementById("gridStatus");

    const socket = io({ path: "/socket.io" });

    let topicsMap = {};

    function fmtPayload(p){
      try { return (typeof p === "string") ? p : JSON.stringify(p, null, 2); }
      catch(e){ return String(p); }
    }
    function escapeHtml(s){
      return String(s)
        .replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")
        .replaceAll('"',"&quot;").replaceAll("'","&#039;");
    }

    function cardHtml(m){
      const s = m.summary || {};
      const headline = s.headline || "Message";
      const value = (s.value === undefined || s.value === null) ? "" : s.value;
      const unit = s.unit || "";
      const meta = s.meta || "";
      const qos = (m.qos === undefined || m.qos === null) ? 0 : m.qos;

      return `
        <div class="topic-card">
          <div class="headline">${escapeHtml(headline)}</div>
          <div class="bigrow">
            <div class="bigvalue">${escapeHtml(value)}</div>
            <div class="unit">${escapeHtml(unit)}</div>
          </div>
          <div class="muted" style="margin-top:6px;">${escapeHtml(meta)}</div>

          <div class="cardhead">
            <div class="badge">${escapeHtml(m.topic || "")}</div>
            <div class="badge qos">QoS ${qos}</div>
          </div>
          <div class="muted" style="margin-top:10px;">${escapeHtml(m.received_at || "")}</div>

          <details>
            <summary class="muted">View raw JSON</summary>
            <pre>${escapeHtml(fmtPayload(m.payload))}</pre>
          </details>
        </div>
      `;
    }

    function renderGroups(){
      const temp = [];
      const power = [];
      const status = [];
      const all = Object.values(topicsMap);

      for (const m of all){
        if (!m || !m.topic) continue;
        if (m.topic.includes("temperature_probe")) temp.push(m);
        else if (m.topic.includes("vfd_frequency") || m.topic.includes("power_consumption")) power.push(m);
        else if (m.topic.endsWith("/status")) status.push(m);
      }

      temp.sort((a,b)=> (a.topic||"").localeCompare(b.topic||""));
      power.sort((a,b)=> (a.topic||"").localeCompare(b.topic||""));
      status.sort((a,b)=> (a.topic||"").localeCompare(b.topic||""));

      gridTemp.innerHTML = temp.map(cardHtml).join("") || `<div class="muted">Waiting for temperature messages...</div>`;
      gridPower.innerHTML = power.map(cardHtml).join("") || `<div class="muted">Waiting for power/vfd messages...</div>`;
      gridStatus.innerHTML = status.map(cardHtml).join("") || `<div class="muted">Waiting for status message...</div>`;
    }

    socket.on("mqtt_message", (msg) => {
      lastMetaEl.textContent = `${msg.received_at} | ${msg.topic}`;
      lastPayloadEl.textContent = fmtPayload(msg.payload);

      topicsMap[msg.topic] = msg;
      renderGroups();
    });

    async function refreshStatus(){
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
        `host=${data.mqtt.host || "?"}:${data.mqtt.port} | tls=${data.mqtt.tls} insecure=${data.mqtt.tls_insecure} | last_error=${data.mqtt.last_error || "none"}`;

      topicsInfoEl.textContent =
        `Subscribing to ${data.mqtt.topics.length} topics (exact list, no wildcard).`;
    }

    async function refreshTopics(){
      const r = await fetch("/topics");
      const data = await r.json();
      if (data.ok && data.topics){
        topicsMap = data.topics;
        renderGroups();
      }
    }

    async function refreshAll(){
      await refreshStatus();
      await refreshTopics();
    }

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
