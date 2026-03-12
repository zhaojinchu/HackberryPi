#!/usr/bin/env python3
import time
import threading
from pathlib import Path
from datetime import datetime

import numpy as np
import pygame
from picamera2 import Picamera2
from libcamera import controls
from PIL import Image, ImageEnhance, ImageChops, ImageFilter

# =========================
# USER SETTINGS
# =========================

# Low-res live preview for speed + crunchy look.
PREVIEW_SIZE = (320, 240)      # good "digicam LCD" vibe
PREVIEW_FPS = 30               # lower if your display struggles

# Saved photo resolution.
# If this is too heavy on your Pi/display setup, reduce to (1920, 1080) or (1600, 1200).
STILL_SIZE = (2304, 1296)

# JPEG quality lower = more compression artifacts.

# Touch shutter zone: top-right box as fractions of screen width/height.
SHUTTER_ZONE = (0.72, 0.00, 0.28, 0.24)   # x, y, w, h in relative coordinates

# Preview / photo orientation. Change if your display/camera is mounted rotated.
PREVIEW_ROTATE = 0    # 0, 90, 180, 270
PHOTO_ROTATE = 0      # 0, 90, 180, 270

# Camera look - neutral for testing
DIGI_CONTRAST = 1.5
DIGI_SATURATION = 1.25
DIGI_SHARPNESS = 1.5
DIGI_EXPOSURE_VALUE = 0.75

# Saved-photo post-processing - disabled
POST_DOWNSCALE = 1.0
POST_RED_SHIFT_PX = 0
POST_NOISE_STD = 0.0
POST_BLUR_RADIUS = 0.0

# Higher JPEG quality for clean testing
PHOTO_JPEG_QUALITY = 95

# Prevent double taps from firing twice immediately.
CAPTURE_DEBOUNCE_SEC = 0.45

# Where photos are saved.
OUTPUT_DIR = Path.home() / "digicam_photos"

# =========================
# RESAMPLING COMPAT
# =========================

try:
    RESAMPLE_BILINEAR = Image.Resampling.BILINEAR
except AttributeError:
    RESAMPLE_BILINEAR = Image.BILINEAR


