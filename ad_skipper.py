#!/usr/bin/env python3
"""
ad_skipper.py — automatically skip commercials in YouTube TV recordings on a Vizio TV.

How it works
------------
A camera aimed at the TV watches one small area of the screen (the football
score bug, or a network logo). When that graphic disappears for a few seconds,
the program assumes a commercial break has started and presses the TV's
"skip forward" button over Wi-Fi, one step at a time, checking after each step
whether the graphic is back. When it is, it backs up one step so you don't miss
anything, then goes back to watching.

Commands
--------
  python3 ad_skipper.py pair --ip 192.168.1.50      # one-time: pair with the TV
  python3 ad_skipper.py test-key right              # see what a button does
  python3 ad_skipper.py preview                     # live camera view, for aiming
  python3 ad_skipper.py calibrate --name football   # teach it a graphic
  python3 ad_skipper.py watch                       # live detection readout
  python3 ad_skipper.py run                         # the real thing
  python3 ad_skipper.py set detect.trigger_seconds 12   # change a setting
"""

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
TEMPLATE_DIR = os.path.join(HERE, "templates")

DEFAULT_CONFIG = {
    "tv": {"ip": "", "port": 7345, "auth": ""},
    "camera": {"index": 0, "width": 1280, "height": 720},
    "skip": {
        # Which remote button jumps forward / back in YouTube TV on your TV.
        # Use `test-key` to find the right ones (see README).
        "forward_key": "right",
        "back_key": "left",
        "step_seconds": 15,          # how far one forward press jumps
        "settle_seconds": 1.5,       # wait after a press before looking again
        "back_up_after": True,       # step back once when the show returns
        "resume_key": "",            # pressed when done skipping, e.g. "play" if
                                     # the video is left paused; "" = don't
        "max_skip_seconds": 240,     # never skip more than this in one break
        # On some TVs the first press only brings up the on-screen controls,
        # and only later presses jump. If so, send one extra "wake" press when
        # the controls have probably hidden (no press for hud_timeout_seconds).
        "wake_first": True,
        "hud_timeout_seconds": 3.0,
    },
    "detect": {
        "trigger_seconds": 12.0,     # graphic must be gone this long to count as an ad
        "min_present_seconds": 10.0, # graphic must have been visible this long first
        "cooldown_seconds": 20.0,    # ignore disappearances right after a skip
        "fps": 5,
    },
    "profiles": [],
}

# Vizio SmartCast remote key codes (codeset, code)
KEYS = {
    "up": (3, 8), "down": (3, 0), "left": (3, 1), "right": (3, 7), "ok": (3, 2),
    "back": (4, 0), "home": (4, 3), "menu": (4, 8), "exit": (9, 0),
    "seek_fwd": (2, 0), "seek_back": (2, 1), "pause": (2, 2), "play": (2, 3),
}


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# --------------------------------------------------------------------------- config

def load_config():
    if not os.path.exists(CONFIG_PATH):
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # fill in any missing defaults
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            cfg.setdefault(k, {})
            for kk, vv in v.items():
                cfg[k].setdefault(kk, vv)
        else:
            cfg.setdefault(k, v)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


# --------------------------------------------------------------------------- TV control

class Vizio:
    def __init__(self, ip, port=7345, auth=""):
        self.base = f"https://{ip}:{port}"
        self.auth = auth
        self.last_press = 0.0

    def _put(self, path, body, auth=True):
        headers = {"Content-Type": "application/json"}
        if auth and self.auth:
            headers["AUTH"] = self.auth
        r = requests.put(self.base + path, json=body, headers=headers,
                         verify=False, timeout=4)
        return r.json()

    def press(self, key_name):
        codeset, code = KEYS[key_name]
        self.last_press = time.time()
        return self._put("/key_command/", {
            "KEYLIST": [{"CODESET": codeset, "CODE": code, "ACTION": "KEYPRESS"}]
        })

    def pair(self, device_id="ad-skipper-pi", name="Ad Skipper"):
        res = self._put("/pairing/start",
                        {"DEVICE_ID": device_id, "DEVICE_NAME": name}, auth=False)
        item = res.get("ITEM") or {}
        token = item.get("PAIRING_REQ_TOKEN")
        challenge = item.get("CHALLENGE_TYPE", 1)
        if token is None:
            raise RuntimeError(f"TV refused pairing: {res.get('STATUS')}")
        pin = input("Enter the PIN shown on the TV: ").strip()
        res = self._put("/pairing/pair", {
            "DEVICE_ID": device_id, "CHALLENGE_TYPE": challenge,
            "RESPONSE_VALUE": pin, "PAIRING_REQ_TOKEN": token,
        }, auth=False)
        auth = (res.get("ITEM") or {}).get("AUTH_TOKEN")
        if not auth:
            raise RuntimeError(f"Pairing failed: {res.get('STATUS')}")
        self.auth = auth
        return auth


