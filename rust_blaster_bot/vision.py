"""Screen-space rust and popup detection. No input devices are used here.

Coordinates returned by this module are relative to the captured image, not to
an absolute desktop position. OpenCV images are BGR (as returned by mss).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

Point = tuple[int, int]
Box = tuple[int, int, int, int]  # left, top, right, bottom


@dataclass(frozen=True)
class VisionConfig:
    # Fractions of the *game window*, not the entire virtual desktop. The
    # defaults exclude the left upgrades, top banners, right buttons and the
    # bottom decorations in the supplied Rust Blaster screenshot.
    roi: tuple[float, float, float, float] = (0.24, 0.20, 0.89, 0.93)
    hue: tuple[int, int] = (4, 24)  # OpenCV hue: 0..179
    saturation_min: int = 85
    value: tuple[int, int] = (25, 190)
    close_threshold: float = 0.91

    def __post_init__(self) -> None:
        x0, y0, x1, y1 = self.roi
        if not (0 <= x0 < x1 <= 1 and 0 <= y0 < y1 <= 1):
            raise ValueError("ROI must be four fractions: left,top,right,bottom")
        if not (0 <= self.hue[0] <= self.hue[1] <= 179):
            raise ValueError("Hue must be between 0 and 179")
        if not (0 <= self.saturation_min <= 255):
            raise ValueError("Saturation must be between 0 and 255")
        if not (0 <= self.value[0] <= self.value[1] <= 255):
            raise ValueError("Value must be between 0 and 255")
        if not (0 < self.close_threshold <= 1):
            raise ValueError("Close threshold must be in (0, 1]")


def relative_box(shape: tuple[int, ...], fractions: tuple[float, float, float, float]) -> Box:
    height, width = shape[:2]
    x0, y0, x1, y1 = fractions
    return (round(x0 * width), round(y0 * height),
            round(x1 * width), round(y1 * height))


@dataclass(frozen=True)
class Popup:
    # "blocked" means a probable modal was found, but there is no safe close
    # control to click. The controller must pause rather than spray behind it.
    kind: str = "none"  # none | close | blocked
    close_at: Point | None = None
    panel: Box | None = None
    source: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class Reticle:
    center: Point
    radius: float


@dataclass
class Observation:
    roi: Box
    rust_mask: np.ndarray  # uint8, full image dimensions; 255 = rusty pixel
    rust_pixels: int
    target: Point | None
    popup: Popup
    reticle: Reticle | None = None


def find_reticle(frame: np.ndarray, roi: Box) -> Reticle | None:
    """Locate the pink circular wash cursor visible in Rust Blaster.

    This is independent of item shape and of the operating system's cursor
    coordinates; a game may lock the OS cursor while moving its own reticle.
    """
    height, width = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    pink = cv2.inRange(hsv, (150, 85, 115), (179, 255, 255))
    contours, _ = cv2.findContours(pink, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates: list[tuple[float, Reticle]] = []
    for contour in contours:
        (cx, cy), radius = cv2.minEnclosingCircle(contour)
        if not min(width, height) * 0.045 <= radius <= min(width, height) * 0.19:
            continue
        x, y, w, h = cv2.boundingRect(contour)
        if not 0.72 <= w / h <= 1.28:
            continue
        # A ring may extend beyond the rust ROI; its CENTER must still be
        # near the playfield, not on the right-hand toolbar or top banner.
        if not (roi[0] - width * 0.12 <= cx <= roi[2] + width * 0.04 and
                roi[1] - height * 0.16 <= cy <= roi[3] + height * 0.05):
            continue
        area = cv2.contourArea(contour)
        if area < 0.45 * np.pi * radius * radius:
            continue
        fill = cv2.countNonZero(pink[y:y + h, x:x + w]) / (np.pi * radius * radius)
        if not 0.025 <= fill <= 0.38:  # a thin outline, not a solid button
            continue
        perimeter = cv2.arcLength(contour, True)
        circularity = 4 * np.pi * area / (perimeter * perimeter) if perimeter else 0
        if circularity >= 0.50:
            candidates.append((area * circularity, Reticle((round(cx), round(cy)), radius)))
    return max(candidates, default=(0, None), key=lambda candidate: candidate[0])[1]


class PopupDetector:
    """Recognize an X on a central modal, or user-supplied close-button crops.

    Never looks for or clicks buttons in the upgrades/sidebar. A recognized
    modal without a confident X is reported as blocked rather than guessed at.
    """

    def __init__(self, config: VisionConfig, close_templates: Sequence[str | Path] = ()) -> None:
        self.threshold = config.close_threshold
        self.templates: list[tuple[str, np.ndarray]] = []
        for filename in close_templates:
            template = cv2.imread(str(filename), cv2.IMREAD_COLOR)
            if template is None or min(template.shape[:2]) < 8 or float(template.std()) < 8:
                raise ValueError(f"Invalid close-button template: {filename}")
            self.templates.append((str(filename), template))

    def detect(self, frame: np.ndarray) -> Popup:
        # Templates can also handle small/bright popups that do not resemble a
        # dark dialog. They must match very closely, twice on consecutive frames.
        sx0, sy0, sx1, sy1 = relative_box(frame.shape, (0.26, 0.12, 0.84, 0.86))
        search = frame[sy0:sy1, sx0:sx1]
        best: Popup | None = None
        for filename, template in self.templates:
            th, tw = template.shape[:2]
            if th > search.shape[0] or tw > search.shape[1]:
                continue
            scores = cv2.matchTemplate(search, template, cv2.TM_CCOEFF_NORMED)
            _, score, _, (x, y) = cv2.minMaxLoc(scores)
            if score >= self.threshold and (best is None or score > best.confidence):
                best = Popup("close", (sx0 + x + tw // 2, sy0 + y + th // 2),
                             source=f"template {filename}", confidence=score)
        if best is not None:
            return best

        panel = self._find_panel(frame)
        if panel is None:
            return Popup()
        close_at, score = self._find_x(frame, panel)
        if close_at is not None:
            return Popup("close", close_at, panel, "X on central dialog", score)
        return Popup("blocked", panel=panel, source="central dialog without a recognized X")

    @staticmethod
    def _find_panel(frame: np.ndarray) -> Box | None:
        height, width = frame.shape[:2]
        ox, oy, rx, ry = relative_box(frame.shape, (0.26, 0.18, 0.84, 0.86))
        hsv = cv2.cvtColor(frame[oy:ry, ox:rx], cv2.COLOR_BGR2HSV)
        h, s, v = cv2.split(hsv)
        # Broad, flat dialogs can be navy, charcoal, or light gray. The
        # playfield is green, while rust is orange; the central location and
        # large rectangular fill protect against ordinary item highlights.
        navy = (h >= 96) & (h <= 150) & (s >= 30) & (s <= 185) & (v >= 23) & (v <= 190)
        gray = (s <= 45) & (v >= 32) & (v <= 175)
        light = (s <= 60) & (v >= 175)
        charcoal = (s <= 80) & (v <= 44)
        mask = ((navy | gray | light | charcoal).astype(np.uint8) * 255)
        kernel_size = max(5, round(min(width, height) * 0.011) | 1)
        closed = cv2.morphologyEx(mask, cv2.MORPH_CLOSE,
                                  cv2.getStructuringElement(cv2.MORPH_RECT,
                                                             (kernel_size, kernel_size)))
        contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        candidates: list[tuple[float, Box]] = []
        for contour in contours:
            x, y, w, hgt = cv2.boundingRect(contour)
            if w < width * 0.18 or hgt < height * 0.17 or not 0.65 < w / hgt < 4:
                continue
            cx, cy = ox + x + w / 2, oy + y + hgt / 2
            if not (width * 0.35 <= cx <= width * 0.72 and
                    height * 0.27 <= cy <= height * 0.74):
                continue
            fill = cv2.countNonZero(mask[y:y + hgt, x:x + w]) / (w * hgt)
            if fill < 0.55:
                continue
            candidates.append((w * hgt * fill, (ox + x, oy + y, ox + x + w, oy + y + hgt)))
        return max(candidates, default=(0, None), key=lambda candidate: candidate[0])[1]

    @staticmethod
    def _find_x(frame: np.ndarray, panel: Box) -> tuple[Point | None, float]:
        height, width = frame.shape[:2]
        x0, y0, x1, y1 = panel
        pw, ph = x1 - x0, y1 - y0
        # Search only in the upper-right portion of the detected dialog.
        left = max(0, round(x0 + pw * 0.69))
        top = max(0, round(y0 - ph * 0.06))
        right = min(width, round(x1 + pw * 0.05))
        bottom = min(height, round(y0 + min(ph * 0.25, height * 0.12)))
        if right <= left or bottom <= top:
            return None, 0.0
        hsv = cv2.cvtColor(frame[top:bottom, left:right], cv2.COLOR_BGR2HSV)
        white = np.where((hsv[:, :, 1] <= 110) & (hsv[:, :, 2] >= 155),
                         255, 0).astype(np.uint8)
        dark = np.where(hsv[:, :, 2] <= 100, 255, 0).astype(np.uint8)
        best_score = 0.0
        best_point: Point | None = None
        # A binary pair of diagonals is more tolerant of theme/scale changes
        # than a hard-coded screenshot of a particular popup button. Check a
        # light X on dark UI and a dark X on a pale dialog.
        for fraction in (0.018, 0.022, 0.027, 0.033, 0.040, 0.050):
            size = max(9, round(height * fraction))
            if size >= min(white.shape[:2]):
                continue
            pattern = np.zeros((size, size), dtype=np.uint8)
            inset = round(size * 0.18)
            stroke = max(1, round(size * 0.14))
            cv2.line(pattern, (inset, inset), (size - inset - 1, size - inset - 1), 255, stroke)
            cv2.line(pattern, (size - inset - 1, inset), (inset, size - inset - 1), 255, stroke)
            for icon in (white, dark):
                scores = cv2.matchTemplate(icon, pattern, cv2.TM_CCOEFF_NORMED)
                _, score, _, (x, y) = cv2.minMaxLoc(scores)
                if score > best_score:
                    best_score = score
                    best_point = (left + x + size // 2, top + y + size // 2)
        # Being conservative is important: an uncertain icon must not cause
        # a stray click on a gem/purchase button.
        return (best_point, best_score) if best_score >= 0.72 else (None, best_score)


class Detector:
    def __init__(self, config: VisionConfig | None = None,
                 close_templates: Sequence[str | Path] = ()) -> None:
        self.config = config if config is not None else VisionConfig()
        self.popups = PopupDetector(self.config, close_templates)

    def analyze(self, frame: np.ndarray, excluded: Sequence[Point] = ()) -> Observation:
        if frame.ndim != 3 or frame.shape[2] != 3:
            raise ValueError("Expected a BGR screenshot with three color channels")
        popup = self.popups.detect(frame)
        roi = relative_box(frame.shape, self.config.roi)
        x0, y0, x1, y1 = roi
        hsv = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        h0, h1 = self.config.hue
        v0, v1 = self.config.value
        raw = cv2.inRange(hsv, (h0, self.config.saturation_min, v0), (h1, 255, v1))
        raw = cv2.morphologyEx(raw, cv2.MORPH_CLOSE,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5)))
        raw = cv2.morphologyEx(raw, cv2.MORPH_OPEN,
                               cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
        n, labels, stats, _ = cv2.connectedComponentsWithStats(raw, connectivity=8)
        min_area = max(8, round((x1 - x0) * (y1 - y0) * 0.00004))
        valid_labels = np.flatnonzero(stats[1:, cv2.CC_STAT_AREA] >= min_area) + 1
        cleaned = (np.isin(labels, valid_labels).astype(np.uint8) * 255) if n > 1 else np.zeros_like(raw)
        pixels = cv2.countNonZero(cleaned)
        target: Point | None = None
        if pixels and popup.kind == "none":
            targets = cleaned.copy()
            radius = max(12, round(min(frame.shape[:2]) * 0.035))
            for x, y in excluded:
                cv2.circle(targets, (x - x0, y - y0), radius, 0, -1)
            if cv2.countNonZero(targets):
                distance = cv2.distanceTransform(targets, cv2.DIST_L2, cv2.DIST_MASK_5)
                _, _, _, (x, y) = cv2.minMaxLoc(distance)
                target = (x0 + x, y0 + y)
        full_mask = np.zeros(frame.shape[:2], dtype=np.uint8)
        full_mask[y0:y1, x0:x1] = cleaned
        return Observation(roi, full_mask, pixels, target, popup, find_reticle(frame, roi))


def annotate(frame: np.ndarray, observation: Observation) -> np.ndarray:
    """Create a no-click preview showing exactly where the detector would aim."""
    overlay = frame.copy()
    overlay[observation.rust_mask != 0] = (25, 90, 245)
    preview = cv2.addWeighted(frame, 0.55, overlay, 0.45, 0)
    x0, y0, x1, y1 = observation.roi
    cv2.rectangle(preview, (x0, y0), (x1, y1), (255, 240, 50), 2)
    if observation.target is not None:
        cv2.drawMarker(preview, observation.target, (50, 255, 50),
                       cv2.MARKER_CROSS, 26, 2)
    if observation.reticle is not None:
        cv2.circle(preview, observation.reticle.center, 8, (255, 160, 40), 2)
        cv2.putText(preview, "cursor", (observation.reticle.center[0] + 10,
                                     observation.reticle.center[1] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 160, 40), 1)
    if observation.popup.panel is not None:
        a, b, c, d = observation.popup.panel
        cv2.rectangle(preview, (a, b), (c, d), (255, 60, 220), 3)
    if observation.popup.close_at is not None:
        cv2.circle(preview, observation.popup.close_at, 18, (255, 60, 220), 3)
    return preview