class DigicamApp:
    def __init__(self):
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

        self.picam2 = Picamera2()

        preview_controls = self._build_preview_controls()
        still_controls = self._build_still_controls()

        # Low-res preview config for speed.
        self.preview_config = self.picam2.create_preview_configuration(
            main={"size": PREVIEW_SIZE, "format": "BGR888"},
            buffer_count=4,
            queue=False,
            controls=preview_controls,
        )

        # Higher-res still config for capture.
        self.still_config = self.picam2.create_still_configuration(
            main={"size": STILL_SIZE, "format": "BGR888"},
            buffer_count=1,
            controls=still_controls,
        )

        self.picam2.configure(self.preview_config)
        self.picam2.start()
        time.sleep(0.8)  # warm up AE/AWB a bit

        pygame.init()
        pygame.font.init()

        self.screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
        pygame.display.set_caption("Pi Digicam")
        pygame.mouse.set_visible(False)

        self.screen_w, self.screen_h = self.screen.get_size()
        self.clock = pygame.time.Clock()
        self.font = pygame.font.Font(None, 26)
        self.small_font = pygame.font.Font(None, 22)

        self.running = True
        self.capture_busy = False
        self.last_capture_time = 0.0
        self.flash_until = 0.0
        self.status_text = "READY"
        self.status_until = 0.0

    def _build_preview_controls(self):
        frame_us = int(1_000_000 / PREVIEW_FPS)

        controls_dict = {
            "Contrast": DIGI_CONTRAST,
            "Saturation": DIGI_SATURATION,
            "Sharpness": DIGI_SHARPNESS,
            "ExposureValue": DIGI_EXPOSURE_VALUE,
            "FrameDurationLimits": (frame_us, frame_us),  # fixed preview FPS
        }

        # Camera Module 3 manual focus to default/hyperfocal-ish position if available.
        lens_info = self.picam2.camera_controls.get("LensPosition", None)
        if lens_info is not None and len(lens_info) >= 3:
            controls_dict["AfMode"] = controls.AfModeEnum.Manual
            controls_dict["LensPosition"] = lens_info[2]
        elif lens_info is not None:
            controls_dict["AfMode"] = controls.AfModeEnum.Manual
            controls_dict["LensPosition"] = 0.8

        return controls_dict

    def _build_still_controls(self):
        controls_dict = {
            "Contrast": DIGI_CONTRAST,
            "Saturation": DIGI_SATURATION,
            "Sharpness": DIGI_SHARPNESS,
            "ExposureValue": DIGI_EXPOSURE_VALUE,
        }

        lens_info = self.picam2.camera_controls.get("LensPosition", None)
        if lens_info is not None and len(lens_info) >= 3:
            controls_dict["AfMode"] = controls.AfModeEnum.Manual
            controls_dict["LensPosition"] = lens_info[2]
        elif lens_info is not None:
            controls_dict["AfMode"] = controls.AfModeEnum.Manual
            controls_dict["LensPosition"] = 0.8

        return controls_dict

    def _rel_rect(self, rel_rect):
        rx, ry, rw, rh = rel_rect
        return pygame.Rect(
            int(rx * self.screen_w),
            int(ry * self.screen_h),
            int(rw * self.screen_w),
            int(rh * self.screen_h),
        )

    def _point_in_shutter_zone(self, x, y):
        return self._rel_rect(SHUTTER_ZONE).collidepoint(x, y)

    def _set_status(self, text, seconds=2.0):
        self.status_text = text
        self.status_until = time.time() + seconds

    def _build_preview_surface(self, frame):
        # frame is H x W x 3, pygame wants W x H x 3
        surface = pygame.surfarray.make_surface(np.transpose(frame, (1, 0, 2)))
        if PREVIEW_ROTATE:
            surface = pygame.transform.rotate(surface, PREVIEW_ROTATE)
        return surface

    def _blit_cover(self, surface):
        sw, sh = self.screen.get_size()
        iw, ih = surface.get_size()

        scale = max(sw / iw, sh / ih)
        new_w = int(iw * scale)
        new_h = int(ih * scale)

        scaled = pygame.transform.scale(surface, (new_w, new_h))
        x = (sw - new_w) // 2
        y = (sh - new_h) // 2
        self.screen.blit(scaled, (x, y))

    def _draw_ui(self):
        shutter_rect = self._rel_rect(SHUTTER_ZONE)

        # translucent shutter zone
        overlay = pygame.Surface((shutter_rect.width, shutter_rect.height), pygame.SRCALPHA)
        overlay.fill((255, 255, 255, 35))
        self.screen.blit(overlay, shutter_rect.topleft)

        pygame.draw.rect(self.screen, (255, 255, 255), shutter_rect, width=2, border_radius=14)

        # simple shutter icon
        cx, cy = shutter_rect.center
        r_outer = max(12, min(shutter_rect.width, shutter_rect.height) // 6)
        r_inner = max(6, r_outer // 2)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx, cy), r_outer, width=2)
        pygame.draw.circle(self.screen, (255, 255, 255), (cx, cy), r_inner, width=2)

        label = self.small_font.render("SHOT", True, (255, 255, 255))
        self.screen.blit(label, (shutter_rect.x + 10, shutter_rect.y + 8))

        # status bar
        if time.time() < self.status_until or self.capture_busy:
            bar_h = 30
            bar = pygame.Surface((self.screen_w, bar_h), pygame.SRCALPHA)
            bar.fill((0, 0, 0, 140))
            self.screen.blit(bar, (0, self.screen_h - bar_h))

            text = self.status_text
            if self.capture_busy and not text.startswith("SAVING"):
                text = "SAVING..."
            txt = self.font.render(text, True, (255, 255, 255))
            self.screen.blit(txt, (10, self.screen_h - bar_h + 5))

        # white flash overlay when photo is taken
        if time.time() < self.flash_until:
            flash = pygame.Surface((self.screen_w, self.screen_h), pygame.SRCALPHA)
            flash.fill((255, 255, 255, 130))
            self.screen.blit(flash, (0, 0))

    def _touch_to_xy(self, event):
        if event.type == pygame.FINGERDOWN:
            return int(event.x * self.screen_w), int(event.y * self.screen_h)
        elif event.type == pygame.MOUSEBUTTONDOWN:
            return event.pos
        return None

    def _apply_digicam_look(self, rgb_array):
        img = Image.fromarray(rgb_array)

        if PHOTO_ROTATE:
            img = img.rotate(PHOTO_ROTATE, expand=True)

        return img

    # def _apply_digicam_look(self, rgb_array):
    #     img = Image.fromarray(rgb_array)

    #     if PHOTO_ROTATE:
    #         img = img.rotate(PHOTO_ROTATE, expand=True)

    #     # Old digicam feel: shrink then blow back up slightly.
    #     if 0 < POST_DOWNSCALE < 1.0:
    #         small_w = max(1, int(img.width * POST_DOWNSCALE))
    #         small_h = max(1, int(img.height * POST_DOWNSCALE))
    #         img = img.resize((small_w, small_h), RESAMPLE_BILINEAR)
    #         img = img.resize((rgb_array.shape[1], rgb_array.shape[0]), RESAMPLE_BILINEAR)

    #     # Tiny red-channel shift for imperfect optics vibe.
    #     r, g, b = img.split()
    #     r = ImageChops.offset(r, POST_RED_SHIFT_PX, 0)
    #     img = Image.merge("RGB", (r, g, b))

    #     # Punchy compact-camera processing.
    #     img = ImageEnhance.Contrast(img).enhance(1.20)
    #     img = ImageEnhance.Color(img).enhance(1.10)
    #     img = ImageEnhance.Sharpness(img).enhance(1.45)

    #     # Add some noise/grain.
    #     arr = np.asarray(img).astype(np.int16)
    #     noise = np.random.normal(0, POST_NOISE_STD, arr.shape).astype(np.int16)
    #     arr = np.clip(arr + noise, 0, 255).astype(np.uint8)
    #     img = Image.fromarray(arr)

    #     # Slight tiny blur after everything to stop it feeling too "modern crispy".
    #     if POST_BLUR_RADIUS > 0:
    #         img = img.filter(ImageFilter.GaussianBlur(radius=POST_BLUR_RADIUS))

    #     return img

    def _save_photo_worker(self, rgb_array, filename):
        try:
            img = self._apply_digicam_look(rgb_array)
            img.save(
                filename,
                format="JPEG",
                quality=PHOTO_JPEG_QUALITY,
                subsampling=0,
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
        self.flash_until = time.time() + 0.10
        self._set_status("CAPTURING...", seconds=1.0)

        try:
            # Switch to higher-res still mode, capture, then return to preview mode.
            rgb = self.picam2.switch_mode_and_capture_array(self.still_config, "main")
        except Exception as e:
            self.capture_busy = False
            self._set_status(f"CAPTURE FAILED: {e}", seconds=4.0)
            return

        filename = OUTPUT_DIR / datetime.now().strftime("DIGI_%Y%m%d_%H%M%S_%f.jpg")

        saver = threading.Thread(
            target=self._save_photo_worker,
            args=(rgb, filename),
            daemon=True,
        )
        saver.start()

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
                if pos is not None:
                    x, y = pos
                    if self._point_in_shutter_zone(x, y):
                        self.capture_photo()

    def run(self):
        try:
            while self.running:
                self.handle_events()

                frame = self.picam2.capture_array("main")
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