# --------------------------------------------------------------------------- camera

class Camera:
    """Uses the Pi Camera Module if available, otherwise a USB webcam."""

    def __init__(self, index=0, width=1280, height=720):
        self.picam = None
        self.cap = None
        try:
            from picamera2 import Picamera2  # type: ignore
            self.picam = Picamera2()
            self.picam.configure(self.picam.create_video_configuration(
                main={"size": (width, height), "format": "RGB888"}))
            self.picam.start()
            time.sleep(1.0)
        except Exception:
            self.picam = None
            self.cap = cv2.VideoCapture(index)
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            if not self.cap.isOpened():
                sys.exit("Could not open a camera. Is it plugged in?")
            for _ in range(10):  # let auto-exposure settle
                self.cap.read()

    def read(self):
        if self.picam:
            return self.picam.capture_array()  # BGR order for OpenCV
        ok, frame = self.cap.read()
        if not ok:
            raise RuntimeError("Camera read failed")
        return frame


def to_gray(img):
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return cv2.GaussianBlur(g, (3, 3), 0)


# --------------------------------------------------------------------------- detection

class Detector:
    def __init__(self, profiles):
        if not profiles:
            sys.exit("No graphics calibrated yet. Run: python3 ad_skipper.py calibrate --name football")
        self.items = []
        for p in profiles:
            if not p.get("enabled", True):
                continue
            tmpl = cv2.imread(os.path.join(TEMPLATE_DIR, p["template"]), cv2.IMREAD_GRAYSCALE)
            if tmpl is None:
                log(f"warning: template for '{p['name']}' missing, skipping")
                continue
            self.items.append((p, tmpl))

    def scores(self, frame):
        gray = to_gray(frame)
        out = []
        for p, tmpl in self.items:
            x, y, w, h = p["search"]
            region = gray[y:y + h, x:x + w]
            if region.shape[0] < tmpl.shape[0] or region.shape[1] < tmpl.shape[1]:
                out.append((p["name"], 0.0, p["threshold"]))
                continue
            res = cv2.matchTemplate(region, tmpl, cv2.TM_CCOEFF_NORMED)
            out.append((p["name"], float(res.max()), p["threshold"]))
        return out

    def present(self, frame):
        return any(s >= t for _, s, t in self.scores(frame))


def check_present(cam, det, seconds=0.8, fps=8, need=0.6):
    """Look for a short window and vote, so one bad frame doesn't fool us."""
    hits = total = 0
    end = time.time() + seconds
    while time.time() < end:
        total += 1
        hits += det.present(cam.read())
        time.sleep(1.0 / fps)
    return total > 0 and hits / total >= need


def avg_score(cam, det_items, seconds=2.0):
    vals = []
    end = time.time() + seconds
    while time.time() < end:
        gray = to_gray(cam.read())
        (p, tmpl) = det_items
        x, y, w, h = p["search"]
        res = cv2.matchTemplate(gray[y:y + h, x:x + w], tmpl, cv2.TM_CCOEFF_NORMED)
        vals.append(float(res.max()))
        time.sleep(0.1)
    return float(np.median(vals))


# --------------------------------------------------------------------------- commands

def cmd_pair(args, cfg):
    tv = Vizio(args.ip, args.port)
    log(f"Asking the TV at {args.ip}:{args.port} to pair. Watch the TV for a PIN...")
    auth = tv.pair()
    cfg["tv"].update({"ip": args.ip, "port": args.port, "auth": auth})
    save_config(cfg)
    log("Paired! Saved to config.json.")


