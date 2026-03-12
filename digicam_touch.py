#!/usr/bin/env python3
"""
Retro digicam app for Raspberry Pi 4 (1GB RAM).

Uses picamera2 dual-stream: lores for live preview, main for high-res capture.
No mode switching = fast capture, low memory, no freezes.
"""
import os
import sys
import time
import threading
from pathlib import Path
from datetime import datetime

# SDL hints for RPi4 KMS/DRM - must be set before pygame import.
os.environ.setdefault("SDL_VIDEODRIVER", "kmsdrm")
os.environ.setdefault("SDL_VIDEO_EGL_DRIVER", "libEGL.so")

import cv2
import numpy as np
import pygame
from PIL import Image, ImageEnhance, ImageChops, ImageFilter

# =========================
# USER SETTINGS
# =========================

# Live preview resolution (pulled from camera lores stream).
PREVIEW_SIZE = (320, 240)

# Target preview FPS. 24 is smooth enough and leaves CPU headroom on Pi4.
PREVIEW_FPS = 24

# Saved photo resolution (from camera main stream, always running).
# 1920x1080 is a good balance of quality vs memory on 1GB Pi4.
# For Camera Module 3 you can try (2304, 1296).
STILL_SIZE = (1920, 1080)

# JPEG quality: lower = more compression artifacts = more "digicam" feel.
PHOTO_JPEG_QUALITY = 48

# Touch shutter zone: top-right box (x, y, w, h as fractions of screen).
SHUTTER_ZONE = (0.72, 0.00, 0.28, 0.24)

# Rotation for preview and saved photos (0, 90, 180, 270).
PREVIEW_ROTATE = 0
PHOTO_ROTATE = 0

# Camera ISP look.
DIGI_CONTRAST = 1.35
DIGI_SATURATION = 1.12
DIGI_SHARPNESS = 1.75
DIGI_EXPOSURE_VALUE = -0.35

# Saved-photo post-processing.
POST_DOWNSCALE = 0.78
POST_RED_SHIFT_PX = 1
POST_NOISE_STD = 5.0
POST_BLUR_RADIUS = 0.2

# Debounce time between captures.
CAPTURE_DEBOUNCE_SEC = 0.45

# Where photos are saved.
OUTPUT_DIR = Path.home() / "CreamPi"

# =========================
# PIL resampling compat
# =========================
try:
    RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE_BILINEAR = Image.BILINEAR


def init_camera():
    """Initialize picamera2 with dual-stream config.

    Returns (picam2, camera_has_af) or exits with a helpful message.
    """
    try:
        from picamera2 import Picamera2
    except ImportError:
        print("ERROR: picamera2 not found. Run: sudo apt install -y python3-picamera2")
        sys.exit(1)

    try:
        picam2 = Picamera2()
    except Exception as e:
        print(f"ERROR: Cannot open camera: {e}")
        print("Check that the camera cable is connected and 'libcamera-hello' works.")
        sys.exit(1)

    # Dual-stream config: main for stills, lores for preview.
    # Both streams run simultaneously from the same ISP pipeline - no mode switch needed.
    config = picam2.create_preview_configuration(
        main={"size": STILL_SIZE, "format": "RGB888"},
        lores={"size": PREVIEW_SIZE, "format": "YUV420"},
        buffer_count=4,
        queue=False,
    )
    picam2.configure(config)

    # Now that camera is configured, we can read camera_controls safely.
    camera_has_af = "LensPosition" in picam2.camera_controls

    # Build runtime controls.
    cam_controls = {
        "Contrast": DIGI_CONTRAST,
        "Saturation": DIGI_SATURATION,
        "Sharpness": DIGI_SHARPNESS,
        "ExposureValue": DIGI_EXPOSURE_VALUE,
        "FrameDurationLimits": (int(1_000_000 / PREVIEW_FPS),
                                int(1_000_000 / PREVIEW_FPS)),
    }

    if camera_has_af:
        try:
            from libcamera import controls as lc_controls
            cam_controls["AfMode"] = lc_controls.AfModeEnum.Manual
        except (ImportError, AttributeError):
            # Older libcamera without AfModeEnum - use raw int value (0 = Manual).
            cam_controls["AfMode"] = 0

        lens_info = picam2.camera_controls.get("LensPosition")
        if lens_info is not None and len(lens_info) >= 3:
            cam_controls["LensPosition"] = lens_info[2]  # default position
        else:
            cam_controls["LensPosition"] = 0.8  # reasonable hyperfocal guess

    picam2.start()
    time.sleep(0.4)

    # Apply controls after start so the ISP is running.
    picam2.set_controls(cam_controls)
    time.sleep(0.4)  # let AE/AWB settle

    return picam2, camera_has_af


