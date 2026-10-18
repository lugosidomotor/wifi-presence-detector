import json

from fastapi.testclient import TestClient

from wifisense.web import create_app


def test_calibration_is_saved_and_reloaded(harness, demo_cfg):
    harness.run(5)
    harness.calibrate(20)
    path = harness.rt.calibration_path
    saved = json.loads(path.read_text())
    assert path.name == "wifi-calibration-sim.json"  # never mixed up with a real installation
    assert set(saved["links"]) == {"pi-router", "window-router", "door-router"}
    assert all(d.calibrated for d in harness.rt.detectors.values())
    assert "calibrating" in harness.states and harness.states[-1] == "empty"
    from conftest import Harness
    again = Harness(demo_cfg)
    assert all(d.calibrated for d in again.rt.detectors.values())
    again.rt.store.close()


def test_walking_person_raises_one_alarm_when_armed(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.run(20)
    assert harness.rt.tracker.state == "empty"
    harness.rt.settings.arm_delay = 0
    harness.rt.arm(True)
    harness.rt.sim.action("enter")
    latest = harness.run(15)
    assert latest["alarm"] is True
    assert latest["episode"]["label"] == "ember"
    harness.rt.sim.action("leave")
    harness.run(10)
    harness.run(harness.rt.tracker.hold + 5)
    assert harness.rt.tracker.state == "empty"
    events = harness.rt.store.list_events()
    assert len(events) == 1
    event = events[0]
    assert event["state"] == "closed" and event["alarm"] is True and event["label"] == "ember"
    assert event["notification_status"] == "disabled"  # no ntfy/webhook configured in tests
    assert event["simulated"] is True and event["duration_seconds"] > 10
    assert event["links"] and max(event["links"].values()) >= 1


def test_disarmed_episode_is_logged_without_alarm(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.rt.sim.action("enter")
    latest = harness.run(15)
    assert latest["state"] in ("motion", "presence") and latest["alarm"] is False
    event = harness.rt.store.list_events()[0]
    assert event["state"] == "active" and event["alarm"] is False


def test_fan_does_not_alarm(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.rt.settings.arm_delay = 0
    harness.rt.arm(True)
    harness.rt.sim.action("fan")
    harness.run(60)
    assert not any(e["alarm"] for e in harness.rt.store.list_events())


def test_exit_delay_ignores_the_leaving_owner(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.rt.settings.arm_delay = 60
    harness.rt.sim.action("enter")
    harness.run(5)
    harness.rt.arm(True)  # owner arms while still in the room, then walks out
    harness.rt.sim.action("leave")
    harness.run(55)
    assert harness.rt.latest["arming"]["active"] is False or not harness.rt.latest["alarm"]
    assert not any(e["alarm"] for e in harness.rt.store.list_events())


def test_api_roundtrip(demo_cfg):
    demo_cfg["alarm"]["arm_delay_seconds"] = 0
    with TestClient(create_app(demo_cfg)) as client:
        status = client.get("/api/status").json()
        assert status["simulated"] and len(status["layout"]["links"]) == 3
        assert client.get("/health").status_code == 200
        armed = client.post("/api/arm", json={"armed": True}).json()
        assert armed["armed"] and armed["active"]
        assert client.post("/api/arm", json={"armed": False}).json()["armed"] is False
        assert client.post("/api/sim", json={"action": "enter"}).json()["scene"] == "walk"
        assert client.post("/api/sim", json={"action": "dance"}).status_code == 422
        assert client.post("/api/calibrate", json={"seconds": 5}).status_code == 422
        assert client.post("/api/calibrate", json={"seconds": 20}).json() == {"seconds": 20}
        assert client.post("/api/calibrate", json={"cancel": True}).json() == {"cancelled": True}
        assert client.post("/api/notify-test").json() == {"result": "disabled"}
        assert client.get("/api/events").json()["events"] == []
        assert client.get("/api/live?seconds=30").status_code == 200
        page = client.get("/")
        assert page.status_code == 200 and "WiFi jelenlét" in page.text
        assert client.get("/static/vendor/three/three.module.js").headers["content-type"].startswith("text/javascript")
        # Arming needs a JSON body: a plain form post from another website is rejected.
        assert client.post("/api/arm", data={"armed": "true"}).status_code == 422


def test_settings_survive_restart(tmp_path):
    from wifisense.settings import Settings
    now = [100.0]
    settings = Settings(tmp_path / "s.json", arm_delay=30, clock=lambda: now[0])
    assert settings.set_armed(True)["active"] is False
    now[0] = 131
    reloaded = Settings(tmp_path / "s.json", arm_delay=30, clock=lambda: now[0])
    assert reloaded.arming() == {"armed": True, "active": True, "armed_at": 100.0, "active_at": 130.0, "delay_seconds": 30}


def test_calibration_closes_an_open_episode(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.rt.sim.action("enter")
    harness.run(10)
    assert harness.rt.tracker.episode is not None
    harness.rt.start_calibration(20)
    harness.run(2)
    event = harness.rt.store.list_events()[0]
    assert event["state"] == "closed" and event["end_reason"] == "calibrating" and event["duration_seconds"] > 5


def test_shutdown_closes_the_open_episode(harness):
    harness.run(5)
    harness.calibrate(30)
    harness.rt.sim.action("enter")
    harness.run(10)
    rt = harness.rt
    for source in rt.sources:
        source.close = lambda timeout=3: None  # never started in the harness
    rt.worker.close = lambda timeout=15: True
    store_close, rt.store.close = rt.store.close, lambda: None
    rt.close()
    event = rt.store.list_events()[0]
    store_close()
    assert event["state"] == "closed" and event["end_reason"] == "shutdown" and event["duration_seconds"] > 5
