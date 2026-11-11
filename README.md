# HackberryPi digicam

A digital camera app for a Raspberry Pi 4 and the Pi camera (Module 2 or 3), written in pygame
with picamera2, plus a web gallery that shows new photos in the browser as they are taken.

## Run it

```bash
chmod +x setup.sh && ./setup.sh   # apt packages, ~/CreamPi, and a user service for the camera
pip install flask watchdog        # for the gallery
python3 digicam_touch.py          # the camera
python3 gallery.py                # the gallery, at http://<pi-ip>:5000
```

Take a photo by tapping the top-right corner of the screen, pressing `Space`, or pressing a button
on GPIO26; `Esc` or `Q` quits. Photos go to `~/CreamPi/` at 1920×1080 with a retro look: a
downscale and upscale, a 1 px red channel shift, grain and a slight blur.

`digicam.py` is the plain version: 2304×1296, JPEG quality 95, no post-processing, saved to
`~/digicam_photos/`. To start the camera on boot after `setup.sh`, run
`systemctl --user enable digicam` and `loginctl enable-linger $USER`.