def cmd_set(args, cfg):
    try:
        section, key = args.setting.split(".")
        old = cfg[section][key]
    except (ValueError, KeyError):
        names = [f"{s}.{k}" for s, v in DEFAULT_CONFIG.items() if isinstance(v, dict)
                 for k in v if s != "tv"]
        sys.exit("Unknown setting. Choose from:\n  " + "\n  ".join(names))
    if isinstance(old, bool):
        new = args.value.lower() in ("true", "yes", "on", "1")
    elif isinstance(old, (int, float)):
        try:
            new = type(old)(float(args.value))
        except ValueError:
            sys.exit(f"{args.setting} needs a number.")
    else:
        new = args.value
        if key.endswith("_key") and new and new not in KEYS:
            sys.exit(f"Unknown button. Choose from: {', '.join(sorted(KEYS))}")
    cfg[section][key] = new
    save_config(cfg)
    log(f"{args.setting}: {old} -> {new}")


def cmd_test_key(args, cfg):
    t = cfg["tv"]
    if not t["auth"]:
        sys.exit("Pair with the TV first.")
    tv = Vizio(t["ip"], t["port"], t["auth"])
    for i in range(args.times):
        log(f"pressing '{args.key}' -> {tv.press(args.key).get('STATUS', {}).get('RESULT')}")
        time.sleep(args.gap)


def cmd_preview(args, cfg):
    c = cfg["camera"]
    cam = Camera(c["index"], c["width"], c["height"])
    log("Showing the camera. Click the window and press q (or Esc) to close.")
    try:
        while True:
            frame = cam.read()
            # outline the areas already calibrated, so you can check the aim
            for p in cfg["profiles"]:
                x, y, w, h = p["search"]
                cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
                cv2.putText(frame, p["name"], (x, max(15, y - 6)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 0), 2)
            cv2.imshow("Camera preview", frame)
            if cv2.waitKey(30) & 0xFF in (ord("q"), 27):
                break
    except cv2.error:
        sys.exit("No screen available to show the camera. Run this from the Pi's desktop.")
    cv2.destroyAllWindows()


def cmd_calibrate(args, cfg):
    c = cfg["camera"]
    cam = Camera(c["index"], c["width"], c["height"])
    os.makedirs(TEMPLATE_DIR, exist_ok=True)

    input("\nStep 1: Play a recording where the graphic (score bug / logo) is ON screen.\n"
          "Press Enter when it's showing... ")
    frame = cam.read()
    snap = os.path.join(HERE, "snapshot.jpg")
    cv2.imwrite(snap, frame)

    if args.roi:
        x, y, w, h = [int(v) for v in args.roi.split(",")]
    else:
        try:
            log("Drag a box tightly around the graphic, then press Enter.")
            x, y, w, h = [int(v) for v in cv2.selectROI("Select graphic", frame, False)]
            cv2.destroyAllWindows()
        except cv2.error:
            sys.exit(f"No screen available to draw a box. Open {snap}, find the graphic's "
                     "pixel box, and re-run with --roi x,y,width,height")
    if w < 10 or h < 10:
        sys.exit("Box too small, try again.")

    tmpl = to_gray(frame)[y:y + h, x:x + w]
    tmpl_name = f"{args.name}.png"
    cv2.imwrite(os.path.join(TEMPLATE_DIR, tmpl_name), tmpl)

    # search area = the box plus a margin, so a slightly bumped camera still works
    mx, my = int(w * 0.3) + 10, int(h * 0.3) + 10
    H, W = frame.shape[:2]
    sx, sy = max(0, x - mx), max(0, y - my)
    search = [sx, sy, min(W, x + w + mx) - sx, min(H, y + h + my) - sy]

    profile = {"name": args.name, "template": tmpl_name, "search": search,
               "threshold": 0.6, "enabled": True}
    item = (profile, tmpl)

    log("Measuring with graphic visible (keep it playing)...")
    on = avg_score(cam, item)
    input("\nStep 2: Now go to a commercial (graphic GONE). Press Enter when an ad is playing... ")
    log("Measuring with graphic gone...")
    off = avg_score(cam, item)

    profile["threshold"] = round((on + off) / 2, 3)
    log(f"visible score {on:.2f}, gone score {off:.2f} -> threshold {profile['threshold']}")
    if on - off < 0.25:
        log("WARNING: those are close together. Try a tighter box, better camera aim, "
            "or less glare on the screen, then calibrate again.")

    cfg["profiles"] = [p for p in cfg["profiles"] if p["name"] != args.name] + [profile]
    save_config(cfg)
    log(f"Saved profile '{args.name}'.")


