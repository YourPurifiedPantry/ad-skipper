# Ad Skipper for YouTube TV on a Vizio TV

A Raspberry Pi with a camera watches your TV. When the football score bug or a
network's corner logo disappears for a few seconds, the Pi presses "skip
forward" on the TV over your Wi-Fi, step by step, until the graphic comes back.
Then it backs up one step so you don't miss the snap.

## Parts list (about $80–120 total)

| Part | Notes |
|---|---|
| Raspberry Pi 4 (2 GB+) or Pi 5 | Either works; the Pi 5 is a bit snappier |
| Official power supply for that Pi | Underpowered Pis act flaky |
| 32 GB microSD card | |
| Case | Any |
| **Camera, pick one:** Pi Camera Module 3, *or* any basic 720p/1080p USB webcam | The webcam is simpler to aim and mount |
| Small tripod, gooseneck clip, or flexible mount | Aim the camera at the TV and keep it steady |

The camera can sit on a shelf or coffee table. It only needs a clear,
glare-free view of the part of the screen where the graphic appears.

## 1. Set up the Pi

1. Flash **Raspberry Pi OS (64-bit, with desktop)** with Raspberry Pi Imager.
   Set your Wi-Fi in the imager's settings.
2. Copy `ad_skipper.py` and this README into a folder, e.g. `~/adskipper`.
3. Install the extras:
   ```
   sudo apt update
   sudo apt install -y python3-opencv python3-requests
   ```
   (The Pi Camera library, `picamera2`, already comes with Raspberry Pi OS.)

During setup, use a monitor or VNC with the Pi so you can draw the box in
step 4. After that it runs without a screen.

## 2. Pair with the TV

1. Find the TV's IP address: on the Vizio, go to **Menu → Network → Network
   Information** (wording varies by model).
2. Turn the TV on, then run:
   ```
   cd ~/adskipper
   python3 ad_skipper.py pair --ip 192.168.1.50
   ```
3. Type in the PIN the TV shows.

If nothing happens, try `--port 9000` (older Vizio firmware). The TV must be
on, not asleep, and on the same network as the Pi.

## 3. Find the right skip buttons

YouTube TV's skip behavior can differ between TV models, so test it. Start a
recording, then try:
```
python3 ad_skipper.py test-key right
python3 ad_skipper.py test-key right --times 3
python3 ad_skipper.py test-key seek_fwd
python3 ad_skipper.py test-key left
```
You want the button that makes **one jump forward of a fixed amount** (often
10 or 15 seconds) and the one that jumps back the same amount. Put them in
`config.json` under `skip.forward_key` / `skip.back_key`, and set
`step_seconds` to the size of the jump.

If the first press only brings up the on-screen controls and the second
actually jumps, tell me and I'll adjust the program for that.

## 4. Teach it the graphics

Do this once per graphic. For football, calibrate each network you watch
(CBS, Fox, NBC, ESPN…), since each places its score bug differently. For
reality shows, use the network's corner logo.

```
python3 ad_skipper.py calibrate --name fox_football
python3 ad_skipper.py calibrate --name bravo
```

It will ask you to:
1. Pause on a moment where the graphic is visible, then draw a **tight box
   around the parts of the graphic that don't change** (the frame, the network
   logo — not just the numbers).
2. Go to a commercial so it can see what "gone" looks like.

It then picks a detection threshold automatically and warns you if the
difference is too small to be reliable.

Check that detection works:
```
python3 ad_skipper.py watch
```
You should see `SEEN` during the show and `----` during ads.

## 5. Run it

```
python3 ad_skipper.py run
```

Then just watch. The first time, sit through a break or two to make sure it
behaves.

### Start automatically when the Pi boots
```
sudo tee /etc/systemd/system/adskipper.service >/dev/null <<EOF
[Unit]
Description=Ad Skipper
After=network-online.target

[Service]
User=$USER
WorkingDirectory=$HOME/adskipper
ExecStart=/usr/bin/python3 $HOME/adskipper/ad_skipper.py run
Restart=always

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl enable --now adskipper
```
See its log with `journalctl -u adskipper -f`.

## Settings worth tuning (`config.json`)

| Setting | Default | What it does |
|---|---|---|
| `detect.trigger_seconds` | 4 | How long the graphic must be gone before skipping. Raise it if replays or wide shots trigger false skips |
| `detect.min_present_seconds` | 10 | The graphic must be steadily visible this long before it will skip, so it leaves you alone in menus |
| `skip.settle_seconds` | 1.5 | Wait after each press. Raise it if the on-screen controls cover the graphic after a jump |
| `skip.max_skip_seconds` | 240 | Safety limit per break, so halftime or a lost picture can't send it skipping to the end |
| `skip.back_up_after` | true | Back up one step when the show returns |

## Known limits

- **Locked recordings:** if YouTube TV doesn't let you fast-forward a
  recording, this can't either; it only presses the buttons you could press.
- **False triggers:** if a replay briefly hides the score bug, it skips one
  step, sees the bug, and immediately backs up, so you lose almost nothing.
- **Camera bumps:** the search area has some margin, but if the camera moves a
  lot, run `calibrate` again.
- **Glare and lighting:** a lamp reflecting off the screen is the most common
  cause of poor detection. Aim the camera to avoid reflections.
