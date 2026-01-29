#!/usr/bin/env python3
"""
Aruka / KH-01 Industrial Control Dashboard (Single File)

Render Start Command (recommended for stability):
  gunicorn -w 1 --threads 8 --worker-class gthread --timeout 120 app:app --bind 0.0.0.0:$PORT --log-level info

Render Environment Variables:
  MQTT_HOST=t569f61e.ala.asia-southeast1.emqxsl.com
  MQTT_PORT=8883
  MQTT_USER=KH-01-device
  MQTT_PASS=Radiation0-Disperser8-Sternum1-Trio4
  MQTT_TLS=1
  MQTT_TLS_INSECURE=0
  MQTT_CLIENT_ID=Aruka_KH
  SECRET_KEY=anystring

Optional (if you want to override topics):
  MQTT_TOPICS=KH/site-01/KH-01/temperature_probe1,KH/site-01/KH-01/temperature_probe2,...
"""

import os
import ssl
import json
import time
import socket
import logging
import threading
import certifi
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template_string
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt


# =====================================================
# LOGGING
# =====================================================
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
LOG = logging.getLogger("KH01")


# =====================================================
# HELPERS
# =====================================================
def env_bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_json_loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return s


def norm_topic(s: str) -> str:
    return (s or "").strip()


# =====================================================
# CONFIG
# =====================================================
MQTT_HOST = os.environ.get("MQTT_HOST", "t569f61e.ala.asia-southeast1.emqxsl.com").strip()
MQTT_PORT = int(os.environ.get("MQTT_PORT", "8883"))
MQTT_USER = os.environ.get("MQTT_USER", "KH-01-device").strip()
MQTT_PASS = os.environ.get("MQTT_PASS", "Radiation0-Disperser8-Sternum1-Trio4").strip()

MQTT_TLS = env_bool("MQTT_TLS", "1")
MQTT_TLS_INSECURE = env_bool("MQTT_TLS_INSECURE", "0")

BASE_CLIENT_ID = os.environ.get("MQTT_CLIENT_ID", "Aruka_KH").strip()
SECRET_KEY = os.environ.get("SECRET_KEY", "change-me").strip()

MQTT_KEEPALIVE_SEC = int(os.environ.get("MQTT_KEEPALIVE_SEC", "60"))
MQTT_RECONNECT_MIN = int(os.environ.get("MQTT_RECONNECT_MIN", "1"))
MQTT_RECONNECT_MAX = int(os.environ.get("MQTT_RECONNECT_MAX", "30"))

DEFAULT_TOPICS = [
    "KH/site-01/KH-01/temperature_probe1",
    "KH/site-01/KH-01/temperature_probe2",
    "KH/site-01/KH-01/temperature_probe3",
    "KH/site-01/KH-01/temperature_probe4",
    "KH/site-01/KH-01/vfd_frequency",
    "KH/site-01/KH-01/power_consumption",
    "KH/site-01/KH-01/status",
]

MQTT_TOPICS_ENV = os.environ.get("MQTT_TOPICS", "").strip()
if MQTT_TOPICS_ENV:
    MQTT_TOPICS: List[str] = [norm_topic(x) for x in MQTT_TOPICS_ENV.split(",") if norm_topic(x)]
else:
    MQTT_TOPICS = DEFAULT_TOPICS

# Avoid broker kicking you for duplicate client_id: add stable-ish suffix
HOSTNAME = socket.gethostname()
CLIENT_ID = f"{BASE_CLIENT_ID}_{HOSTNAME}_{os.getpid()}"


# =====================================================
# FLASK / SOCKETIO
# =====================================================
app = Flask(__name__)
app.config["SECRET_KEY"] = SECRET_KEY

# threading mode is safe on Python 3.13 (eventlet is not)
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
lock = threading.Lock()

MQTT_STATUS: Dict[str, Any] = {
    "connected": False,
    "host": MQTT_HOST,
    "port": MQTT_PORT,
    "tls": MQTT_TLS,
    "tls_insecure": MQTT_TLS_INSECURE,
    "client_id": CLIENT_ID,
    "topics": MQTT_TOPICS,
    "last_connect_at": None,
    "last_disconnect_at": None,
    "last_error": None,
}

