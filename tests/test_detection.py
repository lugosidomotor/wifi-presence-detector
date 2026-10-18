import json

import numpy as np

from wifisense.detector import Baseline, HumanScorer, LinkDetector, PresenceTracker
from wifisense.features import extract, mechanical, profile_shift
from wifisense.localize import Localizer


def test_mechanical_tone_versus_noise():
    rng = np.random.default_rng(1)
    t = np.arange(150) / 50
    fan = np.sin(2 * np.pi * 9 * t) + rng.normal(0, 0.05, len(t))
    walk = np.cumsum(rng.normal(0, 1, len(t)))  # slow random wandering
    assert mechanical(t, fan) > 0.6
    assert mechanical(t, walk) < 0.15
    assert mechanical(t, rng.normal(0, 1, len(t))) < 0.15


def test_profile_shift_ignores_gain():
    base = np.linspace(1, 2, 52)
    assert profile_shift(base * 1.3, base) < 1e-9
    assert profile_shift(base[::-1], base) > 1.5


def test_extract_needs_samples():
    assert extract(np.array([0.0, 0.1]), np.array([-50.0, -51.0]), None) is None
    feats = extract(np.arange(30) / 10, np.full(30, -50.0), None)
    assert feats["rssi_std"] == 0 and feats["rssi_mean"] == -50


def rssi_feats(std, mean=-60.0):
    return {"rssi_std": std, "rssi_mean": mean, "mech": 0.0}


def test_link_detector_scores_against_calibration(demo_cfg):
    calm = [rssi_feats(0.5 + 0.02 * (i % 5)) for i in range(40)]
    detector = LinkDetector(demo_cfg, Baseline.from_features(calm, "calibration"))
    assert detector.calibrated
    quiet = detector.update(rssi_feats(0.52), empty=True)
    busy = detector.update(rssi_feats(2.5), empty=False)
    shifted = detector.update(rssi_feats(0.5, mean=-66), empty=False)
    assert quiet["motion"] < 0.2
    assert busy["motion"] > 2
    assert shifted["presence"] > 1 and shifted["motion"] < 0.2


def test_auto_baseline_ignores_occupied_part(demo_cfg):
    detector = LinkDetector(demo_cfg)
    rng = np.random.default_rng(3)
    # 60% empty room, 40% somebody walking: the quiet part must define the baseline.
    for i in range(200):
        std = rng.normal(0.5, 0.05) if i % 5 < 3 else rng.normal(2.5, 0.5)
        detector.update(rssi_feats(std), empty=False)
    assert detector.ready and not detector.calibrated
    assert detector.update(rssi_feats(0.55), empty=False)["motion"] < 0.5
    assert detector.update(rssi_feats(2.5), empty=False)["motion"] > 1


def test_baseline_roundtrip():
    feats = [{"rssi_std": 0.5, "rssi_mean": -60.0, "csi_std": 0.02, "csi_profile": np.linspace(1, 2, 8)}] * 10
    baseline = Baseline.from_features(feats, "calibration", created="x")
    again = Baseline.from_json(json.loads(json.dumps(baseline.to_json())))
    assert again.stats == baseline.stats and again.origin == "calibration"
    np.testing.assert_allclose(again.profile, baseline.profile, atol=1e-6)


def test_human_scorer_rejects_spike_and_machine(demo_cfg):
    tick = demo_cfg["detection"]["tick_seconds"]
    walker, spike, fan = HumanScorer(demo_cfg), HumanScorer(demo_cfg), HumanScorer(demo_cfg)
    for i in range(16):
        now = i * tick
        w = walker.update(now, 2.5 + (i % 3) * 0.5, 0.05)
        s = spike.update(now, 3.0 if i in (8, 9) else 0.1, 0.0)
        f = fan.update(now, 1.6, 0.9)
    assert w["confirmed"] and w["likelihood"] > 0.9
    assert not s["confirmed"]
    assert not f["confirmed"] and f["likelihood"] < 0.5


def test_tracker_episode_lifecycle(demo_cfg):
    demo_cfg["detection"]["hold_seconds"] = 5
    tracker = PresenceTracker(demo_cfg)
    human = {"likelihood": 0.9, "confirmed": True}
    calm = {"likelihood": 0.0, "confirmed": False}
    assert tracker.update(0, 0, 0, calm, ready=False, calibrating=False) == (None, None)
    assert tracker.state == "learning"
    tracker.update(0.5, 0, 0, calm, ready=True, calibrating=False)
    assert tracker.state == "empty"
    started, _ = tracker.update(1.0, 2, 0, human, True, False)
    assert started is None  # one tick above threshold is not enough (trigger_seconds = 1)
    started, _ = tracker.update(1.5, 2, 0, human, True, False)
    assert started is not None and tracker.state == "motion"
    tracker.update(2.0, 0.1, 0, calm, True, False)
    assert tracker.state == "presence"
    _, ended = tracker.update(4.0, 0.1, 0, calm, True, False)
    assert ended is None
    _, ended = tracker.update(7.5, 0.1, 0, calm, True, False)
    assert ended is not None and ended["confirmed"] and ended["reason"] == "calm"
    assert tracker.state == "empty"


def test_tracker_static_shift_expires(demo_cfg):
    demo_cfg["detection"]["static_max_seconds"] = 20
    tracker = PresenceTracker(demo_cfg)
    calm = {"likelihood": 0.0, "confirmed": False}
    tracker.update(0, 0, 0, calm, True, False)
    tracker.update(0.5, 2, 0, calm, True, False)
    tracker.update(1.0, 2, 0, calm, True, False)
    t = 1.5
    ended = None
    while ended is None and t < 100:
        _, ended = tracker.update(t, 0.1, 2.0, calm, True, False)  # sitting still in the link
        t += 0.5
    assert ended["reason"] == "static" and 20 <= t <= 23


def test_localizer_band_for_one_link_and_point_for_three(demo_cfg):
    localizer = Localizer(demo_cfg)
    heat, position = localizer.update({"pi-router": {"motion": 0.1}, "window-router": {"motion": 0.1},
                                       "door-router": {"motion": 0.1}})
    assert heat is None and position is None
    heat, position = localizer.update({"pi-router": {"motion": 0.1}, "window-router": {"motion": 0.1},
                                       "door-router": {"motion": 2.5}})
    # The door link runs along x ≈ 0.4: the estimate sits near it, away from the calm links.
    assert position is not None and position[0] < 1.2 and position[2] > 1.5
    assert heat.max() == 1.0