def cmd_watch(args, cfg):
    c = cfg["camera"]
    cam = Camera(c["index"], c["width"], c["height"])
    det = Detector(cfg["profiles"])
    log("Live readout (Ctrl+C to stop). Score above threshold = graphic visible.")
    while True:
        parts = [f"{n}: {s:.2f}/{t:.2f} {'SEEN' if s >= t else '----'}"
                 for n, s, t in det.scores(cam.read())]
        print("  |  ".join(parts), flush=True)
        time.sleep(0.3)


def press_jump(tv, key, sk):
    """Press a jump key, first waking the on-screen controls if they've hidden."""
    if sk["wake_first"] and time.time() - tv.last_press > sk["hud_timeout_seconds"]:
        tv.press(key)
        time.sleep(0.5)
    tv.press(key)


def skip_break(tv, cam, det, sk):
    skipped = 0
    while skipped < sk["max_skip_seconds"]:
        press_jump(tv, sk["forward_key"], sk)
        skipped += sk["step_seconds"]
        time.sleep(sk["settle_seconds"])
        if check_present(cam, det):
            if sk["back_up_after"]:
                press_jump(tv, sk["back_key"], sk)
            resume(tv, sk)
            log(f"show is back — skipped about {skipped}s")
            return True
    resume(tv, sk)
    log(f"gave up after {skipped}s without seeing the graphic; watching again")
    return False


def resume(tv, sk):
    if sk["resume_key"]:
        time.sleep(0.5)
        tv.press(sk["resume_key"])


def cmd_run(args, cfg):
    t, c, sk, d = cfg["tv"], cfg["camera"], cfg["skip"], cfg["detect"]
    if not t["auth"]:
        sys.exit("Pair with the TV first.")
    tv = Vizio(t["ip"], t["port"], t["auth"])
    cam = Camera(c["index"], c["width"], c["height"])
    det = Detector(cfg["profiles"])

    present_since = None   # when the graphic most recently appeared
    absent_since = None    # when it most recently disappeared
    armed = False          # True once the graphic has been steadily visible
    cooldown_until = 0.0
    log("Ad skipper running. Ctrl+C to stop.")

    while True:
        now = time.time()
        if det.present(cam.read()):
            absent_since = None
            present_since = present_since or now
            if not armed and now - present_since >= d["min_present_seconds"]:
                armed = True
                log("graphic steady — armed")
        else:
            present_since = None
            absent_since = absent_since or now
            if (armed and now >= cooldown_until
                    and now - absent_since >= d["trigger_seconds"]):
                log("graphic gone — looks like a commercial, skipping")
                ok = skip_break(tv, cam, det, sk)
                cooldown_until = time.time() + d["cooldown_seconds"]
                absent_since = None
                if not ok:
                    armed = False  # wait until the show is clearly back
        time.sleep(1.0 / d["fps"])


def main():
    ap = argparse.ArgumentParser(description="Auto-skip commercials on a Vizio TV")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("pair", help="pair with the TV (one time)")
    p.add_argument("--ip", required=True)
    p.add_argument("--port", type=int, default=7345)

    p = sub.add_parser("test-key", help="press a remote button")
    p.add_argument("key", choices=sorted(KEYS))
    p.add_argument("--times", type=int, default=1)
    p.add_argument("--gap", type=float, default=1.0)

    sub.add_parser("preview", help="live camera view, for aiming the camera")

    p = sub.add_parser("calibrate", help="teach it a graphic to watch for")
    p.add_argument("--name", required=True, help="e.g. football, hgtv, bravo")
    p.add_argument("--roi", help="x,y,width,height if no screen is attached")

    sub.add_parser("watch", help="live detection readout")
    sub.add_parser("run", help="start auto-skipping")

    p = sub.add_parser("set", help="change a setting, e.g. set detect.trigger_seconds 12")
    p.add_argument("setting")
    p.add_argument("value")

    args = ap.parse_args()
    cfg = load_config()
    {"pair": cmd_pair, "test-key": cmd_test_key, "preview": cmd_preview, "calibrate": cmd_calibrate,
     "watch": cmd_watch, "run": cmd_run, "set": cmd_set}[args.cmd](args, cfg)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
