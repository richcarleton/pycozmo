"""purrter lab - one process that owns Cozmo and serves a dark web dashboard.

Merged from:
  * purrter (le0):   motion watch -> YOLO cat check -> ntfy push, reconnect loop, Wi-Fi rejoin
  * cozmo-app:       PC-mic voice commands (Vosk), PC TTS out through Cozmo's speaker,
                     robot event hooks, head/lift controls
  * CozmoPod:        browser dashboard + MJPEG feed idea
Built on pycozmo 0.9 (display_image fix -> face flash is the cat-safe reaction).

Run:  python purrter.py      then open http://localhost:5055
"""
import io, os, sys, time, json, queue, random, logging, threading, subprocess, urllib.request, wave
from collections import deque
from datetime import datetime

import numpy as np
import pycozmo
from PIL import Image, ImageDraw, ImageFont, ImageChops, ImageStat, ImageOps
from flask import Flask, Response, jsonify, request, send_from_directory

# ================= settings (env vars override) =================
def env(name, default, cast=str):
    v = os.environ.get("PURRTER_" + name)
    return cast(v) if v is not None else default

HOST       = env("HOST", "127.0.0.1")       # "0.0.0.0" = whole LAN, no password!
PORT       = env("PORT", 5055, int)
TOPIC      = env("TOPIC", "purrter-mk-7f3q9x")   # ntfy topic; "" disables push
SSID       = env("SSID", "COZMO_5274F0")
IFACE      = env("IFACE", "Wi-Fi")
DOOR_HEAD  = env("DOOR_HEAD", -0.35, float)  # head angle in doorman mode (floor ~ -0.44)
THRESH     = env("THRESH", 1.5, float)       # % of frame changed = motion
CAT_CONF   = env("CAT_CONF", 0.25, float)
VOLUME     = env("VOLUME", 40000, int)
VOSK_MODEL = env("VOSK_MODEL", "")           # folder of a Vosk model; "" = auto-search
COOLDOWN, BURST, CAT_HOLD = 10, 3, 60
ANIMALS = {"cat", "dog", "horse", "sheep", "cow", "bear", "teddy bear"}

BASE = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE)
for d in ("events/cats", "tmp"):
    os.makedirs(d, exist_ok=True)

# ================= shared state =================
S = {
    "cli": None, "connected": False, "mode": "doorman",   # doorman | play
    "n": 0, "prev": None, "last_img": None, "peak": 0.0,
    "last_evt": 0.0, "mute_until": 0.0, "burst_until": 0.0,
    "cat_until": 0.0, "last_hit": 0.0, "last_cat": None,
    "drive_until": 0.0, "voice": False, "voice_ok": None, "heard": "",
    "yolo": "loading",
}
bot = threading.RLock()          # every Cozmo command goes through this
checks = queue.Queue(maxsize=20)
speech = queue.Queue(maxsize=5)
events = deque(maxlen=80)

def log(msg):
    line = f"{datetime.now():%H:%M:%S}  {msg}"
    print(line, flush=True)
    events.appendleft(line)

def cmd(fn, *a, **k):
    """Run a Cozmo command if connected. Returns False when he isn't there."""
    cli = S["cli"]
    if cli is None:
        return False
    with bot:
        fn(cli, *a, **k)
    return True

# ================= face =================
def _font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                        # Pillow < 10.1
        return ImageFont.load_default()

def face_cat():
    im = Image.new("1", (128, 32), 0)
    d = ImageDraw.Draw(im)
    d.ellipse((44, 8, 76, 31), outline=1, width=2)
    d.polygon([(46, 14), (50, 2), (57, 10)], fill=1)
    d.polygon([(74, 14), (70, 2), (63, 10)], fill=1)
    d.ellipse((52, 16, 56, 20), fill=1)
    d.ellipse((64, 16, 68, 20), fill=1)
    d.line((58, 24, 62, 24), fill=1)
    for y in (22, 26):
        d.line((36, y, 50, 23), fill=1)
        d.line((70, 23, 84, y), fill=1)
    d.text((90, 10), "MEOW", fill=1, font=_font(11))
    return im