LAST_BY_TOPIC: Dict[str, Dict[str, Any]] = {}  # topic -> payload record
LAST_UPDATE_AT: Optional[str] = None

mqtt_client: Optional[mqtt.Client] = None


# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc, properties=None):
    LOG.info(f"MQTT on_connect rc={rc}")
    with lock:
        MQTT_STATUS["connected"] = (rc == 0)
        MQTT_STATUS["last_connect_at"] = utc_iso()
        MQTT_STATUS["last_error"] = None if rc == 0 else f"connect rc={rc}"

    if rc == 0:
        ok = 0
        for t in MQTT_TOPICS:
            try:
                client.subscribe(t, qos=0)
                ok += 1
            except Exception as e:
                LOG.error(f"Subscribe failed for {t}: {e}")
                with lock:
                    MQTT_STATUS["last_error"] = f"subscribe failed: {e}"
        LOG.info(f"Subscribed to {ok}/{len(MQTT_TOPICS)} topics")


def on_disconnect(client, userdata, rc, properties=None):
    LOG.warning(f"MQTT on_disconnect rc={rc}")
    with lock:
        MQTT_STATUS["connected"] = False
        MQTT_STATUS["last_disconnect_at"] = utc_iso()
        if rc != 0:
            MQTT_STATUS["last_error"] = f"disconnect rc={rc}"


def on_message(client, userdata, msg):
    global LAST_UPDATE_AT
    try:
        raw = msg.payload.decode("utf-8", errors="replace")
        parsed = safe_json_loads(raw)

        record = {
            "topic": msg.topic,
            "payload": parsed,
            "received_at": utc_iso(),
            "qos": int(getattr(msg, "qos", 0)),
        }

        with lock:
            LAST_BY_TOPIC[msg.topic] = record
            LAST_UPDATE_AT = record["received_at"]

        socketio.emit("mqtt_message", record)

    except Exception as e:
        LOG.exception("on_message error")
        with lock:
            MQTT_STATUS["last_error"] = f"on_message error: {e}"


# =====================================================
# MQTT WORKER
# =====================================================
def build_mqtt_client() -> mqtt.Client:
    c = mqtt.Client(client_id=CLIENT_ID, protocol=mqtt.MQTTv311, clean_session=True)
    c.enable_logger(LOG)

    if MQTT_USER:
        c.username_pw_set(MQTT_USER, MQTT_PASS)

    if MQTT_TLS:
        c.tls_set(
            ca_certs=certifi.where(),
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
        c.tls_insecure_set(MQTT_TLS_INSECURE)

    c.reconnect_delay_set(min_delay=MQTT_RECONNECT_MIN, max_delay=MQTT_RECONNECT_MAX)

    c.on_connect = on_connect
    c.on_disconnect = on_disconnect
    c.on_message = on_message
    return c


def mqtt_worker():
    global mqtt_client

    if not MQTT_HOST:
        LOG.error("MQTT_HOST is empty. Set MQTT_HOST in environment variables.")
        with lock:
            MQTT_STATUS["last_error"] = "MQTT_HOST not set"
        return

    while True:
        try:
            mqtt_client = build_mqtt_client()
            LOG.info(
                f"MQTT connecting to {MQTT_HOST}:{MQTT_PORT} "
                f"TLS={MQTT_TLS} insecure={MQTT_TLS_INSECURE} client_id={CLIENT_ID}"
            )

            mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=MQTT_KEEPALIVE_SEC)
            mqtt_client.loop_forever(retry_first_connection=True)

        except Exception as e:
            LOG.error(f"MQTT worker exception: {e}")
            with lock:
                MQTT_STATUS["connected"] = False
                MQTT_STATUS["last_error"] = str(e)

            try:
                if mqtt_client:
                    mqtt_client.disconnect()
            except Exception:
                pass

            time.sleep(3)


_started = False
_started_lock = threading.Lock()


