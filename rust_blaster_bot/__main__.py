"""Run with ``python -m rust_blaster_bot``."""

from __future__ import annotations

import argparse
import ctypes
import logging
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from .automation import BotController, PynputPointer, Timings
from .vision import Detector, VisionConfig, annotate


def int_pair(text: str) -> tuple[int, int]:
    try:
        a, b = (int(part.strip()) for part in text.split(","))
        return a, b
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected two integers separated by a comma") from exc


def fractions(text: str) -> tuple[float, float, float, float]:
    try:
        a, b, c, d = (float(part.strip()) for part in text.split(","))
        return a, b, c, d
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected left,top,right,bottom fractions") from exc


def screen_region(text: str) -> dict[str, int]:
    try:
        x, y, w, h = (int(part.strip()) for part in text.split(","))
        if w <= 0 or h <= 0:
            raise ValueError()
        return {"left": x, "top": y, "width": w, "height": h}
    except ValueError as exc:
        raise argparse.ArgumentTypeError("Expected x,y,width,height (positive width and height)") from exc


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m rust_blaster_bot",
                                description="Detect and spray rust in Roblox Rust Blaster. Starts PAUSED.")
    modes = p.add_mutually_exclusive_group()
    modes.add_argument("--inspect", metavar="IMAGE", help="analyze a saved screenshot, without using the mouse")
    modes.add_argument("--preview", metavar="FILE", help="save an annotated live screenshot, without clicking")
    modes.add_argument("--screenshot", metavar="FILE", help="save a raw live screenshot (e.g. for a popup template)")
    modes.add_argument("--list-monitors", action="store_true", help="list capture monitor indices and exit")
    p.add_argument("--output", metavar="FILE", help="annotated output for --inspect")
    p.add_argument("--monitor", type=int, default=1, help="mss monitor number (default: 1)")
    p.add_argument("--region", type=screen_region, help="game rectangle x,y,width,height on desktop; overrides --monitor")
    p.add_argument("--roi", type=fractions, default=(0.24, 0.20, 0.89, 0.93),
                   help="safe playfield fractions left,top,right,bottom")
    p.add_argument("--hue", type=int_pair, default=(4, 24), help="rust OpenCV hue range, 0..179 (default: 4,24)")
    p.add_argument("--sat-min", type=int, default=85, help="rust minimum saturation, 0..255")
    p.add_argument("--value", type=int_pair, default=(25, 190), help="rust brightness range, 0..255")
    p.add_argument("--close-template", action="append", default=[], metavar="CROP.PNG",
                   help="crop tightly around a real popup X; repeat for multiple types")
    p.add_argument("--close-threshold", type=float, default=0.91, help="minimum close-template similarity")
    p.add_argument("--pulse", type=float, default=0.35, help="seconds to hold the wash beam per target")
    p.add_argument("--window-title", default="Roblox", help="required foreground window/app name (default: Roblox)")
    p.add_argument("--no-focus-check", action="store_true", help="disable foreground guard (use with caution)")
    return p


def _save(filename: str, image: np.ndarray) -> None:
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), image):
        raise OSError(f"Could not write {path}")
    print(f"Saved {path}")


def _dpi_awareness() -> None:
    if sys.platform == "win32":
        # Make mss screenshot pixels and pynput mouse coordinates agree when
        # Windows display scaling is not 100%. mss may also set this itself.
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass


class FocusGuard:
    """Check the active window; cache briefly to avoid frequent OS calls."""

    def __init__(self, title: str) -> None:
        self.title = title.casefold()
        self.checked_at = 0.0
        self.was_focused = False
        if sys.platform == "win32":
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            # HWND is pointer-sized: the default ctypes c_int truncates it on
            # 64-bit Windows, making the focus safety check fail unpredictably.
            user32.GetForegroundWindow.restype = wintypes.HWND
            user32.GetWindowTextW.argtypes = (wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int)
            user32.GetWindowTextW.restype = ctypes.c_int

    def __call__(self) -> bool:
        now = time.monotonic()
        if now - self.checked_at < 0.15:
            return self.was_focused
        self.checked_at = now
        if sys.platform == "win32":
            hwnd = ctypes.windll.user32.GetForegroundWindow()
            buf = ctypes.create_unicode_buffer(512)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, len(buf))
            name = buf.value
        elif sys.platform == "darwin":
            try:
                result = subprocess.run(
                    ["osascript", "-e", 'tell application "System Events" to get name of first application process whose frontmost is true'],
                    capture_output=True, text=True, timeout=0.8, check=False)
                name = result.stdout.strip() if result.returncode == 0 else ""
            except (OSError, subprocess.TimeoutExpired):
                name = ""
        else:
            return False
        self.was_focused = self.title in name.casefold()
        return self.was_focused


