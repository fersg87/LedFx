"""Unit tests for the transient colour override applied to virtuals.

The override recolours a running effect without touching its config, so these
tests care about two things: that the recolour maths preserves the effect's
animation, and that the data it produces is sized to match the frame it will be
combined with.
"""

import threading

import numpy as np
import pytest

from ledfx.effects import Effect
from ledfx.effects.gradient import GradientEffect
from ledfx.events import Event
from ledfx.virtuals import Virtual, _apply_gradient_override

RED_TO_BLUE = "linear-gradient(90deg, rgb(255, 0, 0) 0%, rgb(0, 0, 255) 100%)"


class _DummyEvents:
    """Collects fired events so tests can assert on them."""

    def __init__(self):
        self.fired = []

    def fire_event(self, event):
        self.fired.append(event)


class _DummyLedFx:
    def __init__(self):
        self.events = _DummyEvents()
        self.config = {"global_brightness": 1.0}
        self.config_dir = ""


class _StubVirtual:
    """Minimal stand-in exposing only what the override code path touches.

    Borrows the real methods off :class:`~ledfx.virtuals.Virtual` so the tests
    exercise production code rather than a copy of it.
    """

    _GRADIENT_LUT_SIZE = Virtual._GRADIENT_LUT_SIZE
    _build_override_frame = Virtual._build_override_frame
    _apply_color_override_to_effect = Virtual._apply_color_override_to_effect
    set_color_override = Virtual.set_color_override
    clear_color_override = Virtual.clear_color_override
    color_override = Virtual.color_override

    def __init__(self, pixel_count=8, group_size=1, effect=None):
        self.id = "stub-virtual"
        self.lock = threading.RLock()
        self._ledfx = _DummyLedFx()
        self.pixel_count = pixel_count
        # Pixel grouping shrinks what the effect renders; the override frame
        # has to follow the effect, not the physical strip.
        self.effective_pixel_count = pixel_count // group_size
        self._active_effect = effect
        self._color_override = None
        self._color_override_frame = None
        self._color_override_lut = None


@Effect.no_registration
class _PlainGradientEffect(GradientEffect):
    """Concrete GradientEffect subclass, so config plumbing resolves normally.

    GradientEffect is an abstract base and cannot be instantiated directly.
    """

    NAME = "TestGradient"


def _make_gradient_effect(pixel_count=64):
    """Build a gradient effect with defaults, without activating it."""
    effect = _PlainGradientEffect(_DummyLedFx(), {})
    effect.pixels = np.zeros((pixel_count, 3))
    return effect


# ---------------------------------------------------------------------------
# _apply_gradient_override maths
# ---------------------------------------------------------------------------


def test_gradient_override_preserves_per_pixel_brightness():
    """A dimmed pixel stays dimmed after being recoloured."""
    lut = np.tile([255.0, 255.0, 255.0], (4, 1))
    # Two saturated pixels at different brightnesses
    frame = np.array([[255.0, 0.0, 0.0], [51.0, 0.0, 0.0]])

    out = _apply_gradient_override(frame, lut, 4)

    # Brightness ratio between the two pixels is unchanged (255 vs 51 = 20%)
    assert out[0].max() == pytest.approx(255.0)
    assert out[1].max() == pytest.approx(51.0)


def test_gradient_override_maps_hue_to_lut_position():
    """Pixel hue selects the position sampled from the gradient LUT."""
    lut_size = 100
    # Each LUT entry is tagged with its own index in the red channel
    lut = np.array([[float(i), 0.0, 0.0] for i in range(lut_size)])
    # Pure red is hue 0.0; pure blue is hue 2/3
    frame = np.array([[255.0, 0.0, 0.0], [0.0, 0.0, 255.0]])

    out = _apply_gradient_override(frame, lut, lut_size)

    assert out[0][0] == pytest.approx(0.0)
    assert out[1][0] == pytest.approx(int((2 / 3) * (lut_size - 1)))


def test_gradient_override_handles_greyscale_pixels():
    """Achromatic pixels take LUT index 0 without dividing by zero."""
    lut = np.array([[10.0, 20.0, 30.0], [0.0, 0.0, 0.0]])
    frame = np.array([[255.0, 255.0, 255.0], [0.0, 0.0, 0.0]])

    out = _apply_gradient_override(frame, lut, 2)

    assert np.allclose(out[0], [10.0, 20.0, 30.0])
    assert np.allclose(out[1], [0.0, 0.0, 0.0])


# ---------------------------------------------------------------------------
# _build_override_frame sizing
# ---------------------------------------------------------------------------


def test_solid_override_frame_matches_pixel_count():
    virtual = _StubVirtual(pixel_count=8)
    virtual._color_override = "#ff0000"

    spatial, lut = virtual._build_override_frame()

    assert spatial.shape == (8, 3)
    assert lut is None
    assert np.allclose(spatial[0], [255.0, 0.0, 0.0])


def test_solid_override_frame_follows_grouping():
    """With grouping on, the override must be sized to the effect, not the strip.

    Regression test: sizing this with pixel_count produced an array that could
    not be broadcast against the effect-sized assembled frame, which killed the
    render thread.
    """
    virtual = _StubVirtual(pixel_count=12, group_size=4)
    virtual._color_override = "#00ff00"

    spatial, _ = virtual._build_override_frame()

    assert spatial.shape == (3, 3)  # 12 pixels / group size 4

    # And the shape the render loop actually relies on: the multiply must work
    assembled = np.full((virtual.effective_pixel_count, 3), 128.0)
    luminance = np.max(assembled, axis=1, keepdims=True) / 255.0
    assert (spatial * luminance).shape == assembled.shape