@app.before_request
def _start_once():
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
# UI (Responsive Dashboard)
# =====================================================
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover"/>
  <title>KH-01 Industrial Control</title>
  <style>
    :root{
      --bg0:#070b12;
      --bg1:#0b1220;
      --line:rgba(255,255,255,.08);
      --muted:rgba(255,255,255,.65);
      --text:#e9eefc;
      --accent:#36d399;
      --danger:#ef4444;
      --blue:#60a5fa;
      --cyan:#22d3ee;
    }
    *{box-sizing:border-box;}
    body{
      margin:0;
      background:
        radial-gradient(1200px 700px at 20% 10%, rgba(96,165,250,.14), transparent 55%),
        radial-gradient(1000px 600px at 70% 40%, rgba(34,211,238,.10), transparent 60%),
        linear-gradient(180deg, var(--bg1), var(--bg0));
      color:var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, "Noto Sans", "Liberation Sans", sans-serif;
    }

    .wrap{
      max-width:1100px;
      margin:0 auto;
      padding:
        calc(18px + env(safe-area-inset-top))
        calc(14px + env(safe-area-inset-right))
        40px
        calc(14px + env(safe-area-inset-left));
    }

    .topbar{
      display:flex;align-items:center;justify-content:space-between;
      gap:12px;flex-wrap:wrap;
      padding:14px 16px;border:1px solid var(--line);border-radius:16px;
      background:linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
      backdrop-filter: blur(8px);
    }
    .brand{display:flex;gap:12px;align-items:center;}
    .logo{
      width:36px;height:36px;border-radius:10px;
      background:linear-gradient(135deg, rgba(96,165,250,.9), rgba(34,211,238,.8));
      display:flex;align-items:center;justify-content:center;
      box-shadow: 0 10px 30px rgba(34,211,238,.18);
      font-weight:800;color:#08111f;
      flex:0 0 auto;
    }
    .title h1{margin:0;font-size:18px;letter-spacing:.2px;}
    .title p{margin:2px 0 0;color:var(--muted);font-size:12px;}

    .statusbar{display:flex;gap:12px;align-items:center;flex-wrap:wrap;}
    .pill{
      display:inline-flex;align-items:center;gap:8px;
      padding:8px 12px;border-radius:999px;border:1px solid var(--line);
      background:rgba(255,255,255,.03);
      font-size:13px;color:var(--muted);
    }
    .dot{width:10px;height:10px;border-radius:50%;}
    .dot.ok{background:var(--accent);box-shadow:0 0 0 4px rgba(54,211,153,.12);}
    .dot.bad{background:var(--danger);box-shadow:0 0 0 4px rgba(239,68,68,.12);}
    .small{font-size:12px;color:var(--muted);}

    .section{
      margin-top:14px;
      border:1px solid var(--line);border-radius:16px;
      background:linear-gradient(180deg, rgba(255,255,255,.04), rgba(255,255,255,.02));
      overflow:hidden;
    }
    .sectionHead{
      display:flex;align-items:center;justify-content:space-between;
      gap:10px;
      padding:12px 14px;border-bottom:1px solid var(--line);
      flex-wrap:wrap;
    }
    .sectionHead .left{display:flex;gap:10px;align-items:center;}
    .ico{
      width:30px;height:30px;border-radius:10px;
      display:flex;align-items:center;justify-content:center;
      background:rgba(96,165,250,.18);border:1px solid rgba(96,165,250,.25);
      color:#bcd7ff;font-weight:700;
      flex:0 0 auto;
    }
    .sectionHead h2{margin:0;font-size:14px;letter-spacing:.2px;}

    /* Auto responsive grid (works for laptop + iPhone 12 Pro Max + Android) */
    .cards{
      padding:14px;
      display:grid;
      gap:12px;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    }

    .card{
      border:1px solid var(--line);border-radius:16px;
      background:linear-gradient(180deg, rgba(255,255,255,.03), rgba(255,255,255,.01));
      padding:14px;
      min-height:92px;
    }
    .card .label{font-size:12px;color:var(--muted);display:flex;justify-content:space-between;gap:10px;}
    .tag{
      padding:4px 8px;border-radius:999px;border:1px solid var(--line);
      background:rgba(255,255,255,.03);color:rgba(255,255,255,.72);font-size:11px;white-space:nowrap;
    }
    .value{margin-top:10px;font-size:34px;font-weight:800;letter-spacing:.4px;}
    .unit{font-size:14px;color:rgba(255,255,255,.75);font-weight:700;margin-left:8px;}
    .sub{margin-top:6px;font-size:12px;color:var(--muted);}
    .accentBar{
      height:3px;border-radius:999px;margin-top:10px;background:rgba(255,255,255,.10);overflow:hidden;
    }
    .accentBar > div{height:100%;width:40%;background:linear-gradient(90deg, var(--accent), var(--cyan));}

    /* Burners: responsive auto-fit */
    .burnerRow{
      display:grid;
      grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
      gap:12px;
      padding:14px;
    }
    .burner{
      border:1px solid rgba(239,68,68,.45);
      border-radius:16px;padding:14px;
      background:linear-gradient(180deg, rgba(239,68,68,.10), rgba(255,255,255,.01));
      display:flex;justify-content:space-between;align-items:center;gap:12px;
    }
    .burner.on{
      border-color: rgba(54,211,153,.45);
      background:linear-gradient(180deg, rgba(54,211,153,.12), rgba(255,255,255,.01));
    }
    .burner .bname{font-weight:800;}
    .burner .bsub{margin-top:3px;font-size:12px;color:var(--muted);}
    .badge{
      padding:7px 12px;border-radius:999px;font-weight:800;font-size:12px;
      border:1px solid var(--line);background:rgba(0,0,0,.18);
      min-width:64px;text-align:center;
    }
    .badge.off{color:rgba(255,255,255,.65);}
    .badge.on{color:var(--accent);border-color:rgba(54,211,153,.35);}

    .toolbar{display:flex;gap:10px;align-items:center;}
    button{
      cursor:pointer;
      padding:8px 12px;border-radius:12px;
      border:1px solid var(--line);
      background:rgba(255,255,255,.04);
      color:rgba(255,255,255,.86);
    }
    button:hover{background:rgba(255,255,255,.06);}
    pre{
      margin:0;
      padding:12px 14px;
      color:rgba(255,255,255,.82);
      overflow:auto;max-height:220px;
      background:rgba(0,0,0,.18);
      border-top:1px solid var(--line);
    }
    .foot{margin-top:12px;color:var(--muted);font-size:12px;text-align:right;}

    /* Extra mobile tuning */
    @media (max-width: 480px){
      .title h1{ font-size: 16px; }
      .title p{ font-size: 11px; }
      .pill{ padding: 7px 10px; font-size: 12px; }
      .sectionHead{ padding: 10px 12px; }
      .cards, .burnerRow{ padding: 12px; gap: 10px; }
      .card{ padding: 12px; border-radius: 14px; }
      .value{ font-size: 28px; }
      .unit{ font-size: 13px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <div class="logo">KH</div>
        <div class="title">
          <h1>KH-01 INDUSTRIAL CONTROL</h1>
          <p>Real-time process monitoring dashboard</p>
        </div>
      </div>

      <div class="statusbar">
        <div class="pill">
          <span id="dot" class="dot bad"></span>
          <span id="connText">Disconnected</span>
        </div>
        <div class="pill">
          <span class="small">Last update:</span>
          <b id="lastUpdate">--:--:--</b>
        </div>
      </div>
    </div>

    <div class="section">
      <div class="sectionHead">
        <div class="left">
          <div class="ico">T</div>
          <h2>Temperature Monitoring</h2>
        </div>
        <div class="toolbar">
          <button onclick="refreshAll()">Refresh</button>
        </div>
      </div>
      <div class="cards" id="tempCards"></div>
    </div>

    <div class="section">
      <div class="sectionHead">
        <div class="left">
          <div class="ico">P</div>
          <h2>Power Consumption</h2>
        </div>
      </div>
      <div class="cards" id="powerCards"></div>
    </div>

    <div class="section">
      <div class="sectionHead">
        <div class="left">
          <div class="ico">B</div>
          <h2>Burner Status</h2>
        </div>
      </div>
      <div class="burnerRow" id="burnerRow"></div>
    </div>

    <div class="section">
      <div class="sectionHead">
        <div class="left">
          <div class="ico">V</div>
          <h2>VFD Frequency Control</h2>
        </div>
      </div>
      <div class="cards" id="vfdCards"></div>
    </div>

    <div class="section">
      <div class="sectionHead">
        <div class="left">
          <div class="ico">D</div>
          <h2>Last Raw Messages (Debug)</h2>
        </div>
      </div>
      <pre id="debug"></pre>
    </div>

    <div class="foot">Aruka / KH-01</div>
  </div>

  <script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
  <script>
    const TOPICS = {
      t1: "KH/site-01/KH-01/temperature_probe1",
      t2: "KH/site-01/KH-01/temperature_probe2",
      t3: "KH/site-01/KH-01/temperature_probe3",
      t4: "KH/site-01/KH-01/temperature_probe4",
      vfd: "KH/site-01/KH-01/vfd_frequency",
      pwr: "KH/site-01/KH-01/power_consumption",
      sts: "KH/site-01/KH-01/status",
    };

    const state = {
      connected: false,
      lastUpdate: null,
      byTopic: {},
    };

    const dot = document.getElementById("dot");
    const connText = document.getElementById("connText");
    const lastUpdate = document.getElementById("lastUpdate");
    const debug = document.getElementById("debug");

    const tempCards = document.getElementById("tempCards");
    const powerCards = document.getElementById("powerCards");
    const burnerRow = document.getElementById("burnerRow");
    const vfdCards = document.getElementById("vfdCards");

    function setConnected(ok){
      state.connected = ok;
      if(ok){
        dot.className = "dot ok";
        connText.textContent = "Connected";
      } else {
        dot.className = "dot bad";
        connText.textContent = "Disconnected";
      }
    }

    function fmtTime(iso){
      if(!iso) return "--:--:--";
      try{
        // force re-evaluation even if same timestamp string
        const d = new Date(iso);

        if (isNaN(d.getTime())) return "--:--:--";

        return new Intl.DateTimeFormat("en-GB", {
          timeZone: "Asia/Phnom_Penh",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: true
        }).format(d);
      }catch(e){
        return "--:--:--";
      }
    }




    function safeObj(x){
      return (x && typeof x === "object") ? x : null;
    }

    function getPayload(topic){
      const rec = state.byTopic[topic];
      if(!rec) return null;
      return rec.payload;
    }

    function cardHTML(label, tag, value, unit, sub, barPct){
      const pct = Math.max(0, Math.min(100, barPct ?? 40));
      return `
        <div class="card">
          <div class="label">
            <span>${label}</span>
            <span class="tag">${tag}</span>
          </div>
          <div class="value">${value}<span class="unit">${unit || ""}</span></div>
          <div class="sub">${sub || ""}</div>
          <div class="accentBar"><div style="width:${pct}%"></div></div>
        </div>
      `;
    }

    function renderTemps(){
      const defs = [
        {topic:TOPICS.t1, title:"Reactor (outscrew)", tag:"Probe #1"},
        {topic:TOPICS.t2, title:"Primary Burner", tag:"Probe #2"},
        {topic:TOPICS.t3, title:"Secondary Burner", tag:"Probe #3"},
        {topic:TOPICS.t4, title:"Reactor (end)", tag:"Probe #4"},
      ];


      let html = "";
      for(const d of defs){
        const p = getPayload(d.topic);
        const obj = safeObj(p);
        const v = obj?.value ?? "--";
        const unit = obj?.unit ?? "°C";
        const ts = state.byTopic[d.topic]?.received_at ?? obj?.timestamp ?? "";

        const sub = ts ? ("Updated: " + fmtTime(ts)) : "Waiting for data...";
        const num = Number(v);
        const pct = isFinite(num) ? Math.max(5, Math.min(100, (num/900)*100)) : 25;
        html += cardHTML(d.title, d.tag, v, unit, sub, pct);
      }
      tempCards.innerHTML = html;
    }

    function renderPower(){
      const p = getPayload(TOPICS.pwr);
      const obj = safeObj(p);
      const val = safeObj(obj?.value);

      const unit = obj?.unit || "A";
      const ts = state.byTopic[d.topic]?.received_at ?? obj?.timestamp ?? "";

      const sub = ts ? ("Updated: " + fmtTime(ts)) : "Waiting for data...";

      const a = val?.phaseA ?? "--";
      const b = val?.phaseB ?? "--";
      const c = val?.phaseC ?? "--";

      const defs = [
        {title:"PHASE A", v:a},
        {title:"PHASE B", v:b},
        {title:"PHASE C", v:c},
      ];

      let html = "";
      for(const d of defs){
        const num = Number(d.v);
        const pct = isFinite(num) ? Math.max(5, Math.min(100, (num/100)*100)) : 20;
        html += cardHTML(d.title, "Current", d.v, unit, sub, pct);
      }
      powerCards.innerHTML = html;
    }

    function renderBurners(){
      const p = getPayload(TOPICS.sts);
      const obj = safeObj(p);
      const val = safeObj(obj?.value);

      const ts = state.byTopic[d.topic]?.received_at ?? obj?.timestamp ?? "";

      const timeLine = ts ? fmtTime(ts) : "--:--:--";

      const burners = [
        {k:"burner1", name:"Burner #1", sub:"Primary"},
        {k:"burner3", name:"Burner #3", sub:"Secondary"},
        {k:"burner4", name:"Burner #4", sub:"Tertiary"},
        {k:"burner6", name:"Burner #6", sub:"Quaternary"},
      ];

      let html = "";
      for(const b of burners){
        const raw = val ? val[b.k] : null;
        const on = (raw === 1 || raw === true || raw === "1" || raw === "on" || raw === "ON");
        html += `
          <div class="burner ${on ? "on" : ""}">
            <div>
              <div class="bname">${b.name}</div>
              <div class="bsub">${b.sub} • Updated: ${timeLine}</div>
            </div>
            <div class="badge ${on ? "on" : "off"}">${on ? "ON" : "OFF"}</div>
          </div>
        `;
      }
      burnerRow.innerHTML = html;
    }

    function renderVFD(){
      const p = getPayload(TOPICS.vfd);
      const obj = safeObj(p);
      const val = safeObj(obj?.value);

      const unit = obj?.unit || "Hz";
      const ts = state.byTopic[d.topic]?.received_at ?? obj?.timestamp ?? "";

      const sub = ts ? ("Updated: " + fmtTime(ts)) : "Waiting for data...";

      /* IMPORTANT: match your real JSON keys exactly */
      const keys = [
        ["Bucket", "BUCKET"],
        ["INLETScrew", "INLET SCREW"],
        ["air locker", "AIR LOCKER"],
        ["exaust1", "EXHAUST 1"],
        ["exaust2", "EXHAUST 2"],
        ["outscrew1", "OUTSCREW 1"],
        ["outscrew2", "OUTSCREW 2"],
        ["reactor", "REACTOR"],
        ["syn gas", "SYNGAS"],
      ];

      let html = "";
      for(const [k, label] of keys){
        const v = (val && (k in val)) ? val[k] : "--";
        const num = Number(v);
        const pct = isFinite(num) ? Math.max(5, Math.min(100, (num/100)*100)) : 15;
        html += cardHTML(label, "VFD", v, unit, sub, pct);
      }
      vfdCards.innerHTML = html;
    }

    function renderAll(){
      renderTemps();
      renderPower();
      renderBurners();
      renderVFD();

      if(state.lastUpdate){
        lastUpdate.textContent = fmtTime(state.lastUpdate);
      }
    }

    function pushDebug(rec){
      const line = JSON.stringify(rec, null, 2);
      debug.textContent = (line + "\n\n" + debug.textContent).slice(0, 9000);
    }

    async function refreshAll(){
      const r = await fetch("/api/state");
      const data = await r.json();
      setConnected(!!data.mqtt.connected);

      state.lastUpdate = data.last_update_at;
      state.byTopic = data.by_topic || {};
      renderAll();
    }

    const socketioClient = io({ path: "/socket.io", transports: ["websocket","polling"] });

    socketioClient.on("connect", () => {
      refreshAll();
    });

    socketioClient.on("disconnect", () => {
      setConnected(false);
    });

    socketioClient.on("mqtt_message", (rec) => {
      state.byTopic[rec.topic] = rec;
      state.lastUpdate = rec.received_at;
      setConnected(true);
      renderAll();
      pushDebug(rec);
    });

    refreshAll();
    setInterval(refreshAll, 5000);
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


@app.get("/api/state")
def api_state():
    with lock:
        return jsonify(
            {
                "ok": True,
                "mqtt": MQTT_STATUS,
                "last_update_at": LAST_UPDATE_AT,
                "by_topic": LAST_BY_TOPIC,
            }
        )


# =====================================================
# LOCAL RUN (Render uses gunicorn)
# =====================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    socketio.run(app, host="0.0.0.0", port=port)
