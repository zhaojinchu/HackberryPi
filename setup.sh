#!/bin/bash
# Setup script for Pi Digicam on Raspberry Pi 4 (Raspberry Pi OS Bookworm).
# Run as: chmod +x setup.sh && ./setup.sh

set -e

echo "=== Pi Digicam Setup ==="

# System packages.
echo "[1/4] Installing system dependencies..."
sudo apt update
sudo apt install -y \
    python3-picamera2 \
    python3-pygame \
    python3-pil \
    python3-numpy \
    python3-libcamera \
    python3-opencv

# Create photo output directory.
echo "[2/4] Creating photo directory..."
mkdir -p "$HOME/CreamPi"

# Enable camera interface if not already enabled.
echo "[3/4] Checking camera interface..."
if ! grep -q "^dtoverlay=imx" /boot/firmware/config.txt 2>/dev/null; then
    echo "  Note: If your camera isn't detected, add the appropriate dtoverlay"
    echo "  to /boot/firmware/config.txt. For Camera Module 3: dtoverlay=imx708"
    echo "  For Camera Module 2: dtoverlay=imx219"
fi

# Optional: install systemd service for auto-start.
echo "[4/4] Installing systemd service (optional)..."
SERVICE_FILE="$HOME/.config/systemd/user/digicam.service"
mkdir -p "$(dirname "$SERVICE_FILE")"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Pi Digicam Touch
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 ${SCRIPT_DIR}/digicam_touch.py
Restart=on-failure
RestartSec=3
Environment=SDL_VIDEODRIVER=kmsdrm

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "  Service installed. To enable auto-start on boot:"
echo "    systemctl --user enable digicam"
echo "    loginctl enable-linger $USER"
echo ""
echo "  To start now:  systemctl --user start digicam"
echo "  To stop:       systemctl --user stop digicam"
echo ""
echo "=== Setup complete ==="
echo "Run manually with: python3 ${SCRIPT_DIR}/digicam_touch.py"
echo "Photos will be saved to: $HOME/CreamPi/"
