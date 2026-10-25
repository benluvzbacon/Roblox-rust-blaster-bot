"""Cautious mouse automation around the screen detector.

The bot verifies that Rust Blaster's pink *in-game* wash cursor reaches a rusty
spot before pressing. Moving the desktop cursor alone is not proof that Roblox
received a mouse-movement event (especially when the game locks the cursor).
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

from .input import PynputPointer  # backwards-compatible export
from .vision import Detector, Observation, Point

__all__ = ["BotController", "Pointer", "PynputPointer", "Timings"]

log = logging.getLogger(__name__)


class Pointer(Protocol):
    @property
    def position(self) -> Point: ...

    def move_to(self, point: Point) -> None: ...
    def move_relative(self, delta: Point) -> None: ...
    def press(self) -> None: ...
    def release(self) -> None: ...


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
    """Each tick looks for popups, aims, then (unless testing) washes rust.

    Screen capture, pointer and focus checks are injectable so the alignment
    and safety logic can be tested without Roblox or physical mouse input.
    """

    def __init__(self, detector: Detector, capture: Callable[[], np.ndarray],
                 pointer: Pointer, origin: Point = (0, 0),
                 focused: Callable[[], bool] = lambda: True,
                 timings: Timings | None = None,
                 require_reticle: bool = True, probe_only: bool = False) -> None:
        self.detector = detector
        self.capture = capture
        self.pointer = pointer
        self.origin = origin
        self.focused = focused
        self.timings = timings if timings is not None else Timings()
        self.require_reticle = require_reticle
        self.probe_only = probe_only
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
        self.last_target: Point | None = None  # also used by --test-mouse's after image

    def resume(self) -> None:
        if not self.stopped.is_set():
            self._pending_close = None
            self._last_bot_position = None  # user may reposition the pointer while paused
            self._failed_positions.clear()
            self._stale_pulses = 0
            self.active.set()
            log.info("Running. F8 pauses, F7 stops; move to the capture's upper-left corner to stop.")

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
            self.pause("Game is not the foreground window; focus it and press F8 to resume")
            return False
        x, y = self.pointer.position
        if self.origin[0] <= x <= self.origin[0] + 8 and self.origin[1] <= y <= self.origin[1] + 8:
            self.stop()
            return False
        # A locked game may recenter the OS pointer after every relative event;
        # in that mode the *visible reticle*, not GetCursorPos, is our feedback.
        if (not self.require_reticle and self._last_bot_position is not None and
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
                self.pause("Popup remains after two close clicks; check the X or add a close template")
                return "blocked"
            self._close_attempts += 1
        else:
            self._last_closed, self._close_attempts = point, 1
        with self._input_lock:
            if not self._safe_to_act():
                return "paused"
            absolute = self._desktop_point(point)
            self.pointer.move_to(absolute)
            self._last_bot_position = self.pointer.position
            try:
                self.pointer.press()
            finally:
                self.pointer.release()
        log.info("Closed popup at %s", absolute)
        self._pending_close = None
        self._failed_positions.clear()
        self._wait(self.timings.popup_settle)
        return "closed popup"

    def _aim(self, point: Point, observed: Observation) -> Observation | None:
        """Move the in-game cursor to rust, checking screenshots along the way.

        Roblox may ignore a programmatic cursor teleport. On Windows the
        pointer sends relative SendInput motion events, and the observed pink
        circle (not the OS pointer position) closes the feedback loop.
        """
        if not self.require_reticle:
            absolute = self._desktop_point(point)
            with self._input_lock:
                if not self._safe_to_act():
                    return None
                self.pointer.move_to(absolute)
                self._last_bot_position = absolute
            self._wait(0.06)
            if not self._safe_to_act():
                return None
            if math.dist(self.pointer.position, absolute) > 35:
                self.pause("OS pointer did not reach the target; check screen scaling / --region")
                return None
            return observed  # explicitly opted out of in-game cursor verification

        if observed.reticle is None:
            self.pause("Pink in-game wash cursor not found; run --test-mouse and inspect the screenshot")
            return None
        stalled = 0
        gain = 1.0  # observed in-game pixels per injected mouse-motion count
        for attempt in range(24):
            if not self._safe_to_act() or observed.popup.kind != "none":
                return None
            assert observed.reticle is not None
            cursor = observed.reticle.center
            dx, dy = point[0] - cursor[0], point[1] - cursor[1]
            distance = math.hypot(dx, dy)
            tolerance = max(12, round(observed.reticle.radius * 0.34))
            if distance <= tolerance:
                log.debug("In-game cursor aligned at %s, target %s after %d moves", cursor, point, attempt)
                return observed
            max_step = max(40, round(min(observed.rust_mask.shape) * 0.09))
            requested = (dx / gain, dy / gain)
            scale = min(1.0, max_step / math.hypot(*requested))
            delta = (round(requested[0] * scale), round(requested[1] * scale))
            with self._input_lock:
                if not self._safe_to_act():
                    return None
                try:
                    self.pointer.move_relative(delta)
                except OSError as exc:
                    self.pause(f"Mouse movement was rejected: {exc}")
                    return None
            self._wait(0.07)
            if not self._safe_to_act():
                return None
            updated = self.detector.analyze(self.capture())
            if updated.popup.kind != "none":
                return None  # recheck the popup next tick; do not press behind it
            if updated.reticle is None:
                # A particle effect may hide a segment of the ring briefly.
                updated = self.detector.analyze(self.capture())
                if updated.reticle is None:
                    self.pause("In-game wash cursor disappeared during aiming; no click was sent")
                    return None
            shift = (updated.reticle.center[0] - cursor[0],
                     updated.reticle.center[1] - cursor[1])
            if math.hypot(*shift) < 3:
                stalled += 1
            else:
                stalled = 0
                projected = (shift[0] * delta[0] + shift[1] * delta[1]) / max(1, delta[0] ** 2 + delta[1] ** 2)
                if projected > 0.1:
                    gain = min(5.0, max(0.15, projected))
            if stalled >= 2:
                self.pause("Mouse events did not move the in-game wash cursor. "
                           "Check focus/permissions; Roblox may reject synthetic motion")
                return None
            observed = updated
        self.pause("Could not align the in-game cursor with rust after 24 movements; no click was sent")
        return None

    def _spray(self, point: Point, shape: tuple[int, ...]) -> bool:
        # _aim already positioned the brush. Do NOT warp the OS pointer again:
        # that was the original cause of clicking while the game cursor stayed put.
        absolute = self._desktop_point(point)
        try:
            with self._input_lock:
                if not self._safe_to_act():
                    return False
                self.pointer.press()
            end = time.monotonic() + self.timings.pulse
            tolerance = max(35, round(min(shape[:2]) * 0.055))
            while time.monotonic() < end:
                if not self._safe_to_act():
                    return False
                if (not self.require_reticle and
                        math.dist(self.pointer.position, absolute) > tolerance):
                    self.pause("Mouse moved during a pulse; press F8 to resume")
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
        inside = (xx - point[0]) ** 2 + (yy - point[1]) ** 2 <= radius ** 2
        return int(np.count_nonzero((patch != 0) & inside))

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
                self.pause("Rust did not decrease after repeated pulses; check alignment and HSV settings")

    def tick(self) -> str:
        if not self._safe_to_act():
            return "paused"
        frame = self.capture()
        result = self.detector.analyze(frame, self._failed_positions)
        popup = result.popup
        if self.probe_only and popup.kind != "none":
            self.pause("Mouse test does not close popups; dismiss the popup and try again")
            return "blocked"
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
            self.pause("Rust remains but every target failed; check the color range")
            return "blocked"
        self.last_target = result.target
        aimed = self._aim(result.target, result)
        if aimed is None:
            return "paused" if not self.active.is_set() else "retrying"
        if aimed.popup.kind != "none" or aimed.rust_pixels == 0:
            return "retrying"
        if self.probe_only:
            return "aligned"  # never click in this mode
        if self.require_reticle and self._nearby_rust(aimed, result.target) == 0:
            return "retrying"  # item moved or was cleaned while steering
        if not self._spray(result.target, frame.shape):
            return "paused"
        self.pulse_count += 1
        self._wait(self.timings.settle)
        if not self._safe_to_act():
            return "paused"
        after = self.detector.analyze(self.capture())
        self._check_progress(aimed, after, result.target)
        if self.pulse_count % 10 == 0:
            log.info("%d pulses; rust pixels: %d -> %d", self.pulse_count,
                     aimed.rust_pixels, after.rust_pixels)
        return "sprayed" if self.active.is_set() else "blocked"