def face_text(text):
    text = text.strip()[:40] or " "
    im = Image.new("1", (128, 32), 0)
    d = ImageDraw.Draw(im)
    words = text.split()
    # candidate layouts: one line, then every two-line split; biggest font that fits wins
    layouts = [[text]] + [[" ".join(words[:i]), " ".join(words[i:])] for i in range(1, len(words))]
    for size in (22, 18, 14, 12, 10, 9):
        f = _font(size)
        for lines in layouts:
            boxes = [d.textbbox((0, 0), ln, font=f) for ln in lines]
            lh = max(b - t for _, t, _, b in boxes)
            if max(r - l for l, _, r, _ in boxes) <= 126 and lh * len(lines) + 2 * (len(lines) - 1) <= 31:
                y = (32 - (lh * len(lines) + 2 * (len(lines) - 1))) // 2
                for ln, (l, t, r, b) in zip(lines, boxes):
                    d.text(((128 - (r - l)) // 2 - l, y - t), ln, fill=1, font=f)
                    y += lh + 2
                return im
    d.text((1, 10), text[:24], fill=1, font=_font(9))   # last resort: clip
    return im

def show_face(im, secs=4.0):
    return cmd(lambda c: c.display_image(im, duration=secs))

# ================= speech out (PC TTS -> Cozmo speaker) =================
def to_cozmo_wav(src, dst):
    """Any 8/16-bit PCM wav -> 22050 Hz mono 16-bit, the format pycozmo plays."""
    with wave.open(src, "rb") as w:
        ch, sw, rate, n = w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()
        raw = w.readframes(n)
    a = (np.frombuffer(raw, np.uint8).astype(np.float32) - 128) * 256 if sw == 1 \
        else np.frombuffer(raw, np.int16).astype(np.float32)
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate != 22050 and len(a):
        a = np.interp(np.linspace(0, len(a) - 1, int(len(a) * 22050 / rate)), np.arange(len(a)), a)
    with wave.open(dst, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)
        w.writeframes(np.clip(a, -32768, 32767).astype(np.int16).tobytes())

def speech_worker():
    try:
        import pyttsx3                       # Windows SAPI voices; Linux espeak; Mac nsss
        engine = pyttsx3.init()
        engine.setProperty("rate", 160)
    except Exception as e:
        log(f"tts unavailable: {e}")
        return
    while True:
        text = speech.get()
        try:
            raw, out = os.path.join(BASE, "tmp", "say_raw.wav"), os.path.join(BASE, "tmp", "say.wav")
            engine.save_to_file(text, raw)
            engine.runAndWait()
            to_cozmo_wav(raw, out)
            if cmd(lambda c: (c.set_volume(VOLUME), c.play_audio(out))):
                log(f"said: {text}")
        except Exception as e:
            log(f"say failed: {e}")

def say(text):
    try:
        speech.put_nowait(text)
    except queue.Full:
        log("speech queue full")

# ================= voice in (PC mic -> Vosk) =================
def find_vosk_model():
    cands = [VOSK_MODEL, "vosk-model", "../vosk-model-small-en-us-0.15",
             "../../cozmo-app/vosk-model-small-en-us-0.15"]
    for c in cands:
        if c and os.path.isdir(c):
            return c
    return None

def handle_phrase(text):
    S["heard"] = text
    t = text.replace("cosmo", "cozmo")
    if "time" in t:
        say(datetime.now().strftime("It's %I:%M %p").replace(" 0", " "))
    elif "hello" in t or "hi cozmo" in t:
        say("Hello!"); show_face(face_text("HI :)"), 3)
    elif "cat" in t or "kitty" in t:
        show_face(face_cat(), 4)
    elif "look up" in t:
        cmd(lambda c: c.set_head_angle(0.6))
    elif "look down" in t:
        cmd(lambda c: c.set_head_angle(-0.4))
    elif "battery" in t:
        cli = S["cli"]
        if cli:
            say(f"Battery is {cli.battery_voltage:.1f} volts")

def voice_worker():
    try:
        import sounddevice as sd
        from vosk import Model, KaldiRecognizer, SetLogLevel
    except Exception as e:
        S["voice_ok"] = False
        log(f"voice unavailable (pip install vosk sounddevice): {e}")
        return
    path = find_vosk_model()
    if not path:
        S["voice_ok"] = False
        log("voice: no Vosk model found - set PURRTER_VOSK_MODEL")
        return
    SetLogLevel(-1)
    rec = KaldiRecognizer(Model(path), 16000)
    S["voice_ok"] = True
    log(f"voice ready ({os.path.basename(path)})")
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16", channels=1) as stream:
        while True:
            data, _ = stream.read(4000)
            if not S["voice"]:
                continue
            if rec.AcceptWaveform(bytes(data)):
                text = json.loads(rec.Result()).get("text", "")
                if text:
                    log(f"heard: {text}")
                    handle_phrase(text)

# ================= doorman: motion -> YOLO -> push + face =================
def notify(path, conf):
    if not TOPIC:
        return
    def send():
        try:
            with open(path, "rb") as f:
                req = urllib.request.Request(
                    f"https://ntfy.sh/{TOPIC}", data=f.read(), method="PUT",
                    headers={"Title": "Kitty at the door", "Tags": "cat",
                             "Filename": "kitty.jpg", "Message": f"confidence {conf:.2f}"})
                urllib.request.urlopen(req, timeout=10)
        except Exception as e:
            log(f"notify failed: {e}")
    threading.Thread(target=send, daemon=True).start()

def prep(image):
    img = image.convert("RGB")
    img = img.resize((img.width * 2, img.height * 2))
    img = img.point(lambda p: int(255 * (p / 255) ** 0.6))
    return ImageOps.autocontrast(img, cutoff=2)

def cat_worker():
    try:
        from ultralytics import YOLO
        model = YOLO("yolo11s.pt")
        S["yolo"] = "ready"
        log("yolo ready")
    except Exception as e:
        S["yolo"] = "off"
        log(f"yolo unavailable, motion only ({e.__class__.__name__})")
        return
    while True:
        image, stamp = checks.get()
        if time.time() < S["cat_until"]:
            continue
        try:
            res = model(prep(image), conf=0.15, verbose=False)[0]
            seen = [(res.names[int(c)], float(p)) for c, p in zip(res.boxes.cls, res.boxes.conf)]
            cats = [p for n, p in seen if n in ANIMALS and p >= CAT_CONF]
            if not cats:
                continue
            if time.time() - S["last_hit"] < 5:          # 2nd hit = confirmed
                S["cat_until"] = time.time() + CAT_HOLD
                name = f"events/cats/{stamp}.jpg"
                res.save(filename=name)
                S["last_cat"] = datetime.now().strftime("%H:%M:%S")
                log(f"CAT {max(cats):.2f}")
                notify(name, max(cats))
                show_face(face_cat(), 5)
            S["last_hit"] = time.time()
        except Exception as e:
            log(f"yolo error: {e}")

def on_frame(cli, image):
    try:
        _on_frame(image)
    except Exception as e:
        log(f"frame error: {e}")

def _on_frame(image):
    S["n"] += 1
    S["last_img"] = image
    now = time.time()
    if S["mode"] != "doorman" or S["n"] < 20 or now < S["mute_until"]:
        S["prev"] = None
        return
    if S["yolo"] == "ready" and now > S["cat_until"]:
        if (now < S["burst_until"] and S["n"] % 5 == 0) or S["n"] % 30 == 0:
            try:
                checks.put_nowait((image.copy(), datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]))
            except queue.Full:
                pass
    small = image.convert("L").resize((80, 60))
    prev, S["prev"] = S["prev"], small
    if prev is None:
        return
    diff = ImageChops.difference(small, prev).point(lambda p: 255 if p > 15 else 0)
    score = ImageStat.Stat(diff).mean[0] / 255 * 100
    S["peak"] = max(S["peak"] * 0.95, score)
    if score > THRESH and now - S["last_evt"] > COOLDOWN:
        S["last_evt"] = now
        S["burst_until"] = now + BURST
        image.save(datetime.now().strftime("events/%Y%m%d-%H%M%S.jpg"))
        log(f"motion {score:.1f}%")

# ================= robot event hooks (from cozmo-app, actually registered this time) =================
def hook_events(cli):
    E, P = pycozmo.event, pycozmo.protocol_encoder
    def flag(label, on, off=None):
        return lambda c, state: log(on if state else off) if (state or off) else None
    cli.add_handler(E.EvtRobotPickedUpChange, flag("pick", "picked up", "put down"))
    cli.add_handler(E.EvtRobotChargingChange, flag("chg", "charging", "stopped charging"))
    cli.add_handler(E.EvtRobotFallingChange, flag("fall", "falling!"))
    def cliff(c, state):
        if state:
            log("cliff detected - wheels stopped")
            c.stop_all_motors()
    cli.add_handler(E.EvtCliffDetectedChange, cliff)
    cli.add_handler(P.RobotPoked, lambda c, pkt: log("poked"))
    cli.add_handler(P.ButtonPressed, lambda c, pkt: log("backpack button") if pkt.pressed else None)

# ================= connection =================
def ensure_wifi():
    if os.name != "nt":
        return True
    for _ in range(6):
        if subprocess.run(["ping", "-n", "1", "-w", "1000", "172.31.1.1"],
                          capture_output=True).returncode == 0:
            return True
        subprocess.run(["netsh", "wlan", "connect", f"name={SSID}", f"interface={IFACE}"],
                       capture_output=True)
        time.sleep(5)
    return False

def apply_mode(cli):
    S["mute_until"] = time.time() + 3
    if S["mode"] == "doorman":
        cli.stop_all_motors()
        cli.set_head_angle(DOOR_HEAD)
        cli.set_lift_height(pycozmo.robot.MIN_LIFT_HEIGHT.mm)
    else:
        cli.set_head_angle(0.1)

def drive_watchdog():
    """Dead-man switch: no drive command for 0.4 s -> wheels stop."""
    moving = False
    while True:
        time.sleep(0.1)
        if time.time() < S["drive_until"]:
            moving = True
        elif moving:
            moving = False
            cmd(lambda c: c.stop_all_motors())

def run():
    with pycozmo.connect(log_level="WARNING") as cli:
        S["cli"], S["connected"], S["prev"] = cli, True, None
        log(f"connected - battery {cli.battery_voltage:.2f} V")
        hook_events(cli)
        with bot:
            cli.set_volume(VOLUME)
            apply_mode(cli)
            cli.enable_camera(enable=True, color=False)
        cli.add_handler(pycozmo.event.EvtNewRawCameraImage, on_frame)
        show_face(face_text("purrter"), 2)
        while True:
            time.sleep(random.uniform(45, 90))
            if S["mode"] == "doorman":                   # tiny fidget keeps him awake
                S["mute_until"] = time.time() + 2
                cmd(lambda c: c.set_head_angle(DOOR_HEAD + random.uniform(-0.05, 0.05)))

# ================= web =================
app = Flask(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

def body():
    return request.get_json(silent=True) or {}

def ok(done=True):
    return (jsonify(ok=True), 200) if done else (jsonify(ok=False, error="cozmo offline"), 503)

@app.route("/")
def index():
    return send_from_directory(BASE, "dashboard.html")

@app.route("/video")
def video():
    def gen():
        last = -1
        while True:
            img, n = S["last_img"], S["n"]
            if img is not None and n != last:
                last = n
                buf = io.BytesIO()
                img.convert("L").save(buf, "JPEG", quality=70)
                yield b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + buf.getvalue() + b"\r\n"
            time.sleep(0.07)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=f")

@app.route("/api/status")
def status():
    cli, st = S["cli"], {}
    if cli:
        F = pycozmo.robot.RobotStatusFlag
        f = cli.robot_status
        st = dict(battery=round(cli.battery_voltage, 2),
                  head=round(cli.head_angle.radians, 2),
                  lift=round(cli.lift_position.height.mm, 1),
                  charging=bool(f & F.IS_CHARGING), on_charger=bool(f & F.IS_ON_CHARGER),
                  picked_up=bool(f & F.IS_PICKED_UP), cliff=bool(f & F.CLIFF_DETECTED))
    cats = sorted(os.listdir("events/cats"), reverse=True)[:12]
    return jsonify(connected=S["connected"], mode=S["mode"], frames=S["n"], motion=round(S["peak"], 1),
                   yolo=S["yolo"], voice=S["voice"], voice_ok=S["voice_ok"], heard=S["heard"],
                   last_cat=S["last_cat"], robot=st, log=list(events), cats=cats,
                   limits=dict(head=[pycozmo.robot.MIN_HEAD_ANGLE.radians, pycozmo.robot.MAX_HEAD_ANGLE.radians],
                               lift=[pycozmo.robot.MIN_LIFT_HEIGHT.mm, pycozmo.robot.MAX_LIFT_HEIGHT.mm]))

@app.post("/api/mode")
def set_mode():
    m = body().get("mode")
    if m not in ("doorman", "play"):
        return jsonify(ok=False, error="mode must be doorman or play"), 400
    S["mode"] = m
    log(f"mode -> {m}")
    return ok(cmd(apply_mode) or True)

@app.post("/api/head")
def head():
    a = float(body().get("angle", 0))
    return ok(cmd(lambda c: c.set_head_angle(a)))

@app.post("/api/lift")
def lift():
    if S["mode"] != "play":
        return jsonify(ok=False, error="switch to play mode first"), 409
    h = float(body().get("mm", 32))
    return ok(cmd(lambda c: c.set_lift_height(h)))

@app.post("/api/drive")
def drive():
    if S["mode"] != "play":
        return jsonify(ok=False, error="switch to play mode first"), 409
    b = body()
    l = max(-150.0, min(150.0, float(b.get("l", 0))))
    r = max(-150.0, min(150.0, float(b.get("r", 0))))
    S["drive_until"] = time.time() + 0.4
    return ok(cmd(lambda c: c.drive_wheels(l, r)))

@app.post("/api/stop")
def stop():
    S["drive_until"] = 0
    return ok(cmd(lambda c: c.stop_all_motors()))

@app.post("/api/face")
def face():
    b = body()
    im = face_cat() if b.get("preset") == "cat" else face_text(str(b.get("text", "")))
    if b.get("preset") == "clear":
        return ok(cmd(lambda c: c.clear_screen()))
    return ok(show_face(im, float(b.get("secs", 4))))

@app.post("/api/say")
def say_api():
    text = str(body().get("text", "")).strip()[:200]
    if not text:
        return jsonify(ok=False, error="nothing to say"), 400
    say(text)
    return ok()

@app.post("/api/voice")
def voice_api():
    S["voice"] = bool(body().get("on"))
    log(f"voice listening {'on' if S['voice'] else 'off'}")
    return ok()

@app.route("/cats/<path:f>")
def cat_file(f):
    return send_from_directory(os.path.join(BASE, "events", "cats"), f)

# ================= main =================
def main():
    for target in (cat_worker, speech_worker, voice_worker, drive_watchdog):
        threading.Thread(target=target, daemon=True).start()
    threading.Thread(target=lambda: app.run(host=HOST, port=PORT, threaded=True, use_reloader=False),
                     daemon=True).start()
    print(f"dashboard: http://localhost:{PORT}", flush=True)
    while True:
        try:
            if not ensure_wifi():
                log("cozmo wifi not reachable - wiggle his lift?")
                time.sleep(30)
                continue
            run()
            break                                       # clean exit (Ctrl+C)
        except KeyboardInterrupt:
            break
        except Exception as e:
            log(f"dropped: {e} - retrying in 10s")
            time.sleep(10)
        finally:
            S["cli"], S["connected"] = None, False

if __name__ == "__main__":
    main()
