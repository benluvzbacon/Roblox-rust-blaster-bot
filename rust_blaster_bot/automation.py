"""Cautious mouse automation around the pure screen detector.

The controller is intentionally started paused. It never clicks purchase or
upgrade UI: only detected rust inside the playfield, or a verified close X.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np

from .vision import Detector, Observation, Point

log = logging.getLogger(__name__)


class Pointer(Protocol):
    @property
    def position(self) -> Point: ...

    def move_to(self, point: Point) -> None: ...
    def press(self) -> None: ...
    def release(self) -> None: ...


class PynputPointer:
    """Load the desktop-input library only when live automation is requested."""

    def __init__(self) -> None:
        from pynput.mouse import Button, Controller
        self.mouse = Controller()
        self.button = Button.left
        self._held = False

    @property
    def position(self) -> Point:
        x, y = self.mouse.position
        return (round(x), round(y))

    def move_to(self, point: Point) -> None:
        self.mouse.position = point

    def press(self) -> None:
        if not self._held:
            self._held = True
            try:
                self.mouse.press(self.button)
            except Exception:
                self.release()
                raise

    def release(self) -> None:
        if self._held:
            self.mouse.release(self.button)
            self._held = False


@dataclass(frozen=True)
class Timings:
    pulse: float = 0.35
    settle: float = 0.14
    popup_settle: float = 0.55
    max_stale_pulses: int = 10

    def __post_init__(self) -> None:
        if not (0.02 <= self.pulse <= 3.0 and 0 <= self.settle <= 3.0 and
                0 <= self.popup_settle <= 5.0 and self.max_stale_pulses >= 2):
            raise ValueError("Invalid pulse/settle timing or stale-pulse limit")


class BotController:
    """One tick takes a screenshot, acts at most once, and checks the result.

    Injected screen/pointer/focus interfaces also let the safety behavior be
    tested without a running game or any physical mouse clicks.
    """

    def __init__(self, detector: Detector, capture: Callable[[], np.ndarray],
                 pointer: Pointer, origin: Point = (0, 0),
                 focused: Callable[[], bool] = lambda: True,
                 timings: Timings | None = None) -> None:
        self.detector = detector
        self.capture = capture
        self.pointer = pointer
        self.origin = origin
        self.focused = focused
        self.timings = timings if timings is not None else Timings()
        self.active = threading.Event()
        self.stopped = threading.Event()
        self._input_lock = threading.RLock()
        self._pending_close: Point | None = None
        self._last_bot_position: Point | None = None
        self._last_closed: Point | None = None
        self._close_attempts = 0
        self._failed_positions: list[Point] = []
        self._stale_pulses = 0
        self.pulse_count = 0

    def resume(self) -> None:
        if not self.stopped.is_set():
            self._pending_close = None
            self._last_bot_position = None  # user may reposition the pointer while paused
            self._failed_positions.clear()
            self._stale_pulses = 0
            self.active.set()
            log.info("Running. F8 pauses, F7 stops; move mouse to the capture's upper-left corner to stop.")

    def pause(self, reason: str = "Paused") -> None:
        was_active = self.active.is_set()
        self.active.clear()
        with self._input_lock:
            self.pointer.release()
        if was_active:
            log.warning("%s", reason)

    def stop(self) -> None:
        self.stopped.set()
        self.pause("Stopped")

    def _safe_to_act(self) -> bool:
        if not self.active.is_set() or self.stopped.is_set():
            return False
        if not self.focused():
            self.pause("Game is not the foreground window; press F8 to resume when it is focused")
            return False
        x, y = self.pointer.position
        if self.origin[0] <= x <= self.origin[0] + 8 and self.origin[1] <= y <= self.origin[1] + 8:
            self.stop()
            return False
        if (self._last_bot_position is not None and
                math.dist((x, y), self._last_bot_position) > 65):
            self.pause("Mouse moved since the last bot action; press F8 to resume")
            return False
        return True

    def _desktop_point(self, point: Point) -> Point:
        return point[0] + self.origin[0], point[1] + self.origin[1]

    def _wait(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while self.active.is_set() and not self.stopped.is_set() and time.monotonic() < end:
            time.sleep(min(0.03, max(0, end - time.monotonic())))

    def _close_popup(self, point: Point) -> str:
        if self._last_closed is not None and math.dist(point, self._last_closed) <= 15:
            if self._close_attempts >= 2:
                self.pause("Popup is still present after two close clicks; check the X or add a close template")
                return "blocked"
            self._close_attempts += 1
        else:
            self._last_closed, self._close_attempts = point, 1
        with self._input_lock:
            if not self._safe_to_act():
                return "paused"
            absolute = self._desktop_point(point)
            self.pointer.move_to(absolute)
            self._last_bot_position = absolute
            try:
                self.pointer.press()
            finally:
                self.pointer.release()
        log.info("Closed popup at %s", absolute)
        self._pending_close = None
        self._failed_positions.clear()
        self._wait(self.timings.popup_settle)
        return "closed popup"

    def _spray(self, point: Point, shape: tuple[int, ...]) -> bool:
        absolute = self._desktop_point(point)
        try:
            with self._input_lock:
                if not self._safe_to_act():
                    return False
                self.pointer.move_to(absolute)
                self._last_bot_position = absolute
                self.pointer.press()
            end = time.monotonic() + self.timings.pulse
            tolerance = max(35, round(min(shape[:2]) * 0.055))
            while time.monotonic() < end:
                if not self._safe_to_act():
                    return False
                if math.dist(self.pointer.position, absolute) > tolerance:
                    self.pause("Mouse moved by user during a pulse; press F8 to resume")
                    return False
                time.sleep(min(0.025, max(0, end - time.monotonic())))
        finally:
            with self._input_lock:
                self.pointer.release()
        return self.active.is_set()

    @staticmethod
    def _nearby_rust(observation: Observation, point: Point) -> int:
        mask = observation.rust_mask
        radius = max(22, round(min(mask.shape) * 0.06))
        x0, x1 = max(0, point[0] - radius), min(mask.shape[1], point[0] + radius + 1)
        y0, y1 = max(0, point[1] - radius), min(mask.shape[0], point[1] + radius + 1)
        patch = mask[y0:y1, x0:x1]
        yy, xx = np.ogrid[y0:y1, x0:x1]
        return int(np.count_nonzero(patch & ((xx - point[0]) ** 2 + (yy - point[1]) ** 2 <= radius ** 2)))

    def _check_progress(self, before: Observation, after: Observation, point: Point) -> None:
        if after.popup.kind != "none" or after.rust_pixels == 0:
            self._stale_pulses = 0
            self._failed_positions.clear()
            return
        old = self._nearby_rust(before, point)
        new = self._nearby_rust(after, point)
        changed = old - new >= max(8, round(old * 0.03))
        changed |= before.rust_pixels - after.rust_pixels >= max(15, round(before.rust_pixels * 0.005))
        if changed:
            self._stale_pulses = 0
            self._failed_positions.clear()
        else:
            self._stale_pulses += 1
            if self._stale_pulses >= 2:
                self._failed_positions.append(point)
            if self._stale_pulses >= self.timings.max_stale_pulses:
                self.pause("Rust did not decrease after repeated pulses; check the preview/HSV settings")

    def tick(self) -> str:
        if not self._safe_to_act():
            return "paused"
        frame = self.capture()
        result = self.detector.analyze(frame, self._failed_positions)
        popup = result.popup
        if popup.kind == "blocked":
            self.pause("A popup covers the playfield but its X was not recognized; supply --close-template")
            return "blocked"
        if popup.kind == "close":
            assert popup.close_at is not None
            if (self._pending_close is not None and
                    math.dist(popup.close_at, self._pending_close) <= max(8, round(min(frame.shape[:2]) * 0.015))):
                return self._close_popup(popup.close_at)
            self._pending_close = popup.close_at
            return "verifying popup"
        self._pending_close = None
        self._last_closed = None
        self._close_attempts = 0
        if result.rust_pixels == 0:
            self._failed_positions.clear()
            self._stale_pulses = 0
            return "waiting for rust"
        if result.target is None:
            self.pause("Rust remains but every target failed; check whether the color range matches clean metal")
            return "blocked"
        if not self._spray(result.target, frame.shape):
            return "paused"
        self.pulse_count += 1
        self._wait(self.timings.settle)
        if not self._safe_to_act():
            return "paused"
        after = self.detector.analyze(self.capture())
        self._check_progress(result, after, result.target)
        if self.pulse_count % 10 == 0:
            log.info("%d pulses; rust pixels: %d -> %d", self.pulse_count,
                     result.rust_pixels, after.rust_pixels)
        return "sprayed" if self.active.is_set() else "blocked"
