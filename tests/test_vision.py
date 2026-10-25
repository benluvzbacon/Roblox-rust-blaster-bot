from pathlib import Path

import cv2
import numpy as np
import pytest

from rust_blaster_bot.vision import Detector, VisionConfig, annotate

REFERENCE = Path(__file__).parent / "fixtures" / "rust_key_reference.jpg"


def screenshot():
    frame = cv2.imread(str(REFERENCE))
    assert frame is not None
    return frame


def synthetic_item(shape="plate"):
    # An orange upgrade sidebar and bottom pumpkin should NEVER be targets.
    frame = np.full((600, 900, 3), (9, 25, 7), dtype=np.uint8)
    cv2.rectangle(frame, (0, 40), (208, 560), (25, 125, 230), -1)
    cv2.circle(frame, (500, 585), 39, (18, 150, 240), -1)
    rust = (32, 65, 126)  # BGR brown/orange like the user-provided screenshot
    if shape == "plate":
        cv2.rectangle(frame, (420, 240), (660, 345), rust, -1)
    elif shape == "gear":
        cv2.circle(frame, (550, 340), 110, rust, 25)
        for angle in range(0, 360, 45):
            x = round(550 + 100 * np.cos(np.deg2rad(angle)))
            y = round(340 + 100 * np.sin(np.deg2rad(angle)))
            cv2.circle(frame, (x, y), 22, rust, -1)
    else:
        raise ValueError(shape)
    return frame


def popup_frame(close=True):
    frame = screenshot()
    # Draw a generic navy dialog over the original game screenshot.
    cv2.rectangle(frame, (315, 120), (690, 400), (45, 35, 31), -1)
    cv2.rectangle(frame, (315, 120), (690, 400), (96, 60, 49), 2)
    cv2.putText(frame, "REWARD", (350, 155), cv2.FONT_HERSHEY_SIMPLEX, 0.9, (250, 250, 250), 2)
    if close:
        cv2.circle(frame, (666, 141), 17, (30, 40, 200), -1)
        cv2.line(frame, (659, 134), (673, 148), (255, 255, 255), 3)
        cv2.line(frame, (673, 134), (659, 148), (255, 255, 255), 3)
    return frame


def test_reference_screenshot_detects_rust_not_ui():
    frame = screenshot()
    obs = Detector().analyze(frame)
    assert obs.popup.kind == "none"
    assert obs.reticle is not None
    assert np.linalg.norm(np.subtract(obs.reticle.center, (369, 195))) < 5
    assert 48 <= obs.reticle.radius <= 58
    assert 15_000 < obs.rust_pixels < 26_000
    assert obs.rust_mask[65, 20] == 0  # bright upgrades button
    assert obs.rust_mask[485, 398] == 0  # bottom pumpkin
    assert obs.target is not None
    x, y = obs.target
    assert 390 <= x <= 705 and 101 <= y <= 460
    assert obs.rust_mask[y, x] != 0
    assert not np.array_equal(annotate(frame, obs), frame)


@pytest.mark.parametrize("shape", ["plate", "gear"])
def test_not_tied_to_the_skeleton_key_shape(shape):
    obs = Detector().analyze(synthetic_item(shape))
    assert obs.popup.kind == "none"
    assert obs.reticle is None  # an orange item is not a pink wash cursor
    assert obs.rust_pixels > 2_000
    assert obs.target is not None
    x, y = obs.target
    assert 400 <= x <= 675 and 200 <= y <= 470
    assert obs.rust_mask[y, x] == 255
    assert obs.rust_mask[: , :209].sum() == 0
    assert obs.rust_mask[570:, :].sum() == 0


def test_popup_detected_and_close_x_confined_to_dialog():
    obs = Detector().analyze(popup_frame())
    assert obs.popup.kind == "close"
    assert obs.popup.source == "X on central dialog"
    assert obs.popup.close_at is not None
    assert np.linalg.norm(np.subtract(obs.popup.close_at, (666, 141))) < 10
    assert obs.target is None  # never spray behind a popup


def test_unrecognized_modal_blocks_spraying_instead_of_guessing():
    obs = Detector().analyze(popup_frame(close=False))
    assert obs.popup.kind == "blocked"
    assert obs.popup.close_at is None
    assert obs.target is None


def test_light_dialog_with_dark_close_x():
    frame = screenshot()
    cv2.rectangle(frame, (316, 122), (690, 400), (224, 224, 224), -1)
    cv2.circle(frame, (666, 141), 17, (15, 35, 190), -1)
    cv2.line(frame, (659, 134), (673, 148), (35, 35, 35), 3)
    cv2.line(frame, (673, 134), (659, 148), (35, 35, 35), 3)
    obs = Detector().analyze(frame)
    assert obs.popup.kind == "close"
    assert obs.popup.close_at is not None
    assert np.linalg.norm(np.subtract(obs.popup.close_at, (666, 141))) < 10
    assert obs.target is None


def test_custom_close_template_for_a_different_style(tmp_path):
    frame = screenshot()
    # A small light popup may not satisfy the navy-panel heuristic.
    cv2.rectangle(frame, (350, 130), (645, 290), (215, 215, 215), -1)
    cv2.rectangle(frame, (616, 145), (638, 167), (40, 40, 215), -1)
    cv2.line(frame, (621, 150), (633, 162), (255, 255, 255), 2)
    cv2.line(frame, (633, 150), (621, 162), (255, 255, 255), 2)
    template = tmp_path / "popup_x.png"
    assert cv2.imwrite(str(template), frame[141:171, 612:642])
    obs = Detector(close_templates=[template]).analyze(frame)
    assert obs.popup.kind == "close"
    assert obs.popup.source.startswith("template ")
    assert np.linalg.norm(np.subtract(obs.popup.close_at, (627, 156))) <= 3


def test_excluding_failed_spot_moves_to_another_rust_patch():
    detector = Detector()
    frame = screenshot()
    first = detector.analyze(frame)
    assert first.target is not None
    second = detector.analyze(frame, excluded=[first.target])
    assert second.target is not None
    assert np.linalg.norm(np.subtract(first.target, second.target)) > 12
    assert second.rust_pixels == first.rust_pixels


@pytest.mark.parametrize("kwargs", [
    {"roi": (0.9, 0, 0.1, 1)},
    {"hue": (-1, 60)},
    {"value": (200, 20)},
    {"saturation_min": 256},
    {"close_threshold": 0},
])
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        VisionConfig(**kwargs)
