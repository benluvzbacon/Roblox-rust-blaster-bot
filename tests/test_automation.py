import numpy as np
import pytest
from test_vision import popup_frame, screenshot, synthetic_item

from rust_blaster_bot.automation import BotController, Timings
from rust_blaster_bot.vision import Detector


class FakePointer:
    def __init__(self, position=(100, 100)):
        self._position = position
        self.held = False
        self.actions = []
        self.interfere = False
        self.on_press = None

    @property
    def position(self):
        if self.interfere and self.held:
            return self._position[0] + 200, self._position[1]
        return self._position

    def move_to(self, point):
        self._position = point
        self.actions.append(("move", point))

    def press(self):
        self.held = True
        self.actions.append(("press", None))
        if self.on_press is not None:
            self.on_press()

    def release(self):
        if self.held:
            self.held = False
            self.actions.append(("release", None))


class FakeCapture:
    def __init__(self, *frames):
        self.frames = list(frames)
        self.index = 0

    def __call__(self):
        frame = self.frames[min(self.index, len(self.frames) - 1)]
        self.index += 1
        return frame.copy()


def controller(frame, *, origin=(0, 0), focus=lambda: True, max_stale=10):
    pointer = FakePointer(position=(120, 100))
    bot = BotController(Detector(), FakeCapture(frame), pointer, origin, focus,
                        Timings(pulse=0.02, settle=0, popup_settle=0,
                                max_stale_pulses=max_stale))
    return bot, pointer


def test_starts_paused_then_sprays_with_screen_origin_and_releases():
    bot, pointer = controller(synthetic_item(), origin=(125, 60))
    assert bot.tick() == "paused"
    assert pointer.actions == []
    bot.resume()
    assert bot.tick() == "sprayed"
    assert pointer.actions[0][0] == "move"
    x, y = pointer.actions[0][1]
    assert x > 125 + 400 and y > 60 + 200
    assert [action for action, _ in pointer.actions] == ["move", "press", "release"]
    assert not pointer.held
    bot.stop()
    assert bot.stopped.is_set()


def test_popup_requires_two_consistent_frames_before_clicking():
    bot, pointer = controller(popup_frame())
    bot.resume()
    assert bot.tick() == "verifying popup"
    assert pointer.actions == []
    assert bot.tick() == "closed popup"
    assert [action for action, _ in pointer.actions] == ["move", "press", "release"]
    assert np.linalg.norm(np.subtract(pointer.actions[0][1], (666, 141))) < 10
    # The same stubborn popup is clicked no more than twice, then pauses.
    assert bot.tick() == "verifying popup"
    assert bot.tick() == "closed popup"
    assert bot.tick() == "verifying popup"
    assert bot.tick() == "blocked"
    assert not bot.active.is_set()
    assert sum(action == "press" for action, _ in pointer.actions) == 2


def test_popup_disappearing_during_confirmation_is_not_clicked():
    pointer = FakePointer()
    bot = BotController(Detector(), FakeCapture(popup_frame(), screenshot()), pointer,
                        timings=Timings(pulse=0.02, settle=0, popup_settle=0))
    bot.resume()
    assert bot.tick() == "verifying popup"
    assert bot.tick() == "sprayed"  # now it is the actual rust, not the popup
    assert sum(action == "press" for action, _ in pointer.actions) == 1


def test_unknown_popup_pauses_with_no_mouse_input():
    bot, pointer = controller(popup_frame(close=False))
    bot.resume()
    assert bot.tick() == "blocked"
    assert not bot.active.is_set()
    assert pointer.actions == []


def test_foreground_guard_prevents_clicks():
    bot, pointer = controller(synthetic_item(), focus=lambda: False)
    bot.resume()
    assert bot.tick() == "paused"
    assert not bot.active.is_set()
    assert pointer.actions == []


def test_user_mouse_movement_pauses_and_releases_immediately():
    bot, pointer = controller(synthetic_item())
    pointer.interfere = True
    bot.resume()
    assert bot.tick() == "paused"
    assert not bot.active.is_set()
    assert not pointer.held
    assert [action for action, _ in pointer.actions] == ["move", "press", "release"]


def test_user_mouse_movement_between_pulses_does_not_get_overwritten():
    bot, pointer = controller(synthetic_item())
    bot.resume()
    assert bot.tick() == "sprayed"
    actions_before = pointer.actions.copy()
    pointer._position = (200, 100)  # hand moved away before the next screenshot
    assert bot.tick() == "paused"
    assert not bot.active.is_set()
    assert pointer.actions == actions_before


def test_hotkey_style_pause_while_pressed_releases_button():
    bot, pointer = controller(synthetic_item())
    pointer.on_press = lambda: bot.pause("hotkey")
    bot.resume()
    assert bot.tick() == "paused"
    assert not pointer.held
    assert not bot.active.is_set()


def test_input_exception_still_releases_left_button():
    bot, pointer = controller(synthetic_item())

    def broken_press():
        raise RuntimeError("input failed")

    pointer.on_press = broken_press
    bot.resume()
    with pytest.raises(RuntimeError, match="input failed"):
        bot.tick()
    assert not pointer.held
    assert pointer.actions[-1][0] == "release"


def test_no_progress_does_not_result_in_endless_spraying():
    bot, pointer = controller(synthetic_item(), max_stale=3)
    bot.resume()
    assert bot.tick() == "sprayed"
    assert bot.tick() == "sprayed"
    assert bot.tick() == "blocked"
    assert not bot.active.is_set()
    assert not pointer.held
    assert sum(action == "press" for action, _ in pointer.actions) == 3


def test_empty_playfield_waits_without_clicking():
    frame = np.full((600, 900, 3), (10, 20, 5), dtype=np.uint8)
    bot, pointer = controller(frame)
    bot.resume()
    assert bot.tick() == "waiting for rust"
    assert pointer.actions == []
