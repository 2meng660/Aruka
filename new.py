#!/usr/bin/env python3
import json
import time
import ssl
import threading
from datetime import datetime, timezone

from flask import Flask, jsonify
from flask_socketio import SocketIO
import paho.mqtt.client as mqtt

# =====================================================
# CONFIG
# =====================================================
MQTT_BROKER = "t569f61e.ala.asia-southeast1.emqxsl.com"
MQTT_PORT = 8883
MQTT_USERNAME = "KH-01-device"
MQTT_PASSWORD = "Radiation0-Disperser8-Sternum1-Trio4"

TOPIC_ALL = "KH/site-01/KH-01/#"

TOPIC_T1 = "KH/site-01/KH-01/temperature_probe1"
TOPIC_T2 = "KH/site-01/KH-01/temperature_probe2"
TOPIC_T3 = "KH/site-01/KH-01/temperature_probe3"
TOPIC_T4 = "KH/site-01/KH-01/temperature_probe4"
TOPIC_VFD = "KH/site-01/KH-01/vfd_frequency"
TOPIC_PWR = "KH/site-01/KH-01/power_consumption"

# =====================================================
# APP
# =====================================================
app = Flask(__name__)
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

# =====================================================
# STATE (LATEST VALUES)
# =====================================================
lock = threading.Lock()

STATE = {
    "mqtt": False,
    "last_ts": None,

    "temp": {
        "p1": None,
        "p2": None,
        "p3": None,
        "p4": None,
    },

    "vfd": {
        "Bucket": None,
        "INLETScrew": None,
        "air locker": None,
        "reactor": None,
    },

    "power": {
        "phaseA": None,
        "phaseB": None,
        "phaseC": None,
    }
}

def now_z():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

# =====================================================
# MQTT CALLBACKS
# =====================================================
def on_connect(client, userdata, flags, rc):
    with lock:
        STATE["mqtt"] = (rc == 0)
    client.subscribe(TOPIC_ALL)
    socketio.emit("mqtt", {"ok": rc == 0})
    print("[MQTT] connected rc=", rc)

def on_disconnect(client, userdata, rc):
    with lock:
        STATE["mqtt"] = False
    socketio.emit("mqtt", {"ok": False})
    print("[MQTT] disconnected")

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload.decode())
    except:
        return

    with lock:
        STATE["last_ts"] = now_z()

        if msg.topic == TOPIC_T1:
            STATE["temp"]["p1"] = data.get("value")
        elif msg.topic == TOPIC_T2:
            STATE["temp"]["p2"] = data.get("value")
        elif msg.topic == TOPIC_T3:
            STATE["temp"]["p3"] = data.get("value")
        elif msg.topic == TOPIC_T4:
            STATE["temp"]["p4"] = data.get("value")

        elif msg.topic == TOPIC_VFD:
            v = data.get("value", {})
            for k in STATE["vfd"]:
                if k in v:
                    STATE["vfd"][k] = v[k]

        elif msg.topic == TOPIC_PWR:
            v = data.get("value", {})
            for k in STATE["power"]:
                if k in v:
                    STATE["power"][k] = v[k]

    socketio.emit("update", STATE)

# =====================================================
# MQTT THREAD
# =====================================================
def mqtt_loop():
    cid = f"kh01-web-{int(time.time())}"
    client = mqtt.Client(client_id=cid, protocol=mqtt.MQTTv311)
    client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message

    print("[MQTT] connecting...")
    client.connect(MQTT_BROKER, MQTT_PORT, 60)
    client.loop_forever()

# =====================================================
# ROUTES
# =====================================================
@app.route("/")
def index():
    return DASH_HTML

@app.route("/api/state")
def api_state():
    with lock:
        return jsonify(STATE)
    bb

