"""Win32 SendInput event layout and coordinate mapping, tested without Windows."""

import ctypes

import pytest

from rust_blaster_bot.input import (
    _ABSOLUTE,
    _LEFTDOWN,
    _LEFTUP,
    _MOVE,
    _VIRTUALDESK,
    Input,
    ScreenPoint,
    Win32Pointer,
)


class FakeUser32:
    def __init__(self):
        self.events = []
        self.metrics = {76: -1920, 77: 0, 78: 3840, 79: 1080}
        self.cursor = (-960, 400)
        self.fail_next = False

    def GetSystemMetrics(self, index):
        return self.metrics[index]

    def GetCursorPos(self, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(ScreenPoint)).contents
        point.x, point.y = self.cursor
        return 1

    def SendInput(self, count, pointer, size):
        assert count == 1
        assert size == ctypes.sizeof(Input)
        event = ctypes.cast(pointer, ctypes.POINTER(Input)).contents
        flags = int(event.mi.dwFlags)
        dx, dy = int(event.mi.dx), int(event.mi.dy)
        self.events.append((dx, dy, flags))
        if self.fail_next:
            self.fail_next = False
            return 0
        if flags & _MOVE:
            if flags & _ABSOLUTE:
                self.cursor = (round(-1920 + dx * 3839 / 65535),
                               round(dy * 1079 / 65535))
            else:
                self.cursor = (self.cursor[0] + dx, self.cursor[1] + dy)
        return 1


def test_relative_move_delivers_mouse_motion_event_without_cursor_teleport():
    api = FakeUser32()
    pointer = Win32Pointer(api)
    pointer.move_relative((34, -19))
    assert api.events == [(34, -19, _MOVE)]
    assert pointer.position == (-926, 381)
    pointer.move_relative((0, 0))
    assert len(api.events) == 1


def test_absolute_move_uses_virtual_desktop_coordinates():
    api = FakeUser32()
    pointer = Win32Pointer(api)
    pointer.move_to((-1000, 500))
    dx, dy, flags = api.events[-1]
    assert flags == _MOVE | _ABSOLUTE | _VIRTUALDESK
    assert 0 <= dx <= 65535 and 0 <= dy <= 65535
    assert pointer.position == (-1000, 500)


def test_press_release_emits_matching_events_and_release_is_idempotent():
    api = FakeUser32()
    pointer = Win32Pointer(api)
    pointer.press()
    pointer.press()
    pointer.release()
    pointer.release()
    assert [event[2] for event in api.events] == [_LEFTDOWN, _LEFTUP]


def test_failed_press_attempts_to_release_and_reports_error():
    api = FakeUser32()
    pointer = Win32Pointer(api)
    api.fail_next = True
    with pytest.raises(OSError, match="SendInput"):
        pointer.press()
    assert [event[2] for event in api.events] == [_LEFTDOWN, _LEFTUP]
    pointer.release()  # no further events
    assert len(api.events) == 2