class DigicamApp:
    def __init__(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        self.picam2, self.camera_has_af = init_camera()

        # Initialize pygame.
        pygame.init()
        pygame.font.init()

        # Try fullscreen; fall back to a window if KMS/DRM isn't available.
        try:
            self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        except pygame.error:
            # Fallback for X11 / Wayland / SSH-forwarded sessions.
            os.environ["SDL_VIDEODRIVER"] = ""
            pygame.display.quit()
            pygame.display.init()
            self.screen = pygame.display.set_mode((480, 320))

        pygame.display.set_caption("Pi Digicam")
        pygame.mouse.set_visible(False)

        self.screen_w, self.screen_h = self.screen.get_size()
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 26)
        self.small_font = pygame.font.Font(None, 22)

        # Pre-compute shutter zone rect once.
        rx, ry, rw, rh = SHUTTER_ZONE
        self.shutter_rect = pygame.Rect(
            int(rx * self.screen_w), int(ry * self.screen_h),
            int(rw * self.screen_w), int(rh * self.screen_h),
        )

        self.running = True
        self.capture_busy = False
        self.last_capture_time = 0.0
        self.flash_until = 0.0
        self.status_text = "READY"
        self.status_until = 0.0

        # Pre-allocate overlay surfaces to avoid per-frame allocation.
        self._shutter_overlay = pygame.Surface(
            (self.shutter_rect.width, self.shutter_rect.height), pygame.SRCALPHA
        )
        self._shutter_overlay.fill((255, 255, 255, 35))

        self._status_bar = pygame.Surface((self.screen_w, 30), pygame.SRCALPHA)
        self._status_bar.fill((0, 0, 0, 140))

        self._flash_surface = pygame.Surface(
            (self.screen_w, self.screen_h), pygame.SRCALPHA
        )
        self._flash_surface.fill((255, 255, 255, 130))

    def _set_status(self, text, seconds=2.0):
        self.status_text = text
        self.status_until = time.time() + seconds

    def _build_preview_surface(self, frame):
        """Convert camera lores YUV420 frame to a pygame surface."""
        # lores stream is YUV420: shape is (H*3//2, W) - convert to RGB.
        rgb = cv2.cvtColor(frame, cv2.COLOR_YUV420p2RGB)
        # pygame wants (W, H, 3) layout.
        surface = pygame.surfarray.make_surface(np.transpose(rgb, (1, 0, 2)))
        if PREVIEW_ROTATE:
            surface = pygame.transform.rotate(surface, PREVIEW_ROTATE)
        return surface

    def _blit_cover(self, surface):
        """Scale and center-crop the preview to fill the screen."""
        sw, sh = self.screen_w, self.screen_h
        iw, ih = surface.get_size()
        scale = max(sw / iw, sh / ih)
        new_w = int(iw * scale)
        new_h = int(ih * scale)
        scaled = pygame.transform.scale(surface, (new_w, new_h))
        self.screen.blit(scaled, ((sw - new_w) // 2, (sh - new_h) // 2))

    def _draw_ui(self):
        now = time.time()

        # Shutter zone overlay (pre-allocated surface).
        self.screen.blit(self._shutter_overlay, self.shutter_rect.topleft)
        pygame.draw.rect(self.screen, (255, 255, 255), self.shutter_rect,
                         width=2, border_radius=14)

        # Shutter icon.
        cx, cy = self.shutter_rect.center
        r_outer = max(12, min(self.shutter_rect.width, self.shutter_rect.height) // 6)
        r_inner = max(6, r_outer // 2)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx, cy), r_outer, width=2)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx, cy), r_inner, width=2)

        label = self.small_font.render("SHOT", True, (255, 255, 255))
        self.screen.blit(label, (self.shutter_rect.x + 10, self.shutter_rect.y + 8))

        # Status bar.
        if now < self.status_until or self.capture_busy:
            self.screen.blit(self._status_bar, (0, self.screen_h - 30))
            text = self.status_text
            if self.capture_busy and not text.startswith("SAVING"):
                text = "SAVING..."
            txt = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(txt, (10, self.screen_h - 25))

        # Flash overlay.
        if now < self.flash_until:
            self.screen.blit(self._flash_surface, (0, 0))

    def _touch_to_xy(self, event):
        if event.type == pygame.FINGERDOWN:
            return int(event.x * self.screen_w), int(event.y * self.screen_h)
        if event.type == pygame.MOUSEBUTTONDOWN:
            return event.pos
        return None

    def _apply_digicam_look(self, rgb_array):
        """Post-process a captured frame to get the retro digicam aesthetic."""
        img = Image.fromarray(rgb_array)

        if PHOTO_ROTATE:
            img = img.rotate(PHOTO_ROTATE, expand=True)

        w, h = img.size

        # Downscale then upscale for that old-sensor look.
        if 0 < POST_DOWNSCALE < 1.0:
            small_w = max(1, int(w * POST_DOWNSCALE))
            small_h = max(1, int(h * POST_DOWNSCALE))
            img = img.resize((small_w, small_h), RESAMPLE_BILINEAR)
            img = img.resize((w, h), RESAMPLE_BILINEAR)

        # Chromatic misalignment: shift red channel.
        if POST_RED_SHIFT_PX:
            r, g, b = img.split()
            r = ImageChops.offset(r, POST_RED_SHIFT_PX, 0)
            img = Image.merge("RGB", (r, g, b))

        # Punchy compact-camera processing.
        img = ImageEnhance.Contrast(img).enhance(1.20)
        img = ImageEnhance.Color(img).enhance(1.10)
        img = ImageEnhance.Sharpness(img).enhance(1.45)

        # Add grain. Use uint8 math to halve memory vs int16 on full-res images.
        if POST_NOISE_STD > 0:
            arr = np.asarray(img)
            noise = np.random.normal(0, POST_NOISE_STD, arr.shape)
            noisy = np.clip(arr.astype(np.float32) + noise.astype(np.float32),
                            0, 255).astype(np.uint8)
            img = Image.fromarray(noisy)
            del arr, noise, noisy

        # Slight softness.
        if POST_BLUR_RADIUS > 0:
            img = img.filter(ImageFilter.GaussianBlur(radius=POST_BLUR_RADIUS))

        return img

    def _save_photo_worker(self, rgb_array, filename):
        """Run in a background thread to avoid blocking the preview."""
        try:
            img = self._apply_digicam_look(rgb_array)
            img.save(
                filename,
                format="JPEG",
                quality=PHOTO_JPEG_QUALITY,
                subsampling=2,
                optimize=False,
            )
            self._set_status(f"SAVED {filename.name}", seconds=2.5)
        except Exception as e:
            self._set_status(f"SAVE FAILED: {e}", seconds=4.0)
        finally:
            self.capture_busy = False

    def capture_photo(self):
        now = time.time()
        if self.capture_busy:
            return
        if now - self.last_capture_time < CAPTURE_DEBOUNCE_SEC:
            return

        self.last_capture_time = now
        self.capture_busy = True
        self.flash_until = now + 0.10
        self._set_status("CAPTURING...", seconds=1.0)

        try:
            # Grab from the main (high-res) stream - no mode switch needed.
            rgb = self.picam2.capture_array("main")
        except Exception as e:
            self.capture_busy = False
            self._set_status(f"CAPTURE FAILED: {e}", seconds=4.0)
            return

        filename = OUTPUT_DIR / datetime.now().strftime("DIGI_%Y%m%d_%H%M%S_%f.jpg")

        thread = threading.Thread(
            target=self._save_photo_worker,
            args=(rgb, filename),
            daemon=True,
        )
        thread.start()

    def handle_events(self):
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                self.running = False
                return

            if event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    self.running = False
                    return
                if event.key == pygame.K_SPACE:
                    self.capture_photo()

            if event.type in (pygame.MOUSEBUTTONDOWN, pygame.FINGERDOWN):
                pos = self._touch_to_xy(event)
                if pos and self.shutter_rect.collidepoint(pos):
                    self.capture_photo()

    def run(self):
        try:
            while self.running:
                self.handle_events()

                # Read from lores stream for fast, low-memory preview.
                frame = self.picam2.capture_array("lores")
                preview_surface = self._build_preview_surface(frame)

                self.screen.fill((0, 0, 0))
                self._blit_cover(preview_surface)
                self._draw_ui()

                pygame.display.flip()
                self.clock.tick(PREVIEW_FPS)
        finally:
            try:
                self.picam2.stop()
            except Exception:
                pass
            pygame.quit()


if __name__ == "__main__":
    app = DigicamApp()
    app.run()
