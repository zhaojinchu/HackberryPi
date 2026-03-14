# HackberryPi - Retro Digicam

A retro digital camera app for Raspberry Pi 4 with a real-time web photo gallery.

## Hardware

- Raspberry Pi 4 (1GB+ RAM)
- Raspberry Pi Camera Module 2 or 3
- Touchscreen display (optional, keyboard works too)

## Setup

```bash
chmod +x setup.sh && ./setup.sh
```

Or install manually:

```bash
sudo apt install -y python3-picamera2 python3-pygame python3-pil python3-numpy python3-libcamera python3-opencv
pip install flask watchdog
```

## Camera Apps

Two camera implementations are included:

### digicam_touch.py (recommended)

Dual-stream mode — lores preview + high-res capture run simultaneously. Faster captures, no freezes. Includes retro post-processing (grain, chromatic aberration, downscale artifacts).

```bash
python3 digicam_touch.py
```

- Photos saved to `~/CreamPi/`
- Resolution: 1920x1080, JPEG quality 48 (compressed retro look)

### digicam.py

Mode-switching approach — switches between preview and still configs on capture. Clean output, no post-processing.

```bash
python3 digicam.py
```

- Photos saved to `~/digicam_photos/`
- Resolution: 2304x1296, JPEG quality 95

### Controls

| Input | Action |
|-------|--------|
| Tap shutter zone (top-right) | Take photo |
| Spacebar | Take photo |
| ESC / Q | Quit |

## Web Gallery

A real-time photo gallery that updates live as new photos are taken.

```bash
python3 gallery.py
```

Then open `http://<pi-ip>:5000` in a browser on any device on the same network.

The gallery watches the photo directories and pushes new images to connected browsers instantly via Server-Sent Events.

### Gallery options

```bash
python3 gallery.py --port 8080                          # custom port
python3 gallery.py --dirs ~/CreamPi ~/digicam_photos    # watch specific dirs
python3 gallery.py --host 0.0.0.0                       # listen on all interfaces (default)
```

## Auto-start on boot

```bash
systemctl --user enable digicam
loginctl enable-linger $USER
```

## File naming

All photos follow the pattern: `DIGI_YYYYMMDD_HHMMSS_microseconds.jpg`
