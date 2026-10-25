"""Desktop mouse drivers.

Windows Roblox may read mouse *motion events*, not just the OS cursor position.
Setting ``pynput.mouse.Controller.position`` teleports the pointer without
necessarily delivering a movement event to the game. Win32Pointer uses the
standard SendInput mouse stream, including relative movement for aiming. It
cannot override an application's decision to reject synthetic input.
"""

from __future__ import annotations

import ctypes
import sys

from .vision import Point

_MOUSE = 0
_MOVE = 0x0001
_LEFTDOWN = 0x0002
_LEFTUP = 0x0004
_ABSOLUTE = 0x8000
_VIRTUALDESK = 0x4000


class MouseInput(ctypes.Structure):
    # Win32 LONG and DWORD are 32 bits on both 32- and 64-bit Windows.
    _fields_ = [
        ("dx", ctypes.c_int32),
        ("dy", ctypes.c_int32),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class Input(ctypes.Structure):
    _fields_ = [("type", ctypes.c_uint32), ("mi", MouseInput)]


class ScreenPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class Win32Pointer:
    """SendInput-backed pointer with relative moves for game cursor control.

    ``api`` is injectable for headless tests; ordinary callers use user32.
    Display coordinates are physical pixels (DPI awareness is set at startup).
    """

    def __init__(self, api: object | None = None) -> None:
        if api is None:
            if sys.platform != "win32":
                raise OSError("Win32Pointer is only available on Windows")
            api = ctypes.WinDLL("user32", use_last_error=True)
            api.SendInput.argtypes = (ctypes.c_uint32, ctypes.POINTER(Input), ctypes.c_int)
            api.SendInput.restype = ctypes.c_uint32
            api.GetCursorPos.argtypes = (ctypes.POINTER(ScreenPoint),)
            api.GetCursorPos.restype = ctypes.c_int
            api.GetSystemMetrics.argtypes = (ctypes.c_int,)
            api.GetSystemMetrics.restype = ctypes.c_int
        self.api = api
        self._held = False

    @property
    def position(self) -> Point:
        point = ScreenPoint()
        if not self.api.GetCursorPos(ctypes.byref(point)):
            raise OSError("GetCursorPos failed")
        return int(point.x), int(point.y)

    def _send(self, dx: int, dy: int, flags: int) -> None:
        event = Input(_MOUSE, MouseInput(dx, dy, 0, flags, 0, 0))
        if self.api.SendInput(1, ctypes.byref(event), ctypes.sizeof(Input)) != 1:
            error = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
            raise OSError(error, "SendInput did not deliver a mouse event")

    def move_relative(self, delta: Point) -> None:
        dx, dy = map(round, delta)
        if dx or dy:
            self._send(dx, dy, _MOVE)

    def move_to(self, point: Point) -> None:
        left = self.api.GetSystemMetrics(76)  # SM_XVIRTUALSCREEN
        top = self.api.GetSystemMetrics(77)   # SM_YVIRTUALSCREEN
        width = self.api.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
        height = self.api.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        if width <= 1 or height <= 1:
            raise OSError("Could not read the virtual desktop size")
        x = min(65535, max(0, round((point[0] - left) * 65535 / (width - 1))))
        y = min(65535, max(0, round((point[1] - top) * 65535 / (height - 1))))
        self._send(x, y, _MOVE | _ABSOLUTE | _VIRTUALDESK)

    def press(self) -> None:
        if not self._held:
            self._held = True
            try:
                self._send(0, 0, _LEFTDOWN)
            except Exception:
                self.release()
                raise

    def release(self) -> None:
        if self._held:
            try:
                self._send(0, 0, _LEFTUP)
            finally:
                self._held = False


class PynputPointer:
    """Quartz/pynput input on macOS (also useful as a fallback backend)."""

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

    def move_relative(self, delta: Point) -> None:
        self.mouse.move(round(delta[0]), round(delta[1]))

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
            try:
                self.mouse.release(self.button)
            finally:
                self._held = False