def test_gradient_override_builds_lut_and_spatial_frame():
    virtual = _StubVirtual(pixel_count=6)
    virtual._color_override = RED_TO_BLUE

    spatial, lut = virtual._build_override_frame()

    assert spatial.shape == (6, 3)
    assert lut.shape == (Virtual._GRADIENT_LUT_SIZE, 3)
    # Gradient runs red -> blue
    assert lut[0][0] > lut[0][2]
    assert lut[-1][2] > lut[-1][0]


def test_override_frame_is_none_without_pixels():
    virtual = _StubVirtual(pixel_count=0)
    virtual._color_override = "#ff0000"

    assert virtual._build_override_frame() == (None, None)


def test_invalid_override_falls_back_to_white():
    virtual = _StubVirtual(pixel_count=2)
    virtual._color_override = "not-a-color"

    spatial, lut = virtual._build_override_frame()

    assert lut is None
    assert np.allclose(spatial[0], [255.0, 255.0, 255.0])


# ---------------------------------------------------------------------------
# Routing: gradient effects vs everything else
# ---------------------------------------------------------------------------


def test_gradient_effect_override_is_injected_not_post_processed():
    """Gradient effects recolour at the source, so no frame work is queued."""
    effect = _make_gradient_effect()
    virtual = _StubVirtual(effect=effect)

    virtual.set_color_override("#ff0000")

    assert effect._gradient_override == "#ff0000"
    assert virtual._color_override_frame is None
    assert virtual._color_override_lut is None


def test_non_gradient_effect_override_is_post_processed():
    """Effects with no gradient config get the render-time recolour instead."""
    virtual = _StubVirtual(effect=object())

    virtual.set_color_override("#ff0000")

    assert virtual._color_override_frame is not None
    assert virtual._color_override_frame.shape == (8, 3)


def test_clear_restores_gradient_effect_config():
    effect = _make_gradient_effect()
    original = effect._config["gradient"]
    virtual = _StubVirtual(effect=effect)

    virtual.set_color_override("#ff0000")
    virtual.clear_color_override()

    assert effect._gradient_override is None
    # The effect's own config was never touched
    assert effect._config["gradient"] == original


def test_clear_drops_post_processing_state():
    virtual = _StubVirtual(effect=object())

    virtual.set_color_override(RED_TO_BLUE)
    assert virtual._color_override_lut is not None

    virtual.clear_color_override()

    assert virtual._color_override is None
    assert virtual._color_override_frame is None
    assert virtual._color_override_lut is None


def test_override_never_writes_to_effect_config():
    """The whole point of an override: the show is recoverable."""
    effect = _make_gradient_effect()
    before = dict(effect._config)
    virtual = _StubVirtual(effect=effect)

    virtual.set_color_override(RED_TO_BLUE)

    assert effect._config == before


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


def test_setting_override_fires_event():
    virtual = _StubVirtual(effect=object())

    virtual.set_color_override("#ff0000")

    (event,) = virtual._ledfx.events.fired
    assert event.event_type == Event.VIRTUAL_COLOR_OVERRIDE
    assert event.virtual_id == "stub-virtual"
    assert event.color_override == "#ff0000"


def test_clearing_override_fires_event_with_none():
    virtual = _StubVirtual(effect=object())

    virtual.set_color_override("#ff0000")
    virtual.clear_color_override()

    assert virtual._ledfx.events.fired[-1].color_override is None


def test_clearing_without_an_override_is_a_noop():
    virtual = _StubVirtual(effect=object())

    virtual.clear_color_override()

    assert virtual._ledfx.events.fired == []


def test_color_override_property_reports_current_state():
    virtual = _StubVirtual(effect=object())

    assert virtual.color_override is None
    virtual.set_color_override("#ff0000")
    assert virtual.color_override == "#ff0000"
    virtual.clear_color_override()
    assert virtual.color_override is None


# ---------------------------------------------------------------------------
# GradientEffect override behaviour
# ---------------------------------------------------------------------------


def test_set_gradient_override_shadows_config_gradient():
    effect = _make_gradient_effect()
    effect._assert_gradient()
    configured_curve = effect._gradient_curve.copy()

    effect.set_gradient_override("#ff0000")
    effect._assert_gradient()

    assert not np.allclose(effect._gradient_curve, configured_curve)


def test_clear_gradient_override_restores_config_gradient():
    effect = _make_gradient_effect()
    effect._assert_gradient()
    configured_curve = effect._gradient_curve.copy()

    effect.set_gradient_override("#ff0000")
    effect._assert_gradient()
    effect.clear_gradient_override()
    effect._assert_gradient()

    assert np.allclose(effect._gradient_curve, configured_curve)


def test_config_update_does_not_drop_an_active_override():
    """A config change while gelled must not reveal the underlying colour."""
    effect = _make_gradient_effect()
    effect.set_gradient_override("#ff0000")

    effect.config_updated(effect._config)
    effect._assert_gradient()

    assert effect._gradient_override == "#ff0000"
    # Curve is solid red across its length
    assert np.allclose(effect._gradient_curve[0], 255.0)
    assert np.allclose(effect._gradient_curve[1], 0.0)