def _run_live(detector: Detector, args: argparse.Namespace) -> int:
    if sys.platform not in ("win32", "darwin") and not (args.preview or args.screenshot or args.list_monitors):
        print("Live mouse control is supported on Windows and macOS; --inspect works anywhere.", file=sys.stderr)
        return 2
    _dpi_awareness()
    try:
        import mss
        with mss.mss() as sct:
            if args.list_monitors:
                for index, monitor in enumerate(sct.monitors[1:], start=1):
                    print(f"{index}: {monitor}")
                return 0
            if args.region is not None:
                bounds = args.region
            elif 1 <= args.monitor < len(sct.monitors):
                bounds = sct.monitors[args.monitor]
            else:
                raise ValueError(f"Monitor {args.monitor} not found; use --list-monitors")

            def capture() -> np.ndarray:
                return cv2.cvtColor(np.asarray(sct.grab(bounds)), cv2.COLOR_BGRA2BGR)

            if args.screenshot:
                _save(args.screenshot, capture())
                return 0
            if args.preview:
                frame = capture()
                _save(args.preview, annotate(frame, detector.analyze(frame)))
                return 0

            from pynput.keyboard import Key, Listener
            pointer = PynputPointer()
            focused = (lambda: True) if args.no_focus_check else FocusGuard(args.window_title)
            if args.no_focus_check:
                print("WARNING: foreground protection disabled; keep the game visible and focused.")
            bot = BotController(detector, capture, pointer,
                                origin=(bounds["left"], bounds["top"]),
                                focused=focused, timings=Timings(pulse=args.pulse))
            pressed: set[object] = set()

            def on_press(key: object) -> None:
                if key in pressed:
                    return  # ignore keyboard repeat while holding a function key
                pressed.add(key)
                if key == Key.f8:
                    if bot.active.is_set():
                        bot.pause("Paused with F8")
                    else:
                        bot.resume()
                elif key == Key.f7:
                    bot.stop()

            def on_release(key: object) -> None:
                pressed.discard(key)

            print("Bot PAUSED. Focus Roblox, then F8 to start/pause; F7, Ctrl+C or mouse to capture's upper-left corner to stop.")
            with Listener(on_press=on_press, on_release=on_release):
                try:
                    while not bot.stopped.is_set():
                        if bot.active.is_set():
                            outcome = bot.tick()
                            if outcome == "waiting for rust":
                                time.sleep(0.35)
                            elif outcome == "verifying popup":
                                time.sleep(0.09)
                        else:
                            time.sleep(0.08)
                except KeyboardInterrupt:
                    print("\nStopped by Ctrl+C.")
                finally:
                    bot.stop()  # always release left mouse button, even on an exception
        return 0
    except (ImportError, OSError, ValueError) as exc:
        print(f"Cannot start: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.output and not args.inspect:
        print("--output requires --inspect", file=sys.stderr)
        return 2
    try:
        config = VisionConfig(args.roi, args.hue, args.sat_min, args.value, args.close_threshold)
        detector = Detector(config, args.close_template)
        Timings(pulse=args.pulse)  # validate before starting any screen or input hooks
    except ValueError as exc:
        print(f"Invalid settings: {exc}", file=sys.stderr)
        return 2
    if args.inspect:
        frame = cv2.imread(args.inspect, cv2.IMREAD_COLOR)
        if frame is None:
            print(f"Cannot read screenshot: {args.inspect}", file=sys.stderr)
            return 2
        result = detector.analyze(frame)
        print(f"playfield={result.roi}  rust_pixels={result.rust_pixels}  "
              f"target={result.target}  popup={result.popup.kind}")
        if result.popup.source:
            print(f"popup: {result.popup.source}, close_at={result.popup.close_at}")
        if args.output:
            try:
                _save(args.output, annotate(frame, result))
            except OSError as exc:
                print(str(exc), file=sys.stderr)
                return 2
        return 0
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    return _run_live(detector, args)


if __name__ == "__main__":
    raise SystemExit(main())