# =====================================================
# EMBEDDED UI (SINGLE FILE)
# =====================================================
DASH_HTML = """
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>KH-01 Dashboard</title>
<script src="https://cdn.socket.io/4.7.5/socket.io.min.js"></script>
<style>
body{margin:0;background:#0b1020;color:#e5e7eb;font-family:Segoe UI}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px;padding:24px}
.card{background:#121a2f;border-radius:18px;padding:20px;box-shadow:0 20px 40px #0008}
h2{margin:0 0 10px 0;font-size:18px}
.row{display:flex;justify-content:space-between;
     padding:14px;border-radius:14px;background:#0f172a;margin-bottom:10px}
.label{color:#9ca3af}
.value{font-size:20px;font-weight:700}
.top{padding:16px 24px;display:flex;justify-content:space-between}
.dot{width:10px;height:10px;border-radius:50%;background:red}
.dot.ok{background:#22c55e}
</style>
</head>

<body>
<div class="top">
  <h1>KH-01 Monitoring</h1>
  <div><span id="dot" class="dot"></span> MQTT</div>
</div>

<div class="grid">
  <div class="card">
    <h2>Temperature Sensors</h2>
    <div class="row"><span class="label">Probe 1 (Reactor)</span><span id="t1" class="value">-- C</span></div>
    <div class="row"><span class="label">Probe 2 (Burner1)</span><span id="t2" class="value">-- C</span></div>
    <div class="row"><span class="label">Probe 3 (Burner2)</span><span id="t3" class="value">-- C</span></div>
    <div class="row"><span class="label">Probe 4 (Outcreew)</span><span id="t4" class="value">-- C</span></div>
  </div>

  <div class="card">
    <h2>VFD Frequency</h2>
    <div class="row"><span class="label">Bucket</span><span id="v1" class="value">-- Hz</span></div>
    <div class="row"><span class="label">INLET Screw</span><span id="v2" class="value">-- Hz</span></div>
    <div class="row"><span class="label">Air Locker</span><span id="v3" class="value">-- Hz</span></div>
    <div class="row"><span class="label">Reactor</span><span id="v4" class="value">-- Hz</span></div>
  </div>

  <div class="card">
    <h2>Power Consumption</h2>
    <div class="row"><span class="label">Phase A</span><span id="p1" class="value">-- A</span></div>
    <div class="row"><span class="label">Phase B</span><span id="p2" class="value">-- A</span></div>
    <div class="row"><span class="label">Phase C</span><span id="p3" class="value">-- A</span></div>
  </div>
</div>

<script>
const s = io();
s.on("mqtt", d=>{
  document.getElementById("dot").classList.toggle("ok", d.ok);
});
s.on("update", d=>{
  document.getElementById("t1").innerText = (d.temp.p1 ?? "--")+" C";
  document.getElementById("t2").innerText = (d.temp.p2 ?? "--")+" C";
  document.getElementById("t3").innerText = (d.temp.p3 ?? "--")+" C";
  document.getElementById("t4").innerText = (d.temp.p4 ?? "--")+" C";

  document.getElementById("v1").innerText = (d.vfd["Bucket"] ?? "--")+" Hz";
  document.getElementById("v2").innerText = (d.vfd["INLETScrew"] ?? "--")+" Hz";
  document.getElementById("v3").innerText = (d.vfd["air locker"] ?? "--")+" Hz";
  document.getElementById("v4").innerText = (d.vfd["reactor"] ?? "--")+" Hz";

  document.getElementById("p1").innerText = (d.power.phaseA ?? "--")+" A";
  document.getElementById("p2").innerText = (d.power.phaseB ?? "--")+" A";
  document.getElementById("p3").innerText = (d.power.phaseC ?? "--")+" A";
});
</script>
</body>
</html>
"""

# =====================================================
# MAIN
# =====================================================
if __name__ == "__main__":
    threading.Thread(target=mqtt_loop, daemon=True).start()
    print("Dashboard: http://127.0.0.1:5000")
    socketio.run(app, host="0.0.0.0", port=5000, debug=True, use_reloader=False)
